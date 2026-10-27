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

from kosu import RunStore

EN_AZ_ORNEK = int(os.environ.get('CADRE_TAHMIN_EN_AZ', 3))
SAPMA_ESIGI = float(os.environ.get('CADRE_TAHMIN_SAPMA', 0.50))


def serit_sayisi(home: Path, run_id: str) -> int:
    try:
        import sozlesme
        s = sozlesme.oku(home, run_id) or {}
        n = len(s.get('seritler') or [])
        return n if n > 0 else 1
    except Exception:
        return 1


def tamamlanan_kosular(home: Path, limit: int = 20) -> list[dict[str, Any]]:
    """Completed runs that DID work (have spending), newest first."""
    store = RunStore(home)
    kok = home / 'runs'
    if not kok.is_dir():
        return []
    cikti = []
    for d in sorted(kok.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        try:
            k = store.read(d.name)
        except Exception:
            continue
        if k.get('state') != 'completed':
            continue
        harcama = float(k.get('budget_spent_usd') or 0)
        sure = float(k.get('updated') or 0) - float(k.get('created') or 0)
        if sure <= 0:
            continue
        n = serit_sayisi(home, d.name)
        cikti.append({'run_id': d.name, 'harcama': harcama, 'sure_sn': sure, 'serit': n,
                      'serit_usd': harcama / n, 'serit_dk': sure / 60 / n})
        if len(cikti) >= limit:
            break
    return cikti


def tahmin(home: Path, serit: int, haric: str | None = None) -> dict[str, Any] | None:
    """Estimate from historically measured PER-LANE averages. None without enough samples (nothing invented)."""
    gecmis = [k for k in tamamlanan_kosular(home) if k['run_id'] != haric]
    if len(gecmis) < EN_AZ_ORNEK:
        return None
    usd = sum(k['serit_usd'] for k in gecmis) / len(gecmis)
    dk = sum(k['serit_dk'] for k in gecmis) / len(gecmis)
    return {'ornek': len(gecmis), 'serit': serit, 'usd': round(usd * serit, 4), 'dk': round(dk * serit, 1)}


def karsilastir(home: Path, run_id: str) -> dict[str, Any]:
    """Estimate ↔ actual for one run. No estimate means no deviation."""
    store = RunStore(home)
    k = store.read(run_id)
    serit = serit_sayisi(home, run_id)
    gercek_usd = float(k.get('budget_spent_usd') or 0)
    gercek_dk = (float(k.get('updated') or 0) - float(k.get('created') or 0)) / 60
    t = tahmin(home, serit, haric=run_id)
    satir = {'run_id': run_id, 'serit': serit, 'gercek_usd': round(gercek_usd, 4),
             'gercek_dk': round(gercek_dk, 1), 'tahmin': t}
    if t:
        satir['sapma_usd'] = (abs(gercek_usd - t['usd']) / t['usd']) if t['usd'] > 0 else None
        satir['sapma_dk'] = (abs(gercek_dk - t['dk']) / t['dk']) if t['dk'] > 0 else None
        asim = [ad for ad, v in (('maliyet', satir['sapma_usd']), ('süre', satir['sapma_dk']))
                if v is not None and v > SAPMA_ESIGI]
        satir['kapi'] = ('SAPMA: ' + ', '.join(asim)) if asim else 'tamam'
    else:
        satir['kapi'] = f"tahmin yok (en az {EN_AZ_ORNEK} tamamlanmış koşu gerekiyor)"
    return satir


def ozet(home: Path, n: int = 3) -> str:
    kosular = tamamlanan_kosular(home, limit=n)
    if not kosular:
        return ''
    satirlar = []
    for k in kosular:
        s = karsilastir(home, k['run_id'])
        t = s.get('tahmin')
        if t:
            satirlar.append(f"`{s['run_id'][:19]}` {s['serit']} şerit · gerçek ${s['gercek_usd']:.3f}/{s['gercek_dk']:.0f}dk"
                            f" · tahmin ${t['usd']:.3f}/{t['dk']:.0f}dk ({t['ornek']} örnek) · {s['kapi']}")
        else:
            satirlar.append(f"`{s['run_id'][:19]}` {s['serit']} şerit · gerçek ${s['gercek_usd']:.3f}/{s['gercek_dk']:.0f}dk · {s['kapi']}")
    return '\n'.join('- ' + x for x in satirlar)


def gecmis_maliyet(home: Path, limit: int = 20) -> str:
    """D12: the real average cost PER TASK of past runs, for the planner (ledger: budget_spent_usd / lane).
    Empty string without a measurement (nothing invented)."""
    kosular = tamamlanan_kosular(home, limit=limit)
    if not kosular:
        return ''
    usd = sum(k['serit_usd'] for k in kosular) / len(kosular)
    dk = sum(k['serit_dk'] for k in kosular) / len(kosular)
    return f"{len(kosular)} tamamlanmış koşu · görev başı ortalama ~${usd:.3f} · ~{dk:.0f} dk"


if __name__ == '__main__':
    ev = Path(os.environ.get('CADRE_HOME', Path.home() / '.local/share/cadre-engineering'))
    if sys.argv[1:2] == ['maliyet']:
        print(gecmis_maliyet(ev) or 'henüz ölçüm yok')
    else:
        print(ozet(ev) or 'tamamlanmış koşu yok')
