"""DSH运行边界：请求独占子进程、统一模型出口预算、有界IPC与脱敏进度。"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
from .execution_budget import ExecutionLimits,RuntimeMetrics,RuntimeFailure,runtime_stage,RUNTIME_STAGES

ROOT=Path(__file__).resolve().parent.parent


def load_config(path):
    """保留已验证Ollama协议；预算来自宿主配置，学生不能扩大限制。"""
    data=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    from .providers import provider_for
    provider=provider_for(data)
    url=urlsplit(provider.base_url)
    if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or url.path!='/v1' or url.query or url.fragment:
        raise ValueError('Ollama 地址必须是无凭据的 http://地址:端口/v1。')
    if not isinstance(data['model'],str) or not data['model'].strip():raise ValueError('模型名称不能为空。')
    for key in ('context_window','max_output_tokens','total_timeout_seconds','sdk_request_timeout_seconds'):
        if type(data[key]) is not int or data[key]<1:raise ValueError(key+'必须是正整数。')
        if key.endswith('seconds') and data[key]>3600:raise ValueError(key+'不能超过3600秒。')
    if data['context_window']<8192:raise ValueError('当前pi-ai要求至少8192上下文，并须匹配Ollama实际配置。')
    if data.get('reasoning_effort') not in (None,'off'):raise ValueError('其他推理等级需先验证。')
    if data.get('reasoning_effort')=='off' and (not isinstance(data.get('reasoning_off_value'),str) or not 0<len(data['reasoning_off_value'])<=32):
        raise ValueError('显式off必须配置当前提供方已验证的reasoning_off_value。')
    limits=ExecutionLimits.model_validate(data.get('execution_limits',{}))
    if data['max_output_tokens']>limits.max_total_output_tokens:raise ValueError('单次输出超过整个请求的输出预留。')
    data['execution_limits']=limits.model_dump()
    return data


def require_execution_identity(config):
    """日常入口只允许受限运行身份；此检查不替代操作系统隔离。"""
    expected=config.get('execution_user')
    if not isinstance(expected,str) or not expected.strip() or len(expected)>128:raise ValueError('配置必须指定有效的 execution_user。')
    if os.name!='posix':raise RuntimeError('当前受限运行身份检查仅支持 POSIX。')
    import pwd
    actual=pwd.getpwuid(os.geteuid()).pw_name
    if actual!=expected:raise PermissionError(f'DSH 必须以 {expected} 身份运行，当前为 {actual}。')


def render_patch(config,*,bridge_plugin=None,budget_plugin=None,control_plugin=None):
    """DSH公开patch挂载，课程与记忆不依赖具体模型品牌。"""
    from .providers import provider_patch
    text=(ROOT/'ollama.patch.yml').read_text(encoding='utf-8-sig')
    plugins=[provider_patch(config)]+[p for p in (budget_plugin,control_plugin,bridge_plugin) if p is not None]
    return text+'\n- insert: '+json.dumps(plugins,ensure_ascii=False)+'\n'


def terminate_owned(proc,*,owns_group=False):
    """仅清理本次拥有的进程组；本机退出不能证明远端Ollama停止。"""
    if os.name=='nt':
        if proc.poll() is not None:return
        subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],capture_output=True,check=False)
        if proc.poll() is None:proc.kill()
    else:
        try:
            if owns_group:os.killpg(proc.pid,signal.SIGKILL)
            elif proc.poll() is None:proc.kill()
        except ProcessLookupError:pass
    proc.wait()


def _metrics(path):
    if not path.is_file() or path.stat().st_size>16384:raise RuntimeFailure('run_failed')
    return RuntimeMetrics.model_validate_json(path.read_bytes()).safe()


def _progress(path,seen):
    """固定目录的有限元数据；未完成JSONL行留待下次读取，不转发正文。"""
    if not path.exists():return seen
    if path.stat().st_size>65536:raise RuntimeFailure('run_failed')
    raw=path.read_bytes();lines=raw[:raw.rfind(b'\n')+1].splitlines()
    if len(lines)>256:raise RuntimeFailure('run_failed')
    for line in lines[seen:]:
        row=json.loads(line)
        if set(row)!={'stage','at'} or row['stage'] not in RUNTIME_STAGES:raise RuntimeFailure('run_failed')
        runtime_stage(row['stage'])
    return len(lines)


def run_prompt(prompt,*,config_path,state_dir,tool_bridge=None):
    """父进程轮询白名单进度；stdout/stderr写有限临时文件，不无界读入内存。"""
    from .execution_control import current_control
    from .providers import provider_for
    control=current_control()
    if control is not None and control.requested:raise RuntimeFailure('cancelled_before_dispatch',known_end=True)
    try:
        config=load_config(config_path);provider_for(config).environment()
    except Exception:raise RuntimeFailure('provider_config_rejected',known_end=True) from None
    require_execution_identity(config)
    bridge=None
    if tool_bridge is not None:
        from .mcp_bridge import BridgeSpec
        bridge=BridgeSpec.model_validate(tool_bridge).model_dump()
        if len(json.dumps(bridge).encode())>32768:raise ValueError('工具桥接上下文过长。')
    if bridge and provider_for(config).capabilities.tools=='unsupported':raise RuntimeFailure('provider_request_rejected',known_end=True)
    if not isinstance(prompt,str) or not 0<len(prompt)<=10000:raise ValueError('输入须为1到10000字符。')
    run_dir=Path(state_dir).resolve()/uuid4().hex;run_dir.mkdir(parents=True)
    from .run_registry import RunLease
    registration=RunLease(run_dir,'dsh');outcome='unknown'
    request_file=run_dir/'request.json';out_path=run_dir/'stdout.tmp';err_path=run_dir/'stderr.tmp'
    from datetime import datetime,timezone
    started=time.monotonic();run_started_at=datetime.now(timezone.utc).isoformat();seen=0;boot=None;proc=None;cancel_sent=False
    try:
        request_file.write_text(json.dumps({'config':config,'prompt':prompt,'tool_bridge':bridge},ensure_ascii=False),encoding='utf-8')
        with out_path.open('wb') as out,err_path.open('wb') as err:
            proc=subprocess.Popen([sys.executable,'-m','dsh_tutor.runtime',str(request_file)],cwd=ROOT,
                env=dict(os.environ,PYTHONUTF8='1'),stdout=out,stderr=err,start_new_session=os.name!='nt')
            while True:
                if control is not None and control.requested and not cancel_sent:
                    (run_dir/'cancel.request').touch(exist_ok=True);cancel_sent=True
                new_seen=_progress(run_dir/'progress.jsonl',seen)
                if new_seen and boot is None:boot=time.monotonic()-started
                seen=new_seen
                if out_path.stat().st_size>2097152 or err_path.stat().st_size>131072:raise RuntimeFailure('run_failed')
                if time.monotonic()-started>=config['total_timeout_seconds']:raise RuntimeFailure('run_failed')
                try:proc.wait(timeout=.05);break
                except subprocess.TimeoutExpired:pass
        _progress(run_dir/'progress.jsonl',seen)
        metrics=_metrics(run_dir/'model-budget.json')
        metrics['timings']['runtime_total']=round(time.monotonic()-started,6)
        if boot is not None:metrics['timings']['worker_boot']=round(boot,6)
        if proc.returncode:
            safe=metrics['budget_stop'] is not None and metrics['unconfirmed_calls']==0
            not_sent=metrics['model_calls']==0
            cancelled=(run_dir/'cancel.request').exists()
            code='model_budget_exhausted' if safe else 'cancelled_before_dispatch' if not_sent and cancelled else 'provider_request_rejected' if not_sent else 'cancel_unconfirmed' if cancelled else 'provider_outcome_unknown'
            raise RuntimeFailure(code,metrics,known_end=safe or not_sent)
        if metrics['unconfirmed_calls']:
            # 子进程退出不等于提供方结束；缺流终态仍按未知处理，不能放行下一问。
            raise RuntimeFailure('run_failed',metrics)
        result=json.loads(out_path.read_text(encoding='utf-8'))
        metrics['timings'].update(result.pop('_startup_timings',{}))
        result.update(runtime_metrics=RuntimeMetrics.model_validate(metrics).safe(),elapsed_seconds=round(time.monotonic()-started,2),
                      run_id=run_dir.name,run_started_at=run_started_at)
        outcome='completed'
        return result
    except BaseException as exc:
        if isinstance(exc,RuntimeFailure) and exc.known_end:outcome='failed_known'
        if proc is not None:terminate_owned(proc,owns_group=os.name!='nt')
        raise
    finally:
        # 诊断元数据留作追踪；原始输入与进程输出无论成功失败都清理。
        for p in (request_file,out_path,err_path):p.unlink(missing_ok=True)
        registration.close(outcome)


def build_budget_plugin(config,run_dir,*,tool_bridge=None,stream_path=None):
    """由可信运行目录和工具桥构建唯一预算插件配置；模型不能指定路径或工具集。"""
    from .providers import provider_for
    return {'id':'tutor-model-budget','name':str(ROOT/'dsh_tutor/plugins/model_budget.mjs'),'config':{
        'limits':config['execution_limits'],'provider':provider_for(config).id,'model':config['model'],
        'maxTokens':config['max_output_tokens'],'contextWindow':config['context_window'],
        'deadlineUnix':time.time()+config['total_timeout_seconds'],
        'allowedTools':['mcp__'+tool_bridge['server_name']+'__get_service_status'] if tool_bridge else [],'cancelPath':str(run_dir/'cancel.request'),'metricsPath':str(run_dir/'model-budget.json'),'progressPath':str(run_dir/'progress.jsonl'),**({'streamPath':str(stream_path)} if stream_path else {})}}


def build_control_plugin(run_dir,session_id):
    """单请求固定session和本地信号路径；模型不能选择取消其他学生。"""
    return {'id':'tutor-execution-control','name':str(ROOT/'dsh_tutor/plugins/execution_control.mjs'),'config':{
        'sessionId':session_id,'cancelPath':str(run_dir/'cancel.request'),'statusPath':str(run_dir/'control-status.json')}}


def worker(path):
    """每请求独占DSH实例及预算账本；正式模型始终核对受限账号。"""
    payload=json.loads(path.read_text(encoding='utf-8'));config=payload['config'];require_execution_identity(config)
    run_dir=path.parent;progress_path=run_dir/'progress.jsonl';metrics_path=run_dir/'model-budget.json'
    def progress(stage):
        with progress_path.open('a',encoding='utf-8') as stream:stream.write(json.dumps({'stage':stage,'at':time.monotonic()})+'\n')
    progress('worker_start')
    from deepseek_harness import DeepSeekHarness
    bridge=payload.get('tool_bridge');audit_path=run_dir/'tool-audit.jsonl';context_path=run_dir/'tool-context.json';plugin=None
    if bridge is not None:
        from .mcp_bridge import BridgeSpec
        bridge=BridgeSpec.model_validate(bridge).model_dump();context=dict(bridge['context'])
        context.update(execution_user=config['execution_user'],audit_path=str(audit_path))
        context['policy']={**context['policy'],'run_id':run_dir.name,'deadline_unix':time.time()+config['total_timeout_seconds']}
        context_path.write_text(json.dumps(context,ensure_ascii=False),encoding='utf-8')
        plugin={'id':'mcp-course-tools','name':'@deepseek-ai/dsh-mcp-client','config':{'serverName':bridge['server_name'],
            'transport':'stdio','command':sys.executable,'args':['-m',bridge['entry_module'],str(context_path)],'cwd':str(ROOT),
            'env':{'PYTHONPATH':str(ROOT),'PYTHONDONTWRITEBYTECODE':'1'},'toolCallTimeoutMs':5000,
            'failOnStartupError':True,'reconnect':{'enabled':False}}}
    from .providers import provider_for
    provider=provider_for(config);session_id='request-'+uuid4().hex
    control_plugin=build_control_plugin(run_dir,session_id)
    guard=build_budget_plugin(config,run_dir,tool_bridge=bridge)
    patch=run_dir/'runtime.patch.yml';patch.write_text(render_patch(config,bridge_plugin=plugin,budget_plugin=guard,control_plugin=control_plugin),encoding='utf-8')
    host=urlsplit(provider.base_url).hostname;timings={}
    env={**provider.environment(),'NO_PROXY':f'{host},localhost,127.0.0.1','no_proxy':f'{host},localhost,127.0.0.1'}
    try:
        # __enter__已启动运行时，计时必须在进入with之前开始。
        progress('dsh_startup');at=time.monotonic()
        with DeepSeekHarness(dsh_home=str(run_dir/'home'),cwd=str(run_dir),profile='sdk-minimal',patches=(str(patch),),
             provider=provider.id,model=config['model'],env=env,reasoning_effort=config.get('reasoning_effort'),
             max_tokens=config['max_output_tokens'],request_timeout_seconds=config['sdk_request_timeout_seconds']) as harness:
            harness.start();timings['dsh_startup']=time.monotonic()-at
            control_file=run_dir/'control-status.json'
            if not control_file.is_file() or control_file.stat().st_size>4096:raise RuntimeFailure('run_failed')
            control_state=json.loads(control_file.read_text())
            if control_state!={'version':1,'requested':False,'agent_cancelled':False,'local_idle':False,'remote_confirmed':False}:raise RuntimeFailure('run_failed')
            _metrics(metrics_path) # 没有真正挂载预算就不能向模型发送第一条消息。
            if bridge is not None:
                progress('mcp_discovery');check=time.monotonic()
                from .tools import AuditLog
                if not any(x['type']=='discovery' for x in AuditLog(audit_path).read()):raise RuntimeFailure('run_failed')
                # 仅“确认已发现”的核对时间；实际发现发生在初始化中，计入dsh_startup。
                timings['mcp_discovery']=time.monotonic()-check
            result=harness.run(payload['prompt'],session_id=session_id)
        progress('runtime_finish');metrics=_metrics(metrics_path)
        if metrics['budget_stop'] is not None:raise RuntimeFailure('model_budget_exhausted')
        calls=[e for e in result.events if e.get('type')=='tool/call']
        if bridge is None and calls:raise RuntimeFailure('run_failed')
        if bridge is None and (result.finish_reason!='completed' or not result.final_response.strip()):raise RuntimeFailure('run_failed')
        output={'answer':result.final_response if result.finish_reason=='completed' else '',
                'model':config['model'],'tool_calls':0,'_startup_timings':timings}
        if bridge is not None:
            from .tools import AuditLog
            rows=AuditLog(audit_path).read();results=[x for x in rows if x['type']=='result']
            output.update(tool_calls=sum(x['type']=='execution' for x in rows),tool_attempts=sum(x['type']=='attempt' for x in rows),
                tool_trace=results[-32:],evidence=[x['evidence'] for x in results if x.get('evidence')][-16:],
                status='partial' if any(x['status']!='ok' for x in results) else 'completed',
                limitations=['工具结果仅限本轮授权范围；模拟来源不代表访问了真实实验环境。'])
            (run_dir/'tool-events.json').write_text(json.dumps(calls,ensure_ascii=False),encoding='utf-8')
        print(json.dumps(output,ensure_ascii=False))
    finally:
        patch.unlink(missing_ok=True);context_path.unlink(missing_ok=True)


if __name__=='__main__':
    try:worker(Path(sys.argv[1]))
    except Exception:
        # 不把SDK错误、模型输入或文件路径跨进程打印到日志。
        print('tutor_runtime_failed',file=sys.stderr);sys.exit(1)
