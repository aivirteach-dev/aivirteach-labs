"""管理面板接线演示：真实HTTP/调度/SQLite，有限虚构流量，无Mac调用。"""
from pathlib import Path
from dataclasses import replace
from threading import Event,Thread
from unittest.mock import patch
from uuid import uuid4
import time
from dsh_tutor.agent_http import create_agent_server
from dsh_tutor.diagnostics import DiagnosticStore
from dsh_tutor.lifecycle import migrate_lifecycle
from dsh_tutor.task_memory import migrate_task_memory,TaskRepository
from sandbox.integration_fixture import Fixture,fake_model
from sandbox.integration_client import PlatformClient,ClientError


class IntegrationDemo:
    def __init__(self,root):
        self.root=Path(root)/'sandbox/results/integration-demo'/uuid4().hex
        self.fixture=Fixture(self.root)
        store=self.fixture.store;store.migrate_learning_memory();migrate_task_memory(store);migrate_lifecycle(store)
        # v3起禁止未绑定任务的问答；演示也走与真实宿主相同的显式任务入口。
        repo=TaskRepository(store)
        for name,binding in self.fixture.bindings.items():
            repo.create(binding.handle,'demo-task','虚构课程任务')
            self.fixture.bindings[name]=replace(binding,store=repo.bind(binding.handle,'demo-task'))
        self.fixture.tutor.diagnostics=DiagnosticStore(store)
        self.database=self.root/'chat.db';self.service=self.fixture.service
        self.server=create_agent_server(self.service,self.fixture.resolve,port=0)
        self.stop=Event();self.mock=None;self.error_code=None

    def start(self):
        def slow_model(*a,**kw):time.sleep(.4);return fake_model(*a,**kw)
        self.mock=patch('dsh_tutor.runtime.run_prompt',side_effect=slow_model);self.mock.start()
        self.service.verify_ready(upstream_idle=True)
        self.http=Thread(target=self.server.serve_forever,daemon=True);self.http.start()
        self.worker=Thread(target=self._traffic,daemon=True);self.worker.start();return self

    def _traffic(self):
        base='http://127.0.0.1:'+str(self.server.server_port)
        try:
            for name in 'ABCDE':
                if self.stop.is_set():break
                client=PlatformClient(base,self.fixture.tokens[name]);body=self.fixture.request('demo-'+name)
                _,view=client.submit(body)
                # 第二份提交模拟响应丢失后的重发；应返回同一编号。
                _,replay=client.submit(body)
                if replay['operation_id']!=view['operation_id']:raise RuntimeError('duplicate_operation')
                if name=='B':client.cancel(view['operation_id'])
        except (ClientError,RuntimeError,OSError):self.error_code='simulation_client_failed'

    def close(self):
        self.stop.set();self.worker.join(4)
        self.server.shutdown();self.server.server_close();self.http.join(1)
        try:self.fixture.close()
        finally:
            if self.mock is not None:self.mock.stop()
