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
from run_store import FileLock, RunStore, atomic_json, identifier

# This is the SINGLE SOURCE of the daily REQUEST cap; `provider_budget.py` reads from here, consistency with the router's rpd
# is bound by a test (B110). 4c-11: of the free providers only groq remains, for the pre-filter.
DAILY_REQUEST = {'groq': 900}
FREE = {'groq': ('groq', DAILY_REQUEST['groq'])}
PAID = {'opencode-go': 'go', 'opencode': 'zen', 'openrouter': 'or'}   # Go is a subscription pool; Zen and OpenRouter are prepaid

# Item 0b — THE BUCKET UNIT IS THE MODEL: zero-priced models are written to the '<provider>:free' bucket,
# REQUESTS are counted instead of dollars and they do not enter the run's dollar cap; the provider window still binds.
FREE_SUFFIX = ':free'


def free_day_cap() -> int:
    try:
        value = int(os.environ.get('CADRE_FREE_DAY_CAP', '2000'))
    except ValueError:
        return 2000
    return value if value > 0 else 2000


MODEL_SUFFIX = '#'
# B110: the daily REQUEST limit may be per model ('<provider>#<model>' bucket). No such provider today
# (Gemini was removed in 4c-11); the mechanism remains, the table is empty.
MODEL_DAILY_REQUEST: dict[str, tuple[tuple[str, int], ...]] = {}


def base_provider(provider: str) -> str:
    """'opencode-go:free' -> 'opencode-go', 'provider#model' -> 'provider'.
    The circuit breaker, cooldown and observation always work with the base id."""
    return provider.split(MODEL_SUFFIX, 1)[0].split(FREE_SUFFIX, 1)[0]


def model_id(provider: str) -> str:
    """'provider#model' -> 'model'; empty string if the bucket carries no model."""
    return provider.split(MODEL_SUFFIX, 1)[1] if MODEL_SUFFIX in provider else ''


def model_bucket(provider: str, model: str) -> str:
    """Router and executor ask for the model bucket the plugin reserved."""
    if provider in MODEL_DAILY_REQUEST:
        return f'{provider}#{model}'
    if provider not in FREE:
        from model_catalog import is_free
        if is_free(provider, model) is True:
            return provider + FREE_SUFFIX
    return provider


def or_deviation_active(home: Path) -> bool:
    """4c-7: the drift file closes OR and shows it as failed only while active: a persistent alarm (receipt ↔ breakdown) or a closed last credit
    observation. An old file whose drift has ended (04.10) does not list the provider as failed."""
    chain = Path(home) / 'chain'
    try:
        deviation = json.loads((chain / 'openrouter-deviation.json').read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(deviation, dict) or deviation.get('permanent') is True:
        return True
    try:
        return json.loads((chain / 'openrouter-credit.json').read_text()).get('closed') is True
    except (OSError, ValueError, AttributeError):
        return True   # if the credit observation cannot be read, the drift counts as ongoing


def or_endpoint_allowed(rule: Any, m: dict, endpoint: str) -> bool:
    """4c-7: does the endpoint match the policy — an explicit list (tag or name) or a rule (closed weights / reported quantization)."""
    if isinstance(rule, list):
        return endpoint in rule
    if not isinstance(rule, dict):
        return False
    items_list = rule.get('tiers', rule.get('providers'))
    if isinstance(items_list, list):
        return endpoint in items_list
    return rule.get('closed') is True or ((m.get('tiers') or {}).get(endpoint) or {}).get('quantization') == (rule.get('quantization') or 'fp8')


def or_pin_price(home: Path, model: str, output: int | None = None) -> bool:
    """OR-PIN: the plugin's `orMostExpensive` rule (fresh v2 price file, age-limited last good price, endpoints matching the policy).
    If `output` is given at least one endpoint must carry that output (the plugin filters at request time; the router must not offer an empty link)."""
    from limits import OR as OR_LIMIT
    try:
        d = json.loads((Path(home) / 'chain/openrouter-price.json').read_text())
        now_ts = time.time()
        m = d['models'][model]
        rule = json.loads(Path(__file__).with_name('openrouter-policy.json').read_text())[model]
        paths = m['providers']
        return (d.get('version') == 2 and 0 <= now_ts - float(d['ts']) < OR_LIMIT['price_fresh_sec']
                and 0 <= now_ts - float(m.get('price_ts', d['ts'])) < OR_LIMIT['price_age_sec']
                and m.get('fp8_cache') is True and isinstance(paths, list) and bool(paths)
                and all(or_endpoint_allowed(rule, m, y) for y in paths) and float(m['input']) > 0 and float(m['output']) > 0
                and (output is None or any((((m.get('tiers') or {}).get(y) or {}).get('max_output') or output) >= output
                                          for y in paths)))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def model_daily_request(provider: str, model: str) -> int:
    """Daily request cap of this model. An unknown model = the LOWEST published value of that provider
    (fail-closed: do not take a Flash quota of 20 for 500 and run into a string of 429s)."""
    patterns = MODEL_DAILY_REQUEST.get(base_provider(provider))
    if not patterns:
        return DAILY_REQUEST.get(base_provider(provider), 0)
    entry_name = (model or '').lower()
    for part, value in patterns:
        if part in entry_name:
            return value
    return min(value for _, value in patterns)


class BudgetDenied(RuntimeError):
    """No proven allowance; callers must use another provider or wait locally."""


class ProviderWait(BudgetDenied):
    pass


def note_denial(home: Path, provider: str | None = None, reason: str = '', slice: str | None = None, try_no: bool = True) -> None:
    """A task-owned receipt, so the engine does not infer budget state from logs.
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
                # a second record for the same provider + task within 2 s does not open a new line, it increments the `repeat` counter (the first reason is kept).
                _now_ts = time.time(); _task = os.environ.get('CADRE_TASK_ID') or 'main'
                _twin = None
                for _r in rows[-5:]:
                    try:
                        _ts = time.mktime(time.strptime(str(_r.get('ts', ''))[:19], '%Y-%m-%dT%H:%M:%S'))
                    except (ValueError, TypeError):
                        continue
                    if provider and _r.get('provider') == provider and _r.get('task') == _task and abs(_now_ts - _ts) <= 2:
                        _twin = _r
                row = {'ts':time.strftime('%Y-%m-%dT%H:%M:%S'), 'provider':provider, 'reason':reason,
                       'task':os.environ.get('CADRE_TASK_ID') or 'main', 'attempt':os.environ.get('CADRE_BUDGET_ATTEMPT')}
                if rows and all(rows[-1].get(k) == row[k] for k in ('provider', 'reason', 'task', 'attempt')):
                    rows[-1]['repeat'] = int(rows[-1].get('repeat', 1)) + 1  # B49: the same denial line must not repeat for the same attempt
                    row = None
                elif _twin is not None:
                    _twin['repeat'] = int(_twin.get('repeat', 1)) + 1        # B169: twin denial in the same second
                    row = None
                if row:
                    rows.append(row)
                record.update(denials=rows[-50:], updated=time.time())
                atomic_json(store.path(run)/'run.json', record)
        except (OSError, ValueError, KeyError):
            pass
    if not try_no:
        return
    try:
        run = os.environ['CADRE_RUN_ID']
        task = os.environ.get('CADRE_TASK_ID') or 'main'
        attempt = identifier(os.environ['CADRE_BUDGET_ATTEMPT'])
        store = RunStore(home)
        store.read(run)
        path = store.task_dir(run, task)/'attempts'/attempt
        if path.is_dir():
            target = path/'budget-denied.json'
            with FileLock(path/'budget-denied.lock'):
                saved = json.loads(target.read_text()) if target.exists() else {}
                providers = set(saved.get('providers', []))
                if provider and base_provider(provider) in {*PAID, *FREE, 'claude', 'opencode'}:
                    providers.add(provider)   # 0b: bucket names like 'opencode-go:free' must also be written —
                    # otherwise qe_core's fallback 'if the list is empty, ban ALL cloud' kicks in.
                refusals = list(saved.get('refusals') or [])
                refusal = {'provider': provider, 'reason': reason, 'slice': slice,
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
BUCKET_HOUR = {'openrouter': 'UTC'}


def bucket_day_beginning(provider: str, now: float | None = None) -> float:
    """Start epoch of this provider's daily bucket (local day by default)."""
    now = time.time() if now is None else now
    region = BUCKET_HOUR.get(base_provider(provider))
    if not region:
        return datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    from zoneinfo import ZoneInfo
    local = datetime.fromtimestamp(now, ZoneInfo(region))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def bucket_period(provider: str, now: float, reserve: str) -> str:
    region = BUCKET_HOUR.get(base_provider(provider))
    if not region:
        return reserve
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(now, ZoneInfo(region)).strftime('%Y-%m-%d')


def measured_pay(snapshot: Any, provider: str) -> int | None:
    """B129: the MEASURED share of the dollar bucket (micro-USD; remaining balance / Go period share), margin subtracted; None for count buckets.
    `allowance()` gives the remainder; adding the ledger's `settled` to it would silently grow the cap when a stale reservation closes."""
    try:
        if base_provider(provider) not in PAID or provider.endswith(FREE_SUFFIX) or provider == 'opencode-go':
            return None   # The Go share is pool points, not dollars (4c-13); its capacity is in go_capacity
        row = snapshot[PAID[base_provider(provider)]]
        pay = Decimal(str(amount(row['pay'])))
        if base_provider(provider) == 'opencode':
            pay = max(Decimal(0), pay - pay * Decimal('0.15'))   # the SAME margin as allowance
        return int((pay * 1_000_000).to_integral_value(rounding=ROUND_FLOOR))
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None


def or_receipt_observation(con: sqlite3.Connection | None, stamp: float) -> dict[str, Any]:
    """N-BU03: receipt ids before the credit request; estimated/later spending is not included."""
    rows = [] if con is None else con.execute(
        "select id,actual,receipt,coalesce(json_extract(metadata,'$.settled_ts'),created) from reservation "
        "where provider='openrouter' and actual > 0 and receipt is not null and receipt != '' "
        "and receipt not like 'phantom:%' and receipt not like 'recovery:%' "
        "and coalesce(json_extract(metadata,'$.settled_ts'),created) <= ? order by id", (stamp,)).fetchall()
    if any(type(cost) is not int or not 0 < cost < 2**53
           or type(when) not in (int, float) or not math.isfinite(when) or when < 0
           for _, cost, _, when in rows):
        raise ValueError('invalid receipt observation')
    encoded = json.dumps(rows, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    return {'ts': stamp, 'receipt_sha256': hashlib.sha256(encoded).hexdigest(),
            'receipt_micro': sum(row[1] for row in rows)}


def or_included_receipt(con: sqlite3.Connection, snapshot: Any) -> int:
    """N-BU01/03: only history bound to the same credit/ledger observation; it is not subtracted twice from the net balance."""
    try:
        proof = snapshot['or']['credit_observation']
        stamp = amount(proof['ts'])
        if not 0 <= time.time() - stamp < 300:
            return 0
        seen = or_receipt_observation(con, stamp)
        if any(proof[key] != seen[key] for key in ('receipt_sha256', 'receipt_micro')):
            return 0
        usage = proof['spent_micro']
        if type(usage) is not int or not 0 <= seen['receipt_micro'] <= usage < 2**53:
            return 0
        return seen['receipt_micro']
    except (BudgetDenied, KeyError, TypeError, ValueError, sqlite3.Error):
        return 0


def allowance(snapshot: Any, provider: str, now: float | None = None, model: str | None = None) -> tuple[str, int]:
    """Reject missing, stale, future, or partially unreadable observations. On Go the remaining points are converted to this model's
    list dollars; without a model the limit of the heaviest model (the smallest amount) is used."""
    try:
        now = time.time() if now is None else now
        stamp = datetime.fromisoformat(snapshot['ts']).timestamp()
        period = datetime.fromtimestamp(now).strftime('%Y-%m-%d')
        if not 0 <= now - stamp <= 90 or snapshot['today'] != period:
            raise BudgetDenied('stale budget')
        period = bucket_period(provider, now, period)   # the count bucket rolls over on the provider's calendar
        if provider == 'claude':
            from claude_policy import period as claude_period, ZONE
            period = claude_period(datetime.fromtimestamp(now, ZONE))
            row = snapshot['claude']
            # 'fresh' (<=30 min) or provider_budget.py's stale-but-compensated observation ('valid': True, <=7 days).
            if not (row.get('source') == 'fresh' or row.get('valid') is True):
                raise BudgetDenied('Claude quota observation unavailable')
            # The weekly reserve policy supplies remaining percentage. Without
            # a conversion observation, an automatic USD authorization is unknown.
            value = min(amount(row['pay']) * amount(row['percent1_usd']), amount(row['pool_remaining_usd']))
            return period, int((Decimal(str(value)) * 1_000_000).to_integral_value(rounding=ROUND_FLOOR))
        if provider in PAID:
            row = snapshot[PAID[provider]]
            if row.get('db') != 'ok':
                raise BudgetDenied('accounting unavailable')
            if provider == 'opencode-go' and row.get('window_open') is not True:
                raise BudgetDenied('provider window closed')
            value = amount(row['available'])
            if provider == 'opencode-go':
                if row.get('unit') != 'points':
                    raise BudgetDenied('Go pool unit unknown')
                from go_window import usd as go_usd
                value = go_usd(model or '', value)
            if provider in ('opencode', 'openrouter'):
                # One 85% boundary for admission, the running brake and reopening.
                # Use decimal arithmetic so the exact boundary cannot flicker.
                margin = Decimal(str(amount(row['pay']))) * Decimal('0.15')
                value = max(Decimal(0), Decimal(str(value)) - margin)
            return period, int((Decimal(str(value)) * 1_000_000).to_integral_value(rounding=ROUND_FLOOR))
        if provider.endswith(FREE_SUFFIX):
            base = base_provider(provider)
            if base not in PAID and base not in FREE and base != 'opencode':
                raise BudgetDenied('unmetered provider')
            # Even at zero price the provider window binds: while the Go window is closed no call goes out.
            if base == 'opencode-go' and snapshot.get('go', {}).get('window_open') is not True:
                raise BudgetDenied('provider window closed')
            # The daily count is held by the ledger's own 'settled' total: since the allocation cap is fixed with min()
            # within the period, returning a fixed cap gives the right result (1 is reserved per request).
            if base == 'openrouter':
                counter = (snapshot.get('or') or {}).get('free_request') or {}
                if any(type(counter.get(k)) is not int or counter[k] < 0 for k in ('used', 'limit', 'remaining')):
                    raise BudgetDenied('OpenRouter account request limit unknown')
                if counter['remaining'] > max(0, counter['limit'] - counter['used']):
                    raise BudgetDenied('OpenRouter account request count inconsistent')
                return period, counter['remaining']
            return period, free_day_cap()
        base = base_provider(provider)
        if base in FREE:
            key, limit = FREE[base]
            row = snapshot[key]
            if row.get('db') != 'ok':
                raise BudgetDenied('request accounting unavailable')
            # If the bucket carries a MODEL (<provider>#<model>), both the limit and the counter belong to that model.
            model = model_id(provider)
            if model:
                limit = model_daily_request(base, model)
                counters = row.get('today_request_model')
                # Without a per-model counter (old observation) we use the provider total: the cap is the model's,
                # the count is the provider's — it over-counts, never under-counts (fail-closed, and it does not block the call).
                used = amount(counters.get(model, 0)) if isinstance(counters, dict) else amount(row['today_request'])
            else:
                used = amount(row['today_request'])
            if not used.is_integer():
                raise BudgetDenied('invalid request count')
            return period, max(0, limit - int(used))
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        raise BudgetDenied('unknown budget') from exc
    raise BudgetDenied('unmetered provider')


USD_PROVIDERS = frozenset({*PAID, 'claude'})


JUDGE_ROLE = 'quality'
JUDGE_PAY_RATIO = 0.15          # Item 3: this much of the run cap is reserved for the judge
JUDGE_PAY_BASE = 200_000      # ... and at least $0.20


def judge_reserve(cap: int, ratio: float = JUDGE_PAY_RATIO) -> int:
    """Micro-dollars reserved for the judge: the contract ratio (default 15%), at least $0.20, at most half the cap (B110).
    Without the half limit, a small cap leaves nothing for the lanes."""
    if cap <= 0:
        return 0
    try:
        ratio = float(ratio)
    except (TypeError, ValueError):
        ratio = JUDGE_PAY_RATIO
    if not 0 <= ratio <= 1:
        ratio = JUDGE_PAY_RATIO
    return min(cap // 2, max(int(cap * ratio), JUDGE_PAY_BASE))


ALARM_WINDOW_SEC = 600
ALARM_ABSOLUTE_BASE = 1_000_000
ALARM_PROGRESSLESS_SEC = 1200
ALARM_PROGRESSLESS_CALL = 3


PROGRESS_REASONS = ('qa-pass', 'acceptance', 'lane-produced', 'plan-acceptance', 'plan-review')


def product_id(record: dict[str, Any]) -> str | None:
    """N-BU06: product evidence = worktree product digest; unknown (None) if there is no worktree or it cannot be read."""
    wt = record.get('worktree')
    if not wt or not Path(str(wt)).is_dir():
        return None
    try:
        from outcome import revision
        return revision(wt)['digest']
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def spend_alarm(home: Path, con: sqlite3.Connection, run_id: str, now_ts: float | None = None) -> tuple[str | None, dict | None]:
    """N-BU06: speed is only a warning; a recorded definite alarm waits for a separate approval. Cash and subscription equivalents are measured separately.
    Progress is split three ways: productive (recorded step/product/criterion/test evidence in the window), none (in a continuously expensive section
    >= ALARM_PROGRESSLESS_CALL closed calls, no progress record AND the product digest unchanged since the section start) and
    unknown (the rest). Only `none` produces a definite alarm (FIX-53/CR-03); a missing record alone is not inefficiency."""
    now_ts = time.time() if now_ts is None else now_ts
    try:
        record = RunStore(home).read(run_id)
    except (OSError, ValueError, KeyError):
        return None, None
    approval = amount(record.get('spend_approval_ts') or 0)
    old = record.get('spend_alarm') or {}
    if old and amount(old.get('ts') or 0) > approval:
        return f"spend alarm: awaiting approval ({old.get('reason')})", None
    cap = run_cap_micro(home, run_id) or 0
    base_ts = max(amount(record.get('created') or now_ts), approval)
    base = int(amount(os.environ.get('CADRE_SPEND_ALARM_BASE_USD', '0.20')) * 1_000_000)   # a cent of jitter is not an alarm
    base = max(base, int(0.2 * cap))
    pools = []
    for scope, providers, unit in (('cash', ('opencode', 'openrouter'), 'USD'),
                                    ('opencode-go', ('opencode-go',), 'USD-equivalent'),
                                    ('claude', ('claude',), 'USD-equivalent')):
        marks = ','.join('?' * len(providers))
        lines = con.execute(f"select created, actual from reservation where json_extract(metadata,'$.run_id')=? and provider in ({marks}) "
                               "and actual is not null and created >= ? and created <= ?",
                               (run_id, *providers, base_ts, now_ts)).fetchall()
        total = sum(a for _, a in lines)
        window = sum(a for c, a in lines if c >= now_ts - ALARM_WINDOW_SEC)
        average = total * ALARM_WINDOW_SEC / max(now_ts - base_ts, ALARM_WINDOW_SEC)
        reason = None
        if cap and window > max(0.4 * cap, ALARM_ABSOLUTE_BASE):
            reason = f"last 10 min ${window / 1e6:.2f} > 40% of cap (${0.4 * cap / 1e6:.2f})"
        elif window >= base and window > 3 * average:
            reason = f"last 10 min ${window / 1e6:.2f} > 3 times the average rate (${average / 1e6:.2f}/10 min)"
        if reason:
            pools.append({'scope': scope, 'unit': unit, 'reason': reason, 'window_usd': round(window / 1e6, 6)})
    if not pools:
        return None, None
    p = record.get('budget_progress') or {}
    try:
        record_ts = amount(p.get('ts') or 0) if isinstance(p, dict) else 0.0
        productive = p.get('reason') in PROGRESS_REASONS and bool(p.get('id')) and 0 <= now_ts - record_ts <= 1800
    except (ValueError, TypeError, AttributeError, BudgetDenied):
        record_ts, productive = 0.0, False
    warning = {'ts': now_ts, 'kind': 'speed-warning', 'reason': '; '.join(f"{h['scope']}: {h['reason']}" for h in pools),
             'pools': pools, 'cap_usd': round(cap / 1e6, 6),
             'progress': 'productive' if productive else 'unknown', 'evidence': p if productive else None, 'first_ts': now_ts, 'product': None}
    previous = record.get('spend_warning') or {}
    try:
        previous_ts, previous_first = amount(previous.get('ts') or 0), amount(previous.get('first_ts') or 0)
    except (BudgetDenied, AttributeError):
        previous_ts = previous_first = 0
    proceed = previous.get('kind') == 'speed-warning' and previous_first >= base_ts and 0 <= now_ts - previous_ts <= ALARM_WINDOW_SEC
    if proceed:
        warning['first_ts'] = previous_first
        warning['product'] = previous.get('product') if isinstance(previous.get('product'), str) else None
    if productive:
        return None, warning
    try:
        duration = amount(os.environ.get('CADRE_SPEND_ALARM_NO_PROGRESS_SEC', str(ALARM_PROGRESSLESS_SEC)))
    except BudgetDenied:
        duration = float(ALARM_PROGRESSLESS_SEC)
    if not proceed or warning['product'] is None:
        warning['product'] = product_id(record)
    call = con.execute("select count(*) from reservation where json_extract(metadata,'$.run_id')=? and actual is not null "
                        "and created >= ? and created <= ?", (run_id, warning['first_ts'], now_ts)).fetchone()[0]
    warning['call'] = call
    enrolled_step = warning['first_ts'] <= record_ts <= now_ts
    if duration > 0 and now_ts - warning['first_ts'] >= duration and call >= ALARM_PROGRESSLESS_CALL and not enrolled_step and warning['product'] is not None:
        current = product_id(record)
        if current is not None and current == warning['product']:
            warning['progress'] = 'none'
            reason = (f"no product change or recorded step during a {int(duration // 60)} min speed warning ({call} calls); {warning['reason']}")
            return f"spend alarm: awaiting approval ({reason})", {'ts': now_ts, 'kind': 'no-progress-spend', 'reason': reason, 'warning': warning}
    return None, warning


def run_cap_micro(home: Path, run_id: str | None) -> int | None:
    """P5: the run's WRITTEN USD cap (micro-dollars); no record/field -> None (no cap applied).
    This value is the run's contract and independent of role; use `role_cap` for role narrowing."""
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
        total = int((Decimal(str(amount(cap))) * 1_000_000).to_integral_value(rounding=ROUND_FLOOR))
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise BudgetDenied('run budget cap unreadable') from exc
    return total


def operator_cap(record: dict[str, Any]) -> int | None:
    try:
        values = [record[k] for k in ('operator_budget_usd', 'project_budget_cap_usd') if k in record]
        for key in ('request_budget', 'project_operator_budget'):
            if record.get(key):
                values.append(record[key]['amount_usd'])
        return min(int(Decimal(str(amount(value))) * 1_000_000) for value in values) if values else None
    except (ValueError, TypeError, ArithmeticError, KeyError) as exc:
        raise BudgetDenied('operator budget record unreadable') from exc


def budget_providers(provider: str) -> frozenset[str]:
    return frozenset({'opencode', 'openrouter'}) if provider in ('opencode', 'openrouter') else frozenset({provider}) if provider else USD_PROVIDERS


# HARD SPLIT: each role spends only from ITS OWN pocket, call order does not change the outcome (with a $5 cap
# lanes $3.75 · judge $0.75 · helper $0.50).
def lanes_finished(home: Path, run_id: str | None) -> bool:
    """Are all lanes in the plan terminal? A hand-off is safe only once the handing role's work is done;
    since the judge runs after QA, when the lanes are done the remaining share goes to the judge (B82)."""
    if not run_id:
        return False
    try:
        from contract import read as contract_read
        if (RunStore(home).path(run_id) / 'extra-lane.flag').exists():
            return False   # B226: G-R7 extra lanes (second analyst, reconciler) are part of the writer lane; no hand-off until they finish
        record = RunStore(home).read(run_id)
        contract = contract_read(home, run_id) or {}
        lanes = [s.get('id') for s in (contract.get('lanes') or []) if s.get('id')]
        if not lanes:
            return False
        tasks = record.get('tasks') or {}
        return all(isinstance(tasks.get(g), dict) and tasks[g].get('state') in ('produced', 'completed')
                   for g in lanes)
    except Exception:
        return False


def role_cap(home: Path, run_id: str | None, role: str | None = None, slice_name: str | None = None,
               provider: str = '', connection: sqlite3.Connection | None = None) -> int | None:
    """The part of the run cap open to this ROLE: each role sees only its own pocket, which fills with its own spending (hard split).
    Shares come from the run contract (lanes 75% · judge 15% · helper 10%); when the lanes finish the remaining share goes to the judge."""
    total = run_cap_micro(home, run_id)
    if total is None:
        return None
    enabled = role or os.environ.get('CADRE_ROLE') or ''
    try:
        from contract import task_slice, bases
        mine = slice_name or task_slice(enabled)
        shares = bases(home, run_id)
    except Exception:
        mine, shares = slice_name or 'lanes', {'lanes': 0.75, 'judge': 0.15, 'helper': 0.10}
    # Judge/helper take their percentage, lanes get the REMAINDER; the pockets add up to the full cap. The judge pocket is protected
    # by a $0.20 minimum and cannot exceed half the cap.
    helper_share = int(total * float(shares.get('helper', 0)))
    judge_ratio = float(shares.get('judge', JUDGE_PAY_RATIO))              # B110: the pocket comes from the contract
    assurance = 0
    if run_id:
        try:
            record = RunStore(home).read(run_id)
            floors = record.get('acceptance_assurance_bases_micro')
            pool = 'cash' if provider in ('opencode', 'openrouter') else provider
            assurance = max(0, int((floors.get(pool, 0) if provider and floors is not None else record.get('acceptance_assurance_base_micro')) or 0))
        except (OSError, ValueError, TypeError):
            raise BudgetDenied('acceptance reservation record unreadable')
    judge_base = min(total - helper_share, max(judge_reserve(total, judge_ratio), assurance))
    lane_share = max(0, total - judge_base - helper_share)   # lanes get the REMAINDER
    handover = lanes_finished(home, run_id)
    # Hand-off (B82): once all lanes are terminal the remaining share goes to the judge, the lane pocket is FROZEN
    # where it spent. Invariant: SUM(pockets) <= cap at all times.
    lane_spent = 0
    if handover:
        ledger = home / 'chain/request-budget.db'
        if ledger.exists():          # no ledger means no spending either
            try:
                with closing(sqlite3.connect(ledger.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)) as con:
                    settled, pending = run_usage(con, str(run_id), 'lanes', providers=budget_providers(provider), bounded=True)
                    lane_spent = settled + pending
            except (OSError, sqlite3.Error):
                handover = False        # if we cannot read it we do not hand off (fail-closed)
    if provider and connection is None and (home / 'chain/request-budget.db').exists():
        with closing(sqlite3.connect((home / 'chain/request-budget.db').resolve().as_uri() + '?mode=ro', uri=True, timeout=15)) as con:
            return role_cap(home, run_id, role, slice_name, provider, con)
    if provider and connection is not None and connection.execute("select 1 from sqlite_master where name='role_allocation'").fetchone():
        pool = 'cash' if provider in ('opencode', 'openrouter') else provider
        row = connection.execute('select total,caps from role_allocation where run_id=? and pool=?', (run_id, pool)).fetchone()
        if row:
            caps = json.loads(row[1])
            if row[0] != total or sum(caps.values()) != total or any(type(v) is not int or v < 0 for v in caps.values()):
                raise BudgetDenied('role allocation changed or unreadable')
            if handover:
                spent = sum(run_usage(connection, str(run_id), 'lanes', providers=budget_providers(provider), bounded=True))
                caps['judge'] += max(0, caps['lanes'] - spent)
                caps['lanes'] = min(caps['lanes'], spent)
            return caps[mine]
    if mine == 'judge':
        base = judge_base   # contract ratio, at least $0.20, at most half the cap
        if not handover:
            return base
        return max(base, total - min(lane_spent, lane_share) - helper_share)
    if mine == 'lanes':
        # Once the hand-off opened, the lane pocket freezes where it spent: its money went to the judge (the cap contract outranks cloud comfort).
        return min(lane_share, lane_spent) if handover else lane_share
    return max(0, helper_share)


def role_budget(home: Path, run_id: str | None, role: str | None = None, provider: str = '') -> tuple[int | None, str]:
    """(cap, slice): a role's pocket and the pocket's name come from one place; if they diverge the judge's cap is applied while its spending is charged to the lane pocket."""
    try:
        from contract import task_slice
        entry_name = task_slice(role or os.environ.get('CADRE_ROLE') or '')
    except Exception:
        entry_name = 'lanes'
    return role_cap(home, run_id, role, provider=provider), entry_name


def run_usage(con: sqlite3.Connection, run_id: str, slice: str | None = None, plan_assurance_excluded: bool = False,
              providers: frozenset[str] = USD_PROVIDERS, bounded: bool = False) -> tuple[int, int]:
    """(settled, pending) micro-dollars — this run's reservations across USD providers.
    If `slice` is given only that pocket is counted (hard split); old records without a pocket count as 'lanes'."""
    marks = ','.join('?' * len(providers))
    condition = "json_extract(metadata,'$.run_id')=? and provider in (%s)" % marks
    param: tuple = (run_id, *providers)
    if slice:
        condition += " and coalesce(json_extract(metadata,'$.slice'),'lanes')=?"
        param = (*param, slice)
    if plan_assurance_excluded:   # the plan reserve holds provider money, not the run pocket (it made the primary plan get denied)
        condition += " and not (coalesce(json_extract(metadata,'$.assurance'),0)=1 and json_extract(metadata,'$.assurance_role')='plan')"
    settled = con.execute(f"select coalesce(sum(actual),0) from reservation where {condition} and actual is not null", param).fetchone()[0]
    bound = "max(reserved,coalesce(json_extract(metadata,'$.worst'),reserved))" if bounded else 'reserved'
    held = con.execute(f"select coalesce(sum({bound}),0) from reservation where {condition} and actual is null", param).fetchone()[0]
    return int(settled), int(held)


def run_models(home: Path, run_id: str | None, roles: tuple[str, ...] = (), *, read_only_excluded: bool = False,
                   excluded_attempts: frozenset[str] | set[str] = frozenset()) -> set[str]:
    """Models called in this run (from ledger metadata; if `roles` is given, only those roles).
    For judge independence: the family of the model that wrote the product cannot be the judge (B110)."""
    db = Path(home) / 'chain/request-budget.db'
    if not run_id or not db.exists():
        return set()
    try:
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)) as con:
            rows = con.execute("select json_extract(metadata,'$.role'), json_extract(metadata,'$.phase_role'), "
                               "json_extract(metadata,'$.model'), json_extract(metadata,'$.attempt_id') "
                               "from reservation where json_extract(metadata,'$.run_id')=? "
                               "and coalesce(json_extract(metadata,'$.assurance'),0)!=1 "
                               # the direct HTTP helper (plan_ds) has no tools and cannot write to the product; it must not narrow the judge pool
                               "and (? = 0 or coalesce(json_extract(metadata,'$.adapter'),'') != 'direct')",
                               (run_id, int(read_only_excluded))).fetchall()
    except (OSError, sqlite3.Error) as exc:
        if read_only_excluded:
            raise BudgetDenied('writer accounting unavailable') from exc
        return set()
    readonly = {'plan', 'cadre-plan', 'scout', 'cadre-scout', 'compaction', 'autopsy'}
    # The OpenCode main agent also reports "build" during planning. Only open this generic label
    # with the engine's phase; sub-agents keep their own roles. Old records do not change.
    # `excluded_attempts`: receipts of judge calls PROVEN by an attempt record not to touch the product (§0a/10a).
    resolved = ((phase if role == 'build' and phase else role, model) for role, phase, model, attempt in rows
                if attempt not in excluded_attempts)
    return {str(model) for role, model in resolved if model and (not roles or str(role or 'impl') in roles)
            and (not read_only_excluded or role not in readonly)}


def run_spend_write(home: Path, ledger: 'BudgetLedger', run_id: str | None) -> None:
    """Structured spending in the run record: budget_spent_usd (settled) and budget_held_usd (pending). `cadre status` shows it."""
    if not run_id or not ledger.path.exists():
        return
    try:
        with closing(sqlite3.connect(ledger.path.resolve().as_uri()+'?mode=ro', uri=True, timeout=15)) as con:
            settled, held = run_usage(con, run_id)
            rows = con.execute("select provider, reserved, coalesce(actual,0), actual is null, created, metadata from reservation "
                               "where json_extract(metadata,'$.run_id')=? order by created", (run_id,)).fetchall()
        # B55: "how much money, at which step" from a single source — provider/task/role/model breakdown + unclosed reservations
        split_out: dict[tuple, dict] = {}
        pending_amount = []
        for prov, reserved, actual, pending, created, meta in rows:
            try:
                m = json.loads(meta or '{}')
            except ValueError:
                m = {}
            key = (prov, str(m.get('task') or m.get('task_id') or ''), str(m.get('role') or ''), str(m.get('model') or ''))
            a = split_out.setdefault(key, {'provider': prov, 'task': key[1], 'role': key[2], 'model': key[3], 'n': 0, 'reserved_usd': 0.0, 'actual_usd': 0.0, 'pending': 0})
            a['n'] += 1; a['reserved_usd'] += reserved / 1e6; a['actual_usd'] += actual / 1e6; a['pending'] += int(bool(pending))
            if pending:
                pending_amount.append({'provider': prov, 'role': key[2], 'model': key[3], 'reserved_usd': round(reserved / 1e6, 6), 'created': created})
        breakdown = [{**a, 'reserved_usd': round(a['reserved_usd'], 6), 'actual_usd': round(a['actual_usd'], 6)} for a in split_out.values()]
        RunStore(home).update(run_id, budget_spent_usd=round(settled / 1_000_000, 6), budget_held_usd=round(held / 1_000_000, 6),
                              budget_breakdown=breakdown, budget_unsettled=pending_amount[:20])
    except (OSError, sqlite3.Error, ValueError, KeyError):
        pass








def step_cost(provider: str, model: Any, cost: Any, tokens: Any) -> float:
    """B230/B280: OpenCode's `cost` may count cache reads as $0; some providers charge the full input
    price. Without a REAL cache discount in the catalogue a step is not counted below token x list price (max: no double counting)."""
    enrolled = amount(cost)
    if not model:
        return enrolled
    from model_catalog import price
    f = price(provider, str(model))
    if not f or 'input' not in f:
        return enrolled
    cache = float(f.get('cache_read', f['input']))
    if cache < float(f['input']) and enrolled > 0:   # real discount + record present: the provider's price is kept
        return enrolled
    try:
        t = json.loads(tokens) if isinstance(tokens, str) else (tokens or {})
        c = t.get('cache') or {}
        input = int(t.get('input') or 0) + int(c.get('write') or 0)
        read = int(c.get('read') or 0)
        output = int(t.get('output') or 0) + int(t.get('reasoning') or 0)
    except (TypeError, ValueError, AttributeError):
        return enrolled
    # B296: with a discounted cache, a zero record is not counted below token x (discounted) list price either
    return max(enrolled, (input * float(f['input']) + read * cache + output * float(f.get('output', 0) or 0)) / 1_000_000)


def progress_write(home: Path, run_id: str, reason: str, id: str) -> None:
    """G-B5/P2: re-reading the same QA/product does not renew the progress clock. N-BU06: lane output and plan acceptance are also recorded steps;
    cap extension (`cap_progress`) still looks only at QA PASS/acceptance evidence."""
    if reason not in PROGRESS_REASONS or not id:
        return
    store = RunStore(home)
    with FileLock(store.path(run_id) / '.record.lock'):
        r = store.read(run_id)
        seen = r.setdefault('budget_progress_seen', {})
        history = r.setdefault('budget_progress_history', [])
        identity = [reason, id]
        if seen.get(reason) == id or identity in history:
            return
        history.append(identity)
        seen[reason] = id
        r['budget_progress'] = {'ts': time.time(), 'reason': reason, 'id': id}
        atomic_json(store.path(run_id) / 'run.json', r)
    if r.get('project_ref_parent'):
        progress_write(home, r['project_ref_parent'], reason, run_id + ':' + id)


def cap_progress(home: Path, record: dict[str, Any], field: str = 'budget_cap_usd') -> dict[str, Any] | None:
    now = time.time()
    p = record.get('budget_progress') or {}
    used = [e.get('evidence') or {} for e in record.get('budget_extensions', []) if e.get('field') == field]
    def fresh(proof: dict[str, Any]) -> bool:
        return not any((old.get('reason'), old.get('id')) == (proof.get('reason'), proof.get('id')) for old in used)
    if p.get('reason') in ('qa-pass', 'acceptance') and p.get('id') and fresh(p) and 0 <= now - amount(p.get('ts', 0)) <= 1800:
        return p
    if record.get('project_active_run'):
        child = RunStore(home).read(record['project_active_run'])
        if child.get('project_ref_parent') == record['run_id']:
            p = cap_progress(home, child, field)
            if p:
                p = {**p, 'id': child['run_id'] + ':' + p['id']}
                if fresh(p):
                    return p
    return None


def cap_extend(home: Path, run_id: str, field: str) -> bool:
    """P2: healthy work extends by 50% each time it fills up; the provider share is a separate hard limit."""
    store = RunStore(home)
    with FileLock(store.path(run_id) / '.record.lock'):
        r = store.read(run_id)
        if field == 'project_budget_cap_usd' or r.get('budget_manual') or (field == 'budget_cap_usd' and 'budget_plan_cap_usd' not in r):
            return False
        evidence = cap_progress(home, r, field)
        cap = amount(r.get(field))
        if not evidence or cap <= 0 or cap * 1.5 * 1e6 >= 2**53:
            return False
        bound = operator_cap(r)
        fresh = min(math.ceil(cap * 1.5 * 1e6), bound if bound is not None else int(cap * 1e6)) / 1e6
        if fresh <= cap:
            return False
        r[field] = fresh
        r.setdefault('budget_extensions', []).append({'ts': time.time(), 'field': field, 'before': cap, 'after': fresh, 'evidence': evidence})
        atomic_json(store.path(run_id) / 'run.json', r)
        return True


RECEIPT_OVERRUN_SQL = ("from reservation where json_extract(metadata,'$.run_id')=? and actual > coalesce("
                   "json_extract(metadata,'$.overage_accepted_micro'), json_extract(metadata,'$.verified_bound_micro'), "
                   "json_extract(metadata,'$.worst'), reserved)")


def receipt_limit(con: sqlite3.Connection, run_ids: set[str]) -> None:
    """N-BU01/06: excess receipts cannot be bypassed by approval or by handing off to another paid route; only a recorded operator acceptance (FIX-17) reopens the family."""
    for rid in run_ids:
        if con.execute('select 1 ' + RECEIPT_OVERRUN_SQL + ' limit 1', (rid,)).fetchone():
            raise BudgetDenied('run budget exhausted: project receipt bound exceeded')


def receipt_overruns(con: sqlite3.Connection, run_ids: set[str]) -> list[dict[str, Any]]:
    """FIX-17 (N-BU06): unaccepted excess receipts; acceptance is recorded with `overage_accepted_micro` and does not grow the caps."""
    rows = []
    for rid in sorted(run_ids):
        for ident, provider, actual, reserved, meta, created in con.execute(
                'select id, provider, actual, reserved, metadata, created ' + RECEIPT_OVERRUN_SQL + ' order by created', (rid,)):
            try:
                m = json.loads(meta) if meta else {}
                bound = int(m.get('verified_bound_micro') or m.get('worst') or reserved)
            except (ValueError, TypeError, AttributeError):
                bound = reserved
            rows.append({'id': ident, 'provider': provider, 'run_id': rid, 'actual': actual, 'bound': bound, 'created': created})
    return rows


def receipt_acceptance(home: Path, ident: str, operator: str = 'operator') -> dict[str, Any]:
    """FIX-17 (N-BU06): an excess receipt reopens the family with a recorded operator acceptance; the real amount stays in the ledger, total/monthly caps do not grow."""
    if os.environ.get('CADRE_RUN_ID'):
        raise BudgetDenied('receipt acceptance is an operator command outside a run')
    ledger = BudgetLedger(home / 'chain/request-budget.db')
    with ledger.transaction() as con:
        row = con.execute('select provider, actual, reserved, metadata from reservation where id=?', (ident,)).fetchone()
    if row is None:
        raise BudgetDenied('unknown reservation')
    provider, actual, reserved, meta = row
    if actual is None:
        raise BudgetDenied('reservation has no receipt yet')
    try:
        m = json.loads(meta) if meta else {}
        bound = int(m.get('verified_bound_micro') or m.get('worst') or reserved)
    except (ValueError, TypeError, AttributeError) as exc:
        raise BudgetDenied('reservation metadata unreadable') from exc
    acceptance = {'id': ident, 'provider': provider, 'run_id': m.get('run_id'), 'actual': actual, 'bound': bound}
    if m.get('overage_accepted_micro') is not None and int(m['overage_accepted_micro']) >= actual:
        return {**acceptance, 'already': True}
    if actual <= bound:
        raise BudgetDenied('receipt within its verified bound')
    run_id = m.get('run_id')
    store = RunStore(home)
    if run_id and (store.path(run_id) / 'run.json').exists():
        from run_store import intervention_add
        with FileLock(store.path(run_id) / '.record.lock'):
            record = store.read(run_id)
            intervention_add(record, 'receipt-overage-accepted', {'reservation': ident, 'provider': provider,
                                                               'actual_usd': round(actual / 1e6, 6), 'bound_usd': round(bound / 1e6, 6)})
            atomic_json(store.path(run_id) / 'run.json', record)
    with ledger.transaction() as con:
        con.execute("update reservation set metadata=json_set(metadata,'$.overage_accepted_micro',?,'$.overage_accepted_ts',?,"
                    "'$.overage_accepted_by',?,'$.overage_bound_micro',?) where id=? and actual=?",
                    (actual, time.time(), operator, bound, ident, actual))
    _recovery_log(home, {'operation': 'receipt-acceptance', 'record': ident, 'provider': provider, 'run_id': run_id,
                         'actual_micro': actual, 'bound_micro': bound, 'operator': operator})
    return {**acceptance, 'already': False}


def dynamic_caps(home: Path, con: sqlite3.Connection, run_id: str, run_cap: int | None,
                    slice: str, maximum: int, extend: bool, provider: str = '', *, preview: bool = False) -> tuple[int | None, int | None]:
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
    receipt_limit(con, {run_id})
    ids, cap = operator_family(home, run_id)
    receipt_limit(con, ids)
    providers = budget_providers(provider)
    used_total = sum(sum(run_usage(con, rid, providers=providers, bounded=True)) for rid in ids)
    project_remaining = max(0, cap - used_total) if cap is not None else None
    extend = extend and (project_remaining is None or maximum <= project_remaining)
    if run_cap is not None and 'budget_plan_cap_usd' in r:
        used = sum(run_usage(con, run_id, slice, providers=budget_providers(provider), bounded=True))
        run_cap = role_cap(home, run_id, slice_name=slice)
        role_extend = extend and not preview and not (slice == 'lanes' and lanes_finished(home, run_id))
        for _ in range(128):
            if used + maximum <= run_cap or not role_extend or not cap_extend(home, run_id, 'budget_cap_usd'):
                break
            fresh = role_cap(home, run_id, slice_name=slice)
            if fresh <= run_cap:
                break
            run_cap = fresh
    allocation = con.execute("select 1 from sqlite_master where name='role_allocation'").fetchone()
    pool = 'cash' if provider in ('opencode', 'openrouter') else provider
    transferred = allocation and con.execute('select 1 from role_allocation where run_id=? and pool=?', (run_id, pool)).fetchone()
    pending = slice == 'helper' and r.get('plan_review_claim') and pending_review(home, r, store.plan_view(run_id))
    if (run_cap is not None and slice in ('lanes', 'judge', 'helper')
            and ('budget_plan_cap_usd' in r or 'budget_need_micro' in r or transferred or pending)):
        run_cap = role_cap(home, run_id, slice_name=slice, provider=provider, connection=con)
        used = sum(run_usage(con, run_id, slice, providers=budget_providers(provider), bounded=True))
        if extend and used + maximum > run_cap:
            run_cap = role_transfer(home, con, r, provider, slice, used + maximum, run_cap, preview=preview)
    return run_cap, project_remaining


def aile_candidate(store: RunStore, path: Path) -> dict[str, Any] | None:
    """FIX-28: an unrelated unreadable record does not block the family; member candidates are found only by run_id/project_ref_parent, members are read strictly."""
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
    return {'run_id': raw['run_id'], 'project_ref_parent': raw.get('project_ref_parent')}


def operator_family(home: Path, run_id: str) -> tuple[set[str], int | None]:
    store = RunStore(home)
    try:
        root = run_id
        record = store.read(root)
        seen = set()
        while record.get('project_ref_parent'):
            if root in seen:
                raise BudgetDenied('project budget ownership cycle')
            seen.add(root)
            root = record['project_ref_parent']
            record = store.read(root)
        cap = operator_cap(record)
        ids = set(record.get('project_run_ids') or []) | {root, run_id}
        records = [aile_candidate(store, p) for p in store.root.glob('*/run.json')]
        records = [c for c in records if c is not None]
        for _ in range(len(records) + 1):
            children = {c['run_id'] for c in records if c.get('project_ref_parent') in ids}
            if children <= ids:
                break
            ids.update(children)
        for rid in ids:
            store.read(rid)
        return ids, cap
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BudgetDenied('project budget ownership unavailable') from exc


def pending_review(home: Path, record: dict[str, Any], view: dict[str, Any]) -> bool:
    """Only the engine's owned review belonging to the current plan may take a share from the main job (G-B5)."""
    from run_store import TERMINAL, project_time_expired
    task = os.environ.get('CADRE_TASK_ID', '')
    claim = record.get('plan_review_claim') or {}
    return bool(re.fullmatch(r'plan-review-[1-9][0-9]*-[a-f0-9]{8}', task)
                and claim.get('task_id') == task and claim.get('revision') == view['revision']
                and task.startswith(f"plan-review-{view['revision']}-") and os.environ.get('CADRE_ROLE') == 'review'
                and view['revision'] > 0 and view['execution_plan_matches']
                and not (view['held'] and view['changes'][-1].get('status') == 'stop_requested')
                and record['state'] not in TERMINAL and not project_time_expired(record)
                and not (RunStore(home).path(record['run_id']) / 'cancel.flag').exists())


def role_transfer(home: Path, con: sqlite3.Connection, record: dict[str, Any], provider: str,
              slice: str, needed: int, cap: int, *, preview: bool = False) -> int:
    rid = record['run_id']
    if slice == 'lanes' and lanes_finished(home, rid):
        return cap
    proof = cap_progress(home, record)
    view = RunStore(home).plan_view(rid)
    review = (view['reviews'] or [{}])[-1]
    reviewed = (view['revision'] > 0 and review.get('status') == 'PASS'
                and review.get('revision_number') == view['revision'] and view['execution_plan_matches'] and not view['held'])
    pending = slice == 'helper' and pending_review(home, record, view)
    if not proof and not reviewed and not pending:
        return cap
    needs = dict(record.get('budget_need_micro') or {})
    if reviewed:
        needs['helper'] = 0
    if lanes_finished(home, rid):
        needs['lanes'] = 0
    pool = 'cash' if provider in ('opencode', 'openrouter') else provider
    floors = record.get('acceptance_assurance_bases_micro')
    floor = floors.get(pool, 0) if floors is not None else record.get('acceptance_assurance_base_micro', 0)
    if floor:
        needs['judge'] = max(needs.get('judge') or 0, int(floor))
    slices = ('lanes', 'judge', 'helper')
    caps = {s: role_cap(home, rid, slice_name=s, provider=provider, connection=con) for s in slices}
    if any(v is None for v in caps.values()):
        return cap
    if pending and not proof and not reviewed:
        from dispatch_util import validate_plan
        plan = view['history'][-1]['plan']
        validate_plan(plan)
        tasks = plan.get('tasks') or []
        estimates = [t.get('estimate_usd') for t in tasks]
        known = bool(estimates) and all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in estimates)
        if record.get('mode') == 'plan-only' or plan.get('kind') == 'verified-backlog':
            work_need = 0
        elif known:
            work_need = math.ceil(sum(estimates) * 1e6)
        else:
            return cap
        policy = review_rule(record, 'plan-review')
        if needed > run_cap_micro(home, rid) * policy['pay_percent'] // 100:
            return cap
        spent = sum(run_usage(con, rid, 'lanes', providers=budget_providers(provider), bounded=True))
        needs = {'lanes': spent + work_need, 'judge': caps['judge']}
    pool = 'cash' if provider in ('opencode', 'openrouter') else provider
    before = dict(caps)
    for donor in slices:
        if donor == slice or donor not in needs or needs[donor] is None:
            continue
        if type(needs[donor]) is not int or needs[donor] < 0:
            raise BudgetDenied('remaining role need unreadable')
        spent = sum(run_usage(con, rid, donor, providers=budget_providers(provider), bounded=True))
        floor = max(spent, needs[donor])
        delta = min(max(0, caps[donor] - floor), needed - caps[slice])
        caps[donor] -= delta
        caps[slice] += delta
        if caps[slice] >= needed:
            break
    if caps[slice] < needed:
        return cap
    total = run_cap_micro(home, rid)
    if sum(caps.values()) != total:
        raise BudgetDenied('role allocation total mismatch')
    if preview:
        return caps[slice]
    con.execute('insert into role_allocation values(?,?,?,?) on conflict(run_id,pool) do update set caps=excluded.caps',
                (rid, pool, total, json.dumps(caps)))
    con.execute('insert into role_transfer(run_id,pool,created,metadata) values(?,?,?,?)',
                (rid, pool, time.time(), json.dumps({'before': before, 'after': caps, 'slice': slice,
                 'reason': 'verified-progress' if proof else 'reviewed-plan' if reviewed else 'owned-plan-review',
                 'evidence': proof or {'revision': view['revision']}})))
    return caps[slice]


def calibration_receipt(receipt: Any, metadata: dict[str, Any]) -> bool:
    return (isinstance(receipt, str) and bool(receipt) and not receipt.startswith(('phantom:', 'recovery:'))
            and not metadata.get('assurance') and not metadata.get('observation'))


def review_phase(task: str, role: str) -> str | None:
    """The phase comes from the engine's task/role identity, not from the user body."""
    for phase, pattern, expected in (
            ('plan-review', r'plan-review-[1-9][0-9]*-[a-f0-9]{8}', 'review'),
            ('review-author', r'review-author-[a-f0-9]{12}', 'impl'),
            ('review-final', r'review-final-[a-f0-9]{12}', 'quality'),
            ('review-recheck', r'review-recheck-[a-f0-9]{12}', 'quality')):
        if role == expected and re.fullmatch(pattern, task):
            return phase
    return None


def review_rule(record: dict[str, Any], phase: str) -> dict[str, Any]:
    from limits import review_policy
    policy = record.get('review_limits', review_policy())
    try:
        rule = policy[phase]
        if (not isinstance(policy['version'], str) or not policy['version'] or len(policy['version']) > 100
                or any(type(rule[k]) is not int or rule[k] <= 0 for k in ('request', 'tool', 'duration_sec', 'pay_percent'))
                or rule['pay_percent'] > 100):
            raise ValueError('invalid review limits')
        return {'version': policy['version'], **rule}
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
        phase = review_phase(task, role)
        if not run_id or not phase:
            return None
        home = self.path.parent.parent
        record = RunStore(home).read(run_id)
        rule = review_rule(record, phase)
        total = operator_family(home, run_id)[1]
        local = run_cap_micro(home, run_id)
        if total is None or local is None:
            raise BudgetDenied('review total cap unavailable')
        limit = min(total, local) * rule['pay_percent'] // 100
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
            if not meta.get('assurance') and review_phase(str(meta.get('task_id') or ''), str(meta.get('phase_role') or '')) == phase:
                rows.append((provider, created, actual if actual is not None else max(reserved, meta.get('verified_bound_micro') or meta.get('worst') or reserved)))
        now = time.time()
        started, deadline, calls, tools = row[:4] if row else (
            min([now, *[r[1] for r in rows]]), 0, len(rows), 0)
        if not row:
            deadline = started + rule['duration_sec']
        if (row and row[4] != policy or clock and not row
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in (started, deadline, paused_total))
                or paused_total < 0 or any(type(v) is not int or v < 0 for v in (calls, tools))
                or deadline != started + rule['duration_sec'] + paused_total or calls < len(rows) or started > now
                or paused_at is not None and (type(paused_at) not in (int, float) or not math.isfinite(paused_at)
                                              or not started <= paused_at <= now or paused_at >= deadline)):
            raise BudgetDenied('review control changed or unreadable')
        if paused_at is not None:
            paused_total += now - paused_at
            deadline = started + rule['duration_sec'] + paused_total
            if create:
                con.execute('update review_control set deadline=? where run_id=? and phase=?', (deadline, run_id, phase))
                con.execute('update review_pause set paused_at=null,paused_total=? where run_id=? and phase=?',
                            (paused_total, run_id, phase))
        if create and not row:
            con.execute('insert into review_control values(?,?,?,?,?,?,?)',
                        (run_id, phase, started, deadline, calls, tools, policy))
        from run_store import request_remaining_sec, request_root_id
        store = RunStore(home)
        root = store.read(request_root_id(store, run_id))
        request_end = None
        if root.get('request') and (root.get('request_clock') or {}).get('start') is not None:
            remaining = request_remaining_sec(root, now)
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
                deadline = started + rule['duration_sec'] + paused_total
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
        if context['calls'] >= context['rule']['request']:
            raise BudgetDenied(f'review limit: {phase}: requests')
        # Cash and subscription units stay separate; all model/helper calls in the same phase are counted.
        used = sum(r[2] for r in context['rows'] if r[0] in budget_providers(provider))
        return max(0, context['limit'] - used) if provider in USD_PROVIDERS else None

    def begin_review(self, run_id: str | None, task: str, role: str) -> dict[str, Any] | None:
        if not run_id or not review_phase(task, role):
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
        phase = review_phase(task, role)
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
        if not run_id or not review_phase(task, role):
            return
        with self.transaction() as con:
            context = self.review_context(con, run_id, task, role, create=True)
            if context['tools'] >= context['rule']['tool']:
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
            return remaining + or_included_receipt(con, snapshot), settled, held
        if provider == 'opencode-go':
            # 4c-13: the pool is points to dollars; each pending line converts to points with its own model's limit, the total to this model's dollars.
            # 4c QA2: lines opened and settled AFTER the observation are not in the observation's Σ; they are subtracted from the wall too.
            from go_window import points, usd
            held = sum(points(m or '', r) for r, m in con.execute(
                "select reserved, json_extract(metadata,'$.model') from reservation where provider=? and actual is null",
                (provider,)))
            try:
                observation_ani = datetime.fromisoformat(str(snapshot['ts'])).timestamp()
            except (KeyError, TypeError, ValueError):
                observation_ani = time.time()
            then = sum(points(m or '', a) for a, m in con.execute(
                "select actual, json_extract(metadata,'$.model') from reservation where provider=? and actual is not null "
                "and coalesce(json_extract(metadata,'$.settled_ts'), created) > ?", (provider, observation_ani)))
            return remaining, math.ceil(round(usd(model or '', then), 6)), math.ceil(round(usd(model or '', held), 6))
        allocation = con.execute('select ceiling from allocation where provider=? and period=?',
                                 (provider, period)).fetchone()
        settled = con.execute('select coalesce(sum(actual),0) from reservation where provider=? and period=?',
                              (provider, period)).fetchone()[0]
        counted = base_provider(provider) in FREE or provider.endswith(FREE_SUFFIX)
        held = con.execute(
            'select coalesce(sum(reserved),0) from reservation where provider=? and actual is null'
            + (' and period=?' if counted else ''),
            (provider, period) if counted else (provider,)).fetchone()[0]
        ceiling = remaining + settled
        measured = measured_pay(snapshot, provider)
        if measured is not None:
            ceiling = min(ceiling, measured)
        elif allocation and counted:
            # Latch only in a COUNT bucket: raising the setting mid-day must not widen that day's allowance (B129 a).
            # The dollar bucket (Claude: weekly reserve curve) follows the observation both ways (B150: the latch was pinned to a falling cap).
            ceiling = min(ceiling, allocation[0])
        return ceiling, settled, held

    @staticmethod
    def assurance_share(con, provider, run_id, role=None, model=None):
        if not run_id or role not in ('quality', 'qa', 'plan'):   # 4b: the plan reserve is also handed to the real plan call
            return []
        rows = con.execute("select id,reserved,metadata from reservation where provider=? and actual is null "
                           "and json_extract(metadata,'$.run_id')=? and json_extract(metadata,'$.assurance')=1 "
                           "and json_extract(metadata,'$.assurance_role')=? order by created,id",
                           (provider, run_id, role)).fetchall()
        return [r for r in rows if model is None or json.loads(r[2]).get('model') == model]

    def assurance_leave(self, run_id, role=None, ids=None):
        with self.transaction() as con:
            for ident in [None] if ids is None else ids:
                con.execute("update reservation set actual=0,receipt='unused:'||id where actual is null "
                            "and json_extract(metadata,'$.run_id')=? and json_extract(metadata,'$.assurance')=1 "
                            "and (? is null or json_extract(metadata,'$.assurance_role')=?) and (? is null or id=?)",
                            (run_id, role, role, ident, ident))
        run_spend_write(self.path.parents[1], self, run_id)   # if the record does not see the refund, the status screen shows phantom pending money

    def assurance_carry(self, run_id: str, role: str, provider: str, maximum: int, snapshot: Any,
                    metadata: dict[str, Any], run_cap: int | None = None, slice: str | None = None,
                    once_leave: bool = False) -> dict[str, list[str]] | None:
        if (metadata.get('run_id') != run_id or metadata.get('assurance') is not True
                or metadata.get('assurance_role') != role or role not in ('quality', 'qa', 'plan')
                or not metadata.get('model') or type(maximum) is not int or not 0 < maximum < 2**53):
            raise BudgetDenied('invalid guarantee transfer')
        with self.transaction() as con:
            rows = con.execute(
                "select id,provider,reserved,metadata from reservation where actual is null "
                "and json_extract(metadata,'$.run_id')=? and json_extract(metadata,'$.assurance')=1 "
                "and json_extract(metadata,'$.assurance_role')=? order by created,id", (run_id, role)).fetchall()
            old = [row[0] for row in rows]
            if not old:
                return None
            if all(row[1] == provider and row[2] == maximum and json.loads(row[3]).get('model') == metadata['model']
                   and (slice is None or json.loads(row[3]).get('slice') == slice) for row in rows):
                result = {'old': old, 'fresh': old}
            else:
                if not once_leave:
                    con.execute('SAVEPOINT guarantee_transfer')
                for ident in old:
                    con.execute("update reservation set actual=0,receipt='unused:'||id where id=?", (ident,))
                if once_leave:
                    con.execute('SAVEPOINT guarantee_transfer')
                fresh: list[str] = []
                try:
                    for _ in old:
                        fresh.append(self.reserve(provider, maximum, snapshot, {**metadata, 'assurance_previous': old},
                                                 run_cap, slice, _connection=con))
                except BudgetDenied:
                    con.execute('ROLLBACK TO guarantee_transfer')
                    result = {'old': old, 'fresh': []} if once_leave else None
                else:
                    for ident in old:
                        con.execute("update reservation set metadata=json_set(metadata,'$.assurance_next',json(?)) where id=?",
                                    (json.dumps(fresh), ident))
                    result = {'old': old, 'fresh': fresh}
                con.execute('RELEASE guarantee_transfer')
        if result is not None:
            run_spend_write(self.path.parents[1], self, run_id)
        return result

    def available(self, provider: str, snapshot: Any, run_cap: int | None = None, run_id: str | None = None,
                  slice: str | None = None, required: int = 1, role: str | None = None, model: str | None = None,
                  read_only: bool = False) -> int:
        """Admission hint; the atomic decision is in reserve(). Holds the share of a pending call, not used to cut that call.
        `run_cap` (micro-dollars) is a role's pocket; spending is counted from that pocket with `slice`."""
        period, remaining = allowance(snapshot, provider, model=model)
        if not self.path.exists():
            measured = measured_pay(snapshot, provider)
            if measured is not None:
                remaining = min(remaining, measured)
            if run_cap is not None and provider in USD_PROVIDERS:
                root_cap = operator_family(self.path.parent.parent, run_id)[1] if run_id and (self.path.parent.parent / 'runs' / run_id / 'run.json').exists() else None
                if root_cap is not None:
                    with closing(sqlite3.connect(':memory:')) as con:
                        self.schema(con)
                        run_cap, root_cap = dynamic_caps(self.path.parent.parent, con, run_id, run_cap,
                                                             slice or 'lanes', required, remaining >= required, provider, preview=True)
                        review = self.review_context(con, run_id, os.environ.get('CADRE_TASK_ID', ''), role or os.environ.get('CADRE_ROLE', ''))
                        review_left = self.review_allowance(review, provider)
                        if review_left is not None:
                            remaining = min(remaining, review_left)
                return max(0, min(remaining, run_cap, root_cap if root_cap is not None else run_cap))
            return remaining
        try:
            connection = closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)) if read_only else self.transaction()
            with connection as con:
                if con.execute('select 1 from pricing_fault where provider=?', (provider,)).fetchone():
                    return 0
                review = self.review_context(con, run_id, os.environ.get('CADRE_TASK_ID', ''), role or os.environ.get('CADRE_ROLE', ''))
                review_left = self.review_allowance(review, provider)
                ceiling, settled, held = self.capacity(con, provider, snapshot, period, remaining, model)
                slots = self.assurance_share(con, provider, run_id, role)
                if provider == 'opencode-go':   # the guarantee may have been held with another Go model's dollars: converted via points
                    from go_window import points, usd
                    credit = int(round(usd(model or '', sum(points(json.loads(r[2]).get('model') or '', r[1]) for r in slots)), 6))
                else:
                    credit = sum(r[1] for r in slots)
                value = max(0, ceiling - settled - held + credit)
                if review_left is not None:
                    value = min(value, review_left)
                project_remaining = None
                if run_id and provider in USD_PROVIDERS:
                    run_cap, project_remaining = dynamic_caps(self.path.parent.parent, con, run_id, run_cap,
                                                                  slice or 'lanes', max(0, required - credit), value >= required, provider,
                                                                  preview=read_only)
                if run_cap is not None and run_id and provider in USD_PROVIDERS:
                    run_settled, run_held = run_usage(con, run_id, slice or 'lanes', plan_assurance_excluded=True,
                                                     providers=budget_providers(provider), bounded=True)
                    run_credit = 0 if slots and role == 'plan' else credit
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

    CALIBRATED_DAY, CALIBRATED_AT_LEAST, CALIBRATED_BASE = 7, 30, 0.1
    CALIBRATE_MIN_PROVIDERS = {'claude': 10}   # user approval: Claude could not reach 30 receipts (an allocation that does not fit = receipts that never accumulate)

    def calibrated(self, con: sqlite3.Connection, provider: str, maximum: int) -> int:
        """B198: the allocation is 2x the p95 of the provider's real/worst-case ratio over the last 7 days (at least 30 receipts,
        otherwise the worst case). The ratio is taken against the worst case (so feedback does not push it to 1); an overrun shows up in `settle` and stops the next job."""
        if maximum <= 1 or provider.endswith(':free'):
            return maximum
        ratios = []
        now = time.time()
        for reserved, actual, receipt, meta in con.execute(
                'select reserved, actual, receipt, metadata from reservation where provider=? and created>? and created<=? and actual>=0 and reserved>0',
                (provider, now - self.CALIBRATED_DAY * 86400, now)):
            try:
                metadata = json.loads(meta or '{}') or {}
                if not calibration_receipt(receipt, metadata):
                    continue
                worst = int(metadata.get('verified_bound_micro') or metadata.get('worst') or reserved)
            except (ValueError, TypeError, AttributeError):
                continue
            if worst > 0:
                ratios.append(actual / worst)
        if len(ratios) < self.CALIBRATE_MIN_PROVIDERS.get(provider, self.CALIBRATED_AT_LEAST):
            return maximum
        ratios.sort()
        p95 = ratios[min(len(ratios) - 1, int(0.95 * len(ratios)))]
        return max(1, min(maximum, math.ceil(maximum * max(self.CALIBRATED_BASE, 2 * p95))))

    def reserve(self, provider: str, maximum: int, snapshot: Any, metadata: dict[str, Any],
                run_cap: int | None = None, slice: str | None = None, observation: bool = False,
                *, _connection: sqlite3.Connection | None = None) -> str:
        """`observation=True` (B124): OBSERVATION MODE — spending is written to the ledger but no cap is APPLIED.
        For manual `opencode` use: when the user works from their own shell the gate does NOT BLOCK them,
        it only observes. Automated runs never use this mode; there the cap is binding."""
        # if `slice` is given it is stamped into the metadata HERE, so that the applied cap and the counted pocket come from the same
        # decision (see role_budget). If not given, the old behaviour: metadata or "lanes".
        if slice:
            metadata = {**metadata, "slice": slice}
        if observation:
            metadata = {**metadata, "observation": True}
        period, remaining = allowance(snapshot, provider, model=metadata.get('model'))
        # Aliases share an account; they cannot each spend a separate allowance.
        if type(maximum) is not int or not 0 < maximum < 2**53:
            raise BudgetDenied('invalid reservation')
        ident = uuid.uuid4().hex
        denied = False
        run_denied = ''
        alarm_record = None
        with self.transaction() if _connection is None else nullcontext(_connection) as con:
            if con.execute('select 1 from pricing_fault where provider=?', (provider,)).fetchone():
                raise BudgetDenied('observed usage exceeded its verified bound')
            worst = maximum
            review = None if metadata.get('assurance') else self.review_context(
                con, metadata.get('run_id'), str(metadata.get('task_id') or ''), str(metadata.get('phase_role') or ''), create=True)
            review_left = self.review_allowance(review, provider)
            if review_left is not None and worst > review_left:
                raise BudgetDenied(f"review limit: {review['phase']}: cost")
            metadata = {k: v for k, v in metadata.items() if k != 'review_phase'}
            if review:
                metadata['review_phase'] = review['phase']
            metadata = {**metadata, 'verified_bound_micro': worst}
            if not observation and not metadata.get('assurance'):
                maximum = self.calibrated(con, provider, maximum)
                if maximum < worst:
                    metadata = {**metadata, "worst": worst}
            ceiling, settled, held = self.capacity(con, provider, snapshot, period, remaining, metadata.get('model'))
            assurance_role = metadata.get('phase_role') or metadata.get('role')
            slots = [] if metadata.get('assurance') else self.assurance_share(
                con, provider, metadata.get('run_id'), assurance_role, metadata.get('model'))
            slot = slots[0] if slots else None
            credit = slot[1] if slot else 0
            project_remaining = None
            if not observation and provider in USD_PROVIDERS and metadata.get('run_id'):
                run_cap, project_remaining = dynamic_caps(self.path.parent.parent, con, str(metadata['run_id']),
                    run_cap, str(metadata.get('slice') or 'lanes'), max(0, worst - credit), settled + held + maximum - credit <= ceiling, provider)
            # P5: run-wide cap — this run's settled + pending total across USD providers cannot exceed the cap.
            if run_cap is not None and not observation and provider in USD_PROVIDERS and metadata.get('run_id'):
                run_settled, run_held = run_usage(con, str(metadata['run_id']), str(metadata.get('slice') or 'lanes'),
                                                  plan_assurance_excluded=True, providers=budget_providers(provider), bounded=True)
                run_credit = 0 if slot and assurance_role == 'plan' else credit   # the plan reserve was not counted to the pocket; the call is written in full to the pocket
                if run_settled + run_held + worst - run_credit > run_cap:  # B49: a reason with the amount — removes the "cap with no spending at all" misreading
                    run_denied = (f"run budget exhausted: reservation ${(worst - run_credit) / 1e6:.2f} > remaining "
                                  f"${max(0, run_cap - run_settled - run_held) / 1e6:.2f} (cap ${run_cap / 1e6:.2f}, settled ${run_settled / 1e6:.2f}, held ${run_held / 1e6:.2f}"
                                  + (f", calibrated ${maximum / 1e6:.2f})" if maximum < worst else ')'))
            if not run_denied and project_remaining is not None and worst - credit > project_remaining:
                run_denied = 'run budget exhausted: project total cap'
            if (not run_denied and not observation and provider in USD_PROVIDERS and metadata.get('run_id') and not metadata.get('assurance')
                    and os.environ.get('CADRE_SPEND_ALARM', '1') != '0'):
                run_denied, alarm_record = spend_alarm(self.path.parent.parent, con, str(metadata['run_id']))
                run_denied = run_denied or ''
            con.execute("""insert into allocation values(?,?,?) on conflict(provider,period)
                do update set ceiling=excluded.ceiling""", (provider, period, ceiling))
            # 07.10 16(b): OR does its own pre-check with the worst-case amount (max_tokens x price; 402 if insufficient). A request that passed with a small
            # calibrated reservation fell there and stayed pending: on OR admission is measured with the worst case, the allocated amount stays calibrated.
            needed = worst if provider == 'openrouter' else maximum
            if settled + held + needed - credit > ceiling and not observation:
                denied = True
            elif not run_denied or observation:
                if review:
                    con.execute('update review_control set calls=calls+1 where run_id=? and phase=?',
                                (metadata['run_id'], review['phase']))
                if slot:
                    con.execute("update reservation set actual=0,receipt='claimed:'||id where id=?", (slot[0],))
                    metadata = {**metadata, 'assurance_source': slot[0]}
                con.execute('insert into reservation values(?,?,?,?,?,null,null,?)',
                            (ident, provider, period, time.time(), maximum, json.dumps(metadata, allow_nan=False)))
        if alarm_record:   # the record is written outside the transaction: the record lock must not wait for the ledger lock
            field = 'spend_alarm' if alarm_record.get('kind') == 'no-progress-spend' else 'spend_warning'
            RunStore(self.path.parent.parent).update(str(metadata['run_id']), **{field: alarm_record})
        if denied:
            raise BudgetDenied('allocation exhausted or outcome unknown')
        if run_denied and not observation:
            raise BudgetDenied(run_denied)
        return ident

    def error_save(self, ident: str, error: dict[str, Any]) -> dict[str, Any] | None:
        with self.transaction() as con:
            row = con.execute("select provider, metadata from reservation where id=? and actual is null and json_valid(metadata)",
                              (ident,)).fetchone()
            if row is None:
                return None
            con.execute("update reservation set metadata=json_set(metadata,'$.provider_error',json(?)) where id=? and actual is null",
                        (json.dumps(error), ident))
            metadata = json.loads(row[1] or '{}') or {}
            endpoints = (metadata.get('endpoint_selection') or {}).get('order')
            endpoint = endpoints[0] if isinstance(endpoints, list) and len(endpoints) == 1 and isinstance(endpoints[0], str) and endpoints[0] else None
            return {'provider': row[0], 'model': str(metadata.get('model') or ''), 'endpoint': endpoint}

    def production_save(self, ident: str, generation: str) -> None:
        if not re.fullmatch(r'gen-[A-Za-z0-9_-]{1,128}', generation):
            return
        with self.transaction() as con:
            con.execute("update reservation set metadata=json_set(metadata,'$.generation',?) where id=? and actual is null "
                        "and json_valid(metadata) and json_extract(metadata,'$.generation') is null", (generation, ident))

    def settle(self, ident: str, actual: int, receipt: str, usage: Any = None) -> None:
        if type(actual) is not int or not 0 <= actual < 2**53 or not receipt:
            raise BudgetDenied('invalid receipt')
        usage = usage_clean(usage)
        with self.transaction() as con:
            row = con.execute('select reserved,actual,receipt,provider,metadata from reservation where id=?', (ident,)).fetchone()
            if row is None or (row[1] is not None and (row[1], row[2]) != (actual, receipt)):
                raise BudgetDenied('conflicting receipt')
            try:
                meta = json.loads(row[4] or '{}') or {}
                limit = int(meta.get('verified_bound_micro') or meta.get('worst') or row[0])
            except (ValueError, TypeError, AttributeError):
                limit = row[0]
            if actual > row[0]:
                # Never conceal an overrun. Its actual cost blocks subsequent work.
                con.execute('update reservation set reserved=? where id=?', (actual, ident))
                if actual > limit:   # B198: only exceeding the worst-case limit means the price model is wrong; exceeding the calibrated allocation is the expected tail
                    con.execute('insert or ignore into pricing_fault values(?)', (row[3],))
            con.execute("update reservation set actual=?,receipt=?,metadata=case when json_valid(metadata) then "
                        "json_set(metadata,'$.verified_bound_micro',?,'$.settled_ts',coalesce(json_extract(metadata,'$.settled_ts'),?)) else metadata end where id=?",
                        (actual, receipt, limit, time.time(), ident))
            if usage:
                con.execute("update reservation set metadata=json_set(metadata,'$.usage',json(?)) where id=? "
                            "and json_valid(metadata) and json_extract(metadata,'$.usage') is null",
                            (json.dumps(usage, allow_nan=False), ident))


USAGE_NUMBERS = ('input', 'output', 'thinking', 'cache_reading', 'cache_write', 'duration_ms')
USAGE_USD = ('fee_usd', 'cache_discount_usd', 'parent_fee_usd')
USAGE_TEXT = {'endpoint': 64, 'endpoint_label': 64, 'model_response': 128, 'end': 32, 'format': 16, 'price_source': 32}
USAGE_PATTERN = re.compile(r'[A-Za-z0-9 ._:/()+@-]+')


def usage_clean(value: Any) -> dict[str, Any] | None:
    """N-TS05 (CR-52): secret-free usage metadata of the receipt; unknown/invalid fields are dropped, missing fields are not written as zero."""
    if not isinstance(value, dict):
        return None
    clean: dict[str, Any] = {}
    for key in USAGE_NUMBERS:
        item = value.get(key)
        if type(item) is int and 0 <= item < 2**53:
            clean[key] = item
    for key in (*USAGE_USD, 'price_ts'):
        item = value.get(key)
        if type(item) in (int, float) and math.isfinite(item) and (item > 0 if key == 'price_ts' else item >= 0) and item < 1e12:
            clean[key] = item
    if type(value.get('byok')) is bool:
        clean['byok'] = value['byok']
    for key, limit in USAGE_TEXT.items():
        item = value.get(key)
        if isinstance(item, str) and 0 < len(item) <= limit and USAGE_PATTERN.fullmatch(item):
            clean[key] = item
    return clean or None


def renewal_share(snapshot: Any, provider: str) -> int | None:
    """The measured share of the period (dollar bucket: share with margin; Claude and count buckets: the allowance given by the observation). It grows only
    on renewal — spending and pending reservations do not grow this number."""
    measure = measured_pay(snapshot, provider)
    if measure is not None:
        return measure
    try:
        return allowance(snapshot, provider)[1]
    except BudgetDenied:
        return None


RENEWAL_RATIO, RENEWAL_BASE = 0.10, 50_000   # crumbs from a delayed API read do not count as a renewal


def is_renewed(row: dict[str, Any], snapshot: Any, provider: str) -> bool:
    old = row.get('pay')
    if not isinstance(old, int) or isinstance(old, bool):
        return False
    return (renewal_share(snapshot, provider) or 0) > old + max(RENEWAL_BASE, int(old * RENEWAL_RATIO))


class ProviderBans:
    """Task-owned bans: budget renewal and provider cooldown are separate; the task lease is the writer lock.
    Routing only reads; a reservation with unknown outcome also binds the renewed quota."""
    def __init__(self, path: Path, home: Path):
        self.path = path
        self.ledger = BudgetLedger(home/'chain/request-budget.db')

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

    def blocked(self, slice: str | None = None) -> set[str]:
        """B110: run-cap BANS carried no pocket information; a ban written when the lanes' pocket ran out
        also closed the JUDGE — the exact opposite of the hard split (each role only its own pocket). A pocket-stamped ban binds only
        that pocket; a ban without a pocket (provider error / daily share) binds everyone."""
        data = self.read()
        if slice is None:
            return set(data)
        return {entry_name for entry_name, line in data.items()
                if not line.get('slice') or line.get('slice') == slice}

    def block(self, provider: str, reason: str, snapshot: Any, retry_after: float = 60,
              slice: str | None = None) -> None:
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
                          **({'slice': slice} if slice else {}),
                          **({'pay': pay} if reason == 'budget' and (pay := renewal_share(snapshot, provider)) is not None else {})}
        atomic_json(self.path, data)

    def refresh(self, snapshot: Any) -> None:
        data = self.read()
        updated = dict(data)
        for provider, row in data.items():
            try:
                period, _ = allowance(snapshot, provider)
                # B286/G-B3: within the same period it is re-checked only if the share is RENEWED; receipt/reservation jitter does not reopen it (C14).
                if row.get('reason') == 'budget' and row.get('period') == period and not is_renewed(row, snapshot, provider):
                    # a pocket-stamped (slice) cap ban must also lift when the run cap rises; after recheck_at
                    # the provider share is checked again, the pocket is re-measured at the gate (the ban is refreshed if empty). The provider-share ban is the old rule.
                    if not (row.get('slice') and time.time() >= amount(row.get('recheck_at', 0))):
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
    path = RunStore(home).task_dir(run, os.environ.get('CADRE_TASK_ID') or 'main')/'budget-bans.json'
    return ProviderBans(path, home)


def available_budget(home: Path, snapshot: Any, provider: str,
                     role: str | None = None, run_id: str | None = None, required: int = 1, model: str | None = None,
                     read_only: bool = False) -> int:
    """B81: `role`/`run_id` may be given EXPLICITLY. The engine's own os.environ has no CADRE_ROLE (the role is only
    written into the child environment), so when the caller did not know, everyone saw the LANE pocket."""
    bans = task_bans(home)
    run_id = run_id or os.environ.get('CADRE_RUN_ID')
    cap, slice = role_budget(home, run_id, role, provider)
    # B110: the ban list is read with the ASKING ROLE's pocket — another pocket running out does not close this role.
    if bans and base_provider(provider) in bans.blocked(slice):
        row = bans.read().get(base_provider(provider), {})
        r = RunStore(home).read(run_id) if run_id else {}
        if not (row.get('slice') and not r.get('budget_manual') and 'budget_plan_cap_usd' in r and cap_progress(home, r)):
            return 0
    return BudgetLedger(home/'chain/request-budget.db').available(provider, snapshot, cap, run_id, slice, required,
                                                                   role or os.environ.get('CADRE_ROLE'), model, read_only)


def run_remaining_usd(home: Path, run_id: str | None, role: str | None = None, provider: str = '') -> float | None:
    """Remaining USD share of this ROLE (pocket − settled − pending); None without a cap.
    The engine's view of 'is there money' and the limit the gate applies must come from the same pocket."""
    cap, slice_name = role_budget(home, run_id, role, provider)
    if cap is None or not run_id:
        return None
    ledger = BudgetLedger(home/'chain/request-budget.db')
    settled = held = 0
    if ledger.path.exists():
        try:
            with closing(sqlite3.connect(ledger.path.resolve().as_uri()+'?mode=ro', uri=True, timeout=15)) as con:
                settled, held = run_usage(con, run_id, slice_name, plan_assurance_excluded=True,
                                           providers=budget_providers(provider), bounded=True)
                enabled = role or os.environ.get('CADRE_ROLE')
                if enabled in ('quality', 'qa'):
                    providers = budget_providers(provider)
                    marks = ','.join('?' for _ in providers)
                    credit = con.execute("select coalesce(sum(reserved),0) from reservation where actual is null "
                        "and json_extract(metadata,'$.run_id')=? and json_extract(metadata,'$.assurance')=1 "
                        f"and json_extract(metadata,'$.assurance_role')=? and provider in ({marks})", (run_id, enabled, *providers)).fetchone()[0]
                    held -= credit
                else:
                    credit = 0
                if provider:
                    cap, remaining = dynamic_caps(home, con, run_id, cap, slice_name, 0, False, provider)
                    if remaining is not None:
                        return max(0.0, min(cap - settled - held, remaining + credit) / 1e6)
        except (OSError, sqlite3.Error):
            return 0.0
    return max(0.0, (cap - settled - held) / 1_000_000)


def observed_budget(home: Path, code: Path) -> Any:
    """One cached observation per minute, shared by all executors and helpers."""
    target = home/'chain/request-budget.json'
    with FileLock(target.with_suffix('.lock')):
        try:
            saved = json.loads(target.read_text())
            age = time.time() - float(saved['observed'])
            # B327: after midnight yesterday's observation counts as 'stale budget'; if the day changed, measure again
            if 0 <= age < 60 and (saved['budget'] or {}).get('today', datetime.now().strftime('%Y-%m-%d')) == datetime.now().strftime('%Y-%m-%d'):
                return saved['budget']
        except (OSError, ValueError, KeyError, TypeError):
            pass
        observed = time.time()  # Observation must not postdate its source query.
        try:
            result = subprocess.run([sys.executable, str(code/'lib/provider_budget.py')], capture_output=True, text=True, timeout=30, check=True)
            budget = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            budget = None
        atomic_json(target, {'observed':observed, 'budget':budget})
        return budget


# ---- Recovery path (Item 0a) --------------------------------------------
# Closing does NOT silently give money back: by default the reserved amount counts as SPENT; giving money back is a separate and
# deliberate decision (--zero). Both actions require --yes and are written to recovery.log.

def _recovery_log(home: Path, record: dict[str, Any]) -> None:
    path = home / 'chain/recovery.log'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps({'ts': time.time(), **record}, ensure_ascii=False) + '\n')


def stale_reservations(con: sqlite3.Connection, age_hour: float) -> list[tuple]:
    threshold = time.time() - age_hour * 3600
    return con.execute('select id,provider,created,reserved,metadata from reservation '
                       "where actual is null and coalesce(json_extract(metadata,'$.assurance'),0)!=1 and created < ? order by created", (threshold,)).fetchall()


def ownership_status(home: Path, run_id: str | None) -> str:
    if not run_id:
        return 'legacy'
    from run_store import active_runs, process_identity, process_command
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
        command = process_command(pid)
        if not command:
            return 'unknown'
        import re
        match = re.search(r'(?:^|[/\s])run_store\.py\s+(run|execute)\s+(\S+)(?:\s|$)', command)
        owned = bool(record.get('owner_identity')) and current == record.get('owner_identity')
        if match and ((match.group(1) == 'run' and match.group(2) == run_id)
                      or (match.group(1) == 'execute' and owned and match.group(2) == record.get('project'))):
            return 'live' if locked and current else 'unknown'
        if match or 'run_store.py' not in command:
            return 'unknown' if locked else 'dead'
        return 'unknown'
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return 'unknown'


def is_owner_dead(home: Path, run_id: str | None) -> bool:
    return ownership_status(home, run_id) == 'dead'


def is_owner_live(home: Path, run_id: str | None) -> bool:
    return ownership_status(home, run_id) in ('live', 'unknown')


@contextmanager
def recovery_lock(home: Path, run_id: str | None) -> Iterator[bool]:
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


def _number_environment(entry_name: str, fallback: float) -> float:
    """B114-10: a module-level `float(os.environ[...])` blew up the IMPORT on a bad value —
    a shell that set `CADRE_PHANTOM_AGE_MIN=30dk` stopped the whole engine with 'could not convert string to float',
    and the message did not even name the variable. Now a bad value falls back to the default."""
    try:
        return float(os.environ.get(entry_name, fallback))
    except (TypeError, ValueError):
        return float(fallback)


PHANTOM_AGE_MIN = _number_environment('CADRE_PHANTOM_AGE_MIN', 30)


def opencode_receipt(home: Path, con: sqlite3.Connection, ident: str, provider: str,
                    created: float, meta: dict[str, Any]) -> tuple[int, str] | None:
    """G-B2/P4: only the receipt of a single, completed session recorded for this run is used."""
    if provider not in PAID or not meta.get('session_id') or not meta.get('model'):
        return None
    try:
        from session import session_ids
        if meta['session_id'] not in session_ids(RunStore(home).read(meta['run_id'])):
            return None
        next = con.execute("""select min(created) from reservation where id != ? and created >= ?
            and provider=? and json_extract(metadata,'$.session_id')=?
            and json_extract(metadata,'$.model')=?""", (ident, created, provider, meta['session_id'], meta['model'])).fetchone()[0]
        last = min(created + 1800, next if next is not None else created + 1800)
        db = Path(os.environ.get('OPENCODE_DB', str(Path.home() / '.local/share/opencode/opencode.db'))).expanduser()
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as oc:
            rows = oc.execute("""select id,data from message where session_id=? and time_created >= ? and time_created < ?
                and json_extract(data,'$.role')='assistant' and json_extract(data,'$.providerID')=?
                and json_extract(data,'$.modelID')=? and json_extract(data,'$.time.completed') >= ?
                and json_extract(data,'$.time.completed') < ?""",
                (meta['session_id'], (created - 5) * 1000, last * 1000, provider, meta['model'], created * 1000, last * 1000)).fetchall()
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
            from model_catalog import model
            f = (model(provider, meta['model']) or {})['cost']
            tiers = [f] + ([f['context_over_200k']] if f.get('context_over_200k') else [])
            p = {'freshRate': max(amount(c['input']) for c in tiers), 'outRate': max(amount(c['output']) for c in tiers),
                 'readRate': max(amount((c.get('cache') or {}).get('read', c.get('cache_read', c['input']))) for c in tiers),
                 'writeRate': max(amount((c.get('cache') or {}).get('write', c.get('cache_write')) or c['input']) for c in tiers)}
        p = {**p, 'writeRate': p.get('writeRate') or p['freshRate']}
        rates = [amount(p[k]) for k in ('freshRate', 'outRate', 'readRate', 'writeRate')]
        if rates[0] <= 0 or rates[1] <= 0:
            return None
        from go_window import moment_factor
        multiplier = moment_factor(meta['model'], created)   # Go DeepSeek costs 2× points during peak hours (4c-13)
        factor = amount(meta.get('factor', 1))   # 4c-13: the coefficient was removed; old Go lines carry their own coefficient
        if factor < 1:
            return None
        cost = counts[0] * rates[0] + (counts[1] + counts[2]) * rates[1] + counts[3] * rates[2] + counts[4] * rates[3]
        return math.ceil(cost * multiplier * factor), 'opencode:' + mid
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, AttributeError, OverflowError, BudgetDenied):
        return None


def phantom_close(home: Path, age_min: float | None = None) -> dict[str, Any]:
    """B85: pending reservations whose owner died and that are older than `age_min` close automatically (counted as spent, reversible).
    The pending amount is a safe upper bound; a single crash locked half the cap with unspent money."""
    age = PHANTOM_AGE_MIN if age_min is None else float(age_min)
    ledger = BudgetLedger(home / 'chain/request-budget.db')
    if not ledger.path.exists():
        return {'closed': 0, 'amount_micro': 0, 'runs': []}
    with ledger.transaction() as con:
        lines = stale_reservations(con, age / 60.0)
    closed = 0
    amount = 0
    monthly = monthly_recovery_complete(home)
    runs: set[str] = set()
    uncertain: set[str] = set()
    for ident, _provider, _formation, reserved, meta in lines:
        m = json.loads(meta) if meta else {}
        run_id = m.get('run_id')
        if not is_owner_dead(home, run_id):
            if ownership_status(home, run_id) == 'unknown':
                uncertain.add(str(run_id))
            continue
        try:
            # The same message must not be used by two concurrent recoveries (G-B2).
            with recovery_lock(home, run_id) as safe, FileLock(home / 'chain/phantom-receipt.lock'):
                if not safe:
                    uncertain.add(str(run_id))
                    continue
                with ledger.transaction() as con:
                    receipt_row = opencode_receipt(home, con, ident, _provider, _formation, m)
                actual, receipt = receipt_row or (recovery_amount(reserved, m), 'phantom:' + ident)
                ledger.settle(ident, actual, receipt)
        except BudgetDenied:
            continue
        closed += 1
        amount += actual
        monthly += monthly_pair_close(home, _provider, _formation, m, actual)
        if run_id:
            runs.add(str(run_id))
    if closed:
        _recovery_log(home, {'operation': 'phantom-close', 'age_min': age, 'closed': closed, 'monthly': monthly,
                             'amount_micro': amount, 'runs': sorted(runs)})
    if uncertain:
        _recovery_log(home, {'operation': 'ownership-uncertain', 'runs': sorted(uncertain)})
    return {'closed': closed, 'amount_micro': amount, 'runs': sorted(runs), 'monthly': monthly,
            'uncertain_runs': sorted(uncertain)}


def daily_reconciliation(home: Path, snapshot: dict[str, Any]) -> str:
    """P4: daily observation with named periods; missing API data does not count as zero."""
    now = datetime.now()
    target = home / 'chain' / f'reconciliation-{now.date().isoformat()}.json'
    with FileLock(target.with_suffix('.lock')):
        if target.exists():
            return ''
        db = home / 'chain/request-budget.db'
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as con:
            rows = con.execute('select provider, sum(actual), sum(case when actual is null then reserved else 0 end) '
                               'from reservation where created >= ? group by provider',
                               (now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp(),)).fetchall()
        go, zen = (snapshot.get(k) or {} for k in ('go', 'zen'))
        data = {'ts': now.isoformat(), 'observation_ts': snapshot.get('ts'),
                'ledger_today_usd': {p: {'spent': (a or 0) / 1e6, 'pending': h / 1e6} for p, a, h in rows if p in PAID},
                'go_month_points': go.get('month_points') if go.get('db') == 'ok' else None,
                'go_source': go.get('source'),
                'go_period_start': go.get('period_start'),
                'zen_spent_after_entry_usd': zen.get('spent') if zen.get('db') == 'ok' else None,
                'zen_entry_date': zen.get('entry_date'),
                'zen_remaining_usd': zen.get('remaining') if zen.get('db') == 'ok' else None}
        atomic_json(target, data)
        return 'reconciliation: ' + json.dumps(data, ensure_ascii=False)


MONTHLY_EQUIV_WINDOW_SEC = 600


def recovery_amount(reserved: int, metadata: dict[str, Any]) -> int:
    values = [reserved, *(metadata[k] for k in ('worst', 'verified_bound_micro') if metadata.get(k) is not None)]
    if any(type(v) is not int or not 0 <= v < 2**53 for v in values):
        raise BudgetDenied('recovery liability bound unreadable')
    return max(values)


def monthly_recovery_complete(home: Path) -> int:
    path = home / 'chain/request-budget.db'
    if not path.exists():
        return 0
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)) as con:
        rows = con.execute("select provider,created,metadata,actual from reservation where actual is not null "
                           "and provider in ('opencode','openrouter') and (json_extract(metadata,'$.monthly_id') is not null "
                           "or json_extract(metadata,'$.session_id') is not null)").fetchall()
    def key(provider: str, metadata: dict[str, Any]) -> str:
        if metadata.get('monthly_id'):
            return 'id:' + str(metadata['monthly_id'])
        return json.dumps([provider, metadata.get('session_id'), metadata.get('model'), metadata.get('role')])
    counts: dict[str, int] = {}
    for provider, _, raw, _ in rows:
        ident = key(provider, json.loads(raw))
        counts[ident] = counts.get(ident, 0) + 1
    repaired = 0
    for provider, created, raw, actual in rows:
        metadata = json.loads(raw)
        if counts[key(provider, metadata)] == 1:
            repaired += monthly_pair_close(home, provider, created, metadata, actual)
    if repaired:
        _recovery_log(home, {'operation': 'monthly-pair-complete', 'closed': repaired})
    return repaired


def monthly_pair_close(home: Path, provider: str, formation: float, meta: dict[str, Any], amount: int | None) -> int:
    path = home / 'chain/monthly-budget.db'
    id, session, model, role = meta.get('monthly_id'), meta.get('session_id'), meta.get('model'), meta.get('role')
    if not path.exists() or not (id or session):
        return 0
    from monthly_budget import MonthlyLedger, MonthlyBudgetDenied
    monthly = MonthlyLedger(path, cap=1)
    with monthly.transaction() as con:
        if id:
            line = con.execute('select id, reserved from reservation where id=? and actual is null and provider=? '
                                "and (? is null or json_extract(metadata,'$.session_id')=?) "
                                "and (? is null or json_extract(metadata,'$.model')=?) "
                                "and (? is null or json_extract(metadata,'$.role')=?)",
                                (str(id), provider, session, session, model, model, role, role)).fetchone()
        else:
            matches = con.execute("select id, reserved from reservation where actual is null and provider=? and created between ? and ? "
                                "and json_extract(metadata,'$.session_id')=? and (? is null or json_extract(metadata,'$.model')=?) "
                                "and (? is null or json_extract(metadata,'$.role')=?)",
                                (provider, formation - MONTHLY_EQUIV_WINDOW_SEC, formation + 5, session, model, model, role, role)).fetchall()
            line = matches[0] if len(matches) == 1 else None
    if not line:
        return 0
    try:
        monthly.settle(line[0], int(line[1]) if amount is None else int(amount), 'recovery:' + line[0])
    except MonthlyBudgetDenied:
        return 0
    return 1


ESTIMATED_PREFIXES = ('phantom:', 'recovery:')


def _monthly_pair_fix(home: Path, meta: dict[str, Any], actual: int, generation: str, apply: bool) -> str:
    id = meta.get('monthly_id')
    path = home / 'chain/monthly-budget.db'
    if not id or not path.exists():
        return 'absent'
    from monthly_budget import MonthlyLedger
    fresh = f'{id}:{generation}'
    with MonthlyLedger(path, cap=1).transaction() as con:
        line = con.execute('select actual, receipt from reservation where id=?', (str(id),)).fetchone()
        if line is None:
            return 'absent'
        if tuple(line) == (actual, fresh):
            return 'already'
        if line[0] is not None and not str(line[1] or '').startswith(ESTIMATED_PREFIXES):
            return 'contradicts'
        if apply:
            con.execute('update reservation set reserved=max(reserved, ?), actual=?, receipt=? where id=?', (actual, actual, fresh, str(id)))
        return 'fixed'


def estimated_closing_fix(home: Path, ident: str, generation: str, actual: int, rationale: str) -> dict[str, Any]:
    if (not isinstance(ident, str) or not re.fullmatch(r'gen-[A-Za-z0-9_-]{1,128}', str(generation or ''))
            or type(actual) is not int or not 0 <= actual < 2**53):
        return {'status': 'invalid'}
    ledger = BudgetLedger(home / 'chain/request-budget.db')
    receipt = f'{ident}:{generation}'
    with FileLock(home / 'chain/phantom-receipt.lock'):
        with ledger.transaction() as con:
            line = con.execute('select reserved, actual, receipt, metadata from reservation where id=?', (ident,)).fetchone()
        if line is None:
            return {'status': 'absent'}
        reserved, previous, previous_receipt, meta = line[0], line[1], line[2], json.loads(line[3] or '{}') or {}
        if meta.get('generation') != generation:
            return {'status': 'id-mismatch'}
        if (previous, previous_receipt) == (actual, receipt):
            return {'status': 'previously-fixed'}
        if previous is None or not str(previous_receipt or '').startswith(ESTIMATED_PREFIXES):
            return {'status': 'estimated-not'}
        if actual > recovery_amount(reserved, meta):
            return {'status': 'parent-limit-exceeds'}
        if _monthly_pair_fix(home, meta, actual, generation, apply=False) == 'contradicts':
            return {'status': 'monthly-pair-contradicts'}
        monthly = _monthly_pair_fix(home, meta, actual, generation, apply=True)
        trace = {'previous_actual': previous, 'previous_receipt': previous_receipt, 'rationale': str(rationale or '')[:200], 'ts': time.time()}
        with ledger.transaction() as con:
            if tuple(con.execute('select actual, receipt from reservation where id=?', (ident,)).fetchone()) != (previous, previous_receipt):
                return {'status': 'concurrent-changed'}
            con.execute("update reservation set actual=?, receipt=?, metadata=json_set(metadata,'$.estimated_correction',json(?)) where id=?",
                        (actual, receipt, json.dumps(trace), ident))
    _recovery_log(home, {'operation': 'estimated-fix', 'id': ident, 'previous_micro': previous, 'fresh_micro': actual, 'monthly': monthly})
    return {'status': 'fixed', 'previous_actual': previous, 'actual': actual, 'monthly': monthly}


def recovery(home: Path, argv: list[str]) -> int:
    ledger = BudgetLedger(home / 'chain/request-budget.db')
    command = argv[0]
    yes = '--yes' in argv
    zero = '--zero' in argv
    age = 6.0
    if '--hour' in argv:
        try:
            age = float(argv[argv.index('--hour') + 1])
        except (IndexError, ValueError):
            print('--hour needs a number', file=sys.stderr)
            return 2
    if age < 0:
        print('--hour cannot be negative', file=sys.stderr)
        return 2

    if command in ('--stale-list', '--stale-close'):
        if command == '--stale-close' and yes:
            monthly_recovery_complete(home)
        with ledger.transaction() as con:
            lines = stale_reservations(con, age)
        if not lines:
            print('No pending reservation older than %g hours.' % age)
            return 0
        total = sum(r[3] for r in lines)
        for ident, provider, formation, reserved, meta in lines:
            m = json.loads(meta) if meta else {}
            ownership = ownership_status(home, m.get('run_id'))
            print('%s %-12s $%8.4f %6.1fs once run=%s task=%s%s' % (
                ident[:8], provider, reserved / 1e6, (time.time() - formation) / 3600,
                m.get('run_id') or '-', m.get('task_id') or '-',
                '  [LIVE RUN — cannot close]' if ownership == 'live' else
                '  [OWNERSHIP UNCERTAIN — preserved]' if ownership == 'unknown' else ''))
        print('total %d reservation, $%.4f pending' % (len(lines), total / 1e6))
        if command == '--stale-list':
            return 0
        if not yes:
            print('Add --yes to close. Fallback: the reserved amount is COUNTED AS SPENT (no capacity released).\n'
                  'Add --zero if you know the calls never happened (funds are released).', file=sys.stderr)
            return 2
        closed = skipped = live = monthly = 0
        uncertain = set()
        for ident, provider, _formation, reserved, _meta in lines:
            _m = json.loads(_meta) if _meta else {}
            ownership = ownership_status(home, _m.get('run_id'))
            if ownership == 'unknown':
                uncertain.add(str(_m.get('run_id')))
                continue
            if ownership == 'live':
                live += 1        # the receipt of an in-flight call must not be lost (B110)
                continue
            try:
                real = 0 if zero else recovery_amount(reserved, _m)
                with recovery_lock(home, _m.get('run_id')) as safe:
                    if not safe:
                        uncertain.add(str(_m.get('run_id')))
                        continue
                    ledger.settle(ident, real, 'recovery:' + ident)
                    closed += 1
                    monthly += monthly_pair_close(home, provider, _formation, _m, real)
            except BudgetDenied:
                skipped += 1
        _recovery_log(home, {'operation': 'stale-close', 'zero': zero, 'age_hour': age,
                             'closed': closed, 'skipped': skipped, 'live': live, 'uncertain_runs': sorted(uncertain), 'amount_micro': total})
        if uncertain:
            _recovery_log(home, {'operation': 'ownership-uncertain', 'runs': sorted(uncertain)})
            print('Ownership of %d runs is uncertain; their reservations are preserved.' % len(uncertain))
        print('%d reservations closed (%s), %d skipped, %d live runs untouched; %d matching entries closed in the monthly ledger.' % (
            closed, 'reset, funds released' if zero else 'reserved amount counted as spent', skipped, live, monthly))
        return 0

    if command in ('--phantom-list', '--phantom-go-back'):
        with ledger.transaction() as con:
            lines = con.execute("select id,provider,created,reserved,actual,metadata from reservation "
                                   "where receipt like 'phantom:%' and actual > 0 order by created").fetchall()
        if not lines:
            print('No automatically closed (phantom) reservations.')
            return 0
        total = sum(r[4] for r in lines)
        for ident, provider, formation, _resv, real, meta in lines:
            m = json.loads(meta) if meta else {}
            print('%s %-12s $%8.4f %6.1fs once run=%s' % (
                ident[:8], provider, real / 1e6, (time.time() - formation) / 3600, m.get('run_id') or '-'))
        print('total %d record, $%.4f COUNTED as spent (may have actually been spent)' % (len(lines), total / 1e6))
        if command == '--phantom-list':
            return 0
        if not yes:
            print('Add --yes to roll back. CHECK THE PROVIDER PANEL FIRST: if it shows this amount,\n'
                  'rolling back would UNDERSTATE actual spending in our ledger.', file=sys.stderr)
            return 2
        with ledger.transaction() as con:
            con.execute("update reservation set actual=0 where receipt like 'phantom:%' and actual > 0")
        _recovery_log(home, {'operation': 'phantom-go-back', 'record': len(lines), 'amount_micro': total})
        print('%d records rolled back, $%.4f capacity released.' % (len(lines), total / 1e6))
        return 0

    if command in ('--failure-list', '--failure-clean'):
        with ledger.transaction() as con:
            failure = [r[0] for r in con.execute('select provider from pricing_fault order by provider')]
        if or_deviation_active(home) and 'openrouter' not in failure:
            failure.append('openrouter')   # an OR-PIN ledger-panel drift also closes the provider; cleanup renews the baseline
        if not failure:
            print('pricing_fault is empty; no closed providers.')
            return 0
        print('pricing fault (provider fully closed): ' + ', '.join(failure))
        if command == '--failure-list':
            return 0
        target = [a for a in argv[1:] if not a.startswith('--')]
        to_clear = [p for p in failure if not target or p in target]
        if not to_clear:
            print('the given provider is not in the list', file=sys.stderr)
            return 2
        if not yes:
            print('To clear: %s; add --yes to approve.\n'
                  'If the cause (actual spending exceeded the estimate) is unresolved, the fault will return immediately.' % ', '.join(to_clear),
                  file=sys.stderr)
            return 2
        with ledger.transaction() as con:
            for provider in to_clear:
                con.execute('delete from pricing_fault where provider=?', (provider,))
        if 'openrouter' in to_clear:
            for entry_name in ('openrouter-deviation.json', 'openrouter-base.json', 'openrouter-credit.json'):
                (home / 'chain' / entry_name).unlink(missing_ok=True)
        _recovery_log(home, {'operation': 'failure-clean', 'providers': to_clear})
        print('cleared: ' + ', '.join(to_clear))
        return 0

    if command in ('--receipt-list', '--receipt-acceptance'):
        target = [a for a in argv[1:] if not a.startswith('--')]
        if command == '--receipt-acceptance':
            if len(target) != 1:
                print('usage: --receipt-acceptance RESERVATION_ID --yes', file=sys.stderr)
                return 2
            if not yes:
                print('Accepting an excess receipt reopens the family; the amount stays in the ledger and total/monthly caps do not grow. Add --yes to approve.',
                      file=sys.stderr)
                return 2
            try:
                acceptance = receipt_acceptance(home, target[0])
            except BudgetDenied as exc:
                print('receipt not accepted: %s' % exc, file=sys.stderr)
                return 2
            print('%s %s run=%s actual $%.4f upper bound $%.4f -> %s' % (
                acceptance['id'][:8], acceptance['provider'], acceptance['run_id'] or '-', acceptance['actual'] / 1e6, acceptance['bound'] / 1e6,
                'already accepted' if acceptance['already'] else 'acceptance saved'))
            return 0
        with ledger.transaction() as con:
            if target:
                ids = {target[0]}
                if (RunStore(home).path(target[0]) / 'run.json').exists():
                    ids |= operator_family(home, target[0])[0]
            else:
                ids = {r[0] for r in con.execute("select distinct json_extract(metadata,'$.run_id') from reservation "
                                                 "where json_extract(metadata,'$.run_id') is not null")}
            lines = receipt_overruns(con, ids)
        if not lines:
            print('acceptance pending excess receipt absent.')
            return 0
        for r in lines:
            print('%s %-12s run=%s actual $%.4f upper bound $%.4f  -> cadre recover --receipt-acceptance %s --yes' % (
                r['id'][:8], r['provider'], r['run_id'], r['actual'] / 1e6, r['bound'] / 1e6, r['id']))
        print('total %d excess receipts; the family cannot make new paid calls until acceptance.' % len(lines))
        return 0

    print('unknown recovery command: ' + command, file=sys.stderr)
    return 2


RECOVERY_COMMANDS = ('--stale-list', '--stale-close', '--phantom-list', '--phantom-go-back',
                      '--failure-list', '--failure-clean', '--receipt-list', '--receipt-acceptance')


PROVIDER_OUTAGE = 'provider_unavailable'
OUTAGE_REJECTION = re.compile(r'provider unavailable: (\S+) until (\d+)')


def provider_wait_rejection(receipt: Any) -> tuple[str, float] | None:
    for row in ((receipt or {}).get('refusals') or []) if isinstance(receipt, dict) else []:
        match = OUTAGE_REJECTION.fullmatch(str((row or {}).get('reason') or ''))
        if match:
            return match[1], float(match[2])
    return None


def provider_error_save(ledger: BudgetLedger, request: dict[str, Any]) -> dict[str, Any]:
    round = str(request.get('round') or '')[:64]
    error = {'code': request.get('code') if isinstance(request.get('code'), int) else None, 'round': round,
            'message': str(request.get('message') or '')[:160], 'ts': time.time()}
    line = ledger.error_save(str(request.get('id') or ''), error)
    if not line or not line['model']:
        return {}
    try:
        import model_router
    except ImportError:
        return {}
    provider_key = base_provider(line['provider'])
    key = model_router.model_key(provider_key, line['model'])
    endpoint = line.get('endpoint') if provider_key == 'openrouter' and round == PROVIDER_OUTAGE else None
    model_router.failure(provider_key, f"{error['code']} {round} {error['message']}", model=key,
                       allowed_fails=1 if round == PROVIDER_OUTAGE else 3, endpoint=endpoint)
    return {'path': key, 'endpoint': endpoint}


def closed_path(provider: str, model: str, endpoints: list[str] | None = None) -> tuple[str, float] | None:
    if not model:
        return None
    try:
        import model_router
    except ImportError:
        return None
    provider_key = base_provider(provider)
    key = model_router.model_key(provider_key, model)
    record = model_router.status_read()
    status = record.get(key) or {}
    try:
        end = float(status.get('cooldown_until') or 0)
    except (TypeError, ValueError):
        return None
    if end > time.time() and '[net]' in str(status.get('last') or ''):
        return key, end
    closed = model_router.endpoint_coolings(provider_key, model, record) if provider_key == 'openrouter' else {}
    if not closed:
        return None
    if endpoints is None:
        try:
            endpoints = json.loads((model_router.QE / 'chain/openrouter-price.json').read_text())['models'][key.split('/', 1)[1]]['providers']
        except (OSError, ValueError, KeyError, TypeError):
            return key, min(closed.values())
        if any(endpoint not in closed for endpoint in endpoints):
            return None
    if not isinstance(endpoints, list) or not endpoints or any(not isinstance(endpoint, str) or not endpoint for endpoint in endpoints):
        raise BudgetDenied('endpoint selection invalid')
    endings = [closed[endpoint] for endpoint in endpoints if endpoint in closed]
    return (key, min(endings)) if endings else None


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
    if len(sys.argv) > 1 and sys.argv[1] in RECOVERY_COMMANDS:
        return recovery(home, sys.argv[1:])
    if len(sys.argv) >= 4 and sys.argv[1] == '--progress':
        run_id = sys.argv[4] if len(sys.argv) > 4 else os.environ.get('CADRE_RUN_ID', '')
        if not run_id or sys.argv[2] not in PROGRESS_REASONS:
            print('usage: --progress REASON ID [RUN_ID]; reason: ' + ', '.join(PROGRESS_REASONS), file=sys.stderr)
            return 2
        try:
            progress_write(home, run_id, sys.argv[2], sys.argv[3])
        except (OSError, ValueError, KeyError) as exc:
            print(f'Could not write progress: {exc}', file=sys.stderr)
            return 1
        return 0
    ledger = BudgetLedger(home/'chain/request-budget.db')
    request = {}
    _slice_name = None
    try:
        request = json.load(sys.stdin)
        if request['action'] == 'reserve':
            metadata = request.get('metadata') or {}
            closed = closed_path(request['provider'], str(metadata.get('model') or ''),
                                (metadata.get('endpoint_selection') or {}).get('order'))
            if closed:
                raise BudgetDenied(f'provider unavailable: {closed[0]} until {closed[1]:.0f}')
            snapshot = observed_budget(home, code)
            maximum = int(Decimal(str(amount(request['maximum']))).to_integral_value(rounding=ROUND_CEILING))
            run_id = os.environ.get('CADRE_RUN_ID')
            # The helper pocket is real only if the calling AGENT is known: the engine writes only the PHASE role into the environment
            # (plan/impl/qa/quality/autopsy), compaction/scout never came through. The plugin reports the calling
            # agent's name in the metadata; for helper roles we go by that.
            agent = str((request.get('metadata') or {}).get('role') or '')
            # if the contract module cannot be read (isolated test home, incomplete install) role selection is NOT DONE and the old
            # behaviour continues — the whole reservation path must not break for the sake of one helper label.
            try:
                from contract import slice as _slice
                role_selection = agent if _slice(agent) == 'helper' else None
            except Exception:
                role_selection = None
            _cap, _slice_name = role_budget(home, run_id, role_selection, request['provider'])
            # B124: in manual `opencode` use the gate does NOT BLOCK, it only observes. For a request belonging to a run
            # (CADRE_RUN_ID set) observation mode is IGNORED — automated work is always subject to the cap.
            observation = os.environ.get('CADRE_BUDGET_OBSERVATION') == '1' and not run_id
            ident = ledger.reserve(request['provider'], maximum, snapshot,
                                   {**request.get('metadata', {}), 'run_id':run_id,
                                    'phase_role':os.environ.get('CADRE_ROLE'),
                                    'attempt_id':os.environ.get('CADRE_BUDGET_ATTEMPT'),
                                    'task_id':os.environ.get('CADRE_TASK_ID') or 'main'},
                                   run_cap=_cap, slice=_slice_name, observation=observation)
            run_spend_write(home, ledger, run_id)
            print(json.dumps({'id':ident}))
        elif request['action'] == 'settle':
            ledger.settle(request['id'], request['actual'], request['receipt'], request.get('usage'))
            run_spend_write(home, ledger, os.environ.get('CADRE_RUN_ID'))
            print('{}')
        elif request['action'] == 'provider-error':
            print(json.dumps(provider_error_save(ledger, request)))
        elif request['action'] == 'gen':
            try:
                ledger.production_save(str(request.get('id') or ''), str(request.get('generation') or ''))
            except (OSError, sqlite3.Error):
                pass
            print('{}')
        elif request['action'] == 'review-tool':
            try:
                ledger.review_tool(os.environ.get('CADRE_RUN_ID'), os.environ.get('CADRE_TASK_ID', ''), os.environ.get('CADRE_ROLE', ''))
            except BudgetDenied as exc:
                if not re.fullmatch(r'review limit: [a-z-]+: tools', str(exc)):
                    raise
                note_denial(home, reason=str(exc), try_no=False)
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
                    reason=str(exc) if isinstance(exc, BudgetDenied) else '', slice=_slice_name)
        print('budget denied: no verified allowance or receipt', file=sys.stderr)
        return 75


if __name__ == '__main__':
    raise SystemExit(main())
