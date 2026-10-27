"""C2: real scheduler, fake writers, measurable ordering and exclusion."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import test_outcome as fixtures
from dispatch_util import ordered_tasks

import os
from unittest.mock import patch

def setUpModule():
    environment = patch.dict(os.environ, {'CADRE_CHAIN_QUALITY': 'opencode-go/qwen3.8-max'})
    environment.start()
    unittest.addModuleCleanup(environment.stop)



WORKER = '''
import json,os,pathlib,sys,time
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT']+'/lib')
from run_store import RunStore
p=pathlib.Path(os.environ['CADRE_RUN_DIR'])
gid=os.environ['CADRE_TASK_ID']
def event(kind):
    with (p/'writer-events.jsonl').open('a') as f:
        f.write(json.dumps({'id':gid,'event':kind,'time':time.monotonic()})+'\\n')
event('start')
product=pathlib.Path(os.environ['CADRE_WORKTREE'])/'product.txt'
before=product.read_text()
time.sleep(.3)
product.write_text(before+gid+'\\n')
event('end')
RunStore().task_update(os.environ['CADRE_RUN_ID'],gid,state='produced')
'''


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.CompletionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def task(self, gid, dependencies=(), model='ds-pro'):
        return {'id':gid,'definition':'edit product','model':model,'parallel_group':1 if model != 'local' else 0,
                'files':['product.txt'],'acceptance':'product works','difficulty':2,'estimate_min':1,'estimate_usd':0.01,'dependent':list(dependencies)}

    def events(self):
        path=self.fixture.store.path(self.fixture.rid)/'writer-events.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_same_file_writers_are_serial_and_dependencies_precede_consumers(self) -> None:
        tasks=[self.task('g2', ['g1']), self.task('g1'), self.task('g3', model='auto')]
        self.assertEqual(self.fixture.dispatch_fixture('PASS', tasks=tasks, worker=WORKER, plan_extra={'scope_points': 30, 'scope_rationale': 'three dependent steps'}), 0, (self.fixture.store.path(self.fixture.rid) / 'dispatch.log').read_text())
        events=self.events()
        starts={e['id']:e['time'] for e in events if e['event']=='start'}
        ends={e['id']:e['time'] for e in events if e['event']=='end'}
        self.assertGreaterEqual(starts['g2'], ends['g1'])
        ordered=sorted(starts,key=starts.get)
        self.assertTrue(all(starts[b]>=ends[a] for a,b in zip(ordered,ordered[1:])))
        product=(self.fixture.wt/'product.txt').read_text().splitlines()
        for gid in ('g1','g2','g3'):
            self.assertEqual(product.count(gid),1)

    def test_cycle_or_unknown_dependency_is_rejected_before_any_writer(self) -> None:
        tasks=[self.task('g1',['g2']),self.task('g2',['g1'])]
        self.assertNotEqual(self.fixture.dispatch_fixture('PASS', tasks=tasks, worker=WORKER, plan_extra={'scope_points': 30, 'scope_rationale': 'three dependent steps'}),0)
        self.assertEqual(self.events(),[])

    def test_failed_dependency_never_starts_its_consumer(self) -> None:
        tasks=[self.task('g2',['g1'],model='auto'),self.task('g1',model='auto')]
        worker=WORKER.replace("event('start')", "event('start')\nif gid=='g1':\n    RunStore().task_update(os.environ['CADRE_RUN_ID'],gid,state='half')\n    raise SystemExit(1)")
        self.assertNotEqual(self.fixture.dispatch_fixture('PASS', tasks=tasks, worker=worker),0)
        self.assertNotIn('g2',[e['id'] for e in self.events()])

    def test_invalid_dependency_shapes_are_rejected(self) -> None:
        for needs in (['absent'], 'g1', ['g1'], [None]):
            with self.subTest(needs=needs):
                task=self.task('g1')
                task['dependent']=needs
                with self.assertRaises(ValueError):
                    ordered_tasks({'tasks':[task]})


if __name__ == '__main__':
    unittest.main()


class TaskDurationCapTests(unittest.TestCase):
    """The task time cap is derived from estimate_min in the plan; the floor is 30 minutes so a low estimate does not cut the executor
    in the middle of finished work (26.10 pilot 2c: a comment edit estimated at 3 min was cut at a 10 min cap, the work had finished in production)."""

    def cap(self, estimate_min):
        import subprocess, sys
        plan = {'tasks': [{'id': 'g1', 'estimate_min': estimate_min, 'model': 'auto', 'parallel_group': 0, 'difficulty': 1}]}
        out = subprocess.run([sys.executable, str(Path(__file__).resolve().parent.parent / 'lib/dispatch_util.py'), 'abort', 'g1'],
                             input=json.dumps(plan), capture_output=True, text=True, check=True, timeout=60).stdout
        return int(out.strip())

    def test_abort_seconds_have_a_thirty_minute_floor_and_scale_with_the_estimate(self):
        self.assertEqual(self.cap(3), 1800)
        self.assertEqual(self.cap(10), 1800)
        self.assertEqual(self.cap(20), 3600)


class PlanRejectionDistinctionTests(unittest.TestCase):
    """B126 (19.09, caught in the first real run): on a budget denial the model is NEVER called, the output comes back EMPTY
    and `extract` counted empty input as a "schema violation". The upper layer took this for a model defect and went back to the SAME
    model with a correction request — since the budget does not change in two seconds that attempt was guaranteed to be
    wasted. Live evidence: in two separate runs deepseek-v4-pro burned two attempts and fell to minimax-m3.

    B128: in the same file the word `sub_agents` was inside backticks; in double-quoted text that triggers command
    substitution and every run printed `sub_agents: command not found`."""

    SOURCE = Path(__file__).resolve().parents[1] / 'bin/cadre-dispatch.sh'

    def test_the_script_has_no_command_substitution_inside_the_plan_prompt(self):
        """B128 negative guard: if a backtick stays in the prompt text the shell takes it for a command."""
        import subprocess
        r = subprocess.run(['bash', '-n', str(self.SOURCE)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('`sub_agents`', self.SOURCE.read_text(),
                         'Backticks trigger command substitution; use single quotes')

    def test_light_merge_runs_no_project_tests_outside_the_sandboxed_qa_gate(self):
        text = self.SOURCE.read_text()
        block = text[text.index('LIGHT MERGE'):text.index('One final predicate owns QA')]
        for forbidden in ('make test', 'tests/*.sh', 'uv run', 'pytest'):
            self.assertNotIn(forbidden, block, f'A lightweight merge must not run project tests outside its sandbox: {forbidden}')
        self.assertIn('git diff --stat', block)

    def test_the_plan_prompt_forbids_a_qa_only_task(self):
        """B197 (23.09 R1-medium-v2): the planner opened a local lane that only ran `make check` (~20 min); the QA gate and its repair already do that."""
        self.assertIn('verification/qa-only task', self.SOURCE.read_text().lower(), 'The plan prompt must forbid QA-only tasks')

    def test_the_plan_prompt_scores_the_whole_request_and_never_narrows_it(self):
        text = self.SOURCE.read_text().lower()
        self.assertIn('score the entire request from 0 to 100, rather than just your plan', text)
        self.assertIn('a backlog with too many items for 8 tasks', text)
        self.assertIn('do not narrow or expand scope on your own', text)
        correction = text.split('previous attempt rejected', 1)[1].split('continue', 1)[0]
        self.assertIn('recorded scope', correction)
        self.assertIn('record routine decisions as justified assumptions', correction)
        self.assertIn('report critical purpose/authority/cap uncertainty for an operator decision', correction)
        self.assertIn('do not expand scope on your own', correction)
        self.assertNotIn('the broadest reasonable', correction)


class ProjectModeHandoverTests(unittest.TestCase):
    """G-P1: ultra level + planner approval -> the dispatcher hands over to the project coordinator before moving to plan lanes and returns its exit
    code; without approval it continues like very hard. The real script, with fake level/project/helpers."""
    STUBS = {
        'common.sh': 'qe_repo_path(){ printf "%s\\n" "$FAKE_REPO"; }\n',
        'dispatch_util.py': 'import sys\nsys.stdin.read()\nprint("1 task")\n',
        'level.py': 'print("ultra 95 10.0 2 8")\n',
        'project_mode.py': ('import os, sys\na = sys.argv[1:]\n'
                     'if a[0] == "suitable": sys.exit(0 if os.environ["FAKE_SUITABLE"] == "1" else 1)\n'
                     'open(os.environ["FAKE_PROJECT_REF"], "a").write(" ".join(a) + "\\n"); sys.exit(7)\n'),
        'run_store.py': 'import sys\nsys.exit(0 if sys.argv[1] == "plan-ready" else 1)\n',
    }

    def execute_run(self, suitable):
        import os, subprocess, tempfile
        t = Path(tempfile.mkdtemp(prefix='gp1-'))
        (t / 'eng/lib').mkdir(parents=True); (t / 'eng/bin').mkdir(); (t / 'run').mkdir(); (t / 'repo').mkdir()
        for entry_name, content in self.STUBS.items():
            (t / 'eng/lib' / entry_name).write_text(content)
        for entry_name in ('limits.py', 'limits.json'):   # 4c-4: plan deadline from a single source
            (t / 'eng/lib' / entry_name).write_text((Path(__file__).resolve().parents[1] / 'lib' / entry_name).read_text())
        (t / 'eng/bin/cadre-dispatch.sh').write_text(PlanRejectionDistinctionTests.SOURCE.read_text())
        (t / 'run/plan.json').write_text('{"job_id":"x","tasks":[]}')
        (t / 'job.md').write_text('project from scratch\n')
        env = dict(os.environ, CADRE_ENGINE_ROOT=str(t / 'eng'), CADRE_HOME=str(t), CADRE_RUN_ID='main', CADRE_RUN_DIR=str(t / 'run'),
                   CADRE_REAL_OPENCODE='/usr/bin/true', FAKE_REPO=str(t / 'repo'), FAKE_SUITABLE=suitable, FAKE_PROJECT_REF=str(t / 'project-ref-call'))
        r = subprocess.run(['bash', str(t / 'eng/bin/cadre-dispatch.sh'), 'p', str(t / 'job.md')], env=env,
                           capture_output=True, text=True, timeout=60)
        call = (t / 'project-ref-call').read_text() if (t / 'project-ref-call').exists() else ''
        return r.returncode, call, (t / 'run/dispatch.log').read_text()

    def test_ultra_and_approved_job_is_handed_to_the_project_coordinator(self):
        rc, call, log = self.execute_run('1')
        self.assertEqual((rc, call), (7, 'start main p\n'), log)
        self.assertIn('PROJECT MODE', log)

    def test_project_mode_does_not_open_if_the_planner_does_not_approve(self):
        rc, call, log = self.execute_run('0')
        self.assertEqual(call, '')
        self.assertIn('no project mode', log)


class PlanAttemptCountTests(unittest.TestCase):
    """B171 (23.09 T1/T1b/T1v3): the plan loop also counted a budget denial as an "attempt"; three attempts ended before reaching a link
    and the run fell to the rule-based path. The real script is run up to the plan section with a fake budget/router/model."""

    STUBS = {
        'common.sh': 'qe_repo_path(){ printf "%s\\n" "$FAKE_REPO"; }\n',
        'provider_budget.py': 'import json; print(json.dumps({"go": {"ratio": 0.5, "window_open": True}, '
                    '"claude": {"pay": 0, "available_usd": 0}, "ds": {"ratio": 0.5}}))\n',
        'prefilter.py': 'import os; print(os.environ.get("FAKE_PREFILTER", "PASS STANDARD"))\n',
        'model_router.py': ('import os, sys\na = sys.argv[1:]\n'
                            'if a[0] == "first":\n'
                            '    open(os.environ["FAKE_FIRST_COUNTER"], "a").write("x")\n'
                            '    s = os.environ["FAKE_FIRST"].split("|")\n'
                            '    print(s[min(len(open(os.environ["FAKE_FIRST_COUNTER"]).read()), len(s)) - 1])\n'
                            'elif a[0] == "wait": print(os.environ.get("FAKE_WAIT", "0"))\n'
                            'elif a[0] == "next": print(dict(x.split(">") for x in os.environ["FAKE_NEXT"].split(",")).get(a[2], ""))\n'),
        'qe_core.py': ('import json, os\n'
                       'def capture_opencode(rb, cwd, args, timeout):\n'
                       '    model = args[1]\n'
                       '    if os.environ.get("FAKE_PROMPT"): open(os.environ["FAKE_PROMPT"], "a").write(args[2] + "\\n")\n'
                       '    with open(os.environ["FAKE_CALL"], "a") as f: f.write(model + "\\n")\n'
                       '    call_count = open(os.environ["FAKE_CALL"]).read().split().count(model)\n'
                       '    order = dict(x.split("=") for x in os.environ["FAKE_BEHAVIOR"].split(","))[model].split("+")\n'
                       '    d = order[min(call_count, len(order)) - 1]\n'
                       '    if d == "red": return "CADRE_BUDGET_DENIED: allocation exhausted"\n'
                       '    if d == "broken": return "not a plan"\n'
                       '    return json.dumps({"tasks": [{"id": "g1"}]})\n'),
        'dispatch_util.py': ('import json, os, sys\nt = sys.stdin.read()\n'
                             'open(os.environ["FAKE_EXTRACT"], "a").write(" ".join(sys.argv[1:]) + "\\n")\n'
                             'try: d = json.loads(t); assert d["tasks"]\n'
                             'except Exception: print("SCHEMA: plan is not JSON", file=sys.stderr); sys.exit(1)\n'
                             'print(json.dumps(d))\n'),
        'run_store.py': ('import os, pathlib, sys\n'
                    'class RunStore: pass\n'
                    'def capture_source_plan(store, rid, real, model, text, task, timeout, difficulty):\n'
                    '    from qe_core import capture_opencode\n'
                    '    return capture_opencode(real, os.environ["FAKE_REPO"], ["-m", model, text], timeout)\n'
                    'a = sys.argv[1:] if __name__ == "__main__" else [""]\nz = pathlib.Path(os.environ["CADRE_RUN_DIR"])\n'
                    'if a[0] == "task-path": d = z / "tasks" / a[2]; d.mkdir(parents=True, exist_ok=True); print(d)\n'
                    'elif a[0] == "save-plan": (z / "saved-plan.json").write_text(sys.stdin.read()); sys.exit(1)\n'
                    'elif a[0] == "hard-cap": (z / "hard-cap").write_text(" ".join(a[1:])); print("2.00 5.00")\n'),
        'level.py': 'import sys\nprint(37 if sys.argv[1:2] == ["rule"] else "")\n',
    }

    def execute_run(self, first, next, behavior, prefilter='PASS STANDARD', wait='0', record=None):
        import os, subprocess, tempfile
        t = Path(tempfile.mkdtemp(prefix='b171-'))
        (t / 'eng/lib').mkdir(parents=True); (t / 'eng/bin').mkdir(); (t / 'run').mkdir(); (t / 'repo').mkdir()
        for entry_name, content in self.STUBS.items():
            (t / 'eng/lib' / entry_name).write_text(content)
        for entry_name in ('limits.py', 'limits.json'):   # 4c-4: plan deadline from a single source
            (t / 'eng/lib' / entry_name).write_text((Path(__file__).resolve().parents[1] / 'lib' / entry_name).read_text())
        (t / 'eng/bin/cadre-dispatch.sh').write_text(PlanRejectionDistinctionTests.SOURCE.read_text())
        (t / 'job.md').write_text('task\n')
        if record is not None:
            (t / 'run/run.json').write_text(json.dumps(record))
        env = dict(os.environ, CADRE_ENGINE_ROOT=str(t / 'eng'), CADRE_HOME=str(t), CADRE_RUN_ID='fixture', CADRE_RUN_DIR=str(t / 'run'),
                   CADRE_REAL_OPENCODE='/usr/bin/true', FAKE_REPO=str(t / 'repo'), FAKE_FIRST=first, FAKE_NEXT=next,
                   FAKE_BEHAVIOR=behavior, FAKE_CALL=str(t / 'call'), FAKE_PREFILTER=prefilter,
                   FAKE_EXTRACT=str(t / 'extract-argv'), FAKE_FIRST_COUNTER=str(t / 'first-counter'), FAKE_WAIT=wait,
                   FAKE_PROMPT=str(t / 'prompt'))
        env.pop('CADRE_RUN_CAP_USD', None)
        self.last_root = t
        subprocess.run(['bash', str(t / 'eng/bin/cadre-dispatch.sh'), 'p', str(t / 'job.md')], env=env,
                       capture_output=True, text=True, timeout=120)
        call = (t / 'call').read_text().split() if (t / 'call').exists() else []
        return (t / 'run/saved-plan.json').exists(), call, (t / 'run/dispatch.log').read_text()

    def test_a_project_feature_plan_is_told_where_its_acceptance_tests_must_live(self):
        self.execute_run('m1', 'm1>m2', 'm1=plan', record={'feature_acceptance': {'qa_command': 'uv run pytest tests/unit/test_feature.py'}})
        prompt = (self.last_root / 'prompt').read_text()
        self.assertIn('FEATURE ACCEPTANCE COMMAND (the QA gate runs ONLY this command): uv run pytest tests/unit/test_feature.py', prompt)
        self.assertIn("add those files to the task\'s 'files' list", prompt)

    def test_a_plain_run_plan_carries_no_feature_acceptance_line(self):
        self.execute_run('m1', 'm1>m2', 'm1=plan')
        self.assertNotIn('FEATURE ACCEPTANCE COMMAND', (self.last_root / 'prompt').read_text())

    def test_t1_sequence_reaches_the_ring_after_the_denials(self):
        """The same as the T1 log: claude denial, deepseek schema violation, deepseek denial → minimax must be tried."""
        plan, call, log = self.execute_run('m1', 'm1>m2,m2>m3', 'm1=red,m2=broken+red,m3=plan')
        self.assertEqual(call, ['m1', 'm2', 'm2', 'm3'], log)
        self.assertTrue(plan, log)

    def test_a_hard_task_raises_the_run_cap_before_planning(self):
        """B199: a hard job (pre-filter PREMIUM) is raised to its own cap before planning; not called for easy/medium work."""
        _p, _c, log = self.execute_run('m1', 'm1>m2', 'm1=plan', prefilter='PASS PREMIUM')
        self.assertEqual((self.last_root / 'run/hard-cap').read_text(), 'fixture p')
        self.assertIn('run cap $2.00 -> $5.00', log)
        self.assertLess(log.index('run cap'), log.index('plan attempt 1'))
        self.execute_run('m1', 'm1>m2', 'm1=plan')
        self.assertFalse((self.last_root / 'run/hard-cap').exists())

    def test_plan_output_is_validated_against_the_rule_base(self):
        """G-E9 plan limit: the dispatcher computes the rule floor once and gives it to plan extraction; the level limit is applied there."""
        self.execute_run('m1', 'm1>m2', 'm1=plan')
        self.assertEqual((self.last_root / 'extract-argv').read_text().splitlines()[0], 'extract 37')

    def test_a_hard_task_waits_for_renewal_when_no_strong_cloud_can_plan(self):
        """Design 3.3: without a strong cloud, if the renewal is near (router wait > 0) the plan waits and the chain is asked again."""
        plan, call, log = self.execute_run('|m1', 'm1>m2', 'm1=plan', wait='1')
        self.assertIn('BUDGET WAIT (plan)', log)
        self.assertEqual(call, ['m1'], log)
        self.assertTrue(plan, log)

    def test_no_wait_when_the_router_sees_no_reason(self):
        _p, _c, log = self.execute_run('m1', 'm1>m2', 'm1=plan')
        self.assertNotIn('BUDGET WAIT', log)

    def test_a_budget_denial_is_not_logged_as_a_schema_violation(self):
        """23.09 R1-medium-v2: the denial was written as "JSON broken/schema violation" before being classified, and the watcher counted it as an anomaly."""
        _plan, _call, log = self.execute_run('m1', 'm1>m2', 'm1=red,m2=plan')
        self.assertIn('plan BUDGET REJECTION (m1)', log)
        self.assertNotIn('schema-violation (attempt 1); RAW last 300: CADRE_BUDGET_DENIED', log)

    def test_real_failures_still_stop_after_three_attempts(self):
        plan, call, log = self.execute_run('m1', 'm1>m2,m2>m3', 'm1=broken,m2=broken,m3=plan')
        self.assertEqual(call, ['m1', 'm1', 'm2'], log)
        self.assertFalse(plan)
        self.assertIn('no plan produced', log)

    def test_a_chain_that_cycles_through_denials_terminates(self):
        plan, call, log = self.execute_run('m1', 'm1>m2,m2>m1', 'm1=red,m2=red')
        self.assertFalse(plan)
        self.assertIn('no plan produced', log)
        self.assertLessEqual(len(call), 10, call)


class SubCommandContractTests(unittest.TestCase):
    """B130 (20.09, caught in a live run): the `qe_core.py secret` subcommand had been deleted together with the secret policy
    but `cadre-dispatch.sh` kept calling it. The unknown subcommand printed the usage
    text and exited, and the returned garbage text went to the next command as an argument.

    This test binds the contract: EVERY subcommand the shell scripts call must really be recognized
    on the Python side. If a subcommand is deleted its call must be deleted too."""

    ROOT = Path(__file__).resolve().parents[1]

    def _called(self, module):
        """The `<module>" <subcommand>` form in shell scripts. Comment lines do not count."""
        import re
        calls = set()
        for script in (self.ROOT / 'bin').glob('*.sh'):
            for line in script.read_text().split('\n'):
                if line.lstrip().startswith('#'):
                    continue
                for m in re.finditer(rf'{re.escape(module)}"?\s+([a-z][a-z0-9_-]*)', line):
                    calls.add(m.group(1))
        return calls

    def _recognized(self, file):
        import re
        source = (self.ROOT / 'lib' / file).read_text()
        patterns = (r'sys\.argv\[1:2\] == \[[\'"]([a-z0-9_-]+)[\'"]\]',
                    r'sys\.argv\[1\] == [\'"]([a-z0-9_-]+)[\'"]',
                    r'cmd == [\'"]([a-z0-9_-]+)[\'"]', r'kom == [\'"]([a-z0-9_-]+)[\'"]')
        recognized = set()
        for d in patterns:
            recognized |= set(re.findall(d, source))
        import ast
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Compare) and ast.unparse(node.left) == 'sys.argv[1]':
                for operator, value in zip(node.ops, node.comparators):
                    if isinstance(operator, ast.In) and isinstance(value, (ast.Tuple, ast.List)):
                        recognized.update(item.value for item in value.elts
                                       if isinstance(item, ast.Constant) and isinstance(item.value, str))
        return recognized

    def test_every_subcommand_the_shell_calls_actually_exists(self):
        for module, file in (('qe_core.py', 'qe_core.py'),
                             ('dispatch_util.py', 'dispatch_util.py'),
                             ('model_router.py', 'model_router.py')):
            called, recognized = self._called(module), self._recognized(file)
            missing = {k for k in called if k not in recognized}
            with self.subTest(module=module):
                self.assertFalse(missing, f'{module}: the shell calls subcommands that Python does not recognize: {sorted(missing)}')


SCOPE_WORKER = '''
import os,pathlib,subprocess,sys
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT']+'/lib')
from run_store import RunStore
wt=pathlib.Path(os.environ['CADRE_WORKTREE']); gid=os.environ['CADRE_TASK_ID']
(wt/'product.txt').write_text((wt/'product.txt').read_text()+gid+'\\n')
(wt/'extra_test.py').write_text('def test_x():\\n    assert True\\n')
(wt/'extra_committed.py').write_text('X = 1\\n')
subprocess.run(['git','-C',str(wt),'add','extra_committed.py'],check=True)
subprocess.run(['git','-C',str(wt),'-c','user.name=agent','-c','user.email=a@x','commit','-qm','agent commit'],check=True)
if (wt/'notes.txt').exists():
    (wt/'notes.txt').write_text('agent changed\\n')
RunStore().task_update(os.environ['CADRE_RUN_ID'],gid,state='produced')
'''


class LaneScopeTests(unittest.TestCase):
    """B192 (23.09 R1-medium): lane g1 should only have touched evidence_ledger.py but wrote a new test file outside the plan and put it in its own
    commit; no gate caught it, the judge rejected it 70 min later. A NEW file outside the plan is removed from the product (with a backup),
    an EXISTING file changed outside the plan goes to the record and to the judge as a separate criterion."""

    def setUp(self) -> None:
        self.fixture = fixtures.CompletionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def task(self):
        return [{'id': 'g1', 'definition': 'edit product', 'model': 'auto', 'parallel_group': 0, 'files': ['product.txt'],
                 'acceptance': 'product works', 'difficulty': 2, 'estimate_min': 1}]

    def test_missing_plan_warns_and_still_enforces_scope(self):
        import os
        import subprocess
        import sys
        import qe_core
        f = self.fixture
        run = f.store.path(f.rid)
        before = run / 'before.json'
        before.write_text(json.dumps(qe_core.lane_status(f.wt)))
        (f.wt / 'unexpected.py').write_text('print(1)\n')
        (f.wt / 'product.txt').write_text('repair\n')
        r = subprocess.run([sys.executable, qe_core.__file__, 'scope', str(f.wt), str(run / 'plan.json'),
                            'correction-1', str(before), str(run / 'scope-outside')],
                           env={**os.environ, 'CADRE_HOME': str(f.home)}, capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('plan.json missing; scope audit will use an empty plan', r.stdout)
        self.assertFalse((f.wt / 'unexpected.py').exists())
        self.assertTrue((run / 'scope-outside/correction-1/unexpected.py').exists())
        task = f.store.read(f.rid)['tasks']['correction-1']
        self.assertEqual(task['scope_outside_changed'], ['product.txt'])
        self.assertEqual(task['scope_outside_fresh'], ['unexpected.py'])

    def test_new_files_outside_the_plan_are_removed_from_the_product_and_backed_up(self) -> None:
        self.assertEqual(self.fixture.dispatch_fixture('PASS', tasks=self.task(), worker=SCOPE_WORKER), 0)
        f = self.fixture
        for entry_name in ('extra_test.py', 'extra_committed.py'):
            self.assertFalse((f.wt / entry_name).exists(), entry_name)
            self.assertTrue((f.store.path(f.rid) / 'scope-outside' / 'g1' / entry_name).is_file(), entry_name)
        self.assertEqual(f.store.read(f.rid)['tasks']['g1'].get('scope_outside_fresh'), ['extra_committed.py', 'extra_test.py'])
        self.assertIn('OUTSIDE SCOPE', (f.store.path(f.rid) / 'dispatch.log').read_text())
        self.assertIn('g1', (f.wt / 'product.txt').read_text())

    def test_existing_file_changed_outside_the_plan_stays_and_becomes_a_judge_criterion(self) -> None:
        f = self.fixture
        (f.wt / 'notes.txt').write_text('once\n')
        f.git('add', 'notes.txt')
        f.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-qm', 'notes')
        self.assertEqual(f.dispatch_fixture('PASS', tasks=self.task(), worker=SCOPE_WORKER), 0)
        self.assertEqual((f.wt / 'notes.txt').read_text(), 'agent changed\n', 'Do not restore an existing file')
        self.assertEqual(f.store.read(f.rid)['tasks']['g1'].get('scope_outside_changed'), ['notes.txt'])
        criteria = [c['id'] for c in f.store.read(f.rid)['final_result']['review'].get('acceptance', [])]
        self.assertIn('scope_outside', criteria)


class ScopeFolderTests(unittest.TestCase):  # B192: a folder declaration covers the new file under it
    def test_new_file_under_the_declared_folder_is_inside_the_plan(self) -> None:
        import subprocess, tempfile, sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
        import qe_core
        wt = Path(tempfile.mkdtemp(prefix='scope-'))
        g = lambda *a: subprocess.run(['git', '-C', str(wt), *a], check=True, capture_output=True)
        g('init', '-q'); (wt / 'a.txt').write_text('a\n'); g('add', '.'); g('-c', 'user.name=t', '-c', 'user.email=t@x', 'commit', '-qm', 'b')
        once = qe_core.lane_status(wt)
        (wt / 'pkg').mkdir(); (wt / 'pkg' / 'fresh.py').write_text('x = 1\n'); (wt / 'outside.py').write_text('y = 2\n')
        exit, outside = qe_core.scope_audit(wt, {'tasks': [{'files': ['pkg/']}]}, 'g1', once, wt.parent / (wt.name + '-reserve'))
        self.assertEqual((exit, outside), (['outside.py'], []))
        self.assertTrue((wt / 'pkg' / 'fresh.py').exists())


class SubAgentlessWiringTests(unittest.TestCase):  # B196 + 4c-9: the lane starts without sub-agents; the old plan field is not rejected
    def test_lane_starts_without_a_sub_agent(self) -> None:
        f = fixtures.CompletionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        worker = WORKER.replace("event('start')", "event('start')\n(p/'sub.txt').write_text(os.environ.get('CADRE_SUB_AGENTLESS', '<absent>'))")
        task = {'id': 'g1', 'definition': 'edit product', 'model': 'auto', 'parallel_group': 0, 'files': ['product.txt'],
                 'acceptance': 'product works', 'difficulty': 2, 'estimate_min': 1, 'sub_agents': [{'role': 'scout', 'reason': 'old'}]}
        self.assertEqual(f.dispatch_fixture('PASS', tasks=[task], worker=worker), 0)
        self.assertEqual((f.store.path(f.rid) / 'sub.txt').read_text(), '1')
