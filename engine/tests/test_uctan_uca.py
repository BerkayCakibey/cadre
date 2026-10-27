"""END TO END (fake data): `cadre gorev` -> coordinator -> dispatcher (fake planner) -> lanes (fake worker, local+cloud)
-> QA (real command) -> quality (fake judge) -> acceptance -> ledger -> rotation archive -> restore. QA-red and
cancel scenarios on the same path. A single fake `opencode`; a real bin/lib copy, only butce.py is fake (no network). No model/Windows/Claude."""

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

LIB = Path(__file__).resolve().parents[1] / 'lib'
ENGINE = LIB.parent
sys.path.insert(0, str(LIB))
from kosu import RunStore  # noqa: E402

FAKE_OPENCODE = f'''#!{sys.executable}
import json, os, pathlib, re, sys, time
args = sys.argv[1:]
home = pathlib.Path(os.environ['CADRE_HOME'])
task = os.environ.get('CADRE_GOREV_ID', '')
req = os.environ.get('CADRE_REQUEST_DIR')
if req:   # gercek CLI'da cadre-plugin.mjs araclari kusanirken tools-armed.json makbuzunu yazar; sahte CLI ayni makbuzu birakir
    adir = pathlib.Path(req).parent; pol = adir / 'tool-policy.json'; marker = adir / 'tools-armed.json'
    if pol.exists() and not marker.exists():
        p = json.loads(pol.read_text())
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        os.write(fd, json.dumps({{'owner': p.get('owner'), 'pid': os.getpid(), 'profile_sha256': __import__('hashlib').sha256(str(p.get('profile', '')).encode()).hexdigest()}}).encode()); os.close(fd)
prompt = args[-1]
if prompt.startswith('Kaldigin yerden devam et'):   # resumed session: the real CLI keeps the task in context; the fake re-reads it
    prompt = (pathlib.Path(os.environ['CADRE_RUN_DIR'])/'tasks'/task/'prompt.md').read_text()
    role = 'worker-resume'
else:
    role = ('plan-review' if prompt.startswith('PLAN ISTEK UYUMU') else 'worker' if '[QE-GOREV v1]' in prompt else 'planner' if 'PLANLAYICISIN' in prompt else
            'review' if 'KALITE VE KABUL' in prompt else 'repair' if 'QA KAPISI KIRMIZI' in prompt else 'other')
with (home/'opencode-calls.jsonl').open('a') as f: f.write(json.dumps({{'cwd': os.getcwd(), 'args': args, 'task': task, 'role': role}})+'\\n')
sid = args[args.index('--session')+1] if '--session' in args else 'ses_' + __import__('uuid').uuid4().hex[:12]   # pid tabanli id 8 sa maratonda pid geri donusuyle baska kosunun oturumuyla cakisti (04:33); gercek CLI rastgele id verir
if os.environ.get('E2E_REUSE_REVIEW_SESSION') == '1' and role in ('plan-review', 'review'):
    sid = 'ses_reused_review'
def event(text):
    print(json.dumps({{'type':'text','sessionID':sid,'part':{{'type':'text','text':text}}}}), flush=True)
print(json.dumps({{'type':'step_start','sessionID':sid,'part':{{'type':'step-start'}}}}), flush=True)
if prompt.startswith('PLAN ISTEK UYUMU'):
    question=json.loads(prompt.splitlines()[-1])
    pathlib.Path('README.md').read_text()
    answer={{'verdict':os.environ.get('E2E_PLAN_VERDICT','GECER'),'revision':question['revision'],
             'acceptance':[{{'id':c['id'],'status':'PASS','evidence':'README.md source and original request inspected'}} for c in question['criteria']]}}
    event(json.dumps(answer)); sys.exit(0)
if prompt.startswith('Kaynak dosyalarini oku;'):
    question=json.loads(prompt.splitlines()[-1]); pathlib.Path('README.md').read_text()
    plan=question['schema']; plan['is_turu']='code'
    if os.environ.get('E2E_VERIFIED_BACKLOG') == '1':
        plan.update(kind='verified-backlog',gorevler=[],source_items=[{{'id':i['id'],'source_sha256':i['sha256'],'tasks':[],
                    'status':'verified','evidence':'a.py and b.py already contain TAMAM; run the actual acceptance command'}} for i in question['backlog']['items']])
        event(json.dumps(plan)); sys.exit(0)
    plan['gorevler']=[{{'id':'g1','tanim':'a.py yaz','dosyalar':['a.py'],'kabul':'a.py py_compile gecer','bagimli':[],'zorluk':2,'model':'auto','paralel_grup':0,'tahmin_dk':1,'tahmin_usd':0}},
                      {{'id':'g2','tanim':'b.py yaz','dosyalar':['b.py'],'kabul':'b.py py_compile gecer','bagimli':['g1'],'zorluk':2,'model':'auto','paralel_grup':0,'tahmin_dk':1,'tahmin_usd':0}}]
    plan['source_items']=[{{'id':i['id'],'source_sha256':i['sha256'],'tasks':['g1','g2'],'status':'pending','evidence':'original backlog inspected'}} for i in question['backlog']['items']]
    event(json.dumps(plan)); sys.exit(0)
if 'SEN BIR GOREV PLANLAYICISIN' in prompt:
    context=json.loads(prompt.splitlines()[-1])
    source=pathlib.Path('README.md').read_text()
    (home/'legacy-source-read.json').write_text(json.dumps({{'cwd':os.getcwd(),'source':source,'context':context,'prompt':prompt,'config':json.loads(os.environ['OPENCODE_CONFIG_CONTENT'])}}))
    if os.environ.get('E2E_PLAN_SOURCE_WRITE') == '1':
        pathlib.Path('README.md').write_text('unauthorized source change'); sys.exit(1)
    is_id = re.search(r'"is_id":"([^"]+)"', prompt).group(1)
    if os.environ.get('E2E_PLAN'):   # kontrol defteri: plani disaridan ver (zorluk/model atamasi senaryolari)
        plan = json.loads(os.environ['E2E_PLAN']); plan['is_id'] = is_id
        event('PLAN:\\n' + json.dumps(plan)); sys.exit(0)
    plan = {{'is_id': is_id, 'gorevler': [
        {{'id':'g1','tanim':'a.py yaz','dosyalar':['a.py'],'kabul':'py_compile gecer','kapsam_disi':[],'bagimli':[],'zorluk':2,'model':'auto','paralel_grup':0,'tahmin_dk':1,'tahmin_usd':0}},
        {{'id':'g2','tanim':'b.py yaz','dosyalar':['b.py'],'kabul':'py_compile gecer','kapsam_disi':[],'bagimli':['g1'],'zorluk':3,'model':'ds-pro','paralel_grup':1,'tahmin_dk':1,'tahmin_usd':0.01}}],
        'birlestirme_sirasi':['g1','g2'], 'kalite':{{'model':'oz','kapsam':'hepsi'}}}}
    event('PLAN:\\n' + json.dumps(plan)); sys.exit(0)
if '[QE-GOREV v1]' in prompt:
    files = re.search(r'DOSYALAR \\(SADECE bunlara dokun\\): (.*)', prompt).group(1).split(', ')
    if os.environ.get('E2E_SLOW') == '1':
        (home/'worker-started').write_text(str(os.getpid())); time.sleep(30)
    for name in files:
        if os.environ.get('E2E_SYNTAX_BROKEN', '') and name.startswith(os.environ['E2E_SYNTAX_BROKEN']):
            pathlib.Path(name).write_text('def f(:\\n'); continue
        broken = os.environ.get('E2E_BROKEN', '') and name.startswith(os.environ['E2E_BROKEN'])
        # Broken = derlenir ama QA'nin aradigi TAMAM isareti yok (sozdizimi bozuk olsa erken dogrulama QA'ya varmadan durdurur).
        pathlib.Path(name).write_text(f'print("{{name}} BOZUK")\\n' if broken else f'print("{{name}} TAMAM")\\n')
    if os.environ.get('E2E_EXTRA_BROKEN') == '1':   # bildirilmemis dosyada sozdizimi hatasi: hizli-dogrula gormez, QA gormeli
        pathlib.Path(os.environ.get('E2E_EXTRA_BROKEN_FILE', 'bozuk.py')).write_text('def f(:\\n')
    event('yazildi'); sys.exit(0)
if 'QA KAPISI KIRMIZI' in prompt:
    event('duzeltme denendi (fixture dokunmaz)'); sys.exit(0)
if 'KALITE VE KABUL KONTROLU' in prompt:
    if os.environ.get('E2E_SLOW_FINAL_REVIEW') == '1':
        (home/'final-review-started').write_text(str(os.getpid())); time.sleep(30)
    question = json.loads(prompt.splitlines()[-1])
    if 'source_backlog' in question:
        source=pathlib.Path(question['source_backlog']['full_path']).read_bytes()
        (home/'final-backlog-read.json').write_text(json.dumps({{'bytes':len(source),'sha256':__import__('hashlib').sha256(source).hexdigest()}}))
    answer = {{'verdict': os.environ.get('E2E_VERDICT', 'GECER'), 'revision': question['revision'],
              'acceptance': [{{'id': c['id'], 'status': 'PASS', 'evidence': 'dosya derlendi; QA rc=0'}} for c in question['criteria']]}}
    event(json.dumps(answer)); sys.exit(0)
event('bilinmeyen rol'); sys.exit(1)
'''

FAKE_BUTCE = '''import json, time
print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "bugun": time.strftime("%Y-%m-%d"), "kalan_gun": 10,
  "go": {"db":"ok","pay":33.333334,"kullanilabilir":33.333334,"pencere_acik":True,"oran":1.0, "birim": "puan"},
  "zen": {"db":"ok","pay":4.0,"kullanilabilir":4.0,"kalan":4.0,"oran":1.0}, "or": {"db":"ok","pay":4.0,"kullanilabilir":4.0,"kalan":4.0},
  "gemini": {"db":"ok","bugun_istek":10,"kalan_istek":440,"oran":0.9}, "groq": {"db":"ok","bugun_istek":0,"oran":1.0},
  "claude": {"kaynak":"taze","gecerli":True,"pay":0,"yuzde1_usd":30,"havuz_kalan_usd":0,"fable_bugun":0,"fable_attempts":[]}}))
'''


class E2EFixture(unittest.TestCase):
    """Isolated home + isolated code root + fake opencode/budget. The tests are in subclasses (inheriting packages do not re-run them)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-e2e-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        self.root = Path(self.temp.name) / 'engine'
        from qa_fixture import instructions
        instructions(self.root)
        for sub in ('bin', 'lib', 'hooks'):
            (self.root / sub).mkdir(parents=True)
            for path in (ENGINE / sub).iterdir():
                if path.is_file():
                    shutil.copy(path, self.root / sub / path.name)
                elif path.is_dir() and path.name != '__pycache__':
                    shutil.copytree(path, self.root / sub / path.name, ignore=shutil.ignore_patterns('__pycache__'))
        (self.root / 'lib/butce.py').write_text(FAKE_BUTCE)          # the only fixture inside the engine: no network
        self.fake = Path(self.temp.name) / 'opencode'
        self.fake.write_text(FAKE_OPENCODE)
        self.fake.chmod(0o700)
        for d in ('zincir', 'logs', 'hooks', 'projects/e2e'):
            (self.home / d).mkdir(parents=True)
        for name in ('pre-push', 'pre-commit', 'commit-msg'):
            shutil.copy(ENGINE / 'hooks' / name, self.home / 'hooks' / name)
            (self.home / 'hooks' / name).chmod(0o700)
        (self.home / 'zincir/maliyet-katsayi.txt').write_text('1.0\n')
        (self.home / 'zincir/go-gunluk-tavan.txt').write_text('2.0\n')
        self.repo = (Path(self.temp.name) / 'repo').resolve()
        self.repo.mkdir()
        self.git('init', '-q', '-b', 'main')
        (self.repo / 'README.md').write_text('e2e fixture\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        project = self.home / 'projects/e2e'
        (project / 'repo-path').write_text(str(self.repo))
        (project / 'base-branch').write_text('main')
        (project / 'POLICY.md').write_text('never push\n')
        from qa_fixture import command
        shutil.copy(Path(__file__).with_name('qa_fixture.py'), self.root / 'lib/qa_fixture.py')
        (project / 'qa-komut').write_text(command('python3 -m py_compile a.py b.py && grep -q TAMAM a.py && grep -q TAMAM b.py', self.root / 'lib/qa_fixture.py'))
        from test_kosu_sessions import KATALOG
        (self.home / 'models.json').write_text(json.dumps(KATALOG))
        cfg = Path(self.temp.name) / 'opencode-cfg'
        cfg.mkdir()
        (cfg / 'opencode.jsonc').write_text('{}')
        # This fake provider is not an acceptance calibration, it tests the engine loop; the final judge path is an explicit fixture input.
        self.env = {**os.environ, 'CADRE_ZINCIR_KALITE': 'opencode-go/qwen3.8-max', 'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(self.root), 'CADRE_REAL_OPENCODE': str(self.fake), 'CADRE_MODEL_KATALOG': str(self.home / 'models.json'), 'CADRE_RADAR': '0',
                    'CADRE_PROGRESS_POLL': '1', 'CADRE_STALL_SEC': '600', 'CADRE_ABORT_SEC': '600', 'CADRE_MAX_RESUME': '1',
                    'CADRE_ONELEK_LLM': '0', 'CADRE_IZLE': '0', 'OPENCODE_DB': str(self.home / 'absent.db'), 'CADRE_OC_LOG': str(self.home / 'oc.log'),
                    'OPENCODE_CFG': str(cfg), 'CLAUDE_BIN': '/nonexistent/claude', 'CADRE_BEKCI_SAHIP_PID': str(os.getpid()),   # B191
                    'CADRE_KRITIK_DOSYALAR': f"{self.home/'zincir/maliyet-katsayi.txt'}:{self.home/'zincir/go-gunluk-tavan.txt'}",
                    'CADRE_YON_KAYIT': str(self.home / 'zincir/yonlendirici.jsonl'), 'CADRE_YON_DURUM': str(self.home / 'zincir/saglayici-durum.json')}
        for key in ('CADRE_RUN_ID', 'CADRE_RUN_DIR', 'CADRE_TASKS_DIR', 'CADRE_GOREV_ID', 'CADRE_WORKTREE', 'CADRE_OWNER_FDS', 'CADRE_DISPATCH', 'CADRE_RUN_MODEL',
                    'CADRE_KOORDINATOR_PID'):
            self.env.pop(key, None)
        self.store = RunStore(self.home)
        # B32: the real cadre.sh/coordinator starts the REAL guards (saglik-nobet, kota-izle) from the isolated root; they are killed when the test ends
        self.addCleanup(self.kill_guards)

    def kill_guards(self):
        import signal
        for name in ('saglik-nobet', 'kota-izle'):
            try:
                pid = int((self.home / 'zincir' / f'{name}.pid').read_text().strip())
            except (OSError, ValueError):
                continue
            for target in (lambda: os.killpg(pid, signal.SIGKILL), lambda: os.kill(pid, signal.SIGKILL)):
                try:
                    target()
                except (ProcessLookupError, PermissionError):
                    pass
        subprocess.run(['pkill', '-9', '-f', str(self.root)], capture_output=True)

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=e2e', '-c', 'user.email=e2e@x', *args],
                              check=True, capture_output=True, text=True, timeout=20).stdout.strip()

    PROMPT = 'a.py ve b.py dosyalarini olustur, sonra ikisinin de derlendigini dogrula ve testi gecir\n'

    def submit(self, extra=None):
        result = subprocess.run(['bash', str(self.root / 'bin/cadre.sh'), 'gorev'], input=self.PROMPT + 'go\nevet\nevet\n', cwd=self.repo,
                                env={**self.env, **(extra or {})}, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Görev kaydedildi', result.stdout)
        records = self.store.list('e2e')
        return records[-1]['run_id']

    def wait_terminal(self, rid, timeout=150):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = self.store.read(rid)
            if record['state'] in ('completed', 'cancelled', 'failed', 'retryable'):
                if record['state'] == 'completed' and (record.get('final_result', {}).get('delivery') or {}).get('git_permission') == 'required':
                    from kosu import FileLock
                    owner = FileLock(self.store.path(rid) / '.owner.lock')
                    if not owner.acquire(blocking=False):
                        time.sleep(.1)
                        continue
                    owner.close()
                    token = record['final_result']['delivery']['action_token']
                    env = {key: value for key, value in self.env.items() if key != 'CADRE_RUN_ID'}
                    def operator(command, *extra):
                        response = subprocess.run([sys.executable, str(self.root / 'lib/izin.py'), command, rid, token, *extra],
                                                  env=env, capture_output=True, text=True, timeout=30)
                        self.assertEqual(response.returncode, 0, response.stderr)
                        return json.loads(response.stdout)
                    first = operator('approve')
                    operator('approve', first['confirmation'])
                    operator('commit')
                    operator('adopt-commit')
                    record = self.store.read(rid)
                return record
            time.sleep(.5)
        self.fail(f"run did not finish: {json.dumps(self.store.read(rid))[:600]}\n{(self.store.path(rid)/'coordinator.log').read_text()[-3000:]}")

    def calls(self):
        return [json.loads(l) for l in (self.home / 'opencode-calls.jsonl').read_text().splitlines()]


class EndToEndTests(E2EFixture):
    def test_legacy_planner_reads_actual_source_and_preserves_long_original_request(self):
        self.PROMPT = self.PROMPT + 'Kaynak kabulunu koru. ' * 700 + '\nTAIL-ACCEPTANCE-7721\n'
        rid = self.submit()
        record = self.wait_terminal(rid)
        self.assertEqual(record['state'], 'completed', (self.store.path(rid) / 'coordinator.log').read_text()[-3000:])
        read = json.loads((self.home / 'legacy-source-read.json').read_text())
        self.assertEqual(Path(read['cwd']).resolve(), self.repo.resolve())
        self.assertEqual(read['source'], (self.repo / 'README.md').read_text())
        self.assertIn('TAIL-ACCEPTANCE-7721', read['prompt'])
        self.assertIn(self.PROMPT.strip(), read['prompt'])
        self.assertEqual(read['context']['characters'], len(Path(record['prompt']).read_text()))
        self.assertEqual(read['config']['permission']['edit'], 'deny')
        self.assertEqual(read['config']['permission']['read'], 'deny')
        self.assertEqual(read['config']['permission']['bash'], 'allow')
        policies = [json.loads(p.read_text()) for p in self.store.path(rid).glob('tasks/*/attempts/*/tool-policy.json')]
        self.assertTrue(any(not p['writable'] and Path(p['worktree']).resolve() == self.repo.resolve() for p in policies))
        self.assertFalse(any('Hicbir arac kullanma' in c['args'][-1] for c in self.calls() if c['role'] == 'planner'))

    def test_legacy_planner_source_write_and_executor_failure_cannot_fall_through_to_worker(self):
        rid = self.submit({'E2E_PLAN_SOURCE_WRITE': '1'})
        record = self.wait_terminal(rid)
        self.assertNotEqual(record['state'], 'completed')
        self.assertEqual([c['role'] for c in self.calls()], ['planner'])
        self.assertFalse((self.store.path(rid) / 'plan.json').exists())
        self.assertEqual((self.repo / 'README.md').read_text(), 'unauthorized source change')
        self.assertIn('plan-review-source-modified', (self.store.path(rid) / 'dispatch.log').read_text())

    def test_full_pipeline_completes_only_with_real_qa_and_review_then_archives_and_restores(self):
        rid = self.submit()
        record = self.wait_terminal(rid)
        log = (self.store.path(rid) / 'coordinator.log').read_text()
        self.assertEqual(record['state'], 'completed', log[-2000:])
        self.assertEqual(record['final_result']['status'], 'PASS')
        self.assertEqual(record['final_result']['qa']['status'], 'PASS')
        self.assertEqual(record['final_result']['review']['verdict'], 'GECER')
        wt = Path(record['worktree'])
        self.assertEqual((wt / 'a.py').read_text(), 'print("a.py TAMAM")\n')
        self.assertEqual((wt / 'b.py').read_text(), 'print("b.py TAMAM")\n')
        self.assertEqual(record['tasks']['g1']['dispatch_result'], 'uretti')
        self.assertEqual(record['tasks']['g2']['dispatch_result'], 'uretti')
        roles = [c['args'][-1][:40] for c in self.calls()]
        self.assertTrue(any('PLANLAYICISIN' in c['args'][-1] for c in self.calls()))
        workers = [c for c in self.calls() if c['role'].startswith('worker')]
        self.assertEqual(len(workers), 2)
        g2 = [c for c in workers if c['task'] == 'g2'][0]
        # 18.09: the HEAD of the chain changes as it is measured (kimi-k2.7-code left the chain); the test looks at BEHAVIOUR,
        # not identity: a cloud task must really go out with a cloud model (must not fall to local).
        g2_model = g2['args'][g2['args'].index('-m') + 1]
        self.assertNotIn('lmstudio/', g2_model, 'bulut gorevi bulut modeliyle cikmali')
        self.assertIn('/', g2_model)
        self.assertTrue(all(Path(c['cwd']).resolve() == wt.resolve() for c in workers))
        review = [c for c in self.calls() if 'KALITE VE KABUL KONTROLU' in c['args'][-1]]
        self.assertEqual(len(review), 1)
        # Every worker got the project memory and no harness file entered the product commit.
        committed = subprocess.run(['git', '-C', str(wt), 'show', '--name-only', '--format=', 'HEAD'], capture_output=True, text=True).stdout.split()
        self.assertEqual(sorted(committed), ['a.py', 'b.py'])   # B16: verification/QA leftovers (__pycache__) do not enter the commit
        # Ledger scanner archives the run; rotation refuses first (too young), then archives after aging; restore is exact.
        env = {**self.env, 'OPENCODE_DB': str(self.home / 'absent.db')}
        ledger = subprocess.run([sys.executable, str(self.root / 'lib/kosu_defteri.py'), 'tara'], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(ledger.returncode, 0, ledger.stderr)
        report = self.home / 'reports' / f'kosu-e2e-{wt.name}'
        meta = json.loads((report / 'meta.json').read_text())
        self.assertEqual(meta['run_id'], rid)
        self.assertTrue((report / 'attempts/g1').exists() and (report / 'attempts/g2').exists())
        sys.path.insert(0, str(self.root / 'lib'))
        import importlib
        rotasyon = importlib.import_module('rotasyon')
        arsiv = importlib.import_module('arsiv')
        self.assertEqual(rotasyon.rotate(self.home, keep=0)['archived'], 0)      # younger than 2 days: retained
        old = time.time() - 3 * 86400
        os.utime(wt, (old, old))
        import izin
        action = izin.cleanup_proposal(self.store, rid, wt)
        izin.approve(self.store, rid, action['token'])
        self.assertEqual(rotasyon.rotate(self.home, keep=0)['archived'], 1)
        self.assertFalse(wt.exists())
        arsiv.restore_run_worktree(self.store, rid)
        self.assertEqual((wt / 'b.py').read_text(), 'print("b.py TAMAM")\n')
        status = subprocess.run(['git', '-C', str(wt), 'status', '--porcelain'], capture_output=True, text=True).stdout
        self.assertEqual([l for l in status.splitlines() if not l.startswith('??')], [])   # restored tree: no tracked drift

    def test_red_qa_never_becomes_success_and_keeps_the_product(self):
        rid = self.submit({'E2E_BROKEN': 'b.py'})
        record = self.wait_terminal(rid)
        self.assertEqual(record['state'], 'retryable')
        self.assertNotEqual(record.get('final_result', {}).get('status'), 'PASS')
        self.assertEqual(record['final_result']['qa']['status'], 'FAIL')
        self.assertEqual([t['imza'][:3] for t in record.get('duzeltme') or []], ['qa:'])   # G-C16: one correction round, the same error -> stop
        self.assertEqual(len(record['final_result']['qa']['attempts']), 1)   # after the loop started there is no inner repair at the gate
        self.assertEqual((Path(record['worktree']) / 'b.py').read_text(), 'print("b.py BOZUK")\n')   # product kept for inspection
        self.assertFalse(any('KALITE VE KABUL KONTROLU' in c['args'][-1] for c in self.calls()))   # no review on red QA
        self.assertEqual(len([c for c in self.calls() if 'QA KAPISI KIRMIZI' in c['args'][-1]]), 1)   # a single inner repair at the first acceptance
        self.assertEqual(len([c for c in self.calls() if 'DUZELTME TURU' in c['args'][-1]]), 1)   # then a single correction round

    def test_syntax_broken_product_is_caught_before_qa_and_escalated_then_fails(self):
        rid = self.submit({'E2E_SYNTAX_BROKEN': 'b.py'})
        record = self.wait_terminal(rid)
        self.assertEqual(record['state'], 'retryable')
        self.assertNotIn('final_result', record)                              # never reached the acceptance gate
        self.assertEqual(record['tasks']['g2']['dispatch_result'], 'basarisiz')
        workers = [c for c in self.calls() if c['task'] == 'g2' and c['role'].startswith('worker')]
        dispatch_log = (self.store.path(rid) / 'dispatch.log').read_text()
        self.assertGreaterEqual(len(workers), 1, dispatch_log[-1500:])
        self.assertTrue(all('-m' in c['args'] and not c['args'][c['args'].index('-m') + 1].startswith('lmstudio/') for c in workers))
        self.assertTrue(all('--session' in c['args'] for c in workers[1:]))
        self.assertIn('bulut zinciri tukendi', dispatch_log)

    def test_duzelt_verdict_is_not_success(self):
        # G-C16: DUZELT triggers the correction round; the fixture executor does not change the product in the correction prompt -> the B193 lock, permanent FAIL
        rid = self.submit({'E2E_VERDICT': 'DUZELT'})
        record = self.wait_terminal(rid)
        self.assertEqual(record['state'], 'failed')
        self.assertEqual(record['final_result']['status'], 'FAIL')
        self.assertEqual(record['final_result']['reason'], 'revision-already-rejected')
        self.assertEqual([t['imza'][:6] for t in record.get('duzeltme') or []], ['hakem:'])

    def test_cancel_during_a_lane_stops_everything_and_marks_cancelled(self):
        rid = self.submit({'E2E_SLOW': '1', 'CADRE_KILL_GRACE': '2'})
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not (self.home / 'worker-started').exists():
            time.sleep(.2)
        self.assertTrue((self.home / 'worker-started').exists(), (self.store.path(rid) / 'coordinator.log').read_text()[-2000:])
        worker = int((self.home / 'worker-started').read_text())
        cancel = subprocess.run(['bash', str(self.root / 'bin/cadre-iptal')], env=self.env, capture_output=True, text=True, timeout=20)
        self.assertIn(rid, cancel.stdout)
        started = time.monotonic()
        record = self.wait_terminal(rid, timeout=90)
        self.assertEqual(record['state'], 'cancelled')
        self.assertLess(time.monotonic() - started, 60)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and subprocess.run(['ps', '-o', 'stat=', '-p', str(worker)], capture_output=True, text=True).stdout.strip() not in ('', 'Z', 'Z+'):
            time.sleep(.2)
        self.assertIn(subprocess.run(['ps', '-o', 'stat=', '-p', str(worker)], capture_output=True, text=True).stdout.strip(), ('', 'Z', 'Z+'))
        self.assertFalse(any('KALITE VE KABUL KONTROLU' in c['args'][-1] for c in self.calls()))


if __name__ == '__main__':
    unittest.main()
