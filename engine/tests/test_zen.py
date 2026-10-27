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
import butce_kapisi as budget  # noqa: E402


def sahte_db(yol: Path, kayitlar):
    """records: [(provider, epoch_s, usd)] or [(provider, epoch_s, usd, modelID, tokens)] (B230: cache reads)"""
    with closing(sqlite3.connect(yol)) as con, con:
        con.execute('create table message(id text, data text)')
        con.execute('create table part(id text, message_id text, time_created integer, data text)')
        for i, kayit in enumerate(kayitlar):
            sag, ts, usd = kayit[:3]
            mesaj = {'providerID': sag, **({'modelID': kayit[3]} if len(kayit) > 3 else {})}
            parca = {'type': 'step-finish', 'cost': usd, **({'tokens': kayit[4]} if len(kayit) > 4 else {})}
            con.execute('insert into message values(?,?)', (f'm{i}', json.dumps(mesaj)))
            con.execute('insert into part values(?,?,?,?)', (f'p{i}', f'm{i}', int(ts * 1000), json.dumps(parca)))


class ZenButceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-zen-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'zincir').mkdir()
        self.db = self.home / 'opencode.db'

    def pencere_sabitle(self, rolling=5.0):
        """Lesson from B90: when `go_pencere.oku()` finds no cache in the isolated home it goes to the LIVE endpoint.
        After B86 zen's daily share was bound to Go's window, so these tests let the outside world
        in (when the real window closed 3 tests turned red at once). The test defines its own world."""
        (self.home / 'zincir').mkdir(parents=True, exist_ok=True)
        (self.home / 'zincir/go-pencere.json').write_text(json.dumps({
            'ts': __import__('time').time(), 'rolling': rolling, 'weekly': rolling, 'monthly': rolling,
            'resets': {}, 'gecmis': []}))

    def butce(self):
        self.pencere_sabitle()
        env = {**os.environ, 'CADRE_HOME': str(self.home), 'OPENCODE_DB': str(self.db), 'CADRE_ENGINE_ROOT': str(LIB.parent)}
        env.pop('CADRE_SIM_YUK', None)
        r = subprocess.run([sys.executable, str(LIB / 'butce.py')], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)['zen']

    def bakiye_yaz(self, tarih: datetime.date, tutar: float):
        (self.home / 'zincir/zen-bakiye.txt').write_text(f'{tarih.isoformat()} {tutar}\n')

    def tam_butce(self, sim=None):
        self.pencere_sabitle()
        env = {**os.environ, 'CADRE_HOME': str(self.home), 'OPENCODE_DB': str(self.db), 'CADRE_ENGINE_ROOT': str(LIB.parent)}
        env.pop('CADRE_SIM_YUK', None)
        if sim is not None:
            yol = self.home / 'sim.json'
            yol.write_text(json.dumps(sim))
            env['CADRE_SIM_YUK'] = str(yol)
        r = subprocess.run([sys.executable, str(LIB / 'butce.py')], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_zen_takes_over_when_go_is_exhausted(self):
        """B86 (the user's question): "there is zen too, why does everything depend on Go?" — Go is a MONTHLY ALLOWANCE, unused allowance
        EXPIRES; zen is PREPAID and does not expire. So Go is spent first and zen is spread over 30 days ($0.28/day).
        BUT when Go closed, zen, which should be the fallback, could carry no work. While Go is closed the horizon shortens."""
        sahte_db(self.db, [])
        self.bakiye_yaz(datetime.date.today(), 8.40)
        acik = self.tam_butce()
        self.assertGreater(acik['go']['kullanilabilir'], 0, 'bu testin on kosulu: Go acik')
        kapali = self.tam_butce(sim={'go_puan': 1000.0})     # Go pool exhausted (4c-13: points)
        self.assertEqual(kapali['go']['kullanilabilir'], 0)
        # S1(1) (03.10): no horizon/daily split — while Go is closed zen carries the job with its whole balance, never exceeding it.
        self.assertGreater(kapali['zen']['kullanilabilir'], 0, 'Go kapaliyken zen isi tasir, yerele cokmez')
        self.assertLessEqual(kapali['zen']['kullanilabilir'], 8.40, 'pay bakiyeyi asamaz')

    def test_without_a_balance_entry_zen_offers_nothing(self):
        sahte_db(self.db, [])
        z = self.butce()
        self.assertEqual(z['db'], 'yok')
        self.assertEqual(z['pay'], 0)
        self.assertEqual(z['kullanilabilir'], 0)

    def test_spending_before_the_entry_date_is_not_charged_to_the_engine(self):
        bugun = datetime.date.today()
        dun = bugun - datetime.timedelta(days=1)
        simdi = datetime.datetime.now().timestamp()
        oncesi = datetime.datetime.combine(dun, datetime.time(1, 0)).timestamp()
        # Go spending is kept SMALL: this test measures zen's date filter, not Go's state.
        # (After B86, if Go's daily share runs out zen's horizon shortens; a large Go spending tied this
        # test to that behaviour without noticing.)
        sahte_db(self.db, [('opencode', oncesi, 3.0), ('opencode', simdi, 0.5), ('opencode-go', simdi, 0.1)])
        self.bakiye_yaz(bugun, 10.0)
        z = self.butce()
        self.assertEqual(z['db'], 'ok')
        self.assertAlmostEqual(z['harcanan'], 0.5, places=4)     # yesterday's 3.0 does not count, Go's 9.0 never enters
        self.assertAlmostEqual(z['kalan'], 9.5, places=2)
        # S1(1): no daily split — share = remaining + spent today, available = remaining.
        self.assertAlmostEqual(z['kullanilabilir'], 9.5, places=2)

    def ufuk(self):
        import calendar
        bugun = datetime.date.today()
        return max(30, calendar.monthrange(bugun.year, bugun.month)[1] - bugun.day + 1)


    def test_an_exhausted_balance_offers_nothing(self):
        bugun = datetime.date.today()
        sahte_db(self.db, [('opencode', datetime.datetime.now().timestamp(), 12.0)])
        self.bakiye_yaz(bugun, 10.0)
        z = self.butce()
        self.assertEqual(z['kalan'], 0)
        self.assertEqual(z['kullanilabilir'], 0)

    def test_the_gate_treats_zen_as_a_paid_bucket_with_the_same_fifteen_percent_margin(self):
        anlik = {'ts': datetime.datetime.now().isoformat(), 'bugun': datetime.date.today().isoformat(),
                 'zen': {'db': 'ok', 'pay': 1.0, 'kullanilabilir': 1.0}}
        _, kalan = budget.allowance(anlik, 'opencode')
        self.assertEqual(kalan, 850_000)                          # 1.00 - 0.15 pay
        self.assertIn('opencode', budget.USD_PROVIDERS)           # enters the run dollar cap
        anlik['zen']['db'] = 'yok'
        with self.assertRaises(budget.BudgetDenied):
            budget.allowance(anlik, 'opencode')


class ZenKomutTests(unittest.TestCase):
    """`cadre zen-bakiye`: 1 query, 2 top up. A top-up writes the new balance with TODAY's date, the baseline shifts."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-zencmd-', ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        (self.home / 'zincir').mkdir(parents=True)
        self.root = Path(self.temp.name) / 'kok'
        (self.root / 'bin').mkdir(parents=True)
        (self.root / 'lib').symlink_to(LIB)
        self.db = self.home / 'opencode.db'
        sahte_db(self.db, [])

    def cadre(self, *args, **env_ek):
        env = {**os.environ, 'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(self.root),
               'OPENCODE_DB': str(self.db), **env_ek}
        env.pop('CADRE_RUN_ID', None)
        return subprocess.run(['bash', str(LIB.parent / 'bin/cadre.sh'), *args],
                              env=env, capture_output=True, text=True, timeout=90, stdin=subprocess.DEVNULL)

    def test_query_then_top_up_then_query_again(self):
        r = self.cadre('zen-bakiye', CADRE_ZEN_SECIM='1')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('kayit yok', r.stdout)
        r = self.cadre('zen-bakiye', CADRE_ZEN_SECIM='2', CADRE_ZEN_TUTAR='8.44')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('Yeni bakiye: $8.44', r.stdout)
        yazilan = (self.home / 'zincir/zen-bakiye.txt').read_text().split()
        # B92: since the baseline is the ENTRY TIME the file now writes '<date>T<time> <amount>'
        self.assertEqual(len(yazilan), 2)
        self.assertTrue(yazilan[0].startswith(datetime.date.today().isoformat()))
        self.assertIn('T', yazilan[0], 'saat yazilmali; yoksa o gun onceden harcanan da dusulur')
        self.assertEqual(yazilan[1], '8.44')
        r = self.cadre('zen-bakiye', CADRE_ZEN_SECIM='1')
        self.assertIn('girilen   : $8.44', r.stdout)
        self.assertIn('kalan     : $8.44', r.stdout)
        # a second top-up adds ON TOP of the current remainder
        r = self.cadre('zen-bakiye', CADRE_ZEN_SECIM='2', CADRE_ZEN_TUTAR='5')
        self.assertIn('Yeni bakiye: $13.44', r.stdout)

    def test_a_bad_amount_writes_nothing(self):
        for tutar in ('abc', '1.2.3', '.', '5.'):
            with self.subTest(tutar=tutar):
                r = self.cadre('zen-bakiye', CADRE_ZEN_SECIM='2', CADRE_ZEN_TUTAR=tutar)
                self.assertEqual(r.returncode, 1)
                self.assertFalse((self.home / 'zincir/zen-bakiye.txt').exists())


if __name__ == '__main__':
    unittest.main()


class ZenCanliListeTests(unittest.TestCase):
    """The catalogue is a SUPERSET: it lists 103 models for zen, the account can use 71. Putting a model the account cannot use
    into the chain means the call fails with 401 and wastes a link of the chain."""

    def setUp(self):
        import katalog
        self.katalog = katalog
        self.temp = tempfile.TemporaryDirectory(prefix='qe-canli-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'zincir').mkdir()
        self.eski = os.environ.get('CADRE_HOME')
        os.environ['CADRE_HOME'] = str(self.home)
        self.addCleanup(lambda: os.environ.__setitem__('CADRE_HOME', self.eski)
                        if self.eski else os.environ.pop('CADRE_HOME', None))

    def liste_yaz(self, modeller):
        (self.home / 'zincir/opencode-canli.json').write_text(json.dumps({'ts': 1, 'modeller': modeller}))

    def test_without_a_live_list_nothing_is_filtered(self):
        self.assertIsNone(self.katalog.canli_liste('opencode'))
        self.assertTrue(self.katalog.kullanilabilir('opencode', 'her-neyse'))

    def test_a_model_the_account_cannot_use_is_dropped_from_the_chain(self):
        import importlib
        import yonlendirici
        importlib.reload(yonlendirici)
        self.liste_yaz(['big-pickle'])
        self.assertTrue(self.katalog.kullanilabilir('opencode', 'big-pickle'))
        self.assertFalse(self.katalog.kullanilabilir('opencode', 'glm-5-free'))
        with unittest.mock.patch.dict(yonlendirici.ROLLER, {'deneme': ['opencode/glm-5-free', 'opencode/big-pickle']}), \
             unittest.mock.patch.object(yonlendirici, 'butce_var', return_value=(True, 'test')):   # the role table is restored after the test
            z, neden = yonlendirici.zincir('deneme', b={}, d={})
        self.assertNotIn('opencode/glm-5-free', z)
        self.assertIn('opencode/big-pickle', z)
        self.assertTrue(any('hesapta yok' in n for n in neden), neden)

    def test_a_corrupt_or_empty_list_never_blocks_everything(self):
        (self.home / 'zincir/opencode-canli.json').write_text('{bozuk')
        self.assertTrue(self.katalog.kullanilabilir('opencode', 'big-pickle'))
        self.liste_yaz([])
        self.assertTrue(self.katalog.kullanilabilir('opencode', 'big-pickle'))
