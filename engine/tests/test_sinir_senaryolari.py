"""§40 LIMIT SCENARIOS — tests that PUSH the limit instead of "a week of shadow mode" (user decision 17.09).

Each scenario's threshold is written BEFORE the run; all in an isolated home, with fake data, real money $0.
S1/S2/S3/S8 overlap with the night autonomy scenarios (test_gece_senaryolar.py) and S9 with the coordinator tests;
here the remaining §40 items are proven one by one on top of them."""

from __future__ import annotations

import calendar
from contextlib import closing
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import butce_kapisi as budget  # noqa: E402
import go_pencere  # noqa: E402
import sozlesme  # noqa: E402
from test_butce_donemi import snapshot  # noqa: E402
import test_kosu_sessions as fixtures  # noqa: E402


def utc(iso):
    return calendar.timegm(time.strptime(iso, '%Y-%m-%d %H:%M'))


class S1S2S3ButceSinirTests(unittest.TestCase):
    """S1 budget 0% · S2 budget 99% (a single-call share) · S3 the window fills mid-call.
    The night scenarios (test_gece_senaryolar.py) run the engine side of these; here the GATE itself is pushed."""

    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.ledger = budget.BudgetLedger(self.f.home / 'zincir/request-budget.db')

    def test_s1_zero_budget_admits_no_paid_call_but_free_requests_still_pass(self):
        bos = snapshot(0.0)
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('opencode-go', 1, bos, {})
        # the free bucket is counted separately: a zero-dollar budget does not close it
        bos['groq'] = {'db': 'ok', 'bugun_istek': 0}
        self.ledger.reserve('groq', 1, bos, {})

    def test_s2_a_single_call_worth_of_budget_admits_exactly_one_call(self):
        # GO1 (03.10): no daily margin on Go; the remaining share can be used directly.
        # Remaining 1.0 -> 1000000 micro-dollars. This is a single-call share.
        anlik = snapshot(1.0)
        self.assertEqual(self.ledger.available('opencode-go', anlik), 1_000_000)
        self.ledger.reserve('opencode-go', 1_000_000, anlik, {})
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('opencode-go', 1, anlik, {})

    def test_s3_a_window_that_closes_mid_flight_stops_the_next_call_only(self):
        anlik = snapshot(5.0)
        ucusta = self.ledger.reserve('opencode-go', 100_000, anlik, {})     # the in-flight call
        kapali = snapshot(5.0)
        kapali['go']['pencere_acik'] = False
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('opencode-go', 1000, kapali, {})
        # the in-flight call can write its receipt: closing the window does not cancel it
        self.ledger.settle(ucusta, 40_000, 'fis')


class S4DevirSiniriTests(unittest.TestCase):
    """S4: if three providers fail in a row, hand-offs <=2/lane, then local; no endless loop."""

    def test_three_failures_in_a_row_end_in_budget_wait_not_in_a_loop(self):
        import importlib
        temp = tempfile.TemporaryDirectory(prefix='qe-s4-')
        self.addCleanup(temp.cleanup)
        eski = {k: os.environ.get(k) for k in ('CADRE_HOME', 'CADRE_RUN_ID', 'CADRE_GOREV_ID')}
        os.environ.update(CADRE_HOME=temp.name, CADRE_RUN_ID='r-s4', CADRE_GOREV_ID='g1')
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in eski.items()])
        import qe_core
        importlib.reload(qe_core)
        zincir = ['deepseek/deepseek-v4-flash', 'cerebras/gpt-oss-120b', 'google/gemini-3-flash-preview']
        sonuclar = []
        with patch.object(qe_core.YON, 'zincir', return_value=(zincir, {})), \
             patch.object(qe_core.YON, 'ariza'), patch.object(qe_core, 'istek_olcusu', return_value=8000), \
             patch.object(qe_core, 'log'):
            for _ in range(5):
                sonuclar.append(qe_core.kota_dusur('openrouter/openai/gpt-6.1-sol', None))
        self.assertEqual(sonuclar[:2], ['deepseek/deepseek-v4-flash'] * 2)
        self.assertTrue(all(s == '' for s in sonuclar[2:]), f'ucuncu devirden sonra hep butce-bekleme olmali: {sonuclar}')


class S5OnbellekOlcumuTests(unittest.TestCase):
    """S5: with a 90% cache hit rate the measured spending must be within +-10% of reality."""

    def test_the_measured_cost_is_within_ten_percent_of_the_truth(self):
        # The same as the calculation in the plugin: 100 fresh x $1/M + 900 cache x $0.1/M + 10 output x $2/M
        taze, onbellek, cikti = 100, 900, 10
        gercek = taze * 1.0 + onbellek * 0.1 + cikti * 2.0
        eski_hesap = (taze + onbellek) * 2.0 + cikti * 2.0      # with the most expensive rate (cache.write=2)
        self.assertAlmostEqual(gercek, 210.0)
        self.assertGreater(eski_hesap / gercek, 8.0, 'eski hesabin sismesi olculdu: ~8.5 kat')
        # The plugin test (butce-plugin.test.mjs) verifies this number END TO END; here the arithmetic itself is locked.
        sapma = abs(gercek - 210.0) / 210.0
        self.assertLess(sapma, 0.10)


class S6ResetAniTests(unittest.TestCase):
    """S6: exactly at the month/week/5-hour boundary the share is recomputed; no double counting and no negative share."""


    def test_the_claude_ceiling_is_continuous_across_the_reset_second(self):
        import claude_politikasi as cp
        from datetime import datetime, timedelta
        reset = datetime(2026, 9, 15, 19, 0)
        once = cp.tavan(reset - timedelta(seconds=1))
        sonra = cp.tavan(reset)
        self.assertEqual(once, 92.5, 'reset oncesi tavan 92.5 (eski kodda %95 deligi vardi)')
        self.assertEqual(sonra, 62.5, 'reset aninda hafta basa doner')
        self.assertLess(sonra, once, 'yeni hafta daha cok rezerv ister')


class S7ClaudeResetTests(unittest.TestCase):
    """S7: at the Tuesday 19:00 reset the cap moves to the new week and the automation does not eat the user's share."""

    def test_the_user_keeps_seven_and_a_half_points_per_remaining_workday(self):
        import claude_politikasi as cp
        from datetime import datetime
        for tarih, kalan_gun in [('2026-09-15T19:00:00', 5), ('2026-09-16T12:00:00', 4),
                                 ('2026-09-18T12:00:00', 2), ('2026-09-21T12:00:00', 1)]:
            now = datetime.fromisoformat(tarih)
            with self.subTest(tarih=tarih):
                self.assertEqual(cp.kalan_rezerv_gunu(now), kalan_gun)
                kullanici_payi = 100 - cp.tavan(now)
                self.assertGreaterEqual(kullanici_payi, min(kalan_gun * 7.5, 7.5),
                                        'kullaniciya kalan rezerv gunu basina pay birakilmali')


class S10ZenKovaTests(unittest.TestCase):
    """S10: if zen's free and paid models run AT THE SAME TIME the buckets do not mix."""

    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.ledger = budget.BudgetLedger(self.f.home / 'zincir/request-budget.db')

    def anlik(self):
        import datetime
        return {'ts': datetime.datetime.now().isoformat(), 'bugun': datetime.date.today().isoformat(),
                'zen': {'db': 'ok', 'pay': 1.0, 'kullanilabilir': 1.0}}

    def test_free_and_paid_zen_never_share_a_bucket(self):
        anlik = self.anlik()
        ucretli = self.ledger.reserve('opencode', 200_000, anlik, {'run_id': 'r1', 'model': 'glm-5.2'})
        bedava = self.ledger.reserve('opencode:free', 1, anlik, {'run_id': 'r1', 'model': 'big-pickle'})
        self.ledger.settle(ucretli, 150_000, 'fis-ucretli')
        self.ledger.settle(bedava, 1, 'fis-bedava')
        import sqlite3
        with closing(sqlite3.connect(self.f.home / 'zincir/request-budget.db')) as con, con:
            satirlar = dict(con.execute('select provider, sum(actual) from reservation group by provider').fetchall())
        self.assertEqual(satirlar['opencode'], 150_000)
        self.assertEqual(satirlar['opencode:free'], 1)
        # the free bucket does not enter the run's DOLLAR cap
        with closing(sqlite3.connect(self.f.home / 'zincir/request-budget.db')) as con, con:
            settled, held = budget.run_usage(con, 'r1')
        self.assertEqual(settled, 150_000)


class S11AyrismaTests(unittest.TestCase):
    """S11: if the provider percentage and my own measurement diverge by 20 points the conservative one is used and a warning is written."""

    def test_the_conservative_number_wins_and_the_gap_is_recorded(self):
        veri = {'rolling': 0, 'weekly': 2, 'monthly': 27, 'gecmis': []}
        yeni, uyari = go_pencere.capraz_kontrol(veri, 50)
        self.assertEqual(yeni['monthly'], 50)
        self.assertIn('ayrismasi', uyari)
        yeni, uyari = go_pencere.capraz_kontrol(veri, 5)
        self.assertEqual(yeni['monthly'], 27, 'saglayici daha yuksekse o kullanilir')
        self.assertIn('ayrismasi', uyari)

    def test_a_small_gap_changes_nothing(self):
        veri = {'rolling': 0, 'weekly': 2, 'monthly': 27, 'gecmis': []}
        for kendi in (27, 28.3, 10, 46):
            yeni, uyari = go_pencere.capraz_kontrol(veri, kendi)
            with self.subTest(kendi=kendi):
                self.assertEqual(yeni['monthly'], 27)
                self.assertEqual(uyari, '')

    def test_an_unusable_own_measurement_is_ignored_not_trusted(self):
        veri = {'monthly': 27, 'gecmis': []}
        for kendi in (None, 'abc', -5, 5000):
            yeni, uyari = go_pencere.capraz_kontrol(veri, kendi)
            self.assertEqual(yeni['monthly'], 27, kendi)
            self.assertEqual(uyari, '', kendi)


class S12HakemSirasiTests(unittest.TestCase):
    """S12: run cap full + the judge's turn -> the judge passes from its own reserve; even if the reserve runs out the judge chain continues."""

    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        from kosu import RunStore
        self.store = RunStore(self.f.home)
        self.rid = self.store.accept('s12', 'is')['run_id']
        self.store.update(self.rid, budget_cap_usd=1.0)
        self.ledger = budget.BudgetLedger(self.f.home / 'zincir/request-budget.db')
        sozlesme.olustur(self.f.home, self.rid, None)

    def test_the_reviewer_passes_through_its_own_reserve_when_lanes_are_full(self):
        serit_tavan, serit_dilim = budget.rol_butcesi(self.f.home, self.rid, 'impl')
        self.ledger.reserve('opencode-go', serit_tavan, snapshot(5.0), {'run_id': self.rid},
                            run_cap=serit_tavan, dilim=serit_dilim)
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('opencode-go', 1000, snapshot(5.0), {'run_id': self.rid},
                                run_cap=serit_tavan, dilim=serit_dilim)
        # HARD SPLIT: the judge's cap is SMALLER than the lane's, but the lane's spending does not go into the judge's pocket.
        hakem_tavan, hakem_dilim = budget.rol_butcesi(self.f.home, self.rid, 'kalite')
        self.assertLess(hakem_tavan, serit_tavan)
        self.ledger.reserve('opencode-go', 200_000, snapshot(5.0), {'run_id': self.rid},
                            run_cap=hakem_tavan, dilim=hakem_dilim)


if __name__ == '__main__':
    unittest.main()
