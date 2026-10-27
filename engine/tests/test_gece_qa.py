"""P6 (user decision 16.09 "I will not do anything, QA runs at the end of every job"):
in a project without a QA command the task does not die silently with SKIPPED and is not rejected at the front gate either — a QA command is derived
from the repo at acceptance (Makefile/pytest/npm/go/cargo; syntax QA if none), recorded as `qa.source=derived`, and the QA
gate runs this command. The derived syntax QA catches an undeclared broken file; the run cannot PASS."""

from __future__ import annotations

import json
import shlex
import shutil
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
import test_uctan_uca as e2e  # noqa: E402
import qa_turet  # noqa: E402
from kosu import RunStore  # noqa: E402


class DeriveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-p6-')
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / 'repo'
        self.repo.mkdir()
        self.root = Path('/kod-koku')

    def test_makefile_targets_are_preferred_in_check_test_lint_order(self):
        (self.repo / 'Makefile').write_text('lint:\n\techo lint\ntest:\n\techo test\n')
        self.assertEqual(qa_turet.turet(self.repo, self.root), {'command': 'make test', 'kind': 'makefile'})
        (self.repo / 'Makefile').write_text('check: lint test\n\t@true\nlint:\n\techo lint\ntest:\n\techo test\n')
        self.assertEqual(qa_turet.turet(self.repo, self.root)['command'], 'make check')

    def test_nested_unittest_suite_is_run_not_just_syntax_checked(self):
        """B325 (02.10): the tests of a project were under `engine/tests`, with no Makefile/pytest trace → QA fell to syntax only.
        A nested test folder is found too; command tests run with an isolated HOME, the system's CADRE_* environment does not leak (never writes to the live home)."""
        (self.repo / 'engine/tests').mkdir(parents=True)
        for ad in ('test_a.py', 'test_b.py'):
            (self.repo / 'engine/tests' / ad).write_text('import unittest\nclass T(unittest.TestCase):\n    def test_x(self): pass\n')
        derived = qa_turet.turet(self.repo, self.root)
        self.assertEqual(derived['kind'], 'unittest')
        self.assertIn('engine/tests', derived['command'])
        self.assertIn('-u CADRE_HOME', derived['command'])
        r = subprocess.run(['bash', '-c', derived['command']], cwd=self.repo, capture_output=True, text=True, timeout=120,
                           env={**os.environ, 'CADRE_HOME': '/canli-ev-olmamali'})
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertIn('Ran 2 tests', r.stderr)

    def test_pytest_layout_npm_go_and_cargo_are_recognised(self):
        (self.repo / 'tests').mkdir()
        (self.repo / 'tests/test_a.py').write_text('def test_a(): pass\n')
        (self.repo / 'pyproject.toml').write_text('[tool.pytest.ini_options]\ntestpaths=["tests"]\n')
        derived = qa_turet.turet(self.repo, self.root)
        self.assertIn(derived['kind'], ('pytest', 'unittest'))
        self.assertIn('-m ' + derived['kind'], derived['command'])
        npm = Path(self.temp.name) / 'npm'
        npm.mkdir()
        (npm / 'package.json').write_text(json.dumps({'scripts': {'test': 'echo "Error: no test specified" && exit 1'}}))
        self.assertEqual(qa_turet.turet(npm, self.root)['kind'], 'sozdizim', 'placeholder npm test is not QA')
        (npm / 'package.json').write_text(json.dumps({'scripts': {'test': 'vitest run'}}))
        self.assertEqual(qa_turet.turet(npm, self.root), {'command': 'npm test --silent', 'kind': 'npm'})
        go = Path(self.temp.name) / 'go'
        go.mkdir()
        (go / 'go.mod').write_text('module x\n')
        self.assertEqual(qa_turet.turet(go, self.root)['command'], 'go test ./...')
        rust = Path(self.temp.name) / 'rust'
        rust.mkdir()
        (rust / 'Cargo.toml').write_text('[package]\nname="x"\n')
        self.assertEqual(qa_turet.turet(rust, self.root)['command'], 'cargo test -q')

    def test_bare_repo_falls_back_to_syntax_qa_from_the_code_root(self):
        derived = qa_turet.turet(self.repo, self.root)
        self.assertEqual(derived['kind'], 'sozdizim')
        self.assertEqual(derived['command'], f'{shlex.quote(sys.executable)} /kod-koku/lib/qa_turet.py sozdizim .')

    def test_a_real_npm_test_ending_in_exit_1_is_kept_and_its_failure_is_seen(self):
        npm = Path(self.temp.name) / 'npm'
        npm.mkdir()
        (npm / 'failing.test.js').write_text("require('node:test')('kirmizi', () => { throw new Error('kirmizi') })\n")
        for betik in ('node --test failing.test.js || exit 1', 'node --test failing.test.js; exit 10', 'test -f yok || exit 1'):
            (npm / 'package.json').write_text(json.dumps({'scripts': {'test': betik}}))
            self.assertEqual(qa_turet.turet(npm, self.root), {'command': 'npm test --silent', 'kind': 'npm'}, betik)
        if shutil.which('npm'):
            (npm / 'package.json').write_text(json.dumps({'scripts': {'test': 'node --test failing.test.js || exit 1'}}))
            r = subprocess.run(['bash', '-c', qa_turet.turet(npm, self.root)['command']], cwd=npm, capture_output=True, text=True, timeout=120)
            self.assertNotEqual(r.returncode, 0, 'kırmızı ürün testi QA\'yı kırmızı yapar')
        for betik in ('echo "Error: no test specified" && exit 1', "echo 'Error: no test specified' && exit 1"):
            (npm / 'package.json').write_text(json.dumps({'scripts': {'test': betik}}))
            self.assertEqual(qa_turet.turet(npm, self.root)['kind'], 'sozdizim', betik)

    def test_derived_commands_survive_spaces_and_quotes_in_paths(self):
        kok = Path(self.temp.name) / "Benim Proje'm"
        (kok / '.venv/bin').mkdir(parents=True)
        (kok / '.venv/bin/python').symlink_to(sys.executable)
        (kok / 'tests').mkdir()
        (kok / 'tests/__init__.py').write_text('')
        (kok / 'tests/test_a.py').write_text('import unittest\nclass T(unittest.TestCase):\n    def test_x(self): pass\n')
        derived = qa_turet.turet(kok, self.root)
        r = subprocess.run(['bash', '-c', derived['command']], cwd=kok, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, derived['command'] + '\n' + r.stderr[-400:])
        ic = Path(self.temp.name) / 'ic'
        (ic / 'motor testleri').mkdir(parents=True)
        (ic / 'motor testleri/test_a.py').write_text('import unittest\nclass T(unittest.TestCase):\n    def test_x(self): pass\n')
        derived = qa_turet.turet(ic, Path(self.temp.name) / 'kod kökü')
        r = subprocess.run(['bash', '-c', derived['command']], cwd=ic, capture_output=True, text=True, timeout=120)
        self.assertEqual((derived['kind'], r.returncode), ('unittest', 0), derived['command'] + '\n' + r.stderr[-400:])
        bos = Path(self.temp.name) / 'bos proje'
        bos.mkdir()
        derived = qa_turet.turet(bos, LIB.parent)
        r = subprocess.run(['bash', '-c', derived['command']], cwd=bos, capture_output=True, text=True, timeout=120)
        self.assertEqual((derived['kind'], r.returncode), ('sozdizim', 0), derived['command'] + '\n' + r.stderr[-400:])

    def test_derived_python_is_absolute_so_a_login_shell_cannot_swap_it(self):
        """B334 (03.10): when `bash -lc` reset PATH a bare python3 fell to Xcode 3.9, 1000 false red tests."""
        (self.repo / 'engine/tests').mkdir(parents=True)
        (self.repo / 'engine/tests/test_a.py').write_text('')
        self.assertIn(f'{sys.executable} -m unittest', qa_turet.turet(self.repo, self.root)['command'])

    def test_syntax_qa_names_broken_files_and_writes_no_bytecode(self):
        (self.repo / 'ok.py').write_text('x = 1\n')
        (self.repo / 'ok.sh').write_text('echo ok\n')
        (self.repo / 'ok.json').write_text('{"a": 1}\n')
        (self.repo / 'node_modules').mkdir()
        (self.repo / 'node_modules/ignored.py').write_text('def (:\n')
        result = subprocess.run([sys.executable, str(LIB / 'qa_turet.py'), 'sozdizim', str(self.repo)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(list(self.repo.rglob('__pycache__')))
        (self.repo / 'bozuk.py').write_text('def f(:\n')
        (self.repo / 'bozuk.sh').write_text('if [ ; then\n')
        result = subprocess.run([sys.executable, str(LIB / 'qa_turet.py'), 'sozdizim', str(self.repo)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn('bozuk.py', result.stderr)
        self.assertIn('bozuk.sh', result.stderr)
        self.assertIn('bozuk=2', result.stdout)


    def test_login_shell_keeps_the_engine_path(self):
        """B334: the QA/baseline/format command runs in a login shell but PATH is the engine's (path_helper cannot put /usr/bin first)."""
        import qe_core
        import shutil
        r = subprocess.run(qe_core.giris_kabugu('command -v python3'), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.strip(), shutil.which('python3'))

    def test_project_tests_do_not_see_the_run_environment(self):
        """B338 (03.10): when CADRE_RUN_ID leaked into the tests the real run record was read and 9 false reds came out."""
        import qe_core
        sizan = {'CADRE_RUN_ID': 'canli-kosu', 'CADRE_GOREV_ID': 'kalite', 'OPENCODE_CONFIG_CONTENT': '{}', 'CADRE_YEREL_YOK': '1'}
        with patch.dict(os.environ, sizan):
            env = qe_core.qa_ortami()
        self.assertFalse(set(sizan) & set(env))
        self.assertIn('PATH', env)


class BosBitisTest(unittest.TestCase):
    def test_a_length_finish_without_tools_is_not_a_product(self):
        """B341 (03.10 run A): glm-5.3-flash spent 8192 output tokens on thinking and ended without tools; rc=0 counted as 'produced', 4 lanes empty."""
        import qe_core
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as d:
            yol = Path(d) / 'stdout.jsonl'
            proc = SimpleNamespace(attempt=SimpleNamespace(data={'stdout': str(yol)}))
            bos = [{'part': {'type': 'step-start'}}, {'part': {'type': 'step-finish', 'reason': 'length'}}]
            yol.write_text('\n'.join(json.dumps(x) for x in bos))
            self.assertTrue(qe_core.bos_bitis(proc))
            yol.write_text('\n'.join(json.dumps(x) for x in [{'part': {'type': 'tool'}}] + bos))
            self.assertTrue(qe_core.bos_bitis(proc), 'arac kullanimi son yanitin kesildigi gercegini degistirmez')
            yol.write_text(json.dumps({'part': {'type': 'step-finish', 'reason': 'stop'}}))
            self.assertFalse(qe_core.bos_bitis(proc))
        self.assertFalse(qe_core.bos_bitis(SimpleNamespace()))


class AracCalisiyorTest(unittest.TestCase):
    def test_a_sleeping_shell_tool_counts_as_running(self):
        """B342 (03.10): an 11 min test suite is at %CPU<3 most of the time; the guard took it for a 'broken stream' and killed the correction round twice."""
        import qe_core
        ebeveyn = subprocess.Popen([sys.executable, '-c', 'import subprocess; subprocess.run(["/bin/zsh", "-c", "sleep 2; true"])'])
        try:
            time.sleep(1)
            self.assertTrue(qe_core.arac_calisiyor(ebeveyn.pid))
        finally:
            ebeveyn.kill(); ebeveyn.wait()
        bos = subprocess.Popen([sys.executable, '-c', 'import subprocess; subprocess.run(["sleep", "2"])'])
        try:
            time.sleep(1)
            self.assertFalse(qe_core.arac_calisiyor(bos.pid), 'kabuksuz ve CPU tuketmeyen alt surec arac degildir')
        finally:
            bos.kill(); bos.wait()


class AcceptIntegrationTests(e2e.E2EFixture):
    def test_project_without_qa_command_gets_a_derived_one_recorded_at_acceptance(self):
        (self.home / 'projects/e2e/qa-komut').unlink()
        (self.repo / 'Makefile').write_text('test:\n\t@echo ok\n')
        self.git('add', 'Makefile')
        self.git('commit', '-qm', 'makefile')
        with patch.dict(os.environ, {'CADRE_ENGINE_ROOT': str(self.root)}):
            record = self.store.accept('e2e', 'gorev')
        self.assertEqual(record['qa'], {'source': 'derived', 'kind': 'makefile', 'command': 'make test'})
        self.assertEqual(Path(record['constraints']['qa-komut']).read_text().strip(), 'make test')

    def test_project_with_its_own_qa_command_is_untouched(self):
        record = self.store.accept('e2e', 'gorev')
        self.assertEqual(record['qa']['source'], 'project')
        self.assertIn('py_compile a.py b.py', record['qa']['command'])
        self.assertEqual(Path(record['constraints']['qa-komut']).read_text(), (self.home / 'projects/e2e/qa-komut').read_text())

    def test_full_pipeline_with_derived_syntax_qa_passes_clean_product_and_fails_an_undeclared_broken_file(self):
        (self.home / 'projects/e2e/qa-komut').unlink()
        rid = self.submit()
        record = self.wait_terminal(rid)
        self.assertEqual(record['qa']['kind'], 'sozdizim')
        self.assertNotEqual(record['state'], 'completed')
        self.assertTrue(record['final_result']['qa']['command'].endswith('qa_turet.py sozdizim .'))
        self.assertEqual(record['final_result']['qa']['status'], 'ERROR')
        self.assertEqual(record['final_result']['qa']['reason'], 'qa-discovery-unverified')
        from kosu import iptal_et
        self.assertTrue(iptal_et(self.store, record))
        # red: the worker leaves a syntax error in an EXISTING file it did not declare -> fast verification does not see it, the derived QA does
        # (B192: an undeclared NEW file is already removed from the product; separate test below)
        (self.repo / 'yardimci.py').write_text('X = 1\n')
        self.git('add', 'yardimci.py')
        self.git('commit', '-qm', 'yardimci')
        rid2 = self.submit({'E2E_EXTRA_BROKEN': '1', 'E2E_EXTRA_BROKEN_FILE': 'yardimci.py'})
        record2 = self.wait_terminal(rid2)
        self.assertNotEqual(record2['state'], 'completed')
        self.assertEqual(record2['final_result']['qa']['status'], 'FAIL')
        qa_log = Path(record2['final_result']['qa']['attempts'][-1]['output']).read_text()
        self.assertIn('yardimci.py', qa_log)
        self.assertEqual(record2['final_result']['status'], 'FAIL')

    def test_an_undeclared_new_broken_file_is_removed_from_the_product_before_qa(self):
        """B192: a new file not named in the plan is removed from the product at the end of the lane (with a backup); the clean product enters the acceptance path."""
        rid = self.submit({'E2E_EXTRA_BROKEN': '1'})
        record = self.wait_terminal(rid)
        self.assertEqual(record['state'], 'completed', (self.store.path(rid) / 'coordinator.log').read_text()[-1500:])
        self.assertFalse((Path(record['worktree']) / 'bozuk.py').exists())
        self.assertTrue(list((self.store.path(rid) / 'kapsam-disi').glob('*/bozuk.py')), 'yedeklenmeli')


if __name__ == '__main__':
    unittest.main()
