"""P6 acceptance guarantee (06.10 smoke test): the role output cap and the automatic raise of a small cap."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import test_kosu_sessions as fixtures  # noqa: E402
import qe_core as core  # noqa: E402
import kabul  # noqa: E402
from butce_kapisi import BudgetDenied  # noqa: E402
from test_gece_bekleme import full_budget  # noqa: E402

IMPL, HAKEM = 'opencode-go/deepseek-v4-pro', 'opencode-go/glm-5.3'


def zincir(rol, *_a, **_k):
    return ([HAKEM, 'opencode-go/qwen3.8-max'] if rol == 'kalite' else [IMPL]), []


class KabulGuvencesiTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.enter(patch.object(core.YON, 'zincir', side_effect=zincir))
        b = full_budget(cloud=True)
        b['go'].update(kullanilabilir=50.0, pay=50.0)
        self.f.enter(patch.object(core, 'butce_json', return_value=b))
        self.log = self.f.home / 'guvence.log'

    def beklenen_toplam(self, tam_cikti=False):
        from katalog import model as km
        usd = 0.0
        for rol, m, adet in (('kalite', HAKEM, 2), ('qa', IMPL, 1)):
            sag, mid = m.split('/', 1)
            cost, lim = km(sag, mid)['cost'], km(sag, mid)['limit']
            ctx = min(int(lim.get('input') or lim['context']), core.YON.rol_baglami(rol))
            yogun = 2 if mid.startswith('deepseek') else 1   # Go DeepSeek reservation at the peak price (4c-13)
            cikti = int(lim['output']) if tam_cikti else min(kabul.ROL_CIKTI[rol], int(lim['output']))
            usd += adet * yogun * (ctx * float(cost['input']) + cikti * float(cost['output'])) / 1e6
        return usd

    def test_guarantee_uses_role_output_caps_not_the_full_output_limit(self):
        self.f.store.update(self.f.rid, budget_cap_usd=50.0)
        secim = kabul.kabul_guvencesi(self.log)
        self.assertEqual(len(secim['rezervasyonlar']), 3)
        taban = self.f.store.read(self.f.rid)['kabul_guvence_taban_micro'] / 1e6
        self.assertAlmostEqual(taban, self.beklenen_toplam(), places=4)
        self.assertLess(taban, self.beklenen_toplam(tam_cikti=True), 'rol tavani modelin tam cikti sinirindan kucuk')

    def test_a_small_automatic_cap_is_raised_so_the_guarantee_fits(self):
        self.f.store.update(self.f.rid, budget_cap_usd=0.05, budget_spent_usd=0.02)
        secim = kabul.kabul_guvencesi(self.log)
        self.assertEqual(len(secim['rezervasyonlar']), 3)
        r = self.f.store.read(self.f.rid)
        self.assertGreaterEqual(r['budget_cap_usd'] * 0.6 + 1e-6, self.beklenen_toplam() + 0.02, 'guvence + harcanan tavanin %60i')
        self.assertIn('P6: kosu tavani', self.log.read_text())

    def test_a_resumed_run_with_a_recorded_guarantee_still_gets_its_cap_raised(self):
        """06.10 smoke test, 2nd attempt: with the guarantee recorded, the cap was not raised in a continuing run and the lanes fell to waiting again."""
        self.f.store.update(self.f.rid, budget_cap_usd=50.0)
        kabul.kabul_guvencesi(self.log)
        self.f.store.update(self.f.rid, budget_cap_usd=0.05, budget_spent_usd=0.4)
        kabul.kabul_guvencesi(self.log)
        r = self.f.store.read(self.f.rid)
        self.assertGreaterEqual(r['budget_cap_usd'] * 0.6 + 1e-6, self.beklenen_toplam() + 0.4)

    def test_a_manual_cap_that_cannot_hold_the_guarantee_is_denied(self):
        self.f.store.update(self.f.rid, budget_cap_usd=0.05, budget_manual=True)
        with self.assertRaises(BudgetDenied):
            kabul.kabul_guvencesi(self.log)
        self.assertEqual(self.f.store.read(self.f.rid).get('kabul_guvence_taban_micro', 0), 0)
        self.assertEqual(self.f.store.read(self.f.rid)['budget_cap_usd'], 0.05)


    def test_released_guarantee_leaves_no_held_money_in_the_run_record(self):
        """07.10 smoke: when the run ended the guarantee closed in the ledger but run.json kept showing $0.18 pending + a warning."""
        from butce_kapisi import BudgetLedger, kosu_harcama_yaz
        self.f.store.update(self.f.rid, budget_cap_usd=50.0)
        kabul.kabul_guvencesi(self.log)
        ledger = BudgetLedger(self.f.home / 'zincir/request-budget.db')
        kosu_harcama_yaz(self.f.home, ledger, self.f.rid)
        self.assertGreater(self.f.store.read(self.f.rid)['budget_held_usd'], 0)
        ledger.guvence_birak(self.f.rid)
        r = self.f.store.read(self.f.rid)
        self.assertEqual((r['budget_held_usd'], r['budget_unsettled']), (0, []))

    def test_plan_reserve_holds_one_call_on_a_cash_path_and_is_released_after_planning(self):
        """4b (07.10 user): at run start one plan call is reserved on Zen/OR (without relying on Claude/Go); refunded when the plan ends."""
        import sqlite3
        b = full_budget(cloud=True)
        b['go'].update(kullanilabilir=50.0, pay=50.0)
        b['zen'] = {'db': 'ok', 'pay': 9.0, 'kullanilabilir': 9.0, 'kalan': 9.0}
        self.f.store.update(self.f.rid, budget_cap_usd=50.0)
        plan_zinciri = (['opencode-go/qwen3.8-max', 'opencode/kimi-k3'], [])
        with patch.object(core, 'butce_json', return_value=b), patch.object(core.YON, 'zincir', return_value=plan_zinciri):
            secim = kabul.plan_guvencesi(self.log)
        self.assertEqual(secim['model'], 'opencode/kimi-k3', 'Go degil, nakit yol (Zen/OR)')
        db = self.f.home / 'zincir/request-budget.db'
        from contextlib import closing
        def satir():
            with closing(sqlite3.connect(db)) as con:
                return con.execute("select actual, json_extract(metadata,'$.guvence_rol') from reservation where id=?",
                                   (secim['rezervasyon'],)).fetchone()
        self.assertEqual(satir(), (None, 'plan'))
        # 07.10 T3 live: the reserve filled the lane pocket and got the primary plan call (Go) denied; the reserve is not charged to the run pocket
        from butce_kapisi import BudgetLedger, rol_butcesi
        self.f.store.update(self.f.rid, budget_cap_usd=2.0)
        cap, dilim = rol_butcesi(self.f.home, self.f.rid, 'plan')
        import butce_kapisi
        self.assertAlmostEqual(butce_kapisi.run_remaining_usd(self.f.home, self.f.rid, 'plan'), cap / 1e6, places=4,
                               msg='on kontrol de plan rezervini cepten dusmez (kapiyla ayni muhasebe)')
        self.assertGreaterEqual(BudgetLedger(db).available('opencode-go', b, cap, self.f.rid, dilim, rol='plan', model='qwen3.8-max'),
                                cap - 10_000)
        BudgetLedger(db).reserve('opencode-go', cap - 10_000, b, {'run_id': self.f.rid, 'phase_role': 'plan', 'model': 'qwen3.8-max'}, cap, dilim)
        kabul.plan_guvencesi_birak()
        self.assertEqual(satir(), (0, 'plan'))
        self.assertTrue(self.f.store.read(self.f.rid)['plan_guvencesi']['birakildi'])

    def test_every_terminal_path_releases_the_unused_guarantee(self):
        """07.10: when a run without a coordinator was cancelled with `cadre-iptal` the acceptance guarantee was not refunded; live, 3 lines
        ($1.67) stayed pending and `cadre kurtar` could not close them because it did not see guarantee lines. Every terminal path refunds."""
        import sqlite3
        from contextlib import closing
        import kosu
        self.f.store.update(self.f.rid, budget_cap_usd=50.0)
        kabul.kabul_guvencesi(self.log)
        def askida():
            with closing(sqlite3.connect(self.f.home / 'zincir/request-budget.db')) as con:
                return con.execute("select count(*) from reservation where actual is null").fetchone()[0]
        self.assertEqual(askida(), 3)
        self.f.store.update(self.f.rid, state='retryable', owner_pid=None)
        self.assertTrue(kosu.iptal_et(self.f.store, self.f.store.read(self.f.rid)))
        self.assertEqual(self.f.store.read(self.f.rid)['state'], 'cancelled')
        self.assertEqual(askida(), 0)
        self.assertEqual(self.f.store.read(self.f.rid)['budget_held_usd'], 0)


class HedefliQaTests(unittest.TestCase):
    def test_son_kabul_qa_takilan_ozelliklerin_komutlarini_da_kosar(self):
        self.assertEqual(kabul.ek_kabul_ile('make check', {'ek_kabul_komutlari': ['pytest -q t.py', ' ']}), '(make check) && (pytest -q t.py)')
        self.assertEqual(kabul.ek_kabul_ile('make check', {}), 'make check')
        self.assertEqual(kabul.ek_kabul_ile('', {'ek_kabul_komutlari': ['pytest -q t.py']}), '(pytest -q t.py)')

    def test_pytest_projects_run_selected_tests_with_pytest_from_the_repo_root(self):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            wt = Path(d)
            git = lambda *a: subprocess.run(['git', '-C', d, *a], check=True, capture_output=True, text=True).stdout.strip()
            git('init', '-q'); git('config', 'user.email', 't@t'); git('config', 'user.name', 't')
            (wt / 'foo.py').write_text('X = 1\n')
            git('add', '-A'); git('commit', '-qm', 'taban')
            taban = git('rev-parse', 'HEAD')
            (wt / 'tests').mkdir()
            (wt / 'tests/test_foo.py').write_text('import unittest\n\nclass T(unittest.TestCase):\n    def test_value(self):\n        assert 1\n')
            kayit = {'source_sha': taban, 'feature_acceptance': {}}
            self.assertIn('-m unittest test_foo', kabul.hedefli_qa_komutu(kayit, wt))
            (wt / 'tests/test_foo.py').write_text('def test_value():\n    assert 1\n')
            self.assertIn('-m pytest -q tests/test_foo.py', kabul.hedefli_qa_komutu(kayit, wt), 'ayarsiz pytest bicimli test')
            (wt / 'tests/test_foo.py').write_text('import unittest\n\nclass T(unittest.TestCase):\n    def test_value(self):\n        assert 1\n')
            (wt / 'pyproject.toml').write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
            komut = kabul.hedefli_qa_komutu(kayit, wt)
            self.assertIn('-m pytest -q tests/test_foo.py', komut)
            self.assertNotIn('cd ', komut)


if __name__ == '__main__':
    unittest.main()


SOL = 'openrouter/openai/gpt-6.1-sol'


class PinliGuvenceTests(unittest.TestCase):
    def test_guarantee_matches_the_verified_exclusive_route_and_rejects_unknown_prices(self):
        import json
        import time
        import model_secimi
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / 'zincir').mkdir()
            path = home / 'zincir/openrouter-fiyat.json'
            quote = {'version': 2, 'ts': time.time(), 'modeller': {'openai/gpt-6.1-sol': {
                'input': 1, 'output': 5, 'providers': ['openai'], 'fp8_cache': True}}}
            path.write_text(json.dumps(quote))
            model = fixtures.KATALOG['openrouter']['models']['openai/gpt-6.1-sol']
            with patch.object(core, 'QE', home), patch('katalog.model', return_value=model), \
                 patch.object(model_secimi, 'katalog_model', return_value=model):
                amount = kabul._guvence_tutari('kalite', SOL)
                self.assertEqual(amount, 219_840, 'FLEX 1/5; uncallable 2/10 catalog routes do not enlarge the guarantee')
                self.assertEqual(amount, model_secimi.cagri_tavani(SOL, core.YON.rol_baglami('kalite'), 32768, home))
                quote['ts'] -= 4000
                path.write_text(json.dumps(quote))
                with self.assertRaisesRegex(ValueError, 'OR-PIN'):
                    kabul._guvence_tutari('kalite', SOL)
                quote['ts'] = time.time()
                path.write_text(json.dumps(quote))
                unknown = {**model, 'limit': {k: v for k, v in model['limit'].items() if k != 'output'}}
                with patch('katalog.model', return_value=unknown), patch.object(model_secimi, 'katalog_model', return_value=unknown):
                    with self.assertRaisesRegex(ValueError, 'cikti kapasitesi'):
                        kabul._guvence_tutari('kalite', SOL)
            self.assertFalse((home / 'zincir/request-budget.db').exists())


class HakemBoyutTahminiTests(unittest.TestCase):
    """4c-5: the P6 judge is chosen by the size estimate; if the real size changes the judge the quality guarantee moves."""

    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        zincirler = {'kalite': [SOL, HAKEM, 'opencode-go/qwen3.8-max'], 'impl': [IMPL, HAKEM]}
        self.f.enter(patch.object(core.YON, 'zincir', side_effect=lambda rol, *a, **k: (list(zincirler.get(rol, [IMPL])), [])))
        self.b = full_budget(cloud=True)
        self.b['go'].update(kullanilabilir=50.0, pay=50.0)
        self.b['or'].update(pay=20.0, kullanilabilir=20.0, kalan=20.0)
        self.f.enter(patch.object(core, 'butce_json', return_value=self.b))
        import json
        import time
        (self.f.home / 'zincir/openrouter-fiyat.json').write_text(json.dumps({
            'version': 2, 'ts': time.time(), 'modeller': {'openai/gpt-6.1-sol': {
                'input': 1, 'output': 5, 'providers': ['openai'], 'fp8_cache': True}}}))
        self.f.store.update(self.f.rid, budget_cap_usd=50.0)
        self.log = self.f.home / 'guvence.log'

    def plan(self, dosya_sayisi):
        core.atomic_json(self.f.store.path(self.f.rid) / 'plan.json',
                         {'gorevler': [{'id': f'g{i}', 'dosyalar': [f'f{i}.py']} for i in range(dosya_sayisi)]})

    def acik_kalite(self):
        import json
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.f.home / 'zincir/request-budget.db')) as con:
            return [(p, json.loads(m)['model']) for p, m in con.execute(
                "select provider, metadata from reservation where actual is null "
                "and json_extract(metadata,'$.guvence_rol')='kalite' order by created, id")]

    def test_dosya_listesi_yoksa_buyuk_sayilir_ve_sol_ayrilir(self):
        self.assertFalse(kabul.tahmini_kucuk(self.f.store, self.f.rid))
        secim = kabul.kabul_guvencesi(self.log)
        self.assertEqual((secim['kucuk_tahmin'], secim['modeller']['kalite']), (False, SOL))

    def test_or_onde_ve_or_parasizsa_guvence_siradaki_hakeme_duser(self):
        self.b['or'].update(pay=0.14, kullanilabilir=0.14, kalan=0.14)
        secim = kabul.kabul_guvencesi(self.log)
        self.assertEqual((secim['kucuk_tahmin'], secim['modeller']['kalite']), (False, HAKEM))
        self.assertEqual(self.acik_kalite(), [('opencode-go', 'glm-5.3')] * 2)

    def test_unknown_output_cannot_guarantee_an_openrouter_call_and_uses_the_explicit_alternative(self):
        import katalog
        gercek = katalog.model

        def canli_gibi(sag, mid):
            m = gercek(sag, mid)
            if sag == 'openrouter' and m:
                m = {**m, 'limit': {k: v for k, v in (m.get('limit') or {}).items() if k != 'output'}}
            return m
        with patch.object(katalog, 'model', side_effect=canli_gibi):
            with patch('model_secimi.katalog_model', side_effect=canli_gibi):
                with self.assertRaisesRegex(ValueError, 'cikti kapasitesi'):
                    kabul._guvence_tutari('kalite', SOL)
                secim = kabul.kabul_guvencesi(self.log)
        self.assertEqual(secim['modeller']['kalite'], HAKEM)

    def test_rapor_isinde_qa_guvencesi_ayrilmaz(self):
        gercek = core.RunStore.read
        with patch.object(core.RunStore, 'read', lambda st, rid: {**gercek(st, rid), 'rapor': {'yol': 'R.md', 'bolumler': []}}):
            secim = kabul.kabul_guvencesi(self.log)
        self.assertEqual((len(secim['rezervasyonlar']), secim['modeller'].get('qa')), (2, None))

    def test_bolunmus_planda_on_dosyaya_kadar_kucuk_ve_go_hakem_once(self):
        self.plan(10)
        secim = kabul.kabul_guvencesi(self.log)
        self.assertEqual((secim['kucuk_tahmin'], secim['modeller']['kalite']), (True, HAKEM))
        self.plan(11)
        self.assertFalse(kabul.tahmini_kucuk(self.f.store, self.f.rid))

    def test_yalniz_beklenen_yazarin_ailesi_dusulur(self):
        """The code chain's fallback link (glm) can be the judge as long as it did not write; the real writer drops out dynamically in finalize."""
        self.plan(2)
        self.assertIn(HAKEM, kabul.kabul_guvencesi(self.log)['adaylar'])

    def test_guvence_yeni_hakeme_tasinir_eskisi_birakilir(self):
        self.plan(2)
        kabul.kabul_guvencesi(self.log)
        self.assertEqual(self.acik_kalite(), [('opencode-go', 'glm-5.3')] * 2)
        self.assertTrue(kabul.guvence_tasi(self.f.store, self.f.rid, SOL, self.b, self.log))
        self.assertEqual(self.acik_kalite(), [('openrouter', 'openai/gpt-6.1-sol')] * 2)
        secim = self.f.store.read(self.f.rid)['kabul_guvencesi']
        self.assertEqual((secim['modeller']['kalite'], len(secim['rezervasyonlar'])), (SOL, 3))

    def test_yeni_hakem_sigmazsa_eski_guvence_aynen_kalir(self):
        self.plan(2)
        kabul.kabul_guvencesi(self.log)
        once = self.acik_kalite()
        self.b['or'].update(pay=0.0, kullanilabilir=0.0)
        self.assertFalse(kabul.guvence_tasi(self.f.store, self.f.rid, SOL, self.b, self.log))
        self.assertEqual(self.acik_kalite(), once)
        self.assertEqual(self.f.store.read(self.f.rid)['kabul_guvencesi']['modeller']['kalite'], HAKEM)


class OluGuvenceTests(unittest.TestCase):
    """4c QA (Fable, 07.10): when the guaranteed judge (glm) was removed as being from the writer/planner family and the move did not hold, the dead guarantee
    held the judge pocket until the terminal. If the old model can no longer be the judge it is released first and the new one is reserved."""

    def test_a_dead_guarantee_is_released_before_the_new_judge_reserves(self):
        import datetime
        import sqlite3
        from contextlib import closing
        import butce_kapisi as bk
        with tempfile.TemporaryDirectory() as t:
            db = Path(t) / 'zincir/request-budget.db'
            defter = bk.BudgetLedger(db)
            simdi = datetime.datetime.now()
            anlik = {'ts': simdi.isoformat(), 'bugun': simdi.date().isoformat(),
                     'go': {'db': 'ok', 'pay': 2.0, 'kullanilabilir': 2.0, 'pencere_acik': True, 'birim': 'puan'}}
            meta = lambda m: {'run_id': 'r', 'model': m, 'guvence': True, 'guvence_rol': 'kalite', 'phase_role': 'kalite'}
            eski = [defter.reserve('opencode-go', 100_000, anlik, meta('glm-5.3')) for _ in range(2)]   # 2 x 0,67 puan
            tasi = defter.guvence_tasi('r', 'kalite', 'opencode-go', 100_000, anlik, meta('qwen3.8-max'))
            self.assertEqual((tasi['eski'], len(tasi['yeni'])), (eski, 2))
            repeated = defter.guvence_tasi('r', 'kalite', 'opencode-go', 100_000, anlik, meta('qwen3.8-max'), once_birak=True)
            self.assertEqual(repeated['yeni'], tasi['yeni'])
            with closing(sqlite3.connect(db)) as con:
                acik = con.execute("select json_extract(metadata,'$.model') from reservation where actual is null").fetchall()
            self.assertEqual(sorted(m for (m,) in acik), ['qwen3.8-max', 'qwen3.8-max'])
