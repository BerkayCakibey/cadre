// Real-plugin integration tests (07.10): the real cadre-plugin.mjs + the real Python gate/ledger/monthly pool
// + a real catalogue record (veri/katalog-20261007.json, from opencode models.json) + a fake HTTP endpoint. The only fake part is the provider observation.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { cpSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { execFileSync } from 'node:child_process';
import { createServer } from 'node:http';

const LIB = fileURLToPath(new URL('../lib/', import.meta.url));
const KATALOG = fileURLToPath(new URL('./veri/katalog-20261007.json', import.meta.url));
const katalog = JSON.parse(readFileSync(KATALOG, 'utf8'));
process.env.CADRE_PYTHON ||= 'python3';
const gecici = mkdtempSync(join(tmpdir(), 'qe-entegrasyon-'));
after(() => rmSync(gecici, { recursive: true, force: true }));
let sira = 0;

const go = { go: { db: 'ok', birim: 'puan', pay: 200, kullanilabilir: 200, pencere_acik: true } };   // 4c-13: points ($30, the $15 model)
const yanit = (usage = { prompt_tokens: 1000, completion_tokens: 100 }) =>
  new Response(JSON.stringify({ id: 'makbuz', usage }), { headers: { 'content-type': 'application/json' } });

function kodKoku(home, ad, gozlem, inspectIpc = false) {
  const code = join(home, ad);
  cpSync(LIB, join(code, 'lib'), { recursive: true, filter: (p) => !p.includes('__pycache__') });
  writeFileSync(join(code, 'lib', 'butce.py'), `import json,datetime\nd=json.loads(${JSON.stringify(JSON.stringify(gozlem))})\n` +
    `d.update(ts=datetime.datetime.now().isoformat(),bugun=datetime.date.today().isoformat())\nprint(json.dumps(d))\n`);
  if (inspectIpc) {
    const gate = join(code, 'lib', 'butce_kapisi.py');
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

function py(code, kod) {
  return execFileSync(process.env.CADRE_PYTHON, ['-c', `import sys;sys.path.insert(0,sys.argv[1]);${kod}`, join(code, 'lib')],
    { encoding: 'utf8', env: process.env }).trim();
}

function ev(gozlem, tavan = 2, inspectIpc = false) {
  const home = join(gecici, String(++sira));
  mkdirSync(join(home, 'zincir'), { recursive: true });
  const code = kodKoku(home, 'code', gozlem, inspectIpc);
  Object.assign(process.env, { CADRE_HOME: home, CADRE_ENGINE_ROOT: code, CADRE_MODEL_KATALOG: KATALOG,
    OPENCODE_EXPERIMENTAL_NATIVE_LLM: 'false', CADRE_BUTCE_GOZLEM: '0', CADRE_GOREV_ID: 'g1' });
  process.env.CADRE_RUN_ID = py(code, `from kosu import RunStore;s=RunStore();r=s.accept('lab','kucuk is');` +
    `s.update(r['run_id'],budget_cap_usd=${tavan});print(r['run_id'])`);
  execFileSync(process.env.CADRE_PYTHON, [join(code, 'lib', 'izin.py'), 'model-consent', process.env.CADRE_RUN_ID],
    { input: 'evet\nevet\n', env: { ...process.env, CADRE_RUN_ID: '' } });
  const satirlar = () => JSON.parse(execFileSync(process.env.CADRE_PYTHON, ['-c',
    "import sqlite3,json,sys;c=sqlite3.connect(sys.argv[1]);c.row_factory=sqlite3.Row;" +
    "print(json.dumps([dict(r) for r in c.execute('select * from reservation')]))",
    join(home, 'zincir', 'request-budget.db')], { encoding: 'utf8' }));
  const kayit = () => JSON.parse(readFileSync(join(home, 'runs', process.env.CADRE_RUN_ID, 'run.json'), 'utf8'));
  return { home, code, satirlar, kayit };
}

const yukle = async (dosya) => (await import(pathToFileURL(dosya).href)).default();

test('operator permission: observation mode and environment answers cannot authorize transport', async () => {
  const f = ev(go);
  py(f.code, `from kosu import RunStore;RunStore().update('${process.env.CADRE_RUN_ID}',operator_model_permission='')`);
  let calls = 0;
  const config = { provider: { 'opencode-go': { options: { fetch: async () => { calls += 1; return yanit(); } } } } };
  const hooks = [await yukle(join(f.code, 'lib', 'cadre-plugin.mjs'))];
  await hooks[0].config(config);
  process.env.CADRE_ONAY = process.env.CADRE_ONAY_2 = 'evet';
  process.env.CADRE_BUTCE_GOZLEM = '1';
  try {
    await assert.rejects(cagir(hooks, config, 'opencode-go', 'deepseek-v4-pro'), /operator model permission required/);
    assert.equal(calls, 0);
    const rows = py(f.code, "from pathlib import Path;import sqlite3;p=Path('" + join(f.home, 'zincir', 'request-budget.db') + "');" +
      "c=sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True) if p.exists() else None;" +
      "print(c.execute('select count(*) from reservation').fetchone()[0] if c and c.execute(\"select 1 from sqlite_master where name='reservation'\").fetchone() else 0)");
    assert.equal(rows, '0');
  } finally {
    delete process.env.CADRE_ONAY;
    delete process.env.CADRE_ONAY_2;
    process.env.CADRE_BUTCE_GOZLEM = '0';
  }
});

test('manual session: without CADRE_RUN_ID the fetch wrapper asks for no run permission, inside the run the same call is denied', async () => {
  const f = ev(go);
  const rid = process.env.CADRE_RUN_ID;
  py(f.code, `from kosu import RunStore;RunStore().update('${rid}',operator_model_permission='')`);
  let calls = 0;
  const config = { provider: { 'opencode-go': { options: { fetch: async () => { calls += 1; return yanit(); } } } } };
  const hooks = [await yukle(join(f.code, 'lib', 'cadre-plugin.mjs'))];
  await hooks[0].config(config);
  process.env.CADRE_RUN_ID = '';
  process.env.CADRE_BUTCE_GOZLEM = '1';
  try {
    const res = await cagir(hooks, config, 'opencode-go', 'deepseek-v4-pro');
    assert.equal(res.status, 200);
    assert.equal(calls, 1, 'manual session reaches the provider without a run permission record');
    assert.equal(f.satirlar().filter((r) => r.run_id === rid).length, 0, 'manual session reserves nothing on the run');
    process.env.CADRE_RUN_ID = rid;
    await assert.rejects(cagir(hooks, config, 'opencode-go', 'deepseek-v4-pro'), /operator model permission required/);
    assert.equal(calls, 1, 'inside the run observation mode cannot bypass the permission gate');
  } finally {
    process.env.CADRE_RUN_ID = rid;
    process.env.CADRE_BUTCE_GOZLEM = '0';
  }
});

// opencode: every plugin's config hook sees the same object, chat.params/chat.headers run in order per call.
async function cagir(kancalar, config, saglayici, id, rol = 'build', govde = 'kucuk is') {
  const m = katalog[saglayici].models[id];
  const input = { sessionID: 's1', agent: rol, model: { providerID: saglayici, id, api: { id }, limit: m.limit, cost: m.cost } };
  const params = { maxOutputTokens: 32768 };
  for (const k of kancalar) await k['chat.params']?.(input, params);
  const headers = { headers: {} };
  for (const k of kancalar) await k['chat.headers']?.(input, headers);
  const res = await config.provider[saglayici].options.fetch('https://fixture.invalid/chat/completions', {
    method: 'POST', headers: headers.headers,
    body: JSON.stringify({ model: id, max_tokens: params.maxOutputTokens, messages: [{ role: 'user', content: govde }] }) });
  await res.text();
  return res;
}

async function incelemeOrtami(task, role, work) {
  const saved = { CADRE_GOREV_ID: process.env.CADRE_GOREV_ID, CADRE_ROL: process.env.CADRE_ROL };
  Object.assign(process.env, { CADRE_GOREV_ID: task, CADRE_ROL: role });
  try { return await work(); } finally {
    for (const [key, value] of Object.entries(saved)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}

test('in the current plan review a small gap of the real body is transferred from the same total; preview and both ledgers agree', async () => {
  const f = ev({ or: { db: 'ok', pay: 50, kullanilabilir: 50, kalan: 50 } }, 2.1);
  writeFileSync(join(f.home, 'zincir', 'openrouter-fiyat.json'), JSON.stringify({ version: 2, ts: Date.now() / 1000, modeller: {
    'deepseek/deepseek-v4-pro': { input: 1.3, output: 2.6, providers: ['streamlake/fp8'], fp8_cache: true } } }));
  await incelemeOrtami('plan-review-1-abcdef01', 'review', async () => {
    py(f.code, `import os;from kosu import RunStore,atomic_json;from butce_kapisi import BudgetLedger,observed_budget;` +
      `s=RunStore();rid=os.environ['CADRE_RUN_ID'];s.save_plan(rid,{'is_id':'p1','gorevler':[{'id':'g1','tanim':'Read source','kabul':'Source preserved','dosyalar':['README.md'],'model':'auto','zorluk':1,'paralel_grup':0,'tahmin_dk':1,'tahmin_usd':0.5,'bagimli':[]}]});` +
      `r=s.read(rid);r['plan_review_claim']={'id':'owned','revision':1,'task_id':os.environ['CADRE_GOREV_ID']};atomic_json(s.path(rid)/'run.json',r);` +
      `l=BudgetLedger(s.home/'zincir/request-budget.db');i=l.reserve('openrouter',200200,observed_budget(s.home,__import__('pathlib').Path(os.environ['CADRE_ENGINE_ROOT'])),` +
      `{'run_id':rid,'phase_role':'review','task_id':os.environ['CADRE_GOREV_ID']},210000,'yardimci');l.settle(i,4000,i)`);
    const before = readFileSync(join(f.home, 'zincir', 'request-budget.db'));
    assert.equal(py(f.code, `import os;from pathlib import Path;from butce_kapisi import BudgetLedger,observed_budget;l=BudgetLedger(Path(os.environ['CADRE_HOME'])/'zincir/request-budget.db');` +
      `print(l.available('openrouter',observed_budget(Path(os.environ['CADRE_HOME']),Path(os.environ['CADRE_ENGINE_ROOT'])),210000,os.environ['CADRE_RUN_ID'],'yardimci',206900,'review',salt_okur=True))`), '206900');
    assert.deepEqual(readFileSync(join(f.home, 'zincir', 'request-budget.db')), before);
    let calls = 0;
    const config = { provider: { openrouter: { options: { fetch: async () => {
      calls += 1; return yanit({ prompt_tokens: 1000, completion_tokens: 100, cost: 0.004 }); } } } } };
    const hooks = [await yukle(join(f.code, 'lib', 'cadre-plugin.mjs'))];
    await hooks[0].config(config);
    await cagir(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro', 'build', 'x'.repeat(90000));
    assert.equal(calls, 1);
    const last = f.satirlar().at(-1);
    const caps = JSON.parse(py(f.code, `import sqlite3,json,os;c=sqlite3.connect(os.environ['CADRE_HOME']+'/zincir/request-budget.db');print(c.execute('select caps from role_allocation').fetchone()[0])`));
    assert.ok(last.reserved > 206000);
    assert.equal(caps.yardimci, 4000 + last.reserved);
    assert.equal(caps.hakem, 315000);
    assert.equal(Object.values(caps).reduce((a, b) => a + b, 0), 2100000);
    assert.equal(last.actual, 4000);
    assert.equal(JSON.parse(last.metadata).review_phase, 'plan-review');
  });
});

const yazarSiniri = (f) => JSON.parse(readFileSync(join(f.code, 'lib', 'sinirlar.json'), 'utf8')).inceleme['review-author'];

test('the review round cap holds in the real plugin across new tasks and HTTP retries; a denial holds no money in either ledger', async () => {
  const f = ev({ or: { db: 'ok', pay: 50, kullanilabilir: 50, kalan: 50 } }, 10);
  writeFileSync(join(f.home, 'zincir', 'openrouter-fiyat.json'), JSON.stringify({ version: 2, ts: Date.now() / 1000, modeller: {
    'deepseek/deepseek-v4-pro': { input: 1.3, output: 2.6, providers: ['streamlake/fp8'], fp8_cache: true } } }));
  await incelemeOrtami('review-author-abcdef012345', 'impl', async () => {
    let calls = 0;
    const config = { provider: { openrouter: { options: { fetch: async () => {
      calls += 1; return yanit({ prompt_tokens: 1000, completion_tokens: 100, cost: 0.0012 }); } } } } };
    const hooks = [await yukle(join(f.code, 'lib', 'cadre-plugin.mjs'))];
    await hooks[0].config(config);
    const { istek } = yazarSiniri(f);
    for (let n = 0; n < istek; n++) await cagir(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro');
    process.env.CADRE_GOREV_ID = 'review-author-eeeeeeeeeeee';
    await assert.rejects(cagir(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro'), /CADRE_BUDGET_DENIED/);
    assert.equal(calls, istek);
    assert.equal(f.satirlar().length, istek);
    assert.equal(py(f.code, `import sqlite3,os;c=sqlite3.connect(os.environ['CADRE_HOME']+'/zincir/request-budget.db');print(c.execute('select calls from review_control').fetchone()[0])`), String(istek));
    assert.ok(f.kayit().denials.some((d) => String(d.reason).includes('review-author: requests')));
    const monthly = JSON.parse(py(f.code, `import sqlite3,json,os;c=sqlite3.connect(os.environ['CADRE_HOME']+'/zincir/aylik-butce.db');c.row_factory=sqlite3.Row;print(json.dumps([dict(r) for r in c.execute('select * from reservation')]))`));
    assert.equal(monthly.filter((r) => r.actual === null).length, 0);
    assert.equal(monthly.filter((r) => r.actual > 0).length, istek);
  });
});

test('after a repair the tool cap still applies to every tool call in the judge phase', async () => {
  const f = ev(go, 10);
  await incelemeOrtami('review-recheck-abcdef012345', 'kalite', async () => {
    const hooks = await yukle(join(f.code, 'lib', 'butce-plugin.mjs'));
    const { arac } = JSON.parse(readFileSync(join(f.code, 'lib', 'sinirlar.json'), 'utf8')).inceleme['review-recheck'];
    for (let n = 0; n < arac; n++) await hooks['tool.execute.before']({ tool: 'todoread' }, { args: {} });
    await assert.rejects(hooks['tool.execute.before']({ tool: 'todoread' }, { args: {} }), /CADRE_TOOL_LIMIT/);
    assert.ok(f.kayit().denials.some((d) => String(d.reason).includes('review-recheck: tools')));
  });
});

test('the review tool cap is independent of the prompt text and a new task does not reset the counter', async () => {
  const f = ev(go, 10);
  await incelemeOrtami('review-author-abcdef012345', 'impl', async () => {
    const hooks = await yukle(join(f.code, 'lib', 'butce-plugin.mjs'));
    const { arac } = yazarSiniri(f);
    for (let n = 0; n < arac; n++) await hooks['tool.execute.before']({ tool: 'todoread' }, { args: {} });
    process.env.CADRE_GOREV_ID = 'review-author-000000000002';
    await assert.rejects(hooks['tool.execute.before']({ tool: 'todoread' }, { args: {} }), /CADRE_TOOL_LIMIT/);
    assert.equal(py(f.code, `import sqlite3,os;c=sqlite3.connect(os.environ['CADRE_HOME']+'/zincir/request-budget.db');print(c.execute('select tools from review_control').fetchone()[0])`), String(arac));
    assert.ok(f.kayit().denials.some((d) => String(d.reason).includes('review-author: tools')));
    assert.ok(!f.kayit().denials.some((d) => String(d.reason).includes('ledger refused')), 'arac tavani genel bütçe reddi yazmaz');
    assert.equal(f.satirlar().length, 0);
  });
});

test('the review cost and time caps cut before the real fetch; pending and monthly shares are kept', async () => {
  const f = ev({ or: { db: 'ok', pay: 50, kullanilabilir: 50, kalan: 50 } }, 1, true);
  writeFileSync(join(f.home, 'zincir', 'openrouter-fiyat.json'), JSON.stringify({ version: 2, ts: Date.now() / 1000, modeller: {
    'deepseek/deepseek-v4-pro': { input: 1.3, output: 2.6, providers: ['streamlake/fp8'], fp8_cache: true } } }));
  await incelemeOrtami('review-author-abcdef012345', 'impl', async () => {
    let calls = 0;
    const config = { provider: { openrouter: { options: { fetch: async () => {
      calls += 1; return yanit({ prompt_tokens: 1000, completion_tokens: 100, cost: 0.1 }); } } } } };
    const hooks = [await yukle(join(f.code, 'lib', 'cadre-plugin.mjs'))];
    await hooks[0].config(config);
    await cagir(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro', 'build', 'x'.repeat(30000));
    const ipc = JSON.parse(readFileSync(join(f.home, 'budget-ipc.jsonl'), 'utf8').trim());
    assert.ok(!ipc.keys.includes('payload'), 'unused prompt body must stay outside ledger IPC');
    assert.ok(ipc.bytes < 4096, `ledger request ${ipc.bytes} bytes`);
    await assert.rejects(cagir(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro', 'build', 'x'.repeat(30000)), /CADRE_BUDGET_DENIED/);
    assert.equal(calls, 1);
    assert.ok(f.kayit().denials.some((d) => String(d.reason).includes('review-author: cost')),
      JSON.stringify({ denials: f.kayit().denials, rows: f.satirlar().map(({ reserved, actual }) => ({ reserved, actual })) }));
    const sure = yazarSiniri(f).sure_sn;
    py(f.code, `import sqlite3,os,time;c=sqlite3.connect(os.environ['CADRE_HOME']+'/zincir/request-budget.db');t=int(time.time())-${sure + 1};c.execute('update review_control set started=?,deadline=?',(t,t+${sure}));c.commit()`);
    process.env.CADRE_GOREV_ID = 'review-author-ffffffffffff';
    await assert.rejects(cagir(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro'), /CADRE_BUDGET_DENIED/);
    assert.equal(calls, 1);
    assert.ok(f.kayit().denials.some((d) => String(d.reason).includes('review-author: time')));
    assert.equal(f.satirlar().length, 1);
    const monthly = JSON.parse(py(f.code, `import sqlite3,json,os;c=sqlite3.connect(os.environ['CADRE_HOME']+'/zincir/aylik-butce.db');c.row_factory=sqlite3.Row;print(json.dumps([dict(r) for r in c.execute('select * from reservation')]))`));
    assert.equal(monthly.filter((r) => r.actual === null).length, 0);
    assert.equal(monthly.filter((r) => r.actual > 0).length, 1);
  });
});

test('double gate: even if the same gate loads twice (cadre-plugin + butce-plugin, live + repo root) the call passes once', async () => {
  // 06.10 smoke: the global config loaded cadre-plugin.mjs, the run config loaded butce-plugin.mjs; the header token was not in the other copy.
  for (const ikinci of ['ayni-kok-butce-plugin', 'baska-kok-cadre-plugin']) {
    const f = ev(go);
    const ikinciDosya = ikinci === 'ayni-kok-butce-plugin' ? join(f.code, 'lib', 'butce-plugin.mjs')
      : join(kodKoku(f.home, 'code2', go), 'lib', 'cadre-plugin.mjs');
    let ag = 0;
    const config = { enabled_providers: ['lmstudio'], provider: { lmstudio: { options: {} },
      'opencode-go': { options: { fetch: async () => { ag += 1; return yanit(); } } } } };
    const kancalar = [await yukle(join(f.code, 'lib', 'cadre-plugin.mjs')), await yukle(ikinciDosya)];
    for (const k of kancalar) await k.config?.(config);
    assert.ok(!config.provider.lmstudio && !config.enabled_providers.includes('lmstudio'), 'eski canli configteki yerel saglayici acilmaz (06.10)');
    const res = await cagir(kancalar, config, 'opencode-go', 'deepseek-v4-pro');
    assert.equal(res.status, 200, ikinci);
    assert.equal(ag, 1, `${ikinci}: saglayiciya tek istek`);
    const satir = f.satirlar().filter((r) => r.provider === 'opencode-go');
    assert.equal(satir.length, 1, `${ikinci}: tek rezervasyon (cift sayim yok)`);
    assert.notEqual(satir[0].actual, null, `${ikinci}: makbuzla kapandi`);
  }
});

test('small cap: if the pocket is not enough at the real catalogue price there is no network call, and the denial is written to the run record with the provider', async () => {
  const f = ev(go, 0.05);   // lane pocket 75% = $0.0375; deepseek-v4-pro output cap 32768 x $1.98/M = $0.065
  let ag = 0;
  const config = { enabled_providers: [], provider: { 'opencode-go': { options: { fetch: async () => { ag += 1; return yanit(); } } } } };
  const kancalar = [await yukle(join(f.code, 'lib', 'cadre-plugin.mjs'))];
  await kancalar[0].config(config);
  await assert.rejects(cagir(kancalar, config, 'opencode-go', 'deepseek-v4-pro'), /CADRE_BUDGET_DENIED/);
  assert.equal(ag, 0, 'red agdan once');
  assert.ok((f.kayit().denials ?? []).some((d) => d.provider === 'opencode-go'), 'red kayitta, saglayiciyla');
  assert.equal(f.satirlar().filter((r) => r.provider === 'opencode-go' && r.actual === null).length, 0, 'askida para yok');

  py(f.code, `from kosu import RunStore;RunStore().update('${process.env.CADRE_RUN_ID}',budget_cap_usd=2)`);
  execFileSync(process.env.CADRE_PYTHON, [join(f.code, 'lib', 'izin.py'), 'model-consent', process.env.CADRE_RUN_ID],
    { input: 'evet\nevet\n', env: { ...process.env, CADRE_RUN_ID: '' } });
  const res = await cagir(kancalar, config, 'opencode-go', 'deepseek-v4-pro');
  assert.equal(res.status, 200);
  const satir = f.satirlar().filter((r) => r.provider === 'opencode-go' && r.actual !== null).at(-1);
  // receipt with the real catalogue rates: 1000 x 0.66 + 100 x 1.98 = 858 micro-dollars; Go DeepSeek 2× at peak hours (4c-13)
  const yogun = (() => { const d = new Date(), g = d.getUTCDay(), s = d.getUTCHours();
    return g >= 1 && g <= 5 && ((s >= 1 && s < 4) || (s >= 6 && s < 10)); })();
  assert.equal(satir.actual, 858 * (yogun ? 2 : 1));

  // (06.10 note): a Go lane reservation is priced by the request body, not by the model's 1M context: a 100K-character
  // request ~$0.134, always 2× at the peak price on DeepSeek (it would be $1.45 with a 1M context). With a small cap the pocket rule is what removes Go.
  const once = f.satirlar().length;
  assert.equal((await cagir(kancalar, config, 'opencode-go', 'deepseek-v4-pro', 'build', 'x'.repeat(100000))).status, 200);
  const yeni = f.satirlar().slice(once).filter((r) => r.provider === 'opencode-go');
  assert.equal(yeni.length, 1);
  assert.ok(yeni[0].reserved > 260000 && yeni[0].reserved < 280000, `rezervasyon ${yeni[0].reserved} mikro-dolar`);
});

test('route hand-off: when Go closes the OR route of the same model is chosen and the plugin pins it and settles with the real receipt', async () => {
  const gozlem = { go: { ...go.go, pencere_acik: false }, or: { db: 'ok', pay: 5, kullanilabilir: 5, kalan: 5 } };
  const f = ev(gozlem);
  const devret = () => py(f.code, "import qe_core;print(qe_core.yol_devret('opencode-go/deepseek-v4-pro','" +
    join(f.home, 'devir.log') + "','Go limit') or 'BEKLE')");
  assert.equal(devret(), 'BEKLE', 'OR fiyat gozlemi yokken eklentinin reddedecegi yol sunulmaz');
  writeFileSync(join(f.home, 'zincir', 'openrouter-fiyat.json'), JSON.stringify({ version: 2, ts: Date.now() / 1000, modeller: {
    'deepseek/deepseek-v4-pro': { input: 1.3, output: 2.6, providers: ['streamlake/fp8', 'deepinfra/fp8'], fp8_cache: true } } }));
  assert.equal(devret(), 'openrouter/deepseek/deepseek-v4-pro');

  let govde;
  const config = { enabled_providers: [], provider: { openrouter: { options: { fetch: async (_u, o) => {
    govde = JSON.parse(o.body); return yanit({ prompt_tokens: 1000, completion_tokens: 100, cost: 0.0012 }); } } } } };
  const kancalar = [await yukle(join(f.code, 'lib', 'cadre-plugin.mjs'))];
  await kancalar[0].config(config);
  const res = await cagir(kancalar, config, 'openrouter', 'deepseek/deepseek-v4-pro');
  assert.equal(res.status, 200);
  assert.deepEqual(govde.provider.only, ['streamlake/fp8', 'deepinfra/fp8']);
  assert.deepEqual(govde.provider.quantizations, ['fp8']);
  const satir = f.satirlar().filter((r) => r.provider === 'openrouter').at(-1);
  assert.equal(satir.actual, 1200, 'makbuz saglayicinin usage.cost degeri (tahmin degil)');
});

test('title agent: if the SDK retries with the same header both gates pass it; a 429 leaves no obligation (4c-4)', async () => {
  // 07.10 05:15: the monthly gate deleted the token on the first attempt, and the SDK's second attempt with the same header got 'monthly request context missing'.
  const f = ev({ or: { db: 'ok', pay: 5, kullanilabilir: 5, kalan: 5 } });
  writeFileSync(join(f.home, 'zincir', 'openrouter-fiyat.json'), JSON.stringify({ version: 2, ts: Date.now() / 1000, modeller: {
    'deepseek/deepseek-v4-pro': { input: 1.3, output: 2.6, providers: ['streamlake/fp8', 'deepinfra/fp8'], fp8_cache: true } } }));
  let n = 0;
  const config = { enabled_providers: [], provider: { openrouter: { options: { fetch: async () => (++n === 1
    ? new Response('{"error":{"code":429}}', { status: 429, headers: { 'content-type': 'application/json' } })
    : yanit({ prompt_tokens: 1000, completion_tokens: 100, cost: 0.0012 })) } } } };
  const kancalar = [await yukle(join(f.code, 'lib', 'cadre-plugin.mjs'))];
  await kancalar[0].config(config);
  const id = 'deepseek/deepseek-v4-pro', m = katalog.openrouter.models[id];
  const input = { sessionID: 's-baslik', agent: 'title', model: { providerID: 'openrouter', id, api: { id }, limit: m.limit, cost: m.cost } };
  const params = { maxOutputTokens: 1000 };
  for (const k of kancalar) await k['chat.params']?.(input, params);
  const headers = { headers: {} };
  for (const k of kancalar) await k['chat.headers']?.(input, headers);
  const istek = () => config.provider.openrouter.options.fetch('https://fixture.invalid/chat/completions', { method: 'POST',
    headers: headers.headers, body: JSON.stringify({ model: id, max_tokens: params.maxOutputTokens, messages: [{ role: 'user', content: 'baslik' }] }) });
  assert.equal((await istek()).status, 429);
  const ikinci = await istek();
  await ikinci.text();
  assert.equal(ikinci.status, 200, 'ayni baslikla ikinci deneme gecer');
  const aylik = JSON.parse(execFileSync(process.env.CADRE_PYTHON, ['-c',
    "import sqlite3,json,sys;c=sqlite3.connect(sys.argv[1]);c.row_factory=sqlite3.Row;" +
    "print(json.dumps([dict(r) for r in c.execute('select * from reservation')]))",
    join(f.home, 'zincir', 'aylik-butce.db')], { encoding: 'utf8' }));
  for (const [ad, satir] of [['saglayici', f.satirlar()], ['aylik', aylik]]) {
    assert.equal(satir.filter((r) => r.actual === null).length, 0, `${ad}: askida yukumluluk yok`);
    assert.ok(satir.some((r) => r.actual === 0 && r.receipt.endsWith(':http-429')), `${ad}: ilk denemenin hatasi kayitta`);
    assert.equal(satir.filter((r) => r.actual > 0).length, 1, `${ad}: tek makbuz`);
  }
});

test('OpenRouter yanitinda maliyet yoksa iki defter de rezervasyonu tutar; aylik defter tahmini kesinlestirmez', async () => {
  const f = ev({ or: { db: 'ok', pay: 5, kullanilabilir: 5, kalan: 5 } });
  writeFileSync(join(f.home, 'zincir', 'openrouter-fiyat.json'), JSON.stringify({ version: 2, ts: Date.now() / 1000, modeller: {
    'deepseek/deepseek-v4-pro': { input: 1.3, output: 2.6, providers: ['streamlake/fp8', 'deepinfra/fp8'], fp8_cache: true } } }));
  let agBasliklari;
  const config = { enabled_providers: [], provider: { openrouter: { options: { fetch: async (url, o) => {
    agBasliklari = new Headers(o.headers); return yanit({ prompt_tokens: 1000, completion_tokens: 100 }); } } } } };
  const kancalar = [await yukle(join(f.code, 'lib', 'cadre-plugin.mjs'))];
  await kancalar[0].config(config);
  const id = 'deepseek/deepseek-v4-pro', m = katalog.openrouter.models[id];
  const input = { sessionID: 's-maliyetsiz', agent: 'title', model: { providerID: 'openrouter', id, api: { id }, limit: m.limit, cost: m.cost } };
  const params = { maxOutputTokens: 1000 };
  for (const k of kancalar) await k['chat.params']?.(input, params);
  const headers = { headers: {} };
  for (const k of kancalar) await k['chat.headers']?.(input, headers);
  const cevap = await config.provider.openrouter.options.fetch('https://fixture.invalid/chat/completions', { method: 'POST',
    headers: headers.headers, body: JSON.stringify({ model: id, max_tokens: params.maxOutputTokens, messages: [{ role: 'user', content: 'baslik' }] }) });
  await cevap.text();
  const aylik = JSON.parse(execFileSync(process.env.CADRE_PYTHON, ['-c',
    "import sqlite3,json,sys;c=sqlite3.connect(sys.argv[1]);c.row_factory=sqlite3.Row;" +
    "print(json.dumps([dict(r) for r in c.execute('select * from reservation')]))",
    join(f.home, 'zincir', 'aylik-butce.db')], { encoding: 'utf8' }));
  for (const [ad, satir] of [['saglayici', f.satirlar()], ['aylik', aylik]]) {
    assert.equal(satir.length, 1, `${ad}: tek rezervasyon`);
    assert.equal(satir[0].actual, null, `${ad}: maliyet bilinmeden kesinlesmez`);
  }
  assert.equal(JSON.parse(f.satirlar()[0].metadata).aylik_id, aylik[0].id, 'saglayici kaydi aylik esin kimligini tasir');
  assert.equal(agBasliklari.get('x-cadre-aylik-kimlik'), null, 'eslesme basligi aga gitmez');
});

test('real HTTP: speed warning continues, explicit alarm approval preserves monthly and run limits', async () => {
  const previousCap = process.env.CADRE_KOSU_TAVAN_USD;
  const previousMonthly = process.env.CADRE_AYLIK_TAVAN_USD;
  process.env.CADRE_KOSU_TAVAN_USD = '4';
  process.env.CADRE_AYLIK_TAVAN_USD = '4';
  let server;
  try {
    const f = ev({ or: { db: 'ok', pay: 50, kullanilabilir: 50, kalan: 50 } }, 4);
    const rid = process.env.CADRE_RUN_ID;
    writeFileSync(join(f.home, 'zincir', 'openrouter-fiyat.json'), JSON.stringify({ version: 2, ts: Date.now() / 1000, modeller: {
      'deepseek/deepseek-v4-pro': { input: 1.3, output: 2.6, providers: ['deepinfra/fp8'], fp8_cache: true } } }));
    py(f.code, `from kosu import RunStore;from pathlib import Path;import butce_kapisi as b;` +
      `s=RunStore();s.update('${rid}',budget_manual=True);` +
      `l=b.BudgetLedger(s.home/'zincir/request-budget.db');` +
      `i=l.reserve('openrouter',1700000,b.observed_budget(s.home,Path('${f.code}')),{'run_id':'${rid}'});l.settle(i,1700000,i);` +
      `b.ilerleme_yaz(s.home,'${rid}','qa-pass','accepted-revision')`);
    let requests = 0;
    server = createServer((req, res) => {
      requests += 1;
      const row = f.satirlar().at(-1);
      assert.equal(row.actual, null, 'provider hold exists before HTTP');
      assert.ok(JSON.parse(row.metadata).aylik_id, 'monthly twin exists before HTTP');
      assert.equal(req.headers['x-cadre-aylik-kimlik'], undefined);
      assert.equal(req.headers['x-cadre-budget-context'], undefined);
      req.resume();
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ id: 'local-receipt-' + requests, usage: { prompt_tokens: 1000, completion_tokens: 100, cost: 0.0012 } }));
    });
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
    const url = `http://127.0.0.1:${server.address().port}/chat/completions`;
    const config = { enabled_providers: [], provider: { openrouter: { options: { fetch: (_u, opts) => fetch(url, opts) } } } };
    const hooks = [await yukle(join(f.code, 'lib', 'cadre-plugin.mjs'))];
    await hooks[0].config(config);
    await cagir(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro');
    assert.equal(f.kayit().harcama_uyarisi.ilerleme, 'productive');
    assert.equal(f.kayit().harcama_alarmi, undefined);
    py(f.code, `from kosu import RunStore;import time;RunStore().update('${rid}',harcama_alarmi={'ts':time.time(),'neden':'explicit stop'})`);
    await assert.rejects(cagir(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro'), /CADRE_BUDGET_DENIED/);
    assert.equal(requests, 1, 'explicit alarm opens no HTTP request');
    py(f.code, `from kosu import RunStore,approve_spending;approve_spending(RunStore(),'${rid}')`);
    process.env.CADRE_AYLIK_TAVAN_USD = '0.01';
    await assert.rejects(cagir(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro'), /CADRE_BUDGET_DENIED/);
    assert.equal(requests, 1, 'alarm approval cannot override monthly cap');
    process.env.CADRE_AYLIK_TAVAN_USD = '4';
    await cagir(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro');
    assert.equal(requests, 2, 'approved call completes through both gates');
    py(f.code, `from kosu import RunStore;RunStore().update('${rid}',budget_cap_usd=0)`);
    await assert.rejects(cagir(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro'), /CADRE_BUDGET_DENIED/);
    assert.equal(requests, 2, 'alarm approval cannot override run cap');
    assert.equal(f.satirlar().filter(r => r.actual === null).length, 0);
    assert.equal(py(f.code, `from aylik_butce import MonthlyLedger;from pathlib import Path;` +
      `print(MonthlyLedger(Path('${f.home}')/'zincir/aylik-butce.db').status()['held_micro'])`), '0');
  } finally {
    if (server) await new Promise(resolve => server.close(resolve));
    if (previousCap === undefined) delete process.env.CADRE_KOSU_TAVAN_USD; else process.env.CADRE_KOSU_TAVAN_USD = previousCap;
    if (previousMonthly === undefined) delete process.env.CADRE_AYLIK_TAVAN_USD; else process.env.CADRE_AYLIK_TAVAN_USD = previousMonthly;
  }
});

test('real composed gate: an over-bound cash receipt blocks the same project on the subscription path', async () => {
  const previousCap = process.env.CADRE_KOSU_TAVAN_USD;
  process.env.CADRE_KOSU_TAVAN_USD = '4';
  try {
    const f = ev({ ...go, or: { db: 'ok', pay: 50, kullanilabilir: 50, kalan: 50 } }, 4);
    const rid = process.env.CADRE_RUN_ID;
    writeFileSync(join(f.home, 'zincir', 'openrouter-fiyat.json'), JSON.stringify({ version: 2, ts: Date.now() / 1000, modeller: {
      'deepseek/deepseek-v4-pro': { input: 1.3, output: 2.6, providers: ['deepinfra/fp8'], fp8_cache: true } } }));
    py(f.code, `from kosu import RunStore;from proje import proje_butcesi_sabitle;proje_butcesi_sabitle(RunStore(),'${rid}')`);
    let calls = 0;
    const fetcher = async () => { calls += 1; return yanit({ prompt_tokens: 1000, completion_tokens: 100, cost: 0.8 }); };
    const config = { enabled_providers: [], provider: { openrouter: { options: { fetch: fetcher } },
      'opencode-go': { options: { fetch: fetcher } } } };
    const hooks = [await yukle(join(f.code, 'lib', 'cadre-plugin.mjs'))];
    await hooks[0].config(config);
    await cagir(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro');
    const row = f.satirlar().at(-1);
    assert.equal(row.actual, 800000, 'unexpected cost remains visible');
    assert.ok(JSON.parse(row.metadata).verified_bound_micro < row.actual);
    py(f.code, `from kosu import RunStore,approve_spending;approve_spending(RunStore(),'${rid}')`);
    await assert.rejects(cagir(hooks, config, 'opencode-go', 'deepseek-v4-pro'), /CADRE_BUDGET_DENIED/);
    assert.equal(calls, 1, 'provider change cannot bypass the project overrun');
    assert.equal(f.satirlar().length, 1);
  } finally {
    if (previousCap === undefined) delete process.env.CADRE_KOSU_TAVAN_USD; else process.env.CADRE_KOSU_TAVAN_USD = previousCap;
  }
});

const KESINTI = { code: 502, message: 'Flex processing is temporarily unavailable. Please try again later or use standard processing.',
  metadata: { error_type: 'provider_unavailable' } };
const kesintiSse = (n) => new Response(`data: ${JSON.stringify({ id: `gen-1792404912-kesinti${n}`, error: KESINTI })}\n\n`,
  { headers: { 'content-type': 'text/event-stream' } });
const kesintiHttp = () => new Response(JSON.stringify({ error: KESINTI }), { status: 502, headers: { 'content-type': 'application/json' } });
const dsFiyati = (f) => writeFileSync(join(f.home, 'zincir', 'openrouter-fiyat.json'), JSON.stringify({ version: 2, ts: Date.now() / 1000,
  modeller: { 'deepseek/deepseek-v4-pro': { input: 1.3, output: 2.6, providers: ['streamlake/fp8'], fp8_cache: true } } }));
const yolDurumu = (f) => JSON.parse(readFileSync(join(f.home, 'zincir', 'saglayici-durum.json'), 'utf8'));

for (const [bicim, yanitUret] of [['SSE', kesintiSse], ['HTTP', kesintiHttp]]) {
  test(`${bicim} provider_unavailable kesiciye OpenCode tekrarindan once ulasir; ayni kapali yolda yeni belirsiz rezerv acilmaz`, async () => {
    const f = ev({ or: { db: 'ok', pay: 50, kullanilabilir: 50, kalan: 50 } }, 2);
    dsFiyati(f);
    let calls = 0;
    const config = { provider: { openrouter: { options: { fetch: async () => { calls += 1; return yanitUret(calls); } } } } };
    const hooks = [await yukle(join(f.code, 'lib', 'cadre-plugin.mjs'))];
    await hooks[0].config(config);
    await cagir(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro');
    const [ilk] = f.satirlar();
    assert.equal(ilk.actual, null, 'tutar bilinmiyor: rezerv korunur, 502 sifir sayilmaz');
    assert.equal(JSON.parse(ilk.metadata).saglayici_hatasi.tur, 'provider_unavailable');
    const durum = yolDurumu(f);
    const modelDurumu = durum['openrouter/deepseek/deepseek-v4-pro'];
    assert.ok(modelDurumu.uclar['streamlake/fp8'].cooldown_bitis > Date.now() / 1000, 'hata veren uc yeniden denemeden once kapali');
    assert.ok(!(modelDurumu.cooldown_bitis > Date.now() / 1000), 'tek uc kesintisi modelin diger uclarini kapatmaz');
    assert.ok(!(durum.openrouter?.cooldown_bitis > Date.now() / 1000), 'butun saglayici kapanmaz');
    await assert.rejects(cagir(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro'), /CADRE_BUDGET_DENIED/);
    assert.equal(calls, 1, 'kapali yola ikinci istek gitmez');
    assert.equal(f.satirlar().length, 1, 'kapali yolda rezerv birikmez');
    assert.ok(f.kayit().denials.some((d) => String(d.reason).startsWith('provider unavailable: openrouter/deepseek/deepseek-v4-pro')));
  });
}

test('a general 5xx error keeps the reserve but does not close the route on a single error; 429 keeps its old behaviour', async () => {
  const f = ev({ or: { db: 'ok', pay: 50, kullanilabilir: 50, kalan: 50 } }, 2);
  dsFiyati(f);
  let calls = 0;
  const config = { provider: { openrouter: { options: { fetch: async () => {
    calls += 1;
    return calls === 1 ? new Response('{"error":{"code":500,"message":"upstream"}}', { status: 500, headers: { 'content-type': 'application/json' } })
      : new Response('{"error":{"code":429,"message":"rate limit"}}', { status: 429, headers: { 'content-type': 'application/json' } });
  } } } } };
  const hooks = [await yukle(join(f.code, 'lib', 'cadre-plugin.mjs'))];
  await hooks[0].config(config);
  await cagir(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro');
  await cagir(hooks, config, 'openrouter', 'deepseek/deepseek-v4-pro');
  assert.equal(calls, 2);
  const [genel, kota] = f.satirlar();
  assert.equal(genel.actual, null);
  assert.deepEqual([kota.actual, kota.receipt.endsWith(':http-429')], [0, true]);
  assert.ok(!(yolDurumu(f)['openrouter/deepseek/deepseek-v4-pro']?.cooldown_bitis > Date.now() / 1000));
});
