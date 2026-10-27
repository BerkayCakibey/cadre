"""CR-28 (N-PUB01): read-only install check; a missing dependency is a real error, personal paths are visible, nothing is written."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import install_check


def tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob('*'))


class InstallCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='install-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home, self.config = self.tmp / 'home_dir', self.tmp / 'cfg'
        self.home.mkdir(); self.config.mkdir()
        self.engine = Path(__file__).resolve().parents[1]

    def test_missing_runtime_is_a_real_gap_and_nothing_is_written(self):
        load = self.tmp / 'bin'
        load.mkdir()
        for name in ('git', 'bash'):
            os.symlink(shutil.which(name), load / name)
        before = tree(self.tmp)
        env = {'PATH': str(load), 'CADRE_UI_PYTHON': str(self.tmp / 'absent/python'), 'HOME': str(self.tmp),
               'CADRE_REAL_OPENCODE': str(self.tmp / 'absent/opencode')}
        result = install_check.check(self.home, self.config, self.engine, environment=env)
        self.assertEqual(result['outcome'], 'MISSING')
        self.assertIn('node', result['missing'])
        self.assertIn('opencode', result['missing'])
        self.assertNotIn('panel (Textual)', result['missing'])   # optional
        status = {i['entry_name']: i['status'] for i in result['items']}
        self.assertEqual((status['git'], status['bash'], status['python']), ('PRESENT', 'PRESENT', 'PRESENT'))
        self.assertEqual(status['panel (Textual)'], 'MISSING')
        self.assertEqual(tree(self.tmp), before)   # read-only

    def test_personal_absolute_paths_in_config_are_reported(self):
        (self.config / 'opencode.jsonc').write_text('{"plugin": ["file:///Users/another-user/.local/share/cadre-engineering/lib/cadre-plugin.mjs"],'
                                                     ' "x": "/home/old/.local/share/cadre-engineering/non"}')
        result = install_check.check(self.home, self.config, self.engine, environment=dict(os.environ))
        item = next(i for i in result['items'] if i['entry_name'] == 'personal fixed path')
        self.assertEqual(item['status'], 'MISSING')
        self.assertIn('another-user', item['detail'])
        self.assertIn('/home/old', item['detail'])
        self.assertFalse(item['required'])

    def test_cli_json_and_exit_code(self):
        with patch.object(install_check, 'check', return_value={'version': 1, 'outcome': 'MISSING', 'missing': ['node'],
                                                                     'items': [{'entry_name': 'node', 'status': 'MISSING', 'detail': 'absent', 'required': True}], 'note': ''}):
            with patch.object(sys, 'stdout', io.StringIO()) as out:
                self.assertEqual(install_check.main(['--json']), 1)
            self.assertEqual(json.loads(out.getvalue())['missing'], ['node'])
            with patch.object(sys, 'stdout', io.StringIO()) as out:
                self.assertEqual(install_check.main([]), 1)
            self.assertIn('OUTCOME: MISSING', out.getvalue())

    def test_v2_or_unknown_opencode_cannot_pass_the_v1_plugin_prerequisite_check(self):
        """N-RT03/N-PUB01: an incompatible plugin API does not count as ready just because the binary exists; the check does not write."""
        binary = self.tmp / 'fixture-opencode'
        binary.touch()
        env = {'PATH': '', 'CADRE_REAL_OPENCODE': str(binary), 'CADRE_UI_PYTHON': str(self.tmp / 'no-ui')}
        before = tree(self.tmp)
        for version, expected in (('1.18.31', 'PRESENT'), ('1.19.3', 'PRESENT'), ('1.0.0', 'MISSING'), ('1.17.9', 'MISSING'), ('2.0.32', 'MISSING'),
                                  ('v2.0.32', 'MISSING'), ('nightly', 'MISSING'), (None, 'MISSING')):
            with self.subTest(version=version), patch.object(install_check, '_version', return_value=version):
                result = install_check.check(self.home, self.config, self.engine, environment=env)
                item = next(row for row in result['items'] if row['entry_name'] == 'opencode')
                self.assertEqual(item['status'], expected)
                self.assertEqual('opencode' in result['missing'], expected == 'MISSING')
                if version and expected == 'MISSING':
                    self.assertIn('V1', item['detail'])
        self.assertEqual(tree(self.tmp), before)


if __name__ == '__main__':
    unittest.main()
