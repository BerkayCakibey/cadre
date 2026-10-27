"""P1 isolation + P2 coordinator lifetime (night autonomy plan).

P1: the engine and everything it starts run only from code under CADRE_ENGINE_ROOT. Sabotage: if the "live" root under the fake HOME
(~/.local/share/cadre-engineering/{lib,bin}) is executed it leaves a trace and breaks rc -> the test is red.
P2: guard liveness is pidfile+argv+heartbeat (not pgrep -f); the coordinator revives a dead guard, the guard revives a dead coordinator;
no executor starts below the disk threshold; caffeinate is a single process during the coordinator's lifetime.
Fake executor, fake HOME, isolated home; no model/network/Windows."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
ENGINE = LIB.parent
sys.path.insert(0, str(LIB))
import test_run_sessions as fixtures
import test_end_to_end as e2e  # noqa: E402  (fake opencode + fake budget + isolated root setup)
import run_store  # noqa: E402
from run_store import RunStore  # noqa: E402


def setUpModule():
    environment = patch.dict(os.environ, {'CADRE_CHAIN_QUALITY': 'opencode-go/qwen3.8-max'})
    environment.start()
    unittest.addModuleCleanup(environment.stop)


SHIM = '''#!{python}
import json, os, sys, time
if os.environ.get('CADRE_ARGV_LOG'):  # B348: the QA environment does not carry CADRE_*; a product test is not an engine invocation
    with open(os.environ['CADRE_ARGV_LOG'], 'a') as f:
        f.write(json.dumps({{'tool': {name!r}, 'args': sys.argv[1:], 'ppid': os.getppid()}}) + '\\n')
{body}
'''
LEAK = 'echo "$0 $*" >> "${CADRE_HOME:-/tmp}/LIVE-LEAK"; exit 1\n'
LEAK_PY = 'import os,sys; open(os.path.join(os.environ.get("CADRE_HOME","/tmp"),"LIVE-LEAK"),"a").write(" ".join(sys.argv)+"\\n"); sys.exit(1)\n'


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class IsolatedNight(e2e.E2EFixture):
    """E2E fixture + fake HOME (a sabotaged live root) + PATH shims (bash/python3/caffeinate argv ledger)."""

    def setUp(self):
        super().setUp()
        base = Path(self.temp.name)
        self.fake_home = base / 'fakehome'
        live = self.fake_home / '.local/share/cadre-engineering'
        for sub in ('lib', 'bin'):
            (live / sub).mkdir(parents=True)
        for name in ('run_store.py', 'provider_budget.py', 'run_ledger.py', 'qe_core.py', 'liveness.py', 'acceptance.py', 'runner.py', 'rotation.py', 'model_router.py'):
            (live / 'lib' / name).write_text(LEAK_PY)
        (live / 'lib/common.sh').write_text(LEAK)
        for name in ('health-watch.sh', 'capacity-watch.sh', 'job-runner.sh', 'cadre-dispatch.sh', 'orchestrator.sh', 'restricted-shell.sh', 'meta-supervisor.sh', 'lesson-summary.sh'):
            (live / 'bin' / name).write_text('#!/bin/bash\n' + LEAK)
            (live / 'bin' / name).chmod(0o755)
        (self.fake_home / '.gitconfig').write_text('[user]\n\tname = e2e\n\temail = e2e@x\n[init]\n\tdefaultBranch = main\n')
        shims = self.root / 'bin/fixture-shims'
        shims.mkdir()
        self.argv_log = self.home / 'argv.jsonl'
        for name, body in (
            ('bash', 'os.execv("/bin/bash", ["/bin/bash", *sys.argv[1:]])'),
            ('python3', f'os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])'),
            ('caffeinate', 'a = sys.argv[1:]\n'
                           'pid = int(a[a.index("-w")+1]) if "-w" in a else 0\n'
                           'while True:\n'
                           '    time.sleep(.2)\n'
                           '    if pid:\n'
                           '        try: os.kill(pid, 0)\n'
                           '        except ProcessLookupError: sys.exit(0)'),
        ):
            shim = shims / name
            shim.write_text(SHIM.format(python=sys.executable, name=name, body=body))
            shim.chmod(0o755)
        self.env.update(HOME=str(self.fake_home), PATH=f"{shims}:{self.env['PATH']}", CADRE_ARGV_LOG=str(self.argv_log),
                        CADRE_WATCH_INTERVAL='2', CADRE_GUARD_CHECK='1', CADRE_COORD_TICK='0.2')

    def guard_close(self, guard):
        """B219: the watch's tick children and the watchers it revives (in their own process groups) must not keep writing to the temporary home."""
        groups = [guard.pid]
        for pidfile in (self.home / 'chain').glob('*.pid'):
            try:
                groups.append(int(pidfile.read_text().split()[0]))
            except (OSError, ValueError, IndexError):
                pass
        for pid in groups:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        root = str(Path(self.temp.name))   # a watcher that has not written its pidfile yet also carries the fixture path on its command line
        for _ in range(50):
            lines = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True).stdout.splitlines()
            remaining = [int(s.split(None, 1)[0]) for s in lines if root in s and int(s.split(None, 1)[0]) != os.getpid()]
            if not remaining:
                break
            for pid in remaining:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            time.sleep(.1)
        guard.wait(timeout=5)

    def argv(self):
        if not self.argv_log.exists():
            return []
        return [json.loads(l) for l in self.argv_log.read_text().splitlines()]

    def wait_for(self, predicate, timeout, what):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.1)
        self.fail(f'timeout: {what}')


class CaffeinateReapingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-caffeinate-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = RunStore(self.home/'home')
        self.root = self.home/'code'
        (self.root/'bin').mkdir(parents=True)
        (self.root/'bin/job-runner.sh').write_text('exit 0\n')
        self.children = []
        self.addCleanup(self.cleanup_children)

    def cleanup_children(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

    def run_coordinator(self, *, ignore_term=False, fail=False):
        real_popen = subprocess.Popen

        def launch(args, *positional, **kwargs):
            if args[0] != 'caffeinate':
                return real_popen(args, *positional, **kwargs)
            script = ('import signal,time\n'
                      + ('signal.signal(signal.SIGTERM, signal.SIG_IGN)\n' if ignore_term else '')
                      + 'print("ready",flush=True)\ntime.sleep(30)\n')
            child = real_popen([sys.executable, '-c', script], stdout=subprocess.PIPE, text=True)
            self.children.append(child)
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            child.stdout.close()
            return child

        rid = self.store.accept('fixture', 'plain task')['run_id']
        env = {'CADRE_HOME': str(self.store.home), 'CADRE_ENGINE_ROOT': str(self.root),
               'CADRE_DISPATCH': '0', 'CADRE_DISK_MIN_MB': '0'}
        with patch.dict(os.environ, env), patch.object(subprocess, 'Popen', side_effect=launch), \
             patch.object(run_store, 'guard_revive', return_value=[],
                          side_effect=RuntimeError('fixture error') if fail else None), \
             patch.object(run_store, 'queue_start'):
            if fail:
                with self.assertRaisesRegex(RuntimeError, 'fixture error'):
                    run_store.execute_run(self.store, rid)
            else:
                self.assertNotEqual(run_store.execute_run(self.store, rid), 0)  # no QA/judge evidence.
        child, = self.children
        self.assertIsNotNone(child.returncode, 'Reap the child with wait before the coordinator returns')
        with self.assertRaises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)
        return child.returncode

    def test_coordinator_reaps_its_caffeinate_before_returning(self):
        self.assertEqual(self.run_coordinator(), -signal.SIGTERM)

    def test_coordinator_kills_and_reaps_caffeinate_that_ignores_term(self):
        self.assertEqual(self.run_coordinator(ignore_term=True), -signal.SIGKILL)

    def test_coordinator_error_path_also_reaps_caffeinate(self):
        self.assertEqual(self.run_coordinator(fail=True), -signal.SIGTERM)


class LifetimeTests(IsolatedNight):
    def test_a_tail_or_dead_pid_is_not_a_live_guard(self):
        """P2(c3): a `tail` process reading the log or a dead pid does not show the guard as 'up' -> it is restarted."""
        home, root = self.home, self.root
        log = home / 'logs/health-watch.log'
        log.write_text('')
        tail = subprocess.Popen(['tail', '-f', str(log)])
        self.addCleanup(lambda: (tail.kill(), tail.wait(timeout=5)))
        (home / 'chain/health-watch.pid').write_text(str(tail.pid))
        self.assertFalse(run_store.guard_live(home, 'health-watch'))
        (home / 'chain/capacity-watch.pid').write_text('999999')
        self.assertFalse(run_store.guard_live(home, 'capacity-watch'))
        with patch.object(run_store, 'guard_start') as start:
            self.assertEqual(sorted(run_store.guard_revive(home, root)), ['capacity-watch', 'health-watch'])
            self.assertEqual(start.call_count, 2)
        # stale heartbeat (pid alive, argv right) -> counted as dead
        script = Path(self.temp.name) / 'health-watch.sh'
        script.write_text('#!/bin/bash\nsleep 10\n')
        script.chmod(0o755)
        proc = subprocess.Popen(['bash', str(script)])
        self.addCleanup(lambda: (proc.kill(), proc.wait(timeout=5)))
        (home / 'chain/health-watch.pid').write_text(str(proc.pid))
        (home / 'chain/health-watch.tick').write_text(str(int(time.time()) - 10 * 3600))
        with patch.dict(os.environ, {'CADRE_WATCH_INTERVAL': '60'}):
            self.assertFalse(run_store.guard_live(home, 'health-watch'))

    def test_guard_script_defaults_match_the_liveness_clock(self):
        """The coordinator measures the heartbeat with the GUARDS interval and the interval environment is not passed to the guard (B75); if the script default
        differs, a healthy guard is counted as 'hung' and killed (07.10: script 1800, coordinator 600)."""
        for entry_name, (key, interval) in run_store.GUARDS.items():
            self.assertIn(f'${{{key}:-{interval}}}', (ENGINE / 'bin' / f'{entry_name}.sh').read_text(), entry_name)

    def coordinator_fixture(self, dispatcher_body):
        """Real run_store.py execute; fake dispatcher; fake guards write a pidfile and sleep."""
        (self.root / 'bin/cadre-dispatch.sh').write_text(dispatcher_body)
        for name in ('health-watch', 'capacity-watch'):
            (self.root / f'bin/{name}.sh').write_text(
                f'#!/bin/bash\nmkdir -p "$CADRE_HOME/chain"; echo $$ > "$CADRE_HOME/chain/{name}.pid"; '
                f'while :; do date +%s > "$CADRE_HOME/chain/{name}.tik"; echo tick >> "$CADRE_HOME/{name}-ticks"; sleep 0.5; done\n')
        run = self.store.accept('e2e', 'lifetime test')
        return run['run_id']

    def guard_pid(self, name):
        try:
            return int((self.home / 'chain' / f'{name}.pid').read_text().strip())
        except (OSError, ValueError):
            return None

    def test_coordinator_revives_a_killed_guard_within_its_check_window(self):
        """P2(c1): guard SIGKILL -> a new pidfile at the coordinator tick (CADRE_GUARD_CHECK), alive, a different pid."""
        rid = self.coordinator_fixture('#!/bin/bash\nsleep 6\nexit 3\n')
        (self.root / 'bin/job-runner.sh').write_text('#!/bin/bash\nexit 1\n')
        proc = subprocess.Popen([sys.executable, str(self.root / 'lib/run_store.py'), 'run', rid], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait(timeout=5), proc.stdout.close()))
        self.wait_for(lambda: self.guard_pid('health-watch') and pid_alive(self.guard_pid('health-watch')), 5, 'guard start')
        first = self.guard_pid('health-watch')
        os.kill(first, signal.SIGKILL)
        self.wait_for(lambda: not pid_alive(first), 3, 'guard death')
        self.wait_for(lambda: (self.guard_pid('health-watch') not in (None, first)) and pid_alive(self.guard_pid('health-watch')), 5, 'guard revive')
        self.assertTrue(run_store.guard_live(self.home, 'health-watch'))
        rc = proc.wait(timeout=30)
        self.assertEqual(rc, 1, proc.stdout.read())
        self.assertEqual(self.store.read(rid)['state'], 'retryable')

    def test_real_guard_script_revives_a_dead_coordinator_and_reports_disk(self):
        """P2(c2)+P1: the REAL health-watch.sh runs from the isolated root; it revives a run with state=running + an empty owner lock with `run_store.py revive`
        (revive_count=1, revive #1 in start.log); writes the disk line; leaves a heartbeat and a pidfile."""
        run = self.store.accept('e2e', 'dead coordinator')
        rid = run['run_id']
        self.store.update(rid, state='running', phase='runner', owner_pid=999999)
        (self.root / 'bin/job-runner.sh').write_text('#!/bin/bash\nexit 1\n')
        (self.root / 'bin/cadre-dispatch.sh').write_text('#!/bin/bash\nexit 3\n')
        guard = subprocess.Popen(['bash', str(self.root / 'bin/health-watch.sh')], env={**self.env, 'CADRE_DISK_MIN_MB': '1'},
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(self.guard_close, guard)
        self.wait_for(lambda: (self.home / 'chain/health-watch.pid').exists(), 5, 'pidfile')
        self.assertEqual(int((self.home / 'chain/health-watch.pid').read_text()), guard.pid)
        self.wait_for(lambda: (self.home / 'chain/health-watch.tick').exists(), 5, 'heartbeat')
        self.wait_for(lambda: self.store.read(rid).get('revive_count') == 1, 15, 'revive')
        self.wait_for(lambda: self.store.read(rid)['state'] in ('retryable', 'failed'), 30, 'revived coordinator finished')
        self.assertIn('revive #1', (self.store.path(rid) / 'start.log').read_text())
        log = (self.home / 'logs/health-watch.log').read_text()
        self.assertRegex(log, r'disk: \d+ MB free')
        self.assertFalse((self.home / 'LIVE-LEAK').exists())
        self.assertTrue(run_store.guard_live(self.home, 'health-watch'))

    def test_low_disk_starts_no_executor_and_records_the_reason(self):
        """P2(c4): free space below the threshold -> executor Popen 0, at the wait cap state=failed, reason=disk-low."""
        rid = self.coordinator_fixture('#!/bin/bash\necho dispatch >> "$CADRE_RUN_DIR/calls"\nexit 3\n')
        (self.root / 'bin/job-runner.sh').write_text('#!/bin/bash\necho runner >> "$CADRE_RUN_DIR/calls"\nexit 0\n')
        env = {**self.env, 'CADRE_DISK_MIN_MB': str(10 ** 9), 'CADRE_DISK_WAIT': '1', 'CADRE_DISK_POLL': '0.2'}
        t0 = time.monotonic()
        result = subprocess.run([sys.executable, str(self.root / 'lib/run_store.py'), 'run', rid], env=env, capture_output=True, text=True, timeout=30)
        elapsed = time.monotonic() - t0
        record = self.store.read(rid)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.store.path(rid) / 'calls').exists(), 'executor must not start on low disk')
        self.assertEqual(record['state'], 'failed')
        self.assertTrue(record.get('reason', '').startswith('disk-low'), record.get('reason'))
        self.assertLess(elapsed, 15)
        # normal threshold -> the same record is not retryable (stays failed); a new record takes the normal path
        rid2 = self.store.accept('e2e', 'disk normal')['run_id']
        subprocess.run([sys.executable, str(self.root / 'lib/run_store.py'), 'run', rid2], env={**self.env, 'CADRE_DISK_MIN_MB': '1'},
                       capture_output=True, text=True, timeout=30)
        self.assertEqual((self.store.path(rid2) / 'calls').read_text().split(), ['dispatch', 'runner'])

    def test_coordinator_holds_exactly_one_caffeinate_for_its_own_lifetime(self):
        """P2(c5): a single `caffeinate -i -s -w <coordinator pid>` on the full path; the engine does not start another; it ends when the run ends."""
        rid = self.submit()
        record = self.wait_terminal(rid)
        self.assertEqual(record['state'], 'completed')
        caff = [e for e in self.argv() if e['tool'] == 'caffeinate']
        self.assertEqual(len(caff), 1, caff)
        args = caff[0]['args']
        self.assertEqual(args[:3], ['-i', '-s', '-w'])
        self.assertEqual(int(args[3]), caff[0]['ppid'], 'caffeinate must wait on its own parent, the coordinator')
        coordinator_cmd = [e for e in self.argv() if e['tool'] == 'python3' and any(a.endswith('run_store.py') for a in e['args']) and 'execute' in e['args']]
        self.assertTrue(coordinator_cmd, 'coordinator not observed')
        self.wait_for(lambda: not pid_alive(int(args[3])), 5, 'coordinator exit')
        time.sleep(1)
        self.assertNotIn(str(args[3]), subprocess.run(['pgrep', '-f', f'caffeinate -i -s -w {args[3]}'], capture_output=True, text=True).stdout)

    def test_guards_and_revived_coordinators_do_not_inherit_per_run_knobs(self):
        """B33: settings given for one task like CADRE_DISK_MIN_MB/CADRE_MODEL_WAIT/E2E_* do not pass to the guard or to the coordinator
        the guard starts; home/root and persistent harness settings (CADRE_REAL_OPENCODE) do pass."""
        seen = []
        real_popen = subprocess.Popen
        rid = self.store.accept('e2e', 'x')['run_id']   # the Popen patch also affects subprocess.run: acceptance first
        with patch.dict(os.environ, {'CADRE_DISK_MIN_MB': '999999999', 'E2E_BROKEN': 'b', 'CADRE_REAL_OPENCODE': '/fake/opencode', 'CADRE_RUN_ID': 'old-run'}), \
             patch.object(run_store.subprocess, 'Popen', side_effect=lambda cmd, **kw: seen.append(kw.get('env') or {}) or real_popen(['sleep', '0'])):
            run_store.guard_start(self.home, self.root, self.root / 'bin/health-watch.sh')
            run_store._start(self.store, rid, self.root, 'test')
        self.assertEqual(len(seen), 2)
        for env in seen:
            for key in ('CADRE_DISK_MIN_MB', 'E2E_BROKEN', 'CADRE_RUN_ID'):   # CADRE_MODEL_WAIT was removed with P3
                self.assertNotIn(key, env)
            self.assertEqual(env['CADRE_HOME'], str(self.home))
            self.assertEqual(env['CADRE_ENGINE_ROOT'], str(self.root))
            self.assertEqual(env['CADRE_REAL_OPENCODE'], '/fake/opencode')

    def test_guard_runs_rotation_when_disk_is_low(self):
        """P2(b): the disk threshold and rotation were added to the EXISTING guard loop (no new guard): below the threshold rotation runs on the tick."""
        guard = subprocess.Popen(['bash', str(self.root / 'bin/health-watch.sh')], env={**self.env, 'CADRE_DISK_MIN_MB': str(10 ** 9), 'CADRE_WATCH_INTERVAL': '1'},
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(self.guard_close, guard)
        log = self.home / 'logs/health-watch.log'
        self.wait_for(lambda: log.exists() and 'rotation:' in log.read_text(), 20, 'rotation line')
        text = log.read_text()
        self.assertIn('WARNING disk low', text)
        self.assertRegex(text, r"rotation: \{'archived': \d+, 'retained': \d+, 'errors': 0\}")
        self.assertFalse((self.home / 'LIVE-LEAK').exists())


class ActiveHealthLogTests(IsolatedNight):
    def setUp(self):
        super().setUp()
        self.env.update(CADRE_NO_NET='1', CADRE_DISK_MIN_MB='0', CADRE_WATCH_INTERVAL='1')
        reports = self.home / 'reports'
        reports.mkdir(exist_ok=True)
        (reports / time.strftime('SUMMARY-%Y%m%d.md')).write_text('fixture\n')

    def active_run(self, project='e2e', parent=None):
        record = self.store.accept(project, 'health fixture')
        rid = record['run_id']
        lock = run_store.FileLock(self.store.path(rid) / '.owner.lock')
        self.assertTrue(lock.acquire(blocking=False))
        self.addCleanup(lock.close)
        self.store.update(rid, state='running', owner_pid=os.getpid(), owner_identity=run_store.process_identity(os.getpid()))
        if parent:
            self.store.update(rid, project_ref_parent=parent)
            self.store.update(parent, project_active_run=rid)
        path = self.store.path(rid) / 'coordinator.log'
        path.write_text('fixture coordinator\n')
        return rid, lock

    def old_log(self, path, seconds=7200):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('Review Agent Plan Agent RECOVERY\n')
        then = time.time() - seconds
        os.utime(path, (then, then))

    def start_attempt(self, rid, lock, tid='main', adapter='opencode', role='impl'):
        from session import Attempt, project_tool_policy
        env = {**self.env, 'CADRE_RUN_ID': rid, 'CADRE_TASK_ID': tid}
        def fixture_policy(store, record, directory, wt, selected_adapter, selected_role, selected_env, **kwargs):
            return project_tool_policy(store, record, directory, wt, 'opencode', selected_role, selected_env, **kwargs)
        with patch.dict(os.environ, env), patch('session.project_tool_policy', side_effect=fixture_policy):
            attempt = Attempt(adapter, role, 'fixture/model', 'health fixture')
            self.addCleanup(attempt.lease.close)
            process = attempt.launch([sys.executable, '-c', 'import time; print("fixture output", flush=True); time.sleep(120)'],
                                     str(self.repo), attempt.root / 'runner.log', env, (lock.fd,))
        def stop():
            if process.proc.poll() is None:
                process.proc.kill()
            process.proc.wait(timeout=5)
        self.addCleanup(stop)
        self.wait_for(lambda: (attempt.directory / 'process.json').exists() and attempt.stdout_path.stat().st_size > 0,
                      10, 'owned executor output')
        return attempt, process

    def health_cli(self):
        result = subprocess.run([sys.executable, str(self.root / 'lib/run_store.py'), 'health-logs'], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(result.stderr)
        return result.stdout

    def guard(self):
        guard = subprocess.Popen(['bash', str(self.root / 'bin/health-watch.sh')], env=self.env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(self.guard_close, guard)
        return guard

    def guard_tick(self, after=0):
        path = self.home / 'logs/health-watch.log'
        def complete():
            if not path.exists():
                return False
            return path.read_text()[after:].count('run ledger') >= 1
        self.wait_for(complete, 20, 'complete health tick')
        return path.read_text()[after:]

    def test_no_run_and_stale_logs_do_not_warn_or_create_escalation(self):
        self.old_log(self.home / 'logs/job-ancient.log')
        self.guard()
        output = self.guard_tick()
        self.assertIn('log:NO_ACTIVE', output)
        self.assertNotIn('WARNING log', output)
        self.assertNotIn('agent:', output)
        self.assertNotIn('WITHOUT HANDOFF', output)
        self.assertFalse((self.home / 'chain/handover-escalation.flag').exists())
        self.assertFalse((self.home / 'LIVE-LEAK').exists())
        self.assertEqual(self.health_cli().strip(), 'log:NO_ACTIVE')

    def test_coordinator_silence_uses_owned_log_and_read_only_cli(self):
        import hashlib
        rid, _ = self.active_run()
        self.old_log(self.store.path(rid) / 'coordinator.log')
        (self.home / 'logs/job-newer.log').write_text('unrelated recent output\n')
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.store.root.rglob('*') if p.is_file()}
        output = self.health_cli()
        after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.store.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertIn(f'WARNING log run={rid}', output)
        self.assertIn('role=coordinator', output)
        self.assertNotIn('job-newer', output)
        self.guard()
        self.assertIn(f'WARNING log run={rid}', self.guard_tick())

    def test_child_current_attempt_beats_parent_old_attempt_and_legacy_logs(self):
        parent, _ = self.active_run()
        child, lock = self.active_run('child', parent)
        self.old_log(self.store.path(parent) / 'coordinator.log')
        self.old_log(self.store.path(child) / 'coordinator.log')
        self.old_log(self.home / 'logs/job-ancient.log')
        attempt, _ = self.start_attempt(child, lock)
        self.old_log(attempt.root / 'attempts/old/stdout.jsonl')
        output = self.health_cli()
        self.assertIn(f'log:OK run={child} parent={parent}', output)
        self.assertIn(f'attempt={attempt.data["attempt_id"]}', output)
        self.assertNotIn('WARNING log', output)
        self.assertNotIn('coordinator.log', output)
        self.guard()
        self.assertIn(f'attempt={attempt.data["attempt_id"]}', self.guard_tick())

    def test_same_guard_rebinds_child_and_attempt_after_restart(self):
        parent, _ = self.active_run()
        first, first_lock = self.active_run('first-child', parent)
        old, process = self.start_attempt(first, first_lock)
        self.guard()
        self.assertIn(f'attempt={old.data["attempt_id"]}', self.guard_tick())
        process.proc.kill()
        process.proc.wait(timeout=5)
        self.store.update(first, state='completed', owner_pid=None)
        first_lock.close()
        second, second_lock = self.active_run('second-child', parent)
        current, _ = self.start_attempt(second, second_lock)
        standalone, standalone_lock = self.active_run('standalone-claude')
        claude, _ = self.start_attempt(standalone, standalone_lock, adapter='claude')
        self.old_log(old.stdout_path)
        after = (self.home / 'logs/health-watch.log').stat().st_size
        output = self.guard_tick(after)
        self.assertIn(f'attempt={current.data["attempt_id"]}', output)
        self.assertIn(f'run={second} parent={parent}', output)
        self.assertIn(f'attempt={claude.data["attempt_id"]}', output)
        self.assertNotIn(f'attempt={old.data["attempt_id"]}', output)
        self.assertNotIn('WARNING log', output)

    def test_parallel_attempts_have_independent_silence_and_exact_threshold(self):
        rid, lock = self.active_run()
        quiet, _ = self.start_attempt(rid, lock, tid='g1')
        fresh, _ = self.start_attempt(rid, lock, tid='g2')
        self.old_log(quiet.stdout_path)
        output = self.health_cli()
        self.assertIn(f'WARNING log run={rid} parent=- task=g1', output)
        self.assertIn(f'log:OK run={rid} parent=- task=g2', output)
        stamp = fresh.stdout_path.stat().st_mtime
        at = run_store.health_logs(self.store, stamp + 2700)
        over = run_store.health_logs(self.store, stamp + 2701)
        self.assertEqual(next(e['status'] for e in at['logs'] if e['task_id'] == 'g2'), 'ok')
        self.assertEqual(next(e['status'] for e in over['logs'] if e['task_id'] == 'g2'), 'silent')
        during = run_store.health_logs(self.store, stamp - 1)
        self.assertEqual(next(e['age_seconds'] for e in during['logs'] if e['task_id'] == 'g2'), 0)
        self.assertFalse((self.home / 'chain/handover-escalation.flag').exists())

    def test_unknown_owner_corrupt_attempt_and_wrong_identity_never_pick_stale_log(self):
        rid, lock = self.active_run()
        attempt, _ = self.start_attempt(rid, lock)
        self.old_log(self.home / 'logs/job-ancient.log')
        path = attempt.directory / 'process.json'
        original = path.read_bytes()
        for mutation in (b'{', json.dumps({**json.loads(original), 'attempt_id': 'wrong'}).encode()):
            with self.subTest(mutation=mutation[:1]):
                path.write_bytes(mutation)
                output = self.health_cli()
                self.assertIn('log:UNCERTAIN', output)
                self.assertNotIn('WARNING log', output)
                self.assertNotIn('stdout.jsonl', output)
        path.write_bytes(original)
        identity = run_store.process_identity(os.getpid())
        self.store.update(rid, owner_identity={**identity, 'started': 'previous-process'})
        self.assertIn('log:UNCERTAIN', self.health_cli())
        self.store.update(rid, owner_identity=identity)
        self.assertIn(f'attempt={attempt.data["attempt_id"]}', self.health_cli())
        run_path = self.store.path(rid) / 'run.json'
        saved = run_path.read_bytes()
        run_path.write_text('{')
        try:
            other, _ = self.active_run('other')
            output = self.health_cli()
            self.assertIn(f'log:UNCERTAIN run={rid}', output)
            self.assertIn(f'log:OK run={other}', output)
            self.assertNotIn('WARNING log', output)
        finally:
            run_path.write_bytes(saved)

    def test_unsafe_log_types_and_future_time_are_unknown_without_reading_output(self):
        rid, lock = self.active_run()
        attempt, _ = self.start_attempt(rid, lock)
        target = Path(self.temp.name) / 'outside.log'
        target.write_text('fixture confidential content\n')
        saved = attempt.stdout_path.read_bytes()
        attempt.stdout_path.unlink()
        for kind in ('symlink', 'hardlink', 'fifo', 'future'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    attempt.stdout_path.symlink_to(target)
                elif kind == 'hardlink':
                    os.link(target, attempt.stdout_path)
                elif kind == 'fifo':
                    os.mkfifo(attempt.stdout_path)
                else:
                    attempt.stdout_path.write_bytes(saved)
                    os.utime(attempt.stdout_path, (time.time() + 3600, time.time() + 3600))
                output = self.health_cli()
                self.assertIn('log:UNCERTAIN', output)
                self.assertNotIn('WARNING log', output)
                self.assertNotIn('confidential', output)
                attempt.stdout_path.unlink()
        attempt.stdout_path.write_bytes(saved)

    def test_completed_child_returns_monitoring_to_parent_and_orphan_is_unknown(self):
        parent, _ = self.active_run()
        child, lock = self.active_run('child', parent)
        attempt, process = self.start_attempt(child, lock)
        process.proc.kill()
        process.proc.wait(timeout=5)
        self.store.update(child, state='completed', owner_pid=None)
        lock.close()
        self.assertIn(f'log:OK run={parent}', self.health_cli())
        self.store.update(parent, project_active_run=None)
        self.store.update(child, state='running', owner_pid=os.getpid(), owner_identity=run_store.process_identity(os.getpid()))
        self.assertTrue(lock.acquire(blocking=False))
        output = self.health_cli()
        self.assertIn(f'log:UNCERTAIN run={child}', output)
        self.assertNotIn(f'attempt={attempt.data["attempt_id"]}', output)

    def test_changed_snapshot_and_concurrent_readers_do_not_mutate_or_claim_old_health(self):
        import concurrent.futures
        import hashlib
        rid, lock = self.active_run()
        attempt, _ = self.start_attempt(rid, lock)
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.store.root.rglob('*') if p.is_file()}
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            outputs = list(pool.map(lambda _: self.health_cli(), range(4)))
        after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.store.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        for output in outputs:
            self.assertIn(f'attempt={attempt.data["attempt_id"]}', output)
            self.assertNotIn('WARNING', output)
        real_read = run_store.health_json
        count = 0
        def changed(store, path):
            nonlocal count
            if path == self.store.path(rid) / 'run.json':
                count += 1
                if count == 2:
                    self.store.update(rid, state='cancelled', owner_pid=None)
            return real_read(store, path)
        with patch.object(run_store, 'health_json', side_effect=changed):
            result = run_store.health_logs(self.store)
        self.assertEqual(result['logs'], [])
        self.assertEqual(result['unknown'][0]['run_id'], rid)

    def test_restarted_guard_ignores_finished_run_and_monitoring_stops_after_cancel(self):
        first, lock = self.active_run()
        first_guard = self.guard()
        self.assertIn(f'log:OK run={first}', self.guard_tick())
        os.killpg(first_guard.pid, signal.SIGTERM)
        first_guard.wait(timeout=5)
        self.store.update(first, state='completed', owner_pid=None)
        lock.close()
        self.old_log(self.store.path(first) / 'coordinator.log')
        second, second_lock = self.active_run('restarted')
        after = (self.home / 'logs/health-watch.log').stat().st_size
        self.guard()
        output = self.guard_tick(after)
        self.assertIn(f'log:OK run={second}', output)
        self.assertNotIn(first, output)
        self.assertNotIn('WARNING log', output)
        self.store.update(second, state='cancelled', owner_pid=None)
        second_lock.close()
        after = (self.home / 'logs/health-watch.log').stat().st_size
        self.assertIn('log:NO_ACTIVE', self.guard_tick(after))
        self.assertFalse((self.home / 'chain/handover-escalation.flag').exists())


if __name__ == '__main__':
    unittest.main()


class NightManagerIsolationTests(IsolatedNight):
    """B110 (18.09): `night-manager.sh` hard-coded the home and the CODE ROOT (`$HOME/.local/share/...`), so even in an isolated
    home it read the LIVE install and could not be tested; it also measured liveness with `pgrep -f` (our own
    command line could match). Now CADRE_HOME/CADRE_ENGINE_ROOT are authoritative, liveness comes from the recorded guard measure."""

    def test_the_night_manager_stays_in_the_isolated_home_and_reads_the_watchdog_registry(self):
        (self.home / 'logs').mkdir(parents=True, exist_ok=True)
        (self.home / 'logs/health-watch.log').write_text('WARNING attempt\n')
        env = {**self.env, 'CADRE_NIGHT_REVIVE': '0'}          # the test leaves no background process by reviving
        r = subprocess.run(['bash', str(self.root / 'bin/health-summary.sh')], env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertIn('WATCHER:', r.stdout)
        self.assertIn('health-watch:DEAD', r.stdout, 'no registered guard must not show as LIVE')
        self.assertIn('capacity-watch:DEAD', r.stdout)
        self.assertIn('health last round', r.stdout, 'the guard log in the isolated home must be read')
        self.assertFalse((self.home / 'LIVE-LEAK').exists(), 'the live install must not be touched')


class GuardFamilySmokeTests(IsolatedNight):
    """B110 (18.09): the guard family (`canary`, `game-guard`, `error-browser`, `verifier`, `claude-quota`)
    was UNTESTED and all of them hard-coded the home — even in an isolated home they read the LIVE install. This smoke test
    measures that all of them run in an isolated home and do not touch the live root."""

    def execute(self, entry_name, second=30):
        try:
            r = subprocess.run(['bash', str(self.root / 'bin' / entry_name)], env=self.env,
                               capture_output=True, text=True, timeout=second)
            return (r.stdout + r.stderr)
        except subprocess.TimeoutExpired as moment:          # looping guard: cut when its time is up
            return (moment.stdout or b'').decode() + (moment.stderr or b'').decode()

    def test_every_watchdog_runs_in_the_isolated_home_without_touching_the_live_root(self):
        for entry_name, second in (('error-browser.sh', 60), ('verifier.sh', 60), ('claude-quota.sh', 60),
                           ('canary.sh', 5), ('game-guard.sh', 5)):
            with self.subTest(guard=entry_name):
                output = self.execute(entry_name, second)
                self.assertNotIn('Traceback', output, f'{entry_name}: crash')
                self.assertNotIn('unbound variable', output, f'{entry_name}: undefined variable')
                self.assertFalse((self.home / 'LIVE-LEAK').exists(),
                                 f'{entry_name}: touched the live install -> {(self.home / "LIVE-LEAK").read_text()[:200] if (self.home / "LIVE-LEAK").exists() else ""}')


class PeriodicCommitTests(IsolatedNight):
    """T0/V4: in a long run an intermediate commit is made every 10 minutes — if the session crashes the work stays committed on disk.
    Previously this was "verified" only by searching for `PERIODIC CKPT` in the source text; now the behaviour is measured."""

    def test_a_long_run_starts_the_periodic_checkpoint_writer(self):
        from unittest.mock import patch
        import qe_core as core
        f = fixtures.SessionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.enter(patch.object(core, 'project_memory', return_value=[]))
        f.runner_fixture(standalone=True, env={'CADRE_MAX_RESUME': '0'})
        ckpt = [c for c in f.popen_calls if c[:2] == ['bash', '-c'] and 'commit' in c[2] and 'sleep' in c[2]]
        self.assertTrue(ckpt, f'The periodic checkpoint process was not started: {f.popen_calls[:3]}')
        self.assertIn('600', ckpt[0][2], 'The checkpoint interval must be 10 minutes')
