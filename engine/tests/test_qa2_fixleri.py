"""QA-2 (15.10.2026, FIX-28..FIX-49): regression tests for the defects verified by five independent agents."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import arayuz_veri  # noqa: E402
import butce_kapisi as bk  # noqa: E402
from canlilik import Canlilik  # noqa: E402
import dil  # noqa: E402
import izin  # noqa: E402
import kabul  # noqa: E402
import kosu  # noqa: E402
from kosu import RunStore, atomic_json, process_identity  # noqa: E402
import kurulum_kontrol  # noqa: E402
import verim  # noqa: E402
from test_butce_donemi import snapshot  # noqa: E402

GIT_KIMLIK = {'GIT_AUTHOR_NAME': 'fixture', 'GIT_AUTHOR_EMAIL': 'f@local', 'GIT_COMMITTER_NAME': 'fixture', 'GIT_COMMITTER_EMAIL': 'f@local'}


class ButceAilesiTests(unittest.TestCase):
    """FIX-28 / FIX-33: an unrelated broken record does not lock the family; recovery does not stop at a single broken line."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='qa2-butce-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        env = patch.dict(os.environ, {'CADRE_HOME': str(self.home), 'CADRE_KOSU_TAVAN_USD': '1', 'CADRE_HARCAMA_ALARMI': '0',
                                      'CADRE_GOREV_ID': '', 'CADRE_AG_YOK': '1'})
        env.start()
        self.addCleanup(env.stop)
        self.store = RunStore(self.home)
        self.rid = self.store.accept('work', 'selected work')['run_id']
        self.ledger = bk.BudgetLedger(self.home / 'zincir/request-budget.db')
        self.budget = snapshot(50)
        self.budget.update(zen={'db': 'ok', 'pay': 50, 'kullanilabilir': 50}, **{'or': {'db': 'ok', 'pay': 50, 'kullanilabilir': 50}})

    def reserve(self):
        cap = bk.rol_tavani(self.home, self.rid, dilim_adi='seritler')
        return self.ledger.reserve('openrouter', 1000, self.budget, {'run_id': self.rid, 'phase_role': 'seritler'}, run_cap=cap, dilim='seritler')

    def test_unrelated_unreadable_records_do_not_block_the_family_but_a_broken_member_does(self):
        self.reserve()
        for name, body in (('zzz-eski', json.dumps({'run_id': 'zzz-eski', 'version': 0})), ('zzz-bos', ''), ('zzz-cop', '{not json')):
            (self.store.root / name).mkdir()
            (self.store.root / name / 'run.json').write_text(body)
        self.assertEqual(bk.operator_ailesi(self.home, self.rid)[0], {self.rid})
        self.reserve()
        (self.store.root / 'zzz-uye').mkdir()
        (self.store.root / 'zzz-uye' / 'run.json').write_text(json.dumps({'run_id': 'zzz-uye', 'proje_ust': self.rid, 'version': 0}))
        with self.assertRaisesRegex(bk.BudgetDenied, 'ownership unavailable'):
            bk.operator_ailesi(self.home, self.rid)
        with self.assertRaises(bk.BudgetDenied):
            self.reserve()

    def test_stale_recovery_skips_an_unreadable_bound_and_closes_the_readable_rows(self):
        now = time.time()
        with self.ledger.transaction() as con:
            con.execute('insert into reservation values(?,?,?,?,?,?,?,?)',
                        ('legacy-a', 'openrouter', '2026-10', now - 7200, 5000, None, None, json.dumps({'en_kotu': 1.5})))
            con.execute('insert into reservation values(?,?,?,?,?,?,?,?)',
                        ('legacy-b', 'openrouter', '2026-10', now - 7200, 4000, None, None, json.dumps({})))
        with patch.object(sys, 'stdout', io.StringIO()), patch.object(sys, 'stderr', io.StringIO()):
            bk.kurtarma(self.home, ['--bayat-kapat', '--evet', '--saat', '1'])
        with self.ledger.transaction() as con:
            acik = [r[0] for r in con.execute('select id from reservation where actual is null order by id').fetchall()]
        self.assertEqual(acik, ['legacy-a'])


class TabanOrtamiTests(unittest.TestCase):
    """FIX-30: terminal/session variables do not change the baseline fingerprint; the build environment does."""

    def test_session_variables_do_not_change_the_fingerprint(self):
        with tempfile.TemporaryDirectory(prefix='qa2-taban-') as d:
            subprocess.run(['git', '-C', d, 'init', '-q', '-b', 'main'], check=True)
            (Path(d) / 'a.txt').write_text('a\n')
            subprocess.run(['git', '-C', d, 'add', '-A'], check=True)
            subprocess.run(['git', '-C', d, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'init'], check=True)
            base = kabul.baseline_context(d, 'true', '')['environment']
            with patch.dict(os.environ, {'TERM_SESSION_ID': 'w0t1', 'ITERM_SESSION_ID': 'x', 'SSH_AUTH_SOCK': '/tmp/sock',
                                         'TMUX': '/tmp/tmux', 'TMPDIR': '/tmp/other', 'VSCODE_PID': '1', 'COLUMNS': '300',
                                         'CODEX_THREAD_ID': 'another-session', 'CODEX_TOOL_CALL_ID': 'another-call'}):
                self.assertEqual(kabul.baseline_context(d, 'true', '')['environment'], base)
            with patch.dict(os.environ, {'LANG': 'xx_YY.UTF-8'}):
                self.assertNotEqual(kabul.baseline_context(d, 'true', '')['environment'], base)
            with patch.dict(os.environ, {'PRODUCT_TEST_MODE': 'changed'}):
                self.assertNotEqual(kabul.baseline_context(d, 'true', '')['environment'], base)


class KurulumZamanAsimiTests(unittest.TestCase):
    """FIX-29: an approved install action that timed out is completed; it can be retried with a new-generation approval."""

    def setUp(self):
        from test_baseline import BaselineTests
        self.b = BaselineTests()
        self.b.setUp()
        self.addCleanup(self.b.doCleanups)

    def test_timed_out_setup_completes_its_action_and_can_be_retried(self):
        b = self.b
        (b.project / 'kurulum-komut').write_text('sleep 3')
        with patch.dict(os.environ, {'CADRE_KURULUM_TIMEOUT': '1'}):
            result = b.baseline(approve_setup=True)
        self.assertEqual((result['status'], result['reason'], result['kurulum']['exit_code']), ('FAIL', 'kurulum-hatasi', 124))
        folder = izin.scope(b.store, b.rid) / 'operator-actions'
        self.assertEqual(sorted(json.loads(p.read_text())['status'] for p in folder.glob('*.json')), ['completed'])
        again = izin.setup_proposal(b.store, b.rid, b.wt, 'sleep 3')
        self.assertEqual((again['status'], again['identity']['generation']), ('pending', 2))


class CozumlemeTests(unittest.TestCase):
    """FIX-31: an action left half-done (in-flight/uncertain) closes with an operator decision; denied while a live owner exists."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='qa2-izin-')
        self.addCleanup(folder.cleanup)
        self.store = RunStore(Path(folder.name))
        self.rid = self.store.accept('fixture', 'local work')['run_id']
        self.args = (self.store, self.rid, 'push', [['git', 'push', 'origin', 'main']], {'path': '/fixture', 'branch': 'main'}, {'head': 'h'})
        self.action = izin.propose(*self.args)
        first = izin.approve(self.store, self.rid, self.action['token'])
        if first['required'] == 2:
            izin.approve(self.store, self.rid, self.action['token'], first['confirmation'])

    def yaz(self, **values):
        action = izin.read_action(self.store, self.rid, self.action['token'])
        action.update(values)
        atomic_json(izin.scope(self.store, self.rid) / 'operator-actions' / (self.action['token'] + '.json'), action)
        return action

    def test_in_flight_action_can_be_closed_and_reopened_by_the_operator(self):
        self.yaz(status='in-flight', consumed=time.time())
        self.assertEqual(izin.propose(*self.args)['token'], self.action['token'])
        with self.assertRaisesRegex(ValueError, 'operator-resolution-invalid'):
            izin.resolve(self.store, self.rid, self.action['token'], 'belki')
        closed = izin.resolve(self.store, self.rid, self.action['token'], 'etkisiz')
        self.assertEqual((closed['status'], closed['resolution']['previous'], closed['resolution']['by']), ('failed', 'in-flight', 'operator'))
        self.assertEqual(izin.propose(*self.args)['identity']['generation'], 2)
        self.assertEqual(self.store.read(self.rid)['operator_interventions'][-1]['kind'], 'operator-resolve')
        with self.assertRaisesRegex(PermissionError, 'operator-effect-unresolved'):
            izin.resolve(self.store, self.rid, self.action['token'], 'tamamlandi')

    def test_uncertain_action_marked_done_keeps_the_single_generation(self):
        self.yaz(status='uncertain', error='KeyboardInterrupt')
        done = izin.resolve(self.store, self.rid, self.action['token'], 'tamamlandi')
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(izin.propose(*self.args)['token'], self.action['token'])

    def test_live_owner_and_running_runs_block_resolution(self):
        self.yaz(status='in-flight', effect_seed={'process': process_identity(os.getpid())})
        with self.assertRaisesRegex(PermissionError, 'operator-effect-owner-active'):
            izin.resolve(self.store, self.rid, self.action['token'], 'etkisiz')
        with patch.dict(os.environ, {'CADRE_RUN_ID': self.rid, 'CADRE_HOME': str(self.store.home)}):
            with self.assertRaisesRegex(PermissionError, 'operator-command-required'):
                izin.main(['resolve', self.rid, self.action['token'], 'etkisiz'])


class ModelOnayBeklemeTests(unittest.TestCase):
    """FIX-47: root and child records waiting for model approval are released together with the approval."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='qa2-model-')
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.store = RunStore(self.home)
        self.root = self.store.accept('fixture', 'proje')['run_id']
        self.child = self.store.accept('fixture', 'cocuk')['run_id']
        self.store.update(self.child, proje_ust=self.root)

    def test_activation_clears_the_root_and_its_children(self):
        action = izin.propose(self.store, self.root, 'model-calls', *izin.model_details(self.store, self.root))
        first = izin.approve(self.store, self.root, action['token'])
        izin.approve(self.store, self.root, action['token'], first['confirmation'])
        self.store.update(self.child, tool_boundary_wait='project-tool-operator-model-approval-required', reason='operator-model-approval-required')
        self.store.update(self.root, reason='proje-durdu: project-tool-operator-model-approval-required')
        izin.model_activate(self.store, self.root, action['token'])
        for rid in (self.root, self.child):
            record = self.store.read(rid)
            self.assertEqual((record.get('tool_boundary_wait') or '', record['reason']), ('', 'operator-model-ready'), rid)

    def test_denied_check_marks_both_root_and_child(self):
        with patch.dict(os.environ, {'CADRE_HOME': str(self.home)}), patch.object(izin, 'model_check', side_effect=PermissionError('x')):
            with self.assertRaises(PermissionError):
                izin.main(['model-check', self.child, 'openrouter'])
        for rid in (self.root, self.child):
            self.assertEqual(self.store.read(rid)['tool_boundary_wait'], 'project-tool-operator-model-approval-required', rid)


class YarimBaslatmaTests(unittest.TestCase):
    """FIX-46: an empty folder interrupted between git init and the first commit continues by asking for permission again."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='qa2-init-')
        self.addCleanup(folder.cleanup)
        self.tmp = Path(folder.name)
        self.home = self.tmp / 'ev'
        self.home.mkdir()
        env = patch.dict(os.environ, {'CADRE_HOME': str(self.home), 'GIT_CEILING_DIRECTORIES': str(self.tmp.parent), **GIT_KIMLIK})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('CADRE_RUN_ID', None)
        self.store = RunStore(self.home)

    def test_init_without_commit_is_resumed_as_an_empty_project(self):
        import proje_kayit
        target = self.tmp / 'yeni'
        target.mkdir()
        subprocess.run(['git', '-C', str(target), 'init', '-q', '-b', 'main'], check=True)
        with self.assertRaises(proje_kayit.KayitIzni) as first:
            proje_kayit.kaydet(target, olustur=True, home=self.home)
        token = first.exception.token
        action = izin.read_action(self.store, 'system', token)
        self.assertEqual(action['identity']['kind'], 'init-commit')
        action.update(status='in-flight', consumed=time.time())
        atomic_json(izin.scope(self.store, 'system') / 'operator-actions' / (token + '.json'), action)
        with self.assertRaises(proje_kayit.KayitIzni) as second:
            proje_kayit.kaydet(target, olustur=False, home=self.home)
        self.assertNotEqual(second.exception.token, token)
        self.assertEqual(izin.read_action(self.store, 'system', token)['status'], 'failed')
        (target / 'x.txt').write_text('x\n')
        with self.assertRaises(proje_kayit.KayitHatasi):
            proje_kayit.kaydet(target, olustur=True, home=self.home)


class ParalelAracTests(unittest.TestCase):
    """FIX-34: the set of open tool steps; a sibling tool finishing does not drop an open step."""

    def test_open_tool_set_survives_a_sibling_completion(self):
        with tempfile.TemporaryDirectory(prefix='qa2-canli-') as d:
            log = Path(d) / 'out.log'
            bekci = object.__new__(Canlilik)
            bekci.logf, bekci.ofset, bekci.artik, bekci.adimlar = log, 0, b'', set()
            bekci.acik_araclar, bekci.acik_adim = set(), False

            def olay(call, status):
                return json.dumps({'part': {'type': 'tool', 'callID': call, 'tool': 'bash',
                                            'state': {'status': status, 'input': {'c': call}, 'output': ''}}}) + '\n'
            log.write_text(olay('a', 'running') + olay('b', 'running') + olay('b', 'completed'))
            self.assertTrue(bekci._adim())
            self.assertTrue(bekci.acik_adim, 'A hala acik')
            with log.open('a') as stream:
                stream.write(olay('a', 'completed'))
            bekci._adim()
            self.assertFalse(bekci.acik_adim)


class KoordinatorKimlikTests(unittest.TestCase):
    """FIX-35: the coordinator completes its identity after the terminal/turn checks, before work starts."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='qa2-kimlik-')
        self.addCleanup(folder.cleanup)
        self.tmp = Path(folder.name)
        self.home = self.tmp / 'ev'
        (self.home / 'projects' / 'p').mkdir(parents=True)
        repo = self.tmp / 'repo'
        repo.mkdir()
        subprocess.run(['git', '-C', str(repo), 'init', '-q', '-b', 'main'], check=True)
        (repo / 'a.py').write_text('x = 1\n')
        subprocess.run(['git', '-C', str(repo), 'add', '-A'], check=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'init'], check=True)
        (self.home / 'projects' / 'p' / 'repo-path').write_text(str(repo) + '\n')
        (self.home / 'projects' / 'p' / 'base-branch').write_text('main\n')
        env = patch.dict(os.environ, {'CADRE_HOME': str(self.home), 'OPENCODE_CFG': str(self.tmp / 'cfg')})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('CADRE_RUN_ID', None)
        self.store = RunStore(self.home)

    def test_coordinator_completes_identity_before_any_work_starts(self):
        record = self.store.accept('p', 'kucuk is')
        self.assertIsNone(record['provenance']['completed'])
        real = subprocess.Popen

        def popen(args, *rest, **kw):
            if args and args[0] == 'caffeinate':
                raise OSError('no caffeinate')
            return real(args, *rest, **kw)
        with patch.object(kosu, 'disk_bekle', return_value=None), patch.object(kosu, 'bekci_dirilt', return_value=[]), \
             patch.object(subprocess, 'Popen', side_effect=popen):
            self.assertEqual(kosu.execute_run(self.store, record['run_id']), 130)
        done = self.store.read(record['run_id'])
        self.assertEqual(done['state'], 'cancelled')
        self.assertIsNotNone(done['provenance']['completed'])
        self.assertIsInstance(done['provenance']['runtime']['git_version'], str)


class OperatorKomutTests(unittest.TestCase):
    """FIX-36 / FIX-48: `cadre init --olustur` goes through the wrapper; request-approve does not work from inside a run."""

    def test_init_wrapper_passes_olustur_and_help_lists_the_new_commands(self):
        with tempfile.TemporaryDirectory(prefix='qa2-cadre-') as d:
            home = Path(d) / 'home'
            (Path(d) / 'h').mkdir()
            home.mkdir()
            env = {**os.environ, 'HOME': str(Path(d) / 'h'), 'CADRE_HOME': str(home),
                   'GIT_CEILING_DIRECTORIES': str(Path(d).parent), **GIT_KIMLIK}
            env.pop('CADRE_RUN_ID', None)
            target = Path(d) / 'yeni-proje'
            result = subprocess.run(['bash', str(LIB.parent / 'bin/cadre.sh'), 'init', '--olustur', str(target)], input='',
                                    capture_output=True, text=True, env=env, cwd=d, timeout=120)
            self.assertTrue(target.is_dir(), result.stdout + result.stderr)
            self.assertNotIn('zaten var', result.stdout + result.stderr)
            help_text = subprocess.run(['bash', str(LIB.parent / 'bin/cadre.sh'), '--help'], capture_output=True, text=True, env=env, timeout=60).stdout
            self.assertIn('init [--olustur] [--tur python] [klasor]', help_text)
            self.assertIn('izin-cozumle', help_text)

    def test_request_approve_refuses_to_run_inside_a_run(self):
        with tempfile.TemporaryDirectory(prefix='qa2-kosu-') as d:
            env = {**os.environ, 'CADRE_HOME': d, 'CADRE_RUN_ID': 'r1'}
            result = subprocess.run([sys.executable, str(LIB / 'kosu.py'), 'request-approve', 'r1'], input='{}',
                                    capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(result.returncode, 78)
            self.assertIn('request-approve-operator-command-required', result.stderr)


class KatalogTests(unittest.TestCase):
    """FIX-32: a missing catalogue is visible in the install check; the router names the rejection reason."""

    def test_install_check_reports_the_model_catalog(self):
        with tempfile.TemporaryDirectory(prefix='qa2-katalog-') as d:
            env = {'PATH': os.environ.get('PATH', ''), 'CADRE_MODEL_KATALOG': str(Path(d) / 'models.json'), 'CADRE_HOME': d,
                   'OPENCODE_CFG': d, 'CADRE_ENGINE_ROOT': str(LIB.parent)}
            items = {i['ad']: i for i in kurulum_kontrol.kontrol(environment=env)['maddeler']}
            self.assertEqual((items['model katalogu']['durum'], items['model katalogu']['gerekli']), ('EKSIK', False))
            (Path(d) / 'models.json').write_text('{}')
            items = {i['ad']: i for i in kurulum_kontrol.kontrol(environment=env)['maddeler']}
            self.assertEqual(items['model katalogu']['durum'], 'VAR')

    def test_router_names_the_missing_catalog(self):
        import yonlendirici
        with patch('model_secimi.cagri_tavani', side_effect=ValueError('model katalogda yok')), \
             patch.object(yonlendirici, 'istek_tavani', return_value=10 ** 9), \
             patch.object(yonlendirici, 'gunluk_token_kalan', return_value=None), \
             patch('butce_kapisi.model_kovasi', return_value='kova'):
            ok, reason = yonlendirici.butce_var('opencode-go', 'glm-5.3', {'go': {'pay': 50}}, 8000, rol='impl', selection={'preference': 'balanced'})
        self.assertFalse(ok)
        self.assertIn('model katalogu/fiyat eksik', reason)
        self.assertIn('model katalogda yok', reason)


class VerimOzetTests(unittest.TestCase):
    """FIX-44: a value that cannot be measured stays unknown, never written as zero; labels are in the language table."""

    def test_unknown_units_and_missing_waits_are_not_printed_as_zero(self):
        stage = {**verim._bos(), 'istek': 2, 'bilinmeyen_birim': 2}
        data = {'maliyet': {'asamalar': {'impl': stage, 'qa': verim._bos()}},
                'sure': {'calisma_sn': {'impl': 12.0}, 'kuyruk_sn': None, 'kota_bekleme_sn': None, 'kurulum_sn': None}}
        lines = verim.ozet_satirlari(data)
        self.assertEqual(len(lines), 2)
        self.assertIn('bilinmeyen birimde', lines[0])
        self.assertNotIn('$0.0000', lines[0])
        self.assertIn('kota bekleme bilinmiyor', lines[1])
        self.assertIn('kurulum bilinmiyor', lines[1])
        dil.EKSIK.clear()
        dil.kur('en')
        try:
            english = verim.ozet_satirlari(data)
        finally:
            dil.kur('tr')
        self.assertEqual(sorted(dil.EKSIK), [])
        self.assertIn('quota wait unknown', english[1])

    def test_missing_wait_records_stay_unknown(self):
        with tempfile.TemporaryDirectory(prefix='qa2-verim-') as d:
            store = RunStore(Path(d))
            rid = store.accept('p', 'is')['run_id']
            self.assertIsNone(verim.sure(store, [rid], rid)['kota_bekleme_sn'])
            store.update(rid, budget_waits=[{'ts': 10.0, 'until': 25.0}])
            self.assertEqual(verim.sure(store, [rid], rid)['kota_bekleme_sn'], 15.0)


class PanelVeriTests(unittest.TestCase):
    """FIX-41: in a finished run a step that never started is 'yapilmadi'; EN singular forms; no leftover semicolons."""

    def test_terminal_run_shows_unstarted_resumed_tasks_as_not_done(self):
        k = {'run_id': 'r1', 'state': 'failed', 'project': 'p', 'tasks': {'g1': {'state': 'bekliyor'}, 'g2': {'state': 'running'}}}
        with tempfile.TemporaryDirectory(prefix='qa2-panel-') as d:
            bekleyen = arayuz_veri.alt_gorev_dugumu(Path(d), k, {'id': 'g1', 'tanim': 'x'}, {}, {})
            yarim = arayuz_veri.alt_gorev_dugumu(Path(d), k, {'id': 'g2', 'tanim': 'y'}, {}, {})
        self.assertEqual(bekleyen['ozet'].split(' · ')[0], 'Yapılmadı')
        self.assertEqual(yarim['ozet'].split(' · ')[0], 'Yarım kaldı')

    def test_english_singular_forms(self):
        dil.kur('en')
        try:
            self.assertEqual((dil.t('… {n} görev daha · Görevler', n=1), dil.t('… {n} uyarı daha · Dikkat', n=1), dil.t('{n} kayıt', n=1)),
                             ('… 1 more task · Tasks', '… 1 more alert · Attention', '1 record'))
            self.assertEqual(dil.t('{n} kayıt', n=3), '3 records')
        finally:
            dil.kur('tr')
        self.assertFalse([k for k, v in {**dil.EN, **dil.TEKIL}.items() if ';' in k or ';' in v])


if __name__ == '__main__':
    unittest.main()
