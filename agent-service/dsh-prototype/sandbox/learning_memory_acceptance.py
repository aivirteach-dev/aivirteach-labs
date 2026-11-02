"""第三步受限模型验收：超出近期窗口的学生目标、更正、删除；不额外调用摘要模型。"""
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

ROOT=Path(__file__).resolve().parent.parent;sys.path.insert(0,str(ROOT))
from dsh_tutor.storage import SQLiteChatStore
from dsh_tutor.learning_memory import MemoryLimits
from dsh_tutor.chat_session import answer_chat
from dsh_tutor.contracts import CourseQuestion
from dsh_tutor.runtime import load_config,require_execution_identity


def source_fingerprint():
 paths=list((ROOT/'dsh_tutor').glob('*.py'))+list((ROOT/'dsh_tutor/plugins').glob('*.mjs'))+[ROOT/x for x in ['config.json','ollama.patch.yml','config/storage.example.json','sandbox/learning_memory_acceptance.py']]
 digest=hashlib.sha256()
 for p in sorted(paths):digest.update(p.relative_to(ROOT).as_posix().encode());digest.update(p.read_bytes())
 return digest.hexdigest()


def declare(store,h,rid,text):
 store.begin(h,rid,text)
 store.remember_question(h,rid,text,limits=MemoryLimits())
 store.finish(h,rid,answer='虚构数据准备回执，非模型回答。')


def child(case,path):
 assert path.parent.parent==ROOT/'sandbox/results/runner-check/learning-memory-fixtures'
 if case=='remember':
  store=SQLiteChatStore.create(path);store.migrate_learning_memory()
  h=store.activate(dict(student_id='fictional-student',learning_context_id='learning-demo',course_id='learning-demo',
    course_version='v1',lesson_id='l1',instance_id='vm-demo',instance_generation='g1',environment_os='linux'))
  declare(store,h,'source','学习目标：搭建课程演示页面\n已尝试：刷新浏览器\n未解决：页面仍然空白\n偏好：请使用简短中文解释')
  for i in range(6):
   store.begin(h,'filler-'+str(i),'这是无关的练习占位问答。');store.finish(h,'filler-'+str(i),answer='无关回执。')
 else:
  store=SQLiteChatStore(path);h=store.current(student_id='fictional-student',learning_context_id='learning-demo')
 if case=='correct':declare(store,h,'correction','学习目标：搭建新闻简报工作流')
 if case=='forget':
  item=next(x for x in store.learning_items(h) if x.kind=='goal')
  store.forget_learning(h,'goal',expected_revision=item.revision)
 question='根据我明确记录的学习目标，我当前准备做什么？请只复述学生目标，不把它说成课程要求。'
 if case=='forget':question='我当前还有明确的学习目标记录吗？没有就说当前没有记录，不要根据其他问题猜测目标。'
 request=CourseQuestion(request_id='learning-'+case,question=question,course=dict(course_id='learning-demo',version='v1',lesson_id='l1',title='虚构学习记忆验收',
   instructions=['本课程没有规定学生的个性化目标；只能引用有效学生声明，没有则明确未提供。']))
 result=answer_chat(request,store=store,handle=h,learning_memory=store,config_path=ROOT/'config.json',state_dir=path.parent/'runtime')
 brief=store.learning_brief(h)
 return {'reply':result.model_dump(),'brief':brief.model_dump(),'pid':os.getpid()}


def main():
 require_execution_identity(load_config(ROOT/'config.json'));os.umask(0o077)
 os.environ['PYTHONDONTWRITEBYTECODE']='1';sys.dont_write_bytecode=True
 parser=argparse.ArgumentParser();parser.add_argument('--case',choices=['remember','correct','forget']);parser.add_argument('--db')
 args=parser.parse_args()
 if args.case:
  print(json.dumps(child(args.case,Path(args.db).resolve()),ensure_ascii=False));return 0
 output=ROOT/'sandbox/results/runner-check';fixture=output/'learning-memory-fixtures'/uuid4().hex
 fixture.mkdir(parents=True,mode=0o700);path=fixture/'fictional-memory.db'
 report={'at':datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds'),'source_fingerprint':source_fingerprint(),
         'scope':'虚构学生；3次串行模型请求，记录准备为确定性夹具，摘录摘要无额外模型调用；无真实平台或VM。','cases':[]}
 for case in ['remember','correct','forget']:
  print('开始学习记忆验收：'+case,flush=True)
  try:
   proc=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--case',case,'--db',str(path)],cwd=ROOT,capture_output=True,text=True,timeout=220)
   if proc.returncode:raise RuntimeError('子进程未成功')
   out=json.loads(proc.stdout);r=out['reply'];result=r['result'];text=r['answer'] or ''
   passed=r['state']=='saved' and result is not None and result['tool_calls']==0
   if case=='remember':passed=passed and '演示页面' in text and r['history']['memory_items']==4
   elif case=='correct':passed=passed and '新闻简报' in text and '演示页面' not in text
   else:passed=passed and bool(re.search('没有|未提供|未记录|未明确|无法确定',text)) and '新闻简报' not in text and '演示页面' not in text and not any(x['kind']=='goal' for x in out['brief']['items'])
   row={'id':case,'mechanical_check_passed':bool(passed),'output':out}
  except (subprocess.TimeoutExpired,ValueError,RuntimeError,KeyError,TypeError):row={'id':case,'mechanical_check_passed':False,'error':'子进程或检查失败；未自动重试，远端取消未确认。'}
  report['cases'].append(row);(output/'learning-memory-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
  print(json.dumps(row,ensure_ascii=False),flush=True)
  if not row['mechanical_check_passed']:report['stopped_early']=True;break
 report['source_unchanged']=report['source_fingerprint']==source_fingerprint()
 report['status']='mechanical_checks_passed' if len(report['cases'])==3 and report['source_unchanged'] and all(x['mechanical_check_passed'] for x in report['cases']) else 'needs_review'
 (output/'learning-memory-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
 return 0 if report['status']=='mechanical_checks_passed' else 1


if __name__=='__main__':raise SystemExit(main())
