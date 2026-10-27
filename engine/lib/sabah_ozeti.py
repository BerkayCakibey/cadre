#!/usr/bin/env python3
"""Morning summary: what happened overnight, what was spent, where it got stuck — one page (`reports/OZET-<date>.md`).
Produced only from recorded fields (G-H3); the state is read with `cadre`."""

from __future__ import annotations

import opencode_adaptor as OC

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any

from kosu import RunStore
from ekran import simge, kosu_ozeti, butce_ozeti, para, temiz


def ev() -> Path:
    return Path(os.environ.get('CADRE_HOME', Path.home() / '.local/share/cadre-engineering'))


def _gun_basi(simdi: float) -> float:
    t = time.localtime(simdi)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def kosular(home: Path, since: float) -> list[dict[str, Any]]:
    store = RunStore(home)
    cikti = []
    kok = home / 'runs'
    if not kok.is_dir():
        return cikti
    for d in sorted(kok.iterdir()):
        if not d.is_dir():
            continue
        try:
            kayit = store.read(d.name)
        except Exception:
            continue
        if float(kayit.get('updated') or kayit.get('created') or 0) < since:
            continue
        cikti.append(kayit)
    return cikti


def butce(home: Path) -> dict[str, Any]:
    try:
        saved = json.loads((home / 'zincir/request-budget.json').read_text())
        if 0 <= time.time() - float(saved['observed']) < 60 and isinstance(saved['budget'], dict):
            return saved['budget']
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    return {}


def bayat_rezervasyon(home: Path) -> tuple[int, float]:
    db = home / 'zincir/request-budget.db'
    try:
        with closing(OC.connect(db)) as con:   # B142b: the connection must not stay open
            n, tutar = con.execute('select count(*), coalesce(sum(reserved),0) from reservation '
                                   'where actual is null and created < ?', (time.time() - 6 * 3600,)).fetchone()
        return int(n), float(tutar) / 1e6
    except sqlite3.Error:
        return 0, 0.0


def uret(home: Path | None = None, simdi: float | None = None, ayrinti: bool = False) -> str:
    home = home or ev()
    simdi = simdi if simdi is not None else time.time()
    since = _gun_basi(simdi) - 12 * 3600          # night jobs spill over from the previous day
    b = butce(home)
    satirlar = [f"# Gece özeti — {time.strftime('%Y-%m-%d %H:%M', time.localtime(simdi))}", '']

    kayitlar = kosular(home, since)
    if not ayrinti:
        completed = sum(k.get('state') == 'completed' for k in kayitlar)
        active = sum(k.get('state') in ('running', 'queued', 'accepted') for k in kayitlar)
        stopped = sum(k.get('state') in ('failed', 'retryable', 'cancelled') for k in kayitlar)
        spent = sum(float(k.get('budget_spent_usd') or 0) for k in kayitlar)
        status = f'{stopped} iş durdu' if stopped else 'sorun yok' if kayitlar else 'henüz iş yok'
        satirlar.append(f'{completed} koşu bitti · {active} devam · {para(spent)} harcandı · {status}')
        satirlar += [f'{temiz(k.get("project", "?"))} · {kosu_ozeti(k)}' for k in kayitlar]
        satirlar += ['', f'{simge("bakiye")} Kullanılabilir: {butce_ozeti(b)}']
        n, amount = bayat_rezervasyon(home)
        if n:
            satirlar.append(f'{simge("uyari")} {n} askıda rezervasyon ({para(amount)}) · cadre kurtar')
        if (b.get('go') or {}).get('pencere_acik') is False:
            satirlar.append(f'{simge("uyari")} Go penceresi kapalı · ayrıntı: cadre butce')
        satirlar += ['', 'Ayrıntı: cadre ozet --ayrinti']
        return '\n'.join(satirlar) + '\n'
    bitmis = [k for k in kayitlar if k.get('state') == 'completed']
    kalan = [k for k in kayitlar if k.get('state') not in ('completed', 'cancelled')]
    satirlar += ['## Koşular', '']
    if not kayitlar:
        satirlar.append('Bu pencerede koşu yok.')
    # Noise control: runs that DID work or are still standing are written line by line; those cancelled
    # without spending anything are collected on one line.
    sessiz = 0
    for k in kayitlar:
        harcama = float(k.get('budget_spent_usd') or 0)
        askida = float(k.get('budget_held_usd') or 0)
        onemli = harcama > 0 or askida > 0 or k.get('state') in ('completed', 'failed') or k in kalan
        if not onemli:
            sessiz += 1
            continue
        satirlar.append(f"- `{k.get('run_id', '?')[:19]}` **{k.get('state', '?')}** · {k.get('project', '?')}"
                        f" · harcanan ${harcama:.3f}"
                        + (f" · askıda ${askida:.3f}" if askida else '')
                        + (f" · {k.get('phase')}" if k.get('phase') else ''))
        for red in (k.get('denials') or [])[-3:]:
            satirlar.append(f"    - red: {red.get('provider')} — {str(red.get('reason'))[:80]}")
    if sessiz:
        satirlar.append(f"- ({sessiz} koşu hiçbir şey harcamadan iptal edildi)")
    toplam = sum(float(k.get('budget_spent_usd') or 0) for k in kayitlar)
    satirlar += ['', f"Biten {len(bitmis)} · devam/bekleyen {len(kalan)} · toplam harcanan ${toplam:.3f}", '']

    satirlar += ['## Bütçe', '']
    go = b.get('go') or {}
    p = go.get('pencereler') or {}
    if p:
        satirlar.append(f"- Go pencereleri: rolling %{float(p.get('rolling') or 0):.0f} · "
                        f"weekly %{float(p.get('weekly') or 0):.0f} · monthly %{float(p.get('monthly') or 0):.0f}"
                        f" · {'açık' if go.get('pencere_acik') else 'KAPALI: ' + str(go.get('pencere_neden'))}")
        for ad, ne_zaman in (p.get('resets') or {}).items():
            if ne_zaman:
                satirlar.append(f"    - {ad} reset: {ne_zaman}")
    satirlar.append(f"- Go: duvara {float(go.get('kullanilabilir') or 0):.1f} puan · bu ay %{float(go.get('ay_puan') or 0):.1f}"
                    if go.get('db') == 'ok' else '- Go: havuz bilinmiyor (gozlem okunamadi)')
    for ad, anahtar in (('OpenRouter', 'or'), ('Zen', 'zen')):
        v = b.get(anahtar) or {}
        if v:
            satirlar.append(f"- {ad}: kullanılabilir {para(v.get('kullanilabilir'))}"
                            + (f" · bakiye ${float(v.get('kalan') or 0):.2f}" if anahtar == 'zen' else ''))
    cl = b.get('claude') or {}
    if cl:
        satirlar.append(f"- Claude: pay %{cl.get('pay')} (7 gün %{cl.get('used7')}) · tavan %{cl.get('tavan')}")
    satirlar.append('- Go aboneliği kendi penceresinde yenilenir; ön ödemeli bakiyeler günlere bölünmez.')

    # §40.3 real-world gate: estimate ↔ actual. The estimate is NOT INVENTED, it is measured from history; without enough samples
    # the line says so explicitly and measurement keeps accumulating.
    try:
        import tahmin_gercek
        tg = tahmin_gercek.ozet(home)
    except Exception:
        tg = ''
    if tg:
        satirlar += ['', '## Tahmin ↔ gerçek', '', tg]

    n, tutar = bayat_rezervasyon(home)
    satirlar += ['', '## Dikkat', '']
    if n:
        satirlar.append(f"- {n} askıda rezervasyon (${tutar:.2f}) 6 saatten eski → `cadre kurtar --bayat-listele`")
    # B88: if a free model starts charging, the bucket decision changes SILENTLY because it is read from the catalogue
    # at call time. The catalogue has no "when will it become paid" field (checked), so we cannot know
    # beforehand; the only thing we can do is write the change as soon as we see it.
    try:
        import katalog
        for d in katalog.fiyat_degisimi(home):
            yon = 'ÜCRETLİ OLDU' if d['eski_bedava'] else 'bedavaya döndü'
            satirlar.append(f"- Model fiyatı değişti: `{d['model']}` {yon} "
                            f"(girdi {d['yeni']['input']}, çıktı {d['yeni']['output']})")
    except Exception:
        pass
    if not go.get('pencere_acik', True):
        satirlar.append(f"- Go penceresi kapalı: {go.get('pencere_neden')}")
    if go.get('pencere_ayrisma'):
        satirlar.append(f"- {go['pencere_ayrisma']}")
    if len(satirlar) and satirlar[-1] == '':
        satirlar.append('Uyarı yok.')
    return '\n'.join(satirlar) + '\n'


def yaz(home: Path | None = None, simdi: float | None = None) -> Path:
    home = home or ev()
    simdi = simdi if simdi is not None else time.time()
    hedef = home / 'reports' / f"OZET-{time.strftime('%Y%m%d', time.localtime(simdi))}.md"
    hedef.parent.mkdir(parents=True, exist_ok=True)
    gecici = hedef.with_suffix('.tmp')
    gecici.write_text(uret(home, simdi, ayrinti=True), encoding='utf-8')
    gecici.replace(hedef)
    return hedef


if __name__ == '__main__':
    import sys
    if '--yaz' in sys.argv:
        print(yaz())
    else:
        print(uret(ayrinti='--ayrinti' in sys.argv), end='')
