"""G-E10 adaptive splitting: at very hard+ level a lane whose chain is exhausted (failed) is split once into 2–3 sequential sub-tasks;
a sub-task is not split again, sub-task files cannot leave the parent task's list, a user decision / environment failure
is not split. No model/network; the end-to-end tests use the real dispatcher + coordinator with a fake worker/planner/judge."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import task_split  # noqa: E402
import dispatch_util  # noqa: E402

import os
from unittest.mock import patch

def setUpModule():
    environment = patch.dict(os.environ, {'CADRE_CHAIN_QUALITY': 'opencode-go/qwen3.8-max'})
    environment.start()
    unittest.addModuleCleanup(environment.stop)



class SplitSampleTest(unittest.TestCase):
    def test_the_example_sub_plan_passes_plan_validation(self):
        """06.10 QA: the example did not carry `estimate_usd`; a sub-plan copying it was rejected by D12 and G-E10 died."""
        plan = {'job_id': 'x', 'tasks': [{'id': 'g1', 'definition': 't', 'acceptance': 'k', 'files': ['a.py'], 'dependent': [],
                                            'difficulty': 4, 'model': 'ds-pro', 'parallel_group': 1, 'estimate_min': 30, 'estimate_usd': 1.0}]}
        sample = task_split.split_sample(plan, 'g1')
        dispatch_util.validate_plan(sample)
        self.assertIn('"estimate_usd"', task_split.split_prompt(plan, 'g1', 'reason'))


def plan():
    return {'job_id': 'task', 'tasks': [
        {'id': 'g1', 'definition': 'write the module', 'files': ['src/a.py', 'tests/test_a.py'], 'acceptance': 'tests pass', 'dependent': [],
         'difficulty': 4, 'model': 'auto', 'parallel_group': 1, 'estimate_min': 60},
        {'id': 'g2', 'definition': 'document', 'files': ['README.md'], 'acceptance': 'line present', 'dependent': ['g1'],
         'difficulty': 2, 'model': 'auto', 'parallel_group': 1, 'estimate_min': 10}]}


def sub(id_, files, dependent=()):
    return {'id': id_, 'definition': f'part {id_}', 'files': files, 'acceptance': f'{id_} is measurable', 'dependent': list(dependent),
            'difficulty': 3, 'model': 'auto', 'parallel_group': 1, 'estimate_min': 20, 'sub_agents': []}


def sub_output(*tasks):
    return 'description\n```json\n' + json.dumps({'job_id': 'g1-divide', 'tasks': list(tasks)}) + '\n```'


class SplittableTest(unittest.TestCase):
    def test_non_converging_lane_at_very_hard_and_ultra_level_is_split(self):
        for level in ('very-hard', 'ultra'):
            self.assertTrue(task_split.splittable(plan(), 'g1', level, 'rc=1')[0], level)
        self.assertTrue(task_split.splittable(plan(), 'g1', 'very-hard', '')[0], 'A lane without a recorded reason can also be split')

    def test_hard_and_below_are_not_split(self):
        for level in ('simple', 'middle', 'hard', ''):
            self.assertFalse(task_split.splittable(plan(), 'g1', level, 'rc=1')[0], level)

    def test_user_decision_and_environment_failure_are_not_split(self):
        for reason in ('no-capacity', 'disk-low', 'install-error', 'critical-file-violation', 'approval-waiting: X or Y'):
            self.assertFalse(task_split.splittable(plan(), 'g1', 'very-hard', reason)[0], reason)

    def test_sub_task_and_split_task_are_not_split_again(self):
        p = plan()
        p['tasks'][0]['parent'] = 'g0'
        self.assertFalse(task_split.splittable(p, 'g1', 'very-hard', 'rc=1')[0])
        p = plan()
        p['tasks'][0]['split_into'] = ['g1-1', 'g1-2']
        self.assertFalse(task_split.splittable(p, 'g1', 'very-hard', 'rc=1')[0])


class SplitPromptTest(unittest.TestCase):
    def test_prompt_contains_the_parent_task_the_reason_the_files_and_the_schema(self):
        prompt = task_split.split_prompt(plan(), 'g1', 'rc=1', '[{"id":"m1","passes":true}]')
        for part in ('SPLIT the task INTO SUB-TASKS', 'write the module', 'tests pass', 'src/a.py', 'tests/test_a.py', 'rc=1', '2-3', '"tasks"', '"m1"'):
            self.assertIn(part, prompt)


class SubPlanMergeTest(unittest.TestCase):
    def test_valid_sub_plan_is_appended_in_order_after_the_parent_task(self):
        fresh, ids = task_split.sub_plan_merge(plan(), 'g1', sub_output(sub('a', ['src/a.py']), sub('b', ['./tests/test_a.py'], ['a'])))
        self.assertEqual(ids, ['g1-1', 'g1-2'])
        g = {t['id']: t for t in fresh['tasks']}
        self.assertEqual([t['id'] for t in fresh['tasks']], ['g1', 'g1-1', 'g1-2', 'g2'])
        self.assertEqual(g['g1']['split_into'], ids)
        self.assertEqual((g['g1-1']['parent'], g['g1-1']['dependent'], g['g1-2']['dependent']), ('g1', [], ['g1-1']))
        self.assertEqual(g['g1-2']['files'], ['tests/test_a.py'], 'The path is normalized')
        self.assertNotIn('sub_agents', g['g1-1'], 'A subtask does not call a helper')

    def test_out_of_bounds_number_of_sub_tasks_is_rejected(self):
        for n in (1, 4):
            with self.subTest(n=n), self.assertRaises(ValueError):
                task_split.sub_plan_merge(plan(), 'g1', sub_output(*[sub(f'x{i}', ['src/a.py']) for i in range(n)]))

    def test_file_outside_the_parent_task_is_rejected(self):
        with self.assertRaises(ValueError):
            task_split.sub_plan_merge(plan(), 'g1', sub_output(sub('a', ['src/a.py']), sub('b', ['src/fresh.py'])))

    def test_non_json_output_is_rejected(self):
        with self.assertRaises(ValueError):
            task_split.sub_plan_merge(plan(), 'g1', 'I could not produce a plan')

    def test_main_loop_order_excludes_sub_tasks(self):
        fresh, _ = task_split.sub_plan_merge(plan(), 'g1', sub_output(sub('a', ['src/a.py']), sub('b', ['src/a.py'])))
        self.assertEqual(dispatch_util.main_order(fresh), ['g1', 'g2'])



class SplitPlannerDurationTest(unittest.TestCase):
    """The splitting planner may fall to a local model (~10 tokens/s); the plan loop's 300 s are not enough for it, splitting gives 900 s."""
    def test_split_call_is_made_with_a_long_timeout(self):
        source = (LIB.parent / 'bin' / 'cadre-dispatch.sh').read_text()
        self.assertTrue('"divide-$gid" "$bp/sandbox" 4 split' in source, 'The split call uses the splitting deadline')
        self.assertTrue("deadline(os.environ['DURATION_ROLE'])" in source and 'DURATION_ROLE="${6:-plan}"' in source, 'The default planner uses the plan deadline')
        import limits
        self.assertEqual((limits.deadline('split'), limits.deadline('plan')), (900, 1800))   # 4c-4: single source

SPLIT_WORKER = """
import os, pathlib, sys
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
from run_store import RunStore
gid = os.environ['CADRE_TASK_ID']; wt = pathlib.Path(os.environ['CADRE_WORKTREE'])
if gid == 'g1' or gid == os.environ.get('FIXTURE_SUB_FAILED'):
    sys.exit(1)
(wt / 'product.txt').write_text((wt / 'product.txt').read_text() + gid + '\\n')
RunStore().task_update(os.environ['CADRE_RUN_ID'], gid, state='produced')
"""
SUB_PLAN = json.dumps({'job_id': 'g1-divide', 'tasks': [sub('a', ['product.txt']), sub('b', ['product.txt'], ['a'])]})


class SplitEndToEndTest(unittest.TestCase):
    """Real dispatcher: the main lane exhausts the chain -> split at very hard level -> produces sub-tasks -> acceptance."""
    def setUp(self):
        import test_outcome
        self.f = test_outcome.CompletionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def execute_run(self, plan_extra=None, **env):
        rc = self.f.dispatch_fixture('PASS', worker=SPLIT_WORKER, env={'FIXTURE_SUB_PLAN': SUB_PLAN, **env}, plan_extra=plan_extra)
        record = self.f.store.read(self.f.rid)
        log = (self.f.store.path(self.f.rid) / 'dispatch.log').read_text()
        return rc, record, log

    def test_failed_lane_at_very_hard_level_is_split_and_completed(self):
        rc, record, log = self.execute_run(plan_extra={'scope_points': 70})
        self.assertEqual((rc, record['state']), (0, 'completed'), log[-3000:])
        self.assertIn('SPLIT INTO SUBTASKS', log)
        outcome = {g: record['tasks'][g].get('dispatch_result') for g in ('g1', 'g1-1', 'g1-2')}
        self.assertEqual(outcome, {'g1': 'produced', 'g1-1': 'produced', 'g1-2': 'produced'})
        fresh = json.loads((self.f.store.path(self.f.rid) / 'plan.json').read_text())
        self.assertEqual(fresh['tasks'][0]['split_into'], ['g1-1', 'g1-2'])
        self.assertIn('g1-2', (self.f.wt / 'product.txt').read_text())

    def test_parent_task_stays_failed_if_sub_task_failed(self):
        rc, record, log = self.execute_run(plan_extra={'scope_points': 70}, FIXTURE_SUB_FAILED='g1-2')
        self.assertEqual(rc, 1)
        self.assertEqual(record['tasks']['g1'].get('dispatch_result'), 'failed')
        self.assertIn('subtask g1-2', log)

    def test_no_split_is_tried_at_medium_level(self):
        rc, _record, log = self.execute_run()
        self.assertEqual(rc, 1)
        self.assertNotIn('SPLIT INTO SUBTASKS', log)


if __name__ == '__main__':
    unittest.main()
