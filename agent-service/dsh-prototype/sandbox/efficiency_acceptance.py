"""阶段1组合验收：固定4个虚构操作，实际DSH仅连接本机假提供方。"""
from pathlib import Path
import sys,json,time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sandbox.efficiency_demo import EfficiencyDemo
from dsh_tutor.management import ManagementView
from tools.check_execution_profile import check_profile
ROOT=Path(__file__).resolve().parents[1]

def main():
    demo=EfficiencyDemo(ROOT).start();checks=[]
    def check(name,condition):
        if not condition:raise AssertionError(name)
        checks.append(name)
    try:
        check('有限虚构流量结束',demo.done.wait(30) and demo.error_code is None)
        r=demo.results
        check('首次保存与重发去重',r['A']['state']=='succeeded')
        check('工具后续被统一模型预算拒绝',r['B']['state']=='failed' and r['B']['error_code']=='model_budget_exhausted')
        check('已知预算停止不暂停后续学生',r['C']['state']=='succeeded' and demo.service.metrics()['ready'])
        check('排队取消不执行模型',r['D']['state']=='failed' and r['D']['cancel_requested'])
        check('提供方实际仅收到三个模拟请求',len(demo.received)==3)
        view=ManagementView(demo.database,ROOT/'config.json',gate=demo.fixture.tutor.gate.snapshot,
            operations=demo.service.metrics,source='simulation:efficiency-demo')
        overview=view.overview();execution=overview['queue']['execution']
        check('有界运行汇总含失败预算',execution['samples']==3 and execution['model_calls']==3 and execution['budget_stops']==1)
        check('启动模型首段工具与保存分开采集',all(k in execution['timings'] for k in ['dsh_startup','model_stream','first_visible','tool_execution','chat_save']))
        check('交付保存与完整操作有独立测量',overview['operations']['timings']['delivery_save']['samples']==2)
        check('估算与未知真实用量分开',execution['actual_usage']=='unknown' and execution['hardware']=='unknown')
        client=demo.client();before=len(demo.received)
        for _ in range(50):
            result=client.view(r['A']['operation_id']);time.sleep(.03)
        check('50次只读查询不重新推理',len(demo.received)==before and result['state']=='succeeded')
        events=view.query({'limit':'20'})['items']
        check('诊断能看出DSH初始化和工具阶段',any(t['stage']=='dsh_startup' for e in events for t in e['timeline']) and any(t['stage']=='tool_execution' for e in events for t in e['timeline']))
        check('实际profile关闭摘要重试和无关工具',check_profile()['status']=='passed')
        report={'status':'passed','source':'simulation:efficiency-demo','checks':checks,'actual_model_calls':0,
          'simulated_provider_http_requests':len(demo.received),'operations':{k:{'state':v['state'],'error':v['error_code']} for k,v in r.items()},
          'execution':execution,'profile':check_profile(),'limits_note':'演示调用上限1；正式默认4。不是30～50人模型容量测试。'}
        path=demo.root/'report.json';path.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({'status':'passed','checks':len(checks),'report':str(path)},ensure_ascii=False))
    finally:demo.close()
if __name__=='__main__':main()
