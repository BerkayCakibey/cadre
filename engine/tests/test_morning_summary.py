"""Item 10 — the morning summary. The user's rule: the state is read only with `cadre`. Information produced overnight
was scattered across log lines and run.json fields; there was no single answer in the morning to "what happened, how much was spent, where did it get stuck".
The summary computes nothing, it only collects — and suppresses the noise."""

from __future__ import annotations

import json
import os
from pathlib import Path
from contextlib import closing
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import morning_summary  # noqa: E402
from run_store import RunStore  # noqa: E402

BUDGET = {
    'go': {'db': 'ok', 'pay': 77.0, 'available': 50.0, 'month_points': 27.0, 'window_open': True, 'window_reason': 'open',
           'windows': {'rolling': 3, 'weekly': 12, 'monthly': 27, 'resets': {'monthly': '2026-10-02T19:17Z'}}, 'unit': 'points'},
    'or': {'pay': 0.9, 'available': 0.9, 'remaining': 4.0},
    'zen': {'pay': 0.35, 'available': 0.3, 'remaining': 8.39},
    'claude': {'pay': 4.8, 'used7': 63.0, 'cap': 77.5},
}


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-summary-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'projects/p').mkdir(parents=True)
        (self.home / 'projects/p/repo-path').write_text(str(self.home))
        (self.home / 'chain').mkdir(exist_ok=True)
        self.store = RunStore(self.home)

    def execute_run(self, state, spend=0.0, denials=None, phase='runner'):
        r = self.store.accept('p', 'job')
        self.store.update(r['run_id'], state=state, budget_spent_usd=spend, phase=phase,
                          **({'denials': denials} if denials else {}))
        return r['run_id']

    def produce(self):
        with patch.object(morning_summary, 'budget', return_value=BUDGET):
            return morning_summary.produce(self.home, detail=True)

    def test_runs_that_spent_nothing_are_collapsed_into_one_line(self):
        for _ in range(5):
            self.execute_run('cancelled')
        running = self.execute_run('completed', spend=0.356)
        text = self.produce()
        self.assertIn('(5 run(s) cancelled without spending anything)', text)
        self.assertIn(running[:19], text)
        self.assertIn('total spent $0.356', text)

    def test_a_cancelled_run_that_did_spend_is_still_shown(self):
        self.execute_run('cancelled', spend=0.435)
        text = self.produce()
        self.assertIn('$0.435', text)
        self.assertNotIn('cancelled without spending', text)

    def test_denials_are_carried_so_the_morning_shows_what_blocked_work(self):
        self.execute_run('failed', spend=0.02, denials=[{'provider': 'opencode-go', 'reason': 'allocation exhausted'}])
        text = self.produce()
        self.assertIn('denied: opencode-go', text)
        self.assertIn('allocation exhausted', text)

    def test_the_three_windows_and_every_pool_are_reported(self):
        self.execute_run('completed', spend=0.1)
        text = self.produce()
        self.assertIn('rolling %3', text)
        self.assertIn('weekly %12', text)
        self.assertIn('monthly %27', text)
        self.assertIn('monthly reset: 2026-10-02T19:17Z', text)
        for entry_name in ('Go:', 'OpenRouter:', 'Zen:', 'Claude:'):
            self.assertIn(entry_name, text)
        self.assertIn('balance $8.39', text)

    def test_stale_reservations_appear_under_attention(self):
        db = self.home / 'chain/request-budget.db'
        with closing(sqlite3.connect(db)) as con, con:
            con.execute('create table reservation(id text primary key, provider text, period text, created real, '
                        'reserved integer, actual integer, receipt text, metadata text)')
            con.execute('insert into reservation values(?,?,?,?,?,null,null,?)',
                        ('a', 'opencode-go', 'p', time.time() - 9 * 3600, 1_230_000, '{}'))
        text = self.produce()
        self.assertIn('1 stale reservation(s) ($1.23)', text)
        self.assertIn('cadre recover', text)

    def test_a_closed_window_is_called_out(self):
        self.execute_run('completed')
        closed = json.loads(json.dumps(BUDGET))
        closed['go']['window_open'] = False
        closed['go']['window_reason'] = 'monthly %100 — pool closed'
        with patch.object(morning_summary, 'budget', return_value=closed):
            text = morning_summary.produce(self.home, detail=True)
        self.assertIn('Go window closed', text)
        self.assertIn('pool closed', text)

    def test_writing_produces_a_dated_file(self):
        self.execute_run('completed', spend=0.5)
        with patch.object(morning_summary, 'budget', return_value=BUDGET):
            path = morning_summary.write(self.home)
        self.assertTrue(path.is_file())
        self.assertIn(time.strftime('%Y%m%d'), path.name)
        self.assertIn('Night summary', path.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
