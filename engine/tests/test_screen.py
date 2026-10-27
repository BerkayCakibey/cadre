"""D14: raw records are optional; state and money come from measurement."""
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import screen_render
import watch
import morning_summary


class ScreenTests(unittest.TestCase):
    def test_summary_does_not_mark_failed_work_as_good_or_unknown_money_as_zero(self):
        text = screen_render.run_summary({'state': 'failed', 'tasks': {'a': {'state': 'produced'}, 'b': {}}})
        self.assertIn('1/2 parts', text)
        self.assertIn('stopped', text)
        self.assertIn('no measurement', text)
        self.assertNotIn('no problems', text)
        self.assertNotIn('$0.00', screen_render.budget_summary({}))

    def test_harness_steps_are_not_counted_as_plan_parts(self):
        """07.10 smoke: a finished 2-task run showed '2/5 parts done' (plan/qa/quality steps were counted as parts)."""
        text = screen_render.run_summary({'state': 'completed', 'budget_spent_usd': 0.68, 'tasks': {
            'plan': {}, 'g1': {'state': 'produced'}, 'g2': {'state': 'produced'}, 'qa': {'qa_result': {}}, 'quality': {},
            'correction-1': {}}})
        self.assertIn('2/2 parts', text)
        self.assertIn('no problems', text)

    def test_an_unapproved_spending_alarm_is_the_status(self):
        """4b (07.10): a spending alarm waits for user approval; the status line shows it and the approval command."""
        record = {'state': 'running', 'run_id': 'r1', 'budget_spent_usd': 1.3, 'tasks': {'g1': {'state': 'running'}},
                 'spend_alarm': {'ts': 20.0, 'reason': "last 10 min $1.30 > 40% of the cap ($1.20)"}, 'spend_approval_ts': 10.0}
        self.assertIn('spend alarm', screen_render.run_summary(record))
        self.assertIn('cadre spend-approval r1', screen_render.run_summary(record))
        self.assertNotIn('spend alarm', screen_render.run_summary({**record, 'spend_approval_ts': 30.0}))

    def test_unicode_wrapping_preserves_text_and_terminal_width(self):
        original = 'Résumé e\u0301 中文 ' * 30
        lines = screen_render.wrap_text(original, 19)
        self.assertEqual(''.join(lines), original)
        self.assertTrue(all(screen_render.span_width(line) <= 19 for line in lines))
        self.assertNotIn('\x1b', screen_render.clean('\x1b[2Jtext\x1b]0;bad\x07'))

    def test_details_replay_without_counting_events_twice(self):
        viewer = watch.Watcher('test', '', plain=True)
        out = io.StringIO()
        event = {'type': 'tool', 'tool': 'bash', 'state': {'status': 'completed',
                 'input': {'command': 'printf internal-command'}, 'output': 'raw-output'}}
        with redirect_stdout(out):
            viewer.part_draw(None, {}, event, '')
            viewer.part_draw(None, {}, {'type': 'reasoning', 'text': 'raw-reasoning'}, '')
        self.assertIn('command run completed', out.getvalue())
        for text in ('internal-command', 'raw-output', 'raw-reasoning'):
            self.assertNotIn(text, out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            viewer.detail_change()
            viewer.detail_change()
        for text in ('internal-command', 'raw-output', 'raw-reasoning'):
            self.assertIn(text, out.getvalue())
        self.assertEqual(viewer.count['tool'], 1)
        self.assertFalse(viewer.detail)

    def test_running_tool_is_not_shown_as_success_even_in_single_frame(self):
        viewer = watch.Watcher('test', '', plain=True, single=True)
        out = io.StringIO()
        with redirect_stdout(out):
            viewer.part_draw(None, {}, {'type': 'tool', 'tool': 'bash', 'state': {'status': 'running'}}, '')
        self.assertIn('in progress', out.getvalue())
        self.assertNotIn('✔', out.getvalue())

    def test_runner_start_is_quiet_and_failure_has_a_human_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'logs').mkdir()
            worktree = str(root / 'worktree')
            log = root / 'logs/job-test-20261004.log'
            log.write_text(f'2026-10-04 10:00:00 run starting RAW worktree: {worktree}\n'
                           '2026-10-04 10:00:01 run fell (rc=9)\n')
            unrelated = root / 'logs/job-test-20261005.log'
            unrelated.write_text('2026-10-05 10:00:00 run starting unrelated worktree\n')
            for runner_log in (str(log), None):
                with self.subTest(runner_log=runner_log), patch.object(watch, 'QE', root):
                    viewer = watch.Watcher('test', worktree, plain=True, runner_log=runner_log)
                    out = io.StringIO()
                    with redirect_stdout(out):
                        viewer.runner_round()
                    self.assertEqual(viewer.runner_log, str(log))
                    self.assertNotIn('RAW', out.getvalue())
                    self.assertNotIn('rc=9', out.getvalue())
                    self.assertNotIn('unrelated', out.getvalue())
                    self.assertIn('job stopped', out.getvalue())
                    with redirect_stdout(out):
                        viewer.detail_change()
                    self.assertIn('RAW', out.getvalue())
            success = root / 'logs/job-success-20261004.log'
            success.write_text('2026-10-04 10:00:00 run finished on its own (rc=0)\n'
                               f'2026-10-04 10:00:01 FINISHED. worktree={worktree}\n')
            with patch.object(watch, 'QE', root), redirect_stdout(io.StringIO()):
                viewer = watch.Watcher('success', worktree, plain=True)
                viewer.runner_round()
                self.assertEqual((viewer.finished, viewer.rc), (True, 0))
                bound = watch.Watcher('success', worktree, plain=True, runner_log=str(success), run_id='recorded')
                bound.runner_round()
                self.assertEqual((bound.finished, bound.rc), (False, None))

    def test_morning_summary_hides_ids_but_keeps_failures_and_openrouter(self):
        record = {'run_id': 'private-id', 'project': 'demo', 'state': 'failed', 'budget_spent_usd': .25}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(morning_summary, 'runs', return_value=[record]), \
                patch.object(morning_summary, 'budget', return_value={'or': {'available': 2}}):
            text = morning_summary.produce(Path(directory))
        self.assertIn('jobs stopped: 1', text)
        self.assertIn('OR $2.00', text)
        self.assertNotIn('private-id', text)
        self.assertNotIn('no problems', text)
        self.assertNotIn('midnight', text)

    def test_stale_budget_is_unknown_and_status_does_not_query_the_network(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'chain').mkdir()
            path = home / 'chain/request-budget.json'
            path.write_text(json.dumps({'observed': time.time() - 90, 'budget': {'go': {}}}))
            self.assertEqual(morning_summary.budget(home), {})
            path.write_text(json.dumps({'observed': time.time(), 'budget': {'go': {'available': 1}}}))
            self.assertEqual(morning_summary.budget(home)['go']['available'], 1)


class OpeningSpeedTests(unittest.TestCase):
    """07.10: opening `cadre` took 5-15 s; the status line ran provider_budget.py synchronously and waited for the network (measured 3-8 s)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name); (self.home / 'chain').mkdir()

    def write(self, age):
        (self.home / 'chain/request-budget.json').write_text(json.dumps(
            {'observed': time.time() - age, 'budget': {'go': {'available': 3}}}))

    def test_an_old_observation_is_shown_with_its_age_and_refreshed_in_the_background(self):
        self.write(300)
        with patch.object(screen_render, 'budget_fresh') as paired, patch.object(screen_render, 'behind_refresh') as back:
            budget, age = screen_render.budget_view(self.home)
        self.assertEqual(budget['go']['available'], 3)
        self.assertGreaterEqual(age, 300)
        paired.assert_not_called(); back.assert_called_once()

    def test_a_fresh_observation_needs_no_refresh_and_no_observation_is_measured_once(self):
        self.write(5)
        with patch.object(screen_render, 'budget_fresh') as paired, patch.object(screen_render, 'behind_refresh') as back:
            screen_render.budget_view(self.home)
        paired.assert_not_called(); back.assert_not_called()
        (self.home / 'chain/request-budget.json').unlink()
        with patch.object(screen_render, 'budget_fresh', return_value={'go': {}}) as paired:
            self.assertEqual(screen_render.budget_view(self.home), ({'go': {}}, 0.0))
        paired.assert_called_once()

    def test_a_run_without_its_worktree_costs_no_git_call(self):
        import outcome
        record = {'state': 'completed', 'worktree': str(self.home / 'deleted')}
        with patch.object(outcome, 'approved', return_value=True) as approval:
            self.assertEqual(screen_render.delivery_pending([record]), 0)
        approval.assert_not_called()
        (self.home / 'deleted').mkdir()
        with patch.object(outcome, 'approved', return_value=True):
            self.assertEqual(screen_render.delivery_pending([record]), 1)


if __name__ == '__main__':
    unittest.main()
