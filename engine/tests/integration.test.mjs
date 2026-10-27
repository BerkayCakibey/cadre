// Real-plugin integration tests (07.10): the real cadre-plugin.mjs + the real Python gate/ledger/monthly pool
// + a real catalogue record (data/catalog-20261007.json, from opencode models.json) + a fake HTTP endpoint. The only fake part is the provider observation.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { cpSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { execFileSync } from 'node:child_process';
import { createServer } from 'node:http';

const LIB = fileURLToPath(new URL('../lib/', import.meta.url));
const CATALOG = fileURLToPath(new URL('./data/catalog-20261007.json', import.meta.url));
const catalog = JSON.parse(readFileSync(CATALOG, 'utf8'));
process.env.CADRE_PYTHON ||= 'python3';
const temporary = mkdtempSync(join(tmpdir(), 'qe-integration-'));
after(() => rmSync(temporary, { recursive: true, force: true }));
let order = 0;

const go = { go: { db: 'ok', unit: 'points', pay: 200, available: 200, window_open: true } };   // 4c-13: points ($30, the $15 model)
const response = (usage = { prompt_tokens: 1000, completion_tokens: 100 }) =>
  new Response(JSON.stringify({ id: 'receipt', usage }), { headers: { 'content-type': 'application/json' } });

function codeRoot(home, entry_name, observation, inspectIpc = false) {
  const code = join(home, entry_name);
  cpSync(LIB, join(code, 'lib'), { recursive: true, filter: (p) => !p.includes('__pycache__') });
  writeFileSync(join(code, 'lib', 'provider_budget.py'), `import json,datetime\nd=json.loads(${JSON.stringify(JSON.stringify(observation))})\n` +
    `d.update(ts=datetime.datetime.now().isoformat(),today=datetime.date.today().isoformat())\nprint(json.dumps(d))\n`);
  if (inspectIpc) {
    const gate = join(code, 'lib', 'budget_gate.py');
    const observer = `\ndef _test_ipc_read(stream):
    raw = stream.read()
    value = json.loads(raw)
    if value.get('action') == 'reserve':
        with open(${JSON.stringify(join(home, 'budget-ipc.jsonl'))}, 'a') as out:
            out.write(json.dumps({'keys': sorted(value), 'bytes': len(raw.encode())}) + '\\n')
    return value
\n`;
    writeFileSync(gate, readFileSync(gate, 'utf8').replace('request = json.load(sys.stdin)', 'request = _test_ipc_read(sys.stdin)') + observer);
    // The observer must be defined before the CLI enters main().
    const contents = readFileSync(gate, 'utf8');
    writeFileSync(gate, contents.replace("if __name__ == '__main__':\n    raise SystemExit(main())", '') + "\nif __name__ == '__main__':\n    raise SystemExit(main())\n");
  }
  return code;
}

function py(code, script) {
  return execFileSync(process.env.CADRE_PYTHON, ['-c', `import sys;sys.path.insert(0,sys.argv[1]);${script}`, join(code, 'lib')],
    { encoding: 'utf8', env: process.env }).trim();
}

function home_dir(observation, cap = 2, inspectIpc = false) {
  const home = join(temporary, String(++order));
  mkdirSync(join(home, 'chain'), { recursive: true });
  const code = codeRoot(home, 'code', observation, inspectIpc);
  Object.assign(process.env, { CADRE_HOME: home, CADRE_ENGINE_ROOT: code, CADRE_MODEL_CATALOG: CATALOG,
    OPENCODE_EXPERIMENTAL_NATIVE_LLM: 'false', CADRE_BUDGET_OBSERVATION: '0', CADRE_TASK_ID: 'g1' });
  process.env.CADRE_RUN_ID = py(code, `from run_store import RunStore;s=RunStore();r=s.accept('lab','small task');` +
    `s.update(r['run_id'],budget_cap_usd=${cap});print(r['run_id'])`);
  execFileSync(process.env.CADRE_PYTHON, [join(code, 'lib', 'permission.py'), 'model-consent', process.env.CADRE_RUN_ID],
    { input: 'yes\nyes\n', env: { ...process.env, CADRE_RUN_ID: '' } });
  const lines = () => JSON.parse(execFileSync(process.env.CADRE_PYTHON, ['-c',
    "import sqlite3,json,sys;c=sqlite3.connect(sys.argv[1]);c.row_factory=sqlite3.Row;" +
    "print(json.dumps([dict(r) for r in c.execute('select * from reservation')]))",
    join(home, 'chain', 'request-budget.db')], { encoding: 'utf8' }));
  const record = () => JSON.parse(readFileSync(join(home, 'runs', process.env.CADRE_RUN_ID, 'run.json'), 'utf8'));
  return { home, code, lines, record };
}

const load = async (file) => (await import(pathToFileURL(file).href)).default();

test('operator permission: observation mode and environment answers cannot authorize transport', async () => {
  const f = home_dir(go);
  py(f.code, `from run_store import RunStore;RunStore().update('${process.env.CADRE_RUN_ID}',operator_model_permission='')`);
  let calls = 0;
  const config = { provider: { 'opencode-go': { options: { fetch: async () => { calls += 1; return response(); } } } } };
  const hooks = [await load(join(f.code, 'lib', 'cadre-plugin.mjs'))];
  await hooks[0].config(config);
  process.env.CADRE_APPROVAL = process.env.CADRE_APPROVAL_2 = 'yes';
  process.env.CADRE_BUDGET_OBSERVATION = '1';
  try {
    await assert.rejects(call(hooks, config, 'opencode-go', 'deepseek-v4-pro'), /operator model permission required/);
    assert.equal(calls, 0);
    const rows = py(f.code, "from pathlib import Path;import sqlite3;p=Path('" + join(f.home, 'chain', 'request-budget.db') + "');" +
      "c=sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True) if p.exists() else None;" +
      "print(c.execute('select count(*) from reservation').fetchone()[0] if c and c.execute(\"select 1 from sqlite_master where name='reservation'\").fetchone() else 0)");
    assert.equal(rows, '0');
  } finally {
    delete process.env.CADRE_APPROVAL;
    delete process.env.CADRE_APPROVAL_2;
    process.env.CADRE_BUDGET_OBSERVATION = '0';
  }
});

test('manual session: without CADRE_RUN_ID the fetch wrapper asks for no run permission, inside the run the same call is denied', async () => {
  const f = home_dir(go);
  const rid = process.env.CADRE_RUN_ID;
  py(f.code, `from run_store import RunStore;RunStore().update('${rid}',operator_model_permission='')`);
  let calls = 0;
  const config = { provider: { 'opencode-go': { options: { fetch: async () => { calls += 1; return response(); } } } } };
  const hooks = [await load(join(f.code, 'lib', 'cadre-plugin.mjs'))];
  await hooks[0].config(config);
  process.env.CADRE_RUN_ID = '';
  process.env.CADRE_BUDGET_OBSERVATION = '1';
  try {
    const res = await call(hooks, config, 'opencode-go', 'deepseek-v4-pro');
    assert.equal(res.status, 200);
    assert.equal(calls, 1, 'manual session reaches the provider without a run permission record');
    assert.equal(f.lines().filter((r) => r.run_id === rid).length, 0, 'manual session reserves nothing on the run');
    process.env.CADRE_RUN_ID = rid;
    await assert.rejects(call(hooks, config, 'opencode-go', 'deepseek-v4-pro'), /operator model permission required/);
    assert.equal(calls, 1, 'inside the run observation mode cannot bypass the permission gate');
  } finally {
    process.env.CADRE_RUN_ID = rid;
    process.env.CADRE_BUDGET_OBSERVATION = '0';
  }
});

// opencode: every plugin's config hook sees the same object, chat.params/chat.headers run in order per call.
async function call(hooks, config, provider, id, role = 'build', body = 'small task') {
  const m = catalog[provider].models[id];
  const input = { sessionID: 's1', agent: role, model: { providerID: provider, id, api: { id }, limit: m.limit, cost: m.cost } };
  const params = { maxOutputTokens: 32768 };
  for (const k of hooks) await k['chat.params']?.(input, params);
  const headers = { headers: {} };
  for (const k of hooks) await k['chat.headers']?.(input, headers);
  const res = await config.provider[provider].options.fetch('https://fixture.invalid/chat/completions', {
    method: 'POST', headers: headers.headers,
    body: JSON.stringify({ model: id, max_tokens: params.maxOutputTokens, messages: [{ role: 'user', content: body }] }) });
  await res.text();
  return res;
}

async function reviewEnvironment(task, role, work) {
  const saved = { CADRE_TASK_ID: process.env.CADRE_TASK_ID, CADRE_ROLE: process.env.CADRE_ROLE };
  Object.assign(process.env, { CADRE_TASK_ID: task, CADRE_ROLE: role });
  try { return await work(); } finally {
    for (const [key, value] of Object.entries(saved)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}

test('in the current plan review a small gap of the real body is transferred from the same total; preview and both ledgers agree', async () => {
  const f = home_dir({ or: { db: 'ok', pay: 50, available: 50, remaining: 50 } }, 2.1);
  writeFileSync(join(f.home, 'chain', 'openrouter-price.json'), JSON.stringify({ version: 2, ts: Date.now() / 1000, models: {
    'deepseek/deepseek-v4-pro': { input: 1.3, output: 2.6, providers: ['streamlake/fp8'], fp8_cache: true } } }));
  await reviewEnvironment('plan-review-1-abcdef01', 'review', async () => {
    py(f.code, `import os;from run_store import RunStore,atomic_json;from budget_gate import BudgetLedger,observed_budget;` +
      `s=RunStore();rid=os.environ['CADRE_RUN_ID'];s.save_plan(rid,{'job_id':'p1','tasks':[{'id':'g1','definition':'Read source','acceptance':'Source preserved','files':['README.md'],'model':'auto','difficulty':1,'parallel_group':0,'estimate_min':1,'estimate_usd':0.5,'dependent':[]}]});` +
      `r=s.read(rid);r['plan_review_claim']={'id':'owned','revision':1,'task_id':os.environ['CADRE_TASK_ID']};atomic_json(s.path(rid)/'run.json',r);` +
      `l=BudgetLedger(s.home/'chain/request-budget.db');i=l.reserve('openrouter',200200,observed_budget(s.home,__import__('pathlib').Path(os.environ['CADRE_ENGINE_ROOT'])),` +
      `{'run_id':rid,'phase_role':'review','task_id':os.environ['CADRE_TASK_ID']},210000,'helper');l.settle(i,4000,i)`);
    const before = readFileSync(join(f.home, 'chain', 'request-budget.db'));
    assert.equal(py(f.code, `import os;from pathlib import Path;from budget_gate import BudgetLedger,observed_budget;l=BudgetLedger(Path(os.environ['CADRE_HOME'])/'chain/request-budget.db');` +
      `print(l.available('openrouter',observed_budget(Path(os.environ['CADRE_HOME']),Path(os.environ['CADRE_ENGINE_ROOT'])),210000,os.environ['CADRE_RUN_ID'],'helper',206900,'review',read_only=True))`), '206900');
    assert.deepEqual(readFileSync(join(f.home, 'chain', 'request-budget.db')), before);
    let calls = 0;
    const config = { provider: { openrouter: { options: { fetch: async () => {
      calls += 1; return response({ prompt_tokens: 1000, completion_tokens: 100, cost: 0.004 }); } } } } };
    const hooks = [await load(join(f.code, 'lib', 'cadre-plugin.mjs'))];
    await hooks[0].config(config);
    await call(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro', 'build', 'x'.repeat(90000));
    assert.equal(calls, 1);
    const last = f.lines().at(-1);
    const caps = JSON.parse(py(f.code, `import sqlite3,json,os;c=sqlite3.connect(os.environ['CADRE_HOME']+'/chain/request-budget.db');print(c.execute('select caps from role_allocation').fetchone()[0])`));
    assert.ok(last.reserved > 206000);
    assert.equal(caps.helper, 4000 + last.reserved);
    assert.equal(caps.judge, 315000);
    assert.equal(Object.values(caps).reduce((a, b) => a + b, 0), 2100000);
    assert.equal(last.actual, 4000);
    assert.equal(JSON.parse(last.metadata).review_phase, 'plan-review');
  });
});

const writerLimit = (f) => JSON.parse(readFileSync(join(f.code, 'lib', 'limits.json'), 'utf8')).review['review-author'];

test('the review round cap holds in the real plugin across new tasks and HTTP retries; a denial holds no money in either ledger', async () => {
  const f = home_dir({ or: { db: 'ok', pay: 50, available: 50, remaining: 50 } }, 10);
  writeFileSync(join(f.home, 'chain', 'openrouter-price.json'), JSON.stringify({ version: 2, ts: Date.now() / 1000, models: {
    'deepseek/deepseek-v4-pro': { input: 1.3, output: 2.6, providers: ['streamlake/fp8'], fp8_cache: true } } }));
  await reviewEnvironment('review-author-abcdef012345', 'impl', async () => {
    let calls = 0;
    const config = { provider: { openrouter: { options: { fetch: async () => {
      calls += 1; return response({ prompt_tokens: 1000, completion_tokens: 100, cost: 0.0012 }); } } } } };
    const hooks = [await load(join(f.code, 'lib', 'cadre-plugin.mjs'))];
    await hooks[0].config(config);
    const { request } = writerLimit(f);
    for (let n = 0; n < request; n++) await call(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro');
    process.env.CADRE_TASK_ID = 'review-author-eeeeeeeeeeee';
    await assert.rejects(call(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro'), /CADRE_BUDGET_DENIED/);
    assert.equal(calls, request);
    assert.equal(f.lines().length, request);
    assert.equal(py(f.code, `import sqlite3,os;c=sqlite3.connect(os.environ['CADRE_HOME']+'/chain/request-budget.db');print(c.execute('select calls from review_control').fetchone()[0])`), String(request));
    assert.ok(f.record().denials.some((d) => String(d.reason).includes('review-author: requests')));
    const monthly = JSON.parse(py(f.code, `import sqlite3,json,os;c=sqlite3.connect(os.environ['CADRE_HOME']+'/chain/monthly-budget.db');c.row_factory=sqlite3.Row;print(json.dumps([dict(r) for r in c.execute('select * from reservation')]))`));
    assert.equal(monthly.filter((r) => r.actual === null).length, 0);
    assert.equal(monthly.filter((r) => r.actual > 0).length, request);
  });
});

test('after a repair the tool cap still applies to every tool call in the judge phase', async () => {
  const f = home_dir(go, 10);
  await reviewEnvironment('review-recheck-abcdef012345', 'quality', async () => {
    const hooks = await load(join(f.code, 'lib', 'budget-plugin.mjs'));
    const { tool } = JSON.parse(readFileSync(join(f.code, 'lib', 'limits.json'), 'utf8')).review['review-recheck'];
    for (let n = 0; n < tool; n++) await hooks['tool.execute.before']({ tool: 'todoread' }, { args: {} });
    await assert.rejects(hooks['tool.execute.before']({ tool: 'todoread' }, { args: {} }), /CADRE_TOOL_LIMIT/);
    assert.ok(f.record().denials.some((d) => String(d.reason).includes('review-recheck: tools')));
  });
});

test('the review tool cap is independent of the prompt text and a new task does not reset the counter', async () => {
  const f = home_dir(go, 10);
  await reviewEnvironment('review-author-abcdef012345', 'impl', async () => {
    const hooks = await load(join(f.code, 'lib', 'budget-plugin.mjs'));
    const { tool } = writerLimit(f);
    for (let n = 0; n < tool; n++) await hooks['tool.execute.before']({ tool: 'todoread' }, { args: {} });
    process.env.CADRE_TASK_ID = 'review-author-000000000002';
    await assert.rejects(hooks['tool.execute.before']({ tool: 'todoread' }, { args: {} }), /CADRE_TOOL_LIMIT/);
    assert.equal(py(f.code, `import sqlite3,os;c=sqlite3.connect(os.environ['CADRE_HOME']+'/chain/request-budget.db');print(c.execute('select tools from review_control').fetchone()[0])`), String(tool));
    assert.ok(f.record().denials.some((d) => String(d.reason).includes('review-author: tools')));
    assert.ok(!f.record().denials.some((d) => String(d.reason).includes('ledger refused')), 'tool caps do not record a general budget rejection');
    assert.equal(f.lines().length, 0);
  });
});

test('the review cost and time caps cut before the real fetch; pending and monthly shares are kept', async () => {
  const f = home_dir({ or: { db: 'ok', pay: 50, available: 50, remaining: 50 } }, 1, true);
  writeFileSync(join(f.home, 'chain', 'openrouter-price.json'), JSON.stringify({ version: 2, ts: Date.now() / 1000, models: {
    'deepseek/deepseek-v4-pro': { input: 1.3, output: 2.6, providers: ['streamlake/fp8'], fp8_cache: true } } }));
  await reviewEnvironment('review-author-abcdef012345', 'impl', async () => {
    let calls = 0;
    const config = { provider: { openrouter: { options: { fetch: async () => {
      calls += 1; return response({ prompt_tokens: 1000, completion_tokens: 100, cost: 0.1 }); } } } } };
    const hooks = [await load(join(f.code, 'lib', 'cadre-plugin.mjs'))];
    await hooks[0].config(config);
    await call(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro', 'build', 'x'.repeat(30000));
    const ipc = JSON.parse(readFileSync(join(f.home, 'budget-ipc.jsonl'), 'utf8').trim());
    assert.ok(!ipc.keys.includes('payload'), 'unused prompt body must stay outside ledger IPC');
    assert.ok(ipc.bytes < 4096, `ledger request ${ipc.bytes} bytes`);
    await assert.rejects(call(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro', 'build', 'x'.repeat(30000)), /CADRE_BUDGET_DENIED/);
    assert.equal(calls, 1);
    assert.ok(f.record().denials.some((d) => String(d.reason).includes('review-author: cost')),
      JSON.stringify({ denials: f.record().denials, rows: f.lines().map(({ reserved, actual }) => ({ reserved, actual })) }));
    const duration = writerLimit(f).duration_sec;
    py(f.code, `import sqlite3,os,time;c=sqlite3.connect(os.environ['CADRE_HOME']+'/chain/request-budget.db');t=int(time.time())-${duration + 1};c.execute('update review_control set started=?,deadline=?',(t,t+${duration}));c.commit()`);
    process.env.CADRE_TASK_ID = 'review-author-ffffffffffff';
    await assert.rejects(call(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro'), /CADRE_BUDGET_DENIED/);
    assert.equal(calls, 1);
    assert.ok(f.record().denials.some((d) => String(d.reason).includes('review-author: time')));
    assert.equal(f.lines().length, 1);
    const monthly = JSON.parse(py(f.code, `import sqlite3,json,os;c=sqlite3.connect(os.environ['CADRE_HOME']+'/chain/monthly-budget.db');c.row_factory=sqlite3.Row;print(json.dumps([dict(r) for r in c.execute('select * from reservation')]))`));
    assert.equal(monthly.filter((r) => r.actual === null).length, 0);
    assert.equal(monthly.filter((r) => r.actual > 0).length, 1);
  });
});

test('double gate: even if the same gate loads twice (cadre-plugin + budget-plugin, live + repo root) the call passes once', async () => {
  // 06.10 smoke: the global config loaded cadre-plugin.mjs, the run config loaded budget-plugin.mjs; the header token was not in the other copy.
  for (const second of ['same-root-budget-plugin', 'other-root-cadre-plugin']) {
    const f = home_dir(go);
    const secondFile = second === 'same-root-budget-plugin' ? join(f.code, 'lib', 'budget-plugin.mjs')
      : join(codeRoot(f.home, 'code2', go), 'lib', 'cadre-plugin.mjs');
    let net = 0;
    const config = { enabled_providers: ['lmstudio'], provider: { lmstudio: { options: {} },
      'opencode-go': { options: { fetch: async () => { net += 1; return response(); } } } } };
    const hooks = [await load(join(f.code, 'lib', 'cadre-plugin.mjs')), await load(secondFile)];
    for (const k of hooks) await k.config?.(config);
    assert.ok(!config.provider.lmstudio && !config.enabled_providers.includes('lmstudio'), 'a local provider in old live config is not enabled (06.10)');
    const res = await call(hooks, config, 'opencode-go', 'deepseek-v4-pro');
    assert.equal(res.status, 200, second);
    assert.equal(net, 1, `${second}: one request reaches the provider`);
    const line = f.lines().filter((r) => r.provider === 'opencode-go');
    assert.equal(line.length, 1, `${second}: single reservation (no double counting)`);
    assert.notEqual(line[0].actual, null, `${second}: settled with a receipt`);
  }
});

test('small cap: if the pocket is not enough at the real catalogue price there is no network call, and the denial is written to the run record with the provider', async () => {
  const f = home_dir(go, 0.05);   // lane pocket 75% = $0.0375; deepseek-v4-pro output cap 32768 x $1.98/M = $0.065
  let net = 0;
  const config = { enabled_providers: [], provider: { 'opencode-go': { options: { fetch: async () => { net += 1; return response(); } } } } };
  const hooks = [await load(join(f.code, 'lib', 'cadre-plugin.mjs'))];
  await hooks[0].config(config);
  await assert.rejects(call(hooks, config, 'opencode-go', 'deepseek-v4-pro'), /CADRE_BUDGET_DENIED/);
  assert.equal(net, 0, 'denial occurs before network access');
  assert.ok((f.record().denials ?? []).some((d) => d.provider === 'opencode-go'), 'Record the denial with its provider');
  assert.equal(f.lines().filter((r) => r.provider === 'opencode-go' && r.actual === null).length, 0, 'no pending reservation remains after denial');

  py(f.code, `from run_store import RunStore;RunStore().update('${process.env.CADRE_RUN_ID}',budget_cap_usd=2)`);
  execFileSync(process.env.CADRE_PYTHON, [join(f.code, 'lib', 'permission.py'), 'model-consent', process.env.CADRE_RUN_ID],
    { input: 'yes\nyes\n', env: { ...process.env, CADRE_RUN_ID: '' } });
  const res = await call(hooks, config, 'opencode-go', 'deepseek-v4-pro');
  assert.equal(res.status, 200);
  const line = f.lines().filter((r) => r.provider === 'opencode-go' && r.actual !== null).at(-1);
  // receipt with the real catalogue rates: 1000 x 0.66 + 100 x 1.98 = 858 micro-dollars; Go DeepSeek 2× at peak hours (4c-13)
  const dense = (() => { const d = new Date(), g = d.getUTCDay(), s = d.getUTCHours();
    return g >= 1 && g <= 5 && ((s >= 1 && s < 4) || (s >= 6 && s < 10)); })();
  assert.equal(line.actual, 858 * (dense ? 2 : 1));

  // (06.10 note): a Go lane reservation is priced by the request body, not by the model's 1M context: a 100K-character
  // request ~$0.134, always 2× at the peak price on DeepSeek (it would be $1.45 with a 1M context). With a small cap the pocket rule is what removes Go.
  const once = f.lines().length;
  assert.equal((await call(hooks, config, 'opencode-go', 'deepseek-v4-pro', 'build', 'x'.repeat(100000))).status, 200);
  const fresh = f.lines().slice(once).filter((r) => r.provider === 'opencode-go');
  assert.equal(fresh.length, 1);
  assert.ok(fresh[0].reserved > 260000 && fresh[0].reserved < 280000, `reservation ${fresh[0].reserved} microdollars`);
});

test('route hand-off: when Go closes the OR route of the same model is chosen and the plugin pins it and settles with the real receipt', async () => {
  const observation = { go: { ...go.go, window_open: false }, or: { db: 'ok', pay: 5, available: 5, remaining: 5 } };
  const f = home_dir(observation);
  const hand_over = () => py(f.code, "import qe_core;print(qe_core.path_hand_over('opencode-go/deepseek-v4-pro','" +
    join(f.home, 'handover.log') + "','Go limit') or 'WAIT')");
  assert.equal(hand_over(), 'WAIT', 'without an OR price observation, do not offer a route the plugin would reject');
  writeFileSync(join(f.home, 'chain', 'openrouter-price.json'), JSON.stringify({ version: 2, ts: Date.now() / 1000, models: {
    'deepseek/deepseek-v4-pro': { input: 1.3, output: 2.6, providers: ['streamlake/fp8', 'deepinfra/fp8'], fp8_cache: true } } }));
  assert.equal(hand_over(), 'openrouter/deepseek/deepseek-v4-pro');

  let body;
  const config = { enabled_providers: [], provider: { openrouter: { options: { fetch: async (_u, o) => {
    body = JSON.parse(o.body); return response({ prompt_tokens: 1000, completion_tokens: 100, cost: 0.0012 }); } } } } };
  const hooks = [await load(join(f.code, 'lib', 'cadre-plugin.mjs'))];
  await hooks[0].config(config);
  const res = await call(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro');
  assert.equal(res.status, 200);
  assert.deepEqual(body.provider.only, ['streamlake/fp8', 'deepinfra/fp8']);
  assert.deepEqual(body.provider.quantizations, ['fp8']);
  const line = f.lines().filter((r) => r.provider === 'openrouter').at(-1);
  assert.equal(line.actual, 1200, 'The receipt uses provider usage.cost, not an estimate');
});

test('title agent: if the SDK retries with the same header both gates pass it; a 429 leaves no obligation (4c-4)', async () => {
  // 07.10 05:15: the monthly gate deleted the token on the first attempt, and the SDK's second attempt with the same header got 'monthly request context missing'.
  const f = home_dir({ or: { db: 'ok', pay: 5, available: 5, remaining: 5 } });
  writeFileSync(join(f.home, 'chain', 'openrouter-price.json'), JSON.stringify({ version: 2, ts: Date.now() / 1000, models: {
    'deepseek/deepseek-v4-pro': { input: 1.3, output: 2.6, providers: ['streamlake/fp8', 'deepinfra/fp8'], fp8_cache: true } } }));
  let n = 0;
  const config = { enabled_providers: [], provider: { openrouter: { options: { fetch: async () => (++n === 1
    ? new Response('{"error":{"code":429}}', { status: 429, headers: { 'content-type': 'application/json' } })
    : response({ prompt_tokens: 1000, completion_tokens: 100, cost: 0.0012 })) } } } };
  const hooks = [await load(join(f.code, 'lib', 'cadre-plugin.mjs'))];
  await hooks[0].config(config);
  const id = 'deepseek/deepseek-v4-pro', m = catalog.openrouter.models[id];
  const input = { sessionID: 's-title', agent: 'title', model: { providerID: 'openrouter', id, api: { id }, limit: m.limit, cost: m.cost } };
  const params = { maxOutputTokens: 1000 };
  for (const k of hooks) await k['chat.params']?.(input, params);
  const headers = { headers: {} };
  for (const k of hooks) await k['chat.headers']?.(input, headers);
  const request = () => config.provider.openrouter.options.fetch('https://fixture.invalid/chat/completions', { method: 'POST',
    headers: headers.headers, body: JSON.stringify({ model: id, max_tokens: params.maxOutputTokens, messages: [{ role: 'user', content: 'title' }] }) });
  assert.equal((await request()).status, 429);
  const second = await request();
  await second.text();
  assert.equal(second.status, 200, 'A second attempt with the same header passes');
  const monthly = JSON.parse(execFileSync(process.env.CADRE_PYTHON, ['-c',
    "import sqlite3,json,sys;c=sqlite3.connect(sys.argv[1]);c.row_factory=sqlite3.Row;" +
    "print(json.dumps([dict(r) for r in c.execute('select * from reservation')]))",
    join(f.home, 'chain', 'monthly-budget.db')], { encoding: 'utf8' }));
  for (const [entry_name, line] of [['provider', f.lines()], ['monthly', monthly]]) {
    assert.equal(line.filter((r) => r.actual === null).length, 0, `${entry_name}: no pending liability`);
    assert.ok(line.some((r) => r.actual === 0 && r.receipt.endsWith(':http-429')), `${entry_name}: The first attempt failure is recorded`);
    assert.equal(line.filter((r) => r.actual > 0).length, 1, `${entry_name}: single receipt`);
  }
});

test('Without OpenRouter response cost, both ledgers retain the reservation and the monthly ledger does not settle an estimate', async () => {
  const f = home_dir({ or: { db: 'ok', pay: 5, available: 5, remaining: 5 } });
  writeFileSync(join(f.home, 'chain', 'openrouter-price.json'), JSON.stringify({ version: 2, ts: Date.now() / 1000, models: {
    'deepseek/deepseek-v4-pro': { input: 1.3, output: 2.6, providers: ['streamlake/fp8', 'deepinfra/fp8'], fp8_cache: true } } }));
  let netTitles;
  const config = { enabled_providers: [], provider: { openrouter: { options: { fetch: async (url, o) => {
    netTitles = new Headers(o.headers); return response({ prompt_tokens: 1000, completion_tokens: 100 }); } } } } };
  const hooks = [await load(join(f.code, 'lib', 'cadre-plugin.mjs'))];
  await hooks[0].config(config);
  const id = 'deepseek/deepseek-v4-pro', m = catalog.openrouter.models[id];
  const input = { sessionID: 's-no-cost', agent: 'title', model: { providerID: 'openrouter', id, api: { id }, limit: m.limit, cost: m.cost } };
  const params = { maxOutputTokens: 1000 };
  for (const k of hooks) await k['chat.params']?.(input, params);
  const headers = { headers: {} };
  for (const k of hooks) await k['chat.headers']?.(input, headers);
  const answer = await config.provider.openrouter.options.fetch('https://fixture.invalid/chat/completions', { method: 'POST',
    headers: headers.headers, body: JSON.stringify({ model: id, max_tokens: params.maxOutputTokens, messages: [{ role: 'user', content: 'title' }] }) });
  await answer.text();
  const monthly = JSON.parse(execFileSync(process.env.CADRE_PYTHON, ['-c',
    "import sqlite3,json,sys;c=sqlite3.connect(sys.argv[1]);c.row_factory=sqlite3.Row;" +
    "print(json.dumps([dict(r) for r in c.execute('select * from reservation')]))",
    join(f.home, 'chain', 'monthly-budget.db')], { encoding: 'utf8' }));
  for (const [entry_name, line] of [['provider', f.lines()], ['monthly', monthly]]) {
    assert.equal(line.length, 1, `${entry_name}: single reservation`);
    assert.equal(line[0].actual, null, `${entry_name}: an unknown expense cannot be settled`);
  }
  assert.equal(JSON.parse(f.lines()[0].metadata).monthly_id, monthly[0].id, 'The provider record carries the matching monthly entry id');
  assert.equal(netTitles.get('x-cadre-monthly-id'), null, 'the correlation header never reaches the network');
});

test('real HTTP: speed warning continues, explicit alarm approval preserves monthly and run limits', async () => {
  const previousCap = process.env.CADRE_RUN_CAP_USD;
  const previousMonthly = process.env.CADRE_MONTHLY_CAP_USD;
  process.env.CADRE_RUN_CAP_USD = '4';
  process.env.CADRE_MONTHLY_CAP_USD = '4';
  let server;
  try {
    const f = home_dir({ or: { db: 'ok', pay: 50, available: 50, remaining: 50 } }, 4);
    const rid = process.env.CADRE_RUN_ID;
    writeFileSync(join(f.home, 'chain', 'openrouter-price.json'), JSON.stringify({ version: 2, ts: Date.now() / 1000, models: {
      'deepseek/deepseek-v4-pro': { input: 1.3, output: 2.6, providers: ['deepinfra/fp8'], fp8_cache: true } } }));
    py(f.code, `from run_store import RunStore;from pathlib import Path;import budget_gate as b;` +
      `s=RunStore();s.update('${rid}',budget_manual=True);` +
      `l=b.BudgetLedger(s.home/'chain/request-budget.db');` +
      `i=l.reserve('openrouter',1700000,b.observed_budget(s.home,Path('${f.code}')),{'run_id':'${rid}'});l.settle(i,1700000,i);` +
      `b.progress_write(s.home,'${rid}','qa-pass','accepted-revision')`);
    let requests = 0;
    server = createServer((req, res) => {
      requests += 1;
      const row = f.lines().at(-1);
      assert.equal(row.actual, null, 'provider hold exists before HTTP');
      assert.ok(JSON.parse(row.metadata).monthly_id, 'monthly twin exists before HTTP');
      assert.equal(req.headers['x-cadre-monthly-id'], undefined);
      assert.equal(req.headers['x-cadre-budget-context'], undefined);
      req.resume();
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ id: 'local-receipt-' + requests, usage: { prompt_tokens: 1000, completion_tokens: 100, cost: 0.0012 } }));
    });
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
    const url = `http://127.0.0.1:${server.address().port}/chat/completions`;
    const config = { enabled_providers: [], provider: { openrouter: { options: { fetch: (_u, opts) => fetch(url, opts) } } } };
    const hooks = [await load(join(f.code, 'lib', 'cadre-plugin.mjs'))];
    await hooks[0].config(config);
    await call(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro');
    assert.equal(f.record().spend_warning.progress, 'productive');
    assert.equal(f.record().spend_alarm, undefined);
    py(f.code, `from run_store import RunStore;import time;RunStore().update('${rid}',spend_alarm={'ts':time.time(),'reason':'explicit stop'})`);
    await assert.rejects(call(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro'), /CADRE_BUDGET_DENIED/);
    assert.equal(requests, 1, 'explicit alarm opens no HTTP request');
    py(f.code, `from run_store import RunStore,approve_spending;approve_spending(RunStore(),'${rid}')`);
    process.env.CADRE_MONTHLY_CAP_USD = '0.01';
    await assert.rejects(call(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro'), /CADRE_BUDGET_DENIED/);
    assert.equal(requests, 1, 'alarm approval cannot override monthly cap');
    process.env.CADRE_MONTHLY_CAP_USD = '4';
    await call(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro');
    assert.equal(requests, 2, 'approved call completes through both gates');
    py(f.code, `from run_store import RunStore;RunStore().update('${rid}',budget_cap_usd=0)`);
    await assert.rejects(call(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro'), /CADRE_BUDGET_DENIED/);
    assert.equal(requests, 2, 'alarm approval cannot override run cap');
    assert.equal(f.lines().filter(r => r.actual === null).length, 0);
    assert.equal(py(f.code, `from monthly_budget import MonthlyLedger;from pathlib import Path;` +
      `print(MonthlyLedger(Path('${f.home}')/'chain/monthly-budget.db').status()['held_micro'])`), '0');
  } finally {
    if (server) await new Promise(resolve => server.close(resolve));
    if (previousCap === undefined) delete process.env.CADRE_RUN_CAP_USD; else process.env.CADRE_RUN_CAP_USD = previousCap;
    if (previousMonthly === undefined) delete process.env.CADRE_MONTHLY_CAP_USD; else process.env.CADRE_MONTHLY_CAP_USD = previousMonthly;
  }
});

test('real composed gate: an over-bound cash receipt blocks the same project on the subscription path', async () => {
  const previousCap = process.env.CADRE_RUN_CAP_USD;
  process.env.CADRE_RUN_CAP_USD = '4';
  try {
    const f = home_dir({ ...go, or: { db: 'ok', pay: 50, available: 50, remaining: 50 } }, 4);
    const rid = process.env.CADRE_RUN_ID;
    writeFileSync(join(f.home, 'chain', 'openrouter-price.json'), JSON.stringify({ version: 2, ts: Date.now() / 1000, models: {
      'deepseek/deepseek-v4-pro': { input: 1.3, output: 2.6, providers: ['deepinfra/fp8'], fp8_cache: true } } }));
    py(f.code, `from run_store import RunStore;from project_mode import project_ref_budget_pin;project_ref_budget_pin(RunStore(),'${rid}')`);
    let calls = 0;
    const fetcher = async () => { calls += 1; return response({ prompt_tokens: 1000, completion_tokens: 100, cost: 0.8 }); };
    const config = { enabled_providers: [], provider: { openrouter: { options: { fetch: fetcher } },
      'opencode-go': { options: { fetch: fetcher } } } };
    const hooks = [await load(join(f.code, 'lib', 'cadre-plugin.mjs'))];
    await hooks[0].config(config);
    await call(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro');
    const row = f.lines().at(-1);
    assert.equal(row.actual, 800000, 'unexpected cost remains visible');
    assert.ok(JSON.parse(row.metadata).verified_bound_micro < row.actual);
    py(f.code, `from run_store import RunStore,approve_spending;approve_spending(RunStore(),'${rid}')`);
    await assert.rejects(call(hooks, config, 'opencode-go', 'deepseek-v4-pro'), /CADRE_BUDGET_DENIED/);
    assert.equal(calls, 1, 'provider change cannot bypass the project overrun');
    assert.equal(f.lines().length, 1);
  } finally {
    if (previousCap === undefined) delete process.env.CADRE_RUN_CAP_USD; else process.env.CADRE_RUN_CAP_USD = previousCap;
  }
});

const OUTAGE = { code: 502, message: 'Flex processing is temporarily unavailable. Please try again later or use standard processing.',
  metadata: { error_type: 'provider_unavailable' } };
const outageSse = (n) => new Response(`data: ${JSON.stringify({ id: `gen-1792404912-outage${n}`, error: OUTAGE })}\n\n`,
  { headers: { 'content-type': 'text/event-stream' } });
const outageHttp = () => new Response(JSON.stringify({ error: OUTAGE }), { status: 502, headers: { 'content-type': 'application/json' } });
const dsPrice = (f) => writeFileSync(join(f.home, 'chain', 'openrouter-price.json'), JSON.stringify({ version: 2, ts: Date.now() / 1000,
  models: { 'deepseek/deepseek-v4-pro': { input: 1.3, output: 2.6, providers: ['streamlake/fp8'], fp8_cache: true } } }));
const pathStatus = (f) => JSON.parse(readFileSync(join(f.home, 'chain', 'provider-status.json'), 'utf8'));

for (const [format, responseProduce] of [['SSE', outageSse], ['HTTP', outageHttp]]) {
  test(`${format} provider_unavailable reaches the breaker before an OpenCode retry; a closed route creates no new uncertain reservation`, async () => {
    const f = home_dir({ or: { db: 'ok', pay: 50, available: 50, remaining: 50 } }, 2);
    dsPrice(f);
    let calls = 0;
    const config = { provider: { openrouter: { options: { fetch: async () => { calls += 1; return responseProduce(calls); } } } } };
    const hooks = [await load(join(f.code, 'lib', 'cadre-plugin.mjs'))];
    await hooks[0].config(config);
    await call(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro');
    const [first] = f.lines();
    assert.equal(first.actual, null, 'an unknown amount retains the reservation; a 502 is not zero cost');
    assert.equal(JSON.parse(first.metadata).provider_error.round, 'provider_unavailable');
    const status = pathStatus(f);
    const modelStatus = status['openrouter/deepseek/deepseek-v4-pro'];
    assert.ok(modelStatus.endpoints['streamlake/fp8'].cooldown_until > Date.now() / 1000, 'the failed endpoint closes before a retry');
    assert.ok(!(modelStatus.cooldown_until > Date.now() / 1000), 'An endpoint outage does not close other model endpoints');
    assert.ok(!(status.openrouter?.cooldown_until > Date.now() / 1000), 'Do not close the whole provider');
    await assert.rejects(call(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro'), /CADRE_BUDGET_DENIED/);
    assert.equal(calls, 1, 'a closed route receives no second request');
    assert.equal(f.lines().length, 1, 'reservations cannot accumulate on a closed route');
    assert.ok(f.record().denials.some((d) => String(d.reason).startsWith('provider unavailable: openrouter/deepseek/deepseek-v4-pro')));
  });
}

test('a general 5xx error keeps the reserve but does not close the route on a single error; 429 keeps its old behaviour', async () => {
  const f = home_dir({ or: { db: 'ok', pay: 50, available: 50, remaining: 50 } }, 2);
  dsPrice(f);
  let calls = 0;
  const config = { provider: { openrouter: { options: { fetch: async () => {
    calls += 1;
    return calls === 1 ? new Response('{"error":{"code":500,"message":"upstream"}}', { status: 500, headers: { 'content-type': 'application/json' } })
      : new Response('{"error":{"code":429,"message":"rate limit"}}', { status: 429, headers: { 'content-type': 'application/json' } });
  } } } } };
  const hooks = [await load(join(f.code, 'lib', 'cadre-plugin.mjs'))];
  await hooks[0].config(config);
  await call(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro');
  await call(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro');
  assert.equal(calls, 2);
  const [general, quota] = f.lines();
  assert.equal(general.actual, null);
  assert.deepEqual([quota.actual, quota.receipt.endsWith(':http-429')], [0, true]);
  assert.ok(!(pathStatus(f)['openrouter/deepseek/deepseek-v4-pro']?.cooldown_until > Date.now() / 1000));
});
