#!/usr/bin/env python3
"""Adaptive splitting (G-E10): a lane that exhausts its chain at very hard level is split once into 2-3 sequential sub-tasks.
A sub-task is never split again, cannot leave the parent task's files and calls no helper; user decisions and environment failures are not solved by splitting."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

SPLIT_LEVELS = ('very-hard', 'ultra')
INDIVISIBLE_REASONS = ('no-capacity', 'disk-low', 'install-error', 'critical-file-violation', 'approval-waiting')
SUB_AT_LEAST, SUB_MOST = 2, 3


def _task(plan: dict[str, Any], gid: str) -> dict[str, Any] | None:
    return next((g for g in plan.get('tasks') or [] if g.get('id') == gid), None)


def splittable(plan: dict[str, Any], gid: str, level_entry_name: str, reason: str) -> tuple[bool, str]:
    g = _task(plan, gid)
    if level_entry_name not in SPLIT_LEVELS:
        return False, f"level {level_entry_name or '-'}: splitting only at very-hard+"
    if g is None:
        return False, 'task not in the plan'
    if g.get('parent'):
        return False, 'a sub-task is not split again'
    if g.get('split_into'):
        return False, 'task already split'
    if any((reason or '').startswith(n) for n in INDIVISIBLE_REASONS):
        return False, f'this reason is not solved by splitting: {reason[:80]}'
    return True, 'splittable'


def split_sample(plan: dict[str, Any], gid: str) -> dict[str, Any]:
    """Sample sub-plan put in the request; it MUST pass `validate_plan`."""
    g = _task(plan, gid) or {}
    return {'job_id': f'{gid}-divide', 'tasks': [{'id': 'a', 'definition': '...', 'files': [(g.get('files') or ['path'])[0]],
            'acceptance': 'measurable acceptance criterion', 'dependent': [], 'difficulty': 3, 'model': 'ds-pro', 'parallel_group': 1,
            'estimate_min': 20, 'estimate_usd': 0.5}]}


def split_prompt(plan: dict[str, Any], gid: str, reason: str, executor_items: str = '') -> str:
    g = _task(plan, gid) or {}
    sample = split_sample(plan, gid)
    item = f"\nTHE EXECUTOR'S OWN ITEM LIST (which parts are finished):\n{executor_items[:3000]}\n" if executor_items.strip() else ''
    return (
        "YOU ARE A TASK PLANNER. Review the real source read-only; write exactly one JSON block.\n"
        "The task below was tried until the executor chain was exhausted and could not be finished"
        f" (last reason: {reason or 'not given'}). SPLIT the task INTO SUB-TASKS: 2-3 small, SEQUENTIAL sub-tasks; each small enough "
        "to finish in a single session, the first one the most fundamental part. The worktree may hold a half-done product; the sub-tasks take over the existing state.\n"
        f"RULES: files ONLY from this list (no new files): {', '.join(g.get('files') or [])}; "
        f"out of scope: {', '.join(g.get('scope_outside') or []) or '-'}; every sub-task's acceptance criterion must be measurable.\n"
        f"TASK: {g.get('definition', '')}\nACCEPTANCE: {g.get('acceptance', '')}\n{item}"
        'Keep the source ids and the parent acceptance; do not create new scope, permissions or caps. '
        'Report routine choices under assumptions and critical uncertainty under decisions. '
        'If you give source_items, map every item bound to the parent task as pending with its own hash and the sub-task ids.\n'
        + 'SOURCE BINDINGS: ' + json.dumps({'job_kind': plan.get('job_kind'),
          'source_items': [item for item in plan.get('source_items', []) if gid in item['tasks']],
          'source_coverage': plan.get('source_coverage'),
          'assumptions': plan.get('assumptions', []), 'decisions': plan.get('decisions', [])}, ensure_ascii=False) + '\n'
        f"SCHEMA (sample, same fields): {json.dumps(sample, ensure_ascii=False)}\n"
    )


def sub_plan_merge(plan: dict[str, Any], gid: str, raw: str, root: str | Path | None = None) -> tuple[dict[str, Any], list[str]]:
    """Validates the planner output and appends it as sequential sub-tasks after the parent task; returns (new plan, sub ids)."""
    from dispatch_util import extract_plan, validate_plan
    from run_store import request_plan_coverage
    from outcome import merge_uncertainties
    parent = _task(plan, gid)
    if parent is None:
        raise ValueError('task not in the plan')
    sub_plan = extract_plan(raw)
    validate_plan(sub_plan, root)
    if sub_plan.get('job_kind', plan.get('job_kind')) != plan.get('job_kind'):
        raise ValueError('request-job-type-mismatch')
    sub = sub_plan['tasks']
    if not SUB_AT_LEAST <= len(sub) <= SUB_MOST:
        raise ValueError(f'sub-task count {len(sub)}; must be {SUB_AT_LEAST}-{SUB_MOST}')
    allowed = set(_normal_files(parent, root))
    ids = [f'{gid[:29]}-{i}' for i in range(1, len(sub) + 1)]
    new_ones = []
    for i, (id, g) in enumerate(zip(ids, sub)):
        files = _normal_files(g, root)
        outside = sorted(set(files) - allowed)
        if outside:
            raise ValueError(f'a sub-task asks for files outside the parent task: {", ".join(outside[:5])}')
        fresh = {k: v for k, v in g.items() if k not in ('id', 'dependent')}
        new_ones.append({**fresh, 'id': id, 'files': files, 'dependent': ids[i - 1:i], 'parent': gid})
    outcome = copy.deepcopy(plan)
    outcome.update(merge_uncertainties(plan, sub_plan))
    order = [t['id'] for t in outcome['tasks']].index(gid)
    outcome['tasks'][order]['split_into'] = ids
    outcome['tasks'][order + 1:order + 1] = new_ones
    sources = [item for item in plan.get('source_items', []) if gid in item['tasks']]
    if 'source_items' in sub_plan:
        if (not isinstance(sub_plan['source_items'], list)
                or any(not isinstance(item, dict) or item.get('status') != 'pending' for item in sub_plan['source_items'])):
            raise ValueError('request-backlog-coverage')
        request_plan_coverage(sub_plan, {'items': [{'id': item['id'], 'sha256': item['source_sha256'], 'truncated': False}
                                                for item in sources]})
        children = {item['id']: item['tasks'] for item in sub_plan['source_items']}
    else:
        children = {item['id']: [task['id'] for task in sub] for item in sources}
    renamed = {task['id']: name for task, name in zip(sub, ids)}
    for item in outcome.get('source_items', []):
        if gid in item['tasks']:
            item['tasks'] = [*item['tasks'], *[renamed[name] for name in children[item['id']]]]
    if isinstance(outcome.get('source_coverage'), dict):
        merged = []
        for task in outcome['tasks']:
            mapped = [item['id'] for item in outcome.get('source_items', []) if task['id'] in item['tasks']]
            if len(mapped) > 1:
                merged.append({'task': task['id'], 'source_items': mapped})
        outcome['source_coverage']['merged'] = merged
    if isinstance(outcome.get('merge_order'), list) and gid in outcome['merge_order']:
        j = outcome['merge_order'].index(gid)
        outcome['merge_order'][j + 1:j + 1] = ids
    validate_plan(outcome, root)
    return outcome, ids


def _normal_files(g: dict[str, Any], root: str | Path | None) -> list[str]:
    from dispatch_util import plan_path
    return list(dict.fromkeys(plan_path(n, root) for n in g.get('files') or []))


def _record(run_id: str):
    from run_store import RunStore
    store = RunStore()
    return store, store.read(run_id), store.path(run_id) / 'plan.json'


def _reason(store, run_id: str, gid: str) -> str:
    status = store.task_dir(run_id, gid) / 'status.txt'
    parts = status.read_text(errors='replace').split(None, 1) if status.exists() else []
    return parts[1].strip() if len(parts) > 1 else ''


if __name__ == '__main__':
    cmd, a = (sys.argv[1] if len(sys.argv) > 1 else ''), sys.argv[2:]
    if cmd == 'splittable' and len(a) >= 3:
        store, _k, path = _record(a[0])
        ok, reason = splittable(json.loads(path.read_text()), a[1], a[2], _reason(store, a[0], a[1]))
        print(reason)
        raise SystemExit(0 if ok else 1)
    if cmd == 'prompt' and len(a) >= 2:
        store, record, path = _record(a[0])
        items = Path(record.get('worktree') or '/absent') / '.tasks.json'
        print(split_prompt(json.loads(path.read_text()), a[1], _reason(store, a[0], a[1]),
                           items.read_text(errors='replace') if items.exists() else ''))
        raise SystemExit(0)
    if cmd == 'merge' and len(a) >= 3:
        from run_store import automatic_revision
        _store, _k, path = _record(a[0])
        try:
            fresh, ids = sub_plan_merge(json.loads(path.read_text()), a[1], sys.stdin.read(), a[2])
        except ValueError as exc:
            print(f'SPLIT: {exc}', file=sys.stderr)
            raise SystemExit(2)
        import os
        review = automatic_revision(_store, a[0], fresh, os.environ.get('CADRE_REAL_OPENCODE', '/opt/homebrew/bin/opencode'))
        if review.get('status') != 'PASS':
            print('SPLIT: current-plan-review-required', file=sys.stderr)
            raise SystemExit(78)
        print(' '.join(ids))
        raise SystemExit(0)
    if cmd == 'sub' and len(a) >= 2:
        _store, _k, path = _record(a[0])
        print(' '.join((_task(json.loads(path.read_text()), a[1]) or {}).get('split_into') or []))
        raise SystemExit(0)
    print(__doc__)
    raise SystemExit(2)
