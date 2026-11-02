"""第三阶段组合验收：真实HTTP/SQLite，虚构学生和模型；不访问Mac或平台。

结果集中在sandbox，源码指纹绑定本次运行。进程强制中断等边界另由自动回归
覆盖；本脚本主要复核平台客户端提交、进度、取消、重发、交付之间的连接。
"""
from pathlib import Path
from threading import Event,Thread
from unittest.mock import patch
from uuid import uuid4
import json,sys,time
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tools'))
from dashboard import fingerprint
from dsh_tutor.agent_http import create_agent_server
from sandbox.integration_fixture import Fixture,fake_model
from sandbox.integration_client import PlatformClient,ClientError


def wait(predicate):
    until=time.monotonic()+5
    while not predicate():
        if time.monotonic()>until:raise AssertionError('simulation_deadline')
        time.sleep(.01)


def main():
    output=ROOT/'sandbox/results/integration-stage3'/uuid4().hex;output.mkdir(parents=True,mode=0o700)
    fixture=Fixture(output);release=Event();entered=Event();cases=[];server=None;http=None
    report={'at':datetime.now(timezone.utc).isoformat(),'source':'simulation','model_calls':0,'source_fingerprint':fingerprint(),
        'cases':cases,'scope':'HTTP/SQLite/进度/交付组合；不是正式平台、真实模型或硬件压测。'}
    def check(name,condition):
        cases.append({'name':name,'passed':bool(condition)})
        if not condition:raise AssertionError(name)
    def model(*a,**kw):
        report['model_calls']+=1;entered.set()
        if not release.wait(5):raise RuntimeError('simulation_not_released')
        return fake_model(*a,**kw)
    try:
        with patch('dsh_tutor.runtime.run_prompt',side_effect=model):
            fixture.service.verify_ready(upstream_idle=True)
            server=create_agent_server(fixture.service,fixture.resolve,port=0);http=Thread(target=server.serve_forever,daemon=True);http.start()
            base='http://127.0.0.1:'+str(server.server_port)
            a=PlatformClient(base,fixture.tokens['A']);b=PlatformClient(base,fixture.tokens['B'])
            status,first=a.submit(fixture.request('a-question'));op=first['operation_id']
            check('快速接受并返回操作编号',status==202 and len(op)==32);check('进入模型阶段',entered.wait(2))
            _,duplicate=a.submit(fixture.request('a-question'));check('断线重发复用同一操作',duplicate['operation_id']==op and report['model_calls']==1)
            _,second=b.submit(fixture.request('b-question'));bop=second['operation_id']
            wait(lambda:fixture.tutor.gate.snapshot()['waiting']==1)
            live=a.view(op);check('进度来自实际模型阶段',live['stage']=='model' and live['result'] is None)
            try:b.view(op);isolated=False
            except ClientError as exc:isolated=exc.status==404
            check('学生无法读取其他人的操作',isolated)
            b.cancel(bop);wait(lambda:b.view(bop)['state']=='failed')
            check('等待取消未额外推理',report['model_calls']==1)
            release.set();wait(lambda:fixture.service.metrics()['active_operations']==0)
            final=a.view(op);result=final['result']
            check('聊天与交付日志均保存才返回答案',final['state']=='succeeded' and result['persistence']['agent_chat']=='saved' and result['persistence']['operation_record']=='saved')
            check('平台保存未冒充已完成',result['persistence']['platform']=='unconfirmed')
            check('未知Token用量不是0',result['usage']['state']=='unknown' and result['usage']['output_tokens'] is None)
            check('增量进度读取不重复发模型',a.view(op,final['events']['next_after'])['events']['items']==[] and report['model_calls']==1)
            _,replay=a.submit(fixture.request('a-question'));check('完成后重发仍读取原回答',replay['result']['answer']==result['answer'] and report['model_calls']==1)
            report['passed']=True
    except Exception as exc:
        report['passed']=False;report['failure_type']=type(exc).__name__;raise
    finally:
        release.set()
        if server is not None:server.shutdown();server.server_close();http.join(1)
        fixture.close()
        report['source_unchanged']=report['source_fingerprint']==fingerprint()
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'report':str(output/'report.json'),'passed':report.get('passed',False),'checks':len(cases),'model_calls':report['model_calls']},ensure_ascii=False))


if __name__=='__main__':main()
