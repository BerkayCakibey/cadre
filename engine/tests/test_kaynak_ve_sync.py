"""C16/C26: the accepted source is pinned at acceptance; the updater refuses to race a running coordinator."""

from __future__ import annotations

import json
import hashlib
import importlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import shutil
import signal
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
from kosu import FileLock, RunStore  # noqa: E402
runtime_sync = importlib.import_module('runtime_sync')


def sync_betigi():
    """sync.sh is a REPO tool; it is not in the live install ($HOME/.local/share/cadre-engineering).
    17.09: when RC51 ran `unittest discover` from the live root, LIB.parents[1] fell to `$HOME/.local/share`
    and the tests stayed at 127. It searches upwards; if not found the test is SKIPPED (it does not pass silently)."""
    for kok in Path(__file__).resolve().parents:
        aday = kok / 'sync.sh'
        if aday.is_file() and (kok / 'engine').is_dir():
            return aday
    return None


def isolated_sync(base: Path) -> Path | None:
    """Source and runtime are sibling roots; a .tmp inside the repo does not nest the source and the target."""
    source = sync_betigi()
    if source is None:
        return None
    repo = base / 'repo'
    repo.mkdir()
    for name in ('engine', 'config'):
        shutil.copytree(source.parent / name, repo / name, ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(source, repo / 'sync.sh')
    return repo / 'sync.sh'


def git(repo: Path, *args: str) -> str:
    return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=f', '-c', 'user.email=f@x', *args],
                          check=True, capture_output=True, text=True, timeout=20).stdout.strip()


class PinnedSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-source-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = RunStore(self.home)
        self.repo = (self.home/'repo').resolve()
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '-b', 'main')
        (self.repo/'tracked.txt').write_text('v1\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'base')
        project = self.home/'projects/a'
        project.mkdir(parents=True)
        (project/'repo-path').write_text(str(self.repo))
        (project/'base-branch').write_text('main')
        hooks = self.home/'hooks'
        hooks.mkdir()
        for name in ('pre-push', 'pre-commit', 'commit-msg'):
            (hooks/name).write_text('#!/bin/sh\nexit 0\n')
            (hooks/name).chmod(0o700)

    def make_worktree(self, run):
        env = {**os.environ, **self.store.environment(run['run_id']), 'CADRE_ENGINE_ROOT': str(LIB.parent)}
        return subprocess.run(['bash', '-c', '. "$1"; qe_make_worktree a test', 'x', str(LIB/'common.sh')],
                              env=env, capture_output=True, text=True, timeout=30)

    def test_worktree_uses_the_accepted_snapshot_not_the_live_repo(self):
        (self.repo/'tracked.txt').write_text('v1 + accepted edit\n')          # uncommitted at acceptance
        (self.repo/'notes.md').write_text('accepted untracked\n')
        run = self.store.accept('a', 'work on it')
        accepted_sha = git(self.repo, 'rev-parse', 'HEAD')
        self.assertEqual(run['source_sha'], accepted_sha)
        self.assertTrue(run['source']['carried'])
        # The user keeps working after acceptance: new commit, further edits, new untracked file.
        (self.repo/'later.txt').write_text('later commit\n')
        git(self.repo, 'add', 'later.txt')
        git(self.repo, 'commit', '-qm', 'later')
        (self.repo/'tracked.txt').write_text('v1 + LATER edit\n')
        (self.repo/'notes.md').write_text('LATER untracked\n')
        (self.repo/'extra.md').write_text('never accepted\n')
        result = self.make_worktree(run)
        self.assertEqual(result.returncode, 0, result.stderr)
        wt = Path(result.stdout.strip())
        self.assertEqual(git(wt, 'rev-parse', 'HEAD'), accepted_sha)          # pinned, not the later commit
        self.assertEqual((wt/'tracked.txt').read_text(), 'v1 + accepted edit\n')
        self.assertEqual((wt/'notes.md').read_text(), 'accepted untracked\n')
        self.assertFalse((wt/'later.txt').exists())
        self.assertFalse((wt/'extra.md').exists())

    def test_unapplicable_snapshot_refuses_to_start_and_leaves_no_worktree(self):
        (self.repo/'tracked.txt').write_text('v1 + edit\n')
        run = self.store.accept('a', 'work')
        # A well-formed patch whose context no longer exists: git apply must fail (a garbage file would count as "empty").
        Path(run['source']['patch']).write_text('diff --git a/tracked.txt b/tracked.txt\n--- a/tracked.txt\n+++ b/tracked.txt\n'
                                                '@@ -1 +1 @@\n-line that never existed\n+replacement\n')
        result = self.make_worktree(run)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('snapshot', result.stderr)
        self.assertTrue((self.home/'worktrees/a'/run['run_id']).exists())
        self.assertIn('cadre/' + run['run_id'], git(self.repo, 'branch', '--list', 'cadre/*'))
        self.assertEqual((self.repo/'tracked.txt').read_text(), 'v1 + edit\n')
        self.assertEqual(self.store.read(run['run_id'])['worktree'], '')

    def test_detached_or_cadre_branch_pins_to_base_head_without_carrying(self):
        git(self.repo, 'checkout', '-q', '-b', 'cadre/old-work')
        (self.repo/'tracked.txt').write_text('dirty on cadre branch\n')
        run = self.store.accept('a', 'work')
        self.assertFalse(run['source']['carried'])
        self.assertEqual(run['source_sha'], git(self.repo, 'rev-parse', 'refs/heads/main'))
        result = self.make_worktree(run)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((Path(result.stdout.strip())/'tracked.txt').read_text(), 'v1\n')


    def test_project_child_pins_to_the_given_ref_without_carrying(self):
        """G-P3: a project-mode child run starts from the tip of the intermediate branch; half work on the user's active branch is not carried."""
        git(self.repo, 'checkout', '-q', '-b', 'cadre/proje-x')
        (self.repo/'tracked.txt').write_text('ozellik 1\n')
        git(self.repo, 'commit', '-qam', 'f1')
        git(self.repo, 'checkout', '-q', 'main')
        (self.repo/'tracked.txt').write_text('kullanicinin yarim isi\n')
        run = self.store.accept('a', 'ozellik 2', base_ref='cadre/proje-x')
        self.assertFalse(run['source']['carried'])
        self.assertEqual(run['source_sha'], git(self.repo, 'rev-parse', 'refs/heads/cadre/proje-x'))
        result = self.make_worktree(run)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((Path(result.stdout.strip())/'tracked.txt').read_text(), 'ozellik 1\n')

class SyncGuardTests(unittest.TestCase):
    def test_push_refuses_while_a_coordinator_owns_a_run_and_copies_nothing(self):
        temp = tempfile.TemporaryDirectory(prefix='qe-sync-')
        self.addCleanup(temp.cleanup)
        home = Path(temp.name).resolve()
        qe, cfg, bin_dir = home/'qe', home/'cfg', home/'bin'
        for d in (qe, cfg/'agents', cfg/'skills', cfg/'command'):
            d.mkdir(parents=True)
        (cfg/'opencode.jsonc').write_text('{}')
        store = RunStore(qe)
        run = store.accept('a', 'busy run')
        # The process-table guard is machine-wide (other suites may be running motors); the lock guard is what this test owns.
        env = {**os.environ, 'CADRE_HOME': str(qe), 'OPENCODE_CFG': str(cfg), 'CADRE_BIN_DIR': str(bin_dir), 'CADRE_SYNC_SKIP_PGREP': '1'}
        env.pop('CADRE_RUN_ID', None)
        sync = isolated_sync(home)
        if sync is None:
            self.skipTest('sync.sh yok (repo disindan kosuluyor) — repo kopyasinda kosar')
        with FileLock(store.path(run['run_id'])/'.owner.lock'):
            result = subprocess.run(['bash', str(sync), 'push'], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertIn(run['run_id'], result.stderr)
            self.assertFalse((qe/'lib').exists())
            self.assertFalse((bin_dir/'cadre').exists())
        result = subprocess.run(['bash', str(sync), 'push'], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((qe/'lib/qe_core.py').exists())
        self.assertIn(str(qe), (bin_dir/'cadre').read_text())
        self.assertFalse((cfg/'opencode.jsonc').read_text() != '{}')   # never pushed (key stays local)
        # B139: managed files removed from the repo cannot stay in the live install.
        removed = [qe/'lib/obsolete.py', qe/'bin/obsolete.sh', qe/'tests/obsolete.py',
                   qe/'hooks/obsolete', cfg/'agents/obsolete.md', cfg/'skills/obsolete.md', cfg/'command/obsolete.md']
        for path in removed:
            path.write_text('obsolete')
        manifest_path = qe/'zincir/runtime-manifest.json'
        manifest = json.loads(manifest_path.read_text())
        for path in removed:
            manifest['files'][str(path)] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                           'mode': path.stat().st_mode & 0o777}
        manifest_path.write_text(json.dumps(manifest))
        user_file = qe/'lib/user-extension.py'
        user_file.write_text('user-owned')
        result = subprocess.run(['bash', str(sync), 'push'], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(path.exists() for path in removed), [str(p) for p in removed if p.exists()])
        self.assertEqual(user_file.read_text(), 'user-owned')

    def test_fark_reports_missing_live_directories_and_equality_after_push(self):
        """B263 (29.09 external audit): with the live lib/hooks/tests/bin directories missing, the rsync error was swallowed and `fark`
        said 'no difference — repo == live' and returned 0; it would hide that the wrong version was installed."""
        temp = tempfile.TemporaryDirectory(prefix='qe-sync-')
        self.addCleanup(temp.cleanup)
        home = Path(temp.name).resolve()
        qe, cfg, bin_dir = home/'qe', home/'cfg', home/'bin'
        qe.mkdir(); cfg.mkdir()
        sync = isolated_sync(home)
        if sync is None:
            self.skipTest('sync.sh yok (repo disindan kosuluyor) — repo kopyasinda kosar')
        repo = sync.parent
        for kaynak, hedef in ((repo/'engine/USAGE.md', qe/'USAGE.md'), (repo/'config/AGENTS.md', cfg/'AGENTS.md'),
                              (repo/'config/opencode.jsonc', cfg/'opencode.jsonc')):
            hedef.write_bytes(kaynak.read_bytes())
        env = {**os.environ, 'CADRE_HOME': str(qe), 'OPENCODE_CFG': str(cfg), 'CADRE_BIN_DIR': str(bin_dir), 'CADRE_SYNC_SKIP_PGREP': '1'}
        env.pop('CADRE_RUN_ID', None)
        result = subprocess.run(['bash', str(sync), 'fark'], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn('fark yok', result.stdout)
        for eksik in ('engine/lib', 'engine/hooks', 'engine/tests', 'engine/bin', 'config/agents', 'config/skills'):
            self.assertIn(eksik, result.stdout, result.stdout)
        result = subprocess.run(['bash', str(sync), 'push'], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run(['bash', str(sync), 'fark'], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, 'fark yok — repo == canlı'), result.stdout + result.stderr)

    def test_process_guard_blocks_only_executors_running_from_the_live_root(self):
        """16.09: engines of isolated test/marathon copies do not read the live files; the old pgrep pattern counted them as 'live engine' too
        and blocked the push for hours. Only a process running from under $QE/lib|bin blocks."""
        temp = tempfile.TemporaryDirectory(prefix='qe-sync2-')
        self.addCleanup(temp.cleanup)
        home = Path(temp.name).resolve()
        qe, cfg, bin_dir, other = home/'qe', home/'cfg', home/'bin', home/'baska-kok'
        for d in (qe, cfg/'agents', cfg/'skills', cfg/'command', other/'lib'):
            d.mkdir(parents=True)
        (cfg/'opencode.jsonc').write_text('{}')
        env = {**os.environ, 'CADRE_HOME': str(qe), 'OPENCODE_CFG': str(cfg), 'CADRE_BIN_DIR': str(bin_dir)}
        env.pop('CADRE_SYNC_SKIP_PGREP', None)
        env.pop('CADRE_RUN_ID', None)
        sync = isolated_sync(home)
        if sync is None:
            self.skipTest('sync.sh yok (repo disindan kosuluyor) — repo kopyasinda kosar')
        (qe/'lib').mkdir()
        for root in (qe, other):
            (root/'lib/qe_core.py').write_text('import time\ntime.sleep(30)\n')
        # an "engine" running from another root (that root's path in argv): does not block
        foreign = subprocess.Popen([sys.executable, str(other/'lib/qe_core.py'), 'motor', 'p'])
        self.addCleanup(lambda: (foreign.kill(), foreign.wait(timeout=5)))
        # an engine running from the live root: blocks
        live = subprocess.Popen([sys.executable, str(qe/'lib/qe_core.py'), 'motor', 'p'])
        self.addCleanup(lambda: (live.kill(), live.wait(timeout=5)))
        time.sleep(.3)
        result = subprocess.run(['bash', str(sync), 'push'], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn('canlı kökten', result.stderr)
        self.assertFalse((qe/'lib/kosu.py').exists(), 'nothing copied while blocked')
        live.kill(); live.wait(timeout=5)
        time.sleep(.3)
        manifest = {'scope': [str(qe), str(cfg), str(bin_dir)], 'files': {
                    str(qe/'lib/qe_core.py'): {'sha256': hashlib.sha256((qe/'lib/qe_core.py').read_bytes()).hexdigest(),
                                              'mode': (qe/'lib/qe_core.py').stat().st_mode & 0o777}}}
        (qe/'zincir').mkdir(exist_ok=True)
        (qe/'zincir/runtime-manifest.json').write_text(json.dumps(manifest))
        result = subprocess.run(['bash', str(sync), 'push'], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((qe/'lib/kosu.py').exists())


class RuntimeSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cadre-sync-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.repo, self.home, self.config, self.binary = (self.base/name for name in ('repo', 'runtime', 'config', 'bin'))
        source = isolated_sync(self.base)
        if source is None:
            self.skipTest('sync.sh yok — repo kopyası gerekli')
        self.env = {**os.environ, 'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(self.home),
                    'OPENCODE_CFG': str(self.config), 'CADRE_BIN_DIR': str(self.binary),
                    'HOME': str(self.base/'user'), 'CADRE_AG_YOK': '1', 'CADRE_BEKCI_SAHIP_PID': str(os.getpid())}
        self.env.pop('CADRE_RUN_ID', None)
        self.env.pop('CADRE_SYNC_SKIP_PGREP', None)
        self.guards = set()
        self.addCleanup(self.cleanup_guards)

    def cleanup_guards(self):
        for name in runtime_sync.GUARDS:
            try:
                self.guards.add(int((self.home/'zincir'/f'{name}.pid').read_text()))
            except (OSError, ValueError):
                pass
        for pid in self.guards:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def sync(self, *args):
        return subprocess.run(['bash', str(self.repo/'sync.sh'), *(args or ('push',))],
                              env=self.env, capture_output=True, text=True, timeout=30)

    def install(self):
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def hashes(self):
        return {str(p.relative_to(self.base)): hashlib.sha256(p.read_bytes()).hexdigest()
                for root in (self.home, self.config, self.binary) if root.exists() for p in root.rglob('*')
                if p.is_file() and not any(part.startswith('.sync-stage-') for part in p.parts)
                and p.name not in ('.runtime.lock', '.sync-operation.lock') and '__pycache__' not in p.parts}

    def wait_guard(self, name, old=None):
        deadline = time.monotonic()+10
        while time.monotonic() < deadline:
            try:
                receipt = json.loads((self.home/'zincir'/f'{name}.runtime.json').read_text())
                pid = receipt['identity']['pid']
                if pid != old and int((self.home/'zincir'/f'{name}.pid').read_text()) == pid:
                    idle = subprocess.run(['pgrep', '-P', str(pid), '-x', 'sleep'], capture_output=True, timeout=2)
                    if idle.returncode:
                        time.sleep(.025)
                        continue
                    gate = runtime_sync.lock_gate(self.home, directory=True)
                    if gate is None:
                        time.sleep(.025)
                        continue
                    gate.close()
                    self.guards.add(pid)
                    return receipt
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(.025)
        self.fail('bekçi kaydı oluşmadı: '+name)

    def start_guard(self, name):
        process = subprocess.Popen(['bash', str(self.home/'bin'/f'{name}.sh')], env={**self.env,
                    'CADRE_NOBET_ARALIK': '60', 'CADRE_KOTA_ARALIK': '60'},
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.guards.add(process.pid)
        self.addCleanup(lambda: (process.kill(), process.wait(timeout=5)))
        return self.wait_guard(name)

    def test_force_cannot_bypass_owner_or_start_gate_and_no_files_are_copied(self):
        store = RunStore(self.home)
        run = store.accept('a', 'pending')
        with FileLock(store.path(run['run_id'])/'.owner.lock'):
            before = self.hashes()
            result = self.sync('push', '--zorla')
            self.assertEqual(result.returncode, 2, 'FIX-07: push icin zorlama bayragi yok')
            self.assertIn('zorla', result.stderr)
            self.assertEqual(self.hashes(), before)
            result = self.sync('push')
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn(run['run_id'], result.stderr)
        self.assertEqual(self.hashes(), before)
        with runtime_sync.lock_gate(self.home, directory=True):
            with self.assertRaisesRegex(ValueError, 'runtime-sync-active'):
                store.accept('b', 'must not create work')
            result = self.sync('push')
            self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(len(store.list()), 1)
        self.assertEqual(self.hashes(), before)

    def test_adopt_two_step_takes_ownership_of_repo_managed_live_files_only(self):
        (self.home/'lib').mkdir(parents=True)
        collision = self.home/'lib/kosu.py'
        collision.write_text('user file\n')
        foreign = self.home/'lib/user.py'
        foreign.write_text('user data\n')
        self.assertEqual(self.sync('push').returncode, 3)
        first = self.sync('adopt')
        self.assertEqual(first.returncode, 0, first.stderr)
        offer = json.loads(first.stdout)
        self.assertEqual((offer['status'], offer['paths']), ('PENDING', [str(collision)]))
        wrong = self.sync('adopt', 'f' * 64)
        self.assertEqual(wrong.returncode, 3)
        self.assertIn('sync-adopt-token-mismatch', wrong.stderr)
        self.assertFalse((self.home/'zincir/runtime-manifest.json').exists())
        store = RunStore(self.home)
        run = store.accept('a', 'pending')
        with FileLock(store.path(run['run_id'])/'.owner.lock'):
            blocked = self.sync('adopt', offer['token'])
        self.assertEqual(blocked.returncode, 3)
        self.assertIn(run['run_id'], blocked.stderr)
        self.assertFalse((self.home/'zincir/runtime-manifest.json').exists())
        store.update(run['run_id'], state='cancelled', exit_code=130, owner_pid=None)
        before = self.hashes()
        done = self.sync('adopt', offer['token'])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {'status': 'PASS', 'adopted': [str(collision)]})
        self.assertEqual(collision.read_text(), 'user file\n')
        manifest = json.loads((self.home/'zincir/runtime-manifest.json').read_text())
        self.assertEqual(set(manifest['files']), {str(collision)})
        self.assertEqual(self.hashes().keys() - before.keys(), {str((self.home/'zincir/runtime-manifest.json').relative_to(self.base))})
        self.install()
        self.assertEqual(collision.read_bytes(), (self.repo/'engine/lib/kosu.py').read_bytes())
        self.assertEqual(foreign.read_text(), 'user data\n')

    def test_launcher_keeps_the_installed_home_and_config_without_environment(self):
        self.install()
        launcher = (self.binary / 'cadre').read_text().splitlines()
        self.assertTrue(launcher[-1].startswith('exec bash '))
        probe = '\n'.join(launcher[:-1]) + '\nprintf "%s\\n%s\\n" "$CADRE_HOME" "$OPENCODE_CFG"'
        env = {k: v for k, v in self.env.items() if k not in ('CADRE_HOME', 'OPENCODE_CFG')}
        result = subprocess.run(['bash', '-c', probe], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.stdout.splitlines(), [str(self.home), str(self.config)])
        env['CADRE_HOME'] = str(self.base / 'other')
        result = subprocess.run(['bash', '-c', probe], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.stdout.splitlines(), [str(self.base / 'other'), str(self.config)])

    def test_link_in_live_root_is_a_listed_conflict_and_blocks_push_without_crashing(self):
        self.install()
        target = self.home/'lib/kosu.py'
        target.unlink()
        target.symlink_to(self.repo/'engine/lib/kosu.py')
        before = self.hashes()
        result = self.sync('plan')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn({'path': str(target), 'reason': 'unreadable-or-link'}, json.loads(result.stdout)['conflicts'])
        result = self.sync('push')
        self.assertEqual(result.returncode, 3)
        self.assertIn('sync-ownership-conflict', result.stderr)
        self.assertTrue(target.is_symlink())
        self.assertEqual(self.hashes(), before)

    def test_incomplete_sync_message_names_the_pending_file_and_completion_command(self):
        self.install()
        marker = self.home/'zincir/runtime-restart.json'
        marker.write_text('{}')
        result = self.sync('fark')
        self.assertIn(str(marker), result.stderr)
        self.assertIn('./sync.sh push', result.stderr)
        gated = subprocess.run([sys.executable, str(self.home/'lib/kosu.py'), 'accept', 'a', 'must not start'],
                               env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(gated.returncode, 75, gated.stderr)
        self.assertIn('runtime-sync-incomplete', gated.stderr)
        self.assertIn(str(marker), gated.stderr)
        self.assertIn('./sync.sh push', gated.stderr)
        self.assertFalse((self.home/'runs').exists())

    def test_managed_removal_preserves_unknown_and_user_modified_files(self):
        self.install()
        removed = self.repo/'engine/lib/obsolete.py'
        removed.write_text('old managed\n')
        self.install()
        user = self.home/'lib/user.py'
        user.write_text('user data\n')
        removed.unlink()
        self.install()
        self.assertFalse((self.home/'lib/obsolete.py').exists())
        self.assertEqual(user.read_text(), 'user data\n')
        removed.write_text('managed again\n')
        self.install()
        (self.home/'lib/obsolete.py').write_text('user changed managed\n')
        removed.unlink()
        result = self.install()
        self.assertIn(str(self.home/'lib/obsolete.py'), json.loads(result.stdout)['preserved'])
        self.assertEqual((self.home/'lib/obsolete.py').read_text(), 'user changed managed\n')

    def test_unknown_collision_and_modified_managed_file_block_without_overwrite(self):
        (self.home/'lib').mkdir(parents=True)
        collision = self.home/'lib/kosu.py'
        collision.write_text('user file\n')
        before = self.hashes()
        result = self.sync()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(self.hashes(), before)
        collision.unlink()
        self.install()
        collision.write_text('user edit\n')
        before = self.hashes()
        result = self.sync()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(self.hashes(), before)

    def test_changed_guards_restart_with_new_identity_unchanged_guards_keep_pid(self):
        self.install()
        receipts = {name: self.start_guard(name) for name in runtime_sync.GUARDS}
        result = self.install()
        self.assertEqual(json.loads(result.stdout)['restarted'], [])
        for name, receipt in receipts.items():
            self.assertEqual(self.wait_guard(name)['identity'], receipt['identity'])
        (self.repo/'engine/lib/sync-probe.txt').write_text('next version\n')
        result = self.install()
        self.assertEqual(json.loads(result.stdout)['restarted'], sorted(runtime_sync.GUARDS))
        import kosu
        for name, receipt in receipts.items():
            renewed = self.wait_guard(name, receipt['identity']['pid'])
            self.assertIsNone(kosu.process_identity(receipt['identity']['pid']))
            self.assertEqual(renewed['root'], str(self.home))
            self.assertEqual(renewed['version'], runtime_sync.guard_version(self.home))
            self.assertEqual(renewed['settings']['CADRE_BEKCI_SAHIP_PID'], str(os.getpid()))

    def test_unknown_guard_owner_is_never_killed_or_updated(self):
        self.install()
        process = subprocess.Popen(['sleep', '30'], start_new_session=True)
        self.addCleanup(lambda: (process.kill(), process.wait(timeout=5)))
        (self.home/'zincir/saglik-nobet.pid').write_text(str(process.pid))
        (self.repo/'engine/lib/probe.txt').write_text('changed')
        before = self.hashes()
        result = self.sync()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn('sync-guard-owner-unknown', result.stderr)
        self.assertIsNone(process.poll())
        self.assertEqual(self.hashes(), before)

    def test_copy_error_rolls_back_and_retry_is_idempotent(self):
        self.install()
        before = self.hashes()
        (self.repo/'engine/lib/probe.txt').write_text('changed')
        original = runtime_sync.write_target
        calls = []
        def broken(path, data, mode=0o600):
            if path.is_relative_to(self.home/'lib'):
                calls.append(path)
                if len(calls) == 1:
                    original(path, data, mode)
                    raise OSError('disk-write-failed')
            return original(path, data, mode)
        with patch.object(runtime_sync, 'write_target', side_effect=broken):
            with self.assertRaisesRegex(OSError, 'disk-write-failed'):
                runtime_sync.push(self.repo, self.home, self.config, self.binary)
        current = self.hashes()
        current.pop('runtime/zincir/runtime-recovery.json', None)
        self.assertEqual(current, before)
        self.install()
        after = self.hashes()
        self.install()
        self.assertEqual(self.hashes(), after)

    def test_sigkill_leaves_execution_closed_until_recovery_and_one_retry(self):
        self.install()
        (self.repo/'engine/lib/probe.txt').write_text('changed')
        script = self.base/'kill-sync.py'
        script.write_text('import os,sys,signal\nfrom pathlib import Path\n'
            f'sys.path.insert(0,{str(LIB)!r})\nimport runtime_sync as s\noriginal=s.write_target\n'
            'def broken(path,data,mode=0o600):\n'
            ' original(path,data,mode)\n'
            f' if path==Path({str(self.home/"lib/probe.txt")!r}): os.kill(os.getpid(),signal.SIGKILL)\n'
            's.write_target=broken\n'
            f's.push(Path({str(self.repo)!r}),Path({str(self.home)!r}),Path({str(self.config)!r}),Path({str(self.binary)!r}))\n')
        result = subprocess.run([sys.executable, str(script)], env=self.env, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, -signal.SIGKILL)
        self.assertTrue((self.home/'zincir/.sync-pending.json').is_file())
        result = subprocess.run([sys.executable, str(self.home/'lib/kosu.py'), 'accept', 'a', '-'],
                    input='no work', env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertIn('runtime-sync-incomplete', result.stderr)
        self.install()
        self.assertFalse((self.home/'zincir/.sync-pending.json').exists())
        self.assertEqual((self.home/'lib/probe.txt').read_text(), 'changed')
        after = self.hashes()
        self.install()
        self.assertEqual(self.hashes(), after)

    def test_links_fifo_and_manifest_escape_fail_closed(self):
        self.install()
        outside = self.base/'outside'
        outside.write_text('protected')
        target = self.home/'lib/kosu.py'
        for kind in ('symlink', 'hardlink', 'fifo'):
            original = target.read_bytes()
            mode = target.stat().st_mode & 0o777
            target.unlink()
            if kind == 'symlink':
                target.symlink_to(outside)
            elif kind == 'hardlink':
                os.link(outside, target)
            else:
                os.mkfifo(target)
            result = self.sync()
            self.assertEqual(result.returncode, 3, (kind, result.stderr))
            self.assertEqual(outside.read_text(), 'protected')
            target.unlink()
            target.write_bytes(original)
            target.chmod(mode)
        manifest_path = self.home/'zincir/runtime-manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['files'][str(outside)] = {'sha256': hashlib.sha256(outside.read_bytes()).hexdigest(), 'mode': 0o600}
        manifest_path.write_text(json.dumps(manifest))
        result = self.sync()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(outside.read_text(), 'protected')

    def test_plan_is_read_only_and_never_reads_or_overwrites_private_config(self):
        self.install()
        (self.home/'zincir/.runtime.lock').unlink(missing_ok=True)
        (self.config/'opencode.jsonc').mkdir()
        private = self.home/'lib/.env'
        os.mkfifo(private)
        before = self.hashes()
        result = self.sync('plan')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['conflicts'], [])
        health = subprocess.run([sys.executable, str(self.home/'lib/kosu.py'), 'health-logs'],
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(health.returncode, 0, health.stderr)
        self.assertIn('log:AKTIF_YOK', health.stdout)
        self.assertFalse((self.home/'zincir/.runtime.lock').exists())
        self.assertEqual(self.hashes(), before)
        self.install()
        self.assertTrue((self.config/'opencode.jsonc').is_dir())
        self.assertTrue(private.is_fifo())
        private.unlink()
        private_source = self.repo/'engine/lib/.env'
        os.mkfifo(private_source)
        result = self.sync()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn('sync-sensitive-source', result.stderr)

    def test_loaded_old_generation_cannot_start_after_sync(self):
        self.install()
        ready, proceed, outcome = (self.base/name for name in ('ready', 'proceed', 'outcome'))
        script = self.base/'old-process.py'
        script.write_text('import sys,time\nfrom pathlib import Path\n'
            f'sys.path.insert(0,{str(self.home/"lib")!r})\nfrom kosu import RunStore\n'
            f'Path({str(ready)!r}).write_text("ready")\n'
            f'while not Path({str(proceed)!r}).exists(): time.sleep(.01)\n'
            'try:\n'
            f' RunStore(Path({str(self.home)!r})).accept("a","must not start")\n'
            'except ValueError as error:\n'
            f' Path({str(outcome)!r}).write_text(str(error))\n')
        process = subprocess.Popen([sys.executable, str(script)], env=self.env)
        self.addCleanup(lambda: (process.kill(), process.wait(timeout=5)))
        deadline = time.monotonic()+10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(ready.exists())
        (self.repo/'engine/lib/probe.txt').write_text('changed')
        self.install()
        proceed.write_text('go')
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertEqual(outcome.read_text(), 'runtime-generation-changed')
        self.assertEqual(RunStore(self.home).list(), [])

    def test_two_syncs_and_start_attempt_during_copy_never_mix_versions(self):
        self.install()
        (self.repo/'engine/lib/probe.txt').write_text('changed')
        ready, proceed = self.base/'copy-ready', self.base/'copy-proceed'
        script = self.base/'slow-sync.py'
        script.write_text('import sys,time\nfrom pathlib import Path\n'
            f'sys.path.insert(0,{str(LIB)!r})\nimport runtime_sync as s\noriginal=s.write_target\n'
            'def slow(path,data,mode=0o600):\n'
            ' original(path,data,mode)\n'
            f' if path==Path({str(self.home/"lib/probe.txt")!r}):\n'
            f'  Path({str(ready)!r}).write_text("ready")\n'
            f'  while not Path({str(proceed)!r}).exists(): time.sleep(.01)\n'
            's.write_target=slow\n'
            f's.push(Path({str(self.repo)!r}),Path({str(self.home)!r}),Path({str(self.config)!r}),Path({str(self.binary)!r}))\n')
        process = subprocess.Popen([sys.executable, str(script)], env=self.env)
        self.addCleanup(lambda: (process.kill(), process.wait(timeout=5)))
        deadline = time.monotonic()+10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(ready.exists())
        result = self.sync('push')
        self.assertEqual(result.returncode, 3, result.stderr)
        result = subprocess.run([sys.executable, str(self.home/'lib/kosu.py'), 'accept', 'a', '-'],
                    input='must not start', env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 75, result.stderr)
        proceed.write_text('go')
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertEqual(RunStore(self.home).list(), [])
        self.assertEqual(self.sync('fark').returncode, 0)

    def test_corrupt_recovery_cannot_write_outside_managed_roots(self):
        self.install()
        outside = self.base/'outside'
        outside.write_text('protected')
        transaction = {'scope': [str(self.home), str(self.config), str(self.binary)], 'guards': {},
                       'entries': [{'path': str(outside), 'backup': None, 'before_hash': None,
                                    'after_hash': hashlib.sha256(b'protected').hexdigest(), 'mode': 0o600}]}
        (self.home/'zincir/.sync-pending.json').write_text(json.dumps(transaction))
        result = self.sync()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(outside.read_text(), 'protected')
        self.assertTrue((self.home/'zincir/.sync-pending.json').exists())
        (self.home/'zincir/.sync-pending.json').unlink()
        (self.home/'zincir/.sync-pending.json').symlink_to(outside)
        blocked = subprocess.run([sys.executable, str(self.home/'lib/kosu.py'), 'accept', 'a', '-'],
                    input='must not start', env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(blocked.returncode, 75, blocked.stderr)
        self.assertIn('runtime-sync-incomplete', blocked.stderr)
        self.assertEqual(outside.read_text(), 'protected')

    def test_source_and_target_changes_during_staging_are_not_overwritten(self):
        self.install()
        source = self.repo/'engine/lib/probe.txt'
        source.write_text('v2')
        original = runtime_sync.write_target
        changed = []
        def race(path, data, mode=0o600):
            original(path, data, mode)
            if '.after' in path.name and not changed:
                changed.append(True)
                source.write_text('v3')
        with patch.object(runtime_sync, 'write_target', side_effect=race):
            with self.assertRaisesRegex(ValueError, 'sync-source-changed'):
                runtime_sync.push(self.repo, self.home, self.config, self.binary)
        self.assertFalse((self.home/'lib/probe.txt').exists())
        self.install()
        source.write_text('v4')
        changed.clear()
        def target_race(path, data, mode=0o600):
            original(path, data, mode)
            if '.after' in path.name and not changed:
                changed.append(True)
                (self.home/'lib/probe.txt').write_text('user changed')
        with patch.object(runtime_sync, 'write_target', side_effect=target_race):
            with self.assertRaisesRegex(ValueError, 'sync-recovery-target-modified'):
                runtime_sync.push(self.repo, self.home, self.config, self.binary)
        self.assertEqual((self.home/'lib/probe.txt').read_text(), 'user changed')
        self.assertTrue((self.home/'zincir/.sync-pending.json').exists())

    def test_killed_sync_restarts_recorded_guard_once_after_recovery(self):
        self.install()
        old = self.start_guard('kota-izle')
        (self.repo/'engine/lib/probe.txt').write_text('v2')
        script = self.base/'kill-sync.py'
        script.write_text('import os,sys,signal\nfrom pathlib import Path\n'
            f'sys.path.insert(0,{str(LIB)!r})\nimport runtime_sync as s\noriginal=s.write_target\n'
            'def broken(path,data,mode=0o600):\n'
            ' original(path,data,mode)\n'
            f' if path==Path({str(self.home/"lib/probe.txt")!r}): os.kill(os.getpid(),signal.SIGKILL)\n'
            's.write_target=broken\n'
            f's.push(Path({str(self.repo)!r}),Path({str(self.home)!r}),Path({str(self.config)!r}),Path({str(self.binary)!r}))\n')
        result = subprocess.run([sys.executable, str(script)], env=self.env, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, -signal.SIGKILL)
        self.install()
        renewed = self.wait_guard('kota-izle', old['identity']['pid'])
        self.assertEqual(renewed['version'], runtime_sync.guard_version(self.home))
        self.assertFalse((self.home/'zincir/runtime-restart.json').exists())
        self.install()
        self.assertEqual(self.wait_guard('kota-izle')['identity'], renewed['identity'])


if __name__ == '__main__':
    unittest.main()

class ProjeTespitTests(unittest.TestCase):
    def test_alt_proje_klasorleri_asil_projeyi_golgelemez(self):
        """B270 (30.09 rehearsal): project-mode sub-projects (`ad--p<id>`) carry the same repo path; since they come first in glob order
        `cadre gorev` bound to a stale sub-project instead of the real project (the rehearsal became a nested sub-project)."""
        temp = tempfile.TemporaryDirectory(prefix='qe-proje-')
        self.addCleanup(temp.cleanup)
        home = Path(temp.name)
        repo = home / 'repo'
        repo.mkdir()
        for ad in ('butcem--p6de82766--p0936', 'butcem--p6de82766', 'butcem', 'butcem--pf5f7b409'):
            (home / 'projects' / ad).mkdir(parents=True)
            (home / 'projects' / ad / 'repo-path').write_text(str(repo) + '\n')
        env = {**os.environ, 'CADRE_HOME': str(home), 'CADRE_ENGINE_ROOT': str(LIB.parent)}
        r = subprocess.run(['bash', '-c', '. "$1"; qe_project_for_dir "$2"', 'x', str(LIB / 'common.sh'), str(repo)],
                           env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, 'butcem'), r.stderr)
