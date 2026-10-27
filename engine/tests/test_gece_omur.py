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
import test_kosu_sessions as fixtures
import test_uctan_uca as e2e  # noqa: E402  (fake opencode + fake budget + isolated root setup)
import kosu  # noqa: E402
from kosu import RunStore  # noqa: E402


def setUpModule():
    ortam = patch.dict(os.environ, {'CADRE_ZINCIR_KALITE': 'opencode-go/qwen3.8-max'})
    ortam.start()
    unittest.addModuleCleanup(ortam.stop)


SHIM = '''#!{python}
import json, os, sys, time
if os.environ.get('CADRE_ARGV_LOG'):  # B348: QA ortamı CADRE_* taşımaz; ürün testi motor argv'si değildir
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
        for name in ('kosu.py', 'butce.py', 'kosu_defteri.py', 'qe_core.py', 'canlilik.py', 'kabul.py', 'motor.py', 'rotasyon.py', 'yonlendirici.py'):
            (live / 'lib' / name).write_text(LEAK_PY)
        (live / 'lib/common.sh').write_text(LEAK)
        for name in ('saglik-nobet.sh', 'kota-izle.sh', 'is-motoru.sh', 'cadre-dispatch.sh', 'orkestra.sh', 'kisitli-kabuk.sh', 'meta-gozetmen.sh', 'ders-ozeti.sh'):
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
                        CADRE_NOBET_ARALIK='2', CADRE_BEKCI_KONTROL='1', CADRE_KOORD_TIK='0.2')

    def bekci_kapat(self, guard):
        """B219: the watch's tick children and the watchers it revives (in their own process groups) must not keep writing to the temporary home."""
        gruplar = [guard.pid]
        for pidfile in (self.home / 'zincir').glob('*.pid'):
            try:
                gruplar.append(int(pidfile.read_text().split()[0]))
            except (OSError, ValueError, IndexError):
                pass
        for pid in gruplar:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        kok = str(Path(self.temp.name))   # a watcher that has not written its pidfile yet also carries the fixture path on its command line
        for _ in range(50):
            satirlar = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True).stdout.splitlines()
            kalan = [int(s.split(None, 1)[0]) for s in satirlar if kok in s and int(s.split(None, 1)[0]) != os.getpid()]
            if not kalan:
                break
            for pid in kalan:
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
        (self.root/'bin/is-motoru.sh').write_text('exit 0\n')
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
             patch.object(kosu, 'bekci_dirilt', return_value=[],
                          side_effect=RuntimeError('fixture error') if fail else None), \
             patch.object(kosu, 'kuyruk_baslat'):
            if fail:
                with self.assertRaisesRegex(RuntimeError, 'fixture error'):
                    kosu.execute_run(self.store, rid)
            else:
                self.assertNotEqual(kosu.execute_run(self.store, rid), 0)  # no QA/judge evidence.
        child, = self.children
        self.assertIsNotNone(child.returncode, 'koordinator donmeden cocuk wait ile toplanmali')
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
        log = home / 'logs/saglik-nobet.log'
        log.write_text('')
        tail = subprocess.Popen(['tail', '-f', str(log)])
        self.addCleanup(lambda: (tail.kill(), tail.wait(timeout=5)))
        (home / 'zincir/saglik-nobet.pid').write_text(str(tail.pid))
        self.assertFalse(kosu.bekci_canli(home, 'saglik-nobet'))
        (home / 'zincir/kota-izle.pid').write_text('999999')
        self.assertFalse(kosu.bekci_canli(home, 'kota-izle'))
        with patch.object(kosu, 'bekci_baslat') as baslat:
            self.assertEqual(sorted(kosu.bekci_dirilt(home, root)), ['kota-izle', 'saglik-nobet'])
            self.assertEqual(baslat.call_count, 2)
        # stale heartbeat (pid alive, argv right) -> counted as dead
        script = Path(self.temp.name) / 'saglik-nobet.sh'
        script.write_text('#!/bin/bash\nsleep 10\n')
        script.chmod(0o755)
        proc = subprocess.Popen(['bash', str(script)])
        self.addCleanup(lambda: (proc.kill(), proc.wait(timeout=5)))
        (home / 'zincir/saglik-nobet.pid').write_text(str(proc.pid))
        (home / 'zincir/saglik-nobet.tik').write_text(str(int(time.time()) - 10 * 3600))
        with patch.dict(os.environ, {'CADRE_NOBET_ARALIK': '60'}):
            self.assertFalse(kosu.bekci_canli(home, 'saglik-nobet'))

    def test_guard_script_defaults_match_the_liveness_clock(self):
        """The coordinator measures the heartbeat with the BEKCILER interval and the interval environment is not passed to the guard (B75); if the script default
        differs, a healthy guard is counted as 'hung' and killed (07.10: script 1800, coordinator 600)."""
        for ad, (anahtar, aralik) in kosu.BEKCILER.items():
            self.assertIn(f'${{{anahtar}:-{aralik}}}', (ENGINE / 'bin' / f'{ad}.sh').read_text(), ad)

    def coordinator_fixture(self, dispatcher_body):
        """Real kosu.py execute; fake dispatcher; fake guards write a pidfile and sleep."""
        (self.root / 'bin/cadre-dispatch.sh').write_text(dispatcher_body)
        for name in ('saglik-nobet', 'kota-izle'):
            (self.root / f'bin/{name}.sh').write_text(
                f'#!/bin/bash\nmkdir -p "$CADRE_HOME/zincir"; echo $$ > "$CADRE_HOME/zincir/{name}.pid"; '
                f'while :; do date +%s > "$CADRE_HOME/zincir/{name}.tik"; echo tick >> "$CADRE_HOME/{name}-ticks"; sleep 0.5; done\n')
        run = self.store.accept('e2e', 'omur testi')
        return run['run_id']

    def guard_pid(self, name):
        try:
            return int((self.home / 'zincir' / f'{name}.pid').read_text().strip())
        except (OSError, ValueError):
            return None

    def test_coordinator_revives_a_killed_guard_within_its_check_window(self):
        """P2(c1): guard SIGKILL -> a new pidfile at the coordinator tick (CADRE_BEKCI_KONTROL), alive, a different pid."""
        rid = self.coordinator_fixture('#!/bin/bash\nsleep 6\nexit 3\n')
        (self.root / 'bin/is-motoru.sh').write_text('#!/bin/bash\nexit 1\n')
        proc = subprocess.Popen([sys.executable, str(self.root / 'lib/kosu.py'), 'run', rid], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait(timeout=5), proc.stdout.close()))
        self.wait_for(lambda: self.guard_pid('saglik-nobet') and pid_alive(self.guard_pid('saglik-nobet')), 5, 'guard start')
        first = self.guard_pid('saglik-nobet')
        os.kill(first, signal.SIGKILL)
        self.wait_for(lambda: not pid_alive(first), 3, 'guard death')
        self.wait_for(lambda: (self.guard_pid('saglik-nobet') not in (None, first)) and pid_alive(self.guard_pid('saglik-nobet')), 5, 'guard revive')
        self.assertTrue(kosu.bekci_canli(self.home, 'saglik-nobet'))
        rc = proc.wait(timeout=30)
        self.assertEqual(rc, 1, proc.stdout.read())
        self.assertEqual(self.store.read(rid)['state'], 'retryable')

    def test_real_guard_script_revives_a_dead_coordinator_and_reports_disk(self):
        """P2(c2)+P1: the REAL saglik-nobet.sh runs from the isolated root; it revives a run with state=running + an empty owner lock with `kosu.py revive`
        (revive_count=1, revive #1 in start.log); writes the disk line; leaves a heartbeat and a pidfile."""
        run = self.store.accept('e2e', 'olu koordinator')
        rid = run['run_id']
        self.store.update(rid, state='running', phase='motor', owner_pid=999999)
        (self.root / 'bin/is-motoru.sh').write_text('#!/bin/bash\nexit 1\n')
        (self.root / 'bin/cadre-dispatch.sh').write_text('#!/bin/bash\nexit 3\n')
        guard = subprocess.Popen(['bash', str(self.root / 'bin/saglik-nobet.sh')], env={**self.env, 'CADRE_DISK_MIN_MB': '1'},
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(self.bekci_kapat, guard)
        self.wait_for(lambda: (self.home / 'zincir/saglik-nobet.pid').exists(), 5, 'pidfile')
        self.assertEqual(int((self.home / 'zincir/saglik-nobet.pid').read_text()), guard.pid)
        self.wait_for(lambda: (self.home / 'zincir/saglik-nobet.tik').exists(), 5, 'heartbeat')
        self.wait_for(lambda: self.store.read(rid).get('revive_count') == 1, 15, 'revive')
        self.wait_for(lambda: self.store.read(rid)['state'] in ('retryable', 'failed'), 30, 'revived coordinator finished')
        self.assertIn('revive #1', (self.store.path(rid) / 'start.log').read_text())
        log = (self.home / 'logs/saglik-nobet.log').read_text()
        self.assertRegex(log, r'disk: \d+ MB bos')
        self.assertFalse((self.home / 'LIVE-LEAK').exists())
        self.assertTrue(kosu.bekci_canli(self.home, 'saglik-nobet'))

    def test_low_disk_starts_no_executor_and_records_the_reason(self):
        """P2(c4): free space below the threshold -> executor Popen 0, at the wait cap state=failed, reason=disk-low."""
        rid = self.coordinator_fixture('#!/bin/bash\necho dispatch >> "$CADRE_RUN_DIR/calls"\nexit 3\n')
        (self.root / 'bin/is-motoru.sh').write_text('#!/bin/bash\necho motor >> "$CADRE_RUN_DIR/calls"\nexit 0\n')
        env = {**self.env, 'CADRE_DISK_MIN_MB': str(10 ** 9), 'CADRE_DISK_WAIT': '1', 'CADRE_DISK_POLL': '0.2'}
        t0 = time.monotonic()
        result = subprocess.run([sys.executable, str(self.root / 'lib/kosu.py'), 'run', rid], env=env, capture_output=True, text=True, timeout=30)
        elapsed = time.monotonic() - t0
        record = self.store.read(rid)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.store.path(rid) / 'calls').exists(), 'executor must not start on low disk')
        self.assertEqual(record['state'], 'failed')
        self.assertTrue(record.get('reason', '').startswith('disk-low'), record.get('reason'))
        self.assertLess(elapsed, 15)
        # normal threshold -> the same record is not retryable (stays failed); a new record takes the normal path
        rid2 = self.store.accept('e2e', 'disk normal')['run_id']
        subprocess.run([sys.executable, str(self.root / 'lib/kosu.py'), 'run', rid2], env={**self.env, 'CADRE_DISK_MIN_MB': '1'},
                       capture_output=True, text=True, timeout=30)
        self.assertEqual((self.store.path(rid2) / 'calls').read_text().split(), ['dispatch', 'motor'])

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
        coordinator_cmd = [e for e in self.argv() if e['tool'] == 'python3' and any(a.endswith('kosu.py') for a in e['args']) and 'execute' in e['args']]
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
        with patch.dict(os.environ, {'CADRE_DISK_MIN_MB': '999999999', 'E2E_BROKEN': 'b', 'CADRE_REAL_OPENCODE': '/fake/opencode', 'CADRE_RUN_ID': 'eski-kosu'}), \
             patch.object(kosu.subprocess, 'Popen', side_effect=lambda cmd, **kw: seen.append(kw.get('env') or {}) or real_popen(['sleep', '0'])):
            kosu.bekci_baslat(self.home, self.root, self.root / 'bin/saglik-nobet.sh')
            kosu._baslat(self.store, rid, self.root, 'test')
        self.assertEqual(len(seen), 2)
        for env in seen:
            for key in ('CADRE_DISK_MIN_MB', 'E2E_BROKEN', 'CADRE_RUN_ID'):   # CADRE_MODEL_WAIT was removed with P3
                self.assertNotIn(key, env)
            self.assertEqual(env['CADRE_HOME'], str(self.home))
            self.assertEqual(env['CADRE_ENGINE_ROOT'], str(self.root))
            self.assertEqual(env['CADRE_REAL_OPENCODE'], '/fake/opencode')

    def test_guard_runs_rotation_when_disk_is_low(self):
        """P2(b): the disk threshold and rotation were added to the EXISTING guard loop (no new guard): below the threshold rotation runs on the tick."""
        guard = subprocess.Popen(['bash', str(self.root / 'bin/saglik-nobet.sh')], env={**self.env, 'CADRE_DISK_MIN_MB': str(10 ** 9), 'CADRE_NOBET_ARALIK': '1'},
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(self.bekci_kapat, guard)
        log = self.home / 'logs/saglik-nobet.log'
        self.wait_for(lambda: log.exists() and 'rotasyon:' in log.read_text(), 20, 'rotation line')
        text = log.read_text()
        self.assertIn('UYARI disk dusuk', text)
        self.assertRegex(text, r"rotasyon: \{'archived': \d+, 'retained': \d+, 'errors': 0\}")
        self.assertFalse((self.home / 'LIVE-LEAK').exists())


class ActiveHealthLogTests(IsolatedNight):
    def setUp(self):
        super().setUp()
        self.env.update(CADRE_AG_YOK='1', CADRE_DISK_MIN_MB='0', CADRE_NOBET_ARALIK='1')
        reports = self.home / 'reports'
        reports.mkdir(exist_ok=True)
        (reports / time.strftime('OZET-%Y%m%d.md')).write_text('fixture\n')

    def active_run(self, project='e2e', parent=None):
        record = self.store.accept(project, 'health fixture')
        rid = record['run_id']
        lock = kosu.FileLock(self.store.path(rid) / '.owner.lock')
        self.assertTrue(lock.acquire(blocking=False))
        self.addCleanup(lock.close)
        self.store.update(rid, state='running', owner_pid=os.getpid(), owner_identity=kosu.process_identity(os.getpid()))
        if parent:
            self.store.update(rid, proje_ust=parent)
            self.store.update(parent, project_active_run=rid)
        path = self.store.path(rid) / 'coordinator.log'
        path.write_text('fixture coordinator\n')
        return rid, lock

    def old_log(self, path, seconds=7200):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('Review Agent Plan Agent KURTARICI\n')
        then = time.time() - seconds
        os.utime(path, (then, then))

    def start_attempt(self, rid, lock, tid='main', adapter='opencode', role='impl'):
        from oturum import Attempt, project_tool_policy
        env = {**self.env, 'CADRE_RUN_ID': rid, 'CADRE_GOREV_ID': tid}
        def fixture_policy(store, record, directory, wt, selected_adapter, selected_role, selected_env, **kwargs):
            return project_tool_policy(store, record, directory, wt, 'opencode', selected_role, selected_env, **kwargs)
        with patch.dict(os.environ, env), patch('oturum.project_tool_policy', side_effect=fixture_policy):
            attempt = Attempt(adapter, role, 'fixture/model', 'health fixture')
            self.addCleanup(attempt.lease.close)
            process = attempt.launch([sys.executable, '-c', 'import time; print("fixture output", flush=True); time.sleep(120)'],
                                     str(self.repo), attempt.root / 'motor.log', env, (lock.fd,))
        def stop():
            if process.proc.poll() is None:
                process.proc.kill()
            process.proc.wait(timeout=5)
        self.addCleanup(stop)
        self.wait_for(lambda: (attempt.directory / 'process.json').exists() and attempt.stdout_path.stat().st_size > 0,
                      10, 'owned executor output')
        return attempt, process

    def health_cli(self):
        result = subprocess.run([sys.executable, str(self.root / 'lib/kosu.py'), 'health-logs'], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(result.stderr)
        return result.stdout

    def guard(self):
        guard = subprocess.Popen(['bash', str(self.root / 'bin/saglik-nobet.sh')], env=self.env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(self.bekci_kapat, guard)
        return guard

    def guard_tick(self, after=0):
        path = self.home / 'logs/saglik-nobet.log'
        def complete():
            if not path.exists():
                return False
            return path.read_text()[after:].count('kosu defteri') >= 1
        self.wait_for(complete, 20, 'complete health tick')
        return path.read_text()[after:]

    def test_no_run_and_stale_logs_do_not_warn_or_create_escalation(self):
        self.old_log(self.home / 'logs/is-ancient.log')
        self.guard()
        output = self.guard_tick()
        self.assertIn('log:AKTIF_YOK', output)
        self.assertNotIn('UYARI log', output)
        self.assertNotIn('ajan:', output)
        self.assertNotIn('DEVIRSIZ', output)
        self.assertFalse((self.home / 'zincir/devir-eskalasyon.flag').exists())
        self.assertFalse((self.home / 'LIVE-LEAK').exists())
        self.assertEqual(self.health_cli().strip(), 'log:AKTIF_YOK')

    def test_coordinator_silence_uses_owned_log_and_read_only_cli(self):
        import hashlib
        rid, _ = self.active_run()
        self.old_log(self.store.path(rid) / 'coordinator.log')
        (self.home / 'logs/is-newer.log').write_text('unrelated recent output\n')
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.store.root.rglob('*') if p.is_file()}
        output = self.health_cli()
        after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.store.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertIn(f'UYARI log kosu={rid}', output)
        self.assertIn('rol=coordinator', output)
        self.assertNotIn('is-newer', output)
        self.guard()
        self.assertIn(f'UYARI log kosu={rid}', self.guard_tick())

    def test_child_current_attempt_beats_parent_old_attempt_and_legacy_logs(self):
        parent, _ = self.active_run()
        child, lock = self.active_run('child', parent)
        self.old_log(self.store.path(parent) / 'coordinator.log')
        self.old_log(self.store.path(child) / 'coordinator.log')
        self.old_log(self.home / 'logs/is-ancient.log')
        attempt, _ = self.start_attempt(child, lock)
        self.old_log(attempt.root / 'attempts/old/stdout.jsonl')
        output = self.health_cli()
        self.assertIn(f'log:OK kosu={child} parent={parent}', output)
        self.assertIn(f'deneme={attempt.data["attempt_id"]}', output)
        self.assertNotIn('UYARI log', output)
        self.assertNotIn('coordinator.log', output)
        self.guard()
        self.assertIn(f'deneme={attempt.data["attempt_id"]}', self.guard_tick())

    def test_same_guard_rebinds_child_and_attempt_after_restart(self):
        parent, _ = self.active_run()
        first, first_lock = self.active_run('first-child', parent)
        old, process = self.start_attempt(first, first_lock)
        self.guard()
        self.assertIn(f'deneme={old.data["attempt_id"]}', self.guard_tick())
        process.proc.kill()
        process.proc.wait(timeout=5)
        self.store.update(first, state='completed', owner_pid=None)
        first_lock.close()
        second, second_lock = self.active_run('second-child', parent)
        current, _ = self.start_attempt(second, second_lock)
        standalone, standalone_lock = self.active_run('standalone-claude')
        claude, _ = self.start_attempt(standalone, standalone_lock, adapter='claude')
        self.old_log(old.stdout_path)
        after = (self.home / 'logs/saglik-nobet.log').stat().st_size
        output = self.guard_tick(after)
        self.assertIn(f'deneme={current.data["attempt_id"]}', output)
        self.assertIn(f'kosu={second} parent={parent}', output)
        self.assertIn(f'deneme={claude.data["attempt_id"]}', output)
        self.assertNotIn(f'deneme={old.data["attempt_id"]}', output)
        self.assertNotIn('UYARI log', output)

    def test_parallel_attempts_have_independent_silence_and_exact_threshold(self):
        rid, lock = self.active_run()
        quiet, _ = self.start_attempt(rid, lock, tid='g1')
        fresh, _ = self.start_attempt(rid, lock, tid='g2')
        self.old_log(quiet.stdout_path)
        output = self.health_cli()
        self.assertIn(f'UYARI log kosu={rid} parent=- gorev=g1', output)
        self.assertIn(f'log:OK kosu={rid} parent=- gorev=g2', output)
        stamp = fresh.stdout_path.stat().st_mtime
        at = kosu.health_logs(self.store, stamp + 2700)
        over = kosu.health_logs(self.store, stamp + 2701)
        self.assertEqual(next(e['status'] for e in at['logs'] if e['task_id'] == 'g2'), 'ok')
        self.assertEqual(next(e['status'] for e in over['logs'] if e['task_id'] == 'g2'), 'silent')
        during = kosu.health_logs(self.store, stamp - 1)
        self.assertEqual(next(e['age_seconds'] for e in during['logs'] if e['task_id'] == 'g2'), 0)
        self.assertFalse((self.home / 'zincir/devir-eskalasyon.flag').exists())

    def test_unknown_owner_corrupt_attempt_and_wrong_identity_never_pick_stale_log(self):
        rid, lock = self.active_run()
        attempt, _ = self.start_attempt(rid, lock)
        self.old_log(self.home / 'logs/is-ancient.log')
        path = attempt.directory / 'process.json'
        original = path.read_bytes()
        for mutation in (b'{', json.dumps({**json.loads(original), 'attempt_id': 'wrong'}).encode()):
            with self.subTest(mutation=mutation[:1]):
                path.write_bytes(mutation)
                output = self.health_cli()
                self.assertIn('log:BELIRSIZ', output)
                self.assertNotIn('UYARI log', output)
                self.assertNotIn('stdout.jsonl', output)
        path.write_bytes(original)
        identity = kosu.process_identity(os.getpid())
        self.store.update(rid, owner_identity={**identity, 'started': 'previous-process'})
        self.assertIn('log:BELIRSIZ', self.health_cli())
        self.store.update(rid, owner_identity=identity)
        self.assertIn(f'deneme={attempt.data["attempt_id"]}', self.health_cli())
        run_path = self.store.path(rid) / 'run.json'
        saved = run_path.read_bytes()
        run_path.write_text('{')
        try:
            other, _ = self.active_run('other')
            output = self.health_cli()
            self.assertIn(f'log:BELIRSIZ kosu={rid}', output)
            self.assertIn(f'log:OK kosu={other}', output)
            self.assertNotIn('UYARI log', output)
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
                self.assertIn('log:BELIRSIZ', output)
                self.assertNotIn('UYARI log', output)
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
        self.assertIn(f'log:OK kosu={parent}', self.health_cli())
        self.store.update(parent, project_active_run=None)
        self.store.update(child, state='running', owner_pid=os.getpid(), owner_identity=kosu.process_identity(os.getpid()))
        self.assertTrue(lock.acquire(blocking=False))
        output = self.health_cli()
        self.assertIn(f'log:BELIRSIZ kosu={child}', output)
        self.assertNotIn(f'deneme={attempt.data["attempt_id"]}', output)

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
            self.assertIn(f'deneme={attempt.data["attempt_id"]}', output)
            self.assertNotIn('UYARI', output)
        real_read = kosu.health_json
        count = 0
        def changed(store, path):
            nonlocal count
            if path == self.store.path(rid) / 'run.json':
                count += 1
                if count == 2:
                    self.store.update(rid, state='cancelled', owner_pid=None)
            return real_read(store, path)
        with patch.object(kosu, 'health_json', side_effect=changed):
            result = kosu.health_logs(self.store)
        self.assertEqual(result['logs'], [])
        self.assertEqual(result['unknown'][0]['run_id'], rid)

    def test_restarted_guard_ignores_finished_run_and_monitoring_stops_after_cancel(self):
        first, lock = self.active_run()
        first_guard = self.guard()
        self.assertIn(f'log:OK kosu={first}', self.guard_tick())
        os.killpg(first_guard.pid, signal.SIGTERM)
        first_guard.wait(timeout=5)
        self.store.update(first, state='completed', owner_pid=None)
        lock.close()
        self.old_log(self.store.path(first) / 'coordinator.log')
        second, second_lock = self.active_run('restarted')
        after = (self.home / 'logs/saglik-nobet.log').stat().st_size
        self.guard()
        output = self.guard_tick(after)
        self.assertIn(f'log:OK kosu={second}', output)
        self.assertNotIn(first, output)
        self.assertNotIn('UYARI log', output)
        self.store.update(second, state='cancelled', owner_pid=None)
        second_lock.close()
        after = (self.home / 'logs/saglik-nobet.log').stat().st_size
        self.assertIn('log:AKTIF_YOK', self.guard_tick(after))
        self.assertFalse((self.home / 'zincir/devir-eskalasyon.flag').exists())


if __name__ == '__main__':
    unittest.main()


class GeceMuduruIzolasyonTests(IsolatedNight):
    """B110 (18.09): `gece-muduru.sh` hard-coded the home and the CODE ROOT (`$HOME/.local/share/...`), so even in an isolated
    home it read the LIVE install and could not be tested; it also measured liveness with `pgrep -f` (our own
    command line could match). Now CADRE_HOME/CADRE_ENGINE_ROOT are authoritative, liveness comes from the recorded guard measure."""

    def test_the_night_manager_stays_in_the_isolated_home_and_reads_the_watchdog_registry(self):
        (self.home / 'logs').mkdir(parents=True, exist_ok=True)
        (self.home / 'logs/saglik-nobet.log').write_text('UYARI deneme\n')
        env = {**self.env, 'CADRE_GECE_DIRILT': '0'}          # the test leaves no background process by reviving
        r = subprocess.run(['bash', str(self.root / 'bin/saglik-ozeti.sh')], env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertIn('IZLEYICI:', r.stdout)
        self.assertIn('saglik-nobet:OLU', r.stdout, 'kayitli bekci yokken CANLI gorunmemeli')
        self.assertIn('kota-izle:OLU', r.stdout)
        self.assertIn('saglik son tur', r.stdout, 'izole evdeki bekci gunlugu okunmali')
        self.assertFalse((self.home / 'LIVE-LEAK').exists(), 'canli kuruluma dokunulmamali')


class BekciAilesiDumanTests(IsolatedNight):
    """B110 (18.09): the guard family (`kanarya`, `oyun-bekci`, `hata-tarayici`, `dogrulayici`, `claude-kota`)
    was UNTESTED and all of them hard-coded the home — even in an isolated home they read the LIVE install. This smoke test
    measures that all of them run in an isolated home and do not touch the live root."""

    def kos(self, ad, saniye=30):
        try:
            r = subprocess.run(['bash', str(self.root / 'bin' / ad)], env=self.env,
                               capture_output=True, text=True, timeout=saniye)
            return (r.stdout + r.stderr)
        except subprocess.TimeoutExpired as zaman:          # looping guard: cut when its time is up
            return (zaman.stdout or b'').decode() + (zaman.stderr or b'').decode()

    def test_every_watchdog_runs_in_the_isolated_home_without_touching_the_live_root(self):
        for ad, saniye in (('hata-tarayici.sh', 60), ('dogrulayici.sh', 60), ('claude-kota.sh', 60),
                           ('kanarya.sh', 5), ('oyun-bekci.sh', 5)):
            with self.subTest(bekci=ad):
                cikti = self.kos(ad, saniye)
                self.assertNotIn('Traceback', cikti, f'{ad}: cokme')
                self.assertNotIn('unbound variable', cikti, f'{ad}: tanimsiz degisken')
                self.assertFalse((self.home / 'LIVE-LEAK').exists(),
                                 f'{ad}: canli kuruluma dokundu -> {(self.home / "LIVE-LEAK").read_text()[:200] if (self.home / "LIVE-LEAK").exists() else ""}')


class PeriyodikCommitTests(IsolatedNight):
    """T0/V4: in a long run an intermediate commit is made every 10 minutes — if the session crashes the work stays committed on disk.
    Previously this was "verified" only by searching for `PERIYODIK CKPT` in the source text; now the behaviour is measured."""

    def test_a_long_run_starts_the_periodic_checkpoint_writer(self):
        from unittest.mock import patch
        import qe_core as core
        f = fixtures.SessionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.enter(patch.object(core, 'project_memory', return_value=[]))
        f.motor_fixture(standalone=True, env={'CADRE_MAX_RESUME': '0'})
        ckpt = [c for c in f.popen_calls if c[:2] == ['bash', '-c'] and 'commit' in c[2] and 'sleep' in c[2]]
        self.assertTrue(ckpt, f'periyodik ara commit sureci baslatilmadi: {f.popen_calls[:3]}')
        self.assertIn('600', ckpt[0][2], 'ara commit araligi 10 dakika olmali')
