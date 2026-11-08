/**
 * 平台请求的 DSH 模型出口：约束每次模型调用，并把过程事件转发给 Python。
 *
 * 约束（任一不满足即停止本请求，原因写入 budgetPath）：
 *   调用次数上限、单次输出上限、只允许本请求授权的工具、总时限、输出总字节、取消。
 * 重试：连接被切断或请求超时（TRANSPORT / TIMEOUT），或 firstTokenMs 内一个字都没输出（FIRST_TOKEN_TIMEOUT），
 *   且模型还没输出任何内容时，同一次调用重试一次。
 * 转发（每行一个 JSON，带毫秒时间戳 t，不含提示词）：
 *   start 调用开始 / delta 可见文字 / block 内容块类型 / usage 实际用量 / finish 结束原因 / error 调用失败 /
 *   retry 因连接类错误重试（值为错误码）。
 * 只依赖 DSH 公开的 llm/stream 钩子；stdout 留给 SDK 的 JSON-RPC，不在这里输出。
 */
import { appendFileSync, existsSync, writeFileSync } from 'node:fs';

export const name = 'tutor-model-budget';
export const inject = ['llm'];

export function apply(ctx, config) {
  const {
    provider, model, maxModelCalls, maxTokens, maxOutputBytes, deadlineUnix,
    allowedTools = [], cancelPath, budgetPath, streamPath, firstTokenMs = 0,
  } = config;
  if (![maxModelCalls, maxTokens, maxOutputBytes].every(Number.isInteger) || !Number.isFinite(deadlineUnix)
      || !budgetPath || !streamPath || !Number.isInteger(firstTokenMs) || firstTokenMs < 0) {
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

  // 连接类错误（连接被切断、请求超时）或首字超时，在模型还没输出任何内容时重试一次：不会出现重复内容，也不算新的模型调用。
  const RETRYABLE_CODES = new Set(['TRANSPORT', 'TIMEOUT', 'FIRST_TOKEN_TIMEOUT']);
  const FIRST_TOKEN_TIMEOUT = Symbol('first_token_timeout');
  const RETRY_DELAY_MS = 300;
  // DSH 的 finish 块把失败放在 reason.failure（实测；turn/end 里叫 reason.error），两种都认。
  const failureCode = (chunk) => (chunk.type === 'finish' && chunk.reason?.kind === 'error'
    ? (chunk.reason.failure ?? chunk.reason.error)?.code : undefined);
  // 模型真正输出的内容（文字、工具调用参数）；在它之前收到的数据块先暂存，确认不重试后再放行。
  const content = (chunk) => (chunk.type === 'text-delta' ? typeof chunk.text === 'string' && chunk.text.length > 0
    : typeof chunk.type === 'string' && chunk.type.endsWith('-delta'));

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
    const pass = (chunk) => {  // 计字节、转发，返回原数据块给 DSH
      const text = chunk.type === 'text-delta' ? chunk.text : chunk.type === 'tool-call-delta' ? chunk.argumentsDelta : '';
      if (typeof text === 'string' && text.length) {
        outputBytes += Buffer.byteLength(text, 'utf8');
        if (outputBytes > maxOutputBytes) stop('output_bytes');
      }
      if (chunk.type === 'text-delta' && typeof chunk.text === 'string' && chunk.text.length) relay({ call, delta: chunk.text });
      else if (chunk.type === 'block-start') relay({ call, block: String(chunk.blockType ?? 'unknown') });
      else if (chunk.type === 'usage' && chunk.usage && typeof chunk.usage === 'object') relay({ call, usage: chunk.usage });
      else if (chunk.type === 'finish') relay({ call, finish: chunk.reason?.kind ?? 'unknown' });
      return chunk;
    };
    try {
      for (let attempt = 0; ; attempt++) {
        const held = [];  // 输出内容之前收到的数据块（block-start、usage、finish 等），确认不重试后再放行
        let started = false;
        let retryCode;
        const iterator = next()[Symbol.asyncIterator]();
        try {
          for (;;) {
            let step;
            if (!started && firstTokenMs > 0) {  // 首字超时：上游一直不返回内容时放弃这次请求
              let timer;
              const waitMs = Math.max(1, Math.min(firstTokenMs, deadlineUnix * 1000 - Date.now()));
              const pending = iterator.next();
              pending.catch(() => {});  // 放弃后上游迟到的错误不能变成未处理的 rejection
              step = await Promise.race([pending, new Promise((resolve) => {
                timer = setTimeout(() => resolve(FIRST_TOKEN_TIMEOUT), waitMs);
              })]);
              clearTimeout(timer);
              if (step === FIRST_TOKEN_TIMEOUT) {
                iterator.return?.().catch?.(() => {});
                const error = new Error('first_token_timeout');
                error.code = 'FIRST_TOKEN_TIMEOUT';
                throw error;
              }
            } else {
              step = await iterator.next();
            }
            if (step.done) break;
            const chunk = step.value;
            if (++chunks % 16 === 0 && cancelled()) throw new Error('tutor_cancelled');
            if (!started && !content(chunk)) {
              const code = failureCode(chunk);
              if (attempt === 0 && RETRYABLE_CODES.has(code)) {
                retryCode = code;
                break;
              }
              held.push(chunk);
              continue;
            }
            if (!started) {
              started = true;
              for (const early of held.splice(0)) yield pass(early);
            }
            yield pass(chunk);
          }
        } catch (error) {
          if (started || attempt > 0 || !RETRYABLE_CODES.has(error?.code)) throw error;
          retryCode = error.code;
        }
        if (retryCode === undefined) {
          for (const late of held) yield pass(late);
          break;
        }
        relay({ call, retry: retryCode });
        await new Promise((resolve) => setTimeout(resolve, RETRY_DELAY_MS));
        if (cancelled()) throw new Error('tutor_cancelled');
        if (Date.now() / 1000 >= deadlineUnix) stop('deadline');
      }
    } catch (error) {
      // 只记稳定的原因码，不记提供方返回的原始错误文本。
      relay({ call, error: error?.code === 'TUTOR_MODEL_BUDGET' ? 'budget' : error?.message === 'tutor_cancelled' ? 'cancelled' : 'stream_error' });
      throw error;
    }
  });
}
