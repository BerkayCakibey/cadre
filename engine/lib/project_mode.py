#!/usr/bin/env python3
"""Project mode (G-P): an ultra-level job is split into a feature list; each feature accumulates as a child run on an intermediate branch
(`cadre/project-ref-<id>`), the canonical branch is not touched. The first layer is a pure core (no model/network/git)."""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

FEATURE_CAP = 40          # cap on pending features (per list)
ATTEMPT_CAP = 2            # on the second failure the feature gets stuck, the project continues
CONSECUTIVE_FAILED_CAP = 3  # this many failed features in a row = no progress
PROGRESS_ABSENT_SEC = 86400      # stop if no new feature passed within 24 h
DURATION_CAP_SEC = 7 * 86400   # project time horizon
FINAL_ACCEPTANCE_ROUND = 2
PLAN_REQUEST_CAP = 150_000
DOCUMENT_EXTENSIONS = ('.md', '.markdown', '.txt', '.rst', '.adoc')
SENSITIVE_PATH = re.compile(r'(^|/)\.env|secret|credential|passw|token|private|id_rsa|id_ed25519', re.I)
NEGATIVE = re.compile(r"\b(?:do not|don't|never|ignore|exclud(?:e|ing)|except(?:ing)?|skip|leave (?:\S+ )?alone)\b", re.I | re.ASCII)
EXTRA_FILE_CAP = 40_000
CONTEXT_CAP = 6000
ID = re.compile(r'[a-z0-9_-]{1,32}')


STEP_POLICY = {'version': 1, 'mode': 'step'}
STEP_INSTRUCTIONS = ('SUPERVISION: jobs run one after another and operator verification is awaited after every feature. '
                     'Do not merge different backlog items into one task; keep every source id as a separate task/acceptance. '
                     'One card may be split into several small tasks if needed; the whole card closes only with full acceptance.\n')


def supervision_policy(home: Path, project: str) -> dict[str, Any] | None:
    from runtime_sync import regular
    data = regular(home.resolve() / 'projects' / project / 'supervision.json')
    if data is None:
        return None
    try:
        policy = json.loads(data) if len(data) <= 4096 else None
    except (ValueError, UnicodeError):
        policy = None
    if policy != STEP_POLICY or type(policy.get('version')) is not int:
        raise ValueError('project-supervision-policy-invalid')
    return dict(STEP_POLICY)


def supervision_waiting(store: Any, record: dict[str, Any]) -> bool:
    from run_store import bounded_json
    if record.get('supervision') is None:
        return False
    if record['supervision'] != STEP_POLICY or type(record['supervision'].get('version')) is not int:
        raise ValueError('project-supervision-policy-invalid')
    path = store.path(record['run_id']) / 'project_mode.json'
    expected = record.get('reason') in ('project-ref-stopped: project-step-verification-required', 'project-step-resume-required')
    if not path.exists():
        if expected or record.get('project_deadline') is not None or path.is_symlink():
            raise ValueError('project-supervision-checkpoint-invalid')
        return False
    try:
        state = bounded_json(path)
    except (OSError, ValueError):
        raise ValueError('project-supervision-checkpoint-invalid') from None
    if (not _fresh_status(record['run_id'], '', 0).keys() <= state.keys()
            or not isinstance(state['children'], list) or not isinstance(state['features'], list)
            or any(not isinstance(f, dict) for f in state['features'])):
        raise ValueError('project-supervision-checkpoint-invalid')
    checkpoint = state.get('checkpoint')
    if checkpoint is None:
        if expected or state.get('children') or any(f.get('run') for f in state.get('features', [])):
            raise ValueError('project-supervision-checkpoint-invalid')
        return False
    if (not isinstance(checkpoint, dict) or checkpoint.get('status') not in ('waiting', 'verified', 'retry-approved')
            or not isinstance(checkpoint.get('id'), str) or not isinstance(checkpoint.get('success'), bool)):
        raise ValueError('project-supervision-checkpoint-invalid')
    return checkpoint['status'] == 'waiting'


def verify_step(store: Any, run_id: str, checkpoint_id: str, action: str = 'continue', note: str = '') -> dict[str, Any]:
    """N-RUN01/N-QA05: operator verifies one bound result; spending and the original deadline remain fixed."""
    from run_store import (
        TERMINAL,
        FileLock,
        atomic_json,
        bounded_json,
        intervention_add,
        plan_decisions,
        plan_digest,
        plan_hold,
    )
    from outcome import approved, implemented
    if os.environ.get('CADRE_RUN_ID') or action not in ('continue', 'retry') or len(note) > 4000:
        raise ValueError('project-step-operator-command-required')
    owner = FileLock(store.path(run_id) / '.owner.lock')
    if not owner.acquire(blocking=False):
        raise ValueError('project-step-owner-active')
    try:
        with FileLock(store.path(run_id) / '.record.lock'):
            record = store.read(run_id)
            if (record.get('supervision') != STEP_POLICY or record['state'] in TERMINAL or plan_hold(record)
                    or plan_decisions(record) or (store.path(run_id) / 'cancel.flag').exists()):
                raise ValueError('project-step-release-blocked')
            supervision_waiting(store, record)
            path = store.path(run_id) / 'project_mode.json'
            state = bounded_json(path)
            checkpoint = state.get('checkpoint') or {}
            if checkpoint.get('id') != checkpoint_id:
                raise ValueError('project-step-stale-checkpoint')
            if checkpoint.get('status') != 'waiting':
                if checkpoint.get('status') == ('verified' if action == 'continue' else 'retry-approved'):
                    return checkpoint
                raise ValueError('project-step-release-blocked')
            child = store.read(checkpoint['run_id'])
            if (child.get('project_ref_parent') != run_id or checkpoint.get('final_digest') != plan_digest(child.get('final_result') or {})):
                raise ValueError('project-step-result-changed')
            if action == 'continue':
                if not checkpoint.get('success') or not (approved(child) or implemented(child)):
                    raise ValueError('project-step-acceptance-required')
                import sqlite3
                from contextlib import closing
                ledger = store.home / 'chain/request-budget.db'
                if ledger.is_symlink() or ledger.parent.is_symlink():
                    raise ValueError('project-step-unsettled-cost')
                if ledger.exists():
                    with closing(sqlite3.connect(ledger.resolve().as_uri() + '?mode=ro', uri=True)) as con:
                        pending = con.execute("select count(*) from reservation where actual is null "
                                              "and json_extract(metadata,'$.run_id')=? "
                                              "and coalesce(json_extract(metadata,'$.assurance'),0)!=1", (child['run_id'],)).fetchone()[0]
                    if pending:
                        raise ValueError('project-step-unsettled-cost')
            elif checkpoint.get('success') or not note.strip():
                raise ValueError('project-step-retry-evidence-required')
            elif checkpoint['feature'] != 'final-acceptance':
                feature = next((f for f in state['features'] if f['id'] == checkpoint['feature']), None)
                if feature is None or feature['status'] != 'waiting':
                    raise ValueError('project-plan-review-required')
                state['retry_feature'] = feature['id']
            checkpoint = {**checkpoint, 'status': 'verified' if action == 'continue' else 'retry-approved',
                          'verified_at': time.time(), 'note': note}
            state['checkpoint'] = checkpoint
            intervention_add(record, 'project-step-verify', {'checkpoint': checkpoint_id, 'action': action,
                                                       'child': child['run_id'], 'feature': checkpoint['feature'], 'note': note})
            record.update(state='accepted', reason='project-step-resume-required', owner_pid=None, updated=time.time())
            atomic_json(store.path(run_id) / 'run.json', record)
            atomic_json(path, state)
            return checkpoint
    finally:
        owner.close()


def _raw_plan(raw: str) -> dict[str, Any]:
    """Returns the single, unambiguous project plan in the text."""
    solver = json.JSONDecoder()
    from outcome import validate_uncertainties
    found = []
    i = raw.find('{')
    while i != -1:
        try:
            value, last = solver.raw_decode(raw, i)
        except ValueError:
            i = raw.find('{', i + 1)
            continue
        if isinstance(value, dict) and 'features' in value:
            validate_uncertainties(value)
            if not isinstance(value['features'], list):
                raise ValueError('feature list (features) not found')
            found.append(value)
        i = raw.find('{', last)
    if not found:
        raise ValueError('feature list (features) not found')
    if len(found) != 1:
        raise ValueError('one unambiguous valid project plan required')
    return found[0]


def _raw_list(raw: str) -> list[Any]:
    return _raw_plan(raw)['features']


def _verify(items_list: list[Any], known: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    if not isinstance(items_list, list) or not items_list:
        raise ValueError('feature list is empty')
    if len(items_list) > FEATURE_CAP:
        raise ValueError(f'feature count {len(items_list)} > {FEATURE_CAP}')
    clean: list[dict[str, Any]] = []
    for f in items_list:
        if not isinstance(f, dict) or not isinstance(f.get('id'), str) or not ID.fullmatch(f['id']):
            raise ValueError('invalid feature id')
        for field in ('title', 'description', 'acceptance'):
            if not isinstance(f.get(field), str) or not f[field].strip() or len(f[field]) > 8000:
                raise ValueError(f"{f['id']}: {field} is empty or invalid")
        priority = f.get('priority', 3)
        if type(priority) is not int or not 1 <= priority <= 5:
            raise ValueError(f"{f['id']}: priority must be 1-5")
        dependent = f.get('dependent', [])
        if not isinstance(dependent, list) or any(not isinstance(b, str) for b in dependent):
            raise ValueError(f"{f['id']}: dependent must be a list")
        if not _acceptance_command(f) or len(f['qa_command']) > 2000:
            raise ValueError(f"{f['id']}: qa_command is empty or invalid")
        clean.append({'id': f['id'], 'title': f['title'].strip(), 'description': f['description'].strip(), 'acceptance': f['acceptance'].strip(),
                      'dependent': list(dict.fromkeys(dependent)), 'priority': priority, 'status': 'waiting', 'attempt': 0, 'run': [],
                      'qa_command': _acceptance_command(f)})
        for key in ('scope_points', 'estimate_usd', 'risky', 'repair_of', 'source_tasks', 'source_items', 'source_acceptance'):
            if key in f:
                clean[-1][key] = f[key]
        if 'repair_of' in f and (not isinstance(f['repair_of'], str) or not ID.fullmatch(f['repair_of']) or f['repair_of'] == f['id']):
            raise ValueError('invalid repair source')
        feature_cap(clean[-1])
    ids = [f['id'] for f in clean]
    if len(set(ids)) != len(ids) or set(ids) & known:
        raise ValueError('duplicate feature id')
    all_items = set(ids) | known
    for f in clean:
        if any(b not in all_items or b == f['id'] for b in f['dependent']):
            raise ValueError(f"{f['id']}: unknown dependency")
    no__loop(clean)
    return clean


def no__loop(items_list: list[dict[str, Any]]) -> None:
    bag = {f['id']: set(f['dependent']) for f in items_list}
    finished: set[str] = set()
    while bag:
        ready = [k for k, v in bag.items() if v <= finished | (v - set(bag))]
        if not ready:
            raise ValueError('feature dependencies are cyclic')
        for k in ready:
            finished.add(k)
            del bag[k]


def feature_list(raw: str) -> list[dict[str, Any]]:
    return _verify(_raw_list(raw))


def following(items_list: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The highest-priority (smallest number) pending feature whose dependencies have passed; list order on a tie."""
    passing = {f['id'] for f in items_list if f['status'] in ('passed', 'applied')}
    candidates = [(f['priority'], i, f) for i, f in enumerate(items_list) if f['status'] == 'waiting' and set(f['dependent']) <= passing]
    return min(candidates, key=lambda a: (a[0], a[1]))[2] if candidates else None


def apply_outcome(f: dict[str, Any], run_id: str, success: bool) -> dict[str, Any]:
    fresh = {**f, 'attempt': f['attempt'] + 1, 'run': [*f['run'], run_id]}
    fresh['status'] = 'applied' if success else ('stuck' if fresh['attempt'] >= ATTEMPT_CAP else 'waiting')
    return fresh


def stop_decision(status: dict[str, Any], now_ts: float) -> tuple[bool, str]:
    """(stop?, reason). `(False, 'final-acceptance')` = features done, move to whole-project acceptance."""
    list = status['features']
    if now_ts - status['start'] >= DURATION_CAP_SEC:
        return True, 'duration-cap'
    remaining = [f for f in list if f['status'] == 'waiting']
    if not remaining:
        if not any(f['status'] in ('passed', 'applied') for f in list):
            return True, 'no-progress'
        if status.get('final_acceptance_round', 0) >= FINAL_ACCEPTANCE_ROUND:
            return True, 'final-acceptance-rounds-finished'
        unevidenced = [f['id'] for f in list if f['status'] == 'stuck' and not _acceptance_command(f)]
        if unevidenced:
            return True, 'unevidenced-stuck-feature: ' + ','.join(unevidenced)
        return False, 'final-acceptance'
    if now_ts - status['start'] >= DURATION_CAP_SEC:
        return True, 'duration-cap'
    if status.get('consecutive_failed', 0) >= CONSECUTIVE_FAILED_CAP:
        return True, 'no-progress'
    if now_ts - max(status.get('last_passed') or 0.0, status['start']) >= PROGRESS_ABSENT_SEC:
        return True, 'no-progress'
    if following(list) is None:
        return True, 'blocked'
    return False, ''


def _purpose(text: str) -> set[str]:
    text = text.lower()
    synonyms = {
        'detect': 'find', 'discover': 'find', 'identify': 'find', 'find': 'find',
        'source': 'source', 'file': 'source', 'inventor': 'inventory', 'list': 'inventory',
        'task': 'item', 'item': 'item', 'extract': 'produce', 'create': 'produce', 'write': 'produce', 'produce': 'produce', 'generate': 'produce',
        'verify': 'check', 'audit': 'check', 'check': 'check',
    }
    unnecessary = {'the', 'a', 'an', 'and', 'or', 'for', 'to', 'of', 'in', 'on', 'with', 'all', 'every', 'one', 'do', 'make', 'be', 'is', 'are',
                   'active', 'existing', 'current'}
    outcome = set()
    for word in re.findall(r'[a-z0-9_]+', text):
        if word in unnecessary:
            continue
        root = next((v for k, v in synonyms.items() if word.startswith(k)), word)
        outcome.add(root)
    return outcome


def same_purpose(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """B315: renaming the same goal is not a new feature."""
    for field in ('title', 'acceptance'):
        x, y = _purpose(a.get(field, '')), _purpose(b.get(field, ''))
        minimum = 4 if field == 'acceptance' else 2
        if len(x) < minimum or len(y) < minimum:
            continue
        code_x = {v for v in x if re.fullmatch(r'[a-z]+[0-9]+', v)}
        code_y = {v for v in y if re.fullmatch(r'[a-z]+[0-9]+', v)}
        if code_x and code_y and code_x.isdisjoint(code_y):
            continue
        if len(x & y) / len(x | y) >= .8:
            return True
    return False


def again_plan_merge(old: list[dict[str, Any]], raw: str) -> list[dict[str, Any]]:
    """Passed and stuck features are kept (with their history); pending ones change with the new list (the attempt history of the same id is carried)."""
    protected = [copy.deepcopy(f) for f in old if f['status'] in ('passed', 'applied', 'stuck')]
    protected_ids = frozenset(f['id'] for f in protected)
    history = {f['id']: f for f in old if f['status'] == 'waiting'}
    fresh = _verify([f for f in _raw_list(raw) if not (isinstance(f, dict) and f.get('id') in protected_ids)], protected_ids)
    stuck = {f['id'] for f in protected if f['status'] == 'stuck'}
    for f in fresh:
        if set(f['dependent']) & stuck:
            raise ValueError(f"{f['id']}: unfinished preserved dependency")
        if f.get('repair_of'):
            source = next((g for g in protected if g['id'] == f['repair_of']), None)
            if source is None or f['acceptance'] != source['acceptance']:
                raise ValueError(f"{f['id']}: repair must preserve source acceptance criteria")
    for i, f in enumerate(fresh):
        previous = next((g for g in [*protected, *fresh[:i]] if same_purpose(f, g)
                       and not (f.get('repair_of') == g['id'] and f['acceptance'] == g['acceptance'])), None)
        if previous:
            raise ValueError(f"{f['id']}: duplicate purpose ({previous['id']}); do not repeat the acceptance of an existing feature")
    for f in fresh:
        if f['id'] in history:
            f['attempt'], f['run'] = history[f['id']]['attempt'], list(history[f['id']]['run'])
    return protected + fresh


def project_ref_sources(plan: dict[str, Any], features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tasks = {task['id']: task for task in plan.get('tasks', [])}
    if not tasks:
        if plan.get('source_items') or any(f.get('source_tasks') or f.get('source_items') or f.get('source_acceptance') for f in features):
            raise ValueError('project-source-coverage')
        return copy.deepcopy(features)
    covered: set[str] = set()
    result = []
    for feature in features:
        names = feature.get('source_tasks')
        if (not isinstance(names, list) or not names or any(not isinstance(name, str) or name not in tasks for name in names)
                or len(set(names)) != len(names)):
            raise ValueError('project-source-coverage')
        covered.update(names)
        items = [{k: item[k] for k in ('id', 'source_sha256', 'evidence')}
                 for item in plan.get('source_items', []) if set(names) & set(item['tasks'])]
        acceptance = [{'id': name, 'acceptance': tasks[name]['acceptance']} for name in names]
        for key, value in (('source_items', items), ('source_acceptance', acceptance)):
            if key in feature and feature[key] != value:
                raise ValueError('project-source-coverage')
        result.append({**copy.deepcopy(feature), 'source_items': items, 'source_acceptance': acceptance})
    if covered != tasks.keys():
        raise ValueError('project-source-coverage')
    return result


def project_ref_plan_save(store: Any, run_id: str, features: list[dict[str, Any]], raw: str = '') -> list[dict[str, Any]]:
    if not hasattr(store, 'home'):
        return features
    from run_store import automatic_revision, bounded_json, plan_digest
    from outcome import merge_uncertainties
    import os
    path = store.path(run_id) / 'plan.json'
    plan = bounded_json(path) if path.exists() else {'job_id': run_id, 'kind': 'project', 'tasks': []}
    before = plan_digest(plan)
    if raw:
        envelope = _raw_plan(raw)
        if envelope.get('job_kind', plan.get('job_kind')) != plan.get('job_kind'):
            raise ValueError('request-job-type-mismatch')
        plan.update(merge_uncertainties(plan, envelope))
    features = project_ref_sources(plan, features)
    if store.read(run_id).get('supervision') == STEP_POLICY and any(len(f.get('source_items', [])) > 1 for f in features):
        raise ValueError('project-step-multiple-source-items')
    definitions = [{k: v for k, v in f.items() if k not in ('status', 'attempt', 'run', 'last_acceptance')} for f in features]
    history = store.plan_view(run_id)['history']
    original = next((e['plan']['project_features'] for e in history if e['plan'].get('project_features')), definitions)
    scope = {f['id']: f for f in original if not f.get('repair_of')}
    if not set(scope) <= {f['id'] for f in definitions}:
        raise ValueError('project-scope-removal')
    for f in definitions:
        source = scope.get(f.get('repair_of') or f['id'])
        if (not source or f['acceptance'] != source['acceptance'] or f.get('source_tasks') != source.get('source_tasks')
                or (not f.get('repair_of') and f['title'] != source['title'])):
            raise ValueError('project-scope-expansion')
    plan['project_features'] = definitions
    unchanged = before == plan_digest(plan)
    if not path.exists():
        store.save_plan(run_id, plan)
        from run_store import plan_ready
        review = plan_ready(store, run_id, os.environ.get('CADRE_REAL_OPENCODE', '/opt/homebrew/bin/opencode'))
    elif unchanged and not store.plan_view(run_id)['held']:
        from run_store import plan_ready
        review = plan_ready(store, run_id, os.environ.get('CADRE_REAL_OPENCODE', '/opt/homebrew/bin/opencode'))
    else:
        review = automatic_revision(store, run_id, plan, os.environ.get('CADRE_REAL_OPENCODE', '/opt/homebrew/bin/opencode'))
    if review.get('status') != 'PASS':
        raise ValueError('current-plan-review-required')
    return features


def project_ref_budget_pin(store: Any, run_id: str, started: float | None = None) -> None:
    from run_store import FileLock, atomic_json
    from budget_gate import amount
    if not hasattr(store, 'home'):
        return
    with FileLock(store.path(run_id) / '.record.lock'):
        r = store.read(run_id)
        cap = amount(r.get('operator_budget_usd', r.get('budget_cap_usd')))
        if 'project_budget_cap_usd' in r:
            cap = min(cap, amount(r['project_budget_cap_usd']))
        saved = r.get('project_operator_budget')
        if saved:
            cap = min(cap, amount(saved['amount_usd']))
        r.update(project_budget_cap_usd=cap, project_operator_budget={
            'amount_usd': cap, 'currency': 'USD', 'scope': 'operator-total',
            'providers': ['opencode', 'openrouter'],
            'subscription_usage': {'opencode-go': {'cap_usd_equivalent': cap}, 'claude': {'cap_usd_equivalent': cap}}}, project_run_ids=sorted(set(r.get('project_run_ids') or []) | {run_id}))
        if started is not None:
            r['project_deadline'] = min(r.get('project_deadline', started + DURATION_CAP_SEC), started + DURATION_CAP_SEC)
        atomic_json(store.path(run_id) / 'run.json', r)


def feature_cap(f: dict[str, Any]) -> float:
    from level import plan_cap, level
    points = f.get('scope_points', 50)
    if type(points) is not int or not 0 <= points <= 100:
        raise ValueError('feature scope score must be 0-100')
    return plan_cap({'tasks': [{'estimate_usd': f.get('estimate_usd', 0)}]}, level(min(points, 85))['cap_usd'])


def project_ref_cap_write(store: Any, run_id: str, items_list: list[dict[str, Any]]) -> None:
    from contextlib import nullcontext
    from run_store import FileLock, atomic_json
    project_ref_budget_pin(store, run_id)
    with FileLock(store.path(run_id) / '.record.lock') if hasattr(store, 'home') else nullcontext():
        r = store.read(run_id)
        plan = {f['id']: max(feature_cap(f), (r.get('project_feature_caps') or {}).get(f['id'], 0)) for f in items_list}
        ids = set(r.get('project_run_ids') or [run_id])
        if hasattr(store, 'home'):
            ids.update(c['run_id'] for c in store.list() if c.get('project_ref_parent') == run_id)
        r.update(project_budget_plan=plan, project_run_ids=sorted(ids))
        if hasattr(store, 'home'):
            atomic_json(store.path(run_id) / 'run.json', r)
        else:
            store.update(run_id, **{k: r[k] for k in ('project_budget_plan', 'project_run_ids')})


def feature_cap_update(store: Any, run_id: str, feature: str, cap: float) -> None:
    """P2: the child's detailed plan updates the preliminary estimate in the project list."""
    from run_store import FileLock, atomic_json
    from budget_gate import amount
    cap = amount(cap)
    with FileLock(store.path(run_id) / '.record.lock'):
        r = store.read(run_id)
        plan = r.get('project_budget_plan') or {}
        if feature not in plan:
            return
        r.setdefault('project_feature_caps', {})[feature] = cap
        plan[feature] = max(plan[feature], cap)
        atomic_json(store.path(run_id) / 'run.json', r)


def project_ref_cost(home: str | Path, runs: list[str]) -> dict[str, float] | None:
    """B291: ledger total of the main + child runs; measured receipts, ghost estimates (B85) and pending separately. None without a ledger."""
    import sqlite3
    from contextlib import closing
    from budget_gate import USD_PROVIDERS
    db = Path(home or '/nonexistent') / 'chain/request-budget.db'
    if not home or not db.exists() or not runs:
        return None
    try:
        from run_store import RunStore
        ids = set(runs)
        # G-B12/B317: an active/cancelled child not yet added to the parent summary is also included in the ledger.
        ids.update(r['run_id'] for r in RunStore(Path(home)).list() if r.get('project_ref_parent') in runs)
        runs = sorted(ids)
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)) as con:
            lines = con.execute(
                "select reserved, actual, receipt, metadata from reservation where json_extract(metadata,'$.run_id') in (%s) and provider in (%s)"
                % (','.join('?' * len(runs)), ','.join('?' * len(USD_PROVIDERS))), (*runs, *USD_PROVIDERS)).fetchall()
    except (OSError, ValueError, sqlite3.Error):
        return None
    from budget_gate import recovery_amount, BudgetDenied
    estimate = ('phantom:', 'recovery:')
    measured = sum(a for _, a, r, _ in lines if a is not None and not str(r or '').startswith(estimate))
    estimated = sum(a for _, a, r, _ in lines if a is not None and str(r or '').startswith(estimate))
    pending = 0
    for rz, a, _, meta in lines:
        if a is None:
            try:
                pending += recovery_amount(rz, json.loads(meta or '{}'))
            except (ValueError, TypeError, AttributeError, BudgetDenied):
                return None
    return {k: round(v / 1e6, 6) for k, v in (('measured_usd', measured), ('estimated_usd', estimated), ('pending_usd', pending),
                                             ('liability_usd', measured + estimated + pending))}


def account_summary(expense: dict[str, float], cap_usd: float) -> dict[str, float]:
    return {**expense, 'cap_usd': round(float(cap_usd), 6),
            'remaining_usd': round(float(cap_usd) - expense['liability_usd'], 6)}


def _spend_line(d: dict[str, Any]) -> str:
    m = d.get('expense')
    if not isinstance(m, dict):
        return f"spend ${float(d.get('spend_usd') or 0):.2f} (child summaries)"
    return (f"settled ${m['measured_usd']:.2f} · estimated ${m['estimated_usd']:.2f} · pending parent limit ${m['pending_usd']:.2f}"
            f" · liability ${m['liability_usd']:.2f}")


def report(d: dict[str, Any]) -> str:
    list = d.get('features') or []
    counts = {k: sum(1 for f in list if f['status'] == k) for k in ('passed', 'applied', 'stuck', 'waiting')}
    lines = [f"# Project Ref report — {d.get('id', '?')}", '',
                f"Request: {d.get('request_summary', '')}",
                f"Status: {d.get('status', '?')}" + (f" ({d['reason']})" if d.get('reason') else '') +
                f" · intermediate branch `{d.get('intermediate_branch', '-')}` · " + _spend_line(d),
                f"Features: {counts['passed']} passed · {counts['applied']} applied · {counts['stuck']} stuck · {counts['waiting']} waiting", '',
                '| id | title | status | attempt | runs |', '|---|---|---|---|---|']
    lines += [f"| {f['id']} | {f['title']} | {f['status']}{' (at final acceptance)' if f.get('last_acceptance') else ''} | {f['attempt']} | "
                 f"{', '.join(f['run'][-3:]) or '-'} |" for f in list]
    return '\n'.join(lines) + '\n'


# --- Orchestration (G-P1..P6): on top of the pure core; the planner/child run/merge are supplied from outside ---

from level import CONTEXT_MARK  # noqa: E402  the context part of the child prompt does not enter the rule floor (G-P1)

LIST_SCHEMA = {'features': [{'id': 'f1', 'title': '...', 'description': '...', 'acceptance': 'measurable acceptance (which test/command)',
                                'dependent': [], 'priority': 1, 'scope_points': 50, 'estimate_usd': 2.0, 'qa_command': 'targeted test command', 'risky': False}]}


def _acceptance_command(f: dict[str, Any]) -> str:
    command = f.get('qa_command')
    return command.strip() if isinstance(command, str) and command.strip() and command.strip() != 'targeted test command' else ''


def _extras_block(extras: str) -> str:
    return f"FILES NAMED IN THE REQUEST (content at acceptance):\n{extras}\n" if extras else ''


def list_prompt(request: str, map: str = '', extras: str = '', first_plan: str = '') -> str:
    # B346: in an existing repo (map filled) skeleton/preparation does not count as product progress; features are bound to the acceptance of the items in the source.
    structure = ("The first feature must be the skeleton (project_ref structure, test infrastructure, quality command). "
            if not map else
            "In an existing repo do not open a separate skeleton/preparation feature; every feature must meet the acceptance criterion "
            "of one concrete item (backlog) in the request. Preparatory work such as research/discovery/inventory is not a feature. ")
    return ("YOU ARE A PROJECT_REF PLANNER. Review the real source files with read-only tools; write ONLY a single JSON block.\n"
            f"Split the request below into FEATURES with measurable acceptance criteria, each fitting into a SINGLE development run (1-4 hours) "
            f"(target 6 features; more when needed, safety cap {FEATURE_CAP}). {structure}"
            "Bind every mandatory item written in the request and in the files it names to a feature; drop none. "
            "Write the dependencies; priority 1 (first) - 5. Ids in lower case (f1, f2, ...).\n"
            "For every feature write a fast, targeted acceptance command into the qa_command field; do not write the whole test suite. "
            "If there is an auth, secret, permission, network or data-loss risk, write risky=true (it requires an intermediate judge). "
            "For every feature write a scope score (0-100) and the estimated total dollar cost (estimate_usd).\n"
            "Bind the tasks of the initial plan to each feature's source_tasks list; all task ids must be covered. "
            "Keep the source ids/hashes and the original acceptance text. Report routine choices as assumptions and critical questions as decisions.\n"
            f"SCHEMA: {json.dumps(LIST_SCHEMA, ensure_ascii=False)}\n"
            + (f"REPO MAP:\n{map}\n" if map else '') + f"REQUEST:\n{request}\n" + _extras_block(extras)
            + (f"TASKS EXTRACTED BY THE INITIAL PLANNER (hint; do not lose scope):\n{first_plan}\n" if first_plan else ''))


def feature_prompt(request: str, d: dict[str, Any], f: dict[str, Any], path: str = '') -> str:
    finished = [g for g in d['features'] if g['status'] in ('passed', 'applied')]
    context = request if len(request) <= CONTEXT_CAP else (
        request[:CONTEXT_CAP] + f"\n[... the full request is {len(request)} characters: {path or 'the prompt.md file of the main run'}]")
    return (f"PROJECT_REF MODE — feature {f['id']}: {f['title']} ({len(finished) + 1}/{len(d['features'])})\n"
            f"In this run do ONLY this feature: {f['description']}\nACCEPTANCE: {f['acceptance']}\n"
            + ('SOURCE AND ORIGINAL ACCEPTANCE: ' + json.dumps({k: f.get(k, []) for k in ('source_tasks', 'source_items', 'source_acceptance')}, ensure_ascii=False) + '\n')
            + f"Finished features (do not break them): {', '.join(g['id'] + ' ' + g['title'] for g in finished) or '-'}\n"
            "Rules: do not move on to other features; keep the existing tests green; write a test for the new behaviour.\n"
            f"{CONTEXT_MARK}:\n{context}\n")


def final_acceptance_prompt(request: str, d: dict[str, Any], extras: str = '') -> str:
    stuck = [f for f in d['features'] if f['status'] == 'stuck']
    missing = ''.join(f"- {f['id']} {f['title']}: {f['description']}\n  ACCEPTANCE: {f['acceptance']}\n"
                    + (f"  ACCEPTANCE COMMAND (the final acceptance QA runs this too): {_acceptance_command(f)}\n" if _acceptance_command(f) else '')
                    for f in stuck)
    return ("PROJECT_REF END ACCEPTANCE — the feature round is finished. Audit the project end to end against the original request: fix what is missing or broken, "
            "all tests and the quality command must be green; do not invent new features.\n"
            f"Finished features: {', '.join(g['id'] + ' ' + g['title'] for g in d['features'] if g['status'] in ('passed', 'applied'))}\n"
            + (f"STUCK FEATURES (not done yet; complete them in this run, the job does not count as finished until their acceptance is met):\n{missing}" if missing else '')
            + f"ORIGINAL REQUEST:\n{request}\n" + _extras_block(extras))


def again_plan_prompt(request: str, d: dict[str, Any], final_acceptance: dict[str, Any] | None = None, extras: str = '') -> str:
    # B346: show the description and acceptance text of completed/failed features too; new features must not drift from the acceptance in the source.
    status = []
    for f in d['features']:
        o = {k: f[k] for k in ('id', 'title', 'status', 'attempt', 'dependent', 'priority')}
        if f['status'] in ('passed', 'applied', 'stuck'):
            o.update(description=f.get('description', ''), acceptance=f.get('acceptance', ''))
        status.append(o)
    extra = ''
    if final_acceptance is not None:
        extra = ("THE END ACCEPTANCE FAILED; its result (judge/QA): " + json.dumps(final_acceptance.get('final_result') or {}, ensure_ascii=False)[:4000] +
              "\nAdd CORRECTION features that close these gaps.\n")
    return ("YOU ARE A PROJECT_REF PLANNER. Review the real source files with read-only tools; write ONLY a single JSON block.\n"
            "Update the feature list: 'passed' and 'stuck' ones do not change (they are preserved even if you omit them); you may split and "
            "reorder the waiting ones; keep the chosen ids, titles and acceptance texts. Do NOT ADD new product goals or features. "
              "If a repair is needed, bind it to the chosen feature with repair_of and keep that feature's acceptance text verbatim. "
              "Keep the source_tasks bindings to the source tasks; report routine choices as assumptions and critical questions as decisions.\n" + extra +
            f"EXISTING STATUS: {json.dumps(status, ensure_ascii=False)}\n"
            f"SCHEMA: {json.dumps(LIST_SCHEMA, ensure_ascii=False)}\nORIGINAL REQUEST:\n{request}\n" + _extras_block(extras))


def first_plan_summary(root: Path) -> str:
    try:
        plan = json.loads((Path(root) / 'plan.json').read_text())
        tasks = [{k: g[k] for k in ('id', 'definition', 'acceptance', 'files') if g.get(k)} for g in plan.get('tasks') or []]
    except (OSError, ValueError, AttributeError, TypeError):
        return ''
    if not tasks:
        return ''
    return json.dumps({'scope_rationale': plan.get('scope_rationale'), 'tasks': tasks,
                       **{key: plan.get(key, []) for key in ('source_items', 'assumptions', 'decisions')},
                       'job_kind': plan.get('job_kind')}, ensure_ascii=False)


def _fresh_status(run_id: str, request: str, now_ts: float) -> dict[str, Any]:
    id = re.sub(r'[^a-z0-9]', '', run_id.rsplit('-', 1)[-1].lower())[:8] or 'project_ref'
    first = next((line.strip() for line in request.splitlines() if line.strip()), '')
    return {'id': id, 'request_summary': first[:200], 'intermediate_branch': f'cadre/project-ref-{id}', 'features': [],
            'start': now_ts, 'last_passed': now_ts, 'consecutive_failed': 0, 'final_acceptance_round': 0,
            'status': 'running', 'reason': '', 'spend_usd': 0.0, 'finished_count': 0, 'children': []}


def entry_obstacle(record: dict[str, Any]) -> int | None:
    from run_store import TERMINAL, plan_hold, git_permission_waiting, project_time_expired
    state = record.get('state')
    if state in TERMINAL:
        from outcome import approved
        return 0 if state == 'completed' and approved(record) else 130 if state == 'cancelled' else 78
    if plan_hold(record):
        return 78
    if _alarm_waiting(record):
        return 75
    if (git_permission_waiting(record) or project_time_expired(record)
            or record.get('reason') == 'project-ref-stopped: project-plan-review-required'):
        return 78
    return None


def status_view(record: dict[str, Any], project_ref: dict[str, Any]) -> dict[str, Any]:
    from run_store import plan_hold
    d = dict(project_ref)
    state = record.get('state')
    reason = str(record.get('reason') or '')
    project_reason = reason.removeprefix('project-ref-stopped: ')
    phase = ''
    if state in ('cancelled', 'failed', 'implemented'):
        d.update(status='stopped', reason='cancel' if state == 'cancelled' else reason or str(state))
    elif state == 'completed' and d.get('status') != 'complete':
        phase = 'Status conflict'
    elif plan_hold(record):
        change = record['plan_changes'][-1]
        status = change['status']
        phase = ('Awaiting stop confirmation' if status == 'stop_requested' else
                 'Plan review' if status == 'review_pending' else
                 'Plan review rejected' if status == 'rejected' else
                 'Awaiting approved continuation' if status == 'withdrawn' else 'Awaiting plan change')
        if status != 'stop_requested':
            d.update(status='stopped', reason=reason)
    elif (project_ref.get('checkpoint') or {}).get('status') == 'waiting':
        d.update(status='stopped', reason='project-step-verification-required')
        phase = 'Awaiting job result verification'
    elif state != 'running' and (reason == 'plan-change-resume-recorded' or
            (project_ref.get('reason') == 'spend-alarm' and record.get('spend_approval_ts') and not _alarm_waiting(record))):
        d.update(status='stopped')
        phase = 'Awaiting approved continuation'
    elif state == 'retryable':
        d.update(status='stopped', reason=project_reason or d.get('reason') or '')
    d['stage_label'] = phase
    return d


def status_match(store: Any, run_id: str) -> None:
    from run_store import atomic_json, atomic_write, bounded_json
    path = store.path(run_id) / 'project_mode.json'
    if not path.exists():
        return
    old = bounded_json(path)
    current = status_view(store.read(run_id), old)
    if current != old:
        atomic_json(path, current)
        atomic_write(path.with_name('project-ref-report.md'), report(current))


def execute(store: Any, run_id: str, *, plan: Callable[[str, str], str], child_execute: Callable[[str, str], dict[str, Any]],
          merge: Callable[[str, dict[str, Any]], bool], now_ts: Callable[[], float] = time.time, map: str = '',
          extras: str = '') -> int:
    """Project loop (single writer, sequential). State in `project_mode.json`, report in `project-ref-report.md`; resumes where it left off after an interruption."""
    record = store.read(run_id)
    blocked = entry_obstacle(record)
    if blocked is not None:
        return blocked
    if supervision_waiting(store, record):
        return 78
    supervised = record.get('supervision') == STEP_POLICY
    root = Path(store.path(run_id))
    path = root / 'project_mode.json'
    request_path = store.read(run_id)['prompt']
    request = Path(request_path).read_text(errors='replace')
    d = {**_fresh_status(run_id, request, now_ts()), **(json.loads(path.read_text()) if path.exists() else {})}   # an old record gets the new fields with defaults
    if not d['features'] and hasattr(store, 'home'):
        view = store.plan_view(run_id)
        review = view['reviews'][-1] if view['reviews'] else {}
        if (view['revision'] and view['execution_plan_matches'] and not view['held'] and review.get('status') == 'PASS'
                and review.get('revision_number') == view['revision']):
            definitions = view['history'][-1]['plan'].get('project_features')
            if definitions:
                d['features'] = _verify(definitions)
    project_ref_budget_pin(store, run_id, d['start'])

    def save() -> None:
        from run_store import atomic_json, atomic_write
        d['expense'] = project_ref_cost(getattr(store, 'home', ''), [run_id, *(c.get('run') for c in d['children'] if c.get('run'))])
        d['cap_usd'] = store.read(run_id).get('project_budget_cap_usd')
        atomic_json(path, d)
        atomic_write(root / 'project-ref-report.md', report(d))

    def process_child(k: dict[str, Any], label: str) -> bool:
        success = k.get('state') in ('completed', 'implemented') and merge(d['intermediate_branch'], k)
        d['spend_usd'] = round(d['spend_usd'] + float(k.get('budget_spent_usd') or 0), 4)
        d['children'].append({'feature': label, 'run': k.get('run_id'), 'status': k.get('state'), 'success': success,
                              'reason': (k.get('final_result') or {}).get('reason') or k.get('reason')})
        return success

    def permission_wait(exc: PermissionError, k: dict[str, Any]) -> int:
        if str(exc) == 'commit-permission-required':
            git_commit_wait(store, run_id, Path(k['worktree']), k)
        d.update(status='stopped', reason=str(exc), pending_product={'run': k.get('run_id'), 'worktree': k.get('worktree'),
                 'revision': (k.get('final_result') or {}).get('revision')})
        store.update(run_id, reason='project-ref-stopped: ' + str(exc))
        save()
        return 78

    def checkpoint_wait(feature: str, child: dict[str, Any], success: bool) -> int:
        import uuid

        from run_store import plan_digest
        d.update(status='stopped', reason='project-step-verification-required', checkpoint={
            'id': uuid.uuid4().hex, 'feature': feature, 'run_id': child['run_id'], 'success': success,
            'status': 'waiting', 'created': now_ts(), 'final_digest': plan_digest(child.get('final_result') or {})})
        save()
        store.update(run_id, reason='project-ref-stopped: project-step-verification-required')
        return 78

    def again(final_acceptance: dict[str, Any] | None = None) -> bool:
        try:
            raw = plan(again_plan_prompt(request, d, final_acceptance, extras), 'project-ref-again') if final_acceptance is not None else ''
            features = again_plan_merge(d['features'], raw) if raw else d['features']
            features = project_ref_plan_save(store, run_id, features, raw)
            view = store.plan_view(run_id) if hasattr(store, 'home') else {}
            d.setdefault('plan_evaluations', []).append({'ts': now_ts(), 'reason': 'final-acceptance-failed' if final_acceptance else 'progress-or-stall',
                'decision': 'revised' if features != d['features'] else 'unchanged',
                'plan_revision': view.get('revision'), 'plan_digest': (view.get('history') or [{}])[-1].get('digest'),
                'source_sha': store.read(run_id).get('source_sha'),
                'evidence': {'completed': d['finished_count'], 'stalled': [f['id'] for f in features if f['status'] == 'stuck']}})
            d['features'] = features
            project_ref_cap_write(store, run_id, d['features'])
            return True
        except (ValueError, RuntimeError, OSError) as exc:
            d['last_plan_error'] = str(exc)[:300]
            d.update(status='stopped', reason='project-plan-review-required')
            store.update(run_id, reason='project-ref-stopped: project-plan-review-required')
            return False

    if len(request) > PLAN_REQUEST_CAP:
        d['status'], d['reason'] = 'stopped', f'request-many-long: {len(request)} characters (cap {PLAN_REQUEST_CAP})'
        save()
        return 1
    d.update(status='running', reason='', stage_label='')
    store.update(run_id, reason=None)
    save()
    if d.pop('replan_pending', False):
        if not again():
            save()
            return 78
        save()
    while True:
        if (root / 'cancel.flag').exists():
            d['status'], d['reason'] = 'stopped', 'cancel'
            break
        if now_ts() - d['start'] >= DURATION_CAP_SEC:
            d.update(status='stopped', reason='duration-cap')
            break
        if not d['features']:
            try:
                raw = plan(list_prompt(request, map, extras, first_plan_summary(root)), 'project-ref-list')
                features = project_ref_plan_save(store, run_id, feature_list(raw), raw)
                d['features'] = features
            except (ValueError, RuntimeError, OSError) as exc:
                d['status'], d['reason'], d['last_plan_error'] = 'stopped', 'list-could-not-produce', str(exc)[:300]
                break
            save()
        project_ref_cap_write(store, run_id, d['features'])
        stop, reason = stop_decision(d, now_ts())
        if stop:
            d['status'], d['reason'] = 'stopped', reason
            break
        if reason == 'final-acceptance':
            store.update(run_id, project_active_feature=None)
            k = child_execute(final_acceptance_prompt(request, d, extras), d['intermediate_branch'])
            if wait_reason(k):
                if supervised:
                    return checkpoint_wait('final-acceptance', k, False)
                d['status'], d['reason'] = 'stopped', wait_reason(k)
                save()
                return 1
            try:
                accepted = process_child(k, 'final-acceptance')
            except PermissionError as exc:
                return permission_wait(exc, k)
            if accepted:
                for f in d['features']:
                    if f['status'] == 'applied':
                        f['status'] = 'passed'
                    elif f['status'] == 'stuck':
                        f['status'], f['last_acceptance'] = 'passed', True
                d['status'], d['reason'], d['last_child'] = 'complete', 'final-acceptance-passed', k.get('run_id')
                store.update(run_id, worktree=k.get('worktree'), final_result=k.get('final_result'),
                             final_acceptance_run_id=k['run_id'])
                break
            if supervised:
                return checkpoint_wait('final-acceptance', k, False)
            d['final_acceptance_round'] += 1
            if not again(k):
                break
            save()
            continue
        retry = d.pop('retry_feature', None)
        f = next((item for item in d['features'] if item['id'] == retry), None) if retry else following(d['features'])
        if f is None or f['status'] != 'waiting':
            raise ValueError('project-step-retry-feature-invalid')
        if supervised and len(f.get('source_items', [])) > 1:
            raise ValueError('project-step-multiple-source-items')
        store.update(run_id, project_active_feature=f['id'])
        k = child_execute(feature_prompt(request, d, f, request_path), d['intermediate_branch'])
        if wait_reason(k):
            if supervised:
                return checkpoint_wait(f['id'], k, False)
            d['status'], d['reason'] = 'stopped', wait_reason(k)
            save()
            return 1
        try:
            success = process_child(k, f['id'])
        except PermissionError as exc:
            return permission_wait(exc, k)
        fresh = apply_outcome(f, str(k.get('run_id')), success)
        d['features'] = [fresh if g['id'] == f['id'] else g for g in d['features']]
        if success:
            d['last_passed'], d['consecutive_failed'], d['finished_count'] = now_ts(), 0, d['finished_count'] + 1
        else:
            d['consecutive_failed'] += 1
        needs_replan = fresh['status'] == 'stuck' or (success and d['finished_count'] % 3 == 0 and following(d['features']) is not None)
        if supervised:
            d['replan_pending'] = needs_replan
            return checkpoint_wait(f['id'], k, success)
        if needs_replan:
            if not again():
                break
        save()
    save()
    return 0 if d['status'] == 'complete' else 1


# --- Git and project settings (G-P3): only `cadre/project-ref-*` is written, the canonical branch is not touched ---

INTERMEDIATE_BRANCH = re.compile(r'cadre/project-ref-[a-z0-9]{1,32}')
DERIVED_FILES = ('repo-path', 'base-branch', 'PROJECT.md', 'POLICY.md', 'STATE.md', 'qa-command', 'install-command', 'run-cap-usd',
                      'run-cap-hard-usd', 'format-command')   # B272: the hard cap and the format command are inherited too


def _git(location: str | Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    if args[:1] == ('status',):
        from outcome import git_read_environment
        with git_read_environment(location) as env:
            return subprocess.run(['git', '-C', str(location), *args], capture_output=True, text=True, timeout=120, check=check, env=env)
    return subprocess.run(['git', '-C', str(location), *args], capture_output=True, text=True, timeout=120, check=check,
                          env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})


def intermediate_branch_prepare(repo: str | Path, intermediate_branch: str, sha: str, source: dict[str, Any] | None = None) -> None:
    if not INTERMEDIATE_BRANCH.fullmatch(intermediate_branch):
        raise ValueError('the intermediate branch can only be cadre/project-ref-<id>')
    if _git(repo, 'show-ref', '--verify', '--quiet', f'refs/heads/{intermediate_branch}', check=False).returncode == 0:
        return
    _git(repo, 'branch', intermediate_branch, acceptance_image(repo, sha, source or {}))


def _negative_mentioned(request: str, start: int, last: int) -> bool:
    once = re.split(r'[.!?;,:]\s|\n', request[max(0, start - 120):start])[-1]
    then = re.split(r'[.!?;,:]\s|\n', request[last:last + 120])[0]
    return bool(NEGATIVE.search(once + ' ' + then))


def request_extras(repo: str | Path, ref: str, request: str) -> str:
    files = [y for y in _git(repo, 'ls-tree', '-r', '--name-only', ref, check=False).stdout.splitlines()
                if y and Path(y).suffix.lower() in DOCUMENT_EXTENSIONS and not SENSITIVE_PATH.search(y)]

    def mentioned(pattern: str) -> bool:
        return any(not _negative_mentioned(request, m.start(), m.end()) for m in re.finditer(pattern, request, re.I))
    names: dict[str, list[str]] = {}
    for path in files:
        names.setdefault(Path(path).name.casefold(), []).append(path)
    selected = [y for y in files if '/' in y and mentioned(re.escape(y))]
    for entry_name, paths in names.items():
        if mentioned(rf'(?<![\w/.-]){re.escape(entry_name)}(?![\w-])'):
            root = [y for y in paths if '/' not in y]
            selected += [y for y in (root or paths) if y not in selected]
    parts = []
    for path in selected:
        r = subprocess.run(['git', '-C', str(repo), 'show', f'{ref}:{path}'], capture_output=True, timeout=60)
        if r.returncode == 0 and b'\0' not in r.stdout[:8192]:
            text = r.stdout.decode('utf-8', 'replace')
            if len(text) > EXTRA_FILE_CAP:
                text = text[:EXTRA_FILE_CAP] + f"\n[... the full file is {len(text)} characters: {path}; the remaining items are read from this file in the child runs]"
            parts.append(f'--- {path} ---\n' + text)
    return '\n'.join(parts)


def acceptance_image(repo: str | Path, sha: str, source: dict[str, Any]) -> str:
    patch, copy = source.get('patch'), Path(str(source.get('untracked') or '-'))
    if not source.get('carried') or not (patch or copy.is_dir()):
        return sha
    raise PermissionError('source-commit-permission-required')


def git_image_tree(repo: Path, sha: str, source: dict[str, Any] | None = None) -> str:
    import os
    import tempfile
    with tempfile.TemporaryDirectory(prefix='cadre-git-proof-') as temporary:
        root = Path(temporary)
        objects = root / 'objects'
        objects.mkdir()
        alternate = Path(_git(repo, 'rev-parse', '--git-path', 'objects').stdout.strip())
        alternate = alternate if alternate.is_absolute() else repo / alternate
        env = {**os.environ, 'GIT_INDEX_FILE': str(root / 'index'), 'GIT_OBJECT_DIRECTORY': str(objects),
               'GIT_ALTERNATE_OBJECT_DIRECTORIES': str(alternate.resolve()), 'GIT_OPTIONAL_LOCKS': '0'}
        def git(*args: str) -> str:
            return subprocess.run(['git', '-C', str(repo), *args], env=env, capture_output=True,
                                  text=True, check=True, timeout=120).stdout.strip()
        git('read-tree', sha)
        if source is not None:
            if source.get('patch'):
                git('apply', '--cached', '--binary', '--whitespace=nowarn', '--allow-empty', str(source['patch']))
            copy_root = Path(str(source.get('untracked') or '-'))
            if copy_root.is_dir():
                for path in sorted(copy_root.rglob('*')):
                    if path.is_file() and not path.is_symlink():
                        blob = git('hash-object', '-w', '--', str(path))
                        mode = '100755' if path.stat().st_mode & 0o111 else '100644'
                        git('update-index', '--add', '--cacheinfo', f'{mode},{blob},{path.relative_to(copy_root).as_posix()}')
        else:
            from qe_core import PRODUCT_EXCLUDED
            git('add', '-A', '--', '.')
            excluded = [p.replace(':(exclude,glob)', ':(glob)').replace(':(exclude)', '') for p in PRODUCT_EXCLUDED]
            git('rm', '--cached', '-r', '-q', '--ignore-unmatch', '--', *excluded)
        return git('write-tree')


def git_commit_wait(store: Any, run_id: str, repo: Path, child: dict[str, Any] | None = None) -> None:
    from run_store import FileLock, atomic_json, plan_digest
    from outcome import approved, implemented, revision
    if child and not (approved(child) or implemented(child)):
        raise ValueError('project-commit-unverified-product')
    with FileLock(store.path(run_id) / '.record.lock'):
        r = store.read(run_id)
        head = child['final_result']['revision']['head'] if child else r['source_sha']
        pending = {'kind': 'product' if child else 'source', 'path': str(repo.resolve()), 'head': head,
                   'tree': git_image_tree(repo, head, None if child else r.get('source') or {}),
                   'child': child['run_id'] if child else '', 'result_digest': plan_digest(child['final_result']) if child else '',
                   'plan_revision': store.plan_view(run_id)['revision'], 'source_sha': r['source_sha']}
        if child and revision(repo) != child['final_result']['revision']:
            raise ValueError('project-commit-evidence-mismatch')
        r['project_git_wait'] = pending
        atomic_json(store.path(run_id) / 'run.json', r)


def git_commit_take_over(store: Any, run_id: str, sha: str) -> dict[str, Any]:
    from contextlib import ExitStack
    from run_store import FileLock, atomic_json, plan_digest, plan_hold, project_time_expired, TERMINAL
    from outcome import revision, approved, implemented
    if not re.fullmatch('[a-f0-9]{40}|[a-f0-9]{64}', sha):
        raise ValueError('invalid-project-commit')
    with ExitStack() as locks:
        owner = FileLock(store.path(run_id) / '.owner.lock')
        if not owner.acquire(blocking=False):
            raise ValueError('project-commit-owner-active')
        locks.callback(owner.close)
        locks.enter_context(FileLock(store.path(run_id) / '.record.lock'))
        r = store.read(run_id)
        pending = r.get('project_git_wait') or {}
        if (not pending or r['state'] in TERMINAL or plan_hold(r) or project_time_expired(r)
                or (store.path(run_id) / 'cancel.flag').exists()
                or pending['plan_revision'] != store.plan_view(run_id)['revision']
                or pending['source_sha'] != r['source_sha']):
            raise ValueError('stale-project-commit')
        if pending.get('adopted_sha'):
            if pending['adopted_sha'] != sha:
                raise ValueError('stale-project-commit')
            return pending
        repo = Path(pending['path'])
        if _git(repo, 'rev-list', '--parents', '-n', '1', sha).stdout.split() != [sha, pending['head']]:
            raise ValueError('project-commit-parent-mismatch')
        if pending['kind'] == 'source':
            if (_git(repo, 'rev-parse', f'{sha}^{{tree}}').stdout.strip() != pending['tree']
                    or git_image_tree(repo, r['source_sha'], r.get('source') or {}) != pending['tree']):
                raise ValueError('project-commit-content-mismatch')
            r['project_source_commit'] = {'sha': sha, 'tree': pending['tree'], 'source_sha': r['source_sha']}
        else:
            child_id = pending['child']
            child_owner = FileLock(store.path(child_id) / '.owner.lock')
            if not child_owner.acquire(blocking=False):
                raise ValueError('project-commit-owner-active')
            locks.callback(child_owner.close)
            locks.enter_context(FileLock(store.path(child_id) / '.record.lock'))
            child = store.read(child_id)
            from qe_core import PRODUCT_EXCLUDED
            if (child.get('project_ref_parent') != run_id or child['state'] not in ('completed', 'implemented')
                    or Path(child['worktree']).resolve() != repo
                    or not _git(repo, 'branch', '--show-current').stdout.strip().startswith('cadre/')
                    or _git(repo, 'rev-parse', 'HEAD').stdout.strip() != sha
                    or _git(repo, 'status', '--porcelain', '--', '.', *PRODUCT_EXCLUDED).stdout.strip()
                    or git_image_tree(repo, sha) != pending['tree']):
                raise ValueError('project-commit-content-mismatch')
            result = child['final_result']
            proof = {'sha': sha, 'tree': pending['tree'], 'previous_result': pending['result_digest']}
            if result.get('commit_adoption') != proof:
                if plan_digest(result) != pending['result_digest']:
                    raise ValueError('project-commit-evidence-mismatch')
                current = revision(repo)
                result = {**result, 'revision': current, 'qa': {**result['qa'], 'revision': current},
                          'review': {**result.get('review', {}), 'revision': current}, 'commit_adoption': proof}
                if result.get('delivery'):
                    result['delivery'] = {**result['delivery'], 'revision': current, 'git_permission': 'operator-committed'}
                candidate = {**child, 'final_result': result}
                if not (approved(candidate) or implemented(candidate)):
                    raise ValueError('project-commit-evidence-mismatch')
                child['final_result'] = result
                atomic_json(store.path(child_id) / 'run.json', child)
            elif not (approved(child) or implemented(child)):
                raise ValueError('project-commit-evidence-mismatch')
        pending['adopted_sha'] = sha
        from run_store import intervention_add
        intervention_add(r, 'git-adopt', {'sha': sha, 'kind': pending['kind']})
        r.update(reason=None, project_git_wait=pending)
        atomic_json(store.path(run_id) / 'run.json', r)
        return pending


def git_transfer_approval(store: Any, run_id: str, token: str, confirmation: str = '') -> dict[str, Any]:
    from run_store import FileLock, atomic_json, plan_digest, plan_hold
    import uuid
    with FileLock(store.path(run_id) / '.record.lock'):
        r = store.read(run_id)
        action = r.get('project_git_action') or {}
        if (not action or action['token'] != token or plan_digest(action['identity']) != token or plan_hold(r)
                or action['identity']['plan_revision'] != store.plan_view(run_id)['revision']
                or r['state'] in ('completed', 'failed', 'cancelled', 'implemented')
                or (store.path(run_id) / 'cancel.flag').exists()):
            raise ValueError('stale-git-action')
        if action['status'] == 'pending' and not confirmation:
            action.update(status='first-approved', confirmation=uuid.uuid4().hex)
        elif action['status'] == 'first-approved' and confirmation and confirmation == action.get('confirmation'):
            action.update(status='approved')
        else:
            raise ValueError('git-second-approval-required')
        from run_store import intervention_add
        intervention_add(r, 'git-transfer-approve', {'stage': 2 if confirmation else 1})
        atomic_json(store.path(run_id) / 'run.json', r)
        return action


def git_transfer(store: Any, run_id: str, repo: str | Path, ref: str, fresh: str, old: str) -> bool:
    from run_store import FileLock, atomic_json, plan_digest, plan_hold
    command = ['git', '-C', str(Path(repo).resolve()), 'update-ref', ref, fresh, old]
    if (not ref.startswith('refs/heads/') or not INTERMEDIATE_BRANCH.fullmatch(ref.removeprefix('refs/heads/'))
            or any(not re.fullmatch('[a-f0-9]{40}|[a-f0-9]{64}', sha) for sha in (fresh, old))):
        raise ValueError('invalid-project-git-action')
    if store is None:
        raise PermissionError('git-transfer-permission-required')
    with FileLock(store.path(run_id) / '.record.lock'):
        r = store.read(run_id)
        if (r['state'] in ('completed', 'failed', 'cancelled', 'implemented') or plan_hold(r)
                or (store.path(run_id) / 'cancel.flag').exists()):
            raise PermissionError('git-transfer-stopped')
        action = r.get('project_git_action') or {}
        identity = {'command': command, 'source_sha': r['source_sha'], 'plan_revision': store.plan_view(run_id)['revision']}
        token = plan_digest(identity)
        if action.get('identity') != identity or action.get('status') in ('consumed', 'failed'):
            action = {'command': command, 'identity': identity, 'token': token, 'status': 'pending'}
            r['project_git_action'] = action
            atomic_json(store.path(run_id) / 'run.json', r)
        if action.get('status') != 'approved' or action.get('token') != token:
            raise PermissionError('git-transfer-permission-required')
        action.update(status='consumed')
        atomic_json(store.path(run_id) / 'run.json', r)
        return _git(repo, 'update-ref', ref, fresh, old, check=False).returncode == 0


def merge_ff(repo: str | Path, intermediate_branch: str, k: dict[str, Any], *, store: Any = None, run_id: str = '') -> bool:
    """Fast-forwards the clean product of a completed child onto the intermediate branch (update-ref checked against the old kind); False in every other case."""
    wt = Path(str(k.get('worktree') or ''))
    if k.get('state') not in ('completed', 'implemented') or not INTERMEDIATE_BRANCH.fullmatch(intermediate_branch) or not wt.is_dir():
        return False
    if not _git(wt, 'branch', '--show-current', check=False).stdout.strip().startswith('cadre/'):
        return False
    if k.get('state') == 'implemented':
        from outcome import implemented
        if not implemented(k):
            return False
    from qe_core import PRODUCT_EXCLUDED   # B234: the dirt measure is the same as the engine's; engine leftovers like `.tasks.json` are not product
    if _git(wt, 'status', '--porcelain', '--', '.', *PRODUCT_EXCLUDED, check=False).stdout.strip():
        raise PermissionError('commit-permission-required')
    fresh = _git(wt, 'rev-parse', 'HEAD').stdout.strip()
    old = _git(repo, 'rev-parse', f'refs/heads/{intermediate_branch}').stdout.strip()
    if fresh == old:
        return True
    if _git(repo, 'merge-base', '--is-ancestor', old, fresh, check=False).returncode != 0:
        return False
    return git_transfer(store, run_id, repo, f'refs/heads/{intermediate_branch}', fresh, old)


def derive_project_ref(home: str | Path, project_ref_name: str, id: str) -> str:
    """Project of the child runs: same repo and settings, a separate queue (a child must not wait in line while the main project run is going)."""
    from run_store import atomic_write, identifier
    entry_name = identifier(f'{project_ref_name}--p{id}')
    target = Path(home) / 'projects' / entry_name
    target.mkdir(parents=True, exist_ok=True)
    for file in DERIVED_FILES:
        source = Path(home) / 'projects' / project_ref_name / file
        if source.is_file():
            atomic_write(target / file, source.read_bytes())
    return entry_name


# --- Real bindings: planner (plan chain), child run (RunStore + execute_run), entry command ---

def _planner_call(store: Any, main_rid: str, model: str, prompt: str, task_id: str) -> str:
    from run_store import capture_source_plan
    from limits import deadline
    if store.read(main_rid).get('supervision') == STEP_POLICY:
        first, _, rest = prompt.partition('\n')
        prompt = first + '\n' + STEP_INSTRUCTIONS + rest
    return capture_source_plan(store, main_rid, os.environ.get('CADRE_REAL_OPENCODE') or '/opt/homebrew/bin/opencode',
                               model, prompt, task_id, deadline('plan'), 4)


def plan_real(store: Any, main_rid: str) -> Callable[[str, str], str]:
    """The first three links of the plan chain in order; returns the first answer that carries `features`."""
    def plan_history() -> list[dict[str, Any]]:
        # B252: dependencies on ids kept in a re-plan (passed/stuck) are valid; link validation must know this.
        try:
            d = json.loads((Path(store.path(main_rid)) / 'project_mode.json').read_text())
            features = d['features']
            if not isinstance(features, list) or any(not isinstance(f, dict) or f.get('status') not in
                    ('passed', 'applied', 'stuck', 'waiting') for f in features):
                raise ValueError('replan-history-invalid')
            return features
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            raise ValueError('replan-history-unreadable') from None

    def plan(prompt: str, task_id: str) -> str:
        from model_router import chain
        z, _ = chain('plan', 'hard', max(30000, len(prompt) // 3))
        errors = []
        previous = plan_history() if task_id == 'project-ref-again' else None
        for model in z[:3]:
            prompt_ = prompt
            for attempt in range(2):   # B252b: a validation error goes back to the same link ONCE with the error text
                try:
                    raw = _planner_call(store, main_rid, model, prompt_, task_id)
                    # B250: a list that passes the rough check but fails full validation (truncated last item) is left to the next link
                    if previous is not None:
                        again_plan_merge(previous, raw)
                    else:
                        _verify(_raw_list(raw))
                    return raw
                except ValueError as exc:
                    if str(exc) in ('plan-review-source-modified', 'plan-review-session-unverified',
                                    'request-or-plan-change-gate', 'request-plan-context-limit', 'plan-review-owner-active'):
                        raise
                    errors.append(f'{model}: {str(exc)[:120]}')
                    if attempt:
                        break
                    prompt_ = (prompt + f"\n\nYOUR PREVIOUS ANSWER WAS INVALID ({exc}). Give dependencies only to ids in this list or to the protected ones, "
                              "every item must have id/title/description/acceptance/qa_command filled in; ONLY a single JSON block.")
                except PermissionError:
                    raise
                except Exception as exc:   # one link's failure does not prevent trying the others
                    errors.append(f'{model}: {str(exc)[:120]}')
                    break
        raise RuntimeError('the planner did not give a valid list: ' + '; '.join(errors))
    return plan


CAPACITY_WAIT_CAP = 3   # B241: at most this many judge-capacity waits for one child


def _consumed(store: Any, main_rid: str, cid: str) -> bool:
    """Has the parent recorded this child's result in `project_mode.json` (G-P4 single writer; re-entry is idempotent)."""
    try:
        d = json.loads((store.path(main_rid) / 'project_mode.json').read_text())
        return any(c.get('run') == cid for c in d.get('children') or [])
    except (OSError, ValueError, AttributeError):
        return False


def child_execute_real(store: Any, main_rid: str, child_project_ref: str, prompt: str, base_ref: str,
                          execute: Callable[[Any, str], int] | None = None) -> dict[str, Any]:
    """A child run opens from the tip of the intermediate branch and runs in the same process; an interrupted child (same prompt) continues without opening a new run."""
    import hashlib
    from run_store import TERMINAL, atomic_json, execute_run
    execute = execute or execute_run
    active_path = store.path(main_rid) / 'project-ref-active.json'
    signature = hashlib.sha256(prompt.encode()).hexdigest()
    active = json.loads(active_path.read_text()) if active_path.exists() else {}
    cid = active.get('child') if active.get('signature') == signature else None
    if cid:
        parent = store.read(main_rid)
        store.update(cid, project_ref_parent=main_rid, project_ref_feature=parent.get('project_active_feature'))
        store.update(main_rid, project_active_run=cid, project_run_ids=sorted(set(parent.get('project_run_ids') or [main_rid]) | {cid}))
    if cid and store.read(cid).get('state') in TERMINAL and not _consumed(store, main_rid, cid):
        return store.read(cid)   # B292: the parent was interrupted before recording this result; the same job does not run twice
    if not cid or store.read(cid).get('state') in TERMINAL:
        cid = store.accept(child_project_ref, prompt, base_ref=base_ref, parent_run_id=main_rid)['run_id']
        parent = store.read(main_rid)
        feature = parent.get('project_active_feature')
        store.update(cid, project_ref_parent=main_rid, project_ref_feature=feature)
        store.update(main_rid, project_active_run=cid, project_run_ids=sorted(set(parent.get('project_run_ids') or [main_rid]) | {cid}))
        atomic_json(active_path, {'signature': signature, 'child': cid})
    parent = store.read(main_rid)
    supervised = parent.get('supervision') == STEP_POLICY
    fid = parent.get('project_active_feature')
    try:
        state = json.loads((store.path(main_rid) / 'project_mode.json').read_text())
    except (OSError, ValueError):
        state = {}
    if fid:
        feature = next(f for f in state['features'] if f['id'] == fid)
        store.update(cid, feature_acceptance={k: feature.get(k) for k in ('id', 'acceptance', 'qa_command', 'risky',
                                                                        'source_tasks', 'source_items', 'source_acceptance')})
    else:
        extra = [c for c in (_acceptance_command(f) for f in state.get('features') or [] if f.get('status') == 'stuck') if c]
        if extra:
            store.update(cid, extra_acceptance_commands=extra)
    try:
        end = float(state['start']) + DURATION_CAP_SEC
    except (KeyError, TypeError, ValueError):
        end = float('inf')
    execute(store, cid)
    last = store.read(cid)
    round = 0
    # B268: CADRE_WAIT_THRESHOLD_SEC=0 disables waiting (e2e fixtures hung on the clock);
    # wait_threshold=0 -> no sleep but the B241/B273 retry logic runs, at most 1 round.
    wait_threshold = int(os.environ.get("CADRE_WAIT_THRESHOLD_SEC", "30"))
    retry_cap = 1 if wait_threshold == 0 else CAPACITY_WAIT_CAP
    # B241: a child waiting for judge capacity (retryable + a future retry_after) is NOT failed; when the share renews
    # the same run continues (lanes that produced are skipped, acceptance again). Otherwise a passed feature was redone from scratch.
    from run_store import run_cap_exhausted
    while not supervised and last.get('state') == 'retryable' and float(last.get('retry_after') or 0) > time.time() and time.time() < end and not _alarm_waiting(last) and not run_cap_exhausted(last, store.home) and ((wait_threshold != 0 and str(last.get('reason') or '').startswith('budget-wait')) or round < retry_cap):   # if waiting is disabled, the budget wait is also limited to the round
        target = min(float(last['retry_after']), end)
        while time.time() < target and not (store.path(main_rid) / 'cancel.flag').exists():
            if wait_threshold == 0:
                break   # no wait; try immediately
            time.sleep(min(60.0, max(0.1, target - time.time())))
        if (store.path(main_rid) / 'cancel.flag').exists() or time.time() >= end:
            break
        round += 1
        execute(store, cid)
        last = store.read(cid)
    if _alarm_waiting(last):
        store.update(main_rid, spend_alarm=last['spend_alarm'])
        return last
    if wait_reason(last) and time.time() < end:
        if last.get('retry_after'):
            store.update(main_rid, retry_after=last['retry_after'])
        return last
    # B273: the coordinator leaves 'retryable' with rc!=0 (no retry_after); a normal run is retried overnight via P4,
    # but a child counted as 'feature failed' after a single attempt. ONE retry allowance for the same run (if not cancelled).
    if not supervised and last.get('state') == 'retryable' and not last.get('retry_after') and not (store.path(main_rid) / 'cancel.flag').exists():
        store.update(cid, state='accepted', reason='', retry_count=int(last.get('retry_count') or 0) + 1)
        execute(store, cid)
        last = store.read(cid)
    if last.get('state') not in TERMINAL and time.time() >= end:
        last = store.update(cid, state='failed', reason=f"project-ref-duration-cap: {last.get('reason') or ''}".strip())
    if last.get('state') not in TERMINAL:   # must not hold the next child's queue; the decision is in the project loop
        last = store.update(cid, state='failed', reason=f"project-ref-feature-failed: {last.get('state')} {last.get('reason') or ''}".strip())
    return last


def _alarm_waiting(k: dict[str, Any]) -> bool:
    alarm = k.get('spend_alarm') or {}
    return bool(alarm) and float(alarm.get('ts') or 0) > float(k.get('spend_approval_ts') or 0)


def wait_reason(k: dict[str, Any]) -> str:
    if k.get('state') == 'retryable' and str(k.get('tool_boundary_wait') or '').startswith('project-tool-'):
        return k['tool_boundary_wait']
    if _alarm_waiting(k):
        return 'spend-alarm'
    if k.get('state') == 'retryable' and float(k.get('retry_after') or 0) > time.time():
        return 'capacity-wait'
    return ''


MAP_LINE = 80


def _map(repo: str | Path) -> str:
    """N-REP03/N-PL01: in a large repo, map truncation is visible in the prompt; the map is not evidence of understanding."""
    lines = _git(repo, 'ls-files', check=False).stdout.splitlines()
    title = f'({len(lines)} files' + (f'; first {MAP_LINE} shown, TRIMMED' if len(lines) > MAP_LINE else '') + ')'
    return '\n'.join([title, *lines[:MAP_LINE]])


def suitable(record: dict[str, Any]) -> bool:
    """G-P1/N-RUN01: a supervised code or ultra (planner ≥66) job is split sequentially; a project child is not split again."""
    if record.get('supervision') == STEP_POLICY and record.get('job_type') == 'code' and not record.get('project_ref_parent'):
        return True
    k = record.get('scope') or {}
    try:
        planner = int(k.get('planner'))
    except (TypeError, ValueError):
        return False
    return k.get('level') == 'ultra' and planner >= 66 and not record.get('project_ref_parent')


def start(run_id: str, project_ref_name: str, *, plan: Callable[[str, str], str] | None = None,
           execute_fn: Callable[[Any, str], int] | None = None) -> int:
    from run_store import RunStore, atomic_json
    store = RunStore()
    record = store.read(run_id)
    if supervision_waiting(store, record):
        return 78
    blocked = entry_obstacle(record)
    if blocked is not None:
        if blocked == 75:
            store.update(run_id, reason='project-ref-stopped: spend-alarm')
        return blocked
    repo = Path((store.home / 'projects' / project_ref_name / 'repo-path').read_text().strip())
    path = store.path(run_id) / 'project_mode.json'
    request = Path(record['prompt']).read_text(errors='replace')
    if not path.exists():
        atomic_json(path, _fresh_status(run_id, request, time.time()))   # identity and intermediate branch are pinned at the first moment
    d0 = json.loads(path.read_text())
    project_ref_budget_pin(store, run_id, d0['start'])
    from run_store import project_time_expired
    if project_time_expired(store.read(run_id)):
        store.update(run_id, reason='project-ref-stopped: duration-cap')
        return 78
    try:
        accepted = (record.get('project_source_commit') or {}).get('sha') or record.get('source_sha') or 'HEAD'
        intermediate_branch_prepare(repo, d0['intermediate_branch'], accepted, {} if record.get('project_source_commit') else record.get('source') or {})
    except PermissionError as exc:
        git_commit_wait(store, run_id, repo)
        d0.update(status='stopped', reason=str(exc))
        atomic_json(path, d0)
        store.update(run_id, reason='project-ref-stopped: ' + str(exc))
        return 78
    from run_store import FileLock, planning_environment, base_qa_save
    import qe_core as core
    with FileLock(store.path(run_id) / '.project-baseline.lock'), planning_environment(store):
        os.environ.update(store.environment(run_id))
        baseline_source = store.path(run_id) / 'baseline-source'
        if not baseline_source.exists():
            _git(repo, 'worktree', 'add', '--detach', str(baseline_source), accepted)
        baseline = core.base_preparation(project_ref_name, baseline_source, store.path(run_id) / 'baseline.log',
            store.path(run_id) / 'base-qa.json', store.home / 'projects' / project_ref_name)
        base_qa_save(store, run_id, baseline)
        if baseline.get('status') != 'PASS':
            store.update(run_id, reason=baseline.get('reason') or 'project-baseline-unavailable')
            return 1
    extras = request_extras(repo, d0['intermediate_branch'], request)
    child_project_ref = derive_project_ref(store.home, project_ref_name, d0['id'])
    store.update(run_id, project_ref={'id': d0['id'], 'intermediate_branch': d0['intermediate_branch'], 'child_project_ref': child_project_ref})
    rc = execute(store, run_id, plan=plan or plan_real(store, run_id),
               child_execute=lambda prompt, branch: child_execute_real(store, run_id, child_project_ref, prompt, branch, execute_fn),
               merge=lambda branch, k: merge_ff(repo, branch, k, store=store, run_id=run_id), map=_map(repo), extras=extras)
    d = json.loads(path.read_text())
    if rc != 0 and d.get('reason') != 'cancel':
        store.update(run_id, reason=f"project-ref-stopped: {d.get('reason')}")
    return rc


def status_summary(store: Any, project_ref_name: str) -> str:
    """One line for `cadre status`: progress of the project's newest project run; empty without a project run."""
    runs = [r for r in store.list(project_ref_name) if r.get('project_ref')]
    if not runs:
        return ''
    last = max(runs, key=lambda r: r.get('created') or 0)
    path = store.path(last['run_id']) / 'project_mode.json'
    if not path.exists():
        return f"{last['project_ref'].get('intermediate_branch', '?')} · preparing"
    d = status_view(last, json.loads(path.read_text()))
    list = d.get('features') or []
    passing = sum(1 for f in list if f.get('status') == 'passed')
    d['expense'] = project_ref_cost(store.home, [last['run_id']])
    return (f"{d.get('intermediate_branch', '?')} · {passing}/{len(list)} passed · {d.get('status', '?')}"
            + (f" ({d['reason']})" if d.get('reason') else '') + (f" · {d['stage_label']}" if d.get('stage_label') else '') + ' · ' + _spend_line(d)
            + f" · report {store.path(last['run_id']) / 'project-ref-report.md'}")


if __name__ == '__main__':
    import sys
    if sys.argv[1:2] == ['suitable'] and len(sys.argv) >= 3:
        from run_store import RunStore
        raise SystemExit(0 if suitable(RunStore().read(sys.argv[2])) else 1)
    if sys.argv[1:2] == ['summary'] and len(sys.argv) >= 3:
        from run_store import RunStore
        print(status_summary(RunStore(), sys.argv[2]))
        raise SystemExit(0)
    if sys.argv[1:2] == ['start'] and len(sys.argv) >= 4:
        raise SystemExit(start(sys.argv[2], sys.argv[3]))
    print(__doc__)
    raise SystemExit(2)
