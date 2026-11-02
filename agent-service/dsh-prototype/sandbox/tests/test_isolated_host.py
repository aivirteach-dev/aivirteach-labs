"""隔离装配回归：默认暂停、独占、篡改拒绝、只读探针、容器绑定和固定假模型边界。"""
import hashlib
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen

from dsh_tutor.isolated_host import initialize,IsolatedHost,settings,control_server
from dsh_tutor.service_contracts import ServiceError
from dsh_tutor.agent_http import create_agent_server
from dsh_tutor.management_http import create_management_server
from sandbox.isolated_provider import create_provider
from sandbox.integration_fixture import Fixture
from tools.isolated_setup import prepare
from tools.isolated_acceptance import request as acceptance_request


class IsolatedHostTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'private';prepare(self.root)
        self.state=self.root/'state';self.config=self.root/'config/host.json';self.host=None
        self.identity=patch('dsh_tutor.isolated_host.require_execution_identity');self.identity.start();self.addCleanup(self.identity.stop)
    def host_at(self,**kw):
        self.host=IsolatedHost(self.state,self.config,'m'*48,**kw)
        self.addCleanup(lambda:self.host.service.close());return self.host
    def token(self,name):return (self.root/'secrets'/(name+'.token')).read_text().strip()
    def test_setup_secrets_are_private_and_not_in_configuration(self):
        self.assertEqual(self.root.stat().st_mode&0o777,0o700)
        for p in (self.root/'secrets').iterdir():self.assertEqual(p.stat().st_mode&0o777,0o600)
        self.assertNotIn(self.token('test-a'),self.config.read_text())
    def test_setup_refuses_existing_directory(self):
        with self.assertRaises(ValueError):prepare(self.root)
    def test_missing_state_does_not_create_database(self):
        with self.assertRaises(FileNotFoundError):self.host_at()
        self.assertFalse((self.state/'chat.db').exists())
    def test_initialize_is_explicit_and_cannot_overwrite(self):
        initialize(self.state,self.config);before=(self.state/'chat.db').read_bytes()
        with self.assertRaises(ValueError):initialize(self.state,self.config)
        self.assertEqual(before,(self.state/'chat.db').read_bytes())
    def test_duplicate_test_tokens_rejected(self):
        value=json.loads(self.config.read_text());value['students'][1]['token_sha256']=value['students'][0]['token_sha256'];self.config.write_text(json.dumps(value))
        with self.assertRaises(ValueError):settings(self.config)
    def test_config_tamper_rejected_before_service(self):
        initialize(self.state,self.config);self.config.write_text(self.config.read_text()+' ')
        with self.assertRaisesRegex(ValueError,'initialized_config_changed'):self.host_at()
    def test_runtime_cannot_be_switched_to_real_model(self):
        initialize(self.state,self.config);path=self.state/'runtime.json';c=json.loads(path.read_text());c['ollama_base_url']='http://example.invalid/v1';path.write_text(json.dumps(c))
        with self.assertRaisesRegex(ValueError,'isolated_runtime_changed'):self.host_at()
    def test_students_have_distinct_task_bindings_and_no_tools(self):
        initialize(self.state,self.config);host=self.host_at()
        a=host.resolve(self.token('test-a'));b=host.resolve(self.token('test-b'))
        self.assertNotEqual(a.handle.scope.student_id,b.handle.scope.student_id)
        self.assertIsNone(a.tool_bridge);self.assertIsNone(host.resolve('not-authorized'))
        self.assertIsNotNone(a.store.task_id)
    def test_start_paused_verify_requires_actual_trusted_idle_check(self):
        initialize(self.state,self.config);host=self.host_at(upstream_check=lambda:False)
        self.assertFalse(host.readiness()['ready'])
        with self.assertRaises(ServiceError):host.verify()
        self.assertFalse(host.readiness()['ready'])
        host.upstream_check=lambda:True;self.assertEqual(host.verify()['status'],'ready')
    def test_probe_does_not_call_upstream_and_restart_is_paused(self):
        initialize(self.state,self.config);host=self.host_at(upstream_check=lambda:(_ for _ in ()).throw(AssertionError('probe called model')))
        for _ in range(20):self.assertFalse(host.readiness()['ready'])
        self.assertTrue(host.service.close());self.host=IsolatedHost(self.state,self.config,'m'*48,upstream_check=lambda:True)
        self.assertFalse(self.host.readiness()['ready'])
    def test_second_process_owner_rejected(self):
        initialize(self.state,self.config);self.host_at()
        with self.assertRaises(ServiceError) as context:IsolatedHost(self.state,self.config,'m'*48)
        self.assertEqual(context.exception.code,'journal_owned')
    def test_local_control_rejects_unknown_command_and_socket_is_private(self):
        initialize(self.state,self.config);host=self.host_at(upstream_check=lambda:True)
        server=control_server(host);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            self.assertEqual((self.state/'control.sock').stat().st_mode&0o777,0o600)
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as c:
                c.settimeout(3);c.connect(str(self.state/'control.sock'));c.sendall(b'{"command":"reset_database"}\n')
                self.assertEqual(json.loads(c.recv(2048))['status'],'paused')
            self.assertFalse(host.readiness()['ready'])
        finally:server.shutdown();server.server_close();thread.join(2)


class IsolatedTransportTests(unittest.TestCase):
    def test_restart_proxy_html_is_retryable_status_not_json_success(self):
        error=HTTPError('http://127.0.0.1:18792/health',502,'Bad Gateway',{},io.BytesIO(b'<html>private proxy detail</html>'))
        with patch('tools.isolated_acceptance.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect=error
            code,body=acceptance_request('/health')
        self.assertEqual(code,502);self.assertEqual(body,{'error':'non_json_upstream_error'})
    def test_acceptance_response_body_is_bounded(self):
        error=HTTPError('http://127.0.0.1:18792/health',502,'Bad Gateway',{},io.BytesIO(b'x'*65537))
        with patch('tools.isolated_acceptance.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect=error
            with self.assertRaisesRegex(RuntimeError,'too_large'):acceptance_request('/health')
    def request(self,server,path,headers=None):
        try:
            with urlopen(Request(f'http://127.0.0.1:{server.server_port}'+path,headers=headers or {}),timeout=3) as r:return r.status,json.load(r)
        except HTTPError as e:return e.code,json.load(e)
    def test_container_bind_requires_explicit_isolated_profile(self):
        with tempfile.TemporaryDirectory() as d:
            fixture=Fixture(d)
            try:
                with self.assertRaises(ValueError):create_agent_server(fixture.service,fixture.resolve,0,bind_host='0.0.0.0')
            finally:fixture.close()
    def test_health_has_no_auth_or_model_side_effect_but_api_still_protected(self):
        with tempfile.TemporaryDirectory() as d:
            fixture=Fixture(d,settings={'profile':'isolated_test'})
            server=create_agent_server(fixture.service,lambda _:(_ for _ in ()).throw(AssertionError('health resolved user')),0,bind_host='0.0.0.0',health=lambda:{'ready':False})
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                self.assertEqual(self.request(server,'/health')[0],200)
                self.assertEqual(self.request(server,'/ready')[0],503)
                self.assertEqual(self.request(server,'/v1/tutor/capabilities')[0],401)
                self.assertEqual(self.request(server,'/health',{'Host':'evil.invalid'})[0],403)
            finally:server.shutdown();server.server_close();thread.join(2);fixture.close()
    def test_management_extra_host_requires_isolation_and_still_requires_token(self):
        view=SimpleNamespace(overview=lambda:{'scope':'test'})
        with self.assertRaises(ValueError):create_management_server(view,'m'*48,0,bind_host='0.0.0.0')
        server=create_management_server(view,'m'*48,0,bind_host='0.0.0.0',isolated_test=True,allowed_hosts=('agent:18788',))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            self.assertEqual(self.request(server,'/v1/management/overview',{'Host':'agent:18788'})[0],401)
            self.assertEqual(self.request(server,'/v1/management/overview',{'Host':'agent:18788','Authorization':'Bearer '+'m'*48})[0],200)
        finally:server.shutdown();server.server_close();thread.join(2)
    def test_fake_probe_counts_no_model_requests_and_rejects_nonstream(self):
        server=create_provider();thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            self.assertEqual(self.request(server,'/stats')[1]['requests'],0)
            req=Request(f'http://127.0.0.1:{server.server_port}/v1/chat/completions',data=b'{"stream":false}',headers={'Content-Type':'application/json'})
            with self.assertRaises(HTTPError):urlopen(req,timeout=2)
            self.assertEqual(self.request(server,'/stats')[1]['requests'],0)
        finally:server.shutdown();server.server_close();thread.join(2)
