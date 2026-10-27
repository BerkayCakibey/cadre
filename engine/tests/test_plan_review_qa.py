"""RCA code28/29: in a single-task code job QA runs from the project restriction; the plan reviewer sees the fixed QA configuration explicitly."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))


class FixedQaReviewTests(unittest.TestCase):
    def test_plan_reviewer_sees_the_real_qa_command_in_the_restriction_as_fixed_configuration(self):
        from test_end_to_end import E2EFixture
        fixture = E2EFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        command = [sys.executable, str(fixture.root / 'lib/run_store.py')]

        def cli(*args, data=None):
            return subprocess.run([*command, *args], input=json.dumps(data) if data is not None else None,
                                  text=True, capture_output=True, env=fixture.env, timeout=60)

        raw = dict(submission_id='constant-qa', project='e2e', target_path=str(fixture.repo), target_kind='existing', input_mode='goal',
                   prompt=fixture.PROMPT, output='Working a.py and b.py', acceptance='Both files compile and contain COMPLETE',
                   preference='economic', difficulty_override='easy', duration_minutes='60', budget_usd='2', backlog_path='', source_token='')
        rid = json.loads(cli('request', data=raw).stdout)['run_id']
        proposal = json.loads(cli('request-proposal', rid).stdout)
        first = json.loads(cli('request-approve', rid, data=proposal).stdout)
        self.assertEqual(cli('request-approve', rid, first['confirmation'], data=proposal).returncode, 0)
        self.assertEqual(cli('request-plan', rid).returncode, 0)
        restriction = (fixture.home / 'projects/e2e/qa-command').read_text().strip()
        review = [c for c in fixture.calls() if c['role'] == 'plan-review']
        self.assertEqual(len(review), 1)
        prompt = review[0]['args'][-1]
        question = json.loads(prompt.splitlines()[-1])
        self.assertEqual(question['constant_qa'], {'mode': 'single-task', 'source': 'approved-request', 'command': restriction})
        self.assertIn('constant_qa is the acceptance configuration', prompt)
        self.assertFalse(any('qa_command' in g for g in question['plan']['tasks']), 'No copy into a plan field is expected')
        text = json.dumps(fixture.PROMPT, ensure_ascii=False)
        self.assertEqual(question['original_request'], fixture.PROMPT)
        self.assertNotIn('prompt', question['request'], 'The reviewer receives request text only in original_request')
        self.assertEqual(prompt.count(text), 1, 'Do not carry identical request text twice in the prompt')
        planner = [c for c in fixture.calls() if 'estimate_cap_usd' in c['args'][-1]]
        self.assertEqual(len(planner), 1)
        plan_prompt = planner[0]['args'][-1]
        plan_question = json.JSONDecoder().raw_decode(plan_prompt[plan_prompt.index('{"request":'):])[0]
        self.assertEqual(plan_question['original_request'], fixture.PROMPT)
        self.assertNotIn('prompt', plan_question['request'])
        self.assertEqual(plan_prompt.count(text), 1)


if __name__ == '__main__':
    unittest.main()
