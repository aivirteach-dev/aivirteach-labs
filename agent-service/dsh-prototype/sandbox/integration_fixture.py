"""虚构平台/学生绑定；仅测试使用，正式模块不能依赖本文件。

替换的是现有推理传输，不另造Agent工作流。unused配置确保漏替换时失败，
不会意外访问Mac。令牌随机生成，不写报告，不模拟成正式平台JWT。
"""
from datetime import datetime,timezone
from pathlib import Path
from uuid import uuid4
import secrets
from dsh_tutor.agent_service import AgentService
from dsh_tutor.contracts import CourseSnapshot
from dsh_tutor.operation_journal import OperationJournal
from dsh_tutor.resources import ResourceManagedTutor
from dsh_tutor.service_contracts import TrustedBinding,API_VERSION
from dsh_tutor.storage import SQLiteChatStore
from dsh_tutor.conversation import ConversationScope


def fake_model(prompt,**kwargs):
    return {'answer':'这是虚构课程的测试回答。','model':'simulation-only','run_id':uuid4().hex,
        'run_started_at':datetime.now(timezone.utc).isoformat(),'elapsed_seconds':.001}


class Fixture:
    def __init__(self,root,*,settings=None,policy=None,reopen=False):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.store=SQLiteChatStore(self.root/'chat.db') if reopen else SQLiteChatStore.create(self.root/'chat.db')
        self.course=CourseSnapshot(course_id='course-a',version='v1',lesson_id='l1',title='虚构课程')
        self.bindings={};self.tokens={}
        for name,os_name in [('A','linux'),('B','windows'),('C','linux'),('D','windows'),('E','linux'),('F','linux')]:
            student='demo-student-'+name;context='demo-context-'+name
            h=self.store.current(student_id=student,learning_context_id=context) if reopen else self.store.activate(
                ConversationScope(student_id=student,learning_context_id=context,course_id='course-a',course_version='v1',lesson_id='l1',
                    instance_id='vm-'+name,instance_generation='g1',environment_os=os_name))
            self.bindings[name]=TrustedBinding(self.store,h,self.course,lab_id='demo-lab')
            self.tokens[name]=secrets.token_urlsafe(32)
        self.tutor=ResourceManagedTutor(policy={'min_free_bytes':0,**(policy or {})},config_path='unused/not-a-model-config',state_dir=self.root)
        self.journal=OperationJournal(self.root/'operations.db',settings) if reopen else OperationJournal.create(self.root/'operations.db',settings)
        self.service=AgentService(self.tutor,self.journal)

    def resolve(self,token):
        for name,known in self.tokens.items():
            if secrets.compare_digest(known,token):return self.bindings[name]
        return None

    def request(self,rid='example-1',question='本课的学习目标是什么？'):
        return {'api_version':API_VERSION,'payload':{'request_id':rid,'question':question,'course':self.course.model_dump()}}

    def close(self):
        if not self.service.close(timeout=3):raise RuntimeError('simulation_still_running')
