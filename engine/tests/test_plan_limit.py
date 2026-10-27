"""C9: reject untrusted plan paths (symlink escape, type/schema violation, two-plan ambiguity)."""

import json
import os
from pathlib import Path
import unittest
import time
from unittest.mock import patch

import test_run_sessions as fixtures
import test_dispatch as dispatch_fixtures
import qe_core as core
import dispatch_util


SECRET = 'sk-' + 'Q' * 32  # Synthetic marker, never a credential.


class PlanBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def plan(self):
        return {'job_id':'work', 'tasks':[{'id':'g1', 'model':'auto', 'files':['new.py'],
                'definition':'create a function', 'acceptance':'function returns 1', 'dependent':[],
                'difficulty':1, 'parallel_group':0, 'estimate_min':1, 'estimate_usd':0}]}

    def test_invalid_paths_ids_and_types_are_rejected(self):
        changes = [('files', ['../outside']), ('files', ['/tmp/outside']),
                   ('files', ['.git/config']), ('files', ['dir/../../outside']),
                   ('files', ['dir\\outside']), ('files', ['*.py']),
                   ('files', ['a,b.py']), ('files', 'file.py'), ('files', [None]),
                   ('id', 'g1/../../outside'), ('difficulty', True), ('estimate_min', '1; false'),
                   ('estimate_usd', float('nan')), ('acceptance', {}), ('model', 'unknown')]
        for field, value in changes:
            with self.subTest(field=field, value=value):
                plan = self.plan()
                plan['tasks'][0][field] = value
                with self.assertRaises(ValueError):
                    dispatch_util.validate_plan(plan, self.f.wt)

    def test_harmless_path_shapes_are_normalized_instead_of_killing_the_whole_plan(self):
        """B95 (18.09 live): deepseek -> cerebras -> gemini, all three plans were thrown away with `file path must be explicit and
        relative`. Throwing away a structurally correct plan because of one path DETAIL is our defect."""
        plan = self.plan()
        plan['tasks'][0]['files'] = ['./a.py', 'a.py', 'dir//b.py', 'dir/', ' "c.py" ']
        dispatch_util.validate_plan(plan, self.f.wt)
        self.assertEqual(plan['tasks'][0]['files'], ['a.py', 'dir/b.py', 'dir', 'c.py'])

    def test_annotated_plan_paths_keep_the_module_the_plan_named(self):
        """D01 (30.09 rehearsal 080502/134757): `path (description)` was accepted as the path as is, and the scope check counted the real
        file as outside the plan and removed it -> ModuleNotFoundError. The description is separated from the path; the path stays in scope."""
        import test_outcome
        plan = self.plan()
        plan['tasks'][0]['files'] = [
            'src/budget/ (existing migration/schema module — the scout chooses)',
            'src/budget/operations.py (new storage module; use the project convention if storage modules have different names)',
            'src/budget/cli.py (or the existing CLI entry module / __main__.py)', 'tests/test_operation.py']
        dispatch_util.validate_plan(plan, self.f.wt)
        self.assertEqual(plan['tasks'][0]['files'],
                         ['src/budget', 'src/budget/operations.py', 'src/budget/cli.py', 'tests/test_operation.py'])
        f = test_outcome.CompletionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        once = core.lane_status(f.wt)
        module = f.wt/'src/budget/operations.py'
        module.parent.mkdir(parents=True)
        module.write_text('VALUE = 1\n')
        scoped_plan = {'tasks': [{'id': 'g1', 'files': [dispatch_util.plan_path(
            'src/budget/operations.py (new storage module)', f.wt)]}]}
        outgoing, _ = core.scope_audit(f.wt, scoped_plan, 'g1', once, f.home/'reserve')
        self.assertEqual(outgoing, [])
        self.assertTrue(module.exists())

    def test_schema_violation_is_reported_as_a_correctable_schema_error(self):
        """A schema violation is NOT A MODEL FAILURE: a fixed format + exit code 2 so the caller can tell them apart."""
        import subprocess
        import sys as _sys
        plan = self.plan()
        plan['tasks'][0]['files'] = ['/etc/passwd']
        tool = Path(dispatch_util.__file__)
        r = subprocess.run([_sys.executable, str(tool), 'extract'], input=json.dumps(plan),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)
        self.assertTrue(r.stderr.startswith('SCHEMA: '), r.stderr)
        self.assertEqual(r.stdout.strip(), '')

    def test_symlink_escape_is_rejected_before_any_writer(self):
        f = dispatch_fixtures.SchedulerTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        outside = f.fixture.home/'outside'
        outside.mkdir()
        (f.fixture.wt/'link').symlink_to(outside, target_is_directory=True)
        task = f.task('g1')
        task['files'] = ['link/product.txt']
        self.assertNotEqual(f.fixture.dispatch_fixture('PASS', tasks=[task], worker=dispatch_fixtures.WORKER), 0)
        self.assertEqual(f.events(), [])
        self.assertEqual(list(outside.iterdir()), [])

    def test_json_braces_in_text_are_data_and_two_plans_are_ambiguous(self):
        plan = self.plan()
        plan['tasks'][0]['definition'] = 'parse {a:{b:1}} safely'
        text = '```json\n' + json.dumps(plan) + '\n```'
        self.assertEqual(dispatch_util.extract_plan(text), plan)
        with self.assertRaises(ValueError):
            dispatch_util.extract_plan(text + text)



class LevelPlanLimitTests(unittest.TestCase):
    """G-E9: `dispatch_util.py extract <rule>` rejects a plan exceeding the level limit with a SCHEMA error; the dispatcher gives it back
    to the planner as a correction request (not counted as a model failure)."""
    def call(self, n, points, *arg):
        import subprocess
        import sys
        def task(i):
            return {'id': f'g{i}', 'model': 'auto', 'files': [f'f{i}.py'], 'definition': 't', 'acceptance': 'k', 'dependent': [],
                    'difficulty': 1, 'parallel_group': 0, 'estimate_min': 1}
        plan = {'job_id': 'work', 'scope_points': points, 'tasks': [task(i) for i in range(1, n + 1)]}
        r = subprocess.run([sys.executable, dispatch_util.__file__, 'extract', *arg], input=json.dumps(plan),
                           capture_output=True, text=True, timeout=30)
        return r.returncode, r.stderr

    def test_plan_exceeding_the_level_limit_gives_a_schema_error(self):
        rc, err = self.call(4, 30, '25')
        self.assertEqual(rc, 2, err)
        self.assertTrue(err.startswith('SCHEMA: '), err)
        self.assertIn('at most 3', err)

    def test_call_within_the_limit_and_without_a_rule_is_accepted(self):
        self.assertEqual(self.call(3, 30, '25')[0], 0)
        self.assertEqual(self.call(4, 30)[0], 0, 'Legacy callers without a rule are unaffected')

if __name__ == '__main__':
    unittest.main()
