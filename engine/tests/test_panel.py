from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import panel_data  # noqa: E402
import ui_messages  # noqa: E402
import project_registry  # noqa: E402
from monthly_budget import cap_micro, period  # noqa: E402
from run_store import RunStore  # noqa: E402
import test_end_to_end as e2e  # noqa: E402

OR_SOL, GLM, DS = 'openrouter/openai/gpt-6.1-sol', 'opencode-go/glm-5.3', 'opencode-go/deepseek-v4-pro'

class SpendingWarningTests(unittest.TestCase):
    def test_warning_and_explicit_alarm_remain_distinct_readonly(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, store = Path(tmp), RunStore(tmp)
            rid = store.accept('fixture', 'spending warning')['run_id']
            language = 'en'
            for progress in ('productive', 'unknown'):
                store.update(rid, state='running', spend_warning={'kind': 'speed-warning', 'ts': time.time(),
                             'reason': 'cash: high rate', 'progress': progress})
                record = store.read(rid)
                before = (store.path(rid) / 'run.json').read_bytes()
                warnings = panel_data.run_warnings(home, record)
                self.assertEqual([n['id'] for n in warnings], ['speed-' + rid])
                detail = panel_data.operation_node(home, record, {})
                self.assertNotIn('spending-approval', str(detail))
                self.assertIn('Spending rate warning', str(detail))
                self.assertIn('Recorded progress' if progress == 'productive' else 'Progress unknown', str(detail))
                self.assertEqual(before, (store.path(rid) / 'run.json').read_bytes())
            store.update(rid, spend_alarm={'ts': time.time(), 'reason': 'explicit stop'})
            self.assertEqual({n['id'] for n in panel_data.run_warnings(home, store.read(rid))}, {'speed-' + rid, 'alarm-' + rid})
            store.update(rid, spend_warning={'kind': 'speed-warning', 'ts': time.time() - 601})
            self.assertEqual([n['id'] for n in panel_data.run_warnings(home, store.read(rid))], ['alarm-' + rid])
            store.update(rid, spend_alarm={})


SAMPLE = """
import asyncio, io, os, sys, tempfile
os.environ['CADRE_HOME'] = tempfile.mkdtemp()
sys.path.insert(0, sys.argv[1])
import panel_app, panel_data, ui_messages
from rich.console import Console
def text(r, w):
    c = Console(width=w, record=True, file=io.StringIO())
    c.print(r)
    return c.export_text()
def walk(app, d):
    app.detail_r(d, False)
    for c in d['child']:
        walk(app, c)
async def wait(pilot, app):
    for _ in range(100):
        if app.loaded and app.section('agents')['child']:
            return
        await pilot.pause(0.05)
async def loaded(pilot, app):
    for _ in range(200):
        await app.workers.wait_for_complete()
        if not app.busy and not app.pending:
            return
        await pilot.pause(0.05)
async def execute(size):
    app = panel_app.Cadre(sample=True, simulation=False)
    async with app.run_test(size=size) as pilot:
        await wait(pilot, app)
        events = [line.split(' ', 2)[-1] for path in app.home.glob('runs/*/dispatch.log')
                  for line in path.read_text().splitlines()]
        assert 'plan accepted: 1 task' in events
        assert 'plan accepted: 2 tasks' in events
        assert 'plan accepted: 1 tasks' not in events
        assert app.query_one('#inner').styles.margin.top == 1
        assert app.query_one('#sub').styles.margin.top == 1
        await pilot.press('right')
        parent = app.selected['id']
        await pilot.press('1', 'enter')
        skip = '>'.join(app.ident(d) for d in app.chain()[1:])
        await pilot.press('down', 'enter', 'enter', 'down', 'down', 'enter', 'enter', 'down')
        await pilot.pause()
        deep = '>'.join((d.get('project_ref') or d['entry_name']).split(' · ')[0][:12] for d in app.chain()[1:]) + '|' + app.selected['entry_name'][:2]
        menu = app.query_one('#menu').display
        await pilot.press('enter')
        leaf = len(app.path)
        await pilot.press('o')
        await pilot.pause()
        reader = type(app.screen).__name__
        await pilot.press('q', 'left', 'left', 'left', 'left', 'left')
        await pilot.pause()
        go_back = f"{len(app.path)}:{app.query_one('#menu').display}"
        history = next(c for c in app.section('tasks')['child'] if c.get('id') == 'history')['child']
        sub = text(app.list_r(history, 4, 11), 36)
        scrolling = f"{'▲ 2 more above' in sub}:{len(sub.splitlines()) <= 11}"
        await pilot.press('n')
        await pilot.pause()
        options = [s['round'] for s in app.screen.options]
        await pilot.press('up', 'enter', *'newfolder', 'enter', 'x', 'ctrl+s')
        await pilot.pause()
        fresh = f"{app.screen.step}:{app.screen.target['round']}:{app.screen.folder}"
        await pilot.press('escape', '5')
        english = f"{'tasks running' in str(app.header_r())}"
        walk(app, app.root)
        app.hint_r()
        while app.selected.get('id') != 'color':
            await pilot.press('down')
        await pilot.press('enter')
        await pilot.pause()
        palette = f"{panel_app.R['entry_name']}:{ui_messages.color_read()}:{'Edit money caps' in text(app.description_r(), 80)}"
        ui_messages.color_write('Gruvbox')
        panel_app.palette_setup('Gruvbox')
        for _ in range(16):
            app.sim.step()
        app.root_change(panel_data.tree(app.home))
        delivery = sum(1 for c in app.section('tasks')['child'][-1]['child'] if c.get('delivery'))
        return f'{parent}|{skip}|{deep}|{menu}|{leaf}|{reader}|{go_back}|{scrolling}|{options[0]}:{options[-1]}|{fresh}|{english}|{palette}|{delivery}'
for size in ((160, 44), (100, 30)):
    print(asyncio.run(execute(size)))
"""

REAL = """
import asyncio, contextlib, json, os, sys
os.environ.update(GIT_AUTHOR_NAME='fixture',GIT_AUTHOR_EMAIL='fixture@local',GIT_COMMITTER_NAME='fixture',GIT_COMMITTER_EMAIL='fixture@local',CADRE_RUN_ID='')
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import panel_app, panel_data, ui_messages
from run_store import RunStore
home = Path(sys.argv[2])
calls = []
real_run = panel_app.subprocess.run
class Outcome:
    returncode, stdout, stderr = 0, '✓ Task saved: r1.', ''
def fake(command, **kw):
    if command[0] == 'git':
        return real_run(command, **kw)
    calls.append({'command': command, 'cwd': kw.get('cwd'), 'watch': (kw.get('env') or {}).get('CADRE_WATCH'),
                     'run': (kw.get('env') or {}).get('CADRE_RUN_ID')})
    return Outcome()
panel_app.subprocess.run = fake
panel_app.Cadre.reload = lambda self, *a: None
import screen_render
refresh = []
screen_render.behind_refresh = lambda h: refresh.append(str(h))
store = RunStore(home)
active = store.accept('lab', 'add notes.py')['run_id']
store.update(active, state='running')
async def execute():
    app = panel_app.Cadre(sample=False, home=home)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        start = str(app.header_r())
        app.task_send({'round': 'project_ref', 'entry_name': 'lab'}, 'add notes.py')
        await app.workers.wait_for_complete()
        app.task_send({'round': 'fresh', 'path': str(home / 'fresh-project-ref')}, 'first task')
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen,panel_app.Approval)
        assert not (home/'fresh-project-ref/.git').exists()
        await pilot.pause(panel_app.Approval.DEAD_DURATION_SEC+0.05);await pilot.press(*'yes','enter');await pilot.pause()
        assert isinstance(app.screen,panel_app.Approval)
        assert not (home/'fresh-project-ref/.git').exists()
        await pilot.pause(panel_app.Approval.DEAD_DURATION_SEC+0.05);await pilot.press(*'yes','enter');await pilot.pause()
        await app.workers.wait_for_complete()
        app.root_change(panel_data.tree(home, agents=False))
        await pilot.press('2', 'x')
        await pilot.pause()
        approval = type(app.screen).__name__
        await pilot.pause(panel_app.Approval.DEAD_DURATION_SEC+0.05)
        await pilot.press('enter')
        await pilot.pause()
        await pilot.press('i')
        await pilot.pause()
        followed = app.screen.run_id
        await pilot.press('q')
        app.measurement_refresh()
        no_net = os.environ.pop('CADRE_NO_NET', None)
        app.measurement_refresh()
        app.measurement_refresh()
        os.environ['CADRE_NO_NET'] = no_net or ''
    return approval, start, followed
approval, start, followed = asyncio.run(execute())
print(json.dumps({'approval': approval, 'start': start, 'active': active, 'followed': followed, 'calls': calls, 'refresh': refresh}))
"""


ERROR = """
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import panel_app, panel_data, ui_messages
notifications, actions = [], []
def broken(*a, **k):
    raise ValueError('broken record')
panel_data.tree = broken
async def execute():
    app = panel_app.Cadre(sample=False, home=Path(sys.argv[2]))
    app.notify = lambda m, **kw: notifications.append(str(m))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.2)
        await app.workers.wait_for_complete()
        app.reload(True)
        await app.workers.wait_for_complete()
        await pilot.pause(0.1)
        standing = app.is_running
        screens = []
        for key in ('q', 'escape'):
            app.push_screen(panel_app.Approval('question', 'detail', lambda: actions.append(key)))
            await pilot.pause()
            await pilot.press(key)
            await pilot.pause()
            screens.append(type(app.screen).__name__)
        await pilot.press('n')
        await pilot.pause()
        await pilot.press('enter', 'ctrl+s')
        await pilot.pause()
        step = app.screen.step
    return standing, screens, step
standing, screens, step = asyncio.run(execute())
print(json.dumps({'standing': standing, 'screens': screens, 'actions': actions, 'step': step, 'notifications': notifications}))
"""


WINDOW = """
import asyncio, json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import panel_app, ui_messages
home = Path(sys.argv[2])
for i in range(20):
    (home / 'repos' / f'p{i:02d}').mkdir(parents=True)
    (home / 'projects' / f'p{i:02d}').mkdir(parents=True)
    (home / 'projects' / f'p{i:02d}' / 'repo-path').write_text(str(home / 'repos' / f'p{i:02d}'))
panel_app.Cadre.reload = lambda self, *a: None
def visible(app, size):
    r = app.screen.query_one('#nt-hint').region
    return r.height > 0 and r.bottom <= size[1]
async def execute(size):
    app = panel_app.Cadre(sample=False, home=home)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        await pilot.press('n')
        await pilot.pause()
        first = visible(app, size)
        for _ in range(15):
            await pilot.press('down')
        await pilot.pause()
        selected = app.screen.options[app.screen.choice]['label']
        selected_visible = selected in app.export_screenshot()
        await pilot.press('enter')
        await pilot.pause()
        app.screen.query_one('#nt-text').text = '\\n'.join(f'line {i}' for i in range(40))
        await pilot.press('ctrl+s')
        await pilot.pause()
        approval = (app.screen.step, visible(app, size), 'more line' in app.export_screenshot().replace('&#160;', ' '))
    return [first, selected_visible, *approval]
print(json.dumps([asyncio.run(execute(size)) for size in ((80, 24), (60, 20))]))
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', '-C', str(cwd), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def node_texts(node: dict) -> list[str]:
    texts = [node['entry_name'], node['summary']]
    for round, value in node['detail']:
        if round == 'field':
            texts.append(str(value[0]))
        elif round in ('title', 'section', 'line', 'muted'):
            texts.append(str(value))
    for c in node['child']:
        texts += node_texts(c)
    return texts


def find(node: dict, *ids: str) -> dict:
    for id in ids:
        node = next(c for c in node['child'] if c.get('id') == id)
    return node


def field_list(node: dict) -> dict:
    return dict(value for round, value in node['detail'] if round == 'field')


class RequestApprovalTextTests(unittest.TestCase):
    def test_second_approval_shows_summary_first_then_labelled_exact_command(self):
        proposal = {'token': 'a' * 64, 'action': {'output': 'report', 'target_path': '/target', 'providers': ['openrouter'],
                                                  'duration_minutes': 40, 'budget_usd': '1.5',
                                                  'models': {'plan': ['openrouter/m'], 'review': ['openrouter/m'], 'impl': []}}}
        command = "printf '%s' '{\"action\": 1}' | python3 run_store.py request-approve r1 c1"
        language = 'en'
        first = panel_data.request_approval_text('r1', proposal)
        second = panel_data.request_approval_text('r1', proposal, command)
        self.assertNotIn('printf', first)
        self.assertNotIn('impl:', first, 'an empty model pool is not shown')
        self.assertTrue(second.startswith(first), 'the readable summary comes first')
        self.assertIn(command, second, 'the exact command from the contract is preserved')
        extra = second[len(first):]
        self.assertLess(extra.index('a' * 64), extra.index(command))
        labels = [line for line in extra.splitlines() if line and command not in line and 'a' * 64 not in line]
        self.assertEqual(len(labels), 1, extra)
        self.assertIn(ui_messages.t('Approval token'), extra)

    def test_request_task_shows_work_and_waiting_time_from_the_engine_clock(self):
        from run_store import atomic_json
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(tmp)
            rid = store.accept('fixture', 'request clock')['run_id']
            record = store.read(rid)
            record.update(request={'duration_minutes': 10}, request_clock={'start': time.time() - 900, 'active_sec': 300.0, 'session': None})
            atomic_json(store.path(rid) / 'run.json', record)
            hour = panel_data.request_hour(store.read(rid))
            self.assertEqual(round(hour['operation_sec']), 300)
            self.assertAlmostEqual(hour['wait_sec'], 600, delta=5)
            self.assertIsNone(panel_data.request_hour({'run_id': rid}))

    def test_internal_plan_input_tasks_are_not_listed_as_plan_subtasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, store = Path(tmp), RunStore(tmp)
            rid = store.accept('fixture', 'plan subtasks')['run_id']
            store.update(rid, tasks={'g1': {'state': 'pending'}, 'plan-input-98f5b5d1c311': {'state': 'done'},
                                     'main': {'state': 'pending'}, 'g9': {'state': 'pending'}})
            plan = {'tasks': [{'id': 'g1', 'definition': 'evidence slice'}]}
            nodes = panel_data.sub_task_nodes(home, store.read(rid), plan)
            self.assertEqual([n['id'] for n in nodes], ['g1', 'g9'], 'a real task outside the plan is visible, the internal plan entry is not')


class PanelDataTests(unittest.TestCase):
    def setup(self, home: Path, repo: Path) -> dict:
        for project_ref in ('lab', 'lab--ptrial'):
            (home / 'projects' / project_ref).mkdir(parents=True)
            (home / 'projects' / project_ref / 'repo-path').write_text(str(repo))
        (repo / 'a.py').write_text('x = 1\n')
        git(repo, 'init', '-q', '-b', 'main')
        git(repo, 'add', '.')
        git(repo, 'commit', '-qm', 'base')
        base = git(repo, 'rev-parse', 'HEAD')
        wt = home / 'wt'
        git(repo, 'worktree', 'add', '-q', '-b', 'cadre/attempt', str(wt))
        (wt / 'a.py').write_text('x = 2\n')
        (wt / 'docs').mkdir()
        (wt / 'docs/report_mode.md').write_text('# Report\n\nsummary line\n')
        git(wt, 'add', '.')
        git(wt, 'commit', '-qm', 'product')
        store = RunStore(home)
        finished = store.accept('lab', 'finished task', source_sha=base)['run_id']
        store.update(finished, state='completed', worktree=str(wt), budget_spent_usd=0.4, budget_cap_usd=2.0,
                     final_result={'status': 'PASS', 'qa': {'status': 'PASS'}, 'review': {'status': 'PASS', 'model': 'opencode-go/qwen3.8-max',
                                                                                          'verdict': 'PASS', 'independent': True}})
        report = store.accept('lab', 'architecture report', source_sha=base)['run_id']
        store.update(report, state='completed', worktree=str(wt), delivery_merge='2026-10-08 10:00:00', final_result={'status': 'PASS'})
        record = json.loads((store.path(report) / 'run.json').read_text())
        record.update(mode='report', report={'path': 'docs/report_mode.md', 'sections': ['Summary']})
        (store.path(report) / 'run.json').write_text(json.dumps(record))
        normal = store.accept('lab', 'add notes.py\ndetail')['run_id']
        store.update(normal, state='running', plan_model=GLM, budget_cap_usd=2.0)
        (store.path(normal) / 'plan.json').write_text(json.dumps({'tasks': [
            {'id': f'g{i}', 'definition': f'task {i}', 'files': [f'{i}.py'], 'acceptance': 'pytest', 'dependent': [], 'difficulty': 2, 'model': 'ds-pro',
             'parallel_group': 1, 'estimate_min': 5, 'estimate_usd': 0.1} for i in (1, 2, 3)]}))
        store.task_update(normal, 'g1', state='produced')
        store.task_update(normal, 'g2', state='running')
        (store.path(normal) / 'dispatch.log').write_text('2026-10-08 15:18:01 plan acceptance: 3 task\n2026-10-08 15:20:00 task g1 produced\n'
                                                        '2026-10-08 15:20:00 task g1 produced\n')
        attempt = store.path(normal) / 'tasks/g2/attempts/a1'
        attempt.mkdir(parents=True)
        (attempt / 'attempt.json').write_text(json.dumps({'model': DS, 'role': 'impl', 'session_id': 'ses_1'}))
        (attempt / 'stdout.jsonl').write_text('\n'.join(json.dumps(e) for e in [
            {'type': 'reasoning', 'timestamp': '2026-10-08T12:20:17.829Z', 'part': {'text': 'first I will   read the tests'}},
            {'type': 'tool_use', 'timestamp': 1759926017829, 'part': {'tool': 'bash', 'state': {'input': {'command': 'pytest -q'}}}},
            {'type': 'text', 'timestamp': '2026-10-08T12:21:00Z', 'part': {'text': '{"a": 1}'}}]) + '\nbroken line\n')
        child = store.accept('lab--ptrial', 'f1 first')['run_id']
        store.update(child, state='implemented', final_result={'status': 'APPLIED', 'qa': {'status': 'PASS'}}, project_ref_parent='parent')
        store.task_update(child, 'g1', state='produced')
        last = store.accept('lab--ptrial', 'last acceptance')['run_id']
        store.update(last, state='running', project_ref_parent='parent')
        risky = store.accept('lab--ptrial', 'f4 risky')['run_id']
        store.update(risky, state='implemented', project_ref_parent='parent', final_result={
            'status': 'APPLIED', 'qa': {'status': 'PASS'}, 'review': {'status': 'PASS', 'verdict': 'PASS', 'model': GLM}})
        big = store.accept('lab', 'big task')['run_id']
        store.update(big, state='running', project_active_feature='f2', budget_spent_usd=1.0, budget_cap_usd=2.0, project_budget_cap_usd=20.0)
        store.update(child, budget_spent_usd=7.0)
        (store.path(big) / 'project_mode.json').write_text(json.dumps({'status': 'running', 'cap_usd': 10, 'children': [
            {'feature': 'f1', 'run': child}, {'feature': 'final-acceptance', 'run': last}], 'features': [
            {'id': 'f1', 'title': 'first', 'status': 'passed', 'attempt': 1, 'run': [child], 'dependent': ['f0'], 'scope_points': 40},
            {'id': 'f2', 'title': 'second', 'status': 'waiting', 'attempt': 0},
            {'id': 'f3', 'title': 'third', 'status': 'stuck', 'attempt': 3},
            {'id': 'f4', 'title': 'fourth', 'status': 'passed', 'attempt': 1, 'run': [risky], 'risky': True}]}))
        (home / 'chain').mkdir()
        con = sqlite3.connect(home / 'chain/monthly-budget.db')
        con.execute('create table reservation(id text primary key, provider text not null, period text not null, created real not null, '
                    'reserved integer not null, actual integer, receipt text unique, metadata text not null)')
        for i, (provider, amount) in enumerate((('openrouter', 1_000_000), ('opencode-go', 5_000_000))):
            con.execute('insert into reservation values (?,?,?,?,?,?,?,?)', (f'r{i}', provider, period(), 1.0, amount, amount, f'm{i}', '{}'))
        con.commit()
        con.close()
        return {'finished': finished, 'report': report, 'normal': normal, 'big': big}

    def tree(self, home: Path) -> tuple[dict, dict]:
        view = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'today': time.strftime('%Y-%m-%d'),
                   'go': {'own': {'rolling': 10, 'weekly': 20, 'monthly': 95}, 'available': 3, 'window_open': True},
                   'or': {'remaining': 12.5, 'available': 12.5}, 'zen': {'remaining': 4}, 'claude': {'used7': 50, 'cap': 77}}

        def chain(role: str, **_: object) -> tuple[list, list]:
            return ([GLM], [f'{OR_SOL}: OK (pay present)', f'{OR_SOL}: no verified share']) if role == 'plan' else ([DS], [])
        panel_data.clean()
        with patch('screen_render.budget_view', return_value=(view, 30)), \
             patch('model_router.ROLES', {'plan': [OR_SOL, GLM], 'impl': [DS], 'impl-light': [DS]}), \
             patch('model_router.chain', side_effect=chain), patch('model_router.status_read', return_value={}), \
             patch('model_catalog.context', return_value=1048576), patch('model_catalog.price', return_value={'input': 0.5, 'output': 1.5}), \
             patch('outcome.approved', return_value=True):
            fast = panel_data.tree(home, agents=False)
            return fast, panel_data.tree(home)

    def test_real_data_tree_follows_the_engine_and_touches_nothing(self):
        with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as r:
            home, repo = Path(t), Path(r)
            k = self.setup(home, repo)
            once = sorted(p.relative_to(home) for p in home.rglob('*') if '.git' not in p.parts)
            fast, root = self.tree(home)
            projects = panel_data.projects(home)
            self.assertEqual(find(fast, 'agents')['child'], [], 'the first stage does not compute the model chain')
            self.assertEqual([(b['id'], b['entry_name']) for b in root['child']],
                             [('panel', 'Panel'), ('tasks', 'Tasks'), ('budget', 'Budget'), ('agents', 'Agents'), ('settings', 'Settings')])
            panel = find(root, 'panel')
            self.assertEqual([(c['id'], c.get('git')) for c in panel['child']],
                             [('current', ['tasks']), ('summary', ['tasks', 'history']), ('attention', None), ('panel-budget', ['budget'])])
            tasks = find(root, 'tasks')
            self.assertEqual((sorted(c['id'] for c in tasks['child'][:-1]), tasks['child'][-1]['id']),
                             (sorted([k['big'], k['normal']]), 'history'), 'child runs do not appear in the top list')
            normal = find(tasks, k['normal'])
            self.assertEqual([c['id'] for c in normal['child']], ['plan', 'test', 'judge', 'agents', 'spend', 'events', 'operation', 'choice'])
            self.assertEqual([a for a, _ in [v for round, v in normal['description'] if round == 'field']],
                             ['Plan', 'Test', 'Judge', 'Agents', 'Spend', 'Events', 'Decision and operations', 'Task model selection'], 'the task menu describes its contents')
            self.assertEqual([(c['id'], c['status']) for c in find(normal, 'plan')['child']], [('g1', 'finished'), ('g2', 'now_ts'), ('g3', 'waiting')])
            record = [v for round, v in find(normal, 'plan', 'g2')['detail'] if round == 'record']
            self.assertEqual([s[6:] for s in record[0]], ['first I will read the tests', '(JSON output · 8 characters)'])
            self.assertRegex(record[1][0], r'^\d\d:\d\d bash +pytest -q$')
            self.assertEqual(normal['now'], 'Coding · g2 task 2')
            self.assertEqual(next(v for round, v in find(normal, 'events')['detail'] if round == 'record'),
                             ['2026-10-08 15:18:01 (Timezone unrecorded) plan acceptance: 3 task',
                              '2026-10-08 15:20:00 (Timezone unrecorded) task g1 produced'], 'the same event line once, absolute time preserved')
            big = find(tasks, k['big'])
            self.assertEqual([c['id'] for c in big['child']], ['plan', 'last-check', 'agents', 'spend', 'events', 'operation', 'choice'])
            self.assertEqual([(c['id'], c['status']) for c in find(big, 'plan')['child']],
                             [('f1', 'finished'), ('f2', 'now_ts'), ('f3', 'error'), ('f4', 'finished')])
            self.assertEqual([c['id'] for c in find(big, 'plan', 'f1')['child']], ['plan', 'test'], 'no review in the part, it happens in the final check')
            self.assertEqual(([c['id'] for c in find(big, 'plan', 'f4')['child']], find(big, 'plan', 'f4', 'judge')['summary']),
                             (['plan', 'test', 'judge'], 'Pass'), "the risky part's own judge review is visible")
            self.assertEqual([c['id'] for c in find(big, 'last-check')['child']], ['plan', 'test', 'judge'])
            self.assertEqual(field_list(find(big, 'plan', 'f1'))['Depends on'], 'f0')
            self.assertEqual((find(big, 'spend')['summary'], field_list(big)['Spend']), ('$8.00 / $20.00', '$8.00 / $20.00'),
                             "the big task's spend comes from the child runs and the project_ref cap")
            self.assertEqual(dict(v for round, v in find(root, 'budget')['context'] if round == 'field')['lab'], '$8.40', 'counts the highest-spending child runs')
            history = find(tasks, 'history')
            finished = find(history, k['finished'])
            delivery = find(finished, 'delivery')
            self.assertTrue(finished['delivery'] and delivery['delivery'])
            self.assertEqual((delivery['summary'], field_list(delivery)['Branch'], field_list(delivery)['Changes']), ('Awaiting delivery', 'cadre/attempt', '2 files · +4 −1'))
            self.assertIn('-x = 1', panel_data.file_read(delivery['file'])[2])
            self.assertIn('Awaiting delivery', finished['summary'])
            report_task = find(history, k['report'])
            report = find(report_task, 'report')
            self.assertEqual((report['file']['round'], panel_data.file_read(report['file'])[2].splitlines()[0]), ('md', '# Report'))
            self.assertEqual(find(report_task, 'delivery')['summary'], 'Merged · 2026-10-08 10:00:00')
            self.assertIn('g1 · task 1', panel_data.file_read(find(normal, 'plan')['file'])[2])
            budget = find(root, 'budget')
            self.assertEqual((find(budget, 'go')['summary'], find(budget, 'openrouter')['summary'], find(budget, 'cash')['summary']),
                             ('Monthly 95%', '$12.50', f"$1.00 / {panel_data.money(cap_micro() / 1e6)}"), 'Go does not count toward the cash cap')
            attention = find(panel, 'attention')
            self.assertEqual(find(attention, 'attention-go')['git'], ['budget', 'go'])
            self.assertIn('Stuck part · lab', [u['entry_name'] for u in attention['child']])
            agents = find(root, 'agents')
            self.assertEqual([c['entry_name'] for c in agents['child']], ['Planner', 'Developer', 'Pre-screener'],
                             'only the configured main roles are visible')
            self.assertEqual(find(agents, 'impl', 'impl-light')['entry_name'], 'Lightweight development')
            self.assertTrue(all(b.get('description') and b['description'][0][0] == 'title' for b in root['child']), 'every section has a description')
            rings = find(agents, 'plan')['child']
            self.assertEqual([(c['entry_name'], c['summary'], field_list(c)['Reason']) for c in rings],
                             [(f'1. {OR_SOL}', 'Eliminated', 'no verified share'), (f'2. {GLM}', 'First choice', 'In the chain, share and context fit')])
            self.assertEqual((field_list(rings[0])['Context'], field_list(rings[0])['Price']), ('1.05M tokens', 'Input $0.50 · output $1.50 / 1M tokens'))
            self.assertEqual(field_list(find(agents, 'on-evaluation'))['Model'], 'groq/openai/gpt-oss-120b')
            self.assertEqual(find(root, 'settings', 'color').get('action'), 'color')
            self.assertEqual(projects, ['lab'])
            self.assertFalse([m for m in node_texts(root) if ';' in m], 'no semicolons in panel text')
            self.assertFalse([k for k, v in ui_messages.SINGULAR.items() if ';' in k or ';' in v], 'no semicolons in the singular table (FIX-41)')
            self.assertEqual(find(root, 'tasks')['entry_name'], 'Tasks')
            self.assertEqual(find(root, 'tasks', k['normal'])['description'][0], ('title', 'This task'))
            self.assertEqual((ui_messages.t('{n} tasks', n=1), ui_messages.t('{n} tasks', n=2), ui_messages.t('{f} files · +{e} −{s}', f=1, e=2, s=0)),
                             ('1 task', '2 tasks', '1 file · +2 −0'))
            then = sorted(p.relative_to(home) for p in home.rglob('*') if '.git' not in p.parts and '__pycache__' not in str(p))
            self.assertEqual(then, once, 'the panel writes no file')


    def test_finished_task_delivery_is_rechecked_when_the_product_changes(self):
        with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as r:
            home, repo = Path(t), Path(r)
            k = self.setup(home, repo)
            panel_data.clean()

            def approval(record: dict) -> bool:
                return (Path(record['worktree']) / 'a.py').read_text() == 'x = 2\n'
            with patch('outcome.approved', side_effect=approval):
                first = find(panel_data.tree(home, agents=False), 'tasks', 'history', k['finished'])
                (home / 'wt/a.py').write_text('x = 3\n')
                panel_data.TRACE.clear()
                then = find(panel_data.tree(home, agents=False), 'tasks', 'history', k['finished'])
            self.assertEqual((bool(first.get('delivery')), bool(then.get('delivery'))), (True, False), 'the delivery expectation drops when the product changes')

    def test_claude_thinking_text_and_tool_events_are_shown(self):
        with tempfile.TemporaryDirectory() as t:
            attempt = Path(t)
            events_ = [{'type': 'system', 'subtype': 'init'},
                        {'type': 'assistant', 'message': {'content': [{'type': 'thinking', 'thinking': 'reading   the plan'},
                                                                      {'type': 'text', 'text': 'Opening the file'},
                                                                      {'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'pytest -q'}}]}},
                        {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Read', 'input': {'file_path': 'a.py'}}]}},
                        {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'ok'}]}}]
            (attempt / 'stdout.jsonl').write_text('\n'.join(json.dumps(e) for e in events_) + '\nbroken\n{"type": "assistant", "message": {"con')
            thought, tool = panel_data.brain(attempt)
        self.assertEqual(([x[6:] for x in thought], [x[6:] for x in tool]),
                         (['reading the plan', 'Opening the file'], ['Bash     pytest -q', 'Read     a.py']))

    def test_old_running_and_old_deliverable_tasks_stay_visible_beyond_recent_records(self):
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            store = RunStore(home)
            old = store.accept('lab', 'old running')['run_id']
            store.update(old, state='running')
            (home / 'wt-old').mkdir()
            delivery = store.accept('lab', 'old delivery')['run_id']
            store.update(delivery, state='completed', worktree=str(home / 'wt-old'), final_result={'status': 'PASS'})
            for i in range(160):
                store.update(store.accept('lab', f'fresh {i}')['run_id'], state='completed', final_result={'status': 'FAIL'})
            panel_data.clean()
            with patch('outcome.approved', return_value=True):
                root = panel_data.tree(home, agents=False)
        self.assertIn(old, [c['id'] for c in find(root, 'tasks')['child']], 'an old running task stays in the list out of 150 records')
        self.assertIn(delivery, [c['id'] for c in find(root, 'panel', 'attention', 'attention-delivery')['child']], 'old delivery remains pending in Attention')

    def test_one_broken_record_does_not_take_down_the_tree(self):
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            store = RunStore(home)
            good = store.accept('lab', 'good task')['run_id']
            store.update(good, state='running')
            broken = {}
            for entry_name, field_list_ in (('history', {'state': 'completed', 'worktree': 5, 'final_result': ['y'], 'tasks': ['x']}),
                                 ('active', {'state': 'running', 'prompt': 5, 'spend_alarm': ['z']})):
                rid = store.accept('lab', f'broken {entry_name}')['run_id']
                path = store.path(rid) / 'run.json'
                path.write_text(json.dumps({**json.loads(path.read_text()), **field_list_}))
                broken[entry_name] = rid
            (store.path(broken['active']) / 'project_mode.json').write_text(json.dumps({'features': [{'id': 'f1', 'run': {'a': 1}, 'status': 'stuck'}]}))
            _, root = self.tree(home)
            tasks = find(root, 'tasks')
            active, history = find(tasks, broken['active']), find(tasks, 'history', broken['history'])
            self.assertEqual([(n['summary'], n['status']) for n in (active, history)], [('Unreadable', 'error')] * 2)
            self.assertTrue(active['active'] and active['run'] == broken['active'], 'an unreadable running task can still be cancelled')
            self.assertIn('"worktree": 5', panel_data.file_read(history['file'])[2])
            self.assertNotEqual(find(tasks, good)['summary'], 'Unreadable')
            self.assertIn('Stuck part · lab', [u['entry_name'] for u in find(root, 'panel', 'attention')['child']])

    def test_missing_measurement_bad_cap_and_claude_alternatives(self):
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            opus, sonnet = 'claude/claude-opus-x', 'claude/claude-sonnet-y'
            panel_data.clean()
            with patch('screen_render.budget_view', return_value=({}, 0.0)), patch('model_router.chain', side_effect=AssertionError('No measurement')), \
                 patch.dict(os.environ, {'CADRE_MONTHLY_CAP_USD': 'abc'}):
                root = panel_data.tree(home)
            budget = find(root, 'budget')
            self.assertEqual((budget['summary'], find(budget, 'go')['summary'], find(budget, 'openrouter')['summary'], find(budget, 'zen')['summary']),
                             ('No measurement', 'Monthly —', '—', '—'), 'zero is not shown when there is no measurement')
            self.assertEqual((find(budget, 'cash')['summary'], field_list(find(root, 'settings', 'cash'))['Value']), ('$0.00 / Invalid', 'Invalid'))
            self.assertEqual(find(root, 'agents', 'plan')['summary'], 'No measurement')
            view = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'today': time.strftime('%Y-%m-%d'), 'or': {'remaining': 5, 'available': 5},
                       'claude': {'pay': 2}}
            panel_data.clean()
            with patch('screen_render.budget_view', return_value=(view, 300)), patch('model_router.chain', side_effect=AssertionError('stale measurement')):
                old = find(panel_data.tree(home), 'agents', 'plan')
            self.assertEqual((old['summary'], field_list(old['child'][0])['Reason']), ('Stale measurement', 'The budget measurement is stale, the order is computed with a new measurement'),
                             'the engine rejects a measurement older than 90 s, the panel does not invent an order')
            for closed, chain, expected in ((True, [GLM], ('1. ' + opus + ' | ' + sonnet, 'Eliminated', 'Claude is off inside cadre')),
                                             (False, [sonnet, GLM], ('1. ' + sonnet, 'First choice', 'In the chain, share and context fit'))):
                panel_data.clean()
                with patch('screen_render.budget_view', return_value=(view, 30)), patch('model_router.ROLES', {'plan': [f'{opus}|{sonnet}', GLM]}), \
                     patch('model_router.chain', return_value=(chain, [])), patch('model_router.claude_closed', return_value=closed), \
                     patch('model_router.status_read', return_value={}):
                    ring = find(panel_data.tree(home), 'agents', 'plan')['child'][0]
                self.assertEqual((ring['entry_name'], ring['summary'], field_list(ring)['Reason']), expected)
                self.assertEqual(field_list(ring)['Options'], f'{opus} if the Claude share is 4 points or more, otherwise {sonnet}')

    def test_agents_section_runs_the_real_router_without_writing_or_network(self):
        import model_router
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            (home / 'chain').mkdir()

            def status() -> dict:
                return {str(p.relative_to(home)): (p.stat().st_size, p.stat().st_mtime_ns) for p in home.rglob('*')}
            once = status()
            view = ({'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'today': time.strftime('%Y-%m-%d'), 'or': {'remaining': 20, 'available': 20},
                        'go': {'own': {'monthly': 10}, 'available': 80}, 'claude': {'pay': 6}}, 30.0)
            panel_data.clean()
            panel_data.AT_END.clear()
            with patch.object(model_router, 'QE', home), patch.object(model_router, 'RECORD', home / 'chain/model_router.jsonl'), \
                 patch.object(model_router, 'STATUS', home / 'chain/provider-status.json'), patch.object(model_router, 'budget_read', side_effect=AssertionError('network')):
                section = panel_data.agent_section(home, view)
                self.assertEqual(status(), once, 'the Agents section writes neither the router record, the status file nor the budget cache')
                model_router.chain('plan', b=view[0], d={})
                self.assertTrue((home / 'chain/model_router.jsonl').is_file(), 'the write the check would catch really happens')
        self.assertEqual([c['id'] for c in section['child']][:3], ['plan', 'impl', 'quality'])


class TerminalStatusTests(unittest.TestCase):
    def execute_run(self, store, project='personal', **values):
        rid = store.accept(project, 'Status tree acceptance')['run_id']
        store.update(rid, state='running', **values)
        (store.path(rid) / 'plan.json').write_text(json.dumps({'tasks': [
            {'id': 'g1', 'definition': 'Started job'}, {'id': 'g2', 'definition': 'Unstarted job'}]}))
        for gid in ('g1', 'qa', 'quality'):
            store.task_update(rid, gid, state='running')
            attempt = store.path(rid) / 'tasks' / gid / 'attempts/a1'
            attempt.mkdir(parents=True)
            (attempt / 'attempt.json').write_text(json.dumps({'role': 'quality' if gid == 'quality' else 'impl', 'model': GLM}))
            (attempt / 'stdout.jsonl').write_text('')
        return rid

    def nodes(self, node):
        return [node] + [n for child in node['child'] for n in self.nodes(child)]

    def test_terminal_and_waiting_runs_distinguish_unfinished_unstarted_and_recorded_results(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store = RunStore(home)
            rid = self.execute_run(store)
            lang = 'en'
            for state in (*panel_data.TERMINAL, 'accepted', 'queued', 'retryable'):
                with self.subTest(lang=lang, state=state):
                    record = store.update(rid, state=state, final_result={})
                    before = {p: p.read_bytes() for p in store.path(rid).rglob('*') if p.is_file()}
                    node = panel_data.task_node(home, record)
                    self.assertFalse([n['id'] for n in self.nodes(node) if n['status'] == 'now_ts'])
                    self.assertEqual(node['now'], '')
                    label = ui_messages.t('Left unfinished') if state in panel_data.TERMINAL else ui_messages.t('Waiting')
                    self.assertEqual(field_list(find(node, 'plan', 'g1'))[ui_messages.t('Status')], label)
                    self.assertEqual(find(node, 'test')['summary'], label)
                    self.assertEqual(find(node, 'judge')['summary'], label)
                    if state in panel_data.TERMINAL:
                        self.assertEqual(find(node, 'plan', 'g2')['summary'], ui_messages.t('Not run'))
                        self.assertIn('running', str(node['detail']))
                    self.assertEqual(before, {p: p.read_bytes() for p in store.path(rid).rglob('*') if p.is_file()})
            running = store.update(rid, state='running')
            node = panel_data.task_node(home, running)
            self.assertEqual([find(node, *ids)['status'] for ids in (('plan', 'g1'), ('test',), ('judge',))], ['now_ts'] * 3)
            for gid in ('qa', 'quality'):
                store.task_update(rid, gid, state=None)
            node = panel_data.task_node(home, store.read(rid))
            self.assertEqual([find(node, gid)['status'] for gid in ('test', 'judge')], ['waiting'] * 2)
            self.assertFalse([n for n in find(node, 'agents')['child'] if n['id'] in ('agent-qa', 'agent-quality') and n['status'] == 'now_ts'],
                             'new output and task presence alone are not active execution')
            for result, marker, label in (('PASS', 'finished', 'Passed'), ('FAIL', 'error', 'Failed'),
                                           ('ERROR', 'error', 'error'), ('CANCELLED', 'error', 'Left unfinished'),
                                           ('SKIPPED', '', 'Skipped'), ('UNKNOWN', 'error', 'State unknown')):
                record = store.update(rid, state='failed', final_result={'qa': {'status': result}, 'review': {'status': result}})
                node = panel_data.task_node(home, record)
                for gid in ('test', 'judge'):
                    self.assertEqual((find(node, gid)['status'], find(node, gid)['summary']), (marker, ui_messages.t(label)))
            for gid in ('qa', 'quality'):
                store.task_update(rid, gid, state='running')

    def test_terminal_parent_never_projects_running_children_or_final_acceptance_as_active(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store = RunStore(home)
            parent = store.accept('personal', 'Partitioned product')['run_id']
            child = self.execute_run(store, 'personal--pf2', project_ref_parent=parent)
            final = self.execute_run(store, 'personal--pfinal', project_ref_parent=parent)
            project = {'status': 'running', 'features': [
                {'id': 'f2', 'title': 'Half-done part', 'status': 'waiting', 'run': [child], 'risky': True},
                {'id': 'f3', 'title': 'Unstarted part', 'status': 'waiting'}],
                'children': [{'feature': 'f2', 'run': child}, {'feature': 'final-acceptance', 'run': final}]}
            (store.path(parent) / 'project_mode.json').write_text(json.dumps(project))
            lang = 'en'
            for state in panel_data.TERMINAL:
                with self.subTest(lang=lang, state=state):
                    record = store.update(parent, state=state, project_active_feature='f2')
                    before = {p: p.read_bytes() for p in (home / 'runs').rglob('*') if p.is_file()}
                    node = panel_data.task_node(home, record)
                    self.assertFalse([n['id'] for n in self.nodes(node) if n['status'] == 'now_ts'])
                    self.assertEqual(node['now'], '')
                    self.assertEqual(find(node, 'plan', 'f2')['summary'], ui_messages.t('Left unfinished'))
                    self.assertEqual(find(node, 'plan', 'f3')['summary'], ui_messages.t('Not run'))
                    self.assertEqual(find(node, 'last-check')['summary'], ui_messages.t('Left unfinished'))
                    self.assertIn(child, str(node['detail']))
                    self.assertIn(final, str(node['detail']))
                    self.assertIn('running', str(node['detail']))
                    self.assertEqual(before, {p: p.read_bytes() for p in (home / 'runs').rglob('*') if p.is_file()})
            store.update(child, state='implemented', final_result={'status': 'APPLIED', 'qa': {'status': 'PASS'}})
            project['features'][0]['status'] = 'applied'
            (store.path(parent) / 'project_mode.json').write_text(json.dumps(project))
            for result, marker in (('FAIL', 'error'), ('PASS', 'finished')):
                store.update(final, state='completed', final_result={'status': result})
                node = panel_data.task_node(home, store.update(parent, state='completed'))
                self.assertEqual(find(node, 'plan', 'f2')['summary'], ui_messages.t('Applied'))
                self.assertEqual(find(node, 'last-check')['status'], marker)
            store.update(child, state='running', final_result={})
            store.update(final, state='running', final_result={})
            project['features'][0]['status'] = 'waiting'
            (store.path(parent) / 'project_mode.json').write_text(json.dumps(project))
            project['features'][0]['run'] = ['missing-child']
            project['children'] = [{'feature': 'final-acceptance', 'run': 'missing-final'}]
            (store.path(parent) / 'project_mode.json').write_text(json.dumps(project))
            node = panel_data.task_node(home, store.read(parent))
            self.assertEqual(find(node, 'plan', 'f2')['summary'], ui_messages.t('State unknown'))
            self.assertEqual(find(node, 'last-check')['summary'], ui_messages.t('State unknown'))
            self.assertIn('missing-child', str(node['warnings']))
            self.assertIn('missing-final', str(node['warnings']))


class TaskListTests(unittest.TestCase):
    def test_mixed_records_keep_titles_and_hide_test_runs_without_inflating_success(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store = RunStore(home)
            title = ('verify the src/iPhone_API.py change ' + 'lengthy tag ' * 15).strip()
            real = store.accept('test-dashboard', title)['run_id']
            store.update(real, state='running', created=time.time() - 120)
            passed = store.accept('personal', 'Completed product')['run_id']
            store.update(passed, state='completed', final_result={'status': 'PASS'})
            legacy = store.accept('live-c01-20261010-070000', 'Test product')['run_id']
            (store.path(legacy) / 'dispatch.log').write_text('2026-10-10 07:00:00 FINISHED test-marker\n')
            marked = store.accept('laboratory', 'Marked test')['run_id']
            store.update(marked, test_run=True, state='completed', final_result={'status': 'PASS'})
            child = store.accept('laboratory-child', 'Child job of the test')['run_id']
            store.update(child, project_ref_parent=marked, state='implemented')
            self.assertFalse(panel_data.test_project({'project': 'test-dashboard'}))
            self.assertFalse(panel_data.test_project({'project': 'personal', 'test_run': 'true'}))
            before = {p: p.read_bytes() for p in (home / 'runs').rglob('*') if p.is_file()}
            lang = 'en'
            panel_data.clean()
            hidden = panel_data.tree(home, agents=False)
            tasks = find(hidden, 'tasks')
            self.assertEqual(tasks['hidden_test'], 2)
            self.assertEqual({c['id'] for c in tasks['child']}, {real, 'history'})
            self.assertEqual(find(tasks, real)['entry_name'], title)
            self.assertIn('test-dashboard', find(tasks, real)['summary'])
            self.assertIn('2 min ago', find(tasks, real)['summary'])
            self.assertEqual([c['id'] for c in find(tasks, 'history')['child']], [passed])
            self.assertEqual(field_list(find(hidden, 'panel', 'summary'))[ui_messages.t('Passed')], '1')
            self.assertNotIn('test-marker', str(tasks['context']))
            ui_messages.tests_write(True, home)
            shown = panel_data.tree(home, agents=False)
            self.assertEqual(find(shown, 'tasks')['hidden_test'], 0)
            self.assertIn(legacy, {c['id'] for c in find(shown, 'tasks')['child']})
            self.assertIn(marked, {c['id'] for c in find(shown, 'tasks', 'history')['child']})
            self.assertEqual(field_list(find(shown, 'panel', 'summary'))[ui_messages.t('Passed')], '1')
            self.assertIn('test-marker', str(find(shown, 'tasks')['context']))
            ui_messages.tests_write(False, home)
            self.assertEqual(before, {p: p.read_bytes() for p in (home / 'runs').rglob('*') if p.is_file()})
            (home / 'panel-tests').write_bytes(b'\xff')
            self.assertFalse(ui_messages.tests_read(home))
            (home / 'panel-tests').write_text('true')
            self.assertFalse(ui_messages.tests_read(home))

    def test_relative_age_cached_history_and_event_order_preserve_time_information(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store = RunStore(home)
            records = [store.accept('personal', f'Task {i}') for i in range(7)]
            for i, k in enumerate(records):
                (store.path(k['run_id']) / 'dispatch.log').write_text(f'2026-10-10T07:00:0{i}Z plan acceptance: {i}\n')
            oldest = store.path(records[0]['run_id']) / 'dispatch.log'
            oldest.write_text(oldest.read_text() + '2026-10-10T09:00:09+02:00 QA GATE PASS\n' * 2 + 'invalid\n2026-99-99 00:00:00 FINISHED\n')
            events = panel_data.last_events(home, records)
            self.assertEqual(len(events), 6)
            self.assertIn('QA GATE PASS', events[0], "an old run's new event is not lost, the time zone is preserved in sorting")
            self.assertEqual(sum('QA GATE PASS' in e for e in events), 1)
            self.assertEqual(len(panel_data.last_events(home, records, 20)), 6)
            self.assertEqual(panel_data.last_events(home, records, 0), [])
            self.assertIn('2026-10-10T09:00:09+02:00', panel_data.events(home, records[0]['run_id'], absolute=True)[-1])
            self.assertEqual(panel_data.events(home, 'missing'), [])
            self.assertEqual(panel_data.events(home, records[0]['run_id'], 0), [])
            k = store.update(records[0]['run_id'], state='failed', created=100, updated=160)
            lang = 'en'
            for bad in (None, 0, True, 'bad', float('nan'), float('inf'), 1001):
                with patch('time.time', return_value=1000):
                    self.assertEqual(panel_data.relative_age(bad), ui_messages.t('Time unknown'))
            for bad in (None, 0, True, 'bad', float('nan'), float('inf'), 1e30):
                self.assertEqual(panel_data.absolute_moment(bad), ui_messages.t('Time unknown'))
            for delta, key, n in ((30, 'Just now', 0), (120, '{n} min ago', 2), (7200, '{n} h ago', 2), (172800, '{n} days ago', 2)):
                with patch('time.time', return_value=1000000):
                    self.assertEqual(panel_data.relative_age(1000000 - delta), ui_messages.t(key, n=n))
            with patch('time.time', return_value=200):
                first = panel_data.history_node(home, k)['summary']
            with patch('time.time', return_value=4000):
                later = panel_data.history_node(home, k)['summary']
            self.assertNotEqual(first, later, 'the cache does not freeze the relative age')
            previous = os.environ.get('TZ')
            try:
                outputs = []
                for zone in ('UTC', 'Europe/Istanbul'):
                    os.environ['TZ'] = zone
                    time.tzset()
                    outputs.append(field_list(panel_data.task_node(home, k))['Started'])
                self.assertEqual(outputs, ['1970-01-01 00:01:40 +0000', '1970-01-01 02:01:40 +0200'])
            finally:
                if previous is None:
                    os.environ.pop('TZ', None)
                else:
                    os.environ['TZ'] = previous
                time.tzset()


class ProjectRegistryTests(unittest.TestCase):
    def test_registers_this_folder_and_new_folders_safely(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            home = root / 'home_dir'
            from permission import approve, initialization_execute
            from run_store import RunStore
            with patch.dict(os.environ, GIT_AUTHOR_NAME='fixture', GIT_AUTHOR_EMAIL='fixture@local',
                            GIT_COMMITTER_NAME='fixture', GIT_COMMITTER_EMAIL='fixture@local', CADRE_RUN_ID=''):
                with self.assertRaises(project_registry.RecordPermission) as requested:
                    project_registry.save(root / 'Fresh Project Ref', create=True, home=home)
                self.assertFalse((root / 'Fresh Project Ref' / '.git').exists())
                store = RunStore(home)
                first = approve(store, 'system', requested.exception.token)
                self.assertFalse((root / 'Fresh Project Ref' / '.git').exists())
                approve(store, 'system', requested.exception.token, first['confirmation'])
                initialization_execute(store, requested.exception.token)
            self.assertEqual(project_registry.save(root / 'Fresh Project Ref', home=home), 'fresh-project-ref')
            self.assertEqual(git(root / 'Fresh Project Ref', 'log', '--oneline').count('\n'), 0)
            self.assertEqual((home / 'projects/fresh-project-ref/base-branch').read_text().strip(), 'main')
            self.assertTrue((home / 'projects/fresh-project-ref/PROJECT.md').is_file() and (home / 'projects/fresh-project-ref/POLICY.md').is_file())
            self.assertEqual(project_registry.save(root / 'Fresh Project Ref', home=home), 'fresh-project-ref', 'the same folder is not registered twice')
            (root / 'full').mkdir()
            (root / 'full/not.txt').write_text('x')
            with self.assertRaises(project_registry.RecordError):
                project_registry.save(root / 'full', home=home)
            with self.assertRaises(project_registry.RecordError):
                project_registry.save(root / 'full', create=True, home=home)
            empty_repo = root / 'empty-repo'
            empty_repo.mkdir()
            git(empty_repo, 'init', '-q')
            with self.assertRaises(project_registry.RecordError):
                project_registry.save(empty_repo, home=home)
            with self.assertRaises(project_registry.RecordError):
                project_registry.save(empty_repo / 'inner', create=True, home=home)
            self.assertFalse((empty_repo / 'inner').exists(), 'a new project_ref is not opened inside another repository')
            second = root / 'other' / 'fresh-project-ref'
            (second / 'sub').mkdir(parents=True)
            (second / 'a.txt').write_text('a')
            git(second, 'init', '-q', '-b', 'development')
            git(second, 'add', '.')
            git(second, 'commit', '-qm', 'first')
            self.assertEqual(project_registry.save(second / 'sub', home=home), 'fresh-project-ref-2')
            self.assertEqual(((home / 'projects/fresh-project-ref-2/repo-path').read_text().strip(),
                              (home / 'projects/fresh-project-ref-2/base-branch').read_text().strip()), (str(second.resolve()), 'development'))


READABILITY = r"""
import asyncio, io, json, os, sys, tempfile
from pathlib import Path
os.environ.pop('NO_COLOR', None)
os.environ['COLORTERM'] = 'truecolor'
sys.path.insert(0, sys.argv[1])
import panel_app, panel_data, ui_messages
from run_store import RunStore
from rich.console import Console
from rich.style import Style

def brightness(hex):
    rgb = [int(hex[i:i+2], 16) / 255 for i in (1, 3, 5)]
    rgb = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
    return sum(v * w for v, w in zip(rgb, (0.2126, 0.7152, 0.0722)))

home = Path(os.environ['CADRE_HOME'])
store = RunStore(home)
k = store.accept('personal-project-ref', 'keep the src/iPhone_API.py identity ' + 'lengthy tag ' * 12)
rid = k['run_id']
test_id = store.accept('live-c01-20261010-070000', 'Controlled test task')['run_id']
store.update(rid, state='running', budget_spent_usd=1.25, budget_cap_usd=3,
             tasks={'plan': {'state': 'produced'}, 'g1': {'state': 'running', 'model': 'openrouter/openai/gpt-6.1-sol'}})
(home / 'runs' / rid / 'plan.json').write_text(json.dumps({'tasks': [
    {'id': 'g1', 'definition': 'src/iPhone_API.py', 'files': ['src/iPhone_API.py'], 'model': 'openrouter/openai/gpt-6.1-sol'}]}))
before = {p: p.read_bytes() for p in (home / 'runs').rglob('*') if p.is_file()}
contrast, screens = {}, []
evidence = Path(os.environ['CADRE_UI_EVIDENCE']) if os.environ.get('CADRE_UI_EVIDENCE') else None
if evidence:
    evidence.mkdir(parents=True, exist_ok=True)

async def execute(lang, palette, size):
    ui_messages.color_write(palette)
    panel_data.clean()
    app = panel_app.Cadre(home=home)
    async with app.run_test(size=size) as pilot:
        await app.workers.wait_for_complete()
        await pilot.press('2')
        await pilot.pause()
        assert app.selected['run'] == rid
        assert 'src/iPhone_API.py' in app.selected['entry_name']
        svg = app.export_screenshot()
        if evidence:
            (evidence / f'{lang}-{palette.replace(" ", "-")}-{size[0]}x{size[1]}.svg').write_text(svg)
        assert 'src/iPhone_API.py' in svg
        if size[0] == 100:
            await pilot.press('shift+down', 'shift+down')
            await pilot.pause()
            svg = app.export_screenshot()
            if evidence:
                (evidence / f'{lang}-{palette.replace(" ", "-")}-{size[0]}x{size[1]}-scrolled.svg').write_text(svg)
        assert ('Status') in svg, (lang, palette, size)
        assert ('Running') in svg
        assert panel_app.R['money'] in svg
        for id in ('#list', '#detail', '#hint'):
            region = app.query_one(id).region
            assert region.width > 0 and region.height > 0
            assert region.right <= size[0] and region.bottom <= size[1], (palette, size, id, region)
        console = Console(width=32, file=io.StringIO())
        node = panel_data.d('src/iPhone_API.py', '$1.25 · 12 min · gpt-6.1-sol', status='now_ts')
        lines = console.render_lines(app.list_r([node], 0, 6), console.options, pad=True)
        bg = Style.parse('on ' + panel_app.R['surface']).bgcolor
        for line in lines[1:4]:
            assert sum(seg.cell_length for seg in line) == 32
            assert all(seg.style and seg.style.bgcolor == bg for seg in line if seg.cell_length), line
        for status, role in (('finished', 'good'), ('now_ts', 'accent'), ('error', 'bad'), ('vendor_state', 'muted')):
            marker = app.mark({'status': status})
            assert str(marker.style) == panel_app.R[role]
        assert ui_messages.t('vendor_state') == 'vendor_state'
        assert ui_messages.t('src/iPhone_API.py') == 'src/iPhone_API.py'
        assert ui_messages.t('Where: {h}', h='iPhone_API/Repo-X') == ('Where: ') + 'iPhone_API/Repo-X'
        assert ui_messages.t('Cancelled') == ('Cancelled')
        assert ui_messages.t('o  read').startswith('o  ')
        for text, role in (('$1.25', 'money'), ('$1,25', 'money'), ('openrouter/openai/gpt-6.1-sol', 'model'), ('12 min', 'moment'), ('12 s', 'moment'), ('09:32:01', 'moment')):
            rich = panel_app.prose(text)
            assert rich.plain == text
            assert any(span.start == 0 and span.end == len(text) and span.style == panel_app.R[role] for span in rich.spans), (text, rich.spans)
        screens.append([lang, palette, list(size)])
        await pilot.press('5')
        while app.selected.get('id') != 'color':
            await pilot.press('down')
        await pilot.press('enter')
        await app.workers.wait_for_complete()
        assert ui_messages.color_read() == ui_messages.COLORS[(ui_messages.COLORS.index(palette) + 1) % len(ui_messages.COLORS)]
        if palette == ui_messages.COLORS[0]:
            while app.selected.get('id') != 'tests':
                await pilot.press('down')
            await pilot.press('enter')
            await app.workers.wait_for_complete()
            assert ui_messages.tests_read(home)
            assert test_id in {c['id'] for c in app.section('tasks')['child']}
            await pilot.press('2')
            await pilot.pause()
            if evidence:
                (evidence / f'{lang}-{size[0]}x{size[1]}-tests.svg').write_text(app.export_screenshot())
            await pilot.press('5')
            while app.selected.get('id') != 'tests':
                await pilot.press('down')
            await pilot.press('enter')
            await app.workers.wait_for_complete()
            assert not ui_messages.tests_read(home)
            assert app.section('tasks')['hidden_test'] == 1

assert set(panel_app.PALETTES) == set(ui_messages.COLORS)
for entry_name, p in panel_app.PALETTES.items():
    contrast[entry_name] = {}
    for bg in ('back', 'surface'):
        for fg in ('prose', 'muted', 'accent', 'good', 'warning', 'bad', 'money', 'model', 'moment'):
            a, b = sorted((brightness(p[fg]), brightness(p[bg])))
            ratio = (b + 0.05) / (a + 0.05)
            contrast[entry_name][fg + '/' + bg] = round(ratio, 3)
            assert ratio >= (3 if fg == 'muted' else 4.5), (entry_name, fg, bg, ratio)
lang = 'en'
for entry_name in ui_messages.COLORS:
    for size in ((200, 50), (100, 30)):
        asyncio.run(execute(lang, entry_name, size))
        panel_app.Cadre(home=home)
        assert ui_messages.color_read() == panel_app.R['entry_name']
ui_messages.color_file().write_bytes(b'\xff')
assert ui_messages.color_read() == 'Gruvbox'
os.environ['CADRE_COLOR'] = 'unknown'
assert panel_app.palette_setup() == 'Gruvbox'
assert ui_messages.color_write('unknown') == 'Gruvbox'
ui_messages.color_write('Tokyo Night')
os.environ['CADRE_COLOR'] = 'Dracula'
assert panel_app.palette_setup() == 'Dracula'
del os.environ['CADRE_COLOR']
assert panel_app.Cadre(home=home) is not None and panel_app.R['entry_name'] == 'Tokyo Night'
assert before == {p: p.read_bytes() for p in (home / 'runs').rglob('*') if p.is_file()}
result = {'screens': screens, 'contrast': contrast, 'run_read_only': True}
if evidence:
    (evidence / 'readability.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
print(json.dumps(result, ensure_ascii=False))
"""


class PanelSummaryTests(unittest.TestCase):
    def test_compact_panel_limits_rows_preserves_critical_attention_and_marks_unknown_budget(self):
        from monthly_budget import MonthlyLedger
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CADRE_HOME': directory, 'CADRE_NO_NET': '1'}, clear=True):
            home = Path(directory)
            store = RunStore(home)
            ledger = MonthlyLedger(home / 'chain/monthly-budget.db', cap=65_000_000)
            ledger.reserve('openrouter', 2_000_000)
            language = 'en'
            for count in (0, 1, 20):
                for index in range(count):
                    rid = store.accept(f'lab{index}', 'Long title ' * 80)['run_id']
                    store.update(rid, state='running', spend_alarm={'ts': time.time(), 'reason': 'approval needed'})
                panel_data.clean()
                tree = panel_data.tree(home, agents=False)
                panel = find(tree, 'panel')
                self.assertLessEqual(len(panel['detail']), 12)
                self.assertEqual(panel['compact'], True)
                self.assertEqual([n['id'] for n in panel['child']], ['current', 'summary', 'attention', 'panel-budget'])
                text = str(panel['detail'])
                if count:
                    self.assertIn(ui_messages.t('Awaiting approval'), text)
                self.assertIn('$2', text)
                self.assertIn('No measurement', text)
            (home / 'runs/broken').mkdir()
            (home / 'runs/broken/run.json').write_text('{')
            (home / 'chain/monthly-budget.db').write_bytes(b'broken database')
            panel_data.clean()
            panel = find(panel_data.tree(home, agents=False), 'panel')
            self.assertLessEqual(len(panel['detail']), 12)
            self.assertIn(ui_messages.t('Remaining unknown'), str(panel['detail']))
            self.assertTrue(any(n['id'] == 'attention-record' for n in find(panel, 'attention')['child']))


class EconomyTests(unittest.TestCase):
    def fixture(self, home):
        from budget_gate import BudgetLedger
        store = RunStore(home)
        parent = store.accept('personal', 'Forty features')['run_id']
        child = store.accept('personal-child', 'Partial implementation')['run_id']
        orphan = store.accept('personal-child', 'Live child not yet linked')['run_id']
        unrelated = store.accept('other', 'Unrelated')['run_id']
        store.update(parent, state='failed', final_result={'status': 'FAIL'}, project_budget_cap_usd=20)
        store.update(child, state='implemented', project_ref_parent=parent)
        store.update(orphan, state='running', project_ref_parent=parent)
        project = {'status': 'stopped', 'features': [
            {'id': f'f{i}', 'status': 'applied' if i < 3 else 'waiting', 'run': [child, child] if i == 0 else []}
            for i in range(40)], 'children': [{'run': child, 'feature': 'f0'}]}
        (store.path(parent) / 'project_mode.json').write_text(json.dumps(project))
        ledger = BudgetLedger(home / 'chain/request-budget.db')
        snapshot = {'today': time.strftime('%Y-%m-%d'), 'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
                    'zen': {'db': 'ok', 'available': 30, 'pay': 30}, 'or': {'db': 'ok', 'available': 30, 'pay': 30},
                    'go': {'db': 'ok', 'available': 100, 'pay': 100, 'unit': 'points', 'window_open': True},
                    'groq': {'db': 'ok', 'today_request': 0}}

        def request(rid, attempt, actual, estimated=False, provider='openrouter'):
            ident = ledger.reserve(provider, 1 if provider == 'groq' else 1_000_000, snapshot,
                                   {'run_id': rid, 'task_id': 'g1', 'attempt_id': attempt, 'model': GLM if provider == 'opencode-go' else OR_SOL}, observation=True)
            if actual is not None:
                ledger.settle(ident, actual, ('phantom:' if estimated else 'receipt:') + ident)
            return ident

        request(parent, 'p1', 100_000)
        request(parent, 'go', 100_000, provider='opencode-go')
        request(parent, 'free', 1, provider='groq')
        request(child, 'a1', 200_000)
        request(child, 'a2', 300_000, estimated=True)
        pending = request(orphan, 'live1', None)
        request(unrelated, 'other', 900_000)
        return store, parent, child, orphan, project, ledger, pending

    def test_project_child_attempt_totals_match_real_ledger_and_do_not_double_count_references(self):
        from project_mode import project_ref_cost
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CADRE_HOME': directory, 'CADRE_NO_NET': '1'}, clear=True):
            home = Path(directory)
            store, parent, child, orphan, project, ledger, pending = self.fixture(home)
            before = {str(p): p.read_bytes() for p in home.rglob('*') if p.is_file()}
            language = 'en'
            panel_data.clean()
            node = panel_data.spend_node(home, store.read(parent), project)
            self.assertEqual(node['economy'], {'spent': 700_000, 'held': 1_000_000, 'measured': 400_000, 'estimated': 300_000,
                                               'cash': 600_000, 'equivalent': 100_000})
            self.assertEqual(set(n['id'] for n in node['child']), {parent, child, orphan})
            self.assertEqual(find(node, child)['economy']['spent'], 500_000)
            self.assertEqual(find(node, child, 'g1:a1')['economy']['spent'], 200_000)
            self.assertEqual(find(node, child, 'g1:a2')['economy']['estimated'], 300_000)
            self.assertEqual(sum(n['economy']['spent'] for n in node['child']), node['economy']['spent'])
            self.assertEqual(sum(n['economy']['held'] for n in node['child']), node['economy']['held'])
            expected = project_ref_cost(home, [parent, child])
            self.assertAlmostEqual(expected['measured_usd'] + expected['estimated_usd'], node['economy']['spent'] / 1e6, places=6)
            self.assertEqual(expected['pending_usd'], node['economy']['held'] / 1e6)
            self.assertIn(ui_messages.t('Run snapshot differs from the ledger. This view uses the ledger.'), str(node['detail']))
            task = panel_data.task_node(home, store.read(parent))
            self.assertEqual(field_list(task)[ui_messages.t('Intermediate implementation')], '3/40')
            self.assertFalse(any(kind == 'bar' and value[0] == ui_messages.t('Progress') for kind, value in task['detail']))
            self.assertIn(ui_messages.t('The stage indicator is not a product completion percentage.'), str(task['detail']))
            self.assertEqual(before, {str(p): p.read_bytes() for p in home.rglob('*') if p.is_file()})

    def test_history_refreshes_after_child_and_receipt_changes_without_parent_update(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CADRE_HOME': directory, 'CADRE_NO_NET': '1'}, clear=True):
            home = Path(directory)
            store, parent, child, orphan, project, ledger, pending = self.fixture(home)
            record = store.read(parent)
            observer = sqlite3.connect(ledger.path)
            try:
                observer.execute('pragma journal_mode=WAL')
                observer.execute('begin')
                observer.execute('select actual from reservation where id=?', (pending,)).fetchone()
                first = panel_data.history_node(home, record)
                ledger.settle(pending, 400_000, 'real:' + pending)
                self.assertIsNone(observer.execute('select actual from reservation where id=?', (pending,)).fetchone()[0])
                second = panel_data.history_node(home, record)
                self.assertTrue(Path(str(ledger.path) + '-wal').exists())
            finally:
                observer.close()
            self.assertEqual(find(first, 'spend')['economy']['spent'], 700_000)
            self.assertEqual(find(second, 'spend')['economy']['spent'], 1_100_000)
            self.assertEqual(find(second, 'spend')['economy']['held'], 0)
            store.update(child, state='failed')
            third = panel_data.history_node(home, record)
            self.assertNotEqual(str(find(second, 'plan', 'f0')), str(find(third, 'plan', 'f0')))
            self.assertEqual(store.read(parent), record)

    def test_missing_corrupt_and_invalid_costs_are_unknown_and_valid_siblings_remain_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store = RunStore(home)
            parent = store.accept('personal', 'Unknown cost')['run_id']
            child = store.accept('personal-child', 'Known cost')['run_id']
            store.update(child, project_ref_parent=parent, budget_spent_usd=0.25)
            record = store.read(parent)
            for invalid in (None, 'bad', -1, True, float('nan'), float('inf')):
                store.update(parent, budget_spent_usd=invalid)
                node = panel_data.spend_node(home, store.read(parent), {})
                self.assertIsNone(node['economy']['spent'])
                self.assertEqual(find(node, child)['economy']['spent'], 250_000)
                self.assertIn(ui_messages.t('Unknown'), node['summary'])
            store.update(parent, budget_spent_usd=0)
            node = panel_data.spend_node(home, store.read(parent), {})
            self.assertEqual(node['economy']['spent'], 250_000)
            self.assertIsNone(node['economy']['measured'])
            self.assertIsNone(node['economy']['estimated'])
            linked_missing = {'children': [{'run': 'missing'}]}
            self.assertIsNone(panel_data.spend_node(home, store.read(parent), linked_missing)['economy']['spent'])
            db = home / 'chain/request-budget.db'
            db.parent.mkdir(exist_ok=True)
            db.write_bytes(b'corrupt database')
            self.assertIsNone(panel_data.spend_node(home, record, {})['economy']['spent'])

    def test_summary_keeps_partial_open_failed_and_retry_costs_without_inflating_product_success(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CADRE_HOME': directory, 'CADRE_NO_NET': '1'}, clear=True):
            home = Path(directory)
            store = RunStore(home)
            for i, state in enumerate(('completed', 'failed', 'implemented', 'running')):
                rid = store.accept('personal', 'Same title')['run_id']
                store.update(rid, state=state, final_result={'status': 'PASS' if state == 'completed' else 'APPLIED'}, budget_spent_usd=0.1, budget_held_usd=0.2)
                for attempt in ('a1', 'a2'):
                    path = store.task_dir(rid, 'g1') / 'attempts' / attempt
                    path.mkdir(parents=True)
                    (path / 'attempt.json').write_text(json.dumps({'state': 'done'}))
            test = store.accept('lab', 'Test')['run_id']
            store.update(test, test_run=True, state='completed', final_result={'status': 'PASS'}, budget_spent_usd=10)
            child = store.accept('personal-child', 'Child')['run_id']
            store.update(child, project_ref_parent=rid, state='implemented', budget_spent_usd=0.3, budget_held_usd=0.4)
            for show_tests in (False, True):
                ui_messages.tests_write(show_tests, home)
                panel_data.clean()
                summary = find(panel_data.tree(home, agents=False), 'panel', 'summary')
                fields = field_list(summary)
                self.assertEqual(fields['Finished'], '3')
                self.assertEqual(fields['Passed'], '1')
                self.assertEqual(fields['Failed'], '1')
                self.assertEqual(fields['Open'], '1')
                self.assertEqual(fields['Success'], '1/3 · 33%')
                self.assertEqual(fields['Spend'], '$0.70')
                self.assertEqual(fields['Held'], '$1.20')


class SettingEditingTests(unittest.TestCase):
    def test_two_process_edit_race_has_one_winner_and_lowering_preserves_monthly_holds(self):
        import run_store
        from monthly_budget import MonthlyLedger, usd_micro
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CADRE_HOME': directory, 'CADRE_NO_NET': '1'}, clear=True):
            home = Path(directory)
            state = run_store.budget_option(home, 'monthly')
            worker = '''
import json,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import run_store
request=json.load(sys.stdin)
home=Path(request['home'])
(home / ('ready-'+request['value'])).touch()
limit=time.monotonic()+10
while not (home/'go').exists():
    if time.monotonic()>limit: raise RuntimeError('barrier timed out')
    time.sleep(.01)
try:
    run_store.budget_setting_write(home,'monthly','',request['value'],request['state'])
    print('saved')
except ValueError:
    print('conflict')
'''
            children = []
            try:
                for value in ('10', '20'):
                    child = subprocess.Popen([sys.executable, '-c', worker, str(LIB)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE, text=True, env=dict(os.environ))
                    children.append(child)
                    child.stdin.write(json.dumps({'home': directory, 'value': value, 'state': state}))
                    child.stdin.close()
                limit = time.monotonic() + 10
                while len(list(home.glob('ready-*'))) < 2 and time.monotonic() < limit:
                    time.sleep(.01)
                self.assertEqual(len(list(home.glob('ready-*'))), 2)
                (home / 'go').touch()
                results = []
                for child in children:
                    child.wait(timeout=15)
                    self.assertEqual(child.returncode, 0, child.stderr.read())
                    results.append(child.stdout.read().strip())
                self.assertEqual(sorted(results), ['conflict', 'saved'])
            finally:
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                        child.wait(timeout=5)
                    child.stdout.close()
                    child.stderr.close()
            self.assertEqual(usd_micro('0.000001'), 1)
            self.assertEqual(usd_micro('9007199254.740991'), 2**53 - 1)
            ledger = MonthlyLedger(home / 'chain/monthly-budget.db')
            ledger.reserve('openrouter', 2_000_000)
            state = run_store.budget_option(home, 'monthly')
            run_store.budget_setting_write(home, 'monthly', '', '1', state)
            command = [sys.executable, str(LIB / 'monthly_budget.py')]
            status = subprocess.run(command, input='{"action":"status"}', capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(status.stdout)['held_micro'], 2_000_000)
            self.assertEqual(json.loads(status.stdout)['remaining_micro'], 0)
            denied = subprocess.run(command, input='{"action":"reserve","provider":"opencode","maximum":1}', capture_output=True, text=True)
            self.assertEqual(denied.returncode, 75)
            self.assertIn('monthly paid budget exhausted', denied.stderr)
            self.assertEqual(MonthlyLedger(home / 'chain/monthly-budget.db').status()['held_micro'], 2_000_000)

    def test_budget_edits_persist_and_real_engine_and_monthly_cli_read_the_same_caps(self):
        import run_store
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CADRE_HOME': directory, 'CADRE_NO_NET': '1'}, clear=True):
            home = Path(directory)
            (home / 'projects/lab').mkdir(parents=True)
            store = RunStore(home)
            old = store.accept('lab', 'existing')['run_id']
            for kind, project, value in [('run', 'lab', '0.125'), ('monthly', '', '12.345678')]:
                before = run_store.budget_option(home, kind, project)
                run_store.budget_setting_write(home, kind, project, value, before)
                after = run_store.budget_option(home, kind, project)
                self.assertEqual(after['value'], float(value))
                self.assertEqual(after['source'], 'file')
                self.assertEqual(run_store.budget_option(home, kind, project), after)
            self.assertEqual(store.read(old)['budget_cap_usd'], 2)
            self.assertEqual(store.accept('lab', 'new')['budget_cap_usd'], 0.125)
            result = subprocess.run([sys.executable, str(LIB / 'monthly_budget.py')], input='{"action":"status"}',
                                    capture_output=True, text=True, env=dict(os.environ), check=True)
            self.assertEqual(json.loads(result.stdout)['cap_micro'], 12_345_678)
            self.assertEqual(panel_data.cash_status(home)['limit'], 12.345678)
            with patch.dict(os.environ, {'CADRE_MONTHLY_CAP_USD': '4', 'CADRE_RUN_CAP_USD': '0'}):
                self.assertEqual(cap_micro(home), 4_000_000)
                self.assertEqual(store.accept('lab', 'zero')['budget_cap_usd'], 0)
                for kind, project in [('run', 'lab'), ('monthly', '')]:
                    state = run_store.budget_option(home, kind, project)
                    self.assertEqual(state['source'], 'environment')
                    with self.assertRaises(ValueError):
                        run_store.budget_setting_write(home, kind, project, '5', state)

    def test_invalid_caps_stale_edits_and_write_failure_never_replace_the_previous_value(self):
        import run_store
        from monthly_budget import MonthlyBudgetDenied
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CADRE_HOME': directory}, clear=True):
            home = Path(directory)
            (home / 'projects/lab').mkdir(parents=True)
            for kind, project in [('run', 'lab'), ('monthly', '')]:
                state = run_store.budget_option(home, kind, project)
                run_store.budget_setting_write(home, kind, project, '1', state)
                state = run_store.budget_option(home, kind, project)
                path = Path(state['path'])
                original = path.read_bytes()
                for invalid in ('', 'NaN', 'sNaN', 'inf', '-1', '1e100000', '9007199254.740992', True):
                    with self.subTest(kind=kind, value=invalid), self.assertRaises(ValueError):
                        run_store.budget_setting_write(home, kind, project, invalid, state)
                    self.assertEqual(path.read_bytes(), original)
                with patch.object(run_store.os, 'replace', side_effect=OSError('disk unavailable')), self.assertRaises(OSError):
                    run_store.budget_setting_write(home, kind, project, '2', state)
                self.assertEqual(path.read_bytes(), original)
                run_store.budget_setting_write(home, kind, project, '2', state)
                with self.assertRaises(ValueError):
                    run_store.budget_setting_write(home, kind, project, '3', state)
                self.assertEqual(path.read_text().strip(), '2')
            store = RunStore(home)
            previous = sorted(str(p.relative_to(home)) for p in home.rglob('*'))
            with patch.dict(os.environ, {'CADRE_RUN_CAP_USD': 'nan'}), self.assertRaises(ValueError):
                store.accept('lab', 'must not start')
            self.assertEqual(sorted(str(p.relative_to(home)) for p in home.rglob('*')), previous)
            with patch.dict(os.environ, {'CADRE_MONTHLY_CAP_USD': 'nan'}), self.assertRaises(MonthlyBudgetDenied):
                cap_micro(home)
            monthly = home / 'chain/monthly-cap-usd'
            monthly.write_text('invalid')
            with self.assertRaises(MonthlyBudgetDenied):
                cap_micro(home)
            monthly.write_text('0')
            with self.assertRaises(MonthlyBudgetDenied):
                cap_micro(home)

    def test_symlinks_unknown_projects_and_environment_changes_are_rejected(self):
        import run_store
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'CADRE_HOME': directory}, clear=True):
            home = Path(directory)
            (home / 'projects/lab').mkdir(parents=True)
            state = run_store.budget_option(home, 'run', 'lab')
            with patch.dict(os.environ, {'CADRE_RUN_CAP_USD': '7'}), self.assertRaises(ValueError):
                run_store.budget_setting_write(home, 'run', 'lab', '8', state)
            target = home / 'protected'
            target.write_text('private fixture')
            path = home / 'projects/lab/run-cap-usd'
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                run_store.budget_option(home, 'run', 'lab')
            self.assertEqual(target.read_text(), 'private fixture')
            for project in ('../outside', 'unknown'):
                with self.assertRaises(ValueError):
                    run_store.budget_setting_write(home, 'run', project, '1', {})


class RolePresentationTests(unittest.TestCase):
    def test_four_visible_roles_preserve_real_engine_role_ids_and_unknown_roles(self):
        import model_router
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store = RunStore(home)
            rid = store.accept('personal', 'Role mapping')['run_id']
            roles = ('impl', 'impl-light', 'merge', 'recovery', 'future-role')
            for i, role in enumerate(roles):
                attempt = store.path(rid) / f'tasks/g{i}/attempts/a1'
                attempt.mkdir(parents=True)
                (attempt / 'attempt.json').write_text(json.dumps({'role': role}))
            before = {p: p.read_bytes() for p in home.rglob('*') if p.is_file()}
            chains = {role: list(models) for role, models in model_router.ROLES.items()}
            language = 'en'
            menu = panel_data.agent_section(home, ({}, 0))
            self.assertEqual([n['id'] for n in menu['child']], ['plan', 'impl', 'quality', 'on-evaluation'])
            self.assertEqual([n['entry_name'] for n in menu['child']], [ui_messages.t(n) for n in ('Planner', 'Developer', 'Judge', 'Pre-screener')])
            self.assertEqual(find(menu, 'impl', 'impl-light')['entry_name'], ui_messages.t('Lightweight development'))
            nodes = panel_data.agent_nodes(home, store.read(rid))
            self.assertEqual([field_list(n)[ui_messages.t('Engine role')] for n in nodes], list(roles))
            self.assertEqual([field_list(n)[ui_messages.t('Role')] for n in nodes], [ui_messages.t('Developer')] * 4 + ['future-role'])
            self.assertEqual(chains, model_router.ROLES)
            self.assertEqual(before, {p: p.read_bytes() for p in home.rglob('*') if p.is_file()})


class RecordViewTests(unittest.TestCase):
    def test_combined_trace_keeps_child_attempt_request_revision_duplicates_and_unknown_times_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory); store = RunStore(home)
            parent = store.accept('lab', 'combined trace')['run_id']
            child = store.accept('lab-child', 'child')['run_id']
            store.update(child, project_ref_parent=parent)
            root = store.path(parent)
            (root / 'project_mode.json').write_text(json.dumps({'children': [{'run': child}, {'run': child}, {'run': 'missing-child'}]}))
            (root / 'coordinator.log').write_text('2026-10-10T10:02:00Z coordinator waiting\nunknown parent line\n')
            (root / 'dispatch.log').write_text('2026-10-10T10:01:00+00:00 dispatch plan\n')
            attempt = store.path(child) / 'tasks/g1/attempts/a1'; attempt.mkdir(parents=True)
            (attempt / 'attempt.json').write_text(json.dumps({'attempt_id': 'a1', 'role': 'impl', 'model': 'test-model'}))
            event = {'timestamp': '2026-10-10T10:00:00Z', 'type': 'tool_use', 'request_id': 'request-1', 'revision': 'rev-2',
                     'part': {'tool': 'bash', 'state': {'input': {'command': 'pytest -q'}, 'output': 'PASS'}}}
            future = {'timestamp': '2026-10-10 10:04:00', 'type': 'future-kind', 'data': {'value': 'retain'}}
            (attempt / 'stdout.jsonl').write_text('\n'.join(json.dumps(e) for e in (event, event, future)) + '\n{"type":')
            before = {p: p.read_bytes() for p in home.rglob('*') if p.is_file()}
            with patch('subprocess.run', side_effect=AssertionError('trace must not launch a process')):
                trace = panel_data.event_trace(home, parent)
                self.assertEqual(trace, panel_data.event_trace(home, parent))
                timeline = panel_data.trace_text(trace)
                spec = panel_data.events_node(home, parent)['file']
                self.assertEqual(panel_data.file_read(spec)[2], timeline)
                self.assertEqual(json.loads(panel_data.file_read(spec, raw=True)[2]), trace)
            tool = next(e for e in trace['events'] if e['type'] == 'tool_use')
            self.assertEqual(tool['lines'], [1, 2])
            self.assertEqual((tool['run_id'], tool['parent_run_id'], tool['child_id'], tool['task_id'], tool['attempt_id'], tool['request_id'], tool['revision']),
                             (child, parent, child, 'g1', 'a1', 'request-1', 'rev-2'))
            self.assertEqual(sum(e['type'] == 'tool_use' for e in trace['events']), 1)
            known = [e['timestamp'] for e in trace['events'] if e['timestamp'] is not None]
            self.assertEqual(known, sorted(known))
            self.assertIsNone(next(e for e in trace['events'] if e['type'] == 'future-kind')['timestamp'])
            self.assertIn('unknown parent line', timeline)
            self.assertIn('future-kind', timeline)
            self.assertIn('{\\"type\\":', json.dumps(trace))
            self.assertTrue(any(i['issue'] == 'missing' and 'missing-child' in i['source'] for i in trace['issues']))
            self.assertEqual(before, {p: p.read_bytes() for p in home.rglob('*') if p.is_file()})

    def test_trace_cli_json_markdown_and_replay_preserve_redaction_without_accessing_replay_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory); store = RunStore(home)
            rid = store.accept('lab', 'trace export')['run_id']; root = store.path(rid)
            secret = 'SYNTHETIC_PRIVATE_VALUE'
            (root / 'dispatch.log').write_text('Authorization: Bearer ' + secret + '\n-----BEGIN PRIVATE KEY-----\n' + secret +
                                               '\n-----END PRIVATE KEY-----\nafter block\n')
            attempt = root / 'tasks/g1/attempts/a1'; attempt.mkdir(parents=True)
            (attempt / 'stdout.jsonl').write_text(json.dumps({'type': 'future', 'payload': {'password': secret}, 'text': '```` keep text'}) + '\n')
            env = {**os.environ, 'CADRE_HOME': directory, 'CADRE_NO_NET': '1'}
            command = ['bash', str(LIB.parent / 'bin/cadre.sh'), 'trace']
            before = {p: p.read_bytes() for p in home.rglob('*') if p.is_file()}
            result = subprocess.run([*command, rid], text=True, capture_output=True, env=env, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(secret, result.stdout)
            trace = json.loads(result.stdout)
            self.assertTrue(any(e['lines'] == [5] and e['data'] == 'after block' for e in trace['events']))
            self.assertIn('[REDACTED]', result.stdout)
            for form in ('json', 'markdown'):
                replay = subprocess.run([*command, '--replay', '--format', form], input=result.stdout, text=True,
                                        capture_output=True, env={**env, 'CADRE_HOME': str(home / 'absent-home')}, timeout=10)
                self.assertEqual(replay.returncode, 0, replay.stderr)
                self.assertNotIn(secret, replay.stdout)
                if form == 'json':
                    self.assertEqual(json.loads(replay.stdout), trace)
                else:
                    self.assertIn('`````text', replay.stdout)
            injected = json.loads(result.stdout)
            injected['events'][0]['data'] = {'password': secret}
            with patch('panel_data._trace_source', side_effect=AssertionError('replay must not read sources')):
                self.assertNotIn(secret, panel_data.trace_text(panel_data.trace_replay(json.dumps(injected))))
            invalid = json.loads(result.stdout); invalid['events'][0]['timestamp'] = float('nan')
            with self.assertRaises(ValueError):
                panel_data.trace_replay(json.dumps(invalid))
            invalid['events'][0]['timestamp'] = 10**400
            with self.assertRaises(ValueError):
                panel_data.trace_replay(json.dumps(invalid))
            invalid = json.loads(result.stdout); invalid['sources'][0]['mtime'] = 10**400
            with self.assertRaises(ValueError):
                panel_data.trace_replay(json.dumps(invalid))
            with self.assertRaises(ValueError):
                panel_data.trace_replay('{"schema":"unknown"}')
            self.assertFalse((home / 'absent-home').exists())
            self.assertEqual(before, {p: p.read_bytes() for p in home.rglob('*') if p.is_file()})

    def test_trace_unreadable_large_linked_and_changing_sources_stay_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory); store = RunStore(home)
            rid = store.accept('lab', 'negative trace')['run_id']; root = store.path(rid)
            outside = home / 'synthetic-private'; outside.write_text('SYNTHETIC_DO_NOT_READ')
            (root / 'dispatch.log').symlink_to(outside)
            (root / 'coordinator.log').write_bytes(b'x' * 2_000_001)
            (root / 'plan.json').write_text('{')
            trace = panel_data.event_trace(home, rid)
            self.assertNotIn('SYNTHETIC_DO_NOT_READ', json.dumps(trace))
            self.assertEqual(sum(i['issue'] == 'unreadable-or-limit' for i in trace['issues']), 2)
            self.assertTrue(any(i['issue'] == 'malformed-json' for i in trace['issues']))
            (root / 'dispatch.log').unlink(); os.mkfifo(root / 'dispatch.log')
            self.assertTrue(any('dispatch.log' in i['source'] for i in panel_data.event_trace(home, rid)['issues']))
            original = panel_data._trace_source
            def changed(home, relative):
                raw, modified, _ = original(home, relative)
                return raw, modified, True
            with patch('panel_data._trace_source', side_effect=changed):
                changed_trace = panel_data.event_trace(home, rid)
            self.assertTrue(any(i['issue'] == 'changed-during-read' for i in changed_trace['issues']))
            with self.assertRaises(ValueError):
                panel_data.event_trace(home, '../escape')

    def test_trace_event_limit_is_explicit_and_intermediate_symlink_swap_does_not_read_external_data(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory); store = RunStore(home)
            rid = store.accept('lab', 'bounded trace')['run_id']; root = store.path(rid)
            attempt = root / 'tasks/g1/attempts/a1'; attempt.mkdir(parents=True)
            log = attempt / 'stdout.jsonl'
            log.write_text('\n'.join(json.dumps({'type': 'future', 'index': n}) for n in range(5001)))
            trace = panel_data.event_trace(home, rid)
            self.assertEqual(len(trace['events']), 5000)
            self.assertTrue(any(i['issue'] == 'event-limit' for i in trace['issues']))
            outside = home / 'outside-trace'; (outside / 'g1/attempts/a1').mkdir(parents=True)
            (outside / 'g1/attempts/a1/stdout.jsonl').write_text('SYNTHETIC_DO_NOT_READ')
            log.write_text(json.dumps({'type': 'safe', 'timestamp': 10**400}) + '\n')
            self.assertIsNone(next(e for e in panel_data.event_trace(home, rid)['events'] if e['type'] == 'safe')['timestamp'])
            original_open = os.open
            swapped = False
            def swap(path, flags, *args, **kwargs):
                nonlocal swapped
                if path == 'tasks' and not swapped:
                    swapped = True
                    (root / 'tasks').rename(root / 'parked-tasks')
                    (root / 'tasks').symlink_to(outside)
                return original_open(path, flags, *args, **kwargs)
            with patch('panel_data.os.open', side_effect=swap):
                raw_trace = panel_data.event_trace(home, rid)
            self.assertTrue(swapped)
            self.assertNotIn('SYNTHETIC_DO_NOT_READ', json.dumps(raw_trace))
            self.assertTrue(any(i['issue'] == 'unreadable-or-limit' for i in raw_trace['issues']))

    def test_tail_windows_never_present_a_partial_secret_or_false_line_number(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'stdout.jsonl'
            path.write_text('Authorization: Bearer ' + 'synthetic' * 50000 + '\n{"type":"future"}\n')
            events = panel_data.record_events(path)
            self.assertEqual(len(events), 1)
            self.assertIn('stdout.jsonl@>', events[0][2])
            self.assertNotIn('synthetic', str(events))
            self.assertEqual(panel_data.last_lines(path, 5), ['{"type": "future"}'])

    def test_live_reader_keeps_budget_permission_and_terminal_states_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store = RunStore(home)
            rid = store.accept('personal', 'Monitoring limits')['run_id']
            for state in ('running', 'retryable', 'cancelled', 'failed', 'completed'):
                store.update(rid, state=state, reason='budget-wait', budget_cap_usd=0.01, budget_spent_usd=0.01,
                             spend_alarm={'ts': time.time(), 'reason': 'approval waiting'})
                before = {p: p.read_bytes() for p in store.path(rid).rglob('*') if p.is_file()}
                with patch('subprocess.run', side_effect=AssertionError('the monitor cannot start processes or network')):
                    node = panel_data.monitoring_data(home, rid)
                self.assertEqual(node['id'], rid)
                self.assertIn('budget-wait', str(node['detail']))
                self.assertEqual(store.read(rid)['budget_cap_usd'], 0.01)
                self.assertEqual(before, {p: p.read_bytes() for p in store.path(rid).rglob('*') if p.is_file()})
                self.assertIsNone(node['last_action_time'])
            path = store.path(rid) / 'run.json'
            path.write_text('{')
            with self.assertRaises(ValueError):
                panel_data.monitoring_data(home, rid)

    def test_common_events_raw_detail_redaction_and_read_only_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            attempt = Path(directory)
            token = 'sk-' + 'synthetic' * 4
            events = [
                {'type': 'reasoning', 'part': {'text': 'reading the plan'}},
                {'type': 'text', 'part': {'text': '{"outcome": true}'}},
                {'type': 'tool_use', 'part': {'tool': 'bash', 'state': {'input': {'command': 'pytest -q'}, 'output': 'long-' * 100000 + token}}},
                {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'pytest -q'}}]}},
                {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'tool completed'}]}},
                {'type': 'error', 'error': {'message': 'permission denial', 'password': 'synthetic-password'}},
                {'type': 'future-event', 'payload': {'Authorization': 'Bearer synthetic-authority', 'env': {'SPECIAL': 'synthetic-environment'}}},
                {'type': 'assistant', 'message': {'content': 'unexpected content'}},
            ]
            source = attempt / 'stdout.jsonl'
            source.write_text('\n'.join(json.dumps(e, ensure_ascii=False) for e in events) + '\nbroken password=synthetic-broken\n{"type":')
            before = source.read_bytes()
            language = 'en'
            spec = {'round': 'brain', 'path': str(attempt), 'title': 'Record'}
            _, _, summary = panel_data.file_read(spec)
            _, _, raw = panel_data.file_read(spec, raw=True)
            self.assertIn('pytest -q', summary)
            self.assertEqual(summary.count('pytest -q'), 2)
            self.assertIn('tool completed', summary)
            self.assertIn('future-event', summary)
            self.assertIn('stdout.jsonl:10', summary)
            self.assertIn('\\"outcome\\": true', raw)
            self.assertIn('long-' * 100000, raw)
            self.assertIn('{"type":', raw)
            for secret in (token, 'synthetic-password', 'synthetic-authority', 'synthetic-environment', 'synthetic-broken'):
                self.assertNotIn(secret, summary + raw)
            self.assertIn('[REDACTED]', raw)
            self.assertIn('JSON', summary)
            self.assertEqual(before, source.read_bytes())
            self.assertEqual(raw, panel_data.file_read(spec, raw=True)[2])

    def test_log_sources_include_all_attempts_and_preserve_unknown_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            store = RunStore(home)
            rid = store.accept('lab', 'record view')['run_id']
            run = store.path(rid)
            (run / 'dispatch.log').write_text('2026-10-10 10:00:00 plan acceptance\nunknown line\n')
            (run / 'coordinator.log').write_text('coordinator\n')
            for name in ('a1', 'a2'):
                attempt = run / 'tasks/g1/attempts' / name
                attempt.mkdir(parents=True)
                (attempt / 'stdout.jsonl').write_text('{"type":"fresh"}\n')
                (attempt / 'stderr.log').write_text('error record\n')
            outside = home / 'outside-record'
            outside.write_text('must-not-be-read')
            (run / 'tasks/g1/attempts/a2/linked.log').symlink_to(outside)
            (run / 'tasks/g1/attempts/a3').symlink_to(home, target_is_directory=True)
            before = {str(p): p.read_bytes() for p in run.rglob('*') if p.is_file()}
            with patch('subprocess.run', side_effect=AssertionError('process/net call')):
                sources = panel_data.log_sources(home, rid)
                node = panel_data.events_node(home, rid)
                self.assertEqual(len(sources), 6)
                self.assertEqual(len(node['child']), 6)
                self.assertEqual({s['path'] for s in sources}, {c['file']['path'] for c in node['child']})
                self.assertIn('unknown line', panel_data.file_read(node['file'])[2])
                for source in sources:
                    self.assertIn(Path(source['path']).name, source['title'])
                    panel_data.file_read(source)
                    panel_data.file_read(source, raw=True)
            self.assertEqual(before, {str(p): p.read_bytes() for p in run.rglob('*') if p.is_file()})
            self.assertNotIn('must-not-be-read', panel_data.file_read({'round': 'log', 'path': str(run / 'tasks/g1/attempts/a2/linked.log')})[2])

    def test_plain_json_and_incomplete_logs_mask_secrets_without_losing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'run.json'
            data = {'nested': {'api_key': 'synthetic-key', 'DATABASE_URL': 'postgres://synthetic:password@localhost/db'}, 'task': 'g1'}
            data['tokens'] = {'input_tokens': 123, 'output_tokens': 45}
            data['text'] = json.dumps({'env': {'SPECIAL': 'synthetic-environment'}})
            path.write_text(json.dumps(data))
            _, kind, text = panel_data.file_read({'round': 'json', 'path': str(path)})
            self.assertEqual(kind, 'json')
            self.assertIn('\n  "nested": {\n', text)
            self.assertEqual(json.loads(text)['task'], 'g1')
            self.assertEqual(json.loads(text)['tokens'], data['tokens'])
            self.assertNotIn('synthetic', text)
            path.write_text('Authorization: Bearer synthetic-authority\nTOKEN=synthetic-token\n-----BEGIN PRIVATE KEY-----\nsynthetic-special\n-----END PRIVATE KEY-----\nlast line')
            text = panel_data.file_read({'round': 'log', 'path': str(path)}, raw=True)[2]
            self.assertNotIn('synthetic', text)
            self.assertIn('last line', text)
            path.unlink()
            self.assertIn(ui_messages.t('Could not read file: {y}', y=path), panel_data.file_read({'round': 'log', 'path': str(path)})[2])


class PanelTests(unittest.TestCase):
    def test_task_defaults_editor_form_override_and_stale_submission_and_sizes(self):
        script = r'''
import asyncio,json,os,sys,tempfile
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import panel_app,panel_data,ui_messages
from run_store import RunStore,task_fallbacks,task_fallbacks_write
from textual.widgets import Select,TextArea,Static
screens=[]
def capture(app,name):
    screens.append(name)
    if os.environ.get('CADRE_REQUEST_EVIDENCE'):
        folder=Path(os.environ['CADRE_REQUEST_EVIDENCE']); folder.mkdir(parents=True,exist_ok=True)
        (folder/(name+'.svg')).write_text(app.export_screenshot())
async def wait(app,pilot):
    await app.workers.wait_for_complete(); await pilot.pause(.1)
async def run(lang,size):
  with tempfile.TemporaryDirectory(prefix='cadre-defaults-ui-') as directory:
    home=Path(directory); repo=home/'repo'; repo.mkdir()
    (home/'projects/lab').mkdir(parents=True)
    (home/'projects/lab/repo-path').write_text(str(repo))
    os.environ.update(CADRE_HOME=str(home),CADRE_NO_NET='1')
    panel_data.clean()
    store=RunStore(home); app=panel_app.Cadre(home=home)
    prefix=f'defaults-{lang}-{size[0]}'
    async with app.run_test(size=size) as pilot:
      await wait(app,pilot)
      app.git(['settings']); app.choice=next(i for i,n in enumerate(app.elements) if n['id']=='preference'); app.draw()
      await pilot.press('enter'); assert isinstance(app.screen,panel_app.TaskFallbacks)
      capture(app,prefix+'-editor')
      assert app.screen.query_one('#gv-box').region.bottom <= size[1]
      app.screen.query_one('#gv-preference',Select).value='economic'
      await pilot.press('escape')
      assert not (home/'chain/task-fallbacks.json').exists() and not store.list()
      await pilot.press('enter')
      app.screen.query_one('#gv-preference',Select).value='strong'
      app.screen.query_one('#gv-difficulty',Select).value='hard'
      await pilot.press('ctrl+s'); await wait(app,pilot)
      assert task_fallbacks(home)['preference']=='strong' and not store.list()
      await pilot.press('enter'); editor=app.screen
      editor.query_one('#gv-preference',Select).value='economic'
      task_fallbacks_write(home,'balanced',None,task_fallbacks(home))
      await pilot.press('ctrl+s')
      assert app.screen is editor and task_fallbacks(home)['preference']=='balanced'
      capture(app,prefix+'-conflict')
      await pilot.press('escape')
      task_fallbacks_write(home,'strong','hard',task_fallbacks(home))
      app.push_screen(panel_app.FreshTask([{'round':'project_ref','entry_name':'lab','label':'lab'}])); await pilot.pause()
      await pilot.press('ctrl+s'); form=app.screen
      assert form.query_one('#nt-preference',Select).value=='strong'
      assert form.query_one('#nt-difficulty',Select).value=='hard'
      capture(app,prefix+'-inherited')
      form.query_one('#nt-text',TextArea).text='Keep limits and defaults traceable'
      form.query_one('#nt-preference',Select).value='economic'
      form.query_one('#nt-difficulty',Select).value='medium'
      await pilot.press('ctrl+s'); assert form.step==3 and not store.list()
      assert ('field', ('Difficulty override', 'Medium')) in form.summary_fields(form.input())
      capture(app,prefix+'-summary')
      task_fallbacks_write(home,'balanced',None,task_fallbacks(home))
      await pilot.press('enter'); await wait(app,pilot)
      assert app.screen is form and not form.sending and not store.list()
      capture(app,prefix+'-stale')
      await pilot.press('escape')
      app.push_screen(panel_app.FreshTask([{'round':'project_ref','entry_name':'lab','label':'lab'}])); await pilot.pause()
      await pilot.press('ctrl+s'); form=app.screen
      assert form.query_one('#nt-preference',Select).value=='balanced'
      assert form.query_one('#nt-difficulty',Select).value=='auto'
      form.query_one('#nt-text',TextArea).text='Explicit override after reopening'
      form.query_one('#nt-preference',Select).value='economic'
      form.query_one('#nt-difficulty',Select).value='easy'
      await pilot.press('ctrl+s','enter'); await wait(app,pilot)
      saved=store.list()[0]; before=(store.path(saved['run_id'])/'run.json').read_bytes()
      assert saved['request']['preference']=='economic' and saved['request']['difficulty_override']=='easy'
      assert saved['request']['selection_defaults']['preference']=='balanced'
      assert saved['model_selection']['preference']=='economic' and saved['model_selection']['difficulty_override']=='easy'
      assert saved['model_selection']['difficulty_effective']=='easy'
      assert saved['budget_cap_usd']==2 and saved['budget_spent_usd']==0 and saved['tasks']=={}
      assert saved['reason']=='request-plan-gate' and saved['planning']['permissions']=='pending'
      task_fallbacks_write(home,'strong','hard',task_fallbacks(home))
      assert (store.path(saved['run_id'])/'run.json').read_bytes()==before
      app.root_change(panel_data.tree(home,agents=False)); app.git(['tasks',saved['run_id'],'choice'])
      await pilot.pause()
      assert ui_messages.t('Effective difficulty') in str(app.selected['detail'])
      capture(app,prefix+'-effective-selection')
      app.root_change(panel_data.tree(home,agents=False)); app.git(['tasks',saved['run_id'],'events'])
      await pilot.press('o'); assert isinstance(app.screen,panel_app.Reader)
      assert ui_messages.t('Combined event trace') in app.screen.title
      capture(app,prefix+'-trace')
      await pilot.press('r'); assert app.screen.raw and json.loads(app.screen.text)['schema']=='cadre-trace-v1'
      capture(app,prefix+'-trace-json')
      await pilot.press('q')
      assert (store.path(saved['run_id'])/'run.json').read_bytes()==before
lang = 'en'
for size in ((200,50),(100,30)):
  asyncio.run(run(lang,size))
print(json.dumps({'screens':len(screens),'cases':2}))
'''
        result = subprocess.run([str(self.ui_python()), '-c', script, str(LIB)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1]), {'screens': 16, 'cases': 2})

    def ui_python(self) -> Path:
        py = Path(os.environ.get('CADRE_TEST_PANEL_PYTHON') or os.environ.get('CADRE_UI_PYTHON')
                  or Path.home() / '.local/share/cadre-ui-venv/bin/python')
        if not py.exists():
            self.skipTest('Textual environment is not installed')
        return py

    def test_three_step_request_uses_the_real_cli_and_preserves_limits_and_sizes(self):
        script = r'''
import asyncio,json,os,sys,tempfile
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import panel_app,panel_data,ui_messages
from run_store import RunStore
from textual.widgets import Input,Select,TextArea
evidence = os.environ.get('CADRE_REQUEST_EVIDENCE')
screens = []
def capture(app,name):
    screens.append(name)
    if evidence:
        directory = Path(evidence); directory.mkdir(parents=True,exist_ok=True)
        (directory/(name+'.svg')).write_text(app.export_screenshot())
async def wait(pilot,app):
    await app.workers.wait_for_complete()
    await pilot.pause(.15)
async def run(lang,size):
  with tempfile.TemporaryDirectory(prefix='cadre-request-ui-') as directory:
    home = Path(directory); repo=home/'repo'; repo.mkdir()
    (home/'projects/lab').mkdir(parents=True)
    (home/'projects/lab/repo-path').write_text(str(repo))
    backlog=repo/'BACKLOG.md'; backlog.write_text('### [x] B001 Done\n### [ ] B002 Remaining\n')
    os.environ.update(CADRE_HOME=str(home),CADRE_NO_NET='1')
    store=RunStore(home); app=panel_app.Cadre(home=home)
    messages=[]; app.notify=lambda m,**kw:messages.append(str(m))
    prefix=f'{lang}-{size[0]}x{size[1]}'
    async with app.run_test(size=size) as pilot:
      await wait(pilot,app)
      app.push_screen(panel_app.FreshTask([{'round':'project_ref','entry_name':'lab','label':'lab'}]))
      await pilot.pause()
      await pilot.press('ctrl+s')
      app.screen.query_one('#nt-text',TextArea).text='Cancel before submission'
      await pilot.press('ctrl+s')
      assert app.screen.step==3 and not store.list()
      await pilot.press('escape')
      assert not isinstance(app.screen,panel_app.FreshTask) and not store.list()
      app.push_screen(panel_app.FreshTask([{'round':'project_ref','entry_name':'lab','label':'lab'}]))
      await pilot.pause()
      screen=app.screen
      screen.query_one('#nt-mode',Select).value='backlog'
      await pilot.pause()
      screen.query_one('#nt-backlog',Input).value='BACKLOG.md'
      capture(app,prefix+'-source')
      await pilot.press('ctrl+s')
      screen.query_one('#nt-text',TextArea).text='Preserve B001 and complete B002'
      screen.query_one('#nt-output',Input).value='Working output'
      screen.query_one('#nt-acceptance',Input).value='B002 tested; B001 preserved'
      screen.query_one('#nt-preference',Select).value='strong'
      screen.query_one('#nt-difficulty',Select).value='hard'
      screen.query_one('#nt-budget',Input).value='NaN'
      await pilot.press('ctrl+s')
      assert screen.step==2 and not store.list(),messages
      capture(app,prefix+'-invalid')
      screen.query_one('#nt-budget',Input).value='0.25'
      screen.query_one('#nt-duration',Input).value='15'
      screen.query_one('#nt-duration',Input).focus()
      await pilot.pause()
      assert screen.query_one('#nt-duration').region.bottom <= size[1]
      capture(app,prefix+'-limits')
      await pilot.press('ctrl+s')
      assert screen.step==3 and not store.list()
      await pilot.press('ctrl+b')
      assert screen.step==2 and screen.query_one('#nt-budget',Input).value=='0.25'
      await pilot.press('ctrl+s')
      capture(app,prefix+'-summary')
      backlog.write_text('### [ ] B002 Source changed\n')
      await pilot.press('enter')
      await wait(pilot,app)
      assert app.screen is screen and not screen.sending and not store.list(),messages
      capture(app,prefix+'-changed-source')
      await pilot.press('ctrl+b','ctrl+s')
      submitted=dict(screen.request)
      await pilot.press('enter','enter')
      await wait(pilot,app)
      assert app.screen is not screen
      records=store.list(); assert len(records)==1,records
      record=records[0]; rid=record['run_id']
      assert record['budget_cap_usd']==.25 and record['budget_spent_usd']==0 and record['tasks']=={}
      assert record['request']['duration_minutes']==15 and record['request']['preference']=='strong'
      assert record['request']['difficulty_override']=='hard' and record['planning']['difficulty_estimate'] is None
      assert record['request']['acceptance']=='B002 tested; B001 preserved'
      result=panel_app.subprocess.run(['bash',str(panel_app.RUNNER/'bin/cadre.sh'),'request'],input=json.dumps(submitted),text=True,capture_output=True,env=dict(os.environ),timeout=10)
      assert result.returncode==0 and json.loads(result.stdout)['run_id']==rid,result.stderr
      assert len(store.list())==1
      app.root_change(panel_data.tree(home,agents=False)); app.git(['tasks',rid])
      await pilot.pause()
      capture(app,prefix+'-waiting')
      node=panel_data.task_node(home,record)
      assert node['status']=='waiting' and node['stage_label']==ui_messages.t('Waiting for the planning gate')
      assert ui_messages.t('{n} tasks running',n=0) in app.header_r().plain
      assert ui_messages.t('{n} task inputs awaiting planning',n=1) in app.header_r().plain
      assert node['input_waiting'] and not node.get('delivery')
      before={p:p.read_bytes() for p in store.path(rid).rglob('*') if p.is_file()}
      app.git(['tasks',rid,'input'])
      await pilot.press('o')
      assert isinstance(app.screen,panel_app.Reader)
      await pilot.press('q')
      assert before=={p:p.read_bytes() for p in store.path(rid).rglob('*') if p.is_file()}
      app.git(['tasks'])
      app.choice=next(i for i,node in enumerate(app.elements) if node['id']==rid)
      app.draw(); await pilot.pause()
      other=home/'other'; (other/'projects/lab').mkdir(parents=True)
      (other/'projects/lab/repo-path').write_text(str(repo))
      other_store=RunStore(other); other_record=other_store.request(submitted)
      os.environ['CADRE_HOME']=str(other)
      await pilot.press('x'); await pilot.pause(panel_app.Approval.DEAD_DURATION_SEC+0.05); await pilot.press('enter')
      await wait(pilot,app)
      assert store.read(rid)['state']=='cancelled'
      assert other_store.read(other_record['run_id'])['state']=='accepted','The wrong home was cancelled'
      assert not (home/'chain').exists()
      assert sorted(p.name for p in repo.iterdir())==['BACKLOG.md']
lang = 'en'
for size in ((200,50),(100,30)):
  asyncio.run(run(lang,size))
print(json.dumps({'screens':len(screens),'cases':2,'provider_calls':0}))
'''
        result = subprocess.run([str(self.ui_python()), '-c', script, str(LIB)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1]), {'screens': 12, 'cases': 2, 'provider_calls': 0})

    def test_project_economy_navigation_and_live_receipt_and_sizes(self):
        script = r'''
import asyncio,json,os,sys,tempfile
from pathlib import Path
sys.path.insert(0,sys.argv[1])
sys.path.insert(0,str(Path(sys.argv[1]).parent/'tests'))
import panel_app,panel_data,ui_messages
from test_panel import EconomyTests
def capture(app,name):
    if os.environ.get('CADRE_ECONOMY_EVIDENCE'):
        (Path(os.environ['CADRE_ECONOMY_EVIDENCE'])/(name+'.svg')).write_text(app.export_screenshot())
async def run(root):
    screens=0
    language = 'en'
    for size in ((200,50),(100,30)):
        home=root/f'{language}-{size[0]}'
        home.mkdir()
        os.environ['CADRE_HOME']=str(home)
        store,parent,child,orphan,project,ledger,pending=EconomyTests().fixture(home)
        panel_data.clean()
        before={str(p):p.read_bytes() for p in home.rglob('*') if p.is_file()}
        app=panel_app.Cadre(home=home)
        async with app.run_test(size=size) as pilot:
            for _ in range(100):
                if app.loaded: break
                await pilot.pause(.03)
            await app.workers.wait_for_complete()
            app.git(['tasks','history',parent])
            app.choice=next(i for i,n in enumerate(app.elements) if n['id']=='spend')
            app.draw()
            assert app.selected['economy']['spent']==700_000
            assert app.selected['economy']['estimated']==300_000
            await pilot.pause()
            prefix=f'{language}-{size[0]}x{size[1]}'
            capture(app,prefix+'-project')
            await pilot.press('enter')
            app.choice=next(i for i,n in enumerate(app.elements) if n['id']==child)
            app.draw()
            assert app.selected['economy']['spent']==500_000
            await pilot.pause()
            capture(app,prefix+'-child')
            await pilot.press('enter')
            app.choice=next(i for i,n in enumerate(app.elements) if n['id']=='g1:a2')
            app.draw()
            assert app.selected['economy']['spent']==app.selected['economy']['estimated']==300_000
            await pilot.pause()
            capture(app,prefix+'-attempt')
            await pilot.press('q')
            assert app.chain()[-1]['id']=='spend'
            app.git(['tasks','history',parent,'spend',orphan])
            assert app.selected['economy']['held']==1_000_000
            assert before=={str(p):p.read_bytes() for p in home.rglob('*') if p.is_file()}
            original=store.read(parent)
            ledger.settle(pending,400_000,'receipt:'+pending)
            app.root_change(panel_data.tree(home,agents=False))
            assert app.selected['economy']['spent']==400_000 and app.selected['economy']['held']==0
            assert store.read(parent)==original
            await pilot.pause()
            capture(app,prefix+'-settled')
            screens+=4
    print(json.dumps({'screens':screens}))
with tempfile.TemporaryDirectory(prefix='cadre-economy-ui-') as directory:
    os.environ['CADRE_NO_NET']='1'
    asyncio.run(run(Path(directory)))
'''
        result = subprocess.run([str(self.ui_python()), '-c', script, str(LIB)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1])['screens'], 8)

    def test_budget_editor_validation_preview_cancel_save_override_and_stale_source(self):
        script = r'''
import asyncio, json, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import panel_app, panel_data, ui_messages, run_store
from monthly_budget import cap_micro
from textual.widgets import Input
async def run(home):
    screens = 0
    language = 'en'
    for size in ((200, 50), (100, 30)):
        panel_data.clean()
        app = panel_app.Cadre(home=home)
        async with app.run_test(size=size) as pilot:
            for _ in range(100):
                if app.loaded:
                    break
                await pilot.pause(0.03)
            app.git(['settings'])
            app.choice = next(i for i,n in enumerate(app.elements) if n.get('budget_setting', {}).get('round') == 'monthly')
            app.draw()
            await pilot.press('enter')
            assert isinstance(app.screen, panel_app.BudgetSetting)
            entry = app.screen.query_one(Input)
            state = app.screen.state
            path = Path(state['path'])
            original = path.read_bytes() if path.exists() else None
            entry.value = 'NaN'
            await pilot.press('enter')
            assert not app.screen.preview
            assert (path.read_bytes() if path.exists() else None) == original
            assert str(app.screen.query_one('#ba-error').render())
            if os.environ.get('CADRE_SETTING_EVIDENCE'):
                (Path(os.environ['CADRE_SETTING_EVIDENCE']) / f'{language}-invalid-{size[0]}.svg').write_text(app.export_screenshot())
            entry.value = '70.125'
            await pilot.press('enter')
            assert app.screen.preview
            if os.environ.get('CADRE_SETTING_EVIDENCE'):
                (Path(os.environ['CADRE_SETTING_EVIDENCE']) / f'{language}-review-{size[0]}.svg').write_text(app.export_screenshot())
            await pilot.press('escape')
            assert (path.read_bytes() if path.exists() else None) == original
            await pilot.press('enter')
            app.screen.query_one(Input).value = '70.125'
            await pilot.press('enter', 'ctrl+e')
            assert not app.screen.preview
            app.screen.query_one(Input).value = '71.125'
            await pilot.press('enter', 'enter')
            await app.workers.wait_for_complete()
            assert cap_micro(home) == 71_125_000 and path.read_text().strip() == '71.125'
            app.git(['settings'])
            app.choice = next(i for i,n in enumerate(app.elements) if n.get('budget_setting', {}).get('round') == 'monthly')
            app.draw()
            await pilot.press('enter')
            app.screen.query_one(Input).value = '12'
            await pilot.press('enter')
            path.write_text('9\n')
            await pilot.press('enter')
            assert isinstance(app.screen, panel_app.BudgetSetting) and path.read_text().strip() == '9'
            await pilot.press('escape')
            os.environ['CADRE_MONTHLY_CAP_USD'] = '7'
            app.reload(True)
            await app.workers.wait_for_complete()
            node = panel_data.budget_setting_node(home, 'monthly')
            assert node['budget_setting']['value'] == 7 and not node['action']
            assert cap_micro(home) == 7_000_000
            del os.environ['CADRE_MONTHLY_CAP_USD']
            app.reload(True)
            await app.workers.wait_for_complete()
            app.git(['settings', 'upper-limit'])
            assert app.selected['budget_setting']['project_ref'] == 'lab'
            await pilot.press('enter')
            app.screen.query_one(Input).value = '0'
            await pilot.press('enter')
            if os.environ.get('CADRE_SETTING_EVIDENCE'):
                (Path(os.environ['CADRE_SETTING_EVIDENCE']) / f'{language}-project-{size[0]}.svg').write_text(app.export_screenshot())
            await pilot.press('enter')
            assert run_store.run_cap_usd(home, 'lab') == 0
            screens += 3
    print(json.dumps({'screens': screens}))
with tempfile.TemporaryDirectory(prefix='cadre-settings-ui-') as directory:
    os.environ.update(CADRE_HOME=directory, CADRE_NO_NET='1')
    (Path(directory) / 'projects/lab').mkdir(parents=True)
    (Path(directory) / 'projects/lab/repo-path').write_text(sys.argv[1])
    asyncio.run(run(Path(directory)))
'''
        result = subprocess.run([str(self.ui_python()), '-c', script, str(LIB)], capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1])['screens'], 6)

    def test_four_role_menu_and_lightweight_navigation(self):
        script = r'''
import asyncio, json, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import panel_app, panel_data, ui_messages
async def run(home):
    count = 0
    language = 'en'
    for size in ((200, 50), (100, 30)):
        panel_data.clean()
        app = panel_app.Cadre(home=home)
        async with app.run_test(size=size) as pilot:
            for _ in range(100):
                if app.section('agents')['child']:
                    break
                await pilot.pause(0.03)
            await pilot.press('4')
            assert [n['id'] for n in app.elements] == ['plan', 'impl', 'quality', 'on-evaluation']
            for mode in ('roles', 'lightweight'):
                if mode == 'lightweight':
                    await pilot.press('down', 'enter')
                    app.choice = next(i for i, n in enumerate(app.elements) if n['id'] == 'impl-light')
                    app.draw()
                    await pilot.press('enter')
                    assert app.cap_amount()['id'] == 'impl-light' and app.elements
                if os.environ.get('CADRE_ROLE_EVIDENCE'):
                    (Path(os.environ['CADRE_ROLE_EVIDENCE']) / f'{language}-{mode}-{size[0]}x{size[1]}.svg').write_text(app.export_screenshot())
                count += 1
    print(json.dumps({'screens': count}))
with tempfile.TemporaryDirectory(prefix='cadre-role-ui-') as directory:
    os.environ.update(CADRE_HOME=directory, CADRE_NO_NET='1')
    asyncio.run(run(Path(directory)))
'''
        result = subprocess.run([str(self.ui_python()), '-c', script, str(LIB)], capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1])['screens'], 4)

    def test_compact_dashboard_help_result_and_report_navigation_and_sizes(self):
        script = r'''
import asyncio, io, json, os, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import panel_app, panel_data, ui_messages
from run_store import RunStore
from monthly_budget import MonthlyLedger
from rich.console import Console
def capture(app, name):
    if os.environ.get('CADRE_PANEL_EVIDENCE'):
        (Path(os.environ['CADRE_PANEL_EVIDENCE']) / (name + '.svg')).write_text(app.export_screenshot())
async def run(root):
    screens = 0
    language = 'en'
    for size in ((200, 50), (100, 30)):
        for mode in ('empty', 'many', 'stale', 'broken'):
            home = root / f'{language}-{size[0]}-{mode}'
            home.mkdir()
            os.environ['CADRE_HOME'] = str(home)
            store = RunStore(home)
            ledger = MonthlyLedger(home / 'chain/monthly-budget.db', cap=65_000_000)
            ledger.reserve('openrouter', 2_000_000)
            for i in range(15 if mode == 'many' else 0):
                record = store.accept(f'lab{i}', 'Long task title ' * 70)
                store.update(record['run_id'], state='running', spend_alarm={'ts': time.time(), 'reason': 'approval needed'})
            if mode == 'stale':
                cache = home / 'chain/request-budget.json'
                cache.write_text(json.dumps({'observed': time.time() - 600, 'budget': {'ts': '2026-01-01T12:00:00', 'today': '2026-01-01'}}))
            if mode == 'broken':
                path = home / 'runs/broken'
                path.mkdir(parents=True)
                (path / 'run.json').write_text('{')
                (home / 'chain/monthly-budget.db').write_bytes(b'broken database')
            before = {str(p): p.read_bytes() for p in home.rglob('*') if p.is_file()}
            panel_data.clean()
            app = panel_app.Cadre(home=home)
            async with app.run_test(size=size) as pilot:
                for _ in range(100):
                    if app.loaded:
                        break
                    await pilot.pause(0.03)
                await app.workers.wait_for_complete()
                await pilot.pause()
                panel = app.section('panel')
                console = Console(width=app.query_one('#detail-text').size.width, record=True, file=io.StringIO())
                console.print(app.detail_r(panel, True))
                rendered = console.export_text()
                assert len(rendered.splitlines()) <= 12, (language, size, mode, app.query_one('#detail-text').size.width, rendered, panel['detail'])
                assert app.query_one('#detail').max_scroll_y == 0
                assert not app.query_one('#sub').display and not app.query_one('#context').display
                assert len(panel['detail']) <= 12
                capture(app, f'{language}-{mode}-{size[0]}x{size[1]}')
                screens += 1
                if mode == 'broken':
                    assert panel_app.t('Remaining unknown') in rendered, (language, size, rendered, panel['detail'])
                    assert panel_app.t('Run records could not be read')[:15] in rendered
                    app.git(['panel', 'attention'])
                    assert any(n['id'] == 'attention-record' for n in app.elements)
                if mode == 'empty':
                    original = (list(app.path), app.choice)
                    await pilot.press('question_mark')
                    assert isinstance(app.screen, panel_app.Reader)
                    assert all(key in app.screen.text for key,_ in panel_app.HELP_KEYS)
                    assert panel_app.t('Finished task: Tasks › History › task › Result / Delivery. Report for report work.') in app.screen.text
                    capture(app, f'{language}-help-{size[0]}x{size[1]}')
                    await pilot.press('pagedown', 'pageup', 'escape')
                    assert (app.path, app.choice) == original
                    assert {str(p):p.read_bytes() for p in home.rglob('*') if p.is_file()} == before
                    wt = home / 'output'
                    wt.mkdir()
                    (wt / 'REPORT.md').write_text('# Report\n\nLocal fixture output.')
                    record = store.accept('report', 'ROUND: report\nREPORT.md (write only this file)')
                    rid = record['run_id']
                    store.update(rid, state='completed', worktree=str(wt), final_result={'status':'PASS','qa':{'status':'PASS'},'review':{'status':'PASS'}})
                    panel_data.clean()
                    app.root_change(panel_data.tree(home, agents=False))
                    app.git(['tasks', 'history', rid])
                    assert {'outcome', 'delivery', 'report'}.issubset({n['id'] for n in app.elements})
                    app.choice = next(i for i,n in enumerate(app.elements) if n['id'] == 'outcome')
                    app.draw()
                    assert app.selected['summary'] == 'PASS'
                    capture(app, f'{language}-result-{size[0]}x{size[1]}')
                    await pilot.press('o')
                    assert isinstance(app.screen, panel_app.Reader) and 'final_result' in app.screen.text
                    await pilot.press('q')
                    app.choice = next(i for i,n in enumerate(app.elements) if n['id'] == 'report')
                    app.draw()
                    await pilot.press('o')
                    assert isinstance(app.screen, panel_app.Reader) and 'Local fixture output.' in app.screen.text
                    capture(app, f'{language}-report-{size[0]}x{size[1]}')
                    await pilot.press('q')
                    screens += 3
    print(json.dumps({'screens': screens}))
with tempfile.TemporaryDirectory(prefix='cadre-panel-ui-') as directory:
    os.environ['CADRE_NO_NET'] = '1'
    asyncio.run(run(Path(directory)))
'''
        result = subprocess.run([str(self.ui_python()), '-c', script, str(LIB)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1])['screens'], 14)

    def test_readable_and_raw_log_navigation_and_sizes(self):
        script = r'''
import asyncio, json, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import panel_app, panel_data, ui_messages
from run_store import RunStore
async def run(home):
    store = RunStore(home)
    rid = store.accept('personal', 'Record reading')['run_id']
    store.update(rid, state='completed')
    attempt = store.path(rid) / 'tasks/g1/attempts/a1'
    attempt.mkdir(parents=True)
    (attempt / 'attempt.json').write_text(json.dumps({'role': 'impl'}))
    (attempt / 'stdout.jsonl').write_text(json.dumps({'type': 'tool_use', 'part': {'tool': 'bash', 'state': {'input': {'command': 'pytest -q'}, 'output': 'line\n' * 80}}}) + '\nbroken\n')
    before = {p: p.read_bytes() for p in (home / 'runs').rglob('*') if p.is_file()}
    screens = []
    language = 'en'
    for size in ((200, 50), (100, 30)):
        panel_data.clean()
        app = panel_app.Cadre(home=home)
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.2)
            await app.workers.wait_for_complete()
            app.path, app.choice = [1, 0], 0
            node = app.selected
            app.path.append(app.elements.index(node))
            agents = next(i for i, c in enumerate(node['child']) if c['id'] == 'agents')
            app.choice = agents
            app.path.append(agents)
            app.choice = 0
            app.draw()
            await pilot.press('o')
            assert isinstance(app.screen, panel_app.Reader)
            assert 'pytest -q' in app.screen.text
            for mode in ('summary', 'raw'):
                if mode == 'raw':
                    await pilot.press('r')
                    assert app.screen.raw
                    assert '"type": "tool_use"' in app.screen.text
                if os.environ.get('CADRE_RECORD_EVIDENCE'):
                    name = f'{language}-{mode}-{size[0]}x{size[1]}.svg'
                    (Path(os.environ['CADRE_RECORD_EVIDENCE']) / name).write_text(app.export_screenshot())
                    screens.append(name)
            await pilot.press('pagedown', 'r', 'q')
            assert app.selected['id'].startswith('agent-')
            await pilot.press('o', 'escape')
            assert not isinstance(app.screen, panel_app.Reader)
            assert store.read(rid)['state'] == 'completed'
    assert before == {p: p.read_bytes() for p in (home / 'runs').rglob('*') if p.is_file()}
    print(json.dumps({'screens': screens, 'read_only': True, 'cases': 4}))
with tempfile.TemporaryDirectory(prefix='cadre-log-ui-') as directory:
    os.environ.update(CADRE_HOME=directory, CADRE_NO_NET='1')
    asyncio.run(run(Path(directory)))
'''
        result = subprocess.run([str(self.ui_python()), '-c', script, str(LIB)], capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1])['cases'], 4)

    def test_live_monitor_refresh_selection_errors_reconnect_and_close(self):
        script = r'''
import asyncio, io, json, os, subprocess, sys, tempfile, time
from pathlib import Path
from rich.console import Console
sys.path.insert(0, sys.argv[1])
import panel_app, panel_data, ui_messages
from run_store import RunStore
async def wait(pilot, predicate, seconds=5):
    start = time.monotonic()
    while not predicate():
        assert time.monotonic() - start < seconds, 'live refresh deadline'
        await pilot.pause(0.03)
    return time.monotonic() - start
async def run(home):
    store = RunStore(home)
    rid = store.accept('personal', 'First run')['run_id']
    other = store.accept('personal', 'Next run')['run_id']
    store.update(other, state='running')
    (store.path(rid) / 'plan.json').write_text(json.dumps({'tasks': [{'id': 'g1', 'definition': 'First job', 'files': ['a.py'], 'acceptance': 'pytest -q'}]}))
    log = store.path(rid) / 'dispatch.log'
    log.write_text('2026-10-10 10:00:00 plan acceptance\n')
    readings = []
    language = 'en'
    for size in ((200, 50), (100, 30)):
        store.update(rid, state='running', budget_spent_usd=0.1, budget_cap_usd=1)
        store.task_update(rid, 'g1', state='running')
        panel_data.clean()
        app = panel_app.Cadre(home=home)
        async with app.run_test(size=size) as pilot:
            await wait(pilot, lambda: app.loaded)
            app.path = [1]
            app.choice = next(i for i, n in enumerate(app.elements) if n['id'] == rid)
            app.draw()
            await pilot.press('i')
            screen = app.screen
            assert isinstance(screen, panel_app.LiveMonitoring) and screen.run_id == rid
            await wait(pilot, lambda: bool(screen.data))
            screen.choice = next(i for i, (p, n) in enumerate(screen.elements) if n.get('id') == 'plan')
            key = screen.elements[screen.choice][0]
            await pilot.press('o')
            assert isinstance(app.screen, panel_app.Reader)
            await pilot.press('q')
            store.update(rid, budget_spent_usd=0.25)
            store.task_update(rid, 'g1', state='produced')
            log.write_text(log.read_text() + '2026-10-10 10:00:01 task g1 produced\n')
            seconds = await wait(pilot, lambda: any(t == 'field' and v[0] == ui_messages.t('Spend') and panel_data.money(0.25) in v[1] for t, v in screen.data['detail']))
            assert seconds <= 2.8, seconds
            assert screen.elements[screen.choice][0] == key
            assert screen.data['source_time'] and screen.data['last_action_time']
            readings.append(seconds)
            source = store.path(rid) / 'run.json'
            saved = source.read_bytes()
            source.write_text('{')
            await wait(pilot, lambda: bool(screen.error))
            assert screen.data and screen.elements[screen.choice][0] == key
            console = Console(file=io.StringIO(), width=size[0], record=True)
            console.print(screen.query_one('#trace-status').content)
            assert ui_messages.t('Stale data · read error: {e}', e=screen.error) in console.export_text()
            if os.environ.get('CADRE_MONITORING_EVIDENCE'):
                (Path(os.environ['CADRE_MONITORING_EVIDENCE']) / f'{language}-stale-{size[0]}x{size[1]}.svg').write_text(app.export_screenshot())
            source.write_bytes(saved)
            await wait(pilot, lambda: not screen.error)
            if os.environ.get('CADRE_MONITORING_EVIDENCE'):
                (Path(os.environ['CADRE_MONITORING_EVIDENCE']) / f'{language}-live-{size[0]}x{size[1]}.svg').write_text(app.export_screenshot())
            store.update(rid, state='completed', final_result={'status': 'PASS', 'qa': {'status': 'PASS'}, 'review': {'status': 'PASS'}})
            await wait(pilot, lambda: screen.data.get('active') is False)
            assert {'plan', 'test', 'judge', 'delivery'} <= {n['id'] for n in screen.data['child']}
            assert all(n['status'] != 'now_ts' for _, n in screen.elements)
            if os.environ.get('CADRE_MONITORING_EVIDENCE'):
                (Path(os.environ['CADRE_MONITORING_EVIDENCE']) / f'{language}-terminal-{size[0]}x{size[1]}.svg').write_text(app.export_screenshot())
            store.update(rid, state='running')
            await wait(pilot, lambda: any(n.get('id') == rid for n in app.section('tasks')['child']))
            before = {p: p.read_bytes() for p in (home / 'runs').rglob('*') if p.is_file()}
            await pilot.press('down', 'up', 'pagedown', 'q')
            app.path = [1]
            app.choice = next(i for i, n in enumerate(app.elements) if n['id'] == rid)
            app.draw()
            await pilot.press('i')
            await wait(pilot, lambda: isinstance(app.screen, panel_app.LiveMonitoring) and bool(app.screen.data))
            assert app.screen.run_id == rid
            await pilot.press('escape')
            assert not isinstance(app.screen, panel_app.LiveMonitoring)
            assert before == {p: p.read_bytes() for p in (home / 'runs').rglob('*') if p.is_file()}
            assert store.read(rid)['state'] == store.read(other)['state'] == 'running'
            if language == 'en' and size == (100, 30):
                writer = "import sys,time; from pathlib import Path; sys.path.insert(0,sys.argv[1]); from run_store import RunStore; s=RunStore(Path(sys.argv[2])); [(s.update(sys.argv[3],budget_spent_usd=round(0.3+i/100,2)),time.sleep(0.2)) for i in range(5)]"
                await pilot.press('i')
                with subprocess.Popen([sys.executable, '-c', writer, sys.argv[1], str(home), rid]) as producer:
                    await pilot.press('q')
                    await wait(pilot, lambda: store.read(rid)['budget_spent_usd'] >= 0.3)
                    assert producer.poll() is None
                    assert await asyncio.to_thread(producer.wait, 5) == 0
                assert store.read(rid)['budget_spent_usd'] == 0.34
                assert store.read(rid)['state'] == 'running'
    result = {'cases': len(readings), 'refresh_seconds': readings, 'read_only': True, 'selected_run': rid, 'other_run': other}
    if os.environ.get('CADRE_MONITORING_EVIDENCE'):
        (Path(os.environ['CADRE_MONITORING_EVIDENCE']) / 'results.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result))
with tempfile.TemporaryDirectory(prefix='cadre-live-ui-') as directory:
    os.environ.update(CADRE_HOME=directory, CADRE_NO_NET='1')
    asyncio.run(run(Path(directory)))
'''
        result = subprocess.run([str(self.ui_python()), '-c', script, str(LIB)], capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1])['cases'], 2)

    def test_readability_contrast_full_selection_and_persisted_palettes_on_real_records(self):
        py = self.ui_python()
        with tempfile.TemporaryDirectory() as directory:
            r = subprocess.run([str(py), '-c', READABILITY, str(LIB)], capture_output=True, text=True, timeout=300,
                               env={**os.environ, 'CADRE_HOME': directory, 'CADRE_NO_NET': '1'})
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        result = json.loads(r.stdout.splitlines()[-1])
        self.assertEqual(len(result['screens']), 12)
        self.assertTrue(result['run_read_only'])

    def test_sample_mode_navigation_reader_new_task_and_live_updates(self):
        py = self.ui_python()
        with tempfile.TemporaryDirectory() as t:
            r = subprocess.run([str(py), '-c', SAMPLE, str(LIB)], capture_output=True, text=True, timeout=300, cwd=t,
                               env={**os.environ, 'CADRE_HOME': t, 'CADRE_NO_NET': '1'})
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        expected = ('tasks|tasks|Tasks>feedloom>Plan>f3 Entry dat>Plan|g2|False|5|Reader|0:True|True:True|here:fresh|3:fresh:newfolder|'
                    'True|Everforest:Everforest:True|2')
        self.assertEqual(r.stdout.splitlines(), [expected, expected])

    def test_load_errors_confirm_cancel_and_empty_task_keep_the_app_alive(self):
        py = self.ui_python()
        with tempfile.TemporaryDirectory() as t:
            r = subprocess.run([str(py), '-c', ERROR, str(LIB), t], capture_output=True, text=True, timeout=300, cwd=t,
                               env={**os.environ, 'CADRE_HOME': t, 'CADRE_NO_NET': '1'})
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        outcome = json.loads(r.stdout.splitlines()[-1])
        self.assertTrue(outcome['standing'], 'unreadable data does not close the panel')
        self.assertEqual([b for b in outcome['notifications'] if b.startswith('Data could not be read')], ['Data could not be read: ValueError: broken record'],
                         'the same error is reported once')
        self.assertNotIn('Approval', outcome['screens'])
        self.assertEqual(outcome['actions'], [], 'q and Esc close without confirmation')
        self.assertEqual(outcome['step'], 2)
        self.assertIn('The task text is empty. Write the task first.', outcome['notifications'])


    def test_form_renders_code_blocks_plan_timeout_and_help_keys(self):
        """FIX-37/38/41: a `code` block is drawn, plan generation is not tied to the panel's lifetime, the guide mentions the Approve/Space keys."""
        py = self.ui_python()
        script = (
            "import json, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import panel_app, ui_messages\n"
            "parts = panel_app.form([('field', ('A', 'B')), ('code', 'CODE-TEXT-123'), ('line', 'then')])\n"
            "text = ' '.join(getattr(part, 'plain', str(part)) for part in parts)\n"
            "guide = panel_app.help_text()\n"
            "import os, time, pathlib\n"
            "record = pathlib.Path(sys.argv[2]) / 'logs' / 'request-plan-r1.log'\n"
            "start = time.monotonic()\n"
            "p = panel_app.behind_start([sys.executable, '-c', 'import os, sys, time; time.sleep(1); print(os.getpgid(0)); sys.stderr.write(\"error-line\")'], dict(os.environ), record)\n"
            "fast = time.monotonic() - start < 0.5\n"
            "p.wait(20)\n"
            "log = record.read_text()\n"
            "separate = int(log.split()[0]) != os.getpgid(0)\n"
            "print(json.dumps(['CODE-TEXT-123' in text, fast and separate and 'error-line' in log, 'Confirmation window' in guide, '/Space' in guide]))\n")
        with tempfile.TemporaryDirectory() as t:
            r = subprocess.run([str(py), '-c', script, str(LIB), t], capture_output=True, text=True, timeout=120,
                               env={**os.environ, 'CADRE_HOME': t, 'CADRE_NO_NET': '1'})
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        self.assertEqual(json.loads(r.stdout.splitlines()[-1]), [True, True, True, True],
                         'plan generation starts in a separate session without waiting; its output is written to the record')

    def test_new_task_modal_fits_small_screens_with_many_projects_and_long_text(self):
        py = self.ui_python()
        with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as operation:
            r = subprocess.run([str(py), '-c', WINDOW, str(LIB), t], capture_output=True, text=True, timeout=300, cwd=operation,
                               env={**os.environ, 'CADRE_HOME': t, 'CADRE_NO_NET': '1'})
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        self.assertEqual(json.loads(r.stdout.splitlines()[-1]), [[True, True, 3, True, True]] * 2,
                         'the hint and the selected project_ref are visible, long task text is shortened')


    def test_real_mode_registers_folders_sends_tasks_and_cancels_through_the_cli(self):
        py = self.ui_python()
        with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as repo:
            (Path(t) / 'projects/lab').mkdir(parents=True)
            (Path(t) / 'projects/lab/repo-path').write_text(repo)
            r = subprocess.run([str(py), '-c', REAL, str(LIB), t], capture_output=True, text=True, timeout=300,
                               env={**os.environ, 'CADRE_HOME': t, 'CADRE_NO_NET': '1'})
            self.assertEqual(r.returncode, 0, r.stderr[-3000:])
            self.assertTrue((Path(t) / 'projects/fresh-project-ref/repo-path').is_file())
        outcome = json.loads(r.stdout.splitlines()[-1])
        self.assertNotIn('loading', outcome['start'])
        tasks = [c for c in outcome['calls'] if c['command'][-2] == 'task']
        self.assertEqual([(Path(c['command'][1]).name, c['command'][-1], 'repo' if c['cwd'] == repo else Path(c['cwd']).name, c['watch']) for c in tasks],
                         [('cadre.sh', 'add notes.py', 'repo', '0'), ('cadre.sh', 'first task', 'fresh-project-ref', '0')])
        self.assertEqual(outcome['approval'], 'Approval')
        self.assertEqual([c['command'][-2:] for c in outcome['calls'] if 'cancel' in c['command']], [['cancel', outcome['active']]])
        self.assertEqual(outcome['followed'], outcome['active'],
                         "live monitoring opens the selected run, not the project's last run")
        self.assertEqual([c for c in outcome['calls'] if 'watch' in c['command']], [], 'the panel monitor opens no external process')
        self.assertEqual(len(outcome['refresh']), 1, "a stale budget measurement is handed to the engine's refresher at most once a minute, never in a home without network")


END_TO_END = """
import asyncio, json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import panel_app, panel_data, ui_messages
home, empty = Path(sys.argv[2]), Path(sys.argv[3])
notifications = []
async def execute():
    app = panel_app.Cadre(sample=False, home=home)
    app.notify = lambda m, **kw: notifications.append(str(m))
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause(0.5)
        await pilot.press('n')
        await pilot.pause()
        first = [s['round'] for s in app.screen.options]
        await pilot.press('up', 'enter', *'newproject', 'enter', *'hello', 'ctrl+s', 'enter')
        await app.workers.wait_for_complete()
        os.chdir(empty)
        await pilot.press('n')
        await pilot.pause()
        second = [s['round'] for s in app.screen.options]
        await pilot.press('enter', *'second', 'ctrl+s', 'enter')
        await app.workers.wait_for_complete()
        app.root_change(panel_data.tree(home, agents=False))
        tasks = app.section('tasks')['child']
        listed = sorted({str(c.get('project_ref')) for c in tasks[:-1] + tasks[-1]['child']})
    return first, second, listed
first, second, listed = asyncio.run(execute())
print(json.dumps({'first': first, 'second': second, 'listed': listed, 'notifications': notifications}))
"""


class PlanOperationsUITests(unittest.TestCase):
    ui_python = PanelTests.ui_python


    def test_plan_operations_use_real_cli_and_read_only_history_and_sizes(self):
        script = r"""
import asyncio,json,os,sys,tempfile
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import panel_app,panel_data,ui_messages
from run_store import RunStore,FileLock
from textual.widgets import TextArea,Static
screens=[]
def capture(app,name):
    screens.append(name)
    if os.environ.get('CADRE_PLAN_EVIDENCE'):
        folder=Path(os.environ['CADRE_PLAN_EVIDENCE']); folder.mkdir(parents=True,exist_ok=True)
        (folder/(name+'.svg')).write_text(app.export_screenshot())
async def wait(app,pilot):
    await app.workers.wait_for_complete(); await pilot.pause(.1)
def operation(app,rid):
    app.git(['tasks',rid]); app.choice=next(i for i,c in enumerate(app.elements) if c.get('id')=='operation'); app.draw()
async def run(lang,size):
  with tempfile.TemporaryDirectory(prefix='cadre-plan-ui-') as directory:
    home=Path(directory); os.environ.update(CADRE_HOME=str(home),CADRE_NO_NET='1')
    panel_data.clean()
    store=RunStore(home); rid=store.accept('lab','Fix a typo within README')['run_id']
    plan={'job_id':'p1','tasks':[{'id':'g1','definition':'README text','files':['README.md'],
          'acceptance':'README content preserved','model':'auto','difficulty':1,'parallel_group':1,'estimate_min':1,'dependent':[]}]}
    store.save_plan(rid,plan)
    log=home/'chain/model_router.jsonl'; log.parent.mkdir(exist_ok=True)
    row={'run_id':rid,'event':'chain','role':'impl','ts':'2026-10-11T10:00:00','chain':['opencode-go/glm-5.3'],
         'reason':['opencode-go/glm-5.3: OK','openrouter/model: cooldown api_key=synthetic_fixture_secret']}
    log.write_text(json.dumps(row)+'\n'+json.dumps(dict(row,run_id='unrelated'))+'\n'+json.dumps(dict(row,role='incomplete')))
    app=panel_app.Cadre(home=home)
    async with app.run_test(size=size) as pilot:
      await wait(app,pilot)
      operation(app,rid); await pilot.pause()
      selected_operation=app.selected; detail=str(selected_operation['detail'])
      assert 'cooldown' in detail and 'synthetic_fixture_secret' not in detail,detail
      assert 'incomplete' not in detail
      capture(app,f'{lang}-{size[0]}-decision')
      app.action_open(); await pilot.pause()
      app.git(['tasks',rid,'operation','plan-history'])
      before={str(p.relative_to(home)):p.read_bytes() for p in home.rglob('*') if p.is_file()}
      await pilot.press('o'); await pilot.pause()
      assert isinstance(app.screen,panel_app.Reader)
      capture(app,f'{lang}-{size[0]}-history')
      await pilot.press('q'); await pilot.pause()
      assert before=={str(p.relative_to(home)):p.read_bytes() for p in home.rglob('*') if p.is_file()}
      app.git(['tasks',rid,'operation','plan-change']); await pilot.press('enter'); await pilot.pause()
      assert isinstance(app.screen,panel_app.PlanChange)
      capture(app,f'{lang}-{size[0]}-change')
      await pilot.press('escape'); await pilot.pause()
      assert store.plan_view(rid)['changes']==[]
      app.git(['tasks',rid,'operation','plan-change']); await pilot.press('enter','ctrl+s'); await pilot.pause()
      assert isinstance(app.screen,panel_app.PlanChange)
      assert str(app.screen.query_one('#plan-change-error',Static).render())
      app.screen.query_one('#plan-change-text',TextArea).load_text('Change the method within the same scope')
      await pilot.press('ctrl+s'); await wait(app,pilot)
      assert store.plan_view(rid)['changes'][-1]['status']=='stop_requested'
      operation(app,rid); await pilot.pause(); capture(app,f'{lang}-{size[0]}-waiting')
      with FileLock(store.path(rid)/'.owner.lock'):
        app.git(['tasks',rid,'operation','plan-stop']); await pilot.press('enter'); await pilot.pause(panel_app.Approval.DEAD_DURATION_SEC+0.05); await pilot.press('enter'); await wait(app,pilot)
        assert store.plan_view(rid)['changes'][-1]['status']=='stop_requested'
      app.git(['tasks',rid,'operation','plan-stop']); await pilot.press('enter'); await pilot.pause(panel_app.Approval.DEAD_DURATION_SEC+0.05); await pilot.press('enter'); await wait(app,pilot)
      assert store.plan_view(rid)['changes'][-1]['status']=='stopped'
      operation(app,rid); await pilot.pause(); capture(app,f'{lang}-{size[0]}-stopped')
      app.git(['tasks',rid,'operation','plan-withdraw']); await pilot.press('enter'); await pilot.pause(panel_app.Approval.DEAD_DURATION_SEC+0.05); await pilot.press('enter'); await wait(app,pilot)
      assert store.plan_view(rid)['changes'][-1]['status']=='withdrawn'
      operation(app,rid); await pilot.pause(); capture(app,f'{lang}-{size[0]}-withdrawn')
      assert store.read(rid)['state']=='retryable'
lang = 'en'
for size in ((200,50),(100,30)):
  asyncio.run(run(lang,size))
print(json.dumps({'screens':len(screens),'cases':2}))
"""
        result = subprocess.run([str(self.ui_python()), '-c', script, str(LIB)], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1]), {'screens': 12, 'cases': 2})


class NewTaskEndToEndTests(e2e.E2EFixture):
    def executors_exit_wait(self):
        self.kill_guards()
        last = time.monotonic() + 10
        while time.monotonic() < last:
            r = subprocess.run(['pgrep', '-f', str(self.root)], capture_output=True, text=True, timeout=5)
            if r.returncode == 1:
                return
            self.assertEqual(r.returncode, 0, r.stderr)
            time.sleep(0.05)
        self.fail('The isolated panel test executors did not shut down; the temporary home cannot be deleted')

    def test_new_folder_and_registered_folder_use_the_real_request_without_implicit_git_actions(self):
        py = PanelTests.ui_python(self)
        self.addCleanup(self.executors_exit_wait)
        operation, empty = Path(self.temp.name) / 'operation', Path(self.temp.name) / 'empty-folder'
        operation.mkdir()
        empty.mkdir()
        (self.home / 'projects/empty-folder').mkdir()
        (self.home / 'projects/empty-folder/repo-path').write_text(str(empty))
        r = subprocess.run([str(py), '-c', END_TO_END, str(self.root / 'lib'), str(self.home), str(empty)], capture_output=True, text=True, timeout=300,
                           cwd=operation, env={**self.env, 'CADRE_NO_NET': '1'})
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        outcome = json.loads(r.stdout.splitlines()[-1])
        self.assertEqual((outcome['first'][0], outcome['first'][-1], outcome['second'][0]), ('here', 'fresh', 'project_ref'))
        self.assertEqual([b for b in outcome['notifications'] if 'saved' in b or 'QUEUE' in b].__len__(), 2, outcome['notifications'])
        records = {p.parent.name: Path(p.read_text().strip()) for p in (self.home / 'projects').glob('*/repo-path')}
        self.assertEqual(records['empty-folder'].resolve(), empty.resolve())
        self.assertNotIn('newproject', records, 'a new project_ref record is not created with implicit permission')
        self.assertFalse((operation / 'newproject').exists())
        self.assertEqual(list(empty.iterdir()), [], 'no Git repository or first commit is created in an existing empty folder')
        for project_ref, text in (('newproject', 'hello'), ('empty-folder', 'second')):
            run = self.store.list(project_ref)
            self.assertEqual(len(run), 1, project_ref)
            self.assertIn(text, Path(run[0]['prompt']).read_text())
            self.assertEqual(run[0]['planning']['status'], 'pending')
            self.assertEqual(run[0]['tasks'], {})
            self.assertEqual(run[0]['budget_spent_usd'], 0)
        self.assertTrue({'newproject', 'empty-folder'} <= set(outcome['listed']), outcome['listed'])


if __name__ == '__main__':
    unittest.main()
