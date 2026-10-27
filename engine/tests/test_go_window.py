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
import go_window  # noqa: E402


def raw(rolling=0, weekly=0, monthly=0):
    return {'usage': {'rolling': {'status': 'ok', 'percent': rolling, 'resetsAt': 'r'},
                      'weekly': {'status': 'ok', 'percent': weekly, 'resetsAt': 'w'},
                      'monthly': {'status': 'ok', 'percent': monthly, 'resetsAt': 'm'}}}


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-window-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def test_an_unreachable_endpoint_never_invents_a_percentage(self):
        with patch.object(go_window, '_pull', side_effect=OSError('ag absent')):
            self.assertIsNone(go_window.read(self.home))
        open, reason = go_window.status(None)
        self.assertTrue(open, 'Unknown usage alone does not close the window; the 429 marker is separate')
        self.assertEqual(reason, 'unknown')

    def test_the_last_reading_survives_a_network_outage(self):
        with patch.object(go_window, '_pull', return_value=raw(monthly=42)):
            self.assertEqual(go_window.read(self.home)['monthly'], 42)
        with patch.object(go_window, '_pull', side_effect=OSError('network unavailable')):
            data = go_window.read(self.home, fresh=True)
        self.assertEqual(data['monthly'], 42, 'Use the last known value without network access')
        old = go_window._cache(self.home)
        go_window._write(self.home, {**old, 'ts': time.time() - go_window.API_AGE_SEC - 60})
        with patch.object(go_window, '_pull', side_effect=OSError('network unavailable')):
            self.assertIsNone(go_window.read(self.home, fresh=True), 'Discard stale percentages; the gate falls back to the sum formula')

    def test_any_window_at_a_hundred_closes_the_pool(self):
        for i, kw in enumerate(({'rolling': 100}, {'weekly': 100}, {'monthly': 100})):
            home_dir = self.home / f'home_dir{i}'
            home_dir.mkdir()
            with patch.object(go_window, '_pull', return_value=raw(**kw)):
                data = go_window.read(home_dir, fresh=True)
            open, reason = go_window.status(data)
            self.assertFalse(open, kw)
            self.assertIn('pool closed', reason)

    def test_the_wall_margin_stops_work_before_the_limit_is_hit(self):
        with patch.object(go_window, '_pull', return_value=raw(monthly=89)):
            data = go_window.read(self.home, fresh=True)
        self.assertEqual(go_window.wall_share(data), 5.0)
        # GO1 (03.10): the wall margin is a fixed five points; at 95% new work closes. No truncation margin.
        self.assertTrue(go_window.status(data)[0])                 # 89 + 5 = 94 < 100
        with patch.object(go_window, '_pull', return_value=raw(monthly=94)):
            data = go_window.read(self.home, fresh=True)
        self.assertTrue(go_window.status(data)[0])                 # 94 + 5 = 99 < 100
        with patch.object(go_window, '_pull', return_value=raw(monthly=95)):
            data = go_window.read(self.home, fresh=True)
        open, reason = go_window.status(data)
        self.assertFalse(open)
        self.assertIn('wall share', reason)

    def test_a_big_jump_between_readings_widens_the_wall(self):
        # GO1 (03.10): the wall margin is a fixed five points; a provider route hand-off keeps the running work,
        # the wall no longer grows with the jump between readings.
        for percent in (10, 30, 55):
            with patch.object(go_window, '_pull', return_value=raw(monthly=percent)):
                data = go_window.read(self.home, fresh=True)
        self.assertEqual(go_window.wall_share(data), 5.0)        # the wall is fixed even if the jump grows
        with patch.object(go_window, '_pull', return_value=raw(monthly=94)):
            data = go_window.read(self.home, fresh=True)
        open, reason = go_window.status(data)
        self.assertTrue(open, '94 + 5 = 99 < 100 -> still open')

    def test_history_is_bounded_and_the_cache_has_a_ttl(self):
        for i in range(HISTORY := go_window.HISTORY_N + 7):
            with patch.object(go_window, '_pull', return_value=raw(monthly=i)):
                data = go_window.read(self.home, fresh=True)
        self.assertEqual(len(data['history']), go_window.HISTORY_N)
        # no network within the TTL
        with patch.object(go_window, '_pull', side_effect=AssertionError('Do not access the network within the TTL')):
            self.assertEqual(go_window.read(self.home)['monthly'], HISTORY - 1)


    def test_the_gate_is_one_point_conservative_because_the_endpoint_truncates(self):
        """GO1 (03.10): the truncation margin was removed; since a provider route hand-off keeps the running work, no extra
        one-point conservatism is needed. The gate closes only with the five-point wall margin."""
        with patch.object(go_window, '_pull', return_value=raw(monthly=89)):
            data = go_window.read(self.home, fresh=True)
        open, reason = go_window.status(data)
        self.assertTrue(open, '89 + 5 = 94 -> open')
        with patch.object(go_window, '_pull', return_value=raw(monthly=95)):
            data = go_window.read(self.home, fresh=True)
        self.assertFalse(go_window.status(data)[0], '95 + 5 = 100 -> closed')

    def test_the_status_summary_reads_the_cache_and_never_touches_the_network(self):
        with patch.object(go_window, '_pull', return_value=raw(rolling=3, weekly=12, monthly=27)):
            go_window.read(self.home, fresh=True)
        with patch.object(go_window, '_pull', side_effect=AssertionError('The summary must not access the network')):
            line = go_window.summary(self.home)
        self.assertIn('rolling %3', line)
        self.assertIn('weekly %12', line)
        self.assertIn('monthly %27', line)
        self.assertIn('open', line)
        self.assertEqual(go_window.summary(self.home / 'missing-model'), '', 'Without a cache, omit the line')


class BudgetIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-window-b-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'chain').mkdir()

    def budget(self):
        import subprocess
        env = {**os.environ, 'CADRE_HOME': str(self.home), 'OPENCODE_DB': str(self.home / 'absent.db')}
        r = subprocess.run([sys.executable, str(Path(go_window.__file__).with_name('provider_budget.py'))],
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)['go']

    def test_the_budget_report_carries_the_window_percentages_and_the_reason(self):
        (self.home / 'chain/go-window.json').write_text(json.dumps(
            {'ts': time.time(), 'rolling': 5, 'weekly': 12, 'monthly': 95, 'resets': {}, 'history': []}))
        g = self.budget()
        self.assertFalse(g['window_open'])
        self.assertIn('monthly', g['window_reason'])
        self.assertEqual(g['windows']['monthly'], 95)

    def test_the_simulation_can_drive_the_window_percentages(self):
        """§40 infrastructure: the limit scenarios must be able to drive the window with CADRE_SIM_LOAD, without depending on the network."""
        import subprocess
        sim = self.home / 'sim.json'
        sim.write_text(json.dumps({'window': {'monthly': 97}}))
        (self.home / 'chain/go-window.json').write_text(json.dumps(
            {'ts': time.time(), 'rolling': 0, 'weekly': 0, 'monthly': 0, 'resets': {}, 'history': []}))
        env = {**os.environ, 'CADRE_HOME': str(self.home), 'OPENCODE_DB': str(self.home / 'absent.db'),
               'CADRE_SIM_LOAD': str(sim)}
        r = subprocess.run([sys.executable, str(Path(go_window.__file__).with_name('provider_budget.py'))],
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        g = json.loads(r.stdout)['go']
        self.assertFalse(g['window_open'])
        self.assertIn('97%', g['window_reason'])

    def test_a_429_stamp_still_closes_the_window_on_its_own(self):
        (self.home / 'chain/go-429-last.txt').write_text(str(int(time.time())))
        (self.home / 'chain/go-window.json').write_text(json.dumps(
            {'ts': time.time(), 'rolling': 0, 'weekly': 0, 'monthly': 0, 'resets': {}, 'history': []}))
        g = self.budget()
        self.assertFalse(g['window_open'])
        self.assertIn('429', g['window_reason'])


if __name__ == '__main__':
    unittest.main()


class FiveHourWindowTests(unittest.TestCase):
    """4c-19 reality probe (07.10 live): the 5-hour window Σ looked at a fixed "last 5 hours" (Σ 3%, API 0%); Go counts the window
    back from the reset moment. If the reset moment is known the window starts there, otherwise the last 5 hours (cautious)."""

    def test_the_rolling_window_starts_where_the_api_says_it_did(self):
        import sqlite3
        from contextlib import closing
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as t:
            db = Path(t) / 'opencode.db'
            now_ts = time.time()
            with closing(sqlite3.connect(db)) as con:
                con.execute('create table message(id text, data text)')
                con.execute('create table part(message_id text, data text, time_created integer)')
                con.execute("insert into message values('m', ?)", (json.dumps({'providerID': 'opencode-go', 'modelID': 'glm-5.3'}),))
                for once_sec in (4 * 3600, 1800):
                    con.execute("insert into part values('m', ?, ?)",
                                (json.dumps({'type': 'step-finish', 'cost': 0.15}), int((now_ts - once_sec) * 1000)))
                con.commit()
            one = go_window.points('glm-5.3', 0.15) / go_window.WINDOW_SHARE['rolling']
            reset = datetime.fromtimestamp(now_ts + 2 * 3600, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
            self.assertAlmostEqual(go_window.own_percentages(db, now_ts, {'rolling': reset}, now_ts - 86400)['rolling'], one, 3)
            self.assertAlmostEqual(go_window.own_percentages(db, now_ts, {}, now_ts - 86400)['rolling'], 2 * one, 3)
            # 4c QA2: if the cached reset has passed, the new window starts at that moment (the window that ended earlier does not count)
            history = datetime.fromtimestamp(now_ts - 3600, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
            self.assertAlmostEqual(go_window.own_percentages(db, now_ts, {'rolling': history}, now_ts - 86400)['rolling'], one, 3)
