"""本地组合验收：归档/恢复→问答→诊断→备份→隔离恢复；模型仅传输替身。"""
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
from dsh_tutor.diagnostics import DiagnosticStore
from dsh_tutor.maintenance import LocalMaintenance
from dsh_tutor.chat_session import answer_chat
from dsh_tutor.contracts import CourseQuestion


def main():
    run=ROOT/'sandbox/results/lifecycle-acceptance'/uuid4().hex;run.mkdir(parents=True,mode=0o700)
    initial=fingerprint();store=SQLiteChatStore.create(run/'fictional.db')
    store.migrate_learning_memory();migrate_task_memory(store);migrate_lifecycle(store)
    h=store.activate(dict(student_id='fictional-lifecycle',learning_context_id='fictional-lifecycle',course_id='fictional-course',course_version='v1',lesson_id='l1'))
    repo=TaskRepository(store);repo.create(h,'A','虚构任务A');bound=repo.bind(h,'A');life=TaskLifecycle(store);diag=DiagnosticStore(store)
    for kind,text in [('goal','旧目标'),('attempt','OLD_ATTEMPT_CANARY'),('open_issue','REVOKED_ISSUE_CANARY'),('preference','简短中文')]:
        bound.edit_learning(h,kind,text,expected_revision=0,request_id='prepare-'+kind)
    bound.edit_learning(h,'goal','当前新目标',expected_revision=1,request_id='correct')
    issue_revision=next(x['revision'] for x in bound.inspect(h) if x['kind']=='open_issue')
    bound.forget_learning(h,'open_issue',expected_revision=issue_revision)
    def toggle(target,operation):
        preview=life.preview(h,'A',target)
        return life.apply(h,'A',target,preview_token=preview['preview_token'],operation_id=operation)
    def ask(rid):
        q=CourseQuestion(request_id=rid,question='继续当前任务',course=dict(course_id='fictional-course',version='v1',lesson_id='l1',title='虚构'))
        return answer_chat(q,store=bound,handle=h,config_path='unused',state_dir=run,diagnostics=diag)
    toggle('archived','archive')
    with patch('dsh_tutor.runtime.run_prompt') as transport:blocked=ask('blocked')
    assert blocked.error_code=='task_archived' and transport.call_count==0
    toggle('active','unarchive')
    captured=[]
    def fake(prompt,**kwargs):
        captured.append(prompt)
        return {'answer':'虚构替身回答','model':'transport-substitute','run_id':uuid4().hex,
                'run_started_at':datetime.now(timezone.utc).isoformat(),'elapsed_seconds':0.001}
    with patch('dsh_tutor.runtime.run_prompt',side_effect=fake):saved=ask('saved')
    assert saved.state=='saved' and '当前新目标' in captured[0] and 'OLD_ATTEMPT_CANARY' not in captured[0] and 'REVOKED_ISSUE_CANARY' not in captured[0]
    with patch('dsh_tutor.runtime.run_prompt',side_effect=RuntimeError('PRIVATE_FAILURE_CANARY')):failed=ask('failure')
    events=diag.read_page(h)['events']
    assert failed.error_code=='run_failed' and events[0]['stage']=='model' and 'PRIVATE_FAILURE_CANARY' not in json.dumps(events)
    engine=LocalMaintenance(store,run/'maintenance',{'min_free_bytes':0})
    backup=engine.create_backup();bound.forget_learning(h,'goal',expected_revision=2)
    restored=engine.validate_restore(backup['backup_id'])
    path=engine.root/'validations'/restored['validation_id']/'snapshot.sqlite'
    try:SQLiteChatStore(path)
    except StorageError:rejected=True
    else:rejected=False
    assert rejected and not restored['chat_enabled'] and not restored['live_database_replaced']
    assert not any(x.kind=='goal' for x in bound.learning_items(h))
    report={'at':datetime.now(timezone.utc).isoformat(),'source_fingerprint':initial,'source_unchanged':initial==fingerprint(),
            'status':'local_controls_passed','actual_model_requests':0,'substituted_transport_calls':2,
            'cases':[['归档/恢复与记忆','归档拒绝问答；恢复后旧尝试仍待确认，已撤销问题不进入提示'],
                     ['故障诊断','故障定位到model阶段；正文/异常原文未入诊断'],
                     ['一致性备份','真实SQLite备份完成并校验，源库继续保留撤销状态'],
                     ['独立恢复','副本结构/数量验证通过，普通问答拒绝打开，未覆盖原库']],
            'diagnostic_events':events,'capacity':engine.capacity(),'backup':backup,'restore_validation':restored,
            'report_path':str((run/'report.json').relative_to(ROOT))}
    (run/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':report['status'],'report_path':report['report_path'],'source_unchanged':report['source_unchanged'],'actual_model_requests':0},ensure_ascii=False))


if __name__=='__main__':main()
