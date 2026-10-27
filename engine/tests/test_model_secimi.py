"""D16: measurement and price cannot override the policy or the health/budget filtering."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import model_secimi as secim


class ModelSecimiTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.now = 1000000.0
        self.go = 'opencode-go/deepseek-v4-pro'
        self.ds = 'deepseek/deepseek-v4-pro'
        self.or_ = 'openrouter/deepseek/deepseek-v4-pro'
        self.kimi = 'opencode-go/kimi-k3'
        self.glm = 'opencode-go/glm-5.3'

    def sirala(self, adaylar, tercih=None, rol='impl'):
        with patch.object(secim, 'katalog_model', return_value={'tool_call': True}):
            return secim.sirala(adaylar, tercih or adaylar, rol, 30000, self.home, self.now)[0]

    def test_same_model_uses_cheapest_healthy_route_not_largest_wallet(self):
        with patch.object(secim, 'fiyat', side_effect=lambda provider, model:
                          {'input': 1, 'output': 2} if provider == 'deepseek' else {'input': 3, 'output': 4}):
            self.assertEqual(self.sirala([self.or_, self.ds, self.go]), [self.go, self.ds, self.or_])
            self.assertEqual(self.sirala([self.or_, self.ds]), [self.ds, self.or_])
            self.assertEqual(self.sirala([self.or_], [self.go, self.ds, self.or_]), [self.or_])

    def test_unknown_or_invalid_price_does_not_beat_known_price(self):
        for bad in (None, {'input': -1, 'output': 1}, {'input': math.nan, 'output': 1}):
            with self.subTest(price=bad), patch.object(secim, 'fiyat', side_effect=lambda p, m:
                    {'input': 1, 'output': 2} if p == 'deepseek' else bad):
                self.assertEqual(self.sirala([self.or_, self.ds]), [self.ds, self.or_])

    def test_model_identity_precedes_price_and_groups_all_its_routes(self):
        kimi_zen = 'opencode/kimi-k3'
        with patch.object(secim, 'fiyat', return_value={'input': 1, 'output': 2}):
            self.assertEqual(self.sirala([self.glm, kimi_zen, self.kimi],
                                        [self.kimi, self.glm, kimi_zen], rol='review'),
                             [self.kimi, kimi_zen, self.glm])

    def test_comparable_measurements_are_reported_without_changing_model_policy(self):
        secim.olcum_yaz(self.home, self.kimi, 'hakem', 2, 5, 1, self.now)
        secim.olcum_yaz(self.home, self.glm, 'hakem', 5, 5, 2, self.now)
        self.assertEqual(self.sirala([self.kimi, self.glm], rol='review'), [self.kimi, self.glm])
        self.assertEqual(self.sirala([self.kimi, self.glm], rol='impl'), [self.kimi, self.glm])
        claude = 'claude/opus#high'
        secim.olcum_yaz(self.home, claude, 'genel', 0, 3, 10, self.now)
        secim.olcum_yaz(self.home, self.kimi, 'genel', 3, 3, 1, self.now)
        self.assertEqual(self.sirala([claude, self.kimi, self.glm], rol='plan'), [claude, self.kimi, self.glm])
        _, reasons = secim.sirala([self.kimi, self.glm], [self.kimi, self.glm], 'review', 30000, self.home, self.now)
        self.assertTrue(any('olcum 5/5' in reason for reason in reasons))

    def test_stale_future_and_corrupt_measurements_do_not_change_policy(self):
        secim.olcum_yaz(self.home, self.kimi, 'hakem', 0, 5, 1, self.now - secim.OLCUM_OMRU - 1)
        secim.olcum_yaz(self.home, self.glm, 'hakem', 5, 5, 1, self.now + 1)
        self.assertEqual(secim.olcumler(self.home, 'review', self.now), {})
        path = self.home / 'zincir/model-olcumleri.json'
        for invalid in ('not json', '[]', '{"surum":1,"olcumler":[null,{},3]}'):
            path.write_text(invalid)
            self.assertEqual(self.sirala([self.kimi, self.glm], rol='review'), [self.kimi, self.glm])

    def test_invalid_measurements_cannot_be_written(self):
        for correct, total, seconds in ((-1, 5, 1), (6, 5, 1), (0, 0, 1), (True, 5, 1), (1, 5, math.inf)):
            with self.subTest(correct=correct, total=total), self.assertRaises(ValueError):
                secim.olcum_yaz(self.home, self.kimi, 'hakem', correct, total, seconds)

    def test_explicit_no_tools_capability_excludes_route(self):
        with patch.object(secim, 'katalog_model', return_value={'tool_call': False}):
            models, reasons = secim.sirala([self.go], [self.go], 'impl', 100, self.home, self.now)
        self.assertEqual(models, [])
        self.assertIn('arac cagrisi desteklenmiyor', reasons[0])

    def test_router_wires_price_order_after_its_budget_and_health_filters(self):
        import yonlendirici as yon
        from test_kosu_sessions import KATALOG
        from test_gece_senaryolar import butce
        catalog = self.home / 'catalog.json'
        catalog.write_text(json.dumps(KATALOG))
        with patch.dict(os.environ, {'CADRE_HOME': str(self.home), 'CADRE_MODEL_KATALOG': str(catalog),
                                     'CADRE_RUN_ID': '', 'CADRE_GOREV_ID': ''}), \
                patch.object(yon, 'QE', self.home), patch.object(yon, 'kayit'), \
                patch.object(yon, 'DURUM', self.home / 'state.json'):
            models, _ = yon.zincir('impl', istek_token=1000, b=butce(), d={})
            self.assertIn(self.go, models)
            self.assertEqual(models[0], self.go)
            models, _ = yon.zincir('impl', istek_token=1000, b=butce(),
                                  d={'opencode-go': {'cooldown_bitis': self.now + 100}}, simdi=self.now)
            self.assertNotIn(self.go, models)


class TaskSelectionTests(unittest.TestCase):
    def setUp(self):
        from test_kosu_sessions import SessionTests
        import yonlendirici as yon
        self.fixture = SessionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.home, self.store = self.fixture.home, self.fixture.store
        self.fixture.fake.write_text(self.fixture.fake.read_text().replace("else 'ses_first'", "else 'ses_' + os.environ['CADRE_RUN_ID']"))
        self.yon = yon
        self.fixture.enter(patch.object(yon, 'DURUM', self.home / 'zincir/state.json'))
        self.fixture.enter(patch.object(yon, 'KAYIT', self.home / 'zincir/router.jsonl'))
        import qe_core as core
        self.fixture.enter(patch.object(yon, 'butce_oku', side_effect=core.butce_json))
        self.fixture.enter(patch.dict(os.environ, {'CADRE_ZINCIR_IMPL': '', 'CADRE_ZINCIR_IMPL_HAFIF': '',
                                                  'CADRE_CLAUDE_PLAN': '0', 'CADRE_GOREV_ID': 'g1', 'CADRE_KOSU_TAVAN_USD': ''}))

    def accept(self, preference, difficulty, prompt='README.md yazim hatasini duzelt'):
        from kosu import gorev_varsayilanlari, gorev_varsayilanlari_yaz
        gorev_varsayilanlari_yaz(self.home, preference, difficulty, gorev_varsayilanlari(self.home))
        record = self.store.accept('a', prompt)
        self.fixture.enter(patch.dict(os.environ, self.store.environment(record['run_id'])))
        return record

    def chain(self, record, role='impl', **kwargs):
        import qe_core as core
        with patch.dict(os.environ, {'CADRE_RUN_ID': record['run_id']}):
            return self.yon.zincir(role, b=core.butce_json(), d={}, **kwargs)

    def test_all_preferences_and_difficulties_reach_the_executor_and_attempt_record(self):
        import qe_core as core
        for preference in ('economic', 'balanced', 'strong'):
            for difficulty in ('easy', 'hard'):
                with self.subTest(preference=preference, difficulty=difficulty):
                    record = self.accept(preference, difficulty)
                    expected = 'opencode-go/glm-5.3' if difficulty == 'easy' and preference != 'strong' else 'opencode-go/deepseek-v4-pro'
                    self.assertEqual(self.yon.zorluk_modeli('opencode-go/deepseek-v4-pro', 3), expected)
                    proc = core.run_opencode(str(self.fixture.fake), str(self.fixture.wt), ['bounded task'], self.fixture.log)
                    self.assertEqual(proc.wait(timeout=10), 0)
                    args = self.fixture.calls()[-1]
                    self.assertEqual(args[args.index('-m') + 1], expected)
                    attempts = list(self.store.task_dir(record['run_id'], 'g1').glob('attempts/*/attempt.json'))
                    self.assertEqual(len(attempts), 1)
                    self.assertEqual(json.loads(attempts[0].read_text())['model'], expected)
                    decision = [json.loads(line) for line in self.yon.KAYIT.read_text().splitlines() if json.loads(line).get('run_id') == record['run_id']][-1]
                    self.assertEqual(decision['selection']['preference'], preference)
                    self.assertEqual(decision['selection']['difficulty_effective'], difficulty)
                    self.assertEqual(decision['selection']['policy_version'], secim.POLITIKA_SURUMU)

    def test_cr50_economic_effort_is_opt_in_versioned_recorded_and_only_for_a_first_easy_lane(self):
        import qe_core as core
        import sinirlar
        fake = self.fixture.fake.read_text().replace("else 'ses_' + os.environ['CADRE_RUN_ID']",
                                                     "else 'ses_' + os.environ['CADRE_RUN_ID'] + os.environ.get('CADRE_GOREV_ID', '')")
        self.fixture.fake.write_text(fake + "\nwith (p/'efor.jsonl').open('a') as f: "
                                     "f.write(json.dumps([os.environ.get('CADRE_EFOR'), os.environ.get('CADRE_EFOR_POLITIKASI')])+'\\n')\n")
        seen = lambda: json.loads((self.home / 'efor.jsonl').read_text().splitlines()[-1])
        def run(role='impl'):
            proc = core.run_opencode(str(self.fixture.fake), str(self.fixture.wt), ['bounded task'], self.fixture.log, role=role)
            self.assertEqual(proc.wait(timeout=10), 0)
            return proc
        record = self.accept('economic', 'easy')
        run()
        self.assertEqual(seen(), [None, None], 'olculmemis ayar varsayilan degildir')
        with patch.dict(sinirlar.EFOR, {'surum': 'ekonomik-efor-v1', 'kolay_serit': 'low'}), \
             patch.dict(os.environ, {'CADRE_ZINCIR_KALITE': 'opencode-go/qwen3.8-max'}):
            proc = run()
            self.assertEqual(seen(), ['low', 'ekonomik-efor-v1'])
            policy = {'politika': 'ekonomik-efor-v1', 'seviye': 'low'}
            self.assertEqual(proc.attempt.data['efor_politikasi'], policy)
            self.assertEqual(self.store.read(record['run_id'])['tasks']['g1']['efor_politikasi'], policy)
            for role in ('plan', 'kalite', 'qa', 'review'):
                with patch.dict(os.environ, {'CADRE_GOREV_ID': 'g-' + role}):
                    run(role)
                self.assertEqual(seen(), [None, None], role)
            for gorev in ('duzeltme-1', 'birlestirme'):
                with patch.dict(os.environ, {'CADRE_GOREV_ID': gorev}):
                    run()
                self.assertEqual(seen(), [None, None], gorev)
            self.store.task_update(record['run_id'], 'g1', failure_count=1)
            run()
            self.assertEqual(seen(), [None, None], 'hata sonrasi tekrar varsayilan efora doner')
            for preference, difficulty in (('strong', 'easy'), ('economic', 'hard'), ('balanced', 'medium')):
                self.accept(preference, difficulty)
                run()
                self.assertEqual(seen(), [None, None], (preference, difficulty))
            self.accept('balanced', 'easy')
            run()
            self.assertEqual(seen(), ['low', 'ekonomik-efor-v1'])

    def test_saved_selection_survives_new_defaults_restart_and_plan_estimate(self):
        import subprocess
        from kosu import gorev_varsayilanlari, gorev_varsayilanlari_yaz
        record = self.accept('economic', None)
        selection = record['model_selection']
        self.assertEqual(selection['difficulty_estimate'], 'easy')
        self.assertIsNone(selection['difficulty_override'])
        gorev_varsayilanlari_yaz(self.home, 'strong', 'very_hard', gorev_varsayilanlari(self.home))
        command = [sys.executable, str(Path(secim.__file__).with_name('yonlendirici.py')), 'uygun', '', 'impl', '3']
        cache = self.home / 'zincir/request-budget.json'
        import qe_core as core
        cache.write_text(json.dumps({'observed': __import__('time').time(), 'budget': core.butce_json()}))
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'opencode-go/glm-5.3')
        self.assertEqual(self.store.read(record['run_id'])['model_selection'], selection)
        self.store.update(record['run_id'], kapsam={'puan': 80})
        self.assertEqual(self.chain(record)[0][0], 'opencode-go/deepseek-v4-pro')
        self.assertEqual(secim.kosu_secimi(self.home, record['run_id'])['difficulty_effective'], 'very_hard')
        self.assertEqual(self.store.read(record['run_id'])['model_selection'], selection)
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.store.update(record['run_id'], model_selection={})

    def test_claude_call_difficulty_keeps_the_saved_preference_even_with_explicit_caller_level(self):
        import claude_politikasi as policy
        for preference, difficulty, expected in (('economic', 'easy', 'claude/sonnet#high'),
                                                ('strong', 'easy', 'claude/opus#xhigh'),
                                                ('balanced', 'very_hard', 'claude/opus#max')):
            self.accept(preference, difficulty)
            level = policy.difficulty(self.home, 'plan', 3)
            self.assertEqual(policy.model('claude/opus#high', {'claude': {'pay': 10}}, level, 0), expected)

    def test_parent_selection_is_inherited_instead_of_changed_global_defaults(self):
        from kosu import gorev_varsayilanlari, gorev_varsayilanlari_yaz
        record = self.accept('economic', 'easy')
        gorev_varsayilanlari_yaz(self.home, 'strong', 'hard', gorev_varsayilanlari(self.home))
        import proje
        def execute(store, rid):
            store.update(rid, state='implemented')
            return 0
        child = proje.cocuk_calistir_gercek(self.store, record['run_id'], 'child', 'isolated feature', '', calistir=execute)
        self.assertEqual(child['model_selection']['preference'], 'economic')
        self.assertEqual(child['model_selection']['difficulty_override'], 'easy')
        self.assertEqual(child['model_selection']['parent_run_id'], record['run_id'])
        self.assertEqual(self.chain(child)[0][0], 'opencode-go/glm-5.3')
        resumed = proje.cocuk_calistir_gercek(self.store, record['run_id'], 'child', 'isolated feature', '', calistir=execute)
        self.assertEqual(resumed['run_id'], child['run_id'])

    def test_corrupt_unknown_policy_and_unreadable_run_never_launch_an_executor(self):
        import qe_core as core
        record = self.accept('economic', 'easy')
        path = self.store.path(record['run_id']) / 'run.json'
        for value in ({}, None, {**record['model_selection'], 'policy_version': 'future'},
                      {**record['model_selection'], 'difficulty_effective': 'hard'}):
            path.write_text(json.dumps({**record, 'model_selection': value}))
            self.assertEqual(self.chain(record)[0], [])
            with patch.object(core.OC, 'launch', side_effect=AssertionError('executor forbidden')):
                with self.assertRaises(core.BudgetDenied):
                    core.run_opencode(str(self.fixture.fake), str(self.fixture.wt), ['task'], self.fixture.log)
        path.write_text('{')
        self.assertEqual(self.chain(record)[0], [])
        self.assertFalse((self.home / 'calls.jsonl').exists())

    def test_price_capacity_permission_and_budget_filters_survive_strong_preference(self):
        import qe_core as core
        record = self.accept('strong', 'hard')
        catalog = self.home / 'models.json'
        original = json.loads(catalog.read_text())
        for failure in ('price', 'context', 'output', 'tools', 'account', 'budget', 'cooldown'):
            data = json.loads(json.dumps(original))
            model = data['opencode-go']['models']['deepseek-v4-pro']
            state, budget = {}, core.butce_json()
            if failure == 'price':
                model['cost'] = {'input': -1, 'output': 1}
            elif failure == 'context':
                model['limit']['context'] = 100
            elif failure == 'output':
                model['limit'].pop('output')
            elif failure == 'tools':
                model['tool_call'] = False
            elif failure == 'account':
                (self.home / 'zincir/opencode-go-canli.json').write_text(json.dumps({'modeller': ['glm-5.3']}))
            elif failure == 'budget':
                self.store.update(record['run_id'], budget_cap_usd=0.000001)
            else:
                state = {self.yon.model_anahtari('opencode-go', 'opencode-go/deepseek-v4-pro'): {'cooldown_bitis': __import__('time').time() + 100}}
            catalog.write_text(json.dumps(data))
            with self.subTest(failure=failure):
                chain, reasons = self.yon.zincir('impl', b=budget, d=state)
                self.assertNotIn('opencode-go/deepseek-v4-pro', chain)
                self.assertTrue(reasons)
            (self.home / 'zincir/opencode-go-canli.json').unlink(missing_ok=True)
            self.store.update(record['run_id'], budget_cap_usd=2)
        catalog.write_text(json.dumps(original))
        chain, _ = self.chain(record)
        self.assertEqual(chain[0], 'opencode-go/deepseek-v4-pro')
        self.assertFalse(any(route.startswith(('claude/', 'opencode/')) for route in chain))

    def test_preview_is_read_only_with_pending_reservations_and_progress(self):
        import butce_kapisi as bk
        import qe_core as core
        record = self.accept('economic', 'easy')
        ledger = bk.BudgetLedger(self.home / 'zincir/request-budget.db')
        reservation = ledger.reserve('opencode-go', 1000, core.butce_json(), {'run_id': record['run_id'], 'model': 'glm-5.3'}, run_cap=2_000_000)
        bk.ilerleme_yaz(self.home, record['run_id'], 'qa-pass', 'unchanged-output')
        before = {str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        with patch.object(ledger.__class__, 'transaction', side_effect=AssertionError('preview cannot write')):
            for role in ('impl', 'plan', 'review', 'yazar'):
                chain, _ = self.chain(record, role=role, kaydet=False)
                self.assertTrue(chain, role)
        self.assertEqual(before, {str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob('*') if p.is_file()})
        ledger.settle(reservation, 1000, 'preview-receipt')

    def test_panel_run_cap_is_kept_when_the_real_plan_has_a_higher_estimate(self):
        from kosu import butce_ayari, butce_ayari_yaz
        import seviye
        (self.home / 'projects/a').mkdir(parents=True, exist_ok=True)
        butce_ayari_yaz(self.home, 'kosu', 'a', '0.75', butce_ayari(self.home, 'kosu', 'a'))
        record = self.accept('strong', 'hard')
        self.assertTrue(record['budget_manual'])
        (self.store.path(record['run_id']) / 'plan.json').write_text(json.dumps({'kapsam_puani': 80, 'gorevler': [{'tahmin_usd': 8}]}))
        seviye.uygula(record['run_id'], 'a')
        self.assertEqual(self.store.read(record['run_id'])['budget_cap_usd'], 0.75)
        self.assertEqual(self.store.read(record['run_id'])['budget_spent_usd'], 0)


if __name__ == '__main__':
    unittest.main()


class EtkinCiktiSiniriTest(unittest.TestCase):
    def test_selection_uses_min_of_role_and_verified_model_output_and_rejects_unknown_capacity(self):
        home = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='etkin-cikti-')))
        model = {'limit': {'context': 200000, 'output': 32000}, 'cost': {'input': 1, 'output': 2}}
        with patch.object(secim, 'katalog_model', return_value=model):
            self.assertEqual(secim.cagri_tavani('opencode/kucuk', 1000, 32768, home), 1000 + 2 * 32000)
            self.assertEqual(secim.cagri_tavani('opencode/kucuk', 1000, 8000, home), 1000 + 2 * 8000)
        for limit in ({'context': 200000}, {'context': 200000, 'output': 0}, {'context': 200000, 'output': '32000'}):
            with self.subTest(limit=limit), patch.object(secim, 'katalog_model', return_value={**model, 'limit': limit}):
                with self.assertRaisesRegex(ValueError, 'cikti kapasitesi'):
                    secim.cagri_tavani('opencode/kucuk', 1000, 32768, home)
        dar = {**model, 'limit': {'context': 32500, 'output': 32000}}
        with patch.object(secim, 'katalog_model', return_value=dar), self.assertRaisesRegex(ValueError, 'baglam kapasitesi'):
            secim.cagri_tavani('opencode/kucuk', 1000, 32768, home)
