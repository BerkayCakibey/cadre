"""Item 8 — the cheap layer's tick dropped from 60 s to 15 s.

Condition: while the tick gets more frequent, EXPENSIVE work must not. There are two guards — (a) the budget observation is cached on disk
for 60 s via `observed_budget`, so the router does not start a provider_budget.py PROCESS on every tick; (b) the escalation check stays the same
in TIME, not in tick count (multiplier 10 -> 40), otherwise it would run every 2.5 minutes instead of every 10."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))


class BudgetCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-tick-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'chain').mkdir()
        self.old = os.environ.get('CADRE_HOME')
        os.environ['CADRE_HOME'] = str(self.home)
        self.addCleanup(lambda: os.environ.__setitem__('CADRE_HOME', self.old)
                        if self.old else os.environ.pop('CADRE_HOME', None))
        import importlib
        import model_router
        importlib.reload(model_router)
        self.y = model_router

    def test_a_warm_shared_cache_means_no_new_process_per_tick(self):
        (self.home / 'chain/request-budget.json').write_text(json.dumps(
            {'observed': time.time(), 'budget': {'go': {'db': 'ok'}, 'mark': 'from-cache'}}))
        with patch.object(self.y.subprocess, 'run', side_effect=AssertionError('Do not start a process while the cache is valid')):
            for _ in range(4):                                  # four 15 s ticks = one minute
                data = self.y.budget_read()
        self.assertEqual(data.get('mark'), 'from-cache')

    def test_simulation_environment_bypasses_the_shared_cache(self):
        """The shared cache is only for REAL observation: if the caller injected a simulation environment the cache
        does not see it and answers wrongly (17.09: RC42 broke exactly like this)."""
        (self.home / 'chain/request-budget.json').write_text(json.dumps(
            {'observed': time.time(), 'budget': {'mark': 'from-cache'}}))
        complete = type('R', (), {'stdout': json.dumps({'mark': 'simulated'})})()
        for variable in ('CADRE_SIM_LOAD', 'CADRE_TEST_USED7', 'CADRE_TEST_USED5', 'CADRE_TEST_STALE_MIN'):
            with self.subTest(variable=variable):
                os.environ[variable] = '1'
                try:
                    with patch.object(self.y.subprocess, 'run', return_value=complete):
                        self.assertEqual(self.y.budget_read().get('mark'), 'simulated')
                finally:
                    os.environ.pop(variable, None)
        self.assertEqual(self.y.budget_read().get('mark'), 'from-cache')

    def test_a_cold_cache_still_produces_a_reading(self):
        import budget_gate
        with patch.object(budget_gate, 'observed_budget', return_value={'mark': 'fresh'}):
            self.assertEqual(self.y.budget_read().get('mark'), 'fresh')

    def test_a_broken_cache_falls_back_instead_of_returning_nothing(self):
        (self.home / 'chain/request-budget.json').write_text('{broken')
        import budget_gate
        with patch.object(budget_gate, 'observed_budget', side_effect=OSError('unreadable')):
            complete = type('R', (), {'stdout': json.dumps({'mark': 'reserve path'})})()
            with patch.object(self.y.subprocess, 'run', return_value=complete):
                self.assertEqual(self.y.budget_read().get('mark'), 'reserve path')


if __name__ == '__main__':
    unittest.main()
