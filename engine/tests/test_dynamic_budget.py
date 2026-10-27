"""D1/D2 — the user's 16.09 footnote: "the one with a lot of budget can be pulled forward, the shrinking one throttled; tracking must be continuous".
06.10: daily share/borrowing/pace were removed; share = the remaining balance (prepaid) or the Go period remainder.
D2 dynamic order: among paid candidates in the same quality tier ONLY the tight one is moved back; the quality order and
the free/local/claude links are kept (decision 2). Fake data, no network."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import provider_budget  # noqa: E402
import model_router as router  # noqa: E402


def fake_budget(go_used_amount=2.7, go_base=1.0, ds_used_amount=0.5, ds_base=1.0, cb_used_amount=3.0, cb_base=1.0):
    import time as _t
    return {'ts': _t.strftime('%Y-%m-%dT%H:%M:%S'), 'today': _t.strftime('%Y-%m-%d'), 'remaining_day': 10,'go': {'db': 'ok', 'available': (go_used_amount) * 100 / 15, 'pay_base': (go_base) * 100 / 15, 'pay': (go_base) * 100 / 15, 'window_open': True, 'ratio': 1.0, 'unit': 'points'},
            'ds': {'api': 'ok', 'available': ds_used_amount, 'pay_base': ds_base, 'pay': ds_base, 'ratio': 1.0},
            'cerebras': {'db': 'ok', 'available': cb_used_amount, 'pay_base': cb_base, 'pay': cb_base, 'credit_remaining': 5.0, 'ratio': 1.0},
            'gemini': {'db': 'ok', 'remaining_request': 400, 'today_request': 100, 'ratio': 0.8},
            'groq': {'db': 'ok', 'today_request': 0, 'ratio': 1.0}, 'mistral': {'db': 'ok', 'today_request': 0, 'ratio': 1.0},
            'claude': {'source': 'fresh', 'pay': 5, 'percent1_usd': 30, 'pool_remaining_usd': 200}}


class DynamicOrderTests(unittest.TestCase):
    CANDIDATES = ['opencode-go/kimi-k2.7-code', 'deepseek/deepseek-v4-flash', 'cerebras/qwen-3.8-27b', 'google/gemini-3-flash-preview']

    # Item 7 (17.09): the order now also looks at the CURRENT UNIT PRICE (DeepSeek peak 01-04/06-10 UTC = 2x).
    # This test is about ABUNDANCE, so the clock is pinned; peak behaviour is in test_pool_order.py.
    OFF_PEAK = __import__('calendar').timegm(__import__('time').strptime('2026-09-17 12:00', '%Y-%m-%d %H:%M'))

    def sort(self, b, d=None, observed_at=None):
        return router.abundance_by_sort(list(self.CANDIDATES), b, d if d is not None else {}, observed_at or self.OFF_PEAK)

    def test_net_budget_orders_equal_quality_slots_by_role_cost(self):
        import tempfile
        from test_budget_period import snapshot
        from budget_gate import BudgetLedger, allowance
        with tempfile.TemporaryDirectory() as tmp, patch.object(router, 'QE', Path(tmp)), patch.object(router, 'record'), \
                patch.dict(os.environ, {'CADRE_RUN_ID': '', 'CADRE_TASK_ID': ''}):
            b = snapshot(20)
            b['zen'] = {'db': 'ok', 'pay': .35, 'available': .35}
            # D13 (03.10): deepseek-v4-pro is now QUALITY_STRICT tier 1 of the impl links — abundance_by_sort
            # measures it too, so a readable 'ds' observation is needed (otherwise the whole tier falls back to index order).
            b['ds'] = {'api': 'ok', 'pay': 10, 'available': 10}
            zen, go = 'opencode/deepseek-v4-flash', 'opencode-go/glm-5.3-flash'
            candidates = ['claude/opus#max', zen, 'deepseek/deepseek-v4-pro', go]
            for role in router.ROLES:
                order = router.abundance_by_sort(candidates, b, role=role)
                self.assertEqual(order, [candidates[0], go, candidates[2], zen], role)
            with patch.object(router, 'has_budget', return_value=(True, 'fixture')), \
                 patch.object(router, 'ladder', return_value='active'), \
                 patch('model_catalog.available', return_value=True), patch.dict(router.ROLES, {'impl': [zen, go]}):
                self.assertEqual(router.chain('impl', b=b, d={})[0][0], zen,
                                 'D16: model identity order precedes provider wallet ordering')
            b['job_cost'] = {'lanes': {'opencode-go': 100, 'opencode': .01}}
            self.assertEqual(router.abundance_by_sort([go, zen], b), [zen, go], 'Cost is as decisive as allocation')
            b.pop('job_cost')
            ledger = BudgetLedger(Path(tmp) / 'chain/request-budget.db')
            ledger.reserve('opencode-go', allowance(b, 'opencode-go', model='glm-5.3-flash')[1] - 100_000, b, {'model': 'glm-5.3-flash'})
            self.assertEqual(router.abundance_by_sort([go, zen], b), [zen, go], 'Deduct pending amounts from available allocation')
            with patch.object(router, 'net_capacity_usd', return_value=1):
                self.assertEqual(router.abundance_by_sort([zen, go], b), [zen, go], 'Use the supplied order to break ties')

    def test_free_local_and_claude_links_are_never_reordered(self):
        b = fake_budget(go_used_amount=0.1, ds_used_amount=0.1, cb_used_amount=0.1)   # all are tight
        chain = router.abundance_by_sort(['claude/opus#max', 'opencode-go/glm-5.2', 'opencode-go/qwen3.8-max'], b)
        self.assertEqual(chain[0], 'claude/opus#max', 'Do not reorder Claude')   # 4c-11: no free/local link, only the Claude rule remains

    def test_chain_excludes_local_and_is_empty_without_budget_data_and_ordered_with_it(self):
        # D13/P3 (03.10): NO LOCAL LINK in the impl chain; all three candidates are paid ds-v4-pro routes. Without budget
        # data all are removed with 'no budget/no verified share' -> the chain is EMPTY (budget wait), it does not fall to a local last link
        # as in the old behaviour. With a fully budgeted observation the paid routes stay.
        for b in ({}, None):
            import tempfile
            with patch.object(router, 'QE', Path(tempfile.mkdtemp(prefix='dynamic-chain-'))), \
                 patch.object(router, 'budget_read', return_value={}), patch.object(router, 'record'):
                z, _ = router.chain('impl', 'middle', 20000, b, {})
            self.assertEqual(z, [], z)
        self.assertEqual(router.abundance_by_sort(list(self.CANDIDATES), {}), self.CANDIDATES)
        with tempfile.TemporaryDirectory(prefix='dynamic-chain-') as tmp, \
             patch.object(router, 'QE', Path(tmp)), patch.object(router, 'record'):
            z, _ = router.chain('impl', 'middle', 20000, fake_budget(), {})
        self.assertGreater(len(z), 1, 'A fully funded snapshot must have a nonempty chain')
        self.assertTrue(all(not m.startswith('lmstudio/') for m in z), z)


if __name__ == '__main__':
    unittest.main()

class PrepaidSingleDropTest(unittest.TestCase):
    """D02 (01.10 Codex): in job-cap mode the observation gave the REMAINING allowance and the ledger subtracted the same spending again."""

    def observation(self, home, db, sim):
        import subprocess
        (home / 'sim.json').write_text(json.dumps(sim))
        env = {**os.environ, 'CADRE_HOME': str(home), 'OPENCODE_DB': str(db), 'CADRE_ENGINE_ROOT': str(LIB.parent),
               'CADRE_SIM_LOAD': str(home / 'sim.json'), 'CADRE_PREPAID_MODE': 'job-cap', 'HOME': str(home),
               'CADRE_GO_WINDOW_TTL': '1e9'}   # a stale cache is read without a network call; what follows is added with Σ
        r = subprocess.run([sys.executable, str(LIB / 'provider_budget.py')], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_same_spend_in_observation_and_ledger_counts_once(self):
        from contextlib import closing
        import datetime, sqlite3, tempfile, time
        import budget_gate as bg
        from test_zen import fake_db
        with tempfile.TemporaryDirectory(prefix='single-falling-') as d:
            home = Path(d)
            (home / 'chain').mkdir()
            (home / 'chain/go-window.json').write_text(json.dumps({'ts': time.time() - 3 * 3600, 'rolling': 5.0, 'weekly': 5.0,
                                                                     'monthly': 5.0, 'resets': {}, 'history': []}))
            entry = (datetime.datetime.now() - datetime.timedelta(days=1)).isoformat(timespec='seconds')
            (home / 'chain/zen-balance.txt').write_text(f'{entry} 10\n')
            empty, full = home / 'empty.db', home / 'full.db'
            fake_db(empty, [])
            now_ts = time.time()
            fake_db(full, [(provider_key, now_ts - 30, 0.5) for provider_key in ('opencode', 'opencode-go')])
            once = self.observation(home, empty, {})
            then = self.observation(home, full, {})
            ledger = bg.BudgetLedger(home / 'chain/request-budget.db')
            for provider_key, receipt in (('opencode', 500_000), ('opencode-go', 500_000)):   # Go: list price, counted in points
                with self.subTest(provider_key=provider_key):
                    a0 = bg.BudgetLedger(home / 'absent.db').available(provider_key, once)
                    rid = ledger.reserve(provider_key, receipt, once, {})
                    ledger.settle(rid, receipt, 'receipt-' + provider_key)
                    with closing(sqlite3.connect(home / 'chain/request-budget.db')) as con, con:
                        con.execute("update reservation set created=?, metadata=json_set(metadata,'$.settled_ts',?) where id=?",
                                    (now_ts - 30, now_ts - 30, rid))
                    a1 = ledger.available(provider_key, then)
                    self.assertGreater(a0, receipt)
                    self.assertAlmostEqual(a0 - a1, receipt, delta=2000, msg=(a0, a1, once[bg.PAID[provider_key]], then[bg.PAID[provider_key]]))


class GoSinglePoolTest(unittest.TestCase):
    """4c-13: Go is one pool and its unit is points (1 point = 1% of the monthly pool); window percentage = Σ spending ÷ (share × model limit).
    The gate is the API percentage; steps after the reading are added with Σ; there is no coefficient and no $60 conversion."""

    def home_dir(self, d, window, records, resets=None):
        from test_zen import fake_db
        home = Path(d)
        (home / 'chain').mkdir()
        if window is not None:
            (home / 'chain/go-window.json').write_text(json.dumps({**window, 'resets': resets or {}, 'history': []}))
        fake_db(home / 'opencode.db', records)
        return home

    def observation(self, home):
        import io
        import go_window
        out = io.StringIO()
        env = {k: v for k, v in os.environ.items() if not k.startswith('CADRE_TEST_') and k != 'CADRE_SIM_LOAD'}
        with patch.dict(os.environ, env, clear=True), patch.object(provider_budget, 'QE', home), \
             patch.object(provider_budget, 'DB', home / 'opencode.db'), \
             patch.object(Path, 'home', return_value=home), patch('sys.stdout', out), \
             patch.object(provider_budget, 'session_count', return_value=1), \
             patch.object(go_window, 'read', side_effect=lambda h, *a, **k: go_window._cache(h) or None):
            provider_budget.main()
        return json.loads(out.getvalue())

    def test_remaining_points_come_from_the_narrowest_window_and_steps_after_the_reading_are_deducted(self):
        import tempfile, time
        now_ts = time.time()
        with tempfile.TemporaryDirectory(prefix='go-pool-') as d:
            api = {'ts': now_ts - 60, 'rolling': 40.0, 'weekly': 20.0, 'monthly': 50.0}
            # before the reading $0.30 (not counted: it is in the API) + after glm-5.3 $0.15 = 1 point (DeepSeek depends on the hour, not here)
            home = self.home_dir(d, api, [('opencode-go', now_ts - 120, 0.30, 'glm-5.3'),
                                    ('opencode-go', now_ts - 30, 0.15, 'glm-5.3')])
            go = self.observation(home)['go']
        self.assertEqual((go['source'], go['unit'], go['db']), ('api', 'points', 'ok'), go)
        self.assertAlmostEqual(go['points_then'], 1.0, places=6)
        self.assertAlmostEqual(go['month_points'], 51.0, places=6)
        # rolling (95-40)x0.2 = 11 < weekly 37.5 < monthly 45 -> 11 - 1 = 10 points
        self.assertAlmostEqual(go['available'], 10.0, places=6)
        self.assertNotIn('coefficient', json.dumps(go))

    def test_without_api_the_sigma_formula_is_the_last_seven_days(self):
        import tempfile, time
        now_ts = time.time()
        with tempfile.TemporaryDirectory(prefix='go-sigma-') as d:
            home = self.home_dir(d, None, [('opencode-go', now_ts - 3 * 86400, 0.6, 'glm-5.3-flash'),   # $60 -> 1 points
                                     ('opencode-go', now_ts - 3600, 0.3, 'qwen3.8-flash')])       # $30 -> 1 points
            go = self.observation(home)['go']
        self.assertEqual(go['source'], 'db', go)
        self.assertAlmostEqual(go['own']['rolling'], 5.0, places=4)    # 1 points / 0,2
        self.assertAlmostEqual(go['own']['weekly'], 4.0, places=4)     # 2 points / 0,5
        self.assertAlmostEqual(go['month_points'], 2.0, places=4)

    def test_unreadable_db_and_no_api_means_closed(self):
        import tempfile
        import budget_gate as bg
        with tempfile.TemporaryDirectory(prefix='no-go') as d:
            home = Path(d); (home / 'chain').mkdir()
            go = self.observation(home)
        self.assertEqual(go['go']['db'], 'unreachable')
        with self.assertRaises(bg.BudgetDenied):
            bg.allowance(go, 'opencode-go')

    def test_gate_converts_points_to_the_models_list_dollars_pending_ones_count_in_points(self):
        import tempfile, time
        import budget_gate as bg
        b = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'today': time.strftime('%Y-%m-%d'),
             'go': {'db': 'ok', 'unit': 'points', 'available': 10.0, 'pay': 60.0, 'window_open': True}}
        self.assertEqual(bg.allowance(b, 'opencode-go', model='deepseek-v4-pro')[1], 1_500_000)   # 10 x $0,15
        self.assertEqual(bg.allowance(b, 'opencode-go', model='glm-5.3-flash')[1], 6_000_000)     # 10 x $0,60
        self.assertEqual(bg.allowance(b, 'opencode-go')[1], 1_500_000, 'Without a model, use the strictest limit')
        with tempfile.TemporaryDirectory(prefix='go-gate-') as d:
            ledger = bg.BudgetLedger(Path(d) / 'request-budget.db')
            ledger.reserve('opencode-go', 3_000_000, b, {'model': 'deepseek-v4-flash'})   # $3 / $30 limit = 10 points: the pool is full
            self.assertEqual(ledger.available('opencode-go', b, model='deepseek-v4-pro'), 0)
            with self.assertRaises(bg.BudgetDenied):
                ledger.reserve('opencode-go', 1, b, {'model': 'glm-5.3-flash'})
        with tempfile.TemporaryDirectory(prefix='go-gate2-') as d:
            ledger = bg.BudgetLedger(Path(d) / 'request-budget.db')
            ledger.reserve('opencode-go', 300_000, b, {'model': 'deepseek-v4-flash'})     # 1 points
            self.assertEqual(ledger.available('opencode-go', b, model='deepseek-v4-pro'), 1_350_000)   # 9 x $0,15

    def test_at_peak_hours_go_deepseek_writes_double_points(self):
        import calendar, tempfile, time
        import go_window
        dense = calendar.timegm(time.strptime('2026-10-07 07:30', '%Y-%m-%d %H:%M'))    # Car 07:30 UTC
        quiet = calendar.timegm(time.strptime('2026-10-07 12:30', '%Y-%m-%d %H:%M'))
        with tempfile.TemporaryDirectory(prefix='go-dense-') as d:
            home = self.home_dir(d, None, [('opencode-go', dense, 0.15, 'deepseek-v4-pro'), ('opencode-go', quiet, 0.15, 'deepseek-v4-pro'),
                                     ('opencode-go', dense, 0.15, 'glm-5.3')])
            self.assertAlmostEqual(go_window.sigma(home / 'opencode.db', dense - 1, quiet + 1), 4.0, places=6)

    def test_monthly_cash_pool_does_not_count_go(self):
        import tempfile
        import monthly_budget
        with tempfile.TemporaryDirectory(prefix='go-cash-') as d:
            ledger = monthly_budget.MonthlyLedger(Path(d) / 'monthly-budget.db', cap=65_000_000)
            with ledger.transaction() as con:   # 4c-13 migration: this month's old Go line does not count even if it stays in the pool
                con.execute('insert into reservation values(?,?,?,?,?,?,?,?)',
                            ('old', 'opencode-go', monthly_budget.period(), 0.0, 30_000_000, 30_000_000, 'r-old', '{}'))
            self.assertEqual(ledger.status()['remaining_micro'], 65_000_000)
            with self.assertRaises(monthly_budget.MonthlyBudgetDenied):
                ledger.reserve('opencode-go', 1)
        self.assertEqual(monthly_budget.DEFAULT_CAP_USD, 65)

    def test_watcher_line_shows_points(self):
        import tempfile, time
        import quota_trend
        now_ts = time.time()
        with tempfile.TemporaryDirectory(prefix='go-watch-') as d:
            home = self.home_dir(d, None, [('opencode-go', now_ts - 60, 0.15, 'qwen3.8-max')])
            line = quota_trend.line(home / 'opencode.db', now=now_ts)
        self.assertIn('1.00 monthly points', line)
        self.assertIn('rolling %5.0', line)
