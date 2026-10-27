"""QA evidence observer: which tests were collected and executed, product binding, quality-tool verification."""
from __future__ import annotations

import ast
import fcntl
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
from typing import Any

LIMIT = 8_000_000
SKIP = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', 'dist', 'build', 'target', '.tox', '.cache'}
PROTECTED = re.compile(r'^(secrets?|credentials.*|id_rsa.*|id_ed25519.*)$', re.I)
EVIDENCE = os.environ.get('CADRE_QA_EVIDENCE')
TOKEN = os.environ.get('CADRE_QA_TOKEN')
URUN = os.environ.get('CADRE_QA_URUN')
_urun_yayildi = False


def validate_profile(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if (not isinstance(value, dict) or not {'version', 'scope', 'deferred'} <= set(value)
            or set(value) - {'version', 'scope', 'deferred', 'excluded'}
            or type(value['version']) is not int or value['version'] != 1 or value['scope'] != 'local'
            or any(not isinstance(value.get(key, []), list) or len(value.get(key, [])) > 20 for key in ('deferred', 'excluded'))
            or not value['deferred'] and not value.get('excluded')):
        raise ValueError('qa-profile-invalid')
    names = set()
    for item in [*value['deferred'], *value.get('excluded', [])]:
        if not isinstance(item, dict) or set(item) != {'path', 'sha256', 'reason'}:
            raise ValueError('qa-profile-invalid')
        name, sha, reason = item['path'], item['sha256'], item['reason']
        if (not isinstance(name, str) or not name or len(name) > 500 or '\0' in name or '\\' in name or Path(name).is_absolute()
                or any(part in ('..', '.', '') or part.startswith('.') for part in name.split('/'))
                or not re.fullmatch(r'test_.*\.py|.*_test\.py|.*\.(test|spec)\.(m?js|cjs|tsx?|jsx)', Path(name).name)
                or name in names or not isinstance(sha, str) or not re.fullmatch(r'[a-f0-9]{64}', sha)
                or not isinstance(reason, str) or not reason.strip() or len(reason) > 1000):
            raise ValueError('qa-profile-invalid')
        names.add(name)
    return {'version': 1, 'scope': 'local', **{key: [dict(item) for item in value[key]]
                                            for key in ('deferred', 'excluded') if key in value}}


def project_profile(home: Path, project: str) -> dict[str, Any] | None:
    from runtime_sync import regular
    raw = regular(home.resolve() / 'projects' / project / 'qa-profile.json')
    if raw is None:
        return None
    try:
        value = json.loads(raw) if len(raw) <= 64_000 else False
        if value is None:
            raise ValueError('qa-profile-invalid')
        return validate_profile(value)
    except (ValueError, UnicodeError):
        raise ValueError('qa-profile-invalid') from None


def profile_sources(root: Path, profile: dict[str, Any] | None, section: str = 'deferred') -> dict[str, str]:
    checked = validate_profile(profile)
    if not checked or not checked.get(section):
        return {}
    from runtime_sync import regular
    root = root.resolve()
    result = {}
    for item in (checked or {}).get(section, []):
        path = root / item['path']
        try:
            raw = regular(path)
            if raw is None or len(raw) > 1_000_000 or hashlib.sha256(raw).hexdigest() != item['sha256']:
                raise ValueError('qa-profile-source-changed')
        except (OSError, ValueError):
            raise ValueError('qa-profile-source-changed') from None
        result[item['path']] = item['reason']
    return result


def emit(event: dict[str, Any]) -> None:
    target = EVIDENCE
    if not target:
        return
    event = {**event, 'pid': os.getpid(), 'token': TOKEN}
    fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if os.fstat(fd).st_size >= LIMIT:
            raise ValueError('qa-evidence-limit')
        os.write(fd, (json.dumps(event, ensure_ascii=False) + '\n').encode())
    finally:
        os.close(fd)


def test_info(test: Any) -> dict[str, str]:
    method = getattr(test, '_testFunc', None) or getattr(test, getattr(test, '_testMethodName', ''), None)
    path = inspect.getsourcefile(method or type(test))
    identifier = test.id()
    if getattr(test, '_testFunc', None):
        identifier = method.__module__ + '.' + identifier
    return {'id': identifier, 'name': getattr(method, '__qualname__', ''), 'path': str(Path(path).resolve()) if path else ''}


def install_unittest() -> None:
    import unittest
    if getattr(unittest, '_cadre_observed', False):
        return
    unittest._cadre_observed = True
    original = unittest.TestSuite.run
    def suite_run(self: Any, result: Any, debug: bool = False) -> Any:
        os.environ.pop('CADRE_QA_EVIDENCE', None)
        os.environ.pop('CADRE_QA_TOKEN', None)
        os.environ.pop('CADRE_QA_URUN', None)
        for item in self:
            if isinstance(item, unittest.TestCase):
                emit({'event': 'collect', 'runner': 'unittest', **test_info(item)})
        emit_products('unittest')
        return original(self, result, debug)
    unittest.TestSuite.run = suite_run
    for name, status in [('addSuccess', 'PASS'), ('addSkip', 'SKIP'), ('addFailure', 'FAIL'),
                         ('addError', 'ERROR'), ('addExpectedFailure', 'SKIP'), ('addUnexpectedSuccess', 'FAIL')]:
        old = getattr(unittest.TestResult, name)
        def observed(self: Any, test: Any, *args: Any, _old: Any = old, _status: str = status) -> Any:
            emit({'event': 'result', 'runner': 'unittest', 'status': _status, **test_info(test)})
            return _old(self, test, *args)
        setattr(unittest.TestResult, name, observed)
    old_subtest = unittest.TestResult.addSubTest
    def subtest(self: Any, test: Any, sub: Any, error: Any) -> Any:
        if error is not None:
            emit({'event': 'result', 'runner': 'unittest', 'status': 'FAIL', **test_info(test)})
        return old_subtest(self, test, sub, error)
    unittest.TestResult.addSubTest = subtest


def pytest_collection_finish(session: Any) -> None:
    for item in session.items:
        emit({'event': 'collect', 'runner': 'pytest', 'id': item.nodeid,
              'name': getattr(getattr(item, 'obj', None), '__qualname__', None) or getattr(item, 'originalname', None) or item.name.split('[')[0],
              'path': str(Path(item.path).resolve())})
    emit_products('pytest')


def pytest_runtest_logreport(report: Any) -> None:
    if report.failed or report.skipped or report.when == 'call':
        emit({'event': 'result', 'runner': 'pytest', 'id': report.nodeid,
              'status': 'SKIP' if report.skipped or hasattr(report, 'wasxfail') else 'FAIL' if report.failed else 'PASS'})


def pytest_runtestloop(session: Any) -> None:
    os.environ.pop('CADRE_QA_EVIDENCE', None)
    os.environ.pop('CADRE_QA_TOKEN', None)
    os.environ.pop('CADRE_QA_URUN', None)


def emit_products(runner: str) -> None:
    """CR-12 (N-QA01/N-ENV03): the test process writes to the evidence where the expected product modules were really loaded from; the engine
    does not accept a product loaded from outside the worktree (an old copy, site-packages)."""
    global _urun_yayildi
    if _urun_yayildi or not URUN:
        return
    _urun_yayildi = True
    try:
        expected = json.loads(URUN)
    except ValueError:
        return
    import importlib.util
    for name in expected if isinstance(expected, dict) else []:
        module = sys.modules.get(name)
        path = getattr(module, '__file__', None) if module is not None else None
        if path is None:
            try:
                spec = importlib.util.find_spec(name)
                path = spec.origin if spec is not None and spec.has_location else None
            except (ImportError, ValueError, AttributeError):
                path = None
        emit({'event': 'product', 'runner': runner, 'id': name, 'path': str(Path(path).resolve()) if path else '',
              'loaded': module is not None})


def _paket_yolu(root: Path, name: str) -> tuple[str, Path, Path] | None:
    """(module name, resolved path, import root) of a .py file inside a package; plain modules outside a package, missing files and
    symlinks are not derived — no blind `src` assumption, only the file's real package layout."""
    path = root / name
    if not name.endswith('.py') or path.is_symlink() or not path.is_file():
        return None
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or not (resolved.parent / '__init__.py').is_file():
        return None
    parts = [] if resolved.name == '__init__.py' else [resolved.stem]
    directory = resolved.parent
    while directory != root and (directory / '__init__.py').is_file():
        parts.insert(0, directory.name)
        directory = directory.parent
    return '.'.join(parts), resolved, directory


def product_modules(root: Path, files: list[str]) -> dict[str, str]:
    root = root.resolve()
    found = {}
    for name in sorted(set(files)):
        item = _paket_yolu(root, name)
        if item is not None:
            found[item[0]] = str(item[1])
    return found


def product_roots(root: Path, files: list[str]) -> list[str]:
    root = root.resolve()
    roots = {str(item[2]) for name in set(files) if (item := _paket_yolu(root, name)) is not None and item[2] != root}
    return sorted(roots)


def command_interpreter(command: str) -> str | None:
    for piece in split_commands(command):
        try:
            args = shlex.split(piece)
        except ValueError:
            continue
        if args and Path(args[0]).is_absolute() and re.fullmatch(r'python[0-9.]*', Path(args[0]).name):
            return args[0]
    return None


def bind_roots(env: dict[str, str], roots: list[str]) -> dict[str, str]:
    if not roots:
        return env
    return {**env, 'PYTHONPATH': os.pathsep.join([*roots, *([env['PYTHONPATH']] if env.get('PYTHONPATH') else [])])}


def product_result(root: Path, evidence: Path, token: str, expected: dict[str, str], roots: list[str]) -> dict[str, Any]:
    common: dict[str, Any] = {'binding': list(roots), 'modules': {}}
    if not expected:
        return {**common, 'status': 'NOT_APPLICABLE', 'reason': 'qa-product-not-derived'}
    try:
        events = [e for e in read_events(evidence, token) if e['event'] == 'product']
    except (OSError, ValueError, KeyError, TypeError):
        return {**common, 'status': 'UNKNOWN', 'reason': 'qa-product-unverified'}
    root = root.resolve()
    for name, path in expected.items():
        seen = sorted({e['path'] for e in events if e['id'] == name})
        if not seen:
            common['modules'][name] = {'expected': path, 'path': None, 'status': 'UNVERIFIED'}
        elif '' in seen:
            common['modules'][name] = {'expected': path, 'path': seen[0] if len(seen) == 1 else seen, 'status': 'UNRESOLVED'}
        else:
            ok = all(str(Path(p).resolve()) == path for p in seen)
            common['modules'][name] = {'expected': path, 'path': seen[0] if len(seen) == 1 else seen, 'status': 'PASS' if ok else 'MISMATCH'}
    states = {m['status'] for m in common['modules'].values()}
    if 'MISMATCH' in states:
        return {**common, 'status': 'FAIL', 'reason': 'qa-product-mismatch'}
    if states - {'PASS'}:
        return {**common, 'status': 'UNKNOWN', 'reason': 'qa-product-unverified'}
    return {**common, 'status': 'PASS', 'reason': 'qa-product-verified'}


def inventory(root: Path) -> dict[str, list[str]]:
    root = root.resolve()
    for filename in ('pyproject.toml', 'pytest.ini', '.pytest.ini', 'tox.ini', 'setup.cfg'):
        config = root / filename
        if config.is_file() and re.search(r'(?m)^\s*(python_files|python_functions|python_classes)\s*=', read_artifact(config, 100_000).decode()):
            raise ValueError('qa-test-naming-unverified')
    found: dict[str, list[str]] = {}
    size = 0
    for index, (directory, dirs, files) in enumerate(os.walk(root, followlinks=False)):
        if index > 10_000:
            raise ValueError('qa-test-source-limit')
        dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith('.') and not PROTECTED.match(d) and not (Path(directory) / d).is_symlink()]
        for name in files:
            if not re.match(r'(test_.*\.py|.*_test\.py|.*\.(test|spec)\.(m?js|cjs|tsx?|jsx))$', name):
                continue
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
                raise ValueError('qa-test-source-unsafe')
            relative = path.relative_to(root).as_posix()
            if path.suffix != '.py':
                found[relative] = []
                continue
            raw = read_artifact(path, 1_000_000)
            size += len(raw)
            if size > LIMIT:
                raise ValueError('qa-test-source-limit')
            tree = ast.parse(raw, filename=relative)
            names = [(parent.name + '.' if isinstance(parent, ast.ClassDef) else '') + n.name
                     for parent in [tree, *(n for n in tree.body if isinstance(n, ast.ClassDef))]
                     for n in parent.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith('test')]
            if names:
                found[relative] = names
            if len(found) > 5000:
                raise ValueError('qa-test-source-limit')
    return found


def environment(base: dict[str, str], evidence: Path, token: str, products: dict[str, str] | None = None,
                roots: list[str] | None = None) -> dict[str, str]:
    """FIX-52: only the `qa-sim` directory enters PYTHONPATH; the engine library cannot be imported by the project's tests.
    CR-12: product roots (from the package layout in the worktree) come after the shim and before the caller's PYTHONPATH."""
    shim = str(Path(__file__).resolve().parent / 'qa-sim')
    env = {**base, 'CADRE_QA_EVIDENCE': str(evidence), 'CADRE_QA_TOKEN': token,
           'PYTHONPATH': os.pathsep.join([shim, *(roots or [])]) + os.pathsep + base.get('PYTHONPATH', ''),
           'PYTEST_PLUGINS': ','.join(filter(None, [base.get('PYTEST_PLUGINS'), 'qa_kaniti']))}
    if products:
        env['CADRE_QA_URUN'] = json.dumps(products, sort_keys=True)
    return env


def read_artifact(path: Path, limit: int = LIMIT) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('qa-evidence-unsafe')
        raw = os.read(fd, limit + 1)
        after = os.fstat(fd)
        if len(raw) != info.st_size or (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('qa-evidence-changed')
    finally:
        os.close(fd)
    if len(raw) > limit:
        raise ValueError('qa-evidence-limit')
    return raw


def read_events(path: Path, token: str) -> list[dict[str, Any]]:
    events = [json.loads(line) for line in read_artifact(path).splitlines()]
    if any(not isinstance(e, dict) or e.get('token') != token or e.get('event') not in ('collect', 'result', 'product')
           or e.get('runner') not in ('unittest', 'pytest', 'node') or type(e.get('pid')) is not int or e['pid'] <= 0
           or not isinstance(e.get('id'), str) or not 1 <= len(e['id']) <= 10_000
           or (e['event'] == 'result' and e.get('status') not in ('PASS', 'FAIL', 'ERROR', 'SKIP'))
           or (e['event'] == 'product' and (not isinstance(e.get('path'), str) or type(e.get('loaded')) is not bool)) for e in events):
        raise ValueError('qa-evidence-invalid')
    return events


def coverage(root: Path, evidence: Path, token: str, expected: dict[str, list[str]], scoped: bool,
             profile: dict[str, Any] | None = None) -> dict[str, Any]:
    common: dict[str, Any] = {'scope': 'targeted' if scoped else 'full', 'expected_files': sorted(expected)}
    try:
        profile = validate_profile(profile)
        excluded = profile_sources(root, profile, 'excluded')
        if excluded:
            common.update(inventory_files=sorted(expected), excluded_files=excluded)
            expected = {path: names for path, names in expected.items() if path not in excluded}
            common['expected_files'] = sorted(expected)
        events = read_events(evidence, token)
        common['evidence_sha256'] = hashlib.sha256(read_artifact(evidence)).hexdigest()
        collected: dict[tuple[int, str], dict[str, Any]] = {}
        results: dict[tuple[int, str], set[str]] = {}
        for event in events:
            key = (event['pid'], event['id'])
            if event['event'] == 'collect':
                if key in collected and any(event.get(k) != collected[key].get(k) for k in ('path', 'name', 'runner')):
                    return {**common, 'status': 'UNKNOWN', 'reason': 'qa-discovery-conflict'}
                collected[key] = event
            elif event['event'] == 'result':
                results.setdefault(key, set()).add(event['status'])
        common.update(collected=len(collected), executed=len(results),
                      skipped=sum('SKIP' in statuses for statuses in results.values()),
                      failed=sorted({key[1] for key, statuses in results.items() if statuses & {'FAIL', 'ERROR'}}),
                      passed=sorted({key[1] for key, statuses in results.items() if statuses == {'PASS'}}))
        if not collected or not results:
            return {**common, 'status': 'UNKNOWN', 'reason': 'qa-discovery-unverified'}
        if set(collected) != set(results):
            return {**common, 'status': 'FAIL', 'reason': 'qa-execution-incomplete'}
        if any(s & {'FAIL', 'ERROR'} for s in results.values()):
            return {**common, 'status': 'FAIL', 'reason': 'qa-test-failed'}
        if any(Path(event['path']).resolve().is_relative_to(root.resolve())
               and Path(event['path']).resolve().relative_to(root.resolve()).as_posix() in excluded for event in collected.values()):
            return {**common, 'status': 'FAIL', 'reason': 'qa-out-of-scope-tests-executed'}
        deferred = profile_sources(root, profile) if common['skipped'] and profile is not None else {}
        allowed_skips = []
        for key, statuses in results.items():
            if 'SKIP' not in statuses:
                continue
            path = Path(collected[key]['path']).resolve()
            if path.is_relative_to(root.resolve()) and path.relative_to(root.resolve()).as_posix() in deferred:
                allowed_skips.append(key)
        common.update(required_skipped=common['skipped'] - len(allowed_skips), deferred=len(allowed_skips),
                      deferred_files=deferred, acceptance_scope='local' if profile else 'full')
        if common['required_skipped']:
            return {**common, 'status': 'FAIL', 'reason': 'qa-required-test-skipped'}
        if not any(statuses == {'PASS'} for statuses in results.values()):
            return {**common, 'status': 'FAIL', 'reason': 'qa-required-tests-empty'}
        observed: dict[str, set[str]] = {}
        for event in collected.values():
            path = Path(event['path']).resolve()
            if path.is_relative_to(root.resolve()):
                observed.setdefault(path.relative_to(root.resolve()).as_posix(), set()).add(event['name'])
        missing = [path for path, names in expected.items() if path not in observed or set(names) - observed[path]]
        if missing:
            return {**common, 'status': 'FAIL', 'reason': 'qa-tests-not-discovered', 'missing_files': missing}
        return {**common, 'status': 'PASS', 'reason': 'qa-discovery-verified', 'observed_files': sorted(observed)}
    except (OSError, ValueError, KeyError, TypeError, SyntaxError) as error:
        reason = str(error) if str(error) in ('qa-profile-invalid', 'qa-profile-source-changed') else 'qa-discovery-unverified'
        return {**common, 'status': 'UNKNOWN', 'reason': reason}


def quality_requirements(root: Path) -> set[str]:
    required: set[str] = set()
    pyproject = root / 'pyproject.toml'
    if pyproject.is_file() and not pyproject.is_symlink():
        import tomllib
        data = tomllib.loads(read_artifact(pyproject, 100_000).decode())
        tools = data.get('tool', {})
        if 'ruff' in tools:
            required.add('lint')
            if 'format' in tools['ruff']:
                required.add('format')
        if any(t in tools for t in ('mypy', 'pyright', 'basedpyright')):
            required.add('type')
    for filename in ('ruff.toml', '.ruff.toml'):
        path = root / filename
        if path.is_file():
            import tomllib
            required.add('lint')
            if 'format' in tomllib.loads(read_artifact(path, 100_000).decode()):
                required.add('format')
    if any((root / p).is_file() for p in ('mypy.ini', '.mypy.ini', 'pyrightconfig.json')):
        required.add('type')
    package = root / 'package.json'
    if package.is_file():
        scripts = json.loads(read_artifact(package, 100_000)).get('scripts', {})
        for kind, names in [('lint', ('lint',)), ('type', ('typecheck', 'type-check', 'check-types')), ('format', ('format:check', 'check-format'))]:
            if any(scripts.get(name) for name in names):
                required.add(kind)
    return required


def split_commands(command: str) -> list[str]:
    parts, start, quote, escaped, depth = [], 0, '', False, 0
    for index, char in enumerate(command):
        if escaped:
            escaped = False
            continue
        if char == '\\' and quote != "'":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = ''
            continue
        if char in "\"'":
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        elif char == '&' and command[index:index + 2] == '&&' and not depth:
            parts.append(command[start:index].strip())
            start = index + 2
    parts.append(command[start:].strip())
    return parts


def test_scope(command: str, root: Path, inventory: dict[str, list[str]]) -> set[str] | None:
    root = root.resolve()
    named: set[str] = set()
    for piece in split_commands(command):
        if any(c in piece for c in '$`|;<>\n()'):
            continue
        try:
            args = shlex.split(piece)
        except ValueError:
            continue
        if not args:
            continue
        if '-m' in args[1:]:
            index = args.index('-m', 1)
            tool, rest = (args[index + 1] if index + 1 < len(args) else ''), args[index + 2:]
        else:
            tool, rest = Path(args[0]).name, args[1:]
        if tool not in ('pytest', 'node', 'unittest'):
            continue
        piece_paths: set[str] = set()
        for token in rest if tool != 'unittest' else []:
            if token.startswith('-') or not token or any(c in token for c in '*?['):
                continue
            path = (root / token).resolve()
            if not path.is_relative_to(root) or path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            if path.is_file() and relative in inventory:
                piece_paths.add(relative)
            elif path.is_dir():
                piece_paths.update(p for p in inventory if p == relative or p.startswith(relative + '/'))
        if not piece_paths:
            return None
        named.update(piece_paths)
    return named or None


def quality_command(command: str, root: Path, evidence: Path, changed: list[str]) -> tuple[str, dict[str, Any]]:
    required = quality_requirements(root)
    checks: dict[str, Any] = {}
    pieces = split_commands(command)
    for i, piece in enumerate(pieces):
        if any(c in piece for c in '$`|;<>\n()'):
            continue
        args = shlex.split(piece)
        offset = 3 if len(args) > 3 and args[1] == '-m' else 1
        tool = args[2] if offset == 3 else Path(args[0]).name if args else ''
        tail = args[offset:]
        kind = ('lint' if tool == 'ruff' and tail[:1] == ['check'] else
                'format' if tool == 'ruff' and tail[:2] == ['format', '--check'] else
                'type' if tool in ('mypy', 'pyright', 'basedpyright') else '')
        if not kind:
            continue
        paths = tail[1:] if kind == 'lint' else tail[2:] if kind == 'format' else tail
        options: list[str] = []
        if tool == 'basedpyright' and '--venvpath' in paths:
            index = paths.index('--venvpath')
            if index + 1 < len(paths) and paths[index + 1] and not paths[index + 1].startswith('-'):
                options = paths[index:index + 2]
                paths = paths[:index] + paths[index + 2:]
        scopes = paths or ['.']
        supported = not any(p.startswith('-') for p in scopes) and (bool(paths) or kind != 'type')
        targets = [(root / p).resolve() for p in changed if p.endswith('.py') and (root / p).is_file()]
        if any(not p.is_relative_to(root.resolve()) for p in targets):
            raise ValueError('qa-quality-source-unsafe')
        prefix = offset + (1 if kind == 'lint' else 2 if kind == 'format' else 0)
        scope_command = [*args[:prefix], *options, *map(str, targets)] if targets and supported else []
        checks[kind] = {'command': piece, 'paths': scopes, 'supported': supported,
                        'scope_command': scope_command,
                        'output': str(evidence.with_name(evidence.name + '-' + kind))}
        pieces[i] = shlex.join([sys.executable, str(Path(__file__).resolve()), 'quality', checks[kind]['output'], json.dumps(scope_command), *args])
    return ' && '.join(pieces), {'required': sorted(required), 'checks': checks}


def node_command(command: str) -> str:
    reporter = str(Path(__file__).with_name('qa-node-reporter.cjs'))
    pieces = split_commands(command)
    for index, piece in enumerate(pieces):
        if any(c in piece for c in '$`|;<>\n()'):
            continue
        args = shlex.split(piece)
        if len(args) < 2 or Path(args[0]).name != 'node' or args[1] != '--test' or any('--test-reporter' in a for a in args):
            continue
        pieces[index] = shlex.join([*args[:2], '--test-reporter=tap', '--test-reporter-destination=stdout',
                                   '--test-reporter=' + reporter, '--test-reporter-destination=stdout', *args[2:]])
    return ' && '.join(pieces)


def quality_filters_verified(root: Path, check: dict[str, Any], changed: list[str]) -> bool:
    import tomllib
    args = shlex.split(check['command'])
    tool = args[2] if len(args) > 2 and args[1] == '-m' else Path(args[0]).name
    for filename in ('pyproject.toml', 'ruff.toml', '.ruff.toml', 'mypy.ini', '.mypy.ini', 'pyrightconfig.json'):
        path = root / filename
        if not path.is_file():
            continue
        raw = read_artifact(path, 100_000).decode()
        try:
            if filename == 'pyproject.toml':
                settings = tomllib.loads(raw).get('tool', {}).get(tool, {})
            elif filename in ('ruff.toml', '.ruff.toml'):
                settings = tomllib.loads(raw) if tool == 'ruff' else {}
            elif filename == 'pyrightconfig.json':
                settings = json.loads(raw) if tool in ('pyright', 'basedpyright') else {}
            else:
                if tool == 'mypy' and re.search(r'(?m)^\s*(exclude|files)\s*=', raw):
                    return False
                continue
            if not isinstance(settings, dict):
                return False
            filters = ('exclude', 'extend-exclude', 'force-exclude', 'files', 'include')
            if tool == 'ruff':
                for section in ('lint', 'format'):
                    nested = settings.get(section, {})
                    if not isinstance(nested, dict) or any(nested.get(key) not in (None, [], False) for key in filters):
                        return False
            for key in filters:
                value = settings.get(key)
                if value in (None, [], False):
                    continue
                if key == 'include' and tool in ('pyright', 'basedpyright'):
                    if (not isinstance(value, list) or any(not isinstance(v, str) or not v or re.search(r'[*?\[\]{}!]', v) for v in value)
                            or any(not any((root / scope).resolve().is_relative_to((root / v).resolve()) for v in value) for scope in check['paths'])):
                        return False
                elif key in ('exclude', 'extend-exclude') and tool in ('pyright', 'basedpyright'):
                    if (not isinstance(value, list) or any(v not in ('**/__pycache__', '**/__pycache__/**', '__pycache__') for v in value)
                            or any('__pycache__' in Path(name).parts for name in changed)):
                        return False
                else:
                    return False
        except (OSError, ValueError, TypeError, KeyError):
            return False
    return True


def quality_result(root: Path, planned: dict[str, Any], changed: list[str]) -> dict[str, Any]:
    unknown_reason = ''
    for kind in planned['required']:
        check = planned['checks'].get(kind)
        if not check or not check['supported']:
            unknown_reason = 'qa-quality-unverified'
            continue
        try:
            raw = read_artifact(Path(check['output']), 100_000)
            data = json.loads(raw)
            check['sha256'] = hashlib.sha256(raw).hexdigest()
            if data['exit_code'] != 0 or shlex.split(data['command']) != shlex.split(check['command']) or data['scope_command'] != check['scope_command']:
                return {**planned, 'status': 'FAIL', 'reason': 'qa-quality-failed'}
            if check['scope_command'] and data['scope_exit_code'] != 0:
                return {**planned, 'status': 'FAIL', 'reason': 'qa-quality-scope-failed'}
        except (OSError, ValueError, KeyError):
            unknown_reason = 'qa-quality-unverified'
            continue
        for name in changed:
            if name.endswith('.py') and (root / name).is_file():
                if not any((root / name).resolve().is_relative_to((root / p).resolve()) for p in check['paths']):
                    return {**planned, 'status': 'FAIL', 'reason': 'qa-quality-scope-missing', 'missing_file': name}
        if not quality_filters_verified(root, check, changed):
            unknown_reason = 'qa-quality-exclusions-unverified'
    if unknown_reason:
        return {**planned, 'status': 'UNKNOWN', 'reason': unknown_reason}
    return {**planned, 'status': 'PASS' if planned['required'] else 'NOT_APPLICABLE',
            'reason': 'qa-quality-verified' if planned['required'] else 'qa-quality-not-configured'}


def main() -> int:
    target, scope_command, args = Path(sys.argv[2]), json.loads(sys.argv[3]), sys.argv[4:]
    code = subprocess.run(args).returncode
    scope_code = subprocess.run(scope_command).returncode if code == 0 and scope_command else None
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({'command': shlex.join(args), 'exit_code': code, 'scope_command': scope_command, 'scope_exit_code': scope_code}, stream)
    return code or scope_code or 0


def evidence_valid(qa: dict[str, Any]) -> bool:
    try:
        proof = qa['discovery']
        last = qa['attempts'][-1]
        if proof['status'] != 'PASS' or hashlib.sha256(read_artifact(Path(last['evidence']))).hexdigest() != proof['evidence_sha256']:
            return False
        for kind in qa['quality']['required']:
            check = qa['quality']['checks'][kind]
            if hashlib.sha256(read_artifact(Path(check['output']), 100_000)).hexdigest() != check['sha256']:
                return False
        return qa['quality']['status'] in ('PASS', 'NOT_APPLICABLE')
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return False


if __name__ == '__main__':
    raise SystemExit(main())
