"""Limit and robustness tests (lesson from the 29.09 external audit, B269).

Rule: if the code has a cap/limit there is an N and N+1 test here; broken or huge input does not crash; expensive paths are limited
by time measurement. No model/network. Caps covered: report QA (unevidenced numbers 20, unknown ids 20, line range 200 (inclusive), BIG_FILE,
command output 1 MB), the budget-return wait threshold (7200 s), the Go period reset moment, share formula limits,
hand-off caps (lane 2 / run 5), pocket minimum."""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

TESTS = Path(__file__).resolve().parent
LIB = TESTS.parent / 'lib'
for _p in (str(LIB), str(TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import provider_budget  # noqa: E402
import qe_core as core  # noqa: E402
import report_mode  # noqa: E402
import model_router as router  # noqa: E402
from test_report_mode import VALID_EVIDENCE, VALID_REPORT, K, REPORT_PROMPT, repo, git, evidence_write  # noqa: E402


class ReportQaLimitTest(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix='limit-report-'))
        self.addCleanup(shutil.rmtree, self.t, True)
        self.wt, self.sha = repo(self.t)
        self.declaration = report_mode.declaration(REPORT_PROMPT)

    def qa(self):
        return report_mode.report_qa(str(self.wt), self.declaration, self.sha, str(self.t / 'temporary'), seed='constant')

    def add_and_make_commit(self, entry_name, content):
        (self.wt / entry_name).write_text(content)
        git(self.wt, 'add', entry_name)
        git(self.wt, 'commit', '-qm', entry_name)
        self.sha = git(self.wt, 'rev-parse', 'HEAD').strip()

    def unevidenced(self, n):
        sentences = "\n\n".join(f"Today {100 + i} listings were rejected." for i in range(n))
        evidence_write(self.wt, VALID_REPORT + "\n" + sentences + "\n", VALID_EVIDENCE)
        return [b for b in self.qa()['findings'] if b.startswith('unsupported-number')]

    def test_unevidenced_number_has_no_summary_at_20_lines_and_a_total_line_at_21(self):
        twenty = self.unevidenced(20)
        self.assertEqual(len(twenty), 20, twenty[-2:])
        self.assertFalse(any('listed' in b for b in twenty))
        twentyone = self.unevidenced(21)
        self.assertEqual(len(twentyone), 21, twentyone[-2:])
        self.assertIn('total 21 lines (first 20 listed)', twentyone[-1])

    def test_first_20_are_listed_for_an_unknown_id(self):
        refs = " ".join(f"[K{i}]" for i in range(30, 55))   # 25 unknown ids
        evidence_write(self.wt, VALID_REPORT + f"Sources {refs}.\n", VALID_EVIDENCE)
        finding = next(b for b in self.qa()['findings'] if b.startswith('unknown-evidence'))
        self.assertEqual(len(re.findall(r'\bK\d+\b', finding)), 20, finding)

    def test_line_range_200_lines_passes_201_and_a_reversed_or_zero_range_is_red(self):
        """D18: the cap is an inclusive line count (1-200 = 200 lines); 201 lines used to pass because the difference was 200."""
        self.add_and_make_commit('long.txt', "".join(f"line {i}\n" for i in range(1, 301)))
        evidence_write(self.wt, VALID_REPORT + "Long [K3][K4][K5][K6].\n", VALID_EVIDENCE + [
            K(3, 'file', 'long.txt:1-200', 'line 1'), K(4, 'file', 'long.txt:1-201', 'line 1'),
            K(5, 'file', 'long.txt:5-4', 'line'), K(6, 'file', 'long.txt:0-1', 'line')])
        outcome = self.qa()
        bad = sorted(b.split(':', 2)[1].strip() for b in outcome['findings'] if b.startswith('file-source'))
        self.assertEqual(bad, ['K4', 'K5', 'K6'], outcome['findings'])
        self.assertTrue(all('invalid line range' in b for b in outcome['findings'] if b.startswith('file-source')), outcome['findings'])

    def test_big_file_cap_passes_at_the_exact_limit_and_asks_for_a_line_one_byte_over(self):
        self.add_and_make_commit('full.txt', 'x' * 996 + 'END\n')          # 1000 bytes
        self.add_and_make_commit('big.txt', 'x' * 997 + 'END\n')        # 1001 bytes
        evidence_write(self.wt, VALID_REPORT + "Files [K3][K4][K5].\n", VALID_EVIDENCE + [
            K(3, 'file', 'full.txt', 'END'), K(4, 'file', 'big.txt', 'END'), K(5, 'file', 'big.txt:1', 'END')])
        with patch.object(report_mode, 'BIG_FILE', 1000):
            outcome = self.qa()
        bad = [b for b in outcome['findings'] if b.startswith('file-source')]
        self.assertEqual(len(bad), 1, outcome['findings'])
        self.assertTrue(bad[0].startswith('file-source: K4:') and 'too large' in bad[0], bad)

    def test_command_output_1mb_cap_is_documented(self):
        """An evidence command's output is clipped at 1 MB; if the expected text is beyond this limit the claim counts as unproven."""
        external_path = "python3 -c \"import sys; sys.stdout.write('x' * 1_100_000); print(); print('OUTCOME-OK')\""
        inside = "python3 -c \"print('OUTCOME-OK'); import sys; sys.stdout.write('x' * 1_100_000)\""
        evidence_write(self.wt, VALID_REPORT + "Output [K3][K4].\n", VALID_EVIDENCE + [K(3, 'command', external_path, 'OUTCOME-OK'), K(4, 'command', inside, 'OUTCOME-OK')])
        with patch.dict('os.environ', {'CADRE_REPORT_COMMAND_SAMPLE': '9'}):
            outcome = self.qa()
        bad = [b for b in outcome['findings'] if b.startswith('command-source')]
        self.assertEqual([b.split(':', 2)[1].strip() for b in bad], ['K3'], outcome['findings'])


class WaitThresholdLimitTest(unittest.TestCase):
    def decision(self, hour, minute, second):
        now_ts = datetime(2026, 9, 24, hour, minute, second).timestamp()
        with patch.object(router, 'chain', return_value=([], [])), patch('model_catalog.is_free', return_value=False):
            return router.cloud_wait('plan', 'hard', 50, now_ts=now_ts, threshold=router.WAIT_THRESHOLD_SEC)

    def test_waits_exactly_at_the_threshold_does_not_wait_one_second_beyond(self):
        self.assertEqual(router.WAIT_THRESHOLD_SEC, 7200)
        self.assertEqual(self.decision(22, 0, 0), 7200 + 60)
        self.assertEqual(self.decision(21, 59, 59), 0)


class BudgetFormulaLimitTest(unittest.TestCase):


    def test_go_period_until_the_reset_moment_last_day_then_a_new_period(self):
        with tempfile.TemporaryDirectory() as d, patch.object(provider_budget, 'QE', Path(d)):
            (Path(d) / 'chain').mkdir()
            (Path(d) / 'chain/go-period-reset.txt').write_text('2026-10-02 22:00')
            _start, reset, remaining = provider_budget.go_period(datetime(2026, 10, 2, 21, 59, 59))
            self.assertEqual((reset.month, reset.day, remaining), (10, 2, 1))
            _start, reset, remaining = provider_budget.go_period(datetime(2026, 10, 2, 22, 0, 0))
            self.assertEqual((reset.month, reset.day, remaining), (11, 2, 31))




class ResilienceTest(unittest.TestCase):
    def test_evidence_read_does_not_crash_on_giant_and_broken_lines(self):
        text = ("x" * 2_000_000 + "\n" + json.dumps({'id': 'K1', 'claim': 'a', 'round': 'file', 'source': 'app.py', 'expected': 'x'})
                 + "\n{\"id\": \"K1\"}\n{\"id\": 7}\n[1,2]\n�\n")
        records, findings = report_mode.evidence_read(text)
        self.assertEqual([k['id'] for k in records], ['K1'])
        self.assertGreaterEqual(len(findings), 4, findings)

    def test_big_report_qa_duration_is_limited(self):
        """3000 sentences + 200 file evidences: QA must not exceed 10 s (the external audit did not measure coverage; we measure time)."""
        t = Path(tempfile.mkdtemp(prefix='limit-big-'))
        self.addCleanup(shutil.rmtree, t, True)
        wt, sha = repo(t)
        evidence = [K(i, 'file', 'app.py:2', 'return a - b') for i in range(1, 201)]
        text = "# Summary\n" + "\n\n".join(f"Source [K{1 + i % 200}] supports finding {i}." for i in range(3000)) + "\n"
        evidence_write(wt, text, evidence)
        t0 = time.perf_counter()
        outcome = report_mode.report_qa(str(wt), report_mode.declaration(REPORT_PROMPT), sha, str(t / 'temporary'), seed='x')
        duration = time.perf_counter() - t0
        self.assertEqual(outcome['status'], 'PASS', outcome['findings'][:3])
        self.assertLess(duration, 10.0, f'report QA {duration:.1f} s elapsed')


if __name__ == '__main__':
    unittest.main()


class RecoveryBreathTest(unittest.TestCase):
    def test_recovery_skips_a_provider_that_cannot_carry_the_session(self):
        """B277 (01.10 rehearsal): the DESIGN-5 local->cloud hand-off picked the chain's first budgeted link (DS-pro); the DS share was enough for a single call
        but could not carry the session, the gate denied ~2 min later and the session went back to local (4 times; with money on Go). The rescuer needs a share for at least
        CADRE_RECOVERY_BREATH (6) calls; a provider without enough is skipped, other roles are not affected."""
        Y = core.ROUTER
        pay = {'opencode': 0.15, 'opencode-go': 5.0}
        chain = (['opencode/deepseek-v4-pro', 'opencode-go/qwen3.8-flash'], [])
        with patch.object(Y, 'gate_reservation', return_value=0.05), \
             patch.object(Y, 'allowance_usd', side_effect=lambda b, provider_key, *_: pay.get(provider_key, 0.0)), \
             patch.object(Y, 'run_pocket_remaining', return_value=None), patch('model_catalog.is_free', return_value=False), \
             patch.object(core, 'budget_json', return_value={'ts': 'x'}), patch.object(Y, 'chain', return_value=chain), \
             patch.object(Y, 'status_read', return_value={}):
            self.assertIsNotNone(Y.recovery_breath('opencode/deepseek-v4-pro', {}))
            self.assertIsNone(Y.recovery_breath('opencode-go/qwen3.8-flash', {}))
            self.assertEqual(core.cloud_candidate('recovery'), 'opencode-go/qwen3.8-flash')
            self.assertEqual(core.cloud_candidate('impl'), 'opencode/deepseek-v4-pro')


class LaneAlternativeTest(unittest.TestCase):
    def test_in_a_half_lane_the_next_links_of_the_chain_are_tried(self):
        """B281 (01.10 rehearsal): a half lane tried only ONE other provider and stopped. 4c-17: filtering not by provider but by
        (model, route) — in chain order excluding the failed route, without repeats, at most n; another model of the same provider is a candidate too."""
        z = ['opencode-go/glm-5.3-flash', 'opencode-go/qwen3.8-flash', 'deepseek/deepseek-v4-flash',
             'opencode/nemotron-3-ultra-free']
        with patch.object(router, 'chain', return_value=(z, [])):
            self.assertEqual(router.alternatives('impl', 'opencode-go/glm-5.3-flash'),
                             ['opencode-go/qwen3.8-flash', 'deepseek/deepseek-v4-flash', 'opencode/nemotron-3-ultra-free'])
            self.assertEqual(router.alternatives('impl', 'opencode-go/glm-5.3-flash', 1), ['opencode-go/qwen3.8-flash'])
            self.assertEqual(router.alternatives('impl', 'cerebras/qwen-3.8-27b'), z[:3])


class LaneAlternativeBudgetOrderTest(unittest.TestCase):
    def test_in_a_half_lane_the_budgeted_provider_earlier_in_the_chain_is_also_a_candidate(self):
        """B331 (03.10 live): a half lane on DeepSeek only looked at the ones AFTER it in the chain; with $50 sitting on Go it went
        to Cerebras (24 times more expensive per call) and the free fledge. The chain is already ordered by budget/abundance; all of it are candidates."""
        z = ['opencode-go/glm-5.3-flash', 'opencode-go/qwen3.8-flash', 'opencode/deepseek-v4-flash', 'deepseek/deepseek-v4-flash',
             'cerebras/qwen-3.8-27b', 'opencode/fledge-alpha-free']
        with patch.object(router, 'chain', return_value=(z, [])):
            self.assertEqual(router.alternatives('impl', 'deepseek/deepseek-v4-flash', 3),
                             ['opencode-go/glm-5.3-flash', 'opencode-go/qwen3.8-flash', 'opencode/deepseek-v4-flash'])


class BreathNetCapacityTest(unittest.TestCase):
    def test_recovery_breath_sees_the_pending_reservations_of_other_runs(self):
        """D08 (01.10 Codex): breathing room used the raw observed share; while another run's $1.50 pending reservation left $0.20 of a $2
        share, a 6 x $0.10 rescuer counted as suitable. Breathing room is measured with the ledger's net capacity."""
        import datetime, tempfile
        import budget_gate as bg
        with tempfile.TemporaryDirectory(prefix='breath-net-') as d:
            home = Path(d)
            now_ts = datetime.datetime.now()
            b = {'ts': now_ts.isoformat(), 'today': now_ts.date().isoformat(),
                 'zen': {'db': 'ok', 'pay': 2.0, 'available': 2.0}}
            ledger = bg.BudgetLedger(home / 'chain/request-budget.db')
            ledger.reserve('opencode', 1_500_000, b, {'run_id': 'other-run'})
            with patch.object(router, 'QE', home), patch.object(router, 'gate_reservation', return_value=.1), \
                 patch.object(router, 'run_pocket_remaining', return_value=None), patch('model_catalog.is_free', return_value=False):
                self.assertIsNotNone(router.recovery_breath('opencode/deepseek-v4-pro', b))
                ledger_empty = home / 'absent'
                with patch.object(router, 'QE', ledger_empty):
                    self.assertIsNone(router.recovery_breath('opencode/deepseek-v4-pro', b), 'Sufficient without pending reservations')


class BreathLimitTest(unittest.TestCase):
    def test_net_capacity_covers_exactly_six_calls_one_micro_dollar_short_does_not(self):
        """Codex §6/6 (N/N+1): net capacity exactly 6 x reservation -> suitable; one micro-dollar short -> breathless."""
        import datetime, tempfile
        import budget_gate as bg
        now_ts = datetime.datetime.now()
        b = {'ts': now_ts.isoformat(), 'today': now_ts.date().isoformat(),
             'zen': {'db': 'ok', 'pay': 2.0, 'available': 2.0}}   # share with margin 1.70
        for pending, expected_breathless in ((1_100_000, False), (1_100_001, True)):
            with self.subTest(pending=pending), tempfile.TemporaryDirectory(prefix='breath-limit-') as d:
                home = Path(d)
                bg.BudgetLedger(home / 'chain/request-budget.db').reserve('opencode', pending, b, {'run_id': 'other'})
                with patch.object(router, 'QE', home), patch.object(router, 'gate_reservation', return_value=.1), \
                     patch.object(router, 'run_pocket_remaining', return_value=None), patch('model_catalog.is_free', return_value=False):
                    self.assertEqual(router.recovery_breath('opencode/deepseek-v4-pro', b) is not None, expected_breathless)


class AlternativeBucketTest(unittest.TestCase):
    Z = ['opencode/deepseek-v4-flash', 'opencode-go/glm-5.3-flash', 'deepseek/deepseek-v4-flash',
         'cerebras/qwen-3.8-27b', 'opencode/nemotron-3-ultra-free']

    def free(self, provider_key, model):
        return model.endswith('-free')

    def test_paid_and_free_zen_are_separate_buckets_and_the_free_ring_is_tried(self):
        """D09 (01.10 Codex): B281 deduped paid zen and free zen as the same provider. 4c-17: filtering by (model, route);
        the paid zen route failing does not remove another zen model."""
        z = [*self.Z]
        with patch.object(router, 'chain', return_value=(z, [])), patch('model_catalog.is_free', side_effect=self.free):
            self.assertEqual(router.alternatives('impl', 'opencode/deepseek-v4-flash', 5), z[1:])

    def test_zero_or_negative_n_gives_no_alternative(self):
        """D18: n=0 returned one alternative."""
        with patch.object(router, 'chain', return_value=([*self.Z], [])), patch('model_catalog.is_free', side_effect=self.free):
            self.assertEqual(router.alternatives('impl', self.Z[1], 0), [])
            self.assertEqual(router.alternatives('impl', self.Z[1], -1), [])


class PricelessModelTest(unittest.TestCase):
    def test_model_with_a_missing_price_in_the_catalog_is_not_counted_as_free(self):
        """D17 (01.10 Codex, G-B8): `{id: x}` (no cost) returned is_free=True; the plugin counts the same model as paid."""
        import model_catalog
        for record in ({'id': 'priceless'}, {'id': 'half', 'cost': {'input': 0}}, {'id': 'output', 'cost': {'output': 0}}):
            with self.subTest(record=record), patch.object(model_catalog, 'model', return_value=record):
                self.assertIsNone(model_catalog.is_free('opencode', record['id']))
        with patch.object(model_catalog, 'model', return_value={'id': 'b', 'cost': {'input': 0, 'output': 0}}):
            self.assertIs(model_catalog.is_free('opencode', 'b'), True)
