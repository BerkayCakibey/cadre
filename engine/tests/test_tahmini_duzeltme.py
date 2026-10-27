"""N-BU03 (RCA code28/29): a ghost/recovery estimate close is corrected only on the related line with a real receipt verified later;
the previous amount and reason leave a trace, a repeat/concurrent operation does not produce a double correction, a contradictory receipt changes nothing."""

from __future__ import annotations

from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))

GEN = 'gen-fixture-0001'


class TahminiDuzeltmeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='tahmini-duzelt-')
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.enterContext(patch.dict(os.environ, {'CADRE_HOME': str(self.home)}))
        import butce_kapisi as bk
        from aylik_butce import MonthlyLedger
        from kosu import RunStore
        self.bk = bk
        self.rid = RunStore(self.home).accept('p', 'kod')['run_id']
        import datetime
        simdi = datetime.datetime.now()
        b = {'ts': simdi.isoformat(), 'bugun': simdi.date().isoformat(), 'or': {'db': 'ok', 'pay': 50.0, 'kullanilabilir': 50.0, 'kalan': 50.0}}
        self.defter = bk.BudgetLedger(self.home / 'zincir/request-budget.db')
        self.aylik_yol = self.home / 'zincir/aylik-butce.db'
        self.aylik = MonthlyLedger(self.aylik_yol, cap=10_000_000)
        self.aylik_id = self.aylik.reserve('openrouter', 229_027, {'model': 'openai/gpt-6.1-sol'})
        self.ident = self.defter.reserve('openrouter', 229_027, b, {'run_id': self.rid, 'model': 'openai/gpt-6.1-sol', 'aylik_id': self.aylik_id})
        self.defter.uretim_kaydet(self.ident, GEN)
        self.defter.settle(self.ident, 229_027, 'hayalet:' + self.ident)
        self.aylik.settle(self.aylik_id, 229_027, 'kurtarma:' + self.aylik_id)
        self.olcum = self.defter.reserve('openrouter', 10_000, b, {'run_id': self.rid, 'model': 'openai/gpt-6.1-sol'})
        self.defter.uretim_kaydet(self.olcum, 'gen-olcum')
        self.defter.settle(self.olcum, 2_000, self.olcum + ':gen-olcum')

    def satir(self, db, ident):
        with closing(sqlite3.connect(db)) as con:
            r = con.execute('select actual, receipt, metadata from reservation where id=?', (ident,)).fetchone()
        return r[0], r[1], json.loads(r[2])

    def test_gec_gelen_fatura_yalniz_ilgili_satiri_iz_birakarak_ve_aylik_esle_birlikte_duzeltir(self):
        import proje
        once = proje.proje_maliyeti(self.home, [self.rid])
        self.assertEqual((once['olculen_usd'], once['tahmini_usd']), (0.002, 0.229027))
        sonuc = self.bk.tahmini_kapanis_duzelt(self.home, self.ident, GEN, 7_048, 'OpenRouter generation faturasi')
        self.assertEqual(sonuc['durum'], 'duzeltildi')
        actual, receipt, meta = self.satir(self.home / 'zincir/request-budget.db', self.ident)
        self.assertEqual((actual, receipt), (7_048, f'{self.ident}:{GEN}'))
        self.assertEqual(meta['tahmini_duzeltme']['onceki_actual'], 229_027)
        self.assertEqual(meta['tahmini_duzeltme']['onceki_receipt'], 'hayalet:' + self.ident)
        self.assertEqual(meta['tahmini_duzeltme']['gerekce'], 'OpenRouter generation faturasi')
        self.assertEqual(self.satir(self.aylik_yol, self.aylik_id)[:2], (7_048, f'{self.aylik_id}:{GEN}'))
        self.assertEqual(self.satir(self.home / 'zincir/request-budget.db', self.olcum)[:2], (2_000, self.olcum + ':gen-olcum'))
        sonra = proje.proje_maliyeti(self.home, [self.rid])
        self.assertEqual((sonra['olculen_usd'], sonra['tahmini_usd'], sonra['askida_usd']), (0.009048, 0.0, 0.0))
        log = (self.home / 'zincir/kurtarma.jsonl')
        if log.exists():
            self.assertIn('tahmini-duzelt', log.read_text())

    def test_tekrar_ve_eszamanli_duzeltme_cift_degisiklik_uretmez(self):
        sonuclar = []
        bariyer = threading.Barrier(4)

        def calis():
            bariyer.wait()
            sonuclar.append(self.bk.tahmini_kapanis_duzelt(self.home, self.ident, GEN, 7_048, 'fatura')['durum'])

        iplikler = [threading.Thread(target=calis) for _ in range(4)]
        for i in iplikler:
            i.start()
        for i in iplikler:
            i.join()
        self.assertEqual(sorted(sonuclar), ['duzeltildi', 'zaten-duzeltildi', 'zaten-duzeltildi', 'zaten-duzeltildi'])
        self.assertEqual(self.bk.tahmini_kapanis_duzelt(self.home, self.ident, GEN, 7_048, 'fatura')['durum'], 'zaten-duzeltildi')
        _, _, meta = self.satir(self.home / 'zincir/request-budget.db', self.ident)
        self.assertEqual(meta['tahmini_duzeltme']['onceki_actual'], 229_027, 'iz ilk tahmini tutari korur')
        self.assertEqual(self.satir(self.aylik_yol, self.aylik_id)[0], 7_048)

    def test_celisen_veya_dogrulanamayan_makbuz_hicbir_seyi_degistirmez(self):
        db = self.home / 'zincir/request-budget.db'
        once = (self.satir(db, self.ident), self.satir(db, self.olcum), self.satir(self.aylik_yol, self.aylik_id))
        durumlar = [
            self.bk.tahmini_kapanis_duzelt(self.home, self.ident, 'gen-baska-uretim', 7_048, 'yanlis kimlik')['durum'],
            self.bk.tahmini_kapanis_duzelt(self.home, self.ident, GEN, 229_028, 'ust sinir asimi')['durum'],
            self.bk.tahmini_kapanis_duzelt(self.home, self.olcum, 'gen-olcum', 1_000, 'olculmus satir')['durum'],
            self.bk.tahmini_kapanis_duzelt(self.home, 'yok', GEN, 1, 'satir yok')['durum'],
            self.bk.tahmini_kapanis_duzelt(self.home, self.ident, 'gecersiz kimlik', 1, 'bicim')['durum'],
        ]
        self.assertEqual(durumlar, ['kimlik-uyusmuyor', 'ust-siniri-asiyor', 'tahmini-degil', 'yok', 'gecersiz'])
        self.assertEqual((self.satir(db, self.ident), self.satir(db, self.olcum), self.satir(self.aylik_yol, self.aylik_id)), once)

    def test_aylik_es_celisirse_istek_defteri_de_degismez(self):
        with closing(sqlite3.connect(self.aylik_yol)) as con, con:
            con.execute('update reservation set receipt=? where id=?', ('baska-makbuz', self.aylik_id))
        db = self.home / 'zincir/request-budget.db'
        once = self.satir(db, self.ident)
        self.assertEqual(self.bk.tahmini_kapanis_duzelt(self.home, self.ident, GEN, 7_048, 'fatura')['durum'], 'aylik-es-celisiyor')
        self.assertEqual(self.satir(db, self.ident), once)

    def test_bekci_mutabakati_hayalet_satiri_kendiliginden_degistirmez(self):
        import butce
        yanit = io.BytesIO(json.dumps({'data': {'total_cost': 0.007048}}).encode())
        db = self.home / 'zincir/request-budget.db'
        once = self.satir(db, self.ident)
        with patch.object(butce, 'QE', self.home), patch.object(butce, 'ag_acik', return_value=True), \
             patch.object(butce, 'or_anahtari', return_value='test'), \
             patch.object(butce.urllib.request, 'urlopen', return_value=yanit) as cagri:
            self.assertEqual(butce.or_askida_kapat(), 0)
        cagri.assert_not_called()
        self.assertEqual(self.satir(db, self.ident), once)


if __name__ == '__main__':
    unittest.main()
