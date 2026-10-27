"""P1 isolation: the engine and everything it starts run only from code under CADRE_ENGINE_ROOT.
Fixture and the sabotaged live root: test_night_lifetime.IsolatedNight. Evidence: reports/p1-isolation-<date>.json (--pattern test_night_isolation.py)."""

from __future__ import annotations

from pathlib import Path
import subprocess
import time
import unittest
from unittest.mock import patch

from test_night_lifetime import IsolatedNight, LIB  # noqa: F401
import run_store


class IsolationTests(IsolatedNight):
    def test_every_engine_argv_stays_inside_the_isolated_root_and_live_sabotage_is_never_executed(self):
        """P1(c,e): the full `cadre task` path; the lib/bin path in every recorded bash/python3 argv is under the isolated root;
        the sabotage files in the live root never run (no LIVE-LEAK); the run rc does not change (completed)."""
        rid = self.submit()
        record = self.wait_terminal(rid)
        self.assertEqual(record['state'], 'completed', (self.store.path(rid) / 'coordinator.log').read_text()[-2000:])
        self.assertFalse((self.home / 'LIVE-LEAK').exists(), (self.home / 'LIVE-LEAK').read_text() if (self.home / 'LIVE-LEAK').exists() else '')
        live = str(self.fake_home / '.local/share/cadre-engineering')
        seen = set()
        for entry in self.argv():
            for arg in entry['args']:
                for piece in arg.replace('"', ' ').replace("'", ' ').split():
                    if '/lib/' in piece or '/bin/' in piece:
                        if piece.startswith(('/bin/', '/usr/', '/opt/', '/private/', '/System/')) and not piece.startswith(str(Path(self.temp.name))):
                            continue
                        self.assertNotIn(live, piece)
                        if piece.startswith(str(self.root)):
                            seen.add(Path(piece.split(':')[0]).name)
                        else:
                            self.fail(f'engine path outside isolated root: {piece}')
        expected = {'cadre-dispatch.sh', 'job-runner.sh', 'run_store.py', 'qe_core.py', 'provider_budget.py', 'run_ledger.py',
                    'health-watch.sh', 'capacity-watch.sh'}
        self.assertEqual(expected - seen, set(), f'seen={sorted(seen)}')

    def test_guard_launch_uses_the_code_root_and_skips_a_live_guard(self):
        """P1(b): guard_revive starts from the code root, not the live home; it does not start if the pidfile shows a live process."""
        home, root = self.home, self.root
        launched = []
        with patch.object(run_store, 'guard_start', side_effect=lambda h, r, script: launched.append(str(script))):
            started = run_store.guard_revive(home, root)
        self.assertEqual(sorted(started), ['capacity-watch', 'health-watch'])
        for script in launched:
            self.assertTrue(script.startswith(str(root / 'bin')), script)
        # live guard: pidfile + health-watch.sh in argv
        script = Path(self.temp.name) / 'health-watch.sh'
        script.write_text('#!/bin/bash\nsleep 30\n')
        script.chmod(0o755)
        proc = subprocess.Popen(['bash', str(script)])
        self.addCleanup(lambda: (proc.kill(), proc.wait(timeout=5)))
        (home / 'chain/health-watch.pid').write_text(str(proc.pid))
        (home / 'chain/health-watch.tick').write_text(str(int(time.time())))
        self.assertTrue(run_store.guard_live(home, 'health-watch'))
        with patch.object(run_store, 'guard_start') as start:
            self.assertEqual(run_store.guard_revive(home, root), ['capacity-watch'])
            self.assertEqual(start.call_count, 1)




if __name__ == '__main__':
    unittest.main()
