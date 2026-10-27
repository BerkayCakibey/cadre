"""END TO END (fake data): `cadre task` -> coordinator -> dispatcher (fake planner) -> lanes (fake worker, local+cloud)
-> QA (real command) -> quality (fake judge) -> acceptance -> ledger -> rotation archive -> restore. QA-red and
cancel scenarios on the same path. A single fake `opencode`; a real bin/lib copy, only provider_budget.py is fake (no network). No model/Windows/Claude."""

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
from run_store import RunStore  # noqa: E402

FAKE_OPENCODE = f'''#!{sys.executable}
import json, os, pathlib, re, sys, time
args = sys.argv[1:]
home = pathlib.Path(os.environ['CADRE_HOME'])
task = os.environ.get('CADRE_TASK_ID', '')
req = os.environ.get('CADRE_REQUEST_DIR')
if req:   # The real CLI writes a tools-armed.json receipt when the plugin arms tools; the fake CLI leaves the same receipt
    attempt_directory = pathlib.Path(req).parent; pol = attempt_directory / 'tool-policy.json'; marker = attempt_directory / 'tools-armed.json'
    if pol.exists() and not marker.exists():
        p = json.loads(pol.read_text())
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        os.write(fd, json.dumps({{'owner': p.get('owner'), 'pid': os.getpid(), 'profile_sha256': __import__('hashlib').sha256(str(p.get('profile', '')).encode()).hexdigest()}}).encode()); os.close(fd)
prompt = args[-1]
if prompt.startswith('Continue where you left off'):   # resumed session: the real CLI keeps the task in context; the fake re-reads it
    prompt = (pathlib.Path(os.environ['CADRE_RUN_DIR'])/'tasks'/task/'prompt.md').read_text()
    role = 'worker-resume'
else:
    role = ('plan-review' if prompt.startswith('REVIEW PLAN ALIGNMENT WITH THE REQUEST') else 'worker' if '[QE-TASK v1]' in prompt else 'planner' if 'PLANNER' in prompt else
            'review' if 'QUALITY AND ACCEPTANCE' in prompt else 'repair' if 'QA GATE FAILED' in prompt else 'other')
with (home/'opencode-calls.jsonl').open('a') as f: f.write(json.dumps({{'cwd': os.getcwd(), 'args': args, 'task': task, 'role': role}})+'\\n')
sid = args[args.index('--session')+1] if '--session' in args else 'ses_' + __import__('uuid').uuid4().hex[:12]   # A PID-based id collided with another session after PID reuse in an 8-hour run; the real CLI uses random ids
if os.environ.get('E2E_REUSE_REVIEW_SESSION') == '1' and role in ('plan-review', 'review'):
    sid = 'ses_reused_review'
def event(text):
    print(json.dumps({{'type':'text','sessionID':sid,'part':{{'type':'text','text':text}}}}), flush=True)
print(json.dumps({{'type':'step_start','sessionID':sid,'part':{{'type':'step-start'}}}}), flush=True)
if prompt.startswith('REVIEW PLAN ALIGNMENT WITH THE REQUEST'):
    question=json.loads(prompt.splitlines()[-1])
    pathlib.Path('README.md').read_text()
    answer={{'verdict':os.environ.get('E2E_PLAN_VERDICT','PASS'),'revision':question['revision'],
             'acceptance':[{{'id':c['id'],'status':'PASS','evidence':'README.md source and original request inspected'}} for c in question['criteria']]}}
    event(json.dumps(answer)); sys.exit(0)
if prompt.startswith('Read the source files'):
    question=json.loads(prompt.splitlines()[-1]); pathlib.Path('README.md').read_text()
    plan=question['schema']; plan['job_kind']='code'
    if os.environ.get('E2E_VERIFIED_BACKLOG') == '1':
        plan.update(kind='verified-backlog',tasks=[],source_items=[{{'id':i['id'],'source_sha256':i['sha256'],'tasks':[],
                    'status':'verified','evidence':'a.py and b.py already contain COMPLETE; run the actual acceptance command'}} for i in question['backlog']['items']])
        event(json.dumps(plan)); sys.exit(0)
    plan['tasks']=[{{'id':'g1','definition':'a.py write','files':['a.py'],'acceptance':'a.py py_compile passes','dependent':[],'difficulty':2,'model':'auto','parallel_group':0,'estimate_min':1,'estimate_usd':0}},
                      {{'id':'g2','definition':'b.py write','files':['b.py'],'acceptance':'b.py py_compile passes','dependent':['g1'],'difficulty':2,'model':'auto','parallel_group':0,'estimate_min':1,'estimate_usd':0}}]
    plan['source_items']=[{{'id':i['id'],'source_sha256':i['sha256'],'tasks':['g1','g2'],'status':'pending','evidence':'original backlog inspected'}} for i in question['backlog']['items']]
    event(json.dumps(plan)); sys.exit(0)
if 'YOU ARE A TASK PLANNER' in prompt:
    context=json.loads(prompt.splitlines()[-1])
    source=pathlib.Path('README.md').read_text()
    (home/'legacy-source-read.json').write_text(json.dumps({{'cwd':os.getcwd(),'source':source,'context':context,'prompt':prompt,'config':json.loads(os.environ['OPENCODE_CONFIG_CONTENT'])}}))
    if os.environ.get('E2E_PLAN_SOURCE_WRITE') == '1':
        pathlib.Path('README.md').write_text('unauthorized source change'); sys.exit(1)
    job_id = re.search(r'"job_id":"([^"]+)"', prompt).group(1)
    if os.environ.get('E2E_PLAN'):   # Use an external plan for difficulty and model assignment scenarios
        plan = json.loads(os.environ['E2E_PLAN']); plan['job_id'] = job_id
        event('PLAN:\\n' + json.dumps(plan)); sys.exit(0)
    plan = {{'job_id': job_id, 'tasks': [
        {{'id':'g1','definition':'a.py write','files':['a.py'],'acceptance':'py_compile passes','scope_outside':[],'dependent':[],'difficulty':2,'model':'auto','parallel_group':0,'estimate_min':1,'estimate_usd':0}},
        {{'id':'g2','definition':'b.py write','files':['b.py'],'acceptance':'py_compile passes','scope_outside':[],'dependent':['g1'],'difficulty':3,'model':'ds-pro','parallel_group':1,'estimate_min':1,'estimate_usd':0.01}}],
        'merge_order':['g1','g2'], 'quality':{{'model':'own','scope':'all'}}}}
    event('PLAN:\\n' + json.dumps(plan)); sys.exit(0)
if '[QE-TASK v1]' in prompt:
    files = re.search(r'FILES \\(touch ONLY these\\): (.*)', prompt).group(1).split(', ')
    if os.environ.get('E2E_SLOW') == '1':
        (home/'worker-started').write_text(str(os.getpid())); time.sleep(30)
    for name in files:
        if os.environ.get('E2E_SYNTAX_BROKEN', '') and name.startswith(os.environ['E2E_SYNTAX_BROKEN']):
            pathlib.Path(name).write_text('def f(:\\n'); continue
        broken = os.environ.get('E2E_BROKEN', '') and name.startswith(os.environ['E2E_BROKEN'])
        # The broken fixture compiles but lacks the COMPLETE marker required by QA; a syntax error would stop before QA.
        pathlib.Path(name).write_text(f'print("{{name}} BROKEN")\\n' if broken else f'print("{{name}} COMPLETE")\\n')
    if os.environ.get('E2E_EXTRA_BROKEN') == '1':   # A syntax error in an undeclared file is invisible to fast verification but must be caught by QA
        pathlib.Path(os.environ.get('E2E_EXTRA_BROKEN_FILE', 'broken.py')).write_text('def f(:\\n')
    event('written'); sys.exit(0)
if 'QA GATE FAILED' in prompt:
    event('Correction attempted without modifying the fixture'); sys.exit(0)
if 'QUALITY AND ACCEPTANCE CHECK' in prompt:
    if os.environ.get('E2E_SLOW_FINAL_REVIEW') == '1':
        (home/'final-review-started').write_text(str(os.getpid())); time.sleep(30)
    question = json.loads(prompt.splitlines()[-1])
    if 'source_backlog' in question:
        source=pathlib.Path(question['source_backlog']['full_path']).read_bytes()
        (home/'final-backlog-read.json').write_text(json.dumps({{'bytes':len(source),'sha256':__import__('hashlib').sha256(source).hexdigest()}}))
    answer = {{'verdict': os.environ.get('E2E_VERDICT', 'PASS'), 'revision': question['revision'],
              'acceptance': [{{'id': c['id'], 'status': 'PASS', 'evidence': 'File compiled; QA rc=0'}} for c in question['criteria']]}}
    event(json.dumps(answer)); sys.exit(0)
event('unknown role'); sys.exit(1)
'''

FAKE_BUDGET = '''import json, time
print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "today": time.strftime("%Y-%m-%d"), "remaining_day": 10,
  "go": {"db":"ok","pay":33.333334,"available":33.333334,"window_open":True,"ratio":1.0, "unit": "points"},
  "zen": {"db":"ok","pay":4.0,"available":4.0,"remaining":4.0,"ratio":1.0}, "or": {"db":"ok","pay":4.0,"available":4.0,"remaining":4.0},
  "gemini": {"db":"ok","today_request":10,"remaining_request":440,"ratio":0.9}, "groq": {"db":"ok","today_request":0,"ratio":1.0},
  "claude": {"source":"fresh","valid":True,"pay":0,"percent1_usd":30,"pool_remaining_usd":0,"fable_today":0,"fable_attempts":[]}}))
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
        (self.root / 'lib/provider_budget.py').write_text(FAKE_BUDGET)          # the only fixture inside the engine: no network
        self.fake = Path(self.temp.name) / 'opencode'
        self.fake.write_text(FAKE_OPENCODE)
        self.fake.chmod(0o700)
        for d in ('chain', 'logs', 'hooks', 'projects/e2e'):
            (self.home / d).mkdir(parents=True)
        for name in ('pre-push', 'pre-commit', 'commit-msg'):
            shutil.copy(ENGINE / 'hooks' / name, self.home / 'hooks' / name)
            (self.home / 'hooks' / name).chmod(0o700)
        (self.home / 'chain/expense-coefficient.txt').write_text('1.0\n')
        (self.home / 'chain/go-daily-cap.txt').write_text('2.0\n')
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
        (project / 'qa-command').write_text(command('python3 -m py_compile a.py b.py && grep -q COMPLETE a.py && grep -q COMPLETE b.py', self.root / 'lib/qa_fixture.py'))
        from test_run_sessions import CATALOG
        (self.home / 'models.json').write_text(json.dumps(CATALOG))
        cfg = Path(self.temp.name) / 'opencode-cfg'
        cfg.mkdir()
        (cfg / 'opencode.jsonc').write_text('{}')
        # This fake provider is not an acceptance calibration, it tests the engine loop; the final judge path is an explicit fixture input.
        self.env = {**os.environ, 'CADRE_CHAIN_QUALITY': 'opencode-go/qwen3.8-max', 'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(self.root), 'CADRE_REAL_OPENCODE': str(self.fake), 'CADRE_MODEL_CATALOG': str(self.home / 'models.json'), 'CADRE_RADAR': '0',
                    'CADRE_PROGRESS_POLL': '1', 'CADRE_STALL_SEC': '600', 'CADRE_ABORT_SEC': '600', 'CADRE_MAX_RESUME': '1',
                    'CADRE_PREFILTER_LLM': '0', 'CADRE_WATCH': '0', 'OPENCODE_DB': str(self.home / 'absent.db'), 'CADRE_OC_LOG': str(self.home / 'oc.log'),
                    'OPENCODE_CFG': str(cfg), 'CLAUDE_BIN': '/nonexistent/claude', 'CADRE_GUARD_OWNER_PID': str(os.getpid()),   # B191
                    'CADRE_CRITICAL_FILES': f"{self.home/'chain/expense-coefficient.txt'}:{self.home/'chain/go-daily-cap.txt'}",
                    'CADRE_ROUTER_RECORD': str(self.home / 'chain/model_router.jsonl'), 'CADRE_ROUTER_STATUS': str(self.home / 'chain/provider-status.json')}
        for key in ('CADRE_RUN_ID', 'CADRE_RUN_DIR', 'CADRE_TASKS_DIR', 'CADRE_TASK_ID', 'CADRE_WORKTREE', 'CADRE_OWNER_FDS', 'CADRE_DISPATCH', 'CADRE_RUN_MODEL',
                    'CADRE_COORDINATOR_PID'):
            self.env.pop(key, None)
        self.store = RunStore(self.home)
        # B32: the real cadre.sh/coordinator starts the REAL guards (health-watch, capacity-watch) from the isolated root; they are killed when the test ends
        self.addCleanup(self.kill_guards)

    def kill_guards(self):
        import signal
        for name in ('health-watch', 'capacity-watch'):
            try:
                pid = int((self.home / 'chain' / f'{name}.pid').read_text().strip())
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

    PROMPT = 'Create a.py and b.py, verify that both compile and pass the test\n'

    def submit(self, extra=None):
        result = subprocess.run(['bash', str(self.root / 'bin/cadre.sh'), 'task'], input=self.PROMPT + 'go\nyes\nyes\n', cwd=self.repo,
                                env={**self.env, **(extra or {})}, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Task saved', result.stdout)
        records = self.store.list('e2e')
        return records[-1]['run_id']

    def wait_terminal(self, rid, timeout=150):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = self.store.read(rid)
            if record['state'] in ('completed', 'cancelled', 'failed', 'retryable'):
                if record['state'] == 'completed' and (record.get('final_result', {}).get('delivery') or {}).get('git_permission') == 'required':
                    from run_store import FileLock
                    owner = FileLock(self.store.path(rid) / '.owner.lock')
                    if not owner.acquire(blocking=False):
                        time.sleep(.1)
                        continue
                    owner.close()
                    token = record['final_result']['delivery']['action_token']
                    env = {key: value for key, value in self.env.items() if key != 'CADRE_RUN_ID'}
                    def operator(command, *extra):
                        response = subprocess.run([sys.executable, str(self.root / 'lib/permission.py'), command, rid, token, *extra],
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
        self.PROMPT = self.PROMPT + 'Preserve source acceptance. ' * 700 + '\nTAIL-ACCEPTANCE-7721\n'
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
        self.assertFalse(any('Do not use any tools' in c['args'][-1] for c in self.calls() if c['role'] == 'planner'))

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
        self.assertEqual(record['final_result']['review']['verdict'], 'PASS')
        wt = Path(record['worktree'])
        self.assertEqual((wt / 'a.py').read_text(), 'print("a.py COMPLETE")\n')
        self.assertEqual((wt / 'b.py').read_text(), 'print("b.py COMPLETE")\n')
        self.assertEqual(record['tasks']['g1']['dispatch_result'], 'produced')
        self.assertEqual(record['tasks']['g2']['dispatch_result'], 'produced')
        roles = [c['args'][-1][:40] for c in self.calls()]
        self.assertTrue(any('PLANNER' in c['args'][-1] for c in self.calls()))
        workers = [c for c in self.calls() if c['role'].startswith('worker')]
        self.assertEqual(len(workers), 2)
        g2 = [c for c in workers if c['task'] == 'g2'][0]
        # 18.09: the HEAD of the chain changes as it is measured (kimi-k2.7-code left the chain); the test looks at BEHAVIOUR,
        # not identity: a cloud task must really go out with a cloud model (must not fall to local).
        g2_model = g2['args'][g2['args'].index('-m') + 1]
        self.assertNotIn('lmstudio/', g2_model, 'A cloud task must use a cloud model')
        self.assertIn('/', g2_model)
        self.assertTrue(all(Path(c['cwd']).resolve() == wt.resolve() for c in workers))
        review = [c for c in self.calls() if 'QUALITY AND ACCEPTANCE CHECK' in c['args'][-1]]
        self.assertEqual(len(review), 1)
        # Every worker got the project memory and no harness file entered the product commit.
        committed = subprocess.run(['git', '-C', str(wt), 'show', '--name-only', '--format=', 'HEAD'], capture_output=True, text=True).stdout.split()
        self.assertEqual(sorted(committed), ['a.py', 'b.py'])   # B16: verification/QA leftovers (__pycache__) do not enter the commit
        # Ledger scanner archives the run; rotation refuses first (too young), then archives after aging; restore is exact.
        env = {**self.env, 'OPENCODE_DB': str(self.home / 'absent.db')}
        ledger = subprocess.run([sys.executable, str(self.root / 'lib/run_ledger.py'), 'scan'], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(ledger.returncode, 0, ledger.stderr)
        report = self.home / 'reports' / f'run-e2e-{wt.name}'
        meta = json.loads((report / 'meta.json').read_text())
        self.assertEqual(meta['run_id'], rid)
        self.assertTrue((report / 'attempts/g1').exists() and (report / 'attempts/g2').exists())
        sys.path.insert(0, str(self.root / 'lib'))
        import importlib
        rotation = importlib.import_module('rotation')
        archive = importlib.import_module('worktree_archive')
        self.assertEqual(rotation.rotate(self.home, keep=0)['archived'], 0)      # younger than 2 days: retained
        old = time.time() - 3 * 86400
        os.utime(wt, (old, old))
        import permission
        action = permission.cleanup_proposal(self.store, rid, wt)
        permission.approve(self.store, rid, action['token'])
        self.assertEqual(rotation.rotate(self.home, keep=0)['archived'], 1)
        self.assertFalse(wt.exists())
        archive.restore_run_worktree(self.store, rid)
        self.assertEqual((wt / 'b.py').read_text(), 'print("b.py COMPLETE")\n')
        status = subprocess.run(['git', '-C', str(wt), 'status', '--porcelain'], capture_output=True, text=True).stdout
        self.assertEqual([l for l in status.splitlines() if not l.startswith('??')], [])   # restored tree: no tracked drift

    def test_red_qa_never_becomes_success_and_keeps_the_product(self):
        rid = self.submit({'E2E_BROKEN': 'b.py'})
        record = self.wait_terminal(rid)
        self.assertEqual(record['state'], 'retryable')
        self.assertNotEqual(record.get('final_result', {}).get('status'), 'PASS')
        self.assertEqual(record['final_result']['qa']['status'], 'FAIL')
        self.assertEqual([t['signature'][:3] for t in record.get('correction') or []], ['qa:'])   # G-C16: one correction round, the same error -> stop
        self.assertEqual(len(record['final_result']['qa']['attempts']), 1)   # after the loop started there is no inner repair at the gate
        self.assertEqual((Path(record['worktree']) / 'b.py').read_text(), 'print("b.py BROKEN")\n')   # product kept for inspection
        self.assertFalse(any('QUALITY AND ACCEPTANCE CHECK' in c['args'][-1] for c in self.calls()))   # no review on red QA
        self.assertEqual(len([c for c in self.calls() if 'QA GATE FAILED' in c['args'][-1]]), 1)   # a single inner repair at the first acceptance
        self.assertEqual(len([c for c in self.calls() if 'CORRECTION ROUND' in c['args'][-1]]), 1)   # then a single correction round

    def test_syntax_broken_product_is_caught_before_qa_and_escalated_then_fails(self):
        rid = self.submit({'E2E_SYNTAX_BROKEN': 'b.py'})
        record = self.wait_terminal(rid)
        self.assertEqual(record['state'], 'retryable')
        self.assertNotIn('final_result', record)                              # never reached the acceptance gate
        self.assertEqual(record['tasks']['g2']['dispatch_result'], 'failed')
        workers = [c for c in self.calls() if c['task'] == 'g2' and c['role'].startswith('worker')]
        dispatch_log = (self.store.path(rid) / 'dispatch.log').read_text()
        self.assertGreaterEqual(len(workers), 1, dispatch_log[-1500:])
        self.assertTrue(all('-m' in c['args'] and not c['args'][c['args'].index('-m') + 1].startswith('lmstudio/') for c in workers))
        self.assertTrue(all('--session' in c['args'] for c in workers[1:]))
        self.assertIn('cloud chain exhausted', dispatch_log)

    def test_revise_verdict_is_not_success(self):
        # G-C16: REVISE triggers the correction round; the fixture executor does not change the product in the correction prompt -> the B193 lock, permanent FAIL
        rid = self.submit({'E2E_VERDICT': 'REVISE'})
        record = self.wait_terminal(rid)
        self.assertEqual(record['state'], 'failed')
        self.assertEqual(record['final_result']['status'], 'FAIL')
        self.assertEqual(record['final_result']['reason'], 'revision-already-rejected')
        self.assertEqual([t['signature'][:6] for t in record.get('correction') or []], ['judge:'])

    def test_cancel_during_a_lane_stops_everything_and_marks_cancelled(self):
        rid = self.submit({'E2E_SLOW': '1', 'CADRE_KILL_GRACE': '2'})
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not (self.home / 'worker-started').exists():
            time.sleep(.2)
        self.assertTrue((self.home / 'worker-started').exists(), (self.store.path(rid) / 'coordinator.log').read_text()[-2000:])
        worker = int((self.home / 'worker-started').read_text())
        cancel = subprocess.run(['bash', str(self.root / 'bin/cadre-cancel')], env=self.env, capture_output=True, text=True, timeout=20)
        self.assertIn(rid, cancel.stdout)
        started = time.monotonic()
        record = self.wait_terminal(rid, timeout=90)
        self.assertEqual(record['state'], 'cancelled')
        self.assertLess(time.monotonic() - started, 60)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and subprocess.run(['ps', '-o', 'stat=', '-p', str(worker)], capture_output=True, text=True).stdout.strip() not in ('', 'Z', 'Z+'):
            time.sleep(.2)
        self.assertIn(subprocess.run(['ps', '-o', 'stat=', '-p', str(worker)], capture_output=True, text=True).stdout.strip(), ('', 'Z', 'Z+'))
        self.assertFalse(any('QUALITY AND ACCEPTANCE CHECK' in c['args'][-1] for c in self.calls()))


if __name__ == '__main__':
    unittest.main()
