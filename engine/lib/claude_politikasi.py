"""User's weekly reserve and Claude difficulty policy; no provider I/O."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import math
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from kosu import RunStore

ZONE = ZoneInfo('Europe/Istanbul')
# Item 4 — ONE FORMULA: cap = min(92.5, 100 - rezerv_gunu * 7.5). The per-day table and the fixed margin were removed;
# the rationale and measurements are in the development history.
REMAINING_NEED = {0:7, 1:0, 2:42, 3:29, 4:15, 5:15, 6:15}   # historical record; no longer used in the cap calculation
MARGIN = 5                                                   # historical; the 92.5 cap already includes the safety margin
TAVAN_UST = 92.5
GUN_BASI_REZERV = 7.5
# The observation (~/.claude/claude_quota.json) only refreshes while the user is in Claude Code. A stale observation =
# the user is not active; the automation's own spending is in the ledger. After one reset period (7 days) the observation is invalid.
STALE_FRESH_MINUTES = 30
STALE_PENALTY = 5
STALE_LIMIT_MINUTES = 7 * 24 * 60


def local(now: datetime) -> datetime:
    return now.replace(tzinfo=ZONE) if now.tzinfo is None else now.astimezone(ZONE)


def sonraki_reset(now: datetime) -> datetime:
    """The next weekly reset, Tuesday 19:00 (if now is exactly the reset moment, the following week)."""
    now = local(now)
    reset = (now + timedelta(days=(1 - now.weekday()) % 7)).replace(hour=19, minute=0, second=0, microsecond=0)
    if reset <= now:
        reset += timedelta(days=7)
    return reset


def kalan_rezerv_gunu(now: datetime) -> int:
    """How many reserve days there are from tomorrow up to and including the reset Tuesday."""
    now = local(now)
    son = sonraki_reset(now).date()
    gun = now.date() + timedelta(days=1)
    sayi = 0
    while gun <= son:
        if gun.weekday() < 5:
            sayi += 1
        gun += timedelta(days=1)
    return sayi


def tavan(now: datetime) -> float:
    return min(TAVAN_UST, 100 - kalan_rezerv_gunu(now) * GUN_BASI_REZERV)


def pay(used7: float, used5: float, now: datetime, sessions: int = 1, stale_minutes: float = 0.0) -> float:
    """Automation headroom in percentage points. A stale but recent observation costs one extra margin step;
    an observation older than one reset period is unknown and yields nothing."""
    if not all(math.isfinite(x) and 0 <= x <= 100 for x in (used7, used5)):
        return 0
    if not math.isfinite(stale_minutes) or stale_minutes < 0 or stale_minutes > STALE_LIMIT_MINUTES:
        return 0
    if used5 >= 80 or sessions >= 5 or sessions < 1:
        return 0
    penalty = 0 if stale_minutes <= STALE_FRESH_MINUTES else STALE_PENALTY
    return max(0.0, min(90.0, tavan(now)) - penalty - used7) / sessions


def last_reset(now: datetime) -> datetime:
    now = local(now)
    reset = (now - timedelta(days=(now.weekday()-1) % 7)).replace(hour=19, minute=0, second=0, microsecond=0)
    if now < reset:
        reset -= timedelta(days=7)
    return reset


def period(now: datetime) -> str:
    now = local(now)
    return now.date().isoformat() + '@' + last_reset(now).isoformat()


def oturum_say(comm_lines) -> int:
    """Claude Code process count from `ps -axo comm=` lines. `pgrep -x claude` returned 0 for a live session
    on this Mac (B3); comparing the executable basename is transport-independent. Never below 1."""
    count = 0
    for line in comm_lines:
        name = line.strip().rsplit('/', 1)[-1]
        if name == 'claude':
            count += 1
    return max(1, count)


def difficulty(home: Path, role: str, explicit: int | None = None) -> int:
    run = os.environ.get('CADRE_RUN_ID')
    selection = None
    selected_level = 3
    if run:
        from model_secimi import kosu_secimi
        selection = kosu_secimi(home, run)
        if selection is not None:
            selected_level = {'easy': 2, 'medium': 3, 'hard': 4, 'very_hard': 5}[selection['difficulty_effective']]
            if selection['preference'] == 'strong':
                selected_level = max(4, selected_level)
    if explicit is not None:
        if type(explicit) is not int or not 1 <= explicit <= 5:
            raise ValueError('invalid task difficulty')
        return max(explicit, selected_level) if selection is not None else explicit
    if run:
        plan = RunStore(home).path(run)/'plan.json'
        if plan.exists():
            tasks = json.loads(plan.read_text())['gorevler']
            task_id = os.environ.get('CADRE_GOREV_ID') or 'main'
            levels = [t.get('zorluk', 3) for t in tasks if role == 'kalite' or t['id'] == task_id]
            if levels:
                return difficulty(home, role, max(selected_level, *levels))
    return selected_level


def fable_slots(home: Path, budget: dict) -> int:
    """Attempts, including unknown exits, consume the two daily launch slots."""
    row = budget.get('claude', {})
    observed = row.get('fable_bugun', 0)
    receipt_ids = row.get('fable_attempts', [])
    if type(observed) is not int or observed < 0 or not isinstance(receipt_ids, list) or any(not isinstance(i, str) for i in receipt_ids):
        return 0
    ids = set(receipt_ids)
    unknown_legacy = max(0, observed - len(ids))
    today = datetime.now(ZONE).date()
    for path in (home/'runs').glob('*/tasks/*/attempts/*/attempt.json'):
        attempt = json.loads(path.read_text())
        if ('claude/fable' in attempt.get('model', '') and attempt.get('outcome') != 'not_started'
                and datetime.fromtimestamp(attempt['created'], ZONE).date() == today):
            ids.add(attempt['attempt_id'])
    return max(0, 2 - len(ids) - unknown_legacy)


def model(requested: str, budget: dict, level: int, slots: int) -> str:
    name = requested.split('/', 1)[1].split('#', 1)[0]
    family = name.split('-')[0]
    if family not in ('sonnet', 'opus', 'fable'):
        raise ValueError('unknown Claude model family')
    headroom = float(budget.get('claude', {}).get('pay', 0))
    if not math.isfinite(headroom) or headroom <= 0:
        raise ValueError('no Claude weekly headroom')
    if level <= 3:   # smoke: the requested opus made the medium plan ($0.43); easy/medium work is always sonnet#high
        return "claude/sonnet#high"
    if family == 'fable' and (level < 5 or slots <= 0 or headroom < 10):
        name, family = 'opus', 'opus'
    if family == 'opus' and headroom < 4:
        name, family = 'sonnet', 'sonnet'
    effort = 'high' if family == 'sonnet' else ('max' if level == 5 else 'xhigh')
    return f'claude/{name}#{effort}'
