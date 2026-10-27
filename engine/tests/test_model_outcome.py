"""CR-39 (N-RT01, N-RT02): outcome measurement after a real acceptance decision; measurement does not change the selection; a repeat is one sample."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import qe_core as core
import model_selection
from model_selection import policy_order, outcome_save, outcome_summary
from test_outcome import CompletionTests


class OutcomeMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix='outcome-'))
        (self.home / 'chain').mkdir()
        self.record = {'run_id': 'r1', 'created': time.time() - 90, 'source_sha': 'abc',
                       'model_selection': {'policy_version': 'difficulty-preference-role-v1', 'preference': 'balanced',
                                           'difficulty_estimate': 'medium', 'difficulty_override': None, 'difficulty_effective': 'medium',
                                           'estimate_score': 30, 'estimate_source': 'request-rules', 'estimate_reason': 'rule_base=30',
                                           'prompt_sha256': '0' * 64}}

    def test_same_run_product_and_verdict_is_one_sample_and_cost_is_unknown_without_ledger(self):
        result = {'status': 'PASS', 'revision': {'digest': 'd1'}}
        first = outcome_save(self.home, self.record, result, ['opencode-go/glm-5.3', 'opencode-go/glm-5.3'], 'opencode/glm-5.3', 'claude/opus')
        self.assertIsNotNone(first)
        self.assertIsNone(outcome_save(self.home, self.record, result, ['opencode-go/glm-5.3'], 'opencode/glm-5.3', None))
        self.assertIsNotNone(outcome_save(self.home, self.record, {'status': 'FAIL', 'revision': {'digest': 'd1'}, 'reason': 'qa-not-passed'}, [], None, None))
        rows = [json.loads(l) for l in (self.home / 'chain/model-outcomes.jsonl').read_text().splitlines()]
        self.assertEqual([r['outcome'] for r in rows], ['PASS', 'FAIL'])
        self.assertEqual([(x['role'], x['path']) for x in rows[0]['roles']],
                         [('impl', 'opencode-go/glm-5.3'), ('quality', 'opencode/glm-5.3'), ('plan', 'claude/opus')])
        self.assertEqual(rows[0]['expense'], {'source': 'unknown', 'settled_micro': None, 'pending_micro': None})
        self.assertEqual((rows[0]['policy'], rows[0]['preference'], rows[0]['difficulty_enabled']), ('difficulty-preference-role-v1', 'balanced', 'medium'))
        self.assertGreater(rows[0]['age_sec'], 60)
        self.assertNotIn('duration_sec', rows[0], 'FIX-22: the field is the run age, including queue and quota waits, not role execution time')
        self.assertIsNone(outcome_save(self.home, self.record, {'status': 'WAITING'}, [], None, None))

    def test_measurements_never_reorder_selection(self):
        candidates = ['opencode-go/deepseek-v4-pro', 'opencode-go/glm-5.3', 'openrouter/z-ai/glm-5.3']
        before = policy_order(candidates, 'impl', self.record['model_selection'])
        for n in range(6):
            outcome_save(self.home, {**self.record, 'run_id': f'r{n}'}, {'status': 'FAIL', 'revision': {'digest': 'x'}}, ['opencode-go/deepseek-v4-pro'], None, None)
            outcome_save(self.home, {**self.record, 'run_id': f'ok{n}'}, {'status': 'PASS', 'revision': {'digest': 'y'}}, ['opencode-go/glm-5.3'], None, None)
        self.assertEqual(policy_order(candidates, 'impl', self.record['model_selection']), before)
        ranked, _ = model_selection.sort(candidates, candidates, 'impl', 8000, self.home, time.time())
        self.assertEqual(ranked[:1], ['opencode-go/deepseek-v4-pro'])

    def test_cr50_outcomes_keep_the_effort_policy_separate_for_comparison(self):
        now = time.time()
        economic = {**self.record, 'run_id': 'r-eko', 'tasks': {'g1': {'effort_policy': {'policy': 'economic-effort-v1', 'level': 'low'}}}}
        entry = outcome_save(self.home, economic, {'status': 'PASS', 'revision': {'digest': 'e'}}, ['opencode-go/glm-5.3'], None, None, now_ts=now)
        self.assertEqual(entry['effort'], ['economic-effort-v1'])
        plain = outcome_save(self.home, {**self.record, 'run_id': 'r-std'}, {'status': 'PASS', 'revision': {'digest': 'f'}},
                             ['opencode-go/glm-5.3'], None, None, now_ts=now)
        self.assertIsNone(plain['effort'])
        groups = outcome_summary(self.home, now_ts=now)['groups']
        self.assertEqual(sorted(str(g['effort']) for g in groups if g['role'] == 'impl'), ['None', 'economic-effort-v1'])

    def test_summary_separates_fresh_stale_few_failed_and_unknown(self):
        now = time.time()
        outcome_save(self.home, self.record, {'status': 'PASS', 'revision': {'digest': 'a'}}, ['opencode-go/glm-5.3'], None, None, now_ts=now - 40 * 86400)
        outcome_save(self.home, {**self.record, 'run_id': 'r2', 'created': None}, {'status': 'FAIL', 'revision': {'digest': 'b'}}, ['opencode-go/glm-5.3'], None, None, now_ts=now)
        with (self.home / 'chain/model-outcomes.jsonl').open('a') as broken:
            broken.write('broken line\n')
        summary = outcome_summary(self.home, now_ts=now)
        self.assertEqual((summary['total'], summary['broken']), (2, 1))
        group = summary['groups'][0]
        self.assertEqual((group['sample'], group['fresh'], group['old'], group['acceptance'], group['failed']), (2, 1, 1, 1, 1))
        self.assertEqual(group['uncertainty'], ['least-sample', 'old-sample', 'expense-unknown', 'age-unknown'])
        self.assertEqual(outcome_summary(Path(tempfile.mkdtemp()))['groups'], [])


class ProductionPathTests(unittest.TestCase):
    def setUp(self):
        self.f = CompletionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_final_acceptance_decision_writes_one_sample_per_verdict_from_the_real_path(self):
        self.f.successful_qa()
        with patch.object(core, 'capture_opencode', return_value=self.f.review('REVISE')):
            result = core.finalize_run('a', str(self.f.wt), self.f.log, '/fake/cli', review_model='')
        self.assertEqual(result['status'], 'FAIL')
        path = self.f.home / 'chain/model-outcomes.jsonl'
        rows = [json.loads(l) for l in path.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['run_id'], rows[0]['outcome'], rows[0]['product']), (self.f.rid, 'FAIL', result['revision']['digest']))
        self.assertIn(('impl', 'writer'), [(x['role'], x['model']) for x in rows[0]['roles']])
        with patch.object(core, 'capture_opencode', return_value=self.f.review('REVISE')):
            core.finalize_run('a', str(self.f.wt), self.f.log, '/fake/cli', review_model='')
        self.assertEqual(len(path.read_text().splitlines()), 1)   # a restart does not count the same result as a second sample


if __name__ == '__main__':
    unittest.main()
