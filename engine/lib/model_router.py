#!/usr/bin/env python3
"""Single router: produces a model/provider candidate chain per role, budget and context, for the engine and the dispatcher.
The chain may be empty (no cloud capacity); the caller handles it with a budget wait. Every decision is written to chain/model_router.jsonl.
Input: provider_budget.py JSON, chain/provider-status.json (circuit breaker: 3 errors -> 30 min, 429/quota -> 60 min), role, job class, request size.
CLI:
  model_router.py chain <role> [class] [request_tokens]   -> JSON {"chain":[...], "reason":[...]}
  model_router.py first <role> [class] [request_tokens]      -> first model (rc 76 without capacity)
  model_router.py next <role> <current_model>           -> the next model after the current one in the chain
  model_router.py failure <provider|provider/model> [error]  -> write an error to the circuit breaker (at (model, route) level if a model is given)
  model_router.py config <file>                            -> write OPENCODE_CONFIG for sub-agents + compaction
  model_router.py short <short_name>                        -> plan JSON short name
  model_router.py status                                    -> circuit breaker table"""
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from run_store import FileLock, atomic_json

QE = pathlib.Path(os.environ.get("CADRE_HOME", os.path.expanduser("~/.local/share/cadre-engineering")))
# RCA: injection/regression tests (a fake opencode producing "quota") wrote failures to the LIVE circuit breaker -> DS and Go went into
# a fake 60 min cooldown. Tests redirect these two paths to their own scratch with CADRE_ROUTER_STATUS / CADRE_ROUTER_RECORD.
STATUS = pathlib.Path(os.environ.get("CADRE_ROUTER_STATUS", str(QE / "chain" / "provider-status.json")))
RECORD = pathlib.Path(os.environ.get("CADRE_ROUTER_RECORD", str(QE / "chain" / "model_router.jsonl")))
# --- capability table ------------------------------------------------------------------------------------
# context/request_cap are ONLY A FALLBACK (if the catalogue cannot be read); the real limit comes from the catalogue per MODEL.
# bucket: monthly|credit|free|subscription
PROVIDER = {
    "claude":      {"bucket": "subscription", "context": 200000, "request_cap": 150000},
    "opencode-go": {"bucket": "monthly",    "context": 128000, "request_cap": 100000},
    "opencode":    {"bucket": "credit",    "context": 128000, "request_cap": 100000},   # 0c: zen (prepaid balance)
# Fallback values: the real limit comes from the catalogue per MODEL. On the free tier what cuts is TPM/RPM, not dollars.
    # 4c-11: groq only in the pre-filter (direct HTTP, plan_ds); no free provider in the chain. groq 18.09 (console.groq.com/docs/rate-limits + models): the values below are the FREE plan and
    # EXACTLY ours. The Developer plan gives 250K TPM / 1K RPM for gpt-oss-120b BUT is currently at FULL CAPACITY
    # (waiting list) — so it is not an option that can be bought. Cached tokens do not count towards the limit.
    "groq":        {"bucket": "free",   "context": 128000, "request_cap": 5000,
                    "rpm": 30, "tpm": 8000, "rpd": 1000, "tpd": 200000,
                    "developer_tpm": 250000, "developer_status": "capacity full (waiting list) 18.09"},
}

# MEANING of the request cap: 'ratio' = a fraction of context (the cap grows with the context); 'absolute' = a RATE limit
# (TPM/request), independent of context but never larger than the model's real context.
CAP_TYPE = {'openrouter': 'ratio', 'claude': 'ratio', 'opencode-go': 'ratio', 'opencode': 'ratio', 'groq': 'absolute'}
CAP_RATIO = 0.78          # the rest of the context is kept for output + margin
CAP_PARENT = 400000         # policy cap: we do not send 780k tokens in one request to a 1M-context model


def request_cap(provider_key, model_entry_name=''):
    """Upper bound that can be sent in one request for this model. The fallback in the table without a catalogue."""
    base = PROVIDER.get(provider_key, {}).get('request_cap', 10 ** 9)
    bg = None
    if model_entry_name:
        try:
            from model_catalog import context
            bg = context(provider_key, model_entry_name)
        except Exception:
            bg = None
    if not bg:
        return base
    if CAP_TYPE.get(provider_key) == 'ratio':
        # The model context is always the upper bound; the floor in the table applies only when there is NO catalogue (B110).
        value = min(CAP_PARENT, int(bg * CAP_RATIO))
    else:
        value = min(base, bg)
    # B90: TPM is also the upper bound of a single request — sending a 25K request to a provider that gives 8K tokens a minute
    # returns "Request too large" and opencode compacts and retries, entering a LOOP (measured:
    # groq 238 KB of log in 17 minutes). Even if the context is large, a small per-minute budget is the limit.
    tpm = PROVIDER.get(provider_key, {}).get('tpm')
    return min(value, int(tpm)) if tpm else value


# --- role -> required STRENGTH (item 1): chains say "which model", this table says "WHY". --------------------
#   strength: high=plan/reasoning · medium=code/review · low=summary/search · context: the role's typical request size
#   frequency: how many times the role runs per run (cost multiplier)
ROLE_STRENGTH = {
    "plan":        {"strength": "high", "context": 200000, "frequency": 1,  "note": "largest context; keeping it on an expensive model consumes the Go budget"},
    "quality":      {"strength": "high", "context": 56000,  "frequency": 1,  "note": "final quality gate; small context, high value; local is the last link"},
    "impl":        {"strength": "middle",   "context": 60000,  "frequency": 5,  "note": "per lane; the role that writes code"},
    "review":      {"strength": "middle",   "context": 40000,  "frequency": 5,  "note": "per lane; must use a different family from impl"},
    "impl-light":  {"strength": "low",  "context": 20000,  "frequency": 3,  "note": "small corrections"},
    "merge": {"strength": "low",  "context": 30000,  "frequency": 2,  "note": "combines lane outputs"},
    "recovery":   {"strength": "middle",   "context": 60000,  "frequency": 1,  "note": "recovers stalled work"},
}


# 4c-1: removed providers; no code path or constant carries them (test_removed_providers).
REMOVED_PROVIDERS = ("deepseek", "cerebras", "openai", "mistral", "google", "groqx")   # 4c-1, 4c-11

# --- role -> preference order ---
OR_SOL, ZEN_SOL = "openrouter/openai/gpt-6.1-sol", "opencode/gpt-6.1-sol"   # Zen Sol is the last link: only when OR is cooling down
CODE_CHAIN = ["opencode-go/deepseek-v4-pro", "openrouter/deepseek/deepseek-v4-pro", "opencode-go/glm-5.3", "openrouter/z-ai/glm-5.3"]
# DECISION (D13): model first, then the cheapest route (by abundance, P7). Free models stay only in the helper
# chain and DO NOT WRITE CODE; when Go is full the same model moves to the Zen/OR route. test: test_role_strength.py
JUDGE_CHAIN = [OR_SOL, "opencode-go/glm-5.3", "openrouter/z-ai/glm-5.3", "opencode-go/qwen3.8-max", "openrouter/qwen/qwen3.8-max-0902",
                 ZEN_SOL, "opencode/glm-5.3", "opencode/qwen3.8-max"]
ROLES = {
    # plan (4c-6): claude (quota rule) › Sol (OR) › glm › qwen (Go) › Zen Sol. glm ahead of qwen: equal intelligence, qwen writes cache on every
    # short call. Zen Sol only when OR Sol is cooling down (chain()).
    "plan":        ["claude/opus#high|claude/sonnet#high", OR_SOL, "opencode-go/glm-5.3", "openrouter/z-ai/glm-5.3", "opencode-go/qwen3.8-max",
                    "openrouter/qwen/qwen3.8-max-0902", ZEN_SOL],
    # code roles (4c-17): DS-pro › glm-5.3 › qwen3.8-max, Go › OR within each model; on a model error the next model is used.
    "impl":        CODE_CHAIN,
    "impl-light":  CODE_CHAIN,
    # N-QA03/N-TS03: the route measured at the last acceptance is used; lack of capacity is not an automatic hand-off to an unmeasured model.
    # Plan review keeps its own pool; the final judge's independence comes from a clean read-only session.
    "review":      list(JUDGE_CHAIN),
    "quality":      [OR_SOL],
    # 4c-9/10: no exploration agent, the lane's own model does the compaction — no separate scout/compaction chain.
    "merge": CODE_CHAIN,
    "recovery":   CODE_CHAIN,
    "main":         [],
}





# short names in the dispatcher plan JSON
SHORT = {"ds-pro": "opencode-go/deepseek-v4-pro", "ds-pro-go": "opencode-go/deepseek-v4-pro",
        "auto": "", "ds-flash": "opencode-go/deepseek-v4-flash",
        "glm-flash": "opencode-go/glm-5.3-flash", "cl-last": "claude/sonnet#high", "cl-opus": "claude/opus#high", "cl-fable": "claude/fable#xhigh",
        "cl-opus-max": "claude/opus#max"}


def provider(model):
    return model.split("/", 1)[0] if model else ""


def budget_read():
    """Item 8: when the tick dropped to 15 s this function started a provider_budget.py PROCESS on every call. Now the
    `observed_budget` shared cache (60 s, on disk, shared by all executors) is tried first; if that fails, the old path."""
    # The shared cache is ONLY for real observation. If the caller injected a simulation/test environment (CADRE_SIM_LOAD,
    # CADRE_TEST_USED7 ...) the cache does not see that environment and answers wrongly — in that case provider_budget.py is run directly.
    SIM_ENVIRONMENT = ("CADRE_SIM_LOAD", "CADRE_TEST_USED7", "CADRE_TEST_USED5", "CADRE_TEST_STALE_MIN")
    if not any(os.environ.get(k) for k in SIM_ENVIRONMENT):
        try:
            from budget_gate import observed_budget
            root = pathlib.Path(__file__).resolve().parent
            data = observed_budget(QE, root.parent)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    try:
        return json.loads(subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve().parent / "provider_budget.py")], capture_output=True, text=True, timeout=30).stdout)
    except Exception:
        return {}


def status_read():
    try:
        return json.loads(STATUS.read_text())
    except Exception:
        return {}


def status_write(d):
    atomic_json(STATUS, d)


def record(**k):
    try:
        if k.get('run_id'):
            from run_store import RunStore, revision_context
            try:
                k = {**revision_context(RunStore(QE).read(k['run_id']), k.get('task_id')), **k}
            except (OSError, ValueError, KeyError):
                pass
        k.setdefault('timestamp', time.time())
        RECORD.parent.mkdir(parents=True, exist_ok=True)
        with RECORD.open("a") as f:
            f.write(json.dumps({"ts": time.strftime("%FT%T"), **k}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def model_key(provider_key, model):
    """Key of a (model, route) record: the full `provider/model` id (the effort suffix `#...` is dropped; no new separator)."""
    entry_name = str(model or "").split("#", 1)[0]
    return entry_name if entry_name.startswith(provider_key + "/") else f"{provider_key}/{entry_name}"


def endpoint_coolings(provider_key, model, d=None, now_ts=None):
    """N-RT04: an outage of one known endpoint does not close the same model's other endpoints."""
    d = d if d is not None else status_read()
    now_ts = time.time() if now_ts is None else now_ts
    endpoints = (d.get(model_key(provider_key, model)) or {}).get('endpoints') or {}
    return {endpoint: float(record.get('cooldown_until') or 0) for endpoint, record in endpoints.items()
            if float(record.get('cooldown_until') or 0) > now_ts}


def in_cooldown(provider_key, d=None, now_ts=None, model=None):
    """Is the provider or (if a model is given) this route of that model in its cooldown period?"""
    d = d if d is not None else status_read(); now_ts = now_ts or time.time()
    if float(d.get(provider_key, {}).get("cooldown_until", 0)) > now_ts:
        return True
    if model and float(d.get(model_key(provider_key, model), {}).get("cooldown_until", 0)) > now_ts:
        return True
    closed = endpoint_coolings(provider_key, model, d, now_ts) if model and provider_key == 'openrouter' else {}
    if closed:
        try:
            entry_name = model_key(provider_key, model).split('/', 1)[1]
            endpoints = json.loads((QE / 'chain/openrouter-price.json').read_text())['models'][entry_name]['providers']
            return bool(endpoints) and all(endpoint in closed for endpoint in endpoints)
        except (OSError, ValueError, KeyError, TypeError):
            return True
    return False


def failure(provider_key, error="", d=None, now_ts=None, allowed_fails=3, model=None, *, endpoint=None):
    with FileLock(STATUS.with_suffix(".lock")):
        return _failure_locked(provider_key, error, d, now_ts, allowed_fails, model, endpoint=endpoint)


# Account-level signal (4c-17): key/balance/monthly cap cut all models at once; the provider closes immediately.
ACCOUNT_SIGNATURES = ("unauthorized", "payment required", "insufficient balance", "insufficient funds",
                  "insufficient credit", "invalid api key", "invalid_api_key", "authentication", "monthly limit",
                  "monthly cap", "monthly spend", "monthly usage limit", "spend limit")
# 4c QA2: 401/402 is an account signature only in an HTTP context; "(401 s)" or "140100 tokens" do not count
ACCOUNT_CODE = re.compile(r"(?:status(?:code)?|code|http|error)[\"'\s:=]*40[12]\b|\b40[12]\s+(?:unauthorized|payment|forbidden)")


def error_category(error):
    h = (error or "").lower()
    if any(k in h for k in ACCOUNT_SIGNATURES) or ACCOUNT_CODE.search(h):
        return "account"
    # Error class separation (B110): a context error and a network outage are NOT QUOTA. Definite quota signatures are checked first.
    if re.search(r"\b429\b", h) or any(k in h for k in ("quota", "quota", "rate limit", "ratelimit", "tpm", "rpm",
                                                         "tokens per", "requests per", "daily limit", "per day")):
        return "quota"
    if any(k in h for k in ("context length", "context_length", "maximum context", "context window", "too large",
                            "too long", "too many tokens", "reduce the length")):
        return "context"
    if any(k in h for k in ("timeout", "timed out", "connection", "econnreset", "epipe", "fetch failed", "network",
                            "temporarily unavailable", "502", "503", "504")):
        return "net"
    return "quota" if any(k in h for k in ("insufficient", "exceeded")) else "error"


def _failure_locked(provider_key, error="", d=None, now_ts=None, allowed_fails=3, model=None, *, endpoint=None):
    """Circuit breaker (4c-17): if the model is known, (model, route) closes first; the provider closes only when >=2 of its models fail or on an
    account-level signal. 429/quota -> 60 min immediately; other errors after `allowed_fails` in a row -> 30 min."""
    d = d if d is not None else status_read(); now_ts = now_ts or time.time()
    category = error_category(error)
    key = model_key(provider_key, model) if model and category != "account" else provider_key
    s = d.setdefault(key, {"error": 0, "cooldown_until": 0, "last": ""})
    endpoint = endpoint if model and provider_key == 'openrouter' and category != 'account' else None
    if endpoint:
        s = s.setdefault('endpoints', {}).setdefault(endpoint, {"error": 0, "cooldown_until": 0, "last": ""})
    s["error"] = int(s.get("error", 0)) + 1
    s["last"] = f"{time.strftime('%F %T', time.localtime(now_ts))} [{category}] {error[:100]}"
    if category in ("quota", "account"):
        s["cooldown_until"] = now_ts + 3600; s["error"] = 0
    elif s["error"] >= allowed_fails:
        s["cooldown_until"] = now_ts + 1800; s["error"] = 0
    if not endpoint and key != provider_key and float(s.get("cooldown_until", 0)) > now_ts:
        from model_catalog import canonical_model
        falling = {canonical_model(k) for k, v in d.items() if k.startswith(provider_key + "/") and isinstance(v, dict)
                 and float(v.get("cooldown_until", 0)) > now_ts}
        if len(falling) >= 2:   # two different models failed on the same route: the problem is in the provider
            p = d.setdefault(provider_key, {"error": 0, "cooldown_until": 0, "last": ""})
            p["cooldown_until"] = max(float(p.get("cooldown_until", 0)), now_ts + 1800)
            p["last"] = f"{time.strftime('%F %T', time.localtime(now_ts))} [model] {len(falling)} model fell"
    status_write(d)
    record(event="failure", provider=provider_key, model=key if key != provider_key else None, endpoint=endpoint, error=error[:120], category=category,
          cooldown_until=s["cooldown_until"])
    return d


def success(provider_key, d=None, model=None):
    """C21: a successful call RESETS the consecutive-error counter (provider and, if given, that (model, route)). Cooldown is not touched."""
    with FileLock(STATUS.with_suffix(".lock")):
        d = d if d is not None else status_read()
        changed = False
        for key in (provider_key, model_key(provider_key, model) if model else None):
            s = d.get(key) if key else None
            if s and int(s.get("error", 0)):
                s["error"] = 0
                changed = True
        if changed:
            status_write(d)
            record(event="success", provider=provider_key, model=model_key(provider_key, model) if model else None)
        return d


OBSERVATION_KEY = {'groq': 'groq', 'opencode-go': 'go', 'opencode': 'zen', 'openrouter': 'or'}


def daily_token_remaining(provider_key, b):
    """B106: the `tpd` column was in the table but read in NO decision. For a provider with a daily token cap,
    the tokens left today; None without an observation (the rule is not applied, not fail-open — just no information)."""
    tpd = PROVIDER.get(provider_key, {}).get('tpd')
    if not tpd or not isinstance(b, dict):
        return None
    line = b.get(OBSERVATION_KEY.get(provider_key, provider_key)) or {}
    used = line.get('today_token')
    if not isinstance(used, (int, float)):
        return None
    return max(0, int(tpd) - int(used))


def has_budget(provider_key, model, b, request_token=8000, role=None, read_only=False, selection=None):
    """Does the provider have budget/capacity for this request? (b: provider_budget.py JSON)
    B112: `role` is required information; the reservation at the gate is made from the role's own pocket."""
    if request_token > request_cap(provider_key, model or ''):
        return False, "request cap"
    remaining_tpd = daily_token_remaining(provider_key, b)
    if remaining_tpd is not None and request_token > remaining_tpd:
        return False, "daily token cap"
    if not b:
        return False, "budget absent"
    try:
        from budget_gate import available_budget, model_bucket, or_pin_price
        from limits import output as role_output
        if provider_key == 'openrouter' and not model.endswith(':free') and not or_pin_price(QE, model.split('/', 1)[-1],
                                                                                         role_output(role or 'lane')):
            return False, 'OR-PIN price missing (plugin rejects it)'   # smoke: the plan made an empty OR attempt on every run
        bucket = model_bucket(provider_key, model.split('/', 1)[-1])
        required = 1
        if selection is not None and provider_key != 'claude':
            from model_selection import call_cap
            try:
                required = max(1, call_cap(model, request_token, role_output(role or 'lane'), QE))
            except ValueError as exc:
                return False, f'model catalog/price missing ({exc}); paid path closed, install-check lists the catalog'
        remaining = available_budget(QE, b, bucket, role=role, model=model.split('/', 1)[-1],
                                     required=required, read_only=read_only)
        if remaining < required:
            return False, 'no verified allocation'
        if provider_key == "claude":
            # C31: NO daily dollar cap; the remaining share (%) of the weekly reserve curve + the shared gate decide.
            cl = b.get("claude", {})
            pay = float(cl.get("pay", 0))
            if not pay > 0:
                return False, "no Claude allocation"
            if "fable" in model:
                from claude_policy import fable_slots
                if fable_slots(QE, b) <= 0:
                    return False, "fable daily 2"
            return True, f"claude pay {pay}% (~${cl.get('available_usd', '?')})"
        if provider_key in ('opencode-go', 'opencode', 'openrouter'):
            return True, 'available allocation at the shared gate'
    except Exception as e:
        return False, f"budget unreadable {e}"
    return False, "undefined"


# D2: candidates in the SAME quality tier can replace each other; tiers are not skipped, free/claude
# are not ordered. The role's "strong" choice (qwen3.8-max plan, deepseek-v4-pro quality) is never reordered — quality is #1.
QUALITY_STRICT = {
    # D13: deepseek-v4-pro's routes are the same quality tier — ordered by abundance (P7); Go first within the model (model_selection).
    "opencode-go/deepseek-v4-pro": 1, "openrouter/deepseek/deepseek-v4-pro": 1,
    "opencode-go/glm-5.2": 1, "opencode-go/glm-5.3-flash": 1,
    "opencode/deepseek-v4-flash": 1, "opencode-go/qwen3.8-flash": 1,
    "openrouter/deepseek/deepseek-v4-flash": 1,   # the same model via OR, $0.03/$0.06 (Zen $0.14/$0.28); abundance orders
}
PAID = ("opencode-go", "opencode", "openrouter")


def abundance(provider_key, b):
    """How much of the provider's share is free (provider_budget.py's `available` / `pay` ratio). 0 if unknown.
    NOTE (T3): this measure is NOT the gate's own — the gate (`budget_gate.allowance`) subtracts a 15% margin. The purpose here is a rough
    "how free" signal for ordering; the admission decision always belongs to the gate."""
    try:
        key = {"opencode-go": "go"}.get(provider_key)
        v = (b or {}).get(key) or {}
        base = float(v.get("pay") or 0)
        return (float(v.get("available") or 0) / base) if base > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


# T3 fix: the threshold was 1.0 — after the day's FIRST spending every provider became "tight" and the order flipped every day
# (the check ledger B4/B7/B9 caught this). Now only a provider that is REALLY about to run out (less than 25% of its share left)
# falls back.
COMPACT_THRESHOLD = 0.25

# Hysteresis: the order changes not on every call but when the difference is CLEAR and SUSTAINED. Otherwise the day's first spending
# could flip the order and change model mid-job (two halves of the same job split across two models).
HYSTERESIS_SEC = float(os.environ.get("CADRE_ORDER_HYSTERESIS_SEC", 300))
HYSTERESIS_DIFFERENCE = float(os.environ.get("CADRE_ORDER_HYSTERESIS_DIFF", 0.20))


def _order_status(d):
    s = d.get("_order")
    return s if isinstance(s, dict) else {}


def is_compact(provider_key, b, d, now_ts):
    """Should the provider fall back within its own quality tier? Measure: less than a quarter of its share left.
    Hysteresis: the settled decision changes only when the new trend has BOTH lasted HYSTERESIS_SEC AND is clear."""
    divide = abundance(provider_key, b)
    raw = divide < COMPACT_THRESHOLD
    status = _order_status(d)
    record = status.get(provider_key)
    if not isinstance(record, dict):
        status[provider_key] = {"compact": raw, "candidate": raw, "ts": now_ts}
        d["_order"] = status
        return raw
    if raw == bool(record.get("compact")):
        if record.get("candidate") != raw:                       # the trend went back to the settled decision
            record.update(candidate=raw, ts=now_ts)
            d["_order"] = status
        return raw
    if record.get("candidate") != raw:                           # a new trend started: the clock is set now
        record.update(candidate=raw, ts=now_ts)
        d["_order"] = status
        return bool(record.get("compact"))
    sufficient_duration = now_ts - float(record.get("ts") or now_ts) >= HYSTERESIS_SEC
    distinct = abs(divide - COMPACT_THRESHOLD) >= HYSTERESIS_DIFFERENCE
    if sufficient_duration and distinct:
        record.update(compact=raw, candidate=raw, ts=now_ts)
        d["_order"] = status
        return raw
    return bool(record.get("compact"))


def abundance_by_sort(candidates, b, d=None, now_ts=None, role="impl"):
    """G-E1/P7: order by net share / typical call cost within the same quality group.
    Without a verified net share the old tightness signal is the fallback; on a tie the hand-written order is kept."""
    ordered = list(candidates)
    from budget_gate import BudgetDenied
    index = {m: i for i, m in enumerate(ordered)}
    layers = {}
    for m in ordered:
        multiplier = QUALITY_STRICT.get(m)
        if multiplier is not None and provider(m) in PAID:
            layers.setdefault(multiplier, []).append(m)
    def key(m):
        multiplier = QUALITY_STRICT.get(m)
        if provider(m) not in PAID or multiplier is None or len(layers.get(multiplier, [])) < 2:
            return (index[m], 0, index[m])                     # left untouched
        anchor_index = min(index[k] for k in layers[multiplier])
        compact = 1 if is_compact(provider(m), b, status, observed_at) else 0
        return (anchor_index, compact, index[m])
    status = d if isinstance(d, dict) else {}
    observed_at = now_ts if now_ts is not None else time.time()
    for models in layers.values():
        places = [index[m] for m in models]
        points = {}
        try:
            for m in models:
                provider_key = provider(m)
                expense = job_cost(role, provider_key, b)
                if not 0 < expense < float('inf'):
                    raise ValueError('invalid typical expense')
                points[m] = net_capacity_usd(b, provider_key, m) / expense
            fresh = sorted(models, key=lambda m: -points[m])
        except (BudgetDenied, ValueError, OSError):
            fresh = sorted(models, key=key)
        for i, m in zip(places, fresh):
            ordered[i] = m
    return ordered


def role_context(role):
    """B121: the role's DECLARED typical request size. `ROLE_STRENGTH[role]['context']` was in the table but
    read in no decision; call sites passed hand-written numbers (20,000 for compaction,
    while the declaration was 150,000). Now a caller that does not know uses this number."""
    return int((ROLE_STRENGTH.get(role) or ROLE_STRENGTH.get("impl") or {}).get("context", 8000))


LADDER_RESERVE, LADDER_ACTIVE = 0.5, 1.25   # budget QA (user decision): out if not enough for half a job, active if it covers a job comfortably
ROLE_BUCKET = {"plan": "plan", "quality": "judge", "review": "helper"}
JOB_COST_FALLBACK = {"plan": 0.03, "lanes": 0.06, "judge": 0.07, "helper": 0.01}   # ledger p75; without a measurement


def job_cost(role, provider_key, b):
    """The role's typical job cost (USD): the provider_budget.py ledger measurement (provider, otherwise all providers), otherwise the default."""
    bucket = ROLE_BUCKET.get(role, "lanes")
    measurement = ((b or {}).get("job_cost") or {}).get(bucket) or {}
    try:
        return float(measurement.get(provider_key) or measurement.get("_") or JOB_COST_FALLBACK[bucket])
    except (TypeError, ValueError):
        return JOB_COST_FALLBACK[bucket]


def gate_reservation(role, provider_key, b):
    """B221: the typical reservation the gate holds for a single call on this provider (USD; role bucket, otherwise provider-wide); 0 without a measurement."""
    bucket = ROLE_BUCKET.get(role, "lanes")
    k = ((b or {}).get("calibrated") or {}).get(bucket) or {}
    r = (b or {}).get("reservation") or {}
    try:
        if provider_key == "claude" and k.get(provider_key):   # B253: only the Claude limit holds with the calibrated cap; the other gates use the old rule
            return float(k[provider_key])
        return float(((r.get(bucket) or {}).get(provider_key)) or (r.get("*") or {}).get(provider_key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def provider_ladder(provider_key, role, b, model=None):
    """'active' | 'reserve' | 'outside'; None for a provider without a dollar share or with an unreadable budget."""
    from budget_gate import PAID, allowance
    if provider_key not in PAID:
        return None
    try:
        remaining = allowance(b, provider_key, model=model)[1] / 1_000_000
    except Exception:
        return None   # the chain's budget filter removes an unreadable budget
    resv = gate_reservation(role, provider_key, b)
    j = max(job_cost(role, provider_key, b), resv)
    if remaining < max(LADDER_RESERVE * j, resv):   # B221: money that does not cover a single gate call is denied on every attempt
        return "outside"
    return "reserve" if remaining < LADDER_ACTIVE * j else "active"


def ladder(candidate, role, b):
    """Budget ladder: if the money that can be spent today is not enough for half a typical job in that role it is out (money accumulates), less than 1.25
    jobs it is a fallback, more is active. Free models and Claude are outside the rule (None); a paid model with unknown price counts (G-B8)."""
    provider_key = provider(candidate)
    if not candidate or provider_key == "claude":
        return None
    try:
        from model_catalog import is_free
        if is_free(provider_key, candidate.split("/", 1)[1].split("#")[0]) is True:
            return None
    except Exception:
        pass
    return provider_ladder(provider_key, role, b, candidate.split("/", 1)[-1].split("#")[0])


def writer_breath(candidate, b, *, read_only=False):
    """B239: if a paid candidate's remaining share does not cover the gate reservation (p50) of two lane calls, returns (remaining, needed); if enough,
    free or unmeasured, None (the candidate stays)."""
    return _breath(candidate, b, "impl", read_only=read_only)


def judge_breath(candidate, b):
    """B242: the judge also makes at least two calls (tool + verdict); same measure with the judge bucket and the run's judge pocket."""
    return _breath(candidate, b, "quality")


def recovery_breath(candidate, b):
    """B277: the rescuer takes over the half-done SESSION; the share must cover at least CADRE_RECOVERY_BREATH (6) calls."""
    try:
        n = max(2, int(os.environ.get("CADRE_RECOVERY_BREATH", "6")))
    except ValueError:
        n = 6
    return _breath(candidate, b, "recovery", n)


def _breath(candidate, b, role, call=2, *, read_only=False):
    from budget_gate import PAID
    provider_key = provider(candidate)
    if not candidate or (provider_key not in PAID and provider_key != "claude"):
        return None
    try:
        from model_catalog import is_free
        if is_free(provider_key, candidate.split("/", 1)[1].split("#")[0]) is True:
            return None
    except Exception:
        pass
    resv = pocket_resv = gate_reservation(role, provider_key, b)
    if provider_key == "openrouter":
        try:
            from limits import OUTPUT
            pin = json.loads((QE / "chain/openrouter-price.json").read_text())["models"][candidate.split("/", 1)[1].split("#")[0]]
            resv = max(resv, (role_context(role) * float(pin["input"]) + OUTPUT.get(role, OUTPUT["lane"]) * float(pin["output"])) / 1e6)
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            pass
    if resv <= 0:
        return None
    try:
        remaining = net_capacity_usd(b, provider_key, candidate)
    except Exception:
        return None
    micro = lambda usd: round(usd * 1_000_000)   # floating point: 6 x 0.1 must not exceed 0.6 (an exactly sufficient share is enough)
    if micro(remaining) < micro(call * resv):
        return (remaining, call * resv)
    pocket = run_pocket_remaining(role, provider_key)   # B239b (EXTRA3/R13-v4): even if the provider share is plentiful, if the RUN POCKET does not cover two calls the first call empties the pocket
    if pocket is not None and micro(pocket) < micro(call * pocket_resv) and os.environ.get('CADRE_RUN_ID'):
        from budget_gate import available_budget
        pocket = available_budget(QE, b, provider_key, role=role, required=micro(call * pocket_resv), read_only=read_only) / 1e6
    if pocket is not None and micro(pocket) < micro(call * pocket_resv):
        return (pocket, call * pocket_resv, "pocket")
    return None


def net_capacity_usd(b, provider_key, candidate=""):
    """B289: breathing room is not the raw share but the ledger's net capacity (share - receipts - pending reservations of other runs).
    On Go points are converted to this model's list dollars (4c-13)."""
    from budget_gate import BudgetLedger
    model = candidate.split("/", 1)[-1] if candidate else None
    remaining, ledger = allowance_usd(b, provider_key, model), QE / "chain/request-budget.db"
    return min(remaining, BudgetLedger(ledger).available(provider_key, b, model=model, read_only=True) / 1_000_000) if ledger.exists() else remaining


def allowance_usd(b, provider_key, model=None):
    from budget_gate import allowance
    return allowance(b, provider_key, model=model)[1] / 1_000_000


def run_pocket_remaining(role="impl", provider=""):
    """USD left in the active run's pocket for this role; None without a run or a cap."""
    rid = os.environ.get("CADRE_RUN_ID")
    if not rid:
        return None
    try:
        from budget_gate import run_remaining_usd
        return run_remaining_usd(QE, rid, role, provider)
    except Exception:
        return None


def ladder_order(selected, level, last_care):
    """Fallbacks go just before the last resort (the table's last link), at the very end if the last resort is not in the list."""
    reserve = [m for m in selected if level.get(m) == "reserve"]
    others = [m for m in selected if level.get(m) != "reserve"]
    i = others.index(last_care) if last_care in others else len(others)
    return others[:i] + reserve + others[i:]


def claude_closed() -> bool:
    """06.10: if `CADRE_CLAUDE_PLAN=0` or the `chain/claude-closed` file exists, Claude enters no chain (quota protected)."""
    return os.environ.get("CADRE_CLAUDE_PLAN") == "0" or (QE / "chain/claude-closed").exists()


def role_candidates(role):
    run_id = os.environ.get('CADRE_RUN_ID')
    if run_id:
        from run_store import RunStore, request_root_id, request_authorized
        store = RunStore(QE)
        try:
            record = store.read(request_root_id(store, run_id))
        except (OSError, ValueError, KeyError, TypeError):
            return []
        permission = record.get('request_permission') or {}
        if record.get('request') and permission.get('status') == 'approved':
            # N-IN01/N-RT01: a restart or a child process does not derive the approved pool
            # from the terminal's temporary chain or from the general default.
            if not request_authorized(record):
                return []
            key = {'writer': 'plan', 'impl-light': 'impl'}.get(role, role)
            return list(permission['action']['models'].get(key, []))
    for lookup_key in ((role, "plan") if role == "writer" else (role,)):
        special = [m.strip() for m in os.environ.get("CADRE_CHAIN_" + lookup_key.upper().replace("-", "_"), "").split(",") if m.strip()]
        if special:
            return special
    return ROLES["plan"] if role == "writer" else ROLES.get(role, ROLES["impl"])


def chain(role, category="middle", request_token=None, b=None, d=None, now_ts=None, save=True):
    """Ordered model chain for the role; a provider that does not fit the request is removed (B120).
    `request_token=None`: the caller does not know the real size, the role's declared typical size is used (B121).
    Returns (list, reasons)."""
    request_token = role_context(role) if request_token is None else request_token
    b = b if b is not None else budget_read(); d = d if d is not None else status_read(); now_ts = now_ts or time.time()
    candidates = role_candidates(role)   # B239: report writer = plan chain + breathing filter
    from model_selection import run_selection, policy_order
    selection, policy_reason = None, []
    run_id = os.environ.get('CADRE_RUN_ID')
    if run_id:
        try:
            selection = run_selection(QE, run_id)
            if selection is not None:
                candidates, reason = policy_order(candidates, role, selection)
                policy_reason.append(reason)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            reason = ['selection-policy-invalid: recorded preference or difficulty is unreadable']
            if save:
                record(event='chain', role=role, category=category, request_token=request_token, chain=[], reason=reason,
                      run_id=run_id, task_id=os.environ.get('CADRE_TASK_ID') or None)
            return [], reason
    if claude_closed():   # the user wanted to protect the Claude quota; the plan chain continues with Sol/Go/Zen
        candidates = [m for m in candidates if not m.startswith("claude/")]
    selected, cause = [], policy_reason
    for candidate in candidates:
        if "|" in candidate:  # claude: opus (remaining share of the weekly reserve >= 4 points) / sonnet — the same threshold as claude_policy.model
            opus, sonnet = candidate.split("|")
            try:
                candidate = opus if float((b or {}).get("claude", {}).get("pay", 0)) >= 4.0 else sonnet
            except (TypeError, ValueError):
                candidate = sonnet
        provider_key = provider(candidate)
        # 0c: the catalogue is a SUPERSET — putting a model the account cannot use into the chain means the call fails with 401
        # and wastes a link of the chain. If a live list exists we intersect with it, otherwise we leave it.
        try:
            from model_catalog import available
            if not available(provider_key, candidate.split("/", 1)[1] if "/" in candidate else candidate):
                cause.append(f"{candidate}: unavailable on this account"); continue
        except Exception:
            pass
        if role == "plan" and category in ("trivial", "small") and provider_key == "claude":
            cause.append(f"{candidate}: Claude is not used for small jobs"); continue
        if in_cooldown(provider_key, d, now_ts):
            cause.append(f"{candidate}: cooldown"); continue
        if in_cooldown(provider_key, d, now_ts, candidate):
            cause.append(f"{candidate}: model cooldown (this route failed)"); continue
        if candidate == ZEN_SOL and not (in_cooldown("openrouter", d, now_ts) or in_cooldown("openrouter", d, now_ts, OR_SOL)):
            cause.append(f"{candidate}: Zen Sol is available only while OR Sol is cooling down (4c-3)"); continue
        ok, why = has_budget(provider_key, candidate, b, request_token, role=role, read_only=not save, selection=selection)   # B112: the pocket comes from the role
        if not ok:
            cause.append(f"{candidate}: {why}"); continue
        selected.append(candidate); cause.append(f"{candidate}: OK ({why})")
    _order_once = json.dumps(d.get("_order"), sort_keys=True) if isinstance(d, dict) else None
    selected = abundance_by_sort(selected, b, d, now_ts, role=role)
    level = {m: ladder(m, role, b) for m in selected}   # last step: tier ordering does not undo the ladder
    cause += [f"{m}: ladder — current funds do not cover half a job in this role; accumulating" for m in selected if level[m] == "outside"]
    selected = ladder_order([m for m in selected if level[m] != "outside"], level, candidates[-1] if candidates else None)
    if role == "writer":   # B239 (R13-v4): the writer is a long lane; no attempt on a paid provider whose remaining share does not cover two lane calls
        remaining = []
        for m in selected:
            breath = writer_breath(m, b, read_only=not save)
            if breath is None:
                remaining.append(m)
            else:
                source = "run pocket" if len(breath) > 2 else "remaining allocation"
                cause.append(f"{m}: writer — {source} ${breath[0]:.2f} does not cover two lane calls (${breath[1]:.2f})")
        selected = remaining
    # Item 7: if the hysteresis memory is not written to disk every call is the "first call" and hysteresis never works.
    if save and isinstance(d, dict) and json.dumps(d.get("_order"), sort_keys=True) != _order_once:
        try:
            with FileLock(STATUS.with_suffix(".lock")):
                permanent = status_read()
                permanent["_order"] = d.get("_order")
                status_write(permanent)
        except Exception:
            pass
    from model_selection import sort
    selected, choice_reason = sort(selected, candidates, role, request_token, QE, now_ts)
    last_care = []
    for candidate in reversed(candidates):
        if provider(candidate) != "opencode":
            break
        last_care.insert(0, candidate)
    selected = [m for m in selected if m not in last_care] + [m for m in last_care if m in selected]
    cause.extend(choice_reason)
    if save:
        record(event="chain", role=role, category=category, request_token=request_token, chain=selected, reason=cause,  # B50: the decision is bound to the run/task, reasons are not dropped
              run_id=os.environ.get("CADRE_RUN_ID") or None, task_id=os.environ.get("CADRE_TASK_ID") or None,
              selection=selection)
    return selected, cause


WAIT_LEVELS = ("hard", "very-hard", "ultra")
WAIT_THRESHOLD_SEC = 7200   # design 3.3: wait if <=2 h remain until renewal, continue with what is available if longer


def is_strong(model):
    """Design 3.3: strong = a paid cloud model with budget (claude included); free models do not count as strong."""
    if not model:
        return False
    provider_key = provider(model)
    from budget_gate import PAID
    if provider_key != "claude" and provider_key not in PAID:
        return False
    try:
        from model_catalog import is_free
        return is_free(provider_key, model.split("/", 1)[1].split("#")[0]) is not True
    except Exception:
        return True


def renewing_remaining(now_ts=None):
    """Seconds left until the local midnight when daily shares renew (the same local day as bucket_day_beginning)."""
    from datetime import datetime, timedelta
    now_ts = time.time() if now_ts is None else now_ts
    night = datetime.fromtimestamp(now_ts).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return max(0, int(night.timestamp() - now_ts))


def cloud_wait(role, category, points, now_ts=None, threshold=None):
    """Seconds to wait (0 = no wait): only at hard+ level, if the chain's first link is not strong and the renewal is near."""
    if threshold is None:   # B268: CADRE_WAIT_THRESHOLD_SEC=0 disables waiting (e2e fixtures hung on the clock)
        try:
            threshold = int(os.environ.get("CADRE_WAIT_THRESHOLD_SEC", WAIT_THRESHOLD_SEC))
        except ValueError:
            threshold = WAIT_THRESHOLD_SEC
    from level import level
    if level(points)["entry_name"] not in WAIT_LEVELS:
        return 0
    z, _ = chain(role, category, 30000)
    if z and is_strong(z[0]):
        return 0
    remaining = renewing_remaining(now_ts)
    return remaining + 60 if remaining <= threshold else 0


def same_model_paths(existing, role="impl", request_token=None, b=None, forbidden=(), itself=False):
    """GO1/LIVE1: open routes that can carry the same session without changing model."""
    from model_catalog import canonical_model
    id = canonical_model(existing)
    candidates, _ = chain(role, request_token=request_token, b=b)
    return [m for m in candidates if (itself or m != existing) and provider(m) not in forbidden
            and not m.startswith("claude/")
            and canonical_model(m) == id]


def next_model(existing, role="impl", request_token=None, b=None, forbidden=()):
    """4c-17: if the same model has no open route, the first open route of another model waiting in the chain (filtered by (model, route))."""
    from model_catalog import canonical_model
    id = canonical_model(existing)
    candidates, _ = chain(role, request_token=request_token, b=b)
    return next((m for m in candidates if provider(m) not in forbidden and not m.startswith("claude/")
                 and canonical_model(m) != id), None)


def next_link(role, existing, **kw):
    z, _ = chain(role, **kw)
    if existing in z:
        i = z.index(existing)
        return z[i + 1] if i + 1 < len(z) else None
    return z[0] if z and z[0] != existing else None


def alternatives(role, existing, n=3, **kw):
    """B281/4c-17: the next links to try for a half lane, in chain order, at most n: excluding the failed (model, route); another
    model of the same provider is a candidate too (the provider leaves the chain only when >=2 of its models fail)."""
    if n <= 0:   # B298
        return []
    z, _ = chain(role, **kw)
    falling = model_key(provider(existing), existing) if existing else ""
    return [m for m in z if model_key(provider(m), m) != falling][:n]


# D13: expensive models are used only in the plan/judge role and at difficulty 5; at lower difficulty
# the chain is built with these candidates removed (a difficulty 2 job does not go to an expensive model).
EXPENSIVE = {"openrouter/openai/gpt-6.1-sol", "openrouter/deepseek/deepseek-v4-pro",
          "opencode-go/deepseek-v4-pro", "opencode-go/qwen3.8-max"}


def difficulty_model(m0: str, difficulty: int, role: str = "impl") -> str | None:
    """D13: difficulty 1-2 -> the impl-light chain, others the role chain; m0 is kept if it is in the chain, otherwise the first link
    (None = no capacity)."""
    target = "impl-light" if 1 <= difficulty <= 2 else role
    z, _ = chain(target, "middle", None)
    if not z:
        return None
    if os.environ.get('CADRE_RUN_ID'):
        from model_selection import run_selection
        try:
            if run_selection(QE, os.environ['CADRE_RUN_ID']) is not None:
                return z[0]
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return None
    return m0 if m0 in z else z[0]


def config_write(file, b=None, d=None, extra_permission=None):
    """OPENCODE_CONFIG for sub-agents (the first link of each role's chain); compaction is the lane's model (4c-10)."""
    cfg = {"$schema": "https://opencode.ai/config.json", "agent": {}}
    for agent, role in (("cadre-plan", "plan"), ("cadre-review", "review"), ("cadre-impl", "impl")):
        z, _ = chain(role, "middle", None, b, d)
        m = next((x for x in z if not x.startswith("claude/")), None)
        cfg["agent"][agent] = {"model": m} if m else {"disable": True}
    if extra_permission:
        cfg["agent"]["build"] = {"permission": {"bash": extra_permission}}
    p = pathlib.Path(file); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=1))
    return str(p), cfg


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__); sys.exit(2)
    cmd = a[0]
    if cmd == "chain":
        z, n = chain(a[1], a[2] if len(a) > 2 else "middle", int(a[3]) if len(a) > 3 else None)
        print(json.dumps({"chain": z, "reason": n}, ensure_ascii=False))
    elif cmd == "first":
        z, _ = chain(a[1], a[2] if len(a) > 2 else "middle", int(a[3]) if len(a) > 3 else None)
        if not z:
            print('no-capacity', file=sys.stderr); sys.exit(76)
        print(z[0])
    elif cmd == "alternatives":
        print("\n".join(alternatives(a[1], a[2], int(a[3]) if len(a) > 3 else 3)))
    elif cmd == "next":
        m = next_link(a[1], a[2])
        if m is None:
            print('no-capacity', file=sys.stderr); sys.exit(76)
        print(m)
    elif cmd == "wait":
        print(cloud_wait(a[1], a[2], a[3] if len(a) > 3 else 0))
    elif cmd == "failure":   # if `provider/model` is given the record is at (model, route) level (4c-17)
        failure(provider(a[1]), a[2] if len(a) > 2 else "", model=a[1] if "/" in a[1] else None); print("ok")
    elif cmd == "config":
        p, _ = config_write(a[1]); print(p)
    elif cmd == "short":
        print(SHORT.get(a[1], ""))
    elif cmd == "suitable":
        # suitable <model|empty> <role> [difficulty]: move a model with budget/closed to the first link of the chain ('' = the router chooses).
        # S5: without a preflight the plan assigned tasks to a provider without budget; this check prevents that.
        m0 = a[1] if len(a) > 1 else ""; role = a[2] if len(a) > 2 else "impl"
        difficulty = int(a[3]) if len(a) > 3 and a[3].isdigit() else 0
        m = difficulty_model(m0, difficulty, role)
        if m is None:
            print('no-capacity', file=sys.stderr); sys.exit(76)
        print(m)
    elif cmd == "status":
        print(json.dumps(status_read(), indent=1))
    else:
        print(__doc__); sys.exit(2)
