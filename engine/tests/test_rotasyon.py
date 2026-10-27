"""C4/K5: deletion requires durable, verified output and an inactive owner."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import test_kosu_sessions as fixtures
from kosu import FileLock, RunStore
import kosu_defteri as ledger


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-rotation-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = RunStore(self.home)
        self.run = self.store.accept('a', 'produce output')
        self.rid = self.run['run_id']
        self.wt = self.home/'worktrees/a/old'
        self.wt.mkdir(parents=True)
        (self.wt/'untracked.bin').write_bytes(b'\x00product\xff')
        (self.wt/'STATE.md').write_text('remaining step')
        self.store.update(self.rid, worktree=str(self.wt), state='running')
        os.utime(self.wt, (time.time()-3*86400,)*2)

    def module(self):
        # Old rotasyon.py performs live actions on import. Never import that
        # version: absence of the new API is itself the red behavioral contract.
        import ast
        source = (fixtures.LIB/'rotasyon.py').read_text()
        if not any(isinstance(n, ast.FunctionDef) and n.name=='rotate' for n in ast.parse(source).body):
            self.fail('rotation must expose a safe, explicit entry point before it can be tested')
        import rotasyon
        return rotasyon

    def approve_cleanup(self):
        import izin
        action = izin.cleanup_proposal(self.store, self.rid, Path(self.store.read(self.rid)['worktree']))
        return izin.approve(self.store, self.rid, action['token'])

    def test_old_active_worktree_and_untracked_memory_survive_rotation(self):
        module = self.module()
        with FileLock(self.store.path(self.rid)/'.owner.lock'):
            module.rotate(self.home, keep=0)
        self.assertEqual((self.wt/'untracked.bin').read_bytes(), b'\x00product\xff')
        self.assertEqual((self.wt/'STATE.md').read_text(), 'remaining step')

    def test_verified_archive_restores_untracked_files_and_memory(self):
        import arsiv
        manifest = arsiv.capture(self.wt, self.home/'archive')
        restored = self.home/'restored'
        arsiv.restore(manifest, restored)
        self.assertEqual((restored/'untracked.bin').read_bytes(), b'\x00product\xff')
        self.assertEqual((restored/'STATE.md').read_text(), 'remaining step')

    def test_terminal_run_with_live_child_lock_is_never_removed(self):
        module = self.module()
        self.store.update(self.rid, state='completed')
        lock = self.store.task_dir(self.rid, 'g1')/'.owner.lock'
        with FileLock(lock):
            module.rotate(self.home, keep=0)
        self.assertTrue(self.wt.exists())

    def test_live_executor_lease_alone_prevents_cleanup(self):
        module = self.module()
        self.store.update(self.rid, state='completed')
        with FileLock(self.store.task_dir(self.rid, 'g1')/'.opencode-impl.lock'):
            module.rotate(self.home, keep=0)
        self.assertTrue(self.wt.exists())

    def test_archive_failure_or_corruption_never_removes_source(self):
        module = self.module()
        self.store.update(self.rid, state='completed')
        self.approve_cleanup()
        with patch.object(module, 'capture', side_effect=OSError('disk full')):
            module.rotate(self.home, keep=0)
        self.assertTrue((self.wt/'untracked.bin').is_file())

    def test_terminal_cleanup_can_be_restored_after_restart(self):
        module = self.module()
        self.store.update(self.rid, state='completed')
        self.approve_cleanup()
        module.rotate(self.home, keep=0)
        self.assertFalse(self.wt.exists())
        import arsiv
        arsiv.restore_run_worktree(self.store, self.rid)
        self.assertEqual((self.wt/'untracked.bin').read_bytes(), b'\x00product\xff')
        self.assertEqual((self.wt/'STATE.md').read_text(), 'remaining step')

    def test_interrupted_deletion_restores_original_tree_before_reexecution(self):
        module = self.module()
        self.store.update(self.rid, state='completed')
        self.approve_cleanup()
        with patch.object(module.shutil, 'rmtree', side_effect=OSError('interrupted deletion')):
            module.rotate(self.home, keep=0)
        self.assertFalse(self.wt.exists())
        cleanup = self.store.read(self.rid)['cleanup']
        self.assertTrue(Path(cleanup['staging']).exists())
        from kosu import execute_run
        with patch('sonuc.approved', return_value=True), patch('subprocess.Popen') as child:
            self.assertEqual(execute_run(self.store, self.rid), 0)
        child.assert_not_called()
        self.assertEqual((self.wt/'untracked.bin').read_bytes(), b'\x00product\xff')

    def test_corrupted_blob_is_rejected_without_partial_restore(self):
        import arsiv
        manifest = arsiv.capture(self.wt, self.home/'archive')
        entry = json.loads(manifest.read_text())['entries']['untracked.bin']
        (manifest.parent/'blobs'/entry['sha256']).write_bytes(b'corrupted')
        with self.assertRaises(OSError):
            arsiv.restore(manifest, self.home/'restore')
        self.assertFalse((self.home/'restore').exists())

    def test_archived_git_worktree_survives_prune_and_reopens_same_revision(self):
        module = self.module()
        repo = self.home/'repo'
        def git(*args):
            return subprocess.run(['git', *args], capture_output=True, text=True, check=True, timeout=10).stdout.strip()
        git('init', '-q', str(repo))
        (repo/'tracked.txt').write_text('base')
        git('-C', str(repo), 'add', '.')
        git('-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'base')
        tree = self.home/'worktrees/a/linked'
        git('-C', str(repo), 'worktree', 'add', '-b', 'cadre/fixture', str(tree))
        (tree/'untracked.txt').write_text('product')
        head = git('-C', str(tree), 'rev-parse', 'HEAD')
        self.store.update(self.rid, worktree=str(tree), state='completed')
        os.utime(tree, (time.time()-3*86400,)*2)
        self.approve_cleanup()
        module.rotate(self.home, keep=0)
        self.assertFalse(tree.exists())
        git('-C', str(repo), 'worktree', 'prune', '--expire', 'now')
        from arsiv import restore_run_worktree
        restore_run_worktree(self.store, self.rid)
        self.assertEqual(git('-C', str(tree), 'rev-parse', 'HEAD'), head)
        self.assertEqual((tree/'untracked.txt').read_text(), 'product')
        self.assertEqual(git('-C', str(repo), 'status', '--porcelain'), '')

    def test_ledger_captures_untracked_bytes_even_without_git(self):
        output = self.home/'report'
        output.mkdir()
        (self.wt/'__pycache__').mkdir()
        (self.wt/'__pycache__/x.pyc').write_bytes(b'\x00cache')
        (self.wt/'var/uv-cache/wheels').mkdir(parents=True)
        (self.wt/'var/uv-cache/wheels/pkg.whl').write_bytes(b'\x00wheel')
        (self.wt/'var/data.txt').write_text('kept')
        ledger.git_kaydi(output, str(self.wt), None)
        import arsiv
        manifest = Path(json.loads((output/'worktree-archive.json').read_text())['manifest'])
        arsiv.restore(manifest, self.home/'ledger-restored')
        self.assertEqual((self.home/'ledger-restored/STATE.md').read_text(), 'remaining step')
        self.assertEqual((self.home/'ledger-restored/untracked.bin').read_bytes(), b'\x00product\xff')
        self.assertEqual((self.home/'ledger-restored/var/data.txt').read_text(), 'kept')
        self.assertFalse((self.home/'ledger-restored/__pycache__').exists())          # B18: caches are not product
        self.assertFalse((self.home/'ledger-restored/var/uv-cache').exists())
        # Rotation's own archive stays byte-exact (no skip).
        full = arsiv.capture(self.wt, self.home/'full-archive')
        self.assertIn('__pycache__/x.pyc', json.loads(full.read_text())['entries'])

    def test_failed_ledger_does_not_advance_watermark(self):
        db = self.home/'db.sqlite'
        con = sqlite3.connect(db)
        con.execute('create table session(id text,parent_id text,directory text,time_created integer,time_updated integer)')
        con.execute('insert into session values(?,?,?,?,?)', ('s1',None,str(self.wt),100,200))
        con.commit(); con.close()
        watermark = self.home/'reports/watermark'
        watermark.parent.mkdir()
        watermark.write_text('99')
        with patch.object(ledger, 'QE', self.home), patch.object(ledger, 'DB', db), patch.object(ledger, 'WATERMARK', watermark), \
             patch.object(ledger, 'dump', side_effect=OSError('archive full')):
            with self.assertRaises(OSError):
                ledger.tara()
        self.assertEqual(watermark.read_text(), '99')

    def test_weekly_database_backup_contains_committed_receipt(self):
        import gzip
        from datetime import datetime
        module = self.module()
        db = self.home/'source.db'
        con = sqlite3.connect(db)
        con.execute('create table receipt(value text)')
        con.execute("insert into receipt values('owned-response')")
        con.commit(); con.close()
        archive = module.backup_database(self.home, db, now=datetime(2026, 9, 13, 12).timestamp())
        recovered = self.home/'recovered.db'
        with gzip.open(archive, 'rb') as source:
            recovered.write_bytes(source.read())
        con = sqlite3.connect(recovered)
        self.assertEqual(con.execute('select value from receipt').fetchall(), [('owned-response',)])
        con.close()


if __name__ == '__main__':
    unittest.main()
