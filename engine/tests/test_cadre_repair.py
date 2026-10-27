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
import qa_turet
import butce_kapisi as budget
from kosu import RunStore


class RepairTests(unittest.TestCase):
    def test_model_identity_matches_across_provider_routes(self):
        for model in ('deepseek-v4-pro', 'deepseek-v4-flash'):
            with self.subTest(model=model):
                self.assertEqual(core.model_kimligi('deepseek/' + model),
                                 core.model_kimligi('openrouter/deepseek/' + model))
        self.assertNotEqual(core.model_kimligi('deepseek/deepseek-v4-pro'),
                            core.model_kimligi('deepseek/deepseek-v4-flash'))

    def test_length_after_tools_is_still_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'events.jsonl'
            proc = SimpleNamespace(attempt=SimpleNamespace(data={'stdout': str(output)}))
            for tools in (0, 1, 33):
                output.write_text('\n'.join(json.dumps({'part': part}) for part in
                                           [{'type': 'tool'}] * tools + [{'type': 'step-finish', 'reason': 'length'}]))
                self.assertTrue(core.bos_bitis(proc), tools)
            output.write_text(json.dumps({'part': {'type': 'step-finish', 'reason': 'stop'}}))
            self.assertFalse(core.bos_bitis(proc))

    def test_qa_does_not_inherit_another_engine_root_or_run_policy(self):
        leaked = {'CADRE_HOME': '/live', 'CADRE_ENGINE_ROOT': '/live/engine', 'CADRE_TEST_ISCI': '4',
                  'OPENCODE_CONFIG_CONTENT': '{}', 'E2E_MODE': 'live'}
        with patch.dict(os.environ, {**leaked, 'PATH': '/test/path'}):
            env = core.qa_ortami()
        self.assertFalse(set(leaked) & set(env))
        self.assertEqual(env['PATH'], '/test/path')

    def test_native_parallel_runner_and_node_tests_are_in_the_same_gate(self):
        repo = Path(__file__).resolve().parents[2]
        command = qa_turet.turet(repo, repo / 'engine')['command']
        self.assertIn('paralel_kos.py', command)
        self.assertIn('node --test', command)
        self.assertIn('butce-plugin.test.mjs', command)


    def test_budget_extension_consumes_new_evidence_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            store = RunStore(home)
            with patch.dict(os.environ, {'CADRE_KOSU_TAVAN_USD': '5'}):
                rid = store.accept('fixture', 'task')['run_id']
            store.update(rid, budget_cap_usd=2, budget_plan_cap_usd=2, budget_manual=False)
            budget.ilerleme_yaz(home, rid, 'qa-pass', 'r1')
            self.assertTrue(budget.tavan_uzat(home, rid, 'budget_cap_usd'))
            self.assertFalse(budget.tavan_uzat(home, rid, 'budget_cap_usd'))
            budget.ilerleme_yaz(home, rid, 'qa-pass', 'r2')
            self.assertTrue(budget.tavan_uzat(home, rid, 'budget_cap_usd'))
            budget.ilerleme_yaz(home, rid, 'qa-pass', 'r1')
            self.assertFalse(budget.tavan_uzat(home, rid, 'budget_cap_usd'))
            self.assertEqual(store.read(rid)['budget_cap_usd'], 4.5)
            parent = store.accept('parent', 'project')['run_id']
            store.update(parent, project_active_run=rid, project_budget_cap_usd=5)
            store.update(rid, proje_ust=parent)
            budget.ilerleme_yaz(home, rid, 'kabul', 'r3')
            self.assertFalse(budget.tavan_uzat(home, parent, 'project_budget_cap_usd'))
            self.assertEqual(store.read(parent)['project_budget_cap_usd'], 5)
            self.assertFalse(budget.tavan_uzat(home, parent, 'project_budget_cap_usd'))

    def test_unreviewed_commit_is_not_budget_progress(self):
        import time
        with patch.object(budget.subprocess, 'run', return_value=SimpleNamespace(stdout=f'new {int(time.time())}')):
            self.assertIsNone(budget.tavan_ilerlemesi(Path('/unused'), {
                'source_sha': 'old', 'worktree': '/unused', 'created': time.time() - 1}))


if __name__ == '__main__':
    unittest.main()
