"""QA-PIN: the approval of a code request carries the QA command as content; plan review, baseline preparation, QA and continue run the same command."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))

WRONG = 'touch WRONG_QA_RAN && exit 3'


class ApprovedQaCommandEndToEndTests(unittest.TestCase):
    def setUp(self):
        from test_end_to_end import E2EFixture, FAKE_OPENCODE
        self.fixture = E2EFixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        gate = "if prompt.startswith('REVIEW PLAN ALIGNMENT WITH THE REQUEST'):"
        self.assertEqual(FAKE_OPENCODE.count(gate), 1)
        extra = ("if role == 'plan-review' and (home/'plan-review-cut').exists():\n"
              "    (home/'plan-review-cut').unlink(); sys.exit(1)\n")
        self.fixture.fake.write_text(FAKE_OPENCODE.replace(gate, extra + gate))
        self.setting = self.fixture.home / 'projects/e2e/qa-command'
        self.approved = self.setting.read_text().strip()
        self.command = [sys.executable, str(self.fixture.root / 'lib/run_store.py')]

    def cli(self, *args, data=None, timeout=60):
        return subprocess.run([*self.command, *args], input=json.dumps(data) if data is not None else None,
                              text=True, capture_output=True, env=self.fixture.env, timeout=timeout)

    def approve(self, entry_name):
        raw = dict(submission_id=entry_name, project='e2e', target_path=str(self.fixture.repo), target_kind='existing', input_mode='goal',
                   prompt=self.fixture.PROMPT, output='Working a.py and b.py', acceptance='Both files compile and contain COMPLETE',
                   preference='economic', difficulty_override='easy', duration_minutes='60', budget_usd='2', backlog_path='', source_token='')
        submitted = self.cli('request', data=raw)
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        rid = json.loads(submitted.stdout)['run_id']
        proposal = self.cli('request-proposal', rid)
        self.assertEqual(proposal.returncode, 0, proposal.stderr)
        proposal = json.loads(proposal.stdout)
        first = self.cli('request-approve', rid, data=proposal)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.cli('request-approve', rid, json.loads(first.stdout)['confirmation'], data=proposal)
        self.assertEqual(second.returncode, 0, second.stderr)
        return rid, proposal

    def run_qa_commands(self, rid):
        checks = self.fixture.store.path(rid) / 'tasks/qa/checks'
        return [json.loads(p.read_text()).get('command') for p in sorted(checks.glob('*/result.json'))]

    def assert_finished_with_approved_command(self, rid):
        saved = self.fixture.store.read(rid)
        self.assertEqual((saved['state'], (saved.get('final_result') or {}).get('status')), ('completed', 'PASS'),
                         (saved.get('reason'), saved.get('qa')))
        self.assertEqual(Path(saved['constraints']['qa-command']).read_text().strip(), self.approved)
        self.assertEqual((saved['qa']['source'], saved['qa']['sha256']), ('approved', hashlib.sha256(self.approved.encode()).hexdigest()))
        base = json.loads((self.fixture.store.path(rid) / 'base-qa.json').read_text())
        self.assertEqual(base['qa']['command'], self.approved, 'Preparation runs the approved command')
        commands = self.run_qa_commands(rid)
        self.assertTrue(commands and all(self.approved in k and 'WRONG' not in k for k in commands), commands)
        self.assertFalse(list(Path(saved['worktree']).rglob('WRONG_QA_RAN')), 'The changed project setting is never executed')

    def test_approval_command_travels_with_content_and_a_later_changed_setting_does_not_pass_from_plan_review_to_preparation_qa(self):
        rid, proposal = self.approve('qa-pin-normal')
        self.assertEqual(proposal['action']['qa_command'],
                         {'source': 'project', 'command': self.approved, 'sha256': hashlib.sha256(self.approved.encode()).hexdigest()})
        from panel_data import request_approval_text
        self.assertIn(self.approved, request_approval_text(rid, proposal), 'The operator sees the command to approve')

        self.setting.write_text(WRONG + '\n')
        self.assertEqual(self.cli('request-plan', rid, timeout=120).returncode, 0)
        review = [c for c in self.fixture.calls() if c['role'] == 'plan-review']
        question = json.loads(review[-1]['args'][-1].splitlines()[-1])
        self.assertEqual(question['constant_qa'], {'mode': 'single-task', 'source': 'approved-request', 'command': self.approved})

        executed = self.cli('run', rid, timeout=150)
        self.assertEqual(executed.returncode, 0, executed.stderr[-1500:])
        self.assert_finished_with_approved_command(rid)

    def test_on_restart_the_pinned_command_stays_a_setting_change_does_not_enter_the_run(self):
        rid, _ = self.approve('qa-pin-proceed')
        (self.fixture.home / 'plan-review-cut').touch()
        first = self.cli('run', rid, timeout=150)
        saved = self.fixture.store.read(rid)
        self.assertNotEqual(first.returncode, 0, saved.get('reason'))
        self.assertNotIn(saved['state'], ('completed', 'failed', 'cancelled'), saved.get('reason'))
        self.assertEqual(Path(saved['constraints']['qa-command']).read_text().strip(), self.approved, 'Pinned together with the source')

        self.setting.write_text(WRONG + '\n')
        proceed = self.cli('run', rid, timeout=150)
        self.assertEqual(proceed.returncode, 0, (proceed.stderr[-1500:], self.fixture.store.read(rid).get('reason')))
        self.assert_finished_with_approved_command(rid)


class RunQaCommandTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        (self.home / 'projects/p').mkdir(parents=True)
        self.setting = self.home / 'projects/p/qa-command'
        self.setting.write_text('pytest -q tests/a\n')
        self.restriction = self.home / 'restriction-qa-command'
        self.restriction.write_text('pytest -q tests/a\n')
        sha = hashlib.sha256(b'pytest -q tests/a').hexdigest()
        self.record = {'project': 'p', 'source_sha': 'abc', 'constraints': {'qa-command': str(self.restriction)},
                      'request_permission': {'status': 'approved', 'action': {
                          'qa_command': {'source': 'project', 'command': 'pytest -q tests/a', 'sha256': sha}}}}

    def test_fixed_restriction_gives_the_approved_command_even_if_the_setting_changes(self):
        from run_store import run_qa_command
        self.setting.write_text('exit 3\n')
        self.assertEqual(run_qa_command(self.home, self.record), ('pytest -q tests/a', 'approved-request'))

    def test_a_changed_or_vanished_restriction_does_not_fall_to_the_project_setting(self):
        from run_store import run_qa_command
        self.restriction.write_text('exit 0\n')
        with self.assertRaisesRegex(ValueError, 'qa-command-changed'):
            run_qa_command(self.home, self.record)
        self.restriction.unlink()
        with self.assertRaisesRegex(ValueError, 'qa-command-changed'):
            run_qa_command(self.home, self.record)
        del self.record['constraints']['qa-command']
        with self.assertRaisesRegex(ValueError, 'qa-command-changed'):
            run_qa_command(self.home, self.record)

    def test_approved_content_is_used_before_pinning(self):
        from run_store import run_qa_command
        self.record.update(source_sha='', constraints={})
        self.setting.write_text('exit 3\n')
        self.assertEqual(run_qa_command(self.home, self.record), ('pytest -q tests/a', 'approved-request'))

    def test_old_record_without_a_qa_command_in_its_approval_keeps_the_previous_path(self):
        from run_store import run_qa_command
        del self.record['request_permission']['action']['qa_command']
        self.restriction.write_text('old restriction\n')
        self.assertEqual(run_qa_command(self.home, self.record), ('old restriction', 'record-restriction'))
        self.record['constraints'] = {}
        self.assertEqual(run_qa_command(self.home, self.record), ('pytest -q tests/a', 'project-ref-setting'))


if __name__ == '__main__':
    unittest.main()
