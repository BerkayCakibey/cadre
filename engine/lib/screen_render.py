"""Shared screen rendering for menu, monitor and summary; no network calls."""
from __future__ import annotations

import math
import re
import unicodedata
from typing import Any

SYMBOL = {'running': '●', 'completed': '✔', 'failed': '✗', 'retryable': '⚑',
         'queued': '○', 'accepted': '○', 'cancelled': '⊘', 'warning': '⚑',
         'task': '▶', 'watch': '▤', 'cancel': '⊘', 'summary': '⌾', 'push': '↑',
         'merge': '⤴', 'recovery': '⚕', 'balance': '$', 'exit': '○',
         'thinking': '✻', 'tool': '⏺', 'patch': '📝', 'compaction': '♻'}
STATUS = {'running': 'running', 'completed': 'finished', 'failed': 'stopped',
         'retryable': 'waiting to resume', 'queued': 'queued', 'accepted': 'preparing',
         'cancelled': 'cancelled'}


def symbol(name: str) -> str:
    return SYMBOL.get(name, SYMBOL['warning'])


def clean(body: Any) -> str:
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)', '', str(body))
    return ''.join(c for c in text if c in '\n\t' or unicodedata.category(c) not in ('Cc', 'Cf'))


def span_width(text: str) -> int:
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1
               for c in clean(text))


def wrap_text(text: str, column: int) -> list[str]:
    """Only wraps the screen; does not change the stored text of the task."""
    rows = []
    limit = max(2, column)
    for source in clean(text).expandtabs(4).split('\n'):
        line, width = '', 0
        for c in source:
            size = span_width(c)
            if line and width + size > limit:
                rows.append(line)
                line, width = '', 0
            line += c
            width += size
        rows.append(line)
    return rows


def money(val: Any) -> str:
    try:
        value = float(val)
        if math.isfinite(value) and value >= 0:
            return f'${value:.2f}'
    except (TypeError, ValueError):
        pass
    return 'no measurement'


def run_summary(record: dict) -> str:
    state = record.get('state', '')
    # plan/QA/judge/correction steps are not plan parts; counting them made finished work show '2/5'
    tasks = [t for k, t in (record.get('tasks') or {}).items()
             if k not in ('plan', 'qa', 'quality', 'merge', 'main', 'helper') and not k.startswith('correction')]
    done = sum(t.get('state') in ('completed', 'produced', 'complete') for t in tasks)
    progress = f'{done}/{len(tasks)} parts finished' if tasks else STATUS.get(state, 'status unknown')
    alarm = record.get('spend_alarm') or {}
    if alarm and float(alarm.get('ts') or 0) > float(record.get('spend_approval_ts') or 0):   # 4b: an alarm waiting for approval comes before everything
        status = f"⚠ spend alarm: {alarm.get('reason')} — approval: cadre spend-approval {record.get('run_id', '')}".rstrip()
    elif state in ('failed', 'retryable', 'cancelled'):
        status = STATUS[state]
    elif record.get('denials') or record.get('budget_held_usd'):
        status = 'one warning'
    elif state == 'completed':
        status = 'no problems'
    else:
        status = STATUS.get(state, 'status unknown')
    return f'{symbol(state)} {progress} · {money(record.get("budget_spent_usd"))} spent' + ('' if status == progress else f' · {status}')


def budget_summary(budget: dict) -> str:
    go = budget.get('go') or {}
    go_text = f"Go {float(go['available']):.0f} points" if go.get('unit') == 'points' and go.get('db') == 'ok' else 'Go ?'
    return ' · '.join([go_text] + [f'{name} {money((budget.get(key) or {}).get("available"))}'
                                    for name, key in [('OR', 'or'), ('Zen', 'zen')]])


def tool_summary(data: dict) -> str:
    state = (data.get('state') or {}).get('status')
    if state == 'error':
        return f'{symbol("failed")} tool returned an error · details: d'
    action = {'read': 'file read', 'glob': 'file search', 'grep': 'text search',
              'bash': 'command run', 'edit': 'file edit', 'write': 'file write',
              'apply_patch': 'patch application', 'task': 'helper task', 'webfetch': 'page fetch', 'websearch': 'web search'}
    return f'{symbol("completed" if state == "completed" else "tool")} ' + action.get(data.get('tool'), 'tool operation') + (
        ' completed' if state == 'completed' else ' in progress')


def price_failures(home) -> list[str]:
    import sqlite3
    from contextlib import closing
    try:
        with closing(sqlite3.connect(f'file:{home / "chain/request-budget.db"}?mode=ro', uri=True)) as con:
            return [str(r[0]) for r in con.execute('select provider from pricing_fault')]
    except sqlite3.Error:
        return []


def delivery_pending(records: list) -> int:
    """Accepted runs not merged yet (same rule as cadre.sh delivery_candidates)."""
    from pathlib import Path
    from outcome import approved
    n = 0
    for x in records:
        # An old run whose worktree was deleted cannot be delivered; calling git for each one slowed down startup.
        if x.get('state') == 'completed' and x.get('worktree') and 'delivery_merge' not in x and Path(x['worktree']).is_dir():
            try:
                n += bool(approved(x))
            except Exception:
                pass
    return n


def budget_fresh(home) -> dict:
    """06.10: the status screen showed "no measurement" while the 60 s cache was empty; provider_budget.py runs once (at most 20 s)."""
    import json, os, subprocess, sys
    from pathlib import Path
    try:
        root = Path(os.environ.get('CADRE_ENGINE_ROOT', str(Path(__file__).resolve().parents[1])))
        r = subprocess.run([sys.executable, str(root / 'lib/provider_budget.py')], capture_output=True, text=True,
                           timeout=20, env={**os.environ, 'CADRE_HOME': str(home)})
        d = json.loads(r.stdout)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


VIEW_AGE_SEC = 24 * 3600   # up to this age the status line shows the last measurement with its age; the decision gate takes its own fresh measurement


def behind_refresh(home) -> None:
    """Refreshes the shared budget observation in the background; startup does not wait for the network."""
    import os, subprocess, sys
    from pathlib import Path
    lib = Path(os.environ.get('CADRE_ENGINE_ROOT', str(Path(__file__).resolve().parents[1]))) / 'lib'
    code = (f'import sys; sys.path.insert(0, {str(lib)!r}); from pathlib import Path; '
           f'from budget_gate import observed_budget; observed_budget(Path({str(home)!r}), Path({str(lib.parent)!r}))')
    try:
        subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, env={**os.environ, 'CADRE_HOME': str(home)})
    except OSError:
        pass


def budget_view(home, refresh: bool = True) -> tuple[dict, float]:
    """(budget, age_s): fresh cache, otherwise the last measurement + a background refresh; with no measurement at all, one synchronous measurement.
    `refresh=False`: only the cache is read, no measurement is started (read-only preview)."""
    import json, time
    try:
        saved = json.loads((home / 'chain/request-budget.json').read_text())
        age = time.time() - float(saved['observed'])
        if isinstance(saved['budget'], dict) and 0 <= age < VIEW_AGE_SEC:
            if age >= 60 and refresh:
                behind_refresh(home)
            return saved['budget'], age
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return (budget_fresh(home), 0.0) if refresh else ({}, 0.0)


def status(project_ref: str) -> str:
    import os
    from pathlib import Path
    from run_store import RunStore
    from morning_summary import stale_reservation
    home = Path(os.environ.get('CADRE_HOME', Path.home() / '.local/share/cadre-engineering'))
    records = RunStore(home).list(project_ref or None)
    lines = [run_summary(records[-1]) if records else f'{symbol("queued")} No jobs yet.']
    view, age = budget_view(home)
    lines.append(f'{symbol("balance")} {budget_summary(view)}' + (f' (measured {int(age // 60)} min ago)' if age >= 60 else ''))
    stale, amount = stale_reservation(home)
    if stale:
        lines.append(f'{symbol("warning")} {stale} stale reservation(s) ({money(amount)}) · cadre recover')
    failure = price_failures(home)
    if failure:
        lines.append(f'{symbol("warning")} price failure, provider closed: {", ".join(failure)} · cadre recover --failure-list')
    delivery = delivery_pending(records)
    if delivery:
        lines.append(f'{symbol("push")} Finished jobs awaiting approval: {delivery} · cadre push / cadre merge (every run starts from the main branch)')
    lines.append('Details: cadre status --detail · Watch: cadre watch')
    return '\n'.join(lines)


if __name__ == '__main__':
    import sys
    if sys.argv[1] == 'symbol':
        print(symbol(sys.argv[2]))
    elif sys.argv[1] == 'status':
        print(status(sys.argv[2] if len(sys.argv) > 2 else ''))
