#!/usr/bin/env python3
"""Derive a QA command from the repository when the project has none; QA is never silently skipped (P6): derived at acceptance, written to the run record as `qa`.
Derivation order (first match):
  1) Makefile target  check > test > lint            -> `make <target>`
  2) pytest trace (pyproject [tool.pytest] / pytest.ini / tests directory, if pytest is installed) -> `python -m pytest -q -x`
     (`python -m unittest discover -q` if not installed)
  3) package.json scripts.test (if not a placeholder)  -> `npm test --silent`
  4) go.mod -> `go test ./...` · Cargo.toml -> `cargo test -q`
  5) if none, SYNTAX QA (`kind=sozdizim`; the weakest QA, the judge still demands evidence for the acceptance criteria).
CLI: qa_turet.py tara <repo> [code-root] -> JSON {command, kind} · qa_turet.py sozdizim <dir> -> rc 0/1"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

SKIP_DIRS = {'.git', 'node_modules', '.venv', 'venv', '__pycache__', 'dist', 'build', '.uv-cache', '.cache', '.mypy_cache',
             '.pytest_cache', '.ruff_cache', 'target', '.tox', '.cadre-plan'}
PLACEHOLDER_TEST = re.compile(r'''\s*echo\s+\\?["']?Error: no test specified\\?["']?\s*&&\s*exit\s+1\s*''', re.I)


def _python(repo: Path) -> str:
    # B334: QA runs with `bash -lc`; the login shell resets PATH and a bare python3 falls to Xcode 3.9 on macOS
    venv = repo / '.venv/bin/python'
    return str(venv) if venv.is_file() else sys.executable


def turet(repo: Path, engine_root: Path) -> dict[str, str]:
    repo = Path(repo)
    makefile = repo / 'Makefile'
    if makefile.is_file():
        try:
            targets = set(re.findall(r'^([A-Za-z0-9_.-]+):', makefile.read_text(errors='replace'), re.M))
        except OSError:
            targets = set()
        for target in ('check', 'test', 'lint'):
            if target in targets:
                return {'command': f'make {target}', 'kind': 'makefile'}
    pyproject = repo / 'pyproject.toml'
    pytest_izi = (repo / 'pytest.ini').is_file() or (repo / 'tests').is_dir() or (repo / 'test').is_dir()
    if pyproject.is_file():
        try:
            text = pyproject.read_text(errors='replace')
        except OSError:
            text = ''
        pytest_izi = pytest_izi or '[tool.pytest' in text or 'pytest' in text
    if pytest_izi and (list(repo.glob('**/test_*.py')) or list(repo.glob('**/*_test.py'))):
        py = _python(repo)
        try:
            kurulu = subprocess.run([py, '-c', 'import pytest'], capture_output=True, timeout=20).returncode == 0
        except (OSError, subprocess.SubprocessError):
            kurulu = importlib.util.find_spec('pytest') is not None
        if kurulu:
            return {'command': f'{shlex.quote(py)} -m pytest -q -x', 'kind': 'pytest'}
        return {'command': f'{shlex.quote(py)} -m unittest discover -q', 'kind': 'unittest'}
    package = repo / 'package.json'
    if package.is_file():
        try:
            script = str((json.loads(package.read_text()).get('scripts') or {}).get('test') or '')
        except (OSError, ValueError):
            script = ''
        if script and not PLACEHOLDER_TEST.fullmatch(script):
            return {'command': 'npm test --silent', 'kind': 'npm'}
    if (repo / 'go.mod').is_file():
        return {'command': 'go test ./...', 'kind': 'go'}
    if (repo / 'Cargo.toml').is_file():
        return {'command': 'cargo test -q', 'kind': 'cargo'}
    # B325: a nested unittest folder (e.g. engine/tests) must not fall to the syntax check; no CADRE_* leaks, HOME isolated (never writes to the live home)
    sayim: dict[Path, int] = {}
    for p in repo.glob('**/test_*.py'):
        if not SKIP_DIRS & set(p.relative_to(repo).parts):
            sayim[p.parent] = sayim.get(p.parent, 0) + 1
    if sayim:
        dizin = max(sayim, key=lambda d: (sayim[d], -len(d.parts))).relative_to(repo)
        runner = dizin / 'paralel_kos.py'
        if (repo / runner).is_file():
            command = f'{shlex.quote(_python(repo))} {shlex.quote(str(runner))}'
            node_tests = sorted(p.relative_to(repo) for p in (repo / dizin).glob('*.test.mjs'))
            if node_tests:
                command += ' && node --test ' + ' '.join(shlex.quote(str(p)) for p in node_tests)
            return {'command': command, 'kind': 'parallel-unittest-node' if node_tests else 'parallel-unittest'}
        return {'command': f'cd {shlex.quote(str(dizin))} && env -u CADRE_HOME -u CADRE_ENGINE_ROOT HOME="$(mktemp -d)" '
                           f'{shlex.quote(_python(repo))} -m unittest discover -q -p "test_*.py"', 'kind': 'unittest'}
    node_tests = sorted(p.relative_to(repo) for p in repo.glob('**/*.test.mjs')
                        if not SKIP_DIRS & set(p.relative_to(repo).parts))
    if node_tests:
        return {'command': 'node --test ' + ' '.join(shlex.quote(str(p)) for p in node_tests), 'kind': 'node'}
    return {'command': f'{shlex.quote(sys.executable)} {shlex.quote(str(Path(engine_root) / "lib/qa_turet.py"))} sozdizim .', 'kind': 'sozdizim'}


def _dosyalar(root: Path):
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')]
        for name in files:
            path = Path(directory) / name
            if path.is_symlink():
                continue
            yield path


def sozdizim(root: Path) -> int:
    """Syntax QA: every .py is checked with ast, .sh with `bash -n`, .json/.toml by loading; no bytecode is written."""
    bozuk = []
    sayilar = {'py': 0, 'sh': 0, 'json': 0, 'toml': 0}
    for path in _dosyalar(Path(root)):
        suffix = path.suffix.lower()
        try:
            if suffix == '.py':
                sayilar['py'] += 1
                ast.parse(path.read_text(errors='replace'), filename=str(path))
            elif suffix in ('.sh', '.bash'):
                sayilar['sh'] += 1
                r = subprocess.run(['bash', '-n', str(path)], capture_output=True, text=True, timeout=30)
                if r.returncode:
                    raise SyntaxError(r.stderr.strip()[:200])
            elif suffix == '.json':
                sayilar['json'] += 1
                json.loads(path.read_text(errors='replace'))
            elif suffix == '.toml':
                sayilar['toml'] += 1
                import tomllib
                tomllib.loads(path.read_text(errors='replace'))
        except (SyntaxError, ValueError, UnicodeDecodeError) as error:
            bozuk.append(f'{path}: {type(error).__name__}: {str(error)[:160]}')
        except (OSError, subprocess.SubprocessError) as error:
            bozuk.append(f'{path}: {type(error).__name__}')
    print(f"sozdizim QA: {sayilar} · bozuk={len(bozuk)}")
    for line in bozuk:
        print(line, file=sys.stderr)
    return 1 if bozuk else 0


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == 'tara':
        root = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(__file__).resolve().parents[1]
        print(json.dumps(turet(Path(sys.argv[2]), root)))
        return 0
    if len(sys.argv) >= 3 and sys.argv[1] == 'sozdizim':
        return sozdizim(Path(sys.argv[2]))
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
