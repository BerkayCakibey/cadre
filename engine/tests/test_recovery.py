"""Item 0a — the recovery path. When the engine crashes a reservation stays 'actual is null' and locks the cap
forever; on a price violation the provider closes completely with pricing_fault. These tests prove the CLI runs as a real
process and, most importantly, that CLOSING DOES NOT SILENTLY GIVE MONEY BACK."""

from __future__ import annotations

import json
import os
from pathlib import Path
from contextlib import closing
import sqlite3
import subprocess
import sys
import time
import unittest

import test_run_sessions as fixtures
import budget_gate as budget
from test_budget_period import snapshot

CLI = Path(budget.__file__).resolve()


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.db = self.f.home / 'chain/request-budget.db'
        self.ledger = budget.BudgetLedger(self.db)

    def cli(self, *args):
        env = {**os.environ, 'CADRE_HOME': str(self.f.home), 'CADRE_ENGINE_ROOT': str(CLI.parents[1])}
        return subprocess.run([sys.executable, str(CLI), *args], env=env, capture_output=True, text=True, timeout=60)

    def age(self, hour=10.0):
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute('update reservation set created=created-? where actual is null', (hour * 3600,))

    def pending(self):
        with closing(sqlite3.connect(self.db)) as con, con:
            return con.execute('select count(*) from reservation where actual is null').fetchone()[0]

    def cash(self):
        # 4c-13: on Go the truth is the provider's own percentage; the cash route where the ledger is the truth (Zen) is measured here.
        b = snapshot()
        b['zen'] = {'db': 'ok', 'pay': 4.0, 'available': 4.0}   # $3.40 with a 15% margin
        return b

    def test_recovery_also_closes_the_monthly_twin_of_each_closed_reservation(self):
        import monthly_budget
        monthly = monthly_budget.MonthlyLedger(self.f.home / 'chain/monthly-budget.db', cap=65_000_000)
        ident = self.ledger.reserve('opencode', 2_000_000, self.cash(), {'run_id': 'r1', 'model': 'm1', 'session_id': 's1'})
        silent = self.ledger.reserve('opencode', 1_000_000, self.cash(), {'run_id': 'r2', 'model': 'm1'})
        twin = monthly.reserve('opencode', 2_000_000, {'model': 'm1', 'session_id': 's1'})
        live = monthly.reserve('opencode', 1_000_000, {'model': 'm1', 'session_id': 's2'})
        other = monthly.reserve('opencode', 1_000_000, {'model': 'm2', 'session_id': 's1'})
        self.age()
        with closing(sqlite3.connect(self.f.home / 'chain/monthly-budget.db')) as con, con:
            con.execute('update reservation set created=created-? where id in (?,?)', (10 * 3600, twin, live))
            formation = con.execute('select created from reservation where id=?', (twin,)).fetchone()[0]
            con.execute('update reservation set created=? where id=?', (formation + 0.01, live))
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute('update reservation set created=? where id in (?,?)', (formation + 0.01, ident, silent))
        r = self.cli('--stale-close', '--yes', '--zero')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('1 matching entries closed in the monthly ledger', r.stdout)
        with closing(sqlite3.connect(self.f.home / 'chain/monthly-budget.db')) as con:
            status = dict(con.execute('select id, actual from reservation').fetchall())
        self.assertEqual((status[twin], status[live], status[other]), (0, None, None),
                         'Do not modify monthly records for another session using the same model or for sessionless rows')

    def test_monthly_twin_is_matched_by_its_id_or_same_session_even_when_created_seconds_apart(self):
        import monthly_budget
        monthly = monthly_budget.MonthlyLedger(self.f.home / 'chain/monthly-budget.db', cap=65_000_000)
        bound = monthly.reserve('opencode', 2_000_000, {'model': 'm1', 'session_id': 's1'})
        old = monthly.reserve('opencode', 1_000_000, {'model': 'm1', 'session_id': 's2'})
        self.ledger.reserve('opencode', 2_000_000, self.cash(), {'run_id': 'r1', 'model': 'm1', 'session_id': 's1', 'monthly_id': bound})
        self.ledger.reserve('opencode', 1_000_000, self.cash(), {'run_id': 'r2', 'model': 'm1', 'session_id': 's2'})
        self.age()
        with closing(sqlite3.connect(self.f.home / 'chain/monthly-budget.db')) as con, con:
            con.execute('update reservation set created=created-? where id in (?,?)', (10 * 3600 + 60, bound, old))
        r = self.cli('--stale-close', '--yes', '--zero')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('2 matching entries closed in the monthly ledger', r.stdout)

    def test_automatic_ghost_close_also_settles_the_monthly_twin(self):
        import monthly_budget
        monthly = monthly_budget.MonthlyLedger(self.f.home / 'chain/monthly-budget.db', cap=65_000_000)
        twin = monthly.reserve('opencode', 2_000_000, {'model': 'm1', 'session_id': 's1'})
        self.ledger.reserve('opencode', 2_000_000, self.cash(), {'run_id': 'dead-run', 'model': 'm1', 'session_id': 's1', 'monthly_id': twin})
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute('update reservation set created=created-?', (3600,))
        from unittest.mock import patch
        with patch.object(budget, 'is_owner_dead', return_value=True), patch.object(budget, 'opencode_receipt', return_value=None):
            outcome = budget.phantom_close(self.f.home, age_min=30)
        self.assertEqual((outcome['closed'], outcome['monthly']), (1, 1))
        with closing(sqlite3.connect(self.f.home / 'chain/monthly-budget.db')) as con:
            self.assertEqual(con.execute('select actual from reservation where id=?', (twin,)).fetchone()[0], 2_000_000)

    def test_restart_repairs_a_monthly_twin_after_request_settlement_was_interrupted(self):
        import monthly_budget
        from unittest.mock import patch
        monthly = monthly_budget.MonthlyLedger(self.f.home / 'chain/monthly-budget.db', cap=65_000_000)
        twin = monthly.reserve('opencode', 500_000, {'session_id': 'paired', 'model': 'm'})
        ident = self.ledger.reserve('opencode', 500_000, self.cash(), {'run_id': 'old', 'monthly_id': twin,
                                   'session_id': 'paired', 'model': 'm'})
        self.age()
        with patch.object(budget, 'monthly_pair_close', side_effect=OSError('interrupted between ledgers')):
            with self.assertRaises(OSError):
                budget.recovery(self.f.home, ['--stale-close', '--yes'])
        with self.ledger.transaction() as con:
            self.assertEqual(con.execute('select actual from reservation where id=?', (ident,)).fetchone()[0], 500_000)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.cli('--stale-close', '--yes'), range(2)))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
        with monthly.transaction() as con:
            self.assertEqual(con.execute('select actual from reservation where id=?', (twin,)).fetchone()[0], 500_000)
            self.assertEqual(con.execute('select count(*) from reservation').fetchone()[0], 1)

    def test_ambiguous_legacy_monthly_pairs_are_preserved_and_an_id_cannot_settle_another_provider(self):
        import monthly_budget
        monthly = monthly_budget.MonthlyLedger(self.f.home / 'chain/monthly-budget.db', cap=65_000_000)
        twins = [monthly.reserve('opencode', 500_000, {'session_id': 'shared', 'model': 'm'}) for _ in range(2)]
        now = time.time()
        self.assertEqual(budget.monthly_pair_close(self.f.home, 'opencode', now, {'session_id': 'shared', 'model': 'm'}, 100_000), 0)
        self.assertEqual(budget.monthly_pair_close(self.f.home, 'openrouter', now, {'monthly_id': twins[0]}, 100_000), 0)
        with monthly.transaction() as con:
            self.assertTrue(all(actual is None for (actual,) in con.execute('select actual from reservation')))

    def test_sigkill_between_ledgers_repairs_a_unique_legacy_session_pair_on_restart(self):
        import monthly_budget
        monthly = monthly_budget.MonthlyLedger(self.f.home / 'chain/monthly-budget.db', cap=65_000_000)
        twin = monthly.reserve('opencode', 500_000, {'session_id': 'legacy', 'model': 'm'})
        ident = self.ledger.reserve('opencode', 500_000, self.cash(), {'run_id': 'old', 'session_id': 'legacy', 'model': 'm'})
        self.age()
        with monthly.transaction() as con:
            con.execute('update reservation set created=created-?', (36000,))
        script = ('import os,sys,signal;from pathlib import Path;sys.path.insert(0,sys.argv[1]);'
                  'import budget_gate as b;b.monthly_pair_close=lambda *a:os.kill(os.getpid(),signal.SIGKILL);'
                  'b.recovery(Path(sys.argv[2]),["--stale-close","--yes"])')
        killed = subprocess.run([sys.executable, '-c', script, str(CLI.parent), str(self.f.home)], capture_output=True, text=True, timeout=30)
        self.assertEqual(killed.returncode, -9, killed.stderr)
        with self.ledger.transaction() as con:
            self.assertEqual(con.execute('select actual from reservation where id=?', (ident,)).fetchone()[0], 500_000)
        for _ in range(2):
            result = self.cli('--stale-close', '--yes')
            self.assertEqual(result.returncode, 0, result.stderr)
        with monthly.transaction() as con:
            self.assertEqual(con.execute('select actual from reservation where id=?', (twin,)).fetchone()[0], 500_000)

    def test_stale_reservation_is_visible_and_closing_it_does_not_hand_money_back(self):
        ident = self.ledger.reserve('opencode', 2_000_000, self.cash(), {'run_id': 'r1', 'task_id': 'g1'})
        self.assertEqual(self.ledger.available('opencode', self.cash()), 1_400_000)
        # a fresh reservation is not stale
        r = self.cli('--stale-list')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('No pending reservation', r.stdout)
        self.age()
        r = self.cli('--stale-list')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(ident[:8], r.stdout)
        self.assertIn('2.0000', r.stdout)
        self.assertIn('r1', r.stdout)
        # nothing changes without --yes
        r = self.cli('--stale-close')
        self.assertEqual(r.returncode, 2)
        self.assertIn('--yes', r.stderr)
        self.assertEqual(self.pending(), 1)
        # --yes: it closes but the reserved amount counts as SPENT -> capacity does not open
        r = self.cli('--stale-close', '--yes')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.pending(), 0)
        self.assertEqual(self.ledger.available('opencode', self.cash()), 1_400_000,
                         'Closing must not silently release budget')
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('opencode', 1_500_000, self.cash(), {})
        record = [json.loads(l) for l in (self.f.home / 'chain/recovery.log').read_text().splitlines()]
        self.assertEqual(record[-1]['operation'], 'stale-close')
        self.assertFalse(record[-1]['zero'])
        self.assertEqual(record[-1]['closed'], 1)

    def test_zero_is_the_deliberate_refund_and_restores_capacity(self):
        self.ledger.reserve('opencode-go', 1_700_000, snapshot(), {'run_id': 'r2'})
        self.age()
        r = self.cli('--stale-close', '--zero', '--yes')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('funds released', r.stdout)
        self.assertEqual(self.pending(), 0)
        self.assertGreater(self.ledger.available('opencode-go', snapshot()), 1_000_000)
        self.ledger.reserve('opencode-go', 500_000, snapshot(), {})

    def test_hour_filter_selects_what_is_closed(self):
        self.ledger.reserve('opencode-go', 100_000, snapshot(), {})
        self.age(4)                              # 4 hours
        r = self.cli('--stale-list')            # default 6 hours -> does not see it
        self.assertIn('No pending reservation', r.stdout)
        r = self.cli('--stale-list', '--hour', '3')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('total 1', r.stdout)
        r = self.cli('--stale-close', '--hour', 'abc')
        self.assertEqual(r.returncode, 2)

    def test_pricing_fault_locks_the_provider_and_only_an_explicit_clear_reopens_it(self):
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute('create table if not exists pricing_fault(provider text primary key)')
            con.execute("insert or ignore into pricing_fault values('opencode-go')")
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('opencode-go', 1000, snapshot(), {})
        r = self.cli('--failure-list')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('opencode-go', r.stdout)
        r = self.cli('--failure-clean')
        self.assertEqual(r.returncode, 2)
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('opencode-go', 1000, snapshot(), {})
        r = self.cli('--failure-clean', 'deepseek', '--yes')   # a provider not in the list
        self.assertEqual(r.returncode, 2)
        r = self.cli('--failure-clean', '--yes')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.ledger.reserve('opencode-go', 1000, snapshot(), {})
        record = [json.loads(l) for l in (self.f.home / 'chain/recovery.log').read_text().splitlines()]
        self.assertEqual(record[-1], {**record[-1], 'operation': 'failure-clean', 'providers': ['opencode-go']})

    def test_settled_reservations_are_never_touched(self):
        ident = self.ledger.reserve('opencode-go', 100_000, snapshot(), {})
        self.ledger.settle(ident, 40_000, 'real-slip')
        self.age()
        r = self.cli('--stale-list')
        self.assertIn('No pending reservation', r.stdout)
        with closing(sqlite3.connect(self.db)) as con, con:
            self.assertEqual(con.execute('select actual,receipt from reservation where id=?', (ident,)).fetchone(),
                             (40_000, 'real-slip'))


class PhantomClosingTests(unittest.TestCase):
    """B85: a pending reservation whose owner DIED and that is stale closes automatically; a run whose owner is ALIVE is not touched.
    Why it matters: the reservation is written before the network and is a safe UPPER BOUND (measured $2.78 reserved ->
    $0.32 receipt), so a single crash locked more than half of a $5 cap with unspent money."""

    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.home = self.f.home
        self.ledger = budget.BudgetLedger(self.home / 'chain/request-budget.db')

    def execute_run(self, pid):
        from run_store import RunStore
        from unittest.mock import patch
        store = RunStore(self.home)
        with patch.dict('os.environ', {'CADRE_RUN_CAP_USD': '5'}):
            rid = store.accept('phantom', 'task')['run_id']
        store.update(rid, budget_cap_usd=5.0, owner_pid=pid, state='running')
        return rid

    def reserved(self, rid, micro, age_sec):
        import sqlite3
        from contextlib import closing as _c
        ident = self.ledger.reserve('opencode-go', micro, snapshot(50.0), {'run_id': rid},
                                    run_cap=5_000_000, slice='lanes')
        with _c(sqlite3.connect(self.home / 'chain/request-budget.db')) as con:
            con.execute('update reservation set created=? where id=?', (time.time() - age_sec, ident))
            con.commit()
        return ident

    def test_a_hold_owned_by_a_live_process_is_never_touched(self):
        # B110 (18.09): ownership is now measured by pid + ARGV (so pid reuse does not revive a dead owner),
        # so a "living owner" fake must really be a process named `run_store.py`.
        import subprocess
        import sys as _sys
        fake = Path(self.home) / 'run_store.py'
        fake.write_text('import time\ntime.sleep(60)\n')
        live = subprocess.Popen([_sys.executable, str(fake)])
        self.addCleanup(lambda: (live.kill(), live.wait(timeout=5)))
        rid = self.execute_run(live.pid)            # the owner is ALIVE and really looks like a coordinator process
        self.reserved(rid, 2_780_000, 3600)
        outcome = budget.phantom_close(self.home, age_min=30)
        self.assertEqual(outcome['closed'], 0, 'The call may still be in flight while its owner is alive')

    def test_a_hold_whose_pid_was_recycled_by_an_unrelated_process_is_closed(self):
        """B110: when a pid was reused an unrelated process showed the owner as 'alive' forever and
        the ghost reservation never closed."""
        rid = self.execute_run(os.getpid())          # test process: alive but NOT `run_store.py`
        self.reserved(rid, 2_780_000, 3600)
        self.assertEqual(budget.phantom_close(self.home, age_min=30)['closed'], 1)

    def test_a_stale_hold_from_a_dead_owner_is_closed_as_spent_and_reversible(self):
        rid = self.execute_run(999_999)              # a pid that does not exist: the owner is DEAD
        self.reserved(rid, 2_780_000, 3600)
        outcome = budget.phantom_close(self.home, age_min=30)
        self.assertEqual(outcome['closed'], 1)
        self.assertEqual(outcome['amount_micro'], 2_780_000)
        # counted as SPENT (an error on the money side) but REVERSIBLE with the `phantom:` stamp
        import sqlite3
        from contextlib import closing as _c
        with _c(sqlite3.connect(self.home / 'chain/request-budget.db')) as con:
            real, receipt = con.execute('select actual,receipt from reservation').fetchone()
        self.assertEqual(real, 2_780_000)
        self.assertTrue(receipt.startswith('phantom:'))
        budget.recovery(self.home, ['--phantom-go-back', '--yes'])
        with _c(sqlite3.connect(self.home / 'chain/request-budget.db')) as con:
            self.assertEqual(con.execute('select actual from reservation').fetchone()[0], 0)

    def test_another_runs_coordinator_cannot_keep_a_recycled_pid_alive_in_either_recovery_path(self):
        from run_store import RunStore, process_identity
        other = self.execute_run(0)
        script = self.home / 'run_store.py'
        script.write_text('import time\ntime.sleep(60)\n')
        process = subprocess.Popen([sys.executable, str(script), 'run', other])
        self.addCleanup(lambda: (process.kill(), process.wait(timeout=5)))
        for manual in (False, True):
            rid = self.execute_run(process.pid)
            previous = process_identity(process.pid)
            if not manual:
                previous = {**previous, 'started': 'previous-process'}
            RunStore(self.home).update(rid, owner_identity=previous)
            ident = self.reserved(rid, 200_000, 36000)
            if manual:
                result = RecoveryTests.cli(self, '--stale-close', '--yes')
                self.assertEqual(result.returncode, 0, result.stderr)
            else:
                self.assertEqual(budget.phantom_close(self.home, age_min=30)['closed'], 1)
            with self.ledger.transaction() as con:
                self.assertEqual(con.execute('select actual from reservation where id=?', (ident,)).fetchone()[0], 200_000)
            self.assertFalse(budget.is_owner_live(self.home, rid))

    def test_matching_coordinator_identity_run_and_held_lock_preserve_the_request(self):
        from run_store import RunStore, process_identity
        rid = self.execute_run(0)
        script = self.home / 'run_store.py'
        script.write_text('import fcntl,pathlib,sys,time\n'
                          'p=pathlib.Path(sys.argv[3])/"runs"/sys.argv[2]/".owner.lock"\n'
                          'f=p.open("a"); fcntl.flock(f,fcntl.LOCK_EX)\nprint("ready",flush=True)\ntime.sleep(60)\n')
        process = subprocess.Popen([sys.executable, str(script), 'run', rid, str(self.home)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (process.kill(), process.wait(timeout=5), process.stdout.close()))
        self.assertEqual(process.stdout.readline().strip(), 'ready')
        RunStore(self.home).update(rid, owner_pid=process.pid, owner_identity=process_identity(process.pid))
        ident = self.reserved(rid, 200_000, 36000)
        self.assertTrue(budget.is_owner_live(self.home, rid))
        self.assertEqual(budget.phantom_close(self.home, age_min=30)['closed'], 0)
        result = RecoveryTests.cli(self, '--stale-close', '--yes')
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.ledger.transaction() as con:
            self.assertIsNone(con.execute('select actual from reservation where id=?', (ident,)).fetchone()[0])

    def test_unknown_process_observation_is_recorded_and_never_closes_an_inflight_hold(self):
        from unittest.mock import patch
        import io
        rid = self.execute_run(os.getpid())
        ident = self.reserved(rid, 200_000, 36000)
        with patch('run_store.process_command', return_value=''), patch('run_store.process_identity', return_value=None):
            result = budget.phantom_close(self.home, age_min=30)
            self.assertEqual(result['closed'], 0)
            self.assertIn(rid, result['uncertain_runs'])
            with patch('sys.stdout', io.StringIO()):
                self.assertEqual(budget.recovery(self.home, ['--stale-close', '--yes']), 0)
        with self.ledger.transaction() as con:
            self.assertIsNone(con.execute('select actual from reservation where id=?', (ident,)).fetchone()[0])
        self.assertIn('ownership-uncertain', (self.home / 'chain/recovery.log').read_text())

    def test_a_young_hold_is_left_alone_even_if_the_owner_is_dead(self):
        rid = self.execute_run(999_999)
        self.reserved(rid, 1_000_000, 60)      # 1 minute
        self.assertEqual(budget.phantom_close(self.home, age_min=30)['closed'], 0)

    def test_missing_receipt_closes_at_the_verified_bound_in_both_recovery_paths(self):
        from unittest.mock import patch
        for manual in (False, True):
            rid = self.execute_run(999_999)
            with patch.object(self.ledger, 'calibrated', return_value=70_000):
                ident = self.reserved(rid, 700_000, 36000)
            if manual:
                result = RecoveryTests.cli(self, '--stale-close', '--yes')
                self.assertEqual(result.returncode, 0, result.stderr)
            else:
                budget.phantom_close(self.home, age_min=30)
            with self.ledger.transaction() as con:
                self.assertEqual(con.execute('select actual from reservation where id=?', (ident,)).fetchone()[0], 700_000)

    def test_a_coordinator_starting_after_the_dead_observation_cannot_race_recovery(self):
        from run_store import FileLock, RunStore
        from unittest.mock import patch
        rid = self.execute_run(999_999)
        ident = self.reserved(rid, 200_000, 36000)
        lock = FileLock(RunStore(self.home).path(rid) / '.owner.lock')
        self.addCleanup(lock.close)
        original = budget.is_owner_dead
        def race(home, run_id):
            dead = original(home, run_id)
            self.assertTrue(lock.acquire(blocking=False))
            return dead
        with patch.object(budget, 'is_owner_dead', side_effect=race):
            self.assertEqual(budget.phantom_close(self.home, age_min=30)['closed'], 0)
        with self.ledger.transaction() as con:
            self.assertIsNone(con.execute('select actual from reservation where id=?', (ident,)).fetchone()[0])

    def test_token_receipts_reconcile_only_unique_owned_messages(self):
        from run_store import RunStore
        oc = self.home / 'opencode.db'
        with closing(sqlite3.connect(oc)) as con, con:
            con.execute('create table message (id text, session_id text, time_created integer, data text)')
        for case in ('measured', 'catalogue', 'zero-write', 'missing', 'wrong-model', 'wrong-session', 'ambiguous', 'incomplete', 'bad-tokens', 'reused'):
            with self.subTest(case=case):
                rid = self.execute_run(999_999)
                sid = 's-' + case
                RunStore(self.home).task_update(rid, 'g1', executors={'opencode:impl': {'sessions': [sid]}})
                ident = self.reserved(rid, 1_000_000, 3600)
                with self.ledger.transaction() as con:
                    created = con.execute('select created from reservation where id=?', (ident,)).fetchone()[0]
                    meta = {
                        'run_id': rid, 'model': 'qwen3.8-flash', 'session_id': sid, 'factor': 2,
                        'pricing': {'freshRate': 1, 'outRate': 2, 'readRate': 0.1, 'writeRate': 0 if case == 'zero-write' else 2}}
                    if case == 'catalogue':
                        del meta['pricing']
                    con.execute('update reservation set metadata=? where id=?', (json.dumps(meta), ident))
                    if case == 'reused':
                        con.execute("update reservation set receipt=? where id=(select id from reservation where actual is not null limit 1)",
                                    ('other:msg-' + case,))
                message = {'role': 'assistant', 'providerID': 'opencode-go',
                           'modelID': 'other' if case == 'wrong-model' else 'qwen3.8-flash',
                           'time': {'completed': None if case == 'incomplete' else (created + 10) * 1000},
                           'tokens': {'input': 100, 'output': -1 if case == 'bad-tokens' else 10,
                                      'cache': {'read': 900, 'write': 50}}}
                with closing(sqlite3.connect(oc)) as con, con:
                    for i in range(0 if case == 'missing' else 2 if case == 'ambiguous' else 1):
                        con.execute('insert into message values(?,?,?,?)',
                                    ('msg-' + case + ('-2' if i else ''), 'other' if case == 'wrong-session' else sid,
                                     (created - 1) * 1000, json.dumps(message)))
                result = budget.phantom_close(self.home, age_min=30)
                expected = {'measured': 620, 'catalogue': 89, 'zero-write': 520}.get(case, 1_000_000)
                self.assertEqual(result['amount_micro'], expected)
                with self.ledger.transaction() as con:
                    actual, receipt = con.execute('select actual,receipt from reservation where id=?', (ident,)).fetchone()
                self.assertEqual(actual, expected)
                self.assertTrue(receipt.startswith('opencode:' if expected < 1_000_000 else 'phantom:'))
        snapshot = {'go': {'db': 'ok', 'source': 'api', 'month_points': 2},
                    'zen': {'db': 'ok', 'spent': 3, 'remaining': 4.55}}
        line = budget.daily_reconciliation(self.home, snapshot)
        self.assertIn('"zen_remaining_usd": 4.55', line)
        self.assertIn('"go_month_points": 2', line)
        self.assertEqual(budget.daily_reconciliation(self.home, snapshot), '')


if __name__ == '__main__':
    unittest.main()


class RecoveryVisibility(unittest.TestCase):
    """The recovery path helps only if it is visible: `cadre status` must warn, the `cadre recover` command must work."""

    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory(prefix='qe-recover-', ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        (self.home / 'chain').mkdir(parents=True)
        # Fake code root: lib is real, bin is EMPTY -> `guard-revive` runs but finds no script to start,
        # so the status screen does not release real guard processes into the test home (16.09 P1 rule: guards from the code root).
        self.root = Path(self.temp.name) / 'root'
        (self.root / 'bin').mkdir(parents=True)
        (self.root / 'lib').symlink_to(CLI.parent)
        db = self.home / 'chain/request-budget.db'
        with closing(sqlite3.connect(db)) as con, con:
            con.execute('create table if not exists reservation(id text primary key, provider text not null, '
                        'period text not null, created real not null, reserved integer not null, actual integer, '
                        'receipt text unique, metadata text not null)')
            con.execute('create table if not exists pricing_fault(provider text primary key)')

    def cadre(self, *args):
        env = {**os.environ, 'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(self.root)}
        env.pop('CADRE_RUN_ID', None)
        return subprocess.run(['bash', str(CLI.parents[1] / 'bin/cadre.sh'), *args],
                              env=env, capture_output=True, text=True, timeout=90, stdin=subprocess.DEVNULL)

    def broken_hale_fetch(self):
        import time
        import uuid
        with closing(sqlite3.connect(self.home / 'chain/request-budget.db')) as con, con:
            con.execute('insert into reservation values(?,?,?,?,?,null,null,?)',
                        (uuid.uuid4().hex, 'opencode-go', 'p', time.time() - 9 * 3600, 1_234_567, '{"run_id":"r9"}'))
            con.execute("insert or ignore into pricing_fault values('deepseek')")

    def test_status_is_silent_when_healthy_and_names_the_command_when_not(self):
        r = self.cadre('status')
        self.assertNotIn('recovery', r.stdout)
        self.broken_hale_fetch()
        r = self.cadre('status')
        self.assertIn('stale reservation(s) ($1.23)', r.stdout)
        self.assertIn('cadre recover', r.stdout)
        self.assertIn('price failure', r.stdout)
        self.assertIn('deepseek', r.stdout)

    def test_recover_subcommand_reaches_the_cli(self):
        self.broken_hale_fetch()
        r = self.cadre('recover', '--stale-list')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('total 1 reservation', r.stdout)
        r = self.cadre('recover')                       # default: list, change nothing
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('total 1 reservation', r.stdout)
        with closing(sqlite3.connect(self.home / 'chain/request-budget.db')) as con, con:
            self.assertEqual(con.execute('select count(*) from reservation where actual is null').fetchone()[0], 1)
