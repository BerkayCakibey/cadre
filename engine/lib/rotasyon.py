#!/usr/bin/env python3
"""Completed worktrees are frozen only with a verified archive and an exclusive lock; unknown/retryable/cancelled/active runs stay.
Run records and git records stay; an archived run can be reopened."""
from contextlib import ExitStack
from contextlib import closing
import gzip
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from typing import Any, Callable

from arsiv import capture, index, verify, protect_git_registration
from kosu import FileLock, RunStore, sync_directory


def backup_database(home: Path, db: Path, *, now: float | None = None) -> Path | None:
    """Preserve the weekly DB backup; publish before deleting any older backup."""
    moment = time.localtime(time.time() if now is None else now)
    if moment.tm_wday != 6 or not db.is_file():
        return None
    folder = home/'backups/opencode-db'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with FileLock(folder/'.backup.lock'):
        target = folder/time.strftime('opencode-%Y%m%d.db.gz', moment)
        if target.exists():
            cleanup_backups(home)
            return target
        with tempfile.TemporaryDirectory(prefix='.backup-', dir=folder) as temporary:
            copy = Path(temporary)/'copy.db'
            with closing(sqlite3.connect(db.resolve().as_uri()+'?mode=ro', uri=True)) as source, closing(sqlite3.connect(copy)) as dest:
                source.backup(dest)
                if dest.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise OSError('database backup verification failed')
            compressed = Path(temporary)/'copy.gz'
            with copy.open('rb') as source, compressed.open('wb') as raw:
                with gzip.GzipFile(fileobj=raw, mode='wb') as dest:
                    shutil.copyfileobj(source, dest)
                raw.flush()
                os.fsync(raw.fileno())
            # Verify decompression before publication/retention cleanup.
            from hashlib import file_digest
            with gzip.open(compressed, 'rb') as decoded, copy.open('rb') as original:
                if file_digest(decoded, 'sha256').digest() != file_digest(original, 'sha256').digest():
                    raise OSError('compressed backup verification failed')
            os.replace(compressed, target)
            sync_directory(folder)
        cleanup_backups(home)
        sync_directory(folder)
        return target


def backup_delete_details(home: Path, path: Path):
    import re
    folder = home / 'backups/opencode-db'
    if (any(parent.is_symlink() for parent in (folder, folder.parent)) or path.is_symlink()
            or path.resolve().parent != folder.resolve()):
        raise PermissionError('operator-backup-target-invalid')
    path = path.resolve(strict=True)
    info = path.stat()
    if not re.fullmatch(r'opencode-\d{8}[.]db[.]gz', path.name) or info.st_nlink != 1:
        raise PermissionError('operator-backup-target-invalid')
    return [['cadre', 'cleanup-backup', str(path)]], {'path': str(path), 'identity': [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]}, {}


def backup_delete_proposal(home: Path, path: Path):
    from izin import propose
    return propose(RunStore(home), 'system', 'cleanup', *backup_delete_details(home, path))


def backup_delete_execute(home: Path, token: str) -> None:
    from izin import read_action, identity, permitted
    store = RunStore(home)
    action = read_action(store, 'system', token)
    path = Path(action['identity']['target']['path'])
    def current():
        return identity(store, 'system', 'cleanup', *backup_delete_details(home, path))
    with permitted(store, 'system', token, current):
        path.unlink()
        sync_directory(path.parent)


def cleanup_backups(home: Path) -> None:
    for old in sorted((home / 'backups/opencode-db').glob('opencode-????????.db.gz'))[:-4]:
        action = backup_delete_proposal(home, old)
        if action['status'] == 'approved':
            backup_delete_execute(home, action['token'])


def rotate(home: Path, *, keep: int = 5, now: float | None = None, run_id: str | None = None) -> dict[str, int]:
    now = time.time() if now is None else now
    store = RunStore(home)
    counts = {'archived':0, 'retained':0, 'errors':0}
    with FileLock(home/'locks/rotation.lock'):
        records = store.list()
        by_path = {}
        for record in records:
            if record.get('worktree'):
                by_path.setdefault(str(Path(record['worktree']).resolve()), []).append(record)
        root = home/'worktrees'
        for project in root.iterdir() if root.exists() else ():
            if project.is_symlink() or not project.is_dir():
                continue
            trees = sorted((p for p in project.iterdir() if p.is_dir() and not p.is_symlink()),
                           key=lambda p:p.stat().st_mtime, reverse=True)
            for tree in trees:
                owners = by_path.get(str(tree.resolve()), [])
                if tree in trees[:keep] or (run_id is None and now-tree.stat().st_mtime <= 2*86400) or len(owners) != 1:
                    counts['retained'] += 1
                    continue
                record = owners[0]
                if run_id is not None and record['run_id'] != run_id:
                    counts['retained'] += 1
                    continue
                action = None
                retry_current: Callable[[], dict[str, Any]] | None = None
                try:
                    with ExitStack() as held:
                        owner = FileLock(store.path(record['run_id'])/'.owner.lock')
                        if not owner.acquire(blocking=False):
                            counts['retained'] += 1
                            continue
                        held.callback(owner.close)
                        record = store.read(record['run_id'])
                        if record['state'] != 'completed' or Path(record['worktree']).resolve() != tree.resolve():
                            counts['retained'] += 1
                            continue
                        busy = False
                        lock_paths = []
                        for task in (store.path(record['run_id'])/'tasks').glob('*'):
                            lock_paths += [task/'.owner.lock', *task.glob('.opencode-*.lock'), *task.glob('.claude-*.lock')]
                        for lock_path in lock_paths:
                            task_lock = FileLock(lock_path)
                            if not task_lock.acquire(blocking=False):
                                busy = True
                                break
                            held.callback(task_lock.close)
                        if busy:
                            counts['retained'] += 1
                            continue
                        from izin import cleanup_proposal, cleanup_details, identity, permitted
                        action = cleanup_proposal(store, record['run_id'], tree)
                        if action['status'] != 'approved':
                            counts['retained'] += 1
                            continue
                        def current_action():
                            return identity(store, record['run_id'], 'cleanup', *cleanup_details(store, record['run_id'], tree))
                        retry_current = current_action
                        with permitted(store, record['run_id'], action['token'], current_action):
                            archive = home/'archives'/record['run_id']
                            archive.mkdir(parents=True, exist_ok=True, mode=0o700)
                            manifest = capture(tree, archive/'worktree')
                            run_manifest = capture(store.path(record['run_id']), archive/'run')
                            entries = verify(manifest)
                            verify(run_manifest)
                            if index(tree) != entries:
                                raise OSError('worktree changed before cleanup')
                            staging = home/'cleanup'/(record['run_id']+'-'+uuid.uuid4().hex)
                            staging.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                            # Recovery coordinates reach stable storage before the
                            # original path can disappear. A crash during rmtree
                            # leaves staging debris, never a half-deleted worktree.
                            store.update(record['run_id'], cleanup={'worktree_archive':str(manifest),
                                         'run_archive':str(run_manifest), 'staging':str(staging)})
                            protect_git_registration(tree, record['run_id'])
                            os.rename(tree, staging)
                            sync_directory(tree.parent)
                            sync_directory(staging.parent)
                            shutil.rmtree(staging)
                            sync_directory(staging.parent)
                            counts['archived'] += 1
                except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
                    counts['errors'] += 1
                    print(f"rotation retained/recoverable: {record['run_id']}: {type(error).__name__}")
                    if action and retry_current is not None and tree.exists() and not store.read(record['run_id']).get('cleanup'):
                        from izin import no_effect
                        try:
                            no_effect(store, record['run_id'], action['token'], retry_current)
                        except (OSError, ValueError, KeyError):
                            pass
    return counts


def main() -> int:
    home = Path(os.environ.get('CADRE_HOME', str(Path.home()/'.local/share/cadre-engineering')))
    # Failed ledgers must not permit cleanup. Importing this module does no I/O.
    from kosu_defteri import tara
    tara()
    db = Path(os.environ.get('OPENCODE_DB', str(Path.home()/'.local/share/opencode/opencode.db')))
    backup_database(home, db)
    result = rotate(home)
    print(result)
    return 1 if result['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
