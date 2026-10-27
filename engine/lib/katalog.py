#!/usr/bin/env python3
"""Model catalog: context, price and free/paid classification per model from one place (opencode's copy of models.dev).
Context is a MODEL property; if the catalogue is missing/broken the caller falls back to the old table. cost.input == cost.output == 0 means the free bucket."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from kosu import atomic_json

# Our provider id -> the id in the catalogue.
KIMLIK = {'opencode': 'opencode', 'opencode-go': 'opencode-go', 'zen': 'opencode', 'groq': 'groq'}

_onbellek: dict[str, Any] = {}


# 4c-7/17: some routes offer the same model under a versioned name (OR `qwen/qwen3.8-max-0902` = Go `qwen3.8-max`).
TAKMA_AD = {'qwen3.8-max-0902': 'qwen3.8-max'}


def kanonik_model(ad: str) -> str:
    """B344: transport/vendor prefixes do not give the same model an independent judge identity."""
    model = ad.rsplit('/', 1)[-1].split('#', 1)[0].removesuffix(':free')
    return re.sub(r'[^a-z0-9]', '', TAKMA_AD.get(model.lower(), model).lower())


def yol() -> Path:
    return Path(os.environ.get('CADRE_MODEL_KATALOG') or (Path.home() / '.cache/opencode/models.json'))


def yukle(taze: bool = False) -> dict[str, Any]:
    """Reads the catalogue and does not read it again until the file changes. If unreadable, an empty dict (the caller falls back to the old table)."""
    p = yol()
    try:
        damga = p.stat().st_mtime_ns
    except OSError:
        _onbellek.clear()
        return {}
    if taze or _onbellek.get('damga') != damga or _onbellek.get('yol') != str(p):
        try:
            veri = json.loads(p.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            veri = {}
        _onbellek.update({'damga': damga, 'yol': str(p), 'veri': veri if isinstance(veri, dict) else {}})
    veri = dict(_onbellek.get('veri', {}))
    try:
        or_canli = json.loads(canli_yol('openrouter').read_text())
        if isinstance(or_canli.get('kayitlar'), dict):
            onceki = veri.get('openrouter') or {}
            modeller = dict(onceki.get('models') or {})
            for ad, kayit in or_canli['kayitlar'].items():
                eski = modeller.get(ad) if isinstance(modeller.get(ad), dict) else {}
                if isinstance(kayit, dict) and isinstance(eski.get('limit'), dict) and isinstance(kayit.get('limit'), dict):
                    kayit = {**kayit, 'limit': {**eski['limit'], **kayit['limit']}}
                modeller[ad] = kayit
            veri['openrouter'] = {**onceki, 'models': modeller}
    except (OSError, ValueError, AttributeError):
        pass
    return veri


def model(saglayici: str, ad: str) -> dict[str, Any] | None:
    """Catalogue record. `ad` may come with a provider prefix like 'groq/openai/gpt-oss-120b'; the first part is dropped."""
    veri = yukle()
    kimlik = KIMLIK.get(saglayici, saglayici)
    modeller = (veri.get(kimlik) or {}).get('models') or {}
    if ad in modeller:
        return modeller[ad]
    # 'provider/model' or 'provider/sub/model' — catalogues keep the sub-path (example: openai/gpt-oss-120b)
    if '/' in ad:
        kalan = ad.split('/', 1)[1]
        if kalan in modeller:
            return modeller[kalan]
        if ad.split('/', 1)[0] in modeller:
            return modeller[ad.split('/', 1)[0]]
    return None


def baglam(saglayici: str, ad: str) -> int | None:
    m = model(saglayici, ad) or {}
    deger = (m.get('limit') or {}).get('context')
    return int(deger) if isinstance(deger, (int, float)) and deger > 0 else None


def fiyat(saglayici: str, ad: str) -> dict[str, float] | None:
    """USD / 1M tokens. None if not in the catalogue (unknown) — must not be confused with 'free'."""
    m = model(saglayici, ad)
    if m is None:
        return None
    c = m.get('cost') or {}
    return {k: float(v) for k, v in c.items() if isinstance(v, (int, float))}


def bedava_mi(saglayici: str, ad: str) -> bool | None:
    """True/False, None if not in the catalogue. UNKNOWN does not mean free; the caller must assume paid."""
    f = fiyat(saglayici, ad)
    if f is None or 'input' not in f or 'output' not in f:   # B297: a missing price is unknown (same as the plugin's zeroCost)
        return None
    return f['input'] == 0 and f['output'] == 0


# --- Models the account can REALLY use ------------------------------------------------------
# The catalogue is a SUPERSET (zen: 103 listed, the account can use 71). If a live list exists the router intersects
# with it; otherwise no filtering is done.

def _qe() -> Path:
    return Path(os.environ.get('CADRE_HOME') or (Path.home() / '.local/share/cadre-engineering'))


def canli_yol(saglayici: str) -> Path:
    return _qe() / 'zincir' / f'{saglayici}-canli.json'


def canli_liste(saglayici: str) -> set[str] | None:
    """Model ids the account can use; None if the file is missing/broken (= no filtering)."""
    try:
        veri = json.loads(canli_yol(saglayici).read_text(encoding='utf-8'))
        modeller = veri.get('modeller')
        return set(modeller) if isinstance(modeller, list) and modeller else None
    except (OSError, ValueError, AttributeError):
        return None


def kullanilabilir(saglayici: str, ad: str) -> bool:
    """False if the live list is known and the model is not in it. True if unknown (we do not block)."""
    liste = canli_liste(saglayici)
    if liste is None:
        return True
    return ad in liste or (ad.split('/', 1)[1] in liste if '/' in ad else False)


def zen_yenile(timeout: float = 30.0) -> int:
    """Refreshes the list the account can use from zen's /models endpoint. Returns: model count.
    Note: the endpoint is behind Cloudflare; without a User-Agent it returns 403 (code 1010)."""
    import urllib.request
    istek = urllib.request.Request('https://opencode.ai/zen/v1/models', headers={'User-Agent': 'opencode/1.0'})
    with urllib.request.urlopen(istek, timeout=timeout) as r:
        veri = json.load(r)
    modeller = sorted(m['id'] for m in (veri.get('data') or veri))
    yol = canli_yol('opencode')
    yol.parent.mkdir(parents=True, exist_ok=True)
    gecici = yol.with_suffix('.tmp')
    gecici.write_text(json.dumps({'ts': __import__('time').time(), 'modeller': modeller}, ensure_ascii=False), encoding='utf-8')
    gecici.replace(yol)
    return len(modeller)


def or_yenile(timeout: float = 30.0) -> int:
    """D15: convert OR list prices to the local catalogue format; do not count unknown as free."""
    import urllib.request
    import math
    import time
    with urllib.request.urlopen('https://openrouter.ai/api/v1/models', timeout=timeout) as response:
        data = json.load(response)['data']
    records = {}
    for row in data:
        try:
            price = row['pricing']
            rates = {k: float(price[v]) * 1e6 for k, v in (('input', 'prompt'), ('output', 'completion'))}
            if not all(math.isfinite(n) and n >= 0 for n in rates.values()):
                continue
            records[row['id']] = {'cost': rates, 'limit': {'context': int(row['context_length'])},
                                  'tool_call': 'tools' in row.get('supported_parameters', [])}
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    atomic_json(canli_yol('openrouter'), {'ts': time.time(), 'modeller': sorted(records), 'kayitlar': records})
    return len(records)


FIYAT_NOBET = 'zincir/model-fiyat-nobeti.json'


def izlenen_modeller() -> list[tuple[str, str]]:
    """Models that appear in chains: the price watch follows only the models WE USE."""
    try:
        import yonlendirici
        adaylar = {m for liste in yonlendirici.ROLLER.values() for m in liste}
    except Exception:
        return []
    cikti = []
    for aday in adaylar:
        aday = aday.split('|')[0].split('#')[0]
        if '/' not in aday:
            continue
        sag, ad = aday.split('/', 1)
        cikti.append((sag, ad))
    return sorted(set(cikti))


def fiyat_degisimi(home: Path | None = None, yaz: bool = True) -> list[dict[str, Any]]:
    """B88: the catalogue does not say 'will become paid'; a free -> paid switch (or the reverse) is seen and reported immediately (morning summary).
    The bucket decision is already read from the catalogue at call time; this function keeps the change from going unnoticed."""
    ev = Path(home) if home is not None else _qe()
    nobet = ev / FIYAT_NOBET
    try:
        onceki = json.loads(nobet.read_text(encoding='utf-8'))
        onceki = onceki if isinstance(onceki, dict) else {}
    except (OSError, ValueError):
        onceki = {}
    simdiki: dict[str, Any] = {}
    degisim: list[dict[str, Any]] = []
    for sag, ad in izlenen_modeller():
        f = fiyat(sag, ad)
        if f is None:
            continue
        anahtar = f'{sag}/{ad}'
        bedava = (f.get('input') == 0 and f.get('output') == 0)
        simdiki[anahtar] = {'input': f.get('input'), 'output': f.get('output'), 'bedava': bedava}
        eski = onceki.get(anahtar)
        if isinstance(eski, dict) and eski.get('bedava') is not None and eski['bedava'] != bedava:
            degisim.append({'model': anahtar, 'eski_bedava': bool(eski['bedava']), 'yeni_bedava': bedava,
                            'eski': {'input': eski.get('input'), 'output': eski.get('output')},
                            'yeni': {'input': f.get('input'), 'output': f.get('output')}})
    if yaz and simdiki:
        try:
            nobet.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(nobet, simdiki)
        except OSError:
            pass
    return degisim


def ozet() -> dict[str, int]:
    veri = yukle()
    return {k: len((v or {}).get('models') or {}) for k, v in veri.items() if isinstance(v, dict)}


if __name__ == '__main__':
    import sys
    if len(sys.argv) == 2 and sys.argv[1] == '--zen-yenile':
        print(f'zen canli model listesi tazelendi: {zen_yenile()} model -> {canli_yol("opencode")}')
    elif len(sys.argv) >= 3:
        s, a = sys.argv[1], sys.argv[2]
        print(json.dumps({'saglayici': s, 'model': a, 'baglam': baglam(s, a),
                          'fiyat': fiyat(s, a), 'bedava': bedava_mi(s, a)}, ensure_ascii=False))
    else:
        o = ozet()
        print(f'katalog: {yol()} ({sum(o.values())} model, {len(o)} saglayici)')
        for k in ('opencode', 'opencode-go', 'openrouter'):
            if k in o:
                print(f'  {k:<12} {o[k]}')
