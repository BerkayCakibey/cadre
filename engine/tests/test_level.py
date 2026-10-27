"""G-E9 scope score and level: points 0–100 bound to human-time bands;
the source has two layers (rule floor lower bound + planner estimate), final = max. The level gives the cap, the correction round and the plan
ceiling from one place. No model/network."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import level  # noqa: E402


class AutoEstimateTest(unittest.TestCase):
    def test_untitled_auto_tasks_count_a_default_estimate_toward_the_plan_cap(self):
        """06.10 QA: `auto` always resolves to a paid model; with an estimate of 0 the cap fell to the level floor."""
        plan = {'tasks': [{'model': 'auto'}] * 8}
        self.assertEqual(level.plan_cap(plan, 1.0), round(8 * level.AUTO_ESTIMATE_USD * 1.5, 2))
        self.assertEqual(level.plan_cap({'tasks': [{'model': 'auto', 'estimate_usd': 2.0}]}, 1.0), 3.0)
        self.assertEqual(level.plan_cap({'tasks': [{'model': 'gem-flash'}]}, 1.0), 1.0)


class LevelTableTest(unittest.TestCase):
    def test_plan_estimates_set_the_initial_cap_and_manual_override_wins(self):
        import json
        import os
        import tempfile
        from unittest.mock import patch
        from run_store import RunStore
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'CADRE_HOME': d, 'CADRE_RUN_CAP_USD': ''}):
            store = RunStore()
            rid = store.accept('fixture', 'Fix a typo in README.md')['run_id']
            plan = store.path(rid) / 'plan.json'
            plan.write_text(json.dumps({'scope_points': 25, 'tasks': [{'estimate_usd': 2}, {'estimate_usd': 1}]}))
            self.assertEqual(level.apply(rid, 'fixture')['cap_usd'], 2)
            self.assertEqual(store.read(rid)['budget_plan_cap_usd'], 4.5)
            plan.write_text(json.dumps({'scope_points': 25, 'tasks': [{'estimate_usd': .1}]}))
            self.assertEqual(level.apply(rid, 'fixture')['cap_usd'], 2)
            store.update(rid, budget_spent_usd=5)
            self.assertEqual(level.apply(rid, 'fixture')['cap_usd'], 2)
            store.update(rid, budget_cap_usd=7)
            with patch.dict(os.environ, {'CADRE_RUN_CAP_USD': '7'}):
                self.assertEqual(level.apply(rid, 'fixture')['cap_usd'], 7)
                self.assertEqual(store.read(rid)['operator_budget_usd'], 2)
            for value in (-1, True, float('inf'), 'bad'):
                with self.assertRaises(ValueError):
                    level.plan_cap({'tasks': [{'estimate_usd': value}]}, 2)

    def test_band_limits(self):
        for points, entry_name in ((0, 'simple'), (15, 'simple'), (16, 'middle'), (40, 'middle'), (41, 'hard'), (65, 'hard'),
                         (66, 'very-hard'), (85, 'very-hard'), (86, 'ultra'), (100, 'ultra')):
            with self.subTest(points=points):
                self.assertEqual(level.level(points)['entry_name'], entry_name)

    def test_level_gives_the_cap_the_round_and_the_plan_limit(self):
        self.assertEqual({k: level.level(10)[k] for k in ('cap_usd', 'correction_round', 'plan_most')}, {'cap_usd': 1.0, 'correction_round': 1, 'plan_most': 1})
        self.assertEqual(level.level(30)['cap_usd'], 2.0)
        self.assertEqual(level.level(50)['cap_usd'], 5.0)
        self.assertEqual(level.level(70)['cap_usd'], 10.0)
        self.assertEqual(level.level(70)['correction_round'], 2, 'Research shows diminishing returns after two rounds')

    def test_invalid_points_are_clipped_to_the_limit(self):
        self.assertEqual(level.level(-5)['entry_name'], 'simple')
        self.assertEqual(level.level(250)['entry_name'], 'ultra')


class RuleBaseTest(unittest.TestCase):
    def test_short_single_file_is_simple(self):
        self.assertLessEqual(level.rule_base('Fix the typo in README.md'), 15)

    def test_premium_word_pulls_to_the_hard_base(self):
        self.assertGreaterEqual(level.rule_base('Redesign the auth layer'), 41)

    def test_project_from_scratch_is_ultra(self):
        self.assertGreaterEqual(level.rule_base('Build a new project from scratch: web interface, API, database'), 86)

    def test_numeric_from_scratch_expression_is_not_counted_as_a_project(self):
        """B215 (24.09 R6-easy): 'scratch different offset' (a non-zero offset) made an easy test job ultra. Comparison patterns
        (different/greater/less/equal) are not project markers; a size adjective like 'write a small tool from scratch' is a marker."""
        for request in ('Test RFC-2822 offsets different from zero', 'A date with an offset different from zero',
                      'The value must be greater than zero', 'Numbers less than zero are rejected', 'A value less than or equal to zero',
                      'Warn when the balance is less than zero'):
            with self.subTest(request=request):
                self.assertLess(level.rule_base(request), 86)
        for request in ('Write a small command line note tool from scratch', 'Build a large project from scratch',
                      'Write a web application from scratch', 'write it from scratch'):
            with self.subTest(request=request):
                self.assertGreaterEqual(level.rule_base(request), 86)

    def test_keyword_in_a_code_id_does_not_raise_the_level(self):
        """B216 (24.09 R6-easy): the name `normalize_timestamp` tripped the pre-filter's hard word 'normalize', a test-only job became hard (50).
        Code identifiers (backticks or underscored names) do not count as keywords; a word in plain text does."""
        for request in ("Test missing cases for `normalize_timestamp` in `tests/unit/test_canonical.py`",
                      'Add normalize_alpha2 to country_registry.py and test it',
                      'Fix the bug in `scratch_start`'):
            with self.subTest(request=request):
                self.assertLess(level.rule_base(request), 41)
        self.assertGreaterEqual(level.rule_base('Normalize the database schema, then update the tests'), 41)
        self.assertEqual(level.rule_base('Update `a.py` and `b.py`'), 25, 'The size heuristic still dispatches two file names')

    def test_project_childs_context_does_not_inflate_the_rule_base(self):
        """G-P1: the original request (context) at the end of a feature prompt is not scored; otherwise every child would be very hard because of 'scratch'."""
        import project_mode
        d = {'features': [{'id': 'f1', 'title': 'b', 'status': 'waiting'}]}
        f = {'id': 'f1', 'title': 'record list', 'description': 'Add an install line to README.md', 'acceptance': 'line present'}
        prompt = project_mode.feature_prompt('Build a new project from scratch: web interface, API, database', d, f)
        self.assertIn(level.CONTEXT_MARK, prompt)
        self.assertLess(level.rule_base(prompt), 86)

    def test_long_multi_file_request_is_upper_medium(self):
        request = ('Update src/a.py, src/b.py and tests/test_a.py; ' * 3) + 'then fix the documentation and add tests.'
        self.assertGreaterEqual(level.rule_base(request), 16)


class OnSieveTest(unittest.TestCase):
    """B222: the dispatch PREMIUM flag (the B199 hard cap, the 'hard' class chain) does not trip over code names and substrings; 9 of the
    10 PREMIUMs on 23-24.09 came from here (7 `normalize_*`, 2 'race' inside 'traceback'). The whole golden set runs here too (RC51)."""
    def test_premium_does_not_trigger_on_a_code_name_or_a_substring(self):
        import prefilter
        self.assertEqual(prefilter.decision("Add 4 tests for `normalize_timestamp` in `tests/unit/test_canonical.py` and keep existing tests green")[1], '-')
        self.assertEqual(prefilter.decision('Show a readable error message instead of the traceback and update the CLI tests')[1], '-')
        self.assertEqual(prefilter.decision("Find and fix the race condition in `cache.py`")[1], 'PREMIUM')

    def test_llm_alone_cannot_give_premium(self):
        """OBSERVATION-1 (25.09, user approval): a free LLM raises the rule floor at most to DISPATCH; for PREMIUM (the B199 $5 cap, the 'hard'
        class) the rule must say PREMIUM too. On the night of 25.09 the LLM alone made 2/2 medium jobs PREMIUM (two Opus plans, $0.91)."""
        import json, os, shutil, subprocess, tempfile
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            for entry_name in ('prefilter.py', 'run_store.py', 'runtime_sync.py', 'level.py'):
                shutil.copyfile(LIB / entry_name, root / entry_name)
            (root / 'plan_ds.py').write_text("print('PREMIUM')\n")   # the LLM says PREMIUM for every job
            budget = json.dumps({'go': {'ratio': 1}, 'ds': {'ratio': 1}, 'claude': {'pay': 50}})

            def classify(request):
                (root / 'request.md').write_text(request)
                env = {**os.environ, 'CADRE_HOME': t, 'CADRE_PREFILTER_LLM': '1', 'CADRE_PREFILTER_INTERVAL_SEC': '0'}
                env.pop('CADRE_RUN_ID', None)
                r = subprocess.run([sys.executable, str(root / 'prefilter.py'), str(root / 'request.md')], input=budget, env=env,
                                   capture_output=True, text=True, timeout=30)
                return r.stdout.split()
            self.assertEqual(classify("Add overlap calculations to `src/feedloom/timezones.py` and write 6 tests"), ['PASS', '-'])
            self.assertEqual(classify('Bump the version to 1.2.3'), ['PASS', '-'], 'The LLM may dispatch a short job but cannot make it PREMIUM')
            self.assertEqual(classify('Redesign the auth layer'), ['PASS', 'PREMIUM'], 'A PREMIUM rule stays PREMIUM')
            for request in ('Review the security of src/auth.py; do not make changes.',
                            'Give me only a report on security findings in src/auth.py.',
                            'Please produce only a report on security findings in src/auth.py.',
                            'Do not change any code. Review the security of src/auth.py.'):
                self.assertEqual(classify(request), ['SKIP', '-'], 'The LLM cannot upgrade read-only intent')
            for answer in ('premium', 'distribution'):
                (root / 'plan_ds.py').write_text(f'print({answer!r})\n')
                self.assertEqual(classify('Bump the version to 1.2.3'), ['PASS', '-'])
            for answer in ('PREM\u0131UM', 'DI\u017fTRIBUTION'):
                (root / 'plan_ds.py').write_text(f'print({answer!r})\n')
                self.assertEqual(classify('Bump the version to 1.2.3'), ['SKIP', '-'], 'Non-ASCII classes cannot raise the rule floor')

    def test_at_least_five_seconds_between_groq_requests(self):
        """4c-11: the pre-filter is only Groq gpt-oss-120b; for the free tier >=5 s between requests (stamp file, locked)."""
        import tempfile
        import prefilter
        sleeps = []
        with tempfile.TemporaryDirectory() as t:
            prefilter.interval(t, now_ts=100.0, sleep_fn=sleeps.append)
            prefilter.interval(t, now_ts=102.0, sleep_fn=sleeps.append)     # after 2 s: waits 3 s
            prefilter.interval(t, now_ts=200.0, sleep_fn=sleeps.append)     # much later: does not wait
            prefilter.interval(t, now_ts=50.0, sleep_fn=sleeps.append)      # a clock going backwards: the stamp is ignored
        self.assertEqual([round(u, 3) for u in sleeps], [3.0])

    def test_golden_set_whole(self):
        import prefilter
        from golden_set import CASES
        for request, passes, prem in CASES:
            with self.subTest(request=request[:40]):
                self.assertEqual(prefilter.decision(request), ('PASS' if passes else 'SKIP', 'PREMIUM' if prem else '-'))


class MergeTest(unittest.TestCase):
    def test_final_score_is_the_larger_of_the_rule_and_the_planner(self):
        self.assertEqual(level.merge(20, 55), 55)
        self.assertEqual(level.merge(50, 10), 50, 'A low planner estimate cannot lower the rule floor')
        self.assertEqual(level.merge(20, None), 20)
        self.assertEqual(level.merge(20, 'x'), 20)
        self.assertEqual(level.merge(20, 130), 100)



class PlanLimitTest(unittest.TestCase):
    """G-E9 plan ceiling: the task count cannot exceed what the final level (max(rule, planner)) allows;
    a violation goes back to the planner like a schema error (merge, or raise the score with a justification)."""
    def plan(self, n, points=None):
        p = {'tasks': [{'id': f'g{i}'} for i in range(1, n + 1)]}
        if points is not None:
            p['scope_points'] = points
        return p

    def test_medium_plan_cannot_carry_more_than_three_tasks(self):
        error = level.plan_limit(self.plan(4, 30), rule=25)
        self.assertIn('4 task', error)
        self.assertIn('at most 3', error)
        self.assertIn('scope_points', error, 'The planner is given both ways to resolve the limit')
        self.assertIsNone(level.plan_limit(self.plan(3, 30), rule=25))

    def test_if_the_rule_base_raises_the_level_the_limit_rises_too(self):
        self.assertIsNone(level.plan_limit(self.plan(5, 30), rule=50))

    def test_a_pointless_plan_is_limited_by_the_rule_base(self):
        self.assertIsNotNone(level.plan_limit(self.plan(2), rule=10))
        self.assertIsNone(level.plan_limit(self.plan(1), rule=10))

    def test_planner_prompt_states_the_same_numbers_as_the_level_table(self):
        """Single source level.py: the bands and task limits in the dispatcher prompt must not drift from the table."""
        source = (LIB.parent / 'bin' / 'cadre-dispatch.sh').read_text()
        sub = 0
        for s in level.LEVELS:
            self.assertIn(f"{sub}-{s['parent']} ", source, s['entry_name'])
            sub = s['parent'] + 1
        names = {'simple': 'simple', 'middle': 'middle', 'hard': 'hard', 'very-hard': 'very hard'}
        expected = ', '.join(f"{names[s['entry_name']]} {'' if s['plan_most'] == 1 else 'at most '}{s['plan_most']}"
                             for s in level.LEVELS if s['entry_name'] in names)
        self.assertIn(f'Task count must match the score ({expected})', source)


class LevelApplyEndToEndTest(unittest.TestCase):
    """Real dispatcher: after the plan the level is written to the record, the cap comes from the level, the log gets a SCOPE line."""
    def setUp(self):
        import os
        import test_outcome
        self.f = test_outcome.CompletionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.old = os.environ.pop('CADRE_RUN_CAP_USD', None)
        self.addCleanup(lambda: self.old is not None and os.environ.__setitem__('CADRE_RUN_CAP_USD', self.old))

    def execute_run(self, **plan_extra):
        self.f.dispatch_fixture('PASS', plan_extra=plan_extra)
        return self.f.store.read(self.f.rid), (self.f.store.path(self.f.rid) / 'dispatch.log').read_text()

    def test_planner_score_determines_the_level_and_the_cap(self):
        record, log = self.execute_run(scope_points=70, scope_rationale='module')
        self.assertEqual((record['scope']['level'], record['scope']['planner'], record['budget_cap_usd']), ('very-hard', 70, 2.0))
        self.assertIn('SCOPE: points 70 -> level very-hard', log)

    def test_project_child_cannot_be_ultra(self):
        """G-P1: a project-mode child run does not enter project mode again; its level is at most very hard."""
        parent = self.f.store.accept('lab', 'parent project')['run_id']
        self.f.store.update(self.f.rid, project_ref_parent=parent)
        record, _log = self.execute_run(scope_points=95)
        self.assertEqual((record['scope']['level'], record['scope']['points']), ('very-hard', 85))

    def test_the_rule_base_applies_to_a_pointless_plan(self):
        record, log = self.execute_run()
        self.assertEqual(record['scope']['planner'], None)
        self.assertEqual(record['scope']['points'], record['scope']['rule'])
        self.assertIn('SCOPE: points', log)


if __name__ == '__main__':
    unittest.main()
