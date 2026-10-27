"""Three audit rounds — round 1 fixes (B43-B46, B49, B50, B53, B55). Every test measures a BEHAVIOUR (no source grep)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import run_store  # noqa: E402
from run_store import RunStore  # noqa: E402
import budget_gate  # noqa: E402
from budget_gate import BudgetDenied, BudgetLedger  # noqa: E402
import model_router as router  # noqa: E402
from test_night_wait import full_budget  # noqa: E402


class StoreFixes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-round1-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = RunStore(self.home)

    def test_b43_cancel_racing_with_completion_never_marks_a_completed_run_cancelled(self):
        rid = self.store.accept('a', 'x')['run_id']
        record = self.store.read(rid)
        self.store.update(rid, state='completed')          # the reader comes with a stale record, the run finished in the meantime
        self.assertFalse(run_store.cancel_run(self.store, record))
        self.assertEqual(self.store.read(rid)['state'], 'completed')
        self.assertFalse((self.store.path(rid) / 'cancel.flag').exists())
        # unfinished run: flag + cancelled immediately (no owner)
        rid2 = self.store.accept('b', 'x')['run_id']
        self.assertTrue(run_store.cancel_run(self.store, self.store.read(rid2)))
        self.assertEqual(self.store.read(rid2)['state'], 'cancelled')

    def test_b271_project_cancel_propagates_to_the_active_child(self):
        """B271 (30.09 rehearsal): when the parent project run was cancelled, the child running in the same process stayed a `running` ghost;
        the child's wait loop only checks ITS OWN cancel.flag. Cancel spreads to the active child too."""
        main = self.store.accept('p', 'project_ref')['run_id']
        child = self.store.accept('p--pab12', 'feature')['run_id']
        self.store.update(child, state='running', project_ref_parent=main)
        run_store.atomic_json(self.store.path(main) / 'project-ref-active.json', {'signature': 'x', 'child': child})
        self.assertTrue(run_store.cancel_run(self.store, self.store.read(main)))
        self.assertTrue((self.store.path(child) / 'cancel.flag').exists(), 'The child cancel flag was not written')
        self.assertEqual(self.store.read(child)['state'], 'cancelled')   # no owner -> cancelled immediately (same as B35)
        self.assertEqual(self.store.read(main)['state'], 'cancelled')
        # a finished child is not touched
        main2 = self.store.accept('q', 'project_ref')['run_id']
        child2 = self.store.accept('q--pcd34', 'feature')['run_id']
        self.store.update(child2, state='completed')
        run_store.atomic_json(self.store.path(main2) / 'project-ref-active.json', {'signature': 'y', 'child': child2})
        self.assertTrue(run_store.cancel_run(self.store, self.store.read(main2)))
        self.assertEqual(self.store.read(child2)['state'], 'completed')
        self.assertFalse((self.store.path(child2) / 'cancel.flag').exists())

    def test_b45_accepted_or_queued_run_is_started_at_most_three_times_then_failed(self):
        rid = self.store.accept('a', 'x')['run_id']
        self.store.update(rid, created=time.time() - 10_000)  # stale accepted
        root = self.home / 'code'
        started = []
        with patch.object(run_store, '_start', side_effect=lambda store, r, ro, why: started.append(r)):
            for _ in range(3):
                self.assertEqual(run_store.revive(self.store, root), [rid])
            self.assertEqual(run_store.revive(self.store, root), [])
        self.assertEqual(len(started), 3)
        record = self.store.read(rid)
        self.assertEqual(record['state'], 'failed')
        self.assertIn('start limit', record['reason'])
        self.assertEqual(record['start_count'], 3)

    def test_b46_one_corrupt_run_record_is_skipped_and_logged_instead_of_breaking_the_queue(self):
        good = self.store.accept('a', 'good')['run_id']
        bad = self.store.accept('a', 'broken')['run_id']
        (self.store.path(bad) / 'run.json').write_text('{broken json')
        self.assertEqual([r['run_id'] for r in self.store.list('a')], [good])
        self.assertEqual([r['run_id'] for r in self.store.pending('a')], [good])
        self.assertTrue(self.store.accept('a', 'third')['run_id'])  # acceptance still works (the queue calculation skips the broken record)
        log = (self.home / 'logs/run-broken.log').read_text()
        self.assertIn(bad, log)

    def test_b44_concurrent_accepts_in_one_project_yield_exactly_one_accepted(self):
        import threading
        results = []
        def acceptance():
            results.append(self.store.accept('a', 'race')['state'])
        threads = [threading.Thread(target=acceptance) for _ in range(6)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sorted(results), ['accepted'] + ['queued'] * 5)


class LedgerFixes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-round1-ledger-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'chain').mkdir()
        self.store = RunStore(self.home)
        self.ledger = BudgetLedger(self.home / 'chain/request-budget.db')
        self.snapshot = full_budget(cloud=True)

    def test_b49_run_cap_denial_names_the_amounts_and_repeated_denials_collapse(self):
        with patch.dict(os.environ, {'CADRE_RUN_CAP_USD': '1'}):
            rid = self.store.accept('c', 'x')['run_id']
        cap = budget_gate.run_cap_micro(self.home, rid)
        self.ledger.reserve('opencode', 600_000, self.snapshot, {'run_id': rid}, run_cap=cap)
        with self.assertRaises(BudgetDenied) as ctx:
            self.ledger.reserve('opencode', 500_000, self.snapshot, {'run_id': rid}, run_cap=cap)
        msg = str(ctx.exception)
        self.assertTrue(msg.startswith('run budget exhausted'), msg)
        self.assertIn('reservation $0.50', msg); self.assertIn('remaining $0.40', msg); self.assertIn('cap $1.00', msg)
        with patch.dict(os.environ, {'CADRE_RUN_ID': rid, 'CADRE_TASK_ID': 'plan', 'CADRE_BUDGET_ATTEMPT': 'a1'}):
            for _ in range(3):
                budget_gate.note_denial(self.home, 'claude', reason=msg)
            budget_gate.note_denial(self.home, 'opencode-go', reason=msg)
        rows = self.store.read(rid)['denials']
        self.assertEqual([(r['provider'], r.get('repeat', 1)) for r in rows], [('claude', 3), ('opencode-go', 1)])

    def test_b55_run_record_gets_a_provider_task_role_model_breakdown_and_unsettled_list(self):
        rid = self.store.accept('a', 'x')['run_id']
        a = self.ledger.reserve('opencode', 100_000, self.snapshot, {'run_id': rid, 'task': 'plan', 'role': 'plan', 'model': 'deepseek-v4-flash'}, run_cap=5_000_000)
        self.ledger.settle(a, 40_000, 'r1')
        b = self.ledger.reserve('opencode-go', 200_000, self.snapshot, {'run_id': rid, 'task': 'g1', 'role': 'compaction', 'model': 'glm-5.3-flash'}, run_cap=5_000_000)
        self.ledger.settle(b, 20_000, 'r2')
        self.ledger.reserve('groq', 1, self.snapshot, {'run_id': rid, 'task': 'g1', 'role': 'compaction', 'model': 'openai/gpt-oss-120b'}, run_cap=0)  # not closed
        budget_gate.run_spend_write(self.home, self.ledger, rid)
        record = self.store.read(rid)
        self.assertEqual(record['budget_spent_usd'], 0.06)
        by = {(r['provider'], r['task'], r['role']): r for r in record['budget_breakdown']}
        self.assertEqual(by[('opencode-go', 'g1', 'compaction')]['actual_usd'], 0.02)
        self.assertEqual(by[('opencode', 'plan', 'plan')]['model'], 'deepseek-v4-flash')
        self.assertEqual(by[('groq', 'g1', 'compaction')]['pending'], 1)
        self.assertEqual(len(record['budget_unsettled']), 1)
        self.assertEqual(record['budget_unsettled'][0]['provider'], 'groq')


class RouterFixes(unittest.TestCase):
    def test_b50_chain_decisions_are_tied_to_the_run_and_task_and_keep_their_reasons(self):
        temp = tempfile.TemporaryDirectory(prefix='qe-round1-router-'); self.addCleanup(temp.cleanup)
        record = Path(temp.name) / 'yk.jsonl'
        with patch.object(router, 'RECORD', record), patch.dict(os.environ, {'CADRE_RUN_ID': 'run-x', 'CADRE_TASK_ID': 'g7'}):
            chain, reason = router.chain('impl', 'middle', 20000, full_budget(cloud=True), {})
        line = json.loads(record.read_text().splitlines()[-1])
        self.assertEqual((line['event'], line['run_id'], line['task_id']), ('chain', 'run-x', 'g7'))
        self.assertEqual(line['chain'], chain)
        self.assertTrue(line['reason'] and line['reason'] == reason)


class ClaudeReservationFixes(unittest.TestCase):
    """B56: the Claude reservation is priced with the request's real size (not the model's 1M context) — otherwise Claude could
    never be used with a $5 cap. B58: an unreadable run record does not remove the cap (fail-closed)."""

    CATALOG = {'anthropic': {'models': {'claude-opus-5': {
        'cost': {'input': 15, 'output': 75, 'cache_write': 18.75, 'cache_read': 1.5},
        'limit': {'context': 1_000_000, 'output': 64_000}}}}}

    def test_b56_reservation_follows_request_size_so_a_small_call_fits_the_five_dollar_cap(self):
        import claude_transport as ct
        body = {'model': 'claude-opus-5', 'max_tokens': 4000,
                'messages': [{'role': 'user', 'content': 'one small planning request'}]}
        pricing = ct.price(body, self.CATALOG)
        request = min(1_000_000, len(json.dumps(body, ensure_ascii=False)) + 4096)
        expected = ct.micro(request * pricing.input + 4000 * pricing.output)
        self.assertEqual(pricing.maximum, expected)
        self.assertLess(pricing.maximum, 5_000_000, f'A small request must fit the 5 USD run cap; reservation USD {pricing.maximum / 1e6:.2f}')
        full_context = ct.micro(1_000_000 * pricing.input + 4000 * pricing.output)
        self.assertGreater(full_context, 25_000_000, 'Previous behavior reserved about 30 USD for one request')
        # a large request is still priced with its real size (the ledger denies if the cap is exceeded)
        big = {**body, 'messages': [{'role': 'user', 'content': 'x' * 400_000}]}
        self.assertGreater(ct.price(big, self.CATALOG).maximum, pricing.maximum * 10)

    def test_b58_unreadable_run_record_denies_instead_of_silently_removing_the_cap(self):
        temp = tempfile.TemporaryDirectory(prefix='qe-round1-cap-'); self.addCleanup(temp.cleanup)
        home = Path(temp.name)
        store = RunStore(home)
        rid = store.accept('a', 'x')['run_id']
        self.assertEqual(budget_gate.run_cap_micro(home, rid), 2_000_000)   # B199: easy/medium default $2
        (store.path(rid) / 'run.json').write_text('{broken')
        with self.assertRaisesRegex(BudgetDenied, 'unreadable'):
            budget_gate.run_cap_micro(home, rid)
        self.assertIsNone(budget_gate.run_cap_micro(home, None))  # no run, no cap (unchanged)


class InFlightBrakeFixes(unittest.TestCase):
    """B57: when the run cap fills IN THE MIDDLE OF A JOB the brake works; a free provider is not braked."""

    def test_b57_exhausted_run_cap_brakes_paid_providers_one_by_one_but_not_free_ones(self):
        import qe_core as core
        temp = tempfile.TemporaryDirectory(prefix='qe-round1-brake-'); self.addCleanup(temp.cleanup)
        home = Path(temp.name)
        with patch.object(core, 'QE', home), patch.dict(os.environ, {'CADRE_RUN_ID': 'r1'}):
            with patch.object(core, 'run_remaining_usd', return_value=0.0):
                self.assertEqual(core.provider_brake(full_budget(cloud=True), ()), ('opencode-go', 'run cap exhausted'))
                self.assertEqual(core.provider_brake(full_budget(cloud=True), ('opencode-go',)),
                                 ('opencode', 'run cap exhausted'))     # B110: the order comes from USD_PROVIDERS
                self.assertEqual(core.provider_brake(full_budget(cloud=True), ('opencode-go', 'opencode')),
                                 ('claude', 'run cap exhausted'))
                # B110 (18.09): PAID ZEN ('opencode') was in none of the four brake lists; when the cap was full
                # zen stayed free. The list now comes from a single source (USD_PROVIDERS).
                self.assertEqual(core.provider_brake(full_budget(cloud=True), ('opencode-go', 'claude')),
                                 ('opencode', 'run cap exhausted'))
                forbidden = ('opencode-go', 'opencode', 'claude', 'openrouter')   # 4c-1: USD providers
                self.assertIsNone(core.provider_brake(full_budget(cloud=True), forbidden), 'Do not brake free Google capacity')
            with patch.object(core, 'run_remaining_usd', return_value=3.0):
                self.assertIsNone(core.provider_brake(full_budget(cloud=True), ()), 'Brake when the cap is exhausted; otherwise apply the daily allocation rule')


class Round2MoneyPathTests(unittest.TestCase):
    """Round 2 fixes: B68 a stale hold does not eat the cap · B69 the Claude ladder is skipped at the run cap · B71 UTF-8 upper bound
    · B73 the receipt closes at the real price."""

    CATALOG = {'anthropic': {'models': {'m': {
        'cost': {'input': 10, 'output': 40, 'cache_write': 12.5, 'cache_read': 1.0},
        'limit': {'context': 1_000_000, 'output': 8_000}}}}}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-round2-'); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name); (self.home / 'chain').mkdir()
        self.ledger = BudgetLedger(self.home / 'chain/request-budget.db')
        self.snapshot = full_budget(cloud=True)
        self.snapshot['claude'].update(pay=5, percent1_usd=30, pool_remaining_usd=200)   # give Claude a share (the default fixture is 0)

    def test_b68_a_unknown_outcome_keeps_holding_the_budget_even_when_old(self):
        """T3 FIX: first "a hold older than 6 hours must not eat the cap" was written; `test_budget_period` showed it broke a proven invariant
        (a request with an UNKNOWN OUTCOME never REOPENS the budget, not even the next day = fail-closed). An unknown outcome
        counts as spent; the fix is to close only in the KNOWN-ZERO case (4xx)."""
        import sqlite3
        ident = self.ledger.reserve('opencode', 3_000_000, self.snapshot, {'run_id': 'r1'}, run_cap=None)
        least = self.ledger.available('opencode', self.snapshot)
        self.assertLess(least, 5_000_000, 'A hold reduces the cap')
        with closing(sqlite3.connect(self.ledger.path)) as con, con:   # age the reservation by 7 days
            con.execute('update reservation set created=? where id=?', (time.time() - 7 * 86400, ident))
        self.assertEqual(self.ledger.available('opencode', self.snapshot), least, 'An old reservation with an UNKNOWN OUTCOME does not release budget')
        self.ledger.settle(ident, 0, f'{ident}:http-429')   # known zero (4xx) -> closes
        self.assertGreater(self.ledger.available('opencode', self.snapshot), least)

    def test_b68_b_http_4xx_closes_the_reservation_at_zero(self):
        import claude_transport as ct
        class Response:
            status = 429
            headers = {'Content-Type': 'application/json'}
            def read1(self, n): return b''
            def close(self): pass
        relay = ct.Relay(self.home, self.CATALOG, lambda: self.snapshot, lambda b, h, q: Response())
        ident, pricing, _ = relay.open({'model': 'm', 'max_tokens': 100, 'messages': [{'role': 'user', 'content': 'x'}]}, {})
        import sqlite3
        with closing(sqlite3.connect(self.ledger.path)) as con, con:
            self.assertIsNone(con.execute('select actual from reservation where id=?', (ident,)).fetchone()[0])
        relay.ledger.settle(ident, 0, f'{ident}:http-429')   # what the handler's 4xx branch does
        with closing(sqlite3.connect(self.ledger.path)) as con, con:
            self.assertEqual(con.execute('select actual from reservation where id=?', (ident,)).fetchone()[0], 0)

    def test_b71_reservation_bound_survives_a_non_ascii_body(self):
        import claude_transport as ct
        emoji = {'model': 'm', 'max_tokens': 10, 'messages': [{'role': 'user', 'content': '😀' * 2000}]}
        ascii_ = {'model': 'm', 'max_tokens': 10, 'messages': [{'role': 'user', 'content': 'a' * 2000}]}
        p_emoji, p_ascii = ct.price(emoji, self.CATALOG), ct.price(ascii_, self.CATALOG)
        self.assertGreater(p_emoji.maximum, p_ascii.maximum, 'An emoji body uses more tokens and the limit must account for that')
        # the sent body is ensure_ascii=True: each emoji \ud83d\ude00 = 12 bytes -> the limit is above the token count
        self.assertGreaterEqual(len(json.dumps(emoji).encode()), 2000 * 12)

    def test_b73_receipt_settles_with_real_rates_not_the_reservation_safety_factor(self):
        import claude_transport as ct
        body = {'model': 'm', 'max_tokens': 100, 'messages': [{'role': 'user', 'content': 'x'}]}
        pricing = ct.price(body, self.CATALOG)
        from decimal import Decimal as D
        self.assertEqual([float(x) for x in (pricing.settle_input, pricing.cache_write, pricing.cache_read)],
                         [float(ct.amount(v)) for v in (10, 12.5, 1.0)])
        self.assertEqual(float(pricing.input), float(ct.amount(10)) * 2, 'The reservation uses a conservative 2x amount')
        ident = self.ledger.reserve('claude', pricing.maximum, self.snapshot, {'run_id': 'r2'}, run_cap=None)
        parser = ct.Receipt(self.ledger, ident, pricing, False)
        parser.feed(json.dumps({'type': 'message', 'id': 'msg_1', 'usage': {
            'input_tokens': 1000, 'cache_creation_input_tokens': 2000, 'cache_read_input_tokens': 4000, 'output_tokens': 500}}).encode())
        parser.finish()
        from decimal import Decimal as D
        ratio = lambda v: D(str(ct.amount(v)))
        expected = ct.micro(1000 * ratio(10) + 2000 * ratio(12.5) + 4000 * ratio(1.0) + 500 * ratio(40))
        import sqlite3
        with closing(sqlite3.connect(self.ledger.path)) as con, con:
            self.assertEqual(con.execute('select actual from reservation where id=?', (ident,)).fetchone()[0], expected)
        inflated = ct.micro(7000 * pricing.input + 500 * pricing.output)
        self.assertLess(expected, inflated, 'Previous accounting exceeded the actual invoice')


class Round2BrakeTests(unittest.TestCase):
    """B69: when the run cap is full the Claude ladder is skipped and the job continues locally (user decision 2)."""

    def test_b69_run_cap_downgrade_skips_the_claude_ladder_and_waits_for_budget(self):
        import qe_core as core
        log = Path(tempfile.mkdtemp(prefix='qe-round2-brake-')) / 'runner.log'
        with patch.object(core.ROUTER, 'chain', return_value=(['claude/opus#high', 'opencode-go/kimi-k2.7-code'], [])), \
             patch.object(core.ROUTER, 'failure') as failure:
            self.assertEqual(core.quota_drop('claude/fable#xhigh', log, reason='run-cap'), '', 'run cap full -> budget-wait')
            failure.assert_not_called()  # a budget denial is not a provider failure (the same principle as B52)
            # T3 distinction: only if one provider's DAILY SHARE ran out can another paid provider in the chain be used
            self.assertEqual(core.quota_drop('claude/fable#xhigh', log, reason='pay'), 'opencode-go/kimi-k2.7-code')
            self.assertEqual(core.quota_drop('claude/fable#xhigh', log), 'claude/opus#high', 'Quota or limit reason preserves the ladder')

    def test_b333_ceiling_full_elimination_uses_the_bucket_not_the_provider_name(self):
        """B333 (03.10 02:37): when the cap was full quota_drop removed paid providers BY PROVIDER NAME; the free
        Zen models (opencode/nemotron-*-free, big-pickle) were also counted as 'opencode' and removed, and the job fell to local. G-B8: a free
        bucket (<prov>:free) does not enter the dollar cap — filtering is by model_catalog.is_free / bucket."""
        import qe_core as core
        log = Path(tempfile.mkdtemp(prefix='qe-b333-')) / 'runner.log'
        chain = ['opencode/minimax-m3', 'opencode/nemotron-3-ultra-free', 'opencode/big-pickle']
        free = lambda provider_key, entry_name: entry_name.endswith('-free') or entry_name == 'big-pickle'
        with patch.object(core.ROUTER, 'chain', return_value=(chain, [])), \
             patch.object(core.ROUTER, 'failure'), patch.object(core, 'request_measure', return_value=8000), \
             patch.object(core, 'log'), patch('model_catalog.is_free', side_effect=free):
            # cap full: the paid minimax is removed, the next candidate is the FREE Zen bucket — not local (acceptance criterion)
            self.assertEqual(core.quota_drop('opencode-go/glm-5.3-flash', log, reason='run-cap'),
                             'opencode/nemotron-3-ultra-free')
            # the exhausted bucket of the current PAID Zen does not close the same provider's free bucket (bucket exclusion)
            self.assertEqual(core.quota_drop('opencode/glm-5.2', log, reason='run-cap'),
                             'opencode/nemotron-3-ultra-free')
        with patch.object(core.ROUTER, 'chain', return_value=(['opencode/nemotron-3-ultra-free'], [])), \
             patch.object(core.ROUTER, 'failure'), patch.object(core, 'request_measure', return_value=8000), \
             patch.object(core, 'log'), patch('model_catalog.is_free', return_value=None):
            # catalogue price UNKNOWN (None) -> counts as paid, even if the name is '-free' (G-B8/D17) -> budget wait
            self.assertEqual(core.quota_drop('opencode-go/glm-5.3-flash', log, reason='run-cap'), '')


class Round2LifetimeAndBudgetTests(unittest.TestCase):
    """B74 orphaned lock · B76 Cerebras's own horizon · B77 DeepSeek sliding horizon (no end-of-month jump)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-round2b-'); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = RunStore(self.home)

    def test_b74_an_orphan_holding_the_lock_is_not_counted_as_a_live_coordinator(self):
        """The coordinator dies, a grandchild that inherited the lock fd keeps living: the old code saw the lock as held and counted the run
        as having a 'live coordinator' forever; the queue, revive and cancel could never step in."""
        import subprocess as sp
        rid = self.store.accept('a', 'x')['run_id']
        dead = sp.Popen([sys.executable, '-c', 'pass']); dead.wait()      # a dead coordinator's pid
        lock = run_store.FileLock(self.store.path(rid) / '.owner.lock')      # a living grandchild holds the lock (this process imitates it)
        self.assertTrue(lock.acquire(blocking=False))
        self.addCleanup(lock.close)
        self.store.update(rid, state='running', owner_pid=dead.pid)
        self.assertFalse(run_store.coordinator_live(self.store, self.store.read(rid)), 'A dead coordinator does not count as live')
        self.assertTrue(run_store.has_active(self.store, 'a'))
        lock.close()
        self.assertFalse(run_store.has_active(self.store, 'a'))
        self.assertTrue(lock.acquire(blocking=False))
        # B110: liveness is measured by pid + ARGV (so pid reuse does not revive the coordinator), so
        # a "living coordinator" fake must really be a process named `run_store.py`.
        fake = Path(self.temp.name) / 'run_store.py'
        fake.write_text('import time\ntime.sleep(30)\n')
        live = sp.Popen([sys.executable, str(fake)])
        self.addCleanup(lambda: (live.kill(), live.wait(timeout=5)))
        self.store.update(rid, owner_pid=live.pid)
        self.assertTrue(run_store.coordinator_live(self.store, self.store.read(rid)))
        self.assertTrue(run_store.has_active(self.store, 'a'))

    def test_b82_revive_actually_restarts_a_run_whose_coordinator_died_behind_an_orphan_lock(self):
        """T3: B74 was half done — `revive` still looked at the raw lock, so the recovery it targeted NEVER happened."""
        import subprocess as sp
        rid = self.store.accept('a', 'x')['run_id']
        dead = sp.Popen([sys.executable, '-c', 'pass']); dead.wait()
        lock = run_store.FileLock(self.store.path(rid) / '.owner.lock')       # an orphaned grandchild holds the lock
        self.assertTrue(lock.acquire(blocking=False)); self.addCleanup(lock.close)
        self.store.update(rid, state='running', owner_pid=dead.pid)
        with patch.object(run_store, '_start') as start:
            self.assertEqual(run_store.revive(self.store, self.home / 'code'), [rid])
        start.assert_called_once()
        # B110 (18.09): as long as the lock is with an ORPHANED grandchild a new coordinator returns at once with rc=75; this attempt
        # is not a real revival and must not burn the revive ALLOWANCE (old behaviour: `failed` in vain after 5 ticks).
        self.assertIsNone(self.store.read(rid).get('revive_count'), 'An orphan-held lock does not consume a revival')
        self.assertEqual(self.store.read(rid).get('orphan_lock_count'), 1)
        for _ in range(run_store.REVIVE_LIMIT):
            run_store.revive(self.store, self.home / 'code')
        record = self.store.read(rid)
        self.assertEqual(record['state'], 'failed', 'A persistent orphaned lock closes the run with its own reason')
        self.assertIn('orphaned lock', record['reason'])

    def test_b81_a_finished_run_whose_lock_is_orphaned_never_blocks_the_project_queue(self):
        """T3: if a finished run's lock stayed with an orphaned process the queue never opened (a violation of decision 3)."""
        first = self.store.accept('p', 'first')['run_id']
        second = self.store.accept('p', 'second')['run_id']
        self.assertEqual(self.store.read(second)['state'], 'queued')
        lock = run_store.FileLock(self.store.path(first) / '.owner.lock')
        self.assertTrue(lock.acquire(blocking=False)); self.addCleanup(lock.close)
        self.store.update(first, state='completed', owner_pid=None)        # execute_run sets the pid to None at the end
        self.assertFalse(run_store.coordinator_live(self.store, self.store.read(first)))
        self.assertTrue(run_store.has_active(self.store, 'p'))
        self.assertFalse(self.store.order_came(second))
        lock.close()
        self.assertFalse(run_store.has_active(self.store, 'p'))
        self.assertTrue(self.store.order_came(second))

    def test_b83_cancel_losing_the_race_with_completion_leaves_the_finished_run_intact(self):
        """T3: the run may have finished AFTER the lock was released; the old code wrote cancelled without looking again (work lost)."""
        rid = self.store.accept('a', 'x')['run_id']
        self.store.update(rid, state='running', owner_pid=os.getpid())
        stale = self.store.read(rid)
        real_read = self.store.read
        call = {'n': 0}
        def race(run_id):                      # the run has finished AFTER the 2nd read (while the owner lock is being taken)
            call['n'] += 1
            if call['n'] == 2:                 # write directly without taking the lock (update would try to take .record.lock again)
                finished = dict(real_read(run_id), state='completed', exit_code=0, owner_pid=None, updated=time.time())
                run_store.atomic_json(self.store.path(run_id) / 'run.json', finished)
            return real_read(run_id)
        with patch.object(self.store, 'read', side_effect=race):
            self.assertFalse(run_store.cancel_run(self.store, stale))
        record = self.store.read(rid)
        self.assertEqual(record['state'], 'completed')
        self.assertNotEqual(record.get('exit_code'), 130)
        self.assertFalse((self.store.path(rid) / 'cancel.flag').exists(), 'The flag must be removed')

    def budget_report(self, credit_end_day=10, ds_balance=4.10, today=None):
        """The real `provider_budget.main()` in an isolated home: no network (ds_balance fake), no opencode DB (spending fake)."""
        import datetime, io, contextlib
        import provider_budget
        (self.home / 'chain').mkdir(parents=True, exist_ok=True)
        today = today or datetime.date.today()
        end = (today + datetime.timedelta(days=credit_end_day)).isoformat()
        start = (today - datetime.timedelta(days=20)).isoformat()
        (self.home / 'chain/cerebras-credit.txt').write_text(f'{start} 5.00 expire:{end}\n')
        output = io.StringIO()
        with patch.object(provider_budget, 'QE', self.home), patch.object(provider_budget, 'DB', self.home / 'absent.db'), \
             patch.dict(os.environ, {'CADRE_PREPAID_MODE': 'daily'}), \
             patch.object(provider_budget, 'spending', return_value=(0.0, 0)), \
\
             patch.object(provider_budget, 'go_spend', return_value=0.0), contextlib.redirect_stdout(output):
            provider_budget.main()
        return json.loads(output.getvalue())


if __name__ == '__main__':
    unittest.main()


class FailOpenTests(unittest.TestCase):
    """B114-8 and B114-9 (19.09 audit): in two places `except Exception` swallowed the budget read and
    meant "no limit". The `BudgetDenied` branches are already fail-closed; the problem is non-budget errors
    like sqlite/disk. Rule: if the cap CANNOT BE READ the cloud is closed — an unknown limit is not unlimited."""

    def _selected_model(self, args):
        """The model in effect at the end of the opencode call: `--model=` or `-m`."""
        for i, a in enumerate(args):
            if a.startswith('--model='):
                return a.split('=', 1)[1]
            if a in ('-m', '--model') and i + 1 < len(args):
                return args[i + 1]
        return None

    def _choice(self, cap_outcome):
        """A call that asks for a paid model; the ONLY variable is the run's remaining cap."""
        import qe_core as core
        divide = {'ts': '2026-09-19T00:00:00', 'today': '2026-09-19',
               'go': {'db': 'ok', 'pay': 33.333334, 'available': 33.333334, 'window_open': True, 'unit': 'points'}}
        env = {'CADRE_RUN_ID': 'r1', 'CADRE_ROLE': 'impl'}
        patch_text = ({'side_effect': cap_outcome} if isinstance(cap_outcome, BaseException)
                else {'return_value': cap_outcome})
        with patch.object(core, 'budget_json', return_value=divide), \
             patch.object(core, 'run_record', return_value={}), \
             patch.object(core, 'available_budget', return_value=1_000_000), \
             patch.object(core, 'run_remaining_usd', **patch_text):
            args = core.opencode_budget_env(dict(env), ['-m', 'opencode-go/glm-5.3-flash', 'task'])
        return self._selected_model(args)

    def test_an_unreadable_run_cap_closes_the_cloud_instead_of_opening_it(self):
        open = self._choice(5.0)
        self.assertFalse(open.startswith('lmstudio/'),
                         f'Check: a paid model must be selectable when the cap is readable; got {open!r}')
        # P3 (03.10): local is off by default; while the cap CANNOT BE READ, fail-closed = budget denial
        # (no local fallback — not fail-open, the call is denied).
        with self.assertRaises(BudgetDenied):
            self._choice(sqlite3.OperationalError('database is locked'))

    def test_an_unreadable_run_cap_still_writes_the_provider_brake(self):
        import qe_core as core
        b = {'go': {'db': 'ok', 'pay': 33.333334, 'available': 33.333334, 'window_open': True, 'unit': 'points'}}
        with patch.dict(os.environ, {'CADRE_RUN_ID': 'r1'}, clear=False):
            with patch.object(core, 'run_remaining_usd', side_effect=sqlite3.OperationalError('disk I/O error')):
                provider, reason = core.provider_brake(b)
        self.assertIsNotNone(provider, 'No brake was recorded for an unreadable cap, leaving it silently unrestricted')
        self.assertIn('cap', (reason or '').lower(),
                      f'The brake reason must name the run cap; another rule may have triggered it: {reason!r}')


class RaiseDoesNotCutProcessTests(unittest.TestCase):
    """B133 (20.09, live RCA): the "Go daily share came back" escalation killed a RUNNING HEALTHY process
    with `kill_tree` and restarted it from the recorded session. The restarted process produced a `text`
    part and lost the flow, and the guard cut it 600 s later.

    Correlation 3/3, to the second:
        00:36:12 ESCALATION -> 00:46:34 STALL   (10 min 22 s)
        01:05:56 ESCALATION -> 01:16:18 STALL   (10 min 22 s)
        01:37:13 ESCALATION -> 01:47:36 STALL   (10 min 23 s)
    Three events = 31 minutes lost; the gain was a few cents of helper agents."""

    SOURCE = Path(__file__).resolve().parents[1] / 'lib/runner.py'

    def _rise_block(self):
        """The CODE OF THE BLOCK excluding comment lines. (A comment mentioning 'kill_tree' describes the old behaviour;
        the test must look at the code itself, not at its explanation.)"""
        source = self.SOURCE.read_text()
        i = source.index('if go_restriction_active and "opencode-go" not in forbidden')
        j = source.index('except Exception as _e:', i)
        return '\n'.join(s for s in source[i:j].split('\n') if not s.lstrip().startswith('#'))

    def test_the_go_upgrade_never_kills_a_healthy_running_process(self):
        block = self._rise_block()
        self.assertNotIn('kill_tree', block,
                         'Killing a running process to return to Go loses over 10 minutes each time')
        self.assertNotIn('break', block, 'Leaving the process loop also restarts execution')

    def test_the_decision_is_still_recorded_for_the_next_start(self):
        """The fix must not lose the gain: the decision is still written, only its application is deferred."""
        block = self._rise_block()
        self.assertIn('restriction_env', block, 'Save the new config for the next start')
        self.assertIn('go_restriction_active = False', block, 'Update the restriction state')


