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

import test_kosu_sessions as fixtures
import test_sonuc as completion
import qe_core as core


def setUpModule():
    ortam = patch.dict(os.environ, {'CADRE_ZINCIR_KALITE': 'opencode-go/qwen3.8-max'})
    ortam.start()
    unittest.addModuleCleanup(ortam.stop)


LIB = fixtures.LIB
SECRET = 'sk-' + 'Q' * 40


class CriticalFileGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-guard-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home/'zincir').mkdir()
        self.a = self.home/'zincir/maliyet-katsayi.txt'
        self.b = self.home/'zincir/go-gunluk-tavan.txt'
        self.a.write_text('4.3\n')
        self.b.write_text('2.0\n')
        self.yedek_koku = Path(tempfile.mkdtemp(prefix='imza-yedek-'))
        self.addCleanup(shutil.rmtree, self.yedek_koku, True)
        self.env = {**os.environ, 'CADRE_HOME': str(self.home), 'CADRE_KRITIK_DOSYALAR': f'{self.a}:{self.b}',
                    'CADRE_IMZA_YEDEK_KOKU': str(self.yedek_koku)}

    def guard(self, komut, manifest=None):
        """sign: keeps the output (the engine's memory); verify: per B288 the manifest comes only from stdin."""
        ek = ['-'] if komut == 'dogrula' else []
        girdi = (self.manifest if manifest is None else manifest) if komut == 'dogrula' else None
        sonuc = subprocess.run(['bash', str(LIB.parent/'bin/kisitli-kabuk.sh'), komut, 'fixture', *ek], env=self.env,
                               input=girdi, capture_output=True, text=True, timeout=20)
        if komut == 'imza':
            self.manifest = sonuc.stdout
        elif manifest is None:
            self.manifest = ''   # verification consumes the signature; a second verification without a signature sees an empty manifest
        return sonuc

    def test_deleted_and_modified_files_are_violations_and_restored_individually(self):
        self.assertEqual(self.guard('imza').returncode, 0)
        self.a.unlink()
        self.b.write_text('99.0\n')
        result = self.guard('dogrula')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('SILDI', result.stdout)
        self.assertIn('degistirdi', result.stdout)
        self.assertEqual(self.a.read_text(), '4.3\n')
        self.assertEqual(self.b.read_text(), '2.0\n')

    def test_a_deleted_or_emptied_manifest_is_itself_a_violation(self):
        """B110 (18.09): an empty/deleted manifest was the easy way to remove the protection. B288: the manifest is in the engine's memory;
        an empty manifest, one with a tampered line count or none at all (without stdin) is a VIOLATION."""
        self.assertEqual(self.guard('imza').returncode, 0)
        sonuc = self.guard('dogrula', manifest='')
        self.assertEqual(sonuc.returncode, 1, sonuc.stdout)
        self.assertIn('IHLAL', sonuc.stdout)
        self.guard('imza')
        sonuc = subprocess.run(['bash', str(LIB.parent/'bin/kisitli-kabuk.sh'), 'dogrula', 'fixture'], env=self.env,
                               capture_output=True, text=True, timeout=20)
        self.assertEqual(sonuc.returncode, 1, sonuc.stdout)
        self.assertIn('stdin', sonuc.stdout)
        self.guard('imza')
        self.assertEqual(self.guard('dogrula', manifest=self.manifest.replace('#SATIR 2', '#SATIR 3')).returncode, 1)

    def test_forged_manifest_cannot_hide_a_change_when_the_motor_holds_the_manifest(self):
        """D07: the manifest is where the agent can write; writing `#SATIR 0` let a changed file pass with rc=0.
        The root of trust is the signing output in the engine's memory (stdin); the writable file and the backup are checked against it."""
        self.assertEqual(self.guard('imza').returncode, 0)
        self.b.write_text('99.0\n')
        (self.home/'zincir/.imza-fixture.txt').write_text('#SATIR 0\n')
        sonuc = self.guard('dogrula')
        self.assertEqual(sonuc.returncode, 1, sonuc.stdout)
        self.assertEqual(self.b.read_text(), '2.0\n')
        self.guard('imza')
        self.a.write_text('degisti\n')
        h = self.manifest.split()[0]
        (self.yedek_koku/f'fixture.yedek/{h}').write_text('ajanin surumu\n')   # if the backup is corrupted it is not restored
        sonuc = self.guard('dogrula')
        self.assertEqual(sonuc.returncode, 1, sonuc.stdout)
        self.assertNotEqual(self.a.read_text(), 'ajanin surumu\n')
        self.assertIn('yedek bozuk', sonuc.stdout)

    def test_backups_live_outside_the_data_root_and_stale_orphans_are_removed(self):
        """Audit C-15/R-23: an auth.json copy is not written into CADRE_HOME (the area the sandbox can read); an old backup left by an
        interrupted run is cleaned at the next signing."""
        yetim = self.yedek_koku / 'eski.yedek'
        yetim.mkdir()
        (yetim / 'sir').write_text('eski kopya\n')
        eski = time.time() - 2 * 86400
        os.utime(yetim, (eski, eski))
        self.assertEqual(self.guard('imza').returncode, 0)
        self.assertFalse(yetim.exists())
        self.assertFalse((self.home / 'zincir/.imza-fixture.yedek').exists())
        yedek = self.yedek_koku / 'fixture.yedek'
        self.assertTrue(yedek.is_dir())
        self.assertEqual(oct(yedek.stat().st_mode & 0o777), '0o700')
        self.assertEqual(sorted(p.name for p in yedek.iterdir()), sorted(self.manifest.split()[::2][:2]))
        self.assertEqual(self.guard('dogrula').returncode, 0)
        self.assertFalse(yedek.exists())

    def test_untouched_files_pass_and_only_the_changed_one_is_restored(self):
        self.guard('imza')
        self.b.write_text('legit? no: run changed it\n')
        self.a.write_text('4.3\n')
        result = self.guard('dogrula')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(str(self.a), result.stdout)
        self.assertEqual(self.b.read_text(), '2.0\n')
        self.assertEqual(self.guard('imza').returncode, 0)
        self.assertEqual(self.guard('dogrula').returncode, 0)
        # B110 (18.09): verification without a signature used to be rc=2 ("warning") and the CALLER counted only rc==1 as a violation
        # — so deleting the manifest was the easy way past the protection. Now a missing signature is a VIOLATION.
        self.assertEqual(self.guard('dogrula').returncode, 1)

    def test_manifest_changed_while_trusted_stdin_arrives_cannot_hide_a_violation(self):
        self.assertEqual(self.guard('imza').returncode, 0)
        self.b.write_text('99.0\n')
        bin_dir = self.home / 'bin'
        bin_dir.mkdir()
        cat = bin_dir / 'cat'
        # The delivered stdin is the same; an external writer replaces the file atomically exactly in this window.
        cat.write_text('''#!/bin/bash
girdi="$(/bin/cat "$@")"
printf '%s\\n' "$girdi"
printf '#SATIR 0\\n' > "$CADRE_HOME/zincir/sahte.txt"
mv "$CADRE_HOME/zincir/sahte.txt" "$CADRE_HOME/zincir/.imza-fixture.txt"
: > "$CADRE_HOME/yaris-kostu"
''')
        cat.chmod(0o755)
        self.env['PATH'] = f"{bin_dir}:{os.environ['PATH']}"
        result = self.guard('dogrula')
        self.assertTrue((self.home / 'yaris-kostu').exists())
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('IHLAL', result.stdout)
        self.assertEqual(self.b.read_text(), '2.0\n')

    def test_motor_refuses_success_after_a_critical_file_violation(self):
        f = fixtures.SessionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        root = f.home/'guard-root'
        (root/'lib').mkdir(parents=True)
        (root/'bin').mkdir()
        (root/'lib/common.sh').write_text('qe_verify_ai_worktree(){ return 0; }\n')
        (root/'lib/kosu_defteri.py').write_text("print('fixture ledger')\n")
        (root/'bin/kisitli-kabuk.sh').write_text('[ "$1" = dogrula ] && { echo "IHLAL: kosu kritik dosyayi degistirdi: x"; exit 1; }\nexit 0\n')
        from unittest.mock import Mock
        fixtures.motor_source(f.home, f.wt)
        original = subprocess.Popen
        def popen(cmd, *args, **kwargs):
            if cmd[0] == 'caffeinate':
                return Mock()
            return original(cmd, *args, **kwargs)
        with patch.object(core, 'CODE', root), patch.object(core, 'OC_LOG', f.home/'absent.log'), patch.object(core, 'bekci_dirilt'), \
             patch.object(core, 'commit_wip', return_value=''), patch.object(core, 'finalize_run') as gate, \
             patch.object(subprocess, 'Popen', side_effect=popen), \
             patch.dict(os.environ, {'CADRE_REAL_OPENCODE': str(f.fake), 'CADRE_MAX_RESUME': '0', 'CADRE_PROGRESS_POLL': '1', 'CADRE_RUN_MODEL': '', 'CADRE_GOREV_ID': ''}):
            rc = core.motor('a', f.run['prompt'])
        self.assertEqual(rc, 1)
        gate.assert_not_called()
        self.assertEqual(f.store.read(f.rid)['tasks']['main']['state'], 'yarim')

    def test_motor_rejects_a_run_that_forges_the_manifest_file(self):
        """D07: even if the executor changes a protected file and sets the writable manifest to `#SATIR 0`, the engine sees the violation."""
        f = fixtures.SessionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        root = f.home/'guard-root'
        (root/'lib').mkdir(parents=True)
        (root/'bin').mkdir()
        (root/'lib/common.sh').write_text('qe_verify_ai_worktree(){ return 0; }\n')
        (root/'lib/kosu_defteri.py').write_text("print('fixture ledger')\n")
        (root/'bin/kisitli-kabuk.sh').write_text((LIB.parent/'bin/kisitli-kabuk.sh').read_text())
        korunan = f.home/'korunan.txt'
        korunan.write_text('asil\n')
        f.fake.write_text(f.fake.read_text() + f"""
import glob, pathlib
pathlib.Path({str(korunan)!r}).write_text('degisti\\n')
for m in glob.glob({str(f.home/'zincir/.imza-*.txt')!r}):
    pathlib.Path(m).write_text('#SATIR 0\\n')
""")
        from unittest.mock import Mock
        fixtures.motor_source(f.home, f.wt)
        original = subprocess.Popen
        def popen(cmd, *args, **kwargs):
            if cmd[0] == 'caffeinate':
                return Mock()
            return original(cmd, *args, **kwargs)
        with patch.object(core, 'CODE', root), patch.object(core, 'OC_LOG', f.home/'absent.log'), patch.object(core, 'bekci_dirilt'), \
             patch.object(core, 'commit_wip', return_value=''), patch.object(core, 'finalize_run') as gate, \
             patch.object(subprocess, 'Popen', side_effect=popen), \
             patch.dict(os.environ, {'CADRE_REAL_OPENCODE': str(f.fake), 'CADRE_MAX_RESUME': '0', 'CADRE_PROGRESS_POLL': '1', 'CADRE_RUN_MODEL': '', 'CADRE_GOREV_ID': '',
                                     'CADRE_HOME': str(f.home), 'CADRE_KRITIK_DOSYALAR': str(korunan)}):
            rc = core.motor('a', f.run['prompt'])
        self.assertEqual(rc, 1)
        gate.assert_not_called()
        self.assertEqual(korunan.read_text(), 'asil\n')

    def test_cancelled_run_still_restores_critical_files_and_keeps_cancel_state(self):
        f = fixtures.SessionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        root = f.home/'guard-root'
        (root/'lib').mkdir(parents=True)
        (root/'bin').mkdir()
        (root/'lib/common.sh').write_text('qe_verify_ai_worktree(){ return 0; }\n')
        (root/'lib/kosu_defteri.py').write_text("print('fixture ledger')\n")
        (root/'bin/kisitli-kabuk.sh').write_text((LIB.parent/'bin/kisitli-kabuk.sh').read_text())
        korunan = f.home/'korunan.txt'
        korunan.write_text('asil\n')
        iptal = f.store.path(f.rid)/'cancel.flag'
        f.fake.write_text(f.fake.read_text() + f"""
import pathlib, time
pathlib.Path({str(korunan)!r}).write_text('degisti\\n')
pathlib.Path({str(iptal)!r}).write_text('iptal\\n')
time.sleep(30)
""")
        from unittest.mock import Mock
        fixtures.motor_source(f.home, f.wt)
        original = subprocess.Popen
        def popen(cmd, *args, **kwargs):
            if cmd[0] == 'caffeinate':
                return Mock()
            return original(cmd, *args, **kwargs)
        def on_plan_iptal(*_a, **_k):
            korunan.write_text('degisti\n')
            raise core.CancelledRun('iptal')
        for on_plan, beklenen in ((lambda *_a, **_k: None, 1), (on_plan_iptal, core.CancelledRun)):
            korunan.write_text('asil\n')
            iptal.unlink(missing_ok=True)
            f.store.task_update(f.rid, 'main', state='running', attempt_count=0)
            with patch.object(core, 'CODE', root), patch.object(core, 'OC_LOG', f.home/'absent.log'), patch.object(core, 'bekci_dirilt'), \
                 patch.object(core, 'commit_wip', return_value=''), patch.object(core, 'finalize_run') as gate, \
                 patch.object(core, 'on_plan', side_effect=on_plan), patch.object(subprocess, 'Popen', side_effect=popen), \
                 patch.dict(os.environ, {'CADRE_REAL_OPENCODE': str(f.fake), 'CADRE_MAX_RESUME': '0', 'CADRE_PROGRESS_POLL': '1', 'CADRE_RUN_MODEL': '',
                                         'CADRE_GOREV_ID': '', 'CADRE_HOME': str(f.home), 'CADRE_KRITIK_DOSYALAR': str(korunan)}):
                if beklenen == 1:
                    self.assertEqual(core.motor('a', f.run['prompt']), 1)
                    self.assertEqual(f.store.read(f.rid)['tasks']['main']['state'], 'iptal', 'iptal yeniden denenebilir duruma çevrilmez')
                else:
                    with self.assertRaises(beklenen):
                        core.motor('a', f.run['prompt'])
            gate.assert_not_called()
            self.assertEqual(korunan.read_text(), 'asil\n', 'iptal ya da istisnayla çıkan koşunun değiştirdiği korunan dosya geri alınır')
            self.assertEqual(list((f.home/'zincir').glob('.imza-*')), [], 'imza manifesti tüketilir')


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
        scope = list(f.store.task_dir(f.rid, 'kalite').glob('kapsam-*.txt'))
        self.assertEqual(len(scope), 1)


class MonitorCompletionTests(unittest.TestCase):
    def test_running_tool_stays_live_regardless_of_age(self):
        import izle
        temp = tempfile.TemporaryDirectory(prefix='qe-izle-')
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
        with patch.object(izle, 'DB', db), patch.object(izle, 'QE', home):
            viewer = izle.Izleyici('a', str(home/'wt'), duz=True)
            viewer.tur()
            self.assertEqual(viewer.gorulen.get('p1'), 'canli')
            self.assertEqual(viewer.say['arac'], 0)
            con = sqlite3.connect(db)
            con.execute("update part set data=? where id='p1'", (json.dumps({'type':'tool','tool':'bash','state':{'status':'completed','input':{'command':'make check'},'output':'ok'}}),))
            con.commit()
            con.close()
            viewer.tur()
            self.assertEqual(viewer.gorulen.get('p1'), 'tam')
            self.assertEqual(viewer.say['arac'], 1)

    def test_project_mode_viewer_follows_the_active_child_run(self):
        # B308: in project mode the main run has no worktree and no work session; if the watcher does not attach to the child the screen freezes.
        import io, izle
        from contextlib import redirect_stdout
        temp = tempfile.TemporaryDirectory(prefix='qe-izle-')
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
        (home/'runs'/parent/'proje-aktif.json').write_text(json.dumps({'cocuk': child}))
        (home/'runs'/child/'coordinator.log').write_text('2026-10-02 05:44:16 kosu basliyor\n')
        with patch.object(izle, 'DB', db), patch.object(izle, 'QE', home):
            viewer = izle.Izleyici('a', '', duz=True, tek=True, dusunce=True, run_id=parent, motor_log=str(home/'runs'/parent/'coordinator.log'))
            out = io.StringIO()
            with redirect_stdout(out):
                viewer.kos()
        self.assertEqual(viewer.wt, str(home/'wt'))
        self.assertIn('c1', viewer.oturum_gorulen)
        self.assertEqual(viewer.say['arac'], 1)
        self.assertIn('kosu basliyor', out.getvalue())


class LedgerRedactionTests(unittest.TestCase):
    def test_transcript_sinks_are_redacted_but_product_archive_stays_exact(self):
        import kosu_defteri as ledger
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
             patch.object(ledger, 'config_snapshot'), patch.object(ledger, 'izleyici_dilimleri', return_value={}):
            core.run_claude(str(f.wt), 'claude/sonnet#high', 'task', f.log).wait(timeout=5)
            self.assertEqual(ledger.tara(), 1)
        out = f.home/'reports/kosu-a-worktree'
        for name in ('transcript.md', 'transcript.jsonl', 'motor.log'):
            path = out/name
            if path.exists():
                self.assertNotIn(SECRET, path.read_text(), name)
        for path in (out/'attempts').rglob('*'):
            if path.is_file() and path.suffix in ledger.REDAKTE_UZANTI:
                self.assertNotIn(SECRET, path.read_text(errors='replace'), str(path))
        import arsiv
        manifest = Path(json.loads((out/'worktree-archive.json').read_text())['manifest'])
        restored = f.home/'restored'
        arsiv.restore(manifest, restored)
        self.assertEqual((restored/'config.txt').read_text(), 'KEY=' + SECRET + '\n')
        self.assertGreaterEqual(json.loads((out/'meta.json').read_text())['redaksiyon']['maskelenen_dosya'], 1)


class QuotaLogTests(unittest.TestCase):
    def test_hourly_line_reports_go_pool_points_from_step_receipts(self):
        import kota_gidisat
        temp = tempfile.TemporaryDirectory(prefix='qe-kota-')
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
        line = kota_gidisat.satir(db, now=now)
        self.assertIn('1 adim', line)
        self.assertIn('3.33 aylik puan', line)   # $0.5 / $15 (without a model = the heaviest limit); old receipts and other providers are out
        self.assertIn('rolling %16.7', line)
        self.assertIn('BILINMIYOR', kota_gidisat.satir(Path(temp.name)/'absent.db', now=now))



class SirMaskesiTests(unittest.TestCase):
    def test_a_private_key_body_is_masked_in_text_json_and_truncated_logs(self):
        import kosu_defteri
        govde = 'MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7' * 3
        pem = f'-----BEGIN PRIVATE KEY-----\n{govde}\n{govde}\n-----END PRIVATE KEY-----'
        for metin in (f'once {pem} sonra', json.dumps({'x': pem}), '-----BEGIN RSA PRIVATE KEY-----\n' + govde):
            with self.subTest(metin=metin[:30]):
                maskeli = kosu_defteri.redakte(metin)
                self.assertNotIn(govde[:24], maskeli)
                self.assertIn('[REDACTED]', maskeli)
        self.assertEqual(kosu_defteri.redakte(f'once {pem} sonra'), 'once [REDACTED] sonra')


if __name__ == '__main__':
    unittest.main()

class DigerOturumTests(unittest.TestCase):
    def test_a_new_home_ledger_starts_at_its_first_run_and_never_imports_older_foreign_sessions(self):
        import kosu_defteri as ledger
        f = fixtures.SessionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        dis = Path(tempfile.mkdtemp(prefix='baska-proje-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(dis, ignore_errors=True))
        db = f.home / 'oc.db'
        con = sqlite3.connect(db)
        con.executescript('''
            create table session(id text, parent_id text, agent text, model text, title text, directory text, time_created integer, time_updated integer);
            create table message(id text, session_id text, data text, time_created integer);
            create table part(id text, session_id text, message_id text, data text, time_created integer, time_updated integer);''')
        eski = int((time.time() - 86400) * 1000)
        yeni = int((time.time() + 5) * 1000)
        for sid, t in (('ses-eski', eski), ('ses-yeni', yeni)):
            con.execute('insert into session values(?,?,?,?,?,?,?,?)', (sid, None, 'build', '{}', 't', str(dis), t, t))
        con.commit()
        con.close()
        with patch.object(ledger, 'DB', db), patch.object(ledger, 'dump') as dump:
            ledger.tara()
            ilk = [c.kwargs['sessions'][0]['id'] for c in dump.call_args_list if c.kwargs.get('sessions')]
            self.assertEqual(ledger.WATERMARK.read_text().strip(), str(yeni))
            dump.reset_mock()
            ledger.tara()
        self.assertEqual(ilk, ['ses-yeni'], 'ev kurulmadan onceki yabanci oturum bu evin defterine alinmaz')
        self.assertFalse([c for c in dump.call_args_list if c.kwargs.get('sessions')], 'tarama artimli; ayni oturum tekrar islenmez')

    def test_qe_evinde_acilan_oturum_taramayi_kirmaz_ve_watermark_ilerler(self):
        """B276 (30.09 watch log): the directory of 4 'other' sessions left from 23.09 was the QE home itself; the archive failed with 'archive cannot be
        inside its source', `failed` turned the whole scan into an OSError and held the watermark at 23.09 — the watch wrote the same error every 30 min
        and no new session was ledgered. A session that cannot be archived is SKIPPED, the scan continues, the watermark moves on."""
        import sqlite3, time
        import kosu_defteri as ledger
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
        with patch.object(ledger, 'DB', db), patch.object(ledger, 'config_snapshot'), patch.object(ledger, 'izleyici_dilimleri', return_value={}):
            ledger.tara()   # must not raise OSError
        self.assertEqual(ledger.WATERMARK.read_text().strip(), str(t), 'watermark ilerlemedi')
