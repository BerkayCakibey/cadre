#!/usr/bin/env python3
"""Task contract: written at plan time, immutable during the run; splits the budget into lane, judge and helper pockets
and carries the judge's round/revision allowance. It is the single answer to the question who may spend how much."""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Any

from kosu import FileLock, RunStore, atomic_json, istek_paylari

# Shares are DEFINED here and APPLIED in `butce_kapisi.rol_tavani`. The two files can drift (B110:
# the judge share was read from a fixed HAKEM_PAY_ORAN=0.15). When changing this, look there too.
VARSAYILAN_PAYLAR = {'seritler': 0.75, 'hakem': 0.15, 'yardimci': 0.10}
ROL_DILIMI = {'kalite': 'hakem', 'review': 'yardimci', 'cadre-review': 'yardimci'}   # same as yonlendirici.ROL_KOVASI
# 4c-9/10: no exploration agent; compaction is the lane's job and spends from the calling role's pocket


def yol(home: Path, run_id: str) -> Path:
    return RunStore(home).path(run_id) / 'sozlesme.json'


def dilim(rol: str | None) -> str:
    """Which budget slice the role belongs to. An unknown role is a LANE (not the narrowest slice, but not the widest either)."""
    return ROL_DILIMI.get((rol or '').strip(), 'seritler')


DUZELTME_ONEKI = 'duzeltme-'
SERIT_SONRASI = ('birlestirme', 'qa', 'kalite')


def gorev_dilimi(rol: str | None, gorev_id: str | None = None) -> str:
    """The pocket the spending is written to. The G-C16 correction round runs AFTER the lanes are DONE and the lane pocket is frozen (B82);
    round 2's cloud and a clean restart can only spend from the handed-off money, the acceptance (judge) pocket."""
    ad = dilim(rol)
    gorev = gorev_id if gorev_id is not None else os.environ.get('CADRE_GOREV_ID', '')
    # (B332/B335): work AFTER the lanes (merge, QA repair, acceptance) ran out of money when it spent from the frozen lane pocket
    return 'hakem' if ad == 'seritler' and (str(gorev).startswith(DUZELTME_ONEKI) or str(gorev) in SERIT_SONRASI or rol in SERIT_SONRASI) else ad


def paylar_gecerli(paylar: Any) -> bool:
    if not isinstance(paylar, dict) or set(paylar) != set(VARSAYILAN_PAYLAR):
        return False
    try:
        degerler = [float(paylar[k]) for k in VARSAYILAN_PAYLAR]
    except (TypeError, ValueError):
        return False
    return all(0 < d < 1 for d in degerler) and abs(sum(degerler) - 1.0) < 1e-6


def olustur(home: Path, run_id: str, plan: Any = None, paylar: Any = None) -> dict[str, Any]:
    """Writes the contract once and returns it. If it already exists it is NOT TOUCHED, the existing one is returned."""
    store = RunStore(home)
    hedef = yol(home, run_id)
    with FileLock(hedef.with_suffix('.lock')):
        mevcut = oku(home, run_id)
        if mevcut:
            return mevcut
        kayit = store.read(run_id)
        if not paylar_gecerli(paylar):
            paylar = istek_paylari(kayit.get('request'))
        seritler = []
        for gorev in ((plan or {}).get('gorevler') or []):
            if isinstance(gorev, dict) and gorev.get('id'):
                seritler.append({'id': str(gorev['id']), 'baslik': str(gorev.get('baslik') or '')[:120],
                                 'model': str(gorev.get('model') or ''), 'zorluk': gorev.get('zorluk')})
        sozlesme = {
            'version': 1,
            'olusma': time.time(),
            'run_id': run_id,
            'kosu_tavani_usd': kayit.get('budget_cap_usd'),
            'paylar': dict(paylar) if paylar_gecerli(paylar) else dict(VARSAYILAN_PAYLAR),
            'seritler': seritler,
            'hakem': {'tur': 1, 'revizyon': 1},
            'kaynak': 'plan' if seritler else 'varsayilan',
        }
        atomic_json(hedef, sozlesme)
        return sozlesme


def oku(home: Path, run_id: str | None) -> dict[str, Any] | None:
    if not run_id:
        return None
    try:
        veri = json.loads(yol(home, run_id).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(veri, dict) or veri.get('version') != 1 or not paylar_gecerli(veri.get('paylar')):
        return None
    return veri


def inceleme_cepleri(home: Path, run_id: str | None) -> dict[str, float] | None:
    """G-B5: the pockets of a review job come from the review policy copied at acceptance; if the old copy has no pockets the default stays."""
    if not run_id:
        return None
    try:
        kayit = RunStore(home).read(run_id)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    cepler = (kayit.get('review_limits') or {}).get('cepler') if kayit.get('job_type') == 'review' else None
    return {k: float(cepler[k]) for k in VARSAYILAN_PAYLAR} if paylar_gecerli(cepler) else None


def istek_cepleri(home: Path, run_id: str | None) -> dict[str, float] | None:
    """CR-08: the pocket shares of a typed request bound by the approval; until the contract is written the proposal gate sees the same shares."""
    if not run_id:
        return None
    try:
        kayit = RunStore(home).read(run_id)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return istek_paylari(kayit.get('request'))


def tabanlar(home: Path, run_id: str | None) -> dict[str, float]:
    """The run's base shares. Without a contract the request's shares, without those the review job's recorded pockets, without those the default."""
    sozlesme = oku(home, run_id)
    if sozlesme:
        return dict(sozlesme['paylar'])
    return istek_cepleri(home, run_id) or inceleme_cepleri(home, run_id) or dict(VARSAYILAN_PAYLAR)


def pay_orani(home: Path, run_id: str | None, rol: str | None) -> float:
    """The ratio of the run cap the role gets; from the same source as the bases."""
    return float(tabanlar(home, run_id)[dilim(rol)])


def ozet(home: Path, run_id: str | None) -> str:
    s = oku(home, run_id)
    if not s:
        return ''
    p = s['paylar']
    tavan = RunStore(home).read(run_id).get('budget_cap_usd', s.get('kosu_tavani_usd'))
    return (f"sözleşme: tavan {'$%.2f' % tavan if isinstance(tavan, (int, float)) else 'yok'}"
            f" · şerit %{p['seritler'] * 100:.0f} · hakem %{p['hakem'] * 100:.0f} · yardımcı %{p['yardimci'] * 100:.0f}"
            f" · {len(s.get('seritler') or [])} şerit · hakem {s['hakem']['tur']} tur + {s['hakem']['revizyon']} revizyon")


def main() -> int:
    import sys
    home = Path(os.environ['CADRE_HOME'])
    komut = sys.argv[1] if len(sys.argv) > 1 else 'ozet'
    run_id = sys.argv[2] if len(sys.argv) > 2 else os.environ.get('CADRE_RUN_ID', '')
    if komut == 'yaz':
        plan = None
        ham = sys.stdin.read().strip() if not sys.stdin.isatty() else ''
        if ham:
            try:
                plan = json.loads(ham)
            except ValueError:
                plan = None
        print(json.dumps(olustur(home, run_id, plan), ensure_ascii=False))
        return 0
    if komut == 'ozet':
        satir = ozet(home, run_id)
        if satir:
            print(satir)
        return 0
    print(f'bilinmeyen komut: {komut}', file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
