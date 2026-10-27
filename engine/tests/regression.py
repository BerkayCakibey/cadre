#!/usr/bin/env python3
"""V3: code + config + hook + Python/Node behaviour, only in a temporary home.

The gate does not claim live model and provider acceptance.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE / 'lib'))
import qa_evidence


def archive_path() -> Path:
    if os.environ.get('CADRE_TEST_ARCHIVE'):
        return Path(os.environ['CADRE_TEST_ARCHIVE'])
    return ENGINE.parent / '.tmp/test-archive'


def archive_record(archive: Path, steps: list[dict], daily: str, outcome: str) -> None:
    git = lambda *a: subprocess.run(['git', '-C', str(ENGINE.parent), *a], capture_output=True, text=True).stdout.strip()
    ran = re.findall(r'^Ran (\d+) tests', daily, re.M)
    worker = re.findall(r'^worker: (\d+)', daily, re.M)
    skipped = re.findall(r'^Skipped: (\d+)', daily, re.M)
    node = dict(re.findall(r'^\u2139 (tests|pass|fail|skipped) (\d+)', daily, re.M))
    stamp = time.strftime('%Y%m%d-%H%M%S')
    record = {'ts': stamp, 'commit': git('rev-parse', '--short', 'HEAD'), 'dirty': bool(git('status', '--porcelain')),
             'outcome': outcome, 'steps': steps, 'python_test': int(ran[-1]) if ran else None,
             'python_skipped': int(skipped[-1]) if skipped else None, 'node': {k: int(v) for k, v in node.items()},
             'worker': int(worker[-1]) if worker else None}
    try:
        (archive / 'regression').mkdir(parents=True, exist_ok=True)
        (archive / 'regression' / f"{stamp}-{record['commit'] or 'uncommitted'}.log").write_text(daily)
        with open(archive / 'regression.jsonl', 'a') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
        print(f'ARCHIVE: {archive}/regression.jsonl')
    except OSError as exc:
        print(f'WARNING could not archive: {exc}', file=sys.stderr)


def environment_warning(env: dict) -> str:
    if env.get('CADRE_TEST_PYTEST_PYTHON'):
        return ''
    return ('WARNING: CADRE_TEST_PYTEST_PYTHON is unset — test_qa_scope requires a Python environment with pytest, Ruff and BasedPyright; '
            'the shared gate fails in this environment (FIX-18). Example: CADRE_TEST_PYTEST_PYTHON=/path/to/venv/bin/python')


def main() -> int:
    warning = environment_warning(dict(os.environ))
    if warning:
        print(warning, flush=True)
    passed = failed = 0
    archive, steps, daily = archive_path(), [], []
    panel = Path(os.environ.get('CADRE_UI_PYTHON') or Path.home() / '.local/share/cadre-ui-venv/bin/python')
    with tempfile.TemporaryDirectory(prefix='qe-regression-') as directory:
        home = Path(directory)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('CADRE_', 'OPENCODE_', 'E2E_', 'XDG_', 'GIT_'))}
        env.update(HOME=str(home), CADRE_HOME=str(home / 'data'), CADRE_ENGINE_ROOT=str(ENGINE),
                   GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0',
                   CADRE_WAIT_THRESHOLD_SEC='0', PYTHONDONTWRITEBYTECODE='1')
        if panel.exists():
            env['CADRE_UI_PYTHON'] = str(panel)
        for key in ('CADRE_TEST_PYTEST_PYTHON', 'CADRE_TEST_WORKER'):
            if os.environ.get(key):
                env[key] = os.environ[key]
        commands = [
            ('syntax', [sys.executable, str(ENGINE / 'lib/qa_derive.py'), 'syntax', str(ENGINE.parent)]),
            ('Python behavior + contract + config', [sys.executable, str(ENGINE / 'tests/parallel_run.py')]),
            ('Node budget plugin + real-addon integration', ['node', '--test', str(ENGINE / 'tests/budget-plugin.test.mjs'),
                                                                  str(ENGINE / 'tests/integration.test.mjs'),
                                                                  str(ENGINE / 'tests/thinking.test.mjs')]),
            ('Git delivery guard (local bare repo)', ['bash', str(ENGINE / 'tests/pushguard.sh')]),
        ]
        for name, command in commands:
            print(f'== {name} ==', flush=True)
            start = time.time()
            step_env, proof = env, None
            if name.startswith('Node'):
                archive.mkdir(parents=True, exist_ok=True)
                token = uuid.uuid4().hex
                evidence = archive / ('node-evidence-' + token + '.jsonl')
                step_env = {**env, 'CADRE_QA_EVIDENCE': str(evidence), 'CADRE_QA_TOKEN': token}
                command = [*command[:2], '--test-reporter=spec', '--test-reporter-destination=stdout',
                           '--test-reporter=' + str(ENGINE / 'lib/qa-node-reporter.cjs'),
                           '--test-reporter-destination=stdout', *command[2:]]
            try:
                with subprocess.Popen(command, cwd=ENGINE.parent, env=step_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, errors='replace') as proc:
                    hour = threading.Timer(2400, proc.kill)
                    hour.start()
                    try:
                        for line in proc.stdout:
                            print(line, end='', flush=True)
                            daily.append(line)
                        success = proc.wait() == 0
                        if name.startswith('Python'):
                            skipped = re.findall(r'^Skipped: (\d+)', ''.join(daily), re.M)
                            success = success and bool(skipped) and int(skipped[-1]) == 0
                        if name.startswith('Node'):
                            expected = {p: names for p, names in qa_evidence.inventory(ENGINE.parent).items() if not p.endswith('.py')}
                            proof = qa_evidence.coverage(ENGINE.parent, evidence, token, expected, False)
                            success = success and proof['status'] == 'PASS'
                            print('Node discovery: ' + json.dumps(proof, ensure_ascii=False), flush=True)
                    finally:
                        hour.cancel()
            except (OSError, ValueError, SyntaxError, subprocess.TimeoutExpired) as exc:
                print(f'ERROR: {name}: {exc}', file=sys.stderr)
                success = False
            steps.append({'entry_name': name, 'outcome': 'PASS' if success else 'FAIL', 'sec': round(time.time() - start, 1)})
            if proof is not None:
                steps[-1]['discovery'] = proof
                steps[-1]['evidence'] = str(evidence)
            if success:
                passed += 1
            else:
                failed += 1
            print(f'  {"PASS" if success else "FAIL"}  {name}', flush=True)
    print('  SKIP  Live model/provider acceptance run (READY-1)')
    outcome = f'OUTCOME: {passed} PASS, {failed} FAIL, 1 SKIP'
    steps.append({'entry_name': 'Live model/provider acceptance run (READY-1)', 'outcome': 'SKIP', 'sec': 0})
    archive_record(archive, steps, ''.join(daily), outcome)
    print(outcome)
    return int(failed > 0)


if __name__ == '__main__':
    raise SystemExit(main())
