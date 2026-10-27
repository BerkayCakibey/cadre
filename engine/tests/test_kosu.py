"""C5: durable task ownership. All subprocesses use temporary engine homes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))

from kosu import FileLock, RunStore  # noqa: E402


class RunStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="qe-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = RunStore(self.home)

    def test_accept_is_durable_and_preserves_prompt(self) -> None:
        prompt = "sadece rapor\naynı\naynı\n  \n"
        run = self.store.accept("proje", prompt, source_sha="abc123")
        self.store.task_update(run["run_id"], "g1", state="running", session_id="ses_123")
        # A fresh interpreter must find the original prompt, source and step.
        code = "from kosu import RunStore; import json,sys; print(json.dumps(RunStore(sys.argv[1]).read(sys.argv[2])))"
        result = subprocess.run(
            [sys.executable, "-c", code, str(self.home), run["run_id"]],
            env={**os.environ, "PYTHONPATH": str(LIB)}, capture_output=True,
            text=True, check=True, timeout=10,
        )
        restored = json.loads(result.stdout)
        self.assertEqual(restored["source_sha"], "abc123")
        self.assertEqual(restored["tasks"]["g1"]["session_id"], "ses_123")
        self.assertEqual(Path(restored["prompt"]).read_text(), prompt)

    def test_saved_plan_records_the_planner_model(self) -> None:
        """4b (07.10): the judge rule must know the planner's family; the plan model is written to the record when the plan is saved."""
        rid = self.store.accept('lab', 'kucuk is')['run_id']
        plan = {'is_id': 'x', 'kapsam_puani': 10, 'kapsam_gerekce': 'kucuk', 'gorevler': [{'id': 'g1', 'tanim': 'yaz', 'dosyalar': ['a.py'],
                'kabul': 'test', 'kapsam_disi': [], 'bagimli': [], 'alt_ajanlar': [], 'zorluk': 1, 'model': 'ds-pro',
                'paralel_grup': 1, 'tahmin_dk': 5, 'tahmin_usd': 0.1}], 'birlestirme_sirasi': ['g1'], 'kalite': {}}
        r = subprocess.run([sys.executable, str(LIB / 'kosu.py'), 'save-plan', rid, 'opencode-go/qwen3.8-max'],
                           input=json.dumps(plan), text=True, capture_output=True, env={**os.environ, 'CADRE_HOME': str(self.home)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.store.read(rid)['plan_modeli'], 'opencode-go/qwen3.8-max')

    def test_spending_approval_is_recorded_by_the_cli(self) -> None:
        rid = self.store.accept('lab', 'kucuk is')['run_id']
        r = subprocess.run([sys.executable, str(LIB / 'kosu.py'), 'harcama-onay', rid], text=True, capture_output=True,
                           env={**os.environ, 'CADRE_HOME': str(self.home)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertGreater(self.store.read(rid)['harcama_onayi_ts'], 0)

    def test_concurrent_runs_do_not_share_paths(self) -> None:
        runs = [self.store.accept(p, "task") for p in ("a", "a", "b")]
        self.assertEqual(len({r["run_id"] for r in runs}), 3)
        paths = [self.store.task_dir(r["run_id"], "g1") for r in runs]
        self.assertEqual(len(set(paths)), 3)
        for i, path in enumerate(paths):
            (path / "result.txt").write_text(str(i))
        self.assertEqual([(p / "result.txt").read_text() for p in paths], ["0", "1", "2"])

    def test_lock_is_exclusive_between_processes_and_released_after_exit(self) -> None:
        lock_path = self.home / "local.lock"
        code = "from kosu import FileLock; import sys; l=FileLock(sys.argv[1]); sys.exit(0 if l.acquire(blocking=False) else 7)"
        args = [sys.executable, "-c", code, str(lock_path)]
        env = {**os.environ, "PYTHONPATH": str(LIB)}
        with FileLock(lock_path):
            self.assertEqual(subprocess.run(args, env=env, timeout=10).returncode, 7)
        self.assertEqual(subprocess.run(args, env=env, timeout=10).returncode, 0)

    def test_task_updates_do_not_lose_another_process_update(self) -> None:
        run = self.store.accept("a", "task")
        code = "from kosu import RunStore; import sys; RunStore(sys.argv[1]).task_update(sys.argv[2],sys.argv[3],state='done')"
        procs = [subprocess.Popen(
            [sys.executable, "-c", code, str(self.home), run["run_id"], f"g{i}"],
            env={**os.environ, "PYTHONPATH": str(LIB)},
        ) for i in range(8)]
        for proc in procs:
            self.assertEqual(proc.wait(timeout=10), 0)
        self.assertEqual(len(self.store.read(run["run_id"])["tasks"]), 8)

    def test_path_components_cannot_escape_run(self) -> None:
        run = self.store.accept("a", "task")
        for bad in ("../x", "/tmp/x", "g1/../../x", "", "x\ny"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.task_dir(run["run_id"], bad)
        outside = self.home / "outside"
        outside.mkdir()
        (self.store.path(run["run_id"]) / "tasks").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.store.task_dir(run["run_id"], "g1")
        self.assertEqual(list(outside.iterdir()), [])

    def test_failed_atomic_update_preserves_previous_record(self) -> None:
        run = self.store.accept("a", "prompt")
        with patch("kosu.os.replace", side_effect=OSError("injected disk failure")):
            with self.assertRaises(OSError):
                self.store.task_update(run["run_id"], "g1", state="tamam")
        self.assertEqual(self.store.read(run["run_id"])["tasks"], {})

    def test_cancellation_is_scoped_and_survives_restart(self) -> None:
        a, b = [self.store.accept(project, "prompt") for project in ("a", "b")]
        env = {**os.environ, "CADRE_HOME": str(self.home)}
        command = [sys.executable, str(LIB / "kosu.py")]
        subprocess.run(command + ["cancel", "a"], env=env, check=True, stdout=subprocess.DEVNULL, timeout=5)
        self.assertTrue((self.store.path(a["run_id"]) / "cancel.flag").exists())
        self.assertFalse((self.store.path(b["run_id"]) / "cancel.flag").exists())
        self.assertEqual(subprocess.run(command + ["run", a["run_id"]], env=env, timeout=5).returncode, 130)
        self.assertEqual(self.store.read(b["run_id"])["state"], "accepted")

    def test_coordinator_restart_does_not_repeat_dispatch(self) -> None:
        root = self.home / "code"
        (root / "bin").mkdir(parents=True)
        (root / "bin/cadre-dispatch.sh").write_text('echo dispatch >> "$CADRE_RUN_DIR/calls"\nexit 3\n')
        (root / "bin/is-motoru.sh").write_text(
            'echo motor >> "$CADRE_RUN_DIR/calls"\n'
            'if [ ! -f "$CADRE_RUN_DIR/retry" ]; then touch "$CADRE_RUN_DIR/retry"; exit 1; fi\n'
            'exit 0\n'
        )
        run = self.store.accept("a", "original task")
        env = {**os.environ, "CADRE_HOME": str(self.home), "CADRE_ENGINE_ROOT": str(root)}
        command = [sys.executable, str(LIB / "kosu.py"), "run", run["run_id"]]
        self.assertEqual(subprocess.run(command, env=env, timeout=10).returncode, 1)
        self.assertEqual(subprocess.run(command, env=env, timeout=10).returncode, 1)
        self.assertEqual((self.store.path(run["run_id"]) / "calls").read_text().splitlines(),
                         ["dispatch", "motor", "motor"])
        self.assertEqual(self.store.read(run["run_id"])["state"], "retryable")  # Fixture has no QA/review evidence.


class RequestTests(unittest.TestCase):
    def test_saved_defaults_and_explicit_cli_choices_keep_their_own_task_values(self) -> None:
        from kosu import gorev_varsayilanlari, gorev_varsayilanlari_yaz, validate_request
        with patch('kosu._baslat', side_effect=AssertionError('no executor')):
            missing = gorev_varsayilanlari(self.home)
            self.assertFalse((self.home / 'zincir').exists())
            self.assertEqual((missing['preference'], missing['difficulty_override']), ('balanced', None))
            gorev_varsayilanlari_yaz(self.home, 'economic', 'easy', missing)
            state = gorev_varsayilanlari(self.home)
            raw = {k: v for k, v in self.raw.items() if k not in ('preference', 'difficulty_override')}
            checked, _ = validate_request(self.home, raw)
            result = subprocess.run(['bash', str(LIB.parent / 'bin/cadre.sh'), 'istek'], input=json.dumps(raw),
                                    text=True, capture_output=True, env=self.env, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            saved = self.store.read(json.loads(result.stdout)['run_id'])
            self.assertEqual(saved['request'], checked)
            self.assertEqual(saved['request']['selection_defaults']['inherited'], ['preference', 'difficulty_override'])
            self.assertEqual((saved['request']['preference'], saved['request']['difficulty_override']), ('economic', 'easy'))
            self.assertEqual(saved['reason'], 'request-plan-gate')
            before = (self.store.path(saved['run_id']) / 'run.json').read_bytes()
            gorev_varsayilanlari_yaz(self.home, 'strong', 'hard', state)
            self.assertEqual(before, (self.store.path(saved['run_id']) / 'run.json').read_bytes())
            self.assertEqual(self.store.request(raw), saved, 'Lost reply must replay original inherited values')
            explicit = self.store.request({**self.raw, 'submission_id': 'explicit'})
            self.assertEqual((explicit['request']['preference'], explicit['request']['difficulty_override']), ('balanced', None))
            self.assertNotIn('selection_defaults', explicit['request'])
            self.assertEqual(explicit['budget_cap_usd'], saved['budget_cap_usd'])
            self.assertEqual(explicit['planning']['permissions'], 'pending')

    def test_changed_defaults_reject_open_form_and_summary_but_not_published_replay(self) -> None:
        from kosu import RequestError, gorev_varsayilanlari, gorev_varsayilanlari_yaz, validate_request
        state = gorev_varsayilanlari(self.home)
        raw = {**self.raw, 'defaults_token': state['token']}
        checked, _ = validate_request(self.home, raw)
        raw['source_token'] = checked['source_token']
        saved = self.store.request(raw)
        gorev_varsayilanlari_yaz(self.home, 'strong', 'hard', state)
        self.assertEqual(self.store.request(raw), saved)
        with self.assertRaisesRegex(RequestError, 'varsayılanları değişti'):
            self.store.request({**raw, 'submission_id': 'stale'})
        self.assertEqual(len(self.store.list()), 1)
        current = gorev_varsayilanlari(self.home)
        gorev_varsayilanlari_yaz(self.home, 'balanced', None, current)
        self.assertNotEqual(gorev_varsayilanlari(self.home)['token'], state['token'], 'ABA revisions must conflict')
        with self.assertRaises(ValueError):
            gorev_varsayilanlari_yaz(self.home, 'economic', None, state)

    def test_invalid_unreadable_and_special_defaults_never_read_or_publish_work(self) -> None:
        from kosu import RequestError, gorev_varsayilanlari, gorev_varsayilanlari_yaz
        state = gorev_varsayilanlari(self.home)
        for preference, difficulty in (('magic', None), ('strong', True), ('balanced', 'NaN')):
            with self.subTest(preference=preference, difficulty=difficulty), self.assertRaises(ValueError):
                gorev_varsayilanlari_yaz(self.home, preference, difficulty, state)
        self.assertFalse((self.home / 'zincir').exists())
        folder = self.home / 'zincir'; folder.mkdir()
        path = folder / 'gorev-varsayilanlari.json'
        implicit = {k: v for k, v in self.raw.items() if k not in ('preference', 'difficulty_override')}
        for content in (b'{', b'null', b'{}', b'[]', b'\xff', b' ' * 1025,
                        json.dumps({'preference': 'strong', 'difficulty_override': None, 'revision': 'a' * 32, 'permissions': 'allow'}).encode()):
            path.write_bytes(content)
            with self.subTest(content=content[:50]), self.assertRaises(ValueError):
                gorev_varsayilanlari(self.home)
            with self.assertRaises(RequestError):
                self.store.request(implicit)
            self.assertEqual(path.read_bytes(), content)
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises(ValueError):
            gorev_varsayilanlari(self.home)
        path.unlink()
        outside = self.home / 'synthetic-private'; outside.write_text('SYNTHETIC_DO_NOT_READ')
        path.symlink_to(outside)
        with self.assertRaises(OSError):
            gorev_varsayilanlari(self.home)
        self.assertEqual(outside.read_text(), 'SYNTHETIC_DO_NOT_READ')
        path.unlink()
        folder.rmdir()
        folder.symlink_to(self.repo)
        with self.assertRaises(OSError):
            gorev_varsayilanlari(self.home)
        with self.assertRaises(OSError):
            gorev_varsayilanlari_yaz(self.home, 'strong', None, state)
        self.assertFalse((self.repo / 'gorev-varsayilanlari.json').exists())
        self.assertEqual(self.store.list(), [])

    def test_defaults_write_race_disk_failure_and_real_process_death_preserve_atomic_state(self) -> None:
        from kosu import gorev_varsayilanlari, gorev_varsayilanlari_yaz
        state = gorev_varsayilanlari(self.home)
        worker = '''import json,sys
from pathlib import Path
from kosu import gorev_varsayilanlari_yaz
try:
    gorev_varsayilanlari_yaz(Path(sys.argv[1]),sys.argv[2],None,json.load(sys.stdin))
    print('saved')
except ValueError:
    print('conflict')
'''
        env = {**self.env, 'PYTHONPATH': str(LIB)}
        children = [subprocess.Popen([sys.executable, '-c', worker, str(self.home), value], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env) for value in ('economic', 'strong')]
        try:
            for child in children:
                child.stdin.write(json.dumps(state)); child.stdin.close(); child.stdin = None
            replies = []
            for child in children:
                out, err = child.communicate(timeout=10)
                self.assertEqual(child.returncode, 0, err)
                replies.append(out.strip())
            self.assertEqual(sorted(replies), ['conflict', 'saved'])
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate(); child.communicate(timeout=5)
                for stream in (child.stdin, child.stdout, child.stderr):
                    if stream is not None:
                        stream.close()
        path = self.home / 'zincir/gorev-varsayilanlari.json'
        before = path.read_bytes()
        state = gorev_varsayilanlari(self.home)
        with patch('kosu.os.replace', side_effect=OSError('injected disk failure')), self.assertRaises(OSError):
            gorev_varsayilanlari_yaz(self.home, 'balanced', 'hard', state)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(list(path.parent.glob('.gorev-varsayilanlari-*')))
        code = '''import json,os,sys
from pathlib import Path
from unittest.mock import patch
from kosu import gorev_varsayilanlari_yaz
with patch('kosu.os.replace',side_effect=lambda *a,**kw:os._exit(99)):
    gorev_varsayilanlari_yaz(Path(sys.argv[1]),'balanced','hard',json.load(sys.stdin))
'''
        result = subprocess.run([sys.executable, '-c', code, str(self.home)], input=json.dumps(state), text=True,
                                capture_output=True, env=env, timeout=10)
        self.assertEqual(result.returncode, 99, result.stderr)
        self.assertEqual(path.read_bytes(), before)
        gorev_varsayilanlari_yaz(self.home, 'balanced', 'hard', state)
        self.assertEqual(gorev_varsayilanlari(self.home)['difficulty_override'], 'hard')
        self.assertEqual(self.store.list(), [])

    def test_defaults_directory_swap_cannot_redirect_a_write_outside_the_pinned_directory(self) -> None:
        from kosu import gorev_varsayilanlari, gorev_varsayilanlari_yaz
        state = gorev_varsayilanlari(self.home)
        gorev_varsayilanlari_yaz(self.home, 'balanced', None, state)
        state = gorev_varsayilanlari(self.home)
        outside = self.home / 'outside-defaults'; outside.mkdir()
        pinned = self.home / 'parked-defaults'
        original_open = os.open
        swapped = False
        def swap(path, flags, *args, **kwargs):
            nonlocal swapped
            if isinstance(path, str) and path.startswith('.gorev-varsayilanlari-') and not swapped:
                swapped = True
                (self.home / 'zincir').rename(pinned)
                (self.home / 'zincir').symlink_to(outside)
            return original_open(path, flags, *args, **kwargs)
        with patch('kosu.os.open', side_effect=swap), self.assertRaisesRegex(ValueError, 'directory changed'):
            gorev_varsayilanlari_yaz(self.home, 'strong', 'hard', state)
        self.assertTrue(swapped)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(json.loads((pinned / 'gorev-varsayilanlari.json').read_text())['preference'], 'balanced')
        self.assertFalse(list(pinned.glob('.gorev-varsayilanlari-*')))

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix='cadre-request-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.repo = self.home / 'repo'
        self.repo.mkdir()
        project = self.home / 'projects/lab'
        project.mkdir(parents=True)
        (project / 'repo-path').write_text(str(self.repo))
        (self.repo / 'BACKLOG.md').write_text('### [x] B001 Bitti\n### [ ] B002 Kalan\n')
        self.store = RunStore(self.home)
        self.raw = dict(submission_id='submit-1', project='lab', target_path=str(self.repo), target_kind='existing',
                        input_mode='goal', prompt='  Özgün istek\nİkinci satır  ', output='Çalışan çıktı', acceptance='',
                        preference='balanced', difficulty_override=None, duration_minutes='60', budget_usd='0.125',
                        backlog_path='', source_token='')
        self.env = {**os.environ, 'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(LIB.parent), 'CADRE_AG_YOK': '1'}

    def test_panel_validation_and_real_cli_keep_the_same_request_without_starting_work(self) -> None:
        from kosu import validate_request
        for mode in ('goal', 'backlog', 'plan_only'):
            with self.subTest(mode=mode):
                raw = {**self.raw, 'submission_id': mode, 'input_mode': mode, 'backlog_path': 'BACKLOG.md' if mode == 'backlog' else ''}
                checked, backlog = validate_request(self.home, raw)
                raw['source_token'] = checked['source_token']
                result = subprocess.run(['bash', str(LIB.parent / 'bin/cadre.sh'), 'istek'], input=json.dumps(raw),
                                        text=True, capture_output=True, env=self.env, cwd=self.repo, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                record = self.store.read(json.loads(result.stdout)['run_id'])
                self.assertEqual(record['request'], checked)
                self.assertEqual(Path(record['prompt']).read_text(), self.raw['prompt'])
                self.assertEqual(record['planning']['status'], 'pending')
                self.assertIsNone(record['planning']['difficulty_estimate'])
                self.assertEqual(record['budget_cap_usd'], 0.125)
                self.assertEqual(record['request_budget']['amount_usd'], '0.125')
                self.assertEqual(record['budget_spent_usd'], 0)
                self.assertEqual(record['tasks'], {})
                if backlog is not None:
                    self.assertEqual((self.store.path(record['run_id']) / 'backlog.md').read_bytes(), backlog)
        self.assertFalse((self.home / 'zincir').exists(), 'Gönderim model/bütçe gözlemi başlatmaz')
        self.assertEqual(sorted(p.name for p in self.repo.iterdir()), ['BACKLOG.md'])

    def test_request_budget_shares_are_validated_and_bound_into_the_approval(self) -> None:
        """CR-08: justified pocket shares at request level (fixed total) are part of the request; the proposal gate and the run contract use the same shares,
        the approval binds them. An invalid share is rejected; an empty field keeps the default shares."""
        from kosu import RequestError, istek_paylari, or_yol_politikasi, plan_digest, request_authorized, validate_request
        raw = {**self.raw, 'budget_shares': 'seritler=0.71, hakem=0.17, yardimci=0.12'}
        checked, _ = validate_request(self.home, raw)
        self.assertEqual(checked['budget_shares'], {'seritler': 0.71, 'hakem': 0.17, 'yardimci': 0.12})
        plain, _ = validate_request(self.home, self.raw)
        self.assertNotIn('budget_shares', plain)
        bos, _ = validate_request(self.home, {**self.raw, 'budget_shares': ' '})
        self.assertNotIn('budget_shares', bos)
        payli = self.store.request({**raw, 'submission_id': 'payli'})
        paysiz = self.store.request({**self.raw, 'submission_id': 'paysiz'})
        self.assertNotEqual(payli['request_input_hash'], paysiz['request_input_hash'])
        self.assertEqual(istek_paylari(payli['request']), {'seritler': 0.71, 'hakem': 0.17, 'yardimci': 0.12})
        self.assertIsNone(istek_paylari(paysiz['request']))
        for deger in ('seritler=0.8,hakem=0.1,yardimci=0.2', 'seritler=0.9,hakem=0.1', 'seritler=abc,hakem=0.1,yardimci=0.1',
                      'seritler=1,hakem=0,yardimci=0', 'seritler=0.7,hakem=0.2,yardimci=0.1,ekstra=0', '0.7,0.2,0.1',
                      'seritler=0.7,hakem=0.2,hakem=0.1', 'seritler=-0.2,hakem=0.6,yardimci=0.6'):
            with self.subTest(deger=deger), self.assertRaises(RequestError):
                validate_request(self.home, {**raw, 'budget_shares': deger})
        action = {'request_hash': payli['request_input_hash'], 'budget_usd': payli['request']['budget_usd'],
                  'duration_minutes': payli['request']['duration_minutes'], 'review_limits': payli.get('review_limits'),
                  'supervision': payli.get('supervision'), 'qa_profile': payli.get('qa_profile'), 'acceptance_scope': None,
                  'models': {'plan': ['opencode-go/glm-5.3']}, 'or_policy': or_yol_politikasi({'plan': ['opencode-go/glm-5.3']}),
                  'budget_shares': {'seritler': 0.71, 'hakem': 0.17, 'yardimci': 0.12}}
        bagli = {**payli, 'request_permission': {'status': 'approved', 'token': plan_digest(action), 'action': action}}
        self.assertTrue(request_authorized(bagli))
        eksik = {**action, 'budget_shares': None}
        bagsiz = {**payli, 'request_permission': {'status': 'approved', 'token': plan_digest(eksik), 'action': eksik}}
        self.assertFalse(request_authorized(bagsiz), 'onay istegin paylarini baglar')

    def test_request_scope_fields_are_validated_and_bound_into_the_input_hash(self) -> None:
        """CR-35: the narrow work scope (allowed files + required tests) is part of the request; the approval binds this input."""
        from kosu import RequestError, validate_request
        (self.repo / 'tests').mkdir()
        (self.repo / 'tests/test_a.py').write_text('def test_a():\n    assert True\n')
        raw = {**self.raw, 'scope_files': 'tests/test_a.py, src/yeni.py', 'scope_tests': 'tests/test_a.py'}
        checked, _ = validate_request(self.home, raw)
        self.assertEqual(checked['scope_files'], ['src/yeni.py', 'tests/test_a.py'])
        self.assertEqual(checked['scope_tests'], ['tests/test_a.py'])
        plain, _ = validate_request(self.home, self.raw)
        self.assertNotIn('scope_files', plain)
        self.assertNotIn('scope_tests', plain)
        bos, _ = validate_request(self.home, {**self.raw, 'scope_files': ' ', 'scope_tests': ''})
        self.assertNotIn('scope_files', bos)
        with_scope = self.store.request({**raw, 'submission_id': 'kapsamli'})
        without = self.store.request({**self.raw, 'submission_id': 'kapsamsiz'})
        self.assertNotEqual(with_scope['request_input_hash'], without['request_input_hash'])
        self.assertEqual(with_scope['request']['scope_files'], ['src/yeni.py', 'tests/test_a.py'])
        for alan, deger in (('scope_files', '/etc/passwd'), ('scope_files', '../dis.py'), ('scope_files', '.git/config'),
                            ('scope_tests', 'tests/yok.py'), ('scope_tests', 'tests'), ('scope_files', 'credentials.json'),
                            ('scope_tests', 'tests/test_a.py, tests/test_a.py')):
            with self.subTest(alan=alan, deger=deger), self.assertRaises(RequestError):
                validate_request(self.home, {**raw, alan: deger})
        ayrik, _ = validate_request(self.home, {**raw, 'scope_tests': 'tests/test_a.py', 'scope_files': 'src/yeni.py'})
        self.assertEqual((ayrik['scope_files'], ayrik['scope_tests']), (['src/yeni.py'], ['tests/test_a.py']), 'gerekli test degismeyen dosya olabilir')
        with self.assertRaises(RequestError):
            validate_request(self.home, {**raw, 'scope_files': '', 'scope_tests': 'tests/test_a.py'})
        with self.assertRaises(RequestError):
            validate_request(self.home, {**raw, 'prompt': 'Kodu incele; sadece rapor yaz, değişiklik yapma.'})

    def test_invalid_fields_limits_and_paths_create_no_task(self) -> None:
        from kosu import RequestError
        cases = [('prompt', ''), ('output', ''), ('preference', 'magic'), ('difficulty_override', True),
                 ('duration_minutes', 'NaN'), ('duration_minutes', 0), ('duration_minutes', True), ('duration_minutes', '43201'),
                 ('budget_usd', 'NaN'), ('budget_usd', 'Infinity'), ('budget_usd', '-1'), ('budget_usd', True),
                 ('budget_usd', '0.0000001'), ('budget_usd', '9007199254.740992'), ('target_path', '/does-not-exist'),
                 ('input_mode', 'auto'), ('backlog_path', 'BACKLOG.md'), ('submission_id', '../escape'),
                 ('target_path', '\x00'), ('backlog_path', 'a' * 4097)]
        for field, value in cases:
            with self.subTest(field=field, value=value), self.assertRaises(RequestError):
                self.store.request({**self.raw, field: value})
        with self.assertRaises(RequestError):
            self.store.request({**self.raw, 'permissions': 'everything'})
        self.assertEqual(self.store.list(), [])
        self.assertFalse((self.home / 'request-pending').exists())
        zero = self.store.request({**self.raw, 'budget_usd': '0', 'duration_minutes': '43200'})
        self.assertEqual(zero['request_budget']['amount_usd'], '0')

    def test_backlog_missing_secret_symlink_fifo_escape_and_source_change_are_rejected(self) -> None:
        from kosu import RequestError, validate_request
        secret = self.repo / '.env.txt'
        secret.write_text('SYNTHETIC_SECRET_DO_NOT_READ')
        (self.repo / 'linked.md').symlink_to(secret)
        outside = self.home / 'outside.md'
        outside.write_text('outside')
        os.mkfifo(self.repo / 'pipe.md')
        raw = {**self.raw, 'input_mode': 'backlog'}
        for name in ('missing.md', '.env.txt', 'linked.md', 'pipe.md', '../outside.md', str(outside)):
            with self.subTest(name=name), self.assertRaises(RequestError):
                self.store.request({**raw, 'backlog_path': name})
        raw['backlog_path'] = 'BACKLOG.md'
        checked, _ = validate_request(self.home, raw)
        raw['source_token'] = checked['source_token']
        (self.repo / 'BACKLOG.md').write_text('Changed after preview')
        with self.assertRaises(RequestError):
            self.store.request(raw)
        self.assertEqual(self.store.list(), [])

        nested = self.repo / 'nested'
        nested.mkdir()
        (nested / 'BACKLOG.md').write_text('initial source')
        swapped = self.home / 'swapped'
        swapped.mkdir()
        (swapped / 'BACKLOG.md').write_text('SYNTHETIC_SECRET_MUST_NOT_BE_READ')
        original_open = os.open
        def swap_directory(name, flags, *args, **kwargs):
            if name == 'nested':
                nested.rename(self.repo / 'original-nested')
                nested.symlink_to(swapped, target_is_directory=True)
            return original_open(name, flags, *args, **kwargs)
        with patch('kosu.os.open', side_effect=swap_directory), self.assertRaises(RequestError):
            self.store.request({**raw, 'source_token': '', 'backlog_path': 'nested/BACKLOG.md'})
        self.assertEqual(self.store.list(), [])

    def test_repeated_submission_and_cancelled_reconnect_keep_one_original_record(self) -> None:
        from kosu import RequestError, iptal_et
        raw = {**self.raw, 'input_mode': 'backlog', 'backlog_path': 'BACKLOG.md'}
        first = self.store.request(raw)
        (self.repo / 'BACKLOG.md').write_text('source later changed')
        again = self.store.request(raw)
        self.assertEqual(first, again, 'Yanıt kaybından sonra yeni kaynakla yeni iş oluşturulmaz')
        with self.assertRaises(RequestError):
            self.store.request({**raw, 'budget_usd': '3'})
        self.assertTrue(iptal_et(self.store, first))
        self.assertEqual(self.store.request(raw)['state'], 'cancelled')
        result = subprocess.run([sys.executable, str(LIB / 'kosu.py'), 'request'], input=json.dumps(raw), text=True,
                                capture_output=True, env=self.env, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'cancelled')
        self.assertEqual(len(self.store.list()), 1)
        self.assertIn('B002', (self.store.path(first['run_id']) / 'backlog.md').read_text())

    def test_concurrent_cli_submissions_publish_one_task(self) -> None:
        processes = [subprocess.Popen([sys.executable, str(LIB / 'kosu.py'), 'request'], env=self.env,
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
        for process in processes:
            process.stdin.write(json.dumps(self.raw))
            process.stdin.close()
            process.stdin = None
        replies = []
        for process in processes:
            out, err = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, err)
            replies.append(json.loads(out)['run_id'])
        self.assertEqual(len(set(replies)), 1)
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(list((self.home / 'request-pending').iterdir()), [])

    def test_interruption_before_publish_recovers_without_a_partial_task(self) -> None:
        code = '''import json,os,sys
from unittest.mock import patch
from kosu import RunStore
original = os.replace
def stop(src,dst):
    if str(dst).endswith(sys.argv[2]): os._exit(99)
    return original(src,dst)
with patch('kosu.os.replace', side_effect=stop): RunStore(sys.argv[1]).request(json.load(sys.stdin))
'''
        import hashlib
        rid = 'request-' + hashlib.sha256(self.raw['submission_id'].encode()).hexdigest()[:32]
        result = subprocess.run([sys.executable, '-c', code, str(self.home), rid], input=json.dumps(self.raw), text=True,
                                capture_output=True, timeout=10, env={**self.env, 'PYTHONPATH': str(LIB)})
        self.assertEqual(result.returncode, 99, result.stderr)
        self.assertEqual(self.store.list(), [])
        record = self.store.request(self.raw)
        self.assertEqual(record['run_id'], rid)
        self.assertEqual(len(self.store.list()), 1)

    def test_plan_gate_blocks_manual_continue_watchdog_and_budget_mutation(self) -> None:
        from kosu import execute_run, revive
        record = self.store.request(self.raw)
        rid = record['run_id']
        before = (self.store.path(rid) / 'run.json').read_bytes()
        with patch('kosu.subprocess.Popen', side_effect=AssertionError('executor must not start')):
            self.assertEqual(execute_run(self.store, rid, devam=True), 78)
            with patch('butce_kapisi.hayalet_kapat'), patch('kosu._baslat', side_effect=AssertionError('watchdog must not start')):
                self.assertEqual(revive(self.store, LIB.parent), [])
        self.assertEqual((self.store.path(rid) / 'run.json').read_bytes(), before)
        for changes in ({'request': {}}, {'request_input_hash': 'changed'}, {'budget_cap_usd': 9}, {'budget_manual': False}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.update(rid, **changes)
        normal = self.store.accept('lab', 'independent existing task')
        self.assertEqual(normal['state'], 'accepted', 'Plan bekleyen girdi çalışan iş kuyruğunu tutmaz')
        self.assertTrue(self.store.sirasi_geldi(normal['run_id']))

    def test_new_project_intent_never_registers_creates_or_commits_a_target(self) -> None:
        raw = {**self.raw, 'project': 'future', 'target_kind': 'new', 'target_path': str(self.home / 'future')}
        with patch('kosu.subprocess.run', side_effect=AssertionError('no Git or other command')):
            record = self.store.request(raw)
        self.assertFalse((self.home / 'future').exists())
        self.assertFalse((self.home / 'projects/future').exists())
        self.assertEqual(record['planning']['permissions'], 'pending')

    def test_new_project_request_rejects_registered_name_and_accepts_initialized_empty_target(self) -> None:
        from kosu import RequestError, request_product
        with self.assertRaises(RequestError) as caught:
            self.store.request({**self.raw, 'target_kind': 'new', 'target_path': str(self.home / 'elsewhere')})
        self.assertEqual(caught.exception.field, 'project')
        self.assertEqual(self.store.list(), [])
        target = self.home / 'pre-init'
        target.mkdir()
        env = {**os.environ, 'GIT_AUTHOR_NAME': 'Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.com',
               'GIT_COMMITTER_NAME': 'Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.com'}
        subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=target, check=True, env=env)
        subprocess.run(['git', 'commit', '-q', '--allow-empty', '-m', 'init'], cwd=target, check=True, env=env)
        record = self.store.request({**self.raw, 'project': 'pre-init', 'target_kind': 'new', 'target_path': str(target)})
        self.assertEqual(request_product(record)['head'], '')
        (target / 'README.md').write_text('no longer empty')
        with self.assertRaisesRegex(ValueError, 'request-source-changed'):
            request_product(record)
        with self.assertRaises(RequestError):
            self.store.request({**self.raw, 'submission_id': 'submit-2', 'project': 'pre-init-2', 'target_kind': 'new', 'target_path': str(target)})

    def test_disk_error_and_symlink_staging_publish_no_partial_success(self) -> None:
        from kosu import RequestError
        with patch('kosu.atomic_json', side_effect=OSError('injected disk failure')), self.assertRaises(OSError):
            self.store.request(self.raw)
        self.assertEqual(self.store.list(), [])
        saved = self.store.request(self.raw)
        self.assertEqual(len(self.store.list()), 1)
        outside = self.home / 'outside'
        outside.mkdir()
        broken_home = self.home / 'symlink-home'
        broken_home.mkdir()
        (broken_home / 'runs').symlink_to(outside)
        with self.assertRaises(RequestError):
            RunStore(broken_home).request({**self.raw, 'submission_id': 'other'})
        self.assertEqual(list(outside.iterdir()), [])
        for key in ('request', 'request_budget', 'request_input_hash'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.store.update(saved['run_id'], **{key: None})
        record_path = self.store.path(saved['run_id']) / 'run.json'
        record_path.rename(record_path.with_suffix('.original'))
        record_path.symlink_to(outside / 'private.json')
        (outside / 'private.json').write_text('SYNTHETIC_SECRET_MUST_NOT_BE_READ')
        with patch.object(self.store, 'read', side_effect=AssertionError('symlink must not be read')), self.assertRaises(RequestError):
            self.store.request(self.raw)


if __name__ == "__main__":
    unittest.main()


class MotorOrtamiTests(unittest.TestCase):
    """B114-6 MEASURED and REJECTED (19.09). The audit had said "the coordinator removes only three keys from the environment
    going to the engine, it should also remove the 24 in `GECICI_ORTAM`". Tried: variables like `CADRE_ABORT_SEC`/`CADRE_MODEL_WAIT`
    are how the end-to-end tests speed the run up, and `CADRE_KOSU_TAVAN_USD` is the path RECOMMENDED to the user
    — with all of them removed four tests fell to default waits and timed out.
    Kept behaviour: IDENTITY variables coming from another run's shell are dropped, the settings channel stays."""

    def test_identity_variables_from_another_run_never_reach_the_engine(self):
        import os
        import kosu
        eski = dict(os.environ)
        os.environ.update({'CADRE_WORKTREE': '/eski/wt', 'CADRE_GOREV_ID': 'eski', 'CADRE_OWNER_FDS': '9'})
        try:
            temiz = kosu.motor_ortami()
        finally:
            os.environ.clear(); os.environ.update(eski)
        for anahtar in ('CADRE_WORKTREE', 'CADRE_GOREV_ID', 'CADRE_OWNER_FDS'):
            with self.subTest(anahtar=anahtar):
                self.assertNotIn(anahtar, temiz, f'{anahtar} eski kabuktan motora sizdi')

    def test_the_operator_configuration_channel_survives(self):
        import os
        import kosu
        eski = dict(os.environ)
        os.environ.update({'CADRE_KOSU_TAVAN_USD': '3', 'CADRE_ABORT_SEC': '5', 'E2E_SAHTE': '1'})
        try:
            temiz = kosu.motor_ortami()
            bekci = kosu.kalici_ortam()
        finally:
            os.environ.clear(); os.environ.update(eski)
        self.assertEqual(temiz.get('CADRE_KOSU_TAVAN_USD'), '3', 'kullaniciya onerilen ayar yolu kapatilamaz')
        self.assertEqual(temiz.get('CADRE_ABORT_SEC'), '5')
        self.assertEqual(temiz.get('E2E_SAHTE'), '1', 'motor bu kosunun taklitlerini gormeli')
        self.assertIsNone(bekci.get('CADRE_ABORT_SEC'), 'uzun omurlu bekci kosuya ozel ayari miras almaz')


class GorevModelleriTest(unittest.TestCase):
    def test_atama_kaydi_motor_ici_devri_ve_makbuz_saglayicisini_ayirir(self):
        """D14 (01.10 Codex, F g5-1): the assignment line had only the start model (local); the DS-pro hand-off inside the engine and the budget denial
        were not visible. Tried models come in order from the attempt records, providers with a receipt from the ledger."""
        import datetime, tempfile
        import butce_kapisi as bk
        from kosu import RunStore, atomic_json, gorev_modelleri
        tmp = tempfile.TemporaryDirectory(prefix='gorev-model-')
        self.addCleanup(tmp.cleanup)
        store = RunStore(Path(tmp.name))
        rid = store.accept('p', 'is')['run_id']
        kok = store.task_dir(rid, 'g5-1') / 'attempts'
        for i, (t, m) in enumerate(((1.0, 'opencode-go/glm-5.3'), (3.0, 'opencode-go/glm-5.3'), (2.0, 'opencode/deepseek-v4-pro'))):
            (kok / f'a{i}').mkdir(parents=True)
            atomic_json(kok / f'a{i}/attempt.json', {'created': t, 'model': m})
        simdi = datetime.datetime.now()
        b = {'ts': simdi.isoformat(), 'bugun': simdi.date().isoformat(), 'zen': {'db': 'ok', 'pay': 5.0, 'kullanilabilir': 5.0}}
        defter = bk.BudgetLedger(Path(tmp.name) / 'zincir/request-budget.db')
        defter.settle(defter.reserve('opencode', 10_000, b, {'run_id': rid, 'task_id': 'g5-1'}), 4_000, 'm1')
        defter.reserve('opencode', 10_000, b, {'run_id': rid, 'task_id': 'g9'})
        self.assertEqual(gorev_modelleri(store, rid, 'g5-1'), {
            'denenen_modeller': ['opencode-go/glm-5.3', 'opencode/deepseek-v4-pro'], 'son_model': 'opencode-go/glm-5.3',
            'makbuz_saglayicilari': ['opencode']})


class ButceReddiArizaDegilTest(unittest.TestCase):
    def test_butce_reddiyle_yarim_kalan_serit_saglayici_arizasi_sayilmaz(self):
        """D20 (01.10 Codex): `diger_provider` wrote a 'task partial' failure to the provider on every half lane; a budget denial also
        grew the circuit breaker counter (the B52 principle existed for the plan, not for the lane)."""
        import tempfile
        from kosu import RunStore, atomic_json, butce_reddi_var
        tmp = tempfile.TemporaryDirectory(prefix='butce-reddi-')
        self.addCleanup(tmp.cleanup)
        store = RunStore(Path(tmp.name))
        rid = store.accept('p', 'is')['run_id']
        yol = store.task_dir(rid, 'g1') / 'attempts/a1'
        yol.mkdir(parents=True)
        atomic_json(yol / 'budget-denied.json', {'providers': ['deepseek', 'opencode:free']})
        self.assertTrue(butce_reddi_var(store, rid, 'g1', 'deepseek'))
        self.assertTrue(butce_reddi_var(store, rid, 'g1', 'opencode'))
        self.assertFalse(butce_reddi_var(store, rid, 'g1', 'opencode-go'))
        self.assertFalse(butce_reddi_var(store, rid, 'g2', 'deepseek'))
        self.assertFalse(butce_reddi_var(store, rid, '', 'deepseek'))


class PlanControlTests(unittest.TestCase):
    def setUp(self):
        from test_uctan_uca import E2EFixture
        self.fixture = E2EFixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.home, self.store = self.fixture.home, self.fixture.store
        self.record = self.store.accept('e2e', 'README.md yazim hatasini duzelt')
        self.rid = self.record['run_id']
        self.store.update(self.rid, worktree=str(self.fixture.repo))
        self.plan = {'is_id': 'p1', 'gorevler': [{'id': 'g1', 'tanim': 'README yazimini duzelt',
                     'dosyalar': ['README.md'], 'kabul': 'README metni korunur ve yazim duzelir', 'model': 'auto',
                     'zorluk': 1, 'paralel_grup': 1, 'tahmin_dk': 1, 'tahmin_usd': 0.01, 'bagimli': []}]}
        self.store.save_plan(self.rid, self.plan, 'opencode-go/glm-5.3')
        self.env = {**self.fixture.env, 'CADRE_HOME': str(self.home), 'PYTHONPATH': str(LIB)}

    def cli(self, *args, text=None):
        return subprocess.run([sys.executable, str(LIB / 'kosu.py'), *args], env=self.env,
                              input=text, capture_output=True, text=True, timeout=10)

    def test_cli_journal_is_read_only_and_revisions_preserve_original_plan(self):
        import copy
        before = {p: p.read_bytes() for p in self.store.path(self.rid).rglob('*') if p.is_file()}
        view = json.loads(self.cli('plan-show', self.rid).stdout)
        self.assertEqual(view['revision'], 1)
        self.assertEqual(before, {p: p.read_bytes() for p in self.store.path(self.rid).rglob('*') if p.is_file()})
        self.assertEqual(self.cli('plan-change', self.rid, 'change1', '1', text='Yontemi degistir, kapsami koru').returncode, 0)
        self.assertEqual(self.cli('plan-stop', self.rid, 'change1').returncode, 0)
        new = copy.deepcopy(self.plan)
        new['gorevler'][0]['tanim'] = 'Ayni kabul icin farkli yontem'
        result = self.cli('save-plan', self.rid, 'opencode-go/glm-5.3', '1', text=json.dumps(new))
        self.assertEqual(result.returncode, 0, result.stderr)
        final = self.store.plan_view(self.rid)
        self.assertEqual([e['revision'] for e in final['history']], [1, 2])
        self.assertEqual(final['history'][0], view['history'][0])
        self.assertEqual(final['history'][1]['review']['status'], 'pending')
        self.assertEqual(final['changes'][0]['status'], 'review_pending')
        self.assertEqual(self.cli('run', self.rid, '--devam').returncode, 78)
        self.assertEqual(self.store.read(self.rid)['budget_cap_usd'], self.record['budget_cap_usd'])
        self.assertNotEqual(self.cli('save-plan', self.rid, 'x', '1', text=json.dumps(self.plan)).returncode, 0)
        with self.assertRaises(ValueError):
            self.store.update(self.rid, plan_changes=[])

    def test_same_change_replay_races_once_and_stale_conflicts_do_not_mutate(self):
        command = [sys.executable, str(LIB / 'kosu.py'), 'plan-change', self.rid, 'same', '1']
        processes = [subprocess.Popen(command, env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True) for _ in range(4)]
        for proc in processes:
            out, err = proc.communicate('Ayni yontem degisikligi', timeout=10)
            self.assertEqual(proc.returncode, 0, err)
            self.assertEqual(json.loads(out)['status'], 'stop_requested')
        changes = self.store.plan_view(self.rid)['changes']
        self.assertEqual(len(changes), 1)
        self.assertEqual(len(changes[0]['events']), 1)
        path = self.store.path(self.rid) / 'run.json'
        before = path.read_bytes()
        for ident, revision, text in (('same', '1', 'farkli'), ('other', '1', 'yeni'), ('third', '0', 'eski')):
            self.assertEqual(self.cli('plan-change', self.rid, ident, revision, text=text).returncode, 78)
            self.assertEqual(path.read_bytes(), before)

    def test_failed_change_write_and_failed_mirror_preserve_replayable_history(self):
        from kosu import atomic_json
        with patch('kosu.os.replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                self.store.change_plan(self.rid, 'x', 1, 'Yontem')
        self.assertEqual(self.store.plan_view(self.rid)['changes'], [])
        self.store.change_plan(self.rid, 'x', 1, 'Yontem')
        self.store.stop_plan(self.rid, 'x')
        modified = dict(self.plan, is_id='p2')
        def write(path, value):
            if Path(path).name == 'plan.json':
                raise OSError('mirror failure')
            atomic_json(path, value)
        with patch('kosu.atomic_json', side_effect=write), self.assertRaises(OSError):
            self.store.save_plan(self.rid, modified, expected=1)
        self.assertEqual(self.store.plan_view(self.rid)['revision'], 2)
        self.assertFalse(self.store.plan_view(self.rid)['execution_plan_matches'])
        self.store.save_plan(self.rid, modified, expected=2)
        self.assertEqual(json.loads((self.store.path(self.rid) / 'plan.json').read_text()), modified)
        self.assertEqual(self.store.plan_view(self.rid)['revision'], 2)
        self.assertTrue(self.store.plan_view(self.rid)['execution_plan_matches'])

    def test_stop_requires_owner_and_executor_release_and_withdrawal_keeps_hold(self):
        self.store.change_plan(self.rid, 'x', 1, 'Yontem')
        for path in (self.store.path(self.rid) / '.owner.lock', self.store.task_dir(self.rid, 'g1') / '.opencode-impl.lock'):
            with FileLock(path):
                self.assertEqual(self.cli('plan-stop', self.rid, 'x').returncode, 78)
                self.assertEqual(self.store.plan_view(self.rid)['changes'][0]['status'], 'stop_requested')
        stopped = self.store.stop_plan(self.rid, 'x')
        self.assertEqual(stopped['status'], 'stopped')
        self.assertIn(self.rid, stopped['stop']['products'])
        self.assertEqual(self.store.stop_plan(self.rid, 'x'), stopped)
        self.store.withdraw_plan(self.rid, 'x')
        self.assertEqual(self.cli('run', self.rid, '--devam').returncode, 78)
        from kosu import revive
        with patch('kosu.hayalet_kapat', create=True), patch('kosu._baslat') as start:
            self.assertEqual(revive(self.store, self.fixture.root), [])
            start.assert_not_called()
        self.assertEqual(self.store.plan_view(self.rid)['changes'][0]['status'], 'withdrawn')

    def test_stop_tracks_child_costs_and_retains_unknown_liabilities(self):
        from butce_kapisi import BudgetLedger
        child = self.store.accept('e2e', 'parca', parent_run_id=self.rid)['run_id']
        self.store.update(child, proje_ust=self.rid)
        ledger = BudgetLedger(self.home / 'zincir/request-budget.db')
        with ledger.transaction() as con:
            for ident, rid, actual in (('settled', self.rid, 200000), ('pending', child, None)):
                con.execute('insert into reservation values(?,?,?,?,?,?,?,?)',
                            (ident, 'openrouter', 'today', 1, 300000, actual, ident if actual else None, json.dumps({'run_id': rid})))
        self.store.change_plan(self.rid, 'x', 1, 'Yontem')
        with FileLock(self.store.path(child) / '.owner.lock'):
            self.assertEqual(self.cli('plan-stop', self.rid, 'x').returncode, 78)
        stopped = self.store.stop_plan(self.rid, 'x')
        self.assertEqual(stopped['stop']['cost']['settled_micro'], 200000)
        self.assertEqual(stopped['stop']['cost']['held_micro'], 300000)
        self.assertEqual(set(stopped['stop']['runs']), {self.rid, child})
        with ledger.transaction() as con:
            self.assertIsNone(con.execute("select actual from reservation where id='pending'").fetchone()[0])

    def test_live_coordinator_stops_only_its_own_tree_before_acknowledgement(self):
        import time
        root = self.home / 'stop-engine'
        (root / 'bin').mkdir(parents=True)
        (root / 'bin/is-motoru.sh').write_text('echo started > "$CADRE_RUN_DIR/started"\nsleep 30\n')
        env = {**self.env, 'CADRE_ENGINE_ROOT': str(root), 'CADRE_DISPATCH': '0', 'CADRE_DISK_MIN_MB': '0'}
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(lambda: unrelated.poll() is None and unrelated.terminate())
        self.addCleanup(lambda: unrelated.wait(timeout=5))
        proc = subprocess.Popen([sys.executable, str(LIB / 'kosu.py'), 'run', self.rid], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 8
            while not (self.store.path(self.rid) / 'started').exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue((self.store.path(self.rid) / 'started').exists())
            self.store.change_plan(self.rid, 'x', 1, 'Yontem')
            self.assertEqual(proc.wait(timeout=10), 78)
            self.assertIsNone(unrelated.poll())
            self.assertEqual(self.store.read(self.rid)['reason'], 'plan-change-stop-requested')
            self.assertEqual(self.store.plan_view(self.rid)['changes'][0]['status'], 'stop_requested')
            self.assertEqual(self.store.stop_plan(self.rid, 'x')['status'], 'stopped')
            self.assertFalse((self.store.path(self.rid) / 'cancel.flag').exists())
        finally:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=5)
            unrelated.terminate()
            unrelated.wait(timeout=5)

    def test_launch_boundary_rejects_parent_change_and_marks_attempt_not_started(self):
        from test_kosu_sessions import SessionTests
        import qe_core as core
        fixture = SessionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        parent = fixture.store.accept('a', 'parent')['run_id']
        fixture.store.save_plan(parent, self.plan)
        fixture.store.update(fixture.rid, proje_ust=parent)
        fixture.store.change_plan(parent, 'x', 1, 'Yontem')
        with self.assertRaises(ValueError):
            core.run_opencode(str(fixture.fake), str(fixture.wt), ['task'], fixture.log)
        self.assertFalse((fixture.home / 'calls.jsonl').exists())
        attempts = list(fixture.store.task_dir(fixture.rid, 'g1').glob('attempts/*/attempt.json'))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(json.loads(attempts[0].read_text())['outcome'], 'not_started')

    def test_terminal_corrupt_and_sensitive_sources_never_acknowledge_stop(self):
        from kosu import atomic_json
        self.store.change_plan(self.rid, 'x', 1, 'Yontem')
        (self.fixture.repo / '.env').write_text('synthetic-sensitive-fixture')
        with self.assertRaisesRegex(ValueError, 'protected-source'):
            self.store.stop_plan(self.rid, 'x')
        (self.fixture.repo / '.env').unlink()
        db = self.home / 'zincir/request-budget.db'
        db.write_text('corrupt fixture ledger')
        self.assertEqual(self.cli('plan-stop', self.rid, 'x').returncode, 78)
        db.unlink()
        self.store.update(self.rid, state='completed')
        self.assertEqual(self.cli('plan-stop', self.rid, 'x').returncode, 78)
        self.assertEqual(self.store.read(self.rid)['state'], 'completed')
        record = self.store.read(self.rid)
        record['plan_history'][0]['plan']['is_id'] = 'corrupt'
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        self.assertEqual(self.cli('plan-show', self.rid).returncode, 78)


class PlanReviewTests(unittest.TestCase):
    def setUp(self):
        from test_kosu_sessions import SessionTests
        from test_uctan_uca import E2EFixture
        self.source = E2EFixture()
        self.source.setUp()
        self.addCleanup(self.source.doCleanups)
        self.fixture = SessionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store, self.rid, self.home = self.fixture.store, self.fixture.rid, self.fixture.home
        self.store.update(self.rid, worktree=str(self.source.repo))
        self.plan = {'is_id': 'p1', 'gorevler': [{'id': 'g1', 'tanim': 'README yazimini duzelt', 'dosyalar': ['README.md'],
                     'kabul': 'README metni korunur', 'model': 'auto', 'zorluk': 1, 'paralel_grup': 1, 'tahmin_dk': 1, 'bagimli': []}]}
        self.store.save_plan(self.rid, self.plan)
        import qe_core as core
        self.fixture.enter(patch.object(core.YON, 'KAYIT', self.home / 'zincir/yonlendirici.jsonl'))
        self.fixture.enter(patch.object(core.YON, 'DURUM', self.home / 'zincir/state.json'))
        self.fixture.enter(patch.object(core.YON, 'butce_oku', side_effect=core.butce_json))
        self.fixture.fake.write_text('#!' + sys.executable + '\n' + r'''
import json,os,pathlib,sys
args=sys.argv[1:]; home=pathlib.Path(os.environ['CADRE_HOME'])
question=json.loads(args[-1].splitlines()[-1]); mode=(home/'mode').read_text() if (home/'mode').exists() else ''
sid='ses_'+os.environ['CADRE_GOREV_ID']
(home/'review-config.json').write_text(os.environ['OPENCODE_CONFIG_CONTENT'])
if mode=='write': pathlib.Path('README.md').write_text('unauthorized fixture write')
if mode=='cancel': (pathlib.Path(os.environ['CADRE_RUN_DIR'])/'cancel.flag').write_text('fixture cancel')
acceptance=[{'id':c['id'],'status':'PASS','evidence':'README.md source and g1 acceptance were examined'} for c in question['criteria']]
if mode=='missing': acceptance=acceptance[:-1]
if mode=='reject': acceptance[0]['status']='FAIL'
answer={'revision':question['revision'] if mode!='wrong' else '0'*64,'verdict':'GECER','acceptance':acceptance}
print(json.dumps({'type':'step_start','sessionID':sid,'part':{'type':'step-start'}}),flush=True)
print(json.dumps({'type':'text','sessionID':sid,'part':{'type':'text','text':json.dumps(answer)}}),flush=True)
''')

    def review(self, revision=1):
        from kosu import review_plan
        return review_plan(self.store, self.rid, revision, str(self.fixture.fake))

    def test_real_executor_uses_clean_read_only_review_and_withdrawn_plan_needs_resume_decision(self):
        from kosu import plan_hold
        first = self.review()
        self.assertEqual(first['status'], 'PASS', first)
        self.store.change_plan(self.rid, 'x', 1, 'Yontem')
        self.store.stop_plan(self.rid, 'x')
        self.store.withdraw_plan(self.rid, 'x')
        second = self.review()
        self.assertEqual(second['status'], 'PASS', second)
        self.assertNotEqual(first['session_id'], second['session_id'])
        self.assertNotEqual(first['attempt_id'], second['attempt_id'])
        self.assertTrue(plan_hold(self.store.read(self.rid)))
        config = json.loads((self.home / 'review-config.json').read_text())
        self.assertEqual(config['permission']['edit'], 'deny')
        self.assertEqual(config['permission']['bash'], 'allow')
        self.assertEqual(config['permission']['external_directory'], 'deny')
        self.assertEqual(config['permission']['read'], 'deny')
        self.assertEqual(config['agent']['build']['permission']['edit'], 'deny')
        policies = list(self.store.path(self.rid).glob('tasks/*/attempts/*/tool-policy.json'))
        self.assertEqual(len(policies), 2)
        for path in policies:
            policy = json.loads(path.read_text())
            self.assertFalse(policy['writable'])
            denied = subprocess.run([policy['sandbox'], '-p', policy['profile'], sys.executable, '-c',
                "from pathlib import Path; Path('README.md').write_text('unauthorized')"],
                cwd=self.source.repo, capture_output=True, timeout=5)
            self.assertNotEqual(denied.returncode, 0)
        change = self.store.resume_plan(self.rid, 'x', 1)
        self.assertEqual(change['status'], 'resumed')
        self.assertFalse(plan_hold(self.store.read(self.rid)))
        self.assertEqual(self.store.resume_plan(self.rid, 'x', 1), change)
        self.assertEqual(len(self.store.plan_view(self.rid)['reviews']), 2)

    def test_revised_plan_rejects_missing_wrong_failed_and_mutating_reviews(self):
        self.store.change_plan(self.rid, 'x', 1, 'Yontem')
        self.store.stop_plan(self.rid, 'x')
        for mode in ('missing', 'wrong', 'reject', 'write'):
            with self.subTest(mode=mode):
                view = self.store.plan_view(self.rid)
                plan = dict(self.plan, is_id='plan' + str(view['revision'] + 1))
                self.store.save_plan(self.rid, plan, expected=view['revision'])
                (self.home / 'mode').write_text(mode)
                review = self.review(view['revision'] + 1)
                self.assertNotEqual(review['status'], 'PASS', review)
                self.assertEqual(self.store.plan_view(self.rid)['changes'][-1]['status'], 'rejected')
                with self.assertRaises(ValueError):
                    self.store.resume_plan(self.rid, 'x', view['revision'] + 1)
        self.assertFalse((self.store.path(self.rid) / 'cancel.flag').exists())

    def test_source_or_budget_change_blocks_resume_and_cancel_invalidates_review(self):
        self.store.change_plan(self.rid, 'x', 1, 'Yontem')
        self.store.stop_plan(self.rid, 'x')
        review = self.review()
        self.assertEqual(review['status'], 'PASS', review)
        source = self.source.repo / 'README.md'
        original = source.read_bytes()
        source.write_text('changed after review')
        with self.assertRaisesRegex(ValueError, 'source-changed'):
            self.store.resume_plan(self.rid, 'x', 1)
        source.write_bytes(original)
        self.store.update(self.rid, budget_cap_usd=0)
        with self.assertRaisesRegex(ValueError, 'cap-exhausted'):
            self.store.resume_plan(self.rid, 'x', 1)
        self.store.update(self.rid, budget_cap_usd=2)
        (self.home / 'mode').write_text('cancel')
        self.assertEqual(self.review()['status'], 'ERROR')
        self.assertTrue((self.store.path(self.rid) / 'cancel.flag').exists())

    def test_no_capacity_and_busy_owner_start_no_review_executor(self):
        import qe_core as core
        with FileLock(self.store.path(self.rid) / '.owner.lock'), self.assertRaisesRegex(ValueError, 'owner-active'):
            self.review()
        with patch.object(core.YON, 'zincir', return_value=([], ['fixture no capacity'])):
            self.assertEqual(self.review()['status'], 'ERROR')
        self.assertFalse((self.home / 'review-config.json').exists())
        self.assertIsNone(self.store.read(self.rid)['plan_review_claim'])
        self.assertEqual(len(self.store.plan_view(self.rid)['reviews']), 1)
        self.store.update(self.rid, budget_cap_usd=0)
        self.assertEqual(self.review()['reason'], 'operator-cap-exhausted')
        self.assertFalse((self.home / 'review-config.json').exists())

    def test_execution_plan_divergence_blocks_review_start_resume_and_launch(self):
        from kosu import atomic_json, launch_guard, start_run
        self.store.change_plan(self.rid, 'x', 1, 'Method')
        self.store.stop_plan(self.rid, 'x')
        self.store.withdraw_plan(self.rid, 'x')
        self.assertEqual(self.review()['status'], 'PASS')
        atomic_json(self.store.path(self.rid) / 'plan.json', dict(self.plan, is_id='unreviewed'))
        self.assertFalse(self.store.plan_view(self.rid)['execution_plan_matches'])
        self.assertEqual(self.review()['reason'], 'execution-plan-mismatch')
        with self.assertRaisesRegex(ValueError, 'execution-plan-mismatch'):
            self.store.resume_plan(self.rid, 'x', 1)
        self.store.save_plan(self.rid, self.plan, expected=1)
        self.store.resume_plan(self.rid, 'x', 1)
        atomic_json(self.store.path(self.rid) / 'plan.json', dict(self.plan, is_id='unreviewed'))
        with self.assertRaisesRegex(ValueError, 'execution-plan-mismatch'):
            start_run(self.store, self.rid)
        with self.assertRaisesRegex(ValueError, 'execution-plan-mismatch'):
            with launch_guard(self.store, self.rid):
                self.fail('an unreviewed execution plan was launched')


class PlanInterruptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cadre-plan-interruption-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = RunStore(self.home)
        self.rid = self.store.accept('lab', 'small task')['run_id']
        self.plan = {'is_id': 'p', 'gorevler': [{'id': 'g1', 'tanim': 'small task', 'dosyalar': ['README.md'],
                     'kabul': 'README checked', 'model': 'auto', 'zorluk': 1, 'paralel_grup': 1, 'tahmin_dk': 1}]}
        self.store.save_plan(self.rid, self.plan)
        self.env = {**os.environ, 'CADRE_HOME': str(self.home), 'PYTHONPATH': str(LIB)}

    def test_kill_before_change_publication_preserves_original_and_replay(self):
        import signal
        record_path = self.store.path(self.rid) / 'run.json'
        before = record_path.read_bytes()
        code = 'import kosu,sys,time; kosu.os.replace=lambda *a: (print("ready",flush=True),time.sleep(30)); kosu.RunStore(sys.argv[1]).change_plan(sys.argv[2],"x",1,"method")'
        with subprocess.Popen([sys.executable, '-c', code, str(self.home), self.rid], env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
            self.assertEqual(proc.stdout.readline().strip(), 'ready')
            proc.send_signal(signal.SIGKILL)
            proc.communicate(timeout=5)
        self.assertEqual(record_path.read_bytes(), before)
        self.assertEqual(self.store.change_plan(self.rid, 'x', 1, 'method')['status'], 'stop_requested')
        self.assertEqual(len(self.store.plan_view(self.rid)['changes']), 1)

    def test_detached_orphan_lease_prevents_false_stop_and_interrupted_review_is_preserved(self):
        import signal,time
        from kosu import atomic_json
        self.store.change_plan(self.rid, 'x', 1, 'method')
        code = ('from kosu import FileLock; import subprocess,sys; lock=FileLock(sys.argv[1]); lock.acquire(); '
                'proc=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"],pass_fds=(lock.fd,),'
                'start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); print(proc.pid)')
        result = subprocess.run([sys.executable, '-c', code, str(self.store.path(self.rid) / '.owner.lock')],
                                env=self.env, capture_output=True, text=True, check=True, timeout=5)
        pid = int(result.stdout.strip())
        try:
            with self.assertRaisesRegex(ValueError, 'owner-active'):
                self.store.stop_plan(self.rid, 'x')
        finally:
            os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            lock = FileLock(self.store.path(self.rid) / '.owner.lock')
            if lock.acquire(blocking=False):
                lock.close()
                break
            time.sleep(.05)
        record = self.store.read(self.rid)
        record['plan_review_claim'] = {'id': 'interrupted', 'revision': 1, 'task_id': 'plan-review-1-dead'}
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        self.store.stop_plan(self.rid, 'x')
        self.assertIsNone(self.store.read(self.rid)['plan_review_claim'])
        self.assertEqual(self.store.plan_view(self.rid)['reviews'][-1]['reason'], 'interrupted-review')
        self.store.save_plan(self.rid, dict(self.plan, is_id='new'), expected=1)
        self.assertEqual(self.store.plan_view(self.rid)['revision'], 2)


class OperationStartTests(unittest.TestCase):
    def test_start_and_spending_approval_replay_do_not_bypass_plan_or_cap(self):
        from kosu import approve_spending, start_run
        import time
        with tempfile.TemporaryDirectory(prefix='cadre-start-') as directory:
            store = RunStore(Path(directory))
            rid = store.accept('lab', 'small task')['run_id']
            real_run = subprocess.run
            calls = []
            def transport(command, **kwargs):
                if command[:2] == ['bash', '-c']:
                    calls.append(command)
                    return subprocess.CompletedProcess(command, 0, '424242\n', '')
                return real_run(command, **kwargs)
            with patch('kosu.subprocess.run', side_effect=transport):
                self.assertEqual(start_run(store, rid)['status'], 'start-requested')
                with patch('kosu.pid_yasiyor', return_value=True), patch('kosu.surec_komutu', return_value='kosu.py run ' + rid):
                    self.assertEqual(start_run(store, rid)['status'], 'already-starting')
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][-3:], ['run', rid, '--devam'])
            store.update(rid, harcama_alarmi={'ts': time.time()}, retry_after=time.time() + 3600)
            first = approve_spending(store, rid)
            second = approve_spending(store, rid)
            self.assertEqual(first['harcama_onayi_ts'], second['harcama_onayi_ts'])
            self.assertEqual(first['retry_after'], second['retry_after'])
            self.assertEqual(first['budget_cap_usd'], second['budget_cap_usd'])
            child = store.accept('child', 'owned child')['run_id']
            alarm_time = first['harcama_onayi_ts'] + 1
            store.update(child, proje_ust=rid, harcama_alarmi={'ts': alarm_time}, retry_after=alarm_time + 3600)
            store.update(rid, project_active_run=child)
            from kosu import atomic_json
            def interrupted_write(path, value):
                if Path(path) == store.path(child) / 'run.json':
                    raise OSError('interrupted child approval')
                atomic_json(path, value)
            with patch('kosu.time.time', return_value=alarm_time + 1), patch('kosu.atomic_json', side_effect=interrupted_write):
                with self.assertRaises(OSError):
                    approve_spending(store, rid)
            saved_approval = store.read(rid)['harcama_onayi_ts']
            self.assertGreater(saved_approval, first['harcama_onayi_ts'])
            self.assertNotIn('harcama_onayi_ts', store.read(child))
            replay = approve_spending(store, rid)
            self.assertEqual(replay['harcama_onayi_ts'], saved_approval)
            self.assertEqual(store.read(child)['harcama_onayi_ts'], saved_approval)
            self.assertEqual(store.read(child)['retry_after'], alarm_time + 3600)
            self.assertEqual(approve_spending(store, rid)['harcama_onayi_ts'], saved_approval)
            store.update(rid, budget_cap_usd=0)
            with self.assertRaisesRegex(ValueError, 'cap-exhausted'):
                start_run(store, rid)
            plan = {'is_id': 'p', 'gorevler': [{'id': 'g1', 'tanim': 'small task', 'dosyalar': ['README.md'],
                     'kabul': 'README checked', 'model': 'auto', 'zorluk': 1, 'paralel_grup': 1, 'tahmin_dk': 1}]}
            store.save_plan(rid, plan)
            store.change_plan(rid, 'x', 1, 'method')
            for operation in (lambda: start_run(store, rid), lambda: approve_spending(store, rid)):
                with self.assertRaises(ValueError):
                    operation()


class OperationErrorTests(unittest.TestCase):
    """N-OBS03 (audit B-02/C-K1): the approval chain's kebab reason codes show up as codes in the CLI/panel instead of a bare `ValueError`;
    free-text errors keep their type name."""

    def test_kebab_reason_codes_are_shown_as_is_and_free_text_falls_back_to_the_type(self):
        from kosu import operation_error
        for kod in ('final-review-no-capacity', 'request-proposal-changed', 'request-second-approval-required', 'qa-command-changed'):
            self.assertEqual(operation_error(ValueError(kod)), kod)
        self.assertEqual(operation_error(PermissionError('git-transfer-source-dirty')), 'git-transfer-source-dirty')
        self.assertEqual(operation_error(ValueError('Expecting value: line 1 column 1 (char 0)')), 'ValueError')
        self.assertEqual(operation_error(KeyError('x')), 'KeyError')
        self.assertEqual(operation_error(ValueError('')), 'ValueError')
