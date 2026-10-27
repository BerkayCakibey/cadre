"""Verified filesystem snapshots of worktrees, including untracked and task-memory bytes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from typing import Any

from kosu import RunStore, atomic_json, sync_directory


def protect_git_registration(tree: Path, run_id: str, *, release: bool = False) -> None:
    """Keep archived worktree metadata reachable across Git's prune/GC."""
    if not (tree/'.git').is_file():
        return
    result = subprocess.run(['git', '-C', str(tree), 'rev-parse', '--absolute-git-dir'],
                            capture_output=True, text=True, timeout=15, check=True)
    lock = Path(result.stdout.strip())/'locked'
    reason = 'cadre-archive ' + run_id
    existing = lock.read_text().strip() if lock.exists() else None
    if release:
        if existing in (reason, 'qwen-archive ' + run_id):   # locks are also released before the rename
            subprocess.run(['git', '-C', str(tree), 'worktree', 'unlock', str(tree)],
                           check=True, capture_output=True, timeout=15)
    elif existing != reason:
        subprocess.run(['git', '-C', str(tree), 'worktree', 'lock', '--reason', reason, str(tree)],
                       check=True, capture_output=True, timeout=15)
        with lock.open('rb') as stream:
            os.fsync(stream.fileno())
        sync_directory(lock.parent)


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def index(root: Path, skip: Any = None) -> dict[str, dict[str, Any]]:
    """`skip(relative_dir_path) -> bool` prunes whole directories (ledger use: build/cache dirs are not product).
    Rotation passes no `skip`: its archive must be byte-exact for restore."""
    from sonuc import protected_source
    if root.is_symlink() or not root.is_dir():
        raise ValueError('snapshot root must be a real directory')
    entries = {}
    def fail(error: OSError) -> None:
        raise error
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        if skip is not None:
            dirs[:] = [d for d in dirs if not skip((Path(directory)/d).relative_to(root).as_posix())]
        for name in sorted(dirs + files):
            path = Path(directory)/name
            info = path.lstat()
            relative = path.relative_to(root).as_posix()
            if protected_source(relative):
                raise PermissionError('archive-protected-source')
            row: dict[str, Any] = {'mode':stat.S_IMODE(info.st_mode)}
            if stat.S_ISLNK(info.st_mode):
                row.update(kind='symlink', target=os.readlink(path))
                if name in dirs:
                    dirs.remove(name)
            elif stat.S_ISDIR(info.st_mode):
                row.update(kind='dir')
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise PermissionError('archive-hardlink-source')
                row.update(kind='file', sha256=digest(path), size=info.st_size)
            else:
                raise ValueError('special files cannot be safely archived')
            entries[relative] = row
    return entries


def fingerprint(entries: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()


def capture(root: Path, destination: Path, skip: Any = None) -> Path:
    if root.is_symlink():
        raise ValueError('snapshot root cannot be a symlink')
    root, destination = root.resolve(), destination.resolve()
    if destination.is_relative_to(root):
        raise ValueError('archive cannot be inside its source')
    entries = index(root, skip)
    blobs = destination/'blobs'
    blobs.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, row in entries.items():
        if row['kind'] != 'file':
            continue
        target = blobs/row['sha256']
        if target.exists() and digest(target) == row['sha256']:
            continue
        fd, temporary = tempfile.mkstemp(dir=blobs, prefix='.blob-')
        try:
            with os.fdopen(fd, 'wb') as output, (root/name).open('rb') as source:
                shutil.copyfileobj(source, output, 1024*1024)
                output.flush()
                os.fsync(output.fileno())
            if digest(Path(temporary)) != row['sha256']:
                raise OSError('source changed while archiving')
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    sync_directory(blobs)
    if index(root, skip) != entries:
        raise OSError('source changed while archiving')
    ident = fingerprint(entries)
    manifest = destination/(ident + '.json')
    atomic_json(manifest, {'version':1, 'fingerprint':ident, 'entries':entries})
    verify(manifest)
    return manifest


def verify(manifest: Path) -> dict[str, dict[str, Any]]:
    from sonuc import protected_source
    data = json.loads(manifest.read_text())
    entries = data['entries']
    if data.get('version') != 1 or fingerprint(entries) != data['fingerprint']:
        raise ValueError('invalid archive manifest')
    for name, row in entries.items():
        if protected_source(name):
            raise PermissionError('archive-protected-source')
        path = Path(name)
        if path.is_absolute() or not path.parts or any(p in ('.', '..') for p in path.parts):
            raise ValueError('invalid archive path')
        for parent in path.parents:
            if str(parent) != '.' and entries.get(parent.as_posix(), {}).get('kind') != 'dir':
                raise ValueError('archive path traverses a non-directory')
        if type(row.get('mode')) is not int or not 0 <= row['mode'] <= 0o7777:
            raise ValueError('invalid archive mode')
        if row['kind'] == 'file':
            sha = row['sha256']
            if not isinstance(sha, str) or len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha):
                raise ValueError('invalid blob identity')
            blob = manifest.parent/'blobs'/sha
            if blob.is_symlink() or blob.stat().st_size != row['size'] or digest(blob) != sha:
                raise OSError('archive blob missing or damaged')
        elif row['kind'] == 'symlink':
            if not isinstance(row['target'], str) or '\x00' in row['target']:
                raise ValueError('invalid archived link')
        elif row['kind'] != 'dir':
            raise ValueError('unsupported archive entry')
    return entries


def restore(manifest: Path, destination: Path) -> None:
    entries = verify(manifest)
    if destination.exists() or destination.is_symlink():
        raise ValueError('restore destination must not exist')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.restore-', dir=destination.parent))
    try:
        for name, row in sorted(entries.items(), key=lambda item: (len(Path(item[0]).parts), item[0])):
            path = temporary/name
            if row['kind'] == 'dir':
                path.mkdir()
            elif row['kind'] == 'file':
                with (manifest.parent/'blobs'/row['sha256']).open('rb') as source, path.open('xb') as output:
                    shutil.copyfileobj(source, output, 1024*1024)
                    output.flush()
                    os.fsync(output.fileno())
                path.chmod(row['mode'])
            else:
                path.symlink_to(row['target'])  # Links are never traversed.
        for name, row in sorted(entries.items(), reverse=True):
            if row['kind'] == 'dir':
                (temporary/name).chmod(row['mode'])
                sync_directory(temporary/name)
        if index(temporary) != entries:
            raise OSError('restored tree verification failed')
        sync_directory(temporary)
        os.rename(temporary, destination)
        sync_directory(destination.parent)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def restore_run_worktree(store: RunStore, run_id: str) -> None:
    """Caller holds the run owner lock. Only completed runs can be rotated."""
    record = store.read(run_id)
    cleanup = record.get('cleanup')
    if not cleanup:
        return
    manifest = Path(cleanup['worktree_archive'])
    verify(manifest)
    worktree = Path(record['worktree'])
    if not worktree.exists():
        restore(manifest, worktree)
    protect_git_registration(worktree, run_id, release=True)
    # A pre-rename crash leaves the original tree. Preserve any later edits;
    # the normal acceptance check will detect a changed product revision.
    store.update(run_id, cleanup=None)
