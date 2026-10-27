"""17.09 live measurement: the run cap was set to $0.05, the run was retried 12 times over **159 minutes**,
hit the same "run budget exhausted" wall every time and finally failed.

The run cap does NOT GROW within a run; retrying with the same denial only burns time. These tests prove that a run with a full cap
fails fast and with an explanation, while a run whose cap is not full is retried as before."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from run_store import run_cap_exhausted  # noqa: E402

RED = [{'provider': 'claude', 'reason': 'run budget exhausted: reservation $1.25 > remaining $0.05'}]


class CapExhaustedTests(unittest.TestCase):
    def test_a_cap_that_is_spent_is_permanent(self):
        self.assertTrue(run_cap_exhausted({'denials': RED, 'budget_cap_usd': 0.05, 'budget_spent_usd': 0.05}))
        self.assertTrue(run_cap_exhausted({'denials': RED, 'budget_cap_usd': 5.0, 'budget_spent_usd': 4.6}))

    def test_a_cap_with_room_left_is_still_retryable(self):
        self.assertFalse(run_cap_exhausted({'denials': RED, 'budget_cap_usd': 5.0, 'budget_spent_usd': 0.3}))

    def test_without_a_cap_denial_nothing_changes(self):
        self.assertFalse(run_cap_exhausted({'denials': [{'provider': 'go', 'reason': 'cooldown'}],
                                              'budget_cap_usd': 0.05, 'budget_spent_usd': 0.05}))
        self.assertFalse(run_cap_exhausted({'budget_cap_usd': 0.05, 'budget_spent_usd': 0.05}))

    def test_a_tiny_remainder_counts_as_exhausted(self):
        # $0.05 cap, $0.036 spent: $0.014 remaining — not enough for even a single call
        self.assertTrue(run_cap_exhausted({'denials': RED, 'budget_cap_usd': 0.05, 'budget_spent_usd': 0.036}))

    def test_broken_numbers_never_crash_the_watchdog(self):
        for bad in ({'budget_cap_usd': 'abc'}, {'budget_cap_usd': None}, {'budget_cap_usd': 0}):
            self.assertFalse(run_cap_exhausted({'denials': RED, **bad}))



class RealRunTests(unittest.TestCase):
    """17.09 AUDIT: the tests above measured the guard with HAND-PICKED numbers and never tried the real run that
    MOTIVATED the guard (20260917-121533). In that run the cap was $0.05 and $0.003625 was spent:
    both the "90% full" and the "remaining < $0.02" rules were wrong, the guard returned False and the run was retried twice
    more, burning 159 minutes. These tests close that hole."""

    # denial lines taken VERBATIM from run.json (denials[0], [3], [7])
    REAL_REJECTS = [
        {'provider': 'claude', 'reason': 'run budget exhausted: reservation $1.25 > remaining $0.05 '
                                         '(cap $0.05, settled $0.00, held $0.00)'},
        {'provider': 'opencode-go', 'reason': 'run budget exhausted: reservation $0.53 > remaining $0.03 '
                                              '(cap $0.03, settled $0.00, held $0.00)'},
        {'provider': 'cerebras', 'reason': 'run budget exhausted: reservation $0.02 > remaining $0.02 '
                                           '(cap $0.03, settled $0.00, held $0.00)'},
    ]

    def test_the_run_that_motivated_the_guard_is_detected(self):
        real = {'denials': self.REAL_REJECTS, 'budget_cap_usd': 0.05,
                  'budget_spent_usd': 0.003625, 'budget_held_usd': 0.0}
        self.assertTrue(run_cap_exhausted(real),
                        'A denied over-cap request cannot raise the cap; retrying hits the same boundary')

    def test_a_denial_that_fits_the_cap_stays_retryable(self):
        # The requested amount is BELOW the cap: it may have been denied because of another pending call at the time,
        # room opens when that call closes. This run must keep being retried.
        temporary = [{'provider': 'cerebras', 'reason': 'run budget exhausted: reservation $0.02 > remaining $0.01 '
                                                     '(cap $5.00, settled $0.10, held $4.89)'}]
        self.assertFalse(run_cap_exhausted({'denials': temporary, 'budget_cap_usd': 5.0,
                                              'budget_spent_usd': 0.10, 'budget_held_usd': 0.0}))

    def test_held_money_counts_as_spent(self):
        # Pending money (outcome unknown) counts as spent at the gate; the guard must do the same calculation.
        self.assertTrue(run_cap_exhausted({'denials': [{'reason': 'run budget exhausted: reservation $0.10 '
                                                                    '> remaining $0.00 (cap $5.00)'}],
                                             'budget_cap_usd': 5.0, 'budget_spent_usd': 0.2,
                                             'budget_held_usd': 4.6}))

    def test_the_denial_text_is_produced_by_the_real_gate(self):
        """Hand-writing the denial text is a tautology: the text is produced by the REAL gate, written to the run record and
        it is measured that the guard recognizes it. If the gate's f-string changes this test breaks — that is the intent."""
        import datetime
        import os
        import tempfile

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
        from budget_gate import BudgetDenied, BudgetLedger      # noqa: E402
        from run_store import RunStore                                # noqa: E402

        with tempfile.TemporaryDirectory() as t:
            home_dir = Path(t)
            old = os.environ.get('CADRE_HOME')
            os.environ['CADRE_HOME'] = str(home_dir)
            try:
                store = RunStore(home_dir)
                record = store.accept('audit', 'small task')
                rid = record['run_id']
                store.update(rid, budget_cap_usd=0.05)
                observation = {'ts': datetime.datetime.now().isoformat(),
                          'today': datetime.date.today().isoformat(),
                          'go': {'db': 'ok', 'pay': 60.0, 'available': 60.0,
                                 'window_open': True, 'unit': 'points'}}
                ledger = BudgetLedger(home_dir / 'chain/request-budget.db')
                with self.assertRaises(BudgetDenied) as red:
                    ledger.reserve('opencode-go', 1_250_000, observation, {'run_id': rid}, run_cap=50_000)
                record = store.read(rid)
                record['denials'] = [{'provider': 'opencode-go', 'reason': str(red.exception)}]
                self.assertIn('run budget exhausted', record['denials'][0]['reason'])
                self.assertTrue(run_cap_exhausted(record),
                                f"The guard did not recognize the actual gate denial: {record['denials'][0]['reason']}")
            finally:
                os.environ.pop('CADRE_HOME', None) if old is None else os.environ.__setitem__('CADRE_HOME', old)

    def test_a_calibrated_denial_names_the_worst_case_amount_the_cap_was_compared_with(self):
        import datetime
        import os
        import re
        import tempfile
        from unittest.mock import patch

        from budget_gate import BudgetDenied, BudgetLedger      # noqa: E402
        from run_store import RunStore                                # noqa: E402

        with tempfile.TemporaryDirectory() as t:
            home_dir = Path(t)
            with patch.dict(os.environ, {'CADRE_HOME': str(home_dir)}), \
                    patch.object(BudgetLedger, 'calibrated', lambda self, con, provider, maximum: maximum // 10):
                store = RunStore(home_dir)
                rid = store.accept('audit', 'small task')['run_id']
                store.update(rid, budget_cap_usd=1.0)
                observation = {'ts': datetime.datetime.now().isoformat(), 'today': datetime.date.today().isoformat(),
                          'go': {'db': 'ok', 'pay': 60.0, 'available': 60.0, 'window_open': True, 'unit': 'points'}}
                with self.assertRaises(BudgetDenied) as red:
                    BudgetLedger(home_dir / 'chain/request-budget.db').reserve('opencode-go', 1_250_000, observation, {'run_id': rid},
                                                                          run_cap=1_000_000)
                text = str(red.exception)
                requested, remaining = (float(x) for x in re.search(r'reservation \$([0-9.]+) > remaining \$([0-9.]+)', text).groups())
                self.assertEqual((requested, remaining), (1.25, 1.0), text)
                self.assertIn('calibrated $0.12', text)
                record = {**store.read(rid), 'denials': [{'provider': 'opencode-go', 'reason': text}]}
                self.assertTrue(run_cap_exhausted(record), text)


class ExecutorChecksCapTests(unittest.TestCase):
    """Second live defect: the executor environment only looked at the PROVIDER daily share. In a run with a $0.05 run cap,
    since the provider shares were plentiful, sub-agents were assigned a CLOUD model; then every call hit the run
    cap with CADRE_BUDGET_DENIED and the whole lane failed. Now the remaining cap is checked too."""

    def test_an_exhausted_cap_closes_the_cloud_before_any_call(self):
        import os
        import tempfile
        import importlib
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as t:
            home_dir = Path(t)
            (home_dir / 'projects/p').mkdir(parents=True)
            (home_dir / 'projects/p/repo-path').write_text(str(home_dir))
            old = {k: os.environ.get(k) for k in ('CADRE_HOME', 'CADRE_RUN_ID')}
            os.environ['CADRE_HOME'] = str(home_dir)
            try:
                import qe_core
                importlib.reload(qe_core)
                from run_store import RunStore
                store = RunStore(home_dir)
                rid = store.accept('p', 'task')['run_id']
                store.update(rid, budget_cap_usd=0.05)   # not an immutable field: may be updated after accept
                os.environ['CADRE_RUN_ID'] = rid
                budget = {'go': {'db': 'ok', 'pay': 60.0, 'available': 60.0, 'window_open': True, 'unit': 'points'}}
                with patch.object(qe_core, 'budget_json', return_value=budget), \
                     patch.object(qe_core, 'run_remaining_usd', return_value=0.001):
                    env = {}
                    # P3 (03.10): with the cap exhausted, BudgetDenied at the gate — no fall to local, no cloud call.
                    with self.assertRaises(qe_core.BudgetDenied):
                        qe_core.opencode_budget_env(env, ['-m', 'opencode-go/kimi-k2.7-code', 'task'])
            finally:
                for k, v in old.items():
                    os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


class RolePocketOnProductionPathTests(unittest.TestCase):
    """B81 (17.09 second audit): the executor environment read the role from the OS environment, but the role is only written into the CHILD
    environment. Result: when the lanes used up their share, the judge and the helper fell to local WHILE THEIR OWN MONEY
    WAS STILL THERE. This test goes through the PRODUCTION PATH (role_environment -> opencode_budget_env), not the helper function
    — lesson 9c: if an invariant is read from two places, test it through the production path."""

    def test_the_reviewer_reaches_the_cloud_while_the_lanes_pocket_is_empty(self):
        import os
        import test_run_sessions as fixtures
        import budget_gate as budget
        import contract
        import qe_core
        from unittest.mock import patch
        from run_store import RunStore
        from test_budget_scenarios import instant
        f = fixtures.SessionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        rid = RunStore(f.home).accept('b81', 'task')['run_id']
        RunStore(f.home).update(rid, budget_cap_usd=1.0)
        contract.create(f.home, rid, None)
        instant_data = instant(go={'db': 'ok', 'pay': 100.0, 'available': 100.0, 'window_open': True})
        ledger = budget.BudgetLedger(f.home / 'chain/request-budget.db')
        cap, slice = budget.role_budget(f.home, rid, 'impl')
        ledger.reserve('opencode-go', cap, instant_data, {'run_id': rid}, run_cap=cap, slice=slice)

        old = {k: os.environ.get(k) for k in ('CADRE_HOME', 'CADRE_RUN_ID', 'CADRE_ROLE')}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in old.items()])
        os.environ['CADRE_HOME'] = str(f.home)
        os.environ['CADRE_RUN_ID'] = rid
        os.environ.pop('CADRE_ROLE', None)          # there is NO role in the engine's own environment — the real situation

        selected = {}
        with patch.object(qe_core, 'budget_json', return_value=instant_data):
            for role in ('quality', 'review'):   # 4c-10: compaction from the lane's pocket; review uses the helper pocket
                env = qe_core.role_environment(dict(os.environ), role)
                args = qe_core.opencode_budget_env(env, ['-m', 'opencode-go/kimi-k2.7-code', 'task'])
                selected[role] = args[args.index('-m') + 1]
            # P3 (03.10): when the lane pocket runs out, BudgetDenied at the gate — no fall to local, it waits.
            env = qe_core.role_environment(dict(os.environ), 'impl')
            with self.assertRaises(qe_core.BudgetDenied):
                qe_core.opencode_budget_env(env, ['-m', 'opencode-go/kimi-k2.7-code', 'task'])

        self.assertFalse(selected['quality'].startswith('lmstudio/'),
                         'The judge can use cloud capacity while its own pocket is funded')
        self.assertFalse(selected['review'].startswith('lmstudio/'),
                         'The helper can use cloud capacity while its own pocket is funded')


class PhantomPendingTests(unittest.TestCase):
    """B84: if the coordinator dies mid-call the reservation stays pending; `run_cap_exhausted` counts it as
    spent and the revival fails permanently with "cap full, raise the cap". But the pending amount is a safe
    UPPER BOUND (measured: $2.78 reserved -> $0.32 receipt), so the money may not have been spent. The right tool
    is `cadre recover`, not raising the cap."""

    def record(self, cap, settled, pending):
        return {'budget_cap_usd': cap, 'budget_spent_usd': settled, 'budget_held_usd': pending,
                'denials': [{'reason': 'run budget exhausted: reservation $0.01 > remaining $0.00'}]}

    def test_a_cap_that_is_full_only_because_of_holds_is_reported_as_such(self):
        from run_store import cap_is_phantom_from_pending
        # $5 cap, real spending $0.32, pending $4.50 -> the cap looks FULL but the money was not spent
        self.assertTrue(run_cap_exhausted(self.record(5.0, 0.32, 4.50)))
        self.assertTrue(cap_is_phantom_from_pending(self.record(5.0, 0.32, 4.50)))

    def test_a_genuinely_spent_cap_is_not_blamed_on_holds(self):
        from run_store import cap_is_phantom_from_pending
        self.assertTrue(run_cap_exhausted(self.record(5.0, 4.90, 0.0)))
        self.assertFalse(cap_is_phantom_from_pending(self.record(5.0, 4.90, 0.0)))
        self.assertFalse(cap_is_phantom_from_pending(self.record(5.0, 4.60, 0.30)),
                         'When settled spending alone exhausts the cap, pending reservations are not responsible')

    def _revive_reason(self, cap, spent, pending):
        """Really RUNS the guard and reads the `reason` text written to the run record (not a source scan)."""
        import tempfile
        from run_store import RunStore, revive
        with tempfile.TemporaryDirectory(prefix='qe-cap-') as t:
            home_dir = Path(t)
            store = RunStore(home_dir)
            rid = store.accept('cap', 'task')['run_id']
            store.update(rid, state='retryable', owner_pid=None, budget_cap_usd=cap,
                         budget_spent_usd=spent, budget_held_usd=pending,
                         denials=[{'provider': 'claude', 'reason': 'run budget exhausted'}])
            revive(store, home_dir)
            record = store.read(rid)
        return record['state'], record.get('reason') or ''

    def test_a_cap_full_only_of_holds_sends_the_operator_to_recovery(self):
        """Wrong advice costs money: saying 'raise the cap' sets aside unspent money a second time.
        In the ghost-pending state the guard must point the operator to `cadre recover`."""
        status, reason = self._revive_reason(5.0, 0.32, 4.50)
        self.assertEqual(status, 'failed', 'An exhausted run must fail promptly without retrying')
        self.assertIn('cadre recover', reason, f'Must direct the user to review pending reservations: {reason!r}')
        self.assertNotIn('CADRE_RUN_CAP_USD', reason, 'Do not advise raising the cap when pending reservations cause exhaustion')

    def test_a_genuinely_spent_cap_still_tells_the_operator_to_raise_it(self):
        status, reason = self._revive_reason(5.0, 4.90, 0.0)
        self.assertEqual(status, 'failed')
        self.assertIn('CADRE_RUN_CAP_USD', reason, f'Keep the previous advice for actual exhaustion: {reason!r}')
        self.assertNotIn('cadre recover', reason)


if __name__ == "__main__":
    unittest.main()
