"""Budget slices — the possibility matrix.

Decision of 17.09: HARD SPLIT. Each role spends only from its own pocket and its pocket fills with its own spending; the call
order does not change the outcome. Judge 15% (at least $0.20, at most half the cap) · helper 10% · lanes the remainder.
The only exception: when all lanes are terminal, the remaining share goes to the judge.

This file asks the real gate about every combination and writes the result to the `docs/SLICE-MATRIX-20260917.md` table:
`python3 engine/tests/test_slice_matrix.py --report`"""

from __future__ import annotations

import datetime
from pathlib import Path
import sys
import unittest

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import budget_gate as budget  # noqa: E402
import contract  # noqa: E402
import test_run_sessions as fixtures  # noqa: E402

LINES: list[tuple] = []


def divide(**extra):
    now = datetime.datetime.now()
    return {'ts': now.isoformat(), 'today': now.date().isoformat(),
            'go': {'db': 'ok', 'pay': 666.666667, 'available': 666.666667, 'window_open': True, 'unit': 'points'}, **extra}


class SliceMatrixTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.home = self.f.home
        self.ledger = budget.BudgetLedger(self.home / 'chain/request-budget.db')

    def execute_run(self, cap, contracted=True):
        from run_store import RunStore
        from unittest.mock import patch
        store = RunStore(self.home)
        with patch.dict('os.environ', {'CADRE_RUN_CAP_USD': str(cap)}):
            rid = store.accept('matrix', 'task')['run_id']
        store.update(rid, budget_cap_usd=cap)
        if contracted:
            contract.create(self.home, rid, None)
        return rid

    def cap(self, rid, role):
        return budget.role_cap(self.home, rid, role)

    def try_reserve(self, rid, role, micro):
        """Can this role set aside this amount? (accept/deny). The slice is written to the metadata — a condition of the hard split."""
        cap, slice = budget.role_budget(self.home, rid, role)
        try:
            self.ledger.reserve('opencode-go', micro, divide(), {'run_id': rid}, run_cap=cap, slice=slice)
            return 'accepted'
        except budget.BudgetDenied:
            return 'denied'

    def save(self, status, role, expected, observed):
        LINES.append((status, role, expected, observed, expected == observed))
        self.assertEqual(observed, expected, f'{status} / {role}')

    # --- 1: each role's cap in an empty run -----------------------------------------------------
    def test_caps_in_an_empty_run(self):
        for cap, wait in ((5.0, {'impl': 3_750_000, 'quality': 750_000, 'review': 500_000}),
                           (1.0, {'impl': 700_000, 'quality': 200_000, 'review': 100_000}),
                           (0.05, {'impl': 20_000, 'quality': 25_000, 'review': 5_000})):
            rid = self.execute_run(cap)
            for role, value in wait.items():
                self.save(f'empty run, cap ${cap}', role, value, self.cap(rid, role))

    # --- 2-4: CALL ORDER must not matter (the user's question) ----------------------------
    def _all_items_share_spend(self, rid, role):
        return self.try_reserve(rid, role, self.cap(rid, role))

    def test_whichever_role_is_called_first_the_others_do_not_starve(self):
        """User question: 'if a helper or the judge is called before a lane, won't the lane lose out?'
        With the hard split, NO: each role spends only from its own pocket, the call order does not change the outcome."""
        import itertools
        roles = ['impl', 'review', 'quality']
        for order in itertools.permutations(roles):
            rid = self.execute_run(5.0)
            outcome = [self._all_items_share_spend(rid, r) for r in order]
            self.save(f'order: {" → ".join(order)}', 'all three spent their own allocations',
                        ['accepted', 'accepted', 'accepted'], outcome)

    def test_when_a_role_exhausts_its_share_only_itself_is_rejected(self):
        for victim in ('impl', 'review', 'quality'):
            rid = self.execute_run(5.0)
            self._all_items_share_spend(rid, victim)
            self.save(f'{victim} exhausted its allocation', victim, 'denied', self.try_reserve(rid, victim, 1000))
            for other in ('impl', 'review', 'quality'):
                if other == victim:
                    continue
                self.save(f'{victim} exhausted its allocation', other, 'accepted', self.try_reserve(rid, other, 50_000))

    def test_the_sum_of_the_shares_is_exactly_the_cap(self):
        for cap in (5.0, 1.0, 0.05):
            rid = self.execute_run(cap)
            total = sum(self.cap(rid, r) for r in ('impl', 'review', 'quality'))
            self.save(f'cap ${cap} total allocations', 'total', int(cap * 1e6), total)

    # --- 4b: HAND-OFF — when the lanes finish, the remaining share goes to the judge -------------------------------
    def test_no_handover_before_the_lanes_finish(self):
        """A hand-off happens only when the handing role's work is DONE; otherwise it leaves the one called first hungry."""
        rid = self.execute_run(5.0)
        self.save('lanes running', 'judge cap', 750_000, self.cap(rid, 'quality'))

    def test_correction_round_spends_from_the_acceptance_pocket_after_handover(self):
        """G-C16 + B82 (24.09 R3-hard finding): the correction round runs AFTER the lanes are DONE; the lane pocket is frozen.
        Round 2's cloud and the strong model of a hard+ clean restart can only spend from the handed-off money (the acceptance pocket)."""
        import json
        import os
        from unittest.mock import patch
        from run_store import RunStore
        rid = self.execute_run(5.0)
        path = contract.path(self.home, rid)
        data = json.loads(path.read_text())
        data['lanes'] = [{'id': 'g1', 'title': '', 'model': '', 'difficulty': 2}]
        path.write_text(json.dumps(data))
        self.try_reserve(rid, 'impl', 1_000_000)
        RunStore(self.home).update(rid, tasks={'g1': {'state': 'produced'}})
        with patch.dict(os.environ, {'CADRE_TASK_ID': 'g1'}):
            self.save('ordinary lane after handover', 'impl', 'denied', self.try_reserve(rid, 'impl', 1000))
        with patch.dict(os.environ, {'CADRE_TASK_ID': 'correction-2'}):
            self.assertEqual(budget.role_budget(self.home, rid, 'impl')[1], 'judge')
            self.save('correction round after handover', 'impl', 'accepted', self.try_reserve(rid, 'impl', 500_000))
            self.assertGreater(budget.run_remaining_usd(self.home, rid, 'impl'), 2.5)
        with patch.dict(os.environ, {'CADRE_TASK_ID': 'correction-again'}):
            self.save('clean restart after handover', 'recovery', 'accepted', self.try_reserve(rid, 'recovery', 500_000))
        self.save('correction spending reduces the judge allocation', 'judge cap', 3_500_000, self.cap(rid, 'quality'))

    def test_when_lanes_finish_the_remainder_is_handed_to_the_judge(self):
        rid = self.execute_run(5.0)
        from run_store import RunStore
        store = RunStore(self.home)
        # make the lanes in the contract terminal (without a plan the contract writes an empty lane; set up manually)
        import contract
        path = contract.path(self.home, rid)
        data = __import__('json').loads(path.read_text())
        data['lanes'] = [{'id': 'g1', 'title': '', 'model': '', 'difficulty': 2}]
        path.write_text(__import__('json').dumps(data))
        self.save('lanes unfinished', 'judge cap', 750_000, self.cap(rid, 'quality'))
        # the lane spends first, THEN finishes — this is the real order
        self.try_reserve(rid, 'impl', 1_000_000)
        store.update(rid, tasks={'g1': {'state': 'produced'}})
        # B82: the hand-off does not eat the HELPER's guarantee — judge = total − lane spending − helper share.
        self.save('lanes finished, $1.00 spent', 'judge cap', 3_500_000, self.cap(rid, 'quality'))
        # ... and the lane pocket FREEZES where it spent: after the hand-off the lane cannot spend another cent
        self.save('lanes finished, $1.00 spent', 'lane cap (frozen)', 1_000_000, self.cap(rid, 'impl'))
        self.save('new lane spending after handover', 'impl', 'denied', self.try_reserve(rid, 'impl', 1000))
        self.save('helper uses its own allocation after handover', 'review', 'accepted', self.try_reserve(rid, 'review', 400_000))

    def test_no_handover_while_the_report_extra_lane_runs(self):
        """B226 (25.09 live R12-report): the hard report's second analyst and reconciler run AFTER the writer lane 'produced';
        the hand-off froze the lane pocket at that moment, so the plan chain was empty ('no second strong analyst'). While the dispatcher's extra-lane
        marker stays, the lane pocket stays open; when the marker is removed the hand-off works normally."""
        rid = self.execute_run(5.0)
        from run_store import RunStore
        store = RunStore(self.home)
        import contract
        path = contract.path(self.home, rid)
        data = __import__('json').loads(path.read_text())
        data['lanes'] = [{'id': 'g1', 'title': '', 'model': '', 'difficulty': 2}]
        path.write_text(__import__('json').dumps(data))
        self.try_reserve(rid, 'impl', 1_000_000)
        store.update(rid, tasks={'g1': {'state': 'produced'}})
        mark = store.path(rid) / 'extra-lane.flag'
        mark.write_text('g1\n')
        self.save('extra lane running', 'plan', 'accepted', self.try_reserve(rid, 'plan', 500_000))
        self.save('extra lane running', 'judge cap', 750_000, self.cap(rid, 'quality'))
        mark.unlink()
        self.save('extra lane finished', 'judge cap', 3_000_000, self.cap(rid, 'quality'))
        self.save('extra lane finished', 'plan', 'denied', self.try_reserve(rid, 'plan', 1000))

    def test_the_sum_of_pockets_never_exceeds_the_cap(self):
        """B82 INVARIANT: SUM(pockets) <= cap at all times. The hand-off broke this invariant — measured: a run with a $5 cap
        could spend $8.10 (lane $1.00 -> judge $3.90 -> helper $0.50 -> QA repair $2.70)."""
        from run_store import RunStore
        import json as _json
        import contract
        for cap in (5.0, 1.0, 0.05):
            rid = self.execute_run(cap)
            path = contract.path(self.home, rid)
            data = _json.loads(path.read_text())
            data['lanes'] = [{'id': 'g1', 'title': '', 'model': '', 'difficulty': 2}]
            path.write_text(_json.dumps(data))
            for spend in (0, int(cap * 1e6) // 4, int(cap * 1e6) // 2):
                for finished in (False, True):
                    rid2 = self.execute_run(cap)
                    y2 = contract.path(self.home, rid2)
                    v2 = _json.loads(y2.read_text())
                    v2['lanes'] = [{'id': 'g1', 'title': '', 'model': '', 'difficulty': 2}]
                    y2.write_text(_json.dumps(v2))
                    if spend:
                        self.try_reserve(rid2, 'impl', spend)
                    if finished:
                        RunStore(self.home).update(rid2, tasks={'g1': {'state': 'produced'}})
                    total_pocket = sum(self.cap(rid2, r) for r in ('impl', 'review', 'quality'))
                    self.save(f'cap ${cap} · lane ${spend/1e6:.2f} · finished={finished}',
                                'total allocations ≤ cap', True, total_pocket <= int(cap * 1e6))

    # --- 5: default without a contract --------------------------------------------------------
    def test_run_without_a_contract_uses_the_default(self):
        rid = self.execute_run(5.0, contracted=False)
        for role, value in (('impl', 3_750_000), ('quality', 750_000), ('review', 500_000)):
            self.save('no contract', role, value, self.cap(rid, role))

    # --- 6: an unknown role counts as a lane -----------------------------------------------------
    def test_unknown_role_is_in_the_lane_slice(self):
        rid = self.execute_run(5.0)
        for role in ('', 'made_up', 'plan', 'autopsy'):
            self.save('unknown/phase role', role or '(empty)', 3_750_000, self.cap(rid, role))
        # B332/B335 (03.10): QA repair is post-lane work — it spends from the judge pocket, not from the frozen lane pocket.
        self.save('lane-after role', 'qa', 750_000, self.cap(rid, 'qa'))

    # --- 7: run without a cap ---------------------------------------------------------------------
    def test_acceptance_writes_the_default_cap(self):
        """`accept` writes the default cap to every run ($2, B199 user decision 23.09); there is no such thing as a 'run without a cap'."""
        from run_store import RunStore
        store = RunStore(self.home)
        rid = store.accept('matrix', 'task')['run_id']
        for role, value in (('impl', 1_500_000), ('quality', 300_000), ('review', 200_000)):
            self.save('no cap written → default $2', role, value, self.cap(rid, role))


def report():
    target = Path(__file__).resolve().parents[2] / 'docs/SLICE-MATRIX-20260917.md'
    s = ['# Budget allocations — scenario matrix', '',
         '**Strict allocation:** every role spends ONLY its own allocation, which tracks its own spending.',
         'Judge 15% (at least $0.20, at most half the cap) · helper 10% · lanes receive the remainder.',
         'Call ORDER does not change the result; roles cannot spend each other’s allocations.',
         '**Invariant: Σ(pockets) ≤ cap at all times.** When all lanes are terminal, their remaining allocation',
         '**transfers** to the judge while lane allocations **freeze** at their spent amounts; otherwise the total exceeded the cap',
         '(measured: a run capped at $5 spent $8.10). The helper’s guarantee is preserved during handover.',
         'The enforced cap and the allocation charged both come from one decision: `role_budget(home, run_id, role)`.', '',
         'Reproduce with: `python3 engine/tests/test_slice_matrix.py --report`', '',
         '| status | role | expected | observed | ✓ |', '|---|---|---|---|---|']
    for status, role, wait, eye, ok in LINES:
        f = lambda v: ('—' if v is None else (f'${v/1e6:.3f}' if isinstance(v, int) else str(v)))
        s.append(f'| {status} | `{role}` | {f(wait)} | {f(eye)} | {"✅" if ok else "❌"} |')
    s += ['', f'**{sum(1 for *_, ok in LINES if ok)}/{len(LINES)} rows matched expectations.**', '',
          '## Reading guide',
          '- **STRICT ALLOCATION**: each role spends only its own allocation. Allocations sum exactly to the cap.',
          '- **Call order does not change the result.** All three roles could spend their allocations in all six orders.',
          '- **Only the role that exhausts its allocation is rejected**; other roles are unaffected.',
          '- Cost: unused allocations remain idle. Measurements make this inexpensive; in run 2 no role used',
          '  even half its allocation (lane $0.71/$3.75, judge $0.29/$0.75).',
          '- **No cap written** → `accept` writes the default $5; runs are never uncapped.']
    target.write_text('\n'.join(s) + '\n', encoding='utf-8')
    print('report:', target)


if __name__ == '__main__':
    do_report = '--report' in sys.argv
    if do_report:
        sys.argv.remove('--report')
    outcome = unittest.main(exit=False, verbosity=0)
    if do_report:
        report()
    sys.exit(0 if outcome.result.wasSuccessful() else 1)


class PreCheckPocketTests(unittest.TestCase):
    """B112 (19.09, two independent audits pointed at the same place): `available_budget`/`run_remaining_usd`
    TAKE a role parameter but it was not passed on the pre-check paths. When the role drops the pocket becomes `lanes`
    (`contract.slice('') == 'lanes'`), while the real reservation at the gate is made from the judge pocket.
    Result: when the lane pocket ran out the judge dropped out of the chain with 'no verified share' while its own money was still there."""

    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.home = self.home_ = self.f.home
        self.ledger = budget.BudgetLedger(self.home / 'chain/request-budget.db')

    def _lane_pocket_finish(self, cap=5.0):
        from run_store import RunStore
        from unittest.mock import patch
        with patch.dict('os.environ', {'CADRE_RUN_CAP_USD': str(cap)}):
            rid = RunStore(self.home).accept('b112', 'task')['run_id']
        RunStore(self.home).update(rid, budget_cap_usd=cap)
        contract.create(self.home, rid, None)
        ceiling, slice = budget.role_budget(self.home, rid, 'impl')
        self.ledger.reserve('opencode-go', ceiling, divide(), {'run_id': rid}, run_cap=ceiling, slice=slice)
        return rid

    def test_the_chain_for_a_referee_role_keeps_paid_links_when_only_the_lane_pocket_is_empty(self):
        import os
        import model_router as router
        rid = self._lane_pocket_finish()
        b = divide(cerebras={'db': 'ok', 'pay': 5.0, 'available': 5.0},
                zen={'db': 'ok', 'pay': 5.0, 'available': 5.0},
                ds={'api': 'ok', 'pay': 5.0, 'available': 5.0}, **{'or': {'db': 'ok', 'pay': 5.0, 'available': 5.0, 'remaining': 5.0}})
        import json
        import time
        (self.home / 'chain/openrouter-price.json').write_text(json.dumps({'version': 2, 'ts': time.time(), 'models': {
            'openai/gpt-6.1-sol': {'input': 1, 'output': 5, 'providers': ['openai'], 'fp8_cache': True}}}))
        old = dict(os.environ)
        os.environ.update(CADRE_HOME=str(self.home), CADRE_RUN_ID=rid)
        try:
            os.environ.pop('CADRE_CHAIN_QUALITY', None)  # This test checks the default GPT path, not the fixture pool.
            router.QE = self.home
            quality, reasons = router.chain('quality', 'middle', 8000, b, {})
        finally:
            os.environ.clear(); os.environ.update(old)
        paid = [m for m in quality if not m.startswith('lmstudio/')]
        self.assertTrue(paid, f'the chain is empty despite available judge budget; reasons: {reasons}')
        self.assertEqual(paid, [router.OR_SOL], 'The final fallback judge is the measured GPT/FLEX route')
        self.assertFalse(any('no verified allocation' in n for n in reasons),
                         f'judge fallbacks were eliminated because the lane allocation was exhausted: {reasons}')

    def test_budget_exists_asks_the_pocket_of_the_role_it_is_called_for(self):
        import os
        import model_router as router
        rid = self._lane_pocket_finish()
        b = divide(zen={'db': 'ok', 'pay': 5.0, 'available': 5.0})
        old = dict(os.environ)
        os.environ.update(CADRE_HOME=str(self.home), CADRE_RUN_ID=rid)
        try:
            router.QE = self.home
            lane = router.has_budget('opencode-go', 'opencode-go/glm-5.3', b, 8000, role='impl')
            judge = router.has_budget('opencode-go', 'opencode-go/glm-5.3', b, 8000, role='quality')
            self.assertTrue(router.has_budget('opencode', 'opencode/glm-5.3', b, 8000, role='impl')[0])
        finally:
            os.environ.clear(); os.environ.update(old)
        self.assertEqual(lane, (False, 'no verified allocation'), 'the lane allocation is exhausted, so the lane role must be rejected')
        self.assertTrue(judge[0], f'the judge has budget and must be accepted; returned: {judge}')

    def test_the_claude_call_cap_comes_from_the_pocket_of_its_own_role(self):
        """The second face of B112 + the 19.09 mutation audit: when `qe_core:358` dropped the role it ESCAPED (no test
        broke). In a run with the lane pocket exhausted and the judge pocket full, the judge role's call cap must not be 0."""
        import os
        import budget_gate as budget2
        rid = self._lane_pocket_finish()
        old = dict(os.environ)
        os.environ.update(CADRE_HOME=str(self.home), CADRE_RUN_ID=rid)
        try:
            lane = budget2.run_remaining_usd(self.home, rid, 'impl')
            judge = budget2.run_remaining_usd(self.home, rid, 'quality')
            roleless = budget2.run_remaining_usd(self.home, rid)
        finally:
            os.environ.clear(); os.environ.update(old)
        self.assertLessEqual(lane, 0, 'The lane pocket must be exhausted by installation')
        self.assertGreater(judge, 0, 'The funded judge must see its own cap')
        self.assertEqual(roleless, lane, 'A roleless call sees the LANE pocket, so a role must be passed')

    def test_run_claude_asks_for_the_cap_with_its_role(self):
        """The difference above helps only if `run_claude` PASSES the role. Is the call really made with the role:
        we wrap `run_remaining_usd` and record which role it was called with (not a source scan)."""
        import os
        from unittest.mock import patch
        import qe_core as core
        rid = self._lane_pocket_finish()
        calls = []

        def fake(home, run_id, role=None, provider=''):
            calls.append(role)
            self.assertEqual(provider, 'claude')
            return 5.0

        old = dict(os.environ)
        os.environ.update(CADRE_HOME=str(self.home), CADRE_RUN_ID=rid)
        try:
            import claude_policy as cp
            with patch.object(core, 'QE', self.home), \
                 patch.object(core, 'budget_json', return_value=divide()), \
                 patch.object(core, 'available_budget', return_value=1_000_000), \
                 patch.object(core, 'run_remaining_usd', side_effect=fake), \
                 patch.object(cp, 'difficulty', return_value=2), \
                 patch.object(cp, 'fable_slots', return_value=0), \
                 patch.object(cp, 'model', return_value='claude/sonnet#high'), \
                 patch.object(core, 'Attempt', side_effect=RuntimeError('stop')):
                try:
                    core.run_claude(str(self.home), 'claude/sonnet#high', 'task', self.home / 'log.txt', role='quality')
                except Exception:
                    pass
        finally:
            os.environ.clear(); os.environ.update(old)
        self.assertIn('quality', calls, f'A call cap was requested without a role: {calls}')
