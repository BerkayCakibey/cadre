"""Budget ladder (budget QA 24.09, user decision): on a prepaid provider (DeepSeek, zen, OpenAI) the unused share
carries over to the next day (K1: daily share = fresh share + accumulation, fixed at the start of the day; no floor). The position in the chain comes from the ratio of
the money that can be spent today to the typical job cost in that role (ledger p75 of the last 7 days): not enough for half a job stays out and accumulates,
up to one job it is a fallback (before the last resort), covering 1.25 jobs it is active. No model/network."""

from __future__ import annotations

import datetime
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import call, patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import provider_budget  # noqa: E402
import model_router as router  # noqa: E402
from test_zen import fake_db  # noqa: E402
from test_budget_scenarios import instant  # noqa: E402

CATALOG = {'opencode': {'models': {'deepseek-v4-flash': {'cost': {'input': 0.14, 'output': 0.28}},
                                   'minimax-m3': {'cost': {'input': 0.3, 'output': 1.2}},
                                   'big-pickle': {'cost': {'input': 0, 'output': 0}},
                                   'nemotron-3-ultra-free': {'cost': {'input': 0, 'output': 0}}}},
           'deepseek': {'models': {'deepseek-v4-pro': {'cost': {'input': 0.5, 'output': 2}},
                                   'deepseek-v4-flash': {'cost': {'input': 0.14, 'output': 0.28}}}},
           'opencode-go': {'models': {'glm-5.3-flash': {'cost': {'input': 0.1, 'output': 0.4}},
                                      'qwen3.8-flash': {'cost': {'input': 0.1, 'output': 0.4}},
                                      'qwen3.7-plus': {'cost': {'input': 0.4, 'output': 1.6}}}},
           'cerebras': {'models': {'qwen-3.8-27b': {'cost': {'input': 0.6, 'output': 1.2}}}},
           'openai': {'models': {'gpt-6.1-sol': {'cost': {'input': 1, 'output': 8}}}}}


def catalog_environment(test):
    t = tempfile.TemporaryDirectory(prefix='ladder-catalog-')
    test.addCleanup(t.cleanup)
    (Path(t.name) / 'models.json').write_text(json.dumps(CATALOG))
    ctx = patch.dict(os.environ, {'CADRE_MODEL_CATALOG': str(Path(t.name) / 'models.json')})
    ctx.__enter__()
    test.addCleanup(ctx.__exit__, None, None, None)


def snapshot(**provider):
    """instant() observation: provider=(share, available) dollars; Go dollars are converted to the points of the $15 model (4c-13)."""
    extra = {k: {'pay': v[0], 'available': v[1]} if k != 'go' else {'pay': v[0] * 100 / 15, 'available': v[1] * 100 / 15}
          for k, v in provider.items() if k != 'job_cost'}
    if 'oa' in extra:
        extra['oa'].update(db='ok')
    if 'or' in extra:
        extra['or'].update(db='ok')
    return instant(**extra, job_cost=provider.get('job_cost', {}))




class LadderTest(unittest.TestCase):
    def setUp(self):
        catalog_environment(self)

    def test_three_levels_by_job_cost(self):
        is_m = {'lanes': {'opencode': 0.1}}
        for used_amount, expected in ((0.049 + 0.0525, 'outside'), (0.08 + 0.0525, 'reserve'), (0.13 + 0.0525, 'active')):
            with self.subTest(expected=expected):
                b = snapshot(zen=(0.35, used_amount), job_cost=is_m)
                self.assertEqual(router.ladder('opencode/deepseek-v4-flash', 'impl', b), expected)

    def test_money_that_does_not_cover_a_single_gate_call_is_out(self):
        """B221 (25.09 live R10-hard): OpenAI's money for today was $0.048, a plan job $0.027 -> counted as 'active'; the gate however holds a
        ~$0.33 calibrated reservation for a single call and denied the attempt. Unit = max(typical job, typical reservation); money that does not cover the
        reservation stays out and accumulates. Without a measurement in the role bucket the provider-wide ('*') one is used."""
        is_m = {'plan': {'opencode': 0.027}}
        for resv in ({'plan': {'opencode': 0.33}}, {'*': {'opencode': 0.33}}):
            for used_amount, expected in ((0.063, 'outside'), (0.30, 'outside'), (0.37, 'reserve'), (0.5, 'active')):
                with self.subTest(resv=resv, used_amount=used_amount):
                    b = snapshot(zen=(0.1, used_amount), job_cost=is_m)
                    b['reservation'] = resv
                    self.assertEqual(router.provider_ladder('opencode', 'plan', b), expected)
        b = snapshot(zen=(0.1, 0.063), job_cost=is_m)
        self.assertEqual(router.provider_ladder('opencode', 'plan', b), 'active', 'Without a reservation measurement, use the previous measure')


    def test_free_and_claude_are_not_subject_to_the_ladder(self):
        b = snapshot(zen=(0.35, 0.0), job_cost={'lanes': {'opencode': 1.0}})
        self.assertIsNone(router.ladder('opencode/big-pickle', 'impl', b))
        self.assertIsNone(router.ladder('claude/sonnet#high', 'plan', b))

    def test_role_default_without_a_measurement(self):
        self.assertEqual(router.job_cost('quality', 'openai', {}), router.JOB_COST_FALLBACK['judge'])
        self.assertEqual(router.job_cost('impl', 'opencode', {'job_cost': {'lanes': {'_': 0.2}}}), 0.2)

    def test_the_one_outside_the_chain_is_eliminated_and_the_chain_is_not_local_last(self):
        # D13 (03.10): impl chain = the three provider routes of ds-v4-pro; there is NO local link in the chain (P3).
        # abundance on Go is not enough for half a job (job $0.05 > remaining $0.02) -> out, money accumulates;
        # the OpenRouter/DeepSeek routes stay active. No fallback: today's money in the container is at full-job level.
        from unittest.mock import patch as _p
        t = tempfile.TemporaryDirectory(prefix='ladder-qe-')
        self.addCleanup(t.cleanup)
        b = snapshot(go=(0.02, 0.02), ds=(0.2, 0.1), job_cost={'lanes': {'opencode-go': 0.05, 'deepseek': 0.05}})
        with _p.object(router, 'QE', Path(t.name)), _p.object(router, 'has_budget', return_value=(True, 'test')), \
             _p.object(router, 'is_compact', return_value=False):
            z, reason = router.chain('impl', 'middle', 20000, b, {})
        self.assertNotIn('opencode-go/deepseek-v4-pro', z, 'Go cannot cover even half a job; leave it outside while funds accumulate')
        self.assertEqual(set(z), {'openrouter/deepseek/deepseek-v4-pro', 'openrouter/z-ai/glm-5.3'})
        self.assertTrue(any('ladder' in n for n in reason), reason)

    def test_role_priority_is_kept_among_models_with_enough_budget(self):
        # D16: if both models cover the call's money, the model priority
        # does not change with the size of the wallet; without a measurement the role decision applies.
        from unittest.mock import patch as _p
        t = tempfile.TemporaryDirectory(prefix='ladder-qe-')
        self.addCleanup(t.cleanup)
        b = snapshot(**{'or': (0.3, 0.075)}, go=(9.0, 0.05))
        selected = {'openrouter/openai/gpt-6.1-sol', 'opencode-go/qwen3.8-max'}
        with _p.object(router, 'QE', Path(t.name)), _p.object(router, 'has_budget',
                side_effect=lambda provider_key, m, *a, **k: (m in selected, 'test')), \
             _p.object(router, 'is_compact', return_value=False):
            z = router.chain('plan', 'middle', 20000, b, {})[0]
        self.assertEqual(z, ['openrouter/openai/gpt-6.1-sol', 'opencode-go/qwen3.8-max'], z)   # 4c-6: role order Sol > qwen

    def test_writer_chain_skips_a_provider_that_cannot_cover_two_calls(self):
        """B239's D13 adaptation: the report writer is a long lane; trying a provider whose daily share barely covers a single lane call
        and failing at the gate burns time and chain allowance. The 'writer' role = plan
        chain + breathing filter; with enough share for Sol (openrouter) in the plan chain the writer skips the breathless provider."""
        from unittest.mock import patch as _p
        t = tempfile.TemporaryDirectory(prefix='ladder-qe-')
        self.addCleanup(t.cleanup)
        b = snapshot(go=(8.5, 8.2), job_cost={'plan': {'opencode-go': 0.07, 'openrouter': 0.03}})
        b['or'] = {'db': 'ok', 'pay': 0.2, 'available': 0.079}
        b['reservation'] = {'lanes': {'openrouter': 0.042, 'opencode-go': 0.05}}
        selected = {'openrouter/openai/gpt-6.1-sol', 'opencode-go/qwen3.8-max'}
        with _p.object(router, 'QE', Path(t.name)), _p.object(router, 'has_budget',
                side_effect=lambda provider_key, m, *a, **k: (m in selected, 'test')), \
             _p.object(router, 'is_compact', return_value=False):
            writer, reason = router.chain('writer', 'middle', 30000, b, {})
            plan = router.chain('plan', 'middle', 30000, b, {})[0]
        self.assertIn('openrouter/openai/gpt-6.1-sol', plan, 'The plan chain is unchanged; one call covers a plan')
        self.assertNotIn('openrouter/openai/gpt-6.1-sol', writer, 'writer for 0,049 < 2 x 0,042')
        self.assertEqual(writer[0], 'opencode-go/qwen3.8-max', writer)
        self.assertTrue(any('writer' in n and 'openrouter' in n for n in reason), reason)

    def test_writer_chain_skips_a_provider_that_cannot_fit_two_calls_in_the_run_pocket(self):
        """B239b (D13 adaptation): even if the provider's daily share is plentiful, if the RUN POCKET (lane slice) does not cover two Claude calls
        the first call empties the pocket and the writer is left partial. The breathing filter measures the run pocket too; in the D13 plan
        chain the paid counterpart of this scenario is Go qwen3.8-max."""
        from unittest.mock import patch as _p
        t = tempfile.TemporaryDirectory(prefix='ladder-qe-')
        self.addCleanup(t.cleanup)
        b = snapshot(go=(8.5, 8.2))
        b['reservation'] = {'lanes': {'opencode-go': 0.05, 'claude': 1.31}}
        b['claude'] = {'source': 'fresh', 'pay': 3, 'percent1_usd': 10, 'pool_remaining_usd': 20.0}   # share<4: sonnet is chosen
        selected = {'claude/sonnet#high', 'opencode-go/qwen3.8-max'}
        with _p.object(router, 'QE', Path(t.name)), _p.object(router, 'has_budget',
                side_effect=lambda provider_key, m, *a, **k: (m in selected, 'test')), \
             _p.object(router, 'run_pocket_remaining', return_value=1.29), \
             _p.object(router, 'allowance_usd', side_effect=lambda b_, provider_key, *_: 50.0):
            writer, reason = router.chain('writer', 'middle', 30000, b, {})
        self.assertNotIn('claude/sonnet#high', writer, 'run pocket 1.29 < 2 x 1.31')
        self.assertEqual(writer[0], 'opencode-go/qwen3.8-max', writer)
        self.assertTrue(any('writer' in n and 'claude' in n and 'run pocket' in n for n in reason), reason)

    def test_gate_reservation_uses_the_calibrated_value_if_there_is_one(self):
        """B253: if the gate now reserves with the calibrated cap, the breathing filters (B239/B242) must measure the same number; the old p50 is only a fallback."""
        b = snapshot(go=(8.5, 8.2))
        b['reservation'] = {'judge': {'claude': 1.72}, '*': {'claude': 1.6}}
        b['calibrated'] = {'judge': {'claude': 0.615}}
        self.assertAlmostEqual(router.gate_reservation('quality', 'claude', b), 0.615, places=4)
        self.assertAlmostEqual(router.gate_reservation('impl', 'claude', b), 1.6, places=4, msg='Use p50 as fallback without calibration')
        b['calibrated']['judge']['opencode-go'] = 0.02
        b['reservation']['judge']['opencode-go'] = 0.05
        self.assertAlmostEqual(router.gate_reservation('quality', 'opencode-go', b), 0.05, places=4, msg='The Go gate does not reserve a calibrated amount; p50 stays')

    def test_judge_breath_marks_a_provider_that_cannot_fit_two_calls_in_the_run_judge_pocket(self):
        """B242 (26.09 live R14-easy): the Claude judge's first call passed ($0.18), the second did not fit the run's judge pocket -> the link
        was wasted. The judge also makes at least two calls (tool + verdict); if the pocket does not cover two gate reservations the candidate is never called."""
        from unittest.mock import patch as _p
        b = snapshot(go=(8.5, 8.2))
        b['reservation'] = {'judge': {'claude': 3.636, 'opencode-go': 0.05}}
        with _p.object(router, 'run_pocket_remaining', side_effect=lambda role='impl', provider='': {'quality': 1.42}.get(role, 9.0)) as remaining, \
             _p.object(router, 'allowance_usd', side_effect=lambda b_, provider_key, *_: 50.0):
            narrow = router.judge_breath('claude/opus#max', b)
            divide = router.judge_breath('opencode-go/glm-5.2', b)
        self.assertEqual(narrow[:2], (1.42, 2 * 3.636), narrow)
        self.assertEqual(narrow[2], 'pocket')
        self.assertIsNone(divide, 'With a Go reservation of 0.05, 1.42 covers two calls')
        self.assertEqual(remaining.call_args_list, [call('quality', 'claude'), call('quality', 'opencode-go')])

    def test_writer_breath_measures_the_gates_worst_amount_on_openrouter(self):
        import json
        import tempfile
        from pathlib import Path as _P
        from unittest.mock import patch as _p
        b = snapshot(go=(8.5, 8.2))
        b['reservation'] = {'*': {'openrouter': 0.02}}
        with tempfile.TemporaryDirectory() as d:
            home_dir = _P(d)
            (home_dir / 'chain').mkdir()
            (home_dir / 'chain/openrouter-price.json').write_text(json.dumps({'models': {'openai/gpt-6.1-sol': {'input': 2, 'output': 10}}}))
            with _p.object(router, 'QE', home_dir), _p.object(router, 'run_pocket_remaining', return_value=None):
                with _p.object(router, 'net_capacity_usd', return_value=0.14):
                    self.assertIsNotNone(router.writer_breath('openrouter/openai/gpt-6.1-sol', b), 'OR 0.14 USD does not cover two worst-case calls')
                with _p.object(router, 'net_capacity_usd', return_value=65.0):
                    self.assertIsNone(router.writer_breath('openrouter/openai/gpt-6.1-sol', b))
            with _p.object(router, 'QE', home_dir), _p.object(router, 'run_pocket_remaining', return_value=0.46), \
                 _p.object(router, 'net_capacity_usd', return_value=65.0):
                self.assertIsNone(router.writer_breath('openrouter/openai/gpt-6.1-sol', b), 'Measure the pocket using the calibrated gate amount')

    def test_small_change_in_judge_order_reserve_last(self):
        import qe_core as core
        b = snapshot(zen=(0.2, 0.05), job_cost={'judge': {'opencode': 0.07}})
        self.assertEqual(core.judge_cost('opencode', b), 9)
        b = snapshot(zen=(4.0, 4.0), job_cost={'judge': {'opencode': 0.07}})
        self.assertEqual(core.judge_cost('opencode', b), core.JUDGE_EXPENSE_ORDER['opencode'])


class BudgetRuleTest(unittest.TestCase):
    """Three rules missing tests in the budget matrix (budget QA 24.09): the Claude plan on a small job, an easy job on local, the cap source."""
    def setUp(self):
        catalog_environment(self)
        t = tempfile.TemporaryDirectory(prefix='ladder-rule-')
        self.addCleanup(t.cleanup)
        self.home_dir = Path(t.name)
        ctx = patch.dict(os.environ, {'CADRE_HOME': str(self.home_dir), 'CADRE_ROUTER_STATUS': str(self.home_dir / 'd.json'), 'CADRE_ROUTER_RECORD': '/dev/null'})
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def test_claude_does_not_plan_a_small_job(self):
        b = snapshot()
        with patch.object(router, 'has_budget', return_value=(True, 'test')), patch.object(router, 'is_compact', return_value=False):
            small = router.chain('plan', 'small', 20000, b, {})[0]
            middle = router.chain('plan', 'middle', 20000, b, {})[0]
        self.assertFalse(any(m.startswith('claude/') for m in small), small)
        self.assertTrue(middle[0].startswith('claude/'), middle)

    def test_easy_job_is_not_given_to_a_paid_model(self):
        # D13/P3 (03.10): difficulty 1 -> impl-light; NO local link in the chain. If the budget is not visible, paid links
        # are removed and strictness_model returns None (rc 76 'no-capacity' in the subprocess); the old behaviour (difficulty 1 + paid
        # model -> local) was reverted — the user decision (P3) is kept, as the test dispatch showed.
        with patch.object(router, 'budget_read', return_value={}):
            self.assertEqual(router.difficulty_model('opencode-go/kimi-k3', 1, 'impl'), None,
                             'Difficulty 1 with a paid model; local disabled and no budget means no capacity')

    def test_planner_determines_the_cap_spent_and_operator_are_preserved(self):
        import level
        from run_store import RunStore
        (self.home_dir / 'projects/a').mkdir(parents=True)
        (self.home_dir / 'projects/a/run-cap-usd').write_text('4.0')
        store = RunStore(self.home_dir)
        execute = lambda points, spent=0.0: (lambda rid: (store.update(rid, budget_spent_usd=spent),
                                                       (store.path(rid) / 'plan.json').write_text(json.dumps({'scope_points': points})),
                                                       level.apply(rid, 'a'), store.read(rid)['budget_cap_usd'])[-1])(
            store.accept('a', 'Add one function')['run_id'])
        self.assertEqual(execute(30), 4.0, 'The recorded user cap is preserved')
        self.assertEqual(execute(80), 4.0, 'The plan cannot raise the user cap')
        self.assertEqual(execute(30, spent=3.0), 4.0, 'Spending does not change the cap')
        with patch.dict(os.environ, {'CADRE_RUN_CAP_USD': '7'}):
            self.assertEqual(execute(30), 7.0, 'The operator setting is preserved')


if __name__ == '__main__':
    unittest.main()
