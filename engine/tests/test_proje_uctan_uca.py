"""G-P3/G-P4/G-P6 end to end: `proje.baslat` runs child runs with the real RunStore + the real coordinator (`execute_run`) + the real dispatcher;
only the worker (adds a line to a file), the judge (GECER) and the planner are fake. Proven: children open from the tip of the intermediate branch, the product
is committed and fast-forwards, the final acceptance evidence passes to the main run and the coordinator's acceptance gate (`approved`) sees it as PASS."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import proje  # noqa: E402

KUTUPHANE = ('arsiv.py', 'kosu.py', 'runtime_sync.py', 'sonuc.py', 'oturum.py', 'qe_core.py', 'canlilik.py', 'kabul.py', 'motor.py', 'dispatch_util.py', 'butce_kapisi.py', 'go_pencere.py', 'sinirlar.py', 'sinirlar.json', 'sinirlar.mjs', 'opencode_adaptor.py', 'butce-plugin.mjs', 'cadre-plugin.mjs', 'aylik-butce-plugin.mjs', 'yonlendirici.py', 'model_secimi.py',
             'katalog.py', 'sozlesme.py', 'seviye.py', 'onelek.py', 'bolme.py', 'proje.py', 'standart-gorev-formu.txt')
ORTAK = r'''qe_repo_path(){ cat "$CADRE_HOME/projects/$1/repo-path"; }
qe_verify_ai_worktree(){ return 0; }
qe_make_worktree(){
  local wt="$CADRE_HOME/worktrees/$1/$CADRE_RUN_ID" sha
  sha="$(python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" source-info "$CADRE_RUN_ID" | awk '{print $1}')"
  mkdir -p "${wt%/*}"
  git -C "$(qe_repo_path "$1")" worktree add -q -b "cadre/$CADRE_RUN_ID" "$wt" "$sha" >&2 && printf '%s\n' "$wt"
}
qe_commit_wip(){ git -C "$1" add -A >&2; git -C "$1" -c user.name=f -c user.email=f@x commit -qm "$3" >&2 && echo 1 || echo 0; }
'''
ISCI = '''
import json, os, pathlib, sys
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
from kosu import RunStore
rid, gid = os.environ['CADRE_RUN_ID'], os.environ['CADRE_GOREV_ID']
wt = pathlib.Path(os.environ['CADRE_WORKTREE'])
urun = wt / 'urun.txt'
urun.write_text((urun.read_text() if urun.exists() else '') + rid + '\\n')
yazar = RunStore().task_dir(rid, gid) / 'attempts' / 'fx'   # yazar bulutta: yerel hakem bagimsiz kalir (B180)
yazar.mkdir(parents=True, exist_ok=True)
(yazar / 'attempt.json').write_text(json.dumps({'role': 'impl', 'model': 'fixture/writer', 'pid': 123}))
RunStore().task_update(rid, gid, state='uretti')
'''
HAKEM = '''
import json, sys, uuid
soru = json.loads(sys.argv[-1].splitlines()[-1])
cevap = {'verdict': 'GECER', 'revision': soru['revision'],
         'acceptance': [{'id': k['id'], 'status': 'PASS', 'evidence': 'urun.txt satiri incelendi'} for k in soru['criteria']]}
print(json.dumps({'type': 'text', 'sessionID': 'ses_' + uuid.uuid4().hex, 'part': {'type': 'text', 'text': json.dumps(cevap)}}))   # opencode gibi her cagri yeni oturum
'''


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=f', '-c', 'user.email=f@x', *args],
                          check=True, capture_output=True, text=True, timeout=30).stdout.strip()


class ProjeUctanUcaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='proje-kosu-')
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.repo = self.home / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '-b', 'main')
        (self.repo / 'README.md').write_text('bos\n')
        # The baseline runs in the main source, the child QA in the copied engine. The shared QA input
        # must be in both allowed product trees; it does not escape to the operator's engine directory.
        shutil.copyfile(Path(__file__).with_name('qa_fixture.py'), self.repo / 'qa_fixture.py')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'base')
        self.taban = git(self.repo, 'rev-parse', 'HEAD')
        (self.home / 'zincir').mkdir()
        self.or_fiyat = self.home / 'zincir/openrouter-fiyat.json'
        self.or_fiyat.write_text(json.dumps({'version': 2, 'ts': time.time(), 'modeller': {'openai/gpt-6.1-sol': {
            'input': 1, 'output': 5, 'providers': ['openai'], 'fp8_cache': True}}}))
        pd = self.home / 'projects' / 'p'
        pd.mkdir(parents=True)
        (pd / 'repo-path').write_text(str(self.repo))
        (pd / 'base-branch').write_text('main')
        # This fixture needs only the stdlib; it does not carry the UI test interpreter's external venv
        # into the product QA as a dependency.
        self.qa_command = shlex.join([str(Path(sys.executable).resolve()), 'qa_fixture.py', 'test -s urun.txt'])
        (pd / 'qa-komut').write_text(self.qa_command)
        kok = self.home / 'motor'
        from qa_fixture import instructions
        instructions(kok)
        (kok / 'lib').mkdir(parents=True)
        (kok / 'bin').mkdir()
        for ad in (*KUTUPHANE, 'openrouter-politika.json'):
            shutil.copyfile(LIB / ad, kok / 'lib' / ad)
        for source in LIB.glob('*.py'):
            if not (kok / 'lib' / source.name).exists():
                shutil.copyfile(source, kok / 'lib' / source.name)
        shutil.copytree(LIB / 'qa-sim', kok / 'lib/qa-sim', ignore=shutil.ignore_patterns('__pycache__'), dirs_exist_ok=True)
        shutil.copyfile(LIB.parent / 'bin' / 'cadre-dispatch.sh', kok / 'bin' / 'cadre-dispatch.sh')
        (kok / 'lib' / 'common.sh').write_text(ORTAK)
        import test_uctan_uca   # P3 (03.10): no local -> the judge is in the cloud; a fake open Go budget (no network)
        (kok / 'lib' / 'butce.py').write_text(test_uctan_uca.FAKE_BUTCE)
        (kok / 'isci.py').write_text(ISCI)
        (kok / 'bin' / 'is-motoru.sh').write_text(f'exec "{sys.executable}" "$CADRE_ENGINE_ROOT/isci.py"\n')
        hakem = self.home / 'sahte-opencode'
        hakem.write_text(f'#!{sys.executable}\n' + HAKEM)
        hakem.chmod(0o700)
        temiz = {k: v for k, v in os.environ.items() if not k.startswith(('CADRE_'))}
        from test_kosu_sessions import KATALOG   # no real catalogue in an isolated HOME; cloud models count as "in the account"
        (self.home / 'models.json').write_text(json.dumps(KATALOG))
        temiz['CADRE_MODEL_KATALOG'] = str(self.home / 'models.json')
        ortam = patch.dict(os.environ, {**temiz, 'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(kok), 'CADRE_REAL_OPENCODE': str(hakem),
                                        'OPENCODE_DB': str(self.home / 'yok.db'), 'CADRE_BEKLEME_ESIK_SN': '0'}, clear=True)
        ortam.start()
        self.addCleanup(ortam.stop)
        from kosu import RunStore
        self.store = RunStore(self.home)
        self.ana = self.store.accept('p', 'sifirdan kucuk bir arac yaz')['run_id']
        from test_proje import izinli_birlestir
        manager = patch.object(proje, 'birlestir_ff', side_effect=izinli_birlestir)
        manager.start()
        self.addCleanup(manager.stop)

    def calistir(self, store, cid):
        """Real coordinator; only the child's plan is written beforehand (the planner is not the subject of this test)."""
        from kosu import execute_run
        plan = {'is_id': 'cocuk', 'kapsam_puani': 30, 'gorevler': [{'id': 'g1', 'tanim': 'urun satiri ekle', 'model': 'auto',
                'paralel_grup': 0, 'dosyalar': ['urun.txt'], 'kabul': 'urun.txt satir tasir', 'zorluk': 2, 'tahmin_dk': 1}]}
        (store.path(cid) / 'plan.json').write_text(json.dumps(plan))
        store.update(cid, phase='dispatch')
        return execute_run(store, cid)

    def planla(self, istem, gorev_id):
        return 'liste:\n' + json.dumps({'ozellikler': [
            {'id': 'f1', 'baslik': 'iskelet', 'aciklama': 'ilk satir', 'kabul': 'urun.txt var', 'qa_komut': self.qa_command, 'bagimli': [], 'oncelik': 1},
            {'id': 'f2', 'baslik': 'ikinci', 'aciklama': 'ikinci satir', 'kabul': 'iki satir', 'qa_komut': self.qa_command, 'bagimli': ['f1'], 'oncelik': 2}]})

    def parent_engine(self, worker_prefix):
        root = self.home / 'motor'
        shutil.copyfile(root / 'bin/cadre-dispatch.sh', root / 'bin/child-dispatch.sh')
        (root / 'bin/cadre-dispatch.sh').write_text(
            'if [ "$CADRE_RUN_ID" = "' + self.ana + '" ]; then\n'
            'exec python3 "$CADRE_ENGINE_ROOT/parent.py"\nfi\n'
            'exec bash "$CADRE_ENGINE_ROOT/bin/child-dispatch.sh" "$@"\n')
        (root / 'features.json').write_text(self.planla('', ''))
        (root / 'parent.py').write_text('''import json, os, sys
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
import proje
from kosu import execute_run
from pathlib import Path
original_transfer = proje.birlestir_ff
def transfer(repo, dal, k, *, store, run_id):
    try:
        return original_transfer(repo, dal, k, store=store, run_id=run_id)
    except PermissionError as exc:
        if str(exc) != 'git-transfer-permission-required':
            raise
    action = store.read(run_id)['project_git_action']
    first = proje.git_aktarim_onayi(store, run_id, action['token'])
    proje.git_aktarim_onayi(store, run_id, action['token'], first['confirmation'])
    return original_transfer(repo, dal, k, store=store, run_id=run_id)
proje.birlestir_ff = transfer
def child(store, cid):
    plan = {'is_id':'child','kapsam_puani':30,'gorevler':[{'id':'g1','tanim':'urun satiri',
        'model':'auto','paralel_grup':0,'dosyalar':['urun.txt'],'kabul':'urun.txt var','zorluk':2,'tahmin_dk':1}]}
    (store.path(cid) / 'plan.json').write_text(json.dumps(plan))
    store.update(cid, phase='dispatch')
    return execute_run(store, cid)
raise SystemExit(proje.baslat(os.environ['CADRE_RUN_ID'], 'p',
    planla=lambda *args: Path(os.environ['CADRE_ENGINE_ROOT'] + '/features.json').read_text(), calistir=child))
''')
        (root / 'isci.py').write_text(worker_prefix + ISCI)
        self.store.update(self.ana, phase='dispatch')

    def test_real_parent_alarm_approval_resume_and_final_acceptance_keep_all_states_consistent(self):
        from kosu import approve_spending, execute_run
        self.parent_engine('''import json, os, pathlib, sys, time
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
from kosu import RunStore
store = RunStore()
child = store.read(os.environ['CADRE_RUN_ID'])
parent = store.read(child['proje_ust'])
project = json.loads((store.path(parent['run_id']) / 'proje.json').read_text())
assert project['durum'] == 'calisiyor' and not project['neden'] and not parent.get('reason')
marker = store.home / 'alarm-once'
if not marker.exists():
    marker.write_text('alarm')
    store.update(child['run_id'], harcama_alarmi={'ts':time.time()}, reason='butce-bekleme')
    raise SystemExit(75)
if float((child.get('harcama_alarmi') or {}).get('ts') or 0) > float(child.get('harcama_onayi_ts') or 0):
    raise SystemExit(75)
with (store.home / 'observations.jsonl').open('a') as output:
    output.write(json.dumps({'parent':parent['run_id'],'child':child['run_id'],
        'feature':parent.get('project_active_feature'),'project':project['durum']}) + '\\n')
''')
        self.assertNotEqual(execute_run(self.store, self.ana), 0)
        record = self.store.read(self.ana)
        child = record['project_active_run']
        state = json.loads((self.store.path(self.ana) / 'proje.json').read_text())
        self.assertEqual((record['state'], state['durum'], state['neden']), ('retryable', 'durdu', 'harcama-alarmi'))
        self.assertNotEqual(execute_run(self.store, self.ana, devam=True), 0)
        self.assertFalse((self.home / 'observations.jsonl').exists())
        approve_spending(self.store, self.ana)
        self.assertEqual(self.store.read(self.ana)['state'], 'retryable')
        self.assertNotIn('calisiyor', proje.durum_ozeti(self.store, 'p'))
        before_wait = self.store.read(child)['retry_after']
        self.assertNotEqual(execute_run(self.store, self.ana, devam=True), 0)
        self.assertEqual(self.store.read(child)['retry_after'], before_wait)
        self.assertEqual(self.store.read(self.ana)['project_active_run'], child)
        self.assertEqual(self.store.read(child)['state'], 'retryable')
        waiting = json.loads((self.store.path(self.ana) / 'proje.json').read_text())
        self.assertEqual((waiting['neden'], waiting['ardisik_basarisiz'], waiting['ozellikler'][0]['deneme']), ('kapasite-bekleme', 0, 0))
        self.assertFalse((self.home / 'observations.jsonl').exists())
        import time
        self.store.update(child, retry_after=time.time() - 1)
        self.store.update(self.ana, retry_after=time.time() - 1)
        self.assertEqual(execute_run(self.store, self.ana, devam=True), 0)
        record = self.store.read(self.ana)
        state = json.loads((self.store.path(self.ana) / 'proje.json').read_text())
        rows = [json.loads(line) for line in (self.home / 'observations.jsonl').read_text().splitlines()]
        self.assertEqual([r['feature'] for r in rows], ['f1', 'f2', None])
        self.assertEqual(rows[0]['child'], child)
        self.assertEqual((record['state'], state['durum'], state['neden']), ('completed', 'tamam', 'son-kabul-gecti'))
        self.assertEqual([self.store.read(r['child'])['state'] for r in rows], ['implemented', 'implemented', 'completed'])
        before = (self.home / 'observations.jsonl').read_bytes()
        self.assertEqual(execute_run(self.store, self.ana), 0)
        self.assertEqual((self.home / 'observations.jsonl').read_bytes(), before)
        self.assertEqual(git(self.repo, 'rev-parse', 'main'), self.taban)

    def test_real_project_tool_environment_wait_preserves_child_and_blocks_automatic_revival(self):
        from kosu import execute_run, revive
        self.parent_engine('''import os, sys
from pathlib import Path
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
from oturum import Attempt
if not (Path(os.environ['CADRE_HOME']) / 'supported-environment').exists():
    attempt = Attempt('claude', 'impl', 'fixture/worker', 'work')
    attempt.launch([sys.executable, '-c', 'raise SystemExit(99)'], os.environ['CADRE_WORKTREE'],
                   Path(os.environ['CADRE_HOME']) / 'tool-wait.log', dict(os.environ), ())
''')
        self.assertNotEqual(execute_run(self.store, self.ana), 0)
        record = self.store.read(self.ana)
        child = record['project_active_run']
        state = json.loads((self.store.path(self.ana) / 'proje.json').read_text())
        self.assertEqual((record['state'], state['durum'], state['neden']),
                         ('retryable', 'durdu', 'project-tool-sandbox-unavailable'))
        self.assertEqual(self.store.read(child)['tool_boundary_wait'], state['neden'])
        self.assertEqual((state['ardisik_basarisiz'], state['ozellikler'][0]['deneme']), (0, 0))
        self.assertFalse((Path(self.store.read(child)['worktree']) / 'urun.txt').exists())
        with patch('kosu.subprocess.Popen') as start:
            self.assertEqual(revive(self.store, self.home / 'motor'), [])
            start.assert_not_called()
        self.assertNotEqual(execute_run(self.store, self.ana, devam=True), 0)
        self.assertEqual(self.store.read(self.ana)['project_active_run'], child)
        again = json.loads((self.store.path(self.ana) / 'proje.json').read_text())
        self.assertEqual(again['ozellikler'][0]['deneme'], 0)
        (self.home / 'supported-environment').touch()
        self.assertEqual(execute_run(self.store, self.ana, devam=True), 0)
        self.assertIsNone(self.store.read(child).get('tool_boundary_wait'))
        final = self.store.read(self.ana)
        self.assertEqual(final['state'], 'completed')
        rows = (Path(final['worktree']) / 'urun.txt').read_text().splitlines()
        self.assertEqual(rows.count(child), 1)
        self.assertEqual(git(self.repo, 'rev-parse', 'main'), self.taban)

    def test_real_parent_cancel_finishes_child_and_project_without_resurrection(self):
        import time
        from kosu import FileLock, active_runs, iptal_et, process_identity, revive
        self.parent_engine('''import os, pathlib, time
home = pathlib.Path(os.environ['CADRE_HOME'])
(home / 'live-child').write_text(os.environ['CADRE_RUN_ID'])
time.sleep(120)
''')
        root = self.home / 'motor'
        foreign = self.store.accept('other', 'unrelated')['run_id']
        other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], start_new_session=True)
        owner = FileLock(self.store.path(foreign) / '.owner.lock')
        self.assertTrue(owner.acquire(blocking=False))
        self.store.update(foreign, state='running', owner_pid=other.pid, owner_identity=process_identity(other.pid))
        proc = subprocess.Popen([sys.executable, str(root / 'lib/kosu.py'), 'run', self.ana],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            until = time.monotonic() + 15
            marker = self.home / 'live-child'
            while not marker.exists() and proc.poll() is None and time.monotonic() < until:
                time.sleep(.02)
            self.assertTrue(marker.exists(), self.store.read(self.ana))
            cid = marker.read_text()
            self.assertTrue(iptal_et(self.store, self.store.read(self.ana)))
            self.assertEqual(proc.wait(timeout=15), 130)
            self.assertEqual(self.store.read(self.ana)['state'], 'cancelled')
            self.assertEqual(self.store.read(cid)['state'], 'cancelled')
            state = json.loads((self.store.path(self.ana) / 'proje.json').read_text())
            self.assertEqual((state['durum'], state['neden']), ('durdu', 'iptal'))
            self.assertEqual(active_runs(self.store), [foreign])
            self.assertIsNone(other.poll())
            self.assertEqual(self.store.read(foreign)['state'], 'running')
            with patch('kosu._baslat') as start:
                revive(self.store, root)
                start.assert_not_called()
        finally:
            owner.close()
            other.terminate()
            other.wait(timeout=5)
            if proc.poll() is None:
                from qe_core import kill_tree
                kill_tree(proc.pid)
                proc.wait(timeout=15)

    def test_olculmus_son_hakem_yolu_yokken_proje_kapasite_beklemesinde_kalir_ve_kabul_uretmez(self):
        from sonuc import approved
        self.or_fiyat.unlink()
        rc = proje.baslat(self.ana, 'p', planla=self.planla, calistir=self.calistir)
        durum = json.loads((self.store.path(self.ana) / 'proje.json').read_text())
        ana = self.store.read(self.ana)
        self.assertNotEqual(rc, 0)
        self.assertEqual((durum['durum'], durum['neden']), ('durdu', 'kapasite-bekleme'))
        cocuk = self.store.read(ana['project_active_run'])
        self.assertEqual(cocuk['state'], 'retryable')
        self.assertGreater(float(cocuk.get('retry_after') or 0), time.time())
        self.assertFalse(approved(ana))
        self.assertFalse(approved(cocuk))
        self.assertNotIn('final_acceptance_run_id', ana)

    def test_cocuklar_gercek_kosuyla_ara_dalda_birikir_ve_ana_kosu_kabul_kapisindan_gecer(self):
        from sonuc import approved
        rc = proje.baslat(self.ana, 'p', planla=self.planla, calistir=self.calistir)
        ana = self.store.read(self.ana)
        durum = json.loads((self.store.path(self.ana) / 'proje.json').read_text())
        cocuklar = [c['kosu'] for c in durum['cocuklar']]
        kayit = {c: self.store.read(c) for c in cocuklar}
        ozet = {c: (k['state'], (k.get('final_result') or {}).get('reason')) for c, k in kayit.items()}
        self.assertEqual(rc, 0, f"{durum.get('neden')} {ozet}")
        self.assertEqual([c['ozellik'] for c in durum['cocuklar']], ['f1', 'f2', 'son-kabul'])
        # B346/K6 [03.10]: feature children are 'implemented' (an intermediate state), only the final acceptance is 'completed' — both are success
        self.assertTrue(all(k['state'] in ('completed', 'implemented') and k['proje_ust'] == self.ana for k in kayit.values()), ozet)
        dal = ana['proje']['ara_dal']
        self.assertEqual(git(self.repo, 'show', f'{dal}:urun.txt').splitlines(), cocuklar, 'her cocuk oncekinin ustune yazdi')
        self.assertEqual(git(self.repo, 'rev-parse', 'main'), self.taban, 'kanonik dal yerinde')
        self.assertTrue(approved(ana), 'koordinatorun kabul kapisi ana kosuda son kabulun kanitini PASS gorur')
        self.assertEqual(ana['final_acceptance_run_id'], cocuklar[-1])
        from yonlendirici import OR_SOL
        self.assertEqual(kayit[cocuklar[-1]]['final_result']['review']['model'], OR_SOL,
                         'varsayilan proje son kabulu olculmus GPT/FLEX modelini gercek yurutucuya vermeli')
        parent_prompt = Path(ana['prompt'])
        original = parent_prompt.read_bytes()
        parent_prompt.write_text('changed original project goal')
        self.assertFalse(approved(ana))
        parent_prompt.write_bytes(original)
        self.assertTrue(approved(ana))
        self.store.update(self.ana, final_acceptance_run_id=cocuklar[0])
        self.assertFalse(approved(self.store.read(self.ana)))


if __name__ == '__main__':
    unittest.main()
