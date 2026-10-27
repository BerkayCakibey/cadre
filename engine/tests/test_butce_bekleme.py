"""Design 3.3 (G-E11 waiting for the budget to come back): at hard+ level the plan and the correction round wait when there is no strong (paid, budgeted) cloud
and ≤2 h remain until the daily shares renew; otherwise they continue with what is available (local/free). No model/network."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import yonlendirici  # noqa: E402


class GucluModelTest(unittest.TestCase):
    def test_bos_guclu_degil(self):
        self.assertFalse(yonlendirici.guclu_mu(''))

    def test_ucretli_bulut_ve_claude_guclu(self):
        with patch('katalog.bedava_mi', return_value=False):
            self.assertTrue(yonlendirici.guclu_mu('openrouter/deepseek/deepseek-v4-pro'))
            self.assertTrue(yonlendirici.guclu_mu('claude/opus#high'))

    def test_bedava_model_guclu_degil(self):
        with patch('katalog.bedava_mi', return_value=True):
            self.assertFalse(yonlendirici.guclu_mu('opencode/big-pickle'))


class BulutBeklemeTest(unittest.TestCase):
    def karar(self, zincir, puan, saat, dakika=0):
        simdi = datetime(2026, 9, 24, saat, dakika).timestamp()
        with patch.object(yonlendirici, 'zincir', return_value=(zincir, [])), \
                patch('katalog.bedava_mi', side_effect=lambda sag, model: 'pickle' in model):
            return yonlendirici.bulut_bekleme('plan', 'zor', puan, simdi=simdi, esik=yonlendirici.BEKLEME_ESIK_SN)

    def test_zor_is_guclu_bulut_yokken_yenilenmeye_yakinsa_bekler(self):
        self.assertEqual(self.karar([], 50, 22, 30), 90 * 60 + 60)

    def test_yalniz_bedava_model_kaldiysa_da_bekler(self):
        self.assertGreater(self.karar(['opencode/big-pickle'], 70, 23), 0)

    def test_yenilenme_uzaksa_beklemez(self):
        self.assertEqual(self.karar([], 50, 20), 0)

    def test_esik_ortamdan_sifirlanirsa_beklemez(self):
        """B268 (29.09): the dispatcher e2e fixtures waited on the real clock; a test run between 22:00-00:00 hung until
        midnight. `CADRE_BEKLEME_ESIK_SN=0` disables waiting (for the fixtures and the operator)."""
        simdi = datetime(2026, 9, 24, 22, 30).timestamp()
        with patch.object(yonlendirici, 'zincir', return_value=([], [])), \
                patch('katalog.bedava_mi', return_value=False), patch.dict(os.environ, {'CADRE_BEKLEME_ESIK_SN': '0'}):
            self.assertEqual(yonlendirici.bulut_bekleme('plan', 'zor', 50, simdi=simdi), 0)

    def test_orta_is_beklemez(self):
        self.assertEqual(self.karar([], 30, 23, 30), 0)

    def test_guclu_bulut_varsa_beklemez(self):
        self.assertEqual(self.karar(['openrouter/deepseek/deepseek-v4-pro'], 50, 23, 30), 0)


if __name__ == '__main__':
    unittest.main()
