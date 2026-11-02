"""阶段1有限演示：DSH发往本机假模型，4操作/最多3个模拟提供方请求。"""
from dataclasses import replace
from pathlib import Path
from threading import Event,Thread
from uuid import uuid4
import json,os,pwd,time
from dsh_tutor.agent_http import create_agent_server
from dsh_tutor.diagnostics import DiagnosticStore
from dsh_tutor.lifecycle import migrate_lifecycle
from dsh_tutor.task_memory import migrate_task_memory,TaskRepository
from dsh_tutor.runtime import load_config
from sandbox.integration_fixture import Fixture
from sandbox.integration_client import PlatformClient
from sandbox.mcp_acceptance import bridge_for
from sandbox.efficiency_fixture import fake_provider


class EfficiencyDemo:
    def __init__(self,project):
        self.project=Path(project);self.root=self.project/'sandbox/results/efficiency-demo'/uuid4().hex
        self.fixture=Fixture(self.root);self.service=self.fixture.service;store=self.fixture.store
        store.migrate_learning_memory();migrate_task_memory(store);migrate_lifecycle(store)
        repo=TaskRepository(store)
        for name,binding in self.fixture.bindings.items():
            repo.create(binding.handle,'demo-task','虚构课程任务')
            self.fixture.bindings[name]=replace(binding,store=repo.bind(binding.handle,'demo-task'))
        self.fixture.tutor.diagnostics=DiagnosticStore(store);self.database=store.path
        self.server=create_agent_server(self.service,self.fixture.resolve,port=0)
        self.stop=Event();self.done=Event();self.error_code=None;self.results={};self.mode={'value':'text'}

    def start(self):
        self.provider=fake_provider(self.mode,delay=.15);url,self.received=self.provider.__enter__()
        config=load_config(self.project/'config.json')
        # 只在sandbox临时配置允许开发账号访问本机假服务，正式config仍为dsh-runner。
        config.update(ollama_base_url=url,execution_user=pwd.getpwuid(os.geteuid()).pw_name,total_timeout_seconds=20)
        config['execution_limits']['max_model_calls']=1
        cfg=self.root/'fake-provider-config.json';cfg.write_text(json.dumps(config))
        self.fixture.tutor.config_path=cfg
        self.service.verify_ready(upstream_idle=True)
        self.http=Thread(target=self.server.serve_forever,daemon=True);self.http.start()
        self.worker=Thread(target=self._traffic,daemon=True);self.worker.start();return self

    def client(self,name='A'):
        return PlatformClient('http://127.0.0.1:'+str(self.server.server_port),self.fixture.tokens[name])

    def wait(self,client,operation):
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            view=client.view(operation)
            if view['state'] in ('succeeded','failed','recovery_required'):return view
            time.sleep(.12)
        raise RuntimeError('simulation_timeout')

    def _traffic(self):
        try:
            a=self.client();body=self.fixture.request('efficiency-A')
            _,view=a.submit(body);_,replay=a.submit(body)
            if view['operation_id']!=replay['operation_id']:raise RuntimeError('duplicate')
            self.results['A']=self.wait(a,view['operation_id'])
            self.mode['value']='tool';binding=self.fixture.bindings['B'];h=binding.handle
            bridge=bridge_for(binding.course.course_id,'n8n','stopped',request_id='efficiency-B',instance_id=h.scope.instance_id)
            bridge['context']['policy'].update(student_id=h.scope.student_id,course_version=binding.course.version,learning_context_id=h.scope.learning_context_id,
                 lesson_id=h.scope.lesson_id,instance_generation=h.scope.instance_generation)
            self.fixture.bindings['B']=replace(binding,tool_bridge=bridge)
            b=self.client('B');_,view=b.submit(self.fixture.request('efficiency-B','请检查n8n。'))
            self.results['B']=self.wait(b,view['operation_id'])
            self.mode.update(value='text',hold=Event(),entered=Event())
            c=self.client('C');_,v=c.submit(self.fixture.request('efficiency-C'))
            if not self.mode['entered'].wait(5):raise RuntimeError('fake_provider_not_entered')
            d=self.client('D');_,cancel=d.submit(self.fixture.request('efficiency-D'));d.cancel(cancel['operation_id'])
            self.results['D']=self.wait(d,cancel['operation_id'])
            self.mode['hold'].set();self.results['C']=self.wait(c,v['operation_id'])
        except Exception:self.error_code='efficiency_demo_failed'
        finally:
            if self.mode.get('hold') is not None:self.mode['hold'].set()
            self.done.set()

    def close(self):
        self.stop.set();self.worker.join(30)
        self.server.shutdown();self.server.server_close();self.http.join(2)
        try:self.fixture.close()
        finally:self.provider.__exit__(None,None,None)
