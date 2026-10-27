#!/usr/bin/env python3
"""Model catalog: context, price and free/paid classification per model from one place (opencode's copy of models.dev).
Context is a MODEL property; if the catalogue is missing/broken the caller falls back to the old table. cost.input == cost.output == 0 means the free bucket."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from run_store import atomic_json

# Our provider id -> the id in the catalogue.
ID = {'opencode': 'opencode', 'opencode-go': 'opencode-go', 'zen': 'opencode', 'groq': 'groq'}

_cache: dict[str, Any] = {}


# 4c-7/17: some routes offer the same model under a versioned name (OR `qwen/qwen3.8-max-0902` = Go `qwen3.8-max`).
MODEL_ALIASES = {'qwen3.8-max-0902': 'qwen3.8-max'}


def canonical_model(entry_name: str) -> str:
    """B344: transport/vendor prefixes do not give the same model an independent judge identity."""
    model = entry_name.rsplit('/', 1)[-1].split('#', 1)[0].removesuffix(':free')
    return re.sub(r'[^a-z0-9]', '', MODEL_ALIASES.get(model.lower(), model).lower())


def path() -> Path:
    return Path(os.environ.get('CADRE_MODEL_CATALOG') or (Path.home() / '.cache/opencode/models.json'))


def load(fresh: bool = False) -> dict[str, Any]:
    """Reads the catalogue and does not read it again until the file changes. If unreadable, an empty dict (the caller falls back to the old table)."""
    p = path()
    try:
        stamp = p.stat().st_mtime_ns
    except OSError:
        _cache.clear()
        return {}
    if fresh or _cache.get('stamp') != stamp or _cache.get('path') != str(p):
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            data = {}
        _cache.update({'stamp': stamp, 'path': str(p), 'data': data if isinstance(data, dict) else {}})
    data = dict(_cache.get('data', {}))
    try:
        or_live = json.loads(live_path('openrouter').read_text())
        if isinstance(or_live.get('records'), dict):
            previous = data.get('openrouter') or {}
            models = dict(previous.get('models') or {})
            for entry_name, record in or_live['records'].items():
                old = models.get(entry_name) if isinstance(models.get(entry_name), dict) else {}
                if isinstance(record, dict) and isinstance(old.get('limit'), dict) and isinstance(record.get('limit'), dict):
                    record = {**record, 'limit': {**old['limit'], **record['limit']}}
                models[entry_name] = record
            data['openrouter'] = {**previous, 'models': models}
    except (OSError, ValueError, AttributeError):
        pass
    return data


def model(provider: str, entry_name: str) -> dict[str, Any] | None:
    """Catalogue record. `entry_name` may come with a provider prefix like 'groq/openai/gpt-oss-120b'; the first part is dropped."""
    data = load()
    id = ID.get(provider, provider)
    models = (data.get(id) or {}).get('models') or {}
    if entry_name in models:
        return models[entry_name]
    # 'provider/model' or 'provider/sub/model' — catalogues keep the sub-path (example: openai/gpt-oss-120b)
    if '/' in entry_name:
        remaining = entry_name.split('/', 1)[1]
        if remaining in models:
            return models[remaining]
        if entry_name.split('/', 1)[0] in models:
            return models[entry_name.split('/', 1)[0]]
    return None


def context(provider: str, entry_name: str) -> int | None:
    m = model(provider, entry_name) or {}
    value = (m.get('limit') or {}).get('context')
    return int(value) if isinstance(value, (int, float)) and value > 0 else None


def price(provider: str, entry_name: str) -> dict[str, float] | None:
    """USD / 1M tokens. None if not in the catalogue (unknown) — must not be confused with 'free'."""
    m = model(provider, entry_name)
    if m is None:
        return None
    c = m.get('cost') or {}
    return {k: float(v) for k, v in c.items() if isinstance(v, (int, float))}


def is_free(provider: str, entry_name: str) -> bool | None:
    """True/False, None if not in the catalogue. UNKNOWN does not mean free; the caller must assume paid."""
    f = price(provider, entry_name)
    if f is None or 'input' not in f or 'output' not in f:   # B297: a missing price is unknown (same as the plugin's zeroCost)
        return None
    return f['input'] == 0 and f['output'] == 0


# --- Models the account can REALLY use ------------------------------------------------------
# The catalogue is a SUPERSET (zen: 103 listed, the account can use 71). If a live list exists the router intersects
# with it; otherwise no filtering is done.

def _qe() -> Path:
    return Path(os.environ.get('CADRE_HOME') or (Path.home() / '.local/share/cadre-engineering'))


def live_path(provider: str) -> Path:
    return _qe() / 'chain' / f'{provider}-live.json'


def live_list(provider: str) -> set[str] | None:
    """Model ids the account can use; None if the file is missing/broken (= no filtering)."""
    try:
        data = json.loads(live_path(provider).read_text(encoding='utf-8'))
        models = data.get('models')
        return set(models) if isinstance(models, list) and models else None
    except (OSError, ValueError, AttributeError):
        return None


def available(provider: str, entry_name: str) -> bool:
    """False if the live list is known and the model is not in it. True if unknown (we do not block)."""
    list = live_list(provider)
    if list is None:
        return True
    return entry_name in list or (entry_name.split('/', 1)[1] in list if '/' in entry_name else False)


def zen_reload(timeout: float = 30.0) -> int:
    """Refreshes the list the account can use from zen's /models endpoint. Returns: model count.
    Note: the endpoint is behind Cloudflare; without a User-Agent it returns 403 (code 1010)."""
    import urllib.request
    request = urllib.request.Request('https://opencode.ai/zen/v1/models', headers={'User-Agent': 'opencode/1.0'})
    with urllib.request.urlopen(request, timeout=timeout) as r:
        data = json.load(r)
    models = sorted(m['id'] for m in (data.get('data') or data))
    path = live_path('opencode')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'ts': __import__('time').time(), 'models': models}, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)
    return len(models)


def or_reload(timeout: float = 30.0) -> int:
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
    atomic_json(live_path('openrouter'), {'ts': time.time(), 'models': sorted(records), 'records': records})
    return len(records)


PRICE_WATCH = 'chain/model-price-watch.json'


def followed_models() -> list[tuple[str, str]]:
    """Models that appear in chains: the price watch follows only the models WE USE."""
    try:
        import model_router
        candidates = {m for list in model_router.ROLES.values() for m in list}
    except Exception:
        return []
    output = []
    for candidate in candidates:
        candidate = candidate.split('|')[0].split('#')[0]
        if '/' not in candidate:
            continue
        provider_key, entry_name = candidate.split('/', 1)
        output.append((provider_key, entry_name))
    return sorted(set(output))


def price_change(home: Path | None = None, write: bool = True) -> list[dict[str, Any]]:
    """B88: the catalogue does not say 'will become paid'; a free -> paid switch (or the reverse) is seen and reported immediately (morning summary).
    The bucket decision is already read from the catalogue at call time; this function keeps the change from going unnoticed."""
    home_dir = Path(home) if home is not None else _qe()
    watch = home_dir / PRICE_WATCH
    try:
        previous = json.loads(watch.read_text(encoding='utf-8'))
        previous = previous if isinstance(previous, dict) else {}
    except (OSError, ValueError):
        previous = {}
    current: dict[str, Any] = {}
    change: list[dict[str, Any]] = []
    for provider_key, entry_name in followed_models():
        f = price(provider_key, entry_name)
        if f is None:
            continue
        key = f'{provider_key}/{entry_name}'
        free = (f.get('input') == 0 and f.get('output') == 0)
        current[key] = {'input': f.get('input'), 'output': f.get('output'), 'free': free}
        old = previous.get(key)
        if isinstance(old, dict) and old.get('free') is not None and old['free'] != free:
            change.append({'model': key, 'old_free': bool(old['free']), 'fresh_free': free,
                            'old': {'input': old.get('input'), 'output': old.get('output')},
                            'fresh': {'input': f.get('input'), 'output': f.get('output')}})
    if write and current:
        try:
            watch.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(watch, current)
        except OSError:
            pass
    return change


def summary() -> dict[str, int]:
    data = load()
    return {k: len((v or {}).get('models') or {}) for k, v in data.items() if isinstance(v, dict)}


if __name__ == '__main__':
    import sys
    if len(sys.argv) == 2 and sys.argv[1] == '--zen-reload':
        print(f'Zen live model list refreshed: {zen_reload()} model -> {live_path("opencode")}')
    elif len(sys.argv) >= 3:
        s, a = sys.argv[1], sys.argv[2]
        print(json.dumps({'provider': s, 'model': a, 'context': context(s, a),
                          'price': price(s, a), 'free': is_free(s, a)}, ensure_ascii=False))
    else:
        o = summary()
        print(f'catalog: {path()} ({sum(o.values())} model, {len(o)} provider)')
        for k in ('opencode', 'opencode-go', 'openrouter'):
            if k in o:
                print(f'  {k:<12} {o[k]}')
