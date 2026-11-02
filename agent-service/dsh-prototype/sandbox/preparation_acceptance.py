"""第二阶段组合验收：受控慢读对比、真实管理HTTP和记忆版本，零真实模型。"""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'tools')]
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from threading import Event,Timer,Thread
from unittest.mock import patch
from urllib.request import Request,urlopen
from uuid import uuid4
import hashlib,json,time,traceback
from dashboard import fingerprint
from dsh_tutor.storage import SQLiteChatStore
from dsh_tutor.contracts import CourseQuestion
from dsh_tutor.chat_session import answer_chat
from dsh_tutor.resources import ResourceGate,ResourceManagedTutor
from dsh_tutor.task_memory import TaskRepository,migrate_task_memory
from dsh_tutor.lifecycle import migrate_lifecycle
from dsh_tutor.diagnostics import DiagnosticStore
from dsh_tutor.management import ManagementView
from dsh_tutor.management_http import create_management_server
from dsh_tutor.scheduler_status import SchedulerStatusPublisher,SchedulerStatusReader


def wait_for(condition):
    deadline=time.monotonic()+3
    while not condition():
        if time.monotonic()>deadline:raise AssertionError('state_timeout')
        time.sleep(.005)


def question(rid,text='继续'):
    return CourseQuestion(request_id=rid,question=text,course=dict(course_id='course-a',version='v1',lesson_id='l1',title='虚构'))


def activate(store,name):
    return store.activate(dict(student_id=name,learning_context_id=name,course_id='course-a',course_version='v1',lesson_id='l1',environment_os='linux'))


def fake_answer(prompt):
    return dict(answer='相同的虚构回答',model='mock',run_id=uuid4().hex,
                run_started_at=datetime.now(timezone.utc).isoformat(),elapsed_seconds=.01)


def compare(folder,pipeline):
    folder.mkdir();store=SQLiteChatStore.create(folder/'fictional.db');a=activate(store,'A');b=activate(store,'B')
    service=ResourceManagedTutor(policy={'min_free_bytes':0,'preparation_timeout_seconds':5.0},config_path='unused',state_dir=folder)
    old_gate=ResourceGate({});entered=Event();release=Event();read=store.read_page;prompts={};order=[]
    def slow(h,**kw):
        if h==a:entered.set();assert release.wait(3)
        return read(h,**kw)
    def model(prompt,**kw):
        data=json.loads(prompt.split('以下 JSON 是本轮课程与学生问题：\n')[1]);name=data['question']
        prompts[name]=hashlib.sha256(prompt.encode()).hexdigest();order.append(name)
        return fake_answer(prompt)
    def ask(h,name):
        start=time.monotonic();q=question(name,name)
        if pipeline:
            result=service.answer(q,store=store,handle=h).chat
        else:
            # 对比基线：原来的整个问答占用一个名额，资料读取时也占着DSH位置。
            with old_gate.slot(h.scope.student_id):
                result=answer_chat(q,store=store,handle=h,config_path='unused',state_dir=folder)
        assert result.state=='saved'
        return {'seconds':round(time.monotonic()-start,4),'answer':result.answer}
    with patch.object(store,'read_page',side_effect=slow),patch('dsh_tutor.runtime.run_prompt',side_effect=model),ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(ask,a,'A');assert entered.wait(2)
        second=pool.submit(ask,b,'B')
        if pipeline:
            try:
                ready=second.result(2);assert not first.done()
                snapshot=service.gate.snapshot()
            finally:release.set()
            slow_result=first.result(2)
        else:
            wait_for(lambda:old_gate.snapshot()['waiting']==1)
            release.set();slow_result=first.result(2);ready=second.result(2);snapshot=old_gate.snapshot()
    return {'mode':'split_pipeline' if pipeline else 'whole_request_slot','ready_student':ready,'slow_student':slow_result,
            'model_order':order,'prompt_sha256':prompts,'snapshot':snapshot,
            'note':'用事件人为暂停A读取；对比运行顺序，数字不是服务器延迟压测。'}


def run():
    folder=ROOT/'sandbox/results/preparation-stage2'/uuid4().hex;folder.mkdir(parents=True)
    before=fingerprint();report={'at':datetime.now(timezone.utc).isoformat(),'source_fingerprint':before,
        'actual_model_calls':0,'status':'running','cases':[]};server=None;publisher=None
    def check(name,condition):
        assert condition,name
        report['cases'].append({'name':name,'passed':True})
    try:
        baseline=compare(folder/'baseline',False);split=compare(folder/'split',True)
        report['controlled_comparison']={'baseline':baseline,'split':split}
        check('整体占槽基线顺序A→B，拆分后B可在A慢读未结束时完成',baseline['model_order']==['A','B'] and split['model_order']==['B','A'])
        check('相同业务快照输入完全一致，未通过改问题制造速度差异',baseline['prompt_sha256']==split['prompt_sha256'])
        check('受控模拟输出一致，未丢失保存语义',baseline['ready_student']['answer']==split['ready_student']['answer'])
        check('慢读期间只有准备占用，没有虚假DSH活跃',split['snapshot']['preparing']==1 and split['snapshot']['active']==0)

        store=SQLiteChatStore.create(folder/'task.db');h=activate(store,'memory-demo')
        store.migrate_learning_memory();migrate_task_memory(store);migrate_lifecycle(store)
        repo=TaskRepository(store);repo.create(h,'A','虚构任务A');a=repo.bind(h,'A')
        a.edit_learning(h,'goal','OLD_GOAL',expected_revision=0,request_id='goal')
        service=ResourceManagedTutor(policy={'min_free_bytes':0},config_path='unused',state_dir=folder,diagnostics=DiagnosticStore(store))
        publisher=SchedulerStatusPublisher(service.gate,folder/'scheduler.json',source='simulation:preparation-acceptance',interval=.05).start()
        view=ManagementView(folder/'task.db',ROOT/'config.json',scheduler=SchedulerStatusReader(folder/'scheduler.json'))
        token=uuid4().hex+uuid4().hex;server=create_management_server(view,token,0)
        Thread(target=server.serve_forever,daemon=True).start()
        def overview():
            with urlopen(Request(f'http://127.0.0.1:{server.server_port}/v1/management/overview',headers={'Authorization':'Bearer '+token}),timeout=2) as r:return json.load(r)
        with patch('dsh_tutor.runtime.run_prompt') as model,ThreadPoolExecutor(max_workers=1) as pool:
            with service.gate.slot('blocker'):
                f=pool.submit(service.answer,question('old'),store=a,handle=h)
                wait_for(lambda:service.gate.snapshot()['waiting']==1)
                wait_for(lambda:overview()['queue'].get('waiting')==1)
                snap=overview();report['queued_http_snapshot']=snap['queue']
                check('管理HTTP能区分总占用、准备与DSH等待',snap['queue']['inflight']==2 and snap['queue']['preparing']==0 and snap['queue']['waiting']==1)
                a.edit_learning(h,'goal','NEW_GOAL',expected_revision=1,request_id='edit')
            rejected=f.result(2)
        check('更正记忆后旧准备结果在调用模型前拒绝',rejected.chat.error_code=='memory_changed' and model.call_count==0)
        captured=[]
        def model(prompt,**kw):captured.append(prompt);return fake_answer(prompt)
        with patch('dsh_tutor.runtime.run_prompt',side_effect=model):
            fresh=service.answer(question('fresh'),store=a,handle=h)
            replay=service.answer(question('fresh'),store=a,handle=h)
        check('新请求采用更正目标，旧目标不复活','NEW_GOAL' in captured[0] and 'OLD_GOAL' not in captured[0])
        check('新回答可靠保存，重放不再次推理',fresh.chat.state=='saved' and replay.chat.state=='replayed' and len(captured)==1)
        events=ManagementView(folder/'task.db',ROOT/'config.json').query({})['items']
        report['diagnostic_stages']=[x['stage'] for x in events[0]['timeline']]
        check('问答诊断可追踪等待、重核与保存',any({'execution_wait','revalidate','save'}<=set(x['stage'] for x in e['timeline']) for e in events))
        wait_for(lambda:overview()['queue'].get('inflight')==0)
        report['final_http_snapshot']=overview()['queue']
        check('结束后所有准入与准备计数清零',service.gate.snapshot()['inflight']==0 and service.gate.snapshot()['preparing']==0)
        check('源码冻结与回归版本一致',before==fingerprint())
        report['status']='passed'
    except Exception:report['status']='failed';report['failure']=traceback.format_exc()
    finally:
        if publisher is not None:publisher.close()
        if server is not None:server.shutdown();server.server_close()
        report['boundaries']=['模拟模型只证明程序和输入一致性，不证明Qwen/DeepSeek语义质量',
            '受控阻塞顺序对比不是实际硬件吞吐或加速百分比','SQLite有查询截止时间；不响应预算的外部适配器仍占名额直到返回',
            '单进程调度，正式平台/数据库/VM/认证/远端取消与容量未验收']
        (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        (ROOT/'development/preparation-stage2-acceptance-20261031.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({'status':report['status'],'passed_cases':len(report['cases']),'report':str(folder/'report.json')},ensure_ascii=False))
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(run())
