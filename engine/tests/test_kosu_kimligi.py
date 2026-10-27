"""CR-36 (N-IN02, N-OBS01, N-OBS05): a new run record carries the engine/config/environment identity; intervention records come from production
paths; old records are not invented."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import arayuz_veri
import izin
import kosu
from kosu import RunStore


class KimlikTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='kimlik-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / 'ev'
        (self.home / 'projects' / 'p').mkdir(parents=True)
        self.repo = self.tmp / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', '-C', str(self.repo), 'init', '-q', '-b', 'main'], check=True)
        (self.repo / 'a.py').write_text('x = 1\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', '-A'], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'init'], check=True)
        (self.home / 'projects' / 'p' / 'repo-path').write_text(str(self.repo) + '\n')
        (self.home / 'projects' / 'p' / 'base-branch').write_text('main\n')
        self.env = patch.dict(os.environ, {'CADRE_HOME': str(self.home), 'OPENCODE_CFG': str(self.tmp / 'cfg')}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop('CADRE_RUN_ID', None)
        self.store = RunStore(self.home)

    def test_accept_records_engine_config_and_environment_identity(self):
        record = self.store.accept('p', 'kucuk is')
        kimlik = record['provenance']
        self.assertEqual(kimlik['version'], 1)
        self.assertRegex(kimlik['engine']['guard_version'], '^[0-9a-f]{64}$')
        self.assertEqual(Path(kimlik['engine']['root']), Path(__file__).resolve().parents[1])
        self.assertIsNone(kimlik['engine']['git_head'])   # no subprocess at record time
        self.assertIsNone(kimlik['completed'])
        self.assertIsInstance(kimlik['runtime']['platform'], str)
        self.assertRegex(kimlik['runtime']['python'], r'^\d+\.\d+')
        self.assertIsNone(kimlik['runtime']['git_version'])
        tamam = self.store.kimlik_tamamla(record['run_id'])
        self.assertIsNotNone(tamam['completed'])
        self.assertIsInstance(tamam['runtime']['git_version'], str)
        self.assertIn(tamam['engine']['git_dirty'], (True, False, None))
        self.assertEqual(self.store.kimlik_tamamla(record['run_id']), tamam)   # a second call does not change it
        self.assertEqual(tamam['engine']['guard_version'], kimlik['engine']['guard_version'])
        self.assertEqual(record['operator_interventions'], [])
        self.assertEqual(kimlik['config']['opencode_jsonc_sha256'], None)   # no cfg directory: nothing is invented
        self.assertRegex(kimlik['config']['sinirlar_sha256'], '^[0-9a-f]{64}$')
        saved = self.store.read(record['run_id'])
        self.assertEqual(saved['provenance'], tamam)

    def test_request_record_carries_identity_and_identity_is_immutable(self):
        raw = {'submission_id': 'abc123', 'project': 'p', 'target_path': str(self.repo), 'target_kind': 'existing',
               'input_mode': 'goal', 'prompt': 'README guncelle', 'output': 'README', 'acceptance': '',
               'preference': 'balanced', 'difficulty_override': None, 'duration_minutes': 30, 'budget_usd': '1',
               'backlog_path': '', 'source_token': ''}
        record = self.store.request(raw)
        self.assertRegex(record['provenance']['engine']['guard_version'], '^[0-9a-f]{64}$')
        self.assertEqual(record['operator_interventions'], [])
        with self.assertRaises(ValueError):
            self.store.update(record['run_id'], provenance={'version': 2})
        with self.assertRaises(ValueError):
            self.store.update(record['run_id'], operator_interventions=[{'kind': 'sahte'}])

    def test_engine_outside_git_reports_unknown_not_fabricated(self):
        copy = self.tmp / 'engine-copy'
        shutil.copytree(Path(__file__).resolve().parents[1] / 'lib', copy / 'lib')
        (copy / 'bin').mkdir()
        kimlik = kosu.kaynak_kimligi(self.home, copy, komutlar=True)
        self.assertIsNone(kimlik['engine']['git_head'])
        self.assertIsNone(kimlik['engine']['git_dirty'])
        self.assertRegex(kimlik['engine']['guard_version'], '^[0-9a-f]{64}$')

    def test_operator_paths_record_interventions_without_minutes(self):
        rid = self.store.accept('p', 'kucuk is')['run_id']
        action = izin.propose(self.store, rid, 'install', [['bash', '-lc', 'true']],
                              {'path': str(self.repo), 'operation': 'project-setup'}, {'revision': 'r1'})
        izin.approve(self.store, rid, action['token'])
        kosu.approve_spending(self.store, rid)
        self.store.update(rid, state='failed', reason='x')
        self.assertTrue(kosu.devam_ac(self.store, rid))
        kosu.iptal_et(self.store, self.store.read(rid))
        kinds = [e['kind'] for e in self.store.read(rid)['operator_interventions']]
        self.assertEqual(kinds, ['approve', 'spending-approval', 'resume-failed', 'cancel'])
        ozet = kosu.mudahale_ozeti(self.store.read(rid))
        self.assertEqual((ozet['count'], ozet['minutes']), (4, None))
        self.assertLessEqual(ozet['first'], ozet['last'])
        self.assertEqual(ozet['kinds'], ['approve', 'cancel', 'resume-failed', 'spending-approval'])

    def test_old_record_without_identity_is_shown_as_unknown(self):
        rid = self.store.accept('p', 'kucuk is')['run_id']
        path = self.store.path(rid) / 'run.json'
        record = json.loads(path.read_text())
        record.pop('provenance'); record.pop('operator_interventions')
        path.write_text(json.dumps(record))
        lines = arayuz_veri.kimlik_ozeti(self.store.read(rid))
        self.assertEqual([v for _, (_, v) in lines], ['Kayıt yok (eski koşu)'] * 2)
        fresh = arayuz_veri.kimlik_ozeti(self.store.read(self.store.accept('p', 'ikinci')['run_id']))
        self.assertEqual(len(fresh), 5)
        self.assertIn('0 kayıt · dakika ölçülmedi', fresh[-1][1][1])


if __name__ == '__main__':
    unittest.main()
