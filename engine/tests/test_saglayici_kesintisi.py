"""RCA code28/29: a provider outage (OpenRouter FLEX provider_unavailable) goes to a recorded wait; the reserve is kept,
no uncertain reserve accumulates on a closed route, the account summary keeps the receipted/estimated/pending classes separate."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))

SOL = 'openrouter/openai/gpt-6.1-sol'
KESINTI_REDDI = {'providers': ['openrouter'], 'refusals': [
    {'provider': 'openrouter', 'reason': f'provider unavailable: {SOL} until 1792406700', 'dilim': None, 'scope': 'provider'}]}
BUTCE_REDDI = {'providers': ['openrouter'], 'refusals': [
    {'provider': 'openrouter', 'reason': 'run budget exhausted: reservation $0.22 > remaining $0.00', 'dilim': 'yardimci', 'scope': 'role'}]}


class SaglayiciBeklemeSinifiTests(unittest.TestCase):
    def test_only_a_bound_stopped_process_scope_can_pause_a_provider_failed_review(self):
        import tempfile
        from types import SimpleNamespace

        import qe_core as core
        from butce_kapisi import BudgetLedger, SaglayiciBekleme
        from kosu import RunStore, atomic_json
        with tempfile.TemporaryDirectory(prefix='provider-pause-') as td, patch.dict(
                os.environ, {'CADRE_HOME': td, 'CADRE_KOSU_TAVAN_USD': '3', 'CADRE_AG_YOK': '1'}):
            store = RunStore(Path(td))
            ledger = BudgetLedger(store.home / 'zincir/request-budget.db')
            for variant in ('missing', 'unobserved', 'foreign', 'open-scope', 'verified'):
                with self.subTest(variant=variant):
                    rid = store.accept('lab', 'TUR: inceleme\nSource review')['run_id']
                    task, aid = 'plan-review-1-abcdef01', 'a' * 32
                    directory = store.task_dir(rid, task) / 'attempts' / aid
                    directory.mkdir(parents=True)
                    identity = {'pid': 123, 'started': 'fixture:123', 'pgid': 123}
                    receipt = {'run_id': rid, 'task_id': task, 'attempt_id': aid,
                               'process_identity': identity, 'stop_observed': identity,
                               'scope_digest': 'b' * 64, 'scope_stopped': 'b' * 64}
                    if variant == 'unobserved': receipt.pop('stop_observed')
                    if variant == 'foreign': receipt['run_id'] = 'other-run'
                    if variant == 'open-scope': receipt.pop('scope_stopped')
                    if variant != 'missing': atomic_json(directory / 'process.json', receipt)
                    process = Mock(spec=core.RecordedProcess)
                    process.attempt = SimpleNamespace(run_id=rid, task_id=task, directory=directory, store=store,
                                                      path=directory / 'attempt.json',
                                                      data={'model': SOL, 'attempt_id': aid})
                    process.output.return_value = ''
                    with patch.dict(os.environ, {'CADRE_RUN_ID': rid, 'CADRE_GOREV_ID': task}), \
                         patch.object(core, 'QE', store.home), patch.object(core, 'run_opencode', return_value=process), \
                         patch.object(core, 'wait_process', return_value=75), \
                         patch.object(core, 'budget_denial_receipt', return_value=KESINTI_REDDI), \
                         patch.object(core, 'retire_owned_tools', return_value=variant == 'verified') as stopped, \
                         self.assertRaises(SaglayiciBekleme):
                        core.capture_opencode('fixture', td, ['-m', SOL, 'PLAN ISTEK UYUMU\n{}'], 300, role='review')
                    if variant in ('open-scope', 'verified'):
                        stopped.assert_called_once_with(store, process.attempt.path, process.attempt.data)
                    else:
                        stopped.assert_not_called()
                    with ledger.transaction() as con:
                        pause = con.execute('select paused_at from review_pause where run_id=?', (rid,)).fetchone()
                    self.assertEqual(bool(pause and pause[0] is not None), variant == 'verified')

    def test_kesinti_reddi_yolu_ve_bitisi_verir_butce_reddi_vermez(self):
        from butce_kapisi import saglayici_bekleme_reddi
        self.assertEqual(saglayici_bekleme_reddi(KESINTI_REDDI), (SOL, 1792406700.0))
        self.assertIsNone(saglayici_bekleme_reddi(BUTCE_REDDI))
        self.assertIsNone(saglayici_bekleme_reddi(None))

    def test_kesinti_reddi_saglayiciyi_gorevde_yasaklamaz_butce_reddi_yasaklar(self):
        import qe_core as core

        class Yasak:
            def __init__(self):
                self.engel = []

            def block(self, provider, neden, snapshot, dilim=None):
                self.engel.append((provider, neden))

        kesinti, butce = Yasak(), Yasak()
        core.apply_budget_denial(kesinti, KESINTI_REDDI, {})
        core.apply_budget_denial(butce, {'providers': ['openrouter'], 'refusals': [
            {'provider': 'openrouter', 'reason': 'provider share exhausted', 'dilim': None, 'scope': 'provider'}]}, {})
        self.assertEqual(kesinti.engel, [], 'yalniz etkilenen (model, yol) kesicide kapali; butun saglayici gorevde kapanmaz')
        self.assertEqual(butce.engel, [('openrouter', 'budget')])

    def test_son_hakem_kesintisi_kesici_bitisini_bekler_butce_reddi_gece_yarisi_kurali_korunur(self):
        from kosu import kapasite_bekleme
        simdi = 1792406000.0
        kesinti = {'review': {'status': 'ERROR', 'reason': f'SaglayiciBekleme: saglayici-bekleme: {SOL} until 1792406700'}}
        self.assertEqual(kapasite_bekleme(kesinti, simdi), 1792406700.0)
        butce = {'review': {'status': 'ERROR', 'reason': 'BudgetDenied: run budget exhausted'}}
        self.assertGreater(kapasite_bekleme(butce, simdi), simdi + 3600)
        self.assertIsNone(kapasite_bekleme({'review': {'status': 'FAIL', 'reason': 'DUZELT'}}, simdi))


class OpenRouterUcKesintisiTests(unittest.TestCase):
    def setUp(self):
        from butce_kapisi import BudgetLedger
        from test_butce_donemi import snapshot
        from test_model_kesici import KesiciTabani
        self.fixture = KesiciTabani()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.home = self.fixture.home
        self.ledger = BudgetLedger(self.home / 'zincir/request-budget.db')
        self.snapshot = {**snapshot(5), 'or': {'db': 'ok', 'pay': 5, 'kullanilabilir': 5}}
        self.model = 'deepseek/deepseek-v4-pro'
        self.full_model = 'openrouter/' + self.model
        self.endpoints = ['streamlake/fp8', 'deepinfra/fp8']
        (self.home / 'zincir/openrouter-fiyat.json').write_text(json.dumps({'modeller': {
            self.model: {'providers': self.endpoints}}}))

    def failed_request(self, endpoints, *, code=502, kind='provider_unavailable'):
        from butce_kapisi import saglayici_hatasi_kaydet
        ident = self.ledger.reserve('openrouter', 100, self.snapshot,
                                    {'model': self.model, 'uc_secimi': {'sira': endpoints}})
        saglayici_hatasi_kaydet(self.ledger, {'id': ident, 'kod': code, 'tur': kind,
                                            'mesaj': 'provider temporarily unavailable' if code != 401 else '401 Unauthorized'})
        return ident

    def test_a_known_endpoint_outage_does_not_close_a_healthy_endpoint(self):
        import yonlendirici as yon
        from butce_kapisi import kapali_yol
        ident = self.failed_request(self.endpoints[:1])
        self.assertFalse(yon.cooldownda('openrouter', model=self.full_model))
        self.assertIsNotNone(kapali_yol('openrouter', self.model, self.endpoints[:1]))
        self.assertIsNone(kapali_yol('openrouter', self.model, self.endpoints[1:]))
        self.failed_request(self.endpoints[1:])
        self.assertTrue(yon.cooldownda('openrouter', model=self.full_model))
        self.assertFalse(yon.cooldownda('openrouter'), 'two endpoints of one model are not two failed models')
        with self.ledger.transaction() as con:
            self.assertEqual(con.execute('select reserved,actual from reservation where id=?', (ident,)).fetchone(), (100, None))
        self.assertFalse(yon.cooldownda('openrouter', model=self.full_model, simdi=time.time() + 1801))

    def test_an_unknown_endpoint_or_an_account_error_retains_the_wider_breaker(self):
        import yonlendirici as yon
        self.failed_request(self.endpoints)
        self.assertTrue(yon.cooldownda('openrouter', model=self.full_model), 'an ambiguous outage cannot be assigned to one endpoint')
        yon.durum_yaz({})
        self.failed_request(self.endpoints[:1], code=401)
        self.assertTrue(yon.cooldownda('openrouter'), 'an account error still applies to every endpoint')

    def test_the_reservation_boundary_checks_the_endpoints_actually_selected(self):
        import io

        import butce_kapisi as budget
        self.failed_request(self.endpoints[:1])
        env = {'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(LIB.parent), 'CADRE_RUN_ID': ''}
        for endpoints, expected in ((self.endpoints[:1], 75), (self.endpoints, 75), (self.endpoints[1:], 0)):
            with self.subTest(endpoints=endpoints), patch.dict(os.environ, env), \
                    patch.object(budget, 'observed_budget', return_value=self.snapshot) as observation, \
                    patch.object(sys, 'stdin', io.StringIO(json.dumps({'action': 'reserve', 'provider': 'openrouter',
                        'maximum': 100, 'metadata': {'model': self.model, 'uc_secimi': {'sira': endpoints}}}))), \
                    patch.object(sys, 'stdout', io.StringIO()), patch.object(sys, 'stderr', io.StringIO()):
                self.assertEqual(budget.main(), expected)
                if expected == 75:
                    observation.assert_not_called()


SAHTE_SAGLAYICI = r'''
import { readFileSync, appendFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
const [kok, katalogYolu, model, kayit] = process.argv.slice(2);
const katalog = JSON.parse(readFileSync(katalogYolu, 'utf8'));
const [saglayici, ...parca] = model.split('/');
const id = parca.join('/');
const hata = { code: 502, message: 'Flex processing is temporarily unavailable. Please try again later or use standard processing.',
  metadata: { error_type: 'provider_unavailable' } };
let cagri = 0;
const config = { provider: { [saglayici]: { options: { fetch: async () => {
  cagri += 1;
  appendFileSync(kayit, JSON.stringify({ cagri, ts: Date.now() / 1000 }) + '\n');
  return new Response(`data: ${JSON.stringify({ id: `gen-sahte-${cagri}`, error: hata })}\n\n`, { headers: { 'content-type': 'text/event-stream' } });
} } } } };
const hooks = [await (await import(pathToFileURL(join(kok, 'lib', 'cadre-plugin.mjs')).href)).default()];
await hooks[0].config(config);
const m = katalog[saglayici].models[id];
for (let deneme = 0; deneme < 4; deneme++) {
  const input = { sessionID: 'ses_sahte_kesinti', agent: 'build', model: { providerID: saglayici, id, api: { id }, limit: m.limit, cost: m.cost } };
  const params = { maxOutputTokens: 32768 };
  for (const k of hooks) await k['chat.params']?.(input, params);
  const headers = { headers: {} };
  for (const k of hooks) await k['chat.headers']?.(input, headers);
  try {
    const res = await config.provider[saglayici].options.fetch('https://fixture.invalid/chat/completions', { method: 'POST',
      headers: headers.headers, body: JSON.stringify({ model: id, max_tokens: params.maxOutputTokens, stream: true,
      messages: [{ role: 'user', content: 'plan incele' }] }) });
    await res.text();
  } catch (error) {
    console.log(JSON.stringify({ type: 'error', error: { message: String(error.message) } }));
    process.exit(1);
  }
}
process.exit(1);
'''


class Code29DizisiTests(unittest.TestCase):
    def test_kesinti_tekrari_rezerv_dogru_bekleme_tahmini_kapanis_ve_uc_sinifli_ozet(self):
        from test_uctan_uca import E2EFixture, FAKE_OPENCODE
        fixture = E2EFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        betik = fixture.home.parent / 'saglayici-sahte.mjs'
        betik.write_text(SAHTE_SAGLAYICI)
        cagrilar = fixture.home / 'saglayici-cagrilari.jsonl'
        kapi = "if prompt.startswith('PLAN ISTEK UYUMU'):"
        ek = (f"if role == 'plan-review' and os.environ.get('E2E_SAGLAYICI_KESINTISI') == '1':\n"
              f"    import subprocess\n"
              f"    sys.exit(subprocess.run(['node', {str(betik)!r}, os.environ['CADRE_ENGINE_ROOT'], os.environ['CADRE_MODEL_KATALOG'],"
              f" args[args.index('-m') + 1], {str(cagrilar)!r}]).returncode)\n")
        self.assertEqual(FAKE_OPENCODE.count(kapi), 1)
        fixture.fake.write_text(FAKE_OPENCODE.replace(kapi, ek + kapi))
        (fixture.home / 'zincir/openrouter-fiyat.json').write_text(json.dumps({'version': 2, 'ts': time.time(), 'modeller': {
            'openai/gpt-6.1-sol': {'input': 1, 'output': 5, 'providers': ['openai'], 'fp8_cache': True}}}))
        env = {**fixture.env, 'CADRE_ZINCIR_REVIEW': SOL, 'E2E_SAGLAYICI_KESINTISI': '1'}
        command = [sys.executable, str(fixture.root / 'lib/kosu.py')]

        def cli(*args, data=None, timeout=60):
            return subprocess.run([*command, *args], input=json.dumps(data) if data is not None else None,
                                  text=True, capture_output=True, env=env, timeout=timeout)

        raw = dict(submission_id='kesinti', project='e2e', target_path=str(fixture.repo), target_kind='existing', input_mode='goal',
                   prompt=fixture.PROMPT, output='Working a.py and b.py', acceptance='Both files compile and contain TAMAM',
                   preference='economic', difficulty_override='easy', duration_minutes='60', budget_usd='3', backlog_path='', source_token='')
        submitted = cli('request', data=raw)
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        rid = json.loads(submitted.stdout)['run_id']
        proposal = cli('request-proposal', rid)
        self.assertEqual(proposal.returncode, 0, proposal.stderr)
        proposal = json.loads(proposal.stdout)
        self.assertIn(SOL, proposal['action']['models']['review'])
        first = cli('request-approve', rid, data=proposal)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = cli('request-approve', rid, json.loads(first.stdout)['confirmation'], data=proposal)
        self.assertEqual(second.returncode, 0, second.stderr)
        planned = cli('request-plan', rid, timeout=120)
        self.assertEqual(planned.returncode, 78, (planned.stdout[-800:], planned.stderr[-800:], fixture.store.read(rid).get('reason')))

        kayit = fixture.store.read(rid)
        self.assertTrue(kayit['reason'].startswith(f'saglayici-bekleme: {SOL}'), kayit['reason'])
        self.assertEqual(kayit['planning']['status'], 'provider-wait')
        ilk_bitis = float(json.loads((fixture.home / 'zincir/saglayici-durum.json').read_text())[SOL]['uclar']['openai']['cooldown_bitis'])
        aktif = float((kayit.get('request_clock') or {}).get('aktif_sn') or 0)
        cli('run', rid, timeout=120)
        kayit = fixture.store.read(rid)
        sagla = [json.loads(s) for s in cagrilar.read_text().splitlines()]
        self.assertEqual(len(sagla), 1, 'OpenCode tekrari kapali yola ikinci istek gondermez')
        durum = json.loads((fixture.home / 'zincir/saglayici-durum.json').read_text())
        bitis = float(durum[SOL]['uclar']['openai']['cooldown_bitis'])
        self.assertGreater(bitis, time.time())
        self.assertFalse(float(durum[SOL].get('cooldown_bitis') or 0) > time.time(), 'bilinen FLEX kesintisi butun GPT modelini kapatmaz')
        self.assertFalse(float((durum.get('openrouter') or {}).get('cooldown_bitis') or 0) > time.time(), 'butun saglayici kapanmaz')
        self.assertEqual(kayit['state'], 'retryable', (kayit.get('reason'), kayit.get('planning'), [(r.get('status'), r.get('reason')) for r in kayit.get('plan_reviews') or []], kayit.get('retry_after')))
        self.assertTrue(kayit['reason'].startswith(f'saglayici-bekleme: {SOL}'), kayit['reason'])
        self.assertNotIn('BudgetDenied', kayit['reason'])
        self.assertAlmostEqual(float(kayit['retry_after']), bitis, delta=1)
        self.assertEqual(kayit['plan_reviews'][-1]['status'], 'ERROR')
        self.assertEqual(kayit['planning']['status'], 'provider-wait', 'inceleme yapilamamasi plan reddi degildir')

        import sqlite3
        defter = fixture.home / 'zincir/request-budget.db'
        from contextlib import closing
        with closing(sqlite3.connect(defter.resolve().as_uri() + '?mode=ro', uri=True)) as con:
            satirlar = [(r, a, rc, json.loads(m)) for r, a, rc, m in con.execute(
                "select reserved,actual,receipt,metadata from reservation where json_extract(metadata,'$.run_id')=?", (rid,))]
            clock = con.execute('select started,deadline,calls,tools from review_control where run_id=? and phase=?',
                                (rid, 'plan-review')).fetchone()
            pause = con.execute('select paused_at from review_pause where run_id=? and phase=?',
                                (rid, 'plan-review')).fetchone()
        self.assertIsNotNone(pause, 'saglayici beklemesi yalniz ana is saatini degil inceleme fazini da durdurur')
        self.assertIsNotNone(pause[0])
        self.assertEqual(clock[2:], (1, 0), 'kesinti model istegini veya arac sayacini sifirlamaz')
        from butce_kapisi import BudgetLedger
        gelecekte = bitis + 60
        with patch.object(time, 'time', return_value=gelecekte), closing(sqlite3.connect(
                defter.resolve().as_uri() + '?mode=ro', uri=True)) as con:
            after_wait = BudgetLedger(defter).review_context(con, rid, 'plan-review-2-abcdef01', 'review')
        self.assertAlmostEqual(after_wait['deadline'] - gelecekte, clock[1] - pause[0], places=3)
        sol = [s for s in satirlar if s[3].get('model') == 'openai/gpt-6.1-sol']
        self.assertEqual(len(sol), 1, 'kapali yolda belirsiz rezerv birikmez')
        self.assertIsNone(sol[0][1], '502 sifir ucret kaniti sayilmaz; rezerv korunur')
        self.assertEqual(sol[0][3]['saglayici_hatasi']['tur'], 'provider_unavailable')
        sinir = max(sol[0][0], sol[0][3].get('en_kotu') or 0, sol[0][3].get('verified_bound_micro') or 0)
        self.assertEqual(bitis, ilk_bitis, 'yeniden baslatma kesici suresini yenilemez')
        self.assertGreaterEqual(float(kayit['request_clock']['aktif_sn']), aktif, 'yeniden baslatma sureyi sifirlamaz')
        self.assertEqual(int(kayit.get('retry_count') or 0), 0, 'kayitli bekleme deneme hakki yakmaz')

        import butce_kapisi
        import proje
        with patch.dict(os.environ, {'CADRE_HOME': str(fixture.home)}), \
             patch.object(butce_kapisi.time, 'time', return_value=time.time() + 31 * 60):
            kapanis = butce_kapisi.hayalet_kapat(fixture.home, yas_dk=30)
        self.assertIn(rid, kapanis['kosular'])
        m = proje.proje_maliyeti(fixture.home, [rid])
        self.assertEqual(m['tahmini_usd'], round(sinir / 1e6, 6), 'hayalet kapanis en kotu sinirdan TAHMINI sinifta')
        self.assertEqual(m['askida_usd'], 0.0)
        self.assertEqual(m['yukumluluk_usd'], round(m['olculen_usd'] + m['tahmini_usd'], 6))
        ozet = proje.hesap_ozeti(m, 3.0)
        self.assertEqual(ozet['kalan_usd'], round(3.0 - m['yukumluluk_usd'], 6))
        self.assertIn('tahmini $', proje._harcama_satiri({'maliyet': m}))


if __name__ == '__main__':
    unittest.main()
