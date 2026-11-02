"""第一步持久化验收：保留虚构数据库证据，跨进程恢复；不调用DSH或模型。"""
from datetime import datetime,timezone,timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from dsh_tutor.conversation import ConversationScope
from dsh_tutor.storage import SQLiteChatStore,StorageError


def source_fingerprint():
    digest=hashlib.sha256()
    for name in ['dsh_tutor/conversation.py','dsh_tutor/storage.py','sandbox/storage_acceptance.py','config/storage.example.json']:
        digest.update(name.encode());digest.update((ROOT/name).read_bytes())
    return digest.hexdigest()


def main():
    os.umask(0o077)
    fingerprint=source_fingerprint()
    now=datetime.now(timezone(timedelta(hours=8)))
    output=ROOT/'sandbox/results/storage-step1'/('run-'+now.strftime('%Y%m%d-%H%M%S')+'-'+uuid4().hex[:8])
    output.mkdir(parents=True,mode=0o700)
    path=output/'fictional-chat.sqlite3'
    report={'at':now.isoformat(timespec='seconds'),'source_fingerprint':fingerprint,
            'scope':'只读写独立虚构SQLite样本；没有模型调用、真实学生、平台数据库或自动历史问答接入。',
            'model_calls':0,'cases':[],'database':str(path.relative_to(ROOT))}
    # 子进程先提交两条记录，再模拟突然退出；父进程必须从磁盘恢复，不能依赖内存对象。
    seed="""
import os,sys
from dsh_tutor.storage import SQLiteChatStore
scope=dict(student_id='fictional-student',learning_context_id='fictional-enrollment',course_id='demo',course_version='v1',lesson_id='lesson-1',instance_id='vm-demo',instance_generation='g1',environment_os='linux')
s=SQLiteChatStore.create(sys.argv[1]);h=s.activate(scope)
s.begin(h,'finished','虚构问题一');s.finish(h,'finished',answer='虚构最终回答',run_id='fictional-run')
s.begin(h,'interrupted','虚构未完成问题')
os._exit(9)
"""
    child=subprocess.run([sys.executable,'-c',seed,str(path)],cwd=ROOT,capture_output=True,text=True,timeout=10)
    assert child.returncode==9
    store=SQLiteChatStore(path)
    h=store.current(student_id='fictional-student',learning_context_id='fictional-enrollment')
    rows=store.read_page(h).turns
    assert [(x.request_id,x.status) for x in rows]==[('finished','completed'),('interrupted','pending')]
    assert rows[0].answer=='虚构最终回答' and rows[1].answer is None
    report['cases'].append({'id':'process_restart_restore','passed':True,'restored_turns':2,'states':['completed','pending']})
    original=rows[0]
    again=store.begin(h,'finished','虚构问题一')
    assert again==original
    report['cases'].append({'id':'idempotent_retry','passed':True,'same_sequence':True})
    other=h.model_copy(update={'scope':h.scope.model_copy(update={'student_id':'another-student'})})
    try:
        store.read_page(other)
        raise AssertionError('越权读取没有拒绝')
    except StorageError as exc:
        assert exc.code=='scope_denied'
    report['cases'].append({'id':'student_isolation','passed':True})
    store.finish(h,'interrupted',error_code='interrupted')
    assert store.read_page(h).turns[-1].status=='failed'
    report['cases'].append({'id':'explicit_interruption_recovery','passed':True,
        'note':'由可信应用确认中断后显式标记失败；重新打开数据库不会擅自取消仍在运行的任务。'})
    store.begin(h,'late','虚构旧实例请求')
    new=store.activate(h.scope.model_copy(update={'instance_generation':'g2'}),expected_revision=h.revision)
    try:
        store.finish(h,'late',answer='晚到回答')
        raise AssertionError('旧范围写入没有拒绝')
    except StorageError as exc:
        assert exc.code=='stale_scope'
    assert store.read_page(new).turns==[]
    report['cases'].append({'id':'scope_switch_late_reply','passed':True,'new_scope_empty':True,
                            'old_request_state':store.read_page(h).turns[-1].status})
    store.begin(new,'delete-fixture','可删除虚构问题')
    store.finish(new,'delete-fixture',answer='可删除虚构回答')
    assert store.delete_history(new)==1 and store.read_page(new).turns==[]
    report['cases'].append({'id':'scoped_logical_deletion','passed':True,
                            'note':'仅清理本脚本创建的新范围；旧范围记录保留，墓碑防止重试复活，不是物理擦除。'})
    assert len(SQLiteChatStore(path).read_page(h).turns)==3
    report.update(source_unchanged=source_fingerprint()==fingerprint,
                  status='passed',database_bytes=path.stat().st_size,
                  limitations=['存储接口仅接受可信应用身份，不提供登录认证。',
                               '自动加载前文接入问答属于第二步，尚未实现。',
                               '断电/硬盘故障、服务器多人吞吐、备份恢复和平台集成未验收。'])
    assert report['source_unchanged']
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({'report':str((output/'report.json').relative_to(ROOT)),'cases':len(report['cases']),
                      'status':report['status'],'model_calls':0},ensure_ascii=False))


if __name__=='__main__':main()
