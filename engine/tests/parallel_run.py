#!/usr/bin/env python3
"""Runs unit tests in a separate process per module, in parallel (02.10: `unittest discover` in one process took ~11 min).

Each module gets its own isolated HOME/CADRE_HOME. Output is in unittest form ('Ran N tests' + 'OK' / 'FAILED'),
the regression's RC51/RC51b lines read it as is. Usage: `python3 parallel_run.py [module-part ...]`;
by default at most four workers, explicit choice with `CADRE_TEST_WORKER`.
"""
import concurrent.futures
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'lib'))
import qa_evidence

ROOT = pathlib.Path(__file__).resolve().parent


def worker_count(env: dict[str, str], cpu: int | None) -> int:
    """N-OBS05: heavy modules that spawn subprocesses must not spread over all CPUs and trigger their own timeouts."""
    setting = env.get('CADRE_TEST_WORKER')
    if not setting:
        return max(1, min(4, cpu or 4))
    workers = int(setting)
    if workers < 1:
        raise ValueError('positive worker count required')
    return workers


def execute(mod: str, base: str) -> tuple[str, int, str, float]:
    home_dir = pathlib.Path(tempfile.mkdtemp(prefix=f"ut-{mod}-", dir=base))
    (home_dir / "home_dir").mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(('CADRE_', 'OPENCODE_', 'E2E_'))}
    env.update(HOME=str(home_dir / 'home_dir'), CADRE_HOME=str(home_dir), CADRE_ENGINE_ROOT=str(ROOT.parent), CADRE_WAIT_THRESHOLD_SEC='0')
    # N-OBS05: an empty fixture under .tmp must not take the parent source repository for its own Git identity.
    env['GIT_CEILING_DIRECTORIES'] = os.pathsep.join(filter(None, (os.environ.get('GIT_CEILING_DIRECTORIES'), tempfile.gettempdir())))
    for key in ('CADRE_QA_EVIDENCE', 'CADRE_QA_TOKEN', 'CADRE_TEST_PYTEST_PYTHON'):
        if key in os.environ:
            env[key] = os.environ[key]
    panel = os.environ.get('CADRE_UI_PYTHON') or str(pathlib.Path.home() / '.local/share/cadre-ui-venv/bin/python')
    if pathlib.Path(panel).exists():
        env['CADRE_TEST_PANEL_PYTHON'] = panel
    external_evidence = bool(env.get('CADRE_QA_EVIDENCE'))
    evidence, token = home_dir / 'test-evidence.jsonl', uuid.uuid4().hex
    if not external_evidence:
        env = qa_evidence.environment(env, evidence, token)
    t0 = time.time()
    with subprocess.Popen([sys.executable, '-W', 'default::ResourceWarning', "-m", "unittest", mod],
                          cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, start_new_session=True) as process:
        try:
            out, err = process.communicate(timeout=1800)
            rc, output = process.returncode, out + err
            if not external_evidence:
                expected = qa_evidence.inventory(ROOT)
                expected = {p: names for p, names in expected.items() if p == mod + '.py'}
                proof = qa_evidence.coverage(ROOT, evidence, token, expected, True)
                if proof['status'] != 'PASS':
                    rc = rc or 1
                    output += '\nQA DISCOVERY: ' + str(proof) + '\n'
            return mod, rc, output, time.time() - t0
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            out, err = process.communicate()
            return mod, 124, out + err + '\nERROR: The test module exceeded the 1800s time limit\n', time.time() - t0


def main() -> int:
    modules = sorted(f.stem for f in ROOT.glob("test_*.py"))
    if len(sys.argv) > 1:
        modules = [m for m in modules if any(a in m for a in sys.argv[1:])]
    if not modules:
        print('ERROR: no test module matches the filter', file=sys.stderr)
        return 2
    if len(sys.argv) == 1:
        expected = qa_evidence.inventory(ROOT.parent.parent)
        missing = sorted(p for p in expected if p.endswith('.py') and p not in {'engine/tests/' + m + '.py' for m in modules})
        if missing:
            print('ERROR: test files undiscovered by the shared gate: ' + ', '.join(missing), file=sys.stderr)
            return 2
    setting = os.environ.get("CADRE_TEST_WORKER")
    try:
        worker = worker_count(dict(os.environ), os.cpu_count())
    except ValueError:
        print(f'ERROR: CADRE_TEST_WORKER must be a positive integer: {setting!r}', file=sys.stderr)
        return 2
    print(f"worker: {worker} (CADRE_TEST_WORKER={setting or '-'}, cpu={os.cpu_count()})", flush=True)
    base = tempfile.mkdtemp(prefix="qe-parallel-")
    total, skipped_count, bad_modules, durations, t0 = 0, 0, [], [], time.time()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker) as ex:
            for mod, rc, output, duration in ex.map(lambda m: execute(m, base), modules):
                n = re.findall(r"^Ran (\d+) tests?", output, re.M)
                total += int(n[-1]) if n else 0
                skipped = re.findall(r'^OK \(skipped=(\d+)\)', output, re.M)
                skipped_count += int(skipped[-1]) if skipped else 0
                durations.append((duration, mod))
                bad = rc != 0 or not n or int(n[-1]) == 0 or "ResourceWarning:" in output
                if bad:
                    print(output)
                if skipped:
                    print(f'SKIP: {mod}: {skipped[-1]} test')
                if bad:
                    bad_modules.append(mod)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print("slowest modules: " + ", ".join(f"{m} {s:.0f}sec" for s, m in sorted(durations, reverse=True)[:8]))
    print(f"Ran {total} tests in {time.time() - t0:.1f}s")
    print(f'Skipped: {skipped_count}; executed: {total - skipped_count}')
    print("OK" if not bad_modules else f"FAILED (modules: {' '.join(bad_modules)})")
    return 1 if bad_modules else 0


if __name__ == "__main__":
    sys.exit(main())
