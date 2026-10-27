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

import test_kosu_sessions as fixtures  # noqa: E402,F401
from test_hizlandirma import GECER_HAKEM  # noqa: E402


def setUpModule():
    ortam = patch.dict(os.environ, {'CADRE_ZINCIR_KALITE': 'opencode-go/qwen3.8-max'})
    ortam.start()
    unittest.addModuleCleanup(ortam.stop)


YAZAR = """
import json, os, pathlib, sys, uuid
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
from kosu import RunStore
from izin import model_check
store = RunStore()
rid, gid = os.environ['CADRE_RUN_ID'], os.environ['CADRE_GOREV_ID']
provider, model = os.environ['CADRE_RUN_MODEL'].split('/', 1)
model_check(store, rid, provider, model)
sid=sys.argv[sys.argv.index('--session')+1] if '--session' in sys.argv else 'ses_'+uuid.uuid4().hex
print(json.dumps({'type':'step_start','sessionID':sid,'part':{'type':'step-start'}}),flush=True)
wt=pathlib.Path(os.environ['CADRE_WORKTREE'])
evidence=wt/'analiz.kanit.jsonl'
if os.environ.get('FIXTURE_NEDEN') == 'devam':
    assert 'TASLAK' in (wt/'analiz.md').read_text()
    assert evidence.is_file()
    with (wt/'analiz.md').open('a') as f:f.write('\\n# Bulgular\\nREADME dosyasinin ilk satiri x metnidir [K1].\\n')
    with open(os.environ['FIXTURE_ZAMAN'],'a') as f:f.write(json.dumps({'gid':gid,'model':os.environ.get('CADRE_RUN_MODEL',''),'continued':True})+'\\n')
    (pathlib.Path(os.environ['CADRE_HOME'])/'zincir/openrouter-fiyat.json').write_text(json.dumps({'version':2,'ts':__import__('time').time(),'modeller':{'openai/gpt-6.1-sol':{'fp8_cache':True,'providers':['openai'],'input':2,'output':10}}}))
    store.task_update(rid,gid,state='uretti')
    sys.exit(0)
evidence.write_text(json.dumps({'id':'K1','iddia':'README ilk satiri','tur':'dosya','kaynak':'README.md:1','beklenen':'x'})+'\\n')
(pathlib.Path(os.environ['CADRE_WORKTREE']) / 'analiz.md').write_text('# Ozet\\nTASLAK: yazar kesildi\\n')
deneme = store.task_dir(rid,gid)/'attempts'/os.environ['CADRE_BUDGET_ATTEMPT']
if os.environ.get('FIXTURE_NEDEN') == 'butce':
    (deneme / 'budget-denied.json').write_text(json.dumps({'provider': 'opencode-go'}))
with open(os.environ['FIXTURE_ZAMAN'], 'a') as f:
    f.write(json.dumps({'gid': gid, 'model': os.environ.get('CADRE_RUN_MODEL', '')}) + '\\n')
store.task_update(rid, gid, state='yarim')
sys.exit(1)
"""


class RaporYazarTukenmeTest(unittest.TestCase):
    def setUp(self):
        from kosu import RunStore

        t = Path(tempfile.mkdtemp(prefix="rapor-tukenme-"))
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
        (t / "projects/a/qa-komut").write_text("exit 0")
        (t / "projects/a/repo-path").write_text(str((self.t / "source").resolve()))
        istem = "TUR: rapor\nRAPOR: analiz.md\nBOLUMLER: Ozet; Bulgular\nREADME dosyasini incele ve rapor yaz.\n"
        self.rid = self.store.accept("a", istem, source_sha=g("rev-parse", "HEAD"))[
            "run_id"
        ]
        self.store.update(self.rid, worktree=str(self.wt))

    def kos(self, neden, *, devam=False, izin_ver=True):
        from kosu import execute_run, plan_ready
        from test_kosu_sessions import KATALOG
        from test_uctan_uca import FAKE_BUTCE

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
        (root / "lib/butce.py").write_text(FAKE_BUTCE)
        (root / "lib/common.sh").write_text(
            "qe_verify_ai_worktree(){ return 0; }\nqe_make_worktree(){ return 99; }\nqe_commit_wip(){ echo 0; }\n"
        )
        shutil.copyfile(
            LIB.parent / "bin/cadre-dispatch.sh", root / "bin/cadre-dispatch.sh"
        )
        writer = root / "fake-writer"
        writer.write_text(f"#!{sys.executable}\n" + YAZAR)
        writer.chmod(0o700)
        (root / "worker.py").write_text(
            "import os,sys,pathlib\nsys.path.insert(0,os.environ['CADRE_ENGINE_ROOT']+'/lib')\nimport qe_core as core\nroot=pathlib.Path(os.environ['CADRE_ENGINE_ROOT'])\np=core.run_opencode(str(root/'fake-writer'),os.environ['CADRE_WORKTREE'],['-m',os.environ['CADRE_RUN_MODEL'],'write report'],pathlib.Path(os.environ['CADRE_RUN_DIR'])/'writer.log',role='impl')\nraise SystemExit(p.wait(timeout=15))\n"
        )
        (root / "bin/is-motoru.sh").write_text(
            f'exec "{sys.executable}" "$CADRE_ENGINE_ROOT/worker.py"\n'
        )
        (self.t / "models.json").write_text(json.dumps(KATALOG))
        (self.t / "zincir").mkdir(exist_ok=True)
        plan = {
            "is_id": "c08",
            "kapsam_puani": 30,
            "kapsam_gerekce": "tek rapor",
            "gorevler": [
                {
                    "id": "g1",
                    "tanim": "raporu yaz",
                    "model": "auto",
                    "paralel_grup": 1,
                    "dosyalar": ["analiz.md", "analiz.kanit.jsonl"],
                    "kabul": "bolumler ve kanit",
                    "zorluk": 3,
                    "tahmin_dk": 1,
                    "tahmin_usd": 0.01,
                }
            ],
        }
        if not (self.store.path(self.rid) / "plan.json").exists():
            (self.store.path(self.rid) / "plan.json").write_text(json.dumps(plan))
            self.store.update(self.rid, phase="dispatch")
        hakem = self.t / "fake-reviewer"
        reviewer = GECER_HAKEM.replace(
            "print(json.dumps",
            "if q.get('rapor'):\n    from pathlib import Path\n    assert Path('README.md').read_text() == 'x\\n'\n    a['yoklama']=[{'id':i,'sonuc':'dogrulandi','kanit':'README.md ilk satiri x olarak okundu'} for i in q['rapor']['kanit_idleri']]\nprint(json.dumps",
        )
        hakem.write_text(f"#!{sys.executable}\n" + reviewer)
        hakem.chmod(0o700)
        with patch.dict(
            os.environ,
            {
                "CADRE_ENGINE_ROOT": str(root),
                "CADRE_REAL_OPENCODE": str(hakem),
                "OPENCODE_DB": str(self.t / "absent.db"),
                "FIXTURE_ZAMAN": str(self.t / "zaman"),
                "FIXTURE_NEDEN": neden,
                "CADRE_HOME": str(self.t),
                "CADRE_MODEL_KATALOG": str(self.t / "models.json"),
                "CADRE_BEKLEME_ESIK_SN": "0",
            },
        ):
            if izin_ver and not self.store.read(self.rid).get(
                "operator_model_permission"
            ):
                subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        """
import os, sys
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
import izin
from kosu import RunStore
store = RunStore()
rid = sys.argv[1]
action = izin.propose(store, rid, 'model-calls', *izin.model_details(store, rid))
first = izin.approve(store, rid, action['token'])
izin.approve(store, rid, action['token'], first['confirmation'])
izin.model_activate(store, rid, action['token'])
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
                    plan_ready(self.store, self.rid, str(hakem))
                finally:
                    self.store.update(self.rid, worktree=str(self.wt))
            rc = execute_run(self.store, self.rid, devam=devam)
        denemeler = (
            [json.loads(s) for s in (self.t / "zaman").read_text().splitlines()]
            if (self.t / "zaman").exists()
            else []
        )
        return (
            rc,
            self.store.read(self.rid),
            denemeler,
            (self.store.path(self.rid) / "dispatch.log").read_text(),
        )

    def test_son_deneme_butce_reddiyse_is_butce_beklemesine_gecer_taslak_korunur(self):
        rc, k, denemeler, log = self.kos("butce")
        self.assertNotEqual(rc, 0)
        self.assertGreaterEqual(
            len(denemeler), 2, "yazar zincirinin sonraki halkasi denenir"
        )
        self.assertEqual(k["state"], "retryable", log[-3000:])
        self.assertTrue(
            str(k.get("reason") or "").startswith("butce-bekleme"), k.get("reason")
        )
        self.assertGreater(float(k.get("retry_after") or 0), 0)
        self.assertNotEqual(
            (k.get("final_result") or {}).get("status"), "PASS", "sahte tamamlanma yok"
        )
        self.assertIn("TASLAK", (self.wt / "analiz.md").read_text(), "taslak korunur")

    def test_model_arizasi_butce_beklemesine_donusturulmez(self):
        rc, k, denemeler, log = self.kos("model")
        self.assertNotEqual(rc, 0)
        self.assertTrue(denemeler)
        self.assertFalse(
            str(k.get("reason") or "").startswith("butce-bekleme"),
            (k.get("reason"), log[-3000:]),
        )
        self.assertNotEqual((k.get("final_result") or {}).get("status"), "PASS")
        self.assertIn("TASLAK", (self.wt / "analiz.md").read_text())

    def test_kapasite_donusunde_ayni_taslak_kanit_ve_oturumla_son_kabul(self):
        rc, first, calls, log = self.kos("butce")
        self.assertNotEqual(rc, 0)
        self.assertEqual(first["state"], "retryable", log[-3000:])
        draft = (self.wt / "analiz.md").read_bytes()
        evidence = (self.wt / "analiz.kanit.jsonl").read_bytes()
        plan = (self.store.path(self.rid) / "plan.json").read_bytes()
        prompt = Path(first["prompt"]).read_bytes()
        plan_revision = self.store.plan_view(self.rid)["revision"]
        sessions = {
            sid
            for executor in first["tasks"]["g1"]["executors"].values()
            for sid in executor["sessions"]
        }
        rc, waiting, early_calls, _ = self.kos("devam", devam=True)
        self.assertEqual(rc, 73)
        self.assertEqual(waiting["state"], "retryable")
        self.assertEqual(early_calls, calls)
        self.assertEqual((self.wt / "analiz.md").read_bytes(), draft)
        self.assertEqual((self.wt / "analiz.kanit.jsonl").read_bytes(), evidence)
        deadline = float(first["retry_after"])
        while time.time() <= deadline:
            time.sleep(min(0.1, max(0.01, deadline - time.time())))
        rc, last, continued, log = self.kos("devam", devam=True)
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
        self.assertTrue((self.wt / "analiz.md").read_bytes().startswith(draft))
        self.assertEqual((self.wt / "analiz.kanit.jsonl").read_bytes(), evidence)
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
        self.assertFalse(((self.t / "source") / "analiz.md").exists())
        completed = (self.wt / "analiz.md").read_bytes()
        rc, repeated, repeated_calls, _ = self.kos("devam", devam=True)
        self.assertEqual(rc, 0)
        self.assertEqual(repeated["final_result"], final)
        self.assertEqual(repeated_calls, continued)
        self.assertEqual((self.wt / "analiz.md").read_bytes(), completed)

    def test_kapasite_donusu_kayip_model_iznini_asmaz(self):
        _, first, calls, _ = self.kos("butce")
        self.assertEqual(first["state"], "retryable")
        draft = (self.wt / "analiz.md").read_bytes()
        evidence = (self.wt / "analiz.kanit.jsonl").read_bytes()
        self.store.update(self.rid, operator_model_permission="", retry_after=0)
        rc, last, continued, _ = self.kos("devam", devam=True, izin_ver=False)
        self.assertNotEqual(rc, 0)
        self.assertEqual(continued, calls)
        self.assertNotEqual((last.get("final_result") or {}).get("status"), "PASS")
        self.assertEqual((self.wt / "analiz.md").read_bytes(), draft)
        self.assertEqual((self.wt / "analiz.kanit.jsonl").read_bytes(), evidence)


if __name__ == "__main__":
    unittest.main()
