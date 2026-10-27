#!/usr/bin/env python3
"""Small acceptance package driver (scenarios R01-R07): frozen fixture, typed request path, the real engine/gates.

Usage: CADRE_HOME=<isolated root> CADRE_ENGINE_ROOT=<source engine/> \\
          python3 engine/tests/acceptance_package.py R03 --fixture-dir <campaign>/fixtures [--route fake|go|or] [--minute 20] [--cap 0.15]
`--route fake` is a dry run: acceptance_fake.py writes a canned product per scenario, the real gates run with a fake budget/catalogue; it is not a live acceptance.
`--route go|or` uses the same single-provider wrapper as live_scenario.py and is subject to the existing strict permission rules (no permission is produced here).
Product acceptance commands run in the engine's evidence sandbox (CXR-01); the whole package's shared cap is protected by the `--package-cap` ledger (CXR-02);
live receipt gates separate readability/scope/close/two-ledger states (CXR-03); CADRE_CHAIN_<ROLE> is carried to the subprocess (CXR-04).
Each scenario: the normal path + the related rejection/limit checks (the driver tests locally or binds to an existing test family); the summary
`<output>/summary.json` and `<output>/../acceptance-package.jsonl`. The R id is not passed to live_scenario.py.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time

TESTS = Path(__file__).resolve().parent
ENGINE = TESTS.parent
sys.path.insert(0, str(ENGINE / 'lib'))
sys.path.insert(0, str(TESTS))
HOME = Path(os.environ['CADRE_HOME']).resolve() if os.environ.get('CADRE_HOME') else None
ROOT = Path(os.environ.get('CADRE_ENGINE_ROOT') or ENGINE).resolve()
PY = sys.executable
FALLBACK_HOME_DIR = (Path.home() / '.local/share/cadre-engineering').resolve()
IDS = ('R01', 'R02', 'R03', 'R04', 'R05', 'R06', 'R07')
GIT_ID = {'GIT_AUTHOR_NAME': 'cadre-acceptance', 'GIT_AUTHOR_EMAIL': 'acceptance@cadre.invalid',
              'GIT_COMMITTER_NAME': 'cadre-acceptance', 'GIT_COMMITTER_EMAIL': 'acceptance@cadre.invalid'}
R06_BACKLOG = ('# Ready backlog\n\n## [ ] H001 — spend_summary CLI: category,amount CSV -> category and grand totals (two decimal places, Decimal)\n'
               'Dependencies: absent\nAcceptance: make test; PYTHONPATH=src python3 -m spend_summary <CSV>\n')


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(repo: Path, *args: str) -> str:
    return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=cadre-acceptance', '-c', 'user.email=acceptance@cadre.invalid', *args],
                          capture_output=True, text=True, check=True, timeout=60).stdout.strip()


def tree_hashes(root: Path, excluded: tuple[str, ...] = ('.git', '__pycache__', '.venv')) -> dict[str, str]:
    outcome = {}
    for path in sorted(root.rglob('*')):
        if path.is_file() and not any(p in excluded for p in path.relative_to(root).parts):
            outcome[str(path.relative_to(root))] = sha(path)
    return outcome


def check(items_list: list[dict], id: str, round: str, expected: str, observed, passed: bool | None,
            method: str = 'driver', evidence: str = '') -> bool:
    items_list.append({'id': id, 'round': round, 'method': method, 'expected': expected, 'observed': observed,
                  'outcome': 'PASS' if passed else 'BLOCKED' if passed is None else 'FAIL', 'evidence': evidence})
    return bool(passed)


def bound(items_list: list[dict], id: str, round: str, expected: str, tests: list[str]) -> None:
    """Binds negative/limit behaviour to an existing free test family; the result is read from the shared local gate (no separate model run)."""
    items_list.append({'id': id, 'round': round, 'method': 'test', 'expected': expected, 'tests': tests,
                  'outcome': 'GATE', 'evidence': 'engine/tests/regression.sh archive'})


def safe_command(command: str, wt: Path, temporary: Path, duration: float = 300) -> tuple[int | None, str]:
    """CXR-01: every acceptance command that runs the model's product goes through the engine's evidence sandbox (report_mode.command_execute): no network, writes only to the
    worktree + a temporary directory, sensitive directories/files are not read, the environment is an allow-list (the parent's CADRE_*/secret variables are not visible), the process
    group is reaped. CXR2-01: writable areas go through the engine's FIX-25 preflight (`session.has_product_hardlink`) before the command;
    if a pre-existing external inode alias exists the command never runs. rc None = could not run/timeout/denied; the caller does not count it as success."""
    import session
    import report_mode
    temporary = Path(temporary)
    temporary.mkdir(parents=True, exist_ok=True)
    for field in (Path(wt), temporary):
        if session.has_product_hardlink(field):
            return None, f'hardlink-untrusted: hardlink/external inode alias in a writable area ({field}); command was not run (FIX-25 preflight)'
    return report_mode.command_execute(command, str(wt), str(temporary), duration)


def json_line(output: str) -> dict | None:
    """The last JSON object line in the combined stdout/stderr; None if none (partial/broken output does not count as JSON)."""
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith('{'):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
    return None


def select_chain_environment(env: dict[str, str]) -> dict[str, str]:
    """CXR-04: CADRE_CHAIN_<ROLE> chains given by the operator are carried to the subprocess as is (no other CADRE_* is carried)."""
    return {k: v for k, v in env.items() if k.startswith('CADRE_CHAIN_') and v}


def chain_verify(chain: dict[str, str], prefix: str) -> list[str]:
    """Lists models outside the allowed provider; an empty list = suitable. Does not silently enable another provider."""
    return sorted({m.strip() for v in chain.values() for m in v.split(',') if m.strip() and not m.strip().startswith(prefix)})


class PackageShare:
    """CXR-02: the shared cap of the whole small package lives in one ledger (acceptance-package-pay.json). Allocation BEFORE every request; a retry, a new process
    or a second variant does not reset the remaining share; an allocation exceeding the total cap is denied before the request is recorded.
    CXR2-02: every allocation/binding re-reads the ledger under the engine's file lock (run_store.FileLock), verifies the cap and the total,
    writes atomically; two overlapping objects/processes cannot allocate with an old memory snapshot, the first line is never lost."""

    def __init__(self, file: Path, cap_usd: float) -> None:
        if not math.isfinite(cap_usd) or cap_usd < 0:
            raise ValueError('package cap must be a finite nonnegative USD amount')
        self.file = Path(file)
        self.cap_usd = round(cap_usd, 6)
        with self.lock():
            self.data = self.load()
            if self.data is None:
                self.data = {'package_cap_usd': self.cap_usd, 'allocations': []}
                self.save()
            self.verify()

    def lock(self):
        from run_store import FileLock
        self.file.parent.mkdir(parents=True, exist_ok=True)
        return FileLock(self.file.with_suffix('.lock'))

    def verify(self) -> None:
        if abs(float(self.data['package_cap_usd']) - self.cap_usd) > 1e-9:
            raise ValueError(f"package cap does not match the ledger: ledger {self.data['package_cap_usd']}, supplied {self.cap_usd}")

    def load(self) -> dict | None:
        try:
            data = json.loads(self.file.read_text())
        except FileNotFoundError:
            return None
        if (not isinstance(data, dict) or not isinstance(data.get('allocations'), list) or not math.isfinite(float(data.get('package_cap_usd', 'nan')))
                or any(not isinstance(t, dict) or not math.isfinite(float(t.get('usd', 'nan'))) or float(t['usd']) <= 0 for t in data['allocations'])
                or any('closing' in t and not self.closing_valid(t['closing']) for t in data['allocations'])):
            raise ValueError('package allocation ledger is broken')
        return data

    @staticmethod
    def closing_valid(closing) -> bool:
        if not isinstance(closing, dict) or not isinstance(closing.get('runs'), list) or not all(isinstance(k, str) for k in closing['runs']):
            return False
        try:
            liability = float(closing['liability_usd'])
        except (KeyError, TypeError, ValueError):
            return False
        return math.isfinite(liability) and liability >= 0

    @staticmethod
    def enabled(t: dict) -> float:
        return float(t['closing']['liability_usd']) if 'closing' in t else float(t['usd'])

    def allocation_done(self) -> float:
        return round(sum(self.enabled(t) for t in self.data['allocations']), 6)

    def brut(self) -> float:
        return round(sum(float(t['usd']) for t in self.data['allocations']), 6)

    def refund_done(self) -> float:
        return round(sum(max(0.0, float(t['usd']) - self.enabled(t)) for t in self.data['allocations'] if 'closing' in t), 6)

    def remaining(self) -> float:
        return round(float(self.data['package_cap_usd']) - self.allocation_done(), 6)

    def allocation(self, scenario: str, label: str, requested_usd: float) -> float:
        if not math.isfinite(requested_usd) or requested_usd <= 0:
            raise ValueError('allocation must be a finite positive USD amount')
        requested_usd = round(requested_usd, 6)
        with self.lock():
            self.data = self.load() or {'package_cap_usd': self.cap_usd, 'allocations': []}   # fresh snapshot under the lock
            self.verify()
            if requested_usd > self.remaining() + 1e-9:
                raise RuntimeError(f'insufficient shared package allocation: requested {requested_usd:.6f}, remaining {self.remaining():.6f} '
                                   f"(cap {float(self.data['package_cap_usd']):.6f}, allocated {self.allocation_done():.6f}); request not recorded")
            self.data['allocations'].append({'scenario': scenario, 'label': label, 'usd': requested_usd, 'rid': None, 'ts': time.time()})
            self.save()
        return requested_usd

    def bind(self, label: str, rid: str) -> None:
        with self.lock():
            self.data = self.load() or {'package_cap_usd': self.cap_usd, 'allocations': []}
            self.verify()
            for t in self.data['allocations']:
                if t['label'] == label and t['rid'] is None:
                    t['rid'] = rid
            self.save()

    def refund_audit(self, home: Path, prefix: str | None) -> list[dict]:
        events = []
        with self.lock():
            self.data = self.load() or {'package_cap_usd': self.cap_usd, 'allocations': []}
            self.verify()
            for t in self.data['allocations']:
                event = {'label': t['label'], 'rid': t.get('rid'), 'usd': float(t['usd'])}
                if not t.get('rid'):
                    events.append({**event, 'outcome': 'waiting', 'reason': 'run id absent (request not bound)'})
                    continue
                evidence = closing_evidence(home, t['rid'], prefix)
                closing = t.get('closing')
                if closing is None:
                    if not evidence['may_close']:
                        events.append({**event, 'outcome': 'waiting', 'reason': evidence['reason']})
                        continue
                    liability = evidence['liability_usd']
                    overrun = liability > float(t['usd']) + 1e-9
                    t['closing'] = {'liability_usd': liability, 'refund_usd': 0.0 if overrun else round(float(t['usd']) - liability, 6),
                                    'runs': evidence['runs'], 'receipt': evidence['receipt'], 'ts': time.time(), **({'overrun': True} if overrun else {})}
                    events.append({**event, 'outcome': 'overrun' if overrun else 'chargeback', 'liability_usd': liability, 'reason': evidence['reason']})
                    continue
                previous = float(closing['liability_usd'])
                if not evidence['may_close'] or evidence['runs'] != closing['runs']:
                    target = max(previous, float(t['usd']))
                    if target > previous + 1e-9:
                        closing.update(liability_usd=target, refund_usd=0.0,
                                       go_back_taken={'reason': evidence['reason'] or 'run scope changed', 'ts': time.time()})
                        events.append({**event, 'outcome': 'go_back_taken', 'liability_usd': target, 'reason': evidence['reason'] or 'run scope changed'})
                    else:
                        events.append({**event, 'outcome': 'closed', 'liability_usd': previous, 'reason': evidence['reason']})
                    continue
                if evidence['liability_usd'] > previous + 1e-9:
                    fresh = evidence['liability_usd']
                    closing.update(liability_usd=fresh, refund_usd=max(0.0, round(float(t['usd']) - fresh, 6)), receipt=evidence['receipt'],
                                   late_receipt={'previous_usd': previous, 'ts': time.time()}, **({'overrun': True} if fresh > float(t['usd']) + 1e-9 else {}))
                    events.append({**event, 'outcome': 'late_receipt', 'liability_usd': fresh, 'reason': evidence['reason']})
                else:
                    events.append({**event, 'outcome': 'closed', 'liability_usd': previous, 'reason': evidence['reason']})
            self.save()
        return events

    def save(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.file.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2))
        temporary.replace(self.file)


def r06_shares(aile_cap: float, a_liability_usd: float | None) -> tuple[float, float]:
    """CXR-02: R06's two variants share one family share: A = share/2; B = min(share/2, share - A's settled+pending obligation).
    If A's obligation is unknown (no receipt) A's whole cap counts as spent (conservative)."""
    a = round(aile_cap / 2, 6)
    known = a if a_liability_usd is None else max(0.0, a_liability_usd)
    b = round(min(a, aile_cap - known), 6)
    return a, max(0.0, b)


def receipt_read(db: Path, ids: list[str]) -> dict:
    """Reads request-budget.db rows with the same classification as live_scenario; 'error' if unreadable, an empty set without rows."""
    from budget_gate import FREE, PAID, USD_PROVIDERS, base_provider
    outcome: dict = {'providers': {}, 'line': 0, 'error': None, 'monthly_ids': set(), 'sessions': set()}
    if not ids:
        return {**outcome, 'error': 'run id absent'}
    if not db.exists():
        return {**outcome, 'error': 'request-budget.db absent'}
    mark = ','.join('?' * len(ids))
    try:
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)) as con:
            lines = con.execute(f"select provider, reserved, actual, created, json_extract(metadata,'$.run_id'), receipt, metadata, id "
                                   f"from reservation where json_extract(metadata,'$.run_id') in ({mark})", ids).fetchall()
    except sqlite3.Error as exc:
        return {**outcome, 'error': f'ledger unreadable: {type(exc).__name__}: {exc}'}
    outcome['line'] = len(lines)
    for provider, reserved, real, start, run_id, receipt, raw, ident in lines:
        try:
            metadata = json.loads(raw) if raw else {}
        except ValueError:
            return {**outcome, 'error': f'broken receipt metadata: {ident}'}
        if not isinstance(metadata, dict) or type(reserved) is not int or reserved < 0 or (real is not None and (type(real) is not int or real < 0)):
            return {**outcome, 'error': f'uncertain receipt amount: {ident}'}
        for field, collection in (('monthly_id', 'monthly_ids'), ('session_id', 'sessions')):
            if isinstance(metadata.get(field), str) and metadata[field]:
                outcome[collection].add(metadata[field])
        base = base_provider(provider)
        free = (str(provider).split('#', 1)[0].endswith(':free') and base in {*PAID, *FREE}) or base in FREE
        unit = 'request' if free else 'cash_micro' if base in ('opencode', 'openrouter') else 'subscription_equivalent_micro' if base in ('opencode-go', 'claude') else 'unknown'
        s = outcome['providers'].setdefault(provider, {'unit': unit, 'request': 0, 'real_micro': 0, 'pending': 0, 'pending_micro': 0,
                                                            'assurance_open': 0, 'assurance_consumed': 0, 'unused': 0, 'uncertain': 0})
        if real == 0 and receipt == 'unused:' + ident:
            s['unused'] += 1
            continue
        if metadata.get('assurance') and real == 0 and receipt == 'claimed:' + ident:
            s['assurance_consumed'] += 1
            continue
        bound = max(reserved, real or 0, *[v for v in (metadata.get('worst'), metadata.get('verified_bound_micro')) if type(v) is int and v >= 0])
        if metadata.get('assurance') and real is None:
            s['assurance_open'] += 1
            s['pending_micro'] += bound
            continue
        known = (real is not None and isinstance(receipt, str) and receipt.startswith(ident + ':') and not metadata.get('assurance')
                   and not metadata.get('observation') and (free or base in USD_PROVIDERS))   # bucket name 'provider[#model|:free]'; the base decides
        if known:
            s['request'] += 1
            s['real_micro'] += real
        else:
            s['pending'] += 1
            s['uncertain'] += 1
            s['pending_micro'] += bound
    return outcome


def monthly_cash(db: Path, ids: list[str], monthly_ids: set[str] | None = None, sessions: set[str] | None = None) -> dict:
    """Second ledger (chain/monthly-budget.db): cash rows written for this run family; missing if the file does not exist."""
    if not ids:
        return {'status': 'absent', 'line': 0, 'real_micro': 0, 'open': 0}
    if not db.exists():
        return {'status': 'absent', 'line': 0, 'real_micro': 0, 'open': 0}
    conditions, values = [], []
    for expression, id_group in (("json_extract(metadata,'$.run_id')", list(ids)), ('id', sorted(monthly_ids or ())),
                        ("json_extract(metadata,'$.session_id')", sorted(sessions or ()))):
        if id_group:
            conditions.append(f"{expression} in ({','.join('?' * len(id_group))})")
            values.extend(id_group)
    try:
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)) as con:
            lines = con.execute(f"select provider, reserved, actual from reservation where {' or '.join(conditions)}", values).fetchall()
    except sqlite3.Error as exc:
        return {'status': f'unreadable: {type(exc).__name__}: {exc}', 'line': 0, 'real_micro': 0, 'open': 0}
    return {'status': 'ok', 'line': len(lines), 'real_micro': sum(int(a) for _, _, a in lines if a is not None),
            'open': sum(1 for _, _, a in lines if a is None)}


def receipt_gates(home: Path, ids: list[str], prefix: str | None) -> dict:
    """CXR-03: separate mandatory states for live gates. An unreadable/out-of-scope/empty ledger cannot produce PASS; without a matching record
    'zero receipts in a run that expected a real call' is BLOCKED; an open reserve/guarantee/uncertain amount is FAIL; a provider outside the allowed one is FAIL;
    cash rows must match in both ledgers (in a Go-only run the monthly cash ledger must have no rows for this family)."""
    from budget_gate import base_provider
    m = receipt_read(home / 'chain/request-budget.db', ids)
    outcome = {'readable': m['error'] is None, 'error': m['error'], 'line': m['line'], 'providers': m['providers'],
             'unpermitted': [], 'pending': 0, 'open_assurance': 0, 'request': 0, 'cash_micro': 0, 'equivalent_micro': 0, 'monthly': None, 'status': 'BLOCKED', 'reason': ''}
    if m['error'] is not None:
        outcome['reason'] = m['error']
        return outcome
    if m['line'] == 0:
        outcome['reason'] = 'no matching receipt: zero records cannot prove successful closure for a run expected to make a real call'
        return outcome
    for provider_key, v in m['providers'].items():
        base = base_provider(provider_key)
        if prefix and base != prefix.rstrip('/') and base != 'groq' and (v['request'] or v['pending'] or v['assurance_open']):
            outcome['unpermitted'].append(provider_key)
        outcome['pending'] += v['pending']
        outcome['open_assurance'] += v['assurance_open']
        outcome['request'] += v['request']
        if v['unit'] == 'cash_micro':
            outcome['cash_micro'] += v['real_micro'] + v['pending_micro']
        elif v['unit'] == 'subscription_equivalent_micro':
            outcome['equivalent_micro'] += v['real_micro'] + v['pending_micro']
    outcome['unpermitted'].sort()
    monthly = monthly_cash(home / 'chain/monthly-budget.db', ids, m['monthly_ids'], m['sessions'])
    outcome['monthly'] = monthly
    if outcome['unpermitted']:   # order: unallowed route > unclosed reserve > two ledgers > presence of a closed receipt
        outcome['status'] = 'FAIL'
        outcome['reason'] = 'receipt outside the allowed provider: ' + ', '.join(outcome['unpermitted'])
        return outcome
    if outcome['pending'] or outcome['open_assurance']:
        outcome['status'] = 'FAIL'
        outcome['reason'] = f"unclosed reserve/assurance: pending {outcome['pending']}, open assurance {outcome['open_assurance']}"
        return outcome
    cash_line = sum(1 for v in m['providers'].values() if v['unit'] == 'cash_micro' and (v['request'] or v['pending']))
    if monthly['status'].startswith('unreadable'):
        outcome['reason'] = 'monthly cash ledger ' + monthly['status']
        return outcome
    if cash_line and monthly['status'] == 'absent':
        outcome['reason'] = 'cash receipt exists but monthly ledger absent (the two ledgers cannot be matched)'
        return outcome
    if cash_line and (monthly['open'] or monthly['real_micro'] != sum(v['real_micro'] for v in m['providers'].values() if v['unit'] == 'cash_micro')):
        outcome['status'] = 'FAIL'
        outcome['reason'] = f"two ledgers mismatch: request ledger cash {outcome['cash_micro']} micro, monthly ledger {monthly['real_micro']} micro, open {monthly['open']}"
        return outcome
    if not cash_line and monthly['status'] == 'ok' and (monthly['line'] or monthly['real_micro']):
        outcome['status'] = 'FAIL'
        outcome['reason'] = f"monthly ledger has entries for this family without a cash call: {monthly['line']} line present"
        return outcome
    if outcome['request'] == 0:
        outcome['reason'] = 'no verified closed receipt (only unused/consumed assurance)'
        return outcome
    outcome['status'] = 'PASS'
    outcome['reason'] = f"{outcome['request']} closed receipts; cash {outcome['cash_micro']} micro, equivalent {outcome['equivalent_micro']} micro"
    return outcome


def closing_evidence(home: Path, rid: str, prefix: str | None) -> dict:
    from run_store import TERMINAL, FileLock, RunStore, process_identity
    outcome: dict = {'may_close': False, 'reason': '', 'runs': [rid], 'liability_usd': None, 'receipt': None}
    store = RunStore(home)
    try:
        record = store.read(rid)
    except (OSError, ValueError) as exc:
        outcome['reason'] = f'{rid} record unreadable: {type(exc).__name__}'
        return outcome
    children = record.get('project_run_ids') or []
    if not isinstance(children, list) or not all(isinstance(c, str) for c in children):
        outcome['reason'] = f'{rid} child run list uncertain'
        return outcome
    ids = sorted({rid, *children})
    outcome['runs'] = ids
    for id in ids:
        try:
            k = record if id == rid else store.read(id)
        except (OSError, ValueError) as exc:
            outcome['reason'] = f'{id} record unreadable: {type(exc).__name__}'
            return outcome
        if k.get('state') not in TERMINAL:
            outcome['reason'] = f"{id} not terminal ({k.get('state')})"
            return outcome
        lock = FileLock(store.path(id) / '.owner.lock')
        if not lock.acquire(blocking=False):
            outcome['reason'] = f'{id} owner lock held (active writer)'
            return outcome
        lock.close()
        processes = [k.get('phase_process')]
        for path in [*sorted(store.path(id).glob('tasks/*/attempts/*/process.json')), *sorted(store.path(id).glob('tasks/qa/tool-checks/*/process.json'))]:
            try:
                processes.append(json.loads(path.read_text()).get('process_identity'))
            except (OSError, ValueError, AttributeError):
                outcome['reason'] = f'{id} process record unreadable: {path.parent.name}'
                return outcome
        for id_info in processes:
            if (isinstance(id_info, dict) and type(id_info.get('pid')) is int and id_info['pid'] > 1
                    and process_identity(id_info['pid']) == id_info):
                outcome['reason'] = f"{id} process is live (pid {id_info['pid']})"
                return outcome
    m = receipt_gates(home, ids, prefix)
    outcome['receipt'] = {a: m[a] for a in ('status', 'line', 'request', 'cash_micro', 'equivalent_micro', 'pending', 'open_assurance')}
    if m['status'] != 'PASS':
        outcome['reason'] = f"receipt gate {m['status']}: {m['reason']}"
        return outcome
    unknown = sorted(s for s, v in m['providers'].items() if v['unit'] == 'unknown')
    if unknown:
        outcome['reason'] = 'receipt unit for an unknown provider: ' + ', '.join(unknown)
        return outcome
    outcome.update(may_close=True, liability_usd=round((m['cash_micro'] + m['equivalent_micro']) / 1e6, 6), reason=m['reason'])
    return outcome


def cutting_setup() -> None:
    def cut(signum, frame):
        raise KeyboardInterrupt(signal.Signals(signum).name)
    for indicator in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(indicator, cut)


def cuts_postpone() -> None:
    for indicator in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(indicator, signal.SIG_IGN)


def hooks_setup() -> None:
    for d in ('chain', 'logs', 'hooks'):
        (HOME / d).mkdir(parents=True, exist_ok=True)
    for name in ('pre-push', 'pre-commit', 'commit-msg'):
        if not (HOME / 'hooks' / name).exists():
            shutil.copy(ENGINE / 'hooks' / name, HOME / 'hooks' / name)
            (HOME / 'hooks' / name).chmod(0o700)


class ProposalStance(Exception):
    """--only-proposal: the engine's permission proposal was taken, no approval/call was made (for feasibility)."""

    def __init__(self, proposal: dict) -> None:
        super().__init__('proposal only')
        self.proposal = proposal


class Package:
    STANCE_WAIT_SEC = 60.0

    def __init__(self, id: str, fixture_dir: Path, minute: float, cap: float, path: str, output: Path | None, mode: str,
                 hash_check: bool = True, package_cap: float = 2.0, only_proposal: bool = False, liability_refund: bool = False) -> None:
        if HOME is None or HOME == FALLBACK_HOME_DIR or str(HOME).startswith(str(FALLBACK_HOME_DIR) + os.sep):
            raise SystemExit('CADRE_HOME must be an isolated test root (the default runtime cannot be used)')
        if not math.isfinite(minute) or minute < 1 or not math.isfinite(cap) or cap < 0:
            raise SystemExit('duration must be at least 1 minute and cap must be a finite nonnegative USD amount')
        self.ident, self.minute, self.cap, self.path, self.mode = id, minute, cap, path, mode
        self.fixture = (fixture_dir / id).resolve()
        if not self.fixture.is_dir():
            raise SystemExit(f'fixture absent: {self.fixture}')
        self.stamp = time.strftime('%Y%m%d-%H%M%S')
        self.last_monotonic = time.monotonic() + minute * 60
        self.directory = (output or HOME / 'reports/test-archive' / f'{id}-{path}-{self.stamp}').resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.operation = HOME / 'scratch/acceptance' / f'{id}-{self.stamp}'
        self.operation.mkdir(parents=True, exist_ok=True)
        self.events: list[str] = []
        self.checks: list[dict] = []
        self.runs: list[str] = []
        self.fixture_hash = self.fixture_verify(fixture_dir, hash_check)
        self.root = ROOT
        self.env_extra: dict[str, str] = {}
        self.only_proposal = only_proposal
        self.proposals: list[dict] = []
        self.chain = select_chain_environment(dict(os.environ))
        self.pay = PackageShare(self.directory.parent / 'acceptance-package-pay.json', package_cap)
        self.chargeback = liability_refund
        self.refund_events: list[dict] = []
        self.fake_prepare() if path == 'fake' else self.live_prepare()

    # --- preparation -------------------------------------------------------------------------------------------------
    def fixture_verify(self, fixture_dir: Path, mandatory: bool) -> dict:
        manifest = fixture_dir.parent / 'fixture-manifest.json'
        observed = {str(p.relative_to(fixture_dir)): sha(p) for p in sorted(self.fixture.rglob('*')) if p.is_file()}
        if not manifest.exists():
            if mandatory:
                raise SystemExit(f'fixture-manifest.json absent: {manifest}')
            return {'manifest': None, 'files': observed}
        expected = {k: v for k, v in json.loads(manifest.read_text()).items() if k.startswith(self.ident + '/')}
        difference = sorted(k for k in set(expected) | set(observed) if expected.get(k) != observed.get(k))
        if difference and mandatory:
            raise SystemExit('fixture does not match the manifest: ' + ', '.join(difference))
        return {'manifest': str(manifest), 'manifest_sha256': sha(manifest), 'files': observed, 'difference': difference}

    def fake_prepare(self) -> None:
        import test_end_to_end
        from qa_fixture import instructions
        from test_run_sessions import CATALOG
        root = HOME / 'runner'
        if not root.exists():
            instructions(root)
            for sub in ('bin', 'lib', 'hooks'):
                (root / sub).mkdir(parents=True)
                for path in (ENGINE / sub).iterdir():
                    if path.is_file():
                        shutil.copy(path, root / sub / path.name)
                    elif path.is_dir() and path.name != '__pycache__':
                        shutil.copytree(path, root / sub / path.name, ignore=shutil.ignore_patterns('__pycache__'))
            (root / 'lib/provider_budget.py').write_text(test_end_to_end.FAKE_BUDGET)
            shutil.copy(TESTS / 'qa_fixture.py', root / 'lib/qa_fixture.py')
        self.root = root
        hooks_setup()
        (HOME / 'chain/expense-coefficient.txt').write_text('1.0\n')
        (HOME / 'chain/go-daily-cap.txt').write_text('2.0\n')
        (HOME / 'models.json').write_text(json.dumps(CATALOG))
        cfg = HOME / 'opencode-cfg'
        cfg.mkdir(exist_ok=True)
        (cfg / 'opencode.jsonc').write_text('{}')
        (HOME / 'acceptance-scenario.json').write_text(json.dumps({'scenario': self.ident, 'mode': self.mode}))
        self.wrapper = HOME / 'acceptance-fake-opencode.py'
        shutil.copyfile(TESTS / 'acceptance_fake.py', self.wrapper)
        self.wrapper.chmod(0o700)
        self.env_extra = {'CADRE_MODEL_CATALOG': str(HOME / 'models.json'), 'OPENCODE_DB': str(HOME / 'absent.db'),
                       'CADRE_CHAIN_QUALITY': 'opencode-go/qwen3.8-max',
                       'OPENCODE_CFG': str(cfg), 'CLAUDE_BIN': '/nonexistent/claude', 'CADRE_OC_LOG': str(HOME / 'oc.log'),
                       'CADRE_PROGRESS_POLL': '1', 'CADRE_STALL_SEC': '600', 'CADRE_ABORT_SEC': '600', 'CADRE_MAX_RESUME': '1',
                       'CADRE_WAIT_THRESHOLD_SEC': '0', 'CADRE_GUARD_OWNER_PID': str(os.getpid()),
                       'CADRE_CRITICAL_FILES': f"{HOME / 'chain/expense-coefficient.txt'}:{HOME / 'chain/go-daily-cap.txt'}",
                       'CADRE_ROUTER_RECORD': str(HOME / 'chain/model_router.jsonl'), 'CADRE_ROUTER_STATUS': str(HOME / 'chain/provider-status.json')}

    def live_prepare(self) -> None:
        import live_scenario
        hooks_setup()
        self.wrapper = self.directory / f'{self.path}-only-opencode.py'
        self.wrapper.write_text(live_scenario.wrapper_text(self.path))
        self.wrapper.chmod(0o700)
        self.env_extra = dict(live_scenario.chain_environment(self.path, 'cheap'))
        prefix = live_scenario.PROVIDER[self.path] + '/'
        outside = chain_verify(self.chain, prefix)
        if outside:
            raise SystemExit(f'CADRE_CHAIN_* contains models outside the allowed path (expected {prefix}*): {outside}; no request was recorded')
        self.go_once = live_scenario.Scenario.go_status(self)

    def go_status(self) -> dict:
        import live_scenario
        return live_scenario.Scenario.go_status(self)

    def environment(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(('CADRE_', 'OPENCODE_', 'E2E_'))}
        env.update(CADRE_HOME=str(HOME), CADRE_ENGINE_ROOT=str(self.root), CADRE_REAL_OPENCODE=str(self.wrapper), CADRE_CLAUDE_PLAN='0',
                   CADRE_RADAR='0', CADRE_WATCH='0', CADRE_PREFILTER_LLM='0', **GIT_ID)
        env.update({**self.env_extra, **self.chain})
        return env

    # --- basic operations --------------------------------------------------------------------------------------------
    def event(self, text: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {text}"
        self.events.append(line)
        print(line, flush=True)

    def duration_remained(self) -> float:
        return max(0.0, self.last_monotonic - time.monotonic())

    def cli(self, *args: str, data: dict | None = None, input: str | None = None, timeout: float | None = None,
            script: str = 'lib/run_store.py') -> subprocess.CompletedProcess:
        if not self.duration_remained():
            raise TimeoutError('scenario deadline expired; no new command issued')
        return subprocess.run([PY, str(self.root / script), *args], input=json.dumps(data) if data is not None else input,
                              capture_output=True, text=True, env=self.environment(), timeout=min(timeout or 300, max(1.0, self.duration_remained())))

    def project_ref_save(self, repo: Path, entry_name: str, qa: str, project_ref_md: str, policy: str) -> None:
        pd = HOME / 'projects' / entry_name
        pd.mkdir(parents=True)
        for file, content in (('repo-path', str(repo)), ('base-branch', 'main'), ('qa-command', qa), ('PROJECT.md', project_ref_md), ('POLICY.md', policy)):
            (pd / file).write_text(content + '\n')

    def repo_setup(self, entry_name: str, qa: str, project_ref_md: str, policy: str, excluded: tuple[str, ...] = ('request.txt',)) -> tuple[Path, str]:
        repo = self.operation / entry_name / 'repo'
        shutil.copytree(self.fixture, repo, ignore=shutil.ignore_patterns(*excluded, '__pycache__'))
        git(repo, 'init', '-q', '-b', 'main')
        git(repo, 'add', '.')
        git(repo, 'commit', '-qm', 'acceptance package baseline')
        self.project_ref_save(repo.resolve(), entry_name, qa, project_ref_md, policy)
        return repo.resolve(), git(repo, 'rev-parse', 'HEAD')

    def request_json(self, project_ref: str, target: Path, mode: str, prompt: str, output: str, acceptance: str, *, round_no: str = 'existing',
                   backlog: str = '', difficulty: str | None = 'easy', extra: str = '', cap: float | None = None) -> dict:
        return dict(submission_id=f'{self.ident.lower()}-{self.stamp}{extra}', project=project_ref, target_path=str(target), target_kind=round_no,
                    input_mode=mode, prompt=prompt, output=output, acceptance=acceptance, preference='economic', difficulty_override=difficulty,
                    duration_minutes=str(int(self.minute)), budget_usd=format(round(self.cap if cap is None else cap, 6), 'f'),
                    backlog_path=backlog, source_token='')

    def prefix(self) -> str | None:
        if self.path == 'fake':
            return None
        import live_scenario
        return live_scenario.PROVIDER[self.path] + '/'

    def pending_close(self) -> None:
        if self.path == 'fake':
            return
        try:
            r = subprocess.run([PY, '-c', 'import sys; sys.path.insert(0, sys.argv[1]); import provider_budget; print(provider_budget.or_pending_close())',
                                str(self.root / 'lib')], env=self.environment(), capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            return
        if r.stdout.strip() not in ('', '0'):
            self.event(f'pending receipt reconciliation: {r.stdout.strip()} rows closed from generation records')

    def refund_audit(self) -> list[dict]:
        self.pending_close()
        events = self.pay.refund_audit(HOME, self.prefix())
        for o in events:
            if o['outcome'] in ('chargeback', 'overrun', 'late_receipt', 'go_back_taken'):
                self.event(f"package allocation {o['outcome']}: {o['rid']} allocation {o['usd']} liability {o.get('liability_usd')} ({o['reason']})")
        self.refund_events.extend(events)
        return events

    def request(self, raw: dict) -> str:
        label = raw['submission_id']
        if self.chargeback:
            self.refund_audit()
        self.pay.allocation(self.ident, label, float(raw['budget_usd']))   # CXR-02: the shared package share comes before the request record
        r = self.cli('request', data=raw)
        if r.returncode != 0:
            raise RuntimeError(f'request rejected: {r.stderr.strip()}')
        rid = json.loads(r.stdout)['run_id']
        self.pay.bind(label, rid)
        self.runs.append(rid)
        (self.directory / f'request-{rid[-8:]}.json').write_text(json.dumps(raw, ensure_ascii=False, indent=2))
        self.event(f'request saved: {rid}')
        return rid

    def approve(self, rid: str) -> dict:
        r = self.cli('request-proposal', rid)
        if r.returncode != 0:
            raise RuntimeError(f'proposal could not be generated: {r.stderr.strip()}')
        proposal = json.loads(r.stdout)
        (self.directory / f'proposal-{rid[-8:]}.json').write_text(json.dumps(proposal, ensure_ascii=False, indent=2))
        if self.path != 'fake':
            import live_scenario
            prefix = live_scenario.PROVIDER[self.path] + '/'
            outside = sorted({m for pool in (proposal.get('action', {}).get('models') or {}).values() for m in pool if not str(m).startswith(prefix)})
            if outside:
                raise RuntimeError(f'proposal lists models outside the allowed path (expected {prefix}*): {outside}; no approval or call. '
                                   'Pin only the allowed provider chain with CADRE_CHAIN_<ROLE> (the driver passes it unchanged to the subprocess) or show the proposal to the user.')
        if self.only_proposal:
            self.proposals.append({'rid': rid, 'budget_usd': proposal.get('action', {}).get('budget_usd'), 'models': proposal.get('action', {}).get('models'),
                                  'duration_minutes': proposal.get('action', {}).get('duration_minutes')})
            raise ProposalStance(proposal)
        first = self.cli('request-approve', rid, data=proposal)
        if first.returncode != 0:
            raise RuntimeError(f'first approval rejected: {first.stderr.strip()}')
        second = self.cli('request-approve', rid, json.loads(first.stdout)['confirmation'], data=proposal)
        if second.returncode != 0:
            raise RuntimeError(f'second approval rejected: {second.stderr.strip()}')
        self.event(f"engine permission bound: models={proposal.get('action', {}).get('models')} duration={proposal.get('action', {}).get('duration_minutes')} "
                  f"total={proposal.get('action', {}).get('budget_usd')}")
        return proposal

    def plan(self, rid: str) -> int:
        r = self.cli('request-plan', rid, timeout=self.duration_remained())
        (self.directory / f'plan-{rid[-8:]}.log').write_text(r.stdout + r.stderr)
        self.event(f'plan rc={r.returncode}')
        return r.returncode

    def execute(self, rid: str, proceed: bool = False) -> int:
        if not self.duration_remained():
            raise TimeoutError('scenario deadline expired; execution was not started')
        command = [PY, str(self.root / 'lib/run_store.py'), 'run', rid, *(['--proceed'] if proceed else [])]
        with open(self.directory / f'coordinator-{rid[-8:]}.log', 'a') as log:
            proc = subprocess.Popen(command, env=self.environment(), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while proc.poll() is None and self.duration_remained():
                time.sleep(min(2.0, self.duration_remained()))
        except KeyboardInterrupt:
            cuts_postpone()
            self.event('INTERRUPTED: stopping the run through the engine cancel path')
            self.stop(proc, rid)
            raise
        if proc.poll() is None:
            self.event('DEADLINE EXPIRED: stopping the run through the engine cancel path')
            self.stop(proc, rid)
        self.event(f"run{' --proceed' if proceed else ''} rc={proc.returncode} status={self.record(rid).get('state')} reason={self.record(rid).get('reason')}")
        return proc.returncode if proc.returncode is not None else -1

    def cancel(self, rid: str) -> bool:
        try:
            r = subprocess.run([PY, str(self.root / 'lib/run_store.py'), 'cancel', rid], env=self.environment(), capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            self.event(f'cancel command could not run: {rid}: {type(exc).__name__}: {exc}')
            return False
        return rid in r.stdout

    def orphans_sum(self, rid: str) -> None:
        from run_store import RunStore, reclaim_orphans
        try:
            reclaim_orphans(RunStore(HOME), {'run_id': rid})
        except (OSError, ValueError, KeyError, TypeError, ImportError, subprocess.SubprocessError) as exc:
            self.event(f'orphan process could not be reaped: {rid}: {type(exc).__name__}: {exc}')

    def stop(self, proc: subprocess.Popen, rid: str) -> None:
        self.cancel(rid)
        try:
            proc.wait(timeout=self.STANCE_WAIT_SEC)
        except subprocess.TimeoutExpired:
            self.event(f'coordinator did not finish cancellation within {self.STANCE_WAIT_SEC:.0f} seconds: SIGKILL and orphan process cleanup')
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            self.orphans_sum(rid)

    # --- reading -----------------------------------------------------------------------------------------------------
    def record(self, rid: str) -> dict:
        try:
            return json.loads((HOME / 'runs' / rid / 'run.json').read_text())
        except (OSError, ValueError):
            return {}

    def plan_view(self, rid: str) -> dict:
        from run_store import RunStore
        try:
            return RunStore(HOME).plan_view(rid)
        except Exception as exc:  # noqa: BLE001 - evidence gathering, not an acceptance decision
            return {'error': f'{type(exc).__name__}: {exc}'}

    def attempts(self, rid: str) -> list[dict]:
        outcome = []
        for path in sorted((HOME / 'runs' / rid).glob('tasks/*/attempts/*/attempt.json')):
            try:
                d = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            outcome.append({'task': path.parents[2].name, 'role': d.get('role'), 'model': d.get('model'), 'session_id': d.get('session_id'),
                          'outcome': d.get('outcome'), 'exit_code': d.get('exit_code')})
        return outcome

    def calls(self) -> list[dict]:
        path = HOME / 'acceptance-calls.jsonl'
        return [json.loads(s) for s in path.read_text().splitlines()] if path.exists() else []

    def approved(self, record: dict) -> bool:
        from outcome import approved
        try:
            return bool(approved(record))
        except Exception as exc:  # noqa: BLE001
            self.event(f'approved() error: {type(exc).__name__}: {exc}')
            return False

    def product_check(self, wt: str, command: str, base: str, protected: dict[str, str]) -> dict:
        if not wt or not Path(wt).is_dir():
            return {'worktree': wt, 'rc': None, 'changed': [], 'protected_same': False}
        rc, output = safe_command(command, Path(wt), self.scratch('product'), 300)
        changed = git(Path(wt), 'diff', '--name-only', base, 'HEAD').splitlines() + \
            [x[2:].strip() for x in git(Path(wt), 'status', '--porcelain').splitlines() if '__pycache__' not in x]
        return {'worktree': wt, 'rc': rc, 'output': output[-1500:], 'changed': sorted(set(changed)),
                'protected_same': all((Path(wt) / d).is_file() and sha(Path(wt) / d) == h for d, h in protected.items())}

    def scratch(self, entry_name: str) -> Path:
        """Sandbox temporary area (the only writable external directory); separate for every call."""
        import uuid
        path = self.operation / 'scratch' / f'{entry_name}-{uuid.uuid4().hex[:8]}'
        path.mkdir(parents=True, exist_ok=True)
        return path

    def shared_evidence(self, rid: str, qa: str, base: str, protected: dict[str, str]) -> dict:
        self.pending_close()
        k = self.record(rid)
        fr = k.get('final_result') or {}
        ids = sorted({rid, *(k.get('project_run_ids') or [])})
        prefix = None
        if self.path != 'fake':
            import live_scenario
            prefix = live_scenario.PROVIDER[self.path] + '/'
        m = receipt_gates(HOME, ids, prefix)
        return {'runs': ids, 'status': k.get('state'), 'phase': k.get('phase'), 'reason': k.get('reason'), 'final': fr.get('status'),
                'qa': (fr.get('qa') or {}).get('status'), 'qa_round': (fr.get('qa') or {}).get('kind'),
                'judge': (fr.get('review') or {}).get('status'), 'judge_independent': (fr.get('review') or {}).get('independent'),
                'delivery': (fr.get('delivery') or {}).get('kind'), 'attempts': self.attempts(rid), 'receipt': m['providers'],
                'receipt_status': m, 'unpermitted_path': m['unpermitted'], 'pending': m['pending'] + m['open_assurance'],
                'product': self.product_check(k.get('worktree') or '', qa, base, protected) if qa else {'worktree': k.get('worktree')},
                'approved': self.approved(k)}

    def live_gates(self, e: dict, on: str) -> None:
        """Required on the live path: no unallowed provider, no pending receipt. The fake path produces no receipts; NOT_APPLICABLE is recorded."""
        if self.path == 'fake':
            self.checks.append({'id': f'{on}-L1', 'round': 'limit', 'method': 'driver', 'expected': 'unpermitted path absent; pending receipt 0',
                                    'observed': 'fake provider produces no receipts', 'outcome': 'NOT_APPLICABLE', 'evidence': 'live mode only'})
            return
        m = e['receipt_status']
        check(self.checks, f'{on}-L0', 'limit', 'receipt ledger readable with family records present (zero records do not prove closure)',
                {'readable': m['readable'], 'line': m['line'], 'error': m['error']}, True if m['readable'] and m['line'] else None)
        check(self.checks, f'{on}-L1', 'limit', 'only the allowed provider path', m['unpermitted'], (not m['unpermitted']) if m['readable'] and m['line'] else None)
        check(self.checks, f'{on}-L2', 'limit', 'zero pending/uncertain receipts and open assurances; at least one verified closed receipt',
                {'pending': m['pending'], 'open_assurance': m['open_assurance'], 'request': m['request']},
                (m['pending'] == 0 and m['open_assurance'] == 0 and m['request'] > 0) if m['readable'] and m['line'] else None)
        check(self.checks, f'{on}-L3', 'limit', 'two-ledger agreement (cash rows in monthly ledger; no monthly cash rows for Go-only runs)',
                {'monthly': m['monthly'], 'status': m['status'], 'reason': m['reason']},
                (m['status'] == 'PASS') if m['readable'] and m['line'] else None)

    def roles_check(self, on: str, e: dict, expected: set[str], has_worker: bool) -> None:
        roles = {d['role'] for d in e['attempts']}
        check(self.checks, f'{on}-R', 'normal', f'attempt roles {sorted(expected)} ' + ('(including worker)' if has_worker else '(worker absent)'),
                sorted(roles), expected <= roles and (has_worker == any(d['role'] not in ('plan', 'review') for d in e['attempts'])))
        sessions = [d['session_id'] for d in e['attempts'] if d['role'] == 'review']
        check(self.checks, f'{on}-S', 'limit', 'review sessions are separate and unique', sessions, len(sessions) == len(set(sessions)) and bool(sessions))

    # --- scenarios ------------------------------------------------------------------------------------------------
    def r01(self) -> dict:
        qa = f'{PY} -m unittest -v test_calc'
        repo, base = self.repo_setup('acceptance-r01', qa, 'Integer addition using only standard unittest.',
                                    'Use-at acceptance. Only calc.py changes; test_calc.py remains unchanged. No installation, downloads or remote Git.')
        protected = {'test_calc.py': sha(repo / 'test_calc.py')}
        rid = self.request(self.request_json('acceptance-r01', repo, 'goal', (self.fixture / 'request.txt').read_text(), 'Corrected calc.py',
                                         'the three tests in test_calc.py pass; only calc.py changes'))
        self.approve(rid)
        self.plan(rid)
        rc = self.execute(rid)
        e = self.shared_evidence(rid, qa, base, protected)
        k = self.checks
        check(k, 'R01-N1', 'normal', 'run completed, last acceptance PASS, delivery reviewed-worktree', (rc, e['status'], e['final'], e['delivery']),
                rc == 0 and e['status'] == 'completed' and e['final'] == 'PASS' and e['delivery'] == 'reviewed-worktree')
        check(k, 'R01-N2', 'normal', 'product acceptance command passes', e['product'].get('rc'), e['product'].get('rc') == 0)
        check(k, 'R01-N3', 'normal', 'real QA and clean judge PASS, with an independent judge', (e['qa'], e['judge'], e['judge_independent']),
                e['qa'] == 'PASS' and e['judge'] == 'PASS' and bool(e['judge_independent']))
        self.roles_check('R01', e, {'plan', 'review'}, True)
        check(k, 'R01-O1', 'negative', 'test_calc.py unchanged (no scope or test changes)', e['product'].get('protected_same'), e['product'].get('protected_same'))
        check(k, 'R01-O2', 'negative', 'only calc.py changes', e['product'].get('changed'), e['product'].get('changed') == ['calc.py'])
        record = self.record(rid)
        wt = Path(record.get('worktree') or '')
        if wt.is_dir() and e['approved']:
            original = (wt / 'calc.py').read_bytes()
            (wt / 'calc.py').write_bytes(original + b'# change after QA\n')
            try:
                check(k, 'R01-O3', 'negative', 'a product change after QA/judge invalidates acceptance', self.approved(record), not self.approved(record))
            finally:
                (wt / 'calc.py').write_bytes(original)
            check(k, 'R01-O4', 'limit', 'restoring the product makes the same acceptance valid again', self.approved(record), self.approved(record))
        else:
            check(k, 'R01-O3', 'negative', 'a product change after QA/judge invalidates acceptance', 'worktree/acceptance absent', None)
        bound(k, 'R01-O5', 'negative', 'failed, empty or skipped required QA and weakened tests cannot produce acceptance',
              ['test_qa_scope', 'test_outcome', 'test_judge_policy'])
        self.live_gates(e, 'R01')
        return {'evidence': e}

    def backlog_install(self, entry_name: str) -> tuple[Path, str, str, dict[str, str]]:
        qa = f'{PY} -m unittest discover -s tests -t . -v'
        repo, base = self.repo_setup(entry_name, qa, 'Small text library (text.py, text_cli.py) using only the standard library.',
                                    'Use-at acceptance. tests/ and BACKLOG.md remain unchanged. No installation, downloads or remote Git.')
        protected = {str(p.relative_to(repo)): sha(p) for p in sorted(repo.rglob('*.py')) if 'tests' in p.parts}
        protected['BACKLOG.md'] = sha(repo / 'BACKLOG.md')
        return repo, base, qa, protected

    def item_outcomes(self, wt: str, items: list[str]) -> dict[str, int | None]:
        if not wt or not Path(wt).is_dir():
            return {m: None for m in items}
        return {m: safe_command(f'{PY} -m unittest -v tests.test_{m.lower()}', Path(wt), self.scratch('item'), 120)[0] for m in items}

    def r02(self) -> dict:
        return self.backlog_scenario('R02', 'acceptance-r02', verified=set())

    def r03(self) -> dict:
        return self.backlog_scenario('R03', 'acceptance-r03', verified={'B001', 'B002'})

    def backlog_scenario(self, on: str, entry_name: str, verified: set[str]) -> dict:
        items = ['B001', 'B002', 'B003', 'B004', 'B005']
        repo, base, qa, protected = self.backlog_install(entry_name)
        k = self.checks
        once = self.item_outcomes(str(repo), items)
        check(k, f'{on}-P0', 'limit', 'start: ' + (f'{sorted(verified)} pass, remaining items fail' if verified else 'five items fail'), once,
                all((once[m] == 0) == (m in verified) for m in items))
        old_text = (repo / 'text.py').read_text()
        rid = self.request(self.request_json(entry_name, repo, 'backlog', (self.fixture / 'request.txt').read_text(),
                                         'B001-B005 behavior and CLI', 'every item’s acceptance command and all tests pass', backlog='BACKLOG.md'))
        self.approve(rid)
        self.plan(rid)
        rc = self.execute(rid)
        e = self.shared_evidence(rid, qa, base, protected)
        view = self.plan_view(rid)
        plan = ((view.get('history') or [{}])[-1].get('plan') or {})
        source = {s.get('id'): s for s in plan.get('source_items') or []}
        e['item_rc'] = self.item_outcomes(e['product'].get('worktree') or '', items)
        e['source_items'] = {i: {'status': s.get('status'), 'tasks': s.get('tasks')} for i, s in source.items()}
        check(k, f'{on}-N1', 'normal', 'run completed, last acceptance PASS, reviewed-worktree', (rc, e['status'], e['final'], e['delivery']),
                rc == 0 and e['status'] == 'completed' and e['final'] == 'PASS' and e['delivery'] == 'reviewed-worktree')
        check(k, f'{on}-N2', 'normal', 'every item passes its own acceptance command', e['item_rc'], all(v == 0 for v in e['item_rc'].values()))
        check(k, f'{on}-N3', 'normal', 'whole-product acceptance (discover) passes with an independent judge', (e['product'].get('rc'), e['judge_independent']),
                e['product'].get('rc') == 0 and bool(e['judge_independent']))
        check(k, f'{on}-N4', 'normal', 'five source ids are mapped to hashes in the plan', sorted(source), sorted(source) == items)
        if verified:
            tasked = {t for m in verified for t in (source.get(m) or {}).get('tasks') or []}
            check(k, f'{on}-N5', 'normal', 'previously completed items are verified without tasks', {m: e['source_items'].get(m) for m in sorted(verified)},
                    all((source.get(m) or {}).get('status') == 'verified' and not (source.get(m) or {}).get('tasks') for m in verified))
            check(k, f'{on}-O1', 'negative', 'completed work is not produced or billed again (no worker attempt for verified items)',
                    {'verified_tasks': sorted(tasked), 'worker_tasks': sorted({d['task'] for d in e['attempts'] if d['role'] not in ('plan', 'review')})},
                    not tasked)
            wt = Path(e['product'].get('worktree') or '')
            fresh_text = (wt / 'text.py').read_text() if (wt / 'text.py').is_file() else ''
            old_body = old_text.split('def longest_word')[0].strip()
            check(k, f'{on}-O2', 'negative', 'B001/B002 implementation (words/word_count/reverse_convert) preserved exactly', old_body in fresh_text, old_body in fresh_text)
        else:
            check(k, f'{on}-N5', 'normal', 'dependencies in the plan (B003->B001, B004->B001/B003, B005->B004) preserved as task dependencies',
                    [(g.get('id'), g.get('dependent')) for g in plan.get('tasks') or []],
                    any(g.get('dependent') for g in plan.get('tasks') or []) or len(plan.get('tasks') or []) == 1)
        self.roles_check(on, e, {'plan', 'review'}, True)
        check(k, f'{on}-O3', 'negative', 'tests/ and BACKLOG.md remain unchanged', e['product'].get('protected_same'), e['product'].get('protected_same'))
        bound(k, f'{on}-O4', 'negative', 'missing items, unsourced extra work or child PASS alone do not establish whole-product completion',
              ['test_request_pipeline.RequestPipelineTests.test_omitted_backlog_item_blocks_plan_publication',
               'test_request_pipeline.RequestCLITests.test_verified_backlog_red_qa_cannot_trigger_reimplementation_or_claim_completion',
               'test_project_end_to_end'])
        if verified:
            bound(k, f'{on}-O5', 'negative', 'no duplicate work or billing after outage', ['test_recovery', 'test_night_scenarios', 'test_run_sessions'])
            record = self.record(rid)
            wt = Path(e['product'].get('worktree') or '')
            if (wt / 'text.py').is_file() and e['approved']:
                original = (wt / 'text.py').read_bytes()
                (wt / 'text.py').write_bytes(original + b'\n# change after acceptance\n')
                try:
                    check(k, f'{on}-O6', 'negative', 'a product change after acceptance prevents reusing the old acceptance', self.approved(record), not self.approved(record))
                finally:
                    (wt / 'text.py').write_bytes(original)
            else:
                check(k, f'{on}-O6', 'negative', 'a product change after acceptance prevents reusing the old acceptance', 'worktree/acceptance absent', None)
            once_attempt = len(e['attempts'])
            r = self.cli('run', rid)
            check(k, f'{on}-O7', 'limit', 'restarting completed work makes no new calls or charges',
                    (r.returncode, len(self.attempts(rid)) - once_attempt), r.returncode == 0 and len(self.attempts(rid)) == once_attempt)
            bound(k, f'{on}-O8', 'negative', 'changed source/original request cannot be accepted under an old review',
                  ['test_request_pipeline.RequestCLITests.test_verified_backlog_zero_budget_and_changed_source_do_not_start_another_call',
                   'test_request_pipeline.RequestPipelineTests.test_cached_legacy_review_cannot_accept_a_changed_original_request'])
        self.live_gates(e, on)
        return {'evidence': e, 'plan': plan}

    def r04(self) -> dict:
        repo, base, qa, protected = self.backlog_install('acceptance-r04')
        once = tree_hashes(repo)
        rid = self.request(self.request_json('acceptance-r04', repo, 'plan_only', (self.fixture / 'request.txt').read_text(), 'Reviewed plan',
                                         'B001-B005 ids, criteria and dependencies in the plan; no implementation'))
        self.approve(rid)
        self.plan(rid)
        rc = self.execute(rid)
        e = self.shared_evidence(rid, '', base, protected)
        k = self.checks
        record = self.record(rid)
        delivery = record.get('plan_delivery') or {}
        try:
            product = json.loads(Path(delivery['artifact']).read_text())
        except (KeyError, OSError, ValueError, TypeError):
            product = {}
        text = json.dumps(product, ensure_ascii=False)
        check(k, 'R04-N1', 'normal', 'plan delivered: completed/plan-delivered, separate review PASS', (rc, e['status'], e['phase'], (product.get('review') or {}).get('status')),
                rc == 0 and e['status'] == 'completed' and e['phase'] == 'plan-delivered' and (product.get('review') or {}).get('status') == 'PASS')
        check(k, 'R04-N2', 'normal', 'the plan carries five source ids', [b for b in ('B001', 'B002', 'B003', 'B004', 'B005') if b in text],
                all(b in text for b in ('B001', 'B002', 'B003', 'B004', 'B005')))
        check(k, 'R04-N3', 'normal', 'delivery bound to the original request hash', product.get('request_hash') == record.get('request_input_hash'),
                bool(product) and product.get('request_hash') == record.get('request_input_hash'))
        self.roles_check('R04', e, {'plan', 'review'}, False)
        check(k, 'R04-O1', 'negative', 'no implementation or child starts: no worker attempt or worktree',
                (sorted({d['role'] for d in e['attempts']}), record.get('worktree')),
                all(d['role'] in ('plan', 'review') for d in e['attempts']) and not record.get('worktree'))
        check(k, 'R04-O2', 'negative', 'source repository and tests/ remain unchanged', tree_hashes(repo) == once, tree_hashes(repo) == once)
        once_attempt = len(e['attempts'])
        r = self.cli('run', rid)
        check(k, 'R04-O3', 'limit', 'restarting makes no new paid calls or implementation', (r.returncode, len(self.attempts(rid)) - once_attempt),
                r.returncode == 0 and len(self.attempts(rid)) == once_attempt)
        bound(k, 'R04-O4', 'negative', 'no review, dispatch or revival while a critical decision is unanswered',
              ['test_request_pipeline.RequestPipelineTests.test_critical_boundaries_wait_without_review_dispatch_retry_or_revival',
               'test_request_pipeline.RequestPipelineTests.test_answer_requires_a_new_plan_and_clean_review_and_does_not_grant_spending_permission'])
        bound(k, 'R04-O5', 'negative', 'changed source cannot be accepted under an old plan/review',
              ['test_request_pipeline.RequestPipelineTests.test_cached_legacy_review_rejects_changed_source_without_another_paid_review',
               'test_request_pipeline.RequestPipelineTests.test_real_request_delivery_cannot_reuse_a_pass_for_changed_plan_content'])
        self.live_gates(e, 'R04')
        return {'evidence': e, 'delivery': {kk: product.get(kk) for kk in ('request_hash', 'review')}}

    def r05(self) -> dict:
        repo, base = self.repo_setup('acceptance-r05', 'true', 'Cadre source copy (three files): review only.',
                                    'Use-at acceptance. Source remains unchanged; no code repair, installation, downloads or remote Git.')
        once = tree_hashes(repo)
        prompt = 'ROUND: review\n' + (self.fixture / 'request.txt').read_text()
        rid = self.request(self.request_json('acceptance-r05', repo, 'goal', prompt, 'Review report with file:line evidence',
                                         'every claim linked to a source line; source unchanged; unknowns remain explicit'))
        self.approve(rid)
        self.plan(rid)
        rc = self.execute(rid)
        e = self.shared_evidence(rid, '', base, {})
        k = self.checks
        record = self.record(rid)
        delivery = record.get('review_delivery') or {}
        try:
            product = json.loads(Path(delivery['artifact']).read_bytes())
        except (KeyError, OSError, ValueError, TypeError):
            product = {}
        evidence = product.get('evidence') or []
        check(k, 'R05-N1', 'normal', 'review delivered: completed/review-delivered, final acceptance PASS', (rc, e['status'], e['phase'], e['final']),
                rc == 0 and e['status'] == 'completed' and e['phase'] == 'review-delivered' and e['final'] == 'PASS')
        check(k, 'R05-N2', 'normal', 'code QA does not apply (NOT_APPLICABLE), report QA PASS', ((((record.get('final_result') or {}).get('qa') or {}).get('code_qa') or {}).get('status'), e['qa']),
                (((record.get('final_result') or {}).get('qa') or {}).get('code_qa') or {}).get('status') == 'NOT_APPLICABLE' and e['qa'] == 'PASS')
        file_evidence = [c.get('source', '') for c in evidence if c.get('round') == 'file']
        check(k, 'R05-N3', 'normal', 'claims linked to source file:line evidence (at least two of the three files)', file_evidence,
                bool(evidence) and len({c.split(':')[0] for c in file_evidence}) >= 2)
        check(k, 'R05-N4', 'normal', 'writer and final judge sessions are separate', (product.get('session_id'), ((product.get('result') or {}).get('review') or {}).get('session_id')),
                bool(product) and product.get('session_id') != ((product.get('result') or {}).get('review') or {}).get('session_id'))
        check(k, 'R05-O1', 'negative', 'source tree unchanged (review does not become code repair)', tree_hashes(repo) == once, tree_hashes(repo) == once)
        if product and delivery.get('artifact'):
            path = Path(delivery['artifact'])
            raw = path.read_bytes()
            path.write_bytes(raw.replace(b'"report"', b'"report_x"', 1))
            try:
                check(k, 'R05-O2', 'negative', 'later report/evidence changes invalidate acceptance', self.approved(record), not self.approved(record))
            finally:
                path.write_bytes(raw)
            check(k, 'R05-O3', 'limit', 'restoring the original report makes acceptance valid', self.approved(record), self.approved(record))
        if self.path == 'fake':
            (HOME / 'acceptance-scenario.json').write_text(json.dumps({'scenario': 'R05', 'mode': 'writer-source-writer'}))
            try:
                repo2, base2 = self.repo_setup('acceptance-r05-rejection', 'true', 'Review rejection attempt.', 'Source remains unchanged.')
                rid2 = self.request(self.request_json('acceptance-r05-rejection', repo2, 'goal', prompt, 'Review', 'source remains unchanged', extra='-rejection'))
                self.approve(rid2)
                self.plan(rid2)
                rc2 = self.execute(rid2)
                k2 = self.record(rid2)
                check(k, 'R05-O4', 'negative', 'delivery rejected if the writer changes source (read-only role preserved)', (rc2, k2.get('state'), k2.get('reason')),
                        rc2 != 0 and k2.get('state') != 'completed' and k2.get('reason') == 'plan-review-source-modified')
            finally:
                (HOME / 'acceptance-scenario.json').write_text(json.dumps({'scenario': 'R05', 'mode': ''}))
        bound(k, 'R05-O5', 'negative', 'invented evidence, rejection, reused sessions, critical questions and source writes cannot produce delivery',
              ['test_request_pipeline.RequestPipelineTests.test_review_product_rejects_false_evidence_rejection_reused_session_and_critical_questions',
               'test_request_pipeline.RequestPipelineTests.test_review_product_delivers_source_bound_evidence_without_code_qa_or_source_edits'])
        self.live_gates(e, 'R05')
        return {'evidence': e, 'evidence_count': len(evidence), 'file_evidence': file_evidence}

    def skeleton_setup(self, target: Path, round: str = 'python') -> subprocess.CompletedProcess:
        return self.cli('--create', str(target), '--round', round, input='yes\nyes\n', script='lib/project_registry.py', timeout=300)

    def run_product(self, wt: Path, csv_path: Path) -> tuple[int | None, str, dict | None]:
        """Runs the model's product in the sandbox; (rc, combined output, last JSON line|None). rc None = could not run."""
        import shlex
        rc, output = safe_command(f'PYTHONPATH=src {shlex.quote(PY)} -m spend_summary {shlex.quote(str(csv_path))}', wt, self.scratch('cli'), 60)
        return rc, output, json_line(output)

    def r06_checks(self, on: str, rid: str, rc: int, e: dict, expected: dict) -> None:
        k = self.checks
        wt = Path(e['product'].get('worktree') or '')
        check(k, f'{on}-N1', 'normal', 'run completed, last acceptance PASS, reviewed-worktree', (rc, e['status'], e['final'], e['delivery']),
                rc == 0 and e['status'] == 'completed' and e['final'] == 'PASS' and e['delivery'] == 'reviewed-worktree')
        check(k, f'{on}-N2', 'normal', 'skeleton QA (make test) passes', e['product'].get('rc'), e['product'].get('rc') == 0)
        if wt.is_dir():
            rcv, out, observed = self.run_product(wt, self.fixture / 'valid.csv')
            check(k, f'{on}-N3', 'normal', f"valid.csv -> {expected['valid']} (sandbox)", (rcv, observed if observed is not None else out[-200:]),
                    rcv == 0 and observed == expected['valid'])
            for entry_name in ('invalid', 'missing-column'):
                rci, outi, json_data = self.run_product(wt, self.fixture / f'{entry_name}.csv')
                check(k, f'{on}-O-{entry_name}', 'negative', f'{entry_name}.csv -> nonzero, partial JSON output absent (sandbox)', (rci, outi[-200:]),
                        rci is not None and rci != 0 and json_data is None)
            lock = (wt / 'requirements.lock').read_text() if (wt / 'requirements.lock').is_file() else None
            check(k, f'{on}-N4', 'normal', 'no dependencies: requirements.lock contains only comments', lock,
                    lock is not None and all(s.startswith('#') or not s.strip() for s in lock.splitlines()))
            check(k, f'{on}-N5', 'normal', 'skeleton manifest and install/test/execution record present',
                    ((wt / 'cadre-skeleton.json').is_file(), (HOME / 'projects' / self.record(rid)['project'] / 'project_skeleton.json').is_file()),
                    (wt / 'cadre-skeleton.json').is_file() and (HOME / 'projects' / self.record(rid)['project'] / 'project_skeleton.json').is_file())
            once_files = sorted(tree_hashes(wt))
            once_attempt = len(e['attempts'])
            r = self.cli('run', rid)
            check(k, f'{on}-O1', 'negative', 'restarting does not duplicate files or product and creates no new worker',
                    (r.returncode, len(self.attempts(rid)) - once_attempt, sorted(tree_hashes(wt)) == once_files),
                    r.returncode == 0 and len(self.attempts(rid)) == once_attempt and sorted(tree_hashes(wt)) == once_files)
        else:
            for id in (f'{on}-N3', f'{on}-N4', f'{on}-N5', f'{on}-O1'):
                check(k, id, 'normal', 'worktree product', 'worktree absent', None)
        self.roles_check(on, e, {'plan', 'review'}, True)

    def r06(self) -> dict:
        expected = json.loads((self.fixture / 'expected.json').read_text())
        prompt = (self.fixture / 'request.txt').read_text()
        k = self.checks
        acceptance = 'make test passes; valid.csv groceries 20.00 transport 4.50 total 24.50; invalid amount or column exits nonzero without partial output'
        # (a) target only: an empty new target, the first commit with the skeleton goes through two-step permission
        target = self.operation / 'fresh' / 'spend-summary'
        target.parent.mkdir(parents=True)
        pay_a, _ = r06_shares(self.cap, None)
        rid = self.request(self.request_json('spend-summary', target, 'goal', prompt, 'Working spend_summary CLI', acceptance, round_no='new', cap=pay_a))
        if self.only_proposal:   # feasibility: the proposals of both variants are taken, no approval/call
            try:
                self.approve(rid)
            except ProposalStance:
                pass
            target_b = self.operation / 'backlog' / 'spend-summary'
            target_b.parent.mkdir(parents=True)
            setup_b = self.skeleton_setup(target_b, 'python')
            if setup_b.returncode != 0:
                raise RuntimeError('skeleton setup failed: ' + setup_b.stderr[-300:])
            project_ref_b = setup_b.stdout.split()[-1]
            (target_b / 'BACKLOG.md').write_text(R06_BACKLOG)
            git(target_b, 'add', 'BACKLOG.md')
            git(target_b, 'commit', '-qm', 'ready backlog')
            _, pay_b = r06_shares(self.cap, None)
            rid_b = self.request(self.request_json(project_ref_b, target_b.resolve(), 'backlog', prompt, 'Working spend_summary CLI', acceptance, backlog='BACKLOG.md', extra='-b', cap=pay_b))
            self.approve(rid_b)
            raise ProposalStance(self.proposals[-1])
        self.approve(rid)
        rc0 = self.execute(rid)
        k0 = self.record(rid)
        check(k, 'R06A-P1', 'limit', 'implementation in an empty target awaits first-commit permission (retryable/new-project-init-permission-required)',
                (rc0, k0.get('state'), k0.get('reason'), target.exists()),
                rc0 != 0 and k0.get('reason') == 'new-project-init-permission-required' and not target.exists())
        bad = self.skeleton_setup(target, 'go')
        check(k, 'R06A-O2', 'negative', 'unsupported kind rejected without creating the target', (bad.returncode, target.exists()), bad.returncode != 0 and not target.exists())
        setup = self.skeleton_setup(target, 'python')
        check(k, 'R06A-P2', 'normal', 'Python skeleton recorded as the first commit after two written answers', (setup.returncode, setup.stdout.strip()[-40:]),
                setup.returncode == 0 and (target / 'cadre-skeleton.json').is_file())
        base = git(target, 'rev-parse', 'HEAD') if (target / '.git').is_dir() else ''
        rc = self.execute(rid, proceed=True)
        e = self.shared_evidence(rid, 'make test', base, {})
        self.r06_checks('R06A', rid, rc, e, expected)
        self.live_gates(e, 'R06A')
        # (b) ready backlog: the same request, an existing skeleton repo + BACKLOG.md in a separate copy
        target_b = self.operation / 'backlog' / 'spend-summary'   # same folder name: the package name stays spend_summary, the project name differs
        target_b.parent.mkdir(parents=True)
        setup_b = self.skeleton_setup(target_b, 'python')
        if setup_b.returncode != 0:
            check(k, 'R06B-P1', 'normal', 'skeleton repository for the backlog copy', setup_b.stderr[-300:], False)
            return {'evidence_a': e}
        project_ref_b = setup_b.stdout.split()[-1]   # approval prompts are printed on the same line; the recorded name is the last word
        check(k, 'R06B-P1', 'normal', 'second copy registered under a separate project name', project_ref_b, bool(project_ref_b) and project_ref_b != 'spend-summary')
        (target_b / 'BACKLOG.md').write_text(R06_BACKLOG)
        git(target_b, 'add', 'BACKLOG.md')
        git(target_b, 'commit', '-qm', 'ready backlog')
        base_b = git(target_b, 'rev-parse', 'HEAD')
        a_m = e['receipt_status']
        a_liability = ((a_m['cash_micro'] + a_m['equivalent_micro']) / 1e6) if a_m['readable'] and a_m['line'] else None
        pay_a, pay_b = r06_shares(self.cap, a_liability)
        check(k, 'R06B-P0', 'limit', 'the R06 family allocation is shared between two variants (A+B <= allocation; if A liability is unknown, B = allocation - A cap)',
                {'aile_share': self.cap, 'A': pay_a, 'A_liability': a_liability, 'B': pay_b}, pay_a + pay_b <= self.cap + 1e-9 and pay_b > 0)
        if pay_b <= 0:
            return {'evidence_a': e}
        rid_b = self.request(self.request_json(project_ref_b, target_b.resolve(), 'backlog', prompt, 'Working spend_summary CLI', acceptance, backlog='BACKLOG.md', extra='-b', cap=pay_b))
        self.approve(rid_b)
        self.plan(rid_b)
        rc_b = self.execute(rid_b)
        e_b = self.shared_evidence(rid_b, 'make test', base_b, {'BACKLOG.md': sha(target_b / 'BACKLOG.md')})
        self.r06_checks('R06B', rid_b, rc_b, e_b, expected)
        plan_b = ((self.plan_view(rid_b).get('history') or [{}])[-1].get('plan') or {})
        check(k, 'R06B-N6', 'normal', 'backlog id H001 mapped in the plan and BACKLOG.md preserved',
                ([s.get('id') for s in plan_b.get('source_items') or []], e_b['product'].get('protected_same')),
                [s.get('id') for s in plan_b.get('source_items') or []] == ['H001'] and bool(e_b['product'].get('protected_same')))
        bound(k, 'R06-O3', 'negative', 'no continuation with foreign records, a nonempty target or missing permission; interrupted skeleton setup creates one commit',
              ['test_request_pipeline.RequestPipelineTests.test_new_target_with_stray_files_or_foreign_registration_does_not_continue',
               'test_qa3_fixes.SkeletonTests', 'test_project_registry'])
        self.live_gates(e_b, 'R06B')
        return {'evidence_a': e, 'evidence_b': e_b}

    def r07(self) -> dict:
        import csv
        from decimal import Decimal
        repo, base = self.repo_setup('acceptance-r07', 'true', 'Offline operational report: orders.csv, metadata.json, contradictory-summary.txt.',
                                    'Use-at acceptance. Only REPORT.md and REPORT.evidence.jsonl are written; source remains unchanged.')
        once = tree_hashes(repo)
        meta = json.loads((self.fixture / 'metadata.json').read_text())
        total = {'completed': Decimal(0), 'cancelled': Decimal(0), 'all': Decimal(0)}
        with open(self.fixture / 'orders.csv', newline='') as f:
            for line in csv.DictReader(f):
                total[line['status']] += Decimal(line['amount'])
                total['all'] += Decimal(line['amount'])
        independent = {kk: f'{v:.2f}' for kk, v in total.items()}
        prompt = 'ROUND: report\nREPORT: REPORT.md\nSECTIONS: Source; Account; Comment\n' + (self.fixture / 'request.txt').read_text()
        rid = self.request(self.request_json('acceptance-r07', repo, 'goal', prompt, 'REPORT.md + REPORT.evidence.jsonl',
                                         'totals match the independent calculation; contradiction and source date visible; source remains unchanged'))
        self.approve(rid)
        self.plan(rid)
        rc = self.execute(rid)
        e = self.shared_evidence(rid, '', base, {})
        k = self.checks
        record = self.record(rid)
        wt = Path(record.get('worktree') or '')
        report = (wt / 'REPORT.md').read_text() if (wt / 'REPORT.md').is_file() else ''
        fr = record.get('final_result') or {}
        check(k, 'R07-N1', 'normal', 'run completed, final acceptance PASS, report QA kind report', (rc, e['status'], e['final'], e['qa_round']),
                rc == 0 and e['status'] == 'completed' and e['final'] == 'PASS' and e['qa_round'] == 'report')
        check(k, 'R07-N2', 'normal', 'judge probe recorded', bool((fr.get('review') or {}).get('poll')), bool((fr.get('review') or {}).get('poll')))
        check(k, 'R07-N3', 'normal', f'independent calculation {independent} appears in the report with an evidence id',
                {kk: bool(re.search(re.escape(v) + r'[^\n]*\[K', report)) for kk, v in independent.items()},
                all(re.search(re.escape(v) + r'[^\n]*\[K', report) for v in independent.values()))
        check(k, 'R07-N4', 'normal', 'source date appears in the report (Data date) and matches metadata', meta['source_date'] in report and 'Data date' in report,
                meta['source_date'] in report and 'Data date' in report)
        check(k, 'R07-N5', 'normal', 'analyst mode (single analyst/reconciliation) appears in the report', bool(re.search(r'(?i)analyst', report)), bool(re.search(r'(?i)analyst', report)))
        contradiction = bool(re.search(r'21\.00[^\n]*(unverified|contradict|mismatch)', report, re.I))
        check(k, 'R07-O1', 'negative', 'contradictory summary (21.00) is not resolved with invented data; contradiction stated explicitly', contradiction, contradiction)
        changed = sorted(tree_hashes(wt).keys() - once.keys()) if wt.is_dir() else []
        check(k, 'R07-O2', 'negative', 'source outside the report remains unchanged; only report and appendix (plus analyst note) written',
                (tree_hashes(repo) == once, changed),
                tree_hashes(repo) == once and set(changed) <= {'REPORT.md', 'REPORT.evidence.jsonl', '.cadre-analyst.md', 'ANALYST.md'})
        self.r07_local_negatives(repo, record.get('source_sha') or base)
        self.roles_check('R07', e, {'plan', 'review'}, True)
        bound(k, 'R07-O5', 'negative', 'stale or undated sources, conflicting evidence and missing files fail; budget waits resume with the same draft',
              ['test_report_mode.ReportQaTest', 'test_report_writer_exhaustion', 'test_report_mode.ReportEndToEndTest'])
        self.live_gates(e, 'R07')
        return {'evidence': e, 'independent_account': independent, 'report_length': len(report)}

    def r07_local_negatives(self, repo: Path, source_sha: str) -> None:
        """The same report QA gate without a model call: a contradictory claim and a missing required source must be red."""
        import report_mode
        k = self.checks
        declaration = {'path': 'REPORT.md', 'evidence': 'REPORT.evidence.jsonl', 'sections': ['Source', 'Account', 'Comment']}
        for entry_name, text, evidence, expected_finding in (
                ('contradiction', '# Source\norders.csv read [K1].\n# Account\ncompleted total 21.00 [K2].\n# Comment\nabsent.\n',
                 [{'id': 'K1', 'claim': 'source', 'round': 'file', 'source': 'orders.csv:1', 'expected': 'order_id,date,status,amount'},
                  {'id': 'K2', 'claim': 'total', 'round': 'command', 'expected': '21.00',
                   'source': "python3 -c \"import csv,decimal;print(sum(decimal.Decimal(r['amount']) for r in csv.DictReader(open('orders.csv')) if r['status']=='completed'))\""}],
                 'absent from output'),
                ('missing-source', '# Source\nmissing.csv 3 line [K1].\n# Account\nabsent [K1].\n# Comment\nabsent.\n',
                 [{'id': 'K1', 'claim': 'source', 'round': 'file', 'source': 'missing.csv:1', 'expected': 'x'}], 'file absent')):
            copy = self.operation / f'r07-negative-{entry_name}'
            shutil.copytree(repo, copy)
            (copy / 'REPORT.md').write_text(text)
            (copy / 'REPORT.evidence.jsonl').write_text(''.join(json.dumps(x) + '\n' for x in evidence))
            try:
                outcome = report_mode.report_qa(str(copy), declaration, source_sha, str(self.operation / f'r07-temporary-{entry_name}'), seed='acceptance')
                observed = (outcome['status'], outcome['findings'][:4])
                passed = outcome['status'] == 'FAIL' and any(expected_finding in b for b in outcome['findings'])
            except Exception as exc:  # noqa: BLE001
                observed, passed = f'{type(exc).__name__}: {exc}', None
            check(k, f'R07-O3-{entry_name}', 'negative', f'{entry_name}: report QA fails ({expected_finding})', observed, passed)

    # --- closing ---------------------------------------------------------------------------------------------------
    def close(self) -> list[str]:
        from run_store import TERMINAL
        closing = []
        for rid in self.runs:
            k = self.record(rid)
            if k and k.get('state') not in TERMINAL:
                self.orphans_sum(rid)
                if self.cancel(rid):
                    closing.append(f"{rid} ({k.get('state')})")
        for pidfile in (HOME / 'chain').glob('*.pid'):
            try:
                pid = int(pidfile.read_text().strip())
                command = subprocess.run(['ps', '-o', 'command=', '-p', str(pid)], capture_output=True, text=True).stdout
                if str(HOME) in command or str(self.root) in command:
                    os.kill(pid, signal.SIGTERM)
                    closing.append(f'guard {pidfile.stem} pid {pid}')
            except (OSError, ValueError):
                continue
        if closing:
            self.event(f'own runs/guards closed: {closing}')
        return closing

    def runner(self) -> str:
        try:
            return git(ENGINE.parent, 'rev-parse', '--short', 'HEAD')
        except (subprocess.CalledProcessError, OSError):
            return '-'

    def write(self, outcome: dict) -> dict:
        normal = [c for c in self.checks if c['round'] == 'normal']
        negative = [c for c in self.checks if c['round'] != 'normal']
        failed = [c['id'] for c in self.checks if c['outcome'] in ('FAIL', 'BLOCKED')]
        decision = outcome.get('decision') or ('PASS' if not failed and normal and negative else 'FAIL')
        if self.path == 'fake' and decision == 'PASS':
            decision = 'LOCAL_PASS'
        if decision == 'PROPOSAL':
            outcome = {**outcome, 'proposal_models': (outcome.get('proposal') or {}).get('action', {}).get('models'),
                     'proposal_duration_min': (outcome.get('proposal') or {}).get('action', {}).get('duration_minutes'),
                     'proposal_total_usd': (outcome.get('proposal') or {}).get('action', {}).get('budget_usd')}
        summary = {'id': self.ident, 'stamp': self.stamp, 'path': self.path, 'live': self.path != 'fake', 'runner': self.runner(),
                'runner_root': str(self.root), 'home_dir': str(HOME), 'minute': self.minute, 'cap_usd': self.cap, 'fixture': str(self.fixture),
                'fixture_verification': self.fixture_hash, 'decision': decision, 'check_count': {'normal': len(normal), 'negative_limit': len(negative),
                'failed': failed}, 'checks': self.checks, 'runs': self.runs, 'events': self.events,
                'go_once': getattr(self, 'go_once', None), 'go_then': self.go_status() if self.path != 'fake' else None,
                'chain_environment': self.chain, 'package_share': {'file': str(self.pay.file), 'cap_usd': self.pay.data['package_cap_usd'],
                                                              'allocation_usd': self.pay.allocation_done(), 'remaining_usd': self.pay.remaining(),
                                                              'brut_usd': self.pay.brut(), 'refund_usd': self.pay.refund_done(),
                                                              'liability_refund': self.chargeback},
                'fake_calls': [c['role'] for c in self.calls()] if self.path == 'fake' else None, **{kk: v for kk, v in outcome.items() if kk != 'decision'}}
        (self.directory / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        with open(self.directory.parent / 'acceptance-package.jsonl', 'a') as f:
            f.write(json.dumps({kk: summary.get(kk) for kk in ('id', 'stamp', 'path', 'runner', 'decision', 'check_count', 'runs')},
                               ensure_ascii=False, default=str) + '\n')
        print(json.dumps({kk: summary.get(kk) for kk in ('id', 'path', 'decision', 'check_count', 'runs')}, ensure_ascii=False, default=str))
        print(f'SUMMARY: {self.directory / "summary.json"}')
        return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('ident', choices=IDS)
    ap.add_argument('--fixture-dir', required=True, type=Path, help='campaign fixtures/ directory (R01..R07 subdirectories)')
    ap.add_argument('--path', choices=('fake', 'go', 'or'), default='fake')
    ap.add_argument('--minute', type=float, default=20.0, help='shared scenario time limit (also used as the request duration)')
    ap.add_argument('--cap', type=float, default=0.0, help='fixed operator total for the typed request (USD); irrelevant in fake mode')
    ap.add_argument('--output', type=Path, default=None, help='evidence directory (default CADRE_HOME/reports/test-archive/<R>-<path>-<stamp>)')
    ap.add_argument('--mode', default='', help='negative fake mode (acceptance_fake.py)')
    ap.add_argument('--no-hash-check', action='store_true', help='skip fixture-manifest.json verification (development only)')
    ap.add_argument('--package-cap', type=float, default=2.0, help='shared cap for the whole package (USD); ledger <output>/../acceptance-package-pay.json')
    ap.add_argument('--only-proposal', action='store_true', help='request and engine permission proposal; no approval or calls (feasibility, model/duration/cap)')
    ap.add_argument('--liability-refund', action='store_true',
                    help='refund the unused allocation of a terminal request with no remaining processes and verified receipts to the shared package budget exactly once')
    a = ap.parse_args()
    if a.path != 'fake' and a.mode:
        ap.error('--mode is only available in fake mode')
    cutting_setup()
    p = Package(a.ident, a.fixture_dir.resolve(), a.minute, a.cap, a.path, a.output, a.mode, hash_check=not a.no_hash_check,
              package_cap=a.package_cap, only_proposal=a.only_proposal, liability_refund=a.liability_refund)
    p.event(f'{a.ident} started: path={a.path} minute={a.minute} cap={a.cap} package_cap={a.package_cap} runner={p.root} home_dir={HOME} '
           f'chain={sorted(p.chain)}')
    try:
        outcome = getattr(p, a.ident.lower())()
    except ProposalStance as stance:
        outcome = {'decision': 'PROPOSAL', 'proposal': stance.proposal, 'proposals': p.proposals, 'reason': 'proposal only: no approval or call'}
        p.event('proposal obtained; no approval or calls')
    except KeyboardInterrupt as cutting:
        cuts_postpone()
        outcome = {'decision': 'STOPPED', 'reason': f"operator interruption ({cutting.args[0] if cutting.args else 'SIGINT'}): runs closed through the engine cancel path"}
        p.event(f"CUTTING: {outcome['reason']}")
    except Exception as exc:  # noqa: BLE001 - a driver error is recorded too and produces no PASS
        outcome = {'decision': 'ERROR', 'reason': f'{type(exc).__name__}: {exc}'}
        p.event(f'ERROR: {outcome["reason"]}')
    outcome['closing'] = p.close()
    if p.chargeback:
        outcome['package_refund'] = p.refund_audit()
    summary = p.write(outcome)
    return 0 if summary['decision'] in ('PASS', 'LOCAL_PASS', 'PROPOSAL') else 130 if summary['decision'] == 'STOPPED' else 1


if __name__ == '__main__':
    raise SystemExit(main())
