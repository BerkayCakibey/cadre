"""P1 isolation: the engine and everything it starts run only from code under CADRE_ENGINE_ROOT.
Fixture and the sabotaged live root: test_gece_omur.IsolatedNight. Evidence: reports/p1-izolasyon-<date>.json (--pattern test_gece_izolasyon.py)."""

from __future__ import annotations

from pathlib import Path
import subprocess
import time
import unittest
from unittest.mock import patch

from test_gece_omur import IsolatedNight, LIB  # noqa: F401
import kosu


class IsolationTests(IsolatedNight):
    def test_every_engine_argv_stays_inside_the_isolated_root_and_live_sabotage_is_never_executed(self):
        """P1(c,e): the full `cadre gorev` path; the lib/bin path in every recorded bash/python3 argv is under the isolated root;
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
        expected = {'cadre-dispatch.sh', 'is-motoru.sh', 'kosu.py', 'qe_core.py', 'butce.py', 'kosu_defteri.py',
                    'saglik-nobet.sh', 'kota-izle.sh'}
        self.assertEqual(expected - seen, set(), f'seen={sorted(seen)}')

    def test_guard_launch_uses_the_code_root_and_skips_a_live_guard(self):
        """P1(b): bekci_dirilt starts from the code root, not the live home; it does not start if the pidfile shows a live process."""
        home, root = self.home, self.root
        launched = []
        with patch.object(kosu, 'bekci_baslat', side_effect=lambda h, r, script: launched.append(str(script))):
            started = kosu.bekci_dirilt(home, root)
        self.assertEqual(sorted(started), ['kota-izle', 'saglik-nobet'])
        for script in launched:
            self.assertTrue(script.startswith(str(root / 'bin')), script)
        # live guard: pidfile + saglik-nobet.sh in argv
        script = Path(self.temp.name) / 'saglik-nobet.sh'
        script.write_text('#!/bin/bash\nsleep 30\n')
        script.chmod(0o755)
        proc = subprocess.Popen(['bash', str(script)])
        self.addCleanup(lambda: (proc.kill(), proc.wait(timeout=5)))
        (home / 'zincir/saglik-nobet.pid').write_text(str(proc.pid))
        (home / 'zincir/saglik-nobet.tik').write_text(str(int(time.time())))
        self.assertTrue(kosu.bekci_canli(home, 'saglik-nobet'))
        with patch.object(kosu, 'bekci_baslat') as baslat:
            self.assertEqual(kosu.bekci_dirilt(home, root), ['kota-izle'])
            self.assertEqual(baslat.call_count, 1)




if __name__ == '__main__':
    unittest.main()
