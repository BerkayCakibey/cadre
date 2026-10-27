"""B206 (24.09 R2-hard): if the QA red is only formatting (`ruff format --check` etc.) and the project gives a `bicim-komut`, the formatter runs once
before the model repair and QA is retried; the local model had taken 13 min to fix a one-line format."""

from __future__ import annotations

from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import test_sonuc
import qe_core as core


class QaBicimTest(unittest.TestCase):
    def setUp(self):
        self.f = test_sonuc.CompletionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        # P3 (03.10): QA repair starts directly in the cloud; these tests measure the format, not the model selection
        aday = patch.object(core, 'bulut_aday', return_value='opencode-go/test')
        aday.start()
        self.addCleanup(aday.stop)

    def kur(self, qa, bicim=None):
        from qa_fixture import command
        Path(self.f.run['constraints']['qa-komut']).write_text(command(qa))
        if bicim is not None:
            (self.f.project / 'bicim-komut').write_text(bicim)

    def qa(self, onarim):
        with patch.object(core, 'run_opencode', side_effect=onarim):
            return core.qa_kapisi('a', str(self.f.wt), self.f.log, '/fake/cli')

    def test_bicim_kirmizisi_mekanik_duzelir_model_cagrilmaz(self):
        self.kur("grep -q formatted product.txt || { echo '1 file would be reformatted'; exit 1; }", 'echo formatted > product.txt')
        sonuc = self.qa(AssertionError('model onarimi cagrilmamali'))
        self.assertEqual(sonuc['status'], 'PASS')
        self.assertEqual(sonuc['bicim']['rc'], 0)
        self.assertIn('QA BICIM', Path(self.f.log).read_text())

    def test_bicim_disi_kirmizida_bicimleyici_kosmaz(self):
        isaret = self.f.home / 'bicim.kostu'
        self.kur("echo 'FAILED tests/test_a.py'; exit 1", f'touch {isaret}')
        sonuc = self.qa(lambda *a, **k: subprocess.Popen(['true']))
        self.assertFalse(isaret.exists())
        self.assertEqual((sonuc['status'], len(sonuc['attempts'])), ('FAIL', 2))

    def test_bicimden_sonra_kalan_kirmizi_model_onarimina_gider(self):
        isaret = self.f.home / 'onarim.ok'
        self.kur(f"grep -q formatted product.txt || {{ echo 'Would reformat: product.txt'; exit 1; }}; "
                 f"test -f {isaret} || {{ echo 'FAILED lint'; exit 1; }}", 'echo formatted > product.txt')
        sonuc = self.qa(lambda *a, **k: (isaret.touch(), subprocess.Popen(['true']))[1])
        self.assertEqual((sonuc['status'], len(sonuc['attempts'])), ('PASS', 3))

    def test_bicim_komutu_yoksa_eski_davranis(self):
        self.kur("echo '1 file would be reformatted'; exit 1")
        sonuc = self.qa(lambda *a, **k: subprocess.Popen(['true']))
        self.assertEqual((sonuc['status'], len(sonuc['attempts'])), ('FAIL', 2))
        self.assertNotIn('bicim', sonuc)

    def test_bicim_komutu_kabulde_kayda_kopyalanir(self):
        (self.f.project / 'bicim-komut').write_text('ruff format .')
        kosu = self.f.store.accept('a', 'yeni is')
        self.assertEqual(Path(kosu['constraints']['bicim-komut']).read_text(), 'ruff format .')


if __name__ == '__main__':
    unittest.main()
