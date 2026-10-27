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
import panel_data
import permission
import run_store
from run_store import RunStore


class IdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='id-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / 'home_dir'
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
        record = self.store.accept('p', 'small job')
        id = record['provenance']
        self.assertEqual(id['version'], 1)
        self.assertRegex(id['engine']['guard_version'], '^[0-9a-f]{64}$')
        self.assertEqual(Path(id['engine']['root']), Path(__file__).resolve().parents[1])
        self.assertIsNone(id['engine']['git_head'])   # no subprocess at record time
        self.assertIsNone(id['completed'])
        self.assertIsInstance(id['runtime']['platform'], str)
        self.assertRegex(id['runtime']['python'], r'^\d+\.\d+')
        self.assertIsNone(id['runtime']['git_version'])
        complete = self.store.id_complete(record['run_id'])
        self.assertIsNotNone(complete['completed'])
        self.assertIsInstance(complete['runtime']['git_version'], str)
        self.assertIn(complete['engine']['git_dirty'], (True, False, None))
        self.assertEqual(self.store.id_complete(record['run_id']), complete)   # a second call does not change it
        self.assertEqual(complete['engine']['guard_version'], id['engine']['guard_version'])
        self.assertEqual(record['operator_interventions'], [])
        self.assertEqual(id['config']['opencode_jsonc_sha256'], None)   # no cfg directory: nothing is invented
        self.assertRegex(id['config']['limits_sha256'], '^[0-9a-f]{64}$')
        saved = self.store.read(record['run_id'])
        self.assertEqual(saved['provenance'], complete)

    def test_request_record_carries_identity_and_identity_is_immutable(self):
        raw = {'submission_id': 'abc123', 'project': 'p', 'target_path': str(self.repo), 'target_kind': 'existing',
               'input_mode': 'goal', 'prompt': 'Update README', 'output': 'README', 'acceptance': '',
               'preference': 'balanced', 'difficulty_override': None, 'duration_minutes': 30, 'budget_usd': '1',
               'backlog_path': '', 'source_token': ''}
        record = self.store.request(raw)
        self.assertRegex(record['provenance']['engine']['guard_version'], '^[0-9a-f]{64}$')
        self.assertEqual(record['operator_interventions'], [])
        with self.assertRaises(ValueError):
            self.store.update(record['run_id'], provenance={'version': 2})
        with self.assertRaises(ValueError):
            self.store.update(record['run_id'], operator_interventions=[{'kind': 'fake'}])

    def test_engine_outside_git_reports_unknown_not_fabricated(self):
        copy = self.tmp / 'engine-copy'
        shutil.copytree(Path(__file__).resolve().parents[1] / 'lib', copy / 'lib')
        (copy / 'bin').mkdir()
        id = run_store.source_id(self.home, copy, commands=True)
        self.assertIsNone(id['engine']['git_head'])
        self.assertIsNone(id['engine']['git_dirty'])
        self.assertRegex(id['engine']['guard_version'], '^[0-9a-f]{64}$')

    def test_operator_paths_record_interventions_without_minutes(self):
        rid = self.store.accept('p', 'small job')['run_id']
        action = permission.propose(self.store, rid, 'install', [['bash', '-lc', 'true']],
                              {'path': str(self.repo), 'operation': 'project-setup'}, {'revision': 'r1'})
        permission.approve(self.store, rid, action['token'])
        run_store.approve_spending(self.store, rid)
        self.store.update(rid, state='failed', reason='x')
        self.assertTrue(run_store.proceed_open(self.store, rid))
        run_store.cancel_run(self.store, self.store.read(rid))
        kinds = [e['kind'] for e in self.store.read(rid)['operator_interventions']]
        self.assertEqual(kinds, ['approve', 'spending-approval', 'resume-failed', 'cancel'])
        summary = run_store.intervention_summary(self.store.read(rid))
        self.assertEqual((summary['count'], summary['minutes']), (4, None))
        self.assertLessEqual(summary['first'], summary['last'])
        self.assertEqual(summary['kinds'], ['approve', 'cancel', 'resume-failed', 'spending-approval'])

    def test_old_record_without_identity_is_shown_as_unknown(self):
        rid = self.store.accept('p', 'small job')['run_id']
        path = self.store.path(rid) / 'run.json'
        record = json.loads(path.read_text())
        record.pop('provenance'); record.pop('operator_interventions')
        path.write_text(json.dumps(record))
        lines = panel_data.id_summary(self.store.read(rid))
        self.assertEqual([v for _, (_, v) in lines], ['Not recorded (older run)'] * 2)
        fresh = panel_data.id_summary(self.store.read(self.store.accept('p', 'second')['run_id']))
        self.assertEqual(len(fresh), 5)
        self.assertIn('0 recorded · minutes not measured', fresh[-1][1][1])


if __name__ == '__main__':
    unittest.main()
