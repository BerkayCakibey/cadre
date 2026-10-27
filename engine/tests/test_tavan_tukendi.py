"""17.09 live measurement: the run cap was set to $0.05, the run was retried 12 times over **159 minutes**,
hit the same "run budget exhausted" wall every time and finally failed.

The run cap does NOT GROW within a run; retrying with the same denial only burns time. These tests prove that a run with a full cap
fails fast and with an explanation, while a run whose cap is not full is retried as before."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from kosu import kosu_tavani_tukendi  # noqa: E402

RED = [{'provider': 'claude', 'reason': 'run budget exhausted: reservation $1.25 > remaining $0.05'}]


class TavanTukendiTests(unittest.TestCase):
    def test_a_cap_that_is_spent_is_permanent(self):
        self.assertTrue(kosu_tavani_tukendi({'denials': RED, 'budget_cap_usd': 0.05, 'budget_spent_usd': 0.05}))
        self.assertTrue(kosu_tavani_tukendi({'denials': RED, 'budget_cap_usd': 5.0, 'budget_spent_usd': 4.6}))

    def test_a_cap_with_room_left_is_still_retryable(self):
        self.assertFalse(kosu_tavani_tukendi({'denials': RED, 'budget_cap_usd': 5.0, 'budget_spent_usd': 0.3}))

    def test_without_a_cap_denial_nothing_changes(self):
        self.assertFalse(kosu_tavani_tukendi({'denials': [{'provider': 'go', 'reason': 'cooldown'}],
                                              'budget_cap_usd': 0.05, 'budget_spent_usd': 0.05}))
        self.assertFalse(kosu_tavani_tukendi({'budget_cap_usd': 0.05, 'budget_spent_usd': 0.05}))

    def test_a_tiny_remainder_counts_as_exhausted(self):
        # $0.05 cap, $0.036 spent: $0.014 remaining — not enough for even a single call
        self.assertTrue(kosu_tavani_tukendi({'denials': RED, 'budget_cap_usd': 0.05, 'budget_spent_usd': 0.036}))

    def test_broken_numbers_never_crash_the_watchdog(self):
        for kotu in ({'budget_cap_usd': 'abc'}, {'budget_cap_usd': None}, {'budget_cap_usd': 0}):
            self.assertFalse(kosu_tavani_tukendi({'denials': RED, **kotu}))



class GercekKosuTests(unittest.TestCase):
    """17.09 AUDIT: the tests above measured the guard with HAND-PICKED numbers and never tried the real run that
    MOTIVATED the guard (20260917-121533). In that run the cap was $0.05 and $0.003625 was spent:
    both the "90% full" and the "remaining < $0.02" rules were wrong, the guard returned False and the run was retried twice
    more, burning 159 minutes. These tests close that hole."""

    # denial lines taken VERBATIM from run.json (denials[0], [3], [7])
    GERCEK_REDLER = [
        {'provider': 'claude', 'reason': 'run budget exhausted: reservation $1.25 > remaining $0.05 '
                                         '(cap $0.05, settled $0.00, held $0.00)'},
        {'provider': 'opencode-go', 'reason': 'run budget exhausted: reservation $0.53 > remaining $0.03 '
                                              '(cap $0.03, settled $0.00, held $0.00)'},
        {'provider': 'cerebras', 'reason': 'run budget exhausted: reservation $0.02 > remaining $0.02 '
                                           '(cap $0.03, settled $0.00, held $0.00)'},
    ]

    def test_the_run_that_motivated_the_guard_is_detected(self):
        gercek = {'denials': self.GERCEK_REDLER, 'budget_cap_usd': 0.05,
                  'budget_spent_usd': 0.003625, 'budget_held_usd': 0.0}
        self.assertTrue(kosu_tavani_tukendi(gercek),
                        'tavani asan istek reddedildiyse tavan buyuyemez: yeniden deneme ayni duvara carpar')

    def test_a_denial_that_fits_the_cap_stays_retryable(self):
        # The requested amount is BELOW the cap: it may have been denied because of another pending call at the time,
        # room opens when that call closes. This run must keep being retried.
        gecici = [{'provider': 'cerebras', 'reason': 'run budget exhausted: reservation $0.02 > remaining $0.01 '
                                                     '(cap $5.00, settled $0.10, held $4.89)'}]
        self.assertFalse(kosu_tavani_tukendi({'denials': gecici, 'budget_cap_usd': 5.0,
                                              'budget_spent_usd': 0.10, 'budget_held_usd': 0.0}))

    def test_held_money_counts_as_spent(self):
        # Pending money (outcome unknown) counts as spent at the gate; the guard must do the same calculation.
        self.assertTrue(kosu_tavani_tukendi({'denials': [{'reason': 'run budget exhausted: reservation $0.10 '
                                                                    '> remaining $0.00 (cap $5.00)'}],
                                             'budget_cap_usd': 5.0, 'budget_spent_usd': 0.2,
                                             'budget_held_usd': 4.6}))

    def test_the_denial_text_is_produced_by_the_real_gate(self):
        """Hand-writing the denial text is a tautology: the text is produced by the REAL gate, written to the run record and
        it is measured that the guard recognizes it. If the gate's f-string changes this test breaks — that is the intent."""
        import datetime
        import os
        import tempfile

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
        from butce_kapisi import BudgetDenied, BudgetLedger      # noqa: E402
        from kosu import RunStore                                # noqa: E402

        with tempfile.TemporaryDirectory() as t:
            ev = Path(t)
            eski = os.environ.get('CADRE_HOME')
            os.environ['CADRE_HOME'] = str(ev)
            try:
                store = RunStore(ev)
                kayit = store.accept('denetim', 'kucuk is')
                rid = kayit['run_id']
                store.update(rid, budget_cap_usd=0.05)
                gozlem = {'ts': datetime.datetime.now().isoformat(),
                          'bugun': datetime.date.today().isoformat(),
                          'go': {'db': 'ok', 'pay': 60.0, 'kullanilabilir': 60.0,
                                 'pencere_acik': True, 'birim': 'puan'}}
                defter = BudgetLedger(ev / 'zincir/request-budget.db')
                with self.assertRaises(BudgetDenied) as red:
                    defter.reserve('opencode-go', 1_250_000, gozlem, {'run_id': rid}, run_cap=50_000)
                kayit = store.read(rid)
                kayit['denials'] = [{'provider': 'opencode-go', 'reason': str(red.exception)}]
                self.assertIn('run budget exhausted', kayit['denials'][0]['reason'])
                self.assertTrue(kosu_tavani_tukendi(kayit),
                                f"bekci gercek kapinin red metnini tanimadi: {kayit['denials'][0]['reason']}")
            finally:
                os.environ.pop('CADRE_HOME', None) if eski is None else os.environ.__setitem__('CADRE_HOME', eski)

    def test_a_calibrated_denial_names_the_worst_case_amount_the_cap_was_compared_with(self):
        import datetime
        import os
        import re
        import tempfile
        from unittest.mock import patch

        from butce_kapisi import BudgetDenied, BudgetLedger      # noqa: E402
        from kosu import RunStore                                # noqa: E402

        with tempfile.TemporaryDirectory() as t:
            ev = Path(t)
            with patch.dict(os.environ, {'CADRE_HOME': str(ev)}), \
                    patch.object(BudgetLedger, 'kalibre', lambda self, con, provider, maximum: maximum // 10):
                store = RunStore(ev)
                rid = store.accept('denetim', 'kucuk is')['run_id']
                store.update(rid, budget_cap_usd=1.0)
                gozlem = {'ts': datetime.datetime.now().isoformat(), 'bugun': datetime.date.today().isoformat(),
                          'go': {'db': 'ok', 'pay': 60.0, 'kullanilabilir': 60.0, 'pencere_acik': True, 'birim': 'puan'}}
                with self.assertRaises(BudgetDenied) as red:
                    BudgetLedger(ev / 'zincir/request-budget.db').reserve('opencode-go', 1_250_000, gozlem, {'run_id': rid},
                                                                          run_cap=1_000_000)
                metin = str(red.exception)
                istenen, kalan = (float(x) for x in re.search(r'reservation \$([0-9.]+) > remaining \$([0-9.]+)', metin).groups())
                self.assertEqual((istenen, kalan), (1.25, 1.0), metin)
                self.assertIn('calibrated $0.12', metin)
                kayit = {**store.read(rid), 'denials': [{'provider': 'opencode-go', 'reason': metin}]}
                self.assertTrue(kosu_tavani_tukendi(kayit), metin)


class YurutucuTavanaBakarTests(unittest.TestCase):
    """Second live defect: the executor environment only looked at the PROVIDER daily share. In a run with a $0.05 run cap,
    since the provider shares were plentiful, sub-agents were assigned a CLOUD model; then every call hit the run
    cap with CADRE_BUDGET_DENIED and the whole lane failed. Now the remaining cap is checked too."""

    def test_an_exhausted_cap_closes_the_cloud_before_any_call(self):
        import os
        import tempfile
        import importlib
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as t:
            ev = Path(t)
            (ev / 'projects/p').mkdir(parents=True)
            (ev / 'projects/p/repo-path').write_text(str(ev))
            eski = {k: os.environ.get(k) for k in ('CADRE_HOME', 'CADRE_RUN_ID')}
            os.environ['CADRE_HOME'] = str(ev)
            try:
                import qe_core
                importlib.reload(qe_core)
                from kosu import RunStore
                store = RunStore(ev)
                rid = store.accept('p', 'is')['run_id']
                store.update(rid, budget_cap_usd=0.05)   # not an immutable field: may be updated after accept
                os.environ['CADRE_RUN_ID'] = rid
                butce = {'go': {'db': 'ok', 'pay': 60.0, 'kullanilabilir': 60.0, 'pencere_acik': True, 'birim': 'puan'}}
                with patch.object(qe_core, 'butce_json', return_value=butce), \
                     patch.object(qe_core, 'run_remaining_usd', return_value=0.001):
                    env = {}
                    # P3 (03.10): with the cap exhausted, BudgetDenied at the gate — no fall to local, no cloud call.
                    with self.assertRaises(qe_core.BudgetDenied):
                        qe_core.opencode_budget_env(env, ['-m', 'opencode-go/kimi-k2.7-code', 'gorev'])
            finally:
                for k, v in eski.items():
                    os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


class RolCebiUretimYolundaTests(unittest.TestCase):
    """B81 (17.09 second audit): the executor environment read the role from the OS environment, but the role is only written into the CHILD
    environment. Result: when the lanes used up their share, the judge and the helper fell to local WHILE THEIR OWN MONEY
    WAS STILL THERE. This test goes through the PRODUCTION PATH (rol_ortami -> opencode_budget_env), not the helper function
    — lesson 9c: if an invariant is read from two places, test it through the production path."""

    def test_the_reviewer_reaches_the_cloud_while_the_lanes_pocket_is_empty(self):
        import os
        import test_kosu_sessions as fixtures
        import butce_kapisi as butce
        import sozlesme
        import qe_core
        from unittest.mock import patch
        from kosu import RunStore
        from test_butce_senaryolari import anlik
        f = fixtures.SessionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        rid = RunStore(f.home).accept('b81', 'is')['run_id']
        RunStore(f.home).update(rid, budget_cap_usd=1.0)
        sozlesme.olustur(f.home, rid, None)
        anlik_veri = anlik(go={'db': 'ok', 'pay': 100.0, 'kullanilabilir': 100.0, 'pencere_acik': True})
        defter = butce.BudgetLedger(f.home / 'zincir/request-budget.db')
        tavan, dilim = butce.rol_butcesi(f.home, rid, 'impl')
        defter.reserve('opencode-go', tavan, anlik_veri, {'run_id': rid}, run_cap=tavan, dilim=dilim)

        eski = {k: os.environ.get(k) for k in ('CADRE_HOME', 'CADRE_RUN_ID', 'CADRE_ROL')}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in eski.items()])
        os.environ['CADRE_HOME'] = str(f.home)
        os.environ['CADRE_RUN_ID'] = rid
        os.environ.pop('CADRE_ROL', None)          # there is NO role in the engine's own environment — the real situation

        secilen = {}
        with patch.object(qe_core, 'butce_json', return_value=anlik_veri):
            for rol in ('kalite', 'review'):   # 4c-10: compaction from the lane's pocket; review uses the helper pocket
                env = qe_core.rol_ortami(dict(os.environ), rol)
                args = qe_core.opencode_budget_env(env, ['-m', 'opencode-go/kimi-k2.7-code', 'gorev'])
                secilen[rol] = args[args.index('-m') + 1]
            # P3 (03.10): when the lane pocket runs out, BudgetDenied at the gate — no fall to local, it waits.
            env = qe_core.rol_ortami(dict(os.environ), 'impl')
            with self.assertRaises(qe_core.BudgetDenied):
                qe_core.opencode_budget_env(env, ['-m', 'opencode-go/kimi-k2.7-code', 'gorev'])

        self.assertFalse(secilen['kalite'].startswith('lmstudio/'),
                         'hakem kendi cebi doluyken buluta cikabilmeli')
        self.assertFalse(secilen['review'].startswith('lmstudio/'),
                         'yardimci kendi cebi doluyken buluta cikabilmeli')


class HayaletAskidaTests(unittest.TestCase):
    """B84: if the coordinator dies mid-call the reservation stays pending; `kosu_tavani_tukendi` counts it as
    spent and the revival fails permanently with "cap full, raise the cap". But the pending amount is a safe
    UPPER BOUND (measured: $2.78 reserved -> $0.32 receipt), so the money may not have been spent. The right tool
    is `cadre kurtar`, not raising the cap."""

    def kayit(self, tavan, kesin, askida):
        return {'budget_cap_usd': tavan, 'budget_spent_usd': kesin, 'budget_held_usd': askida,
                'denials': [{'reason': 'run budget exhausted: reservation $0.01 > remaining $0.00'}]}

    def test_a_cap_that_is_full_only_because_of_holds_is_reported_as_such(self):
        from kosu import tavan_hayalet_askidan_mi
        # $5 cap, real spending $0.32, pending $4.50 -> the cap looks FULL but the money was not spent
        self.assertTrue(kosu_tavani_tukendi(self.kayit(5.0, 0.32, 4.50)))
        self.assertTrue(tavan_hayalet_askidan_mi(self.kayit(5.0, 0.32, 4.50)))

    def test_a_genuinely_spent_cap_is_not_blamed_on_holds(self):
        from kosu import tavan_hayalet_askidan_mi
        self.assertTrue(kosu_tavani_tukendi(self.kayit(5.0, 4.90, 0.0)))
        self.assertFalse(tavan_hayalet_askidan_mi(self.kayit(5.0, 4.90, 0.0)))
        self.assertFalse(tavan_hayalet_askidan_mi(self.kayit(5.0, 4.60, 0.30)),
                         'kesinlesmis harcama tek basina tavani dolduruyorsa suc askidakinin degil')

    def _revive_nedeni(self, tavan, harcanan, askida):
        """Really RUNS the guard and reads the `reason` text written to the run record (not a source scan)."""
        import tempfile
        from kosu import RunStore, revive
        with tempfile.TemporaryDirectory(prefix='qe-tavan-') as t:
            ev = Path(t)
            store = RunStore(ev)
            rid = store.accept('tavan', 'is')['run_id']
            store.update(rid, state='retryable', owner_pid=None, budget_cap_usd=tavan,
                         budget_spent_usd=harcanan, budget_held_usd=askida,
                         denials=[{'provider': 'claude', 'reason': 'run budget exhausted'}])
            revive(store, ev)
            kayit = store.read(rid)
        return kayit['state'], kayit.get('reason') or ''

    def test_a_cap_full_only_of_holds_sends_the_operator_to_recovery(self):
        """Wrong advice costs money: saying 'raise the cap' sets aside unspent money a second time.
        In the ghost-pending state the guard must point the operator to `cadre kurtar`."""
        durum, neden = self._revive_nedeni(5.0, 0.32, 4.50)
        self.assertEqual(durum, 'failed', 'tavani dolu kosu hizli dusmeli, tekrar denenmemeli')
        self.assertIn('cadre kurtar', neden, f'askidakileri incelemeye yonlendirmeli: {neden!r}')
        self.assertNotIn('CADRE_KOSU_TAVAN_USD', neden, 'askida dolulukta tavan buyutme tavsiyesi verilmemeli')

    def test_a_genuinely_spent_cap_still_tells_the_operator_to_raise_it(self):
        durum, neden = self._revive_nedeni(5.0, 4.90, 0.0)
        self.assertEqual(durum, 'failed')
        self.assertIn('CADRE_KOSU_TAVAN_USD', neden, f'gercek doluluk eski tavsiyeyi korumali: {neden!r}')
        self.assertNotIn('cadre kurtar', neden)


if __name__ == "__main__":
    unittest.main()
