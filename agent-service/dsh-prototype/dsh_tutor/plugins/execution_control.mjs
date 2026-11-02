/** 公开Agent.cancel桥：本地取消确认绝不等于远端GPU已停止。 */
import {existsSync,statSync,writeFileSync,renameSync} from 'node:fs';
export const name='tutor-execution-control';
export const inject=['agents'];
export function apply(ctx,config) {
  if(!/^request-[a-f0-9]{32}$/.test(config.sessionId)) throw new Error('control_session');
  const state={version:1,requested:false,agent_cancelled:false,local_idle:false,remote_confirmed:false};
  let cancelled=false;
  function save(){writeFileSync(config.statusPath+'.tmp',JSON.stringify(state),{mode:0o600});renameSync(config.statusPath+'.tmp',config.statusPath);}
  function poll(){
    if(cancelled || !existsSync(config.cancelPath)) return;
    if(statSync(config.cancelPath).size!==0) throw new Error('control_marker');
    state.requested=true;save();
    const agent=ctx.agents.get(config.sessionId);
    if(!agent) return;
    cancelled=true;agent.cancel({kind:'user'});state.agent_cancelled=true;save();
    // whenIdle只用于诊断本地循环结束；远端状态仍由模型流终态或专用回执决定。
    Promise.resolve(agent.whenIdle()).then(()=>{state.local_idle=true;save();},()=>{});
  }
  save();const timer=setInterval(()=>{try{poll();}catch{clearInterval(timer);}},25);
  ctx.on('dispose',()=>clearInterval(timer));
}
