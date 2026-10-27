"""QA-2 (15.10.2026, FIX-28..FIX-49): regression tests for the defects verified by five independent agents."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import panel_data  # noqa: E402
import budget_gate as bg  # noqa: E402
from liveness import Liveness  # noqa: E402
import ui_messages  # noqa: E402
import permission  # noqa: E402
import acceptance  # noqa: E402
import run_store  # noqa: E402
from run_store import RunStore, atomic_json, process_identity  # noqa: E402
import install_check  # noqa: E402
import cost_report  # noqa: E402
from test_budget_period import snapshot  # noqa: E402

GIT_ID = {'GIT_AUTHOR_NAME': 'fixture', 'GIT_AUTHOR_EMAIL': 'f@local', 'GIT_COMMITTER_NAME': 'fixture', 'GIT_COMMITTER_EMAIL': 'f@local'}


class BudgetFamilyTests(unittest.TestCase):
    """FIX-28 / FIX-33: an unrelated broken record does not lock the family; recovery does not stop at a single broken line."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='qa2-budget-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        env = patch.dict(os.environ, {'CADRE_HOME': str(self.home), 'CADRE_RUN_CAP_USD': '1', 'CADRE_SPEND_ALARM': '0',
                                      'CADRE_TASK_ID': '', 'CADRE_NO_NET': '1'})
        env.start()
        self.addCleanup(env.stop)
        self.store = RunStore(self.home)
        self.rid = self.store.accept('work', 'selected work')['run_id']
        self.ledger = bg.BudgetLedger(self.home / 'chain/request-budget.db')
        self.budget = snapshot(50)
        self.budget.update(zen={'db': 'ok', 'pay': 50, 'available': 50}, **{'or': {'db': 'ok', 'pay': 50, 'available': 50}})

    def reserve(self):
        cap = bg.role_cap(self.home, self.rid, slice_name='lanes')
        return self.ledger.reserve('openrouter', 1000, self.budget, {'run_id': self.rid, 'phase_role': 'lanes'}, run_cap=cap, slice='lanes')

    def test_unrelated_unreadable_records_do_not_block_the_family_but_a_broken_member_does(self):
        self.reserve()
        for name, body in (('zzz-old', json.dumps({'run_id': 'zzz-old', 'version': 0})), ('zzz-empty', ''), ('zzz-invalid', '{not json')):
            (self.store.root / name).mkdir()
            (self.store.root / name / 'run.json').write_text(body)
        self.assertEqual(bg.operator_family(self.home, self.rid)[0], {self.rid})
        self.reserve()
        (self.store.root / 'zzz-member').mkdir()
        (self.store.root / 'zzz-member' / 'run.json').write_text(json.dumps({'run_id': 'zzz-member', 'project_ref_parent': self.rid, 'version': 0}))
        with self.assertRaisesRegex(bg.BudgetDenied, 'ownership unavailable'):
            bg.operator_family(self.home, self.rid)
        with self.assertRaises(bg.BudgetDenied):
            self.reserve()

    def test_stale_recovery_skips_an_unreadable_bound_and_closes_the_readable_rows(self):
        now = time.time()
        with self.ledger.transaction() as con:
            con.execute('insert into reservation values(?,?,?,?,?,?,?,?)',
                        ('legacy-a', 'openrouter', '2026-10', now - 7200, 5000, None, None, json.dumps({'worst': 1.5})))
            con.execute('insert into reservation values(?,?,?,?,?,?,?,?)',
                        ('legacy-b', 'openrouter', '2026-10', now - 7200, 4000, None, None, json.dumps({})))
        with patch.object(sys, 'stdout', io.StringIO()), patch.object(sys, 'stderr', io.StringIO()):
            bg.recovery(self.home, ['--stale-close', '--yes', '--hour', '1'])
        with self.ledger.transaction() as con:
            open = [r[0] for r in con.execute('select id from reservation where actual is null order by id').fetchall()]
        self.assertEqual(open, ['legacy-a'])


class BaseEnvironmentTests(unittest.TestCase):
    """FIX-30: terminal/session variables do not change the baseline fingerprint; the build environment does."""

    def test_session_variables_do_not_change_the_fingerprint(self):
        with tempfile.TemporaryDirectory(prefix='qa2-base-') as d:
            subprocess.run(['git', '-C', d, 'init', '-q', '-b', 'main'], check=True)
            (Path(d) / 'a.txt').write_text('a\n')
            subprocess.run(['git', '-C', d, 'add', '-A'], check=True)
            subprocess.run(['git', '-C', d, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'init'], check=True)
            base = acceptance.baseline_context(d, 'true', '')['environment']
            with patch.dict(os.environ, {'TERM_SESSION_ID': 'w0t1', 'ITERM_SESSION_ID': 'x', 'SSH_AUTH_SOCK': '/tmp/sock',
                                         'TMUX': '/tmp/tmux', 'TMPDIR': '/tmp/other', 'VSCODE_PID': '1', 'COLUMNS': '300',
                                         'CODEX_THREAD_ID': 'another-session', 'CODEX_TOOL_CALL_ID': 'another-call'}):
                self.assertEqual(acceptance.baseline_context(d, 'true', '')['environment'], base)
            with patch.dict(os.environ, {'LANG': 'xx_YY.UTF-8'}):
                self.assertNotEqual(acceptance.baseline_context(d, 'true', '')['environment'], base)
            with patch.dict(os.environ, {'PRODUCT_TEST_MODE': 'changed'}):
                self.assertNotEqual(acceptance.baseline_context(d, 'true', '')['environment'], base)


class InstallTimeoutTests(unittest.TestCase):
    """FIX-29: an approved install action that timed out is completed; it can be retried with a new-generation approval."""

    def setUp(self):
        from test_baseline import BaselineTests
        self.b = BaselineTests()
        self.b.setUp()
        self.addCleanup(self.b.doCleanups)

    def test_timed_out_setup_completes_its_action_and_can_be_retried(self):
        b = self.b
        (b.project / 'install-command').write_text('sleep 3')
        with patch.dict(os.environ, {'CADRE_INSTALL_TIMEOUT': '1'}):
            result = b.baseline(approve_setup=True)
        self.assertEqual((result['status'], result['reason'], result['install']['exit_code']), ('FAIL', 'install-error', 124))
        folder = permission.scope(b.store, b.rid) / 'operator-actions'
        self.assertEqual(sorted(json.loads(p.read_text())['status'] for p in folder.glob('*.json')), ['completed'])
        again = permission.setup_proposal(b.store, b.rid, b.wt, 'sleep 3')
        self.assertEqual((again['status'], again['identity']['generation']), ('pending', 2))


class ParsingTests(unittest.TestCase):
    """FIX-31: an action left half-done (in-flight/uncertain) closes with an operator decision; denied while a live owner exists."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='qa2-permission-')
        self.addCleanup(folder.cleanup)
        self.store = RunStore(Path(folder.name))
        self.rid = self.store.accept('fixture', 'local work')['run_id']
        self.args = (self.store, self.rid, 'push', [['git', 'push', 'origin', 'main']], {'path': '/fixture', 'branch': 'main'}, {'head': 'h'})
        self.action = permission.propose(*self.args)
        first = permission.approve(self.store, self.rid, self.action['token'])
        if first['required'] == 2:
            permission.approve(self.store, self.rid, self.action['token'], first['confirmation'])

    def write(self, **values):
        action = permission.read_action(self.store, self.rid, self.action['token'])
        action.update(values)
        atomic_json(permission.scope(self.store, self.rid) / 'operator-actions' / (self.action['token'] + '.json'), action)
        return action

    def test_in_flight_action_can_be_closed_and_reopened_by_the_operator(self):
        self.write(status='in-flight', consumed=time.time())
        self.assertEqual(permission.propose(*self.args)['token'], self.action['token'])
        with self.assertRaisesRegex(ValueError, 'operator-resolution-invalid'):
            permission.resolve(self.store, self.rid, self.action['token'], 'maybe')
        closed = permission.resolve(self.store, self.rid, self.action['token'], 'no-effect')
        self.assertEqual((closed['status'], closed['resolution']['previous'], closed['resolution']['by']), ('failed', 'in-flight', 'operator'))
        self.assertEqual(permission.propose(*self.args)['identity']['generation'], 2)
        self.assertEqual(self.store.read(self.rid)['operator_interventions'][-1]['kind'], 'operator-resolve')
        with self.assertRaisesRegex(PermissionError, 'operator-effect-unresolved'):
            permission.resolve(self.store, self.rid, self.action['token'], 'completed')

    def test_uncertain_action_marked_done_keeps_the_single_generation(self):
        self.write(status='uncertain', error='KeyboardInterrupt')
        done = permission.resolve(self.store, self.rid, self.action['token'], 'completed')
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(permission.propose(*self.args)['token'], self.action['token'])

    def test_live_owner_and_running_runs_block_resolution(self):
        self.write(status='in-flight', effect_seed={'process': process_identity(os.getpid())})
        with self.assertRaisesRegex(PermissionError, 'operator-effect-owner-active'):
            permission.resolve(self.store, self.rid, self.action['token'], 'no-effect')
        with patch.dict(os.environ, {'CADRE_RUN_ID': self.rid, 'CADRE_HOME': str(self.store.home)}):
            with self.assertRaisesRegex(PermissionError, 'operator-command-required'):
                permission.main(['resolve', self.rid, self.action['token'], 'no-effect'])


class ModelApprovalWaitTests(unittest.TestCase):
    """FIX-47: root and child records waiting for model approval are released together with the approval."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='qa2-model-')
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.store = RunStore(self.home)
        self.root = self.store.accept('fixture', 'project_ref')['run_id']
        self.child = self.store.accept('fixture', 'child')['run_id']
        self.store.update(self.child, project_ref_parent=self.root)

    def test_activation_clears_the_root_and_its_children(self):
        action = permission.propose(self.store, self.root, 'model-calls', *permission.model_details(self.store, self.root))
        first = permission.approve(self.store, self.root, action['token'])
        permission.approve(self.store, self.root, action['token'], first['confirmation'])
        self.store.update(self.child, tool_boundary_wait='project-tool-operator-model-approval-required', reason='operator-model-approval-required')
        self.store.update(self.root, reason='project-ref-stopped: project-tool-operator-model-approval-required')
        permission.model_activate(self.store, self.root, action['token'])
        for rid in (self.root, self.child):
            record = self.store.read(rid)
            self.assertEqual((record.get('tool_boundary_wait') or '', record['reason']), ('', 'operator-model-ready'), rid)

    def test_denied_check_marks_both_root_and_child(self):
        with patch.dict(os.environ, {'CADRE_HOME': str(self.home)}), patch.object(permission, 'model_check', side_effect=PermissionError('x')):
            with self.assertRaises(PermissionError):
                permission.main(['model-check', self.child, 'openrouter'])
        for rid in (self.root, self.child):
            self.assertEqual(self.store.read(rid)['tool_boundary_wait'], 'project-tool-operator-model-approval-required', rid)


class HalfStartingTests(unittest.TestCase):
    """FIX-46: an empty folder interrupted between git init and the first commit continues by asking for permission again."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='qa2-init-')
        self.addCleanup(folder.cleanup)
        self.tmp = Path(folder.name)
        self.home = self.tmp / 'home_dir'
        self.home.mkdir()
        env = patch.dict(os.environ, {'CADRE_HOME': str(self.home), 'GIT_CEILING_DIRECTORIES': str(self.tmp.parent), **GIT_ID})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('CADRE_RUN_ID', None)
        self.store = RunStore(self.home)

    def test_init_without_commit_is_resumed_as_an_empty_project(self):
        import project_registry
        target = self.tmp / 'fresh'
        target.mkdir()
        subprocess.run(['git', '-C', str(target), 'init', '-q', '-b', 'main'], check=True)
        with self.assertRaises(project_registry.RecordPermission) as first:
            project_registry.save(target, create=True, home=self.home)
        token = first.exception.token
        action = permission.read_action(self.store, 'system', token)
        self.assertEqual(action['identity']['kind'], 'init-commit')
        action.update(status='in-flight', consumed=time.time())
        atomic_json(permission.scope(self.store, 'system') / 'operator-actions' / (token + '.json'), action)
        with self.assertRaises(project_registry.RecordPermission) as second:
            project_registry.save(target, create=False, home=self.home)
        self.assertNotEqual(second.exception.token, token)
        self.assertEqual(permission.read_action(self.store, 'system', token)['status'], 'failed')
        (target / 'x.txt').write_text('x\n')
        with self.assertRaises(project_registry.RecordError):
            project_registry.save(target, create=True, home=self.home)


class ParallelToolTests(unittest.TestCase):
    """FIX-34: the set of open tool steps; a sibling tool finishing does not drop an open step."""

    def test_open_tool_set_survives_a_sibling_completion(self):
        with tempfile.TemporaryDirectory(prefix='qa2-live-') as d:
            log = Path(d) / 'out.log'
            guard = object.__new__(Liveness)
            guard.logf, guard.offset, guard.remainder, guard.steps = log, 0, b'', set()
            guard.open_tools, guard.open_step = set(), False

            def event(call, status):
                return json.dumps({'part': {'type': 'tool', 'callID': call, 'tool': 'bash',
                                            'state': {'status': status, 'input': {'c': call}, 'output': ''}}}) + '\n'
            log.write_text(event('a', 'running') + event('b', 'running') + event('b', 'completed'))
            self.assertTrue(guard._step())
            self.assertTrue(guard.open_step, 'A is still open')
            with log.open('a') as stream:
                stream.write(event('a', 'completed'))
            guard._step()
            self.assertFalse(guard.open_step)


class CoordinatorIdTests(unittest.TestCase):
    """FIX-35: the coordinator completes its identity after the terminal/turn checks, before work starts."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='qa2-id-')
        self.addCleanup(folder.cleanup)
        self.tmp = Path(folder.name)
        self.home = self.tmp / 'home_dir'
        (self.home / 'projects' / 'p').mkdir(parents=True)
        repo = self.tmp / 'repo'
        repo.mkdir()
        subprocess.run(['git', '-C', str(repo), 'init', '-q', '-b', 'main'], check=True)
        (repo / 'a.py').write_text('x = 1\n')
        subprocess.run(['git', '-C', str(repo), 'add', '-A'], check=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'init'], check=True)
        (self.home / 'projects' / 'p' / 'repo-path').write_text(str(repo) + '\n')
        (self.home / 'projects' / 'p' / 'base-branch').write_text('main\n')
        env = patch.dict(os.environ, {'CADRE_HOME': str(self.home), 'OPENCODE_CFG': str(self.tmp / 'cfg')})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('CADRE_RUN_ID', None)
        self.store = RunStore(self.home)

    def test_coordinator_completes_identity_before_any_work_starts(self):
        record = self.store.accept('p', 'small task')
        self.assertIsNone(record['provenance']['completed'])
        real = subprocess.Popen

        def popen(args, *rest, **kw):
            if args and args[0] == 'caffeinate':
                raise OSError('no caffeinate')
            return real(args, *rest, **kw)
        with patch.object(run_store, 'disk_wait', return_value=None), patch.object(run_store, 'guard_revive', return_value=[]), \
             patch.object(subprocess, 'Popen', side_effect=popen):
            self.assertEqual(run_store.execute_run(self.store, record['run_id']), 130)
        done = self.store.read(record['run_id'])
        self.assertEqual(done['state'], 'cancelled')
        self.assertIsNotNone(done['provenance']['completed'])
        self.assertIsInstance(done['provenance']['runtime']['git_version'], str)


class OperatorCommandTests(unittest.TestCase):
    """FIX-36 / FIX-48: `cadre init --create` goes through the wrapper; request-approve does not work from inside a run."""

    def test_init_wrapper_passes_create_and_help_lists_the_new_commands(self):
        with tempfile.TemporaryDirectory(prefix='qa2-cadre-') as d:
            home = Path(d) / 'home'
            (Path(d) / 'h').mkdir()
            home.mkdir()
            env = {**os.environ, 'HOME': str(Path(d) / 'h'), 'CADRE_HOME': str(home),
                   'GIT_CEILING_DIRECTORIES': str(Path(d).parent), **GIT_ID}
            env.pop('CADRE_RUN_ID', None)
            target = Path(d) / 'fresh-project-ref'
            result = subprocess.run(['bash', str(LIB.parent / 'bin/cadre.sh'), 'init', '--create', str(target)], input='',
                                    capture_output=True, text=True, env=env, cwd=d, timeout=120)
            self.assertTrue(target.is_dir(), result.stdout + result.stderr)
            self.assertNotIn('already present', result.stdout + result.stderr)
            help_text = subprocess.run(['bash', str(LIB.parent / 'bin/cadre.sh'), '--help'], capture_output=True, text=True, env=env, timeout=60).stdout
            self.assertIn('init [--create] [--round python] [dir]', help_text)
            self.assertIn('permission-parse', help_text)

    def test_request_approve_refuses_to_run_inside_a_run(self):
        with tempfile.TemporaryDirectory(prefix='qa2-run-') as d:
            env = {**os.environ, 'CADRE_HOME': d, 'CADRE_RUN_ID': 'r1'}
            result = subprocess.run([sys.executable, str(LIB / 'run_store.py'), 'request-approve', 'r1'], input='{}',
                                    capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(result.returncode, 78)
            self.assertIn('request-approve-operator-command-required', result.stderr)


class CatalogTests(unittest.TestCase):
    """FIX-32: a missing catalogue is visible in the install check; the router names the rejection reason."""

    def test_install_check_reports_the_model_catalog(self):
        with tempfile.TemporaryDirectory(prefix='qa2-catalog-') as d:
            env = {'PATH': os.environ.get('PATH', ''), 'CADRE_MODEL_CATALOG': str(Path(d) / 'models.json'), 'CADRE_HOME': d,
                   'OPENCODE_CFG': d, 'CADRE_ENGINE_ROOT': str(LIB.parent)}
            items = {i['entry_name']: i for i in install_check.check(environment=env)['items']}
            self.assertEqual((items['model catalog']['status'], items['model catalog']['required']), ('MISSING', False))
            (Path(d) / 'models.json').write_text('{}')
            items = {i['entry_name']: i for i in install_check.check(environment=env)['items']}
            self.assertEqual(items['model catalog']['status'], 'PRESENT')

    def test_router_names_the_missing_catalog(self):
        import model_router
        with patch('model_selection.call_cap', side_effect=ValueError('model absent from catalog')), \
             patch.object(model_router, 'request_cap', return_value=10 ** 9), \
             patch.object(model_router, 'daily_token_remaining', return_value=None), \
             patch('budget_gate.model_bucket', return_value='bucket'):
            ok, reason = model_router.has_budget('opencode-go', 'glm-5.3', {'go': {'pay': 50}}, 8000, role='impl', selection={'preference': 'balanced'})
        self.assertFalse(ok)
        self.assertIn('model catalog/price missing', reason)
        self.assertIn('model absent from catalog', reason)


class CostReportSummaryTests(unittest.TestCase):
    """FIX-44: a value that cannot be measured stays unknown, never written as zero; labels come from the message helper."""

    def test_unknown_units_and_missing_waits_are_not_printed_as_zero(self):
        stage = {**cost_report._empty(), 'request': 2, 'unknown_unit': 2}
        data = {'expense': {'stages': {'impl': stage, 'qa': cost_report._empty()}},
                'duration': {'operation_sec': {'impl': 12.0}, 'queue_sec': None, 'quota_wait_sec': None, 'install_sec': None}}
        lines = cost_report.summary_lines(data)
        self.assertEqual(len(lines), 2)
        self.assertIn('in an unknown unit', lines[0])
        self.assertNotIn('$0.0000', lines[0])
        self.assertIn('quota wait unknown', lines[1])
        self.assertIn('setup unknown', lines[1])

    def test_missing_wait_records_stay_unknown(self):
        with tempfile.TemporaryDirectory(prefix='qa2-cost-report-') as d:
            store = RunStore(Path(d))
            rid = store.accept('p', 'task')['run_id']
            self.assertIsNone(cost_report.duration(store, [rid], rid)['quota_wait_sec'])
            store.update(rid, budget_waits=[{'ts': 10.0, 'until': 25.0}])
            self.assertEqual(cost_report.duration(store, [rid], rid)['quota_wait_sec'], 15.0)


class PanelDataTests(unittest.TestCase):
    """FIX-41: in a finished run a step that never started is 'not run'; singular forms; no leftover semicolons."""

    def test_terminal_run_shows_unstarted_resumed_tasks_as_not_done(self):
        k = {'run_id': 'r1', 'state': 'failed', 'project': 'p', 'tasks': {'g1': {'state': 'waiting'}, 'g2': {'state': 'running'}}}
        with tempfile.TemporaryDirectory(prefix='qa2-panel-') as d:
            pending = panel_data.sub_task_node(Path(d), k, {'id': 'g1', 'definition': 'x'}, {}, {})
            half = panel_data.sub_task_node(Path(d), k, {'id': 'g2', 'definition': 'y'}, {}, {})
        self.assertEqual(pending['summary'].split(' · ')[0], 'Not run')
        self.assertEqual(half['summary'].split(' · ')[0], 'Left unfinished')

    def test_english_singular_forms(self):
        self.assertEqual((ui_messages.t('… {n} more tasks · Tasks', n=1), ui_messages.t('… {n} more alerts · Attention', n=1), ui_messages.t('{n} records', n=1)),
                         ('… 1 more task · Tasks', '… 1 more alert · Attention', '1 record'))
        self.assertEqual(ui_messages.t('{n} records', n=3), '3 records')
        for template, singular, plural in (
                ('Parent and {n} children · all attempts', 'Parent and 1 child · all attempts', 'Parent and 2 children · all attempts'),
                ('{n} samples', '1 sample', '2 samples'),
                ('{n} samples · {b} malformed', '1 sample · 0 malformed', '2 samples · 0 malformed'),
                ('{n} tasks waiting', '1 task waiting', '2 tasks waiting'),
                ('{n} task inputs awaiting planning', '1 task input awaiting planning', '2 task inputs awaiting planning'),
                ('{n} revisions', '1 revision', '2 revisions'),
                ('(tool output · {n} characters)', '(tool output · 1 character)', '(tool output · 2 characters)')):
            with self.subTest(template=template):
                self.assertEqual(ui_messages.t(template, n=1, b=0), singular)
                self.assertEqual(ui_messages.t(template, n=2, b=0), plural)
        self.assertFalse([k for k, v in ui_messages.SINGULAR.items() if ';' in k or ';' in v])


if __name__ == '__main__':
    unittest.main()
