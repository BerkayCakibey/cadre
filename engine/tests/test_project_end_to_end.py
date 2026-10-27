"""G-P3/G-P4/G-P6 end to end: `project_mode.start` runs child runs with the real RunStore + the real coordinator (`execute_run`) + the real dispatcher;
only the worker (adds a line to a file), the judge (PASS) and the planner are fake. Proven: children open from the tip of the intermediate branch, the product
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
import project_mode  # noqa: E402

LIBRARY = ('worktree_archive.py', 'run_store.py', 'runtime_sync.py', 'outcome.py', 'session.py', 'qe_core.py', 'liveness.py', 'acceptance.py', 'runner.py', 'dispatch_util.py', 'budget_gate.py', 'go_window.py', 'limits.py', 'limits.json', 'limits.mjs', 'opencode_adaptor.py', 'budget-plugin.mjs', 'cadre-plugin.mjs', 'monthly-budget-plugin.mjs', 'model_router.py', 'model_selection.py',
             'model_catalog.py', 'contract.py', 'level.py', 'prefilter.py', 'task_split.py', 'project_mode.py', 'standard-task-form.txt')
SHARED = r'''qe_repo_path(){ cat "$CADRE_HOME/projects/$1/repo-path"; }
qe_verify_ai_worktree(){ return 0; }
qe_make_worktree(){
  local wt="$CADRE_HOME/worktrees/$1/$CADRE_RUN_ID" sha
  sha="$(python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" source-info "$CADRE_RUN_ID" | awk '{print $1}')"
  mkdir -p "${wt%/*}"
  git -C "$(qe_repo_path "$1")" worktree add -q -b "cadre/$CADRE_RUN_ID" "$wt" "$sha" >&2 && printf '%s\n' "$wt"
}
qe_commit_wip(){ git -C "$1" add -A >&2; git -C "$1" -c user.name=f -c user.email=f@x commit -qm "$3" >&2 && echo 1 || echo 0; }
'''
WORKER = '''
import json, os, pathlib, sys
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
from run_store import RunStore
rid, gid = os.environ['CADRE_RUN_ID'], os.environ['CADRE_TASK_ID']
wt = pathlib.Path(os.environ['CADRE_WORKTREE'])
product = wt / 'product.txt'
product.write_text((product.read_text() if product.exists() else '') + rid + '\\n')
writer = RunStore().task_dir(rid, gid) / 'attempts' / 'fx'   # The writer runs in the cloud; the local judge remains independent (B180)
writer.mkdir(parents=True, exist_ok=True)
(writer / 'attempt.json').write_text(json.dumps({'role': 'impl', 'model': 'fixture/writer', 'pid': 123}))
RunStore().task_update(rid, gid, state='produced')
'''
JUDGE = '''
import json, sys, uuid
question = json.loads(sys.argv[-1].splitlines()[-1])
answer = {'verdict': 'PASS', 'revision': question['revision'],
         'acceptance': [{'id': k['id'], 'status': 'PASS', 'evidence': 'product.txt line inspected'} for k in question['criteria']]}
print(json.dumps({'type': 'text', 'sessionID': 'ses_' + uuid.uuid4().hex, 'part': {'type': 'text', 'text': json.dumps(answer)}}))   # a fresh session for every OpenCode call
'''


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=f', '-c', 'user.email=f@x', *args],
                          check=True, capture_output=True, text=True, timeout=30).stdout.strip()


class ProjectEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='project-ref-run-')
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.repo = self.home / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '-b', 'main')
        (self.repo / 'README.md').write_text('empty\n')
        # The baseline runs in the main source, the child QA in the copied engine. The shared QA input
        # must be in both allowed product trees; it does not escape to the operator's engine directory.
        shutil.copyfile(Path(__file__).with_name('qa_fixture.py'), self.repo / 'qa_fixture.py')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'base')
        self.base = git(self.repo, 'rev-parse', 'HEAD')
        (self.home / 'chain').mkdir()
        self.or_price = self.home / 'chain/openrouter-price.json'
        self.or_price.write_text(json.dumps({'version': 2, 'ts': time.time(), 'models': {'openai/gpt-6.1-sol': {
            'input': 1, 'output': 5, 'providers': ['openai'], 'fp8_cache': True}}}))
        pd = self.home / 'projects' / 'p'
        pd.mkdir(parents=True)
        (pd / 'repo-path').write_text(str(self.repo))
        (pd / 'base-branch').write_text('main')
        # This fixture needs only the stdlib; it does not carry the UI test interpreter's external venv
        # into the product QA as a dependency.
        self.qa_command = shlex.join([str(Path(sys.executable).resolve()), 'qa_fixture.py', 'test -s product.txt'])
        (pd / 'qa-command').write_text(self.qa_command)
        root = self.home / 'runner'
        from qa_fixture import instructions
        instructions(root)
        (root / 'lib').mkdir(parents=True)
        (root / 'bin').mkdir()
        for entry_name in (*LIBRARY, 'openrouter-policy.json'):
            shutil.copyfile(LIB / entry_name, root / 'lib' / entry_name)
        for source in LIB.glob('*.py'):
            if not (root / 'lib' / source.name).exists():
                shutil.copyfile(source, root / 'lib' / source.name)
        shutil.copytree(LIB / 'qa-sim', root / 'lib/qa-sim', ignore=shutil.ignore_patterns('__pycache__'), dirs_exist_ok=True)
        shutil.copyfile(LIB.parent / 'bin' / 'cadre-dispatch.sh', root / 'bin' / 'cadre-dispatch.sh')
        (root / 'lib' / 'common.sh').write_text(SHARED)
        import test_end_to_end   # P3 (03.10): no local -> the judge is in the cloud; a fake open Go budget (no network)
        (root / 'lib' / 'provider_budget.py').write_text(test_end_to_end.FAKE_BUDGET)
        (root / 'worker.py').write_text(WORKER)
        (root / 'bin' / 'job-runner.sh').write_text(f'exec "{sys.executable}" "$CADRE_ENGINE_ROOT/worker.py"\n')
        judge = self.home / 'fake-opencode'
        judge.write_text(f'#!{sys.executable}\n' + JUDGE)
        judge.chmod(0o700)
        clean = {k: v for k, v in os.environ.items() if not k.startswith(('CADRE_'))}
        from test_run_sessions import CATALOG   # no real catalogue in an isolated HOME; cloud models count as "in the account"
        (self.home / 'models.json').write_text(json.dumps(CATALOG))
        clean['CADRE_MODEL_CATALOG'] = str(self.home / 'models.json')
        environment = patch.dict(os.environ, {**clean, 'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(root), 'CADRE_REAL_OPENCODE': str(judge),
                                        'OPENCODE_DB': str(self.home / 'absent.db'), 'CADRE_WAIT_THRESHOLD_SEC': '0'}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        from run_store import RunStore
        self.store = RunStore(self.home)
        self.main = self.store.accept('p', 'Write a small tool from scratch')['run_id']
        from test_project_ref import allowed_merge
        manager = patch.object(project_mode, 'merge_ff', side_effect=allowed_merge)
        manager.start()
        self.addCleanup(manager.stop)

    def execute(self, store, cid):
        """Real coordinator; only the child's plan is written beforehand (the planner is not the subject of this test)."""
        from run_store import execute_run
        plan = {'job_id': 'child', 'scope_points': 30, 'tasks': [{'id': 'g1', 'definition': 'append a product line', 'model': 'auto',
                'parallel_group': 0, 'files': ['product.txt'], 'acceptance': 'product.txt contains the line', 'difficulty': 2, 'estimate_min': 1}]}
        (store.path(cid) / 'plan.json').write_text(json.dumps(plan))
        store.update(cid, phase='dispatch')
        return execute_run(store, cid)

    def plan(self, prompt, task_id):
        return 'list:\n' + json.dumps({'features': [
            {'id': 'f1', 'title': 'skeleton', 'description': 'first line', 'acceptance': 'product.txt present', 'qa_command': self.qa_command, 'dependent': [], 'priority': 1},
            {'id': 'f2', 'title': 'second', 'description': 'second line', 'acceptance': 'two line', 'qa_command': self.qa_command, 'dependent': ['f1'], 'priority': 2}]})

    def parent_engine(self, worker_prefix):
        root = self.home / 'runner'
        shutil.copyfile(root / 'bin/cadre-dispatch.sh', root / 'bin/child-dispatch.sh')
        (root / 'bin/cadre-dispatch.sh').write_text(
            'if [ "$CADRE_RUN_ID" = "' + self.main + '" ]; then\n'
            'exec python3 "$CADRE_ENGINE_ROOT/parent.py"\nfi\n'
            'exec bash "$CADRE_ENGINE_ROOT/bin/child-dispatch.sh" "$@"\n')
        (root / 'features.json').write_text(self.plan('', ''))
        (root / 'parent.py').write_text('''import json, os, sys
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
import project_mode
from run_store import execute_run
from pathlib import Path
original_transfer = project_mode.merge_ff
def transfer(repo, branch, k, *, store, run_id):
    try:
        return original_transfer(repo, branch, k, store=store, run_id=run_id)
    except PermissionError as exc:
        if str(exc) != 'git-transfer-permission-required':
            raise
    action = store.read(run_id)['project_git_action']
    first = project_mode.git_transfer_approval(store, run_id, action['token'])
    project_mode.git_transfer_approval(store, run_id, action['token'], first['confirmation'])
    return original_transfer(repo, branch, k, store=store, run_id=run_id)
project_mode.merge_ff = transfer
def child(store, cid):
    plan = {'job_id':'child','scope_points':30,'tasks':[{'id':'g1','definition':'product line',
        'model':'auto','parallel_group':0,'files':['product.txt'],'acceptance':'product.txt present','difficulty':2,'estimate_min':1}]}
    (store.path(cid) / 'plan.json').write_text(json.dumps(plan))
    store.update(cid, phase='dispatch')
    return execute_run(store, cid)
raise SystemExit(project_mode.start(os.environ['CADRE_RUN_ID'], 'p',
    plan=lambda *args: Path(os.environ['CADRE_ENGINE_ROOT'] + '/features.json').read_text(), execute_fn=child))
''')
        (root / 'worker.py').write_text(worker_prefix + WORKER)
        self.store.update(self.main, phase='dispatch')

    def test_real_parent_alarm_approval_resume_and_final_acceptance_keep_all_states_consistent(self):
        from run_store import approve_spending, execute_run
        self.parent_engine('''import json, os, pathlib, sys, time
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
from run_store import RunStore
store = RunStore()
child = store.read(os.environ['CADRE_RUN_ID'])
parent = store.read(child['project_ref_parent'])
project = json.loads((store.path(parent['run_id']) / 'project_mode.json').read_text())
assert project['status'] == 'running' and not project['reason'] and not parent.get('reason')
marker = store.home / 'alarm-once'
if not marker.exists():
    marker.write_text('alarm')
    store.update(child['run_id'], spend_alarm={'ts':time.time()}, reason='budget-wait')
    raise SystemExit(75)
if float((child.get('spend_alarm') or {}).get('ts') or 0) > float(child.get('spend_approval_ts') or 0):
    raise SystemExit(75)
with (store.home / 'observations.jsonl').open('a') as output:
    output.write(json.dumps({'parent':parent['run_id'],'child':child['run_id'],
        'feature':parent.get('project_active_feature'),'project':project['status']}) + '\\n')
''')
        self.assertNotEqual(execute_run(self.store, self.main), 0)
        record = self.store.read(self.main)
        child = record['project_active_run']
        state = json.loads((self.store.path(self.main) / 'project_mode.json').read_text())
        self.assertEqual((record['state'], state['status'], state['reason']), ('retryable', 'stopped', 'spend-alarm'))
        self.assertNotEqual(execute_run(self.store, self.main, proceed=True), 0)
        self.assertFalse((self.home / 'observations.jsonl').exists())
        approve_spending(self.store, self.main)
        self.assertEqual(self.store.read(self.main)['state'], 'retryable')
        self.assertNotIn('running', project_mode.status_summary(self.store, 'p'))
        before_wait = self.store.read(child)['retry_after']
        self.assertNotEqual(execute_run(self.store, self.main, proceed=True), 0)
        self.assertEqual(self.store.read(child)['retry_after'], before_wait)
        self.assertEqual(self.store.read(self.main)['project_active_run'], child)
        self.assertEqual(self.store.read(child)['state'], 'retryable')
        waiting = json.loads((self.store.path(self.main) / 'project_mode.json').read_text())
        self.assertEqual((waiting['reason'], waiting['consecutive_failed'], waiting['features'][0]['attempt']), ('capacity-wait', 0, 0))
        self.assertFalse((self.home / 'observations.jsonl').exists())
        import time
        self.store.update(child, retry_after=time.time() - 1)
        self.store.update(self.main, retry_after=time.time() - 1)
        self.assertEqual(execute_run(self.store, self.main, proceed=True), 0)
        record = self.store.read(self.main)
        state = json.loads((self.store.path(self.main) / 'project_mode.json').read_text())
        rows = [json.loads(line) for line in (self.home / 'observations.jsonl').read_text().splitlines()]
        self.assertEqual([r['feature'] for r in rows], ['f1', 'f2', None])
        self.assertEqual(rows[0]['child'], child)
        self.assertEqual((record['state'], state['status'], state['reason']), ('completed', 'complete', 'final-acceptance-passed'))
        self.assertEqual([self.store.read(r['child'])['state'] for r in rows], ['implemented', 'implemented', 'completed'])
        before = (self.home / 'observations.jsonl').read_bytes()
        self.assertEqual(execute_run(self.store, self.main), 0)
        self.assertEqual((self.home / 'observations.jsonl').read_bytes(), before)
        self.assertEqual(git(self.repo, 'rev-parse', 'main'), self.base)

    def test_real_project_tool_environment_wait_preserves_child_and_blocks_automatic_revival(self):
        from run_store import execute_run, revive
        self.parent_engine('''import os, sys
from pathlib import Path
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
from session import Attempt
if not (Path(os.environ['CADRE_HOME']) / 'supported-environment').exists():
    attempt = Attempt('claude', 'impl', 'fixture/worker', 'work')
    attempt.launch([sys.executable, '-c', 'raise SystemExit(99)'], os.environ['CADRE_WORKTREE'],
                   Path(os.environ['CADRE_HOME']) / 'tool-wait.log', dict(os.environ), ())
''')
        self.assertNotEqual(execute_run(self.store, self.main), 0)
        record = self.store.read(self.main)
        child = record['project_active_run']
        state = json.loads((self.store.path(self.main) / 'project_mode.json').read_text())
        self.assertEqual((record['state'], state['status'], state['reason']),
                         ('retryable', 'stopped', 'project-tool-sandbox-unavailable'))
        self.assertEqual(self.store.read(child)['tool_boundary_wait'], state['reason'])
        self.assertEqual((state['consecutive_failed'], state['features'][0]['attempt']), (0, 0))
        self.assertFalse((Path(self.store.read(child)['worktree']) / 'product.txt').exists())
        with patch('run_store.subprocess.Popen') as start:
            self.assertEqual(revive(self.store, self.home / 'runner'), [])
            start.assert_not_called()
        self.assertNotEqual(execute_run(self.store, self.main, proceed=True), 0)
        self.assertEqual(self.store.read(self.main)['project_active_run'], child)
        again = json.loads((self.store.path(self.main) / 'project_mode.json').read_text())
        self.assertEqual(again['features'][0]['attempt'], 0)
        (self.home / 'supported-environment').touch()
        self.assertEqual(execute_run(self.store, self.main, proceed=True), 0)
        self.assertIsNone(self.store.read(child).get('tool_boundary_wait'))
        final = self.store.read(self.main)
        self.assertEqual(final['state'], 'completed')
        rows = (Path(final['worktree']) / 'product.txt').read_text().splitlines()
        self.assertEqual(rows.count(child), 1)
        self.assertEqual(git(self.repo, 'rev-parse', 'main'), self.base)

    def test_real_parent_cancel_finishes_child_and_project_without_resurrection(self):
        import time
        from run_store import FileLock, active_runs, cancel_run, process_identity, revive
        self.parent_engine('''import os, pathlib, time
home = pathlib.Path(os.environ['CADRE_HOME'])
(home / 'live-child').write_text(os.environ['CADRE_RUN_ID'])
time.sleep(120)
''')
        root = self.home / 'runner'
        foreign = self.store.accept('other', 'unrelated')['run_id']
        other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], start_new_session=True)
        owner = FileLock(self.store.path(foreign) / '.owner.lock')
        self.assertTrue(owner.acquire(blocking=False))
        self.store.update(foreign, state='running', owner_pid=other.pid, owner_identity=process_identity(other.pid))
        proc = subprocess.Popen([sys.executable, str(root / 'lib/run_store.py'), 'run', self.main],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            until = time.monotonic() + 15
            marker = self.home / 'live-child'
            while not marker.exists() and proc.poll() is None and time.monotonic() < until:
                time.sleep(.02)
            self.assertTrue(marker.exists(), self.store.read(self.main))
            cid = marker.read_text()
            self.assertTrue(cancel_run(self.store, self.store.read(self.main)))
            self.assertEqual(proc.wait(timeout=15), 130)
            self.assertEqual(self.store.read(self.main)['state'], 'cancelled')
            self.assertEqual(self.store.read(cid)['state'], 'cancelled')
            state = json.loads((self.store.path(self.main) / 'project_mode.json').read_text())
            self.assertEqual((state['status'], state['reason']), ('stopped', 'cancel'))
            self.assertEqual(active_runs(self.store), [foreign])
            self.assertIsNone(other.poll())
            self.assertEqual(self.store.read(foreign)['state'], 'running')
            with patch('run_store._start') as start:
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

    def test_without_a_measured_last_judge_route_the_project_stays_in_a_capacity_wait_and_produces_no_acceptance(self):
        from outcome import approved
        self.or_price.unlink()
        rc = project_mode.start(self.main, 'p', plan=self.plan, execute_fn=self.execute)
        status = json.loads((self.store.path(self.main) / 'project_mode.json').read_text())
        main = self.store.read(self.main)
        self.assertNotEqual(rc, 0)
        self.assertEqual((status['status'], status['reason']), ('stopped', 'capacity-wait'))
        child = self.store.read(main['project_active_run'])
        self.assertEqual(child['state'], 'retryable')
        self.assertGreater(float(child.get('retry_after') or 0), time.time())
        self.assertFalse(approved(main))
        self.assertFalse(approved(child))
        self.assertNotIn('final_acceptance_run_id', main)

    def test_children_accumulate_on_the_intermediate_branch_with_a_real_run_and_the_main_run_passes_the_acceptance_gate(self):
        from outcome import approved
        rc = project_mode.start(self.main, 'p', plan=self.plan, execute_fn=self.execute)
        main = self.store.read(self.main)
        status = json.loads((self.store.path(self.main) / 'project_mode.json').read_text())
        children = [c['run'] for c in status['children']]
        record = {c: self.store.read(c) for c in children}
        summary = {c: (k['state'], (k.get('final_result') or {}).get('reason')) for c, k in record.items()}
        self.assertEqual(rc, 0, f"{status.get('reason')} {summary}")
        self.assertEqual([c['feature'] for c in status['children']], ['f1', 'f2', 'final-acceptance'])
        # B346/K6 [03.10]: feature children are 'implemented' (an intermediate state), only the final acceptance is 'completed' — both are success
        self.assertTrue(all(k['state'] in ('completed', 'implemented') and k['project_ref_parent'] == self.main for k in record.values()), summary)
        branch = main['project_ref']['intermediate_branch']
        self.assertEqual(git(self.repo, 'show', f'{branch}:product.txt').splitlines(), children, 'Each child builds on its predecessor')
        self.assertEqual(git(self.repo, 'rev-parse', 'main'), self.base, 'The canonical branch is unchanged')
        self.assertTrue(approved(main), 'The coordinator sees final acceptance evidence on the parent run as PASS')
        self.assertEqual(main['final_acceptance_run_id'], children[-1])
        from model_router import OR_SOL
        self.assertEqual(record[children[-1]]['final_result']['review']['model'], OR_SOL,
                         'Final acceptance on the fallback project must pass the measured GPT/FLEX model to the actual executor')
        parent_prompt = Path(main['prompt'])
        original = parent_prompt.read_bytes()
        parent_prompt.write_text('changed original project goal')
        self.assertFalse(approved(main))
        parent_prompt.write_bytes(original)
        self.assertTrue(approved(main))
        self.store.update(self.main, final_acceptance_run_id=children[0])
        self.assertFalse(approved(self.store.read(self.main)))


if __name__ == '__main__':
    unittest.main()
