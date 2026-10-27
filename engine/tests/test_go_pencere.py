"""Item 5 — Go's THREE WINDOWS. Until now the only source of "is the window open" was the 429 stamp:
we learned about the limit only AFTER HITTING it. Now the rolling/weekly/monthly percentages are read and the gate closes
before touching the wall. The percentage IS THE GATE; it is not converted to dollars."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import go_pencere  # noqa: E402


def ham(rolling=0, weekly=0, monthly=0):
    return {'usage': {'rolling': {'status': 'ok', 'percent': rolling, 'resetsAt': 'r'},
                      'weekly': {'status': 'ok', 'percent': weekly, 'resetsAt': 'w'},
                      'monthly': {'status': 'ok', 'percent': monthly, 'resetsAt': 'm'}}}


class PencereTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-pencere-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def test_an_unreachable_endpoint_never_invents_a_percentage(self):
        with patch.object(go_pencere, '_cek', side_effect=OSError('ag yok')):
            self.assertIsNone(go_pencere.oku(self.home))
        acik, neden = go_pencere.durum(None)
        self.assertTrue(acik, 'bilinmeyen tek basina kapatmaz; 429 damgasi ayri calisir')
        self.assertEqual(neden, 'bilinmiyor')

    def test_the_last_reading_survives_a_network_outage(self):
        with patch.object(go_pencere, '_cek', return_value=ham(monthly=42)):
            self.assertEqual(go_pencere.oku(self.home)['monthly'], 42)
        with patch.object(go_pencere, '_cek', side_effect=OSError('ag gitti')):
            veri = go_pencere.oku(self.home, taze=True)
        self.assertEqual(veri['monthly'], 42, 'ag yokken son bilinen kullanilir')
        eski = go_pencere._onbellek(self.home)
        go_pencere._yaz(self.home, {**eski, 'ts': time.time() - go_pencere.API_YAS_SN - 60})
        with patch.object(go_pencere, '_cek', side_effect=OSError('ag gitti')):
            self.assertIsNone(go_pencere.oku(self.home, taze=True), 'cok eski yuzde atilir; kapi Σ formulune duser')

    def test_any_window_at_a_hundred_closes_the_pool(self):
        for i, kw in enumerate(({'rolling': 100}, {'weekly': 100}, {'monthly': 100})):
            ev = self.home / f'ev{i}'
            ev.mkdir()
            with patch.object(go_pencere, '_cek', return_value=ham(**kw)):
                veri = go_pencere.oku(ev, taze=True)
            acik, neden = go_pencere.durum(veri)
            self.assertFalse(acik, kw)
            self.assertIn('havuz kapali', neden)

    def test_the_wall_margin_stops_work_before_the_limit_is_hit(self):
        with patch.object(go_pencere, '_cek', return_value=ham(monthly=89)):
            veri = go_pencere.oku(self.home, taze=True)
        self.assertEqual(go_pencere.duvar_payi(veri), 5.0)
        # GO1 (03.10): the wall margin is a fixed five points; at 95% new work closes. No truncation margin.
        self.assertTrue(go_pencere.durum(veri)[0])                 # 89 + 5 = 94 < 100
        with patch.object(go_pencere, '_cek', return_value=ham(monthly=94)):
            veri = go_pencere.oku(self.home, taze=True)
        self.assertTrue(go_pencere.durum(veri)[0])                 # 94 + 5 = 99 < 100
        with patch.object(go_pencere, '_cek', return_value=ham(monthly=95)):
            veri = go_pencere.oku(self.home, taze=True)
        acik, neden = go_pencere.durum(veri)
        self.assertFalse(acik)
        self.assertIn('duvar payi', neden)

    def test_a_big_jump_between_readings_widens_the_wall(self):
        # GO1 (03.10): the wall margin is a fixed five points; a provider route hand-off keeps the running work,
        # the wall no longer grows with the jump between readings.
        for yuzde in (10, 30, 55):
            with patch.object(go_pencere, '_cek', return_value=ham(monthly=yuzde)):
                veri = go_pencere.oku(self.home, taze=True)
        self.assertEqual(go_pencere.duvar_payi(veri), 5.0)        # the wall is fixed even if the jump grows
        with patch.object(go_pencere, '_cek', return_value=ham(monthly=94)):
            veri = go_pencere.oku(self.home, taze=True)
        acik, neden = go_pencere.durum(veri)
        self.assertTrue(acik, '94 + 5 = 99 < 100 -> hala acik')

    def test_history_is_bounded_and_the_cache_has_a_ttl(self):
        for i in range(GECMIS := go_pencere.GECMIS_N + 7):
            with patch.object(go_pencere, '_cek', return_value=ham(monthly=i)):
                veri = go_pencere.oku(self.home, taze=True)
        self.assertEqual(len(veri['gecmis']), go_pencere.GECMIS_N)
        # no network within the TTL
        with patch.object(go_pencere, '_cek', side_effect=AssertionError('TTL icinde aga gidilmemeli')):
            self.assertEqual(go_pencere.oku(self.home)['monthly'], GECMIS - 1)


    def test_the_gate_is_one_point_conservative_because_the_endpoint_truncates(self):
        """GO1 (03.10): the truncation margin was removed; since a provider route hand-off keeps the running work, no extra
        one-point conservatism is needed. The gate closes only with the five-point wall margin."""
        with patch.object(go_pencere, '_cek', return_value=ham(monthly=89)):
            veri = go_pencere.oku(self.home, taze=True)
        acik, neden = go_pencere.durum(veri)
        self.assertTrue(acik, '89 + 5 = 94 -> acik')
        with patch.object(go_pencere, '_cek', return_value=ham(monthly=95)):
            veri = go_pencere.oku(self.home, taze=True)
        self.assertFalse(go_pencere.durum(veri)[0], '95 + 5 = 100 -> kapali')

    def test_the_status_summary_reads_the_cache_and_never_touches_the_network(self):
        with patch.object(go_pencere, '_cek', return_value=ham(rolling=3, weekly=12, monthly=27)):
            go_pencere.oku(self.home, taze=True)
        with patch.object(go_pencere, '_cek', side_effect=AssertionError('ozet aga gitmemeli')):
            satir = go_pencere.ozet(self.home)
        self.assertIn('rolling %3', satir)
        self.assertIn('weekly %12', satir)
        self.assertIn('monthly %27', satir)
        self.assertIn('acik', satir)
        self.assertEqual(go_pencere.ozet(self.home / 'hic-yok'), '', 'onbellek yoksa satir da yok')


class ButceEntegrasyonuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-pencere-b-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'zincir').mkdir()

    def butce(self):
        import subprocess
        env = {**os.environ, 'CADRE_HOME': str(self.home), 'OPENCODE_DB': str(self.home / 'yok.db')}
        r = subprocess.run([sys.executable, str(Path(go_pencere.__file__).with_name('butce.py'))],
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)['go']

    def test_the_budget_report_carries_the_window_percentages_and_the_reason(self):
        (self.home / 'zincir/go-pencere.json').write_text(json.dumps(
            {'ts': time.time(), 'rolling': 5, 'weekly': 12, 'monthly': 95, 'resets': {}, 'gecmis': []}))
        g = self.butce()
        self.assertFalse(g['pencere_acik'])
        self.assertIn('monthly', g['pencere_neden'])
        self.assertEqual(g['pencereler']['monthly'], 95)

    def test_the_simulation_can_drive_the_window_percentages(self):
        """§40 infrastructure: the limit scenarios must be able to drive the window with CADRE_SIM_YUK, without depending on the network."""
        import subprocess
        sim = self.home / 'sim.json'
        sim.write_text(json.dumps({'pencere': {'monthly': 97}}))
        (self.home / 'zincir/go-pencere.json').write_text(json.dumps(
            {'ts': time.time(), 'rolling': 0, 'weekly': 0, 'monthly': 0, 'resets': {}, 'gecmis': []}))
        env = {**os.environ, 'CADRE_HOME': str(self.home), 'OPENCODE_DB': str(self.home / 'yok.db'),
               'CADRE_SIM_YUK': str(sim)}
        r = subprocess.run([sys.executable, str(Path(go_pencere.__file__).with_name('butce.py'))],
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        g = json.loads(r.stdout)['go']
        self.assertFalse(g['pencere_acik'])
        self.assertIn('%97', g['pencere_neden'])

    def test_a_429_stamp_still_closes_the_window_on_its_own(self):
        (self.home / 'zincir/go-429-son.txt').write_text(str(int(time.time())))
        (self.home / 'zincir/go-pencere.json').write_text(json.dumps(
            {'ts': time.time(), 'rolling': 0, 'weekly': 0, 'monthly': 0, 'resets': {}, 'gecmis': []}))
        g = self.butce()
        self.assertFalse(g['pencere_acik'])
        self.assertIn('429', g['pencere_neden'])


if __name__ == '__main__':
    unittest.main()


class BesSaatlikPencereTests(unittest.TestCase):
    """4c-19 reality probe (07.10 live): the 5-hour window Σ looked at a fixed "last 5 hours" (Σ 3%, API 0%); Go counts the window
    back from the reset moment. If the reset moment is known the window starts there, otherwise the last 5 hours (cautious)."""

    def test_the_rolling_window_starts_where_the_api_says_it_did(self):
        import sqlite3
        from contextlib import closing
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as t:
            db = Path(t) / 'opencode.db'
            simdi = time.time()
            with closing(sqlite3.connect(db)) as con:
                con.execute('create table message(id text, data text)')
                con.execute('create table part(message_id text, data text, time_created integer)')
                con.execute("insert into message values('m', ?)", (json.dumps({'providerID': 'opencode-go', 'modelID': 'glm-5.3'}),))
                for once_sn in (4 * 3600, 1800):
                    con.execute("insert into part values('m', ?, ?)",
                                (json.dumps({'type': 'step-finish', 'cost': 0.15}), int((simdi - once_sn) * 1000)))
                con.commit()
            bir = go_pencere.puan('glm-5.3', 0.15) / go_pencere.PENCERE_PAYI['rolling']
            sifirlanma = datetime.fromtimestamp(simdi + 2 * 3600, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
            self.assertAlmostEqual(go_pencere.kendi_yuzdeler(db, simdi, {'rolling': sifirlanma}, simdi - 86400)['rolling'], bir, 3)
            self.assertAlmostEqual(go_pencere.kendi_yuzdeler(db, simdi, {}, simdi - 86400)['rolling'], 2 * bir, 3)
            # 4c QA2: if the cached reset has passed, the new window starts at that moment (the window that ended earlier does not count)
            gecmis = datetime.fromtimestamp(simdi - 3600, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
            self.assertAlmostEqual(go_pencere.kendi_yuzdeler(db, simdi, {'rolling': gecmis}, simdi - 86400)['rolling'], bir, 3)
