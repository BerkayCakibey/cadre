"""03.10 night audit: behavioural evidence for false success and independence signals."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import qe_core as core
import qa_derive
import budget_gate as budget
from run_store import RunStore


class RepairTests(unittest.TestCase):
    def test_model_identity_matches_across_provider_routes(self):
        for model in ('deepseek-v4-pro', 'deepseek-v4-flash'):
            with self.subTest(model=model):
                self.assertEqual(core.model_id('deepseek/' + model),
                                 core.model_id('openrouter/deepseek/' + model))
        self.assertNotEqual(core.model_id('deepseek/deepseek-v4-pro'),
                            core.model_id('deepseek/deepseek-v4-flash'))

    def test_length_after_tools_is_still_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'events.jsonl'
            proc = SimpleNamespace(attempt=SimpleNamespace(data={'stdout': str(output)}))
            for tools in (0, 1, 33):
                output.write_text('\n'.join(json.dumps({'part': part}) for part in
                                           [{'type': 'tool'}] * tools + [{'type': 'step-finish', 'reason': 'length'}]))
                self.assertTrue(core.empty_finish(proc), tools)
            output.write_text(json.dumps({'part': {'type': 'step-finish', 'reason': 'stop'}}))
            self.assertFalse(core.empty_finish(proc))

    def test_qa_does_not_inherit_another_engine_root_or_run_policy(self):
        leaked = {'CADRE_HOME': '/live', 'CADRE_ENGINE_ROOT': '/live/engine', 'CADRE_TEST_WORKER': '4',
                  'OPENCODE_CONFIG_CONTENT': '{}', 'E2E_MODE': 'live'}
        with patch.dict(os.environ, {**leaked, 'PATH': '/test/path'}):
            env = core.qa_environment()
        self.assertFalse(set(leaked) & set(env))
        self.assertEqual(env['PATH'], '/test/path')

    def test_native_parallel_runner_and_node_tests_are_in_the_same_gate(self):
        repo = Path(__file__).resolve().parents[2]
        command = qa_derive.derive(repo, repo / 'engine')['command']
        self.assertIn('parallel_run.py', command)
        self.assertIn('node --test', command)
        self.assertIn('budget-plugin.test.mjs', command)


    def test_budget_extension_consumes_new_evidence_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            store = RunStore(home)
            with patch.dict(os.environ, {'CADRE_RUN_CAP_USD': '5'}):
                rid = store.accept('fixture', 'task')['run_id']
            store.update(rid, budget_cap_usd=2, budget_plan_cap_usd=2, budget_manual=False)
            budget.progress_write(home, rid, 'qa-pass', 'r1')
            self.assertTrue(budget.cap_extend(home, rid, 'budget_cap_usd'))
            self.assertFalse(budget.cap_extend(home, rid, 'budget_cap_usd'))
            budget.progress_write(home, rid, 'qa-pass', 'r2')
            self.assertTrue(budget.cap_extend(home, rid, 'budget_cap_usd'))
            budget.progress_write(home, rid, 'qa-pass', 'r1')
            self.assertFalse(budget.cap_extend(home, rid, 'budget_cap_usd'))
            self.assertEqual(store.read(rid)['budget_cap_usd'], 4.5)
            parent = store.accept('parent', 'project')['run_id']
            store.update(parent, project_active_run=rid, project_budget_cap_usd=5)
            store.update(rid, project_ref_parent=parent)
            budget.progress_write(home, rid, 'acceptance', 'r3')
            self.assertFalse(budget.cap_extend(home, parent, 'project_budget_cap_usd'))
            self.assertEqual(store.read(parent)['project_budget_cap_usd'], 5)
            self.assertFalse(budget.cap_extend(home, parent, 'project_budget_cap_usd'))

    def test_unreviewed_commit_is_not_budget_progress(self):
        import time
        with patch.object(budget.subprocess, 'run', return_value=SimpleNamespace(stdout=f'new {int(time.time())}')):
            self.assertIsNone(budget.cap_progress(Path('/unused'), {
                'source_sha': 'old', 'worktree': '/unused', 'created': time.time() - 1}))


if __name__ == '__main__':
    unittest.main()
