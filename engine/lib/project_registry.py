"""Project registration records: repository path, base branch, policies."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ui_messages  # noqa: E402
from ui_messages import t  # noqa: E402


class RecordError(Exception):
    pass


class RecordPermission(RecordError):
    def __init__(self, token: str) -> None:
        super().__init__(t('The initial commit requires exact permission. The existing product is preserved.'))
        self.token = token


def _git(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(['git', '-C', str(path), *args], capture_output=True, text=True, timeout=30)


def enrolled(home: Path, root: Path) -> str | None:
    for p in sorted((home / 'projects').glob('*/repo-path')):
        if '--' in p.parent.name:
            continue
        try:
            if Path(p.read_text().strip()).resolve() == root:
                return p.parent.name
        except OSError:
            continue
    return None


def suitable_entry_name(home: Path, root: Path) -> str:
    base = re.sub(r'[^a-z0-9]+', '-', root.name.lower()).strip('-') or 'project_ref'
    entry_name, order = base, 2
    while (home / 'projects' / entry_name).exists():
        entry_name, order = f'{base}-{order}', order + 1
    return entry_name


def first_commit(path: Path) -> None:
    raise RecordError(t('The initial commit requires exact permission. The existing product is preserved.'))


def empty_started(path: Path) -> bool:
    """FIX-46: a folder containing only `.git` and no commit (interrupted between init and commit) continues like an empty folder.
    CR-33: `.git` + uncommitted skeleton files (matching the manifest) is the same state."""
    from permission import initialized_empty
    return initialized_empty(path)


def skeleton_selection(path: Path, round: str | None) -> dict[str, str] | None:
    """CR-33: if `--round` is given or a half-written skeleton manifest exists, the first commit contains the skeleton."""
    import project_skeleton
    if round is not None:
        if round not in project_skeleton.SUPPORT:
            raise RecordError(t('Unsupported project_ref type: {t}. Supported: {d}', t=round, d=', '.join(project_skeleton.SUPPORT)))
        return {'round': round, 'entry_name': path.name}
    pending = project_skeleton.manifest_read(path) if path.is_dir() else None
    return {'round': pending['round'], 'entry_name': pending['entry_name']} if pending else None


def prepare(path: Path, create: bool, home: Path | None = None, round: str | None = None) -> Path:
    path = path.expanduser()
    if round is not None and not create:
        raise RecordError(t('--round is only used with --create; no skeleton is written into an existing repository.'))
    if round is not None:
        skeleton_selection(path, round)
    if create:
        if path.exists() and (not path.is_dir() or (any(path.iterdir()) and not empty_started(path))):
            raise RecordError(t('This folder already exists and is not empty: {y}', y=path))
        ata = next((a for a in path.absolute().parents if a.is_dir()), None)
        parent_repo = _git(ata, 'rev-parse', '--show-toplevel') if ata else None
        if parent_repo is not None and parent_repo.returncode == 0:
            raise RecordError(t('The new folder would be inside another git repo ({d}). Choose another place with a full path.', d=parent_repo.stdout.strip()))
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise RecordError(t('Folder not found: {y}', y=path))
    path = path.resolve()
    parent = _git(path, 'rev-parse', '--show-toplevel')
    half = parent.returncode == 0 and Path(parent.stdout.strip()).resolve() == path and empty_started(path)
    if parent.returncode != 0 or half:
        if not half and any(path.iterdir()):
            raise RecordError(t('This folder is not a git repo and is not empty. Run git init and make a first commit, then try again.'))
        from permission import initialization_proposal, initialization_recover
        from run_store import RunStore
        store = RunStore(home or ui_messages.home_dir())
        try:
            if half:
                initialization_recover(store, path)
            action = initialization_proposal(store, path, skeleton_selection(path, round))
        except (subprocess.SubprocessError, PermissionError):
            raise RecordError(t('Git author identity is required. The initial commit was not created.')) from None
        raise RecordPermission(action['token'])
    root = Path(parent.stdout.strip()).resolve()
    if _git(root, 'rev-parse', '--verify', '-q', 'HEAD').returncode != 0:
        raise RecordError(t('The repo has no commits. Make one commit first, then try again.'))
    return root


def skeleton_record(home: Path, entry_name: str, root: Path) -> dict | None:
    """N-ENV01: in a skeleton project install/test/run go in one record (`projects/<entry_name>/project_skeleton.json`); running again does not duplicate the product."""
    import project_skeleton
    if project_skeleton.manifest_read(root) is None:
        return None
    outcome = project_skeleton.verify(root, home / 'projects' / entry_name)
    summary = ' · '.join(f"{a['entry_name']} rc={a['rc']}" for a in outcome['steps'])
    print(t('Skeleton flow {d}: {o}', d=outcome['status'], o=summary), file=sys.stderr)
    return outcome


def save(path: str | Path, create: bool = False, home: Path | None = None, entry_name: str | None = None, round: str | None = None) -> str:
    home = home or ui_messages.home_dir()
    root = prepare(Path(path), create, home, round)
    from permission import initialization_recover
    from run_store import RunStore
    initialization_recover(RunStore(home), root)
    present = enrolled(home, root)
    if present:
        skeleton_record(home, present, root)
        return present
    if entry_name and (home / 'projects' / entry_name).exists():
        raise RecordError(t('This project name is already in use: {a}', a=entry_name))
    entry_name = entry_name or suitable_entry_name(home, root)
    branch = _git(root, 'branch', '--show-current').stdout.strip() or 'main'
    directory = home / 'projects' / entry_name
    directory.mkdir(parents=True)
    (directory / 'repo-path').write_text(f'{root}\n')
    (directory / 'base-branch').write_text(f'{branch}\n')
    (directory / 'PROJECT.md').write_text(t('# {a}\n\nCadre registered this project on {z}. Write the architecture, rules and pitfalls here.\n',
                                        a=entry_name, z=time.strftime('%Y-%m-%d')))
    (directory / 'POLICY.md').write_text(t('# POLICY: {a}\n\n- Protected branch: `{d}`. Push and merge only with user approval.\n- Secret files (.env, keys) are never touched.\n', a=entry_name, d=branch))
    skeleton_record(home, entry_name, root)
    return entry_name


def main(argv: list[str]) -> int:
    round = None
    if '--round' in argv:
        position = argv.index('--round')
        if position + 1 >= len(argv) or argv[position + 1].startswith('--'):
            print(t('--round needs a project_ref type (python).'), file=sys.stderr)
            return 2
        round = argv[position + 1]
        argv = argv[:position] + argv[position + 2:]
    paths = [a for a in argv if not a.startswith('--')]
    try:
        path = paths[0] if paths else os.getcwd()
        try:
            registered = save(path, create='--create' in argv, round=round)
        except RecordPermission as requested:
            from permission import initialization_execute, obtain, read_action
            from run_store import RunStore
            store = RunStore(ui_messages.home_dir())
            action = read_action(store, 'system', requested.token)
            initialization_execute(store, obtain(store, 'system', action))
            registered = save(path)
        print(registered)
    except (RecordError, OSError, subprocess.SubprocessError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
