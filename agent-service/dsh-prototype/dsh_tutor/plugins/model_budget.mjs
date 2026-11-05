/**
 * 平台请求的 DSH 模型出口：约束每次模型调用，并把过程事件转发给 Python。
 *
 * 约束（任一不满足即停止本请求，原因写入 budgetPath）：
 *   调用次数上限、单次输出上限、只允许本请求授权的工具、总时限、输出总字节、取消。
 * 转发（每行一个 JSON，带毫秒时间戳 t，不含提示词）：
 *   start 调用开始 / delta 可见文字 / block 内容块类型 / usage 实际用量 / finish 结束原因 / error 调用失败。
 * 只依赖 DSH 公开的 llm/stream 钩子；stdout 留给 SDK 的 JSON-RPC，不在这里输出。
 */
import { appendFileSync, existsSync, writeFileSync } from 'node:fs';

export const name = 'tutor-model-budget';
export const inject = ['llm'];

export function apply(ctx, config) {
  const {
    provider, model, maxModelCalls, maxTokens, maxOutputBytes, deadlineUnix,
    allowedTools = [], cancelPath, budgetPath, streamPath,
  } = config;
  if (![maxModelCalls, maxTokens, maxOutputBytes].every(Number.isInteger) || !Number.isFinite(deadlineUnix)
      || !budgetPath || !streamPath) {
    throw new Error('tutor_budget_config');
  }
  let calls = 0;
  let outputBytes = 0;
  const relay = (row) => appendFileSync(streamPath, JSON.stringify({ t: Date.now(), ...row }) + '\n', { mode: 0o600 });
  const cancelled = () => Boolean(cancelPath) && existsSync(cancelPath);
  const stop = (code) => {
    writeFileSync(budgetPath, JSON.stringify({ budget_stop: code }), { mode: 0o600 });
    const error = new Error('tutor_model_budget_' + code);
    error.code = 'TUTOR_MODEL_BUDGET';
    throw error;
  };
  // 挂载握手：Python 在发出第一条消息前核对这个文件，确认约束确实生效。
  writeFileSync(budgetPath, JSON.stringify({ budget_stop: null }), { mode: 0o600 });

  ctx.on('llm/stream', async function* (options, next) {
    if (cancelled()) throw new Error('tutor_cancelled');
    if (calls >= maxModelCalls) stop('model_calls');
    if (Date.now() / 1000 >= deadlineUnix) stop('deadline');
    if (options.provider !== provider || options.model !== model) stop('route');
    if (!Number.isInteger(options.maxTokens) || options.maxTokens < 1 || options.maxTokens > maxTokens) stop('output_cap');
    const tools = options.tools ?? [];
    if (!Array.isArray(tools) || tools.some((tool) => !allowedTools.includes(tool.name))) stop('tool_set');

    const call = ++calls;
    relay({ call, start: true });
    let chunks = 0;
    try {
      for await (const chunk of next()) {
        if (++chunks % 16 === 0 && cancelled()) throw new Error('tutor_cancelled');
        const text = chunk.type === 'text-delta' ? chunk.text : chunk.type === 'tool-call-delta' ? chunk.argumentsDelta : '';
        if (typeof text === 'string' && text.length) {
          outputBytes += Buffer.byteLength(text, 'utf8');
          if (outputBytes > maxOutputBytes) stop('output_bytes');
        }
        if (chunk.type === 'text-delta' && typeof chunk.text === 'string' && chunk.text.length) relay({ call, delta: chunk.text });
        else if (chunk.type === 'block-start') relay({ call, block: String(chunk.blockType ?? 'unknown') });
        else if (chunk.type === 'usage' && chunk.usage && typeof chunk.usage === 'object') relay({ call, usage: chunk.usage });
        else if (chunk.type === 'finish') relay({ call, finish: chunk.reason?.kind ?? 'unknown' });
        yield chunk;
      }
    } catch (error) {
      // 只记稳定的原因码，不记提供方返回的原始错误文本。
      relay({ call, error: error?.code === 'TUTOR_MODEL_BUDGET' ? 'budget' : error?.message === 'tutor_cancelled' ? 'cancelled' : 'stream_error' });
      throw error;
    }
  });
}
