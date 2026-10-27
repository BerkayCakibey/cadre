"""P5: the night budget.
(1) 5-hour window: even if full at observation time, the Claude share comes back once `resets_at` has passed; in the future/without the field, the old behaviour.
(2) Stale model catalogue: a Claude closure is a structured record inside run.json `denials[]` (not a log).
(3) Run-wide USD cap: the reservation ledger denies a request that exceeds the cap; cap 0 -> no cloud is offered at all (local);
    the Claude call cap (--max-budget-usd) is bound to the run's remainder; spending is in run.json and the `cadre durum` summary.
Fake home, fake quota file, fake claude; no network."""

from __future__ import annotations

from datetime import datetime
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import test_kosu_sessions as fixtures
import test_uctan_uca as e2e
import butce
import butce_kapisi
import kosu
import qe_core as core
from butce_kapisi import BudgetDenied, BudgetLedger
from kosu import RunStore

LIB = fixtures.LIB


class FiveHourResetTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def report(self, five_hour, fixed):
        home = self.f.home
        (home / '.claude').mkdir(exist_ok=True)
        vq = home / '.claude/claude_quota.json'
        vq.write_text(json.dumps({'rate_limits': {'seven_day': {'used_percentage': 30}, 'five_hour': five_hour}}))
        (home / 'zincir/claude-havuz-usd.txt').write_text('3000\n')
        observed = fixed.timestamp() - 600
        os.utime(vq, (observed, observed))

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(fixed.timestamp(), tz) if tz else cls.fromtimestamp(fixed.timestamp())

        out = io.StringIO()
        env = {k: v for k, v in os.environ.items() if not k.startswith('CADRE_TEST_') and k != 'CADRE_SIM_YUK'}
        with patch.dict(os.environ, env, clear=True), patch.object(butce, 'QE', home), patch.object(butce, 'DB', home / 'missing.db'), \
             patch.object(Path, 'home', return_value=home), \
             patch.object(butce.datetime, 'datetime', FixedDateTime), patch('sys.stdout', out), \
             patch.object(butce, 'oturum_say', return_value=1):   # B182: the real `ps` session count makes the test depend on the environment (5 sessions in the daytime -> share 0)
            butce.main()
        return json.loads(out.getvalue())['claude']

    def test_observation_written_after_now_counts_as_fresh_not_invalid(self):
        """B34: if the status line writes the quota file after `now` was taken the age is negative; that is 'fresh', not 'invalid'."""
        fixed = datetime(2026, 9, 17, 3, 0, 0)
        home = self.f.home
        (home / '.claude').mkdir(exist_ok=True)
        vq = home / '.claude/claude_quota.json'
        vq.write_text(json.dumps({'rate_limits': {'seven_day': {'used_percentage': 30}, 'five_hour': {'used_percentage': 10}}}))
        (home / 'zincir/claude-havuz-usd.txt').write_text('3000\n')
        future = fixed.timestamp() + 2   # the file was written 2 s after `now`

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(fixed.timestamp(), tz) if tz else cls.fromtimestamp(fixed.timestamp())

        os.utime(vq, (future, future))
        out = io.StringIO()
        env = {k: v for k, v in os.environ.items() if not k.startswith('CADRE_TEST_') and k != 'CADRE_SIM_YUK'}
        with patch.dict(os.environ, env, clear=True), patch.object(butce, 'QE', home), patch.object(butce, 'DB', home / 'missing.db'), \
             patch.object(Path, 'home', return_value=home), \
             patch.object(butce.datetime, 'datetime', FixedDateTime), patch('sys.stdout', out), \
             patch.object(butce, 'oturum_say', return_value=1):   # B182: the real `ps` session count makes the test depend on the environment (5 sessions in the daytime -> share 0)
            butce.main()
        claude = json.loads(out.getvalue())['claude']
        self.assertTrue(claude['gecerli'])
        self.assertEqual(claude['kaynak'], 'taze')
        self.assertGreater(claude['pay'], 0)

    def test_expired_five_hour_window_restores_the_share_but_a_future_or_missing_reset_does_not(self):
        fixed = datetime(2026, 9, 17, 3, 0, 0)  # Persembe 03:00
        past = self.report({'used_percentage': 90, 'resets_at': fixed.timestamp() - 60}, fixed)
        self.assertTrue(past['reset5_gecti'])
        self.assertEqual(past['used5'], 0.0)
        self.assertGreater(past['pay'], 0)
        future = self.report({'used_percentage': 90, 'resets_at': fixed.timestamp() + 3600}, fixed)
        self.assertFalse(future['reset5_gecti'])
        self.assertEqual(future['pay'], 0)
        missing = self.report({'used_percentage': 90}, fixed)
        self.assertFalse(missing['reset5_gecti'])
        self.assertEqual(missing['pay'], 0)
        broken = self.report({'used_percentage': 10, 'resets_at': 'yakinda'}, fixed)
        self.assertFalse(broken['reset5_gecti'])
        self.assertGreater(broken['pay'], 0)


class CatalogDenialTests(unittest.TestCase):
    def setUp(self):
        self.temp = fixtures.tempfile.TemporaryDirectory(prefix='qe-p5-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        (self.home / 'zincir').mkdir(parents=True)
        self.store = RunStore(self.home)
        self.rid = self.store.accept('a', 'claude isi')['run_id']
        self.catalog = Path(self.temp.name) / 'models.json'
        self.catalog.write_text('{}')
        self.env = {**os.environ, 'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(LIB.parent), 'CADRE_RUN_ID': self.rid,
                    'CADRE_GOREV_ID': 'g1', 'CADRE_BUDGET_ATTEMPT': 'att1', 'CADRE_MODEL_KATALOG': str(self.catalog)}
        for key in ('ANTHROPIC_BASE_URL', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY'):
            self.env.pop(key, None)

    def transport(self):
        return subprocess.run([sys.executable, str(LIB / 'claude_transport.py'), '/usr/bin/true'], env=self.env,
                              capture_output=True, text=True, timeout=30)

    def test_stale_catalog_is_a_structural_denial_in_the_run_record_and_a_fresh_one_is_not(self):
        old = time.time() - 8 * 86400
        os.utime(self.catalog, (old, old))
        self.assertEqual(self.transport().returncode, 75)
        denials = self.store.read(self.rid)['denials']
        self.assertEqual(len(denials), 1)
        self.assertEqual(denials[0]['provider'], 'claude')
        self.assertTrue(denials[0]['reason'].startswith('model catalogue stale'), denials[0])
        self.assertEqual((denials[0]['task'], denials[0]['attempt']), ('g1', 'att1'))
        now = time.time()
        os.utime(self.catalog, (now, now))
        result = self.transport()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.store.read(self.rid)['denials']), 1, 'fresh catalogue must not add a denial')


class RunCapTests(unittest.TestCase):
    def setUp(self):
        self.temp = fixtures.tempfile.TemporaryDirectory(prefix='qe-p5cap-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        (self.home / 'zincir').mkdir(parents=True)
        self.store = RunStore(self.home)
        self.snapshot = e2e.json.loads(subprocess.run([sys.executable, '-c', e2e.FAKE_BUTCE], capture_output=True, text=True, check=True).stdout)
        self.ledger = BudgetLedger(self.home / 'zincir/request-budget.db')

    def test_default_cap_is_two_dollars_and_project_or_env_override_it(self):
        self.assertEqual(self.store.accept('a', 'x')['budget_cap_usd'], 2.0)   # B199 user decision 23.09: easy/medium $2
        (self.home / 'projects/b').mkdir(parents=True)
        (self.home / 'projects/b/kosu-tavan-usd').write_text('3\n')
        self.assertEqual(self.store.accept('b', 'x')['budget_cap_usd'], 3.0)
        with patch.dict(os.environ, {'CADRE_KOSU_TAVAN_USD': '0'}):
            self.assertEqual(self.store.accept('b', 'x')['budget_cap_usd'], 0.0)

    def test_ledger_refuses_reservations_beyond_the_run_cap_but_not_free_request_quotas(self):
        rid = self.store.accept('a', 'x')['run_id']
        cap = butce_kapisi.run_cap_micro(self.home, rid)
        self.assertEqual(cap, 2_000_000)   # B199: default $2
        with patch.dict(os.environ, {'CADRE_KOSU_TAVAN_USD': '1'}):
            rid = self.store.accept('c', 'x')['run_id']
        cap = butce_kapisi.run_cap_micro(self.home, rid)
        self.assertEqual(cap, 1_000_000)
        first = self.ledger.reserve('opencode', 600_000, self.snapshot, {'run_id': rid, 'task_id': 'g1'}, run_cap=cap)
        self.assertEqual(self.ledger.available('opencode', self.snapshot, run_cap=cap, run_id=rid), 400_000)
        with self.assertRaisesRegex(BudgetDenied, 'run budget exhausted'):
            self.ledger.reserve('opencode', 500_000, self.snapshot, {'run_id': rid, 'task_id': 'g1'}, run_cap=cap)
        self.ledger.reserve('groq', 1, self.snapshot, {'run_id': rid}, run_cap=0)  # free provider counts requests, not USD
        self.ledger.settle(first, 300_000, 'receipt-1')
        self.assertEqual(self.ledger.available('opencode', self.snapshot, run_cap=cap, run_id=rid), 700_000)
        # another run on the same provider is not affected
        other = self.store.accept('d', 'x')['run_id']
        self.assertGreater(self.ledger.available('opencode', self.snapshot, run_cap=5_000_000, run_id=other), 700_000)
        # the settle CLI path writes structured spending to run.json; the summary shows it
        second = self.ledger.reserve('opencode', 200_000, self.snapshot, {'run_id': rid, 'task_id': 'g1'}, run_cap=cap)
        env = {**os.environ, 'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(LIB.parent), 'CADRE_RUN_ID': rid, 'CADRE_GOREV_ID': 'g1'}
        result = subprocess.run([sys.executable, str(LIB / 'butce_kapisi.py')], input=json.dumps({'action': 'settle', 'id': second, 'actual': 200_000, 'receipt': 'receipt-2'}),
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.store.read(rid)
        self.assertEqual(record['budget_spent_usd'], 0.5)
        self.assertEqual(record['budget_held_usd'], 0.0)
        self.assertIn('harcama $0.50/1.00', kosu.ozet(self.store, 'c'))
        # 17.09 HARD SPLIT: `run_remaining_usd` now reports the ROLE's pocket, not the whole run.
        # Spending was written without a slice -> charged to the lane pocket. Lane pocket $0.70, spending $0.50 -> $0.20 left.
        self.assertEqual(butce_kapisi.run_remaining_usd(self.home, rid), 0.2)
        self.assertEqual(butce_kapisi.run_remaining_usd(self.home, rid, 'impl'), 0.2)
        # ... and the judge is NOT AFFECTED by this: its own pocket is untouched (the $0.20 floor).
        self.assertEqual(butce_kapisi.run_remaining_usd(self.home, rid, 'kalite'), 0.2)
        self.assertEqual(butce_kapisi.run_remaining_usd(self.home, rid, 'review'), 0.1)


class ExecutorCapTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp(budget_cap_usd=5)
        self.addCleanup(self.f.doCleanups)
        snapshot = json.loads(subprocess.run([sys.executable, '-c', e2e.FAKE_BUTCE], capture_output=True, text=True, check=True).stdout)
        snapshot['claude'] = {'kaynak': 'taze', 'gecerli': True, 'pay': 50, 'yuzde1_usd': 10, 'havuz_kalan_usd': 200, 'fable_bugun': 0, 'fable_attempts': []}
        self.snapshot = snapshot
        self.f.enter(patch.object(core, 'butce_json', return_value=snapshot))

    def test_zero_run_cap_offers_no_paid_provider_to_opencode(self):
        """The cap is for USD: 0 -> paid providers (Go/DeepSeek/Cerebras/Claude) are never offered. P3 (03.10): the engine
        does not fall to local, it gives a budget-wait denial at the gate (the session identity is kept, the coordinator retries when the budget returns).
        Free providers (Gemini/Groq/Mistral) count requests and spend no dollars."""
        self.f.store.update(self.f.rid, budget_cap_usd=0.0)
        env = {}
        with self.assertRaises(BudgetDenied):
            core.opencode_budget_env(env, ['-m', 'opencode-go/kimi-k2.7-code', 'task'])
        for provider in ('opencode-go', 'opencode', 'openrouter', 'claude'):
            self.assertEqual(butce_kapisi.available_budget(self.f.home, self.snapshot, provider), 0)
        self.assertGreater(butce_kapisi.available_budget(self.f.home, self.snapshot, 'groq'), 0)
        self.f.store.update(self.f.rid, budget_cap_usd=5.0)
        env = {}
        args = core.opencode_budget_env(env, ['-m', 'opencode-go/kimi-k2.7-code', 'task'])
        self.assertEqual(args[args.index('-m') + 1], 'opencode-go/kimi-k2.7-code')
        self.assertTrue(json.loads(env['OPENCODE_CONFIG_CONTENT']).get('plugin'))

    def test_claude_call_budget_is_bounded_by_the_remaining_run_cap(self):
        fake = self.f.claude_fixture()
        self.f.enter(patch.dict(os.environ, {'CLAUDE_BIN': str(fake), 'CADRE_HARCAMA_ALARMI': '0'}))   # $3 per second: the alarm is a separate contract
        self.f.store.update(self.f.rid, budget_cap_usd=3.0)
        ledger = BudgetLedger(self.f.home / 'zincir/request-budget.db')
        ident = ledger.reserve('claude', 2_000_000, self.snapshot, {'run_id': self.f.rid, 'task_id': 'g1'}, run_cap=3_000_000)
        ledger.settle(ident, 2_000_000, 'r1')
        proc = core.run_claude(str(self.f.wt), 'claude/sonnet#high', 'task', self.f.log)
        self.assertEqual(proc.wait(timeout=10), 0)
        calls = [json.loads(l) for l in (self.f.home / 'claude-calls.jsonl').read_text().splitlines()]
        args = calls[-1]
        # The call cap is limited by the ROLE's remaining pocket: with a $3 cap the lane pocket is $2.25, $2.00 spent -> $0.25.
        self.assertEqual(args[args.index('--max-budget-usd') + 1], '0.25')
        ident = ledger.reserve('claude', 1_000_000, self.snapshot, {'run_id': self.f.rid, 'task_id': 'g1'}, run_cap=3_000_000)
        ledger.settle(ident, 1_000_000, 'r2')
        with self.assertRaises(BudgetDenied):
            core.run_claude(str(self.f.wt), 'claude/sonnet#high', 'task', self.f.log)


if __name__ == '__main__':
    unittest.main()
