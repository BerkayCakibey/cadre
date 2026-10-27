"""Limit and robustness tests (lesson from the 29.09 external audit, B269).

Rule: if the code has a cap/limit there is an N and N+1 test here; broken or huge input does not crash; expensive paths are limited
by time measurement. No model/network. Caps covered: report QA (unevidenced numbers 20, unknown ids 20, line range 200 (inclusive), BUYUK_DOSYA,
command output 1 MB), the budget-return wait threshold (7200 s), the Go period reset moment, share formula limits,
hand-off caps (lane 2 / run 5), pocket minimum."""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

TESTS = Path(__file__).resolve().parent
LIB = TESTS.parent / 'lib'
for _p in (str(LIB), str(TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import butce  # noqa: E402
import qe_core as core  # noqa: E402
import rapor  # noqa: E402
import yonlendirici as yon  # noqa: E402
from test_rapor_kipi import GECERLI_KANIT, GECERLI_RAPOR, K, RAPOR_ISTEMI, depo, git, kanit_yaz  # noqa: E402


class RaporQaSinirTest(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix='sinir-rapor-'))
        self.addCleanup(shutil.rmtree, self.t, True)
        self.wt, self.sha = depo(self.t)
        self.beyan = rapor.beyan(RAPOR_ISTEMI)

    def qa(self):
        return rapor.rapor_qa(str(self.wt), self.beyan, self.sha, str(self.t / 'gecici'), tohum='sabit')

    def ekle_ve_commitle(self, ad, icerik):
        (self.wt / ad).write_text(icerik)
        git(self.wt, 'add', ad)
        git(self.wt, 'commit', '-qm', ad)
        self.sha = git(self.wt, 'rev-parse', 'HEAD').strip()

    def kanitsiz(self, n):
        cumleler = "\n\n".join(f"Bugün {100 + i} ilan elendi." for i in range(n))
        kanit_yaz(self.wt, GECERLI_RAPOR + "\n" + cumleler + "\n", GECERLI_KANIT)
        return [b for b in self.qa()['bulgular'] if b.startswith('kanitsiz-sayi')]

    def test_kanitsiz_sayi_20_satirda_ozet_yok_21de_toplam_satiri_var(self):
        yirmi = self.kanitsiz(20)
        self.assertEqual(len(yirmi), 20, yirmi[-2:])
        self.assertFalse(any('listelendi' in b for b in yirmi))
        yirmibir = self.kanitsiz(21)
        self.assertEqual(len(yirmibir), 21, yirmibir[-2:])
        self.assertIn('toplam 21 satir (ilk 20 listelendi)', yirmibir[-1])

    def test_bilinmeyen_kimlikte_ilk_20_listelenir(self):
        refs = " ".join(f"[K{i}]" for i in range(30, 55))   # 25 unknown ids
        kanit_yaz(self.wt, GECERLI_RAPOR + f"Kaynaklar {refs}.\n", GECERLI_KANIT)
        bulgu = next(b for b in self.qa()['bulgular'] if b.startswith('bilinmeyen-kanit'))
        self.assertEqual(len(re.findall(r'\bK\d+\b', bulgu)), 20, bulgu)

    def test_satir_araligi_200_satir_gecer_201_ve_ters_ya_da_sifir_aralik_kirmizi(self):
        """D18: the cap is an inclusive line count (1-200 = 200 lines); 201 lines used to pass because the difference was 200."""
        self.ekle_ve_commitle('uzun.txt', "".join(f"satir {i}\n" for i in range(1, 301)))
        kanit_yaz(self.wt, GECERLI_RAPOR + "Uzun [K3][K4][K5][K6].\n", GECERLI_KANIT + [
            K(3, 'dosya', 'uzun.txt:1-200', 'satir 1'), K(4, 'dosya', 'uzun.txt:1-201', 'satir 1'),
            K(5, 'dosya', 'uzun.txt:5-4', 'satir'), K(6, 'dosya', 'uzun.txt:0-1', 'satir')])
        sonuc = self.qa()
        kotu = sorted(b.split(':', 2)[1].strip() for b in sonuc['bulgular'] if b.startswith('dosya-kaynak'))
        self.assertEqual(kotu, ['K4', 'K5', 'K6'], sonuc['bulgular'])
        self.assertTrue(all('gecersiz satir araligi' in b for b in sonuc['bulgular'] if b.startswith('dosya-kaynak')), sonuc['bulgular'])

    def test_buyuk_dosya_tavani_tam_sinirda_gecer_bir_bayt_ustunde_satir_ister(self):
        self.ekle_ve_commitle('tam.txt', 'x' * 996 + 'SON\n')          # 1000 bayt
        self.ekle_ve_commitle('buyuk.txt', 'x' * 997 + 'SON\n')        # 1001 bayt
        kanit_yaz(self.wt, GECERLI_RAPOR + "Dosyalar [K3][K4][K5].\n", GECERLI_KANIT + [
            K(3, 'dosya', 'tam.txt', 'SON'), K(4, 'dosya', 'buyuk.txt', 'SON'), K(5, 'dosya', 'buyuk.txt:1', 'SON')])
        with patch.object(rapor, 'BUYUK_DOSYA', 1000):
            sonuc = self.qa()
        kotu = [b for b in sonuc['bulgular'] if b.startswith('dosya-kaynak')]
        self.assertEqual(len(kotu), 1, sonuc['bulgular'])
        self.assertTrue(kotu[0].startswith('dosya-kaynak: K4:') and 'cok buyuk' in kotu[0], kotu)

    def test_komut_ciktisi_1mb_tavani_belgelenir(self):
        """An evidence command's output is clipped at 1 MB; if the expected text is beyond this limit the claim counts as unproven."""
        otede = "python3 -c \"import sys; sys.stdout.write('x' * 1_100_000); print(); print('SONUC-OK')\""
        icinde = "python3 -c \"print('SONUC-OK'); import sys; sys.stdout.write('x' * 1_100_000)\""
        kanit_yaz(self.wt, GECERLI_RAPOR + "Cikti [K3][K4].\n", GECERLI_KANIT + [K(3, 'komut', otede, 'SONUC-OK'), K(4, 'komut', icinde, 'SONUC-OK')])
        with patch.dict('os.environ', {'CADRE_RAPOR_KOMUT_ORNEK': '9'}):
            sonuc = self.qa()
        kotu = [b for b in sonuc['bulgular'] if b.startswith('komut-kaynak')]
        self.assertEqual([b.split(':', 2)[1].strip() for b in kotu], ['K3'], sonuc['bulgular'])


class BeklemeEsigiSinirTest(unittest.TestCase):
    def karar(self, saat, dakika, saniye):
        simdi = datetime(2026, 9, 24, saat, dakika, saniye).timestamp()
        with patch.object(yon, 'zincir', return_value=([], [])), patch('katalog.bedava_mi', return_value=False):
            return yon.bulut_bekleme('plan', 'zor', 50, simdi=simdi, esik=yon.BEKLEME_ESIK_SN)

    def test_esik_tam_sinirda_bekler_bir_saniye_otesinde_beklemez(self):
        self.assertEqual(yon.BEKLEME_ESIK_SN, 7200)
        self.assertEqual(self.karar(22, 0, 0), 7200 + 60)
        self.assertEqual(self.karar(21, 59, 59), 0)


class ButceFormulSinirTest(unittest.TestCase):


    def test_go_donemi_sifirlama_anina_kadar_son_gun_sonra_yeni_donem(self):
        with tempfile.TemporaryDirectory() as d, patch.object(butce, 'QE', Path(d)):
            (Path(d) / 'zincir').mkdir()
            (Path(d) / 'zincir/go-donem-reset.txt').write_text('2026-10-02 22:00')
            _bas, reset, kalan = butce.go_donem(datetime(2026, 10, 2, 21, 59, 59))
            self.assertEqual((reset.month, reset.day, kalan), (10, 2, 1))
            _bas, reset, kalan = butce.go_donem(datetime(2026, 10, 2, 22, 0, 0))
            self.assertEqual((reset.month, reset.day, kalan), (11, 2, 31))




class DayaniklilikTest(unittest.TestCase):
    def test_kanit_oku_dev_ve_bozuk_satirlarda_cokmez(self):
        metin = ("x" * 2_000_000 + "\n" + json.dumps({'id': 'K1', 'iddia': 'a', 'tur': 'dosya', 'kaynak': 'app.py', 'beklenen': 'x'})
                 + "\n{\"id\": \"K1\"}\n{\"id\": 7}\n[1,2]\n�\n")
        kayitlar, bulgular = rapor.kanit_oku(metin)
        self.assertEqual([k['id'] for k in kayitlar], ['K1'])
        self.assertGreaterEqual(len(bulgular), 4, bulgular)

    def test_buyuk_rapor_qa_suresi_sinirli(self):
        """3000 sentences + 200 file evidences: QA must not exceed 10 s (the external audit did not measure coverage; we measure time)."""
        t = Path(tempfile.mkdtemp(prefix='sinir-buyuk-'))
        self.addCleanup(shutil.rmtree, t, True)
        wt, sha = depo(t)
        kanitlar = [K(i, 'dosya', 'app.py:2', 'return a - b') for i in range(1, 201)]
        metin = "# Özet\n" + "\n\n".join(f"Bulgu {i} için kaynak [K{1 + i % 200}] geçerli." for i in range(3000)) + "\n"
        kanit_yaz(wt, metin, kanitlar)
        t0 = time.perf_counter()
        sonuc = rapor.rapor_qa(str(wt), rapor.beyan(RAPOR_ISTEMI), sha, str(t / 'gecici'), tohum='x')
        sure = time.perf_counter() - t0
        self.assertEqual(sonuc['status'], 'PASS', sonuc['bulgular'][:3])
        self.assertLess(sure, 10.0, f'rapor QA {sure:.1f} sn surdu')


if __name__ == '__main__':
    unittest.main()


class KurtariciNefesTest(unittest.TestCase):
    def test_kurtarici_oturumu_tasiyamayan_saglayiciyi_atlar(self):
        """B277 (01.10 rehearsal): the TASARIM-5 local->cloud hand-off picked the chain's first budgeted link (DS-pro); the DS share was enough for a single call
        but could not carry the session, the gate denied ~2 min later and the session went back to local (4 times; with money on Go). The rescuer needs a share for at least
        CADRE_KURTARICI_NEFES (6) calls; a provider without enough is skipped, other roles are not affected."""
        Y = core.YON
        pay = {'opencode': 0.15, 'opencode-go': 5.0}
        zincir = (['opencode/deepseek-v4-pro', 'opencode-go/qwen3.8-flash'], [])
        with patch.object(Y, 'kapi_rezervasyonu', return_value=0.05), \
             patch.object(Y, 'allowance_usd', side_effect=lambda b, sag, *_: pay.get(sag, 0.0)), \
             patch.object(Y, 'kosu_cebi_kalan', return_value=None), patch('katalog.bedava_mi', return_value=False), \
             patch.object(core, 'butce_json', return_value={'ts': 'x'}), patch.object(Y, 'zincir', return_value=zincir), \
             patch.object(Y, 'durum_oku', return_value={}):
            self.assertIsNotNone(Y.kurtarici_nefesi('opencode/deepseek-v4-pro', {}))
            self.assertIsNone(Y.kurtarici_nefesi('opencode-go/qwen3.8-flash', {}))
            self.assertEqual(core.bulut_aday('kurtarici'), 'opencode-go/qwen3.8-flash')
            self.assertEqual(core.bulut_aday('impl'), 'opencode/deepseek-v4-pro')


class SeritAlternatifTest(unittest.TestCase):
    def test_yarim_seritte_zincirin_sonraki_halkalari_denenir(self):
        """B281 (01.10 rehearsal): a half lane tried only ONE other provider and stopped. 4c-17: filtering not by provider but by
        (model, route) — in chain order excluding the failed route, without repeats, at most n; another model of the same provider is a candidate too."""
        z = ['opencode-go/glm-5.3-flash', 'opencode-go/qwen3.8-flash', 'deepseek/deepseek-v4-flash',
             'opencode/nemotron-3-ultra-free']
        with patch.object(yon, 'zincir', return_value=(z, [])):
            self.assertEqual(yon.alternatifler('impl', 'opencode-go/glm-5.3-flash'),
                             ['opencode-go/qwen3.8-flash', 'deepseek/deepseek-v4-flash', 'opencode/nemotron-3-ultra-free'])
            self.assertEqual(yon.alternatifler('impl', 'opencode-go/glm-5.3-flash', 1), ['opencode-go/qwen3.8-flash'])
            self.assertEqual(yon.alternatifler('impl', 'cerebras/qwen-3.8-27b'), z[:3])


class SeritAlternatifButceSirasiTest(unittest.TestCase):
    def test_yarim_seritte_zincirde_onde_duran_butceli_saglayici_da_aday(self):
        """B331 (03.10 live): a half lane on DeepSeek only looked at the ones AFTER it in the chain; with $50 sitting on Go it went
        to Cerebras (24 times more expensive per call) and the free fledge. The chain is already ordered by budget/abundance; all of it are candidates."""
        z = ['opencode-go/glm-5.3-flash', 'opencode-go/qwen3.8-flash', 'opencode/deepseek-v4-flash', 'deepseek/deepseek-v4-flash',
             'cerebras/qwen-3.8-27b', 'opencode/fledge-alpha-free']
        with patch.object(yon, 'zincir', return_value=(z, [])):
            self.assertEqual(yon.alternatifler('impl', 'deepseek/deepseek-v4-flash', 3),
                             ['opencode-go/glm-5.3-flash', 'opencode-go/qwen3.8-flash', 'opencode/deepseek-v4-flash'])


class NefesNetKapasiteTest(unittest.TestCase):
    def test_kurtarici_nefesi_diger_kosularin_askidaki_rezervasyonlarini_gorur(self):
        """D08 (01.10 Codex): breathing room used the raw observed share; while another run's $1.50 pending reservation left $0.20 of a $2
        share, a 6 x $0.10 rescuer counted as suitable. Breathing room is measured with the ledger's net capacity."""
        import datetime, tempfile
        import butce_kapisi as bk
        with tempfile.TemporaryDirectory(prefix='nefes-net-') as d:
            home = Path(d)
            simdi = datetime.datetime.now()
            b = {'ts': simdi.isoformat(), 'bugun': simdi.date().isoformat(),
                 'zen': {'db': 'ok', 'pay': 2.0, 'kullanilabilir': 2.0}}
            defter = bk.BudgetLedger(home / 'zincir/request-budget.db')
            defter.reserve('opencode', 1_500_000, b, {'run_id': 'baska-kosu'})
            with patch.object(yon, 'QE', home), patch.object(yon, 'kapi_rezervasyonu', return_value=.1), \
                 patch.object(yon, 'kosu_cebi_kalan', return_value=None), patch('katalog.bedava_mi', return_value=False):
                self.assertIsNotNone(yon.kurtarici_nefesi('opencode/deepseek-v4-pro', b))
                defter_bos = home / 'yok'
                with patch.object(yon, 'QE', defter_bos):
                    self.assertIsNone(yon.kurtarici_nefesi('opencode/deepseek-v4-pro', b), 'askida rezervasyon yoksa yeter')


class NefesSinirTest(unittest.TestCase):
    def test_net_kapasite_tam_alti_cagriya_yeter_bir_mikro_dolar_eksigi_yetmez(self):
        """Codex §6/6 (N/N+1): net capacity exactly 6 x reservation -> suitable; one micro-dollar short -> breathless."""
        import datetime, tempfile
        import butce_kapisi as bk
        simdi = datetime.datetime.now()
        b = {'ts': simdi.isoformat(), 'bugun': simdi.date().isoformat(),
             'zen': {'db': 'ok', 'pay': 2.0, 'kullanilabilir': 2.0}}   # share with margin 1.70
        for askida, beklenen_nefessiz in ((1_100_000, False), (1_100_001, True)):
            with self.subTest(askida=askida), tempfile.TemporaryDirectory(prefix='nefes-sinir-') as d:
                home = Path(d)
                bk.BudgetLedger(home / 'zincir/request-budget.db').reserve('opencode', askida, b, {'run_id': 'baska'})
                with patch.object(yon, 'QE', home), patch.object(yon, 'kapi_rezervasyonu', return_value=.1), \
                     patch.object(yon, 'kosu_cebi_kalan', return_value=None), patch('katalog.bedava_mi', return_value=False):
                    self.assertEqual(yon.kurtarici_nefesi('opencode/deepseek-v4-pro', b) is not None, beklenen_nefessiz)


class AlternatifKovaTest(unittest.TestCase):
    Z = ['opencode/deepseek-v4-flash', 'opencode-go/glm-5.3-flash', 'deepseek/deepseek-v4-flash',
         'cerebras/qwen-3.8-27b', 'opencode/nemotron-3-ultra-free']

    def bedava(self, sag, model):
        return model.endswith('-free')

    def test_ucretli_ve_bedava_zen_ayri_kovadir_ve_bedava_halka_denenir(self):
        """D09 (01.10 Codex): B281 deduped paid zen and free zen as the same provider. 4c-17: filtering by (model, route);
        the paid zen route failing does not remove another zen model."""
        z = [*self.Z]
        with patch.object(yon, 'zincir', return_value=(z, [])), patch('katalog.bedava_mi', side_effect=self.bedava):
            self.assertEqual(yon.alternatifler('impl', 'opencode/deepseek-v4-flash', 5), z[1:])

    def test_sifir_ya_da_negatif_n_alternatif_vermez(self):
        """D18: n=0 returned one alternative."""
        with patch.object(yon, 'zincir', return_value=([*self.Z], [])), patch('katalog.bedava_mi', side_effect=self.bedava):
            self.assertEqual(yon.alternatifler('impl', self.Z[1], 0), [])
            self.assertEqual(yon.alternatifler('impl', self.Z[1], -1), [])


class FiyatsizModelTest(unittest.TestCase):
    def test_katalogda_fiyati_eksik_model_bedava_sayilmaz(self):
        """D17 (01.10 Codex, G-B8): `{id: x}` (no cost) returned bedava_mi=True; the plugin counts the same model as paid."""
        import katalog
        for kayit in ({'id': 'fiyatsiz'}, {'id': 'yarim', 'cost': {'input': 0}}, {'id': 'cikti', 'cost': {'output': 0}}):
            with self.subTest(kayit=kayit), patch.object(katalog, 'model', return_value=kayit):
                self.assertIsNone(katalog.bedava_mi('opencode', kayit['id']))
        with patch.object(katalog, 'model', return_value={'id': 'b', 'cost': {'input': 0, 'output': 0}}):
            self.assertIs(katalog.bedava_mi('opencode', 'b'), True)
