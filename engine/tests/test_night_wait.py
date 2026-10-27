"""P3 (03.10): the local model is OFF by default. The local wait/escalation loops were removed; without cloud capacity
the engine leaves the job partial with `budget-wait` (the coordinator marks it retryable, it continues when the budget returns). The coordinator writes structural
reasons (no-capacity/disk-low) as `failed` (no retry). Fake model state, fake budget, fake executor;
no network/model/Windows."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import test_run_sessions as fixtures
import qe_core as core
from run_store import RunStore

LIB = fixtures.LIB


def full_budget(cloud=True):
    day = time.strftime('%Y-%m-%d')
    pay = 5.0 if cloud else 0.0
    return {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'today': day, 'remaining_day': 10,
            'go': {'db': 'ok', 'pay': (pay) * 100 / 15, 'available': (pay) * 100 / 15, 'window_open': True, 'ratio': 1.0 if cloud else 0.0, 'unit': 'points'},
            'zen': {'db': 'ok', 'pay': pay, 'available': pay, 'remaining': pay, 'ratio': 1.0 if cloud else 0.0},
            'or': {'db': 'ok', 'pay': pay, 'available': pay, 'remaining': pay},
            'gemini': {'db': 'ok', 'today_request': 0 if cloud else 450, 'remaining_request': 450 if cloud else 0, 'ratio': 1.0 if cloud else 0.0},
            'groq': {'db': 'ok', 'today_request': 0 if cloud else 900, 'ratio': 1.0 if cloud else 0.0},
            'claude': {'source': 'fresh', 'valid': True, 'pay': 0, 'percent1_usd': 30, 'pool_remaining_usd': 0, 'fable_today': 0, 'fable_attempts': []}}


class CoordinatorOutcomeTests(unittest.TestCase):
    """Coordinator: structural reason no-capacity/disk-low -> failed (no retry); any other reason -> retryable."""

    def setUp(self):
        self.temp = fixtures.tempfile.TemporaryDirectory(prefix='qe-p3-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        self.root = Path(self.temp.name) / 'code'
        (self.root / 'bin').mkdir(parents=True)
        (self.root / 'bin/cadre-dispatch.sh').write_text('exit 3\n')
        self.store = RunStore(self.home)

    def run_with_reason(self, reason):
        run = self.store.accept('a', 'capacity test')
        (self.root / 'bin/job-runner.sh').write_text(
            f'''printf '%s' '{{"reason": "{reason}"}}' | "{sys.executable}" "{LIB / 'run_store.py'}" update "$CADRE_RUN_ID"\nexit 1\n''')
        env = {**os.environ, 'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(self.root), 'CADRE_COORD_TICK': '0.1'}
        for key in ('CADRE_RUN_ID', 'CADRE_RUN_DIR', 'CADRE_OWNER_FDS', 'CADRE_COORDINATOR_PID'):
            env.pop(key, None)
        result = subprocess.run([sys.executable, str(LIB / 'run_store.py'), 'run', run['run_id']], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stderr)
        return self.store.read(run['run_id'])

    def test_no_capacity_and_disk_low_become_failed_but_other_reasons_stay_retryable(self):
        self.assertEqual(self.run_with_reason('no-capacity: local model did not return in 480 min and no cloud model fits the budget')['state'], 'failed')
        self.assertEqual(self.run_with_reason('disk-low: 100 MB < 2048 MB')['state'], 'failed')
        record = self.run_with_reason('qa-red: 2 round')
        self.assertEqual(record['state'], 'retryable')
        self.assertEqual(record['reason'], 'qa-red: 2 round')

    def test_budget_wait_reason_stays_retryable(self):
        # P2/P3 (03.10): a budget denial is 'retryable', not 'failed' — the same run continues when the budget returns (no retry is burned).
        pending = self.run_with_reason('budget-wait')
        self.assertEqual(pending['state'], 'retryable')
        self.assertIn('budget-wait', pending.get('reason') or '')


if __name__ == '__main__':
    unittest.main()
