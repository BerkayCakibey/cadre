"""P3 (03.10): the local model is OFF by default. The local wait/escalation loops were removed; without cloud capacity
the engine leaves the job partial with `butce-bekleme` (the coordinator marks it retryable, it continues when the budget returns). The coordinator writes structural
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

import test_kosu_sessions as fixtures
import qe_core as core
from kosu import RunStore

LIB = fixtures.LIB


def full_budget(cloud=True):
    day = time.strftime('%Y-%m-%d')
    pay = 5.0 if cloud else 0.0
    return {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'bugun': day, 'kalan_gun': 10,
            'go': {'db': 'ok', 'pay': (pay) * 100 / 15, 'kullanilabilir': (pay) * 100 / 15, 'pencere_acik': True, 'oran': 1.0 if cloud else 0.0, 'birim': 'puan'},
            'zen': {'db': 'ok', 'pay': pay, 'kullanilabilir': pay, 'kalan': pay, 'oran': 1.0 if cloud else 0.0},
            'or': {'db': 'ok', 'pay': pay, 'kullanilabilir': pay, 'kalan': pay},
            'gemini': {'db': 'ok', 'bugun_istek': 0 if cloud else 450, 'kalan_istek': 450 if cloud else 0, 'oran': 1.0 if cloud else 0.0},
            'groq': {'db': 'ok', 'bugun_istek': 0 if cloud else 900, 'oran': 1.0 if cloud else 0.0},
            'claude': {'kaynak': 'taze', 'gecerli': True, 'pay': 0, 'yuzde1_usd': 30, 'havuz_kalan_usd': 0, 'fable_bugun': 0, 'fable_attempts': []}}


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
        run = self.store.accept('a', 'kapasite testi')
        (self.root / 'bin/is-motoru.sh').write_text(
            f'''printf '%s' '{{"reason": "{reason}"}}' | "{sys.executable}" "{LIB / 'kosu.py'}" update "$CADRE_RUN_ID"\nexit 1\n''')
        env = {**os.environ, 'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(self.root), 'CADRE_KOORD_TIK': '0.1'}
        for key in ('CADRE_RUN_ID', 'CADRE_RUN_DIR', 'CADRE_OWNER_FDS', 'CADRE_KOORDINATOR_PID'):
            env.pop(key, None)
        result = subprocess.run([sys.executable, str(LIB / 'kosu.py'), 'run', run['run_id']], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stderr)
        return self.store.read(run['run_id'])

    def test_no_capacity_and_disk_low_become_failed_but_other_reasons_stay_retryable(self):
        self.assertEqual(self.run_with_reason('no-capacity: yerel model 480 dk donmedi, butceli bulut yok')['state'], 'failed')
        self.assertEqual(self.run_with_reason('disk-low: 100 MB < 2048 MB')['state'], 'failed')
        record = self.run_with_reason('qa-red: 2 tur')
        self.assertEqual(record['state'], 'retryable')
        self.assertEqual(record['reason'], 'qa-red: 2 tur')

    def test_butce_bekleme_reason_stays_retryable(self):
        # P2/P3 (03.10): a budget denial is 'retryable', not 'failed' — the same run continues when the budget returns (no retry is burned).
        bekleyen = self.run_with_reason('butce-bekleme')
        self.assertEqual(bekleyen['state'], 'retryable')
        self.assertIn('butce-bekleme', bekleyen.get('reason') or '')


if __name__ == '__main__':
    unittest.main()
