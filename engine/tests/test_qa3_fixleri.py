"""Backlog closing round (15.10.2026): local acceptance tests for FIX-17/25/26/51/52/53 and CR-28/30/33."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kosu import RunStore  # noqa: E402

SANDBOX = '/usr/bin/sandbox-exec'
GIT_KIMLIK = ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid']


def git(repo: Path, *args: str) -> str:
    return subprocess.run(['git', '-C', str(repo), *GIT_KIMLIK, *args], check=True, capture_output=True, text=True).stdout.strip()


class KosuSandboxFixture(unittest.TestCase):
    """The real `project_tool_policy` profile: FIX-25/26 are tested with the run sandbox itself."""

    def setUp(self) -> None:
        if sys.platform != 'darwin' or not Path(SANDBOX).is_file():
            self.skipTest('macOS sandbox-exec yok')
        temp = tempfile.TemporaryDirectory(prefix='qa3-sandbox-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        self.store = RunStore(self.home)
        repo = self.home / 'source'
        repo.mkdir()
        git(repo, 'init', '-q')
        (repo / 'app.py').write_text('value = 1\n')
        git(repo, 'add', 'app.py')
        git(repo, 'commit', '-qm', 'fixture')
        self.wt = self.home / 'urun'
        git(repo, 'worktree', 'add', '-qb', 'cadre/qa3', str(self.wt))
        parent = self.store.accept('a', 'project')['run_id']
        self.store.update(parent, proje={'kimlik': 'fixture'})
        self.rid = self.store.accept('a', 'child')['run_id']
        self.store.update(self.rid, proje_ust=parent, worktree=str(self.wt))
        self.env = {**os.environ, **self.store.environment(self.rid), 'CADRE_GOREV_ID': 'g1', 'CADRE_ENGINE_ROOT': str(LIB.parent),
                    'HOME': str(self.home / 'tool-home')}
        Path(self.env['HOME']).mkdir()
        from oturum import project_tool_policy
        directory = self.store.path(self.rid) / 'tasks/g1/attempts' / uuid.uuid4().hex
        policy = project_tool_policy(self.store, self.store.read(self.rid), directory, str(self.wt), 'opencode', 'impl', self.env)
        assert policy is not None
        self.profile = policy['profile']

    def sandbox(self, *argv: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([SANDBOX, '-p', self.profile, *argv], capture_output=True, text=True, timeout=120,
                              cwd=self.wt, env=self.env if env is None else env)


class CanliDuzenOkumaTests(KosuSandboxFixture):
    """N-ENV02 (audit C-13/R-24): when the installed code is in the data root (CADRE_HOME/lib) the tool sandbox reads only the code directories,
    not the whole data root; chain backups, key files, other worktrees and records cannot be read."""

    def canli_profil(self):
        import oturum
        import shutil
        shutil.copytree(LIB, self.home / 'lib')
        for name in ('bin', 'hooks', 'zincir/.imza-9.yedek', 'backups', 'reports/r1', 'worktrees/diger/w'):
            (self.home / name).mkdir(parents=True, exist_ok=True)
        self.dosyalar = {'kod': self.home / 'lib/ok.py', 'yedek': self.home / 'zincir/.imza-9.yedek/abc',
                         'anahtar': self.home / 'zincir/openrouter-anahtar', 'backup': self.home / 'backups/opencode.jsonc',
                         'rapor': self.home / 'reports/r1/defter.json', 'diger': self.home / 'worktrees/diger/w/x.txt'}
        for path in self.dosyalar.values():
            path.write_text('gizli\n')
        from unittest.mock import patch
        directory = self.store.path(self.rid) / 'tasks/g1/attempts' / uuid.uuid4().hex
        with patch.object(oturum, 'kod_koku', return_value=self.home):
            policy = oturum.project_tool_policy(self.store, self.store.read(self.rid), directory, str(self.wt), 'opencode', 'impl', self.env)
        assert policy is not None
        self.profile = policy['profile']

    def test_live_layout_reads_only_engine_code_not_the_whole_data_root(self):
        self.canli_profil()
        self.assertNotIn(f'(subpath "{self.home}")', self.profile)
        self.assertEqual(self.sandbox('cat', str(self.dosyalar['kod'])).returncode, 0)
        self.assertEqual(self.sandbox('cat', str(self.wt / 'app.py')).returncode, 0)
        for ad in ('yedek', 'anahtar', 'backup', 'rapor', 'diger'):
            sonuc = self.sandbox('cat', str(self.dosyalar[ad]))
            self.assertNotEqual(sonuc.returncode, 0, ad)
            self.assertNotIn('gizli', sonuc.stdout, ad)


class HardlinkSinirTests(KosuSandboxFixture):
    def test_run_sandbox_denies_new_hardlinks_while_ordinary_writes_stay_allowed(self):
        """FIX-25 (N-ENV02): `(deny file-link)` applies at runtime too; no skipped directory scan is needed."""
        self.assertIn('(deny file-link)', self.profile)
        self.assertNotIn('(allow file-link', self.profile.split('(deny file-link)', 1)[1])
        (self.wt / 'a.txt').write_text('x')
        link = self.sandbox('/usr/bin/python3', '-c', "import os;os.link('a.txt','b.txt')")
        self.assertNotEqual(link.returncode, 0)
        self.assertIn('Operation not permitted', link.stderr)
        self.assertFalse((self.wt / 'b.txt').exists())
        self.assertNotEqual(self.sandbox('/bin/ln', 'a.txt', 'c.txt').returncode, 0)
        self.assertFalse((self.wt / 'c.txt').exists())
        for folder in ('node_modules/pkg', '.venv/lib'):
            (self.wt / folder).mkdir(parents=True)
            (self.wt / folder / 'dep.bin').write_text('dependency')
            self.assertNotEqual(self.sandbox('/bin/ln', f'{folder}/dep.bin', f'{folder}/alias.bin').returncode, 0)
            self.assertFalse((self.wt / folder / 'alias.bin').exists())
        write = self.sandbox('/usr/bin/python3', '-c', "open('d.txt','w').write('ok')")
        self.assertEqual(write.returncode, 0, write.stderr)
        self.assertEqual((self.wt / 'd.txt').read_text(), 'ok')
        os.link(self.wt / 'a.txt', self.wt / 'e.txt')
        self.assertEqual((self.wt / 'e.txt').stat().st_nlink, 2)

    def test_preexisting_external_aliases_in_skipped_directories_are_refused_before_the_sandbox(self):
        """FIX-25 (Codex P1): a hardlink pointing outside the tree, placed into `.cache`/`node_modules`/`.venv`/`__pycache__` before the sandbox,
        is rejected in the preflight; pairs inside the tree (pnpm) are allowed; the profile also closes writes to install directories (`.venv`, `node_modules`),
        cache directories stay writable for real QA tools (protected only by the preflight)."""
        import oturum
        external = self.home / 'outside-product.txt'
        external.write_text('SYNTHETIC-OUTSIDE-UNCHANGED\n')
        self.assertFalse(oturum.urun_hardlink_var(self.wt))
        for folder in ('.cache', 'node_modules/pkg', '.venv/bin', '__pycache__', '.ruff_cache'):
            with self.subTest(folder=folder):
                (self.wt / folder).mkdir(parents=True, exist_ok=True)
                alias = self.wt / folder / 'preexisting.txt'
                os.link(external, alias)
                self.assertTrue(oturum.urun_hardlink_var(self.wt), 'agac disina giden alias guvensizdir')
                with self.assertRaisesRegex(PermissionError, 'project-tool-hardlink-untrusted'):
                    oturum.project_tool_policy(self.store, self.store.read(self.rid), self.store.path(self.rid) / 'tasks/g1/attempts' / uuid.uuid4().hex,
                                               str(self.wt), 'opencode', 'impl', self.env)
                if folder in ('node_modules/pkg', '.venv/bin'):
                    probe = self.sandbox('/usr/bin/python3', '-c', f"open({str(alias.relative_to(self.wt))!r},'w').write('SYNTHETIC-MODIFIED-VIA-HARDLINK')")
                    self.assertNotEqual(probe.returncode, 0, 'profil kurulum dizinine yazmayi kapatir')
                    self.assertEqual(external.read_text(), 'SYNTHETIC-OUTSIDE-UNCHANGED\n')
                alias.unlink()
        self.assertFalse(oturum.urun_hardlink_var(self.wt))
        (self.wt / 'node_modules/pkg/linked.bin').write_text('dependency')
        os.link(self.wt / 'node_modules/pkg/linked.bin', self.wt / 'node_modules/pkg/alias.bin')
        self.assertFalse(oturum.urun_hardlink_var(self.wt), 'agac ici cift serbest')
        (self.wt / 'src').mkdir()
        (self.wt / 'src/data.txt').write_text('product')
        os.link(self.wt / 'src/data.txt', self.wt / 'src/alias.txt')
        self.assertTrue(oturum.urun_hardlink_var(self.wt), 'urun dizininde her hardlink guvensiz')
        (self.wt / 'src/alias.txt').unlink()
        direct = self.sandbox('/usr/bin/python3', '-c', f"open({str(external)!r},'w').write('MODIFIED-DIRECT')")
        self.assertNotEqual(direct.returncode, 0)
        self.assertNotEqual(self.sandbox('/usr/bin/python3', '-c', "open('.venv/new.txt','w').write('x')").returncode, 0, 'kurulum dizinine yeni dosya da yazilamaz')
        self.assertFalse((self.wt / '.venv/new.txt').exists())
        self.assertEqual(self.sandbox('/usr/bin/python3', '-c', "open('.ruff_cache/tmp','w').write('x')").returncode, 0, 'onbellek dizini QA araclari icin yazilabilir')
        self.assertEqual(self.sandbox('/usr/bin/python3', '-c', "open('src/data.txt','a').write('!')").returncode, 0, 'urun dizini yazilabilir kalir')


class IcYurutucuTests(KosuSandboxFixture):
    def test_nested_executor_without_run_id_is_detected_and_has_no_network_or_tools(self):
        """FIX-26 (N-ENV02/N-BU04): a second executor opened with `env -u CADRE_RUN_ID` is inside the sandbox; no network, the plugin denies."""
        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                self.request.sendall(b'reached')
        with socketserver.TCPServer(('127.0.0.1', 0), Handler) as server:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.shutdown)
            port = server.server_address[1]
            probe = f"import socket;s=socket.create_connection(('127.0.0.1',{port}),2);print(s.recv(16).decode())"
            outside = subprocess.run(['/usr/bin/python3', '-c', probe], capture_output=True, text=True, timeout=30)
            self.assertEqual((outside.returncode, outside.stdout.strip()), (0, 'reached'))
            inside = self.sandbox('/usr/bin/env', '-u', 'CADRE_RUN_ID', '/usr/bin/python3', '-c', probe)
            self.assertNotEqual(inside.returncode, 0)
            self.assertNotIn('reached', inside.stdout)
        check = self.sandbox('/usr/bin/env', '-u', 'CADRE_RUN_ID', '/usr/bin/python3', str(LIB / 'oturum.py'), 'nested-check')
        self.assertEqual(check.returncode, 1, check.stderr)
        self.assertEqual(subprocess.run(['/usr/bin/python3', str(LIB / 'oturum.py'), 'nested-check'], capture_output=True).returncode, 0)
        script = self.wt / 'nested.mjs'
        script.write_text(f"""import plugin from {json.dumps((LIB / 'cadre-plugin.mjs').as_uri())};
const hooks = await plugin({{}});
const config = {{provider: {{openrouter: {{options: {{fetch: async () => 'reached'}}}}}}}};
await hooks.config(config);
const out = [];
try {{ out.push(await config.provider.openrouter.options.fetch('https://example.invalid/v1', {{body: '{{}}'}})); }} catch (e) {{ out.push(String(e)); }}
try {{ await hooks['tool.execute.before']({{tool: 'bash'}}, {{args: {{command: 'true'}}}}); out.push('tool-allowed'); }} catch (e) {{ out.push(String(e)); }}
console.log(JSON.stringify(out));
""")
        node = subprocess.run(['which', 'node'], capture_output=True, text=True).stdout.strip()
        env = {k: v for k, v in self.env.items() if k != 'CADRE_RUN_ID'}
        env.update(CADRE_PYTHON='/usr/bin/python3', OPENCODE_EXPERIMENTAL_NATIVE_LLM='false', CADRE_BUTCE_GOZLEM='1')
        result = self.sandbox(node, str(script), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertIn('CADRE_BUDGET_DENIED: nested executor inside a run sandbox', out[0])
        self.assertIn('CADRE_TOOL_DENIED: nested executor inside a run sandbox', out[1])
        plain = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60, cwd=self.wt, env=env)
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertEqual(json.loads(plain.stdout.strip().splitlines()[-1])[0], 'reached')



class QaSimTests(unittest.TestCase):
    def test_only_the_shim_directory_enters_pythonpath_and_engine_modules_stay_hidden(self):
        """FIX-52 (N-QA01): the project's QA process cannot import engine modules; a user module with the same name is not shadowed; the observer continues."""
        import qa_kaniti
        shim = LIB / 'qa-sim'
        self.assertEqual([p.name for p in shim.iterdir() if p.name != '__pycache__'], ['sitecustomize.py'])
        with tempfile.TemporaryDirectory(prefix='qa3-sim-') as tmp:
            evidence = Path(tmp) / 'evidence.jsonl'
            env = qa_kaniti.environment({'PATH': os.environ['PATH'], 'HOME': tmp}, evidence, 'tok')
            entries = env['PYTHONPATH'].split(os.pathsep)
            self.assertEqual(entries[0], str(shim))
            self.assertNotIn(str(LIB), entries)
            probe = ("import importlib.util, json, sys; print(json.dumps([importlib.util.find_spec('kosu') is None, "
                     "importlib.util.find_spec('butce_kapisi') is None, sys.modules['qa_kaniti'].__file__]))")
            out = subprocess.run([sys.executable, '-c', probe], env=env, capture_output=True, text=True, check=True).stdout
            self.assertEqual(json.loads(out), [True, True, str(LIB / 'qa_kaniti.py')])
            user = Path(tmp) / 'user'
            user.mkdir()
            (user / 'kosu.py').write_text("VALUE = 'user'\n")
            (user / 'test_user.py').write_text('import unittest, kosu\nclass T(unittest.TestCase):\n'
                                               " def test_user_module(self): self.assertEqual(kosu.VALUE, 'user')\n")
            result = subprocess.run([sys.executable, '-m', 'unittest', '-q', 'test_user'], cwd=user, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            events = [json.loads(line) for line in evidence.read_text().splitlines()]
            self.assertTrue(any(e['event'] == 'result' and e['status'] == 'PASS' and 'test_user_module' in e['id'] for e in events), events)
            other = Path(tmp) / 'other'
            other.mkdir()
            (other / 'sitecustomize.py').write_text("import os\nos.environ['CADRE_QA3_ZINCIR'] = '1'\n")
            chained = {**env, 'PYTHONPATH': str(shim) + os.pathsep + str(other)}
            out = subprocess.run([sys.executable, '-c', "import os;print(os.environ.get('CADRE_QA3_ZINCIR'))"],
                                 env=chained, capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(out, '1')
            silent = {k: v for k, v in env.items() if k not in ('CADRE_QA_EVIDENCE', 'CADRE_QA_TOKEN')}
            out = subprocess.run([sys.executable, '-c', "import sys;print('qa_kaniti' in sys.modules)"], env=silent,
                                 capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(out, 'False')


class IlerlemesizAlarmTests(unittest.TestCase):
    """FIX-53 / CR-03 (N-BU06): a speed warning alone is not a stop; productive / non-progressing / uncertain work are separated and only
    evidenced 'no progress' (product unchanged + no recorded step + a continuously expensive section) becomes a definite alarm."""

    def setUp(self) -> None:
        import butce_kapisi as bk
        from unittest.mock import patch
        temp = tempfile.TemporaryDirectory(prefix='qa3-alarm-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        self.enterContext(patch.dict(os.environ, {'CADRE_HOME': str(self.home), 'CADRE_KOSU_TAVAN_USD': '', 'CADRE_GOREV_ID': ''}))
        self.bk = bk
        self.store = RunStore(self.home)
        self.ledger = bk.BudgetLedger(self.home / 'zincir/request-budget.db')
        self.simdi = time.time()
        self.wt = self.home / 'urun'
        self.wt.mkdir()
        git(self.wt, 'init', '-q')
        (self.wt / 'app.py').write_text('value = 1\n')
        git(self.wt, 'add', 'app.py')
        git(self.wt, 'commit', '-qm', 'fixture')
        self.rid = self.kosu('fixture')

    def kosu(self, ad: str, worktree: bool = True) -> str:
        rid = self.store.accept(ad, 'task')['run_id']
        self.store.update(rid, budget_cap_usd=3, budget_manual=True, created=self.simdi - 7200, **({'worktree': str(self.wt)} if worktree else {}))
        with self.ledger.transaction() as con:
            for offset in (500, 300, 100):
                con.execute('insert into reservation values(?,?,?,?,?,?,?,?)',
                            (f'{rid}-{offset}', 'opencode-go', 'p', self.simdi - offset, 450_000, 450_000, f'makbuz-{rid}-{offset}', '{"run_id": "%s"}' % rid))
        return rid

    def alarm(self, rid=None, simdi=None):
        with self.ledger.transaction() as con:
            return self.bk.harcama_alarmi(self.home, con, rid or self.rid, self.simdi if simdi is None else simdi)

    def surekli(self, rid, kayit, ilk=1300):
        self.store.update(rid, harcama_uyarisi={**kayit, 'ts': self.simdi - 500, 'ilk_ts': self.simdi - ilk})

    def test_three_progress_states_follow_the_contract(self):
        from kosu import approve_spending
        from test_butce_donemi import snapshot
        import proje
        denial, kayit = self.alarm()
        self.assertIsNone(denial)
        self.assertEqual((kayit['kind'], kayit['ilerleme'], kayit['ilk_ts'], kayit['cagri']), ('speed-warning', 'unknown', self.simdi, 0))
        self.assertIsInstance(kayit['urun'], str)
        self.surekli(self.rid, kayit, ilk=1100)
        denial, ara = self.alarm()
        self.assertIsNone(denial, 'sonlu sure dolmadan alarm yok')
        self.assertEqual((ara['ilerleme'], ara['urun']), ('unknown', kayit['urun']))
        self.surekli(self.rid, kayit)
        denial, alarm = self.alarm()
        self.assertIn('onay bekleniyor', denial)
        self.assertEqual((alarm['kind'], alarm['uyari']['ilerleme'], alarm['uyari']['ilk_ts']), ('no-progress-spend', 'none', self.simdi - 1300))
        self.assertIn('urun degismedi ve kayitli adim yok (3 cagri)', alarm['neden'])
        (self.wt / 'app.py').write_text('value = 2\n')
        denial, degisti = self.alarm()
        self.assertIsNone(denial, 'urun degistiyse ilerleme yok denemez')
        self.assertEqual(degisti['ilerleme'], 'unknown')
        (self.wt / 'app.py').write_text('value = 1\n')
        self.assertIn('onay bekleniyor', self.alarm()[0] or '')
        self.bk.ilerleme_yaz(self.home, self.rid, 'serit-uretti', 'g1:' + kayit['urun'])
        denial, uretken = self.alarm(simdi=time.time())
        self.assertIsNone(denial)
        self.assertEqual((uretken['ilerleme'], uretken['kanit']['neden']), ('productive', 'serit-uretti'))
        self.assertIsNone(self.bk.tavan_ilerlemesi(self.home, self.store.read(self.rid)), 'serit adimi tavan uzatmaz')
        self.bk.ilerleme_yaz(self.home, self.rid, 'serit-uretti', 'g1:' + kayit['urun'])
        self.assertEqual(len(self.store.read(self.rid)['budget_progress_history']), 1, 'ayni adim saati yenilemez')
        self.bk.ilerleme_yaz(self.home, self.rid, 'cpu-hareketi', 'x')
        self.assertEqual(len(self.store.read(self.rid)['budget_progress_history']), 1, 'CPU/log sinyali adim degildir')
        self.store.update(self.rid, budget_progress={'neden': 'qa-pass', 'kimlik': 'rev-0', 'ts': self.simdi - 5000}, budget_progress_history=[])
        self.surekli(self.rid, kayit)
        self.assertIn('onay bekleniyor', self.alarm()[0] or '', 'bolum oncesi eski kayit saymaz')
        with self.assertRaisesRegex(self.bk.BudgetDenied, 'onay bekleniyor'):
            self.ledger.reserve('opencode-go', 10_000, snapshot(50), {'run_id': self.rid})
        record = self.store.read(self.rid)
        self.assertEqual((record['harcama_alarmi']['kind'], record['harcama_alarmi']['uyari']['ilerleme']), ('no-progress-spend', 'none'))
        self.assertEqual(proje.bekleme_nedeni(record), 'harcama-alarmi')
        approve_spending(self.store, self.rid)
        self.ledger.reserve('opencode-go', 10_000, snapshot(50), {'run_id': self.rid})
        record = self.store.read(self.rid)
        self.assertLess(record['harcama_alarmi']['ts'], record['harcama_onayi_ts'])
        self.assertEqual(proje.bekleme_nedeni(record), '')

    def test_unknown_product_disabled_limit_and_new_episode_never_alarm(self):
        from unittest.mock import patch
        rid2 = self.kosu('worktreesiz', worktree=False)
        denial, kayit = self.alarm(rid2)
        self.assertIsNone(denial)
        self.assertEqual((kayit['ilerleme'], kayit['urun']), ('unknown', None))
        self.surekli(rid2, kayit)
        denial, kayit = self.alarm(rid2)
        self.assertIsNone(denial, 'urun ozeti bilinmiyorsa israf etiketi verilmez')
        self.assertEqual(kayit['ilerleme'], 'unknown')
        denial, kayit = self.alarm()
        self.surekli(self.rid, kayit)
        with patch.dict(os.environ, {'CADRE_HARCAMA_ALARM_ILERLEMESIZ_SN': '0'}):
            self.assertIsNone(self.alarm()[0])
        with patch.dict(os.environ, {'CADRE_HARCAMA_ALARM_ILERLEMESIZ_SN': 'abc'}):
            self.assertIn('onay bekleniyor', self.alarm()[0] or '', 'okunamayan sinir varsayilana duser')
        self.store.update(self.rid, harcama_uyarisi={**kayit, 'ts': self.simdi - 1300, 'ilk_ts': self.simdi - 3000})
        denial, yeni = self.alarm()
        self.assertIsNone(denial, 'bir pencereden eski uyari yeni bolum baslatir')
        self.assertEqual(yeni['ilk_ts'], self.simdi)
        self.store.update(self.rid, harcama_uyarisi={**kayit, 'ts': self.simdi - 500, 'ilk_ts': self.simdi - 1300, 'urun': 'eski-ozet'})
        denial, farkli = self.alarm()
        self.assertIsNone(denial, 'bolum basindaki urun ozetinden farkli urun = ilerleme yok degil')
        self.assertEqual(farkli['ilerleme'], 'unknown')


class YaziliOnayTests(unittest.TestCase):
    """FIX-51 (Codex P1): the event sequence a long-held Enter can produce cannot pass two typed approvals; every step needs `evet`/`yes` typed followed by Enter."""

    def ui_python(self) -> Path:
        py = Path(os.environ.get('CADRE_TEST_ARAYUZ_PYTHON') or os.environ.get('CADRE_UI_PYTHON')
                  or Path.home() / '.local/share/cadre-ui-venv/bin/python')
        if not py.exists():
            self.skipTest('Textual ortami kurulu degil')
        return py

    def test_held_enter_stream_cannot_pass_either_stage_but_typed_words_can(self):
        script = r'''
import asyncio, json, os, sys, tempfile, time
from textual.events import Key
home = tempfile.mkdtemp()
os.environ.update(HOME=home, CADRE_HOME=home, CADRE_AG_YOK='1', PYTHONDONTWRITEBYTECODE='1')
sys.path.insert(0, sys.argv[1])
import arayuz, izin
from kosu import RunStore
async def held(app, seconds):
    started, count = time.monotonic(), 0
    while time.monotonic() - started < seconds:
        app.post_message(Key('enter', '\r')); count += 1
        await asyncio.sleep(0.03)
    return count
async def run():
    store = RunStore(home); rid = store.accept('fixture', 'synthetic approval')['run_id']
    action = izin.propose(store, rid, 'push', [['git', 'push', 'SYNTHETIC-ONLY-NEVER-EXECUTED']], {'target': 'synthetic'}, {'digest': 'synthetic'})
    status = lambda: izin.read_action(store, rid, action['token'])['status']
    out, callbacks = [], []
    app = arayuz.Cadre(home=home)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        app.operator_approval(rid, action['token'], after=lambda: callbacks.append(1))
        await pilot.pause(0.1)
        out.append(type(app.screen).__name__ + ':' + str(app.screen.yazili))
        out.append(await held(app, 1.4)); await pilot.pause(0.1)
        out.append(status())
        await pilot.press(*'evet', 'enter'); await pilot.pause(0.2)
        out.append(status())
        out.append(await held(app, 1.0)); await pilot.pause(0.1)
        out.append(status())
        await pilot.pause(arayuz.Onay.OLU_SURE_SN)
        await pilot.press(*'hayir', 'enter'); await pilot.pause(0.1)
        out.append(status())
        for _ in range(5):
            await pilot.press('backspace')
        await pilot.press(*'yes', 'enter'); await pilot.pause(0.3)
        out.append(status()); out.append(len(callbacks))
        app.push_screen(arayuz.Onay('sade', 'a', lambda: callbacks.append('sade')))
        await pilot.pause(arayuz.Onay.OLU_SURE_SN + 0.1)
        await pilot.press('enter'); await pilot.pause(0.1)
        out.append('sade' in callbacks)
    print(json.dumps(out))
asyncio.run(run())
'''
        result = subprocess.run([str(self.ui_python()), '-c', script, str(LIB)], capture_output=True, text=True, timeout=180,
                                env={k: v for k, v in os.environ.items() if k not in ('CADRE_ONAY_OLU_SURE_SN', 'CADRE_RUN_ID')})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        out = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(out[0], 'Onay:True')
        self.assertGreater(out[1], 30, 'uzun basili tus benzetimi en az 30 olay uretir')
        self.assertEqual(out[2], 'pending', 'ilk asama: Enter dizisi yazili onay degildir')
        self.assertEqual(out[3], 'first-approved', 'evet + Enter ilk yazili onaydir')
        self.assertEqual(out[5], 'first-approved', 'ikinci asama: Enter dizisi gecemez')
        self.assertEqual(out[6], 'first-approved', 'yanlis sozcuk gecemez')
        self.assertEqual((out[7], out[8]), ('approved', 1), 'yes + Enter ikinci yazili onaydir; eylem bir kez')
        self.assertTrue(out[9], 'sade Onay (iptal/plan) Enter ile calisir')


class MakbuzKabulTests(unittest.TestCase):
    """FIX-17 (N-BU06): an excess receipt closes the family; only a recorded operator acceptance opens it, the cap does not grow."""

    def setUp(self) -> None:
        import butce_kapisi as bk
        from unittest.mock import patch
        temp = tempfile.TemporaryDirectory(prefix='qa3-makbuz-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.enterContext(patch.dict(os.environ, {'CADRE_HOME': str(self.home), 'CADRE_KOSU_TAVAN_USD': '', 'CADRE_GOREV_ID': '',
                                                   'CADRE_HARCAMA_ALARMI': '0'}))
        os.environ.pop('CADRE_RUN_ID', None)
        self.bk = bk
        self.store = RunStore(self.home)
        self.ledger = bk.BudgetLedger(self.home / 'zincir/request-budget.db')
        self.rid = self.store.accept('fixture', 'task')['run_id']
        self.store.update(self.rid, budget_cap_usd=2, budget_manual=True)

    def snapshot(self):
        from test_butce_donemi import snapshot
        return {**snapshot(50), 'or': {'db': 'ok', 'pay': 50, 'kullanilabilir': 50}}

    def test_accepted_overage_reopens_the_family_without_raising_any_cap(self):
        from unittest.mock import patch
        ident = self.ledger.reserve('openrouter', 500_000, self.snapshot(), {'run_id': self.rid})
        self.ledger.settle(ident, 900_000, 'makbuz-1')
        with self.assertRaisesRegex(self.bk.BudgetDenied, 'project receipt bound exceeded'):
            self.ledger.reserve('opencode-go', 10_000, self.snapshot(), {'run_id': self.rid})
        with self.ledger.transaction() as con:
            asim = self.bk.makbuz_asimlari(con, {self.rid})
        self.assertEqual([(a['id'], a['actual'], a['bound']) for a in asim], [(ident, 900_000, 500_000)])
        with self.assertRaisesRegex(self.bk.BudgetDenied, 'unknown reservation'):
            self.bk.makbuz_kabul(self.home, 'yok')
        with patch.dict(os.environ, {'CADRE_RUN_ID': self.rid}), self.assertRaisesRegex(self.bk.BudgetDenied, 'outside a run'):
            self.bk.makbuz_kabul(self.home, ident)
        with self.assertRaisesRegex(self.bk.BudgetDenied, 'project receipt bound exceeded'):
            self.ledger.reserve('opencode-go', 10_000, self.snapshot(), {'run_id': self.rid})
        kabul = self.bk.makbuz_kabul(self.home, ident)
        self.assertEqual((kabul['already'], kabul['actual'], kabul['bound'], kabul['run_id']), (False, 900_000, 500_000, self.rid))
        self.ledger.reserve('opencode-go', 10_000, self.snapshot(), {'run_id': self.rid})
        record = self.store.read(self.rid)
        last = record['operator_interventions'][-1]
        self.assertEqual((last['kind'], last['detail']['reservation'], last['detail']['actual_usd']), ('receipt-overage-accepted', ident, 0.9))
        self.assertTrue(self.bk.makbuz_kabul(self.home, ident)['already'])
        self.assertEqual(len(self.store.read(self.rid)['operator_interventions']), 1)
        with self.assertRaisesRegex(self.bk.BudgetDenied, 'observed usage exceeded its verified bound'):
            self.ledger.reserve('openrouter', 10_000, self.snapshot(), {'run_id': self.rid})
        with self.ledger.transaction() as con:
            con.execute("delete from pricing_fault where provider='openrouter'")
        with self.assertRaisesRegex(self.bk.BudgetDenied, 'run budget exhausted'):
            self.ledger.reserve('openrouter', 1_200_000, self.snapshot(), {'run_id': self.rid}, run_cap=2_000_000)
        normal = self.ledger.reserve('openrouter', 100_000, self.snapshot(), {'run_id': self.rid}, run_cap=2_000_000)
        self.ledger.settle(normal, 50_000, 'makbuz-2')
        with self.assertRaisesRegex(self.bk.BudgetDenied, 'within its verified bound'):
            self.bk.makbuz_kabul(self.home, normal)
        log = self.home / 'zincir/kurtarma.log'
        self.assertIn('makbuz-kabul', log.read_text())

    def test_cli_lists_and_accepts_only_with_an_explicit_answer(self):
        ident = self.ledger.reserve('openrouter', 300_000, self.snapshot(), {'run_id': self.rid})
        self.ledger.settle(ident, 400_000, 'makbuz-3')
        env = {**os.environ, 'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(LIB.parent)}
        env.pop('CADRE_RUN_ID', None)
        def cli(*args):
            return subprocess.run([sys.executable, str(LIB / 'butce_kapisi.py'), *args], env=env, capture_output=True, text=True, timeout=60)
        listed = cli('--makbuz-listele', self.rid)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn(ident[:8], listed.stdout)
        self.assertIn('--makbuz-kabul ' + ident, listed.stdout)
        self.assertEqual(cli('--makbuz-kabul', ident).returncode, 2)
        self.assertEqual(cli('--makbuz-kabul').returncode, 2)
        with self.assertRaisesRegex(self.bk.BudgetDenied, 'project receipt bound exceeded'):
            self.ledger.reserve('opencode-go', 10_000, self.snapshot(), {'run_id': self.rid})
        accepted = cli('--makbuz-kabul', ident, '--evet')
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertIn('kabul kaydedildi', accepted.stdout)
        self.assertIn('zaten kabul', cli('--makbuz-kabul', ident, '--evet').stdout)
        self.assertIn('fazla makbuz yok', cli('--makbuz-listele').stdout)
        self.ledger.reserve('opencode-go', 10_000, self.snapshot(), {'run_id': self.rid})
        inside = subprocess.run([sys.executable, str(LIB / 'butce_kapisi.py'), '--makbuz-kabul', ident, '--evet'],
                                env={**env, 'CADRE_RUN_ID': self.rid}, capture_output=True, text=True, timeout=60)
        self.assertEqual(inside.returncode, 2)
        self.assertIn('outside a run', inside.stderr)


class OnayOluSureTests(unittest.TestCase):
    def ui_python(self) -> Path:
        py = Path(os.environ.get('CADRE_TEST_ARAYUZ_PYTHON') or os.environ.get('CADRE_UI_PYTHON')
                  or Path.home() / '.local/share/cadre-ui-venv/bin/python')
        if not py.exists():
            self.skipTest('Textual ortami kurulu degil')
        return py

    def test_enter_inside_the_dead_time_is_ignored_and_a_held_key_cannot_pass_two_stages(self):
        """FIX-51: a short dead time after an approval window opens; two consecutive windows cannot be passed with one gesture. q/Esc are not affected."""
        script = r'''
import asyncio, json, os, sys, tempfile
os.environ['CADRE_HOME'] = tempfile.mkdtemp()
sys.path.insert(0, sys.argv[1])
import arayuz
events = []
async def kos():
    app = arayuz.Cadre(ornek=True)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.push_screen(arayuz.Onay('ilk', 'a', lambda: events.append('ilk')))
        await pilot.pause()
        await pilot.press('enter')
        await pilot.pause()
        events.append(type(app.screen).__name__)
        await pilot.pause(arayuz.Onay.OLU_SURE_SN + 0.1)
        await pilot.press('enter')
        await pilot.pause()
        events.append(type(app.screen).__name__)
        app.push_screen(arayuz.Onay('bir', 'a', lambda: app.push_screen(arayuz.Onay('iki', 'b', lambda: events.append('iki')))))
        await pilot.pause(arayuz.Onay.OLU_SURE_SN + 0.1)
        await pilot.press('enter', 'enter', 'enter')
        await pilot.pause()
        events.append(type(app.screen).__name__ + ':' + app.screen.soru)
        await pilot.press('q')
        await pilot.pause()
        events.append(type(app.screen).__name__)
        app.push_screen(arayuz.Onay('uc', 'c', lambda: events.append('uc')))
        await pilot.pause()
        await pilot.press('escape')
        await pilot.pause()
        events.append(type(app.screen).__name__)
    print(json.dumps([events, arayuz.Onay.OLU_SURE_SN]))
asyncio.run(kos())
'''
        result = subprocess.run([str(self.ui_python()), '-c', script, str(LIB)], capture_output=True, text=True, timeout=120,
                                env={k: v for k, v in os.environ.items() if k != 'CADRE_ONAY_OLU_SURE_SN'})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        events, dead = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(dead, 0.3)
        self.assertEqual(events[0], 'Onay')
        self.assertEqual(events[1], 'ilk')
        self.assertNotEqual(events[2], 'Onay')
        self.assertEqual(events[3], 'Onay:iki')
        self.assertNotIn('iki', events)
        self.assertNotEqual(events[4], 'Onay')
        self.assertNotIn('uc', events)
        self.assertNotEqual(events[5], 'Onay')


class AnalistNotuTests(unittest.TestCase):
    """CR-30 (N-REP03): the single analyst / reconciliation record and the note in the report itself; it does not trip the number check."""

    def setUp(self) -> None:
        import rapor
        temp = tempfile.TemporaryDirectory(prefix='qa3-analist-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.rapor = rapor
        self.store = RunStore(self.home)
        self.rid = self.store.accept('fixture', 'rapor isi')['run_id']
        from kosu import atomic_json
        record = self.store.read(self.rid)
        record['rapor'] = {'yol': 'rapor.md', 'kanit': 'rapor.kanit.jsonl'}
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        self.wt = self.home / 'wt'
        self.wt.mkdir()
        (self.wt / 'rapor.md').write_text('# Ozet\nbulgu [K1].\n')

    def test_record_and_report_note_are_written_once_and_reapplied_before_qa(self):
        kayit = self.rapor.analist_kaydi(self.store.path(self.rid), self.wt, 'tek', ['m/a', '', ''], 'ikinci guclu analist yok')
        self.assertEqual((kayit['kip'], kayit['birinci'], kayit['ikinci'], kayit['uzlastirici']), ('tek', 'm/a', None, None))
        self.assertEqual(self.store.read(self.rid)['rapor']['analist']['neden'], 'ikinci guclu analist yok')
        metin = (self.wt / 'rapor.md').read_text()
        self.assertEqual(metin.count('Analist notu (G-R7)'), 1)
        self.assertEqual(self.rapor.kanitsiz_sayilar(self.rapor.analist_notu('tek', 'ikinci guclu analist yok')), [])
        self.assertEqual(self.rapor.analist_kontrol(self.store.read(self.rid)['rapor'], metin), [])
        self.rapor.analist_kaydi(self.store.path(self.rid), self.wt, 'mutabakat', ['m/a', 'm/b', 'm/c'])
        metin = (self.wt / 'rapor.md').read_text()
        self.assertEqual(metin.count('Analist notu (G-R7)'), 1)
        self.assertIn('iki bagimsiz analist', metin)
        beyan = self.store.read(self.rid)['rapor']
        self.assertEqual(beyan['analist']['uzlastirici'], 'm/c')
        (self.wt / 'rapor.md').write_text('# Ozet\nbulgu [K1].\n')
        self.assertEqual(self.rapor.analist_kontrol(beyan, (self.wt / 'rapor.md').read_text())[0].split(':')[0], 'analist-notu-eksik')
        self.assertTrue(self.rapor.analist_notu_uygula(self.wt, beyan))
        self.assertFalse(self.rapor.analist_notu_uygula(self.wt, beyan))
        self.assertEqual(self.rapor.analist_kontrol(beyan, (self.wt / 'rapor.md').read_text()), [])
        tek = {**beyan, 'analist': {**beyan['analist'], 'kip': 'tek'}}
        self.assertEqual(self.rapor.analist_kontrol(tek, (self.wt / 'rapor.md').read_text())[0].split(':')[0], 'analist-notu-celisiyor')
        with self.assertRaises(ValueError):
            self.rapor.analist_kaydi(self.store.path(self.rid), self.wt, 'uc', ['m/a'])
        self.assertEqual(self.rapor.analist_kontrol({'yol': 'rapor.md'}, 'x'), [])

    def test_project_map_header_shows_total_and_truncation(self):
        import proje
        repo = self.home / 'repo'
        repo.mkdir()
        git(repo, 'init', '-q')
        for i in range(proje.HARITA_SATIR + 5):
            (repo / f'f{i:03d}.txt').write_text('x')
        git(repo, 'add', '.')
        git(repo, 'commit', '-qm', 'files')
        harita = proje._harita(repo).splitlines()
        self.assertEqual(harita[0], f'({proje.HARITA_SATIR + 5} dosya; ilk {proje.HARITA_SATIR} gösterildi, KIRPILDI)')
        self.assertEqual(len(harita), proje.HARITA_SATIR + 1)
        for i in range(proje.HARITA_SATIR, proje.HARITA_SATIR + 5):
            (repo / f'f{i:03d}.txt').unlink()
        git(repo, 'add', '-A')
        git(repo, 'commit', '-qm', 'fewer')
        self.assertEqual(proje._harita(repo).splitlines()[0], f'({proje.HARITA_SATIR} dosya)')


class TemizKurulumTests(unittest.TestCase):
    """CR-28 (N-PUB01): install into a clean home, uninstall and reinstall with the same manifest; the check is read-only, the repo config has no personal paths."""

    def test_install_check_uninstall_and_reinstall_in_an_empty_home(self):
        import shutil
        from test_kaynak_ve_sync import isolated_sync
        temp = tempfile.TemporaryDirectory(prefix='qa3-kurulum-')
        self.addCleanup(temp.cleanup)
        home = Path(temp.name).resolve()
        sync = isolated_sync(home)
        if sync is None:
            self.skipTest('sync.sh yok')
        qe, cfg, bin_dir = home / 'qe', home / 'cfg', home / 'bin'
        for d in (qe, cfg / 'agents', cfg / 'skills', cfg / 'command'):
            d.mkdir(parents=True)
        (cfg / 'opencode.jsonc').write_text('{}')
        env = {**os.environ, 'CADRE_HOME': str(qe), 'OPENCODE_CFG': str(cfg), 'CADRE_BIN_DIR': str(bin_dir), 'CADRE_SYNC_SKIP_PGREP': '1',
               'HOME': str(home / 'user-home')}
        (home / 'user-home').mkdir()
        env.pop('CADRE_RUN_ID', None)

        def push() -> dict:
            result = subprocess.run(['bash', str(sync), 'push'], env=env, capture_output=True, text=True, timeout=180)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads((qe / 'zincir/runtime-manifest.json').read_text())['files']

        first = push()
        self.assertTrue((qe / 'lib/qa-sim/sitecustomize.py').is_file() and (bin_dir / 'cadre').is_file())
        check = subprocess.run([sys.executable, str(qe / 'lib/kurulum_kontrol.py'), '--json'], capture_output=True, text=True, timeout=120,
                               env={**env, 'CADRE_ENGINE_ROOT': str(qe)})
        report = json.loads(check.stdout)
        durum = {m['ad']: m['durum'] for m in report['maddeler']}
        self.assertEqual((durum['CADRE_HOME'], durum['CADRE_ENGINE_ROOT'], durum['OpenCode config'], durum['kisisel sabit yol']), ('VAR',) * 4)
        self.assertIn('kurulum/indirme yapilmadi', report['not'])
        repo_config = (sync.parent / 'config/opencode.jsonc').read_text()
        self.assertIn('file://{env:HOME}/.local/share/cadre-engineering/lib/cadre-plugin.mjs', repo_config)
        self.assertNotIn('/Users/', repo_config)
        import kurulum_kontrol
        repo_check = kurulum_kontrol.kontrol(qe, cfg, sync.parent / 'engine', environment=env)
        self.assertEqual(next(m for m in repo_check['maddeler'] if m['ad'] == 'kisisel sabit yol')['durum'], 'VAR')
        for managed in ('lib', 'bin', 'hooks', 'tests', 'USAGE.md', 'zincir'):
            path = qe / managed
            shutil.rmtree(path) if path.is_dir() else path.unlink()
        (bin_dir / 'cadre').unlink()
        for sub in ('agents', 'skills', 'command'):
            shutil.rmtree(cfg / sub)
            (cfg / sub).mkdir()
        self.assertFalse((qe / 'lib').exists())
        second = push()
        self.assertEqual({p.replace(str(qe), '').replace(str(bin_dir), '').replace(str(cfg), ''): v['sha256'] for p, v in first.items()},
                         {p.replace(str(qe), '').replace(str(bin_dir), '').replace(str(cfg), ''): v['sha256'] for p, v in second.items()})
        fark = subprocess.run(['bash', str(sync), 'fark'], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual((fark.returncode, fark.stdout.strip()), (0, 'fark yok — repo == canlı'), fark.stdout + fark.stderr)
        self.assertEqual((cfg / 'opencode.jsonc').read_text(), '{}')


class IskeletTests(unittest.TestCase):
    """CR-33 (N-ENV01): the python skeleton is part of the first commit permission; install/test/run in one record; a restart does not duplicate."""

    def setUp(self) -> None:
        from unittest.mock import patch
        temp = tempfile.TemporaryDirectory(prefix='qa3-iskelet-')
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name).resolve()
        self.home = self.tmp / 'ev'
        self.home.mkdir()
        self.enterContext(patch.dict(os.environ, {'CADRE_HOME': str(self.home), 'GIT_AUTHOR_NAME': 'fixture', 'GIT_AUTHOR_EMAIL': 'f@local',
                                                   'GIT_COMMITTER_NAME': 'fixture', 'GIT_COMMITTER_EMAIL': 'f@local'}))
        os.environ.pop('CADRE_RUN_ID', None)
        self.store = RunStore(self.home)

    def onayla(self, token: str) -> None:
        import izin
        izin.approve(self.store, 'system', token)
        izin.approve(self.store, 'system', token, izin.read_action(self.store, 'system', token)['confirmation'])
        izin.initialization_execute(self.store, token)

    def test_skeleton_is_deterministic_and_verifiable(self):
        import iskelet
        self.assertEqual(iskelet.dosyalar('Deneme Projesi', 'python'), iskelet.dosyalar('Deneme Projesi', 'python'))
        self.assertEqual((iskelet.paket_adi('Deneme Projesi'), iskelet.paket_adi('3d-çizim'), iskelet.paket_adi('!!')),
                         ('deneme_projesi', 'uygulama_3d_cizim', 'uygulama'))
        with self.assertRaises(ValueError):
            iskelet.dosyalar('x', 'rust')
        target = self.tmp / 'hedef'
        target.mkdir()
        yazilan = iskelet.yaz(target, 'Deneme', 'python')
        self.assertIn('cadre-iskelet.json', yazilan)
        self.assertEqual(yazilan, iskelet.yaz(target, 'Deneme', 'python'), 'ayni icerik yeniden yazilabilir')
        m = iskelet.calisma_agaci_uyuyor(target)
        self.assertEqual((m['tur'], m['ad'], m['paket']), ('python', 'Deneme', 'deneme'))
        (target / 'ekstra.txt').write_text('x')
        self.assertIsNone(iskelet.calisma_agaci_uyuyor(target))
        (target / 'ekstra.txt').unlink()
        (target / 'Makefile').write_text('bozuk')
        self.assertIsNone(iskelet.calisma_agaci_uyuyor(target))
        with self.assertRaises(FileExistsError):
            iskelet.yaz(target, 'Deneme', 'python')
        with self.assertRaises(FileExistsError):
            iskelet.yaz(self.tmp / 'hedef', 'Baska', 'python')

    def test_make_targets_keep_the_qa_evidence_pythonpath(self):
        """CR-33 / FIX-52 (15.10 campaign R06): `make test` removed the QA evidence shim (qa-sim sitecustomize) from the environment with a fixed `PYTHONPATH=src`;
        the run's QA errored with `qa-discovery-unverified`. The targets append the existing PYTHONPATH at the end."""
        import iskelet
        target = self.tmp / 'yol-hedef'
        target.mkdir()
        iskelet.yaz(target, 'Yol Deneme', 'python')
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        for hedef, parca in (('test', '-m unittest discover -q -s tests'), ('run', '-m yol_deneme')):
            with self.subTest(hedef=hedef):
                sade = subprocess.run(['make', '-n', hedef], cwd=target, env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(sade.returncode, 0, sade.stderr)
                self.assertIn('PYTHONPATH=src ', sade.stdout)
                self.assertIn(parca, sade.stdout)
                shim = subprocess.run(['make', '-n', hedef], cwd=target, env={**env, 'PYTHONPATH': '/kanit/qa-sim'}, capture_output=True,
                                      text=True, timeout=60)
                self.assertEqual(shim.returncode, 0, shim.stderr)
                self.assertIn('PYTHONPATH=src:/kanit/qa-sim ', shim.stdout)

    def test_typed_init_commits_the_skeleton_under_the_two_step_permission_and_records_the_flow(self):
        import iskelet
        import izin
        import kosu
        import proje_kayit
        import qa_turet
        target = self.tmp / 'yeni-uygulama'
        with self.assertRaises(proje_kayit.KayitHatasi):
            proje_kayit.kaydet(target, olustur=True, home=self.home, tur='rust')
        self.assertFalse(target.exists())
        with self.assertRaises(proje_kayit.KayitIzni) as asked:
            proje_kayit.kaydet(target, olustur=True, home=self.home, tur='python')
        token = asked.exception.token
        action = izin.read_action(self.store, 'system', token)
        hedef = action['identity']['target']['iskelet']
        self.assertEqual((hedef['tur'], hedef['ad'], hedef['sonra']), ('python', 'yeni-uygulama', ['make install', 'make test', 'make run']))
        self.assertIn('Makefile', hedef['dosyalar'])
        self.assertIn('iskelet.py', action['display'])
        self.assertIn('Initial baseline (cadre python iskeleti)', action['display'])
        self.assertEqual([p.name for p in target.iterdir()], [], 'git init de izinli komutlar arasindadir')
        self.onayla(token)
        self.assertTrue((target / 'Makefile').is_file() and (target / 'tests/test_smoke.py').is_file())
        head = git(target, 'rev-parse', 'HEAD')
        self.assertEqual(git(target, 'rev-list', '--count', 'HEAD'), '1')
        self.assertEqual(iskelet.commit_agaci_uyuyor(target, head)['paket'], 'yeni_uygulama')
        self.assertTrue(kosu.new_target_initialized(target))
        self.assertEqual(izin.read_action(self.store, 'system', token)['status'], 'completed')
        with self.assertRaises(proje_kayit.KayitHatasi):
            proje_kayit.kaydet(target, home=self.home, tur='python')
        ad = proje_kayit.kaydet(target, home=self.home)
        self.assertEqual(ad, 'yeni-uygulama')
        kayit = json.loads((self.home / 'projects' / ad / 'iskelet.json').read_text())
        self.assertEqual((kayit['durum'], [a['ad'] for a in kayit['adimlar']], [a['rc'] for a in kayit['adimlar']]), ('PASS', ['install', 'test', 'run'], [0, 0, 0]))
        self.assertTrue((target / '.venv/bin/python').exists(), 'make install yerel venv kurar, indirme yok')
        self.assertIn('yeni_uygulama 0.1.0: merhaba dunya', kayit['adimlar'][2]['cikti'])
        self.assertTrue(kosu.new_target_initialized(target), 'yok sayilan .venv hedefi kirletmez')
        self.assertEqual(qa_turet.turet(target, LIB.parent)['command'], 'make test')
        again = proje_kayit.kaydet(target, home=self.home)
        self.assertEqual(again, ad)
        self.assertEqual(git(target, 'rev-list', '--count', 'HEAD'), '1')
        self.assertEqual(sorted(p.name for p in (self.home / 'projects').iterdir()), [ad])
        self.assertGreaterEqual(json.loads((self.home / 'projects' / ad / 'iskelet.json').read_text())['ts'], kayit['ts'])
        (target / 'notlar.txt').write_text('elle')
        self.assertFalse(kosu.new_target_initialized(target))

    def test_interrupted_skeleton_write_resumes_with_the_same_identity_and_one_commit(self):
        import iskelet
        import izin
        import kosu
        import proje_kayit
        target = self.tmp / 'yarim'
        target.mkdir()
        git(target, 'init', '-q', '-b', 'main')
        iskelet.yaz(target, 'yarim', 'python')
        self.assertTrue(izin.initialized_empty(target))
        with self.assertRaises(proje_kayit.KayitIzni) as asked:
            proje_kayit.kaydet(target, olustur=True, home=self.home)
        action = izin.read_action(self.store, 'system', asked.exception.token)
        self.assertEqual(action['identity']['target']['iskelet']['tur'], 'python')
        self.onayla(asked.exception.token)
        self.assertEqual(git(target, 'rev-list', '--count', 'HEAD'), '1')
        self.assertTrue(kosu.new_target_initialized(target))
        self.assertEqual(proje_kayit.kaydet(target, home=self.home), 'yarim')
        (target / 'Makefile').write_text('bozuk')
        self.assertFalse(kosu.new_target_initialized(target))
        self.assertFalse(izin.initialized_empty(target))

    def test_cli_registers_a_typed_project_with_two_answers(self):
        target = self.tmp / 'cli-projesi'
        env = {**os.environ, 'CADRE_HOME': str(self.home)}
        bad = subprocess.run([sys.executable, str(LIB / 'proje_kayit.py'), '--olustur', str(target), '--tur', 'go'], env=env,
                             capture_output=True, text=True, input='evet\nevet\n', timeout=120)
        self.assertEqual(bad.returncode, 1)
        self.assertIn('python', bad.stderr)
        result = subprocess.run([sys.executable, str(LIB / 'proje_kayit.py'), '--olustur', str(target), '--tur', 'python'], env=env,
                                capture_output=True, text=True, input='evet\nevet\n', timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(result.stdout.strip().endswith('cli-projesi'), result.stdout)
        self.assertIn('cadre-iskelet.json', result.stdout)
        self.assertIn('İskelet akışı PASS', result.stderr)
        self.assertEqual(json.loads((self.home / 'projects/cli-projesi/iskelet.json').read_text())['durum'], 'PASS')
        self.assertEqual(subprocess.run([sys.executable, str(LIB / 'proje_kayit.py'), '--olustur', str(target), '--tur'], env=env,
                                        capture_output=True, text=True, timeout=60).returncode, 2)

if __name__ == '__main__':
    unittest.main()
