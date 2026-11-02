"""证据记忆组合验收：两环境/两学生模拟授权检查、历史隔离、备份恢复；零模型请求。"""
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.dashboard import fingerprint
from dsh_tutor.storage import SQLiteChatStore,StorageError
from dsh_tutor.task_memory import TaskRepository,migrate_task_memory
from dsh_tutor.lifecycle import TaskLifecycle,migrate_lifecycle
from dsh_tutor.inspection_memory import InspectionRepository,migrate_inspections
from dsh_tutor.maintenance import LocalMaintenance
from dsh_tutor.chat_session import answer_chat
from dsh_tutor.contracts import CourseQuestion
from sandbox.mcp_acceptance import bridge_for
from sandbox.inspection_fixtures import simulated_transport


def main():
    initial=fingerprint();root=ROOT/'sandbox/results/inspection-acceptance'/uuid4().hex
    root.mkdir(parents=True,mode=0o700);store=SQLiteChatStore.create(root/'fictional.db')
    store.migrate_learning_memory();migrate_task_memory(store);migrate_lifecycle(store);migrate_inspections(store)
    repo=TaskRepository(store);history=InspectionRepository(store);handles={}
    for os_name in ('linux','windows'):
        h=store.activate(dict(student_id='fictional-'+os_name,learning_context_id='learning-'+os_name,
                              course_id='course-'+os_name,course_version='import-demo',lesson_id='l1',
                              instance_id='simulated-'+os_name,instance_generation='g1',environment_os=os_name))
        repo.create(h,'task','虚构任务');handles[os_name]=h
    calls=0
    def ask(h,rid,*,failure=False,replay=False,plain=False):
        nonlocal calls
        s=h.scope;bound=repo.bind(h,'task')
        q=CourseQuestion(request_id=rid,question='检查当前服务' if not plain else '请继续说明',course=dict(course_id=s.course_id,version=s.course_version,lesson_id=s.lesson_id,title='虚构'))
        bridge=bridge_for(s.course_id,'n8n','running',request_id=rid,instance_id=s.instance_id)
        bridge['context']['policy'].update(course_version=s.course_version,student_id=s.student_id,learning_context_id=s.learning_context_id,lesson_id=s.lesson_id,instance_generation=s.instance_generation)
        captured=[]
        def no_tool(prompt,**kwargs):
            captured.append(prompt)
            return dict(answer='虚构无工具回答',model='transport-substitute',run_id=uuid4().hex,
                        run_started_at=datetime.now(timezone.utc).isoformat(),elapsed_seconds=0.01)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=no_tool if plain else simulated_transport(bridge,root,failure=failure)) as transport:
            reply=answer_chat(q,store=bound,handle=h,config_path='unused',state_dir=root,tool_bridge=None if plain else bridge)
        calls+=transport.call_count
        assert transport.call_count==(0 if replay else 1)
        return reply,captured
    linux=handles['linux'];windows=handles['windows']
    for name in ('linux','windows'):
        result,_=ask(handles[name],name)
        record=history.get(handles[name],'task',name)
        assert result.state=='saved' and record['snapshot']['observations'][0]['source']=='simulation'
    failed,_=ask(linux,'failure',failure=True)
    assert failed.state=='saved' and not history.get(linux,'task','failure')['snapshot']['observations']
    replay,_=ask(linux,'linux',replay=True);assert replay.state=='replayed'
    follow,prompts=ask(linux,'follow',plain=True)
    assert follow.state=='saved' and follow.history.skipped_inspection>0 and '正在运行' not in prompts[0]
    life=TaskLifecycle(store)
    for i,target in enumerate(('archived','active')):
        p=life.preview(linux,'task',target);life.apply(linux,'task',target,preview_token=p['preview_token'],operation_id=str(i))
        assert history.get(linux,'task','linux')['usable_as_current'] is False
    current=store.activate(linux.scope.model_copy(update={'instance_generation':'g2'}),expected_revision=linux.revision)
    assert history.get(current,'task','linux')['current_environment_matches'] is False
    engine=LocalMaintenance(store,root/'maintenance',{'min_free_bytes':0});backup=engine.create_backup()
    store.delete_history(linux);assert history.get(current,'task','linux')['record_status']=='history_deleted'
    assert history.get(windows,'task','windows')['record_status']=='verified'
    restored=engine.validate_restore(backup['backup_id'])
    assert restored['restored_stats']['inspection_records']==4 and not restored['chat_enabled']
    cases=[['两学生/两类系统模拟','Linux/Windows各一条模拟观察，同一协议；未接真实操作系统工具。'],
           ['失败不回退旧成功','当次失败保存无观察，旧成功仅供历史查看。'],
           ['重复请求','原回答重放，模型/工具新增调用0，记录不重复。'],
           ['模型历史隔离','同环境工具旧轮次不进入自动前文；普通无工具问答仍可恢复。'],
           ['归档与环境切换','历史可追溯，始终不作当前证据，代次变化明确不匹配。'],
           ['删除与备份恢复','源历史删除联动清理证据，其他学生保留；备份含4条记录，恢复副本禁止问答。']]
    report={'at':datetime.now(timezone.utc).isoformat(),'status':'local_inspection_passed','source_fingerprint':initial,
            'source_unchanged':initial==fingerprint(),'actual_model_requests':0,'substituted_transport_calls':calls,'cases':cases,
            'backup':backup,'restore_validation':restored,'example':history.get(windows,'task','windows'),
            'report_path':str((root/'report.json').relative_to(ROOT))}
    (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key:report[key] for key in ['status','report_path','source_unchanged','actual_model_requests']},ensure_ascii=False))


if __name__=='__main__':main()
