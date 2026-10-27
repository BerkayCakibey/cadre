from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from typing import cast
import unittest
from unittest.mock import patch

import test_kosu_sessions as fixtures
import qe_core as core
import motor
import sinirlar
from kosu import RunStore, process_identity
from test_gece_omur import IsolatedNight


class MotorSureTests(unittest.TestCase):
    def fixture(self):
        fixture = fixtures.SessionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_duration_defaults_overrides_and_invalid_values(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(sinirlar.motor_sureleri(), (2700, 5400))
        for warning, limit, expected in [("0", "3", (0, 3)), ("9", "3", (3, 3))]:
            with patch.dict(
                os.environ, {"CADRE_WARN_SEC": warning, "CADRE_ABORT_SEC": limit}
            ):
                self.assertEqual(sinirlar.motor_sureleri(), expected)
        for warning, limit in [("1", "0"), ("-1", "3"), ("nan", "3"), ("1", "inf")]:
            with patch.dict(
                os.environ, {"CADRE_WARN_SEC": warning, "CADRE_ABORT_SEC": limit}
            ):
                with self.assertRaises(ValueError):
                    sinirlar.motor_sureleri()
        with tempfile.TemporaryDirectory(prefix="duration-help-") as directory:
            env = {
                **os.environ,
                "CADRE_HOME": directory,
                "CADRE_ENGINE_ROOT": str(fixtures.LIB.parent),
                "CADRE_WARN_SEC": "1",
                "CADRE_ABORT_SEC": "3",
                "GIT_OPTIONAL_LOCKS": "0",
            }
            result = subprocess.run(
                ["bash", str(fixtures.LIB.parent / "bin/cadre.sh"), "--help"],
                env=env,
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("uyari 1 sn", result.stdout)
            self.assertIn("kesin son 3 sn", result.stdout)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_clock_survives_restart_never_grows_and_rejects_corruption(self):
        with tempfile.TemporaryDirectory(prefix="duration-record-") as directory:
            store = RunStore(Path(directory))
            rid = store.accept("duration", "bounded task")["run_id"]
            with patch.object(core.time, "time", return_value=100.0):
                _, _, first = motor.sure_kaydi(store, rid, "main", 10, 20)
            with patch.object(core.time, "time", return_value=110.0):
                _, deadline, resumed = motor.sure_kaydi(store, rid, "main", 90, 100)
            self.assertEqual(first, resumed)
            self.assertLessEqual(deadline - time.monotonic(), 10)
            with patch.object(core.time, "time", return_value=121.0):
                _, deadline, _ = motor.sure_kaydi(store, rid, "main", 90, 100)
            self.assertLessEqual(deadline, time.monotonic())
            raw = dict(
                submission_id="short-duration",
                project="short",
                target_path=str(Path(directory) / "new-target"),
                target_kind="new",
                input_mode="plan_only",
                prompt="Plan only.",
                output="Reviewed plan",
                acceptance="Source preserved",
                preference="balanced",
                difficulty_override=None,
                duration_minutes="1",
                budget_usd="0",
                backlog_path="",
                source_token="",
            )
            with patch.object(core.time, "time", return_value=100.0):
                limited = store.request(raw)["run_id"]
            with patch.object(core.time, "time", return_value=160.0):
                _, deadline, _ = motor.sure_kaydi(store, limited, "child", 10, 999)
            self.assertGreater(deadline, time.monotonic() + 30, "calisma baslamadan gecen bekleme sureden dusmez")
            from kosu import atomic_json
            record = store.read(limited)
            record["request_clock"] = {"baslangic": 100.0, "aktif_sn": 60.0, "oturum": None}
            atomic_json(store.path(limited) / "run.json", record)
            with patch.object(core.time, "time", return_value=5000.0):
                _, deadline, _ = motor.sure_kaydi(store, limited, "child-2", 10, 999)
            self.assertLessEqual(deadline, time.monotonic())
            for invalid in [
                None,
                {"started": float("nan")},
                {"started": time.time() + 60, "warning_seconds": 1, "limit_seconds": 2},
            ]:
                store.task_update(rid, "main", motor_clock=invalid)
                before = store.path(rid).joinpath("run.json").read_bytes()
                with self.assertRaises(ValueError):
                    motor.sure_kaydi(store, rid, "main", 1, 2)
                self.assertEqual(
                    store.path(rid).joinpath("run.json").read_bytes(), before
                )

    def test_productive_motor_warns_then_stops_its_tree_at_the_persistent_limit(self):
        fixture = self.fixture()
        fixture.fake.write_text(
            fixture.fake.read_text()
            + """
import subprocess, time
child = subprocess.Popen([sys.executable, '-c', 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)'])
(p/'child.pid').write_text(str(child.pid))
i = 0
while True:
    print(json.dumps({'type':'tool','sessionID':sid,'part':{'type':'tool','tool':'bash','state':{'status':'completed','input':{'step':i},'output':str(i)}}}),flush=True)
    i += 1
    time.sleep(.2)
"""
        )
        other = subprocess.Popen(
            [sys.executable, "-c", "import time;time.sleep(30)"], start_new_session=True
        )
        self.addCleanup(
            lambda: (core.kill_tree(other.pid, grace=0.1), other.wait(timeout=3))
        )
        started = time.monotonic()
        rc, gate = fixture.motor_fixture(
            env={
                "CADRE_WARN_SEC": "1",
                "CADRE_ABORT_SEC": "4",
                "CADRE_STALL_SEC": "600",
                "CADRE_KILL_GRACE": ".2",
            }
        )
        elapsed = time.monotonic() - started
        self.assertNotEqual(rc, 0)
        self.assertLess(elapsed, 12)
        task = fixture.store.read(fixture.rid)["tasks"]["g1"]
        self.assertEqual(task["state"], "yarim")
        self.assertEqual(task["time_decision"], "sure-tavani")
        self.assertTrue(task["motor_clock"]["warned"])
        log = (
            fixture.store.task_dir(fixture.rid, "g1").joinpath("motor.log").read_text()
        )
        self.assertIn("SURE UYARISI", log)
        self.assertIn("SURE TAVANI", log)
        self.assertNotIn("cadre-guard: STALL", log)
        self.assertIsNone(
            process_identity(int((fixture.home / "child.pid").read_text()))
        )
        self.assertIsNone(other.poll())
        gate.assert_not_called()
        calls = fixture.calls()
        self.assertNotEqual(
            core.motor("a", fixture.store.read(fixture.rid)["prompt"]), 0
        )
        self.assertEqual(fixture.calls(), calls)
        self.assertEqual(
            fixture.store.read(fixture.rid)["tasks"]["g1"]["motor_clock"],
            task["motor_clock"],
        )

    def test_warning_allows_productive_completion_before_limit(self):
        fixture = self.fixture()
        fixture.fake.write_text(
            fixture.fake.read_text() + "\nimport time\ntime.sleep(1.8)\n"
        )
        rc, _ = fixture.motor_fixture(
            env={"CADRE_WARN_SEC": "1", "CADRE_ABORT_SEC": "8"}
        )
        self.assertEqual(rc, 0)
        task = fixture.store.read(fixture.rid)["tasks"]["g1"]
        self.assertEqual(task["state"], "uretti")
        self.assertTrue(task["motor_clock"]["warned"])
        self.assertNotIn("time_decision", task)

    def test_disk_wait_cannot_outlive_motor_limit_or_start_a_writer(self):
        fixture = self.fixture()
        started = time.monotonic()
        rc, gate = fixture.motor_fixture(
            env={
                "CADRE_WARN_SEC": "0",
                "CADRE_ABORT_SEC": "2",
                "CADRE_DISK_MIN_MB": "999999999",
                "CADRE_DISK_POLL": "30",
            }
        )
        self.assertNotEqual(rc, 0)
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(
            fixture.store.read(fixture.rid)["tasks"]["g1"]["time_decision"],
            "sure-tavani",
        )
        self.assertFalse((fixture.home / "calls.jsonl").exists())
        gate.assert_not_called()


class BoundedLivenessTests(unittest.TestCase):
    def process(self, program, *, recorded=False):
        directory = Path(tempfile.mkdtemp(prefix="bounded-live-"))
        self.addCleanup(shutil.rmtree, directory, True)
        log = directory / "stdout.jsonl"
        with log.open("w") as output:
            process = subprocess.Popen(
                [sys.executable, "-c", program], stdout=output, start_new_session=True
            )
        self.addCleanup(
            lambda: (core.kill_tree(process.pid, grace=0.1), process.wait(timeout=3))
        )
        if not recorded:
            return directory, log, process, process
        wrapper = object.__new__(core.RecordedProcess)
        wrapper.proc = process
        wrapper.pid = process.pid
        wrapper.attempt = cast(
            core.Attempt,
            SimpleNamespace(directory=directory, root=directory, stdout_path=log),
        )
        return directory, log, wrapper, process

    def test_log_cpu_open_connection_and_corrupt_evidence_do_not_renew_forever(self):
        for request in [
            "{",
            "[]",
            json.dumps(
                {
                    "pid": os.getpid(),
                    "started": time.time() * 1000,
                    "last_chunk_at": time.time() * 1000,
                    "tur": "yorum",
                }
            ),
        ]:
            with self.subTest(request=request[:1]):
                directory, log, proc, actual = self.process(
                    'import time\nwhile True:\n print("still working",flush=True)\n end=time.monotonic()+.1\n while time.monotonic()<end: pass',
                    recorded=True,
                )
                (directory / "requests").mkdir()
                (directory / "requests/one.json").write_text(request)
                (directory / "canlilik.json").write_text(
                    "[]" if request == "[]" else json.dumps({"en_uzun": 1e100})
                )
                guard = core.StallGuard(str(log), proc, 0.4)
                started = time.monotonic()
                result = None
                while not result and time.monotonic() - started < 3:
                    result = guard.tik("bounded")
                    time.sleep(0.05)
                self.assertEqual(result, "stall")
                actual.wait(timeout=3)
                state = json.loads((directory / "canlilik.json").read_text())
                self.assertEqual(state["durum"], "unknown")
                self.assertEqual(state["neden"], "uncertain-time-limit")
                self.assertEqual(state["karar"], "stop")
                self.assertLessEqual(state["gozlem_siniri_sn"], 1.2 + 1e-6)
                self.assertLess(time.monotonic() - started, 3)

    def test_completed_tool_steps_progress_but_identical_replay_does_not(self):
        program = """import json,time
for i in range(12):
 print(json.dumps({'type':'tool','part':{'type':'tool','tool':'bash','state':{'status':'completed','input':i,'output':str(i)}}}),flush=True)
 time.sleep(.12)
"""
        _, log, proc, actual = self.process(program)
        guard = core.StallGuard(str(log), proc, 0.3)
        while actual.poll() is None:
            self.assertIsNone(guard.tik("bounded"))
            time.sleep(0.04)
        self.assertEqual(actual.wait(timeout=3), 0)
        _, log, proc, actual = self.process(
            program.replace("range(12)", "range(100)").replace(
                "'input':i,'output':str(i)", "'input':0,'output':'same'"
            )
        )
        guard = core.StallGuard(str(log), proc, 0.3, dongu_esik=999)
        guard.tekrar.esik = 999
        started = time.monotonic()
        result = None
        while not result and time.monotonic() - started < 3:
            result = guard.tik("bounded")
            time.sleep(0.04)
        self.assertEqual(result, "stall")
        actual.wait(timeout=3)
        self.assertEqual(guard.canlilik.neden, "uncertain-time-limit")

    def test_wait_error_reaps_its_real_tree_and_preserves_a_foreign_process(self):
        _, log, proc, actual = self.process("import time\ntime.sleep(30)")
        _, _, _, foreign = self.process("import time\ntime.sleep(30)")
        with patch.object(
            core.Canlilik,
            "takildi",
            side_effect=OSError("fixture evidence disk failed"),
        ):
            with self.assertRaisesRegex(OSError, "fixture evidence disk failed"):
                core.wait_process(proc, 30, logf=log)
        self.assertIsNotNone(actual.poll())
        self.assertIsNone(foreign.poll())

    def test_denied_process_cleanup_raises_without_unbounded_wait_or_false_exit(self):
        _, _, proc, actual = self.process("import time\ntime.sleep(30)")
        started = time.monotonic()
        with patch.object(
            actual, "kill", side_effect=PermissionError("fixture signal denied")
        ):
            with self.assertRaisesRegex(PermissionError, "fixture signal denied"):
                core.reap(proc, timeout=0.1)
        self.assertLess(time.monotonic() - started, 1)
        self.assertIsNone(actual.poll())

    def test_cancel_record_target_is_measured_separately_from_full_teardown(self):
        directory, _, _, actual = self.process("""import json,signal,subprocess,sys,time
signal.signal(signal.SIGTERM,signal.SIG_IGN)
child=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)'])
time.sleep(.1)
print(json.dumps({'child':child.pid}),flush=True)
while True:time.sleep(.1)
""")
        store = RunStore(directory)
        rid = store.accept("cancel", "bounded cancellation")["run_id"]
        store.update(
            rid,
            state="running",
            owner_pid=os.getpid(),
            owner_identity=process_identity(os.getpid()),
        )
        owner = core.FileLock(store.path(rid) / ".owner.lock")
        self.assertTrue(owner.acquire(blocking=False))
        self.addCleanup(owner.close)
        end = time.monotonic() + 5
        while (
            not (directory / "stdout.jsonl").read_text().strip()
            and time.monotonic() < end
        ):
            time.sleep(0.02)
        child = json.loads((directory / "stdout.jsonl").read_text())["child"]
        errors = []

        def wait():
            try:
                core.wait_process(actual, 30)
            except BaseException as error:
                errors.append(error)

        with (
            patch.object(core, "QE", directory),
            patch.dict(os.environ, {"CADRE_RUN_ID": rid, "CADRE_KILL_GRACE": "2.5"}),
        ):
            worker = threading.Thread(target=wait)
            worker.start()
            self.addCleanup(worker.join, 10)
            started = time.monotonic()
            from kosu import iptal_et

            self.assertTrue(iptal_et(store, store.read(rid)))
            recorded = time.monotonic() - started
            self.assertTrue(store.read(rid)["cancel_requested"])
            self.assertTrue((store.path(rid) / "cancel.flag").exists())
            self.assertLess(recorded, 2)
            worker.join(10)
            teardown = time.monotonic() - started
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], core.CancelledRun)
        self.assertIsNotNone(actual.poll())
        self.assertIsNone(process_identity(child))
        self.assertGreater(teardown, 2)
        self.assertLess(teardown, 10)
        print(
            "CR14_CANCEL_METRICS",
            json.dumps({"record_seconds": recorded, "teardown_seconds": teardown}),
        )


class WatchdogTeardownTests(IsolatedNight):
    def test_wrong_owner_birth_starts_no_tick_or_replacement_watchdog(self):
        owner = process_identity(os.getpid())
        assert owner is not None
        env = {
            **self.env,
            "CADRE_BEKCI_SAHIP_PID": str(os.getpid()),
            "CADRE_BEKCI_SAHIP_KIMLIK": json.dumps({**owner, "started": "wrong-birth"}),
        }
        for name in ("saglik-nobet", "kota-izle"):
            result = subprocess.run(
                ["bash", str(self.root / f"bin/{name}.sh")],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 75)
            self.assertFalse((self.home / f"zincir/{name}.tik").exists())
            self.assertFalse((self.home / f"zincir/{name}.pid").exists())
        self.assertEqual(process_identity(os.getpid()), owner)

    def probe(self, mode):
        owner = subprocess.Popen(
            [sys.executable, "-c", "import sys;sys.stdin.read()"],
            stdin=subprocess.PIPE,
            start_new_session=True,
        )
        control = owner.stdin
        assert control is not None
        self.addCleanup(
            lambda: (core.kill_tree(owner.pid, grace=0.1), owner.wait(timeout=3))
        )
        self.addCleanup(control.close)
        foreign = IsolatedNight()
        foreign.setUp()
        self.addCleanup(foreign.doCleanups)
        sentinel = subprocess.Popen(
            ["bash", str(foreign.root / "bin/kota-izle.sh")],
            env=foreign.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.addCleanup(
            lambda: (core.kill_tree(sentinel.pid, grace=0.1), sentinel.wait(timeout=3))
        )
        identity = process_identity(sentinel.pid)
        guards = []
        env = {
            **self.env,
            "CADRE_BEKCI_SAHIP_PID": str(owner.pid),
            "CADRE_NOBET_ARALIK": "600",
            "CADRE_KOTA_ARALIK": "3600",
            "CADRE_AG_YOK": "1",
            "CADRE_DISK_MIN_MB": "0",
        }
        env.pop("CADRE_BEKCI_SAHIP_KIMLIK", None)
        for name in ("saglik-nobet", "kota-izle"):
            proc = subprocess.Popen(
                ["bash", str(self.root / f"bin/{name}.sh")],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            guards.append(proc)
            self.addCleanup(
                lambda p=proc: (core.kill_tree(p.pid, grace=0.1), p.wait(timeout=3))
            )
        self.wait_for(
            lambda: all(
                (self.home / f"zincir/{name}.tik").exists()
                for name in ("saglik-nobet", "kota-izle")
            ),
            15,
            "native watchdog ticks",
        )
        self.wait_for(
            lambda: (foreign.home / "zincir/kota-izle.tik").exists(),
            15,
            "foreign watchdog",
        )
        children = {pid for proc in guards for pid in core._cocuklar(proc.pid)}
        started = time.monotonic()
        if mode == "normal":
            control.close()
            self.assertEqual(owner.wait(timeout=3), 0)
        elif mode == "error":
            owner.kill()
            owner.wait(timeout=3)
        elif mode == "home-deleted":
            shutil.rmtree(self.home)
        else:
            shutil.rmtree(self.root)
        for proc in guards:
            self.assertEqual(proc.wait(timeout=20), 0)
        self.wait_for(
            lambda: all(process_identity(pid) is None for pid in children),
            8,
            "watchdog children gone",
        )
        self.assertLess(time.monotonic() - started, 25)
        self.assertEqual(process_identity(sentinel.pid), identity)
        self.assertIsNone(sentinel.poll())
        if self.home.exists():
            old = {
                name: (self.home / f"zincir/{name}.pid").read_bytes()
                for name in ("saglik-nobet", "kota-izle")
            }
            time.sleep(0.3)
            self.assertEqual(
                {name: (self.home / f"zincir/{name}.pid").read_bytes() for name in old},
                old,
            )

    def test_normal_owner_completion_closes_watchdogs_and_children(self):
        self.probe("normal")

    def test_owner_error_closes_watchdogs_and_children(self):
        self.probe("error")

    def test_removed_test_home_cannot_leave_or_revive_watchdogs(self):
        self.probe("home-deleted")

    def test_removed_code_root_cannot_leave_or_revive_watchdogs(self):
        self.probe("code-deleted")


if __name__ == "__main__":
    unittest.main()
