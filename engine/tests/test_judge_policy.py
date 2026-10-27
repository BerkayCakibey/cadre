from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import go_window
import acceptance
import qe_core as core
import test_outcome
from outcome import approved
from test_go_window import raw


class JudgePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.f = test_outcome.CompletionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.successful_qa()
        self.model = "opencode-go/glm-5.3"
        self.f.enter(patch.object(core.ROUTER, "chain", return_value=([self.model], [])))
        self.f.enter(patch.object(core.ROUTER, "judge_breath", return_value=None))
        self.f.enter(patch.object(core, "budget_json", return_value={}))

    def finalize(self) -> dict:
        return core.finalize_run(
            "a", str(self.f.wt), self.f.log, "/fixture", review_model=self.model
        )

    def test_context_change_during_qa_prevents_a_review_call(self) -> None:
        original_qa = core.qa_gate

        def qa(*args, **kwargs):
            result = original_qa(*args, **kwargs)
            Path(self.f.store.read(self.f.rid)["prompt"]).write_text(
                "changed during QA"
            )
            return result

        with (
            patch.object(core, "qa_gate", side_effect=qa),
            patch.object(core, "capture_opencode") as reviewer,
        ):
            result = self.finalize()
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("review-context-changed", result["reason"])
        reviewer.assert_not_called()
        self.assertFalse(approved(self.f.store.read(self.f.rid)))

    def test_format_repair_uses_a_new_session_and_complete_acceptance_scope(
        self,
    ) -> None:
        from qa_fixture import review_output

        sessions = []

        def answer(*args, **kwargs):
            if not sessions:
                raw = review_output(
                    str(self.f.review()).replace(
                        '"evidence": "', '"evidence": "bad "quote" ', 1
                    )
                )
            else:
                q = json.loads(args[2][-1].splitlines()[-1])
                self.assertEqual(q["original_request"], "deliver product")
                self.assertEqual(q["qa"]["status"], "PASS")
                self.assertIn("plan", q)
                self.assertIn("review_context", q)
                self.assertEqual(args[4]["CADRE_READ_ONLY"], "1")
                raw = review_output(str(self.f.review()))
            sessions.append(getattr(raw, "attempt").data["session_id"])
            return raw

        with patch.object(core, "capture_opencode", side_effect=answer):
            result = self.finalize()
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(len(sessions), 2)
        self.assertNotEqual(*sessions)
        self.assertEqual(result["review"]["session_proof"]["session_id"], sessions[-1])
        self.assertTrue(approved(self.f.store.read(self.f.rid)))

    def test_guarantee_can_reserve_a_single_permitted_family_without_raising_the_cap(
        self,
    ) -> None:
        from test_night_wait import full_budget
        from test_run_sessions import CATALOG

        budget = full_budget(cloud=True)
        budget["go"].update(available=50.0, pay=50.0)
        catalog = self.f.home / "models.json"
        catalog.write_text(json.dumps(CATALOG))
        self.f.store.update(self.f.rid, budget_cap_usd=50.0, budget_manual=True)
        with (
            patch.object(core, "budget_json", return_value=budget),
            patch.dict(os.environ, {"CADRE_MODEL_CATALOG": str(catalog)}),
        ):
            guarantee = acceptance.acceptance_assurance(self.f.log)
        self.assertEqual(guarantee["candidates"], [self.model])
        self.assertEqual(guarantee["models"]["quality"], self.model)
        self.assertEqual(len(guarantee["reservations"]), 3)
        self.assertEqual(self.f.store.read(self.f.rid)["budget_cap_usd"], 50.0)

    def test_same_model_and_planner_use_a_fresh_native_readonly_attempt(self) -> None:
        self.f.store.update(self.f.rid, plan_model=self.model)
        writer = (
            self.f.store.task_dir(self.f.rid, "impl") / "attempts/fixture/attempt.json"
        )
        writer.write_text(
            json.dumps(
                {
                    "role": "impl",
                    "model": self.model,
                    "pid": 123,
                    "session_id": "ses_writer",
                }
            )
        )
        fake = self.f.home / "native-reviewer"
        fake.write_text(
            f"#!{sys.executable}\n"
            + """import json, os, subprocess, sys, uuid
q = json.loads(sys.argv[-1].splitlines()[-1])
assert '--session' not in sys.argv
p = json.loads(os.environ['CADRE_TOOL_POLICY'])
assert p['writable'] is False
r = subprocess.run([p['sandbox'], '-p', p['profile'], sys.executable, '-c', "open('product.txt','w').write('bad')"], capture_output=True)
assert r.returncode != 0
assert q['original_request'] == 'deliver product'
assert 'plan' in q and q['source_sha']
a = {'revision': q['revision'], 'verdict': 'PASS', 'acceptance': [{'id': c['id'], 'status': 'PASS', 'evidence': 'product.txt read; native QA verified'} for c in q['criteria']]}
print(json.dumps({'type': 'text', 'sessionID': 'ses_' + uuid.uuid4().hex, 'part': {'type': 'text', 'text': json.dumps(a)}}))
"""
        )
        fake.chmod(0o700)
        with (
            patch.object(
                core, "run_opencode", side_effect=test_outcome.REAL_RUN_OPENCODE
            ),
            patch.object(
                core, "opencode_budget_env", side_effect=lambda env, args: args
            ),
        ):
            results = [
                core.finalize_run(
                    "a", str(self.f.wt), self.f.log, str(fake), review_model=self.model
                )
                for _ in range(2)
            ]
        for result in results:
            self.assertEqual(result["status"], "PASS", result)
            self.assertTrue(result["review"]["independent"])
            self.assertEqual(result["review"]["model"], self.model)
            self.assertEqual(result["qa"]["revision"], result["review"]["revision"])
        self.assertNotEqual(
            results[0]["review"]["session_proof"]["session_id"],
            results[1]["review"]["session_proof"]["session_id"],
        )
        self.assertEqual((self.f.wt / "product.txt").read_text(), "after\n")
        self.assertTrue(approved(self.f.store.read(self.f.rid)))

    def test_family_difference_or_old_boolean_cannot_replace_session_evidence(
        self,
    ) -> None:
        with patch.object(core, "capture_opencode", return_value=str(self.f.review())):
            result = self.finalize()
        self.assertNotEqual(result["status"], "PASS")
        self.assertFalse(result["review"]["independent"])
        result["review"]["independent"] = True
        result["status"] = "PASS"
        self.f.store.update(self.f.rid, final_result=result)
        self.assertFalse(approved(self.f.store.read(self.f.rid)))

    def test_resumed_wrong_role_reused_session_and_writable_policy_are_rejected(
        self,
    ) -> None:
        for case in ("resumed", "role", "session", "policy"):
            with self.subTest(case=case):
                previous = (
                    self.f.store.task_dir(self.f.rid, "impl")
                    / "attempts/fixture/attempt.json"
                )
                previous.write_text(
                    json.dumps(
                        {
                            "role": "impl",
                            "model": self.model,
                            "pid": 123,
                            "session_id": "ses_writer",
                        }
                    )
                )

                def answer(*args, **kwargs):
                    raw = self.f.review()
                    attempt = getattr(raw, "attempt")
                    if case == "resumed":
                        attempt.save(resumed=True)
                    elif case == "role":
                        attempt.save(role="impl")
                    elif case == "session":
                        attempt.save(session_id="ses_writer")
                    else:
                        attempt.tool_policy["writable"] = True
                        core.atomic_json(
                            attempt.directory / "tool-policy.json", attempt.tool_policy
                        )
                    return raw

                with patch.object(core, "capture_opencode", side_effect=answer):
                    result = self.finalize()
                self.assertNotEqual(result["status"], "PASS", result)
                self.assertFalse(result["review"]["independent"])
                self.assertFalse(approved(self.f.store.read(self.f.rid)))

    def test_readonly_product_change_requires_authorized_repair_and_new_qa(
        self,
    ) -> None:
        def write(*args, **kwargs):
            self.assertEqual(kwargs["role"], "quality")
            self.assertEqual(args[4]["CADRE_READ_ONLY"], "1")
            (self.f.wt / "product.txt").write_text("unauthorized\n")
            return self.f.review()

        with (
            patch.object(core, "capture_opencode", side_effect=write),
            patch.object(core, "qa_gate", wraps=core.qa_gate) as qa,
        ):
            result = self.finalize()
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(qa.call_count, 1)
        self.assertFalse(result["review"]["independent"])
        (self.f.wt / "product.txt").write_text("authorized repair\n")
        with patch.object(
            core, "capture_opencode", side_effect=lambda *a, **k: self.f.review()
        ):
            repaired = self.finalize()
        self.assertEqual(repaired["status"], "PASS", repaired)
        self.assertNotEqual(result["qa"]["revision"], repaired["qa"]["revision"])
        self.assertEqual(repaired["qa"]["revision"], repaired["review"]["revision"])

    def test_missing_original_goal_is_rejected_even_if_every_plan_item_passes(
        self,
    ) -> None:
        plan = {"tasks": [{"id": "g1", "acceptance": "one part"}]}
        core.atomic_json(self.f.store.path(self.f.rid) / "plan.json", plan)
        self.f.store.update(self.f.rid, phase="dispatch")
        self.f.store.task_update(self.f.rid, "g1", dispatch_result="produced")

        def reject(*args, **kwargs):
            q = json.loads(args[2][-1].splitlines()[-1])
            self.assertEqual(q["original_request"], "deliver product")
            self.assertEqual(q["plan"], plan)
            value = json.loads(
                str(self.f.review(ids=tuple(c["id"] for c in q["criteria"])))
            )
            value["verdict"] = "REVISE"
            value["acceptance"][0].update(
                status="FAIL", evidence="original goal needs the missing second part"
            )
            from qa_fixture import review_output

            return review_output(json.dumps(value))

        with patch.object(core, "capture_opencode", side_effect=reject):
            result = self.finalize()
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["review"]["verdict"], "REVISE")
        self.assertTrue(result["review"]["independent"])
        self.assertFalse(approved(self.f.store.read(self.f.rid)))

    def test_acceptance_is_invalid_after_context_or_policy_mutation(self) -> None:
        with patch.object(
            core, "capture_opencode", side_effect=lambda *a, **k: self.f.review()
        ):
            result = self.finalize()
        self.assertEqual(result["status"], "PASS", result)
        record = self.f.store.read(self.f.rid)
        self.assertTrue(approved(record))
        prompt = Path(record["prompt"])
        original = prompt.read_bytes()
        prompt.write_text("changed goal")
        self.assertFalse(approved(record))
        prompt.write_bytes(original)
        core.atomic_json(prompt.parent / "plan.json", {"changed": True})
        self.assertFalse(approved(record))
        (prompt.parent / "plan.json").unlink()
        proof = result["review"]["session_proof"]
        policy = (
            self.f.store.task_dir(self.f.rid, proof["task_id"])
            / "attempts"
            / proof["attempt_id"]
            / "tool-policy.json"
        )
        policy.write_text("{}")
        self.assertFalse(approved(record))


class HistoricalTriageTests(unittest.TestCase):
    def test_corrupt_cache_is_unknown_and_over_hundred_usage_closes_the_pool(
        self,
    ) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            cache = {
                "ts": time.time(),
                "rolling": 0,
                "weekly": 0,
                "monthly": 42,
                "resets": {},
                "history": [],
            }
            for corrupt in (
                {"ts": time.time()},
                {**cache, "monthly": float("nan")},
                {**cache, "ts": float("inf")},
                {**cache, "ts": time.time() + 3600},
            ):
                with (
                    self.subTest(corrupt=corrupt),
                    patch.object(go_window, "_pull", side_effect=OSError("offline")),
                ):
                    go_window._write(home, corrupt)
                    self.assertIsNone(go_window.read(home))
            with patch.object(go_window, "_pull", return_value=raw(monthly=101)):
                reading = go_window.read(home, fresh=True)
            assert reading is not None
            self.assertEqual(reading["monthly"], 101)
            self.assertFalse(go_window.status(reading)[0])
            with patch.object(
                go_window,
                "_pull",
                side_effect=AssertionError("valid cache must stay closed"),
            ):
                cached = go_window.read(home)
            self.assertFalse(go_window.status(cached)[0])

    def test_go_api_reset_does_not_require_a_valid_local_anchor_file(self) -> None:
        import tempfile
        from datetime import datetime, timedelta, timezone

        import provider_budget

        reset = datetime(2026, 11, 2, 20, 39, 30, tzinfo=timezone.utc)
        local_reset = reset.astimezone().replace(tzinfo=None)
        expected_start = local_reset.replace(month=10)
        now = expected_start + timedelta(days=15)
        observation = {"resets": {"monthly": reset.isoformat()}}
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "chain").mkdir()
            path = home / "chain/go-period-reset.txt"
            for saved in (None, "invalid-date", "2026-10-01 00:00"):
                with self.subTest(saved=saved), patch.object(provider_budget, "QE", home), patch.object(go_window, "read", return_value=observation):
                    if saved is not None:
                        path.write_text(saved)
                    actual_start, actual_reset, days = provider_budget.go_period(now)
                    self.assertEqual(actual_start, int(expected_start.timestamp()))
                    self.assertEqual(actual_reset, local_reset)
                    self.assertEqual(days, 16)

    def test_go_unknown_or_past_api_reset_keeps_local_or_calendar_fallback(self) -> None:
        import tempfile
        from datetime import datetime, timezone

        import provider_budget

        now = datetime(2026, 10, 17, 12, tzinfo=timezone.utc).replace(tzinfo=None)
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "chain").mkdir()
            path = home / "chain/go-period-reset.txt"
            for saved in (None, "2026-10-02 22:00"):
                if saved is not None:
                    path.write_text(saved)
                for observation in (None, {"resets": {"monthly": "invalid-date"}}, {"resets": {"monthly": "2026-10-01T00:00:00Z"}}):
                    with self.subTest(saved=saved, observation=observation), patch.object(provider_budget, "QE", home), patch.object(go_window, "read", return_value=observation):
                        start, reset, days = provider_budget.go_period(now)
                        if saved is None:
                            expected_start = now.replace(day=1, hour=0)
                            self.assertEqual((start, reset, days), (int(expected_start.timestamp()), None, 15))
                        else:
                            expected_start = now.replace(day=2, hour=22)
                            expected_reset = expected_start.replace(month=11)
                            self.assertEqual((start, reset, days), (int(expected_start.timestamp()), expected_reset, 17))

    def test_go_month_rollover_preserves_the_billing_anchor(self) -> None:
        import tempfile
        from datetime import datetime
        import provider_budget

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "chain").mkdir()
            reset_path = home / "chain/go-period-reset.txt"
            for anchor, now, expected, start in (
                (
                    "2026-01-31 22:00",
                    datetime(2026, 3, 1),
                    datetime(2026, 3, 31, 22),
                    datetime(2026, 2, 28, 22),
                ),
                (
                    "2024-01-31 22:00",
                    datetime(2024, 3, 1),
                    datetime(2024, 3, 31, 22),
                    datetime(2024, 2, 29, 22),
                ),
                (
                    "2025-12-31 22:00",
                    datetime(2026, 1, 1),
                    datetime(2026, 1, 31, 22),
                    datetime(2025, 12, 31, 22),
                ),
            ):
                with (
                    self.subTest(anchor=anchor),
                    patch.object(provider_budget, "QE", home),
                    patch.object(go_window, "read", return_value=None),
                ):
                    reset_path.write_text(anchor)
                    actual_start, actual_reset, _ = provider_budget.go_period(now)
                    self.assertEqual(actual_reset, expected)
                    self.assertEqual(actual_start, int(start.timestamp()))

    def test_error_and_provider_are_from_the_same_burst_line(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "opencode.log"
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
            first = f'timestamp={stamp} level=ERROR providerID=opencode-go modelID=glm-5.3 session.id=ses_a error.error="429 Too Many Requests"\n'
            other = f'timestamp={stamp} level=ERROR providerID=openrouter modelID=other session.id=ses_a error.error="connection failed"\n'
            log.write_text(first + other * 5)
            with patch.object(core, "OC_LOG", log):
                result = core.error_burst(
                    sessions={"ses_a"}, start=int(time.time()) - 30
                )
            assert result is not None
            self.assertEqual(result[0], 6)
            self.assertIn("429", result[1])
            self.assertEqual(result[2:], ("opencode-go", "glm-5.3"))

    def test_invalid_go_shapes_preserve_known_cache_and_never_invent_zero(self) -> None:
        import tempfile

        invalid = [{}, [], {"usage": {}}, {"usage": {"monthly": {"percent": 0}}}]
        for value in (None, True, "0", -1, float("nan"), float("inf")):
            value_shape = raw(monthly=12)
            value_shape["usage"]["monthly"]["percent"] = value
            invalid.append(value_shape)
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            for value in invalid:
                with (
                    self.subTest(value=value),
                    patch.object(go_window, "_pull", return_value=value),
                ):
                    self.assertIsNone(go_window.read(home, fresh=True))
            with patch.object(go_window, "_pull", return_value=raw(monthly=42)):
                reading = go_window.read(home, fresh=True)
                assert reading is not None
                self.assertEqual(reading["monthly"], 42)
            with patch.object(go_window, "_pull", return_value={}):
                reading = go_window.read(home, fresh=True)
                assert reading is not None
                self.assertEqual(reading["monthly"], 42)

    def test_tmp_alias_uses_the_same_native_scope(self) -> None:
        import tempfile
        from session import project_tool_policy

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            from run_store import RunStore

            store = RunStore(root / "home")
            rid = store.accept("a", "read only")["run_id"]
            worktree = root / "wt"
            worktree.mkdir()
            policy = project_tool_policy(
                store,
                store.read(rid),
                root / "attempt",
                str(worktree),
                "opencode",
                "review",
                dict(os.environ),
            )
            assert policy is not None
            self.assertEqual(policy["worktree"], str(worktree.resolve()))
            self.assertTrue(policy["worktree"].startswith("/private/tmp/"))
            self.assertFalse(policy["writable"])


if __name__ == "__main__":
    unittest.main()
