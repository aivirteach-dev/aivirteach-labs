/** 有限实验专用：记录公开Agent注册表大小，不读取聊天，不作为生产池。 */
import {writeFileSync} from 'node:fs';
export const name='warm-probe';export const inject=['agents'];
export function apply(ctx,config){
  ctx.on('agent/created',()=>{
    const n=ctx.agents.list().length;
    if(n>2) throw new Error('warm_probe_cap');
    writeFileSync(config.path,JSON.stringify({live_agents:n}),{mode:0o600});
  });
}
