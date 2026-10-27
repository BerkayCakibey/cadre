"""C23/C24/C25/C27/C29: critical-file guard, review scope, monitor completion semantics, ledger redaction, one cost base."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import test_run_sessions as fixtures
import test_outcome as completion
import qe_core as core


def setUpModule():
    environment = patch.dict(os.environ, {'CADRE_CHAIN_QUALITY': 'opencode-go/qwen3.8-max'})
    environment.start()
    unittest.addModuleCleanup(environment.stop)


LIB = fixtures.LIB
SECRET = 'sk-' + 'Q' * 40


class CriticalFileGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-guard-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home/'chain').mkdir()
        self.a = self.home/'chain/expense-coefficient.txt'
        self.b = self.home/'chain/go-daily-cap.txt'
        self.a.write_text('4.3\n')
        self.b.write_text('2.0\n')
        self.backup_root = Path(tempfile.mkdtemp(prefix='signature-reserve-'))
        self.addCleanup(shutil.rmtree, self.backup_root, True)
        self.env = {**os.environ, 'CADRE_HOME': str(self.home), 'CADRE_CRITICAL_FILES': f'{self.a}:{self.b}',
                    'CADRE_SIGNATURE_BACKUP_ROOT': str(self.backup_root)}

    def guard(self, command, manifest=None):
        """sign: keeps the output (the engine's memory); verify: per B288 the manifest comes only from stdin."""
        extra = ['-'] if command == 'verify' else []
        input = (self.manifest if manifest is None else manifest) if command == 'verify' else None
        outcome = subprocess.run(['bash', str(LIB.parent/'bin/restricted-shell.sh'), command, 'fixture', *extra], env=self.env,
                               input=input, capture_output=True, text=True, timeout=20)
        if command == 'signature':
            self.manifest = outcome.stdout
        elif manifest is None:
            self.manifest = ''   # verification consumes the signature; a second verification without a signature sees an empty manifest
        return outcome

    def test_deleted_and_modified_files_are_violations_and_restored_individually(self):
        self.assertEqual(self.guard('signature').returncode, 0)
        self.a.unlink()
        self.b.write_text('99.0\n')
        result = self.guard('verify')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('DELETED', result.stdout)
        self.assertIn('changed', result.stdout)
        self.assertEqual(self.a.read_text(), '4.3\n')
        self.assertEqual(self.b.read_text(), '2.0\n')

    def test_a_deleted_or_emptied_manifest_is_itself_a_violation(self):
        """B110 (18.09): an empty/deleted manifest was the easy way to remove the protection. B288: the manifest is in the engine's memory;
        an empty manifest, one with a tampered line count or none at all (without stdin) is a VIOLATION."""
        self.assertEqual(self.guard('signature').returncode, 0)
        outcome = self.guard('verify', manifest='')
        self.assertEqual(outcome.returncode, 1, outcome.stdout)
        self.assertIn('VIOLATION', outcome.stdout)
        self.guard('signature')
        outcome = subprocess.run(['bash', str(LIB.parent/'bin/restricted-shell.sh'), 'verify', 'fixture'], env=self.env,
                               capture_output=True, text=True, timeout=20)
        self.assertEqual(outcome.returncode, 1, outcome.stdout)
        self.assertIn('stdin', outcome.stdout)
        self.guard('signature')
        self.assertEqual(self.guard('verify', manifest=self.manifest.replace('#LINE 2', '#LINE 3')).returncode, 1)

    def test_forged_manifest_cannot_hide_a_change_when_the_runner_holds_the_manifest(self):
        """D07: the manifest is where the agent can write; writing `#LINE 0` let a changed file pass with rc=0.
        The root of trust is the signing output in the engine's memory (stdin); the writable file and the backup are checked against it."""
        self.assertEqual(self.guard('signature').returncode, 0)
        self.b.write_text('99.0\n')
        (self.home/'chain/.signature-fixture.txt').write_text('#LINE 0\n')
        outcome = self.guard('verify')
        self.assertEqual(outcome.returncode, 1, outcome.stdout)
        self.assertEqual(self.b.read_text(), '2.0\n')
        self.guard('signature')
        self.a.write_text('changed\n')
        h = self.manifest.split()[0]
        (self.backup_root/f'fixture.reserve/{h}').write_text('agent version\n')   # if the backup is corrupted it is not restored
        outcome = self.guard('verify')
        self.assertEqual(outcome.returncode, 1, outcome.stdout)
        self.assertNotEqual(self.a.read_text(), 'agent version\n')
        self.assertIn('backup corrupted', outcome.stdout)

    def test_backups_live_outside_the_data_root_and_stale_orphans_are_removed(self):
        """Audit C-15/R-23: an auth.json copy is not written into CADRE_HOME (the area the sandbox can read); an old backup left by an
        interrupted run is cleaned at the next signing."""
        orphan = self.backup_root / 'old.reserve'
        orphan.mkdir()
        (orphan / 'secret').write_text('old copy\n')
        old = time.time() - 2 * 86400
        os.utime(orphan, (old, old))
        self.assertEqual(self.guard('signature').returncode, 0)
        self.assertFalse(orphan.exists())
        self.assertFalse((self.home / 'chain/.signature-fixture.reserve').exists())
        reserve = self.backup_root / 'fixture.reserve'
        self.assertTrue(reserve.is_dir())
        self.assertEqual(oct(reserve.stat().st_mode & 0o777), '0o700')
        self.assertEqual(sorted(p.name for p in reserve.iterdir()), sorted(self.manifest.split()[::2][:2]))
        self.assertEqual(self.guard('verify').returncode, 0)
        self.assertFalse(reserve.exists())

    def test_untouched_files_pass_and_only_the_changed_one_is_restored(self):
        self.guard('signature')
        self.b.write_text('legit? no: run changed it\n')
        self.a.write_text('4.3\n')
        result = self.guard('verify')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(str(self.a), result.stdout)
        self.assertEqual(self.b.read_text(), '2.0\n')
        self.assertEqual(self.guard('signature').returncode, 0)
        self.assertEqual(self.guard('verify').returncode, 0)
        # B110 (18.09): verification without a signature used to be rc=2 ("warning") and the CALLER counted only rc==1 as a violation
        # — so deleting the manifest was the easy way past the protection. Now a missing signature is a VIOLATION.
        self.assertEqual(self.guard('verify').returncode, 1)

    def test_manifest_changed_while_trusted_stdin_arrives_cannot_hide_a_violation(self):
        self.assertEqual(self.guard('signature').returncode, 0)
        self.b.write_text('99.0\n')
        bin_dir = self.home / 'bin'
        bin_dir.mkdir()
        cat = bin_dir / 'cat'
        # The delivered stdin is the same; an external writer replaces the file atomically exactly in this window.
        cat.write_text('''#!/bin/bash
input="$(/bin/cat "$@")"
printf '%s\\n' "$input"
printf '#LINE 0\\n' > "$CADRE_HOME/chain/fake.txt"
mv "$CADRE_HOME/chain/fake.txt" "$CADRE_HOME/chain/.signature-fixture.txt"
: > "$CADRE_HOME/race-ran"
''')
        cat.chmod(0o755)
        self.env['PATH'] = f"{bin_dir}:{os.environ['PATH']}"
        result = self.guard('verify')
        self.assertTrue((self.home / 'race-ran').exists())
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('VIOLATION', result.stdout)
        self.assertEqual(self.b.read_text(), '2.0\n')

    def test_runner_refuses_success_after_a_critical_file_violation(self):
        f = fixtures.SessionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        root = f.home/'guard-root'
        (root/'lib').mkdir(parents=True)
        (root/'bin').mkdir()
        (root/'lib/common.sh').write_text('qe_verify_ai_worktree(){ return 0; }\n')
        (root/'lib/run_ledger.py').write_text("print('fixture ledger')\n")
        (root/'bin/restricted-shell.sh').write_text('[ "$1" = verify ] && { echo "VIOLATION: run changed a critical file: x"; exit 1; }\nexit 0\n')
        from unittest.mock import Mock
        fixtures.runner_source(f.home, f.wt)
        original = subprocess.Popen
        def popen(cmd, *args, **kwargs):
            if cmd[0] == 'caffeinate':
                return Mock()
            return original(cmd, *args, **kwargs)
        with patch.object(core, 'CODE', root), patch.object(core, 'OC_LOG', f.home/'absent.log'), patch.object(core, 'guard_revive'), \
             patch.object(core, 'commit_wip', return_value=''), patch.object(core, 'finalize_run') as gate, \
             patch.object(subprocess, 'Popen', side_effect=popen), \
             patch.dict(os.environ, {'CADRE_REAL_OPENCODE': str(f.fake), 'CADRE_MAX_RESUME': '0', 'CADRE_PROGRESS_POLL': '1', 'CADRE_RUN_MODEL': '', 'CADRE_TASK_ID': ''}):
            rc = core.runner('a', f.run['prompt'])
        self.assertEqual(rc, 1)
        gate.assert_not_called()
        self.assertEqual(f.store.read(f.rid)['tasks']['main']['state'], 'half')

    def test_runner_rejects_a_run_that_forges_the_manifest_file(self):
        """D07: even if the executor changes a protected file and sets the writable manifest to `#LINE 0`, the engine sees the violation."""
        f = fixtures.SessionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        root = f.home/'guard-root'
        (root/'lib').mkdir(parents=True)
        (root/'bin').mkdir()
        (root/'lib/common.sh').write_text('qe_verify_ai_worktree(){ return 0; }\n')
        (root/'lib/run_ledger.py').write_text("print('fixture ledger')\n")
        (root/'bin/restricted-shell.sh').write_text((LIB.parent/'bin/restricted-shell.sh').read_text())
        protected = f.home/'protected.txt'
        protected.write_text('original\n')
        f.fake.write_text(f.fake.read_text() + f"""
import glob, pathlib
pathlib.Path({str(protected)!r}).write_text('changed\\n')
for m in glob.glob({str(f.home/'chain/.signature-*.txt')!r}):
    pathlib.Path(m).write_text('#LINE 0\\n')
""")
        from unittest.mock import Mock
        fixtures.runner_source(f.home, f.wt)
        original = subprocess.Popen
        def popen(cmd, *args, **kwargs):
            if cmd[0] == 'caffeinate':
                return Mock()
            return original(cmd, *args, **kwargs)
        with patch.object(core, 'CODE', root), patch.object(core, 'OC_LOG', f.home/'absent.log'), patch.object(core, 'guard_revive'), \
             patch.object(core, 'commit_wip', return_value=''), patch.object(core, 'finalize_run') as gate, \
             patch.object(subprocess, 'Popen', side_effect=popen), \
             patch.dict(os.environ, {'CADRE_REAL_OPENCODE': str(f.fake), 'CADRE_MAX_RESUME': '0', 'CADRE_PROGRESS_POLL': '1', 'CADRE_RUN_MODEL': '', 'CADRE_TASK_ID': '',
                                     'CADRE_HOME': str(f.home), 'CADRE_CRITICAL_FILES': str(protected)}):
            rc = core.runner('a', f.run['prompt'])
        self.assertEqual(rc, 1)
        gate.assert_not_called()
        self.assertEqual(protected.read_text(), 'original\n')

    def test_cancelled_run_still_restores_critical_files_and_keeps_cancel_state(self):
        f = fixtures.SessionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        root = f.home/'guard-root'
        (root/'lib').mkdir(parents=True)
        (root/'bin').mkdir()
        (root/'lib/common.sh').write_text('qe_verify_ai_worktree(){ return 0; }\n')
        (root/'lib/run_ledger.py').write_text("print('fixture ledger')\n")
        (root/'bin/restricted-shell.sh').write_text((LIB.parent/'bin/restricted-shell.sh').read_text())
        protected = f.home/'protected.txt'
        protected.write_text('original\n')
        cancel = f.store.path(f.rid)/'cancel.flag'
        f.fake.write_text(f.fake.read_text() + f"""
import pathlib, time
pathlib.Path({str(protected)!r}).write_text('changed\\n')
pathlib.Path({str(cancel)!r}).write_text('cancel\\n')
time.sleep(30)
""")
        from unittest.mock import Mock
        fixtures.runner_source(f.home, f.wt)
        original = subprocess.Popen
        def popen(cmd, *args, **kwargs):
            if cmd[0] == 'caffeinate':
                return Mock()
            return original(cmd, *args, **kwargs)
        def on_plan_cancel(*_a, **_k):
            protected.write_text('changed\n')
            raise core.CancelledRun('cancel')
        for on_plan, expected in ((lambda *_a, **_k: None, 1), (on_plan_cancel, core.CancelledRun)):
            protected.write_text('original\n')
            cancel.unlink(missing_ok=True)
            f.store.task_update(f.rid, 'main', state='running', attempt_count=0)
            with patch.object(core, 'CODE', root), patch.object(core, 'OC_LOG', f.home/'absent.log'), patch.object(core, 'guard_revive'), \
                 patch.object(core, 'commit_wip', return_value=''), patch.object(core, 'finalize_run') as gate, \
                 patch.object(core, 'on_plan', side_effect=on_plan), patch.object(subprocess, 'Popen', side_effect=popen), \
                 patch.dict(os.environ, {'CADRE_REAL_OPENCODE': str(f.fake), 'CADRE_MAX_RESUME': '0', 'CADRE_PROGRESS_POLL': '1', 'CADRE_RUN_MODEL': '',
                                         'CADRE_TASK_ID': '', 'CADRE_HOME': str(f.home), 'CADRE_CRITICAL_FILES': str(protected)}):
                if expected == 1:
                    self.assertEqual(core.runner('a', f.run['prompt']), 1)
                    self.assertEqual(f.store.read(f.rid)['tasks']['main']['state'], 'cancel', 'cancellation does not become retryable')
                else:
                    with self.assertRaises(expected):
                        core.runner('a', f.run['prompt'])
            gate.assert_not_called()
            self.assertEqual(protected.read_text(), 'original\n', 'a protected file changed by a cancelled or failed run is restored')
            self.assertEqual(list((f.home/'chain').glob('.signature-*')), [], 'the signature manifest is consumed')


class ReviewScopeTests(unittest.TestCase):
    def test_review_instruction_lists_every_changed_file_since_accepted_sha(self):
        f = completion.CompletionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.successful_qa()
        (f.wt/'second.txt').write_text('second commit\n')
        f.git('add', '.')
        f.git('-c', 'user.name=x', '-c', 'user.email=x@x', 'commit', '-qm', 'second')
        (f.wt/'untracked.txt').write_text('not committed\n')
        with patch.object(core, 'capture_opencode', return_value=f.review()) as reviewer:
            result = core.finalize_run('a', str(f.wt), f.log, '/fake/cli', review_model='')
        self.assertEqual(result['status'], 'PASS')
        instruction = reviewer.call_args.args[2][-1]
        payload = json.loads(instruction.splitlines()[-1])
        self.assertEqual(payload['changed_files'], ['product.txt', 'second.txt', 'untracked.txt'])
        scope = list(f.store.task_dir(f.rid, 'quality').glob('scope-*.txt'))
        self.assertEqual(len(scope), 1)


class MonitorCompletionTests(unittest.TestCase):
    def test_running_tool_stays_live_regardless_of_age(self):
        import watch
        temp = tempfile.TemporaryDirectory(prefix='qe-watch-')
        self.addCleanup(temp.cleanup)
        home = Path(temp.name)
        db = home/'oc.db'
        con = sqlite3.connect(db)
        con.executescript('''
            create table session(id text, parent_id text, agent text, model text, title text, directory text, time_created integer, time_updated integer);
            create table message(id text, session_id text, data text, time_created integer);
            create table part(id text, session_id text, message_id text, data text, time_created integer, time_updated integer);''')
        old = int((time.time() - 120) * 1000)
        con.execute('insert into session values(?,?,?,?,?,?,?,?)', ('s1', None, 'build', json.dumps({'providerID':'lmstudio','id':'q'}), 't', str(home/'wt'), old, old))
        con.execute('insert into message values(?,?,?,?)', ('m1', 's1', json.dumps({'role':'assistant','agent':'build'}), old))
        con.execute('insert into part values(?,?,?,?,?,?)', ('p1', 's1', 'm1', json.dumps({'type':'tool','tool':'bash','state':{'status':'running','input':{'command':'make check'}}}), old, old))
        con.commit()
        con.close()
        with patch.object(watch, 'DB', db), patch.object(watch, 'QE', home):
            viewer = watch.Watcher('a', str(home/'wt'), plain=True)
            viewer.round()
            self.assertEqual(viewer.seen.get('p1'), 'live')
            self.assertEqual(viewer.count['tool'], 0)
            con = sqlite3.connect(db)
            con.execute("update part set data=? where id='p1'", (json.dumps({'type':'tool','tool':'bash','state':{'status':'completed','input':{'command':'make check'},'output':'ok'}}),))
            con.commit()
            con.close()
            viewer.round()
            self.assertEqual(viewer.seen.get('p1'), 'done')
            self.assertEqual(viewer.count['tool'], 1)

    def test_project_mode_viewer_follows_the_active_child_run(self):
        # B308: in project mode the main run has no worktree and no work session; if the watcher does not attach to the child the screen freezes.
        import io, watch
        from contextlib import redirect_stdout
        temp = tempfile.TemporaryDirectory(prefix='qe-watch-')
        self.addCleanup(temp.cleanup)
        home = Path(temp.name)
        db = home/'oc.db'
        con = sqlite3.connect(db)
        con.executescript('''
            create table session(id text, parent_id text, agent text, model text, title text, directory text, time_created integer, time_updated integer);
            create table message(id text, session_id text, data text, time_created integer);
            create table part(id text, session_id text, message_id text, data text, time_created integer, time_updated integer);''')
        now = int(time.time() * 1000)
        con.execute('insert into session values(?,?,?,?,?,?,?,?)', ('c1', None, 'build', json.dumps({'providerID':'lmstudio','id':'q'}), 't', str(home/'wt'), now, now))
        con.execute('insert into message values(?,?,?,?)', ('m1', 'c1', json.dumps({'role':'assistant','agent':'build'}), now))
        con.execute('insert into part values(?,?,?,?,?,?)', ('p1', 'c1', 'm1', json.dumps({'type':'tool','tool':'bash','state':{'status':'completed','input':{'command':'make check'},'output':'ok'}}), now, now))
        con.commit()
        con.close()
        parent, child = '20261002-054123-' + 'a' * 32, '20261002-054347-' + 'b' * 32
        for rid, wt, sessions in ((parent, '', []), (child, str(home/'wt'), ['c1'])):
            (home/'runs'/rid).mkdir(parents=True)
            (home/'runs'/rid/'run.json').write_text(json.dumps({'version': 1, 'run_id': rid, 'project': 'a', 'state': 'running', 'worktree': wt,
                                                                'created': now / 1000, 'updated': now / 1000,
                                                                'tasks': {'main': {'executors': {'opencode:build': {'sessions': sessions}}}}}))
        (home/'runs'/parent/'coordinator.log').write_text('')
        (home/'runs'/parent/'project-ref-active.json').write_text(json.dumps({'child': child}))
        (home/'runs'/child/'coordinator.log').write_text('2026-10-02 05:44:16 starting the run\n')
        with patch.object(watch, 'DB', db), patch.object(watch, 'QE', home):
            viewer = watch.Watcher('a', '', plain=True, single=True, thought=True, run_id=parent, runner_log=str(home/'runs'/parent/'coordinator.log'))
            out = io.StringIO()
            with redirect_stdout(out):
                viewer.execute()
        self.assertEqual(viewer.wt, str(home/'wt'))
        self.assertIn('c1', viewer.session_seen)
        self.assertEqual(viewer.count['tool'], 1)
        self.assertIn('starting the run', out.getvalue())


class LedgerRedactionTests(unittest.TestCase):
    def test_transcript_sinks_are_redacted_but_product_archive_stays_exact(self):
        import run_ledger as ledger
        f = fixtures.SessionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        fake = f.home/'fake-claude-secret'
        fake.write_text(f"#!{sys.executable}\n" + f"""
import json,sys
sid=sys.argv[sys.argv.index('--session-id')+1]
print(json.dumps({{'type':'assistant','session_id':sid,'message':{{'content':[{{'type':'tool_use','name':'Bash','input':{{'command':'echo {SECRET}'}}}}]}}}}))
print(json.dumps({{'type':'result','session_id':sid,'result':'leaked {SECRET} in output','total_cost_usd':.1}}))
""")
        fake.chmod(0o700)
        f.allow_fixture_claude(fake)
        (f.wt/'config.txt').write_text('KEY=' + SECRET + '\n')   # product bytes must survive archival exactly
        with patch.dict(os.environ, {'CLAUDE_BIN': str(fake)}), patch.object(ledger, 'DB', f.home/'absent.db'), \
             patch.object(ledger, 'config_snapshot'), patch.object(ledger, 'watcher_slices', return_value={}):
            core.run_claude(str(f.wt), 'claude/sonnet#high', 'task', f.log).wait(timeout=5)
            self.assertEqual(ledger.scan(), 1)
        out = f.home/'reports/run-a-worktree'
        for name in ('transcript.md', 'transcript.jsonl', 'runner.log'):
            path = out/name
            if path.exists():
                self.assertNotIn(SECRET, path.read_text(), name)
        for path in (out/'attempts').rglob('*'):
            if path.is_file() and path.suffix in ledger.REDACTED_EXTENSION:
                self.assertNotIn(SECRET, path.read_text(errors='replace'), str(path))
        import worktree_archive
        manifest = Path(json.loads((out/'worktree-archive.json').read_text())['manifest'])
        restored = f.home/'restored'
        worktree_archive.restore(manifest, restored)
        self.assertEqual((restored/'config.txt').read_text(), 'KEY=' + SECRET + '\n')
        self.assertGreaterEqual(json.loads((out/'meta.json').read_text())['redaction']['masked_file'], 1)


class QuotaLogTests(unittest.TestCase):
    def test_hourly_line_reports_go_pool_points_from_step_receipts(self):
        import quota_trend
        temp = tempfile.TemporaryDirectory(prefix='qe-quota-')
        self.addCleanup(temp.cleanup)
        db = Path(temp.name)/'oc.db'
        now = 1_800_000_000.0
        con = sqlite3.connect(db)
        con.execute('create table message(id text, data text)')
        con.execute('create table part(id text, message_id text, time_created integer, data text)')
        con.execute('insert into message values(?,?)', ('m1', json.dumps({'providerID':'opencode-go'})))
        con.execute('insert into message values(?,?)', ('m2', json.dumps({'providerID':'deepseek'})))
        rows = [('fresh', 'm1', int((now - 3600) * 1000), 0.5), ('old', 'm1', int((now - 6 * 3600) * 1000), 9.0), ('other', 'm2', int((now - 60) * 1000), 7.0)]
        for ident, message, stamp, cost in rows:
            con.execute('insert into part values(?,?,?,?)', (ident, message, stamp, json.dumps({'type':'step-finish','cost':cost})))
        con.commit()
        con.close()
        line = quota_trend.line(db, now=now)
        self.assertIn('1 step', line)
        self.assertIn('3.33 monthly points', line)   # $0.5 / $15 (without a model = the heaviest limit); old receipts and other providers are out
        self.assertIn('rolling %16.7', line)
        self.assertIn('UNKNOWN', quota_trend.line(Path(temp.name)/'absent.db', now=now))



class SecretRedactionTests(unittest.TestCase):
    def test_a_private_key_body_is_masked_in_text_json_and_truncated_logs(self):
        import run_ledger
        body = 'MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7' * 3
        pem = f'-----BEGIN PRIVATE KEY-----\n{body}\n{body}\n-----END PRIVATE KEY-----'
        for text in (f'once {pem} then', json.dumps({'x': pem}), '-----BEGIN RSA PRIVATE KEY-----\n' + body):
            with self.subTest(text=text[:30]):
                masked = run_ledger.redacted(text)
                self.assertNotIn(body[:24], masked)
                self.assertIn('[REDACTED]', masked)
        self.assertEqual(run_ledger.redacted(f'once {pem} then'), 'once [REDACTED] then')


if __name__ == '__main__':
    unittest.main()

class OtherSessionTests(unittest.TestCase):
    def test_a_new_home_ledger_starts_at_its_first_run_and_never_imports_older_foreign_sessions(self):
        import run_ledger as ledger
        f = fixtures.SessionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        outside = Path(tempfile.mkdtemp(prefix='other-project-ref-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(outside, ignore_errors=True))
        db = f.home / 'oc.db'
        con = sqlite3.connect(db)
        con.executescript('''
            create table session(id text, parent_id text, agent text, model text, title text, directory text, time_created integer, time_updated integer);
            create table message(id text, session_id text, data text, time_created integer);
            create table part(id text, session_id text, message_id text, data text, time_created integer, time_updated integer);''')
        old = int((time.time() - 86400) * 1000)
        fresh = int((time.time() + 5) * 1000)
        for sid, t in (('ses-old', old), ('ses-fresh', fresh)):
            con.execute('insert into session values(?,?,?,?,?,?,?,?)', (sid, None, 'build', '{}', 't', str(outside), t, t))
        con.commit()
        con.close()
        with patch.object(ledger, 'DB', db), patch.object(ledger, 'dump') as dump:
            ledger.scan()
            first = [c.kwargs['sessions'][0]['id'] for c in dump.call_args_list if c.kwargs.get('sessions')]
            self.assertEqual(ledger.WATERMARK.read_text().strip(), str(fresh))
            dump.reset_mock()
            ledger.scan()
        self.assertEqual(first, ['ses-fresh'], 'A foreign session predating this home is not imported into its ledger')
        self.assertFalse([c for c in dump.call_args_list if c.kwargs.get('sessions')], 'The scan is incremental; do not process the same session twice')

    def test_session_opened_in_the_qe_home_does_not_break_the_scan_and_the_watermark_advances(self):
        """B276 (30.09 watch log): the directory of 4 'other' sessions left from 23.09 was the QE home itself; the archive failed with 'archive cannot be
        inside its source', `failed` turned the whole scan into an OSError and held the watermark at 23.09 — the watch wrote the same error every 30 min
        and no new session was ledgered. A session that cannot be archived is SKIPPED, the scan continues, the watermark moves on."""
        import sqlite3, time
        import run_ledger as ledger
        f = fixtures.SessionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        db = f.home / 'oc.db'
        con = sqlite3.connect(db)
        con.executescript('''
            create table session(id text, parent_id text, agent text, model text, title text, directory text, time_created integer, time_updated integer);
            create table message(id text, session_id text, data text, time_created integer);
            create table part(id text, session_id text, message_id text, data text, time_created integer, time_updated integer);''')
        t = int((time.time() - 60) * 1000)
        con.execute('insert into session values(?,?,?,?,?,?,?,?)', ('sx', None, 'build', json.dumps({'providerID': 'lmstudio', 'id': 'q'}), 't', str(f.home), t, t))
        con.commit()
        con.close()
        ledger.WATERMARK.parent.mkdir(parents=True, exist_ok=True)
        ledger.WATERMARK.write_text(str(t - 1000))
        with patch.object(ledger, 'DB', db), patch.object(ledger, 'config_snapshot'), patch.object(ledger, 'watcher_slices', return_value={}):
            ledger.scan()   # must not raise OSError
        self.assertEqual(ledger.WATERMARK.read_text().strip(), str(t), 'The watermark did not advance')
