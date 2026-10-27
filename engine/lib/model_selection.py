"""Versioned difficulty/preference policy that orders a role's allowed model pool (D16).
Measurements are reported; the model order never changes by itself. A suitable cheap route of the same model comes first."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import json
import hashlib
import math
import re
from pathlib import Path
import time
from typing import Any

from model_catalog import canonical_model, model as catalog_model, price
from run_store import FileLock, atomic_json

MEASUREMENT_LIFETIME = 7 * 86400
POLICY_VERSION = 'difficulty-preference-role-v1'
DIFFICULTIES = ('easy', 'medium', 'hard', 'very_hard')


def task_selection(prompt: str, values: dict[str, Any]) -> dict[str, Any]:
    from level import rule_base
    score = rule_base(prompt)
    estimate = DIFFICULTIES[0 if score <= 15 else 1 if score <= 40 else 2 if score <= 65 else 3]
    preference = values['preference']
    override = values['difficulty_override']
    if preference not in ('economic', 'balanced', 'strong') or override not in (None, *DIFFICULTIES):
        raise ValueError('invalid selection input')
    return {'policy_version': POLICY_VERSION, 'preference': preference,
            'difficulty_estimate': estimate, 'estimate_score': score, 'estimate_source': 'request-rules',
            'estimate_reason': f'rule_base={score}', 'difficulty_override': override,
            'difficulty_effective': override or estimate,
            'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest()}


def choice_verify(value: Any) -> dict[str, Any]:
    required = {'policy_version', 'preference', 'difficulty_estimate', 'estimate_score', 'estimate_source',
                'estimate_reason', 'difficulty_override', 'difficulty_effective', 'prompt_sha256'}
    if (not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - {'parent_run_id'}
            or value.get('policy_version') != POLICY_VERSION
            or value.get('preference') not in ('economic', 'balanced', 'strong')
            or value.get('difficulty_estimate') not in DIFFICULTIES
            or value.get('difficulty_override') not in (None, *DIFFICULTIES)
            or value.get('difficulty_effective') != (value.get('difficulty_override') or value.get('difficulty_estimate'))
            or type(value.get('estimate_score')) is not int or not 0 <= value['estimate_score'] <= 100
            or value.get('estimate_source') != 'request-rules'
            or value.get('estimate_reason') != f"rule_base={value.get('estimate_score')}"
            or not isinstance(value.get('prompt_sha256'), str) or not re.fullmatch('[0-9a-f]{64}', value['prompt_sha256'])):
        raise ValueError('invalid selection policy')
    score = value['estimate_score']
    if value['difficulty_estimate'] != DIFFICULTIES[0 if score <= 15 else 1 if score <= 40 else 2 if score <= 65 else 3]:
        raise ValueError('invalid difficulty estimate')
    if 'parent_run_id' in value:
        from run_store import identifier
        identifier(value['parent_run_id'])
    return dict(value)


def run_selection(home: Path, run_id: str) -> dict[str, Any] | None:
    from run_store import RunStore
    record = RunStore(home).read(run_id)
    if 'model_selection' not in record:
        if record.get('request'):
            raise ValueError('selection policy missing')
        return None
    value = choice_verify(record['model_selection'])
    request = record.get('request')
    if request and (any(value[key] != request[key] for key in ('preference', 'difficulty_override'))
                    or value['prompt_sha256'] != hashlib.sha256(request['prompt'].encode()).hexdigest()):
        raise ValueError('selection input mismatch')
    scope = record.get('scope') or {}
    score = scope.get('points')
    if score is not None:
        if type(score) is not int or not 0 <= score <= 100:
            raise ValueError('invalid plan difficulty')
        value['plan_score'] = score
        if value['difficulty_override'] is None:
            score = max(score, value['estimate_score'])
            value['difficulty_effective'] = DIFFICULTIES[0 if score <= 15 else 1 if score <= 40 else 2 if score <= 65 else 3]
    return value


def policy_order(candidates: list[str], role: str, selection: dict[str, Any]) -> tuple[list[str], str]:
    preference, difficulty = selection['preference'], selection['difficulty_effective']
    economical = (preference == 'economic' or preference == 'balanced' and difficulty == 'easy') and difficulty in ('easy', 'medium')
    economical = economical and role not in ('recovery', 'merge')
    ordered = sorted(candidates, key=lambda entry: 0 if any(canonical_model(route) == 'glm53' for route in entry.split('|')) else 1) if economical else list(candidates)
    reason = (f"{POLICY_VERSION}: preference={preference}, difficulty={difficulty}, role={role}, "
              f"rule={'approved-glm-first' if economical else 'approved-role-order'}")
    return ordered, reason


EFFORT_LEVELS = ('none', 'minimal', 'low', 'medium')


def effort_selection(selection: dict[str, Any] | None, role: str, error_count: int, task: str = '') -> dict[str, str] | None:
    """N-TS03 (CR-50): explicit, versioned and reversible economy effort; off by default. Plan/review/judge/repair,
    hard work and a retry after an error run with the default effort."""
    from limits import EFFORT
    level = EFFORT.get('easy_lane')
    if level not in EFFORT_LEVELS or not isinstance(EFFORT.get('version'), str) or not selection:
        return None
    if role != 'impl' or error_count or str(task).startswith(('correction', 'plan', 'quality', 'qa', 'merge')):
        return None
    if selection.get('difficulty_effective') != 'easy' or selection.get('preference') not in ('economic', 'balanced'):
        return None
    return {'policy': EFFORT['version'], 'level': level}


def call_cap(target: str, token: int, output: int, home: Path) -> int:
    provider, model = target.split('/', 1)
    data = catalog_model(provider, model)
    if not isinstance(data, dict):
        raise ValueError('model absent from catalog')
    limit = data.get('limit') or {}
    if type(limit.get('output')) is not int or limit['output'] <= 0:
        raise ValueError('output capacity unknown or invalid')
    output = min(output, limit['output'])
    if type(limit.get('context')) is not int or limit['context'] < token + output:
        raise ValueError('context capacity unknown or insufficient')
    costs = data.get('cost') or {}
    if provider == 'openrouter':
        from budget_gate import or_pin_price
        if not or_pin_price(home, model, output):
            raise ValueError('OR-PIN price missing')
        costs = json.loads((home / 'chain/openrouter-price.json').read_text())['models'][model]
    try:
        rows = [costs, *(costs.get('tiers') or [])]
        inputs = [Decimal(str(row['input'])) for row in rows]
        outputs = [Decimal(str(row['output'])) for row in rows]
        writes = [Decimal(str(row.get('cache_write', 0))) for row in rows]
        if not all(n.is_finite() and n >= 0 for n in (*inputs, *outputs, *writes)):
            raise ValueError('invalid price')
        return int(((max(inputs) + max(writes)) * token + max(outputs) * output).to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation, KeyError, TypeError, OverflowError) as exc:
        raise ValueError('price unknown') from exc


@dataclass(frozen=True)
class ModelMeasurement:
    id: str
    correct: int
    total: int
    second: float

    @property
    def points(self) -> float:
        return (self.correct + 1) / (self.total + 2)


def _records(home: Path) -> list[dict]:
    try:
        data = json.loads((home / 'chain/model-measurements.json').read_text())
        return data['measurements'] if data.get('version') == 1 and isinstance(data.get('measurements'), list) else []
    except (OSError, ValueError, AttributeError):
        return []


def measurement_write(home: Path, target: str, role: str, correct: int, total: int, second: float,
              now_ts: float | None = None) -> None:
    if (type(correct) is not int or type(total) is not int or not 0 <= correct <= total or total <= 0
            or not math.isfinite(second) or second < 0):
        raise ValueError('invalid model measurement')
    path = home / 'chain/model-measurements.json'
    with FileLock(path.with_suffix('.lock')):
        rows = _records(home)
        rows.append({'model': canonical_model(target), 'path': target, 'role': role, 'correct': correct,
                     'total': total, 'second': second, 'ts': time.time() if now_ts is None else now_ts})
        atomic_json(path, {'version': 1, 'measurements': rows[-500:]})


def measurements(home: Path, role: str, now_ts: float) -> dict[str, ModelMeasurement]:
    groups: dict[str, list[dict]] = {}
    kind = 'judge' if role in ('review', 'quality') else 'general'
    for row in _records(home):
        try:
            if (row['role'] != kind or not 0 <= now_ts - float(row['ts']) <= MEASUREMENT_LIFETIME
                    or type(row['total']) is not int or type(row['correct']) is not int
                    or not 0 <= row['correct'] <= row['total'] or row['total'] <= 0
                    or not math.isfinite(float(row['second'])) or row['second'] < 0
                    or not isinstance(row['model'], str)):
                continue
            groups.setdefault(row['model'], []).append(row)
        except (KeyError, TypeError, ValueError):
            continue
    return {key: ModelMeasurement(key, sum(r['correct'] for r in rows), sum(r['total'] for r in rows),
                            sum(r['second'] for r in rows)) for key, rows in groups.items()}


OUTCOME_LIFETIME = 30 * 86400


def outcome_save(home: Path, record: dict[str, Any], result: dict[str, Any], writers: list[str],
                 judge: str | None, planner: str | None, now_ts: float | None = None) -> dict[str, Any] | None:
    """N-RT02 (CR-39): outcome measurement from the production acceptance path. The same run+product+verdict does not count as a second sample; the record
    does not change the selection policy; cost/time that cannot be measured stays 'unknown'."""
    status = str(result.get('status') or '')
    if status not in ('PASS', 'APPLIED', 'FAIL', 'ERROR', 'CANCELLED'):
        return None
    digest = str((result.get('revision') or {}).get('digest') or '')
    selection = record.get('model_selection') if isinstance(record.get('model_selection'), dict) else {}
    now = time.time() if now_ts is None else now_ts
    roles = [('impl', model) for model in sorted(set(writers)) if model]
    roles += [('quality', judge)] if judge else []
    roles += [('plan', planner)] if planner else []
    cost: dict[str, Any] = {'source': 'unknown', 'settled_micro': None, 'pending_micro': None}
    ledger = home / 'chain/request-budget.db'
    if ledger.is_file():
        try:
            import sqlite3
            from contextlib import closing
            from budget_gate import run_usage
            with closing(sqlite3.connect(ledger.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as con:
                settled, held = run_usage(con, str(record['run_id']), bounded=True)
            cost = {'source': 'request-ledger', 'settled_micro': settled, 'pending_micro': held}
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            pass
    created = record.get('created')
    duration = (now - float(created)) if type(created) in (int, float) and 0 < created <= now else None
    try:
        from run_store import run_job_type
        job_type = run_job_type(record)
    except (ValueError, KeyError, TypeError):
        job_type = None
    identity = hashlib.sha256(json.dumps([record['run_id'], digest, status], sort_keys=True).encode()).hexdigest()
    effort = sorted({str(task['effort_policy'].get('policy')) for task in (record.get('tasks') or {}).values()
                   if isinstance(task, dict) and isinstance(task.get('effort_policy'), dict)}) or None
    entry = {'version': 2, 'id': identity, 'run_id': record['run_id'], 'project_ref_parent': record.get('project_ref_parent'),
             'job_kind': job_type, 'outcome': status, 'reason': str(result.get('reason') or '')[:200] or None,
             'product': digest or None, 'source_sha': record.get('source_sha') or None,
             'roles': [{'role': role, 'path': model, 'model': canonical_model(model)} for role, model in roles],
             'policy': selection.get('policy_version'), 'preference': selection.get('preference'),
             'difficulty_estimated': selection.get('difficulty_estimate'), 'difficulty_correction': selection.get('difficulty_override'),
             'difficulty_enabled': selection.get('difficulty_effective'), 'effort': effort,
             'expense': cost, 'age_sec': round(duration, 3) if duration is not None else None, 'ts': now}
    path = home / 'chain/model-outcomes.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(path.with_suffix('.lock')):
        existing = set()
        try:
            for line in path.read_text().splitlines():
                try:
                    existing.add(json.loads(line).get('id'))
                except ValueError:
                    continue
        except OSError:
            pass
        if identity in existing:
            return None
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(entry, ensure_ascii=False, allow_nan=False) + '\n')
    return entry


def outcome_records(home: Path) -> tuple[list[dict[str, Any]], int]:
    rows, broken = [], 0
    try:
        lines = (home / 'chain/model-outcomes.jsonl').read_text().splitlines()
    except OSError:
        return [], 0
    for line in lines:
        try:
            row = json.loads(line)
            if not isinstance(row, dict) or row.get('version') != 2 or not isinstance(row.get('roles'), list):
                raise ValueError('row')
            rows.append(row)
        except ValueError:
            broken += 1
    return rows, broken


def outcome_summary(home: Path, now_ts: float | None = None) -> dict[str, Any]:
    """Report of fresh-old-few-failed samples per role/model/policy; does not affect the selection order."""
    now = time.time() if now_ts is None else now_ts
    rows, broken = outcome_records(home)
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        ts = row.get('ts')
        fresh = type(ts) in (int, float) and 0 <= now - ts <= OUTCOME_LIFETIME
        effort = '+'.join(row['effort']) if isinstance(row.get('effort'), list) and row['effort'] else None
        for role in row['roles']:
            key = (str(role.get('role')), str(role.get('model')), str(row.get('policy')), str(effort))
            group = groups.setdefault(key, {'role': key[0], 'model': key[1], 'policy': key[2], 'effort': effort, 'sample': 0, 'fresh': 0, 'old': 0,
                                            'acceptance': 0, 'failed': 0, 'expense_micro': 0, 'expense_unknown': 0,
                                            'age_sec': 0.0, 'age_unknown': 0})
            group['sample'] += 1
            group['fresh' if fresh else 'old'] += 1
            group['acceptance' if row.get('outcome') in ('PASS', 'APPLIED') else 'failed'] += 1
            cost = row.get('expense') or {}
            if cost.get('source') == 'request-ledger' and type(cost.get('settled_micro')) is int:
                group['expense_micro'] += cost['settled_micro']
            else:
                group['expense_unknown'] += 1
            if type(row.get('age_sec')) in (int, float):
                group['age_sec'] += float(row['age_sec'])
            else:
                group['age_unknown'] += 1
    for group in groups.values():
        group['insufficient'] = group['fresh'] < 5
        group['uncertainty'] = [n for n, ok in (('least-sample', group['insufficient']), ('old-sample', group['old'] > 0),
                                                  ('expense-unknown', group['expense_unknown'] > 0),
                                                  ('age-unknown', group['age_unknown'] > 0)) if ok]
    return {'groups': sorted(groups.values(), key=lambda g: (g['role'], g['model'], g['policy'], str(g['effort']))), 'broken': broken,
            'total': len(rows), 'policies': sorted({str(r.get('policy')) for r in rows}),
            'note': 'measurement does not change the selection; a policy change requires an explicit configuration decision'}


def tool_suitable(target: str) -> bool:
    provider_key, entry_name = target.split('/', 1)
    # An unknown catalogue does not invalidate the approved role table; an explicit rejection is filtered out.
    return (catalog_model(provider_key, entry_name) or {}).get('tool_call') is not False


def path_cost(target: str, token: int) -> float:
    provider_key, entry_name = target.split('/', 1)
    if provider_key == 'opencode-go':
        return 0.0  # GO1: the remaining subscription allowance is used; the window filter is in the router.
    costs = price(provider_key, entry_name)
    if not costs:
        return math.inf
    try:
        input_rate, output_rate = float(costs['input']), float(costs['output'])
        if not all(math.isfinite(n) and n >= 0 for n in (input_rate, output_rate)):
            return math.inf
        return (max(1, token) * input_rate + 4096 * output_rate) / 1e6
    except (KeyError, TypeError, ValueError):
        return math.inf


def sort(candidates: list[str], preference: list[str], role: str, token: int, home: Path,
           now_ts: float) -> tuple[list[str], list[str]]:
    """Cannot undo the existing health/budget filtering; only orders the accepted candidates."""
    groups: dict[str, list[str]] = {}
    reason = []
    for route in candidates:
        if not tool_suitable(route):
            reason.append(f'{route}: tool calls are unsupported')
            continue
        groups.setdefault(canonical_model(route), []).append(route)
    ranks = {}
    for entry in preference:
        for route in entry.split('|'):
            ranks.setdefault(canonical_model(route), len(ranks))
    keys = sorted(groups, key=lambda key: ranks.get(key, len(ranks)))
    measured = measurements(home, role, now_ts)
    result = []
    for key in keys:
        routes = sorted(groups[key], key=lambda route: path_cost(route, token))
        result.extend(routes)
        if key in measured:
            value = measured[key]
            reason.append(f'{key}: measurement {value.correct}/{value.total}, points={value.points:.3f}')
        reason.append(f'{key}: route order {", ".join(routes)}')
    return result, reason
