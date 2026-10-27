#!/usr/bin/env python3
"""Scope score and level, single source (G-E9): 0-100 points bound to human-time bands.
The rule floor is the lower bound; the planner's `kapsam_puani` may raise it, never lower it. The level gives the cap, the correction round and the plan ceiling."""

from __future__ import annotations

import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

SEVIYELER: tuple[dict[str, Any], ...] = (
    {'ad': 'basit', 'ust': 15, 'sure': '<15 dk', 'tavan_usd': 1.0, 'duzeltme_tur': 1, 'plan_en_cok': 1},
    {'ad': 'orta', 'ust': 40, 'sure': '15-60 dk', 'tavan_usd': 2.0, 'duzeltme_tur': 2, 'plan_en_cok': 3},
    {'ad': 'zor', 'ust': 65, 'sure': '1-4 sa', 'tavan_usd': 5.0, 'duzeltme_tur': 2, 'plan_en_cok': 5},
    {'ad': 'cok-zor', 'ust': 85, 'sure': '>4 sa', 'tavan_usd': 10.0, 'duzeltme_tur': 2, 'plan_en_cok': 8},
    {'ad': 'ultra', 'ust': 100, 'sure': 'gunler', 'tavan_usd': 10.0, 'duzeltme_tur': 2, 'plan_en_cok': 8},   # project mode stage 2
)
SIFIRDAN = re.compile(r'sifirdan|sıfırdan|from scratch|greenfield|yeni (bir )?proje|bastan sona proje|baştan sona proje'
                      r'|projeyi (bastan|baştan)', re.I)
# B215: 'sifirdan farkli/buyuk/az...' is a numeric comparison, not a project marker; 'sifirdan kucuk BIR arac' is a size adjective.
KOD_KIMLIGI = re.compile(r'`[^`\n]*`|\b\w*_\w*\b')   # B216: code names like `normalize_timestamp` do not count as keywords
SIFIR_KARSILASTIRMA = re.compile(r's[iı]f[iı]rdan\s+(?:farkl[iı]|fazla|az|e[sş]it|ba[sş]ka|(?:b[uü]y[uü]k|k[uü][cç][uü]k)(?!\s+bir\b))', re.I)


BAGLAM_ISARETI = 'PROJENIN OZGUN ISTEGI (baglam)'   # G-P1: in a project child prompt, what comes after this line is not scored


def seviye(puan: Any) -> dict[str, Any]:
    try:
        p = max(0, min(100, int(puan)))
    except (TypeError, ValueError):
        p = 0
    return {**next(s for s in SEVIYELER if p <= s['ust']), 'puan': p}


def kural_tabani(istek: str) -> int:
    """A cheap and fallible lower bound: pre-filter (ATLA 10 · standard 25 · PREMIUM 50) + a project from scratch 90."""
    istek = (istek or '').split(BAGLAM_ISARETI, 1)[0]
    duz = KOD_KIMLIGI.sub(' ', istek)
    if SIFIRDAN.search(SIFIR_KARSILASTIRMA.sub(' ', duz)):
        return 90
    try:
        from onelek import karar
        gec, prem = karar(istek)[0], karar(duz)[1]   # the size heuristic counts file names; hard words only in plain text
    except Exception:
        gec, prem = 'GECER', '-'
    return 50 if prem == 'PREMIUM' else (25 if gec == 'GECER' else 10)


def birlestir(kural: int, planlayici: Any) -> int:
    try:
        p = int(planlayici)
    except (TypeError, ValueError):
        return max(0, min(100, int(kural)))
    return max(0, min(100, max(int(kural), p)))


def plan_siniri(plan: dict[str, Any], kural: int) -> str | None:
    """Plan ceiling: the error text returned to the planner if the task count exceeds what the final level allows."""
    s = seviye(birlestir(kural, plan.get('kapsam_puani')))
    n = len(plan.get('gorevler') or [])
    if n <= s['plan_en_cok']:
        return None
    return (f"plan {n} gorev iceriyor; kapsam puani {s['puan']} (seviye {s['ad']}) en cok {s['plan_en_cok']} gorev izin verir: "
            f"gorevleri birlestir ya da kapsam_puani'ni isin gercek kapsamina gore gerekcesiyle yukselt")


AUTO_TAHMIN_USD = 0.25   # QA: `auto` resolves to the paid chain; an auto task without an estimate must not count as a zero cap


def plan_tavani(plan: dict[str, Any], taban: float) -> float:
    """G-E9/P2: 1.5 times the task estimates, at least the level floor; an `auto` task without an estimate counts as AUTO_TAHMIN_USD."""
    tahminler = [g.get('tahmin_usd', 0) or (AUTO_TAHMIN_USD if g.get('model') == 'auto' else 0) for g in plan.get('gorevler', [])]
    if any(type(t) not in (int, float) or not math.isfinite(t) or t < 0 for t in tahminler):
        raise ValueError('invalid task estimate')
    tavan = max(taban, sum(tahminler) * 1.5)
    if not math.isfinite(tavan) or tavan * 1e6 >= 2**53:
        raise ValueError('invalid plan budget')
    return math.ceil(tavan * 100) / 100


def uygula(run_id: str, proje: str) -> dict[str, Any]:
    """After the plan: final score → level → record (`run.json.kapsam`) and run cap (not touched if the operator set CADRE_KOSU_TAVAN_USD;
    never below what was already spent)."""
    from kosu import RunStore
    store = RunStore()
    kayit = store.read(run_id)
    istek = Path(kayit['prompt']).read_text() if kayit.get('prompt') and Path(kayit['prompt']).exists() else ''
    plan_yolu = store.path(run_id) / 'plan.json'
    plan = json.loads(plan_yolu.read_text()) if plan_yolu.exists() else {}
    kural = kural_tabani(istek)
    puan = birlestir(kural, plan.get('kapsam_puani'))
    if kayit.get('proje_ust'):   # G-P1: a project child does not enter project mode again
        puan = min(puan, 85)
    s = seviye(puan)
    bilgi = {'puan': s['puan'], 'seviye': s['ad'], 'kural': kural, 'planlayici': plan.get('kapsam_puani'),
             'gerekce': str(plan.get('kapsam_gerekce') or '')[:300]}
    estimate = plan_tavani(plan, s['tavan_usd'])
    tasks = plan.get('gorevler') or []
    known = bool(tasks) and all(type(g.get('tahmin_usd')) in (int, float) and g['tahmin_usd'] > 0 for g in tasks)
    needs = {**(kayit.get('budget_need_micro') or {}),
             'seritler': math.ceil(sum(g['tahmin_usd'] for g in tasks) * 1e6) if known else None}
    guncel = {'kapsam': bilgi, 'budget_plan_cap_usd': estimate, 'budget_need_micro': needs}
    if os.environ.get('CADRE_KOSU_TAVAN_USD', '').strip():
        guncel['budget_manual'] = True
    if not kayit.get('budget_manual') and not os.environ.get('CADRE_KOSU_TAVAN_USD', '').strip():
        from butce_kapisi import operator_tavani
        bound = operator_tavani(kayit)
        guncel['budget_cap_usd'] = min(float(kayit['budget_cap_usd']),
                                      bound / 1e6 if bound is not None else float(kayit['budget_cap_usd']))
    store.update(run_id, **guncel)
    if kayit.get('proje_ust') and kayit.get('proje_ozellik'):
        from proje import ozellik_tavani_guncelle
        ozellik_tavani_guncelle(store, kayit['proje_ust'], kayit['proje_ozellik'],
                               guncel.get('budget_plan_cap_usd', kayit['budget_cap_usd']))
    return {**s, **bilgi, 'tavan_usd': guncel.get('budget_cap_usd', kayit.get('budget_cap_usd'))}


if __name__ == '__main__':
    if sys.argv[1:2] == ['uygula'] and len(sys.argv) >= 4:
        r = uygula(sys.argv[2], sys.argv[3])
        print(f"{r['ad']} {r['puan']} {r['tavan_usd']} {r['duzeltme_tur']} {r['plan_en_cok']}")
        raise SystemExit(0)
    if sys.argv[1:2] == ['kural'] and len(sys.argv) >= 3:
        print(kural_tabani(Path(sys.argv[2]).read_text(errors='replace')))
        raise SystemExit(0)
    print(__doc__)
    raise SystemExit(2)
