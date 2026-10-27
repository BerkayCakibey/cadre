"""Item 7 — pool ordering: budget surplus + the CURRENT UNIT PRICE, with hysteresis.

Because the order was recomputed on every call, even the day's first spending could flip the order and change model
in the middle of a job. Hysteresis: a change must be both CLEAR and SUSTAINED. (DeepSeek's peak hours are in Go accounting since 4c-1.)"""

from __future__ import annotations

import calendar
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))


def utc(iso):
    return calendar.timegm(time.strptime(iso, '%Y-%m-%d %H:%M'))


class DenseHourTests(unittest.TestCase):
    """4c-1: the direct DeepSeek route was removed; peak hours moved to Go accounting (Go charges 2× points for DeepSeek models)."""

    def test_the_peak_window_matches_the_official_table(self):
        import go_window
        for iso, peak in [('2026-09-17 00:59', False), ('2026-09-17 01:00', True), ('2026-09-17 03:59', True),
                          ('2026-09-17 04:00', False), ('2026-09-17 05:59', False), ('2026-09-17 06:00', True),
                          ('2026-09-17 09:59', True), ('2026-09-17 10:00', False),
                          ('2026-09-19 02:00', False), ('2026-09-20 07:00', False)]:   # weekends are always off-peak
            with self.subTest(iso=iso):
                self.assertEqual(go_window.dense_hour(utc(iso)), peak)

    def test_only_go_deepseek_models_double_at_peak(self):
        import go_window
        peak = utc('2026-09-17 02:00')
        self.assertEqual(go_window.moment_factor('deepseek-v4-pro', peak), 2)
        self.assertEqual(go_window.moment_factor('deepseek-v4-pro', utc('2026-09-17 12:00')), 1)
        for model in ('glm-5.3', 'qwen3.8-max', 'kimi-k3'):
            self.assertEqual(go_window.moment_factor(model, peak), 1)


class HysteresisTests(unittest.TestCase):
    """An order change must be both CLEAR and SUSTAINED; the abundance measure is pinned here (the topic is hysteresis)."""
    ZEN, GO = 'opencode/deepseek-v4-flash', 'opencode-go/glm-5.2'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-order-')
        self.addCleanup(self.temp.cleanup)
        home_dir = Path(self.temp.name)
        self.old = {k: os.environ.get(k) for k in ('CADRE_ROUTER_STATUS', 'CADRE_ROUTER_RECORD')}
        os.environ['CADRE_ROUTER_STATUS'] = str(home_dir / 'status.json')
        os.environ['CADRE_ROUTER_RECORD'] = os.devnull
        self.addCleanup(self.go_back)
        import importlib
        from unittest.mock import patch
        import model_router
        importlib.reload(model_router)
        self.y = model_router
        abundance = patch.object(self.y, 'abundance', side_effect=lambda provider_key, b: (b or {}).get(provider_key, 1.0))
        abundance.start(); self.addCleanup(abundance.stop)

    def go_back(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def budget(self, zen_ratio=1.0):
        return {'opencode': zen_ratio, 'opencode-go': 1.0}

    def test_the_first_reading_is_taken_at_face_value(self):
        """Without a memory there is no CHANGE to damp: the current measure is applied directly."""
        d = {}
        ordered = self.y.abundance_by_sort([self.ZEN, self.GO], self.budget(zen_ratio=0.01), d, utc('2026-09-17 12:00'))
        self.assertEqual(ordered[0], self.GO)
        self.assertTrue(d['_order']['opencode']['compact'])

    def test_an_established_order_does_not_flip_on_a_brief_change(self):
        d = {}
        t0 = utc('2026-09-17 12:00')
        candidates = [self.ZEN, self.GO]
        self.y.abundance_by_sort(candidates, self.budget(), d, t0)                     # before: plentiful, normal order
        self.assertFalse(d['_order']['opencode']['compact'])
        early = self.y.abundance_by_sort(candidates, self.budget(zen_ratio=0.01), d, t0 + 60)
        self.assertEqual(early, candidates, 'A one-minute drop must not overturn the order')
        # the timer is set when the NEW TREND is first seen (t0+60); five minutes later it fills
        late = self.y.abundance_by_sort(candidates, self.budget(zen_ratio=0.01), d, t0 + 361)
        self.assertEqual(late[0], self.GO, 'The order changes after five minutes')

    def test_a_change_that_is_not_clear_enough_never_flips(self):
        """A value wandering just below the threshold (0.25 -> 0.20) does not flip the order; the difference must be at least 0.20 points."""
        d = {}
        t0 = utc('2026-09-17 12:00')
        candidates = [self.ZEN, self.GO]
        self.y.abundance_by_sort(candidates, self.budget(), d, t0)
        for passing in (60, 301, 900):
            ordered = self.y.abundance_by_sort(candidates, self.budget(zen_ratio=0.20), d, t0 + passing)
            self.assertEqual(ordered, candidates, f'No significant difference even after {passing}s')

    def test_the_memory_survives_a_new_process(self):
        """If the hysteresis memory is not written to disk every call is the 'first call' and hysteresis never works."""
        from unittest.mock import patch
        t0 = utc('2026-09-17 12:00')
        with patch.object(self.y, 'has_budget', return_value=(True, 'test')), \
             patch.object(self.y, 'in_cooldown', return_value=False):
            self.y.chain('impl', b=self.budget(), d=self.y.status_read(), now_ts=t0)
        enrolled = json.loads(Path(os.environ['CADRE_ROUTER_STATUS']).read_text())
        self.assertIn('_order', enrolled)
        self.assertLessEqual({'opencode-go', 'openrouter'}, set(enrolled['_order']), 'Routes in the same tier of the code chain')


if __name__ == '__main__':
    unittest.main()


class FailureClassificationTests(unittest.TestCase):
    """B110 (18.09): `_failure_locked` counted a CONTEXT error and a NETWORK outage as quota too, with a fixed "rate/limit/too large" string;
    the provider closed for 60 minutes. A context error means our request is too big,
    a network error is temporary; neither has anything to do with the provider's quota."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-failure-')
        self.addCleanup(self.temp.cleanup)
        os.environ['CADRE_HOME'] = self.temp.name
        import model_router
        self.y = model_router

    def category(self, error):
        status = self.y._failure_locked('provider', error, d={}, now_ts=1000.0)['provider']
        return status['cooldown_until'], status['last']

    def test_real_quota_signals_still_close_the_provider_for_an_hour(self):
        for error in ('429 Too Many Requests', 'You exceeded your current quota',
                     'Request too large for model on tokens per minute (TPM)', 'daily limit reached'):
            with self.subTest(error=error):
                end, last = self.category(error)
                self.assertEqual(end, 1000.0 + 3600)
                self.assertIn('[quota]', last)

    def test_context_and_network_errors_are_not_quota_and_do_not_close_the_provider(self):
        for error, category in (('context length limit exceeded: 40000 > 32768', '[context]'),
                            ('prompt is too long', '[context]'),
                            ('connection timed out after 30s', '[net]'),
                            ('503 Service Unavailable', '[net]'),
                            ('fetch failed', '[net]')):
            with self.subTest(error=error):
                end, last = self.category(error)
                self.assertEqual(end, 0, f'{error}: Only quota errors may close the provider for 60 minutes')
                self.assertIn(category, last)
