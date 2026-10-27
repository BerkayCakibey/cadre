"""Budget limit scenarios — with fake (dummy) data, against the real gate, results written to a table.

User request (17.09): "test the budgets with dummy data if needed, lots of different tests with their results
noted". Each scenario: an invented budget observation + the real `budget_gate` gate. Real money $0.
The result table is written with `--report` to `docs/BUDGET-SCENARIOS-20260917.md`."""

from __future__ import annotations

import datetime
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

OUTCOMES: list[tuple[str, str, str, bool]] = []


def instant(**extra):
    now = datetime.datetime.now()
    base = {
        'ts': now.isoformat(), 'today': now.date().isoformat(),
        'go': {'db': 'ok', 'pay': 13.333334, 'available': 13.333334, 'window_open': True, 'unit': 'points'},
        'zen': {'db': 'ok', 'pay': 0.35, 'available': 0.35},
        'groq': {'db': 'ok', 'today_request': 0},
    }
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    return base


class BudgetScenarios(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.home = self.f.home
        self.ledger = budget.BudgetLedger(self.home / 'chain/request-budget.db')

    def measure(self, entry_name, expected, fn, *, rejection_waiting=False):
        """The type and text of the result are verified; a denial passes only with a real BudgetDenied."""
        rejected = False
        try:
            observed = fn()
        except budget.BudgetDenied as e:
            observed = f'RED: {e}'
            rejected = True
        # An unexpected denial cannot be counted as an amount/status; the substring is searched only in the expected real denial reason.
        sleeping = rejected == rejection_waiting and (
            expected.lower() in observed.lower() if rejection_waiting else expected == observed)
        OUTCOMES.append((entry_name, expected, observed[:110], sleeping))
        self.assertTrue(sleeping, f'{entry_name}: expected "{expected}", observed "{observed}"')

    def pay(self, snapshot, provider):
        return str(self.ledger.available(provider, snapshot))

    # --- 1-4: daily share and margin ------------------------------------------------------------
    def test_s01_all_pools_full(self):
        # GO1 (03.10): Go is not split into days, no daily margin — full share 2.0 * 1e6 = 2_000_000.
        self.measure('S01 all pools available', '2000000', lambda: self.pay(instant(), 'opencode-go'))

    def test_s02_go_daily_share_finished(self):
        self.measure('S02 daily Go allocation exhausted', '0',
                 lambda: self.pay(instant(go={'available': 0.0}), 'opencode-go'))

    def test_s03_small_go_share_is_converted_from_points_to_dollars(self):
        # no daily margin on Go: 0.25 points is $0.0375 capacity on the heaviest model (N-BU04).
        self.measure('S03 small Go allocation available', '37500',
                 lambda: self.pay(instant(go={'available': 0.25}), 'opencode-go'))

    # --- 5-7: window, observation, failure --------------------------------------------------------
    def test_s05_go_window_closed(self):
        self.measure('S05 Go window closed', 'provider window closed',
                 lambda: str(budget.allowance(instant(go={'window_open': False}), 'opencode-go')), rejection_waiting=True)

    def test_s06_observation_stale(self):
        old = instant()
        old['ts'] = (datetime.datetime.now() - datetime.timedelta(seconds=200)).isoformat()
        self.measure('S06 observation 200 seconds stale', 'stale budget',
                 lambda: str(budget.allowance(old, 'opencode-go')), rejection_waiting=True)

    def test_s07_price_failure(self):
        import sqlite3
        with self.ledger.transaction() as con:
            con.execute("insert or ignore into pricing_fault values('opencode')")
        self.measure('S07 price failure (zen)', 'observed usage exceeded',
                 lambda: str(self.ledger.reserve('opencode', 1000, instant(), {})), rejection_waiting=True)

    # --- 8-10: zen and free ----------------------------------------------------------------
    def test_s08_zen_balance_zero(self):
        self.measure('S08 zero Zen balance', '0',
                 lambda: self.pay(instant(zen={'pay': 0.0, 'available': 0.0}), 'opencode'))

    def test_s09_zen_small_balance(self):
        # pay 0.35 -> marj 0.0525; remaining 0.35 - 0.0525 = 0.2975
        self.measure('S09 small Zen balance (base)', '297500', lambda: self.pay(instant(), 'opencode'))

    def test_s10_free_daily_request_finished(self):
        self.measure('S10 daily Groq requests exhausted', '0',
                 lambda: self.pay(instant(groq={'today_request': 900}), 'groq'))

    # --- 11-14: run cap, roles, buckets ------------------------------------------------
    def execute_run(self, cap):
        from run_store import RunStore
        store = RunStore(self.home)
        rid = store.accept('scenario', 'task')['run_id']
        store.update(rid, budget_cap_usd=cap)
        contract.create(self.home, rid, None)
        return rid

    def test_s11_run_cap_small(self):
        rid = self.execute_run(0.05)
        tav = budget.role_cap(self.home, rid, 'impl')
        # 17.09: the shares became FLOORS. With a $0.05 cap the judge floor's $0.20 minimum is limited to half (25000)
        # and the helper floor of 10% (5000) is set aside too → the lane is left 50000-25000-5000 = 20000.
        self.measure('S11 run cap $0.05, lane allocation', '20000', lambda: str(tav))

    def test_s12_cap_full_lane_is_rejected(self):
        rid = self.execute_run(1.0)
        tav = budget.role_cap(self.home, rid, 'impl')
        self.ledger.reserve('opencode-go', tav, instant(go={'pay': 10.0, 'available': 10.0}),
                            {'run_id': rid}, run_cap=tav)
        self.measure('S12 lane allocation exhausted → rejected', 'run budget exhausted',
                 lambda: str(self.ledger.reserve('opencode-go', 1000,
                                                 instant(go={'pay': 10.0, 'available': 10.0}),
                                                 {'run_id': rid}, run_cap=tav)), rejection_waiting=True)

    def test_s13_lane_full_judge_passes(self):
        rid = self.execute_run(1.0)
        divide = instant(go={'pay': 10.0, 'available': 10.0})
        lane, lane_slice = budget.role_budget(self.home, rid, 'impl')
        self.ledger.reserve('opencode-go', lane, divide, {'run_id': rid}, run_cap=lane, slice=lane_slice)
        judge, judge_slice = budget.role_budget(self.home, rid, 'quality')
        self.measure('S13 lane exhausted, judge uses its own allocation', 'acceptance',
                 lambda: 'acceptance' if self.ledger.reserve('opencode-go', 150_000, divide, {'run_id': rid},
                                                        run_cap=judge, slice=judge_slice) else 'red')

    def test_s14_free_model_works_even_if_the_paid_pool_is_finished(self):
        empty = instant(go={'available': 0.0})
        self.measure('S14 Go exhausted but zero-price model still works', 'acceptance',
                 lambda: 'acceptance' if self.ledger.reserve('opencode-go:free', 1, empty, {}) else 'red')

    def test_s15_free_bucket_daily_cap(self):
        os.environ['CADRE_FREE_DAY_CAP'] = '2'
        self.addCleanup(lambda: os.environ.pop('CADRE_FREE_DAY_CAP', None))
        for i in range(2):
            self.ledger.settle(self.ledger.reserve('opencode-go:free', 1, instant(), {}), 1, f'f{i}')
        self.measure('S15 daily free bucket cap', 'allocation exhausted',
                 lambda: str(self.ledger.reserve('opencode-go:free', 1, instant(), {})), rejection_waiting=True)

    def test_s16_unknown_provider(self):
        self.measure('S16 unmetered provider', 'unmetered provider',
                 lambda: str(budget.allowance(instant(), 'made-up-provider')), rejection_waiting=True)

    def test_numeric_measurements_reject_wrong_amounts(self):
        # N-QA01: a wrong amount cannot pass even if it contains the right amount as a substring.
        start = len(OUTCOMES)
        try:
            for expected, observed in (('0', '37500'), ('20000', '120000'), ('2000000', '20000000')):
                with self.subTest(expected=expected, observed=observed), self.assertRaises(AssertionError):
                    self.measure('wrong amount', expected, lambda: observed)
            self.measure('full zero', '0', lambda: '0')
            self.measure('full amount', '37500', lambda: '37500')
            def reject():
                raise budget.BudgetDenied('provider window closed: available 0')
            with self.assertRaises(AssertionError):
                self.measure('A rejection is not a zero amount', '0', reject)
            with self.assertRaises(AssertionError):
                self.measure('The word rejection is not an actual denial', 'provider window closed',
                         lambda: 'provider window closed', rejection_waiting=True)
            self.measure('actual rejection', 'provider window closed', reject, rejection_waiting=True)
        finally:
            del OUTCOMES[start:]  # Expected negative checks do not pollute the scenario report.


def report():
    target = Path(__file__).resolve().parents[2] / 'docs/BUDGET-SCENARIOS-20260917.md'
    lines = ['# Budget boundary scenarios — measurements with fake data', '',
                'Each scenario generates a fake budget observation and queries the **real** `budget_gate`.',
                'No real money is spent. Reproduce with: `python3 engine/tests/test_budget_scenarios.py --report`', '',
                '| # | scenario | expected | observed | result |', '|---|---|---|---|---|']
    for entry_name, wait, eye, ok in OUTCOMES:
        lines.append(f"| {entry_name.split()[0]} | {' '.join(entry_name.split()[1:])} | `{wait}` | `{eye}` | {'✅' if ok else '❌'} |")
    lines += ['', f'**{sum(1 for *_, ok in OUTCOMES if ok)}/{len(OUTCOMES)} scenarios behaved as expected.**']
    target.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'report: {target}')


if __name__ == '__main__':
    do_report = '--report' in sys.argv
    if do_report:
        sys.argv.remove('--report')
    outcome = unittest.main(exit=False, verbosity=0)
    if do_report:
        report()
    sys.exit(0 if outcome.result.wasSuccessful() else 1)
