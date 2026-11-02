"""本地第四步组合验收：真实存储/门控＋替身推理，不测Mac容量，不连接平台。"""
import ast
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone,timedelta
import hashlib
import json
import os
from pathlib import Path
from threading import Event,Lock
import time
import sys
from unittest.mock import patch
from uuid import uuid4

ROOT=Path(__file__).resolve().parent.parent;sys.path.insert(0,str(ROOT))
from dsh_tutor.resources import ResourceManagedTutor,ResourceGate,AdmissionError,disk_check,assess_capacity
from dsh_tutor.storage import SQLiteChatStore
from dsh_tutor.contracts import CourseQuestion


def source_fingerprint():
 paths=list((ROOT/'dsh_tutor').glob('*.py'))+list((ROOT/'dsh_tutor/plugins').glob('*.mjs'))+[ROOT/x for x in ['config.json','config/resources.example.json','sandbox/resource_acceptance.py',
 'references/aivirteach/aivirteach-labs/agent-service/aivirteach_agent/models.py',
 'references/aivirteach/aivirteach-server/apps/api/src/chat/agent-client.ts']]
 h=hashlib.sha256()
 for p in sorted(paths):h.update(p.relative_to(ROOT).as_posix().encode());h.update(p.read_bytes())
 return h.hexdigest()


def readiness():
 path=ROOT/'references/aivirteach/aivirteach-labs/agent-service/aivirteach_agent/models.py'
 tree=ast.parse(path.read_text())
 fields={node.name:[x.target.id for x in node.body if isinstance(x,ast.AnnAssign) and isinstance(x.target,ast.Name)]
         for node in tree.body if isinstance(node,ast.ClassDef) and node.name in ('DiagnoseRequest','DiagnoseResponse')}
 assert set(fields)=={'DiagnoseRequest','DiagnoseResponse'}
 client=ROOT/'references/aivirteach/aivirteach-server/apps/api/src/chat/agent-client.ts'
 assert 'const DIAGNOSE_TIMEOUT_MS = 60_000' in client.read_text()
 config=json.loads((ROOT/'config.json').read_text())
 return {'scope':'固定提交源码静态核对，不是当前线上版本或HTTP集成测试。','request_fields':fields['DiagnoseRequest'],'response_fields':fields['DiagnoseResponse'],
  'sources':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [path,client]},
  'server_normal_timeout_seconds':60,'prototype_run_timeout_seconds':config['total_timeout_seconds'],
  'compatible_deadline':False,'real_platform_verified':False,
  'gaps':[['身份/实例','正式Server身份、enrollment/generation与lab_id归属映射未接。'],
          ['输入','course/current_step/history需完整适配，learner_state与diagnostic_scope不能静默丢弃。'],
          ['输出','CourseAnswer不是完整DiagnoseResponse；诊断结构、建议、usage和SSE/complete对账未完成。'],
          ['工具','当前仅模拟get_service_status；真实Gateway及Windows/Linux检查未接。'],
          ['时限','普通平台请求60秒，与本地180秒运行上限及排队预算未对齐；必须部署前处理，不能直接接线上。'],
          ['容量','单进程线程替身验收不证明模型吞吐、服务器资源或多进程全局限流。']]}


def main():
 os.umask(0o077)
 directory=ROOT/'sandbox/results/resources-step4'/uuid4().hex;directory.mkdir(parents=True,mode=0o700)
 report={'at':datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds'),'source_fingerprint':source_fingerprint(),
         'scope':'单进程本地资源门控；存储为虚构SQLite，推理传输替身。真实模型调用0，非模型吞吐压测。','model_calls':0,'cases':[]}
 store=SQLiteChatStore.create(directory/'fictional-chat.db')
 handles=[]
 for i in range(8):handles.append(store.activate(dict(student_id='student-'+str(i),learning_context_id='ctx-'+str(i),course_id='demo',course_version='v1',lesson_id='l1')))
 service=ResourceManagedTutor(policy={'max_waiting':4,'min_free_bytes':0},config_path=ROOT/'config.json',state_dir=directory)
 release=Event();entered=Event();lock=Lock();running=0;peak=0;calls=0
 def transport(prompt,**kwargs):
  nonlocal running,peak,calls
  with lock:running+=1;peak=max(peak,running);calls+=1
  entered.set()
  if not release.wait(5):raise RuntimeError('本地测试协调超时')
  with lock:running-=1
  return dict(answer='替身传输测试回答，非真实模型结果。',model='test-transport',run_id=uuid4().hex,
              run_started_at=datetime.now(timezone.utc).isoformat(),elapsed_seconds=0.01)
 def ask(i):
  req=CourseQuestion(request_id='request-'+str(i),question='虚构问题',course=dict(course_id='demo',version='v1',lesson_id='l1',title='虚构'))
  return service.answer(req,store=store,handle=handles[i])
 with patch('dsh_tutor.runtime.run_prompt',side_effect=transport):
  with ThreadPoolExecutor(max_workers=8) as pool:
   futures=[pool.submit(ask,i) for i in range(8)]
   try:
    assert entered.wait(2)
    deadline=time.monotonic()+3
    while service.gate.snapshot()['counts'].get('queue_full',0)<3:
     if time.monotonic()>deadline:raise AssertionError('队列状态未出现')
     time.sleep(0.005)
    saturated=service.gate.snapshot();assert saturated['active']==1 and saturated['waiting']==4
   finally:release.set()
   results=[f.result(timeout=5) for f in futures]
 assert peak==1 and calls==5
 assert sum(x.admission=='queue_full' for x in results)==3
 assert sum(x.chat is not None and x.chat.state=='saved' for x in results)==5
 report['cases'].append({'id':'bounded_application_burst','passed':True,'requests':8,'saved':5,'queue_full':3,'peak_active':peak,'saturated_snapshot':saturated,'final_snapshot':service.gate.snapshot()})
 # 排队超时只验证等待阶段，未启动任何模型或原型业务请求。
 gate=ResourceGate({'queue_timeout_seconds':0.05})
 with gate.slot('holder'):
  def timeout():
   try:
    with gate.slot('waiter'):return 'ran'
   except AdmissionError as exc:return exc.code
  with ThreadPoolExecutor(max_workers=1) as pool:assert pool.submit(timeout).result(timeout=1)=='queue_timeout'
 report['cases'].append({'id':'queue_timeout_cleanup','passed':True,'final_snapshot':gate.snapshot()})
 free=disk_check(directory,0)
 report['cases'].append({'id':'disk_guard_readonly_probe','passed':True,'free_bytes':free,'deleted_files':0})
 assessment=assess_capacity(expected_online=None)
 assert assessment['load_ratio'] is None and not assessment['automatic_changes']
 report['cases'].append({'id':'unknown_capacity_no_guess','passed':True,'assessment':assessment})
 report['platform_readiness']=readiness()
 report['source_unchanged']=source_fingerprint()==report['source_fingerprint'];assert report['source_unchanged']
 report['status']='local_controls_passed_platform_pending'
 (directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
 print(json.dumps({'report':str((directory/'report.json').relative_to(ROOT)),'cases':len(report['cases']),'status':report['status'],'model_calls':0},ensure_ascii=False))


if __name__=='__main__':main()
