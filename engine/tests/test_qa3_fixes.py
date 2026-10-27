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

from run_store import RunStore  # noqa: E402

SANDBOX = '/usr/bin/sandbox-exec'
GIT_ID = ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid']


def git(repo: Path, *args: str) -> str:
    return subprocess.run(['git', '-C', str(repo), *GIT_ID, *args], check=True, capture_output=True, text=True).stdout.strip()


class RunSandboxFixture(unittest.TestCase):
    """The real `project_tool_policy` profile: FIX-25/26 are tested with the run sandbox itself."""

    def setUp(self) -> None:
        if sys.platform != 'darwin' or not Path(SANDBOX).is_file():
            self.skipTest('macOS sandbox-exec absent')
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
        self.wt = self.home / 'product'
        git(repo, 'worktree', 'add', '-qb', 'cadre/qa3', str(self.wt))
        parent = self.store.accept('a', 'project')['run_id']
        self.store.update(parent, project_ref={'id': 'fixture'})
        self.rid = self.store.accept('a', 'child')['run_id']
        self.store.update(self.rid, project_ref_parent=parent, worktree=str(self.wt))
        self.env = {**os.environ, **self.store.environment(self.rid), 'CADRE_TASK_ID': 'g1', 'CADRE_ENGINE_ROOT': str(LIB.parent),
                    'HOME': str(self.home / 'tool-home')}
        Path(self.env['HOME']).mkdir()
        from session import project_tool_policy
        directory = self.store.path(self.rid) / 'tasks/g1/attempts' / uuid.uuid4().hex
        policy = project_tool_policy(self.store, self.store.read(self.rid), directory, str(self.wt), 'opencode', 'impl', self.env)
        assert policy is not None
        self.profile = policy['profile']

    def sandbox(self, *argv: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([SANDBOX, '-p', self.profile, *argv], capture_output=True, text=True, timeout=120,
                              cwd=self.wt, env=self.env if env is None else env)


class LiveLayoutReadTests(RunSandboxFixture):
    """N-ENV02 (audit C-13/R-24): when the installed code is in the data root (CADRE_HOME/lib) the tool sandbox reads only the code directories,
    not the whole data root; chain backups, key files, other worktrees and records cannot be read."""

    def live_profile(self):
        import session
        import shutil
        shutil.copytree(LIB, self.home / 'lib')
        for name in ('bin', 'hooks', 'chain/.signature-9.reserve', 'backups', 'reports/r1', 'worktrees/other/w'):
            (self.home / name).mkdir(parents=True, exist_ok=True)
        self.files = {'code': self.home / 'lib/ok.py', 'reserve': self.home / 'chain/.signature-9.reserve/abc',
                         'key': self.home / 'chain/openrouter-key', 'backup': self.home / 'backups/opencode.jsonc',
                         'report': self.home / 'reports/r1/ledger.json', 'other': self.home / 'worktrees/other/w/x.txt'}
        for path in self.files.values():
            path.write_text('hidden\n')
        from unittest.mock import patch
        directory = self.store.path(self.rid) / 'tasks/g1/attempts' / uuid.uuid4().hex
        with patch.object(session, 'code_tree_root', return_value=self.home):
            policy = session.project_tool_policy(self.store, self.store.read(self.rid), directory, str(self.wt), 'opencode', 'impl', self.env)
        assert policy is not None
        self.profile = policy['profile']

    def test_live_layout_reads_only_engine_code_not_the_whole_data_root(self):
        self.live_profile()
        self.assertNotIn(f'(subpath "{self.home}")', self.profile)
        self.assertEqual(self.sandbox('cat', str(self.files['code'])).returncode, 0)
        self.assertEqual(self.sandbox('cat', str(self.wt / 'app.py')).returncode, 0)
        for entry_name in ('reserve', 'key', 'backup', 'report', 'other'):
            outcome = self.sandbox('cat', str(self.files[entry_name]))
            self.assertNotEqual(outcome.returncode, 0, entry_name)
            self.assertNotIn('hidden', outcome.stdout, entry_name)


class HardlinkLimitTests(RunSandboxFixture):
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
        import session
        external = self.home / 'outside-product.txt'
        external.write_text('SYNTHETIC-OUTSIDE-UNCHANGED\n')
        self.assertFalse(session.has_product_hardlink(self.wt))
        for folder in ('.cache', 'node_modules/pkg', '.venv/bin', '__pycache__', '.ruff_cache'):
            with self.subTest(folder=folder):
                (self.wt / folder).mkdir(parents=True, exist_ok=True)
                alias = self.wt / folder / 'preexisting.txt'
                os.link(external, alias)
                self.assertTrue(session.has_product_hardlink(self.wt), 'An alias outside the tree is untrusted')
                with self.assertRaisesRegex(PermissionError, 'project-tool-hardlink-untrusted'):
                    session.project_tool_policy(self.store, self.store.read(self.rid), self.store.path(self.rid) / 'tasks/g1/attempts' / uuid.uuid4().hex,
                                               str(self.wt), 'opencode', 'impl', self.env)
                if folder in ('node_modules/pkg', '.venv/bin'):
                    probe = self.sandbox('/usr/bin/python3', '-c', f"open({str(alias.relative_to(self.wt))!r},'w').write('SYNTHETIC-MODIFIED-VIA-HARDLINK')")
                    self.assertNotEqual(probe.returncode, 0, 'The profile blocks writes to install directories')
                    self.assertEqual(external.read_text(), 'SYNTHETIC-OUTSIDE-UNCHANGED\n')
                alias.unlink()
        self.assertFalse(session.has_product_hardlink(self.wt))
        (self.wt / 'node_modules/pkg/linked.bin').write_text('dependency')
        os.link(self.wt / 'node_modules/pkg/linked.bin', self.wt / 'node_modules/pkg/alias.bin')
        self.assertFalse(session.has_product_hardlink(self.wt), 'An in-tree hardlink is allowed')
        (self.wt / 'src').mkdir()
        (self.wt / 'src/data.txt').write_text('product')
        os.link(self.wt / 'src/data.txt', self.wt / 'src/alias.txt')
        self.assertTrue(session.has_product_hardlink(self.wt), 'Every hardlink in the product directory is untrusted')
        (self.wt / 'src/alias.txt').unlink()
        direct = self.sandbox('/usr/bin/python3', '-c', f"open({str(external)!r},'w').write('MODIFIED-DIRECT')")
        self.assertNotEqual(direct.returncode, 0)
        self.assertNotEqual(self.sandbox('/usr/bin/python3', '-c', "open('.venv/new.txt','w').write('x')").returncode, 0, 'New files cannot be written to an install directory')
        self.assertFalse((self.wt / '.venv/new.txt').exists())
        self.assertEqual(self.sandbox('/usr/bin/python3', '-c', "open('.ruff_cache/tmp','w').write('x')").returncode, 0, 'The cache directory is writable by QA tools')
        self.assertEqual(self.sandbox('/usr/bin/python3', '-c', "open('src/data.txt','a').write('!')").returncode, 0, 'The product directory remains writable')


class InnerExecutorTests(RunSandboxFixture):
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
        check = self.sandbox('/usr/bin/env', '-u', 'CADRE_RUN_ID', '/usr/bin/python3', str(LIB / 'session.py'), 'nested-check')
        self.assertEqual(check.returncode, 1, check.stderr)
        self.assertEqual(subprocess.run(['/usr/bin/python3', str(LIB / 'session.py'), 'nested-check'], capture_output=True).returncode, 0)
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
        env.update(CADRE_PYTHON='/usr/bin/python3', OPENCODE_EXPERIMENTAL_NATIVE_LLM='false', CADRE_BUDGET_OBSERVATION='1')
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
        import qa_evidence
        shim = LIB / 'qa-sim'
        self.assertEqual([p.name for p in shim.iterdir() if p.name != '__pycache__'], ['sitecustomize.py'])
        with tempfile.TemporaryDirectory(prefix='qa3-sim-') as tmp:
            evidence = Path(tmp) / 'evidence.jsonl'
            env = qa_evidence.environment({'PATH': os.environ['PATH'], 'HOME': tmp}, evidence, 'tok')
            entries = env['PYTHONPATH'].split(os.pathsep)
            self.assertEqual(entries[0], str(shim))
            self.assertNotIn(str(LIB), entries)
            probe = ("import importlib.util, json, sys; print(json.dumps([importlib.util.find_spec('run') is None, "
                     "importlib.util.find_spec('budget_gate') is None, sys.modules['qa_evidence'].__file__]))")
            out = subprocess.run([sys.executable, '-c', probe], env=env, capture_output=True, text=True, check=True).stdout
            self.assertEqual(json.loads(out), [True, True, str(LIB / 'qa_evidence.py')])
            user = Path(tmp) / 'user'
            user.mkdir()
            (user / 'run_store.py').write_text("VALUE = 'user'\n")
            (user / 'test_user.py').write_text('import unittest, run_store\nclass T(unittest.TestCase):\n'
                                               " def test_user_module(self): self.assertEqual(run_store.VALUE, 'user')\n")
            result = subprocess.run([sys.executable, '-m', 'unittest', '-q', 'test_user'], cwd=user, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            events = [json.loads(line) for line in evidence.read_text().splitlines()]
            self.assertTrue(any(e['event'] == 'result' and e['status'] == 'PASS' and 'test_user_module' in e['id'] for e in events), events)
            other = Path(tmp) / 'other'
            other.mkdir()
            (other / 'sitecustomize.py').write_text("import os\nos.environ['CADRE_QA3_CHAIN'] = '1'\n")
            chained = {**env, 'PYTHONPATH': str(shim) + os.pathsep + str(other)}
            out = subprocess.run([sys.executable, '-c', "import os;print(os.environ.get('CADRE_QA3_CHAIN'))"],
                                 env=chained, capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(out, '1')
            silent = {k: v for k, v in env.items() if k not in ('CADRE_QA_EVIDENCE', 'CADRE_QA_TOKEN')}
            out = subprocess.run([sys.executable, '-c', "import sys;print('qa_evidence' in sys.modules)"], env=silent,
                                 capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(out, 'False')


class ProgresslessAlarmTests(unittest.TestCase):
    """FIX-53 / CR-03 (N-BU06): a speed warning alone is not a stop; productive / non-progressing / uncertain work are separated and only
    evidenced 'no progress' (product unchanged + no recorded step + a continuously expensive section) becomes a definite alarm."""

    def setUp(self) -> None:
        import budget_gate as bg
        from unittest.mock import patch
        temp = tempfile.TemporaryDirectory(prefix='qa3-alarm-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        self.enterContext(patch.dict(os.environ, {'CADRE_HOME': str(self.home), 'CADRE_RUN_CAP_USD': '', 'CADRE_TASK_ID': ''}))
        self.bg = bg
        self.store = RunStore(self.home)
        self.ledger = bg.BudgetLedger(self.home / 'chain/request-budget.db')
        self.now_ts = time.time()
        self.wt = self.home / 'product'
        self.wt.mkdir()
        git(self.wt, 'init', '-q')
        (self.wt / 'app.py').write_text('value = 1\n')
        git(self.wt, 'add', 'app.py')
        git(self.wt, 'commit', '-qm', 'fixture')
        self.rid = self.execute_run('fixture')

    def execute_run(self, entry_name: str, worktree: bool = True) -> str:
        rid = self.store.accept(entry_name, 'task')['run_id']
        self.store.update(rid, budget_cap_usd=3, budget_manual=True, created=self.now_ts - 7200, **({'worktree': str(self.wt)} if worktree else {}))
        with self.ledger.transaction() as con:
            for offset in (500, 300, 100):
                con.execute('insert into reservation values(?,?,?,?,?,?,?,?)',
                            (f'{rid}-{offset}', 'opencode-go', 'p', self.now_ts - offset, 450_000, 450_000, f'receipt-{rid}-{offset}', '{"run_id": "%s"}' % rid))
        return rid

    def alarm(self, rid=None, now_ts=None):
        with self.ledger.transaction() as con:
            return self.bg.spend_alarm(self.home, con, rid or self.rid, self.now_ts if now_ts is None else now_ts)

    def continuous(self, rid, record, first=1300):
        self.store.update(rid, spend_warning={**record, 'ts': self.now_ts - 500, 'first_ts': self.now_ts - first})

    def test_three_progress_states_follow_the_contract(self):
        from run_store import approve_spending
        from test_budget_period import snapshot
        import project_mode
        denial, entry = self.alarm()
        self.assertIsNone(denial)
        self.assertEqual((entry['kind'], entry['progress'], entry['first_ts'], entry['call']), ('speed-warning', 'unknown', self.now_ts, 0))
        self.assertIsInstance(entry['product'], str)
        self.continuous(self.rid, entry, first=1100)
        denial, intermediate = self.alarm()
        self.assertIsNone(denial, 'No alarm before the finite duration expires')
        self.assertEqual((intermediate['progress'], intermediate['product']), ('unknown', entry['product']))
        self.continuous(self.rid, entry)
        denial, alarm = self.alarm()
        self.assertIn('awaiting approval', denial)
        self.assertEqual((alarm['kind'], alarm['warning']['progress'], alarm['warning']['first_ts']), ('no-progress-spend', 'none', self.now_ts - 1300))
        self.assertIn('no product change or recorded step', alarm['reason'])
        (self.wt / 'app.py').write_text('value = 2\n')
        denial, changed = self.alarm()
        self.assertIsNone(denial, 'A product change cannot be called no progress')
        self.assertEqual(changed['progress'], 'unknown')
        (self.wt / 'app.py').write_text('value = 1\n')
        self.assertIn('awaiting approval', self.alarm()[0] or '')
        self.bg.progress_write(self.home, self.rid, 'lane-produced', 'g1:' + entry['product'])
        denial, productive = self.alarm(now_ts=time.time())
        self.assertIsNone(denial)
        self.assertEqual((productive['progress'], productive['evidence']['reason']), ('productive', 'lane-produced'))
        self.assertIsNone(self.bg.cap_progress(self.home, self.store.read(self.rid)), 'A lane step does not extend the cap')
        self.bg.progress_write(self.home, self.rid, 'lane-produced', 'g1:' + entry['product'])
        self.assertEqual(len(self.store.read(self.rid)['budget_progress_history']), 1, 'The same step does not renew the clock')
        self.bg.progress_write(self.home, self.rid, 'cpu-movement', 'x')
        self.assertEqual(len(self.store.read(self.rid)['budget_progress_history']), 1, 'A CPU or log signal is not a step')
        self.store.update(self.rid, budget_progress={'reason': 'qa-pass', 'id': 'rev-0', 'ts': self.now_ts - 5000}, budget_progress_history=[])
        self.continuous(self.rid, entry)
        self.assertIn('awaiting approval', self.alarm()[0] or '', 'Do not count old records preceding the interval')
        with self.assertRaisesRegex(self.bg.BudgetDenied, 'awaiting approval'):
            self.ledger.reserve('opencode-go', 10_000, snapshot(50), {'run_id': self.rid})
        record = self.store.read(self.rid)
        self.assertEqual((record['spend_alarm']['kind'], record['spend_alarm']['warning']['progress']), ('no-progress-spend', 'none'))
        self.assertEqual(project_mode.wait_reason(record), 'spend-alarm')
        approve_spending(self.store, self.rid)
        self.ledger.reserve('opencode-go', 10_000, snapshot(50), {'run_id': self.rid})
        record = self.store.read(self.rid)
        self.assertLess(record['spend_alarm']['ts'], record['spend_approval_ts'])
        self.assertEqual(project_mode.wait_reason(record), '')

    def test_unknown_product_disabled_limit_and_new_episode_never_alarm(self):
        from unittest.mock import patch
        rid2 = self.execute_run('without-worktree', worktree=False)
        denial, record = self.alarm(rid2)
        self.assertIsNone(denial)
        self.assertEqual((record['progress'], record['product']), ('unknown', None))
        self.continuous(rid2, record)
        denial, record = self.alarm(rid2)
        self.assertIsNone(denial, 'Do not label spending wasteful when the product snapshot is unknown')
        self.assertEqual(record['progress'], 'unknown')
        denial, record = self.alarm()
        self.continuous(self.rid, record)
        with patch.dict(os.environ, {'CADRE_SPEND_ALARM_NO_PROGRESS_SEC': '0'}):
            self.assertIsNone(self.alarm()[0])
        with patch.dict(os.environ, {'CADRE_SPEND_ALARM_NO_PROGRESS_SEC': 'abc'}):
            self.assertIn('awaiting approval', self.alarm()[0] or '', 'An unreadable limit uses the default')
        self.store.update(self.rid, spend_warning={**record, 'ts': self.now_ts - 1300, 'first_ts': self.now_ts - 3000})
        denial, fresh = self.alarm()
        self.assertIsNone(denial, 'A warning older than one window starts a new interval')
        self.assertEqual(fresh['first_ts'], self.now_ts)
        self.store.update(self.rid, spend_warning={**record, 'ts': self.now_ts - 500, 'first_ts': self.now_ts - 1300, 'product': 'old-summary'})
        denial, different = self.alarm()
        self.assertIsNone(denial, 'A product differing from the initial interval snapshot is progress')
        self.assertEqual(different['progress'], 'unknown')


class WrittenApprovalTests(unittest.TestCase):
    """FIX-51 (Codex P1): the event sequence a long-held Enter can produce cannot pass two typed approvals; every step needs `yes`/`yes` typed followed by Enter."""

    def ui_python(self) -> Path:
        py = Path(os.environ.get('CADRE_TEST_PANEL_PYTHON') or os.environ.get('CADRE_UI_PYTHON')
                  or Path.home() / '.local/share/cadre-ui-venv/bin/python')
        if not py.exists():
            self.skipTest('The Textual environment is not installed')
        return py

    def test_held_enter_stream_cannot_pass_either_stage_but_typed_words_can(self):
        script = r'''
import asyncio, json, os, sys, tempfile, time
from textual.events import Key
home = tempfile.mkdtemp()
os.environ.update(HOME=home, CADRE_HOME=home, CADRE_NO_NET='1', PYTHONDONTWRITEBYTECODE='1')
sys.path.insert(0, sys.argv[1])
import panel_app, permission
from run_store import RunStore
async def held(app, seconds):
    started, count = time.monotonic(), 0
    while time.monotonic() - started < seconds:
        app.post_message(Key('enter', '\r')); count += 1
        await asyncio.sleep(0.03)
    return count
async def run():
    store = RunStore(home); rid = store.accept('fixture', 'synthetic approval')['run_id']
    action = permission.propose(store, rid, 'push', [['git', 'push', 'SYNTHETIC-ONLY-NEVER-EXECUTED']], {'target': 'synthetic'}, {'digest': 'synthetic'})
    status = lambda: permission.read_action(store, rid, action['token'])['status']
    out, callbacks = [], []
    app = panel_app.Cadre(home=home)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        app.operator_approval(rid, action['token'], after=lambda: callbacks.append(1))
        await pilot.pause(0.1)
        out.append(type(app.screen).__name__ + ':' + str(app.screen.written))
        out.append(await held(app, 1.4)); await pilot.pause(0.1)
        out.append(status())
        await pilot.press(*'yes', 'enter'); await pilot.pause(0.2)
        out.append(status())
        out.append(await held(app, 1.0)); await pilot.pause(0.1)
        out.append(status())
        await pilot.pause(panel_app.Approval.DEAD_DURATION_SEC)
        await pilot.press(*'no', 'enter'); await pilot.pause(0.1)
        out.append(status())
        for _ in range(5):
            await pilot.press('backspace')
        await pilot.press(*'yes', 'enter'); await pilot.pause(0.3)
        out.append(status()); out.append(len(callbacks))
        app.push_screen(panel_app.Approval('plain', 'a', lambda: callbacks.append('plain')))
        await pilot.pause(panel_app.Approval.DEAD_DURATION_SEC + 0.1)
        await pilot.press('enter'); await pilot.pause(0.1)
        out.append('plain' in callbacks)
    print(json.dumps(out))
asyncio.run(run())
'''
        result = subprocess.run([str(self.ui_python()), '-c', script, str(LIB)], capture_output=True, text=True, timeout=180,
                                env={k: v for k, v in os.environ.items() if k not in ('CADRE_APPROVAL_DEAD_TIME_SEC', 'CADRE_RUN_ID')})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        out = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(out[0], 'Approval:True')
        self.assertGreater(out[1], 30, 'The held-key simulation produces at least 30 events')
        self.assertEqual(out[2], 'pending', 'First stage: repeated Enter presses are not written approval')
        self.assertEqual(out[3], 'first-approved', 'yes plus Enter is the first written approval')
        self.assertEqual(out[5], 'first-approved', 'Second stage: repeated Enter presses cannot approve')
        self.assertEqual(out[6], 'first-approved', 'A wrong word cannot approve')
        self.assertEqual((out[7], out[8]), ('approved', 1), 'yes plus Enter is the second written approval; execute once')
        self.assertTrue(out[9], 'Simple cancel and plan approvals work with Enter')


class ReceiptAcceptanceTests(unittest.TestCase):
    """FIX-17 (N-BU06): an excess receipt closes the family; only a recorded operator acceptance opens it, the cap does not grow."""

    def setUp(self) -> None:
        import budget_gate as bg
        from unittest.mock import patch
        temp = tempfile.TemporaryDirectory(prefix='qa3-receipt-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.enterContext(patch.dict(os.environ, {'CADRE_HOME': str(self.home), 'CADRE_RUN_CAP_USD': '', 'CADRE_TASK_ID': '',
                                                   'CADRE_SPEND_ALARM': '0'}))
        os.environ.pop('CADRE_RUN_ID', None)
        self.bg = bg
        self.store = RunStore(self.home)
        self.ledger = bg.BudgetLedger(self.home / 'chain/request-budget.db')
        self.rid = self.store.accept('fixture', 'task')['run_id']
        self.store.update(self.rid, budget_cap_usd=2, budget_manual=True)

    def snapshot(self):
        from test_budget_period import snapshot
        return {**snapshot(50), 'or': {'db': 'ok', 'pay': 50, 'available': 50}}

    def test_accepted_overage_reopens_the_family_without_raising_any_cap(self):
        from unittest.mock import patch
        ident = self.ledger.reserve('openrouter', 500_000, self.snapshot(), {'run_id': self.rid})
        self.ledger.settle(ident, 900_000, 'receipt-1')
        with self.assertRaisesRegex(self.bg.BudgetDenied, 'project receipt bound exceeded'):
            self.ledger.reserve('opencode-go', 10_000, self.snapshot(), {'run_id': self.rid})
        with self.ledger.transaction() as con:
            overrun = self.bg.receipt_overruns(con, {self.rid})
        self.assertEqual([(a['id'], a['actual'], a['bound']) for a in overrun], [(ident, 900_000, 500_000)])
        with self.assertRaisesRegex(self.bg.BudgetDenied, 'unknown reservation'):
            self.bg.receipt_acceptance(self.home, 'absent')
        with patch.dict(os.environ, {'CADRE_RUN_ID': self.rid}), self.assertRaisesRegex(self.bg.BudgetDenied, 'outside a run'):
            self.bg.receipt_acceptance(self.home, ident)
        with self.assertRaisesRegex(self.bg.BudgetDenied, 'project receipt bound exceeded'):
            self.ledger.reserve('opencode-go', 10_000, self.snapshot(), {'run_id': self.rid})
        acceptance = self.bg.receipt_acceptance(self.home, ident)
        self.assertEqual((acceptance['already'], acceptance['actual'], acceptance['bound'], acceptance['run_id']), (False, 900_000, 500_000, self.rid))
        self.ledger.reserve('opencode-go', 10_000, self.snapshot(), {'run_id': self.rid})
        record = self.store.read(self.rid)
        last = record['operator_interventions'][-1]
        self.assertEqual((last['kind'], last['detail']['reservation'], last['detail']['actual_usd']), ('receipt-overage-accepted', ident, 0.9))
        self.assertTrue(self.bg.receipt_acceptance(self.home, ident)['already'])
        self.assertEqual(len(self.store.read(self.rid)['operator_interventions']), 1)
        with self.assertRaisesRegex(self.bg.BudgetDenied, 'observed usage exceeded its verified bound'):
            self.ledger.reserve('openrouter', 10_000, self.snapshot(), {'run_id': self.rid})
        with self.ledger.transaction() as con:
            con.execute("delete from pricing_fault where provider='openrouter'")
        with self.assertRaisesRegex(self.bg.BudgetDenied, 'run budget exhausted'):
            self.ledger.reserve('openrouter', 1_200_000, self.snapshot(), {'run_id': self.rid}, run_cap=2_000_000)
        normal = self.ledger.reserve('openrouter', 100_000, self.snapshot(), {'run_id': self.rid}, run_cap=2_000_000)
        self.ledger.settle(normal, 50_000, 'receipt-2')
        with self.assertRaisesRegex(self.bg.BudgetDenied, 'within its verified bound'):
            self.bg.receipt_acceptance(self.home, normal)
        log = self.home / 'chain/recovery.log'
        self.assertIn('receipt-acceptance', log.read_text())

    def test_cli_lists_and_accepts_only_with_an_explicit_answer(self):
        ident = self.ledger.reserve('openrouter', 300_000, self.snapshot(), {'run_id': self.rid})
        self.ledger.settle(ident, 400_000, 'receipt-3')
        env = {**os.environ, 'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(LIB.parent)}
        env.pop('CADRE_RUN_ID', None)
        def cli(*args):
            return subprocess.run([sys.executable, str(LIB / 'budget_gate.py'), *args], env=env, capture_output=True, text=True, timeout=60)
        listed = cli('--receipt-list', self.rid)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn(ident[:8], listed.stdout)
        self.assertIn('--receipt-acceptance ' + ident, listed.stdout)
        self.assertEqual(cli('--receipt-acceptance', ident).returncode, 2)
        self.assertEqual(cli('--receipt-acceptance').returncode, 2)
        with self.assertRaisesRegex(self.bg.BudgetDenied, 'project receipt bound exceeded'):
            self.ledger.reserve('opencode-go', 10_000, self.snapshot(), {'run_id': self.rid})
        accepted = cli('--receipt-acceptance', ident, '--yes')
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertIn('acceptance saved', accepted.stdout)
        self.assertIn('already accepted', cli('--receipt-acceptance', ident, '--yes').stdout)
        self.assertIn('excess receipt absent', cli('--receipt-list').stdout)
        self.ledger.reserve('opencode-go', 10_000, self.snapshot(), {'run_id': self.rid})
        inside = subprocess.run([sys.executable, str(LIB / 'budget_gate.py'), '--receipt-acceptance', ident, '--yes'],
                                env={**env, 'CADRE_RUN_ID': self.rid}, capture_output=True, text=True, timeout=60)
        self.assertEqual(inside.returncode, 2)
        self.assertIn('outside a run', inside.stderr)


class ApprovalDeadTimeTests(unittest.TestCase):
    def ui_python(self) -> Path:
        py = Path(os.environ.get('CADRE_TEST_PANEL_PYTHON') or os.environ.get('CADRE_UI_PYTHON')
                  or Path.home() / '.local/share/cadre-ui-venv/bin/python')
        if not py.exists():
            self.skipTest('The Textual environment is not installed')
        return py

    def test_enter_inside_the_dead_time_is_ignored_and_a_held_key_cannot_pass_two_stages(self):
        """FIX-51: a short dead time after an approval window opens; two consecutive windows cannot be passed with one gesture. q/Esc are not affected."""
        script = r'''
import asyncio, json, os, sys, tempfile
os.environ['CADRE_HOME'] = tempfile.mkdtemp()
sys.path.insert(0, sys.argv[1])
import panel_app
events = []
async def execute():
    app = panel_app.Cadre(sample=True)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.push_screen(panel_app.Approval('first', 'a', lambda: events.append('first')))
        await pilot.pause()
        await pilot.press('enter')
        await pilot.pause()
        events.append(type(app.screen).__name__)
        await pilot.pause(panel_app.Approval.DEAD_DURATION_SEC + 0.1)
        await pilot.press('enter')
        await pilot.pause()
        events.append(type(app.screen).__name__)
        app.push_screen(panel_app.Approval('one', 'a', lambda: app.push_screen(panel_app.Approval('two', 'b', lambda: events.append('two')))))
        await pilot.pause(panel_app.Approval.DEAD_DURATION_SEC + 0.1)
        await pilot.press('enter', 'enter', 'enter')
        await pilot.pause()
        events.append(type(app.screen).__name__ + ':' + app.screen.question)
        await pilot.press('q')
        await pilot.pause()
        events.append(type(app.screen).__name__)
        app.push_screen(panel_app.Approval('endpoint', 'c', lambda: events.append('endpoint')))
        await pilot.pause()
        await pilot.press('escape')
        await pilot.pause()
        events.append(type(app.screen).__name__)
    print(json.dumps([events, panel_app.Approval.DEAD_DURATION_SEC]))
asyncio.run(execute())
'''
        result = subprocess.run([str(self.ui_python()), '-c', script, str(LIB)], capture_output=True, text=True, timeout=120,
                                env={k: v for k, v in os.environ.items() if k != 'CADRE_APPROVAL_DEAD_TIME_SEC'})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        events, dead = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(dead, 0.3)
        self.assertEqual(events[0], 'Approval')
        self.assertEqual(events[1], 'first')
        self.assertNotEqual(events[2], 'Approval')
        self.assertEqual(events[3], 'Approval:two')
        self.assertNotIn('two', events)
        self.assertNotEqual(events[4], 'Approval')
        self.assertNotIn('endpoint', events)
        self.assertNotEqual(events[5], 'Approval')


class AnalystNoteTests(unittest.TestCase):
    """CR-30 (N-REP03): the single analyst / reconciliation record and the note in the report itself; it does not trip the number check."""

    def setUp(self) -> None:
        import report_mode
        temp = tempfile.TemporaryDirectory(prefix='qa3-analyst-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.report = report_mode
        self.store = RunStore(self.home)
        self.rid = self.store.accept('fixture', 'report task')['run_id']
        from run_store import atomic_json
        record = self.store.read(self.rid)
        record['report'] = {'path': 'report_mode.md', 'evidence': 'report_mode.evidence.jsonl'}
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        self.wt = self.home / 'wt'
        self.wt.mkdir()
        (self.wt / 'report_mode.md').write_text('# Summary\nfinding [K1].\n')

    def test_record_and_report_note_are_written_once_and_reapplied_before_qa(self):
        record = self.report.analyst_record(self.store.path(self.rid), self.wt, 'single', ['m/a', '', ''], 'no second strong analyst')
        self.assertEqual((record['mode'], record['first'], record['second'], record['reconciler']), ('single', 'm/a', None, None))
        self.assertEqual(self.store.read(self.rid)['report']['analyst']['reason'], 'no second strong analyst')
        text = (self.wt / 'report_mode.md').read_text()
        self.assertEqual(text.count('Analyst note (G-R7)'), 1)
        self.assertEqual(self.report.unevidenced_numbers(self.report.analyst_note('single', 'no second strong analyst')), [])
        self.assertEqual(self.report.analyst_check(self.store.read(self.rid)['report'], text), [])
        self.report.analyst_record(self.store.path(self.rid), self.wt, 'reconciliation', ['m/a', 'm/b', 'm/c'])
        text = (self.wt / 'report_mode.md').read_text()
        self.assertEqual(text.count('Analyst note (G-R7)'), 1)
        self.assertIn('two independent analyst', text)
        declaration = self.store.read(self.rid)['report']
        self.assertEqual(declaration['analyst']['reconciler'], 'm/c')
        (self.wt / 'report_mode.md').write_text('# Summary\nfinding [K1].\n')
        self.assertEqual(self.report.analyst_check(declaration, (self.wt / 'report_mode.md').read_text())[0].split(':')[0], 'analyst-note-missing')
        self.assertTrue(self.report.analyst_note_apply(self.wt, declaration))
        self.assertFalse(self.report.analyst_note_apply(self.wt, declaration))
        self.assertEqual(self.report.analyst_check(declaration, (self.wt / 'report_mode.md').read_text()), [])
        single = {**declaration, 'analyst': {**declaration['analyst'], 'mode': 'single'}}
        self.assertEqual(self.report.analyst_check(single, (self.wt / 'report_mode.md').read_text())[0].split(':')[0], 'analyst-note-contradicts')
        with self.assertRaises(ValueError):
            self.report.analyst_record(self.store.path(self.rid), self.wt, 'endpoint', ['m/a'])
        self.assertEqual(self.report.analyst_check({'path': 'report_mode.md'}, 'x'), [])

    def test_project_map_header_shows_total_and_truncation(self):
        import project_mode
        repo = self.home / 'repo'
        repo.mkdir()
        git(repo, 'init', '-q')
        for i in range(project_mode.MAP_LINE + 5):
            (repo / f'f{i:03d}.txt').write_text('x')
        git(repo, 'add', '.')
        git(repo, 'commit', '-qm', 'files')
        map = project_mode._map(repo).splitlines()
        self.assertEqual(map[0], f'({project_mode.MAP_LINE + 5} files; first {project_mode.MAP_LINE} shown, TRIMMED)')
        self.assertEqual(len(map), project_mode.MAP_LINE + 1)
        for i in range(project_mode.MAP_LINE, project_mode.MAP_LINE + 5):
            (repo / f'f{i:03d}.txt').unlink()
        git(repo, 'add', '-A')
        git(repo, 'commit', '-qm', 'fewer')
        self.assertEqual(project_mode._map(repo).splitlines()[0], f'({project_mode.MAP_LINE} files)')


class CleanInstallTests(unittest.TestCase):
    """CR-28 (N-PUB01): install into a clean home, uninstall and reinstall with the same manifest; the check is read-only, the repo config has no personal paths."""

    def test_install_check_uninstall_and_reinstall_in_an_empty_home(self):
        import shutil
        from test_source_and_sync import isolated_sync
        temp = tempfile.TemporaryDirectory(prefix='qa3-install-')
        self.addCleanup(temp.cleanup)
        home = Path(temp.name).resolve()
        sync = isolated_sync(home)
        if sync is None:
            self.skipTest('sync.sh absent')
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
            return json.loads((qe / 'chain/runtime-manifest.json').read_text())['files']

        first = push()
        self.assertTrue((qe / 'lib/qa-sim/sitecustomize.py').is_file() and (bin_dir / 'cadre').is_file())
        check = subprocess.run([sys.executable, str(qe / 'lib/install_check.py'), '--json'], capture_output=True, text=True, timeout=120,
                               env={**env, 'CADRE_ENGINE_ROOT': str(qe)})
        report = json.loads(check.stdout)
        status = {m['entry_name']: m['status'] for m in report['items']}
        self.assertEqual((status['CADRE_HOME'], status['CADRE_ENGINE_ROOT'], status['OpenCode config'], status['personal fixed path']), ('PRESENT',) * 4)
        self.assertIn('no installs or downloads', report['note'])
        repo_config = (sync.parent / 'config/opencode.jsonc').read_text()
        self.assertIn('file://{env:HOME}/.local/share/cadre-engineering/lib/cadre-plugin.mjs', repo_config)
        self.assertNotIn('/Users/', repo_config)
        import install_check
        repo_check = install_check.check(qe, cfg, sync.parent / 'engine', environment=env)
        self.assertEqual(next(m for m in repo_check['items'] if m['entry_name'] == 'personal fixed path')['status'], 'PRESENT')
        for managed in ('lib', 'bin', 'hooks', 'tests', 'USAGE.md', 'chain'):
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
        difference = subprocess.run(['bash', str(sync), 'difference'], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual((difference.returncode, difference.stdout.strip()), (0, 'no difference — repo == live'), difference.stdout + difference.stderr)
        self.assertEqual((cfg / 'opencode.jsonc').read_text(), '{}')


class SkeletonTests(unittest.TestCase):
    """CR-33 (N-ENV01): the python skeleton is part of the first commit permission; install/test/run in one record; a restart does not duplicate."""

    def setUp(self) -> None:
        from unittest.mock import patch
        temp = tempfile.TemporaryDirectory(prefix='qa3-skeleton-')
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name).resolve()
        self.home = self.tmp / 'home_dir'
        self.home.mkdir()
        self.enterContext(patch.dict(os.environ, {'CADRE_HOME': str(self.home), 'GIT_AUTHOR_NAME': 'fixture', 'GIT_AUTHOR_EMAIL': 'f@local',
                                                   'GIT_COMMITTER_NAME': 'fixture', 'GIT_COMMITTER_EMAIL': 'f@local'}))
        os.environ.pop('CADRE_RUN_ID', None)
        self.store = RunStore(self.home)

    def approve(self, token: str) -> None:
        import permission
        permission.approve(self.store, 'system', token)
        permission.approve(self.store, 'system', token, permission.read_action(self.store, 'system', token)['confirmation'])
        permission.initialization_execute(self.store, token)

    def test_skeleton_is_deterministic_and_verifiable(self):
        import project_skeleton
        self.assertEqual(project_skeleton.files('Attempt Project', 'python'), project_skeleton.files('Attempt Project', 'python'))
        self.assertEqual((project_skeleton.package_name('Attempt Project'), project_skeleton.package_name('3d-drawing'), project_skeleton.package_name('!!')),
                         ('attempt_project', 'application_3d_drawing', 'application'))
        with self.assertRaises(ValueError):
            project_skeleton.files('x', 'rust')
        target = self.tmp / 'target'
        target.mkdir()
        written = project_skeleton.write(target, 'Attempt', 'python')
        self.assertIn('cadre-skeleton.json', written)
        self.assertEqual(written, project_skeleton.write(target, 'Attempt', 'python'), 'the same content can be written again')
        m = project_skeleton.operation_tree_sleeping(target)
        self.assertEqual((m['round'], m['entry_name'], m['package']), ('python', 'Attempt', 'attempt'))
        (target / 'extra.txt').write_text('x')
        self.assertIsNone(project_skeleton.operation_tree_sleeping(target))
        (target / 'extra.txt').unlink()
        (target / 'Makefile').write_text('broken')
        self.assertIsNone(project_skeleton.operation_tree_sleeping(target))
        with self.assertRaises(FileExistsError):
            project_skeleton.write(target, 'Attempt', 'python')
        with self.assertRaises(FileExistsError):
            project_skeleton.write(self.tmp / 'target', 'Other', 'python')

    def test_make_targets_keep_the_qa_evidence_pythonpath(self):
        """CR-33 / FIX-52 (15.10 campaign R06): `make test` removed the QA evidence shim (qa-sim sitecustomize) from the environment with a fixed `PYTHONPATH=src`;
        the run's QA errored with `qa-discovery-unverified`. The targets append the existing PYTHONPATH at the end."""
        import project_skeleton
        target = self.tmp / 'path-target'
        target.mkdir()
        project_skeleton.write(target, 'Path Attempt', 'python')
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        for goal, part in (('test', '-m unittest discover -q -s tests'), ('run', '-m path_attempt')):
            with self.subTest(target=goal):
                plain = subprocess.run(['make', '-n', goal], cwd=target, env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(plain.returncode, 0, plain.stderr)
                self.assertIn('PYTHONPATH=src ', plain.stdout)
                self.assertIn(part, plain.stdout)
                shim = subprocess.run(['make', '-n', goal], cwd=target, env={**env, 'PYTHONPATH': '/evidence/qa-sim'}, capture_output=True,
                                      text=True, timeout=60)
                self.assertEqual(shim.returncode, 0, shim.stderr)
                self.assertIn('PYTHONPATH=src:/evidence/qa-sim ', shim.stdout)

    def test_typed_init_commits_the_skeleton_under_the_two_step_permission_and_records_the_flow(self):
        import project_skeleton
        import permission
        import run_store
        import project_registry
        import qa_derive
        target = self.tmp / 'fresh-application'
        with self.assertRaises(project_registry.RecordError):
            project_registry.save(target, create=True, home=self.home, round='rust')
        self.assertFalse(target.exists())
        with self.assertRaises(project_registry.RecordPermission) as asked:
            project_registry.save(target, create=True, home=self.home, round='python')
        token = asked.exception.token
        action = permission.read_action(self.store, 'system', token)
        goal = action['identity']['target']['skeleton']
        self.assertEqual((goal['round'], goal['entry_name'], goal['then']), ('python', 'fresh-application', ['make install', 'make test', 'make run']))
        self.assertIn('Makefile', goal['files'])
        self.assertIn('project_skeleton.py', action['display'])
        self.assertIn('Initial baseline (cadre python skeleton)', action['display'])
        self.assertEqual([p.name for p in target.iterdir()], [], 'git init is also an allowed command')
        self.approve(token)
        self.assertTrue((target / 'Makefile').is_file() and (target / 'tests/test_smoke.py').is_file())
        head = git(target, 'rev-parse', 'HEAD')
        self.assertEqual(git(target, 'rev-list', '--count', 'HEAD'), '1')
        self.assertEqual(project_skeleton.commit_tree_sleeping(target, head)['package'], 'fresh_application')
        self.assertTrue(run_store.new_target_initialized(target))
        self.assertEqual(permission.read_action(self.store, 'system', token)['status'], 'completed')
        with self.assertRaises(project_registry.RecordError):
            project_registry.save(target, home=self.home, round='python')
        entry_name = project_registry.save(target, home=self.home)
        self.assertEqual(entry_name, 'fresh-application')
        record = json.loads((self.home / 'projects' / entry_name / 'project_skeleton.json').read_text())
        self.assertEqual((record['status'], [a['entry_name'] for a in record['steps']], [a['rc'] for a in record['steps']]), ('PASS', ['install', 'test', 'run'], [0, 0, 0]))
        self.assertTrue((target / '.venv/bin/python').exists(), 'make install creates a local venv without downloads')
        self.assertIn('fresh_application 0.1.0: hello world', record['steps'][2]['output'])
        self.assertTrue(run_store.new_target_initialized(target), 'An ignored .venv does not dirty the target')
        self.assertEqual(qa_derive.derive(target, LIB.parent)['command'], 'make test')
        again = project_registry.save(target, home=self.home)
        self.assertEqual(again, entry_name)
        self.assertEqual(git(target, 'rev-list', '--count', 'HEAD'), '1')
        self.assertEqual(sorted(p.name for p in (self.home / 'projects').iterdir()), [entry_name])
        self.assertGreaterEqual(json.loads((self.home / 'projects' / entry_name / 'project_skeleton.json').read_text())['ts'], record['ts'])
        (target / 'notes.txt').write_text('elle')
        self.assertFalse(run_store.new_target_initialized(target))

    def test_interrupted_skeleton_write_resumes_with_the_same_identity_and_one_commit(self):
        import project_skeleton
        import permission
        import run_store
        import project_registry
        target = self.tmp / 'half'
        target.mkdir()
        git(target, 'init', '-q', '-b', 'main')
        project_skeleton.write(target, 'half', 'python')
        self.assertTrue(permission.initialized_empty(target))
        with self.assertRaises(project_registry.RecordPermission) as asked:
            project_registry.save(target, create=True, home=self.home)
        action = permission.read_action(self.store, 'system', asked.exception.token)
        self.assertEqual(action['identity']['target']['skeleton']['round'], 'python')
        self.approve(asked.exception.token)
        self.assertEqual(git(target, 'rev-list', '--count', 'HEAD'), '1')
        self.assertTrue(run_store.new_target_initialized(target))
        self.assertEqual(project_registry.save(target, home=self.home), 'half')
        (target / 'Makefile').write_text('broken')
        self.assertFalse(run_store.new_target_initialized(target))
        self.assertFalse(permission.initialized_empty(target))

    def test_cli_registers_a_typed_project_with_two_answers(self):
        target = self.tmp / 'cli-project'
        env = {**os.environ, 'CADRE_HOME': str(self.home)}
        bad = subprocess.run([sys.executable, str(LIB / 'project_registry.py'), '--create', str(target), '--round', 'go'], env=env,
                             capture_output=True, text=True, input='yes\nyes\n', timeout=120)
        self.assertEqual(bad.returncode, 1)
        self.assertIn('python', bad.stderr)
        result = subprocess.run([sys.executable, str(LIB / 'project_registry.py'), '--create', str(target), '--round', 'python'], env=env,
                                capture_output=True, text=True, input='yes\nyes\n', timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(result.stdout.strip().endswith('cli-project'), result.stdout)
        self.assertIn('cadre-skeleton.json', result.stdout)
        self.assertIn('Skeleton flow PASS', result.stderr)
        self.assertEqual(json.loads((self.home / 'projects/cli-project/project_skeleton.json').read_text())['status'], 'PASS')
        self.assertEqual(subprocess.run([sys.executable, str(LIB / 'project_registry.py'), '--create', str(target), '--round'], env=env,
                                        capture_output=True, text=True, timeout=60).returncode, 2)

if __name__ == '__main__':
    unittest.main()
