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
os.environ.setdefault('CADRE_WAIT_THRESHOLD_SEC', '0')   # B268: the dispatcher fixture must not wait for midnight on the real clock
import qe_core as core
from qa_fixture import command as qa_command, review_output
from run_store import RunStore, execute_run
from outcome import approved, parse_review, revision


def setUpModule():
    environment = patch.dict(os.environ, {'CADRE_CHAIN_QUALITY': 'opencode-go/qwen3.8-max'})
    environment.start()
    unittest.addModuleCleanup(environment.stop)

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
        (self.project/'qa-command').write_text('exit 1')
        self.run = self.store.accept('a', 'deliver product', source_sha=self.base)
        self.rid = self.run['run_id']
        self.store.update(self.rid, worktree=str(self.wt))
        writer = self.store.task_dir(self.rid, 'impl')/'attempts/fixture/attempt.json'
        writer.parent.mkdir(parents=True)
        writer.write_text(json.dumps({'role': 'impl', 'model': 'fixture/writer', 'pid': 123}))
        self.enter(patch.dict(os.environ, {**self.store.environment(self.rid), 'CADRE_TASK_ID':'', 'CADRE_OWNER_FDS':''}))
        self.enter(patch.object(core, 'QE', self.home))
        self.enter(patch.object(core, 'worktree_base_sha', return_value=self.base))
        self.log = self.home/'runner.log'
        self.enter(patch.object(core, 'run_opencode', return_value=Mock(poll=lambda:0, wait=lambda **kwargs:0, returncode=0)))
        self.enter(patch.object(core, 'budget_json', return_value=None))   # B37: the judge chain reads the budget; keep the test hermetic
        # P3 (03.10): no local; the repair/correction starts in the cloud — these tests measure the flow, not the model selection
        self.enter(patch.object(core, 'cloud_candidate', return_value='opencode-go/test'))

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

    def product_worktree(self):
        """qe_commit_wip commits only under CADRE_HOME/worktrees/<project>/... and on a cadre/* branch."""
        wt = self.home/'worktrees/a/run1'
        wt.mkdir(parents=True)
        def git(*args):
            return subprocess.run(['git', '-C', str(wt), '-c', 'user.name=fixture',
                                   '-c', 'user.email=fixture@local', *args],
                                  check=True, capture_output=True, text=True, timeout=10).stdout
        git('init', '-q', '-b', 'cadre/job-1')
        (wt/'product.py').write_text('print(1)\n')
        git('add', '.')
        git('commit', '-qm', 'product')
        return wt, git

    def test_reviewer_repair_is_committed_so_the_delivered_branch_is_the_reviewed_product(self):
        """B110-3: the judge works with 'do not commit', while the final commit is made BEFORE finalize_run. If the fix
        is not committed, `cadre push` delivers the previous state, NOT the reviewed product."""
        wt, git = self.product_worktree()
        (wt/'product.py').write_text('print(2)\n')                    # the judge's small fix
        (wt/'.qa-outcome.txt').write_text('PASS\n')                   # a harness leftover: not product
        id = revision(wt)
        self.run = self.store.accept('a', 'deliver reviewed product', source_sha=git('rev-parse', 'HEAD').strip())
        self.rid = self.run['run_id']
        self.store.update(self.rid, worktree=str(wt))
        Path(self.run['constraints']['qa-command']).write_text(qa_command('python3 product.py | grep -qx 2'))
        with patch.dict(os.environ, self.store.environment(self.rid)):
            qa = core.qa_gate('a', str(wt), self.log, '/forbidden/provider', model='claude/fixture')
        self.assertEqual(qa['status'], 'PASS')
        import acceptance
        with patch.dict(os.environ, self.store.environment(self.rid)):
            raw = self.review(identity=id)
            proof = acceptance.review_binding(self.store, self.rid, getattr(raw, 'attempt'), set(), acceptance.review_context(self.store.read(self.rid)))
        self.assertIsNotNone(proof)
        outcome = {'status': 'PASS', 'revision': id,
                 'qa': qa, 'review': {'status': 'PASS', 'revision': id, 'independent': True, 'session_proof': proof}}
        import permission
        with patch.dict(os.environ, self.store.environment(self.rid)):
            pending = core.delivery_commit(wt, 'a', self.log, outcome)
        self.assertEqual(pending['delivery']['git_permission'], 'required')
        self.store.update(self.rid, state='completed', final_result=pending)
        token = pending['delivery']['action_token']
        with patch.dict(os.environ, {key: value for key, value in os.environ.items() if key != 'CADRE_RUN_ID'}, clear=True):
            first = permission.approve(self.store, self.rid, token)
            permission.approve(self.store, self.rid, token, first['confirmation'])
        permission.commit_execute(self.store, self.rid, token)
        permission.adopt_commit(self.store, self.rid, permission.read_action(self.store, self.rid, token))
        fresh = self.store.read(self.rid)['final_result']
        self.assertEqual(fresh['status'], 'PASS')
        self.assertEqual(git('status', '--porcelain', '--', '.', *core.PRODUCT_EXCLUDED).strip(), '')
        self.assertIn('print(2)', git('show', 'HEAD:product.py'))
        self.store.update(self.rid, worktree=str(wt), final_result=fresh)
        self.assertTrue(approved(self.store.read(self.rid)), 'Acceptance evidence must be current after the delivery commit')

    def test_delivery_commit_keeps_harness_leftovers_out_and_does_not_fail_acceptance(self):
        """QA output/the plan ledger are not committed; so acceptance must not fall to FAIL (a false positive)."""
        wt, git = self.product_worktree()
        (wt/'.qa-outcome.txt').write_text('PASS\n')
        id = revision(wt)
        outcome = {'status': 'PASS', 'revision': id,
                 'qa': {'status': 'PASS', 'revision': id}, 'review': {'status': 'PASS', 'revision': id}}
        fresh = core.delivery_commit(wt, 'a', self.log, outcome)
        self.assertEqual(fresh['status'], 'PASS')
        self.assertEqual(fresh['revision'], id, 'An unchanged product keeps the same identity')
        self.assertEqual(git('log', '--oneline').count('\n'), 1, 'Do not commit leftover files')

    def test_delivery_commit_pathspec_list_matches_common_sh(self):
        """If the two lists diverge the delivery commit either commits leftovers or FAILs acceptance for nothing."""
        import permission
        wt, git = self.product_worktree()
        self.store.update(self.rid, worktree=str(wt))
        commands, target, product = permission.commit_details(self.store, self.rid, wt, 'Fixture product')
        expected = tuple(path.replace(':(exclude,glob)', ':(glob)').replace(':(exclude)', '') for path in core.PRODUCT_EXCLUDED)
        self.assertEqual(tuple(commands[1][commands[1].index('--') + 1:]), expected)
        self.assertIn('lib/permission.py" checkpoint', (LIB/'common.sh').read_text())

    def test_qa_two_failures_return_structured_failure(self) -> None:
        result = core.qa_gate('a', str(self.wt), self.log, 'forbidden-live-cli')
        self.assertIsInstance(result, dict)
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(len(result['attempts']), 2)

    def test_qa_timeout_returns_error(self) -> None:
        """LIVE1 (03.10): the fixed QA timeout was removed; it may continue until the liveness check detects stuck.
        sleep 1 completes normally -> PASS."""
        Path(self.run['constraints']['qa-command']).write_text(qa_command('sleep 1'))
        result = core.qa_gate('a', str(self.wt), self.log, 'forbidden-live-cli')
        self.assertIsInstance(result, dict)
        self.assertEqual(result['status'], 'PASS')

    def test_binary_override_does_not_bypass_qa(self) -> None:
        result = core.qa_gate('a', str(self.wt), self.log, '/fake/cli', fake_run=True)
        self.assertIsInstance(result, dict)
        self.assertEqual(result['status'], 'FAIL')

    def test_missing_qa_is_explicitly_skipped(self) -> None:
        (self.project/'qa-command').unlink()
        Path(self.run['constraints']['qa-command']).unlink()
        result = core.qa_gate('a', str(self.wt), self.log, '/fake/cli')
        self.assertIsInstance(result, dict)
        self.assertEqual(result['status'], 'SKIPPED')

    def test_coordinator_rejects_zero_exit_without_acceptance_evidence(self) -> None:
        root = self.home/'code'
        (root/'bin').mkdir(parents=True)
        (root/'bin/job-runner.sh').write_text('exit 0\n')
        with patch.dict(os.environ, {'CADRE_ENGINE_ROOT':str(root), 'CADRE_DISPATCH':'0'}):
            rc = execute_run(self.store, self.rid)
        self.assertNotEqual(rc, 0)
        self.assertNotEqual(self.store.read(self.rid)['state'], 'completed')

    def test_agent_commits_on_the_run_branch_are_detected_and_reported(self) -> None:
        """B173 (23.09 T1v3, N1): even if the task text says 'do not commit', the executor can make its own commit on the cadre/* branch; no
        gate reported it. Default (reversible): detect + record + log; the delivery revision is still HEAD."""
        self.successful_qa()
        for file, message in (('h.py', 'wip: harness intermediate record (1 file) — automatic intermediate record'), ('agent.py', 'feat: agent commit')):
            (self.wt/file).write_text('x = 1\n')
            self.git('add', file)
            self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-qm', message)
        with patch.object(core, 'capture_opencode', return_value=self.review()):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli')
        topics = [c['subject'] for c in result['agent_commits']]
        self.assertIn('feat: agent commit', topics)
        self.assertFalse(any(k.startswith('wip: harness') for k in topics), 'Harness checkpoints do not count as agent commits')
        self.assertIn('AGENT COMMIT', Path(self.log).read_text())

    def test_quality_chain_is_requested_with_the_declared_reviewer_size(self) -> None:
        """B158(c): the judge chain was asked with a fixed 30000; the B121 principle — a caller that does not know the size uses the role's declaration."""
        self.successful_qa()
        with patch.object(core.ROUTER, 'chain', return_value=(['deepseek/deepseek-v4-pro'], [])) as chain, \
             patch.object(core, 'budget_json', return_value={'ts': 'x'}), \
             patch.object(core, 'capture_opencode', return_value=self.review()):
            core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=None)
        quality = [c for c in chain.call_args_list if c.args and c.args[0] == 'quality']
        self.assertTrue(quality)
        self.assertIsNone(quality[0].args[2], 'With request size None, the router uses the declared role size of quality 56000')

    def test_reviewer_prompt_states_that_qa_repair_changed_the_product(self) -> None:
        """B158(f): when the QA repair round changed the product the judge prompt did not say so explicitly (only a trace in the qa JSON)."""
        mark = self.wt / '.pytest_cache/qa.ok'
        mark.parent.mkdir()
        Path(self.run['constraints']['qa-command']).write_text(qa_command(f'test -f {mark} && exit 0 || {{ touch {mark}; exit 1; }}'))
        prompts = []

        def judge(real, wt, args, timeout, env=None, role='quality'):
            prompts.append(args[-1])
            return self.review()

        with patch.object(core, 'run_opencode', side_effect=lambda *a, **k: subprocess.Popen(['true'])), \
             patch.object(core, 'capture_opencode', side_effect=judge):
            core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
        self.assertTrue(prompts)
        # B158(f): the QA repair prompt starts with QA GATE RED; the judge prompt contains QA REPAIR DONE
        self.assertTrue(any('QA REPAIR PERFORMED' in i for i in prompts), f'no prompt contains QA REPAIR PERFORMED: {prompts}')

    def test_changed_existing_test_file_becomes_a_separate_review_criterion(self) -> None:
        """B167: the count gate (G-C7) cannot see a weaker assert with the same count; a test file that existed in the source and changed
        is given to the judge as a separate criterion. If only a new test is added, no criterion is added."""
        (self.wt/'test_product.py').write_text('def test_x():\n    assert 1 + 1 == 2\n')
        self.git('add', '.')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-qm', 'test added')
        basic = self.git('rev-parse', 'HEAD').strip()

        def acceptance_prompt():
            record = self.store.accept('a', 'write the product', source_sha=basic)
            self.store.update(record['run_id'], worktree=str(self.wt))
            Path(record['constraints']['qa-command']).write_text(qa_command())
            prompts = []

            def judge(real, wt, args, timeout, env=None, role='quality'):
                prompts.append(args[-1])
                return self.review('PASS')

            with patch.dict(os.environ, {**self.store.environment(record['run_id']), 'CADRE_TASK_ID': '', 'CADRE_OWNER_FDS': ''}), \
                 patch.object(core, 'capture_opencode', side_effect=judge), \
                 patch.object(core, 'worktree_base_sha', return_value=basic):
                core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
            return prompts[0] if prompts else ''

        (self.wt/'test_fresh.py').write_text('def test_y():\n    assert True is not False\n')
        self.assertNotIn('changed_tests', acceptance_prompt(), 'A new test alone does not add a criterion')
        (self.wt/'test_product.py').write_text('def test_x():\n    assert 1 + 1 >= 0\n')
        prompt = acceptance_prompt()
        self.assertIn('"id": "changed_tests"', prompt)
        self.assertIn('test_product.py', prompt)

    def test_qa_repair_attempt_start_and_end_are_logged(self) -> None:
        """B170 (23.09 T1b, T1v2): the repair attempt after a QA FAIL was never written to the engine log; 14 min of silence
        could only be read from the attempt record. The start and end of the repair are visible in the log."""
        mark = self.wt / '.pytest_cache/qa.ok'
        mark.parent.mkdir()
        Path(self.run['constraints']['qa-command']).write_text(qa_command(f'test -f {mark} && exit 0 || {{ touch {mark}; exit 1; }}'))
        with patch.object(core, 'run_opencode', side_effect=lambda *a, **k: subprocess.Popen(['true'])):
            result = core.qa_gate('a', str(self.wt), self.log, '/fake/cli')
        self.assertEqual(result['status'], 'PASS')
        daily = Path(self.log).read_text()
        self.assertIn('QA REPAIR: starting', daily)
        self.assertIn('QA REPAIR: finished (', daily)

    def successful_qa(self) -> None:
        from qa_fixture import command
        Path(self.run['constraints']['qa-command']).write_text(command())

    def review_for_prompt(self, verdict='PASS'):
        """A fake judge answering all criterion ids in the JSON at the end of the prompt (B167: the criterion count changes with the product).
        P3/LIVE1: QA repair prompts contain no JSON; in that case it simply passes."""
        def judge(real, wt, args, timeout, env=None, role='quality'):
            prompt = args[-1]
            if '{"revision"' not in prompt:
                return self.review(verdict)    # QA repair prompt
            data = json.JSONDecoder().raw_decode(prompt[prompt.rindex('{"revision"'):])[0]
            return self.review(verdict, ids=tuple(c['id'] for c in data['criteria']))
        return judge

    def review(self, verdict='PASS', *, identity=None, ids=('main',)) -> str:
        return review_output(json.dumps({'revision': (identity or revision(self.wt))['digest'], 'verdict':verdict,
                           'acceptance':[{'id':i,'status':'PASS','evidence':'product.txt inspected; QA exit 0'} for i in ids]}))

    def test_final_reviewer_receives_engine_scope_evidence_instead_of_a_git_instruction(self) -> None:
        self.successful_qa()
        prompts = []
        judge = self.review_for_prompt('PASS')
        def capture(real, wt, args, timeout, env=None, role='quality'):
            prompts.append(args[-1])
            return judge(real, wt, args, timeout, env, role)
        with patch.object(core, 'capture_opencode', side_effect=capture):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
        self.assertEqual(result['status'], 'PASS', result)
        prompt = [p for p in prompts if 'ENGINE EVIDENCE' in p]
        self.assertTrue(prompt, prompts[-1][:300] if prompts else 'The judge was not called')
        self.assertIn('product.txt', prompt[-1])
        self.assertIn('changed_files (', prompt[-1])
        self.assertIn(f"source {self.base}", prompt[-1])
        self.assertNotIn('git diff', prompt[-1])
        from acceptance import runner_evidence
        typed = runner_evidence({'request': {'duration_minutes': 40}, 'request_clock': {'active_sec': 1852.4}, 'budget_spent_usd': 0.2,
                              'budget_held_usd': 0.01, 'budget_cap_usd': 4.5}, ['a.py', 'b.py'], {'head': 'h1', 'digest': 'd1'}, 'h0',
                             [{'sha': 'abc', 'subject': 'worker commit'}])
        self.assertIn('1 executor commits: abc worker commit', typed)
        self.assertIn('1852 s / 2400 s', typed)
        self.assertIn('cap 4.5 USD', typed)

    def test_quality_revise_cannot_pass_even_when_qa_is_green(self) -> None:
        self.successful_qa()
        with patch.object(core, 'capture_opencode', return_value=self.review('REVISE')):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
        self.assertEqual(result['qa']['status'], 'PASS')
        self.assertEqual(result['status'], 'FAIL')
        self.assertFalse(approved(self.store.read(self.rid)))

    def test_reviewer_falls_through_the_quality_chain_when_a_model_fails(self) -> None:
        """B37 (live acceptance 01:37): if the judge model fails with a budget denial/provider error acceptance must not be ERROR; the next
        link in the chain is tried and the model used is recorded. If all links fail the reason is 'judge chain exhausted'."""
        self.successful_qa()
        calls = []
        def reviewer(real, wt, args, timeout, env=None, role='quality'):
            model = args[args.index('-m') + 1] if '-m' in args else 'local'
            calls.append(model)
            if model == 'opencode-go/glm-5.2':
                raise subprocess.CalledProcessError(1, ['opencode'])
            return self.review('PASS')
        chain = (['opencode-go/glm-5.2', 'deepseek/deepseek-v4-pro'], [])
        self.enter(patch.object(core, 'is_judge_small', return_value=False))   # large change: router order (B38 in a separate test)
        with patch.object(core, 'capture_opencode', side_effect=reviewer), patch.object(core, 'budget_json', return_value={'ts': 'x'}), \
             patch.object(core.ROUTER, 'chain', return_value=chain), patch.object(core.ROUTER, 'failure') as failure:
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=None)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(calls, ['opencode-go/glm-5.2', 'deepseek/deepseek-v4-pro'])
        self.assertEqual(result['review']['model'], 'deepseek/deepseek-v4-pro')
        failure.assert_called_once()
        self.assertEqual(failure.call_args.args[0], 'opencode-go')
        # the chain kicks in also if the model given by the dispatcher fails
        calls.clear()
        with patch.object(core, 'capture_opencode', side_effect=reviewer), patch.object(core, 'budget_json', return_value={'ts': 'x'}), \
             patch.object(core.ROUTER, 'chain', return_value=chain), patch.object(core.ROUTER, 'failure'):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='opencode-go/glm-5.2')
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(calls, ['opencode-go/glm-5.2', 'deepseek/deepseek-v4-pro'])
        # all fail -> ERROR, the tried ones in the reason
        calls.clear()
        with patch.object(core, 'capture_opencode', side_effect=subprocess.CalledProcessError(1, ['x'])), patch.object(core, 'budget_json', return_value={'ts': 'x'}), \
             patch.object(core.ROUTER, 'chain', return_value=chain), patch.object(core.ROUTER, 'failure'):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=None)
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['review']['status'], 'ERROR')
        self.assertRegex(result['review']['reason'], r'^judge chain exhausted: opencode-go/glm-5\.2: CalledProcessError[^;]*; deepseek/deepseek-v4-pro: CalledProcessError')  # B48: the real reason text is in the record too
        self.assertEqual(len(result['review']['tried']), 2)  # B48: tried links in a structured field

    def test_cancel_stops_the_acceptance_stage_before_qa_and_before_the_reviewer(self) -> None:
        """B110 (18.09): cancel NEVER reached the acceptance stage; after `cadre cancel` QA, the judge round and the judge
        repair went on for minutes and with MONEY."""
        self.successful_qa()
        (self.store.path(self.rid) / 'cancel.flag').write_text('requested\n')
        with patch.object(core, 'qa_gate', side_effect=AssertionError('Do not run QA after cancellation')), \
             patch.object(core, 'capture_opencode', side_effect=AssertionError('Do not call the judge after cancellation')):
            outcome = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
        self.assertEqual(outcome['status'], 'CANCELLED')
        self.assertEqual(self.store.read(self.rid)['final_result']['status'], 'CANCELLED')
        self.assertFalse(approved(self.store.read(self.rid)))

    def test_cancel_during_qa_stops_the_process_and_never_calls_the_reviewer(self):
        import threading
        import time
        Path(self.run['constraints']['qa-command']).write_text('echo $$ > qa.pid; sleep 3')
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
        self.assertFalse(core.pid_alive(int((self.wt/'qa.pid').read_text())))
        reviewer.assert_not_called()

    def test_deleting_a_test_file_never_gets_permission_from_free_text(self) -> None:
        """B140: negative or positive natural language cannot produce an automatic acceptance exception."""
        (self.wt/'test_product.py').write_text('def test_x():\n    assert True\n')
        self.git('add', '.')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-qm', 'test added')
        basic = self.git('rev-parse', 'HEAD').strip()
        (self.wt/'test_product.py').unlink()
        self.git('add', '-A')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-qm', 'test deleted')

        def acceptance(text):
            record = self.store.accept('a', text, source_sha=basic)
            self.store.update(record['run_id'], worktree=str(self.wt))
            writer = self.store.task_dir(record['run_id'], 'impl')/'attempts/fixture/attempt.json'
            writer.parent.mkdir(parents=True)
            writer.write_text(json.dumps({'role': 'impl', 'model': 'fixture/writer', 'pid': 123}))
            Path(record['constraints']['qa-command']).write_text(qa_command())      # this run's QA is green
            with patch.dict(os.environ, {**self.store.environment(record['run_id']), 'CADRE_TASK_ID': '',
                                         'CADRE_OWNER_FDS': ''}), \
                 patch.object(core, 'capture_opencode', return_value=self.review('PASS')), \
                 patch.object(core, 'worktree_base_sha', return_value=basic):
                return core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')

        for text in ('write the product', 'Do not delete tests', 'Do not delete tests',
                      'Never remove tests', 'Delete obsolete tests and simplify the module'):
            with self.subTest(prompt=text):
                outcome = acceptance(text)
                self.assertEqual(outcome['status'], 'FAIL')
                self.assertIn('test deleted', outcome['reason'])
                self.assertEqual(outcome['deleted_tests'], ['test_product.py'])

    def test_weakening_a_test_file_blocks_automatic_acceptance_but_legit_edits_pass(self) -> None:
        """B144: a known limit of the deletion gate — a test file could be emptied without being deleted (`assert True`).
        If the assertion count DROPS in a test file that changed against the accepted source there is no automatic acceptance
        (reason and file in the record); a legitimate change that keeps/raises the assertion count does not affect automatic acceptance."""
        (self.wt/'test_product.py').write_text('def test_a():\n    assert 1 == 1\n    assert 2 == 2\n\n'
                                            'def test_b():\n    assert 3 == 3\n')
        self.git('add', '.')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-qm', 'tests')
        basic = self.git('rev-parse', 'HEAD').strip()
        cases = [
            ('weak', 'def test_a():\n    assert True\n\ndef test_b():\n    assert True\n', 'FAIL'),
            ('empty', 'def test_a():\n    pass\n\ndef test_b():\n    assert 3 == 3\n', 'FAIL'),
            ('legitimate', 'def test_a():\n    assert 1 == 1\n    assert 2 == 2\n    assert 4 == 4\n\n'
                      'def test_b():\n    assert 3 == 3\n', 'PASS'),
            ('again-naming', 'def test_a_renamed():\n    assert 1 == 1\n    assert 2 == 2\n\n'
                                   'def test_b():\n    assert 3 == 3\n', 'PASS'),
        ]
        for entry_name, content, expected in cases:
            with self.subTest(case=entry_name):
                (self.wt/'test_product.py').write_text(content)
                record = self.store.accept('a', 'fix the product', source_sha=basic)
                self.store.update(record['run_id'], worktree=str(self.wt))
                writer = self.store.task_dir(record['run_id'], 'impl')/'attempts/fixture/attempt.json'
                writer.parent.mkdir(parents=True)
                writer.write_text(json.dumps({'role': 'impl', 'model': 'fixture/writer', 'pid': 123}))
                Path(record['constraints']['qa-command']).write_text(qa_command())
                with patch.dict(os.environ, {**self.store.environment(record['run_id']), 'CADRE_TASK_ID': '',
                                             'CADRE_OWNER_FDS': ''}), \
                     patch.object(core, 'capture_opencode', side_effect=self.review_for_prompt('PASS')), \
                     patch.object(core, 'worktree_base_sha', return_value=basic):
                    outcome = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
                self.assertEqual(outcome['status'], expected, outcome.get('reason'))
                if expected == 'FAIL':
                    self.assertIn('test weakened', outcome['reason'])
                    self.assertEqual(outcome['weakened_tests'], ['test_product.py'])

    def test_skipping_every_test_cannot_turn_qa_green(self) -> None:
        """G-C2/G-C7 (B152, audit agent e3): a broken product + `@unittest.skip` → `OK (skipped=N)` rc=0, the same assertion count,
        and `finalize_run` gave PASS. (a) more skips in a changed test file = weakening; (b) even without the test file changing
        (an unconditional skip by deleting a fixture file) QA does not count as PASS if no test ran in the QA output."""
        base_test = ('import os, unittest\nfrom product import f\n\n'
                     "@unittest.skipUnless(os.path.exists('enable.flag'), 'flag absent')\n"
                     'class T(unittest.TestCase):\n    def test_f(self):\n        self.assertEqual(f(), 1)\n'
                     '    def test_g(self):\n        self.assertTrue(callable(f))\n')
        (self.wt/'product.py').write_text('def f():\n    return 1\n')
        (self.wt/'test_u.py').write_text(base_test)
        (self.wt/'enable.flag').write_text('1\n')
        self.git('add', '.')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-qm', 'product+test')
        basic = self.git('rev-parse', 'HEAD').strip()
        cases = [
            # (a) skip only the broken test: QA still runs one test and is green -> the weakening gate must catch it
            ('skipping-added', lambda: (self.wt/'test_u.py').write_text(base_test.replace('    def test_f', "    @unittest.skip('x')\n    def test_f")), 'qa-required-test-skipped'),
            ('fixture-deleted', lambda: (self.wt/'enable.flag').unlink(), 'qa-all-skipped'),
        ]
        for entry_name, spoil, expected in cases:
            with self.subTest(case=entry_name):
                self.git('checkout', '-q', '--', '.')
                if not (self.wt/'enable.flag').exists():
                    (self.wt/'enable.flag').write_text('1\n')
                (self.wt/'product.py').write_text('def f():\n    return 2\n')     # the product is broken
                spoil()
                record = self.store.accept('a', 'fix the product', source_sha=basic)
                self.store.update(record['run_id'], worktree=str(self.wt))
                writer = self.store.task_dir(record['run_id'], 'impl')/'attempts/fixture/attempt.json'
                writer.parent.mkdir(parents=True)
                writer.write_text(json.dumps({'role': 'impl', 'model': 'fixture/writer', 'pid': 123}))
                Path(record['constraints']['qa-command']).write_text("python3 -m unittest discover -p 'test_*.py'")
                with patch.dict(os.environ, {**self.store.environment(record['run_id']), 'CADRE_TASK_ID': '',
                                             'CADRE_OWNER_FDS': ''}), \
                     patch.object(core, 'capture_opencode', side_effect=self.review_for_prompt('PASS')), \
                     patch.object(core, 'worktree_base_sha', return_value=basic):
                    outcome = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
                self.assertEqual(outcome['status'], 'FAIL', outcome)
                self.assertIn(expected, outcome.get('reason', '') + (outcome.get('qa') or {}).get('reason', ''))

    def test_echoing_the_local_review_template_is_not_a_valid_review(self) -> None:
        """G-C5 (B153, audit agent e2): when the local judge echoed the template lines in the prompt (`DECISION: PASS`, `CRITERION main: PASS - <concrete evidence>`)
        verbatim, it counted as a valid PASS review. A template echo and placeholder evidence are not a review."""
        self.successful_qa()
        def reflect(real, wt, args, timeout, env=None, role='quality'):
            prompt = args[-1]
            block = prompt.split('IF YOU CANNOT PRODUCE JSON', 1)[1]
            return '\n'.join(l for l in block.splitlines() if l.startswith(('REVISION:', 'DECISION:', 'CRITERION ')))
        with patch.object(core, 'capture_opencode', side_effect=reflect):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
        self.assertNotEqual(result['status'], 'PASS', result)
        self.assertNotEqual(result['review'].get('status'), 'PASS')
        # at parser level: placeholder evidence and an echoed choice spelling are rejected
        rev = revision(self.wt)
        for text in (f"REVISION: {rev['digest']}\nDECISION: PASS\nCRITERION main: PASS - <concrete evidence>",
                      f"REVISION: {rev['digest']}\nDECISION: PASS|REVISE\nCRITERION main: PASS|FAIL - file read"):
            self.assertNotEqual(parse_review(text, rev, ['main'])['status'], 'PASS', text)
        self.assertNotEqual(parse_review(json.dumps({'revision': rev['digest'], 'verdict': 'PASS',
                                                      'acceptance': [{'id': 'main', 'status': 'PASS', 'evidence': '...'}]}),
                                         rev, ['main'])['status'], 'PASS')

    def test_common_test_file_names_are_protected_by_the_deletion_gate(self) -> None:
        """G-C7 (B157): the gate did not see the deletion of `*.spec.ts`, `__tests__/`, `conftest.py`, `pytest.ini`, `*Test.java`."""
        names = ['app/user.spec.ts', 'web/user.spec.js', 'src/__tests__/foo.js', 'conftest.py', 'pytest.ini',
                 'src/UserTest.java', 'tests/test_x.py']
        for entry_name in names:
            (self.wt/entry_name).parent.mkdir(parents=True, exist_ok=True)
            (self.wt/entry_name).write_text('x\n')
        self.git('add', '.')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-qm', 'tests')
        basic = self.git('rev-parse', 'HEAD').strip()
        for entry_name in names:
            (self.wt/entry_name).unlink()
        self.git('add', '-A')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-qm', 'deleted')
        self.assertEqual(sorted(core.deleted_tests(str(self.wt), basic)), sorted(names))

    def test_a_provider_less_refusal_falls_back_to_the_conservative_ban(self) -> None:
        """G-B5 (B154, audit agent e4): the plugin's `deny()` receipt came without a provider (`refusals:[{provider:null}]`) and
        `apply_budget_denial` produced no ban → a loop back to the same provider. A provider denial without a provider falls to the cautious
        fallback; a role denial still closes nobody; a denial with a provider closes only that one."""
        for refusals, expected in (
            ([{'provider': None, 'scope': 'provider', 'reason': 'plugin deny'}], set((*core.BUDGET_PAID, *core.BUDGET_FREE, 'claude'))),
            ([{'provider': None, 'scope': 'role', 'reason': 'run budget exhausted'}], set()),
            ([{'provider': 'deepseek', 'scope': 'provider', 'reason': 'allocation exhausted'}], {'deepseek'}),
        ):
            with self.subTest(refusals=refusals):
                bans = Mock()
                core.apply_budget_denial(bans, {'refusals': refusals}, None)
                self.assertEqual({c.args[0] for c in bans.block.call_args_list}, expected)

    def test_qa_generated_caches_do_not_count_as_a_product_change(self) -> None:
        """G-C2 (B156, audit agent e3): when the QA command produced `__pycache__`, `revision` came out different and the first round
        became `ERROR source-changed-during-qa` (an empty round). Cache/harness leftovers do not enter the product identity."""
        Path(self.run['constraints']['qa-command']).write_text(qa_command(
            "python3 -c \"import pathlib; d=pathlib.Path('__pycache__'); d.mkdir(exist_ok=True); (d/'a.cpython-314.pyc').write_bytes(b'x')\""))
        with patch.object(core, 'capture_opencode', return_value=self.review('PASS')):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
        self.assertEqual(result['qa']['status'], 'PASS', result['qa'])
        self.assertEqual(result['qa']['reason'], 'command-result')
        self.assertEqual(result['status'], 'PASS', result.get('reason'))

    def test_the_model_that_wrote_the_product_reviews_it_last(self) -> None:
        """B110 (18.09): judge independence was NEVER enforced IN PRODUCTION; the only guard test checked the WRONG chain
        ('review'), while the judge uses the 'quality' chain. The model that wrote the product is thrown to the end of the chain;
        without another link the review is recorded with an `independent=False` stamp and acceptance is rejected."""
        import budget_gate
        self.successful_qa()
        calls = []

        def reviewer(real, wt, args, timeout, env=None, role='quality'):
            calls.append(args[args.index('-m') + 1] if '-m' in args else 'local')
            return self.review('PASS')

        chain = (['opencode-go/glm-5.2', 'deepseek/deepseek-v4-pro'], [])
        with patch.object(core, 'capture_opencode', side_effect=reviewer), \
             patch.object(core, 'budget_json', return_value={'ts': 'x'}), \
             patch.object(core.ROUTER, 'chain', return_value=chain), \
             patch.object(budget_gate, 'run_models', return_value={'glm-5.2'}):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=None)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(calls[0], 'deepseek/deepseek-v4-pro', 'The product writer cannot move ahead in the judge order')
        self.assertTrue(result['review']['independent'])

        calls.clear()
        single = (['opencode-go/glm-5.2'], [])      # no other link (local is also a writer): no independent acceptance
        with patch.object(core, 'capture_opencode', side_effect=reviewer), \
             patch.object(core, 'budget_json', return_value={'ts': 'x'}), \
             patch.object(core.ROUTER, 'chain', return_value=single), \
             patch.object(budget_gate, 'run_models', return_value={'glm-5.2', 'qwen3.8-27b'}):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=None)
        self.assertEqual(calls, ['opencode-go/glm-5.2'])
        self.assertTrue(result['review']['independent'])
        self.assertIsNotNone(result['review']['session_proof'])
        self.assertEqual(result['status'], 'PASS')
        self.assertTrue(approved(self.store.read(self.rid)))


    def test_judge_whose_two_calls_do_not_fit_the_run_pocket_is_skipped_before_being_called(self) -> None:
        """B242 (26.09 live R14-easy): the Claude judge's first call passed the gate ($0.18 spent), its second call did not fit the run's judge
        pocket; the link was wasted. The breathing filter (B239b's judge counterpart) skips the candidate without calling it; the chain continues."""
        import budget_gate
        self.successful_qa()
        calls = []

        def reviewer(real, wt, args, timeout, env=None, role='quality'):
            calls.append(args[args.index('-m') + 1] if '-m' in args else 'local')
            return self.review('PASS')

        chain = (['claude/opus#max', 'opencode/big-pickle'], [])
        breath = lambda m, b: (1.42, 7.27, 'pocket') if m.startswith('claude/') else None
        with patch.object(core, 'capture_opencode', side_effect=reviewer), \
             patch.object(core, 'budget_json', return_value={'ts': 'x'}), \
             patch.object(core.ROUTER, 'chain', return_value=chain), \
             patch.object(core.ROUTER, 'judge_breath', side_effect=breath), \
             patch.object(budget_gate, 'run_models', return_value={'qwen3.8-27b'}):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=None)
        self.assertEqual(calls, ['opencode/big-pickle'], 'Skip Claude before calling it when funds are insufficient; continue the chain')
        self.assertEqual(result['review']['breathless'], ['claude/opus#max'])
        self.assertEqual(result['status'], 'PASS')
        self.assertIn('B242', self.log.read_text())

    def test_the_last_independent_judge_reviews_read_only(self) -> None:
        """B251 (26.09 live B203d f1): the only independent candidate big-pickle made a small touch to the product -> 'review-changed-product'
        -> no other candidate -> the child failed. The final independent judge runs READ-ONLY: it does not change, it only decides;
        earlier links may make small fixes."""
        import budget_gate
        self.successful_qa()
        prompts = {}

        def reviewer(real, wt, args, timeout, env=None, role='quality'):
            model = args[args.index('-m') + 1] if '-m' in args else 'local'
            prompts[model] = args[-1]
            return 'malformed prose' if model == 'opencode-go/deepseek-v4-pro' else self.review('PASS')

        chain = (['opencode-go/deepseek-v4-pro', 'opencode-go/glm-5.2'], [])
        with patch.object(core, 'capture_opencode', side_effect=reviewer), \
             patch.object(core, 'budget_json', return_value={'ts': 'x'}), \
             patch.object(core.ROUTER, 'chain', return_value=chain), \
             patch.object(core.ROUTER, 'judge_breath', return_value=None), \
             patch.object(budget_gate, 'run_models', return_value={'qwen3.8-27b'}):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=None)
        self.assertEqual(result['status'], 'PASS')
        self.assertIn('READ-ONLY ROUND', prompts['opencode-go/deepseek-v4-pro'])
        self.assertNotIn('You may make a small correction', prompts['opencode-go/deepseek-v4-pro'])
        self.assertIn('READ-ONLY ROUND', prompts['opencode-go/glm-5.2'], 'last independent judge read-only')
        self.assertNotIn('You may make a small correction', prompts['opencode-go/glm-5.2'])

    def test_discarded_lane_models_are_not_product_writers(self) -> None:
        """B256: the model of a discarded lane (rejected.flag) does not count as having written the product; it can be an independent judge."""
        path = self.store.task_dir(self.rid, 'g1-analyst-b') / 'attempts/f2/attempt.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'role': 'impl', 'model': 'deepseek/deepseek-v4-pro', 'pid': 5, 'outcome': 'finished'}))
        self.assertIn(core.model_id('deepseek/deepseek-v4-pro'), core.product_writing_models(self.store, self.rid))
        (self.store.task_dir(self.rid, 'g1-analyst-b') / 'rejected.flag').write_text('')
        self.assertNotIn(core.model_id('deepseek/deepseek-v4-pro'), core.product_writing_models(self.store, self.rid))
        self.assertIn(core.model_id('fixture/writer'), core.product_writing_models(self.store, self.rid), 'The actual writer stays')

    def _saving_judge(self, invoked):
        def judge(real, wt, args, timeout, env=None, role='quality'):
            invoked.append(args[args.index('-m') + 1] if '-m' in args else 'local')
            return self.review()
        return judge

    def _writer_classification(self, result, model, invoked, independent):
        """B180: the writer classification is now applied by candidate filtering. A given judge that is a writer is removed and never called;
        a given judge that is not a writer does the review and gives an independent PASS."""
        self.assertEqual(core.model_id(model) not in result['review']['writers'], independent)
        self.assertNotIn(model, result['review']['excluded_writers'])
        self.assertIn(model, invoked)
        self.assertTrue(result['review']['independent'])
        self.assertEqual(result['status'], 'PASS')

    def _judge_attempt_write(self, attempt_id, model, **extra):
        path = self.store.task_dir(self.rid, 'main')/f'attempts/{attempt_id}/attempt.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'attempt_id': attempt_id, 'role': 'quality', 'model': model, 'pid': 321,
                                    'outcome': 'finished', **extra}))

    def _judge_reservation_write(self, attempt_id, model):
        """The plugin receipt of a cloud judge: the main agent says 'build', the engine phase 'quality', the attempt id comes from the environment."""
        import io

        import budget_gate as budget
        from test_budget_period import snapshot
        request = {'action': 'reserve', 'provider': 'opencode-go', 'maximum': 1,
                   'metadata': {'role': 'build', 'model': model}}
        env = {'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(LIB.parent), 'CADRE_ROLE': 'quality',
               'CADRE_BUDGET_ATTEMPT': attempt_id}
        with patch.dict(os.environ, env), patch.object(budget, 'observed_budget', return_value=snapshot(5)), \
             patch.object(sys, 'argv', ['budget_gate.py']), \
             patch.object(sys, 'stdin', io.StringIO(json.dumps(request))), patch.object(sys, 'stdout', io.StringIO()):
            self.assertEqual(budget.main(), 0)

    def test_restart_keeps_a_recorded_readonly_reviewer_independent(self):
        """§0a/10(a): on a restart earlier judge attempts cautiously counted as WRITERS and the chain was exhausted
        for nothing. A judge RECORDED as not touching the product is not a writer (attempt record + plugin receipt together);
        an attempt that touched it or has an incomplete record stays a writer."""
        self.successful_qa()
        cases = [('reads', {'product_changed': False}, True), ('writer', {'product_changed': True}, False),
                 ('half', {}, False)]
        for attempt_id, record, independent in cases:
            judge = f'fixture/judge-{attempt_id}'
            with self.subTest(attempt_id=attempt_id):
                self._judge_attempt_write(attempt_id, judge, **record)
                self._judge_reservation_write(attempt_id, judge)
                invoked = []
                with patch.object(core, 'capture_opencode', side_effect=self._saving_judge(invoked)):
                    result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=judge)
                self._writer_classification(result, judge, invoked, independent)

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
                records = sorted((json.loads(p.read_text()) for p in
                                   (self.store.task_dir(self.rid, 'main')/'attempts').glob('*/attempt.json')),
                                  key=lambda k: k.get('created', 0))
                judge = [k for k in records if k.get('role') == 'quality'][-1]
                self.assertEqual(judge['product_changed'], changed)
                self.assertEqual(judge['product_after'] == judge['product_before'], not changed)

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
                records = sorted((json.loads(p.read_text()) for p in
                                   (self.store.task_dir(self.rid, 'main')/'attempts').glob('*/attempt.json')),
                                  key=lambda k: k.get('created', 0))
                judge = [k for k in records if k.get('role') == 'quality'][-1]
                self.assertIs(judge.get('product_changed'), changed)
                self.assertEqual(core.model_id('opencode-go/glm-5.3') in core.product_writing_models(self.store, self.rid), changed)

    def test_dispatch_review_requires_the_original_request_in_addition_to_plan(self):
        self.successful_qa()
        Path(self.run['prompt']).write_text('Provide CSV and JSON outputs; test both.')
        (self.store.path(self.rid)/'plan.json').write_text(json.dumps({
            'tasks': [{'id': 'g1', 'acceptance': 'CSV file exists'}]}))
        self.store.update(self.rid, phase='dispatch')
        self.store.task_update(self.rid, 'g1', dispatch_result='produced')
        with patch.object(core, 'capture_opencode', return_value=self.review(ids=('g1',))) as reviewer:
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
        self.assertNotEqual(result['status'], 'PASS', 'The plan cannot drop the original JSON requirement')
        payload = json.loads(reviewer.call_args.args[2][-1].splitlines()[-1])
        self.assertEqual(payload['original_request'], Path(self.run['prompt']).read_text())
        self.assertEqual(len(payload['criteria']), 2)

    def test_request_ledger_phase_distinguishes_planner_from_writers_during_acceptance(self):
        import io

        import budget_gate as budget
        from test_budget_period import snapshot
        self.successful_qa()
        cases = [('plan', 'build', 'planner', True), ('impl', 'build', 'planner', False),
                 ('plan', 'cadre-review', 'helper', False), ('impl', 'compaction', 'compact', True),
                 ('quality', 'build', 'reviewer', False), ('', 'build', 'unknown', False)]
        for phase, agent, name, independent in cases:
            with self.subTest(phase=phase, agent=agent):
                model = f'fixture/{name}'
                request = {'action': 'reserve', 'provider': 'opencode-go', 'maximum': 1,
                           'metadata': {'role': agent, 'model': model}}
                env = {'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(LIB.parent),
                       'CADRE_ROLE': phase}
                with patch.dict(os.environ, env), \
                     patch.object(budget, 'observed_budget', return_value=snapshot(5)), \
                     patch.object(sys, 'argv', ['budget_gate.py']), \
                     patch.object(sys, 'stdin', io.StringIO(json.dumps(request))), \
                     patch.object(sys, 'stdout', io.StringIO()):
                    self.assertEqual(budget.main(), 0)
                invoked = []
                with patch.object(core, 'capture_opencode', side_effect=self._saving_judge(invoked)):
                    result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=model)
                self._writer_classification(result, model, invoked, independent)

    def test_a_direct_helper_call_is_never_a_product_writer(self):
        """07.10 smoke: the pre-filter's free groq gpt-oss call (adapter=direct, no tools) counted as a writer; the gpt family was removed from judging."""
        import budget_gate as budget
        from test_budget_period import snapshot
        ledger = budget.BudgetLedger(self.home / 'chain/request-budget.db')
        ledger.reserve('opencode-go', 1, snapshot(5), {'adapter': 'direct', 'model': 'openai/gpt-oss-120b', 'run_id': 'a', 'task_id': 'helper'})
        ledger.reserve('opencode-go', 1, snapshot(5), {'role': 'build', 'phase_role': 'impl', 'model': 'deepseek-v4-pro', 'run_id': 'a'})
        self.assertEqual(budget.run_models(self.home, 'a', read_only_excluded=True), {'deepseek-v4-pro'})

    def test_the_planner_family_cannot_judge_the_run_it_planned(self):
        """4b (07.10 user): judge pool = plan pool; the family of the model that made the plan in that run cannot be the judge."""
        import budget_gate
        self.successful_qa()
        self.store.update(self.rid, plan_model='opencode-go/glm-5.3')
        calls = []
        def judge(real, wt, args, timeout, env=None, role='quality'):
            calls.append(args[args.index('-m') + 1] if '-m' in args else '?')
            return self.review('PASS')
        with patch.object(core, 'capture_opencode', side_effect=judge), \
             patch.object(core, 'budget_json', return_value={'ts': 'x'}), \
             patch.object(core.ROUTER, 'chain', return_value=(['opencode-go/glm-5.3', 'opencode-go/kimi-k3'], [])), \
             patch.object(budget_gate, 'run_models', return_value={'deepseek-v4-pro'}):
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
        with patch('budget_gate.run_models', return_value={'gemini-3-flash-preview'}), \
             patch.object(core, 'budget_json', return_value={}), \
             patch.object(core.ROUTER, 'chain', return_value=(['google/gemini-3-flash-preview', 'deepseek/deepseek-v4-pro'], [])), \
             patch.object(core, 'capture_opencode', return_value=self.review()) as reviewer:
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli')
        self.assertEqual(reviewer.call_args.args[2][1], 'deepseek/deepseek-v4-pro')
        self.assertTrue(result['review']['independent'])

    def test_reviewer_budget_denial_never_trips_the_provider_circuit(self):
        self.successful_qa()
        with patch.object(core, 'capture_opencode', side_effect=core.BudgetDenied('run budget exhausted')), \
             patch.object(core.ROUTER, 'failure') as circuit:
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
             patch('budget_gate.run_models', return_value={'cerebras/qwen-3.8-27b'}):
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
             patch.object(core, 'budget_json', return_value={'ts': 'x'}), \
             patch.object(core.ROUTER, 'chain', return_value=(['opencode-go/first', 'opencode/second'], [])), \
             patch.object(core.ROUTER, 'failure'):
            result = core.finalize_run('a', str(self.wt), self.log, str(fake), review_model=None)
        calls = [json.loads(p.read_text()) for p in sorted(self.home.glob('call-*'))]
        self.assertEqual(len(calls), 2, 'The second judge candidate never started')
        self.assertNotIn('--session', calls[1], 'The second judge took over the first session')
        self.assertEqual(result['status'], 'PASS', result.get('reason'))
        self.assertEqual(result['review']['model'], 'opencode/second')

    def test_small_change_prefers_cheap_reviewer_and_malformed_review_falls_through(self) -> None:
        """B38: on a small change (fixture: 1 file, 2 lines) the judge chain goes from cheap to expensive (go -> zen/OR -> claude; no free since 4c-11).
        B37b: a judge returning malformed JSON is a model failure -> next link; it stops when a valid review comes."""
        self.successful_qa()
        calls = []
        def reviewer(real, wt, args, timeout, env=None, role='quality'):
            model = args[args.index('-m') + 1] if '-m' in args else 'local'
            calls.append(model)
            if model == 'opencode-go/glm-5.3-flash':
                return '{"verdict": "PASS"}'   # missing: no revision/acceptance -> parse ERROR
            return self.review('PASS')
        chain = (['claude/opus#max', 'opencode/deepseek-v4-pro', 'opencode-go/glm-5.3-flash', 'opencode-go/glm-5.2'], [])
        with patch.object(core, 'capture_opencode', side_effect=reviewer), patch.object(core, 'budget_json', return_value={'ts': 'x'}), \
             patch.object(core.ROUTER, 'chain', return_value=chain), patch.object(core.ROUTER, 'failure'):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=None)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(calls, ['opencode-go/glm-5.3-flash', 'opencode-go/glm-5.2'])
        self.assertEqual(result['review']['model'], 'opencode-go/glm-5.2')
        self.assertTrue(core.is_judge_small(str(self.wt), self.base))
        # B38b (live 03:15): on a small change the expensive model given by the dispatcher also goes behind the cheap ones
        calls.clear()
        with patch.object(core, 'capture_opencode', side_effect=reviewer), patch.object(core, 'budget_json', return_value={'ts': 'x'}), \
             patch.object(core.ROUTER, 'chain', return_value=chain), patch.object(core.ROUTER, 'failure'):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='opencode/deepseek-v4-pro')
        self.assertEqual(calls[0], 'opencode-go/glm-5.3-flash')
        self.assertEqual(result['status'], 'PASS')
        # large change: the router order is kept (claude first, but capture_opencode does not go to claude -> the run_claude path; only the order is measured here)
        (self.wt/'big.txt').write_text('x\n' * 400)
        self.git('add', '.')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-qm', 'big')
        self.assertFalse(core.is_judge_small(str(self.wt), self.base))
        calls.clear()
        chain = (['opencode-go/glm-5.2', 'opencode-go/glm-5.3-flash'], [])
        with patch.object(core, 'capture_opencode', side_effect=reviewer), patch.object(core, 'budget_json', return_value={'ts': 'x'}), \
             patch.object(core.ROUTER, 'chain', return_value=chain), patch.object(core.ROUTER, 'failure'):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=None)
        self.assertEqual(calls, ['opencode-go/glm-5.2'])
        self.assertEqual(result['status'], 'PASS')

    def test_size_rule_precedes_the_guarantee_and_the_guarantee_moves(self) -> None:
        """4c-5: the guarantee was reserved for Go glm with a small estimate. On a small change Go is first anyway (no move); if the real change
        is large Sol comes first and the guarantee moves to Sol; if it cannot move, the judge with money reserved goes first."""
        self.successful_qa()
        calls = []
        def reviewer(real, wt, args, timeout, env=None, role='quality'):
            calls.append(args[args.index('-m') + 1])
            return self.review('PASS')
        sol, glm = 'openrouter/openai/gpt-6.1-sol', 'opencode-go/glm-5.3'
        self.store.update(self.rid, acceptance_assurance={'models': {'quality': glm}, 'candidates': [glm]})
        def execute(moved):
            calls.clear()
            with patch.object(core, 'capture_opencode', side_effect=reviewer), patch.object(core, 'budget_json', return_value={'ts': 'x'}), \
                 patch.object(core.ROUTER, 'chain', return_value=([sol, glm], [])), patch.object(core.ROUTER, 'failure'), \
                 patch.object(core, 'assurance_carry', return_value=moved) as carry:
                result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=None)
            self.assertEqual(result['status'], 'PASS')
            return carry
        self.assertFalse(execute(True).called, 'For a small change, Go comes first and the assured judge is already first')
        self.assertEqual(calls[0], glm)
        (self.wt/'big.txt').write_text('x\n' * 400)
        self.git('add', '.')
        self.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-qm', 'big')
        carry = execute(True)
        self.assertEqual((calls[0], carry.call_args.args[2]), (sol, sol))
        execute(False)
        self.assertEqual(calls[0], glm, 'If transfer fails, prioritize the judge whose funds are reserved')

    def test_malformed_review_gets_one_format_repair_from_the_same_reviewer(self) -> None:
        """B312 (live 02.10 14:06): the judge broke a correct review with an unescaped quote in the evidence text, the chain FAILed with one candidate.
        On a format error the same judge is asked ONCE for a format repair only; if it comes back valid the chain stops there."""
        self.successful_qa()
        calls, prompts = [], []
        def reviewer(real, wt, args, timeout, env=None, role='quality'):
            model = args[args.index('-m') + 1] if '-m' in args else 'local'
            calls.append(model); prompts.append(args[-1])
            if 'FORMAT REPAIR' in args[-1]:
                self.assertEqual((env or {}).get('CADRE_READ_ONLY'), '1')
                return self.review('PASS')
            return self.review('PASS').replace('"evidence": "', '"evidence": "double escaping &#8220;→", ', 1)
        chain = (['opencode-go/qwen3.7-plus'], [])
        with patch.object(core, 'capture_opencode', side_effect=reviewer), patch.object(core, 'budget_json', return_value={'ts': 'x'}), \
             patch.object(core.ROUTER, 'chain', return_value=chain), patch.object(core.ROUTER, 'failure'):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model=None)
        self.assertEqual(calls, ['opencode-go/qwen3.7-plus', 'opencode-go/qwen3.7-plus'])
        self.assertIn('review JSON invalid', prompts[1])
        self.assertEqual((result['status'], result['review']['model']), ('PASS', 'opencode-go/qwen3.7-plus'))

    def test_invalid_outer_review_json_is_reported_as_invalid_not_as_missing_identity(self) -> None:
        """B312(a): when the outer object could not be decoded an inner object (an acceptance item) became a candidate and the reason came out as a misleading 'no identity'."""
        from outcome import parse_review
        identity = revision(self.wt)
        good = {'revision': identity['digest'], 'verdict': 'PASS', 'acceptance': [{'id': 'main', 'status': 'PASS', 'evidence': 'X complete'}]}
        broken = json.dumps(good, ensure_ascii=False).replace('X complete', '&#8220;→", complete')
        result = parse_review(broken, identity, ['main'])
        self.assertEqual(result['status'], 'ERROR')
        self.assertTrue(result['reason'].startswith('review JSON invalid'), result['reason'])

    def test_fenced_review_rejection_reason_is_not_hidden_by_a_brace_in_prose(self) -> None:
        from outcome import parse_review
        identity = revision(self.wt)
        wrong = {'revision': identity['digest'], 'verdict': 'PASS',
                  'acceptance': [{'id': 'acceptance-1', 'status': 'PASS', 'evidence': 'X complete'}]}
        text = '| K3 | `/items/{identifier}` check |\n\n```json\n' + json.dumps(wrong) + '\n```\n'
        result = parse_review(text, identity, ['main'])
        self.assertEqual((result['status'], result['reason']), ('ERROR', 'unknown or duplicate acceptance criterion'))
        correct = text.replace('acceptance-1', 'main')
        self.assertEqual(parse_review(correct, identity, ['main'])['status'], 'PASS')

    def test_review_json_with_invalid_backslash_escapes_is_read_but_identity_and_coverage_stay_strict(self) -> None:
        from outcome import parse_review
        identity = revision(self.wt)
        good = {'revision': identity['digest'], 'verdict': 'PASS', 'acceptance': [{'id': 'main', 'status': 'PASS', 'evidence': 'X complete'}]}
        broken = json.dumps(good, ensure_ascii=False).replace('X complete', "line 7 \\'abc\\' expected; pattern \\d+ and C:\\users")
        with self.assertRaises(ValueError):
            json.loads(broken)
        for planning in (False, True):
            result = parse_review('Review result:\n' + broken, identity, ['main'], planning=planning)
            self.assertEqual(result['status'], 'PASS', result)
            self.assertEqual(result['acceptance'][0]['evidence'], "line 7 'abc' expected; pattern \\d+ and C:\\users")
        wrong = broken.replace(identity['digest'], '0' * 64)
        self.assertNotEqual(parse_review(wrong, identity, ['main'])['status'], 'PASS')
        self.assertNotEqual(parse_review(broken, identity, ['main', 'missing'])['status'], 'PASS')
        conflicting = broken + '\n' + json.dumps({**good, 'verdict': 'REVISE', 'acceptance': [{'id': 'main', 'status': 'FAIL', 'evidence': 'absent'}]})
        self.assertEqual(parse_review(conflicting, identity, ['main'])['status'], 'ERROR')

    def test_review_json_tolerates_prose_wrapping_and_criteria_key_but_not_identity_or_coverage(self) -> None:
        """B39 (live 01:59/02:24): gemini gave the right verdict with a 'criteria' key and inside plain text, and the schema rejected it for a technical reason.
        The packaging and the key name are tolerant; the product identity and scope check stay just as strict."""
        from outcome import parse_review
        identity = revision(self.wt)
        good = {'revision': identity['digest'], 'verdict': 'PASS', 'criteria': [{'id': 'main', 'status': 'PASS', 'evidence': 'file present; lint 0'}]}
        wrapped = '## Review\nOutcome below:\n' + json.dumps(good, ensure_ascii=False) + '\nThanks.'
        result = parse_review(wrapped, identity, ['main'])
        self.assertEqual((result['status'], result['verdict']), ('PASS', 'PASS'))
        self.assertEqual(result['acceptance'][0]['id'], 'main')
        bad_identity = {**good, 'revision': 'deadbeef'}
        self.assertEqual(parse_review(json.dumps(bad_identity), identity, ['main'])['status'], 'ERROR')
        d = identity['digest']
        copy_error = {**good, 'revision': d[:32] + d[40:]}
        self.assertEqual(parse_review(json.dumps(copy_error), identity, ['main'])['status'], 'PASS', 'C01 live: id missing eight middle characters')
        for bad in (d[:15], ('0' if d[0] != '0' else '1') + d[1:], d[:15] + 'g' + d[16:]):
            with self.subTest(bad=bad):
                self.assertEqual(parse_review(json.dumps({**good, 'revision': bad}), identity, ['main'])['status'], 'ERROR')
        missing = {'revision': identity['digest'], 'verdict': 'PASS', 'criteria': []}
        self.assertEqual(parse_review(json.dumps(missing), identity, ['main'])['reason'], 'acceptance coverage incomplete')
        self.assertEqual(parse_review('## Objective\nno json here', identity, ['main'])['status'], 'ERROR')

    def test_review_pass_items_tolerate_null_gap_kind_but_unknown_kinds_stay_rejected(self) -> None:
        from outcome import parse_review
        identity = revision(self.wt)
        items = [{'id': 'main', 'status': 'PASS', 'evidence': 'file present; lint 0', 'missing': None},
                 {'id': 'tests', 'status': 'PASS', 'evidence': 'tests preserved', 'missing': ''}]
        good = {'revision': identity['digest'], 'verdict': 'PASS', 'acceptance': items}
        result = parse_review(json.dumps(good), identity, ['main', 'tests'])
        self.assertEqual((result['status'], result.get('verdict'), result.get('reason')), ('PASS', 'PASS', None), result)
        bad = {**good, 'acceptance': [{**items[0], 'missing': 'uncertain'}, items[1]]}
        self.assertEqual(parse_review(json.dumps(bad), identity, ['main', 'tests'])['reason'], 'invalid gap kind')

    def test_large_untracked_product_preserves_reviewer_priority_in_finalization(self):
        self.successful_qa()
        (self.wt/'new_product.py').write_text('x = 1\n' * 301)
        chain = (['opencode-go/glm-5.2', 'google/gemini-3-flash-preview'], [])
        with patch.object(core.ROUTER, 'chain', return_value=chain), \
             patch.object(core, 'budget_json', return_value={}), \
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
                self.assertEqual(core.is_judge_small(self.wt, head), small)
        path.write_text('x\n' * 299)
        # The product.txt change against the source is two lines: 299 + 2 > 300.
        self.assertFalse(core.is_judge_small(self.wt, self.base))

    def test_unknown_untracked_size_never_enables_small_change_routing(self):
        path = self.wt/'new.dat'
        path.write_bytes(b'\x00binary')
        self.assertFalse(core.is_judge_small(self.wt, self.base))
        path.unlink()
        path.symlink_to(self.wt/'product.txt')
        self.assertFalse(core.is_judge_small(self.wt, self.base))
        path.unlink()
        path.write_text('short\n')
        with patch.object(Path, 'open', side_effect=PermissionError('unreadable')):
            self.assertFalse(core.is_judge_small(self.wt, self.base))
        self.assertFalse(core.is_judge_small(self.wt, 'missing-source-sha'))

    def test_quality_failure_or_timeout_cannot_pass(self) -> None:
        self.successful_qa()
        for error in (subprocess.TimeoutExpired('fake', 1), subprocess.CalledProcessError(2, 'fake')):
            with self.subTest(error=error), patch.object(core, 'capture_opencode', side_effect=error):
                result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
                # B37: an executor error no longer blows up acceptance, the judge chain is exhausted -> verdict FAIL, judge ERROR; still not PASS
                self.assertNotEqual(result['status'], 'PASS')
                self.assertEqual(result['review']['status'], 'ERROR')
                self.assertIn('judge chain exhausted', result['review']['reason'])
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
        with patch.object(core, 'capture_opencode', side_effect=reviewer), patch.object(core, 'qa_gate', wraps=core.qa_gate) as qa, \
             patch.object(core, 'budget_json', return_value={}), \
             patch.object(core.ROUTER, 'chain', return_value=(['opencode-go/repairer', 'opencode-go/reviewer'], [])):
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
             patch.object(core, 'budget_json', return_value={}), \
             patch.object(core.ROUTER, 'chain', return_value=(['opencode-go/a', 'opencode-go/b', 'opencode-go/c'], [])):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli')
        self.assertEqual(calls, ['opencode-go/a'])
        self.assertEqual((result['status'], result['review']['reason']), ('FAIL', 'review-repair-limit'))
        self.assertIsNotNone(core.error_signature(result), 'A cap overrun enters a controlled correction round')

    def test_a_change_exceeding_the_cap_cannot_get_pass_even_if_the_read_only_judge_writes(self):
        """D06: four judges; the first three change the product, the last accepts. It used to be PASS, repairs=3/limit=1, QA=4."""
        self.successful_qa()
        calls = []
        def judge(real, wt, args, timeout, env=None, role='quality'):
            calls.append(args[args.index('-m') + 1])
            if len(calls) <= 3:
                (self.wt/'product.txt').write_text(f'repair {len(calls)}\n')
            return self.review()
        with patch.object(core, 'capture_opencode', side_effect=judge), patch.object(core, 'budget_json', return_value={}), \
             patch.object(core.ROUTER, 'chain', return_value=(['opencode-go/a', 'opencode-go/b', 'opencode-go/c', 'opencode-go/d'], [])), \
             patch.object(core, 'qa_gate', wraps=core.qa_gate) as qa:
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli')
        self.assertNotEqual(result['status'], 'PASS')
        self.assertEqual(calls, ['opencode-go/a'])
        self.assertEqual(qa.call_count, 1, 'A read-only write cannot yield acceptance or inline repair')
        self.assertLessEqual(result['review']['repair_limit'], 1)

    def test_when_the_repair_cap_is_full_the_read_only_final_judge_decides(self):
        """B273 (30.09 rehearsal, child 064142): when the cap was reached the chain was cut without a verdict -> no correction round. D06: once the allowance is used,
        the next link is called TECHNICALLY read-only (CADRE_READ_ONLY -> write permissions closed) and decides."""
        self.successful_qa()
        calls = []
        def judge(real, wt, args, timeout, env=None, role='quality'):
            model = args[args.index('-m') + 1]
            calls.append(model)
            if model == 'opencode-go/a':
                self.assertEqual((env or {}).get('CADRE_READ_ONLY'), '1')
                (self.wt/'product.txt').write_text(f'repair {len(calls)}\n')
                return self.review()
            self.assertIn('READ-ONLY ROUND', args[-1], 'After repair allowance is exhausted, call the next judge read-only')
            self.assertEqual((env or {}).get('CADRE_READ_ONLY'), '1')
            return self.review('REVISE')
        with patch.object(core, 'capture_opencode', side_effect=judge), patch.object(core, 'budget_json', return_value={}), \
             patch.object(core.ROUTER, 'chain', return_value=(['opencode-go/a', 'opencode-go/b', 'opencode-go/c'], [])):
            result = core.finalize_run('a', str(self.wt), self.log, '/fake/cli')
        self.assertEqual(calls, ['opencode-go/a'])
        self.assertEqual((result['status'], result['review']['reason'], result['review']['model']), ('FAIL', 'review-repair-limit', 'opencode-go/a'))
        self.assertIsNotNone(core.error_signature(result), 'A REVISE verdict must allow a correction round')

    def test_read_only_flag_closes_the_opencode_write_permissions(self):
        env = {'CADRE_READ_ONLY': '1'}
        go = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'today': time.strftime('%Y-%m-%d'),
              'go': {'db': 'ok', 'pay': 6.666667, 'window_open': True, 'available': 6.666667, 'unit': 'points'}}
        with patch.object(core, 'budget_json', return_value=go):   # P3: no local, a read-only executor in the cloud
            try:
                core.opencode_budget_env(env, ['-m', 'opencode-go/glm-5.3-flash', 'review'])
            except Exception as exc:   # in a test home without an observation no cloud can be chosen; the permission block must still be written
                self.fail(f'could not prepare config: {exc}')
        permission = json.loads(env['OPENCODE_CONFIG_CONTENT'])['permission']
        self.assertEqual(permission['edit'], 'deny')
        self.assertEqual(permission['bash'], 'allow')
        self.assertEqual(permission['read'], 'deny')
        from session import project_tool_policy
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
        for raw in ('VERDICT: PASS', self.review(ids=()), self.review(identity={'digest':'old'})):
            with self.subTest(raw=raw):
                self.assertEqual(parse_review(raw, current, ['main'])['status'], 'ERROR')
        with patch('outcome.revision', side_effect=RuntimeError('HEAD changed during snapshot')):
            self.assertFalse(approved(self.store.read(self.rid)))

    def test_missing_worker_result_prevents_finalization(self) -> None:
        self.successful_qa()
        (self.store.path(self.rid)/'plan.json').write_text(json.dumps({'tasks':[{'id':'g1','acceptance':'works'}]}))
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
        self.assertIn(f'source {self.base}', instruction)
        self.assertIn('second.txt', instruction, 'The judge sees the whole run scope including the second commit')
        self.assertIn('product.txt', instruction)
        self.assertNotIn('HEAD~1', instruction)
        self.assertNotIn('git diff', instruction, 'No shell in the review session; give no Git instruction')

    def test_reviewer_call_disables_subagent_delegation(self) -> None:
        """B202 (24.09 R2-medium-v2): the judge (big-pickle) handed the review to the cadre-review sub-agent, and the code-writing cadre-impl was open too;
        it was cut at the 15 min limit and independence could be diluted through the sub-agent's model. The judge reviews itself (4c-9: no sub-agents)."""
        self.successful_qa()
        with patch.object(core, 'capture_opencode', return_value=self.review()) as reviewer:
            core.finalize_run('a', str(self.wt), self.log, '/fake/cli', review_model='')
        env_extra = reviewer.call_args.args[4] if len(reviewer.call_args.args) > 4 else reviewer.call_args.kwargs.get('env_extra')
        self.assertEqual((env_extra or {}).get('CADRE_SUB_AGENTLESS'), '1')

    def dispatch_fixture(self, verdict: str, *, tasks=None, worker=None, env=None, plan_extra=None, phase='dispatch') -> int:
        self.successful_qa()
        root = self.home/'dispatcher'
        from qa_fixture import instructions
        instructions(root)
        (root/'lib').mkdir(parents=True)
        (root/'bin').mkdir()
        runner = Path(__file__).with_name('qa_fixture.py')
        shutil.copyfile(runner, root / 'lib/qa_fixture.py')
        qa_path = Path(self.run['constraints']['qa-command'])
        qa_path.write_text(qa_path.read_text().replace(str(runner.resolve()), str(root / 'lib/qa_fixture.py')))
        for name in ('worktree_archive.py', 'run_store.py','outcome.py','session.py','qe_core.py', 'liveness.py', 'acceptance.py', 'runner.py','dispatch_util.py','budget_gate.py', 'go_window.py', 'limits.py', 'limits.json', 'limits.mjs', 'opencode_adaptor.py', 'budget-plugin.mjs', 'cadre-plugin.mjs', 'monthly-budget-plugin.mjs',
                     'model_router.py', 'model_selection.py', 'model_catalog.py', 'contract.py', 'level.py', 'prefilter.py', 'task_split.py', 'standard-task-form.txt'):
            shutil.copyfile(LIB/name, root/'lib'/name)
        for source in LIB.glob('*.py'):
            if not (root / 'lib' / source.name).exists():
                shutil.copyfile(source, root / 'lib' / source.name)
        shutil.copytree(LIB / 'qa-sim', root / 'lib/qa-sim', ignore=shutil.ignore_patterns('__pycache__'), dirs_exist_ok=True)
        shutil.copyfile(LIB.parent/'bin/cadre-dispatch.sh', root/'bin/cadre-dispatch.sh')
        # P3 (03.10): no local -> the judge/repair chain needs the cloud; a fake open Go budget for the dispatcher subprocess (no network)
        import test_end_to_end
        (root/'lib/provider_budget.py').write_text(test_end_to_end.FAKE_BUDGET)
        from test_run_sessions import CATALOG
        (self.home/'models.json').write_text(json.dumps(CATALOG))
        self.enter(patch.dict(os.environ, {'CADRE_MODEL_CATALOG': str(self.home/'models.json')}))
        self.enter(patch.object(core.ROUTER, 'QE', self.home))
        self.enter(patch.object(core, 'budget_json', return_value={'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'today': time.strftime('%Y-%m-%d'),
            'go': {'db': 'ok', 'pay': 33.333334, 'window_open': True, 'available': 33.333334, 'unit': 'points'}}))   # the judge selection is in this process
        (root/'lib/common.sh').write_text('qe_verify_ai_worktree(){ return 0; }\nqe_make_worktree(){ return 99; }\nqe_commit_wip(){ echo 0; }\n')
        (root/'bin/job-runner.sh').write_text('python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" task "$CADRE_RUN_ID" "$CADRE_TASK_ID" produced\n')
        if worker is not None:
            (root/'worker.py').write_text(worker)
            (root/'bin/job-runner.sh').write_text(f'exec "{sys.executable}" "$CADRE_ENGINE_ROOT/worker.py" "$@"\n')
        plan = {'job_id':'fixture','tasks':[{'id':'g1','definition':'edit product','model':'auto','parallel_group':0,
                                            'files':['product.txt'],'acceptance':'product works','difficulty':2,'estimate_min':1}]}
        if tasks is not None:
            plan['tasks'] = tasks
        plan.update(plan_extra or {})
        if phase == 'dispatch':   # 'engine': the plain engine path without a plan (D13)
            (self.store.path(self.rid)/'plan.json').write_text(json.dumps(plan))
        self.store.update(self.rid, phase=phase)
        fake = self.home/'fake-reviewer'
        fake.write_text(f'#!{sys.executable}\n'+'''
import os,sys,json
if sys.argv[-1].startswith('REVIEW PLAN ALIGNMENT WITH THE REQUEST'):
    question=json.loads(sys.argv[-1].splitlines()[-1])
    answer={'revision':question['revision'],'verdict':'PASS','acceptance':[{'id':c['id'],'status':'PASS','evidence':'product.txt and source criteria inspected'} for c in question['criteria']]}
    sid='ses_plan_review_'+os.environ['CADRE_TASK_ID']
    print(json.dumps({'type':'text','sessionID':sid,'part':{'type':'text','text':json.dumps(answer)}}))
    raise SystemExit(0)
if 'SPLIT the task INTO SUB-TASKS' in sys.argv[-1]:   # G-E10 split planner (does not count as a judge call)
    print(json.dumps({'type':'text','sessionID':'ses_divide','part':{'type':'text','text':os.environ.get('FIXTURE_SUB_PLAN','subtask plan absent')}}))
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

    def test_real_dispatcher_and_coordinator_reject_revise(self) -> None:
        # G-C16: REVISE triggers the correction round; the fixture worker does not change the product -> the B193 lock, no acceptance
        self.assertEqual(self.dispatch_fixture('REVISE'), 1)
        record = self.store.read(self.rid)
        self.assertEqual(record['state'], 'failed')
        self.assertEqual([t['signature'][:6] for t in record.get('correction') or []], ['judge:'])
        self.assertEqual(record['final_result']['reason'], 'revision-already-rejected')
        self.assertEqual(record['tasks']['g1']['state'], 'produced')
        self.assertFalse(approved(record))

    def test_a_revision_rejected_by_the_reviewer_is_not_reviewed_again_on_retry(self) -> None:
        """B193 (23.09 R1-medium): a retry after REVISE skipped the lanes and sent the same product back to the judge;
        if the budget changed and another judge was chosen, a rejected product could get accepted. An unchanged product is not reviewed again."""
        # with G-C16 the correction round is also tried in the same run; since the product does not change the first run already becomes a permanent FAIL
        self.assertEqual(self.dispatch_fixture('REVISE'), 1)
        first = self.store.read(self.rid)
        self.assertEqual(first['state'], 'failed')
        self.assertEqual(first.get('rejected_revisions'), [revision(self.wt)['digest']])
        calls = self.home / 'reviewer-calls'
        before = len(calls.read_text())
        with patch.dict(os.environ, {'CADRE_ENGINE_ROOT': str(self.home/'dispatcher'), 'CADRE_REAL_OPENCODE': str(self.home/'fake-reviewer'),
                                     'FIXTURE_VERDICT': 'PASS', 'OPENCODE_DB': str(self.home/'absent.db')}):
            execute_run(self.store, self.rid)   # a manual retry does not produce acceptance with a lax judge either
        record = self.store.read(self.rid)
        self.assertEqual(record['state'], 'failed', 'Retrying the same product is pointless; the failure is permanent')
        self.assertEqual(record['final_result']['reason'], 'revision-already-rejected')
        self.assertEqual(len(calls.read_text()), before, 'Even a lenient judge must not be called')
        self.assertFalse(approved(record))

    def test_real_dispatcher_and_coordinator_accept_matching_qa_and_review(self) -> None:
        self.assertEqual(self.dispatch_fixture('PASS'), 0)
        self.assertEqual(self.store.read(self.rid)['state'], 'completed')
        self.assertTrue(approved(self.store.read(self.rid)))


class NarrowScopeAcceptanceTests(unittest.TestCase):
    """CR-35 (user decision 20.10): a final acceptance with targeted QA passes only with a narrow scope BOUND to the request/approval.
    Just because the approved QA command names an explicit test path, the general allow_targeted is not opened; an out-of-scope change,
    a required test that did not run, test maintenance and a non-independent review are still rejected. Whole-product acceptance is separate."""

    def ident_of(self):
        return {'head': 'h1', 'digest': 'd1'}

    def qa(self, **extra):
        discovery = {'status': 'PASS', 'reason': 'qa-discovery-verified', 'scope': 'targeted',
                     'expected_files': ['tests/unit/test_a.py'], 'observed_files': ['tests/unit/test_a.py'], **extra.pop('discovery', {})}
        return {'status': 'PASS', 'revision': self.ident_of(), 'kind': 'project', 'discovery': discovery, **extra}

    def review(self, status='PASS', independent=True, **extra):
        return {'status': status, 'revision': self.ident_of(), 'independent': independent,
                'verdict': 'PASS' if status == 'PASS' else 'REVISE',
                'acceptance': [{'id': 'main', 'status': 'PASS' if status == 'PASS' else 'FAIL', 'evidence': 'tests/unit/test_a.py inspected'}], **extra}

    def scope(self, **extra):
        return {'files': ['tests/unit/test_a.py'], 'tests': ['tests/unit/test_a.py'], 'changed': ['tests/unit/test_a.py'], **extra}

    def test_targeted_qa_without_a_bound_scope_stays_full_qa_required(self):
        from outcome import decision
        result = decision(self.qa(), self.review(), self.ident_of())
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'full-qa-required'))
        self.assertEqual(decision(self.qa(), self.review(), self.ident_of(), scope=None)['reason'], 'full-qa-required')

    def test_bound_scope_accepts_targeted_qa_when_files_tests_and_independent_review_hold(self):
        from outcome import decision
        result = decision(self.qa(), self.review(), self.ident_of(), scope=self.scope())
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['scope']['files'], ['tests/unit/test_a.py'])
        self.assertEqual(result['scope']['tests'], ['tests/unit/test_a.py'])

    def test_bound_scope_rejects_a_change_outside_the_permitted_files(self):
        from outcome import decision
        result = decision(self.qa(), self.review(), self.ident_of(), scope=self.scope(changed=['src/adapter.py', 'tests/unit/test_a.py']))
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'scope-files-exceeded'))
        self.assertEqual(result['out_of_scope'], ['src/adapter.py'])

    def test_bound_scope_rejects_when_a_required_test_file_did_not_run(self):
        from outcome import decision
        result = decision(self.qa(), self.review(), self.ident_of(), scope=self.scope(tests=['tests/unit/test_a.py', 'tests/unit/test_b.py']))
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'scope-tests-not-run'))
        self.assertEqual(result['missing_tests'], ['tests/unit/test_b.py'])
        unverified = self.qa(discovery={'status': 'UNKNOWN', 'reason': 'qa-discovery-unverified', 'observed_files': []})
        self.assertEqual(decision(unverified, self.review(), self.ident_of(), scope=self.scope())['reason'], 'scope-tests-not-run')

    def test_bound_scope_keeps_every_other_rejection(self):
        from outcome import decision
        cases = [(self.qa(), self.review('FAIL'), 'review-not-passed'),
                 (self.qa(), self.review(independent=False), 'reviewer-not-independent'),
                 (self.qa(status='FAIL'), self.review(), 'qa-not-passed'),
                 (self.qa(test_maintenance={'status': 'PASS', 'paths': ['tests/unit/test_a.py']}), self.review(), 'test-maintenance-review-required'),
                 (self.qa(revision={'head': 'h0', 'digest': 'd0'}), self.review(), 'revision-mismatch')]
        for qa, review, reason in cases:
            with self.subTest(reason=reason):
                result = decision(qa, review, self.ident_of(), scope=self.scope())
                self.assertEqual((result['status'], result['reason']), ('FAIL', reason))

    def test_bound_scope_limits_full_qa_runs_too(self):
        from outcome import decision
        full = self.qa(discovery={'scope': 'full', 'observed_files': ['tests/unit/test_a.py', 'tests/unit/test_b.py']})
        self.assertEqual(decision(full, self.review(), self.ident_of(), scope=self.scope(changed=['x.py']))['reason'], 'scope-files-exceeded')
        self.assertEqual(decision(full, self.review(), self.ident_of(), scope=self.scope())['status'], 'PASS')

    def test_the_old_pilot_without_a_bound_scope_remains_rejected(self):
        """request-4cc629 (20.10 night): the dispatcher ran `make test` outside the sandbox and installed `.venv` in the worktree, the judge gave REVISE,
        the request had no bound scope. With the record's counters/permissions unchanged it stays FAIL; the new rule does not produce acceptance retroactively."""
        from outcome import decision
        ident_of = {'head': 'dde96b28a8b4b4b3aaf6ace622526d30386aada7',
                  'digest': 'ad81f7d6860298c7e48b999e049b6a7bd85ef2f2700e549d4cc464b79fa0621e'}
        qa = {'status': 'PASS', 'reason': 'command-result', 'kind': 'project', 'revision': ident_of,
              'discovery': {'status': 'PASS', 'reason': 'qa-discovery-verified', 'scope': 'targeted',
                            'expected_files': ['tests/unit/test_open_api_adapters.py'], 'observed_files': ['tests/unit/test_open_api_adapters.py']}}
        review = {'status': 'FAIL', 'verdict': 'REVISE', 'revision': ident_of, 'model': 'openrouter/openai/gpt-6.1-sol', 'independent': True,
                  'acceptance': [{'id': 'request_output', 'status': 'PASS', 'evidence': '1288 parse_page actual adapter'},
                                 {'id': 'request_acceptance', 'status': 'FAIL', 'evidence': 'Full diff, untracked files and forbidden action evidence were not provided'}]}
        self.assertEqual(decision(qa, review, ident_of)['reason'], 'full-qa-required')
        passes = {**review, 'status': 'PASS', 'verdict': 'PASS'}
        self.assertEqual(decision(qa, passes, ident_of)['reason'], 'full-qa-required',
                         'Without a bound scope, targeted QA is not final acceptance even if the judge passes')


SANDBOX_PROFILE = ('(version 1)\n(allow default)\n(deny network*)\n'
                  '(deny process-exec (regex "/([pP][iI][pP][0-9.]*|[pP][iI][pP][xX]|[uU][vV]|[bB][rR][eE][wW])$"))\n')


def full_process_records(store, rid, wt, base, *, task='impl', attempt='fixture', carried=True):
    """User decision 21.10: process acceptance rests on defined engine checks. The required records the real dispatcher leaves:
    operator policy; per attempt the sandbox profile + load receipt + process close + action record; worktree-ready;
    a clean dispatch.log; if requested, the carried source snapshot (same content)."""
    import hashlib
    import permission
    from run_store import atomic_json
    permission.policy(store, rid)
    folder = store.task_dir(rid, task) / 'attempts' / attempt
    folder.mkdir(parents=True, exist_ok=True)
    if not (folder / 'attempt.json').exists():
        (folder / 'attempt.json').write_text(json.dumps({'role': 'impl', 'model': 'fixture/writer', 'pid': 123}))
    (folder / 'tool-policy.json').write_text(json.dumps({'version': 1, 'writable': True, 'profile': SANDBOX_PROFILE, 'write_paths': [str(Path(wt) / 'product.txt')]}))
    (folder / 'tools-armed.json').write_text(json.dumps({'pid': 123, 'profile_sha256': hashlib.sha256(SANDBOX_PROFILE.encode()).hexdigest()}))
    (folder / 'process.json').write_text(json.dumps({'process_identity': {'pid': 123}, 'stop_observed': {'pid': 123}}))
    (folder / 'stdout.jsonl').write_text(json.dumps({'type': 'tool_use', 'part': {'tool': 'bash', 'state': {'input': {'command': f"exec '{folder}/tool' 'ls tests'"}}}}) + '\n')
    run_dir = store.path(rid)
    (run_dir / 'worktree-ready').write_text(str(wt))
    (run_dir / 'dispatch.log').write_text('2026-10-21 14:00:00 LIGHT MERGE: 1 task\n2026-10-21 14:00:05 QA GATE: PASS rc=0\n')
    if carried:
        record = store.read(rid)
        record['source'] = {'sha': base, 'branch': 'cr35', 'carried': True, 'dirty_files': 0}
        atomic_json(run_dir / 'run.json', record)


class RunnerEvidenceTests(unittest.TestCase):
    """The engine evidence given to the judge is derived from real permission/process/ledger records; there is no fixed "no forbidden action was taken / network closed"
    statement, anything without a record is written as unknown; receipted / pending (estimated) / trusted open upper bound are separated.
    21.10 live request-9c7a9d76: the install ban was counted 0/3 in a case-folded regex; the "product same -> no writes" inference was removed."""

    PROFILE = SANDBOX_PROFILE

    def setUp(self) -> None:
        self.f = CompletionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.store, self.rid, self.wt = self.f.store, self.f.rid, self.f.wt
        shutil.rmtree(self.store.task_dir(self.rid, 'impl'))   # the writer fixture of CompletionTests: these tests set up their own records

    def record(self, **extra):
        record = self.store.read(self.rid)
        return {**record, 'worktree': str(self.wt), 'request': {'duration_minutes': 40, 'budget_usd': '4.5'},
                'request_clock': {'active_sec': 1852.4}, 'budget_spent_usd': 0.2, 'budget_held_usd': 0.01, 'budget_cap_usd': 4.5,
                'budget_unsettled': [{'provider': 'openrouter', 'role': 'build', 'model': 'm', 'reserved_usd': 0.01, 'created': 1.0}], **extra}

    def attempt(self, task, attempt, *, policy=True, stop=True, role='impl', armed=True, commands=(), tools=()):
        import hashlib
        folder = self.store.task_dir(self.rid, task) / 'attempts' / attempt
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'attempt.json').write_text(json.dumps({'role': role, 'model': 'fixture/m', 'exit_code': 0, 'outcome': 'finished', 'pid': 1}))
        if policy:
            (folder / 'tool').write_text("#!/bin/sh\nexec /usr/bin/sandbox-exec -p '...' \"$@\"\n")
            (folder / 'tool-policy.json').write_text(json.dumps({'version': 1, 'writable': role != 'quality', 'profile': self.PROFILE,
                                                                 'tool_sha256': hashlib.sha256((folder / 'tool').read_bytes()).hexdigest(),
                                                                 'write_paths': [str(self.wt / 'product.txt')]}))
            if armed:
                (folder / 'tools-armed.json').write_text(json.dumps({'pid': 1, 'profile_sha256': hashlib.sha256(self.PROFILE.encode()).hexdigest()}))
        if stop:
            (folder / 'process.json').write_text(json.dumps({'process_identity': {'pid': 1}, 'stop_observed': {'pid': 1}, 'scope_stopped': 'x'}))
        lines = [json.dumps({'type': 'tool_use', 'part': {'tool': 'bash', 'state': {'input': {'command': f"exec '{folder}/tool' {json.dumps(c)}"}}}}) for c in commands]
        lines += [json.dumps({'type': 'tool_use', 'part': {'tool': t, 'state': {'input': {'filePath': str(self.wt / 'product.txt')}}}}) for t in tools]
        if commands or tools:
            (folder / 'stdout.jsonl').write_text('\n'.join(lines) + '\n')
        return folder

    def evidence(self, record=None, changed=('product.txt',)):
        from acceptance import runner_evidence
        return runner_evidence(record or self.record(), list(changed), {'head': 'h1', 'digest': 'd1'}, self.f.base, [], wt=str(self.wt))

    def test_claims_come_from_permission_and_process_records_not_fixed_text(self):
        empty = self.evidence()
        for constant in ('The permission record forbids commit, push, merge, install and publish', "The sandbox has no network", 'No forbidden action occurred', 'again production/write absent'):
            self.assertNotIn(constant, empty)
        self.assertIn('process record absent', empty)
        self.assertIn('unknown', empty)
        import permission
        permission.policy(self.store, self.rid)
        self.attempt('g1', 'a1')
        self.attempt('qa', 'c1', role='qa')
        text = self.evidence()
        self.assertIn('permission record: operator-policy.json', text)
        self.assertIn('recorded operator actions: absent', text)
        self.assertIn('process records: 2 attempts', text)
        self.assertIn('sandbox profile recorded 2/2 (network denial 2/2, installer execution denial 2/2)', text)
        self.assertIn('profile load receipt (matching sha) 2/2', text)
        self.assertIn('process closure observed 2/2', text)
        self.assertIn('attempts without a sandbox record: 0', text)
        self.assertIn('do not prove every denial was enforced on every call', text)
        self.assertIn('install directory trace: absent', text)
        self.assertIn('not verified by the engine', text)
        self.assertIn('receipted (settled) 0.2 USD', text)
        self.assertIn('pending reserve (estimated) 0.01 USD', text)
        self.assertIn('reliable open parent', text)
        self.assertIn('cap 4.5 USD', text)
        self.assertIn('1852 s / 2400 s', text)

    def test_missing_sandbox_record_unarmed_profile_and_install_trace_are_reported(self):
        self.attempt('g1', 'a1')
        self.attempt('g1', 'a2', policy=False, stop=False)
        self.attempt('g1', 'a3', armed=False)
        (self.wt / '.venv').mkdir()
        (self.wt / '.venv/pyvenv.cfg').write_text('home = /usr/bin\n')
        (self.wt / '.gitignore').write_text('.venv/\n')
        text = self.evidence()
        self.assertIn('sandbox profile recorded 2/3', text)
        self.assertIn('profile load receipt (matching sha) 1/3', text)
        self.assertIn('attempts without a sandbox record: 1', text)
        self.assertIn('process closure observed 2/3', text)
        self.assertIn('unknown', text)
        self.assertIn('install directory trace: .venv', text)
        self.assertIn('recorded operator actions: absent', text)

    def test_bound_request_scope_and_engine_check_are_shown_to_the_reviewer(self):
        record = self.record(request={'duration_minutes': 40, 'budget_usd': '4.5', 'scope_files': ['product.txt'], 'scope_tests': ['test_x.py']},
                           request_input_hash='r1',
                           request_permission={'status': 'approved', 'action': {'request_hash': 'r1', 'acceptance_scope': {'files': ['product.txt'], 'tests': ['test_x.py']}}})
        text = self.evidence(record, ['product.txt', 'extra.py'])
        self.assertIn('request scope (approved): allowed files product.txt; required tests test_x.py', text)
        self.assertIn('changed outside scope: extra.py', text)
        self.assertIn('bound scope: absent', self.evidence())

    def carried_image(self):
        run_dir = self.store.path(self.rid)
        source = run_dir / 'source'
        source.mkdir(exist_ok=True)
        (self.wt / 'product.txt').write_text('carried\n')
        patch_bytes = subprocess.run(['git', '-C', str(self.wt), 'diff', '--binary', '--no-ext-diff', 'HEAD', '--'], check=True, capture_output=True).stdout
        (source / 'change.patch').write_bytes(patch_bytes)
        (run_dir / 'worktree-ready').write_text(str(self.wt))
        old = time.time() - 3600
        os.utime(self.wt / 'product.txt', (old, old))
        os.utime(run_dir / 'worktree-ready', (old + 1, old + 1))
        return self.record(source={'sha': self.f.git('rev-parse', 'HEAD').strip(), 'branch': 'cr35', 'carried': True, 'dirty_files': 1,
                                  'patch': str(source / 'change.patch')})

    def test_extending_the_carried_file_keeps_the_patch_lines_and_is_reported_as_preserved(self):
        """New code work rightly extends a carried (accepted) file: the content deviates from the snapshot but the patch's lines
        stay in the product. Preservation is measured at line level; timestamps/write tools are product work, not a deviation."""
        from acceptance import evidence_package
        record = self.carried_image()
        self.attempt('g1', 'a1', commands=('ls tests',), tools=('edit',))
        (self.wt / 'product.txt').write_text('carried\nnew feature line\n')
        package = evidence_package(record, ['product.txt'], {'head': 'h1', 'digest': 'd1'}, self.f.base, [], wt=str(self.wt))
        text = package['text']
        self.assertIn('final content differs from the carried snapshot', text)
        self.assertIn('carried patch lines: preserved — product.txt: 1/1 added lines present, 1/1 deleted lines absent', text)
        self.assertIn('patch preservation: PRESERVED', text)
        self.assertIn('file also changed', text)
        self.assertEqual(package['check']['patch'], 'PRESERVED')
        self.assertEqual(package['core']['version'], 3)
        self.assertEqual(package['core']['write']['patch'],
                         {'preserved': True, 'broken': False, 'unknown': False,
                          'files': {'product.txt': {'added': 1, 'lost': 0, 'deleted': 1, 'go_back': 0, 'trivial': 0}}})

    def test_removing_or_reverting_a_patch_line_in_the_carried_file_is_a_deviation(self):
        """If a line the patch added is deleted or a line it removed comes back, DETECTED; even if the file is reverted to HEAD and drops out of
        `changed`, the breakage is visible and the evidence core differs from the preserved state."""
        from acceptance import evidence_package
        record = self.carried_image()
        self.attempt('g1', 'a1', tools=('edit',))
        (self.wt / 'product.txt').write_text('carried\nfresh\n')
        protected = evidence_package(record, ['product.txt'], {'head': 'h1', 'digest': 'd1'}, self.f.base, [], wt=str(self.wt))
        (self.wt / 'product.txt').write_text('after\nfresh\n')
        package = evidence_package(record, ['product.txt'], {'head': 'h1', 'digest': 'd1'}, self.f.base, [], wt=str(self.wt))
        self.assertEqual(package['check']['patch'], 'DETECTED')
        self.assertIn('carried patch lines: BROKEN — product.txt: 1/1 added lines missing from the product, 1 deleted lines restored', package['text'])
        self.assertIn('patch preservation: DETECTED', package['text'])
        self.assertEqual(package['core']['write']['patch']['files']['product.txt'], {'added': 1, 'lost': 1, 'deleted': 1, 'go_back': 1, 'trivial': 0})
        self.assertNotEqual(package['sha256'], protected['sha256'])
        (self.wt / 'product.txt').write_text('after\n')   # reverted to HEAD: changed is empty
        go_back = evidence_package(record, [], {'head': 'h1', 'digest': 'd1'}, self.f.base, [], wt=str(self.wt))
        self.assertEqual(go_back['check']['patch'], 'DETECTED')
        self.assertEqual(go_back['core']['write']['patch']['files']['product.txt']['lost'], 1)

    def test_binary_or_unreadable_patch_leaves_patch_preservation_unknown(self):
        from acceptance import evidence_package
        record = self.carried_image()
        self.attempt('g1', 'a1', tools=('edit',))
        (self.wt / 'product.txt').write_text('carried\nfresh\n')
        pathlib.Path(record['source']['patch']).write_bytes(b'diff --git a/product.txt b/product.txt\nindex 1..2 100644\nGIT binary patch\nliteral 0\n')
        package = evidence_package(record, ['product.txt'], {'head': 'h1', 'digest': 'd1'}, self.f.base, [], wt=str(self.wt))
        self.assertEqual(package['check']['patch'], 'UNKNOWN')
        self.assertIn('carried patch lines: could not be compared', package['text'])
        pathlib.Path(record['source']['patch']).unlink()
        package = evidence_package(record, ['product.txt'], {'head': 'h1', 'digest': 'd1'}, self.f.base, [], wt=str(self.wt))
        self.assertEqual(package['check']['patch'], 'UNKNOWN')
        self.assertIsNone(package['core']['write']['patch'])

    def test_write_evidence_comes_from_timestamps_and_recorded_actions_never_from_content_equality(self):
        """Content parity only proves that the final content was preserved; "not written" is said only as far as the records support it."""
        record = self.carried_image()
        self.attempt('g1', 'a1', commands=('ls tests', 'git diff product.txt', 'cat product.txt'))
        (self.store.path(self.rid) / 'dispatch.log').write_text('2026-10-21 00:32:10 LIGHT MERGE: 1 task\n')
        text = self.evidence(record)
        self.assertIn('final content matches the carried snapshot (content preserved; not proof of no writes)', text)
        self.assertIn('timestamp: unchanged since worktree setup (no write trace; timestamp is not an absolute guarantee)', text)
        self.assertIn('write-tool calls (edit/write/patch): 0', text)
        self.assertIn('shell commands: 3, write patterns: 0', text)
        self.assertIn('shell writes may fall outside these patterns -> absence of writes is unproven', text)
        self.assertIn('dispatcher log: no install/package/test command traces found (1 lines); unlogged commands unknown', text)
        self.assertNotIn('again production/write absent', text)
        (self.wt / 'product.txt').write_text('carried\n')   # the same content rewritten
        text = self.evidence(record)
        self.assertIn('final content matches the carried snapshot', text)
        self.assertIn('timestamp: file timestamp changed AFTER worktree setup: product.txt (rewrite trace, even if content is identical)', text)
        self.assertNotIn('write izi absent', text)
        (self.wt / 'product.txt').write_text('changed\n')
        (self.wt / 'product.txt').write_text('carried\n')   # change and revert
        text = self.evidence(record)
        self.assertIn('changed AFTER', text)
        self.attempt('correction-1', 'b1', commands=("sed -i '' 's/a/b/' product.txt", 'cat product.txt'), tools=('edit',))
        (self.store.path(self.rid) / 'dispatch.log').write_text('2026-10-20 21:05 uv run --locked pytest tests\n')
        text = self.evidence(record)
        self.assertIn('write-tool calls (edit/write/patch): 1', text)
        self.assertIn('shell commands: 5, write patterns: 1 (sed -i', text)
        self.assertIn('dispatcher log: install/package/test command traces found by the scan: uv run', text)
        (self.wt / 'product.txt').write_text('deviation\n')
        self.assertIn('final content differs from the carried snapshot', self.evidence(record))

    def package(self, record=None, changed=('product.txt',)):
        from acceptance import evidence_package
        return evidence_package(record or self.record(), list(changed), {'head': 'h1', 'digest': 'd1'}, self.f.base, [], wt=str(self.wt))

    def test_evidence_core_ignores_log_volume_cost_and_reviewer_records_but_tracks_real_traces(self):
        """The core is derived from structured evidence facts: log line counts, cost/time and the judge's own session are out;
        a new install/package/test trace, a new write action and a new process record are in."""
        from acceptance import evidence_core_sha
        self.attempt('g1', 'a1', commands=('ls tests', 'cat product.txt'))
        base = self.package()
        self.assertEqual(base['sha256'], evidence_core_sha(base['core']))
        self.assertIn('ENGINE EVIDENCE', base['text'])
        self.assertEqual(base['text'], self.evidence())
        daily = self.store.path(self.rid) / 'dispatch.log'
        daily.write_text('2026-10-21 00:32:10 LIGHT MERGE: 1 task\n')
        self.assertEqual(self.package()['sha256'], base['sha256'], 'An ordinary log does not count as evidence')
        with open(daily, 'a') as f:
            f.write(''.join(f'2026-10-21 00:4{i}:00 Info: status queried\n' for i in range(5)))
        self.assertEqual(self.package()['sha256'], base['sha256'], 'Line count is not evidence')
        self.assertEqual(self.package(self.record(budget_spent_usd=1.5, request_clock={'active_sec': 9.0}))['sha256'], base['sha256'], 'Expense and duration are not evidence')
        self.attempt('quality', 'h1', role='quality', commands=('ls', 'cat product.txt', 'git status'), tools=('read',))
        self.assertEqual(self.package()['sha256'], base['sha256'], 'The judge session is not evidence')
        self.assertIn('shell commands: 2,', self.package()['text'])
        with open(daily, 'a') as f:
            f.write('2026-10-20 21:05 uv run --locked pytest tests\n')
        traced = self.package()
        self.assertNotEqual(traced['sha256'], base['sha256'], 'An actual install, package or test trace is new evidence')
        self.assertEqual(traced['core']['dispatcher'], ['uv run'])
        daily.write_text('2026-10-21 00:32:10 LIGHT MERGE: 1 task\n')
        self.assertEqual(self.package()['sha256'], base['sha256'])
        self.attempt('g1', 'a2', commands=('cat product.txt',), tools=('edit',))
        write = self.package()
        self.assertNotEqual(write['sha256'], base['sha256'], 'A new write action record is new evidence')
        self.assertEqual(write['core']['write']['action']['tool'], 1)
        self.assertEqual(write['core']['process']['n'], 2)
        self.assertNotIn('quality', write['core']['process']['roles'])

    def test_active_time_line_includes_the_open_session_and_stays_out_of_the_core(self):
        """21.10 live request-1e0782bb: the judge saw "0 s / 2700 s excluding review" (an open session's duration had not closed in the record) and
        counted the time criterion as unevidenced. The line also counts the open session; time does not enter the core."""
        now_ts = time.time()
        closed = self.record(request_clock={'start': now_ts - 1000, 'active_sec': 100.0, 'session': None, 'session_pid': None})
        open = self.record(request_clock={'start': now_ts - 1000, 'active_sec': 100.0, 'session': now_ts - 500, 'session_pid': 1})
        self.assertIn('duration: recorded active 100 s / 2400 s', self.evidence(closed))
        text = self.evidence(open)
        self.assertRegex(text, r'duration: recorded active (59[5-9]|60[0-5]) s / 2400 s \(including the open session')
        self.assertEqual(self.package(closed)['sha256'], self.package(open)['sha256'], 'Duration is excluded from the evidence core')

    def test_missing_action_records_and_snapshot_keep_write_evidence_unknown(self):
        self.attempt('g1', 'a1')   # no stdout.jsonl
        text = self.evidence()
        self.assertIn('write trace: no carried snapshot (clean source at start)', text)
        self.assertIn('action record: absent -> write status unknown', text)
        self.assertIn('no worktree setup record -> timestamp could not be compared', text)
        self.assertIn('dispatcher log: absent -> shell steps outside the sandbox unknown', text)
        self.assertNotIn('write izi absent', text)


class EvidenceMissingTests(unittest.TestCase):
    """21.10 live request-9c7a9d76: the judge found the product right and gave REVISE only for missing process evidence; the engine sent this to a code
    repair (0.017 USD wasted) and wrote the product digest as rejected. Missing evidence (missing: evidence) opens no code repair;
    a blind repeat with an unchanged product + unchanged evidence is blocked; really changed evidence may be re-reviewed once in the same run;
    old records/FAILs/costs do not change."""

    def setUp(self) -> None:
        self.f = CompletionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.store, self.rid, self.wt = self.f.store, self.f.rid, self.f.wt
        self.f.successful_qa()
        self.call = 0

    def judge(self, missing_ids=(), fail_ids=(), verdict='REVISE'):
        def answer(real, wt, args, timeout, env=None, role='quality'):
            prompt = args[-1]
            if '{"revision"' not in prompt:
                return self.f.review('PASS')
            self.call += 1
            data = json.JSONDecoder().raw_decode(prompt[prompt.rindex('{"revision"'):])[0]
            items = []
            for c in data['criteria']:
                item = {'id': c['id'], 'status': 'FAIL' if c['id'] in fail_ids or c['id'] in missing_ids else 'PASS',
                        'evidence': 'product.txt inspected; process evidence ' + ('missing' if c['id'] in missing_ids else 'full')}
                if c['id'] in missing_ids:
                    item['missing'] = 'evidence'
                items.append(item)
            return review_output(json.dumps({'revision': revision(self.wt)['digest'], 'verdict': verdict, 'acceptance': items}))
        return answer

    def finalize(self, answer):
        with patch.object(core, 'capture_opencode', side_effect=answer):
            return core.finalize_run('a', str(self.wt), self.f.log, '/fake/cli', review_model='')

    def test_review_schema_accepts_only_known_gap_values(self):
        id = revision(self.wt)
        ok = parse_review(json.dumps({'revision': id['digest'], 'verdict': 'REVISE',
                                      'acceptance': [{'id': 'main', 'status': 'FAIL', 'evidence': 'process evidence missing', 'missing': 'evidence'}]}), id, ['main'])
        self.assertEqual((ok['status'], ok['acceptance'][0]['missing']), ('FAIL', 'evidence'))
        bad = parse_review(json.dumps({'revision': id['digest'], 'verdict': 'REVISE',
                                        'acceptance': [{'id': 'main', 'status': 'FAIL', 'evidence': 'x', 'missing': 'other'}]}), id, ['main'])
        self.assertEqual(bad['status'], 'ERROR')

    def test_evidence_gap_does_not_open_code_repair_and_blocks_blind_repeat_until_evidence_changes(self):
        from acceptance import correction_decision, error_signature
        result = self.finalize(self.judge(missing_ids=('main',)))
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'review-evidence-gap'), result)
        self.assertEqual(self.call, 1)
        self.assertIsNone(error_signature(result), 'Missing evidence is not a repairable code defect')
        record = self.store.read(self.rid)
        self.assertEqual(correction_decision(record)[0], 'STOP')
        self.assertFalse(record.get('rejected_revisions'), 'The product was not rejected; evidence is missing')
        red = record['rejected_evidence']
        self.assertEqual(red[-1]['digest'], revision(self.wt)['digest'])
        self.assertEqual(red[-1]['evidence'], result['evidence_sha256'])
        self.assertFalse(approved(record))
        previous = {k: record.get(k) for k in ('budget_spent_usd', 'budget_held_usd', 'request_clock', 'final_result')}
        repeat = self.finalize(self.judge(verdict='PASS'))
        self.assertEqual((repeat['status'], repeat['reason']), ('FAIL', 'evidence-already-rejected'), repeat)
        self.assertEqual(self.call, 1, 'Do not call the judge for an unchanged product and unchanged evidence')
        then = self.store.read(self.rid)
        self.assertEqual({k: then.get(k) for k in ('budget_spent_usd', 'budget_held_usd', 'request_clock')},
                         {k: previous[k] for k in ('budget_spent_usd', 'budget_held_usd', 'request_clock')})
        self.assertEqual(then['rejected_evidence'], red, 'Do not delete the earlier rejection record')
        folder = self.store.task_dir(self.rid, 'impl') / 'attempts/fixture'
        (folder / 'tool-policy.json').write_text(json.dumps({'version': 1, 'writable': True, 'profile': '(version 1)\n(deny network*)\n', 'write_paths': []}))
        (folder / 'process.json').write_text(json.dumps({'stop_observed': {'pid': 123}}))
        passes = self.finalize(self.judge(verdict='PASS'))
        self.assertEqual(passes['status'], 'PASS', passes)
        self.assertEqual(self.call, 2, 'Actually changed evidence is reviewed once more')
        self.assertNotEqual(passes['evidence_sha256'], red[-1]['evidence'])
        self.assertTrue(approved(self.store.read(self.rid)))

    def daily(self, *lines, add=False):
        path = self.store.path(self.rid) / 'dispatch.log'
        with open(path, 'a' if add else 'w') as f:
            f.write(''.join(l + '\n' for l in lines))

    def session_record(self, task, attempt, role, commands=(), tools=()):
        """Records of an executor/judge session: attempt.json, the (non-writable) policy, the close and tool events."""
        folder = self.store.task_dir(self.rid, task) / 'attempts' / attempt
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'attempt.json').write_text(json.dumps({'role': role, 'model': 'fixture/m', 'exit_code': 0, 'outcome': 'finished', 'pid': 7}))
        (folder / 'tool-policy.json').write_text(json.dumps({'version': 1, 'writable': role != 'quality', 'profile': '(version 1)\n(deny network*)\n', 'write_paths': []}))
        (folder / 'process.json').write_text(json.dumps({'process_identity': {'pid': 7}, 'stop_observed': {'pid': 7}}))
        lines = [json.dumps({'type': 'tool_use', 'part': {'tool': 'bash', 'state': {'input': {'command': f"exec '{folder}/tool' {json.dumps(c)}"}}}}) for c in commands]
        lines += [json.dumps({'type': 'tool_use', 'part': {'tool': t, 'state': {'input': {'filePath': str(self.wt / 'product.txt')}}}}) for t in tools]
        (folder / 'stdout.jsonl').write_text('\n'.join(lines) + '\n')
        return folder

    def test_ordinary_dispatcher_log_lines_and_reviewer_session_records_are_not_new_evidence(self):
        """Codex reproduction: after the first evidence rejection, adding just an info line to dispatch.log opened a second judge
        (the core carried the log line count and the judge's own session records). Ordinary logs, line counts and
        judge records are not meaningful new evidence; since it is missing evidence, not a product rejection, no code repair opens either."""
        from acceptance import correction_decision
        self.daily('2026-10-21 00:32:10 LIGHT MERGE: 1 task', '2026-10-21 00:33:15 starting the run (opencode run)')
        result = self.finalize(self.judge(missing_ids=('main',)))
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'review-evidence-gap'), result)
        self.assertEqual(self.call, 1)
        red = list(self.store.read(self.rid)['rejected_evidence'])
        self.daily('2026-10-21 00:40:00 Info: status queried', '2026-10-21 00:40:05 ACCEPTANCE RESULT: FAIL', add=True)
        self.session_record('quality', 'judge1', 'quality', commands=('ls tests', 'cat product.txt', 'git diff product.txt'), tools=('read',))
        self.store.update(self.rid, budget_spent_usd=0.9, request_clock={'active_sec': 1200.0})
        repeat = self.finalize(self.judge(verdict='PASS'))
        self.assertEqual((repeat['status'], repeat['reason']), ('FAIL', 'evidence-already-rejected'), repeat)
        self.assertEqual(self.call, 1, 'An ordinary log line, judge records and spending changes do not reopen the judge')
        self.assertEqual(repeat['evidence_sha256'], red[-1]['evidence'], 'The core is unchanged')
        then = self.store.read(self.rid)
        self.assertEqual(then['rejected_evidence'], red)
        self.assertFalse(then.get('rejected_revisions'), 'The correct product was not rejected')
        self.assertEqual(correction_decision(then)[0], 'STOP', 'Missing evidence does not trigger code repair')
        self.assertFalse(approved(then))

    def test_new_recorded_action_evidence_reopens_exactly_one_review(self):
        """A really new action record (tool events of a new executor session) changes the core; the same product is reviewed once more."""
        self.daily('2026-10-21 00:32:10 LIGHT MERGE: 1 task')
        result = self.finalize(self.judge(missing_ids=('main',)))
        self.assertEqual(result['reason'], 'review-evidence-gap', result)
        red = list(self.store.read(self.rid)['rejected_evidence'])
        self.session_record('g1', 'writer2', 'impl', commands=('cat product.txt',), tools=('edit',))
        passes = self.finalize(self.judge(verdict='PASS'))
        self.assertEqual(passes['status'], 'PASS', passes)
        self.assertEqual(self.call, 2)
        self.assertNotEqual(passes['evidence_sha256'], red[-1]['evidence'])
        self.assertTrue(approved(self.store.read(self.rid)))

    def test_product_rejection_still_opens_repair_and_legacy_rejections_stay_permanent(self):
        from acceptance import error_signature
        result = self.finalize(self.judge(fail_ids=('main',)))
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'review-not-passed'))
        self.assertEqual(error_signature(result), 'judge:main')
        record = self.store.read(self.rid)
        self.assertEqual(record['rejected_revisions'], [revision(self.wt)['digest']])
        self.assertFalse(record.get('rejected_evidence'))
        (self.store.task_dir(self.rid, 'impl') / 'attempts/fixture/process.json').write_text(json.dumps({'stop_observed': {'pid': 123}}))
        repeat = self.finalize(self.judge(verdict='PASS'))
        self.assertEqual((repeat['status'], repeat['reason']), ('FAIL', 'revision-already-rejected'), 'A product rejection stays permanent even if evidence changes')
        self.assertEqual(self.call, 1)


class ProcessCheckTests(unittest.TestCase):
    """User decision 21.10: process acceptance rests on defined and verifiable engine checks; no absolute "it happened
    nowhere" guarantee is sought. In a typed request: enough records -> COMPLETE (acceptable); a missing/broken required record -> MISSING
    (even if the judge mistakenly says PASS the engine does not accept, no repair); a real violation -> FAIL before the paid judge; a wrong product -> product
    rejection and repair (being COMPLETE does not get a wrong product accepted)."""

    def setUp(self) -> None:
        self.f = CompletionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.store, self.rid, self.wt = self.f.store, self.f.rid, self.f.wt
        self.f.successful_qa()
        self.typed_request()
        self.call, self.prompts = 0, []

    def typed_request(self):
        from run_store import atomic_json
        record = self.store.read(self.rid)
        record['request'] = {'job_type': 'code', 'duration_minutes': 40, 'budget_usd': '4.5', 'output': 'product.txt contains after',
                            'acceptance': 'product.txt contains after; no installs, external network or writes to original sources', 'input_mode': 'goal',
                            'target_kind': 'existing'}
        record['request_input_hash'] = 'r1'
        atomic_json(self.store.path(self.rid) / 'run.json', record)

    def full_records(self):
        full_process_records(self.store, self.rid, self.wt, self.f.base)

    def judge(self, missing=None, verdict='PASS'):
        def answer(real, wt, args, timeout, env=None, role='quality'):
            prompt = args[-1]
            if '{"revision"' not in prompt:
                return self.f.review('PASS')
            self.call += 1
            self.prompts.append(prompt)
            data = json.JSONDecoder().raw_decode(prompt[prompt.rindex('{"revision"'):])[0]
            items = []
            for c in data['criteria']:
                item = {'id': c['id'], 'status': 'FAIL' if (missing and c['id'] == 'main') else 'PASS', 'evidence': 'engine records: process check, diff and QA record'}
                if missing and c['id'] == 'main':
                    item['missing'] = missing
                items.append(item)
            return review_output(json.dumps({'revision': revision(self.wt)['digest'], 'verdict': verdict, 'acceptance': items}))
        return answer

    def finalize(self, answer):
        with patch.object(core, 'capture_opencode', side_effect=answer):
            return core.finalize_run('a', str(self.wt), self.f.log, '/fake/cli', review_model='')

    def test_complete_records_yield_full_and_the_reviewer_rule_accepts_process_criteria(self):
        self.full_records()
        result = self.finalize(self.judge())
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(self.call, 1)
        prompt = self.prompts[-1]
        self.assertIn('process check: COMPLETE', prompt)
        self.assertIn('sandbox profile 1/1 (network denial 1/1, installer denial 1/1), load receipt 1/1, process closure 1/1, no install trace, nothing outside scope, no log trace', prompt)
        self.assertIn('not an absolute guarantee', prompt)
        self.assertIn('patch preservation: PRESERVED', prompt)
        self.assertIn('PROCESS CRITERION RULE', prompt)
        self.assertIn('lack of such certainty alone is not FAIL', prompt)
        self.assertIn("Your own statement that you saw no violation is not evidence", prompt)
        self.assertTrue(approved(self.store.read(self.rid)))

    def test_extending_the_carried_file_keeps_complete_and_preserved_when_the_patch_lines_survive(self):
        """The next real code job: the carried (accepted) patch file grows with new code. As long as the patch's lines stay in the product
        the engine writes 'patch preservation: PRESERVED' and acceptance continues; timestamps/write tools are product work."""
        from run_store import atomic_json
        self.full_records()
        head = self.f.git('rev-parse', 'HEAD').strip()
        (self.wt / 'product.txt').write_text('after\ncarried\n')
        patch = subprocess.run(['git', '-C', str(self.wt), 'diff', '--binary', '--no-ext-diff', 'HEAD', '--'], check=True, capture_output=True).stdout
        source = self.store.path(self.rid) / 'source'
        source.mkdir(exist_ok=True)
        (source / 'change.patch').write_bytes(patch)
        record = self.store.read(self.rid)
        record['source'] = {'sha': head, 'branch': 'cr35', 'carried': True, 'dirty_files': 1, 'patch': str(source / 'change.patch')}
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        (self.wt / 'product.txt').write_text('after\ncarried\nfresh feature\n')
        result = self.finalize(self.judge())
        self.assertEqual(result['status'], 'PASS', result)
        prompt = self.prompts[-1]
        self.assertIn('process check: COMPLETE', prompt)
        self.assertIn('patch preservation: PRESERVED', prompt)
        self.assertIn('carried patch lines: preserved — product.txt: 1/1 added lines present', prompt)
        self.assertIn('file also changed', prompt)
        self.assertTrue(approved(self.store.read(self.rid)))

    def test_missing_mandatory_record_blocks_acceptance_even_when_the_reviewer_says_valid(self):
        """Codex 21.10: with a required record missing, a judge saying PASS produced PASS/approved. On MISSING the engine does not accept,
        opens no code repair and does not call a second paid judge with the same product+evidence."""
        from acceptance import correction_decision, error_signature
        result = self.finalize(self.judge())
        self.assertEqual(result['status'], 'FAIL', result)
        self.assertTrue(result['reason'].startswith('process-check-missing'), result['reason'])
        self.assertIn('attempts without a sandbox record: 1', result['reason'])
        self.assertEqual(self.call, 1)
        self.assertIn('process check: MISSING', self.prompts[-1])
        self.assertIsNone(error_signature(result), 'A missing record cannot be fixed with code repair')
        record = self.store.read(self.rid)
        self.assertFalse(approved(record))
        self.assertFalse(record.get('rejected_revisions'), 'The product was not rejected')
        self.assertEqual(record['rejected_evidence'][-1]['digest'], revision(self.wt)['digest'])
        self.assertEqual(correction_decision(record)[0], 'STOP')
        repeat = self.finalize(self.judge())
        self.assertEqual((repeat['status'], repeat['reason']), ('FAIL', 'evidence-already-rejected'), repeat)
        self.assertEqual(self.call, 1, 'An unchanged record does not call the judge again')

    def test_missing_mandatory_record_with_evidence_gap_verdict_ends_without_repair(self):
        from acceptance import correction_decision, error_signature
        result = self.finalize(self.judge(missing='evidence', verdict='REVISE'))
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'review-evidence-gap'), result)
        self.assertIn('process check: MISSING', self.prompts[-1])
        self.assertIsNone(error_signature(result))
        record = self.store.read(self.rid)
        self.assertFalse(record.get('rejected_revisions'))
        self.assertEqual(correction_decision(record)[0], 'STOP')
        self.assertFalse(approved(record))

    def test_detected_violation_fails_before_the_paid_reviewer(self):
        from acceptance import error_signature
        self.full_records()
        with open(self.store.path(self.rid) / 'dispatch.log', 'a') as f:
            f.write('2026-10-21 14:00:09 uv run --locked pytest tests\n')
        result = self.finalize(self.judge())
        self.assertEqual(result['status'], 'FAIL', result)
        self.assertTrue(result['reason'].startswith('process-violation'), result['reason'])
        self.assertIn('uv run', result['reason'])
        self.assertEqual(self.call, 0, 'Do not call a paid judge for an actual violation')
        self.assertIsNone(error_signature(result), 'Code repair cannot resolve a process violation')
        self.assertFalse(approved(self.store.read(self.rid)))

    def test_wrong_product_is_rejected_as_product_and_opens_repair_even_when_process_is_full(self):
        from acceptance import error_signature
        self.full_records()
        result = self.finalize(self.judge(missing='product', verdict='REVISE'))
        self.assertEqual((result['status'], result['reason']), ('FAIL', 'review-not-passed'), result)
        self.assertEqual(error_signature(result), 'judge:main')
        record = self.store.read(self.rid)
        self.assertEqual(record['rejected_revisions'], [revision(self.wt)['digest']])
        self.assertFalse(record.get('rejected_evidence'))
        self.assertIn('process check: COMPLETE', self.prompts[-1], 'The product decision remains independent even with complete process evidence')
        self.assertFalse(approved(record))


class JudgeDifferenceAndInstallTraceTests(unittest.TestCase):
    """The judge gets the real source->product diff (uncommitted + untracked) within a safe scope; a file list/digest is not diff evidence.
    An install directory outside the sandbox in the worktree (the dispatcher's old `make test` path) closes acceptance."""

    def setUp(self) -> None:
        self.f = CompletionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.store, self.rid, self.wt = self.f.store, self.f.rid, self.f.wt

    def finalize(self, verdict='PASS'):
        prompts = []
        judge = self.f.review_for_prompt(verdict)

        def capture(real, wt, args, timeout, env=None, role='quality'):
            prompts.append(args[-1])
            return judge(real, wt, args, timeout, env, role)

        with patch.object(core, 'capture_opencode', side_effect=capture):
            result = core.finalize_run('a', str(self.wt), self.f.log, '/fake/cli', review_model='')
        return result, [p for p in prompts if 'ENGINE EVIDENCE' in p]

    def test_final_reviewer_receives_the_real_source_to_product_diff_including_untracked_files(self):
        self.f.successful_qa()
        (self.wt / 'fresh.txt').write_text('fresh line\n')
        result, prompts = self.finalize()
        self.assertEqual(result['status'], 'PASS', result)
        prompt = prompts[-1]
        self.assertIn('SOURCE->PRODUCT DIFF', prompt)
        self.assertIn('-before', prompt)
        self.assertIn('+after', prompt)
        self.assertIn('+++ b/fresh.txt', prompt)
        self.assertIn('+fresh line', prompt)
        self.assertIn('difference COMPLETE', prompt)
        self.assertIn('a file list or digest alone is not diff evidence', prompt.lower())
        difference = sorted(self.store.task_dir(self.rid, 'quality').glob('difference-*.diff'))
        self.assertTrue(difference, 'The difference file must be recorded in the quality record')
        self.assertIn('+fresh line', difference[-1].read_text())
        self.assertEqual(result['review_diff']['files'], 2)
        self.assertTrue(result['review_diff']['complete'])

    def test_oversized_diff_is_truncated_visibly_and_marked_partial(self):
        self.f.successful_qa()
        (self.wt / 'big.txt').write_text(''.join(f'line {i} ' + 'x' * 70 + '\n' for i in range(3000)))
        result, prompts = self.finalize()
        prompt = prompts[-1]
        self.assertIn('difference PARTIAL', prompt)
        self.assertIn('TRIMMED', prompt)
        self.assertLess(len(prompt), 200_000)
        self.assertFalse(result['review_diff']['complete'])
        self.assertIn('big.txt', result['review_diff']['truncated'])

    def test_diff_helper_skips_secret_named_files_and_binaries(self):
        from acceptance import product_difference
        (self.wt / 'credentials.txt').write_text('TOKEN=hidden-value\n')
        (self.wt / 'blob.bin').write_bytes(b'\x00\x01\x02binary')
        difference = product_difference(str(self.wt), self.f.base, ['blob.bin', 'credentials.txt', 'product.txt'])
        self.assertNotIn('hidden-value', difference['text'])
        self.assertIn('credentials.txt', difference['skipped'])
        self.assertIn('+after', difference['text'])
        self.assertNotIn('\x00', difference['text'])

    def test_install_directory_created_in_the_run_worktree_fails_acceptance_before_the_paid_reviewer(self):
        self.f.successful_qa()
        (self.wt / '.venv').mkdir()
        (self.wt / '.venv/pyvenv.cfg').write_text('home = /usr/bin\n')
        (self.wt / '.gitignore').write_text('.venv/\n')
        result, prompts = self.finalize()
        self.assertEqual(result['status'], 'FAIL')
        self.assertTrue(result['reason'].startswith('install-trace'), result)
        self.assertIn('.venv', result['reason'])
        self.assertEqual(prompts, [], 'Do not call a paid judge when install traces exist')
        self.assertFalse(approved(self.store.read(self.rid)))
        from acceptance import install_traces
        self.assertEqual(install_traces(str(self.wt)), ['.venv'])
        (self.wt / '.venv/pyvenv.cfg').unlink()
        (self.wt / '.venv').rmdir()
        self.assertEqual(install_traces(str(self.wt)), [])


if __name__ == '__main__':
    unittest.main()
