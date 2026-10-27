"""TASARIM-5 speed-up (user approval 24.09 17:xx), decision (1): hand-off to the cloud BUT budget-protected — when a long local attempt
or QA repair exceeds the threshold, a cloud model that passes the budget gate continues the same job (B218). Without a budgeted cloud it stays local.
Real engine / real QA gate, fake executor; no model/network."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import qe_core as core  # noqa: E402
import test_kosu_sessions as fixtures  # noqa: E402
from test_gece_senaryolar import butce, zincir_sahte  # noqa: E402


def setUpModule():
    ortam = patch.dict(os.environ, {'CADRE_ZINCIR_KALITE': 'opencode-go/qwen3.8-max'})
    ortam.start()
    unittest.addModuleCleanup(ortam.stop)


SAHTE_YURUTUCU = """
import json, os, pathlib, sys, time
args = sys.argv[1:]
sid = args[args.index('--session') + 1] if '--session' in args else 'ses_t'
print(json.dumps({'type': 'step_start', 'sessionID': sid, 'part': {'type': 'step-start'}}), flush=True)
with (pathlib.Path(os.environ['CADRE_HOME']) / 'cagrilar').open('a') as f:
    f.write(' '.join(args[:-2]) + '\\n')
print(json.dumps({'type': 'text', 'sessionID': sid, 'part': {'type': 'text', 'text': 'bitti'}}), flush=True)
"""


class QaOnarimDevirTest(unittest.TestCase):
    """CANLI1/P3: the fixed QA hand-off time was removed; the repair is done directly with the model."""
    def setUp(self):
        import test_sonuc
        self.f = test_sonuc.CompletionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        from qa_fixture import command
        Path(self.f.run['constraints']['qa-komut']).write_text(command('test -f duzeldi.txt'))

    def onarim(self, bulut):
        cagrilar = []

        def calistir(real, wt, args, logf, env_ek=None, role='impl'):
            cagrilar.append(list(args))
            if '-m' in args:
                return subprocess.Popen([sys.executable, '-c', f"open({str(Path(wt) / 'duzeldi.txt')!r}, 'w').write('x')"])
            return subprocess.Popen(['sleep', '5'])
        with patch.object(core, 'run_opencode', side_effect=calistir), patch.object(core, 'bulut_aday', return_value=bulut):
            sonuc = core.qa_kapisi('a', str(self.f.wt), self.f.log, '/fake/cli')
        return sonuc, cagrilar

    def test_cr51_green_local_qa_and_its_rerun_make_no_model_call(self):
        (Path(self.f.wt) / 'duzeldi.txt').write_text('x')
        cagrilar = []
        with patch.object(core, 'run_opencode', side_effect=lambda *a, **k: cagrilar.append(a)), \
             patch.object(core, 'bulut_aday', return_value='deepseek/deepseek-v4-pro'):
            ilk = core.qa_kapisi('a', str(self.f.wt), self.f.log, '/fake/cli')
            tekrar = core.qa_kapisi('a', str(self.f.wt), self.f.log, '/fake/cli')
        self.assertEqual((ilk['status'], tekrar['status']), ('PASS', 'PASS'))
        self.assertEqual(cagrilar, [], 'test/lint/sayac yerel yoldur; gizli ucretli cagri yok')

    def test_onarim_dogrudan_bulutta_baslar(self):
        """CANLI1: QA repair goes directly to the model, no hand-off."""
        sonuc, cagrilar = self.onarim('deepseek/deepseek-v4-pro')
        self.assertEqual(sonuc['status'], 'PASS')
        self.assertEqual(cagrilar[0][cagrilar[0].index('-m') + 1], 'deepseek/deepseek-v4-pro')
        self.assertIn('QA ONARIM: basliyor', Path(self.f.log).read_text())


class SureSonuTest(unittest.TestCase):
    """CR-48 (N-TS01): a helper call and a lane carry the remaining wall time to the provider gate; time measurement is not a paid call."""
    def test_confirmed_plan_stops_freeze_lane_time_without_resetting_its_initial_limit(self):
        import tempfile
        from motor import sure_kaydi
        with tempfile.TemporaryDirectory(prefix='sure-durus-') as ev:
            store = core.RunStore(Path(ev))
            rid = store.accept('a', 'bounded work')['run_id']
            def stopped(start, end, runs=None):
                return {'status': 'resumed', 'stop': {'ts': start, 'runs': [rid] if runs is None else runs},
                        'events': [{'action': 'resumed', 'ts': end}]}
            cases = [('normal', [], 0), ('verified', [stopped(1200, 1700)], 500),
                     ('overlap-once', [stopped(1200, 1700), stopped(1400, 1800)], 600),
                     ('before-lane', [stopped(500, 900)], 0),
                     ('other-run', [stopped(1200, 1700, ['another'])], 0),
                     ('requested-only', [{'status': 'stop_requested', 'created': 1200}], 0)]
            for name, changes, pause in cases:
                with self.subTest(case=name), patch.object(core.time, 'time', return_value=2000), \
                        patch.object(core.time, 'monotonic', return_value=10):
                    core.atomic_json(store.path(rid) / 'run.json', {**store.read(rid), 'plan_changes': changes})
                    store.task_update(rid, 'g1', motor_clock={'started': 1000, 'warning_seconds': 600, 'limit_seconds': 900})
                    warning, deadline, clock = sure_kaydi(store, rid, 'g1', 5000, 5000)
                    self.assertEqual((warning, deadline), (10 + max(0, pause - 400), 10 + max(0, pause - 100)))
                    self.assertEqual((clock['started'], clock['limit_seconds'], clock['warning_seconds']), (1000, 900, 600))
            with patch.object(core.time, 'time', return_value=2000), patch.object(core.time, 'monotonic', return_value=10):
                for invalid in (stopped(1200, 2200), stopped(1200, 1700, rid), {'status': 'resumed', 'stop': 'broken'}):
                    core.atomic_json(store.path(rid) / 'run.json', {**store.read(rid), 'plan_changes': [invalid]})
                    with self.assertRaisesRegex(ValueError, 'motor-clock-pause-invalid'):
                        sure_kaydi(store, rid, 'g1', 600, 900)

    def test_live_request_work_is_a_conservative_bound_when_legacy_lane_has_no_activity_offset(self):
        import tempfile
        from motor import sure_kaydi
        with tempfile.TemporaryDirectory(prefix='sure-aktif-') as ev:
            store = core.RunStore(Path(ev))
            rid = store.accept('a', 'bounded work')['run_id']
            store.task_update(rid, 'g1', motor_clock={'started': 1000, 'warning_seconds': 600, 'limit_seconds': 900})
            core.atomic_json(store.path(rid) / 'run.json', {**store.read(rid), 'request': {'duration_minutes': 40},
                'request_clock': {'aktif_sn': 500, 'oturum': 1990}})
            with patch.object(core.time, 'time', return_value=2000), patch.object(core.time, 'monotonic', return_value=10):
                warning, deadline, clock = sure_kaydi(store, rid, 'g1', 5000, 5000)
                self.assertEqual((warning, deadline), (100, 400))
                self.assertEqual((clock['started'], clock['limit_seconds']), (1000, 900))
                core.atomic_json(store.path(rid) / 'run.json', {**store.read(rid), 'project_deadline': 2050})
                self.assertEqual(sure_kaydi(store, rid, 'g1', 600, 900)[1], 60)
                record = store.read(rid)
                record.pop('project_deadline')
                core.atomic_json(store.path(rid) / 'run.json', record)
            with patch.object(core.time, 'time', return_value=2390), patch.object(core.time, 'monotonic', return_value=10):
                self.assertEqual(sure_kaydi(store, rid, 'g1', 600, 900)[1], 10)

    def test_parent_stop_freezes_child_lane_but_cannot_extend_request_or_project_deadline(self):
        import tempfile
        from motor import sure_kaydi
        with tempfile.TemporaryDirectory(prefix='sure-ust-') as ev:
            store = core.RunStore(Path(ev))
            parent = store.accept('a', 'parent')['run_id']
            child = store.accept('a', 'child')['run_id']
            store.update(child, proje_ust=parent)
            store.task_update(child, 'g1', motor_clock={'started': 1000, 'warning_seconds': 600, 'limit_seconds': 900})
            change = {'status': 'resumed', 'stop': {'ts': 1200, 'runs': [parent, child]},
                      'events': [{'action': 'resumed', 'ts': 1700}]}
            core.atomic_json(store.path(parent) / 'run.json', {**store.read(parent), 'plan_changes': [change],
                'project_deadline': 2050, 'request': {'duration_minutes': 1},
                'request_clock': {'aktif_sn': 20, 'oturum': None}})
            with patch.object(core.time, 'time', return_value=2000), patch.object(core.time, 'monotonic', return_value=10):
                self.assertEqual(sure_kaydi(store, child, 'g1', 600, 900)[1], 50)
                store.update(parent, request_clock={'aktif_sn': 0, 'oturum': None})
                self.assertEqual(sure_kaydi(store, child, 'g1', 600, 900)[1], 60)
                store.update(parent, request_clock={'aktif_sn': 60, 'oturum': None})
                self.assertEqual(sure_kaydi(store, child, 'g1', 600, 900)[1], 10)

    def test_capture_passes_each_attempts_wall_clock_deadline_to_the_gate(self):
        import tempfile
        import time
        with tempfile.TemporaryDirectory(prefix='sure-') as ev:
            seen = []

            def calistir(real, wt, args, logf, env_ek=None, role='impl'):
                seen.append(dict(env_ek or {}))
                return subprocess.Popen([sys.executable, '-c', 'pass'])
            with patch.object(core, 'QE', Path(ev)), patch.dict(os.environ, {'CADRE_RUN_ID': '', 'CADRE_HOME': ev}), \
                 patch.object(core, 'run_opencode', side_effect=calistir):
                once = time.time()
                core.capture_opencode('/fake', ev, ['-m', 'opencode-go/glm-5.3', 'x'], 120, role='plan')
                sonra = time.time()
        self.assertEqual(len(seen), 1)
        self.assertTrue(once + 120 <= float(seen[0]['CADRE_SURE_SON_TS']) <= sonra + 120, seen[0])

    def test_motor_lane_gets_the_same_deadline_it_enforces(self):
        import time
        fixture = fixtures.SessionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.fake.write_text(fixture.fake.read_text()
                                + "\n(p / 'sure-son.txt').write_text(os.environ.get('CADRE_SURE_SON_TS', 'YOK'))\n")
        once = time.time()
        fixture.motor_fixture(env={'CADRE_WARN_SEC': '300', 'CADRE_ABORT_SEC': '777'})
        gorulen = (fixture.home / 'sure-son.txt').read_text()
        self.assertNotEqual(gorulen, 'YOK', 'gercek sahte yurutucu sureci son zamani ortamindan almali')
        saat = fixture.store.read(fixture.rid)['tasks']['g1']['motor_clock']
        self.assertEqual(saat['limit_seconds'], 777)
        self.assertAlmostEqual(float(gorulen), saat['started'] + saat['limit_seconds'], delta=2,
                               msg='serit kapisinin gordugu son zaman motorun uyguladigi kesin sonla ayni')
        self.assertLessEqual(once + 777 - 2, float(gorulen))
        self.assertTrue(once + 30 <= float(core.sure_sonu(30)) <= time.time() + 30)
        self.assertTrue(once <= float(core.sure_sonu(-5)) <= time.time(), 'gecmis son tarih kalan sure sifir demektir')


class YurutucuSlotuTest(unittest.TestCase):
    """TASARIM-5(2) + G-F1: the local executor goes through one slot serially; the cloud executor does not wait for the local slot."""
    def setUp(self):
        import tempfile
        t = tempfile.TemporaryDirectory(prefix='slot-')
        self.addCleanup(t.cleanup)
        self.home = Path(t.name)
        self.gercek = self.home / 'opencode'
        self.gercek.write_text('#!/bin/sh\nsleep 2\n')
        self.gercek.chmod(0o700)
        for ctx in (patch.object(core, 'QE', self.home), patch.dict(os.environ, {'CADRE_RUN_ID': '', 'CADRE_SLOT_POLL': '0.1'}),
                    patch.object(core, 'opencode_budget_env', side_effect=lambda env, args: list(args))):
            ctx.__enter__()
            self.addCleanup(ctx.__exit__, None, None, None)

    def ikinci_bekleme(self, model):
        import time
        ilk = core.run_opencode(str(self.gercek), str(self.home), ['-m', model, 'x'], self.home / 'log')
        self.addCleanup(ilk.wait)
        bas = time.monotonic()
        ikinci = core.run_opencode(str(self.gercek), str(self.home), ['-m', model, 'x'], self.home / 'log')
        self.addCleanup(ikinci.wait)
        return time.monotonic() - bas

    def test_bulut_yurutuculeri_eszamanli(self):
        self.assertLess(self.ikinci_bekleme('deepseek/deepseek-v4-flash'), 1.0)


class ParalelSecimTest(unittest.TestCase):
    """TASARIM-5(2): at the same time only independent (different paralel_grup, no dependencies, disjoint files) cloud lanes."""
    def test_yalniz_bagimsiz_bulut_seritleri_birlikte_secilir(self):
        import tempfile
        from kosu import RunStore
        with tempfile.TemporaryDirectory(prefix='paralel-') as ev:
            store = RunStore(Path(ev))
            rid = store.accept('a', 'is')['run_id']
            g = lambda i, grp, dosya, model='ds-pro', bagimli=(): {'id': f'g{i}', 'tanim': 't', 'kabul': 'k', 'model': model, 'zorluk': 3,
                                                                  'paralel_grup': grp, 'tahmin_dk': 5, 'dosyalar': [dosya], 'bagimli': list(bagimli)}
            plan = {'is_id': 'x', 'gorevler': [g(1, 1, 'src/a.py'), g(2, 2, 'src/b.py'), g(3, 1, 'src/c.py'), g(4, 3, 'src/d.py', bagimli=['g1']),
                                               g(5, 0, 'src/e.py', model='auto'), g(6, 4, 'src'), g(7, 5, 'docs/x.md')]}
            env = {**os.environ, **store.environment(rid)}
            kos = lambda gid, n: subprocess.run([sys.executable, str(LIB / 'dispatch_util.py'), 'paralel', gid, str(n)], input=json.dumps(plan),
                                                text=True, capture_output=True, env=env, timeout=20).stdout.split()
            # B340 (03.10): the empty-dependencies + disjoint-files rule instead of the paralel_grup field
            self.assertEqual(kos('g1', 3), ['g2', 'g3'])
            self.assertEqual(kos('g1', 1), [])
            self.assertEqual(kos('g5', 3), ['g7'], '06.10: auto serit de buluttur; dosyasi ayrik g7 paralel')
            store.task_update(rid, 'g1', dispatch_result='uretti')
            self.assertEqual(kos('g2', 3), ['g3', 'g4'])

    def test_real_file_aliases_and_new_names_follow_filesystem_identity(self):
        import tempfile
        import dispatch_util as du
        with tempfile.TemporaryDirectory(prefix='path-identity-') as directory:
            root = Path(directory)
            (root / 'Src').mkdir()
            file = root / 'Src' / 'Main.py'
            file.write_text('original')
            (root / 'Other').mkdir()
            os.link(file, root / 'Other' / 'hard.py')
            sensitive = not (root / 'src').exists()
            cases = [('Src/Main.py', 'Other/hard.py', False), ('Src', 'Src/new.py', False),
                     ('Src/Main.py', 'Src/other.py', True), ('Src/new.py', 'Src/NEW.py', sensitive),
                     ('Src/New/a.py', 'Src/new/A.py', sensitive), ('Src/Main.py', 'src/main.py', sensitive),
                     ('Src', 'src/new.py', sensitive)]
            for left, right, parallel in cases:
                with self.subTest(left=left, right=right):
                    tasks = [{'id': gid, 'dosyalar': [name], 'bagimli': [], 'model': 'auto'}
                             for gid, name in (('g1', left), ('g2', right))]
                    plan = {'gorevler': tasks}
                    self.assertEqual(du.paralel_adaylar(plan, 'g1', 2, {}, root=root), ['g2'] if parallel else [])
            self.assertEqual(file.read_text(), 'original')
            self.assertFalse((root / 'Src/new.py').exists())
            self.assertFalse((root / 'Src/New').exists())
            if sys.platform == 'darwin':
                plan = {'gorevler': [{'id': gid, 'dosyalar': [name], 'bagimli': [], 'model': 'auto'}
                                     for gid, name in (('g1', 'new.py'), ('g2', 'NEW.py'))]}
                for policy, expected in ((0, []), (1, ['g2']), (-1, [])):
                    with self.subTest(policy=policy), patch.object(du.os, 'pathconf', return_value=policy):
                        self.assertEqual(du.paralel_adaylar(plan, 'g1', 2, {}, root=root), expected)

    def test_parallel_selection_uses_recorded_worktree_and_unknown_paths_stay_serial(self):
        import tempfile
        import dispatch_util as du
        from kosu import RunStore
        with tempfile.TemporaryDirectory(prefix='path-cli-') as directory:
            root = Path(directory)
            store = RunStore(root / 'home')
            rid = store.accept('a', 'work')['run_id']
            wt = root / 'wt'
            wt.mkdir()
            (wt / 'one.py').write_text('x')
            os.link(wt / 'one.py', wt / 'two.py')
            store.update(rid, worktree=str(wt))
            plan = {'gorevler': [{'id': 'g1', 'dosyalar': ['one.py'], 'model': 'auto'},
                                {'id': 'g2', 'dosyalar': ['two.py'], 'model': 'auto'}]}
            result = subprocess.run([sys.executable, str(LIB / 'dispatch_util.py'), 'paralel', 'g1', '2'],
                                    input=json.dumps(plan), text=True, capture_output=True,
                                    env={**os.environ, **store.environment(rid)}, cwd=root, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), '')
            plan['gorevler'][1]['dosyalar'] = ['new.py']
            with patch.object(du, 'path_identity', side_effect=OSError('unverifiable filesystem')):
                self.assertEqual(du.paralel_adaylar(plan, 'g1', 2, {}, root=wt), [])
            (wt / 'linked').symlink_to(root, target_is_directory=True)
            plan['gorevler'][1]['dosyalar'] = ['linked/escape.py']
            self.assertEqual(du.paralel_adaylar(plan, 'g1', 2, {}, root=wt), [])


PARALEL_ISCI = """
import json, os, pathlib, sys, time
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
from kosu import RunStore
gid = os.environ['CADRE_GOREV_ID']; wt = pathlib.Path(os.environ['CADRE_WORKTREE'])
bas = time.time(); time.sleep(float(os.environ.get('FIXTURE_SURE', '1')))
(wt / (gid + '.txt')).write_text(gid + '\\n')
with open(os.environ['FIXTURE_ZAMAN'], 'a') as f:
    f.write(json.dumps({'gid': gid, 'model': os.environ.get('CADRE_RUN_MODEL') or 'yerel', 'bas': bas, 'son': time.time()}) + '\\n')
RunStore().task_update(os.environ['CADRE_RUN_ID'], gid, state='uretti')
if gid == 'birlestirme':
    sayac = pathlib.Path(os.environ['FIXTURE_ZAMAN'] + '.birlestirme')
    n = int(sayac.read_text()) if sayac.exists() else 0
    sayac.write_text(str(n + 1))
    durum = 'yarim' if n < int(os.environ.get('FIXTURE_BIRLESTIRME_YARIM', '0')) else 'uretti'
    (RunStore().task_dir(os.environ['CADRE_RUN_ID'], gid) / 'status.txt').write_text(durum + '\\n')
"""

GECER_HAKEM = """
import os, sys, json
q = json.loads(sys.argv[-1].splitlines()[-1])
a = {'verdict': 'GECER', 'revision': q['revision'], 'acceptance': [{'id': i['id'], 'status': 'PASS', 'evidence': 'dosyalar okundu'} for i in q['criteria']]}
print(json.dumps({'type': 'text', 'sessionID': 'ses_' + os.environ['CADRE_GOREV_ID'], 'part': {'type': 'text', 'text': json.dumps(a)}}))
"""


class ParalelDagiticiTest(unittest.TestCase):
    """P3 (03.10): local is closed; these tests tested a mixed local/cloud lane dispatch."""
    def setUp(self):
        import tempfile
        import shutil
        from kosu import RunStore
        t = Path(tempfile.mkdtemp(prefix='paralel-e2e-'))
        self.addCleanup(shutil.rmtree, t, True)
        self.t = t
        wt = t / 'wt'
        wt.mkdir()
        g = lambda *a: subprocess.run(['git', '-C', str(wt), '-c', 'user.name=f', '-c', 'user.email=f@l', *a], check=True, capture_output=True)
        g('init', '-q'); (wt / 'README.md').write_text('x\n'); g('add', '.'); g('commit', '-qm', 'base')
        base = subprocess.run(['git', '-C', str(wt), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
        self.store = RunStore(t)
        (t / 'projects/a').mkdir(parents=True)
        from qa_fixture import command
        (t / 'projects/a/qa-komut').write_text(command('test -s g1.txt && test -s g2.txt'))
        self.rid = self.store.accept('a', 'iki bagimsiz degisiklik', source_sha=base)['run_id']
        self.store.update(self.rid, worktree=str(wt))
        yazan = self.store.task_dir(self.rid, 'impl') / 'attempts/fixture/attempt.json'
        yazan.parent.mkdir(parents=True)
        yazan.write_text(json.dumps({'role': 'impl', 'model': 'fixture/writer', 'pid': 123}))

    def kos(self, modeller=('ds-pro-go', 'ds-pro'), **env):
        import shutil
        from kosu import execute_run
        root = self.t / 'dispatcher'
        from qa_fixture import instructions
        instructions(root)
        (root / 'lib').mkdir(parents=True)
        (root / 'bin').mkdir()
        runner = Path(__file__).with_name('qa_fixture.py')
        shutil.copyfile(runner, root / 'lib/qa_fixture.py')
        qa_path = Path(self.store.read(self.rid)['constraints']['qa-komut'])
        qa_path.write_text(qa_path.read_text().replace(str(runner.resolve()), str(root / 'lib/qa_fixture.py')))
        for name in ('arsiv.py', 'kosu.py', 'runtime_sync.py', 'sonuc.py', 'oturum.py', 'qe_core.py', 'canlilik.py', 'kabul.py', 'motor.py', 'dispatch_util.py', 'butce_kapisi.py', 'go_pencere.py', 'sinirlar.py', 'sinirlar.json', 'sinirlar.mjs', 'opencode_adaptor.py', 'butce-plugin.mjs', 'cadre-plugin.mjs', 'aylik-butce-plugin.mjs', 'yonlendirici.py', 'model_secimi.py',
                     'katalog.py', 'sozlesme.py', 'seviye.py', 'onelek.py', 'bolme.py', 'rapor.py', 'standart-gorev-formu.txt'):
            shutil.copyfile(LIB / name, root / 'lib' / name)
        for source in LIB.glob('*.py'):
            if not (root / 'lib' / source.name).exists():
                shutil.copyfile(source, root / 'lib' / source.name)
        shutil.copytree(LIB / 'qa-sim', root / 'lib/qa-sim', ignore=shutil.ignore_patterns('__pycache__'), dirs_exist_ok=True)
        from test_uctan_uca import FAKE_BUTCE
        from test_kosu_sessions import KATALOG
        (root / 'lib/butce.py').write_text(FAKE_BUTCE)
        (self.t / 'models.json').write_text(json.dumps(KATALOG))
        shutil.copyfile(LIB.parent / 'bin/cadre-dispatch.sh', root / 'bin/cadre-dispatch.sh')
        (root / 'lib/common.sh').write_text('qe_verify_ai_worktree(){ return 0; }\nqe_make_worktree(){ return 99; }\nqe_commit_wip(){ echo 0; }\n')
        (root / 'worker.py').write_text(PARALEL_ISCI)
        (root / 'bin/is-motoru.sh').write_text(f'exec "{sys.executable}" "$CADRE_ENGINE_ROOT/worker.py"\n')
        plan = {'is_id': 'fixture', 'kapsam_puani': 30, 'kapsam_gerekce': 'iki bagimsiz ozellik', 'gorevler': [
            {'id': f'g{i}', 'tanim': 't', 'model': m, 'paralel_grup': i, 'dosyalar': [f'g{i}.txt'], 'kabul': 'k', 'zorluk': 3, 'tahmin_dk': 1, 'tahmin_usd': .01}
            for i, m in enumerate(modeller, 1)]}
        (self.store.path(self.rid) / 'plan.json').write_text(json.dumps(plan))
        self.store.update(self.rid, phase='dispatch')
        hakem = self.t / 'fake-reviewer'
        hakem.write_text(f'#!{sys.executable}\n' + GECER_HAKEM)
        hakem.chmod(0o700)
        with patch.dict(os.environ, {'CADRE_ENGINE_ROOT': str(root), 'CADRE_REAL_OPENCODE': str(hakem), 'OPENCODE_DB': str(self.t / 'absent.db'),
                                    'FIXTURE_ZAMAN': str(self.t / 'zaman'), 'CADRE_MODEL_KATALOG': str(self.t / 'models.json'), 'CADRE_BEKLEME_ESIK_SN': '0', 'CADRE_HOME': str(self.t), **env}):
            rc = execute_run(self.store, self.rid)
        self.assertTrue((self.t / 'zaman').exists(), (self.store.path(self.rid) / 'dispatch.log').read_text())
        kayitlar = {j['gid']: j for j in map(json.loads, (self.t / 'zaman').read_text().splitlines())}
        return rc, kayitlar

    def test_bagimsiz_bulut_seritleri_eszamanli_kosar(self):
        rc, k = self.kos()
        self.assertEqual(rc, 0, self.store.read(self.rid).get('final_result'))
        self.assertLess(max(k['g1']['bas'], k['g2']['bas']), min(k['g1']['son'], k['g2']['son']), 'seritler ortusmedi')
        self.assertEqual(self.store.read(self.rid)['state'], 'completed')

    def test_seri_ayarda_ortusme_yok(self):
        rc, k = self.kos(CADRE_PARALEL_SERIT='1')
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(max(k['g1']['bas'], k['g2']['bas']), min(k['g1']['son'], k['g2']['son']))

    def test_takilan_birlestirme_operator_zincirindeki_sonraki_modele_yukselir(self):
        zincir = 'opencode-go/glm-5.3,opencode-go/kimi-k3'
        rc, _ = self.kos(modeller=('ds-pro', 'ds-pro', 'ds-pro', 'ds-pro'), FIXTURE_BIRLESTIRME_YARIM='2',
                         CADRE_ZINCIR_BIRLESTIRME=zincir)
        birlestirme = [json.loads(s)['model'] for s in (self.t / 'zaman').read_text().splitlines() if json.loads(s)['gid'] == 'birlestirme']
        self.assertEqual(birlestirme, ['opencode-go/glm-5.3', 'opencode-go/glm-5.3', 'opencode-go/kimi-k3'],
                         'iki yarim denemeden sonra zincir disi sabit karar modeline degil zincirin sonraki halkasina gecilir')
        self.assertEqual(rc, 0, (self.store.path(self.rid) / 'dispatch.log').read_text()[-2000:])

    def test_ayni_kucuk_saglayici_ikinci_seritte_goya_gider(self):
        rc, k = self.kos(modeller=('ds-pro', 'ds-pro'))
        self.assertEqual(rc, 0)
        # 4c-17: no direct DeepSeek route in the code chain; both lanes run on the same model's Go route
        self.assertEqual(sorted(j['model'] for j in k.values()), ['opencode-go/deepseek-v4-pro', 'opencode-go/deepseek-v4-pro'])


if __name__ == '__main__':
    unittest.main()


class ZorlukDagitimiTests(unittest.TestCase):
    def test_hard_work_goes_to_budgeted_cloud_and_easy_work_uses_the_cheap_chain(self):
        """D13 (03.10): difficulty 1-2 -> the impl-light chain; 3-4 -> the role chain; 5 -> the strong role chain.
        No local model (06.10). If the model is in the chain it is used, otherwise the first link; an empty chain means no capacity."""
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
        import yonlendirici as yon
        from unittest.mock import patch
        acik = (['opencode-go/qwen3.8-flash'], [])
        with patch.object(yon, 'zincir', return_value=acik):
            # D13: difficulty <=2 -> impl-light; empty model -> first link
            self.assertEqual(yon.zorluk_modeli('', 4), 'opencode-go/qwen3.8-flash')
            self.assertEqual(yon.zorluk_modeli('', 2), 'opencode-go/qwen3.8-flash')
            # D13: if the model is in the chain it is used
            self.assertEqual(yon.zorluk_modeli('opencode-go/qwen3.8-flash', 2), 'opencode-go/qwen3.8-flash')
            self.assertEqual(yon.zorluk_modeli('opencode-go/qwen3.8-flash', 3), 'opencode-go/qwen3.8-flash')
        with patch.object(yon, 'zincir', return_value=([], [])):
            self.assertIsNone(yon.zorluk_modeli('', 5))
        # If the model is in the chain it is used (D13: no free/paid distinction)
        for model in ('openai/gpt-6.1-sol', 'opencode/big-pickle', 'google/gemini-3-flash-preview'):
            with patch.object(yon, 'zincir', return_value=([model], [])):
                self.assertEqual(yon.zorluk_modeli(model, 2), model)
        self.assertIn('uygun "$gmdl" impl "$gz"', (Path(__file__).resolve().parents[1] / 'bin' / 'cadre-dispatch.sh').read_text().split('serit_modeli(){')[1].split('\n}')[0])


class PlanParalelKuraliTests(unittest.TestCase):
    def test_plan_prompt_keeps_shared_docs_to_the_last_task(self):
        """B328 (03.10): since every task touched BACKLOG/SOZLESME, 5 independent code jobs were serialized (~6 h)."""
        istem = (Path(__file__).resolve().parents[1] / 'bin' / 'cadre-dispatch.sh').read_text()
        self.assertIn('ORTAK BELGE (BACKLOG.md, README) yalniz SON gorevde', istem)
        self.assertIn('docs/SOZLESME.md kod haritasina o dosyanin satirini KENDISI ekler', istem)


class ZamanAyarlariTests(unittest.TestCase):
    def test_plan_with_shared_registry_in_two_tasks_is_rejected(self):
        """B328: BACKLOG/README/SOZLESME only in the last task; otherwise a plan schema error (the planner fixes it)."""
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
        import dispatch_util as du
        def plan(d1, d2):
            g = lambda i, d: {'id': i, 'tanim': 'x', 'dosyalar': d, 'kabul': 'test gecer', 'kapsam_disi': [], 'bagimli': [], 'zorluk': 3,
                              'model': 'auto', 'paralel_grup': 1, 'tahmin_dk': 10}
            return {'is_id': 'p', 'kapsam_puani': 30, 'gorevler': [g('g1', d1), g('g2', d2)], 'birlestirme_sirasi': ['g1', 'g2'],
                    'kalite': {'model': 'x', 'kapsam': 'y'}}
        self.assertTrue(du.ortak_belge_hatasi(plan(['a.py', 'BACKLOG.md'], ['b.py', 'BACKLOG.md'])))
        self.assertTrue(du.ortak_belge_hatasi(plan(['a.py', 'BACKLOG.md'], ['b.py'])))
        self.assertIsNone(du.ortak_belge_hatasi(plan(['a.py'], ['b.py', 'BACKLOG.md'])))
        # B343: a task adding a new engine file updates the contract map itself (the guard requires it in the same task)
        self.assertIsNone(du.ortak_belge_hatasi(plan(['a.py', 'docs/SOZLESME.md'], ['b.py', 'docs/SOZLESME.md', 'BACKLOG.md'])))
        # 03.10 00:26 live: when the rule was applied while re-reading a recorded plan the running run failed — re-reading must accept
        du.validate_plan(plan(['a.py', 'BACKLOG.md'], ['b.py', 'BACKLOG.md']))
        import subprocess as sp, json as js
        r = sp.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'lib/dispatch_util.py'), 'extract'],
                   input=js.dumps(plan(['a.py', 'BACKLOG.md'], ['b.py', 'BACKLOG.md'])), capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0, 'yeni plan kabulunde kural uygulanir')

