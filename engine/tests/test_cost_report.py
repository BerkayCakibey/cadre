"""CR-40 (N-BU07, N-OBS02): stage/pool cost and time breakdown; the ledger is the single source, unknown is never written as zero."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import panel_data
import ui_messages
from budget_gate import BudgetLedger
from run_store import RunStore, atomic_json
import cost_report


class CostReportTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix='cost-report-'))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        (self.home / 'projects/p').mkdir(parents=True)
        (self.home / 'chain').mkdir()
        self.env = patch.dict(os.environ, {'CADRE_HOME': str(self.home)}, clear=False)
        self.env.start(); self.addCleanup(self.env.stop)
        os.environ.pop('CADRE_RUN_ID', None)
        self.store = RunStore(self.home)
        self.parent = self.store.accept('p', 'big job')['run_id']
        self.child = self.store.accept('p', 'part', parent_run_id=self.parent)['run_id']
        self.store.update(self.child, project_ref_parent=self.parent, final_result={'status': 'FAIL', 'reason': 'qa-not-passed'})
        self.store.update(self.parent, project_run_ids=[self.parent, self.child], final_result={'status': 'PASS'},
                          budget_waits=[{'ts': 100.0, 'until': 160.0}, {'ts': 200.0, 'until': 230.0}, 'broken'])
        self.ledger = BudgetLedger(self.home / 'chain/request-budget.db')
        now = time.time()
        rows = [  # (run, provider, reserved, actual, receipt, metadata)
            (self.parent, 'opencode-go', 1000, 700, 'r1', {'task_id': 'plan', 'phase_role': 'plan', 'slice': 'lanes'}),
            (self.parent, 'openrouter', 2000, 1500, 'r2', {'task_id': 'plan-review-1-ab', 'role': 'review', 'slice': 'judge'}),
            (self.parent, 'openrouter', 3000, 2500, 'r3', {'task_id': 'g1', 'phase_role': 'impl', 'slice': 'lanes'}),
            (self.parent, 'openrouter', 400, None, None, {'task_id': 'g1', 'phase_role': 'impl', 'slice': 'lanes'}),
            (self.parent, 'opencode-go', 900, 900, 'phantom:x', {'task_id': 'qa', 'phase_role': 'qa', 'slice': 'lanes'}),
            (self.parent, 'openrouter', 500, 450, 'r5', {'task_id': 'quality', 'phase_role': 'quality', 'slice': 'judge'}),
            (self.parent, 'openrouter', 600, 0, 'unused:1', {'task_id': 'main', 'assurance': True, 'assurance_role': 'quality', 'slice': 'judge'}),
            (self.child, 'openrouter', 800, 650, 'r7', {'task_id': 'correction-1', 'phase_role': 'recovery', 'slice': 'lanes'}),
            (self.child, 'groq', 1, 1, 'r8', {'task_id': 'g1', 'phase_role': 'impl'}),
        ]
        with self.ledger.transaction() as con:
            for n, (rid, provider, reserved, actual, receipt, meta) in enumerate(rows):
                con.execute('insert into reservation values(?,?,?,?,?,?,?,?)', (f'id{n}', provider, '2026-10', now - 100 + n,
                            reserved, actual, receipt, json.dumps({**meta, 'run_id': rid, 'model': 'm'})))
            con.execute('insert into reservation values(?,?,?,?,?,?,?,?)', ('bad', 'openrouter', '2026-10', now, 5, None, None, 'not json'))
        for rid, task, role, start, end in ((self.parent, 'plan', 'plan', 1000.0, 1030.0), (self.parent, 'g1', 'impl', 1100.0, 1400.0),
                                             (self.parent, 'quality', 'quality', 1500.0, 1560.0), (self.child, 'correction-1', 'recovery', 2000.0, 2090.0)):
            path = self.store.task_dir(rid, task) / 'attempts/a1/attempt.json'
            path.parent.mkdir(parents=True)
            atomic_json(path, {'run_id': rid, 'task_id': task, 'attempt_id': 'a1', 'role': role, 'created': start, 'finished': end})
        atomic_json(self.store.task_dir(self.parent, 'g2') / 'attempts/a1/attempt.json', {'task_id': 'g2', 'role': 'impl', 'created': 'absent'})
        atomic_json(self.store.path(self.parent) / 'base-qa.json', {'status': 'PASS', 'install': {'status': 'PASS', 'seconds': 12.5}, 'qa': {'status': 'PASS'}})
        record = json.loads((self.store.path(self.parent) / 'run.json').read_text())
        record['created'] = 950.0
        (self.store.path(self.parent) / 'run.json').write_text(json.dumps(record))

    def test_stages_pools_and_certainty_are_separated_from_one_ledger(self):
        data = cost_report.breakdown(self.home, self.parent)
        self.assertEqual(data['scope'], sorted([self.parent, self.child]))
        stages = data['expense']['stages']
        self.assertEqual(data['expense']['source'], 'request-ledger')
        self.assertEqual(data['expense']['broken'], 1)
        self.assertEqual(stages['plan']['equivalent'], {'settled': 700, 'estimated': 0, 'pending': 0})
        self.assertEqual(stages['plan-review']['cash'], {'settled': 1500, 'estimated': 0, 'pending': 0})
        self.assertEqual(stages['impl']['cash'], {'settled': 2500, 'estimated': 0, 'pending': 400})
        self.assertEqual(stages['impl']['unknown_unit'], 1)   # groq: neither cash nor equivalent; not summed
        self.assertEqual(stages['qa']['equivalent'], {'settled': 0, 'estimated': 900, 'pending': 0})
        self.assertEqual(stages['quality']['cash']['settled'], 450)
        self.assertEqual(stages['assurance']['cash'], {'settled': 0, 'estimated': 0, 'pending': 0})
        self.assertEqual(stages['repair']['cash']['settled'], 650)
        self.assertEqual(data['token'], 'unknown')
        self.assertEqual(data['acceptance'], {'status': 'PASS', 'acceptance_done': True})
        self.assertEqual(data['failed_child']['runs'], [self.child])
        self.assertEqual(data['failed_child']['expense']['stages']['repair']['cash']['settled'], 650)

    def test_running_child_is_not_a_failed_child_but_cancelled_one_is(self):
        running = self.store.accept('p', 'running part', parent_run_id=self.parent)['run_id']
        self.store.update(running, project_ref_parent=self.parent, state='running')
        cancelled = self.store.accept('p', 'cancel part', parent_run_id=self.parent)['run_id']
        self.store.update(cancelled, project_ref_parent=self.parent, state='cancelled')
        self.store.update(self.parent, project_run_ids=[self.parent, self.child, running, cancelled])
        runs = cost_report.breakdown(self.home, self.parent)['failed_child']['runs']
        self.assertIn(self.child, runs)
        self.assertIn(cancelled, runs)
        self.assertNotIn(running, runs, 'FIX-21: a running child is not a failure')

    def test_durations_separate_work_queue_quota_wait_and_setup(self):
        s = cost_report.breakdown(self.home, self.parent)['duration']
        self.assertEqual(s['operation_sec'], {'impl': 300.0, 'quality': 60.0, 'plan': 30.0, 'repair': 90.0})
        self.assertEqual(s['queue_sec'], 50.0)
        self.assertEqual(s['quota_wait_sec'], 90.0)
        self.assertEqual(s['install_sec'], 12.5)
        self.assertEqual(s['uncertain_attempt'], 1)

    def test_missing_ledger_is_unknown_not_zero_and_ui_lists_the_breakdown(self):
        (self.home / 'chain/request-budget.db').unlink()
        data = cost_report.breakdown(self.home, self.parent)
        self.assertEqual(data['expense']['source'], 'unknown')
        self.assertTrue(all(not v['request'] for v in data['expense']['stages'].values()))
        lines = cost_report.summary_lines(data)
        self.assertIn('queue 50.0', lines[-1])
        node = panel_data.spend_node(self.home, self.store.read(self.parent), {})
        self.assertIn(('section', 'Stage breakdown'), node['detail'])

    def test_cr52_tokens_cache_reasoning_and_endpoint_come_from_receipt_metadata_unknown_stays_unknown(self):
        with self.ledger.transaction() as con:
            for ident, usage in (('id2', {'input': 1000, 'output': 200, 'thinking': 50, 'cache_reading': 800, 'cache_write': 0,
                                          'endpoint': 'StreamLake', 'fee_usd': 0.0025}),
                                 ('id7', {'input': 300, 'output': 100, 'endpoint': 'DeepInfra'})):
                con.execute("update reservation set metadata=json_set(metadata,'$.usage',json(?)) where id=?", (json.dumps(usage), ident))
            con.execute("update reservation set metadata=json_set(metadata,'$.prefix',json(?)) where id='id2'",
                        (json.dumps({'constant': 'a' * 16, 'proceed': True, 'message': 4}),))
            con.execute("update reservation set metadata=json_set(metadata,'$.prefix',json(?)) where id='id3'",
                        (json.dumps({'constant': 'a' * 16, 'proceed': False, 'message': 2}),))
        data = cost_report.breakdown(self.home, self.parent)
        token = data['token']
        self.assertEqual((token['measured_request'], token['unknown_request']), (2, 6), 'An assurance row is not a request')
        self.assertEqual((token['input'], token['output'], token['cache_reading'], token['cache_write']), (1300, 300, 800, 0))
        self.assertEqual((token['thinking'], token['thinking_unknown']), (50, 1), 'Thinking is included in output; missing values are unknown')
        self.assertEqual(token['cache_unknown'], 1)
        self.assertEqual(token['endpoints'], {'StreamLake': 1, 'DeepInfra': 1, 'unknown': 6})
        self.assertEqual(token['prefix'], {'proceed': 1, 'broken': 1, 'first_or_unknown': 6})
        stages = data['expense']['stages']
        self.assertEqual(stages['impl']['token']['cache_reading'], 800)
        self.assertEqual(stages['repair']['token']['measured_request'], 1)
        self.assertEqual(stages['plan']['token']['measured_request'], 0)
        self.assertEqual(stages['impl']['cash'], {'settled': 2500, 'estimated': 0, 'pending': 400}, 'Usage does not change the cash total')
        self.assertIn('separate', data['service_fee'])
        lines = '\n'.join(cost_report.summary_lines(data))
        self.assertIn('2/8', lines)
        self.assertIn('800', lines)

    def test_cr52_helper_calls_are_visible_and_counted_in_the_same_total(self):
        with self.ledger.transaction() as con:
            for n, role in enumerate(('title', 'summary', 'compaction')):
                con.execute('insert into reservation values(?,?,?,?,?,?,?,?)', (f'y{n}', 'openrouter', '2026-10', time.time(), 50, 40,
                            f'ry{n}', json.dumps({'run_id': self.parent, 'role': role, 'task_id': 'g1', 'model': 'm'})))
        data = cost_report.breakdown(self.home, self.parent)
        self.assertEqual(data['helper_calls'], {'compaction': 1, 'summary': 1, 'title': 1})
        self.assertEqual(data['expense']['stages']['helper']['cash']['settled'], 120)

    def test_cr52_campaign_cost_per_accepted_job_includes_failures_and_is_undefined_without_acceptance(self):
        other = self.store.accept('p', 'second job')['run_id']
        self.store.update(other, final_result={'status': 'FAIL', 'reason': 'qa-not-passed'})
        with self.ledger.transaction() as con:
            con.execute('insert into reservation values(?,?,?,?,?,?,?,?)', ('o1', 'openrouter', '2026-10', time.time(), 900, 700, 'ro1',
                        json.dumps({'run_id': other, 'task_id': 'g1', 'phase_role': 'impl', 'model': 'm'})))
        data = cost_report.campaign(self.home, [self.parent, other, self.child])
        self.assertEqual(data['jobs'], sorted([self.parent, other]))
        self.assertEqual(data['acceptance_done'], 1)
        self.assertEqual(data['total']['cash']['settled'], 1500 + 2500 + 450 + 650 + 700, 'The total includes failed jobs and child costs')
        self.assertEqual(data['total']['equivalent'], {'settled': 700, 'estimated': 900, 'pending': 0})
        self.assertEqual(data['acceptance_per']['cash_settled_usd'], round((1500 + 2500 + 450 + 650 + 700) / 1e6, 6))
        self.assertEqual(data['total']['cash']['pending'], 400)
        self.store.update(self.parent, final_result={'status': 'FAIL', 'reason': 'x'})
        none = cost_report.campaign(self.home, [self.parent, other])
        self.assertEqual((none['acceptance_done'], none['acceptance_per']), (0, 'undefined'))
        import io
        with patch.object(sys, 'stdout', io.StringIO()) as out:
            self.assertEqual(cost_report.main(['--campaign', self.parent, other]), 0)
        self.assertEqual(json.loads(out.getvalue())['acceptance_per'], 'undefined')
        with patch.object(sys, 'stderr', io.StringIO()):
            self.assertEqual(cost_report.main(['--campaign']), 2)
            self.assertEqual(cost_report.main(['--campaign', '../x']), 1)

    def test_tsr03_missing_or_unreadable_ledger_never_reports_a_zero_campaign_cost(self):
        for broken in (None, b'Not SQLite', b''):
            path = self.home / 'chain/request-budget.db'
            path.unlink(missing_ok=True)
            if broken is not None:
                path.write_bytes(broken)
            data = cost_report.campaign(self.home, [self.parent])
            self.assertEqual(data['source'], 'unknown', broken)
            self.assertEqual(data['acceptance_done'], 1)
            self.assertEqual(data['acceptance_per'], 'unknown', 'Missing data is not a zero-dollar ratio')
            self.assertEqual(data['total'], 'unknown', 'An unknown total is not reported as zero')
            self.assertEqual(data['expense_scope']['status'], 'unknown')
            self.assertNotIn('0.0', json.dumps(data['acceptance_per']))
        self.store.update(self.parent, final_result={'status': 'FAIL', 'reason': 'x'})
        self.assertEqual(cost_report.campaign(self.home, [self.parent])['acceptance_per'], 'undefined', 'without an acceptance the ratio stays undefined')

    def test_tsr03_real_zero_stays_zero_and_partial_ledger_coverage_is_explicit(self):
        clean = Path(tempfile.mkdtemp(prefix='cost-report-zero-'))
        self.addCleanup(shutil.rmtree, clean, ignore_errors=True)
        (clean / 'projects/p').mkdir(parents=True)
        store = RunStore(clean)
        rid = store.accept('p', 'zero-cost acceptance')['run_id']
        store.update(rid, final_result={'status': 'PASS'})
        ledger = BudgetLedger(clean / 'chain/request-budget.db')
        with ledger.transaction() as con:
            con.execute('insert into reservation values(?,?,?,?,?,?,?,?)', ('z1', 'openrouter', '2026-10', time.time(), 300, 0,
                        'gen-z1:http-429', json.dumps({'run_id': rid, 'task_id': 'g1', 'phase_role': 'impl', 'model': 'm'})))
        zero = cost_report.campaign(clean, [rid])
        self.assertEqual(zero['source'], 'request-ledger')
        self.assertEqual(zero['expense_scope'], {'status': 'complete', 'broken_line': 0, 'unknown_unit': 0})
        self.assertEqual(zero['acceptance_per']['cash_settled_usd'], 0.0, 'A verified actual zero is preserved')
        self.assertEqual(zero['acceptance_per']['scope'], 'complete')
        partial = cost_report.campaign(self.home, [self.parent, self.child])
        self.assertEqual(partial['expense_scope'], {'status': 'partial', 'broken_line': 1, 'unknown_unit': 1})
        self.assertEqual(partial['acceptance_per']['scope'], 'partial', 'With a broken row or unknown unit, the ratio is a lower bound')
        self.assertEqual(partial['total']['cash']['pending'], 400, 'Pending amounts stay separate')
        self.assertEqual(partial['acceptance_per']['pending_separate_usd'], round(400 / 1e6, 6))
        self.assertEqual(partial['total']['cash']['settled'], 1500 + 2500 + 450 + 650, 'Count child costs once')

    def test_tsr04_missing_cache_write_and_reasoning_fields_are_shown_unknown_or_partial_never_zero(self):
        with self.ledger.transaction() as con:
            con.execute("update reservation set metadata=json_set(metadata,'$.usage',json(?)) where id='id2'",
                        (json.dumps({'input': 10, 'output': 2}),))
        only = cost_report.breakdown(self.home, self.parent)
        self.assertEqual((only['token']['cache_unknown'], only['token']['cache_write_unknown'],
                          only['token']['thinking_unknown']), (1, 1, 1))
        text = '\n'.join(cost_report.summary_lines(only))
        self.assertIn('cache read unknown', text)
        self.assertIn('write unknown', text)
        self.assertIn('reasoning unknown', text)
        self.assertNotIn('cache read 0', text)
        with self.ledger.transaction() as con:
            con.execute("update reservation set metadata=json_set(metadata,'$.usage',json(?)) where id='id1'",
                        (json.dumps({'input': 1000, 'output': 200, 'thinking': 0, 'cache_reading': 800, 'cache_write': 0,
                                     'fee_usd': 0.0015}),))
        partial = cost_report.breakdown(self.home, self.parent)
        text = '\n'.join(cost_report.summary_lines(partial))
        self.assertIn('cache read 800 [in 1/2 requests]', text)
        self.assertIn('write 0 [in 1/2 requests]', text)
        self.assertIn('reasoning 0 [in 1/2 requests]', text)
        self.assertEqual(partial['expense']['stages']['plan-review']['cash'], {'settled': 1500, 'estimated': 0, 'pending': 0},
                         'Receipt cost and token breakdown do not count the ledger amount twice')
        with self.ledger.transaction() as con:
            con.execute("update reservation set metadata=json_set(metadata,'$.usage',json(?)) where id='id2'",
                        (json.dumps({'input': 10, 'output': 2, 'thinking': 0, 'cache_reading': 0, 'cache_write': 0}),))
        full = '\n'.join(cost_report.summary_lines(cost_report.breakdown(self.home, self.parent)))
        self.assertIn('cache read 800, write 0', full, 'an explicit zero on the receipt is a measured zero')
        self.assertIn('reasoning 0)', full)
        english = '\n'.join(cost_report.summary_lines(partial))
        self.assertIn('cache read 800 [in 1/2 requests]', english)

    def test_tsr04_requests_without_any_measured_usage_show_tokens_unknown(self):
        data = cost_report.breakdown(self.home, self.parent)
        self.assertEqual(data['token'], 'unknown')
        self.assertIn('tokens: unknown', cost_report.summary_lines(data))

    def test_cli_prints_json_and_rejects_bad_identity(self):
        import io
        with patch.object(sys, 'stdout', io.StringIO()) as out:
            self.assertEqual(cost_report.main([self.parent]), 0)
        self.assertEqual(json.loads(out.getvalue())['run_id'], self.parent)
        with patch.object(sys, 'stderr', io.StringIO()):
            self.assertEqual(cost_report.main(['../x']), 1)
            self.assertEqual(cost_report.main([]), 2)


if __name__ == '__main__':
    unittest.main()
