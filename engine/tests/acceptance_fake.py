#!/usr/bin/env python3
"""Scenario-aware fake `opencode` for the acceptance package (R01-R07) dry run: no network, the real engine/gates/QA/judge path runs.

Used only with `acceptance_package.py --route fake`, reading `CADRE_HOME/acceptance-scenario.json` ({"scenario": "R03", "mode": ""}).
Fake output is not a live provider acceptance; it proves the driver and the gates work locally. Negative modes (the mode field)
trigger the existing rejection paths with the same driver: "writer-source-writer" (the review writer writes to the source), "judge-rejection" (the final judge says REVISE).
"""
from __future__ import annotations

import csv
import decimal
import json
import os
import pathlib
import re
import sys
import uuid

HOME = pathlib.Path(os.environ['CADRE_HOME'])
ARGS = sys.argv[1:]
PROMPT = ARGS[-1] if ARGS else ''
TASK = os.environ.get('CADRE_TASK_ID', '')
if PROMPT.startswith('Continue where you left off') and os.environ.get('CADRE_RUN_DIR') and TASK:
    # the real CLI keeps the task in context in a recorded session; the fake re-reads the task's recorded prompt (same as test_end_to_end)
    try:
        PROMPT = (pathlib.Path(os.environ['CADRE_RUN_DIR']) / 'tasks' / TASK / 'prompt.md').read_text()
    except OSError:
        pass
try:
    SETTING = json.loads((HOME / 'acceptance-scenario.json').read_text())
except (OSError, ValueError):
    SETTING = {}
SCENARIO, MODE = SETTING.get('scenario', ''), SETTING.get('mode', '')
SID = ARGS[ARGS.index('--session') + 1] if '--session' in ARGS else 'ses_' + uuid.uuid4().hex[:12]

_REQ = os.environ.get('CADRE_REQUEST_DIR')
if _REQ:
    # In the real CLI cadre-plugin.mjs writes the tools-armed.json receipt while arming the tools; the process check (COMPLETE) requires
    # this receipt. The fake CLI leaves the same receipt in the same form as test_end_to_end.FAKE_OPENCODE (0400, single write).
    _IS = pathlib.Path(_REQ).parent
    _POL, _MARKER = _IS / 'tool-policy.json', _IS / 'tools-armed.json'
    if _POL.exists() and not _MARKER.exists():
        try:
            import hashlib
            _P = json.loads(_POL.read_text())
            _FD = os.open(_MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
            os.write(_FD, json.dumps({'owner': _P.get('owner'), 'pid': os.getpid(),
                                      'profile_sha256': hashlib.sha256(str(_P.get('profile', '')).encode()).hexdigest()}).encode())
            os.close(_FD)
        except (OSError, ValueError):
            pass


def record(role: str, extra: dict | None = None) -> None:
    with (HOME / 'acceptance-calls.jsonl').open('a') as f:
        f.write(json.dumps({'scenario': SCENARIO, 'role': role, 'task': TASK, 'cwd': os.getcwd(), 'session': SID, **(extra or {})}) + '\n')


def event(text: str) -> None:
    print(json.dumps({'type': 'text', 'sessionID': SID, 'part': {'type': 'text', 'text': text}}), flush=True)


def question() -> dict:
    """The prompt may have several JSON lines (question + context); the line carrying kind/schema/criteria is the question."""
    selected = None
    for line in PROMPT.splitlines():
        if not line.startswith('{'):
            continue
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict) and ('kind' in candidate or 'schema' in candidate or 'criteria' in candidate):
            selected = candidate
        elif selected is None:
            selected = candidate
    if selected is None:
        raise SystemExit('acceptance_fake: no JSON question in the prompt')
    return selected


def task(gid: str, definition: str, files: list[str], acceptance: str, dependent: list[str] | None = None, group: int = 0) -> dict:
    return {'id': gid, 'definition': definition, 'files': files, 'acceptance': acceptance, 'dependent': dependent or [], 'difficulty': 2,
            'model': 'auto', 'parallel_group': group, 'estimate_min': 2, 'estimate_usd': 0}


def source_mapping(items: list[dict], complete: dict[str, list[str]], verified: set[str]) -> list[dict]:
    outcome = []
    for item in items:
        if item['id'] in verified:
            outcome.append({'id': item['id'], 'source_sha256': item['sha256'], 'tasks': [], 'status': 'verified',
                          'evidence': f"{item['id']} is checked [x] in the source; implemented in text.py, tests/test_{item['id'].lower()}.py passes"})
        else:
            outcome.append({'id': item['id'], 'source_sha256': item['sha256'], 'tasks': complete.get(item['id'], ['g1']),
                          'status': 'pending', 'evidence': f"{item['id']} is pending [ ] in the source; acceptance command fails"})
    return outcome


def plan(q: dict) -> dict:
    p = dict(q['schema'])
    p['job_kind'] = (q.get('request') or {}).get('job_type', 'code')
    p['assumptions'], p['decisions'] = [], []
    items = (q.get('backlog') or {}).get('items') or []
    ids = {i['id'] for i in items}
    if SCENARIO == 'R01':
        p['tasks'] = [task('g1', 'Make calc.py add() return the sum', ['calc.py'], 'python3 -m unittest -v test_calc passes')]
        p['source_items'] = []
    elif SCENARIO in ('R02', 'R03', 'R04'):
        complete = {'B001': ['g1'], 'B002': ['g1'], 'B003': ['g1'], 'B004': ['g1'], 'B005': ['g2']}
        verified = {i['id'] for i in items if i.get('completed')}
        remaining = [b for b in ('B001', 'B002', 'B003', 'B004') if b not in verified]
        tasks = []
        if remaining:
            tasks.append(task('g1', 'text.py: ' + ', '.join(remaining) + ' (B003 depends on B001, B004 on B001/B003)', ['text.py'],
                                  ' && '.join(f'python3 -m unittest -v tests.test_{b.lower()}' for b in remaining)))
        tasks.append(task('g2', 'text_cli.py: B005 (depends on B004)', ['text_cli.py'], 'python3 -m unittest -v tests.test_b005',
                              dependent=['g1'] if remaining else [], group=1 if remaining else 0))
        p['tasks'] = tasks
        p['source_items'] = source_mapping(items, complete, verified) if ids else []
        if not ids:
            p['tasks'][-1]['definition'] += '; source ids B001-B005 in BACKLOG.md'
    elif SCENARIO == 'R05':
        p['tasks'] = [task('g1', 'Review worker-setting propagation and counter semantics in three files read-only',
                               ['engine/tests/regression.py', 'engine/tests/parallel_run.py', 'engine/USAGE.md'], 'report with file:line evidence')]
        p['source_items'] = []
    elif SCENARIO == 'R06':
        p['tasks'] = [task('g1', 'spend_summary CLI: category,amount CSV -> JSON with two decimal places (Decimal)',
                               ['src/spend_summary/summary.py', 'src/spend_summary/__main__.py', 'tests/test_spend_summary.py'],
                               'make test passes; valid.csv groceries 20.00 transport 4.50 total 24.50; invalid input exits nonzero')]
        p['source_items'] = [{'id': i['id'], 'source_sha256': i['sha256'], 'tasks': ['g1'], 'status': 'pending',
                              'evidence': 'BACKLOG item is open; no CLI under src'} for i in items]
    elif SCENARIO == 'R07':
        p['tasks'] = [task('g1', 'orders.csv operational report: separate sources, calculations and comments, with evidence for each claim',
                               ['REPORT.md', 'REPORT.evidence.jsonl'], 'report QA and judge probe pass')]
        p['source_items'] = []
    else:
        raise SystemExit('acceptance_fake: unknown scenario ' + repr(SCENARIO))
    if q.get('change'):
        p['tasks'][0]['definition'] = q['change']
    return p


TEXT_COMPLETE = '''def words(text):
    return text.split()

def word_count(text):
    return len(words(text))

def reverse_convert(text):
    return " ".join(reversed(words(text)))

def longest_word(text):
    words_ = words(text)
    if not words_:
        return None
    return max(words_, key=len)

def summary(text):
    return {"word": word_count(text), "longest": longest_word(text)}
'''
TEXT_CLI = '''import json
import sys

from text import summary

if __name__ == "__main__":
    print(json.dumps(summary(sys.argv[1]), ensure_ascii=False))
'''
SUMMARY_PY = '''"""spend_summary: category and grand totals from category,amount CSV (Decimal, two decimal places)."""
import csv
import json
from decimal import Decimal, InvalidOperation


class InputError(ValueError):
    pass


def summarize(path):
    totals = {}
    general = Decimal("0")
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or "category" not in reader.fieldnames or "amount" not in reader.fieldnames:
            raise InputError("expected columns: category,amount")
        for no, line in enumerate(reader, 2):
            category = (line.get("category") or "").strip()
            raw = (line.get("amount") or "").strip()
            if not category:
                raise InputError(f"line {no}: category is empty")
            try:
                amount = Decimal(raw)
            except InvalidOperation:
                raise InputError(f"line {no}: invalid amount {raw!r}") from None
            if not amount.is_finite():
                raise InputError(f"line {no}: invalid amount {raw!r}")
            totals[category] = totals.get(category, Decimal("0")) + amount
            general += amount
    outcome = {k: format(v.quantize(Decimal("0.01")), "f") for k, v in sorted(totals.items())}
    outcome["total"] = format(general.quantize(Decimal("0.01")), "f")
    return outcome


def summary_json(path):
    return json.dumps(summarize(path), ensure_ascii=False)
'''
MAIN_PY = '''import sys

from spend_summary.summary import InputError, summary_json


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python3 -m spend_summary <CSV>", file=sys.stderr)
        return 2
    try:
        output = summary_json(argv[0])
    except (InputError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''
TEST_SUMMARY = '''import json
import os
import subprocess
import sys
import tempfile
import unittest

from spend_summary.summary import InputError, summarize


class SpendSummaryTests(unittest.TestCase):
    def file(self, content):
        f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
        f.write(content)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_valid_input(self):
        path = self.file("category,amount\\nmarket,12.30\\ntransport,4.50\\nmarket,7.70\\n")
        self.assertEqual(summarize(path), {"market": "20.00", "transport": "4.50", "total": "24.50"})

    def test_invalid_amount_and_missing_column(self):
        for content in ("category,amount\\nmarket,wrong\\n", "category\\nmarket\\n"):
            with self.assertRaises(InputError):
                summarize(self.file(content))

    def test_cli_nonzero_and_no_partial_output(self):
        path = self.file("category,amount\\nmarket,wrong\\n")
        env = {**os.environ, "PYTHONPATH": "src"}
        r = subprocess.run([sys.executable, "-m", "spend_summary", path], capture_output=True, text=True, env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        good = self.file("category,amount\\nmarket,1.005\\n")
        r = subprocess.run([sys.executable, "-m", "spend_summary", good], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout), {"market": "1.00", "total": "1.00"})
'''


def line_no(path: str, part: str) -> tuple[int, str]:
    for no, line in enumerate(pathlib.Path(path).read_text().splitlines(), 1):
        if part in line:
            return no, line.strip()
    raise SystemExit(f'acceptance_fake: {path} does not contain {part!r}')


def report_product() -> None:
    lines = pathlib.Path('orders.csv').read_text().splitlines()
    total = {'completed': decimal.Decimal(0), 'cancelled': decimal.Decimal(0), 'all': decimal.Decimal(0)}
    with open('orders.csv', newline='') as f:
        for r in csv.DictReader(f):
            total[r['status']] += decimal.Decimal(r['amount'])
            total['all'] += decimal.Decimal(r['amount'])
    command = ("python3 -c \"import csv,decimal;print(sum(decimal.Decimal(r['amount']) for r in csv.DictReader(open('orders.csv'))"
             " if {condition}))\"")
    source_no, source_line = line_no('metadata.json', 'source_date')
    contradiction_line = pathlib.Path('contradictory-summary.txt').read_text().splitlines()[0].strip()
    evidence = [
        {'id': 'K1', 'claim': 'o1 completed 12.30', 'round': 'file', 'source': 'orders.csv:2', 'expected': lines[1]},
        {'id': 'K2', 'claim': 'o2 cancelled 4.50', 'round': 'file', 'source': 'orders.csv:3', 'expected': lines[2]},
        {'id': 'K3', 'claim': 'o3 completed 7.70', 'round': 'file', 'source': 'orders.csv:4', 'expected': lines[3]},
        {'id': 'K4', 'claim': 'completed total', 'round': 'command', 'source': command.format(condition="r['status']=='completed'"), 'expected': '20.00'},
        {'id': 'K5', 'claim': 'cancelled total', 'round': 'command', 'source': command.format(condition="r['status']=='cancelled'"), 'expected': '4.50'},
        {'id': 'K6', 'claim': 'total of all rows', 'round': 'command', 'source': command.format(condition='True'), 'expected': '24.50'},
        {'id': 'K7', 'claim': 'source date', 'round': 'file', 'source': f'metadata.json:{source_no}', 'expected': source_line},
        {'id': 'K8', 'claim': 'contradictory summary claim', 'round': 'file', 'source': 'contradictory-summary.txt:1', 'expected': contradiction_line},
    ]
    report = (
        '# Source\n'
        f'orders.csv contains {len(lines) - 1} order rows [K1..K3]; source date is recorded as 2026-10-14 in metadata.json [K7].\n\n'
        '# Account\n'
        f"completed total {total['completed']:.2f} [K4]; cancelled total {total['cancelled']:.2f} [K5]; "
        f"total of all rows {total['all']:.2f} [K6].\n\n"
        '# Comment\n'
        'contradictory-summary.txt claims a completed total of 21.00 [K8]; this claim contradicts the source rows and is unverified; '
        'acceptance is withheld until the contradiction is resolved. Calculations use only orders.csv rows [K1..K6].\n\n'
        'Data date: 2026-10-14\n')
    pathlib.Path('REPORT.md').write_text(report)
    pathlib.Path('REPORT.evidence.jsonl').write_text(''.join(json.dumps(k, ensure_ascii=False) + '\n' for k in evidence))


def write(path: str, content: str) -> None:
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(path).write_text(content)


def worker() -> None:
    files = re.search(r'FILES \(touch ONLY these\): (.*)', PROMPT).group(1).split(', ')
    record('worker', {'files': files})
    if SCENARIO == 'R01':
        pathlib.Path('calc.py').write_text('def add(left, right):\n    return left + right\n')
    elif SCENARIO in ('R02', 'R03'):
        if 'text.py' in files:
            pathlib.Path('text.py').write_text(TEXT_COMPLETE)
        if 'text_cli.py' in files:
            pathlib.Path('text_cli.py').write_text(TEXT_CLI)
    elif SCENARIO == 'R06':
        write('src/spend_summary/summary.py', SUMMARY_PY)
        write('src/spend_summary/__main__.py', MAIN_PY)
        write('tests/test_spend_summary.py', TEST_SUMMARY)
    elif SCENARIO == 'R07':
        report_product()
    else:
        raise SystemExit('acceptance_fake: no worker is expected in this scenario')
    event('written\nWHAT CHANGED: canned product\nHOW VERIFIED: acceptance command\nRISK+REASON: fake worker')


def review_writer(q: dict) -> dict:
    no1, s1 = line_no('engine/tests/regression.py', "if not k.startswith(('CADRE_', 'OPENCODE_'")
    no2, s2 = line_no('engine/tests/regression.py', "CADRE_TEST_PYTEST_PYTHON'] = os.environ")
    no3, s3 = line_no('engine/tests/parallel_run.py', 'CADRE_TEST_WORKER')
    no4, s4 = line_no('engine/USAGE.md', 'CADRE_TEST_ARCHIVE')
    report = ('# Findings\n'
             f'regression.py removes every CADRE_ variable from the subprocess environment [K1]; the restored allow-list contains only '
             f'CADRE_TEST_PYTEST_PYTHON, with CADRE_TEST_WORKER absent [K2]. parallel_run.py reads worker count from CADRE_TEST_WORKER [K3]; '
             'therefore the shared gate’s worker setting never reaches the subprocess, which falls back to the CPU count (defect). '
             'USAGE.md documents only CADRE_TEST_PYTEST_PYTHON and CADRE_TEST_ARCHIVE, omitting the worker setting [K4] (documentation gap).\n'
             '# Uncertainties\n'
             'This review cannot establish that timing failures stem solely from this defect; load effects remain possible and unverified.\n'
             '# Method\n'
             'Three files were read read-only and lines bound to file evidence; no tests were run and source remained unchanged [K1..K4].\n')
    if MODE == 'writer-source-writer':
        pathlib.Path('forbidden.txt').write_text('review wrote to the source')
    return {'report': report, 'assumptions': [], 'decisions': [],
            'evidence': [{'id': 'K1', 'claim': 'CADRE_ environment filter', 'round': 'file', 'source': f'engine/tests/regression.py:{no1}', 'expected': s1},
                         {'id': 'K2', 'claim': 'allow-list contains only the pytest Python', 'round': 'file', 'source': f'engine/tests/regression.py:{no2}', 'expected': s2},
                         {'id': 'K3', 'claim': 'worker count comes from the environment', 'round': 'file', 'source': f'engine/tests/parallel_run.py:{no3}', 'expected': s3},
                         {'id': 'K4', 'claim': 'documentation omits the worker setting', 'round': 'file', 'source': f'engine/USAGE.md:{no4}', 'expected': s4}]}


def judge(q: dict, evidence_ids: list[str] | None = None) -> dict:
    answer = {'verdict': 'PASS', 'revision': q['revision'], 'decisions': [],
             'acceptance': [{'id': c['id'], 'status': 'PASS', 'evidence': 'original request, plan, product and source read in a separate session'}
                            for c in q['criteria']]}
    r = q.get('report') or {}
    ids = evidence_ids if evidence_ids is not None else (r.get('evidence_ids') or [])
    at_least = r.get('at_least') or min(5, len(ids))
    if ids:
        answer['poll'] = [{'id': k, 'outcome': 'verified', 'evidence': k + ' read separately from its source'} for k in ids[:max(at_least, min(5, len(ids)))]]
    if MODE == 'judge-rejection':
        answer['verdict'] = 'REVISE'
        answer['acceptance'][0]['status'] = 'FAIL'
    return answer


def main() -> int:
    print(json.dumps({'type': 'step_start', 'sessionID': SID, 'part': {'type': 'step-start'}}), flush=True)
    if PROMPT.startswith('Read the source files'):
        q = question()
        record('plan')
        event(json.dumps(plan(q), ensure_ascii=False))
    elif PROMPT.startswith('REVIEW PLAN ALIGNMENT WITH THE REQUEST'):
        q = question()
        record('plan-review')
        event(json.dumps(judge(q), ensure_ascii=False))
    elif PROMPT.startswith('REVIEW ONLY'):
        q = question()
        record('review-author')
        event(json.dumps(review_writer(q), ensure_ascii=False))
    elif PROMPT.startswith('SEPARATE CLEAN READ-ONLY END REVIEW'):
        q = question()
        record('review-final')
        event(json.dumps(judge(q, [e['id'] for e in q.get('evidence') or []]), ensure_ascii=False))
    elif '[QE-TASK v1]' in PROMPT:
        worker()
    elif 'QA GATE FAILED' in PROMPT:
        record('repair')
        event('correction attempted (fake worker makes no changes)')
    elif 'QUALITY AND ACCEPTANCE CHECK' in PROMPT:
        q = question()
        record('review')
        event(json.dumps(judge(q), ensure_ascii=False))
    else:
        record('unknown', {'prompt_start': PROMPT[:80]})
        event('unknown role')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
