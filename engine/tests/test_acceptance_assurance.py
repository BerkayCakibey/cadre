"""P6 acceptance guarantee (06.10 smoke test): the role output cap and the automatic raise of a small cap."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import test_run_sessions as fixtures  # noqa: E402
import qe_core as core  # noqa: E402
import acceptance  # noqa: E402
from budget_gate import BudgetDenied  # noqa: E402
from test_night_wait import full_budget  # noqa: E402

IMPL, JUDGE = 'opencode-go/deepseek-v4-pro', 'opencode-go/glm-5.3'


def chain(role, *_a, **_k):
    return ([JUDGE, 'opencode-go/qwen3.8-max'] if role == 'quality' else [IMPL]), []


class AcceptanceAssuranceTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.enter(patch.object(core.ROUTER, 'chain', side_effect=chain))
        b = full_budget(cloud=True)
        b['go'].update(available=50.0, pay=50.0)
        self.f.enter(patch.object(core, 'budget_json', return_value=b))
        self.log = self.f.home / 'assurance.log'

    def expected_total(self, full_output=False):
        from model_catalog import model as catalog_model
        usd = 0.0
        for role, m, count in (('quality', JUDGE, 2), ('qa', IMPL, 1)):
            provider_key, mid = m.split('/', 1)
            cost, lim = catalog_model(provider_key, mid)['cost'], catalog_model(provider_key, mid)['limit']
            ctx = min(int(lim.get('input') or lim['context']), core.ROUTER.role_context(role))
            dense = 2 if mid.startswith('deepseek') else 1   # Go DeepSeek reservation at the peak price (4c-13)
            output = int(lim['output']) if full_output else min(acceptance.ROLE_OUTPUT[role], int(lim['output']))
            usd += count * dense * (ctx * float(cost['input']) + output * float(cost['output'])) / 1e6
        return usd

    def test_guarantee_uses_role_output_caps_not_the_full_output_limit(self):
        self.f.store.update(self.f.rid, budget_cap_usd=50.0)
        choice = acceptance.acceptance_assurance(self.log)
        self.assertEqual(len(choice['reservations']), 3)
        base = self.f.store.read(self.f.rid)['acceptance_assurance_base_micro'] / 1e6
        self.assertAlmostEqual(base, self.expected_total(), places=4)
        self.assertLess(base, self.expected_total(full_output=True), 'The role cap is below the full model output limit')

    def test_a_small_automatic_cap_is_raised_so_the_guarantee_fits(self):
        self.f.store.update(self.f.rid, budget_cap_usd=0.05, budget_spent_usd=0.02)
        choice = acceptance.acceptance_assurance(self.log)
        self.assertEqual(len(choice['reservations']), 3)
        r = self.f.store.read(self.f.rid)
        self.assertGreaterEqual(r['budget_cap_usd'] * 0.6 + 1e-6, self.expected_total() + 0.02, 'assurance plus spent is 60 percent of the cap')
        self.assertIn('P6: run cap', self.log.read_text())

    def test_a_resumed_run_with_a_recorded_guarantee_still_gets_its_cap_raised(self):
        """06.10 smoke test, 2nd attempt: with the guarantee recorded, the cap was not raised in a continuing run and the lanes fell to waiting again."""
        self.f.store.update(self.f.rid, budget_cap_usd=50.0)
        acceptance.acceptance_assurance(self.log)
        self.f.store.update(self.f.rid, budget_cap_usd=0.05, budget_spent_usd=0.4)
        acceptance.acceptance_assurance(self.log)
        r = self.f.store.read(self.f.rid)
        self.assertGreaterEqual(r['budget_cap_usd'] * 0.6 + 1e-6, self.expected_total() + 0.4)

    def test_a_manual_cap_that_cannot_hold_the_guarantee_is_denied(self):
        self.f.store.update(self.f.rid, budget_cap_usd=0.05, budget_manual=True)
        with self.assertRaises(BudgetDenied):
            acceptance.acceptance_assurance(self.log)
        self.assertEqual(self.f.store.read(self.f.rid).get('acceptance_assurance_base_micro', 0), 0)
        self.assertEqual(self.f.store.read(self.f.rid)['budget_cap_usd'], 0.05)


    def test_released_guarantee_leaves_no_held_money_in_the_run_record(self):
        """07.10 smoke: when the run ended the guarantee closed in the ledger but run.json kept showing $0.18 pending + a warning."""
        from budget_gate import BudgetLedger, run_spend_write
        self.f.store.update(self.f.rid, budget_cap_usd=50.0)
        acceptance.acceptance_assurance(self.log)
        ledger = BudgetLedger(self.f.home / 'chain/request-budget.db')
        run_spend_write(self.f.home, ledger, self.f.rid)
        self.assertGreater(self.f.store.read(self.f.rid)['budget_held_usd'], 0)
        ledger.assurance_leave(self.f.rid)
        r = self.f.store.read(self.f.rid)
        self.assertEqual((r['budget_held_usd'], r['budget_unsettled']), (0, []))

    def test_plan_reserve_holds_one_call_on_a_cash_path_and_is_released_after_planning(self):
        """4b (07.10 user): at run start one plan call is reserved on Zen/OR (without relying on Claude/Go); refunded when the plan ends."""
        import sqlite3
        b = full_budget(cloud=True)
        b['go'].update(available=50.0, pay=50.0)
        b['zen'] = {'db': 'ok', 'pay': 9.0, 'available': 9.0, 'remaining': 9.0}
        self.f.store.update(self.f.rid, budget_cap_usd=50.0)
        plan_chain = (['opencode-go/qwen3.8-max', 'opencode/kimi-k3'], [])
        with patch.object(core, 'budget_json', return_value=b), patch.object(core.ROUTER, 'chain', return_value=plan_chain):
            choice = acceptance.plan_assurance(self.log)
        self.assertEqual(choice['model'], 'opencode/kimi-k3', 'A cash route through Zen or OR, rather than Go')
        db = self.f.home / 'chain/request-budget.db'
        from contextlib import closing
        def line():
            with closing(sqlite3.connect(db)) as con:
                return con.execute("select actual, json_extract(metadata,'$.assurance_role') from reservation where id=?",
                                   (choice['reservation'],)).fetchone()
        self.assertEqual(line(), (None, 'plan'))
        # 07.10 T3 live: the reserve filled the lane pocket and got the primary plan call (Go) denied; the reserve is not charged to the run pocket
        from budget_gate import BudgetLedger, role_budget
        self.f.store.update(self.f.rid, budget_cap_usd=2.0)
        cap, slice = role_budget(self.f.home, self.f.rid, 'plan')
        import budget_gate
        self.assertAlmostEqual(budget_gate.run_remaining_usd(self.f.home, self.f.rid, 'plan'), cap / 1e6, places=4,
                               msg='Pre-check does not deduct the plan reserve from the pocket, matching gate accounting')
        self.assertGreaterEqual(BudgetLedger(db).available('opencode-go', b, cap, self.f.rid, slice, role='plan', model='qwen3.8-max'),
                                cap - 10_000)
        BudgetLedger(db).reserve('opencode-go', cap - 10_000, b, {'run_id': self.f.rid, 'phase_role': 'plan', 'model': 'qwen3.8-max'}, cap, slice)
        acceptance.plan_assurance_leave()
        self.assertEqual(line(), (0, 'plan'))
        self.assertTrue(self.f.store.read(self.f.rid)['plan_assurance']['left'])

    def test_every_terminal_path_releases_the_unused_guarantee(self):
        """07.10: when a run without a coordinator was cancelled with `cadre-cancel` the acceptance guarantee was not refunded; live, 3 lines
        ($1.67) stayed pending and `cadre recover` could not close them because it did not see guarantee lines. Every terminal path refunds."""
        import sqlite3
        from contextlib import closing
        import run_store
        self.f.store.update(self.f.rid, budget_cap_usd=50.0)
        acceptance.acceptance_assurance(self.log)
        def pending():
            with closing(sqlite3.connect(self.f.home / 'chain/request-budget.db')) as con:
                return con.execute("select count(*) from reservation where actual is null").fetchone()[0]
        self.assertEqual(pending(), 3)
        self.f.store.update(self.f.rid, state='retryable', owner_pid=None)
        self.assertTrue(run_store.cancel_run(self.f.store, self.f.store.read(self.f.rid)))
        self.assertEqual(self.f.store.read(self.f.rid)['state'], 'cancelled')
        self.assertEqual(pending(), 0)
        self.assertEqual(self.f.store.read(self.f.rid)['budget_held_usd'], 0)


class TargetedQaTests(unittest.TestCase):
    def test_final_acceptance_qa_also_runs_the_commands_of_the_stuck_features(self):
        self.assertEqual(acceptance.with_extra_acceptance('make check', {'extra_acceptance_commands': ['pytest -q t.py', ' ']}), '(make check) && (pytest -q t.py)')
        self.assertEqual(acceptance.with_extra_acceptance('make check', {}), 'make check')
        self.assertEqual(acceptance.with_extra_acceptance('', {'extra_acceptance_commands': ['pytest -q t.py']}), '(pytest -q t.py)')

    def test_pytest_projects_run_selected_tests_with_pytest_from_the_repo_root(self):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            wt = Path(d)
            git = lambda *a: subprocess.run(['git', '-C', d, *a], check=True, capture_output=True, text=True).stdout.strip()
            git('init', '-q'); git('config', 'user.email', 't@t'); git('config', 'user.name', 't')
            (wt / 'foo.py').write_text('X = 1\n')
            git('add', '-A'); git('commit', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            (wt / 'tests').mkdir()
            (wt / 'tests/test_foo.py').write_text('import unittest\n\nclass T(unittest.TestCase):\n    def test_value(self):\n        assert 1\n')
            record = {'source_sha': base, 'feature_acceptance': {}}
            self.assertIn('-m unittest test_foo', acceptance.targeted_qa_command(record, wt))
            (wt / 'tests/test_foo.py').write_text('def test_value():\n    assert 1\n')
            self.assertIn('-m pytest -q tests/test_foo.py', acceptance.targeted_qa_command(record, wt), 'unconfigured pytest-style test')
            (wt / 'tests/test_foo.py').write_text('import unittest\n\nclass T(unittest.TestCase):\n    def test_value(self):\n        assert 1\n')
            (wt / 'pyproject.toml').write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
            command = acceptance.targeted_qa_command(record, wt)
            self.assertIn('-m pytest -q tests/test_foo.py', command)
            self.assertNotIn('cd ', command)


if __name__ == '__main__':
    unittest.main()


SOL = 'openrouter/openai/gpt-6.1-sol'


class PinnedAssuranceTests(unittest.TestCase):
    def test_guarantee_matches_the_verified_exclusive_route_and_rejects_unknown_prices(self):
        import json
        import time
        import model_selection
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / 'chain').mkdir()
            path = home / 'chain/openrouter-price.json'
            quote = {'version': 2, 'ts': time.time(), 'models': {'openai/gpt-6.1-sol': {
                'input': 1, 'output': 5, 'providers': ['openai'], 'fp8_cache': True}}}
            path.write_text(json.dumps(quote))
            model = fixtures.CATALOG['openrouter']['models']['openai/gpt-6.1-sol']
            with patch.object(core, 'QE', home), patch('model_catalog.model', return_value=model), \
                 patch.object(model_selection, 'catalog_model', return_value=model):
                amount = acceptance._assurance_amount('quality', SOL)
                self.assertEqual(amount, 219_840, 'FLEX 1/5; uncallable 2/10 catalog routes do not enlarge the guarantee')
                self.assertEqual(amount, model_selection.call_cap(SOL, core.ROUTER.role_context('quality'), 32768, home))
                quote['ts'] -= 4000
                path.write_text(json.dumps(quote))
                with self.assertRaisesRegex(ValueError, 'OR-PIN'):
                    acceptance._assurance_amount('quality', SOL)
                quote['ts'] = time.time()
                path.write_text(json.dumps(quote))
                unknown = {**model, 'limit': {k: v for k, v in model['limit'].items() if k != 'output'}}
                with patch('model_catalog.model', return_value=unknown), patch.object(model_selection, 'catalog_model', return_value=unknown):
                    with self.assertRaisesRegex(ValueError, 'output capacity'):
                        acceptance._assurance_amount('quality', SOL)
            self.assertFalse((home / 'chain/request-budget.db').exists())


class JudgeSizeEstimateTests(unittest.TestCase):
    """4c-5: the P6 judge is chosen by the size estimate; if the real size changes the judge the quality guarantee moves."""

    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        chains = {'quality': [SOL, JUDGE, 'opencode-go/qwen3.8-max'], 'impl': [IMPL, JUDGE]}
        self.f.enter(patch.object(core.ROUTER, 'chain', side_effect=lambda role, *a, **k: (list(chains.get(role, [IMPL])), [])))
        self.b = full_budget(cloud=True)
        self.b['go'].update(available=50.0, pay=50.0)
        self.b['or'].update(pay=20.0, available=20.0, remaining=20.0)
        self.f.enter(patch.object(core, 'budget_json', return_value=self.b))
        import json
        import time
        (self.f.home / 'chain/openrouter-price.json').write_text(json.dumps({
            'version': 2, 'ts': time.time(), 'models': {'openai/gpt-6.1-sol': {
                'input': 1, 'output': 5, 'providers': ['openai'], 'fp8_cache': True}}}))
        self.f.store.update(self.f.rid, budget_cap_usd=50.0)
        self.log = self.f.home / 'assurance.log'

    def plan(self, file_count):
        core.atomic_json(self.f.store.path(self.f.rid) / 'plan.json',
                         {'tasks': [{'id': f'g{i}', 'files': [f'f{i}.py']} for i in range(file_count)]})

    def open_quality(self):
        import json
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.f.home / 'chain/request-budget.db')) as con:
            return [(p, json.loads(m)['model']) for p, m in con.execute(
                "select provider, metadata from reservation where actual is null "
                "and json_extract(metadata,'$.assurance_role')='quality' order by created, id")]

    def test_without_a_file_list_it_counts_as_big_and_the_left_is_reserved(self):
        self.assertFalse(acceptance.estimated_small(self.f.store, self.f.rid))
        choice = acceptance.acceptance_assurance(self.log)
        self.assertEqual((choice['small_estimate'], choice['models']['quality']), (False, SOL))

    def test_assurance_falls_to_the_next_judge_if_or_is_first_and_or_has_no_money(self):
        self.b['or'].update(pay=0.14, available=0.14, remaining=0.14)
        choice = acceptance.acceptance_assurance(self.log)
        self.assertEqual((choice['small_estimate'], choice['models']['quality']), (False, JUDGE))
        self.assertEqual(self.open_quality(), [('opencode-go', 'glm-5.3')] * 2)

    def test_unknown_output_cannot_guarantee_an_openrouter_call_and_uses_the_explicit_alternative(self):
        import model_catalog
        real = model_catalog.model

        def live_like(provider_key, mid):
            m = real(provider_key, mid)
            if provider_key == 'openrouter' and m:
                m = {**m, 'limit': {k: v for k, v in (m.get('limit') or {}).items() if k != 'output'}}
            return m
        with patch.object(model_catalog, 'model', side_effect=live_like):
            with patch('model_selection.catalog_model', side_effect=live_like):
                with self.assertRaisesRegex(ValueError, 'output capacity'):
                    acceptance._assurance_amount('quality', SOL)
                choice = acceptance.acceptance_assurance(self.log)
        self.assertEqual(choice['models']['quality'], JUDGE)

    def test_qa_assurance_is_not_reserved_in_a_report_job(self):
        real = core.RunStore.read
        with patch.object(core.RunStore, 'read', lambda st, rid: {**real(st, rid), 'report': {'path': 'R.md', 'sections': []}}):
            choice = acceptance.acceptance_assurance(self.log)
        self.assertEqual((len(choice['reservations']), choice['models'].get('qa')), (2, None))

    def test_split_plan_up_to_ten_files_is_small_and_go_judge_first(self):
        self.plan(10)
        choice = acceptance.acceptance_assurance(self.log)
        self.assertEqual((choice['small_estimate'], choice['models']['quality']), (True, JUDGE))
        self.plan(11)
        self.assertFalse(acceptance.estimated_small(self.f.store, self.f.rid))

    def test_only_the_family_of_the_expected_writer_is_dropped(self):
        """The code chain's fallback link (glm) can be the judge as long as it did not write; the real writer drops out dynamically in finalize."""
        self.plan(2)
        self.assertIn(JUDGE, acceptance.acceptance_assurance(self.log)['candidates'])

    def test_assurance_moves_to_the_new_judge_and_the_old_one_is_released(self):
        self.plan(2)
        acceptance.acceptance_assurance(self.log)
        self.assertEqual(self.open_quality(), [('opencode-go', 'glm-5.3')] * 2)
        self.assertTrue(acceptance.assurance_carry(self.f.store, self.f.rid, SOL, self.b, self.log))
        self.assertEqual(self.open_quality(), [('openrouter', 'openai/gpt-6.1-sol')] * 2)
        choice = self.f.store.read(self.f.rid)['acceptance_assurance']
        self.assertEqual((choice['models']['quality'], len(choice['reservations'])), (SOL, 3))

    def test_the_old_assurance_stays_as_is_if_the_new_judge_does_not_fit(self):
        self.plan(2)
        acceptance.acceptance_assurance(self.log)
        once = self.open_quality()
        self.b['or'].update(pay=0.0, available=0.0)
        self.assertFalse(acceptance.assurance_carry(self.f.store, self.f.rid, SOL, self.b, self.log))
        self.assertEqual(self.open_quality(), once)
        self.assertEqual(self.f.store.read(self.f.rid)['acceptance_assurance']['models']['quality'], JUDGE)


class DeadAssuranceTests(unittest.TestCase):
    """4c QA (Fable, 07.10): when the guaranteed judge (glm) was removed as being from the writer/planner family and the move did not hold, the dead guarantee
    held the judge pocket until the terminal. If the old model can no longer be the judge it is released first and the new one is reserved."""

    def test_a_dead_guarantee_is_released_before_the_new_judge_reserves(self):
        import datetime
        import sqlite3
        from contextlib import closing
        import budget_gate as bg
        with tempfile.TemporaryDirectory() as t:
            db = Path(t) / 'chain/request-budget.db'
            ledger = bg.BudgetLedger(db)
            now_ts = datetime.datetime.now()
            instant = {'ts': now_ts.isoformat(), 'today': now_ts.date().isoformat(),
                     'go': {'db': 'ok', 'pay': 2.0, 'available': 2.0, 'window_open': True, 'unit': 'points'}}
            meta = lambda m: {'run_id': 'r', 'model': m, 'assurance': True, 'assurance_role': 'quality', 'phase_role': 'quality'}
            old = [ledger.reserve('opencode-go', 100_000, instant, meta('glm-5.3')) for _ in range(2)]   # 2 x 0,67 points
            carry = ledger.assurance_carry('r', 'quality', 'opencode-go', 100_000, instant, meta('qwen3.8-max'))
            self.assertEqual((carry['old'], len(carry['fresh'])), (old, 2))
            repeated = ledger.assurance_carry('r', 'quality', 'opencode-go', 100_000, instant, meta('qwen3.8-max'), once_leave=True)
            self.assertEqual(repeated['fresh'], carry['fresh'])
            with closing(sqlite3.connect(db)) as con:
                open = con.execute("select json_extract(metadata,'$.model') from reservation where actual is null").fetchall()
            self.assertEqual(sorted(m for (m,) in open), ['qwen3.8-max', 'qwen3.8-max'])
