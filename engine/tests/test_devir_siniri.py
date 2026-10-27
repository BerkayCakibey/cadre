"""Item 2 — hand-off limit. The only expensive event measured on 16-17.09 was a LOOP: the same job was handed from provider to provider and
restarted from scratch on every hand-off, burning $1.15 (the cost of 22 medium jobs). After 2 hand-offs per lane and 5 per run the job is NOT
PASSED AROUND THE CLOUD, it is finished locally. The counter is written to disk: it is not reset even if the engine crashes and restarts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))


class DevirSayaciTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-devir-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.eski = {k: os.environ.get(k) for k in ('CADRE_HOME', 'CADRE_RUN_ID', 'CADRE_GOREV_ID')}
        os.environ.update(CADRE_HOME=str(self.home), CADRE_RUN_ID='r1', CADRE_GOREV_ID='g1')
        self.addCleanup(self.geri_al)
        import importlib
        import qe_core
        importlib.reload(qe_core)
        self.core = qe_core

    def geri_al(self):
        for k, v in self.eski.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_counts_are_per_lane_and_per_run_and_survive_a_restart(self):
        self.assertEqual(self.core.devir_sayaci(), (0, 0))
        self.assertEqual(self.core.devir_sayaci(artir=True), (1, 1))
        self.assertEqual(self.core.devir_sayaci(artir=True), (2, 2))
        os.environ['CADRE_GOREV_ID'] = 'g2'
        self.assertEqual(self.core.devir_sayaci(artir=True), (1, 3))   # a new lane from scratch, the run total grows
        # read from disk: behave like a new process
        veri = json.loads((self.home / 'runs/r1/devir.json').read_text())
        self.assertEqual(veri, {'seritler': {'g1': 2, 'g2': 1}, 'kosu': 3})

    def test_the_counter_is_written_durably_and_leaves_no_half_file(self):
        """B110 (18.09): the counter was written with `write_text` + `replace`, without fsync — on a crash/outage an empty
        or half file could remain and the hand-off cap was reset. That is exactly why the counter exists."""
        from unittest.mock import patch
        import kosu
        with patch.object(kosu, 'atomic_json', wraps=kosu.atomic_json) as yazici:
            self.core.devir_sayaci(artir=True)
        yazici.assert_called_once()                                   # the durable writer was used (fsync + directory fsync)
        klasor = self.home / 'runs/r1'
        self.assertEqual([p.name for p in klasor.glob('*.tmp')], [], 'yarim/gecici dosya birakilmaz')
        self.assertEqual(json.loads((klasor / 'devir.json').read_text())['kosu'], 1)

    def test_no_run_id_means_no_counting_and_no_files(self):
        os.environ.pop('CADRE_RUN_ID')
        self.assertEqual(self.core.devir_sayaci(artir=True), (0, 0))
        self.assertFalse((self.home / 'runs').exists())

    def test_handover_stops_in_budget_wait_once_the_lane_limit_is_passed(self):
        kayitlar = []
        zincir = ['deepseek/deepseek-v4-flash', 'cerebras/gpt-oss-120b', 'google/gemini-3-flash-preview']
        with patch.object(self.core.YON, 'zincir', return_value=(zincir, {})), \
             patch.object(self.core.YON, 'ariza'), patch.object(self.core, 'istek_olcusu', return_value=8000), \
             patch.object(self.core, 'log', side_effect=lambda f, m: kayitlar.append(m)):
            self.assertEqual(self.core.kota_dusur('openrouter/openai/gpt-6.1-sol', None), 'deepseek/deepseek-v4-flash')
            self.assertEqual(self.core.kota_dusur('openrouter/openai/gpt-6.1-sol', None), 'deepseek/deepseek-v4-flash')
            # the third hand-off exceeds the lane's 2 cap -> budget wait (P3)
            self.assertEqual(self.core.kota_dusur('openrouter/openai/gpt-6.1-sol', None), '')
        self.assertTrue(any('DEVIR SINIRI' in m for m in kayitlar), kayitlar)

    def test_the_run_limit_stops_a_job_that_hops_across_many_lanes(self):
        kayitlar = []
        zincir = ['deepseek/deepseek-v4-flash']
        with patch.object(self.core.YON, 'zincir', return_value=(zincir, {})), \
             patch.object(self.core.YON, 'ariza'), patch.object(self.core, 'istek_olcusu', return_value=8000), \
             patch.object(self.core, 'log', side_effect=lambda f, m: kayitlar.append(m)):
            for i in range(5):                       # a DIFFERENT lane each time: the lane cap never fills
                os.environ['CADRE_GOREV_ID'] = f'g{i}'
                self.assertEqual(self.core.kota_dusur('openrouter/openai/gpt-6.1-sol', None), 'deepseek/deepseek-v4-flash')
            os.environ['CADRE_GOREV_ID'] = 'g9'
            self.assertEqual(self.core.kota_dusur('openrouter/openai/gpt-6.1-sol', None), '')
        # 4c-17: a denied hand-off does not grow the counter (checked first, then incremented)
        self.assertTrue(any('DEVIR SINIRI' in m and 'kosu 5/5' in m for m in kayitlar), kayitlar)


if __name__ == '__main__':
    unittest.main()
