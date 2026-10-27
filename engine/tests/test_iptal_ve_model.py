"""C7/C8/C10/C17/C18/C19/C21/C28/C30: cancellation, bounded process-tree termination, model states, read-only mode,
project memory, single harness-exclusion list. Fake executables, temporary engine homes; no model, no network."""

from __future__ import annotations

import json
import re
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import test_kosu_sessions as fixtures
import qe_core as core
from kosu import FileLock, calisma_kipi, revive


def setUpModule():
    ortam = patch.dict(os.environ, {'CADRE_ZINCIR_KALITE': 'opencode-go/qwen3.8-max'})
    ortam.start()
    unittest.addModuleCleanup(ortam.stop)


LIB = fixtures.LIB
SLEEPER = 'import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True: time.sleep(.05)'


def wait_dead(pids, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(core.pid_yasiyor(p) for p in pids):
        time.sleep(.05)
    return not any(core.pid_yasiyor(p) for p in pids)


class ProcessTreeTests(unittest.TestCase):
    def test_kill_tree_escalates_to_kill_for_a_term_ignoring_tree(self):
        code = ("import signal,subprocess,sys,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"child=subprocess.Popen([sys.executable,'-c',{SLEEPER!r}])\nprint(child.pid, flush=True)\n"
                "while True: time.sleep(.05)\n")
        proc = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, text=True)
        grandchild = int(proc.stdout.readline())
        try:
            started = time.monotonic()
            self.assertFalse(core.kill_tree(proc.pid, grace=1))   # TERM ignored -> KILL was necessary
            proc.wait(timeout=5)
            self.assertLess(time.monotonic() - started, 6)
            self.assertTrue(wait_dead([grandchild]))
        finally:
            for pid in (proc.pid, grandchild):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc.wait(timeout=5)
            proc.stdout.close()

    def test_kill_tree_reports_a_clean_term_exit(self):
        proc = subprocess.Popen([sys.executable, '-c', 'import time\nwhile True: time.sleep(.05)'])
        self.assertTrue(core.kill_tree(proc.pid, grace=3))
        self.assertNotEqual(proc.wait(timeout=5), 0)


class CancelTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def flag(self, rid=None):
        (self.f.store.path(rid or self.f.rid)/'cancel.flag').write_text('requested\n')

    def test_cancel_during_a_captured_model_call_terminates_the_real_child(self):
        import threading
        self.f.fake.write_text(self.f.fake.read_text() + '\nimport time\n(p/"child.pid").write_text(str(os.getpid()))\ntime.sleep(3)\n')
        def basladiysa_iptal():
            son = time.monotonic() + 5
            while not (self.f.home / 'child.pid').exists() and time.monotonic() < son:
                time.sleep(.02)
            self.flag()
        timer = threading.Thread(target=basladiysa_iptal)
        timer.start()
        self.addCleanup(timer.join)
        started = time.monotonic()
        with patch.dict(os.environ, {'CADRE_KILL_GRACE': '.1'}), self.assertRaises(core.CancelledRun):
            core.capture_opencode(str(self.f.fake), str(self.f.wt), ['task'], 10)
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(core.pid_yasiyor(int((self.f.home/'child.pid').read_text())))

    def test_argless_cancel_flags_every_active_run_and_spares_terminal_ones(self):
        done = self.f.store.accept('b', 'finished')
        self.f.store.update(done['run_id'], state='completed')
        other = self.f.store.accept('c', 'running elsewhere')
        env = {**os.environ, 'CADRE_HOME': str(self.f.home), 'CADRE_ENGINE_ROOT': str(LIB.parent)}
        env.pop('CADRE_RUN_ID', None)
        out = subprocess.run(['bash', str(LIB.parent/'bin/cadre-iptal')], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(out.returncode, 0, out.stderr)
        for rid in (self.f.rid, other['run_id']):
            self.assertTrue((self.f.store.path(rid)/'cancel.flag').exists(), rid)
            self.assertIn(rid, out.stdout)
        self.assertFalse((self.f.store.path(done['run_id'])/'cancel.flag').exists())

    def test_cancel_flag_stops_the_running_executor_tree_within_grace(self):
        self.f.fake.write_text(self.f.fake.read_text() + f'''
import signal, subprocess, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
kid = subprocess.Popen([sys.executable, '-c', {SLEEPER!r}])
(p/'pids').write_text(f"{{os.getpid()}} {{kid.pid}}")
(p/'runs'/os.environ['CADRE_RUN_ID']/'cancel.flag').write_text('requested')
while True: time.sleep(.05)
''')
        started = time.monotonic()
        with patch.dict(os.environ, {'CADRE_KILL_GRACE': '.1'}):
            rc, gate = self.f.motor_fixture(env={'CADRE_PROGRESS_POLL': '3'})
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(rc, 1)
        gate.assert_not_called()
        self.assertEqual(self.f.store.read(self.f.rid)['tasks']['g1']['state'], 'iptal')
        pids = [int(x) for x in (self.f.home/'pids').read_text().split()]
        self.assertTrue(wait_dead(pids), 'executor tree survived cancellation')

    def test_conflicting_session_identity_ends_the_run_as_controlled_failure(self):
        # First attempt fails; the "resumed" executor reports a DIFFERENT session id than recorded.
        self.f.fake.write_text(f"#!{sys.executable}\n" + """
import json, os, pathlib, sys
p = pathlib.Path(os.environ['CADRE_HOME']); args = sys.argv[1:]
with (p/'calls.jsonl').open('a') as f: f.write(json.dumps(args)+'\\n')
sid = 'ses_other' if '--session' in args else 'ses_first'
print(json.dumps({'type':'step_start', 'sessionID':sid, 'part':{'type':'step-start'}}), flush=True)
raise SystemExit(1)
""")
        rc, gate = self.f.motor_fixture(env={'CADRE_MAX_RESUME': '3'})
        self.assertEqual(rc, 1)
        gate.assert_not_called()
        task = self.f.store.read(self.f.rid)['tasks']['g1']
        self.assertEqual(task['state'], 'yarim')
        self.assertEqual(task['reason'], 'oturum-kimligi')
        self.assertEqual(len(self.f.calls()), 2)          # no blind third launch
        self.assertIn('OTURUM KIMLIGI', (self.f.store.task_dir(self.f.rid, 'g1')/'motor.log').read_text())

    def test_revive_restarts_only_a_crashed_uncancelled_coordinator_and_is_bounded(self):
        root = self.f.home/'revive-root'
        (root/'bin').mkdir(parents=True)
        (root/'bin/cadre-dispatch.sh').write_text('exit 3\n')
        (root/'bin/is-motoru.sh').write_text('echo motor >> "$CADRE_RUN_DIR/revive-calls"\nexit 1\n')
        # P4 (15.09): one writer per project — in a project with a live coordinator no other run is revived; this test measures the revive
        # rule itself, so the three runs are in three separate projects (queue discipline is in test_gece_kuyruk).
        crashed = self.f.store.accept('crash', 'crashed')
        self.f.store.update(crashed['run_id'], state='running', phase='motor', owner_pid=999999)
        cancelled = self.f.store.accept('canc', 'cancelled')
        self.f.store.update(cancelled['run_id'], state='running', phase='motor')
        self.flag(cancelled['run_id'])
        alive = self.f.store.accept('alive', 'alive')
        self.f.store.update(alive['run_id'], state='running', phase='motor')
        with FileLock(self.f.store.path(alive['run_id'])/'.owner.lock'):   # a live coordinator holds its lock throughout
            revived = revive(self.f.store, root)
            self.assertEqual(revived, [crashed['run_id']])
            deadline = time.monotonic() + 30   # B219: under a full-suite load a revived coordinator may take more than 10 s to finish
            while time.monotonic() < deadline and self.f.store.read(crashed['run_id'])['state'] == 'running':
                time.sleep(.1)
            kilit = FileLock(self.f.store.path(crashed['run_id'])/'.owner.lock')   # B219b: 'retryable' is written, the lock is released a moment later
            while time.monotonic() < deadline and not kilit.acquire(blocking=False):
                time.sleep(.1)
            kilit.close()
            self.assertEqual((self.f.store.path(crashed['run_id'])/'revive-calls').read_text().splitlines(), ['motor'])
            self.assertEqual(self.f.store.read(crashed['run_id'])['state'], 'retryable')
            self.assertEqual(self.f.store.read(cancelled['run_id'])['state'], 'cancelled')   # B35: a flagged run without an owner is made terminal, not restarted
            self.assertFalse((self.f.store.path(cancelled['run_id'])/'revive-calls').exists())
            self.assertFalse((self.f.store.path(alive['run_id'])/'revive-calls').exists())
            self.f.store.update(crashed['run_id'], state='running', revive_count=3)
            self.assertEqual(revive(self.f.store, root), [])
            self.assertEqual(self.f.store.read(crashed['run_id'])['state'], 'failed')


class ModelStateTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_rescue_uses_router_bans_and_budget_instead_of_a_fixed_model(self):
        router = Mock()
        router.zincir.return_value = (['claude/opus#max', 'deepseek/deepseek-v4-pro', 'opencode-go/kimi-k2.7-code'], [])
        router.saglayici.side_effect = lambda m: m.split('/', 1)[0]
        router.durum_oku.return_value = {}
        router.kurtarici_nefesi.return_value = None   # B277: enough breathing room (otherwise the Mock counts every candidate as 'insufficient')
        with patch.object(core, 'YON', router), patch.object(core, 'butce_json', return_value={'ts': 'fixture'}):
            self.assertEqual(core.kurtarici_sec(yasak={'deepseek'}), 'opencode-go/kimi-k2.7-code')
            self.assertEqual(router.zincir.call_args.args[0], 'kurtarici')
            router.zincir.return_value = ([], [])
            self.assertEqual(core.kurtarici_sec(), '')
        with patch.object(core, 'YON', None):
            self.assertEqual(core.kurtarici_sec(), '')


class ReadOnlyAndMemoryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        # One session id per run (session ownership is exclusive); record the child's config.
        self.f.fake.write_text(self.f.fake.read_text().replace("else 'ses_first'", "else ('ses_' + os.environ['CADRE_RUN_ID'])")
                               + "\n(p/'child-config.json').write_text(os.environ['OPENCODE_CONFIG_CONTENT'])\n")

    def child_config(self):
        return json.loads((self.f.home/'child-config.json').read_text())

    def test_read_only_prompt_is_recorded_immutably_and_classifier_cannot_upgrade_it(self):
        self.assertEqual(calisma_kipi('Bu modulu incele ve sadece rapor yaz, kod yazma'), 'inceleme')
        self.assertEqual(calisma_kipi('Login hatasini duzelt'), 'uygulama')
        run = self.f.store.accept('a', 'Repoyu incele, sadece rapor yaz; degisiklik yapma')
        self.assertEqual(run['mode'], 'inceleme')
        with self.assertRaises(ValueError):
            self.f.store.update(run['run_id'], mode='uygulama')
        root = self.f.home/'onelek-fixture'
        root.mkdir()
        for name in ('onelek.py', 'kosu.py', 'runtime_sync.py', 'seviye.py'):   # B222: the code-name definition comes from seviye
            shutil.copyfile(LIB/name, root/name)
        (root/'plan_ds.py').write_text("print('PREMIUM')\n")   # the LLM insists on PREMIUM
        prompt = self.f.home/'readonly.md'
        prompt.write_text('Mimariyi incele ve sadece rapor yaz; degisiklik yapma')
        budget = json.dumps({'go': {'oran': 1}, 'ds': {'oran': 1}, 'claude': {'pay': 50}})
        env = {**os.environ, 'CADRE_HOME': str(self.f.home), 'CADRE_ONELEK_LLM': '1'}
        env.pop('CADRE_RUN_ID', None)
        out = subprocess.run([sys.executable, str(root/'onelek.py'), str(prompt)], input=budget, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(out.stdout.split(), ['ATLA', '-'], out.stderr)
        self.assertTrue((self.f.home/'zincir/sinif-gecmisi.jsonl').exists())   # trace stays in CADRE_HOME, not the live home

    def test_read_only_run_gets_technical_write_denial_in_every_executor(self):
        run = self.f.store.accept('a', 'Repoyu incele, sadece rapor yaz; degisiklik yapma')
        with patch.dict(os.environ, {**self.f.store.environment(run['run_id']), 'CADRE_GOREV_ID': 'g1'}):
            core.run_opencode(str(self.f.fake), str(self.f.wt), ['task'], self.f.log).wait(timeout=5)
            config = self.child_config()
            self.assertEqual(config['permission']['edit'], 'deny')
            self.assertEqual(config['permission']['bash'], 'allow')
            self.assertEqual(config['permission']['read'], 'deny')
            self.assertEqual(config['agent']['build']['permission']['edit'], 'deny')
            paths = list(self.f.store.task_dir(run['run_id'], 'g1').glob('attempts/*/tool-policy.json'))
            self.assertEqual(len(paths), 1)
            policy = json.loads(paths[0].read_text())
            self.assertFalse(policy['writable'])
            denied = subprocess.run([policy['sandbox'], '-p', policy['profile'], sys.executable, '-c',
                "from pathlib import Path; Path('forbidden.txt').write_text('unauthorized')"],
                cwd=self.f.wt, capture_output=True, timeout=5)
            self.assertNotEqual(denied.returncode, 0)
            self.assertFalse((self.f.wt / 'forbidden.txt').exists())
            with patch.dict(os.environ, {'CLAUDE_BIN': str(self.f.claude_fixture())}):
                core.run_claude(str(self.f.wt), 'claude/sonnet#high', 'inceleme', self.f.log).wait(timeout=5)
            args = json.loads((self.f.home/'claude-calls.jsonl').read_text().splitlines()[-1])
            self.assertNotIn('Edit', args[args.index('--allowedTools') + 1].split(','))
            self.assertIn('Edit', args[args.index('--disallowedTools') + 1].split(','))
        core.run_opencode(str(self.f.fake), str(self.f.wt), ['task'], self.f.log).wait(timeout=5)   # ordinary run
        self.assertEqual(self.child_config()['permission']['edit'], 'deny')
        self.assertEqual(self.child_config()['permission']['bash'], 'allow')
        policies = list(self.f.store.task_dir(self.f.rid, 'g1').glob('attempts/*/tool-policy.json'))
        self.assertEqual(len(policies), 1)
        self.assertTrue(json.loads(policies[0].read_text())['writable'])

    def test_read_only_shell_permission_leaves_no_write_or_exec_door(self):
        def eslesir(metin, kalip):
            duzen = re.sub(r'[.+^${}()|\[\]\\]', lambda m: '\\' + m.group(0), kalip).replace('*', '.*').replace('?', '.')
            if duzen.endswith(' .*'):
                duzen = duzen[:-3] + '( .*)?'
            return re.fullmatch(duzen, metin, re.S) is not None

        def karar(komut):
            kurallar = list(core.READ_ONLY_PERMISSION['bash'].items())
            return next((eylem for kalip, eylem in reversed(kurallar) if eslesir(komut, kalip)), 'ask')
        for komut in ('git diff HEAD~1 -- a.py', 'git log --oneline -5', 'git show HEAD', 'git status', 'ls -la', 'cat a.py',
                      'head -20 a.py', 'tail -5 a.py', 'grep -n foo a.py', 'wc -l a.py', 'pwd', 'echo bitti'):
            self.assertEqual(karar(komut), 'allow', komut)
        for komut in ('find . -name x -delete', 'find . -exec rm {} ;', "rg --pre 'sh -c x' foo", "sed -n 'w out' a.py", 'tree -o out',
                      'cat a.py > b.py', 'echo x >> ~/.zshrc', 'grep foo a.py 2> hata', 'git diff --output=/tmp/x', 'git log --output /tmp/x',
                      'rm -rf ~', "python3 -c 'print(1)'", 'lsof -i'):
            self.assertEqual(karar(komut), 'deny', komut)

    def test_read_only_claude_reviewer_gets_write_and_exec_tools_denied(self):
        run = self.f.store.accept('a', 'Ozellik ekle')
        with patch.dict(os.environ, {**self.f.store.environment(run['run_id']), 'CADRE_GOREV_ID': 'kalite',
                                     'CLAUDE_BIN': str(self.f.claude_fixture())}):
            core.run_claude(str(self.f.wt), 'claude/sonnet#high', 'incele', self.f.log, tools=core.CLAUDE_READ_ONLY_TOOLS,
                            role='kalite').wait(timeout=5)
        args = json.loads((self.f.home/'claude-calls.jsonl').read_text().splitlines()[-1])
        izinli = args[args.index('--allowedTools') + 1].split(',')
        yasak = args[args.index('--disallowedTools') + 1].split(',')
        for arac in ('Bash(find:*)', 'Bash(rg:*)', 'Bash(sed -n:*)', 'Edit', 'Write'):
            self.assertNotIn(arac, izinli, arac)
        for arac in ('Edit', 'Write', 'Bash(find:*)', 'Bash(rg:*)', 'Bash(sed:*)', 'Bash(rm:*)'):
            self.assertIn(arac, yasak, arac)

    def test_secret_paths_are_denied_in_every_claude_run_not_only_read_only_ones(self):
        """B110-2: secret files are closed to read tools; this also applies in a normal (writing) run."""
        run = self.f.store.accept('a', 'Ozellik ekle')
        with patch.dict(os.environ, {**self.f.store.environment(run['run_id']), 'CADRE_GOREV_ID': 'g1',
                                     'CLAUDE_BIN': str(self.f.claude_fixture())}):
            core.run_claude(str(self.f.wt), 'claude/sonnet#high', 'impl', self.f.log).wait(timeout=5)
        args = json.loads((self.f.home/'claude-calls.jsonl').read_text().splitlines()[-1])
        yasak = args[args.index('--disallowedTools') + 1].split(',')
        self.assertIn('Read(./.env)', yasak)
        self.assertIn('Read(./**/*.pem)', yasak)
        self.assertNotIn('Edit', yasak)          # in a normal run writing stays open

    def test_writing_claude_lane_may_run_read_only_shell_commands_without_approval(self):
        """B246 (26.09 live EK1 writer): in the compound command `git show ... | head`, `head` asked for approval, there is no approval in -p mode -> the round
        was wasted ($0.1). In a writing lane too, read-only shell commands (ls/cat/head/tail/sed -n/grep/rg/find/wc/diff) and pytest
        are allowed from the start; destructive commands (rm/mv/cp) are not on the allow-list."""
        run = self.f.store.accept('a', 'Ozellik ekle')
        with patch.dict(os.environ, {**self.f.store.environment(run['run_id']), 'CADRE_GOREV_ID': 'g1',
                                     'CLAUDE_BIN': str(self.f.claude_fixture())}):
            core.run_claude(str(self.f.wt), 'claude/sonnet#high', 'impl', self.f.log).wait(timeout=5)
        args = json.loads((self.f.home/'claude-calls.jsonl').read_text().splitlines()[-1])
        izinli = args[args.index('--allowedTools') + 1].split(',')
        for arac in ('Bash(head:*)', 'Bash(cat:*)', 'Bash(ls:*)', 'Bash(sed -n:*)', 'Bash(grep:*)', 'Bash(rg:*)', 'Bash(find:*)',
                     'Bash(tail:*)', 'Bash(wc:*)', 'Bash(diff:*)', 'Bash(pytest:*)', 'Edit', 'Bash(git:*)'):
            self.assertIn(arac, izinli, arac)
        for arac in ('Bash(rm:*)', 'Bash(mv:*)', 'Bash(cp:*)', 'Bash', 'Bash(*)'):
            self.assertNotIn(arac, izinli, arac)

    def test_project_memory_reaches_every_executor_and_missing_memory_refuses(self):
        project = self.f.home/'projects/a'
        project.mkdir(parents=True)
        (project/'POLICY.md').write_text('never push to main')
        (project/'PROJECT.md').write_text('project facts')
        run = self.f.store.accept('a', 'do work')
        with patch.dict(os.environ, {**self.f.store.environment(run['run_id']), 'CADRE_GOREV_ID': 'g1'}):
            core.run_opencode(str(self.f.fake), str(self.f.wt), ['task'], self.f.log).wait(timeout=5)
            paths = self.child_config()['instructions']
            self.assertEqual(sorted(Path(p).name for p in paths), ['AGENTS.md', 'POLICY.md', 'PROJECT.md', 'SKILL.md'])
            loaded = {Path(p).name: Path(p).read_text() for p in paths}
            self.assertEqual(loaded['POLICY.md'], 'never push to main')
            self.assertEqual(loaded['PROJECT.md'], 'project facts')
            source = Path(__file__).resolve().parents[2] / 'config'
            self.assertEqual(loaded['AGENTS.md'], (source / 'AGENTS.md').read_text())
            self.assertEqual(loaded['SKILL.md'], (source / 'skills/self-check/SKILL.md').read_text())
            self.assertTrue(all(Path(p).is_relative_to(self.f.store.path(run['run_id'])) for p in paths))
            with patch.dict(os.environ, {'CLAUDE_BIN': str(self.f.claude_fixture())}):
                core.run_claude(str(self.f.wt), 'claude/sonnet#high', 'work', self.f.log).wait(timeout=5)
            args = json.loads((self.f.home/'claude-calls.jsonl').read_text().splitlines()[-1])
            self.assertIn('never push to main', args[args.index('--append-system-prompt') + 1])
            Path(run['constraints']['POLICY.md']).unlink()
            with self.assertRaises(ValueError):
                core.run_opencode(str(self.f.fake), str(self.f.wt), ['task'], self.f.log)
            self.assertEqual(len(self.f.calls()), 1)


class TextRouterCommitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-text-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def test_task_text_compaction_keeps_repeated_and_middle_lines(self):
        text = ('GOREV\n' + '\n'.join(f'madde {i}' for i in range(3000)) +
                '\nsu dosyaya dokunma: config.py\nsu dosyaya dokunma: config.py\n' + '\n'.join(f'son {i}' for i in range(3000)))
        kept = core.sikistir(text, 100, gorev=True)
        self.assertEqual(kept.count('su dosyaya dokunma: config.py'), 2)
        self.assertIn('madde 1500', kept)
        self.assertNotIn('sikistirildi', kept)
        context = core.sikistir(text, 100)
        self.assertIn('sikistirildi', context)   # context mode still trims
        out = subprocess.run([sys.executable, str(LIB/'qe_core.py'), 'sikistir', '--gorev', '100'], input=text, capture_output=True, text=True, timeout=20,
                             env={**os.environ, 'CADRE_HOME': str(self.home)})
        self.assertEqual(out.stdout.count('su dosyaya dokunma: config.py'), 2)

    def test_router_success_resets_the_consecutive_failure_counter(self):
        import yonlendirici
        with patch.object(yonlendirici, 'DURUM', self.home/'durum.json'), patch.object(yonlendirici, 'KAYIT', self.home/'yon.jsonl'):
            for _ in range(2):
                yonlendirici.ariza('deepseek', 'boom')
            yonlendirici.basari('deepseek')
            for _ in range(2):
                yonlendirici.ariza('deepseek', 'boom')
            self.assertFalse(yonlendirici.cooldownda('deepseek'))
            yonlendirici.ariza('deepseek', 'boom')
            self.assertTrue(yonlendirici.cooldownda('deepseek'))

    def test_harness_files_never_enter_a_commit(self):
        def git(*args, cwd):
            return subprocess.run(['git', '-c', 'user.name=f', '-c', 'user.email=f@x', *args], cwd=cwd, check=True,
                                  capture_output=True, text=True, timeout=10).stdout
        repo = self.home/'repo'
        repo.mkdir()
        git('init', '-q', '-b', 'main', cwd=repo)
        (repo/'README.md').write_text('base')
        git('add', '.', cwd=repo)
        git('commit', '-qm', 'base', cwd=repo)
        wt = self.home/'worktrees/a/x'
        wt.parent.mkdir(parents=True)
        git('worktree', 'add', '-q', '-b', 'cadre/x', str(wt), cwd=repo)
        (wt/'product.txt').write_text('deliverable')
        (wt/'.cadre-plan').mkdir()
        (wt/'.cadre-plan/plan.md').write_text('harness plan')
        (wt/'.qa-sonuc.txt').write_text('harness qa')
        (wt/'.gorevler.json').write_text('[]')
        (wt/'STATE').write_text('memory')
        (wt/'var/audit').mkdir(parents=True)
        (wt/'var/audit/OTOPSI-1.md').write_text('post mortem')
        import izin
        from kosu import RunStore
        store = RunStore(self.home)
        rid = store.accept('a', 'fixture checkpoint')['run_id']
        store.update(rid, worktree=str(wt))
        action = izin.commit_proposal(store, rid, wt, 'test')
        first = izin.approve(store, rid, action['token'])
        izin.approve(store, rid, action['token'], first['confirmation'])
        out = subprocess.run(['bash', '-c', '. "$1"; qe_commit_wip "$2" a test', 'x', str(LIB/'common.sh'), str(wt)],
                             env={**os.environ, 'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(LIB.parent),
                                  'CADRE_RUN_ID': rid, 'CADRE_COMMIT_ACTION': action['token'],
                                  'GIT_AUTHOR_NAME': 'f', 'GIT_AUTHOR_EMAIL': 'f@x', 'GIT_COMMITTER_NAME': 'f', 'GIT_COMMITTER_EMAIL': 'f@x'},
                             capture_output=True, text=True, timeout=20)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), '1')
        committed = git('show', '--name-only', '--format=', 'HEAD', cwd=wt).split()
        self.assertEqual(committed, ['product.txt'])
        self.assertTrue((wt/'.cadre-plan/plan.md').exists())

    def test_motor_caffeinate_prevents_lid_sleep(self):
        """An independent engine (the old path/test without a coordinator) holds the wake lock itself."""
        f = fixtures.SessionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.motor_fixture()
        caffeinate = [cmd for cmd in f.popen_calls if cmd and cmd[0] == 'caffeinate']
        self.assertEqual(len(caffeinate), 1)
        self.assertIn('-s', caffeinate[0])
        self.assertIn('-i', caffeinate[0])

    def test_motor_under_a_coordinator_leaves_caffeinate_to_the_coordinator(self):
        """P2: the coordinator (CADRE_KOORDINATOR_PID) holds a single caffeinate for the whole run lifetime; the engine does not open a second one."""
        f = fixtures.SessionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.motor_fixture(env={'CADRE_KOORDINATOR_PID': '4242'})
        self.assertEqual([cmd for cmd in f.popen_calls if cmd and cmd[0] == 'caffeinate'], [])


if __name__ == '__main__':
    unittest.main()
