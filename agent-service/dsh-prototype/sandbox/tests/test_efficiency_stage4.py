"""阶段4：验证隔离、所有权、取消终态、原生卸载及适配边界，不访问真实模型。"""
import json,os,threading,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from dsh_tutor.providers import ProviderSpec,Capabilities,provider_for,provider_patch
from dsh_tutor.runtime import load_config,ROOT,render_patch,run_prompt
from dsh_tutor.execution_budget import RuntimeFailure
from dsh_tutor.execution_control import ModelOwnership,execution_control,current_control
from dsh_tutor.model_lifecycle import OllamaLifecycle
from sandbox.integration_fixture import Fixture
from sandbox.runtime_optimization_acceptance import comparable

class ProviderTests(unittest.TestCase):
    def spec(self,**kw):return ProviderSpec.model_validate({'id':'server','kind':'openai_compatible','base_url':'https://example.invalid/v1','auth_env':'TUTOR_FAKE_KEY',**kw})
    def test_unknown_capabilities_are_not_zero(self):
        c=self.spec().capabilities
        self.assertEqual(c.usage,'unknown');self.assertIsNone(c.concurrency);self.assertEqual(c.remote_cancel,'unknown')
    def test_no_unsupported_claim_of_remote_cancel(self):
        with self.assertRaises(ValueError):Capabilities(remote_cancel='supported')
    def test_secrets_rejected_in_url(self):
        for address in ('https://key@example.invalid/v1','https://example.invalid/v1?key=abc','https://example.invalid/v1#key','file:///v1'):
            with self.subTest(address=address),self.assertRaises(ValueError):self.spec(base_url=address)
    def test_redirects_and_extra_paths_cannot_hide_in_config(self):
        for address in ('https://example.invalid/v1/','https://example.invalid/v2','https://example.invalid:abc/v1'):
            with self.subTest(address=address),self.assertRaises(ValueError):self.spec(base_url=address)
    def test_secret_field_is_not_accepted(self):
        with self.assertRaises(ValueError):self.spec(api_key='do-not-serialize')
    def test_missing_auth_is_pre_dispatch_failure(self):
        with patch.dict(os.environ,{},clear=True),self.assertRaisesRegex(ValueError,'provider_auth_unavailable'):self.spec().environment()
    def test_public_summary_and_patch_do_not_include_key(self):
        p=self.spec()
        with patch.dict(os.environ,{'TUTOR_FAKE_KEY':'fake-test-secret'}):
            self.assertEqual(p.environment()['TUTOR_FAKE_KEY'],'fake-test-secret')
            c=load_config(ROOT/'config.json');c['provider']=p.model_dump()
            value=json.dumps(p.public())+render_patch(c)
            self.assertNotIn('fake-test-secret',value);self.assertIn('TUTOR_FAKE_KEY',value)
    def test_invalid_auth_name_and_reserved_names(self):
        for n in ('A-B','DSH_HOME','OLLAMA_PLACEHOLDER_KEY'):
            with self.subTest(name=n),self.assertRaises(ValueError):self.spec(auth_env=n)
    def test_legacy_route_and_budget_remain(self):
        c=load_config(ROOT/'config.json');p=provider_for(c)
        self.assertEqual(p.id,'ollama-local');self.assertEqual(c['execution_limits']['max_model_calls'],4)
        q=provider_patch(c)['config']['providers'][p.id]
        self.assertEqual(q['retryPolicy']['maxRetries'],0);self.assertEqual(q['models'][0]['compat']['maxTokensField'],'max_tokens')
    def test_explicit_second_protocol_and_context_window(self):
        c=load_config(ROOT/'config.json');c['provider']=self.spec(max_tokens_field='max_completion_tokens',developer_role=True).model_dump()
        p=provider_patch(c)['config']['providers']['server']['models'][0]
        self.assertEqual(p['compat'],{'supportsDeveloperRole':True,'maxTokensField':'max_completion_tokens'})
        self.assertEqual(p['contextWindow'],c['context_window'])
    def test_business_input_comparison_only_normalizes_runtime_directory(self):
        a={'messages':[{'role':'user','content':'教材A 目标A'},{'role':'user','content':'Current runtime context. This snapshot supersedes earlier runtime-context snapshots.\n\nCurrent working directory: "/a".'}]}
        b=json.loads(json.dumps(a));b['messages'][1]['content']=b['messages'][1]['content'].replace('/a','/b')
        self.assertEqual(comparable(a),comparable(b))
        b['messages'][0]['content']='教材A 目标B';self.assertNotEqual(comparable(a),comparable(b))

class OwnershipTests(unittest.TestCase):
    def setUp(self):self.now=[10.0];self.owner=ModelOwnership(2,clock=lambda:self.now[0])
    def test_capacity_shared_across_students(self):
        a=self.owner.claim('student-A',seconds=2);b=self.owner.claim('student-B',seconds=2)
        with self.assertRaisesRegex(RuntimeError,'model_capacity'):self.owner.claim('student-C',seconds=2)
        self.owner.settle(a,known_end=True);self.owner.claim('student-C',seconds=2)
        self.assertEqual(self.owner.snapshot()['active'],2)
    def test_duplicate_request_owner_rejected(self):
        self.owner.claim('same-request',seconds=2)
        with self.assertRaisesRegex(RuntimeError,'duplicate'):self.owner.claim('same-request',seconds=2)
    def test_cancel_does_not_free_capacity(self):
        a=self.owner.claim('a',seconds=2);self.owner.cancel(a)
        self.assertEqual(self.owner.snapshot()['active'],1)
        with self.assertRaises(RuntimeError):self.owner.renew(a,seconds=2)
    def test_expiry_quarantines_instead_of_releasing(self):
        a=self.owner.claim('a',seconds=2);self.now[0]+=3
        self.assertTrue(self.owner.snapshot()['quarantined']);self.assertEqual(self.owner.snapshot()['active'],1)
        with self.assertRaises(RuntimeError):self.owner.claim('b',seconds=2)
    def test_renew_keeps_original_owner(self):
        a=self.owner.claim('a',seconds=2);self.now[0]+=1;self.owner.renew(a,seconds=3);self.now[0]+=2
        self.assertFalse(self.owner.snapshot()['quarantined']);self.owner.settle(a,known_end=True)
    def test_unknown_end_blocks_future_model_even_after_local_release(self):
        a=self.owner.claim('a',seconds=2);self.owner.settle(a,known_end=False)
        self.assertEqual(self.owner.snapshot()['active'],0)
        with self.assertRaises(RuntimeError):self.owner.claim('b',seconds=2)
        with self.assertRaises(RuntimeError):self.owner.verify_idle(upstream_idle=False)
        self.owner.verify_idle(upstream_idle=True);self.owner.claim('b',seconds=2)
    def test_operator_cannot_clear_active_lease(self):
        self.owner.claim('a',seconds=2)
        with self.assertRaises(RuntimeError):self.owner.verify_idle(upstream_idle=True)
    def test_settle_cannot_be_repeated(self):
        a=self.owner.claim('a',seconds=2);self.owner.settle(a,known_end=True)
        with self.assertRaises(RuntimeError):self.owner.settle(a,known_end=True)
    def test_control_and_inference_are_mutually_exclusive(self):
        with self.owner.maintenance():
            with self.assertRaises(RuntimeError):self.owner.claim('a',seconds=2)
            with self.assertRaises(RuntimeError):
                with self.owner.maintenance():pass
        a=self.owner.claim('a',seconds=2)
        with self.assertRaises(RuntimeError):
            with self.owner.maintenance():pass
        self.owner.settle(a,known_end=True)
    def test_control_timeout_or_error_quarantines_model(self):
        with self.assertRaises(TimeoutError):
            with self.owner.maintenance():raise TimeoutError()
        self.assertTrue(self.owner.snapshot()['quarantined'])
    def test_request_context_does_not_leak_across_threads(self):
        e=threading.Event();e.set();other=[]
        with execution_control(e):
            self.assertTrue(current_control().requested)
            t=threading.Thread(target=lambda:other.append(current_control()));t.start();t.join()
        self.assertEqual(other,[None]);self.assertIsNone(current_control())
    def test_cancel_before_dispatch_requires_no_config_or_process(self):
        e=threading.Event();e.set()
        with execution_control(e),self.assertRaises(RuntimeFailure) as caught:run_prompt('x',config_path='does-not-exist',state_dir='unused')
        self.assertEqual(caught.exception.code,'cancelled_before_dispatch');self.assertTrue(caught.exception.known_end)
    def test_bad_provider_configuration_does_not_quarantine_as_unknown(self):
        with self.assertRaises(RuntimeFailure) as caught:run_prompt('x',config_path='does-not-exist',state_dir='unused')
        self.assertEqual(caught.exception.code,'provider_config_rejected');self.assertTrue(caught.exception.known_end)

@contextmanager
def control_server(mode):
    rows=[]
    class H(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));rows.append((self.path,body))
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
            self.wfile.write(json.dumps({'model':body['model'],'done':True,'done_reason':('stop' if mode=='bad' else 'unload' if body['keep_alive']==0 else 'load')}).encode())
    s=ThreadingHTTPServer(('127.0.0.1',0),H);t=threading.Thread(target=s.serve_forever,daemon=True);t.start()
    try:yield 'http://127.0.0.1:'+str(s.server_port)+'/v1',rows
    finally:s.shutdown();s.server_close();t.join()

class LifecycleTests(unittest.TestCase):
    def test_busy_and_recent_model_skip_unload_without_http(self):
        now=[0.0];owner=ModelOwnership(clock=lambda:now[0])
        with control_server('ok') as (url,rows):
            c=OllamaLifecycle({'ollama_base_url':url,'model':'fake'},owner,idle_seconds=2)
            self.assertEqual(c.unload_if_idle(),'deferred');a=owner.claim('a',seconds=10);now[0]=5
            self.assertEqual(c.unload_if_idle(),'deferred');self.assertEqual(rows,[])
            owner.settle(a,known_end=True);now[0]=8;self.assertEqual(c.unload_if_idle(),'unload_confirmed')
            self.assertEqual(rows,[('/api/generate',{'model':'fake','prompt':'','stream':False,'keep_alive':0})])
    def test_warm_is_shared_and_no_student_prompt_is_sent(self):
        owner=ModelOwnership()
        with control_server('ok') as (url,rows):
            c=OllamaLifecycle({'ollama_base_url':url,'model':'fake'},owner,idle_seconds=5);c.warm()
            self.assertEqual(c.snapshot()['state'],'warm');self.assertEqual(rows[0][1]['prompt'],'');self.assertEqual(rows[0][1]['keep_alive'],5)
    def test_http_success_without_unload_receipt_stays_unknown(self):
        now=[0.0];owner=ModelOwnership(clock=lambda:now[0]);now[0]=4
        with control_server('bad') as (url,rows):
            c=OllamaLifecycle({'ollama_base_url':url,'model':'fake'},owner,idle_seconds=2)
            with self.assertRaisesRegex(RuntimeError,'unconfirmed'):c.unload_if_idle()
            self.assertTrue(owner.snapshot()['quarantined']);self.assertEqual(c.snapshot()['state'],'unknown')
    def test_unknown_provider_has_no_ollama_control_fallback(self):
        with self.assertRaises(ValueError):OllamaLifecycle({'model':'x','provider':{'id':'x','kind':'openai_compatible','base_url':'https://x.invalid/v1','auth_env':'TUTOR_KEY'}},ModelOwnership())
    def test_snapshot_never_makes_network_request(self):
        with control_server('ok') as (url,rows):
            c=OllamaLifecycle({'ollama_base_url':url,'model':'fake'},ModelOwnership())
            for _ in range(20):self.assertEqual(c.snapshot()['hardware_memory'],'not_measured')
            self.assertEqual(rows,[])
    def test_service_control_not_exposed_and_needs_explicit_configuration(self):
        with tempfile.TemporaryDirectory() as d:
            f=Fixture(d)
            try:
                with self.assertRaises(Exception):f.service.maintain_model('warm')
                self.assertIsNone(f.tutor.model_lifecycle)
                with self.assertRaises(ValueError):f.service.maintain_model('arbitrary_shell')
            finally:f.close()


class ServiceCancellationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.f=Fixture(self.tmp.name);self.s=self.f.service;self.b=self.f.bindings['A'];self.s.verify_ready(upstream_idle=True)
    def tearDown(self):self.f.close();self.tmp.cleanup()
    def done(self,op):
        from sandbox.tests.test_resources import wait_for
        wait_for(lambda:self.s.metrics()['active_operations']==0)
        return self.s.view(self.b,op)
    def test_known_pre_dispatch_failure_keeps_model_available(self):
        with patch('dsh_tutor.runtime.run_prompt',side_effect=RuntimeFailure('provider_request_rejected',known_end=True)):
            view,_=self.s.submit(self.b,self.f.request());view=self.done(view['operation_id'])
        self.assertEqual(view['error_code'],'provider_request_rejected');self.assertTrue(self.s.metrics()['ready'])
        self.assertFalse(self.f.tutor.model_owner.snapshot()['quarantined'])
    def test_unknown_remote_failure_quarantines_and_requires_verification(self):
        with patch('dsh_tutor.runtime.run_prompt',side_effect=RuntimeFailure('provider_outcome_unknown')) as run:
            view,_=self.s.submit(self.b,self.f.request());view=self.done(view['operation_id'])
            with self.assertRaises(Exception):self.s.submit(self.b,self.f.request('second'))
            self.assertEqual(run.call_count,1)
        self.assertEqual(view['error_code'],'provider_outcome_unknown');self.assertFalse(self.s.metrics()['ready'])
        self.assertTrue(self.f.tutor.model_owner.snapshot()['quarantined'])
        self.s.verify_ready(upstream_idle=True);self.assertTrue(self.s.metrics()['ready'])
    def test_late_success_after_cancel_is_saved_once(self):
        from sandbox.integration_fixture import fake_model
        entered=threading.Event();release=threading.Event()
        def model(*a,**kw):entered.set();release.wait(3);return fake_model(*a,**kw)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=model) as run:
            view,_=self.s.submit(self.b,self.f.request());self.assertTrue(entered.wait(2));op=view['operation_id']
            self.s.cancel(self.b,op);release.set();v=self.done(op)
            self.assertEqual(v['state'],'succeeded');self.assertEqual(v['execution_cancellation'],'late_success')
            self.assertEqual(v['cancellation'],'completed_after_cancel');self.assertEqual(run.call_count,1)
            _,created=self.s.submit(self.b,self.f.request());self.assertFalse(created);self.assertEqual(run.call_count,1)
    def test_cancel_unknown_never_reported_not_running(self):
        entered=threading.Event();release=threading.Event()
        def model(*a,**kw):entered.set();release.wait(3);raise RuntimeFailure('cancel_unconfirmed')
        with patch('dsh_tutor.runtime.run_prompt',side_effect=model):
            v,_=self.s.submit(self.b,self.f.request());self.assertTrue(entered.wait(2));self.s.cancel(self.b,v['operation_id']);release.set();v=self.done(v['operation_id'])
        self.assertEqual(v['cancellation'],'requested_unconfirmed');self.assertEqual(v['execution_cancellation'],'outcome_unknown')
        self.assertFalse(self.s.metrics()['ready'])
    def test_failed_remote_state_survives_reopen_as_startup_pause(self):
        with patch('dsh_tutor.runtime.run_prompt',side_effect=RuntimeFailure('provider_outcome_unknown')):
            v,_=self.s.submit(self.b,self.f.request());self.done(v['operation_id'])
        self.f.close();self.f=Fixture(self.tmp.name,reopen=True);self.s=self.f.service;self.b=self.f.bindings['A']
        self.assertFalse(self.s.metrics()['ready']);self.assertTrue(self.f.tutor.model_owner.snapshot()['quarantined'])
        v=self.s.view(self.b,v['operation_id']);self.assertEqual(v['error_code'],'provider_outcome_unknown')
    def test_readonly_model_config_does_not_probe_network(self):
        from dsh_tutor.management import ManagementView
        p=Path(self.tmp.name)/'fake-config.json';c=load_config(ROOT/'config.json');p.write_text(json.dumps(c))
        with patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('must_not_probe')):
            out=ManagementView(self.f.store.path,p,operations=self.s.metrics).configuration()
            self.assertEqual(out['provider']['id'],'ollama-local');self.assertEqual(out['actual_usage'],'unknown')
    def test_unsupported_tools_refused_before_process_launch(self):
        from sandbox.mcp_acceptance import bridge_for
        c=load_config(ROOT/'config.json');c['execution_user']=__import__('pwd').getpwuid(os.geteuid()).pw_name
        c['provider']={'id':'no-tools','kind':'ollama','base_url':'http://127.0.0.1:1/v1','capabilities':{'tools':'unsupported'}}
        p=Path(self.tmp.name)/'cfg.json';p.write_text(json.dumps(c))
        with patch('subprocess.Popen',side_effect=AssertionError('no_process')),self.assertRaises(RuntimeFailure) as e:
            run_prompt('x',config_path=p,state_dir=Path(self.tmp.name)/'runs',tool_bridge=bridge_for('c','n8n','stopped',request_id='r',instance_id='v'))
        self.assertEqual(e.exception.code,'provider_request_rejected');self.assertTrue(e.exception.known_end)

    def test_busy_control_is_deferred_without_pausing_valid_inference(self):
        from sandbox.integration_fixture import fake_model
        from dsh_tutor.service_contracts import ServiceError
        entered=threading.Event();release=threading.Event()
        def model(*a,**kw):entered.set();release.wait(3);return fake_model(*a,**kw)
        with control_server('ok') as (url,rows):
            self.f.tutor.configure_model_lifecycle({'ollama_base_url':url,'model':'fake'})
            with patch('dsh_tutor.runtime.run_prompt',side_effect=model):
                v,_=self.s.submit(self.b,self.f.request());self.assertTrue(entered.wait(2))
                try:
                    with self.assertRaises(ServiceError) as e:self.s.maintain_model('warm')
                    self.assertEqual(e.exception.code,'maintenance_busy');self.assertTrue(self.s.metrics()['ready']);self.assertEqual(rows,[])
                finally:release.set();self.done(v['operation_id'])
    def test_host_shared_warm_and_control_failure_have_distinct_outcomes(self):
        with control_server('ok') as (url,rows):
            self.f.tutor.configure_model_lifecycle({'ollama_base_url':url,'model':'fake'},idle_seconds=2)
            self.assertEqual(self.s.maintain_model('warm'),'warm_confirmed');self.assertEqual(len(rows),1)
            self.assertTrue(self.s.metrics()['ready'])
            self.assertEqual(self.s.maintain_model('unload_if_idle'),'deferred');self.assertEqual(len(rows),1)
    def test_hot_limit_reduction_keeps_existing_leases_and_blocks_new_claim(self):
        owner=ModelOwnership(2);a=owner.claim('a',seconds=10);b=owner.claim('b',seconds=10)
        with owner.lock:owner.capacity=1
        self.assertEqual(owner.snapshot()['active'],2)
        owner.settle(a,known_end=True)
        with self.assertRaises(RuntimeError):owner.claim('c',seconds=10)
        owner.settle(b,known_end=True);owner.claim('c',seconds=10)
    def test_expired_lease_checked_on_claim_without_polling_snapshot(self):
        now=[0.0];o=ModelOwnership(2,clock=lambda:now[0]);o.claim('a',seconds=1);now[0]=2
        with self.assertRaisesRegex(RuntimeError,'verification'):o.claim('b',seconds=1)
