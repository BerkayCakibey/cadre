"""G-C5 packaging tolerance (23.09 live T1v4, B174): three cloud judges fell in a row with "malformed review", the local judge timed out
after 15 min, acceptance FAIL `review-not-passed`. The raw outputs (fixtures/hakem-20260923/) show that big-pickle and Cerebras
had produced VALID JSON with the right product identity and a GECER verdict — but inside prose: braces in the text like `{package_version()}`
broke the "outermost {…}" tolerance (B39) ("Expecting property name", "Extra data"). Contract: JSON candidates
are searched with `raw_decode` from every `{` position and in ```json fences; the meaning check (identity, verdict, scope, placeholder) stays the SAME.
glm's invented criterion ids are still rejected. No model/network."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import sonuc  # noqa: E402

FIX = Path(__file__).resolve().parent / 'fixtures' / 'hakem-20260923'
DIGEST = '1ac22fd46a8262c44dff3425958192510553527ba8b45125b643f28026540951'
EXPECTED = {'digest': DIGEST}


def oku(ad: str) -> str:
    return (FIX / ad).read_text(encoding='utf-8')


class HakemAmbalajTest(unittest.TestCase):
    def test_big_pickle_duz_yazi_icindeki_citli_json_kabul_edilir(self):
        r = sonuc.parse_review(oku('hakem-opencode_big-pickle.txt'), EXPECTED, ['main'])
        self.assertEqual((r['status'], r['verdict']), ('PASS', 'GECER'), r.get('reason'))
        self.assertEqual([k['id'] for k in r['acceptance']], ['main'])

    def test_cerebras_yazi_arasindaki_citsiz_json_kabul_edilir(self):
        r = sonuc.parse_review(oku('hakem-cerebras_qwen-3.8-27b.txt'), EXPECTED, ['main'])
        self.assertEqual((r['status'], r['verdict']), ('PASS', 'GECER'), r.get('reason'))

    def test_glm_uydurma_kriter_kimlikleri_yine_reddedilir(self):
        r = sonuc.parse_review(oku('hakem-opencode-go_glm-5.3-flash.txt'), EXPECTED, ['main'])
        self.assertEqual(r['status'], 'ERROR')
        self.assertIn('acceptance', r['reason'])  # coverage incomplete / unknown criterion — the meaning check did not loosen

    def test_yanlis_kimlikli_ilk_json_atlanir_dogru_olan_alinir(self):
        metin = ('Once taslak: {"revision": "deadbeef", "verdict": "DUZELT", "acceptance": []}\n'
                 'Sonra gercek karar:\n```json\n{"revision": "' + DIGEST + '", "verdict": "GECER", '
                 '"acceptance": [{"id": "main", "status": "PASS", "evidence": "tests/unit/test_cli_version.py:1-36 iki test yesil"}]}\n```\n')
        r = sonuc.parse_review(metin, EXPECTED, ['main'])
        self.assertEqual((r['status'], r['verdict']), ('PASS', 'GECER'), r.get('reason'))

    def test_json_hic_yoksa_satir_sozlesmesi_hala_calisir(self):
        metin = f'REVIZYON: {DIGEST}\nKARAR: GECER\nKRITER main: PASS - tests/unit/test_cli_version.py:1-36 iki test yesil\n'
        r = sonuc.parse_review(metin, EXPECTED, ['main'])
        self.assertEqual(r['status'], 'PASS')




class YazarCiktisiTest(unittest.TestCase):
    def test_tek_rapor_nesnesi_ara_metin_ve_citten_okunur_cok_anlamli_ya_da_eksik_cevap_reddedilir(self):
        import json
        cevap = {'report': '# Bulgular\nx', 'evidence': [], 'assumptions': [], 'decisions': []}
        metin = 'Kaynak okundu {not}.\n```json\n' + json.dumps(cevap) + '\n```\n' + json.dumps(cevap)
        self.assertEqual(sonuc.tek_json_nesnesi(metin, 'report'), cevap)
        for kotu in (json.dumps(cevap) + '\n' + json.dumps({**cevap, 'report': 'baska'}), 'rapor yok {"x": 1}', ''):
            with self.subTest(kotu=kotu[:30]), self.assertRaisesRegex(ValueError, 'review-output-invalid'):
                sonuc.tek_json_nesnesi(kotu, 'report')


if __name__ == '__main__':
    unittest.main()
