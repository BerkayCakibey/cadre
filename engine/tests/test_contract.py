"""Item 9 — the task contract. The user's request: AT plan time write a document that splits the budget
across agents/compaction/judge, and make the system FOLLOW it. Until now the split was buried in the code
and not visible per run; there was no single answer to "who may spend how much"."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import unittest

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import budget_gate as budget  # noqa: E402
import contract  # noqa: E402
import test_run_sessions as fixtures  # noqa: E402
from test_budget_period import snapshot  # noqa: E402


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.home = self.f.home
        from run_store import RunStore
        self.store = RunStore(self.home)
        r = self.store.accept('contract', 'test task')
        self.rid = r['run_id']
        self.store.update(self.rid, budget_cap_usd=5.0)

    def test_the_contract_is_written_once_and_never_changes(self):
        plan = {'tasks': [{'id': 'g1', 'title': 'first', 'model': 'opencode-go/glm-5.2', 'difficulty': 3},
                             {'id': 'g2', 'title': 'second'}]}
        first = contract.create(self.home, self.rid, plan)
        self.assertEqual(first['shares'], contract.FALLBACK_SHARES)
        self.assertEqual([s['id'] for s in first['lanes']], ['g1', 'g2'])
        self.assertEqual(first['judge'], {'round': 1, 'revision': 1})
        self.assertEqual(first['source'], 'plan')
        # even if a second call comes with different shares it does NOT CHANGE the existing contract
        second = contract.create(self.home, self.rid, {'tasks': []},
                                  shares={'lanes': 0.9, 'judge': 0.05, 'helper': 0.05})
        self.assertEqual(second['formation'], first['formation'])
        self.assertEqual(second['shares'], contract.FALLBACK_SHARES)

    def request_shares_write(self, shares):
        from run_store import atomic_json
        record = self.store.read(self.rid)
        record['request'] = {'budget_shares': shares, 'duration_minutes': 45, 'budget_usd': '5.0'}
        atomic_json(self.store.path(self.rid) / 'run.json', record)

    def test_request_shares_apply_before_the_contract_and_are_written_into_it(self):
        """CR-08: the proposal gate (without a contract) and the run contract use the same request shares; the total stays fixed."""
        self.request_shares_write({'lanes': 0.71, 'judge': 0.17, 'helper': 0.12})
        self.assertEqual(contract.bases(self.home, self.rid), {'lanes': 0.71, 'judge': 0.17, 'helper': 0.12})
        pockets = {d: budget.role_cap(self.home, self.rid, slice_name=d) for d in ('lanes', 'judge', 'helper')}
        self.assertEqual(pockets, {'lanes': 3_550_000, 'judge': 850_000, 'helper': 600_000})
        self.assertEqual(sum(pockets.values()), 5_000_000)
        s = contract.create(self.home, self.rid, {'tasks': [{'id': 'g1', 'title': 'single'}]})
        self.assertEqual(s['shares'], {'lanes': 0.71, 'judge': 0.17, 'helper': 0.12})
        self.assertEqual(contract.bases(self.home, self.rid), s['shares'])
        self.assertEqual(budget.role_cap(self.home, self.rid, slice_name='helper'), 600_000)

    def test_invalid_request_shares_are_ignored_before_and_in_the_contract(self):
        self.request_shares_write({'lanes': 0.9, 'judge': 0.2, 'helper': 0.1})
        self.assertEqual(contract.bases(self.home, self.rid), contract.FALLBACK_SHARES)
        self.assertEqual(contract.create(self.home, self.rid, {'tasks': []})['shares'], contract.FALLBACK_SHARES)

    def test_invalid_shares_fall_back_to_the_defaults(self):
        for bad in ({'lanes': 0.5, 'judge': 0.2}, {'lanes': 0.5, 'judge': 0.2, 'helper': 0.5},
                     {'lanes': 1.0, 'judge': 0.0, 'helper': 0.0}, 'text', None):
            self.assertFalse(contract.shares_valid(bad), bad)
        s = contract.create(self.home, self.rid, None, shares={'lanes': 2, 'judge': -1, 'helper': 0})
        self.assertEqual(s['shares'], contract.FALLBACK_SHARES)

    def test_a_corrupt_contract_is_ignored_rather_than_obeyed(self):
        contract.path(self.home, self.rid).write_text('{broken')
        self.assertIsNone(contract.read(self.home, self.rid))
        self.assertEqual(contract.pay_ratio(self.home, self.rid, 'impl'), 0.75)
        contract.path(self.home, self.rid).write_text(json.dumps({'version': 99, 'shares': contract.FALLBACK_SHARES}))
        self.assertIsNone(contract.read(self.home, self.rid))

    def test_every_role_maps_to_exactly_one_slice(self):
        self.assertEqual(contract.slice('quality'), 'judge')
        self.assertEqual(contract.slice('review'), 'helper')
        self.assertEqual(contract.slice('cadre-review'), 'helper')
        for role in ('impl', 'plan', 'compaction', '', None, 'unknown'):   # 4c-10: compaction from the lane's pocket
            self.assertEqual(contract.slice(role), 'lanes', role)


class ContractComplianceTests(unittest.TestCase):
    """The contract is not just written, the gate FOLLOWS it."""

    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.home = self.f.home
        from run_store import RunStore
        self.store = RunStore(self.home)
        from unittest.mock import patch
        with patch.dict(os.environ, {'CADRE_RUN_CAP_USD': '5'}):
            self.rid = self.store.accept('contract', 'test')['run_id']
        self.store.update(self.rid, budget_cap_usd=5.0)
        self.ledger = budget.BudgetLedger(self.home / 'chain/request-budget.db')
        self.old = os.environ.get('CADRE_ROLE')
        self.addCleanup(lambda: os.environ.__setitem__('CADRE_ROLE', self.old)
                        if self.old else os.environ.pop('CADRE_ROLE', None))

    def test_each_slice_sees_its_own_ceiling(self):
        contract.create(self.home, self.rid, None)
        # 17.09 HARD SPLIT: no bands, POCKETS. Each role sees only its own share and fills it with its own spending.
        # with a $5 cap: lane $3.75 · helper $0.50 · judge $0.75. They add up to the full cap.
        self.assertEqual(budget.role_cap(self.home, self.rid, 'impl'), 3_750_000)
        self.assertEqual(budget.role_cap(self.home, self.rid, 'review'), 500_000)
        self.assertEqual(budget.role_cap(self.home, self.rid, 'cadre-review'), 500_000)
        self.assertEqual(budget.role_cap(self.home, self.rid, 'quality'), 750_000,
                         'The judge sees its own pocket; unused funds transfer only AFTER LANES FINISH')
        self.assertEqual(3_750_000 + 500_000 + 750_000, 5_000_000)

    def test_lanes_can_never_enter_the_helper_pocket(self):
        """What the guarantee means: the LANES cannot enter the helper's pocket — even if they use up the whole lane share
        the helper passes from its own pocket. With the hard split this is independent of call ORDER."""
        contract.create(self.home, self.rid, None)
        lane, lane_slice = budget.role_budget(self.home, self.rid, 'impl')
        self.ledger.reserve('opencode-go', lane, snapshot(5.0), {'run_id': self.rid},
                            run_cap=lane, slice=lane_slice)
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('opencode-go', 1000, snapshot(5.0), {'run_id': self.rid},
                                run_cap=lane, slice=lane_slice)
        # the helper passes from its own pocket
        cap, slice = budget.role_budget(self.home, self.rid, 'review')
        self.ledger.reserve('opencode-go', 400_000, snapshot(5.0), {'run_id': self.rid},
                            run_cap=cap, slice=slice)

    def test_the_role_comes_from_the_environment_when_not_given(self):
        contract.create(self.home, self.rid, None)
        os.environ['CADRE_ROLE'] = 'review'
        self.assertEqual(budget.role_cap(self.home, self.rid), 500_000)
        self.assertEqual(budget.role_budget(self.home, self.rid)[1], 'helper')
        os.environ['CADRE_ROLE'] = 'quality'
        self.assertEqual(budget.role_cap(self.home, self.rid), 750_000)
        self.assertEqual(budget.role_budget(self.home, self.rid)[1], 'judge')

    def test_without_a_contract_the_defaults_still_apply(self):
        self.assertIsNone(contract.read(self.home, self.rid))
        self.assertEqual(budget.role_cap(self.home, self.rid, 'impl'), 3_750_000)
        self.assertEqual(budget.role_cap(self.home, self.rid, 'review'), 500_000)


class JudgeKindTests(unittest.TestCase):
    """The contract also binds the judge round count: one review + one revision. The code now reads this from the contract."""

    def test_the_default_contract_means_one_review_and_one_revision(self):
        self.assertEqual(contract.FALLBACK_SHARES['judge'], 0.15)
        import tempfile
        from run_store import RunStore
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            (home / 'projects/p').mkdir(parents=True)
            (home / 'projects/p/repo-path').write_text(str(home))
            rid = RunStore(home).accept('p', 'task')['run_id']
            s = contract.create(home, rid, None)
            self.assertEqual(s['judge']['round'] + s['judge']['revision'], 2)


if __name__ == '__main__':
    unittest.main()


class MergePocketTest(unittest.TestCase):
    def test_merge_spends_from_the_remaining_pocket_when_lanes_finish(self):
        """03.10 02:37 live: when all lanes were terminal the remaining share was handed to the judge (B82) and the lane pocket froze at what was spent;
        since the merge spent from the LANE pocket it ran out of money on every run and fell to local. The merge runs after the lanes."""
        import contract
        self.assertEqual(contract.task_slice('', 'merge'), 'judge')
        self.assertEqual(contract.task_slice('', 'g1'), 'lanes')
        # B335: QA repair (the rescuer role) runs in the acceptance task; the frozen lane pocket closed all paid ones
        self.assertEqual(contract.task_slice('recovery', 'quality'), 'judge')
        self.assertEqual(contract.task_slice('recovery', 'qa'), 'judge')
        self.assertEqual(contract.task_slice('recovery', 'g2'), 'lanes')
