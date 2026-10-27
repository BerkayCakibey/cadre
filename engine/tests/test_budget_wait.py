"""Design 3.3 (G-E11 waiting for the budget to come back): at hard+ level the plan and the correction round wait when there is no strong (paid, budgeted) cloud
and ≤2 h remain until the daily shares renew; otherwise they continue with what is available (local/free). No model/network."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import model_router  # noqa: E402


class StrongModelTest(unittest.TestCase):
    def test_empty_is_not_strong(self):
        self.assertFalse(model_router.is_strong(''))

    def test_paid_cloud_and_claude_are_strong(self):
        with patch('model_catalog.is_free', return_value=False):
            self.assertTrue(model_router.is_strong('openrouter/deepseek/deepseek-v4-pro'))
            self.assertTrue(model_router.is_strong('claude/opus#high'))

    def test_free_model_is_not_strong(self):
        with patch('model_catalog.is_free', return_value=True):
            self.assertFalse(model_router.is_strong('opencode/big-pickle'))


class CloudWaitTest(unittest.TestCase):
    def decision(self, chain, points, hour, minute=0):
        now_ts = datetime(2026, 9, 24, hour, minute).timestamp()
        with patch.object(model_router, 'chain', return_value=(chain, [])), \
                patch('model_catalog.is_free', side_effect=lambda provider_key, model: 'pickle' in model):
            return model_router.cloud_wait('plan', 'hard', points, now_ts=now_ts, threshold=model_router.WAIT_THRESHOLD_SEC)

    def test_hard_job_waits_if_renewal_is_near_without_a_strong_cloud(self):
        self.assertEqual(self.decision([], 50, 22, 30), 90 * 60 + 60)

    def test_waits_even_if_only_a_free_model_remains(self):
        self.assertGreater(self.decision(['opencode/big-pickle'], 70, 23), 0)

    def test_does_not_wait_if_the_renewal_is_far(self):
        self.assertEqual(self.decision([], 50, 20), 0)

    def test_does_not_wait_if_the_threshold_is_zeroed_from_the_environment(self):
        """B268 (29.09): the dispatcher e2e fixtures waited on the real clock; a test run between 22:00-00:00 hung until
        midnight. `CADRE_WAIT_THRESHOLD_SEC=0` disables waiting (for the fixtures and the operator)."""
        now_ts = datetime(2026, 9, 24, 22, 30).timestamp()
        with patch.object(model_router, 'chain', return_value=([], [])), \
                patch('model_catalog.is_free', return_value=False), patch.dict(os.environ, {'CADRE_WAIT_THRESHOLD_SEC': '0'}):
            self.assertEqual(model_router.cloud_wait('plan', 'hard', 50, now_ts=now_ts), 0)

    def test_medium_job_does_not_wait(self):
        self.assertEqual(self.decision([], 30, 23, 30), 0)

    def test_does_not_wait_if_a_strong_cloud_exists(self):
        self.assertEqual(self.decision(['openrouter/deepseek/deepseek-v4-pro'], 50, 23, 30), 0)


if __name__ == '__main__':
    unittest.main()
