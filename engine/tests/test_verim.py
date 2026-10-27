"""CR-40 (N-BU07, N-OBS02): stage/pool cost and time breakdown; the ledger is the single source, unknown is never written as zero."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import arayuz_veri
import dil
from butce_kapisi import BudgetLedger
from kosu import RunStore, atomic_json
import verim


class VerimTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix='verim-'))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        (self.home / 'projects/p').mkdir(parents=True)
        (self.home / 'zincir').mkdir()
        self.env = patch.dict(os.environ, {'CADRE_HOME': str(self.home)}, clear=False)
        self.env.start(); self.addCleanup(self.env.stop)
        os.environ.pop('CADRE_RUN_ID', None)
        self.store = RunStore(self.home)
        self.parent = self.store.accept('p', 'buyuk is')['run_id']
        self.child = self.store.accept('p', 'parca', parent_run_id=self.parent)['run_id']
        self.store.update(self.child, proje_ust=self.parent, final_result={'status': 'FAIL', 'reason': 'qa-not-passed'})
        self.store.update(self.parent, project_run_ids=[self.parent, self.child], final_result={'status': 'PASS'},
                          budget_waits=[{'ts': 100.0, 'until': 160.0}, {'ts': 200.0, 'until': 230.0}, 'bozuk'])
        self.ledger = BudgetLedger(self.home / 'zincir/request-budget.db')
        now = time.time()
        rows = [  # (run, provider, reserved, actual, receipt, metadata)
            (self.parent, 'opencode-go', 1000, 700, 'r1', {'task_id': 'plan', 'phase_role': 'plan', 'dilim': 'seritler'}),
            (self.parent, 'openrouter', 2000, 1500, 'r2', {'task_id': 'plan-review-1-ab', 'role': 'review', 'dilim': 'hakem'}),
            (self.parent, 'openrouter', 3000, 2500, 'r3', {'task_id': 'g1', 'phase_role': 'impl', 'dilim': 'seritler'}),
            (self.parent, 'openrouter', 400, None, None, {'task_id': 'g1', 'phase_role': 'impl', 'dilim': 'seritler'}),
            (self.parent, 'opencode-go', 900, 900, 'hayalet:x', {'task_id': 'qa', 'phase_role': 'qa', 'dilim': 'seritler'}),
            (self.parent, 'openrouter', 500, 450, 'r5', {'task_id': 'kalite', 'phase_role': 'kalite', 'dilim': 'hakem'}),
            (self.parent, 'openrouter', 600, 0, 'unused:1', {'task_id': 'main', 'guvence': True, 'guvence_rol': 'kalite', 'dilim': 'hakem'}),
            (self.child, 'openrouter', 800, 650, 'r7', {'task_id': 'duzeltme-1', 'phase_role': 'kurtarici', 'dilim': 'seritler'}),
            (self.child, 'groq', 1, 1, 'r8', {'task_id': 'g1', 'phase_role': 'impl'}),
        ]
        with self.ledger.transaction() as con:
            for n, (rid, provider, reserved, actual, receipt, meta) in enumerate(rows):
                con.execute('insert into reservation values(?,?,?,?,?,?,?,?)', (f'id{n}', provider, '2026-10', now - 100 + n,
                            reserved, actual, receipt, json.dumps({**meta, 'run_id': rid, 'model': 'm'})))
            con.execute('insert into reservation values(?,?,?,?,?,?,?,?)', ('bad', 'openrouter', '2026-10', now, 5, None, None, 'not json'))
        for rid, task, role, start, end in ((self.parent, 'plan', 'plan', 1000.0, 1030.0), (self.parent, 'g1', 'impl', 1100.0, 1400.0),
                                             (self.parent, 'kalite', 'kalite', 1500.0, 1560.0), (self.child, 'duzeltme-1', 'kurtarici', 2000.0, 2090.0)):
            path = self.store.task_dir(rid, task) / 'attempts/a1/attempt.json'
            path.parent.mkdir(parents=True)
            atomic_json(path, {'run_id': rid, 'task_id': task, 'attempt_id': 'a1', 'role': role, 'created': start, 'finished': end})
        atomic_json(self.store.task_dir(self.parent, 'g2') / 'attempts/a1/attempt.json', {'task_id': 'g2', 'role': 'impl', 'created': 'yok'})
        atomic_json(self.store.path(self.parent) / 'taban-qa.json', {'status': 'PASS', 'kurulum': {'status': 'PASS', 'seconds': 12.5}, 'qa': {'status': 'PASS'}})
        record = json.loads((self.store.path(self.parent) / 'run.json').read_text())
        record['created'] = 950.0
        (self.store.path(self.parent) / 'run.json').write_text(json.dumps(record))

    def test_stages_pools_and_certainty_are_separated_from_one_ledger(self):
        data = verim.kirilim(self.home, self.parent)
        self.assertEqual(data['kapsam'], sorted([self.parent, self.child]))
        stages = data['maliyet']['asamalar']
        self.assertEqual(data['maliyet']['kaynak'], 'request-ledger')
        self.assertEqual(data['maliyet']['bozuk'], 1)
        self.assertEqual(stages['plan']['esdeger'], {'kesin': 700, 'tahmini': 0, 'askida': 0})
        self.assertEqual(stages['plan-review']['nakit'], {'kesin': 1500, 'tahmini': 0, 'askida': 0})
        self.assertEqual(stages['impl']['nakit'], {'kesin': 2500, 'tahmini': 0, 'askida': 400})
        self.assertEqual(stages['impl']['bilinmeyen_birim'], 1)   # groq: neither cash nor equivalent; not summed
        self.assertEqual(stages['qa']['esdeger'], {'kesin': 0, 'tahmini': 900, 'askida': 0})
        self.assertEqual(stages['kalite']['nakit']['kesin'], 450)
        self.assertEqual(stages['guvence']['nakit'], {'kesin': 0, 'tahmini': 0, 'askida': 0})
        self.assertEqual(stages['repair']['nakit']['kesin'], 650)
        self.assertEqual(data['token'], 'bilinmiyor')
        self.assertEqual(data['kabul'], {'durum': 'PASS', 'kabul_edildi': True})
        self.assertEqual(data['basarisiz_cocuk']['kosular'], [self.child])
        self.assertEqual(data['basarisiz_cocuk']['maliyet']['asamalar']['repair']['nakit']['kesin'], 650)

    def test_running_child_is_not_a_failed_child_but_cancelled_one_is(self):
        running = self.store.accept('p', 'suren parca', parent_run_id=self.parent)['run_id']
        self.store.update(running, proje_ust=self.parent, state='running')
        cancelled = self.store.accept('p', 'iptal parca', parent_run_id=self.parent)['run_id']
        self.store.update(cancelled, proje_ust=self.parent, state='cancelled')
        self.store.update(self.parent, project_run_ids=[self.parent, self.child, running, cancelled])
        kosular = verim.kirilim(self.home, self.parent)['basarisiz_cocuk']['kosular']
        self.assertIn(self.child, kosular)
        self.assertIn(cancelled, kosular)
        self.assertNotIn(running, kosular, 'FIX-21: calisan cocuk basarisiz degildir')

    def test_durations_separate_work_queue_quota_wait_and_setup(self):
        s = verim.kirilim(self.home, self.parent)['sure']
        self.assertEqual(s['calisma_sn'], {'impl': 300.0, 'kalite': 60.0, 'plan': 30.0, 'repair': 90.0})
        self.assertEqual(s['kuyruk_sn'], 50.0)
        self.assertEqual(s['kota_bekleme_sn'], 90.0)
        self.assertEqual(s['kurulum_sn'], 12.5)
        self.assertEqual(s['belirsiz_deneme'], 1)

    def test_missing_ledger_is_unknown_not_zero_and_ui_lists_the_breakdown(self):
        (self.home / 'zincir/request-budget.db').unlink()
        data = verim.kirilim(self.home, self.parent)
        self.assertEqual(data['maliyet']['kaynak'], 'bilinmiyor')
        self.assertTrue(all(not v['istek'] for v in data['maliyet']['asamalar'].values()))
        lines = verim.ozet_satirlari(data)
        self.assertIn('kuyruk 50.0', lines[-1])
        node = arayuz_veri.harcama_dugumu(self.home, self.store.read(self.parent), {})
        self.assertIn(('bolum', 'Aşama kırılımı'), node['ayrinti'])

    def test_cr52_tokens_cache_reasoning_and_endpoint_come_from_receipt_metadata_unknown_stays_unknown(self):
        with self.ledger.transaction() as con:
            for ident, usage in (('id2', {'girdi': 1000, 'cikti': 200, 'dusunme': 50, 'onbellek_okuma': 800, 'onbellek_yazma': 0,
                                          'uc': 'StreamLake', 'ucret_usd': 0.0025}),
                                 ('id7', {'girdi': 300, 'cikti': 100, 'uc': 'DeepInfra'})):
                con.execute("update reservation set metadata=json_set(metadata,'$.kullanim',json(?)) where id=?", (json.dumps(usage), ident))
            con.execute("update reservation set metadata=json_set(metadata,'$.onek',json(?)) where id='id2'",
                        (json.dumps({'sabit': 'a' * 16, 'devam': True, 'mesaj': 4}),))
            con.execute("update reservation set metadata=json_set(metadata,'$.onek',json(?)) where id='id3'",
                        (json.dumps({'sabit': 'a' * 16, 'devam': False, 'mesaj': 2}),))
        data = verim.kirilim(self.home, self.parent)
        token = data['token']
        self.assertEqual((token['olculen_istek'], token['bilinmeyen_istek']), (2, 6), 'guvence satiri istek degildir')
        self.assertEqual((token['girdi'], token['cikti'], token['onbellek_okuma'], token['onbellek_yazma']), (1300, 300, 800, 0))
        self.assertEqual((token['dusunme'], token['dusunme_bilinmeyen']), (50, 1), 'dusunme cikti icindedir, eksikse bilinmiyor')
        self.assertEqual(token['onbellek_bilinmeyen'], 1)
        self.assertEqual(token['uclar'], {'StreamLake': 1, 'DeepInfra': 1, 'bilinmiyor': 6})
        self.assertEqual(token['onek'], {'devam': 1, 'kirik': 1, 'ilk_veya_bilinmiyor': 6})
        stages = data['maliyet']['asamalar']
        self.assertEqual(stages['impl']['token']['onbellek_okuma'], 800)
        self.assertEqual(stages['repair']['token']['olculen_istek'], 1)
        self.assertEqual(stages['plan']['token']['olculen_istek'], 0)
        self.assertEqual(stages['impl']['nakit'], {'kesin': 2500, 'tahmini': 0, 'askida': 400}, 'kullanim para toplamini degistirmez')
        self.assertIn('ayri', data['hizmet_ucreti'])
        lines = '\n'.join(verim.ozet_satirlari(data))
        self.assertIn('2/8', lines)
        self.assertIn('800', lines)

    def test_cr52_helper_calls_are_visible_and_counted_in_the_same_total(self):
        with self.ledger.transaction() as con:
            for n, role in enumerate(('title', 'summary', 'compaction')):
                con.execute('insert into reservation values(?,?,?,?,?,?,?,?)', (f'y{n}', 'openrouter', '2026-10', time.time(), 50, 40,
                            f'ry{n}', json.dumps({'run_id': self.parent, 'role': role, 'task_id': 'g1', 'model': 'm'})))
        data = verim.kirilim(self.home, self.parent)
        self.assertEqual(data['yardimci_cagrilar'], {'compaction': 1, 'summary': 1, 'title': 1})
        self.assertEqual(data['maliyet']['asamalar']['yardimci']['nakit']['kesin'], 120)

    def test_cr52_campaign_cost_per_accepted_job_includes_failures_and_is_undefined_without_acceptance(self):
        other = self.store.accept('p', 'ikinci is')['run_id']
        self.store.update(other, final_result={'status': 'FAIL', 'reason': 'qa-not-passed'})
        with self.ledger.transaction() as con:
            con.execute('insert into reservation values(?,?,?,?,?,?,?,?)', ('o1', 'openrouter', '2026-10', time.time(), 900, 700, 'ro1',
                        json.dumps({'run_id': other, 'task_id': 'g1', 'phase_role': 'impl', 'model': 'm'})))
        data = verim.kampanya(self.home, [self.parent, other, self.child])
        self.assertEqual(data['isler'], sorted([self.parent, other]))
        self.assertEqual(data['kabul_edilen'], 1)
        self.assertEqual(data['toplam']['nakit']['kesin'], 1500 + 2500 + 450 + 650 + 700, 'basarisiz is ve cocuk gideri toplamda')
        self.assertEqual(data['toplam']['esdeger'], {'kesin': 700, 'tahmini': 900, 'askida': 0})
        self.assertEqual(data['kabul_basina']['nakit_kesin_usd'], round((1500 + 2500 + 450 + 650 + 700) / 1e6, 6))
        self.assertEqual(data['toplam']['nakit']['askida'], 400)
        self.store.update(self.parent, final_result={'status': 'FAIL', 'reason': 'x'})
        none = verim.kampanya(self.home, [self.parent, other])
        self.assertEqual((none['kabul_edilen'], none['kabul_basina']), (0, 'tanimsiz'))
        import io
        with patch.object(sys, 'stdout', io.StringIO()) as out:
            self.assertEqual(verim.main(['--kampanya', self.parent, other]), 0)
        self.assertEqual(json.loads(out.getvalue())['kabul_basina'], 'tanimsiz')
        with patch.object(sys, 'stderr', io.StringIO()):
            self.assertEqual(verim.main(['--kampanya']), 2)
            self.assertEqual(verim.main(['--kampanya', '../x']), 1)

    def test_tsr03_missing_or_unreadable_ledger_never_reports_a_zero_campaign_cost(self):
        for bozuk in (None, b'SQLite degil', b''):
            path = self.home / 'zincir/request-budget.db'
            path.unlink(missing_ok=True)
            if bozuk is not None:
                path.write_bytes(bozuk)
            data = verim.kampanya(self.home, [self.parent])
            self.assertEqual(data['kaynak'], 'bilinmiyor', bozuk)
            self.assertEqual(data['kabul_edilen'], 1)
            self.assertEqual(data['kabul_basina'], 'bilinmiyor', 'veri yoklugu sifir dolar orani degildir')
            self.assertEqual(data['toplam'], 'bilinmiyor', 'toplam da sifir yazilmaz')
            self.assertEqual(data['maliyet_kapsami']['durum'], 'bilinmiyor')
            self.assertNotIn('0.0', json.dumps(data['kabul_basina']))
        self.store.update(self.parent, final_result={'status': 'FAIL', 'reason': 'x'})
        self.assertEqual(verim.kampanya(self.home, [self.parent])['kabul_basina'], 'tanimsiz', 'kabul yoksa oran tanimsiz kalir')

    def test_tsr03_real_zero_stays_zero_and_partial_ledger_coverage_is_explicit(self):
        temiz = Path(tempfile.mkdtemp(prefix='verim-sifir-'))
        self.addCleanup(shutil.rmtree, temiz, ignore_errors=True)
        (temiz / 'projects/p').mkdir(parents=True)
        store = RunStore(temiz)
        rid = store.accept('p', 'sifir maliyetli kabul')['run_id']
        store.update(rid, final_result={'status': 'PASS'})
        ledger = BudgetLedger(temiz / 'zincir/request-budget.db')
        with ledger.transaction() as con:
            con.execute('insert into reservation values(?,?,?,?,?,?,?,?)', ('z1', 'openrouter', '2026-10', time.time(), 300, 0,
                        'gen-z1:http-429', json.dumps({'run_id': rid, 'task_id': 'g1', 'phase_role': 'impl', 'model': 'm'})))
        sifir = verim.kampanya(temiz, [rid])
        self.assertEqual(sifir['kaynak'], 'request-ledger')
        self.assertEqual(sifir['maliyet_kapsami'], {'durum': 'tam', 'bozuk_satir': 0, 'bilinmeyen_birim': 0})
        self.assertEqual(sifir['kabul_basina']['nakit_kesin_usd'], 0.0, 'dogrulanmis gercek sifir korunur')
        self.assertEqual(sifir['kabul_basina']['kapsam'], 'tam')
        kismi = verim.kampanya(self.home, [self.parent, self.child])
        self.assertEqual(kismi['maliyet_kapsami'], {'durum': 'kismi', 'bozuk_satir': 1, 'bilinmeyen_birim': 1})
        self.assertEqual(kismi['kabul_basina']['kapsam'], 'kismi', 'bozuk satir/bilinmeyen birim varken oran alt sinirdir')
        self.assertEqual(kismi['toplam']['nakit']['askida'], 400, 'askidaki tutar ayri kalir')
        self.assertEqual(kismi['kabul_basina']['askida_ayri_usd'], round(400 / 1e6, 6))
        self.assertEqual(kismi['toplam']['nakit']['kesin'], 1500 + 2500 + 450 + 650, 'cocuk gideri bir kez')

    def test_tsr04_missing_cache_write_and_reasoning_fields_are_shown_unknown_or_partial_never_zero(self):
        with self.ledger.transaction() as con:
            con.execute("update reservation set metadata=json_set(metadata,'$.kullanim',json(?)) where id='id2'",
                        (json.dumps({'girdi': 10, 'cikti': 2}),))
        yalniz = verim.kirilim(self.home, self.parent)
        self.assertEqual((yalniz['token']['onbellek_bilinmeyen'], yalniz['token']['onbellek_yazma_bilinmeyen'],
                          yalniz['token']['dusunme_bilinmeyen']), (1, 1, 1))
        metin = '\n'.join(verim.ozet_satirlari(yalniz))
        self.assertIn('önbellek okuma bilinmiyor', metin)
        self.assertIn('yazma bilinmiyor', metin)
        self.assertIn('düşünme bilinmiyor', metin)
        self.assertNotIn('okuma 0', metin)
        with self.ledger.transaction() as con:
            con.execute("update reservation set metadata=json_set(metadata,'$.kullanim',json(?)) where id='id1'",
                        (json.dumps({'girdi': 1000, 'cikti': 200, 'dusunme': 0, 'onbellek_okuma': 800, 'onbellek_yazma': 0,
                                     'ucret_usd': 0.0015}),))
        kismi = verim.kirilim(self.home, self.parent)
        metin = '\n'.join(verim.ozet_satirlari(kismi))
        self.assertIn('önbellek okuma 800 [1/2 istekte]', metin)
        self.assertIn('yazma 0 [1/2 istekte]', metin)
        self.assertIn('düşünme 0 [1/2 istekte]', metin)
        self.assertEqual(kismi['maliyet']['asamalar']['plan-review']['nakit'], {'kesin': 1500, 'tahmini': 0, 'askida': 0},
                         'makbuz ucreti ve token kirilimi defter tutarina ikinci kez eklenmez')
        with self.ledger.transaction() as con:
            con.execute("update reservation set metadata=json_set(metadata,'$.kullanim',json(?)) where id='id2'",
                        (json.dumps({'girdi': 10, 'cikti': 2, 'dusunme': 0, 'onbellek_okuma': 0, 'onbellek_yazma': 0}),))
        tam = '\n'.join(verim.ozet_satirlari(verim.kirilim(self.home, self.parent)))
        self.assertIn('önbellek okuma 800, yazma 0', tam, 'acik makbuz sifiri gercek sifirdir')
        self.assertIn('düşünme 0)', tam)
        dil.EKSIK.clear()
        dil.kur('en')
        try:
            ingilizce = '\n'.join(verim.ozet_satirlari(kismi))
        finally:
            dil.kur('tr')
        self.assertEqual(sorted(dil.EKSIK), [])
        self.assertIn('cache read 800 [in 1/2 requests]', ingilizce)

    def test_tsr04_requests_without_any_measured_usage_show_tokens_unknown(self):
        data = verim.kirilim(self.home, self.parent)
        self.assertEqual(data['token'], 'bilinmiyor')
        self.assertIn('token: bilinmiyor', verim.ozet_satirlari(data))

    def test_cli_prints_json_and_rejects_bad_identity(self):
        import io
        with patch.object(sys, 'stdout', io.StringIO()) as out:
            self.assertEqual(verim.main([self.parent]), 0)
        self.assertEqual(json.loads(out.getvalue())['run_id'], self.parent)
        with patch.object(sys, 'stderr', io.StringIO()):
            self.assertEqual(verim.main(['../x']), 1)
            self.assertEqual(verim.main([]), 2)


if __name__ == '__main__':
    unittest.main()
