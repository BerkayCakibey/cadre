from __future__ import annotations

import copy
import importlib
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
izin = importlib.import_module("izin")
RunStore = importlib.import_module("kosu").RunStore


class OperatorPermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(prefix="cadre-permission-")
        self.addCleanup(folder.cleanup)
        self.store = RunStore(Path(folder.name))
        self.rid = self.store.accept("fixture", "local work")["run_id"]
        self.args = (
            self.store,
            self.rid,
            "commit",
            [["git", "-C", "/fixture", "commit", "-m", "fixture"]],
            {"path": "/fixture", "branch": "cadre/fixture"},
            {"head": "before", "digest": "product"},
        )
        self.action = izin.propose(*self.args)

    def approve(self, action: dict | None = None) -> dict:
        action = action or self.action
        first = izin.approve(self.store, self.rid, action["token"])
        return (
            izin.approve(self.store, self.rid, action["token"], first["confirmation"])
            if first["required"] == 2
            else first
        )

    def test_specific_operator_reasons_survive_staleness_marking(self) -> None:
        self.approve()

        def invalid():
            raise PermissionError("operator-commit-worktree-invalid")
        with self.assertRaisesRegex(PermissionError, "operator-commit-worktree-invalid"):
            with izin.permitted(self.store, self.rid, self.action["token"], invalid):
                raise AssertionError("must not run")
        self.assertEqual(izin.read_action(self.store, self.rid, self.action["token"])["status"], "stale")
        fresh = izin.propose(*self.args)
        self.assertNotEqual(fresh["token"], self.action["token"])
        self.approve(fresh)

        def broken():
            raise ValueError("unexpected failure")
        with self.assertRaisesRegex(PermissionError, "operator-action-stale"):
            with izin.permitted(self.store, self.rid, fresh["token"], broken):
                raise AssertionError("must not run")
        self.assertEqual(izin.read_action(self.store, self.rid, fresh["token"])["status"], "stale")

    def test_commit_temporary_index_lives_beside_the_target_index(self) -> None:
        fixture, repo, wt, parent, rid, env = self.managed_fixture()
        store = fixture.store
        (wt / "app.py").write_text("value = 2\n")
        action = izin.commit_proposal(store, rid, wt, "Fixture reviewed product")
        first = izin.approve(store, rid, action["token"])
        izin.approve(store, rid, action["token"], first["confirmation"])
        index = Path(izin.git(wt, "rev-parse", "--git-path", "index"))
        if not index.is_absolute():
            index = wt / index
        seen = []
        real = izin.tempfile.mkstemp

        def record(*args, **kwargs):
            if kwargs.get("prefix") == "operator-commit-index-":
                seen.append(Path(kwargs["dir"]).resolve())
            return real(*args, **kwargs)
        with patch.object(izin.tempfile, "mkstemp", side_effect=record):
            izin.commit_execute(store, rid, action["token"])
        self.assertEqual(seen, [index.parent.resolve()])
        self.assertEqual([p.name for p in index.parent.iterdir() if p.name.startswith("operator-commit-index-")], [])
        self.assertEqual([p.name for p in fixture.store.path(rid).iterdir() if "index" in p.name], [])

    def test_unrecorded_merge_is_adopted_when_main_advanced_and_noted_when_rewound(self) -> None:
        for later in ("advance", "rewind"):
            with self.subTest(later=later):
                fixture, rid = self.completed_fixture()
                action = izin.delivery_proposal(fixture.store, rid, "merge")
                self.approve_in(fixture.store, rid, action)
                real_json = izin.atomic_json

                def write(path, value):
                    if Path(path).name == "run.json":
                        raise OSError("fixture metadata unavailable")
                    return real_json(path, value)
                with patch.object(izin, "atomic_json", side_effect=write), self.assertRaises(OSError):
                    izin.delivery_execute(fixture.store, rid, action["token"])
                merged = fixture.git("rev-parse", "HEAD")
                before = action["identity"]["target"]["head"]
                self.assertNotEqual(merged, before)
                if later == "advance":
                    fixture.git("-c", "user.name=u", "-c", "user.email=u@x", "commit", "--allow-empty", "-qm", "user commit after merge")
                    izin.recover_delivery(fixture.store, rid)
                    self.assertEqual(fixture.store.read(rid)["delivery_state"]["merge_sha"], merged)
                    self.assertNotEqual(fixture.git("rev-parse", "HEAD"), merged)
                else:
                    fixture.git("reset", "-q", "--hard", before)
                    izin.recover_delivery(fixture.store, rid)
                    izin.recover_delivery(fixture.store, rid)
                    self.assertNotIn("merge_sha", fixture.store.read(rid).get("delivery_state") or {})
                    noted = izin.read_action(fixture.store, rid, action["token"])
                    self.assertEqual(noted["recovery"]["status"], "unrecorded")
                    self.assertEqual(noted["recovery"]["reason"], "operator-delivery-recovery-mismatch")
                    self.assertEqual(fixture.git("rev-parse", "HEAD"), before)

    def managed_fixture(self):
        fixture = importlib.import_module("test_kosu_engine").AdapterTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        repo, wt, parent, rid, env = fixture.project_tool_fixture()
        managed = fixture.home / "worktrees/a/fixture"
        managed.parent.mkdir(parents=True)
        izin.git(repo, "worktree", "move", str(wt), str(managed))
        fixture.store.update(rid, worktree=str(managed))
        return fixture, repo, managed, parent, rid, env

    def test_two_messages_bind_exact_action_and_consume_once(self) -> None:
        self.assertEqual(izin.policy(self.store, self.rid), izin.POLICY)
        effect = []
        with self.assertRaisesRegex(PermissionError, "operator-approval-required"):
            with izin.permitted(
                self.store,
                self.rid,
                self.action["token"],
                lambda: self.action["identity"],
            ):
                effect.append("unapproved")
        first = izin.approve(self.store, self.rid, self.action["token"])
        self.assertEqual(first["status"], "first-approved")
        self.assertIn("git -C /fixture commit -m fixture", first["display"])
        for confirmation in ("", "wrong"):
            with self.assertRaisesRegex(
                PermissionError, "operator-next-approval-required"
            ):
                izin.approve(self.store, self.rid, self.action["token"], confirmation)
        with self.assertRaisesRegex(PermissionError, "operator-approval-required"):
            with izin.permitted(
                self.store,
                self.rid,
                self.action["token"],
                lambda: self.action["identity"],
            ):
                effect.append("first-only")
        final = izin.approve(
            self.store, self.rid, self.action["token"], first["confirmation"]
        )
        self.assertEqual(len({message["id"] for message in final["messages"]}), 2)
        with izin.permitted(
            self.store, self.rid, self.action["token"], lambda: self.action["identity"]
        ):
            effect.append("approved")
        with self.assertRaisesRegex(PermissionError, "operator-approval-required"):
            with izin.permitted(
                self.store,
                self.rid,
                self.action["token"],
                lambda: self.action["identity"],
            ):
                effect.append("replayed")
        self.assertEqual(effect, ["approved"])

    def test_changed_command_target_product_or_policy_never_reuses_approval(
        self,
    ) -> None:
        for field in ("commands", "target", "product"):
            action = izin.propose(*self.args)
            self.approve(action)
            changed = copy.deepcopy(action["identity"])
            changed[field] = {"changed": True}
            with self.assertRaisesRegex(PermissionError, "operator-action-stale"):
                with izin.permitted(
                    self.store, self.rid, action["token"], lambda: changed
                ):
                    self.fail("changed action executed")
        fresh = izin.propose(*self.args)
        self.assertNotEqual(fresh["token"], self.action["token"])
        self.assertEqual(fresh["status"], "pending")
        from kosu import atomic_json

        atomic_json(
            self.store.path(self.rid) / "operator-policy.json",
            {**izin.POLICY, "secrets": "allow"},
        )
        with self.assertRaisesRegex(PermissionError, "operator-policy-invalid"):
            izin.propose(*self.args)

    def test_single_approval_actions_and_model_denial(self) -> None:
        for kind in ("install", "cleanup"):
            args = (*self.args[:2], kind, *self.args[3:])
            action = izin.propose(*args)
            with patch.dict(os.environ, CADRE_RUN_ID=self.rid):
                with self.assertRaisesRegex(
                    PermissionError, "operator-approval-command-required"
                ):
                    izin.approve(self.store, self.rid, action["token"])
            approved = izin.approve(self.store, self.rid, action["token"])
            self.assertEqual(approved["status"], "approved")
            with izin.permitted(
                self.store, self.rid, action["token"], lambda: action["identity"]
            ):
                pass
        action = izin.propose(*self.args[:2], "restore", *self.args[3:])
        self.assertEqual(action["required"], 0)
        with izin.permitted(
            self.store, self.rid, action["token"], lambda: action["identity"]
        ):
            pass

    def test_concurrent_execution_and_interruption_do_not_replay(self) -> None:
        self.approve()
        effect = []
        failures = []

        def execute() -> None:
            try:
                with izin.permitted(
                    self.store,
                    self.rid,
                    self.action["token"],
                    lambda: self.action["identity"],
                ):
                    effect.append("once")
            except PermissionError:
                failures.append("consumed")

        threads = [threading.Thread(target=execute) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(effect, ["once"])
        self.assertEqual(failures, ["consumed"] * 3)
        args = (*self.args[:2], "merge", *self.args[3:])
        action = izin.propose(*args)
        self.approve(action)
        with self.assertRaises(InterruptedError):
            with izin.permitted(
                self.store, self.rid, action["token"], lambda: action["identity"]
            ):
                raise InterruptedError("fixture interruption")
        self.assertEqual(
            izin.read_action(self.store, self.rid, action["token"])["status"],
            "uncertain",
        )
        self.assertEqual(izin.propose(*args)["token"], action["token"])

    def test_real_commit_path_preserves_index_until_two_approvals_and_rejects_changed_product(
        self,
    ) -> None:
        fixture, repo, wt, parent, rid, env = self.managed_fixture()
        store = fixture.store
        (wt / "app.py").write_text("value = 2\n")
        (wt / "new.py").write_text("value = 3\n")
        (wt / ".qa-sonuc.txt").write_text("fixture evidence\n")
        before = izin.git(wt, "rev-parse", "HEAD")
        index = Path(izin.git(wt, "rev-parse", "--git-path", "index"))
        index_bytes = index.read_bytes()
        action = izin.commit_proposal(store, rid, wt, "Fixture reviewed product")
        with self.assertRaisesRegex(PermissionError, "operator-approval-required"):
            izin.commit_execute(store, rid, action["token"])
        first = izin.approve(store, rid, action["token"])
        with self.assertRaisesRegex(PermissionError, "operator-approval-required"):
            izin.commit_execute(store, rid, action["token"])
        izin.approve(store, rid, action["token"], first["confirmation"])
        (wt / "app.py").write_text("value = 4\n")
        with self.assertRaisesRegex(PermissionError, "operator-action-stale"):
            izin.commit_execute(store, rid, action["token"])
        self.assertEqual(izin.git(wt, "rev-parse", "HEAD"), before)
        self.assertEqual(index.read_bytes(), index_bytes)
        fresh = izin.commit_proposal(store, rid, wt, "Fixture reviewed product")
        first = izin.approve(store, rid, fresh["token"])
        izin.approve(store, rid, fresh["token"], first["confirmation"])
        result = izin.commit_execute(store, rid, fresh["token"])
        self.assertNotEqual(result["revision"]["head"], before)
        self.assertEqual(result["files"], 2)
        self.assertEqual(izin.git(wt, "show", "HEAD:app.py"), "value = 4")
        self.assertEqual(izin.git(wt, "show", "HEAD:new.py"), "value = 3")
        self.assertNotIn(
            ".qa-sonuc.txt",
            izin.git(wt, "ls-tree", "-r", "--name-only", "HEAD").splitlines(),
        )
        self.assertEqual((repo / "app.py").read_text(), "value = 1\n")
        self.assertFalse(index.with_name(index.name + ".lock").exists())
        self.assertEqual(list(store.path(rid).glob("operator-commit-index-*")), [])
        with self.assertRaisesRegex(PermissionError, "operator-approval-required"):
            izin.commit_execute(store, rid, fresh["token"])

    def test_real_checkpoint_without_action_never_commits_or_stages(self) -> None:
        fixture, repo, wt, parent, rid, env = self.managed_fixture()
        (wt / "app.py").write_text("value = 2\n")
        before = izin.git(wt, "rev-parse", "HEAD")
        index = Path(izin.git(wt, "rev-parse", "--git-path", "index"))
        index_bytes = index.read_bytes()
        env.update(CADRE_NO_AUTO_COMMIT="0")
        env.pop("CADRE_COMMIT_ACTION", None)
        core = importlib.import_module("qe_core")
        with patch.dict(os.environ, env):
            self.assertEqual(core.commit_wip(str(wt), "a", "fixture checkpoint"), "")
        self.assertEqual(izin.git(wt, "rev-parse", "HEAD"), before)
        self.assertEqual(index.read_bytes(), index_bytes)
        self.assertEqual((wt / "app.py").read_text(), "value = 2\n")

    def test_unknown_git_hook_is_blocked_before_it_can_run(self) -> None:
        fixture, repo, wt, parent, rid, env = self.managed_fixture()
        hook = repo / ".git/hooks/pre-commit"
        marker = repo / "hook-ran"
        hook.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\n')
        hook.chmod(0o755)
        with self.assertRaisesRegex(PermissionError, "operator-git-hooks-untrusted"):
            izin.commit_proposal(fixture.store, rid, wt, "Fixture product")
        self.assertFalse(marker.exists())

    def test_producer_preserves_reviewed_worktree_and_requests_commit_permission(
        self,
    ) -> None:
        fixture = importlib.import_module("test_uctan_uca").E2EFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        rid = fixture.submit()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            record = fixture.store.read(rid)
            if record["state"] in ("completed", "failed", "retryable", "cancelled"):
                break
            time.sleep(0.2)
        log = (fixture.store.path(rid) / "coordinator.log").read_text()
        dispatch = (fixture.store.path(rid) / "dispatch.log").read_text()
        self.assertEqual(
            record["state"],
            "completed",
            str(record) + "\n" + log[-2500:] + "\n" + dispatch[-4500:],
        )
        self.assertEqual(
            record["final_result"]["delivery"]["git_permission"], "required"
        )
        token = record["final_result"]["delivery"]["action_token"]
        self.assertEqual(
            izin.read_action(fixture.store, rid, token)["status"], "pending"
        )
        self.assertTrue((Path(record["worktree"]) / "a.py").exists())

    def completed_fixture(self):
        fixture = importlib.import_module("test_gece_teslim").DeliveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        rid = fixture.submit()
        self.assertEqual(fixture.wait_terminal(rid)["state"], "completed")
        return fixture, rid

    def approve_in(self, store, rid, action):
        first = izin.approve(store, rid, action["token"])
        if first["required"] == 2:
            izin.approve(store, rid, action["token"], first["confirmation"])

    def test_merge_and_deferred_canonical_push_resume_without_another_merge(
        self,
    ) -> None:
        fixture, rid = self.completed_fixture()
        merged = fixture.cadre("merge", "e2e", rid, onay="evet", onay_push="hayir")
        self.assertEqual(merged.returncode, 0, merged.stderr + merged.stdout)
        record = fixture.store.read(rid)
        sha = fixture.git("rev-parse", "HEAD")
        self.assertEqual(record["delivery_state"]["merge_sha"], sha)
        self.assertEqual(record["delivery_state"]["canonical_push"], "pending")
        self.assertTrue(izin.delivery_candidate(record))
        resumed = fixture.cadre("merge", "e2e", rid, onay="evet")
        self.assertEqual(resumed.returncode, 0, resumed.stderr + resumed.stdout)
        self.assertEqual(fixture.git("rev-parse", "HEAD"), sha)
        self.assertEqual(
            fixture.store.read(rid)["delivery_state"]["canonical_push"], "completed"
        )
        self.assertEqual(izin.git(fixture.bare, "rev-parse", "main"), sha)
        self.assertFalse(izin.delivery_candidate(fixture.store.read(rid)))

    def test_merge_effect_and_record_write_failures_reconcile_without_replay(
        self,
    ) -> None:
        for failure in ("effect", "record"):
            with self.subTest(failure=failure):
                fixture, rid = self.completed_fixture()
                action = izin.delivery_proposal(fixture.store, rid, "merge")
                self.approve_in(fixture.store, rid, action)
                real_run = izin.subprocess.run
                real_json = izin.atomic_json

                def run(command, **kwargs):
                    result = real_run(command, **kwargs)
                    if "merge" in command:
                        raise InterruptedError("fixture post-effect interruption")
                    return result

                def write(path, value):
                    if Path(path).name == "run.json":
                        raise OSError("fixture metadata unavailable")
                    return real_json(path, value)

                context = (
                    patch.object(izin.subprocess, "run", side_effect=run)
                    if failure == "effect"
                    else patch.object(izin, "atomic_json", side_effect=write)
                )
                with context, self.assertRaises((InterruptedError, OSError)):
                    izin.delivery_execute(fixture.store, rid, action["token"])
                sha = fixture.git("rev-parse", "HEAD")
                self.assertNotEqual(sha, action["identity"]["target"]["head"])
                izin.recover_delivery(fixture.store, rid)
                izin.recover_delivery(fixture.store, rid)
                self.assertEqual(fixture.git("rev-parse", "HEAD"), sha)
                record = fixture.store.read(rid)
                self.assertEqual(record["delivery_state"]["merge_sha"], sha)
                self.assertEqual(record["delivery_state"]["canonical_push"], "pending")
                self.assertEqual(
                    izin.read_action(fixture.store, rid, action["token"])["status"],
                    "completed",
                )

    def test_commit_effect_before_index_publication_is_recovered_without_repeating_commit(
        self,
    ) -> None:
        fixture, repo, wt, parent, rid, env = self.managed_fixture()
        (wt / "app.py").write_text("value = 8\n")
        action = izin.commit_proposal(
            fixture.store, rid, wt, "Fixture interrupted commit"
        )
        self.approve_in(fixture.store, rid, action)
        original = izin.os.replace

        def replace(source, destination):
            if Path(destination).name == "index":
                raise OSError("fixture interrupted index publication")
            return original(source, destination)

        with (
            patch.object(izin.os, "replace", side_effect=replace),
            self.assertRaises(OSError),
        ):
            izin.commit_execute(fixture.store, rid, action["token"])
        sha = izin.git(wt, "rev-parse", "HEAD")
        self.assertNotEqual(sha, action["identity"]["target"]["head"])
        izin.reconcile_effect(fixture.store, rid, action["token"])
        self.assertEqual(izin.git(wt, "rev-parse", "HEAD"), sha)
        self.assertEqual(izin.git(wt, "status", "--porcelain"), "")
        self.assertTrue(
            izin.read_action(fixture.store, rid, action["token"])["reconciled"]
        )

    def test_sigkill_after_merge_reconciles_the_git_effect_after_restart(self) -> None:
        fixture, rid = self.completed_fixture()
        action = izin.delivery_proposal(fixture.store, rid, "merge")
        self.approve_in(fixture.store, rid, action)
        script = """import os,signal,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import izin
from kosu import RunStore
original=izin.subprocess.run
def run(command,**kwargs):
 result=original(command,**kwargs)
 if 'merge' in command and result.returncode==0:os.kill(os.getpid(),signal.SIGKILL)
 return result
izin.subprocess.run=run
izin.delivery_execute(RunStore(Path(sys.argv[2])),sys.argv[3],sys.argv[4])
"""
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(Path(izin.__file__).parent),
                str(fixture.home),
                rid,
                action["token"],
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env={**fixture.env, "CADRE_RUN_ID": ""},
        )
        self.assertEqual(child.returncode, -9, child.stdout + child.stderr)
        self.assertEqual(
            izin.read_action(fixture.store, rid, action["token"])["status"], "in-flight"
        )
        sha = fixture.git("rev-parse", "HEAD")
        izin.recover_delivery(fixture.store, rid)
        self.assertEqual(fixture.git("rev-parse", "HEAD"), sha)
        self.assertEqual(fixture.store.read(rid)["delivery_state"]["merge_sha"], sha)
        self.assertTrue(
            izin.read_action(fixture.store, rid, action["token"])["reconciled"]
        )

    def test_rejected_canonical_push_requires_fresh_approval_and_preserves_the_merge(
        self,
    ) -> None:
        fixture, rid = self.completed_fixture()
        merged = izin.delivery_proposal(fixture.store, rid, "merge")
        self.approve_in(fixture.store, rid, merged)
        result = izin.delivery_execute(fixture.store, rid, merged["token"])
        reject = fixture.bare / "hooks/pre-receive"
        reject.write_text("#!/bin/sh\nexit 1\n")
        reject.chmod(0o700)
        pushed = izin.delivery_proposal(fixture.store, rid, "canonical-push")
        self.approve_in(fixture.store, rid, pushed)
        with self.assertRaises(subprocess.CalledProcessError):
            izin.delivery_execute(fixture.store, rid, pushed["token"])
        self.assertEqual(
            izin.read_action(fixture.store, rid, pushed["token"])["status"], "uncertain"
        )
        self.assertEqual(fixture.git("rev-parse", "HEAD"), result["sha"])
        reject.unlink()
        retried = izin.delivery_proposal(fixture.store, rid, "canonical-push")
        self.assertNotEqual(retried["token"], pushed["token"])
        with self.assertRaises(PermissionError):
            izin.delivery_execute(fixture.store, rid, retried["token"])
        self.approve_in(fixture.store, rid, retried)
        izin.delivery_execute(fixture.store, rid, retried["token"])
        self.assertEqual(fixture.git("rev-parse", "HEAD"), result["sha"])
        self.assertEqual(izin.git(fixture.bare, "rev-parse", "main"), result["sha"])
        self.assertEqual(
            fixture.store.read(rid)["delivery_state"]["canonical_push"], "completed"
        )

    def test_archived_delivery_stays_visible_without_listing_side_effects(self) -> None:
        fixture, rid = self.completed_fixture()
        record = fixture.store.read(rid)
        wt = Path(record["worktree"])
        action = izin.cleanup_proposal(fixture.store, rid, wt)
        from rotasyon import rotate

        self.assertEqual(rotate(fixture.home, keep=0, run_id=rid)["retained"], 1)
        self.assertTrue(wt.exists())
        self.approve_in(fixture.store, rid, action)
        counts = rotate(fixture.home, keep=0, run_id=rid)
        self.assertEqual(
            counts["archived"],
            1,
            str(counts) + "\n" + str(izin.pending(fixture.store, rid)),
        )
        self.assertFalse(wt.exists())
        import arayuz_veri

        record = fixture.store.read(rid)
        self.assertTrue(izin.delivery_candidate(record))
        self.assertTrue(arayuz_veri.teslim_edilebilir(record))
        self.assertFalse(wt.exists())
        restored = izin.restore_proposal(fixture.store, rid)
        self.assertEqual(restored["required"], 0)
        izin.restore_execute(fixture.store, rid, restored["token"])
        self.assertTrue(wt.exists())
        self.assertTrue(
            importlib.import_module("sonuc").approved(fixture.store.read(rid))
        )
        delivered = fixture.cadre("push", "e2e", rid, onay="evet")
        self.assertEqual(delivered.returncode, 0, delivered.stderr + delivered.stdout)

    def test_model_macro_requires_two_messages_and_never_raises_its_cap_or_duration(
        self,
    ) -> None:
        from kosu import atomic_json

        action = izin.propose(
            self.store,
            self.rid,
            "model-calls",
            *izin.model_details(self.store, self.rid),
        )
        with self.assertRaises((PermissionError, ValueError)):
            izin.model_check(self.store, self.rid, "openrouter")
        self.approve(action)
        izin.model_activate(self.store, self.rid, action["token"])
        izin.model_check(self.store, self.rid, "openrouter")
        original = self.store.read(self.rid)["budget_cap_usd"]
        self.store.update(self.rid, budget_cap_usd=float(original) + 1)
        with self.assertRaises(PermissionError):
            izin.model_check(self.store, self.rid, "openrouter")
        self.store.update(self.rid, budget_cap_usd=original)
        with self.assertRaises(PermissionError):
            izin.model_check(self.store, self.rid, "claude")
        receipt = izin.read_action(self.store, self.rid, action["token"])
        receipt["finished"] = time.time() - 3601
        atomic_json(
            izin.scope(self.store, self.rid)
            / "operator-actions"
            / (action["token"] + ".json"),
            receipt,
        )
        with self.assertRaises(PermissionError):
            izin.model_check(self.store, self.rid, "openrouter")

    def test_panel_uses_the_same_two_separate_approval_transitions(self) -> None:
        python = Path(
            os.environ.get("CADRE_TEST_ARAYUZ_PYTHON")
            or str(Path.home() / ".local/share/cadre-ui-venv/bin/python")
        )
        self.assertTrue(
            python.exists(), "installed Textual fixture environment required"
        )
        script = """import asyncio,json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import arayuz,arayuz_veri,dil,izin
from kosu import RunStore
home=Path(sys.argv[2]);rid=sys.argv[3];token=sys.argv[4];store=RunStore(home)
async def run(lang):
 dil.kur(lang);app=arayuz.Cadre(home=home)
 async with app.run_test(size=(120,40)) as pilot:
  await pilot.pause()
  app.operator_approval(rid,token);await pilot.pause()
  assert isinstance(app.screen,arayuz.Onay)
  await pilot.pause(arayuz.Onay.OLU_SURE_SN+0.05);await pilot.press('enter');await pilot.pause()
  assert izin.read_action(store,rid,token)['status']=='pending','Enter tek basina yazili onay degildir'
  await pilot.press(*'evet','enter');await pilot.pause()
  assert izin.read_action(store,rid,token)['status']=='first-approved'
  assert isinstance(app.screen,arayuz.Onay)
  assert 'git -C /fixture commit' in app.screen.ayrinti
  await pilot.press('escape');await pilot.pause()
  assert izin.read_action(store,rid,token)['status']=='first-approved'
  app.operator_approval(rid,token);await pilot.pause(arayuz.Onay.OLU_SURE_SN+0.05)
  await pilot.press(*'yes','enter');await pilot.pause()
  assert izin.read_action(store,rid,token)['status']=='approved'
  assert len(izin.read_action(store,rid,token)['messages'])==2
  assert not dil.EKSIK,sorted(dil.EKSIK)
asyncio.run(run('en'))
print('panel-approval-pass')
"""
        result = subprocess.run(
            [
                str(python),
                "-c",
                script,
                str(Path(izin.__file__).parent),
                str(self.store.home),
                self.rid,
                self.action["token"],
            ],
            capture_output=True,
            text=True,
            timeout=40,
            env={**os.environ, "CADRE_RUN_ID": "", "CADRE_HOME": str(self.store.home)},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("panel-approval-pass", result.stdout)

    def test_backup_deletion_requires_one_fresh_approval_for_the_exact_file(
        self,
    ) -> None:
        import rotasyon

        folder = self.store.home / "backups/opencode-db"
        folder.mkdir(parents=True)
        files = [folder / f"opencode-202610{day:02d}.db.gz" for day in range(1, 6)]
        for path in files:
            path.write_bytes(b"owned synthetic fixture")
        rotasyon.cleanup_backups(self.store.home)
        self.assertTrue(all(path.exists() for path in files))
        action = rotasyon.backup_delete_proposal(self.store.home, files[0])
        izin.approve(self.store, "system", action["token"])
        files[0].write_bytes(b"changed fixture identity")
        with self.assertRaisesRegex(PermissionError, "operator-action-stale"):
            rotasyon.backup_delete_execute(self.store.home, action["token"])
        self.assertTrue(files[0].exists())
        action = rotasyon.backup_delete_proposal(self.store.home, files[0])
        izin.approve(self.store, "system", action["token"])
        rotasyon.backup_delete_execute(self.store.home, action["token"])
        self.assertFalse(files[0].exists())
        self.assertTrue(all(path.exists() for path in files[1:]))
        link = folder / "opencode-20261006.db.gz"
        link.symlink_to(files[1])
        with self.assertRaises(PermissionError):
            rotasyon.backup_delete_proposal(self.store.home, link)

    def test_manual_composed_tool_guard_allows_source_tools_and_rejects_shell_secrets_links_and_control(
        self,
    ) -> None:
        root = self.store.home / "manual-project"
        root.mkdir()
        (root / ".git").mkdir()
        (root / "app.py").write_text("value = 1\n")
        (root / ".env.fixture").write_text("synthetic_fixture_value\n")
        (root / ".env.example").write_text("SYNTHETIC_TEMPLATE_ONLY\n")
        (root / "alias.py").symlink_to(root / "app.py")
        os.link(root / "app.py", root / "hard.py")
        script = """import Cadre from './engine/lib/cadre-plugin.mjs';
const root=process.argv[1];const hooks=await Cadre({worktree:root,directory:root});
const check=async(tool,args,allowed)=>{let passed=true;try{await hooks['tool.execute.before']({tool},{args});}catch{passed=false;}
 if(passed!==allowed)throw new Error(tool+':'+JSON.stringify(args));};
await check('read',{filePath:root+'/app.py'},false);
await check('write',{filePath:root+'/new.py'},true);
await check('read',{filePath:root+'/.env.example'},true);
await check('bash',{command:'python3 -c "print(1)"'},false);
await check('webfetch',{url:'https://fixture.invalid'},false);
await check('read',{filePath:root+'/.env.fixture'},false);
await check('read',{filePath:root+'/alias.py'},false);
await check('read',{filePath:root+'/hard.py'},false);
await check('write',{filePath:root+'/opencode.jsonc'},false);
await check('read',{filePath:root+'/../runs'},false);
console.log('manual-tool-pass');
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script, str(root)],
            cwd=Path(izin.__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=30,
            env={
                **os.environ,
                "CADRE_PROJECT_TOOLS": "",
                "CADRE_HOME": str(self.store.home),
            },
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("manual-tool-pass", result.stdout)
        (root / "hard.py").unlink()
        command = [
            sys.executable,
            str(Path(izin.__file__).with_name("oturum.py")),
            "manual-check",
            str(root),
            str(root),
            "read",
        ]
        result = subprocess.run(
            command, input='{"filePath":"app.py"}', capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_initialization_requires_real_author_and_two_fresh_messages_without_duplicate_commits(
        self,
    ) -> None:
        import proje_kayit

        empty = self.store.home / "new-project"
        isolated = {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "CADRE_RUN_ID": "",
            "GIT_AUTHOR_NAME": "",
            "GIT_AUTHOR_EMAIL": "",
            "GIT_COMMITTER_NAME": "",
            "GIT_COMMITTER_EMAIL": "",
        }
        with patch.dict(os.environ, isolated):
            with self.assertRaises(proje_kayit.KayitHatasi):
                proje_kayit.kaydet(empty, olustur=True, home=self.store.home)
            self.assertFalse((empty / ".git").exists())
            with patch.dict(
                os.environ,
                GIT_AUTHOR_NAME="fixture owner",
                GIT_AUTHOR_EMAIL="fixture@local",
                GIT_COMMITTER_NAME="fixture owner",
                GIT_COMMITTER_EMAIL="fixture@local",
            ):
                with self.assertRaises(proje_kayit.KayitIzni) as requested:
                    proje_kayit.kaydet(empty, home=self.store.home)
                token = requested.exception.token
                first = izin.approve(self.store, "system", token)
                self.assertFalse((empty / ".git").exists())
                izin.approve(self.store, "system", token, first["confirmation"])
                changed = empty / "unapproved.txt"
                changed.write_text("preserve this product")
                with self.assertRaises(PermissionError):
                    izin.initialization_execute(self.store, token)
                self.assertFalse((empty / ".git").exists())
                self.assertEqual(changed.read_text(), "preserve this product")
                changed.unlink()
                fresh = izin.initialization_proposal(self.store, empty)
                first = izin.approve(self.store, "system", fresh["token"])
                izin.approve(
                    self.store, "system", fresh["token"], first["confirmation"]
                )
                izin.initialization_execute(self.store, fresh["token"])
                self.assertEqual(izin.git(empty, "rev-list", "--count", "HEAD"), "1")
                self.assertEqual(
                    izin.git(empty, "show", "-s", "--format=%an <%ae>", "HEAD"),
                    "fixture owner <fixture@local>",
                )
                with self.assertRaises(PermissionError):
                    izin.initialization_execute(self.store, fresh["token"])
                self.assertEqual(
                    proje_kayit.kaydet(empty, home=self.store.home), "new-project"
                )
                self.assertEqual(
                    proje_kayit.kaydet(empty, home=self.store.home), "new-project"
                )
                self.assertEqual(izin.git(empty, "rev-list", "--count", "HEAD"), "1")

    def test_initial_commit_cli_and_sigkill_recovery_never_duplicate_the_commit(
        self,
    ) -> None:
        from kosu import RunStore
        import proje_kayit

        home = self.store.home / "initial-cli-home"
        env = {
            **os.environ,
            "CADRE_HOME": str(home),
            "CADRE_RUN_ID": "",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "fixture owner",
            "GIT_AUTHOR_EMAIL": "fixture@local",
            "GIT_COMMITTER_NAME": "fixture owner",
            "GIT_COMMITTER_EMAIL": "fixture@local",
            "CADRE_ONAY": "evet",
            "CADRE_ONAY_2": "evet",
        }
        empty = self.store.home / "cli-project"
        command = [
            sys.executable,
            str(Path(izin.__file__).with_name("proje_kayit.py")),
            str(empty),
            "--olustur",
        ]
        denied = subprocess.run(
            command, input="", text=True, capture_output=True, env=env, timeout=15
        )
        self.assertEqual(denied.returncode, 1)
        self.assertIn("Çalışacak komut", denied.stdout, "FIX-05: komut ilk sorudan once gorunur")
        self.assertIn("git", denied.stdout)
        self.assertFalse((empty / ".git").exists())
        first = subprocess.run(
            command, input="evet\n", text=True, capture_output=True, env=env, timeout=15
        )
        self.assertEqual(first.returncode, 1)
        self.assertIn("Çalışacak komut", first.stdout)
        self.assertFalse((empty / ".git").exists())
        second = subprocess.run(
            command, input="evet\n", text=True, capture_output=True, env=env, timeout=15
        )
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(izin.git(empty, "rev-list", "--count", "HEAD"), "1")
        crashed = self.store.home / "crashed-project"
        crashed.mkdir()
        with patch.dict(os.environ, env):
            store = RunStore(home)
            action = izin.initialization_proposal(store, crashed)
            self.approve_in(store, "system", action)
            script = """import os,signal,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import izin
from kosu import RunStore
original=izin.subprocess.run
def run(command,**kwargs):
 result=original(command,**kwargs)
 if 'commit' in command and result.returncode==0:os.kill(os.getpid(),signal.SIGKILL)
 return result
izin.subprocess.run=run
izin.initialization_execute(RunStore(Path(sys.argv[2])),sys.argv[3])
"""
            child = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(Path(izin.__file__).parent),
                    str(home),
                    action["token"],
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
            )
            self.assertEqual(child.returncode, -9, child.stdout + child.stderr)
            self.assertEqual(
                izin.read_action(store, "system", action["token"])["status"],
                "in-flight",
            )
            sha = izin.git(crashed, "rev-parse", "HEAD")
            self.assertEqual(proje_kayit.kaydet(crashed, home=home), "crashed-project")
            self.assertEqual(proje_kayit.kaydet(crashed, home=home), "crashed-project")
            self.assertEqual(izin.git(crashed, "rev-parse", "HEAD"), sha)
            self.assertEqual(izin.git(crashed, "rev-list", "--count", "HEAD"), "1")
            self.assertTrue(
                izin.read_action(store, "system", action["token"])["reconciled"]
            )

    def test_initial_commit_hook_failure_is_recorded_without_closing_the_panel(
        self,
    ) -> None:
        python = Path(
            os.environ.get("CADRE_TEST_ARAYUZ_PYTHON")
            or str(Path.home() / ".local/share/cadre-ui-venv/bin/python")
        )
        self.assertTrue(python.exists())
        script = """import asyncio,json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import arayuz,dil,izin
from kosu import RunStore
home=Path(sys.argv[2]);store=RunStore(home);project=home/'new-ui-project';project.mkdir()
template=home/'template';(template/'hooks').mkdir(parents=True)
marker=home/'unexpected-hook'
hook=template/'hooks/pre-commit';hook.write_text('#!/bin/sh\\necho forbidden > '+str(marker)+'\\n');hook.chmod(0o700)
config=home/'synthetic-git-config';config.write_text('[init]\\n templateDir = '+str(template)+'\\n')
os.environ.update(GIT_CONFIG_GLOBAL=str(config),GIT_CONFIG_NOSYSTEM='1',GIT_AUTHOR_NAME='fixture',GIT_AUTHOR_EMAIL='fixture@local',GIT_COMMITTER_NAME='fixture',GIT_COMMITTER_EMAIL='fixture@local',CADRE_RUN_ID='',CADRE_HOME=str(home))
action=izin.initialization_proposal(store,project);dil.kur('en')
async def run():
 app=arayuz.Cadre(home=home)
 async with app.run_test(size=(120,40)) as pilot:
  await pilot.pause();app.operator_approval('system',action['token']);await pilot.pause()
  await pilot.pause(arayuz.Onay.OLU_SURE_SN+0.05);await pilot.press(*'evet','enter');await pilot.pause(arayuz.Onay.OLU_SURE_SN+0.05);await pilot.press(*'evet','enter');await pilot.pause()
  assert izin.read_action(store,'system',action['token'])['status']=='uncertain'
  assert not marker.exists()
  result=izin.subprocess.run(['git','-C',str(project),'rev-parse','--verify','HEAD'],capture_output=True)
  assert result.returncode!=0
  assert app.is_running
  await pilot.press('escape');await pilot.pause()
asyncio.run(run());print('initialization-ui-failure-pass')
"""
        result = subprocess.run(
            [
                str(python),
                "-c",
                script,
                str(Path(izin.__file__).parent),
                str(self.store.home),
            ],
            text=True,
            capture_output=True,
            timeout=40,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("initialization-ui-failure-pass", result.stdout)


if __name__ == "__main__":
    unittest.main()
