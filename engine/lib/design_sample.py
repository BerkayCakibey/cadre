"""Sample data for the panel's design preview."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

import panel_data
import ui_messages


def run_id(ts: float) -> str:
    return time.strftime('%Y%m%d-%H%M%S', time.localtime(ts)) + '-' + uuid.uuid4().hex


def write(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.writing')
    temporary.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(temporary, path)
    return path


def add(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        f.write(line + '\n')


def stamp(ts: float) -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(['git', '-c', 'user.name=cadre', '-c', 'user.email=cadre@localhost', '-C', str(cwd), *args],
                       capture_output=True, text=True, timeout=30)
    return r.stdout.strip()


def tool(ts: float, entry_name: str, input: dict) -> dict:
    return {'type': 'tool_use', 'timestamp': int(ts * 1000), 'part': {'type': 'tool', 'tool': entry_name, 'state': {'status': 'completed', 'input': input}}}


def word(ts: float, text: str) -> dict:
    return {'type': 'text', 'timestamp': int(ts * 1000), 'part': {'type': 'text', 'text': text}}


def attempt(run: Path, gid: str, role: str, model: str, events: list[dict]) -> Path:
    path = run / 'tasks' / gid / 'attempts' / uuid.uuid4().hex
    write(path / 'attempt.json', {'attempt_id': path.name, 'run_id': run.name, 'task_id': gid, 'adapter': 'opencode', 'role': role, 'model': model,
                               'session_id': 'ses_' + uuid.uuid4().hex[:24]})
    write(path / 'stdout.jsonl', ''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in events))
    return path


def update(run: Path, **values: Any) -> dict:
    record = json.loads((run / 'run.json').read_text())
    record['updated'] = time.time()
    record.update(values)
    write(run / 'run.json', record)
    return record


def task_update(run: Path, gid: str, **values: Any) -> None:
    record = json.loads((run / 'run.json').read_text())
    record.setdefault('tasks', {}).setdefault(gid, {}).update(values, updated=time.time())
    record['updated'] = time.time()
    write(run / 'run.json', record)


def event(run: Path, ts: float, text: str) -> None:
    add(run / 'dispatch.log', f'{stamp(ts)} {text}')


def qa_outcome(run: Path, command: str, status: str, lines: list[str], sec: float) -> dict:
    output = write(run / 'tasks' / 'qa' / 'checks' / uuid.uuid4().hex / '1.log', '\n'.join(lines) + '\n')
    return {'status': status, 'reason': 'command-result', 'command': command,
            'attempts': [{'status': status, 'exit_code': 0 if status == 'PASS' else 1, 'seconds': sec, 'output': str(output)}]}


def repo_setup(home: Path, entry_name: str, files: dict[str, str]) -> tuple[Path, str]:
    repo = home / 'repos' / entry_name
    for path, content in files.items():
        write(repo / path, content)
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'first status')
    project_ref = home / 'projects' / entry_name
    for file, content in (('repo-path', f'{repo}\n'), ('base-branch', 'main\n'), ('PROJECT.md', f'# {entry_name}\n'), ('POLICY.md', f'# POLICY: {entry_name}\n')):
        write(project_ref / file, content)
    return repo, git(repo, 'rev-parse', 'HEAD')


def operation_tree(home: Path, repo: Path, rid: str, change: dict[str, str], message: str) -> Path:
    wt = home / 'worktrees' / repo.name / rid
    git(repo, 'worktree', 'add', '-q', '-b', f'cadre/{rid[:15]}', str(wt))
    for path, content in change.items():
        write(wt / path, content)
    git(wt, 'add', '.')
    git(wt, 'commit', '-qm', message)
    return wt


def run_setup(home: Path, project_ref: str, ts: float, prompt: str, **field_list: Any) -> Path:
    rid = run_id(ts)
    run = home / 'runs' / rid
    write(run / 'prompt.md', prompt + '\n')
    record = {'run_id': rid, 'project': project_ref, 'state': 'running', 'created': ts, 'updated': ts, 'prompt': str(run / 'prompt.md'),
             'mode': 'application', 'tasks': {}, 'budget_spent_usd': 0.0, 'budget_cap_usd': 2.0}
    record.update(field_list)
    write(run / 'run.json', record)
    return run


def plan_write(run: Path, points: int, rationale: str, tasks: list[dict]) -> None:
    write(run / 'plan.json', {'job_id': 'prompt', 'scope_points': points, 'scope_rationale': rationale, 'tasks': tasks,
                             'merge_order': [g['id'] for g in tasks]})
    for g in tasks:
        write(run / 'tasks' / g['id'] / 'prompt.md', '\n'.join([
            f"[QE-TASK v1]\n{'TASK'} ({g['id']}): {g['definition']}",
            f"{'FILES (touch ONLY these)'}: {', '.join(g['files'])}",
            f"{'ACCEPTANCE'}: {g['acceptance']}",
            f"{'OUT OF SCOPE (never touch)'}: {', '.join(g['scope_outside'])}"]) + '\n')


def sub_task(gid: str, definition: str, files: list[str], acceptance: str, dependent: list[str], difficulty: int, group: int, min: int, usd: float) -> dict:
    return {'id': gid, 'definition': definition, 'files': files, 'acceptance': acceptance, 'scope_outside': ['tests/', 'BACKLOG.md'], 'dependent': dependent,
            'difficulty': difficulty, 'model': 'ds-pro', 'parallel_group': group, 'estimate_min': min, 'estimate_usd': usd}


def assignment(run: Path, ts: float, project_ref: str, gid: str, model: str, status: str, min: int) -> None:
    add(run / 'assignment-history.jsonl', json.dumps({'ts': time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(ts)), 'project_ref': project_ref, 'job_id': 'prompt',
                                                   'task_id': gid, 'actual_model': model, 'start_model': model, 'tried_models': [model],
                                                   'last_model': model, 'status': status, 'duration_min': min}, ensure_ascii=False))


def spend(task: str, model: str, provider: str, usd: float, role: str = 'build') -> dict:
    return {'provider': provider, 'task': task, 'role': role, 'model': model, 'n': 3, 'reserved_usd': usd, 'actual_usd': usd, 'pending': 0}


OR_DS = 'openrouter/deepseek/deepseek-v4-pro'
OR_SOL = 'openrouter/openai/gpt-6.1-sol'
GO_GLM = 'opencode-go/glm-5.3'
GO_QWEN = 'opencode-go/qwen3.8-max'
OR_DS_MODEL, OR_SOL_MODEL = OR_DS.split('/', 1)[1], OR_SOL.split('/', 1)[1]


def coder_events(ts: float, file: str, test: str, thought: list[str]) -> list[dict]:
    return [tool(ts, 'read', {'filePath': file}), word(ts + 40, thought[0]), tool(ts + 70, 'edit', {'filePath': file}),
            tool(ts + 110, 'bash', {'command': test}), word(ts + 150, thought[1])]


def feedloom(home: Path, repo: Path, now_ts: float) -> dict:
    t0 = now_ts - 72 * 60
    jr = run_setup(home, 'feedloom', t0, "Implement the 'First 5 Cadre-ready items' section of BACKLOG.md.\nEach item lists its acceptance command. Do not change existing tests under tests/. Do not push or merge.",
                  budget_spent_usd=1.62, budget_cap_usd=10.0, project_active_feature='f3',
                  scope={'points': 78, 'level': 'very-hard', 'rationale': 'Five independent items, each with its own test. Run in parts.'},
                  tasks={'project-ref-list': {'executors': [], 'updated': t0 + 120}})
    attempt(jr, 'project-ref-list', 'plan', GO_GLM, [
        tool(t0 + 20, 'read', {'filePath': 'BACKLOG.md'}),
        word(t0 + 50, "Found the 'First 5 Cadre-ready items' section in BACKLOG.md. Five items, each with an acceptance command."),
        tool(t0 + 60, 'glob', {'pattern': 'tests/*.py'}), tool(t0 + 75, 'read', {'filePath': 'src/parser/date.py'}),
        word(t0 + 100, 'f4 report summary uses the date field. Made it depend on f3.'),
        word(t0 + 118, json.dumps({'features': ['f1', 'f2', 'f3', 'f4', 'f5']}))])
    features = [
        ('f1', 'Profile matching fix',
         'Profile keywords should match regardless of case and Turkish characters.', 'tests/test_profile.py', [], 35, 0.6),
        ('f2', 'Duplicate source filter',
         'The same entry from different sources should be listed once.',
         'tests/test_source.py', [], 40, 0.5),
        ('f3', 'Entry date parsing',
         "Relative dates ('3 days ago', 'yesterday') should become real dates.",
         'tests/test_date.py', [], 55, 1.1),
        ('f4', 'Report summary', 'Add a five-line summary to the top of the daily report.',
         'tests/test_report.py', ['f3'], 45, 0.7),
        ('f5', 'Export command', 'Add a feedloom export --csv command.',
         'tests/test_cli.py', [], 40, 0.6)]
    project_ref = {'id': jr.name[:24], 'request_summary': 'The first 5 Cadre items in BACKLOG.md, each with its own acceptance command.',
             'intermediate_branch': f'cadre/project-ref-{jr.name[16:24]}', 'status': 'running', 'spend_usd': 1.62, 'cap_usd': 10.0, 'children': [], 'features': []}
    for order, (fid, title, description, test, dependent, points, usd) in enumerate(features, 1):
        project_ref['features'].append({'id': fid, 'title': title, 'description': description, 'acceptance': f'uv run pytest {test} -q', 'dependent': dependent,
                                    'priority': order, 'status': 'waiting', 'attempt': 0, 'run': [], 'qa_command': f'uv run pytest {test} -q',
                                    'scope_points': points, 'estimate_usd': usd, 'risky': False})
    event(jr, t0, 'run starting: project mode')
    event(jr, t0 + 120, 'project plan accepted: 5 parts')
    sub_project_ref = f'feedloom--p{jr.name[16:24]}'
    steps = [
        ('f1', t0 + 180, [sub_task('g1', 'Rewrite the profile.py matching function with casefold and normalize',
                                    ['src/profile.py'], 'uv run pytest tests/test_profile.py -q', [], 2, 1, 10, 0.2),
                          sub_task('g2', 'Add Turkish character matching tests',
                                    ['tests/test_profile_tr.py'], 'uv run pytest tests/test_profile.py -q', ['g1'], 2, 2, 8, 0.15)],
         'tests/test_profile.py', ['14 passed in 0.42s']),
        ('f2', t0 + 1260, [sub_task('g1', 'Add an entry fingerprint filter to the shared layer (title, source, date)',
                                     ['src/sources/shared.py'], 'uv run pytest tests/test_source.py -q', [], 3, 1, 15, 0.3)],
         'tests/test_source.py', ['9 passed in 0.31s'])]
    for fid, ts, tasks, test, output in steps:
        c = run_setup(home, sub_project_ref, ts, f'{fid}: ' + next(o[2] for o in features if o[0] == fid),
                     project_ref_parent=jr.name, project_ref_feature=fid, plan_model=GO_GLM, budget_cap_usd=2.0,
                     scope={'points': 35, 'level': 'middle', 'rationale': 'A small change with tests.'})
        plan_write(c, 35, 'A small change with tests.', tasks)
        attempt(c, 'plan', 'plan', GO_GLM, [tool(ts + 10, 'read', {'filePath': 'BACKLOG.md'}),
                                           word(ts + 40, 'Small item. Split into subtasks.'),
                                           word(ts + 55, json.dumps({'tasks': [g['id'] for g in tasks]}))])
        breakdown = [spend('plan', 'glm-5.3', 'opencode-go', 0.05)]
        for g in tasks:
            attempt(c, g['id'], 'impl', OR_DS, coder_events(ts + 90, g['files'][0], g['acceptance'], [
                'Read the existing code. The change is in one function.',
                'Tests are green. Did not touch out-of-scope files.']))
            task_update(c, g['id'], state='produced', attempt_count=1, last_executor=OR_DS)
            assignment(c, ts + 400, c.name, g['id'], OR_DS, 'produced', 7)
            breakdown.append(spend(g['id'], OR_DS_MODEL, 'openrouter', 0.21))
        qa = qa_outcome(c, f'uv run pytest {test} -q', 'PASS', ['collected tests', *output], 4.2)
        task_update(c, 'qa', qa_result=qa)
        task_update(c, 'plan', executors=[])
        update(c, state='implemented', plan_ready=True, budget_spent_usd=round(sum(x['actual_usd'] for x in breakdown), 2), budget_breakdown=breakdown,
                 updated=ts + 620, final_result={'status': 'APPLIED', 'qa': qa, 'review': {'status': 'SKIPPED', 'reason': 'project-final-acceptance-waiting'},
                                                 'reason': 'project-final-acceptance-waiting'})
        event(c, ts + 30, f"plan accepted: {ui_messages.t('{n} tasks', n=len(tasks))}")
        for g in tasks:
            event(c, ts + 400, f"task {g['id']} produced")
        event(c, ts + 600, 'QA GATE: PASS')
        own = next(o for o in project_ref['features'] if o['id'] == fid)
        own.update(status='passed', attempt=1, run=[c.name])
        project_ref['children'].append({'feature': fid, 'run': c.name, 'status': 'implemented', 'success': True, 'reason': 'project-final-acceptance-waiting'})
        event(jr, ts + 620, f'{fid} passed')
    t3 = now_ts - 26 * 60
    c3 = run_setup(home, sub_project_ref, t3, 'f3: ' + features[2][2], project_ref_parent=jr.name, project_ref_feature='f3', plan_model=OR_SOL,
                  plan_ready=True, budget_cap_usd=2.0, scope={'points': 55, 'level': 'hard', 'rationale': 'The parser is new. Two files and a new test are needed.'})
    plan_write(c3, 55, 'The parser is new. Two files and a new test are needed.', [
        sub_task('g1', 'Write a relative date parser in date.py', ['src/parser/date.py'],
                  'uv run pytest tests/test_date.py -q', [], 3, 1, 15, 0.3),
        sub_task('g2', 'Wire the old parse call in shared.py to the new parser',
                  ['src/sources/shared.py'], 'uv run pytest tests/test_date.py -q', ['g1'], 3, 2, 12, 0.25),
        sub_task('g3', 'Write the relative date tests', ['tests/test_date.py'],
                  'uv run pytest tests/test_date.py -q', ['g1'], 2, 2, 10, 0.2)])
    attempt(c3, 'plan', 'plan', OR_SOL, [tool(t3 + 15, 'read', {'filePath': 'src/parser/date.py'}),
                                        word(t3 + 50, "Relative dates need today's date. A helper that pins the clock is required."),
                                        word(t3 + 70, json.dumps({'tasks': ['g1', 'g2', 'g3']}))])
    attempt(c3, 'g1', 'impl', OR_DS, coder_events(t3 + 90, 'src/parser/date.py', 'uv run pytest tests/test_date.py -q', [
        "Wrote the parser for '3 days ago' and 'yesterday'.",
        'ISO and 08 Oct formats work too.']))
    g2 = attempt(c3, 'g2', 'impl', OR_DS, [tool(now_ts - 420, 'read', {'filePath': 'src/sources/shared.py'}),
                                          word(now_ts - 360, 'The old parser in shared.py is called twice. I should remove it.'),
                                          tool(now_ts - 300, 'edit', {'filePath': 'src/sources/shared.py'}),
                                          tool(now_ts - 200, 'bash', {'command': 'uv run pytest tests/test_date.py -q'}),
                                          word(now_ts - 150, 'One test is red because of the time zone. I will pin Europe/Istanbul.')])
    task_update(c3, 'plan', executors=[])
    task_update(c3, 'g1', state='produced', attempt_count=1, last_executor=OR_DS)
    task_update(c3, 'g2', state='running', attempt_count=1, last_executor=OR_DS)
    assignment(c3, t3 + 600, c3.name, 'g1', OR_DS, 'produced', 9)
    update(c3, budget_spent_usd=0.31, budget_breakdown=[spend('plan', OR_SOL_MODEL, 'openrouter', 0.08),
                                                           spend('g1', OR_DS_MODEL, 'openrouter', 0.16),
                                                           spend('g2', OR_DS_MODEL, 'openrouter', 0.07)])
    event(c3, t3 + 60, 'plan accepted: 3 tasks')
    event(c3, t3 + 600, 'task g1 produced')
    event(c3, t3 + 620, 'PARALLEL: g2 and g3 share a group, g2 started')
    project_ref['features'][2].update(attempt=1, run=[c3.name])
    project_ref['last_child'] = c3.name
    write(jr / 'project_mode.json', project_ref)
    event(jr, t3, 'f3 started')
    update(jr)
    return {'jr': jr, 'c3': c3, 'live_g2': g2, 'sub_project_ref': sub_project_ref}


def notes(home: Path, repo: Path, now_ts: float) -> dict:
    t0 = now_ts - 9 * 60
    nl = run_setup(home, 'notes-lab', t0, 'Add CSV export to notes.py and write 6 tests.',
                  plan_model=OR_SOL, plan_ready=True, budget_cap_usd=2.0, qa={'source': 'project', 'kind': 'project', 'command': 'make check'},
                  scope={'points': 30, 'level': 'middle', 'rationale': 'A new function and a test file.'})
    tasks = [sub_task('g1', 'Add export_csv to notes.py', ['notes.py'],
                          'make check', [], 2, 1, 8, 0.12),
                sub_task('g2', 'Write 6 tests in tests/test_csv.py', ['tests/test_csv.py'],
                          'make check', [], 2, 1, 6, 0.09)]
    plan_write(nl, 30, 'A new function and a test file.', tasks)
    attempt(nl, 'plan', 'plan', OR_SOL, [tool(t0 + 10, 'read', {'filePath': 'notes.py'}),
                                        word(t0 + 30, 'Two independent subtasks. They can run in parallel.')])
    for g in tasks:
        attempt(nl, g['id'], 'impl', OR_DS, coder_events(t0 + 60, g['files'][0], 'make check', [
            'Read the file. Appending the new function.',
            'make check is green locally.']))
        task_update(nl, g['id'], state='produced', attempt_count=1, last_executor=OR_DS)
        assignment(nl, t0 + 300, 'notes-lab', g['id'], OR_DS, 'produced', 4)
    task_update(nl, 'plan', executors=[])
    task_update(nl, 'qa', updated=now_ts - 60)
    update(nl, budget_spent_usd=0.29, budget_breakdown=[spend('plan', OR_SOL_MODEL, 'openrouter', 0.08),
                                                           spend('g1', OR_DS_MODEL, 'openrouter', 0.12),
                                                           spend('g2', OR_DS_MODEL, 'openrouter', 0.09)])
    event(nl, t0 + 40, 'plan accepted: 2 tasks')
    event(nl, t0 + 300, 'task g1 produced')
    event(nl, t0 + 310, 'task g2 produced')
    event(nl, now_ts - 60, 'QA GATE started: make check')
    return {'nl': nl, 'nl_repo': repo, 'nl_base': git(repo, 'rev-parse', 'HEAD')}


def history(home: Path, repos: dict, now_ts: float) -> None:
    repo, base = repos['notes-lab']
    h1 = run_setup(home, 'notes-lab', now_ts - 3 * 3600, 'Add tag search to notes (filter by #tag).',
                  plan_model=OR_SOL, plan_ready=True, budget_cap_usd=2.0, source_sha=base,
                  scope={'points': 28, 'level': 'middle', 'rationale': 'One function and a test.'})
    plan_write(h1, 28, 'One function and a test.', [
        sub_task('g1', 'Add label_intermediate to notes.py', ['notes.py'], 'make check', [], 2, 1, 8, 0.1)])
    attempt(h1, 'g1', 'impl', OR_DS, coder_events(now_ts - 3 * 3600 + 60, 'notes.py', 'make check', [
        'Tags start with #. A simple filter is enough.',
        'make check is green.']))
    attempt(h1, 'quality', 'quality', GO_QWEN, [tool(now_ts - 2.6 * 3600, 'bash', {'command': f'git diff {base[:7]}..HEAD --stat'}),
                                             word(now_ts - 2.6 * 3600 + 60, 'Only notes.py and a new test changed. Acceptance items are met.')])
    wt = operation_tree(home, repo, h1.name, {'notes.py': repo.joinpath('notes.py').read_text() + '\n\ndef label_intermediate(notes, label):\n'
                                                                                            '    return [n for n in notes if label in n.split()]\n',
                                             'tests/test_label.py': 'from notes import label_intermediate\n\n\ndef test_label():\n'
                                                                     "    assert label_intermediate(['a #x', 'b'], '#x') == ['a #x']\n"},
                        'tag search')
    qa = qa_outcome(h1, 'make check', 'PASS', ['ruff check . ok', 'mypy ok', '33 passed in 1.12s'], 6.8)
    write(h1 / 'tasks' / 'quality' / 'review-1.txt', 'Changes are in two files. Existing tests untouched.\n\nVerdict: PASS\n')
    task_update(h1, 'plan', executors=[])
    task_update(h1, 'g1', state='produced', attempt_count=1, last_executor=OR_DS)
    task_update(h1, 'qa', qa_result=qa)
    task_update(h1, 'quality', executors=[])
    assignment(h1, now_ts - 2.8 * 3600, 'notes-lab', 'g1', OR_DS, 'produced', 6)
    update(h1, state='completed', worktree=str(wt), budget_spent_usd=0.34, updated=now_ts - 2.5 * 3600, final_result={
        'status': 'PASS', 'qa': qa, 'review': {'status': 'PASS', 'verdict': 'PASS', 'model': GO_QWEN, 'independent': True, 'writers': ['deepseek-v4-pro'],
                                               'excluded_writers': [OR_DS], 'repairs': 0, 'repair_limit': 1, 'acceptance': [
                                                   {'id': 'original_request', 'status': 'PASS', 'evidence': "git diff changed only notes.py and tests/test_label.py. label_intermediate filters '#x' correctly."},
                                                   {'id': 'g1', 'status': 'PASS', 'evidence': 'make check 33 tests green.'}]}})
    event(h1, now_ts - 3 * 3600 + 30, 'plan accepted: 1 task')
    event(h1, now_ts - 2.6 * 3600, 'REVIEWER: PASS (qwen3.8-max, independent)')
    event(h1, now_ts - 2.5 * 3600, 'ACCEPTANCE RESULT: PASS · DONE')
    jr_repo, jr_base = repos['feedloom']
    h2 = run_setup(home, 'feedloom', now_ts - 6 * 3600, 'Write a feedloom architecture summary report (module map, risks, suggestions).',
                  mode='report', plan_model=OR_SOL, source_sha=jr_base, budget_cap_usd=2.0,
                  report={'path': 'docs/reviews/architecture-summary.md', 'sections': ['Summary', 'Module map',
                                                                            'Risks', 'Suggestions']})
    wt2 = operation_tree(home, jr_repo, h2.name, {'docs/reviews/architecture-summary.md': '# Architecture summary\n\n## Summary\nSource adapters pass through a shared layer.\n\n## Module map\n- src/sources\n- src/parser\n\n## Risks\n- Date parsing differs per source.\n\n## Suggestions\n- A shared parser.\n'}, 'architecture summary report')
    update(h2, state='completed', worktree=str(wt2), budget_spent_usd=0.83, updated=now_ts - 5.4 * 3600, delivery_merge=stamp(now_ts - 5 * 3600),
             final_result={'status': 'PASS', 'qa': {'status': 'PASS', 'command': 'report check'},
                           'review': {'status': 'PASS', 'verdict': 'PASS', 'model': GO_QWEN, 'independent': True}})
    b_repo, b_base = repos['demo-lab']
    h3 = run_setup(home, 'demo-lab', now_ts - 5 * 3600, 'Use Europe/Istanbul in the price log.',
                  plan_model=OR_SOL, plan_ready=True, source_sha=b_base, budget_cap_usd=2.0, qa={'command': 'pytest -q'})
    plan_write(h3, 25, 'One file.', [sub_task('g1', 'price.py time zone fix', ['price.py'],
                                                              'pytest -q', [], 2, 1, 6, 0.08)])
    attempt(h3, 'g1', 'impl', OR_DS, coder_events(now_ts - 5 * 3600 + 60, 'price.py', 'pytest -q', [
        'Fixing it with zoneinfo.', 'Two tests are still red.']))
    qa3 = qa_outcome(h3, 'pytest -q', 'FAIL', ['FAILED tests/test_price.py::test_night_half', 'FAILED tests/test_price.py::test_write_hour',
                                              '2 failed, 11 passed in 0.9s'], 3.1)
    task_update(h3, 'g1', state='produced', attempt_count=2, last_executor=OR_DS)
    task_update(h3, 'qa', qa_result=qa3)
    task_update(h3, 'correction1', state='failed', attempt_count=1, last_executor=OR_DS, reason='test still red')
    attempt(h3, 'correction1', 'impl', OR_DS, [word(now_ts - 4.6 * 3600, 'DST transition must be handled.')])
    update(h3, state='failed', reason='qa-red: 2 tests failed', budget_spent_usd=0.21, updated=now_ts - 4.5 * 3600,
             final_result={'status': 'FAIL', 'qa': qa3})
    event(h3, now_ts - 4.6 * 3600, 'QA GATE: FAIL (2 tests)')
    h4 = run_setup(home, 'notes-lab', now_ts - 26 * 3600, 'Add setup steps to README.',
                  plan_model=GO_GLM, plan_ready=True, budget_cap_usd=1.0)
    update(h4, state='completed', budget_spent_usd=0.12, updated=now_ts - 25.8 * 3600, delivery_push=stamp(now_ts - 25 * 3600),
             final_result={'status': 'PASS', 'qa': {'status': 'PASS'}, 'review': {'status': 'PASS', 'verdict': 'PASS', 'model': GO_QWEN}})
    h5 = run_setup(home, 'feedloom', now_ts - 28 * 3600, 'Rescan all sources and list broken ones.',
                  budget_cap_usd=2.0)
    update(h5, state='cancelled', reason='cancelled by user', updated=now_ts - 27.9 * 3600)


def ledgers(home: Path, now_ts: float) -> None:
    schema = ('create table reservation(id text primary key, provider text not null, period text not null, created real not null, '
            'reserved integer not null, actual integer, receipt text unique, metadata text not null)')
    (home / 'chain').mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(home / 'chain' / 'request-budget.db')) as con, con:
        con.execute(schema)
        lines = [('openrouter', OR_DS_MODEL, 'g2', 21000, 600), ('openrouter', OR_DS_MODEL, 'g1', 34000, 1500),
                    ('openrouter', OR_SOL_MODEL, 'plan', 80000, 1700), ('openrouter', OR_DS_MODEL, 'g1', 47000, 2400),
                    ('openrouter', OR_DS_MODEL, 'g2', 29000, 3300), ('opencode-go', 'qwen3.8-max', 'quality', 120000, 9300),
                    ('opencode-go', 'glm-5.3', 'plan', 50000, 12000)]
        for day, amount in enumerate((180000, 420000, 90000, 610000, 300000, 740000), 1):
            lines.append(('openrouter', OR_DS_MODEL, 'g1', amount, day * 86400 + 3600))
        for i, (provider, model, task, amount, once) in enumerate(lines):
            con.execute('insert into reservation values (?,?,?,?,?,?,?,?)', (uuid.uuid4().hex, provider, time.strftime('%Y-%m-%d'), now_ts - once,
                                                                            amount, amount, f'receipt-{i}', json.dumps({'model': model, 'task_id': task})))
    with closing(sqlite3.connect(home / 'chain' / 'monthly-budget.db')) as con, con:
        con.execute(schema)
        for i, (provider, amount) in enumerate((('openrouter', 3_200_000), ('opencode', 220_000), ('opencode-go', 2_100_000))):
            con.execute('insert into reservation values (?,?,?,?,?,?,?,?)', (uuid.uuid4().hex, provider, time.strftime('%Y-%m'), now_ts - 3600 * (i + 1),
                                                                            amount, amount, f'monthly-{i}', '{}'))
    history = [{'ts': now_ts - (23 - i) * 3600, 'rolling': (17 * i) % 70, 'weekly': 70 + i * 13 // 23, 'monthly': 80 + i * 8 // 23} for i in range(24)]
    write(home / 'chain' / 'go-window.json', {'ts': now_ts, 'rolling': 57, 'weekly': 83, 'monthly': 88, 'history': history, 'resets': {
        'rolling': time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(now_ts + 2 * 3600)),
        'weekly': time.strftime('%Y-%m-%dT00:00:00.000Z', time.gmtime(now_ts + 4 * 86400)),
        'monthly': time.strftime('%Y-%m-02T20:39:30.000Z', time.gmtime(now_ts + (32 - time.gmtime(now_ts).tm_mday) * 86400))}})
    write(home / 'chain' / 'groq-day-counter.txt', f"{time.strftime('%Y-%m-%d')} 4\n")


def budget_probe(home: Path) -> tuple[dict, float]:
    return ({'ts': time.strftime('%Y-%m-%dT00:00:00'), 'today': time.strftime('%Y-%m-%d'),
             'go': {'own': {'rolling': 57, 'weekly': 83, 'monthly': 88}, 'available': 5, 'window_open': True},
             'or': {'remaining': 61.72, 'available': 61.72}, 'zen': {'remaining': 4.47}, 'claude': {'used7': 71, 'used5': 4, 'cap': 77.5}}, 40.0)


def chain_probe(role: str, b: dict | None = None, status: dict | None = None) -> tuple[list, list]:
    import model_router
    candidates = list(dict.fromkeys(m for candidate in model_router.ROLES.get(role, []) for m in candidate.split('|')))
    reasons = {'opencode-go': "Go's cadre share is used up, it reopens on the 2nd",
                'claude': 'Claude is off inside cadre'}
    chain = [m for m in candidates if m.split('/')[0] not in reasons]
    return chain, [f"{m}: {reasons[m.split('/')[0]]}" for m in candidates if m.split('/')[0] in reasons]


def approval_probe(k: dict) -> bool:
    fr = k.get('final_result') or {}
    return str(fr.get('status')).upper() == 'PASS' and str((fr.get('review') or {}).get('status')).upper() == 'PASS'


def probes_setup() -> None:
    panel_data.AT_END.update(budget=budget_probe, chain=chain_probe, approval=approval_probe, claude_closed=lambda: True,
                   failure=lambda: {OR_SOL: {'error': 1, 'last': 'today 14:02 timeout, moved to the next link'}})


def probes_remove() -> None:
    panel_data.AT_END.clear()


def setup(home: Path | None = None) -> tuple[Path, dict]:
    home = Path(home or tempfile.mkdtemp(prefix='cadre-design-'))
    now_ts = time.time()
    repos = {
        'feedloom': repo_setup(home, 'feedloom', {'README.md': '# feedloom\n', 'src/parser/date.py': 'def parse_date(text):\n    return text\n',
                                                  'src/sources/shared.py': 'from src.parser.date import parse_date\n', 'BACKLOG.md': '# BACKLOG\n'}),
        'notes-lab': repo_setup(home, 'notes-lab', {'notes.py': 'def notes():\n    return []\n', 'README.md': '# notes-lab\n',
                                                    'Makefile': 'check:\n\tpytest -q\n'}),
        'demo-lab': repo_setup(home, 'demo-lab', {'price.py': 'def price():\n    return 0\n', 'README.md': '# demo-lab\n'})}
    history(home, repos, now_ts)
    scene: dict[str, Any] = {'home': home}
    scene.update(feedloom(home, repos['feedloom'][0], now_ts))
    scene.update(notes(home, repos['notes-lab'][0], now_ts))
    ledgers(home, now_ts)
    return home, scene


def clean(home: Path) -> None:
    if home.name.startswith('cadre-design-'):
        shutil.rmtree(home, ignore_errors=True)


CODER_LINES = [
    ('tool', 'edit', {'filePath': 'src/parser/date.py'}),
    ('word', 'Pinned Europe/Istanbul. Running the test again.'),
    ('tool', 'bash', {'command': 'uv run pytest tests/test_date.py -q'}),
    ('word', 'All four tests are green. No out-of-scope file touched.'),
    ('tool', 'read', {'filePath': 'tests/test_date.py'}),
    ('word', "Edge case: 'yesterday' right after midnight. Adding it."),
]
JUDGE_LINES = [
    ('tool', 'bash', {'command': 'git diff --stat'}),
    ('word', 'Changes are in two files. Existing tests untouched.'),
    ('tool', 'read', {'filePath': 'notes.py'}),
    ('tool', 'bash', {'command': 'make check'}),
    ('word', 'Acceptance items are met. Verdict: PASS.'),
]
PLANNER_LINES = [
    ('tool', 'read', {'filePath': 'src/report_mode.py'}),
    ('word', 'The date field is ready for the summary. Two subtasks are enough.'),
    ('tool', 'glob', {'pattern': 'tests/test_report*.py'}),
]


class Simulation:
    def __init__(self, scene: dict) -> None:
        self.scene = scene
        self.tick = 0
        self.live: list[tuple[Path, list]] = [(scene['live_g2'], CODER_LINES)]
        self.orders = {3: self.nl_test_passed, 6: self.c3_g2_finished, 9: self.nl_review_finished, 12: self.c3_g3_finished, 15: self.c3_test_passed}

    def step(self) -> None:
        self.tick += 1
        step = self.orders.get(self.tick)
        if step:
            step()
        for attempt_directory, lines in self.live:
            round, *content = lines[self.tick % len(lines)]
            event_ = tool(time.time(), content[0], content[1]) if round == 'tool' else word(time.time(), content[0])
            add(attempt_directory / 'stdout.jsonl', json.dumps(event_, ensure_ascii=False))
        for key in ('c3', 'jr', 'nl'):
            run = self.scene.get(key)
            if run and json.loads((run / 'run.json').read_text()).get('state') not in panel_data.TERMINAL:
                update(run)

    def is_judge(self, attempt_directory: Path) -> bool:
        return attempt_directory.parent.parent.name == 'quality'

    def nl_test_passed(self) -> None:
        nl = self.scene['nl']
        qa = qa_outcome(nl, 'make check', 'PASS', ['ruff check . ok', 'mypy ok', '38 passed in 1.31s'], 7.4)
        task_update(nl, 'qa', qa_result=qa)
        task_update(nl, 'quality', executors=[])
        judge = attempt(nl, 'quality', 'quality', OR_SOL, [tool(time.time(), 'bash', {'command': 'git diff --stat'})])
        self.live.append((judge, JUDGE_LINES))
        event(nl, time.time(), 'QA GATE: PASS (make check)')
        event(nl, time.time(), 'REVIEWER started: openai/gpt-6.1-sol')

    def nl_review_finished(self) -> None:
        nl = self.scene['nl']
        record = json.loads((nl / 'run.json').read_text())
        repo = self.scene['nl_repo']
        wt = operation_tree(self.scene['home'], repo, nl.name, {
            'notes.py': repo.joinpath('notes.py').read_text() + '\n\ndef export_csv(notes, path):\n    import csv\n'
                         "    with open(path, 'w', newline='') as f:\n        csv.writer(f).writerows([[n] for n in notes])\n",
            'tests/test_csv.py': 'def test_csv():\n    assert True\n'}, 'CSV export')
        write(nl / 'tasks' / 'quality' / 'review-1.txt', 'Changes are in two files.\n\nVerdict: PASS\n')
        self.live = [c for c in self.live if not self.is_judge(c[0])]
        update(nl, state='completed', worktree=str(wt), source_sha=self.scene['nl_base'], budget_spent_usd=0.38, final_result={
            'status': 'PASS', 'qa': record['tasks']['qa']['qa_result'], 'review': {
                'status': 'PASS', 'verdict': 'PASS', 'model': OR_SOL, 'independent': True, 'writers': ['deepseek-v4-pro'],
                'excluded_writers': [OR_DS], 'repairs': 0, 'repair_limit': 1, 'acceptance': [
                    {'id': 'original_request', 'status': 'PASS', 'evidence': 'export_csv added, 6 tests green.'}]}})
        event(nl, time.time(), 'ACCEPTANCE RESULT: PASS · DONE')

    def c3_g2_finished(self) -> None:
        c3 = self.scene['c3']
        task_update(c3, 'g2', state='produced')
        assignment(c3, time.time(), c3.name, 'g2', OR_DS, 'produced', 8)
        fresh = attempt(c3, 'g3', 'impl', OR_DS, [tool(time.time(), 'read', {'filePath': 'tests/test_date.py'})])
        task_update(c3, 'g3', state='running', attempt_count=1, last_executor=OR_DS)
        self.live = [(fresh, CODER_LINES)] + [c for c in self.live if self.is_judge(c[0])]
        event(c3, time.time(), 'task g2 produced')
        event(c3, time.time(), 'task g3 started')

    def c3_g3_finished(self) -> None:
        c3 = self.scene['c3']
        task_update(c3, 'g3', state='produced')
        assignment(c3, time.time(), c3.name, 'g3', OR_DS, 'produced', 6)
        task_update(c3, 'qa', updated=time.time())
        self.live = [c for c in self.live if self.is_judge(c[0])]
        event(c3, time.time(), 'task g3 produced')
        event(c3, time.time(), 'QA GATE started: uv run pytest tests/test_date.py -q')

    def c3_test_passed(self) -> None:
        c3, jr = self.scene['c3'], self.scene['jr']
        qa = qa_outcome(c3, 'uv run pytest tests/test_date.py -q', 'PASS', ['4 passed in 0.38s'], 3.9)
        task_update(c3, 'qa', qa_result=qa)
        update(c3, state='implemented', final_result={'status': 'APPLIED', 'qa': qa, 'review': {'status': 'SKIPPED'},
                                                       'reason': 'project-final-acceptance-waiting'})
        project_ref = json.loads((jr / 'project_mode.json').read_text())
        project_ref['features'][2]['status'] = 'passed'
        project_ref['children'].append({'feature': 'f3', 'run': c3.name, 'status': 'implemented', 'success': True, 'reason': 'project-final-acceptance-waiting'})
        c4 = run_setup(self.scene['home'], self.scene['sub_project_ref'], time.time(), 'f4: ' + project_ref['features'][3]['description'],
                      project_ref_parent=jr.name, project_ref_feature='f4', budget_cap_usd=2.0)
        planner = attempt(c4, 'plan', 'plan', OR_SOL, [tool(time.time(), 'read', {'filePath': 'BACKLOG.md'}),
                                                        word(time.time(), 'f4 uses the date field from f3. f3 is done, starting.')])
        project_ref['features'][3].update(run=[c4.name], attempt=1)
        project_ref['last_child'] = c4.name
        write(jr / 'project_mode.json', project_ref)
        update(jr, project_active_feature='f4', budget_spent_usd=2.01)
        self.scene['c3'] = c4
        self.live = [(planner, PLANNER_LINES)] + [c for c in self.live if self.is_judge(c[0])]
        event(c3, time.time(), 'QA GATE: PASS')
        event(jr, time.time(), 'f3 passed · f4 started')
