"""Item 8 — the cheap layer's tick dropped from 60 s to 15 s.

Condition: while the tick gets more frequent, EXPENSIVE work must not. There are two guards — (a) the budget observation is cached on disk
for 60 s via `observed_budget`, so the router does not start a butce.py PROCESS on every tick; (b) the escalation check stays the same
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


class ButceOnbellegiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-tik-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'zincir').mkdir()
        self.eski = os.environ.get('CADRE_HOME')
        os.environ['CADRE_HOME'] = str(self.home)
        self.addCleanup(lambda: os.environ.__setitem__('CADRE_HOME', self.eski)
                        if self.eski else os.environ.pop('CADRE_HOME', None))
        import importlib
        import yonlendirici
        importlib.reload(yonlendirici)
        self.y = yonlendirici

    def test_a_warm_shared_cache_means_no_new_process_per_tick(self):
        (self.home / 'zincir/request-budget.json').write_text(json.dumps(
            {'observed': time.time(), 'budget': {'go': {'db': 'ok'}, 'isaret': 'onbellekten'}}))
        with patch.object(self.y.subprocess, 'run', side_effect=AssertionError('onbellek varken surec baslatilmamali')):
            for _ in range(4):                                  # four 15 s ticks = one minute
                veri = self.y.butce_oku()
        self.assertEqual(veri.get('isaret'), 'onbellekten')

    def test_simulation_environment_bypasses_the_shared_cache(self):
        """The shared cache is only for REAL observation: if the caller injected a simulation environment the cache
        does not see it and answers wrongly (17.09: RC42 broke exactly like this)."""
        (self.home / 'zincir/request-budget.json').write_text(json.dumps(
            {'observed': time.time(), 'budget': {'isaret': 'onbellekten'}}))
        tamam = type('R', (), {'stdout': json.dumps({'isaret': 'simulasyondan'})})()
        for degisken in ('CADRE_SIM_YUK', 'CADRE_TEST_USED7', 'CADRE_TEST_USED5', 'CADRE_TEST_STALE_DK'):
            with self.subTest(degisken=degisken):
                os.environ[degisken] = '1'
                try:
                    with patch.object(self.y.subprocess, 'run', return_value=tamam):
                        self.assertEqual(self.y.butce_oku().get('isaret'), 'simulasyondan')
                finally:
                    os.environ.pop(degisken, None)
        self.assertEqual(self.y.butce_oku().get('isaret'), 'onbellekten')

    def test_a_cold_cache_still_produces_a_reading(self):
        import butce_kapisi
        with patch.object(butce_kapisi, 'observed_budget', return_value={'isaret': 'taze'}):
            self.assertEqual(self.y.butce_oku().get('isaret'), 'taze')

    def test_a_broken_cache_falls_back_instead_of_returning_nothing(self):
        (self.home / 'zincir/request-budget.json').write_text('{bozuk')
        import butce_kapisi
        with patch.object(butce_kapisi, 'observed_budget', side_effect=OSError('okunamadi')):
            tamam = type('R', (), {'stdout': json.dumps({'isaret': 'yedek yol'})})()
            with patch.object(self.y.subprocess, 'run', return_value=tamam):
                self.assertEqual(self.y.butce_oku().get('isaret'), 'yedek yol')


if __name__ == '__main__':
    unittest.main()
