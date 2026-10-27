"""Night scenarios updated with the 03.10 decisions — fake data, real decision code (no network/model/Windows):
S1  compaction/sub-agent: cloud if there is budget, otherwise the helper closes; the decision is made on EVERY executor call (opencode_budget_env).
S2  GO1: if the Go share runs out IN THE MIDDLE of a job the in-flight lane is NOT CUT (it runs to the end); the brake only stops a new push.
S3  P3: when the Go share runs out the main model does NOT FALL to local — a budget-wait denial at the gate; when the budget returns the next call is cloud again.
    B93: when the budget observation cannot be read the engine goes to a budget wait, not to local (retryable); the read error is logged as 'unreadable'.
Every test measures a behaviour; no source-text grep."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import test_run_sessions as fixtures  # noqa: E402
import test_end_to_end as e2e  # noqa: E402
import qe_core as core  # noqa: E402
from test_night_wait import full_budget  # noqa: E402

GO_IMPL, GO_SMALL, DS, GEMINI = 'opencode-go/kimi-k2.7-code', 'opencode-go/glm-5.3-flash', 'opencode/deepseek-v4-flash', 'google/gemini-3-flash-preview'
# 4c-1: DS = the second paid route (DeepSeek flash on Zen); the direct DeepSeek provider was removed.
JUDGES = ['opencode-go/glm-5.3', 'opencode-go/qwen3.8-max']   # P6: two families independent of the writer families (kimi, deepseek)


def chain_fake(role, *_a, **_k):
    if role == 'quality':   # review (an in-lane helper) uses the writer chain; the ban test looks at it
        return list(JUDGES), []
    return [GO_IMPL, DS], []


def budget(go=5.0, zen=5.0, gemini=450):
    b = full_budget(cloud=True)
    b['go'].update(available=go * 100 / 15, ratio=go / 5.0)   # 4c-13: Go points; the parameter is the dollars of the $15 model
    b['zen'].update(available=zen, remaining=zen, ratio=zen / 5.0)
    b['gemini'].update(remaining_request=gemini, today_request=500 - gemini, ratio=gemini / 500)
    if gemini == 0:
        b['groq'].update(today_request=900, ratio=0.0)
    return b


class ConfigPerCallTests(unittest.TestCase):
    """S1 + S3: opencode_budget_env reads the budget on every call; compaction/sub-agent and the main model are chosen accordingly."""

    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.enter(patch.object(core, 'project_memory', return_value=[]))
        self.f.enter(patch.object(core.ROUTER, 'chain', side_effect=chain_fake))

    def config_for(self, b, model_arg):
        self.f.enter(patch.object(core, 'budget_json', return_value=b))
        env = {'OPENCODE_CONFIG': '/nonexistent'}
        args = core.opencode_budget_env(env, ['-m', model_arg, 'task text'])
        cfg = json.loads(env['OPENCODE_CONFIG_CONTENT'])
        return args, cfg


    def test_compaction_follows_the_session_model_when_no_helper_candidate_fits(self):
        """A disabled compaction agent is DELETED from the opencode registry; when compaction runs the agent is not found and
        the session fails. Without a candidate, compaction is written to the session model that already holds the context."""
        def chain_compaction_empty(role, *_a, **_k):
            return ([], []) if role == 'compaction' else chain_fake(role)
        self.f.enter(patch.object(core.ROUTER, 'chain', side_effect=chain_compaction_empty))
        args, cfg = self.config_for(budget(), GO_IMPL)
        self.assertEqual(args[args.index('-m') + 1], GO_IMPL)
        compaction = cfg['agent']['compaction']
        self.assertEqual((compaction['model'], compaction['disable']), (GO_IMPL, False))
        self.assertEqual(compaction['permission']['bash'], 'allow')
        self.assertEqual(compaction['permission']['read'], 'deny')


    def test_s3_main_model_follows_the_budget_call_by_call(self):
        args, _ = self.config_for(budget(), GO_IMPL)
        self.assertIn(GO_IMPL, args, 'With budget available, a cloud lane starts in the cloud')
        # P3 (03.10): Go share exhausted -> the main model does NOT FALL to local; a budget-wait denial at the gate (the model identity is kept).
        with self.assertRaises(core.BudgetDenied):
            self.config_for(budget(go=0.0), GO_IMPL)
        args, _ = self.config_for(budget(), GO_IMPL)
        self.assertIn(GO_IMPL, args, 'Budget returned after a new day or reset; the next call is cloud-based again')


class MidRunBudgetHandoverTests(unittest.TestCase):
    """S2: the Go share runs out while a cloud lane runs -> brake, controlled interruption, next provider, same session, the part is kept."""

    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_s2_go_budget_exhausted_mid_lane_does_not_cut_the_inflight_lane(self):
        home, wt = self.f.home, self.f.wt
        full, finished = budget(), budget(go=0.0)
        self.f.enter(patch.object(core, 'budget_json', side_effect=lambda: finished if (home / 'calls.jsonl').exists() else full))
        self.f.enter(patch.object(core, 'project_memory', return_value=[]))
        self.f.enter(patch.object(core.ROUTER, 'chain', side_effect=chain_fake))
        self.f.fake.write_text(self.f.fake.read_text() + f"""
import time
n = sum(1 for _ in (p/'calls.jsonl').open())
if n == 1:
    pathlib.Path({str(wt)!r}, 'part.txt').write_text('partially completed cloud lane work')
    time.sleep(3)   # GO1: the in-flight lane is NOT INTERRUPTED; otherwise the next progress tick would stop it and record a second call
    raise SystemExit(0)
""")
        rc, _gate = self.f.runner_fixture(env={'CADRE_RUN_MODEL': GO_IMPL, 'CADRE_MAX_RESUME': '3', 'CADRE_PROGRESS_POLL': '1'})
        calls = self.f.calls()
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(calls[0][calls[0].index('-m') + 1], GO_IMPL, 'The lane started in the cloud')
        self.assertEqual((wt / 'part.txt').read_text(), 'partially completed cloud lane work')
        self.assertEqual(self.f.store.read(self.f.rid)['tasks']['g1']['state'], 'produced')
        log = (self.f.store.task_dir(self.f.rid, 'g1') / 'runner.log').read_text()
        self.assertNotIn('GO-RESTRICTION', log, 'GO1: exhausting Go during a job does not interrupt the in-flight lane; the brake blocks only new admission')


class BudgetObservationReturnsTests(unittest.TestCase):
    """B93 (18.09 live): when the budget observation could not be read the restriction ladder fell to local WITHOUT looking at the chain and this decision
    stayed PERMANENT FOR THE WHOLE RUN. P3 (03.10): an unreadable budget no longer DROPS to local — the engine leaves the job partial with a budget wait
    (the coordinator marks it retryable, the same run continues when the budget returns). Fail-closed is right, being PERMANENT is not."""

    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.enter(patch.object(core, 'project_memory', return_value=[]))
        self.f.enter(patch.object(core.ROUTER, 'chain', side_effect=chain_fake))

    def test_unreadable_budget_marks_lane_budget_wait_not_local(self):
        home, wt = self.f.home, self.f.wt
        stale = budget()
        stale['ts'] = '2020-01-01T00:00:00'      # stale observation -> allowance 'stale budget' -> step 'no-budget'
        fresh = budget()
        self.f.enter(patch.object(core, 'budget_json',
                                  side_effect=lambda: fresh if (home / 'calls.jsonl').exists() else stale))
        rc, _gate = self.f.runner_fixture(env={'CADRE_MAX_RESUME': '3', 'CADRE_PROGRESS_POLL': '1',
                                              'CADRE_RISE_ROUND': '1'})
        log = (self.f.store.task_dir(self.f.rid, 'g1') / 'runner.log').read_text()
        # B94 + B131: the message must say "unreadable", it must NOT say "share exhausted". The MEANING is tested.
        self.assertIn('unreadable', log, 'B94: the job-start message must report a read failure')
        self.assertNotIn('daily Go allocation exhausted', log,
                         'B131: reporting a read failure as allocation exhaustion is a wrong diagnosis')
        # P3: an unreadable budget does not fall to local — it is left partial with a budget wait (retryable, not permanent).
        self.assertIn('BUDGET WAIT', log, 'P3: without budget observation, wait for budget rather than falling back locally')
        task = self.f.store.read(self.f.rid)['tasks']['g1']
        self.assertEqual((task['state'], task.get('reason')), ('half', 'budget-wait'))
        self.assertFalse((self.f.home / 'calls.jsonl').exists(), 'Do not start an executor with unreadable budget observation')


if __name__ == '__main__':
    unittest.main()
