"""Executor identity written durably before start; sandbox profile and readable roots; output reconciliation before every retry.
No background watcher; raw stdout is a file, so the executor never blocks on an unread Python pipe."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import shutil
import shlex
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from typing import Any, Iterator, TYPE_CHECKING
from urllib.parse import unquote, urlsplit
import uuid


if TYPE_CHECKING or __name__ != '__main__' or sys.argv[1:2] not in (['scope-check'], ['manual-check'], ['nested-check']):
    import opencode_adaptor as OC
    from opencode_adaptor import events, session_rows
    from sonuc import PROTECTED_SOURCE
    from kosu import FileLock, RunStore, atomic_json, atomic_write, bounded_json, identifier, launch_guard, revision_context, process_identity, run_job_type, health_json, health_file


def sandbox_icinde(pid: int) -> bool:
    """FIX-26 (N-ENV02): sandbox_check(pid, NULL, 0) returns non-zero for a process inside a sandbox."""
    import ctypes
    library = ctypes.CDLL('/usr/lib/libsandbox.dylib', use_errno=True)
    check = library.sandbox_check
    check.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int)
    check.restype = ctypes.c_int
    return check(pid, None, 0) != 0


def sandbox_scope_decisions(pid: int, markers: list[Path], *, probe: tuple[Any, int] | None = None) -> list[int]:
    import ctypes
    try:
        if probe is None:
            library = ctypes.CDLL('/usr/lib/libsandbox.dylib', use_errno=True)
            check = library.sandbox_check
            check.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int)
            check.restype = ctypes.c_int
            flags = 1 | ctypes.c_int.in_dll(library, 'SANDBOX_CHECK_NO_REPORT').value
        else:
            check, flags = probe
        sandboxed = check(pid, None, 0)
        if sandboxed == 0:
            return []
        values = [check(pid, b'file-read-data', flags, ctypes.c_char_p(str(marker).encode())) for marker in markers]
        if sandboxed != 1 or any(value not in (0, 1) for value in values):
            raise PermissionError('project-tool-process-scope-unavailable')
        return values
    except (OSError, AttributeError, ValueError) as error:
        raise PermissionError('project-tool-process-scope-unavailable') from error


if __name__ == '__main__' and sys.argv[1:2] == ['manual-check']:
    from sonuc import PROTECTED_SOURCE
    try:
        root = Path(sys.argv[2]).resolve(strict=True)
        base = Path(sys.argv[3]).resolve(strict=True)
        tool = sys.argv[4]
        args = json.load(sys.stdin)
        if not (root / '.git').exists() or root == Path.home() or not base.is_relative_to(root):
            raise PermissionError('manual-project-required')
        if tool not in ('read', 'edit', 'write', 'grep', 'glob', 'list'):
            raise PermissionError('manual-tool-denied')
        raw = Path(args.get('filePath') or args.get('path') or str(root))
        path = (raw if raw.is_absolute() else base / raw)
        requested_root = Path(sys.argv[2]).absolute()
        if path.is_relative_to(requested_root):
            path = root / path.relative_to(requested_root)
        canonical = path.resolve()
        template = tool == 'read' and path.name in ('.env.example', '.env.sample', '.env.template') and canonical.name == path.name
        protected_path = path.parent if template else path
        protected_canonical = canonical.parent if template else canonical
        sensitive = {'.git', '.opencode', 'node_modules', 'site-packages', 'opencode.json', 'opencode.jsonc'}
        if (not canonical.is_relative_to(root) or PROTECTED_SOURCE.search(str(protected_path))
                or PROTECTED_SOURCE.search(str(protected_canonical))
                or sensitive.intersection(path.relative_to(root).parts)
                or sensitive.intersection(canonical.relative_to(root).parts)):
            raise PermissionError('manual-source-denied')
        protected = Path(os.environ.get('CADRE_HOME') or str(Path.home() / '.local/share/cadre-engineering')).resolve()
        if any(canonical.is_relative_to(protected / name) for name in ('runs', 'projects', 'zincir', 'operator-system', 'locks')):
            raise PermissionError('manual-control-denied')
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent.is_relative_to(root)):
            raise PermissionError('manual-link-denied')
        if path.is_file() and path.stat().st_nlink != 1:
            raise PermissionError('manual-link-denied')
        if tool in ('edit', 'write') and canonical.is_relative_to(Path(__file__).resolve().parent.parent):
            raise PermissionError('manual-engine-denied')
        if tool == 'glob':
            pattern = Path(args.get('pattern') or '*')
            if pattern.is_absolute() or '..' in pattern.parts:
                raise PermissionError('manual-glob-denied')
        if tool in ('grep', 'glob', 'list'):
            count = 0
            for directory, dirs, files in os.walk(path, followlinks=False):
                for name in dirs + files:
                    child = Path(directory) / name
                    count += 1
                    if (count > 10_000 or child.is_symlink()
                            or (tool == 'grep' and (PROTECTED_SOURCE.search(str(child)) or name in sensitive))):
                        raise PermissionError('manual-search-denied')
                    if tool == 'grep' and child.is_file() and child.stat().st_nlink != 1:
                        raise PermissionError('manual-search-denied')
        raise SystemExit(0)
    except (OSError, ValueError, TypeError, KeyError):
        raise SystemExit(75)


if __name__ == '__main__' and sys.argv[1:2] == ['nested-check']:
    try:
        raise SystemExit(1 if sys.platform == 'darwin' and sandbox_icinde(os.getpid()) else 0)
    except (OSError, AttributeError, ValueError):
        raise SystemExit(2)

if __name__ == '__main__' and sys.argv[1:2] == ['scope-check']:
    try:
        markers = [Path(name) for name in sys.argv[2:]]
        if len(markers) != 2 or sandbox_scope_decisions(os.getpid(), markers) != [0, 1]:
            raise SystemExit(75)
        import ctypes
        library = ctypes.CDLL('/usr/lib/libsandbox.dylib')
        library.sandbox_init.argtypes = (ctypes.c_char_p, ctypes.c_uint64, ctypes.POINTER(ctypes.c_char_p))
        error = ctypes.c_char_p()
        result = library.sandbox_init(b'(version 1)(deny default)', 0, ctypes.byref(error))
        if error.value is not None:
            library.sandbox_free_error.argtypes = (ctypes.c_char_p,)
            library.sandbox_free_error(error)
        raise SystemExit(0 if result != 0 and sandbox_scope_decisions(os.getpid(), markers) == [0, 1] else 75)
    except (OSError, AttributeError, ValueError):
        raise SystemExit(75)


CONTINUE = "Kaldigin yerden devam et. Kayitli gorev ve kabul kriterlerini koru; yalniz bitmemis adimi yap."
YENI_KAPSAM_ROLLERI = ("kalite", "qa", "otopsi")   # G-A4: only worker (impl) and plan attempts take over a session


INSTRUCTION_PATHS = ('AGENTS.md', 'agents/build.md', 'agents/cadre-impl.md', 'agents/cadre-plan.md',
                     'agents/cadre-review.md', 'command/plan.md', 'command/review.md', 'command/yardim.md',
                     'skills/self-check/SKILL.md', 'skills/rapor-kaniti/SKILL.md')


def instruction_env(record: dict[str, Any] | None, env: dict[str, str]) -> dict[str, str]:
    provenance = record.get('provenance') if isinstance(record, dict) else None
    config = provenance.get('config') if isinstance(provenance, dict) else None
    root = config.get('root') if isinstance(config, dict) else None
    return {'OPENCODE_CFG': root} if isinstance(root, str) and root else env


def instruction_digest(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps({name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()},
                                     sort_keys=True).encode()).hexdigest()


def instruction_sources(env: dict[str, str]) -> tuple[Path, dict[str, str]]:
    from runtime_sync import regular
    def read(path: Path) -> bytes | None:
        try:
            return regular(path)
        except (OSError, ValueError):
            raise PermissionError('project-tool-instructions-invalid') from None
    package = Path(__file__).resolve().parents[1]
    candidate = package.parent / 'config'
    managed = not candidate.is_dir()
    manifest = {}
    if managed:
        candidate = Path(env.get('OPENCODE_CFG') or str(Path(env.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')) / 'opencode'))
        data = read(package / 'zincir/runtime-manifest.json')
        if data is None:
            raise PermissionError('project-tool-instructions-unowned')
        try:
            metadata = json.loads(data)
            manifest = metadata['files']
            version = metadata['version']
        except (ValueError, TypeError, KeyError):
            raise PermissionError('project-tool-instructions-unowned') from None
        if (not isinstance(manifest, dict) or not isinstance(version, str)
                or read(package / '.runtime-generation') != version.encode()):
            raise PermissionError('project-tool-instructions-unowned')
    if candidate.is_symlink():
        raise PermissionError('project-tool-instructions-link')
    candidate = candidate.resolve(strict=True)
    files = {}
    for name in INSTRUCTION_PATHS:
        path = candidate / name
        raw = read(path)
        if raw is None or len(raw) > 50_000:
            raise PermissionError('project-tool-instructions-unavailable')
        owner = manifest.get(str(path))
        if managed and (not isinstance(owner, dict) or owner.get('sha256') != hashlib.sha256(raw).hexdigest()):
            raise PermissionError('project-tool-instructions-unowned')
        try:
            text = raw.decode()
        except UnicodeError:
            raise PermissionError('project-tool-instructions-invalid') from None
        if name != 'AGENTS.md':
            parts = text.split('---', 2)
            if len(parts) != 3 or parts[0] or re.search(r'(?m)^model:', parts[1]):
                raise PermissionError('project-tool-instructions-invalid')
        files[name] = text
    if any(read(candidate / name) != text.encode() for name, text in files.items()):
        raise PermissionError('project-tool-instructions-changed')
    return candidate, files


def loader_environment(directory: Path, env: dict[str, str], receipt: dict[str, Any]) -> None:
    plugin_path = Path(__file__).with_name('cadre-plugin.mjs').resolve(strict=True)
    plugin = plugin_path.as_uri()
    try:
        config = json.loads(env.get('OPENCODE_CONFIG_CONTENT') or '{}')
    except (ValueError, TypeError):
        raise PermissionError('project-tool-loader-config-invalid') from None
    if not isinstance(config, dict):
        raise PermissionError('project-tool-loader-config-invalid')
    if config.get('mcp'):
        raise PermissionError('project-tool-loader-external-service-unapproved')
    plugins = config.get('plugin', [])
    def owned_plugin(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        try:
            parsed = urlsplit(value)
            if parsed.scheme != 'file' or parsed.netloc or parsed.query or parsed.fragment or not parsed.path.startswith('/'):
                return False
            return Path(unquote(parsed.path)).resolve(strict=True) == plugin_path
        except (OSError, ValueError):
            return False
    if not isinstance(plugins, list) or any(not owned_plugin(item) for item in plugins):
        raise PermissionError('project-tool-loader-plugin-unapproved')
    record = RunStore(Path(env['CADRE_HOME'])).read(env['CADRE_RUN_ID'])
    instruction_root, instructions = instruction_sources(instruction_env(record, env))
    config['plugin'] = [plugin]
    if env.get('CADRE_PROJECT_TOOLS') == '1':
        gizli = {'skill': False, 'question': False}
        if re.fullmatch(r'plan-review-[1-9][0-9]*-[a-f0-9]{8}|review-(?:author|final)-[a-f0-9]{12}', env.get('CADRE_GOREV_ID', '')):
            gizli.update(todowrite=False, todoread=False)
        config['tools'] = {**(config.get('tools') or {}), **gizli}
    root = directory / 'loader'
    root.mkdir(mode=0o700)
    identity = root.stat()
    receipt.update(root=str(root), identity=[identity.st_dev, identity.st_ino])
    atomic_json(directory / 'loader-policy.json', receipt)
    paths = [root / 'home', root / 'config', root / 'config/opencode', root / 'state']
    for path in paths:
        path.mkdir(mode=0o700)
    target = root / 'config/opencode'
    frozen = {}
    for name, text in instructions.items():
        path = (target if name.startswith(('command/', 'skills/')) else root / 'instructions') / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write(path, text)
        path.chmod(0o400)
        frozen[name] = str(path)
    for name, row in config.get('agent', {}).items():
        path = 'agents/' + name + '.md'
        if path in instructions:
            row['prompt'] = instructions[path].split('---', 2)[2].strip() + '\n' + (row.get('prompt') or '')
    shared = [frozen['AGENTS.md']]
    if env.get('CADRE_ROL') in ('impl', 'qa', 'kalite', 'birlestirme', 'kurtarici'):
        shared.append(frozen['skills/self-check/SKILL.md'])
    if run_job_type(record) in ('research', 'report') or record.get('rapor'):
        shared.append(frozen['skills/rapor-kaniti/SKILL.md'])
    config['instructions'] = [*config.get('instructions', []), *shared]
    receipt['instructions'] = {'source': str(instruction_root), 'files': {
        name: {'sha256': hashlib.sha256(text.encode()).hexdigest(), 'frozen': frozen[name]}
        for name, text in instructions.items()}}
    receipt['instructions']['sha256'] = instruction_digest(instructions)
    atomic_json(target / 'opencode.json', {'$schema': 'https://opencode.ai/config.json'})
    atomic_write(target / '.gitignore', 'node_modules\npackage.json\npackage-lock.json\nbun.lock\n')
    for path in (target / 'opencode.json', target / '.gitignore'):
        path.chmod(0o400)
    directories = [p for p in root.rglob('*') if p.is_dir() and p != root / 'state']
    for path in [*directories, root]:
        path.chmod(0o500)
    if any(os.access(path, os.W_OK) or path.stat().st_mode & 0o222 for path in [root, *paths[:3]]):
        raise PermissionError('project-tool-loader-config-writable')
    receipt.update(config=str(target), home=str(root / 'home'), state=str(root / 'state'), plugin=plugin)
    atomic_json(directory / 'loader-policy.json', receipt)
    for key in ('BUN_OPTIONS', 'NODE_OPTIONS', 'NODE_PATH', 'OPENCODE_TUI_CONFIG'):
        env.pop(key, None)
    env.update(XDG_CONFIG_HOME=str(root / 'config'), OPENCODE_CONFIG_DIR=str(target),
               OPENCODE_CONFIG=str(target / 'opencode.json'), OPENCODE_TEST_HOME=str(root / 'home'),
               XDG_STATE_HOME=str(root / 'state'), OPENCODE_CONFIG_CONTENT=json.dumps(config), OPENCODE_PURE='false')
    env.update(OPENCODE_DISABLE_MODELS_FETCH='true', OPENCODE_EXPERIMENTAL_NATIVE_LLM='false')


def retire_loader(directory: Path, receipt: dict[str, Any], *, release: bool = True) -> None:
    if not receipt:
        return
    root = directory / 'loader'
    if str(root) != receipt['root'] or root.is_symlink():
        raise PermissionError('project-tool-loader-root-untrusted')
    current = root.stat()
    if [current.st_dev, current.st_ino] != receipt['identity']:
        raise PermissionError('project-tool-loader-root-untrusted')
    paths = [root, root / 'home', root / 'config', root / 'config/opencode',
             *[p for p in root.rglob('*') if p.is_dir() and p != root / 'state']]
    if any(path.is_symlink() for path in paths):
        raise PermissionError('project-tool-loader-root-untrusted')
    for path in paths if release else []:
        if path.is_dir():
            path.chmod(0o700)


def cleanup_tool_scratch(store: RunStore, policy: dict[str, Any], *, remove: bool = True) -> None:
    if not policy:
        return
    root = store.home.resolve() / 'tool-tmp'
    path = Path(policy['scratch'])
    if path.parent != root or not re.fullmatch('[a-f0-9]{32}', path.name) or root.is_symlink():
        raise PermissionError('project-tool-scratch-untrusted')
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    try:
        try:
            current = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(current.st_mode) or [current.st_dev, current.st_ino] != policy['scratch_identity']:
            raise PermissionError('project-tool-scratch-untrusted')
        if not shutil.rmtree.avoids_symlink_attacks:
            raise PermissionError('project-tool-scratch-cleanup-unavailable')
        if remove:
            shutil.rmtree(path.name, dir_fd=fd)
    finally:
        os.close(fd)


HARDLINK_ATLANAN = frozenset({'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache', '.mypy_cache',
                              '.ruff_cache', '.cache', '.tox'})
HARDLINK_KURULUM = frozenset({'.venv', 'venv', '.tox'})


def urun_hardlink_var(worktree: Path) -> bool:
    """FIX-04/FIX-25 (N-ENV02): every hardlink in product files is unsafe; in dependency/cache directories an inode alias pointing outside
    the tree (link count inside the tree less than st_nlink) is unsafe, pairs inside the tree (pnpm etc.) are allowed.
    `.git` is not scanned (local clone objects). The sandbox profile separately closes writes to install directories (`.venv`/`venv`/`.tox`; `node_modules`/`site-packages`
    are already closed); cache directories (`__pycache__`, `.ruff_cache`, `.pytest_cache`, `.cache`) are protected only by this preflight because real QA
    tools need to write there."""
    worktree = Path(worktree)
    aliaslar: dict[tuple[int, int], list[int]] = {}
    for directory, dirs, files in os.walk(worktree, followlinks=False):
        dirs[:] = [d for d in dirs if d != '.git']
        atlanan = bool(HARDLINK_ATLANAN.intersection(Path(directory).relative_to(worktree).parts))
        for name in files:
            try:
                info = os.lstat(os.path.join(directory, name))
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink <= 1:
                continue
            if not atlanan:
                return True
            kayit = aliaslar.setdefault((info.st_dev, info.st_ino), [info.st_nlink, 0])
            kayit[1] += 1
    return any(gorulen < nlink for nlink, gorulen in aliaslar.values())


ARAC_ORTAMI = ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TZ', 'PYTHONPATH', 'VIRTUAL_ENV',
               'CADRE_HOME', 'CADRE_ENGINE_ROOT', 'CADRE_RUN_ID', 'CADRE_GOREV_ID')


def review_source_paths(plan: dict[str, Any], worktree: Path, backlog: dict[str, Any] | None = None,
                        sources: list[str] | None = None) -> list[Path]:
    """N-IN04/N-REP01: the read scope of an accepted review is only the plan's explicit paths."""
    from dispatch_util import plan_path
    try:
        names = {name for task in plan['gorevler'] for name in task['dosyalar']}
        if backlog and backlog.get('full_path'):
            names.add(str(Path(backlog['full_path']).relative_to(worktree.resolve())))
        sections = {(worktree / section['path']).resolve() for section in (backlog or {}).get('sections') or []}
        paths = sorted({(worktree / plan_path(name, worktree)).resolve() for name in names} | sections)
        if sources is not None:
            allowed = [(worktree / name).resolve() for name in sources]
            if any(p not in sections and not any(p == a or (a.is_dir() and p.is_relative_to(a)) for a in allowed) for p in paths):
                raise ValueError('plan path outside request sources')
    except (KeyError, TypeError, ValueError, OSError, RuntimeError) as exc:
        raise PermissionError('project-tool-review-read-scope-invalid') from exc
    if not paths:
        raise PermissionError('project-tool-review-read-scope-missing')
    return paths


def qa_yorumlayici_kokleri(store: RunStore, record: dict[str, Any]) -> set[Path]:
    try:
        from kosu import kosu_qa_komutu
        command = kosu_qa_komutu(store.home, record)[0] if record.get('project') else ''
        tokens = shlex.split(command)
    except (OSError, ValueError, KeyError, TypeError):
        return set()
    roots: set[Path] = set()
    for token in tokens:
        path = Path(token)
        if not path.is_absolute() or not re.fullmatch(r'python[0-9.]*', path.name) or path.is_symlink() and not path.exists():
            continue
        if not path.is_file() or not os.access(path, os.X_OK):
            continue
        if (path.parent.parent / 'pyvenv.cfg').is_file():
            roots.add(path.parent.parent.resolve(strict=True))
        roots.add(path.resolve(strict=True).parent.parent)
    return roots


def kod_koku() -> Path:
    return Path(__file__).resolve().parent.parent


def project_tool_policy(store: RunStore, record: dict[str, Any], directory: Path, wt: str,
                        adapter: str, role: str, env: dict[str, str], *, checking: bool = False,
                        evidence: Path | None = None) -> dict[str, Any] | None:
    project = record.get('proje') or record.get('proje_ust') or record.get('project_operator_budget')
    if adapter != 'opencode' or sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file():
        raise PermissionError('project-tool-sandbox-unavailable')
    worktree = Path(wt).resolve(strict=True)
    writable = role in ('impl', 'qa') and run_job_type(record) != 'review' and env.get('CADRE_SALT_OKUR') != '1'
    if writable and not checking and record.get('worktree') and worktree != Path(record['worktree']).resolve(strict=True):
        raise PermissionError('project-tool-worktree-mismatch')
    if urun_hardlink_var(worktree):
        raise PermissionError('project-tool-hardlink-untrusted')
    git_env = {k: v for k, v in env.items() if not k.startswith('GIT_')}
    git_env.update(GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0')
    metadata: list[Path] = []
    git_root = worktree if writable else Path(record.get('worktree') or worktree).resolve(strict=True)
    if (project and writable) or (git_root / '.git').exists():
        try:
            result = subprocess.run(['git', '-C', str(git_root), 'rev-parse', '--path-format=absolute', '--git-dir', '--git-common-dir'],
                                    env=git_env, capture_output=True, text=True, check=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            raise PermissionError('project-tool-git-metadata-unavailable') from exc
        metadata = [Path(p).resolve(strict=True) for p in result.stdout.splitlines()]
        if len(metadata) != 2 or any(p == git_root or git_root.is_relative_to(p) for p in metadata):
            raise PermissionError('project-tool-git-metadata-invalid')
    scratch = store.home.resolve() / 'tool-tmp' / (directory.name if re.fullmatch('[a-f0-9]{32}', directory.name) else uuid.uuid4().hex)
    quoted = lambda p: json.dumps(str(p), ensure_ascii=False)
    profile = ['(version 1)', '(allow default)', '(deny file-write*)', '(deny file-link)', '(deny network*)', '(deny mach-lookup)',
               '(deny signal (target others))', '(deny process-info* (target others))',
               '(allow signal (target same-sandbox))', '(allow process-info* (target same-sandbox))',
               f'(allow file-write* (subpath {quoted(scratch)}) (literal "/dev/null"))',
               f'(deny file-write* (literal {quoted(scratch)}))']
    write_paths = [worktree] if writable else []
    if writable and record.get('request') and role == 'impl':
        write_paths = []
        history = store.plan_view(record['run_id'])['history']
        for task in history[-1]['plan']['gorevler'] if history else []:
            if task['id'] == env.get('CADRE_GOREV_ID'):
                write_paths = [(worktree / name).resolve() for name in task['dosyalar']]
        if any(p == worktree or not p.is_relative_to(worktree) for p in write_paths):
            raise PermissionError('project-tool-write-scope-invalid')
        parents = {p for path in write_paths for p in path.parents if p != worktree and p.is_relative_to(worktree)}
        for p in sorted(parents):
            profile.append(f'(allow file-write-create (literal {quoted(p)}))')
    for p in write_paths:
        profile.append(f'(allow file-write* (subpath {quoted(p)}))')
    if evidence is not None:
        evidence = evidence.resolve(strict=True)
        if (not checking or evidence.parent != (store.task_dir(record['run_id'], 'qa') / 'checks').resolve()
                or not re.fullmatch('[a-f0-9]{32}', evidence.name)):
            raise PermissionError('project-tool-evidence-path-invalid')
        profile.append(f'(allow file-write* (subpath {quoted(evidence)}))')
    code_root = kod_koku()
    code_paths = ([code_root / name for name in ('lib', 'bin', 'hooks', 'tests', 'USAGE.md')]
                  if code_root == store.home.resolve() else [code_root])
    readable = {worktree, scratch, (directory / 'process-allow').resolve(), *code_paths, git_root, *metadata}
    readable.update(p.parent for p in metadata if p.name == '.git')
    for key in ('VIRTUAL_ENV', 'CADRE_TEST_PYTEST_PYTHON', 'CADRE_UI_PYTHON', 'CADRE_TEST_ARAYUZ_PYTHON'):
        value = env.get(key)
        if value:
            path = Path(value)
            readable.add((path if key == 'VIRTUAL_ENV' else path.parent.parent).resolve(strict=True))
            # N-ENV03: a venv's Python may depend on a separate distribution inside home, like uv.
            binary = path / 'bin/python' if key == 'VIRTUAL_ENV' else path
            if binary.is_file() and os.access(binary, os.X_OK):
                readable.add(binary.resolve(strict=True).parent.parent)
    readable.update(qa_yorumlayici_kokleri(store, record))
    if evidence is not None:
        readable.add(evidence)
    readable.update(Path(p) for p in ('/System', '/usr', '/bin', '/sbin', '/Library', '/opt', '/private/etc',
                                      '/private/var/db', '/private/preboot', '/dev'))
    read_scopes = ' '.join(f'(subpath {quoted(p)})' for p in sorted(readable))
    if sys.prefix != sys.base_prefix:
        # N-ENV02/N-QA01: scope-check also uses the engine's Python. Even without the environment variable
        # its own pyvenv.cfg must be read; the whole venv/parent directory is not opened.
        bootstrap = Path(sys.prefix) / 'pyvenv.cfg'
        if bootstrap.is_symlink() or not bootstrap.is_file():
            raise PermissionError('project-tool-python-config-untrusted')
        read_scopes += f' (literal {quoted(bootstrap.resolve(strict=True))})'
    profile.append(f'(deny file-read-data (require-not (require-any (literal "/") {read_scopes})))')
    read_paths = None
    task_id = env.get('CADRE_GOREV_ID', '')
    request = record.get('request') or {}
    sources = request.get('source_paths') or None
    if (record.get('request') and run_job_type(record) == 'review'
            and task_id.startswith(('review-author-', 'review-final-', 'review-recheck-'))):
        from kosu import request_backlog
        history = store.plan_view(record['run_id'])['history']
        read_paths = review_source_paths(history[-1]['plan'] if history else {}, worktree,
                                       request_backlog(store, record['run_id'], worktree), sources)
    elif sources:
        read_paths = sorted({(worktree / name).resolve() for name in sources})
    if read_paths is not None:
        parents = {p for path in read_paths for p in path.parents if p.is_relative_to(worktree)}
        allowed = [f'({"subpath" if p.is_dir() else "literal"} {quoted(p)})' for p in read_paths]
        allowed += [f'(literal {quoted(p)})' for p in sorted(parents)]
        profile.append(f'(deny file-read-data (require-all (subpath {quoted(worktree)}) '
                       f'(require-not (require-any {" ".join(allowed)}))))')
        # Git objects also carry the content of unselected files; git show cannot cross the read limit.
        for p in metadata:
            profile.append(f'(deny file-read-data (subpath {quoted(p)}))')
    if request.get('backlog_items'):
        try:
            section = worktree / Path(request['backlog_path']).relative_to(request['target_path'])
        except (KeyError, TypeError, ValueError) as exc:
            raise PermissionError('project-tool-section-scope-invalid') from exc
        profile.append(f'(deny file-read-data (literal {quoted(section)}) (literal {quoted(section.resolve())}))')
    if request.get('backlog_items') or sources:
        profile.append(f'(deny file-read-data (subpath {quoted(store.root.resolve())}))')
        profile.extend(f'(deny file-read-data (subpath {quoted(p)}))' for p in metadata)
    profile.append(f'(deny file-write* (literal {quoted(worktree)}) (literal {quoted(worktree / ".git")}))')
    # N-ENV02: if the installed code is in the data root, worktrees/tool-tmp are in the same root.
    # Protect only the installed code; product authority is limited by write_paths above.
    protected = [store.root.resolve(), store.home.resolve() / 'projects', store.home.resolve() / 'zincir',
                 store.home.resolve() / 'operator-system', store.home.resolve() / 'locks',
                 *code_paths, *metadata]
    for p in protected:
        condition = f'(subpath {quoted(p)})'
        if p == store.root.resolve() and evidence is not None:
            condition = f'(require-all {condition} (require-not (subpath {quoted(evidence)})))'
        profile.append(f'(deny file-write* {condition})')
    profile.append('(deny file-write* (regex "/[.][gG][iI][tT](/|$)"))')
    profile.append('(deny file-write* (regex "/([nN][oO][dD][eE]_[mM][oO][dD][uU][lL][eE][sS]|[sS][iI][tT][eE]-[pP][aA][cC][kK][aA][gG][eE][sS]|[^/]*[.][dD][iI][sS][tT]-[iI][nN][fF][oO]|[^/]*[.][eE][gG][gG]-[iI][nN][fF][oO])(/|$)"))')
    installers = r'/(pip[0-9.]*|pipx|uv|poetry|pipenv|easy_install[0-9.]*|brew|cargo|rustup|go|bun|install)$'
    installer_modules = r'/(pip|pipx|ensurepip|venv|virtualenv|poetry|pipenv)(/|[.]py$)'
    def folded(value: str) -> str:
        return ''.join(f'[{c.lower()}{c.upper()}]' if c.isascii() and c.isalpha() else c for c in value)
    profile.append(f'(deny process-exec (regex {quoted(folded(installers))}))')
    install_dirs = '/(' + '|'.join(folded(d).replace('.', '[.]') for d in sorted(HARDLINK_KURULUM)) + ')(/|$)'
    profile.append(f'(deny file-write* (regex {quoted(install_dirs)}))')
    profile.append(f'(deny file-read-data (regex {quoted(folded(installers))}) (regex {quoted(folded(installer_modules))}))')
    secret_roots = [Path(env.get('HOME') or str(Path.home())).resolve() / name
                    for name in ('.ssh', '.aws', '.config/gcloud', '.config/gh', '.local/share/opencode', '.config/opencode/auth.json',
                                 '.codex', '.claude.json', '.netrc')]
    profile.extend(f'(deny file-read* (subpath {quoted(p)}))' for p in secret_roots)
    sensitive = folded(PROTECTED_SOURCE.pattern)
    profile.append(f'(deny file-read-data (regex {quoted(sensitive)}))')
    policy = {'version': 1, 'sandbox': '/usr/bin/sandbox-exec', 'profile': '\n'.join(profile),
              'worktree': str(worktree), 'scratch': str(scratch), 'writable': writable,
              'write_paths': [str(p) for p in write_paths]}
    if read_paths is not None:
        policy['read_paths'] = [str(p) for p in read_paths]
    if not re.fullmatch('[a-f0-9]{32}', scratch.name) or scratch.parent.is_symlink():
        raise PermissionError('project-tool-scratch-untrusted')
    scratch.mkdir(parents=True, mode=0o700)
    identity = scratch.stat()
    policy['scratch_identity'] = [identity.st_dev, identity.st_ino]
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        owner = directory.stat()
        policy['owner'] = {'run_id': record['run_id'], 'directory': str(directory), 'identity': [owner.st_dev, owner.st_ino]}
        scope = {}
        for name in ('process-allow', 'process-deny'):
            marker = directory / name
            atomic_write(marker, b'')
            marker.chmod(0o400)
            info = marker.stat()
            scope[name] = [info.st_dev, info.st_ino]
        policy['process_scope'] = scope
        profile.append(f'(allow file-read-data (literal {quoted((directory / "process-allow").resolve())}))')
        profile.append(f'(deny file-read-data (literal {quoted((directory / "process-deny").resolve())}))')
        policy['profile'] = '\n'.join(profile)
        ortam = ' '.join(f'{k}={shlex.quote(env[k])}' for k in ARAC_ORTAMI if isinstance(env.get(k), str))
        betik = ('#!/bin/sh\n'
                 f'exec {policy["sandbox"]} -p {shlex.quote(policy["profile"])} /usr/bin/env -i {ortam} '
                 f'TMPDIR={shlex.quote(str(scratch))} GIT_OPTIONAL_LOCKS=0 PYTHONDONTWRITEBYTECODE=1 /bin/sh -c "$1"\n').encode()
        arac = directory / 'arac'
        atomic_write(arac, betik)
        arac.chmod(0o550)
        policy.update(arac=str(arac.resolve()), arac_sha256=hashlib.sha256(betik).hexdigest())
        atomic_json(directory / 'tool-policy.json', policy)
        subprocess.run([policy['sandbox'], '-p', policy['profile'], sys.executable, str((code_root / 'lib' / 'oturum.py').resolve()),
                        'scope-check', str((directory / 'process-allow').resolve()), str((directory / 'process-deny').resolve())],
                       cwd=worktree, env=git_env, capture_output=True, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        cleanup_tool_scratch(store, policy)
        raise PermissionError('project-tool-sandbox-unavailable') from exc
    return policy



def tool_scope_members(store: RunStore, directory: Path, policy: dict[str, Any]) -> list[dict[str, Any]]:
    import ctypes
    scope = policy.get('process_scope')
    current = directory.stat()
    owner = policy.get('owner', {})
    if (sys.platform != 'darwin' or not isinstance(scope, dict)
            or owner.get('directory') != str(directory)
            or owner.get('identity') != [current.st_dev, current.st_ino]):
        raise PermissionError('project-tool-process-scope-untrusted')
    markers = [directory / name for name in ('process-allow', 'process-deny')]
    for marker in markers:
        with health_file(store, marker) as fd:
            current = os.fstat(fd)
            if (scope.get(marker.name) != [current.st_dev, current.st_ino] or current.st_size != 0
                    or current.st_nlink != 1 or current.st_uid != os.getuid() or current.st_mode & 0o222):
                raise PermissionError('project-tool-process-scope-untrusted')
    try:
        canonical = [marker.resolve(strict=True) for marker in markers]
        library = ctypes.CDLL('/usr/lib/libsandbox.dylib', use_errno=True)
        check = library.sandbox_check
        check.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int)
        check.restype = ctypes.c_int
        probe = check, 1 | ctypes.c_int.in_dll(library, 'SANDBOX_CHECK_NO_REPORT').value
        query = ctypes.CDLL('/usr/lib/libproc.dylib').proc_listpids
        query.argtypes = (ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int)
        query.restype = ctypes.c_int
        size = query(1, 0, None, 0)
        if size < 0 or size > 4_000_000:
            raise PermissionError('project-tool-process-scope-unavailable')
        pids = (ctypes.c_int * (size // ctypes.sizeof(ctypes.c_int) + 1024))()
        used = query(1, 0, ctypes.byref(pids), ctypes.sizeof(pids))
        if used < 0 or used >= ctypes.sizeof(pids) or used % ctypes.sizeof(ctypes.c_int):
            raise PermissionError('project-tool-process-scope-unavailable')
        members = []
        for pid in pids[:used // ctypes.sizeof(ctypes.c_int)]:
            if pid <= 0:
                continue
            try:
                values = sandbox_scope_decisions(pid, canonical, probe=probe)
            except PermissionError:
                if process_identity(pid) is None:
                    continue
                raise
            if values == [0, 1]:
                identity = process_identity(pid)
                if identity is None:
                    if sandbox_scope_decisions(pid, canonical, probe=probe) == [0, 1]:
                        raise PermissionError('project-tool-process-scope-unavailable')
                    continue
                if sandbox_scope_decisions(pid, canonical, probe=probe) != [0, 1] or process_identity(pid) != identity:
                    raise PermissionError('project-tool-process-scope-changed')
                members.append(identity)
        for marker in markers:
            with health_file(store, marker) as fd:
                current = os.fstat(fd)
                if (scope.get(marker.name) != [current.st_dev, current.st_ino] or current.st_size != 0
                        or current.st_mode & 0o222):
                    raise PermissionError('project-tool-process-scope-changed')
        return members
    except (AttributeError, ValueError) as error:
        raise PermissionError('project-tool-process-scope-unavailable') from error


def tool_scope_digest(policy: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()


def tool_scope_armed(store: RunStore, path: Path, receipt: dict[str, Any], policy: dict[str, Any]) -> bool:
    marker = path.parent / 'tools-armed.json'
    if not marker.exists() and not marker.is_symlink():
        return False
    armed = health_json(store, marker)
    expected = {'owner': policy['owner'], 'pid': receipt['process_identity']['pid'],
                'profile_sha256': hashlib.sha256(policy['profile'].encode()).hexdigest()}
    if armed != expected:
        raise PermissionError('project-tool-process-scope-untrusted')
    return True


def reclaim_tool_scope(store: RunStore, path: Path, data: dict[str, Any]) -> None:
    tool_scope(store, path, data)
    observer = data.get('observer_identity')
    if observer is None:
        return
    if (not isinstance(observer, dict) or type(observer.get('pid')) is not int or observer['pid'] <= 1
            or type(observer.get('pgid')) is not int or not isinstance(observer.get('started'), str) or not observer['started']):
        raise PermissionError('project-tool-recovery-process-untrusted')
    if process_identity(observer['pid']) == observer:
        return
    policy = health_json(store, path.parent / 'tool-policy.json')
    if not policy.get('process_scope'):
        return
    process_path = path.parent / 'process.json'
    if not process_path.exists() and not process_path.is_symlink():
        return
    receipt = health_json(store, process_path)
    _, binding = tool_scope(store, path, data)
    if (any(receipt.get(key) != value for key, value in binding.items())
            or receipt.get('scope_digest') != tool_scope_digest(policy)):
        raise PermissionError('project-tool-process-scope-untrusted')
    tool_scope_armed(store, path, receipt, policy)
    deadline = time.monotonic() + 5
    while members := tool_scope_members(store, path.parent, policy):
        if time.monotonic() >= deadline:
            raise PermissionError('project-tool-process-scope-active')
        for identity in members:
            if identity['pid'] == os.getpid():
                raise PermissionError('project-tool-process-scope-untrusted')
            for operation in (signal.SIGSTOP, signal.SIGKILL):
                if process_identity(identity['pid']) == identity:
                    try:
                        os.kill(identity['pid'], operation)
                    except ProcessLookupError:
                        pass
        time.sleep(.01)
    atomic_json(path.parent / 'process.json', {**receipt, 'scope_stopped': receipt['scope_digest']})


def tool_process_active(identity: Any) -> bool:
    if (not isinstance(identity, dict) or type(identity.get('pid')) is not int or identity['pid'] <= 1
            or identity.get('pgid') != identity['pid'] or not isinstance(identity.get('started'), str)):
        raise PermissionError('project-tool-recovery-process-untrusted')
    if process_identity(identity['pid']) == identity:
        return True
    if sys.platform != 'darwin':
        raise PermissionError('project-tool-recovery-platform-unsupported')
    import ctypes
    query = ctypes.CDLL('/usr/lib/libproc.dylib').proc_listpids
    query.argtypes = (ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int)
    query.restype = ctypes.c_int
    size = query(2, identity['pgid'], None, 0)
    if size < 0 or size > 4_000_000:
        raise PermissionError('project-tool-recovery-process-unavailable')
    members = (ctypes.c_int * (size // ctypes.sizeof(ctypes.c_int) + 128))()
    used = query(2, identity['pgid'], ctypes.byref(members), ctypes.sizeof(members))
    if used < 0 or used >= ctypes.sizeof(members) or used % ctypes.sizeof(ctypes.c_int):
        raise PermissionError('project-tool-recovery-process-unavailable')
    for pid in members[:used // ctypes.sizeof(ctypes.c_int)]:
        current = process_identity(pid)
        if current is not None and current['pgid'] == identity['pgid']:
            return True
    return False


def tool_scope(store: RunStore, path: Path, data: dict[str, Any]) -> tuple[Path, dict[str, str]]:
    run_id, task_id = identifier(data['run_id']), identifier(data['task_id'])
    task = store.path(run_id) / 'tasks' / task_id
    if path.name == 'attempt.json':
        key = 'attempt_id'
        expected = task / 'attempts' / identifier(data[key]) / path.name
        lease = task / f".{identifier(data['adapter'])}-{identifier(data['role'])}.lock"
    else:
        key = 'check_id'
        expected = task / 'tool-checks' / identifier(data[key]) / 'check.json'
        lease = task / f".check-{identifier(data[key])}.lock"
        if task_id != 'qa':
            raise PermissionError('project-tool-recovery-record-untrusted')
    if path != expected:
        raise PermissionError('project-tool-recovery-record-untrusted')
    return lease, {name: data[name] for name in ('run_id', 'task_id', key)}


def finish_tool_artifacts(store: RunStore, path: Path, data: dict[str, Any], policy: dict[str, Any], loader: dict[str, Any],
                          *, observed_pid: int | None = None) -> None:
    process = path.parent / 'process.json'
    if process.is_file() and observed_pid is not None:
        receipt = health_json(store, process)
        if receipt.get('process_identity', {}).get('pid') != observed_pid:
            raise PermissionError('project-tool-recovery-process-untrusted')
        atomic_json(process, {**receipt, 'stop_observed': receipt['process_identity']})
    if process.is_file() and tool_process_active(health_json(store, process).get('process_identity')):
        store.update(data['run_id'], tool_boundary_wait='project-tool-process-group-active', reason='project-tool-process-group-active')
        raise PermissionError('project-tool-process-group-active')
    if policy.get('process_scope') and tool_scope_members(store, path.parent, policy):
        store.update(data['run_id'], tool_boundary_wait='project-tool-process-group-active', reason='project-tool-process-group-active')
        raise PermissionError('project-tool-process-group-active')
    try:
        cleanup_tool_scratch(store, policy)
    finally:
        retire_loader(path.parent, loader)
    if policy or loader:
        _, binding = tool_scope(store, path, data)
        current = path.parent.stat()
        atomic_json(path.parent / 'tool-retired.json', {**binding, 'directory_identity': [current.st_dev, current.st_ino],
                    'scratch_removed': bool(policy), 'loader_identity': loader.get('identity')})


def retire_owned_tools(store: RunStore, path: Path, data: dict[str, Any]) -> bool:
    _, binding = tool_scope(store, path, data)
    directory = path.parent
    scratch = store.home.resolve() / 'tool-tmp' / directory.name
    resources = [*[directory / name for name in ('tool-policy.json', 'loader-policy.json', 'loader', 'tool-retired.json')], scratch]
    if not any(p.exists() or p.is_symlink() for p in resources):
        return True
    retired = directory / 'tool-retired.json'
    already_retired = False
    if retired.exists() or retired.is_symlink():
        marker = health_json(store, retired)
        current = directory.stat()
        root = directory / 'loader'
        scratch = store.home.resolve() / 'tool-tmp' / directory.name
        if (any(marker.get(key) != value for key, value in binding.items())
                or marker.get('directory_identity') != [current.st_dev, current.st_ino]):
            raise PermissionError('project-tool-recovery-retirement-untrusted')
        if not scratch.exists() and not scratch.is_symlink():
            if marker.get('loader_identity') is None and not root.exists() and not root.is_symlink():
                already_retired = True
            if not root.is_symlink() and root.is_dir():
                current = root.stat()
                if marker.get('loader_identity') == [current.st_dev, current.st_ino] and current.st_mode & 0o200:
                    already_retired = True
    process = directory / 'process.json'
    if process.exists() or process.is_symlink():
        receipt = health_json(store, process)
        if any(receipt.get(key) != value for key, value in binding.items()):
            raise PermissionError('project-tool-recovery-process-untrusted')
        if tool_process_active(receipt.get('process_identity')):
            return False
        policy = health_json(store, directory / 'tool-policy.json')
        if policy.get('process_scope') and not already_retired:
            if receipt.get('scope_digest') != tool_scope_digest(policy):
                raise PermissionError('project-tool-process-scope-untrusted')
            if tool_scope_members(store, directory, policy):
                return False
        verified_scope = False
        if not already_retired and (path.name == 'check.json' or tool_scope_armed(store, path, receipt, policy)):
            verified_scope = (bool(policy.get('process_scope')) and receipt.get('scope_digest') == tool_scope_digest(policy)
                              and not tool_scope_members(store, directory, policy))
        if not already_retired and not verified_scope and receipt.get('stop_observed') != receipt.get('process_identity'):
            raise PermissionError('project-tool-recovery-teardown-unobserved')
    elif data.get('pid') and not already_retired:
        raise PermissionError('project-tool-recovery-process-unrecorded')
    if already_retired:
        return True
    policy_path, loader_path = directory / 'tool-policy.json', directory / 'loader-policy.json'
    policy = health_json(store, policy_path) if policy_path.exists() or policy_path.is_symlink() else {}
    loader = health_json(store, loader_path) if loader_path.exists() or loader_path.is_symlink() else {}
    scratch = store.home.resolve() / 'tool-tmp' / directory.name
    if (scratch.exists() or scratch.is_symlink()) and not policy:
        raise PermissionError('project-tool-recovery-scratch-unrecorded')
    if ((directory / 'loader').exists() or (directory / 'loader').is_symlink()) and not loader:
        raise PermissionError('project-tool-recovery-loader-unrecorded')
    if policy:
        owner = directory.stat()
        expected_owner = {'run_id': data['run_id'], 'directory': str(directory), 'identity': [owner.st_dev, owner.st_ino]}
        if policy.get('owner') != expected_owner or Path(policy['scratch']).name != directory.name:
            raise PermissionError('project-tool-recovery-policy-untrusted')
        cleanup_tool_scratch(store, policy, remove=False)
    retire_loader(directory, loader, release=False)
    finish_tool_artifacts(store, path, data, policy, loader)
    return True


def recover_tool_artifacts(store: RunStore, run_id: str, *, reclaim: bool = False) -> dict[str, Any]:
    report: dict[str, Any] = {'run_id': run_id, 'retired': [], 'active': [], 'unknown': []}
    health_json(store, store.path(run_id) / 'run.json')
    owner = FileLock(store.path(run_id) / '.owner.lock')
    if not owner.acquire(blocking=False):
        return report
    try:
        tasks = store.path(run_id) / 'tasks'
        paths = [*tasks.glob('*/attempts/*/attempt.json'), *tasks.glob('qa/tool-checks/*/check.json')]
        for path in sorted(paths):
            lease = None
            try:
                resources = [*[path.parent / name for name in ('tool-policy.json', 'loader-policy.json', 'loader')],
                             store.home.resolve() / 'tool-tmp' / path.parent.name]
                if not any(p.exists() or p.is_symlink() for p in resources):
                    continue
                data = health_json(store, path)
                if data.get('run_id') != run_id:
                    raise PermissionError('project-tool-recovery-record-untrusted')
                lock_path, binding = tool_scope(store, path, data)
                if reclaim:
                    reclaim_tool_scope(store, path, data)
                with health_file(store, lock_path) as fd:
                    identity = os.fstat(fd)
                lease = FileLock(lock_path)
                if not lease.acquire(blocking=False):
                    report['active'].append(binding)
                    continue
                assert lease.fd is not None
                current = os.fstat(lease.fd)
                if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                    raise PermissionError('project-tool-recovery-lease-untrusted')
                if retire_owned_tools(store, path, data):
                    report['retired'].append(binding)
                else:
                    report['active'].append(binding)
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                report['unknown'].append({'scope': str(path.relative_to(tasks)), 'reason': 'project-tool-recovery-untrusted'})
            finally:
                if lease is not None:
                    lease.close()
        target = store.path(run_id) / 'tool-recovery.json'
        if paths and (not target.exists() or health_json(store, target) != report):
            atomic_json(target, report)
        if report['unknown']:
            store.update(run_id, tool_boundary_wait='project-tool-recovery-untrusted', reason='project-tool-recovery-untrusted')
        elif not report['active'] and store.read(run_id).get('tool_boundary_wait') in (
                'project-tool-recovery-untrusted', 'project-tool-process-group-active'):
            store.update(run_id, tool_boundary_wait=None, reason=None)
        return report
    finally:
        owner.close()


class SessionRecoveryError(RuntimeError):
    """Identity is unknown/conflicting: retain artifacts and refuse a fresh task."""




def result_json(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and (value.get("type") == "result" or "result" in value):
            result = value
    return result


def run_for_worktree(store: RunStore, worktree: str, project: str | None = None) -> dict[str, Any] | None:
    matches = [r for r in store.list(project) if r.get("worktree") and Path(r["worktree"]).resolve() == Path(worktree).resolve()]
    if len(matches) > 1:
        raise SessionRecoveryError("multiple runs claim the same worktree")
    return matches[0] if matches else None


def session_ids(record: dict[str, Any], task_id: str | None = None) -> set[str]:
    tasks = record["tasks"]
    if task_id is not None:
        tasks = {task_id: tasks.get(task_id, {})}
    return {sid for task in tasks.values() for key, scope in task.get("executors", {}).items()
            if key.startswith("opencode:") for sid in scope.get("sessions", [])}




class Attempt:
    def __init__(self, adapter: str, role: str, model: str, prompt: str, config: str = "", devam_ek: str = "",
                 devam_zorunlu: bool = False) -> None:
        self.store = RunStore()
        self.run_id = os.environ["CADRE_RUN_ID"]
        self.task_id = os.environ.get("CADRE_GOREV_ID") or "main"
        self.adapter = adapter
        self.tool_policy: dict[str, Any] = {}
        self.loader_policy: dict[str, Any] = {}
        self.role = identifier(role)
        self.key = f"{adapter}:{role}"
        self.root = self.store.task_dir(self.run_id, self.task_id)
        self.lease = FileLock(self.root / f".{adapter}-{role}.lock")
        # An orphaned executor (the adapter died, the child lives) holds the scope.
        # In the cloud too, wait a limited time until the child exits, then take over the same session; if time runs out, the old error.
        son = time.time() + float(os.environ.get("CADRE_KAPSAM_BEKLE_SN", "900"))
        while not self.lease.acquire(blocking=False):
            if time.time() >= son:
                raise SessionRecoveryError("executor scope already has an owner")
            time.sleep(0.2)
        try:
            try:
                for path in sorted(self.root.glob('attempts/*/attempt.json')):
                    data = health_json(self.store, path)
                    if (data.get('adapter'), data.get('role')) != (adapter, role):
                        continue
                    if (data.get('run_id'), data.get('task_id')) != (self.run_id, self.task_id):
                        raise SessionRecoveryError('attempt record identity mismatch')
                    try:
                        if not retire_owned_tools(self.store, path, data):
                            raise SessionRecoveryError('executor process group still active')
                    except PermissionError as error:
                        if str(error) != 'project-tool-recovery-teardown-unobserved':
                            raise
                        self.store.update(self.run_id, tool_boundary_wait='project-tool-recovery-untrusted',
                                          reason='project-tool-recovery-untrusted')
            except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
                raise PermissionError('project-tool-recovery-untrusted') from None
            record = self.store.read(self.run_id)
            task = record["tasks"].get(self.task_id, {})
            previous = task.get("executors", {}).get(self.key, {}).get("attempt_id")
            if role in YENI_KAPSAM_ROLLERI and not devam_zorunlu:
                # G-A4/G-C10 (B151): judge/QA/autopsy attempts do not take over like a worker — if the previous judge failed without a session
                # the chain must not lock up, and the second judge must not continue the first one's conversation. Every attempt is a new scope.
                previous = None
            self.handoff = role == "impl" and bool(task.get("attempt_count")) and task.get("last_executor", self.key if previous else "") != self.key
            self.resuming = False
            sid = ""
            if devam_zorunlu and not previous:
                raise SessionRecoveryError('yol devri icin onceki oturum kaydi yok')
            if previous:
                old = self.root / "attempts" / identifier(previous) / "attempt.json"
                data = json.loads(old.read_text())
                if (data.get("run_id"), data.get("task_id"), data.get("adapter"), data.get("role")) != (self.run_id, self.task_id, adapter, role):
                    raise SessionRecoveryError("attempt record identity mismatch")
                self.reconcile(data, old)
                sid = data.get("session_id", "")
                if not sid and data["outcome"] != "not_started" and self.oturumsuz_cokme(data, old):
                    data["outcome"] = "not_started"   # B278: the session never opened; the record stays, a new attempt starts
                    atomic_json(old, data)
                    self.kilit_bekle()   # parallel lanes should release the DB lock
                if not sid and data["outcome"] != "not_started":
                    raise SessionRecoveryError(f"session unknown for {previous}; retained for reconciliation")
                self.resuming = bool(sid) and data["outcome"] != "not_started"
            attempt_id = uuid.uuid4().hex
            self.directory = self.root / "attempts" / attempt_id
            self.directory.mkdir(parents=True, mode=0o700)
            if adapter == "claude" and not self.resuming:
                sid = str(uuid.uuid4())
            self.path = self.directory / "attempt.json"
            self.stdout_path = self.directory / "stdout.jsonl"
            self.data: dict[str, Any] = {
                "attempt_id": attempt_id, "run_id": self.run_id, "task_id": self.task_id,
                "adapter": adapter, "role": role, "model": model, "session_id": sid,
                "title": f"qe:{self.run_id}:{self.task_id}:{role}:{attempt_id}",
                "outcome": "unknown", "created": time.time(), "config": config,
                "observer_identity": process_identity(os.getpid()),
                "stdout": str(self.stdout_path), "resumed": self.resuming,
                **revision_context(record, self.task_id),
            }
            self.prompt = prompt
            if self.handoff:
                atomic_json(self.directory / "handoff.json", task)
                self.prompt = (f"DEVIR: Bu gorev daha once baska yurutucuyle basladi. Mevcut worktree'deki emegi koru; "
                               f"once {self.directory / 'handoff.json'} ve oradaki oturum/deneme kayitlarini oku. "
                               "Kabul kriterlerini yeniden kontrol et, yalniz bitmemis adimi yap.\n\nOZGUN GOREV:\n" + prompt)
            elif self.resuming and (role == "impl" or devam_zorunlu):
                self.prompt = CONTINUE + devam_ek   # §0a/10(b): the continuing worker should also know about the closed helper
            atomic_write(self.directory / "prompt.md", self.prompt)
            atomic_json(self.path, self.data)
            def start(task: dict[str, Any]) -> None:
                scopes = task.setdefault("executors", {})
                scope = scopes.setdefault(self.key, {"sessions": []})
                scope.update(attempt_id=attempt_id, attempt_count=scope.get("attempt_count", 0) + 1, resume_required=True)
                if role == "impl":
                    task["attempt_count"] = task.get("attempt_count", 0) + 1
                    task["last_executor"] = self.key
            self.store.task_mutate(self.run_id, self.task_id, start)
            if sid:
                self.bind(sid, self.data, self.path)
        except BaseException as error:
            try:
                if isinstance(error, PermissionError) and str(error).startswith('project-tool-'):
                    self.store.update(self.run_id, tool_boundary_wait=str(error), reason=str(error))
            finally:
                self.lease.close()
            raise

    def bind(self, sid: str, data: dict[str, Any], path: Path) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid):
            raise SessionRecoveryError("invalid session identifier")
        if data.get("session_id") and data["session_id"] != sid:
            raise SessionRecoveryError("executor emitted a different session identity")
        claim = self.store.home / "session-owners" / self.adapter / f"{sid}.json"
        owner = {"run_id": self.run_id, "task_id": self.task_id, "scope": self.key}
        with FileLock(self.store.home / "session-owners/.lock"):
            if claim.exists() and json.loads(claim.read_text()) != owner:
                raise SessionRecoveryError("session already belongs to another task/run")
            if not claim.exists():
                atomic_json(claim, owner)
            if data.get("session_id") != sid:
                data["session_id"] = sid
                atomic_json(path, data)
            def update(task: dict[str, Any]) -> None:
                scope = task.setdefault("executors", {}).setdefault(self.key, {"sessions": []})
                if sid not in scope["sessions"]:
                    scope["sessions"].append(sid)
                scope["session_id"] = sid
            self.store.task_mutate(self.run_id, self.task_id, update)

    def oturumsuz_cokme(self, data: dict[str, Any], path: Path) -> bool:
        """B278: when parallel lanes wrote to opencode's single SQLite DB at the same time, opencode failed with 'database is locked'
        BEFORE opening a session (22 times). An attempt that produced no events (empty stdout), exited non-zero and for which reconcile could find no session
        by title never started; an attempt that produced events but has an unclear identity is still not blindly restarted."""
        if self.adapter != "opencode" or data.get("outcome") != "finished" or data.get("exit_code") in (0, None):
            return False
        try:
            return (path.parent / "stdout.jsonl").stat().st_size == 0
        except OSError:
            return False

    def reconcile(self, data: dict[str, Any], path: Path) -> None:
        field = "sessionID" if self.adapter == "opencode" else "session_id"
        found = {e[field] for e in events(path.parent / "stdout.jsonl") if isinstance(e.get(field), str)}
        if not found and self.adapter == "opencode":
            db = Path(os.environ.get("OPENCODE_DB", str(Path.home() / ".local/share/opencode/opencode.db")))
            if db.exists():
                found = OC.session_by_title(db, data["title"])
        if len(found) > 1:
            raise SessionRecoveryError("ambiguous session receipt")
        if found:
            self.bind(found.pop(), data, path)
        elif data.get("session_id"):
            self.bind(data["session_id"], data, path)

    def kilit_bekle(self) -> None:
        """B278/G-C9: the wait is in 0.5 s slices; it ends as soon as the run's cancel flag is seen (the executor does not start, the engine checks)."""
        son = time.monotonic() + float(os.environ.get("CADRE_KILIT_BEKLEME_SN", "5"))
        bayrak = self.store.path(self.run_id) / "cancel.flag"
        while (kalan := son - time.monotonic()) > 0 and not bayrak.exists():
            time.sleep(min(.5, kalan))

    def save(self, **changes: Any) -> None:
        self.data.update(changes)
        atomic_json(self.path, self.data)

    def launch(self, command: list[str], wt: str, log_path: Path, env: dict[str, str], fds: tuple[int, ...]) -> RecordedProcess:
        proc = None
        handed_off = False
        try:
            assert self.lease.fd is not None
            with launch_guard(self.store, self.run_id, self.task_id if self.role in ('review', 'plan') else '', executing=True):
                record = self.store.read(self.run_id)
                if record.get('request'):
                    from kosu import istek_suresi_doldu, request_authorized
                    if not request_authorized(record, self.data['model']):
                        raise ValueError('request-model-permission-required')
                    if istek_suresi_doldu(record):
                        raise ValueError('request-time-limit')
                env = dict(env)
                policy = project_tool_policy(self.store, record, self.directory, wt, self.adapter, self.role, env)
                if policy is not None:
                    self.tool_policy = policy
                    env.update(CADRE_PROJECT_TOOLS='1', CADRE_TOOL_POLICY=json.dumps(policy), SHELL='/bin/sh',
                               OPENCODE_DISABLE_PROJECT_CONFIG='true', OPENCODE_DISABLE_AUTOUPDATE='true',
                               OPENCODE_DISABLE_LSP_DOWNLOAD='true', OPENCODE_DISABLE_DEFAULT_PLUGINS='true',
                               OPENCODE_DISABLE_EXTERNAL_SKILLS='true', OPENCODE_DISABLE_SHARE='true')
                    for name in ('BASH_ENV', 'ENV', 'ZDOTDIR'):
                        env.pop(name, None)
                    if record.get('tool_boundary_wait'):
                        record.update(tool_boundary_wait=None, reason=None, updated=time.time())
                        atomic_json(self.store.path(self.run_id) / 'run.json', record)
                if self.adapter == 'opencode':
                    try:
                        loader_environment(self.directory, env, self.loader_policy)
                    except OSError as error:
                        if isinstance(error, PermissionError) and str(error).startswith('project-tool-'):
                            raise
                        raise PermissionError('project-tool-loader-config-unavailable') from None
                with self.stdout_path.open("wb") as output, open(log_path, "a") as errors:
                    registered = [sys.executable, str(Path(__file__).resolve()), 'executor-exec', str(self.path), str(self.lease.fd), *command]
                    proc = (OC.launch if self.adapter == "opencode" else subprocess.Popen)(registered, cwd=wt, env=env, stdin=subprocess.DEVNULL,
                                        stdout=output, stderr=errors, pass_fds=(*fds, self.lease.fd), start_new_session=True)
            self.save(pid=proc.pid)
            handed_off = True
            return RecordedProcess(proc, self, Path(log_path))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            if proc is None:
                self.save(outcome="not_started")
                self.store.task_mutate(self.run_id, self.task_id,
                                       lambda task: task["executors"][self.key].update(resume_required=False))
                if isinstance(exc, PermissionError) and str(exc).startswith('project-tool-'):
                    self.store.update(self.run_id, tool_boundary_wait=str(exc), reason=str(exc))
            else:
                # The child may already have spent money. Keep outcome=unknown.
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            raise
        finally:
            try:
                if not handed_off and (proc is None or proc.poll() is not None):
                    finish_tool_artifacts(self.store, self.path, self.data, self.tool_policy, self.loader_policy)
            finally:
                self.lease.close()  # The executor owns the inherited descriptor now.


class RecordedProcess:
    def __init__(self, proc: subprocess.Popen[bytes], attempt: Attempt, log_path: Path) -> None:
        self.proc, self.attempt, self.log_path = proc, attempt, log_path
        self.pid, self.args = proc.pid, proc.args
        self.stdout = None
        self.offset = 0
        self.finished = False

    @property
    def returncode(self) -> int | None:
        return self.proc.returncode

    def refresh(self, final: bool = False) -> None:
        if self.finished:
            return
        try:
            field = "sessionID" if self.attempt.adapter == "opencode" else "session_id"
            with self.attempt.stdout_path.open("rb") as stream:
                stream.seek(self.offset)
                while line := stream.readline():
                    if not line.endswith(b"\n"):
                        break
                    self.offset = stream.tell()
                    with self.log_path.open("ab") as log:
                        log.write(line)
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if isinstance(event, dict) and isinstance(event.get(field), str):
                        sid = event[field]
                        if sid != self.attempt.data.get("session_id"):
                            self.attempt.bind(sid, self.attempt.data, self.attempt.path)
            if final:
                try:
                    self.attempt.reconcile(self.attempt.data, self.attempt.path)
                    self.attempt.save(outcome="finished", exit_code=self.returncode, finished=time.time())
                    if not self.attempt.data.get("session_id") and self.returncode == 0:
                        raise SessionRecoveryError("successful executor returned no session receipt")
                finally:
                    finish_tool_artifacts(self.attempt.store, self.attempt.path, self.attempt.data,
                                          self.attempt.tool_policy, self.attempt.loader_policy, observed_pid=self.pid)
                self.finished = True
        except SessionRecoveryError:
            self.attempt.save(identity_error=True)
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
            finish_tool_artifacts(self.attempt.store, self.attempt.path, self.attempt.data,
                                  self.attempt.tool_policy, self.attempt.loader_policy, observed_pid=self.pid)
            raise

    def poll(self) -> int | None:
        rc = self.proc.poll()
        self.refresh(final=rc is not None)
        return rc

    def wait(self, timeout: float | None = None) -> int:
        rc = self.proc.wait(timeout=timeout)
        self.refresh(final=True)
        return rc

    def output(self) -> str:
        return self.attempt.stdout_path.read_text(errors="replace")

    def communicate(self, input: None = None, timeout: float | None = None) -> tuple[str, None]:
        if input is not None:
            raise ValueError("recorded executor accepts prompt in argv only")
        self.wait(timeout=timeout)
        return self.output(), None

    def kill(self) -> None:
        self.proc.kill()

    def terminate(self) -> None:
        self.proc.terminate()


def executor_exec(path: Path, lease_fd: int, command: list[str]) -> int:
    data = bounded_json(path)
    store = RunStore()
    expected = store.task_dir(data['run_id'], data['task_id']) / 'attempts' / identifier(data['attempt_id']) / 'attempt.json'
    if path != expected:
        return 75
    lock_path = expected.parents[2] / f".{identifier(data['adapter'])}-{identifier(data['role'])}.lock"
    inherited, lock = os.fstat(lease_fd), lock_path.stat()
    if (inherited.st_dev, inherited.st_ino) != (lock.st_dev, lock.st_ino):
        return 75
    identity = process_identity(os.getpid())
    if identity is None:
        return 75
    with launch_guard(store, data['run_id'], data['task_id'] if data['role'] in ('review', 'plan') else '', executing=True):
        record = store.read(data['run_id'])
        if record.get('owner_identity') and process_identity(record['owner_pid']) != record['owner_identity']:
            return 75
        if record.get('request'):
            from kosu import istek_suresi_doldu, request_authorized
            if not request_authorized(record, data['model']) or istek_suresi_doldu(record):
                return 75
        receipt = {key: data[key] for key in ('run_id', 'task_id', 'attempt_id')}
        policy = health_json(store, path.parent / 'tool-policy.json') if (path.parent / 'tool-policy.json').exists() else {}
        atomic_json(path.parent / 'process.json', {**receipt, 'process_identity': identity, 'scope_digest': tool_scope_digest(policy)})
    os.execvpe(command[0], command, dict(os.environ))
    return 75


def tool_exec(home: Path, path: Path, lease_fd: int, command: list[str]) -> int:
    store = RunStore(home)
    data = health_json(store, path)
    lock_path, binding = tool_scope(store, path, data)
    with health_file(store, lock_path) as fd:
        lock = os.fstat(fd)
    inherited = os.fstat(lease_fd)
    if (inherited.st_dev, inherited.st_ino) != (lock.st_dev, lock.st_ino):
        return 75
    identity = process_identity(os.getpid())
    observer = data.get('observer_identity')
    observer_pid = observer.get('pid') if isinstance(observer, dict) else None
    if identity is None or type(observer_pid) is not int or process_identity(observer_pid) != observer:
        return 75
    with launch_guard(store, data['run_id'], executing=True):
        policy = health_json(store, path.parent / 'tool-policy.json')
        atomic_json(path.parent / 'process.json', {**binding, 'process_identity': identity, 'scope_digest': tool_scope_digest(policy)})
    os.execvpe(command[0], command, dict(os.environ))
    return 75


if __name__ == '__main__':
    try:
        if sys.argv[1:2] == ['tool-exec']:
            raise SystemExit(tool_exec(Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4]), sys.argv[5:]))
        if sys.argv[1:2] != ['executor-exec']:
            raise ValueError('internal-command-required')
        raise SystemExit(executor_exec(Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4:]))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        raise SystemExit(75)
