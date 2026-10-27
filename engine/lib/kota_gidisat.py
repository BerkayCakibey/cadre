#!/usr/bin/env python3
"""Hourly Go usage line, same measure as butce.py (4c-13: Σ formula, points; rolling percentage of the last 5 hours).
Read-only; no provider call."""
from __future__ import annotations

from pathlib import Path
import time

from butce_kapisi import spending
import go_pencere


def satir(db: Path, now: float | None = None, pencere_sn: int = 5 * 3600) -> str:
    now = time.time() if now is None else now
    stamp = time.strftime('%F %H:%M', time.localtime(now))
    measured = spending(db, 'opencode-go', now - pencere_sn)
    puan = go_pencere.sigma(db, now - pencere_sn, now)
    if measured is None or puan is None:
        return f"{stamp} 5h: Go fisleri okunamadi (DB yok/ulasilamaz) — harcama BILINMIYOR"
    _usd, steps = measured
    return (f"{stamp} 5h: {steps} adim · {puan:.2f} aylik puan = rolling %{puan / go_pencere.PENCERE_PAYI['rolling']:.1f}"
            f" (Σ formulu; butce.py ile ayni olcu)")


def main() -> int:
    import butce
    print(satir(butce.DB))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
