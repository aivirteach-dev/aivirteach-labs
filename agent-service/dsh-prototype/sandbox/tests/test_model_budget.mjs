/** 直接驱动预算插件的 llm/stream 钩子：约束、停止原因和转发行。运行：node --test sandbox/tests/test_model_budget.mjs */
import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { apply } from '../../dsh_tutor/plugins/model_budget.mjs';

function fixture(extra = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'tutor-budget-'));
  const handlers = {};
  apply({ on: (name, fn) => { handlers[name] = fn; } }, {
    provider: 'p', model: 'm', maxModelCalls: 2, maxTokens: 512, maxOutputBytes: 1024,
    deadlineUnix: Date.now() / 1000 + 20, allowedTools: ['mcp__aivirteach_tools__get_vm_status'],
    cancelPath: join(dir, 'cancel.request'), budgetPath: join(dir, 'budget.json'), streamPath: join(dir, 'stream.jsonl'),
    ...extra,
  });
  return {
    dir,
    stream: (options, next) => handlers['llm/stream'](options, next),
    stop: () => JSON.parse(readFileSync(join(dir, 'budget.json'))).budget_stop,
    rows: () => existsSync(join(dir, 'stream.jsonl'))
      ? readFileSync(join(dir, 'stream.jsonl'), 'utf8').trim().split('\n').map((line) => JSON.parse(line)) : [],
    close: () => rmSync(dir, { recursive: true }),
  };
}
const request = (extra = {}) => ({ provider: 'p', model: 'm', maxTokens: 512, tools: [], ...extra });
async function consume(stream) { for await (const ignored of stream); }
async function* reply() {
  yield { type: 'block-start', blockType: 'text' };
  yield { type: 'text-delta', text: '你好' };
  yield { type: 'usage', usage: { inputTokens: 40, outputTokens: 2, totalTokens: 122, cacheReadTokens: 80 } };
  yield { type: 'finish', reason: { kind: 'stop' } };
}

test('handshake file is written when the plugin mounts', () => {
  const f = fixture();
  try { assert.equal(f.stop(), null); } finally { f.close(); }
});

test('relays start, block, delta, usage and finish with call number and timestamp', async () => {
  const f = fixture();
  try {
    await consume(f.stream(request(), reply));
    const rows = f.rows();
    assert.deepEqual(rows.map(({ t, ...rest }) => rest), [
      { call: 1, start: true }, { call: 1, block: 'text' }, { call: 1, delta: '你好' },
      { call: 1, usage: { inputTokens: 40, outputTokens: 2, totalTokens: 122, cacheReadTokens: 80 } },
      { call: 1, finish: 'stop' },
    ]);
    assert.ok(rows.every((row, i) => Number.isInteger(row.t) && (i === 0 || row.t >= rows[i - 1].t)));
  } finally { f.close(); }
});

test('model call limit stops before the provider is called', async () => {
  const f = fixture();
  let called = 0;
  try {
    for (let i = 0; i < 2; i++) await consume(f.stream(request(), () => { called++; return reply(); }));
    await assert.rejects(consume(f.stream(request(), () => { called++; return reply(); })), /model_calls/);
    assert.equal(called, 2);
    assert.equal(f.stop(), 'model_calls');
  } finally { f.close(); }
});

for (const [label, options, code] of [
  ['another provider', request({ provider: 'other' }), 'route'],
  ['another model', request({ model: 'other' }), 'route'],
  ['a larger output cap', request({ maxTokens: 4096 }), 'output_cap'],
  ['a missing output cap', request({ maxTokens: undefined }), 'output_cap'],
  ['an unapproved tool', request({ tools: [{ name: 'shell' }] }), 'tool_set'],
]) {
  test(`${label} is rejected without calling the provider`, async () => {
    const f = fixture();
    let called = false;
    try {
      await assert.rejects(consume(f.stream(options, () => { called = true; return reply(); })), new RegExp(code));
      assert.equal(called, false);
      assert.equal(f.stop(), code);
    } finally { f.close(); }
  });
}

test('an approved tool is allowed', async () => {
  const f = fixture();
  try { await consume(f.stream(request({ tools: [{ name: 'mcp__aivirteach_tools__get_vm_status' }] }), reply)); }
  finally { f.close(); }
});

test('deadline stops new calls', async () => {
  const f = fixture({ deadlineUnix: Date.now() / 1000 - 1 });
  try {
    await assert.rejects(consume(f.stream(request(), reply)), /deadline/);
    assert.equal(f.stop(), 'deadline');
  } finally { f.close(); }
});

test('output byte limit counts text and tool arguments across calls', async () => {
  const f = fixture({ maxOutputBytes: 10 });
  try {
    await assert.rejects(consume(f.stream(request(), async function* () {
      yield { type: 'tool-call-delta', argumentsDelta: '{"a":1}' };
      yield { type: 'text-delta', text: '超过上限' };
    })), /output_bytes/);
    assert.equal(f.stop(), 'output_bytes');
    assert.deepEqual(f.rows().map((row) => row.error).filter(Boolean), ['budget']);
  } finally { f.close(); }
});

test('cancel file stops before a call and during a stream', async () => {
  const f = fixture();
  try {
    await assert.rejects(consume(f.stream(request(), async function* () {
      for (let i = 0; i < 64; i++) {
        if (i === 10) writeFileSync(join(f.dir, 'cancel.request'), '');
        yield { type: 'text-delta', text: 'x' };
      }
    })), /tutor_cancelled/);
    assert.deepEqual(f.rows().map((row) => row.error).filter(Boolean), ['cancelled']);
    await assert.rejects(consume(f.stream(request(), reply)), /tutor_cancelled/);
    assert.equal(f.stop(), null);
  } finally { f.close(); }
});

test('provider failure is relayed as a stable code without its text', async () => {
  const f = fixture();
  try {
    await assert.rejects(consume(f.stream(request(), async function* () { throw new Error('secret upstream text'); })));
    const text = readFileSync(join(f.dir, 'stream.jsonl'), 'utf8');
    assert.ok(text.includes('"stream_error"'));
    assert.ok(!text.includes('secret'));
  } finally { f.close(); }
});

const dropped = (code) => async function* () {
  yield { type: 'usage', usage: { inputTokens: 0, outputTokens: 0, totalTokens: 0 } };
  yield { type: 'finish', reason: { kind: 'error', failure: { message: 'Connection error.', code } } };
};
async function collect(stream) { const out = []; for await (const chunk of stream) out.push(chunk); return out; }

test('a connection error before any output is retried once within the same call', async () => {
  const f = fixture({ maxModelCalls: 1 });
  let attempts = 0;
  try {
    const out = await collect(f.stream(request(), () => (++attempts === 1 ? dropped('TRANSPORT')() : reply())));
    assert.equal(attempts, 2);
    assert.deepEqual(out.map((c) => c.type), ['block-start', 'text-delta', 'usage', 'finish']);
    assert.equal(out.at(-1).reason.kind, 'stop');
    assert.deepEqual(f.rows().filter((r) => r.retry).map((r) => [r.call, r.retry]), [[1, 'TRANSPORT']]);
    assert.equal(f.stop(), null);  // 重试不算新的模型调用
  } finally { f.close(); }
});

test('a thrown timeout before any output is retried once', async () => {
  const f = fixture();
  let attempts = 0;
  try {
    await consume(f.stream(request(), () => (++attempts === 1
      ? (async function* () { throw Object.assign(new Error('Request timed out.'), { code: 'TIMEOUT' }); })()
      : reply())));
    assert.equal(attempts, 2);
  } finally { f.close(); }
});

test('no retry after output started, for other error codes, or a second time', async () => {
  for (const [label, factory, expectedAttempts] of [
    ['after output', () => async function* () { yield { type: 'text-delta', text: '半' }; yield* dropped('TRANSPORT')(); }, 1],
    ['auth error', () => dropped('AUTH'), 1],
    ['second failure', () => dropped('TRANSPORT'), 2],
  ]) {
    const f = fixture();
    let attempts = 0;
    try {
      const out = await collect(f.stream(request(), () => { attempts++; return factory()(); }));
      assert.equal(attempts, expectedAttempts, label);
      assert.equal(out.at(-1).reason.kind, 'error', label);  // 失败原样交给 DSH
    } finally { f.close(); }
  }
});

test('invalid configuration refuses to mount', () => {
  assert.throws(() => apply({ on() {} }, { maxModelCalls: 1, maxTokens: 1, maxOutputBytes: 1 }), /tutor_budget_config/);
});

test('a call that outputs nothing within firstTokenMs is abandoned and retried once', async () => {
  const f = fixture({ firstTokenMs: 30 });
  let attempts = 0;
  async function* silentThenReply() {
    attempts++;
    if (attempts === 1) {
      yield { type: 'block-start', blockType: 'text' };  // 不是内容，不算首字
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
    yield* reply();
  }
  try {
    const started = Date.now();
    await consume(f.stream(request(), silentThenReply));
    assert.ok(Date.now() - started < 1500, 'must not wait for the silent attempt');  // 30ms 放弃 + 300ms 重试间隔
    assert.equal(attempts, 2);
    const rows = f.rows().map(({ t, ...rest }) => rest);
    assert.deepEqual(rows.filter((row) => row.retry), [{ call: 1, retry: 'FIRST_TOKEN_TIMEOUT' }]);
    assert.deepEqual(rows.filter((row) => row.block), [{ call: 1, block: 'text' }]);  // 放弃的那次不转发
  } finally { f.close(); }
});

test('a second silent attempt fails the call', async () => {
  const f = fixture({ firstTokenMs: 30 });
  async function* silent() { await new Promise((resolve) => setTimeout(resolve, 200)); yield* reply(); }
  try {
    await assert.rejects(consume(f.stream(request(), silent)), /first_token_timeout/);
    assert.deepEqual(f.rows().map(({ t, ...rest }) => rest).slice(-1), [{ call: 1, error: 'stream_error' }]);
  } finally { f.close(); }
});
