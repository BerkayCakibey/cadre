from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import qe_core as core
from qa_bakim import authorize
from sonuc import approved, revision

CHECKS = '''import unittest

from calc import twice
class Checks(unittest.TestCase):
 def test_positive(self): self.assertEqual(twice(2),4)
 def test_negative(self):
  with self.assertRaises(ValueError): twice(-1)
 def test_boundary(self): self.assertEqual(twice(0),0)
'''


class QaCoverageTests(unittest.TestCase):
    def setUp(self):
        from test_sonuc import CompletionTests
        self.f = CompletionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.wt, self.store = self.f.wt, self.f.store
        (self.wt / 'calc.py').write_text('def twice(value):\n if value<0: raise ValueError(value)\n return value*2\n')
        (self.wt / 'test_checks.py').write_text(CHECKS)
        self.command = shlex.quote(sys.executable) + ' -B -m unittest -q test_checks'
        self.set_command(self.command)

    def set_command(self, command):
        Path(self.f.run['constraints']['qa-komut']).write_text(command)

    def check(self):
        return core.qa_kapisi('a', str(self.wt), self.f.log, '/forbidden/provider', model='claude/fixture')

    def pytest_command(self):
        python = os.environ.get('CADRE_TEST_PYTEST_PYTHON', sys.executable)
        available = subprocess.run([python, '-c', 'import pytest'], capture_output=True).returncode == 0
        self.assertTrue(available, 'pytest required: set CADRE_TEST_PYTEST_PYTHON; missing dependency is not SKIP')
        return shlex.quote(python) + ' -B -m pytest -q'

    def source(self):
        self.f.git('add', '.')
        self.f.git('-c', 'user.name=fixture', '-c', 'user.email=f@local', 'commit', '-qm', 'checks source')
        self.f.base = self.f.git('rev-parse', 'HEAD').strip()
        self.f.run = self.store.accept('a', 'Preserve twice positive, rejection and zero behavior', source_sha=self.f.base)
        self.f.rid = self.f.run['run_id']
        self.store.update(self.f.rid, worktree=str(self.wt))
        self.f.enter(patch.dict(os.environ, self.store.environment(self.f.rid)))
        self.set_command(self.command)

    def finalize(self, verdict='GECER'):
        with patch.object(core, 'capture_opencode', side_effect=self.f.review_for_prompt(verdict)), \
             patch.object(core, 'urunu_yazan_modeller', return_value={'fixture/writer'}):
            return core.finalize_run('a', str(self.wt), self.f.log, '/forbidden/provider', review_model='fixture/reviewer')

    def maintenance(self):
        ids = ['test_replacement.Checks.test_' + name for name in ('positive', 'negative', 'boundary')]
        return {'source_sha': self.f.base, 'revision': revision(self.wt), 'command': self.command,
                'reason': 'Duplicate suite replaced while preserving positive, rejection and zero criteria',
                'changes': [{'path': 'test_checks.py', 'purpose': 'twice positive, rejection and zero behavior',
                             'preserved_criterion': 'main: Preserve twice positive, rejection and zero behavior',
                             'replacement': ids, 'positive': ids[:1], 'negative': ids[1:2], 'boundary': ids[2:]}]}

    def prepare_maintenance(self):
        self.source()
        marker = self.store.path(self.f.rid) / 'taban-qa.json'
        baseline = core.taban_hazirlik('a', self.wt, self.f.log, marker, self.f.project)
        self.assertEqual(baseline['qa']['status'], 'PASS')
        (self.wt / 'test_checks.py').unlink()
        (self.wt / 'test_replacement.py').write_text(CHECKS)
        self.command = self.command.replace('test_checks', 'test_replacement')
        self.set_command(self.command)
        return self.maintenance()

    def test_unittest_records_actual_test_ids_counts_and_full_scope(self):
        result = self.check()
        self.assertEqual(result['status'], 'PASS', result)
        proof = result['discovery']
        self.assertEqual((proof['collected'], proof['executed'], proof['skipped']), (3, 3, 0))
        self.assertEqual(proof['scope'], 'full')
        self.assertEqual(proof['observed_files'], ['test_checks.py'])
        self.assertIn('test_checks.Checks.test_negative', proof['passed'])

    def local_profile(self, body='import unittest\n@unittest.skip("explicit live phase")\nclass Live(unittest.TestCase):\n def test_live(self): self.fail("live endpoint not called")\n'):
        import hashlib

        from kosu import atomic_json
        live = self.wt / 'test_live.py'
        live.write_text(body)
        profile = {'version': 1, 'scope': 'local', 'deferred': [
            {'path': 'test_live.py', 'sha256': hashlib.sha256(live.read_bytes()).hexdigest(),
             'reason': 'Requires the separately authorized live network phase'}]}
        atomic_json(self.f.project / 'qa-profile.json', profile)
        self.command = shlex.quote(sys.executable) + ' -B -m unittest -q test_checks test_live'
        self.source()
        return profile, live

    def test_local_profile_keeps_declared_live_skips_visible_and_required_tests_executed(self):
        profile, _ = self.local_profile()
        (self.f.project / 'qa-profile.json').unlink()
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.store.update(self.f.rid, qa_profile=None)
        child = self.store.accept('child', 'Nested feature', parent_run_id=self.f.rid)
        self.assertEqual(child['qa_profile'], profile)
        result = self.check()
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual((result['reason'], result['acceptance_scope'], result['deferred_tests']),
                         ('qa-local-scope-live-tests-deferred', 'local', 1))
        proof = result['discovery']
        self.assertEqual((proof['collected'], proof['executed'], proof['skipped'], proof['deferred'], proof['required_skipped']), (4, 4, 1, 1, 0))
        self.assertEqual(proof['acceptance_scope'], 'local')
        self.assertEqual(proof['observed_files'], ['test_checks.py', 'test_live.py'])

    def test_local_profile_never_defers_an_unregistered_or_changed_test(self):
        _, live = self.local_profile()
        (self.wt / 'test_checks.py').write_text(CHECKS.replace(' def test_negative', ' @unittest.skip("required")\n def test_negative'))
        self.assertEqual(self.check()['discovery']['reason'], 'qa-required-test-skipped')
        (self.wt / 'test_checks.py').write_text(CHECKS)
        live.write_text(live.read_text() + '# Changed after the operator profile was frozen.\n')
        self.assertEqual(self.check()['discovery']['reason'], 'qa-profile-source-changed')

    def test_local_profile_excludes_only_operator_bound_unchanged_archive_tests(self):
        import hashlib

        from kosu import atomic_json
        profile, _ = self.local_profile()
        archive = self.wt / 'test_archive.py'
        body = 'import unittest\nclass Old(unittest.TestCase):\n def test_old(self): self.fail("archival diagnostic")\n'
        archive.write_text(body)
        profile['excluded'] = [{'path': 'test_archive.py', 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                                'reason': 'Historical diagnostic; outside the current native acceptance phase'}]
        atomic_json(self.f.project / 'qa-profile.json', profile)
        self.source()
        result = self.check()
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['discovery']['excluded_files'], {'test_archive.py': profile['excluded'][0]['reason']})
        self.assertIn('test_archive.py', result['discovery']['inventory_files'])
        archive.write_text(body + '# changed\n')
        self.assertEqual(self.check()['discovery']['reason'], 'qa-profile-source-changed')
        archive.write_text(body)
        (self.wt / 'test_hidden.py').write_text('def test_uncollected(): assert False\n')
        self.assertEqual(self.check()['discovery']['reason'], 'qa-tests-not-discovered')

    def test_local_profile_cannot_hide_missing_collection_failures_or_zero_executed_tests(self):
        self.local_profile()
        self.set_command(self.command.replace(' test_live', ''))
        self.assertEqual(self.check()['discovery']['reason'], 'qa-tests-not-discovered')
        self.set_command(shlex.quote(sys.executable) + ' -B -m unittest -q test_live')
        self.assertNotEqual(self.check()['status'], 'PASS')
        self.local_profile('import unittest\nclass Live(unittest.TestCase):\n def test_live(self): self.fail("real failure")\n')
        self.assertEqual(self.check()['discovery']['reason'], 'qa-test-failed')

    def test_pytest_testpaths_omission_fails_real_gate_and_blocks_review(self):
        command = self.pytest_command()
        (self.wt / 'tests').mkdir()
        (self.wt / 'tests/test_unit.py').write_text('def test_unit(): assert 2*2==4\n')
        (self.wt / 'pyproject.toml').write_text('[tool.pytest.ini_options]\ntestpaths=["tests"]\n')
        self.set_command(command)
        with patch.object(core, 'capture_opencode') as reviewer:
            result = core.finalize_run('a', str(self.wt), self.f.log, '/forbidden/provider', review_model='fixture/reviewer')
            reviewer.assert_not_called()
        self.assertEqual(result['status'], 'FAIL', result)
        self.assertEqual(result['qa']['reason'], 'qa-tests-not-discovered')
        self.assertIn('test_checks.py', result['qa']['discovery']['missing_files'])
        self.assertFalse(approved(self.store.read(self.f.rid)))

    def test_pytest_unit_integration_and_parameterized_tests_run_together(self):
        self.set_command(self.pytest_command())
        (self.wt / 'integration').mkdir()
        (self.wt / 'integration/test_integration.py').write_text('import pytest\nfrom calc import twice\n'
            '@pytest.mark.parametrize("value",[0,2])\ndef test_roundtrip(value): assert twice(value)==value*2\n')
        result = self.check()
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['discovery']['executed'], 5)
        self.assertEqual(result['discovery']['observed_files'], ['integration/test_integration.py', 'test_checks.py'])
        (self.wt / 'pyproject.toml').write_text('[tool.pytest.ini_options]\npython_files=["checks_*.py"]\n')
        self.assertNotEqual(self.check()['status'], 'PASS')

    def test_explicit_test_paths_in_the_approved_command_bound_discovery_and_changed_tests_outside_them_fail(self):
        command = self.pytest_command()
        (self.wt / 'tests').mkdir()
        (self.wt / 'tests/test_unit.py').write_text('from calc import twice\ndef test_unit(): assert twice(2)==4\n')
        (self.wt / 'tests/test_more.py').write_text('from calc import twice\ndef test_more(): assert twice(0)==0\n')
        self.command = command + ' tests/test_unit.py'
        self.source()
        result = self.check()
        self.assertEqual((result['status'], result['discovery']['scope']), ('PASS', 'targeted'), result)
        self.assertEqual(result['discovery']['expected_files'], ['tests/test_unit.py'])
        self.assertEqual(result['discovery']['observed_files'], ['tests/test_unit.py'])
        self.assertNotIn('uncovered_changed_tests', result['discovery'])
        (self.wt / 'tests/test_more.py').write_text('from calc import twice\ndef test_more(): assert twice(1)==2\n')
        result = self.check()
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'qa-changed-tests-not-covered'), result)
        self.assertEqual(result['discovery']['uncovered_changed_tests'], ['tests/test_more.py'])
        self.assertFalse(approved(self.store.read(self.f.rid)))
        self.command = command + ' tests'
        self.set_command(self.command)
        result = self.check()
        self.assertEqual((result['status'], result['discovery']['scope']), ('PASS', 'targeted'), result)
        self.assertEqual(result['discovery']['observed_files'], ['tests/test_more.py', 'tests/test_unit.py'])
        self.command = command + ' -o addopts= tests/missing.py'
        self.set_command(self.command)
        result = self.check()
        self.assertEqual(result['discovery']['scope'], 'full', 'var olmayan yol beklentiyi daraltmaz')
        self.assertNotEqual(result['status'], 'PASS')

    def test_targeted_scope_cannot_omit_new_or_changed_test_methods(self):
        self.source()
        target = self.command + '.Checks.test_positive'
        self.store.update(self.f.rid, feature_acceptance={'id': 'f1', 'qa_komut': target, 'riskli': False})
        result = self.check()
        self.assertEqual((result['status'], result['discovery']['scope']), ('PASS', 'targeted'))
        (self.wt / 'test_checks.py').write_text(CHECKS + '\nclass New(unittest.TestCase):\n def test_positive(self): self.assertEqual(twice(3),6)\n')
        result = self.check()
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'qa-tests-not-discovered'))

    def test_unknown_syntax_lint_and_zero_discovery_never_pass(self):
        for command in ('true', 'echo "3 passed"', shlex.quote(sys.executable) + ' -m py_compile calc.py',
                        shlex.quote(sys.executable) + ' -m unittest discover -q -p "missing_*.py"'):
            with self.subTest(command=command):
                self.set_command(command)
                result = self.check()
                self.assertIn(result['status'], ('FAIL', 'ERROR'), result)
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.store.update(self.f.rid, rapor={'kind': 'research'})
        self.store.update(self.f.rid, qa={'kind': 'rapor'})
        self.set_command(self.command)
        result = self.check()
        self.assertEqual((result['status'], result['kind']), ('PASS', 'project'), result)

    def test_partial_skip_xfail_and_hidden_failure_are_not_success(self):
        for replacement, suffix in [(CHECKS.replace(' def test_positive', " @unittest.skip('required')\n def test_positive"), ''),
                                    (CHECKS.replace('twice(2),4', 'twice(2),99'), ' || true')]:
            with self.subTest(suffix=suffix):
                (self.wt / 'test_checks.py').write_text(replacement)
                self.set_command(self.command + suffix)
                result = self.check()
                self.assertEqual(result['status'], 'FAIL', result)
                self.assertIn(result['reason'], ('qa-required-test-skipped', 'qa-test-failed'))
        (self.wt / 'test_checks.py').unlink()
        (self.wt / 'test_xfail.py').write_text('import pytest\n@pytest.mark.xfail(reason="required")\ndef test_required(): assert False\n')
        self.set_command(self.pytest_command())
        self.assertEqual(self.check()['status'], 'FAIL')

    def test_source_mutation_timeout_and_corrupt_evidence_stay_unaccepted(self):
        (self.wt / 'test_checks.py').write_text(CHECKS.replace('self.assertEqual(twice(0),0)',
            'self.assertEqual(twice(0),0); __import__("pathlib").Path("calc.py").write_text("changed=1\\n")'))
        result = self.check()
        self.assertEqual((result['status'], result['reason']), ('ERROR', 'source-changed-during-qa'))
        (self.wt / 'test_checks.py').write_text(CHECKS)
        (self.wt / 'calc.py').write_text('def twice(value):\n if value<0: raise ValueError(value)\n return value*2\n')
        self.set_command(self.command + '; printf broken > "$CADRE_QA_EVIDENCE"')
        self.assertEqual(self.check()['reason'], 'qa-discovery-unverified')
        self.set_command(self.command + ' && sleep 2')
        with patch.dict(os.environ, {'CADRE_QA_TIMEOUT': '.1'}):
            self.assertNotEqual(self.check()['status'], 'PASS')

    def test_node_test_runner_contributes_real_test_file_and_results(self):
        (self.wt / 'checks.test.mjs').write_text("import test from 'node:test'; import assert from 'node:assert/strict';\n"
            "test('positive',()=>assert.equal(2*2,4)); test('negative',()=>assert.throws(()=>JSON.parse('invalid')));\n")
        self.set_command(self.command + ' && node --test checks.test.mjs')
        result = self.check()
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['discovery']['executed'], 5)
        self.assertIn('checks.test.mjs', result['discovery']['observed_files'])
        (self.wt / 'empty.test.mjs').write_text('')
        self.set_command(self.command + ' && node --test checks.test.mjs empty.test.mjs')
        self.assertNotEqual(self.check()['status'], 'PASS')
        (self.wt / 'empty.test.mjs').unlink()
        (self.wt / 'checks.test.mjs').write_text("import test from 'node:test'; import assert from 'node:assert/strict';\n"
            "test('positive',()=>assert.equal(2*2,4)); test.describe.skip('required suite',()=>test('needed',()=>assert.equal(1,1)));\n")
        self.set_command(self.command + ' && node --test checks.test.mjs')
        self.assertEqual(self.check()['status'], 'FAIL')

    def test_type_quality_preserves_the_existing_venv_option_and_checks_the_changed_file(self):
        from qa_kaniti import quality_command
        python = str(Path(sys.executable))
        venv_root = self.wt / 'existing environment'
        command = shlex.join([python, '-m', 'basedpyright', '--venvpath', str(venv_root), 'calc.py'])
        _, plan = quality_command(command, self.wt, self.wt / 'evidence.jsonl', ['calc.py'])
        check = plan['checks']['type']
        self.assertTrue(check['supported'])
        self.assertEqual(check['paths'], ['calc.py'])
        self.assertEqual(check['scope_command'],
                         [python, '-m', 'basedpyright', '--venvpath', str(venv_root), str((self.wt / 'calc.py').resolve())])
        for args in (['--venvpath'], ['--venvpath', str(venv_root), '--unknown', 'calc.py'],
                     ['--venvpath', str(venv_root)]):
            with self.subTest(args=args):
                _, invalid = quality_command(shlex.join([python, '-m', 'basedpyright', *args]),
                                             self.wt, self.wt / 'evidence.jsonl', ['calc.py'])
                self.assertFalse(invalid['checks']['type']['supported'])
                self.assertEqual(invalid['checks']['type']['scope_command'], [])

    def test_quality_directory_omission_and_missing_configured_check_fail(self):
        (self.wt / 'pyproject.toml').write_text('[tool.ruff]\nline-length=100\n')
        self.assertEqual(self.check()['reason'], 'qa-quality-unverified')
        tools = self.wt / '.venv/bin'
        tools.mkdir(parents=True)
        ruff = tools / 'ruff'
        ruff.write_text('#!' + sys.executable + '\nimport sys\nassert sys.argv[1]=="check"\n')
        ruff.chmod(0o700)
        (self.wt / 'src').mkdir()
        for scope, expected in [('src', 'FAIL'), ('.', 'PASS')]:
            self.set_command(self.command + ' && ' + shlex.quote(str(ruff)) + ' check ' + scope)
            result = self.check()
            self.assertEqual(result['status'], expected, result)
        (self.wt / 'pyproject.toml').write_text('[tool.ruff]\nexclude=["test_checks.py"]\n')
        self.assertEqual(self.check()['reason'], 'qa-quality-exclusions-unverified')

    def real_quality_command(self, lint_scope='.'):
        python = os.environ.get('CADRE_TEST_PYTEST_PYTHON', sys.executable)
        available = subprocess.run([python, '-c', 'import ruff, basedpyright'], capture_output=True, timeout=20)
        self.assertEqual(available.returncode, 0,
                         'Ruff/BasedPyright required in CADRE_TEST_PYTEST_PYTHON; missing dependency is not SKIP')
        (self.wt / 'calc.py').write_text('def twice(value: int) -> int:\n'
            '    if value < 0:\n        raise ValueError(value)\n    return value * 2\n')
        (self.wt / 'pyproject.toml').write_text('[tool.ruff]\nline-length=100\n'
            '[tool.ruff.format]\n[tool.basedpyright]\ntypeCheckingMode="standard"\n')
        subprocess.run([python, '-m', 'ruff', 'format', str(self.wt)],
                       check=True, capture_output=True, timeout=20)
        command = self.command + ' && ' + shlex.join([python, '-m', 'ruff', 'check', lint_scope])
        command += ' && ' + shlex.join([python, '-m', 'ruff', 'format', '--check', '.'])
        command += ' && ' + shlex.join([python, '-m', 'basedpyright', '.'])
        self.set_command(command)
        return command

    def test_real_quality_tools_accept_clean_code_and_reject_scope_and_quality_errors(self):
        for variant in ('clean', 'lint-scope', 'lint-failure', 'format-failure',
                        'type-failure', 'default-exclusion'):
            with self.subTest(variant=variant):
                fixture = QaCoverageTests()
                fixture.setUp()
                try:
                    fixture.real_quality_command('src' if variant == 'lint-scope' else '.')
                    (fixture.wt / 'src').mkdir()
                    (fixture.wt / 'src/module.py').write_text('VALUE = 1\n')
                    if variant in ('lint-failure', 'default-exclusion'):
                        target = fixture.wt / 'lint_example.py'
                        if variant == 'default-exclusion':
                            (fixture.wt / '.venv').mkdir()
                            target = fixture.wt / '.venv/lint_example.py'
                        target.write_text('VALUE = undefined_name\n')
                        if variant == 'default-exclusion':
                            python = os.environ.get('CADRE_TEST_PYTEST_PYTHON', sys.executable)
                            unscoped = subprocess.run([python, '-m', 'ruff', 'check', '.'],
                                cwd=fixture.wt, capture_output=True, text=True, timeout=20)
                            self.assertEqual(unscoped.returncode, 0, unscoped.stdout + unscoped.stderr)
                    if variant == 'format-failure':
                        (fixture.wt / 'format_example.py').write_text('VALUE =  1\n')
                    if variant == 'type-failure':
                        (fixture.wt / 'type_example.py').write_text('def wrong() -> int:\n    return "wrong"\n')
                    before = revision(fixture.wt)
                    with patch.object(core, 'capture_opencode') as provider:
                        result = fixture.check()
                        provider.assert_not_called()
                    self.assertEqual(result['status'], 'PASS' if variant == 'clean' else 'FAIL', result)
                    self.assertEqual(revision(fixture.wt), before)
                    self.assertEqual(result['discovery']['executed'], 3)
                    if variant == 'clean':
                        self.assertEqual(result['quality']['status'], 'PASS', result)
                        self.assertEqual(set(result['quality']['required']), {'lint', 'format', 'type'})
                        for check in result['quality']['checks'].values():
                            receipt = json.loads(Path(check['output']).read_text())
                            self.assertEqual((receipt['exit_code'], receipt['scope_exit_code']), (0, 0))
                    if variant == 'lint-scope':
                        self.assertEqual(result['reason'], 'qa-quality-scope-missing', result)
                    diagnostics = {'lint-failure': 'F821', 'format-failure': 'reformat',
                                   'type-failure': 'reportReturnType', 'default-exclusion': 'F821'}
                    if variant in diagnostics:
                        output = Path(result['attempts'][-1]['output']).read_text()
                        self.assertIn(diagnostics[variant], output.lower() if variant == 'format-failure' else output)
                        self.assertEqual(result['quality']['status'], 'FAIL', result)
                        self.assertEqual(result['quality']['reason'], 'qa-quality-scope-failed'
                                         if variant == 'default-exclusion' else 'qa-quality-failed', result)
                    if variant == 'default-exclusion':
                        self.assertEqual(result['quality']['reason'], 'qa-quality-scope-failed', result)
                finally:
                    fixture.doCleanups()

    def test_quality_filters_distinguish_packaging_and_cache_from_source_exclusion(self):
        self.real_quality_command()
        config = self.wt / 'pyproject.toml'
        config.write_text(config.read_text() + 'include=["."]\nexclude=["**/__pycache__"]\n'
                          '[tool.hatch.build.targets.sdist]\ninclude=["calc.py"]\n')
        result = self.check()
        self.assertEqual(result['quality']['status'], 'PASS', result)
        from qa_kaniti import quality_result
        baseline = config.read_text()
        for settings in ('exclude=["calc.py"]', 'include=["src"]'):
            with self.subTest(settings=settings):
                changed = baseline.replace('exclude=["**/__pycache__"]', settings) if settings.startswith('exclude') else baseline.replace('include=["."]', settings)
                config.write_text(changed)
                rejected = quality_result(self.wt, result['quality'], ['calc.py'])
                self.assertEqual(rejected['status'], 'UNKNOWN', rejected)
                self.assertEqual(rejected['reason'], 'qa-quality-exclusions-unverified')
        config.write_text(baseline.replace('[tool.ruff.format]\n', '[tool.ruff.format]\nexclude=["calc.py"]\n'))
        self.assertEqual(quality_result(self.wt, result['quality'], ['calc.py'])['status'], 'UNKNOWN')
        config.write_text(baseline)

    def test_real_quality_receipt_changes_invalidate_final_acceptance(self):
        command = self.real_quality_command()
        result = self.finalize()
        self.assertEqual(result['status'], 'PASS', result)
        record = self.store.read(self.f.rid)
        self.assertTrue(approved(record))
        for kind in ('lint', 'format', 'type'):
            with self.subTest(kind=kind):
                artifact = Path(result['qa']['quality']['checks'][kind]['output'])
                original = artifact.read_bytes()
                artifact.write_text('{}')
                self.assertFalse(approved(record))
                artifact.write_bytes(original)
                self.assertTrue(approved(record))
        self.set_command(command + ' && true')
        self.assertFalse(approved(record))

    def test_unauthorized_deletion_and_free_text_do_not_authorize_maintenance(self):
        self.prepare_maintenance()
        result = self.finalize()
        self.assertEqual(result['status'], 'FAIL', result)
        self.assertEqual(result['qa']['reason'], 'baseline-tests-removed')
        self.assertEqual(result['qa']['baseline']['removed'], ['test_checks.py'])
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.store.update(self.f.rid, test_maintenance={'origin': 'operator'})

    def test_authorized_maintenance_is_bound_to_real_replacements_and_review(self):
        value = self.prepare_maintenance()
        accepted = authorize(self.store, self.f.rid, value)
        self.assertEqual(authorize(self.store, self.f.rid, value), accepted)
        result = self.finalize()
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['qa']['test_maintenance']['status'], 'PASS')
        self.assertIn('authorized_test_maintenance', [c['id'] for c in result['review']['acceptance']])
        self.assertTrue(approved(self.store.read(self.f.rid)))

    def test_missing_negative_replacement_or_changed_command_revision_rejects_maintenance(self):
        value = self.prepare_maintenance()
        invalid = json.loads(json.dumps(value))
        invalid['changes'][0]['negative'] = []
        with self.assertRaisesRegex(ValueError, 'evidence-required'):
            authorize(self.store, self.f.rid, invalid)
        invalid = json.loads(json.dumps(value))
        invalid['changes'][0]['negative'] = invalid['changes'][0]['positive']
        with self.assertRaisesRegex(ValueError, 'negative-evidence-required'):
            authorize(self.store, self.f.rid, invalid)
        authorize(self.store, self.f.rid, value)
        self.set_command(self.command + ' && true')
        self.assertEqual(self.check()['reason'], 'test-maintenance-unverified')
        self.set_command(self.command)
        (self.wt / 'calc.py').write_text((self.wt / 'calc.py').read_text() + '\nextra=1\n')
        self.assertEqual(self.check()['reason'], 'test-maintenance-unverified')

    def test_missing_replacement_test_and_criterion_losing_review_cannot_pass(self):
        value = self.prepare_maintenance()
        value['changes'][0]['replacement'].append('missing.test')
        authorize(self.store, self.f.rid, value)
        self.assertEqual(self.check()['reason'], 'test-maintenance-unverified')
        (self.wt / 'calc.py').write_text((self.wt / 'calc.py').read_text() + '\nextra=1\n')
        value = self.maintenance()
        authorize(self.store, self.f.rid, value)
        result = self.finalize('DUZELT')
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'review-not-passed'))

    def test_cli_maintenance_race_owner_and_idempotent_restart(self):
        from kosu import FileLock
        value = self.prepare_maintenance()
        env = {k: v for k, v in os.environ.items() if not k.startswith('CADRE_')}
        env['CADRE_HOME'] = str(self.f.home)
        args = [sys.executable, str(Path(core.__file__).with_name('kosu.py')), 'test-maintenance', self.f.rid]
        def call():
            return subprocess.run(args, input=json.dumps(value), text=True, capture_output=True, env=env)
        with FileLock(self.store.path(self.f.rid) / '.owner.lock'):
            self.assertEqual(call().returncode, 78)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: call(), range(4)))
        self.assertEqual([r.returncode for r in results], [0]*4, [r.stderr for r in results])
        self.assertEqual(len({json.dumps(json.loads(r.stdout), sort_keys=True) for r in results}), 1)
        conflict = {**value, 'reason': 'Conflicting decision for the same revision'}
        with self.assertRaisesRegex(ValueError, 'decision-conflict'):
            authorize(self.store, self.f.rid, conflict)

    def test_changed_test_evidence_and_command_invalidate_final_acceptance(self):
        result = self.finalize()
        self.assertEqual(result['status'], 'PASS', result)
        record = self.store.read(self.f.rid)
        self.assertTrue(approved(record))
        legacy = json.loads(json.dumps(record))
        legacy['final_result']['qa'].pop('discovery')
        self.assertFalse(approved(legacy))
        legacy['final_result']['qa']['kind'] = 'rapor'
        self.assertFalse(approved(legacy))
        relabeled = json.loads(json.dumps(record))
        relabeled['final_result']['qa']['kind'] = 'rapor'
        self.assertFalse(approved(relabeled))
        artifact = Path(result['qa']['attempts'][-1]['evidence'])
        original = artifact.read_bytes()
        artifact.write_text('broken evidence')
        self.assertFalse(approved(record))
        artifact.write_bytes(original)
        self.set_command(self.command + ' && true')
        self.assertFalse(approved(record))
        self.set_command(self.command)
        self.assertTrue(approved(record))
        from sonuc import decision
        qa = {**result['qa'], 'discovery': {**result['qa']['discovery'], 'scope': 'targeted'}}
        self.assertEqual(decision(qa, result['review'], result['revision'])['reason'], 'full-qa-required')

    def bagli_kapsam(self, files, tests, *, onayda=True):
        """Typed request + two approved actions: the acceptance scope is bound to the request/approval before start (CR-35 user decision 20.10)."""
        request = {'job_type': 'code', 'duration_minutes': 40, 'budget_usd': '2', 'output': 'calisan test',
                   'acceptance': 'twice davranisi korunur', 'scope_files': files, 'scope_tests': tests}
        action = {'request_hash': 'r1', 'duration_minutes': 40, 'budget_usd': '2'}
        if onayda:
            action['acceptance_scope'] = {'files': files, 'tests': tests}
        from kosu import atomic_json
        record = self.store.read(self.f.rid)
        record.update(request=request, request_input_hash='r1', request_permission={'status': 'approved', 'token': 't', 'action': action})
        atomic_json(self.store.path(self.f.rid) / 'run.json', record)
        from test_sonuc import tam_surec_kayitlari
        tam_surec_kayitlari(self.store, self.f.rid, self.wt, self.f.base, carried=False)

    def hedefli(self, verdict='GECER'):
        self.istemler = []
        hakem = self.f.review_for_prompt(verdict)

        def yakala(real, wt, args, timeout, env=None, role='kalite'):
            self.istemler.append(args[-1])
            return hakem(real, wt, args, timeout, env, role)

        with patch('qa_kaniti.test_scope', return_value={'test_checks.py'}), \
             patch.object(core, 'capture_opencode', side_effect=yakala), \
             patch.object(core, 'urunu_yazan_modeller', return_value={'fixture/writer'}):
            return core.finalize_run('a', str(self.wt), self.f.log, '/forbidden/provider', review_model='fixture/reviewer')

    def test_request_bound_scope_ignores_files_carried_from_the_pinned_source(self):
        self.source()
        ozgun = (self.wt / 'calc.py').read_text()
        (self.wt / 'calc.py').write_text(ozgun + '# tasinan commitsiz satir\n')
        yama = subprocess.run(['git', '-C', str(self.wt), 'diff', '--binary', '--no-ext-diff', 'HEAD', '--'],
                              capture_output=True, check=True).stdout
        kaynak = self.f.home / 'tasinan-kaynak'
        kaynak.mkdir()
        (kaynak / 'degisiklik.patch').write_bytes(yama)
        record = self.store.read(self.f.rid)
        self.store.update(self.f.rid, source={**(record.get('source') or {}), 'carried': True, 'patch': str(kaynak / 'degisiklik.patch')})
        (self.wt / 'test_checks.py').write_text(CHECKS + ' def test_four(self): self.assertEqual(twice(4),8)\n')
        self.bagli_kapsam(['test_checks.py'], ['test_checks.py'])
        result = self.hedefli()
        self.assertEqual(result['status'], 'PASS', result)
        self.assertTrue(approved(self.store.read(self.f.rid)), 'tasinan kaynak satiri urun degildir')
        (self.wt / 'calc.py').write_text('def twice(value):\n if value<0: raise ValueError(value)\n return value+value\n# tasinan commitsiz satir\n')
        result = self.hedefli()
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'scope-files-exceeded'), result)
        self.assertEqual(result['out_of_scope'], ['calc.py'], 'tasinan yamanin otesindeki degisiklik urundur')

    def test_request_bound_scope_accepts_targeted_qa_and_rejects_out_of_scope_change(self):
        self.source()
        (self.wt / 'test_checks.py').write_text(CHECKS + ' def test_four(self): self.assertEqual(twice(4),8)\n')
        self.bagli_kapsam(['test_checks.py'], ['test_checks.py'])
        result = self.hedefli()
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['qa']['discovery']['scope'], 'targeted')
        self.assertEqual(result['scope']['files'], ['test_checks.py'])
        record = self.store.read(self.f.rid)
        self.assertTrue(approved(record))
        istem = [p for p in self.istemler if 'MOTOR KANITI' in p][-1]
        self.assertIn('istek kapsami (onayli): izinli dosyalar test_checks.py; gerekli testler test_checks.py', istem)
        (self.wt / 'calc.py').write_text('def twice(value):\n if value<0: raise ValueError(value)\n return value+value\n')
        self.assertFalse(approved(self.store.read(self.f.rid)), 'kapsam disi degisiklik kayitli kabulu de dusurur')
        result = self.hedefli()
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'scope-files-exceeded'), result)
        self.assertEqual(result['out_of_scope'], ['calc.py'])

    def test_scope_declared_only_in_the_request_but_not_in_the_approval_is_not_bound(self):
        self.source()
        (self.wt / 'test_checks.py').write_text(CHECKS + ' def test_four(self): self.assertEqual(twice(4),8)\n')
        self.bagli_kapsam(['test_checks.py'], ['test_checks.py'], onayda=False)
        result = self.hedefli()
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'full-qa-required'), result)
        self.assertFalse(approved(self.store.read(self.f.rid)))

    def test_bound_scope_requires_the_named_tests_to_run_and_the_reviewer_to_pass(self):
        self.source()
        (self.wt / 'test_checks.py').write_text(CHECKS + ' def test_four(self): self.assertEqual(twice(4),8)\n')
        self.bagli_kapsam(['test_checks.py'], ['test_checks.py', 'test_other.py'])
        result = self.hedefli()
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'scope-tests-not-run'), result)
        self.assertEqual(result['missing_tests'], ['test_other.py'])
        self.bagli_kapsam(['test_checks.py'], ['test_checks.py'])
        result = self.hedefli('DUZELT')
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'review-not-passed'), result)
        self.assertFalse(approved(self.store.read(self.f.rid)))

    def test_common_runner_rejects_zero_skip_uncollected_method_and_module(self):
        repo = self.f.home / 'gate'
        tests, library = repo / 'engine/tests', repo / 'engine/lib'
        tests.mkdir(parents=True)
        library.mkdir()
        source = Path(__file__).resolve().parent
        shutil.copyfile(source / 'paralel_kos.py', tests / 'paralel_kos.py')
        for name in ('qa_kaniti.py', 'qa-sim/sitecustomize.py'):
            (library / name).parent.mkdir(exist_ok=True)
            shutil.copyfile(source.parent / 'lib' / name, library / name)
        env = {k: v for k, v in os.environ.items() if not k.startswith('CADRE_')}
        cases = [('import unittest\nclass Empty(unittest.TestCase): pass\n', 'FAILED'),
                 ("import unittest\nclass T(unittest.TestCase):\n @unittest.skip('required')\n def test_needed(self): self.assertEqual(1,1)\n", 'FAILED'),
                 ('import unittest\nclass T(unittest.TestCase):\n def test_run(self): self.assertEqual(1,1)\ndef test_omitted(): assert False\n', 'qa-tests-not-discovered')]
        for code, reason in cases:
            with self.subTest(reason=reason):
                (tests / 'test_gate.py').write_text(code)
                result = subprocess.run([sys.executable, str(tests / 'paralel_kos.py')], env=env, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(reason, result.stdout + result.stderr)
        (tests / 'hidden').mkdir()
        (tests / 'hidden/test_outside.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_outside(self): self.assertEqual(1,1)\n')
        result = subprocess.run([sys.executable, str(tests / 'paralel_kos.py')], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('test_outside.py', result.stderr)

    def test_common_regression_rejects_node_skip_empty_and_omitted_file(self):
        repo = self.f.home / 'nodegate'
        tests, library = repo / 'engine/tests', repo / 'engine/lib'
        tests.mkdir(parents=True)
        library.mkdir()
        source = Path(__file__).resolve().parent
        for name in ('regresyon.py', 'paralel_kos.py'):
            shutil.copyfile(source / name, tests / name)
        for name in ('qa_turet.py', 'qa_kaniti.py', 'qa-sim/sitecustomize.py', 'qa-node-reporter.cjs'):
            (library / name).parent.mkdir(exist_ok=True)
            shutil.copyfile(source.parent / 'lib' / name, library / name)
        (tests / 'pushguard.sh').write_text('exit 0\n')
        (tests / 'test_gate.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertEqual(1,1)\n')
        good = "import test from 'node:test'; import assert from 'node:assert/strict'; test('ok',()=>assert.equal(1,1));\n"
        for name in ('butce-plugin.test.mjs', 'entegrasyon.test.mjs', 'dusunme.test.mjs'):
            (tests / name).write_text(good)
        env = {k: v for k, v in os.environ.items() if not k.startswith('CADRE_')}
        archive = self.f.home / 'nodegate-evidence'
        env['CADRE_TEST_ARSIVI'] = str(archive)
        for variant in ('good', 'skip', 'empty', 'omitted'):
            with self.subTest(variant=variant):
                (tests / 'dusunme.test.mjs').write_text(good if variant in ('good', 'omitted') else
                    "import test from 'node:test'; test.skip('required',()=>{});\n" if variant == 'skip' else '')
                if variant == 'omitted':
                    (tests / 'extra.test.mjs').write_text(good)
                result = subprocess.run([sys.executable, str(tests / 'regresyon.py')], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0 if variant == 'good' else 1, result.stdout + result.stderr)
                record = json.loads((archive / 'regresyon.jsonl').read_text().splitlines()[-1])
                node = next(step for step in record['adimlar'] if step['ad'].startswith('Node'))
                self.assertEqual(node['sonuc'], 'PASS' if variant == 'good' else 'FAIL', node)
                self.assertTrue(Path(node['evidence']).is_file())
                self.assertEqual(record['node']['tests'], 3)

    def test_worker_setting_reaches_the_parallel_runner_through_the_common_gate(self):
        """CR-35 (15.10 campaign): `CADRE_TEST_ISCI` was not carried from the shared gate to the subprocess (regresyon.py removed all CADRE_* variables
        and put back only the allow-list); Codex's full gate ran with 10 workers and two timing tests turned red.
        The effective worker count is now visible in the output and the archive record; an invalid value stops with an explicit error."""
        repo = self.f.home / 'workergate'
        tests, library = repo / 'engine/tests', repo / 'engine/lib'
        tests.mkdir(parents=True)
        library.mkdir()
        source = Path(__file__).resolve().parent
        for name in ('regresyon.py', 'paralel_kos.py'):
            shutil.copyfile(source / name, tests / name)
        for name in ('qa_turet.py', 'qa_kaniti.py', 'qa-sim/sitecustomize.py', 'qa-node-reporter.cjs'):
            (library / name).parent.mkdir(exist_ok=True)
            shutil.copyfile(source.parent / 'lib' / name, library / name)
        (tests / 'pushguard.sh').write_text('exit 0\n')
        (tests / 'test_gate.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertEqual(1,1)\n')
        good = "import test from 'node:test'; import assert from 'node:assert/strict'; test('ok',()=>assert.equal(1,1));\n"
        for name in ('butce-plugin.test.mjs', 'entegrasyon.test.mjs', 'dusunme.test.mjs'):
            (tests / name).write_text(good)
        env = {k: v for k, v in os.environ.items() if not k.startswith('CADRE_')}
        archive = self.f.home / 'workergate-evidence'
        env['CADRE_TEST_ARSIVI'] = str(archive)
        env['CADRE_TEST_ISCI'] = '1'
        result = subprocess.run([sys.executable, str(tests / 'regresyon.py')], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('isci: 1 (CADRE_TEST_ISCI=1', result.stdout)
        record = json.loads((archive / 'regresyon.jsonl').read_text().splitlines()[-1])
        self.assertEqual(record['isci'], 1)
        del env['CADRE_TEST_ISCI']
        plain = subprocess.run([sys.executable, str(tests / 'paralel_kos.py')], env=env, capture_output=True, text=True)
        self.assertEqual(plain.returncode, 0, plain.stdout + plain.stderr)
        self.assertIn(f'isci: {max(1, min(4, os.cpu_count() or 4))} (CADRE_TEST_ISCI=-', plain.stdout)
        for bad in ('abc', '', '0', '-2'):
            with self.subTest(bad=bad):
                broken = subprocess.run([sys.executable, str(tests / 'paralel_kos.py')], env={**env, 'CADRE_TEST_ISCI': bad},
                                        capture_output=True, text=True)
                if bad == '':
                    self.assertEqual(broken.returncode, 0, broken.stderr)
                else:
                    self.assertEqual(broken.returncode, 2)
                    self.assertIn('CADRE_TEST_ISCI', broken.stderr)


if __name__ == '__main__':
    unittest.main()


class QaUrunBagiTests(unittest.TestCase):
    """CR-12 (N-QA01/N-ENV03): when the interpreter installed in copy A runs in worktree B, QA must load B's product;
    copy A's result cannot be B's acceptance."""

    ESKI = 'def ikiye(deger):\n    return deger * 2\n'
    TEST = ('import unittest\n\nfrom urun import ikiye\n\n\nclass Ikiye(unittest.TestCase):\n'
            '    def test_ikiye(self):\n        self.assertEqual(ikiye(2), 4)\n')

    def setUp(self):
        from test_sonuc import CompletionTests
        self.f = CompletionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.wt, self.store = self.f.wt, self.f.store
        self.kopya = self.f.home / 'kopya-a'
        (self.kopya / 'src/urun').mkdir(parents=True)
        (self.kopya / 'src/urun/__init__.py').write_text(self.ESKI)
        venv = self.kopya / '.venv'
        subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(venv)], check=True, capture_output=True, timeout=120)
        self.site = next((venv / 'lib').glob('python*/site-packages'))
        (self.site / 'urun').mkdir()
        (self.site / 'urun/__init__.py').write_text(self.ESKI)
        self.python = venv / 'bin/python'
        (self.wt / 'src/urun').mkdir(parents=True)
        (self.wt / 'src/urun/__init__.py').write_text(self.ESKI)
        (self.wt / 'tests').mkdir()
        (self.wt / 'tests/test_urun.py').write_text(self.TEST)
        self.f.git('add', '.')
        self.f.git('-c', 'user.name=fixture', '-c', 'user.email=f@local', 'commit', '-qm', 'urun kaynagi')
        self.f.base = self.f.git('rev-parse', 'HEAD').strip()
        self.f.run = self.store.accept('a', 'urun davranisi', source_sha=self.f.base)
        self.f.rid = self.f.run['run_id']
        self.store.update(self.f.rid, worktree=str(self.wt))
        self.f.enter(patch.dict(os.environ, self.store.environment(self.f.rid)))
        self.command = f'{shlex.quote(str(self.python))} -B -m unittest -q tests.test_urun'
        Path(self.f.run['constraints']['qa-komut']).write_text(self.command)

    def check(self, model='claude/fixture'):
        return core.qa_kapisi('a', str(self.wt), self.f.log, '/forbidden/provider', model=model)

    def test_old_copy_green_cannot_hide_a_broken_worktree_product(self):
        (self.wt / 'src/urun/__init__.py').write_text(self.ESKI.replace('* 2', '* 3'))
        result = self.check()
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'command-result'), result)
        urun = result['product']
        self.assertEqual(urun['status'], 'PASS', urun)
        self.assertTrue(Path(urun['modules']['urun']['path']).is_relative_to(self.wt.resolve()), urun)

    def test_worktree_only_behavior_passes_without_a_repair_round(self):
        (self.wt / 'src/urun/__init__.py').write_text(self.ESKI + '\n\ndef uc(deger):\n    return deger * 3\n')
        (self.wt / 'tests/test_urun.py').write_text(self.TEST.replace('import ikiye', 'import ikiye, uc')
                                                   + '\n    def test_uc(self):\n        self.assertEqual(uc(2), 6)\n')
        with patch.object(core, 'capture_opencode') as repair:
            result = self.check(model='fixture/impl')
        self.assertEqual(result['status'], 'PASS', result)
        repair.assert_not_called()
        self.assertEqual(result['product']['status'], 'PASS', result['product'])
        self.assertEqual(result['product']['binding'], [str(self.wt.resolve() / 'src')])
        self.assertTrue(Path(result['product']['modules']['urun']['path']).is_relative_to(self.wt.resolve()))

    def test_unbindable_mismatch_is_an_environment_error_not_a_test_verdict(self):
        import qa_kaniti
        (self.wt / 'src/urun/__init__.py').write_text(self.ESKI.replace('* 2', '* 3'))
        with patch.object(qa_kaniti, 'product_roots', return_value=[]), patch.object(core, 'capture_opencode') as repair:
            result = self.check(model='fixture/impl')
        self.assertEqual((result['status'], result['reason']), ('ERROR', 'qa-product-mismatch'), result)
        repair.assert_not_called()
        actual = result['product']['modules']['urun']['path']
        self.assertTrue(Path(actual).is_relative_to(self.kopya.resolve()), actual)
        self.assertEqual(result['product']['modules']['urun']['expected'], str((self.wt / 'src/urun/__init__.py').resolve()))

    def test_unreadable_editable_source_is_unverified_not_a_red_product(self):
        import qa_kaniti
        shutil.rmtree(self.site / 'urun')
        (self.site / 'urun.pth').write_text(str(self.kopya / 'src') + '\n')
        (self.wt / 'src/urun/__init__.py').write_text(self.ESKI.replace('* 2', '* 3'))
        with patch.object(qa_kaniti, 'product_roots', return_value=[]), patch.object(core, 'capture_opencode') as repair:
            result = self.check(model='fixture/impl')
        self.assertEqual((result['status'], result['reason']), ('ERROR', 'qa-product-unverified'), result)
        repair.assert_not_called()
        self.assertEqual(result['product']['modules']['urun']['status'], 'UNRESOLVED')

    def test_task_environment_names_the_pinned_command_interpreter_binding_and_write_rule(self):
        from kosu import gorev_ortami
        metin = gorev_ortami(self.store, self.store.read(self.f.rid), ['src/urun/__init__.py', 'tests/test_urun.py'])
        self.assertIn('QA KOMUTU', metin)
        self.assertIn(self.command, metin)
        self.assertIn(f"PYTHON: {self.python}", metin)
        self.assertIn(f"PYTHONPATH={self.wt.resolve() / 'src'}", metin)
        for kural in ('sed -i', '$TMPDIR', 'uv', 'kisitli kabuk', 'EOF'):
            self.assertIn(kural, metin)
        self.assertNotIn('edit arac', metin)
        self.assertIn('DOSYALAR', metin)
        self.assertNotIn('DOSYALAR', gorev_ortami(self.store, self.store.read(self.f.rid), ['src/urun/__init__.py'], kapsam='worktree'))
        self.assertEqual(gorev_ortami(self.store, {'rapor': None}, ['src/a.py']), '')

    def test_lane_prompt_carries_the_environment_block_instead_of_a_bare_test_hint(self):
        plan = {'is_id': 'cr47', 'gorevler': [{'id': 'g1', 'tanim': 'urun degisikligi', 'dosyalar': ['src/urun/__init__.py'],
                                                'kabul': 'test gecer', 'kapsam_disi': [], 'bagimli': []}], 'birlestirme_sirasi': ['g1']}
        env = {**os.environ, 'CADRE_RUN_DIR': str(self.store.path(self.f.rid)), 'CADRE_HOME': str(self.f.home)}
        lib = Path(__file__).resolve().parents[1] / 'lib'
        istem = subprocess.run([sys.executable, str(lib / 'dispatch_util.py'), 'gorev_pf', 'g1'], input=json.dumps(plan),
                               capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(istem.returncode, 0, istem.stderr)
        self.assertIn('ORTAM', istem.stdout)
        self.assertIn(self.command, istem.stdout)
        self.assertIn('QA KOMUTU ile test et', istem.stdout)
        self.assertNotIn('testi varsa kos', istem.stdout)
        self.assertNotIn('websearch aracini kullan', istem.stdout)
        self.assertIn('websearch araclari kapali', istem.stdout)
        self.assertIn('kisitli kabuk', istem.stdout)
        self.assertLess(istem.stdout.index('GIT:'), istem.stdout.index('ORTAM'))

    def test_repair_prompt_carries_the_same_environment_block(self):
        (self.wt / 'src/urun/__init__.py').write_text(self.ESKI.replace('* 2', '* 3'))
        with patch.object(core, 'capture_opencode') as repair:
            result = self.check(model='fixture/impl')
        self.assertEqual(result['status'], 'FAIL', result)
        prompt = repair.call_args.args[2][-1]
        self.assertIn('QA KAPISI KIRMIZI', prompt)
        self.assertIn('QA KOMUTU', prompt)
        self.assertIn(f"PYTHONPATH={self.wt.resolve() / 'src'}", prompt)
        self.assertNotIn('DOSYALAR listesi', prompt)

    def test_product_modules_follow_package_layout_and_skip_flat_or_missing_files(self):
        import qa_kaniti
        (self.wt / 'src/urun/alt').mkdir()
        (self.wt / 'src/urun/alt/__init__.py').write_text('')
        (self.wt / 'src/urun/alt/mod.py').write_text('')
        (self.wt / 'kok').mkdir()
        (self.wt / 'kok/__init__.py').write_text('')
        (self.wt / 'kok/m.py').write_text('')
        (self.wt / 'hesap.py').write_text('')
        files = ['src/urun/__init__.py', 'src/urun/alt/mod.py', 'kok/m.py', 'hesap.py', 'src/urun/yok.py', 'tests/test_urun.py']
        self.assertEqual(qa_kaniti.product_modules(self.wt, files), {
            'urun': str((self.wt / 'src/urun/__init__.py').resolve()),
            'urun.alt.mod': str((self.wt / 'src/urun/alt/mod.py').resolve()),
            'kok.m': str((self.wt / 'kok/m.py').resolve())})
        self.assertEqual(qa_kaniti.product_roots(self.wt, files), [str((self.wt / 'src').resolve())])
