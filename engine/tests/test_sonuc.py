"""C1/K4: executor exit is not acceptance; only bound QA/review evidence passes."""

from __future__ import annotations

import json
import os
import pathlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
os.environ.setdefault('CADRE_BEKLEME_ESIK_SN', '0')   # B268: the dispatcher fixture must not wait for midnight on the real clock
import qe_core as core
from qa_fixture import command as qa_command, review_output
from kosu import RunStore, execute_run
from sonuc import approved, parse_review, revision


def setUpModule():
    ortam = patch.dict(os.environ, {'CADRE_ZINCIR_KALITE': 'opencode-go/qwen3.8-max'})
    ortam.start()
    unittest.addModuleCleanup(ortam.stop)

REAL_RUN_OPENCODE = core.run_opencode


class CompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix='qe-completion-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = RunStore(self.home)
        self.wt = self.home/'wt'
        self.wt.mkdir()
        self.git('init', '-q')
        (self.wt/'product.txt').write_text('before\n')
        self.git('add', '.')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD').strip()
        (self.wt/'product.txt').write_text('after\n')
        self.git('add', '.')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-qm', 'work')
        self.project = self.home/'projects/a'
        self.project.mkdir(parents=True)
        (self.project/'qa-komut').write_text('exit 1')
        self.run = self.store.accept('a', 'deliver product', source_sha=self.base)
        self.rid = self.run['run_id']
        self.store.update(self.rid, worktree=str(self.wt))
        writer = self.store.task_dir(self.rid, 'impl')/'attempts/fixture/attempt.json'
        writer.parent.mkdir(parents=True)
        writer.write_text(json.dumps({'role': 'impl', 'model': 'fixture/writer', 'pid': 123}))
        self.enter(patch.dict(os.environ, {**self.store.environment(self.rid), 'CADRE_GOREV_ID':'', 'CADRE_OWNER_FDS':''}))
        self.enter(patch.object(core, 'QE', self.home))
        self.enter(patch.object(core, 'worktree_base_sha', return_value=self.base))
        self.log = self.home/'motor.log'
        self.enter(patch.object(core, 'run_opencode', return_value=Mock(poll=lambda:0, wait=lambda **kwargs:0, returncode=0)))
        self.enter(patch.object(core, 'butce_json', return_value=None))   # B37: the judge chain reads the budget; keep the test hermetic
        # P3 (03.10): no local; the repair/correction starts in the cloud — these tests measure the flow, not the model selection
        self.enter(patch.object(core, 'bulut_aday', return_value='opencode-go/test'))

    def enter(self, context):
        result = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return result

    def git(self, *args: str) -> str:
        return subprocess.run(['git', '-C', str(self.wt), *args], check=True, capture_output=True, text=True, timeout=10).stdout

    def test_git_snapshot_keeps_racy_index_detection_and_preserves_the_source_index(self) -> None:
        self.git('config', 'core.trustctime', 'false')
        self.git('config', 'core.checkStat', 'minimal')
        product = self.wt / 'racy.txt'
        product.write_text('before\n')
        stamp = time.time_ns() - 10_000_000_000
        os.utime(product, ns=(stamp, stamp))
        self.git('add', '.')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-qm', 'racy-index')
        index = self.wt / '.git/index'
        os.utime(index, ns=(stamp, stamp))
        index_bytes = index.read_bytes()
        before = revision(self.wt)
        product.write_text('after!\n')
        os.utime(product, ns=(stamp, stamp))
        after = revision(self.wt)
        self.assertNotEqual(before, after, 'same-size racy changes must invalidate the accepted product')
        self.assertEqual(index.read_bytes(), index_bytes)
        self.assertEqual(index.stat().st_mtime_ns, stamp)

    def urun_worktree(self):
        """qe_commit_wip commits only under CADRE_HOME/worktrees/<project>/... and on a cadre/* branch."""
        wt = self.home/'worktrees/a/kosu1'
        wt.mkdir(parents=True)
        def git(*args):
            return subprocess.run(['git', '-C', str(wt), '-c', 'user.name=fixture',
                                   '-c', 'user.email=fixture@local', *args],
                                  check=True, capture_output=True, text=True, timeout=10).stdout
        git('init', '-q', '-b', 'cadre/is-1')
        (wt/'urun.py').write_text('print(1)\n')
        git('add', '.')
        git('commit', '-qm', 'urun')
        return wt, git

    def test_reviewer_repair_is_committed_so_the_delivered_branch_is_the_reviewed_product(self):
        """B110-3: the judge works with 'do not commit', while the final commit is made BEFORE finalize_run. If the fix
        is not committed, `cadre push` delivers the previous state, NOT the reviewed product."""
        wt, git = self.urun_worktree()
        (wt/'urun.py').write_text('print(2)\n')                    # the judge's small fix
        (wt/'.qa-sonuc.txt').write_text('PASS\n')                   # a harness leftover: not product
        kimlik = revision(wt)
        self.run = self.store.accept('a', 'deliver reviewed product', source_sha=git('rev-parse', 'HEAD').strip())
        self.rid = self.run['run_id']
        self.store.update(self.rid, worktree=str(wt))
        Path(self.run['constraints']['qa-komut']).write_text(qa_command('python3 urun.py | grep -qx 2'))
        with patch.dict(os.environ, self.store.environment(self.rid)):
            qa = core.qa_kapisi('a', str(wt), self.log, '/forbidden/provider', model='claude/fixture')
        self.assertEqual(qa['status'], 'PASS')
        import kabul
        with patch.dict(os.environ, self.store.environment(self.rid)):
            raw = self.review(identity=kimlik)
            proof = kabul.review_binding(self.store, self.rid, getattr(raw, 'attempt'), set(), kabul.review_context(self.store.read(self.rid)))
        self.assertIsNotNone(proof)
        sonuc = {'status': 'PASS', 'revision': kimlik,
                 'qa': qa, 'review': {'status': 'PASS', 'revision': kimlik, 'independent': True, 'session_proof': proof}}
        import izin
        with patch.dict(os.environ, self.store.environment(self.rid)):
            pending = core.teslim_commit(wt, 'a', self.log, sonuc)
        self.assertEqual(pending['delivery']['git_permission'], 'required')
        self.store.update(self.rid, state='completed', final_result=pending)
        token = pending['delivery']['action_token']
        with patch.dict(os.environ, {key: value for key, value in os.environ.items() if key != 'CADRE_RUN_ID'}, clear=True):
            first = izin.approve(self.store, self.rid, token)
            izin.approve(self.store, self.rid, token, first['confirmation'])
        izin.commit_execute(self.store, self.rid, token)
        izin.adopt_commit(self.store, self.rid, izin.read_action(self.store, self.rid, token))
        yeni = self.store.read(self.rid)['final_result']
        self.assertEqual(yeni['status'], 'PASS')
        self.assertEqual(git('status', '--porcelain', '--', '.', *core.URUN_HARIC).strip(), '')
        self.assertIn('print(2)', git('show', 'HEAD:urun.py'))
        self.store.update(self.rid, worktree=str(wt), final_result=yeni)
        self.assertTrue(approved(self.store.read(self.rid)), 'teslim commit sonrasi kabul kaniti guncel olmali')

    def test_delivery_commit_keeps_harness_leftovers_out_and_does_not_fail_acceptance(self):
        """QA output/the plan ledger are not committed; so acceptance must not fall to FAIL (a false positive)."""
        wt, git = self.urun_worktree()
        (wt/'.qa-sonuc.txt').write_text('PASS\n')
        kimlik = revision(wt)
        sonuc = {'status': 'PASS', 'revision': kimlik,
                 'qa': {'status': 'PASS', 'revision': kimlik}, 'review': {'status': 'PASS', 'revision': kimlik}}
        yeni = core.teslim_commit(wt, 'a', self.log, sonuc)
        self.assertEqual(yeni['status'], 'PASS')
        self.assertEqual(yeni['revision'], kimlik, 'urun degismediyse kimlik de degismez')
        self.assertEqual(git('log', '--oneline').count('\n'), 1, 'artik icin commit atilmaz')

    def test_teslim_commit_pathspec_list_matches_common_sh(self):
        """If the two lists diverge the delivery commit either commits leftovers or FAILs acceptance for nothing."""
        import izin
        wt, git = self.urun_worktree()
        self.store.update(self.rid, worktree=str(wt))
        commands, target, product = izin.commit_details(self.store, self.rid, wt, 'Fixture product')
        expected = tuple(path.replace(':(exclude,glob)', ':(glob)').replace(':(exclude)', '') for path in core.URUN_HARIC)
        self.assertEqual(tuple(commands[1][commands[1].index('--') + 1:]), expected)
        self.assertIn('lib/izin.py" checkpoint', (LIB/'common.sh').read_text())

    def test_qa_two_failures_return_structured_failure(self) -> None:
        result = core.qa_kapisi('a', str(self.wt), self.log, 'forbidden-live-cli')
        self.assertIsInstance(result, dict)
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(len(result['attempts']), 2)

    def test_qa_timeout_returns_error(self) -> None:
        """CANLI1 (03.10): the fixed QA timeout was removed; it may continue until the liveness check detects stuck.
        sleep 1 completes normally -> PASS."""
        Path(self.run['constraints']['qa-komut']).write_text(qa_command('sleep 1'))
        result = core.qa_kapisi('a', str(self.wt), self.log, 'forbidden-live-cli')
        self.assertIsInstance(result, dict)
        self.assertEqual(result['status'], 'PASS')

    def test_binary_override_does_not_bypass_qa(self) -> None:
        result = core.qa_kapisi('a', str(self.wt), self.log, '/fake/cli', fake_kosu=True)
        self.assertIsInstance(result, dict)
        self.assertEqual(result['status'], 'FAIL')

    def test_missing_qa_is_explicitly_skipped(self) -> None:
        (self.project/'qa-komut').unlink()
        Path(self.run['constraints']['qa-komut']).unlink()
        result = core.qa_kapisi('a', str(self.wt), self.log, '/fake/cli')
        self.assertIsInstance(result, dict)
        self.assertEqual(result['status'], 'SKIPPED')

    def test_coordinator_rejects_zero_exit_without_acceptance_evidence(self) -> None:
        root = self.home/'code'
        (root/'bin').mkdir(parents=True)
        (root/'bin/is-motoru.sh').write_text('exit 0\n')
        with patch.dict(os.environ, {'CADRE_ENGINE_ROOT':str(root), 'CADRE_DISPATCH':'0'}):
            rc = execute_run(self.store, self.rid)
        self.assertNotEqual(rc, 0)
        self.assertNotEqual(self.store.read(self.rid)['state'], 'completed')

    def test_agent_commits_on_the_run_branch_are_detected_and_reported(self) -> None:
        """B173 (23.09 T1v3, N1): even if the task text says 'do not commit', the executor can make its own commit on the cadre/* branch; no
        gate reported it. Default (reversible): detect + record + log; the delivery revision is still HEAD."""
        self.successful_qa()
        for dosya, mesaj in (('h.py', 'wip: harness ara kayit (1 dosya) — otomatik ara kayıt'), ('ajan.py', 'feat: ajanin kendi commiti')):
            (self.wt/dosya).write_text('x = 1\n')
            self.git('add', dosya)
            self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-qm', mesaj)
        with patch.object(core, 'capture_opencode', return_value=self.review()):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli')
        konular = [c['subject'] for c in result['agent_commits']]
        self.assertIn('feat: ajanin kendi commiti', konular)
        self.assertFalse(any(k.startswith('wip: harness') for k in konular), 'harness ara kayitlari ajan commit sayilmaz')
        self.assertIn('AJAN COMMIT', Path(self.log).read_text())

    def test_quality_chain_is_requested_with_the_declared_reviewer_size(self) -> None:
        """B158(c): the judge chain was asked with a fixed 30000; the B121 principle — a caller that does not know the size uses the role's declaration."""
        self.successful_qa()
        with patch.object(core.YON, 'zincir', return_value=(['deepseek/deepseek-v4-pro'], [])) as zincir, \
             patch.object(core, 'butce_json', return_value={'ts': 'x'}), \
             patch.object(core, 'capture_opencode', return_value=self.review()):
            core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=None)
        kalite = [c for c in zincir.call_args_list if c.args and c.args[0] == 'kalite']
        self.assertTrue(kalite)
        self.assertIsNone(kalite[0].args[2], 'istek boyutu None: yonlendirici rol beyanini (kalite 56000) kullanir')

    def test_reviewer_prompt_states_that_qa_repair_changed_the_product(self) -> None:
        """B158(f): when the QA repair round changed the product the judge prompt did not say so explicitly (only a trace in the qa JSON)."""
        isaret = self.wt / '.pytest_cache/qa.ok'
        isaret.parent.mkdir()
        Path(self.run['constraints']['qa-komut']).write_text(qa_command(f'test -f {isaret} && exit 0 || {{ touch {isaret}; exit 1; }}'))
        istemler = []

        def hakem(real, wt, args, timeout, env=None, role='kalite'):
            istemler.append(args[-1])
            return self.review()

        with patch.object(core, 'run_opencode', side_effect=lambda *a, **k: subprocess.Popen(['true'])), \
             patch.object(core, 'capture_opencode', side_effect=hakem):
            core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
        self.assertTrue(istemler)
        # B158(f): the QA repair prompt starts with QA KAPISI KIRMIZI; the judge prompt contains QA ONARIMI YAPILDI
        self.assertTrue(any('QA ONARIMI YAPILDI' in i for i in istemler), f'hiçbir istem QA ONARIMI YAPILDI içermiyor: {istemler}')

    def test_changed_existing_test_file_becomes_a_separate_review_criterion(self) -> None:
        """B167: the count gate (G-C7) cannot see a weaker assert with the same count; a test file that existed in the source and changed
        is given to the judge as a separate criterion. If only a new test is added, no criterion is added."""
        (self.wt/'test_urun.py').write_text('def test_x():\n    assert 1 + 1 == 2\n')
        self.git('add', '.')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-qm', 'test eklendi')
        temel = self.git('rev-parse', 'HEAD').strip()

        def kabul_istemi():
            kayit = self.store.accept('a', 'urunu yaz', source_sha=temel)
            self.store.update(kayit['run_id'], worktree=str(self.wt))
            Path(kayit['constraints']['qa-komut']).write_text(qa_command())
            istemler = []

            def hakem(real, wt, args, timeout, env=None, role='kalite'):
                istemler.append(args[-1])
                return self.review('GECER')

            with patch.dict(os.environ, {**self.store.environment(kayit['run_id']), 'CADRE_GOREV_ID': '', 'CADRE_OWNER_FDS': ''}), \
                 patch.object(core, 'capture_opencode', side_effect=hakem), \
                 patch.object(core, 'worktree_base_sha', return_value=temel):
                core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
            return istemler[0] if istemler else ''

        (self.wt/'test_yeni.py').write_text('def test_y():\n    assert True is not False\n')
        self.assertNotIn('degisen_testler', kabul_istemi(), 'yalniz yeni test: kriter yok')
        (self.wt/'test_urun.py').write_text('def test_x():\n    assert 1 + 1 >= 0\n')
        istem = kabul_istemi()
        self.assertIn('"id": "degisen_testler"', istem)
        self.assertIn('test_urun.py', istem)

    def test_qa_repair_attempt_start_and_end_are_logged(self) -> None:
        """B170 (23.09 T1b, T1v2): the repair attempt after a QA FAIL was never written to the engine log; 14 min of silence
        could only be read from the attempt record. The start and end of the repair are visible in the log."""
        isaret = self.wt / '.pytest_cache/qa.ok'
        isaret.parent.mkdir()
        Path(self.run['constraints']['qa-komut']).write_text(qa_command(f'test -f {isaret} && exit 0 || {{ touch {isaret}; exit 1; }}'))
        with patch.object(core, 'run_opencode', side_effect=lambda *a, **k: subprocess.Popen(['true'])):
            result = core.qa_kapisi('a', str(self.wt), self.log, '/fake/cli')
        self.assertEqual(result['status'], 'PASS')
        gunluk = Path(self.log).read_text()
        self.assertIn('QA ONARIM: basliyor', gunluk)
        self.assertIn('QA ONARIM: bitti (', gunluk)

    def successful_qa(self) -> None:
        from qa_fixture import command
        Path(self.run['constraints']['qa-komut']).write_text(command())

    def review_for_prompt(self, verdict='GECER'):
        """A fake judge answering all criterion ids in the JSON at the end of the prompt (B167: the criterion count changes with the product).
        P3/CANLI1: QA repair prompts contain no JSON; in that case it simply passes."""
        def hakem(real, wt, args, timeout, env=None, role='kalite'):
            istem = args[-1]
            if '{"revision"' not in istem:
                return self.review(verdict)    # QA repair prompt
            veri = json.JSONDecoder().raw_decode(istem[istem.rindex('{"revision"'):])[0]
            return self.review(verdict, ids=tuple(c['id'] for c in veri['criteria']))
        return hakem

    def review(self, verdict='GECER', *, identity=None, ids=('main',)) -> str:
        return review_output(json.dumps({'revision': (identity or revision(self.wt))['digest'], 'verdict':verdict,
                           'acceptance':[{'id':i,'status':'PASS','evidence':'product.txt inspected; QA exit 0'} for i in ids]}))

    def test_final_reviewer_receives_engine_scope_evidence_instead_of_a_git_instruction(self) -> None:
        self.successful_qa()
        prompts = []
        hakem = self.review_for_prompt('GECER')
        def capture(real, wt, args, timeout, env=None, role='kalite'):
            prompts.append(args[-1])
            return hakem(real, wt, args, timeout, env, role)
        with patch.object(core, 'capture_opencode', side_effect=capture):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
        self.assertEqual(result['status'], 'PASS', result)
        istem = [p for p in prompts if 'MOTOR KANITI' in p]
        self.assertTrue(istem, prompts[-1][:300] if prompts else 'hakem cagrilmadi')
        self.assertIn('product.txt', istem[-1])
        self.assertIn('degisen_dosyalar (', istem[-1])
        self.assertIn(f"kaynak {self.base}", istem[-1])
        self.assertNotIn('git diff', istem[-1])
        from kabul import motor_kaniti
        typed = motor_kaniti({'request': {'duration_minutes': 40}, 'request_clock': {'aktif_sn': 1852.4}, 'budget_spent_usd': 0.2,
                              'budget_held_usd': 0.01, 'budget_cap_usd': 4.5}, ['a.py', 'b.py'], {'head': 'h1', 'digest': 'd1'}, 'h0',
                             [{'sha': 'abc', 'subject': 'worker commit'}])
        self.assertIn('1 yurutucu commit\'i: abc worker commit', typed)
        self.assertIn('1852 sn / 2400 sn', typed)
        self.assertIn('tavan 4.5 USD', typed)

    def test_quality_duzelt_cannot_pass_even_when_qa_is_green(self) -> None:
        self.successful_qa()
        with patch.object(core, 'capture_opencode', return_value=self.review('DUZELT')):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
        self.assertEqual(result['qa']['status'], 'PASS')
        self.assertEqual(result['status'], 'FAIL')
        self.assertFalse(approved(self.store.read(self.rid)))

    def test_reviewer_falls_through_the_quality_chain_when_a_model_fails(self) -> None:
        """B37 (live acceptance 01:37): if the judge model fails with a budget denial/provider error acceptance must not be ERROR; the next
        link in the chain is tried and the model used is recorded. If all links fail the reason is 'judge chain exhausted'."""
        self.successful_qa()
        calls = []
        def reviewer(real, wt, args, timeout, env=None, role='kalite'):
            model = args[args.index('-m') + 1] if '-m' in args else 'yerel'
            calls.append(model)
            if model == 'opencode-go/glm-5.2':
                raise subprocess.CalledProcessError(1, ['opencode'])
            return self.review('GECER')
        chain = (['opencode-go/glm-5.2', 'deepseek/deepseek-v4-pro'], [])
        self.enter(patch.object(core, 'hakem_kucuk_mu', return_value=False))   # large change: router order (B38 in a separate test)
        with patch.object(core, 'capture_opencode', side_effect=reviewer), patch.object(core, 'butce_json', return_value={'ts': 'x'}), \
             patch.object(core.YON, 'zincir', return_value=chain), patch.object(core.YON, 'ariza') as ariza:
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=None)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(calls, ['opencode-go/glm-5.2', 'deepseek/deepseek-v4-pro'])
        self.assertEqual(result['review']['model'], 'deepseek/deepseek-v4-pro')
        ariza.assert_called_once()
        self.assertEqual(ariza.call_args.args[0], 'opencode-go')
        # the chain kicks in also if the model given by the dispatcher fails
        calls.clear()
        with patch.object(core, 'capture_opencode', side_effect=reviewer), patch.object(core, 'butce_json', return_value={'ts': 'x'}), \
             patch.object(core.YON, 'zincir', return_value=chain), patch.object(core.YON, 'ariza'):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='opencode-go/glm-5.2')
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(calls, ['opencode-go/glm-5.2', 'deepseek/deepseek-v4-pro'])
        # all fail -> ERROR, the tried ones in the reason
        calls.clear()
        with patch.object(core, 'capture_opencode', side_effect=subprocess.CalledProcessError(1, ['x'])), patch.object(core, 'butce_json', return_value={'ts': 'x'}), \
             patch.object(core.YON, 'zincir', return_value=chain), patch.object(core.YON, 'ariza'):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=None)
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['review']['status'], 'ERROR')
        self.assertRegex(result['review']['reason'], r'^hakem zinciri tukendi: opencode-go/glm-5\.2: CalledProcessError[^;]*; deepseek/deepseek-v4-pro: CalledProcessError')  # B48: the real reason text is in the record too
        self.assertEqual(len(result['review']['tried']), 2)  # B48: tried links in a structured field

    def test_cancel_stops_the_acceptance_stage_before_qa_and_before_the_reviewer(self) -> None:
        """B110 (18.09): cancel NEVER reached the acceptance stage; after `cadre iptal` QA, the judge round and the judge
        repair went on for minutes and with MONEY."""
        self.successful_qa()
        (self.store.path(self.rid) / 'cancel.flag').write_text('requested\n')
        with patch.object(core, 'qa_kapisi', side_effect=AssertionError('iptalden sonra QA kosmamali')), \
             patch.object(core, 'capture_opencode', side_effect=AssertionError('iptalden sonra hakem cagrilmamali')):
            sonuc = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
        self.assertEqual(sonuc['status'], 'CANCELLED')
        self.assertEqual(self.store.read(self.rid)['final_result']['status'], 'CANCELLED')
        self.assertFalse(approved(self.store.read(self.rid)))

    def test_cancel_during_qa_stops_the_process_and_never_calls_the_reviewer(self):
        import threading
        import time
        Path(self.run['constraints']['qa-komut']).write_text('echo $$ > qa.pid; sleep 3')
        flag = self.store.path(self.rid)/'cancel.flag'
        qa_ready = threading.Event()
        def cancel_when_running():
            deadline = time.monotonic() + 1.5
            while not (self.wt / 'qa.pid').exists() and time.monotonic() < deadline:
                time.sleep(.01)
            if (self.wt / 'qa.pid').exists():
                qa_ready.set()
            flag.write_text('cancel')
        timer = threading.Thread(target=cancel_when_running)
        timer.start()
        self.addCleanup(timer.join)
        started = time.monotonic()
        with patch.dict(os.environ, {'CADRE_KILL_GRACE': '.1'}), patch.object(core, 'capture_opencode') as reviewer:
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli')
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(result['status'], 'CANCELLED')
        self.assertTrue(qa_ready.is_set())
        self.assertFalse(core.pid_yasiyor(int((self.wt/'qa.pid').read_text())))
        reviewer.assert_not_called()

    def test_deleting_a_test_file_never_gets_permission_from_free_text(self) -> None:
        """B140: negative or positive natural language cannot produce an automatic acceptance exception."""
        (self.wt/'test_urun.py').write_text('def test_x():\n    assert True\n')
        self.git('add', '.')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-qm', 'test eklendi')
        temel = self.git('rev-parse', 'HEAD').strip()
        (self.wt/'test_urun.py').unlink()
        self.git('add', '-A')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-qm', 'test silindi')

        def kabul(metin):
            kayit = self.store.accept('a', metin, source_sha=temel)
            self.store.update(kayit['run_id'], worktree=str(self.wt))
            writer = self.store.task_dir(kayit['run_id'], 'impl')/'attempts/fixture/attempt.json'
            writer.parent.mkdir(parents=True)
            writer.write_text(json.dumps({'role': 'impl', 'model': 'fixture/writer', 'pid': 123}))
            Path(kayit['constraints']['qa-komut']).write_text(qa_command())      # this run's QA is green
            with patch.dict(os.environ, {**self.store.environment(kayit['run_id']), 'CADRE_GOREV_ID': '',
                                         'CADRE_OWNER_FDS': ''}), \
                 patch.object(core, 'capture_opencode', return_value=self.review('GECER')), \
                 patch.object(core, 'worktree_base_sha', return_value=temel):
                return core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')

        for metin in ('urunu yaz', 'Testleri silme', 'Do not delete tests',
                      'Never remove tests', 'Eskimis testleri sil ve modulu sadelestir'):
            with self.subTest(prompt=metin):
                sonuc = kabul(metin)
                self.assertEqual(sonuc['status'], 'FAIL')
                self.assertIn('test silindi', sonuc['reason'])
                self.assertEqual(sonuc['silinen_testler'], ['test_urun.py'])

    def test_weakening_a_test_file_blocks_automatic_acceptance_but_legit_edits_pass(self) -> None:
        """B144: a known limit of the deletion gate — a test file could be emptied without being deleted (`assert True`).
        If the assertion count DROPS in a test file that changed against the accepted source there is no automatic acceptance
        (reason and file in the record); a legitimate change that keeps/raises the assertion count does not affect automatic acceptance."""
        (self.wt/'test_urun.py').write_text('def test_a():\n    assert 1 == 1\n    assert 2 == 2\n\n'
                                            'def test_b():\n    assert 3 == 3\n')
        self.git('add', '.')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-qm', 'testler')
        temel = self.git('rev-parse', 'HEAD').strip()
        cases = [
            ('zayif', 'def test_a():\n    assert True\n\ndef test_b():\n    assert True\n', 'FAIL'),
            ('bos', 'def test_a():\n    pass\n\ndef test_b():\n    assert 3 == 3\n', 'FAIL'),
            ('mesru', 'def test_a():\n    assert 1 == 1\n    assert 2 == 2\n    assert 4 == 4\n\n'
                      'def test_b():\n    assert 3 == 3\n', 'PASS'),
            ('yeniden-adlandirma', 'def test_a_renamed():\n    assert 1 == 1\n    assert 2 == 2\n\n'
                                   'def test_b():\n    assert 3 == 3\n', 'PASS'),
        ]
        for ad, icerik, beklenen in cases:
            with self.subTest(case=ad):
                (self.wt/'test_urun.py').write_text(icerik)
                kayit = self.store.accept('a', 'urunu duzelt', source_sha=temel)
                self.store.update(kayit['run_id'], worktree=str(self.wt))
                writer = self.store.task_dir(kayit['run_id'], 'impl')/'attempts/fixture/attempt.json'
                writer.parent.mkdir(parents=True)
                writer.write_text(json.dumps({'role': 'impl', 'model': 'fixture/writer', 'pid': 123}))
                Path(kayit['constraints']['qa-komut']).write_text(qa_command())
                with patch.dict(os.environ, {**self.store.environment(kayit['run_id']), 'CADRE_GOREV_ID': '',
                                             'CADRE_OWNER_FDS': ''}), \
                     patch.object(core, 'capture_opencode', side_effect=self.review_for_prompt('GECER')), \
                     patch.object(core, 'worktree_base_sha', return_value=temel):
                    sonuc = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
                self.assertEqual(sonuc['status'], beklenen, sonuc.get('reason'))
                if beklenen == 'FAIL':
                    self.assertIn('test zayiflatildi', sonuc['reason'])
                    self.assertEqual(sonuc['zayiflatilan_testler'], ['test_urun.py'])

    def test_skipping_every_test_cannot_turn_qa_green(self) -> None:
        """G-C2/G-C7 (B152, audit agent e3): a broken product + `@unittest.skip` → `OK (skipped=N)` rc=0, the same assertion count,
        and `finalize_run` gave PASS. (a) more skips in a changed test file = weakening; (b) even without the test file changing
        (an unconditional skip by deleting a fixture file) QA does not count as PASS if no test ran in the QA output."""
        base_test = ('import os, unittest\nfrom urun import f\n\n'
                     "@unittest.skipUnless(os.path.exists('enable.flag'), 'flag yok')\n"
                     'class T(unittest.TestCase):\n    def test_f(self):\n        self.assertEqual(f(), 1)\n'
                     '    def test_g(self):\n        self.assertTrue(callable(f))\n')
        (self.wt/'urun.py').write_text('def f():\n    return 1\n')
        (self.wt/'test_u.py').write_text(base_test)
        (self.wt/'enable.flag').write_text('1\n')
        self.git('add', '.')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-qm', 'urun+test')
        temel = self.git('rev-parse', 'HEAD').strip()
        cases = [
            # (a) skip only the broken test: QA still runs one test and is green -> the weakening gate must catch it
            ('atlama-eklendi', lambda: (self.wt/'test_u.py').write_text(base_test.replace('    def test_f', "    @unittest.skip('x')\n    def test_f")), 'qa-required-test-skipped'),
            ('fixture-silindi', lambda: (self.wt/'enable.flag').unlink(), 'qa-all-skipped'),
        ]
        for ad, boz, beklenen in cases:
            with self.subTest(case=ad):
                self.git('checkout', '-q', '--', '.')
                if not (self.wt/'enable.flag').exists():
                    (self.wt/'enable.flag').write_text('1\n')
                (self.wt/'urun.py').write_text('def f():\n    return 2\n')     # the product is broken
                boz()
                kayit = self.store.accept('a', 'urunu duzelt', source_sha=temel)
                self.store.update(kayit['run_id'], worktree=str(self.wt))
                writer = self.store.task_dir(kayit['run_id'], 'impl')/'attempts/fixture/attempt.json'
                writer.parent.mkdir(parents=True)
                writer.write_text(json.dumps({'role': 'impl', 'model': 'fixture/writer', 'pid': 123}))
                Path(kayit['constraints']['qa-komut']).write_text("python3 -m unittest discover -p 'test_*.py'")
                with patch.dict(os.environ, {**self.store.environment(kayit['run_id']), 'CADRE_GOREV_ID': '',
                                             'CADRE_OWNER_FDS': ''}), \
                     patch.object(core, 'capture_opencode', side_effect=self.review_for_prompt('GECER')), \
                     patch.object(core, 'worktree_base_sha', return_value=temel):
                    sonuc = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
                self.assertEqual(sonuc['status'], 'FAIL', sonuc)
                self.assertIn(beklenen, sonuc.get('reason', '') + (sonuc.get('qa') or {}).get('reason', ''))

    def test_echoing_the_local_review_template_is_not_a_valid_review(self) -> None:
        """G-C5 (B153, audit agent e2): when the local judge echoed the template lines in the prompt (`KARAR: GECER`, `KRITER main: PASS - <somut kanit>`)
        verbatim, it counted as a valid GECER review. A template echo and placeholder evidence are not a review."""
        self.successful_qa()
        def yansit(real, wt, args, timeout, env=None, role='kalite'):
            prompt = args[-1]
            blok = prompt.split('JSON ureteMIYORSAN', 1)[1]
            return '\n'.join(l for l in blok.splitlines() if l.startswith(('REVIZYON:', 'KARAR:', 'KRITER ')))
        with patch.object(core, 'capture_opencode', side_effect=yansit):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
        self.assertNotEqual(result['status'], 'PASS', result)
        self.assertNotEqual(result['review'].get('status'), 'PASS')
        # at parser level: placeholder evidence and an echoed choice spelling are rejected
        rev = revision(self.wt)
        for metin in (f"REVIZYON: {rev['digest']}\nKARAR: GECER\nKRITER main: PASS - <somut kanit>",
                      f"REVIZYON: {rev['digest']}\nKARAR: GECER|DUZELT\nKRITER main: PASS|FAIL - dosya okundu"):
            self.assertNotEqual(parse_review(metin, rev, ['main'])['status'], 'PASS', metin)
        self.assertNotEqual(parse_review(json.dumps({'revision': rev['digest'], 'verdict': 'GECER',
                                                      'acceptance': [{'id': 'main', 'status': 'PASS', 'evidence': '...'}]}),
                                         rev, ['main'])['status'], 'PASS')

    def test_common_test_file_names_are_protected_by_the_deletion_gate(self) -> None:
        """G-C7 (B157): the gate did not see the deletion of `*.spec.ts`, `__tests__/`, `conftest.py`, `pytest.ini`, `*Test.java`."""
        adlar = ['app/user.spec.ts', 'web/user.spec.js', 'src/__tests__/foo.js', 'conftest.py', 'pytest.ini',
                 'src/UserTest.java', 'tests/test_x.py']
        for ad in adlar:
            (self.wt/ad).parent.mkdir(parents=True, exist_ok=True)
            (self.wt/ad).write_text('x\n')
        self.git('add', '.')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-qm', 'testler')
        temel = self.git('rev-parse', 'HEAD').strip()
        for ad in adlar:
            (self.wt/ad).unlink()
        self.git('add', '-A')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-qm', 'silindi')
        self.assertEqual(sorted(core.silinen_testler(str(self.wt), temel)), sorted(adlar))

    def test_a_provider_less_refusal_falls_back_to_the_conservative_ban(self) -> None:
        """G-B5 (B154, audit agent e4): the plugin's `deny()` receipt came without a provider (`refusals:[{provider:null}]`) and
        `apply_budget_denial` produced no ban → a loop back to the same provider. A provider denial without a provider falls to the cautious
        fallback; a role denial still closes nobody; a denial with a provider closes only that one."""
        for refusals, beklenen in (
            ([{'provider': None, 'scope': 'provider', 'reason': 'plugin deny'}], set((*core.BUTCE_PAID, *core.BUTCE_FREE, 'claude'))),
            ([{'provider': None, 'scope': 'role', 'reason': 'run budget exhausted'}], set()),
            ([{'provider': 'deepseek', 'scope': 'provider', 'reason': 'allocation exhausted'}], {'deepseek'}),
        ):
            with self.subTest(refusals=refusals):
                bans = Mock()
                core.apply_budget_denial(bans, {'refusals': refusals}, None)
                self.assertEqual({c.args[0] for c in bans.block.call_args_list}, beklenen)

    def test_qa_generated_caches_do_not_count_as_a_product_change(self) -> None:
        """G-C2 (B156, audit agent e3): when the QA command produced `__pycache__`, `revision` came out different and the first round
        became `ERROR source-changed-during-qa` (an empty round). Cache/harness leftovers do not enter the product identity."""
        Path(self.run['constraints']['qa-komut']).write_text(qa_command(
            "python3 -c \"import pathlib; d=pathlib.Path('__pycache__'); d.mkdir(exist_ok=True); (d/'a.cpython-314.pyc').write_bytes(b'x')\""))
        with patch.object(core, 'capture_opencode', return_value=self.review('GECER')):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
        self.assertEqual(result['qa']['status'], 'PASS', result['qa'])
        self.assertEqual(result['qa']['reason'], 'command-result')
        self.assertEqual(result['status'], 'PASS', result.get('reason'))

    def test_the_model_that_wrote_the_product_reviews_it_last(self) -> None:
        """B110 (18.09): judge independence was NEVER enforced IN PRODUCTION; the only guard test checked the WRONG chain
        ('review'), while the judge uses the 'kalite' chain. The model that wrote the product is thrown to the end of the chain;
        without another link the review is recorded with an `independent=False` stamp and acceptance is rejected."""
        import butce_kapisi
        self.successful_qa()
        calls = []

        def reviewer(real, wt, args, timeout, env=None, role='kalite'):
            calls.append(args[args.index('-m') + 1] if '-m' in args else 'yerel')
            return self.review('GECER')

        chain = (['opencode-go/glm-5.2', 'deepseek/deepseek-v4-pro'], [])
        with patch.object(core, 'capture_opencode', side_effect=reviewer), \
             patch.object(core, 'butce_json', return_value={'ts': 'x'}), \
             patch.object(core.YON, 'zincir', return_value=chain), \
             patch.object(butce_kapisi, 'kosu_modelleri', return_value={'glm-5.2'}):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=None)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(calls[0], 'deepseek/deepseek-v4-pro', 'urunu yazan model hakem sirasinda one gecemez')
        self.assertTrue(result['review']['independent'])

        calls.clear()
        tek = (['opencode-go/glm-5.2'], [])      # no other link (local is also a writer): no independent acceptance
        with patch.object(core, 'capture_opencode', side_effect=reviewer), \
             patch.object(core, 'butce_json', return_value={'ts': 'x'}), \
             patch.object(core.YON, 'zincir', return_value=tek), \
             patch.object(butce_kapisi, 'kosu_modelleri', return_value={'glm-5.2', 'qwen3.8-27b'}):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=None)
        self.assertEqual(calls, ['opencode-go/glm-5.2'])
        self.assertTrue(result['review']['independent'])
        self.assertIsNotNone(result['review']['session_proof'])
        self.assertEqual(result['status'], 'PASS')
        self.assertTrue(approved(self.store.read(self.rid)))


    def test_judge_whose_two_calls_do_not_fit_the_run_pocket_is_skipped_before_being_called(self) -> None:
        """B242 (26.09 live R14-easy): the Claude judge's first call passed the gate ($0.18 spent), its second call did not fit the run's judge
        pocket; the link was wasted. The breathing filter (B239b's judge counterpart) skips the candidate without calling it; the chain continues."""
        import butce_kapisi
        self.successful_qa()
        calls = []

        def reviewer(real, wt, args, timeout, env=None, role='kalite'):
            calls.append(args[args.index('-m') + 1] if '-m' in args else 'yerel')
            return self.review('GECER')

        chain = (['claude/opus#max', 'opencode/big-pickle'], [])
        nefes = lambda m, b: (1.42, 7.27, 'cep') if m.startswith('claude/') else None
        with patch.object(core, 'capture_opencode', side_effect=reviewer), \
             patch.object(core, 'butce_json', return_value={'ts': 'x'}), \
             patch.object(core.YON, 'zincir', return_value=chain), \
             patch.object(core.YON, 'hakem_nefesi', side_effect=nefes), \
             patch.object(butce_kapisi, 'kosu_modelleri', return_value={'qwen3.8-27b'}):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=None)
        self.assertEqual(calls, ['opencode/big-pickle'], 'nefessiz Claude cagrilmadan atlanir, zincir surer')
        self.assertEqual(result['review']['breathless'], ['claude/opus#max'])
        self.assertEqual(result['status'], 'PASS')
        self.assertIn('B242', self.log.read_text())

    def test_the_last_independent_judge_reviews_read_only(self) -> None:
        """B251 (26.09 live B203d f1): the only independent candidate big-pickle made a small touch to the product -> 'review-changed-product'
        -> no other candidate -> the child failed. The final independent judge runs READ-ONLY: it does not change, it only decides;
        earlier links may make small fixes."""
        import butce_kapisi
        self.successful_qa()
        istemler = {}

        def reviewer(real, wt, args, timeout, env=None, role='kalite'):
            model = args[args.index('-m') + 1] if '-m' in args else 'yerel'
            istemler[model] = args[-1]
            return 'bicimsiz yazi' if model == 'opencode-go/deepseek-v4-pro' else self.review('GECER')

        chain = (['opencode-go/deepseek-v4-pro', 'opencode-go/glm-5.2'], [])
        with patch.object(core, 'capture_opencode', side_effect=reviewer), \
             patch.object(core, 'butce_json', return_value={'ts': 'x'}), \
             patch.object(core.YON, 'zincir', return_value=chain), \
             patch.object(core.YON, 'hakem_nefesi', return_value=None), \
             patch.object(butce_kapisi, 'kosu_modelleri', return_value={'qwen3.8-27b'}):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=None)
        self.assertEqual(result['status'], 'PASS')
        self.assertIn('SALT-OKUR TUR', istemler['opencode-go/deepseek-v4-pro'])
        self.assertNotIn('Kucuk duzeltme yapabilirsin', istemler['opencode-go/deepseek-v4-pro'])
        self.assertIn('SALT-OKUR TUR', istemler['opencode-go/glm-5.2'], 'son bagimsiz hakem salt-okur')
        self.assertNotIn('Kucuk duzeltme yapabilirsin', istemler['opencode-go/glm-5.2'])

    def test_discarded_lane_models_are_not_product_writers(self) -> None:
        """B256: the model of a discarded lane (atildi.flag) does not count as having written the product; it can be an independent judge."""
        yol = self.store.task_dir(self.rid, 'g1-analist-b') / 'attempts/f2/attempt.json'
        yol.parent.mkdir(parents=True)
        yol.write_text(json.dumps({'role': 'impl', 'model': 'deepseek/deepseek-v4-pro', 'pid': 5, 'outcome': 'finished'}))
        self.assertIn(core.model_kimligi('deepseek/deepseek-v4-pro'), core.urunu_yazan_modeller(self.store, self.rid))
        (self.store.task_dir(self.rid, 'g1-analist-b') / 'atildi.flag').write_text('')
        self.assertNotIn(core.model_kimligi('deepseek/deepseek-v4-pro'), core.urunu_yazan_modeller(self.store, self.rid))
        self.assertIn(core.model_kimligi('fixture/writer'), core.urunu_yazan_modeller(self.store, self.rid), 'gercek yazici kalir')

    def _kaydeden_hakem(self, invoked):
        def hakem(real, wt, args, timeout, env=None, role='kalite'):
            invoked.append(args[args.index('-m') + 1] if '-m' in args else 'yerel')
            return self.review()
        return hakem

    def _yazici_siniflamasi(self, result, model, invoked, bagimsiz):
        """B180: the writer classification is now applied by candidate filtering. A given judge that is a writer is removed and never called;
        a given judge that is not a writer does the review and gives an independent PASS."""
        self.assertEqual(core.model_kimligi(model) not in result['review']['writers'], bagimsiz)
        self.assertNotIn(model, result['review']['excluded_writers'])
        self.assertIn(model, invoked)
        self.assertTrue(result['review']['independent'])
        self.assertEqual(result['status'], 'PASS')

    def _hakem_denemesi_yaz(self, attempt_id, model, **ek):
        path = self.store.task_dir(self.rid, 'main')/f'attempts/{attempt_id}/attempt.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'attempt_id': attempt_id, 'role': 'kalite', 'model': model, 'pid': 321,
                                    'outcome': 'finished', **ek}))

    def _hakem_rezervasyonu_yaz(self, attempt_id, model):
        """The plugin receipt of a cloud judge: the main agent says 'build', the engine phase 'kalite', the attempt id comes from the environment."""
        import io

        import butce_kapisi as budget
        from test_butce_donemi import snapshot
        request = {'action': 'reserve', 'provider': 'opencode-go', 'maximum': 1,
                   'metadata': {'role': 'build', 'model': model}}
        env = {'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(LIB.parent), 'CADRE_ROL': 'kalite',
               'CADRE_BUDGET_ATTEMPT': attempt_id}
        with patch.dict(os.environ, env), patch.object(budget, 'observed_budget', return_value=snapshot(5)), \
             patch.object(sys, 'argv', ['butce_kapisi.py']), \
             patch.object(sys, 'stdin', io.StringIO(json.dumps(request))), patch.object(sys, 'stdout', io.StringIO()):
            self.assertEqual(budget.main(), 0)

    def test_restart_keeps_a_recorded_readonly_reviewer_independent(self):
        """§0a/10(a): on a restart earlier judge attempts cautiously counted as WRITERS and the chain was exhausted
        for nothing. A judge RECORDED as not touching the product is not a writer (attempt record + plugin receipt together);
        an attempt that touched it or has an incomplete record stays a writer."""
        self.successful_qa()
        cases = [('okur', {'product_changed': False}, True), ('yazar', {'product_changed': True}, False),
                 ('yarim', {}, False)]
        for attempt_id, kayit, independent in cases:
            judge = f'fixture/judge-{attempt_id}'
            with self.subTest(attempt_id=attempt_id):
                self._hakem_denemesi_yaz(attempt_id, judge, **kayit)
                self._hakem_rezervasyonu_yaz(attempt_id, judge)
                invoked = []
                with patch.object(core, 'capture_opencode', side_effect=self._kaydeden_hakem(invoked)):
                    result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=judge)
                self._yazici_siniflamasi(result, judge, invoked, independent)

    def test_review_attempt_records_whether_it_touched_the_product(self):
        """When a judge attempt ends, whether the product identity changed is written to the attempt record (persistent evidence)."""
        self.successful_qa()
        fake = self.home/'reviewer'
        fake.write_text(f'#!{sys.executable}\nimport json, os, pathlib\n'
                        "if os.environ.get('FIXTURE_TOUCH'): pathlib.Path('touched.txt').write_text('x')\n"
                        'print(' + repr(json.dumps({'type': 'text', 'sessionID': 'ses_r',
                                                    'part': {'text': self.review()}})) + ')\n')
        fake.chmod(0o700)
        for touch, changed in (('', False), ('1', True)):
            with self.subTest(touch=touch), patch.dict(os.environ, {'FIXTURE_TOUCH': touch}), \
                 patch.object(core, 'run_opencode', side_effect=REAL_RUN_OPENCODE), \
                 patch.object(core, 'opencode_budget_env', side_effect=lambda env, args: ['-m', 'opencode-go/glm-5.3', args[-1]]):
                core.finalize_run('a', str(self.wt), self.log, str(fake), review_model='deepseek/deepseek-v4-pro')
                kayitlar = sorted((json.loads(p.read_text()) for p in
                                   (self.store.task_dir(self.rid, 'main')/'attempts').glob('*/attempt.json')),
                                  key=lambda k: k.get('created', 0))
                hakem = [k for k in kayitlar if k.get('role') == 'kalite'][-1]
                self.assertEqual(hakem['product_changed'], changed)
                self.assertEqual(hakem['product_after'] == hakem['product_before'], not changed)

    def test_a_reviewer_that_failed_without_touching_the_product_stays_independent_on_restart(self):
        """B214 (24.09 R5-hard): the record of a judge that failed with a budget denial stayed incomplete; on retry it counted as a writer and the independent
        judge pool shrank (it could have wasted B213's midnight attempt). A failed judge also records whether it touched the product."""
        self.successful_qa()
        fake = self.home/'reviewer'
        fake.write_text(f'#!{sys.executable}\nimport os, pathlib, sys\n'
                        "if os.environ.get('FIXTURE_TOUCH'): pathlib.Path('touched.txt').write_text('x')\n"
                        'sys.exit(75)\n')   # 75 = budget gate denial (capture_opencode -> BudgetDenied)
        fake.chmod(0o700)
        for touch, changed in (('', False), ('1', True)):
            with self.subTest(touch=touch), patch.dict(os.environ, {'FIXTURE_TOUCH': touch}), \
                 patch.object(core, 'run_opencode', side_effect=REAL_RUN_OPENCODE), \
                 patch.object(core, 'opencode_budget_env', side_effect=lambda env, args: ['-m', 'opencode-go/glm-5.3', args[-1]]):
                core.finalize_run('a', str(self.wt), self.log, str(fake), review_model='deepseek/deepseek-v4-pro')
                kayitlar = sorted((json.loads(p.read_text()) for p in
                                   (self.store.task_dir(self.rid, 'main')/'attempts').glob('*/attempt.json')),
                                  key=lambda k: k.get('created', 0))
                hakem = [k for k in kayitlar if k.get('role') == 'kalite'][-1]
                self.assertIs(hakem.get('product_changed'), changed)
                self.assertEqual(core.model_kimligi('opencode-go/glm-5.3') in core.urunu_yazan_modeller(self.store, self.rid), changed)

    def test_dispatch_review_requires_the_original_request_in_addition_to_plan(self):
        self.successful_qa()
        Path(self.run['prompt']).write_text('CSV ve JSON ciktilari sagla; ikisini de test et.')
        (self.store.path(self.rid)/'plan.json').write_text(json.dumps({
            'gorevler': [{'id': 'g1', 'kabul': 'CSV dosyasi var'}]}))
        self.store.update(self.rid, phase='dispatch')
        self.store.task_update(self.rid, 'g1', dispatch_result='uretti')
        with patch.object(core, 'capture_opencode', return_value=self.review(ids=('g1',))) as reviewer:
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
        self.assertNotEqual(result['status'], 'PASS', 'plan ozgun JSON istegini dusuremez')
        payload = json.loads(reviewer.call_args.args[2][-1].splitlines()[-1])
        self.assertEqual(payload['original_request'], Path(self.run['prompt']).read_text())
        self.assertEqual(len(payload['criteria']), 2)

    def test_request_ledger_phase_distinguishes_planner_from_writers_during_acceptance(self):
        import io

        import butce_kapisi as budget
        from test_butce_donemi import snapshot
        self.successful_qa()
        cases = [('plan', 'build', 'planner', True), ('impl', 'build', 'planner', False),
                 ('plan', 'cadre-review', 'helper', False), ('impl', 'compaction', 'compact', True),
                 ('kalite', 'build', 'reviewer', False), ('', 'build', 'unknown', False)]
        for phase, agent, name, independent in cases:
            with self.subTest(phase=phase, agent=agent):
                model = f'fixture/{name}'
                request = {'action': 'reserve', 'provider': 'opencode-go', 'maximum': 1,
                           'metadata': {'role': agent, 'model': model}}
                env = {'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(LIB.parent),
                       'CADRE_ROL': phase}
                with patch.dict(os.environ, env), \
                     patch.object(budget, 'observed_budget', return_value=snapshot(5)), \
                     patch.object(sys, 'argv', ['butce_kapisi.py']), \
                     patch.object(sys, 'stdin', io.StringIO(json.dumps(request))), \
                     patch.object(sys, 'stdout', io.StringIO()):
                    self.assertEqual(budget.main(), 0)
                invoked = []
                with patch.object(core, 'capture_opencode', side_effect=self._kaydeden_hakem(invoked)):
                    result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=model)
                self._yazici_siniflamasi(result, model, invoked, independent)

    def test_a_direct_helper_call_is_never_a_product_writer(self):
        """07.10 smoke: the pre-filter's free groq gpt-oss call (adapter=direct, no tools) counted as a writer; the gpt family was removed from judging."""
        import butce_kapisi as budget
        from test_butce_donemi import snapshot
        ledger = budget.BudgetLedger(self.home / 'zincir/request-budget.db')
        ledger.reserve('opencode-go', 1, snapshot(5), {'adapter': 'direct', 'model': 'openai/gpt-oss-120b', 'run_id': 'a', 'task_id': 'helper'})
        ledger.reserve('opencode-go', 1, snapshot(5), {'role': 'build', 'phase_role': 'impl', 'model': 'deepseek-v4-pro', 'run_id': 'a'})
        self.assertEqual(budget.kosu_modelleri(self.home, 'a', salt_okur_haric=True), {'deepseek-v4-pro'})

    def test_the_planner_family_cannot_judge_the_run_it_planned(self):
        """4b (07.10 user): judge pool = plan pool; the family of the model that made the plan in that run cannot be the judge."""
        import butce_kapisi
        self.successful_qa()
        self.store.update(self.rid, plan_modeli='opencode-go/glm-5.3')
        calls = []
        def hakem(real, wt, args, timeout, env=None, role='kalite'):
            calls.append(args[args.index('-m') + 1] if '-m' in args else '?')
            return self.review('GECER')
        with patch.object(core, 'capture_opencode', side_effect=hakem), \
             patch.object(core, 'butce_json', return_value={'ts': 'x'}), \
             patch.object(core.YON, 'zincir', return_value=(['opencode-go/glm-5.3', 'opencode-go/kimi-k3'], [])), \
             patch.object(butce_kapisi, 'kosu_modelleri', return_value={'deepseek-v4-pro'}):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=None)
        self.assertEqual(calls, ['opencode-go/kimi-k3'])
        self.assertNotIn('opencode-go/glm-5.3', result['review']['excluded_writers'])
        self.assertEqual(result['status'], 'PASS')

    def test_the_writer_cannot_review_its_own_product(self):
        self.successful_qa()
        path = self.store.task_dir(self.rid, 'g1')/'attempts/local/attempt.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'role': 'impl', 'model': 'cerebras/qwen-3.8-27b', 'pid': 123}))
        with patch.object(core, 'capture_opencode', return_value=self.review()):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='cerebras/qwen-3.8-27b')
        self.assertTrue(result['review']['independent'])
        self.assertEqual(result['status'], 'PASS')
        self.assertIsNotNone(result['review']['session_proof'])

    def test_cheap_writer_stays_behind_a_more_expensive_independent_reviewer(self):
        self.successful_qa()
        with patch('butce_kapisi.kosu_modelleri', return_value={'gemini-3-flash-preview'}), \
             patch.object(core, 'butce_json', return_value={}), \
             patch.object(core.YON, 'zincir', return_value=(['google/gemini-3-flash-preview', 'deepseek/deepseek-v4-pro'], [])), \
             patch.object(core, 'capture_opencode', return_value=self.review()) as reviewer:
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli')
        self.assertEqual(reviewer.call_args.args[2][1], 'deepseek/deepseek-v4-pro')
        self.assertTrue(result['review']['independent'])

    def test_reviewer_budget_denial_never_trips_the_provider_circuit(self):
        self.successful_qa()
        with patch.object(core, 'capture_opencode', side_effect=core.BudgetDenied('run budget exhausted')), \
             patch.object(core.YON, 'ariza') as circuit:
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='deepseek/deepseek-v4-pro')
        circuit.assert_not_called()
        self.assertEqual(result['status'], 'FAIL')

    def test_real_executor_model_swap_cannot_keep_the_requested_cloud_identity(self):
        self.successful_qa()
        fake = self.home/'reviewer'
        fake.write_text(f'#!{sys.executable}\nimport json\nprint(' + repr(json.dumps({
            'type': 'text', 'sessionID': 'ses_fallback',
            'part': {'text': self.review()}})) + ')\n')
        fake.chmod(0o700)
        with patch.object(core, 'run_opencode', side_effect=REAL_RUN_OPENCODE), \
             patch.object(core, 'opencode_budget_env', side_effect=lambda env, args: ['-m', 'cerebras/qwen-3.8-27b', args[-1]]), \
             patch('butce_kapisi.kosu_modelleri', return_value={'cerebras/qwen-3.8-27b'}):
            result = core.finalize_run('a', str(self.wt), self.log, str(fake), review_model='deepseek/deepseek-v4-pro')
        self.assertEqual(result['review']['model'], 'cerebras/qwen-3.8-27b')
        self.assertTrue(result['review']['independent'])
        self.assertEqual(result['status'], 'PASS')
        self.assertIsNotNone(result['review']['session_proof'])

    def test_reviewer_attempts_start_fresh_after_a_sessionless_failure(self):
        """G-A4/G-C10 (B151, 22.09 audit agent): judge attempts TOOK OVER the previous attempt like `impl`. If the first judge
        failed without leaving a session receipt (rc!=0, no events) the next candidates never started with `SessionRecoveryError` ->
        acceptance permanent FAIL; if it left a receipt the second judge continued the first one's `--session` -> the "independent" stamp
        inside the first judge's conversation. Every judge attempt is a NEW scope."""
        self.successful_qa()
        fake = self.home/'reviewer'
        fake.write_text(f'#!{sys.executable}\nimport json, os, pathlib, sys\n'
                        "p = pathlib.Path(os.environ['CADRE_HOME']); n = len(list(p.glob('call-*'))) + 1\n"
                        "(p / f'call-{n}').write_text(json.dumps(sys.argv[1:]))\n"
                        "if n == 1: raise SystemExit(1)\n"   # the first judge: fails without a session
                        'print(' + repr(json.dumps({'type': 'text', 'sessionID': 'ses_second',
                                                    'part': {'text': self.review()}})) + ')\n')
        fake.chmod(0o700)
        with patch.object(core, 'run_opencode', side_effect=REAL_RUN_OPENCODE), \
             patch.object(core, 'opencode_budget_env', side_effect=lambda env, args: list(args)), \
             patch.object(core, 'butce_json', return_value={'ts': 'x'}), \
             patch.object(core.YON, 'zincir', return_value=(['opencode-go/first', 'opencode/second'], [])), \
             patch.object(core.YON, 'ariza'):
            result = core.finalize_run('a', str(self.wt), self.log, str(fake), review_model=None)
        calls = [json.loads(p.read_text()) for p in sorted(self.home.glob('call-*'))]
        self.assertEqual(len(calls), 2, 'ikinci hakem adayi hic baslamadi')
        self.assertNotIn('--session', calls[1], 'ikinci hakem ilkinin oturumunu devraldi')
        self.assertEqual(result['status'], 'PASS', result.get('reason'))
        self.assertEqual(result['review']['model'], 'opencode/second')

    def test_small_change_prefers_cheap_reviewer_and_malformed_review_falls_through(self) -> None:
        """B38: on a small change (fixture: 1 file, 2 lines) the judge chain goes from cheap to expensive (go -> zen/OR -> claude; no free since 4c-11).
        B37b: a judge returning malformed JSON is a model failure -> next link; it stops when a valid review comes."""
        self.successful_qa()
        calls = []
        def reviewer(real, wt, args, timeout, env=None, role='kalite'):
            model = args[args.index('-m') + 1] if '-m' in args else 'yerel'
            calls.append(model)
            if model == 'opencode-go/glm-5.3-flash':
                return '{"verdict": "GECER"}'   # missing: no revision/acceptance -> parse ERROR
            return self.review('GECER')
        chain = (['claude/opus#max', 'opencode/deepseek-v4-pro', 'opencode-go/glm-5.3-flash', 'opencode-go/glm-5.2'], [])
        with patch.object(core, 'capture_opencode', side_effect=reviewer), patch.object(core, 'butce_json', return_value={'ts': 'x'}), \
             patch.object(core.YON, 'zincir', return_value=chain), patch.object(core.YON, 'ariza'):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=None)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(calls, ['opencode-go/glm-5.3-flash', 'opencode-go/glm-5.2'])
        self.assertEqual(result['review']['model'], 'opencode-go/glm-5.2')
        self.assertTrue(core.hakem_kucuk_mu(str(self.wt), self.base))
        # B38b (live 03:15): on a small change the expensive model given by the dispatcher also goes behind the cheap ones
        calls.clear()
        with patch.object(core, 'capture_opencode', side_effect=reviewer), patch.object(core, 'butce_json', return_value={'ts': 'x'}), \
             patch.object(core.YON, 'zincir', return_value=chain), patch.object(core.YON, 'ariza'):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='opencode/deepseek-v4-pro')
        self.assertEqual(calls[0], 'opencode-go/glm-5.3-flash')
        self.assertEqual(result['status'], 'PASS')
        # large change: the router order is kept (claude first, but capture_opencode does not go to claude -> the run_claude path; only the order is measured here)
        (self.wt/'big.txt').write_text('x\n' * 400)
        self.git('add', '.')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-qm', 'big')
        self.assertFalse(core.hakem_kucuk_mu(str(self.wt), self.base))
        calls.clear()
        chain = (['opencode-go/glm-5.2', 'opencode-go/glm-5.3-flash'], [])
        with patch.object(core, 'capture_opencode', side_effect=reviewer), patch.object(core, 'butce_json', return_value={'ts': 'x'}), \
             patch.object(core.YON, 'zincir', return_value=chain), patch.object(core.YON, 'ariza'):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=None)
        self.assertEqual(calls, ['opencode-go/glm-5.2'])
        self.assertEqual(result['status'], 'PASS')

    def test_size_rule_precedes_the_guarantee_and_the_guarantee_moves(self) -> None:
        """4c-5: the guarantee was reserved for Go glm with a small estimate. On a small change Go is first anyway (no move); if the real change
        is large Sol comes first and the guarantee moves to Sol; if it cannot move, the judge with money reserved goes first."""
        self.successful_qa()
        calls = []
        def reviewer(real, wt, args, timeout, env=None, role='kalite'):
            calls.append(args[args.index('-m') + 1])
            return self.review('GECER')
        sol, glm = 'openrouter/openai/gpt-6.1-sol', 'opencode-go/glm-5.3'
        self.store.update(self.rid, kabul_guvencesi={'modeller': {'kalite': glm}, 'adaylar': [glm]})
        def kos(tasindi):
            calls.clear()
            with patch.object(core, 'capture_opencode', side_effect=reviewer), patch.object(core, 'butce_json', return_value={'ts': 'x'}), \
                 patch.object(core.YON, 'zincir', return_value=([sol, glm], [])), patch.object(core.YON, 'ariza'), \
                 patch.object(core, 'guvence_tasi', return_value=tasindi) as tasi:
                result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=None)
            self.assertEqual(result['status'], 'PASS')
            return tasi
        self.assertFalse(kos(True).called, 'kucuk degisiklikte Go once: guvenceli hakem zaten basta')
        self.assertEqual(calls[0], glm)
        (self.wt/'big.txt').write_text('x\n' * 400)
        self.git('add', '.')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-qm', 'big')
        tasi = kos(True)
        self.assertEqual((calls[0], tasi.call_args.args[2]), (sol, sol))
        kos(False)
        self.assertEqual(calls[0], glm, 'tasinamazsa parasi ayrilmis hakem one gecer')

    def test_malformed_review_gets_one_format_repair_from_the_same_reviewer(self) -> None:
        """B312 (live 02.10 14:06): the judge broke a correct review with an unescaped quote in the evidence text, the chain FAILed with one candidate.
        On a format error the same judge is asked ONCE for a format repair only; if it comes back valid the chain stops there."""
        self.successful_qa()
        calls, prompts = [], []
        def reviewer(real, wt, args, timeout, env=None, role='kalite'):
            model = args[args.index('-m') + 1] if '-m' in args else 'yerel'
            calls.append(model); prompts.append(args[-1])
            if 'BICIM ONARIMI' in args[-1]:
                self.assertEqual((env or {}).get('CADRE_SALT_OKUR'), '1')
                return self.review('GECER')
            return self.review('GECER').replace('"evidence": "', '"evidence": "cift kacis &#8220;→", ', 1)
        chain = (['opencode-go/qwen3.7-plus'], [])
        with patch.object(core, 'capture_opencode', side_effect=reviewer), patch.object(core, 'butce_json', return_value={'ts': 'x'}), \
             patch.object(core.YON, 'zincir', return_value=chain), patch.object(core.YON, 'ariza'):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=None)
        self.assertEqual(calls, ['opencode-go/qwen3.7-plus', 'opencode-go/qwen3.7-plus'])
        self.assertIn('review JSON invalid', prompts[1])
        self.assertEqual((result['status'], result['review']['model']), ('PASS', 'opencode-go/qwen3.7-plus'))

    def test_invalid_outer_review_json_is_reported_as_invalid_not_as_missing_identity(self) -> None:
        """B312(a): when the outer object could not be decoded an inner object (an acceptance item) became a candidate and the reason came out as a misleading 'no identity'."""
        from sonuc import parse_review
        identity = revision(self.wt)
        good = {'revision': identity['digest'], 'verdict': 'GECER', 'acceptance': [{'id': 'main', 'status': 'PASS', 'evidence': 'X tamam'}]}
        bozuk = json.dumps(good, ensure_ascii=False).replace('X tamam', '&#8220;→", tamam')
        result = parse_review(bozuk, identity, ['main'])
        self.assertEqual(result['status'], 'ERROR')
        self.assertTrue(result['reason'].startswith('review JSON invalid'), result['reason'])

    def test_fenced_review_rejection_reason_is_not_hidden_by_a_brace_in_prose(self) -> None:
        from sonuc import parse_review
        identity = revision(self.wt)
        yanlis = {'revision': identity['digest'], 'verdict': 'GECER',
                  'acceptance': [{'id': 'acceptance-1', 'status': 'PASS', 'evidence': 'X tamam'}]}
        metin = '| K3 | `/items/{identifier}` kontrolu |\n\n```json\n' + json.dumps(yanlis) + '\n```\n'
        result = parse_review(metin, identity, ['main'])
        self.assertEqual((result['status'], result['reason']), ('ERROR', 'unknown or duplicate acceptance criterion'))
        dogru = metin.replace('acceptance-1', 'main')
        self.assertEqual(parse_review(dogru, identity, ['main'])['status'], 'PASS')

    def test_review_json_with_invalid_backslash_escapes_is_read_but_identity_and_coverage_stay_strict(self) -> None:
        from sonuc import parse_review
        identity = revision(self.wt)
        good = {'revision': identity['digest'], 'verdict': 'GECER', 'acceptance': [{'id': 'main', 'status': 'PASS', 'evidence': 'X tamam'}]}
        bozuk = json.dumps(good, ensure_ascii=False).replace('X tamam', "satir 7 \\'abc\\' bekler; desen \\d+ ve C:\\users")
        with self.assertRaises(ValueError):
            json.loads(bozuk)
        for planning in (False, True):
            result = parse_review('Inceleme sonucu:\n' + bozuk, identity, ['main'], planning=planning)
            self.assertEqual(result['status'], 'PASS', result)
            self.assertEqual(result['acceptance'][0]['evidence'], "satir 7 'abc' bekler; desen \\d+ ve C:\\users")
        yanlis = bozuk.replace(identity['digest'], '0' * 64)
        self.assertNotEqual(parse_review(yanlis, identity, ['main'])['status'], 'PASS')
        self.assertNotEqual(parse_review(bozuk, identity, ['main', 'eksik'])['status'], 'PASS')
        catisan = bozuk + '\n' + json.dumps({**good, 'verdict': 'DUZELT', 'acceptance': [{'id': 'main', 'status': 'FAIL', 'evidence': 'yok'}]})
        self.assertEqual(parse_review(catisan, identity, ['main'])['status'], 'ERROR')

    def test_review_json_tolerates_prose_wrapping_and_criteria_key_but_not_identity_or_coverage(self) -> None:
        """B39 (live 01:59/02:24): gemini gave the right verdict with a 'criteria' key and inside plain text, and the schema rejected it for a technical reason.
        The packaging and the key name are tolerant; the product identity and scope check stay just as strict."""
        from sonuc import parse_review
        identity = revision(self.wt)
        good = {'revision': identity['digest'], 'verdict': 'GECER', 'criteria': [{'id': 'main', 'status': 'PASS', 'evidence': 'dosya var; lint 0'}]}
        wrapped = '## Inceleme\nSonuc asagida:\n' + json.dumps(good, ensure_ascii=False) + '\nTesekkurler.'
        result = parse_review(wrapped, identity, ['main'])
        self.assertEqual((result['status'], result['verdict']), ('PASS', 'GECER'))
        self.assertEqual(result['acceptance'][0]['id'], 'main')
        bad_identity = {**good, 'revision': 'deadbeef'}
        self.assertEqual(parse_review(json.dumps(bad_identity), identity, ['main'])['status'], 'ERROR')
        d = identity['digest']
        kopya_hatasi = {**good, 'revision': d[:32] + d[40:]}
        self.assertEqual(parse_review(json.dumps(kopya_hatasi), identity, ['main'])['status'], 'PASS', 'C01 canli: ortadan 8 karakter dusen kimlik')
        for kotu in (d[:15], ('0' if d[0] != '0' else '1') + d[1:], d[:15] + 'g' + d[16:]):
            with self.subTest(kotu=kotu):
                self.assertEqual(parse_review(json.dumps({**good, 'revision': kotu}), identity, ['main'])['status'], 'ERROR')
        missing = {'revision': identity['digest'], 'verdict': 'GECER', 'criteria': []}
        self.assertEqual(parse_review(json.dumps(missing), identity, ['main'])['reason'], 'acceptance coverage incomplete')
        self.assertEqual(parse_review('## Objective\nno json here', identity, ['main'])['status'], 'ERROR')

    def test_review_pass_items_tolerate_null_gap_kind_but_unknown_kinds_stay_rejected(self) -> None:
        from sonuc import parse_review
        identity = revision(self.wt)
        items = [{'id': 'main', 'status': 'PASS', 'evidence': 'dosya var; lint 0', 'eksik': None},
                 {'id': 'tests', 'status': 'PASS', 'evidence': 'testler korundu', 'eksik': ''}]
        good = {'revision': identity['digest'], 'verdict': 'GECER', 'acceptance': items}
        result = parse_review(json.dumps(good), identity, ['main', 'tests'])
        self.assertEqual((result['status'], result.get('verdict'), result.get('reason')), ('PASS', 'GECER', None), result)
        bad = {**good, 'acceptance': [{**items[0], 'eksik': 'belirsiz'}, items[1]]}
        self.assertEqual(parse_review(json.dumps(bad), identity, ['main', 'tests'])['reason'], 'invalid gap kind')

    def test_large_untracked_product_preserves_reviewer_priority_in_finalization(self):
        self.successful_qa()
        (self.wt/'new_product.py').write_text('x = 1\n' * 301)
        chain = (['opencode-go/glm-5.2', 'google/gemini-3-flash-preview'], [])
        with patch.object(core.YON, 'zincir', return_value=chain), \
             patch.object(core, 'butce_json', return_value={}), \
             patch.object(core, 'capture_opencode', return_value=self.review()) as reviewer:
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli')
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(reviewer.call_args.args[2][1], 'opencode-go/glm-5.2')

    def test_untracked_size_counts_unterminated_lines_and_tracked_changes_together(self):
        path = self.wt/'new\nfile.txt'
        head = self.git('rev-parse', 'HEAD').strip()
        for lines, small in ((0, True), (300, True), (301, False)):
            with self.subTest(lines=lines):
                path.write_text('\n'.join(['x'] * lines))
                self.assertEqual(core.hakem_kucuk_mu(self.wt, head), small)
        path.write_text('x\n' * 299)
        # The product.txt change against the source is two lines: 299 + 2 > 300.
        self.assertFalse(core.hakem_kucuk_mu(self.wt, self.base))

    def test_unknown_untracked_size_never_enables_small_change_routing(self):
        path = self.wt/'new.dat'
        path.write_bytes(b'\x00binary')
        self.assertFalse(core.hakem_kucuk_mu(self.wt, self.base))
        path.unlink()
        path.symlink_to(self.wt/'product.txt')
        self.assertFalse(core.hakem_kucuk_mu(self.wt, self.base))
        path.unlink()
        path.write_text('short\n')
        with patch.object(Path, 'open', side_effect=PermissionError('unreadable')):
            self.assertFalse(core.hakem_kucuk_mu(self.wt, self.base))
        self.assertFalse(core.hakem_kucuk_mu(self.wt, 'missing-source-sha'))

    def test_quality_failure_or_timeout_cannot_pass(self) -> None:
        self.successful_qa()
        for error in (subprocess.TimeoutExpired('fake', 1), subprocess.CalledProcessError(2, 'fake')):
            with self.subTest(error=error), patch.object(core, 'capture_opencode', side_effect=error):
                result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
                # B37: an executor error no longer blows up acceptance, the judge chain is exhausted -> verdict FAIL, judge ERROR; still not PASS
                self.assertNotEqual(result['status'], 'PASS')
                self.assertEqual(result['review']['status'], 'ERROR')
                self.assertIn('hakem zinciri tukendi', result['review']['reason'])
                self.assertFalse(approved(self.store.read(self.rid)))

    def test_review_changes_force_new_qa_and_review_of_same_revision(self) -> None:
        self.successful_qa()
        calls = []
        def reviewer(*args, **kwargs):
            current = revision(self.wt)
            calls.append(current)
            if len(calls) == 1:
                (self.wt/'product.txt').write_text('review fixed the product\n')
            return self.review(identity=current)
        with patch.object(core, 'capture_opencode', side_effect=reviewer), patch.object(core, 'qa_kapisi', wraps=core.qa_kapisi) as qa, \
             patch.object(core, 'butce_json', return_value={}), \
             patch.object(core.YON, 'zincir', return_value=(['opencode-go/repairer', 'opencode-go/reviewer'], [])):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=None)
        self.assertEqual(len(calls), 1)
        self.assertEqual(qa.call_count, 1)
        self.assertEqual(result['status'], 'FAIL')
        self.assertFalse(result['review']['independent'])
        (self.wt/'product.txt').write_text('authorized repair outside review\n')
        with patch.object(core, 'capture_opencode', side_effect=lambda *a, **k: self.review()):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='opencode-go/reviewer')
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['review']['model'], 'opencode-go/reviewer')
        self.assertEqual(result['qa']['revision'], result['review']['revision'])
        self.assertTrue(approved(self.store.read(self.rid)))
        (self.wt/'product.txt').write_text('changed after acceptance\n')
        self.assertFalse(approved(self.store.read(self.rid)))

    def test_reviewer_changes_cannot_expand_the_contract_repair_limit(self):
        """G-C6/D06 (01.10 Codex): the repair allowance (limit 1) is used by a's repair; b is called read-only. If b still
        changes the product the change is not accepted: the chain stops, FAIL `review-repair-limit` is fixable (a G-C16 round opens)."""
        self.successful_qa()
        calls = []
        def repair(*args, **kwargs):
            calls.append(args[2][1])
            (self.wt/'product.txt').write_text(f'repair {len(calls)}\n')
            return self.review()
        with patch.object(core, 'capture_opencode', side_effect=repair), \
             patch.object(core, 'butce_json', return_value={}), \
             patch.object(core.YON, 'zincir', return_value=(['opencode-go/a', 'opencode-go/b', 'opencode-go/c'], [])):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli')
        self.assertEqual(calls, ['opencode-go/a'])
        self.assertEqual((result['status'], result['review']['reason']), ('FAIL', 'review-repair-limit'))
        self.assertIsNotNone(core.hata_imzasi(result), 'tavan asimi kontrollu duzeltme turuna gider')

    def test_salt_okur_hakem_yazsa_da_tavan_asan_degisiklik_pass_alamaz(self):
        """D06: four judges; the first three change the product, the last accepts. It used to be PASS, repairs=3/limit=1, QA=4."""
        self.successful_qa()
        calls = []
        def hakem(real, wt, args, timeout, env=None, role='kalite'):
            calls.append(args[args.index('-m') + 1])
            if len(calls) <= 3:
                (self.wt/'product.txt').write_text(f'repair {len(calls)}\n')
            return self.review()
        with patch.object(core, 'capture_opencode', side_effect=hakem), patch.object(core, 'butce_json', return_value={}), \
             patch.object(core.YON, 'zincir', return_value=(['opencode-go/a', 'opencode-go/b', 'opencode-go/c', 'opencode-go/d'], [])), \
             patch.object(core, 'qa_kapisi', wraps=core.qa_kapisi) as qa:
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli')
        self.assertNotEqual(result['status'], 'PASS')
        self.assertEqual(calls, ['opencode-go/a'])
        self.assertEqual(qa.call_count, 1, 'salt-okur yazimi kabul ve inline onarim uretemez')
        self.assertLessEqual(result['review']['repair_limit'], 1)

    def test_onarim_tavani_dolunca_salt_okur_son_hakem_karar_verir(self):
        """B273 (30.09 rehearsal, child 064142): when the cap was reached the chain was cut without a verdict -> no correction round. D06: once the allowance is used,
        the next link is called TECHNICALLY read-only (CADRE_SALT_OKUR -> write permissions closed) and decides."""
        self.successful_qa()
        calls = []
        def hakem(real, wt, args, timeout, env=None, role='kalite'):
            model = args[args.index('-m') + 1]
            calls.append(model)
            if model == 'opencode-go/a':
                self.assertEqual((env or {}).get('CADRE_SALT_OKUR'), '1')
                (self.wt/'product.txt').write_text(f'repair {len(calls)}\n')
                return self.review()
            self.assertIn('SALT-OKUR TUR', args[-1], 'onarim hakki dolunca sonraki hakem salt-okur cagrilmali')
            self.assertEqual((env or {}).get('CADRE_SALT_OKUR'), '1')
            return self.review('DUZELT')
        with patch.object(core, 'capture_opencode', side_effect=hakem), patch.object(core, 'butce_json', return_value={}), \
             patch.object(core.YON, 'zincir', return_value=(['opencode-go/a', 'opencode-go/b', 'opencode-go/c'], [])):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli')
        self.assertEqual(calls, ['opencode-go/a'])
        self.assertEqual((result['status'], result['review']['reason'], result['review']['model']), ('FAIL', 'review-repair-limit', 'opencode-go/a'))
        self.assertIsNotNone(core.hata_imzasi(result), 'DUZELT verdict ile duzeltme turu acilabilmeli')

    def test_salt_okur_bayragi_opencode_yazma_izinlerini_kapatir(self):
        env = {'CADRE_SALT_OKUR': '1'}
        go = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'bugun': time.strftime('%Y-%m-%d'),
              'go': {'db': 'ok', 'pay': 6.666667, 'pencere_acik': True, 'kullanilabilir': 6.666667, 'birim': 'puan'}}
        with patch.object(core, 'butce_json', return_value=go):   # P3: no local, a read-only executor in the cloud
            try:
                core.opencode_budget_env(env, ['-m', 'opencode-go/glm-5.3-flash', 'incele'])
            except Exception as exc:   # in a test home without an observation no cloud can be chosen; the permission block must still be written
                self.fail(f'config kurulamadi: {exc}')
        izin = json.loads(env['OPENCODE_CONFIG_CONTENT'])['permission']
        self.assertEqual(izin['edit'], 'deny')
        self.assertEqual(izin['bash'], 'allow')
        self.assertEqual(izin['read'], 'deny')
        from oturum import project_tool_policy
        policy = project_tool_policy(self.store, self.store.read(self.rid), self.home / 'readonly-check',
                                     str(self.wt), 'opencode', 'review', {**os.environ, **env})
        self.assertFalse(policy['writable'])
        denied = subprocess.run([policy['sandbox'], '-p', policy['profile'], sys.executable, '-c',
            "from pathlib import Path; Path('product.txt').write_text('unauthorized')"],
            cwd=self.wt, capture_output=True, timeout=5)
        self.assertNotEqual(denied.returncode, 0)
        self.assertEqual((self.wt / 'product.txt').read_text(), 'after\n')

    def test_incomplete_stale_or_text_only_review_is_rejected(self) -> None:
        current = revision(self.wt)
        for raw in ('HUKUM: GECER', self.review(ids=()), self.review(identity={'digest':'old'})):
            with self.subTest(raw=raw):
                self.assertEqual(parse_review(raw, current, ['main'])['status'], 'ERROR')
        with patch('sonuc.revision', side_effect=RuntimeError('HEAD changed during snapshot')):
            self.assertFalse(approved(self.store.read(self.rid)))

    def test_missing_worker_result_prevents_finalization(self) -> None:
        self.successful_qa()
        (self.store.path(self.rid)/'plan.json').write_text(json.dumps({'gorevler':[{'id':'g1','kabul':'works'}]}))
        self.store.update(self.rid, phase='dispatch')
        with patch.object(core, 'capture_opencode') as reviewer:
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
        self.assertEqual(result['status'], 'ERROR')
        reviewer.assert_not_called()

    def test_review_covers_entire_run_from_accepted_sha(self) -> None:
        self.successful_qa()
        (self.wt/'second.txt').write_text('second commit\n')
        self.git('add', '.')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-qm', 'second')
        with patch.object(core, 'capture_opencode', return_value=self.review()) as reviewer:
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
        self.assertEqual(result['status'], 'PASS')
        instruction = reviewer.call_args.args[2][-1]
        self.assertIn(f'kaynak {self.base}', instruction)
        self.assertIn('second.txt', instruction, 'ikinci commit dahil butun kosu kapsami hakeme listelenir')
        self.assertIn('product.txt', instruction)
        self.assertNotIn('HEAD~1', instruction)
        self.assertNotIn('git diff', instruction, 'inceleme oturumunda kabuk yok; git talimati verilmez')

    def test_reviewer_call_disables_subagent_delegation(self) -> None:
        """B202 (24.09 R2-medium-v2): the judge (big-pickle) handed the review to the cadre-review sub-agent, and the code-writing cadre-impl was open too;
        it was cut at the 15 min limit and independence could be diluted through the sub-agent's model. The judge reviews itself (4c-9: no sub-agents)."""
        self.successful_qa()
        with patch.object(core, 'capture_opencode', return_value=self.review()) as reviewer:
            core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
        env_ek = reviewer.call_args.args[4] if len(reviewer.call_args.args) > 4 else reviewer.call_args.kwargs.get('env_ek')
        self.assertEqual((env_ek or {}).get('CADRE_ALT_AJANSIZ'), '1')

    def dispatch_fixture(self, verdict: str, *, tasks=None, worker=None, env=None, plan_ek=None, faz='dispatch') -> int:
        self.successful_qa()
        root = self.home/'dispatcher'
        from qa_fixture import instructions
        instructions(root)
        (root/'lib').mkdir(parents=True)
        (root/'bin').mkdir()
        runner = Path(__file__).with_name('qa_fixture.py')
        shutil.copyfile(runner, root / 'lib/qa_fixture.py')
        qa_path = Path(self.run['constraints']['qa-komut'])
        qa_path.write_text(qa_path.read_text().replace(str(runner.resolve()), str(root / 'lib/qa_fixture.py')))
        for name in ('arsiv.py', 'kosu.py','sonuc.py','oturum.py','qe_core.py', 'canlilik.py', 'kabul.py', 'motor.py','dispatch_util.py','butce_kapisi.py', 'go_pencere.py', 'sinirlar.py', 'sinirlar.json', 'sinirlar.mjs', 'opencode_adaptor.py', 'butce-plugin.mjs', 'cadre-plugin.mjs', 'aylik-butce-plugin.mjs',
                     'yonlendirici.py', 'model_secimi.py', 'katalog.py', 'sozlesme.py', 'seviye.py', 'onelek.py', 'bolme.py', 'standart-gorev-formu.txt'):
            shutil.copyfile(LIB/name, root/'lib'/name)
        for source in LIB.glob('*.py'):
            if not (root / 'lib' / source.name).exists():
                shutil.copyfile(source, root / 'lib' / source.name)
        shutil.copytree(LIB / 'qa-sim', root / 'lib/qa-sim', ignore=shutil.ignore_patterns('__pycache__'), dirs_exist_ok=True)
        shutil.copyfile(LIB.parent/'bin/cadre-dispatch.sh', root/'bin/cadre-dispatch.sh')
        # P3 (03.10): no local -> the judge/repair chain needs the cloud; a fake open Go budget for the dispatcher subprocess (no network)
        import test_uctan_uca
        (root/'lib/butce.py').write_text(test_uctan_uca.FAKE_BUTCE)
        from test_kosu_sessions import KATALOG
        (self.home/'models.json').write_text(json.dumps(KATALOG))
        self.enter(patch.dict(os.environ, {'CADRE_MODEL_KATALOG': str(self.home/'models.json')}))
        self.enter(patch.object(core.YON, 'QE', self.home))
        self.enter(patch.object(core, 'butce_json', return_value={'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'bugun': time.strftime('%Y-%m-%d'),
            'go': {'db': 'ok', 'pay': 33.333334, 'pencere_acik': True, 'kullanilabilir': 33.333334, 'birim': 'puan'}}))   # the judge selection is in this process
        (root/'lib/common.sh').write_text('qe_verify_ai_worktree(){ return 0; }\nqe_make_worktree(){ return 99; }\nqe_commit_wip(){ echo 0; }\n')
        (root/'bin/is-motoru.sh').write_text('python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" task "$CADRE_RUN_ID" "$CADRE_GOREV_ID" uretti\n')
        if worker is not None:
            (root/'worker.py').write_text(worker)
            (root/'bin/is-motoru.sh').write_text(f'exec "{sys.executable}" "$CADRE_ENGINE_ROOT/worker.py" "$@"\n')
        plan = {'is_id':'fixture','gorevler':[{'id':'g1','tanim':'edit product','model':'auto','paralel_grup':0,
                                            'dosyalar':['product.txt'],'kabul':'product works','zorluk':2,'tahmin_dk':1}]}
        if tasks is not None:
            plan['gorevler'] = tasks
        plan.update(plan_ek or {})
        if faz == 'dispatch':   # 'motor': the plain engine path without a plan (D13)
            (self.store.path(self.rid)/'plan.json').write_text(json.dumps(plan))
        self.store.update(self.rid, phase=faz)
        fake = self.home/'fake-reviewer'
        fake.write_text(f'#!{sys.executable}\n'+'''
import os,sys,json
if sys.argv[-1].startswith('PLAN ISTEK UYUMU'):
    question=json.loads(sys.argv[-1].splitlines()[-1])
    answer={'revision':question['revision'],'verdict':'GECER','acceptance':[{'id':c['id'],'status':'PASS','evidence':'product.txt and source criteria inspected'} for c in question['criteria']]}
    sid='ses_plan_review_'+os.environ['CADRE_GOREV_ID']
    print(json.dumps({'type':'text','sessionID':sid,'part':{'type':'text','text':json.dumps(answer)}}))
    raise SystemExit(0)
if 'ALT GOREVLERE BOL' in sys.argv[-1]:   # G-E10 bolme planlayicisi (hakem sayacina girmez)
    print(json.dumps({'type':'text','sessionID':'ses_bol','part':{'type':'text','text':os.environ.get('FIXTURE_ALT_PLAN','alt plan yok')}}))
    raise SystemExit(0)
calls=os.path.join(os.path.dirname(sys.argv[0]), 'reviewer-calls')
open(calls, 'a').write('x')
seq=os.environ.get('FIXTURE_VERDICTS','').split(',') if os.environ.get('FIXTURE_VERDICTS') else [os.environ['FIXTURE_VERDICT']]
question=json.loads(sys.argv[-1].splitlines()[-1])
answer={'verdict':seq[min(len(open(calls).read()), len(seq)) - 1],'revision':question['revision'],
        'acceptance':[{'id':i['id'],'status':'PASS','evidence':'product inspected; fixture QA passed'} for i in question['criteria']]}
print(json.dumps({'type':'text','sessionID':'ses_review_'+os.environ['CADRE_BUDGET_ATTEMPT'],'part':{'type':'text','text':json.dumps(answer)}}))
''')
        fake.chmod(0o700)
        with patch.dict(os.environ, {'CADRE_ENGINE_ROOT':str(root), 'CADRE_REAL_OPENCODE':str(fake), 'FIXTURE_VERDICT':verdict,
                                    'OPENCODE_DB':str(self.home/'absent.db'), **(env or {})}):
            return execute_run(self.store, self.rid)

    def test_real_dispatcher_and_coordinator_reject_duzelt(self) -> None:
        # G-C16: DUZELT triggers the correction round; the fixture worker does not change the product -> the B193 lock, no acceptance
        self.assertEqual(self.dispatch_fixture('DUZELT'), 1)
        record = self.store.read(self.rid)
        self.assertEqual(record['state'], 'failed')
        self.assertEqual([t['imza'][:6] for t in record.get('duzeltme') or []], ['hakem:'])
        self.assertEqual(record['final_result']['reason'], 'revision-already-rejected')
        self.assertEqual(record['tasks']['g1']['state'], 'uretti')
        self.assertFalse(approved(record))

    def test_a_revision_rejected_by_the_reviewer_is_not_reviewed_again_on_retry(self) -> None:
        """B193 (23.09 R1-medium): a retry after DUZELT skipped the lanes and sent the same product back to the judge;
        if the budget changed and another judge was chosen, a rejected product could get accepted. An unchanged product is not reviewed again."""
        # with G-C16 the correction round is also tried in the same run; since the product does not change the first run already becomes a permanent FAIL
        self.assertEqual(self.dispatch_fixture('DUZELT'), 1)
        first = self.store.read(self.rid)
        self.assertEqual(first['state'], 'failed')
        self.assertEqual(first.get('rejected_revisions'), [revision(self.wt)['digest']])
        calls = self.home / 'reviewer-calls'
        before = len(calls.read_text())
        with patch.dict(os.environ, {'CADRE_ENGINE_ROOT': str(self.home/'dispatcher'), 'CADRE_REAL_OPENCODE': str(self.home/'fake-reviewer'),
                                     'FIXTURE_VERDICT': 'GECER', 'OPENCODE_DB': str(self.home/'absent.db')}):
            execute_run(self.store, self.rid)   # a manual retry does not produce acceptance with a lax judge either
        record = self.store.read(self.rid)
        self.assertEqual(record['state'], 'failed', 'ayni urunle yeniden deneme anlamsiz: kalici')
        self.assertEqual(record['final_result']['reason'], 'revision-already-rejected')
        self.assertEqual(len(calls.read_text()), before, 'gevsek hakem bile cagrilmamali')
        self.assertFalse(approved(record))

    def test_real_dispatcher_and_coordinator_accept_matching_qa_and_review(self) -> None:
        self.assertEqual(self.dispatch_fixture('GECER'), 0)
        self.assertEqual(self.store.read(self.rid)['state'], 'completed')
        self.assertTrue(approved(self.store.read(self.rid)))


class DarKapsamKabulTests(unittest.TestCase):
    """CR-35 (user decision 20.10): a final acceptance with targeted QA passes only with a narrow scope BOUND to the request/approval.
    Just because the approved QA command names an explicit test path, the general allow_targeted is not opened; an out-of-scope change,
    a required test that did not run, test maintenance and a non-independent review are still rejected. Whole-product acceptance is separate."""

    def kimlik(self):
        return {'head': 'h1', 'digest': 'd1'}

    def qa(self, **ek):
        discovery = {'status': 'PASS', 'reason': 'qa-discovery-verified', 'scope': 'targeted',
                     'expected_files': ['tests/unit/test_a.py'], 'observed_files': ['tests/unit/test_a.py'], **ek.pop('discovery', {})}
        return {'status': 'PASS', 'revision': self.kimlik(), 'kind': 'project', 'discovery': discovery, **ek}

    def review(self, status='PASS', independent=True, **ek):
        return {'status': status, 'revision': self.kimlik(), 'independent': independent,
                'verdict': 'GECER' if status == 'PASS' else 'DUZELT',
                'acceptance': [{'id': 'main', 'status': 'PASS' if status == 'PASS' else 'FAIL', 'evidence': 'tests/unit/test_a.py inspected'}], **ek}

    def kapsam(self, **ek):
        return {'files': ['tests/unit/test_a.py'], 'tests': ['tests/unit/test_a.py'], 'changed': ['tests/unit/test_a.py'], **ek}

    def test_targeted_qa_without_a_bound_scope_stays_full_qa_required(self):
        from sonuc import decision
        result = decision(self.qa(), self.review(), self.kimlik())
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'full-qa-required'))
        self.assertEqual(decision(self.qa(), self.review(), self.kimlik(), scope=None)['reason'], 'full-qa-required')

    def test_bound_scope_accepts_targeted_qa_when_files_tests_and_independent_review_hold(self):
        from sonuc import decision
        result = decision(self.qa(), self.review(), self.kimlik(), scope=self.kapsam())
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['scope']['files'], ['tests/unit/test_a.py'])
        self.assertEqual(result['scope']['tests'], ['tests/unit/test_a.py'])

    def test_bound_scope_rejects_a_change_outside_the_permitted_files(self):
        from sonuc import decision
        result = decision(self.qa(), self.review(), self.kimlik(), scope=self.kapsam(changed=['src/adapter.py', 'tests/unit/test_a.py']))
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'scope-files-exceeded'))
        self.assertEqual(result['out_of_scope'], ['src/adapter.py'])

    def test_bound_scope_rejects_when_a_required_test_file_did_not_run(self):
        from sonuc import decision
        result = decision(self.qa(), self.review(), self.kimlik(), scope=self.kapsam(tests=['tests/unit/test_a.py', 'tests/unit/test_b.py']))
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'scope-tests-not-run'))
        self.assertEqual(result['missing_tests'], ['tests/unit/test_b.py'])
        unverified = self.qa(discovery={'status': 'UNKNOWN', 'reason': 'qa-discovery-unverified', 'observed_files': []})
        self.assertEqual(decision(unverified, self.review(), self.kimlik(), scope=self.kapsam())['reason'], 'scope-tests-not-run')

    def test_bound_scope_keeps_every_other_rejection(self):
        from sonuc import decision
        cases = [(self.qa(), self.review('FAIL'), 'review-not-passed'),
                 (self.qa(), self.review(independent=False), 'reviewer-not-independent'),
                 (self.qa(status='FAIL'), self.review(), 'qa-not-passed'),
                 (self.qa(test_maintenance={'status': 'PASS', 'paths': ['tests/unit/test_a.py']}), self.review(), 'test-maintenance-review-required'),
                 (self.qa(revision={'head': 'h0', 'digest': 'd0'}), self.review(), 'revision-mismatch')]
        for qa, review, reason in cases:
            with self.subTest(reason=reason):
                result = decision(qa, review, self.kimlik(), scope=self.kapsam())
                self.assertEqual((result['status'], result['reason']), ('FAIL', reason))

    def test_bound_scope_limits_full_qa_runs_too(self):
        from sonuc import decision
        tam = self.qa(discovery={'scope': 'full', 'observed_files': ['tests/unit/test_a.py', 'tests/unit/test_b.py']})
        self.assertEqual(decision(tam, self.review(), self.kimlik(), scope=self.kapsam(changed=['x.py']))['reason'], 'scope-files-exceeded')
        self.assertEqual(decision(tam, self.review(), self.kimlik(), scope=self.kapsam())['status'], 'PASS')

    def test_the_old_pilot_without_a_bound_scope_remains_rejected(self):
        """request-4cc629 (20.10 night): the dispatcher ran `make test` outside the sandbox and installed `.venv` in the worktree, the judge gave DUZELT,
        the request had no bound scope. With the record's counters/permissions unchanged it stays FAIL; the new rule does not produce acceptance retroactively."""
        from sonuc import decision
        kimlik = {'head': 'dde96b28a8b4b4b3aaf6ace622526d30386aada7',
                  'digest': 'ad81f7d6860298c7e48b999e049b6a7bd85ef2f2700e549d4cc464b79fa0621e'}
        qa = {'status': 'PASS', 'reason': 'command-result', 'kind': 'project', 'revision': kimlik,
              'discovery': {'status': 'PASS', 'reason': 'qa-discovery-verified', 'scope': 'targeted',
                            'expected_files': ['tests/unit/test_open_api_adapters.py'], 'observed_files': ['tests/unit/test_open_api_adapters.py']}}
        review = {'status': 'FAIL', 'verdict': 'DUZELT', 'revision': kimlik, 'model': 'openrouter/openai/gpt-6.1-sol', 'independent': True,
                  'acceptance': [{'id': 'request_output', 'status': 'PASS', 'evidence': '1288 parse_page gercek adapter'},
                                 {'id': 'request_acceptance', 'status': 'FAIL', 'evidence': 'tam diff/untracked ve yasak eylem kaniti saglanmadi'}]}
        self.assertEqual(decision(qa, review, kimlik)['reason'], 'full-qa-required')
        gecer = {**review, 'status': 'PASS', 'verdict': 'GECER'}
        self.assertEqual(decision(qa, gecer, kimlik)['reason'], 'full-qa-required',
                         'bagli kapsam yokken hakem GECER olsa da hedefli QA son kabul degildir')


SANDBOX_PROFIL = ('(version 1)\n(allow default)\n(deny network*)\n'
                  '(deny process-exec (regex "/([pP][iI][pP][0-9.]*|[pP][iI][pP][xX]|[uU][vV]|[bB][rR][eE][wW])$"))\n')


def tam_surec_kayitlari(store, rid, wt, base, *, task='impl', attempt='fixture', carried=True):
    """User decision 21.10: process acceptance rests on defined engine checks. The required records the real dispatcher leaves:
    operator policy; per attempt the sandbox profile + load receipt + process close + action record; worktree-ready;
    a clean dispatch.log; if requested, the carried source snapshot (same content)."""
    import hashlib
    import izin
    from kosu import atomic_json
    izin.policy(store, rid)
    folder = store.task_dir(rid, task) / 'attempts' / attempt
    folder.mkdir(parents=True, exist_ok=True)
    if not (folder / 'attempt.json').exists():
        (folder / 'attempt.json').write_text(json.dumps({'role': 'impl', 'model': 'fixture/writer', 'pid': 123}))
    (folder / 'tool-policy.json').write_text(json.dumps({'version': 1, 'writable': True, 'profile': SANDBOX_PROFIL, 'write_paths': [str(Path(wt) / 'product.txt')]}))
    (folder / 'tools-armed.json').write_text(json.dumps({'pid': 123, 'profile_sha256': hashlib.sha256(SANDBOX_PROFIL.encode()).hexdigest()}))
    (folder / 'process.json').write_text(json.dumps({'process_identity': {'pid': 123}, 'stop_observed': {'pid': 123}}))
    (folder / 'stdout.jsonl').write_text(json.dumps({'type': 'tool_use', 'part': {'tool': 'bash', 'state': {'input': {'command': f"exec '{folder}/arac' 'ls tests'"}}}}) + '\n')
    run_dir = store.path(rid)
    (run_dir / 'worktree-ready').write_text(str(wt))
    (run_dir / 'dispatch.log').write_text('2026-10-21 14:00:00 HAFIF BIRLESTIRME: 1 gorev\n2026-10-21 14:00:05 QA KAPISI: PASS rc=0\n')
    if carried:
        kayit = store.read(rid)
        kayit['source'] = {'sha': base, 'branch': 'cr35', 'carried': True, 'dirty_files': 0}
        atomic_json(run_dir / 'run.json', kayit)


class MotorKanitiTests(unittest.TestCase):
    """The engine evidence given to the judge is derived from real permission/process/ledger records; there is no fixed "no forbidden action was taken / network closed"
    statement, anything without a record is written as unknown; receipted / pending (estimated) / trusted open upper bound are separated.
    21.10 live request-9c7a9d76: the install ban was counted 0/3 in a case-folded regex; the "product same -> no writes" inference was removed."""

    PROFIL = SANDBOX_PROFIL

    def setUp(self) -> None:
        self.f = CompletionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.store, self.rid, self.wt = self.f.store, self.f.rid, self.f.wt
        shutil.rmtree(self.store.task_dir(self.rid, 'impl'))   # the writer fixture of CompletionTests: these tests set up their own records

    def kayit(self, **ek):
        record = self.store.read(self.rid)
        return {**record, 'worktree': str(self.wt), 'request': {'duration_minutes': 40, 'budget_usd': '4.5'},
                'request_clock': {'aktif_sn': 1852.4}, 'budget_spent_usd': 0.2, 'budget_held_usd': 0.01, 'budget_cap_usd': 4.5,
                'budget_unsettled': [{'provider': 'openrouter', 'role': 'build', 'model': 'm', 'reserved_usd': 0.01, 'created': 1.0}], **ek}

    def deneme(self, task, attempt, *, policy=True, stop=True, role='impl', armed=True, komutlar=(), araclar=()):
        import hashlib
        folder = self.store.task_dir(self.rid, task) / 'attempts' / attempt
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'attempt.json').write_text(json.dumps({'role': role, 'model': 'fixture/m', 'exit_code': 0, 'outcome': 'finished', 'pid': 1}))
        if policy:
            (folder / 'arac').write_text("#!/bin/sh\nexec /usr/bin/sandbox-exec -p '...' \"$@\"\n")
            (folder / 'tool-policy.json').write_text(json.dumps({'version': 1, 'writable': role != 'kalite', 'profile': self.PROFIL,
                                                                 'arac_sha256': hashlib.sha256((folder / 'arac').read_bytes()).hexdigest(),
                                                                 'write_paths': [str(self.wt / 'product.txt')]}))
            if armed:
                (folder / 'tools-armed.json').write_text(json.dumps({'pid': 1, 'profile_sha256': hashlib.sha256(self.PROFIL.encode()).hexdigest()}))
        if stop:
            (folder / 'process.json').write_text(json.dumps({'process_identity': {'pid': 1}, 'stop_observed': {'pid': 1}, 'scope_stopped': 'x'}))
        satirlar = [json.dumps({'type': 'tool_use', 'part': {'tool': 'bash', 'state': {'input': {'command': f"exec '{folder}/arac' {json.dumps(c)}"}}}}) for c in komutlar]
        satirlar += [json.dumps({'type': 'tool_use', 'part': {'tool': t, 'state': {'input': {'filePath': str(self.wt / 'product.txt')}}}}) for t in araclar]
        if komutlar or araclar:
            (folder / 'stdout.jsonl').write_text('\n'.join(satirlar) + '\n')
        return folder

    def kanit(self, kayit=None, changed=('product.txt',)):
        from kabul import motor_kaniti
        return motor_kaniti(kayit or self.kayit(), list(changed), {'head': 'h1', 'digest': 'd1'}, self.f.base, [], wt=str(self.wt))

    def test_claims_come_from_permission_and_process_records_not_fixed_text(self):
        bos = self.kanit()
        for sabit in ('izin kaydinda commit/push/merge/kurulum/yayin yasak', "sandbox'i agsiz", 'yasak eylem yapilmadi', 'yeniden uretim/yazma yok'):
            self.assertNotIn(sabit, bos)
        self.assertIn('surec kaydi yok', bos)
        self.assertIn('bilinmiyor', bos)
        import izin
        izin.policy(self.store, self.rid)
        self.deneme('g1', 'a1')
        self.deneme('qa', 'c1', role='qa')
        metin = self.kanit()
        self.assertIn('izin kaydi: operator-policy.json', metin)
        self.assertIn('kayitli operator eylemi: yok', metin)
        self.assertIn('surec kayitlari: 2 deneme', metin)
        self.assertIn('sandbox profili kayitli 2/2 (ag reddi 2/2, kurulum araci exec reddi 2/2)', metin)
        self.assertIn('profil yukleme makbuzu (sha eslesen) 2/2', metin)
        self.assertIn('surec kapanisi gozlendi 2/2', metin)
        self.assertIn('sandbox kaydi olmayan deneme: 0', metin)
        self.assertIn('her yasagin her cagrida uygulandiginin kaniti degildir', metin)
        self.assertIn('kurulum dizini izi: yok', metin)
        self.assertIn('motorun dogrulamadigi', metin)
        self.assertIn('makbuzlu (kesinlesmis) 0.2 USD', metin)
        self.assertIn('askida rezerv (tahmini) 0.01 USD', metin)
        self.assertIn('guvenilir acik ust', metin)
        self.assertIn('tavan 4.5 USD', metin)
        self.assertIn('1852 sn / 2400 sn', metin)

    def test_missing_sandbox_record_unarmed_profile_and_install_trace_are_reported(self):
        self.deneme('g1', 'a1')
        self.deneme('g1', 'a2', policy=False, stop=False)
        self.deneme('g1', 'a3', armed=False)
        (self.wt / '.venv').mkdir()
        (self.wt / '.venv/pyvenv.cfg').write_text('home = /usr/bin\n')
        (self.wt / '.gitignore').write_text('.venv/\n')
        metin = self.kanit()
        self.assertIn('sandbox profili kayitli 2/3', metin)
        self.assertIn('profil yukleme makbuzu (sha eslesen) 1/3', metin)
        self.assertIn('sandbox kaydi olmayan deneme: 1', metin)
        self.assertIn('surec kapanisi gozlendi 2/3', metin)
        self.assertIn('bilinmiyor', metin)
        self.assertIn('kurulum dizini izi: .venv', metin)
        self.assertIn('kayitli operator eylemi: yok', metin)

    def test_bound_request_scope_and_engine_check_are_shown_to_the_reviewer(self):
        kayit = self.kayit(request={'duration_minutes': 40, 'budget_usd': '4.5', 'scope_files': ['product.txt'], 'scope_tests': ['test_x.py']},
                           request_input_hash='r1',
                           request_permission={'status': 'approved', 'action': {'request_hash': 'r1', 'acceptance_scope': {'files': ['product.txt'], 'tests': ['test_x.py']}}})
        metin = self.kanit(kayit, ['product.txt', 'extra.py'])
        self.assertIn('istek kapsami (onayli): izinli dosyalar product.txt; gerekli testler test_x.py', metin)
        self.assertIn('kapsam disi degisen: extra.py', metin)
        self.assertIn('bagli kapsam: yok', self.kanit())

    def tasinan_goruntu(self):
        run_dir = self.store.path(self.rid)
        kaynak = run_dir / 'source'
        kaynak.mkdir(exist_ok=True)
        (self.wt / 'product.txt').write_text('tasinan\n')
        patch_bytes = subprocess.run(['git', '-C', str(self.wt), 'diff', '--binary', '--no-ext-diff', 'HEAD', '--'], check=True, capture_output=True).stdout
        (kaynak / 'degisiklik.patch').write_bytes(patch_bytes)
        (run_dir / 'worktree-ready').write_text(str(self.wt))
        eski = time.time() - 3600
        os.utime(self.wt / 'product.txt', (eski, eski))
        os.utime(run_dir / 'worktree-ready', (eski + 1, eski + 1))
        return self.kayit(source={'sha': self.f.git('rev-parse', 'HEAD').strip(), 'branch': 'cr35', 'carried': True, 'dirty_files': 1,
                                  'patch': str(kaynak / 'degisiklik.patch')})

    def test_extending_the_carried_file_keeps_the_patch_lines_and_is_reported_as_preserved(self):
        """New code work rightly extends a carried (accepted) file: the content deviates from the snapshot but the patch's lines
        stay in the product. Preservation is measured at line level; timestamps/write tools are product work, not a deviation."""
        from kabul import kanit_paketi
        kayit = self.tasinan_goruntu()
        self.deneme('g1', 'a1', komutlar=('ls tests',), araclar=('edit',))
        (self.wt / 'product.txt').write_text('tasinan\nyeni ozellik satiri\n')
        paket = kanit_paketi(kayit, ['product.txt'], {'head': 'h1', 'digest': 'd1'}, self.f.base, [], wt=str(self.wt))
        metin = paket['metin']
        self.assertIn('son icerik tasinan goruntuden sapiyor', metin)
        self.assertIn('tasinan yama satirlari: korundu — product.txt eklenen 1/1 urunde, silinen 1/1 urunde yok', metin)
        self.assertIn('yama korunumu: KORUNDU', metin)
        self.assertIn('dosya ayrica degisti', metin)
        self.assertEqual(paket['kontrol']['yama'], 'KORUNDU')
        self.assertEqual(paket['cekirdek']['surum'], 3)
        self.assertEqual(paket['cekirdek']['yazma']['yama'],
                         {'korundu': True, 'bozuk': False, 'bilinmiyor': False,
                          'dosyalar': {'product.txt': {'eklenen': 1, 'kayip': 0, 'silinen': 1, 'geri': 0, 'onemsiz': 0}}})

    def test_removing_or_reverting_a_patch_line_in_the_carried_file_is_a_deviation(self):
        """If a line the patch added is deleted or a line it removed comes back, SAPTI; even if the file is reverted to HEAD and drops out of
        `changed`, the breakage is visible and the evidence core differs from the preserved state."""
        from kabul import kanit_paketi
        kayit = self.tasinan_goruntu()
        self.deneme('g1', 'a1', araclar=('edit',))
        (self.wt / 'product.txt').write_text('tasinan\nyeni\n')
        korunan = kanit_paketi(kayit, ['product.txt'], {'head': 'h1', 'digest': 'd1'}, self.f.base, [], wt=str(self.wt))
        (self.wt / 'product.txt').write_text('after\nyeni\n')
        paket = kanit_paketi(kayit, ['product.txt'], {'head': 'h1', 'digest': 'd1'}, self.f.base, [], wt=str(self.wt))
        self.assertEqual(paket['kontrol']['yama'], 'SAPTI')
        self.assertIn('tasinan yama satirlari: BOZULDU — product.txt eklenen satirin 1/1 urunde yok, silinen satirin 1 geri geldi', paket['metin'])
        self.assertIn('yama korunumu: SAPTI', paket['metin'])
        self.assertEqual(paket['cekirdek']['yazma']['yama']['dosyalar']['product.txt'], {'eklenen': 1, 'kayip': 1, 'silinen': 1, 'geri': 1, 'onemsiz': 0})
        self.assertNotEqual(paket['sha256'], korunan['sha256'])
        (self.wt / 'product.txt').write_text('after\n')   # reverted to HEAD: changed is empty
        geri = kanit_paketi(kayit, [], {'head': 'h1', 'digest': 'd1'}, self.f.base, [], wt=str(self.wt))
        self.assertEqual(geri['kontrol']['yama'], 'SAPTI')
        self.assertEqual(geri['cekirdek']['yazma']['yama']['dosyalar']['product.txt']['kayip'], 1)

    def test_binary_or_unreadable_patch_leaves_patch_preservation_unknown(self):
        from kabul import kanit_paketi
        kayit = self.tasinan_goruntu()
        self.deneme('g1', 'a1', araclar=('edit',))
        (self.wt / 'product.txt').write_text('tasinan\nyeni\n')
        pathlib.Path(kayit['source']['patch']).write_bytes(b'diff --git a/product.txt b/product.txt\nindex 1..2 100644\nGIT binary patch\nliteral 0\n')
        paket = kanit_paketi(kayit, ['product.txt'], {'head': 'h1', 'digest': 'd1'}, self.f.base, [], wt=str(self.wt))
        self.assertEqual(paket['kontrol']['yama'], 'BILINMIYOR')
        self.assertIn('tasinan yama satirlari: karsilastirilamadi', paket['metin'])
        pathlib.Path(kayit['source']['patch']).unlink()
        paket = kanit_paketi(kayit, ['product.txt'], {'head': 'h1', 'digest': 'd1'}, self.f.base, [], wt=str(self.wt))
        self.assertEqual(paket['kontrol']['yama'], 'BILINMIYOR')
        self.assertIsNone(paket['cekirdek']['yazma']['yama'])

    def test_write_evidence_comes_from_timestamps_and_recorded_actions_never_from_content_equality(self):
        """Content parity only proves that the final content was preserved; "not written" is said only as far as the records support it."""
        kayit = self.tasinan_goruntu()
        self.deneme('g1', 'a1', komutlar=('ls tests', 'git diff product.txt', 'cat product.txt'))
        (self.store.path(self.rid) / 'dispatch.log').write_text('2026-10-21 00:32:10 HAFIF BIRLESTIRME: 1 gorev\n')
        metin = self.kanit(kayit)
        self.assertIn('son icerik tasinan goruntuyle ayni (icerik korunmus; yazma kaniti degil)', metin)
        self.assertIn('zaman damgasi worktree kurulumundan sonra degismemis (yazma izi yok; damga mutlak garanti degil)', metin)
        self.assertIn('yazma araci (edit/write/patch) cagrisi: 0', metin)
        self.assertIn('kabuk komutu: 3, yazma kalibi: 0', metin)
        self.assertIn('kabuk yazmasi kalip disinda olabilir -> yazilmadigi kesin degil', metin)
        self.assertIn('dispatcher gunlugu: taramada kurulum/paket/test komutu izi yok (1 satir); gunluge yazilmayan komutlar bilinmiyor', metin)
        self.assertNotIn('yeniden uretim/yazma yok', metin)
        (self.wt / 'product.txt').write_text('tasinan\n')   # the same content rewritten
        metin = self.kanit(kayit)
        self.assertIn('son icerik tasinan goruntuyle ayni', metin)
        self.assertIn('zaman damgasi worktree kurulumundan SONRA degisti: product.txt (yeniden yazma izi; icerik ayni olsa da)', metin)
        self.assertNotIn('yazma izi yok', metin)
        (self.wt / 'product.txt').write_text('degisti\n')
        (self.wt / 'product.txt').write_text('tasinan\n')   # change and revert
        metin = self.kanit(kayit)
        self.assertIn('SONRA degisti', metin)
        self.deneme('duzeltme-1', 'b1', komutlar=("sed -i '' 's/a/b/' product.txt", 'cat product.txt'), araclar=('edit',))
        (self.store.path(self.rid) / 'dispatch.log').write_text('2026-10-20 21:05 uv run --locked pytest tests\n')
        metin = self.kanit(kayit)
        self.assertIn('yazma araci (edit/write/patch) cagrisi: 1', metin)
        self.assertIn('kabuk komutu: 5, yazma kalibi: 1 (sed -i', metin)
        self.assertIn('dispatcher gunlugu: taramada kurulum/paket/test komutu izi: uv run', metin)
        (self.wt / 'product.txt').write_text('sapma\n')
        self.assertIn('son icerik tasinan goruntuden sapiyor', self.kanit(kayit))

    def paket(self, kayit=None, changed=('product.txt',)):
        from kabul import kanit_paketi
        return kanit_paketi(kayit or self.kayit(), list(changed), {'head': 'h1', 'digest': 'd1'}, self.f.base, [], wt=str(self.wt))

    def test_evidence_core_ignores_log_volume_cost_and_reviewer_records_but_tracks_real_traces(self):
        """The core is derived from structured evidence facts: log line counts, cost/time and the judge's own session are out;
        a new install/package/test trace, a new write action and a new process record are in."""
        from kabul import kanit_cekirdegi_sha
        self.deneme('g1', 'a1', komutlar=('ls tests', 'cat product.txt'))
        taban = self.paket()
        self.assertEqual(taban['sha256'], kanit_cekirdegi_sha(taban['cekirdek']))
        self.assertIn('MOTOR KANITI', taban['metin'])
        self.assertEqual(taban['metin'], self.kanit())
        gunluk = self.store.path(self.rid) / 'dispatch.log'
        gunluk.write_text('2026-10-21 00:32:10 HAFIF BIRLESTIRME: 1 gorev\n')
        self.assertEqual(self.paket()['sha256'], taban['sha256'], 'siradan gunlugun varligi kanit degil')
        with open(gunluk, 'a') as f:
            f.write(''.join(f'2026-10-21 00:4{i}:00 Bilgi: durum sorgulandi\n' for i in range(5)))
        self.assertEqual(self.paket()['sha256'], taban['sha256'], 'satir sayisi kanit degil')
        self.assertEqual(self.paket(self.kayit(budget_spent_usd=1.5, request_clock={'aktif_sn': 9.0}))['sha256'], taban['sha256'], 'maliyet/sure kanit degil')
        self.deneme('kalite', 'h1', role='kalite', komutlar=('ls', 'cat product.txt', 'git status'), araclar=('read',))
        self.assertEqual(self.paket()['sha256'], taban['sha256'], 'hakemin kendi oturumu kanit degil')
        self.assertIn('kabuk komutu: 2,', self.paket()['metin'])
        with open(gunluk, 'a') as f:
            f.write('2026-10-20 21:05 uv run --locked pytest tests\n')
        izli = self.paket()
        self.assertNotEqual(izli['sha256'], taban['sha256'], 'gercek kurulum/paket/test izi yeni kanittir')
        self.assertEqual(izli['cekirdek']['dispatcher'], ['uv run'])
        gunluk.write_text('2026-10-21 00:32:10 HAFIF BIRLESTIRME: 1 gorev\n')
        self.assertEqual(self.paket()['sha256'], taban['sha256'])
        self.deneme('g1', 'a2', komutlar=('cat product.txt',), araclar=('edit',))
        yazma = self.paket()
        self.assertNotEqual(yazma['sha256'], taban['sha256'], 'yeni yazma eylemi kaydi yeni kanittir')
        self.assertEqual(yazma['cekirdek']['yazma']['eylem']['arac'], 1)
        self.assertEqual(yazma['cekirdek']['surec']['n'], 2)
        self.assertNotIn('kalite', yazma['cekirdek']['surec']['roller'])

    def test_active_time_line_includes_the_open_session_and_stays_out_of_the_core(self):
        """21.10 live request-1e0782bb: the judge saw "0 s / 2700 s excluding review" (an open session's duration had not closed in the record) and
        counted the time criterion as unevidenced. The line also counts the open session; time does not enter the core."""
        simdi = time.time()
        kapali = self.kayit(request_clock={'baslangic': simdi - 1000, 'aktif_sn': 100.0, 'oturum': None, 'oturum_pid': None})
        acik = self.kayit(request_clock={'baslangic': simdi - 1000, 'aktif_sn': 100.0, 'oturum': simdi - 500, 'oturum_pid': 1})
        self.assertIn('sure: kayitli aktif 100 sn / 2400 sn', self.kanit(kapali))
        metin = self.kanit(acik)
        self.assertRegex(metin, r'sure: kayitli aktif (59[5-9]|60[0-5]) sn / 2400 sn \(acik oturum dahil')
        self.assertEqual(self.paket(kapali)['sha256'], self.paket(acik)['sha256'], 'sure cekirdege girmez')

    def test_missing_action_records_and_snapshot_keep_write_evidence_unknown(self):
        self.deneme('g1', 'a1')   # no stdout.jsonl
        metin = self.kanit()
        self.assertIn('uygulama izi: tasinan goruntu yok (kaynak temiz baslangic)', metin)
        self.assertIn('eylem kaydi yok -> yazma durumu bilinmiyor', metin)
        self.assertIn('worktree kurulum kaydi yok -> zaman damgasi karsilastirilamadi', metin)
        self.assertIn('dispatcher gunlugu: yok -> sandbox disi kabuk adimlari bilinmiyor', metin)
        self.assertNotIn('yazma izi yok', metin)


class KanitEksigiTests(unittest.TestCase):
    """21.10 live request-9c7a9d76: the judge found the product right and gave DUZELT only for missing process evidence; the engine sent this to a code
    repair (0.017 USD wasted) and wrote the product digest as rejected. Missing evidence (eksik: kanit) opens no code repair;
    a blind repeat with an unchanged product + unchanged evidence is blocked; really changed evidence may be re-reviewed once in the same run;
    old records/FAILs/costs do not change."""

    def setUp(self) -> None:
        self.f = CompletionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.store, self.rid, self.wt = self.f.store, self.f.rid, self.f.wt
        self.f.successful_qa()
        self.cagri = 0

    def hakem(self, eksik_ids=(), fail_ids=(), verdict='DUZELT'):
        def cevap(real, wt, args, timeout, env=None, role='kalite'):
            istem = args[-1]
            if '{"revision"' not in istem:
                return self.f.review('GECER')
            self.cagri += 1
            veri = json.JSONDecoder().raw_decode(istem[istem.rindex('{"revision"'):])[0]
            items = []
            for c in veri['criteria']:
                item = {'id': c['id'], 'status': 'FAIL' if c['id'] in fail_ids or c['id'] in eksik_ids else 'PASS',
                        'evidence': 'product.txt inspected; surec kaniti ' + ('eksik' if c['id'] in eksik_ids else 'tam')}
                if c['id'] in eksik_ids:
                    item['eksik'] = 'kanit'
                items.append(item)
            return review_output(json.dumps({'revision': revision(self.wt)['digest'], 'verdict': verdict, 'acceptance': items}))
        return cevap

    def finalize(self, cevap):
        with patch.object(core, 'capture_opencode', side_effect=cevap):
            return core.finalize_run('a', str(self.wt), self.f.log, '/fake/cli', review_model='')

    def test_review_schema_accepts_only_known_gap_values(self):
        kimlik = revision(self.wt)
        ok = parse_review(json.dumps({'revision': kimlik['digest'], 'verdict': 'DUZELT',
                                      'acceptance': [{'id': 'main', 'status': 'FAIL', 'evidence': 'surec kaniti eksik', 'eksik': 'kanit'}]}), kimlik, ['main'])
        self.assertEqual((ok['status'], ok['acceptance'][0]['eksik']), ('FAIL', 'kanit'))
        kotu = parse_review(json.dumps({'revision': kimlik['digest'], 'verdict': 'DUZELT',
                                        'acceptance': [{'id': 'main', 'status': 'FAIL', 'evidence': 'x', 'eksik': 'baska'}]}), kimlik, ['main'])
        self.assertEqual(kotu['status'], 'ERROR')

    def test_evidence_gap_does_not_open_code_repair_and_blocks_blind_repeat_until_evidence_changes(self):
        from kabul import duzeltme_karari, hata_imzasi
        result = self.finalize(self.hakem(eksik_ids=('main',)))
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'review-evidence-gap'), result)
        self.assertEqual(self.cagri, 1)
        self.assertIsNone(hata_imzasi(result), 'kanit eksigi duzeltilebilir kod hatasi degildir')
        record = self.store.read(self.rid)
        self.assertEqual(duzeltme_karari(record)[0], 'DUR')
        self.assertFalse(record.get('rejected_revisions'), 'urun reddedilmedi; kanit eksik')
        red = record['rejected_evidence']
        self.assertEqual(red[-1]['digest'], revision(self.wt)['digest'])
        self.assertEqual(red[-1]['kanit'], result['evidence_sha256'])
        self.assertFalse(approved(record))
        onceki = {k: record.get(k) for k in ('budget_spent_usd', 'budget_held_usd', 'request_clock', 'final_result')}
        tekrar = self.finalize(self.hakem(verdict='GECER'))
        self.assertEqual((tekrar['status'], tekrar['reason']), ('FAIL', 'evidence-already-rejected'), tekrar)
        self.assertEqual(self.cagri, 1, 'degismeyen urun + degismeyen kanitla hakem cagrilmaz')
        sonra = self.store.read(self.rid)
        self.assertEqual({k: sonra.get(k) for k in ('budget_spent_usd', 'budget_held_usd', 'request_clock')},
                         {k: onceki[k] for k in ('budget_spent_usd', 'budget_held_usd', 'request_clock')})
        self.assertEqual(sonra['rejected_evidence'], red, 'eski red kaydi silinmez')
        folder = self.store.task_dir(self.rid, 'impl') / 'attempts/fixture'
        (folder / 'tool-policy.json').write_text(json.dumps({'version': 1, 'writable': True, 'profile': '(version 1)\n(deny network*)\n', 'write_paths': []}))
        (folder / 'process.json').write_text(json.dumps({'stop_observed': {'pid': 123}}))
        gecer = self.finalize(self.hakem(verdict='GECER'))
        self.assertEqual(gecer['status'], 'PASS', gecer)
        self.assertEqual(self.cagri, 2, 'gercekten degisen kanit bir kez yeniden incelenir')
        self.assertNotEqual(gecer['evidence_sha256'], red[-1]['kanit'])
        self.assertTrue(approved(self.store.read(self.rid)))

    def gunluk(self, *satirlar, ekle=False):
        yol = self.store.path(self.rid) / 'dispatch.log'
        with open(yol, 'a' if ekle else 'w') as f:
            f.write(''.join(l + '\n' for l in satirlar))

    def oturum_kaydi(self, task, attempt, role, komutlar=(), araclar=()):
        """Records of an executor/judge session: attempt.json, the (non-writable) policy, the close and tool events."""
        folder = self.store.task_dir(self.rid, task) / 'attempts' / attempt
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'attempt.json').write_text(json.dumps({'role': role, 'model': 'fixture/m', 'exit_code': 0, 'outcome': 'finished', 'pid': 7}))
        (folder / 'tool-policy.json').write_text(json.dumps({'version': 1, 'writable': role != 'kalite', 'profile': '(version 1)\n(deny network*)\n', 'write_paths': []}))
        (folder / 'process.json').write_text(json.dumps({'process_identity': {'pid': 7}, 'stop_observed': {'pid': 7}}))
        satirlar = [json.dumps({'type': 'tool_use', 'part': {'tool': 'bash', 'state': {'input': {'command': f"exec '{folder}/arac' {json.dumps(c)}"}}}}) for c in komutlar]
        satirlar += [json.dumps({'type': 'tool_use', 'part': {'tool': t, 'state': {'input': {'filePath': str(self.wt / 'product.txt')}}}}) for t in araclar]
        (folder / 'stdout.jsonl').write_text('\n'.join(satirlar) + '\n')
        return folder

    def test_ordinary_dispatcher_log_lines_and_reviewer_session_records_are_not_new_evidence(self):
        """Codex reproduction: after the first evidence rejection, adding just an info line to dispatch.log opened a second judge
        (the core carried the log line count and the judge's own session records). Ordinary logs, line counts and
        judge records are not meaningful new evidence; since it is missing evidence, not a product rejection, no code repair opens either."""
        from kabul import duzeltme_karari
        self.gunluk('2026-10-21 00:32:10 HAFIF BIRLESTIRME: 1 gorev', '2026-10-21 00:33:15 kosu basliyor (opencode run)')
        result = self.finalize(self.hakem(eksik_ids=('main',)))
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'review-evidence-gap'), result)
        self.assertEqual(self.cagri, 1)
        red = list(self.store.read(self.rid)['rejected_evidence'])
        self.gunluk('2026-10-21 00:40:00 Bilgi: durum sorgulandi', '2026-10-21 00:40:05 KABUL SONUCU: FAIL', ekle=True)
        self.oturum_kaydi('kalite', 'hakem1', 'kalite', komutlar=('ls tests', 'cat product.txt', 'git diff product.txt'), araclar=('read',))
        self.store.update(self.rid, budget_spent_usd=0.9, request_clock={'aktif_sn': 1200.0})
        tekrar = self.finalize(self.hakem(verdict='GECER'))
        self.assertEqual((tekrar['status'], tekrar['reason']), ('FAIL', 'evidence-already-rejected'), tekrar)
        self.assertEqual(self.cagri, 1, 'siradan gunluk satiri + hakem kayitlari + maliyet degisimi hakemi yeniden acmaz')
        self.assertEqual(tekrar['evidence_sha256'], red[-1]['kanit'], 'cekirdek degismedi')
        sonra = self.store.read(self.rid)
        self.assertEqual(sonra['rejected_evidence'], red)
        self.assertFalse(sonra.get('rejected_revisions'), 'dogru urun reddedilmedi')
        self.assertEqual(duzeltme_karari(sonra)[0], 'DUR', 'kanit eksigi kod onarimina gitmez')
        self.assertFalse(approved(sonra))

    def test_new_recorded_action_evidence_reopens_exactly_one_review(self):
        """A really new action record (tool events of a new executor session) changes the core; the same product is reviewed once more."""
        self.gunluk('2026-10-21 00:32:10 HAFIF BIRLESTIRME: 1 gorev')
        result = self.finalize(self.hakem(eksik_ids=('main',)))
        self.assertEqual(result['reason'], 'review-evidence-gap', result)
        red = list(self.store.read(self.rid)['rejected_evidence'])
        self.oturum_kaydi('g1', 'yazar2', 'impl', komutlar=('cat product.txt',), araclar=('edit',))
        gecer = self.finalize(self.hakem(verdict='GECER'))
        self.assertEqual(gecer['status'], 'PASS', gecer)
        self.assertEqual(self.cagri, 2)
        self.assertNotEqual(gecer['evidence_sha256'], red[-1]['kanit'])
        self.assertTrue(approved(self.store.read(self.rid)))

    def test_product_rejection_still_opens_repair_and_legacy_rejections_stay_permanent(self):
        from kabul import hata_imzasi
        result = self.finalize(self.hakem(fail_ids=('main',)))
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'review-not-passed'))
        self.assertEqual(hata_imzasi(result), 'hakem:main')
        record = self.store.read(self.rid)
        self.assertEqual(record['rejected_revisions'], [revision(self.wt)['digest']])
        self.assertFalse(record.get('rejected_evidence'))
        (self.store.task_dir(self.rid, 'impl') / 'attempts/fixture/process.json').write_text(json.dumps({'stop_observed': {'pid': 123}}))
        tekrar = self.finalize(self.hakem(verdict='GECER'))
        self.assertEqual((tekrar['status'], tekrar['reason']), ('FAIL', 'revision-already-rejected'), 'urun reddi kanit degisse de kalici')
        self.assertEqual(self.cagri, 1)


class SurecKontroluTests(unittest.TestCase):
    """User decision 21.10: process acceptance rests on defined and verifiable engine checks; no absolute "it happened
    nowhere" guarantee is sought. In a typed request: enough records -> TAM (acceptable); a missing/broken required record -> EKSIK
    (even if the judge mistakenly says GECER the engine does not accept, no repair); a real violation -> FAIL before the paid judge; a wrong product -> product
    rejection and repair (being TAM does not get a wrong product accepted)."""

    def setUp(self) -> None:
        self.f = CompletionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.store, self.rid, self.wt = self.f.store, self.f.rid, self.f.wt
        self.f.successful_qa()
        self.tipli_istek()
        self.cagri, self.istemler = 0, []

    def tipli_istek(self):
        from kosu import atomic_json
        kayit = self.store.read(self.rid)
        kayit['request'] = {'job_type': 'code', 'duration_minutes': 40, 'budget_usd': '4.5', 'output': 'product.txt after icerir',
                            'acceptance': 'product.txt after icerir; kurulum, dis ag ve asil kaynakta yazma yok', 'input_mode': 'goal',
                            'target_kind': 'existing'}
        kayit['request_input_hash'] = 'r1'
        atomic_json(self.store.path(self.rid) / 'run.json', kayit)

    def tam_kayitlar(self):
        tam_surec_kayitlari(self.store, self.rid, self.wt, self.f.base)

    def hakem(self, eksik=None, verdict='GECER'):
        def cevap(real, wt, args, timeout, env=None, role='kalite'):
            istem = args[-1]
            if '{"revision"' not in istem:
                return self.f.review('GECER')
            self.cagri += 1
            self.istemler.append(istem)
            veri = json.JSONDecoder().raw_decode(istem[istem.rindex('{"revision"'):])[0]
            items = []
            for c in veri['criteria']:
                item = {'id': c['id'], 'status': 'FAIL' if (eksik and c['id'] == 'main') else 'PASS', 'evidence': 'motor satirlari: surec kontrolu, fark, QA kaydi'}
                if eksik and c['id'] == 'main':
                    item['eksik'] = eksik
                items.append(item)
            return review_output(json.dumps({'revision': revision(self.wt)['digest'], 'verdict': verdict, 'acceptance': items}))
        return cevap

    def finalize(self, cevap):
        with patch.object(core, 'capture_opencode', side_effect=cevap):
            return core.finalize_run('a', str(self.wt), self.f.log, '/fake/cli', review_model='')

    def test_complete_records_yield_tam_and_the_reviewer_rule_accepts_process_criteria(self):
        self.tam_kayitlar()
        result = self.finalize(self.hakem())
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(self.cagri, 1)
        istem = self.istemler[-1]
        self.assertIn('surec kontrolu: TAM', istem)
        self.assertIn('sandbox profili 1/1 (ag reddi 1/1, kurulum araci reddi 1/1), yukleme makbuzu 1/1, surec kapanisi 1/1, kurulum izi yok, kapsam disi yok, gunluk izi yok', istem)
        self.assertIn('mutlak garanti degildir', istem)
        self.assertIn('yama korunumu: KORUNDU', istem)
        self.assertIn('SUREC KRITERI KURALI', istem)
        self.assertIn('kesinlik yoklugu tek basina FAIL nedeni degildir', istem)
        self.assertIn("'ihlal gormedim' beyanin kanit degildir", istem)
        self.assertTrue(approved(self.store.read(self.rid)))

    def test_extending_the_carried_file_keeps_tam_and_korundu_when_the_patch_lines_survive(self):
        """The next real code job: the carried (accepted) patch file grows with new code. As long as the patch's lines stay in the product
        the engine writes 'yama korunumu: KORUNDU' and acceptance continues; timestamps/write tools are product work."""
        from kosu import atomic_json
        self.tam_kayitlar()
        head = self.f.git('rev-parse', 'HEAD').strip()
        (self.wt / 'product.txt').write_text('after\ntasinan\n')
        yama = subprocess.run(['git', '-C', str(self.wt), 'diff', '--binary', '--no-ext-diff', 'HEAD', '--'], check=True, capture_output=True).stdout
        kaynak = self.store.path(self.rid) / 'source'
        kaynak.mkdir(exist_ok=True)
        (kaynak / 'degisiklik.patch').write_bytes(yama)
        kayit = self.store.read(self.rid)
        kayit['source'] = {'sha': head, 'branch': 'cr35', 'carried': True, 'dirty_files': 1, 'patch': str(kaynak / 'degisiklik.patch')}
        atomic_json(self.store.path(self.rid) / 'run.json', kayit)
        (self.wt / 'product.txt').write_text('after\ntasinan\nyeni ozellik\n')
        result = self.finalize(self.hakem())
        self.assertEqual(result['status'], 'PASS', result)
        istem = self.istemler[-1]
        self.assertIn('surec kontrolu: TAM', istem)
        self.assertIn('yama korunumu: KORUNDU', istem)
        self.assertIn('tasinan yama satirlari: korundu — product.txt eklenen 1/1 urunde', istem)
        self.assertIn('dosya ayrica degisti', istem)
        self.assertTrue(approved(self.store.read(self.rid)))

    def test_missing_mandatory_record_blocks_acceptance_even_when_the_reviewer_says_gecer(self):
        """Codex 21.10: with a required record missing, a judge saying GECER produced PASS/approved. On EKSIK the engine does not accept,
        opens no code repair and does not call a second paid judge with the same product+evidence."""
        from kabul import duzeltme_karari, hata_imzasi
        result = self.finalize(self.hakem())
        self.assertEqual(result['status'], 'FAIL', result)
        self.assertTrue(result['reason'].startswith('surec-kontrolu-eksik'), result['reason'])
        self.assertIn('sandbox kaydi olmayan deneme: 1', result['reason'])
        self.assertEqual(self.cagri, 1)
        self.assertIn('surec kontrolu: EKSIK', self.istemler[-1])
        self.assertIsNone(hata_imzasi(result), 'eksik kayit kod onarimiyla kapanmaz')
        record = self.store.read(self.rid)
        self.assertFalse(approved(record))
        self.assertFalse(record.get('rejected_revisions'), 'urun reddedilmedi')
        self.assertEqual(record['rejected_evidence'][-1]['digest'], revision(self.wt)['digest'])
        self.assertEqual(duzeltme_karari(record)[0], 'DUR')
        tekrar = self.finalize(self.hakem())
        self.assertEqual((tekrar['status'], tekrar['reason']), ('FAIL', 'evidence-already-rejected'), tekrar)
        self.assertEqual(self.cagri, 1, 'degismeyen kayitla hakem yeniden cagrilmaz')

    def test_missing_mandatory_record_with_evidence_gap_verdict_ends_without_repair(self):
        from kabul import duzeltme_karari, hata_imzasi
        result = self.finalize(self.hakem(eksik='kanit', verdict='DUZELT'))
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'review-evidence-gap'), result)
        self.assertIn('surec kontrolu: EKSIK', self.istemler[-1])
        self.assertIsNone(hata_imzasi(result))
        record = self.store.read(self.rid)
        self.assertFalse(record.get('rejected_revisions'))
        self.assertEqual(duzeltme_karari(record)[0], 'DUR')
        self.assertFalse(approved(record))

    def test_detected_violation_fails_before_the_paid_reviewer(self):
        from kabul import hata_imzasi
        self.tam_kayitlar()
        with open(self.store.path(self.rid) / 'dispatch.log', 'a') as f:
            f.write('2026-10-21 14:00:09 uv run --locked pytest tests\n')
        result = self.finalize(self.hakem())
        self.assertEqual(result['status'], 'FAIL', result)
        self.assertTrue(result['reason'].startswith('surec-ihlali'), result['reason'])
        self.assertIn('uv run', result['reason'])
        self.assertEqual(self.cagri, 0, 'gercek ihlalde ucretli hakem cagrilmaz')
        self.assertIsNone(hata_imzasi(result), 'surec ihlali kod onarimiyla kapanmaz')
        self.assertFalse(approved(self.store.read(self.rid)))

    def test_wrong_product_is_rejected_as_product_and_opens_repair_even_when_process_is_tam(self):
        from kabul import hata_imzasi
        self.tam_kayitlar()
        result = self.finalize(self.hakem(eksik='urun', verdict='DUZELT'))
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'review-not-passed'), result)
        self.assertEqual(hata_imzasi(result), 'hakem:main')
        record = self.store.read(self.rid)
        self.assertEqual(record['rejected_revisions'], [revision(self.wt)['digest']])
        self.assertFalse(record.get('rejected_evidence'))
        self.assertIn('surec kontrolu: TAM', self.istemler[-1], 'surec tam olsa da urun karari bagimsizdir')
        self.assertFalse(approved(record))


class HakemFarkiVeKurulumIziTests(unittest.TestCase):
    """The judge gets the real source->product diff (uncommitted + untracked) within a safe scope; a file list/digest is not diff evidence.
    An install directory outside the sandbox in the worktree (the dispatcher's old `make test` path) closes acceptance."""

    def setUp(self) -> None:
        self.f = CompletionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.store, self.rid, self.wt = self.f.store, self.f.rid, self.f.wt

    def finalize(self, verdict='GECER'):
        prompts = []
        hakem = self.f.review_for_prompt(verdict)

        def capture(real, wt, args, timeout, env=None, role='kalite'):
            prompts.append(args[-1])
            return hakem(real, wt, args, timeout, env, role)

        with patch.object(core, 'capture_opencode', side_effect=capture):
            result = core.finalize_run('a', str(self.wt), self.f.log, '/fake/cli', review_model='')
        return result, [p for p in prompts if 'MOTOR KANITI' in p]

    def test_final_reviewer_receives_the_real_source_to_product_diff_including_untracked_files(self):
        self.f.successful_qa()
        (self.wt / 'yeni.txt').write_text('taze satir\n')
        result, istemler = self.finalize()
        self.assertEqual(result['status'], 'PASS', result)
        istem = istemler[-1]
        self.assertIn('KAYNAK->URUN FARKI', istem)
        self.assertIn('-before', istem)
        self.assertIn('+after', istem)
        self.assertIn('+++ b/yeni.txt', istem)
        self.assertIn('+taze satir', istem)
        self.assertIn('fark TAM', istem)
        self.assertIn('dosya listesi veya digest tek basina fark kaniti degildir', istem.lower())
        fark = sorted(self.store.task_dir(self.rid, 'kalite').glob('fark-*.diff'))
        self.assertTrue(fark, 'fark dosyasi kalite kaydina yazilmali')
        self.assertIn('+taze satir', fark[-1].read_text())
        self.assertEqual(result['review_diff']['files'], 2)
        self.assertTrue(result['review_diff']['complete'])

    def test_oversized_diff_is_truncated_visibly_and_marked_partial(self):
        self.f.successful_qa()
        (self.wt / 'buyuk.txt').write_text(''.join(f'satir {i} ' + 'x' * 70 + '\n' for i in range(3000)))
        result, istemler = self.finalize()
        istem = istemler[-1]
        self.assertIn('fark KISMI', istem)
        self.assertIn('KIRPILDI', istem)
        self.assertLess(len(istem), 200_000)
        self.assertFalse(result['review_diff']['complete'])
        self.assertIn('buyuk.txt', result['review_diff']['truncated'])

    def test_diff_helper_skips_secret_named_files_and_binaries(self):
        from kabul import urun_farki
        (self.wt / 'credentials.txt').write_text('TOKEN=gizli-deger\n')
        (self.wt / 'blob.bin').write_bytes(b'\x00\x01\x02binary')
        fark = urun_farki(str(self.wt), self.f.base, ['blob.bin', 'credentials.txt', 'product.txt'])
        self.assertNotIn('gizli-deger', fark['text'])
        self.assertIn('credentials.txt', fark['skipped'])
        self.assertIn('+after', fark['text'])
        self.assertNotIn('\x00', fark['text'])

    def test_install_directory_created_in_the_run_worktree_fails_acceptance_before_the_paid_reviewer(self):
        self.f.successful_qa()
        (self.wt / '.venv').mkdir()
        (self.wt / '.venv/pyvenv.cfg').write_text('home = /usr/bin\n')
        (self.wt / '.gitignore').write_text('.venv/\n')
        result, istemler = self.finalize()
        self.assertEqual(result['status'], 'FAIL')
        self.assertTrue(result['reason'].startswith('kurulum-izi'), result)
        self.assertIn('.venv', result['reason'])
        self.assertEqual(istemler, [], 'kurulum izi varken ucretli hakem cagrilmaz')
        self.assertFalse(approved(self.store.read(self.rid)))
        from kabul import kurulum_izleri
        self.assertEqual(kurulum_izleri(str(self.wt)), ['.venv'])
        (self.wt / '.venv/pyvenv.cfg').unlink()
        (self.wt / '.venv').rmdir()
        self.assertEqual(kurulum_izleri(str(self.wt)), [])


if __name__ == '__main__':
    unittest.main()
