from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))

import qe_core as core
from kosu import atomic_json


class BaselineTests(unittest.TestCase):
    def setUp(self):
        from test_kosu_sessions import SessionTests
        self.f = SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.store, self.home, self.wt = self.f.store, self.f.home, self.f.wt
        self.git('init', '-q')
        (self.wt / 'calc.py').write_text('value=1\nold=False\nnew=False\n')
        (self.wt / 'test_product.py').write_text('import unittest,calc\nclass Product(unittest.TestCase):\n'
            ' def test_value(self): self.assertEqual(calc.value,1)\n'
            ' def test_old(self): self.assertFalse(calc.old)\n'
            ' def test_new(self): self.assertFalse(calc.new)\n')
        self.git('add', '.')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=f@local', 'commit', '-qm', 'baseline')
        self.project = self.home / 'projects/a'
        self.project.mkdir(parents=True)
        (self.project / 'repo-path').write_text(str(self.wt))
        self.command = shlex.quote(sys.executable) + ' -B -m unittest -v test_product'
        (self.project / 'qa-komut').write_text(self.command)
        self.rid = self.store.accept('a', 'Baseline checks', source_sha=self.git('rev-parse', 'HEAD').strip())['run_id']
        self.store.update(self.rid, worktree=str(self.wt))
        self.f.enter(patch.dict(os.environ, self.store.environment(self.rid)))
        self.marker = self.store.path(self.rid) / 'taban-qa.json'
        self.log = self.home / 'baseline.log'

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.wt), *args], text=True)

    def baseline(self, approve_setup=False):
        setup = self.project / 'kurulum-komut'
        if approve_setup and setup.exists() and setup.read_text().strip():
            import izin
            action = izin.setup_proposal(self.store, self.rid, self.wt, setup.read_text().strip())
            if action['status'] == 'pending':
                with patch.dict(os.environ, CADRE_RUN_ID=''):
                    izin.approve(self.store, self.rid, action['token'])
        return core.taban_hazirlik('a', self.wt, self.log, self.marker, self.project)

    def qa_command(self, command):
        (self.project / 'qa-komut').write_text(command)
        Path(self.store.read(self.rid)['constraints']['qa-komut']).write_text(command)

    def check(self):
        return core.qa_kapisi('a', str(self.wt), self.log, str(self.f.fake), model='claude/fixture')

    def test_green_baseline_then_real_new_test_failure_is_a_new_regression(self):
        baseline = self.baseline()
        self.assertEqual(baseline['qa']['status'], 'PASS')
        (self.wt / 'calc.py').write_text('value=1\nold=False\nnew=True\n')
        result = self.check()
        self.assertEqual((result['status'], result['baseline']['reason']), ('FAIL', 'new-regression'))
        self.assertEqual(self.baseline()['applicability'], 'original-source')
        self.assertEqual(json.loads(self.marker.read_text()), baseline)
        self.assertFalse((self.home / 'calls.jsonl').exists())

    def test_red_baseline_separates_existing_and_added_failures_without_accepting_either(self):
        (self.wt / 'calc.py').write_text('value=1\nold=True\nnew=False\n')
        self.git('add', '.')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=f@local', 'commit', '-qm', 'existing failure')
        self.rid = self.store.accept('a', 'Existing failing source', source_sha=self.git('rev-parse', 'HEAD').strip())['run_id']
        self.marker = self.store.path(self.rid) / 'taban-qa.json'
        self.f.enter(patch.dict(os.environ, self.store.environment(self.rid)))
        baseline = self.baseline()
        self.assertEqual((baseline['status'], baseline['qa']['status']), ('PASS', 'FAIL'))
        result = self.check()
        self.assertEqual((result['status'], result['baseline']['reason']), ('FAIL', 'pre-existing-failures'))
        (self.wt / 'calc.py').write_text('value=1\nold=True\nnew=True\n')
        result = self.check()
        self.assertEqual((result['status'], result['baseline']['reason']), ('FAIL', 'new-regression'))
        self.assertIn('test_new', ' '.join(result['baseline']['introduced']))
        self.assertIn('test_old', ' '.join(result['baseline']['existing']))
        (self.wt / 'calc.py').write_text('value=1\nold=2\nnew=False\n')
        result = self.check()
        self.assertEqual(result['baseline']['reason'], 'new-regression')
        self.assertIn('test_old', ' '.join(result['baseline']['changed']))

    def test_same_source_environment_and_command_share_parent_baseline(self):
        parent = self.store.accept('parent', 'project baseline')['run_id']
        parent_marker = self.store.path(parent) / 'taban-qa.json'
        with patch.dict(os.environ, self.store.environment(parent)):
            original = core.taban_hazirlik('a', self.wt, self.log, parent_marker, self.project)
        self.store.update(self.rid, proje_ust=parent, feature_acceptance={'id': 'f1', 'qa_komut': self.command})
        with patch.object(core, '_taban_komut', side_effect=AssertionError('matching baseline must be reused')):
            shared = self.baseline()
        self.assertEqual(shared['reused_from'], parent)
        self.assertEqual(shared['qa'], original['qa'])
        self.assertEqual(shared['context'], original['context'])
        self.marker.unlink()
        Path(original['qa']['output']).write_text('corrupted parent evidence')
        measured = self.baseline()
        self.assertNotIn('reused_from', measured)
        self.assertEqual(measured['qa']['status'], 'PASS')

    def test_environment_change_rejects_cached_baseline_and_new_child_measures_its_own_environment(self):
        original = self.baseline()
        with patch.dict(os.environ, {'PRODUCT_TEST_MODE': 'changed'}):
            self.assertEqual(self.baseline()['reason'], 'baseline-context-changed')
        with patch.dict(os.environ, {'LANG': 'C.UTF-8', 'TZ': 'UTC'}):
            denied = self.baseline()
            self.assertEqual((denied['status'], denied['reason']), ('FAIL', 'baseline-context-changed'))
            self.assertEqual(json.loads(self.marker.read_text()), original)
            child = self.store.accept('child', 'new environment')['run_id']
            self.store.update(child, proje_ust=self.rid, feature_acceptance={'id': 'f1'})
            with patch.dict(os.environ, self.store.environment(child)):
                result = core.taban_hazirlik('a', self.wt, self.log, self.store.path(child) / 'taban-qa.json', self.project)
            self.assertNotIn('reused_from', result)
            self.assertEqual(result['qa']['status'], 'PASS')
            self.assertNotEqual(result['context']['environment'], original['context']['environment'])

    def test_deleted_test_cannot_turn_real_qa_green(self):
        self.baseline()
        (self.wt / 'test_product.py').unlink()
        (self.wt / 'test_replacement.py').write_text('import unittest\nclass Kept(unittest.TestCase):\n def test_kept(self): self.assertTrue(True)\n')
        (self.project / 'qa-komut').write_text(shlex.quote(sys.executable) + ' -B -m unittest discover -v')
        record = self.store.read(self.rid)
        Path(record['constraints']['qa-komut']).write_text((self.project / 'qa-komut').read_text())
        result = self.check()
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'baseline-tests-removed'))
        self.assertEqual(result['baseline']['removed'], ['test_product.py'])

    def test_explicit_baseline_renewal_remeasures_original_source_and_preserves_previous_evidence(self):
        import kabul
        self.store.update(self.rid, state='retryable')
        original = self.baseline()
        output = Path(original['qa']['output']).read_bytes()
        record = self.marker.read_bytes()
        with patch.dict(os.environ, {'PRODUCT_TEST_MODE': 'renewed'}):
            self.assertEqual(self.baseline()['reason'], 'baseline-context-changed')
            measured = kabul.taban_yenile('a', self.wt, self.log, self.marker, self.project)
            self.assertEqual((measured['status'], measured['qa']['status']), ('PASS', 'PASS'))
            self.assertNotEqual(measured['context']['environment'], original['context']['environment'])
            history = measured['previous_baseline']
            self.assertEqual(Path(history['record']).read_bytes(), record)
            self.assertEqual(Path(history['output']).read_bytes(), output)
            self.assertEqual(self.baseline(), measured | {'applicability': 'same-source', 'observed_source': measured['context']['source']})
        self.assertFalse((self.home / 'calls.jsonl').exists())

    def test_baseline_renewal_cannot_hide_product_edits_or_repeat_setup_or_change_qa(self):
        import kabul
        self.store.update(self.rid, state='retryable')
        self.baseline()
        original = self.marker.read_bytes()
        with core.FileLock(self.store.path(self.rid) / '.owner.lock'):
            self.assertEqual(kabul.taban_yenile('a', self.wt, self.log, self.marker, self.project)['reason'],
                             'baseline-renewal-owner-active')
        with patch.object(core, '_taban_komut', side_effect=AssertionError('unsafe renewal ran a command')):
            (self.wt / 'calc.py').write_text('value=2\nold=False\nnew=False\n')
            self.assertEqual(kabul.taban_yenile('a', self.wt, self.log, self.marker, self.project)['status'], 'FAIL')
            self.git('checkout', '--', 'calc.py')
            (self.project / 'kurulum-komut').write_text('echo must-not-repeat')
            self.assertEqual(kabul.taban_yenile('a', self.wt, self.log, self.marker, self.project)['status'], 'FAIL')
            (self.project / 'kurulum-komut').write_text('')
            self.qa_command('echo changed-command')
            self.assertEqual(kabul.taban_yenile('a', self.wt, self.log, self.marker, self.project)['status'], 'FAIL')
        self.assertEqual(self.marker.read_bytes(), original)

    def test_baseline_renewal_failure_restores_the_previous_record_and_output(self):
        import kabul
        self.store.update(self.rid, state='retryable')
        original = self.baseline()
        marker, output = self.marker.read_bytes(), Path(original['qa']['output']).read_bytes()
        with patch.object(core, '_taban_hazirlik', side_effect=RuntimeError('interrupted remeasurement')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted remeasurement'):
                kabul.taban_yenile('a', self.wt, self.log, self.marker, self.project)
        self.assertEqual(self.marker.read_bytes(), marker)
        self.assertEqual(Path(original['qa']['output']).read_bytes(), output)

    def test_parallel_setup_and_baseline_execute_once_and_restart_preserves_evidence(self):
        scratch = self.wt / '.pytest_cache'
        scratch.mkdir()
        setup_count, qa_count = scratch / 'setup-count', scratch / 'qa-count'
        (self.project / 'kurulum-komut').write_text('echo setup >> ' + shlex.quote(str(setup_count)))
        self.qa_command('echo qa >> ' + shlex.quote(str(qa_count)) + '; ' + self.command)
        import izin
        action = izin.setup_proposal(self.store, self.rid, self.wt, (self.project / 'kurulum-komut').read_text())
        with patch.dict(os.environ, CADRE_RUN_ID=''):
            izin.approve(self.store, self.rid, action['token'])
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.baseline())) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=20)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(results), 2)
        self.assertEqual(setup_count.read_text().split(), ['setup'])
        self.assertEqual(qa_count.read_text().split(), ['qa'])
        self.assertEqual(results[0]['context'], results[1]['context'])
        self.assertEqual(self.baseline()['qa'], results[0]['qa'])
        self.assertEqual(qa_count.read_text().split(), ['qa'])

    def test_setup_waits_without_side_effects_until_exact_single_operator_approval(self):
        target = self.wt / '.pytest_cache' / 'setup-proof'
        target.parent.mkdir(exist_ok=True)
        (self.project / 'kurulum-komut').write_text('echo setup > ' + shlex.quote(str(target)))
        result = self.baseline(approve_setup=False)
        self.assertEqual(result['reason'], 'onay-bekliyor')
        self.assertFalse(target.exists())
        self.assertFalse(self.marker.exists())
        token = result['kurulum']['action_token']
        import izin
        with patch.dict(os.environ, CADRE_RUN_ID=''):
            izin.approve(self.store, self.rid, token)
        result = self.baseline(approve_setup=False)
        self.assertEqual(result['kurulum']['status'], 'PASS')
        self.assertEqual(target.read_text(), 'setup\n')
        self.assertEqual(izin.read_action(self.store, self.rid, token)['status'], 'completed')

    def test_disabled_empty_changing_and_malformed_baselines_do_not_claim_acceptance(self):
        with patch.dict(os.environ, {'CADRE_TABAN_QA': '0'}):
            disabled = self.baseline()
        self.assertEqual((disabled['status'], disabled['qa']['reason']), ('FAIL', 'baseline-disabled'))
        self.marker.unlink()
        self.qa_command("printf 'Ran 0 tests\\nOK\\n'")
        self.assertEqual(self.baseline()['qa']['reason'], 'baseline-no-tests')
        self.marker.unlink()
        self.qa_command('echo changed > calc.py')
        self.assertEqual(self.baseline()['qa']['reason'], 'baseline-source-or-environment-changed')
        self.marker.write_text('{broken')
        self.assertEqual(self.baseline()['reason'], 'baseline-record-invalid')
        self.marker.write_text(json.dumps({'status': 'PASS', 'qa': {'status': 'ERROR'}}))
        self.assertEqual(self.baseline()['reason'], 'baseline-record-invalid')
        self.marker.unlink()
        missing = self.home / 'missing-git'
        missing.mkdir()
        # Temporaries may be inside the repo; this negative example must not find the outer repo's .git.
        with patch.dict(os.environ, {'GIT_CEILING_DIRECTORIES': str(self.home)}):
            result = core.taban_hazirlik('a', missing, self.log, self.marker, self.project)
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'baseline-source-unavailable'))
        self.marker.unlink()
        self.git('add', '.')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=f@local', 'commit', '-qm', 'different source')
        self.assertEqual(self.baseline()['reason'], 'baseline-source-mismatch')

    def test_changed_receipt_and_different_qa_scope_stay_unknown(self):
        baseline = self.baseline()
        Path(baseline['qa']['output']).write_text('altered evidence')
        self.assertEqual(self.check()['baseline']['reason'], 'baseline-output-changed')
        self.marker.unlink()
        self.baseline()
        record = self.store.read(self.rid)
        self.store.update(self.rid, feature_acceptance={'id': 'f1', 'qa_komut': self.command.replace('test_product', 'test_product.Product.test_value'), 'riskli': False})
        result = self.check()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['baseline']['reason'], 'different-qa-scope')

    def test_non_code_work_records_source_without_running_setup_or_code_tests(self):
        self.rid = self.store.accept('a', 'TUR: rapor\nRead source and report findings')['run_id']
        self.marker = self.store.path(self.rid) / 'taban-qa.json'
        (self.project / 'kurulum-komut').write_text('exit 91')
        with (patch.dict(os.environ, self.store.environment(self.rid)),
              patch.object(core, '_taban_komut', side_effect=AssertionError('non-code work must not install or run code QA'))):
            baseline = self.baseline()
        self.assertEqual(baseline['reason'], 'non-code-source-baseline')
        self.assertEqual(baseline['qa']['status'], 'NOT_APPLICABLE')
        self.assertTrue(baseline['source']['digest'])
        (self.wt / 'calc.py').write_text('value=2\n')
        with patch.dict(os.environ, self.store.environment(self.rid)):
            restarted = self.baseline()
            self.assertEqual(restarted['applicability'], 'original-source')
            self.assertEqual(restarted['source'], baseline['source'])
            self.assertEqual(json.loads(self.marker.read_text()), baseline)
            self.marker.write_text('{broken')
            self.assertEqual(self.baseline()['reason'], 'baseline-input-unavailable')
        plain = self.home / 'plain-data'
        plain.mkdir()
        (plain / 'values.csv').write_text('1,2,3\n')
        (plain / '.env').write_text('SYNTHETIC_NEVER_READ')
        from sonuc import input_revision
        first = input_revision(plain)
        self.assertEqual(first['files'], 1)
        (plain / '.env').write_text('SYNTHETIC_CHANGED_SECRET')
        self.assertEqual(first, input_revision(plain))
        (plain / 'values.csv').write_text('1,2,4\n')
        self.assertNotEqual(first['digest'], input_revision(plain)['digest'])
        (plain / 'large.csv').write_bytes(b'x' * 2_000_001)
        with self.assertRaisesRegex(ValueError, 'input-source-limit'):
            input_revision(plain)

    def test_mutable_planning_type_cannot_skip_a_code_baseline(self):
        (self.wt / 'calc.py').write_text('value=1\nold=False\nnew=True\n')
        self.git('add', '.')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=f@local', 'commit', '-qm', 'failing baseline')
        self.rid = self.store.accept('a', 'Measure the existing code baseline')['run_id']
        self.marker = self.store.path(self.rid) / 'taban-qa.json'
        for kind in ('review', 'research'):
            with self.subTest(kind=kind), patch.dict(os.environ, self.store.environment(self.rid)):
                self.store.update(self.rid, planning={'job_type': kind})
                baseline = self.baseline()
                self.assertEqual(baseline['qa']['status'], 'FAIL', baseline)
                self.assertNotEqual(baseline['reason'], 'non-code-source-baseline')
                self.assertIn('test_new', Path(baseline['qa']['output']).read_text())
                self.marker.unlink()

    def test_legacy_review_preserves_source_baseline_and_child_readonly_type(self):
        self.rid = self.store.accept('a', 'TUR: inceleme\nInspect the existing source only.')['run_id']
        record = self.store.read(self.rid)
        record.pop('job_type', None)
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        original = (self.store.path(self.rid) / 'run.json').read_bytes()
        self.marker = self.store.path(self.rid) / 'taban-qa.json'
        (self.project / 'kurulum-komut').write_text('exit 91')
        with (patch.dict(os.environ, self.store.environment(self.rid)),
              patch.object(core, '_taban_komut', side_effect=AssertionError('review must not install or run code QA')),
              patch('kosu.calisma_kipi', return_value='uygulama')):
            baseline = self.baseline()
            child = self.store.accept('review-child', 'Implement the generated feature.', parent_run_id=self.rid)
        self.assertEqual(baseline['reason'], 'non-code-source-baseline')
        self.assertEqual(baseline['qa']['status'], 'NOT_APPLICABLE')
        self.assertEqual(child['mode'], 'inceleme')
        self.assertEqual(child['job_type'], 'review')
        self.assertEqual((self.store.path(self.rid) / 'run.json').read_bytes(), original)

    def test_timeout_and_cancellation_reap_baseline_processes_without_success_marker(self):
        import time
        scratch = self.wt / '.pytest_cache'
        scratch.mkdir()
        pidfile = scratch / 'qa-pid'
        script = 'import os,pathlib,time;pathlib.Path(' + repr(str(pidfile)) + ').write_text(str(os.getpid()));time.sleep(20)'
        self.qa_command(shlex.quote(sys.executable) + ' -c ' + shlex.quote(script))
        with patch.dict(os.environ, {'CADRE_QA_TIMEOUT': '.3', 'CADRE_KILL_GRACE': '.1'}):
            result = self.baseline()
        self.assertEqual((result['status'], result['qa']['reason']), ('FAIL', 'baseline-timeout'))
        self.assertFalse(core.pid_yasiyor(int(pidfile.read_text())))
        self.marker.unlink()
        pidfile.unlink()
        errors = []
        def run():
            try: self.baseline()
            except core.CancelledRun as exc: errors.append(type(exc).__name__)
        with patch.dict(os.environ, {'CADRE_KILL_GRACE': '.1'}):
            thread = threading.Thread(target=run)
            thread.start()
            deadline = time.monotonic() + 10
            while not pidfile.exists() and time.monotonic() < deadline: time.sleep(.02)
            self.assertTrue(pidfile.exists())
            (self.store.path(self.rid) / 'cancel.flag').touch()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ['CancelledRun'])
        self.assertFalse(self.marker.exists())
        self.assertFalse(core.pid_yasiyor(int(pidfile.read_text())))


class ProjectBaselineTests(unittest.TestCase):
    def fixture(self):
        from test_proje_uctan_uca import ProjeUctanUcaTest
        f = ProjeUctanUcaTest()
        f.setUp()
        self.addCleanup(f.doCleanups)
        worker = f.home / 'motor/isci.py'
        prefix = '''import os,sys,pathlib
sys.path.insert(0,os.environ['CADRE_ENGINE_ROOT']+'/lib')
import qe_core as core
from kosu import RunStore,taban_qa_kaydet
store=RunStore(); rid=os.environ['CADRE_RUN_ID']; record=store.read(rid)
baseline=core.taban_hazirlik(record['project'],os.environ['CADRE_WORKTREE'],store.path(rid)/'baseline.log',store.path(rid)/'taban-qa.json',store.home/'projects'/record['project'])
taban_qa_kaydet(store,rid,baseline)
if baseline['status']!='PASS': sys.exit(21)
'''
        worker.write_text(prefix + worker.read_text())
        return f

    def test_project_baseline_precedes_first_feature_is_shared_and_final_scope_remains_separate(self):
        import proje
        f = self.fixture()
        marker = f.store.path(f.ana) / 'taban-qa.json'
        def plan(istem, gorev):
            self.assertEqual(json.loads(marker.read_text())['qa']['status'], 'FAIL')
            return f.planla(istem, gorev)
        self.assertEqual(proje.baslat(f.ana, 'p', planla=plan, calistir=f.calistir), 0)
        state = json.loads((f.store.path(f.ana) / 'proje.json').read_text())
        first = state['cocuklar'][0]['kosu']
        child = json.loads((f.store.path(first) / 'taban-qa.json').read_text())
        self.assertEqual(child['reused_from'], f.ana)
        self.assertEqual(child['qa']['status'], 'FAIL')
        self.assertEqual(f.store.read(first)['state'], 'implemented')
        self.assertEqual(f.store.read(state['cocuklar'][-1]['kosu'])['state'], 'completed')
        self.assertEqual(f.store.read(f.ana)['final_result']['status'], 'PASS')
        self.assertEqual(json.loads(marker.read_text())['qa']['status'], 'FAIL')
        self.assertEqual(f.store.read(f.ana)['baseline_warning']['status'], 'FAIL')

    def test_project_setup_failure_stops_before_planning_and_keeps_original_repo_unchanged(self):
        import proje
        f = self.fixture()
        (f.home / 'projects/p/kurulum-komut').write_text('exit 7')
        import izin
        source = f.store.path(f.ana) / 'baseline-source'
        subprocess.run(['git', '-C', str(f.repo), 'worktree', 'add', '--detach', str(source), f.taban],
                       capture_output=True, check=True)
        action = izin.setup_proposal(f.store, f.ana, source, 'exit 7')
        with patch.dict(os.environ, CADRE_RUN_ID=''):
            izin.approve(f.store, f.ana, action['token'])
        plan = unittest.mock.Mock(side_effect=AssertionError('failed setup must precede planner'))
        self.assertEqual(proje.baslat(f.ana, 'p', planla=plan, calistir=f.calistir), 1)
        plan.assert_not_called()
        self.assertEqual(f.store.read(f.ana)['reason'], 'kurulum-hatasi')
        self.assertEqual(subprocess.check_output(['git', '-C', str(f.repo), 'rev-parse', 'HEAD'], text=True).strip(), f.taban)
        self.assertFalse((f.repo / 'urun.txt').exists())
