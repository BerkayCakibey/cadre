#!/usr/bin/env python3
"""Task contract: written at plan time, immutable during the run; splits the budget into lane, judge and helper pockets
and carries the judge's round/revision allowance. It is the single answer to the question who may spend how much."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Any

from run_store import FileLock, RunStore, atomic_json, request_shares

# Shares are DEFINED here and APPLIED in `budget_gate.role_cap`. The two files can drift (B110:
# the judge share was read from a fixed JUDGE_PAY_RATIO=0.15). When changing this, look there too.
FALLBACK_SHARES = {'lanes': 0.75, 'judge': 0.15, 'helper': 0.10}
ROLE_SLICE = {'quality': 'judge', 'review': 'helper', 'cadre-review': 'helper'}   # same as model_router.ROLE_BUCKET
# 4c-9/10: no exploration agent; compaction is the lane's job and spends from the calling role's pocket


def path(home: Path, run_id: str) -> Path:
    return RunStore(home).path(run_id) / 'contract.json'


def slice(role: str | None) -> str:
    """Which budget slice the role belongs to. An unknown role is a LANE (not the narrowest slice, but not the widest either)."""
    return ROLE_SLICE.get((role or '').strip(), 'lanes')


CORRECTION_PREFIX = 'correction-'
LANE_AFTER = ('merge', 'qa', 'quality')


def task_slice(role: str | None, task_id: str | None = None) -> str:
    """The pocket the spending is written to. The G-C16 correction round runs AFTER the lanes are DONE and the lane pocket is frozen (B82);
    round 2's cloud and a clean restart can only spend from the handed-off money, the acceptance (judge) pocket."""
    name = slice(role)
    task = task_id if task_id is not None else os.environ.get('CADRE_TASK_ID', '')
    # (B332/B335): work AFTER the lanes (merge, QA repair, acceptance) ran out of money when it spent from the frozen lane pocket
    return 'judge' if name == 'lanes' and (str(task).startswith(CORRECTION_PREFIX) or str(task) in LANE_AFTER or role in LANE_AFTER) else name


def shares_valid(shares: Any) -> bool:
    if not isinstance(shares, dict) or set(shares) != set(FALLBACK_SHARES):
        return False
    try:
        values = [float(shares[k]) for k in FALLBACK_SHARES]
    except (TypeError, ValueError):
        return False
    return all(0 < d < 1 for d in values) and abs(sum(values) - 1.0) < 1e-6


def create(home: Path, run_id: str, plan: Any = None, shares: Any = None) -> dict[str, Any]:
    """Writes the contract once and returns it. If it already exists it is NOT TOUCHED, the existing one is returned."""
    store = RunStore(home)
    target = path(home, run_id)
    with FileLock(target.with_suffix('.lock')):
        existing = read(home, run_id)
        if existing:
            return existing
        record = store.read(run_id)
        if not shares_valid(shares):
            shares = request_shares(record.get('request'))
        lanes = []
        for task in ((plan or {}).get('tasks') or []):
            if isinstance(task, dict) and task.get('id'):
                lanes.append({'id': str(task['id']), 'title': str(task.get('title') or '')[:120],
                                 'model': str(task.get('model') or ''), 'difficulty': task.get('difficulty')})
        contract = {
            'version': 1,
            'formation': time.time(),
            'run_id': run_id,
            'run_cap_usd': record.get('budget_cap_usd'),
            'shares': dict(shares) if shares_valid(shares) else dict(FALLBACK_SHARES),
            'lanes': lanes,
            'judge': {'round': 1, 'revision': 1},
            'source': 'plan' if lanes else 'fallback',
        }
        atomic_json(target, contract)
        return contract


def read(home: Path, run_id: str | None) -> dict[str, Any] | None:
    if not run_id:
        return None
    try:
        data = json.loads(path(home, run_id).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get('version') != 1 or not shares_valid(data.get('shares')):
        return None
    return data


def review_pockets(home: Path, run_id: str | None) -> dict[str, float] | None:
    """G-B5: the pockets of a review job come from the review policy copied at acceptance; if the old copy has no pockets the default stays."""
    if not run_id:
        return None
    try:
        record = RunStore(home).read(run_id)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    pockets = (record.get('review_limits') or {}).get('pockets') if record.get('job_type') == 'review' else None
    return {k: float(pockets[k]) for k in FALLBACK_SHARES} if shares_valid(pockets) else None


def request_pockets(home: Path, run_id: str | None) -> dict[str, float] | None:
    """CR-08: the pocket shares of a typed request bound by the approval; until the contract is written the proposal gate sees the same shares."""
    if not run_id:
        return None
    try:
        record = RunStore(home).read(run_id)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return request_shares(record.get('request'))


def bases(home: Path, run_id: str | None) -> dict[str, float]:
    """The run's base shares. Without a contract the request's shares, without those the review job's recorded pockets, without those the default."""
    contract = read(home, run_id)
    if contract:
        return dict(contract['shares'])
    return request_pockets(home, run_id) or review_pockets(home, run_id) or dict(FALLBACK_SHARES)


def pay_ratio(home: Path, run_id: str | None, role: str | None) -> float:
    """The ratio of the run cap the role gets; from the same source as the bases."""
    return float(bases(home, run_id)[slice(role)])


def summary(home: Path, run_id: str | None) -> str:
    s = read(home, run_id)
    if not s:
        return ''
    p = s['shares']
    cap = RunStore(home).read(run_id).get('budget_cap_usd', s.get('run_cap_usd'))
    return (f"contract: cap {'$%.2f' % cap if isinstance(cap, (int, float)) else 'absent'}"
            f" · lanes %{p['lanes'] * 100:.0f} · judge %{p['judge'] * 100:.0f} · helper %{p['helper'] * 100:.0f}"
            f" · {len(s.get('lanes') or [])} lanes · judge {s['judge']['round']} round + {s['judge']['revision']} revision")


def main() -> int:
    import sys
    home = Path(os.environ['CADRE_HOME'])
    command = sys.argv[1] if len(sys.argv) > 1 else 'summary'
    run_id = sys.argv[2] if len(sys.argv) > 2 else os.environ.get('CADRE_RUN_ID', '')
    if command == 'write':
        plan = None
        raw = sys.stdin.read().strip() if not sys.stdin.isatty() else ''
        if raw:
            try:
                plan = json.loads(raw)
            except ValueError:
                plan = None
        print(json.dumps(create(home, run_id, plan), ensure_ascii=False))
        return 0
    if command == 'summary':
        line = summary(home, run_id)
        if line:
            print(line)
        return 0
    print(f'unknown command: {command}', file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
