#!/usr/bin/env python3
"""Reality checks: the system's assumptions about the outside world, measured by a weekly canary (4c-19). A deviation becomes a PROBLEM line and
`zincir/gerceklik.json`. Go only (cash $0); OR only when there is a balance and only with free listing data."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any

import go_pencere
from kosu import atomic_json
from sinirlar import CIKTI

QE = Path(os.environ.get('CADRE_HOME') or (Path.home() / '.local/share/cadre-engineering'))
KESME = 1.0   # the Go API truncates the percentage down (real >= API); tolerance ±1 point + truncation margin


def go_pencereleri(home: Path, db: Path, ay_bas: float, tolerans: float = 1.0) -> list[str]:
    """Go percentage ↔ Σ formula (with window coefficients), separately in three windows."""
    api = go_pencere._onbellek(home)
    if not api.get('ts'):
        return ['go-pencere: API gozlemi yok']
    kendi = go_pencere.kendi_yuzdeler(db, float(api['ts']), api.get('resets'), ay_bas)
    if kendi is None:
        return ['go-pencere: opencode DB okunamadi']
    return [f"go-pencere {w}: Σ %{kendi[w]:.1f} ≠ API %{float(api.get(w) or 0):.0f}" for w in go_pencere.PENCERELER
            if not -tolerans <= kendi[w] - float(api.get(w) or 0) <= tolerans + KESME]


def uc_sinirlari(fiyat: dict[str, Any], katalog_veri: dict[str, Any], zincirler: dict[str, list[str]]) -> list[str]:
    """Every model in the chain has an output limit that fits its role's cap; on OR at least one allowed endpoint carries the cap and the token parameter."""
    sorun = []
    for rol, zincir in zincirler.items():
        tavan = CIKTI.get(rol, CIKTI['serit'])
        for rota in zincir:
            for aday in rota.split('|'):
                sag, _, mid = aday.partition('/')
                mid = mid.split('#')[0]
                if sag == 'claude':
                    continue
                if sag == 'openrouter':
                    m = (fiyat.get('modeller') or {}).get(mid)
                    uclar = (m or {}).get('kademeler') or {}
                    if not m:
                        sorun.append(f'uc {rol}: {aday} fiyat dosyasinda yok')
                    elif not any((u.get('max_cikti') or tavan) >= tavan and u.get('params') != [] for u in uclar.values()):   # None = unknown
                        sorun.append(f'uc {rol}: {aday} hicbir izinli uc {tavan} tokeni tasimiyor')
                    continue
                kayit = ((katalog_veri.get(sag) or {}).get('models') or {}).get(mid)
                if kayit is None:
                    sorun.append(f'uc {rol}: {aday} katalogda yok')
                elif int((kayit.get('limit') or {}).get('output') or 0) < tavan:
                    sorun.append(f"uc {rol}: {aday} cikti {kayit['limit'].get('output')} < {tavan}")
    return sorted(set(sorun))


def sol_kademeleri(fiyat: dict[str, Any], politika: dict[str, Any]) -> list[str]:
    """Sol tiers come with the tags in the policy, are below the maximum price, and every tier's token parameter is known."""
    sorun = []
    for ad, kural in politika.items():
        if not isinstance(kural, dict) or 'kademeler' not in kural or 'sol' not in ad:
            continue
        m = (fiyat.get('modeller') or {}).get(ad)
        if not m:
            sorun.append(f'sol: {ad} fiyat dosyasinda yok')
            continue
        eksik = [k for k in kural['kademeler'] if k not in (m.get('kademeler') or {})]
        if eksik:
            sorun.append(f"sol: {ad} kademe yok: {', '.join(eksik)}")
        tavan = kural.get('max_price') or {}
        for etiket, uc in (m.get('kademeler') or {}).items():
            if not uc.get('params'):
                sorun.append(f'sol: {ad} {etiket} token parametresi bilinmiyor')
            if tavan and (uc['input'] > float(tavan['prompt']) * 1.000001 or uc['output'] > float(tavan['completion']) * 1.000001):
                sorun.append(f'sol: {ad} {etiket} azami fiyatin ustunde')
    return sorun


def fiyatlar(home: Path, katalog_veri: dict[str, Any], zincirler: dict[str, list[str]]) -> tuple[list[str], dict[str, Any]]:
    """Writes it if the catalogue price of models in the chain changed since the previous canary (a change affects the bucket/reservation decision)."""
    simdi, sorun = {}, []
    for zincir in zincirler.values():
        for rota in zincir:
            for aday in rota.split('|'):
                sag, _, mid = aday.partition('/')
                cost = (((katalog_veri.get(sag) or {}).get('models') or {}).get(mid.split('#')[0]) or {}).get('cost') or {}
                if sag != 'claude' and {'input', 'output'} <= set(cost):
                    simdi[aday] = {'input': cost['input'], 'output': cost['output']}
    try:
        onceki = json.loads((home / 'zincir/gerceklik.json').read_text()).get('fiyatlar') or {}
    except (OSError, ValueError, AttributeError):
        onceki = {}
    for aday, f in sorted(simdi.items()):
        if aday in onceki and onceki[aday] != f:
            sorun.append(f"fiyat: {aday} {onceki[aday]['input']}/{onceki[aday]['output']} -> {f['input']}/{f['output']}")
    return sorun, simdi


def dusunme_metni(model: str, opencode: str | None = None, db: Path | None = None, timeout: float = 180) -> list[str]:
    """A small request to a thinking Go model; thinking TEXT must appear in the opencode DB (usage alone is not proof).
    On a very easy question the model may skip thinking (07.10 canary); the question needs a little thought, and if there is no text it is tried once more."""
    import shutil
    import sqlite3
    from contextlib import closing
    opencode = opencode or os.environ.get('CADRE_REAL_OPENCODE') or shutil.which('opencode') or '/opt/homebrew/bin/opencode'
    db = db or Path(os.path.expanduser(os.environ.get('OPENCODE_DB', '~/.local/share/opencode/opencode.db')))
    # in a cron environment the plugin's gate cannot be installed (no CADRE_PYTHON -> denial); the gate is installed, observation mode and the run id are not carried.
    # Without a role, whether to think is left to the model: plan role = high.
    ortam = {k: v for k, v in os.environ.items() if k not in ('CADRE_RUN_ID', 'CADRE_GOREV_ID', 'CADRE_BUTCE_GOZLEM')}
    ortam.update(CADRE_HOME=str(QE), CADRE_ENGINE_ROOT=str(Path(__file__).resolve().parents[1]), CADRE_PYTHON=sys.executable,
                 OPENCODE_EXPERIMENTAL_NATIVE_LLM='false', CADRE_ROL='plan')
    istem = 'Dizi: 3, 7, 15, 31, ... Kural nedir, siradaki sayi kac? Yalniz sayiyi yaz.'
    sorun = []
    for _deneme in range(2):
        with tempfile.TemporaryDirectory(prefix='cadre-gerceklik-') as dizin:
            try:
                r = subprocess.run([opencode, 'run', '--dir', dizin, '-m', model, '--format', 'json', istem],
                                   stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, cwd=dizin, env=ortam)
            except (OSError, subprocess.TimeoutExpired) as exc:
                return [f'dusunme: {model} cagrilamadi ({type(exc).__name__})']
        oturum = next((json.loads(s).get('sessionID') for s in r.stdout.splitlines()
                       if s.startswith('{') and json.loads(s).get('sessionID')), None)
        if r.returncode != 0 or not oturum:
            return [f'dusunme: {model} yanit vermedi (rc={r.returncode})']
        try:
            with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as con:
                uzunluk = con.execute("select coalesce(sum(length(json_extract(p.data,'$.text'))),0) from part p join message m "
                                      "on m.id=p.message_id where m.session_id=? and json_extract(p.data,'$.type')='reasoning'",
                                      (oturum,)).fetchone()[0]
        except sqlite3.Error:
            return [f'dusunme: {model} opencode DB okunamadi']
        if uzunluk:
            return []
        sorun = [f'dusunme: {model} dusunme metni uretmedi (2 deneme)']
    return sorun


def kos(home: Path = QE, canli: bool = True) -> dict[str, Any]:
    import butce
    import katalog
    import yonlendirici
    katalog_veri = katalog.yukle(taze=True)
    try:
        fiyat = json.loads((home / 'zincir/openrouter-fiyat.json').read_text())
    except (OSError, ValueError):
        fiyat = {}
    politika = json.loads((Path(__file__).with_name('openrouter-politika.json')).read_text())
    zincirler = {r: z for r, z in yonlendirici.ROLLER.items() if z}
    fiyat_sorunlari, fiyat_simdi = fiyatlar(home, katalog_veri, zincirler)
    sonuc = {'go-pencere': go_pencereleri(home, butce.DB, butce.go_donem()[0]),
             'uc-sinirlari': uc_sinirlari(fiyat, katalog_veri, zincirler),
             'sol-kademeleri': sol_kademeleri(fiyat, politika),
             'fiyatlar': fiyat_sorunlari}
    if canli and os.environ.get('CADRE_TEST_KIPI') != '1':
        sonuc['dusunme'] = dusunme_metni(os.environ.get('CADRE_GERCEKLIK_MODEL', 'opencode-go/deepseek-v4-flash'))
    atomic_json(home / 'zincir/gerceklik.json', {'ts': time.time(), 'sonuc': sonuc, 'fiyatlar': fiyat_simdi})
    return sonuc


if __name__ == '__main__':
    for ad, sorunlar in kos().items():
        print(f'gerceklik {ad}: ' + ('SAGLAM' if not sorunlar else 'SORUN -> ' + ' | '.join(sorunlar)[:300]))
