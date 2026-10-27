"""§40.3 — the gate that replaces shadow mode: estimate ↔ actual.

Critical rule: the estimate is NEVER INVENTED. Today there is no history to produce an honest estimate; the module says so explicitly
and the gate starts working by itself as measurements accumulate. The unit is cost and time PER LANE,
because runs come in different sizes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import contract  # noqa: E402
import estimate_vs_actual as estimates  # noqa: E402
from run_store import RunStore  # noqa: E402


class EstimateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-tg-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'projects/p').mkdir(parents=True)
        (self.home / 'projects/p/repo-path').write_text(str(self.home))
        self.store = RunStore(self.home)

    def execute_run(self, spend, minute, lane=1, state='completed'):
        r = self.store.accept('p', 'task')
        rid = r['run_id']
        self.store.update(rid, state=state, budget_spent_usd=spend,
                          created=time.time() - minute * 60, updated=time.time())
        contract.create(self.home, rid, {'tasks': [{'id': f'g{i}'} for i in range(lane)]})
        return rid

    def test_no_prediction_is_invented_before_there_is_history(self):
        rid = self.execute_run(0.4, 30)
        s = estimates.compare(self.home, rid)
        self.assertIsNone(s['estimate'])
        self.assertIn('no estimate', s['gate'])
        self.assertEqual(s['real_usd'], 0.4)

    def test_the_prediction_appears_once_enough_runs_exist(self):
        for _ in range(3):
            self.execute_run(0.30, 20, lane=2)
        fresh = self.execute_run(0.32, 21, lane=2)
        s = estimates.compare(self.home, fresh)
        self.assertIsNotNone(s['estimate'])
        self.assertEqual(s['estimate']['sample'], 3)
        self.assertAlmostEqual(s['estimate']['usd'], 0.30, places=2)
        self.assertAlmostEqual(s['estimate']['min'], 20, delta=1)
        self.assertEqual(s['gate'], 'complete')

    def test_a_run_that_costs_far_more_than_history_trips_the_gate(self):
        for _ in range(3):
            self.execute_run(0.20, 20)
        expensive = self.execute_run(1.00, 20)
        s = estimates.compare(self.home, expensive)
        self.assertIn('DEVIATION', s['gate'])
        self.assertIn('expense', s['gate'])
        self.assertGreater(s['deviation_usd'], 0.5)

    def test_lane_count_makes_runs_of_different_sizes_comparable(self):
        for _ in range(3):
            self.execute_run(0.20, 10, lane=1)
        big = self.execute_run(0.80, 40, lane=4)          # the same cost per lane
        s = estimates.compare(self.home, big)
        self.assertEqual(s['lane'], 4)
        self.assertAlmostEqual(s['estimate']['usd'], 0.80, places=2)
        self.assertEqual(s['gate'], 'complete', 'Identical per-lane expense is not a deviation')

    def test_unfinished_runs_never_enter_the_history(self):
        for status in ('failed', 'cancelled', 'running'):
            self.execute_run(5.0, 60, state=status)
        self.assertEqual(estimates.completed_runs(self.home), [])
        self.assertIsNone(estimates.estimate(self.home, 1))

    def test_the_summary_line_is_empty_when_there_is_nothing_to_compare(self):
        self.assertEqual(estimates.summary(self.home), '')
        self.execute_run(0.1, 5)
        self.assertIn('no estimate', estimates.summary(self.home))


if __name__ == '__main__':
    unittest.main()
