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

import test_run_sessions as fixtures
import qe_core as core
from run_store import FileLock, operation_mode, revive


def setUpModule():
    environment = patch.dict(os.environ, {'CADRE_CHAIN_QUALITY': 'opencode-go/qwen3.8-max'})
    environment.start()
    unittest.addModuleCleanup(environment.stop)


LIB = fixtures.LIB
SLEEPER = 'import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True: time.sleep(.05)'


def wait_dead(pids, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(core.pid_alive(p) for p in pids):
        time.sleep(.05)
    return not any(core.pid_alive(p) for p in pids)


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
        def started_cancel():
            last = time.monotonic() + 5
            while not (self.f.home / 'child.pid').exists() and time.monotonic() < last:
                time.sleep(.02)
            self.flag()
        timer = threading.Thread(target=started_cancel)
        timer.start()
        self.addCleanup(timer.join)
        started = time.monotonic()
        with patch.dict(os.environ, {'CADRE_KILL_GRACE': '.1'}), self.assertRaises(core.CancelledRun):
            core.capture_opencode(str(self.f.fake), str(self.f.wt), ['task'], 10)
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(core.pid_alive(int((self.f.home/'child.pid').read_text())))

    def test_argless_cancel_flags_every_active_run_and_spares_terminal_ones(self):
        done = self.f.store.accept('b', 'finished')
        self.f.store.update(done['run_id'], state='completed')
        other = self.f.store.accept('c', 'running elsewhere')
        env = {**os.environ, 'CADRE_HOME': str(self.f.home), 'CADRE_ENGINE_ROOT': str(LIB.parent)}
        env.pop('CADRE_RUN_ID', None)
        out = subprocess.run(['bash', str(LIB.parent/'bin/cadre-cancel')], env=env, capture_output=True, text=True, timeout=10)
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
            rc, gate = self.f.runner_fixture(env={'CADRE_PROGRESS_POLL': '3'})
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(rc, 1)
        gate.assert_not_called()
        self.assertEqual(self.f.store.read(self.f.rid)['tasks']['g1']['state'], 'cancel')
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
        rc, gate = self.f.runner_fixture(env={'CADRE_MAX_RESUME': '3'})
        self.assertEqual(rc, 1)
        gate.assert_not_called()
        task = self.f.store.read(self.f.rid)['tasks']['g1']
        self.assertEqual(task['state'], 'half')
        self.assertEqual(task['reason'], 'session-id')
        self.assertEqual(len(self.f.calls()), 2)          # no blind third launch
        self.assertIn('SESSION IDENTITY', (self.f.store.task_dir(self.f.rid, 'g1')/'runner.log').read_text())

    def test_revive_restarts_only_a_crashed_uncancelled_coordinator_and_is_bounded(self):
        root = self.f.home/'revive-root'
        (root/'bin').mkdir(parents=True)
        (root/'bin/cadre-dispatch.sh').write_text('exit 3\n')
        (root/'bin/job-runner.sh').write_text('echo runner >> "$CADRE_RUN_DIR/revive-calls"\nexit 1\n')
        # P4 (15.09): one writer per project — in a project with a live coordinator no other run is revived; this test measures the revive
        # rule itself, so the three runs are in three separate projects (queue discipline is in test_night_queue).
        crashed = self.f.store.accept('crash', 'crashed')
        self.f.store.update(crashed['run_id'], state='running', phase='runner', owner_pid=999999)
        cancelled = self.f.store.accept('canc', 'cancelled')
        self.f.store.update(cancelled['run_id'], state='running', phase='runner')
        self.flag(cancelled['run_id'])
        alive = self.f.store.accept('alive', 'alive')
        self.f.store.update(alive['run_id'], state='running', phase='runner')
        with FileLock(self.f.store.path(alive['run_id'])/'.owner.lock'):   # a live coordinator holds its lock throughout
            revived = revive(self.f.store, root)
            self.assertEqual(revived, [crashed['run_id']])
            deadline = time.monotonic() + 30   # B219: under a full-suite load a revived coordinator may take more than 10 s to finish
            while time.monotonic() < deadline and self.f.store.read(crashed['run_id'])['state'] == 'running':
                time.sleep(.1)
            lock = FileLock(self.f.store.path(crashed['run_id'])/'.owner.lock')   # B219b: 'retryable' is written, the lock is released a moment later
            while time.monotonic() < deadline and not lock.acquire(blocking=False):
                time.sleep(.1)
            lock.close()
            self.assertEqual((self.f.store.path(crashed['run_id'])/'revive-calls').read_text().splitlines(), ['runner'])
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
        router.chain.return_value = (['claude/opus#max', 'deepseek/deepseek-v4-pro', 'opencode-go/kimi-k2.7-code'], [])
        router.provider.side_effect = lambda m: m.split('/', 1)[0]
        router.status_read.return_value = {}
        router.recovery_breath.return_value = None   # B277: enough breathing room (otherwise the Mock counts every candidate as 'insufficient')
        with patch.object(core, 'ROUTER', router), patch.object(core, 'budget_json', return_value={'ts': 'fixture'}):
            self.assertEqual(core.recovery_select(forbidden={'deepseek'}), 'opencode-go/kimi-k2.7-code')
            self.assertEqual(router.chain.call_args.args[0], 'recovery')
            router.chain.return_value = ([], [])
            self.assertEqual(core.recovery_select(), '')
        with patch.object(core, 'ROUTER', None):
            self.assertEqual(core.recovery_select(), '')


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
        self.assertEqual(operation_mode('Review this module and write only a report; do not write code'), 'review')
        self.assertEqual(operation_mode('Fix the login error'), 'application')
        run = self.f.store.accept('a', 'Review the repo, write only a report and make no changes')
        self.assertEqual(run['mode'], 'review')
        with self.assertRaises(ValueError):
            self.f.store.update(run['run_id'], mode='application')
        root = self.f.home/'prefilter-fixture'
        root.mkdir()
        for name in ('prefilter.py', 'run_store.py', 'runtime_sync.py', 'level.py'):   # B222: the code-name definition comes from level
            shutil.copyfile(LIB/name, root/name)
        (root/'plan_ds.py').write_text("print('PREMIUM')\n")   # the LLM insists on PREMIUM
        prompt = self.f.home/'readonly.md'
        prompt.write_text('Review the architecture and write only a report; make no changes')
        budget = json.dumps({'go': {'ratio': 1}, 'ds': {'ratio': 1}, 'claude': {'pay': 50}})
        env = {**os.environ, 'CADRE_HOME': str(self.f.home), 'CADRE_PREFILTER_LLM': '1'}
        env.pop('CADRE_RUN_ID', None)
        out = subprocess.run([sys.executable, str(root/'prefilter.py'), str(prompt)], input=budget, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(out.stdout.split(), ['SKIP', '-'], out.stderr)
        self.assertTrue((self.f.home/'chain/category-history.jsonl').exists())   # trace stays in CADRE_HOME, not the live home

    def test_read_only_run_gets_technical_write_denial_in_every_executor(self):
        run = self.f.store.accept('a', 'Review the repo, write only a report and make no changes')
        with patch.dict(os.environ, {**self.f.store.environment(run['run_id']), 'CADRE_TASK_ID': 'g1'}):
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
                core.run_claude(str(self.f.wt), 'claude/sonnet#high', 'review', self.f.log).wait(timeout=5)
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
        def matches(text, pattern):
            layout = re.sub(r'[.+^${}()|\[\]\\]', lambda m: '\\' + m.group(0), pattern).replace('*', '.*').replace('?', '.')
            if layout.endswith(' .*'):
                layout = layout[:-3] + '( .*)?'
            return re.fullmatch(layout, text, re.S) is not None

        def decision(command):
            rules = list(core.READ_ONLY_PERMISSION['bash'].items())
            return next((action for pattern, action in reversed(rules) if matches(command, pattern)), 'ask')
        for command in ('git diff HEAD~1 -- a.py', 'git log --oneline -5', 'git show HEAD', 'git status', 'ls -la', 'cat a.py',
                      'head -20 a.py', 'tail -5 a.py', 'grep -n foo a.py', 'wc -l a.py', 'pwd', 'echo finished'):
            self.assertEqual(decision(command), 'allow', command)
        for command in ('find . -name x -delete', 'find . -exec rm {} ;', "rg --pre 'sh -c x' foo", "sed -n 'w out' a.py", 'tree -o out',
                      'cat a.py > b.py', 'echo x >> ~/.zshrc', 'grep foo a.py 2> error', 'git diff --output=/tmp/x', 'git log --output /tmp/x',
                      'rm -rf ~', "python3 -c 'print(1)'", 'lsof -i'):
            self.assertEqual(decision(command), 'deny', command)

    def test_read_only_claude_reviewer_gets_write_and_exec_tools_denied(self):
        run = self.f.store.accept('a', 'Add a feature')
        with patch.dict(os.environ, {**self.f.store.environment(run['run_id']), 'CADRE_TASK_ID': 'quality',
                                     'CLAUDE_BIN': str(self.f.claude_fixture())}):
            core.run_claude(str(self.f.wt), 'claude/sonnet#high', 'review', self.f.log, tools=core.CLAUDE_READ_ONLY_TOOLS,
                            role='quality').wait(timeout=5)
        args = json.loads((self.f.home/'claude-calls.jsonl').read_text().splitlines()[-1])
        allowed = args[args.index('--allowedTools') + 1].split(',')
        forbidden = args[args.index('--disallowedTools') + 1].split(',')
        for tool in ('Bash(find:*)', 'Bash(rg:*)', 'Bash(sed -n:*)', 'Edit', 'Write'):
            self.assertNotIn(tool, allowed, tool)
        for tool in ('Edit', 'Write', 'Bash(find:*)', 'Bash(rg:*)', 'Bash(sed:*)', 'Bash(rm:*)'):
            self.assertIn(tool, forbidden, tool)

    def test_secret_paths_are_denied_in_every_claude_run_not_only_read_only_ones(self):
        """B110-2: secret files are closed to read tools; this also applies in a normal (writing) run."""
        run = self.f.store.accept('a', 'Add a feature')
        with patch.dict(os.environ, {**self.f.store.environment(run['run_id']), 'CADRE_TASK_ID': 'g1',
                                     'CLAUDE_BIN': str(self.f.claude_fixture())}):
            core.run_claude(str(self.f.wt), 'claude/sonnet#high', 'impl', self.f.log).wait(timeout=5)
        args = json.loads((self.f.home/'claude-calls.jsonl').read_text().splitlines()[-1])
        forbidden = args[args.index('--disallowedTools') + 1].split(',')
        self.assertIn('Read(./.env)', forbidden)
        self.assertIn('Read(./**/*.pem)', forbidden)
        self.assertNotIn('Edit', forbidden)          # in a normal run writing stays open

    def test_writing_claude_lane_may_run_read_only_shell_commands_without_approval(self):
        """B246 (26.09 live EXTRA1 writer): in the compound command `git show ... | head`, `head` asked for approval, there is no approval in -p mode -> the round
        was wasted ($0.1). In a writing lane too, read-only shell commands (ls/cat/head/tail/sed -n/grep/rg/find/wc/diff) and pytest
        are allowed from the start; destructive commands (rm/mv/cp) are not on the allow-list."""
        run = self.f.store.accept('a', 'Add a feature')
        with patch.dict(os.environ, {**self.f.store.environment(run['run_id']), 'CADRE_TASK_ID': 'g1',
                                     'CLAUDE_BIN': str(self.f.claude_fixture())}):
            core.run_claude(str(self.f.wt), 'claude/sonnet#high', 'impl', self.f.log).wait(timeout=5)
        args = json.loads((self.f.home/'claude-calls.jsonl').read_text().splitlines()[-1])
        allowed = args[args.index('--allowedTools') + 1].split(',')
        for tool in ('Bash(head:*)', 'Bash(cat:*)', 'Bash(ls:*)', 'Bash(sed -n:*)', 'Bash(grep:*)', 'Bash(rg:*)', 'Bash(find:*)',
                     'Bash(tail:*)', 'Bash(wc:*)', 'Bash(diff:*)', 'Bash(pytest:*)', 'Edit', 'Bash(git:*)'):
            self.assertIn(tool, allowed, tool)
        for tool in ('Bash(rm:*)', 'Bash(mv:*)', 'Bash(cp:*)', 'Bash', 'Bash(*)'):
            self.assertNotIn(tool, allowed, tool)

    def test_project_memory_reaches_every_executor_and_missing_memory_refuses(self):
        project = self.f.home/'projects/a'
        project.mkdir(parents=True)
        (project/'POLICY.md').write_text('never push to main')
        (project/'PROJECT.md').write_text('project facts')
        run = self.f.store.accept('a', 'do work')
        with patch.dict(os.environ, {**self.f.store.environment(run['run_id']), 'CADRE_TASK_ID': 'g1'}):
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
        text = ('TASK\n' + '\n'.join(f'item {i}' for i in range(3000)) +
                '\nDo not touch config.py\nDo not touch config.py\n' + '\n'.join(f'last {i}' for i in range(3000)))
        kept = core.compress(text, 100, task=True)
        self.assertEqual(kept.count('Do not touch config.py'), 2)
        self.assertIn('item 1500', kept)
        self.assertNotIn('compressed', kept)
        context = core.compress(text, 100)
        self.assertIn('compressed', context)   # context mode still trims
        out = subprocess.run([sys.executable, str(LIB/'qe_core.py'), 'compress', '--task', '100'], input=text, capture_output=True, text=True, timeout=20,
                             env={**os.environ, 'CADRE_HOME': str(self.home)})
        self.assertEqual(out.stdout.count('Do not touch config.py'), 2)

    def test_router_success_resets_the_consecutive_failure_counter(self):
        import model_router
        with patch.object(model_router, 'STATUS', self.home/'status.json'), patch.object(model_router, 'RECORD', self.home/'router.jsonl'):
            for _ in range(2):
                model_router.failure('deepseek', 'boom')
            model_router.success('deepseek')
            for _ in range(2):
                model_router.failure('deepseek', 'boom')
            self.assertFalse(model_router.in_cooldown('deepseek'))
            model_router.failure('deepseek', 'boom')
            self.assertTrue(model_router.in_cooldown('deepseek'))

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
        (wt/'.qa-outcome.txt').write_text('harness qa')
        (wt/'.tasks.json').write_text('[]')
        (wt/'STATE').write_text('memory')
        (wt/'present/audit').mkdir(parents=True)
        (wt/'present/audit/AUTOPSY-1.md').write_text('post mortem')
        import permission
        from run_store import RunStore
        store = RunStore(self.home)
        rid = store.accept('a', 'fixture checkpoint')['run_id']
        store.update(rid, worktree=str(wt))
        action = permission.commit_proposal(store, rid, wt, 'test')
        first = permission.approve(store, rid, action['token'])
        permission.approve(store, rid, action['token'], first['confirmation'])
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

    def test_runner_caffeinate_prevents_lid_sleep(self):
        """An independent engine (the old path/test without a coordinator) holds the wake lock itself."""
        f = fixtures.SessionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.runner_fixture()
        caffeinate = [cmd for cmd in f.popen_calls if cmd and cmd[0] == 'caffeinate']
        self.assertEqual(len(caffeinate), 1)
        self.assertIn('-s', caffeinate[0])
        self.assertIn('-i', caffeinate[0])

    def test_runner_under_a_coordinator_leaves_caffeinate_to_the_coordinator(self):
        """P2: the coordinator (CADRE_COORDINATOR_PID) holds a single caffeinate for the whole run lifetime; the engine does not open a second one."""
        f = fixtures.SessionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.runner_fixture(env={'CADRE_COORDINATOR_PID': '4242'})
        self.assertEqual([cmd for cmd in f.popen_calls if cmd and cmd[0] == 'caffeinate'], [])


if __name__ == '__main__':
    unittest.main()
