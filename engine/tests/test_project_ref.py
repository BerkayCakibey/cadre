"""G-P project mode (stage 2) — the pure core: feature list validation, selection, state transitions,
stop conditions, re-plan merging, report. No model/network/git."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import project_mode  # noqa: E402

DAY = 86400


def own(id_, dependent=(), priority=2, status='waiting', attempt=0):
    return {'id': id_, 'title': f'{id_} title', 'description': f'Implement {id_}', 'acceptance': f'{id_} test passes',
            'dependent': list(dependent), 'priority': priority, 'status': status, 'attempt': attempt, 'run': [], 'qa_command': f'test -e {id_}'}


def output(*features):
    return 'list:\n```json\n' + json.dumps({'features': list(features)}) + '\n```'


class FeatureListTest(unittest.TestCase):
    def test_valid_list_is_normalized(self):
        list = project_mode.feature_list(output({'id': 'f1', 'title': 'core', 'description': 'data model', 'acceptance': 'unit tests',
                                             'dependent': [], 'priority': 1, 'qa_command': ' python3 -m unittest tests.test_model '}))
        self.assertEqual(list, [{'id': 'f1', 'title': 'core', 'description': 'data model', 'acceptance': 'unit tests',
                                  'dependent': [], 'priority': 1, 'status': 'waiting', 'attempt': 0, 'run': [],
                                  'qa_command': 'python3 -m unittest tests.test_model'}])

    def test_broken_lists_are_rejected(self):
        many = [own(f'f{i}') for i in range(project_mode.FEATURE_CAP + 1)]
        statuses = {
            'empty': output(),
            'cap': output(*many),
            'repeat id': output(own('f1'), own('f1')),
            'unknown dependency': output(own('f1', ['f9'])),
            'loop': output(own('f1', ['f2']), own('f2', ['f1'])),
            'without-acceptance': output({**own('f1'), 'acceptance': ''}),
            'without an acceptance command': output({**own('f1'), 'qa_command': ' '}),
            'placeholder command': output({**own('f1'), 'qa_command': 'targeted test command'}),
            'invalid id': output(own('F 1')),
            'not json': 'could not complete',
        }
        for entry_name, raw in statuses.items():
            with self.subTest(entry_name), self.assertRaises(ValueError):
                project_mode.feature_list(raw)


class PlannerRingTest(unittest.TestCase):
    """B250 (26.09 live B203c, $2.15 wasted): (a) when the first link's list passed the rough check but failed full validation
    (big-pickle's truncated 15th item) the project stopped with 'list-could-not-produce' — the next link was never tried;
    (b) the list went to Claude with #max effort + difficulty 5, and opus#max cut the long output at the budget cap ($1.84)."""
    def test_ring_that_falls_in_full_verification_passes_to_the_next(self):
        from unittest.mock import patch
        answers = {'m1': output({**own('f1'), 'acceptance': ''}, own('f2')), 'm2': output(own('f1'), own('f2', ['f1']))}
        calls = []

        def fake(store, rid, model, prompt, gid):
            calls.append(model)
            return answers[model]

        with patch.object(project_mode, '_planner_call', side_effect=fake), \
             patch('model_router.chain', return_value=(['m1', 'm2', 'm3'], [])):
            raw = project_mode.plan_real(None, 'main')('prompt', 'project-ref-list')
        self.assertEqual(calls, ['m1', 'm1', 'm2'], 'a fallback returning an invalid list is skipped after one correction opportunity (B252b); the valid one returns')
        self.assertEqual([f['id'] for f in project_mode.feature_list(raw)], ['f1', 'f2'])

    def test_replan_ring_accepts_dependency_on_preserved_ids(self):
        """B252 (26.09 live B203d, 09:05): B250's link validation did not know the kept (passed/stuck) ids; in a re-plan
        a new item depending on f17 (stuck) was rejected as an 'unknown dependency' and the project stayed blocked."""
        from unittest.mock import MagicMock, patch
        root = Path(tempfile.mkdtemp(prefix='again-')); store = MagicMock(); store.path.return_value = root
        (root / 'project_mode.json').write_text(json.dumps({'features': [own('f16', status='passed'), own('f17', status='applied', attempt=2), own('f18', ['f17'])]}))
        calls = []

        def fake(store_, rid, model, prompt, gid):
            calls.append(model)
            return output(own('f18', ['f17']), own('f24', ['f16', 'f18']))

        with patch.object(project_mode, '_planner_call', side_effect=fake), \
             patch('model_router.chain', return_value=(['m1', 'm2'], [])):
            raw = project_mode.plan_real(store, 'main')('prompt', 'project-ref-again')
        self.assertEqual(calls, ['m1'], 'a dependency on a completed protected id is valid; no second fallback is needed')
        fresh = project_mode.again_plan_merge(json.loads((root / 'project_mode.json').read_text())['features'], raw)
        self.assertEqual([f['id'] for f in fresh], ['f16', 'f17', 'f18', 'f24'])

    def test_verification_error_is_given_back_to_the_same_ring_once(self):
        """B252b: a real validation error (unknown id) goes back to the same link ONCE with the error text; if fixed, accepted."""
        from unittest.mock import patch
        calls = []

        def fake(store, rid, model, prompt, gid):
            calls.append((model, 'YOUR PREVIOUS ANSWER WAS INVALID' in prompt))
            return output(own('f1'), own('f2', ['f1'])) if len(calls) > 1 else output(own('f1'), own('f2', ['f9']))

        with patch.object(project_mode, '_planner_call', side_effect=fake), \
             patch('model_router.chain', return_value=(['m1', 'm2'], [])):
            raw = project_mode.plan_real(None, 'main')('prompt', 'project-ref-list')
        self.assertEqual(calls, [('m1', False), ('m1', True)])
        self.assertEqual([f['id'] for f in project_mode.feature_list(raw)], ['f1', 'f2'])

    def test_claude_list_request_goes_with_high_effort_and_difficulty_4(self):
        from test_run_sessions import SessionTests, runner_source
        import qe_core as core
        f = SessionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        runner_source(f.home, f.wt)
        (f.wt / 'README.md').write_text('source at the real project root')
        fake = f.home / 'source-claude'
        fake.write_text('#!' + sys.executable + '\n' + r'''
import json,os,pathlib,sys
a=sys.argv[1:]; sid=a[a.index('--session-id')+1]
home=pathlib.Path(os.environ['CADRE_HOME'])
(home/'source-read.json').write_text(json.dumps({'args':a,'cwd':os.getcwd(),'source':pathlib.Path('README.md').read_text()}))
print(json.dumps({'type':'result','session_id':sid,'result':'source read','total_cost_usd':0}))
''')
        fake.chmod(0o700)
        f.allow_fixture_claude(fake)
        with patch.dict(os.environ, {'CLAUDE_BIN': str(fake)}), patch.object(core, 'run_claude', wraps=core.run_claude) as call:
            answer = project_mode._planner_call(f.store, f.rid, 'claude/opus#high', 'prompt', 'project-ref-list')
        self.assertEqual(answer, 'source read')
        self.assertEqual(call.call_args.args[1], 'claude/opus#high')
        self.assertEqual(call.call_args.kwargs['difficulty'], 4)
        read = json.loads((f.home / 'source-read.json').read_text())
        self.assertEqual(Path(read['cwd']).resolve(), f.wt.resolve())
        self.assertEqual(read['source'], (f.wt / 'README.md').read_text())
        args = read['args']
        self.assertEqual(args[args.index('--allowedTools') + 1], core.CLAUDE_READ_ONLY_TOOLS)
        self.assertIn(core.CLAUDE_WRITE_TOOLS, args[args.index('--disallowedTools') + 1])

    def test_source_or_permission_failure_never_retries_another_model(self):
        for error in (ValueError('plan-review-source-modified'), ValueError('plan-review-session-unverified'),
                      ValueError('request-or-plan-change-gate'), PermissionError('permission-required')):
            with self.subTest(error=str(error)), patch.object(project_mode, '_planner_call', side_effect=error) as call, \
                 patch('model_router.chain', return_value=(['m1', 'm2'], [])), self.assertRaises(type(error)):
                project_mode.plan_real(None, 'main')('prompt', 'project-ref-list')
            self.assertEqual(call.call_count, 1)

    def test_real_project_planner_repairs_structure_in_its_own_session_with_full_source(self):
        from test_run_sessions import SessionTests, runner_source
        import qe_core as core
        f = SessionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        runner_source(f.home, f.wt)
        (f.wt / 'README.md').write_text('project source acceptance')
        answer = output(own('f1'))
        f.fake.write_text('#!' + sys.executable + '\n' + 'answer=' + repr(answer) + '\n' + r'''
import json,os,pathlib,sys
a=sys.argv[1:]; p=a[-1]; home=pathlib.Path(os.environ['CADRE_HOME'])
sid=a[a.index('--session')+1] if '--session' in a else 'ses_project_source'
with (home/'project-source-calls.jsonl').open('a') as out:
    out.write(json.dumps({'prompt':p,'cwd':os.getcwd(),'source':pathlib.Path('README.md').read_text(),
                         'session':sid,'config':json.loads(os.environ['OPENCODE_CONFIG_CONTENT'])})+'\n')
print(json.dumps({'type':'step_start','sessionID':sid,'part':{'type':'step-start'}}))
print(json.dumps({'type':'text','sessionID':sid,'part':{'type':'text','text':answer if 'YOUR PREVIOUS ANSWER WAS INVALID' in p else 'invalid list'}}))
''')
        before = (f.wt / 'README.md').read_bytes()
        with patch.dict(os.environ, {'CADRE_REAL_OPENCODE': str(f.fake)}), \
             patch.object(core.ROUTER, 'chain', return_value=(['opencode-go/glm-5.3'], [])):
            result = project_mode.plan_real(f.store, f.rid)(project_mode.list_prompt('Preserve README.md'), 'project-ref-list')
        self.assertEqual(project_mode.feature_list(result)[0]['id'], 'f1')
        calls = [json.loads(line) for line in (f.home / 'project-source-calls.jsonl').read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertEqual(len({call['session'] for call in calls}), 1)
        for call in calls:
            self.assertEqual(Path(call['cwd']).resolve(), f.wt.resolve())
            self.assertEqual(call['source'], before.decode())
            self.assertEqual(call['config']['permission']['edit'], 'deny')
            self.assertNotIn('Do not use any tools', call['prompt'])
        self.assertEqual((f.wt / 'README.md').read_bytes(), before)
        attempts = [json.loads(path.read_text()) for path in f.store.task_dir(f.rid, 'project-ref-list').glob('attempts/*/attempt.json')]
        self.assertEqual(sorted(a['resumed'] for a in attempts), [False, True])
        self.assertTrue(all(a['role'] == 'plan' and not a['product_changed'] for a in attempts))

    def test_zero_budget_project_planning_starts_no_executor(self):
        from test_run_sessions import SessionTests, runner_source
        import qe_core as core
        f = SessionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        runner_source(f.home, f.wt)
        f.store.update(f.rid, budget_cap_usd=0)
        with patch.dict(os.environ, {'CADRE_REAL_OPENCODE': str(f.fake)}), \
             patch.object(core.ROUTER, 'chain', return_value=(['opencode-go/glm-5.3'], [])), self.assertRaises(RuntimeError):
            project_mode.plan_real(f.store, f.rid)('source plan', 'project-ref-list')
        self.assertFalse((f.home / 'calls.jsonl').exists())


class ChoiceTest(unittest.TestCase):
    def test_highest_priority_feature_with_finished_dependencies_is_selected(self):
        list = [own('f1', priority=2), own('f2', ['f1'], priority=1), own('f3', priority=1)]
        self.assertEqual(project_mode.following(list)['id'], 'f3', 'f3 comes first because the f2 dependency is unfinished')
        list[0]['status'] = 'passed'
        self.assertEqual(project_mode.following(list)['id'], 'f2')

    def test_stuck_dependency_blocks_the_dependent_feature(self):
        list = [own('f1', status='stuck'), own('f2', ['f1'])]
        self.assertIsNone(project_mode.following(list))

    def test_no_selection_when_all_are_finished(self):
        self.assertIsNone(project_mode.following([own('f1', status='passed'), own('f2', status='stuck')]))


class StatusTransitionTest(unittest.TestCase):
    def test_success_passed_writes_and_records_the_run(self):
        f = project_mode.apply_outcome(own('f1'), 'r1', True)
        self.assertEqual((f['status'], f['attempt'], f['run']), ('applied', 1, ['r1']))  # B346: passed -> implemented [03.10]

    def test_second_failure_marks_stuck(self):
        f = project_mode.apply_outcome(own('f1'), 'r1', False)
        self.assertEqual((f['status'], f['attempt']), ('waiting', 1))
        f = project_mode.apply_outcome(f, 'r2', False)
        self.assertEqual((f['status'], f['attempt'], f['run']), ('stuck', 2, ['r1', 'r2']))


class StopConditionTest(unittest.TestCase):
    def status(self, list, **extra):
        return {'features': list, 'start': 0.0, 'last_passed': 0.0, 'consecutive_failed': 0, 'final_acceptance_round': 0, **extra}

    def test_continue_if_a_feature_remains_to_work_on(self):
        self.assertEqual(project_mode.stop_decision(self.status([own('f1')]), now_ts=3600), (False, ''))

    def test_all_finished_moves_to_final_acceptance(self):
        self.assertEqual(project_mode.stop_decision(self.status([own('f1', status='passed')]), now_ts=3600), (False, 'final-acceptance'))

    def test_stuck_feature_without_an_acceptance_command_does_not_count_as_passed_by_final_acceptance(self):
        old = {**own('f2', status='stuck'), 'qa_command': ''}
        self.assertEqual(project_mode.stop_decision(self.status([own('f1', status='passed'), old]), now_ts=3600), (True, 'unevidenced-stuck-feature: f2'))

    def test_seven_day_cap_applies_before_final_acceptance(self):
        """B322 [IMPLEMENTED 03.10]: if nothing remains, a time overrun does not block the final acceptance; it stops only if something remains.
        D12 (01.10 Codex, G-P5): when the features were done the final acceptance opened before the 7-day check."""
        day = 86400
        # Nothing remaining → final acceptance ignores the time (B322)
        for list in ([own('f1', status='passed')], [own('f1', status='passed'), own('f2', status='stuck')]):
            with self.subTest(list=[f['status'] for f in list]):
                self.assertEqual(project_mode.stop_decision(self.status(list, last_passed=7 * day), now_ts=7 * day), (True, 'duration-cap'))
                self.assertEqual(project_mode.stop_decision(self.status(list, last_passed=7 * day), now_ts=8 * day + 1), (True, 'duration-cap'))
        # Something remaining → time cap (B322: with pending work the time condition applies)
        self.assertEqual(project_mode.stop_decision(self.status([own('f1')], last_passed=7 * day), now_ts=7 * day), (True, 'duration-cap'))
        self.assertEqual(project_mode.stop_decision(self.status([own('f1', status='passed')], last_passed=7 * day - 1), now_ts=7 * day - 1),
                         (False, 'final-acceptance'))

    def test_three_consecutive_failures_no_progress(self):
        self.assertEqual(project_mode.stop_decision(self.status([own('f1')], consecutive_failed=3), now_ts=3600), (True, 'no-progress'))

    def test_no_progress_unless_a_new_pass_in_twenty_four_hours(self):
        self.assertEqual(project_mode.stop_decision(self.status([own('f1')]), now_ts=DAY + 1), (True, 'no-progress'))

    def test_duration_cap(self):
        d = self.status([own('f1')], last_passed=7 * DAY)
        self.assertEqual(project_mode.stop_decision(d, now_ts=7 * DAY + 1), (True, 'duration-cap'))

    def test_blocked_when_nothing_selectable_but_something_pending(self):
        self.assertEqual(project_mode.stop_decision(self.status([own('f1', status='stuck'), own('f2', ['f1'])]), now_ts=3600), (True, 'blocked'))

    def test_final_acceptance_rounds_finished(self):
        d = self.status([own('f1', status='passed')], final_acceptance_round=project_mode.FINAL_ACCEPTANCE_ROUND)
        self.assertEqual(project_mode.stop_decision(d, now_ts=3600), (True, 'final-acceptance-rounds-finished'))


class AgainPlanTest(unittest.TestCase):
    def test_stalled_dependency_is_rejected_by_planner_and_merger_without_rewriting_history(self):
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory(prefix='cadre-replan-') as directory:
            root = Path(directory)
            store = MagicMock()
            store.path.return_value = root
            old = [own('f16', status='passed'), own('f17', status='stuck', attempt=2)]
            path = root/'project_mode.json'
            path.write_text(json.dumps({'features': old}))
            before = path.read_bytes()
            invalid = output(own('f18', ['f17']))
            with self.assertRaisesRegex(ValueError, 'unfinished preserved dependency'):
                project_mode.again_plan_merge(old, invalid)
            calls = []
            def planner(store_, rid, model, prompt, task):
                calls.append((model, prompt))
                return invalid if model == 'm1' else output(own('f18', ['f16']))
            with patch.object(project_mode, '_planner_call', side_effect=planner), \
                    patch('model_router.chain', return_value=(['m1', 'm2'], [])):
                accepted = project_mode.plan_real(store, 'main')('prompt', 'project-ref-again')
            self.assertEqual([c[0] for c in calls], ['m1', 'm1', 'm2'])
            self.assertIn('unfinished preserved dependency', calls[1][1])
            merged = project_mode.again_plan_merge(old, accepted)
            self.assertEqual(merged[:2], old)
            self.assertEqual(project_mode.following(merged)['id'], 'f18')
            self.assertEqual(project_mode.stop_decision({'features': merged, 'start': 100, 'last_passed': 100}, 101), (False, ''))
            self.assertEqual(path.read_bytes(), before)

    def test_explicit_stalled_repair_preserves_criteria_and_has_no_false_dependency(self):
        old = [own('f17', status='stuck', attempt=2)]
        repair = {**own('repair'), 'repair_of': 'f17', 'acceptance': old[0]['acceptance']}
        merged = project_mode.again_plan_merge(old, output(repair))
        self.assertEqual(merged[0], old[0])
        self.assertEqual(project_mode.following(merged)['id'], 'repair')
        for invalid in ({**repair, 'dependent': ['f17']}, {**repair, 'acceptance': 'weaker criterion'},
                        {**repair, 'repair_of': 'missing'}):
            with self.assertRaises(ValueError):
                project_mode.again_plan_merge(old, output(invalid))

    def test_unknown_replan_history_never_calls_paid_planner(self):
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory(prefix='cadre-replan-') as directory:
            store = MagicMock()
            store.path.return_value = Path(directory)
            for content in ('{broken', json.dumps({'features': [{'id': 'f17', 'status': 'unknown'}]})):
                (Path(directory)/'project_mode.json').write_text(content)
                with patch.object(project_mode, '_planner_call') as planner, \
                        patch('model_router.chain', return_value=(['m1'], [])):
                    with self.assertRaisesRegex(ValueError, 'replan-history-unreadable'):
                        project_mode.plan_real(store, 'main')('request', 'project-ref-again')
                planner.assert_not_called()

    def test_passed_feature_is_kept_new_ones_are_added_removed_pending_one_drops(self):
        old = [own('f1', status='passed'), own('f2'), own('f3', status='stuck', attempt=2)]
        old[0]['run'] = ['r1']
        fresh = project_mode.again_plan_merge(old, output({**own('f1'), 'description': 'modified'}, own('f4', ['f1']), own('f3')))
        g = {f['id']: f for f in fresh}
        self.assertEqual(sorted(g), ['f1', 'f3', 'f4'])
        self.assertEqual((g['f1']['status'], g['f1']['description'], g['f1']['run']), ('passed', 'Implement f1', ['r1']), 'a passed feature is untouched')
        self.assertEqual((g['f3']['status'], g['f3']['attempt']), ('stuck', 2), 'a stuck feature’s history is preserved')
        self.assertEqual(g['f4']['status'], 'waiting')

    def test_new_list_cannot_delete_a_passed_feature(self):
        fresh = project_mode.again_plan_merge([own('f1', status='passed'), own('f2')], output(own('f2')))
        self.assertIn('f1', [f['id'] for f in fresh])


class ReportTest(unittest.TestCase):
    def test_report_numbers_contain_the_table_and_the_reason(self):
        d = {'id': 'p1', 'request_summary': 'small tool', 'features': [own('f1', status='passed'), own('f2', status='stuck'), own('f3')],
             'status': 'stopped', 'reason': 'no-progress', 'intermediate_branch': 'cadre/project-ref-p1', 'spend_usd': 1.25}
        text = project_mode.report(d)
        for part in ('p1', 'small tool', '1 passed', '1 stuck', '1 waiting', 'cadre/project-ref-p1', 'no-progress', '| f2 |', '$1.25'):
            self.assertIn(part, text)



class StepVerificationTests(unittest.TestCase):
    def setUp(self):
        from run_store import RunStore, atomic_json, plan_digest
        self.temp = tempfile.TemporaryDirectory(prefix='project-step-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.project = self.home / 'projects/lab'
        self.project.mkdir(parents=True)
        self.policy = self.project / 'supervision.json'
        atomic_json(self.policy, {'version': 1, 'mode': 'step'})
        self.store = RunStore(self.home)
        self.parent = self.store.accept('lab', 'Complete the backlog in sequence.')['run_id']
        self.child = self.store.accept('child', 'Complete the first feature.', parent_run_id=self.parent)['run_id']
        final = {'status': 'PASS', 'revision': {'digest': 'selected-child-product'}}
        self.store.update(self.child, state='completed', final_result=final)
        state = project_mode._fresh_status(self.parent, 'Complete the backlog in sequence.', 1000)
        state.update(features=[own('f1', status='applied'), own('f2', ['f1'])], checkpoint={
            'id': 'a' * 32, 'run_id': self.child, 'feature': 'f1', 'status': 'waiting',
            'success': True, 'final_digest': plan_digest(final)})
        self.state_path = self.store.path(self.parent) / 'project_mode.json'
        atomic_json(self.state_path, state)
        self.store.update(self.parent, state='retryable', reason='project-ref-stopped: project-step-verification-required')
        self.enter(patch.dict(os.environ, {'CADRE_RUN_ID': '', 'CADRE_NO_NET': '1'}))

    def enter(self, manager):
        result = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        return result

    def test_project_policy_is_frozen_and_inherited_and_bad_policy_stops_admission(self):
        from run_store import atomic_json
        self.policy.unlink()
        for rid in (self.parent, self.child):
            self.assertEqual(self.store.read(rid)['supervision'], {'version': 1, 'mode': 'step'})
            with self.assertRaisesRegex(ValueError, 'immutable'):
                self.store.update(rid, supervision=None)
        self.assertIsNone(self.store.accept('lab', 'New independent task.')['supervision'])
        for value in ({'version': True, 'mode': 'step'}, {'version': 1, 'mode': 'automatic'}, []):
            atomic_json(self.policy, value)
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'supervision-policy-invalid'):
                self.store.accept('lab', 'Invalid policy must not start.')

    def test_project_feature_cannot_combine_two_bound_backlog_cards(self):
        from run_store import atomic_json
        plan = {'tasks': [{'id': 't1', 'acceptance': 'First acceptance'}, {'id': 't2', 'acceptance': 'Second acceptance'}],
                'source_items': [{'id': item, 'source_sha256': 'a' * 64, 'evidence': '', 'tasks': [task]}
                                 for item, task in (('B001', 't1'), ('B002', 't2'))]}
        atomic_json(self.store.path(self.parent) / 'plan.json', plan)
        with self.assertRaisesRegex(ValueError, 'project-step-multiple-source-items'):
            project_mode.project_ref_plan_save(self.store, self.parent, [{**own('mixed'), 'source_tasks': ['t1', 't2']}])

    def test_checkpoint_blocks_manual_execution_revival_and_child_launch(self):
        from run_store import execute_run, launch_guard, revive
        with patch('run_store.subprocess.Popen') as spawn, patch('run_store._start') as restart:
            self.assertEqual(execute_run(self.store, self.parent, proceed=True), 78)
            self.assertEqual(revive(self.store, LIB.parent), [])
            spawn.assert_not_called()
            restart.assert_not_called()
        for rid in (self.parent, self.child):
            with (self.subTest(rid=rid), self.assertRaisesRegex(ValueError, 'project-step-verification-required'),
                  launch_guard(self.store, rid)):
                self.fail('held project admitted a new operation')

    def test_supervised_roots_wait_for_explicit_start_instead_of_automatic_start_or_retry(self):
        import time

        from run_store import ACCEPT_STALE_SEC, atomic_json, revive
        for n, prompt in enumerate(('ROUND: review\nInspect sources', 'ROUND: report\nWrite a report', 'Implement one small feature')):
            project = 'root' + str(n)
            path = self.home / 'projects' / project
            path.mkdir(parents=True)
            atomic_json(path / 'supervision.json', project_mode.STEP_POLICY)
            rid = self.store.accept(project, prompt)['run_id']
            for state in ('accepted', 'queued', 'retryable'):
                self.store.update(rid, state=state, reason='first-attempt-needs-review')
                record = self.store.read(rid)
                record['created'] = time.time() - ACCEPT_STALE_SEC - 10
                atomic_json(self.store.path(rid) / 'run.json', record)
                with (self.subTest(kind=n, state=state), patch('run_store._start') as launch,
                      patch.object(self.store, 'order_came', return_value=True)):
                    self.assertEqual(revive(self.store, LIB.parent), [])
                    launch.assert_not_called()
                    after = self.store.read(rid)
                    for key in ('start_count', 'retry_count', 'created', 'budget_cap_usd'):
                        self.assertEqual(after.get(key), record.get(key))
            self.store.update(rid, state='cancelled')
        current = self.store.accept('root0', 'ROUND: review\nRecover the current interrupted job')['run_id']
        self.store.update(current, state='running')
        with patch('run_store._start') as launch, patch.object(self.store, 'order_came', return_value=True):
            revive(self.store, LIB.parent)
            self.assertEqual(launch.call_args.args[1], current)
            launch.assert_called_once()

    def test_automatic_queue_handoff_skips_supervised_jobs_and_preserves_the_default(self):
        from run_store import queue_start
        for n, supervised in enumerate((True, False)):
            project = 'queue' + str(n)
            if supervised:
                from run_store import atomic_json
                path = self.home / 'projects' / project
                path.mkdir(parents=True)
                atomic_json(path / 'supervision.json', project_mode.STEP_POLICY)
            rid = self.store.accept(project, 'ROUND: review\nInspect the next source')['run_id']
            self.store.update(rid, state='queued')
            with self.subTest(supervised=supervised), patch('run_store._start') as launch, patch('run_store.has_active', return_value=False):
                self.assertEqual(queue_start(self.store, project, LIB.parent), None if supervised else rid)
                self.assertEqual(launch.call_count, 0 if supervised else 1)
            self.store.update(rid, state='cancelled')

    def test_missing_project_state_or_checkpoint_cannot_reopen_a_finished_step(self):
        from run_store import atomic_json, launch_guard, revive
        original = json.loads(self.state_path.read_text())
        for missing in ('file', 'checkpoint', 'empty', 'corrupt'):
            if missing == 'file':
                self.state_path.unlink()
            elif missing == 'empty':
                atomic_json(self.state_path, {})
            elif missing == 'corrupt':
                self.state_path.write_text('{broken')
            else:
                atomic_json(self.state_path, {k: v for k, v in original.items() if k != 'checkpoint'})
            with self.subTest(missing=missing):
                with self.assertRaisesRegex(ValueError, 'supervision-checkpoint-invalid'), launch_guard(self.store, self.parent):
                    self.fail('missing checkpoint admitted an operation')
                with patch('run_store._start') as restart:
                    self.assertEqual(revive(self.store, LIB.parent), [])
                    restart.assert_not_called()

    def test_explicit_release_runs_only_the_next_feature_and_waits_again(self):
        from run_store import atomic_json, plan_digest
        with patch('outcome.approved', return_value=True):
            project_mode.verify_step(self.store, self.parent, 'a' * 32, note='First feature independently verified.')
        child = self.store.accept('child2', 'Complete the second feature.', parent_run_id=self.parent)
        self.store.update(child['run_id'], state='completed', final_result={'status': 'PASS', 'revision': {'digest': 'second-product'}})
        child = self.store.read(child['run_id'])
        calls = []
        def run_child(prompt, branch):
            calls.append(prompt)
            return child
        self.assertEqual(project_mode.execute(self.store, self.parent, plan=lambda *_: self.fail('unexpected replan'),
                                    child_execute=run_child, merge=lambda *_: True, now_ts=lambda: 1001), 78)
        state = json.loads(self.state_path.read_text())
        self.assertEqual(len(calls), 1)
        self.assertIn('feature f2:', calls[0])
        self.assertEqual((state['checkpoint']['feature'], state['checkpoint']['status']), ('f2', 'waiting'))
        self.assertEqual(state['checkpoint']['final_digest'], plan_digest(child['final_result']))
        self.assertNotEqual(state['checkpoint']['id'], 'a' * 32)
        self.assertEqual([f['status'] for f in state['features']], ['applied', 'applied'])
        self.assertTrue(project_mode.supervision_waiting(self.store, self.store.read(self.parent)))
        state.pop('checkpoint')
        atomic_json(self.state_path, state)
        with self.assertRaisesRegex(ValueError, 'supervision-checkpoint-invalid'):
            project_mode.supervision_waiting(self.store, self.store.read(self.parent))

    def test_release_is_bound_idempotent_and_does_not_renew_budget_or_deadline(self):
        before = self.store.read(self.parent)
        with patch('outcome.approved', return_value=True):
            verified = project_mode.verify_step(self.store, self.parent, 'a' * 32, note='Independent product and cost checks passed.')
            self.assertEqual(verified['status'], 'verified')
            self.assertEqual(project_mode.verify_step(self.store, self.parent, 'a' * 32), verified)
        after = self.store.read(self.parent)
        for key in ('created', 'budget_cap_usd', 'operator_budget_usd', 'budget_allocation', 'review_limits'):
            self.assertEqual(after[key], before[key])
        self.assertFalse(project_mode.supervision_waiting(self.store, after))
        self.assertEqual(after['reason'], 'project-step-resume-required')
        self.assertEqual(len(after['operator_interventions']), 1)
        with patch('run_store._start') as restart:
            self.assertEqual(__import__('run_store').revive(self.store, LIB.parent), [])
            restart.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'project-step-verification-required'), __import__('run_store').launch_guard(self.store, self.child):
            self.fail('child resumed before explicit parent start')

    def test_stale_missing_acceptance_changed_result_and_worker_release_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'stale-checkpoint'):
            project_mode.verify_step(self.store, self.parent, 'b' * 32)
        with patch('outcome.approved', return_value=False), self.assertRaisesRegex(ValueError, 'acceptance-required'):
            project_mode.verify_step(self.store, self.parent, 'a' * 32)
        self.store.update(self.child, final_result={'status': 'PASS', 'revision': {'digest': 'changed-product'}})
        with patch('outcome.approved', return_value=True), self.assertRaisesRegex(ValueError, 'result-changed'):
            project_mode.verify_step(self.store, self.parent, 'a' * 32)
        with patch.dict(os.environ, {'CADRE_RUN_ID': self.child}), self.assertRaisesRegex(ValueError, 'operator-command-required'):
            project_mode.verify_step(self.store, self.parent, 'a' * 32)
        self.assertTrue(project_mode.supervision_waiting(self.store, self.store.read(self.parent)))

    def test_active_owner_cannot_be_released(self):
        from run_store import FileLock
        with FileLock(self.store.path(self.parent) / '.owner.lock'), self.assertRaisesRegex(ValueError, 'owner-active'):
            project_mode.verify_step(self.store, self.parent, 'a' * 32)

    def test_unsettled_call_blocks_success_release_while_future_guarantee_is_preserved(self):
        from budget_gate import BudgetLedger
        ledger = BudgetLedger(self.home / 'chain/request-budget.db')
        with ledger.transaction() as con:
            con.execute('insert into reservation(id,provider,period,created,reserved,metadata) values(?,?,?,?,?,?)',
                        ('call', 'openrouter', 'test', 1000, 5000, json.dumps({'run_id': self.child})))
            con.execute('insert into reservation(id,provider,period,created,reserved,metadata) values(?,?,?,?,?,?)',
                        ('future-final', 'openrouter', 'test', 1000, 10000, json.dumps({'run_id': self.parent, 'assurance': True})))
        with patch('outcome.approved', return_value=True):
            with self.assertRaisesRegex(ValueError, 'unsettled-cost'):
                project_mode.verify_step(self.store, self.parent, 'a' * 32)
            ledger.settle('call', 5000, 'known-receipt')
            self.assertEqual(project_mode.verify_step(self.store, self.parent, 'a' * 32)['status'], 'verified')
        with ledger.transaction() as con:
            self.assertIsNone(con.execute("select actual from reservation where id='future-final'").fetchone()[0])

    def test_failed_step_needs_an_explicit_retry_record_and_keeps_the_same_feature(self):
        from run_store import atomic_json, plan_digest
        final = {'status': 'FAIL', 'reason': 'review-not-passed'}
        self.store.update(self.child, state='failed', final_result=final)
        state = json.loads(self.state_path.read_text())
        state['checkpoint'].update(success=False, final_digest=plan_digest(final))
        state['features'][0]['status'] = 'waiting'
        atomic_json(self.state_path, state)
        with self.assertRaisesRegex(ValueError, 'retry-evidence-required'):
            project_mode.verify_step(self.store, self.parent, 'a' * 32, 'retry')
        with self.assertRaisesRegex(ValueError, 'acceptance-required'):
            project_mode.verify_step(self.store, self.parent, 'a' * 32)
        result = project_mode.verify_step(self.store, self.parent, 'a' * 32, 'retry', 'Fixed the recorded parser failure; targeted regression passed.')
        self.assertEqual(result['status'], 'retry-approved')
        self.assertEqual(json.loads(self.state_path.read_text())['retry_feature'], 'f1')


class FakeStore:
    """The face of RunStore the project loop uses: record, path, update."""
    def __init__(self, root, request):
        self.root = Path(root)
        (self.root / 'runs' / 'main').mkdir(parents=True)
        (self.root / 'runs' / 'main' / 'prompt.md').write_text(request)
        self.records = {'main': {'run_id': 'main', 'prompt': str(self.root / 'runs' / 'main' / 'prompt.md'), 'source_sha': 'abc'}}

    def path(self, rid):
        return self.root / 'runs' / rid

    def read(self, rid):
        return dict(self.records[rid])

    def update(self, rid, **d):
        self.records[rid].update(d)
        return dict(self.records[rid])


class ExecuteTest(unittest.TestCase):
    """The project loop with a fake planner / child run / merge: selection, state transitions, re-plan, final acceptance, report."""
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory(prefix='project-ref-')
        self.addCleanup(self.tmp.cleanup)
        self.store = FakeStore(self.tmp.name, 'Write a small note tool from scratch')
        self.plans = []
        self.children = []
        self.hour = [1000.0]

    def plan(self, *lists):
        order = list(lists)
        def f(prompt, task_id):
            self.plans.append((task_id, prompt))
            return output(*order.pop(0)) if order else 'plan absent'
        return f

    def child(self, outcomes):
        """results: feature id (or 'final-acceptance') -> list of success bools (consumed in order)."""
        def f(prompt, base_ref):
            key = 'final-acceptance' if prompt.startswith('PROJECT_REF END ACCEPTANCE') else prompt.split('feature ', 1)[1].split(' ', 1)[0].rstrip(':')
            success = outcomes[key].pop(0)
            rid = f'c{len(self.children) + 1}'
            self.children.append((key, base_ref))
            self.hour[0] += 600
            return {'run_id': rid, 'state': 'completed' if success else 'failed', 'worktree': f'/wt/{rid}',
                    'final_result': {'status': 'PASS' if success else 'FAIL', 'reason': None if success else 'review-not-passed'},
                    'budget_spent_usd': 0.5}
        return f

    def execute(self, plan, child):
        return project_mode.execute(self.store, 'main', plan=plan, child_execute=child, merge=lambda branch, k: k['state'] == 'completed',
                           now_ts=lambda: self.hour[0])

    def status(self):
        return json.loads((self.store.path('main') / 'project_mode.json').read_text())

    def test_supervised_project_stops_after_one_feature_and_restart_keeps_the_gate(self):
        self.store.update('main', supervision={'version': 1, 'mode': 'step'})
        child = self.child({'f1': [True], 'f2': [True], 'final-acceptance': [True]})
        rc = self.execute(self.plan([own('f1', priority=1), own('f2', ['f1'])]), child)
        self.assertEqual(rc, 78)
        self.assertEqual([item[0] for item in self.children], ['f1'])
        checkpoint = self.status()['checkpoint']
        self.assertEqual((checkpoint['feature'], checkpoint['status'], checkpoint['success']), ('f1', 'waiting', True))
        self.assertEqual(self.execute(self.plan(), child), 78)
        self.assertEqual([item[0] for item in self.children], ['f1'])
        self.assertEqual(self.status()['checkpoint'], checkpoint)

    def test_supervision_stops_at_the_first_failed_attempt_without_retry_or_replanning(self):
        self.store.update('main', supervision={'version': 1, 'mode': 'step'})
        rc = self.execute(self.plan([own('f1')]), self.child({'f1': [False, True], 'final-acceptance': [True]}))
        self.assertEqual(rc, 78)
        self.assertEqual([item[0] for item in self.children], ['f1'])
        self.assertFalse(self.status()['checkpoint']['success'])
        self.assertEqual(len(self.plans), 1)

    def test_supervision_requires_verification_before_the_whole_project_final_review(self):
        self.store.update('main', supervision={'version': 1, 'mode': 'step'})
        rc = self.execute(self.plan([own('f1')]), self.child({'f1': [True], 'final-acceptance': [True]}))
        self.assertEqual(rc, 78)
        self.assertEqual([item[0] for item in self.children], ['f1'])
        self.assertNotEqual(self.status()['status'], 'complete')

    def test_supervision_rejects_a_cached_feature_combining_backlog_cards_before_execution(self):
        from run_store import atomic_json
        self.store.update('main', supervision=dict(project_mode.STEP_POLICY))
        state = project_mode._fresh_status('main', 'Complete the backlog.', self.hour[0])
        state['features'] = [{**own('mixed'), 'source_items': [{'id': 'B001'}, {'id': 'B002'}]}]
        atomic_json(self.store.path('main') / 'project_mode.json', state)
        with self.assertRaisesRegex(ValueError, 'project-step-multiple-source-items'):
            self.execute(self.plan(), self.child({'mixed': [True]}))
        self.assertEqual(self.children, [])
        self.assertEqual(self.plans, [])

    def test_project_completes_if_three_features_and_the_final_acceptance_pass(self):
        rc = self.execute(self.plan([own('f1', priority=1), own('f2', ['f1']), own('f3', ['f1'])]),
                        self.child({'f1': [True], 'f2': [True], 'f3': [True], 'final-acceptance': [True]}))
        d = self.status()
        self.assertEqual((rc, d['status'], d['reason']), (0, 'complete', 'final-acceptance-passed'))
        self.assertEqual([c[0] for c in self.children], ['f1', 'f2', 'f3', 'final-acceptance'])
        self.assertTrue(all(c[1] == d['intermediate_branch'] for c in self.children), 'every child starts from the intermediate branch tip')
        self.assertTrue(d['intermediate_branch'].startswith('cadre/project-ref-'))
        self.assertEqual(self.store.read('main')['final_result']['status'], 'PASS', 'the final acceptance child’s evidence passes to the parent run')
        self.assertEqual(self.store.read('main')['worktree'], '/wt/c4')
        self.assertIn('3 passed', (self.store.path('main') / 'project-ref-report.md').read_text())
        self.assertAlmostEqual(d['spend_usd'], 2.0)

    def test_feature_that_falls_twice_gets_stuck_the_others_continue(self):
        rc = self.execute(self.plan([own('f1', priority=1), own('f2')], [own('f2')]),
                        self.child({'f1': [False, False], 'f2': [True], 'final-acceptance': [True]}))
        d = self.status()
        g = {f['id']: f for f in d['features']}
        self.assertEqual((g['f1']['status'], g['f1'].get('last_acceptance'), g['f1']['attempt'], g['f2']['status']), ('passed', True, 2, 'passed'))
        self.assertEqual(rc, 0)
        self.assertEqual([p[0] for p in self.plans], ['project-ref-list'])
        self.assertEqual(d['plan_evaluations'][-1]['decision'], 'unchanged')
        self.assertIn('f1', d['plan_evaluations'][-1]['evidence']['stalled'])

    def test_three_failures_in_a_row_stop_the_project(self):
        rc = self.execute(self.plan([own('f1', priority=1), own('f2', priority=2), own('f3', priority=3)]),
                        self.child({'f1': [False, False], 'f2': [False, False], 'f3': [False, False]}))
        d = self.status()
        self.assertEqual((rc, d['status'], d['reason']), (1, 'stopped', 'no-progress'))
        self.assertEqual(len(self.children), 3)

    def test_if_final_acceptance_falls_a_correction_feature_is_added_and_passes_in_the_second_round(self):
        rc = self.execute(self.plan([own('f1')], [own('f1'), own('d1', ['f1'])]),
                        self.child({'f1': [True], 'd1': [True], 'final-acceptance': [False, True]}))
        d = self.status()
        self.assertEqual((rc, d['status'], d['final_acceptance_round']), (0, 'complete', 1))
        self.assertEqual([c[0] for c in self.children], ['f1', 'final-acceptance', 'd1', 'final-acceptance'])

    def test_continue_from_the_recorded_state_list_is_not_regenerated(self):
        (self.store.path('main') / 'project_mode.json').write_text(json.dumps({
            'id': 'x', 'request_summary': 'tool', 'intermediate_branch': 'cadre/project-ref-x', 'start': 1000.0, 'last_passed': 1000.0,
            'consecutive_failed': 0, 'final_acceptance_round': 0, 'status': 'running', 'reason': '', 'spend_usd': 0.0, 'finished_count': 1,
            'features': [own('f1', status='passed'), own('f2', ['f1'])]}))
        rc = self.execute(self.plan(), self.child({'f2': [True], 'final-acceptance': [True]}))
        self.assertEqual(rc, 0)
        self.assertEqual([c[0] for c in self.children], ['f2', 'final-acceptance'])
        self.assertEqual(self.plans, [], 'the planner is not called for a continuing project')

    def test_cancel_flag_stops_the_project(self):
        (self.store.path('main') / 'cancel.flag').write_text('')
        rc = self.execute(self.plan([own('f1')]), self.child({}))
        self.assertEqual((rc, self.status()['reason']), (1, 'cancel'))
        self.assertEqual(self.children, [])

    def test_invalid_feature_list_does_not_start_the_project(self):
        rc = self.execute(self.plan(), self.child({}))
        self.assertEqual((rc, self.status()['reason']), (1, 'list-could-not-produce'))

    def test_stuck_feature_is_mandatory_in_final_acceptance_and_the_project_is_complete_only_if_it_passes(self):
        prompts = []
        normal = self.child({'f1': [False, False], 'f2': [True], 'final-acceptance': [True]})
        def child(prompt, branch):
            prompts.append(prompt)
            return normal(prompt, branch)
        f1 = {**own('f1', priority=1), 'qa_command': 'python3 -m pytest -q tests/test_f1.py'}
        rc = self.execute(self.plan([f1, own('f2')], [own('f2')]), child)
        last = prompts[-1]
        self.assertTrue(last.startswith('PROJECT_REF END ACCEPTANCE'))
        for part in ('STUCK FEATURES', 'f1 test passes', 'tests/test_f1.py'):
            self.assertIn(part, last)
        d = self.status()
        f = {g['id']: g for g in d['features']}['f1']
        self.assertEqual((rc, d['status'], f['status'], f.get('last_acceptance')), (0, 'complete', 'passed', True))
        self.assertIn('(at final acceptance)', (self.store.path('main') / 'project-ref-report.md').read_text())

    def test_the_end_of_a_long_request_reaches_the_planner_the_replan_and_the_final_acceptance(self):
        path = self.store.records['main']['prompt']
        Path(path).write_text('Write a tool from scratch\n' + 'x' * 20000 + '\nMANDATORY-END-ITEM: export\n')
        prompts = []
        normal = self.child({'f1': [True], 'final-acceptance': [False, True], 'd1': [True]})
        def child(prompt, branch):
            prompts.append(prompt)
            return normal(prompt, branch)
        rc = self.execute(self.plan([own('f1')], [own('f1'), own('d1', ['f1'])]), child)
        self.assertEqual(rc, 0)
        self.assertEqual([g for g, _ in self.plans], ['project-ref-list', 'project-ref-again'])
        self.assertTrue(all('MANDATORY-END-ITEM' in p for _, p in self.plans))
        self.assertIn('MANDATORY-END-ITEM', prompts[-1])
        feature = prompts[0]
        self.assertNotIn('MANDATORY-END-ITEM', feature)
        self.assertIn(f'the full request is {len(Path(path).read_text())} characters: {path}', feature)

    def test_big_extra_document_does_not_stop_the_project(self):
        rc = project_mode.execute(self.store, 'main', plan=self.plan([own('f1')]), child_execute=self.child({'f1': [True], 'final-acceptance': [True]}),
                         merge=lambda branch, k: k['state'] == 'completed', now_ts=lambda: self.hour[0], extras='x' * (project_mode.PLAN_REQUEST_CAP + 1))
        self.assertEqual(rc, 0)

    def test_request_exceeding_the_cap_is_not_trimmed_the_project_stops_with_a_reason(self):
        Path(self.store.records['main']['prompt']).write_text('x' * (project_mode.PLAN_REQUEST_CAP + 1))
        rc = self.execute(self.plan([own('f1')]), self.child({}))
        self.assertEqual(rc, 1)
        self.assertTrue(self.status()['reason'].startswith('request-many-long'))
        self.assertEqual((self.plans, self.children), ([], []))



def git(repo, *args):
    import subprocess
    return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=f', '-c', 'user.email=f@x', *args],
                          check=True, capture_output=True, text=True, timeout=30).stdout.strip()


_MERGE = project_mode.merge_ff


def allowed_merge(repo, branch, child, *, store=None, run_id=''):
    from run_store import RunStore
    if store is None:
        store = RunStore(Path(repo).parent / 'operator-fixture')
        run_id = store.accept('git-fixture', 'transfer fixture')['run_id']
    try:
        return _MERGE(repo, branch, child, store=store, run_id=run_id)
    except PermissionError as exc:
        if str(exc) != 'git-transfer-permission-required':
            raise
    action = store.read(run_id)['project_git_action']
    first = project_mode.git_transfer_approval(store, run_id, action['token'])
    project_mode.git_transfer_approval(store, run_id, action['token'], first['confirmation'])
    return _MERGE(repo, branch, child, store=store, run_id=run_id)


class ProjectRefGitTest(unittest.TestCase):
    """G-P3 intermediate branch: only cadre/project-ref-* is written; the child product fast-forwards (CAS); the canonical branch and a dirty product are rejected."""
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory(prefix='project-ref-git-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '-b', 'main')
        (self.repo / 'a.txt').write_text('v1\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'base')
        self.base = git(self.repo, 'rev-parse', 'HEAD')

    def child_branch(self, entry_name, start, content, dirty=False):
        wt = self.root / entry_name
        git(self.repo, 'worktree', 'add', '-q', '-b', f'cadre/{entry_name}', str(wt), start)
        (wt / 'a.txt').write_text(content)
        git(wt, 'commit', '-qam', entry_name)
        if dirty:
            (wt / 'a.txt').write_text(content + 'uncommitted\n')
        return {'run_id': entry_name, 'state': 'completed', 'worktree': str(wt)}

    def test_intermediate_branch_opens_from_the_accepted_source_and_canonical_branch_is_rejected(self):
        project_mode.intermediate_branch_prepare(self.repo, 'cadre/project-ref-x', self.base)
        self.assertEqual(git(self.repo, 'rev-parse', 'cadre/project-ref-x'), self.base)
        project_mode.intermediate_branch_prepare(self.repo, 'cadre/project-ref-x', 'HEAD')   # an existing branch stays in place
        with patch.object(project_mode, 'acceptance_image', side_effect=AssertionError('existing branch must not request a snapshot')):
            project_mode.intermediate_branch_prepare(self.repo, 'cadre/project-ref-x', self.base, {'carried': True, 'patch': 'carried.patch'})
        self.assertEqual(git(self.repo, 'rev-parse', 'cadre/project-ref-x'), self.base)
        for bad in ('main', 'feature/x', 'cadre/other'):
            with self.subTest(bad), self.assertRaises(ValueError):
                project_mode.intermediate_branch_prepare(self.repo, bad, self.base)

    def test_request_extras_accept_only_documents_not_sensitive_or_banned_ones(self):
        for entry_name, content in (('.env', 'FAKE_SECRET=1\n'), ('BACKLOG.md', '- item-1\n'), ('README.md', 'content-r\n'),
                           ('secrets.md', 'content-s\n'), ('config.json', '{"x": 1}\n')):
            (self.repo / entry_name).write_text(content)
        git(self.repo, 'add', '-f', '.')
        git(self.repo, 'commit', '-qm', 'documents')
        extra = project_mode.request_extras(self.repo, 'main', "Apply BACKLOG.md items. Do not read .env or README.md; "
                                                   "use secrets.md and config.json")
        self.assertIn('item-1', extra)
        for absent in ('FAKE_SECRET', 'content-r', 'content-s', '"x"', 'v1'):
            self.assertNotIn(absent, extra)
        (self.repo / 'NOTES.md').write_text('content-n\n')
        (self.repo / 'HANDOVER.md').write_text('content-d\n')
        (self.repo / 'BIG.md').write_text('B' * (project_mode.EXTRA_FILE_CAP + 5000) + 'END-LINE\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'notes')
        extra = project_mode.request_extras(self.repo, 'main', "Read NOTES.md first. Do not read HANDOVER.md. Apply the password "
                                                   "change item in BIG.md.")
        self.assertIn('content-n', extra)
        self.assertNotIn('content-d', extra)
        self.assertNotIn('END-LINE', extra)
        self.assertIn(f'the full file is {len((self.repo / "BIG.md").read_text())} characters: BIG.md', extra)

    def test_child_product_merges_fast_forward(self):
        project_mode.intermediate_branch_prepare(self.repo, 'cadre/project-ref-x', self.base)
        k = self.child_branch('c1', 'cadre/project-ref-x', 'feature 1\n')
        self.assertTrue(allowed_merge(self.repo, 'cadre/project-ref-x', k))
        self.assertEqual(git(self.repo, 'show', 'cadre/project-ref-x:a.txt'), 'feature 1')
        self.assertEqual(git(self.repo, 'rev-parse', 'main'), self.base, 'the canonical branch is untouched')

    def test_harness_surplus_is_not_dirty_but_an_uncommitted_product_counts(self):
        """B234 (25.09 live B203b): the child finished PASS but the merge was rejected because the engine's `.tasks.json` leftover stayed in the worktree;
        the main run counted f1 as failed and restarted the same feature (an endless loop). The dirt measure is the same as the engine's (PRODUCT_EXCLUDED)."""
        project_mode.intermediate_branch_prepare(self.repo, 'cadre/project-ref-x', self.base)
        k = self.child_branch('c1', 'cadre/project-ref-x', 'feature 1\n')
        wt = Path(k['worktree'])
        (wt / '.tasks.json').write_text('[]')
        (wt / '__pycache__').mkdir(); (wt / '__pycache__' / 'a.pyc').write_bytes(b'x')
        (wt / '.cadre-plan').mkdir(); (wt / '.cadre-plan' / 'briefing.md').write_text('x')
        self.assertTrue(allowed_merge(self.repo, 'cadre/project-ref-x', k), 'harness artifacts are not product code')
        self.assertEqual(git(self.repo, 'show', 'cadre/project-ref-x:a.txt'), 'feature 1')
        k2 = self.child_branch('c2', 'cadre/project-ref-x', 'feature 2\n')
        (Path(k2['worktree']) / 'fresh.py').write_text('print(1)\n')   # an uncommitted REAL product: not merged
        with self.assertRaisesRegex(PermissionError, 'commit-permission-required'):
            allowed_merge(self.repo, 'cadre/project-ref-x', k2)

    def test_intermediate_branch_does_not_merge_if_advanced_or_product_dirty(self):
        project_mode.intermediate_branch_prepare(self.repo, 'cadre/project-ref-x', self.base)
        old = self.child_branch('c1', 'cadre/project-ref-x', 'old\n')
        fresh = self.child_branch('c2', 'cadre/project-ref-x', 'fresh\n')
        self.assertTrue(allowed_merge(self.repo, 'cadre/project-ref-x', fresh))
        self.assertFalse(allowed_merge(self.repo, 'cadre/project-ref-x', old), 'a product not based on the intermediate branch tip is not merged')
        dirty = self.child_branch('c3', 'cadre/project-ref-x', 'dirty\n', dirty=True)
        with self.assertRaisesRegex(PermissionError, 'commit-permission-required'):
            allowed_merge(self.repo, 'cadre/project-ref-x', dirty)
        self.assertFalse(allowed_merge(self.repo, 'cadre/project-ref-x', {**fresh, 'state': 'failed'}))

    def test_derived_project_settings_copies(self):
        home = self.root / 'qe'
        main = home / 'projects' / 'p'
        main.mkdir(parents=True)
        for entry_name, content in (('repo-path', str(self.repo)), ('base-branch', 'main'), ('qa-command', 'make check'), ('POLICY.md', 'rule'),
                           ('run-cap-usd', '5'), ('run-cap-hard-usd', '10'), ('format-command', 'ruff format')):
            (main / entry_name).write_text(content)
        entry_name = project_mode.derive_project_ref(home, 'p', 'ab12')
        self.assertEqual(entry_name, 'p--pab12')
        self.assertEqual((home / 'projects' / entry_name / 'qa-command').read_text(), 'make check')
        self.assertEqual((home / 'projects' / entry_name / 'repo-path').read_text(), str(self.repo))
        # B272 (30.09 rehearsal): the hard cap and the format command are inherited too; otherwise a child run falls to the default $5 and the format repair is lost
        for config_name, content in (('run-cap-usd', '5'), ('run-cap-hard-usd', '10'), ('format-command', 'ruff format')):
            self.assertEqual((home / 'projects' / entry_name / config_name).read_text(), content, config_name)

    def test_transfer_requires_two_operator_cli_messages_and_never_reuses_an_approval(self):
        import os
        import subprocess
        from run_store import RunStore
        store = RunStore(self.root / 'gate')
        rid = store.accept('fixture', 'transfer')['run_id']
        project_mode.intermediate_branch_prepare(self.repo, 'cadre/project-ref-x', self.base)
        child = self.child_branch('c1', 'cadre/project-ref-x', 'accepted\n')
        with self.assertRaisesRegex(PermissionError, 'git-transfer-permission-required'):
            project_mode.merge_ff(self.repo, 'cadre/project-ref-x', child, store=store, run_id=rid)
        action = store.read(rid)['project_git_action']
        self.assertEqual(action['command'], ['git', '-C', str(self.repo), 'update-ref', 'refs/heads/cadre/project-ref-x',
                                           git(Path(child['worktree']), 'rev-parse', 'HEAD'), self.base])
        env = {k: v for k, v in os.environ.items() if not k.startswith(('CADRE_', 'OPENCODE_'))}
        env['CADRE_HOME'] = str(store.home)
        command = [sys.executable, str(LIB / 'run_store.py'), 'project-git-approve', rid, action['token']]
        denied = subprocess.run(command, env={**env, 'CADRE_RUN_ID': rid}, capture_output=True, text=True, timeout=10)
        self.assertEqual(denied.returncode, 78)
        first = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(first.returncode, 0, first.stderr)
        confirmation = json.loads(first.stdout)['confirmation']
        with self.assertRaisesRegex(PermissionError, 'git-transfer-permission-required'):
            project_mode.merge_ff(self.repo, 'cadre/project-ref-x', child, store=store, run_id=rid)
        for suffix in ([], ['wrong']):
            denied = subprocess.run(command + suffix, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(denied.returncode, 78)
        self.assertEqual(git(self.repo, 'rev-parse', 'cadre/project-ref-x'), self.base)
        second = subprocess.run(command + [confirmation], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(project_mode.merge_ff(self.repo, 'cadre/project-ref-x', child, store=RunStore(store.home), run_id=rid))
        self.assertEqual(store.read(rid)['project_git_action']['status'], 'consumed')
        another = self.child_branch('c2', 'cadre/project-ref-x', 'next\n')
        with self.assertRaisesRegex(PermissionError, 'git-transfer-permission-required'):
            project_mode.merge_ff(self.repo, 'cadre/project-ref-x', another, store=store, run_id=rid)
        denied = subprocess.run(command + [confirmation], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(denied.returncode, 78)

    def test_transfer_race_and_interrupted_consumption_never_run_the_command_twice(self):
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        from run_store import RunStore
        store = RunStore(self.root / 'race')
        rid = store.accept('fixture', 'transfer')['run_id']
        ref = 'refs/heads/cadre/project-ref-x'
        project_mode.intermediate_branch_prepare(self.repo, 'cadre/project-ref-x', self.base)
        child = self.child_branch('c1', 'cadre/project-ref-x', 'next\n')
        new = git(child['worktree'], 'rev-parse', 'HEAD')
        with self.assertRaises(PermissionError):
            project_mode.git_transfer(store, rid, self.repo, ref, new, self.base)
        action = store.read(rid)['project_git_action']
        first = project_mode.git_transfer_approval(store, rid, action['token'])
        project_mode.git_transfer_approval(store, rid, action['token'], first['confirmation'])
        with patch('run_store.atomic_json', side_effect=OSError('disk')), patch.object(project_mode, '_git') as command:
            with self.assertRaises(OSError):
                project_mode.git_transfer(store, rid, self.repo, ref, new, self.base)
            command.assert_not_called()
        self.assertEqual(store.read(rid)['project_git_action']['status'], 'approved')
        with patch.object(project_mode, '_git', side_effect=OSError('interrupted')) as command:
            with self.assertRaises(OSError):
                project_mode.git_transfer(store, rid, self.repo, ref, new, self.base)
            self.assertEqual(command.call_count, 1)
        self.assertEqual(store.read(rid)['project_git_action']['status'], 'consumed')
        with self.assertRaises(PermissionError):
            project_mode.git_transfer(store, rid, self.repo, ref, new, self.base)
        action = store.read(rid)['project_git_action']
        first = project_mode.git_transfer_approval(store, rid, action['token'])
        project_mode.git_transfer_approval(store, rid, action['token'], first['confirmation'])
        def transfer():
            try:
                return project_mode.git_transfer(RunStore(store.home), rid, self.repo, ref, new, self.base)
            except PermissionError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(lambda _: transfer(), range(2))), [False, True])
        self.assertEqual(git(self.repo, 'rev-parse', ref), new)

    def test_stale_plan_and_terminal_actions_cannot_use_the_second_confirmation(self):
        from run_store import RunStore
        store = RunStore(self.root / 'stale')
        rid = store.accept('fixture', 'transfer')['run_id']
        project_mode.intermediate_branch_prepare(self.repo, 'cadre/project-ref-x', self.base)
        child = self.child_branch('c1', 'cadre/project-ref-x', 'next\n')
        with self.assertRaises(PermissionError):
            project_mode.merge_ff(self.repo, 'cadre/project-ref-x', child, store=store, run_id=rid)
        action = store.read(rid)['project_git_action']
        first = project_mode.git_transfer_approval(store, rid, action['token'])
        store.save_plan(rid, {'job_id': rid, 'kind': 'project', 'tasks': [], 'project_features': [own('f1')]})
        with self.assertRaisesRegex(ValueError, 'stale-git-action'):
            project_mode.git_transfer_approval(store, rid, action['token'], first['confirmation'])
        for state in ('cancelled', 'failed', 'completed'):
            store.update(rid, state=state)
            with self.assertRaises(ValueError):
                project_mode.git_transfer_approval(store, rid, action['token'], first['confirmation'])
        self.assertEqual(git(self.repo, 'rev-parse', 'cadre/project-ref-x'), self.base)



class RevivalTest(unittest.TestCase):
    """G-P4: the main project run continues the child run; the guard does not revive it independently, and closes the child if the main one finished."""
    def test_child_is_not_revived_independently_it_closes_when_the_main_ends(self):
        import tempfile
        from run_store import FileLock, RunStore, revive
        tmp = tempfile.TemporaryDirectory(prefix='project-ref-revive-')
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        store = RunStore(home)
        root = home / 'root'
        (root / 'bin').mkdir(parents=True)
        (root / 'bin' / 'cadre-dispatch.sh').write_text('echo dispatch >> "$CADRE_RUN_DIR/call"\nexit 1\n')
        main = store.accept('p', 'main')
        store.update(main['run_id'], state='running', phase='dispatch')
        child = store.accept('p--pab', 'child')
        store.update(child['run_id'], state='running', phase='dispatch', owner_pid=999999, project_ref_parent=main['run_id'])
        with FileLock(store.path(main['run_id']) / '.owner.lock'):   # the main run's coordinator is alive
            self.assertEqual(revive(store, root), [])
        self.assertEqual(store.read(child['run_id'])['state'], 'running')
        self.assertFalse((store.path(child['run_id']) / 'call').exists())
        store.update(main['run_id'], state='failed')
        revive(store, root)
        last = store.read(child['run_id'])
        self.assertEqual((last['state'], last.get('reason')), ('cancelled', 'project-ref-parent-finished'))



class SuitableTest(unittest.TestCase):
    def test_project_mode_requires_ultra_and_planner_approval(self):
        self.assertTrue(project_mode.suitable({'scope': {'level': 'ultra', 'planner': 90}}))
        self.assertFalse(project_mode.suitable({'scope': {'level': 'ultra', 'planner': 20}}), 'a keyword alone (rule 90) is insufficient')
        self.assertFalse(project_mode.suitable({'scope': {'level': 'ultra', 'planner': None}}))
        self.assertFalse(project_mode.suitable({'scope': {'level': 'very-hard', 'planner': 80}}))
        self.assertFalse(project_mode.suitable({'scope': {'level': 'ultra', 'planner': 95}, 'project_ref_parent': 'x'}))

    def test_supervision_uses_project_steps_for_small_code_tasks_only(self):
        record = {'supervision': dict(project_mode.STEP_POLICY), 'job_type': 'code',
                  'scope': {'level': 'easy', 'planner': 10}}
        self.assertTrue(project_mode.suitable(record))
        self.assertFalse(project_mode.suitable({**record, 'project_ref_parent': 'parent'}))
        for kind in ('review', 'research'):
            with self.subTest(kind=kind):
                self.assertFalse(project_mode.suitable({**record, 'job_type': kind}))

    def test_supervised_project_planning_preserves_protocol_and_separate_card_instruction(self):
        from unittest.mock import Mock
        store = Mock()
        store.read.return_value = {'supervision': dict(project_mode.STEP_POLICY)}
        prompt = 'PROJECT_REF LIST\nOriginal scope\n' + json.dumps({'features': []})
        with patch('run_store.capture_source_plan', return_value='result') as capture:
            self.assertEqual(project_mode._planner_call(store, 'parent', 'model', prompt, 'project-ref-list'), 'result')
        forwarded = capture.call_args.args[4]
        self.assertEqual(forwarded.splitlines()[0], 'PROJECT_REF LIST')
        self.assertEqual(json.loads(forwarded.splitlines()[-1]), {'features': []})
        self.assertIn('different backlog items', forwarded)
        self.assertIn('operator verification', forwarded)


class StartEndToEndTest(unittest.TestCase):
    """Real RunStore + real git: the intermediate branch, the derived project, child runs open from the intermediate branch, the product fast-forwards,
    the evidence of the final acceptance passes to the main run. Only the child's execution (execute_run) is fake: it opens a worktree and makes a commit."""
    def setUp(self):
        import os
        import tempfile
        from unittest.mock import patch
        self.tmp = tempfile.TemporaryDirectory(prefix='project-ref-e2e-')
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.repo = self.home / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '-b', 'main')
        (self.repo / 'README.md').write_text('empty\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'base')
        pd = self.home / 'projects' / 'p'
        pd.mkdir(parents=True)
        (pd / 'repo-path').write_text(str(self.repo))
        (pd / 'base-branch').write_text('main')
        (pd / 'qa-command').write_text('exit 0')
        environment = patch.dict(os.environ, {'CADRE_HOME': str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        from run_store import RunStore
        self.store = RunStore(self.home)
        self.main = self.store.accept('p', 'Write a small note tool from scratch')['run_id']
        self.running = []
        import qe_core as core
        from test_run_sessions import CATALOG
        from test_end_to_end import FAKE_OPENCODE
        import time
        fake = self.home / 'plan-review-fixture'
        fake.write_text(FAKE_OPENCODE)
        fake.chmod(0o700)
        (self.home / 'models.json').write_text(json.dumps(CATALOG))
        budget = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'today': time.strftime('%Y-%m-%d'),
                  'go': {'db': 'ok', 'pay': 33.333334, 'available': 33.333334, 'window_open': True, 'unit': 'points'}}
        for manager in (patch.object(project_mode, 'merge_ff', side_effect=allowed_merge), patch.dict(os.environ, {'CADRE_REAL_OPENCODE': str(fake), 'CADRE_MODEL_CATALOG': str(self.home / 'models.json'),
                                               'OPENCODE_DB': str(self.home / 'absent.db')}),
                        patch.object(core, 'QE', self.home), patch.object(core.ROUTER, 'QE', self.home),
                        patch.object(core, 'budget_json', return_value=budget), patch.object(core.ROUTER, 'budget_read', return_value=budget),
                        patch.object(core.ROUTER, 'chain', return_value=(['opencode-go/glm-5.3'], ['fixture plan review']))):
            manager.start()
            self.addCleanup(manager.stop)

    def execute(self, failed=()):
        def f(store, cid):
            k = store.read(cid)
            self.running.append(cid)
            prompt = Path(k['prompt']).read_text()
            label = 'final-acceptance' if prompt.startswith('PROJECT_REF END ACCEPTANCE') else prompt.split('feature ', 1)[1].split(':', 1)[0]
            if label in failed:
                store.update(cid, state='retryable', reason='acceptance FAIL')
                return 1
            wt = self.home / 'worktrees' / k['project'] / cid
            wt.parent.mkdir(parents=True, exist_ok=True)
            git(self.repo, 'worktree', 'add', '-q', '-b', f'cadre/{cid}', str(wt), k['source_sha'])
            (wt / f'{label}.txt').write_text(label + '\n')
            git(wt, 'add', '.')
            git(wt, 'commit', '-qm', label)
            store.update(cid, state='completed', worktree=str(wt), final_result={'status': 'PASS', 'label': label}, budget_spent_usd=0.25)
            return 0
        return f

    def plan(self, *lists):
        order = list(lists)
        return lambda prompt, task_id: output(*order.pop(0)) if order else 'absent'

    def test_alarm_approval_waits_and_real_resume_updates_project_and_active_child_views(self):
        import time
        import panel_data as view
        from run_store import approve_spending
        normal = self.execute()
        seen = []
        def alarm(store, cid):
            seen.append(cid)
            store.update(cid, state='retryable', reason='budget-wait', retry_after=time.time() + 60,
                         spend_alarm={'ts': time.time(), 'reason': 'fixture'})
            return 75
        self.assertEqual(project_mode.start(self.main, 'p', plan=self.plan([own('f1')]), execute_fn=alarm), 1)
        self.store.update(self.main, state='retryable')
        before = (self.store.path(self.main) / 'project_mode.json').read_bytes()
        approve_spending(self.store, self.main)
        self.assertEqual((self.store.path(self.main) / 'project_mode.json').read_bytes(), before)
        self.assertNotIn('running', project_mode.status_summary(self.store, 'p'))
        self.store.update(self.main, state='running')
        snapshots = []
        def resume(store, cid):
            parent = store.read(self.main)
            state = json.loads((store.path(self.main) / 'project_mode.json').read_text())
            self.assertEqual((state['status'], state['reason'], parent.get('reason')), ('running', '', None))
            store.update(cid, state='running')
            files = {p: p.read_bytes() for p in store.path(self.main).glob('*.json')}
            tree = view.project_ref_structure(self.home, parent, state)
            final = Path(store.read(cid)['prompt']).read_text().startswith('PROJECT_REF END ACCEPTANCE')
            node = tree[0][1] if final else tree[0][0]['child'][0]
            self.assertIn(cid, str(node['detail']))
            self.assertEqual(node['status'], 'now_ts')
            self.assertIn('running', project_mode.status_summary(store, 'p'))
            self.assertEqual({p:p.read_bytes() for p in files}, files)
            snapshots.append((cid, final))
            return normal(store, cid)
        self.assertEqual(project_mode.start(self.main, 'p', plan=self.plan(), execute_fn=resume), 0)
        self.assertEqual(snapshots[0], (seen[0], False))
        self.assertTrue(snapshots[-1][1])
        state = json.loads((self.store.path(self.main) / 'project_mode.json').read_text())
        self.assertEqual((state['status'], state['reason']), ('complete', 'final-acceptance-passed'))

    def test_terminal_project_entry_never_reopens_or_performs_git_or_calls(self):
        from unittest.mock import Mock
        for status in ('completed', 'implemented', 'failed', 'cancelled'):
            with self.subTest(status=status):
                self.store.update(self.main, state=status)
                before = (self.store.path(self.main) / 'run.json').read_bytes()
                planner, executor = Mock(), Mock()
                with patch.object(project_mode, '_git', side_effect=AssertionError('terminal Git I/O')):
                    rc = project_mode.start(self.main, 'p', plan=planner, execute_fn=executor)
                self.assertEqual(rc, 130 if status == 'cancelled' else 78)
                self.assertEqual((self.store.path(self.main) / 'run.json').read_bytes(), before)
                planner.assert_not_called()
                executor.assert_not_called()
                self.assertFalse((self.store.path(self.main) / 'project_mode.json').exists())

    def test_active_child_pointer_requires_owned_identity_and_never_reads_outside_runs(self):
        import panel_data as view
        self.store.update(self.main, state='running', project_active_feature='f1')
        foreign = self.store.accept('other', 'unrelated')['run_id']
        self.store.update(foreign, state='running', project_ref_parent='other', project_ref_feature='f1')
        outside = self.home / 'outside'
        outside.mkdir()
        (outside / 'run.json').write_text(json.dumps({'run_id':'linked', 'project_ref_parent':self.main, 'project_ref_feature':'f1', 'state':'running'}))
        (self.home / 'runs/linked').symlink_to(outside, target_is_directory=True)
        state = {**project_mode._fresh_status(self.main, 'fixture', 1), 'features':[own('f1')]}
        read = view.record_read
        for pointer in (foreign, '../../outside', 'linked'):
            with self.subTest(pointer=pointer):
                record = self.store.update(self.main, project_active_run=pointer)
                paths = []
                def checked(path):
                    paths.append(Path(path))
                    return read(path)
                with patch.object(view, 'record_read', side_effect=checked):
                    tree = view.project_ref_structure(self.home, record, state)
                node = tree[0][0]['child'][0]
                self.assertEqual(node['status'], 'error')
                self.assertNotIn('now_ts', str(node))
                self.assertTrue(all(path.resolve().is_relative_to(self.home / 'runs') for path in paths))

    def test_project_plan_phases_use_recorded_stop_review_and_resume_without_reader_writes(self):
        import copy
        import panel_data as view
        import qe_core as core
        from run_store import FileLock, review_plan
        def alarm(store, cid):
            store.update(cid, state='retryable', spend_alarm={'ts': 1}, reason='budget-wait')
            return 75
        self.assertEqual(project_mode.start(self.main, 'p', plan=self.plan([own('f1')]), execute_fn=alarm), 1)
        self.store.update(self.main, state='retryable', spend_approval_ts=2)
        revision = self.store.plan_view(self.main)['revision']
        self.store.change_plan(self.main, 'change', revision, 'fix the acceptance description')
        def observe(label):
            record = self.store.read(self.main)
            data = json.loads((self.store.path(self.main) / 'project_mode.json').read_text())
            before = {p:p.read_bytes() for p in self.store.path(self.main).glob('*.json')}
            self.assertIn(label, project_mode.status_summary(self.store, 'p'))
            node = view.task_node(self.home, record)
            self.assertTrue(node['waiting'])
            self.assertIn(label, str(node))
            self.assertEqual({p:p.read_bytes() for p in before}, before)
            return data
        observe('Awaiting stop confirmation')
        child = self.store.read(self.main)['project_active_run']
        with FileLock(self.store.path(child) / '.owner.lock'), self.assertRaisesRegex(ValueError, 'owner-active'):
            self.store.stop_plan(self.main, 'change')
        observe('Awaiting stop confirmation')
        self.store.stop_plan(self.main, 'change')
        observe('Awaiting plan change')
        plan = copy.deepcopy(self.store.plan_view(self.main)['history'][-1]['plan'])
        plan['project_features'][0]['description'] += ' updated'
        self.store.save_plan(self.main, plan, expected=revision)
        observe('Plan review')
        capture = core.capture_opencode
        def reviewing(*args, **kwargs):
            observe('Plan review')
            return capture(*args, **kwargs)
        with patch.object(core, 'capture_opencode', side_effect=reviewing):
            review = review_plan(self.store, self.main, revision + 1, os.environ['CADRE_REAL_OPENCODE'])
        self.assertEqual(review['status'], 'PASS', review)
        self.store.resume_plan(self.main, 'change', revision + 1)
        data = observe('Awaiting approved continuation')
        self.assertEqual(data['status'], 'stopped')
        self.assertEqual(self.store.read(self.main)['state'], 'retryable')

    def test_dirty_work_carried_at_acceptance_is_the_start_of_the_intermediate_branch_and_the_users_tree_is_unchanged(self):
        (self.repo / 'README.md').write_text('dirty line\n')
        (self.repo / 'sub').mkdir()
        (self.repo / 'sub' / 'fresh.py').write_text('print(1)\n')
        git(self.repo, 'add', 'README.md')
        before = {str(p.relative_to(self.repo)): p.read_bytes() for p in self.repo.rglob('*') if p.is_file()}
        main = self.store.accept('p', 'Write a small note tool from scratch')['run_id']
        source = self.store.read(main)['source']
        patch_bytes = Path(source['patch']).read_bytes()
        rc = project_mode.start(main, 'p', plan=self.plan([own('f1')]), execute_fn=self.execute())
        self.assertEqual(rc, 78)
        self.assertEqual(self.running, [])
        self.assertEqual(self.store.read(main)['reason'], 'project-ref-stopped: source-commit-permission-required')
        self.assertEqual(Path(source['patch']).read_bytes(), patch_bytes)
        self.assertEqual((Path(source['untracked']) / 'sub/fresh.py').read_text(), 'print(1)\n')
        after = {str(p.relative_to(self.repo)): p.read_bytes() for p in self.repo.rglob('*') if p.is_file()}
        self.assertEqual(before, after)


    def test_file_named_in_the_request_and_the_first_plan_reach_the_feature_planner(self):
        from run_store import atomic_json
        (self.repo / 'docs').mkdir()
        (self.repo / 'docs' / 'BACKLOG.md').write_text('# Backlog\n- ITEM-X: CSV export\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'backlog')
        main = self.store.accept('p', "Implement the work in BACKLOG.md")['run_id']
        atomic_json(self.store.path(main) / 'plan.json', {'job_id': 'initial', 'scope_rationale': 'big', 'tasks': [
            {'id': 'g1', 'definition': 'INITIAL-PLAN-TASK', 'acceptance': 'k', 'files': ['README.md'],
             'model': 'auto', 'difficulty': 1, 'parallel_group': 0, 'estimate_min': 1}]})
        prompts = []
        plan = self.plan([{**own('f1'), 'source_tasks': ['g1']}])
        def planning(prompt, task_id):
            prompts.append(prompt)
            return plan(prompt, task_id)
        self.assertEqual(project_mode.start(main, 'p', plan=planning, execute_fn=self.execute()), 0)
        for part in ('--- docs/BACKLOG.md ---', 'ITEM-X: CSV export', 'INITIAL-PLAN-TASK'):
            self.assertIn(part, prompts[0])
        self.assertNotIn('--- README.md ---', prompts[0])
        self.assertIn('ITEM-X', Path(self.store.read(self.running[-1])['prompt']).read_text(), 'final acceptance also sees the file')
        self.assertEqual(self.store.plan_view(main)['history'][-1]['plan']['project_features'][0]['source_tasks'], ['g1'])

    def test_permission_wait_preserves_accepted_child_and_resume_does_not_repeat_implementation(self):
        from run_store import RunStore, execute_run, revive
        with patch.object(project_mode, 'merge_ff', side_effect=_MERGE):
            self.assertEqual(project_mode.start(self.main, 'p', plan=self.plan([own('f1')]), execute_fn=self.execute()), 78)
            d = json.loads((self.store.path(self.main) / 'project_mode.json').read_text())
            self.assertEqual((d['reason'], d['finished_count'], d['features'][0]['attempt'], d['final_acceptance_round']),
                             ('git-transfer-permission-required', 0, 0, 0))
            self.assertEqual(len(self.running), 1)
            child = self.store.read(self.running[0])
            self.assertEqual(self.store.environment(child['run_id'])['CADRE_NO_AUTO_COMMIT'], '1')
            product = Path(child['worktree']) / 'f1.txt'
            before = product.read_bytes()
            self.assertEqual(execute_run(self.store, self.main, proceed=True), 78)
            self.assertEqual(revive(self.store, LIB.parent), [])
            self.assertEqual(len(self.running), 1)
            action = self.store.read(self.main)['project_git_action']
            first = project_mode.git_transfer_approval(self.store, self.main, action['token'])
            project_mode.git_transfer_approval(self.store, self.main, action['token'], first['confirmation'])
            self.store = RunStore(self.home)
            self.assertEqual(project_mode.start(self.main, 'p', plan=self.plan(), execute_fn=self.execute()), 78)
            self.assertEqual(self.running.count(child['run_id']), 1)
            self.assertEqual(product.read_bytes(), before)
            d = json.loads((self.store.path(self.main) / 'project_mode.json').read_text())
            self.assertEqual((d['finished_count'], d['features'][0]['status'], d['final_acceptance_round']), (1, 'applied', 0))
            self.assertEqual(d['reason'], 'git-transfer-permission-required')

    def test_operator_committed_source_is_adopted_only_when_it_matches_the_preserved_snapshot(self):
        import shutil
        import subprocess
        from run_store import FileLock
        (self.repo / 'README.md').write_text('accepted dirty source\n')
        (self.repo / 'extra.txt').write_text('accepted untracked source\n')
        git(self.repo, 'add', 'README.md')
        rid = self.store.accept('p', 'Write a small note tool from scratch')['run_id']
        source = self.store.read(rid)['source']
        original_index = (self.repo / '.git/index').read_bytes()
        self.assertEqual(project_mode.start(rid, 'p', plan=self.plan([own('f1')]), execute_fn=self.execute()), 78)
        pending = self.store.read(rid)['project_git_wait']
        with self.assertRaisesRegex(ValueError, 'parent-mismatch'):
            project_mode.git_commit_take_over(self.store, rid, source['sha'])
        wt = self.home / 'operator-source'
        git(self.repo, 'worktree', 'add', '-q', '--detach', str(wt), source['sha'])
        git(wt, 'apply', source['patch'])
        shutil.copytree(source['untracked'], wt, dirs_exist_ok=True)
        git(wt, 'add', '.')
        git(wt, 'commit', '-qm', 'fixture separately authorized accepted source')
        sha = git(wt, 'rev-parse', 'HEAD')
        wrong = git(wt, 'commit-tree', git(wt, 'rev-parse', f"{source['sha']}^{{tree}}"), '-p', source['sha'], '-m', 'fixture wrong source')
        with self.assertRaisesRegex(ValueError, 'content-mismatch'):
            project_mode.git_commit_take_over(self.store, rid, wrong)
        flag = self.store.path(rid) / 'cancel.flag'
        flag.touch()
        with self.assertRaisesRegex(ValueError, 'stale-project-commit'):
            project_mode.git_commit_take_over(self.store, rid, sha)
        flag.unlink()
        with FileLock(self.store.path(rid) / '.owner.lock'), self.assertRaisesRegex(ValueError, 'owner-active'):
            project_mode.git_commit_take_over(self.store, rid, sha)
        env = {k: v for k, v in os.environ.items() if not k.startswith(('CADRE_', 'OPENCODE_'))}
        env['CADRE_HOME'] = str(self.home)
        cmd = [sys.executable, str(LIB / 'run_store.py'), 'project-git-adopt', rid, sha]
        missing = subprocess.run(cmd[:-1] + ['0' * 40], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(missing.returncode, 78)
        denied = subprocess.run(cmd, env={**env, 'CADRE_RUN_ID': rid}, capture_output=True, text=True, timeout=10)
        self.assertEqual(denied.returncode, 78)
        adopted = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(adopted.returncode, 0, adopted.stderr)
        self.assertEqual(json.loads(adopted.stdout)['tree'], pending['tree'])
        before = {str(p.relative_to(self.repo)): p.read_bytes() for p in (self.repo / '.git').rglob('*') if p.is_file()}
        self.assertEqual(project_mode.git_commit_take_over(self.store, rid, sha)['adopted_sha'], sha)
        self.assertEqual(before, {str(p.relative_to(self.repo)): p.read_bytes() for p in (self.repo / '.git').rglob('*') if p.is_file()})
        self.assertEqual(project_mode.start(rid, 'p', plan=self.plan([own('f1')]), execute_fn=self.execute()), 0)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), source['sha'])
        self.assertEqual((self.repo / '.git/index').read_bytes(), original_index)
        self.assertEqual((self.repo / 'extra.txt').read_text(), 'accepted untracked source\n')
        self.assertEqual((self.store.path(rid) / 'baseline-source/extra.txt').read_text(), 'accepted untracked source\n')

    def test_operator_product_commit_reanchors_identical_qa_and_resumes_the_same_child_after_partial_record_failure(self):
        import qe_core as core
        from qa_fixture import command
        from outcome import implemented
        normal = self.execute()
        def worker(store, cid):
            k = store.read(cid)
            if not k.get('feature_acceptance'):
                return normal(store, cid)
            self.running.append(cid)
            wt = self.home / 'worktrees' / k['project'] / cid
            wt.parent.mkdir(parents=True, exist_ok=True)
            git(self.repo, 'worktree', 'add', '-q', '-b', f'cadre/{cid}', str(wt), k['source_sha'])
            (wt / 'f1.txt').write_text('reviewed dirty product\n')
            store.update(cid, worktree=str(wt))
            with patch.dict(os.environ, store.environment(cid)):
                qa = core.qa_gate(k['project'], str(wt), self.home / 'adopt-qa.log', '/forbidden/provider', model='claude/fixture')
            self.assertEqual(qa['status'], 'PASS', qa)
            result = {'status': 'APPLIED', 'revision': qa['revision'], 'qa': qa, 'risky': False,
                      'review': {'status': 'PASS', 'revision': qa['revision'], 'independent': True}}
            store.update(cid, state='implemented', final_result=result)
            self.assertTrue(implemented(store.read(cid)))
            return 0
        with patch.object(project_mode, 'merge_ff', side_effect=_MERGE):
            self.assertEqual(project_mode.start(self.main, 'p', plan=self.plan([{**own('f1'), 'qa_command': command('test -f f1.txt')}]), execute_fn=worker), 78)
            cid = self.running[0]
            child = self.store.read(cid)
            old_result = child['final_result']
            wt = Path(child['worktree'])
            git(wt, 'add', 'f1.txt')
            git(wt, 'commit', '-qm', 'fixture separately authorized reviewed product')
            sha = git(wt, 'rev-parse', 'HEAD')
            self.assertFalse(implemented(self.store.read(cid)))
            from run_store import FileLock
            with FileLock(self.store.path(cid) / '.owner.lock'), self.assertRaisesRegex(ValueError, 'owner-active'):
                project_mode.git_commit_take_over(self.store, self.main, sha)
            product = wt / 'f1.txt'
            content = product.read_bytes()
            product.write_text('changed after review\n')
            with self.assertRaisesRegex(ValueError, 'content-mismatch'):
                project_mode.git_commit_take_over(self.store, self.main, sha)
            product.write_bytes(content)
            self.store.update(cid, final_result={**old_result, 'status': 'FAIL'})
            with self.assertRaisesRegex(ValueError, 'evidence-mismatch'):
                project_mode.git_commit_take_over(self.store, self.main, sha)
            self.store.update(cid, final_result=old_result)
            for field in ('project_git_wait', 'project_source_commit', 'project_deadline'):
                with self.assertRaisesRegex(ValueError, 'immutable'):
                    self.store.update(self.main, **{field: 'changed'})
            before_git = {str(p.relative_to(self.repo)): p.read_bytes() for p in (self.repo / '.git').rglob('*') if p.is_file()}
            import run_store
            write = run_store.atomic_json
            def failing(path, value):
                if path == self.store.path(self.main) / 'run.json':
                    raise OSError('fixture parent record disk failure')
                return write(path, value)
            with patch('run_store.atomic_json', side_effect=failing), self.assertRaises(OSError):
                project_mode.git_commit_take_over(self.store, self.main, sha)
            self.assertTrue(implemented(self.store.read(cid)))
            self.assertEqual(self.store.read(self.main)['reason'], 'project-ref-stopped: commit-permission-required')
            self.assertEqual(project_mode.git_commit_take_over(self.store, self.main, sha)['adopted_sha'], sha)
            after_git = {str(p.relative_to(self.repo)): p.read_bytes() for p in (self.repo / '.git').rglob('*') if p.is_file()}
            self.assertEqual(before_git, after_git, [p for p in before_git.keys() | after_git.keys() if before_git.get(p) != after_git.get(p)])
            self.assertEqual(self.store.read(cid)['final_result']['qa']['attempts'], old_result['qa']['attempts'])
            self.assertEqual(project_mode.start(self.main, 'p', plan=self.plan(), execute_fn=worker), 78)
            action = self.store.read(self.main)['project_git_action']
            first = project_mode.git_transfer_approval(self.store, self.main, action['token'])
            project_mode.git_transfer_approval(self.store, self.main, action['token'], first['confirmation'])
            self.assertEqual(project_mode.start(self.main, 'p', plan=self.plan(), execute_fn=worker), 78)
            self.assertEqual(self.running.count(cid), 1)
            d = json.loads((self.store.path(self.main) / 'project_mode.json').read_text())
            self.assertEqual((d['finished_count'], d['features'][0]['attempt']), (1, 1))
            self.assertEqual(d['reason'], 'git-transfer-permission-required')

    def test_three_passes_preserve_reviewed_plan_with_a_local_decision_and_no_planner_repeat(self):
        from unittest.mock import Mock
        planner = Mock(side_effect=self.plan([own(f'f{i}') for i in range(1, 5)]))
        self.assertEqual(project_mode.start(self.main, 'p', plan=planner, execute_fn=self.execute()), 0)
        d = json.loads((self.store.path(self.main) / 'project_mode.json').read_text())
        self.assertEqual(planner.call_count, 1)
        self.assertEqual(d['plan_evaluations'][0]['decision'], 'unchanged')
        self.assertEqual(d['plan_evaluations'][0]['evidence']['completed'], 3)
        self.assertEqual(self.store.plan_view(self.main)['revision'], 1)
        self.assertEqual(len(self.store.plan_view(self.main)['reviews']), 1)

    def test_replan_cannot_add_or_drop_selected_acceptance_and_rejected_review_cannot_execute(self):
        self.assertEqual(project_mode.start(self.main, 'p', plan=self.plan([own('f1'), own('f2')]), execute_fn=self.execute()), 0)
        view = self.store.plan_view(self.main)
        self.store.update(self.main, state='running')
        features = json.loads((self.store.path(self.main) / 'project_mode.json').read_text())['features']
        for proposal in ([*features, own('new')], features[:1], [{**features[0], 'acceptance': 'other goal'}, features[1]]):
            with self.assertRaisesRegex(ValueError, 'project-scope'):
                project_mode.project_ref_plan_save(self.store, self.main, proposal)
            self.assertEqual(self.store.plan_view(self.main)['revision'], view['revision'])
        changed = [{**features[0], 'qa_command': 'test -e updated'}, features[1]]
        with patch('run_store.review_plan', return_value={'status': 'FAIL'}):
            with self.assertRaisesRegex(ValueError, 'current-plan-review-required'):
                project_mode.project_ref_plan_save(self.store, self.main, changed)
        next_view = self.store.plan_view(self.main)
        self.assertEqual(next_view['revision'], 2)
        self.assertTrue(next_view['held'])
        from run_store import execute_run
        before = list(self.running)
        self.assertEqual(execute_run(self.store, self.main, proceed=True), 78)
        self.assertEqual(self.running, before)
        try:
            project_mode.project_ref_plan_save(self.store, self.main, changed)
        except ValueError as error:
            reviews = self.store.read(self.main).get('plan_reviews', [])
            review = reviews[-1] if reviews else {}
            error.add_note(f"Last plan review: revision={review.get('revision_number')}, "
                           f"status={review.get('status')}, reason={review.get('reason')}")
            raise
        accepted = self.store.plan_view(self.main)
        self.assertEqual(accepted['revision'], 2)
        self.assertFalse(accepted['held'])
        self.assertEqual(accepted['reviews'][-1]['status'], 'PASS')
        self.assertEqual(accepted['reviews'][-1]['revision_number'], 2)

    def test_expired_project_cannot_launch_planning_children_or_final_acceptance_after_restart(self):
        from run_store import atomic_json, launch_guard
        from unittest.mock import Mock
        state = project_mode._fresh_status(self.main, 'request', 0)
        state['features'] = [own('f1', status='applied')]
        atomic_json(self.store.path(self.main) / 'project_mode.json', state)
        planner, child = Mock(), Mock()
        for _ in range(2):
            self.assertNotEqual(project_mode.execute(self.store, self.main, plan=planner, child_execute=child,
                                           merge=Mock()), 0)
            self.assertEqual(json.loads((self.store.path(self.main) / 'project_mode.json').read_text())['reason'], 'duration-cap')
        planner.assert_not_called()
        child.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'project-time-limit'):
            with launch_guard(self.store, self.main):
                self.fail('expired parent launched')
        cid = self.store.accept('fixture-child', 'child', parent_run_id=self.main)['run_id']
        with self.assertRaisesRegex(ValueError, 'project-time-limit'):
            with launch_guard(self.store, cid):
                self.fail('expired child launched')

    def test_final_acceptance_repair_keeps_original_criteria_and_requires_a_reviewed_revision(self):
        initial = own('f1')
        initial['acceptance'] = 'the same selected product acceptance criterion is preserved'
        repair = {**own('repair', ['f1']), 'repair_of': 'f1', 'acceptance': initial['acceptance']}
        normal = self.execute()
        finals = []
        def child(store, cid):
            if Path(store.read(cid)['prompt']).read_text().startswith('PROJECT_REF END ACCEPTANCE'):
                finals.append(cid)
                if len(finals) == 1:
                    store.update(cid, state='failed', final_result={'status': 'FAIL', 'reason': 'missing-output'})
                    return 1
            return normal(store, cid)
        self.assertEqual(project_mode.start(self.main, 'p', plan=self.plan([initial], [initial, repair]), execute_fn=child), 0)
        view = self.store.plan_view(self.main)
        self.assertEqual(view['revision'], 2)
        self.assertEqual(view['reviews'][-1]['status'], 'PASS')
        state = json.loads((self.store.path(self.main) / 'project_mode.json').read_text())
        self.assertEqual([f['id'] for f in state['features']], ['f1', 'repair'])
        self.assertEqual({f['acceptance'] for f in state['features']}, {initial['acceptance']})
        self.assertEqual(state['final_acceptance_round'], 1)

    def test_final_acceptance_child_also_runs_the_acceptance_command_of_the_stuck_feature_in_qa(self):
        f1 = {**own('f1', priority=1), 'qa_command': 'python3 -m pytest -q tests/test_f1.py'}
        rc = project_mode.start(self.main, 'p', plan=self.plan([f1, own('f2')], [own('f2')]), execute_fn=self.execute(failed=('f1',)))
        last = self.store.read(self.running[-1])
        self.assertTrue(Path(last['prompt']).read_text().startswith('PROJECT_REF END ACCEPTANCE'))
        self.assertEqual(last.get('extra_acceptance_commands'), ['python3 -m pytest -q tests/test_f1.py'])
        self.assertEqual(rc, 0)

    def test_project_accumulates_on_the_intermediate_branch_and_the_final_acceptance_evidence_passes_to_the_main_run(self):
        rc = project_mode.start(self.main, 'p', plan=self.plan([own('f1', priority=1), own('f2', ['f1'])]), execute_fn=self.execute())
        self.assertEqual(rc, 0)
        main = self.store.read(self.main)
        branch = main['project_ref']['intermediate_branch']
        self.assertEqual(git(self.repo, 'log', '--format=%s', branch).splitlines()[:3], ['final-acceptance', 'f2', 'f1'])
        self.assertEqual(git(self.repo, 'rev-parse', 'main'), git(self.repo, 'rev-list', '--max-parents=0', 'main'), 'the canonical branch stays in place')
        self.assertEqual(main['final_result'], {'status': 'PASS', 'label': 'final-acceptance'})
        self.assertEqual(Path(main['worktree']).name, self.running[-1])
        children = [self.store.read(c) for c in self.running]
        self.assertTrue(all(c['project_ref_parent'] == self.main and c['project'] == main['project_ref']['child_project_ref'] for c in children))
        self.assertIn('2 passed', (self.store.path(self.main) / 'project-ref-report.md').read_text())

    def test_half_finished_child_closes_as_failed_and_the_project_stops_with_a_permanent_reason(self):
        rc = project_mode.start(self.main, 'p', plan=self.plan([own('f1')]), execute_fn=self.execute(failed=('f1',)))
        self.assertEqual(rc, 1)
        self.assertTrue(all(self.store.read(c)['state'] == 'failed' for c in self.running), 'a retryable child must not block the next child')
        self.assertEqual(self.store.read(self.main)['reason'], 'project-ref-stopped: no-progress')

    def test_child_waiting_for_judge_capacity_is_not_failed_it_waits_and_continues(self):
        """B241 (26.09 live B203b): the child passed the acceptance gate but the judge was waiting for budget ('retryable' + retry_after); the main run counted it
        as 'feature failed' and restarted the same feature from scratch ($1.16 wasted). A waiting child is waited for and the SAME run continues."""
        import time
        normal = self.execute()
        seen = []
        def f(store, cid):
            if cid not in seen and not Path(store.read(cid)['prompt']).read_text().startswith('PROJECT_REF END ACCEPTANCE'):
                seen.append(cid); self.running.append(cid)
                store.update(cid, state='retryable', reason='judge-capacity: retry when shares renew', retry_after=time.time() + 1,
                             final_result={'status': 'FAIL', 'qa': {'status': 'PASS'}, 'review': {'status': 'ERROR', 'reason': 'judge chain exhausted: claude/opus#max: BudgetDenied'}})
                return 1
            return normal(store, cid)
        rc = project_mode.start(self.main, 'p', plan=self.plan([own('f1')]), execute_fn=f)
        self.assertEqual(rc, 0)
        self.assertEqual(self.running[0], self.running[1], 'the pending child continues as the same run without creating a new child')
        d = json.loads((self.store.path(self.main) / 'project_mode.json').read_text())
        self.assertEqual([x['status'] for x in d['features']], ['passed'])
        self.assertEqual(d['consecutive_failed'], 0)

    def test_child_waiting_on_a_spend_alarm_is_not_failed_the_main_run_waits_for_approval(self):
        import time
        def f(store, cid):
            self.running.append(cid)
            store.update(cid, state='retryable', reason='budget-wait', retry_after=time.time() + 60,
                         spend_alarm={'ts': time.time(), 'reason': 'last 10 min'})
            return 75
        rc = project_mode.start(self.main, 'p', plan=self.plan([own('f1')]), execute_fn=f)
        self.assertNotEqual(rc, 0)
        self.assertEqual(len(self.running), 1, 'the child does not run again while awaiting alarm approval')
        d = json.loads((self.store.path(self.main) / 'project_mode.json').read_text())
        self.assertEqual((d['reason'], d['consecutive_failed']), ('spend-alarm', 0))
        self.assertTrue(self.store.read(self.main).get('spend_alarm'))
        self.assertEqual(self.store.read(self.running[0])['state'], 'retryable')

    def test_a_spend_alarm_in_final_acceptance_does_not_consume_a_round_it_waits_for_approval(self):
        import time
        normal = self.execute()
        final_acceptance = []
        def f(store, cid):
            if Path(store.read(cid)['prompt']).read_text().startswith('PROJECT_REF END ACCEPTANCE'):
                final_acceptance.append(cid)
                store.update(cid, state='retryable', reason='budget-wait', retry_after=time.time() + 60,
                             spend_alarm={'ts': time.time(), 'reason': 'last 10 min'})
                return 75
            return normal(store, cid)
        rc = project_mode.start(self.main, 'p', plan=self.plan([own('f1')]), execute_fn=f)
        self.assertNotEqual(rc, 0)
        d = json.loads((self.store.path(self.main) / 'project_mode.json').read_text())
        self.assertEqual((d['reason'], d['final_acceptance_round']), ('spend-alarm', 0))
        self.assertEqual(len(final_acceptance), 1, 'final acceptance does not rerun while awaiting alarm approval')

    def test_retryable_child_without_retry_after_is_tried_once_more(self):
        """B273 (30.09 rehearsal, child 060218/064142): the coordinator left `retryable` with rc!=0 (no retry_after); project mode did not take it for
        a capacity wait and counted the feature as failed after a SINGLE attempt. Normal runs are retried overnight via P4; a child is retried once too."""
        normal = self.execute()
        seen = []
        def f(store, cid):
            if cid not in seen and not Path(store.read(cid)['prompt']).read_text().startswith('PROJECT_REF END ACCEPTANCE'):
                seen.append(cid); self.running.append(cid)
                store.update(cid, state='retryable', reason='rc!=0',
                             final_result={'status': 'FAIL', 'qa': {'status': 'PASS'}, 'review': {'status': 'FAIL', 'reason': 'review-repair-limit'}})
                return 1
            return normal(store, cid)
        rc = project_mode.start(self.main, 'p', plan=self.plan([own('f1')]), execute_fn=f)
        self.assertEqual(rc, 0)
        self.assertEqual(self.running[0], self.running[1], 'the same child run was retried once without creating a new child')
        d = json.loads((self.store.path(self.main) / 'project_mode.json').read_text())
        self.assertEqual([x['status'] for x in d['features']], ['passed'])

    def test_supervised_native_project_does_not_retry_a_failed_child_or_launch_another_after_restart(self):
        from run_store import atomic_json
        self.store.update(self.main, state='cancelled')
        atomic_json(self.home / 'projects/p/supervision.json', {'version': 1, 'mode': 'step'})
        self.main = self.store.accept('p', 'Complete the feature list under supervision.')['run_id']
        worker = self.execute(failed=('f1',))
        planner = self.plan([own('f1', priority=1), own('f2', ['f1'])])
        self.assertEqual(project_mode.start(self.main, 'p', plan=planner, execute_fn=worker), 78)
        self.assertEqual(len(self.running), 1)
        state = json.loads((self.store.path(self.main) / 'project_mode.json').read_text())
        self.assertEqual((state['checkpoint']['feature'], state['checkpoint']['success']), ('f1', False))
        self.assertEqual(project_mode.start(self.main, 'p', plan=planner, execute_fn=worker), 78)
        self.assertEqual(len(self.running), 1)

    def test_cut_child_continues_with_the_same_prompt_without_opening_a_new_run(self):
        from run_store import atomic_json
        plan = self.plan([own('f1')])
        project_mode.start(self.main, 'p', plan=plan, execute_fn=lambda store, cid: 0)   # the child coordinator failed: the record stayed 'accepted'
        first = json.loads((self.store.path(self.main) / 'project-ref-active.json').read_text())['child']
        self.store.update(first, state='running')
        d = json.loads((self.store.path(self.main) / 'project_mode.json').read_text())
        d.update(status='running', reason='', consecutive_failed=0,
                 features=[{**f, 'status': 'waiting', 'attempt': 0, 'run': []} for f in d['features']], children=[])
        atomic_json(self.store.path(self.main) / 'project_mode.json', d)
        project_mode.start(self.main, 'p', plan=plan, execute_fn=self.execute())
        self.assertEqual(self.running[0], first, 'the unfinished child continued without creating a new run')



class ParentRecordCrashTest(unittest.TestCase):
    def test_finished_child_does_not_run_the_same_job_twice_if_cut_before_the_parent_saves(self):
        """D11 (01.10 Codex, G-P4): when the process was cut after the child became terminal but before the parent wrote project_mode.json, a restart opened
        a NEW child with the same prompt (two executions, two costs). A terminal child result the parent has not consumed is returned as is."""
        import tempfile
        from run_store import RunStore, atomic_json
        tmp = tempfile.TemporaryDirectory(prefix='project-ref-crash-')
        self.addCleanup(tmp.cleanup)
        store = RunStore(Path(tmp.name))
        main = store.accept('p', 'main')['run_id']
        call = []
        def execute(st, rid):
            call.append(rid)
            st.update(rid, state='completed', budget_spent_usd=.1)
            return 0
        first = project_mode.child_execute_real(store, main, 'p--x', 'same feature', 'main', execute=execute)
        second = project_mode.child_execute_real(store, main, 'p--x', 'same feature', 'main', execute=execute)
        self.assertEqual((second['run_id'], call), (first['run_id'], [first['run_id']]))
        atomic_json(store.path(main) / 'project_mode.json', {'children': [{'feature': 'f1', 'run': first['run_id']}]})   # the parent consumed it
        third = project_mode.child_execute_real(store, main, 'p--x', 'same feature', 'main', execute=execute)
        self.assertNotEqual(third['run_id'], first['run_id'], 'retrying a consumed result creates a new child')
        self.assertEqual(len(call), 2)


class ProjectDurationCapWaitTest(unittest.TestCase):
    def test_budget_wait_cannot_exceed_the_project_duration(self):
        import time
        from run_store import RunStore, atomic_json
        tmp = tempfile.TemporaryDirectory(prefix='project-ref-duration-')
        self.addCleanup(tmp.cleanup)
        store = RunStore(Path(tmp.name))
        main = store.accept('p', 'main')['run_id']
        atomic_json(store.path(main) / 'project_mode.json', {'start': time.time() - project_mode.DURATION_CAP_SEC - 86400, 'children': []})
        call = []
        def execute(st, rid):
            call.append(rid)
            if len(call) == 1:
                st.update(rid, state='retryable', reason='budget-wait: no allocation', retry_after=time.time() + 1)
            else:
                st.update(rid, state='completed')
            return 0
        with patch.dict(os.environ, {'CADRE_WAIT_THRESHOLD_SEC': '30'}):
            last = project_mode.child_execute_real(store, main, 'p--x', 'feature', 'main', execute=execute)
        self.assertEqual(len(call), 1, 'the child does not run again after the deadline')
        self.assertEqual(last['state'], 'failed')
        self.assertTrue(last['reason'].startswith('project-ref-duration-cap'), last['reason'])


class ProjectRefCostTest(unittest.TestCase):
    def test_project_spend_measured_from_the_main_and_child_ledger_estimated_and_pending_separate(self):
        """D10 (01.10 Codex): the report wrote only the children's total at their end ($20.36); the main planning ($2.75) and reservations closed
        later were missing (ledger $25.52, of which $2.58 was a ghost estimate)."""
        import datetime, tempfile
        import budget_gate as bg
        tmp = tempfile.TemporaryDirectory(prefix='project-ref-expense-')
        self.addCleanup(tmp.cleanup)
        self.enterContext(patch.dict(os.environ, {'CADRE_SPEND_ALARM': '0', 'CADRE_RUN_CAP_USD': '50'}))   # a $25-per-second ledger setup; the alarm is a separate contract
        home = Path(tmp.name)
        now_ts = datetime.datetime.now()
        b = {'ts': now_ts.isoformat(), 'today': now_ts.date().isoformat(),
             'zen': {'db': 'ok', 'pay': 50.0, 'available': 50.0}}
        ledger = bg.BudgetLedger(home / 'chain/request-budget.db')
        def record(rid, usd, receipt=None):
            ident = ledger.reserve('opencode', int(usd * 1e6), b, {'run_id': rid})
            if receipt:
                ledger.settle(ident, int(usd * 1e6), receipt + ident)
        from run_store import RunStore, atomic_json
        store = RunStore(home)
        main = store.accept('p', 'project')['run_id']
        c1 = store.accept('p-child', 'active feature')['run_id']
        c2 = store.accept('p-child', 'cancelled feature')['run_id']
        store.update(main, project_ref={'id': 'x'})
        store.update(c1, project_ref_parent=main, state='running')
        store.update(c2, project_ref_parent=main, state='cancelled')
        atomic_json(store.path(main) / 'project_mode.json', {'id': 'x', 'features': [], 'spend_usd': 0})
        record(main, 2.0, 'measurement:')
        record(c1, 1.0, 'measurement:')
        record(c1, 0.5, 'phantom:')
        record(c2, 0.25)
        record('other', 9.0, 'measurement:')
        m = project_mode.project_ref_cost(home, [main])
        self.assertEqual(m, {'measured_usd': 3.0, 'estimated_usd': 0.5, 'pending_usd': 0.25, 'liability_usd': 3.75})
        self.assertIn('settled $3.00', project_mode.status_summary(store, 'p'))
        self.assertIn('pending parent limit $0.25', project_mode.status_summary(store, 'p'))
        text = project_mode.report({'id': 'x', 'features': [], 'spend_usd': 1.0, 'expense': m})
        self.assertIn('settled $3.00 · estimated $0.50 · pending parent limit $0.25 · liability $3.75', text)

    def test_account_summary_pending_trusted_upper_bound_estimate_separate_from_settled_and_from_remaining_liability(self):
        """N-BU03/N-OBS02 (RCA code28/29): pending showed only the reserve (0.05) on the panel while the trusted upper bound was 0.22;
        the ghost estimate was reported as 'definite spending'. The remainder is subtracted from the total obligation of the three classes."""
        import datetime, tempfile
        import budget_gate as bg
        tmp = tempfile.TemporaryDirectory(prefix='account-summary-')
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        now_ts = datetime.datetime.now()
        b = {'ts': now_ts.isoformat(), 'today': now_ts.date().isoformat(), 'zen': {'db': 'ok', 'pay': 50.0, 'available': 50.0}}
        ledger = bg.BudgetLedger(home / 'chain/request-budget.db')
        from run_store import RunStore
        rid = RunStore(home).accept('p', 'code')['run_id']
        measurement = ledger.reserve('opencode', 60_000, b, {'run_id': rid})
        ledger.settle(measurement, 10_000, 'measurement:' + measurement)
        phantom = ledger.reserve('opencode', 40_000, b, {'run_id': rid})
        ledger.settle(phantom, 40_000, 'phantom:' + phantom)
        recovery = ledger.reserve('opencode', 30_000, b, {'run_id': rid})
        ledger.settle(recovery, 30_000, 'recovery:' + recovery)
        ledger.reserve('opencode', 50_000, b, {'run_id': rid, 'worst': 220_000, 'verified_bound_micro': 220_000})
        m = project_mode.project_ref_cost(home, [rid])
        self.assertEqual(m, {'measured_usd': 0.01, 'estimated_usd': 0.07, 'pending_usd': 0.22, 'liability_usd': 0.3})
        self.assertEqual(project_mode.account_summary(m, 1.0)['remaining_usd'], 0.7)
        line = project_mode._spend_line({'expense': m})
        self.assertIn('settled $0.01', line)
        self.assertIn('estimated $0.07', line)
        self.assertIn('pending parent limit $0.22', line)
        self.assertNotIn('spend $0.08', line)


class StatusSummaryTest(unittest.TestCase):
    """The `cadre status` line: progress of the project's last project run (empty if none)."""
    def test_progress_of_the_last_project_run_in_one_line(self):
        import tempfile
        from run_store import RunStore, atomic_json
        tmp = tempfile.TemporaryDirectory(prefix='project-ref-summary-')
        self.addCleanup(tmp.cleanup)
        store = RunStore(Path(tmp.name))
        self.assertEqual(project_mode.status_summary(store, 'p'), '')
        rid = store.accept('p', 'tool from scratch')['run_id']
        store.update(rid, project_ref={'id': 'ab12', 'intermediate_branch': 'cadre/project-ref-ab12', 'child_project_ref': 'p--pab12'})
        atomic_json(store.path(rid) / 'project_mode.json', {'status': 'running', 'reason': '', 'intermediate_branch': 'cadre/project-ref-ab12', 'spend_usd': 0.75,
                                                    'features': [own('f1', status='passed'), own('f2'), own('f3')]})
        line = project_mode.status_summary(store, 'p')
        for part in ('cadre/project-ref-ab12', '1/3 passed', 'running', '$0.75', 'project-ref-report.md'):
            self.assertIn(part, line)


if __name__ == '__main__':
    unittest.main()
