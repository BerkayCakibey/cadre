"""Deterministic project skeleton for supported project types with locked dependency lists (N-ENV01, CR-33), plus an install/run/test record.
The skeleton is part of the first commit permission; the file list appears in the approval text, and the tree is verified against the manifest."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

MANIFEST = 'cadre-skeleton.json'
VERSION = 1
SUPPORT = {
    'python': 'Python 3.12+ standard library; no dependencies (requirements.lock empty and locked); '
              'make install (.venv, no download) / make run / make test (unittest)',
}
STEPS = ('install', 'test', 'run')


def sha256(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def package_name(entry_name: str) -> str:
    folded = unicodedata.normalize('NFKD', entry_name.replace('\u0131', 'i')).encode('ascii', 'ignore').decode()
    clean = re.sub(r'[^a-z0-9_]+', '_', folded.lower()).strip('_')
    if not clean or clean[0].isdigit():
        clean = 'application' + ('_' + clean if clean else '')
    return clean


def files(entry_name: str, round: str) -> dict[str, str]:
    if round not in SUPPORT:
        raise ValueError('skeleton-round-unsupported: ' + round)
    package = package_name(entry_name)
    return {
        'README.md': (f'# {entry_name}\n\nCadre `python` skeleton ({SUPPORT["python"]}).\n\n'
                      '| Step | Command | Note |\n| --- | --- | --- |\n'
                      '| Install | `make install` | local `.venv`; no download because `requirements.lock` is empty |\n'
                      f'| Run | `make run` | `python -m {package}` |\n'
                      '| Test | `make test` | `unittest discover -s tests` |\n\n'
                      'A new dependency is added to `requirements.lock` with its hash; `make install` installs only the locked list, '
                      'offline (`--no-index`). Unsupported kinds and steps that need an account/key do not exist here.\n'),
        'pyproject.toml': (f'[project]\nname = "{package}"\nversion = "0.1.0"\nrequires-python = ">=3.12"\ndependencies = []\n\n'
                           '[tool.cadre]\nskeleton = "python"\n'),
        'requirements.lock': '# locked dependency list (pip --require-hashes --no-index); empty = standard library only\n',
        'Makefile': ('PYTHON ?= python3\nVENV := .venv\nPY := $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,$(PYTHON))\n\n'
                     '.PHONY: install run test\n\n'
                     'install:\n\t$(PYTHON) -m venv $(VENV)\n'
                     "\t@if grep -qv '^#' requirements.lock; then $(VENV)/bin/python -m pip install --require-hashes --no-index -r requirements.lock; "
                     'else echo "requirements.lock empty: no download"; fi\n\n'
                     f'run:\n\tPYTHONPATH=src$(if $(PYTHONPATH),:$(PYTHONPATH)) $(PY) -m {package}\n\n'
                     'test:\n\tPYTHONPATH=src$(if $(PYTHONPATH),:$(PYTHONPATH)) $(PY) -m unittest discover -q -s tests\n'),
        '.gitignore': '.venv/\n__pycache__/\n*.pyc\n',
        f'src/{package}/__init__.py': f'"""{entry_name}: Cadre python skeleton."""\n\n__version__ = "0.1.0"\n\n\ndef greet(who: str = "world") -> str:\n    return f"hello {{who}}"\n',
        f'src/{package}/__main__.py': (f'from {package} import __version__, greet\n\n\n'
                                     f'def main() -> int:\n    print(f"{package} {{__version__}}: {{greet()}}")\n    return 0\n\n\n'
                                     'if __name__ == "__main__":\n    raise SystemExit(main())\n'),
        'tests/test_smoke.py': (f'import unittest\n\nfrom {package} import __version__, greet\n\n\n'
                                'class SmokeTests(unittest.TestCase):\n'
                                '    def test_version_and_greeting(self):\n'
                                '        self.assertEqual(__version__, "0.1.0")\n'
                                '        self.assertEqual(greet("cadre"), "hello cadre")\n\n\n'
                                'if __name__ == "__main__":\n    unittest.main()\n'),
    }


def manifest(entry_name: str, round: str) -> dict[str, Any]:
    content = files(entry_name, round)
    return {'version': VERSION, 'round': round, 'entry_name': entry_name, 'package': package_name(entry_name), 'support': SUPPORT[round],
            'files': {rel: sha256(text) for rel, text in sorted(content.items())},
            'commands': {step: f'make {step}' for step in STEPS}}


def manifest_text(entry_name: str, round: str) -> str:
    return json.dumps(manifest(entry_name, round), ensure_ascii=False, indent=2, sort_keys=True) + '\n'


def write(path: Path, entry_name: str, round: str) -> list[str]:
    """Writes the files deterministically; writes nothing if an existing file has different content."""
    path = Path(path)
    content = {**files(entry_name, round), MANIFEST: manifest_text(entry_name, round)}
    for rel, text in content.items():
        target = path / rel
        if target.is_symlink() or (target.exists() and (not target.is_file() or target.read_text() != text)):
            raise FileExistsError('skeleton-file-conflicts: ' + rel)
    for rel, text in content.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return sorted(content)


def _manifest_verify(raw: str | bytes) -> dict[str, Any] | None:
    try:
        m = json.loads(raw)
        if (m.get('version') != VERSION or m.get('round') not in SUPPORT or not isinstance(m.get('entry_name'), str)
                or manifest_text(m['entry_name'], m['round']) != (raw.decode() if isinstance(raw, bytes) else raw)):
            return None
        return m
    except (ValueError, TypeError, AttributeError, UnicodeDecodeError):
        return None


def manifest_read(path: Path) -> dict[str, Any] | None:
    target = Path(path) / MANIFEST
    if target.is_symlink() or not target.is_file():
        return None
    try:
        return _manifest_verify(target.read_bytes())
    except OSError:
        return None


def operation_tree_sleeping(path: Path) -> dict[str, Any] | None:
    """Returns the manifest if the working tree outside `.git` consists exactly of the manifest's files and their contents match."""
    path = Path(path)
    m = manifest_read(path)
    if not m:
        return None
    seen: set[str] = set()
    for root, directories, names in os.walk(path):
        directories[:] = [d for d in directories if d != '.git']
        for entry_name in names:
            seen.add((Path(root) / entry_name).relative_to(path).as_posix())
    if seen != set(m['files']) | {MANIFEST}:
        return None
    for rel, digest in m['files'].items():
        target = path / rel
        if target.is_symlink() or not target.is_file() or sha256(target.read_bytes()) != digest:
            return None
    return m


def commit_tree_sleeping(path: Path, head: str) -> dict[str, Any] | None:
    """Returns the manifest if the commit tree consists exactly of manifest + files and the blobs match."""
    def git(*args: str) -> bytes:
        return subprocess.run(['git', '-c', 'core.fsmonitor=false', '-C', str(path), *args], capture_output=True, check=True, timeout=30).stdout
    try:
        names = set(git('ls-tree', '-r', '--name-only', head).decode().splitlines())
        if MANIFEST not in names:
            return None
        m = _manifest_verify(git('show', f'{head}:{MANIFEST}'))
        if not m or names != set(m['files']) | {MANIFEST}:
            return None
        for rel, digest in m['files'].items():
            if sha256(git('show', f'{head}:{rel}')) != digest:
                return None
        return m
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return None


def verify(path: Path, record_directory: Path, moment: float = 300) -> dict[str, Any]:
    """Install/test/run in one record: each step's rc/duration is written to `project_skeleton.json`; running again does not duplicate the product."""
    from run_store import atomic_json
    path, record_directory = Path(path), Path(record_directory)
    m = manifest_read(path)
    outcome: dict[str, Any] = {'version': VERSION, 'ts': time.time(), 'round': m['round'] if m else None, 'entry_name': m['entry_name'] if m else None,
                             'python': sys.version.split()[0], 'steps': [], 'status': 'FAIL'}
    if not m:
        outcome['reason'] = 'no-skeleton-manifest'
        atomic_json(record_directory / 'project_skeleton.json', outcome)
        return outcome
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': os.environ.get('HOME', str(path)), 'PYTHON': sys.executable,
           'LANG': os.environ.get('LANG', 'C.UTF-8'), 'PYTHONDONTWRITEBYTECODE': '1'}
    for step in STEPS:
        start = time.time()
        try:
            r = subprocess.run(['make', '-s', step], cwd=path, capture_output=True, text=True, timeout=moment, env=env)
            rc, output = r.returncode, (r.stdout + r.stderr)[-2000:]
        except (OSError, subprocess.SubprocessError) as exc:
            rc, output = 124 if isinstance(exc, subprocess.TimeoutExpired) else 127, str(exc)[-2000:]
        outcome['steps'].append({'entry_name': step, 'command': m['commands'][step], 'rc': rc, 'sec': round(time.time() - start, 2), 'output': output})
        if rc != 0:
            outcome['reason'] = f'{step}-failed'
            break
    else:
        outcome['status'] = 'PASS'
    atomic_json(record_directory / 'project_skeleton.json', outcome)
    return outcome


def main(argv: list[str]) -> int:
    command = argv[0] if argv else ''
    if command == 'kinds':
        for round, description in SUPPORT.items():
            print(f'{round}: {description}')
        return 0
    if command == 'write' and len(argv) == 4:
        try:
            for rel in write(Path(argv[1]), argv[2], argv[3]):
                print(rel)
        except (OSError, ValueError, FileExistsError) as exc:
            print(f'skeleton could not be written: {exc}', file=sys.stderr)
            return 1
        return 0
    if command == 'verify' and len(argv) == 3:
        outcome = verify(Path(argv[1]), Path(argv[2]))
        for step in outcome['steps']:
            print(f"{step['entry_name']}: rc={step['rc']} {step['sec']} s")
        print(f"skeleton: {outcome['status']}" + (f" ({outcome.get('reason')})" if outcome.get('reason') else ''))
        return 0 if outcome['status'] == 'PASS' else 1
    print('usage: project_skeleton.py kinds | write <folder> <name> <kind> | verify <folder> <record-directory>', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main(sys.argv[1:]))
