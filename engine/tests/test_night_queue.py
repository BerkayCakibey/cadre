"""P4: a second task in the same project does NOT OPEN a second coordinator, it enters the queue and starts by itself when the one ahead
finishes (coordinator hand-off or guard tick). A `retryable` run is retried at most 2 times overnight, the counter in run.json
is persistent, failed on the 3rd; 0 attempts with a cancel flag. A run that was accepted but whose coordinator never opened is started by the guard.
Real cadre.sh/run_store.py; fake dispatcher/engine; no model/network."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import test_end_to_end as e2e  # noqa: E402
import run_store  # noqa: E402
from run_store import RunStore  # noqa: E402


class QueueEndToEndTests(e2e.E2EFixture):
    def setUp(self):
        super().setUp()
        (self.root / 'bin/cadre-dispatch.sh').write_text('#!/bin/bash\nexit 3\n')
        (self.root / 'bin/job-runner.sh').write_text(
            '#!/bin/bash\necho "$CADRE_RUN_ID" >> "$CADRE_HOME/runner-starts"\n'
            'while [ ! -f "$CADRE_HOME/release-$CADRE_RUN_ID" ]; do sleep 0.1; done\nexit 130\n')
        self.env.update(CADRE_COORD_TICK='0.1')
        self.max_active = 0
        self.stop = threading.Event()
        self.watcher = threading.Thread(target=self.watch_active, daemon=True)
        self.watcher.start()
        self.addCleanup(self.stop.set)
        self.addCleanup(lambda: subprocess.run(['pkill', '-9', '-f', str(self.root)], capture_output=True))

    def watch_active(self):
        while not self.stop.is_set():
            try:
                self.max_active = max(self.max_active, len(run_store.active_runs(self.store)))
            except (OSError, ValueError):
                pass
            time.sleep(.05)

    def submit_raw(self, text):
        result = subprocess.run(['bash', str(self.root / 'bin/cadre.sh'), 'task'], input=text + '\ngo\nyes\nyes\n', cwd=self.repo,
                                env=self.env, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.store.list('e2e')[-1]['run_id'], result.stdout

    def starts(self):
        path = self.home / 'runner-starts'
        return path.read_text().split() if path.exists() else []

    def wait_for(self, predicate, timeout, what):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.05)
        self.fail(f'timeout: {what}')

    def test_second_task_in_the_same_project_is_queued_and_starts_only_after_the_first_ends(self):
        a, out_a = self.submit_raw('first task')
        self.assertIn('Task saved', out_a)
        self.wait_for(lambda: self.starts() == [a], 10, 'first engine start')
        b, out_b = self.submit_raw('second task')
        self.assertIn('Task QUEUED', out_b)
        self.assertNotIn('Task saved', out_b)
        record_b = self.store.read(b)
        self.assertEqual(record_b['state'], 'queued')
        self.assertEqual(record_b['queued_behind'], [a])
        time.sleep(1)
        self.assertEqual(self.starts(), [a], 'queued run must not start a coordinator')
        self.assertEqual(run_store.active_runs(self.store), [a])
        (self.home / f'release-{a}').touch()
        self.wait_for(lambda: self.store.read(a)['state'] == 'cancelled', 10, 'first run terminal')
        self.wait_for(lambda: self.starts() == [a, b], 10, 'queued run started by handoff')
        self.assertIn('starting from the queue', (self.store.path(b) / 'start.log').read_text())
        self.wait_for(lambda: self.store.read(b)['state'] == 'running', 5, 'second run running')
        self.assertEqual(self.store.read(b)['queued_behind'], [])
        (self.home / f'release-{b}').touch()
        self.wait_for(lambda: self.store.read(b)['state'] == 'cancelled', 10, 'second run terminal')
        self.stop.set()
        self.assertLessEqual(self.max_active, 1, 'two coordinators owned runs of the same project at once')

    def test_running_a_queued_record_by_hand_is_refused_while_its_predecessor_is_alive(self):
        a, _ = self.submit_raw('first task')
        self.wait_for(lambda: self.starts() == [a], 10, 'first engine start')
        b, _ = self.submit_raw('second task')
        result = subprocess.run([sys.executable, str(self.root / 'lib/run_store.py'), 'run', b], env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 73)
        self.assertEqual(self.store.read(b)['state'], 'queued')
        self.assertIn('not this run\'s turn', (self.store.path(b) / 'start.log').read_text())
        self.assertEqual(self.starts(), [a])
        (self.home / f'release-{a}').touch()
        self.wait_for(lambda: self.starts() == [a, b], 10, 'handoff')
        (self.home / f'release-{b}').touch()
        self.wait_for(lambda: self.store.read(b)['state'] == 'cancelled', 10, 'second terminal')

    def test_concurrent_starts_dispatch_once_and_leave_no_owner_after_completion(self):
        rid = self.store.accept('e2e', 'one owned execution')['run_id']
        command = [sys.executable, str(self.root / 'lib/run_store.py'), 'run', rid]
        processes = [subprocess.Popen(command, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                     for _ in range(4)]
        try:
            self.wait_for(lambda: self.starts() == [rid], 10, 'single worker')
            self.wait_for(lambda: sum(p.poll() is not None for p in processes) == 3, 10, 'competing starts refused')
            self.assertEqual(run_store.active_runs(self.store), [rid])
            self.assertTrue(self.store.read(rid)['phase_process'])
            (self.home / f'release-{rid}').touch()
            codes = sorted(p.wait(timeout=10) for p in processes)
            self.assertEqual(codes, [75, 75, 75, 130])
            self.assertEqual(self.starts(), [rid])
            self.assertEqual(run_store.active_runs(self.store), [])
            self.assertEqual(self.store.read(rid)['state'], 'cancelled')
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)


class ReviveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-p4-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        self.root = Path(self.temp.name) / 'code'
        self.root.mkdir()
        self.store = RunStore(self.home)
        self.started = []
        self.enter(patch.object(run_store, '_start', side_effect=lambda store, rid, root, message: self.started.append((rid, message))))

    def enter(self, context):
        value = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return value

    def fresh(self, rid):
        return RunStore(self.home).read(rid)  # counters are read from disk, never from memory

    def test_corrupt_owner_record_blocks_new_writers_until_the_real_lock_releases(self):
        rid = self.store.accept('a', 'owned work')['run_id']
        other = self.store.accept('a', 'next work')['run_id']
        path = self.store.path(rid) / 'run.json'
        original = path.read_bytes()
        with run_store.FileLock(self.store.path(rid) / '.owner.lock'):
            path.write_text('{broken')
            self.assertIn(rid, run_store.active_runs(self.store))
            self.assertTrue(run_store.has_active(self.store, 'a', exclude=other))
            self.assertFalse(self.store.order_came(other))
            self.assertEqual(self.cli(other).returncode, 73)
            self.assertFalse((self.home / 'cli-started').exists())
            self.assertEqual(path.read_text(), '{broken')
        self.assertNotIn(rid, run_store.active_runs(self.store))
        self.assertTrue(self.store.order_came(other))
        path.write_bytes(original)
        self.assertIn('unreadable', (self.home / 'logs/run-broken.log').read_text())

    def test_terminal_and_dead_coordinator_locks_protect_the_project_not_unrelated_projects(self):
        rid = self.store.accept('a', 'old work')['run_id']
        other = self.store.accept('a', 'next work')['run_id']
        independent = self.store.accept('b', 'independent work')['run_id']
        for state in ('running', 'failed', 'cancelled', 'completed', 'implemented'):
            with self.subTest(state=state):
                self.store.update(rid, state=state, owner_pid=999999)
                with run_store.FileLock(self.store.path(rid) / '.owner.lock'):
                    self.assertTrue(run_store.has_active(self.store, 'a', exclude=other))
                    self.assertFalse(self.store.order_came(other))
                    self.assertTrue(self.store.order_came(independent))
                if state != 'running':
                    self.assertTrue(self.store.order_came(other))

    def test_unreadable_identity_and_detached_executor_lease_fail_closed(self):
        rid = self.store.accept('a', 'work')['run_id']
        other = self.store.accept('a', 'next')['run_id']
        path = self.store.path(rid) / 'run.json'
        original = path.read_bytes()
        data = json.loads(original)
        data.pop('project')
        path.write_text(json.dumps(data))
        with run_store.FileLock(self.store.path(rid) / '.owner.lock'):
            self.assertTrue(run_store.has_active(self.store, 'a', exclude=other))
            independent = self.store.accept('b', 'safe independent record')['run_id']
            self.assertFalse(self.store.order_came(independent))
        self.assertTrue(self.store.order_came(independent))
        path.write_bytes(original)
        with run_store.FileLock(self.store.task_dir(rid, 'g1') / '.opencode-impl.lock'):
            self.assertIn(rid, run_store.active_runs(self.store))
            self.assertTrue(run_store.has_active(self.store, 'a', exclude=other))
        self.assertEqual(run_store.active_runs(self.store), [])

    def cli(self, rid, *options):
        (self.root / 'bin').mkdir(exist_ok=True)
        (self.root / 'bin/job-runner.sh').write_text('echo started > "$CADRE_HOME/cli-started"\nexit 1\n')
        env = {**os.environ, 'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(self.root),
               'CADRE_DISK_MIN_MB': '0'}
        return subprocess.run([sys.executable, str(LIB / 'run_store.py'), 'run', rid, *options],
                              env=env, capture_output=True, text=True, timeout=10)

    def test_continue_cli_reopens_failed_run_only_with_explicit_flag(self):
        rid = self.store.accept('a', 'operator resume')['run_id']
        self.store.update(rid, state='failed', phase='runner')
        self.assertEqual(self.cli(rid).returncode, 73)
        self.assertFalse((self.home / 'cli-started').exists())
        result = self.cli(rid, '--proceed')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue((self.home / 'cli-started').exists())
        self.assertEqual(self.fresh(rid)['state'], 'retryable')

    def test_coordinator_records_independence_rejection_despite_two_passes(self):
        from outcome import decision
        rid = self.store.accept('a', 'acceptance reason')['run_id']
        revision = {'digest': 'same'}
        result = decision({'status': 'PASS', 'revision': revision, 'discovery': {'status': 'PASS', 'scope': 'full'}},
                          {'status': 'PASS', 'revision': revision, 'independent': False}, revision)
        self.store.update(rid, phase='runner', final_result=result)
        self.assertEqual(self.cli(rid).returncode, 1)
        self.assertIn('reviewer-not-independent', self.fresh(rid)['reason'])

    def test_retryable_run_is_retried_twice_then_failed_with_a_persistent_counter(self):
        rid = self.store.accept('a', 'night job')['run_id']
        self.store.update(rid, state='retryable', reason='qa-red: 2 round', exit_code=1)
        self.assertEqual(run_store.revive(self.store, self.root), [rid])
        self.assertEqual(self.fresh(rid)['retry_count'], 1)
        self.assertIn('retry #1/2', self.started[-1][1])
        self.store.update(rid, state='retryable')
        self.assertEqual(run_store.revive(self.store, self.root), [rid])
        self.assertEqual(self.fresh(rid)['retry_count'], 2)
        self.store.update(rid, state='retryable')
        self.assertEqual(run_store.revive(self.store, self.root), [])
        record = self.fresh(rid)
        self.assertEqual(record['state'], 'failed')
        self.assertTrue(record['reason'].startswith('retry limit: still failed after 2 retries'), record['reason'])
        self.assertEqual(len(self.started), 2)

    def test_a_run_stopped_by_a_spending_alarm_waits_for_approval_without_burning_retries(self):
        import time as _t
        rid = self.store.accept('a', 'report task')['run_id']
        self.store.update(rid, state='retryable', reason='lane-failed: g5', spend_alarm={'ts': _t.time(), 'reason': 'x'})
        self.assertEqual(run_store.revive(self.store, self.root), [])
        self.assertEqual((self.fresh(rid)['state'], self.fresh(rid).get('retry_count', 0)), ('retryable', 0))
        self.store.update(rid, spend_approval_ts=_t.time() + 1)
        self.assertEqual(run_store.revive(self.store, self.root), [rid])
        dead = self.store.accept('b', 'dead coordinator')['run_id']
        self.store.update(dead, state='running', spend_alarm={'ts': _t.time(), 'reason': 'x'})
        run_store.revive(self.store, self.root)
        self.assertEqual(self.fresh(dead).get('revive_count', 0), 0, 'Waiting for alarm approval does not consume a revival')

    def test_spending_approval_also_approves_the_owned_child_and_preserves_capacity_wait(self):
        import os
        import subprocess
        import time as _t
        main = self.store.accept('a', 'project_ref')['run_id']
        child = self.store.accept('a--p1', 'feature')['run_id']
        self.store.update(main, project_active_run=child, retry_after=_t.time() + 3600)
        self.store.update(child, project_ref_parent=main, retry_after=_t.time() + 3600, spend_alarm={'ts': _t.time(), 'reason': 'x'})
        waits = {rid: self.fresh(rid)['retry_after'] for rid in (main, child)}
        r = subprocess.run([sys.executable, str(Path(run_store.__file__)), 'spend-approval', main], capture_output=True, text=True,
                           env={**os.environ, 'CADRE_HOME': str(self.home)}, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        for rid in (main, child):
            record = self.fresh(rid)
            self.assertEqual(record.get('retry_after'), waits[rid])
            self.assertGreater(float(record.get('spend_approval_ts') or 0), 0)

    def test_budget_wait_is_recorded_only_when_the_last_attempt_was_a_budget_denial(self):
        import os
        import time as _t
        rid = self.store.accept('a', 'task')['run_id']
        root = self.store.task_dir(rid, 'g1') / 'attempts'
        self.assertFalse(run_store.last_attempt_budget_rejection(self.store, rid, 'g1'))
        old, fresh = root / 'a1', root / 'a2'
        old.mkdir(parents=True); fresh.mkdir()
        (old / 'budget-denied.json').write_text('{}')
        os.utime(old, (_t.time() - 60, _t.time() - 60))
        self.assertFalse(run_store.last_attempt_budget_rejection(self.store, rid, 'g1'), 'The last attempt failed for a reason unrelated to budget')
        (fresh / 'budget-denied.json').write_text('{}')
        self.assertTrue(run_store.last_attempt_budget_rejection(self.store, rid, 'g1'))

    def test_a_report_without_a_budgeted_planner_waits_but_a_failed_plan_does_not(self):
        rid = self.store.accept('a', 'ROUND: report\nreport')['run_id']
        self.assertTrue(run_store.report_no_plan_reason(self.store, rid, True).startswith('budget-wait'))
        self.assertTrue(run_store.report_no_plan_reason(self.store, rid, False).startswith('no-capacity'))
        attempt = self.store.task_dir(rid, 'plan') / 'attempts' / 'a1'
        attempt.mkdir(parents=True)
        (attempt / 'budget-denied.json').write_text('{}')
        self.assertTrue(run_store.report_no_plan_reason(self.store, rid, False).startswith('budget-wait'))

    def test_reviewer_capacity_exhaustion_waits_for_renewal_without_burning_retries(self):
        """B213 (24.09 R5-hard): when the judge chain ran out because of BUDGET (Claude/Sol/deepseek shares exhausted) the run was tried
        twice, every 30 min, and became a permanent FAIL — the shares only renew at midnight. Now it waits until the renewal and burns no allowance."""
        import time as _t
        rid = self.store.accept('a', 'hard job')['run_id']
        self.store.update(rid, state='retryable', reason='acceptance FAIL: qa=PASS judge=ERROR', retry_after=_t.time() + 3600)
        self.assertEqual(run_store.revive(self.store, self.root), [])
        self.assertEqual((self.fresh(rid)['state'], self.fresh(rid).get('retry_count', 0)), ('retryable', 0))
        self.store.update(rid, retry_after=_t.time() - 1)
        self.assertEqual(run_store.revive(self.store, self.root), [rid])
        fresh = self.fresh(rid)
        self.assertEqual((fresh.get('retry_count', 0), fresh.get('capacity_attempt')), (0, 1), 'An attempt after waiting does not consume the normal allowance')
        self.assertIsNone(fresh.get('retry_after'), 'Clear the field when the wait ends')

    def test_capacity_wait_survives_an_exhausted_retry_budget_and_is_bounded(self):
        """B213: a run whose attempt allowance is used up (2/2) is not written FAIL while it waits for capacity; when the wait ends one CAPACITY attempt
        is made (no normal allowance is burned); at most 3 capacity attempts against an endless wait."""
        import time as _t
        rid = self.store.accept('a', 'hard job')['run_id']
        self.store.update(rid, state='retryable', reason='judge-capacity', retry_count=2, retry_after=_t.time() + 3600)
        self.assertEqual(run_store.revive(self.store, self.root), [])
        self.assertEqual(self.fresh(rid)['state'], 'retryable', 'Waiting with a full allowance must not produce FAIL')
        self.store.update(rid, retry_after=_t.time() - 1)
        self.assertEqual(run_store.revive(self.store, self.root), [rid])
        fresh = self.fresh(rid)
        self.assertEqual((fresh['retry_count'], fresh.get('capacity_attempt')), (2, 1))
        self.store.update(rid, state='retryable', retry_after=_t.time() + 3600, capacity_attempt=run_store.CAPACITY_ATTEMPT_LIMIT)
        self.assertEqual(run_store.revive(self.store, self.root), [])
        self.assertEqual(self.fresh(rid)['state'], 'failed', 'The capacity allowance is also exhausted; do not wait needlessly')

    def test_a_run_waiting_for_reviewer_capacity_does_not_hold_the_project_queue(self):
        import time as _t
        a = self.store.accept('a', 'pending')['run_id']
        self.store.update(a, state='retryable', reason='judge-capacity', retry_after=_t.time() + 3600)
        b = self.store.accept('a', 'next')['run_id']
        self.assertTrue(self.store.order_came(b), 'A pending run must not hold the queue')

    def test_a_recycled_pid_does_not_make_a_dead_coordinator_look_alive(self):
        """B110 (18.09): ownership was measured ONLY by the pid; when the operating system reused the pid
        an unrelated process showed the coordinator as ALIVE and the project's queue was locked permanently."""
        import budget_gate
        rid = self.store.accept('a', 'task')['run_id']
        lock = run_store.FileLock(self.store.path(rid) / '.owner.lock')
        self.assertTrue(lock.acquire(blocking=False))
        self.addCleanup(lock.close)
        self.store.update(rid, owner_pid=os.getpid(), state='running')   # test process: alive but NOT run_store.py
        self.assertFalse(run_store.coordinator_live(self.store, self.fresh(rid)),
                         'A live PID is not a coordinator unless argv matches run_store.py')
        self.assertFalse(budget_gate.is_owner_dead(self.store.home, rid))
        lock.close()
        self.assertTrue(budget_gate.is_owner_dead(self.store.home, rid))

    def test_execute_form_coordinator_with_matching_identity_is_live(self):
        import budget_gate
        rid = self.store.accept('a', 'task')['run_id']
        lock = run_store.FileLock(self.store.path(rid) / '.owner.lock')
        self.assertTrue(lock.acquire(blocking=False))
        self.addCleanup(lock.close)
        self.store.update(rid, owner_pid=os.getpid(), owner_identity=run_store.process_identity(os.getpid()), state='running')
        for command, expected in (('python3 /x/lib/run_store.py execute a /x/prompt.md', 'live'),
                                  (f'python3 /x/lib/run_store.py run {rid}', 'live'),
                                  ('python3 /x/lib/run_store.py execute b /x/prompt.md', 'unknown'),
                                  ('python3 /x/lib/run_store.py run other', 'unknown')):
            with self.subTest(command=command), patch.object(run_store, 'process_command', return_value=command):
                self.assertEqual(budget_gate.ownership_status(self.store.home, rid), expected)

    def test_operator_continue_reopens_a_failed_run_but_automation_cannot(self):
        """B110 (18.09): because 'failed' is TERMINAL, `cadre proceed <id>` could NOT OPEN a failed run, while the system
        suggested exactly that command in the recovery message. The denial only landed in start.log and the user saw "recorded"."""
        rid = self.store.accept('a', 'failed job')['run_id']
        self.store.update(rid, state='failed', reason='retry limit: still failed after 2 retries')
        self.assertNotIn(rid, [r['run_id'] for r in self.store.pending('a')])
        self.assertEqual(run_store.revive(self.store, self.root), [], 'The automatic path does not reopen a failed run')
        self.assertTrue(run_store.proceed_open(self.store, rid), 'An explicit operator request reopens the run')
        record = self.fresh(rid)
        self.assertEqual(record['state'], 'retryable')
        self.assertIn('retry limit', record['reason'], 'The old reason must stay in the record')
        self.assertIn(rid, [r['run_id'] for r in self.store.pending('a')])
        for status in ('completed', 'cancelled'):
            self.store.update(rid, state=status)
            self.assertFalse(run_store.proceed_open(self.store, rid), f'{status} run cannot be reopened with proceed')

    def test_cancel_flag_means_zero_retries_and_the_idle_run_becomes_cancelled(self):
        rid = self.store.accept('a', 'night job')['run_id']
        self.store.update(rid, state='retryable', reason='qa-red')
        (self.store.path(rid) / 'cancel.flag').write_text('requested\n')
        self.assertEqual(run_store.revive(self.store, self.root), [])
        self.assertEqual(self.started, [])
        self.assertEqual(self.fresh(rid)['state'], 'cancelled')   # B35: a flagged run without an owner is made terminal
        self.assertNotIn('retry_count', self.fresh(rid))

    def test_cancel_all_makes_idle_pending_runs_terminal_at_once_and_unblocks_the_project(self):
        """B35: if accepted/retryable/running records left from old sessions are only flagged and counted as unfinished, a new task
        stays in the queue forever. A run without an owner becomes cancelled immediately with `cancel-all`; one with a live owner waits for the flag."""
        a = self.store.accept('a', 'old')['run_id']
        self.store.update(a, state='running', owner_pid=999999)
        b = self.store.accept('a', 'old 2')['run_id']
        self.store.update(b, state='retryable')
        c = self.store.accept('a', 'live')['run_id']
        self.store.update(c, state='running')
        lock = run_store.FileLock(self.store.path(c) / '.owner.lock')
        self.assertTrue(lock.acquire(blocking=False))
        self.addCleanup(lock.close)
        out = subprocess.run([sys.executable, str(LIB / 'run_store.py'), 'cancel-all'], env={**os.environ, 'CADRE_HOME': str(self.home)},
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(sorted(out.stdout.split()), sorted([a, b, c]))
        self.assertEqual([self.fresh(x)['state'] for x in (a, b)], ['cancelled', 'cancelled'])
        self.assertEqual(self.fresh(c)['state'], 'running')   # a live coordinator processes the flag itself
        self.assertTrue((self.store.path(c) / 'cancel.flag').exists())
        self.assertEqual([r['run_id'] for r in self.store.pending('a')], [])   # a flagged c does not hold the turn
        lock.close()
        d = self.store.accept('a', 'fresh')['run_id']
        self.assertEqual(self.fresh(d)['state'], 'accepted')
        self.assertTrue(self.store.order_came(d))

    def test_stale_accepted_run_without_a_coordinator_is_started_but_a_fresh_one_is_left_alone(self):
        rid = self.store.accept('a', 'accepted')['run_id']
        self.assertEqual(run_store.revive(self.store, self.root), [], 'cadre.sh may still be starting it')
        self.store.update(rid, created=time.time() - run_store.ACCEPT_STALE_SEC - 1)
        self.assertEqual(run_store.revive(self.store, self.root), [rid])
        self.assertIn('never started', self.started[-1][1])

    def test_guard_starts_only_the_oldest_pending_run_of_a_project(self):
        a = self.store.accept('a', 'one')['run_id']
        b = self.store.accept('a', 'two')['run_id']
        c = self.store.accept('a', 'endpoint')['run_id']
        self.assertEqual([self.fresh(x)['state'] for x in (a, b, c)], ['accepted', 'queued', 'queued'])
        self.assertEqual(self.fresh(c)['queued_behind'], [a, b])
        self.store.update(a, state='failed', reason='retry limit')
        self.assertEqual(run_store.revive(self.store, self.root), [b])
        self.assertEqual(self.started[-1][0], b)
        # b retryable (older, not terminal) blocks c; b is retried first
        self.store.update(b, state='retryable', reason='qa-red')
        self.assertEqual(run_store.revive(self.store, self.root), [b])
        self.assertEqual(self.fresh(c)['state'], 'queued')
        # a live coordinator on b blocks everything
        lock = run_store.FileLock(self.store.path(b) / '.owner.lock')
        self.assertTrue(lock.acquire(blocking=False))
        self.addCleanup(lock.close)
        self.store.update(b, state='running')
        self.assertEqual(run_store.revive(self.store, self.root), [])

    def test_status_summary_is_structural(self):
        a = self.store.accept('a', 'one')['run_id']
        b = self.store.accept('a', 'two')['run_id']
        self.store.update(a, state='retryable', reason='qa-red: 1 round', retry_count=1)
        text = run_store.summary(self.store, 'a')
        self.assertIn(f'{b} · queued/accepted', text)
        self.assertIn('pending:', text)
        self.assertIn(a[:15] + '(retryable)', text)
        result = subprocess.run([sys.executable, str(LIB / 'run_store.py'), 'summary', 'a'], env={**os.environ, 'CADRE_HOME': str(self.home)},
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.stdout.strip(), text)


if __name__ == '__main__':
    unittest.main()


class CoordinatorStartingTests(unittest.TestCase):
    """T0/NET2: the coordinator starts in its own session (`start_new_session`) and its stdin is not attached; it does not die when the terminal
    closes. Previously only the string `nohup ... </dev/null` was grepped — now the call is measured."""

    def test_the_coordinator_starts_detached_so_closing_the_terminal_cannot_kill_it(self):
        from unittest.mock import patch
        temp = tempfile.TemporaryDirectory(prefix='qe-start-')
        self.addCleanup(temp.cleanup)
        store = RunStore(Path(temp.name))
        rid = store.accept('a', 'task')['run_id']
        with patch.object(run_store.subprocess, 'Popen') as popen:
            run_store._start(store, rid, Path(temp.name), 'test')
        self.assertTrue(popen.called, 'The coordinator was not started')
        self.assertTrue(popen.call_args.kwargs.get('start_new_session'), 'Must start in its own session')
        self.assertEqual(popen.call_args.kwargs.get('stdin'), run_store.subprocess.DEVNULL, 'stdin must not remain attached')


class JudgeCapacityDecisionTests(unittest.TestCase):
    """B213 pure decision: if the judge ERROR comes from a BUDGET denial the wait moment = local midnight + 10 min; otherwise None."""
    def test_budget_denied_reviewer_chain_waits_until_after_midnight(self):
        import datetime
        now_ts = datetime.datetime(2026, 9, 24, 8, 38).timestamp()
        final = {'status': 'FAIL', 'review': {'status': 'ERROR', 'reason': 'judge chain exhausted: claude/opus#max: BudgetDenied: x; opencode/big-pickle: malformed'}}
        expected = datetime.datetime(2026, 9, 25, 0, 10).timestamp()
        self.assertEqual(run_store.capacity_wait(final, now_ts), expected)
        alarm = {'review': {'status': 'ERROR', 'reason': 'BudgetDenied: spend alarm: awaiting approval'}}
        self.assertIsNone(run_store.capacity_wait(alarm, now_ts), 'Approval clears the alarm without waiting for midnight')

    def test_on_claude_rejection_the_five_hour_window_is_awaited_if_before_midnight(self):
        """B240 (26.09 live R13-v5): the only independent judge was Claude and the automation share was exhausted; the wait was set to midnight (21 h),
        while Claude's 5-hour window renews at 06:00. On a Claude denial the wait = min(midnight+10min, 5h reset+5min)."""
        import datetime, json, tempfile
        now_ts = datetime.datetime(2026, 9, 26, 3, 20).timestamp()
        final = {'status': 'FAIL', 'review': {'status': 'ERROR', 'reason': 'judge chain exhausted: claude/opus#max: BudgetDenied: claude budget denied'}}
        with tempfile.TemporaryDirectory() as t:
            quota = Path(t) / 'claude_quota.json'
            reset = datetime.datetime(2026, 9, 26, 6, 0).timestamp()
            quota.write_text(json.dumps({'rate_limits': {'five_hour': {'used_percentage': 90, 'resets_at': reset}}}))
            self.assertEqual(run_store.capacity_wait(final, now_ts, quota=quota), reset + 300)
            quota.write_text(json.dumps({'rate_limits': {'five_hour': {'resets_at': now_ts - 60}}}))   # past reset: no information
            self.assertEqual(run_store.capacity_wait(final, now_ts, quota=quota), datetime.datetime(2026, 9, 27, 0, 10).timestamp())
            other = {'status': 'FAIL', 'review': {'status': 'ERROR', 'reason': 'judge chain exhausted: deepseek/x: BudgetDenied: y'}}
            self.assertEqual(run_store.capacity_wait(other, now_ts, quota=quota), datetime.datetime(2026, 9, 27, 0, 10).timestamp())

    def test_other_failures_do_not_wait(self):
        for final in ({'status': 'FAIL', 'review': {'status': 'FAIL', 'reason': 'REVISE'}},
                      {'status': 'FAIL', 'review': {'status': 'ERROR', 'reason': 'judge chain exhausted: x: malformed review'}},
                      {'status': 'FAIL', 'qa': {'status': 'FAIL'}, 'review': {'status': 'SKIPPED'}}, {}, None):
            with self.subTest(final=final):
                self.assertIsNone(run_store.capacity_wait(final, 0.0))


class JudgeCapacityCoordinatorTests(unittest.TestCase):
    """B213 end to end: the real coordinator; a fake dispatcher records the judge ERROR (budget denial) and exits with 1 ->
    run retryable + wait moment (midnight + 10 min) + a readable reason."""
    def test_coordinator_records_the_renewal_time_for_a_budget_starved_review(self):
        import datetime
        temp = tempfile.TemporaryDirectory(prefix='qe-b213-')
        self.addCleanup(temp.cleanup)
        home, root = Path(temp.name) / 'home', Path(temp.name) / 'root'
        (root / 'bin').mkdir(parents=True)
        (root / 'lib').symlink_to(LIB)
        final = {'status': 'FAIL', 'reason': 'review-not-passed', 'qa': {'status': 'PASS'},
                 'review': {'status': 'ERROR', 'reason': 'judge chain exhausted: claude/opus#max: BudgetDenied: x'}}
        (root / 'bin' / 'cadre-dispatch.sh').write_text(
            f"printf '%s' '{json.dumps({'final_result': final})}' | python3 \"$CADRE_ENGINE_ROOT/lib/run_store.py\" update \"$CADRE_RUN_ID\"\nexit 1\n")
        store = RunStore(home)
        rid = store.accept('a', 'hard job')['run_id']
        with patch.dict(os.environ, {'CADRE_HOME': str(home), 'CADRE_ENGINE_ROOT': str(root), 'CADRE_COORD_TICK': '0.1',
                                     'CADRE_CLAUDE_QUOTA': str(home / 'no-quota.json')}):   # B240/B220: the developer's real Claude quota file must not be read
            run_store.execute_run(store, rid)
        record = RunStore(home).read(rid)
        expected = (datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                    + datetime.timedelta(days=1, minutes=10)).timestamp()
        self.assertEqual(record['state'], 'retryable')
        self.assertAlmostEqual(float(record['retry_after']), expected, delta=1)
        self.assertTrue(record['reason'].startswith('judge-capacity'), record['reason'])
