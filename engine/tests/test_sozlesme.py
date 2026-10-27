"""Item 9 — the task contract. The user's request: AT plan time write a document that splits the budget
across agents/compaction/judge, and make the system FOLLOW it. Until now the split was buried in the code
and not visible per run; there was no single answer to "who may spend how much"."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import unittest

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import butce_kapisi as budget  # noqa: E402
import sozlesme  # noqa: E402
import test_kosu_sessions as fixtures  # noqa: E402
from test_butce_donemi import snapshot  # noqa: E402


class SozlesmeTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.home = self.f.home
        from kosu import RunStore
        self.store = RunStore(self.home)
        r = self.store.accept('sozlesme', 'test gorevi')
        self.rid = r['run_id']
        self.store.update(self.rid, budget_cap_usd=5.0)

    def test_the_contract_is_written_once_and_never_changes(self):
        plan = {'gorevler': [{'id': 'g1', 'baslik': 'ilk', 'model': 'opencode-go/glm-5.2', 'zorluk': 3},
                             {'id': 'g2', 'baslik': 'ikinci'}]}
        ilk = sozlesme.olustur(self.home, self.rid, plan)
        self.assertEqual(ilk['paylar'], sozlesme.VARSAYILAN_PAYLAR)
        self.assertEqual([s['id'] for s in ilk['seritler']], ['g1', 'g2'])
        self.assertEqual(ilk['hakem'], {'tur': 1, 'revizyon': 1})
        self.assertEqual(ilk['kaynak'], 'plan')
        # even if a second call comes with different shares it does NOT CHANGE the existing contract
        ikinci = sozlesme.olustur(self.home, self.rid, {'gorevler': []},
                                  paylar={'seritler': 0.9, 'hakem': 0.05, 'yardimci': 0.05})
        self.assertEqual(ikinci['olusma'], ilk['olusma'])
        self.assertEqual(ikinci['paylar'], sozlesme.VARSAYILAN_PAYLAR)

    def istek_paylari_yaz(self, paylar):
        from kosu import atomic_json
        kayit = self.store.read(self.rid)
        kayit['request'] = {'budget_shares': paylar, 'duration_minutes': 45, 'budget_usd': '5.0'}
        atomic_json(self.store.path(self.rid) / 'run.json', kayit)

    def test_request_shares_apply_before_the_contract_and_are_written_into_it(self):
        """CR-08: the proposal gate (without a contract) and the run contract use the same request shares; the total stays fixed."""
        self.istek_paylari_yaz({'seritler': 0.71, 'hakem': 0.17, 'yardimci': 0.12})
        self.assertEqual(sozlesme.tabanlar(self.home, self.rid), {'seritler': 0.71, 'hakem': 0.17, 'yardimci': 0.12})
        cepler = {d: budget.rol_tavani(self.home, self.rid, dilim_adi=d) for d in ('seritler', 'hakem', 'yardimci')}
        self.assertEqual(cepler, {'seritler': 3_550_000, 'hakem': 850_000, 'yardimci': 600_000})
        self.assertEqual(sum(cepler.values()), 5_000_000)
        s = sozlesme.olustur(self.home, self.rid, {'gorevler': [{'id': 'g1', 'baslik': 'tek'}]})
        self.assertEqual(s['paylar'], {'seritler': 0.71, 'hakem': 0.17, 'yardimci': 0.12})
        self.assertEqual(sozlesme.tabanlar(self.home, self.rid), s['paylar'])
        self.assertEqual(budget.rol_tavani(self.home, self.rid, dilim_adi='yardimci'), 600_000)

    def test_invalid_request_shares_are_ignored_before_and_in_the_contract(self):
        self.istek_paylari_yaz({'seritler': 0.9, 'hakem': 0.2, 'yardimci': 0.1})
        self.assertEqual(sozlesme.tabanlar(self.home, self.rid), sozlesme.VARSAYILAN_PAYLAR)
        self.assertEqual(sozlesme.olustur(self.home, self.rid, {'gorevler': []})['paylar'], sozlesme.VARSAYILAN_PAYLAR)

    def test_invalid_shares_fall_back_to_the_defaults(self):
        for kotu in ({'seritler': 0.5, 'hakem': 0.2}, {'seritler': 0.5, 'hakem': 0.2, 'yardimci': 0.5},
                     {'seritler': 1.0, 'hakem': 0.0, 'yardimci': 0.0}, 'metin', None):
            self.assertFalse(sozlesme.paylar_gecerli(kotu), kotu)
        s = sozlesme.olustur(self.home, self.rid, None, paylar={'seritler': 2, 'hakem': -1, 'yardimci': 0})
        self.assertEqual(s['paylar'], sozlesme.VARSAYILAN_PAYLAR)

    def test_a_corrupt_contract_is_ignored_rather_than_obeyed(self):
        sozlesme.yol(self.home, self.rid).write_text('{bozuk')
        self.assertIsNone(sozlesme.oku(self.home, self.rid))
        self.assertEqual(sozlesme.pay_orani(self.home, self.rid, 'impl'), 0.75)
        sozlesme.yol(self.home, self.rid).write_text(json.dumps({'version': 99, 'paylar': sozlesme.VARSAYILAN_PAYLAR}))
        self.assertIsNone(sozlesme.oku(self.home, self.rid))

    def test_every_role_maps_to_exactly_one_slice(self):
        self.assertEqual(sozlesme.dilim('kalite'), 'hakem')
        self.assertEqual(sozlesme.dilim('review'), 'yardimci')
        self.assertEqual(sozlesme.dilim('cadre-review'), 'yardimci')
        for rol in ('impl', 'plan', 'compaction', '', None, 'bilinmeyen'):   # 4c-10: compaction from the lane's pocket
            self.assertEqual(sozlesme.dilim(rol), 'seritler', rol)


class SozlesmeyeUymaTests(unittest.TestCase):
    """The contract is not just written, the gate FOLLOWS it."""

    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.home = self.f.home
        from kosu import RunStore
        self.store = RunStore(self.home)
        from unittest.mock import patch
        with patch.dict(os.environ, {'CADRE_KOSU_TAVAN_USD': '5'}):
            self.rid = self.store.accept('sozlesme', 'test')['run_id']
        self.store.update(self.rid, budget_cap_usd=5.0)
        self.ledger = budget.BudgetLedger(self.home / 'zincir/request-budget.db')
        self.eski = os.environ.get('CADRE_ROL')
        self.addCleanup(lambda: os.environ.__setitem__('CADRE_ROL', self.eski)
                        if self.eski else os.environ.pop('CADRE_ROL', None))

    def test_each_slice_sees_its_own_ceiling(self):
        sozlesme.olustur(self.home, self.rid, None)
        # 17.09 HARD SPLIT: no bands, POCKETS. Each role sees only its own share and fills it with its own spending.
        # with a $5 cap: lane $3.75 · helper $0.50 · judge $0.75. They add up to the full cap.
        self.assertEqual(budget.rol_tavani(self.home, self.rid, 'impl'), 3_750_000)
        self.assertEqual(budget.rol_tavani(self.home, self.rid, 'review'), 500_000)
        self.assertEqual(budget.rol_tavani(self.home, self.rid, 'cadre-review'), 500_000)
        self.assertEqual(budget.rol_tavani(self.home, self.rid, 'kalite'), 750_000,
                         'hakem kendi cebini gorur; artan ancak SERITLER BITINCE devreder')
        self.assertEqual(3_750_000 + 500_000 + 750_000, 5_000_000)

    def test_lanes_can_never_enter_the_helper_pocket(self):
        """What the guarantee means: the LANES cannot enter the helper's pocket — even if they use up the whole lane share
        the helper passes from its own pocket. With the hard split this is independent of call ORDER."""
        sozlesme.olustur(self.home, self.rid, None)
        serit, serit_dilim = budget.rol_butcesi(self.home, self.rid, 'impl')
        self.ledger.reserve('opencode-go', serit, snapshot(5.0), {'run_id': self.rid},
                            run_cap=serit, dilim=serit_dilim)
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('opencode-go', 1000, snapshot(5.0), {'run_id': self.rid},
                                run_cap=serit, dilim=serit_dilim)
        # the helper passes from its own pocket
        tavan, dilim = budget.rol_butcesi(self.home, self.rid, 'review')
        self.ledger.reserve('opencode-go', 400_000, snapshot(5.0), {'run_id': self.rid},
                            run_cap=tavan, dilim=dilim)

    def test_the_role_comes_from_the_environment_when_not_given(self):
        sozlesme.olustur(self.home, self.rid, None)
        os.environ['CADRE_ROL'] = 'review'
        self.assertEqual(budget.rol_tavani(self.home, self.rid), 500_000)
        self.assertEqual(budget.rol_butcesi(self.home, self.rid)[1], 'yardimci')
        os.environ['CADRE_ROL'] = 'kalite'
        self.assertEqual(budget.rol_tavani(self.home, self.rid), 750_000)
        self.assertEqual(budget.rol_butcesi(self.home, self.rid)[1], 'hakem')

    def test_without_a_contract_the_defaults_still_apply(self):
        self.assertIsNone(sozlesme.oku(self.home, self.rid))
        self.assertEqual(budget.rol_tavani(self.home, self.rid, 'impl'), 3_750_000)
        self.assertEqual(budget.rol_tavani(self.home, self.rid, 'review'), 500_000)


class HakemTuruTests(unittest.TestCase):
    """The contract also binds the judge round count: one review + one revision. The code now reads this from the contract."""

    def test_the_default_contract_means_one_review_and_one_revision(self):
        self.assertEqual(sozlesme.VARSAYILAN_PAYLAR['hakem'], 0.15)
        import tempfile
        from kosu import RunStore
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            (home / 'projects/p').mkdir(parents=True)
            (home / 'projects/p/repo-path').write_text(str(home))
            rid = RunStore(home).accept('p', 'is')['run_id']
            s = sozlesme.olustur(home, rid, None)
            self.assertEqual(s['hakem']['tur'] + s['hakem']['revizyon'], 2)


if __name__ == '__main__':
    unittest.main()


class BirlestirmeCebiTest(unittest.TestCase):
    def test_birlestirme_seritler_bitince_artan_cepten_harcar(self):
        """03.10 02:37 live: when all lanes were terminal the remaining share was handed to the judge (B82) and the lane pocket froze at what was spent;
        since the merge spent from the LANE pocket it ran out of money on every run and fell to local. The merge runs after the lanes."""
        import sozlesme
        self.assertEqual(sozlesme.gorev_dilimi('', 'birlestirme'), 'hakem')
        self.assertEqual(sozlesme.gorev_dilimi('', 'g1'), 'seritler')
        # B335: QA repair (the rescuer role) runs in the acceptance task; the frozen lane pocket closed all paid ones
        self.assertEqual(sozlesme.gorev_dilimi('kurtarici', 'kalite'), 'hakem')
        self.assertEqual(sozlesme.gorev_dilimi('kurtarici', 'qa'), 'hakem')
        self.assertEqual(sozlesme.gorev_dilimi('kurtarici', 'g2'), 'seritler')
