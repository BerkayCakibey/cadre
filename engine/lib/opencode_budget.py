#!/usr/bin/env python3
"""Shows the two pools of an opencode account on one screen: the Go subscription (three windows; unused allowance expires)
and Zen credit (prepaid; never expires, spent by `opencode/...` models). The two must not be mixed up."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from screen_render import money

ROOT = Path(os.environ.get('CADRE_ENGINE_ROOT', str(Path(__file__).resolve().parents[1])))


def _budget() -> dict:
    raw = subprocess.run([sys.executable, str(ROOT / 'lib/provider_budget.py')],
                         capture_output=True, text=True, timeout=60).stdout
    return json.loads(raw) if raw.strip() else {}


def _bar(percent: float, span_width: int = 18) -> str:
    full = max(0, min(span_width, round(span_width * percent / 100)))
    return '█' * full + '·' * (span_width - full)


def summary(b: dict | None = None) -> str:
    b = b if b is not None else _budget()
    go, zen = b.get('go') or {}, b.get('zen') or {}
    p = go.get('windows') or {}
    line = []
    line.append('OPENCODE — single account, two separate pools of money')
    line.append('')
    line.append(f"  GO (subscription · unused allowance BURNS · {go.get('remaining_day', '?')} days left)")
    for name, label in (('rolling', 'rolling'), ('weekly', 'weekly '), ('monthly', 'monthly')):
        value = p.get(name)
        if value is None:
            continue
        line.append(f"    {label} %{value:>5.1f}  {_bar(float(value))}")
    line.append(f"    this month %{float(go.get('month_points') or 0):.1f} · {float(go.get('available') or 0):.1f} points left to the wall")
    line.append(f"    window: {go.get('window_reason') or '?'}")
    line.append('')
    line.append('  CREDIT / ZEN (prepaid · NEVER EXPIRES · `opencode/...` models spend this)')
    line.append(f"    balance {money(zen.get('remaining'))} · spent {money(zen.get('spent'))}")
    line.append(f"    available {money(zen.get('available'))}")
    line.append('')
    line.append('  NOTE: if "One-time credits" is OFF in the panel, credit does NOT kick in when the Go limit is reached.')
    line.append('       "Go exhausted" ≠ "money exhausted" — the two are separate pools.')
    return '\n'.join(line)


if __name__ == '__main__':
    print(summary())
