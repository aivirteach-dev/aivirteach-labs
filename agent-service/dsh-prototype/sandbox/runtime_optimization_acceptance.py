"""阶段4有限组合：实际DSH＋本机固定假服务，不请求Mac或真实模型。"""
from pathlib import Path
from threading import Event,Thread
from tempfile import TemporaryDirectory
import json,os,pwd,time,sys,re
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dsh_tutor.runtime import ROOT,load_config,run_prompt,render_patch,build_budget_plugin
from dsh_tutor.execution_control import execution_control,ModelOwnership
from dsh_tutor.execution_budget import RuntimeFailure
from sandbox.efficiency_fixture import fake_provider

def config_at(path,url,second=False):
    c=load_config(ROOT/'config.json');c.update(ollama_base_url=url,execution_user=pwd.getpwuid(os.geteuid()).pw_name,total_timeout_seconds=15)
    if second:c['provider']={'id':'fake-second','kind':'openai_compatible','base_url':url,'auth_env':'TUTOR_STAGE4_FAKE_KEY'}
    p=path/'config.json';p.write_text(json.dumps(c));return p,c

def warm_probe(root):
    """只复用一个实验运行时两次；结束总是关闭，不保存或启用暖池。"""
    from deepseek_harness import DeepSeekHarness
    from dsh_tutor.providers import provider_for
    with fake_provider() as (url,received):
        _,c=config_at(root,url);p=provider_for(c);patch=root/'warm.patch.yml'
        observer={'id':'warm-probe','name':str(ROOT/'sandbox/warm_probe.mjs'),'config':{'path':str(root/'agents.json')}}
        patch.write_text(render_patch(c,budget_plugin=build_budget_plugin(c,root),control_plugin=observer))
        started=time.monotonic();measure=[]
        with DeepSeekHarness(dsh_home=str(root/'home'),cwd=str(root),profile='sdk-minimal',patches=(str(patch),),provider=p.id,model=c['model'],
            env={'OLLAMA_PLACEHOLDER_KEY':'local-ollama','NO_PROXY':'127.0.0.1'},reasoning_effort=c.get('reasoning_effort'),max_tokens=c['max_output_tokens'],request_timeout_seconds=10) as h:
            startup=time.monotonic()-started
            for i in range(2):
                at=time.monotonic();r=h.run('Only reply probe. Scope marker student-'+str(i),session_id='warm-probe-'+str(i))
                if r.final_response!='probe':raise AssertionError('warm_response')
                rss=0
                children=Path('/proc/self/task/'+str(os.getpid())+'/children').read_text().split()
                for pid in children[:8]:
                    status=Path('/proc')/pid/'status'
                    if status.exists():
                        for line in status.read_text().splitlines():
                            if line.startswith('VmRSS:'):rss+=int(line.split()[1])*1024
                measure.append({'prompt_seconds':round(time.monotonic()-at,6),'live_agents':json.loads((root/'agents.json').read_text())['live_agents'],'direct_child_rss_bytes':rss})
        return {'startup_seconds':round(startup,6),'samples':measure,'simulated_requests':len(received),
            'decision':'retain_per_request_runtime','reason':'两次会话注册表未回收；SDK无公开逐会话关闭。请求绑定的MCP权限和预算不能直接跨学生复用。未完成安全回收前不启用暖池。',
            'scope':'固定假HTTP，RSS只含直接子进程；不是生产内存或真实冷暖模型性能。'}

def comparable(row):
    row=json.loads(json.dumps(row))
    for m in row['messages']:
        text=m.get('content')
        if isinstance(text,str) and text.startswith('Current runtime context. This snapshot supersedes earlier runtime-context snapshots.'):
            m['content']=re.sub(r'Current working directory: "[^"\n]+"\.', 'Current working directory: "<owned-run-directory>".',text)
    return {'messages':row['messages'],'tools':row.get('tools')}

def main():
    checks=[];calls=0
    def check(name,value):
        if not value:raise AssertionError(name)
        checks.append(name)
    old=os.environ.get('TUTOR_STAGE4_FAKE_KEY');os.environ['TUTOR_STAGE4_FAKE_KEY']='fixed-fake-no-secret'
    try:
        with TemporaryDirectory(prefix='stage4-',dir=ROOT/'sandbox/results') as d:
            root=Path(d).resolve();payloads=[];cold=[]
            for second in (False,True):
                folder=root/('second' if second else 'ollama');folder.mkdir()
                with fake_provider() as (url,received):
                    cfg,_=config_at(folder,url,second)
                    r=run_prompt('教材：固定原文。学习目标：目标A。问题：只回复probe。',config_path=cfg,state_dir=folder/'runs')
                    check('第二适配器协议通过' if second else '原Ollama协议保持',r['answer']=='probe' and len(received)==1)
                    payloads.append(received[0]);cold.append(r['runtime_metrics']['timings']);calls+=len(received)
            # 比较完整模型语义输入，允许提供方路由/授权不同；不使用删掉记忆的假比较。
            check('两个提供方完整消息和工具输入一致',comparable(payloads[0])==comparable(payloads[1]))
            from sandbox.mcp_acceptance import bridge_for
            tool_payloads=[]
            for second in (False,True):
                folder=root/('tool-second' if second else 'tool-ollama');folder.mkdir()
                with fake_provider('tool') as (url,received):
                    cfg,c=config_at(folder,url,second);c['execution_limits']['max_model_calls']=1;cfg.write_text(json.dumps(c))
                    bridge=bridge_for('course','n8n','stopped',request_id='probe-tools',instance_id='fake-vm')
                    try:run_prompt('教材：固定原文。学习目标：目标A。请检查服务。',config_path=cfg,state_dir=folder/'runs',tool_bridge=bridge)
                    except RuntimeFailure as e:check('工具路径预算独立：'+str(second),e.code=='model_budget_exhausted')
                    check('工具路径只发送一次：'+str(second),len(received)==1 and bool(received[0]['tools']))
                    tool_payloads.append(received[0]);calls+=len(received)
            check('两条适配路径课程记忆及完整工具定义一致',comparable(tool_payloads[0])==comparable(tool_payloads[1]))
            folder=root/'before';folder.mkdir();cancel=Event();cancel.set()
            with fake_provider() as (url,received):
                cfg,_=config_at(folder,url)
                try:
                    with execution_control(cancel):run_prompt('probe',config_path=cfg,state_dir=folder/'runs')
                    raise AssertionError('cancel_not_seen')
                except RuntimeFailure as e:check('发送前取消不调用提供方',e.code=='cancelled_before_dispatch' and e.known_end and len(received)==0)
            folder=root/'during';folder.mkdir();cancel=Event();hold=Event();entered=Event();mode={'hold':hold,'entered':entered};out={}
            with fake_provider(mode) as (url,received):
                cfg,_=config_at(folder,url)
                def job():
                    try:
                        with execution_control(cancel):out['result']=run_prompt('probe',config_path=cfg,state_dir=folder/'runs')
                    except RuntimeFailure as e:out.update(code=e.code,known_end=e.known_end)
                t=Thread(target=job);t.start();check('取消前已实际发出请求',entered.wait(10));cancel.set();t.join(6)
                try:
                    check('DSH公开取消结束本地等待',not t.is_alive() and out.get('code')=='cancel_unconfirmed' and out.get('known_end') is False)
                    status=json.loads(next((folder/'runs').glob('*/control-status.json')).read_text())
                    check('本地取消回执不伪造远端确认',status['requested'] and status['agent_cancelled'] and status['remote_confirmed'] is False)
                    check('取消不重发模型请求',len(received)==1);calls+=len(received)
                finally:hold.set();t.join(10)
            warmdir=root/'warm';warmdir.mkdir();warm=warm_probe(warmdir);calls+=warm['simulated_requests']
            check('暖运行时实验严格两次',warm['simulated_requests']==2)
            check('发现会话未单独回收并保留独立运行策略',warm['samples'][-1]['live_agents']==2 and warm['decision']=='retain_per_request_runtime')
            # 不把实验中的固定响应称为任意模型质量或真实远端停止验证。
            report={'status':'passed','checks':checks,'actual_model_calls':0,'simulated_provider_http_requests':calls,'warm_experiment':warm,'cold_samples':cold,
              'boundary':'本机固定假提供方协议、取消及资源规则；无Mac调用、无服务器容量/真实模型质量/远端停止保证。'}
            target=ROOT/'sandbox/results/runtime-optimization-report.json';target.write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps({'status':'passed','checks':len(checks),'report':str(target)},ensure_ascii=False))
    finally:
        if old is None:os.environ.pop('TUTOR_STAGE4_FAKE_KEY',None)
        else:os.environ['TUTOR_STAGE4_FAKE_KEY']=old
if __name__=='__main__':main()
