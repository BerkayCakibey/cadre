#!/usr/bin/env python3
"""Persistent request reservations and allowances: the ledger holds the full share of every call whose result is unknown.
Amounts are micro-dollars (request counts for free providers); a price/quota estimate is not an invoice."""

from __future__ import annotations

from contextlib import closing, contextmanager, nullcontext, ExitStack
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_CEILING
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from typing import Any, Iterator
import uuid

import opencode_adaptor as OC
from opencode_adaptor import spending, spending_model, spending_tokens
from kosu import FileLock, RunStore, atomic_json, identifier

# This is the SINGLE SOURCE of the daily REQUEST cap; `butce.py` reads from here, consistency with the router's rpd
# is bound by a test (B110). 4c-11: of the free providers only groq remains, for the pre-filter.
GUNLUK_ISTEK = {'groq': 900}
FREE = {'groq': ('groq', GUNLUK_ISTEK['groq'])}
PAID = {'opencode-go': 'go', 'opencode': 'zen', 'openrouter': 'or'}   # Go is a subscription pool; Zen and OpenRouter are prepaid

# Item 0b — THE BUCKET UNIT IS THE MODEL: zero-priced models are written to the '<provider>:free' bucket,
# REQUESTS are counted instead of dollars and they do not enter the run's dollar cap; the provider window still binds.
BEDAVA_SONEK = ':free'


def bedava_gun_tavan() -> int:
    try:
        deger = int(os.environ.get('CADRE_BEDAVA_GUN_TAVAN', '2000'))
    except ValueError:
        return 2000
    return deger if deger > 0 else 2000


MODEL_SONEK = '#'
# B110: the daily REQUEST limit may be per model ('<provider>#<model>' bucket). No such provider today
# (Gemini was removed in 4c-11); the mechanism remains, the table is empty.
MODEL_GUNLUK_ISTEK: dict[str, tuple[tuple[str, int], ...]] = {}


def taban_saglayici(provider: str) -> str:
    """'opencode-go:free' -> 'opencode-go', 'provider#model' -> 'provider'.
    The circuit breaker, cooldown and observation always work with the base id."""
    return provider.split(MODEL_SONEK, 1)[0].split(BEDAVA_SONEK, 1)[0]


def model_kimligi(provider: str) -> str:
    """'provider#model' -> 'model'; empty string if the bucket carries no model."""
    return provider.split(MODEL_SONEK, 1)[1] if MODEL_SONEK in provider else ''


def model_kovasi(provider: str, model: str) -> str:
    """Router and executor ask for the model bucket the plugin reserved."""
    if provider in MODEL_GUNLUK_ISTEK:
        return f'{provider}#{model}'
    if provider not in FREE:
        from katalog import bedava_mi
        if bedava_mi(provider, model) is True:
            return provider + BEDAVA_SONEK
    return provider


def or_sapma_aktif(home: Path) -> bool:
    """4c-7: the drift file closes OR and shows it as failed only while active: a persistent alarm (receipt ↔ breakdown) or a closed last credit
    observation. An old file whose drift has ended (04.10) does not list the provider as failed."""
    zincir = Path(home) / 'zincir'
    try:
        sapma = json.loads((zincir / 'openrouter-sapma.json').read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(sapma, dict) or sapma.get('kalici') is True:
        return True
    try:
        return json.loads((zincir / 'openrouter-kredi.json').read_text()).get('kapali') is True
    except (OSError, ValueError, AttributeError):
        return True   # if the credit observation cannot be read, the drift counts as ongoing


def or_uc_izinli(kural: Any, m: dict, uc: str) -> bool:
    """4c-7: does the endpoint match the policy — an explicit list (tag or name) or a rule (closed weights / reported quantization)."""
    if isinstance(kural, list):
        return uc in kural
    if not isinstance(kural, dict):
        return False
    liste = kural.get('kademeler', kural.get('saglayicilar'))
    if isinstance(liste, list):
        return uc in liste
    return kural.get('kapali') is True or ((m.get('kademeler') or {}).get(uc) or {}).get('kuantizasyon') == (kural.get('kuantizasyon') or 'fp8')


def or_pin_fiyati(home: Path, model: str, cikti: int | None = None) -> bool:
    """OR-PIN: the plugin's `orEnPahali` rule (fresh v2 price file, age-limited last good price, endpoints matching the policy).
    If `cikti` is given at least one endpoint must carry that output (the plugin filters at request time; the router must not offer an empty link)."""
    from sinirlar import OR as OR_SINIR
    try:
        d = json.loads((Path(home) / 'zincir/openrouter-fiyat.json').read_text())
        simdi = time.time()
        m = d['modeller'][model]
        kural = json.loads(Path(__file__).with_name('openrouter-politika.json').read_text())[model]
        yollar = m['providers']
        return (d.get('version') == 2 and 0 <= simdi - float(d['ts']) < OR_SINIR['fiyat_taze_sn']
                and 0 <= simdi - float(m.get('fiyat_ts', d['ts'])) < OR_SINIR['fiyat_yas_sn']
                and m.get('fp8_cache') is True and isinstance(yollar, list) and bool(yollar)
                and all(or_uc_izinli(kural, m, y) for y in yollar) and float(m['input']) > 0 and float(m['output']) > 0
                and (cikti is None or any((((m.get('kademeler') or {}).get(y) or {}).get('max_cikti') or cikti) >= cikti
                                          for y in yollar)))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def model_gunluk_istek(provider: str, model: str) -> int:
    """Daily request cap of this model. An unknown model = the LOWEST published value of that provider
    (fail-closed: do not take a Flash quota of 20 for 500 and run into a string of 429s)."""
    kalıplar = MODEL_GUNLUK_ISTEK.get(taban_saglayici(provider))
    if not kalıplar:
        return GUNLUK_ISTEK.get(taban_saglayici(provider), 0)
    ad = (model or '').lower()
    for parca, deger in kalıplar:
        if parca in ad:
            return deger
    return min(deger for _, deger in kalıplar)


class BudgetDenied(RuntimeError):
    """No proven allowance; callers must use another provider or wait locally."""


class SaglayiciBekleme(BudgetDenied):
    pass


def note_denial(home: Path, provider: str | None = None, reason: str = '', dilim: str | None = None, deneme: bool = True) -> None:
    """A task-owned receipt, so the motor does not infer budget state from logs.
    P5: if `reason` is given, a structured line is also added to the run record (run.json `denials[]`) — silent closures such as a stale
    catalogue are read from the record in the morning, not from a log line."""
    if reason:
        try:
            run = os.environ['CADRE_RUN_ID']
            store = RunStore(home)
            with FileLock(store.path(run)/'.record.lock'):
                record = store.read(run)
                rows = list(record.get('denials') or [])
                # B169: the ledger denial + the plugin's catch denial wrote two lines in the same second (one without a provider);
                # a second record for the same provider + task within 2 s does not open a new line, it increments the `tekrar` counter (the first reason is kept).
                _simdi = time.time(); _gorev = os.environ.get('CADRE_GOREV_ID') or 'main'
                _ikiz = None
                for _r in rows[-5:]:
                    try:
                        _ts = time.mktime(time.strptime(str(_r.get('ts', ''))[:19], '%Y-%m-%dT%H:%M:%S'))
                    except (ValueError, TypeError):
                        continue
                    if provider and _r.get('provider') == provider and _r.get('task') == _gorev and abs(_simdi - _ts) <= 2:
                        _ikiz = _r
                row = {'ts':time.strftime('%Y-%m-%dT%H:%M:%S'), 'provider':provider, 'reason':reason,
                       'task':os.environ.get('CADRE_GOREV_ID') or 'main', 'attempt':os.environ.get('CADRE_BUDGET_ATTEMPT')}
                if rows and all(rows[-1].get(k) == row[k] for k in ('provider', 'reason', 'task', 'attempt')):
                    rows[-1]['tekrar'] = int(rows[-1].get('tekrar', 1)) + 1  # B49: the same denial line must not repeat for the same attempt
                    row = None
                elif _ikiz is not None:
                    _ikiz['tekrar'] = int(_ikiz.get('tekrar', 1)) + 1        # B169: twin denial in the same second
                    row = None
                if row:
                    rows.append(row)
                record.update(denials=rows[-50:], updated=time.time())
                atomic_json(store.path(run)/'run.json', record)
        except (OSError, ValueError, KeyError):
            pass
    if not deneme:
        return
    try:
        run = os.environ['CADRE_RUN_ID']
        task = os.environ.get('CADRE_GOREV_ID') or 'main'
        attempt = identifier(os.environ['CADRE_BUDGET_ATTEMPT'])
        store = RunStore(home)
        store.read(run)
        path = store.task_dir(run, task)/'attempts'/attempt
        if path.is_dir():
            target = path/'budget-denied.json'
            with FileLock(path/'budget-denied.lock'):
                saved = json.loads(target.read_text()) if target.exists() else {}
                providers = set(saved.get('providers', []))
                if provider and taban_saglayici(provider) in {*PAID, *FREE, 'claude', 'opencode'}:
                    providers.add(provider)   # 0b: bucket names like 'opencode-go:free' must also be written —
                    # otherwise qe_core's fallback 'if the list is empty, ban ALL cloud' kicks in.
                refusals = list(saved.get('refusals') or [])
                refusal = {'provider': provider, 'reason': reason, 'dilim': dilim,
                           'scope': 'role' if reason.startswith('run budget exhausted') else 'provider'}
                if refusal not in refusals:
                    refusals.append(refusal)
                atomic_json(target, {'attempt_id':attempt, 'time':time.time(), 'providers':sorted(providers),
                                     'refusals': refusals})
    except (OSError, ValueError, KeyError):
        pass  # Refusal still takes effect if its diagnostic cannot be written.


def amount(value: Any) -> float:
    if isinstance(value, bool):
        raise BudgetDenied('invalid budget number')
    try:
        number = float(value)
    except (ValueError, TypeError) as exc:
        raise BudgetDenied('unknown budget number') from exc
    if not math.isfinite(number) or number < 0:
        raise BudgetDenied('invalid budget number')
    return number


# Google's RPD resets at PACIFIC midnight (official docs); the count bucket follows the provider's calendar.
KOVA_SAATI = {'openrouter': 'UTC'}


def kova_gun_basi(provider: str, now: float | None = None) -> float:
    """Start epoch of this provider's daily bucket (local day by default)."""
    now = time.time() if now is None else now
    bolge = KOVA_SAATI.get(taban_saglayici(provider))
    if not bolge:
        return datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    from zoneinfo import ZoneInfo
    yerel = datetime.fromtimestamp(now, ZoneInfo(bolge))
    return yerel.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def kova_donemi(provider: str, now: float, yedek: str) -> str:
    bolge = KOVA_SAATI.get(taban_saglayici(provider))
    if not bolge:
        return yedek
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(now, ZoneInfo(bolge)).strftime('%Y-%m-%d')


def olculen_pay(snapshot: Any, provider: str) -> int | None:
    """B129: the MEASURED share of the dollar bucket (micro-USD; remaining balance / Go period share), margin subtracted; None for count buckets.
    `allowance()` gives the remainder; adding the ledger's `settled` to it would silently grow the cap when a stale reservation closes."""
    try:
        if taban_saglayici(provider) not in PAID or provider.endswith(BEDAVA_SONEK) or provider == 'opencode-go':
            return None   # The Go share is pool points, not dollars (4c-13); its capacity is in go_kapasite
        row = snapshot[PAID[taban_saglayici(provider)]]
        pay = Decimal(str(amount(row['pay'])))
        if taban_saglayici(provider) == 'opencode':
            pay = max(Decimal(0), pay - pay * Decimal('0.15'))   # the SAME margin as allowance
        return int((pay * 1_000_000).to_integral_value(rounding=ROUND_FLOOR))
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None


def or_makbuz_gozlemi(con: sqlite3.Connection | None, stamp: float) -> dict[str, Any]:
    """N-BU03: receipt ids before the credit request; estimated/later spending is not included."""
    rows = [] if con is None else con.execute(
        "select id,actual,receipt,coalesce(json_extract(metadata,'$.kesin_ts'),created) from reservation "
        "where provider='openrouter' and actual > 0 and receipt is not null and receipt != '' "
        "and receipt not like 'hayalet:%' and receipt not like 'kurtarma:%' "
        "and coalesce(json_extract(metadata,'$.kesin_ts'),created) <= ? order by id", (stamp,)).fetchall()
    if any(type(cost) is not int or not 0 < cost < 2**53
           or type(when) not in (int, float) or not math.isfinite(when) or when < 0
           for _, cost, _, when in rows):
        raise ValueError('invalid receipt observation')
    encoded = json.dumps(rows, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    return {'ts': stamp, 'makbuz_sha256': hashlib.sha256(encoded).hexdigest(),
            'makbuz_micro': sum(row[1] for row in rows)}


def or_dahil_makbuz(con: sqlite3.Connection, snapshot: Any) -> int:
    """N-BU01/03: only history bound to the same credit/ledger observation; it is not subtracted twice from the net balance."""
    try:
        proof = snapshot['or']['kredi_gozlemi']
        stamp = amount(proof['ts'])
        if not 0 <= time.time() - stamp < 300:
            return 0
        seen = or_makbuz_gozlemi(con, stamp)
        if any(proof[key] != seen[key] for key in ('makbuz_sha256', 'makbuz_micro')):
            return 0
        usage = proof['harcanan_micro']
        if type(usage) is not int or not 0 <= seen['makbuz_micro'] <= usage < 2**53:
            return 0
        return seen['makbuz_micro']
    except (BudgetDenied, KeyError, TypeError, ValueError, sqlite3.Error):
        return 0


def allowance(snapshot: Any, provider: str, now: float | None = None, model: str | None = None) -> tuple[str, int]:
    """Reject missing, stale, future, or partially unreadable observations. On Go the remaining points are converted to this model's
    list dollars; without a model the limit of the heaviest model (the smallest amount) is used."""
    try:
        now = time.time() if now is None else now
        stamp = datetime.fromisoformat(snapshot['ts']).timestamp()
        period = datetime.fromtimestamp(now).strftime('%Y-%m-%d')
        if not 0 <= now - stamp <= 90 or snapshot['bugun'] != period:
            raise BudgetDenied('stale budget')
        period = kova_donemi(provider, now, period)   # the count bucket rolls over on the provider's calendar
        if provider == 'claude':
            from claude_politikasi import period as claude_period, ZONE
            period = claude_period(datetime.fromtimestamp(now, ZONE))
            row = snapshot['claude']
            # 'fresh' (<=30 min) or butce.py's stale-but-compensated observation ('gecerli': True, <=7 days).
            if not (row.get('kaynak') == 'taze' or row.get('gecerli') is True):
                raise BudgetDenied('Claude quota observation unavailable')
            # The weekly reserve policy supplies remaining percentage. Without
            # a conversion observation, an automatic USD authorization is unknown.
            value = min(amount(row['pay']) * amount(row['yuzde1_usd']), amount(row['havuz_kalan_usd']))
            return period, int((Decimal(str(value)) * 1_000_000).to_integral_value(rounding=ROUND_FLOOR))
        if provider in PAID:
            row = snapshot[PAID[provider]]
            if row.get('db') != 'ok':
                raise BudgetDenied('accounting unavailable')
            if provider == 'opencode-go' and row.get('pencere_acik') is not True:
                raise BudgetDenied('provider window closed')
            value = amount(row['kullanilabilir'])
            if provider == 'opencode-go':
                if row.get('birim') != 'puan':
                    raise BudgetDenied('Go pool unit unknown')
                from go_pencere import usd as go_usd
                value = go_usd(model or '', value)
            if provider in ('opencode', 'openrouter'):
                # One 85% boundary for admission, the running brake and reopening.
                # Use decimal arithmetic so the exact boundary cannot flicker.
                margin = Decimal(str(amount(row['pay']))) * Decimal('0.15')
                value = max(Decimal(0), Decimal(str(value)) - margin)
            return period, int((Decimal(str(value)) * 1_000_000).to_integral_value(rounding=ROUND_FLOOR))
        if provider.endswith(BEDAVA_SONEK):
            taban = taban_saglayici(provider)
            if taban not in PAID and taban not in FREE and taban != 'opencode':
                raise BudgetDenied('unmetered provider')
            # Even at zero price the provider window binds: while the Go window is closed no call goes out.
            if taban == 'opencode-go' and snapshot.get('go', {}).get('pencere_acik') is not True:
                raise BudgetDenied('provider window closed')
            # The daily count is held by the ledger's own 'settled' total: since the allocation cap is fixed with min()
            # within the period, returning a fixed cap gives the right result (1 is reserved per request).
            if taban == 'openrouter':
                counter = (snapshot.get('or') or {}).get('bedava_istek') or {}
                if any(type(counter.get(k)) is not int or counter[k] < 0 for k in ('used', 'limit', 'remaining')):
                    raise BudgetDenied('OpenRouter account request limit unknown')
                if counter['remaining'] > max(0, counter['limit'] - counter['used']):
                    raise BudgetDenied('OpenRouter account request count inconsistent')
                return period, counter['remaining']
            return period, bedava_gun_tavan()
        taban = taban_saglayici(provider)
        if taban in FREE:
            key, limit = FREE[taban]
            row = snapshot[key]
            if row.get('db') != 'ok':
                raise BudgetDenied('request accounting unavailable')
            # If the bucket carries a MODEL (<provider>#<model>), both the limit and the counter belong to that model.
            model = model_kimligi(provider)
            if model:
                limit = model_gunluk_istek(taban, model)
                sayaclar = row.get('bugun_istek_model')
                # Without a per-model counter (old observation) we use the provider total: the cap is the model's,
                # the count is the provider's — it over-counts, never under-counts (fail-closed, and it does not block the call).
                used = amount(sayaclar.get(model, 0)) if isinstance(sayaclar, dict) else amount(row['bugun_istek'])
            else:
                used = amount(row['bugun_istek'])
            if not used.is_integer():
                raise BudgetDenied('invalid request count')
            return period, max(0, limit - int(used))
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        raise BudgetDenied('unknown budget') from exc
    raise BudgetDenied('unmetered provider')


USD_PROVIDERS = frozenset({*PAID, 'claude'})


HAKEM_ROL = 'kalite'
HAKEM_PAY_ORAN = 0.15          # Item 3: this much of the run cap is reserved for the judge
HAKEM_PAY_TABAN = 200_000      # ... and at least $0.20


def hakem_rezervi(cap: int, oran: float = HAKEM_PAY_ORAN) -> int:
    """Micro-dollars reserved for the judge: the contract ratio (default 15%), at least $0.20, at most half the cap (B110).
    Without the half limit, a small cap leaves nothing for the lanes."""
    if cap <= 0:
        return 0
    try:
        oran = float(oran)
    except (TypeError, ValueError):
        oran = HAKEM_PAY_ORAN
    if not 0 <= oran <= 1:
        oran = HAKEM_PAY_ORAN
    return min(cap // 2, max(int(cap * oran), HAKEM_PAY_TABAN))


ALARM_PENCERE_SN = 600
ALARM_MUTLAK_TABAN = 1_000_000
ALARM_ILERLEMESIZ_SN = 1200
ALARM_ILERLEMESIZ_CAGRI = 3


ILERLEME_NEDENLERI = ('qa-pass', 'kabul', 'serit-uretti', 'plan-kabul', 'plan-inceleme')


def urun_kimligi(kayit: dict[str, Any]) -> str | None:
    """N-BU06: product evidence = worktree product digest; unknown (None) if there is no worktree or it cannot be read."""
    wt = kayit.get('worktree')
    if not wt or not Path(str(wt)).is_dir():
        return None
    try:
        from sonuc import revision
        return revision(wt)['digest']
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def harcama_alarmi(home: Path, con: sqlite3.Connection, run_id: str, simdi: float | None = None) -> tuple[str | None, dict | None]:
    """N-BU06: speed is only a warning; a recorded definite alarm waits for a separate approval. Cash and subscription equivalents are measured separately.
    Progress is split three ways: productive (recorded step/product/criterion/test evidence in the window), none (in a continuously expensive section
    >= ALARM_ILERLEMESIZ_CAGRI closed calls, no progress record AND the product digest unchanged since the section start) and
    unknown (the rest). Only `none` produces a definite alarm (FIX-53/CR-03); a missing record alone is not inefficiency."""
    simdi = time.time() if simdi is None else simdi
    try:
        kayit = RunStore(home).read(run_id)
    except (OSError, ValueError, KeyError):
        return None, None
    onay = amount(kayit.get('harcama_onayi_ts') or 0)
    eski = kayit.get('harcama_alarmi') or {}
    if eski and amount(eski.get('ts') or 0) > onay:
        return f"harcama alarmi: onay bekleniyor ({eski.get('neden')})", None
    tavan = run_cap_micro(home, run_id) or 0
    taban_ts = max(amount(kayit.get('created') or simdi), onay)
    taban = int(amount(os.environ.get('CADRE_HARCAMA_ALARM_TABAN_USD', '0.20')) * 1_000_000)   # a cent of jitter is not an alarm
    taban = max(taban, int(0.2 * tavan))
    havuzlar = []
    for scope, providers, unit in (('cash', ('opencode', 'openrouter'), 'USD'),
                                    ('opencode-go', ('opencode-go',), 'USD-equivalent'),
                                    ('claude', ('claude',), 'USD-equivalent')):
        marks = ','.join('?' * len(providers))
        satirlar = con.execute(f"select created, actual from reservation where json_extract(metadata,'$.run_id')=? and provider in ({marks}) "
                               "and actual is not null and created >= ? and created <= ?",
                               (run_id, *providers, taban_ts, simdi)).fetchall()
        toplam = sum(a for _, a in satirlar)
        pencere = sum(a for c, a in satirlar if c >= simdi - ALARM_PENCERE_SN)
        ortalama = toplam * ALARM_PENCERE_SN / max(simdi - taban_ts, ALARM_PENCERE_SN)
        neden = None
        if tavan and pencere > max(0.4 * tavan, ALARM_MUTLAK_TABAN):
            neden = f"son 10 dk ${pencere / 1e6:.2f} > tavanin %40'i (${0.4 * tavan / 1e6:.2f})"
        elif pencere >= taban and pencere > 3 * ortalama:
            neden = f"son 10 dk ${pencere / 1e6:.2f} > ortalama hizin 3 kati (${ortalama / 1e6:.2f}/10 dk)"
        if neden:
            havuzlar.append({'scope': scope, 'unit': unit, 'neden': neden, 'pencere_usd': round(pencere / 1e6, 6)})
    if not havuzlar:
        return None, None
    p = kayit.get('budget_progress') or {}
    try:
        kayit_ts = amount(p.get('ts') or 0) if isinstance(p, dict) else 0.0
        productive = p.get('neden') in ILERLEME_NEDENLERI and bool(p.get('kimlik')) and 0 <= simdi - kayit_ts <= 1800
    except (ValueError, TypeError, AttributeError, BudgetDenied):
        kayit_ts, productive = 0.0, False
    uyari = {'ts': simdi, 'kind': 'speed-warning', 'neden': '; '.join(f"{h['scope']}: {h['neden']}" for h in havuzlar),
             'havuzlar': havuzlar, 'tavan_usd': round(tavan / 1e6, 6),
             'ilerleme': 'productive' if productive else 'unknown', 'kanit': p if productive else None, 'ilk_ts': simdi, 'urun': None}
    onceki = kayit.get('harcama_uyarisi') or {}
    try:
        onceki_ts, onceki_ilk = amount(onceki.get('ts') or 0), amount(onceki.get('ilk_ts') or 0)
    except (BudgetDenied, AttributeError):
        onceki_ts = onceki_ilk = 0
    devam = onceki.get('kind') == 'speed-warning' and onceki_ilk >= taban_ts and 0 <= simdi - onceki_ts <= ALARM_PENCERE_SN
    if devam:
        uyari['ilk_ts'] = onceki_ilk
        uyari['urun'] = onceki.get('urun') if isinstance(onceki.get('urun'), str) else None
    if productive:
        return None, uyari
    try:
        sure = amount(os.environ.get('CADRE_HARCAMA_ALARM_ILERLEMESIZ_SN', str(ALARM_ILERLEMESIZ_SN)))
    except BudgetDenied:
        sure = float(ALARM_ILERLEMESIZ_SN)
    if not devam or uyari['urun'] is None:
        uyari['urun'] = urun_kimligi(kayit)
    cagri = con.execute("select count(*) from reservation where json_extract(metadata,'$.run_id')=? and actual is not null "
                        "and created >= ? and created <= ?", (run_id, uyari['ilk_ts'], simdi)).fetchone()[0]
    uyari['cagri'] = cagri
    kayitli_adim = uyari['ilk_ts'] <= kayit_ts <= simdi
    if sure > 0 and simdi - uyari['ilk_ts'] >= sure and cagri >= ALARM_ILERLEMESIZ_CAGRI and not kayitli_adim and uyari['urun'] is not None:
        simdiki = urun_kimligi(kayit)
        if simdiki is not None and simdiki == uyari['urun']:
            uyari['ilerleme'] = 'none'
            neden = (f"{int(sure // 60)} dk suren hiz uyarisinda urun degismedi ve kayitli adim yok ({cagri} cagri); {uyari['neden']}")
            return f"harcama alarmi: onay bekleniyor ({neden})", {'ts': simdi, 'kind': 'no-progress-spend', 'neden': neden, 'uyari': uyari}
    return None, uyari


def run_cap_micro(home: Path, run_id: str | None) -> int | None:
    """P5: the run's WRITTEN USD cap (micro-dollars); no record/field -> None (no cap applied).
    This value is the run's contract and independent of role; use `rol_tavani` for role narrowing."""
    if not run_id:
        return None
    try:
        cap = RunStore(home).read(run_id).get('budget_cap_usd')
    except (OSError, ValueError, KeyError) as exc:
        # B58 (T1): unknown counts as CLOSED. The old code returned None ("no cap") on every error: a broken/unreadable run.json
        # silently removed the run cap (the rest of the module is fail-closed).
        raise BudgetDenied('run budget record unreadable') from exc
    if cap is None:
        return None
    try:
        toplam = int((Decimal(str(amount(cap))) * 1_000_000).to_integral_value(rounding=ROUND_FLOOR))
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise BudgetDenied('run budget cap unreadable') from exc
    return toplam


def operator_tavani(record: dict[str, Any]) -> int | None:
    try:
        values = [record[k] for k in ('operator_budget_usd', 'project_budget_cap_usd') if k in record]
        for key in ('request_budget', 'project_operator_budget'):
            if record.get(key):
                values.append(record[key]['amount_usd'])
        return min(int(Decimal(str(amount(value))) * 1_000_000) for value in values) if values else None
    except (ValueError, TypeError, ArithmeticError, KeyError) as exc:
        raise BudgetDenied('operator budget record unreadable') from exc


def butce_saglayicilari(provider: str) -> frozenset[str]:
    return frozenset({'opencode', 'openrouter'}) if provider in ('opencode', 'openrouter') else frozenset({provider}) if provider else USD_PROVIDERS


# HARD SPLIT: each role spends only from ITS OWN pocket, call order does not change the outcome (with a $5 cap
# lanes $3.75 · judge $0.75 · helper $0.50).
def seritler_bitti(home: Path, run_id: str | None) -> bool:
    """Are all lanes in the plan terminal? A hand-off is safe only once the handing role's work is done;
    since the judge runs after QA, when the lanes are done the remaining share goes to the judge (B82)."""
    if not run_id:
        return False
    try:
        from sozlesme import oku as sozlesme_oku
        if (RunStore(home).path(run_id) / 'ek-serit.flag').exists():
            return False   # B226: G-R7 extra lanes (second analyst, reconciler) are part of the writer lane; no hand-off until they finish
        kayit = RunStore(home).read(run_id)
        sozlesme = sozlesme_oku(home, run_id) or {}
        seritler = [s.get('id') for s in (sozlesme.get('seritler') or []) if s.get('id')]
        if not seritler:
            return False
        gorevler = kayit.get('tasks') or {}
        return all(isinstance(gorevler.get(g), dict) and gorevler[g].get('state') in ('uretti', 'completed')
                   for g in seritler)
    except Exception:
        return False


def rol_tavani(home: Path, run_id: str | None, rol: str | None = None, dilim_adi: str | None = None,
               provider: str = '', connection: sqlite3.Connection | None = None) -> int | None:
    """The part of the run cap open to this ROLE: each role sees only its own pocket, which fills with its own spending (hard split).
    Shares come from the run contract (lanes 75% · judge 15% · helper 10%); when the lanes finish the remaining share goes to the judge."""
    toplam = run_cap_micro(home, run_id)
    if toplam is None:
        return None
    etkin = rol or os.environ.get('CADRE_ROL') or ''
    try:
        from sozlesme import gorev_dilimi, tabanlar
        benim = dilim_adi or gorev_dilimi(etkin)
        paylar = tabanlar(home, run_id)
    except Exception:
        benim, paylar = dilim_adi or 'seritler', {'seritler': 0.75, 'hakem': 0.15, 'yardimci': 0.10}
    # Judge/helper take their percentage, lanes get the REMAINDER; the pockets add up to the full cap. The judge pocket is protected
    # by a $0.20 minimum and cannot exceed half the cap.
    yardimci_payi = int(toplam * float(paylar.get('yardimci', 0)))
    hakem_orani = float(paylar.get('hakem', HAKEM_PAY_ORAN))              # B110: the pocket comes from the contract
    guvence = 0
    if run_id:
        try:
            record = RunStore(home).read(run_id)
            floors = record.get('kabul_guvence_tabanlari_micro')
            pool = 'cash' if provider in ('opencode', 'openrouter') else provider
            guvence = max(0, int((floors.get(pool, 0) if provider and floors is not None else record.get('kabul_guvence_taban_micro')) or 0))
        except (OSError, ValueError, TypeError):
            raise BudgetDenied('acceptance reservation record unreadable')
    hakem_tabani = min(toplam - yardimci_payi, max(hakem_rezervi(toplam, hakem_orani), guvence))
    serit_payi = max(0, toplam - hakem_tabani - yardimci_payi)   # lanes get the REMAINDER
    devir = seritler_bitti(home, run_id)
    # Hand-off (B82): once all lanes are terminal the remaining share goes to the judge, the lane pocket is FROZEN
    # where it spent. Invariant: SUM(pockets) <= cap at all times.
    serit_harcadi = 0
    if devir:
        defter = home / 'zincir/request-budget.db'
        if defter.exists():          # no ledger means no spending either
            try:
                with closing(sqlite3.connect(defter.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)) as con:
                    kesin, askida = run_usage(con, str(run_id), 'seritler', providers=butce_saglayicilari(provider), bounded=True)
                    serit_harcadi = kesin + askida
            except (OSError, sqlite3.Error):
                devir = False        # if we cannot read it we do not hand off (fail-closed)
    if provider and connection is None and (home / 'zincir/request-budget.db').exists():
        with closing(sqlite3.connect((home / 'zincir/request-budget.db').resolve().as_uri() + '?mode=ro', uri=True, timeout=15)) as con:
            return rol_tavani(home, run_id, rol, dilim_adi, provider, con)
    if provider and connection is not None and connection.execute("select 1 from sqlite_master where name='role_allocation'").fetchone():
        pool = 'cash' if provider in ('opencode', 'openrouter') else provider
        row = connection.execute('select total,caps from role_allocation where run_id=? and pool=?', (run_id, pool)).fetchone()
        if row:
            caps = json.loads(row[1])
            if row[0] != toplam or sum(caps.values()) != toplam or any(type(v) is not int or v < 0 for v in caps.values()):
                raise BudgetDenied('role allocation changed or unreadable')
            if devir:
                spent = sum(run_usage(connection, str(run_id), 'seritler', providers=butce_saglayicilari(provider), bounded=True))
                caps['hakem'] += max(0, caps['seritler'] - spent)
                caps['seritler'] = min(caps['seritler'], spent)
            return caps[benim]
    if benim == 'hakem':
        taban = hakem_tabani   # contract ratio, at least $0.20, at most half the cap
        if not devir:
            return taban
        return max(taban, toplam - min(serit_harcadi, serit_payi) - yardimci_payi)
    if benim == 'seritler':
        # Once the hand-off opened, the lane pocket freezes where it spent: its money went to the judge (the cap contract outranks cloud comfort).
        return min(serit_payi, serit_harcadi) if devir else serit_payi
    return max(0, yardimci_payi)


def rol_butcesi(home: Path, run_id: str | None, rol: str | None = None, provider: str = '') -> tuple[int | None, str]:
    """(cap, dilim): a role's pocket and the pocket's name come from one place; if they diverge the judge's cap is applied while its spending is charged to the lane pocket."""
    try:
        from sozlesme import gorev_dilimi
        ad = gorev_dilimi(rol or os.environ.get('CADRE_ROL') or '')
    except Exception:
        ad = 'seritler'
    return rol_tavani(home, run_id, rol, provider=provider), ad


def run_usage(con: sqlite3.Connection, run_id: str, dilim: str | None = None, plan_guvencesi_haric: bool = False,
              providers: frozenset[str] = USD_PROVIDERS, bounded: bool = False) -> tuple[int, int]:
    """(settled, pending) micro-dollars — this run's reservations across USD providers.
    If `dilim` is given only that pocket is counted (hard split); old records without a pocket count as 'seritler'."""
    marks = ','.join('?' * len(providers))
    kosul = "json_extract(metadata,'$.run_id')=? and provider in (%s)" % marks
    param: tuple = (run_id, *providers)
    if dilim:
        kosul += " and coalesce(json_extract(metadata,'$.dilim'),'seritler')=?"
        param = (*param, dilim)
    if plan_guvencesi_haric:   # the plan reserve holds provider money, not the run pocket (it made the primary plan get denied)
        kosul += " and not (coalesce(json_extract(metadata,'$.guvence'),0)=1 and json_extract(metadata,'$.guvence_rol')='plan')"
    settled = con.execute(f"select coalesce(sum(actual),0) from reservation where {kosul} and actual is not null", param).fetchone()[0]
    bound = "max(reserved,coalesce(json_extract(metadata,'$.en_kotu'),reserved))" if bounded else 'reserved'
    held = con.execute(f"select coalesce(sum({bound}),0) from reservation where {kosul} and actual is null", param).fetchone()[0]
    return int(settled), int(held)


def kosu_modelleri(home: Path, run_id: str | None, roller: tuple[str, ...] = (), *, salt_okur_haric: bool = False,
                   haric_denemeler: frozenset[str] | set[str] = frozenset()) -> set[str]:
    """Models called in this run (from ledger metadata; if `roller` is given, only those roles).
    For judge independence: the family of the model that wrote the product cannot be the judge (B110)."""
    db = Path(home) / 'zincir/request-budget.db'
    if not run_id or not db.exists():
        return set()
    try:
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)) as con:
            rows = con.execute("select json_extract(metadata,'$.role'), json_extract(metadata,'$.phase_role'), "
                               "json_extract(metadata,'$.model'), json_extract(metadata,'$.attempt_id') "
                               "from reservation where json_extract(metadata,'$.run_id')=? "
                               "and coalesce(json_extract(metadata,'$.guvence'),0)!=1 "
                               # the direct HTTP helper (plan_ds) has no tools and cannot write to the product; it must not narrow the judge pool
                               "and (? = 0 or coalesce(json_extract(metadata,'$.adapter'),'') != 'direct')",
                               (run_id, int(salt_okur_haric))).fetchall()
    except (OSError, sqlite3.Error) as exc:
        if salt_okur_haric:
            raise BudgetDenied('writer accounting unavailable') from exc
        return set()
    readonly = {'plan', 'cadre-plan', 'scout', 'cadre-scout', 'compaction', 'otopsi'}
    # The OpenCode main agent also reports "build" during planning. Only open this generic label
    # with the engine's phase; sub-agents keep their own roles. Old records do not change.
    # `haric_denemeler`: receipts of judge calls PROVEN by an attempt record not to touch the product (§0a/10a).
    resolved = ((phase if rol == 'build' and phase else rol, model) for rol, phase, model, deneme in rows
                if deneme not in haric_denemeler)
    return {str(model) for rol, model in resolved if model and (not roller or str(rol or 'impl') in roller)
            and (not salt_okur_haric or rol not in readonly)}


def kosu_harcama_yaz(home: Path, ledger: 'BudgetLedger', run_id: str | None) -> None:
    """Structured spending in the run record: budget_spent_usd (settled) and budget_held_usd (pending). `cadre durum` shows it."""
    if not run_id or not ledger.path.exists():
        return
    try:
        with closing(sqlite3.connect(ledger.path.resolve().as_uri()+'?mode=ro', uri=True, timeout=15)) as con:
            settled, held = run_usage(con, run_id)
            rows = con.execute("select provider, reserved, coalesce(actual,0), actual is null, created, metadata from reservation "
                               "where json_extract(metadata,'$.run_id')=? order by created", (run_id,)).fetchall()
        # B55: "how much money, at which step" from a single source — provider/task/role/model breakdown + unclosed reservations
        kirilim: dict[tuple, dict] = {}
        askida = []
        for prov, reserved, actual, pending, created, meta in rows:
            try:
                m = json.loads(meta or '{}')
            except ValueError:
                m = {}
            key = (prov, str(m.get('task') or m.get('task_id') or ''), str(m.get('role') or ''), str(m.get('model') or ''))
            a = kirilim.setdefault(key, {'provider': prov, 'task': key[1], 'role': key[2], 'model': key[3], 'n': 0, 'reserved_usd': 0.0, 'actual_usd': 0.0, 'pending': 0})
            a['n'] += 1; a['reserved_usd'] += reserved / 1e6; a['actual_usd'] += actual / 1e6; a['pending'] += int(bool(pending))
            if pending:
                askida.append({'provider': prov, 'role': key[2], 'model': key[3], 'reserved_usd': round(reserved / 1e6, 6), 'created': created})
        breakdown = [{**a, 'reserved_usd': round(a['reserved_usd'], 6), 'actual_usd': round(a['actual_usd'], 6)} for a in kirilim.values()]
        RunStore(home).update(run_id, budget_spent_usd=round(settled / 1_000_000, 6), budget_held_usd=round(held / 1_000_000, 6),
                              budget_breakdown=breakdown, budget_unsettled=askida[:20])
    except (OSError, sqlite3.Error, ValueError, KeyError):
        pass








def adim_maliyeti(provider: str, model: Any, cost: Any, tokens: Any) -> float:
    """B230/B280: OpenCode's `cost` may count cache reads as $0; some providers charge the full input
    price. Without a REAL cache discount in the catalogue a step is not counted below token x list price (max: no double counting)."""
    kayitli = amount(cost)
    if not model:
        return kayitli
    from katalog import fiyat
    f = fiyat(provider, str(model))
    if not f or 'input' not in f:
        return kayitli
    onbellek = float(f.get('cache_read', f['input']))
    if onbellek < float(f['input']) and kayitli > 0:   # real discount + record present: the provider's price is kept
        return kayitli
    try:
        t = json.loads(tokens) if isinstance(tokens, str) else (tokens or {})
        c = t.get('cache') or {}
        girdi = int(t.get('input') or 0) + int(c.get('write') or 0)
        okunan = int(c.get('read') or 0)
        cikti = int(t.get('output') or 0) + int(t.get('reasoning') or 0)
    except (TypeError, ValueError, AttributeError):
        return kayitli
    # B296: with a discounted cache, a zero record is not counted below token x (discounted) list price either
    return max(kayitli, (girdi * float(f['input']) + okunan * onbellek + cikti * float(f.get('output', 0) or 0)) / 1_000_000)


def ilerleme_yaz(home: Path, run_id: str, neden: str, kimlik: str) -> None:
    """G-B5/P2: re-reading the same QA/product does not renew the progress clock. N-BU06: lane output and plan acceptance are also recorded steps;
    cap extension (`tavan_ilerlemesi`) still looks only at QA PASS/acceptance evidence."""
    if neden not in ILERLEME_NEDENLERI or not kimlik:
        return
    store = RunStore(home)
    with FileLock(store.path(run_id) / '.record.lock'):
        r = store.read(run_id)
        seen = r.setdefault('budget_progress_seen', {})
        history = r.setdefault('budget_progress_history', [])
        identity = [neden, kimlik]
        if seen.get(neden) == kimlik or identity in history:
            return
        history.append(identity)
        seen[neden] = kimlik
        r['budget_progress'] = {'ts': time.time(), 'neden': neden, 'kimlik': kimlik}
        atomic_json(store.path(run_id) / 'run.json', r)
    if r.get('proje_ust'):
        ilerleme_yaz(home, r['proje_ust'], neden, run_id + ':' + kimlik)


def tavan_ilerlemesi(home: Path, record: dict[str, Any], alan: str = 'budget_cap_usd') -> dict[str, Any] | None:
    now = time.time()
    p = record.get('budget_progress') or {}
    used = [e.get('kanit') or {} for e in record.get('budget_extensions', []) if e.get('alan') == alan]
    def fresh(proof: dict[str, Any]) -> bool:
        return not any((old.get('neden'), old.get('kimlik')) == (proof.get('neden'), proof.get('kimlik')) for old in used)
    if p.get('neden') in ('qa-pass', 'kabul') and p.get('kimlik') and fresh(p) and 0 <= now - amount(p.get('ts', 0)) <= 1800:
        return p
    if record.get('project_active_run'):
        child = RunStore(home).read(record['project_active_run'])
        if child.get('proje_ust') == record['run_id']:
            p = tavan_ilerlemesi(home, child, alan)
            if p:
                p = {**p, 'kimlik': child['run_id'] + ':' + p['kimlik']}
                if fresh(p):
                    return p
    return None


def tavan_uzat(home: Path, run_id: str, alan: str) -> bool:
    """P2: healthy work extends by 50% each time it fills up; the provider share is a separate hard limit."""
    store = RunStore(home)
    with FileLock(store.path(run_id) / '.record.lock'):
        r = store.read(run_id)
        if alan == 'project_budget_cap_usd' or r.get('budget_manual') or (alan == 'budget_cap_usd' and 'budget_plan_cap_usd' not in r):
            return False
        kanit = tavan_ilerlemesi(home, r, alan)
        cap = amount(r.get(alan))
        if not kanit or cap <= 0 or cap * 1.5 * 1e6 >= 2**53:
            return False
        bound = operator_tavani(r)
        yeni = min(math.ceil(cap * 1.5 * 1e6), bound if bound is not None else int(cap * 1e6)) / 1e6
        if yeni <= cap:
            return False
        r[alan] = yeni
        r.setdefault('budget_extensions', []).append({'ts': time.time(), 'alan': alan, 'once': cap, 'sonra': yeni, 'kanit': kanit})
        atomic_json(store.path(run_id) / 'run.json', r)
        return True


MAKBUZ_ASIM_SQL = ("from reservation where json_extract(metadata,'$.run_id')=? and actual > coalesce("
                   "json_extract(metadata,'$.overage_accepted_micro'), json_extract(metadata,'$.verified_bound_micro'), "
                   "json_extract(metadata,'$.en_kotu'), reserved)")


def makbuz_siniri(con: sqlite3.Connection, run_ids: set[str]) -> None:
    """N-BU01/06: excess receipts cannot be bypassed by approval or by handing off to another paid route; only a recorded operator acceptance (FIX-17) reopens the family."""
    for rid in run_ids:
        if con.execute('select 1 ' + MAKBUZ_ASIM_SQL + ' limit 1', (rid,)).fetchone():
            raise BudgetDenied('run budget exhausted: project receipt bound exceeded')


def makbuz_asimlari(con: sqlite3.Connection, run_ids: set[str]) -> list[dict[str, Any]]:
    """FIX-17 (N-BU06): unaccepted excess receipts; acceptance is recorded with `overage_accepted_micro` and does not grow the caps."""
    rows = []
    for rid in sorted(run_ids):
        for ident, provider, actual, reserved, meta, created in con.execute(
                'select id, provider, actual, reserved, metadata, created ' + MAKBUZ_ASIM_SQL + ' order by created', (rid,)):
            try:
                m = json.loads(meta) if meta else {}
                bound = int(m.get('verified_bound_micro') or m.get('en_kotu') or reserved)
            except (ValueError, TypeError, AttributeError):
                bound = reserved
            rows.append({'id': ident, 'provider': provider, 'run_id': rid, 'actual': actual, 'bound': bound, 'created': created})
    return rows


def makbuz_kabul(home: Path, ident: str, operator: str = 'operator') -> dict[str, Any]:
    """FIX-17 (N-BU06): an excess receipt reopens the family with a recorded operator acceptance; the real amount stays in the ledger, total/monthly caps do not grow."""
    if os.environ.get('CADRE_RUN_ID'):
        raise BudgetDenied('receipt acceptance is an operator command outside a run')
    ledger = BudgetLedger(home / 'zincir/request-budget.db')
    with ledger.transaction() as con:
        row = con.execute('select provider, actual, reserved, metadata from reservation where id=?', (ident,)).fetchone()
    if row is None:
        raise BudgetDenied('unknown reservation')
    provider, actual, reserved, meta = row
    if actual is None:
        raise BudgetDenied('reservation has no receipt yet')
    try:
        m = json.loads(meta) if meta else {}
        bound = int(m.get('verified_bound_micro') or m.get('en_kotu') or reserved)
    except (ValueError, TypeError, AttributeError) as exc:
        raise BudgetDenied('reservation metadata unreadable') from exc
    kabul = {'id': ident, 'provider': provider, 'run_id': m.get('run_id'), 'actual': actual, 'bound': bound}
    if m.get('overage_accepted_micro') is not None and int(m['overage_accepted_micro']) >= actual:
        return {**kabul, 'already': True}
    if actual <= bound:
        raise BudgetDenied('receipt within its verified bound')
    run_id = m.get('run_id')
    store = RunStore(home)
    if run_id and (store.path(run_id) / 'run.json').exists():
        from kosu import mudahale_ekle
        with FileLock(store.path(run_id) / '.record.lock'):
            record = store.read(run_id)
            mudahale_ekle(record, 'receipt-overage-accepted', {'reservation': ident, 'provider': provider,
                                                               'actual_usd': round(actual / 1e6, 6), 'bound_usd': round(bound / 1e6, 6)})
            atomic_json(store.path(run_id) / 'run.json', record)
    with ledger.transaction() as con:
        con.execute("update reservation set metadata=json_set(metadata,'$.overage_accepted_micro',?,'$.overage_accepted_ts',?,"
                    "'$.overage_accepted_by',?,'$.overage_bound_micro',?) where id=? and actual=?",
                    (actual, time.time(), operator, bound, ident, actual))
    _kurtarma_log(home, {'islem': 'makbuz-kabul', 'kayit': ident, 'saglayici': provider, 'run_id': run_id,
                         'actual_micro': actual, 'bound_micro': bound, 'operator': operator})
    return {**kabul, 'already': False}


def dinamik_tavanlar(home: Path, con: sqlite3.Connection, run_id: str, run_cap: int | None,
                    dilim: str, maximum: int, uzat: bool, provider: str = '', *, preview: bool = False) -> tuple[int | None, int | None]:
    """G-B5/P2: while the ledger transaction is locked, the role + parent total limit are computed together."""
    store = RunStore(home)
    def read(rid: str) -> dict[str, Any]:
        try:
            return store.read(rid)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise BudgetDenied('project budget ownership unavailable') from exc
    if not (store.path(run_id) / 'run.json').exists():
        return run_cap, None
    r = read(run_id)
    makbuz_siniri(con, {run_id})
    ids, cap = operator_ailesi(home, run_id)
    makbuz_siniri(con, ids)
    providers = butce_saglayicilari(provider)
    used_total = sum(sum(run_usage(con, rid, providers=providers, bounded=True)) for rid in ids)
    project_remaining = max(0, cap - used_total) if cap is not None else None
    uzat = uzat and (project_remaining is None or maximum <= project_remaining)
    if run_cap is not None and 'budget_plan_cap_usd' in r:
        used = sum(run_usage(con, run_id, dilim, providers=butce_saglayicilari(provider), bounded=True))
        run_cap = rol_tavani(home, run_id, dilim_adi=dilim)
        rol_uzat = uzat and not preview and not (dilim == 'seritler' and seritler_bitti(home, run_id))
        for _ in range(128):
            if used + maximum <= run_cap or not rol_uzat or not tavan_uzat(home, run_id, 'budget_cap_usd'):
                break
            yeni = rol_tavani(home, run_id, dilim_adi=dilim)
            if yeni <= run_cap:
                break
            run_cap = yeni
    allocation = con.execute("select 1 from sqlite_master where name='role_allocation'").fetchone()
    pool = 'cash' if provider in ('opencode', 'openrouter') else provider
    transferred = allocation and con.execute('select 1 from role_allocation where run_id=? and pool=?', (run_id, pool)).fetchone()
    pending = dilim == 'yardimci' and r.get('plan_review_claim') and bekleyen_inceleme(home, r, store.plan_view(run_id))
    if (run_cap is not None and dilim in ('seritler', 'hakem', 'yardimci')
            and ('budget_plan_cap_usd' in r or 'budget_need_micro' in r or transferred or pending)):
        run_cap = rol_tavani(home, run_id, dilim_adi=dilim, provider=provider, connection=con)
        used = sum(run_usage(con, run_id, dilim, providers=butce_saglayicilari(provider), bounded=True))
        if uzat and used + maximum > run_cap:
            run_cap = rol_aktar(home, con, r, provider, dilim, used + maximum, run_cap, preview=preview)
    return run_cap, project_remaining


def aile_adayi(store: RunStore, path: Path) -> dict[str, Any] | None:
    """FIX-28: an unrelated unreadable record does not block the family; member candidates are found only by run_id/proje_ust, members are read strictly."""
    try:
        return store.read(path.parent.name)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get('run_id'), str):
        return None
    return {'run_id': raw['run_id'], 'proje_ust': raw.get('proje_ust')}


def operator_ailesi(home: Path, run_id: str) -> tuple[set[str], int | None]:
    store = RunStore(home)
    try:
        root = run_id
        record = store.read(root)
        seen = set()
        while record.get('proje_ust'):
            if root in seen:
                raise BudgetDenied('project budget ownership cycle')
            seen.add(root)
            root = record['proje_ust']
            record = store.read(root)
        cap = operator_tavani(record)
        ids = set(record.get('project_run_ids') or []) | {root, run_id}
        records = [aile_adayi(store, p) for p in store.root.glob('*/run.json')]
        records = [c for c in records if c is not None]
        for _ in range(len(records) + 1):
            children = {c['run_id'] for c in records if c.get('proje_ust') in ids}
            if children <= ids:
                break
            ids.update(children)
        for rid in ids:
            store.read(rid)
        return ids, cap
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BudgetDenied('project budget ownership unavailable') from exc


def bekleyen_inceleme(home: Path, record: dict[str, Any], view: dict[str, Any]) -> bool:
    """Only the engine's owned review belonging to the current plan may take a share from the main job (G-B5)."""
    from kosu import TERMINAL, project_time_expired
    task = os.environ.get('CADRE_GOREV_ID', '')
    claim = record.get('plan_review_claim') or {}
    return bool(re.fullmatch(r'plan-review-[1-9][0-9]*-[a-f0-9]{8}', task)
                and claim.get('task_id') == task and claim.get('revision') == view['revision']
                and task.startswith(f"plan-review-{view['revision']}-") and os.environ.get('CADRE_ROL') == 'review'
                and view['revision'] > 0 and view['execution_plan_matches']
                and not (view['held'] and view['changes'][-1].get('status') == 'stop_requested')
                and record['state'] not in TERMINAL and not project_time_expired(record)
                and not (RunStore(home).path(record['run_id']) / 'cancel.flag').exists())


def rol_aktar(home: Path, con: sqlite3.Connection, record: dict[str, Any], provider: str,
              dilim: str, gereken: int, cap: int, *, preview: bool = False) -> int:
    rid = record['run_id']
    if dilim == 'seritler' and seritler_bitti(home, rid):
        return cap
    proof = tavan_ilerlemesi(home, record)
    view = RunStore(home).plan_view(rid)
    review = (view['reviews'] or [{}])[-1]
    reviewed = (view['revision'] > 0 and review.get('status') == 'PASS'
                and review.get('revision_number') == view['revision'] and view['execution_plan_matches'] and not view['held'])
    pending = dilim == 'yardimci' and bekleyen_inceleme(home, record, view)
    if not proof and not reviewed and not pending:
        return cap
    needs = dict(record.get('budget_need_micro') or {})
    if reviewed:
        needs['yardimci'] = 0
    if seritler_bitti(home, rid):
        needs['seritler'] = 0
    pool = 'cash' if provider in ('opencode', 'openrouter') else provider
    floors = record.get('kabul_guvence_tabanlari_micro')
    floor = floors.get(pool, 0) if floors is not None else record.get('kabul_guvence_taban_micro', 0)
    if floor:
        needs['hakem'] = max(needs.get('hakem') or 0, int(floor))
    slices = ('seritler', 'hakem', 'yardimci')
    caps = {s: rol_tavani(home, rid, dilim_adi=s, provider=provider, connection=con) for s in slices}
    if any(v is None for v in caps.values()):
        return cap
    if pending and not proof and not reviewed:
        from dispatch_util import validate_plan
        plan = view['history'][-1]['plan']
        validate_plan(plan)
        tasks = plan.get('gorevler') or []
        estimates = [t.get('tahmin_usd') for t in tasks]
        known = bool(estimates) and all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in estimates)
        if record.get('mode') == 'plan-only' or plan.get('kind') == 'verified-backlog':
            work_need = 0
        elif known:
            work_need = math.ceil(sum(estimates) * 1e6)
        else:
            return cap
        policy = inceleme_kurali(record, 'plan-review')
        if gereken > run_cap_micro(home, rid) * policy['pay_yuzde'] // 100:
            return cap
        spent = sum(run_usage(con, rid, 'seritler', providers=butce_saglayicilari(provider), bounded=True))
        needs = {'seritler': spent + work_need, 'hakem': caps['hakem']}
    pool = 'cash' if provider in ('opencode', 'openrouter') else provider
    before = dict(caps)
    for donor in slices:
        if donor == dilim or donor not in needs or needs[donor] is None:
            continue
        if type(needs[donor]) is not int or needs[donor] < 0:
            raise BudgetDenied('remaining role need unreadable')
        spent = sum(run_usage(con, rid, donor, providers=butce_saglayicilari(provider), bounded=True))
        floor = max(spent, needs[donor])
        delta = min(max(0, caps[donor] - floor), gereken - caps[dilim])
        caps[donor] -= delta
        caps[dilim] += delta
        if caps[dilim] >= gereken:
            break
    if caps[dilim] < gereken:
        return cap
    total = run_cap_micro(home, rid)
    if sum(caps.values()) != total:
        raise BudgetDenied('role allocation total mismatch')
    if preview:
        return caps[dilim]
    con.execute('insert into role_allocation values(?,?,?,?) on conflict(run_id,pool) do update set caps=excluded.caps',
                (rid, pool, total, json.dumps(caps)))
    con.execute('insert into role_transfer(run_id,pool,created,metadata) values(?,?,?,?)',
                (rid, pool, time.time(), json.dumps({'before': before, 'after': caps, 'slice': dilim,
                 'reason': 'verified-progress' if proof else 'reviewed-plan' if reviewed else 'owned-plan-review',
                 'evidence': proof or {'revision': view['revision']}})))
    return caps[dilim]


def kalibrasyon_makbuzu(receipt: Any, metadata: dict[str, Any]) -> bool:
    return (isinstance(receipt, str) and bool(receipt) and not receipt.startswith(('hayalet:', 'kurtarma:'))
            and not metadata.get('guvence') and not metadata.get('gozlem'))


def inceleme_fazi(task: str, role: str) -> str | None:
    """The phase comes from the engine's task/role identity, not from the user body."""
    for phase, pattern, expected in (
            ('plan-review', r'plan-review-[1-9][0-9]*-[a-f0-9]{8}', 'review'),
            ('review-author', r'review-author-[a-f0-9]{12}', 'impl'),
            ('review-final', r'review-final-[a-f0-9]{12}', 'kalite'),
            ('review-recheck', r'review-recheck-[a-f0-9]{12}', 'kalite')):
        if role == expected and re.fullmatch(pattern, task):
            return phase
    return None


def inceleme_kurali(record: dict[str, Any], phase: str) -> dict[str, Any]:
    from sinirlar import inceleme_politikasi
    policy = record.get('review_limits', inceleme_politikasi())
    try:
        rule = policy[phase]
        if (not isinstance(policy['surum'], str) or not policy['surum'] or len(policy['surum']) > 100
                or any(type(rule[k]) is not int or rule[k] <= 0 for k in ('istek', 'arac', 'sure_sn', 'pay_yuzde'))
                or rule['pay_yuzde'] > 100):
            raise ValueError('invalid review limits')
        return {'surum': policy['surum'], **rule}
    except (KeyError, TypeError, ValueError) as exc:
        raise BudgetDenied('review limits unavailable') from exc


class BudgetLedger:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    @staticmethod
    def schema(con: sqlite3.Connection) -> None:
        con.execute('''create table if not exists allocation(
            provider text, period text, ceiling integer not null, primary key(provider, period))''')
        con.execute('''create table if not exists reservation(
            id text primary key, provider text not null, period text not null,
            created real not null, reserved integer not null, actual integer,
            receipt text unique, metadata text not null)''')
        con.execute('create table if not exists pricing_fault(provider text primary key)')
        con.execute('create table if not exists role_allocation(run_id text, pool text, total integer not null, caps text not null, primary key(run_id,pool))')
        con.execute('create table if not exists role_transfer(id integer primary key, run_id text, pool text, created real, metadata text not null)')
        con.execute('''create table if not exists review_control(
            run_id text, phase text, started real not null, deadline real not null,
            calls integer not null, tools integer not null, policy text not null,
            primary key(run_id,phase))''')
        con.execute('''create table if not exists review_pause(
            run_id text, phase text, paused_at real, paused_total real not null,
            primary key(run_id,phase))''')

    def review_context(self, con: sqlite3.Connection, run_id: str | None, task: str, role: str,
                       *, create: bool = False) -> dict[str, Any] | None:
        phase = inceleme_fazi(task, role)
        if not run_id or not phase:
            return None
        home = self.path.parent.parent
        record = RunStore(home).read(run_id)
        rule = inceleme_kurali(record, phase)
        total = operator_ailesi(home, run_id)[1]
        local = run_cap_micro(home, run_id)
        if total is None or local is None:
            raise BudgetDenied('review total cap unavailable')
        limit = min(total, local) * rule['pay_yuzde'] // 100
        policy = json.dumps(rule, sort_keys=True, allow_nan=False)
        exists = con.execute("select 1 from sqlite_master where name='review_control'").fetchone()
        row = con.execute('select started,deadline,calls,tools,policy from review_control where run_id=? and phase=?',
                          (run_id, phase)).fetchone() if exists else None
        clock_exists = con.execute("select 1 from sqlite_master where name='review_pause'").fetchone()
        clock = con.execute('select paused_at,paused_total from review_pause where run_id=? and phase=?',
                            (run_id, phase)).fetchone() if clock_exists else None
        paused_at, paused_total = clock if clock else (None, 0)
        # A new task/attempt/model id in the same phase does not reset the counter or the clock.
        rows = []
        for provider, created, reserved, actual, raw in con.execute(
                "select provider,created,reserved,actual,metadata from reservation where json_extract(metadata,'$.run_id')=?", (run_id,)):
            meta = json.loads(raw)
            if not meta.get('guvence') and inceleme_fazi(str(meta.get('task_id') or ''), str(meta.get('phase_role') or '')) == phase:
                rows.append((provider, created, actual if actual is not None else max(reserved, meta.get('verified_bound_micro') or meta.get('en_kotu') or reserved)))
        now = time.time()
        started, deadline, calls, tools = row[:4] if row else (
            min([now, *[r[1] for r in rows]]), 0, len(rows), 0)
        if not row:
            deadline = started + rule['sure_sn']
        if (row and row[4] != policy or clock and not row
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in (started, deadline, paused_total))
                or paused_total < 0 or any(type(v) is not int or v < 0 for v in (calls, tools))
                or deadline != started + rule['sure_sn'] + paused_total or calls < len(rows) or started > now
                or paused_at is not None and (type(paused_at) not in (int, float) or not math.isfinite(paused_at)
                                              or not started <= paused_at <= now or paused_at >= deadline)):
            raise BudgetDenied('review control changed or unreadable')
        if paused_at is not None:
            paused_total += now - paused_at
            deadline = started + rule['sure_sn'] + paused_total
            if create:
                con.execute('update review_control set deadline=? where run_id=? and phase=?', (deadline, run_id, phase))
                con.execute('update review_pause set paused_at=null,paused_total=? where run_id=? and phase=?',
                            (paused_total, run_id, phase))
        if create and not row:
            con.execute('insert into review_control values(?,?,?,?,?,?,?)',
                        (run_id, phase, started, deadline, calls, tools, policy))
        from kosu import istek_kalan_sn, istek_kok_kimligi
        store = RunStore(home)
        root = store.read(istek_kok_kimligi(store, run_id))
        request_end = None
        if root.get('request') and (root.get('request_clock') or {}).get('baslangic') is not None:
            remaining = istek_kalan_sn(root, now)
            work = root['request']['duration_minutes'] * 60 - remaining
            if not math.isfinite(remaining) or not math.isfinite(work) or work < 0:
                raise BudgetDenied('review work clock unreadable')
            # N-RUN04: the phase's work time is shorter than or equal to both the wall time and the
            # total work accumulated at the root. Provider waits do not consume the cap;
            # if an old record has no phase start offset, the narrower safe share remains.
            elapsed = max(0, now - started - paused_total)
            credit = max(0, elapsed - work)
            if credit:
                paused_total += credit
                deadline = started + rule['sure_sn'] + paused_total
                if create:
                    con.execute('update review_control set deadline=? where run_id=? and phase=?', (deadline, run_id, phase))
                    con.execute('insert into review_pause values(?,?,null,?) on conflict(run_id,phase) '
                                'do update set paused_at=null,paused_total=excluded.paused_total',
                                (run_id, phase, paused_total))
            request_end = now + remaining
        if request_end is not None:
            deadline = min(deadline, request_end)
        if now >= deadline:
            raise BudgetDenied(f'review limit: {phase}: time')
        return {'run_id': run_id, 'phase': phase, 'deadline': deadline, 'calls': calls, 'tools': tools,
                'rule': rule, 'limit': limit, 'rows': rows}

    @staticmethod
    def review_allowance(context: dict[str, Any] | None, provider: str) -> int | None:
        if context is None:
            return None
        phase = context['phase']
        if context['calls'] >= context['rule']['istek']:
            raise BudgetDenied(f'review limit: {phase}: requests')
        # Cash and subscription units stay separate; all model/helper calls in the same phase are counted.
        used = sum(r[2] for r in context['rows'] if r[0] in butce_saglayicilari(provider))
        return max(0, context['limit'] - used) if provider in USD_PROVIDERS else None

    def begin_review(self, run_id: str | None, task: str, role: str) -> dict[str, Any] | None:
        if not run_id or not inceleme_fazi(task, role):
            return None
        with self.transaction() as con:
            context = self.review_context(con, run_id, task, role, create=True)
            self.review_allowance(context, '')
            return context

    def pause_review(self, run_id: str, task: str, role: str) -> None:
        """The engine stops the clock after the verified end of a single-owner resource call.

        During a verified provider wait the process area must also be closed.
        Cancel/general error/uncertain end does not use this path; all counters are kept.
        """
        phase = inceleme_fazi(task, role)
        if not phase:
            return
        with self.transaction() as con:
            if not con.execute('select 1 from review_control where run_id=? and phase=?', (run_id, phase)).fetchone():
                raise BudgetDenied('review control changed or unreadable')
            context = self.review_context(con, run_id, task, role)
            clock = con.execute('select paused_at,paused_total from review_pause where run_id=? and phase=?',
                                (run_id, phase)).fetchone()
            if clock and clock[0] is not None:
                return
            now = time.time()
            if now >= context['deadline']:
                raise BudgetDenied(f'review limit: {phase}: time')
            con.execute('insert into review_pause values(?,?,?,?) on conflict(run_id,phase) '
                        'do update set paused_at=excluded.paused_at', (run_id, phase, now, clock[1] if clock else 0))

    def review_tool(self, run_id: str | None, task: str, role: str) -> None:
        if not run_id or not inceleme_fazi(task, role):
            return
        with self.transaction() as con:
            context = self.review_context(con, run_id, task, role, create=True)
            if context['tools'] >= context['rule']['arac']:
                raise BudgetDenied(f"review limit: {context['phase']}: tools")
            con.execute('update review_control set tools=tools+1 where run_id=? and phase=?', (run_id, context['phase']))

    @staticmethod
    def capacity(con: sqlite3.Connection, provider: str, snapshot: Any, period: str,
                 remaining: int, model: str | None = None) -> tuple[int, int, int]:
        """B140: admission and reserve use the same cap, spending and pending totals."""
        if provider == 'openrouter':
            # Net credit includes the old receipt; post-observation and estimated obligations are kept in every period.
            settled, held = con.execute(
                "select coalesce(sum(actual),0),coalesce(sum(case when actual is null then reserved else 0 end),0) "
                "from reservation where provider=?", (provider,)).fetchone()
            return remaining + or_dahil_makbuz(con, snapshot), settled, held
        if provider == 'opencode-go':
            # 4c-13: the pool is points to dollars; each pending line converts to points with its own model's limit, the total to this model's dollars.
            # 4c QA2: lines opened and settled AFTER the observation are not in the observation's Σ; they are subtracted from the wall too.
            from go_pencere import puan, usd
            held = sum(puan(m or '', r) for r, m in con.execute(
                "select reserved, json_extract(metadata,'$.model') from reservation where provider=? and actual is null",
                (provider,)))
            try:
                gozlem_ani = datetime.fromisoformat(str(snapshot['ts'])).timestamp()
            except (KeyError, TypeError, ValueError):
                gozlem_ani = time.time()
            sonra = sum(puan(m or '', a) for a, m in con.execute(
                "select actual, json_extract(metadata,'$.model') from reservation where provider=? and actual is not null "
                "and coalesce(json_extract(metadata,'$.kesin_ts'), created) > ?", (provider, gozlem_ani)))
            return remaining, math.ceil(round(usd(model or '', sonra), 6)), math.ceil(round(usd(model or '', held), 6))
        allocation = con.execute('select ceiling from allocation where provider=? and period=?',
                                 (provider, period)).fetchone()
        settled = con.execute('select coalesce(sum(actual),0) from reservation where provider=? and period=?',
                              (provider, period)).fetchone()[0]
        counted = taban_saglayici(provider) in FREE or provider.endswith(BEDAVA_SONEK)
        held = con.execute(
            'select coalesce(sum(reserved),0) from reservation where provider=? and actual is null'
            + (' and period=?' if counted else ''),
            (provider, period) if counted else (provider,)).fetchone()[0]
        ceiling = remaining + settled
        measured = olculen_pay(snapshot, provider)
        if measured is not None:
            ceiling = min(ceiling, measured)
        elif allocation and counted:
            # Latch only in a COUNT bucket: raising the setting mid-day must not widen that day's allowance (B129 a).
            # The dollar bucket (Claude: weekly reserve curve) follows the observation both ways (B150: the latch was pinned to a falling cap).
            ceiling = min(ceiling, allocation[0])
        return ceiling, settled, held

    @staticmethod
    def guvence_payi(con, provider, run_id, rol=None, model=None):
        if not run_id or rol not in ('kalite', 'qa', 'plan'):   # 4b: the plan reserve is also handed to the real plan call
            return []
        rows = con.execute("select id,reserved,metadata from reservation where provider=? and actual is null "
                           "and json_extract(metadata,'$.run_id')=? and json_extract(metadata,'$.guvence')=1 "
                           "and json_extract(metadata,'$.guvence_rol')=? order by created,id",
                           (provider, run_id, rol)).fetchall()
        return [r for r in rows if model is None or json.loads(r[2]).get('model') == model]

    def guvence_birak(self, run_id, rol=None, idler=None):
        with self.transaction() as con:
            for ident in [None] if idler is None else idler:
                con.execute("update reservation set actual=0,receipt='unused:'||id where actual is null "
                            "and json_extract(metadata,'$.run_id')=? and json_extract(metadata,'$.guvence')=1 "
                            "and (? is null or json_extract(metadata,'$.guvence_rol')=?) and (? is null or id=?)",
                            (run_id, rol, rol, ident, ident))
        kosu_harcama_yaz(self.path.parents[1], self, run_id)   # if the record does not see the refund, the status screen shows phantom pending money

    def guvence_tasi(self, run_id: str, rol: str, provider: str, maximum: int, snapshot: Any,
                    metadata: dict[str, Any], run_cap: int | None = None, dilim: str | None = None,
                    once_birak: bool = False) -> dict[str, list[str]] | None:
        if (metadata.get('run_id') != run_id or metadata.get('guvence') is not True
                or metadata.get('guvence_rol') != rol or rol not in ('kalite', 'qa', 'plan')
                or not metadata.get('model') or type(maximum) is not int or not 0 < maximum < 2**53):
            raise BudgetDenied('invalid guarantee transfer')
        with self.transaction() as con:
            rows = con.execute(
                "select id,provider,reserved,metadata from reservation where actual is null "
                "and json_extract(metadata,'$.run_id')=? and json_extract(metadata,'$.guvence')=1 "
                "and json_extract(metadata,'$.guvence_rol')=? order by created,id", (run_id, rol)).fetchall()
            eski = [row[0] for row in rows]
            if not eski:
                return None
            if all(row[1] == provider and row[2] == maximum and json.loads(row[3]).get('model') == metadata['model']
                   and (dilim is None or json.loads(row[3]).get('dilim') == dilim) for row in rows):
                result = {'eski': eski, 'yeni': eski}
            else:
                if not once_birak:
                    con.execute('SAVEPOINT guarantee_transfer')
                for ident in eski:
                    con.execute("update reservation set actual=0,receipt='unused:'||id where id=?", (ident,))
                if once_birak:
                    con.execute('SAVEPOINT guarantee_transfer')
                yeni: list[str] = []
                try:
                    for _ in eski:
                        yeni.append(self.reserve(provider, maximum, snapshot, {**metadata, 'guvence_onceki': eski},
                                                 run_cap, dilim, _connection=con))
                except BudgetDenied:
                    con.execute('ROLLBACK TO guarantee_transfer')
                    result = {'eski': eski, 'yeni': []} if once_birak else None
                else:
                    for ident in eski:
                        con.execute("update reservation set metadata=json_set(metadata,'$.guvence_sonraki',json(?)) where id=?",
                                    (json.dumps(yeni), ident))
                    result = {'eski': eski, 'yeni': yeni}
                con.execute('RELEASE guarantee_transfer')
        if result is not None:
            kosu_harcama_yaz(self.path.parents[1], self, run_id)
        return result

    def available(self, provider: str, snapshot: Any, run_cap: int | None = None, run_id: str | None = None,
                  dilim: str | None = None, gerekli: int = 1, rol: str | None = None, model: str | None = None,
                  salt_okur: bool = False) -> int:
        """Admission hint; the atomic decision is in reserve(). Holds the share of a pending call, not used to cut that call.
        `run_cap` (micro-dollars) is a role's pocket; spending is counted from that pocket with `dilim`."""
        period, remaining = allowance(snapshot, provider, model=model)
        if not self.path.exists():
            measured = olculen_pay(snapshot, provider)
            if measured is not None:
                remaining = min(remaining, measured)
            if run_cap is not None and provider in USD_PROVIDERS:
                root_cap = operator_ailesi(self.path.parent.parent, run_id)[1] if run_id and (self.path.parent.parent / 'runs' / run_id / 'run.json').exists() else None
                if root_cap is not None:
                    with closing(sqlite3.connect(':memory:')) as con:
                        self.schema(con)
                        run_cap, root_cap = dinamik_tavanlar(self.path.parent.parent, con, run_id, run_cap,
                                                             dilim or 'seritler', gerekli, remaining >= gerekli, provider, preview=True)
                        review = self.review_context(con, run_id, os.environ.get('CADRE_GOREV_ID', ''), rol or os.environ.get('CADRE_ROL', ''))
                        review_left = self.review_allowance(review, provider)
                        if review_left is not None:
                            remaining = min(remaining, review_left)
                return max(0, min(remaining, run_cap, root_cap if root_cap is not None else run_cap))
            return remaining
        try:
            connection = closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)) if salt_okur else self.transaction()
            with connection as con:
                if con.execute('select 1 from pricing_fault where provider=?', (provider,)).fetchone():
                    return 0
                review = self.review_context(con, run_id, os.environ.get('CADRE_GOREV_ID', ''), rol or os.environ.get('CADRE_ROL', ''))
                review_left = self.review_allowance(review, provider)
                ceiling, settled, held = self.capacity(con, provider, snapshot, period, remaining, model)
                slots = self.guvence_payi(con, provider, run_id, rol)
                if provider == 'opencode-go':   # the guarantee may have been held with another Go model's dollars: converted via points
                    from go_pencere import puan, usd
                    credit = int(round(usd(model or '', sum(puan(json.loads(r[2]).get('model') or '', r[1]) for r in slots)), 6))
                else:
                    credit = sum(r[1] for r in slots)
                value = max(0, ceiling - settled - held + credit)
                if review_left is not None:
                    value = min(value, review_left)
                project_remaining = None
                if run_id and provider in USD_PROVIDERS:
                    run_cap, project_remaining = dinamik_tavanlar(self.path.parent.parent, con, run_id, run_cap,
                                                                  dilim or 'seritler', max(0, gerekli - credit), value >= gerekli, provider,
                                                                  preview=salt_okur)
                if run_cap is not None and run_id and provider in USD_PROVIDERS:
                    run_settled, run_held = run_usage(con, run_id, dilim or 'seritler', plan_guvencesi_haric=True,
                                                     providers=butce_saglayicilari(provider), bounded=True)
                    run_credit = 0 if slots and rol == 'plan' else credit
                    value = max(0, min(value, run_cap - run_settled - run_held + run_credit))
                if project_remaining is not None:
                    value = min(value, project_remaining + credit)
                return value
        except (OSError, sqlite3.Error) as exc:
            raise BudgetDenied('reservation accounting unavailable') from exc

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=15)
        try:
            con.execute('PRAGMA synchronous=FULL')
            con.execute('BEGIN IMMEDIATE')
            self.schema(con)
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    KALIBRE_GUN, KALIBRE_EN_AZ, KALIBRE_TABAN = 7, 30, 0.1
    KALIBRE_EN_AZ_SAGLAYICI = {'claude': 10}   # user approval: Claude could not reach 30 receipts (an allocation that does not fit = receipts that never accumulate)

    def kalibre(self, con: sqlite3.Connection, provider: str, maximum: int) -> int:
        """B198: the allocation is 2x the p95 of the provider's real/worst-case ratio over the last 7 days (at least 30 receipts,
        otherwise the worst case). The ratio is taken against the worst case (so feedback does not push it to 1); an overrun shows up in `settle` and stops the next job."""
        if maximum <= 1 or provider.endswith(':free'):
            return maximum
        oranlar = []
        now = time.time()
        for reserved, actual, receipt, meta in con.execute(
                'select reserved, actual, receipt, metadata from reservation where provider=? and created>? and created<=? and actual>=0 and reserved>0',
                (provider, now - self.KALIBRE_GUN * 86400, now)):
            try:
                metadata = json.loads(meta or '{}') or {}
                if not kalibrasyon_makbuzu(receipt, metadata):
                    continue
                en_kotu = int(metadata.get('verified_bound_micro') or metadata.get('en_kotu') or reserved)
            except (ValueError, TypeError, AttributeError):
                continue
            if en_kotu > 0:
                oranlar.append(actual / en_kotu)
        if len(oranlar) < self.KALIBRE_EN_AZ_SAGLAYICI.get(provider, self.KALIBRE_EN_AZ):
            return maximum
        oranlar.sort()
        p95 = oranlar[min(len(oranlar) - 1, int(0.95 * len(oranlar)))]
        return max(1, min(maximum, math.ceil(maximum * max(self.KALIBRE_TABAN, 2 * p95))))

    def reserve(self, provider: str, maximum: int, snapshot: Any, metadata: dict[str, Any],
                run_cap: int | None = None, dilim: str | None = None, gozlem: bool = False,
                *, _connection: sqlite3.Connection | None = None) -> str:
        """`gozlem=True` (B124): OBSERVATION MODE — spending is written to the ledger but no cap is APPLIED.
        For manual `opencode` use: when the user works from their own shell the gate does NOT BLOCK them,
        it only observes. Automated runs never use this mode; there the cap is binding."""
        # if `dilim` is given it is stamped into the metadata HERE, so that the applied cap and the counted pocket come from the same
        # decision (see rol_butcesi). If not given, the old behaviour: metadata or "seritler".
        if dilim:
            metadata = {**metadata, "dilim": dilim}
        if gozlem:
            metadata = {**metadata, "gozlem": True}
        period, remaining = allowance(snapshot, provider, model=metadata.get('model'))
        # Aliases share an account; they cannot each spend a separate allowance.
        if type(maximum) is not int or not 0 < maximum < 2**53:
            raise BudgetDenied('invalid reservation')
        ident = uuid.uuid4().hex
        denied = False
        run_denied = ''
        alarm_kaydi = None
        with self.transaction() if _connection is None else nullcontext(_connection) as con:
            if con.execute('select 1 from pricing_fault where provider=?', (provider,)).fetchone():
                raise BudgetDenied('observed usage exceeded its verified bound')
            en_kotu = maximum
            review = None if metadata.get('guvence') else self.review_context(
                con, metadata.get('run_id'), str(metadata.get('task_id') or ''), str(metadata.get('phase_role') or ''), create=True)
            review_left = self.review_allowance(review, provider)
            if review_left is not None and en_kotu > review_left:
                raise BudgetDenied(f"review limit: {review['phase']}: cost")
            metadata = {k: v for k, v in metadata.items() if k != 'review_phase'}
            if review:
                metadata['review_phase'] = review['phase']
            metadata = {**metadata, 'verified_bound_micro': en_kotu}
            if not gozlem and not metadata.get('guvence'):
                maximum = self.kalibre(con, provider, maximum)
                if maximum < en_kotu:
                    metadata = {**metadata, "en_kotu": en_kotu}
            ceiling, settled, held = self.capacity(con, provider, snapshot, period, remaining, metadata.get('model'))
            guvence_rol = metadata.get('phase_role') or metadata.get('role')
            slots = [] if metadata.get('guvence') else self.guvence_payi(
                con, provider, metadata.get('run_id'), guvence_rol, metadata.get('model'))
            slot = slots[0] if slots else None
            credit = slot[1] if slot else 0
            project_remaining = None
            if not gozlem and provider in USD_PROVIDERS and metadata.get('run_id'):
                run_cap, project_remaining = dinamik_tavanlar(self.path.parent.parent, con, str(metadata['run_id']),
                    run_cap, str(metadata.get('dilim') or 'seritler'), max(0, en_kotu - credit), settled + held + maximum - credit <= ceiling, provider)
            # P5: run-wide cap — this run's settled + pending total across USD providers cannot exceed the cap.
            if run_cap is not None and not gozlem and provider in USD_PROVIDERS and metadata.get('run_id'):
                run_settled, run_held = run_usage(con, str(metadata['run_id']), str(metadata.get('dilim') or 'seritler'),
                                                  plan_guvencesi_haric=True, providers=butce_saglayicilari(provider), bounded=True)
                run_credit = 0 if slot and guvence_rol == 'plan' else credit   # the plan reserve was not counted to the pocket; the call is written in full to the pocket
                if run_settled + run_held + en_kotu - run_credit > run_cap:  # B49: a reason with the amount — removes the "cap with no spending at all" misreading
                    run_denied = (f"run budget exhausted: reservation ${(en_kotu - run_credit) / 1e6:.2f} > remaining "
                                  f"${max(0, run_cap - run_settled - run_held) / 1e6:.2f} (cap ${run_cap / 1e6:.2f}, settled ${run_settled / 1e6:.2f}, held ${run_held / 1e6:.2f}"
                                  + (f", calibrated ${maximum / 1e6:.2f})" if maximum < en_kotu else ')'))
            if not run_denied and project_remaining is not None and en_kotu - credit > project_remaining:
                run_denied = 'run budget exhausted: project total cap'
            if (not run_denied and not gozlem and provider in USD_PROVIDERS and metadata.get('run_id') and not metadata.get('guvence')
                    and os.environ.get('CADRE_HARCAMA_ALARMI', '1') != '0'):
                run_denied, alarm_kaydi = harcama_alarmi(self.path.parent.parent, con, str(metadata['run_id']))
                run_denied = run_denied or ''
            con.execute("""insert into allocation values(?,?,?) on conflict(provider,period)
                do update set ceiling=excluded.ceiling""", (provider, period, ceiling))
            # 07.10 16(b): OR does its own pre-check with the worst-case amount (max_tokens x price; 402 if insufficient). A request that passed with a small
            # calibrated reservation fell there and stayed pending: on OR admission is measured with the worst case, the allocated amount stays calibrated.
            gereken = en_kotu if provider == 'openrouter' else maximum
            if settled + held + gereken - credit > ceiling and not gozlem:
                denied = True
            elif not run_denied or gozlem:
                if review:
                    con.execute('update review_control set calls=calls+1 where run_id=? and phase=?',
                                (metadata['run_id'], review['phase']))
                if slot:
                    con.execute("update reservation set actual=0,receipt='claimed:'||id where id=?", (slot[0],))
                    metadata = {**metadata, 'guvence_kaynagi': slot[0]}
                con.execute('insert into reservation values(?,?,?,?,?,null,null,?)',
                            (ident, provider, period, time.time(), maximum, json.dumps(metadata, allow_nan=False)))
        if alarm_kaydi:   # the record is written outside the transaction: the record lock must not wait for the ledger lock
            alan = 'harcama_alarmi' if alarm_kaydi.get('kind') == 'no-progress-spend' else 'harcama_uyarisi'
            RunStore(self.path.parent.parent).update(str(metadata['run_id']), **{alan: alarm_kaydi})
        if denied:
            raise BudgetDenied('allocation exhausted or outcome unknown')
        if run_denied and not gozlem:
            raise BudgetDenied(run_denied)
        return ident

    def hata_kaydet(self, ident: str, hata: dict[str, Any]) -> dict[str, Any] | None:
        with self.transaction() as con:
            row = con.execute("select provider, metadata from reservation where id=? and actual is null and json_valid(metadata)",
                              (ident,)).fetchone()
            if row is None:
                return None
            con.execute("update reservation set metadata=json_set(metadata,'$.saglayici_hatasi',json(?)) where id=? and actual is null",
                        (json.dumps(hata), ident))
            metadata = json.loads(row[1] or '{}') or {}
            uclar = (metadata.get('uc_secimi') or {}).get('sira')
            uc = uclar[0] if isinstance(uclar, list) and len(uclar) == 1 and isinstance(uclar[0], str) and uclar[0] else None
            return {'provider': row[0], 'model': str(metadata.get('model') or ''), 'uc': uc}

    def uretim_kaydet(self, ident: str, generation: str) -> None:
        if not re.fullmatch(r'gen-[A-Za-z0-9_-]{1,128}', generation):
            return
        with self.transaction() as con:
            con.execute("update reservation set metadata=json_set(metadata,'$.generation',?) where id=? and actual is null "
                        "and json_valid(metadata) and json_extract(metadata,'$.generation') is null", (generation, ident))

    def settle(self, ident: str, actual: int, receipt: str, kullanim: Any = None) -> None:
        if type(actual) is not int or not 0 <= actual < 2**53 or not receipt:
            raise BudgetDenied('invalid receipt')
        kullanim = kullanim_temizle(kullanim)
        with self.transaction() as con:
            row = con.execute('select reserved,actual,receipt,provider,metadata from reservation where id=?', (ident,)).fetchone()
            if row is None or (row[1] is not None and (row[1], row[2]) != (actual, receipt)):
                raise BudgetDenied('conflicting receipt')
            try:
                meta = json.loads(row[4] or '{}') or {}
                sinir = int(meta.get('verified_bound_micro') or meta.get('en_kotu') or row[0])
            except (ValueError, TypeError, AttributeError):
                sinir = row[0]
            if actual > row[0]:
                # Never conceal an overrun. Its actual cost blocks subsequent work.
                con.execute('update reservation set reserved=? where id=?', (actual, ident))
                if actual > sinir:   # B198: only exceeding the worst-case limit means the price model is wrong; exceeding the calibrated allocation is the expected tail
                    con.execute('insert or ignore into pricing_fault values(?)', (row[3],))
            con.execute("update reservation set actual=?,receipt=?,metadata=case when json_valid(metadata) then "
                        "json_set(metadata,'$.verified_bound_micro',?,'$.kesin_ts',coalesce(json_extract(metadata,'$.kesin_ts'),?)) else metadata end where id=?",
                        (actual, receipt, sinir, time.time(), ident))
            if kullanim:
                con.execute("update reservation set metadata=json_set(metadata,'$.kullanim',json(?)) where id=? "
                            "and json_valid(metadata) and json_extract(metadata,'$.kullanim') is null",
                            (json.dumps(kullanim, allow_nan=False), ident))


KULLANIM_SAYILARI = ('girdi', 'cikti', 'dusunme', 'onbellek_okuma', 'onbellek_yazma', 'sure_ms')
KULLANIM_USD = ('ucret_usd', 'onbellek_indirimi_usd', 'ust_ucret_usd')
KULLANIM_METIN = {'uc': 64, 'uc_etiket': 64, 'model_yanit': 128, 'bitis': 32, 'bicim': 16, 'fiyat_kaynagi': 32}
KULLANIM_DESENI = re.compile(r'[A-Za-z0-9 ._:/()+@-]+')


def kullanim_temizle(value: Any) -> dict[str, Any] | None:
    """N-TS05 (CR-52): secret-free usage metadata of the receipt; unknown/invalid fields are dropped, missing fields are not written as zero."""
    if not isinstance(value, dict):
        return None
    temiz: dict[str, Any] = {}
    for key in KULLANIM_SAYILARI:
        item = value.get(key)
        if type(item) is int and 0 <= item < 2**53:
            temiz[key] = item
    for key in (*KULLANIM_USD, 'fiyat_ts'):
        item = value.get(key)
        if type(item) in (int, float) and math.isfinite(item) and (item > 0 if key == 'fiyat_ts' else item >= 0) and item < 1e12:
            temiz[key] = item
    if type(value.get('byok')) is bool:
        temiz['byok'] = value['byok']
    for key, limit in KULLANIM_METIN.items():
        item = value.get(key)
        if isinstance(item, str) and 0 < len(item) <= limit and KULLANIM_DESENI.fullmatch(item):
            temiz[key] = item
    return temiz or None


def yenilenme_payi(snapshot: Any, provider: str) -> int | None:
    """The measured share of the period (dollar bucket: share with margin; Claude and count buckets: the allowance given by the observation). It grows only
    on renewal — spending and pending reservations do not grow this number."""
    olcu = olculen_pay(snapshot, provider)
    if olcu is not None:
        return olcu
    try:
        return allowance(snapshot, provider)[1]
    except BudgetDenied:
        return None


YENILENME_ORAN, YENILENME_TABAN = 0.10, 50_000   # crumbs from a delayed API read do not count as a renewal


def yenilendi_mi(row: dict[str, Any], snapshot: Any, provider: str) -> bool:
    eski = row.get('pay')
    if not isinstance(eski, int) or isinstance(eski, bool):
        return False
    return (yenilenme_payi(snapshot, provider) or 0) > eski + max(YENILENME_TABAN, int(eski * YENILENME_ORAN))


class ProviderBans:
    """Task-owned bans: budget renewal and provider cooldown are separate; the task lease is the writer lock.
    Routing only reads; a reservation with unknown outcome also binds the renewed quota."""
    def __init__(self, path: Path, home: Path):
        self.path = path
        self.ledger = BudgetLedger(home/'zincir/request-budget.db')

    def read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict) or any(not isinstance(row, dict) for row in data.values()):
                raise ValueError('invalid bans')
            return data
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise BudgetDenied('provider exclusions unreadable') from exc

    def blocked(self, dilim: str | None = None) -> set[str]:
        """B110: run-cap BANS carried no pocket information; a ban written when the lanes' pocket ran out
        also closed the JUDGE — the exact opposite of the hard split (each role only its own pocket). A pocket-stamped ban binds only
        that pocket; a ban without a pocket (provider error / daily share) binds everyone."""
        veri = self.read()
        if dilim is None:
            return set(veri)
        return {ad for ad, satir in veri.items()
                if not satir.get('dilim') or satir.get('dilim') == dilim}

    def block(self, provider: str, reason: str, snapshot: Any, retry_after: float = 60,
              dilim: str | None = None) -> None:
        data = self.read()
        previous = data.get(provider)
        if previous and previous.get('reason') == 'budget':
            return  # A new error must not shorten an exhausted period's ban.
        try:
            period, _ = allowance(snapshot, provider)
        except BudgetDenied:
            period = datetime.fromtimestamp(time.time()).date().isoformat()
            if reason == 'budget':
                reason = 'unavailable'
        data[provider] = {'reason':reason, 'period':period,
                          'recheck_at':time.time() + retry_after,
                          **({'dilim': dilim} if dilim else {}),
                          **({'pay': pay} if reason == 'budget' and (pay := yenilenme_payi(snapshot, provider)) is not None else {})}
        atomic_json(self.path, data)

    def refresh(self, snapshot: Any) -> None:
        data = self.read()
        updated = dict(data)
        for provider, row in data.items():
            try:
                period, _ = allowance(snapshot, provider)
                # B286/G-B3: within the same period it is re-checked only if the share is RENEWED; receipt/reservation jitter does not reopen it (C14).
                if row.get('reason') == 'budget' and row.get('period') == period and not yenilendi_mi(row, snapshot, provider):
                    # a pocket-stamped (dilim) cap ban must also lift when the run cap rises; after recheck_at
                    # the provider share is checked again, the pocket is re-measured at the gate (the ban is refreshed if empty). The provider-share ban is the old rule.
                    if not (row.get('dilim') and time.time() >= amount(row.get('recheck_at', 0))):
                        continue
                if row.get('reason') != 'budget' and time.time() < amount(row['recheck_at']):
                    continue
                if self.ledger.available(provider, snapshot) > 0:
                    updated.pop(provider)
            except (BudgetDenied, KeyError):
                continue
        if updated != data:
            atomic_json(self.path, updated)


def task_bans(home: Path) -> ProviderBans | None:
    run = os.environ.get('CADRE_RUN_ID')
    if not run:
        return None
    path = RunStore(home).task_dir(run, os.environ.get('CADRE_GOREV_ID') or 'main')/'budget-bans.json'
    return ProviderBans(path, home)


def available_budget(home: Path, snapshot: Any, provider: str,
                     rol: str | None = None, run_id: str | None = None, gerekli: int = 1, model: str | None = None,
                     salt_okur: bool = False) -> int:
    """B81: `rol`/`run_id` may be given EXPLICITLY. The engine's own os.environ has no CADRE_ROL (the role is only
    written into the child environment), so when the caller did not know, everyone saw the LANE pocket."""
    bans = task_bans(home)
    run_id = run_id or os.environ.get('CADRE_RUN_ID')
    tavan, dilim = rol_butcesi(home, run_id, rol, provider)
    # B110: the ban list is read with the ASKING ROLE's pocket — another pocket running out does not close this role.
    if bans and taban_saglayici(provider) in bans.blocked(dilim):
        row = bans.read().get(taban_saglayici(provider), {})
        r = RunStore(home).read(run_id) if run_id else {}
        if not (row.get('dilim') and not r.get('budget_manual') and 'budget_plan_cap_usd' in r and tavan_ilerlemesi(home, r)):
            return 0
    return BudgetLedger(home/'zincir/request-budget.db').available(provider, snapshot, tavan, run_id, dilim, gerekli,
                                                                   rol or os.environ.get('CADRE_ROL'), model, salt_okur)


def run_remaining_usd(home: Path, run_id: str | None, rol: str | None = None, provider: str = '') -> float | None:
    """Remaining USD share of this ROLE (pocket − settled − pending); None without a cap.
    The engine's view of 'is there money' and the limit the gate applies must come from the same pocket."""
    cap, dilim_adi = rol_butcesi(home, run_id, rol, provider)
    if cap is None or not run_id:
        return None
    ledger = BudgetLedger(home/'zincir/request-budget.db')
    settled = held = 0
    if ledger.path.exists():
        try:
            with closing(sqlite3.connect(ledger.path.resolve().as_uri()+'?mode=ro', uri=True, timeout=15)) as con:
                settled, held = run_usage(con, run_id, dilim_adi, plan_guvencesi_haric=True,
                                           providers=butce_saglayicilari(provider), bounded=True)
                etkin = rol or os.environ.get('CADRE_ROL')
                if etkin in ('kalite', 'qa'):
                    providers = butce_saglayicilari(provider)
                    marks = ','.join('?' for _ in providers)
                    credit = con.execute("select coalesce(sum(reserved),0) from reservation where actual is null "
                        "and json_extract(metadata,'$.run_id')=? and json_extract(metadata,'$.guvence')=1 "
                        f"and json_extract(metadata,'$.guvence_rol')=? and provider in ({marks})", (run_id, etkin, *providers)).fetchone()[0]
                    held -= credit
                else:
                    credit = 0
                if provider:
                    cap, remaining = dinamik_tavanlar(home, con, run_id, cap, dilim_adi, 0, False, provider)
                    if remaining is not None:
                        return max(0.0, min(cap - settled - held, remaining + credit) / 1e6)
        except (OSError, sqlite3.Error):
            return 0.0
    return max(0.0, (cap - settled - held) / 1_000_000)


def observed_budget(home: Path, code: Path) -> Any:
    """One cached observation per minute, shared by all executors and helpers."""
    target = home/'zincir/request-budget.json'
    with FileLock(target.with_suffix('.lock')):
        try:
            saved = json.loads(target.read_text())
            age = time.time() - float(saved['observed'])
            # B327: after midnight yesterday's observation counts as 'stale budget'; if the day changed, measure again
            if 0 <= age < 60 and (saved['budget'] or {}).get('bugun', datetime.now().strftime('%Y-%m-%d')) == datetime.now().strftime('%Y-%m-%d'):
                return saved['budget']
        except (OSError, ValueError, KeyError, TypeError):
            pass
        observed = time.time()  # Observation must not postdate its source query.
        try:
            result = subprocess.run([sys.executable, str(code/'lib/butce.py')], capture_output=True, text=True, timeout=30, check=True)
            budget = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            budget = None
        atomic_json(target, {'observed':observed, 'budget':budget})
        return budget


# ---- Kurtarma yolu (Madde 0a) --------------------------------------------
# Closing does NOT silently give money back: by default the reserved amount counts as SPENT; giving money back is a separate and
# deliberate decision (--sifir). Both actions require --evet and are written to kurtarma.log.

def _kurtarma_log(home: Path, kayit: dict[str, Any]) -> None:
    yol = home / 'zincir/kurtarma.log'
    yol.parent.mkdir(parents=True, exist_ok=True)
    with yol.open('a', encoding='utf-8') as f:
        f.write(json.dumps({'ts': time.time(), **kayit}, ensure_ascii=False) + '\n')


def bayat_rezervasyonlar(con: sqlite3.Connection, yas_saat: float) -> list[tuple]:
    esik = time.time() - yas_saat * 3600
    return con.execute('select id,provider,created,reserved,metadata from reservation '
                       "where actual is null and coalesce(json_extract(metadata,'$.guvence'),0)!=1 and created < ? order by created", (esik,)).fetchall()


def sahiplik_durumu(home: Path, run_id: str | None) -> str:
    if not run_id:
        return 'legacy'
    from kosu import active_runs, process_identity, surec_komutu
    store = RunStore(home)
    try:
        if not (store.path(run_id) / 'run.json').exists():
            return 'legacy'
        record = store.read(run_id)
        locked = run_id in active_runs(store)
        if record.get('state') in ('completed', 'failed', 'cancelled'):
            return 'unknown' if locked else 'dead'
        pid = record.get('owner_pid')
        if type(pid) is not int or pid <= 0:
            return 'unknown'
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return 'unknown' if locked else 'dead'
        except PermissionError:
            return 'unknown'
        current = process_identity(pid)
        if record.get('owner_identity') and current:
            if current != record['owner_identity']:
                return 'unknown' if locked else 'dead'
        command = surec_komutu(pid)
        if not command:
            return 'unknown'
        import re
        match = re.search(r'(?:^|[/\s])kosu\.py\s+(run|execute)\s+(\S+)(?:\s|$)', command)
        owned = bool(record.get('owner_identity')) and current == record.get('owner_identity')
        if match and ((match.group(1) == 'run' and match.group(2) == run_id)
                      or (match.group(1) == 'execute' and owned and match.group(2) == record.get('project'))):
            return 'live' if locked and current else 'unknown'
        if match or 'kosu.py' not in command:
            return 'unknown' if locked else 'dead'
        return 'unknown'
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return 'unknown'


def sahip_olu_mu(home: Path, run_id: str | None) -> bool:
    return sahiplik_durumu(home, run_id) == 'dead'


def sahip_canli_mi(home: Path, run_id: str | None) -> bool:
    return sahiplik_durumu(home, run_id) in ('live', 'unknown')


@contextmanager
def kurtarma_kilidi(home: Path, run_id: str | None) -> Iterator[bool]:
    store = RunStore(home)
    if not run_id or not (store.path(run_id) / 'run.json').exists():
        yield True
        return
    before = store.read(run_id)
    directory = store.path(run_id)
    paths = [directory / '.owner.lock', *sorted((directory / 'tasks').glob('*/.*.lock'))]
    with ExitStack() as stack:
        for path in paths:
            lock = FileLock(path)
            if path.parent.is_symlink() or not lock.acquire(blocking=False):
                yield False
                return
            stack.callback(lock.close)
        after = store.read(run_id)
        if any(before.get(k) != after.get(k) for k in ('owner_pid', 'owner_identity', 'state')):
            yield False
            return
        yield True


def _sayi_ortam(ad: str, varsayilan: float) -> float:
    """B114-10: a module-level `float(os.environ[...])` blew up the IMPORT on a bad value —
    a shell that set `CADRE_HAYALET_YAS_DK=30dk` stopped the whole engine with 'could not convert string to float',
    and the message did not even name the variable. Now a bad value falls back to the default."""
    try:
        return float(os.environ.get(ad, varsayilan))
    except (TypeError, ValueError):
        return float(varsayilan)


HAYALET_YAS_DK = _sayi_ortam('CADRE_HAYALET_YAS_DK', 30)


def opencode_makbuzu(home: Path, con: sqlite3.Connection, ident: str, provider: str,
                    created: float, meta: dict[str, Any]) -> tuple[int, str] | None:
    """G-B2/P4: only the receipt of a single, completed session recorded for this run is used."""
    if provider not in PAID or not meta.get('session_id') or not meta.get('model'):
        return None
    try:
        from oturum import session_ids
        if meta['session_id'] not in session_ids(RunStore(home).read(meta['run_id'])):
            return None
        sonraki = con.execute("""select min(created) from reservation where id != ? and created >= ?
            and provider=? and json_extract(metadata,'$.session_id')=?
            and json_extract(metadata,'$.model')=?""", (ident, created, provider, meta['session_id'], meta['model'])).fetchone()[0]
        son = min(created + 1800, sonraki if sonraki is not None else created + 1800)
        db = Path(os.environ.get('OPENCODE_DB', str(Path.home() / '.local/share/opencode/opencode.db'))).expanduser()
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as oc:
            rows = oc.execute("""select id,data from message where session_id=? and time_created >= ? and time_created < ?
                and json_extract(data,'$.role')='assistant' and json_extract(data,'$.providerID')=?
                and json_extract(data,'$.modelID')=? and json_extract(data,'$.time.completed') >= ?
                and json_extract(data,'$.time.completed') < ?""",
                (meta['session_id'], (created - 5) * 1000, son * 1000, provider, meta['model'], created * 1000, son * 1000)).fetchall()
        if len(rows) != 1:
            return None
        mid, raw = rows[0]
        if con.execute('select 1 from reservation where receipt=? or receipt like ?',
                       ('opencode:' + mid, '%:' + mid)).fetchone():
            return None
        message = json.loads(raw)
        if message.get('error'):
            return None
        t = message['tokens']
        cache = t.get('cache') or {}
        counts = [t['input'], t['output'], t.get('reasoning', 0), cache.get('read', 0), cache.get('write', 0)]
        if not all(type(n) is int and 0 <= n < 2**53 for n in counts) or not any(counts):
            return None
        p = meta.get('pricing')
        if not p:
            from katalog import model
            f = (model(provider, meta['model']) or {})['cost']
            tiers = [f] + ([f['context_over_200k']] if f.get('context_over_200k') else [])
            p = {'freshRate': max(amount(c['input']) for c in tiers), 'outRate': max(amount(c['output']) for c in tiers),
                 'readRate': max(amount((c.get('cache') or {}).get('read', c.get('cache_read', c['input']))) for c in tiers),
                 'writeRate': max(amount((c.get('cache') or {}).get('write', c.get('cache_write')) or c['input']) for c in tiers)}
        p = {**p, 'writeRate': p.get('writeRate') or p['freshRate']}
        rates = [amount(p[k]) for k in ('freshRate', 'outRate', 'readRate', 'writeRate')]
        if rates[0] <= 0 or rates[1] <= 0:
            return None
        from go_pencere import zaman_carpani
        multiplier = zaman_carpani(meta['model'], created)   # Go DeepSeek yogun saatte 2× puan (4c-13)
        factor = amount(meta.get('factor', 1))   # 4c-13: the coefficient was removed; old Go lines carry their own coefficient
        if factor < 1:
            return None
        cost = counts[0] * rates[0] + (counts[1] + counts[2]) * rates[1] + counts[3] * rates[2] + counts[4] * rates[3]
        return math.ceil(cost * multiplier * factor), 'opencode:' + mid
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, AttributeError, OverflowError, BudgetDenied):
        return None


def hayalet_kapat(home: Path, yas_dk: float | None = None) -> dict[str, Any]:
    """B85: pending reservations whose owner died and that are older than `yas_dk` close automatically (counted as spent, reversible).
    The pending amount is a safe upper bound; a single crash locked half the cap with unspent money."""
    yas = HAYALET_YAS_DK if yas_dk is None else float(yas_dk)
    ledger = BudgetLedger(home / 'zincir/request-budget.db')
    if not ledger.path.exists():
        return {'kapandi': 0, 'tutar_micro': 0, 'kosular': []}
    with ledger.transaction() as con:
        satirlar = bayat_rezervasyonlar(con, yas / 60.0)
    kapandi = 0
    tutar = 0
    aylik = aylik_kurtarma_tamamla(home)
    kosular: set[str] = set()
    belirsiz: set[str] = set()
    for ident, _saglayici, _olusma, rezerve, meta in satirlar:
        m = json.loads(meta) if meta else {}
        run_id = m.get('run_id')
        if not sahip_olu_mu(home, run_id):
            if sahiplik_durumu(home, run_id) == 'unknown':
                belirsiz.add(str(run_id))
            continue
        try:
            # The same message must not be used by two concurrent recoveries (G-B2).
            with kurtarma_kilidi(home, run_id) as safe, FileLock(home / 'zincir/hayalet-makbuz.lock'):
                if not safe:
                    belirsiz.add(str(run_id))
                    continue
                with ledger.transaction() as con:
                    makbuz = opencode_makbuzu(home, con, ident, _saglayici, _olusma, m)
                actual, receipt = makbuz or (kurtarma_tutari(rezerve, m), 'hayalet:' + ident)
                ledger.settle(ident, actual, receipt)
        except BudgetDenied:
            continue
        kapandi += 1
        tutar += actual
        aylik += aylik_esini_kapat(home, _saglayici, _olusma, m, actual)
        if run_id:
            kosular.add(str(run_id))
    if kapandi:
        _kurtarma_log(home, {'islem': 'hayalet-kapat', 'yas_dk': yas, 'kapandi': kapandi, 'aylik': aylik,
                             'tutar_micro': tutar, 'kosular': sorted(kosular)})
    if belirsiz:
        _kurtarma_log(home, {'islem': 'sahiplik-belirsiz', 'kosular': sorted(belirsiz)})
    return {'kapandi': kapandi, 'tutar_micro': tutar, 'kosular': sorted(kosular), 'aylik': aylik,
            'belirsiz_kosular': sorted(belirsiz)}


def gunluk_mutabakat(home: Path, snapshot: dict[str, Any]) -> str:
    """P4: daily observation with named periods; missing API data does not count as zero."""
    now = datetime.now()
    hedef = home / 'zincir' / f'mutabakat-{now.date().isoformat()}.json'
    with FileLock(hedef.with_suffix('.lock')):
        if hedef.exists():
            return ''
        db = home / 'zincir/request-budget.db'
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as con:
            rows = con.execute('select provider, sum(actual), sum(case when actual is null then reserved else 0 end) '
                               'from reservation where created >= ? group by provider',
                               (now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp(),)).fetchall()
        go, zen = (snapshot.get(k) or {} for k in ('go', 'zen'))
        data = {'ts': now.isoformat(), 'gozlem_ts': snapshot.get('ts'),
                'defter_bugun_usd': {p: {'harcanan': (a or 0) / 1e6, 'askida': h / 1e6} for p, a, h in rows if p in PAID},
                'go_ay_puan': go.get('ay_puan') if go.get('db') == 'ok' else None,
                'go_kaynak': go.get('kaynak'),
                'go_donem_bas': go.get('donem_bas'),
                'zen_giris_sonrasi_harcanan_usd': zen.get('harcanan') if zen.get('db') == 'ok' else None,
                'zen_giris_tarihi': zen.get('giris_tarihi'),
                'zen_kalan_usd': zen.get('kalan') if zen.get('db') == 'ok' else None}
        atomic_json(hedef, data)
        return 'mutabakat: ' + json.dumps(data, ensure_ascii=False)


AYLIK_ES_PENCERE_SN = 600


def kurtarma_tutari(reserved: int, metadata: dict[str, Any]) -> int:
    values = [reserved, *(metadata[k] for k in ('en_kotu', 'verified_bound_micro') if metadata.get(k) is not None)]
    if any(type(v) is not int or not 0 <= v < 2**53 for v in values):
        raise BudgetDenied('recovery liability bound unreadable')
    return max(values)


def aylik_kurtarma_tamamla(home: Path) -> int:
    path = home / 'zincir/request-budget.db'
    if not path.exists():
        return 0
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)) as con:
        rows = con.execute("select provider,created,metadata,actual from reservation where actual is not null "
                           "and provider in ('opencode','openrouter') and (json_extract(metadata,'$.aylik_id') is not null "
                           "or json_extract(metadata,'$.session_id') is not null)").fetchall()
    def key(provider: str, metadata: dict[str, Any]) -> str:
        if metadata.get('aylik_id'):
            return 'id:' + str(metadata['aylik_id'])
        return json.dumps([provider, metadata.get('session_id'), metadata.get('model'), metadata.get('role')])
    counts: dict[str, int] = {}
    for provider, _, raw, _ in rows:
        ident = key(provider, json.loads(raw))
        counts[ident] = counts.get(ident, 0) + 1
    repaired = 0
    for provider, created, raw, actual in rows:
        metadata = json.loads(raw)
        if counts[key(provider, metadata)] == 1:
            repaired += aylik_esini_kapat(home, provider, created, metadata, actual)
    if repaired:
        _kurtarma_log(home, {'islem': 'aylik-es-tamamla', 'kapandi': repaired})
    return repaired


def aylik_esini_kapat(home: Path, saglayici: str, olusma: float, meta: dict[str, Any], tutar: int | None) -> int:
    yol = home / 'zincir/aylik-butce.db'
    kimlik, oturum, model, rol = meta.get('aylik_id'), meta.get('session_id'), meta.get('model'), meta.get('role')
    if not yol.exists() or not (kimlik or oturum):
        return 0
    from aylik_butce import MonthlyLedger, MonthlyBudgetDenied
    aylik = MonthlyLedger(yol, cap=1)
    with aylik.transaction() as con:
        if kimlik:
            satir = con.execute('select id, reserved from reservation where id=? and actual is null and provider=? '
                                "and (? is null or json_extract(metadata,'$.session_id')=?) "
                                "and (? is null or json_extract(metadata,'$.model')=?) "
                                "and (? is null or json_extract(metadata,'$.role')=?)",
                                (str(kimlik), saglayici, oturum, oturum, model, model, rol, rol)).fetchone()
        else:
            matches = con.execute("select id, reserved from reservation where actual is null and provider=? and created between ? and ? "
                                "and json_extract(metadata,'$.session_id')=? and (? is null or json_extract(metadata,'$.model')=?) "
                                "and (? is null or json_extract(metadata,'$.role')=?)",
                                (saglayici, olusma - AYLIK_ES_PENCERE_SN, olusma + 5, oturum, model, model, rol, rol)).fetchall()
            satir = matches[0] if len(matches) == 1 else None
    if not satir:
        return 0
    try:
        aylik.settle(satir[0], int(satir[1]) if tutar is None else int(tutar), 'kurtarma:' + satir[0])
    except MonthlyBudgetDenied:
        return 0
    return 1


TAHMINI_ONEKLER = ('hayalet:', 'kurtarma:')


def _aylik_es_duzelt(home: Path, meta: dict[str, Any], actual: int, generation: str, uygula: bool) -> str:
    kimlik = meta.get('aylik_id')
    yol = home / 'zincir/aylik-butce.db'
    if not kimlik or not yol.exists():
        return 'yok'
    from aylik_butce import MonthlyLedger
    yeni = f'{kimlik}:{generation}'
    with MonthlyLedger(yol, cap=1).transaction() as con:
        satir = con.execute('select actual, receipt from reservation where id=?', (str(kimlik),)).fetchone()
        if satir is None:
            return 'yok'
        if tuple(satir) == (actual, yeni):
            return 'zaten'
        if satir[0] is not None and not str(satir[1] or '').startswith(TAHMINI_ONEKLER):
            return 'celisiyor'
        if uygula:
            con.execute('update reservation set reserved=max(reserved, ?), actual=?, receipt=? where id=?', (actual, actual, yeni, str(kimlik)))
        return 'duzeltildi'


def tahmini_kapanis_duzelt(home: Path, ident: str, generation: str, actual: int, gerekce: str) -> dict[str, Any]:
    if (not isinstance(ident, str) or not re.fullmatch(r'gen-[A-Za-z0-9_-]{1,128}', str(generation or ''))
            or type(actual) is not int or not 0 <= actual < 2**53):
        return {'durum': 'gecersiz'}
    ledger = BudgetLedger(home / 'zincir/request-budget.db')
    makbuz = f'{ident}:{generation}'
    with FileLock(home / 'zincir/hayalet-makbuz.lock'):
        with ledger.transaction() as con:
            satir = con.execute('select reserved, actual, receipt, metadata from reservation where id=?', (ident,)).fetchone()
        if satir is None:
            return {'durum': 'yok'}
        rezerve, onceki, onceki_makbuz, meta = satir[0], satir[1], satir[2], json.loads(satir[3] or '{}') or {}
        if meta.get('generation') != generation:
            return {'durum': 'kimlik-uyusmuyor'}
        if (onceki, onceki_makbuz) == (actual, makbuz):
            return {'durum': 'zaten-duzeltildi'}
        if onceki is None or not str(onceki_makbuz or '').startswith(TAHMINI_ONEKLER):
            return {'durum': 'tahmini-degil'}
        if actual > kurtarma_tutari(rezerve, meta):
            return {'durum': 'ust-siniri-asiyor'}
        if _aylik_es_duzelt(home, meta, actual, generation, uygula=False) == 'celisiyor':
            return {'durum': 'aylik-es-celisiyor'}
        aylik = _aylik_es_duzelt(home, meta, actual, generation, uygula=True)
        iz = {'onceki_actual': onceki, 'onceki_receipt': onceki_makbuz, 'gerekce': str(gerekce or '')[:200], 'ts': time.time()}
        with ledger.transaction() as con:
            if tuple(con.execute('select actual, receipt from reservation where id=?', (ident,)).fetchone()) != (onceki, onceki_makbuz):
                return {'durum': 'eszamanli-degisti'}
            con.execute("update reservation set actual=?, receipt=?, metadata=json_set(metadata,'$.tahmini_duzeltme',json(?)) where id=?",
                        (actual, makbuz, json.dumps(iz), ident))
    _kurtarma_log(home, {'islem': 'tahmini-duzelt', 'id': ident, 'onceki_micro': onceki, 'yeni_micro': actual, 'aylik': aylik})
    return {'durum': 'duzeltildi', 'onceki_actual': onceki, 'actual': actual, 'aylik': aylik}


def kurtarma(home: Path, argv: list[str]) -> int:
    ledger = BudgetLedger(home / 'zincir/request-budget.db')
    komut = argv[0]
    evet = '--evet' in argv
    sifir = '--sifir' in argv
    yas = 6.0
    if '--saat' in argv:
        try:
            yas = float(argv[argv.index('--saat') + 1])
        except (IndexError, ValueError):
            print('--saat sayi ister', file=sys.stderr)
            return 2
    if yas < 0:
        print('--saat negatif olamaz', file=sys.stderr)
        return 2

    if komut in ('--bayat-listele', '--bayat-kapat'):
        if komut == '--bayat-kapat' and evet:
            aylik_kurtarma_tamamla(home)
        with ledger.transaction() as con:
            satirlar = bayat_rezervasyonlar(con, yas)
        if not satirlar:
            print('%g saatten eski askida rezervasyon yok.' % yas)
            return 0
        toplam = sum(r[3] for r in satirlar)
        for ident, saglayici, olusma, rezerve, meta in satirlar:
            m = json.loads(meta) if meta else {}
            ownership = sahiplik_durumu(home, m.get('run_id'))
            print('%s %-12s $%8.4f %6.1fs once kosu=%s gorev=%s%s' % (
                ident[:8], saglayici, rezerve / 1e6, (time.time() - olusma) / 3600,
                m.get('run_id') or '-', m.get('task_id') or '-',
                '  [CANLI KOSU — kapatilmaz]' if ownership == 'live' else
                '  [SAHIPLIK BELIRSIZ — korunuyor]' if ownership == 'unknown' else ''))
        print('toplam %d rezervasyon, $%.4f askida' % (len(satirlar), toplam / 1e6))
        if komut == '--bayat-listele':
            return 0
        if not evet:
            print('Kapatmak icin --evet ekle. Varsayilan: rezerve tutar HARCANMIS sayilir (kapasite acilmaz).\n'
                  'Cagrilarin hic yapilmadigini biliyorsan --sifir ekle (para geri doner).', file=sys.stderr)
            return 2
        kapandi = atlandi = canli = aylik = 0
        belirsiz = set()
        for ident, saglayici, _olusma, rezerve, _meta in satirlar:
            _m = json.loads(_meta) if _meta else {}
            ownership = sahiplik_durumu(home, _m.get('run_id'))
            if ownership == 'unknown':
                belirsiz.add(str(_m.get('run_id')))
                continue
            if ownership == 'live':
                canli += 1        # the receipt of an in-flight call must not be lost (B110)
                continue
            try:
                gercek = 0 if sifir else kurtarma_tutari(rezerve, _m)
                with kurtarma_kilidi(home, _m.get('run_id')) as safe:
                    if not safe:
                        belirsiz.add(str(_m.get('run_id')))
                        continue
                    ledger.settle(ident, gercek, 'kurtarma:' + ident)
                    kapandi += 1
                    aylik += aylik_esini_kapat(home, saglayici, _olusma, _m, gercek)
            except BudgetDenied:
                atlandi += 1
        _kurtarma_log(home, {'islem': 'bayat-kapat', 'sifir': sifir, 'yas_saat': yas,
                             'kapandi': kapandi, 'atlandi': atlandi, 'canli': canli, 'belirsiz_kosular': sorted(belirsiz), 'tutar_micro': toplam})
        if belirsiz:
            _kurtarma_log(home, {'islem': 'sahiplik-belirsiz', 'kosular': sorted(belirsiz)})
            print('%d kosunun sahipligi belirsiz; rezervasyonlari korunuyor.' % len(belirsiz))
        print('%d rezervasyon kapatildi (%s), %d atlandi, %d yasayan kosuya dokunulmadi; aylik defterde %d es kapatildi.' % (
            kapandi, 'sifirla, para geri verildi' if sifir else 'rezerve tutar harcanmis sayildi', atlandi, canli, aylik))
        return 0

    if komut in ('--hayalet-listele', '--hayalet-geri'):
        with ledger.transaction() as con:
            satirlar = con.execute("select id,provider,created,reserved,actual,metadata from reservation "
                                   "where receipt like 'hayalet:%' and actual > 0 order by created").fetchall()
        if not satirlar:
            print('otomatik kapatilmis (hayalet) rezervasyon yok.')
            return 0
        toplam = sum(r[4] for r in satirlar)
        for ident, saglayici, olusma, _rez, gercek, meta in satirlar:
            m = json.loads(meta) if meta else {}
            print('%s %-12s $%8.4f %6.1fs once kosu=%s' % (
                ident[:8], saglayici, gercek / 1e6, (time.time() - olusma) / 3600, m.get('run_id') or '-'))
        print('toplam %d kayit, $%.4f harcanmis SAYILDI (gercekten harcanmis olabilir)' % (len(satirlar), toplam / 1e6))
        if komut == '--hayalet-listele':
            return 0
        if not evet:
            print('Geri almak icin --evet ekle. ONCE SAGLAYICI PANELINE BAK: bu tutar orada gorunuyorsa\n'
                  'geri alma defterimizi gercegin ALTINA yazar.', file=sys.stderr)
            return 2
        with ledger.transaction() as con:
            con.execute("update reservation set actual=0 where receipt like 'hayalet:%' and actual > 0")
        _kurtarma_log(home, {'islem': 'hayalet-geri', 'kayit': len(satirlar), 'tutar_micro': toplam})
        print('%d kayit geri alindi, $%.4f kapasite acildi.' % (len(satirlar), toplam / 1e6))
        return 0

    if komut in ('--ariza-listele', '--ariza-temizle'):
        with ledger.transaction() as con:
            ariza = [r[0] for r in con.execute('select provider from pricing_fault order by provider')]
        if or_sapma_aktif(home) and 'openrouter' not in ariza:
            ariza.append('openrouter')   # an OR-PIN ledger-panel drift also closes the provider; cleanup renews the baseline
        if not ariza:
            print('pricing_fault bos — kapali saglayici yok.')
            return 0
        print('fiyat arizasi (saglayici tamamen kapali): ' + ', '.join(ariza))
        if komut == '--ariza-listele':
            return 0
        hedef = [a for a in argv[1:] if not a.startswith('--')]
        sil = [p for p in ariza if not hedef or p in hedef]
        if not sil:
            print('verilen saglayici listede yok', file=sys.stderr)
            return 2
        if not evet:
            print('Temizlenecek: %s — onaylamak icin --evet ekle.\n'
                  'Arizanin sebebi (gercek maliyet tahmini asti) giderilmediyse ayni anda geri gelir.' % ', '.join(sil),
                  file=sys.stderr)
            return 2
        with ledger.transaction() as con:
            for saglayici in sil:
                con.execute('delete from pricing_fault where provider=?', (saglayici,))
        if 'openrouter' in sil:
            for ad in ('openrouter-sapma.json', 'openrouter-taban.json', 'openrouter-kredi.json'):
                (home / 'zincir' / ad).unlink(missing_ok=True)
        _kurtarma_log(home, {'islem': 'ariza-temizle', 'saglayicilar': sil})
        print('temizlendi: ' + ', '.join(sil))
        return 0

    if komut in ('--makbuz-listele', '--makbuz-kabul'):
        hedef = [a for a in argv[1:] if not a.startswith('--')]
        if komut == '--makbuz-kabul':
            if len(hedef) != 1:
                print('kullanim: --makbuz-kabul REZERVASYON_ID --evet', file=sys.stderr)
                return 2
            if not evet:
                print('Fazla makbuz kabulu aileyi yeniden acar; tutar defterde kalir, toplam/aylik tavan buyumez. Onaylamak icin --evet ekle.',
                      file=sys.stderr)
                return 2
            try:
                kabul = makbuz_kabul(home, hedef[0])
            except BudgetDenied as exc:
                print('makbuz kabul edilmedi: %s' % exc, file=sys.stderr)
                return 2
            print('%s %s kosu=%s gercek $%.4f ust sinir $%.4f -> %s' % (
                kabul['id'][:8], kabul['provider'], kabul['run_id'] or '-', kabul['actual'] / 1e6, kabul['bound'] / 1e6,
                'zaten kabul edilmisti' if kabul['already'] else 'kabul kaydedildi'))
            return 0
        with ledger.transaction() as con:
            if hedef:
                ids = {hedef[0]}
                if (RunStore(home).path(hedef[0]) / 'run.json').exists():
                    ids |= operator_ailesi(home, hedef[0])[0]
            else:
                ids = {r[0] for r in con.execute("select distinct json_extract(metadata,'$.run_id') from reservation "
                                                 "where json_extract(metadata,'$.run_id') is not null")}
            satirlar = makbuz_asimlari(con, ids)
        if not satirlar:
            print('kabul bekleyen fazla makbuz yok.')
            return 0
        for r in satirlar:
            print('%s %-12s kosu=%s gercek $%.4f ust sinir $%.4f  -> cadre kurtar --makbuz-kabul %s --evet' % (
                r['id'][:8], r['provider'], r['run_id'], r['actual'] / 1e6, r['bound'] / 1e6, r['id']))
        print('toplam %d fazla makbuz; kabul edilene kadar aile yeni ucretli cagri acmaz.' % len(satirlar))
        return 0

    print('bilinmeyen kurtarma komutu: ' + komut, file=sys.stderr)
    return 2


KURTARMA_KOMUTLARI = ('--bayat-listele', '--bayat-kapat', '--hayalet-listele', '--hayalet-geri',
                      '--ariza-listele', '--ariza-temizle', '--makbuz-listele', '--makbuz-kabul')


SAGLAYICI_KESINTISI = 'provider_unavailable'
KESINTI_REDDI = re.compile(r'provider unavailable: (\S+) until (\d+)')


def saglayici_bekleme_reddi(receipt: Any) -> tuple[str, float] | None:
    for row in ((receipt or {}).get('refusals') or []) if isinstance(receipt, dict) else []:
        eslesme = KESINTI_REDDI.fullmatch(str((row or {}).get('reason') or ''))
        if eslesme:
            return eslesme[1], float(eslesme[2])
    return None


def saglayici_hatasi_kaydet(ledger: BudgetLedger, request: dict[str, Any]) -> dict[str, Any]:
    tur = str(request.get('tur') or '')[:64]
    hata = {'kod': request.get('kod') if isinstance(request.get('kod'), int) else None, 'tur': tur,
            'mesaj': str(request.get('mesaj') or '')[:160], 'ts': time.time()}
    satir = ledger.hata_kaydet(str(request.get('id') or ''), hata)
    if not satir or not satir['model']:
        return {}
    try:
        import yonlendirici
    except ImportError:
        return {}
    sag = taban_saglayici(satir['provider'])
    anahtar = yonlendirici.model_anahtari(sag, satir['model'])
    uc = satir.get('uc') if sag == 'openrouter' and tur == SAGLAYICI_KESINTISI else None
    yonlendirici.ariza(sag, f"{hata['kod']} {tur} {hata['mesaj']}", model=anahtar,
                       allowed_fails=1 if tur == SAGLAYICI_KESINTISI else 3, uc=uc)
    return {'yol': anahtar, 'uc': uc}


def kapali_yol(provider: str, model: str, uclar: list[str] | None = None) -> tuple[str, float] | None:
    if not model:
        return None
    try:
        import yonlendirici
    except ImportError:
        return None
    sag = taban_saglayici(provider)
    anahtar = yonlendirici.model_anahtari(sag, model)
    kayit = yonlendirici.durum_oku()
    durum = kayit.get(anahtar) or {}
    try:
        bitis = float(durum.get('cooldown_bitis') or 0)
    except (TypeError, ValueError):
        return None
    if bitis > time.time() and '[ag]' in str(durum.get('son') or ''):
        return anahtar, bitis
    kapali = yonlendirici.uc_sogumalari(sag, model, kayit) if sag == 'openrouter' else {}
    if not kapali:
        return None
    if uclar is None:
        try:
            uclar = json.loads((yonlendirici.QE / 'zincir/openrouter-fiyat.json').read_text())['modeller'][anahtar.split('/', 1)[1]]['providers']
        except (OSError, ValueError, KeyError, TypeError):
            return anahtar, min(kapali.values())
        if any(uc not in kapali for uc in uclar):
            return None
    if not isinstance(uclar, list) or not uclar or any(not isinstance(uc, str) or not uc for uc in uclar):
        raise BudgetDenied('endpoint selection invalid')
    bitisler = [kapali[uc] for uc in uclar if uc in kapali]
    return (anahtar, min(bitisler)) if bitisler else None


def main() -> int:
    # B114-7: these two reads were outside the `try` — with a missing env the process died with an UNRECORDED traceback.
    # the "every denial is recorded" contract forbids that; recording needs home, so we read them first and
    # exit with the standard denial (75) when they are missing.
    try:
        home = Path(os.environ['CADRE_HOME'])
        code = Path(os.environ['CADRE_ENGINE_ROOT'])
    except KeyError as exc:
        print(f'budget denied: missing environment {exc}', file=sys.stderr)
        return 75
    if len(sys.argv) > 1 and sys.argv[1] in KURTARMA_KOMUTLARI:
        return kurtarma(home, sys.argv[1:])
    if len(sys.argv) >= 4 and sys.argv[1] == '--ilerleme':
        run_id = sys.argv[4] if len(sys.argv) > 4 else os.environ.get('CADRE_RUN_ID', '')
        if not run_id or sys.argv[2] not in ILERLEME_NEDENLERI:
            print('kullanim: --ilerleme NEDEN KIMLIK [RUN_ID]; neden: ' + ', '.join(ILERLEME_NEDENLERI), file=sys.stderr)
            return 2
        try:
            ilerleme_yaz(home, run_id, sys.argv[2], sys.argv[3])
        except (OSError, ValueError, KeyError) as exc:
            print(f'ilerleme yazilamadi: {exc}', file=sys.stderr)
            return 1
        return 0
    ledger = BudgetLedger(home/'zincir/request-budget.db')
    request = {}
    _dilim_adi = None
    try:
        request = json.load(sys.stdin)
        if request['action'] == 'reserve':
            metadata = request.get('metadata') or {}
            kapali = kapali_yol(request['provider'], str(metadata.get('model') or ''),
                                (metadata.get('uc_secimi') or {}).get('sira'))
            if kapali:
                raise BudgetDenied(f'provider unavailable: {kapali[0]} until {kapali[1]:.0f}')
            snapshot = observed_budget(home, code)
            maximum = int(Decimal(str(amount(request['maximum']))).to_integral_value(rounding=ROUND_CEILING))
            run_id = os.environ.get('CADRE_RUN_ID')
            # The helper pocket is real only if the calling AGENT is known: the engine writes only the PHASE role into the environment
            # (plan/impl/qa/kalite/otopsi), compaction/scout never came through. The plugin reports the calling
            # agent's name in the metadata; for helper roles we go by that.
            ajan = str((request.get('metadata') or {}).get('role') or '')
            # if the contract module cannot be read (isolated test home, incomplete install) role selection is NOT DONE and the old
            # behaviour continues — the whole reservation path must not break for the sake of one helper label.
            try:
                from sozlesme import dilim as _dilim
                rol_secimi = ajan if _dilim(ajan) == 'yardimci' else None
            except Exception:
                rol_secimi = None
            _tavan, _dilim_adi = rol_butcesi(home, run_id, rol_secimi, request['provider'])
            # B124: in manual `opencode` use the gate does NOT BLOCK, it only observes. For a request belonging to a run
            # (CADRE_RUN_ID set) observation mode is IGNORED — automated work is always subject to the cap.
            gozlem = os.environ.get('CADRE_BUTCE_GOZLEM') == '1' and not run_id
            ident = ledger.reserve(request['provider'], maximum, snapshot,
                                   {**request.get('metadata', {}), 'run_id':run_id,
                                    'phase_role':os.environ.get('CADRE_ROL'),
                                    'attempt_id':os.environ.get('CADRE_BUDGET_ATTEMPT'),
                                    'task_id':os.environ.get('CADRE_GOREV_ID') or 'main'},
                                   run_cap=_tavan, dilim=_dilim_adi, gozlem=gozlem)
            kosu_harcama_yaz(home, ledger, run_id)
            print(json.dumps({'id':ident}))
        elif request['action'] == 'settle':
            ledger.settle(request['id'], request['actual'], request['receipt'], request.get('kullanim'))
            kosu_harcama_yaz(home, ledger, os.environ.get('CADRE_RUN_ID'))
            print('{}')
        elif request['action'] == 'saglayici-hatasi':
            print(json.dumps(saglayici_hatasi_kaydet(ledger, request)))
        elif request['action'] == 'gen':
            try:
                ledger.uretim_kaydet(str(request.get('id') or ''), str(request.get('generation') or ''))
            except (OSError, sqlite3.Error):
                pass
            print('{}')
        elif request['action'] == 'review-tool':
            try:
                ledger.review_tool(os.environ.get('CADRE_RUN_ID'), os.environ.get('CADRE_GOREV_ID', ''), os.environ.get('CADRE_ROL', ''))
            except BudgetDenied as exc:
                if not re.fullmatch(r'review limit: [a-z-]+: tools', str(exc)):
                    raise
                note_denial(home, reason=str(exc), deneme=False)
                print(json.dumps({'tool_limit': str(exc)}))
            else:
                print('{}')
        elif request['action'] == 'deny':
            # G-B5 (B154): a plugin denial is recorded with provider and reason; the engine turns it into a task ban.
            note_denial(home, request.get('provider'), reason=str(request.get('reason') or 'plugin refusal'))
            print('{}')
        else:
            raise BudgetDenied('unknown operation')
        return 0
    except (BudgetDenied, OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        note_denial(home, request.get('provider') if isinstance(request, dict) else None,
                    reason=str(exc) if isinstance(exc, BudgetDenied) else '', dilim=_dilim_adi)
        print('budget denied: no verified allowance or receipt', file=sys.stderr)
        return 75


if __name__ == '__main__':
    raise SystemExit(main())
