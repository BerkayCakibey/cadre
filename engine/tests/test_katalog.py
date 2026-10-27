"""Item 0a-2 — context is a MODEL property. Keeping a single number per provider removed large requests
needlessly (Go said 128k while the models were 262k-1M). The catalogue is read; otherwise it falls back to the old table."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import katalog  # noqa: E402
import yonlendirici as yon  # noqa: E402

ORNEK = {
    'opencode-go': {'models': {
        'buyuk': {'limit': {'context': 1_000_000, 'output': 131072}, 'cost': {'input': 2, 'output': 6}},
        'orta': {'limit': {'context': 262144, 'output': 262144}, 'cost': {'input': 0.95, 'output': 4}},
        'bedava-model': {'limit': {'context': 128000}, 'cost': {'input': 0, 'output': 0}},
    }},
    'groq': {'models': {
        'openai/gpt-oss-120b': {'limit': {'context': 131072}, 'cost': {'input': 0.15, 'output': 0.6}},
        'minik': {'limit': {'context': 4000}, 'cost': {'input': 1, 'output': 1}},
    }},
}


class KatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-katalog-')
        self.addCleanup(self.temp.cleanup)
        self.dosya = Path(self.temp.name) / 'models.json'
        self.dosya.write_text(json.dumps(ORNEK), encoding='utf-8')
        self.eski = os.environ.get('CADRE_MODEL_KATALOG')
        os.environ['CADRE_MODEL_KATALOG'] = str(self.dosya)
        katalog._onbellek.clear()
        self.addCleanup(katalog._onbellek.clear)
        self.addCleanup(lambda: os.environ.__setitem__('CADRE_MODEL_KATALOG', self.eski)
                        if self.eski else os.environ.pop('CADRE_MODEL_KATALOG', None))

    def test_context_price_and_free_flag_come_from_the_catalog(self):
        self.assertEqual(katalog.baglam('opencode-go', 'buyuk'), 1_000_000)
        self.assertEqual(katalog.fiyat('opencode-go', 'orta'), {'input': 0.95, 'output': 4.0})
        self.assertFalse(katalog.bedava_mi('opencode-go', 'orta'))
        self.assertTrue(katalog.bedava_mi('opencode-go', 'bedava-model'))
        # UNKNOWN does not mean free
        self.assertIsNone(katalog.bedava_mi('opencode-go', 'hic-yok'))
        self.assertIsNone(katalog.baglam('opencode-go', 'hic-yok'))
        # a provider-prefixed name and a name with a sub-path
        self.assertEqual(katalog.baglam('groq', 'openai/gpt-oss-120b'), 131072)
        self.assertEqual(katalog.baglam('opencode-go', 'opencode-go/orta'), 262144)

    def test_a_missing_or_corrupt_catalog_never_raises_and_falls_back(self):
        self.dosya.write_text('{bozuk', encoding='utf-8')
        katalog._onbellek.clear()
        self.assertEqual(katalog.yukle(), {})
        self.assertIsNone(katalog.baglam('opencode-go', 'buyuk'))
        self.assertEqual(yon.istek_tavani('opencode-go', 'buyuk'), yon.SAGLAYICI['opencode-go']['istek_tavan'])
        self.dosya.unlink()
        katalog._onbellek.clear()
        self.assertEqual(katalog.ozet(), {})
        self.assertEqual(yon.istek_tavani('opencode', 'her-neyse'), yon.SAGLAYICI['opencode']['istek_tavan'])

    def test_cache_refreshes_when_the_file_changes(self):
        self.assertEqual(katalog.baglam('opencode-go', 'orta'), 262144)
        veri = json.loads(json.dumps(ORNEK))
        veri['opencode-go']['models']['orta']['limit']['context'] = 999
        self.dosya.write_text(json.dumps(veri), encoding='utf-8')
        os.utime(self.dosya, (0, 0))          # mtime changed -> must be read again
        self.assertEqual(katalog.baglam('opencode-go', 'orta'), 999)

    def test_ratio_providers_grow_with_the_model_and_rate_capped_ones_do_not(self):
        yedek = yon.SAGLAYICI['opencode-go']['istek_tavan']
        self.assertEqual(yon.istek_tavani('opencode-go', 'buyuk'), yon.TAVAN_UST)      # 1M * .78 > the policy cap
        self.assertEqual(yon.istek_tavani('opencode-go', 'orta'), int(262144 * 0.78))
        self.assertGreater(yon.istek_tavani('opencode-go', 'orta'), yedek)
        # B110 (18.09): there used to be `max(yedek, ...)` and on a SMALL-context model the cap exceeded the model's own
        # context; now the model context is always the upper bound (the fallback only without a catalogue).
        self.assertEqual(yon.istek_tavani('opencode-go', 'bedava-model'), int(128000 * 0.78))
        self.assertLess(yon.istek_tavani('opencode-go', 'bedava-model'), yedek)
        # rate-limited provider: a large context does not raise the cap, a small context LOWERS it
        self.assertEqual(yon.istek_tavani('groq', 'openai/gpt-oss-120b'), yon.SAGLAYICI['groq']['istek_tavan'])
        self.assertEqual(yon.istek_tavani('groq', 'minik'), 4000)
        self.assertEqual(yon.istek_tavani('opencode-go', ''), yedek)                   # the fallback if the model is unknown

    def test_admission_uses_the_model_cap_not_the_provider_cap(self):
        buyuk_istek = 150_000
        self.assertGreater(buyuk_istek, yon.SAGLAYICI['opencode-go']['istek_tavan'])
        ok, neden = yon.butce_var('opencode-go', 'orta', None, istek_token=buyuk_istek)
        self.assertNotEqual(neden, 'istek tavani', 'gercek baglam yetiyorsa istek elenmemeli')
        ok, neden = yon.butce_var('groq', 'minik', None, istek_token=10_000)
        self.assertFalse(ok)
        self.assertEqual(neden, 'istek tavani')

    def test_live_openrouter_record_keeps_the_catalog_output_limit(self):
        veri = json.loads(json.dumps(ORNEK))
        veri['openrouter'] = {'models': {'deepseek/model-a': {'limit': {'context': 163840, 'output': 384000},
                                                              'cost': {'input': 0.9, 'output': 1.9}}}}
        self.dosya.write_text(json.dumps(veri), encoding='utf-8')
        home = Path(self.temp.name) / 'home'
        (home / 'zincir').mkdir(parents=True)
        (home / 'zincir/openrouter-canli.json').write_text(json.dumps({'ts': 1, 'modeller': ['deepseek/model-a', 'yeni/model-b'], 'kayitlar': {
            'deepseek/model-a': {'cost': {'input': 0.2, 'output': 0.4}, 'limit': {'context': 1048576}, 'tool_call': True},
            'yeni/model-b': {'cost': {'input': 1, 'output': 2}, 'limit': {'context': 8000}, 'tool_call': False}}}))
        katalog._onbellek.clear()
        with unittest.mock.patch.dict(os.environ, {'CADRE_HOME': str(home)}):
            kayit = katalog.model('openrouter', 'deepseek/model-a')
            self.assertEqual(kayit['limit'], {'context': 1048576, 'output': 384000},
                             'canlı kayıt bağlamı günceller, bilinen çıktı sınırını silmez')
            self.assertEqual(kayit['cost'], {'input': 0.2, 'output': 0.4})
            self.assertTrue(kayit['tool_call'])
            self.assertEqual(katalog.model('openrouter', 'yeni/model-b')['limit'], {'context': 8000},
                             'katalogda olmayan modele çıktı sınırı uydurulmaz')


class FiyatNobetiTests(unittest.TestCase):
    """B88 (the user's question: "if a free model becomes paid, will we know?").
    The catalogue has NO end date — we cannot know beforehand. The bucket decision is read from the price at call time, so the switch
    is automatic but SILENT. The watch breaks that silence: a free->paid switch is written to the morning summary."""

    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory(prefix='qe-nobet-')
        self.addCleanup(self.temp.cleanup)
        self.ev = Path(self.temp.name)
        (self.ev / 'zincir').mkdir(parents=True)
        # B220 (29.09): the watch reads the prices of the watched models from the catalogue; the catalogue did not exist in an empty HOME, so no baseline was written.
        (self.ev / 'models.json').write_text(json.dumps({
            'opencode-go': {'models': {'glm-5.3-flash': {'cost': {'input': 0.1, 'output': 0.4}},
                                       'deepseek-v4-pro': {'cost': {'input': 0.66, 'output': 1.98}}}},   # the first link of the code chain
            'opencode': {'models': {'big-pickle': {'cost': {'input': 0, 'output': 0}}}}}))
        ortam = unittest.mock.patch.dict(os.environ, {'CADRE_MODEL_KATALOG': str(self.ev / 'models.json')})
        ortam.start(); self.addCleanup(ortam.stop)

    def test_the_first_scan_reports_nothing_and_records_the_baseline(self):
        import katalog
        self.assertEqual(katalog.fiyat_degisimi(self.ev), [], 'ilk taramada kiyaslanacak gecmis yok')
        nobet = self.ev / katalog.FIYAT_NOBET
        self.assertTrue(nobet.exists())
        self.assertGreater(len(json.loads(nobet.read_text())), 0, 'zincirdeki modeller kaydedilmeli')

    def test_a_model_that_stops_being_free_is_reported(self):
        import katalog
        katalog.fiyat_degisimi(self.ev)
        nobet = self.ev / katalog.FIYAT_NOBET
        kayit = json.loads(nobet.read_text())
        parali = next(k for k, v in kayit.items() if not v['bedava'])
        kayit[parali] = {'input': 0, 'output': 0, 'bedava': True}     # fake "it was free yesterday"
        nobet.write_text(json.dumps(kayit))
        degisim = katalog.fiyat_degisimi(self.ev)
        self.assertEqual([d['model'] for d in degisim], [parali])
        self.assertTrue(degisim[0]['eski_bedava'])
        self.assertFalse(degisim[0]['yeni_bedava'])
        self.assertEqual(katalog.fiyat_degisimi(self.ev), [], 'ayni degisim iki kez raporlanmaz')

    def test_only_models_we_actually_use_are_watched(self):
        import katalog
        izlenen = katalog.izlenen_modeller()
        self.assertGreater(len(izlenen), 0)
        self.assertNotIn('lmstudio', {s for s, _ in izlenen}, 'yerel modelin fiyati yok, izlenmez')


if __name__ == '__main__':
    unittest.main()


class ZenKatalogYenilemeTest(unittest.TestCase):
    def test_public_catalog_refresh_reads_no_credentials_and_sends_no_authorization(self):
        kok = Path(tempfile.mkdtemp(prefix='zen-yenile-'))
        self.addCleanup(__import__('shutil').rmtree, kok, True)
        istekler = []

        class Cevap:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({'data': [{'id': 'glm-5.3-flash-free'}, {'id': 'deepseek-v4.1-flash-free'}]}).encode()

        def ac(istek, timeout):
            istekler.append(istek)
            return Cevap()

        with (unittest.mock.patch.dict(os.environ, {'CADRE_HOME': str(kok / 'ev')}),
              unittest.mock.patch.object(Path, 'home', return_value=kok / 'kimlik-yok'),
              unittest.mock.patch('urllib.request.urlopen', ac)):
            self.assertEqual(katalog.zen_yenile(), 2)
        self.assertEqual(istekler[0].full_url, 'https://opencode.ai/zen/v1/models')
        self.assertIsNone(istekler[0].get_header('Authorization'))
        self.assertEqual(istekler[0].get_header('User-agent'), 'opencode/1.0')
        veri = json.loads((kok / 'ev/zincir/opencode-canli.json').read_text())
        self.assertEqual(veri['modeller'], ['deepseek-v4.1-flash-free', 'glm-5.3-flash-free'])
