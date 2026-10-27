"""CR-39 (N-RT01, N-RT02): outcome measurement after a real acceptance decision; measurement does not change the selection; a repeat is one sample."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import qe_core as core
import model_secimi
from model_secimi import politika_sirasi, sonuc_kaydet, sonuc_ozeti
from test_sonuc import CompletionTests


class SonucOlcumTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix='sonuc-'))
        (self.home / 'zincir').mkdir()
        self.record = {'run_id': 'r1', 'created': time.time() - 90, 'source_sha': 'abc',
                       'model_selection': {'policy_version': 'difficulty-preference-role-v1', 'preference': 'balanced',
                                           'difficulty_estimate': 'medium', 'difficulty_override': None, 'difficulty_effective': 'medium',
                                           'estimate_score': 30, 'estimate_source': 'request-rules', 'estimate_reason': 'kural_tabani=30',
                                           'prompt_sha256': '0' * 64}}

    def test_same_run_product_and_verdict_is_one_sample_and_cost_is_unknown_without_ledger(self):
        result = {'status': 'PASS', 'revision': {'digest': 'd1'}}
        first = sonuc_kaydet(self.home, self.record, result, ['opencode-go/glm-5.3', 'opencode-go/glm-5.3'], 'opencode/glm-5.3', 'claude/opus')
        self.assertIsNotNone(first)
        self.assertIsNone(sonuc_kaydet(self.home, self.record, result, ['opencode-go/glm-5.3'], 'opencode/glm-5.3', None))
        self.assertIsNotNone(sonuc_kaydet(self.home, self.record, {'status': 'FAIL', 'revision': {'digest': 'd1'}, 'reason': 'qa-not-passed'}, [], None, None))
        rows = [json.loads(l) for l in (self.home / 'zincir/model-sonuclari.jsonl').read_text().splitlines()]
        self.assertEqual([r['sonuc'] for r in rows], ['PASS', 'FAIL'])
        self.assertEqual([(x['rol'], x['yol']) for x in rows[0]['roller']],
                         [('impl', 'opencode-go/glm-5.3'), ('kalite', 'opencode/glm-5.3'), ('plan', 'claude/opus')])
        self.assertEqual(rows[0]['maliyet'], {'kaynak': 'bilinmiyor', 'kesin_micro': None, 'askida_micro': None})
        self.assertEqual((rows[0]['politika'], rows[0]['tercih'], rows[0]['zorluk_etkin']), ('difficulty-preference-role-v1', 'balanced', 'medium'))
        self.assertGreater(rows[0]['yas_sn'], 60)
        self.assertNotIn('sure_sn', rows[0], 'FIX-22: alan kosu yasidir (kuyruk/kota beklemesi dahil), rol calisma suresi degil')
        self.assertIsNone(sonuc_kaydet(self.home, self.record, {'status': 'WAITING'}, [], None, None))

    def test_measurements_never_reorder_selection(self):
        adaylar = ['opencode-go/deepseek-v4-pro', 'opencode-go/glm-5.3', 'openrouter/z-ai/glm-5.3']
        before = politika_sirasi(adaylar, 'impl', self.record['model_selection'])
        for n in range(6):
            sonuc_kaydet(self.home, {**self.record, 'run_id': f'r{n}'}, {'status': 'FAIL', 'revision': {'digest': 'x'}}, ['opencode-go/deepseek-v4-pro'], None, None)
            sonuc_kaydet(self.home, {**self.record, 'run_id': f'ok{n}'}, {'status': 'PASS', 'revision': {'digest': 'y'}}, ['opencode-go/glm-5.3'], None, None)
        self.assertEqual(politika_sirasi(adaylar, 'impl', self.record['model_selection']), before)
        ranked, _ = model_secimi.sirala(adaylar, adaylar, 'impl', 8000, self.home, time.time())
        self.assertEqual(ranked[:1], ['opencode-go/deepseek-v4-pro'])

    def test_cr50_outcomes_keep_the_effort_policy_separate_for_comparison(self):
        now = time.time()
        economic = {**self.record, 'run_id': 'r-eko', 'tasks': {'g1': {'efor_politikasi': {'politika': 'ekonomik-efor-v1', 'seviye': 'low'}}}}
        entry = sonuc_kaydet(self.home, economic, {'status': 'PASS', 'revision': {'digest': 'e'}}, ['opencode-go/glm-5.3'], None, None, simdi=now)
        self.assertEqual(entry['efor'], ['ekonomik-efor-v1'])
        plain = sonuc_kaydet(self.home, {**self.record, 'run_id': 'r-std'}, {'status': 'PASS', 'revision': {'digest': 'f'}},
                             ['opencode-go/glm-5.3'], None, None, simdi=now)
        self.assertIsNone(plain['efor'])
        groups = sonuc_ozeti(self.home, simdi=now)['gruplar']
        self.assertEqual(sorted(str(g['efor']) for g in groups if g['rol'] == 'impl'), ['None', 'ekonomik-efor-v1'])

    def test_summary_separates_fresh_stale_few_failed_and_unknown(self):
        now = time.time()
        sonuc_kaydet(self.home, self.record, {'status': 'PASS', 'revision': {'digest': 'a'}}, ['opencode-go/glm-5.3'], None, None, simdi=now - 40 * 86400)
        sonuc_kaydet(self.home, {**self.record, 'run_id': 'r2', 'created': None}, {'status': 'FAIL', 'revision': {'digest': 'b'}}, ['opencode-go/glm-5.3'], None, None, simdi=now)
        with (self.home / 'zincir/model-sonuclari.jsonl').open('a') as bozuk:
            bozuk.write('bozuk satir\n')
        ozet = sonuc_ozeti(self.home, simdi=now)
        self.assertEqual((ozet['toplam'], ozet['bozuk']), (2, 1))
        group = ozet['gruplar'][0]
        self.assertEqual((group['ornek'], group['taze'], group['eski'], group['kabul'], group['basarisiz']), (2, 1, 1, 1, 1))
        self.assertEqual(group['belirsizlik'], ['az-ornek', 'eski-ornek', 'maliyet-bilinmiyor', 'yas-bilinmiyor'])
        self.assertEqual(sonuc_ozeti(Path(tempfile.mkdtemp()))['gruplar'], [])


class UretimYoluTests(unittest.TestCase):
    def setUp(self):
        self.f = CompletionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_final_acceptance_decision_writes_one_sample_per_verdict_from_the_real_path(self):
        self.f.successful_qa()
        with patch.object(core, 'capture_opencode', return_value=self.f.review('DUZELT')):
            result = core.finalize_run('a', str(self.f.wt), self.f.log, '/fake/cli', review_model='')
        self.assertEqual(result['status'], 'FAIL')
        path = self.f.home / 'zincir/model-sonuclari.jsonl'
        rows = [json.loads(l) for l in path.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['run_id'], rows[0]['sonuc'], rows[0]['urun']), (self.f.rid, 'FAIL', result['revision']['digest']))
        self.assertIn(('impl', 'writer'), [(x['rol'], x['model']) for x in rows[0]['roller']])
        with patch.object(core, 'capture_opencode', return_value=self.f.review('DUZELT')):
            core.finalize_run('a', str(self.f.wt), self.f.log, '/fake/cli', review_model='')
        self.assertEqual(len(path.read_text().splitlines()), 1)   # a restart does not count the same result as a second sample


if __name__ == '__main__':
    unittest.main()
