"""G-C16 correction loop (G-E9/G-C16): when QA or the judge says REVISE the findings go back
to the executor, the product is fixed and acceptance runs again. Stop conditions: PASS · round allowance · same error signature · breaking
test count did not drop · unfixable reason (ERROR, independence, test deletion/weakening, rejected revision). No model/network."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import qe_core as core  # noqa: E402

import os
from unittest.mock import patch

def setUpModule():
    environment = patch.dict(os.environ, {'CADRE_CHAIN_QUALITY': 'opencode-go/qwen3.8-max'})
    environment.start()
    unittest.addModuleCleanup(environment.stop)



def qa_outcome(output: str) -> dict:
    f = Path(tempfile.mkdtemp(prefix='correction-qa-')) / 'qa.log'
    f.write_text(output)
    return {'status': 'FAIL', 'reason': 'qa-not-passed', 'qa': {'status': 'FAIL', 'attempts': [{'output': str(f)}]},
            'review': {'status': 'SKIPPED'}}


def judge_outcome(fail_ids=('g1',), verdict='REVISE', status='FAIL') -> dict:
    acceptance = [{'id': i, 'status': 'FAIL', 'evidence': f'{i}: evidence line tests/x.py:3'} for i in fail_ids]
    acceptance.append({'id': 'original_request', 'status': 'PASS', 'evidence': 'complete'})
    return {'status': 'FAIL', 'reason': 'review-not-passed', 'qa': {'status': 'PASS'},
            'review': {'status': status, 'verdict': verdict, 'acceptance': acceptance}}


class ErrorSignatureTest(unittest.TestCase):
    def test_qa_signature_is_the_broken_test_ids(self):
        fr = qa_outcome('..F\nFAILED tests/b.py::t2 - x\nFAILED tests/a.py::t1 - y\n')
        self.assertEqual(core.error_signature(fr), 'qa:tests/a.py::t1,tests/b.py::t2')

    def test_stable_summary_signature_for_id_less_qa_output(self):
        """Without a breaking test id (build/lint error) the signature is a stable digest of the tail of the output."""
        a, b = core.error_signature(qa_outcome('ruff: E501 line too long\n')), core.error_signature(qa_outcome('ruff: E501 line too long\n'))
        self.assertTrue(a.startswith('qa:#'), a)
        self.assertEqual(a, b)

    def test_judge_signature_is_the_fail_criteria_and_only_revise_counts(self):
        self.assertEqual(core.error_signature(judge_outcome(('g2', 'g1'))), 'judge:g1,g2')
        self.assertIsNone(core.error_signature(judge_outcome(verdict=None, status='ERROR')), 'A judge failure is not a product defect')

    def test_unfixable_reasons_produce_no_signature(self):
        for reason in ('revision-already-rejected', 'reviewer-not-independent', 'test deleted: tests/a.py',
                      'test weakened: tests/a.py (3->1)', 'finalization-incomplete'):
            with self.subTest(reason=reason):
                self.assertIsNone(core.error_signature({'status': 'FAIL', 'reason': reason}))


class CorrectionDecisionTest(unittest.TestCase):
    def record(self, fr, history=()):
        return {'final_result': fr, 'correction': list(history)}

    def test_continue_on_first_error(self):
        decision, round, signature = core.correction_decision(self.record(judge_outcome()), parent=2)
        self.assertEqual((decision, round, signature), ('PROCEED', 1, 'judge:g1'))

    def test_stop_when_the_round_allowance_is_finished(self):
        history = [{'round': 1, 'signature': 'judge:g0'}, {'round': 2, 'signature': 'judge:g3'}]
        self.assertEqual(core.correction_decision(self.record(judge_outcome(), history), parent=2)[0], 'STOP')

    def test_same_signature_twice_no_progress(self):
        decision, _, reason = core.correction_decision(self.record(judge_outcome(), [{'round': 1, 'signature': 'judge:g1'}]), parent=3)
        self.assertEqual(decision, 'STOP')
        self.assertIn('same error', reason)

    def test_stop_if_the_broken_test_count_did_not_decrease_continue_if_it_did(self):
        once = [{'round': 1, 'signature': 'qa:tests/a.py::t1'}]
        decision, _, reason = core.correction_decision(self.record(qa_outcome('FAILED tests/b.py::t2\nFAILED tests/c.py::t3\n'), once), parent=3)
        self.assertEqual((decision, 'did not decrease' in reason), ('STOP', True))
        two = [{'round': 1, 'signature': 'qa:tests/a.py::t1,tests/b.py::t2'}]
        self.assertEqual(core.correction_decision(self.record(qa_outcome('FAILED tests/b.py::t2\n'), two), parent=3)[0], 'PROCEED')

    def test_unfixable_reason_and_pass_stop(self):
        self.assertEqual(core.correction_decision(self.record({'status': 'FAIL', 'reason': 'revision-already-rejected'}), parent=2)[0], 'STOP')
        self.assertEqual(core.correction_decision(self.record({'status': 'PASS'}), parent=2)[0], 'STOP')



class CleanRestartTest(unittest.TestCase):
    """Design 3.2 (research: when the effect fades after 2-3 rounds, restarting with a clean context is better): at hard+ level if the loop does not converge
    (round allowance, same signature, broken tests not decreasing) one 'restart' round is given; not at medium/simple level, never twice."""
    def record(self, fr, history=()):
        return {'final_result': fr, 'correction': list(history)}

    def test_non_converging_loop_at_hard_level_restarts_once(self):
        history = [{'round': 1, 'signature': 'judge:g1'}]
        self.assertEqual(core.correction_decision(self.record(judge_outcome(), history), parent=2, again=True)[:2], ('PROCEED', 'again'))
        self.assertEqual(core.correction_decision(self.record(judge_outcome(), history), parent=2, again=False)[0], 'STOP')

    def test_the_retry_round_is_given_once(self):
        history = [{'round': 1, 'signature': 'judge:g1'}, {'round': 'again', 'signature': 'judge:g1'}]
        self.assertEqual(core.correction_decision(self.record(judge_outcome(), history), parent=2, again=True)[0], 'STOP')

    def test_no_retry_on_an_unfixable_reason(self):
        self.assertEqual(core.correction_decision(self.record({'status': 'FAIL', 'reason': 'revision-already-rejected'}), parent=2, again=True)[0], 'STOP')

    def test_the_retry_prompt_carries_the_permission_to_rewrite_from_scratch(self):
        request = Path(tempfile.mkdtemp(prefix='correction-request-')) / 'prompt.md'
        request.write_text('job\n')
        text = core.correction_prompt({'prompt': str(request), 'final_result': judge_outcome()}, round='again', parent=2)
        self.assertIn('CLEAN RESTART', text)


class CorrectionPromptTest(unittest.TestCase):
    def test_prompt_carries_the_findings_the_rules_and_the_original_request(self):
        request = Path(tempfile.mkdtemp(prefix='correction-request-')) / 'prompt.md'
        request.write_text('original task: add the function\n')
        record = {'prompt': str(request), 'final_result': judge_outcome(('g2',)),
                 'tasks': {'g1': {'scope_outside_fresh': ['tests/excess.py']}}}
        text = core.correction_prompt(record, round=1, parent=2)
        for part in ('CORRECTION ROUND 1/2', 'JUDGE FAIL [g2]', 'g2: evidence line', 'tests/excess.py', 'BLOCKED', 'original task: add the function'):
            self.assertIn(part, text)
        self.assertNotIn('JUDGE FAIL [original_request]', text, 'A passing criterion does not inflate the prompt')

    def test_cr49_long_original_request_is_never_cut_silently_and_the_failed_last_criterion_still_reaches_repair(self):
        request = Path(tempfile.mkdtemp(prefix='correction-request-')) / 'prompt.md'
        long = 'original criterion line\n' * 400 + 'END CRITERION: README preserved\n'
        request.write_text(long)
        text = core.correction_prompt({'prompt': str(request), 'final_result': judge_outcome(('last',))}, round=1, parent=2)
        self.assertIn('JUDGE FAIL [last]', text, 'Carry the remaining criterion explicitly into repair')
        self.assertIn(f'TRIMMED: first 4000/{len(long)} characters', text)
        request.with_name('short.md').write_text('short request\n')
        short = core.correction_prompt({'prompt': str(request.with_name('short.md')), 'final_result': judge_outcome()}, round=1, parent=2)
        self.assertNotIn('TRIMMED', short)

    def test_qa_prompt_carries_the_broken_test_output(self):
        request = Path(tempfile.mkdtemp(prefix='correction-request-')) / 'prompt.md'
        request.write_text('job\n')
        text = core.correction_prompt({'prompt': str(request), 'final_result': qa_outcome('E   assert 1 == 2\nFAILED tests/a.py::t1\n')}, round=2, parent=2)
        self.assertIn('assert 1 == 2', text)
        self.assertIn('QA', text)



CORRECTION_WORKER = """
import os, pathlib, sys
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
from run_store import RunStore
gid = os.environ['CADRE_TASK_ID']; wt = pathlib.Path(os.environ['CADRE_WORKTREE'])
if gid.startswith('correction') and os.environ.get('FIXTURE_CORRECTION_CHANGE', '1') == '1':
    (wt / 'product.txt').write_text((wt / 'product.txt').read_text() + gid + '\\n')
RunStore().task_update(os.environ['CADRE_RUN_ID'], gid, state='produced')
"""


class CorrectionLoopEndToEndTest(unittest.TestCase):
    """Real dispatcher + coordinator, a fake worker and a fake judge with ordered, deterministic verdicts."""
    def setUp(self):
        import test_outcome
        self.f = test_outcome.CompletionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def execute_run(self, decisions, plan_extra=None, **env):
        rc = self.f.dispatch_fixture('PASS', worker=CORRECTION_WORKER, env={'FIXTURE_VERDICTS': decisions, **env}, plan_extra=plan_extra)
        record = self.f.store.read(self.f.rid)
        call = len((self.f.home / 'reviewer-calls').read_text()) if (self.f.home / 'reviewer-calls').exists() else 0
        return rc, record, call

    def test_after_revise_the_correction_round_fixes_the_product_and_accepts(self):
        rc, record, call = self.execute_run('REVISE,PASS')
        self.assertEqual((rc, record['state'], call), (0, 'completed', 2))
        self.assertEqual([t['round'] for t in record.get('correction') or []], [1])
        self.assertIn('correction-1', (self.f.wt / 'product.txt').read_text())
        lines = [json.loads(s) for s in (self.f.store.path(self.f.rid) / 'assignment-history.jsonl').read_text().splitlines()]
        self.assertTrue(lines and all({'start_model', 'tried_models', 'last_model'} <= set(s) for s in lines), lines)

    def test_loop_stops_if_the_same_error_comes_twice(self):
        rc, record, call = self.execute_run('REVISE,REVISE,PASS')
        self.assertEqual(rc, 1)
        self.assertNotEqual(record['state'], 'completed')
        self.assertEqual((len(record.get('correction') or []), call), (1, 2), 'A second identical REVISE stops execution before PASS')

    def test_qa_red_turns_green_with_the_correction_round(self):
        # a fix in the planned file: a new file outside the plan is removed from the product by the scope check (B192), it does not turn QA green
        from qa_fixture import command
        self.f.successful_qa = lambda: Path(self.f.run['constraints']['qa-command']).write_text(command('grep -q correction product.txt'))
        _rc, record, _ = self.execute_run('PASS')
        self.assertEqual([t['signature'][:3] for t in record.get('correction') or []], ['qa:'])
        self.assertEqual(record['final_result']['qa']['status'], 'PASS', 'QA passes after the correction round')
        # the judge step depends on the environment: QA repair makes the local model the writer (B180), there is no cloud judge candidate in the fixture environment

    def test_non_converging_loop_at_hard_level_ends_with_a_clean_restart(self):
        rc, record, call = self.execute_run('REVISE,REVISE,PASS', plan_extra={'scope_points': 50})
        self.assertEqual((rc, record['state'], call), (0, 'completed', 3), record.get('final_result'))
        self.assertEqual([t['round'] for t in record.get('correction') or []], [1, 'again'])
        self.assertEqual(record['scope']['level'], 'hard')
        run = self.f.store.path(self.f.rid)
        output = (run / 'dispatch.log').read_text() + (run / 'coordinator.log').read_text()
        self.assertNotIn('command not found', output, 'Dispatcher functions must exist when a run resumes with a saved plan (G-E11)')

    def test_a_correction_that_does_not_change_the_product_stops_on_the_rejected_revision(self):
        rc, record, call = self.execute_run('REVISE,PASS', FIXTURE_CORRECTION_CHANGE='0')
        self.assertEqual((rc, call), (1, 1), 'An unchanged product is not reviewed again (B193)')
        self.assertEqual(record['state'], 'failed')
        self.assertEqual(record['final_result']['reason'], 'revision-already-rejected')


PLAIN_RUNNER_WORKER = """
import os, pathlib, subprocess, sys
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
from run_store import RunStore, owner_fds
gid = os.environ.get('CADRE_TASK_ID', ''); wt = pathlib.Path(os.environ['CADRE_WORKTREE'])
if not gid:   # The simple engine runs its own final acceptance without correction
    pathlib.Path(os.environ['CADRE_ENGINE_ROOT'], 'plain-runner-call').open('a').write('x')
    env = {**os.environ, 'CADRE_TASK_ID': 'quality'}
    raise SystemExit(subprocess.run([sys.executable, os.environ['CADRE_ENGINE_ROOT'] + '/lib/qe_core.py', 'finish', sys.argv[1], str(wt)],
                                    env=env, pass_fds=owner_fds()).returncode)
(wt / 'product.txt').write_text((wt / 'product.txt').read_text() + gid + '\\n')
RunStore().task_update(os.environ['CADRE_RUN_ID'], gid, state='produced')
"""


class PlainRunnerCorrectionTest(unittest.TestCase):
    """D13 (B274, 30.09 child 060218): on the plan fallback (plain engine) path the judge said REVISE, and the run ended at the first acceptance FAIL."""
    def setUp(self):
        import test_outcome
        self.f = test_outcome.CompletionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_if_plain_runner_acceptance_falls_the_same_g_c16_loop_runs(self):
        rc = self.f.dispatch_fixture('PASS', worker=PLAIN_RUNNER_WORKER, phase='runner', env={'FIXTURE_VERDICTS': 'REVISE,PASS'})
        record = self.f.store.read(self.f.rid)
        self.assertEqual((rc, record['state']), (0, 'completed'), record.get('final_result', {}).get('reason'))
        self.assertEqual([t['round'] for t in record.get('correction') or []], [1])
        self.assertIn('correction-1', (self.f.wt / 'product.txt').read_text())
        self.assertEqual(len((self.f.home / 'reviewer-calls').read_text()), 2, 'Do not repeat the first acceptance (B193)')
        self.assertEqual(len((self.f.home / 'dispatcher' / 'plain-runner-call').read_text()), 1)

    def test_failed_correction_phase_leaves_the_retry_to_the_runner(self):
        rc = self.f.dispatch_fixture('PASS', worker=PLAIN_RUNNER_WORKER, phase='runner', env={'FIXTURE_VERDICTS': 'REVISE,REVISE,REVISE'})
        record = self.f.store.read(self.f.rid)
        self.assertEqual(rc, 1)
        self.assertGreaterEqual(len(record.get('correction') or []), 1)
        self.assertEqual(record['phase'], 'runner')

    def test_no_loop_is_opened_on_an_unfixable_plain_runner_error(self):
        rc = self.f.dispatch_fixture('PASS', worker=PLAIN_RUNNER_WORKER, phase='runner', env={'FIXTURE_VERDICTS': 'PASS'})
        self.assertEqual((rc, self.f.store.read(self.f.rid)['state']), (0, 'completed'))
        self.assertNotIn('SIMPLE ENGINE ACCEPTANCE FAILED', (self.f.store.path(self.f.rid) / 'coordinator.log').read_text())


if __name__ == '__main__':
    unittest.main()
