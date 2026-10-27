"""Cost and time breakdown per job (N-BU07/N-OBS02, CR-40). The ledger is the only source of money (no second ledger);
cash and subscription equivalents are summed separately; a value that cannot be measured stays unknown, never written as zero."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any

from run_store import RunStore, bounded_json, identifier

STAGES = ('plan', 'plan-review', 'impl', 'qa', 'quality', 'repair', 'helper', 'assurance', 'other')
CASH = frozenset({'opencode', 'openrouter'})
EQUIVALENT = frozenset({'opencode-go', 'claude'})
SERVICE_FEE = 'separate; not in the ledger — credit loading/BYOK/tool fees live in the account record and are not added to model spending'


def stage_label(meta: dict[str, Any]) -> str:
    task = str(meta.get('task_id') or meta.get('task') or '')
    role = str(meta.get('phase_role') or meta.get('role') or '')
    if meta.get('assurance') and meta.get('assurance_role'):
        return 'assurance'
    if task.startswith('plan-review') or role == 'review':
        return 'plan-review'
    if task.startswith('plan') or role == 'plan':
        return 'plan'
    if task.startswith('correction') or role in ('recovery', 'repair'):
        return 'repair'
    if task == 'qa' or role == 'qa':
        return 'qa'
    if task == 'quality' or role == 'quality':
        return 'quality'
    if str(meta.get('slice') or '') == 'helper' or role in ('scout', 'cadre-scout', 'compaction', 'autopsy', 'title', 'summary'):
        return 'helper'
    if role in ('impl', 'build', 'cadre-impl', 'merge') or str(meta.get('slice') or 'lanes') == 'lanes':
        return 'impl'
    return 'other'


def _empty_token() -> dict[str, int]:
    return {'measured_request': 0, 'unknown_request': 0, 'input': 0, 'output': 0, 'thinking': 0, 'thinking_unknown': 0,
            'cache_reading': 0, 'cache_write': 0, 'cache_unknown': 0, 'cache_write_unknown': 0}


def _empty() -> dict[str, Any]:
    return {'cash': {'settled': 0, 'estimated': 0, 'pending': 0}, 'equivalent': {'settled': 0, 'estimated': 0, 'pending': 0},
            'unknown_unit': 0, 'request': 0, 'token': _empty_token()}


def _number(value: Any) -> bool:
    return type(value) is int and value >= 0


def token_add(target: dict[str, int], usage: Any) -> bool:
    """N-TS05 (CR-52): a request is measured only if the receipt's input+output were measured; thinking is inside the output, not added separately."""
    if not isinstance(usage, dict) or not (_number(usage.get('input')) and _number(usage.get('output'))):
        target['unknown_request'] += 1
        return False
    target['measured_request'] += 1
    target['input'] += usage['input']
    target['output'] += usage['output']
    if _number(usage.get('thinking')):
        target['thinking'] += usage['thinking']
    else:
        target['thinking_unknown'] += 1
    if _number(usage.get('cache_reading')):
        target['cache_reading'] += usage['cache_reading']
    else:
        target['cache_unknown'] += 1
    if _number(usage.get('cache_write')):
        target['cache_write'] += usage['cache_write']
    else:
        target['cache_write_unknown'] += 1
    return True


def family(store: RunStore, run_id: str) -> list[str]:
    ids = {run_id}
    try:
        record = store.read(run_id)
        ids.update(str(x) for x in (record.get('project_run_ids') or []) if isinstance(x, str))
    except (OSError, ValueError, KeyError, TypeError):
        return sorted(ids)
    for candidate in store.list():
        if candidate.get('project_ref_parent') in ids:
            ids.add(str(candidate['run_id']))
    return sorted(ids)


def expense(home: Path, ids: list[str]) -> dict[str, Any]:
    path = home / 'chain/request-budget.db'
    stages = {name: _empty() for name in STAGES}
    if not path.is_file():
        return {'source': 'unknown', 'stages': stages, 'broken': 0, 'note': 'request ledger absent; amount unknown'}
    marks = ','.join('?' * len(ids))
    endpoints: dict[str, int] = {}
    prefix = {'proceed': 0, 'broken': 0, 'first_or_unknown': 0}
    helper: dict[str, int] = {}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as con:
            rows = con.execute("select provider,reserved,actual,receipt,metadata from reservation where "
                               f"json_valid(metadata) and json_extract(metadata,'$.run_id') in ({marks})", ids).fetchall()
            broken = con.execute('select count(*) from reservation where not json_valid(metadata)').fetchone()[0]
    except sqlite3.Error:
        return {'source': 'unknown', 'stages': stages, 'broken': 0, 'note': 'request ledger unreadable'}
    for provider, reserved, actual, receipt, metadata in rows:
        try:
            meta = json.loads(metadata)
            if type(reserved) is not int or (actual is not None and type(actual) is not int):
                raise ValueError('amount')
        except (ValueError, TypeError):
            broken += 1
            continue
        name = stage_label(meta)
        stage = stages[name]
        stage['request'] += 1
        if name != 'assurance':
            token_add(stage['token'], meta.get('usage'))
            endpoint = (meta.get('usage') or {}).get('endpoint') if isinstance(meta.get('usage'), dict) else None
            endpoints[endpoint if isinstance(endpoint, str) and endpoint else 'unknown'] = endpoints.get(endpoint if isinstance(endpoint, str) and endpoint else 'unknown', 0) + 1
            proceed = (meta.get('prefix') or {}).get('proceed') if isinstance(meta.get('prefix'), dict) else None
            prefix['proceed' if proceed is True else 'broken' if proceed is False else 'first_or_unknown'] += 1
        if name == 'helper':
            role = str(meta.get('role') or meta.get('phase_role') or 'unknown')
            helper[role] = helper.get(role, 0) + 1
        pool = 'cash' if provider in CASH else 'equivalent' if provider in EQUIVALENT else None
        if pool is None:
            stage['unknown_unit'] += 1
            continue
        if actual is None:
            stage[pool]['pending'] += reserved
        elif str(receipt or '').startswith(('phantom:', 'recovery:')):
            stage[pool]['estimated'] += actual
        else:
            stage[pool]['settled'] += actual
    return {'source': 'request-ledger', 'stages': stages, 'broken': broken, 'line': len(rows), 'endpoints': endpoints, 'prefix': prefix,
            'helper_calls': dict(sorted(helper.items()))}


def token_summary(cost: dict[str, Any]) -> Any:
    total = _empty_token()
    for stage in cost['stages'].values():
        for key, value in stage['token'].items():
            total[key] += value
    if not total['measured_request']:
        return 'unknown'
    return {'source': 'request-ledger usage', **total, 'endpoints': cost.get('endpoints') or {}, 'prefix': cost.get('prefix') or {},
            'note': 'thinking is inside output; cache read/write is inside input; the fee is the receipt amount, it is not re-summed from tokens'}


def duration(store: RunStore, ids: list[str], root_id: str | None = None) -> dict[str, Any]:
    root_id = root_id or ids[0]
    work: dict[str, float] = {}
    queue, waits, setup, unknown_attempts = None, None, None, 0
    first_attempt = None
    for rid in ids:
        try:
            record = store.read(rid)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        created = record.get('created') if type(record.get('created')) in (int, float) else None
        if isinstance(record.get('budget_waits'), list):
            waits = waits or 0.0
            for window in record['budget_waits']:
                if isinstance(window, dict) and type(window.get('ts')) in (int, float) and type(window.get('until')) in (int, float):
                    waits += max(0.0, float(window['until']) - float(window['ts']))
        marker = store.path(rid) / 'base-qa.json'
        if marker.is_file():
            try:
                seconds = (bounded_json(marker).get('install') or {}).get('seconds')
                if type(seconds) in (int, float):
                    setup = (setup or 0.0) + float(seconds)
            except (OSError, ValueError, TypeError):
                pass
        for path in sorted((store.path(rid) / 'tasks').glob('*/attempts/*/attempt.json')):
            try:
                attempt = bounded_json(path)
            except (OSError, ValueError):
                unknown_attempts += 1
                continue
            start, end = attempt.get('created'), attempt.get('finished')
            if type(start) not in (int, float):
                unknown_attempts += 1
                continue
            if rid == root_id and created is not None:
                first_attempt = start if first_attempt is None else min(first_attempt, start)
            if type(end) in (int, float) and end >= start:
                role = stage_label({'task_id': attempt.get('task_id'), 'role': attempt.get('role')})
                work[role] = work.get(role, 0.0) + float(end - start)
            else:
                unknown_attempts += 1
    try:
        root = store.read(root_id)
        created = root.get('created')
        if first_attempt is not None and type(created) in (int, float):
            queue = max(0.0, float(first_attempt) - float(created))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return {'operation_sec': {k: round(v, 3) for k, v in sorted(work.items())}, 'queue_sec': round(queue, 3) if queue is not None else None,
            'quota_wait_sec': round(waits, 3) if waits is not None else None, 'install_sec': round(setup, 3) if setup is not None else None,
            'uncertain_attempt': unknown_attempts, 'note': 'wait time is not model slowness; token/cache missing from the ledger means unknown'}


def breakdown(home: Path, run_id: str) -> dict[str, Any]:
    store = RunStore(home)
    identifier(run_id)
    root = store.read(run_id)
    ids = family(store, run_id)
    cost = expense(home, ids)
    def failures(rid: str) -> bool:
        record = store.read(rid)
        status = (record.get('final_result') or {}).get('status')
        return status not in (None, 'PASS', 'APPLIED') or (status is None and record.get('state') in ('failed', 'cancelled'))
    failed = [rid for rid in ids if rid != run_id and failures(rid)]
    failed_cost = expense(home, failed) if failed else None
    result = (root.get('final_result') or {}).get('status')
    return {'version': 1, 'run_id': run_id, 'scope': ids, 'acceptance': {'status': result or 'unknown', 'acceptance_done': result == 'PASS'},
            'expense': cost, 'failed_child': {'runs': failed, 'expense': failed_cost},
            'duration': duration(store, ids, run_id), 'token': token_summary(cost), 'subscription_price': 'separate; not in the ledger',
            'service_fee': SERVICE_FEE, 'helper_calls': cost.get('helper_calls') or {},
            'intervention': __import__('run_store').intervention_summary(root), 'ts': time.time()}


def campaign(home: Path, run_ids: list[str]) -> dict[str, Any]:
    """N-TS05 (CR-52, TSR-03): campaign total cost / accepted jobs; failed jobs and child costs stay in the total, without an acceptance the ratio is undefined.
    Without a readable ledger total and ratio are unknown; with a broken line or an unknown unit the scope is written as partial."""
    store = RunStore(home)
    for rid in run_ids:
        identifier(rid)
    families = {rid: set(family(store, rid)) for rid in dict.fromkeys(run_ids)}
    jobs = sorted(rid for rid in families if not any(rid in members and other != rid for other, members in families.items()))
    scope = sorted(set().union(*(families[rid] for rid in jobs))) if jobs else []
    cost = expense(home, scope) if scope else {'source': 'unknown', 'stages': {}}
    known = cost['source'] == 'request-ledger'
    total = {'cash': {'settled': 0, 'estimated': 0, 'pending': 0}, 'equivalent': {'settled': 0, 'estimated': 0, 'pending': 0},
              'unknown_unit': 0, 'request': 0}
    for stage in cost['stages'].values():
        for pool in ('cash', 'equivalent'):
            for key in ('settled', 'estimated', 'pending'):
                total[pool][key] += stage[pool][key]
        total['unknown_unit'] += stage['unknown_unit']
        total['request'] += stage['request']
    acceptance = [rid for rid in jobs if (store.read(rid).get('final_result') or {}).get('status') == 'PASS']
    missing = {'broken_line': cost.get('broken', 0), 'unknown_unit': total['unknown_unit']}
    scope_status = ('partial' if any(missing.values()) else 'complete') if known else 'unknown'
    ratio: Any = 'undefined'
    if acceptance and not known:
        ratio = 'unknown'
    elif acceptance:
        ratio = {f'{pool}_{key}_usd': round(total[pool][key] / 1e6 / len(acceptance), 6) for pool in ('cash', 'equivalent') for key in ('settled', 'estimated')}
        ratio['pending_separate_usd'] = round((total['cash']['pending'] + total['equivalent']['pending']) / 1e6, 6)
        ratio['scope'] = scope_status
    return {'version': 1, 'jobs': jobs, 'scope': scope, 'acceptance_done': len(acceptance), 'failed_jobs': sorted(set(jobs) - set(acceptance)),
            'total': total if known else 'unknown', 'acceptance_per': ratio, 'source': cost['source'],
            'expense_scope': {'status': scope_status, **missing} if known else {'status': 'unknown', 'note': cost.get('note') or 'scope empty'},
            'token': token_summary(cost) if scope else 'unknown',
            'service_fee': SERVICE_FEE, 'subscription_price': 'separate; not in the ledger',
            'note': 'cash and equivalent are separate; pending amounts are not part of the ratio; unknown units are not summed; a small sample is no general guarantee',
            'ts': time.time()}


def summary_lines(data: dict[str, Any]) -> list[str]:
    """FIX-44: a value that cannot be measured stays 'unknown', never written as zero; labels are translated with t()."""
    from ui_messages import t as convert
    def t(text: str, **values: object) -> str:
        return convert(text, **values).lower()
    lines = []
    unknown = t('Unknown')
    for name, stage in data['expense']['stages'].items():
        if not stage['request']:
            continue
        if stage['unknown_unit'] >= stage['request']:
            lines.append(f"{name}: {t('{n} requests in an unknown unit, amount unknown', n=stage['request'])}")
            continue
        cash, eq = stage['cash'], stage['equivalent']
        unit = f" · {t('{n} requests in an unknown unit', n=stage['unknown_unit'])}" if stage['unknown_unit'] else ''
        lines.append(f"{name}: {t('Cash')} ${cash['settled'] / 1e6:.4f} {t('Settled')} / ${cash['estimated'] / 1e6:.4f} {t('Estimated')} / ${cash['pending'] / 1e6:.4f} {t('Pending')}"
                     f" · {t('Equivalent')} ${eq['settled'] / 1e6:.4f} / ${eq['estimated'] / 1e6:.4f} / ${eq['pending'] / 1e6:.4f} · {t('{n} requests', n=stage['request'])}{unit}")
    token = data.get('token')
    if isinstance(token, dict):
        measured = token['measured_request']
        total = measured + token['unknown_request']
        def field(value: int, missing: int) -> str:
            if missing >= measured:
                return unknown
            return f"{value} [{t('In {a}/{n} requests', a=measured - missing, n=measured)}]" if missing else str(value)
        lines.append(f"{t('Tokens')}: {t('Measured in {a}/{n} requests', a=measured, n=total)} · {t('Input')} {token['input']}"
                     f" ({t('Cache read')} {field(token['cache_reading'], token['cache_unknown'])},"
                     f" {t('Write')} {field(token['cache_write'], token.get('cache_write_unknown', measured))})"
                     f" · {t('Output')} {token['output']} ({t('Reasoning')} {field(token['thinking'], token['thinking_unknown'])})")
    elif token == 'unknown' and any(stage['request'] for stage in data['expense']['stages'].values()):
        lines.append(f"{t('Tokens')}: {unknown}")
    s = data['duration']
    quota = f"{s['quota_wait_sec']:.0f}s" if s.get('quota_wait_sec') is not None else unknown
    lines.append(f"{t('Time')}: " + ', '.join(f'{k} {v:.0f}s' for k, v in s['operation_sec'].items())
                 + f" · {t('Queue')} {s['queue_sec'] if s['queue_sec'] is not None else unknown}"
                 + f" · {t('Quota wait')} {quota} · {t('Setup')} {s['install_sec'] if s['install_sec'] is not None else unknown}")
    return lines


def main(argv: list[str]) -> int:
    campaign_mode = bool(argv) and argv[0] == '--campaign'
    if (campaign_mode and len(argv) < 2) or (not campaign_mode and len(argv) != 1):
        print('usage: cost_report.py RUN_ID | cost_report.py --campaign RUN_ID...', file=sys.stderr)
        return 2
    import os
    home = Path(os.environ.get('CADRE_HOME', str(Path.home() / '.local/share/cadre-engineering')))
    try:
        data = campaign(home, argv[1:]) if campaign_mode else breakdown(home, argv[0])
        print(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'cost_report unreadable: {type(exc).__name__}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
