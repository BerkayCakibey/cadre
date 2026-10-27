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

WINDOWS = ('rolling', 'weekly', 'monthly')
TTL = float(os.environ.get('CADRE_GO_WINDOW_TTL', 120))
BASE_PAY = 5.0  # GO1: new work closes at 95%; the total wall margin is at most five points.
HISTORY_N = 20
API_AGE_SEC = 2 * 3600
ENDPOINT = 'https://opencode.ai/zen/go/v1/usage'

# One pool (4c-13): window percentage = Σ spending ÷ (share × the model's monthly limit). Source opencode.ai/docs/go (07.10):
# 5 h = 20% of the monthly limit, a week 50%; the limit is the model's weight in the pool. 54/18/2 reproduced exactly from the DB.
WINDOW_SHARE = {'rolling': 0.2, 'weekly': 0.5, 'monthly': 1.0}
MODEL_LIMIT_USD = {
    **dict.fromkeys(('deepseek-v4-pro', 'deepseek-v4-flash-vision-exp', 'glm-5.3', 'kimi-k3', 'qwen3.8-max', 'mimo-v2.6-pro',
                     'mimo-v2.5-pro', 'grok-4.7', 'grok-4.6', 'gpt-6-luna', 'gpt-5.6-luna'), 15.0),
    **dict.fromkeys(('deepseek-v4-flash', 'qwen3.8-flash', 'hy4-preview', 'space-bunny'), 30.0),
    **dict.fromkeys(('deepseek-v4.1-flash', 'glm-5.3-flash', 'glm-5.2', 'kimi-k2.7-code', 'kimi-k2.6', 'longcat-2.0',
                     'mimo-v2.6-flash', 'mimo-v2.5', 'minimax-m3', 'minimax-m2.7', 'muse-spark-1.3-contributor',
                     'muse-spark-1.2-contributor', 'qwen3.7-plus', 'hy3'), 60.0),
}
DENSE_HOURS = ((1, 4), (6, 10))   # DeepSeek peak hours (UTC, Mon–Fri): the Go pool charges 2× points during these hours


def model_limit(model: str) -> float:
    """An unknown model counts as the heaviest (lowest limit): it over-charges points and never exceeds the pool."""
    return MODEL_LIMIT_USD.get(str(model or '').split('/')[-1], min(MODEL_LIMIT_USD.values()))


def dense_hour(ts: float) -> bool:
    t = time.gmtime(ts)
    return t.tm_wday < 5 and any(start <= t.tm_hour < last for start, last in DENSE_HOURS)


def moment_factor(model: str, ts: float) -> int:
    """The catalogue price for DeepSeek is off-peak; at peak hours Go charges 2× points for the same call."""
    return 2 if str(model or '').split('/')[-1].startswith('deepseek') and dense_hour(ts) else 1


def points(model: str, usd: float) -> float:
    """Monthly points from list dollars (including the peak multiplier): 1 point = 1% of the pool."""
    return float(usd) * 100.0 / model_limit(model)


def usd(model: str, points_: float) -> float:
    return float(points_) * model_limit(model) / 100.0


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
    total = 0.0
    for expense, model, observed_at in rows:
        try:
            value = float(expense or 0)
        except (TypeError, ValueError):
            return None
        total += points(model, value * moment_factor(model, observed_at / 1000))
    return total


def _iso(value: Any) -> float | None:
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        return None


def own_percentages(db: Path, now: float, resets: dict[str, Any] | None, month_start: float) -> dict[str, float] | None:
    """Window percentages without the API (Σ formula). If the week start is unknown the last 7 days are counted (over-counts, cautious)."""
    resets = resets or {}
    # 4c-19 reality probe: the 5-hour window is not a fixed "last 5 hours"; it counts back from the API's reset time
    rolling_reset = _iso(resets.get('rolling'))
    if rolling_reset and rolling_reset > now:
        rolling_start = rolling_reset - 5 * 3600
    elif rolling_reset and now - rolling_reset < 5 * 3600:   # the cached reset has passed: the new window starts at that moment at the earliest
        rolling_start = rolling_reset
    else:
        rolling_start = now - 5 * 3600
    week_last = _iso(resets.get('weekly'))
    week_start = week_last - 7 * 86400 if week_last and week_last > now else now - 7 * 86400
    month_last = _iso(resets.get('monthly'))
    if month_last and month_last > now:
        from datetime import datetime
        last = datetime.fromtimestamp(month_last)
        year, month = (last.year, last.month - 1) if last.month > 1 else (last.year - 1, 12)
        import calendar
        month_start = last.replace(year=year, month=month, day=min(last.day, calendar.monthrange(year, month)[1])).timestamp()
    values = {'rolling': sigma(db, rolling_start, now), 'weekly': sigma(db, week_start, now), 'monthly': sigma(db, month_start, now)}
    if any(v is None for v in values.values()):
        return None
    return {p: round(v / WINDOW_SHARE[p], 4) for p, v in values.items()}


def remaining_points(data: dict[str, Any] | None, then: float = 0.0) -> float | None:
    """Monthly points left before the wall: in each window (100 − wall − percentage) × share, the narrowest; spending after the reading is subtracted."""
    if not data:
        return None
    try:
        remaining = min((100.0 - BASE_PAY - float(data.get(p) or 0)) * WINDOW_SHARE[p] for p in WINDOWS)
    except (TypeError, ValueError):
        return None
    return max(0.0, remaining - max(0.0, float(then or 0)))


def path(home: Path) -> Path:
    return home / 'chain/go-window.json'


def _percent(raw: Any) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw) or raw < 0:
        raise ValueError('go-window-shape-invalid')
    return float(raw)


def _cache(home: Path) -> dict[str, Any]:
    try:
        data = json.loads(path(home).read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            return {}
        stamp = _percent(data['ts'])
        if not 0 < stamp <= time.time() + 60:
            return {}
        for p in WINDOWS:
            _percent(data[p])
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _write(home: Path, data: dict[str, Any]) -> None:
    p = path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    temporary = p.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    temporary.replace(p)


def _key() -> str:
    return json.loads((Path.home() / '.local/share/opencode/auth.json').read_text())['opencode-go']['key']


def _pull(timeout: float) -> dict[str, Any]:
    if os.environ.get('CADRE_NO_NET') == '1':   # an isolated test home does not go to the network
        raise OSError('CADRE_NO_NET')
    # behind Cloudflare: without a User-Agent it returns 403 (code 1010) — not an identity problem.
    request = urllib.request.Request(ENDPOINT, headers={'Authorization': 'Bearer ' + _key(), 'User-Agent': 'opencode/1.0'})
    with urllib.request.urlopen(request, timeout=timeout) as r:
        return json.load(r)


def read(home: Path, fresh: bool = False, timeout: float = 10.0) -> dict[str, Any] | None:
    """{'rolling': %, 'weekly': %, 'monthly': %, 'resets': {...}, 'ts': …, 'history': [...]}; None if unknown."""
    cache = _cache(home)
    if not fresh and cache.get('ts') and time.time() - float(cache['ts']) < TTL:
        return cache or None
    try:
        raw = _pull(timeout)
        usage = raw['usage']
        percent = {}
        for p in WINDOWS:
            percent[p] = _percent(usage[p]['percent'])
        resets = {p: usage[p].get('resetsAt') for p in WINDOWS}
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
        if cache.get('ts') and time.time() - float(cache['ts']) >= API_AGE_SEC:
            return None
        return cache or None                      # no network: use the last known value, invent nothing
    history = list(cache.get('history') or [])
    history.append({'ts': time.time(), **percent})
    data = {'ts': time.time(), **percent, 'resets': resets, 'history': history[-HISTORY_N:]}
    try:
        _write(home, data)
    except OSError:
        pass
    return data


def wall_share(data: dict[str, Any]) -> float:
    """GO1: a provider route hand-off keeps the remaining work; five points are reserved for new work."""
    return BASE_PAY


DIVERGENCE_THRESHOLD = float(os.environ.get('CADRE_WINDOW_DIVERGENCE', 20))


def cross_check(data: dict[str, Any] | None, own_percent: float | None) -> tuple[dict[str, Any] | None, str]:
    """S11: if the provider's percentage and our own measurement diverge, the larger one (the one saying less can be spent) wins;
    if the difference exceeds the threshold a warning text is returned (shown in the morning summary)."""
    if not data:
        return data, ''
    try:
        endpoint_percent = float(data.get('monthly') or 0)
    except (TypeError, ValueError):
        return data, ''
    if own_percent is None:
        return data, ''
    try:
        own = float(own_percent)
    except (TypeError, ValueError):
        return data, ''
    if not 0 <= own <= 1000:
        return data, ''
    difference = abs(own - endpoint_percent)
    if difference < DIVERGENCE_THRESHOLD:
        return data, ''
    to_be_used = max(endpoint_percent, own)
    fresh = {**data, 'monthly': to_be_used, 'monthly_endpoint': endpoint_percent, 'monthly_own': own}
    return fresh, (f'monthly deviation {difference:.0f} points: provider {endpoint_percent:.0f}%, own measurement {own:.0f}%'
                  f' -> used the conservative value ({to_be_used:.0f}%)')


def status(data: dict[str, Any] | None) -> tuple[bool, str]:
    """(open?, reason). Without data (True, 'unknown') — unknown alone does not close; the 429 stamp works separately."""
    if not data:
        return True, 'unknown'
    pay = wall_share(data)
    percentages = {}
    for p in WINDOWS:
        try:
            percentages[p] = float(data.get(p, 0) or 0)
        except (TypeError, ValueError):
            continue
    # The "full" state first: it is a stronger statement and must not get lost under the wall warning.
    for p, percent in percentages.items():
        if percent >= 100:
            return False, f'{p} 100% — pool closed'
    for p, percent in percentages.items():
        if percent + pay >= 100:
            return False, f'{p} {percent:.0f}% + wall share {pay:.0f} >= 100'
    return True, 'open'


def summary(home: Path, data: dict[str, Any] | None = None) -> str:
    """One line for the status screen. Does NOT go to the network: only reads the cache (the status screen must stay fast)."""
    data = data if data is not None else (_cache(home) or None)
    if not data:
        return ''
    open, reason = status(data)
    return (f"rolling %{float(data.get('rolling') or 0):.0f} · weekly %{float(data.get('weekly') or 0):.0f}"
            f" · monthly %{float(data.get('monthly') or 0):.0f} · wall share {wall_share(data):.0f}"
            f" · {'open' if open else 'CLOSED: ' + reason}")


if __name__ == '__main__':
    import sys
    home_dir = Path(os.environ.get('CADRE_HOME', Path.home() / '.local/share/cadre-engineering'))
    if '--summary' in sys.argv:
        line = summary(home_dir)
        if line:
            print(line)
        raise SystemExit(0)
    data = read(home_dir, fresh='--fresh' in sys.argv)
    line = summary(home_dir, data)
    print(line or 'window unknown (no network and empty cache)')
