#!/usr/bin/env python3
"""Live scenario driver (scenarios C01-C07): a throwaway repo, Go only, the real engine and gates.

Usage: python3 engine/tests/live_scenario.py C01 [--minute 20] [--cap 10]
The summary is written to `reports/test-archive/<CID>-<stamp>/summary.json` and `test-archive/scenarios.jsonl`.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

HOME = Path(os.environ.get('CADRE_HOME') or Path.home() / '.local/share/cadre-engineering')
ROOT = Path(os.environ.get('CADRE_ENGINE_ROOT') or HOME)
sys.path.insert(0, str(ROOT / 'lib'))
ARCHIVE = HOME / 'reports/test-archive'
from run_store import TERMINAL  # noqa: E402
OPENCODE = '/opt/homebrew/bin/opencode'
PY = sys.executable

PROVIDER = {'go': 'opencode-go', 'or': 'openrouter'}
OR_DS, OR_GLM, OR_QWEN, OR_SOL = ('openrouter/deepseek/deepseek-v4-pro', 'openrouter/z-ai/glm-5.3', 'openrouter/qwen/qwen3.8-max-0902',
                                  'openrouter/openai/gpt-6.1-sol')
WRAPPER = """#!@PY@
import json, os, sys
PROVIDER_ID = @PROVIDER_ID@
args = sys.argv[1:]
try:
    model = args[args.index('-m') + 1]
except (ValueError, IndexError):
    model = ''
if model and not model.startswith(PROVIDER_ID + '/'):
    print('LIVE_SCENARIO: unpermitted route rejected (' + PROVIDER_ID + ' excluded): ' + model, file=sys.stderr)
    raise SystemExit(75)
env = dict(os.environ)
try:
    config = json.loads(env['OPENCODE_CONFIG_CONTENT'])
except (KeyError, ValueError):
    print('LIVE_SCENARIO: protected config missing', file=sys.stderr)
    raise SystemExit(75)
if not any(str(p).endswith('/cadre-plugin.mjs') for p in config.get('plugin') or []):
    print('LIVE_SCENARIO: budget plugin missing', file=sys.stderr)
    raise SystemExit(75)
config['enabled_providers'] = [PROVIDER_ID]
config['disabled_providers'] = sorted((set(config.get('disabled_providers') or []) | {'opencode', 'opencode-go', 'openrouter', 'groq', 'anthropic'}) - {PROVIDER_ID})
config['provider'] = {k: v for k, v in (config.get('provider') or {}).items() if k == PROVIDER_ID}
if model:
    config['model'] = config['small_model'] = model
for row in (config.get('agent') or {}).values():
    if isinstance(row, dict) and row.get('model') and not str(row['model']).startswith(PROVIDER_ID + '/'):
        row['disable'] = True
env['OPENCODE_CONFIG_CONTENT'] = json.dumps(config)
os.execvpe(@OPENCODE@, [@OPENCODE@, *args], env)
"""


def wrapper_text(path: str, opencode: str = OPENCODE) -> str:
    return WRAPPER.replace('@PY@', PY).replace('@PROVIDER_ID@', repr(PROVIDER[path])).replace('@OPENCODE@', repr(opencode))


def chain_environment(path: str, chain: str) -> dict[str, str]:
    if path != 'or':
        return {}
    code = ','.join((OR_DS, OR_GLM))
    plan, judge = (OR_SOL, f'{OR_SOL},{OR_GLM},{OR_QWEN}') if chain == 'production' else (OR_DS, f'{OR_GLM},{OR_QWEN}')
    return {**{f'CADRE_CHAIN_{role}': code for role in ('IMPL', 'IMPL_LIGHT', 'MERGE', 'RECOVERY')},
            'CADRE_CHAIN_PLAN': plan, 'CADRE_CHAIN_QUALITY': judge, 'CADRE_CHAIN_REVIEW': judge}

CALC = 'def add(left, right):\n    return left - right\n'
TEST_CALC = '''import unittest

from calc import add


class AddTests(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(add(2, 3), 5)

    def test_negative(self):
        self.assertEqual(add(-2, -3), -5)

    def test_zero(self):
        self.assertEqual(add(0, 7), 7)


if __name__ == '__main__':
    unittest.main()
'''
PROMPT_CALC = ('Fix add(left, right) in calc.py to return the sum of two integers.\n'
              'Change only calc.py. Preserve existing test_calc.py tests; do not add files, dependencies or features.\n'
              'Acceptance: The three unittest tests in test_calc.py must pass.\nQA command: python3 -m unittest -v test_calc.\n'
              'This is a temporary test project. Do not push, merge, open a PR, install or download anything.\n')

TWO_FILE = {
    'frame.py': 'def frame(x):\n    raise NotImplementedError\n',
    'cube.py': 'def cube(x):\n    raise NotImplementedError\n',
    'test_frame.py': 'import unittest\n\nfrom frame import frame\n\n\nclass T(unittest.TestCase):\n    def test_frame(self):\n'
                    '        self.assertEqual([frame(x) for x in (-2, 0, 3)], [4, 0, 9])\n',
    'test_cube.py': 'import unittest\n\nfrom cube import cube\n\n\nclass T(unittest.TestCase):\n    def test_cube(self):\n'
                   '        self.assertEqual([cube(x) for x in (-2, 0, 3)], [-8, 0, 27])\n',
}
PROMPT_TWO = ('Two independent fixes: frame(x) in frame.py must return x*x; cube(x) in cube.py must return x*x*x. '
             'Each function has its own file; test_frame.py and test_cube.py already exist and must remain unchanged.\n'
             'Acceptance: python3 -m unittest -v test_frame test_cube must pass. Do not push, merge or install.\n')

BACKLOG_TESTS = {
    'B001': ('text.words(text) splits text into a word list; text.word_count(text) counts the words', [],
             "from text import word_count, words\n\n\nclass T(unittest.TestCase):\n    def test_b001(self):\n"
             "        self.assertEqual(words(' a  bb c '), ['a', 'bb', 'c'])\n        self.assertEqual(word_count('a bb c'), 3)\n"),
    'B002': ('text.reverse_convert(text) reverses word order and returns text with single spaces', [],
             "from text import reverse_convert\n\n\nclass T(unittest.TestCase):\n    def test_b002(self):\n"
             "        self.assertEqual(reverse_convert('one two  three'), 'three two one')\n"),
    'B003': ('text.longest_word(text) returns the longest word, first in a tie, using words()', ['B001'],
             "from text import longest_word\n\n\nclass T(unittest.TestCase):\n    def test_b003(self):\n"
             "        self.assertEqual(longest_word('ab abc xyz a'), 'abc')\n        self.assertIsNone(longest_word('  '))\n"),
    'B004': ("text.summary(text) returns a dictionary with word count and longest word", ['B001', 'B003'],
             "from text import summary\n\n\nclass T(unittest.TestCase):\n    def test_b004(self):\n"
             "        self.assertEqual(summary('ab abc'), {'word': 2, 'longest': 'abc'})\n"),
    'B005': ('python3 text_cli.py "text" prints summary(text) as JSON', ['B004'],
             "import json\nimport subprocess\nimport sys\n\n\nclass T(unittest.TestCase):\n    def test_b005(self):\n"
             "        output = subprocess.run([sys.executable, 'text_cli.py', 'ab abc'], capture_output=True, text=True, check=True).stdout\n"
             "        self.assertEqual(json.loads(output), {'word': 2, 'longest': 'abc'})\n"),
}
IMPOSSIBLE = ('B009', ('Product-independent contradictory acceptance: 1 + 1 == 3 cannot pass without changing the test', [],
                     "\n\nclass T(unittest.TestCase):\n    def test_b009(self):\n        self.assertEqual(1 + 1, 3)\n"))


def git(repo: Path, *args: str) -> str:
    return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=cadre-test', '-c', 'user.email=test@cadre.invalid', *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def summary_hash(repo: Path, files: list[str]) -> dict[str, str]:
    return {d: hashlib.sha256((repo / d).read_bytes()).hexdigest() for d in files if (repo / d).is_file()}


class Scenario:
    def __init__(self, cid: str, minute: float, cap: float, path: str = 'go', chain: str = 'cheap') -> None:
        if not math.isfinite(minute * 60) or minute <= 0 or not math.isfinite(cap * 1e6) or cap < 0:
            raise ValueError('Duration must be positive and finite, and the total cap finite and nonnegative')
        self.cid, self.minute, self.cap, self.path, self.chain = cid, minute, cap, path, chain
        self.total_cap = cap
        self.last_monotonic = time.monotonic() + minute * 60
        self.stamp = time.strftime('%Y%m%d-%H%M%S')
        self.directory = ARCHIVE / (f'{cid}-{self.stamp}' if path == 'go' else f'{cid}-or-{chain}-{self.stamp}')
        self.directory.mkdir(parents=True, exist_ok=True)
        self.project_ref = f'live-{cid.lower()}-{self.stamp}'
        self.repo = HOME / 'scratch/live' / f'{cid}-{self.stamp}' / 'repo'
        self.events: list[str] = []
        self.wrapper = self.directory / f'{path}-only-opencode.py'
        self.wrapper.write_text(wrapper_text(path))
        self.wrapper.chmod(0o700)
        self.go_once = self.go_status()

    def event(self, text: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {text}"
        self.events.append(line)
        print(line, flush=True)

    def go_status(self) -> dict:
        try:
            v = json.loads((HOME / 'chain/go-window.json').read_text())
            return {k: v.get(k) for k in ('ts', 'rolling', 'weekly', 'monthly')}
        except (OSError, ValueError):
            return {}

    def repo_setup(self, files: dict[str, str], qa: str, project_ref_md: str, policy: str) -> str:
        self.repo.mkdir(parents=True)
        for entry_name, content in files.items():
            (self.repo / entry_name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / entry_name).write_text(content)
        git(self.repo, 'init', '-q', '-b', 'main')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'scenario baseline')
        pd = HOME / 'projects' / self.project_ref
        pd.mkdir(parents=True)
        for entry_name, content in (('repo-path', str(self.repo)), ('base-branch', 'main'), ('qa-command', qa),
                           ('PROJECT.md', project_ref_md), ('POLICY.md', policy)):
            (pd / entry_name).write_text(content + '\n')
        return git(self.repo, 'rev-parse', 'HEAD')

    def environment(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in ('CADRE_RUN_ID', 'CADRE_RUN_DIR', 'CADRE_OWNER_FDS', 'CADRE_COORDINATOR_PID')}
        env.update(CADRE_HOME=str(HOME), CADRE_ENGINE_ROOT=str(ROOT), CADRE_REAL_OPENCODE=str(self.wrapper), CADRE_CLAUDE_PLAN='0',
                   CADRE_RUN_CAP_USD=str(self.cap), **chain_environment(self.path, self.chain))
        return env

    def duration_remained(self) -> float:
        return max(0.0, self.last_monotonic - time.monotonic())

    def acceptance(self, prompt: str) -> str:
        if not self.duration_remained():
            raise TimeoutError('Scenario duration expired; no new run accepted')
        r = subprocess.run([PY, str(ROOT / 'lib/run_store.py'), 'accept', self.project_ref, '-'], input=prompt, capture_output=True, text=True,
                           env=self.environment(), check=True, timeout=self.duration_remained())
        rid = r.stdout.strip().splitlines()[-1]
        self.event(f'acceptance: {rid}')
        return rid

    def start(self, rid: str, *extra: str) -> subprocess.Popen:
        if not self.duration_remained():
            raise TimeoutError('Scenario duration expired; no new execution started')
        command = [PY, str(ROOT / 'lib/run_store.py'), *(('run', rid, *extra) if extra else ('execute', self.project_ref, str(HOME / 'runs' / rid / 'prompt.md')))]
        env = self.environment()
        if not extra:
            env['CADRE_RUN_ID'] = rid
        with open(self.directory / f'coordinator-{rid[-8:]}.log', 'a') as log:
            return subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

    def wait(self, proc: subprocess.Popen, condition=None, interval: float = 10.0) -> bool:
        while self.duration_remained():
            if proc.poll() is not None:
                return True
            if condition and condition():
                return False
            time.sleep(min(interval, self.duration_remained()))
        self.event('TIME EXPIRED: stopping the process tree')
        self.stop(proc)
        return True

    def again_wait(self, rid: str, then: float = 0.0) -> bool:
        while self.duration_remained():
            record = self.record(rid)
            if not record:
                raise ValueError('retry record uncertain')
            if record.get('state') in TERMINAL:
                return False
            retry = record.get('retry_after') or 0
            if isinstance(retry, bool) or not isinstance(retry, (int, float)) or not math.isfinite(retry):
                raise ValueError('retry time uncertain')
            remaining = max(then, retry) - time.time()
            if remaining <= 0:
                return True
            time.sleep(min(remaining, self.duration_remained(), 1))
        return False

    def stop(self, proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()

    def record(self, rid: str) -> dict:
        try:
            return json.loads((HOME / 'runs' / rid / 'run.json').read_text())
        except (OSError, ValueError):
            return {}

    def tree(self, rid: str) -> list[str]:
        ids = {rid, *(self.record(rid).get('project_run_ids') or [])}
        for path in (HOME / 'runs').glob('*/run.json'):
            try:
                if json.loads(path.read_text()).get('project_ref_parent') == rid:
                    ids.add(path.parent.name)
            except (OSError, ValueError):
                continue
        return sorted(ids)

    def receipts(self, ids: list[str]) -> dict:
        from budget_gate import FREE, PAID, USD_PROVIDERS, base_provider
        if not ids:
            return {'providers': {}, 'lines': [], 'calls': []}
        db = HOME / 'chain/request-budget.db'
        mark = ','.join('?' * len(ids))
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)) as con:
            lines = con.execute(f"select provider, reserved, actual, created, json_extract(metadata,'$.run_id'), "
                                   f"json_extract(metadata,'$.session_id'), receipt, metadata, id from reservation "
                                   f"where json_extract(metadata,'$.run_id') in ({mark})", ids).fetchall()
        outcome: dict = {}
        calls = []
        for provider, reserved, real, start, run_id, session, receipt, raw, ident in lines:
            metadata = json.loads(raw)
            if not isinstance(metadata, dict):
                raise ValueError('receipt metadata uncertain')
            if (type(reserved) is not int or reserved < 0 or (real is not None and (type(real) is not int or real < 0))
                    or not math.isfinite(start)):
                raise ValueError('receipt amount or time uncertain')
            base = base_provider(provider)
            free = (provider.split('#', 1)[0].endswith(':free') and base in {*PAID, *FREE}) or base in FREE
            unit = 'request' if free else 'cash_micro' if base in ('opencode', 'openrouter') else 'subscription_equivalent_micro' if base in ('opencode-go', 'claude') else 'unknown'
            s = outcome.setdefault(provider, {'unit': unit, 'request': 0, 'paid_request': 0, 'free_request': 0,
                'real_micro': 0, 'pending': 0, 'pending_micro': 0, 'pending_count': 0, 'free_count': 0,
                'assurance': 0, 'assurance_consumed': 0, 'unused': 0, 'uncertain': 0})
            if real == 0 and receipt == 'unused:' + ident:
                s['unused'] += 1
                continue
            if metadata.get('assurance') and real == 0 and receipt == 'claimed:' + ident:
                s['assurance_consumed'] += 1
                continue
            bounds = [metadata.get(name) or 0 for name in ('worst', 'verified_bound_micro')]
            if any(type(value) is not int or value < 0 for value in bounds):
                raise ValueError('The receipt upper bound is uncertain')
            bound = max(reserved, real or 0, *bounds)
            if metadata.get('assurance') and real is None:
                s['assurance'] += 1
                s['pending_micro' if not free else 'pending_count'] += bound
                continue
            known = (real is not None and isinstance(receipt, str) and receipt.startswith(ident + ':')
                       and not metadata.get('assurance') and not metadata.get('observation')
                       and (free or base in USD_PROVIDERS))   # CXR2-03: bucket name 'provider[#model|:free]'; the base provider decides
            round = 'free' if known and free else 'paid' if known else 'uncertain'
            if not known:
                s['pending'] += 1
                s['uncertain'] += 1
                s['pending_micro' if not free else 'pending_count'] += bound
            else:
                s['request'] += 1
                s['free_request' if free else 'paid_request'] += 1
                s['free_count' if free else 'real_micro'] += real
            last = metadata.get('settled_ts')
            if not isinstance(last, (int, float)) or isinstance(last, bool) or not math.isfinite(last) or last < start:
                last = None
            calls.append({'id': ident, 'provider': provider, 'run_id': run_id, 'session_id': session,
                            'round': round, 'start': start, 'end': last})
        return {'providers': outcome, 'lines': lines, 'calls': calls}

    def window_calls(self, receipt: dict, start: float, end: float, *, only_paid: bool = True) -> list[dict]:
        outcome = []
        for row in receipt['calls']:
            if only_paid and row['round'] == 'free':
                continue
            if row['start'] >= end or (row['end'] is not None and row['end'] <= start):
                continue
            outcome.append({**row, 'moment': 'uncertain' if row['start'] < start else 'within range'})
        return outcome

    def liability(self, receipt: dict) -> int:
        from budget_gate import base_provider
        return sum(value['real_micro'] + value['pending_micro'] for provider, value in receipt.items()
                   if base_provider(provider) == PROVIDER[self.path] and value.get('unit') != 'request')

    def product_check(self, wt: str, command: str, protected: dict[str, str]) -> dict:
        if not wt or not Path(wt).is_dir():
            return {'worktree': wt, 'rc': None}
        r = subprocess.run(command, shell=True, cwd=wt, capture_output=True, text=True, timeout=300)
        changed = git(Path(wt), 'diff', '--name-only', self.base, 'HEAD').splitlines() + \
            [x for x in git(Path(wt), 'status', '--porcelain').splitlines() if '__pycache__' not in x]
        return {'worktree': wt, 'rc': r.returncode, 'output': (r.stdout + r.stderr)[-1500:], 'changed': changed,
                'protected_same': summary_hash(Path(wt), list(protected)) == protected}

    def write(self, outcome: dict) -> dict:
        outcome = {'id': self.cid, 'stamp': self.stamp, 'runner': self.runner(), 'path': self.path, 'chain': self.chain if self.path == 'or' else '-',
                 'project_ref': self.project_ref, 'repo': str(self.repo), 'minute': self.minute, 'total_cap_usd': self.total_cap,
                 'go_once': self.go_once, 'go_then': self.go_status(), 'events': self.events, **outcome}
        (self.directory / 'summary.json').write_text(json.dumps(outcome, ensure_ascii=False, indent=2, default=str))
        with open(ARCHIVE / 'scenarios.jsonl', 'a') as f:
            f.write(json.dumps({k: outcome.get(k) for k in ('id', 'stamp', 'runner', 'decision', 'runs', 'reason')},
                               ensure_ascii=False, default=str) + '\n')
        print(json.dumps({k: outcome.get(k) for k in ('id', 'decision', 'reason', 'runs')}, ensure_ascii=False, default=str))
        return outcome

    def close(self) -> list[str]:
        closing = []
        for path in (HOME / 'runs').glob('*/run.json'):
            try:
                k = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if str(k.get('project') or '').startswith(self.project_ref) and k.get('state') not in TERMINAL:
                r = subprocess.run([PY, str(ROOT / 'lib/run_store.py'), 'cancel', k['run_id']], env=self.environment(), capture_output=True, text=True)
                if k['run_id'] in r.stdout:
                    closing.append(f"{k['run_id']} ({k.get('state')})")
        if closing:
            self.event(f'Test runs closed: {closing}')
        return closing

    def runner(self) -> str:
        try:
            return git(Path(__file__).resolve().parents[2], 'rev-parse', '--short', 'HEAD')
        except (subprocess.CalledProcessError, OSError):
            return '-'


def single_run(s: Scenario, prompt: str) -> tuple[str, dict]:
    rid = s.acceptance(prompt)
    proc = s.start(rid)
    s.wait(proc)
    return rid, s.record(rid)


def evidence(s: Scenario, rid: str, k: dict, qa: str, protected: dict[str, str]) -> dict:
    from budget_gate import base_provider
    fr = k.get('final_result') or {}
    m = s.receipts(s.tree(rid))
    return {'runs': s.tree(rid), 'status': k.get('state'), 'reason': k.get('reason'), 'final': fr.get('status'),
            'qa': (fr.get('qa') or {}).get('status'), 'judge': (fr.get('review') or {}).get('status'),
            'judge_independent': (fr.get('review') or {}).get('independent'), 'receipt': m['providers'],
            'unpermitted_path': sorted(p for p, v in m['providers'].items() if base_provider(p) not in (PROVIDER[s.path], 'groq') and (v['request'] or v['pending'])),
            'pending': sum(v['pending'] for v in m['providers'].values()), 'product': s.product_check(k.get('worktree') or '', qa, protected)}


def c01(s: Scenario) -> dict:
    qa = f'{PY} -m unittest -v test_calc'
    s.base = s.repo_setup({'calc.py': CALC, 'test_calc.py': TEST_CALC}, qa, 'Integer addition using only standard unittest.',
                         'Disposable test. Only calc.py may change; preserve test_calc.py. No installs, downloads or remote Git actions.')
    protected = summary_hash(s.repo, ['test_calc.py'])
    rid, k = single_run(s, PROMPT_CALC)
    e = evidence(s, rid, k, qa, protected)
    passed = (e['final'] == 'PASS' and e['product'].get('rc') == 0 and e['product'].get('protected_same')
             and e['product'].get('changed') == ['calc.py'] and not e['unpermitted_path'] and e['pending'] == 0)
    return {'decision': 'PASS' if passed else 'FAIL', **e}


def backlog_repo(s: Scenario, items: dict) -> tuple[str, dict[str, str]]:
    files = {'tests/__init__.py': ''}
    lines = ['# BACKLOG', '']
    for mid, (definition, dependent, body) in items.items():
        files[f'tests/test_{mid.lower()}.py'] = 'import unittest\n' + body
        lines.append(f"- {mid}: {definition}. Dependent: {', '.join(dependent) or '-'}. "
                        f"Acceptance command: python3 -m unittest -v tests.test_{mid.lower()}")
    files['BACKLOG.md'] = '\n'.join(lines) + '\n'
    qa = f'{PY} -m unittest discover -s tests -t . -v'
    s.base = s.repo_setup(files, qa, 'Small text library in text.py and text_cli.py using only the standard library.',
                         'Disposable test. Preserve tests/ files and BACKLOG.md. No installs, downloads or remote Git actions.')
    return qa, summary_hash(s.repo, [d for d in files if d.startswith('tests/')] + ['BACKLOG.md'])


def project_ref_execute(s: Scenario, prompt: str) -> tuple[str, int]:
    rid = s.acceptance(prompt)
    driver = (f"import os,sys,time;sys.path.insert(0,{str(ROOT / 'lib')!r});import project_mode;from run_store import RunStore,FileLock;"
              f"from outcome import approved;st=RunStore();rid={rid!r};l=FileLock(st.path(rid)/'.owner.lock');assert l.acquire(blocking=False);"
              f"st.update(rid,state='running',phase='dispatch',owner_pid=os.getpid());rc=project_mode.start(rid,{s.project_ref!r});k=st.read(rid);"
              f"st.update(rid,state='completed' if rc==0 and approved(k) else 'failed',exit_code=rc,owner_pid=None);sys.exit(rc)")
    env = s.environment()
    env['CADRE_RUN_ID'] = rid
    with open(s.directory / 'project-ref-driver.log', 'a') as log:
        proc = subprocess.Popen([PY, '-c', driver], env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
        s.wait(proc, interval=30)
    return rid, proc.returncode


def project_ref_evidence(s: Scenario, rid: str, qa: str, protected: dict[str, str], items: list[str]) -> dict:
    k = s.record(rid)
    try:
        d = json.loads((HOME / 'runs' / rid / 'project_mode.json').read_text())
    except (OSError, ValueError):
        d = {}
    e = evidence(s, rid, k, qa, protected)
    features = [{x: f.get(x) for x in ('id', 'title', 'status', 'attempt', 'qa_command', 'last_acceptance')} for f in d.get('features') or []]
    match = {m: [f['id'] for f in features if m in f'{f.get("title")} {f.get("qa_command")}'] for m in items}
    item_rc = {}
    if e['product'].get('worktree'):
        for m in items:
            item_rc[m] = subprocess.run(f'{PY} -m unittest -v tests.test_{m.lower()}', shell=True, cwd=e['product']['worktree'],
                                         capture_output=True, text=True, timeout=300).returncode
    return {**e, 'project_ref_status': d.get('status'), 'project_ref_reason': d.get('reason'), 'features': features, 'item_match': match,
            'item_rc': item_rc, 'children': [(c.get('feature'), c.get('run'), c.get('status'), c.get('success')) for c in d.get('children') or []]}


def c02(s: Scenario) -> dict:
    qa, protected = backlog_repo(s, BACKLOG_TESTS)
    rid, rc = project_ref_execute(s, "Implement B001-B005 from BACKLOG.md. Each item has an acceptance command recorded in BACKLOG.md; "
                           "Preserve tests/ and BACKLOG.md. Do not push, merge or install anything.\n")
    e = project_ref_evidence(s, rid, qa, protected, list(BACKLOG_TESTS))
    repeat = [f['id'] for f in e['features'] if f['status'] == 'passed' and (f.get('attempt') or 0) > 1]
    passed = (rc == 0 and e['final'] == 'PASS' and all(v == 0 for v in e['item_rc'].values()) and len(e['item_rc']) == 5
             and all(e['item_match'].values()) and e['product'].get('protected_same') and not e['unpermitted_path'] and e['pending'] == 0)
    return {'decision': 'PASS' if passed else 'FAIL', 'driver_rc': rc, 'second_attempt_passing': repeat, **e}


def c03(s: Scenario) -> dict:
    items = {'B001': BACKLOG_TESTS['B001'], IMPOSSIBLE[0]: IMPOSSIBLE[1]}
    qa, protected = backlog_repo(s, items)
    rid, rc = project_ref_execute(s, "Implement B001 and B009 from BACKLOG.md. Each item has an acceptance command in BACKLOG.md; "
                           "Preserve tests/ and BACKLOG.md. Do not push, merge or install anything.\n")
    e = project_ref_evidence(s, rid, qa, protected, list(items))
    impossible = [f for f in e['features'] if 'B009' in f'{f.get("title")} {f.get("qa_command")}']
    passed = (e['final'] != 'PASS' and e['project_ref_status'] != 'complete' and rc != 0 and e['product'].get('protected_same', True)
             and not any(f['status'] == 'passed' for f in impossible) and not e['unpermitted_path'])
    return {'decision': 'PASS' if passed else 'FAIL', 'driver_rc': rc, 'impossible_feature': impossible, **e}


def two_file_repo(s: Scenario) -> tuple[str, dict[str, str]]:
    qa = f'{PY} -m unittest -v test_frame test_cube'
    s.base = s.repo_setup(TWO_FILE, qa, 'Two small mathematical functions.', 'Disposable test. Preserve test_*.py. No installs or remote Git actions.')
    return qa, summary_hash(s.repo, ['test_frame.py', 'test_cube.py'])


def tasks(k: dict) -> dict:
    return {g: (v or {}).get('state') for g, v in (k.get('tasks') or {}).items()}


def c04(s: Scenario) -> dict:
    qa, protected = two_file_repo(s)
    rid = s.acceptance(PROMPT_TWO)
    proc = s.start(rid)

    def lane_started() -> bool:
        k = s.record(rid)
        return any(g not in ('plan', 'qa', 'quality') and st in ('running', 'produced') for g, st in tasks(k).items())
    finished = s.wait(proc, lane_started, interval=3)
    cut = None
    if not finished:
        time.sleep(20)
        cut = {'tasks': tasks(s.record(rid)), 'receipt': s.receipts([rid])['providers']}
        s.event(f'stopping the coordinator tree with SIGKILL: {cut["tasks"]}')
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        time.sleep(5)
        last = time.time() + 600
        while True:
            proc = s.start(rid, '--proceed')
            s.wait(proc)
            if proc.returncode != 75 or time.time() > last:
                break
            s.event('ownership lock still held by lanes; resuming again after 15s')
            time.sleep(15)
    k = s.record(rid)
    e = evidence(s, rid, k, qa, protected)
    attempts = {g: len([d for d in (HOME / 'runs' / rid / 'tasks' / g / 'attempts').glob('*') if d.is_dir()])
                 for g in tasks(k) if (HOME / 'runs' / rid / 'tasks' / g / 'attempts').is_dir()}
    once_finished = [g for g, st in ((cut or {}).get('tasks') or {}).items() if st == 'produced']
    passed = (cut is not None and e['final'] == 'PASS' and e['product'].get('rc') == 0 and e['product'].get('protected_same')
             and all(attempts.get(g, 0) <= 1 for g in once_finished) and not e['unpermitted_path'] and e['pending'] == 0)
    return {'decision': 'PASS' if passed else 'FAIL', 'outage': cut, 'attempts': attempts, **e}


def c05(s: Scenario) -> dict:
    from budget_gate import base_provider
    if s.path != 'go':
        return {'decision': 'SKIPPED', 'reason': 'C05 closes and reopens the Go window; no equivalent in OR mode'}
    qa, protected = two_file_repo(s)
    rid = s.acceptance(PROMPT_TWO)
    stamp = HOME / 'chain/go-429-last.txt'
    old = stamp.read_text() if stamp.exists() else None
    proc = s.start(rid)

    def first_receipt() -> bool:
        line = s.receipts(s.tree(rid))['calls']
        return any(r['round'] == 'paid' for r in line) and any(st == 'running' for g, st in tasks(s.record(rid)).items()
                                                            if g not in ('plan', 'qa', 'quality'))
    finished = s.wait(proc, first_receipt, interval=3)
    closing = None
    if not finished:
        closing = time.time()
        stamp.write_text(str(int(closing - 18000 + 150)))
        s.event('Go window closed for a controlled 150s using a 429 marker')
        try:
            s.wait(proc)
        finally:
            if stamp.exists() and stamp.read_text().strip() == str(int(closing - 18000 + 150)):
                stamp.unlink() if old is None else stamp.write_text(old)
        k = s.record(rid)
        if k.get('state') != 'completed':
            wait = max(0.0, float(k.get('retry_after') or 0) - time.time())
            s.event(f"intermediate status {k.get('state')} / {k.get('reason')}; {wait:.0f} s before continuing")
            if s.again_wait(rid, closing + 155):
                proc = s.start(rid, '--proceed')
                s.wait(proc)
    elif old is None and stamp.exists():
        stamp.unlink()
    k = s.record(rid)
    e = evidence(s, rid, k, qa, protected)
    receipt = s.receipts(s.tree(rid))
    then = [row for row in s.window_calls(receipt, closing + 5, closing + 140, only_paid=False)
             if row['round'] != 'free' or base_provider(row['provider']) == PROVIDER[s.path]] if closing else []
    rejection, rejection_uncertain = [], []
    for d in (HOME / 'runs' / rid / 'tasks').glob('*/attempts/*/budget-denied.json'):
        name = str(d.relative_to(HOME / 'runs' / rid))
        try:
            denial = json.loads(d.read_text())
            if (closing is not None and closing <= denial['time'] <= closing + 150
                    and any(base_provider(row.get('provider') or '') == 'opencode-go'
                            and row.get('scope') == 'provider' and row.get('reason') == 'provider window closed'
                            for row in denial['refusals'])):
                rejection.append(name)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            rejection_uncertain.append(name)
    failure = []
    failure_uncertain = False
    try:
        failure = [m for m, v in json.loads((HOME / 'chain/provider-status.json').read_text()).items()
                 if 'opencode-go' in m and closing and float((v or {}).get('ts') or 0) > closing]
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, AttributeError):
        failure_uncertain = True
    passed = (closing is not None and e['final'] == 'PASS' and not then and bool(rejection) and not failure
             and e['product'].get('rc') == 0 and not e['unpermitted_path'] and e['pending'] == 0
             and k.get('state') == 'completed' and e['product'].get('protected_same') and s.duration_remained() > 0
             and not rejection_uncertain and not failure_uncertain)
    return {'decision': 'PASS' if passed else 'FAIL', 'closed_while_receipt': len(then), 'closed_while_calls': then,
            'budget_rejection': rejection, 'budget_rejection_uncertain': rejection_uncertain, 'go_failure_record': failure,
            'go_failure_uncertain': failure_uncertain, **e}


def c06(s: Scenario, *, only_alarm: bool = False) -> dict:
    qa = f'{PY} -m unittest -v test_calc'
    s.base = s.repo_setup({'calc.py': CALC, 'test_calc.py': TEST_CALC}, qa, 'Integer addition.', 'Disposable test. Preserve test_calc.py.')
    protected = summary_hash(s.repo, ['test_calc.py'])
    sub = {}
    if only_alarm:
        alarm = alarm_continuation(s, qa, protected)
        return {'decision': 'PASS' if alarm['passed'] else 'FAIL', 'sub': {'alarm': alarm}, 'runs': [alarm['run']]}
    total_micro = int(s.cap * 1e6)
    low_micro = min(total_micro, 4000)
    s.cap = low_micro / 1e6
    rid = s.acceptance(PROMPT_CALC)
    s.wait(s.start(rid))
    k = s.record(rid)
    m = s.receipts(s.tree(rid))['providers']
    spent = s.liability(m) / 1e6
    cap_rejection = [row for row in (k.get('denials') or []) if str(row.get('reason') or '').startswith('run budget exhausted')]
    sub['low_cap'] = {'run': rid, 'status': k.get('state'), 'reason': k.get('reason'), 'spent_usd': spent,
                          'final': (k.get('final_result') or {}).get('status'),
                          'cap_rejection': cap_rejection,
                          'passed': k.get('state') in ('failed', 'retryable', 'paused') and bool(cap_rejection)
                          and (k.get('final_result') or {}).get('status') != 'PASS'
                          and spent <= low_micro / 1e6 and s.duration_remained() > 0}
    subprocess.run([PY, str(ROOT / 'lib/run_store.py'), 'cancel', rid], env=s.environment(), capture_output=True, text=True)
    middle_micro = (total_micro - low_micro) // 2
    s.cap = middle_micro / 1e6
    rid = s.acceptance(PROMPT_CALC)
    proc = s.start(rid)
    finished = s.wait(proc, lambda: 'qa' in tasks(s.record(rid)), interval=2)
    cancel = None
    if not finished:
        cancel = time.time()
        subprocess.run([PY, str(ROOT / 'lib/run_store.py'), 'cancel', rid], env=s.environment(), capture_output=True, text=True)
        s.event('cancel during QA')
        s.wait(proc)
    k = s.record(rid)
    receipt = s.receipts(s.tree(rid))
    then = s.window_calls(receipt, cancel + 2, time.time(), only_paid=False) if cancel else []
    sub['qa_cancel'] = {'run': rid, 'status': k.get('state'), 'new_request_after_cancel': len(then),
                       'pending': sum(v['pending'] for v in receipt['providers'].values()), 'cancel_after_calls': then,
                       'passed': cancel is not None and k.get('state') == 'cancelled' and not then}
    s.cap = (total_micro - low_micro - middle_micro) / 1e6
    sub['alarm'] = alarm_continuation(s, qa, protected)
    return {'decision': 'PASS' if all(v['passed'] for v in sub.values()) else 'FAIL', 'sub': sub, 'runs': [v['run'] for v in sub.values()]}


def alarm_continuation(s: Scenario, qa: str, protected: dict[str, str]) -> dict:
    rid = s.acceptance(PROMPT_CALC)
    alarm_start = time.time()
    subprocess.run([PY, str(ROOT / 'lib/run_store.py'), 'update', rid], input=json.dumps(
        {'spend_alarm': {'ts': time.time(), 'reason': 'controlled test alarm'}}), text=True, env=s.environment(), check=True)
    s.wait(s.start(rid))
    k1 = s.record(rid)
    s.wait(s.start(rid, '--proceed'))
    unapproved = s.record(rid)
    alarm_receipt = s.receipts(s.tree(rid))
    request_alarm = s.window_calls(alarm_receipt, alarm_start, time.time())
    subprocess.run([PY, str(ROOT / 'lib/run_store.py'), 'spend-approval', rid], env=s.environment(), capture_output=True, text=True, check=True)
    s.event('Controlled alarm approved; continuing the run')
    if s.again_wait(rid):
        s.wait(s.start(rid, '--proceed'))
    k2 = s.record(rid)
    e = evidence(s, rid, k2, qa, protected)
    liability = s.liability(e['receipt'])
    return {'run': rid, 'alarm_status': k1.get('state'), 'alarm_reason': k1.get('reason'),
            'unapproved_status': unapproved.get('state'), 'unapproved_reason': unapproved.get('reason'),
            'alarm_request': len(request_alarm), 'alarm_calls': request_alarm,
            'alarm_receipt': alarm_receipt['providers'], 'approval_after_final': e['final'], 'evidence': e,
            'liability_micro': liability, 'cap_usd': s.cap,
            'passed': not request_alarm and k2.get('state') == 'completed' and e['final'] == 'PASS' and e['pending'] == 0
            and (k1.get('final_result') or {}).get('status') != 'PASS'
            and (unapproved.get('final_result') or {}).get('status') != 'PASS'
            and all(k1.get(key, 0) == unapproved.get(key, 0) for key in ('retry_count', 'revive_count'))
            and e['product'].get('rc') == 0 and e['product'].get('protected_same')
            and e['product'].get('changed') == ['calc.py'] and e['judge_independent'] and not e['unpermitted_path']
            and liability <= int(s.cap * 1e6) and s.duration_remained() > 0}


def c07(s: Scenario) -> dict:
    qa = (f'{PY} -m unittest -v test_calc && {PY} -c "import calc,sys; sys.exit(0 if getattr(calc, \'VERSION\', None) == \'1.0\' '
          f'else \'PROJECT RULE: calc.py must define the module constant VERSION as the string 1.0\')"')
    s.base = s.repo_setup({'calc.py': CALC, 'test_calc.py': TEST_CALC}, qa, 'Integer addition.',
                         'Disposable test. Preserve test_calc.py. No installs or remote Git actions.')
    protected = summary_hash(s.repo, ['test_calc.py'])
    rid, k = single_run(s, PROMPT_CALC)
    e = evidence(s, rid, k, qa, protected)
    fr = k.get('final_result') or {}
    repair = [a for a in ((fr.get('qa') or {}).get('attempts') or []) if a.get('status') == 'FAIL']
    sub = {'qa_repair': {'run': rid, 'first_qa_red': len(repair), 'final': e['final'], 'qa': e['qa'],
                          'passed': bool(repair) and e['final'] == 'PASS' and e['product'].get('rc') == 0}}
    (HOME / 'projects' / s.project_ref / 'qa-command').write_text('echo "QA intentionally fails: the product cannot satisfy this rule"; exit 1\n')
    s.cap = 0.5
    red = s.acceptance('Append a "# note" comment to calc.py; change nothing else.\n'
                  'Acceptance: The last line of calc.py is "# note". Do not push, merge or install.\n')
    s.wait(s.start(red))
    k1 = s.record(red)
    rev1 = ((k1.get('final_result') or {}).get('qa') or {}).get('revision')
    if k1.get('state') != 'completed':
        s.event(f"Failed run {k1.get('state')}; retrying acceptance without product changes")
        s.wait(s.start(red, '--proceed'))
    k2 = s.record(red)
    sub['unchanging_red'] = {'run': red, 'first': (k1.get('final_result') or {}).get('status'), 'first_reason': k1.get('reason'),
                             'second': (k2.get('final_result') or {}).get('status'), 'second_reason': k2.get('reason'),
                             'same_revision': rev1 == ((k2.get('final_result') or {}).get('qa') or {}).get('revision'),
                             'passed': (k1.get('final_result') or {}).get('status') != 'PASS'
                             and (k2.get('final_result') or {}).get('status') != 'PASS'}
    return {'decision': 'PASS' if all(v['passed'] for v in sub.values()) else 'FAIL', 'sub': sub, 'runs': [rid, red], **e}


SCENARIOS = {'C01': c01, 'C02': c02, 'C03': c03, 'C04': c04, 'C05': c05, 'C06': c06, 'C07': c07}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('ident', choices=sorted(SCENARIOS))
    ap.add_argument('--minute', type=float, default=None)
    ap.add_argument('--cap', type=float, default=10.0)
    ap.add_argument('--path', choices=sorted(PROVIDER), default='go', help='go: Go only, or: OpenRouter only')
    ap.add_argument('--chain', choices=('cheap', 'production'), default='cheap',
                    help='Plan and judge chain for OR mode: cheap (DeepSeek plan, GLM/Qwen judge) or production (Sol)')
    ap.add_argument('--only-alarm', action='store_true', help='C06: wait without approval and continue after approval in a single run')
    a = ap.parse_args()
    if a.only_alarm and a.ident != 'C06':
        ap.error('--only-alarm is only valid with C06')
    s = Scenario(a.ident, a.minute if a.minute is not None else (45.0 if a.ident in ('C02', 'C03') else 25.0), a.cap, a.path, a.chain)
    s.base = ''
    try:
        outcome = c06(s, only_alarm=a.only_alarm) if a.ident == 'C06' else SCENARIOS[a.ident](s)
    except Exception as exc:
        outcome = {'decision': 'ERROR', 'reason': f'{type(exc).__name__}: {exc}'}
    outcome['closing'] = s.close()
    s.write(outcome)
    return 0 if outcome.get('decision') in ('PASS', 'SKIPPED') else 1


if __name__ == '__main__':
    raise SystemExit(main())
