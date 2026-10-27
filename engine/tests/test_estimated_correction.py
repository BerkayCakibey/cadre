"""N-BU03 (RCA code28/29): a ghost/recovery estimate close is corrected only on the related line with a real receipt verified later;
the previous amount and reason leave a trace, a repeat/concurrent operation does not produce a double correction, a contradictory receipt changes nothing."""

from __future__ import annotations

from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))

GEN = 'gen-fixture-0001'


class EstimatedCorrectionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='estimated-fix-')
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.enterContext(patch.dict(os.environ, {'CADRE_HOME': str(self.home)}))
        import budget_gate as bg
        from monthly_budget import MonthlyLedger
        from run_store import RunStore
        self.bg = bg
        self.rid = RunStore(self.home).accept('p', 'code')['run_id']
        import datetime
        now_ts = datetime.datetime.now()
        b = {'ts': now_ts.isoformat(), 'today': now_ts.date().isoformat(), 'or': {'db': 'ok', 'pay': 50.0, 'available': 50.0, 'remaining': 50.0}}
        self.ledger = bg.BudgetLedger(self.home / 'chain/request-budget.db')
        self.monthly_path = self.home / 'chain/monthly-budget.db'
        self.monthly = MonthlyLedger(self.monthly_path, cap=10_000_000)
        self.monthly_id = self.monthly.reserve('openrouter', 229_027, {'model': 'openai/gpt-6.1-sol'})
        self.ident = self.ledger.reserve('openrouter', 229_027, b, {'run_id': self.rid, 'model': 'openai/gpt-6.1-sol', 'monthly_id': self.monthly_id})
        self.ledger.production_save(self.ident, GEN)
        self.ledger.settle(self.ident, 229_027, 'phantom:' + self.ident)
        self.monthly.settle(self.monthly_id, 229_027, 'recovery:' + self.monthly_id)
        self.measurement = self.ledger.reserve('openrouter', 10_000, b, {'run_id': self.rid, 'model': 'openai/gpt-6.1-sol'})
        self.ledger.production_save(self.measurement, 'gen-measurement')
        self.ledger.settle(self.measurement, 2_000, self.measurement + ':gen-measurement')

    def line(self, db, ident):
        with closing(sqlite3.connect(db)) as con:
            r = con.execute('select actual, receipt, metadata from reservation where id=?', (ident,)).fetchone()
        return r[0], r[1], json.loads(r[2])

    def test_late_invoice_corrects_only_the_relevant_line_leaving_a_trace_and_together_with_the_monthly_equivalent(self):
        import project_mode
        once = project_mode.project_ref_cost(self.home, [self.rid])
        self.assertEqual((once['measured_usd'], once['estimated_usd']), (0.002, 0.229027))
        outcome = self.bg.estimated_closing_fix(self.home, self.ident, GEN, 7_048, 'OpenRouter generation invoice')
        self.assertEqual(outcome['status'], 'fixed')
        actual, receipt, meta = self.line(self.home / 'chain/request-budget.db', self.ident)
        self.assertEqual((actual, receipt), (7_048, f'{self.ident}:{GEN}'))
        self.assertEqual(meta['estimated_correction']['previous_actual'], 229_027)
        self.assertEqual(meta['estimated_correction']['previous_receipt'], 'phantom:' + self.ident)
        self.assertEqual(meta['estimated_correction']['rationale'], 'OpenRouter generation invoice')
        self.assertEqual(self.line(self.monthly_path, self.monthly_id)[:2], (7_048, f'{self.monthly_id}:{GEN}'))
        self.assertEqual(self.line(self.home / 'chain/request-budget.db', self.measurement)[:2], (2_000, self.measurement + ':gen-measurement'))
        then = project_mode.project_ref_cost(self.home, [self.rid])
        self.assertEqual((then['measured_usd'], then['estimated_usd'], then['pending_usd']), (0.009048, 0.0, 0.0))
        log = (self.home / 'chain/recovery.jsonl')
        if log.exists():
            self.assertIn('estimated-fix', log.read_text())

    def test_repeated_and_concurrent_correction_does_not_produce_a_double_change(self):
        outcomes = []
        barrier = threading.Barrier(4)

        def work():
            barrier.wait()
            outcomes.append(self.bg.estimated_closing_fix(self.home, self.ident, GEN, 7_048, 'invoice')['status'])

        threads = [threading.Thread(target=work) for _ in range(4)]
        for i in threads:
            i.start()
        for i in threads:
            i.join()
        self.assertEqual(sorted(outcomes), ['fixed', 'previously-fixed', 'previously-fixed', 'previously-fixed'])
        self.assertEqual(self.bg.estimated_closing_fix(self.home, self.ident, GEN, 7_048, 'invoice')['status'], 'previously-fixed')
        _, _, meta = self.line(self.home / 'chain/request-budget.db', self.ident)
        self.assertEqual(meta['estimated_correction']['previous_actual'], 229_027, 'Keep the original estimated amount in the trace')
        self.assertEqual(self.line(self.monthly_path, self.monthly_id)[0], 7_048)

    def test_contradicting_or_unverifiable_receipt_changes_nothing(self):
        db = self.home / 'chain/request-budget.db'
        once = (self.line(db, self.ident), self.line(db, self.measurement), self.line(self.monthly_path, self.monthly_id))
        statuses = [
            self.bg.estimated_closing_fix(self.home, self.ident, 'gen-other-production', 7_048, 'wrong id')['status'],
            self.bg.estimated_closing_fix(self.home, self.ident, GEN, 229_028, 'upper bound overrun')['status'],
            self.bg.estimated_closing_fix(self.home, self.measurement, 'gen-measurement', 1_000, 'measured row')['status'],
            self.bg.estimated_closing_fix(self.home, 'absent', GEN, 1, 'line absent')['status'],
            self.bg.estimated_closing_fix(self.home, self.ident, 'invalid id', 1, 'format')['status'],
        ]
        self.assertEqual(statuses, ['id-mismatch', 'parent-limit-exceeds', 'estimated-not', 'absent', 'invalid'])
        self.assertEqual((self.line(db, self.ident), self.line(db, self.measurement), self.line(self.monthly_path, self.monthly_id)), once)

    def test_request_ledger_is_unchanged_if_monthly_equivalent_contradicts(self):
        with closing(sqlite3.connect(self.monthly_path)) as con, con:
            con.execute('update reservation set receipt=? where id=?', ('other-receipt', self.monthly_id))
        db = self.home / 'chain/request-budget.db'
        once = self.line(db, self.ident)
        self.assertEqual(self.bg.estimated_closing_fix(self.home, self.ident, GEN, 7_048, 'invoice')['status'], 'monthly-pair-contradicts')
        self.assertEqual(self.line(db, self.ident), once)

    def test_guard_reconciliation_does_not_change_a_phantom_line_by_itself(self):
        import provider_budget
        response = io.BytesIO(json.dumps({'data': {'total_cost': 0.007048}}).encode())
        db = self.home / 'chain/request-budget.db'
        once = self.line(db, self.ident)
        with patch.object(provider_budget, 'QE', self.home), patch.object(provider_budget, 'net_open', return_value=True), \
             patch.object(provider_budget, 'or_key', return_value='test'), \
             patch.object(provider_budget.urllib.request, 'urlopen', return_value=response) as call:
            self.assertEqual(provider_budget.or_pending_close(), 0)
        call.assert_not_called()
        self.assertEqual(self.line(db, self.ident), once)


if __name__ == '__main__':
    unittest.main()
