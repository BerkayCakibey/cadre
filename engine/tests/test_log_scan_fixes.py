"""23.09 morning static log scan — four defects from the logs of 10 runs:
B169 the same denial was recorded twice (one without a provider, 'plugin refusal'); if only the provider-less line remains the engine bans all providers
(T2 also had the line with a provider, only zen was banned — the 16:20 audit fix); B177 a Claude budget denial was logged as "insufficient output" on the plan path
and triggered the Claude breaker, and was mislabelled "executor failed" on the judge path; B178 the reason of a run ending with a missing lane was `rc=1`; B179 a series of attempts
failing with rc≠0 at 15 s intervals (zen 404 helper model) used up the 12-attempt limit in 3 min, and the error-loop detector did not see it because it counted per attempt.
No model/network."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import qe_core as core  # noqa: E402
import run_store  # noqa: E402
import budget_gate  # noqa: E402


class BudgetRejectionTextTest(unittest.TestCase):  # B177
    def test_claude_403_text_is_a_budget_rejection(self):
        self.assertTrue(core.budget_rejection_text("Failed to authenticate. API Error: 403 CADRE_BUDGET_DENIED"))
        self.assertTrue(core.budget_rejection_text('{"result": "CADRE_BUDGET_DENIED: no verified request allowance"}'))

    def test_normal_output_and_empty_are_not_a_rejection(self):
        self.assertFalse(core.budget_rejection_text("## ITEMS\n```json\n[]\n```"))
        self.assertFalse(core.budget_rejection_text(""))
        self.assertFalse(core.budget_rejection_text(None))


class FastDropTest(unittest.TestCase):  # B179
    def test_three_fast_drops_count_as_a_series_and_keep_the_first_start(self):
        hd = core.FastDrop(threshold_sec=60, threshold=3)
        self.assertEqual(hd.save(rc=1, duration_sec=15, start=100.0), 1)
        self.assertEqual(hd.save(rc=1, duration_sec=15, start=130.0), 2)
        self.assertEqual(hd.save(rc=1, duration_sec=16, start=160.0), 3)
        self.assertTrue(hd.loop)
        self.assertEqual(hd.series_start, 100.0)

    def test_successful_or_long_attempt_resets_the_series(self):
        hd = core.FastDrop(threshold_sec=60, threshold=3)
        hd.save(rc=1, duration_sec=10, start=1.0); hd.save(rc=1, duration_sec=10, start=20.0)
        self.assertEqual(hd.save(rc=1, duration_sec=600, start=40.0), 0, "A long attempt is not a rapid failure")
        hd.save(rc=1, duration_sec=10, start=700.0)
        self.assertEqual(hd.save(rc=0, duration_sec=5, start=720.0), 0)
        self.assertFalse(hd.loop)


class LaneOutcomeTest(unittest.TestCase):  # B178
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="lane-"))
        self.store = run_store.RunStore(self.home)
        (self.home / "projects" / "p").mkdir(parents=True)
        self.rid = self.store.accept("p", "task")["run_id"]

    def test_a_non_producing_lane_writes_a_structural_reason(self):
        run_store.lane_outcome_save(self.store, self.rid, "g2", "failed")
        r = self.store.read(self.rid)
        self.assertEqual(r["tasks"]["g2"]["dispatch_result"], "failed")
        self.assertEqual(r.get("reason"), "lane-failed: g2 (failed)")

    def test_a_producing_lane_does_not_change_the_reason_and_the_rc_reason_is_overridden(self):
        run_store.lane_outcome_save(self.store, self.rid, "g1", "produced")
        self.assertFalse(self.store.read(self.rid).get("reason"))
        self.store.update(self.rid, reason="rc=1")
        run_store.lane_outcome_save(self.store, self.rid, "g4", "blocked")
        self.assertEqual(self.store.read(self.rid).get("reason"), "lane-failed: g4 (blocked)")
        run_store.lane_outcome_save(self.store, self.rid, "g5", "failed")
        self.assertEqual(self.store.read(self.rid).get("reason"), "lane-failed: g4 (blocked), g5 (failed)")


class RedSingleTest(unittest.TestCase):  # B169 (ledger side)
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="red-"))
        self.store = run_store.RunStore(self.home)
        (self.home / "projects" / "p").mkdir(parents=True)
        self.rid = self.store.accept("p", "task")["run_id"]
        os.environ["CADRE_RUN_ID"] = self.rid

    def tearDown(self):
        os.environ.pop("CADRE_RUN_ID", None)

    def test_second_rejection_of_the_same_provider_in_the_same_second_is_one_record(self):
        budget_gate.note_denial(self.home, "deepseek", reason="allocation exhausted")
        budget_gate.note_denial(self.home, "deepseek", reason="ledger refused or unavailable")
        rows = self.store.read(self.rid).get("denials") or []
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["reason"], "allocation exhausted")

    def test_different_provider_is_a_separate_record(self):
        budget_gate.note_denial(self.home, "deepseek", reason="x")
        budget_gate.note_denial(self.home, "opencode", reason="y")
        self.assertEqual(len(self.store.read(self.rid).get("denials") or []), 2)


if __name__ == "__main__":
    unittest.main()
