"""Item 3 — the judge guarantee.

Three legs: (a) 15% of the run cap is reserved for the judge, the lanes cannot leave it hungry; (b) the local judge can also decide
with the line-based contract (the 27B model does not reliably produce strict JSON and it was always the last link of the chain);
(c) without the judge producing a valid verdict the run cannot PASS."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import butce_kapisi as budget  # noqa: E402
from sonuc import decision, parse_review  # noqa: E402
from test_butce_donemi import snapshot  # noqa: E402
import test_kosu_sessions as fixtures  # noqa: E402


class HakemPayiTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.ledger = budget.BudgetLedger(self.f.home / 'zincir/request-budget.db')
        self.eski = os.environ.get('CADRE_ROL')
        self.addCleanup(lambda: os.environ.__setitem__('CADRE_ROL', self.eski)
                        if self.eski else os.environ.pop('CADRE_ROL', None))

    def kosu(self, cap_usd):
        from kosu import RunStore
        store = RunStore(self.f.home)
        r = store.accept('hakem', 'test gorevi')
        store.update(r['run_id'], budget_cap_usd=cap_usd)
        return r['run_id']

    def test_the_reviewer_share_is_reserved_and_never_eats_more_than_half(self):
        self.assertEqual(budget.hakem_rezervi(5_000_000), 750_000)     # %15
        self.assertEqual(budget.hakem_rezervi(1_000_000), 200_000)     # taban $0.20
        self.assertEqual(budget.hakem_rezervi(300_000), 150_000)       # the half limit
        self.assertEqual(budget.hakem_rezervi(0), 0)

    def test_the_reviewer_pocket_follows_the_contract_share_not_a_fixed_fifteen_percent(self):
        """B110 (18.09): the lane and helper pockets came from the contract but the judge pocket was a FIXED 15%;
        even if the contract gave the judge a larger share the pocket did not change, and the pockets did not add up to the cap."""
        import sozlesme
        rid = self.kosu(5.0)
        sozlesme.olustur(self.f.home, rid, paylar={'seritler': 0.55, 'hakem': 0.25, 'yardimci': 0.20})
        self.assertEqual(budget.rol_tavani(self.f.home, rid, 'kalite'), 1_250_000)      # %25
        self.assertEqual(budget.rol_tavani(self.f.home, rid, 'review'), 1_000_000)  # %20
        self.assertEqual(budget.rol_tavani(self.f.home, rid, 'impl'), 2_750_000)        # artan
        toplam = sum(budget.rol_tavani(self.f.home, rid, r) for r in ('kalite', 'review', 'impl'))
        self.assertLessEqual(toplam, 5_000_000, 'ceplerin toplami tavani asamaz')

    def test_a_lane_cap_ban_does_not_close_the_provider_for_the_reviewer(self):
        """B110 (18.09): the run cap ban carried no POCKET information; a ban written when the lanes' pocket ran out
        also closed the judge — the exact opposite of the hard split (each role only its own pocket)."""
        from unittest.mock import patch
        rid = self.kosu(5.0)
        with patch.dict(os.environ, {'CADRE_RUN_ID': rid, 'CADRE_GOREV_ID': 'g1'}):
            yasaklar = budget.task_bans(self.f.home)
            yasaklar.block('opencode-go', 'budget', snapshot(), dilim='seritler')
            self.assertEqual(budget.available_budget(self.f.home, snapshot(), 'opencode-go', 'impl', rid), 0)
            self.assertGreater(budget.available_budget(self.f.home, snapshot(), 'opencode-go', 'kalite', rid), 0,
                               'hakemin kendi cebi dururken serit yasagi onu kapatmamali')
        with patch.dict(os.environ, {'CADRE_RUN_ID': rid, 'CADRE_GOREV_ID': 'g2'}):
            budget.task_bans(self.f.home).block('opencode-go', 'error', snapshot())   # a ban without a pocket binds EVERYONE
            for rol in ('impl', 'kalite'):
                self.assertEqual(budget.available_budget(self.f.home, snapshot(), 'opencode-go', rol, rid), 0, rol)

    def test_every_role_sees_only_its_own_pocket(self):
        # 17.09 HARD SPLIT: the judge no longer SEES the whole cap — it sees its own pocket. The old state (judge =
        # total) created a dependence on call order: the role called first ate the other's share.
        rid = self.kosu(1.0)
        self.assertEqual(budget.rol_tavani(self.f.home, rid, 'kalite'), 200_000)     # 15% -> the $0.20 floor
        self.assertEqual(budget.rol_tavani(self.f.home, rid, 'impl'), 700_000)       # artan
        self.assertEqual(budget.rol_tavani(self.f.home, rid, 'review'), 100_000) # helper 10%
        os.environ['CADRE_ROL'] = 'kalite'                              # the role is also read from the environment
        self.assertEqual(budget.rol_tavani(self.f.home, rid), 200_000)
        os.environ['CADRE_ROL'] = 'impl'
        self.assertEqual(budget.rol_tavani(self.f.home, rid), 700_000)
        # The pockets add up to NO MORE than the cap: nobody can eat anybody else's share.
        self.assertLessEqual(700_000 + 200_000 + 100_000, 1_000_000)

    def test_a_lane_that_burns_its_cap_cannot_starve_the_reviewer(self):
        rid = self.kosu(1.0)
        # fill the lane cap
        self.ledger.reserve('opencode-go', 700_000, snapshot(5.0), {"run_id": rid},
                            *budget.rol_butcesi(self.f.home, rid, 'impl'))
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('opencode-go', 100_000, snapshot(5.0), {"run_id": rid},
                                *budget.rol_butcesi(self.f.home, rid, 'impl'))
        # the judge can still work: its own $0.20 pocket is not full
        tavan, dilim = budget.rol_butcesi(self.f.home, rid, 'kalite')
        ident = self.ledger.reserve('opencode-go', 200_000, snapshot(5.0), {"run_id": rid},
                                    run_cap=tavan, dilim=dilim)
        self.assertTrue(ident)

    def test_the_applied_cap_and_the_counted_pocket_come_from_one_decision(self):
        """B79/B80: if `run_cap` comes from one role and `metadata.dilim` from somewhere else the hard split collapses —
        the judge's $0.20 cap is applied but its spending is charged to the LANE pocket and the judge is denied."""
        rid = self.kosu(1.0)
        for rol, beklenen in (('kalite', 'hakem'), ('review', 'yardimci'),
                              ('cadre-review', 'yardimci'), ('impl', 'seritler'), (None, 'seritler')):
            os.environ.pop('CADRE_ROL', None)
            tavan, dilim = budget.rol_butcesi(self.f.home, rid, rol)
            self.assertEqual(dilim, beklenen, rol)
            self.assertEqual(tavan, budget.rol_tavani(self.f.home, rid, rol), rol)
        # the slice stamp is written to the metadata inside reserve — the caller cannot forget it
        tavan, dilim = budget.rol_butcesi(self.f.home, rid, 'kalite')
        self.ledger.reserve('opencode-go', 150_000, snapshot(5.0), {"run_id": rid}, run_cap=tavan, dilim=dilim)
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.f.home / 'zincir/request-budget.db')) as con:
            kesin, askida = budget.run_usage(con, rid, 'hakem')
        self.assertEqual(kesin + askida, 150_000)


class YerelHakemSozlesmesiTests(unittest.TestCase):
    EXP = {'digest': 'abc123def4567890'}

    def test_the_line_contract_is_accepted_with_the_same_strictness_as_json(self):
        metin = ("Inceledim.\nREVIZYON: abc123def4567890\nKARAR: GECER\n"
                 "KRITER m1: PASS - 12/12 test yesil\nKRITER m2: PASS - dosya olustu\n")
        r = parse_review(metin, self.EXP, ['m1', 'm2'])
        self.assertEqual(r['status'], 'PASS')
        self.assertEqual(r['format'], 'satir')
        self.assertEqual([k['id'] for k in r['acceptance']], ['m1', 'm2'])

    def test_a_missing_criterion_or_evidence_is_refused(self):
        eksik = "REVIZYON: abc123def4567890\nKARAR: GECER\nKRITER m1: PASS - kanit\n"
        self.assertEqual(parse_review(eksik, self.EXP, ['m1', 'm2'])['status'], 'ERROR')
        kanitsiz = "REVIZYON: abc123def4567890\nKARAR: GECER\nKRITER m1: PASS - \nKRITER m2: PASS - var\n"
        self.assertEqual(parse_review(kanitsiz, self.EXP, ['m1', 'm2'])['status'], 'ERROR')
        yanlis_rev = "REVIZYON: ffffffffffff\nKARAR: GECER\nKRITER m1: PASS - kanit\n"
        self.assertEqual(parse_review(yanlis_rev, self.EXP, ['m1'])['status'], 'ERROR')
        kararsiz = "REVIZYON: abc123def4567890\nKRITER m1: PASS - kanit\n"
        self.assertEqual(parse_review(kararsiz, self.EXP, ['m1'])['status'], 'ERROR')

    def test_a_duzelt_verdict_is_a_fail_not_an_error(self):
        metin = "REVIZYON: abc123def4567890\nKARAR: DUZELT\nKRITER m1: FAIL - test yok\n"
        r = parse_review(metin, self.EXP, ['m1'])
        self.assertEqual(r['status'], 'FAIL')
        self.assertEqual(r['verdict'], 'DUZELT')

    def test_short_or_repeated_revision_and_verdict_are_rejected(self):
        valid = 'REVIZYON: abc123def4567890\nKARAR: GECER\nKRITER m1: PASS - kanit\n'
        for raw in (valid.replace('abc123def4567890', 'abc123'),
                    valid + 'KARAR: DUZELT\n', valid + 'KARAR: GECER\n',
                    valid + 'REVIZYON: abc123def4567890\n', valid + 'REVIZYON: ffffff\n'):
            with self.subTest(raw=raw):
                self.assertEqual(parse_review(raw, self.EXP, ['m1'])['status'], 'ERROR')

    def test_json_still_wins_when_present(self):
        govde = json.dumps({'revision': self.EXP['digest'], 'verdict': 'GECER',
                            'acceptance': [{'id': 'm1', 'status': 'PASS', 'evidence': 'kanit'}]})
        r = parse_review(govde, self.EXP, ['m1'])
        self.assertEqual(r['status'], 'PASS')
        self.assertNotIn('format', r)


class HakemsizGecilemezTests(unittest.TestCase):
    def test_a_run_cannot_pass_without_a_working_reviewer(self):
        rev = {'digest': 'd1'}
        qa = {'status': 'PASS', 'revision': rev, 'discovery': {'status': 'PASS', 'scope': 'full'}}
        for hakem in ({'status': 'ERROR', 'reason': 'hakem zinciri tukendi', 'revision': rev},
                      {'status': 'SKIPPED', 'reason': 'qa-not-passed', 'revision': rev},
                      {'status': 'FAIL', 'revision': rev}):
            self.assertEqual(decision(qa, hakem, rev)['status'], 'FAIL', hakem)
        for stamp in (False, None, 'true', 1):
            self.assertEqual(decision(qa, {'status': 'PASS', 'revision': rev, 'independent': stamp}, rev)['status'], 'FAIL')
        self.assertEqual(decision(qa, {'status': 'PASS', 'revision': rev, 'independent': True}, rev)['status'], 'PASS')


if __name__ == '__main__':
    unittest.main()
