"""B206 (24.09 R2-hard): if the QA red is only formatting (`ruff format --check` etc.) and the project gives a `format-command`, the formatter runs once
before the model repair and QA is retried; the local model had taken 13 min to fix a one-line format."""

from __future__ import annotations

from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import test_outcome
import qe_core as core


class QaFormatTest(unittest.TestCase):
    def setUp(self):
        self.f = test_outcome.CompletionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        # P3 (03.10): QA repair starts directly in the cloud; these tests measure the format, not the model selection
        candidate = patch.object(core, 'cloud_candidate', return_value='opencode-go/test')
        candidate.start()
        self.addCleanup(candidate.stop)

    def setup(self, qa, format=None):
        from qa_fixture import command
        Path(self.f.run['constraints']['qa-command']).write_text(command(qa))
        if format is not None:
            (self.f.project / 'format-command').write_text(format)

    def qa(self, repair):
        with patch.object(core, 'run_opencode', side_effect=repair):
            return core.qa_gate('a', str(self.f.wt), self.f.log, '/fake/cli')

    def test_format_red_is_fixed_mechanically_no_model_call(self):
        self.setup("grep -q formatted product.txt || { echo '1 file would be reformatted'; exit 1; }", 'echo formatted > product.txt')
        outcome = self.qa(AssertionError('Do not call model repair'))
        self.assertEqual(outcome['status'], 'PASS')
        self.assertEqual(outcome['format']['rc'], 0)
        self.assertIn('QA FORMAT', Path(self.f.log).read_text())

    def test_formatter_does_not_run_on_a_non_format_red(self):
        mark = self.f.home / 'format.ran'
        self.setup("echo 'FAILED tests/test_a.py'; exit 1", f'touch {mark}')
        outcome = self.qa(lambda *a, **k: subprocess.Popen(['true']))
        self.assertFalse(mark.exists())
        self.assertEqual((outcome['status'], len(outcome['attempts'])), ('FAIL', 2))

    def test_red_remaining_after_format_goes_to_model_repair(self):
        mark = self.f.home / 'repair.ok'
        self.setup(f"grep -q formatted product.txt || {{ echo 'Would reformat: product.txt'; exit 1; }}; "
                 f"test -f {mark} || {{ echo 'FAILED lint'; exit 1; }}", 'echo formatted > product.txt')
        outcome = self.qa(lambda *a, **k: (mark.touch(), subprocess.Popen(['true']))[1])
        self.assertEqual((outcome['status'], len(outcome['attempts'])), ('PASS', 3))

    def test_old_behavior_without_a_format_command(self):
        self.setup("echo '1 file would be reformatted'; exit 1")
        outcome = self.qa(lambda *a, **k: subprocess.Popen(['true']))
        self.assertEqual((outcome['status'], len(outcome['attempts'])), ('FAIL', 2))
        self.assertNotIn('format', outcome)

    def test_format_command_is_copied_to_the_record_at_acceptance(self):
        (self.f.project / 'format-command').write_text('ruff format .')
        run = self.f.store.accept('a', 'fresh job')
        self.assertEqual(Path(run['constraints']['format-command']).read_text(), 'ruff format .')


if __name__ == '__main__':
    unittest.main()
