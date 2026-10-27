"""Item 0c — opencode zen. The balance CANNOT BE READ BY A PROGRAM (neither API nor CLI gives it), so the source is the date+amount
the user entered, and the spending we measured ourselves is subtracted from it. Spending BEFORE that date does not count: the user's
past manual use must not be written to the engine's ledger."""

from __future__ import annotations

from contextlib import closing
import datetime
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import budget_gate as budget  # noqa: E402


def fake_db(path: Path, records):
    """records: [(provider, epoch_s, usd)] or [(provider, epoch_s, usd, modelID, tokens)] (B230: cache reads)"""
    with closing(sqlite3.connect(path)) as con, con:
        con.execute('create table message(id text, data text)')
        con.execute('create table part(id text, message_id text, time_created integer, data text)')
        for i, record in enumerate(records):
            provider_key, ts, usd = record[:3]
            message = {'providerID': provider_key, **({'modelID': record[3]} if len(record) > 3 else {})}
            part = {'type': 'step-finish', 'cost': usd, **({'tokens': record[4]} if len(record) > 4 else {})}
            con.execute('insert into message values(?,?)', (f'm{i}', json.dumps(message)))
            con.execute('insert into part values(?,?,?,?)', (f'p{i}', f'm{i}', int(ts * 1000), json.dumps(part)))


class ZenBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-zen-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'chain').mkdir()
        self.db = self.home / 'opencode.db'

    def window_pin(self, rolling=5.0):
        """Lesson from B90: when `go_window.read()` finds no cache in the isolated home it goes to the LIVE endpoint.
        After B86 zen's daily share was bound to Go's window, so these tests let the outside world
        in (when the real window closed 3 tests turned red at once). The test defines its own world."""
        (self.home / 'chain').mkdir(parents=True, exist_ok=True)
        (self.home / 'chain/go-window.json').write_text(json.dumps({
            'ts': __import__('time').time(), 'rolling': rolling, 'weekly': rolling, 'monthly': rolling,
            'resets': {}, 'history': []}))

    def budget(self):
        self.window_pin()
        env = {**os.environ, 'CADRE_HOME': str(self.home), 'OPENCODE_DB': str(self.db), 'CADRE_ENGINE_ROOT': str(LIB.parent)}
        env.pop('CADRE_SIM_LOAD', None)
        r = subprocess.run([sys.executable, str(LIB / 'provider_budget.py')], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)['zen']

    def balance_write(self, date: datetime.date, amount: float):
        (self.home / 'chain/zen-balance.txt').write_text(f'{date.isoformat()} {amount}\n')

    def full_budget(self, sim=None):
        self.window_pin()
        env = {**os.environ, 'CADRE_HOME': str(self.home), 'OPENCODE_DB': str(self.db), 'CADRE_ENGINE_ROOT': str(LIB.parent)}
        env.pop('CADRE_SIM_LOAD', None)
        if sim is not None:
            path = self.home / 'sim.json'
            path.write_text(json.dumps(sim))
            env['CADRE_SIM_LOAD'] = str(path)
        r = subprocess.run([sys.executable, str(LIB / 'provider_budget.py')], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_zen_takes_over_when_go_is_exhausted(self):
        """B86 (the user's question): "there is zen too, why does everything depend on Go?" — Go is a MONTHLY ALLOWANCE, unused allowance
        EXPIRES; zen is PREPAID and does not expire. So Go is spent first and zen is spread over 30 days ($0.28/day).
        BUT when Go closed, zen, which should be the fallback, could carry no work. While Go is closed the horizon shortens."""
        fake_db(self.db, [])
        self.balance_write(datetime.date.today(), 8.40)
        open = self.full_budget()
        self.assertGreater(open['go']['available'], 0, 'This test requires an open Go window')
        closed = self.full_budget(sim={'go_points': 1000.0})     # Go pool exhausted (4c-13: points)
        self.assertEqual(closed['go']['available'], 0)
        # S1(1) (03.10): no horizon/daily split — while Go is closed zen carries the job with its whole balance, never exceeding it.
        self.assertGreater(closed['zen']['available'], 0, 'Zen handles the work while Go is closed without falling back locally')
        self.assertLessEqual(closed['zen']['available'], 8.40, 'Allocation must not exceed the balance')

    def test_without_a_balance_entry_zen_offers_nothing(self):
        fake_db(self.db, [])
        z = self.budget()
        self.assertEqual(z['db'], 'absent')
        self.assertEqual(z['pay'], 0)
        self.assertEqual(z['available'], 0)

    def test_spending_before_the_entry_date_is_not_charged_to_the_engine(self):
        today = datetime.date.today()
        yesterday = today - datetime.timedelta(days=1)
        now_ts = datetime.datetime.now().timestamp()
        before = datetime.datetime.combine(yesterday, datetime.time(1, 0)).timestamp()
        # Go spending is kept SMALL: this test measures zen's date filter, not Go's state.
        # (After B86, if Go's daily share runs out zen's horizon shortens; a large Go spending tied this
        # test to that behaviour without noticing.)
        fake_db(self.db, [('opencode', before, 3.0), ('opencode', now_ts, 0.5), ('opencode-go', now_ts, 0.1)])
        self.balance_write(today, 10.0)
        z = self.budget()
        self.assertEqual(z['db'], 'ok')
        self.assertAlmostEqual(z['spent'], 0.5, places=4)     # yesterday's 3.0 does not count, Go's 9.0 never enters
        self.assertAlmostEqual(z['remaining'], 9.5, places=2)
        # S1(1): no daily split — share = remaining + spent today, available = remaining.
        self.assertAlmostEqual(z['available'], 9.5, places=2)

    def horizon(self):
        import calendar
        today = datetime.date.today()
        return max(30, calendar.monthrange(today.year, today.month)[1] - today.day + 1)


    def test_an_exhausted_balance_offers_nothing(self):
        today = datetime.date.today()
        fake_db(self.db, [('opencode', datetime.datetime.now().timestamp(), 12.0)])
        self.balance_write(today, 10.0)
        z = self.budget()
        self.assertEqual(z['remaining'], 0)
        self.assertEqual(z['available'], 0)

    def test_the_gate_treats_zen_as_a_paid_bucket_with_the_same_fifteen_percent_margin(self):
        instant = {'ts': datetime.datetime.now().isoformat(), 'today': datetime.date.today().isoformat(),
                 'zen': {'db': 'ok', 'pay': 1.0, 'available': 1.0}}
        _, remaining = budget.allowance(instant, 'opencode')
        self.assertEqual(remaining, 850_000)                          # 1.00 - 0.15 pay
        self.assertIn('opencode', budget.USD_PROVIDERS)           # enters the run dollar cap
        instant['zen']['db'] = 'absent'
        with self.assertRaises(budget.BudgetDenied):
            budget.allowance(instant, 'opencode')


class ZenCommandTests(unittest.TestCase):
    """`cadre zen-balance`: 1 query, 2 top up. A top-up writes the new balance with TODAY's date, the baseline shifts."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-zencmd-', ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        (self.home / 'chain').mkdir(parents=True)
        self.root = Path(self.temp.name) / 'root'
        (self.root / 'bin').mkdir(parents=True)
        (self.root / 'lib').symlink_to(LIB)
        self.db = self.home / 'opencode.db'
        fake_db(self.db, [])

    def cadre(self, *args, **env_extra):
        env = {**os.environ, 'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(self.root),
               'OPENCODE_DB': str(self.db), **env_extra}
        env.pop('CADRE_RUN_ID', None)
        return subprocess.run(['bash', str(LIB.parent / 'bin/cadre.sh'), *args],
                              env=env, capture_output=True, text=True, timeout=90, stdin=subprocess.DEVNULL)

    def test_query_then_top_up_then_query_again(self):
        r = self.cadre('zen-balance', CADRE_ZEN_CHOICE='1')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('no record', r.stdout)
        r = self.cadre('zen-balance', CADRE_ZEN_CHOICE='2', CADRE_ZEN_AMOUNT='8.44')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('Fresh balance: $8.44', r.stdout)
        written = (self.home / 'chain/zen-balance.txt').read_text().split()
        # B92: since the baseline is the ENTRY TIME the file now writes '<date>T<time> <amount>'
        self.assertEqual(len(written), 2)
        self.assertTrue(written[0].startswith(datetime.date.today().isoformat()))
        self.assertIn('T', written[0], 'A timestamp is required to exclude spending earlier that day')
        self.assertEqual(written[1], '8.44')
        r = self.cadre('zen-balance', CADRE_ZEN_CHOICE='1')
        self.assertIn('entered   : $8.44', r.stdout)
        self.assertIn('remaining : $8.44', r.stdout)
        # a second top-up adds ON TOP of the current remainder
        r = self.cadre('zen-balance', CADRE_ZEN_CHOICE='2', CADRE_ZEN_AMOUNT='5')
        self.assertIn('Fresh balance: $13.44', r.stdout)

    def test_a_bad_amount_writes_nothing(self):
        for amount in ('abc', '1.2.3', '.', '5.'):
            with self.subTest(amount=amount):
                r = self.cadre('zen-balance', CADRE_ZEN_CHOICE='2', CADRE_ZEN_AMOUNT=amount)
                self.assertEqual(r.returncode, 1)
                self.assertFalse((self.home / 'chain/zen-balance.txt').exists())


if __name__ == '__main__':
    unittest.main()


class ZenLiveListTests(unittest.TestCase):
    """The catalogue is a SUPERSET: it lists 103 models for zen, the account can use 71. Putting a model the account cannot use
    into the chain means the call fails with 401 and wastes a link of the chain."""

    def setUp(self):
        import model_catalog
        self.catalog = model_catalog
        self.temp = tempfile.TemporaryDirectory(prefix='qe-live-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'chain').mkdir()
        self.old = os.environ.get('CADRE_HOME')
        os.environ['CADRE_HOME'] = str(self.home)
        self.addCleanup(lambda: os.environ.__setitem__('CADRE_HOME', self.old)
                        if self.old else os.environ.pop('CADRE_HOME', None))

    def list_write(self, models):
        (self.home / 'chain/opencode-live.json').write_text(json.dumps({'ts': 1, 'models': models}))

    def test_without_a_live_list_nothing_is_filtered(self):
        self.assertIsNone(self.catalog.live_list('opencode'))
        self.assertTrue(self.catalog.available('opencode', 'any-model'))

    def test_a_model_the_account_cannot_use_is_dropped_from_the_chain(self):
        import importlib
        import model_router
        importlib.reload(model_router)
        self.list_write(['big-pickle'])
        self.assertTrue(self.catalog.available('opencode', 'big-pickle'))
        self.assertFalse(self.catalog.available('opencode', 'glm-5-free'))
        with unittest.mock.patch.dict(model_router.ROLES, {'attempt': ['opencode/glm-5-free', 'opencode/big-pickle']}), \
             unittest.mock.patch.object(model_router, 'has_budget', return_value=(True, 'test')):   # the role table is restored after the test
            z, reason = model_router.chain('attempt', b={}, d={})
        self.assertNotIn('opencode/glm-5-free', z)
        self.assertIn('opencode/big-pickle', z)
        self.assertTrue(any('unavailable on this account' in n for n in reason), reason)

    def test_a_corrupt_or_empty_list_never_blocks_everything(self):
        (self.home / 'chain/opencode-live.json').write_text('{broken')
        self.assertTrue(self.catalog.available('opencode', 'big-pickle'))
        self.list_write([])
        self.assertTrue(self.catalog.available('opencode', 'big-pickle'))
