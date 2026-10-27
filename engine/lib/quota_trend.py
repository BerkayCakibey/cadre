#!/usr/bin/env python3
"""Hourly Go usage line, same measure as provider_budget.py (4c-13: Σ formula, points; rolling percentage of the last 5 hours).
Read-only; no provider call."""
from __future__ import annotations

from pathlib import Path
import time

from budget_gate import spending
import go_window


def line(db: Path, now: float | None = None, window_sec: int = 5 * 3600) -> str:
    now = time.time() if now is None else now
    stamp = time.strftime('%F %H:%M', time.localtime(now))
    measured = spending(db, 'opencode-go', now - window_sec)
    points = go_window.sigma(db, now - window_sec, now)
    if measured is None or points is None:
        return f"{stamp} 5h: Go receipts unreadable (DB absent/unreachable) — spend UNKNOWN"
    _usd, steps = measured
    return (f"{stamp} 5h: {steps} step · {points:.2f} monthly points = rolling %{points / go_window.WINDOW_SHARE['rolling']:.1f}"
            f" (Σ formula; same measure as provider_budget.py)")


def main() -> int:
    import provider_budget
    print(line(provider_budget.DB))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
