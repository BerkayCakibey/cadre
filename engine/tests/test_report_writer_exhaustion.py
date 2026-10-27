"""C08: after the report writer starts, all budgeted routes run out; the dispatcher end to end, with a fake model and budget."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / "lib"

import test_run_sessions as fixtures  # noqa: E402,F401
from test_speedup import PASS_JUDGE  # noqa: E402


def setUpModule():
    environment = patch.dict(os.environ, {'CADRE_CHAIN_QUALITY': 'opencode-go/qwen3.8-max'})
    environment.start()
    unittest.addModuleCleanup(environment.stop)


WRITER = """
import json, os, pathlib, sys, uuid
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
from run_store import RunStore
from permission import model_check
store = RunStore()
rid, gid = os.environ['CADRE_RUN_ID'], os.environ['CADRE_TASK_ID']
provider, model = os.environ['CADRE_RUN_MODEL'].split('/', 1)
model_check(store, rid, provider, model)
sid=sys.argv[sys.argv.index('--session')+1] if '--session' in sys.argv else 'ses_'+uuid.uuid4().hex
print(json.dumps({'type':'step_start','sessionID':sid,'part':{'type':'step-start'}}),flush=True)
wt=pathlib.Path(os.environ['CADRE_WORKTREE'])
evidence=wt/'analysis.evidence.jsonl'
if os.environ.get('FIXTURE_REASON') == 'proceed':
    assert 'DRAFT' in (wt/'analysis.md').read_text()
    assert evidence.is_file()
    with (wt/'analysis.md').open('a') as f:f.write('\\n# Findings\\nThe first README line contains x [K1].\\n')
    with open(os.environ['FIXTURE_MOMENT'],'a') as f:f.write(json.dumps({'gid':gid,'model':os.environ.get('CADRE_RUN_MODEL',''),'continued':True})+'\\n')
    (pathlib.Path(os.environ['CADRE_HOME'])/'chain/openrouter-price.json').write_text(json.dumps({'version':2,'ts':__import__('time').time(),'models':{'openai/gpt-6.1-sol':{'fp8_cache':True,'providers':['openai'],'input':2,'output':10}}}))
    store.task_update(rid,gid,state='produced')
    sys.exit(0)
evidence.write_text(json.dumps({'id':'K1','claim':'first README line','round':'file','source':'README.md:1','expected':'x'})+'\\n')
(pathlib.Path(os.environ['CADRE_WORKTREE']) / 'analysis.md').write_text('# Summary\\nDRAFT: writer interrupted\\n')
attempt = store.task_dir(rid,gid)/'attempts'/os.environ['CADRE_BUDGET_ATTEMPT']
if os.environ.get('FIXTURE_REASON') == 'budget':
    (attempt / 'budget-denied.json').write_text(json.dumps({'provider': 'opencode-go'}))
with open(os.environ['FIXTURE_MOMENT'], 'a') as f:
    f.write(json.dumps({'gid': gid, 'model': os.environ.get('CADRE_RUN_MODEL', '')}) + '\\n')
store.task_update(rid, gid, state='half')
sys.exit(1)
"""


class ReportWriterExhaustionTest(unittest.TestCase):
    def setUp(self):
        from run_store import RunStore

        t = Path(tempfile.mkdtemp(prefix="report-exhaustion-"))
        self.addCleanup(shutil.rmtree, t, True)
        self.t = t
        self.wt = t / "wt"
        (self.t / "source").mkdir()

        def g(*args: str) -> str:
            return subprocess.run(
                [
                    "git",
                    "-C",
                    str((self.t / "source")),
                    "-c",
                    "user.name=f",
                    "-c",
                    "user.email=f@l",
                    *args,
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        g("init", "-q")
        ((self.t / "source") / "README.md").write_text("x\n")
        g("add", ".")
        g("commit", "-qm", "base")
        g("worktree", "add", "-q", "-b", "ai/c08", str(self.wt.resolve()))
        self.store = RunStore(t)
        (t / "projects/a").mkdir(parents=True)
        (t / "projects/a/qa-command").write_text("exit 0")
        (t / "projects/a/repo-path").write_text(str((self.t / "source").resolve()))
        prompt = "ROUND: report\nREPORT: analysis.md\nSECTIONS: Summary; Findings\nReview README and write a report.\n"
        self.rid = self.store.accept("a", prompt, source_sha=g("rev-parse", "HEAD"))[
            "run_id"
        ]
        self.store.update(self.rid, worktree=str(self.wt))

    def execute_run(self, reason, *, proceed=False, grant_permission=True):
        from run_store import execute_run, plan_ready
        from test_run_sessions import CATALOG
        from test_end_to_end import FAKE_BUDGET

        root = self.t / "dispatcher"
        from qa_fixture import instructions

        instructions(root)
        shutil.copytree(
            LIB,
            root / "lib",
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        (root / "bin").mkdir(exist_ok=True)
        (root / "lib/provider_budget.py").write_text(FAKE_BUDGET)
        (root / "lib/common.sh").write_text(
            "qe_verify_ai_worktree(){ return 0; }\nqe_make_worktree(){ return 99; }\nqe_commit_wip(){ echo 0; }\n"
        )
        shutil.copyfile(
            LIB.parent / "bin/cadre-dispatch.sh", root / "bin/cadre-dispatch.sh"
        )
        writer = root / "fake-writer"
        writer.write_text(f"#!{sys.executable}\n" + WRITER)
        writer.chmod(0o700)
        (root / "worker.py").write_text(
            "import os,sys,pathlib\nsys.path.insert(0,os.environ['CADRE_ENGINE_ROOT']+'/lib')\nimport qe_core as core\nroot=pathlib.Path(os.environ['CADRE_ENGINE_ROOT'])\np=core.run_opencode(str(root/'fake-writer'),os.environ['CADRE_WORKTREE'],['-m',os.environ['CADRE_RUN_MODEL'],'write report'],pathlib.Path(os.environ['CADRE_RUN_DIR'])/'writer.log',role='impl')\nraise SystemExit(p.wait(timeout=15))\n"
        )
        (root / "bin/job-runner.sh").write_text(
            f'exec "{sys.executable}" "$CADRE_ENGINE_ROOT/worker.py"\n'
        )
        (self.t / "models.json").write_text(json.dumps(CATALOG))
        (self.t / "chain").mkdir(exist_ok=True)
        plan = {
            "job_id": "c08",
            "scope_points": 30,
            "scope_rationale": "single report",
            "tasks": [
                {
                    "id": "g1",
                    "definition": "write the report",
                    "model": "auto",
                    "parallel_group": 1,
                    "files": ["analysis.md", "analysis.evidence.jsonl"],
                    "acceptance": "sections and evidence",
                    "difficulty": 3,
                    "estimate_min": 1,
                    "estimate_usd": 0.01,
                }
            ],
        }
        if not (self.store.path(self.rid) / "plan.json").exists():
            (self.store.path(self.rid) / "plan.json").write_text(json.dumps(plan))
            self.store.update(self.rid, phase="dispatch")
        judge = self.t / "fake-reviewer"
        reviewer = PASS_JUDGE.replace(
            "print(json.dumps",
            "if q.get('report'):\n    from pathlib import Path\n    assert Path('README.md').read_text() == 'x\\n'\n    a['poll']=[{'id':i,'outcome':'verified','evidence':'Read x from the first line of README.md'} for i in q['report']['evidence_ids']]\nprint(json.dumps",
        )
        judge.write_text(f"#!{sys.executable}\n" + reviewer)
        judge.chmod(0o700)
        with patch.dict(
            os.environ,
            {
                "CADRE_ENGINE_ROOT": str(root),
                "CADRE_REAL_OPENCODE": str(judge),
                "OPENCODE_DB": str(self.t / "absent.db"),
                "FIXTURE_MOMENT": str(self.t / "moment"),
                "FIXTURE_REASON": reason,
                "CADRE_HOME": str(self.t),
                "CADRE_MODEL_CATALOG": str(self.t / "models.json"),
                "CADRE_WAIT_THRESHOLD_SEC": "0",
            },
        ):
            if grant_permission and not self.store.read(self.rid).get(
                "operator_model_permission"
            ):
                subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        """
import os, sys
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
import permission
from run_store import RunStore
store = RunStore()
rid = sys.argv[1]
action = permission.propose(store, rid, 'model-calls', *permission.model_details(store, rid))
first = permission.approve(store, rid, action['token'])
permission.approve(store, rid, action['token'], first['confirmation'])
permission.model_activate(store, rid, action['token'])
""",
                        self.rid,
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            if not self.store.plan_view(self.rid)["revision"]:
                self.store.update(self.rid, worktree="")
                try:
                    plan_ready(self.store, self.rid, str(judge))
                finally:
                    self.store.update(self.rid, worktree=str(self.wt))
            rc = execute_run(self.store, self.rid, proceed=proceed)
        attempts = (
            [json.loads(s) for s in (self.t / "moment").read_text().splitlines()]
            if (self.t / "moment").exists()
            else []
        )
        return (
            rc,
            self.store.read(self.rid),
            attempts,
            (self.store.path(self.rid) / "dispatch.log").read_text(),
        )

    def test_if_the_last_attempt_is_a_budget_rejection_the_job_moves_to_budget_wait_the_draft_is_kept(self):
        rc, k, attempts, log = self.execute_run("budget")
        self.assertNotEqual(rc, 0)
        self.assertGreaterEqual(
            len(attempts), 2, "Try the next link in the writer chain"
        )
        self.assertEqual(k["state"], "retryable", log[-3000:])
        self.assertTrue(
            str(k.get("reason") or "").startswith("budget-wait"), k.get("reason")
        )
        self.assertGreater(float(k.get("retry_after") or 0), 0)
        self.assertNotEqual(
            (k.get("final_result") or {}).get("status"), "PASS", "Do not fabricate completion"
        )
        self.assertIn("DRAFT", (self.wt / "analysis.md").read_text(), "Draft preserved")

    def test_model_failure_is_not_turned_into_a_budget_wait(self):
        rc, k, attempts, log = self.execute_run("model")
        self.assertNotEqual(rc, 0)
        self.assertTrue(attempts)
        self.assertFalse(
            str(k.get("reason") or "").startswith("budget-wait"),
            (k.get("reason"), log[-3000:]),
        )
        self.assertNotEqual((k.get("final_result") or {}).get("status"), "PASS")
        self.assertIn("DRAFT", (self.wt / "analysis.md").read_text())

    def test_on_capacity_return_final_acceptance_with_the_same_draft_evidence_and_session(self):
        rc, first, calls, log = self.execute_run("budget")
        self.assertNotEqual(rc, 0)
        self.assertEqual(first["state"], "retryable", log[-3000:])
        draft = (self.wt / "analysis.md").read_bytes()
        evidence = (self.wt / "analysis.evidence.jsonl").read_bytes()
        plan = (self.store.path(self.rid) / "plan.json").read_bytes()
        prompt = Path(first["prompt"]).read_bytes()
        plan_revision = self.store.plan_view(self.rid)["revision"]
        sessions = {
            sid
            for executor in first["tasks"]["g1"]["executors"].values()
            for sid in executor["sessions"]
        }
        rc, waiting, early_calls, _ = self.execute_run("proceed", proceed=True)
        self.assertEqual(rc, 73)
        self.assertEqual(waiting["state"], "retryable")
        self.assertEqual(early_calls, calls)
        self.assertEqual((self.wt / "analysis.md").read_bytes(), draft)
        self.assertEqual((self.wt / "analysis.evidence.jsonl").read_bytes(), evidence)
        deadline = float(first["retry_after"])
        while time.time() <= deadline:
            time.sleep(min(0.1, max(0.01, deadline - time.time())))
        rc, last, continued, log = self.execute_run("proceed", proceed=True)
        self.assertEqual(rc, 0, log[-5000:])
        self.assertEqual(last["run_id"], first["run_id"])
        self.assertEqual(last["state"], "completed")
        final = last["final_result"]
        self.assertEqual(final["status"], "PASS")
        self.assertEqual(final["qa"]["status"], "PASS")
        self.assertEqual(final["review"]["status"], "PASS")
        self.assertTrue(final["review"]["independent"])
        self.assertEqual(final["qa"]["revision"], final["revision"])
        self.assertEqual(final["review"]["revision"], final["revision"])
        self.assertEqual(self.store.plan_view(self.rid)["revision"], plan_revision)
        self.assertEqual((self.store.path(self.rid) / "plan.json").read_bytes(), plan)
        self.assertEqual(Path(last["prompt"]).read_bytes(), prompt)
        self.assertTrue((self.wt / "analysis.md").read_bytes().startswith(draft))
        self.assertEqual((self.wt / "analysis.evidence.jsonl").read_bytes(), evidence)
        self.assertEqual(len(continued), len(calls) + 1)
        executors = last["tasks"]["g1"]["executors"].values()
        self.assertEqual(
            {sid for executor in executors for sid in executor["sessions"]}, sessions
        )
        attempts = list(
            (self.store.task_dir(self.rid, "g1") / "attempts").glob("*/attempt.json")
        )
        self.assertTrue(
            json.loads(
                max(
                    attempts, key=lambda path: json.loads(path.read_text())["created"]
                ).read_text()
            ).get("session_id")
            in sessions
        )
        self.assertEqual(((self.t / "source") / "README.md").read_text(), "x\n")
        self.assertFalse(((self.t / "source") / "analysis.md").exists())
        completed = (self.wt / "analysis.md").read_bytes()
        rc, repeated, repeated_calls, _ = self.execute_run("proceed", proceed=True)
        self.assertEqual(rc, 0)
        self.assertEqual(repeated["final_result"], final)
        self.assertEqual(repeated_calls, continued)
        self.assertEqual((self.wt / "analysis.md").read_bytes(), completed)

    def test_capacity_return_does_not_exceed_the_lost_model_permission(self):
        _, first, calls, _ = self.execute_run("budget")
        self.assertEqual(first["state"], "retryable")
        draft = (self.wt / "analysis.md").read_bytes()
        evidence = (self.wt / "analysis.evidence.jsonl").read_bytes()
        self.store.update(self.rid, operator_model_permission="", retry_after=0)
        rc, last, continued, _ = self.execute_run("proceed", proceed=True, grant_permission=False)
        self.assertNotEqual(rc, 0)
        self.assertEqual(continued, calls)
        self.assertNotEqual((last.get("final_result") or {}).get("status"), "PASS")
        self.assertEqual((self.wt / "analysis.md").read_bytes(), draft)
        self.assertEqual((self.wt / "analysis.evidence.jsonl").read_bytes(), evidence)


if __name__ == "__main__":
    unittest.main()
