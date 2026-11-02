"""受限账号实际模型验收：任务A目标、任务B隔离、回到A更正目标。

只用虚构记录，最多3次串行DSH问答；管理操作/摘录不调用模型，无真实平台或工具。
原始报告每次保存在独立夹具目录，不覆盖失败报告；最后指针便于助手读取。
"""
import argparse
from datetime import datetime,timezone,timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from dsh_tutor.storage import SQLiteChatStore
from dsh_tutor.task_memory import TaskRepository,migrate_task_memory
from dsh_tutor.chat_session import answer_chat
from dsh_tutor.contracts import CourseQuestion
from dsh_tutor.runtime import load_config,require_execution_identity


def source_fingerprint():
    digest=hashlib.sha256()
    paths=list((ROOT/'dsh_tutor').glob('*.py'))+list((ROOT/'dsh_tutor/plugins').glob('*.mjs'))+[ROOT/x for x in ('config.json','ollama.patch.yml','sandbox/task_memory_acceptance.py')]
    for path in sorted(paths):
        digest.update(path.relative_to(ROOT).as_posix().encode());digest.update(path.read_bytes())
    return digest.hexdigest()


def child(case,path):
    if path.parent.parent!=ROOT/'sandbox/results/runner-check/task-memory-fixtures':
        raise ValueError('fixture_path_rejected')
    if case=='task_a':
        store=SQLiteChatStore.create(path);store.migrate_learning_memory();migrate_task_memory(store)
        handle=store.activate(dict(student_id='fictional-task-student',learning_context_id='fictional-task-learning',
                                  course_id='task-demo',course_version='v1',lesson_id='l1'))
        repo=TaskRepository(store)
        repo.create(handle,'A','虚构任务A');repo.create(handle,'B','虚构任务B')
        a=repo.bind(handle,'A')
        for kind,text in [('goal','搭建新闻简报工作流'),('attempt','刷新浏览器'),('preference','请用简短中文解释')]:
            a.edit_learning(handle,kind,text,expected_revision=0,request_id='prepare-'+kind)
    else:
        store=SQLiteChatStore(path);handle=store.current(student_id='fictional-task-student',learning_context_id='fictional-task-learning')
        repo=TaskRepository(store)
    bound=repo.bind(handle,'B' if case=='task_b' else 'A')
    if case=='correct_a':
        bound.edit_learning(handle,'goal','测试邮件发送',expected_revision=1,request_id='correct-goal')
    request=CourseQuestion(request_id='ask-'+case,question='我在当前练习里明确记录的学习目标是什么？只复述有效目标；没有就明确说当前没有记录，不从其他内容猜测。',
                           course=dict(course_id='task-demo',version='v1',lesson_id='l1',title='虚构任务隔离验收',
                                       instructions=['教材不规定个人目标；学生目标只来自当前有效记忆，不表示课程要求或已掌握。']))
    reply=answer_chat(request,store=bound,handle=handle,config_path=ROOT/'config.json',state_dir=path.parent/'runtime')
    return {'reply':reply.model_dump(),'memory':bound.inspect(handle),'brief':bound.learning_brief(handle).model_dump(),'pid':os.getpid()}


def main():
    require_execution_identity(load_config(ROOT/'config.json'));os.umask(0o077)
    sys.dont_write_bytecode=True;os.environ['PYTHONDONTWRITEBYTECODE']='1'
    parser=argparse.ArgumentParser();parser.add_argument('--case',choices=['task_a','task_b','correct_a']);parser.add_argument('--db')
    args=parser.parse_args()
    if args.case:
        print(json.dumps(child(args.case,Path(args.db).resolve()),ensure_ascii=False));return 0
    output=ROOT/'sandbox/results/runner-check';fixture=output/'task-memory-fixtures'/uuid4().hex
    fixture.mkdir(parents=True,mode=0o700);path=fixture/'fictional.db'
    report={'at':datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds'),'source_fingerprint':source_fingerprint(),
            'scope':'固定虚构学生，3次串行问答；管理与摘录不额外调用模型；没有真实平台、VM或MCP执行。','cases':[]}
    for case in ('task_a','task_b','correct_a'):
        print('开始任务记忆验收：'+case,flush=True)
        try:
            proc=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--case',case,'--db',str(path)],cwd=ROOT,
                                capture_output=True,text=True,timeout=220)
            if proc.returncode:raise RuntimeError('child_failed')
            result=json.loads(proc.stdout);reply=result['reply'];text=reply['answer'] or ''
            passed=reply['state']=='saved' and reply['result'] is not None and reply['result']['tool_calls']==0
            if case=='task_a':passed=passed and reply['task_id']=='A' and '新闻简报' in text
            elif case=='task_b':
                passed=passed and reply['task_id']=='B' and '新闻简报' not in text and bool(re.search('没有|未提供|未记录|未明确|无法确定',text)) and not any(x['kind']=='goal' for x in result['brief']['items'])
            else:
                passed=passed and reply['task_id']=='A' and '邮件' in text and '新闻简报' not in text and not any(x['kind']=='attempt' for x in result['brief']['items'])
            row={'id':case,'mechanical_check_passed':bool(passed),'output':result}
        except (subprocess.TimeoutExpired,ValueError,RuntimeError,KeyError,TypeError):
            row={'id':case,'mechanical_check_passed':False,'error':'子进程或检查失败；未自动重试，远端模型取消未确认。'}
        report['cases'].append(row)
        (fixture/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps(row,ensure_ascii=False),flush=True)
        if not row['mechanical_check_passed']:break
    report['source_unchanged']=report['source_fingerprint']==source_fingerprint()
    report['status']='mechanical_checks_passed' if report['source_unchanged'] and len(report['cases'])==3 and all(x['mechanical_check_passed'] for x in report['cases']) else 'needs_review'
    report['original_report_path']=str((fixture/'report.json').relative_to(ROOT))
    (fixture/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    (output/'task-memory-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['status']=='mechanical_checks_passed' else 1


if __name__=='__main__':raise SystemExit(main())
