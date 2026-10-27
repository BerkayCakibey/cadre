"""Budget limit scenarios — with fake (dummy) data, against the real gate, results written to a table.

User request (17.09): "test the budgets with dummy data if needed, lots of different tests with their results
noted". Each scenario: an invented budget observation + the real `butce_kapisi` gate. Real money $0.
The result table is written with `--rapor` to `docs/BUTCE-SENARYOLARI-20260917.md`."""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
import sys
import unittest

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import butce_kapisi as budget  # noqa: E402
import sozlesme  # noqa: E402
import test_kosu_sessions as fixtures  # noqa: E402

SONUCLAR: list[tuple[str, str, str, bool]] = []


def anlik(**ek):
    now = datetime.datetime.now()
    taban = {
        'ts': now.isoformat(), 'bugun': now.date().isoformat(),
        'go': {'db': 'ok', 'pay': 13.333334, 'kullanilabilir': 13.333334, 'pencere_acik': True, 'birim': 'puan'},
        'zen': {'db': 'ok', 'pay': 0.35, 'kullanilabilir': 0.35},
        'groq': {'db': 'ok', 'bugun_istek': 0},
    }
    for k, v in ek.items():
        if isinstance(v, dict) and isinstance(taban.get(k), dict):
            taban[k] = {**taban[k], **v}
        else:
            taban[k] = v
    return taban


class ButceSenaryolari(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.home = self.f.home
        self.ledger = budget.BudgetLedger(self.home / 'zincir/request-budget.db')

    def olc(self, ad, beklenen, fn, *, ret_bekleniyor=False):
        """The type and text of the result are verified; a denial passes only with a real BudgetDenied."""
        reddedildi = False
        try:
            gozlenen = fn()
        except budget.BudgetDenied as e:
            gozlenen = f'RED: {e}'
            reddedildi = True
        # An unexpected denial cannot be counted as an amount/status; the substring is searched only in the expected real denial reason.
        uyuyor = reddedildi == ret_bekleniyor and (
            beklenen.lower() in gozlenen.lower() if ret_bekleniyor else beklenen == gozlenen)
        SONUCLAR.append((ad, beklenen, gozlenen[:110], uyuyor))
        self.assertTrue(uyuyor, f'{ad}: beklenen "{beklenen}", gozlenen "{gozlenen}"')

    def pay(self, snapshot, saglayici):
        return str(self.ledger.available(saglayici, snapshot))

    # --- 1-4: daily share and margin ------------------------------------------------------------
    def test_s01_tum_havuzlar_dolu(self):
        # GO1 (03.10): Go is not split into days, no daily margin — full share 2.0 * 1e6 = 2_000_000.
        self.olc('S01 tüm havuzlar dolu', '2000000', lambda: self.pay(anlik(), 'opencode-go'))

    def test_s02_go_gunluk_payi_bitti(self):
        self.olc('S02 Go günlük payı bitti', '0',
                 lambda: self.pay(anlik(go={'kullanilabilir': 0.0}), 'opencode-go'))

    def test_s03_kucuk_go_payi_puandan_dolara_cevrilir(self):
        # no daily margin on Go: 0.25 points is $0.0375 capacity on the heaviest model (N-BU04).
        self.olc('S03 küçük Go payı kullanılabilir', '37500',
                 lambda: self.pay(anlik(go={'kullanilabilir': 0.25}), 'opencode-go'))

    # --- 5-7: window, observation, failure --------------------------------------------------------
    def test_s05_go_penceresi_kapali(self):
        self.olc('S05 Go penceresi kapalı', 'provider window closed',
                 lambda: str(budget.allowance(anlik(go={'pencere_acik': False}), 'opencode-go')), ret_bekleniyor=True)

    def test_s06_gozlem_bayat(self):
        eski = anlik()
        eski['ts'] = (datetime.datetime.now() - datetime.timedelta(seconds=200)).isoformat()
        self.olc('S06 gözlem 200 sn bayat', 'stale budget',
                 lambda: str(budget.allowance(eski, 'opencode-go')), ret_bekleniyor=True)

    def test_s07_fiyat_arizasi(self):
        import sqlite3
        with self.ledger.transaction() as con:
            con.execute("insert or ignore into pricing_fault values('opencode')")
        self.olc('S07 fiyat arızası (zen)', 'observed usage exceeded',
                 lambda: str(self.ledger.reserve('opencode', 1000, anlik(), {})), ret_bekleniyor=True)

    # --- 8-10: zen and free ----------------------------------------------------------------
    def test_s08_zen_bakiyesi_sifir(self):
        self.olc('S08 zen bakiyesi sıfır', '0',
                 lambda: self.pay(anlik(zen={'pay': 0.0, 'kullanilabilir': 0.0}), 'opencode'))

    def test_s09_zen_kucuk_bakiye(self):
        # pay 0.35 -> marj 0.0525; kalan 0.35 - 0.0525 = 0.2975
        self.olc('S09 zen küçük bakiye (taban)', '297500', lambda: self.pay(anlik(), 'opencode'))

    def test_s10_bedava_gunluk_istek_bitti(self):
        self.olc('S10 Groq günlük istek bitti', '0',
                 lambda: self.pay(anlik(groq={'bugun_istek': 900}), 'groq'))

    # --- 11-14: run cap, roles, buckets ------------------------------------------------
    def kosu(self, cap):
        from kosu import RunStore
        store = RunStore(self.home)
        rid = store.accept('senaryo', 'is')['run_id']
        store.update(rid, budget_cap_usd=cap)
        sozlesme.olustur(self.home, rid, None)
        return rid

    def test_s11_kosu_tavani_kucuk(self):
        rid = self.kosu(0.05)
        tav = budget.rol_tavani(self.home, rid, 'impl')
        # 17.09: the shares became FLOORS. With a $0.05 cap the judge floor's $0.20 minimum is limited to half (25000)
        # and the helper floor of 10% (5000) is set aside too → the lane is left 50000-25000-5000 = 20000.
        self.olc('S11 koşu tavanı $0.05, şerit dilimi', '20000', lambda: str(tav))

    def test_s12_tavan_dolu_serit_reddedilir(self):
        rid = self.kosu(1.0)
        tav = budget.rol_tavani(self.home, rid, 'impl')
        self.ledger.reserve('opencode-go', tav, anlik(go={'pay': 10.0, 'kullanilabilir': 10.0}),
                            {'run_id': rid}, run_cap=tav)
        self.olc('S12 şerit dilimi dolu → red', 'run budget exhausted',
                 lambda: str(self.ledger.reserve('opencode-go', 1000,
                                                 anlik(go={'pay': 10.0, 'kullanilabilir': 10.0}),
                                                 {'run_id': rid}, run_cap=tav)), ret_bekleniyor=True)

    def test_s13_serit_dolu_hakem_gecer(self):
        rid = self.kosu(1.0)
        bol = anlik(go={'pay': 10.0, 'kullanilabilir': 10.0})
        serit, serit_dilim = budget.rol_butcesi(self.home, rid, 'impl')
        self.ledger.reserve('opencode-go', serit, bol, {'run_id': rid}, run_cap=serit, dilim=serit_dilim)
        hakem, hakem_dilim = budget.rol_butcesi(self.home, rid, 'kalite')
        self.olc('S13 şerit dolu, hakem kendi cebinden geçer', 'kabul',
                 lambda: 'kabul' if self.ledger.reserve('opencode-go', 150_000, bol, {'run_id': rid},
                                                        run_cap=hakem, dilim=hakem_dilim) else 'red')

    def test_s14_bedava_model_parali_havuz_bitse_de_calisir(self):
        bos = anlik(go={'kullanilabilir': 0.0})
        self.olc('S14 Go bitti ama sıfır fiyatlı model çalışır', 'kabul',
                 lambda: 'kabul' if self.ledger.reserve('opencode-go:free', 1, bos, {}) else 'red')

    def test_s15_bedava_kova_gunluk_tavan(self):
        os.environ['CADRE_BEDAVA_GUN_TAVAN'] = '2'
        self.addCleanup(lambda: os.environ.pop('CADRE_BEDAVA_GUN_TAVAN', None))
        for i in range(2):
            self.ledger.settle(self.ledger.reserve('opencode-go:free', 1, anlik(), {}), 1, f'f{i}')
        self.olc('S15 bedava kova günlük tavanı', 'allocation exhausted',
                 lambda: str(self.ledger.reserve('opencode-go:free', 1, anlik(), {})), ret_bekleniyor=True)

    def test_s16_bilinmeyen_saglayici(self):
        self.olc('S16 tanımsız sağlayıcı', 'unmetered provider',
                 lambda: str(budget.allowance(anlik(), 'uydurma-saglayici')), ret_bekleniyor=True)

    def test_numeric_measurements_reject_wrong_amounts(self):
        # N-QA01: a wrong amount cannot pass even if it contains the right amount as a substring.
        bas = len(SONUCLAR)
        try:
            for beklenen, gozlenen in (('0', '37500'), ('20000', '120000'), ('2000000', '20000000')):
                with self.subTest(beklenen=beklenen, gozlenen=gozlenen), self.assertRaises(AssertionError):
                    self.olc('yanlis tutar', beklenen, lambda: gozlenen)
            self.olc('tam sifir', '0', lambda: '0')
            self.olc('tam tutar', '37500', lambda: '37500')
            def reddet():
                raise budget.BudgetDenied('provider window closed: available 0')
            with self.assertRaises(AssertionError):
                self.olc('ret sifir tutar degildir', '0', reddet)
            with self.assertRaises(AssertionError):
                self.olc('ret sozu gercek ret degildir', 'provider window closed',
                         lambda: 'provider window closed', ret_bekleniyor=True)
            self.olc('gercek ret', 'provider window closed', reddet, ret_bekleniyor=True)
        finally:
            del SONUCLAR[bas:]  # Expected negative checks do not pollute the scenario report.


def rapor():
    hedef = Path(__file__).resolve().parents[2] / 'docs/BUTCE-SENARYOLARI-20260917.md'
    satirlar = ['# Bütçe sınır senaryoları — sahte veriyle ölçüm', '',
                'Her senaryo uydurma bir bütçe gözlemi üretir ve **gerçek** `butce_kapisi` kapısına sorar.',
                'Gerçek para harcanmaz. Yeniden üretmek için: `python3 engine/tests/test_butce_senaryolari.py --rapor`', '',
                '| # | senaryo | beklenen | gözlenen | sonuç |', '|---|---|---|---|---|']
    for ad, bek, goz, ok in SONUCLAR:
        satirlar.append(f"| {ad.split()[0]} | {' '.join(ad.split()[1:])} | `{bek}` | `{goz}` | {'✅' if ok else '❌'} |")
    satirlar += ['', f'**{sum(1 for *_, ok in SONUCLAR if ok)}/{len(SONUCLAR)} senaryo beklendiği gibi davrandı.**']
    hedef.write_text('\n'.join(satirlar) + '\n', encoding='utf-8')
    print(f'rapor: {hedef}')


if __name__ == '__main__':
    raporla = '--rapor' in sys.argv
    if raporla:
        sys.argv.remove('--rapor')
    sonuc = unittest.main(exit=False, verbosity=0)
    if raporla:
        rapor()
    sys.exit(0 if sonuc.result.wasSuccessful() else 1)
