"""阶段3组合验收；不连接Mac、不执行真实环境操作。"""
from pathlib import Path
import sys,json
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sandbox.completion_demo import CompletionDemo
from dsh_tutor.management import ManagementView
ROOT=Path(__file__).resolve().parents[1]
def main():
    demo=CompletionDemo(ROOT).start();checks=[]
    def check(name,ok):
        if not ok:raise AssertionError(name)
        checks.append(name)
    try:
        check('有限演示结束',demo.done.wait(35) and demo.error_code is None);o=demo.observed
        check('慢保存没有提前交付',o['first_pending'] and o['first_stage']=='delivery_save')
        check('DSH释放而收尾和总名额仍占用',o['active_during_first_save']==0 and o['finishing_during_first_save']==1)
        check('下一位在前一位保存期间完成',o['second_saved_before_first'])
        check('两个回答最终均双保存',all(v['state']=='succeeded' for v in demo.results.values()))
        check('仅两个假提供方请求',len(demo.received)==2)
        check('真实已用时不冒充倒计时',o['first_elapsed'] is not None and o['eta'] is None)
        check('在线工作期间维护立即延后',o['busy_maintenance']=='deferred')
        check('可信有效期保留规则清除一条样例',o['retention']['rows']==1)
        check('旧请求回放410且不再次推理',o['expired_http_status']==410 and len(demo.received)==2)
        check('精确预览只清理一个一次性目录',len(o['cleanup']['removed'])==1 and o['unknown_preserved'])
        check('容量未知不当成零',o['capacity']['components']['runtime_total'] is None and not o['capacity']['coverage_complete'])
        view=ManagementView(demo.database,ROOT/'config.json',gate=demo.fixture.tutor.gate.snapshot,operations=demo.service.metrics,maintenance=demo.maintenance.status,source='simulation:completion-demo')
        overview=view.overview();check('管理投影可读取收尾和缓存容量',overview['queue']['finishing_reserved']==0 and overview['maintenance']['state']=='available')
        report={'status':'passed','checks':checks,'actual_model_calls':0,'simulated_provider_http_requests':len(demo.received),'observed':o,
          'boundary':'慢交付/程序规则/协议和虚构清理验证；非硬件吞吐或真实模型质量。保留DSH元数据，仅删除一次性清理样例。'}
        path=demo.root/'completion-report.json';path.write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps({'status':'passed','checks':len(checks),'report':str(path)},ensure_ascii=False))
    finally:demo.close()
if __name__=='__main__':main()
