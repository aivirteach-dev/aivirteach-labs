"""阶段2有限流量：真实教材/SQLite/HTTP/DSH，模型仅连接本机固定假提供方。"""
from dataclasses import replace
from dsh_tutor.materials import MaterialRepository
from dsh_tutor.task_memory import TaskRepository
from sandbox.efficiency_demo import EfficiencyDemo
from sandbox.mcp_acceptance import bridge_for
import json

class LowCostDemo(EfficiencyDemo):
    def __init__(self,project):
        super().__init__(project)
        self.repository=MaterialRepository(self.project/'materials/courses')
        report=json.loads((self.project/'development/materials-import-20261029.json').read_text());version=report['version']
        self.material_version=version;lesson='lesson-1-2-install-and-start-n8n'
        for name,locale in [('A','zh-CN'),('B','en')]:
            b=self.fixture.bindings[name];h=self.fixture.store.activate(b.handle.scope.model_copy(update={'course_id':'ai-daily-briefing','course_version':version,'lesson_id':lesson}),expected_revision=b.handle.revision)
            repo=TaskRepository(self.fixture.store);repo.create(h,'material-task','真实教材问答')
            course=self.repository.prepare('ai-daily-briefing',version,locale,lesson,'n8n port command')
            self.fixture.bindings[name]=replace(b,store=repo.bind(h,'material-task'),handle=h,course=course,material_repository=self.repository)
        b=self.fixture.bindings['C'];b.store.edit_learning(b.handle,'goal','练习AI课程中的定时工作流',expected_revision=0,request_id='seed-goal')
        b=self.fixture.bindings['D'];h=b.handle.scope
        bridge=bridge_for(h.course_id,'n8n','stopped',request_id='low-service',instance_id=h.instance_id)
        bridge['context']['policy'].update(student_id=h.student_id,learning_context_id=h.learning_context_id,lesson_id=h.lesson_id,instance_generation=h.instance_generation)
        async def executor(service,policy):return {'state':'stopped','source':'simulation','note':'固定虚构观察，不访问真实VM。'}
        self.fixture.bindings['D']=replace(b,tool_bridge=bridge,service_executor=executor)

    def action(self,name,rid,kind,**fields):
        return {'api_version':'dsh-tutor.v1','profile':'course_actions_v1','history_selection':{'mode':'none'},
          'payload':{'request_id':rid,'course':self.fixture.bindings[name].course.model_dump(),'action':{'kind':kind,**fields}}}
    def send(self,name,body):
        client=self.client(name);_,view=client.submit(body);return self.wait(client,view['operation_id'])
    def _traffic(self):
        try:
            for name,key in [('A','material_zh'),('B','material_en')]:
                b=self.fixture.bindings[name];c=b.course;lesson=self.repository.lesson(c.course_id,c.version,c.locale,c.lesson_id)
                block=next(x for x in lesson['blocks'] if x['kind']=='code' and len(x['text'].encode())<=6000)
                body=self.action(name,'low-'+name,'material_block',block_id=block['id']);self.results[key]=self.send(name,body)
                before=len(self.received);self.send(name,body)
                if len(self.received)!=before:raise RuntimeError('replay_called_provider')
            self.results['goal']=self.send('C',self.action('C','low-goal','learning_goal'))
            self.results['service']=self.send('D',self.action('D','low-service','service_status',service='n8n'))
            b=self.fixture.bindings['E'];body={'api_version':'dsh-tutor.v1','payload':{'request_id':'low-open','question':'为什么要先检查环境？','course':b.course.model_dump()},'history_selection':{'mode':'none'}}
            self.results['open_question']=self.send('E',body)
        except Exception:self.error_code='low_cost_demo_failed'
        finally:self.done.set()
