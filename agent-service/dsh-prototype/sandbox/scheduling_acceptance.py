"""第一阶段端到端验收：8名虚构学生、真实SQLite/HTTP，模型替身，无Mac请求。"""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tools')]
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from threading import Event,Thread,Lock
from unittest.mock import patch
from urllib.request import Request,urlopen
from uuid import uuid4
import json,time,traceback

from dashboard import fingerprint
from dsh_tutor.contracts import CourseQuestion
from dsh_tutor.conversation import ConversationScope
from dsh_tutor.storage import SQLiteChatStore
from dsh_tutor.task_memory import migrate_task_memory,TaskRepository
from dsh_tutor.lifecycle import migrate_lifecycle
from dsh_tutor.diagnostics import DiagnosticStore
from dsh_tutor.resources import ResourceManagedTutor
from dsh_tutor.management import ManagementView
from dsh_tutor.management_http import create_management_server
from dsh_tutor.scheduler_status import SchedulerStatusPublisher,SchedulerStatusReader


def wait_for(condition):
    deadline=time.monotonic()+4
    while not condition():
        if time.monotonic()>deadline:raise AssertionError('expected_state_timeout')
        time.sleep(.005)


def run():
    before=fingerprint();folder=ROOT/'sandbox/results/scheduling-stage1'/uuid4().hex;folder.mkdir(parents=True)
    report={'at':datetime.now(timezone.utc).isoformat(),'source_fingerprint':before,'status':'running',
            'model_source':'simulation','actual_model_calls':0,'cases':[]}
    releases=[Event(),Event()];server=None;publisher=None
    try:
        store=SQLiteChatStore.create(folder/'fictional.db');store.migrate_learning_memory();migrate_task_memory(store);migrate_lifecycle(store)
        repo=TaskRepository(store);inputs=[]
        for i in range(8):
            s=ConversationScope(student_id=f'fictional-{i}',learning_context_id=f'context-{i}',course_id='course-a',
                course_version='v1',lesson_id='l1',instance_id=f'vm-{i}',instance_generation='g1',environment_os='linux' if i%2 else 'windows')
            h=store.activate(s);repo.create(h,'task','虚构任务')
            q=CourseQuestion(request_id=f'request-{i}',question=f'QUESTION-{i}',course=dict(course_id='course-a',version='v1',lesson_id='l1',title='虚构'))
            inputs.append((q,repo.bind(h,'task'),h))
        tutor=ResourceManagedTutor(policy={'max_active':2,'validated_max_active':2,'max_waiting':4,
            'queue_timeout_seconds':10.0,'min_free_bytes':0},config_path='mock-only',state_dir=folder,diagnostics=DiagnosticStore(store))
        calls=[];lock=Lock();cancels=[Event() for _ in inputs]
        def model(prompt,**kw):
            payload=json.loads(prompt.split('以下 JSON 是本轮课程与学生问题：\n')[1]);q=payload['question']
            with lock:calls.append(q)
            if q in ('QUESTION-0','QUESTION-1'):
                if not releases[int(q[-1])].wait(8):raise RuntimeError('fixture_timeout')
            return dict(answer='虚构回答',model='mock',run_id=uuid4().hex,run_started_at=datetime.now(timezone.utc).isoformat(),elapsed_seconds=.01)
        publisher=SchedulerStatusPublisher(tutor.gate,folder/'scheduler.json',source='simulation:scheduling-acceptance',interval=.05).start()
        view=ManagementView(store.path if hasattr(store,'path') else folder/'fictional.db',ROOT/'config.json',scheduler=SchedulerStatusReader(folder/'scheduler.json'))
        token=uuid4().hex+uuid4().hex;server=create_management_server(view,token,0)
        thread=Thread(target=server.serve_forever,daemon=True);thread.start()
        def overview():
            with urlopen(Request(f'http://127.0.0.1:{server.server_port}/v1/management/overview',headers={'Authorization':'Bearer '+token}),timeout=2) as r:return json.load(r)
        def check(name,condition):
            assert condition,name
            report['cases'].append({'name':name,'passed':True})
        with patch('dsh_tutor.runtime.run_prompt',side_effect=model):
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures=[]
                try:
                    for i,(q,s,h) in enumerate(inputs):
                        futures.append(pool.submit(tutor.answer,q,store=s,handle=h,cancel=cancels[i]))
                        if i<2:wait_for(lambda:len(calls)==i+1)
                        elif i<6:wait_for(lambda:tutor.gate.snapshot()['waiting']==i-1)
                    check('8人突发：2运行、4等待、2明确拒绝',all(f.result(2).admission=='queue_full' for f in futures[6:]))
                    q,s,h=inputs[0];check('同学生重复提交不启动第二次模型',tutor.answer(q,store=s,handle=h).admission=='student_limit' and len(calls)==2)
                    wait_for(lambda:overview()['queue'].get('waiting')==4)
                    live=overview();report['live_snapshot']=live['queue']
                    check('实际问答调度→文件心跳→管理HTTP完整链路',live['queue']['active']==2 and len(live['queue']['running'])==2)
                    check('明确模拟来源，模型健康仍未知',live['queue']['source']=='simulation:scheduling-acceptance' and live['model']['state']=='unknown')
                    cancels[2].set();check('排队取消不执行模型',futures[2].result(2).admission=='cancelled')
                    old=inputs[3][2];store.activate(old.scope.model_copy(update={'instance_generation':'g2'}),expected_revision=old.revision)
                    tutor.gate.reconfigure({'max_active':1},expected_revision=1)
                    releases[0].set();check('第一位正常保存',futures[0].result(2).chat.state=='saved')
                    check('降低上限保留另一在途请求且不放行等待者',tutor.gate.snapshot()['active']==1 and tutor.gate.snapshot()['waiting']==3 and len(calls)==2)
                finally:
                    for e in releases:e.set()
                results=[f.result(4) for f in futures]
        check('排队期间换环境，旧请求拒绝且未调用模型',results[3].chat.state=='scope_changed' and 'QUESTION-3' not in calls)
        check('其余学生独立保存',all(results[i].chat.state=='saved' for i in (0,1,4,5)))
        with patch('dsh_tutor.runtime.run_prompt',side_effect=AssertionError('must_not_repeat')):
            q,s,h=inputs[0];check('已保存请求重放，不重复推理',tutor.answer(q,store=s,handle=h).chat.state=='replayed')
        check('只有4次模拟推理，无取消/旧环境/重复调用',len(calls)==4)
        wait_for(lambda:overview()['queue'].get('active')==0 and overview()['queue'].get('waiting')==0)
        report['final_snapshot']=overview()['queue'];report['mock_model_calls']=len(calls)
        check('当前源码与验收前指纹一致',fingerprint()==before)
        report['status']='passed'
    except Exception:
        report['status']='failed';report['failure']=traceback.format_exc()
    finally:
        for e in releases:e.set()
        if publisher is not None:publisher.close()
        if server is not None:server.shutdown();server.server_close()
        report['boundaries']=['单进程调度；不是跨进程全局配额','Windows/Linux仅虚构范围信息，未连接VM',
            '真实DSH/Ollama取消、正式模型容量、平台认证和服务器未验收','仅应用阶段；不提供GPU或工具内部细分计时']
        path=folder/'report.json';path.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        (ROOT/'development/scheduling-stage1-acceptance-20261031.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({'status':report['status'],'passed_cases':len(report['cases']),'report':str(path)},ensure_ascii=False))
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':sys.exit(run())
