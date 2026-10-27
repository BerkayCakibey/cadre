"""DESIGN-5 speed-up (user approval 24.09 17:xx), decision (1): hand-off to the cloud BUT budget-protected — when a long local attempt
or QA repair exceeds the threshold, a cloud model that passes the budget gate continues the same job (B218). Without a budgeted cloud it stays local.
Real engine / real QA gate, fake executor; no model/network."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import qe_core as core  # noqa: E402
import test_run_sessions as fixtures  # noqa: E402
from test_night_scenarios import budget, chain_fake  # noqa: E402


def setUpModule():
    environment = patch.dict(os.environ, {'CADRE_CHAIN_QUALITY': 'opencode-go/qwen3.8-max'})
    environment.start()
    unittest.addModuleCleanup(environment.stop)


FAKE_EXECUTOR = """
import json, os, pathlib, sys, time
args = sys.argv[1:]
sid = args[args.index('--session') + 1] if '--session' in args else 'ses_t'
print(json.dumps({'type': 'step_start', 'sessionID': sid, 'part': {'type': 'step-start'}}), flush=True)
with (pathlib.Path(os.environ['CADRE_HOME']) / 'calls').open('a') as f:
    f.write(' '.join(args[:-2]) + '\\n')
print(json.dumps({'type': 'text', 'sessionID': sid, 'part': {'type': 'text', 'text': 'finished'}}), flush=True)
"""


class QaRepairHandoverTest(unittest.TestCase):
    """LIVE1/P3: the fixed QA hand-off time was removed; the repair is done directly with the model."""
    def setUp(self):
        import test_outcome
        self.f = test_outcome.CompletionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        from qa_fixture import command
        Path(self.f.run['constraints']['qa-command']).write_text(command('test -f fixed.txt'))

    def repair(self, cloud):
        calls = []

        def execute(real, wt, args, logf, env_extra=None, role='impl'):
            calls.append(list(args))
            if '-m' in args:
                return subprocess.Popen([sys.executable, '-c', f"open({str(Path(wt) / 'fixed.txt')!r}, 'w').write('x')"])
            return subprocess.Popen(['sleep', '5'])
        with patch.object(core, 'run_opencode', side_effect=execute), patch.object(core, 'cloud_candidate', return_value=cloud):
            outcome = core.qa_gate('a', str(self.f.wt), self.f.log, '/fake/cli')
        return outcome, calls

    def test_cr51_green_local_qa_and_its_rerun_make_no_model_call(self):
        (Path(self.f.wt) / 'fixed.txt').write_text('x')
        calls = []
        with patch.object(core, 'run_opencode', side_effect=lambda *a, **k: calls.append(a)), \
             patch.object(core, 'cloud_candidate', return_value='deepseek/deepseek-v4-pro'):
            first = core.qa_gate('a', str(self.f.wt), self.f.log, '/fake/cli')
            repeat = core.qa_gate('a', str(self.f.wt), self.f.log, '/fake/cli')
        self.assertEqual((first['status'], repeat['status']), ('PASS', 'PASS'))
        self.assertEqual(calls, [], 'Tests, lint and counters use local execution without hidden paid calls')

    def test_repair_starts_directly_in_the_cloud(self):
        """LIVE1: QA repair goes directly to the model, no hand-off."""
        outcome, calls = self.repair('deepseek/deepseek-v4-pro')
        self.assertEqual(outcome['status'], 'PASS')
        self.assertEqual(calls[0][calls[0].index('-m') + 1], 'deepseek/deepseek-v4-pro')
        self.assertIn('QA REPAIR: starting', Path(self.f.log).read_text())


class DurationEndTest(unittest.TestCase):
    """CR-48 (N-TS01): a helper call and a lane carry the remaining wall time to the provider gate; time measurement is not a paid call."""
    def test_confirmed_plan_stops_freeze_lane_time_without_resetting_its_initial_limit(self):
        import tempfile
        from runner import duration_record
        with tempfile.TemporaryDirectory(prefix='duration-stance-') as home_dir:
            store = core.RunStore(Path(home_dir))
            rid = store.accept('a', 'bounded work')['run_id']
            def stopped(start, end, runs=None):
                return {'status': 'resumed', 'stop': {'ts': start, 'runs': [rid] if runs is None else runs},
                        'events': [{'action': 'resumed', 'ts': end}]}
            cases = [('normal', [], 0), ('verified', [stopped(1200, 1700)], 500),
                     ('overlap-once', [stopped(1200, 1700), stopped(1400, 1800)], 600),
                     ('before-lane', [stopped(500, 900)], 0),
                     ('other-run', [stopped(1200, 1700, ['another'])], 0),
                     ('requested-only', [{'status': 'stop_requested', 'created': 1200}], 0)]
            for name, changes, pause in cases:
                with self.subTest(case=name), patch.object(core.time, 'time', return_value=2000), \
                        patch.object(core.time, 'monotonic', return_value=10):
                    core.atomic_json(store.path(rid) / 'run.json', {**store.read(rid), 'plan_changes': changes})
                    store.task_update(rid, 'g1', runner_clock={'started': 1000, 'warning_seconds': 600, 'limit_seconds': 900})
                    warning, deadline, clock = duration_record(store, rid, 'g1', 5000, 5000)
                    self.assertEqual((warning, deadline), (10 + max(0, pause - 400), 10 + max(0, pause - 100)))
                    self.assertEqual((clock['started'], clock['limit_seconds'], clock['warning_seconds']), (1000, 900, 600))
            with patch.object(core.time, 'time', return_value=2000), patch.object(core.time, 'monotonic', return_value=10):
                for invalid in (stopped(1200, 2200), stopped(1200, 1700, rid), {'status': 'resumed', 'stop': 'broken'}):
                    core.atomic_json(store.path(rid) / 'run.json', {**store.read(rid), 'plan_changes': [invalid]})
                    with self.assertRaisesRegex(ValueError, 'runner-clock-pause-invalid'):
                        duration_record(store, rid, 'g1', 600, 900)

    def test_live_request_work_is_a_conservative_bound_when_legacy_lane_has_no_activity_offset(self):
        import tempfile
        from runner import duration_record
        with tempfile.TemporaryDirectory(prefix='duration-active-') as home_dir:
            store = core.RunStore(Path(home_dir))
            rid = store.accept('a', 'bounded work')['run_id']
            store.task_update(rid, 'g1', runner_clock={'started': 1000, 'warning_seconds': 600, 'limit_seconds': 900})
            core.atomic_json(store.path(rid) / 'run.json', {**store.read(rid), 'request': {'duration_minutes': 40},
                'request_clock': {'active_sec': 500, 'session': 1990}})
            with patch.object(core.time, 'time', return_value=2000), patch.object(core.time, 'monotonic', return_value=10):
                warning, deadline, clock = duration_record(store, rid, 'g1', 5000, 5000)
                self.assertEqual((warning, deadline), (100, 400))
                self.assertEqual((clock['started'], clock['limit_seconds']), (1000, 900))
                core.atomic_json(store.path(rid) / 'run.json', {**store.read(rid), 'project_deadline': 2050})
                self.assertEqual(duration_record(store, rid, 'g1', 600, 900)[1], 60)
                record = store.read(rid)
                record.pop('project_deadline')
                core.atomic_json(store.path(rid) / 'run.json', record)
            with patch.object(core.time, 'time', return_value=2390), patch.object(core.time, 'monotonic', return_value=10):
                self.assertEqual(duration_record(store, rid, 'g1', 600, 900)[1], 10)

    def test_parent_stop_freezes_child_lane_but_cannot_extend_request_or_project_deadline(self):
        import tempfile
        from runner import duration_record
        with tempfile.TemporaryDirectory(prefix='duration-parent-') as home_dir:
            store = core.RunStore(Path(home_dir))
            parent = store.accept('a', 'parent')['run_id']
            child = store.accept('a', 'child')['run_id']
            store.update(child, project_ref_parent=parent)
            store.task_update(child, 'g1', runner_clock={'started': 1000, 'warning_seconds': 600, 'limit_seconds': 900})
            change = {'status': 'resumed', 'stop': {'ts': 1200, 'runs': [parent, child]},
                      'events': [{'action': 'resumed', 'ts': 1700}]}
            core.atomic_json(store.path(parent) / 'run.json', {**store.read(parent), 'plan_changes': [change],
                'project_deadline': 2050, 'request': {'duration_minutes': 1},
                'request_clock': {'active_sec': 20, 'session': None}})
            with patch.object(core.time, 'time', return_value=2000), patch.object(core.time, 'monotonic', return_value=10):
                self.assertEqual(duration_record(store, child, 'g1', 600, 900)[1], 50)
                store.update(parent, request_clock={'active_sec': 0, 'session': None})
                self.assertEqual(duration_record(store, child, 'g1', 600, 900)[1], 60)
                store.update(parent, request_clock={'active_sec': 60, 'session': None})
                self.assertEqual(duration_record(store, child, 'g1', 600, 900)[1], 10)

    def test_capture_passes_each_attempts_wall_clock_deadline_to_the_gate(self):
        import tempfile
        import time
        with tempfile.TemporaryDirectory(prefix='duration-') as home_dir:
            seen = []

            def execute(real, wt, args, logf, env_extra=None, role='impl'):
                seen.append(dict(env_extra or {}))
                return subprocess.Popen([sys.executable, '-c', 'pass'])
            with patch.object(core, 'QE', Path(home_dir)), patch.dict(os.environ, {'CADRE_RUN_ID': '', 'CADRE_HOME': home_dir}), \
                 patch.object(core, 'run_opencode', side_effect=execute):
                once = time.time()
                core.capture_opencode('/fake', home_dir, ['-m', 'opencode-go/glm-5.3', 'x'], 120, role='plan')
                then = time.time()
        self.assertEqual(len(seen), 1)
        self.assertTrue(once + 120 <= float(seen[0]['CADRE_DEADLINE_TS']) <= then + 120, seen[0])

    def test_runner_lane_gets_the_same_deadline_it_enforces(self):
        import time
        fixture = fixtures.SessionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.fake.write_text(fixture.fake.read_text()
                                + "\n(p / 'duration-last.txt').write_text(os.environ.get('CADRE_DEADLINE_TS', 'ABSENT'))\n")
        once = time.time()
        fixture.runner_fixture(env={'CADRE_WARN_SEC': '300', 'CADRE_ABORT_SEC': '777'})
        seen = (fixture.home / 'duration-last.txt').read_text()
        self.assertNotEqual(seen, 'ABSENT', 'The actual fake executor must read its deadline from the environment')
        hour = fixture.store.read(fixture.rid)['tasks']['g1']['runner_clock']
        self.assertEqual(hour['limit_seconds'], 777)
        self.assertAlmostEqual(float(seen), hour['started'] + hour['limit_seconds'], delta=2,
                               msg='The lane gate deadline matches the exact deadline enforced by the engine')
        self.assertLessEqual(once + 777 - 2, float(seen))
        self.assertTrue(once + 30 <= float(core.duration_end(30)) <= time.time() + 30)
        self.assertTrue(once <= float(core.duration_end(-5)) <= time.time(), 'A past deadline means zero remaining time')


class ExecutorSlotTest(unittest.TestCase):
    """DESIGN-5(2) + G-F1: the local executor goes through one slot serially; the cloud executor does not wait for the local slot."""
    def setUp(self):
        import tempfile
        t = tempfile.TemporaryDirectory(prefix='slot-')
        self.addCleanup(t.cleanup)
        self.home = Path(t.name)
        self.real = self.home / 'opencode'
        self.real.write_text('#!/bin/sh\nsleep 2\n')
        self.real.chmod(0o700)
        for ctx in (patch.object(core, 'QE', self.home), patch.dict(os.environ, {'CADRE_RUN_ID': '', 'CADRE_SLOT_POLL': '0.1'}),
                    patch.object(core, 'opencode_budget_env', side_effect=lambda env, args: list(args))):
            ctx.__enter__()
            self.addCleanup(ctx.__exit__, None, None, None)

    def second_wait(self, model):
        import time
        first = core.run_opencode(str(self.real), str(self.home), ['-m', model, 'x'], self.home / 'log')
        self.addCleanup(first.wait)
        start = time.monotonic()
        second = core.run_opencode(str(self.real), str(self.home), ['-m', model, 'x'], self.home / 'log')
        self.addCleanup(second.wait)
        return time.monotonic() - start

    def test_cloud_executors_are_concurrent(self):
        self.assertLess(self.second_wait('deepseek/deepseek-v4-flash'), 1.0)


class ParallelChoiceTest(unittest.TestCase):
    """DESIGN-5(2): at the same time only independent (different parallel_group, no dependencies, disjoint files) cloud lanes."""
    def test_only_independent_cloud_lanes_are_selected_together(self):
        import tempfile
        from run_store import RunStore
        with tempfile.TemporaryDirectory(prefix='parallel-') as home_dir:
            store = RunStore(Path(home_dir))
            rid = store.accept('a', 'task')['run_id']
            g = lambda i, grp, file, model='ds-pro', dependent=(): {'id': f'g{i}', 'definition': 't', 'acceptance': 'k', 'model': model, 'difficulty': 3,
                                                                  'parallel_group': grp, 'estimate_min': 5, 'files': [file], 'dependent': list(dependent)}
            plan = {'job_id': 'x', 'tasks': [g(1, 1, 'src/a.py'), g(2, 2, 'src/b.py'), g(3, 1, 'src/c.py'), g(4, 3, 'src/d.py', dependent=['g1']),
                                               g(5, 0, 'src/e.py', model='auto'), g(6, 4, 'src'), g(7, 5, 'docs/x.md')]}
            env = {**os.environ, **store.environment(rid)}
            execute = lambda gid, n: subprocess.run([sys.executable, str(LIB / 'dispatch_util.py'), 'parallel', gid, str(n)], input=json.dumps(plan),
                                                text=True, capture_output=True, env=env, timeout=20).stdout.split()
            # B340 (03.10): the empty-dependencies + disjoint-files rule instead of the parallel_group field
            self.assertEqual(execute('g1', 3), ['g2', 'g3'])
            self.assertEqual(execute('g1', 1), [])
            self.assertEqual(execute('g5', 3), ['g7'], 'An auto lane is also cloud-based; g7 runs in parallel with disjoint files')
            store.task_update(rid, 'g1', dispatch_result='produced')
            self.assertEqual(execute('g2', 3), ['g3', 'g4'])

    def test_real_file_aliases_and_new_names_follow_filesystem_identity(self):
        import tempfile
        import dispatch_util as du
        with tempfile.TemporaryDirectory(prefix='path-identity-') as directory:
            root = Path(directory)
            (root / 'Src').mkdir()
            file = root / 'Src' / 'Main.py'
            file.write_text('original')
            (root / 'Other').mkdir()
            os.link(file, root / 'Other' / 'hard.py')
            sensitive = not (root / 'src').exists()
            cases = [('Src/Main.py', 'Other/hard.py', False), ('Src', 'Src/new.py', False),
                     ('Src/Main.py', 'Src/other.py', True), ('Src/new.py', 'Src/NEW.py', sensitive),
                     ('Src/New/a.py', 'Src/new/A.py', sensitive), ('Src/Main.py', 'src/main.py', sensitive),
                     ('Src', 'src/new.py', sensitive)]
            for left, right, parallel in cases:
                with self.subTest(left=left, right=right):
                    tasks = [{'id': gid, 'files': [name], 'dependent': [], 'model': 'auto'}
                             for gid, name in (('g1', left), ('g2', right))]
                    plan = {'tasks': tasks}
                    self.assertEqual(du.parallel_candidates(plan, 'g1', 2, {}, root=root), ['g2'] if parallel else [])
            self.assertEqual(file.read_text(), 'original')
            self.assertFalse((root / 'Src/new.py').exists())
            self.assertFalse((root / 'Src/New').exists())
            if sys.platform == 'darwin':
                plan = {'tasks': [{'id': gid, 'files': [name], 'dependent': [], 'model': 'auto'}
                                     for gid, name in (('g1', 'new.py'), ('g2', 'NEW.py'))]}
                for policy, expected in ((0, []), (1, ['g2']), (-1, [])):
                    with self.subTest(policy=policy), patch.object(du.os, 'pathconf', return_value=policy):
                        self.assertEqual(du.parallel_candidates(plan, 'g1', 2, {}, root=root), expected)

    def test_parallel_selection_uses_recorded_worktree_and_unknown_paths_stay_serial(self):
        import tempfile
        import dispatch_util as du
        from run_store import RunStore
        with tempfile.TemporaryDirectory(prefix='path-cli-') as directory:
            root = Path(directory)
            store = RunStore(root / 'home')
            rid = store.accept('a', 'work')['run_id']
            wt = root / 'wt'
            wt.mkdir()
            (wt / 'one.py').write_text('x')
            os.link(wt / 'one.py', wt / 'two.py')
            store.update(rid, worktree=str(wt))
            plan = {'tasks': [{'id': 'g1', 'files': ['one.py'], 'model': 'auto'},
                                {'id': 'g2', 'files': ['two.py'], 'model': 'auto'}]}
            result = subprocess.run([sys.executable, str(LIB / 'dispatch_util.py'), 'parallel', 'g1', '2'],
                                    input=json.dumps(plan), text=True, capture_output=True,
                                    env={**os.environ, **store.environment(rid)}, cwd=root, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), '')
            plan['tasks'][1]['files'] = ['new.py']
            with patch.object(du, 'path_identity', side_effect=OSError('unverifiable filesystem')):
                self.assertEqual(du.parallel_candidates(plan, 'g1', 2, {}, root=wt), [])
            (wt / 'linked').symlink_to(root, target_is_directory=True)
            plan['tasks'][1]['files'] = ['linked/escape.py']
            self.assertEqual(du.parallel_candidates(plan, 'g1', 2, {}, root=wt), [])


PARALLEL_WORKER = """
import json, os, pathlib, sys, time
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
from run_store import RunStore
gid = os.environ['CADRE_TASK_ID']; wt = pathlib.Path(os.environ['CADRE_WORKTREE'])
start = time.time(); time.sleep(float(os.environ.get('FIXTURE_DURATION', '1')))
(wt / (gid + '.txt')).write_text(gid + '\\n')
with open(os.environ['FIXTURE_MOMENT'], 'a') as f:
    f.write(json.dumps({'gid': gid, 'model': os.environ.get('CADRE_RUN_MODEL') or 'local', 'start': start, 'last': time.time()}) + '\\n')
RunStore().task_update(os.environ['CADRE_RUN_ID'], gid, state='produced')
if gid == 'merge':
    counter = pathlib.Path(os.environ['FIXTURE_MOMENT'] + '.merge')
    n = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(n + 1))
    status = 'half' if n < int(os.environ.get('FIXTURE_MERGE_HALF', '0')) else 'produced'
    (RunStore().task_dir(os.environ['CADRE_RUN_ID'], gid) / 'status.txt').write_text(status + '\\n')
"""

PASS_JUDGE = """
import os, sys, json
q = json.loads(sys.argv[-1].splitlines()[-1])
a = {'verdict': 'PASS', 'revision': q['revision'], 'acceptance': [{'id': i['id'], 'status': 'PASS', 'evidence': 'files read'} for i in q['criteria']]}
print(json.dumps({'type': 'text', 'sessionID': 'ses_' + os.environ['CADRE_TASK_ID'], 'part': {'type': 'text', 'text': json.dumps(a)}}))
"""


class ParallelDistributorTest(unittest.TestCase):
    """P3 (03.10): local is closed; these tests tested a mixed local/cloud lane dispatch."""
    def setUp(self):
        import tempfile
        import shutil
        from run_store import RunStore
        t = Path(tempfile.mkdtemp(prefix='parallel-e2e-'))
        self.addCleanup(shutil.rmtree, t, True)
        self.t = t
        wt = t / 'wt'
        wt.mkdir()
        g = lambda *a: subprocess.run(['git', '-C', str(wt), '-c', 'user.name=f', '-c', 'user.email=f@l', *a], check=True, capture_output=True)
        g('init', '-q'); (wt / 'README.md').write_text('x\n'); g('add', '.'); g('commit', '-qm', 'base')
        base = subprocess.run(['git', '-C', str(wt), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
        self.store = RunStore(t)
        (t / 'projects/a').mkdir(parents=True)
        from qa_fixture import command
        (t / 'projects/a/qa-command').write_text(command('test -s g1.txt && test -s g2.txt'))
        self.rid = self.store.accept('a', 'two independent changes', source_sha=base)['run_id']
        self.store.update(self.rid, worktree=str(wt))
        writing = self.store.task_dir(self.rid, 'impl') / 'attempts/fixture/attempt.json'
        writing.parent.mkdir(parents=True)
        writing.write_text(json.dumps({'role': 'impl', 'model': 'fixture/writer', 'pid': 123}))

    def execute_run(self, models=('ds-pro-go', 'ds-pro'), **env):
        import shutil
        from run_store import execute_run
        root = self.t / 'dispatcher'
        from qa_fixture import instructions
        instructions(root)
        (root / 'lib').mkdir(parents=True)
        (root / 'bin').mkdir()
        runner = Path(__file__).with_name('qa_fixture.py')
        shutil.copyfile(runner, root / 'lib/qa_fixture.py')
        qa_path = Path(self.store.read(self.rid)['constraints']['qa-command'])
        qa_path.write_text(qa_path.read_text().replace(str(runner.resolve()), str(root / 'lib/qa_fixture.py')))
        for name in ('worktree_archive.py', 'run_store.py', 'runtime_sync.py', 'outcome.py', 'session.py', 'qe_core.py', 'liveness.py', 'acceptance.py', 'runner.py', 'dispatch_util.py', 'budget_gate.py', 'go_window.py', 'limits.py', 'limits.json', 'limits.mjs', 'opencode_adaptor.py', 'budget-plugin.mjs', 'cadre-plugin.mjs', 'monthly-budget-plugin.mjs', 'model_router.py', 'model_selection.py',
                     'model_catalog.py', 'contract.py', 'level.py', 'prefilter.py', 'task_split.py', 'report_mode.py', 'standard-task-form.txt'):
            shutil.copyfile(LIB / name, root / 'lib' / name)
        for source in LIB.glob('*.py'):
            if not (root / 'lib' / source.name).exists():
                shutil.copyfile(source, root / 'lib' / source.name)
        shutil.copytree(LIB / 'qa-sim', root / 'lib/qa-sim', ignore=shutil.ignore_patterns('__pycache__'), dirs_exist_ok=True)
        from test_end_to_end import FAKE_BUDGET
        from test_run_sessions import CATALOG
        (root / 'lib/provider_budget.py').write_text(FAKE_BUDGET)
        (self.t / 'models.json').write_text(json.dumps(CATALOG))
        shutil.copyfile(LIB.parent / 'bin/cadre-dispatch.sh', root / 'bin/cadre-dispatch.sh')
        (root / 'lib/common.sh').write_text('qe_verify_ai_worktree(){ return 0; }\nqe_make_worktree(){ return 99; }\nqe_commit_wip(){ echo 0; }\n')
        (root / 'worker.py').write_text(PARALLEL_WORKER)
        (root / 'bin/job-runner.sh').write_text(f'exec "{sys.executable}" "$CADRE_ENGINE_ROOT/worker.py"\n')
        plan = {'job_id': 'fixture', 'scope_points': 30, 'scope_rationale': 'two independent feature', 'tasks': [
            {'id': f'g{i}', 'definition': 't', 'model': m, 'parallel_group': i, 'files': [f'g{i}.txt'], 'acceptance': 'k', 'difficulty': 3, 'estimate_min': 1, 'estimate_usd': .01}
            for i, m in enumerate(models, 1)]}
        (self.store.path(self.rid) / 'plan.json').write_text(json.dumps(plan))
        self.store.update(self.rid, phase='dispatch')
        judge = self.t / 'fake-reviewer'
        judge.write_text(f'#!{sys.executable}\n' + PASS_JUDGE)
        judge.chmod(0o700)
        with patch.dict(os.environ, {'CADRE_ENGINE_ROOT': str(root), 'CADRE_REAL_OPENCODE': str(judge), 'OPENCODE_DB': str(self.t / 'absent.db'),
                                    'FIXTURE_MOMENT': str(self.t / 'moment'), 'CADRE_MODEL_CATALOG': str(self.t / 'models.json'), 'CADRE_WAIT_THRESHOLD_SEC': '0', 'CADRE_HOME': str(self.t), **env}):
            rc = execute_run(self.store, self.rid)
        self.assertTrue((self.t / 'moment').exists(), (self.store.path(self.rid) / 'dispatch.log').read_text())
        records = {j['gid']: j for j in map(json.loads, (self.t / 'moment').read_text().splitlines())}
        return rc, records

    def test_independent_cloud_lanes_run_concurrently(self):
        rc, k = self.execute_run()
        self.assertEqual(rc, 0, self.store.read(self.rid).get('final_result'))
        self.assertLess(max(k['g1']['start'], k['g2']['start']), min(k['g1']['last'], k['g2']['last']), 'Lanes did not overlap')
        self.assertEqual(self.store.read(self.rid)['state'], 'completed')

    def test_no_overlap_in_the_serial_setting(self):
        rc, k = self.execute_run(CADRE_PARALLEL_LANE='1')
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(max(k['g1']['start'], k['g2']['start']), min(k['g1']['last'], k['g2']['last']))

    def test_stuck_merge_escalates_to_the_next_model_in_the_operator_chain(self):
        chain = 'opencode-go/glm-5.3,opencode-go/kimi-k3'
        rc, _ = self.execute_run(models=('ds-pro', 'ds-pro', 'ds-pro', 'ds-pro'), FIXTURE_MERGE_HALF='2',
                         CADRE_CHAIN_MERGE=chain)
        merge = [json.loads(s)['model'] for s in (self.t / 'moment').read_text().splitlines() if json.loads(s)['gid'] == 'merge']
        self.assertEqual(merge, ['opencode-go/glm-5.3', 'opencode-go/glm-5.3', 'opencode-go/kimi-k3'],
                         'After two partial attempts, use the next chain link rather than an unrelated fixed model')
        self.assertEqual(rc, 0, (self.store.path(self.rid) / 'dispatch.log').read_text()[-2000:])

    def test_same_small_provider_goes_to_go_in_the_second_lane(self):
        rc, k = self.execute_run(models=('ds-pro', 'ds-pro'))
        self.assertEqual(rc, 0)
        # 4c-17: no direct DeepSeek route in the code chain; both lanes run on the same model's Go route
        self.assertEqual(sorted(j['model'] for j in k.values()), ['opencode-go/deepseek-v4-pro', 'opencode-go/deepseek-v4-pro'])


if __name__ == '__main__':
    unittest.main()


class DifficultyDistributionTests(unittest.TestCase):
    def test_hard_work_goes_to_budgeted_cloud_and_easy_work_uses_the_cheap_chain(self):
        """D13 (03.10): difficulty 1-2 -> the impl-light chain; 3-4 -> the role chain; 5 -> the strong role chain.
        No local model (06.10). If the model is in the chain it is used, otherwise the first link; an empty chain means no capacity."""
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
        import model_router as router
        from unittest.mock import patch
        open = (['opencode-go/qwen3.8-flash'], [])
        with patch.object(router, 'chain', return_value=open):
            # D13: difficulty <=2 -> impl-light; empty model -> first link
            self.assertEqual(router.difficulty_model('', 4), 'opencode-go/qwen3.8-flash')
            self.assertEqual(router.difficulty_model('', 2), 'opencode-go/qwen3.8-flash')
            # D13: if the model is in the chain it is used
            self.assertEqual(router.difficulty_model('opencode-go/qwen3.8-flash', 2), 'opencode-go/qwen3.8-flash')
            self.assertEqual(router.difficulty_model('opencode-go/qwen3.8-flash', 3), 'opencode-go/qwen3.8-flash')
        with patch.object(router, 'chain', return_value=([], [])):
            self.assertIsNone(router.difficulty_model('', 5))
        # If the model is in the chain it is used (D13: no free/paid distinction)
        for model in ('openai/gpt-6.1-sol', 'opencode/big-pickle', 'google/gemini-3-flash-preview'):
            with patch.object(router, 'chain', return_value=([model], [])):
                self.assertEqual(router.difficulty_model(model, 2), model)
        self.assertIn('suitable "$gmdl" impl "$gz"', (Path(__file__).resolve().parents[1] / 'bin' / 'cadre-dispatch.sh').read_text().split('lane_model(){')[1].split('\n}')[0])


class PlanParallelRuleTests(unittest.TestCase):
    def test_plan_prompt_keeps_shared_docs_to_the_last_task(self):
        """B328 (03.10): since every task touched BACKLOG/CONTRACT, 5 independent code jobs were serialized (~6 h)."""
        prompt = (Path(__file__).resolve().parents[1] / 'bin' / 'cadre-dispatch.sh').read_text()
        self.assertIn('SHARED DOCUMENTS (BACKLOG.md, README) are updated only in the FINAL task', prompt)
        self.assertIn('A task that adds a NEW engine file must add its row to the code map in docs/CONTRACT.md itself', prompt)


class MomentSettingsTests(unittest.TestCase):
    def test_plan_with_shared_registry_in_two_tasks_is_rejected(self):
        """B328: BACKLOG/README/CONTRACT only in the last task; otherwise a plan schema error (the planner fixes it)."""
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
        import dispatch_util as du
        def plan(d1, d2):
            g = lambda i, d: {'id': i, 'definition': 'x', 'files': d, 'acceptance': 'test passes', 'scope_outside': [], 'dependent': [], 'difficulty': 3,
                              'model': 'auto', 'parallel_group': 1, 'estimate_min': 10}
            return {'job_id': 'p', 'scope_points': 30, 'tasks': [g('g1', d1), g('g2', d2)], 'merge_order': ['g1', 'g2'],
                    'quality': {'model': 'x', 'scope': 'y'}}
        self.assertTrue(du.shared_document_error(plan(['a.py', 'BACKLOG.md'], ['b.py', 'BACKLOG.md'])))
        self.assertTrue(du.shared_document_error(plan(['a.py', 'BACKLOG.md'], ['b.py'])))
        self.assertIsNone(du.shared_document_error(plan(['a.py'], ['b.py', 'BACKLOG.md'])))
        # B343: a task adding a new engine file updates the contract map itself (the guard requires it in the same task)
        self.assertIsNone(du.shared_document_error(plan(['a.py', 'docs/CONTRACT.md'], ['b.py', 'docs/CONTRACT.md', 'BACKLOG.md'])))
        # 03.10 00:26 live: when the rule was applied while re-reading a recorded plan the running run failed — re-reading must accept
        du.validate_plan(plan(['a.py', 'BACKLOG.md'], ['b.py', 'BACKLOG.md']))
        import subprocess as sp, json as js
        r = sp.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'lib/dispatch_util.py'), 'extract'],
                   input=js.dumps(plan(['a.py', 'BACKLOG.md'], ['b.py', 'BACKLOG.md'])), capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0, 'Apply the rule when accepting a fresh plan')
