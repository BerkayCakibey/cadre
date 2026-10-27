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
import model_catalog  # noqa: E402
import model_router as router  # noqa: E402

SAMPLE = {
    'opencode-go': {'models': {
        'big': {'limit': {'context': 1_000_000, 'output': 131072}, 'cost': {'input': 2, 'output': 6}},
        'middle': {'limit': {'context': 262144, 'output': 262144}, 'cost': {'input': 0.95, 'output': 4}},
        'free-model': {'limit': {'context': 128000}, 'cost': {'input': 0, 'output': 0}},
    }},
    'groq': {'models': {
        'openai/gpt-oss-120b': {'limit': {'context': 131072}, 'cost': {'input': 0.15, 'output': 0.6}},
        'tiny': {'limit': {'context': 4000}, 'cost': {'input': 1, 'output': 1}},
    }},
}


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-catalog-')
        self.addCleanup(self.temp.cleanup)
        self.file = Path(self.temp.name) / 'models.json'
        self.file.write_text(json.dumps(SAMPLE), encoding='utf-8')
        self.old = os.environ.get('CADRE_MODEL_CATALOG')
        os.environ['CADRE_MODEL_CATALOG'] = str(self.file)
        model_catalog._cache.clear()
        self.addCleanup(model_catalog._cache.clear)
        self.addCleanup(lambda: os.environ.__setitem__('CADRE_MODEL_CATALOG', self.old)
                        if self.old else os.environ.pop('CADRE_MODEL_CATALOG', None))

    def test_context_price_and_free_flag_come_from_the_catalog(self):
        self.assertEqual(model_catalog.context('opencode-go', 'big'), 1_000_000)
        self.assertEqual(model_catalog.price('opencode-go', 'middle'), {'input': 0.95, 'output': 4.0})
        self.assertFalse(model_catalog.is_free('opencode-go', 'middle'))
        self.assertTrue(model_catalog.is_free('opencode-go', 'free-model'))
        # UNKNOWN does not mean free
        self.assertIsNone(model_catalog.is_free('opencode-go', 'missing-model'))
        self.assertIsNone(model_catalog.context('opencode-go', 'missing-model'))
        # a provider-prefixed name and a name with a sub-path
        self.assertEqual(model_catalog.context('groq', 'openai/gpt-oss-120b'), 131072)
        self.assertEqual(model_catalog.context('opencode-go', 'opencode-go/middle'), 262144)

    def test_a_missing_or_corrupt_catalog_never_raises_and_falls_back(self):
        self.file.write_text('{broken', encoding='utf-8')
        model_catalog._cache.clear()
        self.assertEqual(model_catalog.load(), {})
        self.assertIsNone(model_catalog.context('opencode-go', 'big'))
        self.assertEqual(router.request_cap('opencode-go', 'big'), router.PROVIDER['opencode-go']['request_cap'])
        self.file.unlink()
        model_catalog._cache.clear()
        self.assertEqual(model_catalog.summary(), {})
        self.assertEqual(router.request_cap('opencode', 'any-model'), router.PROVIDER['opencode']['request_cap'])

    def test_cache_refreshes_when_the_file_changes(self):
        self.assertEqual(model_catalog.context('opencode-go', 'middle'), 262144)
        data = json.loads(json.dumps(SAMPLE))
        data['opencode-go']['models']['middle']['limit']['context'] = 999
        self.file.write_text(json.dumps(data), encoding='utf-8')
        os.utime(self.file, (0, 0))          # mtime changed -> must be read again
        self.assertEqual(model_catalog.context('opencode-go', 'middle'), 999)

    def test_ratio_providers_grow_with_the_model_and_rate_capped_ones_do_not(self):
        reserve = router.PROVIDER['opencode-go']['request_cap']
        self.assertEqual(router.request_cap('opencode-go', 'big'), router.CAP_PARENT)      # 1M * .78 > the policy cap
        self.assertEqual(router.request_cap('opencode-go', 'middle'), int(262144 * 0.78))
        self.assertGreater(router.request_cap('opencode-go', 'middle'), reserve)
        # B110 (18.09): there used to be `max(reserve, ...)` and on a SMALL-context model the cap exceeded the model's own
        # context; now the model context is always the upper bound (the fallback only without a catalogue).
        self.assertEqual(router.request_cap('opencode-go', 'free-model'), int(128000 * 0.78))
        self.assertLess(router.request_cap('opencode-go', 'free-model'), reserve)
        # rate-limited provider: a large context does not raise the cap, a small context LOWERS it
        self.assertEqual(router.request_cap('groq', 'openai/gpt-oss-120b'), router.PROVIDER['groq']['request_cap'])
        self.assertEqual(router.request_cap('groq', 'tiny'), 4000)
        self.assertEqual(router.request_cap('opencode-go', ''), reserve)                   # the fallback if the model is unknown

    def test_admission_uses_the_model_cap_not_the_provider_cap(self):
        big_request = 150_000
        self.assertGreater(big_request, router.PROVIDER['opencode-go']['request_cap'])
        ok, reason = router.has_budget('opencode-go', 'middle', None, request_token=big_request)
        self.assertNotEqual(reason, 'request cap', 'Do not eliminate a request that fits the actual context')
        ok, reason = router.has_budget('groq', 'tiny', None, request_token=10_000)
        self.assertFalse(ok)
        self.assertEqual(reason, 'request cap')

    def test_live_openrouter_record_keeps_the_catalog_output_limit(self):
        data = json.loads(json.dumps(SAMPLE))
        data['openrouter'] = {'models': {'deepseek/model-a': {'limit': {'context': 163840, 'output': 384000},
                                                              'cost': {'input': 0.9, 'output': 1.9}}}}
        self.file.write_text(json.dumps(data), encoding='utf-8')
        home = Path(self.temp.name) / 'home'
        (home / 'chain').mkdir(parents=True)
        (home / 'chain/openrouter-live.json').write_text(json.dumps({'ts': 1, 'models': ['deepseek/model-a', 'fresh/model-b'], 'records': {
            'deepseek/model-a': {'cost': {'input': 0.2, 'output': 0.4}, 'limit': {'context': 1048576}, 'tool_call': True},
            'fresh/model-b': {'cost': {'input': 1, 'output': 2}, 'limit': {'context': 8000}, 'tool_call': False}}}))
        model_catalog._cache.clear()
        with unittest.mock.patch.dict(os.environ, {'CADRE_HOME': str(home)}):
            record = model_catalog.model('openrouter', 'deepseek/model-a')
            self.assertEqual(record['limit'], {'context': 1048576, 'output': 384000},
                             'live records update context without deleting the known output limit')
            self.assertEqual(record['cost'], {'input': 0.2, 'output': 0.4})
            self.assertTrue(record['tool_call'])
            self.assertEqual(model_catalog.model('openrouter', 'fresh/model-b')['limit'], {'context': 8000},
                             'an output limit is not invented for a model absent from the catalog')


class PriceWatchTests(unittest.TestCase):
    """B88 (the user's question: "if a free model becomes paid, will we know?").
    The catalogue has NO end date — we cannot know beforehand. The bucket decision is read from the price at call time, so the switch
    is automatic but SILENT. The watch breaks that silence: a free->paid switch is written to the morning summary."""

    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory(prefix='qe-watch-')
        self.addCleanup(self.temp.cleanup)
        self.home_dir = Path(self.temp.name)
        (self.home_dir / 'chain').mkdir(parents=True)
        # B220 (29.09): the watch reads the prices of the watched models from the catalogue; the catalogue did not exist in an empty HOME, so no baseline was written.
        (self.home_dir / 'models.json').write_text(json.dumps({
            'opencode-go': {'models': {'glm-5.3-flash': {'cost': {'input': 0.1, 'output': 0.4}},
                                       'deepseek-v4-pro': {'cost': {'input': 0.66, 'output': 1.98}}}},   # the first link of the code chain
            'opencode': {'models': {'big-pickle': {'cost': {'input': 0, 'output': 0}}}}}))
        environment = unittest.mock.patch.dict(os.environ, {'CADRE_MODEL_CATALOG': str(self.home_dir / 'models.json')})
        environment.start(); self.addCleanup(environment.stop)

    def test_the_first_scan_reports_nothing_and_records_the_baseline(self):
        import model_catalog
        self.assertEqual(model_catalog.price_change(self.home_dir), [], 'The first scan has no history for comparison')
        watch = self.home_dir / model_catalog.PRICE_WATCH
        self.assertTrue(watch.exists())
        self.assertGreater(len(json.loads(watch.read_text())), 0, 'Record models in the chain')

    def test_a_model_that_stops_being_free_is_reported(self):
        import model_catalog
        model_catalog.price_change(self.home_dir)
        watch = self.home_dir / model_catalog.PRICE_WATCH
        record = json.loads(watch.read_text())
        paid = next(k for k, v in record.items() if not v['free'])
        record[paid] = {'input': 0, 'output': 0, 'free': True}     # fake "it was free yesterday"
        watch.write_text(json.dumps(record))
        change = model_catalog.price_change(self.home_dir)
        self.assertEqual([d['model'] for d in change], [paid])
        self.assertTrue(change[0]['old_free'])
        self.assertFalse(change[0]['fresh_free'])
        self.assertEqual(model_catalog.price_change(self.home_dir), [], 'Do not report the same change twice')

    def test_only_models_we_actually_use_are_watched(self):
        import model_catalog
        followed = model_catalog.followed_models()
        self.assertGreater(len(followed), 0)
        self.assertNotIn('lmstudio', {s for s, _ in followed}, 'A local model has no price and is not tracked')


if __name__ == '__main__':
    unittest.main()


class ZenCatalogRefreshTest(unittest.TestCase):
    def test_public_catalog_refresh_reads_no_credentials_and_sends_no_authorization(self):
        root = Path(tempfile.mkdtemp(prefix='zen-reload-'))
        self.addCleanup(__import__('shutil').rmtree, root, True)
        requests = []

        class Answer:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({'data': [{'id': 'glm-5.3-flash-free'}, {'id': 'deepseek-v4.1-flash-free'}]}).encode()

        def open(request, timeout):
            requests.append(request)
            return Answer()

        with (unittest.mock.patch.dict(os.environ, {'CADRE_HOME': str(root / 'home_dir')}),
              unittest.mock.patch.object(Path, 'home', return_value=root / 'no-id'),
              unittest.mock.patch('urllib.request.urlopen', open)):
            self.assertEqual(model_catalog.zen_reload(), 2)
        self.assertEqual(requests[0].full_url, 'https://opencode.ai/zen/v1/models')
        self.assertIsNone(requests[0].get_header('Authorization'))
        self.assertEqual(requests[0].get_header('User-agent'), 'opencode/1.0')
        data = json.loads((root / 'home_dir/chain/opencode-live.json').read_text())
        self.assertEqual(data['models'], ['deepseek-v4.1-flash-free', 'glm-5.3-flash-free'])
