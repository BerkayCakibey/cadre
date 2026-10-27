"""C5 integration checks: real adapters, temporary state and fake executables."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import shutil
import signal
import shlex
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import qe_core as core
from kosu import RunStore


def setUpModule():
    ortam = patch.dict(os.environ, {'CADRE_ZINCIR_KALITE': 'opencode-go/qwen3.8-max'})
    ortam.start()
    unittest.addModuleCleanup(ortam.stop)



class AdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="qe-adapter-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = RunStore(self.home)
        (self.home/'zincir').mkdir()
        (self.home/'zincir/request-budget.json').write_text(json.dumps({'observed':time.time(), 'budget':None}))
        budget_patch = patch.object(core, 'butce_json', return_value=None)
        budget_patch.start()
        self.addCleanup(budget_patch.stop)

    def test_config_is_immutable_and_scoped_to_run_and_task(self) -> None:
        paths = []
        for project in ("a", "a", "b"):
            run = self.store.accept(project, "prompt")
            env = {**self.store.environment(run["run_id"]), "CADRE_GOREV_ID": "g1"}
            with patch.dict(os.environ, env), patch.object(core, "QE", self.home):
                first = Path(core.kisit_config_yaz("go", butce={})[0])
                original = first.read_bytes()
                second = Path(core.kisit_config_yaz("go", go_var=True)[0])
                self.assertNotEqual(first, second)
                self.assertEqual(first.read_bytes(), original)
                self.assertTrue(first.is_relative_to(self.store.task_dir(run["run_id"], "g1")))
                paths.extend((first, second))
        self.assertEqual(len(set(paths)), 6)

    def test_motor_status_reaches_durable_record(self) -> None:
        run = self.store.accept("a", "prompt")
        with patch.dict(os.environ, self.store.environment(run["run_id"])), patch.object(core, "QE", self.home):
            core.qe_durum("g1", "yarim", "test")
        self.assertEqual(self.store.read(run["run_id"])["tasks"]["g1"]["state"], "yarim")
        self.assertTrue((self.store.task_dir(run["run_id"], "g1") / "status.txt").is_file())


    def wait_file(self, filename: str) -> None:
        end = time.monotonic() + 5
        while time.monotonic() < end:
            if (self.home / filename).exists():
                return
            time.sleep(.01)
        self.fail(f"fixture did not reach {filename}")

    def test_router_concurrent_failures_are_not_lost(self) -> None:
        code = "from yonlendirici import ariza; ariza('test', allowed_fails=1000)"
        env = {**os.environ, "CADRE_HOME": str(self.home), "PYTHONPATH": str(LIB),
               "CADRE_YON_DURUM": str(self.home / "router.json"),
               "CADRE_YON_KAYIT": str(self.home / "router.jsonl")}
        procs = [subprocess.Popen([sys.executable, "-c", code], env=env) for _ in range(16)]
        self.assertEqual([p.wait(timeout=10) for p in procs], [0] * 16)
        self.assertEqual(json.loads((self.home / "router.json").read_text())["test"]["hata"], 16)

    def test_real_dispatcher_resumes_saved_plan_and_skips_finished_task(self) -> None:
        root = self.home / "fixture-engine"
        from qa_fixture import instructions
        instructions(root)
        (root / "bin").mkdir(parents=True)
        (root / "lib").mkdir()
        for filename in ("arsiv.py", "kosu.py", "runtime_sync.py", "aylik_butce.py", "dispatch_util.py", "standart-gorev-formu.txt"):
            shutil.copyfile(LIB / filename, root / "lib" / filename)
        for source in LIB.iterdir():
            if source.is_file() and source.suffix in ('.py', '.mjs', '.json') and not (root / 'lib' / source.name).exists():
                shutil.copyfile(source, root / 'lib' / source.name)
        shutil.copyfile(LIB.parent / "bin/cadre-dispatch.sh", root / "bin/cadre-dispatch.sh")
        (root / "lib/common.sh").write_text('CADRE_LOGS="$CADRE_HOME/logs"\nqe_verify_ai_worktree(){ return 0; }\nqe_make_worktree(){ exit 99; }\n')
        # A saved plan must bypass both classification and paid planning.
        from test_uctan_uca import FAKE_BUTCE
        from test_kosu_sessions import KATALOG
        (root / "lib/butce.py").write_text(FAKE_BUTCE)
        shutil.copyfile(LIB / 'yonlendirici.py', root / 'lib/real_yon.py')
        (root / 'lib/yonlendirici.py').write_text("from real_yon import *\nif __name__ == '__main__': print('')\n")
        (self.home / 'models.json').write_text(json.dumps(KATALOG))
        (self.home / 'zincir/request-budget.json').unlink()
        (root / "bin/is-motoru.sh").write_text('''
echo "$CADRE_GOREV_ID" >> "$CADRE_RUN_DIR/worker-calls"
if [ ! -f "$CADRE_RUN_DIR/retry-allowed" ]; then
  python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" task "$CADRE_RUN_ID" "$CADRE_GOREV_ID" yarim
  exit 1
fi
python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" task "$CADRE_RUN_ID" "$CADRE_GOREV_ID" uretti
''')
        wt = self.home / "worktree"
        wt.mkdir()
        subprocess.run(["git", "init", "-q", str(wt)], check=True, timeout=10)
        subprocess.run(['git', '-C', str(wt), '-c', 'user.name=fixture', '-c', 'user.email=fixture@local',
                        'commit', '--allow-empty', '-qm', 'fixture'], check=True, capture_output=True)
        run = self.store.accept("a", "original prompt")
        (self.home / 'projects/a').mkdir(parents=True)
        (self.home / 'projects/a/qa-komut').write_text('exit 0')
        self.store.update(run["run_id"], phase="dispatch", worktree=str(wt))
        self.store.task_update(run["run_id"], "g1", state="uretti", dispatch_result="uretti", session_id="session-g1")
        plan = {"is_id": "job", "gorevler": [
            {"id": gid, "model": 'auto', "paralel_grup": 0, "tanim": "edit", "dosyalar": ["product.txt"], "kabul": "product works", "zorluk": 2, "tahmin_dk": 1}
            for gid in ("g1", "g2")
        ]}
        (self.store.path(run["run_id"]) / "plan.json").write_text(json.dumps(plan))
        fake = self.home / 'review-fixture'
        fake.write_text(f'#!{sys.executable}\n' + '''
import json,os,pathlib,sys
assert 'PLAN ISTEK UYUMU INCELEMESI' in sys.argv[-1]
q=json.loads(sys.argv[-1].splitlines()[-1])
p=pathlib.Path(os.environ['CADRE_RUN_DIR'])/'review-calls'
with p.open('a') as out: out.write('review\\n')
answer={'revision':q['revision'],'verdict':'GECER','acceptance':[{'id':c['id'],'status':'PASS','evidence':'empty fixture source inspected'} for c in q['criteria']]}
print(json.dumps({'type':'text','sessionID':'ses_plan_review','part':{'type':'text','text':json.dumps(answer)}}))
''')
        fake.chmod(0o700)
        env = {**os.environ, "CADRE_HOME": str(self.home), "CADRE_ENGINE_ROOT": str(root), "CADRE_REAL_OPENCODE": str(fake),
               'CADRE_MODEL_KATALOG': str(self.home / 'models.json')}
        command = [sys.executable, str(LIB / "kosu.py"), "run", run["run_id"]]
        self.assertEqual(subprocess.run(command, env=env, timeout=60).returncode, 1)
        (self.store.path(run["run_id"]) / "retry-allowed").touch()
        self.assertEqual(subprocess.run(command, env=env, timeout=60).returncode, 1)  # No fabricated final acceptance.
        calls = (self.store.path(run["run_id"]) / "worker-calls").read_text().splitlines()
        self.assertEqual([task for task in calls if task.startswith("g")], ["g2", "g2"],
                         (self.store.path(run['run_id']) / 'dispatch.log').read_text())
        self.assertEqual((self.store.path(run['run_id']) / 'review-calls').read_text().splitlines(), ['review'])
        self.assertEqual(self.store.read(run["run_id"])["tasks"]["g1"]["session_id"], "session-g1")

    def test_motor_entry_refuses_a_second_owner_of_the_same_task(self) -> None:
        run = self.store.accept("a", "prompt")
        code = """
import os, pathlib, time
import qe_core
p = pathlib.Path(os.environ['CADRE_HOME'])
def fake_motor(*args):
    (p/'worker-entered').touch()
    while not (p/'worker-release').exists(): time.sleep(.01)
    return 0
qe_core._motor = fake_motor
raise SystemExit(qe_core.motor('a', os.environ['PROMPT']))
"""
        env = {**os.environ, **self.store.environment(run["run_id"]), "PYTHONPATH": str(LIB),
               "CADRE_GOREV_ID": "g1", "CADRE_OWNER_FDS": "", "PROMPT": run["prompt"]}
        command = [sys.executable, "-c", code]
        first = subprocess.Popen(command, env=env)
        try:
            self.wait_file("worker-entered")
            self.assertEqual(subprocess.run(command, env=env, timeout=5).returncode, 75)
            self.assertEqual(self.store.read(run["run_id"])["tasks"]["g1"]["owner_pid"], first.pid)
        finally:
            (self.home / "worker-release").touch()
            self.assertEqual(first.wait(timeout=5), 0)

    def test_real_motor_writes_run_scoped_status_and_cleans_pid(self) -> None:
        root = self.home / "motor-fixture"
        (root / "lib").mkdir(parents=True)
        (root / "bin").mkdir()
        (root / "lib/common.sh").write_text('qe_verify_ai_worktree(){ return 0; }\n')
        (root / "lib/kosu_defteri.py").write_text("print('fixture ledger')\n")
        (root / "bin/kisitli-kabuk.sh").write_text("exit 0\n")
        from test_uctan_uca import FAKE_BUTCE
        from test_kosu_sessions import KATALOG
        (root / 'lib/butce.py').write_text(FAKE_BUTCE)
        for ad in ('butce-plugin.mjs', 'cadre-plugin.mjs', 'aylik-butce-plugin.mjs'):
            shutil.copyfile(LIB / ad, root / 'lib' / ad)
        (self.home / 'models.json').write_text(json.dumps(KATALOG))
        (self.home / 'zincir/request-budget.json').unlink()

        fake = self.home / "fake-worker"
        fake.write_text(f"#!{sys.executable}\nimport sys,json,pathlib\npathlib.Path('received.json').write_text(json.dumps(sys.argv[1:]))\nprint(json.dumps({{'type':'text','sessionID':'ses_fixture','part':{{'type':'text','text':'done'}}}}))\n")
        fake.chmod(0o700)
        wt = self.home / "wt"
        wt.mkdir()
        from test_kosu_sessions import motor_source
        motor_source(self.home, wt)
        run = self.store.accept("a", "original prompt")
        self.store.update(run["run_id"], worktree=str(wt))
        code = """
from unittest.mock import Mock, patch
import os, qe_core
original = qe_core.subprocess.Popen
def popen(cmd, *args, **kwargs):
    if cmd[0] == 'caffeinate': return Mock()
    return original(cmd, *args, **kwargs)
with patch.object(qe_core, 'bekci_dirilt'), patch.object(qe_core, 'commit_wip', return_value=''), patch.object(qe_core.subprocess, 'Popen', side_effect=popen):
    raise SystemExit(qe_core.motor('a', os.environ['PROMPT']))
"""
        env = {**os.environ, **self.store.environment(run["run_id"]), "PYTHONPATH": str(LIB),
               "CADRE_ENGINE_ROOT": str(root), "CADRE_GOREV_ID": "g1", "CADRE_OWNER_FDS": "",
               "CADRE_REAL_OPENCODE": str(fake), "CADRE_OC_LOG": str(self.home / "empty.log"), "CADRE_PROGRESS_POLL": "1", "CADRE_MODEL_KATALOG": str(self.home / "models.json"), "PROMPT": run["prompt"]}
        result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        received = json.loads((wt / "received.json").read_text())
        self.assertEqual(received[:5], ["run", "--dir", str(wt), "--format", "json"])
        self.assertIn("--title", received)
        self.assertEqual(received[-1], "original prompt")
        task = self.store.task_dir(run["run_id"], "g1")
        self.assertTrue((task / "motor.log").is_file())
        self.assertTrue((task / "status.txt").is_file())
        self.assertFalse((task / "worker.pid").exists())
        self.assertEqual(self.store.read(run["run_id"])["tasks"]["g1"]["state"], "uretti")

    def test_cadre_accepts_prompt_before_starting_coordinator(self) -> None:
        root = self.home / "fixture-code"
        from qa_fixture import instructions
        instructions(root)
        (root / "bin").mkdir(parents=True)
        (root / "lib").mkdir()
        for filename in ("cadre.sh", "orkestra.sh"):
            shutil.copyfile(LIB.parent / "bin" / filename, root / "bin" / filename)
        for filename in ("arsiv.py", "kosu.py", "runtime_sync.py", "aylik_butce.py", "sonuc.py", "common.sh", "qa_turet.py", "model_secimi.py", "katalog.py", "seviye.py", "onelek.py"):   # P6: acceptance derives QA
            shutil.copyfile(LIB / filename, root / "lib" / filename)
        for source in LIB.glob('*.py'):
            if not (root / 'lib' / source.name).exists():
                shutil.copyfile(source, root / 'lib' / source.name)
        shutil.copytree(LIB / 'qa-sim', root / 'lib/qa-sim', ignore=shutil.ignore_patterns('__pycache__'), dirs_exist_ok=True)
        shutil.copyfile(LIB / 'sinirlar.json', root / 'lib/sinirlar.json')
        (root / "bin/cadre-dispatch.sh").write_text('exit 3\n')
        (root / "bin/is-motoru.sh").write_text('''
test -s "$CADRE_RUN_DIR/run.json" || exit 91
test -s "$CADRE_RUN_DIR/prompt.md" || exit 92
touch "$CADRE_HOME/launcher-entered"
exit 1
''')
        repo = self.home / "repo"
        self.init_repo(repo)
        project = self.home / "projects/a"
        project.mkdir(parents=True)
        (project / "repo-path").write_text(str(repo.resolve()))
        (project / "POLICY.md").write_text("test policy")
        env = {**os.environ, "CADRE_HOME": str(self.home), "CADRE_ENGINE_ROOT": str(root), "CADRE_IZLE": "0"}
        env.pop("CADRE_RUN_ID", None)
        prompt = "preserve spaces  \nkeep repeated\nkeep repeated\n"
        result = subprocess.run(["bash", str(root / "bin/cadre.sh"), "gorev"], input=prompt + "go\nevet\nevet\n", cwd=repo,
                                env=env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.wait_file("launcher-entered")
        records = self.store.list("a")
        self.assertEqual(len(records), 1)
        self.assertEqual(Path(records[0]["prompt"]).read_text(), prompt)
        self.assertEqual(len(records[0]["source_sha"]), 40)
        self.assertEqual(Path(records[0]["constraints"]["POLICY.md"]).read_text(), "test policy")
        # N-RUN01: detached coordinator owns its files until the owner lock is released.
        from kosu import FileLock
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            lock = FileLock(self.store.path(records[0]['run_id']) / '.owner.lock')
            if lock.acquire(blocking=False):
                lock.close()
                break
            time.sleep(.01)
        else:
            self.fail('detached coordinator did not finish')
        self.assertIsNone(self.store.read(records[0]['run_id'])['owner_pid'])

    def init_repo(self, path: Path) -> None:
        subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, timeout=5)
        subprocess.run(["git", "-C", str(path), "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
                        "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-q", "--allow-empty", "-m", "fixture"], check=True, timeout=5)

    def test_worktree_is_unique_per_run_and_reused_after_restart(self) -> None:
        repo = (self.home / "repo").resolve()
        self.init_repo(repo)
        project = self.home / "projects/a"
        project.mkdir(parents=True)
        (project / "repo-path").write_text(str(repo))
        (project / "base-branch").write_text("main")
        hooks = self.home / "hooks"
        hooks.mkdir()
        for name in ("pre-push", "pre-commit", "commit-msg"):
            script = hooks / name
            script.write_text("#!/bin/sh\nexit 0\n")
            script.chmod(0o700)
        paths = []
        for _ in range(2):
            run = self.store.accept("a", "prompt")
            env = {**os.environ, **self.store.environment(run["run_id"]), "CADRE_ENGINE_ROOT": str(LIB.parent)}
            command = ["bash", "-c", '. "$1"; qe_make_worktree a test', "fixture", str(LIB / "common.sh")]
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            again = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(result.stdout, again.stdout)
            self.assertEqual(self.store.read(run["run_id"])["worktree"], result.stdout.strip())
            paths.append(result.stdout.strip())
        self.assertNotEqual(paths[0], paths[1])

    def test_monitor_waits_for_named_run_instead_of_using_old_worktree(self) -> None:
        old = self.home / "worktrees/a/old-product"
        old.mkdir(parents=True)
        run = self.store.accept("a", "new task still planning")
        env = {**os.environ, "CADRE_HOME": str(self.home), "OPENCODE_DB": str(self.home / "absent.db")}
        result = subprocess.run([sys.executable, str(LIB / "izle.py"), "a", "--run", run["run_id"], "--tek", "--duz", "--ayrinti"],
                                env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(run["run_id"], result.stdout)
        self.assertIn("worktree hazırlanıyor", result.stdout)
        self.assertNotIn("old-product", result.stdout)
        self.store.update(run["run_id"], state="retryable", exit_code=1)
        (self.store.path(run["run_id"]) / "coordinator.log").write_text("2026-09-13 00:00:00 kosu kendi bitti\n")
        result = subprocess.run([sys.executable, str(LIB / "izle.py"), "a", "--run", run["run_id"], "--tek", "--duz", "--ayrinti"],
                                env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DURDU", result.stdout)
        self.assertIn("rc=1", result.stdout)
        self.assertNotIn("✔ BİTTİ", result.stdout)

    def test_sigkill_restart_retains_step_and_never_replans(self) -> None:
        root = self.home / "crash-fixture"
        (root / "bin").mkdir(parents=True)
        (root / "bin/cadre-dispatch.sh").write_text('echo dispatch >> "$CADRE_RUN_DIR/calls"\nexit 3\n')
        (root / "bin/is-motoru.sh").write_text(f'exec "{sys.executable}" "$CADRE_ENGINE_ROOT/worker.py"\n')
        (root / "worker.py").write_text("""
import os, pathlib, time
from kosu import RunStore
store = RunStore()
rid = os.environ['CADRE_RUN_ID']
p = store.path(rid)
if (p/'step.done').exists():
    with (p/'calls').open('a') as f: f.write('remaining\\n')
    store.task_update(rid, 'g2', state='tamam')
else:
    with (p/'calls').open('a') as f: f.write('partial\\n')
    store.task_update(rid, 'g1', state='tamam', session_id='saved-session')
    (p/'step.done').touch()
    (p/'worker.pid').write_text(str(os.getpid()))
    (pathlib.Path(os.environ['CADRE_HOME'])/'crash-ready').touch()
    while True: time.sleep(.01)
""")
        run = self.store.accept("a", "original task")
        env = {**os.environ, "CADRE_HOME": str(self.home), "CADRE_ENGINE_ROOT": str(root), "PYTHONPATH": str(LIB)}
        command = [sys.executable, str(LIB / "kosu.py"), "run", run["run_id"]]
        first = subprocess.Popen(command, env=env)
        child_pid = None
        try:
            self.wait_file("crash-ready")
            child_pid = int((self.store.path(run["run_id"]) / "worker.pid").read_text())
            self.assertEqual(subprocess.run(command, env=env, timeout=5).returncode, 75)
            first.kill()
            first.wait(timeout=5)
            # A killed coordinator cannot cause two workers to own this run.
            deadline = time.monotonic() + 5
            while True:
                rc = subprocess.run(command, env=env, timeout=5).returncode
                if rc != 75 or time.monotonic() > deadline:
                    break
                time.sleep(.01)
            self.assertEqual(rc, 1)  # Fixture models ownership/steps, not product acceptance.
            self.assertFalse(core.pid_yasiyor(child_pid))
            child_pid = None
            self.assertEqual((self.store.path(run["run_id"]) / "calls").read_text().splitlines(), ["dispatch", "partial", "remaining"])
            self.assertEqual(self.store.read(run["run_id"])["tasks"]["g1"]["session_id"], "saved-session")
        finally:
            if first.poll() is None:
                first.kill()
                first.wait(timeout=5)
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_detached_executors_are_reclaimed_and_resume_their_receipts_after_owner_death(self):
        import kosu
        root = self.home / 'detached-engine'
        (root / 'bin').mkdir(parents=True)
        (root / 'bin/cadre-dispatch.sh').write_text('exit 3\n')
        (root / 'bin/is-motoru.sh').write_text(f'exec "{sys.executable}" "{root / "worker.py"}"\n')
        executor = self.home / 'executor.py'
        executor.write_text("""
import json,os,pathlib,sys,time
sid=sys.argv[1]
print(json.dumps({'type':'text','sessionID':sid,'part':{'type':'text','text':'partial'}}),flush=True)
home=pathlib.Path(os.environ['CADRE_HOME'])
(home/'partial-output').write_text('retained')
(home/'executor-ready').write_text(str(os.getpid()))
while True: time.sleep(.01)
""")
        (root / 'worker.py').write_text(f"""
import os,pathlib,sys,time
from kosu import RunStore,owner_fds
from oturum import Attempt
store=RunStore(); rid=os.environ['CADRE_RUN_ID']; home=store.home
attempt=Attempt('opencode','impl','fixture/worker','original work')
if attempt.resuming:
    (home/'resumed-session').write_text(attempt.data['session_id'])
    attempt.lease.close()
    sys.exit(1)
proc=attempt.launch([sys.executable,{str(executor)!r},'ses_owned'],str(home),home/'executor.log',dict(os.environ),owner_fds())
while proc.poll() is None: time.sleep(.01)
""")
        run = self.store.accept('a', 'work')
        rid = run['run_id']
        env = {**os.environ, 'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(root),
               'PYTHONPATH': str(LIB), 'CADRE_KILL_GRACE': '.2', 'OPENCODE_DB': str(self.home / 'absent.db')}
        command = [sys.executable, str(LIB / 'kosu.py'), 'run', rid]
        first = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pids = []
        try:
            self.wait_file('executor-ready')
            executor_pid = int((self.home / 'executor-ready').read_text())
            phase_pid = self.store.read(rid)['phase_process']['pid']
            pids = [phase_pid, executor_pid]
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                attempts = list(self.store.task_dir(rid, 'main').glob('attempts/*/attempt.json'))
                if attempts and (attempts[0].parent / 'process.json').exists():
                    break
                time.sleep(.01)
            self.assertTrue(json.loads((attempts[0].parent / 'process.json').read_text())['process_identity'])
            first.kill()
            first.wait(timeout=5)
            os.kill(phase_pid, signal.SIGKILL)
            self.assertTrue(core.pid_yasiyor(executor_pid))
            self.assertEqual(subprocess.run(command, env=env, capture_output=True, timeout=10).returncode, 1)
            self.assertEqual((self.home / 'resumed-session').read_text(), 'ses_owned')
            self.assertEqual((self.home / 'partial-output').read_text(), 'retained')
            self.assertFalse(core.pid_yasiyor(executor_pid))
            self.assertEqual(kosu.active_runs(self.store), [])
            self.assertEqual(self.store.read(rid)['tasks']['main']['attempt_count'], 2)
        finally:
            if first.poll() is None:
                first.kill()
            first.wait(timeout=5)
            for pid in pids:
                if core.pid_yasiyor(pid):
                    core.kill_tree(pid, grace=.1)

    def test_reclaim_checks_process_birth_and_terminal_runs_never_restart(self):
        import kosu
        rid = self.store.accept('a', 'work')['run_id']
        sleeper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
        try:
            identity = kosu.process_identity(sleeper.pid)
            self.assertIsNotNone(identity)
            self.store.update(rid, state='cancelled', owner_pid=None,
                              phase_process={**identity, 'started': 'wrong incarnation'})
            with patch.object(kosu, '_baslat', side_effect=AssertionError('terminal resurrection')):
                self.assertEqual(kosu.revive(self.store, self.home), [])
            self.assertIsNone(sleeper.poll())
            self.store.update(rid, phase_process=identity)
            with patch.dict(os.environ, {'CADRE_KILL_GRACE': '.1'}), \
                 patch.object(kosu, '_baslat', side_effect=AssertionError('terminal resurrection')):
                self.assertEqual(kosu.revive(self.store, self.home), [])
            self.assertIsNotNone(sleeper.poll())
            self.assertEqual(self.store.read(rid)['state'], 'cancelled')
            self.assertEqual(kosu.active_runs(self.store), [])
        finally:
            if sleeper.poll() is None:
                sleeper.kill()
            sleeper.wait(timeout=5)

    def test_executor_registration_precedes_tool_io_and_disk_failure_keeps_it_closed(self):
        from oturum import Attempt
        rid = self.store.accept('a', 'work')['run_id']
        env = {**os.environ, **self.store.environment(rid), 'CADRE_GOREV_ID': 'g1'}
        marker = self.home / 'tool-called'
        code = f"from pathlib import Path; Path({str(marker)!r}).write_text('called'); print('{{\"sessionID\":\"ses_recorded\"}}')"
        with patch.dict(os.environ, env):
            attempt = Attempt('opencode', 'impl', 'fixture/worker', 'work')
            (attempt.directory / 'process.json').mkdir()
            process = attempt.launch([sys.executable, '-c', code], str(self.home), self.home / 'log', env, ())
            with process.proc:
                self.assertEqual(process.wait(timeout=5), 75)
            self.assertFalse(marker.exists())
            (attempt.directory / 'process.json').rmdir()
            next_attempt = Attempt('opencode', 'impl', 'fixture/worker', 'work')
            process = next_attempt.launch([sys.executable, '-c', code], str(self.home), self.home / 'log', env, ())
            self.assertEqual(process.wait(timeout=5), 0)
            receipt = json.loads((next_attempt.directory / 'process.json').read_text())
            self.assertEqual(receipt['process_identity']['pid'], process.pid)
            self.assertEqual(receipt['attempt_id'], next_attempt.data['attempt_id'])
            self.assertEqual(marker.read_text(), 'called')

    def project_tool_fixture(self):
        repo = self.home / 'source'
        repo.mkdir()
        def git(*args):
            return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()
        git('init', '-q')
        (repo / 'app.py').write_text('value = 1\n')
        git('add', 'app.py')
        git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
        wt = self.home / "ürün ' $literal"
        git('worktree', 'add', '-qb', 'cadre/tool-fixture', str(wt))
        parent = self.store.accept('a', 'project')['run_id']
        self.store.update(parent, proje={'kimlik': 'fixture'})
        rid = self.store.accept('a', 'child')['run_id']
        self.store.update(rid, proje_ust=parent, worktree=str(wt))
        env = {**os.environ, **self.store.environment(rid), 'CADRE_GOREV_ID': 'g1', 'CADRE_ENGINE_ROOT': str(LIB.parent),
               'SYNTHETIC_PROVIDER_TOKEN': 'fixture-only'}
        env['HOME'] = str(self.home / 'tool-home')
        Path(env['HOME']).mkdir()
        return repo, wt, parent, rid, env

    def project_tool_execute(self, wt, env, tools, role='impl', model='fixture/worker'):
        from oturum import Attempt
        executor = self.home / ('tool-executor-' + role + '.mjs')
        executor.write_text(f"""import plugin from {json.dumps((LIB / 'cadre-plugin.mjs').as_uri())};
import {{ spawnSync }} from 'node:child_process';
if (process.env.OPENCODE_DISABLE_PROJECT_CONFIG !== 'true' || process.env.OPENCODE_DISABLE_AUTOUPDATE !== 'true') throw new Error('untrusted project config enabled');
const hooks = await plugin();
process.env.CADRE_PROJECT_TOOLS = '0';
process.env.CADRE_TOOL_POLICY = '{{}}';
const results = [];
for (const tool of {json.dumps(tools)}) {{
  const output = {{args: {{command: tool.command, filePath: tool.filePath}}}};
  try {{
    await hooks['tool.execute.before']({{tool: tool.tool}}, output);
    const r = tool.tool === 'bash' ? spawnSync('/bin/sh', ['-c', output.args.command], {{encoding:'utf8'}}) : {{status:0}};
    results.push({{status:r.status, stdout:r.stdout, stderr:r.stderr}});
  }} catch (e) {{ results.push({{denied:String(e)}}); }}
}}
console.log(JSON.stringify({{sessionID:'ses_tools_{role}', results}}));
""")
        with patch.dict(os.environ, env):
            attempt = Attempt('opencode', role, model, 'work')
            process = attempt.launch(['node', str(executor)], str(wt), self.home / 'tool.log', env, ())
            with process.proc:
                self.assertEqual(process.wait(timeout=20), 0, (self.home / 'tool.log').read_text())
            return json.loads(process.output().splitlines()[-1])['results'], attempt

    def test_project_tool_wrapper_stays_short_and_a_copied_wrapper_still_runs_confined(self):
        from oturum import Attempt
        repo, wt, parent, rid, env = self.project_tool_fixture()
        parent_path = self.store.path(parent) / 'run.json'
        before = parent_path.read_bytes()
        kacis = shlex.join([sys.executable, '-c', f"from pathlib import Path; Path({str(parent_path)!r}).write_text('x')"])
        executor = self.home / 'tool-executor-kisa.mjs'
        executor.write_text("""import plugin from PLUGIN;
import { spawnSync } from 'node:child_process';
const hooks = await plugin();
process.env.CADRE_PROJECT_TOOLS = '0';
process.env.CADRE_TOOL_POLICY = '{}';
const q = (v) => "'" + v.replaceAll("'", "'\\\\''") + "'";
const run = async (command) => {
  const output = { args: { command } };
  await hooks['tool.execute.before']({ tool: 'bash' }, output);
  const r = spawnSync('/bin/sh', ['-c', output.args.command], { encoding: 'utf8' });
  return { wrapped: output.args.command, status: r.status, stdout: r.stdout, stderr: r.stderr };
};
const first = await run('printf ilk');
const prefix = first.wrapped.slice(0, first.wrapped.lastIndexOf(" '"));
const copied = await run(prefix + ' ' + q('printf kopya'));
const escaped = await run(prefix + ' ' + q(KACIS));
console.log(JSON.stringify({ sessionID: 'ses_tools_kisa', results: [first, copied, escaped] }));
""".replace('PLUGIN', json.dumps((LIB / 'cadre-plugin.mjs').as_uri())).replace('KACIS', json.dumps(kacis)))
        with patch.dict(os.environ, env):
            attempt = Attempt('opencode', 'impl', 'fixture/worker', 'work')
            process = attempt.launch(['node', str(executor)], str(wt), self.home / 'tool.log', env, ())
            with process.proc:
                self.assertEqual(process.wait(timeout=30), 0, (self.home / 'tool.log').read_text())
            first, copied, escaped = json.loads(process.output().splitlines()[-1])['results']
        self.assertEqual((first['status'], first['stdout']), (0, 'ilk'))
        self.assertLess(len(first['wrapped']), 400, 'gecmise yazilan komut kisa kalir')
        self.assertNotIn('(version 1)', first['wrapped'], 'sandbox profili komuta gomulmez')
        self.assertEqual((copied['status'], copied['stdout']), (0, 'kopya'), 'modelin kopyaladigi sarmalayici ic ice calisir')
        self.assertNotEqual(escaped['status'], 0, escaped)
        self.assertEqual(parent_path.read_bytes(), before, 'ic ice cagri da sandbox disina yazamaz')

    def test_project_tools_confine_real_shell_descendants_and_preserve_git_and_approval_bytes(self):
        from kosu import atomic_json, plan_digest
        repo, wt, parent, rid, env = self.project_tool_fixture()
        source_git = repo / '.git'
        before_git = {str(p): p.read_bytes() for p in source_git.rglob('*') if p.is_file()}
        parent_path = self.store.path(parent) / 'run.json'
        identity = {'command': ['git', '-C', str(repo), 'update-ref', 'refs/heads/cadre/escape', 'HEAD'],
                    'source_sha': self.store.read(parent)['source_sha'], 'plan_revision': 0}
        token = plan_digest(identity)
        record = self.store.read(parent)
        record['project_git_action'] = {'identity': identity, 'token': token, 'status': 'pending'}
        atomic_json(parent_path, record)
        before_parent = parent_path.read_bytes()
        before_pointer = (wt / '.git').read_bytes()
        (wt / 'record-alias').symlink_to(parent_path)
        py = lambda code: shlex.join([sys.executable, '-c', code])
        tools = [
            {'tool': 'bash', 'command': py("from pathlib import Path; import os,subprocess,sys; assert 'SYNTHETIC_PROVIDER_TOKEN' not in os.environ; Path('app.py').write_text('value = 2\\n'); assert Path('app.py').read_text() == 'value = 2\\n'; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)']); p.terminate(); p.wait(timeout=3)")},
            {'tool': 'bash', 'command': 'git status --porcelain'},
            {'tool': 'bash', 'command': py(f"from pathlib import Path; Path({str(parent_path)!r}).write_text('{{}}')")},
            {'tool': 'bash', 'command': py("from pathlib import Path; Path('record-alias').write_text('{}')")},
            {'tool': 'bash', 'command': py(f"import os; os.unlink({str(parent_path)!r})")},
            {'tool': 'bash', 'command': 'git add app.py'},
            {'tool': 'bash', 'command': 'git update-ref refs/heads/cadre/escape HEAD'},
            {'tool': 'bash', 'command': py("from pathlib import Path; Path('.git').write_text('gitdir: /tmp/escape')")},
            {'tool': 'bash', 'command': py(f"import os,subprocess,sys; e=dict(os.environ); e.pop('CADRE_RUN_ID',None); sys.exit(subprocess.call([sys.executable,{str(LIB/'kosu.py')!r},'project-git-approve',{parent!r},{token!r}],env=e))")},
            {'tool': 'bash', 'command': py(f"import subprocess,sys; sys.exit(subprocess.call([sys.executable,'-c',{('from pathlib import Path; Path('+repr(str(parent_path))+').write_text(\"{}\")')!r}],env={{}}))")},
            {'tool': 'bash', 'command': py("import socket; socket.create_connection(('127.0.0.1',9),timeout=1)")},
            {'tool': 'edit', 'filePath': str(parent_path)},
            {'tool': 'write', 'filePath': str(wt / 'app.py')},
            {'tool': 'apply_patch'}, {'tool': 'task'}, {'tool': 'mcp_external'},
        ]
        results, attempt = self.project_tool_execute(wt, env, tools)
        self.assertEqual([r['status'] for r in results[:2]], [0, 0])
        for index, result in enumerate(results[2:11], 2):
            with self.subTest(tool=tools[index]):
                self.assertNotEqual(result['status'], 0, result)
        for result in results[11:]:
            self.assertIn('CADRE_TOOL_DENIED', result['denied'])
        self.assertEqual(parent_path.read_bytes(), before_parent)
        self.assertEqual((wt / '.git').read_bytes(), before_pointer)
        self.assertEqual({str(p): p.read_bytes() for p in source_git.rglob('*') if p.is_file()}, before_git)
        self.assertEqual((wt / 'app.py').read_text(), 'value = 2\n')
        self.assertTrue(json.loads((attempt.directory / 'tool-policy.json').read_text())['writable'])
        self.assertEqual(self.store.read(rid)['tasks']['g1']['attempt_count'], 1)

    def signal_boundary_fixture(self):
        witness = self.home / 'foreign-signal-received'
        code = ("import signal,time\nfrom pathlib import Path\n"
                f"signal.signal(signal.SIGUSR1,lambda *args:Path({str(witness)!r}).touch())\n"
                "print('ready',flush=True)\ntime.sleep(60)\n")
        target = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, text=True, start_new_session=True)
        def stop():
            if target.poll() is None:
                target.kill()
            target.wait(timeout=5)
            target.stdout.close()
        self.addCleanup(stop)
        self.assertEqual(target.stdout.readline().strip(), 'ready')
        body = ("import ctypes,os,signal,subprocess,sys\n"
                "library=ctypes.CDLL('/usr/lib/libproc.dylib',use_errno=True)\n"
                "query=library.proc_pidinfo\n"
                "query.argtypes=(ctypes.c_int,ctypes.c_int,ctypes.c_uint64,ctypes.c_void_p,ctypes.c_int)\n"
                "query.restype=ctypes.c_int\n"
                "buffer=ctypes.create_string_buffer(1024)\n"
                f"foreign={target.pid}\n"
                "try:\n os.kill(foreign,signal.SIGUSR1)\n"
                "except PermissionError: pass\n"
                "else: raise AssertionError('foreign signal allowed')\n"
                "ctypes.set_errno(0)\n"
                "assert query(foreign,3,0,buffer,1024)==0,'foreign process inspection allowed'\n"
                "assert ctypes.get_errno()==1\n"
                "received=[]\n"
                "signal.signal(signal.SIGUSR1,lambda *args:received.append(True))\n"
                "os.kill(os.getpid(),signal.SIGUSR1)\n"
                "assert received==[True]\n"
                "assert query(os.getpid(),3,0,buffer,1024)>0\n"
                "child_code=\"import signal,time;signal.signal(signal.SIGUSR1,lambda *args:None);print('ready',flush=True);time.sleep(60)\"\n"
                "child=subprocess.Popen([sys.executable,'-c',child_code],stdout=subprocess.PIPE,text=True,close_fds=True,start_new_session=True)\n"
                "try:\n"
                " assert child.stdout.readline().strip()=='ready'\n"
                " assert query(child.pid,3,0,buffer,1024)>0\n"
                " child.send_signal(signal.SIGUSR1)\n"
                " child.terminate()\n"
                " assert child.wait(timeout=5)==-signal.SIGTERM\n"
                "finally:\n"
                " if child.poll() is None: child.kill()\n"
                " child.wait(timeout=5)\n child.stdout.close()\n"
                "nested=subprocess.run([sys.executable,'-c',f'import os,signal;os.kill({foreign},signal.SIGUSR1)'],capture_output=True,text=True)\n"
                "assert nested.returncode!=0 and 'PermissionError' in nested.stderr\n"
                "print('PROCESS_BOUNDARY_PASS')\n")
        return target, witness, body

    def test_regular_tool_roles_cannot_signal_or_inspect_foreign_processes_and_keep_child_control(self):
        repo, wt, parent, rid, env = self.project_tool_fixture()
        self.store.update(rid, proje_ust=None)
        target, witness, body = self.signal_boundary_fixture()
        for role in ('impl', 'plan', 'review'):
            with self.subTest(role=role):
                results, attempt = self.project_tool_execute(wt, env, [
                    {'tool': 'bash', 'command': shlex.join([sys.executable, '-c', body])}], role=role)
                self.assertEqual(results[0]['status'], 0, results)
                self.assertIn('PROCESS_BOUNDARY_PASS', results[0]['stdout'])
                policy = json.loads((attempt.directory / 'tool-policy.json').read_text())
                self.assertFalse(Path(policy['scratch']).exists())
                self.assertTrue((attempt.directory / 'tool-retired.json').is_file())
        self.assertIsNone(target.poll())
        self.assertFalse(witness.exists())
        self.assertEqual((repo / 'app.py').read_text(), 'value = 1\n')

    def test_regular_qa_process_boundary_keeps_discovery_repeat_concurrency_and_foreign_owner(self):
        from concurrent.futures import ThreadPoolExecutor
        repo, wt, parent, rid, env = self.project_tool_fixture()
        self.store.update(rid, proje_ust=None)
        target, witness, body = self.signal_boundary_fixture()
        (wt / 'process_boundary.py').write_text(body)
        (wt / 'test_process_boundary.py').write_text(
            "import runpy,unittest\nclass ProcessBoundary(unittest.TestCase):\n"
            " def test_real_process_boundary(self): runpy.run_path('process_boundary.py')\n")
        pd = self.home / 'projects/a'
        pd.mkdir(parents=True, exist_ok=True)
        command = shlex.join([sys.executable, '-m', 'unittest', 'discover', '-v'])
        (pd / 'qa-komut').write_text(command)
        with patch.dict(os.environ, env), patch.object(core, 'QE', self.home):
            for repeat in range(2):
                result = core.qa_kapisi('a', str(wt), self.home / f'process-qa-{repeat}.log', 'unused', model='fixture/worker')
                self.assertEqual(result['status'], 'PASS', result)
                self.assertEqual(result['discovery']['executed'], 1)
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(core._taban_komut, wt, command, self.home / f'process-parallel-{i}.log', 10) for i in range(2)]
                self.assertEqual([future.result(timeout=15) for future in futures], [0, 0])
        self.assertIsNone(target.poll())
        self.assertFalse(witness.exists())
        self.assertEqual(list((self.home / 'tool-tmp').iterdir()), [])
        self.assertEqual((repo / 'app.py').read_text(), 'value = 1\n')

    def test_dependency_stores_are_readable_but_cannot_be_installed_changed_or_deleted_from_tools_or_qa(self):
        repo, wt, parent, rid, env = self.project_tool_fixture()
        self.store.update(rid, proje_ust=None)
        prefix = wt / 'prefix'
        depot = prefix / 'node_modules/fixture'
        depot.mkdir(parents=True)
        dependency = depot / 'index.js'
        dependency.write_text('module.exports = 1;\n')
        (wt / 'dependency-alias').symlink_to(depot, target_is_directory=True)
        body = ("import subprocess,sys\nfrom pathlib import Path\n"
                "p=Path('prefix/node_modules/fixture/index.js')\n"
                "assert p.read_text()=='module.exports = 1;\\n'\n"
                "result=subprocess.run(['node','-e',\"if(require('./prefix/node_modules/fixture')!==1)process.exit(1)\"])\n"
                "assert result.returncode==0\n"
                "for name in ('alternate/node_modules','alternate/NODE_MODULES','alternate/site-packages','alternate/pkg-1.dist-info','alternate/pkg.egg-info'):\n"
                " Path('alternate').mkdir(exist_ok=True)\n"
                " try:Path(name).mkdir()\n"
                " except PermissionError:pass\n"
                " else:raise AssertionError('unapproved dependency store created: '+name)\n"
                "for operation in (lambda:p.write_text('modified'),lambda:p.unlink(),lambda:p.rename(p.with_name('changed.js')),lambda:Path('dependency-alias/new.js').touch()):\n"
                " try:operation()\n"
                " except PermissionError:pass\n"
                " else:raise AssertionError('existing dependency modified')\n"
                "Path('allowed-result.txt').write_text('ordinary product output')\n"
                "print('DEPENDENCY_BOUNDARY_PASS')\n")
        results, attempt = self.project_tool_execute(wt, env, [{'tool':'bash','command':shlex.join([sys.executable,'-c',body])}])
        self.assertEqual(results[0]['status'], 0, results)
        self.assertIn('DEPENDENCY_BOUNDARY_PASS', results[0]['stdout'])
        (wt / 'dependency_boundary.py').write_text(body)
        (wt / 'test_dependency_boundary.py').write_text(
            "import runpy,unittest\nclass DependencyBoundary(unittest.TestCase):\n"
            " def test_dependency_boundary(self):runpy.run_path('dependency_boundary.py')\n")
        pd = self.home / 'projects/a'
        pd.mkdir(parents=True, exist_ok=True)
        (pd / 'qa-komut').write_text(shlex.join([sys.executable,'-m','unittest','discover','-v']))
        with patch.dict(os.environ, env), patch.object(core, 'QE', self.home):
            result = core.qa_kapisi('a', str(wt), self.home / 'dependency-qa.log', 'unused', model='fixture/worker')
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['discovery']['executed'], 1)
        self.assertEqual(dependency.read_text(), 'module.exports = 1;\n')
        self.assertEqual((wt / 'allowed-result.txt').read_text(), 'ordinary product output')
        self.assertEqual(list((self.home / 'tool-tmp').iterdir()), [])

    def test_installer_entrypoints_and_module_aliases_are_denied_before_their_fixture_code_runs(self):
        repo, wt, parent, rid, env = self.project_tool_fixture()
        self.store.update(rid, proje_ust=None)
        marker = wt / 'installer-executed'
        body = f"from pathlib import Path;Path({str(marker)!r}).touch()\n"
        (wt / 'pip').mkdir()
        (wt / 'pip/__init__.py').write_text(body)
        (wt / 'bin').mkdir()
        entry = wt / 'bin/pip3'
        entry.write_text('#!' + sys.executable + '\n' + body)
        entry.chmod(0o755)
        alias = wt / 'copied-entry.py'
        alias.write_text('import pip\n')
        uv = wt / 'bin/uv'
        uv.write_text('#!' + sys.executable + '\n' + body)
        uv.chmod(0o755)
        commands = ([str(entry)], [sys.executable,str(entry)], [sys.executable,str(alias)],
                    [sys.executable,'-c','import pip'], [str(uv)])
        results, attempt = self.project_tool_execute(wt, env, [
            {'tool':'bash','command':shlex.join(command)} for command in commands])
        for result in results:
            self.assertNotEqual(result['status'], 0, result)
            self.assertRegex(result['stderr'], 'PermissionError|Operation not permitted')
        self.assertFalse(marker.exists())
        self.assertFalse(Path(json.loads((attempt.directory / 'tool-policy.json').read_text())['scratch']).exists())

    def test_readonly_source_hardlinks_cannot_hide_secret_names_from_the_tool_boundary(self):
        repo, wt, parent, rid, env = self.project_tool_fixture()
        secret = wt / '.env.fixture'
        secret.write_text('SYNTHETIC_REPO_SECRET')
        os.link(secret, wt / 'ordinary-name.txt')
        for role in ('plan','review'):
            with self.subTest(role=role):
                with self.assertRaisesRegex(PermissionError, 'project-tool-hardlink-untrusted'):
                    self.project_tool_execute(wt, env, [{'tool':'bash','command':'cat ordinary-name.txt'}], role=role)
        self.assertEqual((wt / 'ordinary-name.txt').stat().st_ino, secret.stat().st_ino)
        self.assertEqual((repo / 'app.py').read_text(), 'value = 1\n')
        self.assertFalse((self.home / 'tool-tmp').exists())
        self.assertFalse(list(self.store.path(rid).glob('tasks/g1/attempts/*/process.json')))

    def test_dependency_and_cache_hardlinks_do_not_block_the_tool_boundary(self):
        import oturum
        repo, wt, parent, rid, env = self.project_tool_fixture()
        for folder in ('node_modules/pkg', '.venv/lib', '__pycache__'):
            path = wt / folder
            path.mkdir(parents=True)
            (path / 'linked.bin').write_text('dependency payload')
            os.link(path / 'linked.bin', path / 'alias.bin')
        self.assertFalse(oturum.urun_hardlink_var(wt))
        self.project_tool_execute(wt, env, [{'tool': 'bash', 'command': 'cat app.py'}], role='plan')
        (wt / 'src').mkdir()
        (wt / 'src/data.txt').write_text('product')
        os.link(wt / 'src/data.txt', wt / 'src/alias.txt')
        self.assertTrue(oturum.urun_hardlink_var(wt))
        with self.assertRaisesRegex(PermissionError, 'project-tool-hardlink-untrusted'):
            self.project_tool_execute(wt, env, [{'tool': 'bash', 'command': 'cat src/alias.txt'}], role='plan')

    def test_secret_paths_use_the_same_source_and_kernel_boundary_without_reading_their_values(self):
        import kosu
        import sonuc
        repo, wt, parent, rid, env = self.project_tool_fixture()
        self.store.update(rid, proje_ust=None)
        names = ('.kube/config','.npmrc','.netrc','.pypirc','auth.json','config.local.yaml',
                 'Secrets/account.json','CREDENTIALS.txt','kubeconfig.txt','account.P12')
        original = sonuc.input_revision(wt)
        for name in names:
            path = wt / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('SYNTHETIC_LOCAL_SECRET')
        self.assertEqual(sonuc.input_revision(wt), original)
        with patch('kosu.os.open', side_effect=AssertionError('protected source opened')):
            with self.assertRaisesRegex(ValueError, 'plan-product-protected-source'):
                kosu.plan_product(wt)
            with self.assertRaises(kosu.RequestError):
                kosu.backlog_file(wt.resolve(), 'CREDENTIALS.txt')
            with self.assertRaises(kosu.RequestError):
                kosu.backlog_file(wt.resolve(), 'kubeconfig.txt')
        codes = ["from pathlib import Path;assert Path('app.py').read_text()=='value = 1\\n'"]
        codes += [f"from pathlib import Path;Path({name!r}).read_text()" for name in names]
        for role in ('impl','plan','review'):
            results, attempt = self.project_tool_execute(wt, env, [
                {'tool':'bash','command':shlex.join([sys.executable,'-c',code])} for code in codes], role=role)
            self.assertEqual(results[0]['status'], 0, results)
            for result in results[1:]:
                self.assertNotEqual(result['status'], 0, result)
                self.assertIn('PermissionError', result['stderr'])
                self.assertNotIn('SYNTHETIC_LOCAL_SECRET', result['stdout'])
        (wt / '.gitignore').write_text('\n'.join(names) + '\n')
        (wt / 'secret_boundary.py').write_text("from pathlib import Path\n"
            f"for name in {names!r}:\n"
            " try:Path(name).read_text()\n"
            " except PermissionError:pass\n"
            " else:raise AssertionError('secret read allowed: '+name)\n")
        (wt / 'test_secret_boundary.py').write_text("import runpy,unittest\nclass SecretBoundary(unittest.TestCase):\n"
            " def test_secret_boundary(self):runpy.run_path('secret_boundary.py')\n")
        pd = self.home / 'projects/a'
        pd.mkdir(parents=True, exist_ok=True)
        (pd / 'qa-komut').write_text(shlex.join([sys.executable,'-m','unittest','discover','-v']))
        with patch.dict(os.environ, env), patch.object(core, 'QE', self.home):
            result = core.qa_kapisi('a', str(wt), self.home / 'secret-qa.log', 'unused', model='fixture/worker')
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['discovery']['executed'], 1)
        self.assertNotIn('SYNTHETIC_LOCAL_SECRET', (self.home / 'secret-qa.log').read_text())
        for name in names:
            (wt / name).unlink()
        os.link(repo / 'app.py', wt / 'hardlink.txt')
        fdopen = os.fdopen
        def guard_hardlink(fd, *args, **kwargs):
            if os.fstat(fd).st_nlink != 1:
                os.close(fd)
                raise AssertionError('hardlink bytes read')
            return fdopen(fd, *args, **kwargs)
        with patch('kosu.os.fdopen', side_effect=guard_hardlink):
            with self.assertRaisesRegex(ValueError, 'input-source-hardlink'):
                sonuc.input_revision(wt)
        with self.assertRaisesRegex(ValueError, 'plan-product-hardlink'):
            kosu.plan_product(wt)

    def test_env_template_names_are_product_while_real_env_files_stay_protected(self):
        import arsiv
        import kosu
        import sonuc
        repo, wt, parent, rid, env = self.project_tool_fixture()
        for name in ('.env.example', 'config/.env.sample', '.env.template'):
            path = wt / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('KEY=\n')
        subprocess.run(['git', '-C', str(wt), 'add', '.env.example'], check=True, capture_output=True)
        before = sonuc.revision(wt)
        kosu.plan_product(wt)
        self.assertIn('.env.example', arsiv.index(wt))
        digest = sonuc.input_revision(wt)
        (wt / '.env.example').write_text('KEY=changed\n')
        self.assertNotEqual(sonuc.input_revision(wt), digest)
        self.assertNotEqual(sonuc.revision(wt), before)
        for name in ('.env', '.env.local', '.env.production', '.env.example.bak', 'config/.env.sample.old'):
            secret = wt / name
            secret.parent.mkdir(parents=True, exist_ok=True)
            secret.write_text('SYNTHETIC_PRODUCT_SECRET')
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, 'product-protected-source'):
                    sonuc.revision(wt)
                with self.assertRaisesRegex(ValueError, 'plan-product-protected-source'):
                    kosu.plan_product(wt)
                with self.assertRaisesRegex(PermissionError, 'archive-protected-source'):
                    arsiv.index(wt)
            secret.unlink()
        self.assertEqual(sonuc.revision(wt), sonuc.revision(wt))

    def test_product_revision_rejects_secret_names_and_hardlinks_before_git_diff_or_file_reads(self):
        import sonuc
        repo, wt, parent, rid, env = self.project_tool_fixture()
        original = sonuc.revision(wt)
        (wt / 'app.py').write_text('value = 2\n')
        self.assertNotEqual(sonuc.revision(wt), original)
        run = subprocess.run
        def guard_diff(args, *positional, **kwargs):
            if 'diff' in args:
                raise AssertionError('protected product reached git diff')
            return run(args, *positional, **kwargs)
        for name in ('.npmrc', 'auth.json', 'Secrets/account.json', 'account.P12'):
            secret = wt / name
            secret.parent.mkdir(parents=True, exist_ok=True)
            secret.write_text('SYNTHETIC_PRODUCT_SECRET')
            with patch('sonuc.subprocess.run', side_effect=guard_diff), patch.object(Path, 'read_bytes', side_effect=AssertionError('protected product bytes read')):
                with self.assertRaisesRegex(ValueError, 'product-protected-source'):
                    sonuc.revision(wt)
            secret.unlink()
        os.link(repo / 'app.py', wt / 'ordinary-name.txt')
        with patch('sonuc.subprocess.run', side_effect=guard_diff), patch.object(Path, 'read_bytes', side_effect=AssertionError('hardlink product bytes read')):
            with self.assertRaisesRegex(ValueError, 'product-source-hardlink'):
                sonuc.revision(wt)
        (wt / 'ordinary-name.txt').unlink()
        (wt / '.netrc').write_text('SYNTHETIC_PRODUCT_SECRET')
        run(['git', '-C', str(wt), 'add', '.netrc'], check=True, capture_output=True)
        with patch('sonuc.subprocess.run', side_effect=guard_diff), patch.object(Path, 'read_bytes', side_effect=AssertionError('staged secret bytes read')):
            with self.assertRaisesRegex(ValueError, 'product-protected-source'):
                sonuc.revision(wt)

    def test_regular_opencode_tools_get_the_same_scope_and_cannot_read_host_or_provider_secrets(self):
        repo, wt, parent, rid, env = self.project_tool_fixture()
        self.store.update(rid, proje_ust=None)
        outside = self.home / 'outside.txt'
        outside.write_text('PRIVATE_HOST_FIXTURE')
        auth = Path(env['HOME']) / '.local/share/opencode/auth.json'
        self.assertTrue(auth.resolve().is_relative_to(self.home.resolve()))
        auth.parent.mkdir(parents=True, exist_ok=True)
        auth.write_text('SYNTHETIC_PROVIDER_SECRET')
        tools = [{'tool': 'bash', 'command': shlex.join([sys.executable, '-c', code])} for code in (
            "from pathlib import Path; assert Path('app.py').read_text() == 'value = 1\\n'; Path('app.py').write_text('value = 2\\n')",
            f"from pathlib import Path; print(Path({str(outside)!r}).read_text())",
            f"from pathlib import Path; print(Path({str(auth)!r}).read_text())",
            f"from pathlib import Path; Path({str(outside)!r}).write_text('escape')",
            "import os; assert 'SYNTHETIC_PROVIDER_TOKEN' not in os.environ",
        )]
        results, attempt = self.project_tool_execute(wt, env, tools)
        self.assertEqual(results[0]['status'], 0, results)
        self.assertEqual(results[-1]['status'], 0, results)
        for result in results[1:-1]:
            self.assertNotEqual(result['status'], 0, result)
            self.assertNotIn('PRIVATE_HOST_FIXTURE', result['stdout'])
            self.assertNotIn('SYNTHETIC_PROVIDER_SECRET', result['stdout'])
        self.assertEqual(outside.read_text(), 'PRIVATE_HOST_FIXTURE')
        self.assertEqual((wt / 'app.py').read_text(), 'value = 2\n')
        self.assertTrue((attempt.directory / 'tool-policy.json').is_file())

    def test_regular_plan_and_review_can_read_selected_source_but_never_write_it(self):
        repo, wt, parent, rid, env = self.project_tool_fixture()
        self.store.update(rid, proje_ust=None)
        for role in ('plan', 'review'):
            with self.subTest(role=role):
                tools = [{'tool': 'bash', 'command': shlex.join([sys.executable, '-c', code])} for code in (
                    "from pathlib import Path; assert Path('app.py').read_text() == 'value = 1\\n'",
                    "from pathlib import Path; Path('app.py').write_text('changed')",
                )]
                results, attempt = self.project_tool_execute(wt, env, tools, role=role)
                self.assertEqual(results[0]['status'], 0, results)
                self.assertNotEqual(results[1]['status'], 0, results)
                self.assertFalse(json.loads((attempt.directory / 'tool-policy.json').read_text())['writable'])
                self.assertEqual((wt / 'app.py').read_text(), 'value = 1\n')

    def test_project_readonly_tools_keep_product_and_local_temporary_test_work_separate(self):
        repo, wt, parent, rid, env = self.project_tool_fixture()
        command = shlex.join([sys.executable, '-c', "import tempfile; from pathlib import Path; p=Path(tempfile.mktemp()); p.write_text('test'); assert p.read_text() == 'test'; print(p)"])
        tools = [{'tool': 'bash', 'command': command},
                 {'tool': 'bash', 'command': shlex.join([sys.executable, '-c', "from pathlib import Path; Path('app.py').write_text('changed')"])}]
        results, attempt = self.project_tool_execute(wt, env, tools, role='review')
        self.assertEqual(results[0]['status'], 0, results)
        self.assertNotEqual(results[1]['status'], 0)
        policy = json.loads((attempt.directory / 'tool-policy.json').read_text())
        self.assertTrue(Path(results[0]['stdout'].strip()).is_relative_to(policy['scratch']))
        self.assertFalse(policy['writable'])
        self.assertEqual((wt / 'app.py').read_text(), 'value = 1\n')

    def test_project_planner_uses_a_non_git_readonly_directory_without_worktree_write_authority(self):
        repo, wt, parent, rid, env = self.project_tool_fixture()
        planning = self.store.task_dir(rid, 'g1') / 'planning'
        planning.mkdir()
        tools = [{'tool': 'bash', 'command': shlex.join([sys.executable, '-c', f"from pathlib import Path; assert Path({str(repo / 'app.py')!r}).read_text() == 'value = 1\\n'"])},
                 {'tool': 'bash', 'command': shlex.join([sys.executable, '-c', f"from pathlib import Path; Path({str(wt / 'app.py')!r}).write_text('changed')"])}]
        results, attempt = self.project_tool_execute(planning, env, tools, role='plan')
        self.assertEqual(results[0]['status'], 0, results)
        self.assertNotEqual(results[1]['status'], 0)
        self.assertFalse(json.loads((attempt.directory / 'tool-policy.json').read_text())['writable'])
        self.assertEqual((wt / 'app.py').read_text(), 'value = 1\n')

    def test_project_shell_cannot_read_synthetic_secrets_or_replace_nested_git_metadata(self):
        repo, wt, parent, rid, env = self.project_tool_fixture()
        (wt / '.env.fixture').write_text('SYNTHETIC_SECRET=fixture-only\n')
        nested = wt / 'nested'
        nested.mkdir()
        tools = [
            {'tool': 'bash', 'command': shlex.join([sys.executable, '-c', "from pathlib import Path; print(Path('.env.fixture').read_text())"])},
            {'tool': 'bash', 'command': shlex.join([sys.executable, '-c', "from pathlib import Path; Path('nested/.GiT').write_text('gitdir: /tmp/escape')"])},
            {'tool': 'bash', 'command': shlex.join([sys.executable, '-c', f"import os,signal; os.kill({os.getpid()},signal.SIGCONT)"])},
            {'tool': 'bash', 'command': shlex.join([sys.executable, '-c', f"import os; os.link({str(self.store.path(parent)/'run.json')!r},'hardlink-alias')"])},
            {'tool': 'read', 'filePath': str(wt / '.env.fixture')},
        ]
        results, attempt = self.project_tool_execute(wt, env, tools)
        for index, result in enumerate(results[:-1]):
            with self.subTest(tool=tools[index]):
                self.assertNotEqual(result['status'], 0, result)
                self.assertNotIn('SYNTHETIC_SECRET', result['stdout'])
        self.assertIn('CADRE_TOOL_DENIED', results[-1]['denied'])
        self.assertFalse((nested / '.git').exists())

    def test_project_sandbox_absence_adapter_and_recording_failure_start_no_executor(self):
        from oturum import Attempt
        repo, wt, parent, rid, env = self.project_tool_fixture()
        marker = self.home / 'unexpected-executor'
        command = [sys.executable, '-c', f"from pathlib import Path; Path({str(marker)!r}).touch()"]
        with patch.dict(os.environ, env):
            for adapter, platform in [('claude', sys.platform), ('opencode', 'unsupported')]:
                with self.subTest(adapter=adapter), patch('oturum.sys.platform', platform):
                    attempt = Attempt(adapter, 'impl', 'fixture/worker', 'work')
                    with self.assertRaisesRegex(PermissionError, 'project-tool-sandbox-unavailable'):
                        attempt.launch(command, str(wt), self.home / 'log', env, ())
                    self.assertEqual(attempt.data['outcome'], 'not_started')
                    self.assertEqual(self.store.read(rid)['tool_boundary_wait'], 'project-tool-sandbox-unavailable')
            os.link(self.store.path(parent) / 'run.json', wt / 'existing-hardlink')
            attempt = Attempt('opencode', 'impl', 'fixture/worker', 'work')
            with self.assertRaisesRegex(PermissionError, 'project-tool-hardlink-untrusted'):
                attempt.launch(command, str(wt), self.home / 'log', env, ())
            (wt / 'existing-hardlink').unlink()
            attempt = Attempt('opencode', 'impl', 'fixture/worker', 'work')
            with patch('oturum.atomic_json', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    attempt.launch(command, str(wt), self.home / 'log', env, ())
            self.assertFalse(marker.exists())
            self.assertTrue(attempt.lease.fd is None)

    def test_project_qa_cannot_use_generated_tests_to_write_operator_records(self):
        repo, wt, parent, rid, env = self.project_tool_fixture()
        pd = self.home / 'projects/a'
        pd.mkdir(parents=True, exist_ok=True)
        (pd / 'qa-komut').write_text(shlex.join([sys.executable, '-m', 'unittest', 'discover', '-s', '.']))
        parent_path = self.store.path(parent) / 'run.json'
        before = parent_path.read_bytes()
        attack = wt / 'test_boundary.py'
        attack.write_text("import unittest\nfrom pathlib import Path\nclass Boundary(unittest.TestCase):\n"
                          "    def test_write(self):\n"
                          f"        Path({str(parent_path)!r}).write_text('{{}}')\n")
        with patch.dict(os.environ, env), patch.object(core, 'QE', self.home):
            failed = core.qa_kapisi('a', str(wt), self.home / 'qa.log', 'unused', model='claude/fixture')
        self.assertEqual(failed['status'], 'FAIL', failed)
        self.assertEqual(parent_path.read_bytes(), before)

        attack.write_text("import unittest\nfrom pathlib import Path\nclass Boundary(unittest.TestCase):\n"
                          "    def test_write(self):\n"
                          "        with self.assertRaises(PermissionError):\n"
                          f"            Path({str(parent_path)!r}).write_text('{{}}')\n")
        with patch.dict(os.environ, env), patch.object(core, 'QE', self.home):
            passed = core.qa_kapisi('a', str(wt), self.home / 'qa.log', 'unused', model='claude/fixture')
        self.assertEqual(passed['status'], 'PASS', passed)
        self.assertEqual(passed['discovery']['executed'], 1)
        self.assertEqual(parent_path.read_bytes(), before)

    def test_regular_qa_confines_generated_tests_and_nested_processes_without_losing_discovery(self):
        repo, wt, parent, rid, env = self.project_tool_fixture()
        self.store.update(rid, proje_ust=None)
        outside = self.home / 'host-private.txt'
        outside.write_text('SYNTHETIC_HOST_SECRET')
        witness = self.store.path(rid) / 'operator-only.txt'
        auth = Path(env['HOME']) / '.local/share/opencode/auth.json'
        self.assertTrue(auth.resolve().is_relative_to(self.home.resolve()))
        auth.parent.mkdir(parents=True)
        auth.write_text('SYNTHETIC_PROVIDER_SECRET')
        body = ("import os,socket,subprocess,sys,tempfile,unittest\nfrom pathlib import Path\n"
            "class Boundary(unittest.TestCase):\n"
            " def test_product_and_scratch(self):\n"
            "  self.assertEqual(Path('app.py').read_text(),'value = 1\\n')\n"
            "  with tempfile.TemporaryDirectory() as d: Path(d,'result').write_text('allowed')\n"
            " def test_operator_record(self):\n"
            "  with self.assertRaises(PermissionError):\n"
            f"   Path({str(witness)!r}).write_text('unauthorized')\n"
            " def test_host_secret(self):\n"
            "  with self.assertRaises(PermissionError):\n"
            f"   Path({str(outside)!r}).read_text()\n"
            " def test_provider_secret(self):\n"
            "  with self.assertRaises(PermissionError):\n"
            f"   Path({str(auth)!r}).read_text()\n"
            "  self.assertNotIn('SYNTHETIC_PROVIDER_TOKEN',os.environ)\n"
            " def test_network(self):\n"
            "  with socket.socket() as s:\n"
            "   with self.assertRaises(PermissionError): s.connect(('127.0.0.1',9))\n"
            " def test_nested_process(self):\n"
            f"  p=subprocess.run([sys.executable,'-c',\"from pathlib import Path; Path({str(witness)!r}).write_text('escape')\"],capture_output=True,text=True)\n"
            "  self.assertNotEqual(p.returncode,0)\n"
            "  self.assertIn('PermissionError',p.stderr)\n")
        (wt / 'test_boundary.py').write_text(body)
        pd = self.home / 'projects/a'
        pd.mkdir(parents=True, exist_ok=True)
        (pd / 'qa-komut').write_text(shlex.join([sys.executable, '-m', 'unittest', 'discover', '-v']))
        with patch.dict(os.environ, env), patch.object(core, 'QE', self.home), \
             patch.object(core.subprocess, 'Popen', wraps=core.subprocess.Popen) as processes:
            result = core.qa_kapisi('a', str(wt), self.home / 'qa.log', 'unused', model='claude/fixture')
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['discovery']['executed'], 6)
        self.assertFalse(witness.exists())
        self.assertEqual(outside.read_text(), 'SYNTHETIC_HOST_SECRET')
        raw = Path(result['attempts'][0]['output']).read_text()
        self.assertNotIn('SYNTHETIC_HOST_SECRET', raw)
        self.assertNotIn('SYNTHETIC_PROVIDER_SECRET', raw)
        policy = json.loads(Path(result['attempts'][0]['output']).with_name('tool-policy.json').read_text())
        self.assertEqual(policy['worktree'], str(wt.resolve()))
        guarded = [call for call in processes.call_args_list if '/usr/bin/env' in call.args[0]]
        self.assertTrue(guarded)
        for call in guarded:
            self.assertNotIn('SYNTHETIC_PROVIDER_TOKEN', call.kwargs['env'])

    def test_regular_qa_sandbox_failure_starts_no_generated_test_process(self):
        repo, wt, parent, rid, env = self.project_tool_fixture()
        self.store.update(rid, proje_ust=None)
        marker = wt / 'unauthorized-executor'
        pd = self.home / 'projects/a'
        pd.mkdir(parents=True, exist_ok=True)
        (pd / 'qa-komut').write_text(shlex.join([sys.executable, '-c', f"from pathlib import Path; Path({str(marker)!r}).touch()"]))
        with patch.dict(os.environ, env), patch.object(core, 'QE', self.home), patch('oturum.sys.platform', 'unsupported'):
            result = core.qa_kapisi('a', str(wt), self.home / 'qa.log', 'unused', model='claude/fixture')
        self.assertNotEqual(result['status'], 'PASS', result)
        self.assertFalse(marker.exists())

        self.assertEqual(self.store.read(rid)['tool_boundary_wait'], 'project-tool-sandbox-unavailable')
        write = core.atomic_json
        def fail_policy(path, value):
            if Path(path).name == 'tool-policy.json':
                raise OSError('disk full')
            return write(path, value)
        with patch.dict(os.environ, env), patch.object(core, 'QE', self.home), patch.object(core, 'atomic_json', side_effect=fail_policy):
            result = core.qa_kapisi('a', str(wt), self.home / 'qa.log', 'unused', model='claude/fixture')
        self.assertNotEqual(result['status'], 'PASS', result)
        self.assertFalse(marker.exists())


    def test_regular_baseline_and_formatter_apply_the_same_filesystem_boundary(self):
        repo, wt, parent, rid, env = self.project_tool_fixture()
        self.store.update(rid, proje_ust=None)
        outside = self.home / 'operator-only.txt'
        outside.write_text('preserved')
        denied = shlex.join([sys.executable, '-c', f"from pathlib import Path; Path({str(outside)!r}).write_text('escape')"])
        allowed = shlex.join([sys.executable, '-c', "import tempfile; from pathlib import Path; assert Path('app.py').is_file(); p=Path(tempfile.mktemp()); p.write_text('allowed')"])
        with patch.dict(os.environ, env), patch.object(core, 'QE', self.home):
            self.assertNotEqual(core._taban_komut(wt, denied, self.home / 'baseline-denied.log', 5), 0)
            self.assertEqual(core._taban_komut(wt, allowed, self.home / 'baseline-allowed.log', 5), 0)
            for command, succeeds in [(denied, False), (shlex.join([sys.executable, '-c', "from pathlib import Path; Path('app.py').write_text('value = 2\\n')"]), True)]:
                output = self.store.task_dir(rid, 'qa') / 'checks' / core.uuid.uuid4().hex
                output.mkdir(parents=True)
                result = core._bicim_kos(wt, command, output / 'format.log', self.home / 'format.log')
                self.assertEqual(result['rc'] == 0, succeeds, result)
        self.assertEqual(outside.read_text(), 'preserved')
        self.assertEqual((wt / 'app.py').read_text(), 'value = 2\n')



    def test_regular_qa_removes_its_scratch_on_success_and_failure_without_following_links(self):
        repo, wt, parent, rid, env = self.project_tool_fixture()
        self.store.update(rid, proje_ust=None)
        outside = self.home / 'preserved.txt'
        outside.write_text('preserved')
        pd = self.home / 'projects/a'
        pd.mkdir(parents=True, exist_ok=True)
        (pd / 'qa-komut').write_text(shlex.join([sys.executable, '-m', 'unittest', 'discover']))
        body = ("import os,unittest\nfrom pathlib import Path\n"
                "class Scratch(unittest.TestCase):\n"
                " def test_scratch(self):\n"
                "  p=Path(os.environ['TMPDIR'])\n"
                "  (p/'temporary.txt').write_text('temporary')\n"
                f"  (p/'outside-link').symlink_to({str(outside)!r})\n")
        for failure in (False, True):
            (wt / 'test_scratch.py').write_text(body + ("  self.fail('intentional rejection')\n" if failure else ''))
            with patch.dict(os.environ, env), patch.object(core, 'QE', self.home):
                result = core.qa_kapisi('a', str(wt), self.home / 'qa.log', 'unused', model='claude/fixture')
            self.assertEqual(result['status'], 'FAIL' if failure else 'PASS', result)
            policy_path = Path(result['attempts'][0]['output']).with_name('tool-policy.json')
            policy = json.loads(policy_path.read_text())
            self.assertFalse(Path(policy['scratch']).exists())
            self.assertTrue(policy_path.is_file())
            self.assertTrue(Path(result['attempts'][0]['evidence']).is_file())
            self.assertEqual(outside.read_text(), 'preserved')

    def test_baseline_and_formatter_cleanup_scratch_on_timeout_cancel_and_launch_failure(self):
        repo, wt, parent, rid, env = self.project_tool_fixture()
        self.store.update(rid, proje_ust=None)
        scratch_root = self.home / 'tool-tmp'
        active = scratch_root / core.uuid.uuid4().hex
        active.mkdir(parents=True)
        (active / 'owned-by-another-call').write_text('active')
        command = shlex.join([sys.executable, '-c', "import os,time; from pathlib import Path; Path(os.environ['TMPDIR'],'data').write_text('temporary'); time.sleep(10)"])
        with patch.dict(os.environ, {**env, 'CADRE_KILL_GRACE': '0.1'}), patch.object(core, 'QE', self.home):
            with self.assertRaises(subprocess.TimeoutExpired):
                core._taban_komut(wt, command, self.home / 'timeout.log', .3)
            self.assertEqual(list(scratch_root.iterdir()), [active])
            (self.store.path(rid) / 'cancel.flag').touch()
            with self.assertRaises(core.CancelledRun):
                core._taban_komut(wt, command, self.home / 'cancel.log', 5)
            self.assertEqual(list(scratch_root.iterdir()), [active])
            (self.store.path(rid) / 'cancel.flag').unlink()
            real_popen = core.subprocess.Popen
            def failed_launch(args, *positional, **kwargs):
                if '/usr/bin/env' in args:
                    raise OSError('executor launch failed')
                return real_popen(args, *positional, **kwargs)
            with patch.object(core.subprocess, 'Popen', side_effect=failed_launch):
                with self.assertRaisesRegex(OSError, 'executor launch failed'):
                    core._taban_komut(wt, command, self.home / 'launch.log', 5)
            self.assertEqual(list(scratch_root.iterdir()), [active])
            output = self.store.task_dir(rid, 'qa') / 'checks' / core.uuid.uuid4().hex
            output.mkdir(parents=True)
            def replaced_scratch(args, *positional, **kwargs):
                if '/usr/bin/env' in args:
                    scratch = Path(next(value.split('=', 1)[1] for value in args if value.startswith('TMPDIR=')))
                    scratch.rmdir()
                    scratch.symlink_to(active, target_is_directory=True)
                    raise OSError('executor launch failed')
                return real_popen(args, *positional, **kwargs)
            with patch.object(core.subprocess, 'Popen', side_effect=replaced_scratch):
                with self.assertRaisesRegex(PermissionError, 'project-tool-scratch-untrusted'):
                    core._taban_komut(wt, command, self.home / 'replaced.log', 5)
            links = [p for p in scratch_root.iterdir() if p.is_symlink()]
            self.assertEqual(len(links), 1)
            self.assertEqual((active / 'owned-by-another-call').read_text(), 'active')
            links[0].unlink()
            result = core._bicim_kos(wt, shlex.join([sys.executable, '-c', "import os; from pathlib import Path; Path(os.environ['TMPDIR'],'data').write_text('temporary'); raise SystemExit(1)"]), output / 'format.log', self.home / 'format.log')
            self.assertEqual(result['rc'], 1)
            self.assertEqual(list(scratch_root.iterdir()), [active])
            wait = core.wait_process
            def short_wait(process, timeout, *args, **kwargs):
                return wait(process, .3, *args, **kwargs)
            with patch.object(core, 'wait_process', side_effect=short_wait):
                result = core._bicim_kos(wt, command, output / 'format-timeout.log', self.home / 'format.log')
            self.assertEqual(result['rc'], 124)
            self.assertEqual(list(scratch_root.iterdir()), [active])
        self.assertEqual((active / 'owned-by-another-call').read_text(), 'active')

    def test_concurrent_baselines_cleanup_only_their_own_scratch(self):
        from concurrent.futures import ThreadPoolExecutor
        repo, wt, parent, rid, env = self.project_tool_fixture()
        self.store.update(rid, proje_ust=None)
        code = "import os,time; from pathlib import Path; p=Path(os.environ['TMPDIR']); (p/'data').write_text('temporary'); time.sleep(.3); assert (p/'data').read_text()=='temporary'"
        command = shlex.join([sys.executable, '-c', code])
        with patch.dict(os.environ, env), patch.object(core, 'QE', self.home):
            with ThreadPoolExecutor(max_workers=2) as executor:
                pending = [executor.submit(core._taban_komut, wt, command, self.home / f'parallel-{i}.log', 5) for i in range(2)]
                self.assertEqual([p.result() for p in pending], [0, 0])
        self.assertEqual(list((self.home / 'tool-tmp').iterdir()), [])


    def test_real_tool_executor_cleans_scratch_and_preserves_attempt_evidence_on_repeat_and_launch_error(self):
        from oturum import Attempt
        repo, wt, parent, rid, env = self.project_tool_fixture()
        tools = [{'tool': 'bash', 'command': shlex.join([sys.executable, '-c', "import os; from pathlib import Path; Path(os.environ['TMPDIR'],'temporary').write_text('temporary')"])}]
        policies = []
        for _ in range(2):
            results, attempt = self.project_tool_execute(wt, env, tools)
            self.assertEqual(results[0]['status'], 0, results)
            policy = json.loads((attempt.directory / 'tool-policy.json').read_text())
            policies.append(policy['scratch'])
            self.assertFalse(Path(policy['scratch']).exists())
            self.assertTrue(attempt.stdout_path.is_file())
            self.assertEqual(json.loads(attempt.path.read_text())['outcome'], 'finished')
        self.assertEqual(len(set(policies)), 2)
        with patch.dict(os.environ, env):
            attempt = Attempt('opencode', 'impl', 'fixture/worker', 'work')
            with patch('oturum.OC.launch', side_effect=OSError('executor launch failed')):
                with self.assertRaisesRegex(OSError, 'executor launch failed'):
                    attempt.launch(['node', 'unused'], str(wt), self.home / 'tool.log', env, ())
        policy = json.loads((attempt.directory / 'tool-policy.json').read_text())
        self.assertFalse(Path(policy['scratch']).exists())
        self.assertEqual(json.loads(attempt.path.read_text())['outcome'], 'not_started')
        self.assertEqual(list((self.home / 'tool-tmp').iterdir()), [])


    def test_regular_claude_is_rejected_before_an_unconfined_executor_starts(self):
        from oturum import Attempt
        repo, wt, parent, rid, env = self.project_tool_fixture()
        self.store.update(rid, proje_ust=None)
        marker = self.home / 'unconfined-claude'
        process = None
        with patch.dict(os.environ, env):
            attempt = Attempt('claude', 'impl', 'claude/fixture', 'work')
            command = [sys.executable, '-c', f"from pathlib import Path; import json; Path({str(marker)!r}).touch(); print(json.dumps({{'session_id':{attempt.data['session_id']!r}}}))"]
            try:
                try:
                    process = attempt.launch(command, str(wt), self.home / 'claude.log', env, ())
                except PermissionError as error:
                    self.assertEqual(str(error), 'project-tool-sandbox-unavailable')
                else:
                    process.wait(timeout=5)
                    self.fail('unconfined Claude executor was started')
            finally:
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
        self.assertFalse(marker.exists())
        self.assertEqual(self.store.read(rid)['tool_boundary_wait'], 'project-tool-sandbox-unavailable')
        self.assertEqual(json.loads(attempt.path.read_text())['outcome'], 'not_started')


    def detached_scope_observer(self, kind):
        import kosu
        repo, wt, parent, rid, env = self.project_tool_fixture()
        self.store.update(rid, proje_ust=None)
        env.update(PYTHONPATH=str(LIB), PYTHONDONTWRITEBYTECODE='1', CADRE_KILL_GRACE='.1',
                   OPENCODE_DB=str(self.home / 'absent.db'))
        marker = wt / 'detached-scope.json'
        ready = wt / 'observer-result.json'
        script = wt / 'detach.py'
        script.write_text("import json,os,time\nfrom pathlib import Path\n"
            "scratch=Path(os.environ['TMPDIR'])\n(scratch/'preserved').touch()\n"
            "pid=os.fork()\n"
            "if not pid:\n"
            " os.setsid()\n nested=os.fork()\n"
            " if nested: os._exit(0)\n"
            " os.closerange(0,256)\n"
            f" Path({str(marker)!r}).write_text(json.dumps({{'pid':os.getpid(),'scratch':str(scratch)}}))\n"
            " time.sleep(60)\n os._exit(0)\n"
            "os.waitpid(pid,0)\n"
            f"while not Path({str(marker)!r}).exists(): time.sleep(.01)\n"
            + ("time.sleep(60)\n" if kind == 'qa-live' else ''))
        command = shlex.join([sys.executable, str(script)])
        executor = self.home / 'detached-tools.mjs'
        executor.write_text(f"import plugin from {json.dumps((LIB / 'cadre-plugin.mjs').as_uri())};\n"
            "import {spawnSync} from 'node:child_process';\nconst hooks=await plugin();\n"
            f"const output={{args:{{command:{json.dumps(command)}}}}};\n"
            "await hooks['tool.execute.before']({tool:'bash'},output);\n"
            "const result=spawnSync('/bin/sh',['-c',output.args.command],{encoding:'utf8'});\n"
            "if(result.status!==0)throw new Error(result.stderr);\n"
            "console.log(JSON.stringify({sessionID:'ses_detached_scope'}));\n"
            f"await new Promise(resolve=>setTimeout(resolve,{60000 if kind == 'worker' else 0}));\n")
        observer = self.home / 'detached-observer.py'
        observer.write_text("import json,os,sys,time\nfrom pathlib import Path\n"
            "from kosu import FileLock,RunStore,process_identity\nfrom oturum import Attempt\nimport qe_core as core\n"
            "store=RunStore();rid=os.environ['CADRE_RUN_ID'];core.QE=store.home\n"
            "owner=FileLock(store.path(rid)/'.owner.lock');owner.acquire()\n"
            "store.update(rid,state='running',owner_pid=os.getpid(),owner_identity=process_identity(os.getpid()))\n"
            "try:\n"
            + (" attempt=Attempt('opencode','impl','fixture/worker','preserved work')\n"
               f" process=attempt.launch(['node',{str(executor)!r}],{str(wt)!r},store.home/'detached.log',dict(os.environ),(owner.fd,))\n"
               " while process.poll() is None:time.sleep(.01)\n result={'status':'returned'}\n" if kind.startswith('worker') else
               f" result={{'status':core._taban_komut(Path({str(wt)!r}),{command!r},store.home/'detached.log',15)}}\n")
            + "except PermissionError as e:result={'status':'waiting','reason':str(e)}\n"
            f"Path({str(ready)!r}).write_text(json.dumps(result))\ntime.sleep(60)\n")
        with (self.home / 'detached-observer.log').open('w') as output:
            observer_process = subprocess.Popen([sys.executable, str(observer)], env=env, stdout=output, stderr=output)
        def stop_observer():
            if observer_process.poll() is None:
                observer_process.kill()
            observer_process.wait(timeout=5)
        self.addCleanup(stop_observer)
        deadline = time.monotonic() + 10
        while not marker.exists() and observer_process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(marker.exists(), (self.home / 'detached-observer.log').read_text())
        info = json.loads(marker.read_text())
        identity = kosu.process_identity(info['pid'])
        def stop_descendant():
            if identity and kosu.process_identity(info['pid']) == identity:
                os.kill(info['pid'], signal.SIGKILL)
        self.addCleanup(stop_descendant)
        pattern = 'tasks/g1/attempts/*/attempt.json' if kind.startswith('worker') else 'tasks/qa/tool-checks/*/check.json'
        paths = list(self.store.path(rid).glob(pattern))
        self.assertEqual(len(paths), 1)
        if kind.startswith('worker'):
            stdout = paths[0].with_name('stdout.jsonl')
            deadline = time.monotonic() + 10
            while b'ses_detached_scope' not in stdout.read_bytes() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIn(b'ses_detached_scope', stdout.read_bytes())
        return observer_process, info, paths[0], rid, env, repo, ready

    def test_detached_qa_kernel_scope_keeps_normal_exit_honest_and_reclaims_after_observer_sigkill(self):
        import kosu
        from oturum import recover_tool_artifacts
        observer, info, path, rid, env, repo, ready = self.detached_scope_observer('qa')
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(ready.exists())
        self.assertEqual(json.loads(ready.read_text()), {'status':'waiting','reason':'project-tool-process-group-active'})
        from oturum import tool_scope_members
        policy = json.loads((path.parent / 'tool-policy.json').read_text())
        self.assertIn(info['pid'], [p['pid'] for p in tool_scope_members(self.store, path.parent, policy)])
        self.assertTrue(Path(info['scratch']).exists())
        self.assertFalse((path.parent / 'tool-retired.json').exists())
        observer.kill()
        observer.wait(timeout=5)
        before = path.read_bytes()
        self.store.update(rid, state='cancelled')
        with patch.dict(os.environ, env), patch('kosu._baslat', side_effect=AssertionError('terminal restart')):
            self.assertEqual(kosu.revive(self.store, LIB.parent), [])
        self.assertIsNone(kosu.process_identity(info['pid']))
        self.assertFalse(Path(info['scratch']).exists())
        self.assertTrue((path.parent / 'tool-retired.json').is_file())
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(recover_tool_artifacts(self.store, rid)['unknown'])
        self.assertEqual((repo / 'app.py').read_text(), 'value = 1\n')
        self.assertEqual(self.store.read(rid)['state'], 'cancelled')

    def test_detached_tools_recovery_keeps_other_scope_and_raw_session_and_repeats_without_replaying(self):
        import kosu
        from oturum import recover_tool_artifacts
        observer, info, path, rid, env, repo, ready = self.detached_scope_observer('worker')
        self.assertIsNotNone(kosu.process_identity(info['pid']))
        self.assertTrue(Path(info['scratch']).exists())
        observer.kill()
        observer.wait(timeout=5)
        self.store.update(rid, state='cancelled')
        raw = (path.parent / 'stdout.jsonl').read_bytes()
        self.assertIn(b'ses_detached_scope', raw)
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'], start_new_session=True)
        try:
            with patch.dict(os.environ, env), patch('kosu._baslat', side_effect=AssertionError('terminal restart')):
                self.assertEqual(kosu.revive(self.store, LIB.parent), [])
                self.assertEqual(kosu.revive(self.store, LIB.parent), [])
            self.assertIsNone(kosu.process_identity(info['pid']))
            self.assertFalse(Path(info['scratch']).exists())
            self.assertEqual((path.parent / 'loader').stat().st_mode & 0o777, 0o700)
            self.assertEqual((path.parent / 'stdout.jsonl').read_bytes(), raw)
            self.assertFalse(recover_tool_artifacts(self.store, rid)['unknown'])
            self.assertIsNone(unrelated.poll())
            self.assertEqual(self.store.read(rid)['tasks']['g1']['attempt_count'], 1)
            self.assertEqual((repo / 'app.py').read_text(), 'value = 1\n')
        finally:
            unrelated.kill()
            unrelated.wait(timeout=5)

    def test_detached_scope_marker_substitution_native_failure_and_changed_policy_never_authorize_killing(self):
        import ctypes
        import kosu
        import oturum
        from oturum import recover_tool_artifacts
        observer, info, path, rid, env, repo, ready = self.detached_scope_observer('qa')
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(ready.exists())
        observer.kill()
        observer.wait(timeout=5)
        marker = path.parent / 'process-allow'
        saved = path.parent / 'original-marker'
        self.assertTrue(marker.is_file())
        marker.rename(saved)
        marker.touch(mode=0o400)
        report = recover_tool_artifacts(self.store, rid, reclaim=True)
        self.assertTrue(report['unknown'], report)
        self.assertIsNotNone(kosu.process_identity(info['pid']))
        self.assertTrue(Path(info['scratch']).exists())
        saved.replace(marker)
        probe = oturum.sandbox_scope_decisions
        swapped = []
        def swap_while_sampling(pid, markers, **kwargs):
            if not swapped:
                marker.rename(saved)
                marker.touch(mode=0o400)
                swapped.append(True)
            return probe(pid, markers, **kwargs)
        with patch('oturum.sandbox_scope_decisions', side_effect=swap_while_sampling):
            self.assertTrue(recover_tool_artifacts(self.store, rid, reclaim=True)['unknown'])
        self.assertTrue(swapped)
        self.assertIsNotNone(kosu.process_identity(info['pid']))
        self.assertTrue(Path(info['scratch']).exists())
        saved.replace(marker)
        native = ctypes.CDLL
        def fail_query(name, *args, **kwargs):
            if name == '/usr/lib/libsandbox.dylib':
                raise OSError('native sandbox query unavailable')
            return native(name, *args, **kwargs)
        with patch('ctypes.CDLL', side_effect=fail_query):
            self.assertTrue(recover_tool_artifacts(self.store, rid, reclaim=True)['unknown'])
        self.assertIsNotNone(kosu.process_identity(info['pid']))
        policy_path = path.parent / 'tool-policy.json'
        original = policy_path.read_bytes()
        policy = json.loads(original)
        policy['profile'] += '\n(allow default)'
        policy_path.write_text(json.dumps(policy))
        self.assertTrue(recover_tool_artifacts(self.store, rid, reclaim=True)['unknown'])
        self.assertIsNotNone(kosu.process_identity(info['pid']))
        self.assertTrue(Path(info['scratch']).exists())
        policy_path.write_bytes(original)
        self.assertFalse(recover_tool_artifacts(self.store, rid, reclaim=True)['unknown'])
        self.assertIsNone(kosu.process_identity(info['pid']))
        self.assertFalse(Path(info['scratch']).exists())
        self.assertEqual((repo / 'app.py').read_text(), 'value = 1\n')

    def test_unobserved_qa_primary_death_reclaims_its_kernel_scope_without_touching_another_live_scope(self):
        import kosu
        from oturum import recover_tool_artifacts
        observer, info, path, rid, env, repo, ready = self.detached_scope_observer('qa-live')
        other = AdapterTests()
        other.setUp()
        self.addCleanup(other.doCleanups)
        other_observer, other_info, other_path, other_rid, other_env, other_repo, other_ready = other.detached_scope_observer('qa-live')
        other_record = other_path.read_bytes()
        receipt = json.loads((path.parent / 'process.json').read_text())
        self.assertNotIn('stop_observed', receipt)
        observer.kill()
        observer.wait(timeout=5)
        os.kill(receipt['process_identity']['pid'], signal.SIGKILL)
        self.store.update(rid, state='cancelled')
        deadline = time.monotonic() + 5
        with patch.dict(os.environ, env), patch('kosu._baslat', side_effect=AssertionError('terminal restart')):
            while Path(info['scratch']).exists() and time.monotonic() < deadline:
                self.assertEqual(kosu.revive(self.store, LIB.parent), [])
                time.sleep(.01)
        self.assertIsNone(kosu.process_identity(info['pid']))
        self.assertFalse(Path(info['scratch']).exists())
        self.assertTrue((path.parent / 'tool-retired.json').is_file())
        self.assertIsNone(other_observer.poll())
        self.assertIsNotNone(kosu.process_identity(other_info['pid']))
        self.assertTrue(Path(other_info['scratch']).exists())
        self.assertEqual(other_path.read_bytes(), other_record)
        self.assertTrue(recover_tool_artifacts(other.store, other_rid)['active'] == [])
        self.assertEqual(other.store.read(other_rid)['state'], 'running')
        other_observer.kill()
        other_observer.wait(timeout=5)
        other.store.update(other_rid, state='cancelled')
        with patch.dict(os.environ, other_env):
            kosu.reclaim_orphans(other.store, other.store.read(other_rid))
        self.assertFalse(Path(other_info['scratch']).exists())
        self.assertIsNone(kosu.process_identity(other_info['pid']))

    def test_installed_engine_root_allows_scoped_worktree_and_scratch_but_protects_control_files(self):
        """N-ENV02: allowed product writes are not lost when the installed engine code and the data share a root."""
        import oturum
        repo, wt, parent, rid, env = self.project_tool_fixture()
        installed = self.home / 'lib/oturum.py'
        installed.parent.mkdir()
        shutil.copyfile(oturum.__file__, installed)
        protected = [installed, repo / 'app.py', self.home / 'USAGE.md',
                     self.store.path(rid) / 'run.json', self.home / 'projects/a/policy',
                     self.home / 'zincir/budget-fixture', self.home / 'outside.txt',
                     wt / '.git', wt / 'unplanned.txt']
        for name in ('bin/worker.sh', 'hooks/guard', 'tests/check.py'):
            protected.append(self.home / name)
        for path in protected:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('protected fixture\n')
        originals = {path: path.read_bytes() for path in protected}
        record = {**self.store.read(rid), 'job_type': 'code',
                  'request': {'input_mode': 'goal', 'target_path': str(repo)}}
        history = {'history': [{'plan': {'gorevler': [
            {'id': 'g1', 'dosyalar': ['app.py', 'new/nested/result.txt']}]}}]}
        for role, marker in (('impl', 'a'), ('qa', 'b'), ('review', 'c')):
            with self.subTest(role=role), patch.object(oturum, '__file__', str(installed)), \
                    patch.object(self.store, 'plan_view', return_value=history):
                directory = self.store.task_dir(rid, role) / 'tool-checks' / (marker * 32)
                policy = oturum.project_tool_policy(self.store, record, directory, str(wt), 'opencode', role, env)
                self.addCleanup(oturum.cleanup_tool_scratch, self.store, policy)
                writable = role != 'review'
                blocked = protected if role == 'impl' else [p for p in protected if p != wt / 'unplanned.txt']
                code = ("from pathlib import Path\n"
                        f"scratch = Path({policy['scratch']!r}) / 'local.txt'\n"
                        "scratch.write_text('local'); assert scratch.read_text() == 'local'\n")
                if writable:
                    code += ("Path('app.py').write_text('value = 2\\n')\n"
                             "Path('new/nested').mkdir(parents=True, exist_ok=True)\n"
                             "Path('new/nested/result.txt').write_text('allowed')\n")
                else:
                    blocked += [wt / 'app.py', wt / 'new/nested/result.txt']
                code += (f"for path in {list(map(str, blocked))!r}:\n"
                         " try: Path(path).write_text('escaped')\n"
                         " except PermissionError: pass\n"
                         " else: raise AssertionError('protected write succeeded: ' + path)\n")
                result = subprocess.run([policy['sandbox'], '-p', policy['profile'], sys.executable, '-c', code],
                                        cwd=wt, env=env, text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                for path, original in originals.items():
                    self.assertEqual(path.read_bytes(), original, str(path))
        self.assertEqual((repo / 'app.py').read_text(), 'value = 1\n')
        self.assertEqual((wt / 'app.py').read_text(), 'value = 2\n')

    def test_declared_venv_can_read_its_linked_python_distribution_without_opening_its_parent(self):
        from oturum import cleanup_tool_scratch, project_tool_policy
        repo, wt, parent, rid, env = self.project_tool_fixture()
        venv, distribution = self.home / 'qa-env', self.home / 'python-dist'
        (venv / 'bin').mkdir(parents=True)
        (distribution / 'bin').mkdir(parents=True)
        python = distribution / 'bin/python3'
        python.write_text('#!/bin/sh\nexit 0\n')
        python.chmod(0o700)
        (venv / 'bin/python').symlink_to(python)
        library = distribution / 'lib/encodings-fixture.py'
        library.parent.mkdir()
        library.write_text('PYTHON_LIBRARY_FIXTURE')
        outside = self.home / 'unrelated-distribution.txt'
        outside.write_text('UNRELATED_HOST_FIXTURE')
        env['VIRTUAL_ENV'] = str(venv)
        directory = self.store.task_dir(rid, 'qa') / 'tool-checks' / ('d' * 32)
        policy = project_tool_policy(self.store, self.store.read(rid), directory, str(wt), 'opencode', 'qa', env)
        self.addCleanup(cleanup_tool_scratch, self.store, policy)
        allowed = subprocess.run([policy['sandbox'], '-p', policy['profile'], '/bin/cat', str(library)],
                                 cwd=wt, env=env, capture_output=True, text=True, timeout=10)
        denied = subprocess.run([policy['sandbox'], '-p', policy['profile'], '/bin/cat', str(outside)],
                                cwd=wt, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual((allowed.returncode, allowed.stdout), (0, 'PYTHON_LIBRARY_FIXTURE'), allowed.stderr)
        self.assertNotEqual(denied.returncode, 0)
        self.assertNotIn('UNRELATED_HOST_FIXTURE', denied.stdout)

    def test_frozen_qa_command_interpreter_opens_its_venv_and_distribution_without_environment_variables(self):
        from oturum import cleanup_tool_scratch, project_tool_policy
        repo, wt, parent, rid, env = self.project_tool_fixture()
        venv, distribution = self.home / 'qa-env', self.home / 'uv-dist'
        (venv / 'bin').mkdir(parents=True)
        (distribution / 'bin').mkdir(parents=True)
        python = distribution / 'bin/python3.13'
        python.write_text('#!/bin/sh\nexit 0\n')
        python.chmod(0o700)
        (venv / 'bin/python').symlink_to(python)
        (venv / 'pyvenv.cfg').write_text(f'home = {distribution / "bin"}\n')
        library = distribution / 'lib/encodings-fixture.py'
        library.parent.mkdir()
        library.write_text('PYTHON_LIBRARY_FIXTURE')
        site = venv / 'lib/site-fixture.py'
        site.parent.mkdir()
        site.write_text('VENV_LIBRARY_FIXTURE')
        outside = self.home / 'unrelated-venvpath'
        outside.mkdir()
        (outside / 'secret.txt').write_text('UNRELATED_HOST_FIXTURE')
        for key in ('VIRTUAL_ENV', 'CADRE_UI_PYTHON', 'CADRE_TEST_ARAYUZ_PYTHON', 'CADRE_TEST_PYTEST_PYTHON'):
            env.pop(key, None)
        project = self.home / 'projects' / self.store.read(rid)['project']
        project.mkdir(parents=True, exist_ok=True)
        (project / 'qa-komut').write_text(f'{venv}/bin/python -m ruff check tests && {venv}/bin/python -m basedpyright --venvpath {outside} tests\n')
        directory = self.store.task_dir(rid, 'qa') / 'tool-checks' / ('e' * 32)
        policy = project_tool_policy(self.store, self.store.read(rid), directory, str(wt), 'opencode', 'qa', env)
        self.addCleanup(cleanup_tool_scratch, self.store, policy)
        def cat(path):
            return subprocess.run([policy['sandbox'], '-p', policy['profile'], '/bin/cat', str(path)],
                                  cwd=wt, env=env, capture_output=True, text=True, timeout=10)
        allowed, venv_read, denied = cat(library), cat(site), cat(outside / 'secret.txt')
        self.assertEqual((allowed.returncode, allowed.stdout), (0, 'PYTHON_LIBRARY_FIXTURE'), allowed.stderr)
        self.assertEqual((venv_read.returncode, venv_read.stdout), (0, 'VENV_LIBRARY_FIXTURE'), venv_read.stderr)
        self.assertNotEqual(denied.returncode, 0, 'python olmayan mutlak yol kokleri acilmaz')
        self.assertNotIn('UNRELATED_HOST_FIXTURE', denied.stdout)
        (project / 'qa-komut').write_text('pytest -q tests\n')
        directory = self.store.task_dir(rid, 'qa') / 'tool-checks' / ('f' * 32)
        policy = project_tool_policy(self.store, self.store.read(rid), directory, str(wt), 'opencode', 'qa', env)
        self.addCleanup(cleanup_tool_scratch, self.store, policy)
        self.assertNotEqual(cat(library).returncode, 0, 'QA komutu yorumlayici gostermiyorsa dagitim acilmaz')

    def test_project_tool_bootstraps_its_python_venv_without_opening_the_prefix_or_changing_files(self):
        """N-ENV02/N-QA01: the engine's own Python startup is allowed; neighbour files and install writes are not."""
        from oturum import cleanup_tool_scratch, project_tool_policy
        repo, wt, parent, rid, env = self.project_tool_fixture()
        prefix = self.home / 'fixture-python'
        (prefix / 'bin').mkdir(parents=True)
        python = prefix / 'bin/python'
        python.symlink_to(Path(sys._base_executable).resolve())
        config = prefix / 'pyvenv.cfg'
        original = f'home = {Path(sys._base_executable).resolve().parent}\ninclude-system-site-packages = false\n'
        config.write_text(original)
        neighbour = prefix / 'unrelated.txt'
        neighbour.write_text('outside project')
        for key in ('VIRTUAL_ENV', 'CADRE_UI_PYTHON', 'CADRE_TEST_ARAYUZ_PYTHON', 'CADRE_TEST_PYTEST_PYTHON'):
            env.pop(key, None)
        directory = self.store.task_dir(rid, 'qa') / 'tool-checks' / ('a' * 32)
        with patch.object(sys, 'prefix', str(prefix)), patch.object(sys, 'executable', str(python)):
            policy = project_tool_policy(self.store, self.store.read(rid), directory, str(wt), 'opencode', 'qa', env)
        self.addCleanup(cleanup_tool_scratch, self.store, policy)
        body = ("from pathlib import Path\n"
                f"assert Path({str(config)!r}).read_text() == {original!r}\n"
                f"for operation in (lambda:Path({str(neighbour)!r}).read_text(), lambda:Path({str(config)!r}).write_text('changed')):\n"
                " try:operation()\n"
                " except PermissionError:pass\n"
                " else:raise AssertionError('Python bootstrap opened unrelated read/write access')\n"
                "print('PYTHON_BOOTSTRAP_PASS')\n")
        result = subprocess.run([policy['sandbox'], '-p', policy['profile'], str(python), '-c', body],
                                cwd=wt, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('PYTHON_BOOTSTRAP_PASS', result.stdout)
        self.assertEqual(config.read_text(), original)
        self.assertEqual(neighbour.read_text(), 'outside project')
        config.unlink()
        config.symlink_to(neighbour)
        with patch.object(sys, 'prefix', str(prefix)), patch.object(sys, 'executable', str(python)):
            with self.assertRaisesRegex(PermissionError, 'project-tool-python-config-untrusted'):
                project_tool_policy(self.store, self.store.read(rid), directory, str(wt), 'opencode', 'qa', env)

    def test_kernel_scope_preflight_failure_starts_no_executor_and_keeps_launch_not_started(self):
        from oturum import Attempt
        repo, wt, parent, rid, env = self.project_tool_fixture()
        marker = wt / 'never-started'
        command = [sys.executable, '-c', f"from pathlib import Path;Path({str(marker)!r}).touch()"]
        run = subprocess.run
        def deny_preflight(args, *positional, **kwargs):
            if 'scope-check' in args:
                raise subprocess.CalledProcessError(75, args, stderr=b'unsupported kernel scope')
            return run(args, *positional, **kwargs)
        with patch.dict(os.environ, env), patch('oturum.subprocess.run', side_effect=deny_preflight):
            attempt = Attempt('opencode', 'impl', 'fixture/worker', 'work')
            with self.assertRaisesRegex(PermissionError, 'project-tool-sandbox-unavailable'):
                attempt.launch(command, str(wt), self.home / 'preflight.log', env, ())
        self.assertFalse(marker.exists())
        self.assertEqual(attempt.data['outcome'], 'not_started')
        self.assertFalse((attempt.directory / 'process.json').exists())
        self.assertEqual(list((self.home / 'tool-tmp').iterdir()), [])
        self.assertEqual(self.store.read(rid)['tool_boundary_wait'], 'project-tool-sandbox-unavailable')
        self.assertEqual((repo / 'app.py').read_text(), 'value = 1\n')

    def test_unobserved_tool_primary_death_requires_the_composed_hook_binding_before_complete_teardown(self):
        import kosu
        from oturum import recover_tool_artifacts
        observer, info, path, rid, env, repo, ready = self.detached_scope_observer('worker')
        arm = path.with_name('tools-armed.json')
        self.assertTrue(arm.is_file())
        original = arm.read_bytes()
        receipt = json.loads(path.with_name('process.json').read_text())
        self.assertNotIn('stop_observed', receipt)
        observer.kill()
        observer.wait(timeout=5)
        os.kill(receipt['process_identity']['pid'], signal.SIGKILL)
        self.store.update(rid, state='cancelled')
        lease = kosu.FileLock(self.store.path(rid) / '.owner.lock')
        deadline = time.monotonic() + 5
        while not lease.acquire(blocking=False) and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertIsNotNone(lease.fd)
        lease.close()
        invalid = json.loads(original)
        invalid['pid'] = os.getpid()
        arm.chmod(0o600)
        arm.write_text(json.dumps(invalid))
        arm.chmod(0o400)
        self.assertTrue(recover_tool_artifacts(self.store, rid, reclaim=True)['unknown'])
        self.assertIsNotNone(kosu.process_identity(info['pid']))
        self.assertTrue(Path(info['scratch']).exists())
        arm.chmod(0o600)
        arm.write_bytes(original)
        arm.chmod(0o400)
        deadline = time.monotonic() + 5
        with patch.dict(os.environ, env), patch('kosu._baslat', side_effect=AssertionError('terminal restart')):
            while Path(info['scratch']).exists() and time.monotonic() < deadline:
                self.assertEqual(kosu.revive(self.store, LIB.parent), [])
                time.sleep(.01)
        self.assertIsNone(kosu.process_identity(info['pid']))
        self.assertFalse(Path(info['scratch']).exists())
        self.assertEqual((path.parent / 'loader').stat().st_mode & 0o777, 0o700)
        self.assertTrue((path.parent / 'tool-retired.json').exists())
        self.assertIn(b'ses_detached_scope', path.with_name('stdout.jsonl').read_bytes())
        self.assertEqual(self.store.read(rid)['tasks']['g1']['attempt_count'], 1)
        self.assertEqual((repo / 'app.py').read_text(), 'value = 1\n')

    def recovery_observer(self, kind):
        import kosu
        repo, wt, parent, rid, env = self.project_tool_fixture()
        self.store.update(rid, proje_ust=None)
        env.update(PYTHONPATH=str(LIB), PYTHONDONTWRITEBYTECODE='1', CADRE_KILL_GRACE='.1',
                   OPENCODE_DB=str(self.home / 'absent.db'))
        ready = wt / 'recovery-ready.json'
        executor = wt / 'recovery-executor.py'
        worker = kind.startswith('worker')
        executor.write_text("import json,os,time,subprocess,sys\nfrom pathlib import Path\n"
            + ("scratch=Path(json.loads(os.environ['CADRE_TOOL_POLICY'])['scratch'])\n" if worker else "scratch=Path(os.environ['TMPDIR'])\n")
            + "(scratch/'temporary').write_text('temporary')\n"
            + f"(scratch/'outside-link').symlink_to({str(repo / 'app.py')!r})\n"
            + ("child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],close_fds=True)\n" if kind == 'worker-descendant' else "child=None\n")
            + f"Path({str(ready)!r}).write_text(json.dumps({{'pid':os.getpid(),'scratch':str(scratch),'descendant':child.pid if child else None}}))\n"
            + ("print(json.dumps({'sessionID':'ses_recovery','type':'text','part':{'type':'text','text':'retained'}}),flush=True)\n" if worker else '')
            + "time.sleep(60)\n")
        observer = self.home / 'recovery-observer.py'
        observer.write_text("import os,sys,time,json\nfrom pathlib import Path\n"
            "from kosu import RunStore,FileLock,process_identity\nfrom oturum import Attempt\nimport qe_core as core\n"
            "store=RunStore();rid=os.environ['CADRE_RUN_ID'];core.QE=store.home\n"
            "owner=FileLock(store.path(rid)/'.owner.lock');owner.acquire()\n"
            "store.update(rid,state='running',owner_pid=os.getpid(),owner_identity=process_identity(os.getpid()))\n"
            + (f"attempt=Attempt('opencode','impl','fixture/worker','retained work')\nprocess=attempt.launch([sys.executable,{str(executor)!r}],{str(wt)!r},store.home/'recovery.log',dict(os.environ),(owner.fd,))\nwhile process.poll() is None: time.sleep(.01)\n" if worker else
               f"with (store.home/'recovery.log').open('w') as output: core.qa_sureci({shlex.join([sys.executable,str(executor)])!r},Path({str(wt)!r}),output,60)\n"))
        with (self.home / 'observer.log').open('w') as output:
            process = subprocess.Popen([sys.executable, str(observer)], env=env, stdout=output, stderr=output)
        def stop_observer():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        self.addCleanup(stop_observer)
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(ready.exists(), (self.home / 'observer.log').read_text())
        info = json.loads(ready.read_text())
        for pid in (info['pid'], info['descendant']):
            if pid is None:
                continue
            identity = kosu.process_identity(pid)
            def stop_child(pid=pid, identity=identity):
                if identity and kosu.process_identity(pid) == identity:
                    core.kill_tree(pid, grace=.1)
            self.addCleanup(stop_child)
        pattern = 'tasks/g1/attempts/*/attempt.json' if worker else 'tasks/qa/tool-checks/*/check.json'
        paths = list(self.store.path(rid).glob(pattern))
        self.assertEqual(len(paths), 1, 'owned check/attempt registration missing')
        directory = paths[0].parent
        self.assertTrue((directory / 'process.json').is_file())
        return process, info, directory, rid, env, repo, wt

    def test_sigkill_worker_owner_recovery_keeps_evidence_and_never_revives_terminal_run(self):
        import kosu
        observer, info, directory, rid, env, repo, wt = self.recovery_observer('worker')
        before = (directory / 'stdout.jsonl').read_bytes()
        self.assertTrue(before)
        observer.kill()
        observer.wait(timeout=5)
        self.store.update(rid, state='cancelled')
        unrelated = self.home / 'tool-tmp' / core.uuid.uuid4().hex
        unrelated.mkdir()
        (unrelated / 'preserved').touch()
        with patch.dict(os.environ, env), patch('kosu._baslat', side_effect=AssertionError('terminal restart')):
            self.assertEqual(kosu.revive(self.store, LIB.parent), [])
            self.assertEqual(kosu.revive(self.store, LIB.parent), [])
        self.assertFalse(Path(info['scratch']).exists())
        self.assertEqual((directory / 'loader').stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory / 'stdout.jsonl').read_bytes(), before)
        self.assertTrue((directory / 'tool-retired.json').is_file())
        self.assertTrue((unrelated / 'preserved').is_file())
        self.assertEqual((repo / 'app.py').read_text(), 'value = 1\n')
        self.assertEqual(self.store.read(rid)['state'], 'cancelled')
        self.assertEqual(self.store.read(rid)['tasks']['g1']['attempt_count'], 1)

    def test_sigkill_qa_owner_recovery_reclaims_the_check_and_preserves_receipts(self):
        import kosu
        observer, info, directory, rid, env, repo, wt = self.recovery_observer('qa')
        check = (directory / 'check.json').read_bytes()
        observer.kill()
        observer.wait(timeout=5)
        self.store.update(rid, state='cancelled')
        with patch.dict(os.environ, env), patch('kosu._baslat', side_effect=AssertionError('terminal restart')):
            self.assertEqual(kosu.revive(self.store, LIB.parent), [])
        self.assertFalse(Path(info['scratch']).exists())
        self.assertEqual((directory / 'check.json').read_bytes(), check)
        self.assertTrue((directory / 'process.json').is_file())
        self.assertTrue((directory / 'tool-retired.json').is_file())
        self.assertEqual((repo / 'app.py').read_text(), 'value = 1\n')

    def test_sigkill_retry_preserves_unobserved_artifacts_and_recovers_only_exact_session(self):
        from oturum import Attempt
        observer, info, directory, rid, env, repo, wt = self.recovery_observer('worker')
        observer.kill()
        observer.wait(timeout=5)
        core.kill_tree(info['pid'], grace=.1)
        with patch.dict(os.environ, {**env, 'CADRE_KAPSAM_BEKLE_SN':'.1'}):
            attempt = Attempt('opencode', 'impl', 'fixture/worker', 'retained work')
            self.addCleanup(attempt.lease.close)
            self.assertTrue(attempt.resuming)
            self.assertEqual(attempt.data['session_id'], 'ses_recovery')
            self.assertEqual(json.loads((directory / 'attempt.json').read_text())['session_id'], 'ses_recovery')
            self.assertTrue(Path(info['scratch']).exists())
            self.assertEqual((directory / 'loader').stat().st_mode & 0o777, 0o500)
            self.assertTrue((directory / 'stdout.jsonl').is_file())
            self.assertEqual(self.store.read(rid)['tasks']['g1']['attempt_count'], 2)
            self.assertEqual(self.store.read(rid)['tool_boundary_wait'], 'project-tool-recovery-untrusted')

    def test_recovery_preserves_live_owner_and_lease_free_live_descendant_group(self):
        import kosu
        from oturum import recover_tool_artifacts
        observer, info, directory, rid, env, repo, wt = self.recovery_observer('worker-descendant')
        with patch.dict(os.environ, env):
            kosu.reclaim_orphans(self.store, self.store.read(rid))
            self.assertIsNone(observer.poll())
            self.assertTrue(Path(info['scratch']).exists())
            observer.kill()
            observer.wait(timeout=5)
            os.kill(info['pid'], signal.SIGKILL)
            deadline = time.monotonic() + 5
            while kosu.process_identity(info['pid']) is not None and time.monotonic() < deadline:
                time.sleep(.01)
            for lock_path in (self.store.path(rid) / '.owner.lock', directory.parents[1] / '.opencode-impl.lock'):
                lock = kosu.FileLock(lock_path)
                deadline = time.monotonic() + 5
                while not lock.acquire(blocking=False) and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertIsNotNone(lock.fd, 'dead primary retained its lease')
                lock.close()
            report = recover_tool_artifacts(self.store, rid)
            self.assertTrue(report['active'], report)
            self.assertFalse(report['unknown'], report)
            self.assertTrue(Path(info['scratch']).exists())
            self.assertEqual((directory / 'loader').stat().st_mode & 0o777, 0o500)
            core.kill_tree(info['descendant'], grace=.1)
            report = recover_tool_artifacts(self.store, rid)
            self.assertFalse(report['active'])
            self.assertTrue(report['unknown'])
            self.assertTrue(Path(info['scratch']).exists())

    def test_recovery_rejects_tampered_scratch_binding_and_loader_symlink_without_deleting(self):
        import kosu
        from oturum import recover_tool_artifacts
        observer, info, directory, rid, env, repo, wt = self.recovery_observer('worker')
        observer.kill()
        observer.wait(timeout=5)
        with patch.dict(os.environ, env), patch('oturum.recover_tool_artifacts'):
            kosu.reclaim_orphans(self.store, self.store.read(rid))
        self.assertEqual(json.loads((directory / 'process.json').read_text())['stop_observed']['pid'], info['pid'])
        policy_path = directory / 'tool-policy.json'
        before = policy_path.read_bytes()
        policy = json.loads(before)
        external = self.home / 'tool-tmp' / core.uuid.uuid4().hex
        external.mkdir()
        (external / 'preserved').touch()
        identity = external.stat()
        policy.update(scratch=str(external), scratch_identity=[identity.st_dev, identity.st_ino])
        policy_path.write_text(json.dumps(policy))
        report = recover_tool_artifacts(self.store, rid)
        self.assertTrue(report['unknown'], report)
        self.assertTrue((external / 'preserved').exists())
        self.assertTrue(Path(info['scratch']).exists())
        self.assertEqual((directory / 'loader').stat().st_mode & 0o777, 0o500)
        policy_path.write_bytes(before)
        saved_policy = directory / 'policy-saved.json'
        policy_path.rename(saved_policy)
        try:
            self.assertTrue(recover_tool_artifacts(self.store, rid)['unknown'])
            self.assertTrue(Path(info['scratch']).exists())
        finally:
            saved_policy.rename(policy_path)
        loader = directory / 'loader'
        saved = directory / 'loader-saved'
        loader.rename(saved)
        loader.symlink_to(external, target_is_directory=True)
        try:
            self.assertTrue(recover_tool_artifacts(self.store, rid)['unknown'])
            self.assertTrue(Path(info['scratch']).exists())
            self.assertTrue((external / 'preserved').exists())
        finally:
            loader.unlink()
            saved.rename(loader)
        self.assertFalse(recover_tool_artifacts(self.store, rid)['unknown'])
        self.assertFalse(Path(info['scratch']).exists())

    def test_recovery_does_not_kill_a_reused_pid_or_retire_a_live_unrelated_group(self):
        import kosu
        from oturum import recover_tool_artifacts
        observer, info, directory, rid, env, repo, wt = self.recovery_observer('worker')
        observer.kill()
        observer.wait(timeout=5)
        with patch.dict(os.environ, env), patch('oturum.recover_tool_artifacts'):
            kosu.reclaim_orphans(self.store, self.store.read(rid))
        path = directory / 'process.json'
        before = path.read_bytes()
        sleeper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        try:
            identity = kosu.process_identity(sleeper.pid)
            self.assertIsNotNone(identity)
            receipt = json.loads(before)
            receipt['process_identity'] = {**identity, 'started': 'different incarnation'}
            path.write_text(json.dumps(receipt))
            with patch.dict(os.environ, env):
                kosu.reclaim_orphans(self.store, self.store.read(rid))
            self.assertIsNone(sleeper.poll())
            self.assertTrue(Path(info['scratch']).exists())
            self.assertTrue(recover_tool_artifacts(self.store, rid)['active'])
            path.write_bytes(before)
            self.assertFalse(recover_tool_artifacts(self.store, rid)['active'])
            self.assertFalse(Path(info['scratch']).exists())
            self.assertIsNone(sleeper.poll())
        finally:
            if sleeper.poll() is None:
                sleeper.kill()
            sleeper.wait(timeout=5)

    def test_sigkill_during_loader_preparation_retires_only_recorded_roots_without_starting_executor(self):
        import kosu
        repo, wt, parent, rid, env = self.project_tool_fixture()
        self.store.update(rid, proje_ust=None)
        env.update(PYTHONPATH=str(LIB), PYTHONDONTWRITEBYTECODE='1', OPENCODE_DB=str(self.home / 'absent.db'))
        marker = wt / 'never-started'
        prepared = self.home / 'prepared.json'
        observer = self.home / 'prepare-observer.py'
        command = f"from pathlib import Path; Path({str(marker)!r}).touch()"
        observer.write_text("import json,os,signal,sys\nfrom pathlib import Path\nimport oturum\nfrom oturum import Attempt\n"
            "write=oturum.atomic_json\n"
            "def crash(path,value):\n"
            " write(path,value)\n"
            " if Path(path).name=='loader-policy.json':\n"
            f"  Path({str(prepared)!r}).write_text(json.dumps({{'directory':str(Path(path).parent)}}))\n"
            "  os.kill(os.getpid(),signal.SIGKILL)\n"
            "oturum.atomic_json=crash\n"
            "attempt=Attempt('opencode','impl','fixture/worker','work')\n"
            f"attempt.launch([sys.executable,'-c',{command!r}],{str(wt)!r},Path({str(self.home / 'prepare.log')!r}),dict(os.environ),())\n")
        result = subprocess.run([sys.executable, str(observer)], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
        directory = Path(json.loads(prepared.read_text())['directory'])
        policy = json.loads((directory / 'tool-policy.json').read_text())
        self.assertTrue(Path(policy['scratch']).is_dir())
        self.assertFalse(marker.exists())
        self.assertFalse((directory / 'process.json').exists())
        kosu.reclaim_orphans(self.store, self.store.read(rid))
        self.assertFalse(Path(policy['scratch']).exists())
        self.assertEqual((directory / 'loader').stat().st_mode & 0o777, 0o700)
        self.assertTrue((directory / 'attempt.json').exists())
        self.assertFalse(marker.exists())

    def test_qa_process_receipt_failure_starts_no_generated_command_and_releases_check_lease(self):
        import kosu
        repo, wt, parent, rid, env = self.project_tool_fixture()
        self.store.update(rid, proje_ust=None)
        marker = wt / 'unapproved-check'
        command = shlex.join([sys.executable, '-c', f"from pathlib import Path; Path({str(marker)!r}).touch()"])
        popen = core.subprocess.Popen
        sealed = []
        def deny_receipt(args, *positional, **kwargs):
            if 'tool-exec' in args:
                path = Path(args[args.index('tool-exec') + 2]).parent
                path.chmod(0o500)
                sealed.append(path)
            return popen(args, *positional, **kwargs)
        try:
            with patch.dict(os.environ, env), patch.object(core, 'QE', self.home), patch.object(core.subprocess, 'Popen', side_effect=deny_receipt):
                with self.assertRaises(PermissionError):
                    core._taban_komut(wt, command, self.home / 'check.log', 5)
            self.assertTrue(sealed)
            self.assertFalse(marker.exists())
            self.assertEqual(list((self.home / 'tool-tmp').iterdir()), [])
            self.assertEqual(kosu.active_runs(self.store), [])
        finally:
            for path in sealed:
                path.chmod(0o700)
        with patch.dict(os.environ, env), patch.object(core, 'QE', self.home):
            self.assertEqual(core._taban_komut(wt, command, self.home / 'check-retry.log', 5), 0)
        self.assertTrue(marker.exists())

    def test_finished_qa_with_a_live_background_group_waits_without_removing_its_scratch(self):
        import kosu
        from oturum import recover_tool_artifacts
        repo, wt, parent, rid, env = self.project_tool_fixture()
        self.store.update(rid, proje_ust=None)
        marker = wt / 'background.json'
        script = wt / 'background.py'
        script.write_text("import json,os,time\nfrom pathlib import Path\n"
            "scratch=Path(os.environ['TMPDIR'])\n"
            "pid=os.fork()\n"
            "if pid:\n"
            f" Path({str(marker)!r}).write_text(json.dumps({{'pid':pid,'scratch':str(scratch)}}))\n"
            " os._exit(0)\n"
            "(scratch/'retained').touch()\n"
            "time.sleep(60)\n")
        info = None
        try:
            with patch.dict(os.environ, env), patch.object(core, 'QE', self.home):
                with self.assertRaisesRegex(PermissionError, 'project-tool-process-group-active'):
                    core._taban_komut(wt, shlex.join([sys.executable, str(script)]), self.home / 'background.log', 5)
            info = json.loads(marker.read_text())
            self.assertTrue(Path(info['scratch']).is_dir())
            self.assertEqual(self.store.read(rid)['tool_boundary_wait'], 'project-tool-process-group-active')
            self.assertTrue(recover_tool_artifacts(self.store, rid)['active'])
            record = next((self.store.path(rid) / 'tasks/qa/tool-checks').glob('*/process.json'))
            receipt = json.loads(record.read_text())
            receipt.pop('stop_observed', None)
            receipt.pop('scope_digest', None)
            record.write_text(json.dumps(receipt))
            policy_path = record.with_name('tool-policy.json')
            policy = json.loads(policy_path.read_text())
            policy.pop('process_scope', None)
            policy_path.write_text(json.dumps(policy))
            core.kill_tree(info['pid'], grace=.1)
            report = recover_tool_artifacts(self.store, rid)
            self.assertFalse(report['active'])
            self.assertTrue(report['unknown'])
            self.assertTrue(Path(info['scratch']).exists())
            self.assertEqual(self.store.read(rid)['tool_boundary_wait'], 'project-tool-recovery-untrusted')
        finally:
            if info is None and marker.exists():
                info = json.loads(marker.read_text())
            if info and kosu.process_identity(info['pid']) is not None:
                core.kill_tree(info['pid'], grace=.1)

    def test_loader_uses_sealed_private_config_without_installing_or_reading_ambient_plugins(self):
        from oturum import Attempt
        repo, wt, parent, rid, env = self.project_tool_fixture()
        self.store.update(rid, proje_ust=None)
        home = Path(env['HOME'])
        ambient = home / '.config/opencode'
        ambient.mkdir(parents=True)
        (ambient / 'opencode.json').write_text('{"plugin":["cadre-never-install@1.0.0"]}')
        (home / '.opencode').mkdir()
        marker = self.home / 'installer-would-start'
        env.update(XDG_CONFIG_HOME=str(home / '.config'), OPENCODE_CONFIG_DIR=str(ambient),
                   OPENCODE_CONFIG=str(ambient / 'opencode.json'), OPENCODE_TEST_HOME=str(home),
                   NODE_OPTIONS='--require=/never-load.js', BUN_OPTIONS='--preload=/never-load.js')
        code = ("import os,json,sys\nfrom pathlib import Path\n"
                "p=Path(os.environ['XDG_CONFIG_HOME'])/'opencode'\n"
                "h=Path(os.environ.get('OPENCODE_TEST_HOME',os.environ['HOME']))\n"
                "writable=os.access(p,os.W_OK) or (h/'.opencode').exists()\n"
                f"if writable: Path({str(marker)!r}).touch()\n"
                "denied=False\n"
                "try: (p/'node_modules').mkdir()\n"
                "except PermissionError: denied=True\n"
                "print(json.dumps({'sessionID':'ses_loader','writable':writable,'denied':denied,'config':str(p),"
                "'home':str(h),'config_value':json.loads(Path(os.environ['OPENCODE_CONFIG']).read_text()),"
                "'plugin':json.loads(os.environ.get('OPENCODE_CONFIG_CONTENT','{}')).get('plugin'),"
                "'preloads':any(k in os.environ for k in ('NODE_OPTIONS','BUN_OPTIONS'))}))\n")
        roots = []
        before = (ambient / 'opencode.json').read_bytes()
        with patch.dict(os.environ, env):
            for _ in range(2):
                attempt = Attempt('opencode', 'impl', 'fixture/worker', 'work')
                process = attempt.launch([sys.executable, '-c', code], str(wt), self.home / 'loader.log', env, ())
                self.assertEqual(process.wait(timeout=5), 0)
                output = json.loads(process.output())
                self.assertFalse(output['writable'])
                self.assertTrue(output['denied'])
                self.assertFalse(output['preloads'])
                self.assertEqual(output['config_value'], {'$schema': 'https://opencode.ai/config.json'})
                self.assertEqual(output['plugin'], [(LIB / 'cadre-plugin.mjs').as_uri()])
                receipt = json.loads((attempt.directory / 'loader-policy.json').read_text())
                roots.append(receipt['root'])
                self.assertEqual(output['config'], receipt['config'])
                self.assertEqual(output['home'], receipt['home'])
                self.assertTrue(Path(receipt['root']).is_relative_to(attempt.directory))
                self.assertTrue(os.access(receipt['root'], os.W_OK))
                self.assertFalse((Path(receipt['config']) / 'node_modules').exists())
        self.assertEqual(len(set(roots)), 2)
        self.assertFalse(marker.exists())
        self.assertEqual((ambient / 'opencode.json').read_bytes(), before)

    def test_loader_rejects_foreign_plugins_and_invalid_config_before_executor_start(self):
        from oturum import Attempt
        repo, wt, parent, rid, env = self.project_tool_fixture()
        self.store.update(rid, proje_ust=None)
        marker = self.home / 'unapproved-plugin-executor'
        for value, reason in [('{bad JSON', 'project-tool-loader-config-invalid'),
                              ('[]', 'project-tool-loader-config-invalid'),
                              (json.dumps({'mcp':{'unapproved':{'type':'local','command':['never-start']}}}), 'project-tool-loader-external-service-unapproved'),
                              (json.dumps({'plugin':['cadre-never-install@1.0.0']}), 'project-tool-loader-plugin-unapproved'),
                              (json.dumps({'plugin':[(self.home / 'foreign.mjs').as_uri()]}), 'project-tool-loader-plugin-unapproved')]:
            with self.subTest(config=value), patch.dict(os.environ, {**env, 'OPENCODE_CONFIG_CONTENT':value}):
                attempt = Attempt('opencode', 'impl', 'fixture/worker', 'work')
                process = None
                try:
                    try:
                        process = attempt.launch([sys.executable, '-c', f"from pathlib import Path; import json; Path({str(marker)!r}).touch(); print(json.dumps({{'sessionID':'ses_unapproved'}}))"], str(wt), self.home / 'loader.log', {**env, 'OPENCODE_CONFIG_CONTENT':value}, ())
                    except PermissionError as error:
                        self.assertEqual(str(error), reason)
                    else:
                        process.wait(timeout=5)
                        self.fail('unapproved loader executor was started')
                finally:
                    if process is not None and process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                self.assertFalse(marker.exists())
                self.assertEqual(self.store.read(rid)['tool_boundary_wait'], reason)
                self.assertEqual(json.loads(attempt.path.read_text())['outcome'], 'not_started')

    def test_loader_sealing_and_receipt_failures_start_no_executor_and_release_owned_permissions(self):
        from oturum import Attempt
        repo, wt, parent, rid, env = self.project_tool_fixture()
        self.store.update(rid, proje_ust=None)
        marker = self.home / 'unsealed-loader-executor'
        chmod = Path.chmod
        access = os.access
        write = core.atomic_json
        def seal_error(path, mode, *args, **kwargs):
            if mode == 0o500:
                raise PermissionError('fixture seal failure')
            return chmod(path, mode, *args, **kwargs)
        def receipt_error(path, value):
            if Path(path).name == 'loader-policy.json':
                raise OSError('fixture disk full')
            return write(path, value)
        def writable(path, mode, *args, **kwargs):
            if isinstance(path, (str, Path)) and 'loader' in Path(path).parts:
                return True
            return access(path, mode, *args, **kwargs)
        for fault in (patch.object(Path, 'chmod', seal_error), patch('oturum.atomic_json', side_effect=receipt_error), patch('oturum.os.access', side_effect=writable)):
            with fault, patch.dict(os.environ, env):
                attempt = Attempt('opencode', 'impl', 'fixture/worker', 'work')
                process = None
                try:
                    with self.assertRaises(OSError):
                        process = attempt.launch([sys.executable, '-c', f"from pathlib import Path; Path({str(marker)!r}).touch()"], str(wt), self.home / 'loader.log', env, ())
                finally:
                    if process is not None:
                        process.proc.wait(timeout=5)
                self.assertFalse(marker.exists())
                self.assertTrue(os.access(attempt.directory / 'loader', os.W_OK))
                self.assertEqual(json.loads(attempt.path.read_text())['outcome'], 'not_started')
        self.assertEqual(list((self.home / 'tool-tmp').iterdir()), [])


if __name__ == "__main__":
    unittest.main()
