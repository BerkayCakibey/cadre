#!/usr/bin/env python3
"""Reality checks: the system's assumptions about the outside world, measured by a weekly canary (4c-19). A deviation becomes a PROBLEM line and
`chain/reality.json`. Go only (cash $0); OR only when there is a balance and only with free listing data."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any

import go_window
from run_store import atomic_json
from limits import OUTPUT

QE = Path(os.environ.get('CADRE_HOME') or (Path.home() / '.local/share/cadre-engineering'))
CUTTING = 1.0   # the Go API truncates the percentage down (real >= API); tolerance ±1 point + truncation margin


def go_windows(home: Path, db: Path, month_start: float, tolerance: float = 1.0) -> list[str]:
    """Go percentage ↔ Σ formula (with window coefficients), separately in three windows."""
    api = go_window._cache(home)
    if not api.get('ts'):
        return ['go-window: API observation missing']
    own = go_window.own_percentages(db, float(api['ts']), api.get('resets'), month_start)
    if own is None:
        return ['go-window: opencode DB unreadable']
    return [f"go-window {w}: Σ %{own[w]:.1f} ≠ API %{float(api.get(w) or 0):.0f}" for w in go_window.WINDOWS
            if not -tolerance <= own[w] - float(api.get(w) or 0) <= tolerance + CUTTING]


def endpoint_limits(price: dict[str, Any], catalog_data: dict[str, Any], chains: dict[str, list[str]]) -> list[str]:
    """Every model in the chain has an output limit that fits its role's cap; on OR at least one allowed endpoint carries the cap and the token parameter."""
    problem = []
    for role, chain in chains.items():
        cap = OUTPUT.get(role, OUTPUT['lane'])
        for route in chain:
            for candidate in route.split('|'):
                provider_key, _, mid = candidate.partition('/')
                mid = mid.split('#')[0]
                if provider_key == 'claude':
                    continue
                if provider_key == 'openrouter':
                    m = (price.get('models') or {}).get(mid)
                    endpoints = (m or {}).get('tiers') or {}
                    if not m:
                        problem.append(f'endpoint {role}: {candidate} absent from the price file')
                    elif not any((u.get('max_output') or cap) >= cap and u.get('params') != [] for u in endpoints.values()):   # None = unknown
                        problem.append(f'endpoint {role}: {candidate} no allowed endpoint supports {cap} tokens')
                    continue
                record = ((catalog_data.get(provider_key) or {}).get('models') or {}).get(mid)
                if record is None:
                    problem.append(f'endpoint {role}: {candidate} absent from the catalog')
                elif int((record.get('limit') or {}).get('output') or 0) < cap:
                    problem.append(f"endpoint {role}: {candidate} output {record['limit'].get('output')} < {cap}")
    return sorted(set(problem))


def sol_tiers(price: dict[str, Any], policy: dict[str, Any]) -> list[str]:
    """Sol tiers come with the tags in the policy, are below the maximum price, and every tier's token parameter is known."""
    problem = []
    for entry_name, rule in policy.items():
        if not isinstance(rule, dict) or 'tiers' not in rule or 'sol' not in entry_name:
            continue
        m = (price.get('models') or {}).get(entry_name)
        if not m:
            problem.append(f'sol: {entry_name} absent from the price file')
            continue
        missing = [k for k in rule['tiers'] if k not in (m.get('tiers') or {})]
        if missing:
            problem.append(f"sol: {entry_name} tier missing: {', '.join(missing)}")
        cap = rule.get('max_price') or {}
        for label, endpoint in (m.get('tiers') or {}).items():
            if not endpoint.get('params'):
                problem.append(f'sol: {entry_name} {label} token parameter unknown')
            if cap and (endpoint['input'] > float(cap['prompt']) * 1.000001 or endpoint['output'] > float(cap['completion']) * 1.000001):
                problem.append(f'sol: {entry_name} {label} exceeds the maximum price')
    return problem


def prices(home: Path, catalog_data: dict[str, Any], chains: dict[str, list[str]]) -> tuple[list[str], dict[str, Any]]:
    """Writes it if the catalogue price of models in the chain changed since the previous canary (a change affects the bucket/reservation decision)."""
    now_ts, problem = {}, []
    for chain in chains.values():
        for route in chain:
            for candidate in route.split('|'):
                provider_key, _, mid = candidate.partition('/')
                cost = (((catalog_data.get(provider_key) or {}).get('models') or {}).get(mid.split('#')[0]) or {}).get('cost') or {}
                if provider_key != 'claude' and {'input', 'output'} <= set(cost):
                    now_ts[candidate] = {'input': cost['input'], 'output': cost['output']}
    try:
        previous = json.loads((home / 'chain/reality.json').read_text()).get('prices') or {}
    except (OSError, ValueError, AttributeError):
        previous = {}
    for candidate, f in sorted(now_ts.items()):
        if candidate in previous and previous[candidate] != f:
            problem.append(f"price: {candidate} {previous[candidate]['input']}/{previous[candidate]['output']} -> {f['input']}/{f['output']}")
    return problem, now_ts


def thinking_text(model: str, opencode: str | None = None, db: Path | None = None, timeout: float = 180) -> list[str]:
    """A small request to a thinking Go model; thinking TEXT must appear in the opencode DB (usage alone is not proof).
    On a very easy question the model may skip thinking (07.10 canary); the question needs a little thought, and if there is no text it is tried once more."""
    import shutil
    import sqlite3
    from contextlib import closing
    opencode = opencode or os.environ.get('CADRE_REAL_OPENCODE') or shutil.which('opencode') or '/opt/homebrew/bin/opencode'
    db = db or Path(os.path.expanduser(os.environ.get('OPENCODE_DB', '~/.local/share/opencode/opencode.db')))
    # in a cron environment the plugin's gate cannot be installed (no CADRE_PYTHON -> denial); the gate is installed, observation mode and the run id are not carried.
    # Without a role, whether to think is left to the model: plan role = high.
    environment = {k: v for k, v in os.environ.items() if k not in ('CADRE_RUN_ID', 'CADRE_TASK_ID', 'CADRE_BUDGET_OBSERVATION')}
    environment.update(CADRE_HOME=str(QE), CADRE_ENGINE_ROOT=str(Path(__file__).resolve().parents[1]), CADRE_PYTHON=sys.executable,
                 OPENCODE_EXPERIMENTAL_NATIVE_LLM='false', CADRE_ROLE='plan')
    prompt = 'Sequence: 3, 7, 15, 31, ... What is the rule and the next number? Write only the number.'
    problem = []
    for _attempt in range(2):
        with tempfile.TemporaryDirectory(prefix='cadre-reality-') as directory:
            try:
                r = subprocess.run([opencode, 'run', '--dir', directory, '-m', model, '--format', 'json', prompt],
                                   stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, cwd=directory, env=environment)
            except (OSError, subprocess.TimeoutExpired) as exc:
                return [f'thinking: {model} could not be called ({type(exc).__name__})']
        session = next((json.loads(s).get('sessionID') for s in r.stdout.splitlines()
                       if s.startswith('{') and json.loads(s).get('sessionID')), None)
        if r.returncode != 0 or not session:
            return [f'thinking: {model} gave no answer (rc={r.returncode})']
        try:
            with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as con:
                length = con.execute("select coalesce(sum(length(json_extract(p.data,'$.text'))),0) from part p join message m "
                                      "on m.id=p.message_id where m.session_id=? and json_extract(p.data,'$.type')='reasoning'",
                                      (session,)).fetchone()[0]
        except sqlite3.Error:
            return [f'thinking: {model} opencode DB unreadable']
        if length:
            return []
        problem = [f'thinking: {model} produced no thinking text (2 attempts)']
    return problem


def execute(home: Path = QE, live: bool = True) -> dict[str, Any]:
    import provider_budget
    import model_catalog
    import model_router
    catalog_data = model_catalog.load(fresh=True)
    try:
        price = json.loads((home / 'chain/openrouter-price.json').read_text())
    except (OSError, ValueError):
        price = {}
    policy = json.loads((Path(__file__).with_name('openrouter-policy.json')).read_text())
    chains = {r: z for r, z in model_router.ROLES.items() if z}
    price_problems, price_now_ts = prices(home, catalog_data, chains)
    outcome = {'go-window': go_windows(home, provider_budget.DB, provider_budget.go_period()[0]),
             'endpoint-limits': endpoint_limits(price, catalog_data, chains),
             'sol-tiers': sol_tiers(price, policy),
             'prices': price_problems}
    if live and os.environ.get('CADRE_TEST_MODE') != '1':
        outcome['thinking'] = thinking_text(os.environ.get('CADRE_REALITY_MODEL', 'opencode-go/deepseek-v4-flash'))
    atomic_json(home / 'chain/reality.json', {'ts': time.time(), 'outcome': outcome, 'prices': price_now_ts})
    return outcome


if __name__ == '__main__':
    for entry_name, problems in execute().items():
        print(f'reality {entry_name}: ' + ('SOUND' if not problems else 'PROBLEM -> ' + ' | '.join(problems)[:300]))
