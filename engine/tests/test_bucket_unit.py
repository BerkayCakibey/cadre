"""Item 0b — THE BUCKET UNIT IS THE MODEL. A free/paid split per provider did not match reality: zen had 32 free
+ 71 paid models under one id, and Go has zero-priced models too. In the old code a zero-priced model was
denied in the plugin and had no place in the ledger. These tests prove the new '<provider>:free' bucket."""

from __future__ import annotations

import os
from contextlib import closing
import sqlite3
import unittest

import test_run_sessions as fixtures
import budget_gate as budget
from test_budget_period import snapshot


class BucketUnitTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.db = self.f.home / 'chain/request-budget.db'
        self.ledger = budget.BudgetLedger(self.db)
        self.old = os.environ.get('CADRE_FREE_DAY_CAP')
        self.addCleanup(lambda: os.environ.__setitem__('CADRE_FREE_DAY_CAP', self.old)
                        if self.old else os.environ.pop('CADRE_FREE_DAY_CAP', None))

    def test_zero_cost_models_get_their_own_bucket_and_do_not_touch_dollars(self):
        # Go's dollar share is COMPLETELY full: a paid model is denied, a zero-priced model still works.
        # GO1 (03.10): Go is not split into days; no daily margin — full share = 2.0 * 1e6 = 2_000_000.
        self.ledger.reserve('opencode-go', 2_000_000, snapshot(), {'run_id': 'r1'})
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('opencode-go', 100_000, snapshot(), {'run_id': 'r1'})
        ident = self.ledger.reserve('opencode-go:free', 1, snapshot(), {'run_id': 'r1'})
        self.ledger.settle(ident, 1, 'slip-1')
        with closing(sqlite3.connect(self.db)) as con, con:
            self.assertEqual(con.execute("select count(*) from reservation where provider='opencode-go:free'").fetchone()[0], 1)
            # the run dollar cap does NOT COUNT the zero-priced bucket
            settled, held = budget.run_usage(con, 'r1')
        self.assertEqual(settled, 0)
        self.assertEqual(held, 2_000_000)

    def test_the_free_bucket_is_counted_and_bounded_per_day(self):
        os.environ['CADRE_FREE_DAY_CAP'] = '3'
        for i in range(3):
            self.ledger.settle(self.ledger.reserve('opencode-go:free', 1, snapshot(), {}), 1, f'f{i}')
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('opencode-go:free', 1, snapshot(), {})
        # even if the cap is raised later within the same day, the period cap stays fixed with min()
        os.environ['CADRE_FREE_DAY_CAP'] = '9'
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('opencode-go:free', 1, snapshot(), {})

    def test_a_closed_go_window_still_blocks_its_free_models(self):
        closed = snapshot()
        closed['go']['window_open'] = False
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('opencode-go:free', 1, closed, {})
        self.ledger.reserve('opencode-go:free', 1, snapshot(), {})

    def test_an_unknown_base_provider_is_never_admitted_through_the_free_suffix(self):
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('made_up:free', 1, snapshot(), {})
        self.assertEqual(budget.base_provider('opencode-go:free'), 'opencode-go')
        self.assertEqual(budget.base_provider('deepseek'), 'deepseek')


if __name__ == '__main__':
    unittest.main()


class FreeBucketBanTests(unittest.TestCase):
    """A ':free' ban removes only ZERO-PRICED models; the same provider's paid model keeps working.
    Previously both had the same id: when the free model's daily counter filled the whole provider closed."""

    CATALOG = {'google': {'models': {'free-model': {'limit': {'context': 1000}, 'cost': {'input': 0, 'output': 0}},
                                     'paid-model': {'limit': {'context': 1000}, 'cost': {'input': 1, 'output': 2}}}}}

    def setUp(self):
        import json
        import sys
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
        import model_catalog
        import qe_core as core
        self.core = core
        self.temp = tempfile.TemporaryDirectory(prefix='qe-bucket-')
        self.addCleanup(self.temp.cleanup)
        file = Path(self.temp.name) / 'models.json'
        file.write_text(json.dumps(self.CATALOG), encoding='utf-8')
        old = os.environ.get('CADRE_MODEL_CATALOG')
        os.environ['CADRE_MODEL_CATALOG'] = str(file)
        model_catalog._cache.clear()
        self.addCleanup(model_catalog._cache.clear)
        self.addCleanup(lambda: os.environ.__setitem__('CADRE_MODEL_CATALOG', old)
                        if old else os.environ.pop('CADRE_MODEL_CATALOG', None))
        self.chain = ['google/free-model', 'google/paid-model', 'lmstudio/local']
        patch_text = patch.object(core.ROUTER, 'chain', return_value=(self.chain, {}))
        patch_text.start()
        self.addCleanup(patch_text.stop)
        patch2 = patch.object(core.ROUTER, 'status_read', return_value={})
        patch2.start()
        self.addCleanup(patch2.stop)

    def test_free_ban_skips_only_zero_cost_models_of_that_provider(self):
        budget = {'gemini': {'db': 'ok'}}
        model, _ = self.core.restriction_model_select(budget)
        self.assertEqual(model, 'google/free-model')
        model, _ = self.core.restriction_model_select(budget, forbidden={'google:free'})
        self.assertEqual(model, 'google/paid-model', 'The paid model must remain available')
        model, _ = self.core.restriction_model_select(budget, forbidden={'google'})
        self.assertEqual(model, 'lmstudio/local', 'Banning a provider removes all its routes')


class FreeTierInUpperTierTests(unittest.TestCase):
    """B110 (18.09): the upper layer (`opencode_budget_env`) only asked about the BASE provider id; the `:free`
    bucket was never asked. Result: when the zen/Go balance ran out that provider's ZERO-PRICED models were also
    removed and the job fell to local needlessly (item 0b: the bucket unit is the MODEL)."""

    CATALOG = {'opencode-go': {'models': {
        'free-model': {'limit': {'context': 262144}, 'cost': {'input': 0, 'output': 0}},
        'paid-model': {'limit': {'context': 262144}, 'cost': {'input': 1, 'output': 2}}}}}

    def setUp(self):
        import json
        import sys
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
        import model_catalog
        import qe_core as core
        self.core = core
        self.temp = tempfile.TemporaryDirectory(prefix='qe-free-parent-')
        self.addCleanup(self.temp.cleanup)
        file = Path(self.temp.name) / 'models.json'
        file.write_text(json.dumps(self.CATALOG), encoding='utf-8')
        old = os.environ.get('CADRE_MODEL_CATALOG')
        os.environ['CADRE_MODEL_CATALOG'] = str(file)
        model_catalog._cache.clear()
        self.addCleanup(model_catalog._cache.clear)
        self.addCleanup(lambda: os.environ.__setitem__('CADRE_MODEL_CATALOG', old)
                        if old else os.environ.pop('CADRE_MODEL_CATALOG', None))
        for patch_text in (patch.object(core, 'QE', Path(self.temp.name)),
                     patch.object(core, 'project_memory', return_value=[]),
                     patch.object(core.ROUTER, 'status_read', return_value={}),
                     patch.object(core.ROUTER, 'chain',
                                  return_value=(['opencode-go/paid-model', 'opencode-go/free-model',
                                                 'lmstudio/qwen3.8-27b'], {}))):
            patch_text.start()
            self.addCleanup(patch_text.stop)

    def test_zero_cost_models_are_still_offered_when_the_dollar_bucket_is_empty(self):
        from unittest.mock import patch
        with patch.object(self.core, 'budget_json', return_value=snapshot(0.0)):
            env = {'OPENCODE_CONFIG': '/nonexistent'}
            # P3 (03.10): local is closed; the main model also comes from the free bucket (valid even when the dollars run out).
            self.core.opencode_budget_env(env, ['-m', 'opencode-go/free-model', 'task text'])
        import json
        cfg = json.loads(env['OPENCODE_CONFIG_CONTENT'])
        self.assertEqual(cfg['agent']['cadre-plan']['model'], 'opencode-go/free-model',
                         'With cash exhausted and the free bucket open, select a zero-priced model')
        self.assertTrue(any(p.endswith('cadre-plugin.mjs') for p in cfg.get('plugin', [])),
                        'A model selected from a free bucket is still cloud-based; the plugin records its receipt')


class DailyRequestSingleSourceTests(unittest.TestCase):
    """B110 (18.09): the daily request cap was THREE different numbers in the code — 450 applied, 500 reported, 147 measured.
    The applied number comes from a single source and CANNOT EXCEED the RPD the provider reports/measures."""

    def test_reported_limit_comes_from_the_enforced_one(self):
        """The reported daily cap and the APPLIED cap must be the same number. This used to be tested by scanning the provider_budget.py source
        (is it a constant, does it come from DAILY_REQUEST); a source scan is not behavioural
        evidence. Now provider_budget.py is REALLY run and the `ratio`/`remaining_request` values it reports are compared with the ones derived from the
        cap the gate applies."""
        import json
        import subprocess
        import sys as _sys
        from pathlib import Path
        import budget_gate as budget
        root = Path(__file__).resolve().parents[1]
        r = subprocess.run([_sys.executable, str(root / 'lib/provider_budget.py')], capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        report = json.loads(r.stdout)
        for section, gate_name in (('groq', 'groq'),):   # 4c-11: of the free providers only groq remains, in the pre-filter
            with self.subTest(provider=section):
                data = report.get(section) or {}
                self.assertIn('ratio', data, f'{section} report has no remaining ratio: {sorted(data)}')
                cap = int(budget.DAILY_REQUEST[gate_name])
                used = int(data.get('today_request', 0))
                expected = max(0.0, (cap - used) / cap)
                self.assertAlmostEqual(float(data['ratio']), expected, places=2,
                                       msg=f'{section}: The reported ratio from the applied cap ({cap}) is not derived')
                if 'remaining_request' in data:
                    self.assertEqual(int(data['remaining_request']), max(0, cap - used),
                                     f'{section}: remaining requests do not match the applied cap')

    def test_enforced_daily_limit_never_exceeds_the_providers_own_rpd(self):
        import budget_gate as budget
        import model_router
        for provider_key, applied in budget.DAILY_REQUEST.items():
            rpd = model_router.PROVIDER.get(provider_key, {}).get('rpd')
            if rpd is None:
                continue
            with self.subTest(provider=provider_key):
                self.assertLessEqual(applied, int(rpd),
                                     f'{provider_key}: The applied daily cap exceeds the provider RPD limit')


class PerModelDailyBucketTests(unittest.TestCase):
    """18.09: the daily request cap may be per model (Gemini; removed in 4c-11, the table is empty). The provider-level bucket works."""

    def snapshot(self, counters=None, total=0):
        import time
        return {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'today': time.strftime('%Y-%m-%d'),
                'gemini': {'db': 'ok', 'today_request': total,
                           **({'today_request_model': counters} if counters is not None else {})}}


    def test_the_provider_level_bucket_still_works_for_the_others(self):
        import time
        instant = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'today': time.strftime('%Y-%m-%d'),
                 'groq': {'db': 'ok', 'today_request': 100}}
        _d, remaining = budget.allowance(instant, 'groq')
        self.assertEqual(remaining, budget.DAILY_REQUEST['groq'] - 100)
