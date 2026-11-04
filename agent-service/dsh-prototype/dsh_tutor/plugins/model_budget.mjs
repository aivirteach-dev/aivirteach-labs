/**
 * 当前请求独占的DSH模型出口预算。只依赖公开llm/stream及Node文件API。
 * 普通推理、工具后续、摘要和重试都必须经过这里；失败尝试不退还预留。
 * 不改变冻结请求。只写白名单元数据，stdout保留给SDK JSON-RPC。
 */
import { appendFileSync, writeFileSync, renameSync, statSync, existsSync } from 'node:fs';
import { performance } from 'node:perf_hooks';

export const name = 'tutor-model-budget';
export const inject = ['llm'];

export function apply(ctx, config) {
  const { limits, provider, model, maxTokens, contextWindow, deadlineUnix, metricsPath, progressPath } = config;
  // 可选：平台接口用它把可见文字片段和实际用量实时交给Python（每行一个JSON，不含提示词）。
  const streamPath = config.streamPath;
  function relay(row) { if (streamPath) appendFileSync(streamPath, JSON.stringify(row)+'\n', {mode:0o600}); }
  if (!limits || !Number.isInteger(maxTokens) || maxTokens < 1 || !Number.isFinite(deadlineUnix)) {
    throw new Error('tutor_budget_config');
  }
  const begun = performance.now();
  const state = { version:1, source:'installed_dsh_guard', model_calls:0,
    input_estimated_tokens:0, output_reserved_tokens:0, output_bytes:0,
    budget_stop:null, unconfirmed_calls:0, timings:{}, actual_usage:'unknown',
    estimation:'ceil_utf8_bytes_div_2_not_tokenizer' };
  let eventCount = 0;
  function save() {
    const temporary=metricsPath+'.tmp';
    writeFileSync(temporary, JSON.stringify(state), {mode:0o600});
    renameSync(temporary, metricsPath);
  }
  function event(stage) {
    if (++eventCount > limits.max_runtime_events) throw new Error('tutor_event_budget');
    let size=0;
    try {size=statSync(progressPath).size;} catch(error) {if(error.code!=='ENOENT') throw error;}
    if(size>60000) throw new Error('tutor_event_budget');
    appendFileSync(progressPath, JSON.stringify({stage,at:performance.now()})+'\n', {mode:0o600});
  }
  function stop(code) {
    state.budget_stop=code;save();
    const error=new Error('tutor_model_budget_'+code);
    error.code='TUTOR_MODEL_BUDGET';
    throw error;
  }
  save(); // 初始化握手结束前必须已经挂载预算；Python会核对此文件。
  ctx.on('llm/stream', async function* (options, next) {
    if(config.cancelPath && existsSync(config.cancelPath)) throw new Error('tutor_cancelled_before_dispatch');
    if(state.model_calls>=limits.max_model_calls) stop('model_calls');
    if(Date.now()/1000>=deadlineUnix) stop('deadline');
    if(options.provider!==provider || options.model!==model) stop('route');
    if(!Number.isInteger(options.maxTokens) || options.maxTokens<1 || options.maxTokens>maxTokens) stop('missing_output_cap');
    const tools=options.tools ?? [];
    if(!Array.isArray(tools) || tools.some(tool=>!(config.allowedTools ?? []).includes(tool.name))) stop('tool_set');
    const content=JSON.stringify({messages:options.messages,tools});
    const bytes=Buffer.byteLength(content,'utf8');
    if(bytes>limits.max_request_bytes) stop('request_bytes');
    // 这是启发式估算并预留余量，不保证真实Token上界，也不用于费用统计。
    const estimate=Math.ceil(bytes/2);
    const perCall=Math.min(limits.max_input_estimated_tokens_per_call,
      contextWindow-options.maxTokens-limits.context_headroom_tokens);
    if(estimate>perCall || state.input_estimated_tokens+estimate>limits.max_total_input_estimated_tokens) stop('input_budget');
    if(state.output_reserved_tokens+options.maxTokens>limits.max_total_output_tokens) stop('output_reservation');
    state.model_calls++;
    state.input_estimated_tokens+=estimate;
    state.output_reserved_tokens+=options.maxTokens;
    state.unconfirmed_calls++;save();event('model_request');
    const started=performance.now();let terminal=false;let chunks=0;
    try {
      for await(const chunk of next()) {
        if(++chunks>(limits.max_stream_chunks ?? 8192)) stop('stream_chunks');
        // 平台模式：学生断开或总超时后停止读取，不让模型在后台继续生成（旧接口语义不变）。
        if(streamPath && config.cancelPath && chunks%16===0 && existsSync(config.cancelPath)) throw new Error('tutor_cancelled');
        if(Buffer.byteLength(JSON.stringify(chunk),'utf8')>limits.max_request_bytes) stop('output_bytes');
        const delta=chunk.type==='text-delta' ? chunk.text :
          chunk.type==='reasoning-delta' ? chunk.text :
          chunk.type==='tool-call-delta' ? chunk.argumentsDelta : '';
        if(typeof delta==='string' && delta.length) {
          state.output_bytes=Math.min(limits.max_output_bytes+1,state.output_bytes+Buffer.byteLength(delta,'utf8'));
          if(chunk.type==='text-delta' && state.timings.first_visible===undefined)
            state.timings.first_visible=(performance.now()-begun)/1000;
          if(state.output_bytes>limits.max_output_bytes) stop('output_bytes');
        }
        // 适配器也会合成error/abort finish；它们不是远端计算终态回执。
        if(chunk.type==='finish' && ['stop','tool-calls','length','max-tokens','content-filter'].includes(chunk.reason?.kind)) terminal=true;
        if(chunk.type==='text-delta' && typeof chunk.text==='string' && chunk.text.length) relay({call:state.model_calls,delta:chunk.text});
        else if(chunk.type==='usage' && chunk.usage && typeof chunk.usage==='object') relay({call:state.model_calls,usage:chunk.usage});
        else if(chunk.type==='finish') relay({call:state.model_calls,finish:chunk.reason?.kind ?? 'unknown'});
        yield chunk;
      }
    } finally {
      state.timings.model_stream=(state.timings.model_stream ?? 0)+(performance.now()-started)/1000;
      if(terminal) state.unconfirmed_calls--;
      save();
    }
  });
  ctx.on('tools/execute', async (exec,next) => {
    event('tool_execution');const started=performance.now();
    try {return await next();}
    finally {state.timings.tool_execution=(state.timings.tool_execution ?? 0)+(performance.now()-started)/1000;save();}
  });
}
