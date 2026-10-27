#!/usr/bin/env python3
"""Estimate vs actual: the estimate is never invented, it is measured from past completed runs; without enough samples there is no estimate.
The deviation gate and the cost history in the plan prompt come from here."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from run_store import RunStore

AT_LEAST_SAMPLE = int(os.environ.get('CADRE_ESTIMATE_AT_LEAST', 3))
DEVIATION_THRESHOLD = float(os.environ.get('CADRE_ESTIMATE_DEVIATION', 0.50))


def lane_count(home: Path, run_id: str) -> int:
    try:
        import contract
        s = contract.read(home, run_id) or {}
        n = len(s.get('lanes') or [])
        return n if n > 0 else 1
    except Exception:
        return 1


def completed_runs(home: Path, limit: int = 20) -> list[dict[str, Any]]:
    """Completed runs that DID work (have spending), newest first."""
    store = RunStore(home)
    root = home / 'runs'
    if not root.is_dir():
        return []
    output = []
    for d in sorted(root.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        try:
            k = store.read(d.name)
        except Exception:
            continue
        if k.get('state') != 'completed':
            continue
        spend = float(k.get('budget_spent_usd') or 0)
        duration = float(k.get('updated') or 0) - float(k.get('created') or 0)
        if duration <= 0:
            continue
        n = lane_count(home, d.name)
        output.append({'run_id': d.name, 'spend': spend, 'duration_sec': duration, 'lane': n,
                      'lane_usd': spend / n, 'lane_min': duration / 60 / n})
        if len(output) >= limit:
            break
    return output


def estimate(home: Path, lane: int, excluded: str | None = None) -> dict[str, Any] | None:
    """Estimate from historically measured PER-LANE averages. None without enough samples (nothing invented)."""
    history = [k for k in completed_runs(home) if k['run_id'] != excluded]
    if len(history) < AT_LEAST_SAMPLE:
        return None
    usd = sum(k['lane_usd'] for k in history) / len(history)
    min = sum(k['lane_min'] for k in history) / len(history)
    return {'sample': len(history), 'lane': lane, 'usd': round(usd * lane, 4), 'min': round(min * lane, 1)}


def compare(home: Path, run_id: str) -> dict[str, Any]:
    """Estimate ↔ actual for one run. No estimate means no deviation."""
    store = RunStore(home)
    k = store.read(run_id)
    lane = lane_count(home, run_id)
    real_usd = float(k.get('budget_spent_usd') or 0)
    real_min = (float(k.get('updated') or 0) - float(k.get('created') or 0)) / 60
    t = estimate(home, lane, excluded=run_id)
    line = {'run_id': run_id, 'lane': lane, 'real_usd': round(real_usd, 4),
             'real_min': round(real_min, 1), 'estimate': t}
    if t:
        line['deviation_usd'] = (abs(real_usd - t['usd']) / t['usd']) if t['usd'] > 0 else None
        line['deviation_min'] = (abs(real_min - t['min']) / t['min']) if t['min'] > 0 else None
        overrun = [name for name, v in (('expense', line['deviation_usd']), ('duration', line['deviation_min']))
                if v is not None and v > DEVIATION_THRESHOLD]
        line['gate'] = ('DEVIATION: ' + ', '.join(overrun)) if overrun else 'complete'
    else:
        line['gate'] = f"no estimate (at least {AT_LEAST_SAMPLE} completed runs needed)"
    return line


def summary(home: Path, n: int = 3) -> str:
    runs = completed_runs(home, limit=n)
    if not runs:
        return ''
    lines = []
    for k in runs:
        s = compare(home, k['run_id'])
        t = s.get('estimate')
        if t:
            lines.append(f"`{s['run_id'][:19]}` {s['lane']} lanes · actual ${s['real_usd']:.3f}/{s['real_min']:.0f}min"
                            f" · estimate ${t['usd']:.3f}/{t['min']:.0f}min ({t['sample']} samples) · {s['gate']}")
        else:
            lines.append(f"`{s['run_id'][:19]}` {s['lane']} lanes · actual ${s['real_usd']:.3f}/{s['real_min']:.0f}min · {s['gate']}")
    return '\n'.join('- ' + x for x in lines)


def history_expense(home: Path, limit: int = 20) -> str:
    """D12: the real average cost PER TASK of past runs, for the planner (ledger: budget_spent_usd / lane).
    Empty string without a measurement (nothing invented)."""
    runs = completed_runs(home, limit=limit)
    if not runs:
        return ''
    usd = sum(k['lane_usd'] for k in runs) / len(runs)
    min = sum(k['lane_min'] for k in runs) / len(runs)
    return f"{len(runs)} completed runs · average per task ~${usd:.3f} · ~{min:.0f} min"


if __name__ == '__main__':
    home_dir = Path(os.environ.get('CADRE_HOME', Path.home() / '.local/share/cadre-engineering'))
    if sys.argv[1:2] == ['expense']:
        print(history_expense(home_dir) or 'no measurement yet')
    else:
        print(summary(home_dir) or 'no completed runs')
