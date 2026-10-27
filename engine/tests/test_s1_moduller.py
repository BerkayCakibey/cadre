"""S1: direct module loading and the CLI entry points reach the same core."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

LIB = Path(__file__).resolve().parents[1] / 'lib'


class ModulSiniriTests(unittest.TestCase):
    def test_each_module_can_be_imported_first_without_a_cycle(self):
        for module in ('canlilik', 'kabul', 'motor', 'qe_core'):
            with self.subTest(module=module):
                result = subprocess.run([sys.executable, '-c',
                    f'import {module}; import qe_core; assert not qe_core.qa_atlanmis_mi("Ran 1 test\\nOK")'],
                    env={**os.environ, 'PYTHONPATH': str(LIB)}, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_launcher_enters_dispatcher_for_an_unplanned_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'bin').mkdir()
            (root / 'bin/cadre-dispatch.sh').write_text('printf "dispatch:%s:%s" "$1" "$2"\n')
            env = {k: v for k, v in os.environ.items() if not k.startswith(('CADRE_'))}
            env['CADRE_ENGINE_ROOT'] = str(root)
            for command in (['bash', str(LIB.parent / 'bin/is-motoru.sh')],
                            [sys.executable, str(LIB / 'qe_core.py'), 'motor']):
                result = subprocess.run(command + ['example', 'prompt path'], env=env,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, 'dispatch:example:prompt path')


if __name__ == '__main__':
    unittest.main()
