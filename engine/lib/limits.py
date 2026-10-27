"""Single source of output, deadline and lane-context limits; plugins read the same file through `limits.mjs`."""
from __future__ import annotations

import json
import os
from pathlib import Path

_DATA = json.loads(Path(__file__).with_name('limits.json').read_text(encoding='utf-8'))
OUTPUT: dict[str, int] = {k: int(v) for k, v in _DATA['output'].items()}
DEADLINE: dict[str, int] = {k: int(v) for k, v in _DATA['deadline_sec'].items()}
OR: dict[str, float] = {k: float(v) for k, v in _DATA['openrouter'].items()}   # 4c-7: price file freshness and endpoint order
EFFORT: dict[str, str | None] = dict(_DATA.get('effort') or {})


def review_policy(job_kind: str = 'review') -> dict:
    """Finite review limits are copied into the run at acceptance; the environment cannot widen them."""
    policy = json.loads(json.dumps(_DATA['review']))
    if job_kind == 'code':
        # G-B5: a code plan review uses the fixed helper pocket; it is not split again like the report phases.
        code = _DATA['review_code']
        policy['version'] = code['version']
        policy['plan-review']['pay_percent'] = code['plan_pay_percent']
    return policy


def output(role: str) -> int:
    """The role's output cap (tokens); every role other than plan/quality/qa gets the lane cap."""
    return OUTPUT.get(role, OUTPUT['lane'])


def deadline(role: str) -> int:
    return DEADLINE[role]


def runner_durations() -> tuple[int, int]:
    values = _DATA['runner_duration_sec']
    warn = int(os.environ.get('CADRE_WARN_SEC', str(values['warning'])))
    abort = int(os.environ.get('CADRE_ABORT_SEC', str(values['last'])))
    if warn < 0 or abort <= 0:
        raise ValueError('engine duration must be finite and positive; warning may be zero')
    return min(warn, abort), abort


def lane_context(model_context: int) -> int:
    """The lane's effective context: min(upper bound, model context − margin)."""
    b = _DATA['context']
    return max(0, min(int(b['lane_cap']), int(model_context) - int(b['lane_pay'])))
