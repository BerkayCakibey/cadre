#!/usr/bin/env python3
"""Scope score and level, single source (G-E9): 0-100 points bound to human-time bands.
The rule floor is the lower bound; the planner's `scope_points` may raise it, never lower it. The level gives the cap, the correction round and the plan ceiling."""

from __future__ import annotations

import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

LEVELS: tuple[dict[str, Any], ...] = (
    {'entry_name': 'simple', 'parent': 15, 'duration': '<15 min', 'cap_usd': 1.0, 'correction_round': 1, 'plan_most': 1},
    {'entry_name': 'middle', 'parent': 40, 'duration': '15-60 min', 'cap_usd': 2.0, 'correction_round': 2, 'plan_most': 3},
    {'entry_name': 'hard', 'parent': 65, 'duration': '1-4 h', 'cap_usd': 5.0, 'correction_round': 2, 'plan_most': 5},
    {'entry_name': 'very-hard', 'parent': 85, 'duration': '>4 h', 'cap_usd': 10.0, 'correction_round': 2, 'plan_most': 8},
    {'entry_name': 'ultra', 'parent': 100, 'duration': 'days', 'cap_usd': 10.0, 'correction_round': 2, 'plan_most': 8},   # project mode stage 2
)
SCRATCH = re.compile(r'from scratch|greenfield|\bnew project\b|end[- ]to[- ]end project|from the ground up|start(?:ing)? the project over', re.I | re.ASCII)
# B215: 'different from zero / greater than zero ...' is a numeric comparison, not a project marker; it is stripped before the scratch check.
CODE_ID = re.compile(r'`[^`\n]*`|\b\w*_\w*\b')   # B216: code names like `normalize_timestamp` do not count as keywords
ZERO_COMPARISON = re.compile(r'\b(?:different|other|apart|greater|bigger|larger|more|less|smaller|fewer)\s+(?:from|than)\s+(?:zero|0)\b|\bequal\s+to\s+(?:zero|0)\b|\bnon-zero\b', re.I | re.ASCII)


CONTEXT_MARK = 'ORIGINAL PROJECT REQUEST (context)'   # G-P1: in a project child prompt, what comes after this line is not scored


def level(points: Any) -> dict[str, Any]:
    try:
        p = max(0, min(100, int(points)))
    except (TypeError, ValueError):
        p = 0
    return {**next(s for s in LEVELS if p <= s['parent']), 'points': p}


def rule_base(request: str) -> int:
    """A cheap and fallible lower bound: pre-filter (SKIP 10 · standard 25 · PREMIUM 50) + a project from scratch 90."""
    request = (request or '').split(CONTEXT_MARK, 1)[0]
    plain = CODE_ID.sub(' ', request)
    if SCRATCH.search(ZERO_COMPARISON.sub(' ', plain)):
        return 90
    try:
        from prefilter import decision
        late, prem = decision(request)[0], decision(plain)[1]   # the size heuristic counts file names; hard words only in plain text
    except Exception:
        late, prem = 'PASS', '-'
    return 50 if prem == 'PREMIUM' else (25 if late == 'PASS' else 10)


def merge(rule: int, planner: Any) -> int:
    try:
        p = int(planner)
    except (TypeError, ValueError):
        return max(0, min(100, int(rule)))
    return max(0, min(100, max(int(rule), p)))


def plan_limit(plan: dict[str, Any], rule: int) -> str | None:
    """Plan ceiling: the error text returned to the planner if the task count exceeds what the final level allows."""
    s = level(merge(rule, plan.get('scope_points')))
    n = len(plan.get('tasks') or [])
    if n <= s['plan_most']:
        return None
    return (f"the plan contains {n} tasks; scope score {s['points']} (level {s['entry_name']}) allows at most {s['plan_most']} tasks: "
            f"merge tasks, or raise scope_points to the real scope of the job with a justification")


AUTO_ESTIMATE_USD = 0.25   # QA: `auto` resolves to the paid chain; an auto task without an estimate must not count as a zero cap


def plan_cap(plan: dict[str, Any], base: float) -> float:
    """G-E9/P2: 1.5 times the task estimates, at least the level floor; an `auto` task without an estimate counts as AUTO_ESTIMATE_USD."""
    estimates = [g.get('estimate_usd', 0) or (AUTO_ESTIMATE_USD if g.get('model') == 'auto' else 0) for g in plan.get('tasks', [])]
    if any(type(t) not in (int, float) or not math.isfinite(t) or t < 0 for t in estimates):
        raise ValueError('invalid task estimate')
    cap = max(base, sum(estimates) * 1.5)
    if not math.isfinite(cap) or cap * 1e6 >= 2**53:
        raise ValueError('invalid plan budget')
    return math.ceil(cap * 100) / 100


def apply(run_id: str, project_ref: str) -> dict[str, Any]:
    """After the plan: final score → level → record (`run.json.scope`) and run cap (not touched if the operator set CADRE_RUN_CAP_USD;
    never below what was already spent)."""
    from run_store import RunStore
    store = RunStore()
    record = store.read(run_id)
    request = Path(record['prompt']).read_text() if record.get('prompt') and Path(record['prompt']).exists() else ''
    plan_path = store.path(run_id) / 'plan.json'
    plan = json.loads(plan_path.read_text()) if plan_path.exists() else {}
    rule = rule_base(request)
    points = merge(rule, plan.get('scope_points'))
    if record.get('project_ref_parent'):   # G-P1: a project child does not enter project mode again
        points = min(points, 85)
    s = level(points)
    info = {'points': s['points'], 'level': s['entry_name'], 'rule': rule, 'planner': plan.get('scope_points'),
             'rationale': str(plan.get('scope_rationale') or '')[:300]}
    estimate = plan_cap(plan, s['cap_usd'])
    tasks = plan.get('tasks') or []
    known = bool(tasks) and all(type(g.get('estimate_usd')) in (int, float) and g['estimate_usd'] > 0 for g in tasks)
    needs = {**(record.get('budget_need_micro') or {}),
             'lanes': math.ceil(sum(g['estimate_usd'] for g in tasks) * 1e6) if known else None}
    current = {'scope': info, 'budget_plan_cap_usd': estimate, 'budget_need_micro': needs}
    if os.environ.get('CADRE_RUN_CAP_USD', '').strip():
        current['budget_manual'] = True
    if not record.get('budget_manual') and not os.environ.get('CADRE_RUN_CAP_USD', '').strip():
        from budget_gate import operator_cap
        bound = operator_cap(record)
        current['budget_cap_usd'] = min(float(record['budget_cap_usd']),
                                      bound / 1e6 if bound is not None else float(record['budget_cap_usd']))
    store.update(run_id, **current)
    if record.get('project_ref_parent') and record.get('project_ref_feature'):
        from project_mode import feature_cap_update
        feature_cap_update(store, record['project_ref_parent'], record['project_ref_feature'],
                               current.get('budget_plan_cap_usd', record['budget_cap_usd']))
    return {**s, **info, 'cap_usd': current.get('budget_cap_usd', record.get('budget_cap_usd'))}


if __name__ == '__main__':
    if sys.argv[1:2] == ['apply'] and len(sys.argv) >= 4:
        r = apply(sys.argv[2], sys.argv[3])
        print(f"{r['entry_name']} {r['points']} {r['cap_usd']} {r['correction_round']} {r['plan_most']}")
        raise SystemExit(0)
    if sys.argv[1:2] == ['rule'] and len(sys.argv) >= 3:
        print(rule_base(Path(sys.argv[2]).read_text(errors='replace')))
        raise SystemExit(0)
    print(__doc__)
    raise SystemExit(2)
