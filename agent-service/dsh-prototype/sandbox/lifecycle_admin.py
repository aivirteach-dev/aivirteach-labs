"""管理员虚构演示CLI：固定sandbox库与维护目录，无模型或生产认证。

不接收数据库路径，不给学生HTTP/API增加管理权限。需先显式启用v4演示。
"""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from dsh_tutor.storage import SQLiteChatStore,StorageError
from dsh_tutor.maintenance import LocalMaintenance
from dsh_tutor.diagnostics import DiagnosticStore


def main():
    parser=argparse.ArgumentParser(description='只操作固定虚构演示数据的管理员维护')
    parser.add_argument('action',choices=['status','backup','validate'])
    parser.add_argument('--backup-id');parser.add_argument('--save-snapshot',action='store_true')
    args=parser.parse_args()
    store=SQLiteChatStore(ROOT/'sandbox/results/task-memory-demo/fictional.db')
    handle=store.current(student_id='fictional-student',learning_context_id='fictional-learning')
    engine=LocalMaintenance(store,ROOT/'sandbox/results/lifecycle-maintenance')
    report={'at':datetime.now(timezone.utc).isoformat(),'scope':'固定虚构学生；管理员CLI；无模型调用'}
    if args.action=='backup':report['backup']=engine.create_backup()
    elif args.action=='validate':report['validation']=engine.validate_restore(args.backup_id)
    report['capacity']=engine.capacity();report['diagnostics']=DiagnosticStore(store).read_page(handle)
    if args.save_snapshot:
        path=ROOT/'development/lifecycle-admin-snapshot.json'
        path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    try:main()
    except StorageError as exc:
        print(json.dumps({'status':'failed','error_code':exc.code},ensure_ascii=False));raise SystemExit(1)
