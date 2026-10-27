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
import kurulum_kontrol


def agac(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob('*'))


class KurulumKontrolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='kurulum-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home, self.config = self.tmp / 'ev', self.tmp / 'cfg'
        self.home.mkdir(); self.config.mkdir()
        self.engine = Path(__file__).resolve().parents[1]

    def test_missing_runtime_is_a_real_gap_and_nothing_is_written(self):
        bindir = self.tmp / 'bin'
        bindir.mkdir()
        for name in ('git', 'bash'):
            os.symlink(shutil.which(name), bindir / name)
        before = agac(self.tmp)
        env = {'PATH': str(bindir), 'CADRE_UI_PYTHON': str(self.tmp / 'yok/python'), 'HOME': str(self.tmp),
               'CADRE_REAL_OPENCODE': str(self.tmp / 'yok/opencode')}
        result = kurulum_kontrol.kontrol(self.home, self.config, self.engine, environment=env)
        self.assertEqual(result['sonuc'], 'EKSIK')
        self.assertIn('node', result['eksik'])
        self.assertIn('opencode', result['eksik'])
        self.assertNotIn('panel (Textual)', result['eksik'])   # optional
        durum = {i['ad']: i['durum'] for i in result['maddeler']}
        self.assertEqual((durum['git'], durum['bash'], durum['python']), ('VAR', 'VAR', 'VAR'))
        self.assertEqual(durum['panel (Textual)'], 'EKSIK')
        self.assertEqual(agac(self.tmp), before)   # read-only

    def test_personal_absolute_paths_in_config_are_reported(self):
        (self.config / 'opencode.jsonc').write_text('{"plugin": ["file:///Users/baskasi/.local/share/cadre-engineering/lib/cadre-plugin.mjs"],'
                                                     ' "x": "/home/eski/.local/share/cadre-engineering/olmayan"}')
        result = kurulum_kontrol.kontrol(self.home, self.config, self.engine, environment=dict(os.environ))
        item = next(i for i in result['maddeler'] if i['ad'] == 'kisisel sabit yol')
        self.assertEqual(item['durum'], 'EKSIK')
        self.assertIn('baskasi', item['ayrinti'])
        self.assertIn('/home/eski', item['ayrinti'])
        self.assertFalse(item['gerekli'])

    def test_cli_json_and_exit_code(self):
        with patch.object(kurulum_kontrol, 'kontrol', return_value={'surum': 1, 'sonuc': 'EKSIK', 'eksik': ['node'],
                                                                     'maddeler': [{'ad': 'node', 'durum': 'EKSIK', 'ayrinti': 'yok', 'gerekli': True}], 'not': ''}):
            with patch.object(sys, 'stdout', io.StringIO()) as out:
                self.assertEqual(kurulum_kontrol.main(['--json']), 1)
            self.assertEqual(json.loads(out.getvalue())['eksik'], ['node'])
            with patch.object(sys, 'stdout', io.StringIO()) as out:
                self.assertEqual(kurulum_kontrol.main([]), 1)
            self.assertIn('SONUC: EKSIK', out.getvalue())

    def test_v2_or_unknown_opencode_cannot_pass_the_v1_plugin_prerequisite_check(self):
        """N-RT03/N-PUB01: an incompatible plugin API does not count as ready just because the binary exists; the check does not write."""
        binary = self.tmp / 'fixture-opencode'
        binary.touch()
        env = {'PATH': '', 'CADRE_REAL_OPENCODE': str(binary), 'CADRE_UI_PYTHON': str(self.tmp / 'no-ui')}
        before = agac(self.tmp)
        for version, expected in (('1.18.31', 'VAR'), ('1.19.3', 'VAR'), ('1.0.0', 'EKSIK'), ('1.17.9', 'EKSIK'), ('2.0.32', 'EKSIK'),
                                  ('v2.0.32', 'EKSIK'), ('nightly', 'EKSIK'), (None, 'EKSIK')):
            with self.subTest(version=version), patch.object(kurulum_kontrol, '_surum', return_value=version):
                result = kurulum_kontrol.kontrol(self.home, self.config, self.engine, environment=env)
                item = next(row for row in result['maddeler'] if row['ad'] == 'opencode')
                self.assertEqual(item['durum'], expected)
                self.assertEqual('opencode' in result['eksik'], expected == 'EKSIK')
                if version and expected == 'EKSIK':
                    self.assertIn('V1', item['ayrinti'])
        self.assertEqual(agac(self.tmp), before)


if __name__ == '__main__':
    unittest.main()
