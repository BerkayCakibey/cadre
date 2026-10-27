"""Item 0b — THE BUCKET UNIT IS THE MODEL. A free/paid split per provider did not match reality: zen had 32 free
+ 71 paid models under one id, and Go has zero-priced models too. In the old code a zero-priced model was
denied in the plugin and had no place in the ledger. These tests prove the new '<provider>:free' bucket."""

from __future__ import annotations

import os
from contextlib import closing
import sqlite3
import unittest

import test_kosu_sessions as fixtures
import butce_kapisi as budget
from test_butce_donemi import snapshot


class KovaBirimiTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.db = self.f.home / 'zincir/request-budget.db'
        self.ledger = budget.BudgetLedger(self.db)
        self.eski = os.environ.get('CADRE_BEDAVA_GUN_TAVAN')
        self.addCleanup(lambda: os.environ.__setitem__('CADRE_BEDAVA_GUN_TAVAN', self.eski)
                        if self.eski else os.environ.pop('CADRE_BEDAVA_GUN_TAVAN', None))

    def test_zero_cost_models_get_their_own_bucket_and_do_not_touch_dollars(self):
        # Go's dollar share is COMPLETELY full: a paid model is denied, a zero-priced model still works.
        # GO1 (03.10): Go is not split into days; no daily margin — full share = 2.0 * 1e6 = 2_000_000.
        self.ledger.reserve('opencode-go', 2_000_000, snapshot(), {'run_id': 'r1'})
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('opencode-go', 100_000, snapshot(), {'run_id': 'r1'})
        ident = self.ledger.reserve('opencode-go:free', 1, snapshot(), {'run_id': 'r1'})
        self.ledger.settle(ident, 1, 'fis-1')
        with closing(sqlite3.connect(self.db)) as con, con:
            self.assertEqual(con.execute("select count(*) from reservation where provider='opencode-go:free'").fetchone()[0], 1)
            # the run dollar cap does NOT COUNT the zero-priced bucket
            settled, held = budget.run_usage(con, 'r1')
        self.assertEqual(settled, 0)
        self.assertEqual(held, 2_000_000)

    def test_the_free_bucket_is_counted_and_bounded_per_day(self):
        os.environ['CADRE_BEDAVA_GUN_TAVAN'] = '3'
        for i in range(3):
            self.ledger.settle(self.ledger.reserve('opencode-go:free', 1, snapshot(), {}), 1, f'f{i}')
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('opencode-go:free', 1, snapshot(), {})
        # even if the cap is raised later within the same day, the period cap stays fixed with min()
        os.environ['CADRE_BEDAVA_GUN_TAVAN'] = '9'
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('opencode-go:free', 1, snapshot(), {})

    def test_a_closed_go_window_still_blocks_its_free_models(self):
        kapali = snapshot()
        kapali['go']['pencere_acik'] = False
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('opencode-go:free', 1, kapali, {})
        self.ledger.reserve('opencode-go:free', 1, snapshot(), {})

    def test_an_unknown_base_provider_is_never_admitted_through_the_free_suffix(self):
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('uydurma:free', 1, snapshot(), {})
        self.assertEqual(budget.taban_saglayici('opencode-go:free'), 'opencode-go')
        self.assertEqual(budget.taban_saglayici('deepseek'), 'deepseek')


if __name__ == '__main__':
    unittest.main()


class SerbestKovaYasagiTests(unittest.TestCase):
    """A ':free' ban removes only ZERO-PRICED models; the same provider's paid model keeps working.
    Previously both had the same id: when the free model's daily counter filled the whole provider closed."""

    KATALOG = {'google': {'models': {'bedava-model': {'limit': {'context': 1000}, 'cost': {'input': 0, 'output': 0}},
                                     'parali-model': {'limit': {'context': 1000}, 'cost': {'input': 1, 'output': 2}}}}}

    def setUp(self):
        import json
        import sys
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
        import katalog
        import qe_core as core
        self.core = core
        self.temp = tempfile.TemporaryDirectory(prefix='qe-kova-')
        self.addCleanup(self.temp.cleanup)
        dosya = Path(self.temp.name) / 'models.json'
        dosya.write_text(json.dumps(self.KATALOG), encoding='utf-8')
        eski = os.environ.get('CADRE_MODEL_KATALOG')
        os.environ['CADRE_MODEL_KATALOG'] = str(dosya)
        katalog._onbellek.clear()
        self.addCleanup(katalog._onbellek.clear)
        self.addCleanup(lambda: os.environ.__setitem__('CADRE_MODEL_KATALOG', eski)
                        if eski else os.environ.pop('CADRE_MODEL_KATALOG', None))
        self.zincir = ['google/bedava-model', 'google/parali-model', 'lmstudio/yerel']
        yama = patch.object(core.YON, 'zincir', return_value=(self.zincir, {}))
        yama.start()
        self.addCleanup(yama.stop)
        yama2 = patch.object(core.YON, 'durum_oku', return_value={})
        yama2.start()
        self.addCleanup(yama2.stop)

    def test_free_ban_skips_only_zero_cost_models_of_that_provider(self):
        butce = {'gemini': {'db': 'ok'}}
        model, _ = self.core.kisit_model_sec(butce)
        self.assertEqual(model, 'google/bedava-model')
        model, _ = self.core.kisit_model_sec(butce, yasak={'google:free'})
        self.assertEqual(model, 'google/parali-model', 'parali model ayakta kalmali')
        model, _ = self.core.kisit_model_sec(butce, yasak={'google'})
        self.assertEqual(model, 'lmstudio/yerel', 'taban saglayici yasakliysa o saglayicinin hepsi duser')


class BedavaKatmanUstKatmandaTests(unittest.TestCase):
    """B110 (18.09): the upper layer (`opencode_budget_env`) only asked about the BASE provider id; the `:free`
    bucket was never asked. Result: when the zen/Go balance ran out that provider's ZERO-PRICED models were also
    removed and the job fell to local needlessly (item 0b: the bucket unit is the MODEL)."""

    KATALOG = {'opencode-go': {'models': {
        'bedava-model': {'limit': {'context': 262144}, 'cost': {'input': 0, 'output': 0}},
        'parali-model': {'limit': {'context': 262144}, 'cost': {'input': 1, 'output': 2}}}}}

    def setUp(self):
        import json
        import sys
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
        import katalog
        import qe_core as core
        self.core = core
        self.temp = tempfile.TemporaryDirectory(prefix='qe-bedava-ust-')
        self.addCleanup(self.temp.cleanup)
        dosya = Path(self.temp.name) / 'models.json'
        dosya.write_text(json.dumps(self.KATALOG), encoding='utf-8')
        eski = os.environ.get('CADRE_MODEL_KATALOG')
        os.environ['CADRE_MODEL_KATALOG'] = str(dosya)
        katalog._onbellek.clear()
        self.addCleanup(katalog._onbellek.clear)
        self.addCleanup(lambda: os.environ.__setitem__('CADRE_MODEL_KATALOG', eski)
                        if eski else os.environ.pop('CADRE_MODEL_KATALOG', None))
        for yama in (patch.object(core, 'QE', Path(self.temp.name)),
                     patch.object(core, 'project_memory', return_value=[]),
                     patch.object(core.YON, 'durum_oku', return_value={}),
                     patch.object(core.YON, 'zincir',
                                  return_value=(['opencode-go/parali-model', 'opencode-go/bedava-model',
                                                 'lmstudio/qwen3.8-27b'], {}))):
            yama.start()
            self.addCleanup(yama.stop)

    def test_zero_cost_models_are_still_offered_when_the_dollar_bucket_is_empty(self):
        from unittest.mock import patch
        with patch.object(self.core, 'butce_json', return_value=snapshot(0.0)):
            env = {'OPENCODE_CONFIG': '/nonexistent'}
            # P3 (03.10): local is closed; the main model also comes from the free bucket (valid even when the dollars run out).
            self.core.opencode_budget_env(env, ['-m', 'opencode-go/bedava-model', 'gorev metni'])
        import json
        cfg = json.loads(env['OPENCODE_CONFIG_CONTENT'])
        self.assertEqual(cfg['agent']['cadre-plan']['model'], 'opencode-go/bedava-model',
                         'para bitti ama bedava kova acik: sifir fiyatli model secilmeli')
        self.assertTrue(any(p.endswith('cadre-plugin.mjs') for p in cfg.get('plugin', [])),
                        'bedava kovadan secilen model de buluttur: makbuzu eklenti yazar')


class GunlukIstekTekKaynakTests(unittest.TestCase):
    """B110 (18.09): the daily request cap was THREE different numbers in the code — 450 applied, 500 reported, 147 measured.
    The applied number comes from a single source and CANNOT EXCEED the RPD the provider reports/measures."""

    def test_reported_limit_comes_from_the_enforced_one(self):
        """The reported daily cap and the APPLIED cap must be the same number. This used to be tested by scanning the butce.py source
        (is it a constant, does it come from GUNLUK_ISTEK); a source scan is not behavioural
        evidence. Now butce.py is REALLY run and the `oran`/`kalan_istek` values it reports are compared with the ones derived from the
        cap the gate applies."""
        import json
        import subprocess
        import sys as _sys
        from pathlib import Path
        import butce_kapisi as budget
        kok = Path(__file__).resolve().parents[1]
        r = subprocess.run([_sys.executable, str(kok / 'lib/butce.py')], capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        rapor = json.loads(r.stdout)
        for bolum, kapi_adi in (('groq', 'groq'),):   # 4c-11: of the free providers only groq remains, in the pre-filter
            with self.subTest(saglayici=bolum):
                veri = rapor.get(bolum) or {}
                self.assertIn('oran', veri, f'{bolum} raporda kalan oran yok: {sorted(veri)}')
                tavan = int(budget.GUNLUK_ISTEK[kapi_adi])
                kullanilan = int(veri.get('bugun_istek', 0))
                beklenen = max(0.0, (tavan - kullanilan) / tavan)
                self.assertAlmostEqual(float(veri['oran']), beklenen, places=2,
                                       msg=f'{bolum}: raporlanan oran uygulanan tavandan ({tavan}) turemiyor')
                if 'kalan_istek' in veri:
                    self.assertEqual(int(veri['kalan_istek']), max(0, tavan - kullanilan),
                                     f'{bolum}: kalan istek uygulanan tavanla tutmuyor')

    def test_enforced_daily_limit_never_exceeds_the_providers_own_rpd(self):
        import butce_kapisi as budget
        import yonlendirici
        for sag, uygulanan in budget.GUNLUK_ISTEK.items():
            rpd = yonlendirici.SAGLAYICI.get(sag, {}).get('rpd')
            if rpd is None:
                continue
            with self.subTest(saglayici=sag):
                self.assertLessEqual(uygulanan, int(rpd),
                                     f'{sag}: uygulanan gunluk tavan saglayicinin rpd sinirini asiyor')


class ModelBasinaGunlukKovaTests(unittest.TestCase):
    """18.09: the daily request cap may be per model (Gemini; removed in 4c-11, the table is empty). The provider-level bucket works."""

    def snapshot(self, sayaclar=None, toplam=0):
        import time
        return {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'bugun': time.strftime('%Y-%m-%d'),
                'gemini': {'db': 'ok', 'bugun_istek': toplam,
                           **({'bugun_istek_model': sayaclar} if sayaclar is not None else {})}}


    def test_the_provider_level_bucket_still_works_for_the_others(self):
        import time
        anlik = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'bugun': time.strftime('%Y-%m-%d'),
                 'groq': {'db': 'ok', 'bugun_istek': 100}}
        _d, kalan = budget.allowance(anlik, 'groq')
        self.assertEqual(kalan, budget.GUNLUK_ISTEK['groq'] - 100)
