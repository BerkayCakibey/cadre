"""G-C14 (23.09 live T1v2 finding): the feedloom worktree's `.venv` did not contain the optional `browser` extra (playwright);
`basedpyright` went red independently of the product, and the system blamed the product and started a local repair attempt. Contract:
(1) if `projects/<p>/install-command` exists it runs once in the worktree before the run starts; on failure the run stops with `install-error`
(permanent: no retry). (2) The QA command runs once while there is no product; its result is recorded as INFORMATION (the baseline may be red —
end-to-end acceptance commands are red before the product), a warning is added to the repair prompt; it does not stop the run. Both once per run
(marker file). No model/network."""

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


class BasePreparationTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="base-"))
        self.wt = self.d / "wt"
        self.wt.mkdir()
        subprocess.run(["git", "-C", str(self.wt), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.wt), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "empty"], check=True)
        self.project_ref_dir = self.d / "projects" / "p"
        self.project_ref_dir.mkdir(parents=True)
        self.logf = self.d / "runner.log"
        self.logf.write_text("")
        self.mark = self.d / "base-qa.json"
        self.store = run_store.RunStore(self.d)
        self.rid = self.store.accept('p', 'fixture baseline')['run_id']
        self.store.update(self.rid, worktree=str(self.wt))
        environment = patch.dict(os.environ, {**self.store.environment(self.rid), 'CADRE_ENGINE_ROOT': str(LIB.parent)})
        environment.start()
        self.addCleanup(environment.stop)
        home = patch.object(core, 'QE', self.d)
        home.start()
        self.addCleanup(home.stop)

    def authorize_setup(self):
        import permission
        action = permission.setup_proposal(self.store, self.rid, self.wt, (self.project_ref_dir / 'install-command').read_text().strip())
        with patch.dict(os.environ, CADRE_RUN_ID=''):
            permission.approve(self.store, self.rid, action['token'])

    def prepare(self, install=None, qa=None):
        if install is not None:
            (self.project_ref_dir / "install-command").write_text(install + "\n")
        if qa is not None:
            (self.project_ref_dir / "qa-command").write_text(qa + "\n")
        if install is not None:
            self.authorize_setup()
        return core.base_preparation("p", self.wt, self.logf, self.mark, self.project_ref_dir)

    def test_install_runs_and_a_red_base_qa_is_recorded_as_information(self):
        r = self.prepare(install="echo install > install.txt", qa="exit 2")
        self.assertEqual(r["install"]["status"], "PASS")
        self.assertTrue((self.wt / "install.txt").exists())
        self.assertEqual((r["qa"]["status"], r["qa"]["exit_code"]), ("FAIL", 2))
        self.assertEqual(r["status"], "PASS", "A baseline QA failure DOES NOT STOP the run; QA may already fail before the product exists")
        self.assertEqual(json.loads(self.mark.read_text())["qa"]["status"], "FAIL")
        self.assertIn("BASE QA: FAIL", self.logf.read_text())
        from unittest.mock import patch
        from watch import Watcher
        store = run_store.RunStore(self.d)
        parent = store.accept('parent', 'project')['run_id']
        rid = store.accept('p', 'feature')['run_id']
        store.update(rid, worktree=str(self.wt), project_ref_parent=parent)
        (self.wt / 'uv.lock').touch()
        run_store.base_qa_save(store, rid, r)
        for project in ('p', 'parent'):
            self.assertIn('BASE QA FAILED', run_store.summary(store, project))
            self.assertIn('uv lock', run_store.summary(store, project))
        watcher = Watcher('p', str(self.wt), plain=True, single=True, run_id=rid)
        with patch.object(watcher, 'write') as write:
            watcher.base_warn(store.read(rid))
            watcher.base_warn(store.read(rid))
            self.assertEqual(write.call_count, 1, 'Do not flood logs with the same warning every round')
        self.assertEqual(store.read(rid)['baseline_warning']['exit_code'], 2)
        run_store.base_qa_save(store, rid, {'qa': {'status': 'PASS'}})
        self.assertNotIn('BASE QA FAILED', run_store.summary(store, 'parent'))

    def test_install_error_stops_the_run_and_qa_does_not_run(self):
        r = self.prepare(install="exit 3", qa="echo QA-RAN > qa.txt")
        self.assertEqual((r["status"], r["reason"]), ("FAIL", "install-error"))
        self.assertEqual(r["install"]["exit_code"], 3)
        self.assertFalse((self.wt / "qa.txt").exists())
        self.assertIn("install-error", run_store.PERMANENT_REASONS)

    def test_install_error_is_retried_with_a_new_approval_waits_without_one(self):
        import permission
        calls = []

        def fake_command(wt, command, output, moment_overrun):
            calls.append(command)
            Path(output).write_text("fixture\n")
            return 7 if len(calls) == 1 else 0
        (self.project_ref_dir / "install-command").write_text("make install\n")
        self.authorize_setup()
        with patch.object(core, "_base_command", side_effect=fake_command):
            r = core.base_preparation("p", self.wt, self.logf, self.mark, self.project_ref_dir)
            self.assertEqual((r["status"], r["reason"], len(calls)), ("FAIL", "install-error", 1))
            again = core.base_preparation("p", self.wt, self.logf, self.mark, self.project_ref_dir)
            self.assertEqual((again["status"], again["reason"], len(calls)), ("FAIL", "install-error", 1))
            self.assertEqual(again["install"]["retry"]["status"], "pending")
            token = again["install"]["retry"]["action_token"]
            self.assertEqual(permission.read_action(self.store, self.rid, token)["identity"]["generation"], 2)
            self.assertIn("again attempt approval waiting", self.logf.read_text())
            with patch.dict(os.environ, CADRE_RUN_ID=""):
                permission.approve(self.store, self.rid, token)
            fixed = core.base_preparation("p", self.wt, self.logf, self.mark, self.project_ref_dir)
        self.assertEqual((fixed["status"], fixed["install"]["status"], len(calls)), ("PASS", "PASS", 2))
        self.assertEqual(json.loads(self.mark.read_text())["install"]["status"], "PASS")
        self.assertEqual(permission.read_action(self.store, self.rid, token)["status"], "completed")

    def test_skipped_without_a_command(self):
        r = self.prepare()
        self.assertEqual((r["install"]["status"], r["qa"]["status"], r["status"]), ("SKIPPED", "SKIPPED", "PASS"))

    def test_marker_file_once_per_run(self):
        self.prepare(install="echo x >> counter.txt", qa="exit 0")
        self.prepare()
        self.assertEqual((self.wt / "counter.txt").read_text().count("x"), 1)

    def test_parallel_lanes_run_the_install_and_the_base_qa_once(self):
        """B223 (25.09 live R10-hard): two parallel lanes started in the same worktree at the same time; both saw no marker and
        ran `uv sync` and `make check` twice concurrently. The second lane waits for the first to finish and shares the result."""
        import threading
        scratch = self.wt / '.pytest_cache'
        scratch.mkdir()
        setup_count, qa_count = scratch / 'install-counter', scratch / 'qa-counter'
        (self.project_ref_dir / "install-command").write_text(f"echo k >> {setup_count}; sleep 1\n")
        (self.project_ref_dir / "qa-command").write_text(f"echo q >> {qa_count}\n")
        self.authorize_setup()
        outcomes = []
        lanes = [threading.Thread(target=lambda: outcomes.append(
            core.base_preparation("p", self.wt, self.logf, self.mark, self.project_ref_dir))) for _ in range(2)]
        for s in lanes:
            s.start()
        for s in lanes:
            s.join(timeout=30)
        self.assertEqual(setup_count.read_text().split(), ['k'])
        self.assertEqual(qa_count.read_text().split(), ['q'])
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(outcomes[0]["install"], outcomes[1]["install"])

    def test_base_qa_is_skipped_if_the_product_already_changed(self):
        (self.wt / "product.py").write_text("x = 1\n")
        r = self.prepare(qa="exit 2")
        self.assertEqual((r["qa"]["status"], r["qa"]["reason"]), ("SKIPPED", "has-product"))

    def test_carried_source_image_does_not_count_as_a_product_and_an_old_unmeasured_marker_is_renewed(self):
        """C16/N-ENV03 (21.10 live request-9c7a9d76): an uncommitted patch carried from the active branch makes the worktree dirty; the engine took that as
        "there is a product", skipped baseline QA and the start gate dropped the run. The accepted source snapshot (HEAD + the carried patch)
        is the baseline itself: while the worktree is identical to the snapshot baseline QA runs; a change deviating from the snapshot still counts as product.
        An old marker that took no measurement (has-product) is re-evaluated; a measured marker is kept."""
        (self.wt / "product.py").write_text("x = 0\n")
        subprocess.run(["git", "-C", str(self.wt), "add", "product.py"], check=True)
        subprocess.run(["git", "-C", str(self.wt), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "source"], check=True)
        (self.wt / "product.py").write_text("x = 1\n")
        (self.wt / "fresh.txt").write_text("carried\n")
        source = self.d / "source"
        (source / "untracked").mkdir(parents=True)
        (source / "change.patch").write_bytes(subprocess.run(["git", "-C", str(self.wt), "diff", "--binary", "--no-ext-diff", "HEAD", "--"],
                                                                 check=True, capture_output=True).stdout)
        (source / "untracked/fresh.txt").write_text("carried\n")
        image = {"sha": subprocess.run(["git", "-C", str(self.wt), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip(),
                   "branch": "cr35-acceptance", "carried": True, "dirty_files": 2, "patch": str(source / "change.patch"), "untracked": str(source / "untracked")}
        self.store.update(self.rid, source=image)
        (self.wt / "product.py").write_text("x = 2\n")   # a change deviating from the snapshot = product
        r = self.prepare(qa="exit 0")
        self.assertEqual((r["status"], r["reason"], r["qa"]["reason"]), ("FAIL", "baseline-unavailable", "has-product"))
        (self.wt / "product.py").write_text("x = 1\n")   # identical to the snapshot
        r = self.prepare()
        self.assertEqual((r["status"], r["qa"]["status"], r["qa"]["exit_code"]), ("PASS", "PASS", 0), r)
        self.assertIn("matches the accepted source snapshot", self.logf.read_text())
        self.assertEqual(json.loads(self.mark.read_text())["qa"]["status"], "PASS")
        r2 = self.prepare()
        self.assertEqual(r2["qa"]["status"], "PASS", "A measured marker is not measured again")
        self.assertEqual(self.logf.read_text().count("BASE QA:"), 1)

    def test_base_warning_only_while_the_base_is_red(self):
        self.assertEqual(core.base_warning(self.mark), "")
        self.prepare(qa="exit 2")
        self.assertIn("BASELINE ALSO FAILED", core.base_warning(self.mark))
        self.assertIn("rc=2", core.base_warning(self.mark))


if __name__ == "__main__":
    unittest.main()
