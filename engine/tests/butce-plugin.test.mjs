import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { mkdtempSync, mkdirSync, writeFileSync, cpSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import plugin, { oturumSirasi } from '../lib/butce-plugin.mjs';
import { SINIRLAR } from '../lib/sinirlar.mjs';

const library = fileURLToPath(new URL('../lib/', import.meta.url));
process.env.CADRE_PYTHON ||= 'python3';
const temporary = mkdtempSync(join(tmpdir(), 'qe-plugin-'));
after(() => rmSync(temporary, { recursive: true, force: true }));
let sequence = 0;

async function fixture(budget, fetcher, providerID = 'opencode-go') {
  const home = join(temporary, String(++sequence));
  const code = join(home, 'code');
  mkdirSync(join(code, 'lib'), { recursive: true });
  for (const name of ['kosu.py', 'proje.py', 'qa_kaniti.py', 'izin.py', 'runtime_sync.py', 'aylik_butce.py', 'butce_kapisi.py', 'yonlendirici.py', 'opencode_adaptor.py', 'go_pencere.py', 'model_secimi.py', 'katalog.py', 'seviye.py', 'onelek.py', 'sinirlar.py', 'sinirlar.json']) cpSync(join(library, name), join(code, 'lib', name));
  // Fresh fake observation. No real budget reader, credentials, model, or network.
  writeFileSync(join(code, 'lib', 'butce.py'), `import json,datetime\nb=${JSON.stringify(JSON.stringify(budget))}\nd=json.loads(b)\nif isinstance(d,dict): d.update(ts=datetime.datetime.now().isoformat(),bugun=datetime.date.today().isoformat())\nprint(json.dumps(d))\n`);
  // Each scenario chooses its own mode; the user's shell setting is not the test's default.
  Object.assign(process.env, { CADRE_HOME: home, CADRE_ENGINE_ROOT: code, CADRE_YON_DURUM: join(home, 'zincir', 'saglayici-durum.json'),
    OPENCODE_EXPERIMENTAL_NATIVE_LLM: 'false', CADRE_BUTCE_GOZLEM: '0' });
  process.env.CADRE_RUN_ID = execFileSync(process.env.CADRE_PYTHON, ['-c',
    "import sys;sys.path.insert(0,sys.argv[1]);from kosu import RunStore;print(RunStore().accept('fixture','plain task')['run_id'])",
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
      join(home, 'zincir', 'request-budget.db')], { encoding: 'utf8' }));
  }
  return { hooks, config, call, rows, home };
}

// 4c-13: the Go observation is in points; its dollar equivalent uses the limit of the $15 model (1 point = $0.15)
const go = (remaining = 1) => ({ go: { db: 'ok', birim: 'puan', pay: remaining * 100 / 15, kullanilabilir: remaining * 100 / 15, pencere_acik: true } });
const response = () => new Response(JSON.stringify({ id: 'receipt', usage: { prompt_tokens: 1, completion_tokens: 1 } }),
  { headers: { 'content-type': 'application/json' } });

const orModel = 'deepseek/deepseek-v4-pro';
function orPrices(f, changes = {}) {
  mkdirSync(join(f.home, 'zincir'), { recursive: true });
  writeFileSync(join(f.home, 'zincir', 'openrouter-fiyat.json'), JSON.stringify({
    version: 2, ts: Date.now() / 1000, modeller: {
      [orModel]: { input: 1, output: 2, providers: ['deepinfra/fp8'], fp8_cache: true }
    }, ...changes
  }));
}
const orBudget = { or: { db: 'ok', pay: 10, kullanilabilir: 10, kalan: 10 } };

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
    globalThis[Symbol.for(`cadre.saglayici-kapisi:${f.home}`)] = true;
    await assert.rejects(plugin(), /existing provider has no matching project sandbox/);
    delete process.env.CADRE_TOOL_POLICY;
    delete globalThis[Symbol.for(`cadre.saglayici-kapisi:${f.home}`)];
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
    const endpoint = (input) => ({ input, output: 2, params: ['max_tokens'], max_cikti: 32768, kuantizasyon: 'fp8' });
    const prices = (providers) => orPrices(f, { modeller: { [orModel]: {
      input: 1, output: 2, providers, fp8_cache: true,
      kademeler: { 'streamlake/fp8': endpoint(0.5), 'deepinfra/fp8': endpoint(1) } } } });
    prices(['streamlake/fp8']);
    const { invoke } = await f.call('impl', `endpoint-${wire}`, orModel);
    await (await invoke()).text();
    const failed = f.rows()[0];
    assert.equal(failed.actual, null, 'an outage is not a zero-cost receipt');
    assert.equal(JSON.parse(failed.metadata).saglayici_hatasi.tur, 'provider_unavailable');
    prices(['streamlake/fp8', 'deepinfra/fp8']);
    await (await invoke()).text();
    assert.equal(calls, 2);
    const rows = f.rows();
    assert.equal(rows.length, 2);
    assert.equal(rows[0].actual, null);
    assert.equal(rows[0].reserved, failed.reserved);
    assert.equal(rows[1].actual, 10);
    assert.deepEqual(JSON.parse(rows[1].metadata).uc_secimi.sira, ['deepinfra/fp8']);
    execFileSync(process.env.CADRE_PYTHON, ['-c',
      "import os,sys;sys.path.insert(0,sys.argv[1]);from kosu import RunStore;RunStore().update(os.environ['CADRE_RUN_ID'],budget_cap_usd=0)",
      join(f.home, 'code', 'lib')]);
    await assert.rejects(invoke(), /CADRE_BUDGET_DENIED/);
    assert.equal(calls, 2, 'endpoint selection cannot bypass the run cap');
    assert.equal(f.rows().length, 2, 'a denied attempt makes no new reservation');
  }
});

test('4c-7: an endpoint that cannot carry the requested output is dropped, order by live price, low-uptime endpoints last', async () => {
  const govdeler = [];
  const f = await fixture(orBudget, async (_url, options) => {
    govdeler.push(JSON.parse(options.body));
    return new Response(JSON.stringify({ id: 'actual', usage: { cost: 0.00001 } }));
  }, 'openrouter');
  const uc = (input, output, max_cikti, uptime = 99.9) => ({ input, output, params: ['max_tokens'], max_cikti, uptime, kuantizasyon: 'fp8' });
  orPrices(f, { modeller: { [orModel]: { input: 1.3, output: 2.6, providers: ['deepinfra/fp8', 'streamlake/fp8'], fp8_cache: true,
    kademeler: { 'deepinfra/fp8': uc(1.3, 2.6, 8), 'streamlake/fp8': uc(0.2, 0.4, 384000) } },
    'z-ai/glm-5.3': { input: 1.4, output: 4.4, providers: ['novita/fp8', 'sail-research/fp8', 'z-ai/fp8'], fp8_cache: true,
      kademeler: { 'novita/fp8': uc(0.42, 1.32, 131072, 90), 'sail-research/fp8': uc(0.2, 3.4, 943718), 'z-ai/fp8': uc(1.4, 4.4, 131072) } } } });
  await (await (await f.call('impl', 'ds', orModel)).invoke()).text();
  assert.deepEqual(govdeler[0].provider.only, ['streamlake/fp8'], 'DeepInfra 8 tokenluk ust siniriyla 10 tokenlik istegi tasiyamaz');
  assert.equal(f.rows()[0].reserved, Math.ceil(100 * 0.2 + 10 * 0.4), 'rezervasyon yalniz kalan uclarin en pahalisindan');
  await (await (await f.call('impl', 'glm', 'z-ai/glm-5.3')).invoke()).text();
  assert.deepEqual(govdeler[1].provider.order, ['sail-research/fp8', 'z-ai/fp8', 'novita/fp8'], 'ucuz once; Novita %90 acik kalmayla geride');
  assert.deepEqual(govdeler[1].provider.max_price, { prompt: 1.4, completion: 4.4 });
  assert.deepEqual(govdeler[1].provider.quantizations, ['fp8']);
});

test('16(4): with budget-type thinking the gate measures max_tokens − budget and does not deny; the same request without thinking is denied', async () => {
  let cagri = 0;
  const f = await fixture(go(1), async () => { cagri++; return new Response(JSON.stringify({ type: 'message', id: 'm',
    usage: { input_tokens: 1, output_tokens: 1 } }), { headers: { 'content-type': 'application/json' } }); });
  const model = 'anthropic-bicimli';
  const input = { sessionID: 's-butce', agent: 'impl', model: { providerID: 'opencode-go', id: model,
    api: { id: model, npm: '@ai-sdk/anthropic' }, limit: { context: 100000, output: 10 }, cost: { input: 1, output: 2 } } };
  await f.hooks['chat.params'](input, { maxOutputTokens: 10 });
  const istek = async (ek) => {
    const headers = { headers: {} };
    await f.hooks['chat.headers'](input, headers);
    return (await f.config.provider['opencode-go'].options.fetch('https://fixture.invalid/messages', { method: 'POST',
      headers: headers.headers, body: JSON.stringify({ model, max_tokens: 20, messages: [{ role: 'user', content: 'x' }], ...ek }) })).text();
  };
  await istek({ thinking: { type: 'enabled', budget_tokens: 15 } });   // gorunur 20 − 15 = 5 ≤ 10
  assert.equal(cagri, 1);
  await assert.rejects(istek({}), /CADRE_BUDGET_DENIED/);              // gorunur 20 > 10
  assert.equal(cagri, 1);
});

test('4c QA2: an output request that fits no endpoint is not dropped but clipped to the largest endpoint cap', async () => {
  const govdeler = [];
  const f = await fixture(orBudget, async (_url, options) => {
    govdeler.push(JSON.parse(options.body));
    return new Response(JSON.stringify({ id: 'actual', usage: { cost: 0.00001 } }));
  }, 'openrouter');
  const uc = (max_cikti) => ({ input: 1, output: 2, params: ['max_tokens'], max_cikti, uptime: 99.9, kuantizasyon: 'fp8' });
  orPrices(f, { modeller: { [orModel]: { input: 1, output: 2, providers: ['deepinfra/fp8', 'streamlake/fp8'], fp8_cache: true,
    kademeler: { 'deepinfra/fp8': uc(6), 'streamlake/fp8': uc(8) } } } });
  await (await (await f.call('impl', 'kirp', orModel)).invoke()).text();   // 10 tokens requested
  assert.equal(govdeler[0].max_tokens, 8);
  assert.deepEqual(govdeler[0].provider.only, ['streamlake/fp8']);
});

test('4c-7: OR is denied if the rule-allowed endpoint is not fp8 or the last good price is older than 6 hours', async () => {
  let istek = 0;
  const f = await fixture(orBudget, async () => { istek++; return new Response(JSON.stringify({ id: 'x', usage: { cost: 0.00001 } })); }, 'openrouter');
  const uc = (kuantizasyon) => ({ input: 1, output: 2, params: ['max_tokens'], max_cikti: 1000, kuantizasyon });
  orPrices(f, { modeller: { 'z-ai/glm-5.3': { input: 1, output: 2, providers: ['A', 'B'], fp8_cache: true, kademeler: { A: uc('fp8'), B: uc('fp4') } } } });
  await assert.rejects(async () => (await (await f.call('impl', 'g1', 'z-ai/glm-5.3')).invoke()).text(), /CADRE_BUDGET_DENIED/);
  orPrices(f, { modeller: { [orModel]: { input: 1, output: 2, providers: ['deepinfra/fp8'], fp8_cache: true, fiyat_ts: Date.now() / 1000 - 7 * 3600 } } });
  await assert.rejects(async () => (await (await f.call('impl', 'g2', orModel)).invoke()).text(), /CADRE_BUDGET_DENIED/);
  orPrices(f, { modeller: { [orModel]: { input: 1, output: 2, providers: ['deepinfra/fp8'], fp8_cache: true, fiyat_ts: Date.now() / 1000 - 5 * 3600 } } });
  await (await (await f.call('impl', 'g3', orModel)).invoke()).text();
  assert.equal(istek, 1, 'yas siniri icindeki son iyi fiyat kullanilir');
});

test('Sol is reserved only at the price of the allowed standard endpoint; off-policy tiers and incompatible parameters are denied before the network', async () => {
  const gpt = 'openai/gpt-6.1-sol';
  const govdeler = [];
  const f = await fixture(orBudget, async (_url, options) => {
    govdeler.push(JSON.parse(options.body));
    return new Response(JSON.stringify({ id: 'actual', usage: { cost: 0.00004 } }));
  }, 'openrouter');
  orPrices(f, { modeller: { [gpt]: { input: 2, output: 10, providers: ['openai/flex', 'openai', 'azure'], fp8_cache: true, kapali: true,
    max_price: { prompt: 2, completion: 10 }, kademeler: {
      'openai/flex': { input: 1, output: 5, params: ['max_tokens'] }, openai: { input: 2, output: 10, params: ['max_tokens'] },
      azure: { input: 2, output: 10, params: ['max_completion_tokens'] } } } } });
  await assert.rejects(f.call('plan', 'old-prices', gpt), /CADRE_BUDGET_DENIED/);
  assert.equal(govdeler.length, 0, 'politika disi FLEX/Azure kademeleri iceren fiyat listesiyle cagrilmaz');
  orPrices(f, { modeller: { [gpt]: { input: 2, output: 10, providers: ['openai'], fp8_cache: true, kapali: true,
    max_price: { prompt: 2, completion: 10 }, kademeler: {
      openai: { input: 2, output: 10, params: ['max_tokens'] } } } } });
  const call = await f.call('plan', 'gpt-session', gpt);
  await (await call.invoke('plan')).text();
  assert.deepEqual(govdeler[0].provider, { only: ['openai'], order: ['openai'], allow_fallbacks: false,
    max_price: { prompt: 2, completion: 10 } }, 'yalniz izinli standart uc; kapali agirlikta fp8 suzgeci yok');
  assert.equal(govdeler[0].temperature, undefined);
  assert.equal(f.rows()[0].actual, 40);
  const rez = f.rows()[0].reserved;
  const incompatible = await f.call('plan', 'gpt-session-2', gpt);
  await assert.rejects(incompatible.invoke('plan', { max_tokens: undefined, max_completion_tokens: 10 }), /CADRE_BUDGET_DENIED/);
  assert.equal(govdeler.length, 1, 'ucun desteklemedigi parametre baska uca kacmaz');
  assert.equal(rez, 100 * 2 + 10 * 10, 'rezervasyon yalniz izinli standart ucun ust fiyatindandir');
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
    { version: 1 }, { modeller: { [orModel]: { input: 1, output: 2, providers: ['Relace'], fp8_cache: true } } }]) {
    orPrices(f, changes);
    await assert.rejects(() => f.call('impl', 's', orModel), /CADRE_BUDGET_DENIED/);
  }
  orPrices(f);
  const call = await f.call('impl', 's', orModel);
  orPrices(f, { ts: Date.now() / 1000 - 3601 });
  process.env.CADRE_BUTCE_GOZLEM = '1';
  try { await assert.rejects(call.invoke, /CADRE_BUDGET_DENIED/); }
  finally { process.env.CADRE_BUTCE_GOZLEM = '0'; }
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
  const model = 'buyuk-baglam';
  const input = { sessionID: 's9', agent: 'impl', model: { providerID: 'opencode-go', id: model, api: { id: model },
    limit: { context: 1000000, output: 10 }, cost: { input: 1, output: 2 } } };
  const params = { maxOutputTokens: 10 };
  await f.hooks['chat.params'](input, params);
  const headers = { headers: {} };
  await f.hooks['chat.headers'](input, headers);
  const body = JSON.stringify({ model, max_tokens: params.maxOutputTokens, messages: [{ role: 'user', content: 'kisa istek' }] });
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
  const model = 'bedava-model';
  const input = { sessionID: 's-free', agent: 'impl', model: { providerID: 'opencode-go', id: model, api: { id: model },
    limit: { context: 128000, output: 10 }, cost: { input: 0, output: 0 } } };
  const params = { maxOutputTokens: 10 };
  await f.hooks['chat.params'](input, params);
  const headers = { headers: {} };
  await f.hooks['chat.headers'](input, headers);
  const body = JSON.stringify({ model, max_tokens: params.maxOutputTokens, messages: [{ role: 'user', content: 'kisa istek' }] });
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
  const onceki = process.env.CADRE_ROL;
  process.env.CADRE_ROL = 'kalite';
  try {
    const model = 'dar-cikti-bedava';
    const input = { sessionID: 's-etkin', agent: 'impl', model: { providerID: 'opencode-go', id: model, api: { id: model },
      limit: { context: 200000, output: 32000 }, cost: { input: 0, output: 0 } } };
    const params = {};
    await f.hooks['chat.params'](input, params);
    assert.equal(params.maxOutputTokens, 32000);
    const headers = { headers: {} };
    await f.hooks['chat.headers'](input, headers);
    for (const limit of [undefined, { context: 200000 }, { context: 200000, output: 0 }]) {
      const belirsiz = { ...input, model: { ...input.model, id: `bilinmeyen-${JSON.stringify(limit ?? null)}`, limit } };
      await assert.rejects(() => f.hooks['chat.params'](belirsiz, {}), /CADRE_BUDGET_DENIED/);
      await assert.rejects(() => f.hooks['chat.headers'](belirsiz, { headers: {} }), /CADRE_BUDGET_DENIED/);
    }
  } finally {
    if (onceki === undefined) delete process.env.CADRE_ROL; else process.env.CADRE_ROL = onceki;
  }
});

test('a model with unknown or partial pricing is treated as paid, never as free (0b)', async () => {
  const f = await fixture(go(1), async () => response());
  for (const cost of [undefined, { input: 0 }, { output: 0 }]) {
    const model = `belirsiz-${JSON.stringify(cost ?? null)}`;
    const input = { sessionID: 's-unknown', agent: 'impl', model: { providerID: 'opencode-go', id: model, api: { id: model },
      limit: { context: 1000, output: 10 }, cost } };
    await assert.rejects(() => f.hooks['chat.params'](input, { maxOutputTokens: 10 }), /CADRE_BUDGET_DENIED/);
  }
  // No reservation was opened: the ledger file was never created (so f.rows() is not called).
});

const zen = (remaining = 1) => ({ zen: { db: 'ok', pay: remaining, kullanilabilir: remaining } });

test('zen calls pass through the same gate as every other paid provider (0c)', async () => {
  // before 0c: 'opencode' was not in the supported provider list, so every zen call was denied in chat.params.
  let istek = 0;
  const f = await fixture(zen(1), async () => { istek++; return response(); }, 'opencode');
  const call = await f.call('impl', 'zen-oturum', 'zen-model');
  await (await call.invoke()).text();
  assert.equal(istek, 1);
  const row = f.rows()[0];
  assert.equal(row.provider, 'opencode');
  assert.ok(row.reserved > 0);
  assert.equal(row.actual, 1 * 1 + 1 * 2);
});

test('zen without a readable balance denies before the network (0c)', async () => {
  let istek = 0;
  const f = await fixture({ zen: { db: 'yok', pay: 0, kullanilabilir: 0 } }, async () => { istek++; return response(); }, 'opencode');
  const call = await f.call('impl', 'zen-oturum', 'zen-model');
  await assert.rejects(call.invoke, /CADRE_BUDGET_DENIED/);
  assert.equal(istek, 0);
});

test('a 429 from Go stamps the window marker that the budget reader consumes (item 5)', async () => {
  // The stamp file was read until now but NEVER written: the "we hit the limit" signal was dead code.
  const { existsSync, readFileSync } = await import('node:fs');
  const f = await fixture(go(1), async () => new Response('{"error":"rate limit"}', { status: 429, headers: { 'content-type': 'application/json' } }));
  const damga = join(f.home, 'zincir', 'go-429-son.txt');
  assert.equal(existsSync(damga), false);
  const call = await f.call();
  const res = await call.invoke();
  assert.equal(res.status, 429);
  await res.text();
  assert.equal(existsSync(damga), true);
  const saniye = Number(readFileSync(damga, 'utf8'));
  assert.ok(Math.abs(saniye - Math.floor(Date.now() / 1000)) < 60, 'damga su ana yakin olmali');
});

test('cached input is settled at the cache rate, not the most expensive input rate (item 6)', async () => {
  // Measured defect: the receipt closed ALL input at the most expensive rate. Cache reads cost 1/5-1/100 of the input
  // price, so at a 90% hit rate the ledger recorded 8.5 times reality.
  const cevap = (usage) => new Response(JSON.stringify({ id: 'receipt', usage }),
    { headers: { 'content-type': 'application/json' } });
  const f = await fixture(go(1), async () => cevap({ prompt_tokens: 1000, completion_tokens: 10,
    prompt_tokens_details: { cached_tokens: 900 } }));
  const model = 'onbellekli';
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
  assert.ok(row.reserved > row.actual, 'rezervasyon emniyetli kalmali');
});

test('cached input with no catalogue cache price is settled at the input rate (B230: unknown discount is no discount)', async () => {
  // 25.09 panel audit: Cerebras charged 4.1M cache reads at the full input price ($4.06); the catalogue had no cache price
  // so readRate counted as 0, and the ledger and the daily share recorded $4.07 too little (credit 10.18 vs panel 6.11).
  const cevap = (usage) => new Response(JSON.stringify({ id: 'receipt', usage }),
    { headers: { 'content-type': 'application/json' } });
  const f = await fixture(go(1), async () => cevap({ prompt_tokens: 1000, completion_tokens: 10,
    prompt_tokens_details: { cached_tokens: 900 } }));
  const model = 'onbelleksiz-katalog';
  const input = { sessionID: 's-cache-yok', agent: 'impl', model: { providerID: 'opencode-go', id: model, api: { id: model },
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
  const cevap = (usage) => new Response(JSON.stringify({ id: 'receipt', usage }),
    { headers: { 'content-type': 'application/json' } });
  const f = await fixture(go(1), async () => cevap({ prompt_tokens: 1000, completion_tokens: 10,
    cache_creation_input_tokens: 400, cache_read_input_tokens: 100 }));
  const model = 'yazma';
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
const BEDAVA = { zen: { db: 'ok', pay: 0, kullanilabilir: 0, kalan: 0 } };
const SIFIR = { input: 0, output: 0 };

test('a free request is always counted, even when the stream ends badly (live finding 17.09)', async () => {
  // 11 of 31 free Gemini calls were left pending: when the stream looked broken the receipt was never written.
  // What the free bucket measures is the REQUEST COUNT; if not counted, the daily cap fills with pending reservations.
  const bozuk = () => new Response('{"bu json degil', { headers: { 'content-type': 'application/json' } });
  const f = await fixture(BEDAVA, async () => bozuk(), 'opencode');
  const call = await f.call('impl', 'ses-bedava', 'mimo-v2.6-flash-free', SIFIR);
  await (await call.invoke()).text();
  const rows = f.rows();
  assert.equal(rows.length, 1);
  assert.equal(rows[0].provider, 'opencode:free');   // 4c-11: Gemini was removed; the counted bucket is a zero-priced model
  assert.equal(rows[0].reserved, 1);
  assert.equal(rows[0].actual, 1, 'bozuk akista bile istek sayilmali');
});

test('a free request is counted when the transport itself throws (B83, 17.09 second audit)', async () => {
  // Two sibling branches were closed on 17.09 (broken stream, HTTP error) but the THIRD is open: `await previous(...)`
  // had no try/catch, so a network drop / timeout / `redirect:'error'` / `cadre iptal` left the reservation pending without
  // being counted. The pending total has no period filter, so every event was PERMANENTLY subtracted from the daily free
  // bucket: the provider slowly closes and the only fix is a manual `cadre kurtar --bayat-kapat`.
  const f = await fixture(BEDAVA,
    async () => { throw new TypeError('fetch failed'); }, 'opencode');
  const call = await f.call('impl', 'ses-kopuk', 'mimo-v2.6-flash-free', SIFIR);
  await assert.rejects(call.invoke(), /fetch failed/);
  const rows = f.rows();
  assert.equal(rows.length, 1);
  assert.equal(rows[0].provider, 'opencode:free');   // 4c-11: Gemini was removed; the counted bucket is a zero-priced model
  assert.equal(rows[0].actual, 1, 'ag hatasinda da istek sayilmali; askida kalirsa kova kalici sizar');
});

test('a free request rejected by the provider is still counted (audit 17.09)', async () => {
  // The previous test closed the 200 OK + broken body branch; the LIVE defect was in the HTTP ERROR branch:
  // in run 20260917-121533 Gemini returned 429 ("You exceeded your current quota") 96 times, receiptStream
  // returned without a receipt on `!response.ok` and 96 of 96 reservations were left pending. A pending reservation
  // has no period filter -> it is PERMANENTLY subtracted from the daily request bucket (110 pending `google` lines in the ledger,
  // daily cap 450). A denied request is still a request made: it must be counted.
  const f = await fixture(BEDAVA,
    async () => new Response('{"error":{"code":429,"message":"You exceeded your current quota"}}',
      { status: 429, headers: { 'content-type': 'application/json' } }), 'opencode');
  const call = await f.call('compaction', 'ses-429', 'mimo-v2.6-flash-free', SIFIR);
  const res = await call.invoke();
  assert.equal(res.status, 429);
  await res.text();
  const rows = f.rows();
  assert.equal(rows.length, 1);
  assert.equal(rows[0].provider, 'opencode:free');   // 4c-11: Gemini was removed; the counted bucket is a zero-priced model
  assert.equal(rows[0].reserved, 1);
  assert.equal(rows[0].actual, 1, '429 da bir istektir; askida kalirsa bedava kova kalici olarak kapanir');
});

test('a paid request rejected by the provider still keeps its reservation (money risk)', async () => {
  // The free bucket's fix must not leak into the PAID bucket: there an unknown amount KEEPS the reservation. 429 is the exception (4c-4).
  const f = await fixture(go(1), async () => new Response('{"error":"upstream"}',
    { status: 500, headers: { 'content-type': 'application/json' } }));
  const call = await f.call();
  await (await call.invoke()).text();
  assert.equal(f.rows()[0].actual, null, 'parali cagrida red sonrasi tutar bilinmiyor: rezervasyon durur');
  const g = await fixture(go(1), async () => new Response('{"error":"rate limit"}',
    { status: 429, headers: { 'content-type': 'application/json' } }));
  await (await (await g.call()).invoke()).text();
  assert.deepEqual([g.rows()[0].actual, g.rows()[0].receipt.endsWith(':http-429')], [0, true], '429 islenmedi: yukumluluk birakilmaz');
});

test('a paid request with an unreadable stream keeps its reservation (money risk)', async () => {
  const bozuk = () => new Response('{"bu json degil', { headers: { 'content-type': 'application/json' } });
  const f = await fixture(go(1), async () => bozuk());
  const call = await f.call();
  await (await call.invoke()).text();
  const row = f.rows()[0];
  assert.equal(row.actual, null, 'parali cagrida bilinmeyen tutar rezervasyonu KORUR');
});

test('a free request with a corrupt SSE stream is counted (B110-1)', async () => {
  // `if (invalid) return` on the FIRST line of `flush()` swallowed the free receipt: on a broken SSE `item()` already
  // set invalid=true during transform and flush returned without writing anything. The measured unit is the REQUEST.
  const f = await fixture(BEDAVA,
    async () => new Response('data: {"choices":[{"delta"\n\ndata: {"id":"x"\n\n',
      { headers: { 'content-type': 'text/event-stream' } }), 'opencode');
  const call = await f.call('impl', 'ses-sse-bozuk', 'mimo-v2.6-flash-free', SIFIR);
  await (await call.invoke()).text();
  assert.equal(f.rows()[0].actual, 1, 'bozuk SSE akisinda da istek sayilmali');
});

test('a free request larger than the 4MB stream guard is counted (B110-1)', async () => {
  // a >4MB body sets `invalid=true`; the old flush also returned without a receipt on this branch.
  const f = await fixture(BEDAVA,
    async () => new Response('data: ' + 'x'.repeat(5 * 1024 * 1024) + '\n\n',
      { headers: { 'content-type': 'text/event-stream' } }), 'opencode');
  const call = await f.call('impl', 'ses-buyuk', 'mimo-v2.6-flash-free', SIFIR);
  await (await call.invoke()).text();
  assert.equal(f.rows()[0].actual, 1, '4MB korumasi devreye girse de istek sayilmali');
});

test('a free request whose body the consumer cancels is counted (B110-1)', async () => {
  // If the consumer cancels the body `flush` is NEVER called (`cancel` is called): in a live run `cadre iptal` and
  // a timeout go exactly through this path and the reservation was left pending.
  const govde = () => new Response(new ReadableStream({
    start(controller) { controller.enqueue(new TextEncoder().encode('data: {"id":"r"}\n\n')); },
  }), { headers: { 'content-type': 'text/event-stream' } });
  const f = await fixture(BEDAVA, async () => govde(), 'opencode');
  const call = await f.call('impl', 'ses-iptal', 'mimo-v2.6-flash-free', SIFIR);
  const okuyucu = (await call.invoke()).body.getReader();
  await okuyucu.read();
  await okuyucu.cancel('iptal');
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(f.rows()[0].actual, 1, 'iptal edilen bedava istek de yapilmis istektir');
});

test('a paid request whose body the consumer cancels keeps its reservation (B110-1)', async () => {
  // The free bucket's cancel fix must not leak into the PAID bucket: if the amount is unknown the full reservation stays.
  const govde = () => new Response(new ReadableStream({
    start(controller) { controller.enqueue(new TextEncoder().encode('data: {"id":"r"}\n\n')); },
  }), { headers: { 'content-type': 'text/event-stream' } });
  const f = await fixture(go(1), async () => govde());
  const okuyucu = (await (await f.call()).invoke()).body.getReader();
  await okuyucu.read();
  await okuyucu.cancel('iptal');
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(f.rows()[0].actual, null, 'parali cagrida iptal sonrasi tutar bilinmiyor: rezervasyon durur');
});

test('FIX-70: an OpenRouter stream cut before its receipt records the generation id for reconciliation', async () => {
  const f = await fixture(orBudget, async () => new Response(new ReadableStream({
    start(controller) { controller.enqueue(new TextEncoder().encode('data: {"id":"gen-fix70abc","choices":[{"delta":{"content":"x"}}]}\n\n')); },
  }), { headers: { 'content-type': 'text/event-stream' } }), 'openrouter');
  orPrices(f);
  const okuyucu = (await (await f.call('impl', 'or-kesik', orModel)).invoke()).body.getReader();
  await okuyucu.read();
  await okuyucu.cancel('kesildi');
  await new Promise((r) => setTimeout(r, 50));
  const satir = f.rows()[0];
  assert.equal(satir.actual, null, 'tutar bilinmiyor: rezervasyon durur');
  assert.equal(JSON.parse(satir.metadata).generation, 'gen-fix70abc', 'uretim kimligi mutabakat icin kayitli');
});

test('observe mode records manual opencode spend without ever blocking it (B124)', async () => {
  // User decision 19.09: "opencode run means manual use — the budget must NOT CUT it but WATCH it."
  // In a run environment (CADRE_RUN_ID) the mode is ignored: automated work is always subject to the cap.
  let cagri = 0;
  const f = await fixture(go(0), async () => { cagri++; return response(); });   // daily share 0 = the gate normally denies
  delete process.env.CADRE_RUN_ID;

  const kesildi = await f.call('build');
  await assert.rejects(() => kesildi.invoke('is'), /CADRE_BUDGET_DENIED/, 'gozlem kapaliyken kapi keser');
  assert.equal(cagri, 0, 'reddedilen cagri saglayiciya hic gitmez');

  process.env.CADRE_BUTCE_GOZLEM = '1';
  try {
    const gozlemli = await f.call('build');
    const cevap = await gozlemli.invoke('is');
    assert.equal(cevap.status, 200, 'gozlem kipinde cagri gecer');
    assert.equal(cagri, 1, 'cagri gercekten saglayiciya gitti');
    await cevap.text();
    const satirlar = f.rows();
    assert.ok(satirlar.length > 0, 'harcama deftere yazildi (gorunurluk korunuyor)');
  } finally {
    delete process.env.CADRE_BUTCE_GOZLEM;
  }
});

test('observe mode is ignored inside a run: automatic work always obeys the cap (B124)', async () => {
  let cagri = 0;
  const f = await fixture(go(0), async () => { cagri++; return response(); });
  process.env.CADRE_BUTCE_GOZLEM = '1';
  process.env.CADRE_RUN_ID = '20260919-000000-testkosusu';
  try {
    const kosuda = await f.call('build');
    await assert.rejects(() => kosuda.invoke('is'), /CADRE_BUDGET_DENIED/,
      'kosu icinde gozlem kipi kapiyi acmaz');
    assert.equal(cagri, 0, 'kosu icindeki reddedilen cagri saglayiciya gitmez');
  } finally {
    delete process.env.CADRE_BUTCE_GOZLEM;
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

test('a plugin-side refusal records the provider so the motor can ban it (B154)', async () => {
  const f = await fixture(go(1), async () => response());
  const input = { sessionID: 's-x', agent: 'impl', model: { providerID: 'opencode-go', id: 'fiyatsiz', api: { id: 'fiyatsiz' },
    limit: { context: 100, output: 10 } } };   // no price -> the plugin denies
  await assert.rejects(() => f.hooks['chat.params'](input, { maxOutputTokens: 10 }), /CADRE_BUDGET_DENIED/);
  const denials = JSON.parse(execFileSync(process.env.CADRE_PYTHON, ['-c',
    "import sys,json;sys.path.insert(0,sys.argv[1]);from kosu import RunStore;print(json.dumps(RunStore().read(sys.argv[2]).get('denials') or []))",
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
    "import sys,json;sys.path.insert(0,sys.argv[1]);from kosu import RunStore;print(json.dumps(RunStore().read(sys.argv[2]).get('denials') or []))",
    join(f.home, 'code', 'lib'), process.env.CADRE_RUN_ID], { encoding: 'utf8' }));
  assert.ok(denials.length >= 1, JSON.stringify(denials));
  assert.ok(denials.every((d) => d.provider && d.provider !== 'None'), 'saglayicisiz red kaydi: ' + JSON.stringify(denials));
  assert.equal(denials.filter((d) => d.provider === 'opencode-go').length, 1, 'ayni red iki kez: ' + JSON.stringify(denials));
});

test('free providers refuse an invalid output cap before provider invocation (B186)', async () => {
  // after B169 number() returns NaN; on the free path chat.params should deny an invalid limit early.
  let requests = 0;
  const f = await fixture(null, async () => { requests++; return response(); }, 'opencode');
  const input = { sessionID: 's-free', agent: 'impl', model: { providerID: 'opencode', id: 'bedava-x', api: { id: 'bedava-x' },
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
    + 'data: {"type":"response.output_text.delta","delta":"ozet"}\n\n'
    + 'data: {"type":"response.completed","response":{"id":"resp_1","usage":{"input_tokens":1000,'
    + '"input_tokens_details":{"cached_tokens":900},"output_tokens":10,"output_tokens_details":{"reasoning_tokens":4}}}}\n\n';
  const f = await fixture(zen(1), async () => new Response(sse, { headers: { 'content-type': 'text/event-stream' } }), 'opencode');
  await responsesCall(f, sse);
  const row = f.rows().filter(r => JSON.parse(r.metadata).model === 'gpt-6-luna')[0];
  assert.equal(row.provider, 'opencode');
  assert.equal(row.actual, 210, '100 taze x 1 + 900 onbellek x 0.1 + 10 cikti x 2');
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
  const model = 'anthropic-bicimli';
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
  assert.notEqual(row.actual, null, 'makbuz yazilmali; askida kalan rezervasyon tavani sahte doldurur');
  assert.equal(row.actual, 210, '100 taze x 1 + 900 onbellek x 0.1 + 10 cikti x 2');
  assert.match(row.receipt, /:msg_1$/);
 }
});

test('CANLI1: silent streams are never aborted; the open-request marker follows the request', async (t) => {
  const { readdirSync, readFileSync } = await import('node:fs');
  const istekler = mkdtempSync(join(tmpdir(), 'qe-istek-'));
  const onceki = process.env.CADRE_REQUEST_DIR;
  process.env.CADRE_REQUEST_DIR = istekler;
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
    assert.equal(readdirSync(istekler).length, 1, 'istek girisinde acik-istek marker yazilir; akis sessiz ilerlerken de durur');
    upstream.enqueue(new TextEncoder().encode('data: {"id":"live","usage":{"prompt_tokens":1,"completion_tokens":1}}\n\ndata: [DONE]\n\n'));
    upstream.close();
    await res.text();
    await spin();
    assert.equal(signal.aborted, false, 'akan akis sabit sure yuzunden iptal edilmez');
    assert.equal(f.rows()[0].actual, 3);
    assert.equal(readdirSync(istekler).length, 0, 'istek tamamlandi -> marker silinir');
    // 3) Cancel: the SDK cancels the body -> the signal aborts and the marker is cleaned immediately.
    let cancelSignal;
    const h = await fixture(go(), async (_url, options) => {
      cancelSignal = options.signal;
      return new Response(new ReadableStream({ start(c) { c.enqueue(new TextEncoder().encode(': akis\n\n')); } }),
        { headers: { 'content-type': 'text/event-stream' } });
    });
    const hcall = await h.call('impl', 's-cancel');
    const iade = await hcall.invoke();
    await spin();
    assert.equal(readdirSync(istekler).length, 1, 'iptal oncesi marker yazilir');
    await iade.body.cancel('user cancelled');
    await spin();
    assert.equal(cancelSignal.aborted, true);
    assert.equal(readdirSync(istekler).length, 0, 'iptal edilen istek icin marker temizlenir');
  } finally {
    if (typeof onceki === 'undefined') delete process.env.CADRE_REQUEST_DIR; else process.env.CADRE_REQUEST_DIR = onceki;
    rmSync(istekler, { recursive: true, force: true });
  }
});

test('Zen without a readable balance observation denies before the network', async () => {
  let cagrildi = false;
  const f = await fixture({ zen: { db: 'yok', pay: 0, kullanilabilir: 0 } }, async () => { cagrildi = true; return response(); }, 'opencode');
  await assert.rejects(responsesCall(f, ''), /CADRE_BUDGET_DENIED/);
  assert.equal(cagrildi, false);
});

test('referee and QA calls get a role output cap so the acceptance guarantee fits small run caps (06.10 P6)', async () => {
  const f = await fixture(go(), async () => new Response('{}'));
  const model = { providerID: 'opencode-go', id: 'm', api: { id: 'm' }, limit: { context: 100000, output: 131072 }, cost: { input: 1, output: 2 } };
  for (const [rol, beklenen] of [['kalite', 32768], ['qa', 32768], ['impl', 32768]]) {   // 4c-4: sinirlar.json
    process.env.CADRE_ROL = rol;
    const params = { maxOutputTokens: 131072 };
    await f.hooks['chat.params']({ sessionID: 's', agent: 'build', model }, params);
    assert.equal(params.maxOutputTokens, beklenen, rol);
  }
  delete process.env.CADRE_ROL;
});

test('the 2x output retry is not clipped back to the opencode default output cap (QA3)', async () => {
  const f = await fixture(go(), async () => new Response('{}'));
  const model = { providerID: 'opencode-go', id: 'm', api: { id: 'm' }, limit: { context: 100000, output: 131072 }, cost: { input: 1, output: 2 } };
  process.env.CADRE_ROL = 'impl';
  try {
    for (const [kat, beklenen] of [[undefined, 32000], ['2', 65536]]) {
      if (kat) process.env.CADRE_CIKTI_KAT = kat; else delete process.env.CADRE_CIKTI_KAT;
      const params = { maxOutputTokens: 32000 };
      await f.hooks['chat.params']({ sessionID: 's', agent: 'build', model }, params);
      assert.equal(params.maxOutputTokens, beklenen, String(kat));
    }
  } finally {
    delete process.env.CADRE_ROL;
    delete process.env.CADRE_CIKTI_KAT;
  }
});

test('4c-4: open-request marker records chunk kind; an SSE comment is not progress, a token is', async () => {
  const { readdirSync, readFileSync } = await import('node:fs');
  const istekler = mkdtempSync(join(tmpdir(), 'qe-isaret-'));
  const onceki = process.env.CADRE_REQUEST_DIR;
  process.env.CADRE_REQUEST_DIR = istekler;
  const spin = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
  try {
    let upstream;
    const f = await fixture(go(), async () => new Response(new ReadableStream({ start(c) { upstream = c; } }),
      { headers: { 'content-type': 'text/event-stream' } }));
    const res = await (await f.call('impl', 's-isaret')).invoke();
    const okuyucu = res.body.getReader();
    const isaret = () => JSON.parse(readFileSync(join(istekler, readdirSync(istekler)[0]), 'utf8'));
    upstream.enqueue(new TextEncoder().encode(': OPENROUTER PROCESSING\n\n'));
    await okuyucu.read(); await spin();
    assert.equal(isaret().ilerleme_at, null, 'yorum yalniz baglanti acik demektir');
    for (const bos of ['data: {"type":"ping"}\n\n', 'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n\n',
                       'data: {"choices":[{"delta":{}}]}\n\n', 'data: {"choices":[{"delta":{}}],"usage":null}\n\n']) {
      upstream.enqueue(new TextEncoder().encode(bos));
      await okuyucu.read(); await spin();
      assert.equal(isaret().ilerleme_at, null, bos);
    }
    upstream.enqueue(new TextEncoder().encode('data: {"choices":[{"delta":{"content":"x"}}]}\n\n'));
    await okuyucu.read(); await spin();
    assert.equal(isaret().tur, 'token');
    assert.equal(typeof isaret().ilerleme_at, 'number', 'token parcasi ilerlemedir');
    upstream.close();
    await okuyucu.read();
  } finally {
    if (onceki === undefined) delete process.env.CADRE_REQUEST_DIR; else process.env.CADRE_REQUEST_DIR = onceki;
    rmSync(istekler, { recursive: true, force: true });
  }
});

test('16(13): on an OpenRouter tool round the plugin puts missing reasoning_details back and leaves existing ones untouched', async () => {
  const govdeler = [];
  const sse = (...satirlar) => new Response(satirlar.map((v) => `data: ${JSON.stringify(v)}\n\n`).join('') + 'data: [DONE]\n\n',
    { headers: { 'content-type': 'text/event-stream' } });
  const f = await fixture(orBudget, async (_url, options) => {
    govdeler.push(JSON.parse(options.body));
    if (govdeler.length > 1) return sse({ id: 'r2', choices: [{ delta: { content: 'bitti' }, finish_reason: 'stop' }], usage: { cost: 0.00001 } });
    return sse(
      { id: 'r1', choices: [{ delta: { reasoning_details: [{ type: 'reasoning.text', index: 0, text: 'a' }] } }] },
      { id: 'r1', choices: [{ delta: { reasoning_details: [{ type: 'reasoning.text', index: 0, text: 'b' }],
                                       tool_calls: [{ id: 'call_1', function: { name: 'read' } }] } }] },
      { id: 'r1', choices: [{ delta: {}, finish_reason: 'tool_calls' }], usage: { cost: 0.00002 } });
  }, 'openrouter');
  orPrices(f);
  const ilk = await f.call('impl', 'or-dongu', orModel);
  await (await ilk.invoke()).text();
  const ikinci = await f.call('impl', 'or-dongu', orModel);
  const mesajlar = [{ role: 'user', content: 'is' }, { role: 'assistant', content: '', tool_calls: [{ id: 'call_1', type: 'function' }] },
                    { role: 'tool', tool_call_id: 'call_1', content: 'dosya' }];
  await (await ikinci.invoke('devam', { messages: mesajlar })).text();
  assert.deepEqual(govdeler[1].messages[1].reasoning_details, [{ type: 'reasoning.text', index: 0, text: 'ab' }]);
  const ucuncu = await f.call('impl', 'or-dongu', orModel);
  const tasinmis = [mesajlar[0], { ...mesajlar[1], reasoning_details: [{ type: 'reasoning.text', index: 0, text: 'istemci' }] }, mesajlar[2]];
  await (await ucuncu.invoke('devam', { messages: tasinmis })).text();
  assert.equal(govdeler[2].messages[1].reasoning_details[0].text, 'istemci', 'istemci tasidiysa dokunulmaz');
});

test('CR-10: fragmented UTF-8 reasoning reaches the real budgeted continuation with an empty client field', async () => {
  const bodies = [];
  const f = await fixture(orBudget, async (_url, options) => {
    bodies.push(JSON.parse(options.body));
    if (bodies.length > 1) return new Response(JSON.stringify({ id: 'second', usage: { cost: .00001 } }));
    const frames = ['Türkçe ', 'özet'].map(text => ({ id: 'first', choices: [{ delta: {
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
  assert.deepEqual(bodies[1].messages[0].reasoning_details, [{ type: 'reasoning.text', text: 'Türkçe özet' }]);
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

const sseYanit = (...parcalar) => new Response(parcalar.map((v) => `data: ${JSON.stringify(v)}\n\n`).join('') + 'data: [DONE]\n\n',
  { headers: { 'content-type': 'text/event-stream' } });
const kademe = (input, output, ad, onbellek = input / 10) => ({ input, output, onbellek, ad, params: ['max_tokens'], max_cikti: 1000,
  uptime: 99.9, kuantizasyon: 'fp8' });
function iki(f, a, b, fiyatTs = Date.now() / 1000 - 5) {
  orPrices(f, { modeller: { [orModel]: { input: Math.max(a.input, b.input), output: Math.max(a.output, b.output),
    providers: ['streamlake/fp8', 'deepinfra/fp8'], fp8_cache: true, fiyat_ts: fiyatTs,
    kademeler: { 'streamlake/fp8': a, 'deepinfra/fp8': b } } } });
}
const meta = (row) => JSON.parse(row.metadata);
async function yapiskan(saniye, govde) {
  const onceki = SINIRLAR.openrouter.oturum_yapiskan_sn;
  SINIRLAR.openrouter.oturum_yapiskan_sn = saniye;
  try { return await govde(); } finally { SINIRLAR.openrouter.oturum_yapiskan_sn = onceki; }
}

test('CR-52: OpenRouter receipt keeps real usage, cache, reasoning and the served endpoint on the same ledger row', async () => {
  const f = await fixture(orBudget, async () => sseYanit(
    { id: 'gen-52', provider: 'StreamLake', model: orModel, choices: [{ index: 0, delta: { content: 'tamam' } }] },
    { id: 'gen-52', provider: 'StreamLake', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] },
    { id: 'gen-52', provider: 'StreamLake', choices: [], usage: { prompt_tokens: 1200, completion_tokens: 300,
      completion_tokens_details: { reasoning_tokens: 120 }, prompt_tokens_details: { cached_tokens: 1000, cache_write_tokens: 0 },
      cost: 0.0004, cache_discount: 0.0002, is_byok: false } }), 'openrouter');
  iki(f, kademe(0.4, 0.8, 'StreamLake'), kademe(1.3, 2.6, 'DeepInfra'));
  await (await (await f.call('impl', 'oturum-52', orModel)).invoke()).text();
  const [row] = f.rows();
  assert.equal(row.actual, 400, 'makbuz tutari saglayicinin usage.cost degeridir, token kirilimindan yeniden toplanmaz');
  const { sure_ms: sure, fiyat_ts: zaman, ...kullanim } = meta(row).kullanim;
  assert.deepEqual(kullanim, { girdi: 1200, cikti: 300, dusunme: 120, onbellek_okuma: 1000, onbellek_yazma: 0, ucret_usd: 0.0004,
    onbellek_indirimi_usd: 0.0002, byok: false, uc: 'StreamLake', uc_etiket: 'streamlake/fp8', model_yanit: orModel,
    bitis: 'stop', bicim: 'chat', fiyat_kaynagi: 'openrouter-uclari' });
  assert.ok(Number.isSafeInteger(sure) && sure >= 0);
  assert.ok(zaman > 0 && zaman <= Date.now() / 1000);
});

test('CR-52: Go, Anthropic and Responses usage map to one shape; absent fields stay absent; cut streams store nothing', async () => {
  const go1 = await fixture(go(), async () => new Response(JSON.stringify({ id: 'r', model: 'glm-5.3', choices: [{ finish_reason: 'stop',
    message: { content: 'x' } }], usage: { prompt_tokens: 10, completion_tokens: 5, prompt_tokens_details: { cached_tokens: 4 } } }),
    { headers: { 'content-type': 'application/json' } }));
  await (await (await go1.call()).invoke()).text();
  const { sure_ms: _s1, ...goKullanim } = meta(go1.rows()[0]).kullanim;
  assert.deepEqual(goKullanim, { girdi: 10, cikti: 5, onbellek_okuma: 4, model_yanit: 'glm-5.3', bitis: 'stop', bicim: 'chat',
    fiyat_kaynagi: 'opencode-katalog' });
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
  const { sure_ms: _s2, ...anthKullanim } = meta(anth.rows()[0]).kullanim;
  assert.deepEqual(anthKullanim, { girdi: 10, cikti: 7, onbellek_okuma: 5, onbellek_yazma: 2, model_yanit: 'qwen3.8-flash',
    bitis: 'end_turn', bicim: 'anthropic', fiyat_kaynagi: 'opencode-katalog' });
  const orYalniz = await fixture(orBudget, async () => new Response(JSON.stringify({ id: 'gen-x', usage: { cost: 0.00003 } })), 'openrouter');
  orPrices(orYalniz);
  await (await (await orYalniz.call('impl', 's-or', orModel)).invoke()).text();
  assert.equal(orYalniz.rows()[0].actual, 30);
  assert.deepEqual(Object.keys(meta(orYalniz.rows()[0]).kullanim).sort(), ['bicim', 'fiyat_kaynagi', 'fiyat_ts', 'sure_ms', 'ucret_usd'],
    'token alani yoksa sifir yazilmaz, alan hic olmaz');
  const kesik = await fixture(go(), async () => new Response('data: {"choices":[{"delta":{"content":"yarim"}}],"usage":null}\n\n',
    { headers: { 'content-type': 'text/event-stream' } }));
  await (await (await kesik.call()).invoke()).text();
  assert.equal(kesik.rows()[0].actual, null);
  assert.equal(meta(kesik.rows()[0]).kullanim, undefined);
});

test('CR-49: stable instruction/tool prefix and session continuation are measured on the body actually sent', async () => {
  const f = await fixture(go(10), async () => response());
  const araclar = [{ type: 'function', function: { name: 'read' } }];
  const ilk = [{ role: 'system', content: 'sabit talimat' }, { role: 'user', content: 'is' }];
  const devam = [...ilk, { role: 'assistant', content: '', tool_calls: [{ id: 'c1', type: 'function' }] },
                 { role: 'tool', tool_call_id: 'c1', content: 'cikti' }];
  const gonder = async (oturum, messages, tools = araclar) => (await (await f.call('impl', oturum)).invoke('x', { messages, tools })).text();
  await gonder('p1', ilk);
  await gonder('p1', devam);
  await gonder('p1', [{ role: 'system', content: 'degisen talimat' }, ...devam.slice(1)]);
  await gonder('p2', ilk);
  await gonder('p1', [{ role: 'system', content: 'degisen talimat' }, { role: 'user', content: 'budanmis' }, ...devam.slice(2)]);
  const onek = f.rows().map((r) => meta(r).onek);
  assert.equal(onek[0].devam, null, 'oturumun ilk istegi');
  assert.equal(onek[1].devam, true, 'onceki istegin tum mesajlari aynen once gelir');
  assert.equal(onek[1].sabit, onek[0].sabit);
  assert.notEqual(onek[2].sabit, onek[0].sabit, 'talimat degisti');
  assert.equal(onek[2].devam, false);
  assert.equal(onek[3].sabit, onek[0].sabit, 'ayni talimat/arac oneki baska oturumda da ayni');
  assert.equal(onek[3].devam, null);
  assert.equal(onek[4].sabit, onek[2].sabit);
  assert.equal(onek[4].devam, false, 'eski mesaj degisirse onbellek oneki kirilir');
  assert.ok(onek.every((o) => /^[0-9a-f]{16}$/.test(o.sabit) && Number.isSafeInteger(o.mesaj)));
});

test('CR-48: session order keeps the cached endpoint while staying is cheaper; discount end, stale or disabled record switch', () => {
  const simdi = 1_000_000;
  const m = { kademeler: { A: { input: 1, output: 1, onbellek: 0.1 }, B: { input: 0.8, output: 1, onbellek: 0.08 } } };
  const kayit = { uc: 'A', sira: 'A', basari: true, karakter: 90_000, ts: simdi - 60 };
  const ortak = { m, providers: ['B', 'A'], girdi: 100_000, istenen: 1000, kayit, uclar: {}, kalanSn: NaN, simdi,
    yapiskanSn: 600, hataSn: 1800, devam: true };
  const kal = oturumSirasi(ortak);
  assert.deepEqual(kal.providers, ['A', 'B'], 'kalis 20000 < gecis 81000');
  assert.match(kal.neden.join(' '), /korundu/);
  const zam = oturumSirasi({ ...ortak, m: { kademeler: { ...m.kademeler, A: { input: 10, output: 10, onbellek: 1 } } } });
  assert.deepEqual(zam.providers, ['B', 'A'], 'indirim bitti: onbellek kaybi dahil gecis ucuz');
  assert.match(zam.neden.join(' '), /birakildi/);
  assert.deepEqual(oturumSirasi({ ...ortak, m: { kademeler: { ...m.kademeler, A: { input: 1, output: 1 } } } }).providers, ['B', 'A'],
    'onbellek fiyati bilinmeyen uc indirim varsayilmaz');
  assert.deepEqual(oturumSirasi({ ...ortak, kayit: { ...kayit, ts: simdi - 601 } }).providers, ['B', 'A'], 'bayat oturum kaydi');
  assert.deepEqual(oturumSirasi({ ...ortak, yapiskanSn: 0, hataSn: 0 }).providers, ['B', 'A'], 'kapali politika eski siradir');
  const belirsiz = oturumSirasi({ ...ortak, kayit: { ...kayit, uc: null, sira: 'A' } });
  assert.deepEqual(belirsiz.providers, ['B', 'A'], 'gercek uc bilinmiyorsa ilk tercih onbellegin yeri sayilmaz, kredi yok');
  assert.match(belirsiz.neden.join(' '), /gercek uc bilinmiyor/);
  assert.deepEqual(belirsiz.oturum, { uc: 'A', uc_kaynagi: 'ilk_tercih', onek: true, onbellek_kredisi: 0 });
  assert.deepEqual(kal.oturum, { uc: 'A', uc_kaynagi: 'yanit', onek: true, onbellek_kredisi: 90_000 });
  assert.deepEqual(oturumSirasi({ ...ortak, providers: ['B'] }).providers, ['B'], 'artik izinli olmayan uc geri gelmez');
});

test('CR-48: failed/429 endpoints lose stickiness and go last; measured slow endpoints that cannot fit the time are not first', () => {
  const simdi = 2_000_000;
  const m = { kademeler: { A: { input: 0.2, output: 0.4, onbellek: 0.02 }, B: { input: 1, output: 2, onbellek: 0.1 } } };
  const ortak = { m, providers: ['A', 'B'], girdi: 10_000, istenen: 1000, kayit: null, uclar: {}, kalanSn: NaN, simdi,
    yapiskanSn: 600, hataSn: 1800 };
  assert.deepEqual(oturumSirasi(ortak).providers, ['A', 'B']);
  const hata = { A: { son: 'hata', son_ts: simdi - 10, hata: 1, basari: 3 } };
  assert.deepEqual(oturumSirasi({ ...ortak, uclar: hata }).providers, ['B', 'A'], 'son sonucu hatali uc geride');
  assert.deepEqual(oturumSirasi({ ...ortak, uclar: { A: { ...hata.A, son_ts: simdi - 1801 } } }).providers, ['A', 'B'], 'eski hata');
  const basarisiz = { uc: 'B', sira: 'B', basari: false, karakter: 9000, ts: simdi - 5 };
  assert.deepEqual(oturumSirasi({ ...ortak, kayit: basarisiz }).providers, ['A', 'B'], 'basarisiz oturum ucu yapiskan degil');
  const hiz = { A: { hiz_tps: 10 }, B: { hiz_tps: 100 } };
  assert.deepEqual(oturumSirasi({ ...ortak, uclar: hiz, kalanSn: 50 }).providers, ['B', 'A'], 'A 100 sn ister, 50 sn kaldi');
  assert.deepEqual(oturumSirasi({ ...ortak, uclar: hiz, kalanSn: 500 }).providers, ['A', 'B'], 'sure yeterli: ucuz uc');
  assert.deepEqual(oturumSirasi({ ...ortak, uclar: { B: { hiz_tps: 100 } }, kalanSn: 50 }).providers, ['A', 'B'],
    'olculmemis hiz sigmaz sayilmaz');
  const yapiskan = { uc: 'A', sira: 'A', basari: true, karakter: 9000, ts: simdi - 5 };
  const sonuc = oturumSirasi({ ...ortak, providers: ['B', 'A'], kayit: yapiskan, uclar: hiz, kalanSn: 50 });
  assert.deepEqual(sonuc.providers, ['B', 'A'], 'sureye sigmayan oturum ucu da korunmaz');
  assert.match(sonuc.neden.join(' '), /sigmaz/);
});

test('CR-48: the gate keeps OR filters, reorders only inside them, persists the choice across restart and concurrent calls', () => yapiskan(600, async () => {
  const govdeler = [];
  const f = await fixture(orBudget, async (_url, options) => {
    govdeler.push(JSON.parse(options.body));
    return sseYanit({ id: `gen-${govdeler.length}`, provider: 'StreamLake', choices: [{ delta: { content: 'x' }, finish_reason: 'stop' }],
      usage: { prompt_tokens: 50, completion_tokens: 5, prompt_tokens_details: { cached_tokens: 40 }, cost: 0.00001 } });
  }, 'openrouter');
  iki(f, kademe(0.5, 1, 'StreamLake'), kademe(0.9, 1, 'DeepInfra'));
  await (await (await f.call('impl', 'yapiskan', orModel)).invoke()).text();
  assert.deepEqual(govdeler[0].provider.order, ['streamlake/fp8', 'deepinfra/fp8']);
  iki(f, kademe(1.0, 1, 'StreamLake'), kademe(0.9, 1, 'DeepInfra'));
  await (await (await f.call('impl', 'yapiskan', orModel)).invoke()).text();
  await (await (await f.call('impl', 'baska-oturum', orModel)).invoke()).text();
  assert.deepEqual(govdeler[1].provider, { only: ['streamlake/fp8', 'deepinfra/fp8'], order: ['streamlake/fp8', 'deepinfra/fp8'],
    quantizations: ['fp8'], allow_fallbacks: false, require_parameters: true, max_price: { prompt: 1.74, completion: 3.84 } });
  assert.deepEqual(govdeler[2].provider.order, ['deepinfra/fp8', 'streamlake/fp8'], 'yeni oturumda onbellek yok, ucuz uc');
  const satirlar = f.rows();
  assert.equal(satirlar[1].reserved, satirlar[2].reserved, 'sira rezervasyonu degistirmez: izinli en pahali uctan');
  assert.match(meta(satirlar[1]).uc_secimi.neden.join(' '), /korundu/);
  assert.deepEqual(meta(satirlar[1]).uc_secimi.sira, ['streamlake/fp8', 'deepinfra/fp8']);
  const { readFileSync } = await import('node:fs');
  const ipucu = JSON.parse(readFileSync(join(f.home, 'zincir', 'or-oturum-uclari.json'), 'utf8'));
  assert.equal(ipucu.surum, 1);
  assert.equal(ipucu.oturumlar[JSON.stringify(['yapiskan', orModel])].uc, 'streamlake/fp8');
  delete globalThis[Symbol.for(`cadre.saglayici-kapisi:${f.home}`)];
  const yeniden = await plugin();
  const config = { enabled_providers: [], provider: { openrouter: { options: { fetch: async (_u, o) => {
    govdeler.push(JSON.parse(o.body));
    return sseYanit({ id: `gen-${govdeler.length}`, provider: 'StreamLake', choices: [{ delta: {}, finish_reason: 'stop' }],
      usage: { prompt_tokens: 50, completion_tokens: 5, cost: 0.00001 } });
  } } } } };
  await yeniden.config(config);
  const istek = async (oturum) => {
    const input = { sessionID: oturum, agent: 'impl', model: { providerID: 'openrouter', id: orModel, api: { id: orModel },
      limit: { context: 100, output: 10 }, cost: { input: 1, output: 2 } } };
    const params = { maxOutputTokens: 10 };
    await yeniden['chat.params'](input, params);
    const headers = { headers: {} };
    await yeniden['chat.headers'](input, headers);
    return config.provider.openrouter.options.fetch('https://fixture.invalid/chat/completions', { method: 'POST', headers: headers.headers,
      body: JSON.stringify({ model: orModel, max_tokens: params.maxOutputTokens, messages: [{ role: 'user', content: 'plain task' },
        { role: 'assistant', content: 'x' }, { role: 'user', content: 'devam' }] }) });
  };
  await (await istek('yapiskan')).text();
  assert.deepEqual(govdeler[3].provider.order, ['streamlake/fp8', 'deepinfra/fp8'], 'yeniden baslatma oturum secimini dosyadan okur');
  const ikili = await Promise.all([istek('yapiskan'), istek('yapiskan')]);
  await Promise.all(ikili.map((r) => r.text()));
  const son = f.rows();
  assert.equal(son.length, 6);
  assert.ok(son.every((r) => r.actual === 10), 'eszamanli iki istek ayri rezervasyon ve makbuzla kapanir');
  JSON.parse(readFileSync(join(f.home, 'zincir', 'or-oturum-uclari.json'), 'utf8'));
}));

test('CR-50: the effort actually sent and its policy are recorded on the reservation; default effort leaves no policy', async () => {
  const katalog = join(temporary, 'efor-katalog.json');
  writeFileSync(katalog, JSON.stringify({ 'opencode-go': { models: { model: { reasoning_options: [{ type: 'effort', values: ['low', 'high'] }] } } } }));
  const onceki = { katalog: process.env.CADRE_MODEL_KATALOG, rol: process.env.CADRE_ROL, efor: process.env.CADRE_EFOR,
    politika: process.env.CADRE_EFOR_POLITIKASI };
  try {
    Object.assign(process.env, { CADRE_MODEL_KATALOG: katalog, CADRE_ROL: 'impl', CADRE_EFOR: 'low', CADRE_EFOR_POLITIKASI: 'ekonomik-efor-v1' });
    const f = await fixture(go(10), async () => response());
    await (await (await f.call('build', 'efor-1')).invoke()).text();
    delete process.env.CADRE_EFOR;
    delete process.env.CADRE_EFOR_POLITIKASI;
    await (await (await f.call('build', 'efor-2')).invoke()).text();
    const [ekonomik, varsayilan] = f.rows().map((r) => meta(r).efor);
    assert.deepEqual(ekonomik, { seviye: 'low', politika: 'ekonomik-efor-v1' });
    assert.equal(varsayilan, undefined, 'politika yokken saglayici varsayilani; alan uydurulmaz');
  } finally {
    for (const [key, value] of [['CADRE_MODEL_KATALOG', onceki.katalog], ['CADRE_ROL', onceki.rol], ['CADRE_EFOR', onceki.efor],
                                ['CADRE_EFOR_POLITIKASI', onceki.politika]]) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
  }
});

test('CR-48/52: budget refusal and a consumer cancel record no usage, no session success and make no extra call', async () => {
  const { existsSync, readFileSync } = await import('node:fs');
  let cagri = 0;
  const red = await fixture({ or: { db: 'ok', pay: 0, kullanilabilir: 0, kalan: 0 } }, async () => { cagri++; return response(); }, 'openrouter');
  iki(red, kademe(0.5, 1, 'StreamLake'), kademe(0.9, 1, 'DeepInfra'));
  await assert.rejects((await red.call('impl', 'red-oturum', orModel)).invoke, /CADRE_BUDGET_DENIED/);
  assert.equal(cagri, 0);
  assert.equal(existsSync(join(red.home, 'zincir', 'or-oturum-uclari.json')), false, 'reddedilen istek oturum basarisi yazmaz');
  const parca = new TextEncoder().encode('data: {"id":"gen-i","provider":"StreamLake","choices":[{"delta":{"content":"a"}}]}\n\n');
  const iptal = await fixture(orBudget, async () => new Response(new ReadableStream({ start(c) { c.enqueue(parca); } }),
    { headers: { 'content-type': 'text/event-stream' } }), 'openrouter');
  iki(iptal, kademe(0.5, 1, 'StreamLake'), kademe(0.9, 1, 'DeepInfra'));
  const yanit = await (await iptal.call('impl', 'iptal-oturum', orModel)).invoke();
  const okuyucu = yanit.body.getReader();
  await okuyucu.read();
  await okuyucu.cancel();
  const [satir] = iptal.rows();
  assert.equal(satir.actual, null, 'iptalde yukumluluk askida kalir');
  assert.equal(meta(satir).kullanim, undefined);
  const dosya = join(iptal.home, 'zincir', 'or-oturum-uclari.json');
  const oturumlar = existsSync(dosya) ? JSON.parse(readFileSync(dosya, 'utf8')).oturumlar : {};
  assert.equal(oturumlar[JSON.stringify(['iptal-oturum', orModel])], undefined);
});

test('CR-49: reasoning carried by the gate keeps the tool-loop prefix stable across turns', async () => {
  const f = await fixture(orBudget, async (_url, options) => {
    const body = JSON.parse(options.body);
    if (body.messages.length === 1) {
      return sseYanit({ id: 'r1', choices: [{ delta: { reasoning_details: [{ type: 'reasoning.text', index: 0, text: 'plan' }],
        tool_calls: [{ id: 'call_a', function: { name: 'read' } }] } }] },
        { id: 'r1', choices: [{ delta: {}, finish_reason: 'tool_calls' }], usage: { cost: 0.00001 } });
    }
    return sseYanit({ id: `r${body.messages.length}`, choices: [{ delta: { content: 'ok' }, finish_reason: 'stop' }], usage: { cost: 0.00001 } });
  }, 'openrouter');
  orPrices(f);
  const user = { role: 'user', content: 'is' };
  const asistan = { role: 'assistant', content: '', tool_calls: [{ id: 'call_a', type: 'function' }] };
  const arac = { role: 'tool', tool_call_id: 'call_a', content: 'dosya' };
  const gonder = async (messages) => (await (await f.call('impl', 'arac-dongusu', orModel)).invoke('x', { messages })).text();
  await gonder([user]);
  await gonder([user, asistan, arac]);
  await gonder([user, asistan, arac, { role: 'assistant', content: 'ara' }, { role: 'user', content: 'devam et' }]);
  const onek = f.rows().map((r) => meta(r).onek);
  assert.deepEqual(onek.map((o) => o.devam), [null, true, true], 'eklentinin geri koydugu reasoning_details her turda ayni');
});

test('CR-48: a 429 closes its reservation at zero and the next request in that session does not stick to the failed route', () => yapiskan(600, async () => {
  let n = 0;
  const f = await fixture(orBudget, async () => {
    n += 1;
    if (n === 2) return new Response('{"error":{"code":429}}', { status: 429, headers: { 'content-type': 'application/json' } });
    return sseYanit({ id: `gen-${n}`, provider: 'StreamLake', choices: [{ delta: { content: 'x' }, finish_reason: 'stop' }],
      usage: { prompt_tokens: 50, completion_tokens: 5, cost: 0.00001 } });
  }, 'openrouter');
  iki(f, kademe(0.5, 1, 'StreamLake'), kademe(0.9, 1, 'DeepInfra'));
  await (await (await f.call('impl', 'sinir', orModel)).invoke()).text();
  await (await (await f.call('impl', 'sinir', orModel)).invoke()).text();
  iki(f, kademe(1.0, 1, 'StreamLake'), kademe(0.9, 1, 'DeepInfra'));
  await (await (await f.call('impl', 'sinir', orModel)).invoke()).text();
  const satirlar = f.rows();
  assert.equal(satirlar[1].actual, 0);
  assert.match(satirlar[1].receipt, /:http-429$/);
  assert.deepEqual(meta(satirlar[2]).uc_secimi.sira, ['deepinfra/fp8', 'streamlake/fp8'], 'basarisiz son istekten sonra yapiskanlik yok');
  assert.match(meta(satirlar[2]).uc_secimi.neden.join(' '), /basarisiz/);
}));

test('CR-48: an empty or missing deadline never marks routes as too slow; a real short deadline demotes the measured slow route', async () => {
  const govdeler = [];
  const f = await fixture(orBudget, async (_url, options) => {
    govdeler.push(JSON.parse(options.body));
    return sseYanit({ id: `gen-${govdeler.length}`, choices: [{ delta: {}, finish_reason: 'stop' }], usage: { cost: 0.00001 } });
  }, 'openrouter');
  iki(f, kademe(0.5, 1, 'StreamLake'), kademe(0.9, 1, 'DeepInfra'));
  const simdi = Date.now() / 1000;
  writeFileSync(join(f.home, 'zincir', 'or-oturum-uclari.json'), JSON.stringify({ surum: 1, oturumlar: {}, uclar: {
    [JSON.stringify([orModel, 'streamlake/fp8'])]: { hiz_tps: 1, son: 'basari', son_ts: simdi, ts: simdi },
    [JSON.stringify([orModel, 'deepinfra/fp8'])]: { hiz_tps: 100, son: 'basari', son_ts: simdi, ts: simdi } } }));
  const onceki = process.env.CADRE_SURE_SON_TS;
  try {
    for (const [deger, beklenen] of [['', 'streamlake/fp8'], [undefined, 'streamlake/fp8'], ['yok', 'streamlake/fp8'],
                                     [String(simdi + 3600), 'streamlake/fp8'], [String(simdi + 5), 'deepinfra/fp8']]) {
      if (deger === undefined) delete process.env.CADRE_SURE_SON_TS; else process.env.CADRE_SURE_SON_TS = deger;
      await (await (await f.call('impl', `sure-${govdeler.length}`, orModel)).invoke()).text();
      assert.equal(govdeler.at(-1).provider.order[0], beklenen, String(deger));
      const neden = meta(f.rows().at(-1)).uc_secimi.neden.join(' ');
      assert.equal(/sigmaz/.test(neden), beklenen === 'deepinfra/fp8', `${deger}: ${neden}`);
    }
  } finally {
    if (onceki === undefined) delete process.env.CADRE_SURE_SON_TS; else process.env.CADRE_SURE_SON_TS = onceki;
  }
});

test('CR-48/49: a broken prefix (compaction or changed instructions) gives the old route no cache credit', () => {
  const simdi = 3_000_000;
  const m = { kademeler: { A: { input: 1, output: 1, onbellek: 0.1 }, B: { input: 0.8, output: 1, onbellek: 0.08 } } };
  const kayit = { uc: 'A', sira: 'A', basari: true, karakter: 90_000, ts: simdi - 30 };
  const ortak = { m, providers: ['B', 'A'], girdi: 100_000, istenen: 1000, kayit, uclar: {}, kalanSn: NaN, simdi, yapiskanSn: 600, hataSn: 1800 };
  assert.deepEqual(oturumSirasi({ ...ortak, devam: true }).providers, ['A', 'B']);
  const bilinmeyen = oturumSirasi({ ...ortak, devam: null });
  assert.deepEqual(bilinmeyen.providers, ['B', 'A'], 'restart/compaction sonrasi dogrulanmamis onek onbellek kredisi almaz');
  assert.match(bilinmeyen.neden.join(' '), /onek dogrulanmadi/);
  assert.equal(bilinmeyen.oturum.onbellek_kredisi, 0);
  assert.deepEqual(oturumSirasi({ ...ortak }).providers, ['B', 'A'], 'devam alani hic yoksa da bilinmiyordur');
  const kirik = oturumSirasi({ ...ortak, devam: false });
  assert.deepEqual(kirik.providers, ['B', 'A'], 'onek kirildiysa onbellek kazanci yok, ucuz uc');
  assert.match(kirik.neden.join(' '), /onek kirik/);
});

const ucYaniti = (govdeler, saglayiciAdi = true) => async (_url, options) => {
  const body = JSON.parse(options.body);
  govdeler.push(body);
  const ilk = body.provider?.order?.[0];
  return sseYanit({ id: `gen-${govdeler.length}`, ...(saglayiciAdi ? { provider: ilk === 'deepinfra/fp8' ? 'DeepInfra' : 'StreamLake' } : {}),
    choices: [{ delta: { content: 'x' }, finish_reason: 'stop' }], usage: { prompt_tokens: 50, completion_tokens: 5, cost: 0.00001 } });
};

test('TSR-01: unmeasured session stickiness ships disabled; the gate keeps the health/price order and every OR limit', async () => {
  assert.equal(SINIRLAR.openrouter.oturum_yapiskan_sn, 0, 'olculmemis 600 sn oturum sabitlemesi varsayilan kapali');
  assert.equal(SINIRLAR.openrouter.uc_hata_sn, 1800, 'son hata geri alma sagliktir, acik kalir');
  const govdeler = [];
  const f = await fixture(orBudget, ucYaniti(govdeler), 'openrouter');
  iki(f, kademe(0.5, 1, 'StreamLake'), kademe(0.9, 1, 'DeepInfra'));
  await (await (await f.call('impl', 'varsayilan', orModel)).invoke()).text();
  iki(f, kademe(1.0, 1, 'StreamLake'), kademe(0.9, 1, 'DeepInfra'));
  await (await (await f.call('impl', 'varsayilan', orModel)).invoke()).text();
  const satirlar = f.rows();
  assert.equal(meta(satirlar[1]).onek.devam, true, 'onek dogrulanmis olsa da sabitleme kapali');
  assert.deepEqual(govdeler[1].provider, { only: ['deepinfra/fp8', 'streamlake/fp8'], order: ['deepinfra/fp8', 'streamlake/fp8'],
    quantizations: ['fp8'], allow_fallbacks: false, require_parameters: true, max_price: { prompt: 1.74, completion: 3.84 } });
  assert.doesNotMatch(meta(satirlar[1]).uc_secimi.neden.join(' '), /korundu/);
  assert.equal(meta(satirlar[1]).uc_secimi.oturum, undefined, 'kapali politikada oturum tahmini yazilmaz');
});

test('TSR-01: stickiness never lifts an endpoint below the uptime threshold over a healthy one, at any price', async () => {
  const simdi = 4_000_000;
  const m = { kademeler: { A: { input: 1, output: 1, onbellek: 0.1, uptime: 90 }, B: { input: 0.8, output: 1, onbellek: 0.08, uptime: 99.9 } } };
  const kayit = { uc: 'A', sira: 'A', basari: true, karakter: 90_000, ts: simdi - 30 };
  const ortak = { m, providers: ['B', 'A'], girdi: 100_000, istenen: 1000, kayit, uclar: {}, kalanSn: NaN, simdi,
    yapiskanSn: 600, hataSn: 1800, devam: true, dusukUptime: 95 };
  const sonuc = oturumSirasi(ortak);
  assert.deepEqual(sonuc.providers, ['B', 'A'], 'onbellek kredisi sagligi asamaz');
  assert.match(sonuc.neden.join(' '), /uptime 90 < 95/);
  const saglikli = { kademeler: { ...m.kademeler, A: { ...m.kademeler.A, uptime: 99.9 } } };
  assert.deepEqual(oturumSirasi({ ...ortak, m: saglikli }).providers, ['A', 'B'], 'ayni saglik sinifinda dogrulanmis kalis ucuzsa korunur');
  const ikisiDusuk = { kademeler: { ...m.kademeler, B: { ...m.kademeler.B, uptime: 80 } } };
  assert.deepEqual(oturumSirasi({ ...ortak, m: ikisiDusuk }).providers, ['A', 'B'], 'ayni (dusuk) saglik sinifinda kalis karsilastirmasi gecerli');
  const digeriDusuk = { kademeler: { A: { ...m.kademeler.A, uptime: 99.9 }, B: { ...m.kademeler.B, uptime: 80 } } };
  assert.deepEqual(oturumSirasi({ ...ortak, providers: ['A', 'B'], m: digeriDusuk, devam: null }).providers, ['A', 'B'],
    'daha saglikli oturum ucu taban sirada zaten onde');

  await yapiskan(600, async () => {
    const govdeler = [];
    const f = await fixture(orBudget, ucYaniti(govdeler), 'openrouter');
    iki(f, kademe(0.5, 1, 'StreamLake'), kademe(0.8, 1, 'DeepInfra'));
    await (await (await f.call('impl', 'saglik', orModel)).invoke()).text();
    iki(f, { ...kademe(0.5, 1, 'StreamLake'), uptime: 90 }, kademe(0.8, 1, 'DeepInfra'));
    await (await (await f.call('impl', 'saglik', orModel)).invoke()).text();
    assert.deepEqual(govdeler[0].provider.order, ['streamlake/fp8', 'deepinfra/fp8']);
    assert.deepEqual(govdeler[1].provider.order, ['deepinfra/fp8', 'streamlake/fp8'], 'katalog %90 < %95: saglikli uc once');
    const [ilk, ikinci] = f.rows();
    assert.equal(meta(ikinci).onek.devam, true);
    assert.match(meta(ikinci).uc_secimi.neden.join(' '), /korunmadi: uptime/);
    assert.equal(ikinci.reserved, ilk.reserved, 'rezervasyon izinli en pahali uctan; sira degisimi tutari degistirmez');
  });
});

test('TSR-01: after a restart an unverified prefix earns no cache credit, so the now cheaper route goes first', () => yapiskan(600, async () => {
  const govdeler = [];
  const f = await fixture(orBudget, ucYaniti(govdeler), 'openrouter');
  iki(f, kademe(0.5, 1, 'StreamLake'), kademe(0.8, 1, 'DeepInfra'));
  await (await (await f.call('impl', 'yeniden', orModel)).invoke('x'.repeat(2000))).text();
  iki(f, kademe(1.0, 1, 'StreamLake'), kademe(0.8, 1, 'DeepInfra'));
  delete globalThis[Symbol.for(`cadre.saglayici-kapisi:${f.home}`)];
  const yeniden = await (await import('../lib/butce-plugin.mjs?tsr01-yeniden-baslatma')).default();
  const config = { enabled_providers: [], provider: { openrouter: { options: { fetch: ucYaniti(govdeler) } } } };
  await yeniden.config(config);
  const input = { sessionID: 'yeniden', agent: 'impl', model: { providerID: 'openrouter', id: orModel, api: { id: orModel },
    limit: { context: 100, output: 10 }, cost: { input: 1, output: 2 } } };
  const params = { maxOutputTokens: 10 };
  await yeniden['chat.params'](input, params);
  const headers = { headers: {} };
  await yeniden['chat.headers'](input, headers);
  await (await config.provider.openrouter.options.fetch('https://fixture.invalid/chat/completions', { method: 'POST', headers: headers.headers,
    body: JSON.stringify({ model: orModel, max_tokens: params.maxOutputTokens, messages: [{ role: 'user', content: 'z'.repeat(2000) }] }) })).text();
  const ikinci = meta(f.rows()[1]);
  assert.equal(ikinci.onek.devam, null, 'restart sonrasi onek bilinmiyor');
  assert.deepEqual(govdeler[1].provider.order, ['deepinfra/fp8', 'streamlake/fp8'], 'eski karakterlere kredi yok: ucuz uc once');
  assert.match(ikinci.uc_secimi.neden.join(' '), /onek dogrulanmadi/);
  assert.deepEqual(ikinci.uc_secimi.oturum, { uc: 'streamlake/fp8', uc_kaynagi: 'yanit', onek: null, onbellek_kredisi: 0 });
}));

test('TSR-01: when the response names no endpoint the first preference is not treated as the cache location', () => yapiskan(600, async () => {
  const govdeler = [];
  const f = await fixture(orBudget, ucYaniti(govdeler, false), 'openrouter');
  iki(f, kademe(0.5, 1, 'StreamLake'), kademe(0.9, 1, 'DeepInfra'));
  await (await (await f.call('impl', 'belirsiz-uc', orModel)).invoke()).text();
  const { readFileSync } = await import('node:fs');
  const kayit = JSON.parse(readFileSync(join(f.home, 'zincir', 'or-oturum-uclari.json'), 'utf8')).oturumlar[JSON.stringify(['belirsiz-uc', orModel])];
  assert.equal(kayit.uc, null, 'gonderilen ilk tercih gercek uc diye yazilmaz');
  assert.equal(kayit.sira, 'streamlake/fp8');
  iki(f, kademe(1.0, 1, 'StreamLake'), kademe(0.9, 1, 'DeepInfra'));
  await (await (await f.call('impl', 'belirsiz-uc', orModel)).invoke()).text();
  const ikinci = meta(f.rows()[1]);
  assert.equal(ikinci.onek.devam, true);
  assert.deepEqual(govdeler[1].provider.order, ['deepinfra/fp8', 'streamlake/fp8']);
  assert.match(ikinci.uc_secimi.neden.join(' '), /gercek uc bilinmiyor/);
  assert.deepEqual(ikinci.uc_secimi.oturum, { uc: 'streamlake/fp8', uc_kaynagi: 'ilk_tercih', onek: true, onbellek_kredisi: 0 });
}));
