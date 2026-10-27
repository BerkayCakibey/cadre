"""Manifest-based transfer between the repository and the runtime home."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import signal
import shlex
import stat
import subprocess
import sys
import time
from typing import Any, Iterator
import uuid


CODE = Path(__file__).resolve().parents[1]
GUARDS = ('saglik-nobet', 'kota-izle')


def sensitive(path: Path) -> bool:
    return any(part == '.env' or part.startswith(('.env.', 'credentials', 'id_rsa', 'id_ed25519'))
               or part.endswith(('.pem', '.key')) for part in path.parts)


@contextmanager
def parent_fd(path: Path, create: bool = False) -> Iterator[int]:
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('sync-path-invalid')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = following
        yield fd
    finally:
        os.close(fd)


def regular(path: Path) -> bytes | None:
    try:
        with parent_fd(path) as directory:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('sync-nonregular')
        return stream.read()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def generation(root: Path) -> bytes | None:
    return regular(root / '.runtime-generation')


LOADED_GENERATION = generation(CODE)


def lock_gate(path: Path, shared: bool = False, *, directory: bool = False) -> Any:
    from kosu import FileLock
    with parent_fd(path / 'entry' if directory else path, create=True) as parent:
        fd = os.dup(parent) if directory else os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1):
            raise ValueError('sync-lock-invalid')
        fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    lock = FileLock(path, shared=shared)
    lock.fd = fd
    return lock


@contextmanager
def runtime_gate(home: Path, root: Path = CODE, *, allow_restart: bool = False) -> Iterator[None]:
    home = home.resolve()
    lock = lock_gate(home, shared=True, directory=True)
    if lock is None:
        raise ValueError('runtime-sync-active')
    try:
        for name in ('.sync-pending.json', *(() if allow_restart else ('runtime-restart.json',))):
            try:
                pending = regular(home / 'zincir' / name)
            except (OSError, ValueError):
                raise ValueError('runtime-sync-incomplete') from None
            if pending is not None:
                raise ValueError('runtime-sync-incomplete')
        if root.resolve() == CODE and generation(root) != LOADED_GENERATION:
            raise ValueError('runtime-generation-changed')
        yield
    finally:
        lock.close()


def runtime_operation(function: Any) -> Any:
    @wraps(function)
    def protected(first: Any, *args: Any, **kwargs: Any) -> Any:
        home = first if isinstance(first, Path) else first.home
        with runtime_gate(home):
            return function(first, *args, **kwargs)
    return protected


def guard_version(root: Path) -> str:
    values = {}
    for directory in ('lib', 'bin'):
        for path in sorted((root / directory).rglob('*')):
            if '__pycache__' in path.parts or sensitive(path) or path.is_dir():
                continue
            values[str(path.relative_to(root))] = digest(regular(path) or b'')
    return digest(json.dumps(values, sort_keys=True).encode())


def command_has(command: str, path: Path) -> bool:
    return any(Path(token).is_absolute() and Path(token).resolve() == path.resolve()
               for token in shlex.split(command))


def register_guard(home: Path, root: Path, name: str, pid: int) -> None:
    from kosu import process_identity, surec_komutu
    if name not in GUARDS or pid <= 1:
        raise ValueError('sync-guard-name')
    with runtime_gate(home, root, allow_restart=True):
        identity = process_identity(pid)
        script = root / 'bin' / (name + '.sh')
        if not identity or not command_has(surec_komutu(pid), script):
            raise ValueError('sync-guard-identity')
        settings = {key: os.environ[key] for key in
                    ('CADRE_BEKCI_SAHIP_PID', 'CADRE_AG_YOK', 'CADRE_NOBET_ARALIK', 'CADRE_KOTA_ARALIK')
                    if key in os.environ}
        json_write(home / 'zincir' / (name + '.runtime.json'),
                    {'name': name, 'identity': identity, 'root': str(root),
                     'version': guard_version(root), 'settings': settings})


def targets(repo: Path, home: Path, config: Path, binary: Path) -> dict[str, dict[str, Any]]:
    values = {}
    for source, target in ((repo / 'engine/lib', home / 'lib'), (repo / 'engine/bin', home / 'bin'),
                           (repo / 'engine/hooks', home / 'hooks'), (repo / 'engine/tests', home / 'tests'),
                           (repo / 'config/agents', config / 'agents'), (repo / 'config/skills', config / 'skills'),
                           (repo / 'config/command', config / 'command')):
        if not source.is_dir() or source.is_symlink():
            raise ValueError('sync-source-missing')
        for path in sorted(source.rglob('*')):
            if '__pycache__' in path.parts or (path.is_dir() and not path.is_symlink()):
                continue
            if sensitive(path):
                raise ValueError('sync-sensitive-source')
            data = regular(path)
            if data is None:
                raise ValueError('sync-source-changed')
            values[str(target / path.relative_to(source))] = {'sha256': digest(data),
                       'mode': stat.S_IMODE(path.stat().st_mode), 'source': str(path)}
    for source, target in ((repo / 'engine/USAGE.md', home / 'USAGE.md'),
                           (repo / 'config/AGENTS.md', config / 'AGENTS.md')):
        data = regular(source)
        if data is None:
            raise ValueError('sync-source-missing')
        values[str(target)] = {'sha256': digest(data), 'mode': stat.S_IMODE(source.stat().st_mode), 'source': str(source)}
    data = ('#!/usr/bin/env bash\n'
            + 'if [ -z "${CADRE_HOME:-}" ]; then export CADRE_HOME=' + shlex.quote(str(home)) + '; fi\n'
            + 'if [ -z "${OPENCODE_CFG:-}" ]; then export OPENCODE_CFG=' + shlex.quote(str(config)) + '; fi\n'
            + 'exec bash ' + shlex.quote(str(home / 'bin/cadre.sh')) + ' "$@"\n').encode()
    values[str(binary / 'cadre')] = {'sha256': digest(data), 'mode': 0o755, 'data': data.decode()}
    return values


def read_manifest(home: Path, config: Path, binary: Path) -> dict[str, Any]:
    data = regular(home / 'zincir/runtime-manifest.json')
    if data is None:
        return {'files': {}, 'guards': {}, 'scope': [str(home), str(config), str(binary)]}
    value = json.loads(data)
    if (not isinstance(value, dict) or value.get('scope') != [str(home), str(config), str(binary)]
            or not isinstance(value.get('files'), dict)):
        raise ValueError('sync-manifest-invalid')
    allowed = (home / 'lib', home / 'bin', home / 'hooks', home / 'tests',
               config / 'agents', config / 'skills', config / 'command')
    for name, info in value['files'].items():
        path = Path(name)
        if (not path.is_absolute() or '..' in path.parts or not isinstance(info, dict)
                or not isinstance(info.get('sha256'), str) or len(info['sha256']) != 64
                or type(info.get('mode')) is not int
                or not (any(path.is_relative_to(base) and path != base for base in allowed)
                        or path in (home / 'USAGE.md', config / 'AGENTS.md', binary / 'cadre'))):
            raise ValueError('sync-manifest-invalid')
    return value


def plan(repo: Path, home: Path, config: Path, binary: Path) -> dict[str, Any]:
    desired = targets(repo, home, config, binary)
    previous = read_manifest(home, config, binary)
    changes, conflicts, preserved = [], [], []
    for name in sorted(desired.keys() | previous['files'].keys()):
        path, new, old = Path(name), desired.get(name), previous['files'].get(name)
        try:
            data = regular(path)
        except (OSError, ValueError):
            conflicts.append({'path': name, 'reason': 'unreadable-or-link'})
            continue
        current = {'sha256': digest(data), 'mode': stat.S_IMODE(path.stat().st_mode)} if data is not None else None
        expected = {key: new[key] for key in ('sha256', 'mode')} if new else None
        if current == expected:
            continue
        if old is None and current is not None:
            conflicts.append({'path': name, 'reason': 'ownership-unknown'})
        elif old is not None and current not in (None, old):
            if new is None:
                preserved.append(name)
            else:
                conflicts.append({'path': name, 'reason': 'managed-file-modified'})
        else:
            changes.append({'path': name, 'before': current, 'after': expected})
    return {'changes': changes, 'conflicts': conflicts, 'preserved': preserved,
            'desired': desired, 'previous': previous, 'version': guard_version(repo / 'engine')}


def valid_guard(record: Any, home: Path, name: str) -> bool:
    return (isinstance(record, dict) and record.get('name') == name and record.get('root') == str(home)
            and isinstance(record.get('identity'), dict) and isinstance(record.get('version'), str)
            and type(record['identity'].get('pid')) is int and record['identity']['pid'] > 1
            and record['identity'].get('pgid') == record['identity']['pid']
            and isinstance(record['identity'].get('started'), str) and bool(record['identity']['started'])
            and isinstance(record.get('settings'), dict) and all(isinstance(value, str) for value in record['settings'].values())
            and not record['settings'].keys() - {'CADRE_BEKCI_SAHIP_PID', 'CADRE_AG_YOK', 'CADRE_NOBET_ARALIK', 'CADRE_KOTA_ARALIK'})


def guard_records(home: Path, changed: bool, version: str) -> dict[str, Any]:
    from kosu import bekci_pid, process_identity, surec_komutu
    records = {}
    for name in GUARDS:
        pid = bekci_pid(home, name)
        if not pid or not process_identity(pid):
            continue
        data = regular(home / 'zincir' / (name + '.runtime.json'))
        if not data:
            if changed:
                raise ValueError('sync-guard-owner-unknown')
            continue
        record = json.loads(data)
        if (not valid_guard(record, home, name)
                or record.get('identity') != process_identity(pid)
                or not command_has(surec_komutu(pid), home / 'bin' / (name + '.sh'))):
            raise ValueError('sync-guard-owner-unknown')
        if record.get('version') != version:
            records[name] = record
    return records


def stop_guard(record: dict[str, Any]) -> None:
    from kosu import process_identity
    identity = record['identity']
    pid, group = identity['pid'], identity['pgid']
    if pid == os.getpid() or group != pid or process_identity(pid) != identity:
        raise ValueError('sync-guard-group-unknown')
    os.killpg(group, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        scan = subprocess.run(['ps', '-axo', 'pgid=,stat='], capture_output=True, text=True, check=True, timeout=10)
        if not any(parts[0] == str(group) and not parts[1].startswith('Z')
                   for line in scan.stdout.splitlines() if len(parts := line.split()) >= 2):
            return
        time.sleep(.025)
    raise ValueError('sync-guard-stop-timeout')


def process_busy(home: Path) -> bool:
    output = subprocess.run(['ps', '-axo', 'pid=,command='], check=True, capture_output=True, text=True, timeout=10).stdout
    for line in output.splitlines():
        pieces = line.split(maxsplit=1)
        if len(pieces) != 2 or pieces[0] == str(os.getpid()):
            continue
        command = pieces[1]
        roots = {str(home), os.environ.get('CADRE_HOME', str(home))}
        if any(str(Path(root) / name) in command for root in roots
               for name in ('lib/qe_core.py', 'bin/cadre-dispatch.sh', 'bin/is-motoru.sh')):
            return True
        if any(str(Path(root) / 'lib/kosu.py') in command for root in roots) and any(' ' + name + ' ' in command for name in ('run', 'execute', 'phase-exec', 'start')):
            return True
    return False


def write_target(path: Path, data: bytes | None, mode: int = 0o600) -> None:
    regular(path)
    with parent_fd(path, create=True) as directory:
        if data is None:
            try:
                os.unlink(path.name, dir_fd=directory)
            except FileNotFoundError:
                pass
        else:
            temporary = '.sync-' + uuid.uuid4().hex
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=directory)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fchmod(stream.fileno(), mode)
                    os.fsync(stream.fileno())
                os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
        os.fsync(directory)


def json_write(path: Path, value: Any) -> None:
    write_target(path, json.dumps(value, sort_keys=True, allow_nan=False).encode())


def incomplete_hint(home: Path) -> str:
    """FIX-10 (N-ENV04): a half-done sync/restart file and the completion command are reported together."""
    pending = [str(home / 'zincir' / name) for name in ('.sync-pending.json', 'runtime-restart.json')
               if (home / 'zincir' / name).is_symlink() or (home / 'zincir' / name).exists()]
    if not pending:
        return ''
    return 'runtime sync yarım kaldı: ' + ', '.join(pending) + ' — tamamlamak için repo kökünde ./sync.sh push çalıştır'


def adopt(repo: Path, home: Path, config: Path, binary: Path, token: str | None = None) -> dict[str, Any]:
    """FIX-08 (N-ENV04): live files without a manifest are adopted in two steps; the live file is not touched, the gates are the same as push."""
    from kosu import RunStore, active_runs
    operation = lock_gate(home / 'zincir/.sync-operation.lock')
    if operation is None:
        raise ValueError('runtime-sync-active')
    try:
        lock = lock_gate(home, directory=True)
    except BaseException:
        operation.close()
        raise
    if lock is None:
        operation.close()
        raise ValueError('runtime-sync-active')
    try:
        busy = active_runs(RunStore(home))
        if busy:
            raise ValueError('sync-active-run:' + ','.join(busy))
        if process_busy(home):
            raise ValueError('sync-live-process')
        if regular(home / 'zincir/.sync-pending.json') is not None:
            raise ValueError('runtime-sync-incomplete')
        work = plan(repo, home, config, binary)
        paths = sorted(item['path'] for item in work['conflicts']
                       if item['reason'] == 'ownership-unknown' and item['path'] in work['desired'])
        proof = digest(json.dumps(paths).encode())
        if token is None:
            return {'status': 'PENDING', 'paths': paths, 'token': proof}
        if not paths or token != proof:
            raise ValueError('sync-adopt-token-mismatch')
        files = dict(work['previous']['files'])
        for name in paths:
            data = regular(Path(name))
            if data is None:
                raise ValueError('sync-target-changed')
            files[name] = {'sha256': digest(data), 'mode': stat.S_IMODE(Path(name).stat().st_mode)}
        manifest: dict[str, Any] = {'scope': [str(home), str(config), str(binary)], 'files': files}
        if work['previous'].get('version'):
            manifest['version'] = work['previous']['version']
        json_write(home / 'zincir/runtime-manifest.json', manifest)
        return {'status': 'PASS', 'adopted': paths}
    finally:
        lock.close()
        operation.close()


def recover(home: Path, config: Path, binary: Path) -> dict[str, Any]:
    pending = home / 'zincir/.sync-pending.json'
    data = regular(pending)
    if data is None:
        return {}
    transaction = json.loads(data)
    if (not isinstance(transaction, dict) or transaction.get('scope') != [str(home), str(config), str(binary)]
            or not isinstance(transaction.get('entries'), list) or not isinstance(transaction.get('guards'), dict)
            or any(name not in GUARDS or not valid_guard(record, home, name) for name, record in transaction['guards'].items())):
        raise ValueError('sync-recovery-invalid')
    allowed = (home / 'lib', home / 'bin', home / 'hooks', home / 'tests', config / 'agents', config / 'skills', config / 'command')
    for entry in transaction['entries']:
        if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
            raise ValueError('sync-recovery-invalid')
        path = Path(entry['path'])
        if ('..' in path.parts or not (any(path.is_relative_to(base) and path != base for base in allowed)
                or path in (home / 'USAGE.md', config / 'AGENTS.md', binary / 'cadre',
                            home / 'zincir/runtime-manifest.json', home / '.runtime-generation'))
                or type(entry.get('mode')) is not int or not 0 <= entry['mode'] <= 0o777):
            raise ValueError('sync-recovery-invalid')
        backup = Path(entry['backup']) if entry.get('backup') else None
        if backup and (backup.parent.parent != home / 'zincir' or not backup.parent.name.startswith('.sync-stage-')
                       or '..' in backup.parts):
            raise ValueError('sync-recovery-invalid')
        for key in ('before_hash', 'after_hash'):
            value = entry.get(key)
            if value is not None and (not isinstance(value, str) or len(value) != 64
                                      or any(c not in '0123456789abcdef' for c in value)):
                raise ValueError('sync-recovery-invalid')
    for entry in reversed(transaction['entries']):
        path = Path(entry['path'])
        current = regular(path)
        observed = digest(current) if current is not None else None
        if observed == entry['before_hash'] and (current is None or stat.S_IMODE(path.stat().st_mode) == entry['mode']):
            continue
        if observed != entry['after_hash']:
            raise ValueError('sync-recovery-target-modified')
        backup = regular(Path(entry['backup'])) if entry['backup'] else None
        if (digest(backup) if backup is not None else None) != entry['before_hash']:
            raise ValueError('sync-recovery-backup-invalid')
        write_target(path, backup, entry['mode'])
    json_write(home / 'zincir/runtime-recovery.json', {'status': 'rolled-back', 'guards': transaction['guards']})
    if transaction['guards']:
        json_write(home / 'zincir/runtime-restart.json', transaction['guards'])
    pending.unlink()
    return transaction['guards']


def restart_guards(home: Path, guards: dict[str, Any]) -> None:
    from kosu import bekci_baslat, bekci_canli, process_identity
    for name, record in guards.items():
        if name not in GUARDS or not valid_guard(record, home, name):
            raise ValueError('sync-restart-invalid')
        data = regular(home / 'zincir' / (name + '.runtime.json'))
        latest = json.loads(data) if data else {}
        if (latest.get('version') == guard_version(home) and bekci_canli(home, name)
                and latest.get('identity') == process_identity(latest.get('identity', {}).get('pid'))):
            continue
        bekci_baslat(home, home, home / 'bin' / (name + '.sh'), settings=record['settings'])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            data = regular(home / 'zincir' / (name + '.runtime.json'))
            latest = json.loads(data) if data else {}
            if (latest.get('version') == guard_version(home) and bekci_canli(home, name)
                    and latest.get('identity') == process_identity(latest.get('identity', {}).get('pid'))):
                break
            time.sleep(.025)
        else:
            raise ValueError('sync-guard-restart-unverified')
    (home / 'zincir/runtime-restart.json').unlink(missing_ok=True)


def push(repo: Path, home: Path, config: Path, binary: Path) -> dict[str, Any]:
    from kosu import RunStore, active_runs
    if any(repo.is_relative_to(base) or base.is_relative_to(repo) for base in (home, config, binary)):
        raise ValueError('sync-scope-overlap')
    operation = lock_gate(home / 'zincir/.sync-operation.lock')
    if operation is None:
        raise ValueError('runtime-sync-active')
    try:
        lock = lock_gate(home, directory=True)
    except BaseException:
        operation.close()
        raise
    if lock is None:
        operation.close()
        raise ValueError('runtime-sync-active')
    guards = {}
    try:
        busy = active_runs(RunStore(home))
        if busy:
            raise ValueError('sync-active-run:' + ','.join(busy))
        if process_busy(home):
            raise ValueError('sync-live-process')
        guards.update(recover(home, config, binary))
        restart = regular(home / 'zincir/runtime-restart.json')
        if restart:
            saved = json.loads(restart)
            if not isinstance(saved, dict) or any(name not in GUARDS or not valid_guard(record, home, name) for name, record in saved.items()):
                raise ValueError('sync-restart-invalid')
            guards.update(saved)
        work = plan(repo, home, config, binary)
        if work['conflicts']:
            raise ValueError('sync-ownership-conflict')
        engine_changed = any(Path(item['path']).is_relative_to(home / directory)
                             for item in work['changes'] for directory in ('lib', 'bin'))
        guards.update(guard_records(home, engine_changed, work['version']))
        stage = home / 'zincir' / ('.sync-stage-' + uuid.uuid4().hex)
        with parent_fd(stage / 'entry', create=True):
            pass
        entries = []
        changed = list(work['changes'])
        manifest = {'scope': [str(home), str(config), str(binary)], 'files':
                    {name: {key: info[key] for key in ('sha256', 'mode')} for name, info in work['desired'].items()},
                    'version': work['version']}
        extra = {str(home / 'zincir/runtime-manifest.json'): json.dumps(manifest, sort_keys=True).encode(),
                 str(home / '.runtime-generation'): work['version'].encode()}
        for name, data in extra.items():
            if regular(Path(name)) != data:
                changed.append({'path': name})
        for index, item in enumerate(changed):
            path = Path(item['path'])
            before = regular(path)
            current = {'sha256': digest(before), 'mode': stat.S_IMODE(path.stat().st_mode)} if before is not None else None
            if 'before' in item and current != item['before']:
                raise ValueError('sync-target-changed')
            info = work['desired'].get(str(path))
            after = extra.get(str(path)) if str(path) in extra else (
                    regular(Path(info['source'])) if info and 'source' in info else
                    info['data'].encode() if info else None)
            if info and (after is None or digest(after) != info['sha256']):
                raise ValueError('sync-source-changed')
            backup, update = stage / (str(index) + '.before'), stage / (str(index) + '.after')
            if before is not None:
                write_target(backup, before)
            if after is not None:
                write_target(update, after)
            entries.append({'path': str(path), 'backup': str(backup) if before is not None else None,
                            'update': str(update) if after is not None else None,
                            'before_hash': digest(before) if before is not None else None,
                            'after_hash': digest(after) if after is not None else None,
                            'mode': stat.S_IMODE(path.stat().st_mode) if before is not None else 0o600,
                            'new_mode': info['mode'] if info else 0o600})
        if work['version'] != guard_version(repo / 'engine') or targets(repo, home, config, binary) != work['desired']:
            raise ValueError('sync-source-changed')
        json_write(home / 'zincir/.sync-pending.json', {'entries': entries, 'guards': guards,
                    'scope': [str(home), str(config), str(binary)]})
        try:
            from kosu import process_identity
            for record in guards.values():
                if process_identity(record['identity']['pid']) == record['identity']:
                    stop_guard(record)
            if active_runs(RunStore(home)) or process_busy(home):
                raise ValueError('sync-live-process')
            for entry in entries:
                current = regular(Path(entry['path']))
                if (digest(current) if current is not None else None) != entry['before_hash']:
                    raise ValueError('sync-target-changed')
                data = regular(Path(entry['update'])) if entry['update'] else None
                write_target(Path(entry['path']), data, entry['new_mode'])
            if guards:
                json_write(home / 'zincir/runtime-restart.json', guards)
            (home / 'zincir/.sync-pending.json').unlink()
        except BaseException:
            recover(home, config, binary)
            raise
        return {'status': 'PASS', 'changed': len(work['changes']), 'preserved': work['preserved'], 'restarted': sorted(guards)}
    finally:
        lock.close()
        try:
            if guards and not (home / 'zincir/.sync-pending.json').exists():
                restart_guards(home, guards)
        finally:
            operation.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('push', 'plan', 'fark', 'register', 'adopt'))
    parser.add_argument('args', nargs='*')
    args = parser.parse_args()
    home = Path(os.environ.get('CADRE_HOME', str(Path.home() / '.local/share/cadre-engineering'))).resolve()
    config = Path(os.environ.get('OPENCODE_CFG', str(Path.home() / '.config/opencode'))).resolve()
    binary = Path(os.environ.get('CADRE_BIN_DIR', str(Path.home() / '.local/bin'))).resolve()
    try:
        if args.command == 'register':
            register_guard(home, CODE, args.args[0], int(args.args[1]))
            return 0
        repo = CODE.parent
        if args.command == 'push':
            print(json.dumps(push(repo, home, config, binary), ensure_ascii=False))
        elif args.command == 'adopt':
            print(json.dumps(adopt(repo, home, config, binary, args.args[0] if args.args else None), ensure_ascii=False, indent=2))
        else:
            result = plan(repo, home, config, binary)
            hint = incomplete_hint(home)
            if hint:
                print(hint, file=sys.stderr)
            different = bool(result['changes'] or result['conflicts'] or regular(home / 'zincir/.sync-pending.json'))
            if args.command == 'plan':
                print(json.dumps({key: result[key] for key in ('changes', 'conflicts', 'preserved', 'version')}, ensure_ascii=False, indent=2))
            else:
                print('FARK VAR (yönetilen dosyalar)' if different else 'fark yok — repo == canlı')
                for item in result['changes'] + result['conflicts']:
                    path = Path(item['path'])
                    if path.is_relative_to(home):
                        print('engine/' + str(path.relative_to(home)))
                    elif path.is_relative_to(config):
                        print('config/' + str(path.relative_to(config)))
                    else:
                        print(str(path))
            return int(different)
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        error = sys.exception()
        safe = str(error) if isinstance(error, ValueError) and str(error).startswith(('sync-', 'runtime-')) else type(error).__name__
        label = 'canlı kökten motor/dispatcher süreci çalışıyor: ' if safe == 'sync-live-process' else ''
        print(args.command + ' DURDU: ' + label + safe, file=sys.stderr)
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
