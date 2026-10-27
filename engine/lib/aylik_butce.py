#!/usr/bin/env python3
"""Shared monthly cap for cash providers (Zen, OpenRouter); the per-account provider ledger is authoritative.
Go is a subscription pool and is not counted here (4c-13). Amounts are micro-dollars; a pending request holds the worst case until its receipt."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Iterator
import uuid

PROVIDERS = frozenset({'opencode', 'openrouter'})
DEFAULT_CAP_USD = Decimal('65')


class MonthlyBudgetDenied(RuntimeError):
    pass


def _number(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise MonthlyBudgetDenied('invalid monthly budget number')
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise MonthlyBudgetDenied('invalid monthly budget number') from exc
    if not number.is_finite() or number < 0:
        raise MonthlyBudgetDenied('invalid monthly budget number')
    return number


def usd_micro(raw: Any, *, zero: bool = False) -> int:
    value = _number(raw)
    if value == 0 and not zero:
        raise MonthlyBudgetDenied('monthly budget cap must be positive')
    if value >= Decimal(2**53) / 1_000_000:
        raise MonthlyBudgetDenied('monthly budget cap out of range')
    result = int((value * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
    if not 0 <= result < 2**53:
        raise MonthlyBudgetDenied('monthly budget cap out of range')
    return result


def ayar_dosyasi(home: Path, path: Path) -> str | None:
    if path.is_symlink() or not path.resolve().is_relative_to(home.resolve()):
        raise ValueError('unsafe budget setting path')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, encoding='utf-8') as stream:
        raw = stream.read(257)
    if len(raw) > 256:
        raise ValueError('budget setting too long')
    return raw


def cap_micro(home: Path | None = None) -> int:
    home = home or Path(os.environ.get('CADRE_HOME', str(Path.home() / '.local/share/cadre-engineering')))
    raw = os.environ.get('CADRE_AYLIK_TAVAN_USD') or None
    if raw is None:
        try:
            raw = ayar_dosyasi(home, home / 'zincir/aylik-tavan-usd')
        except (ValueError, OSError) as exc:
            raise MonthlyBudgetDenied('monthly budget setting unreadable') from exc
    return usd_micro(str(DEFAULT_CAP_USD) if raw is None else raw)


def period(now: float | None = None) -> str:
    stamp = time.time() if now is None else now
    return datetime.fromtimestamp(stamp, timezone.utc).strftime('%Y-%m')


class MonthlyLedger:
    def __init__(self, path: Path | str, cap: int | None = None):
        self.path = Path(path)
        self.cap = cap_micro() if cap is None else cap
        if type(self.cap) is not int or not 0 < self.cap < 2**53:
            raise MonthlyBudgetDenied('invalid monthly budget cap')

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=15)
        try:
            con.execute('PRAGMA synchronous=FULL')
            con.execute('BEGIN IMMEDIATE')
            con.execute('''create table if not exists reservation(
                id text primary key, provider text not null, period text not null,
                created real not null, reserved integer not null, actual integer,
                receipt text unique, metadata text not null)''')
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def usage(con: sqlite3.Connection, current: str) -> tuple[int, int]:
        marks = ','.join('?' * len(PROVIDERS))
        args = (current, *sorted(PROVIDERS))
        settled = con.execute(
            f'select coalesce(sum(actual),0) from reservation where period=? and provider in ({marks}) and actual is not null',
            args).fetchone()[0]
        held = con.execute(
            f'select coalesce(sum(reserved),0) from reservation where provider in ({marks}) and actual is null',
            tuple(sorted(PROVIDERS))).fetchone()[0]
        return int(settled), int(held)

    def reserve(self, provider: str, maximum: int, metadata: dict[str, Any] | None = None,
                *, current: str | None = None, observe: bool = False) -> str:
        if provider not in PROVIDERS:
            raise MonthlyBudgetDenied('provider not in monthly paid pool')
        if type(maximum) is not int or not 0 < maximum < 2**53:
            raise MonthlyBudgetDenied('invalid monthly reservation')
        current = current or period()
        ident = uuid.uuid4().hex
        with self.transaction() as con:
            settled, held = self.usage(con, current)
            if settled + held + maximum > self.cap and not observe:
                raise MonthlyBudgetDenied(
                    f'monthly paid budget exhausted: reservation ${maximum / 1e6:.2f} > remaining '
                    f'${max(0, self.cap - settled - held) / 1e6:.2f} (cap ${self.cap / 1e6:.2f})')
            con.execute('insert into reservation values(?,?,?,?,?,null,null,?)',
                        (ident, provider, current, time.time(), maximum,
                         json.dumps(metadata or {}, allow_nan=False, ensure_ascii=False)))
        return ident

    def settle(self, ident: str, actual: int, receipt: str) -> None:
        if type(actual) is not int or not 0 <= actual < 2**53 or not receipt:
            raise MonthlyBudgetDenied('invalid monthly receipt')
        with self.transaction() as con:
            row = con.execute('select reserved,actual,receipt from reservation where id=?', (ident,)).fetchone()
            if row is None or (row[1] is not None and (row[1], row[2]) != (actual, receipt)):
                raise MonthlyBudgetDenied('conflicting monthly receipt')
            if actual > row[0]:
                con.execute('update reservation set reserved=? where id=?', (actual, ident))
            con.execute('update reservation set actual=?,receipt=? where id=?', (actual, receipt, ident))

    def status(self, *, current: str | None = None) -> dict[str, Any]:
        current = current or period()
        if not self.path.exists():
            settled = held = 0
        else:
            with self.transaction() as con:
                settled, held = self.usage(con, current)
        return {
            'period': current,
            'cap_micro': self.cap,
            'settled_micro': settled,
            'held_micro': held,
            'remaining_micro': max(0, self.cap - settled - held),
        }


def main() -> int:
    request: dict[str, Any] = {}
    try:
        home = Path(os.environ['CADRE_HOME'])
        request = json.load(sys.stdin)
        action = request['action']
        ledger = MonthlyLedger(home / 'zincir/aylik-butce.db')
        if action == 'reserve':
            provider = str(request['provider'])
            maximum = int(_number(request['maximum']).to_integral_value(rounding=ROUND_CEILING))
            observe = os.environ.get('CADRE_BUTCE_GOZLEM') == '1' and not os.environ.get('CADRE_RUN_ID')
            ident = ledger.reserve(provider, maximum, request.get('metadata') or {}, observe=observe)
            print(json.dumps({'id': ident}))
        elif action == 'settle':
            ledger.settle(str(request['id']), int(request['actual']), str(request['receipt']))
            print('{}')
        elif action == 'status':
            print(json.dumps(ledger.status()))
        else:
            raise MonthlyBudgetDenied('unknown monthly budget operation')
        return 0
    except (MonthlyBudgetDenied, OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        print(f'monthly budget denied: {exc}', file=sys.stderr)
        return 75


if __name__ == '__main__':
    raise SystemExit(main())
