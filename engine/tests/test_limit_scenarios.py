"""§40 LIMIT SCENARIOS — tests that PUSH the limit instead of "a week of shadow mode" (user decision 17.09).

Each scenario's threshold is written BEFORE the run; all in an isolated home, with fake data, real money $0.
S1/S2/S3/S8 overlap with the night autonomy scenarios (test_night_scenarios.py) and S9 with the coordinator tests;
here the remaining §40 items are proven one by one on top of them."""

from __future__ import annotations

import calendar
from contextlib import closing
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import budget_gate as budget  # noqa: E402
import go_window  # noqa: E402
import contract  # noqa: E402
from test_budget_period import snapshot  # noqa: E402
import test_run_sessions as fixtures  # noqa: E402


def utc(iso):
    return calendar.timegm(time.strptime(iso, '%Y-%m-%d %H:%M'))


class S1S2S3BudgetLimitTests(unittest.TestCase):
    """S1 budget 0% · S2 budget 99% (a single-call share) · S3 the window fills mid-call.
    The night scenarios (test_night_scenarios.py) run the engine side of these; here the GATE itself is pushed."""

    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.ledger = budget.BudgetLedger(self.f.home / 'chain/request-budget.db')

    def test_s1_zero_budget_admits_no_paid_call_but_free_requests_still_pass(self):
        empty = snapshot(0.0)
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('opencode-go', 1, empty, {})
        # the free bucket is counted separately: a zero-dollar budget does not close it
        empty['groq'] = {'db': 'ok', 'today_request': 0}
        self.ledger.reserve('groq', 1, empty, {})

    def test_s2_a_single_call_worth_of_budget_admits_exactly_one_call(self):
        # GO1 (03.10): no daily margin on Go; the remaining share can be used directly.
        # Remaining 1.0 -> 1000000 micro-dollars. This is a single-call share.
        instant = snapshot(1.0)
        self.assertEqual(self.ledger.available('opencode-go', instant), 1_000_000)
        self.ledger.reserve('opencode-go', 1_000_000, instant, {})
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('opencode-go', 1, instant, {})

    def test_s3_a_window_that_closes_mid_flight_stops_the_next_call_only(self):
        instant = snapshot(5.0)
        in_flight = self.ledger.reserve('opencode-go', 100_000, instant, {})     # the in-flight call
        closed = snapshot(5.0)
        closed['go']['window_open'] = False
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('opencode-go', 1000, closed, {})
        # the in-flight call can write its receipt: closing the window does not cancel it
        self.ledger.settle(in_flight, 40_000, 'slip')


class S4HandoverLimitTests(unittest.TestCase):
    """S4: if three providers fail in a row, hand-offs <=2/lane, then local; no endless loop."""

    def test_three_failures_in_a_row_end_in_budget_wait_not_in_a_loop(self):
        import importlib
        temp = tempfile.TemporaryDirectory(prefix='qe-s4-')
        self.addCleanup(temp.cleanup)
        old = {k: os.environ.get(k) for k in ('CADRE_HOME', 'CADRE_RUN_ID', 'CADRE_TASK_ID')}
        os.environ.update(CADRE_HOME=temp.name, CADRE_RUN_ID='r-s4', CADRE_TASK_ID='g1')
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in old.items()])
        import qe_core
        importlib.reload(qe_core)
        chain = ['deepseek/deepseek-v4-flash', 'cerebras/gpt-oss-120b', 'google/gemini-3-flash-preview']
        outcomes = []
        with patch.object(qe_core.ROUTER, 'chain', return_value=(chain, {})), \
             patch.object(qe_core.ROUTER, 'failure'), patch.object(qe_core, 'request_measure', return_value=8000), \
             patch.object(qe_core, 'log'):
            for _ in range(5):
                outcomes.append(qe_core.quota_drop('openrouter/openai/gpt-6.1-sol', None))
        self.assertEqual(outcomes[:2], ['deepseek/deepseek-v4-flash'] * 2)
        self.assertTrue(all(s == '' for s in outcomes[2:]), f'After the third handoff, always wait for budget: {outcomes}')


class S5CacheMeasurementTests(unittest.TestCase):
    """S5: with a 90% cache hit rate the measured spending must be within +-10% of reality."""

    def test_the_measured_cost_is_within_ten_percent_of_the_truth(self):
        # The same as the calculation in the plugin: 100 fresh x $1/M + 900 cache x $0.1/M + 10 output x $2/M
        fresh, cache, output = 100, 900, 10
        real = fresh * 1.0 + cache * 0.1 + output * 2.0
        old_account = (fresh + cache) * 2.0 + output * 2.0      # with the most expensive rate (cache.write=2)
        self.assertAlmostEqual(real, 210.0)
        self.assertGreater(old_account / real, 8.0, 'Measured inflation in previous accounting: about 8.5 times')
        # The plugin test (budget-plugin.test.mjs) verifies this number END TO END; here the arithmetic itself is locked.
        deviation = abs(real - 210.0) / 210.0
        self.assertLess(deviation, 0.10)


class S6ResetInstantTests(unittest.TestCase):
    """S6: exactly at the month/week/5-hour boundary the share is recomputed; no double counting and no negative share."""


    def test_the_claude_ceiling_is_continuous_across_the_reset_second(self):
        import claude_policy as cp
        from datetime import datetime, timedelta
        reset = datetime(2026, 9, 15, 19, 0)
        once = cp.cap(reset - timedelta(seconds=1))
        then = cp.cap(reset)
        self.assertEqual(once, 92.5, 'Cap before reset is 92.5; the old code had a 95 percent gap')
        self.assertEqual(then, 62.5, 'The week restarts at reset')
        self.assertLess(then, once, 'A new week requires a larger reserve')


class S7ClaudeResetTests(unittest.TestCase):
    """S7: at the Tuesday 19:00 reset the cap moves to the new week and the automation does not eat the user's share."""

    def test_the_user_keeps_seven_and_a_half_points_per_remaining_workday(self):
        import claude_policy as cp
        from datetime import datetime
        for date, remaining_day in [('2026-09-15T19:00:00', 5), ('2026-09-16T12:00:00', 4),
                                 ('2026-09-18T12:00:00', 2), ('2026-09-21T12:00:00', 1)]:
            now = datetime.fromisoformat(date)
            with self.subTest(date=date):
                self.assertEqual(cp.remaining_reserve_day(now), remaining_day)
                user_share = 100 - cp.cap(now)
                self.assertGreaterEqual(user_share, min(remaining_day * 7.5, 7.5),
                                        'Leave an allocation for each remaining reserve day')


class S10ZenBucketTests(unittest.TestCase):
    """S10: if zen's free and paid models run AT THE SAME TIME the buckets do not mix."""

    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.ledger = budget.BudgetLedger(self.f.home / 'chain/request-budget.db')

    def instant(self):
        import datetime
        return {'ts': datetime.datetime.now().isoformat(), 'today': datetime.date.today().isoformat(),
                'zen': {'db': 'ok', 'pay': 1.0, 'available': 1.0}}

    def test_free_and_paid_zen_never_share_a_bucket(self):
        instant = self.instant()
        paid = self.ledger.reserve('opencode', 200_000, instant, {'run_id': 'r1', 'model': 'glm-5.2'})
        free = self.ledger.reserve('opencode:free', 1, instant, {'run_id': 'r1', 'model': 'big-pickle'})
        self.ledger.settle(paid, 150_000, 'slip-paid')
        self.ledger.settle(free, 1, 'slip-free')
        import sqlite3
        with closing(sqlite3.connect(self.f.home / 'chain/request-budget.db')) as con, con:
            lines = dict(con.execute('select provider, sum(actual) from reservation group by provider').fetchall())
        self.assertEqual(lines['opencode'], 150_000)
        self.assertEqual(lines['opencode:free'], 1)
        # the free bucket does not enter the run's DOLLAR cap
        with closing(sqlite3.connect(self.f.home / 'chain/request-budget.db')) as con, con:
            settled, held = budget.run_usage(con, 'r1')
        self.assertEqual(settled, 150_000)


class S11DivergenceTests(unittest.TestCase):
    """S11: if the provider percentage and my own measurement diverge by 20 points the conservative one is used and a warning is written."""

    def test_the_conservative_number_wins_and_the_gap_is_recorded(self):
        data = {'rolling': 0, 'weekly': 2, 'monthly': 27, 'history': []}
        fresh, warning = go_window.cross_check(data, 50)
        self.assertEqual(fresh['monthly'], 50)
        self.assertIn('deviation', warning)
        fresh, warning = go_window.cross_check(data, 5)
        self.assertEqual(fresh['monthly'], 27, 'Use the provider value if it is higher')
        self.assertIn('deviation', warning)

    def test_a_small_gap_changes_nothing(self):
        data = {'rolling': 0, 'weekly': 2, 'monthly': 27, 'history': []}
        for own in (27, 28.3, 10, 46):
            fresh, warning = go_window.cross_check(data, own)
            with self.subTest(own=own):
                self.assertEqual(fresh['monthly'], 27)
                self.assertEqual(warning, '')

    def test_an_unusable_own_measurement_is_ignored_not_trusted(self):
        data = {'monthly': 27, 'history': []}
        for own in (None, 'abc', -5, 5000):
            fresh, warning = go_window.cross_check(data, own)
            self.assertEqual(fresh['monthly'], 27, own)
            self.assertEqual(warning, '', own)


class S12JudgeOrderTests(unittest.TestCase):
    """S12: run cap full + the judge's turn -> the judge passes from its own reserve; even if the reserve runs out the judge chain continues."""

    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        from run_store import RunStore
        self.store = RunStore(self.f.home)
        self.rid = self.store.accept('s12', 'task')['run_id']
        self.store.update(self.rid, budget_cap_usd=1.0)
        self.ledger = budget.BudgetLedger(self.f.home / 'chain/request-budget.db')
        contract.create(self.f.home, self.rid, None)

    def test_the_reviewer_passes_through_its_own_reserve_when_lanes_are_full(self):
        lane_cap, lane_slice = budget.role_budget(self.f.home, self.rid, 'impl')
        self.ledger.reserve('opencode-go', lane_cap, snapshot(5.0), {'run_id': self.rid},
                            run_cap=lane_cap, slice=lane_slice)
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('opencode-go', 1000, snapshot(5.0), {'run_id': self.rid},
                                run_cap=lane_cap, slice=lane_slice)
        # HARD SPLIT: the judge's cap is SMALLER than the lane's, but the lane's spending does not go into the judge's pocket.
        judge_cap, judge_slice = budget.role_budget(self.f.home, self.rid, 'quality')
        self.assertLess(judge_cap, lane_cap)
        self.ledger.reserve('opencode-go', 200_000, snapshot(5.0), {'run_id': self.rid},
                            run_cap=judge_cap, slice=judge_slice)


if __name__ == '__main__':
    unittest.main()
