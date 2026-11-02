"""启动本机功能面板：同一随机能力令牌仅通过子进程环境传递，不输出令牌。

需要停止时在启动终端Ctrl+C；只清理本次创建的进程组，不影响模型或其他服务。
原型将loopback访问者视为本机管理员，未来必须接入平台Operator认证。
"""
from pathlib import Path
import subprocess,os,secrets,signal,time,argparse
root=Path(__file__).resolve().parents[1]
node=root/'.runtime/node-v22.13.0-linux-x64/bin/node'
front=root/'management_frontend'
env=os.environ|{'DSH_MANAGEMENT_TOKEN':secrets.token_urlsafe(48),'NEXT_TELEMETRY_DISABLED':'1'}
env['PATH']=str(node.parent)+os.pathsep+env.get('PATH','')
children=[]
parser=argparse.ArgumentParser(description='本机只读管理面板')
parser.add_argument('--scheduler-demo',action='store_true',help='一轮有限虚构流量，无实际模型调用')
parser.add_argument('--integration-demo',action='store_true',help='一轮HTTP交付/恢复接线演示，无实际模型调用')
parser.add_argument('--efficiency-demo',action='store_true',help='一轮实际DSH到本机假提供方，最多3个模拟请求，无真实模型')
parser.add_argument('--low-cost-demo',action='store_true')
parser.add_argument('--completion-demo',action='store_true')
args=parser.parse_args()
if sum([args.scheduler_demo,args.integration_demo,args.efficiency_demo,args.low_cost_demo,args.completion_demo])>1:parser.error('一次只选择一种虚构演示')
backend=[str(root/'.venv/bin/python'),str(root/'tools/serve_management.py')]
if args.scheduler_demo:backend.append('--scheduler-demo')
if args.integration_demo:backend.append('--integration-demo')
if args.efficiency_demo:backend.append('--efficiency-demo')
if args.low_cost_demo:backend.append('--low-cost-demo')
if args.completion_demo:backend.append('--completion-demo')
try:
    for command,cwd in [(backend,root),
                        ([str(node),str(front/'node_modules/next/dist/bin/next'),'start','--hostname','127.0.0.1','--port','3002'],front)]:
        children.append(subprocess.Popen(command,cwd=cwd,env=env,start_new_session=True))
    print('只读管理面板 http://127.0.0.1:3002；固定虚构库；不调用Mac或真实模型。',flush=True)
    while all(p.poll() is None for p in children):time.sleep(.5)
    raise SystemExit('管理子服务已退出，请检查端口或构建结果')
except KeyboardInterrupt:pass
finally:
    for p in children:
        if p.poll() is None:
            try:os.killpg(p.pid,signal.SIGTERM)
            except ProcessLookupError:pass
    for p in children:
        try:p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:os.killpg(p.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            p.wait()
