"""C14: one threshold, durable bans, and reevaluation after budget renewal."""

from datetime import datetime, timedelta
import json
import time
import unittest
from unittest.mock import patch

import test_run_sessions as fixtures
import budget_gate as budget
import qe_core as core


def snapshot(remaining=2.0, day=None):
    now = day or datetime.now()
    return {'ts':now.isoformat(), 'today':now.date().isoformat(),
            # B129: in the real world `available = pay - today_spent`, i.e. it cannot exceed the REMAINING
            # share. The fixture could pin `pay` at 2 and give 50 remaining; an impossible state,
            # and misleading now that the cap is bound to the measured share. The share is at least the remainder.
            'go':{'db':'ok', 'pay':max(2, remaining) * 100 / 15, 'available':(remaining) * 100 / 15,
                  'window_open':True, 'unit': 'points'}}


class BudgetPeriodTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_ninety_percent_consumption_never_reopens_go_or_launches_cloud(self):
        # GO1 (03.10): Go is used to the end, no daily margin; the gate closes only when the share RUNS OUT.
        with patch.object(core, 'budget_json', return_value=snapshot(0.0)):
            for _ in range(12):
                self.assertTrue(core.go_restriction_required()[0])
            # P3: when the share runs out, BudgetDenied at the gate — no fall to local, no cloud call.
            with self.assertRaises(budget.BudgetDenied):
                core.opencode_budget_env({}, ['-m', 'opencode-go/test', 'task'])
        with self.assertRaises(budget.BudgetDenied):
            budget.BudgetLedger(self.f.home/'budget.db').reserve('opencode-go', 1, snapshot(0.0), {})

    def test_reservations_affect_admission_without_aborting_the_call_that_holds_them(self):
        ledger = budget.BudgetLedger(self.f.home/'chain/request-budget.db')
        ledger.reserve('opencode-go', 2000000, snapshot(), {})   # GO1: no daily margin, full share 2.0
        self.assertEqual(ledger.available('opencode-go', snapshot()), 0)
        with patch.object(core, 'budget_json', return_value=snapshot()):
            self.assertTrue(core.go_restriction_required()[0])
            self.assertFalse(core.go_restriction_required(in_flight=True)[0])
            with self.assertRaises(budget.BudgetDenied):
                core.opencode_budget_env({}, ['-m', 'opencode-go/test', 'task'])

    def test_budget_ban_survives_restart_until_fresh_renewal(self):
        path = self.f.store.task_dir(self.f.rid, 'g1')/'budget-bans.json'
        bans = budget.ProviderBans(path, self.f.home)
        bans.block('opencode-go', 'budget', snapshot(.2))
        saved = json.loads(path.read_text())['opencode-go']
        self.assertEqual(saved['reason'], 'budget')
        self.assertIn('recheck_at', saved)
        restarted = budget.ProviderBans(path, self.f.home)
        restarted.refresh(snapshot(.2))   # 4c-13: if the Go remainder grows (window record) it is a renewal; the same remainder keeps the ban
        self.assertIn('opencode-go', restarted.blocked())
        tomorrow = datetime.now() + timedelta(days=1)
        with patch.object(budget.time, 'time', return_value=tomorrow.timestamp()):
            restarted.refresh(snapshot(day=tomorrow))
        self.assertNotIn('opencode-go', restarted.blocked())
        self.assertEqual(json.loads(path.read_text()), {})

    def test_same_day_budget_ban_lifts_only_when_the_observed_share_is_renewed(self):
        """D05 (01.10 Codex, G-B3 both directions): if the share renews on the same day (top-up, pace, 5 h reset) the ban lifts; movements
        that do not change the share, like a pending reservation closing, do not open the ban (C14 oscillation)."""
        bans = budget.ProviderBans(self.f.home/'bans.json', self.f.home)
        ledger = budget.BudgetLedger(self.f.home/'chain/request-budget.db')
        rid = ledger.reserve('opencode-go', 1_000_000, snapshot(2.0), {})
        bans.block('opencode-go', 'budget', snapshot(2.0))
        ledger.settle(rid, 100_000, 'small-receipt')      # capacity opened but the share is the same: the ban continues
        bans.refresh(snapshot(2.0))
        self.assertIn('opencode-go', bans.blocked())
        bans.refresh(snapshot(2.1))                      # a 5% crumb (delayed API): not a renewal
        self.assertIn('opencode-go', bans.blocked())
        renewed = snapshot(20.0)                      # daily share 2 -> 20 (top-up / pace)
        bans.refresh(renewed)
        self.assertNotIn('opencode-go', bans.blocked())

    def test_a_pocket_budget_ban_is_rechecked_after_its_recheck_time(self):
        """06.10 smoke test: the ban written when the run pocket (slice) ran out stayed permanent even when the cap rose via P6; the lane never started.
        A pocket-stamped ban is released back to the gate after recheck_at; a provider share ban (without slice) continues under the old rule."""
        bans = budget.ProviderBans(self.f.home/'bans.json', self.f.home)
        bans.block('opencode-go', 'budget', snapshot(2.0), retry_after=60, slice='lanes')
        bans.block('deepseek', 'budget', snapshot(2.0), retry_after=60)
        bans.refresh(snapshot(2.0))
        self.assertEqual(bans.blocked('lanes'), {'opencode-go', 'deepseek'}, 'Both restrictions last until recheck_at')
        data = bans.read(); data['opencode-go']['recheck_at'] = time.time() - 1; data['deepseek']['recheck_at'] = time.time() - 1
        budget.atomic_json(bans.path, data)
        bans.refresh(snapshot(2.0))
        self.assertNotIn('opencode-go', bans.blocked('lanes'), 'After recheck, the gate decides the pocket restriction')
        self.assertIn('deepseek', bans.blocked('lanes'), 'A provider allocation restriction stays until renewal')

    def test_a_run_pocket_refusal_bans_with_its_pocket_stamp(self):
        """06.10 smoke test: the plugin's run-pocket denial (a `slice` line) wrote an unstamped 'budget' ban; it did not open even when the cap rose."""
        import qe_core as core
        bans = budget.ProviderBans(self.f.home/'bans.json', self.f.home)
        core.apply_budget_denial(bans, {'refusals': [
            {'provider': 'opencode-go', 'scope': 'provider', 'reason': 'run budget exhausted', 'slice': 'lanes'},
            {'provider': 'deepseek', 'scope': 'provider', 'reason': 'allocation exhausted or outcome unknown'}]}, snapshot(2.0))
        data = bans.read()
        self.assertEqual(data['opencode-go'].get('slice'), 'lanes')
        self.assertIsNone(data['deepseek'].get('slice'))
        self.assertEqual(bans.blocked('judge'), {'deepseek'}, 'A pocket-specific restriction does not bind another pocket')

    def test_a_transient_refusal_is_not_a_budget_ban_and_lifts_after_recheck(self):
        bans = budget.ProviderBans(self.f.home/'bans.json', self.f.home)
        core.apply_budget_denial(bans, {'refusals': [
            {'provider': 'openrouter', 'scope': 'provider', 'reason': 'openrouter fail-closed pricing unavailable'},
            {'provider': 'opencode-go', 'scope': 'provider', 'reason': 'ledger refused or unavailable'}]}, snapshot(2.0))
        data = bans.read()
        self.assertEqual((data['openrouter']['reason'], data['opencode-go']['reason']), ('unavailable', 'unavailable'))
        data['opencode-go']['recheck_at'] = time.time() - 1
        budget.atomic_json(bans.path, data)
        bans.refresh(snapshot(2.0))
        self.assertNotIn('opencode-go', bans.blocked())

    def test_a_refusal_with_one_real_budget_reason_stays_a_budget_ban(self):
        bans = budget.ProviderBans(self.f.home/'bans.json', self.f.home)
        core.apply_budget_denial(bans, {'refusals': [
            {'provider': 'opencode-go', 'scope': 'provider', 'reason': 'allocation exhausted or outcome unknown'},
            {'provider': 'opencode-go', 'scope': 'provider', 'reason': 'ledger refused or unavailable'}]}, snapshot(2.0))
        self.assertEqual(bans.read()['opencode-go']['reason'], 'budget')

    def test_unknown_receipt_prevents_reopening_even_on_new_day(self):
        ledger = budget.BudgetLedger(self.f.home/'chain/request-budget.db')
        ledger.reserve('opencode-go', 2000000, snapshot(), {})
        bans = budget.ProviderBans(self.f.home/'bans.json', self.f.home)
        bans.block('opencode-go', 'budget', snapshot())
        tomorrow = datetime.now() + timedelta(days=1)
        with patch.object(budget.time, 'time', return_value=tomorrow.timestamp()):
            bans.refresh(snapshot(day=tomorrow))
        self.assertIn('opencode-go', bans.blocked())


    def free_observation(self, day=None, request=0):
        now = day or datetime.now()
        return {'ts': now.isoformat(), 'today': now.date().isoformat(),
                'groq': {'db': 'ok', 'today_request': request}}

    def test_yesterdays_unsettled_free_request_does_not_eat_todays_request_bucket(self):
        """B110-1: the count bucket (requests) resets every day; YESTERDAY's request without a receipt cannot restrict today."""
        ledger = budget.BudgetLedger(self.f.home/'chain/request-budget.db')
        yesterday = datetime.now() - timedelta(days=1)
        with patch.object(budget.time, 'time', return_value=yesterday.timestamp()):
            ledger.reserve('groq', 1, self.free_observation(yesterday), {})      # stays without a receipt
        # Live defect (Gemini, removed in 4c-11): 110 pending lines were permanently subtracted from the daily bucket.
        self.assertEqual(ledger.available('groq', self.free_observation()), budget.DAILY_REQUEST['groq'])

    def test_todays_unsettled_free_request_still_holds_its_slot(self):
        """A pending request IS still counted within the same day: the fail-closed behaviour within a day did not change."""
        ledger = budget.BudgetLedger(self.f.home/'chain/request-budget.db')
        ledger.reserve('groq', 1, self.free_observation(), {})
        self.assertEqual(ledger.available('groq', self.free_observation()), budget.DAILY_REQUEST['groq'] - 1)

    def test_unsettled_paid_reservation_still_blocks_the_next_day(self):
        """The fix must not leak into the MONEY bucket: spending with an unknown outcome does NOT OPEN the budget the next day (B68)."""
        ledger = budget.BudgetLedger(self.f.home/'chain/request-budget.db')
        ledger.reserve('opencode-go', 2000000, snapshot(), {})
        tomorrow = datetime.now() + timedelta(days=1)
        with patch.object(budget.time, 'time', return_value=tomorrow.timestamp()):
            self.assertEqual(ledger.available('opencode-go', snapshot(day=tomorrow)), 0)

    def test_provider_error_expires_but_budget_ban_does_not_use_error_cooldown(self):
        bans = budget.ProviderBans(self.f.home/'bans.json', self.f.home)
        bans.block('opencode-go', 'error', snapshot(), retry_after=1800)
        bans.refresh(snapshot())
        self.assertIn('opencode-go', bans.blocked())
        later = datetime.now() + timedelta(seconds=1801)
        with patch.object(budget.time, 'time', return_value=later.timestamp()):
            bans.refresh(snapshot(day=later))
        self.assertNotIn('opencode-go', bans.blocked())


if __name__ == '__main__':
    unittest.main()


class BrokenEnvironmentTests(unittest.TestCase):
    """B114 (19.09, cerebras/qwen-3.8-27b audit — both proven with a live command):
    in two places of the gate an environment variable was read outside the `try`. Result: an unrecorded traceback,
    a breach of the "every denial is recorded" contract and a raw stack trace on stderr."""

    def _execute(self, environment, arg=None):
        import subprocess, sys as _sys, pathlib, tempfile
        gate = pathlib.Path(__file__).resolve().parents[1] / 'lib' / 'budget_gate.py'
        home_dir = tempfile.TemporaryDirectory(prefix='broken-environment-home-dir-')   # G-G5: a missing CADRE_HOME default must not fall to the live home (network + writes)
        self.addCleanup(home_dir.cleanup)
        return subprocess.run([_sys.executable, str(gate), *([arg] if arg else [])],
                              input='{}', capture_output=True, text=True, env={**environment, 'HOME': home_dir.name}, timeout=60)

    def test_a_missing_environment_variable_is_a_recorded_denial_not_a_traceback(self):
        import os
        for missing in ('CADRE_HOME', 'CADRE_ENGINE_ROOT'):
            environment = {k: v for k, v in os.environ.items() if k != missing}
            environment.setdefault('CADRE_HOME', '/tmp/absent'); environment.setdefault('CADRE_ENGINE_ROOT', '/tmp/absent')
            environment.pop(missing)
            r = self._execute(environment)
            with self.subTest(missing=missing):
                self.assertNotIn('Traceback', r.stderr, f'{missing} otherwise a raw stack trace is printed')
                self.assertIn('budget denied', r.stderr.lower(), f'{missing} otherwise a standard denial is expected')
                self.assertEqual(r.returncode, 75, 'The gate denial code must be 75')

    def test_a_malformed_numeric_environment_value_does_not_break_the_import(self):
        import os
        for entry_name, broken in (('CADRE_PHANTOM_AGE_MIN', '30dk'), ('CADRE_FREE_DAY_CAP', 'many')):
            environment = {**os.environ, entry_name: broken, 'CADRE_HOME': '/tmp/absent', 'CADRE_ENGINE_ROOT': '/tmp/absent'}
            r = self._execute(environment)
            with self.subTest(entry_name=entry_name):
                self.assertNotIn('ValueError', r.stderr, f'{entry_name}={broken} crashes while importing the module')
                self.assertNotIn('Traceback', r.stderr, f'{entry_name}={broken} prints a raw stack trace')


