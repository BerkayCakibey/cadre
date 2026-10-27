"""D1/D2 — the user's 16.09 footnote: "the one with a lot of budget can be pulled forward, the shrinking one throttled; tracking must be continuous".
06.10: daily share/borrowing/pace were removed; share = the remaining balance (prepaid) or the Go period remainder.
D2 dynamic order: among paid candidates in the same quality tier ONLY the tight one is moved back; the quality order and
the free/local/claude links are kept (decision 2). Fake data, no network."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import butce  # noqa: E402
import yonlendirici as yon  # noqa: E402


def sahte_butce(go_kul=2.7, go_taban=1.0, ds_kul=0.5, ds_taban=1.0, cb_kul=3.0, cb_taban=1.0):
    import time as _t
    return {'ts': _t.strftime('%Y-%m-%dT%H:%M:%S'), 'bugun': _t.strftime('%Y-%m-%d'), 'kalan_gun': 10,'go': {'db': 'ok', 'kullanilabilir': (go_kul) * 100 / 15, 'pay_taban': (go_taban) * 100 / 15, 'pay': (go_taban) * 100 / 15, 'pencere_acik': True, 'oran': 1.0, 'birim': 'puan'},
            'ds': {'api': 'ok', 'kullanilabilir': ds_kul, 'pay_taban': ds_taban, 'pay': ds_taban, 'oran': 1.0},
            'cerebras': {'db': 'ok', 'kullanilabilir': cb_kul, 'pay_taban': cb_taban, 'pay': cb_taban, 'kredi_kalan': 5.0, 'oran': 1.0},
            'gemini': {'db': 'ok', 'kalan_istek': 400, 'bugun_istek': 100, 'oran': 0.8},
            'groq': {'db': 'ok', 'bugun_istek': 0, 'oran': 1.0}, 'mistral': {'db': 'ok', 'bugun_istek': 0, 'oran': 1.0},
            'claude': {'kaynak': 'taze', 'pay': 5, 'yuzde1_usd': 30, 'havuz_kalan_usd': 200}}


class DinamikSiraTests(unittest.TestCase):
    ADAYLAR = ['opencode-go/kimi-k2.7-code', 'deepseek/deepseek-v4-flash', 'cerebras/qwen-3.8-27b', 'google/gemini-3-flash-preview']

    # Item 7 (17.09): the order now also looks at the CURRENT UNIT PRICE (DeepSeek peak 01-04/06-10 UTC = 2x).
    # This test is about ABUNDANCE, so the clock is pinned; peak behaviour is in test_havuz_sirasi.py.
    OFF_PEAK = __import__('calendar').timegm(__import__('time').strptime('2026-09-17 12:00', '%Y-%m-%d %H:%M'))

    def sirala(self, b, d=None, an=None):
        return yon.bolluga_gore_sirala(list(self.ADAYLAR), b, d if d is not None else {}, an or self.OFF_PEAK)

    def test_net_budget_orders_equal_quality_slots_by_role_cost(self):
        import tempfile
        from test_butce_donemi import snapshot
        from butce_kapisi import BudgetLedger, allowance
        with tempfile.TemporaryDirectory() as tmp, patch.object(yon, 'QE', Path(tmp)), patch.object(yon, 'kayit'), \
                patch.dict(os.environ, {'CADRE_RUN_ID': '', 'CADRE_GOREV_ID': ''}):
            b = snapshot(20)
            b['zen'] = {'db': 'ok', 'pay': .35, 'kullanilabilir': .35}
            # D13 (03.10): deepseek-v4-pro is now KALITE_KATI tier 1 of the impl links — bolluga_gore_birala
            # measures it too, so a readable 'ds' observation is needed (otherwise the whole tier falls back to index order).
            b['ds'] = {'api': 'ok', 'pay': 10, 'kullanilabilir': 10}
            zen, go = 'opencode/deepseek-v4-flash', 'opencode-go/glm-5.3-flash'
            adaylar = ['claude/opus#max', zen, 'deepseek/deepseek-v4-pro', go]
            for rol in yon.ROLLER:
                sira = yon.bolluga_gore_sirala(adaylar, b, rol=rol)
                self.assertEqual(sira, [adaylar[0], go, adaylar[2], zen], rol)
            with patch.object(yon, 'butce_var', return_value=(True, 'fixture')), \
                 patch.object(yon, 'merdiven', return_value='aktif'), \
                 patch('katalog.kullanilabilir', return_value=True), patch.dict(yon.ROLLER, {'impl': [zen, go]}):
                self.assertEqual(yon.zincir('impl', b=b, d={})[0][0], zen,
                                 'D16: farkli model kimligi sirasi saglayici cuzdaninin onundedir')
            b['is_maliyeti'] = {'seritler': {'opencode-go': 100, 'opencode': .01}}
            self.assertEqual(yon.bolluga_gore_sirala([go, zen], b), [zen, go], 'maliyet pay kadar belirleyici')
            b.pop('is_maliyeti')
            ledger = BudgetLedger(Path(tmp) / 'zincir/request-budget.db')
            ledger.reserve('opencode-go', allowance(b, 'opencode-go', model='glm-5.3-flash')[1] - 100_000, b, {'model': 'glm-5.3-flash'})
            self.assertEqual(yon.bolluga_gore_sirala([go, zen], b), [zen, go], 'askida tutar kullanilabilir paydan duser')
            with patch.object(yon, 'net_kapasite_usd', return_value=1):
                self.assertEqual(yon.bolluga_gore_sirala([zen, go], b), [zen, go], 'esitlikte elle verilen sira')

    def test_free_local_and_claude_links_are_never_reordered(self):
        b = sahte_butce(go_kul=0.1, ds_kul=0.1, cb_kul=0.1)   # all are tight
        zincir = yon.bolluga_gore_sirala(['claude/opus#max', 'opencode-go/glm-5.2', 'opencode-go/qwen3.8-max'], b)
        self.assertEqual(zincir[0], 'claude/opus#max', 'claude siralanmaz')   # 4c-11: no free/local link, only the Claude rule remains

    def test_chain_excludes_local_and_is_empty_without_budget_data_and_ordered_with_it(self):
        # D13/P3 (03.10): NO LOCAL LINK in the impl chain; all three candidates are paid ds-v4-pro routes. Without budget
        # data all are removed with 'no budget/no verified share' -> the chain is EMPTY (budget wait), it does not fall to a local last link
        # as in the old behaviour. With a fully budgeted observation the paid routes stay.
        for b in ({}, None):
            import tempfile
            with patch.object(yon, 'QE', Path(tempfile.mkdtemp(prefix='dinamik-zincir-'))), \
                 patch.object(yon, 'butce_oku', return_value={}), patch.object(yon, 'kayit'):
                z, _ = yon.zincir('impl', 'orta', 20000, b, {})
            self.assertEqual(z, [], z)
        self.assertEqual(yon.bolluga_gore_sirala(list(self.ADAYLAR), {}), self.ADAYLAR)
        with tempfile.TemporaryDirectory(prefix='dinamik-zincir-') as tmp, \
             patch.object(yon, 'QE', Path(tmp)), patch.object(yon, 'kayit'):
            z, _ = yon.zincir('impl', 'orta', 20000, sahte_butce(), {})
        self.assertGreater(len(z), 1, 'tam butceli anlik goruntude zincir bos olamaz')
        self.assertTrue(all(not m.startswith('lmstudio/') for m in z), z)


if __name__ == '__main__':
    unittest.main()

class OnOdemeliTekDusmeTest(unittest.TestCase):
    """D02 (01.10 Codex): in job-cap mode the observation gave the REMAINING allowance and the ledger subtracted the same spending again."""

    def gozlem(self, home, db, sim):
        import subprocess
        (home / 'sim.json').write_text(json.dumps(sim))
        env = {**os.environ, 'CADRE_HOME': str(home), 'OPENCODE_DB': str(db), 'CADRE_ENGINE_ROOT': str(LIB.parent),
               'CADRE_SIM_YUK': str(home / 'sim.json'), 'CADRE_ONODEMELI_KIP': 'is-tavani', 'HOME': str(home),
               'CADRE_GO_PENCERE_TTL': '1e9'}   # a stale cache is read without a network call; what follows is added with Σ
        r = subprocess.run([sys.executable, str(LIB / 'butce.py')], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_ayni_harcama_gozlemde_ve_defterde_bir_kez_duser(self):
        from contextlib import closing
        import datetime, sqlite3, tempfile, time
        import butce_kapisi as bk
        from test_zen import sahte_db
        with tempfile.TemporaryDirectory(prefix='tek-dusme-') as d:
            home = Path(d)
            (home / 'zincir').mkdir()
            (home / 'zincir/go-pencere.json').write_text(json.dumps({'ts': time.time() - 3 * 3600, 'rolling': 5.0, 'weekly': 5.0,
                                                                     'monthly': 5.0, 'resets': {}, 'gecmis': []}))
            giris = (datetime.datetime.now() - datetime.timedelta(days=1)).isoformat(timespec='seconds')
            (home / 'zincir/zen-bakiye.txt').write_text(f'{giris} 10\n')
            bos, dolu = home / 'bos.db', home / 'dolu.db'
            sahte_db(bos, [])
            simdi = time.time()
            sahte_db(dolu, [(sag, simdi - 30, 0.5) for sag in ('opencode', 'opencode-go')])
            once = self.gozlem(home, bos, {})
            sonra = self.gozlem(home, dolu, {})
            defter = bk.BudgetLedger(home / 'zincir/request-budget.db')
            for sag, makbuz in (('opencode', 500_000), ('opencode-go', 500_000)):   # Go: list price, counted in points
                with self.subTest(sag=sag):
                    a0 = bk.BudgetLedger(home / 'yok.db').available(sag, once)
                    rid = defter.reserve(sag, makbuz, once, {})
                    defter.settle(rid, makbuz, 'makbuz-' + sag)
                    with closing(sqlite3.connect(home / 'zincir/request-budget.db')) as con, con:
                        con.execute("update reservation set created=?, metadata=json_set(metadata,'$.kesin_ts',?) where id=?",
                                    (simdi - 30, simdi - 30, rid))
                    a1 = defter.available(sag, sonra)
                    self.assertGreater(a0, makbuz)
                    self.assertAlmostEqual(a0 - a1, makbuz, delta=2000, msg=(a0, a1, once[bk.PAID[sag]], sonra[bk.PAID[sag]]))


class GoTekHavuzTest(unittest.TestCase):
    """4c-13: Go is one pool and its unit is points (1 point = 1% of the monthly pool); window percentage = Σ spending ÷ (share × model limit).
    The gate is the API percentage; steps after the reading are added with Σ; there is no coefficient and no $60 conversion."""

    def ev(self, d, pencere, kayitlar, resets=None):
        from test_zen import sahte_db
        home = Path(d)
        (home / 'zincir').mkdir()
        if pencere is not None:
            (home / 'zincir/go-pencere.json').write_text(json.dumps({**pencere, 'resets': resets or {}, 'gecmis': []}))
        sahte_db(home / 'opencode.db', kayitlar)
        return home

    def gozlem(self, home):
        import io
        import go_pencere
        out = io.StringIO()
        env = {k: v for k, v in os.environ.items() if not k.startswith('CADRE_TEST_') and k != 'CADRE_SIM_YUK'}
        with patch.dict(os.environ, env, clear=True), patch.object(butce, 'QE', home), \
             patch.object(butce, 'DB', home / 'opencode.db'), \
             patch.object(Path, 'home', return_value=home), patch('sys.stdout', out), \
             patch.object(butce, 'oturum_say', return_value=1), \
             patch.object(go_pencere, 'oku', side_effect=lambda h, *a, **k: go_pencere._onbellek(h) or None):
            butce.main()
        return json.loads(out.getvalue())

    def test_kalan_puan_en_dar_pencereden_ve_okumadan_sonraki_adimlar_dusulur(self):
        import tempfile, time
        simdi = time.time()
        with tempfile.TemporaryDirectory(prefix='go-havuz-') as d:
            api = {'ts': simdi - 60, 'rolling': 40.0, 'weekly': 20.0, 'monthly': 50.0}
            # before the reading $0.30 (not counted: it is in the API) + after glm-5.3 $0.15 = 1 point (DeepSeek depends on the hour, not here)
            home = self.ev(d, api, [('opencode-go', simdi - 120, 0.30, 'glm-5.3'),
                                    ('opencode-go', simdi - 30, 0.15, 'glm-5.3')])
            go = self.gozlem(home)['go']
        self.assertEqual((go['kaynak'], go['birim'], go['db']), ('api', 'puan', 'ok'), go)
        self.assertAlmostEqual(go['puan_sonra'], 1.0, places=6)
        self.assertAlmostEqual(go['ay_puan'], 51.0, places=6)
        # rolling (95-40)x0.2 = 11 < weekly 37.5 < monthly 45 -> 11 - 1 = 10 points
        self.assertAlmostEqual(go['kullanilabilir'], 10.0, places=6)
        self.assertNotIn('katsayi', json.dumps(go))

    def test_api_yoksa_sigma_formulu_hafta_son_yedi_gun(self):
        import tempfile, time
        simdi = time.time()
        with tempfile.TemporaryDirectory(prefix='go-sigma-') as d:
            home = self.ev(d, None, [('opencode-go', simdi - 3 * 86400, 0.6, 'glm-5.3-flash'),   # $60 -> 1 puan
                                     ('opencode-go', simdi - 3600, 0.3, 'qwen3.8-flash')])       # $30 -> 1 puan
            go = self.gozlem(home)['go']
        self.assertEqual(go['kaynak'], 'db', go)
        self.assertAlmostEqual(go['kendi']['rolling'], 5.0, places=4)    # 1 puan / 0,2
        self.assertAlmostEqual(go['kendi']['weekly'], 4.0, places=4)     # 2 puan / 0,5
        self.assertAlmostEqual(go['ay_puan'], 2.0, places=4)

    def test_okunamayan_db_ve_api_yok_kapalidir(self):
        import tempfile
        import butce_kapisi as bk
        with tempfile.TemporaryDirectory(prefix='go-yok-') as d:
            home = Path(d); (home / 'zincir').mkdir()
            go = self.gozlem(home)
        self.assertEqual(go['go']['db'], 'ulasilamadi')
        with self.assertRaises(bk.BudgetDenied):
            bk.allowance(go, 'opencode-go')

    def test_kapi_puani_modelin_liste_dolarina_cevirir_askidakiler_puanla_sayilir(self):
        import tempfile, time
        import butce_kapisi as bk
        b = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'bugun': time.strftime('%Y-%m-%d'),
             'go': {'db': 'ok', 'birim': 'puan', 'kullanilabilir': 10.0, 'pay': 60.0, 'pencere_acik': True}}
        self.assertEqual(bk.allowance(b, 'opencode-go', model='deepseek-v4-pro')[1], 1_500_000)   # 10 x $0,15
        self.assertEqual(bk.allowance(b, 'opencode-go', model='glm-5.3-flash')[1], 6_000_000)     # 10 x $0,60
        self.assertEqual(bk.allowance(b, 'opencode-go')[1], 1_500_000, 'model yoksa en agir limit')
        with tempfile.TemporaryDirectory(prefix='go-kapi-') as d:
            defter = bk.BudgetLedger(Path(d) / 'request-budget.db')
            defter.reserve('opencode-go', 3_000_000, b, {'model': 'deepseek-v4-flash'})   # $3 / $30 limit = 10 points: the pool is full
            self.assertEqual(defter.available('opencode-go', b, model='deepseek-v4-pro'), 0)
            with self.assertRaises(bk.BudgetDenied):
                defter.reserve('opencode-go', 1, b, {'model': 'glm-5.3-flash'})
        with tempfile.TemporaryDirectory(prefix='go-kapi2-') as d:
            defter = bk.BudgetLedger(Path(d) / 'request-budget.db')
            defter.reserve('opencode-go', 300_000, b, {'model': 'deepseek-v4-flash'})     # 1 puan
            self.assertEqual(defter.available('opencode-go', b, model='deepseek-v4-pro'), 1_350_000)   # 9 x $0,15

    def test_yogun_saatte_go_deepseek_iki_kat_puan_yazar(self):
        import calendar, tempfile, time
        import go_pencere
        yogun = calendar.timegm(time.strptime('2026-10-07 07:30', '%Y-%m-%d %H:%M'))    # Car 07:30 UTC
        sakin = calendar.timegm(time.strptime('2026-10-07 12:30', '%Y-%m-%d %H:%M'))
        with tempfile.TemporaryDirectory(prefix='go-yogun-') as d:
            home = self.ev(d, None, [('opencode-go', yogun, 0.15, 'deepseek-v4-pro'), ('opencode-go', sakin, 0.15, 'deepseek-v4-pro'),
                                     ('opencode-go', yogun, 0.15, 'glm-5.3')])
            self.assertAlmostEqual(go_pencere.sigma(home / 'opencode.db', yogun - 1, sakin + 1), 4.0, places=6)

    def test_aylik_nakit_havuzu_go_yu_saymaz(self):
        import tempfile
        import aylik_butce as ab
        with tempfile.TemporaryDirectory(prefix='go-nakit-') as d:
            ledger = ab.MonthlyLedger(Path(d) / 'aylik-butce.db', cap=65_000_000)
            with ledger.transaction() as con:   # 4c-13 migration: this month's old Go line does not count even if it stays in the pool
                con.execute('insert into reservation values(?,?,?,?,?,?,?,?)',
                            ('eski', 'opencode-go', ab.period(), 0.0, 30_000_000, 30_000_000, 'r-eski', '{}'))
            self.assertEqual(ledger.status()['remaining_micro'], 65_000_000)
            with self.assertRaises(ab.MonthlyBudgetDenied):
                ledger.reserve('opencode-go', 1)
        self.assertEqual(ab.DEFAULT_CAP_USD, 65)

    def test_izleyici_satiri_puan_gosterir(self):
        import tempfile, time
        import kota_gidisat
        simdi = time.time()
        with tempfile.TemporaryDirectory(prefix='go-izle-') as d:
            home = self.ev(d, None, [('opencode-go', simdi - 60, 0.15, 'qwen3.8-max')])
            satir = kota_gidisat.satir(home / 'opencode.db', now=simdi)
        self.assertIn('1.00 aylik puan', satir)
        self.assertIn('rolling %5.0', satir)
