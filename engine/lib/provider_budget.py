#!/usr/bin/env python3
"""Provider budget observation (prints JSON): remaining share, ratio, windows; no daily split."""
# provider_budget.py — provider budget observation (prints JSON): remaining share, ratio, window; NO daily split.
# Sources: Go window API + opencode DB (Go points, Σ formula), OpenRouter credit API, manually entered Zen balance, calendar.
import json, sqlite3, datetime, pathlib, re, urllib.parse, urllib.request, sys, calendar, os
import time as time_mod
import math
from decimal import Decimal
from contextlib import closing
from budget_gate import spending, spending_tokens, amount, DAILY_REQUEST, calibration_receipt
from claude_policy import cap as claude_cap, pay as claude_pay, last_reset as claude_last_reset, session_count, STALE_LIMIT_MINUTES, STALE_FRESH_MINUTES
from run_store import atomic_json

# Can be redirected with CADRE_HOME / OPENCODE_DB (like the other modules; tests use scratch)
QE = pathlib.Path(os.environ.get("CADRE_HOME", str(pathlib.Path.home() / ".local/share/cadre-engineering")))
DB = pathlib.Path(os.path.expanduser(os.environ.get("OPENCODE_DB", str(pathlib.Path.home() / ".local/share/opencode/opencode.db"))))
EXECUTOR_ID = pathlib.Path.home() / '.local/share/opencode/auth.json'


def or_key() -> str:
    try:
        key = json.loads(EXECUTOR_ID.read_text())['openrouter']['key']
        if isinstance(key, str) and key.strip():
            return key.strip()
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return (QE / "chain/openrouter-key").read_text().strip()


def sim_load():
    """SIMULATED LOAD: CADRE_SIM_LOAD=<json> is added ON TOP of the real numbers:
    {"go_points": monthly points, "zen_usd": $}. Undefined in production -> {}."""
    try:
        path = os.environ.get("CADRE_SIM_LOAD")
        if path and pathlib.Path(path).exists():
            d = json.loads(pathlib.Path(path).read_text() or "{}")
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def net_open():
    """CADRE_NO_NET=1 (isolated test home): no provider endpoint is contacted; the observation becomes 'unreachable'."""
    return os.environ.get('CADRE_NO_NET') != '1'


def go_observation(now_ts, go_start, sim):
    """4c-13: Go is one pool and its unit is points (1 point = 1% of the monthly pool). The gate is the API percentage; steps after the
    reading are added with the Σ formula. Without the API, the Σ formula; if the opencode DB cannot be read either, unknown (closed)."""
    import go_window
    try:
        windows = go_window.read(QE)
    except Exception:
        windows = None
    sim_window = sim.get("window") if isinstance(sim, dict) else None   # §40: boundary scenarios simulate the percentage
    if isinstance(sim_window, dict):
        windows = {'history': [], **(windows or {}),
                      **{k: v for k, v in sim_window.items() if k in go_window.WINDOWS}}
    own = go_window.own_percentages(DB, now_ts, (windows or {}).get('resets'), go_start)
    source, then, divergence = 'api', 0.0, ''
    if isinstance(windows, dict) and any(p in windows for p in go_window.WINDOWS):
        api_ts = float(windows.get('ts') or now_ts)
        then = go_window.sigma(DB, api_ts, now_ts) if api_ts < now_ts else 0.0
        # S11: if the API and Σ diverge, the cautious one (the larger percentage) wins
        windows, divergence = go_window.cross_check(windows, (own or {}).get('monthly'))
    elif own is not None:
        windows, source = {**own, 'resets': None, 'ts': now_ts, 'history': []}, 'db'
    else:
        windows = None
    if windows is None or then is None:   # the wall is still read from the API; remaining unknown -> gate closed
        return {'db': 'unreachable', 'source': source, 'unit': 'points', 'month_points': None, 'available': 0.0,
                'pay': 0.0, 'ratio': 0.0, 'own': own, 'windows': windows, 'window_divergence': divergence}
    then += float(sim.get("go_points", 0) or 0) if isinstance(sim, dict) else 0.0
    remaining = go_window.remaining_points(windows, then) or 0.0
    month_points = float(windows.get('monthly') or 0) + then
    pay = remaining + month_points
    return {'db': 'ok', 'source': source, 'unit': 'points', 'month_points': round(month_points, 3), 'available': round(remaining, 3),
            'pay': round(pay, 3), 'ratio': round(remaining / pay, 3) if pay > 0 else 0.0, 'points_then': round(then, 4),
            'own': own, 'windows': windows, 'window_divergence': divergence}

OR_DEVIATION_THRESHOLD = 0.20


def _or_ledger_usd():
    """OR-PIN: the total (USD) the ledger (request-budget.db) has SETTLED for OpenRouter."""
    try:
        db = QE / "chain/request-budget.db"
        if not db.exists():
            return 0.0
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as con:
            total = con.execute("select coalesce(sum(actual),0) from reservation where provider='openrouter' and actual is not null "
                                 "and coalesce(receipt,'') not like 'phantom:%' and coalesce(receipt,'') not like 'recovery:%'").fetchone()[0]
        return float(total) / 1e6
    except (OSError, sqlite3.Error):
        return None


def _or_ledger_observation(stamp: float) -> dict[str, object] | None:
    from budget_gate import or_receipt_observation
    db = QE / 'chain/request-budget.db'
    try:
        if not db.exists():
            return or_receipt_observation(None, stamp)
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as con:
            return or_receipt_observation(con, stamp)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return None


def _or_ledger_old_usd(limit):
    try:
        db = QE / "chain/request-budget.db"
        if not db.exists():
            return 0.0
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as con:
            total = con.execute("select coalesce(sum(actual),0) from reservation where provider='openrouter' and actual is not null "
                                 "and coalesce(receipt,'') not like 'phantom:%' and coalesce(receipt,'') not like 'recovery:%' "
                                 "and created < ?", (limit,)).fetchone()[0]
        return float(total) / 1e6
    except (OSError, sqlite3.Error):
        return None


def _or_estimated_usd():
    try:
        db = QE / "chain/request-budget.db"
        if not db.exists():
            return 0.0
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as con:
            total = con.execute("select coalesce(sum(actual),0) from reservation where provider='openrouter' and actual is not null "
                                 "and (receipt like 'phantom:%' or receipt like 'recovery:%')").fetchone()[0]
        return float(total) / 1e6
    except (OSError, sqlite3.Error):
        return 0.0


OR_DEVIATION_DIFF_USD = 0.50   # the ratio alone is not enough; on small amounts 20% is noise
OR_DEVIATION_DELAY_SEC = 3600


def _or_deviation(total_usage):
    """OR-PIN: returns the drift if the spending growth in the credit API diverges from the ledger growth by more than 20% and at least $0.50, else None.
    Measurement starts from the first observation (baseline); a rollback renews the baseline, `cadre recover --failure-clean openrouter` resets it."""
    ledger = _or_ledger_usd()
    if ledger is None:
        return {'reason': 'ledger-unreadable', 'api_usd': total_usage}
    if not all(math.isfinite(value) and value >= 0 for value in (total_usage, ledger)):
        return {'reason': 'invalid-measurement'}
    estimated = _or_estimated_usd()
    base_file = QE / "chain/openrouter-base.json"
    try:
        base = json.loads(base_file.read_text())
        api0, ledger0, estimated0 = float(base['api_usd']), float(base['ledger_usd']), float(base.get('estimated_usd', 0))
        if not all(math.isfinite(v) and v >= 0 for v in (api0, ledger0, estimated0)):
            raise ValueError('base invalid')
    except (OSError, ValueError, TypeError, KeyError):
        api0 = ledger0 = estimated0 = None
    d_api, d_ledger, d_estimated = ((total_usage - api0, ledger - ledger0, estimated - estimated0) if api0 is not None
                                  else (None, None, None))
    if api0 is None or d_api < 0 or d_ledger < 0 or d_estimated < 0:
        atomic_json(base_file, {"ts": time_mod.time(), "api_usd": total_usage, "ledger_usd": ledger, "estimated_usd": estimated})
        return None
    old = _or_ledger_old_usd(time_mod.time() - OR_DEVIATION_DELAY_SEC)
    if old is None:
        return {'reason': 'ledger-unreadable', 'api_usd': total_usage}
    sub, parent = min(d_ledger, max(0.0, old - ledger0)), d_ledger + d_estimated
    difference = 0.0 if sub <= d_api <= parent else (d_api - parent if d_api > parent else sub - d_api)
    ratio = difference / max(d_api, parent, 1e-9)
    if ratio <= OR_DEVIATION_THRESHOLD or difference < OR_DEVIATION_DIFF_USD:
        return None
    return {"api_usd": round(d_api, 4), "ledger_usd": round(d_ledger, 4), "estimated_usd": round(d_estimated, 4), "deviation": round(ratio, 4)}


def _or_close(deviation):
    """OR-PIN: writes the drift to a persistent warning file (read by the watcher/morning summary)."""
    try:
        (QE / "chain/openrouter-deviation.json").write_text(
            json.dumps({"ts": datetime.datetime.now().timestamp(), **deviation}, ensure_ascii=False))
    except OSError:
        pass


def _or_rule(entry_name, rule):
    """Normalizes a policy line. List: provider names (old format). Object: `tiers` endpoint tags,
    `providers` names; if neither, every endpoint tag matching the rule (quantization + maximum price); for `closed`
    weights quantization is not required."""
    if isinstance(rule, list):
        return {'id': 'provider_name', 'allowed': list(rule), 'closed': entry_name.startswith('openai/'), 'quantization': 'fp8', 'cap': None}
    labeled = 'tiers' in rule
    allowed = rule.get('tiers' if labeled else 'providers')
    # OR `only/order` takes a provider slug or an endpoint tag (docs: "deepinfra/turbo"); not the display name ("Sail Research")
    return {'id': 'tag' if labeled or allowed is None else 'provider_name', 'allowed': allowed,
            'closed': rule.get('closed') is True, 'quantization': rule.get('quantization') or 'fp8', 'cap': rule.get('max_price')}


def _or_endpoints(entry_name, rule, key):
    """Endpoints matching the policy: {id: {input, output, params, max_output, uptime, quantization}} (USD / 1M tokens)."""
    k = _or_rule(entry_name, rule)
    request = urllib.request.Request(f"https://openrouter.ai/api/v1/models/{entry_name}/endpoints",
                                     headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(request, timeout=5) as response:
        data = json.load(response)['data']
    endpoints = {}
    for endpoint in data.get('endpoints', []):
        id = endpoint.get(k['id'])
        if not id or (k['allowed'] is not None and id not in k['allowed']):
            continue
        if not k['closed'] and endpoint.get('quantization') != k['quantization']:
            continue
        price = endpoint.get('pricing') or {}
        try:
            input, output, cache = (float(price[a]) * 1e6 for a in ('prompt', 'completion', 'input_cache_read'))
        except (KeyError, ValueError, TypeError):
            continue
        if not (all(math.isfinite(v) for v in (input, output, cache)) and input > 0 and output > 0 and 0 <= cache < input):
            continue
        cap = k['cap']
        if cap and not (input <= float(cap['prompt']) * 1.000001 and output <= float(cap['completion']) * 1.000001):
            continue   # on a tie the smaller share; expensive tiers (region, fast) are left out
        limit = endpoint.get('max_completion_tokens')
        open = [float(v) for v in (endpoint.get('uptime_last_30m'), endpoint.get('uptime_last_1d')) if isinstance(v, (int, float))]
        known = endpoint.get('supported_parameters')
        entry_name = endpoint.get('provider_name')
        end_point = {'input': input, 'output': output, 'quantization': endpoint.get('quantization'),   # no parameter list means unknown (None)
              'params': sorted({'max_tokens', 'max_completion_tokens'} & set(known)) if isinstance(known, list) else None,
              'max_output': limit if isinstance(limit, int) and limit > 0 else None, 'uptime': min(open) if open else None,
              'cache': cache, 'entry_name': entry_name if isinstance(entry_name, str) and entry_name else None}
        if id in endpoints:   # two endpoints with the same name (region copy): the worst value in every field
            o = endpoints[id]
            limits = [v for v in (o['max_output'], end_point['max_output']) if v]
            open = [v for v in (o['uptime'], end_point['uptime']) if v is not None]
            shared = None if o['params'] is None or end_point['params'] is None else sorted(set(o['params']) & set(end_point['params']))
            end_point = {**end_point, 'input': max(o['input'], input), 'output': max(o['output'], output),
                  'params': shared, 'max_output': min(limits) if limits else None,
                  'uptime': min(open) if open else None, 'cache': max(o['cache'], cache),
                  'entry_name': end_point['entry_name'] if o['entry_name'] == end_point['entry_name'] else None}
        endpoints[id] = end_point
    base = list(k['allowed']) if k['allowed'] is not None else list(endpoints)
    from limits import OR as OR_LIMIT
    order = sorted((u for u in base if u in endpoints), key=lambda u: (
        (endpoints[u]['uptime'] if endpoints[u]['uptime'] is not None else 100.0) < OR_LIMIT['low_uptime'],
        endpoints[u]['input'] * 100_000 + endpoints[u]['output'] * 32_768))   # default order; the plugin reorders with the request's own tokens
    return endpoints, order, k


def or_price():
    """OR-PIN: only approved endpoints that report a cache price (fp8 required for open-weight models), below the maximum price.
    4c-7: if the refresh fails the last good price is kept (age-limited) and retried after 5 min; no empty file is written."""
    from limits import OR as OR_LIMIT
    if not net_open():
        return {}
    cache = QE / "chain/openrouter-price.json"
    now_ts = time_mod.time()
    try:
        old = json.loads(cache.read_text())
        old = old if old.get('version') == 2 and old.get('id') == 'label' else {}
    except (OSError, ValueError, TypeError, AttributeError):
        old = {}
    try:
        next = min(float(old.get('next', 'inf')),
                      float(old['ts']) + OR_LIMIT['price_fresh_sec'] - OR_LIMIT['price_repeat_sec'])
        if 0 <= now_ts - float(old['ts']) and now_ts < next:
            return old.get("models", {})
    except (KeyError, ValueError, TypeError):
        pass
    models, failed = {}, False

    def last_good(entry_name):
        previous = (old.get('models') or {}).get(entry_name)
        try:
            stamp = float(previous.get('price_ts', old['ts'])) if previous else 0.0
            if previous and 0 <= now_ts - stamp < OR_LIMIT['price_age_sec']:
                models[entry_name] = {**previous, 'price_ts': stamp}   # an old record without a stamp must not refresh its age as it is carried over
        except (KeyError, ValueError, TypeError, AttributeError):
            pass
    try:
        key = or_key()
        policy = json.loads(pathlib.Path(__file__).with_name('openrouter-policy.json').read_text())
    except (OSError, ValueError, TypeError):
        key, policy, failed = None, {}, True
        for entry_name in old.get('models') or {}:
            last_good(entry_name)
    for entry_name, rule in policy.items():
        try:
            endpoints, order, k = _or_endpoints(entry_name, rule, key)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            failed = True
            last_good(entry_name)
            continue
        if order:
            models[entry_name] = {'input': max(endpoints[u]['input'] for u in order), 'output': max(endpoints[u]['output'] for u in order),
                            'providers': order, 'fp8_cache': True, 'closed': k['closed'],
                            'tiers': {u: endpoints[u] for u in order}, 'price_ts': now_ts,
                            **({'max_price': k['cap']} if k['cap'] else {})}
    atomic_json(cache, {'version': 2, 'id': 'label', 'ts': now_ts, 'models': models,
                      'next': now_ts + (OR_LIMIT['price_repeat_sec'] if failed else OR_LIMIT['price_fresh_sec'] - OR_LIMIT['price_repeat_sec'])})
    return models


def _or_permanent_deviation():
    try:
        return json.loads((QE / "chain/openrouter-deviation.json").read_text()).get('permanent') is True
    except (OSError, ValueError, AttributeError):
        return False


OR_RECEIPT_SAMPLE, OR_RECEIPT_INTERVAL, OR_RECEIPT_CLOSING = 3, 600, 2
OR_RECEIPT_TOLERANCE = (0.10, 0.002)   # ratio and minimum USD; rounding noise on small calls


def or_receipt_audit():
    """4c-7(a): OR receipts in the ledger are sample-compared against OR's call breakdown (generation); on a mismatch a persistent
    drift is written and OR closes (`cadre recover --failure-clean openrouter` reopens it). Returns: mismatching samples or None."""
    if not net_open():
        return None
    record_path = QE / "chain/openrouter-receipt-audit.json"
    try:
        record = json.loads(record_path.read_text())
    except (OSError, ValueError):
        record = {}
    now_ts = time_mod.time()
    if 0 <= now_ts - float(record.get('ts', 0) or 0) < OR_RECEIPT_INTERVAL:
        return None
    tried = list(record.get('tried') or [])
    sub_limit = now_ts - 7 * 86400
    try:   # the period before the baseline (first observation / failure cleanup) was already judged (04.10 drift); only later is sampled
        sub_limit = max(sub_limit, float(json.loads((QE / "chain/openrouter-base.json").read_text())['ts']))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        key = or_key()
        with closing(sqlite3.connect((QE / "chain/request-budget.db").resolve().as_uri() + '?mode=ro', uri=True)) as con:
            lines = con.execute("select id, actual, receipt from reservation where provider='openrouter' and actual is not null "
                                   "and receipt like '%:gen-%' and created < ? and created > ? order by created desc limit 50",
                                   (now_ts - 120, sub_limit)).fetchall()
    except (OSError, sqlite3.Error):
        return None
    nonconforming = []
    for ident, actual, receipt in [r for r in lines if r[0] not in tried][:OR_RECEIPT_SAMPLE]:
        gen = receipt.split(':', 1)[1]
        try:
            request = urllib.request.Request("https://openrouter.ai/api/v1/generation?id=" + urllib.parse.quote(gen),
                                           headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(request, timeout=8) as response:
                breakdown = float(json.load(response)['data']['total_cost'])
        except (OSError, ValueError, KeyError, TypeError):
            continue   # no breakdown yet or unreachable: retried later
        our = actual / 1e6
        if math.isfinite(breakdown) and abs(breakdown - our) > max(OR_RECEIPT_TOLERANCE[0] * max(breakdown, our), OR_RECEIPT_TOLERANCE[1]):
            nonconforming.append({'reservation': ident, 'ledger_usd': round(our, 6), 'breakdown_usd': round(breakdown, 6), 'ts': now_ts})
        tried.append(ident)
    total = [u for u in record.get('nonconforming') or [] if float(u.get('ts') or 0) > sub_limit] + nonconforming
    atomic_json(record_path, {'ts': now_ts, 'tried': tried[-200:], 'nonconforming': total[-20:]})
    if not nonconforming:
        return None
    closed = len({u['reservation'] for u in total}) >= OR_RECEIPT_CLOSING
    if closed:
        _or_close({'reason': 'receipt-breakdown', 'permanent': True, 'samples': total})
    return {'closed': closed, 'samples': nonconforming}


def _attempt_finished(meta):
    parts = [meta.get(k) for k in ('run_id', 'task_id', 'attempt_id')]
    if not all(isinstance(p, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', p) for p in parts):
        return False
    try:
        record = json.loads((QE / 'runs' / parts[0] / 'tasks' / parts[1] / 'attempts' / parts[2] / 'attempt.json').read_text())
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and record.get('outcome') in ('finished', 'not_started')


def or_pending_close():
    if not net_open():
        return 0
    db = QE / "chain/request-budget.db"
    if not db.exists():
        return 0
    try:
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as con:
            lines = con.execute("select id, metadata from reservation where provider like 'openrouter%' and actual is null "
                                   "and json_valid(metadata) and json_extract(metadata,'$.generation') is not null and created < ?",
                                   (time_mod.time() - 20,)).fetchall()
        key = or_key() if lines else ''
    except (OSError, sqlite3.Error):
        return 0
    from budget_gate import BudgetLedger, BudgetDenied
    from monthly_budget import MonthlyLedger, MonthlyBudgetDenied
    closing_ones = 0
    for ident, meta in lines:
        try:
            meta = json.loads(meta)
        except ValueError:
            continue
        gen = meta.get('generation')
        if not isinstance(gen, str) or not re.fullmatch(r'gen-[A-Za-z0-9_-]{1,128}', gen) or not _attempt_finished(meta):
            continue
        try:
            request = urllib.request.Request("https://openrouter.ai/api/v1/generation?id=" + urllib.parse.quote(gen),
                                           headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(request, timeout=8) as response:
                breakdown = float(json.load(response)['data']['total_cost'])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not math.isfinite(breakdown) or breakdown < 0:
            continue
        real = math.ceil(breakdown * 1e6)
        try:
            BudgetLedger(db).settle(ident, real, f'{ident}:{gen}')
            monthly_id = meta.get('monthly_id')
            if isinstance(monthly_id, str) and monthly_id:
                MonthlyLedger(QE / 'chain/monthly-budget.db', cap=1).settle(monthly_id, real, f'{monthly_id}:{gen}')
        except (BudgetDenied, MonthlyBudgetDenied, OSError, sqlite3.Error):
            continue
        closing_ones += 1
    return closing_ones


def or_balance():
    """03.10: OpenRouter prepaid credit (no daily split): remaining = total_credits - total_usage. 5 min cache, key
    chain/openrouter-key (600). Unreadable -> None -> gate closed (fail-closed). OR-PIN (04.10): if the credit API drop
    diverges from the ledger by more than 20%, OR closes + a warning is written; a persistent drift (receipt ↔ breakdown) stays closed until cleared manually."""
    if not net_open():
        return None
    if _or_permanent_deviation():
        return None
    cache = QE / "chain/openrouter-credit.json"
    try:
        d = json.loads(cache.read_text())
        if 0 <= datetime.datetime.now().timestamp() - float(d["ts"]) < 300:
            if d.get("closed"):
                return None
            remaining = float(d['remaining'])
            return remaining if math.isfinite(remaining) and remaining >= 0 else None
    except Exception:
        pass
    try:
        observation = _or_ledger_observation(time_mod.time())
        key = or_key()
        r = urllib.request.Request("https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(r, timeout=8) as response:
            v = json.load(response)['data']
        credits, usage = float(v['total_credits']), float(v['total_usage'])
        if not all(math.isfinite(value) and value >= 0 for value in (credits, usage)):
            return None
        remaining = float(max(Decimal(0), Decimal(str(v['total_credits'])) - Decimal(str(v['total_usage']))))
        deviation = _or_deviation(usage)
        if deviation is not None:
            _or_close(deviation)
            cache.write_text(json.dumps({"ts": datetime.datetime.now().timestamp(), "remaining": remaining,
                                       "closed": True, "deviation": deviation}))
            return None
        (QE / "chain/openrouter-deviation.json").unlink(missing_ok=True)   # 4c-7: drift is over; an old file does not show OR as failed
        if observation is not None:
            observation['spent_micro'] = int(Decimal(str(usage)) * 1_000_000)
        cache.write_text(json.dumps({"ts": datetime.datetime.now().timestamp(), "remaining": remaining, "loaded": credits,
                                  'credit_observation': observation}))
        return remaining
    except Exception:
        return None


def or_free_limit():
    """D15: /key account counter; no new quota is created per model (UTC day)."""
    cache = QE / 'chain/openrouter-account-limit.json'
    now = time_mod.time()
    def verify(data):
        count = data['count']
        if any(type(count.get(k)) is not int or count[k] < 0 for k in ('used', 'limit', 'remaining')):
            raise ValueError('invalid free request count')
        if count['remaining'] > max(0, count['limit'] - count['used']):
            raise ValueError('inconsistent free request count')
        if not 0 <= now - float(data['ts']) < 300 or int(now // 86400) != int(float(data['ts']) // 86400):
            raise ValueError('stale account count')
        return count
    try:
        return verify(json.loads(cache.read_text()))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if not net_open():
        return None
    try:
        key = or_key()
        request = urllib.request.Request('https://openrouter.ai/api/v1/key',
                                         headers={'Authorization': 'Bearer ' + key})
        with urllib.request.urlopen(request, timeout=8) as response:
            count = json.load(response)['data']['free_model_daily_requests']
        data = {'ts': now, 'count': count}
        verify(data)
        from run_store import atomic_json
        atomic_json(cache, data)
        return count
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _or_status():
    """OR-PIN: observation dict of the 'or' bucket. If the credit API was unreachable or there is drift against the ledger
    the gate is closed; the drift also carries a human-readable warning. Also refreshes or_price() (reservation file)."""
    remaining = or_balance()
    or_price()
    net_micro = int(Decimal(str(remaining or 0)) * 1_000_000)
    net = net_micro / 1_000_000
    status = {"db": "ok" if remaining is not None else "unreachable", "remaining": net,
             "pay": net, "available": net}
    try:   # total credit loaded: the status screen shows the remaining ratio
        credit = json.loads((QE / "chain/openrouter-credit.json").read_text())
        loaded = float(credit.get("loaded") or 0)
        if remaining is not None and loaded > 0:
            status["loaded"] = round(loaded, 4)
        if (remaining is not None and int(Decimal(str(credit['remaining'])) * 1_000_000) == net_micro
                and isinstance(credit.get('credit_observation'), dict)):
            status['credit_observation'] = credit['credit_observation']
    except (OSError, ValueError, TypeError, AttributeError, KeyError, OverflowError):
        pass
    if remaining is None:
        try:
            deviation = json.loads((QE / "chain/openrouter-deviation.json").read_text())
            status["db"] = "deviation"
            status["warning"] = (f"OR CLOSED: receipt and OR breakdown differ in {len(deviation.get('samples') or [])} samples"
                              if deviation.get('reason') == 'receipt-breakdown' else
                              f"OR CLOSED: credit-api ${deviation.get('api_usd', 0):.2f} differs from the ledger "
                              f"${deviation.get('ledger_usd', 0):.2f} by {deviation.get('deviation', 0) * 100:.0f}%")
        except Exception:
            pass
    status['free_request'] = or_free_limit()
    return status


def _month_add(dt, n):
    y, m = dt.year, dt.month + n
    y += (m - 1) // 12; m = (m - 1) % 12 + 1
    return dt.replace(year=y, month=m, day=min(dt.day, calendar.monthrange(y, m)[1]))


def go_period(now=None):
    """Go billing period. chain/go-period-reset.txt = 'YYYY-MM-DD HH:MM'.
    The current API takes precedence; without a local date the last fallback is the calendar month.
    A past local date is rolled forward month by month. Returns: (start_epoch, reset_dt|None, remaining_days)."""
    import math
    now = now or datetime.datetime.now()
    try:
        reset = datetime.datetime.strptime((QE / "chain/go-period-reset.txt").read_text().strip(), "%Y-%m-%d %H:%M")
    except Exception:
        reset = None
    try:   # B326: Go's own monthly reset is authoritative; when payment is late the period shifts and the date in the file goes stale
        import go_window
        r = ((go_window.read(QE) or {}).get("resets") or {}).get("monthly")
        api_reset = datetime.datetime.fromisoformat(str(r).replace("Z", "+00:00")).astimezone().replace(tzinfo=None) if r else None
    except Exception:
        api_reset = None
    if api_reset is not None and api_reset > now:
        reset = api_reset
    if reset is None:
        month_day = calendar.monthrange(now.year, now.month)[1]
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return int(start.timestamp()), None, max(1, month_day - now.day + 1)
    anchor, months = reset, 0
    while reset <= now:
        months += 1
        reset = _month_add(anchor, months)
    start = _month_add(anchor, months - 1)
    return int(start.timestamp()), reset, max(1, math.ceil((reset - now).total_seconds() / 86400))


JOB_BUCKET_DAY = 7   # budget ladder: typical job cost from paid jobs of the last 7 days (p75)


def job_cost_measure(now_ts):
    """p75 of the cost per paid job (USD) for role bucket -> provider (at least 5 jobs) and '_' (all providers)."""
    import sqlite3
    from collections import defaultdict
    total = defaultdict(float)
    try:
        with closing(sqlite3.connect(f"file:{QE / 'chain/request-budget.db'}?mode=ro", uri=True, timeout=5)) as con:
            for provider_key, actual, meta in con.execute('select provider, actual, metadata from reservation where actual is not null and created > ?',
                                                 (now_ts - JOB_BUCKET_DAY * 86400,)):
                m = json.loads(meta or '{}')
                if m.get('run_id'):
                    bucket = 'plan' if m.get('task_id') == 'plan' else (m.get('slice') or 'lanes')
                    total[(bucket, provider_key, m['run_id'], m.get('task_id'))] += actual / 1e6
    except Exception:
        return {}
    groups = defaultdict(list)
    for (bucket, provider_key, _r, _t), usd in total.items():
        if usd > 0.0005:
            groups[(bucket, provider_key)].append(usd)
            groups[(bucket, '_')].append(usd)
    outcome = defaultdict(dict)
    for (bucket, provider_key), v in groups.items():
        if len(v) >= 5:
            v.sort()
            outcome[bucket][provider_key] = round(v[min(len(v) - 1, int(0.75 * len(v)))], 4)
    return dict(outcome)


def reservation_measure(now_ts):
    """B221: p50 of the reservation the gate holds per single call (USD, at least
    3 calls) for role bucket -> provider and '*' (provider-wide). Without it the ladder counts as 'active' a provider the gate would deny every time (R10-hard: OpenAI)."""
    import sqlite3
    from collections import defaultdict
    groups = defaultdict(list)
    try:
        with closing(sqlite3.connect(f"file:{QE / 'chain/request-budget.db'}?mode=ro", uri=True, timeout=5)) as con:
            for provider_key, reserved, meta in con.execute('select provider, reserved, metadata from reservation where created > ?',
                                                   (now_ts - JOB_BUCKET_DAY * 86400,)):
                m = json.loads(meta or '{}')
                if m.get('run_id') and not m.get('observation'):
                    bucket = 'plan' if m.get('task_id') == 'plan' else (m.get('slice') or 'lanes')
                    groups[(bucket, provider_key)].append(reserved / 1e6)
                    groups[('*', provider_key)].append(reserved / 1e6)
    except Exception:
        return {}
    outcome = defaultdict(dict)
    for (bucket, provider_key), v in groups.items():
        if len(v) >= 3:
            v.sort()
            middle = len(v) // 2
            outcome[bucket][provider_key] = round(v[middle] if len(v) % 2 else (v[middle - 1] + v[middle]) / 2, 4)
    return dict(outcome)


CALIBRATED_DAY, CALIBRATED_AT_LEAST, CALIBRATED_MULTIPLIER = 14, 5, 1.5


def calibrated_measure(now_ts):
    """B253 (OBSERVATION-6): p95 of SETTLED receipts x1.5 for bucket -> provider = the gate's calibrated cap (USD). The worst case
    (output cap at full price + input 2x) came out at 37x reality for the judge, and sessions seemed not to fit their pocket. Pending, observation and
    receipts older than 14 days are not counted; a bucket with fewer than 5 receipts produces no value (the worst case remains)."""
    import sqlite3
    from collections import defaultdict
    groups = defaultdict(list)
    try:
        with closing(sqlite3.connect(f"file:{QE / 'chain/request-budget.db'}?mode=ro", uri=True, timeout=5)) as con:
            for provider_key, actual, receipt, meta in con.execute('select provider, actual, receipt, metadata from reservation where created > ? and created <= ? and actual > 0',
                                                          (now_ts - CALIBRATED_DAY * 86400, now_ts)):
                m = json.loads(meta or '{}')
                if m.get('run_id') and calibration_receipt(receipt, m):
                    bucket = 'plan' if m.get('task_id') == 'plan' else (m.get('slice') or 'lanes')
                    groups[(bucket, provider_key)].append(actual / 1e6)
    except Exception:
        return {}
    outcome = defaultdict(dict)
    for (bucket, provider_key), v in groups.items():
        if len(v) >= CALIBRATED_AT_LEAST:
            v.sort()
            p95 = v[min(len(v) - 1, int(round(0.95 * (len(v) - 1))))]
            outcome[bucket][provider_key] = round(p95 * CALIBRATED_MULTIPLIER, 4)
    return dict(outcome)


def main():
    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    month_day = calendar.monthrange(now.year, now.month)[1]
    remaining_day_calendar = max(1, month_day - now.day + 1)
    go_start, go_reset, remaining_day = go_period(now)   # Go: panel reset period, not the calendar month
    SIM = sim_load()

    # --- Go: one pool, points (4c-13) + 429 stamp + window wall ---
    go = go_observation(now.timestamp(), go_start, SIM)
    windows = go.pop('windows')
    window_open, window_reason = True, "open"
    try:
        t = int((QE / "chain/go-429-last.txt").read_text().strip())
        if datetime.datetime.now().timestamp() - t < 18000:
            window_open, window_reason = False, "429 marker (5 hour)"
    except Exception: pass
    try:
        import go_window
        is_open, reason = go_window.status(windows)
        if not is_open:
            window_open, window_reason = False, reason
    except Exception:
        windows = None

    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    # --- opencode zen (prepaid balance) --------------------------------------------------------
    # The balance cannot be read by a program: the source is the date+amount entered by the user, the baseline is the ENTRY TIME
    # (B92: taking the start of the day made a fresh balance shrink the share). It does not expire, so it is spread over a sliding horizon.
    zen_file = QE / "chain/zen-balance.txt"       # format: "<YYYY-MM-DD[THH:MM:SS]> <balance-usd>"
    zen_entry, zen_date, zen_db = 0.0, None, 'absent'
    zen_base_ts = 0.0
    try:
        zen_field_list = zen_file.read_text().split()
        try:
            observed_at = datetime.datetime.fromisoformat(zen_field_list[0])
            zen_date = observed_at.date()
            zen_base_ts = observed_at.timestamp() if observed_at.time() != datetime.time() else 0.0
        except ValueError:
            zen_date = datetime.date.fromisoformat(zen_field_list[0])
        zen_entry = float(zen_field_list[1])
        zen_db = 'ok'
    except Exception:
        zen_entry, zen_date = 0.0, None
    if not zen_base_ts:
        zen_base_ts = (datetime.datetime.combine(zen_date, datetime.time()).timestamp() if zen_date else 0.0)
    zen_all = spending(DB, 'opencode', zen_base_ts) if zen_db == 'ok' else None
    zen_day = spending(DB, 'opencode', day_start) if zen_db == 'ok' else None
    if zen_db == 'ok' and (zen_all is None or zen_day is None):
        zen_db = 'unreachable'
    zen_spent = (zen_all[0] if zen_all else 0) + float(SIM.get("zen_usd", 0) or 0)
    zen_remaining = max(0.0, zen_entry - zen_spent) if zen_db == 'ok' else 0.0
    zen_today = (zen_day[0] if zen_day else 0) + float(SIM.get("zen_usd", 0) or 0)
    pay_zen = max(0.0, zen_remaining + zen_today) if zen_db == 'ok' else 0.0
    day_used_amount_zen = max(0.0, pay_zen - zen_today)
    ratio_zen = (day_used_amount_zen / pay_zen) if pay_zen > 0 else 0.0


    # --- Groq (pre-filter only, 4c-11; direct-API counter from a file; limit from a single source) ---
    GROQ_LIMIT = DAILY_REQUEST['groq']
    gq = 0
    try:
        d_, v_ = (QE / "chain/groq-day-counter.txt").read_text().split()
        if d_ == today: gq = int(v_)
    except Exception: pass
    gq_rows = [spending(DB, 'groq', day_start)]
    gq_db = 'ok' if all(r is not None for r in gq_rows) else 'unreachable'
    gq += sum(r[1] for r in gq_rows if r is not None) if gq_db == 'ok' else GROQ_LIMIT
    ratio_gq = max(0, GROQ_LIMIT - gq) / GROQ_LIMIT

    # --- Claude (subscription account; weekly reset rhythm; split by session count; own ledger when stale) ---
    vq = pathlib.Path.home() / ".claude/claude_quota.json"
    cl = {"source": "absent", "pay": 0.0, "session": 0, "fable_today": 0}
    try:
        # B34: while the user is in Claude Code the status line may write the file AFTER `now` was taken -> negative age ->
        # `valid` False, share 0 (Claude silently closed for a minute). Negative age = fresh.
        age_min = max(0.0, float(os.environ.get("CADRE_TEST_STALE_MIN") or (now.timestamp() - vq.stat().st_mtime) / 60))
        d7 = json.loads(vq.read_text())["rate_limits"]
        used7 = float(os.environ.get("CADRE_TEST_USED7") or d7["seven_day"]["used_percentage"]); used5 = float(os.environ.get("CADRE_TEST_USED5") or d7["five_hour"]["used_percentage"])
        all_json = json.loads(vq.read_text())
        ctx = (all_json.get("context_window") or {}).get("used_percentage")
        sess_cost = float((all_json.get("cost") or {}).get("total_cost_usd") or 0)
        # 1% <-> $ CALIBRATOR: learn a delta ratio from (used7, session cost) pairs
        remaining_amount = QE / "chain/claude-calibrated.jsonl"
        try:
            last = json.loads(remaining_amount.read_text().splitlines()[-1]) if remaining_amount.exists() else None
        except Exception:
            last = None
        ledger_scratch = 0.0
        try:
            for _l in (QE / "chain/claude-spend.jsonl").read_text().splitlines():
                ledger_scratch += float(json.loads(_l).get("usd", 0) or 0)
        except Exception:
            pass
        if last is None or abs(used7 - last["used7"]) >= 1.0:
            with open(remaining_amount, "a") as f:
                f.write(json.dumps({"ts": now.isoformat(timespec="seconds"), "used7": used7, "cost": round(ledger_scratch, 4)}) + "\n")
        percent1 = None
        try:
            rows = [json.loads(l) for l in remaining_amount.read_text().splitlines()]
            # filter: used7 must grow (skip reset/account mix-ups), jump <=10 points,
            # cost must grow; cost = ENGINE ledger (not session cost) -> meaning "engine-$/1%"
            pairs = [(b2["cost"] - a["cost"]) / (b2["used7"] - a["used7"])
                       for a, b2 in zip(rows, rows[1:])
                       if 0 < b2["used7"] - a["used7"] <= 10 and b2["cost"] > a["cost"]]
            if len(pairs) < 3:
                pairs = []
            if pairs:
                pairs.sort(); percent1 = round(pairs[len(pairs)//2], 1)
        except Exception: pass
        import subprocess as sp
        # B3: `pgrep -x claude` did not see the live session; counted by command name (basename).
        session = session_count(sp.run(["ps", "-axo", "comm="], capture_output=True, text=True).stdout.splitlines())
        # USD is a conservative conversion of the weekly reserve, not an
        # invented daily or monthly spending cap. An explicit existing profile
        # can seed calibration; absent both, paid admission remains unknown.
        if percent1 is None:
            try:
                percent1 = amount((QE/'chain/claude-pool-usd.txt').read_text().strip()) / 100
            except Exception:
                percent1 = None
        # C31/B2 — STALE OBSERVATION COMPENSATION: the observation refreshes only while the user is in Claude Code. If stale, the user
        # is not active; the only spending after that point is the automation's OWN ledger (usd / per-1% equivalent). If the observation
        # is from BEFORE the last weekly reset, the observed usage belongs to the previous week: only own spending after the reset counts.
        observation_ts = now.timestamp() - age_min * 60
        reset_ts = claude_last_reset(now.astimezone()).timestamp()
        previous_week = observation_ts < reset_ts
        threshold_ts = reset_ts if previous_week else observation_ts
        ledger = QE / "chain/claude-spend.jsonl"
        own_then = 0.0
        if ledger.exists():
            for l in ledger.read_text().splitlines():
                try:
                    r = json.loads(l)
                    r_ts = time_mod.mktime(time_mod.strptime(str(r["ts"])[:19], "%Y-%m-%dT%H:%M:%S"))
                    if r_ts > threshold_ts:
                        if percent1:
                            own_then += float(r.get("usd", 0) or 0) / percent1
                        else:
                            own_then += float(r.get("percent_estimate", 0.05) or 0.05)
                except Exception: pass
        used7_eff = (0.0 if previous_week else used7) + own_then
        # P5: if the 5-hour window was full at observation time but `resets_at` has passed, the window is renewed; it does not stay 0 all night.
        reset5 = (d7.get("five_hour") or {}).get("resets_at")
        try:
            reset5_passed = reset5 is not None and float(reset5) <= now.timestamp()
        except (TypeError, ValueError):
            reset5_passed = False
        used5_eff = 0.0 if (previous_week or reset5_passed) else used5
        cap = claude_cap(now.astimezone())
        fresh = age_min <= STALE_FRESH_MINUTES
        valid = 0 <= age_min <= STALE_LIMIT_MINUTES
        pay = claude_pay(used7_eff, used5_eff, now.astimezone(), session, stale_minutes=age_min)
        fb = 0
        fable_ids = []
        g = QE / "chain/claude-spend.jsonl"
        if g.exists():
            for l in g.read_text().splitlines():
                try:
                    r = json.loads(l)
                    if r["ts"][:10] == today and "fable" in r.get("model", ""):
                        fb += 1
                        if r.get('attempt_id'):
                            fable_ids.append(r['attempt_id'])
                except Exception: pass
        pool_remaining = max(0.0, 100-used7_eff) * percent1 if percent1 is not None else 0.0
        available_usd = pay * percent1 if percent1 is not None else 0.0
        cl = {"source": ("fresh" if fresh else f"stale({int(age_min)}min)"), "valid": valid,
              "observation_previous_week": previous_week, "reset5_passed": reset5_passed, "own_then": round(own_then, 2),
              "used7": round(used7_eff, 1), "used5": round(used5_eff, 1), "cap": cap, "session": session,
              "pay": round(pay, 1), "fable_today": fb, "fable_attempts": sorted(set(fable_ids)),
              "pool_remaining_usd": round(pool_remaining, 2), "available_usd": round(available_usd, 2),
              "ctx": ctx, "percent1_usd": percent1,
              "week_pool_usd_estimate": (round(percent1 * 100) if percent1 else None)}
    except Exception:
        pass

    print(json.dumps({
        "ts": now.isoformat(timespec="seconds"), "today": today, "remaining_day": remaining_day,
        "go": {**go, "period_start": datetime.datetime.fromtimestamp(go_start).strftime("%Y-%m-%d %H:%M"),
               "reset": go_reset.strftime("%Y-%m-%d %H:%M") if go_reset else None, "remaining_day": remaining_day,
               "window_open": window_open, "window_reason": window_reason,
               "windows": ({k: windows.get(k) for k in ("rolling", "weekly", "monthly", "resets", "ts")}
                              if isinstance(windows, dict) else None)},
        "job_cost": job_cost_measure(now.timestamp()),   # budget ladder: role -> provider -> typical job (USD, p75)
        "reservation": reservation_measure(now.timestamp()),   # B221: role -> provider -> the gate's single-call reservation (USD, p50)
        "calibrated": calibrated_measure(now.timestamp()),           # B253: role -> provider -> calibrated cap (real p95 x1.5, USD)
        "zen": {"db": zen_db, "balance_entry": round(zen_entry, 3), "entry_date": (zen_date.isoformat() if zen_date else None),
                "spent": round(zen_spent, 4), "remaining": round(zen_remaining, 3),
                "pay": round(pay_zen, 3), "available": round(day_used_amount_zen, 3), "ratio": round(ratio_zen, 3)},
        "or": _or_status(),
        # B106: `tpd` (daily token cap) is now read — on groq 200K TPD, 25 full-size calls with an 8K request.
        "groq": {"db":gq_db, "today_request": gq, "ratio": round(ratio_gq, 3),
                 "today_token": spending_tokens(DB, 'groq', day_start)},
        "claude": cl,
        "sim": (SIM or None),
    }))

if __name__ == "__main__":
    main()
