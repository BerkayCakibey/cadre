#!/usr/bin/env python3
"""Morning summary: what happened overnight, what was spent, where it got stuck — one page (`reports/SUMMARY-<date>.md`).
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

from run_store import RunStore
from screen_render import symbol, run_summary, budget_summary, money, clean


def home_dir() -> Path:
    return Path(os.environ.get('CADRE_HOME', Path.home() / '.local/share/cadre-engineering'))


def _day_start(now_ts: float) -> float:
    t = time.localtime(now_ts)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def runs(home: Path, since: float) -> list[dict[str, Any]]:
    store = RunStore(home)
    output = []
    root = home / 'runs'
    if not root.is_dir():
        return output
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        try:
            record = store.read(d.name)
        except Exception:
            continue
        if float(record.get('updated') or record.get('created') or 0) < since:
            continue
        output.append(record)
    return output


def budget(home: Path) -> dict[str, Any]:
    try:
        saved = json.loads((home / 'chain/request-budget.json').read_text())
        if 0 <= time.time() - float(saved['observed']) < 60 and isinstance(saved['budget'], dict):
            return saved['budget']
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    return {}


def stale_reservation(home: Path) -> tuple[int, float]:
    db = home / 'chain/request-budget.db'
    try:
        with closing(OC.connect(db)) as con:   # B142b: the connection must not stay open
            n, amount = con.execute('select count(*), coalesce(sum(reserved),0) from reservation '
                                   'where actual is null and created < ?', (time.time() - 6 * 3600,)).fetchone()
        return int(n), float(amount) / 1e6
    except sqlite3.Error:
        return 0, 0.0


def produce(home: Path | None = None, now_ts: float | None = None, detail: bool = False) -> str:
    home = home or home_dir()
    now_ts = now_ts if now_ts is not None else time.time()
    since = _day_start(now_ts) - 12 * 3600          # night jobs spill over from the previous day
    b = budget(home)
    lines = [f"# Night summary — {time.strftime('%Y-%m-%d %H:%M', time.localtime(now_ts))}", '']

    records = runs(home, since)
    if not detail:
        completed = sum(k.get('state') == 'completed' for k in records)
        active = sum(k.get('state') in ('running', 'queued', 'accepted') for k in records)
        stopped = sum(k.get('state') in ('failed', 'retryable', 'cancelled') for k in records)
        spent = sum(float(k.get('budget_spent_usd') or 0) for k in records)
        status = f'jobs stopped: {stopped}' if stopped else 'no problems' if records else 'no jobs yet'
        lines.append(f'{completed} run(s) finished · {active} in progress · {money(spent)} spent · {status}')
        lines += [f'{clean(k.get("project", "?"))} · {run_summary(k)}' for k in records]
        lines += ['', f'{symbol("balance")} Available: {budget_summary(b)}']
        n, amount = stale_reservation(home)
        if n:
            lines.append(f'{symbol("warning")} {n} stale reservation(s) ({money(amount)}) · cadre recover')
        if (b.get('go') or {}).get('window_open') is False:
            lines.append(f'{symbol("warning")} Go window closed · details: cadre budget')
        lines += ['', 'Details: cadre summary --detail']
        return '\n'.join(lines) + '\n'
    finished = [k for k in records if k.get('state') == 'completed']
    remaining = [k for k in records if k.get('state') not in ('completed', 'cancelled')]
    lines += ['## Runs', '']
    if not records:
        lines.append('No runs in this window.')
    # Noise control: runs that DID work or are still standing are written line by line; those cancelled
    # without spending anything are collected on one line.
    silent = 0
    for k in records:
        spend = float(k.get('budget_spent_usd') or 0)
        pending = float(k.get('budget_held_usd') or 0)
        important = spend > 0 or pending > 0 or k.get('state') in ('completed', 'failed') or k in remaining
        if not important:
            silent += 1
            continue
        lines.append(f"- `{k.get('run_id', '?')[:19]}` **{k.get('state', '?')}** · {k.get('project', '?')}"
                        f" · spent ${spend:.3f}"
                        + (f" · held ${pending:.3f}" if pending else '')
                        + (f" · {k.get('phase')}" if k.get('phase') else ''))
        for denial in (k.get('denials') or [])[-3:]:
            lines.append(f"    - denied: {denial.get('provider')} — {str(denial.get('reason'))[:80]}")
    if silent:
        lines.append(f"- ({silent} run(s) cancelled without spending anything)")
    total = sum(float(k.get('budget_spent_usd') or 0) for k in records)
    lines += ['', f"Finished {len(finished)} · in progress/pending {len(remaining)} · total spent ${total:.3f}", '']

    lines += ['## Budget', '']
    go = b.get('go') or {}
    p = go.get('windows') or {}
    if p:
        lines.append(f"- Go windows: rolling %{float(p.get('rolling') or 0):.0f} · "
                        f"weekly %{float(p.get('weekly') or 0):.0f} · monthly %{float(p.get('monthly') or 0):.0f}"
                        f" · {'open' if go.get('window_open') else 'CLOSED: ' + str(go.get('window_reason'))}")
        for name, moment in (p.get('resets') or {}).items():
            if moment:
                lines.append(f"    - {name} reset: {moment}")
    lines.append(f"- Go: {float(go.get('available') or 0):.1f} points to the wall · this month %{float(go.get('month_points') or 0):.1f}"
                    if go.get('db') == 'ok' else '- Go: pool unknown (observation unreadable)')
    for name, key in (('OpenRouter', 'or'), ('Zen', 'zen')):
        v = b.get(key) or {}
        if v:
            lines.append(f"- {name}: available {money(v.get('available'))}"
                            + (f" · balance ${float(v.get('remaining') or 0):.2f}" if key == 'zen' else ''))
    cl = b.get('claude') or {}
    if cl:
        lines.append(f"- Claude: share %{cl.get('pay')} (7 days %{cl.get('used7')}) · cap %{cl.get('cap')}")
    lines.append('- The Go subscription renews in its own window; prepaid balances are not split across days.')

    # §40.3 real-world gate: estimate ↔ actual. The estimate is NOT INVENTED, it is measured from history; without enough samples
    # the line says so explicitly and measurement keeps accumulating.
    try:
        import estimate_vs_actual
        tg = estimate_vs_actual.summary(home)
    except Exception:
        tg = ''
    if tg:
        lines += ['', '## Estimate ↔ actual', '', tg]

    n, total_amount = stale_reservation(home)
    lines += ['', '## Attention', '']
    if n:
        lines.append(f"- {n} stale reservation(s) (${total_amount:.2f}) older than 6 hours → `cadre recover --stale-list`")
    # B88: if a free model starts charging, the bucket decision changes SILENTLY because it is read from the catalogue
    # at call time. The catalogue has no "when will it become paid" field (checked), so we cannot know
    # beforehand; the only thing we can do is write the change as soon as we see it.
    try:
        import model_catalog
        for d in model_catalog.price_change(home):
            change = 'BECAME PAID' if d['old_free'] else 'became free again'
            lines.append(f"- Model price changed: `{d['model']}` {change} "
                            f"(input {d['fresh']['input']}, output {d['fresh']['output']})")
    except Exception:
        pass
    if not go.get('window_open', True):
        lines.append(f"- Go window closed: {go.get('window_reason')}")
    if go.get('window_divergence'):
        lines.append(f"- {go['window_divergence']}")
    if len(lines) and lines[-1] == '':
        lines.append('No warnings.')
    return '\n'.join(lines) + '\n'


def write(home: Path | None = None, now_ts: float | None = None) -> Path:
    home = home or home_dir()
    now_ts = now_ts if now_ts is not None else time.time()
    target = home / 'reports' / f"SUMMARY-{time.strftime('%Y%m%d', time.localtime(now_ts))}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.tmp')
    temporary.write_text(produce(home, now_ts, detail=True), encoding='utf-8')
    temporary.replace(target)
    return target


if __name__ == '__main__':
    import sys
    if '--write' in sys.argv:
        print(write())
    else:
        print(produce(detail='--detail' in sys.argv), end='')
