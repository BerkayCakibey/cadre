"""4c-13 / G-B6: the health summary compares the Go API percentages with the Σ formula (opencode DB, billing period) at the same time."""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

from test_zen import fake_db

ENGINE = Path(__file__).resolve().parents[1]


class GoVerifierTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='qe-verifier-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.chain = self.home / 'chain'
        self.chain.mkdir()
        self.db = self.home / 'opencode.db'
        self.now = time.time()
        reset = (datetime.datetime.now() + datetime.timedelta(days=1)).replace(hour=12, minute=0)
        (self.chain / 'go-period-reset.txt').write_text(reset.strftime('%Y-%m-%d %H:%M'))
        bin_dir = self.home / 'bin'
        bin_dir.mkdir()
        python = bin_dir / 'python3'
        python.write_text('#!/bin/bash\n'
                          'if [ "${1:-}" = "$CADRE_ENGINE_ROOT/lib/provider_budget.py" ]; then\n'
                          '  cat "$CADRE_HOME/go-observed.json"\n'
                          f'else exec {shlex.quote(sys.executable)} "$@"; fi\n')
        python.chmod(0o755)
        curl = bin_dir / 'curl'
        curl.write_text('#!/bin/bash\nprintf \'%s\\n\' \'{"balance_infos":[{"total_balance":1}]}\'\n')
        curl.chmod(0o755)
        self.env = {**os.environ, 'HOME': str(self.home), 'CADRE_HOME': str(self.home),
                    'CADRE_ENGINE_ROOT': str(ENGINE), 'OPENCODE_DB': str(self.db),
                    'PATH': f"{bin_dir}:{os.environ['PATH']}"}

    def execute_run(self, cache=None, db_ok=True):
        observed = {'go': {'db': 'ok' if db_ok else 'unreachable', 'unit': 'points'}}
        (self.home / 'go-observed.json').write_text(json.dumps(observed))
        if cache is not None:
            (self.chain / 'go-window.json').write_text(json.dumps(cache))
        return subprocess.run(['bash', str(ENGINE / 'bin/health-summary.sh')], env=self.env,
                              capture_output=True, text=True, timeout=10)

    def test_no_warning_when_api_percentages_match_the_sigma_formula(self):
        # 4c-13: qwen3.8-max $15 limit -> $1.50 = 10 monthly points = rolling 50% = weekly 20%
        fake_db(self.db, [('opencode-go', self.now - 600, 1.5, 'qwen3.8-max')])
        result = self.execute_run({'rolling': 50, 'weekly': 20, 'monthly': 10, 'ts': self.now, 'resets': {}})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('WARNING', result.stdout)
        self.assertIn('go=%10 (Σ 10.0)', result.stdout)

    def test_sigma_contradicting_api_percentage_gives_a_deviation_warning(self):
        fake_db(self.db, [])
        result = self.execute_run({'rolling': 0, 'weekly': 0, 'monthly': 63, 'ts': self.now, 'resets': {}})
        self.assertIn('go-points-deviation', result.stdout)

    def test_sigma_starts_the_month_from_the_api_monthly_reset(self):
        reset = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3)
        start = (reset - datetime.timedelta(days=31)).timestamp()   # one month ago (month length 28-31: cautious margin)
        fake_db(self.db, [('opencode-go', start - 86400 * 3, 15, 'glm-5.3'),           # previous period: not counted
                           ('opencode-go', self.now - 86400 * 9, 0.75, 'glm-5.3')])   # 5 points, older than a week too
        cache = {'rolling': 0, 'weekly': 0, 'monthly': 5, 'ts': self.now,
                 'resets': {'monthly': reset.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
                            'weekly': (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=2)).strftime('%Y-%m-%dT%H:%M:%S.000Z')}}
        result = self.execute_run(cache)
        self.assertNotIn('WARNING', result.stdout)
        self.assertIn('go=%5 (Σ 5.0)', result.stdout)

    def test_unreadable_db_is_not_equal_to_zero_spend(self):
        result = self.execute_run({'rolling': 0, 'weekly': 0, 'monthly': 0, 'ts': self.now, 'resets': {}})
        self.assertIn('go-account-unverifiable', result.stdout)


if __name__ == '__main__':
    unittest.main()
