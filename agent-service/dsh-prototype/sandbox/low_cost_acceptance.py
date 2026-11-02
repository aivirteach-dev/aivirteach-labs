"""阶段2端到端组合验收；本机假提供方，不调用Mac或真实LLM。"""
from pathlib import Path
import sys,json
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sandbox.low_cost_demo import LowCostDemo
from dsh_tutor.management import ManagementView
ROOT=Path(__file__).resolve().parents[1]
def main():
    demo=LowCostDemo(ROOT).start();checks=[]
    def check(name,ok):
        if not ok:raise AssertionError(name)
        checks.append(name)
    try:
        check('有限五操作结束',demo.done.wait(30) and demo.error_code is None)
        r=demo.results;check('全部可靠保存',all(v['state']=='succeeded' for v in r.values()))
        check('四个固定动作不调用模型',all(r[k]['result']['detail']['execution_path']=='direct' for k in ('material_zh','material_en','goal','service')))
        check('真实双语教材带原文出处',all('SHA256:' in r[k]['result']['answer'] for k in ('material_zh','material_en')))
        check('目标来自当前任务', '练习AI课程' in r['goal']['result']['answer'])
        check('模拟服务证据经既有校验',r['service']['result']['detail']['quality']['facts']=='verified' and r['service']['result']['detail']['evidence'][0]['source']=='simulation')
        check('开放问题仍经过实际DSH',r['open_question']['result']['detail']['execution_path']=='model' and len(demo.received)==1)
        view=ManagementView(demo.database,ROOT/'config.json',gate=demo.fixture.tutor.gate.snapshot,operations=demo.service.metrics,source='simulation:low-cost-demo')
        state=view.overview();check('管理面板计入四个固定动作',state['operations']['direct_saved']==4 and state['operations']['direct_tool_calls']==1)
        check('缓存已复用且有界',demo.repository.cache.snapshot()['hits']>=2 and demo.repository.cache.snapshot()['bytes']<=4*1024*1024)
        check('日志能定位固定动作',any(t['stage']=='direct_action' for e in view.query({'limit':'20'})['items'] for t in e['timeline']))
        check('新能力显式声明', 'course_actions_v1' in demo.service.capabilities()['profiles'] and 'none' in demo.service.capabilities()['context_modes'])
        report={'status':'passed','source':'simulation:low-cost-demo','checks':checks,'actual_model_calls':0,'simulated_provider_http_requests':len(demo.received),
          'material_version':demo.material_version,'cache':demo.repository.cache.snapshot(),'operations':{k:{'state':v['state'],'execution_path':v['result']['detail']['execution_path'],'answer':v['result']['answer']} for k,v in r.items()},
          'boundary':'固定动作/协议/持久化验证；不证明真实模型解释质量、真实VM或人数容量。'}
        path=demo.root/'low-cost-report.json';path.write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps({'status':'passed','checks':len(checks),'report':str(path)},ensure_ascii=False))
    finally:demo.close()
if __name__=='__main__':main()
