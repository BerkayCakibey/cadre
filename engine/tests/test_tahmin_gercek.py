"""§40.3 — the gate that replaces shadow mode: estimate ↔ actual.

Critical rule: the estimate is NEVER INVENTED. Today there is no history to produce an honest estimate; the module says so explicitly
and the gate starts working by itself as measurements accumulate. The unit is cost and time PER LANE,
because runs come in different sizes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import sozlesme  # noqa: E402
import tahmin_gercek as tg  # noqa: E402
from kosu import RunStore  # noqa: E402


class TahminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-tg-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'projects/p').mkdir(parents=True)
        (self.home / 'projects/p/repo-path').write_text(str(self.home))
        self.store = RunStore(self.home)

    def kosu(self, harcama, dakika, serit=1, state='completed'):
        r = self.store.accept('p', 'is')
        rid = r['run_id']
        self.store.update(rid, state=state, budget_spent_usd=harcama,
                          created=time.time() - dakika * 60, updated=time.time())
        sozlesme.olustur(self.home, rid, {'gorevler': [{'id': f'g{i}'} for i in range(serit)]})
        return rid

    def test_no_prediction_is_invented_before_there_is_history(self):
        rid = self.kosu(0.4, 30)
        s = tg.karsilastir(self.home, rid)
        self.assertIsNone(s['tahmin'])
        self.assertIn('tahmin yok', s['kapi'])
        self.assertEqual(s['gercek_usd'], 0.4)

    def test_the_prediction_appears_once_enough_runs_exist(self):
        for _ in range(3):
            self.kosu(0.30, 20, serit=2)
        yeni = self.kosu(0.32, 21, serit=2)
        s = tg.karsilastir(self.home, yeni)
        self.assertIsNotNone(s['tahmin'])
        self.assertEqual(s['tahmin']['ornek'], 3)
        self.assertAlmostEqual(s['tahmin']['usd'], 0.30, places=2)
        self.assertAlmostEqual(s['tahmin']['dk'], 20, delta=1)
        self.assertEqual(s['kapi'], 'tamam')

    def test_a_run_that_costs_far_more_than_history_trips_the_gate(self):
        for _ in range(3):
            self.kosu(0.20, 20)
        pahali = self.kosu(1.00, 20)
        s = tg.karsilastir(self.home, pahali)
        self.assertIn('SAPMA', s['kapi'])
        self.assertIn('maliyet', s['kapi'])
        self.assertGreater(s['sapma_usd'], 0.5)

    def test_lane_count_makes_runs_of_different_sizes_comparable(self):
        for _ in range(3):
            self.kosu(0.20, 10, serit=1)
        buyuk = self.kosu(0.80, 40, serit=4)          # the same cost per lane
        s = tg.karsilastir(self.home, buyuk)
        self.assertEqual(s['serit'], 4)
        self.assertAlmostEqual(s['tahmin']['usd'], 0.80, places=2)
        self.assertEqual(s['kapi'], 'tamam', 'serit basina ayni maliyet sapma sayilmamali')

    def test_unfinished_runs_never_enter_the_history(self):
        for durum in ('failed', 'cancelled', 'running'):
            self.kosu(5.0, 60, state=durum)
        self.assertEqual(tg.tamamlanan_kosular(self.home), [])
        self.assertIsNone(tg.tahmin(self.home, 1))

    def test_the_summary_line_is_empty_when_there_is_nothing_to_compare(self):
        self.assertEqual(tg.ozet(self.home), '')
        self.kosu(0.1, 5)
        self.assertIn('tahmin yok', tg.ozet(self.home))


if __name__ == '__main__':
    unittest.main()
