"""4c-9: the exploration agent and the plan's sub-agent field were removed. A lane hands off to no sub-agent (research via websearch);
a plan that writes `sub_agents` out of old habit is not rejected, the field is dropped."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import dispatch_util as du  # noqa: E402


def plan(sub=None):
    g = {'id': 'g1', 'definition': 'Do one thing', 'acceptance': 'test passes', 'files': ['src/a.py'],
         'scope_outside': [], 'dependent': [], 'difficulty': 2, 'model': 'auto', 'parallel_group': 0,
         'estimate_min': 10, 'estimate_usd': 0.0}
    if sub is not None:
        g['sub_agents'] = sub
    return {'job_id': 'attempt', 'tasks': [g], 'merge_order': ['g1']}


def lane_prompt(p):
    r = subprocess.run([sys.executable, str(LIB / 'dispatch_util.py'), 'task_pf', 'g1'],
                       input=json.dumps(p), capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout


class NoSubAgentTests(unittest.TestCase):
    def test_an_old_sub_agent_field_is_dropped_and_the_lane_never_delegates(self):
        p = plan([{'role': 'scout', 'reason': 'large repo'}])
        du.validate_plan(p, root=None)
        self.assertNotIn('sub_agents', p['tasks'][0])
        prompt = lane_prompt(p)
        self.assertIn('do NOT delegate with task()', prompt)
        self.assertIn('websearch', prompt)
        self.assertNotIn('scout', prompt)

    def test_the_planner_prompt_no_longer_asks_for_sub_agents(self):
        prompt = (LIB.parent / 'bin/cadre-dispatch.sh').read_text()
        for old in ('SUB AGENTS', 'sub_agents', 'CADRE_ALLOWED_SUB_AGENTS', 'allowed-sub'):
            self.assertNotIn(old, prompt)
        self.assertIn('CADRE_SUB_AGENTLESS=1', prompt, 'Lanes and extra lanes start without subagents')

    def test_the_lane_prompt_carries_no_harness_internal_notes(self):
        """B217 (24.09 R6-medium/R6-hard): the internal note in the first line ('# CONSTANT-PREFIX: reduces local prompt-cache eviction') was taken by the model as project
        context and leaked into the product documents as 'local prompt-cache triage'. The fixed prefix stays, its explanation does not enter the prompt."""
        prompt = lane_prompt(plan([]))
        self.assertEqual(prompt.splitlines()[0], '[QE-TASK v1]', 'Preserve the constant cache prefix exactly')
        for inner in ('CONSTANT-PREFIX', 'prompt-cache', 'eviction'):
            self.assertNotIn(inner, prompt)


if __name__ == '__main__':
    unittest.main()
