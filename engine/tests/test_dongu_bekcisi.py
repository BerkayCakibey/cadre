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

KIRIK = 'FAILED tests/unit/test_cli.py::test_cli_version_uses_distribution_version - AssertionError'


class TestDongusuTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="dongu-")
        self.logf = Path(self.d) / "motor.log"
        self.logf.write_text('{"type":"text","part":{"text":"eski deneme: ' + KIRIK + '"}}\n' * 5)  # the previous attempt's slice does not count

    def test_esik_altinda_bos_esikte_test_kimligi(self):
        d = core.TestDongusu(str(self.logf), esik=3)
        with self.logf.open("a") as f:
            f.write(f'{{"type":"tool","output":"1 failed\\n{KIRIK}\\n"}}\n' * 2)
        self.assertEqual(d.tik(), "")
        with self.logf.open("a") as f:
            f.write(f'{{"type":"tool","output":"{KIRIK}"}}\n')
        self.assertEqual(d.tik(), "tests/unit/test_cli.py::test_cli_version_uses_distribution_version")

    def test_farkli_testler_birbirine_sayilmaz(self):
        d = core.TestDongusu(str(self.logf), esik=3)
        with self.logf.open("a") as f:
            f.write("FAILED tests/a.py::t1\nFAILED tests/a.py::t2\nFAILED tests/a.py::t1\nFAILED tests/a.py::t2\n")
        self.assertEqual(d.tik(), "")

    def test_bekci_tik_test_dongusunde_isciyi_keser(self):
        with self.logf.open("a") as f:
            f.write((KIRIK + "\n") * 3)
        proc = subprocess.Popen(["sleep", "30"])
        try:
            with patch.object(core, "cancel_path", return_value=Path(self.d) / "yok.iptal"):
                guard = core.StallGuard(str(self.logf), proc, 9999, dongu_esik=3)
                # lines written AFTER the guard was set up count: the 3 lines before the setup are the previous slice
                self.assertIsNone(guard.tik("p"))
                with self.logf.open("a") as f:
                    f.write((KIRIK + "\n") * 3)
                self.assertEqual(guard.tik("p"), "dongu")
            self.assertIn("test_cli_version_uses_distribution_version", guard.dongu_testi)
            self.assertIsNotNone(proc.wait(timeout=5), "isci kesilmedi")
            self.assertIn("TEST-DONGUSU", self.logf.read_text())
            self.assertFalse(guard.kesti, "dongu kesmesi stall sayilmaz")
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)

    def test_esik_sifir_dedektoru_kapatir(self):
        proc = subprocess.Popen(["sleep", "30"])
        try:
            with patch.object(core, "cancel_path", return_value=Path(self.d) / "yok.iptal"):
                guard = core.StallGuard(str(self.logf), proc, 9999, dongu_esik=0)
                with self.logf.open("a") as f:
                    f.write((KIRIK + "\n") * 50)
                self.assertIsNone(guard.tik("p"))
        finally:
            proc.kill()
            proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
