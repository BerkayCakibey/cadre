#!/usr/bin/env python3
"""OpenCode Go usage windows (rolling 5 h, weekly, monthly; `GET /zen/go/v1/usage`, percentage) — visible before hitting the limit.
The percentage is the gate and is not converted to dollars: the wall margin is five points (at 95% no new work starts on Go, GO1); running work continues via another route of the same model.
Without network the last cache, without that the Σ formula (opencode DB); without both UNKNOWN (the caller falls back to the 429 stamp)."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import time
from typing import Any
import urllib.error
import urllib.request

PENCERELER = ('rolling', 'weekly', 'monthly')
TTL = float(os.environ.get('CADRE_GO_PENCERE_TTL', 120))
TABAN_PAY = 5.0  # GO1: new work closes at 95%; the total wall margin is at most five points.
GECMIS_N = 20
API_YAS_SN = 2 * 3600
UC = 'https://opencode.ai/zen/go/v1/usage'

# One pool (4c-13): window percentage = Σ spending ÷ (share × the model's monthly limit). Source opencode.ai/docs/go (07.10):
# 5 h = 20% of the monthly limit, a week 50%; the limit is the model's weight in the pool. 54/18/2 reproduced exactly from the DB.
PENCERE_PAYI = {'rolling': 0.2, 'weekly': 0.5, 'monthly': 1.0}
MODEL_LIMIT_USD = {
    **dict.fromkeys(('deepseek-v4-pro', 'deepseek-v4-flash-vision-exp', 'glm-5.3', 'kimi-k3', 'qwen3.8-max', 'mimo-v2.6-pro',
                     'mimo-v2.5-pro', 'grok-4.7', 'grok-4.6', 'gpt-6-luna', 'gpt-5.6-luna'), 15.0),
    **dict.fromkeys(('deepseek-v4-flash', 'qwen3.8-flash', 'hy4-preview', 'space-bunny'), 30.0),
    **dict.fromkeys(('deepseek-v4.1-flash', 'glm-5.3-flash', 'glm-5.2', 'kimi-k2.7-code', 'kimi-k2.6', 'longcat-2.0',
                     'mimo-v2.6-flash', 'mimo-v2.5', 'minimax-m3', 'minimax-m2.7', 'muse-spark-1.3-contributor',
                     'muse-spark-1.2-contributor', 'qwen3.7-plus', 'hy3'), 60.0),
}
YOGUN_SAATLER = ((1, 4), (6, 10))   # DeepSeek peak hours (UTC, Mon–Fri): the Go pool charges 2× points during these hours


def model_limiti(model: str) -> float:
    """An unknown model counts as the heaviest (lowest limit): it over-charges points and never exceeds the pool."""
    return MODEL_LIMIT_USD.get(str(model or '').split('/')[-1], min(MODEL_LIMIT_USD.values()))


def yogun_saat(ts: float) -> bool:
    t = time.gmtime(ts)
    return t.tm_wday < 5 and any(bas <= t.tm_hour < son for bas, son in YOGUN_SAATLER)


def zaman_carpani(model: str, ts: float) -> int:
    """The catalogue price for DeepSeek is off-peak; at peak hours Go charges 2× points for the same call."""
    return 2 if str(model or '').split('/')[-1].startswith('deepseek') and yogun_saat(ts) else 1


def puan(model: str, usd: float) -> float:
    """Monthly points from list dollars (including the peak multiplier): 1 point = 1% of the pool."""
    return float(usd) * 100.0 / model_limiti(model)


def usd(model: str, puan_: float) -> float:
    return float(puan_) * model_limiti(model) / 100.0


def sigma(db: Path, since: float, until: float | None = None) -> float | None:
    """Monthly points in [since, until) from Go step receipts in the opencode DB; None if the DB cannot be read."""
    import sqlite3
    from contextlib import closing
    try:
        with closing(sqlite3.connect(Path(db).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as con:
            rows = con.execute("""select json_extract(p.data,'$.cost'), json_extract(m.data,'$.modelID'), p.time_created
                from part p join message m on m.id=p.message_id where p.time_created >= ? and p.time_created < ?
                and json_extract(p.data,'$.type')='step-finish' and json_extract(m.data,'$.providerID')='opencode-go'""",
                               (since * 1000, (until if until is not None else 2**40) * 1000)).fetchall()
    except (OSError, sqlite3.Error):
        return None
    toplam = 0.0
    for maliyet, model, an in rows:
        try:
            deger = float(maliyet or 0)
        except (TypeError, ValueError):
            return None
        toplam += puan(model, deger * zaman_carpani(model, an / 1000))
    return toplam


def _iso(deger: Any) -> float | None:
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(deger).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        return None


def kendi_yuzdeler(db: Path, now: float, resets: dict[str, Any] | None, ay_bas: float) -> dict[str, float] | None:
    """Window percentages without the API (Σ formula). If the week start is unknown the last 7 days are counted (over-counts, cautious)."""
    resets = resets or {}
    # 4c-19 reality probe: the 5-hour window is not a fixed "last 5 hours"; it counts back from the API's reset time
    bes_son = _iso(resets.get('rolling'))
    if bes_son and bes_son > now:
        bes_bas = bes_son - 5 * 3600
    elif bes_son and now - bes_son < 5 * 3600:   # the cached reset has passed: the new window starts at that moment at the earliest
        bes_bas = bes_son
    else:
        bes_bas = now - 5 * 3600
    hafta_son = _iso(resets.get('weekly'))
    hafta_bas = hafta_son - 7 * 86400 if hafta_son and hafta_son > now else now - 7 * 86400
    ay_son = _iso(resets.get('monthly'))
    if ay_son and ay_son > now:
        from datetime import datetime
        son = datetime.fromtimestamp(ay_son)
        yil, ay = (son.year, son.month - 1) if son.month > 1 else (son.year - 1, 12)
        import calendar
        ay_bas = son.replace(year=yil, month=ay, day=min(son.day, calendar.monthrange(yil, ay)[1])).timestamp()
    degerler = {'rolling': sigma(db, bes_bas, now), 'weekly': sigma(db, hafta_bas, now), 'monthly': sigma(db, ay_bas, now)}
    if any(v is None for v in degerler.values()):
        return None
    return {p: round(v / PENCERE_PAYI[p], 4) for p, v in degerler.items()}


def kalan_puan(veri: dict[str, Any] | None, sonra: float = 0.0) -> float | None:
    """Monthly points left before the wall: in each window (100 − wall − percentage) × share, the narrowest; spending after the reading is subtracted."""
    if not veri:
        return None
    try:
        kalan = min((100.0 - TABAN_PAY - float(veri.get(p) or 0)) * PENCERE_PAYI[p] for p in PENCERELER)
    except (TypeError, ValueError):
        return None
    return max(0.0, kalan - max(0.0, float(sonra or 0)))


def yol(home: Path) -> Path:
    return home / 'zincir/go-pencere.json'


def _yuzde(raw: Any) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw) or raw < 0:
        raise ValueError('go-window-shape-invalid')
    return float(raw)


def _onbellek(home: Path) -> dict[str, Any]:
    try:
        veri = json.loads(yol(home).read_text(encoding='utf-8'))
        if not isinstance(veri, dict):
            return {}
        stamp = _yuzde(veri['ts'])
        if not 0 < stamp <= time.time() + 60:
            return {}
        for p in PENCERELER:
            _yuzde(veri[p])
        return veri
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _yaz(home: Path, veri: dict[str, Any]) -> None:
    p = yol(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    gecici = p.with_suffix('.tmp')
    gecici.write_text(json.dumps(veri, ensure_ascii=False), encoding='utf-8')
    gecici.replace(p)


def _anahtar() -> str:
    return json.loads((Path.home() / '.local/share/opencode/auth.json').read_text())['opencode-go']['key']


def _cek(timeout: float) -> dict[str, Any]:
    if os.environ.get('CADRE_AG_YOK') == '1':   # an isolated test home does not go to the network
        raise OSError('CADRE_AG_YOK')
    # behind Cloudflare: without a User-Agent it returns 403 (code 1010) — not an identity problem.
    istek = urllib.request.Request(UC, headers={'Authorization': 'Bearer ' + _anahtar(), 'User-Agent': 'opencode/1.0'})
    with urllib.request.urlopen(istek, timeout=timeout) as r:
        return json.load(r)


def oku(home: Path, taze: bool = False, timeout: float = 10.0) -> dict[str, Any] | None:
    """{'rolling': %, 'weekly': %, 'monthly': %, 'resets': {...}, 'ts': …, 'gecmis': [...]}; None if unknown."""
    onb = _onbellek(home)
    if not taze and onb.get('ts') and time.time() - float(onb['ts']) < TTL:
        return onb or None
    try:
        ham = _cek(timeout)
        kullanim = ham['usage']
        yuzde = {}
        for p in PENCERELER:
            yuzde[p] = _yuzde(kullanim[p]['percent'])
        resets = {p: kullanim[p].get('resetsAt') for p in PENCERELER}
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
        if onb.get('ts') and time.time() - float(onb['ts']) >= API_YAS_SN:
            return None
        return onb or None                      # no network: use the last known value, invent nothing
    gecmis = list(onb.get('gecmis') or [])
    gecmis.append({'ts': time.time(), **yuzde})
    veri = {'ts': time.time(), **yuzde, 'resets': resets, 'gecmis': gecmis[-GECMIS_N:]}
    try:
        _yaz(home, veri)
    except OSError:
        pass
    return veri


def duvar_payi(veri: dict[str, Any]) -> float:
    """GO1: a provider route hand-off keeps the remaining work; five points are reserved for new work."""
    return TABAN_PAY


AYRISMA_ESIGI = float(os.environ.get('CADRE_PENCERE_AYRISMA', 20))


def capraz_kontrol(veri: dict[str, Any] | None, kendi_yuzde: float | None) -> tuple[dict[str, Any] | None, str]:
    """S11: if the provider's percentage and our own measurement diverge, the larger one (the one saying less can be spent) wins;
    if the difference exceeds the threshold a warning text is returned (shown in the morning summary)."""
    if not veri:
        return veri, ''
    try:
        uc_yuzde = float(veri.get('monthly') or 0)
    except (TypeError, ValueError):
        return veri, ''
    if kendi_yuzde is None:
        return veri, ''
    try:
        kendi = float(kendi_yuzde)
    except (TypeError, ValueError):
        return veri, ''
    if not 0 <= kendi <= 1000:
        return veri, ''
    fark = abs(kendi - uc_yuzde)
    if fark < AYRISMA_ESIGI:
        return veri, ''
    kullanilacak = max(uc_yuzde, kendi)
    yeni = {**veri, 'monthly': kullanilacak, 'monthly_uc': uc_yuzde, 'monthly_kendi': kendi}
    return yeni, (f'monthly ayrismasi {fark:.0f} puan: saglayici %{uc_yuzde:.0f}, kendi olcumum %{kendi:.0f}'
                  f' -> muhafazakar olan (%{kullanilacak:.0f}) kullanildi')


def durum(veri: dict[str, Any] | None) -> tuple[bool, str]:
    """(open?, reason). Without data (True, 'bilinmiyor') — unknown alone does not close; the 429 stamp works separately."""
    if not veri:
        return True, 'bilinmiyor'
    pay = duvar_payi(veri)
    yuzdeler = {}
    for p in PENCERELER:
        try:
            yuzdeler[p] = float(veri.get(p, 0) or 0)
        except (TypeError, ValueError):
            continue
    # The "full" state first: it is a stronger statement and must not get lost under the wall warning.
    for p, yuzde in yuzdeler.items():
        if yuzde >= 100:
            return False, f'{p} %100 — havuz kapali'
    for p, yuzde in yuzdeler.items():
        if yuzde + pay >= 100:
            return False, f'{p} %{yuzde:.0f} + duvar payi {pay:.0f} >= 100'
    return True, 'acik'


def ozet(home: Path, veri: dict[str, Any] | None = None) -> str:
    """One line for the status screen. Does NOT go to the network: only reads the cache (the status screen must stay fast)."""
    veri = veri if veri is not None else (_onbellek(home) or None)
    if not veri:
        return ''
    acik, neden = durum(veri)
    return (f"rolling %{float(veri.get('rolling') or 0):.0f} · weekly %{float(veri.get('weekly') or 0):.0f}"
            f" · monthly %{float(veri.get('monthly') or 0):.0f} · duvar payi {duvar_payi(veri):.0f}"
            f" · {'acik' if acik else 'KAPALI: ' + neden}")


if __name__ == '__main__':
    import sys
    ev = Path(os.environ.get('CADRE_HOME', Path.home() / '.local/share/cadre-engineering'))
    if '--ozet' in sys.argv:
        satir = ozet(ev)
        if satir:
            print(satir)
        raise SystemExit(0)
    veri = oku(ev, taze='--taze' in sys.argv)
    satir = ozet(ev, veri)
    print(satir or 'pencere bilinmiyor (ag yok ve onbellek bos)')
