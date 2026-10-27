"""G-C13 (23.09 live T1 finding): the briefing bound all items to an "APPROVAL:" decision, the executor marked all of them `BLOCKED: approval`
but kept editing the product for 40 min and breaking the same test; the guard had no exit path (since it produced artefacts
"guard cut" did not trigger either). Contract: if no workable item remains in `.tasks.json` the run is CUT, the state is
`half approval-waiting: <question>`, the coordinator does not retry, no delivery; the decision falls to the user. No model/network."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import qe_core as core  # noqa: E402
import run_store  # noqa: E402

QUESTION = "APPROVAL: should --version print 'package_version()' or 'feedloom <version>'?"


def _item(mid, title, status):
    return {"id": mid, "title": title, "source": "x", "acceptance": "y", "scope_outside": "", "dependent": "", "status": status, "passes": False}


def _write(wt, items):
    (Path(wt) / ".tasks.json").write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")


class ApprovalBlockTest(unittest.TestCase):
    def setUp(self):
        self.wt = tempfile.mkdtemp(prefix="approval-blocked-")

    def test_approval_question_returns_if_all_items_are_blocked(self):
        _write(self.wt, [_item("v01", QUESTION, "BLOCKED: approval"), _item("v02", "cli.py edit", "BLOCKED: approval (v01)")])
        self.assertEqual(core.approval_block(self.wt), QUESTION)

    def test_no_block_if_a_doable_item_exists(self):
        _write(self.wt, [_item("v01", QUESTION, "BLOCKED: approval"), _item("v02", "test write", "todo")])
        self.assertEqual(core.approval_block(self.wt), "")

    def test_missing_file_broken_or_empty_list_means_no_block(self):
        self.assertEqual(core.approval_block(self.wt), "")
        (Path(self.wt) / ".tasks.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(core.approval_block(self.wt), "")
        _write(self.wt, [])
        self.assertEqual(core.approval_block(self.wt), "")

    def test_guard_tick_cuts_the_worker_on_an_approval_block(self):
        _write(self.wt, [_item("v01", QUESTION, "BLOCKED: approval"), _item("v02", "apply", "BLOCKED: approval (v01)")])
        logf = Path(self.wt) / "runner.log"
        logf.write_text("")
        proc = subprocess.Popen(["sleep", "30"])
        try:
            with patch.object(core, "cancel_path", return_value=Path(self.wt) / "absent.cancel"):
                guard = core.StallGuard(str(logf), proc, 9999, wt=self.wt)
                self.assertEqual(guard.tick("fake-project-ref"), "approval")
            self.assertEqual(guard.approval_question, QUESTION)
            self.assertIsNotNone(proc.wait(timeout=5), "worker was not stopped")
            self.assertIn("APPROVAL-BLOCKED", logf.read_text())
            self.assertFalse(guard.cut, "An approval stop is not a stall and does not trigger helper policy")
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)  # B142b: the Popen object must not be collected with an open child (ResourceWarning)

    def test_guard_tick_follows_the_old_path_without_a_block(self):
        _write(self.wt, [_item("v01", QUESTION, "BLOCKED: approval"), _item("v02", "apply", "in progress")])
        logf = Path(self.wt) / "runner.log"
        logf.write_text("")
        proc = subprocess.Popen(["sleep", "30"])
        try:
            with patch.object(core, "cancel_path", return_value=Path(self.wt) / "absent.cancel"):
                guard = core.StallGuard(str(logf), proc, 9999, wt=self.wt)
                self.assertIsNone(guard.tick("fake-project-ref"))
            self.assertIsNone(proc.poll(), "Do not stop the worker without a block")
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_coordinator_treats_the_awaiting_approval_reason_as_permanent(self):
        # run_store.py:624 — `reason.split(':')[0] in PERMANENT_REASONS` -> failed (no retry)
        self.assertIn("approval-waiting", run_store.PERMANENT_REASONS)

BRIEFING = """# BRIEFING (runner on-plan) — model: x

## ITEMS

```json
[
  {"id": "G01", "title": "cli.py edit", "dependent": "G05"},
  {"id": "G05", "title": "APPROVAL: existing test conflict — output format?", "dependent": ""}
]
```

## PITFALLS
"""


class BriefingApprovalTest(unittest.TestCase):
    """T1b (23.09 02:28): the executor stopped for approval but did not write BLOCKED to `.tasks.json` and exited with rc=0; the run
    went to QA with an empty product (FAIL rc=2). Second trigger: rc=0 + product unchanged + an APPROVAL item in the briefing -> approval-waiting."""

    def setUp(self):
        self.wt = tempfile.mkdtemp(prefix="approval-briefing-")
        self.bp = Path(self.wt) / "plan" / "briefing-runner.md"
        self.bp.parent.mkdir()

    def test_briefing_approval_question_is_read_from_the_items(self):
        self.bp.write_text(BRIEFING, encoding="utf-8")
        self.assertEqual(core.briefing_approval_question(self.bp), "APPROVAL: existing test conflict — output format?")

    def test_briefing_empty_without_approval_or_file(self):
        self.assertEqual(core.briefing_approval_question(self.bp), "")
        self.bp.write_text(BRIEFING.replace("APPROVAL: ", "DECISION RECORDED: "), encoding="utf-8")
        self.assertEqual(core.briefing_approval_question(self.bp), "")
        self.bp.write_text("## ITEMS\n```json\n[broken\n```", encoding="utf-8")
        self.assertEqual(core.briefing_approval_question(self.bp), "")

    def test_approval_decision_question_if_the_product_is_unchanged_and_the_briefing_has_approval(self):
        self.bp.write_text(BRIEFING, encoding="utf-8")
        self.assertEqual(core.approval_decision(self.wt, self.bp, product_changed=False), "APPROVAL: existing test conflict — output format?")

    def test_approval_decision_the_briefing_alone_is_not_enough_if_the_product_changed(self):
        self.bp.write_text(BRIEFING, encoding="utf-8")
        self.assertEqual(core.approval_decision(self.wt, self.bp, product_changed=True), "")

    def test_approval_decision_json_block_is_valid_even_if_the_product_changed(self):
        _write(self.wt, [_item("v01", QUESTION, "BLOCKED: approval"), _item("v02", "apply", "BLOCKED: approval (v01)")])
        self.assertEqual(core.approval_decision(self.wt, self.bp, product_changed=True), QUESTION)


class ApprovalFalsePositiveTest(unittest.TestCase):
    """T1v3 (23.09 04:21): the briefing headers were "APPROVAL: none | <test>" (meaning NO approval is needed) and the agent committed the product
    in the worktree; `startswith("APPROVAL")` + a clean `git status` -> a wrong 'approval-waiting', the right product could not reach the acceptance gate.
    Contract: "APPROVAL: none/not required/..." is not an approval item; the product change is measured against the SOURCE SHA (committed changes included)."""

    def setUp(self):
        self.wt = tempfile.mkdtemp(prefix="approval-yp-")
        self.bp = Path(tempfile.mkdtemp(prefix="approval-yp-run-")) / "plan" / "briefing-runner.md"  # the briefing is in the run directory, not in the worktree
        self.bp.parent.mkdir()
        g = lambda *a: subprocess.run(["git", "-C", self.wt, "-c", "user.email=t@t", "-c", "user.name=t", *a], check=True, capture_output=True, text=True)
        g("init", "-q"); (Path(self.wt) / "a.py").write_text("x = 1\n"); g("add", "a.py"); g("commit", "-qm", "base")
        self.source = g("rev-parse", "HEAD").stdout.strip()
        self.g = g

    def test_no_approval_heading_is_not_an_approval_item(self):
        self.bp.write_text(BRIEFING.replace('"APPROVAL: existing test conflict — output format?"', '"APPROVAL: absent | test_x (acceptance a)"'), encoding="utf-8")
        self.assertEqual(core.briefing_approval_question(self.bp), "")
        for negative in ("APPROVAL: not required — x", "APPROVAL: no", "APPROVAL: unnecessary (a)"):
            self.bp.write_text(BRIEFING.replace("APPROVAL: existing test conflict — output format?", negative), encoding="utf-8")
            self.assertEqual(core.briefing_approval_question(self.bp), "", negative)

    def test_no_block_state_is_not_a_block(self):
        _write(self.wt, [_item("v01", "x", "BLOCKED: absent"), _item("v02", "y", "BLOCKED: absent")])
        self.assertEqual(core.approval_block(self.wt), "")

    def test_committed_product_change_counts_as_change_relative_to_source_sha(self):
        self.bp.write_text(BRIEFING, encoding="utf-8")
        (Path(self.wt) / "b.py").write_text("y = 2\n"); self.g("add", "b.py"); self.g("commit", "-qm", "agent commit")
        (Path(self.wt) / ".tasks.json").write_text("[]"); (Path(self.wt) / ".cadre-plan").mkdir(); (Path(self.wt) / ".cadre-plan" / "p.md").write_text("plan")
        self.assertEqual(core.approval_decision(self.wt, self.bp, source_sha=self.source), "", "A committed product is a change, not an approval block")

    def test_product_counts_as_unchanged_if_only_harness_files_exist(self):
        self.bp.write_text(BRIEFING, encoding="utf-8")
        (Path(self.wt) / ".tasks.json").write_text("[]"); (Path(self.wt) / ".cadre-plan").mkdir(); (Path(self.wt) / ".cadre-plan" / "p.md").write_text("plan")
        self.assertEqual(core.approval_decision(self.wt, self.bp, source_sha=self.source), "APPROVAL: existing test conflict — output format?")


if __name__ == "__main__":
    unittest.main()
