import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { mkdtempSync, mkdirSync, writeFileSync, cpSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import plugin, { sessionOrder } from '../lib/budget-plugin.mjs';
import { LIMITS } from '../lib/limits.mjs';

const library = fileURLToPath(new URL('../lib/', import.meta.url));
process.env.CADRE_PYTHON ||= 'python3';
const temporary = mkdtempSync(join(tmpdir(), 'qe-plugin-'));
after(() => rmSync(temporary, { recursive: true, force: true }));
let sequence = 0;

async function fixture(budget, fetcher, providerID = 'opencode-go') {
  const home = join(temporary, String(++sequence));
  const code = join(home, 'code');
  mkdirSync(join(code, 'lib'), { recursive: true });
  for (const name of ['run_store.py', 'project_mode.py', 'qa_evidence.py', 'permission.py', 'runtime_sync.py', 'monthly_budget.py', 'budget_gate.py', 'model_router.py', 'opencode_adaptor.py', 'go_window.py', 'model_selection.py', 'model_catalog.py', 'level.py', 'prefilter.py', 'limits.py', 'limits.json']) cpSync(join(library, name), join(code, 'lib', name));
  // Fresh fake observation. No real budget reader, credentials, model, or network.
  writeFileSync(join(code, 'lib', 'provider_budget.py'), `import json,datetime\nb=${JSON.stringify(JSON.stringify(budget))}\nd=json.loads(b)\nif isinstance(d,dict): d.update(ts=datetime.datetime.now().isoformat(),today=datetime.date.today().isoformat())\nprint(json.dumps(d))\n`);
  // Each scenario chooses its own mode; the user's shell setting is not the test's default.
  Object.assign(process.env, { CADRE_HOME: home, CADRE_ENGINE_ROOT: code, CADRE_ROUTER_STATUS: join(home, 'chain', 'provider-status.json'),
    OPENCODE_EXPERIMENTAL_NATIVE_LLM: 'false', CADRE_BUDGET_OBSERVATION: '0' });
  process.env.CADRE_RUN_ID = execFileSync(process.env.CADRE_PYTHON, ['-c',
    "import sys;sys.path.insert(0,sys.argv[1]);from run_store import RunStore;print(RunStore().accept('fixture','plain task')['run_id'])",
    join(code, 'lib')], { encoding: 'utf8' }).trim();
  const hooks = await plugin();
  const config = { enabled_providers: [], provider: { [providerID]: { options: { fetch: fetcher } } } };
  await hooks.config(config);
  async function call(role = 'impl', session = 's1', model = 'model', cost = { input: 1, output: 2 }) {
    const input = { sessionID: session, agent: role, model: { providerID, id: model, api: { id: model },
      limit: { context: 100, output: 10 }, cost } };
    const params = { maxOutputTokens: 10 };
    await hooks['chat.params'](input, params);
    const headers = { headers: {} };
    await hooks['chat.headers'](input, headers);
    const invoke = (content = 'plain task', extra = {}) => config.provider[providerID].options.fetch('https://fixture.invalid/chat/completions', {
      method: 'POST', headers: headers.headers, body: JSON.stringify({ model, max_tokens: params.maxOutputTokens, messages: [{ role: 'user', content }], ...extra }) });
    return { invoke, input };
  }
  function rows() {
    return JSON.parse(execFileSync(process.env.CADRE_PYTHON, ['-c',
      "import sqlite3,json,sys;c=sqlite3.connect(sys.argv[1]);c.row_factory=sqlite3.Row;print(json.dumps([dict(r) for r in c.execute('select * from reservation')]))",
      join(home, 'chain', 'request-budget.db')], { encoding: 'utf8' }));
  }
  return { hooks, config, call, rows, home };
}

// 4c-13: the Go observation is in points; its dollar equivalent uses the limit of the $15 model (1 point = $0.15)
const go = (remaining = 1) => ({ go: { db: 'ok', unit: 'points', pay: remaining * 100 / 15, available: remaining * 100 / 15, window_open: true } });
const response = () => new Response(JSON.stringify({ id: 'receipt', usage: { prompt_tokens: 1, completion_tokens: 1 } }),
  { headers: { 'content-type': 'application/json' } });

const orModel = 'deepseek/deepseek-v4-pro';
function orPrices(f, changes = {}) {
  mkdirSync(join(f.home, 'chain'), { recursive: true });
  writeFileSync(join(f.home, 'chain', 'openrouter-price.json'), JSON.stringify({
    version: 2, ts: Date.now() / 1000, models: {
      [orModel]: { input: 1, output: 2, providers: ['deepinfra/fp8'], fp8_cache: true }
    }, ...changes
  }));
}
const orBudget = { or: { db: 'ok', pay: 10, available: 10, remaining: 10 } };

test('project tool confinement preserves the real provider budget boundary and rejects missing policy', async () => {
  const previous = { project: process.env.CADRE_PROJECT_TOOLS, policy: process.env.CADRE_TOOL_POLICY };
  let calls = 0;
  try {
    process.env.CADRE_PROJECT_TOOLS = '1';
    process.env.CADRE_TOOL_POLICY = JSON.stringify({ version: 1, sandbox: '/usr/bin/sandbox-exec',
      profile: '(version 1)(allow default)(deny file-write*)(deny network*)', scratch: temporary });
    const f = await fixture(go(10), async () => { calls += 1; return response(); });
    process.env.CADRE_PROJECT_TOOLS = '0';
    process.env.CADRE_TOOL_POLICY = '{}';
    const tool = { args: { command: '/usr/bin/true' } };
    await f.hooks['tool.execute.before']({ tool: 'bash' }, tool);
    execFileSync('/bin/sh', ['-c', tool.args.command]);
    await assert.rejects(f.hooks['tool.execute.before']({ tool: 'write' }, { args: {} }), /CADRE_TOOL_DENIED/);
    const { invoke } = await f.call();
    const result = await invoke();
    assert.equal(result.status, 200);
    await result.text();
    assert.equal(calls, 1);
    assert.equal(f.rows().filter((r) => r.actual !== null).length, 1);
    process.env.CADRE_PROJECT_TOOLS = '1';
    process.env.CADRE_TOOL_POLICY = JSON.stringify({ version: 1, sandbox: '/usr/bin/sandbox-exec',
      profile: '(version 1)(allow default)(deny file-write*)(deny network*)', scratch: temporary });
    globalThis[Symbol.for(`cadre.provider-gate:${f.home}`)] = true;
    await assert.rejects(plugin(), /existing provider has no matching project sandbox/);
    delete process.env.CADRE_TOOL_POLICY;
    delete globalThis[Symbol.for(`cadre.provider-gate:${f.home}`)];
    await assert.rejects(plugin(), /CADRE_TOOL_DENIED/);
    assert.equal(calls, 1);
  } finally {
    for (const [key, value] of [['CADRE_PROJECT_TOOLS', previous.project], ['CADRE_TOOL_POLICY', previous.policy]]) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
  }
});

test('OpenRouter enforces measured fp8 providers and actual-cost reporting at the transport', async () => {
  const f = await fixture(orBudget, async (_url, options) => {
    const body = JSON.parse(options.body);
    assert.deepEqual(body.provider, { only: ['deepinfra/fp8'], order: ['deepinfra/fp8'], quantizations: ['fp8'],
      allow_fallbacks: false, require_parameters: true, max_price: { prompt: 1.74, completion: 3.84 } });
    assert.deepEqual(body.usage, { include: true });
    return new Response(JSON.stringify({ id: 'actual', usage: { cost: 0.00003 } }));
  }, 'openrouter');
  orPrices(f);
  const call = await f.call('impl', 'or-session', orModel);
  await (await call.invoke('task', { provider: { order: ['Relace'], quantizations: ['fp4'], allow_fallbacks: true },
    usage: { include: false } })).text();
  assert.equal(f.rows()[0].actual, 30);
});

test('N-RT04: a bound endpoint outage preserves its liability and selects an authorized healthy endpoint', async () => {
  for (const wire of ['http', 'sse']) {
    let calls = 0;
    const error = { code: 502, message: 'provider temporarily unavailable', metadata: { error_type: 'provider_unavailable' } };
    const f = await fixture(orBudget, async (_url, options) => {
      const body = JSON.parse(options.body);
      calls += 1;
      assert.deepEqual(body.provider.only, [calls === 1 ? 'streamlake/fp8' : 'deepinfra/fp8']);
      if (calls !== 1) return new Response(JSON.stringify({ usage: { cost: 0.00001 } }));
      return wire === 'http' ? new Response(JSON.stringify({ error }), { status: 502 })
        : new Response(`data: ${JSON.stringify({ error })}\n\ndata: [DONE]\n\n`, { headers: { 'content-type': 'text/event-stream' } });
    }, 'openrouter');
    const endpoint = (input) => ({ input, output: 2, params: ['max_tokens'], max_output: 32768, quantization: 'fp8' });
    const prices = (providers) => orPrices(f, { models: { [orModel]: {
      input: 1, output: 2, providers, fp8_cache: true,
      tiers: { 'streamlake/fp8': endpoint(0.5), 'deepinfra/fp8': endpoint(1) } } } });
    prices(['streamlake/fp8']);
    const { invoke } = await f.call('impl', `endpoint-${wire}`, orModel);
    await (await invoke()).text();
    const failed = f.rows()[0];
    assert.equal(failed.actual, null, 'an outage is not a zero-cost receipt');
    assert.equal(JSON.parse(failed.metadata).provider_error.round, 'provider_unavailable');
    prices(['streamlake/fp8', 'deepinfra/fp8']);
    await (await invoke()).text();
    assert.equal(calls, 2);
    const rows = f.rows();
    assert.equal(rows.length, 2);
    assert.equal(rows[0].actual, null);
    assert.equal(rows[0].reserved, failed.reserved);
    assert.equal(rows[1].actual, 10);
    assert.deepEqual(JSON.parse(rows[1].metadata).endpoint_selection.order, ['deepinfra/fp8']);
    execFileSync(process.env.CADRE_PYTHON, ['-c',
      "import os,sys;sys.path.insert(0,sys.argv[1]);from run_store import RunStore;RunStore().update(os.environ['CADRE_RUN_ID'],budget_cap_usd=0)",
      join(f.home, 'code', 'lib')]);
    await assert.rejects(invoke(), /CADRE_BUDGET_DENIED/);
    assert.equal(calls, 2, 'endpoint selection cannot bypass the run cap');
    assert.equal(f.rows().length, 2, 'a denied attempt makes no new reservation');
  }
});

test('4c-7: an endpoint that cannot carry the requested output is dropped, order by live price, low-uptime endpoints last', async () => {
  const bodies = [];
  const f = await fixture(orBudget, async (_url, options) => {
    bodies.push(JSON.parse(options.body));
    return new Response(JSON.stringify({ id: 'actual', usage: { cost: 0.00001 } }));
  }, 'openrouter');
  const endpoint = (input, output, max_output, uptime = 99.9) => ({ input, output, params: ['max_tokens'], max_output, uptime, quantization: 'fp8' });
  orPrices(f, { models: { [orModel]: { input: 1.3, output: 2.6, providers: ['deepinfra/fp8', 'streamlake/fp8'], fp8_cache: true,
    tiers: { 'deepinfra/fp8': endpoint(1.3, 2.6, 8), 'streamlake/fp8': endpoint(0.2, 0.4, 384000) } },
    'z-ai/glm-5.3': { input: 1.4, output: 4.4, providers: ['novita/fp8', 'sail-research/fp8', 'z-ai/fp8'], fp8_cache: true,
      tiers: { 'novita/fp8': endpoint(0.42, 1.32, 131072, 90), 'sail-research/fp8': endpoint(0.2, 3.4, 943718), 'z-ai/fp8': endpoint(1.4, 4.4, 131072) } } } });
  await (await (await f.call('impl', 'ds', orModel)).invoke()).text();
  assert.deepEqual(bodies[0].provider.only, ['streamlake/fp8'], 'DeepInfra with an 8-token upper bound cannot accept a 10-token request');
  assert.equal(f.rows()[0].reserved, Math.ceil(100 * 0.2 + 10 * 0.4), 'Reserve at the highest price among remaining endpoints');
  await (await (await f.call('impl', 'glm', 'z-ai/glm-5.3')).invoke()).text();
  assert.deepEqual(bodies[1].provider.order, ['sail-research/fp8', 'z-ai/fp8', 'novita/fp8'], 'cheaper first; Novita falls behind at 90% uptime');
  assert.deepEqual(bodies[1].provider.max_price, { prompt: 1.4, completion: 4.4 });
  assert.deepEqual(bodies[1].provider.quantizations, ['fp8']);
});

test('16(4): with budget-type thinking the gate measures max_tokens − budget and does not deny; the same request without thinking is denied', async () => {
  let call = 0;
  const f = await fixture(go(1), async () => { call++; return new Response(JSON.stringify({ type: 'message', id: 'm',
    usage: { input_tokens: 1, output_tokens: 1 } }), { headers: { 'content-type': 'application/json' } }); });
  const model = 'anthropic-formatted';
  const input = { sessionID: 's-budget', agent: 'impl', model: { providerID: 'opencode-go', id: model,
    api: { id: model, npm: '@ai-sdk/anthropic' }, limit: { context: 100000, output: 10 }, cost: { input: 1, output: 2 } } };
  await f.hooks['chat.params'](input, { maxOutputTokens: 10 });
  const request = async (extra) => {
    const headers = { headers: {} };
    await f.hooks['chat.headers'](input, headers);
    return (await f.config.provider['opencode-go'].options.fetch('https://fixture.invalid/messages', { method: 'POST',
      headers: headers.headers, body: JSON.stringify({ model, max_tokens: 20, messages: [{ role: 'user', content: 'x' }], ...extra }) })).text();
  };
  await request({ thinking: { type: 'enabled', budget_tokens: 15 } });   // visible 20 − 15 = 5 ≤ 10
  assert.equal(call, 1);
  await assert.rejects(request({}), /CADRE_BUDGET_DENIED/);              // visible 20 > 10
  assert.equal(call, 1);
});

test('4c QA2: an output request that fits no endpoint is not dropped but clipped to the largest endpoint cap', async () => {
  const bodies = [];
  const f = await fixture(orBudget, async (_url, options) => {
    bodies.push(JSON.parse(options.body));
    return new Response(JSON.stringify({ id: 'actual', usage: { cost: 0.00001 } }));
  }, 'openrouter');
  const endpoint = (max_output) => ({ input: 1, output: 2, params: ['max_tokens'], max_output, uptime: 99.9, quantization: 'fp8' });
  orPrices(f, { models: { [orModel]: { input: 1, output: 2, providers: ['deepinfra/fp8', 'streamlake/fp8'], fp8_cache: true,
    tiers: { 'deepinfra/fp8': endpoint(6), 'streamlake/fp8': endpoint(8) } } } });
  await (await (await f.call('impl', 'kirp', orModel)).invoke()).text();   // 10 tokens requested
  assert.equal(bodies[0].max_tokens, 8);
  assert.deepEqual(bodies[0].provider.only, ['streamlake/fp8']);
});

test('4c-7: OR is denied if the rule-allowed endpoint is not fp8 or the last good price is older than 6 hours', async () => {
  let request = 0;
  const f = await fixture(orBudget, async () => { request++; return new Response(JSON.stringify({ id: 'x', usage: { cost: 0.00001 } })); }, 'openrouter');
  const endpoint = (quantization) => ({ input: 1, output: 2, params: ['max_tokens'], max_output: 1000, quantization });
  orPrices(f, { models: { 'z-ai/glm-5.3': { input: 1, output: 2, providers: ['A', 'B'], fp8_cache: true, tiers: { A: endpoint('fp8'), B: endpoint('fp4') } } } });
  await assert.rejects(async () => (await (await f.call('impl', 'g1', 'z-ai/glm-5.3')).invoke()).text(), /CADRE_BUDGET_DENIED/);
  orPrices(f, { models: { [orModel]: { input: 1, output: 2, providers: ['deepinfra/fp8'], fp8_cache: true, price_ts: Date.now() / 1000 - 7 * 3600 } } });
  await assert.rejects(async () => (await (await f.call('impl', 'g2', orModel)).invoke()).text(), /CADRE_BUDGET_DENIED/);
  orPrices(f, { models: { [orModel]: { input: 1, output: 2, providers: ['deepinfra/fp8'], fp8_cache: true, price_ts: Date.now() / 1000 - 5 * 3600 } } });
  await (await (await f.call('impl', 'g3', orModel)).invoke()).text();
  assert.equal(request, 1, 'Use the last good price within the age limit');
});

test('Sol is reserved only at the price of the allowed standard endpoint; off-policy tiers and incompatible parameters are denied before the network', async () => {
  const gpt = 'openai/gpt-6.1-sol';
  const bodies = [];
  const f = await fixture(orBudget, async (_url, options) => {
    bodies.push(JSON.parse(options.body));
    return new Response(JSON.stringify({ id: 'actual', usage: { cost: 0.00004 } }));
  }, 'openrouter');
  orPrices(f, { models: { [gpt]: { input: 2, output: 10, providers: ['openai/flex', 'openai', 'azure'], fp8_cache: true, closed: true,
    max_price: { prompt: 2, completion: 10 }, tiers: {
      'openai/flex': { input: 1, output: 5, params: ['max_tokens'] }, openai: { input: 2, output: 10, params: ['max_tokens'] },
      azure: { input: 2, output: 10, params: ['max_completion_tokens'] } } } } });
  await assert.rejects(f.call('plan', 'old-prices', gpt), /CADRE_BUDGET_DENIED/);
  assert.equal(bodies.length, 0, 'a price list containing FLEX/Azure tiers outside policy permits no call');
  orPrices(f, { models: { [gpt]: { input: 2, output: 10, providers: ['openai'], fp8_cache: true, closed: true,
    max_price: { prompt: 2, completion: 10 }, tiers: {
      openai: { input: 2, output: 10, params: ['max_tokens'] } } } } });
  const call = await f.call('plan', 'gpt-session', gpt);
  await (await call.invoke('plan')).text();
  assert.deepEqual(bodies[0].provider, { only: ['openai'], order: ['openai'], allow_fallbacks: false,
    max_price: { prompt: 2, completion: 10 } }, 'Only the allowed standard endpoint; no fp8 filter for closed weights');
  assert.equal(bodies[0].temperature, undefined);
  assert.equal(f.rows()[0].actual, 40);
  const resv = f.rows()[0].reserved;
  const incompatible = await f.call('plan', 'gpt-session-2', gpt);
  await assert.rejects(incompatible.invoke('plan', { max_tokens: undefined, max_completion_tokens: 10 }), /CADRE_BUDGET_DENIED/);
  assert.equal(bodies.length, 1, 'an unsupported endpoint parameter cannot leak to another endpoint');
  assert.equal(resv, 100 * 2 + 10 * 10, 'Reserve using the upper price bound of the allowed standard endpoint');
});

test('OpenRouter never substitutes a token estimate for a missing or invalid receipt cost', async () => {
  for (const cost of [undefined, -1, '0.5', null, 0, 0.00002]) {
    const f = await fixture(orBudget, async () => new Response(JSON.stringify({ id: 'actual',
      usage: { prompt_tokens: 1, completion_tokens: 1, cost } })), 'openrouter');
    orPrices(f);
    const call = await f.call('impl', 'or-session', orModel);
    await (await call.invoke()).text();
    assert.equal(f.rows()[0].actual, typeof cost === 'number' && cost >= 0 ? Math.ceil(cost * 1e6) : null);
  }
});

test('OpenRouter rejects missing, stale, future and unapproved price observations before network', async () => {
  let requests = 0;
  const f = await fixture(orBudget, async () => { requests++; return response(); }, 'openrouter');
  await assert.rejects(() => f.call('impl', 's', orModel), /CADRE_BUDGET_DENIED/);
  for (const changes of [{ ts: Date.now() / 1000 - 3601 }, { ts: Date.now() / 1000 + 60 },
    { version: 1 }, { models: { [orModel]: { input: 1, output: 2, providers: ['Relace'], fp8_cache: true } } }]) {
    orPrices(f, changes);
    await assert.rejects(() => f.call('impl', 's', orModel), /CADRE_BUDGET_DENIED/);
  }
  orPrices(f);
  const call = await f.call('impl', 's', orModel);
  orPrices(f, { ts: Date.now() / 1000 - 3601 });
  process.env.CADRE_BUDGET_OBSERVATION = '1';
  try { await assert.rejects(call.invoke, /CADRE_BUDGET_DENIED/); }
  finally { process.env.CADRE_BUDGET_OBSERVATION = '0'; }
  assert.equal(requests, 0);
});

test('unknown budget blocks main, subagent and compaction before provider invocation', async () => {
  let requests = 0;
  const f = await fixture(null, async () => { requests++; return response(); });
  for (const role of ['impl', 'cadre-review', 'compaction']) {
    const call = await f.call(role);
    await assert.rejects(call.invoke, /CADRE_BUDGET_DENIED/);
  }
  assert.equal(requests, 0);
});

test('reservation exists at provider entry and receipt releases only unused allowance', async () => {
  const f = await fixture(go(), async (_url, options) => {
    assert.equal(f.rows().length, 1);
    assert.equal(f.rows()[0].actual, null);
    assert.equal(options.headers.has('x-cadre-budget-context'), false);
    assert.equal(options.redirect, 'error');
    return response();
  });
  const call = await f.call('compaction', 'session-owned');
  await (await call.invoke()).text();
  const row = f.rows()[0];
  assert.equal(row.reserved, 120);
  assert.equal(row.actual, 3);
  assert.equal(JSON.parse(row.metadata).session_id, 'session-owned');
  assert.equal(JSON.parse(row.metadata).role, 'compaction');
});

test('each SDK retry requires its own reservation; network failure keeps liability', async () => {
  let requests = 0;
  const f = await fixture(go(0.00015), async () => { requests++; throw new Error('connection reset'); });
  const call = await f.call();
  await assert.rejects(call.invoke, /connection reset/);
  await assert.rejects(call.invoke, /CADRE_BUDGET_DENIED/);
  assert.equal(requests, 1);
  assert.equal(f.rows()[0].actual, null);
});

test('truncated stream cannot be settled as zero cost', async () => {
  const f = await fixture(go(), async () => new Response('data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
    { headers: { 'content-type': 'text/event-stream' } }));
  const call = await f.call();
  await (await call.invoke()).text();
  assert.equal(f.rows()[0].actual, null);
});

test('SSE receipt persists measured usage after complete stream', async () => {
  const f = await fixture(go(), async () => new Response('data: {"id":"r","usage":{"prompt_tokens":2,"completion_tokens":3}}\n\ndata: [DONE]\n\n',
    { headers: { 'content-type': 'text/event-stream' } }));
  await (await (await f.call()).invoke()).text();
  assert.equal(f.rows()[0].actual, 8);
});

test('native transport or unknown request cannot enable an unguarded paid call', async () => {
  const f = await fixture(go(), async () => { throw new Error('must not call'); });
  await assert.rejects(() => f.config.provider['opencode-go'].options.fetch('https://fixture.invalid/chat/completions',
    { method: 'POST', body: '{}' }), /CADRE_BUDGET_DENIED/);
  process.env.OPENCODE_EXPERIMENTAL_NATIVE_LLM = 'true';
  const baseline = { enabled_providers: [] };
  await assert.rejects(() => f.hooks.config(baseline), /CADRE_BUDGET_DENIED/);
  assert.deepEqual(baseline.enabled_providers, []);
});

test('overlapping helper contexts keep their own session and role receipts', async () => {
  const f = await fixture(go(), async () => response());
  const first = await f.call('impl', 'session-one');
  const second = await f.call('compaction', 'session-two');
  await Promise.all([first.invoke().then(r => r.text()), second.invoke().then(r => r.text())]);
  const identities = f.rows().map(r => { const m = JSON.parse(r.metadata); return [m.session_id, m.role]; });
  assert.deepEqual(identities, [['session-one','impl'],['session-two','compaction']]);
});

test('reservation is sized by the request body, not the model context limit (B36)', async () => {
  // A 1M-context model with a daily share of $1: the old calculation (1e6 tokens x $1/M = $1.00002) would deny every call.
  const f = await fixture(go(1), async () => response());
  const model = 'big-context';
  const input = { sessionID: 's9', agent: 'impl', model: { providerID: 'opencode-go', id: model, api: { id: model },
    limit: { context: 1000000, output: 10 }, cost: { input: 1, output: 2 } } };
  const params = { maxOutputTokens: 10 };
  await f.hooks['chat.params'](input, params);
  const headers = { headers: {} };
  await f.hooks['chat.headers'](input, headers);
  const body = JSON.stringify({ model, max_tokens: params.maxOutputTokens, messages: [{ role: 'user', content: 'short request' }] });
  const res = await f.config.provider['opencode-go'].options.fetch('https://fixture.invalid/chat/completions', { method: 'POST', headers: headers.headers, body });
  assert.equal(res.ok, true);
  await res.text();   // the receipt closes when the body is consumed
  const rows = f.rows().filter(r => JSON.parse(r.metadata).model === model);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].reserved, (body.length + 4096) * 1 + 10 * 2);   // request size + margin, not the model limit
  assert.ok(rows[0].reserved < 20000);
  assert.equal(rows[0].actual, 1 * 1 + 1 * 2);                           // the receipt closed with the measured usage
});

test('zero-cost models are admitted, counted as requests and kept out of the dollar bucket (0b)', async () => {
  // Old behaviour: cost.input === 0 -> quote() DENY with 'inRate <= 0'. So zen/Go free models could never be used.
  const f = await fixture(go(1), async () => response());
  const model = 'free-model';
  const input = { sessionID: 's-free', agent: 'impl', model: { providerID: 'opencode-go', id: model, api: { id: model },
    limit: { context: 128000, output: 10 }, cost: { input: 0, output: 0 } } };
  const params = { maxOutputTokens: 10 };
  await f.hooks['chat.params'](input, params);
  const headers = { headers: {} };
  await f.hooks['chat.headers'](input, headers);
  const body = JSON.stringify({ model, max_tokens: params.maxOutputTokens, messages: [{ role: 'user', content: 'short request' }] });
  const res = await f.config.provider['opencode-go'].options.fetch('https://fixture.invalid/chat/completions', { method: 'POST', headers: headers.headers, body });
  assert.equal(res.ok, true);
  await res.text();
  const rows = f.rows().filter(r => JSON.parse(r.metadata).model === model);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].provider, 'opencode-go:free');   // its own bucket
  assert.equal(rows[0].reserved, 1);                    // REQUESTS are counted, not dollars
  assert.equal(rows[0].actual, 1);
});

test('S1: the call and the reservation use the same effective output limit; unverified model capacity is denied', async () => {
  const f = await fixture(go(1), async () => response());
  const previous = process.env.CADRE_ROLE;
  process.env.CADRE_ROLE = 'quality';
  try {
    const model = 'narrow-output-free';
    const input = { sessionID: 's-enabled', agent: 'impl', model: { providerID: 'opencode-go', id: model, api: { id: model },
      limit: { context: 200000, output: 32000 }, cost: { input: 0, output: 0 } } };
    const params = {};
    await f.hooks['chat.params'](input, params);
    assert.equal(params.maxOutputTokens, 32000);
    const headers = { headers: {} };
    await f.hooks['chat.headers'](input, headers);
    for (const limit of [undefined, { context: 200000 }, { context: 200000, output: 0 }]) {
      const uncertain = { ...input, model: { ...input.model, id: `unknown-${JSON.stringify(limit ?? null)}`, limit } };
      await assert.rejects(() => f.hooks['chat.params'](uncertain, {}), /CADRE_BUDGET_DENIED/);
      await assert.rejects(() => f.hooks['chat.headers'](uncertain, { headers: {} }), /CADRE_BUDGET_DENIED/);
    }
  } finally {
    if (previous === undefined) delete process.env.CADRE_ROLE; else process.env.CADRE_ROLE = previous;
  }
});

test('a model with unknown or partial pricing is treated as paid, never as free (0b)', async () => {
  const f = await fixture(go(1), async () => response());
  for (const cost of [undefined, { input: 0 }, { output: 0 }]) {
    const model = `uncertain-${JSON.stringify(cost ?? null)}`;
    const input = { sessionID: 's-unknown', agent: 'impl', model: { providerID: 'opencode-go', id: model, api: { id: model },
      limit: { context: 1000, output: 10 }, cost } };
    await assert.rejects(() => f.hooks['chat.params'](input, { maxOutputTokens: 10 }), /CADRE_BUDGET_DENIED/);
  }
  // No reservation was opened: the ledger file was never created (so f.rows() is not called).
});

const zen = (remaining = 1) => ({ zen: { db: 'ok', pay: remaining, available: remaining } });

test('zen calls pass through the same gate as every other paid provider (0c)', async () => {
  // before 0c: 'opencode' was not in the supported provider list, so every zen call was denied in chat.params.
  let request = 0;
  const f = await fixture(zen(1), async () => { request++; return response(); }, 'opencode');
  const call = await f.call('impl', 'zen-session', 'zen-model');
  await (await call.invoke()).text();
  assert.equal(request, 1);
  const row = f.rows()[0];
  assert.equal(row.provider, 'opencode');
  assert.ok(row.reserved > 0);
  assert.equal(row.actual, 1 * 1 + 1 * 2);
});

test('zen without a readable balance denies before the network (0c)', async () => {
  let request = 0;
  const f = await fixture({ zen: { db: 'absent', pay: 0, available: 0 } }, async () => { request++; return response(); }, 'opencode');
  const call = await f.call('impl', 'zen-session', 'zen-model');
  await assert.rejects(call.invoke, /CADRE_BUDGET_DENIED/);
  assert.equal(request, 0);
});

test('a 429 from Go stamps the window marker that the budget reader consumes (item 5)', async () => {
  // The stamp file was read until now but NEVER written: the "we hit the limit" signal was dead code.
  const { existsSync, readFileSync } = await import('node:fs');
  const f = await fixture(go(1), async () => new Response('{"error":"rate limit"}', { status: 429, headers: { 'content-type': 'application/json' } }));
  const stamp = join(f.home, 'chain', 'go-429-last.txt');
  assert.equal(existsSync(stamp), false);
  const call = await f.call();
  const res = await call.invoke();
  assert.equal(res.status, 429);
  await res.text();
  assert.equal(existsSync(stamp), true);
  const second = Number(readFileSync(stamp, 'utf8'));
  assert.ok(Math.abs(second - Math.floor(Date.now() / 1000)) < 60, 'The timestamp must be close to now');
});

test('cached input is settled at the cache rate, not the most expensive input rate (item 6)', async () => {
  // Measured defect: the receipt closed ALL input at the most expensive rate. Cache reads cost 1/5-1/100 of the input
  // price, so at a 90% hit rate the ledger recorded 8.5 times reality.
  const answer = (usage) => new Response(JSON.stringify({ id: 'receipt', usage }),
    { headers: { 'content-type': 'application/json' } });
  const f = await fixture(go(1), async () => answer({ prompt_tokens: 1000, completion_tokens: 10,
    prompt_tokens_details: { cached_tokens: 900 } }));
  const model = 'cached';
  const input = { sessionID: 's-cache', agent: 'impl', model: { providerID: 'opencode-go', id: model, api: { id: model },
    limit: { context: 100000, output: 10 }, cost: { input: 1, output: 2, cache: { read: 0.1, write: 2 } } } };
  const params = { maxOutputTokens: 10 };
  await f.hooks['chat.params'](input, params);
  const headers = { headers: {} };
  await f.hooks['chat.headers'](input, headers);
  const body = JSON.stringify({ model, max_tokens: 10, messages: [{ role: 'user', content: 'x' }] });
  await (await f.config.provider['opencode-go'].options.fetch('https://fixture.invalid/chat/completions',
    { method: 'POST', headers: headers.headers, body })).text();
  const row = f.rows().filter(r => JSON.parse(r.metadata).model === model)[0];
  // 100 fresh x 1 + 900 cache x 0.1 + 10 output x 2 = 100 + 90 + 20 = 210
  assert.equal(row.actual, 210);
  // the reservation still uses the MOST EXPENSIVE rate (safety): cache.write = 2
  assert.ok(row.reserved > row.actual, 'the reservation must retain a safety margin');
});

test('cached input with no catalogue cache price is settled at the input rate (B230: unknown discount is no discount)', async () => {
  // 25.09 panel audit: Cerebras charged 4.1M cache reads at the full input price ($4.06); the catalogue had no cache price
  // so readRate counted as 0, and the ledger and the daily share recorded $4.07 too little (credit 10.18 vs panel 6.11).
  const answer = (usage) => new Response(JSON.stringify({ id: 'receipt', usage }),
    { headers: { 'content-type': 'application/json' } });
  const f = await fixture(go(1), async () => answer({ prompt_tokens: 1000, completion_tokens: 10,
    prompt_tokens_details: { cached_tokens: 900 } }));
  const model = 'uncached-catalog';
  const input = { sessionID: 'no-s-cache', agent: 'impl', model: { providerID: 'opencode-go', id: model, api: { id: model },
    limit: { context: 100000, output: 10 }, cost: { input: 1, output: 2 } } };
  const params = { maxOutputTokens: 10 };
  await f.hooks['chat.params'](input, params);
  const headers = { headers: {} };
  await f.hooks['chat.headers'](input, headers);
  const body = JSON.stringify({ model, max_tokens: 10, messages: [{ role: 'user', content: 'x' }] });
  await (await f.config.provider['opencode-go'].options.fetch('https://fixture.invalid/chat/completions',
    { method: 'POST', headers: headers.headers, body })).text();
  const row = f.rows().filter(r => JSON.parse(r.metadata).model === model)[0];
  // 100 fresh x 1 + 900 cache x 1 (discount unknown = no discount) + 10 output x 2 = 1020
  assert.equal(row.actual, 1020);
});

test('cache-creation tokens are settled at the write rate (item 6)', async () => {
  const answer = (usage) => new Response(JSON.stringify({ id: 'receipt', usage }),
    { headers: { 'content-type': 'application/json' } });
  const f = await fixture(go(1), async () => answer({ prompt_tokens: 1000, completion_tokens: 10,
    cache_creation_input_tokens: 400, cache_read_input_tokens: 100 }));
  const model = 'write';
  const input = { sessionID: 's-w', agent: 'impl', model: { providerID: 'opencode-go', id: model, api: { id: model },
    limit: { context: 100000, output: 10 }, cost: { input: 1, output: 2, cache: { read: 0.1, write: 3 } } } };
  await f.hooks['chat.params'](input, { maxOutputTokens: 10 });
  const headers = { headers: {} };
  await f.hooks['chat.headers'](input, headers);
  const body = JSON.stringify({ model, max_tokens: 10, messages: [{ role: 'user', content: 'x' }] });
  await (await f.config.provider['opencode-go'].options.fetch('https://fixture.invalid/chat/completions',
    { method: 'POST', headers: headers.headers, body })).text();
  const row = f.rows().filter(r => JSON.parse(r.metadata).model === model)[0];
  // 500 fresh x 1 + 100 read x 0.1 + 400 write x 3 + 10 output x 2 = 500 + 10 + 1200 + 20 = 1730
  assert.equal(row.actual, 1730);
});

// 4c-11: free providers (Gemini/Groq) have no opencode route; the request-counting bucket is now only a zero-priced model (Zen *-free).
const FREE = { zen: { db: 'ok', pay: 0, available: 0, remaining: 0 } };
const ZERO = { input: 0, output: 0 };

test('a free request is always counted, even when the stream ends badly (live finding 17.09)', async () => {
  // 11 of 31 free Gemini calls were left pending: when the stream looked broken the receipt was never written.
  // What the free bucket measures is the REQUEST COUNT; if not counted, the daily cap fills with pending reservations.
  const broken = () => new Response('{"not valid JSON', { headers: { 'content-type': 'application/json' } });
  const f = await fixture(FREE, async () => broken(), 'opencode');
  const call = await f.call('impl', 'ses-free', 'mimo-v2.6-flash-free', ZERO);
  await (await call.invoke()).text();
  const rows = f.rows();
  assert.equal(rows.length, 1);
  assert.equal(rows[0].provider, 'opencode:free');   // 4c-11: Gemini was removed; the counted bucket is a zero-priced model
  assert.equal(rows[0].reserved, 1);
  assert.equal(rows[0].actual, 1, 'a request must count even when its stream breaks');
});

test('a free request is counted when the transport itself throws (B83, 17.09 second audit)', async () => {
  // Two sibling branches were closed on 17.09 (broken stream, HTTP error) but the THIRD is open: `await previous(...)`
  // had no try/catch, so a network drop / timeout / `redirect:'error'` / `cadre cancel` left the reservation pending without
  // being counted. The pending total has no period filter, so every event was PERMANENTLY subtracted from the daily free
  // bucket: the provider slowly closes and the only fix is a manual `cadre recover --stale-close`.
  const f = await fixture(FREE,
    async () => { throw new TypeError('fetch failed'); }, 'opencode');
  const call = await f.call('impl', 'ses-disconnected', 'mimo-v2.6-flash-free', ZERO);
  await assert.rejects(call.invoke(), /fetch failed/);
  const rows = f.rows();
  assert.equal(rows.length, 1);
  assert.equal(rows[0].provider, 'opencode:free');   // 4c-11: Gemini was removed; the counted bucket is a zero-priced model
  assert.equal(rows[0].actual, 1, 'Count the request even on a network error; a pending entry would leak bucket capacity');
});

test('a free request rejected by the provider is still counted (audit 17.09)', async () => {
  // The previous test closed the 200 OK + broken body branch; the LIVE defect was in the HTTP ERROR branch:
  // in run 20260917-121533 Gemini returned 429 ("You exceeded your current quota") 96 times, receiptStream
  // returned without a receipt on `!response.ok` and 96 of 96 reservations were left pending. A pending reservation
  // has no period filter -> it is PERMANENTLY subtracted from the daily request bucket (110 pending `google` lines in the ledger,
  // daily cap 450). A denied request is still a request made: it must be counted.
  const f = await fixture(FREE,
    async () => new Response('{"error":{"code":429,"message":"You exceeded your current quota"}}',
      { status: 429, headers: { 'content-type': 'application/json' } }), 'opencode');
  const call = await f.call('compaction', 'ses-429', 'mimo-v2.6-flash-free', ZERO);
  const res = await call.invoke();
  assert.equal(res.status, 429);
  await res.text();
  const rows = f.rows();
  assert.equal(rows.length, 1);
  assert.equal(rows[0].provider, 'opencode:free');   // 4c-11: Gemini was removed; the counted bucket is a zero-priced model
  assert.equal(rows[0].reserved, 1);
  assert.equal(rows[0].actual, 1, 'A 429 is also a request; leaving it pending would permanently close the free bucket');
});

test('a paid request rejected by the provider still keeps its reservation (money risk)', async () => {
  // The free bucket's fix must not leak into the PAID bucket: there an unknown amount KEEPS the reservation. 429 is the exception (4c-4).
  const f = await fixture(go(1), async () => new Response('{"error":"upstream"}',
    { status: 500, headers: { 'content-type': 'application/json' } }));
  const call = await f.call();
  await (await call.invoke()).text();
  assert.equal(f.rows()[0].actual, null, 'a paid call with an unknown amount after denial retains its reservation');
  const g = await fixture(go(1), async () => new Response('{"error":"rate limit"}',
    { status: 429, headers: { 'content-type': 'application/json' } }));
  await (await (await g.call()).invoke()).text();
  assert.deepEqual([g.rows()[0].actual, g.rows()[0].receipt.endsWith(':http-429')], [0, true], 'an unprocessed 429 leaves no liability');
});

test('a paid request with an unreadable stream keeps its reservation (money risk)', async () => {
  const broken = () => new Response('{"not valid JSON', { headers: { 'content-type': 'application/json' } });
  const f = await fixture(go(1), async () => broken());
  const call = await f.call();
  await (await call.invoke()).text();
  const row = f.rows()[0];
  assert.equal(row.actual, null, 'a paid call with an unknown amount KEEPS its reservation');
});

test('a free request with a corrupt SSE stream is counted (B110-1)', async () => {
  // `if (invalid) return` on the FIRST line of `flush()` swallowed the free receipt: on a broken SSE `item()` already
  // set invalid=true during transform and flush returned without writing anything. The measured unit is the REQUEST.
  const f = await fixture(FREE,
    async () => new Response('data: {"choices":[{"delta"\n\ndata: {"id":"x"\n\n',
      { headers: { 'content-type': 'text/event-stream' } }), 'opencode');
  const call = await f.call('impl', 'ses-sse-broken', 'mimo-v2.6-flash-free', ZERO);
  await (await call.invoke()).text();
  assert.equal(f.rows()[0].actual, 1, 'a request must count even when its SSE stream breaks');
});

test('a free request larger than the 4MB stream guard is counted (B110-1)', async () => {
  // a >4MB body sets `invalid=true`; the old flush also returned without a receipt on this branch.
  const f = await fixture(FREE,
    async () => new Response('data: ' + 'x'.repeat(5 * 1024 * 1024) + '\n\n',
      { headers: { 'content-type': 'text/event-stream' } }), 'opencode');
  const call = await f.call('impl', 'ses-big', 'mimo-v2.6-flash-free', ZERO);
  await (await call.invoke()).text();
  assert.equal(f.rows()[0].actual, 1, 'a request must count even when the 4MB guard trips');
});

test('a free request whose body the consumer cancels is counted (B110-1)', async () => {
  // If the consumer cancels the body `flush` is NEVER called (`cancel` is called): in a live run `cadre cancel` and
  // a timeout go exactly through this path and the reservation was left pending.
  const body = () => new Response(new ReadableStream({
    start(controller) { controller.enqueue(new TextEncoder().encode('data: {"id":"r"}\n\n')); },
  }), { headers: { 'content-type': 'text/event-stream' } });
  const f = await fixture(FREE, async () => body(), 'opencode');
  const call = await f.call('impl', 'ses-cancel', 'mimo-v2.6-flash-free', ZERO);
  const reader = (await call.invoke()).body.getReader();
  await reader.read();
  await reader.cancel('cancel');
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(f.rows()[0].actual, 1, 'a cancelled free request has still been made');
});

test('a paid request whose body the consumer cancels keeps its reservation (B110-1)', async () => {
  // The free bucket's cancel fix must not leak into the PAID bucket: if the amount is unknown the full reservation stays.
  const body = () => new Response(new ReadableStream({
    start(controller) { controller.enqueue(new TextEncoder().encode('data: {"id":"r"}\n\n')); },
  }), { headers: { 'content-type': 'text/event-stream' } });
  const f = await fixture(go(1), async () => body());
  const reader = (await (await f.call()).invoke()).body.getReader();
  await reader.read();
  await reader.cancel('cancel');
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(f.rows()[0].actual, null, 'a paid call with an unknown amount after cancellation retains its reservation');
});

test('FIX-70: an OpenRouter stream cut before its receipt records the generation id for reconciliation', async () => {
  const f = await fixture(orBudget, async () => new Response(new ReadableStream({
    start(controller) { controller.enqueue(new TextEncoder().encode('data: {"id":"gen-fix70abc","choices":[{"delta":{"content":"x"}}]}\n\n')); },
  }), { headers: { 'content-type': 'text/event-stream' } }), 'openrouter');
  orPrices(f);
  const reader = (await (await f.call('impl', 'or-cut', orModel)).invoke()).body.getReader();
  await reader.read();
  await reader.cancel('cut');
  await new Promise((r) => setTimeout(r, 50));
  const line = f.rows()[0];
  assert.equal(line.actual, null, 'an unknown amount retains its reservation');
  assert.equal(JSON.parse(line.metadata).generation, 'gen-fix70abc', 'Record the production identity for reconciliation');
});

test('observe mode records manual opencode spend without ever blocking it (B124)', async () => {
  // User decision 19.09: "opencode run means manual use — the budget must NOT CUT it but WATCH it."
  // In a run environment (CADRE_RUN_ID) the mode is ignored: automated work is always subject to the cap.
  let call = 0;
  const f = await fixture(go(0), async () => { call++; return response(); });   // daily share 0 = the gate normally denies
  delete process.env.CADRE_RUN_ID;

  const cut = await f.call('build');
  await assert.rejects(() => cut.invoke('task'), /CADRE_BUDGET_DENIED/, 'the gate blocks when observation mode is disabled');
  assert.equal(call, 0, 'a denied call never reaches the provider');

  process.env.CADRE_BUDGET_OBSERVATION = '1';
  try {
    const observed = await f.call('build');
    const answer = await observed.invoke('job');
    assert.equal(answer.status, 200, 'The call passes in observation mode');
    assert.equal(call, 1, 'the call actually reached the provider');
    await answer.text();
    const lines = f.rows();
    assert.ok(lines.length > 0, 'spending is recorded in the ledger and remains visible');
  } finally {
    delete process.env.CADRE_BUDGET_OBSERVATION;
  }
});

test('observe mode is ignored inside a run: automatic work always obeys the cap (B124)', async () => {
  let call = 0;
  const f = await fixture(go(0), async () => { call++; return response(); });
  process.env.CADRE_BUDGET_OBSERVATION = '1';
  process.env.CADRE_RUN_ID = '20260919-000000-testrun';
  try {
    const run = await f.call('build');
    await assert.rejects(() => run.invoke('job'), /CADRE_BUDGET_DENIED/,
      'Observation mode does not open the gate during a run');
    assert.equal(call, 0, 'A denied run call never reaches the provider');
  } finally {
    delete process.env.CADRE_BUDGET_OBSERVATION;
    delete process.env.CADRE_RUN_ID;
  }
});

test('the request-context map is bounded: stale contexts are evicted, recent retries still admitted (B139)', async () => {
  // Every `chat.headers` call leaves a context record; previously records were NEVER deleted and accumulated over a single opencode
  // process. Limit: the oldest records are dropped; a recent SDK retry carries the same header,
  // so new records stay at the gate.
  let requests = 0;
  const f = await fixture(go(1_000_000), async () => { requests++; return response(); });
  const first = await f.call('impl', 's-first');
  let last;
  for (let i = 0; i < 2048; i++) last = await f.call('impl', `s-${i}`);
  await assert.rejects(first.invoke, /CADRE_BUDGET_DENIED/);
  const admitted = await last.invoke();
  assert.equal(admitted.status, 200);
  assert.equal(requests, 1);
});

test('a plugin-side refusal records the provider so the engine can ban it (B154)', async () => {
  const f = await fixture(go(1), async () => response());
  const input = { sessionID: 's-x', agent: 'impl', model: { providerID: 'opencode-go', id: 'priceless', api: { id: 'priceless' },
    limit: { context: 100, output: 10 } } };   // no price -> the plugin denies
  await assert.rejects(() => f.hooks['chat.params'](input, { maxOutputTokens: 10 }), /CADRE_BUDGET_DENIED/);
  const denials = JSON.parse(execFileSync(process.env.CADRE_PYTHON, ['-c',
    "import sys,json;sys.path.insert(0,sys.argv[1]);from run_store import RunStore;print(json.dumps(RunStore().read(sys.argv[2]).get('denials') or []))",
    join(f.home, 'code', 'lib'), process.env.CADRE_RUN_ID], { encoding: 'utf8' }));
  assert.ok(denials.some((d) => d.provider === 'opencode-go' && d.reason), JSON.stringify(denials));
});

test('a ledger refusal is recorded once and always with its provider (B169)', async () => {
  // 23.09 log scan: on a ledger denial the Python record (with provider) + the plugin's catch denial (without provider, 'plugin refusal')
  // wrote two lines in the same second; if only the provider-less line remains the engine bans all providers.
  const f = await fixture(null, async () => response());
  const call = await f.call('impl');
  await assert.rejects(call.invoke, /CADRE_BUDGET_DENIED/);
  const denials = JSON.parse(execFileSync(process.env.CADRE_PYTHON, ['-c',
    "import sys,json;sys.path.insert(0,sys.argv[1]);from run_store import RunStore;print(json.dumps(RunStore().read(sys.argv[2]).get('denials') or []))",
    join(f.home, 'code', 'lib'), process.env.CADRE_RUN_ID], { encoding: 'utf8' }));
  assert.ok(denials.length >= 1, JSON.stringify(denials));
  assert.ok(denials.every((d) => d.provider && d.provider !== 'None'), 'denial record has no provider: ' + JSON.stringify(denials));
  assert.equal(denials.filter((d) => d.provider === 'opencode-go').length, 1, 'the same denial is recorded once: ' + JSON.stringify(denials));
});

test('free providers refuse an invalid output cap before provider invocation (B186)', async () => {
  // after B169 number() returns NaN; on the free path chat.params should deny an invalid limit early.
  let requests = 0;
  const f = await fixture(null, async () => { requests++; return response(); }, 'opencode');
  const input = { sessionID: 's-free', agent: 'impl', model: { providerID: 'opencode', id: 'free-x', api: { id: 'free-x' },
    cost: { input: 0, output: 0 }, limit: { context: 100, output: 10 } } };
  await assert.rejects(() => f.hooks['chat.params'](input, { maxOutputTokens: -5 }), /CADRE_BUDGET_DENIED/);
  assert.equal(requests, 0);
});

// 24.09 OpenAI (GPT-6): opencode calls OpenAI via the Responses API — usage arrives in the `response.completed` event
// as `response.usage.{input_tokens, input_tokens_details.cached_tokens, output_tokens}`, there is no `[DONE]`.
// Zen GPT models stream in the OpenAI Responses format (4c-1: direct OpenAI was removed, the parser stays for Zen).
async function responsesCall(f, sse) {
  const model = 'gpt-6-luna';
  const input = { sessionID: 's-oa', agent: 'compaction', model: { providerID: 'opencode', id: model, api: { id: model },
    limit: { context: 100000, output: 10 }, cost: { input: 1, output: 2, cache: { read: 0.1, write: 2 } } } };
  const params = { maxOutputTokens: 10 };
  await f.hooks['chat.params'](input, params);
  const headers = { headers: {} };
  await f.hooks['chat.headers'](input, headers);
  const body = JSON.stringify({ model, input: [{ role: 'user', content: 'x' }], max_output_tokens: 10, stream: true });
  return (await f.config.provider.opencode.options.fetch('https://fixture.invalid/v1/responses',
    { method: 'POST', headers: headers.headers, body })).text();
}

test('an OpenAI Responses stream is settled from response.completed usage, cached input at the cache rate', async () => {
  const sse = 'data: {"type":"response.created","response":{"id":"resp_1"}}\n\n'
    + 'data: {"type":"response.output_text.delta","delta":"summary"}\n\n'
    + 'data: {"type":"response.completed","response":{"id":"resp_1","usage":{"input_tokens":1000,'
    + '"input_tokens_details":{"cached_tokens":900},"output_tokens":10,"output_tokens_details":{"reasoning_tokens":4}}}}\n\n';
  const f = await fixture(zen(1), async () => new Response(sse, { headers: { 'content-type': 'text/event-stream' } }), 'opencode');
  await responsesCall(f, sse);
  const row = f.rows().filter(r => JSON.parse(r.metadata).model === 'gpt-6-luna')[0];
  assert.equal(row.provider, 'opencode');
  assert.equal(row.actual, 210, '100 fresh x 1 + 900 cache x 0.1 + 10 output x 2');
});

test('Anthropic receipts include cache input for JSON and both stream endings (B310 B319 B321)', async () => {
 for (const mode of ['message_stop', 'stop_reason', 'json', 'invalid', 'invalid-stream', 'unfinished']) {
  // Live 02.10: the @ai-sdk/anthropic stream was not recognized (ends with message_stop, usage in message_start+message_delta) ->
  // 17/17 reservations were left pending, $1.28 of falsely held money filled the lane cap, the cloud was denied.
  const sse = 'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","type":"message","role":"assistant",'
    + '"usage":{"input_tokens":100,"cache_read_input_tokens":900,"cache_creation_input_tokens":0,"output_tokens":1}}}\n\n'
    + 'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"x"}}\n\n'
    + 'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":10}}\n\n'
    + 'event: message_stop\ndata: {"type":"message_stop"}\n\n';
  const payload = mode === 'json' || mode === 'invalid'
    ? JSON.stringify({ type: 'message', id: 'msg_1', usage: { input_tokens: 100, cache_read_input_tokens: 900,
        cache_creation_input_tokens: 0, output_tokens: mode === 'invalid' ? -1 : 10 } })
    : mode === 'invalid-stream' ? sse.replace('"output_tokens":10', '"output_tokens":-1')
    : mode === 'message_stop' ? sse : mode === 'stop_reason' ? sse.split('event: message_stop')[0]
    : sse.split('event: message_delta')[0];
  const f = await fixture(go(1), async () => new Response(payload,
    { headers: { 'content-type': ['json', 'invalid'].includes(mode) ? 'application/json' : 'text/event-stream' } }));
  const model = 'anthropic-formatted';
  const input = { sessionID: 's-anth', agent: 'impl', model: { providerID: 'opencode-go', id: model, api: { id: model, npm: '@ai-sdk/anthropic' },
    limit: { context: 100000, output: 10 }, cost: { input: 1, output: 2, cache: { read: 0.1, write: 2 } } } };
  const params = { maxOutputTokens: 10 };
  await f.hooks['chat.params'](input, params);
  const headers = { headers: {} };
  await f.hooks['chat.headers'](input, headers);
  const body = JSON.stringify({ model, max_tokens: 10, messages: [{ role: 'user', content: 'x' }] });
  await (await f.config.provider['opencode-go'].options.fetch('https://fixture.invalid/messages',
    { method: 'POST', headers: headers.headers, body })).text();
  const row = f.rows().filter(r => JSON.parse(r.metadata).model === model)[0];
  if (['invalid', 'invalid-stream', 'unfinished'].includes(mode)) { assert.equal(row.actual, null, mode); continue; }
  assert.notEqual(row.actual, null, 'Write the receipt; leaving the reservation pending would falsely exhaust the cap');
  assert.equal(row.actual, 210, '100 fresh x 1 + 900 cache x 0.1 + 10 output x 2');
  assert.match(row.receipt, /:msg_1$/);
 }
});

test('LIVE1: silent streams are never aborted; the open-request marker follows the request', async (t) => {
  const { readdirSync, readFileSync } = await import('node:fs');
  const requests = mkdtempSync(join(tmpdir(), 'qe-request-'));
  const previous = process.env.CADRE_REQUEST_DIR;
  process.env.CADRE_REQUEST_DIR = requests;
  const spin = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
  try {
    // 1) Cloud: a marker is written at request entry; the stream is NOT CUT by a fixed timeout, the marker is deleted when it ends.
    let upstream, signal;
    const f = await fixture(go(), async (_url, options) => {
      signal = options.signal;
      return new Response(new ReadableStream({ start(c) { upstream = c; } }),
        { headers: { 'content-type': 'text/event-stream' } });
    });
    const call = await f.call('impl', 's-live');
    const res = await call.invoke();
    await spin();
    assert.equal(readdirSync(requests).length, 1, 'Write an open-request marker on entry and retain it while the stream is silent');
    upstream.enqueue(new TextEncoder().encode('data: {"id":"live","usage":{"prompt_tokens":1,"completion_tokens":1}}\n\ndata: [DONE]\n\n'));
    upstream.close();
    await res.text();
    await spin();
    assert.equal(signal.aborted, false, 'a running stream is not cancelled by a fixed timeout');
    assert.equal(f.rows()[0].actual, 3);
    assert.equal(readdirSync(requests).length, 0, 'Request completed; remove the marker');
    // 3) Cancel: the SDK cancels the body -> the signal aborts and the marker is cleaned immediately.
    let cancelSignal;
    const h = await fixture(go(), async (_url, options) => {
      cancelSignal = options.signal;
      return new Response(new ReadableStream({ start(c) { c.enqueue(new TextEncoder().encode(': flow\n\n')); } }),
        { headers: { 'content-type': 'text/event-stream' } });
    });
    const hcall = await h.call('impl', 's-cancel');
    const chargeback = await hcall.invoke();
    await spin();
    assert.equal(readdirSync(requests).length, 1, 'Write the marker before cancellation');
    await chargeback.body.cancel('user cancelled');
    await spin();
    assert.equal(cancelSignal.aborted, true);
    assert.equal(readdirSync(requests).length, 0, 'Remove the marker for a cancelled request');
  } finally {
    if (typeof previous === 'undefined') delete process.env.CADRE_REQUEST_DIR; else process.env.CADRE_REQUEST_DIR = previous;
    rmSync(requests, { recursive: true, force: true });
  }
});

test('Zen without a readable balance observation denies before the network', async () => {
  let called = false;
  const f = await fixture({ zen: { db: 'absent', pay: 0, available: 0 } }, async () => { called = true; return response(); }, 'opencode');
  await assert.rejects(responsesCall(f, ''), /CADRE_BUDGET_DENIED/);
  assert.equal(called, false);
});

test('referee and QA calls get a role output cap so the acceptance guarantee fits small run caps (06.10 P6)', async () => {
  const f = await fixture(go(), async () => new Response('{}'));
  const model = { providerID: 'opencode-go', id: 'm', api: { id: 'm' }, limit: { context: 100000, output: 131072 }, cost: { input: 1, output: 2 } };
  for (const [role, expected] of [['quality', 32768], ['qa', 32768], ['impl', 32768]]) {   // 4c-4: limits.json
    process.env.CADRE_ROLE = role;
    const params = { maxOutputTokens: 131072 };
    await f.hooks['chat.params']({ sessionID: 's', agent: 'build', model }, params);
    assert.equal(params.maxOutputTokens, expected, role);
  }
  delete process.env.CADRE_ROLE;
});

test('the 2x output retry is not clipped back to the opencode default output cap (QA3)', async () => {
  const f = await fixture(go(), async () => new Response('{}'));
  const model = { providerID: 'opencode-go', id: 'm', api: { id: 'm' }, limit: { context: 100000, output: 131072 }, cost: { input: 1, output: 2 } };
  process.env.CADRE_ROLE = 'impl';
  try {
    for (const [multiplier, expected] of [[undefined, 32000], ['2', 65536]]) {
      if (multiplier) process.env.CADRE_OUTPUT_MULTIPLIER = multiplier; else delete process.env.CADRE_OUTPUT_MULTIPLIER;
      const params = { maxOutputTokens: 32000 };
      await f.hooks['chat.params']({ sessionID: 's', agent: 'build', model }, params);
      assert.equal(params.maxOutputTokens, expected, String(multiplier));
    }
  } finally {
    delete process.env.CADRE_ROLE;
    delete process.env.CADRE_OUTPUT_MULTIPLIER;
  }
});

test('4c-4: open-request marker records chunk kind; an SSE comment is not progress, a token is', async () => {
  const { readdirSync, readFileSync } = await import('node:fs');
  const requests = mkdtempSync(join(tmpdir(), 'qe-mark-'));
  const previous = process.env.CADRE_REQUEST_DIR;
  process.env.CADRE_REQUEST_DIR = requests;
  const spin = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
  try {
    let upstream;
    const f = await fixture(go(), async () => new Response(new ReadableStream({ start(c) { upstream = c; } }),
      { headers: { 'content-type': 'text/event-stream' } }));
    const res = await (await f.call('impl', 's-mark')).invoke();
    const reader = res.body.getReader();
    const mark = () => JSON.parse(readFileSync(join(requests, readdirSync(requests)[0]), 'utf8'));
    upstream.enqueue(new TextEncoder().encode(': OPENROUTER PROCESSING\n\n'));
    await reader.read(); await spin();
    assert.equal(mark().progress_at, null, 'A comment only shows that the connection is open');
    for (const empty of ['data: {"type":"ping"}\n\n', 'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n\n',
                       'data: {"choices":[{"delta":{}}]}\n\n', 'data: {"choices":[{"delta":{}}],"usage":null}\n\n']) {
      upstream.enqueue(new TextEncoder().encode(empty));
      await reader.read(); await spin();
      assert.equal(mark().progress_at, null, empty);
    }
    upstream.enqueue(new TextEncoder().encode('data: {"choices":[{"delta":{"content":"x"}}]}\n\n'));
    await reader.read(); await spin();
    assert.equal(mark().round, 'token');
    assert.equal(typeof mark().progress_at, 'number', 'a token chunk counts as progress');
    upstream.close();
    await reader.read();
  } finally {
    if (previous === undefined) delete process.env.CADRE_REQUEST_DIR; else process.env.CADRE_REQUEST_DIR = previous;
    rmSync(requests, { recursive: true, force: true });
  }
});

test('16(13): on an OpenRouter tool round the plugin puts missing reasoning_details back and leaves existing ones untouched', async () => {
  const bodies = [];
  const sse = (...lines) => new Response(lines.map((v) => `data: ${JSON.stringify(v)}\n\n`).join('') + 'data: [DONE]\n\n',
    { headers: { 'content-type': 'text/event-stream' } });
  const f = await fixture(orBudget, async (_url, options) => {
    bodies.push(JSON.parse(options.body));
    if (bodies.length > 1) return sse({ id: 'r2', choices: [{ delta: { content: 'finished' }, finish_reason: 'stop' }], usage: { cost: 0.00001 } });
    return sse(
      { id: 'r1', choices: [{ delta: { reasoning_details: [{ type: 'reasoning.text', index: 0, text: 'a' }] } }] },
      { id: 'r1', choices: [{ delta: { reasoning_details: [{ type: 'reasoning.text', index: 0, text: 'b' }],
                                       tool_calls: [{ id: 'call_1', function: { name: 'read' } }] } }] },
      { id: 'r1', choices: [{ delta: {}, finish_reason: 'tool_calls' }], usage: { cost: 0.00002 } });
  }, 'openrouter');
  orPrices(f);
  const first = await f.call('impl', 'or-loop', orModel);
  await (await first.invoke()).text();
  const second = await f.call('impl', 'or-loop', orModel);
  const messages = [{ role: 'user', content: 'job' }, { role: 'assistant', content: '', tool_calls: [{ id: 'call_1', type: 'function' }] },
                    { role: 'tool', tool_call_id: 'call_1', content: 'file' }];
  await (await second.invoke('proceed', { messages: messages })).text();
  assert.deepEqual(bodies[1].messages[1].reasoning_details, [{ type: 'reasoning.text', index: 0, text: 'ab' }]);
  const third = await f.call('impl', 'or-loop', orModel);
  const moved = [messages[0], { ...messages[1], reasoning_details: [{ type: 'reasoning.text', index: 0, text: 'client' }] }, messages[2]];
  await (await third.invoke('proceed', { messages: moved })).text();
  assert.equal(bodies[2].messages[1].reasoning_details[0].text, 'client', 'reasoning already carried by the client is preserved');
});

test('CR-10: fragmented UTF-8 reasoning reaches the real budgeted continuation with an empty client field', async () => {
  const bodies = [];
  const f = await fixture(orBudget, async (_url, options) => {
    bodies.push(JSON.parse(options.body));
    if (bodies.length > 1) return new Response(JSON.stringify({ id: 'second', usage: { cost: .00001 } }));
    const frames = ['Résumé ', 'summary'].map(text => ({ id: 'first', choices: [{ delta: {
      reasoning_details: [{ type: 'reasoning.text', text }], tool_calls: [{ id: 'utf8-call' }]
    } }] }));
    frames.push({ id: 'first', choices: [{ delta: {}, finish_reason: 'tool_calls' }], usage: { cost: .00002 } });
    const bytes = new TextEncoder().encode(frames.map(row => 'data: ' + JSON.stringify(row) + '\n\n').join('') + 'data: [DONE]\n\n');
    let index = 0;
    return new Response(new ReadableStream({ pull(controller) {
      if (index === bytes.length) { controller.close(); return; }
      controller.enqueue(bytes.slice(index, ++index));
    } }), { headers: { 'content-type': 'text/event-stream' } });
  }, 'openrouter');
  orPrices(f);
  const first = await f.call('impl', 'cr10-utf8', orModel);
  await (await first.invoke()).text();
  const second = await f.call('impl', 'cr10-utf8', orModel);
  await (await second.invoke('continue', { messages: [{ role: 'assistant', reasoning_details: [],
    tool_calls: [{ id: 'utf8-call', type: 'function' }] }, { role: 'tool', tool_call_id: 'utf8-call', content: 'source' }] })).text();
  assert.deepEqual(bodies[1].messages[0].reasoning_details, [{ type: 'reasoning.text', text: 'Résumé summary' }]);
  assert.equal(f.rows().length, 2);
  assert.equal(f.rows().reduce((total, row) => total + row.actual, 0), 30);
});

test('CR-10: cut, malformed and empty reasoning never replays a partial prefix while receipts remain honest', async () => {
  for (const kind of ['length', 'eof', 'malformed-json', 'malformed-details', 'empty']) {
    const bodies = [];
    const f = await fixture(orBudget, async (_url, options) => {
      bodies.push(JSON.parse(options.body));
      if (bodies.length > 1) return new Response(JSON.stringify({ id: 'continued', usage: { cost: .00001 } }));
      const details = kind === 'empty' ? [] : kind === 'malformed-details' ? [null] : [{ type: 'reasoning.text', index: 0, text: 'partial' }];
      const first = { id: 'cut', choices: [{ delta: { reasoning_details: details, tool_calls: [{ id: 'cut-call' }] } }] };
      let wire = 'data: ' + JSON.stringify(first) + '\n\n';
      if (kind === 'malformed-json') wire += 'data: {broken\n\n';
      if (kind !== 'eof') wire += 'data: ' + JSON.stringify({ id: 'cut', choices: [{ delta: {},
        finish_reason: kind === 'length' ? 'length' : 'tool_calls' }], usage: { cost: .00002 } }) + '\n\ndata: [DONE]\n\n';
      return new Response(wire, { headers: { 'content-type': 'text/event-stream' } });
    }, 'openrouter');
    orPrices(f);
    const first = await f.call('impl', 'cr10-cut-' + kind, orModel);
    await (await first.invoke()).text();
    assert.equal(f.rows()[0].actual, ['eof', 'malformed-json'].includes(kind) ? null : 20, kind);
    const second = await f.call('impl', 'cr10-cut-' + kind, orModel);
    await (await second.invoke('continue', { messages: [{ role: 'assistant',
      tool_calls: [{ id: 'cut-call', type: 'function' }] }, { role: 'tool', tool_call_id: 'cut-call', content: 'source' }] })).text();
    assert.equal(bodies[1].messages[0].reasoning_details, undefined, kind);
    assert.equal(bodies.length, 2, kind);
  }
});

const sseResponse = (...parts) => new Response(parts.map((v) => `data: ${JSON.stringify(v)}\n\n`).join('') + 'data: [DONE]\n\n',
  { headers: { 'content-type': 'text/event-stream' } });
const tier = (input, output, entry_name, cache = input / 10) => ({ input, output, cache, entry_name, params: ['max_tokens'], max_output: 1000,
  uptime: 99.9, quantization: 'fp8' });
function two(f, a, b, priceTs = Date.now() / 1000 - 5) {
  orPrices(f, { models: { [orModel]: { input: Math.max(a.input, b.input), output: Math.max(a.output, b.output),
    providers: ['streamlake/fp8', 'deepinfra/fp8'], fp8_cache: true, price_ts: priceTs,
    tiers: { 'streamlake/fp8': a, 'deepinfra/fp8': b } } } });
}
const meta = (row) => JSON.parse(row.metadata);
async function sticky(second, body) {
  const previous = LIMITS.openrouter.session_sticky_sec;
  LIMITS.openrouter.session_sticky_sec = second;
  try { return await body(); } finally { LIMITS.openrouter.session_sticky_sec = previous; }
}

test('CR-52: OpenRouter receipt keeps real usage, cache, reasoning and the served endpoint on the same ledger row', async () => {
  const f = await fixture(orBudget, async () => sseResponse(
    { id: 'gen-52', provider: 'StreamLake', model: orModel, choices: [{ index: 0, delta: { content: 'complete' } }] },
    { id: 'gen-52', provider: 'StreamLake', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] },
    { id: 'gen-52', provider: 'StreamLake', choices: [], usage: { prompt_tokens: 1200, completion_tokens: 300,
      completion_tokens_details: { reasoning_tokens: 120 }, prompt_tokens_details: { cached_tokens: 1000, cache_write_tokens: 0 },
      cost: 0.0004, cache_discount: 0.0002, is_byok: false } }), 'openrouter');
  two(f, tier(0.4, 0.8, 'StreamLake'), tier(1.3, 2.6, 'DeepInfra'));
  await (await (await f.call('impl', 'session-52', orModel)).invoke()).text();
  const [row] = f.rows();
  assert.equal(row.actual, 400, 'the receipt amount is the provider usage.cost value; do not sum the token breakdown again');
  const { duration_ms: duration, price_ts: moment, ...usage } = meta(row).usage;
  assert.deepEqual(usage, { input: 1200, output: 300, thinking: 120, cache_reading: 1000, cache_write: 0, fee_usd: 0.0004,
    cache_discount_usd: 0.0002, byok: false, endpoint: 'StreamLake', endpoint_label: 'streamlake/fp8', model_response: orModel,
    end: 'stop', format: 'chat', price_source: 'openrouter-endpoints' });
  assert.ok(Number.isSafeInteger(duration) && duration >= 0);
  assert.ok(moment > 0 && moment <= Date.now() / 1000);
});

test('CR-52: Go, Anthropic and Responses usage map to one shape; absent fields stay absent; cut streams store nothing', async () => {
  const go1 = await fixture(go(), async () => new Response(JSON.stringify({ id: 'r', model: 'glm-5.3', choices: [{ finish_reason: 'stop',
    message: { content: 'x' } }], usage: { prompt_tokens: 10, completion_tokens: 5, prompt_tokens_details: { cached_tokens: 4 } } }),
    { headers: { 'content-type': 'application/json' } }));
  await (await (await go1.call()).invoke()).text();
  const { duration_ms: _s1, ...goUsage } = meta(go1.rows()[0]).usage;
  assert.deepEqual(goUsage, { input: 10, output: 5, cache_reading: 4, model_response: 'glm-5.3', end: 'stop', format: 'chat',
    price_source: 'opencode-catalog' });
  const sse = 'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_9","model":"qwen3.8-flash","usage":'
    + '{"input_tokens":3,"cache_read_input_tokens":5,"cache_creation_input_tokens":2,"output_tokens":1}}}\n\n'
    + 'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":7}}\n\n'
    + 'event: message_stop\ndata: {"type":"message_stop"}\n\n';
  const anth = await fixture(go(1), async () => new Response(sse, { headers: { 'content-type': 'text/event-stream' } }));
  const input = { sessionID: 's-a', agent: 'impl', model: { providerID: 'opencode-go', id: 'q', api: { id: 'q', npm: '@ai-sdk/anthropic' },
    limit: { context: 100000, output: 10 }, cost: { input: 1, output: 2, cache: { read: 0.1, write: 2 } } } };
  const params = { maxOutputTokens: 10 };
  await anth.hooks['chat.params'](input, params);
  const headers = { headers: {} };
  await anth.hooks['chat.headers'](input, headers);
  await (await anth.config.provider['opencode-go'].options.fetch('https://fixture.invalid/messages', { method: 'POST',
    headers: headers.headers, body: JSON.stringify({ model: 'q', max_tokens: 10, messages: [{ role: 'user', content: 'x' }] }) })).text();
  const { duration_ms: _s2, ...anthUsage } = meta(anth.rows()[0]).usage;
  assert.deepEqual(anthUsage, { input: 10, output: 7, cache_reading: 5, cache_write: 2, model_response: 'qwen3.8-flash',
    end: 'end_turn', format: 'anthropic', price_source: 'opencode-catalog' });
  const orOnly = await fixture(orBudget, async () => new Response(JSON.stringify({ id: 'gen-x', usage: { cost: 0.00003 } })), 'openrouter');
  orPrices(orOnly);
  await (await (await orOnly.call('impl', 's-or', orModel)).invoke()).text();
  assert.equal(orOnly.rows()[0].actual, 30);
  assert.deepEqual(Object.keys(meta(orOnly.rows()[0]).usage).sort(), ['duration_ms', 'fee_usd', 'format', 'price_source', 'price_ts'],
    'If token usage is absent, omit the field rather than writing zero');
  const cut = await fixture(go(), async () => new Response('data: {"choices":[{"delta":{"content":"half"}}],"usage":null}\n\n',
    { headers: { 'content-type': 'text/event-stream' } }));
  await (await (await cut.call()).invoke()).text();
  assert.equal(cut.rows()[0].actual, null);
  assert.equal(meta(cut.rows()[0]).usage, undefined);
});

test('CR-49: stable instruction/tool prefix and session continuation are measured on the body actually sent', async () => {
  const f = await fixture(go(10), async () => response());
  const toolDefs = [{ type: 'function', function: { name: 'read' } }];
  const first = [{ role: 'system', content: 'constant instruction' }, { role: 'user', content: 'job' }];
  const proceed = [...first, { role: 'assistant', content: '', tool_calls: [{ id: 'c1', type: 'function' }] },
                 { role: 'tool', tool_call_id: 'c1', content: 'output' }];
  const send = async (sessionId, messages, tools = toolDefs) => (await (await f.call('impl', sessionId)).invoke('x', { messages, tools })).text();
  await send('p1', first);
  await send('p1', proceed);
  await send('p1', [{ role: 'system', content: 'changed instruction' }, ...proceed.slice(1)]);
  await send('p2', first);
  await send('p1', [{ role: 'system', content: 'changed instruction' }, { role: 'user', content: 'pruned' }, ...proceed.slice(2)]);
  const prefix = f.rows().map((r) => meta(r).prefix);
  assert.equal(prefix[0].proceed, null, 'the first request in the session');
  assert.equal(prefix[1].proceed, true, 'the previous request all_items messages appear first unchanged');
  assert.equal(prefix[1].constant, prefix[0].constant);
  assert.notEqual(prefix[2].constant, prefix[0].constant, 'The instruction changed');
  assert.equal(prefix[2].proceed, false);
  assert.equal(prefix[3].constant, prefix[0].constant, 'the same instruction/tool prefix is identical across sessions');
  assert.equal(prefix[3].proceed, null);
  assert.equal(prefix[4].constant, prefix[2].constant);
  assert.equal(prefix[4].proceed, false, 'Changing an earlier message breaks the cache prefix');
  assert.ok(prefix.every((o) => /^[0-9a-f]{16}$/.test(o.constant) && Number.isSafeInteger(o.message)));
});

test('CR-48: session order keeps the cached endpoint while staying is cheaper; discount end, stale or disabled record switch', () => {
  const now_ts = 1_000_000;
  const m = { tiers: { A: { input: 1, output: 1, cache: 0.1 }, B: { input: 0.8, output: 1, cache: 0.08 } } };
  const record = { endpoint: 'A', order: 'A', success: true, character: 90_000, ts: now_ts - 60 };
  const shared = { m, providers: ['B', 'A'], input: 100_000, requested: 1000, record, endpoints: {}, remainingSec: NaN, now_ts,
    stickySec: 600, errorSec: 1800, proceed: true };
  const remaining_amount = sessionOrder(shared);
  assert.deepEqual(remaining_amount.providers, ['A', 'B'], 'staying_cost 20000 < switching cost 81000');
  assert.match(remaining_amount.reason.join(' '), /preserved/);
  const markup = sessionOrder({ ...shared, m: { tiers: { ...m.tiers, A: { input: 10, output: 10, cache: 1 } } } });
  assert.deepEqual(markup.providers, ['B', 'A'], 'The discount ended; switching is cheaper even with cache loss');
  assert.match(markup.reason.join(' '), /left/);
  assert.deepEqual(sessionOrder({ ...shared, m: { tiers: { ...m.tiers, A: { input: 1, output: 1 } } } }).providers, ['B', 'A'],
    'an endpoint with unknown cache pricing gets no assumed discount');
  assert.deepEqual(sessionOrder({ ...shared, record: { ...record, ts: now_ts - 601 } }).providers, ['B', 'A'], 'stale session record');
  assert.deepEqual(sessionOrder({ ...shared, stickySec: 0, errorSec: 0 }).providers, ['B', 'A'], 'a disabled policy keeps the original order');
  const uncertain = sessionOrder({ ...shared, record: { ...record, endpoint: null, order: 'A' } });
  assert.deepEqual(uncertain.providers, ['B', 'A'], 'Without a known actual endpoint, the first preference proves no cache location and gets no credit');
  assert.match(uncertain.reason.join(' '), /actual endpoint unknown/);
  assert.deepEqual(uncertain.session, { endpoint: 'A', endpoint_source: 'first_preference', prefix: true, cache_credit: 0 });
  assert.deepEqual(remaining_amount.session, { endpoint: 'A', endpoint_source: 'response', prefix: true, cache_credit: 90_000 });
  assert.deepEqual(sessionOrder({ ...shared, providers: ['B'] }).providers, ['B'], 'An endpoint no longer allowed must not return');
});

test('CR-48: failed/429 endpoints lose stickiness and go last; measured slow endpoints that cannot fit the time are not first', () => {
  const now_ts = 2_000_000;
  const m = { tiers: { A: { input: 0.2, output: 0.4, cache: 0.02 }, B: { input: 1, output: 2, cache: 0.1 } } };
  const shared = { m, providers: ['A', 'B'], input: 10_000, requested: 1000, record: null, endpoints: {}, remainingSec: NaN, now_ts,
    stickySec: 600, errorSec: 1800 };
  assert.deepEqual(sessionOrder(shared).providers, ['A', 'B']);
  const error = { A: { last: 'error', last_ts: now_ts - 10, error: 1, success: 3 } };
  assert.deepEqual(sessionOrder({ ...shared, endpoints: error }).providers, ['B', 'A'], 'Put the endpoint with the latest failure last');
  assert.deepEqual(sessionOrder({ ...shared, endpoints: { A: { ...error.A, last_ts: now_ts - 1801 } } }).providers, ['A', 'B'], 'old error');
  const failed = { endpoint: 'B', order: 'B', success: false, character: 9000, ts: now_ts - 5 };
  assert.deepEqual(sessionOrder({ ...shared, record: failed }).providers, ['A', 'B'], 'A failed session endpoint is not sticky');
  const speed = { A: { speed_tps: 10 }, B: { speed_tps: 100 } };
  assert.deepEqual(sessionOrder({ ...shared, endpoints: speed, remainingSec: 50 }).providers, ['B', 'A'], 'A needs 100s with only 50s remaining');
  assert.deepEqual(sessionOrder({ ...shared, endpoints: speed, remainingSec: 500 }).providers, ['A', 'B'], 'Enough time: choose the cheap endpoint');
  assert.deepEqual(sessionOrder({ ...shared, endpoints: { B: { speed_tps: 100 } }, remainingSec: 50 }).providers, ['A', 'B'],
    'Unknown speed does not imply the deadline cannot be met');
  const sticky = { endpoint: 'A', order: 'A', success: true, character: 9000, ts: now_ts - 5 };
  const outcome = sessionOrder({ ...shared, providers: ['B', 'A'], record: sticky, endpoints: speed, remainingSec: 50 });
  assert.deepEqual(outcome.providers, ['B', 'A'], 'Do not retain a session endpoint that cannot finish in time');
  assert.match(outcome.reason.join(' '), /cannot finish/);
});

test('CR-48: the gate keeps OR filters, reorders only inside them, persists the choice across restart and concurrent calls', () => sticky(600, async () => {
  const bodies = [];
  const f = await fixture(orBudget, async (_url, options) => {
    bodies.push(JSON.parse(options.body));
    return sseResponse({ id: `gen-${bodies.length}`, provider: 'StreamLake', choices: [{ delta: { content: 'x' }, finish_reason: 'stop' }],
      usage: { prompt_tokens: 50, completion_tokens: 5, prompt_tokens_details: { cached_tokens: 40 }, cost: 0.00001 } });
  }, 'openrouter');
  two(f, tier(0.5, 1, 'StreamLake'), tier(0.9, 1, 'DeepInfra'));
  await (await (await f.call('impl', 'sticky', orModel)).invoke()).text();
  assert.deepEqual(bodies[0].provider.order, ['streamlake/fp8', 'deepinfra/fp8']);
  two(f, tier(1.0, 1, 'StreamLake'), tier(0.9, 1, 'DeepInfra'));
  await (await (await f.call('impl', 'sticky', orModel)).invoke()).text();
  await (await (await f.call('impl', 'other-session', orModel)).invoke()).text();
  assert.deepEqual(bodies[1].provider, { only: ['streamlake/fp8', 'deepinfra/fp8'], order: ['streamlake/fp8', 'deepinfra/fp8'],
    quantizations: ['fp8'], allow_fallbacks: false, require_parameters: true, max_price: { prompt: 1.74, completion: 3.84 } });
  assert.deepEqual(bodies[2].provider.order, ['deepinfra/fp8', 'streamlake/fp8'], 'a fresh session has no cache; use the cheaper endpoint');
  const lines = f.rows();
  assert.equal(lines[1].reserved, lines[2].reserved, 'Ordering does not change the reservation; use the highest allowed endpoint price');
  assert.match(meta(lines[1]).endpoint_selection.reason.join(' '), /preserved/);
  assert.deepEqual(meta(lines[1]).endpoint_selection.order, ['streamlake/fp8', 'deepinfra/fp8']);
  const { readFileSync } = await import('node:fs');
  const hint = JSON.parse(readFileSync(join(f.home, 'chain', 'or-session-endpoints.json'), 'utf8'));
  assert.equal(hint.version, 1);
  assert.equal(hint.sessions[JSON.stringify(['sticky', orModel])].endpoint, 'streamlake/fp8');
  delete globalThis[Symbol.for(`cadre.provider-gate:${f.home}`)];
  const again = await plugin();
  const config = { enabled_providers: [], provider: { openrouter: { options: { fetch: async (_u, o) => {
    bodies.push(JSON.parse(o.body));
    return sseResponse({ id: `gen-${bodies.length}`, provider: 'StreamLake', choices: [{ delta: {}, finish_reason: 'stop' }],
      usage: { prompt_tokens: 50, completion_tokens: 5, cost: 0.00001 } });
  } } } } };
  await again.config(config);
  const request = async (session) => {
    const input = { sessionID: session, agent: 'impl', model: { providerID: 'openrouter', id: orModel, api: { id: orModel },
      limit: { context: 100, output: 10 }, cost: { input: 1, output: 2 } } };
    const params = { maxOutputTokens: 10 };
    await again['chat.params'](input, params);
    const headers = { headers: {} };
    await again['chat.headers'](input, headers);
    return config.provider.openrouter.options.fetch('https://fixture.invalid/chat/completions', { method: 'POST', headers: headers.headers,
      body: JSON.stringify({ model: orModel, max_tokens: params.maxOutputTokens, messages: [{ role: 'user', content: 'plain task' },
        { role: 'assistant', content: 'x' }, { role: 'user', content: 'proceed' }] }) });
  };
  await (await request('sticky')).text();
  assert.deepEqual(bodies[3].provider.order, ['streamlake/fp8', 'deepinfra/fp8'], 'a restarted session reads its selection from disk');
  const binary = await Promise.all([request('sticky'), request('sticky')]);
  await Promise.all(binary.map((r) => r.text()));
  const last = f.rows();
  assert.equal(last.length, 6);
  assert.ok(last.every((r) => r.actual === 10), 'Two concurrent requests settle through separate reservations and receipts');
  JSON.parse(readFileSync(join(f.home, 'chain', 'or-session-endpoints.json'), 'utf8'));
}));

test('CR-50: the effort actually sent and its policy are recorded on the reservation; default effort leaves no policy', async () => {
  const catalog = join(temporary, 'effort-catalog.json');
  writeFileSync(catalog, JSON.stringify({ 'opencode-go': { models: { model: { reasoning_options: [{ type: 'effort', values: ['low', 'high'] }] } } } }));
  const previous = { catalog: process.env.CADRE_MODEL_CATALOG, role: process.env.CADRE_ROLE, effort: process.env.CADRE_EFFORT,
    policy: process.env.CADRE_EFFORT_POLICY };
  try {
    Object.assign(process.env, { CADRE_MODEL_CATALOG: catalog, CADRE_ROLE: 'impl', CADRE_EFFORT: 'low', CADRE_EFFORT_POLICY: 'economic-effort-v1' });
    const f = await fixture(go(10), async () => response());
    await (await (await f.call('build', 'effort-1')).invoke()).text();
    delete process.env.CADRE_EFFORT;
    delete process.env.CADRE_EFFORT_POLICY;
    await (await (await f.call('build', 'effort-2')).invoke()).text();
    const [economic, fallback] = f.rows().map((r) => meta(r).effort);
    assert.deepEqual(economic, { level: 'low', policy: 'economic-effort-v1' });
    assert.equal(fallback, undefined, 'without a policy, keep the provider default and invent no field');
  } finally {
    for (const [key, value] of [['CADRE_MODEL_CATALOG', previous.catalog], ['CADRE_ROLE', previous.role], ['CADRE_EFFORT', previous.effort],
                                ['CADRE_EFFORT_POLICY', previous.policy]]) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
  }
});

test('CR-48/52: budget refusal and a consumer cancel record no usage, no session success and make no extra call', async () => {
  const { existsSync, readFileSync } = await import('node:fs');
  let call = 0;
  const red = await fixture({ or: { db: 'ok', pay: 0, available: 0, remaining: 0 } }, async () => { call++; return response(); }, 'openrouter');
  two(red, tier(0.5, 1, 'StreamLake'), tier(0.9, 1, 'DeepInfra'));
  await assert.rejects((await red.call('impl', 'red-session', orModel)).invoke, /CADRE_BUDGET_DENIED/);
  assert.equal(call, 0);
  assert.equal(existsSync(join(red.home, 'chain', 'or-session-endpoints.json')), false, 'A denied request does not record session success');
  const part = new TextEncoder().encode('data: {"id":"gen-i","provider":"StreamLake","choices":[{"delta":{"content":"a"}}]}\n\n');
  const cancel = await fixture(orBudget, async () => new Response(new ReadableStream({ start(c) { c.enqueue(part); } }),
    { headers: { 'content-type': 'text/event-stream' } }), 'openrouter');
  two(cancel, tier(0.5, 1, 'StreamLake'), tier(0.9, 1, 'DeepInfra'));
  const response = await (await cancel.call('impl', 'cancel-session', orModel)).invoke();
  const reader = response.body.getReader();
  await reader.read();
  await reader.cancel();
  const [line] = cancel.rows();
  assert.equal(line.actual, null, 'cancellation keeps the liability pending');
  assert.equal(meta(line).usage, undefined);
  const file = join(cancel.home, 'chain', 'or-session-endpoints.json');
  const sessions = existsSync(file) ? JSON.parse(readFileSync(file, 'utf8')).sessions : {};
  assert.equal(sessions[JSON.stringify(['cancel-session', orModel])], undefined);
});

test('CR-49: reasoning carried by the gate keeps the tool-loop prefix stable across turns', async () => {
  const f = await fixture(orBudget, async (_url, options) => {
    const body = JSON.parse(options.body);
    if (body.messages.length === 1) {
      return sseResponse({ id: 'r1', choices: [{ delta: { reasoning_details: [{ type: 'reasoning.text', index: 0, text: 'plan' }],
        tool_calls: [{ id: 'call_a', function: { name: 'read' } }] } }] },
        { id: 'r1', choices: [{ delta: {}, finish_reason: 'tool_calls' }], usage: { cost: 0.00001 } });
    }
    return sseResponse({ id: `r${body.messages.length}`, choices: [{ delta: { content: 'ok' }, finish_reason: 'stop' }], usage: { cost: 0.00001 } });
  }, 'openrouter');
  orPrices(f);
  const user = { role: 'user', content: 'job' };
  const assistant = { role: 'assistant', content: '', tool_calls: [{ id: 'call_a', type: 'function' }] };
  const tool = { role: 'tool', tool_call_id: 'call_a', content: 'file' };
  const send = async (messages) => (await (await f.call('impl', 'tool-loop', orModel)).invoke('x', { messages })).text();
  await send([user]);
  await send([user, assistant, tool]);
  await send([user, assistant, tool, { role: 'assistant', content: 'intermediate' }, { role: 'user', content: 'proceed et' }]);
  const prefix = f.rows().map((r) => meta(r).prefix);
  assert.deepEqual(prefix.map((o) => o.proceed), [null, true, true], 'Plugin-restored reasoning_details stay identical across rounds');
});

test('CR-48: a 429 closes its reservation at zero and the next request in that session does not stick to the failed route', () => sticky(600, async () => {
  let n = 0;
  const f = await fixture(orBudget, async () => {
    n += 1;
    if (n === 2) return new Response('{"error":{"code":429}}', { status: 429, headers: { 'content-type': 'application/json' } });
    return sseResponse({ id: `gen-${n}`, provider: 'StreamLake', choices: [{ delta: { content: 'x' }, finish_reason: 'stop' }],
      usage: { prompt_tokens: 50, completion_tokens: 5, cost: 0.00001 } });
  }, 'openrouter');
  two(f, tier(0.5, 1, 'StreamLake'), tier(0.9, 1, 'DeepInfra'));
  await (await (await f.call('impl', 'limit', orModel)).invoke()).text();
  await (await (await f.call('impl', 'limit', orModel)).invoke()).text();
  two(f, tier(1.0, 1, 'StreamLake'), tier(0.9, 1, 'DeepInfra'));
  await (await (await f.call('impl', 'limit', orModel)).invoke()).text();
  const lines = f.rows();
  assert.equal(lines[1].actual, 0);
  assert.match(lines[1].receipt, /:http-429$/);
  assert.deepEqual(meta(lines[2]).endpoint_selection.order, ['deepinfra/fp8', 'streamlake/fp8'], 'No stickiness after a failed last request');
  assert.match(meta(lines[2]).endpoint_selection.reason.join(' '), /failed/);
}));

test('CR-48: an empty or missing deadline never marks routes as too slow; a real short deadline demotes the measured slow route', async () => {
  const bodies = [];
  const f = await fixture(orBudget, async (_url, options) => {
    bodies.push(JSON.parse(options.body));
    return sseResponse({ id: `gen-${bodies.length}`, choices: [{ delta: {}, finish_reason: 'stop' }], usage: { cost: 0.00001 } });
  }, 'openrouter');
  two(f, tier(0.5, 1, 'StreamLake'), tier(0.9, 1, 'DeepInfra'));
  const now_ts = Date.now() / 1000;
  writeFileSync(join(f.home, 'chain', 'or-session-endpoints.json'), JSON.stringify({ version: 1, sessions: {}, endpoints: {
    [JSON.stringify([orModel, 'streamlake/fp8'])]: { speed_tps: 1, last: 'success', last_ts: now_ts, ts: now_ts },
    [JSON.stringify([orModel, 'deepinfra/fp8'])]: { speed_tps: 100, last: 'success', last_ts: now_ts, ts: now_ts } } }));
  const previous = process.env.CADRE_DEADLINE_TS;
  try {
    for (const [value, expected] of [['', 'streamlake/fp8'], [undefined, 'streamlake/fp8'], ['absent', 'streamlake/fp8'],
                                     [String(now_ts + 3600), 'streamlake/fp8'], [String(now_ts + 5), 'deepinfra/fp8']]) {
      if (value === undefined) delete process.env.CADRE_DEADLINE_TS; else process.env.CADRE_DEADLINE_TS = value;
      await (await (await f.call('impl', `duration-${bodies.length}`, orModel)).invoke()).text();
      assert.equal(bodies.at(-1).provider.order[0], expected, String(value));
      const reason = meta(f.rows().at(-1)).endpoint_selection.reason.join(' ');
      assert.equal(/cannot finish/.test(reason), expected === 'deepinfra/fp8', `${value}: ${reason}`);
    }
  } finally {
    if (previous === undefined) delete process.env.CADRE_DEADLINE_TS; else process.env.CADRE_DEADLINE_TS = previous;
  }
});

test('CR-48/49: a broken prefix (compaction or changed instructions) gives the old route no cache credit', () => {
  const now_ts = 3_000_000;
  const m = { tiers: { A: { input: 1, output: 1, cache: 0.1 }, B: { input: 0.8, output: 1, cache: 0.08 } } };
  const record = { endpoint: 'A', order: 'A', success: true, character: 90_000, ts: now_ts - 30 };
  const shared = { m, providers: ['B', 'A'], input: 100_000, requested: 1000, record, endpoints: {}, remainingSec: NaN, now_ts, stickySec: 600, errorSec: 1800 };
  assert.deepEqual(sessionOrder({ ...shared, proceed: true }).providers, ['A', 'B']);
  const unknown = sessionOrder({ ...shared, proceed: null });
  assert.deepEqual(unknown.providers, ['B', 'A'], 'An unverified prefix gets no cache credit after restart or compaction');
  assert.match(unknown.reason.join(' '), /prefix unverified/);
  assert.equal(unknown.session.cache_credit, 0);
  assert.deepEqual(sessionOrder({ ...shared }).providers, ['B', 'A'], 'an absent continuation field is also unknown');
  const broken = sessionOrder({ ...shared, proceed: false });
  assert.deepEqual(broken.providers, ['B', 'A'], 'A broken prefix has no cache benefit; choose the cheap endpoint');
  assert.match(broken.reason.join(' '), /prefix broken/);
});

const endpointResponse = (bodies, providerName = true) => async (_url, options) => {
  const body = JSON.parse(options.body);
  bodies.push(body);
  const first = body.provider?.order?.[0];
  return sseResponse({ id: `gen-${bodies.length}`, ...(providerName ? { provider: first === 'deepinfra/fp8' ? 'DeepInfra' : 'StreamLake' } : {}),
    choices: [{ delta: { content: 'x' }, finish_reason: 'stop' }], usage: { prompt_tokens: 50, completion_tokens: 5, cost: 0.00001 } });
};

test('TSR-01: unmeasured session stickiness ships disabled; the gate keeps the health/price order and every OR limit', async () => {
  assert.equal(LIMITS.openrouter.session_sticky_sec, 0, 'The unmeasured 600s session pinning fallback stays disabled');
  assert.equal(LIMITS.openrouter.endpoint_error_sec, 1800, 'Demoting the latest failure is health handling and remains enabled');
  const bodies = [];
  const f = await fixture(orBudget, endpointResponse(bodies), 'openrouter');
  two(f, tier(0.5, 1, 'StreamLake'), tier(0.9, 1, 'DeepInfra'));
  await (await (await f.call('impl', 'fallback', orModel)).invoke()).text();
  two(f, tier(1.0, 1, 'StreamLake'), tier(0.9, 1, 'DeepInfra'));
  await (await (await f.call('impl', 'fallback', orModel)).invoke()).text();
  const lines = f.rows();
  assert.equal(meta(lines[1]).prefix.proceed, true, 'Pinning stays disabled even with a verified prefix');
  assert.deepEqual(bodies[1].provider, { only: ['deepinfra/fp8', 'streamlake/fp8'], order: ['deepinfra/fp8', 'streamlake/fp8'],
    quantizations: ['fp8'], allow_fallbacks: false, require_parameters: true, max_price: { prompt: 1.74, completion: 3.84 } });
  assert.doesNotMatch(meta(lines[1]).endpoint_selection.reason.join(' '), /preserved/);
  assert.equal(meta(lines[1]).endpoint_selection.session, undefined, 'Do not record a session estimate when the policy is disabled');
});

test('TSR-01: stickiness never lifts an endpoint below the uptime threshold over a healthy one, at any price', async () => {
  const now_ts = 4_000_000;
  const m = { tiers: { A: { input: 1, output: 1, cache: 0.1, uptime: 90 }, B: { input: 0.8, output: 1, cache: 0.08, uptime: 99.9 } } };
  const record = { endpoint: 'A', order: 'A', success: true, character: 90_000, ts: now_ts - 30 };
  const shared = { m, providers: ['B', 'A'], input: 100_000, requested: 1000, record, endpoints: {}, remainingSec: NaN, now_ts,
    stickySec: 600, errorSec: 1800, proceed: true, lowUptime: 95 };
  const outcome = sessionOrder(shared);
  assert.deepEqual(outcome.providers, ['B', 'A'], 'cache credit cannot override health');
  assert.match(outcome.reason.join(' '), /uptime 90 < 95/);
  const healthy = { tiers: { ...m.tiers, A: { ...m.tiers.A, uptime: 99.9 } } };
  assert.deepEqual(sessionOrder({ ...shared, m: healthy }).providers, ['A', 'B'], 'In the same health class, retain a verified endpoint when staying is cheaper');
  const bothLow = { tiers: { ...m.tiers, B: { ...m.tiers.B, uptime: 80 } } };
  assert.deepEqual(sessionOrder({ ...shared, m: bothLow }).providers, ['A', 'B'], 'The stay comparison is valid within the same low health class');
  const otherLow = { tiers: { A: { ...m.tiers.A, uptime: 99.9 }, B: { ...m.tiers.B, uptime: 80 } } };
  assert.deepEqual(sessionOrder({ ...shared, providers: ['A', 'B'], m: otherLow, proceed: null }).providers, ['A', 'B'],
    'The healthier session endpoint is already first in the base order');

  await sticky(600, async () => {
    const bodies = [];
    const f = await fixture(orBudget, endpointResponse(bodies), 'openrouter');
    two(f, tier(0.5, 1, 'StreamLake'), tier(0.8, 1, 'DeepInfra'));
    await (await (await f.call('impl', 'health', orModel)).invoke()).text();
    two(f, { ...tier(0.5, 1, 'StreamLake'), uptime: 90 }, tier(0.8, 1, 'DeepInfra'));
    await (await (await f.call('impl', 'health', orModel)).invoke()).text();
    assert.deepEqual(bodies[0].provider.order, ['streamlake/fp8', 'deepinfra/fp8']);
    assert.deepEqual(bodies[1].provider.order, ['deepinfra/fp8', 'streamlake/fp8'], 'Catalog uptime 90 percent below 95 percent: healthy endpoint first');
    const [first, second] = f.rows();
    assert.equal(meta(second).prefix.proceed, true);
    assert.match(meta(second).endpoint_selection.reason.join(' '), /not retained: uptime/);
    assert.equal(second.reserved, first.reserved, 'Reserve at the highest allowed price; changing order does not change the amount');
  });
});

test('TSR-01: after a restart an unverified prefix earns no cache credit, so the now cheaper route goes first', () => sticky(600, async () => {
  const bodies = [];
  const f = await fixture(orBudget, endpointResponse(bodies), 'openrouter');
  two(f, tier(0.5, 1, 'StreamLake'), tier(0.8, 1, 'DeepInfra'));
  await (await (await f.call('impl', 'again', orModel)).invoke('x'.repeat(2000))).text();
  two(f, tier(1.0, 1, 'StreamLake'), tier(0.8, 1, 'DeepInfra'));
  delete globalThis[Symbol.for(`cadre.provider-gate:${f.home}`)];
  const again = await (await import('../lib/budget-plugin.mjs?tsr01-again-starting')).default();
  const config = { enabled_providers: [], provider: { openrouter: { options: { fetch: endpointResponse(bodies) } } } };
  await again.config(config);
  const input = { sessionID: 'again', agent: 'impl', model: { providerID: 'openrouter', id: orModel, api: { id: orModel },
    limit: { context: 100, output: 10 }, cost: { input: 1, output: 2 } } };
  const params = { maxOutputTokens: 10 };
  await again['chat.params'](input, params);
  const headers = { headers: {} };
  await again['chat.headers'](input, headers);
  await (await config.provider.openrouter.options.fetch('https://fixture.invalid/chat/completions', { method: 'POST', headers: headers.headers,
    body: JSON.stringify({ model: orModel, max_tokens: params.maxOutputTokens, messages: [{ role: 'user', content: 'z'.repeat(2000) }] }) })).text();
  const second = meta(f.rows()[1]);
  assert.equal(second.prefix.proceed, null, 'the prefix is unknown after a restart');
  assert.deepEqual(bodies[1].provider.order, ['deepinfra/fp8', 'streamlake/fp8'], 'No credit for earlier characters; cheap endpoint first');
  assert.match(second.endpoint_selection.reason.join(' '), /prefix unverified/);
  assert.deepEqual(second.endpoint_selection.session, { endpoint: 'streamlake/fp8', endpoint_source: 'response', prefix: null, cache_credit: 0 });
}));

test('TSR-01: when the response names no endpoint the first preference is not treated as the cache location', () => sticky(600, async () => {
  const bodies = [];
  const f = await fixture(orBudget, endpointResponse(bodies, false), 'openrouter');
  two(f, tier(0.5, 1, 'StreamLake'), tier(0.9, 1, 'DeepInfra'));
  await (await (await f.call('impl', 'uncertain-endpoint', orModel)).invoke()).text();
  const { readFileSync } = await import('node:fs');
  const record = JSON.parse(readFileSync(join(f.home, 'chain', 'or-session-endpoints.json'), 'utf8')).sessions[JSON.stringify(['uncertain-endpoint', orModel])];
  assert.equal(record.endpoint, null, 'Do not report the submitted first preference as the actual endpoint');
  assert.equal(record.order, 'streamlake/fp8');
  two(f, tier(1.0, 1, 'StreamLake'), tier(0.9, 1, 'DeepInfra'));
  await (await (await f.call('impl', 'uncertain-endpoint', orModel)).invoke()).text();
  const second = meta(f.rows()[1]);
  assert.equal(second.prefix.proceed, true);
  assert.deepEqual(bodies[1].provider.order, ['deepinfra/fp8', 'streamlake/fp8']);
  assert.match(second.endpoint_selection.reason.join(' '), /actual endpoint unknown/);
  assert.deepEqual(second.endpoint_selection.session, { endpoint: 'streamlake/fp8', endpoint_source: 'first_preference', prefix: true, cache_credit: 0 });
}));
