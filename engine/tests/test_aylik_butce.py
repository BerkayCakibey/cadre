import sys
from pathlib import Path
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'lib'))
from aylik_butce import MonthlyBudgetDenied, MonthlyLedger


class MonthlyLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = MonthlyLedger(Path(self.tmp.name) / 'monthly.db', cap=75_000_000)

    def test_shared_cap_spans_providers(self):
        self.ledger.reserve('opencode', 40_000_000, current='2026-10')
        self.ledger.reserve('openrouter', 35_000_000, current='2026-10')
        with self.assertRaisesRegex(MonthlyBudgetDenied, 'monthly paid budget exhausted'):
            self.ledger.reserve('opencode', 1, current='2026-10')

    def test_settlement_releases_unused_reservation(self):
        first = self.ledger.reserve('opencode', 40_000_000, current='2026-10')
        self.ledger.settle(first, 10_000_000, 'receipt-a')
        self.ledger.reserve('openrouter', 65_000_000, current='2026-10')
        state = self.ledger.status(current='2026-10')
        self.assertEqual(state['settled_micro'], 10_000_000)
        self.assertEqual(state['held_micro'], 65_000_000)
        self.assertEqual(state['remaining_micro'], 0)

    def test_month_rollover_starts_new_pool(self):
        first = self.ledger.reserve('opencode', 75_000_000, current='2026-10')
        self.ledger.settle(first, 75_000_000, 'october-receipt')
        self.ledger.reserve('openrouter', 75_000_000, current='2026-11')
        self.assertEqual(self.ledger.status(current='2026-11')['remaining_micro'], 0)

    def test_unknown_old_month_liability_survives_restart_race_and_late_receipt(self):
        from concurrent.futures import ThreadPoolExecutor
        first = self.ledger.reserve('opencode', 74_000_000, current='2026-10')
        restarted = MonthlyLedger(self.ledger.path, cap=self.ledger.cap)
        self.assertEqual(restarted.status(current='2026-11')['held_micro'], 74_000_000)
        def reserve(_):
            try:
                return restarted.reserve('openrouter', 1_000_000, current='2026-11')
            except MonthlyBudgetDenied:
                return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            admitted = [ident for ident in pool.map(reserve, range(4)) if ident]
        self.assertEqual(len(admitted), 1)
        restarted.settle(first, 70_000_000, 'late-october-receipt')
        restarted.settle(first, 70_000_000, 'late-october-receipt')
        self.assertEqual(restarted.status(current='2026-10')['settled_micro'], 70_000_000)
        self.assertEqual(restarted.status(current='2026-11')['held_micro'], 1_000_000)
        self.assertEqual(restarted.status(current='2026-11')['settled_micro'], 0)
        restarted.reserve('opencode', 74_000_000, current='2026-11')

    def test_non_pool_provider_is_rejected(self):
        with self.assertRaisesRegex(MonthlyBudgetDenied, 'provider not in monthly paid pool'):
            self.ledger.reserve('openai', 1, current='2026-10')

    def test_actual_overrun_blocks_following_work(self):
        first = self.ledger.reserve('opencode', 10_000_000, current='2026-10')
        self.ledger.settle(first, 80_000_000, 'receipt-overrun')
        with self.assertRaises(MonthlyBudgetDenied):
            self.ledger.reserve('openrouter', 1, current='2026-10')


if __name__ == '__main__':
    unittest.main()
