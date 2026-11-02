/** 直接驱动正式公开插件，不经过pre-step，验证辅助调用也不能漏计。 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,readFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {apply} from '../../dsh_tutor/plugins/model_budget.mjs';

function fixture(extra={}) {
 const dir=mkdtempSync(join(tmpdir(),'tutor-guard-'));const handlers={};
 const limits={max_model_calls:2,max_total_output_tokens:1024,max_input_estimated_tokens_per_call:6000,
  max_total_input_estimated_tokens:24000,context_headroom_tokens:512,max_request_bytes:65536,
  max_output_bytes:32768,max_runtime_events:128,...extra};
 apply({on:(name,fn)=>{handlers[name]=fn;}},{limits,provider:'local',model:'model',maxTokens:512,contextWindow:8192,
  deadlineUnix:Date.now()/1000+20,metricsPath:join(dir,'metrics.json'),progressPath:join(dir,'events.jsonl')});
 return {handlers,dir,metrics:()=>JSON.parse(readFileSync(join(dir,'metrics.json'))),close:()=>rmSync(dir,{recursive:true})};
}
const request=()=>Object.freeze({provider:'local',model:'model',maxTokens:512,messages:[{role:'user',content:[{type:'text',text:'hello'}]}]});
async function consume(stream){for await(const ignored of stream){}}
async function* success(){yield {type:'text-delta',index:0,text:'ok'};yield {type:'finish',reason:{kind:'stop'}};}
test('direct auxiliary and retry attempts consume the same request budget',async()=>{
 const f=fixture();let calls=0;
 try{
  for(let i=0;i<2;i++) await consume(f.handlers['llm/stream'](request(),()=>{calls++;return success();}));
  await assert.rejects(consume(f.handlers['llm/stream'](request(),()=>{calls++;return success();})),/model_calls/);
  assert.equal(calls,2);assert.equal(f.metrics().model_calls,2);assert.equal(f.metrics().unconfirmed_calls,0);
 }finally{f.close();}
});
test('failed provider attempt is not refunded',async()=>{
 const f=fixture({max_model_calls:1});
 try{await assert.rejects(consume(f.handlers['llm/stream'](request(),async function*(){throw Error('fake_disconnect');})),/fake_disconnect/);
  await assert.rejects(consume(f.handlers['llm/stream'](request(),success)),/model_calls/);
  assert.equal(f.metrics().unconfirmed_calls,1);
 }finally{f.close();}
});
test('different provider route is rejected without invoking it',async()=>{
 const f=fixture();let called=false;
 try{await assert.rejects(consume(f.handlers['llm/stream']({...request(),provider:'other'},()=>{called=true;return success();})),/route/);assert.equal(called,false);}finally{f.close();}
});
test('output reservations reject a hidden summary requesting a larger cap',async()=>{
 const f=fixture();let called=false;
 try{await assert.rejects(consume(f.handlers['llm/stream']({...request(),maxTokens:65536},()=>{called=true;return success();})),/missing_output_cap/);assert.equal(called,false);}finally{f.close();}
});
test('request and per-request budgets are independent across instances',async()=>{
 const a=fixture({max_model_calls:1}),b=fixture({max_model_calls:1});
 try{await consume(a.handlers['llm/stream'](request(),success));await consume(b.handlers['llm/stream'](request(),success));assert.equal(a.metrics().model_calls,1);assert.equal(b.metrics().model_calls,1);}finally{a.close();b.close();}
});

test('an unexpected tool schema is blocked before provider I/O',async()=>{
 const f=fixture();let called=false;
 try{await assert.rejects(consume(f.handlers['llm/stream']({...request(),tools:[{name:'arbitrary_shell'}]},()=>{called=true;return success();})),/tool_set/);assert.equal(called,false);}finally{f.close();}
});
test('empty streaming chunks cannot bypass finite buffers',async()=>{
 const f=fixture({max_stream_chunks:16});
 try{await assert.rejects(consume(f.handlers['llm/stream'](request(),async function*(){for(let i=0;i<20;i++)yield {type:'text-delta',index:0,text:''};})),/stream_chunks/);assert.equal(f.metrics().unconfirmed_calls,1);}finally{f.close();}
});

test('synthetic error finish must not release unknown remote work',async()=>{
 const f=fixture({max_model_calls:1});
 try{
  await consume(f.handlers['llm/stream'](request(),async function*(){yield {type:'finish',reason:{kind:'error',failure:{code:'TRANSPORT'}}};}));
  assert.equal(f.metrics().unconfirmed_calls,1);
  await assert.rejects(consume(f.handlers['llm/stream'](request(),success)),/model_calls/);
  assert.equal(f.metrics().unconfirmed_calls,1);
 }finally{f.close();}
});
test('unknown finish and missing reason stay unconfirmed',async()=>{
 for(const reason of [undefined,{kind:'aborted'},{kind:'other'}]){
  const f=fixture();try{await consume(f.handlers['llm/stream'](request(),async function*(){yield {type:'finish',reason};}));assert.equal(f.metrics().unconfirmed_calls,1);}finally{f.close();}
 }
});
