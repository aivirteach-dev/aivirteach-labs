"""固定虚构库显式升级v5并准备历史查看样例；无真实模型、VM、Gateway或Shell操作。"""
from datetime import datetime,timezone
import json
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from dsh_tutor.storage import SQLiteChatStore
from dsh_tutor.task_memory import TaskRepository
from dsh_tutor.inspection_memory import migrate_inspections,InspectionRepository
from dsh_tutor.contracts import CourseQuestion
from dsh_tutor.chat_session import answer_chat
from sandbox.mcp_acceptance import bridge_for
from sandbox.inspection_fixtures import simulated_transport


def main():
    path=ROOT/'sandbox/results/task-memory-demo/fictional.db'
    store=SQLiteChatStore(path);h=store.current(student_id='fictional-student',learning_context_id='fictional-learning')
    migrate_inspections(store)
    if h.scope.instance_id is None:
        h=store.activate(h.scope.model_copy(update={'instance_id':'simulated-inspection-vm','instance_generation':'demo-g1','environment_os':'linux'}),expected_revision=h.revision)
    repo=TaskRepository(store);repo.create(h,'inspection-demo','历史检查追溯演示');bound=repo.bind(h,'inspection-demo')
    out=ROOT/'sandbox/results/inspection-demo';out.mkdir(parents=True,exist_ok=True,mode=0o700)
    cases=[]
    for case,failure in [('success',False),('failure',True)]:
        rid='inspection-'+case+'-'+uuid4().hex[:8];s=h.scope
        bridge=bridge_for(s.course_id,'n8n','running',request_id=rid,instance_id=s.instance_id)
        bridge['context']['policy'].update(course_version=s.course_version,student_id=s.student_id,learning_context_id=s.learning_context_id,lesson_id=s.lesson_id,instance_generation=s.instance_generation)
        q=CourseQuestion(request_id=rid,question='检查当前服务',course=dict(course_id=s.course_id,version=s.course_version,lesson_id=s.lesson_id,title='虚构历史演示'))
        with patch('dsh_tutor.runtime.run_prompt',side_effect=simulated_transport(bridge,out,failure=failure)):
            reply=answer_chat(q,store=bound,handle=h,config_path='unused',state_dir=out,tool_bridge=bridge)
        assert reply.state=='saved'
        record=InspectionRepository(store).get(h,'inspection-demo',rid)
        cases.append({'id':case,'request_id':rid,'run_id':reply.run_id,'record_status':record['record_status'],'always_historical':record['always_historical']})
    report={'at':datetime.now(timezone.utc).isoformat(),'scope':'固定虚构库，显式v5，新增独立演示任务；原任务不删不覆盖。',
            'actual_model_requests':0,'substituted_transport_calls':2,'cases':cases}
    (ROOT/'development/inspection-demo-seed-20261030.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
