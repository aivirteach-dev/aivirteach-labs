"""本地只读管理入口；只看已存在的固定虚构库，不创建/迁移学生库。"""
from pathlib import Path
import os,sys,argparse
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dsh_tutor.management import ManagementView
from dsh_tutor.management_http import create_management_server
from dsh_tutor.scheduler_status import SchedulerStatusReader
root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description='本机只读Agent管理；可显式启动有限虚构调度演示')
parser.add_argument('--scheduler-demo',action='store_true')
parser.add_argument('--integration-demo',action='store_true')
parser.add_argument('--efficiency-demo',action='store_true')
parser.add_argument('--low-cost-demo',action='store_true')
parser.add_argument('--completion-demo',action='store_true')
args=parser.parse_args();demo=None
if sum([args.scheduler_demo,args.integration_demo,args.efficiency_demo,args.low_cost_demo,args.completion_demo])>1:parser.error('一次只选择一种虚构演示')
if args.completion_demo:
    from sandbox.completion_demo import CompletionDemo
    demo=CompletionDemo(root).start()
    view=ManagementView(demo.database,root/'config.json',gate=demo.fixture.tutor.gate.snapshot,
        operations=demo.service.metrics,maintenance=demo.maintenance.status,source='simulation:completion-demo')
elif args.low_cost_demo:
    from sandbox.low_cost_demo import LowCostDemo
    demo=LowCostDemo(root).start()
    view=ManagementView(demo.database,root/'config.json',gate=demo.fixture.tutor.gate.snapshot,
        operations=demo.service.metrics,material_cache=demo.repository.cache.snapshot,source='simulation:low-cost-demo')
elif args.efficiency_demo:
    from sandbox.efficiency_demo import EfficiencyDemo
    demo=EfficiencyDemo(root).start()
    view=ManagementView(demo.database,root/'config.json',gate=demo.fixture.tutor.gate.snapshot,
        operations=demo.service.metrics,source='simulation:efficiency-demo')
elif args.integration_demo:
    from sandbox.integration_demo import IntegrationDemo
    demo=IntegrationDemo(root).start()
    view=ManagementView(demo.database,root/'config.json',gate=demo.service.tutor.gate.snapshot,
        operations=demo.service.metrics,source='simulation:integration-demo')
elif args.scheduler_demo:
    from sandbox.scheduling_demo import SchedulingDemo
    demo=SchedulingDemo(root).start()
    view=ManagementView(demo.database,root/'config.json',gate=demo.service.gate.snapshot,source='simulation:scheduling-demo')
else:
    # 没有问答发布者时显示未接入；绝不创建空gate伪装真实队列。
    view=ManagementView(root/'sandbox/results/management-demo/fictional.db',root/'config.json',
        scheduler=SchedulerStatusReader(root/'data/management/scheduler.json'),source='simulation:management-demo')
server=create_management_server(view,os.environ['DSH_MANAGEMENT_TOKEN'])
print('本地管理接口127.0.0.1:8788；虚构数据库；只读；不调用Mac或真实模型',flush=True)
try:server.serve_forever()
finally:
    server.server_close()
    if demo is not None:demo.close()
