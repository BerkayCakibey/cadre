"""G-F9 (23.09 live T1/T1c finding): the executor broke the same test 10+ times with the same error and kept editing the same file;
since it produced artefacts "guard cut" (artefact silence) did not trigger, ERROR-LOOP counts provider errors,
STALL needs silence → a 41 min unbounded loop. Contract: if the SAME test id shows up as `FAILED` threshold times in one attempt's log
slice, the attempt is cut and counted as stuck (the existing rescuer/release path sets the limit). No model/network."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import qe_core as core  # noqa: E402

BROKEN = 'FAILED tests/unit/test_cli.py::test_cli_version_uses_distribution_version - AssertionError'


class TestLoopTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="loop-")
        self.logf = Path(self.d) / "runner.log"
        self.logf.write_text('{"type":"text","part":{"text":"old attempt: ' + BROKEN + '"}}\n' * 5)  # the previous attempt's slice does not count

    def test_empty_below_the_threshold_test_id_at_the_threshold(self):
        d = core.TestLoop(str(self.logf), threshold=3)
        with self.logf.open("a") as f:
            f.write(f'{{"type":"tool","output":"1 failed\\n{BROKEN}\\n"}}\n' * 2)
        self.assertEqual(d.tick(), "")
        with self.logf.open("a") as f:
            f.write(f'{{"type":"tool","output":"{BROKEN}"}}\n')
        self.assertEqual(d.tick(), "tests/unit/test_cli.py::test_cli_version_uses_distribution_version")

    def test_different_tests_do_not_count_toward_each_other(self):
        d = core.TestLoop(str(self.logf), threshold=3)
        with self.logf.open("a") as f:
            f.write("FAILED tests/a.py::t1\nFAILED tests/a.py::t2\nFAILED tests/a.py::t1\nFAILED tests/a.py::t2\n")
        self.assertEqual(d.tick(), "")

    def test_guard_tick_cuts_the_worker_in_a_test_loop(self):
        with self.logf.open("a") as f:
            f.write((BROKEN + "\n") * 3)
        proc = subprocess.Popen(["sleep", "30"])
        try:
            with patch.object(core, "cancel_path", return_value=Path(self.d) / "absent.cancel"):
                guard = core.StallGuard(str(self.logf), proc, 9999, loop_threshold=3)
                # lines written AFTER the guard was set up count: the 3 lines before the setup are the previous slice
                self.assertIsNone(guard.tick("p"))
                with self.logf.open("a") as f:
                    f.write((BROKEN + "\n") * 3)
                self.assertEqual(guard.tick("p"), "loop")
            self.assertIn("test_cli_version_uses_distribution_version", guard.loop_test)
            self.assertIsNotNone(proc.wait(timeout=5), "worker was not stopped")
            self.assertIn("TEST-LOOP", self.logf.read_text())
            self.assertFalse(guard.cut, "A loop stop is not a stall")
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)

    def test_threshold_zero_turns_the_detector_off(self):
        proc = subprocess.Popen(["sleep", "30"])
        try:
            with patch.object(core, "cancel_path", return_value=Path(self.d) / "absent.cancel"):
                guard = core.StallGuard(str(self.logf), proc, 9999, loop_threshold=0)
                with self.logf.open("a") as f:
                    f.write((BROKEN + "\n") * 50)
                self.assertIsNone(guard.tick("p"))
        finally:
            proc.kill()
            proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
