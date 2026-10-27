"""G-C16 correction loop (G-E9/G-C16): when QA or the judge says DUZELT the findings go back
to the executor, the product is fixed and acceptance runs again. Stop conditions: PASS · round allowance · same error signature · breaking
test count did not drop · unfixable reason (ERROR, independence, test deletion/weakening, rejected revision). No model/network."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import qe_core as core  # noqa: E402

import os
from unittest.mock import patch

def setUpModule():
    ortam = patch.dict(os.environ, {'CADRE_ZINCIR_KALITE': 'opencode-go/qwen3.8-max'})
    ortam.start()
    unittest.addModuleCleanup(ortam.stop)



def qa_sonucu(cikti: str) -> dict:
    f = Path(tempfile.mkdtemp(prefix='duzeltme-qa-')) / 'qa.log'
    f.write_text(cikti)
    return {'status': 'FAIL', 'reason': 'qa-not-passed', 'qa': {'status': 'FAIL', 'attempts': [{'output': str(f)}]},
            'review': {'status': 'SKIPPED'}}


def hakem_sonucu(fail_idler=('g1',), verdict='DUZELT', status='FAIL') -> dict:
    kabul = [{'id': i, 'status': 'FAIL', 'evidence': f'{i}: kanit satiri tests/x.py:3'} for i in fail_idler]
    kabul.append({'id': 'original_request', 'status': 'PASS', 'evidence': 'tamam'})
    return {'status': 'FAIL', 'reason': 'review-not-passed', 'qa': {'status': 'PASS'},
            'review': {'status': status, 'verdict': verdict, 'acceptance': kabul}}


class HataImzasiTest(unittest.TestCase):
    def test_qa_imzasi_kirilan_test_kimlikleridir(self):
        fr = qa_sonucu('..F\nFAILED tests/b.py::t2 - x\nFAILED tests/a.py::t1 - y\n')
        self.assertEqual(core.hata_imzasi(fr), 'qa:tests/a.py::t1,tests/b.py::t2')

    def test_kimliksiz_qa_ciktisi_icin_kararli_ozet_imza(self):
        """Without a breaking test id (build/lint error) the signature is a stable digest of the tail of the output."""
        a, b = core.hata_imzasi(qa_sonucu('ruff: E501 line too long\n')), core.hata_imzasi(qa_sonucu('ruff: E501 line too long\n'))
        self.assertTrue(a.startswith('qa:#'), a)
        self.assertEqual(a, b)

    def test_hakem_imzasi_fail_kriterleridir_ve_yalniz_duzelt_sayilir(self):
        self.assertEqual(core.hata_imzasi(hakem_sonucu(('g2', 'g1'))), 'hakem:g1,g2')
        self.assertIsNone(core.hata_imzasi(hakem_sonucu(verdict=None, status='ERROR')), 'hakem hatasi urun kusuru degildir')

    def test_duzeltilemez_nedenler_imza_uretmez(self):
        for neden in ('revision-already-rejected', 'reviewer-not-independent', 'test silindi: tests/a.py',
                      'test zayiflatildi: tests/a.py (3->1)', 'finalization-incomplete'):
            with self.subTest(neden=neden):
                self.assertIsNone(core.hata_imzasi({'status': 'FAIL', 'reason': neden}))


class DuzeltmeKarariTest(unittest.TestCase):
    def kayit(self, fr, gecmis=()):
        return {'final_result': fr, 'duzeltme': list(gecmis)}

    def test_ilk_hatada_devam(self):
        karar, tur, imza = core.duzeltme_karari(self.kayit(hakem_sonucu()), ust=2)
        self.assertEqual((karar, tur, imza), ('DEVAM', 1, 'hakem:g1'))

    def test_tur_hakki_bitince_dur(self):
        gecmis = [{'tur': 1, 'imza': 'hakem:g0'}, {'tur': 2, 'imza': 'hakem:g3'}]
        self.assertEqual(core.duzeltme_karari(self.kayit(hakem_sonucu(), gecmis), ust=2)[0], 'DUR')

    def test_ayni_imza_iki_kez_ilerleme_yok(self):
        karar, _, neden = core.duzeltme_karari(self.kayit(hakem_sonucu(), [{'tur': 1, 'imza': 'hakem:g1'}]), ust=3)
        self.assertEqual(karar, 'DUR')
        self.assertIn('ayni hata', neden)

    def test_kirilan_test_sayisi_azalmadiysa_dur_azaldiysa_devam(self):
        once = [{'tur': 1, 'imza': 'qa:tests/a.py::t1'}]
        karar, _, neden = core.duzeltme_karari(self.kayit(qa_sonucu('FAILED tests/b.py::t2\nFAILED tests/c.py::t3\n'), once), ust=3)
        self.assertEqual((karar, 'azalmadi' in neden), ('DUR', True))
        iki = [{'tur': 1, 'imza': 'qa:tests/a.py::t1,tests/b.py::t2'}]
        self.assertEqual(core.duzeltme_karari(self.kayit(qa_sonucu('FAILED tests/b.py::t2\n'), iki), ust=3)[0], 'DEVAM')

    def test_duzeltilemez_neden_ve_pass_dur(self):
        self.assertEqual(core.duzeltme_karari(self.kayit({'status': 'FAIL', 'reason': 'revision-already-rejected'}), ust=2)[0], 'DUR')
        self.assertEqual(core.duzeltme_karari(self.kayit({'status': 'PASS'}), ust=2)[0], 'DUR')



class TemizYenidenBaslamaTest(unittest.TestCase):
    """Design 3.2 (research: when the effect fades after 2-3 rounds, restarting with a clean context is better): at hard+ level if the loop does not converge
    (round allowance, same signature, broken tests not decreasing) one 'restart' round is given; not at medium/simple level, never twice."""
    def kayit(self, fr, gecmis=()):
        return {'final_result': fr, 'duzeltme': list(gecmis)}

    def test_zor_seviyede_yakinsamayan_dongu_bir_kez_yeniden_baslar(self):
        gecmis = [{'tur': 1, 'imza': 'hakem:g1'}]
        self.assertEqual(core.duzeltme_karari(self.kayit(hakem_sonucu(), gecmis), ust=2, yeniden=True)[:2], ('DEVAM', 'yeniden'))
        self.assertEqual(core.duzeltme_karari(self.kayit(hakem_sonucu(), gecmis), ust=2, yeniden=False)[0], 'DUR')

    def test_yeniden_turu_bir_kez_verilir(self):
        gecmis = [{'tur': 1, 'imza': 'hakem:g1'}, {'tur': 'yeniden', 'imza': 'hakem:g1'}]
        self.assertEqual(core.duzeltme_karari(self.kayit(hakem_sonucu(), gecmis), ust=2, yeniden=True)[0], 'DUR')

    def test_duzeltilemez_nedende_yeniden_yok(self):
        self.assertEqual(core.duzeltme_karari(self.kayit({'status': 'FAIL', 'reason': 'revision-already-rejected'}), ust=2, yeniden=True)[0], 'DUR')

    def test_yeniden_istemi_bastan_yazma_iznini_tasir(self):
        istek = Path(tempfile.mkdtemp(prefix='duzeltme-istek-')) / 'prompt.md'
        istek.write_text('is\n')
        metin = core.duzeltme_istemi({'prompt': str(istek), 'final_result': hakem_sonucu()}, tur='yeniden', ust=2)
        self.assertIn('TEMIZ YENIDEN BASLAMA', metin)


class DuzeltmeIstemiTest(unittest.TestCase):
    def test_istem_bulgulari_kurallari_ve_ozgun_istegi_tasir(self):
        istek = Path(tempfile.mkdtemp(prefix='duzeltme-istek-')) / 'prompt.md'
        istek.write_text('ozgun is: fonksiyonu ekle\n')
        kayit = {'prompt': str(istek), 'final_result': hakem_sonucu(('g2',)),
                 'tasks': {'g1': {'kapsam_disi_yeni': ['tests/fazla.py']}}}
        metin = core.duzeltme_istemi(kayit, tur=1, ust=2)
        for parca in ('DUZELTME TURU 1/2', 'HAKEM FAIL [g2]', 'g2: kanit satiri', 'tests/fazla.py', 'BLOKE', 'ozgun is: fonksiyonu ekle'):
            self.assertIn(parca, metin)
        self.assertNotIn('HAKEM FAIL [original_request]', metin, 'gecen kriter istemi sisirmez')

    def test_cr49_long_original_request_is_never_cut_silently_and_the_failed_last_criterion_still_reaches_repair(self):
        istek = Path(tempfile.mkdtemp(prefix='duzeltme-istek-')) / 'prompt.md'
        uzun = 'ozgun kriter satiri\n' * 400 + 'SON KRITER: README korunur\n'
        istek.write_text(uzun)
        metin = core.duzeltme_istemi({'prompt': str(istek), 'final_result': hakem_sonucu(('son',))}, tur=1, ust=2)
        self.assertIn('HAKEM FAIL [son]', metin, 'kalan kriter onarima acikca tasinir')
        self.assertIn(f'KIRPILDI: ilk 4000/{len(uzun)} karakter', metin)
        istek.with_name('kisa.md').write_text('kisa istek\n')
        kisa = core.duzeltme_istemi({'prompt': str(istek.with_name('kisa.md')), 'final_result': hakem_sonucu()}, tur=1, ust=2)
        self.assertNotIn('KIRPILDI', kisa)

    def test_qa_istemi_kirilan_test_ciktisini_tasir(self):
        istek = Path(tempfile.mkdtemp(prefix='duzeltme-istek-')) / 'prompt.md'
        istek.write_text('is\n')
        metin = core.duzeltme_istemi({'prompt': str(istek), 'final_result': qa_sonucu('E   assert 1 == 2\nFAILED tests/a.py::t1\n')}, tur=2, ust=2)
        self.assertIn('assert 1 == 2', metin)
        self.assertIn('QA', metin)



DUZELTME_ISCI = """
import os, pathlib, sys
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
from kosu import RunStore
gid = os.environ['CADRE_GOREV_ID']; wt = pathlib.Path(os.environ['CADRE_WORKTREE'])
if gid.startswith('duzeltme') and os.environ.get('FIXTURE_DUZELTME_DEGISTIR', '1') == '1':
    (wt / 'product.txt').write_text((wt / 'product.txt').read_text() + gid + '\\n')
RunStore().task_update(os.environ['CADRE_RUN_ID'], gid, state='uretti')
"""


class DuzeltmeDongusuUctanUcaTest(unittest.TestCase):
    """Real dispatcher + coordinator, a fake worker and a fake judge with ordered, deterministic verdicts."""
    def setUp(self):
        import test_sonuc
        self.f = test_sonuc.CompletionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def kos(self, kararlar, plan_ek=None, **env):
        rc = self.f.dispatch_fixture('GECER', worker=DUZELTME_ISCI, env={'FIXTURE_VERDICTS': kararlar, **env}, plan_ek=plan_ek)
        kayit = self.f.store.read(self.f.rid)
        cagri = len((self.f.home / 'reviewer-calls').read_text()) if (self.f.home / 'reviewer-calls').exists() else 0
        return rc, kayit, cagri

    def test_duzelt_sonrasi_duzeltme_turu_urunu_duzeltir_ve_kabul_eder(self):
        rc, kayit, cagri = self.kos('DUZELT,GECER')
        self.assertEqual((rc, kayit['state'], cagri), (0, 'completed', 2))
        self.assertEqual([t['tur'] for t in kayit.get('duzeltme') or []], [1])
        self.assertIn('duzeltme-1', (self.f.wt / 'product.txt').read_text())
        satirlar = [json.loads(s) for s in (self.f.store.path(self.f.rid) / 'atama-gecmisi.jsonl').read_text().splitlines()]
        self.assertTrue(satirlar and all({'baslangic_model', 'denenen_modeller', 'son_model'} <= set(s) for s in satirlar), satirlar)

    def test_ayni_hata_iki_kez_gelirse_dongu_durur(self):
        rc, kayit, cagri = self.kos('DUZELT,DUZELT,GECER')
        self.assertEqual(rc, 1)
        self.assertNotEqual(kayit['state'], 'completed')
        self.assertEqual((len(kayit.get('duzeltme') or []), cagri), (1, 2), 'ikinci ayni DUZELT durdurur, GECER\'e varilmaz')

    def test_qa_kirmizisi_duzeltme_turuyla_yesile_doner(self):
        # a fix in the planned file: a new file outside the plan is removed from the product by the scope check (B192), it does not turn QA green
        from qa_fixture import command
        self.f.successful_qa = lambda: Path(self.f.run['constraints']['qa-komut']).write_text(command('grep -q duzeltme product.txt'))
        _rc, kayit, _ = self.kos('GECER')
        self.assertEqual([t['imza'][:3] for t in kayit.get('duzeltme') or []], ['qa:'])
        self.assertEqual(kayit['final_result']['qa']['status'], 'PASS', 'duzeltme turundan sonra QA yesil')
        # the judge step depends on the environment: QA repair makes the local model the writer (B180), there is no cloud judge candidate in the fixture environment

    def test_zor_seviyede_yakinsamayan_dongu_temiz_yeniden_baslamayla_biter(self):
        rc, kayit, cagri = self.kos('DUZELT,DUZELT,GECER', plan_ek={'kapsam_puani': 50})
        self.assertEqual((rc, kayit['state'], cagri), (0, 'completed', 3), kayit.get('final_result'))
        self.assertEqual([t['tur'] for t in kayit.get('duzeltme') or []], [1, 'yeniden'])
        self.assertEqual(kayit['kapsam']['seviye'], 'zor')
        kosu = self.f.store.path(self.f.rid)
        cikti = (kosu / 'dispatch.log').read_text() + (kosu / 'coordinator.log').read_text()
        self.assertNotIn('command not found', cikti, 'kayitli planla devam eden kosuda dagitici islevleri tanimli olmali (G-E11)')

    def test_urunu_degistirmeyen_duzeltme_reddedilmis_revizyonda_durur(self):
        rc, kayit, cagri = self.kos('DUZELT,GECER', FIXTURE_DUZELTME_DEGISTIR='0')
        self.assertEqual((rc, cagri), (1, 1), 'degismemis urun yeniden incelenmez (B193)')
        self.assertEqual(kayit['state'], 'failed')
        self.assertEqual(kayit['final_result']['reason'], 'revision-already-rejected')


SADE_MOTOR_ISCI = """
import os, pathlib, subprocess, sys
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
from kosu import RunStore, owner_fds
gid = os.environ.get('CADRE_GOREV_ID', ''); wt = pathlib.Path(os.environ['CADRE_WORKTREE'])
if not gid:   # sade motor kendi kabulunu kosar (finalize_run), duzeltme yapmaz
    pathlib.Path(os.environ['CADRE_ENGINE_ROOT'], 'sade-motor-cagri').open('a').write('x')
    env = {**os.environ, 'CADRE_GOREV_ID': 'kalite'}
    raise SystemExit(subprocess.run([sys.executable, os.environ['CADRE_ENGINE_ROOT'] + '/lib/qe_core.py', 'finish', sys.argv[1], str(wt)],
                                    env=env, pass_fds=owner_fds()).returncode)
(wt / 'product.txt').write_text((wt / 'product.txt').read_text() + gid + '\\n')
RunStore().task_update(os.environ['CADRE_RUN_ID'], gid, state='uretti')
"""


class SadeMotorDuzeltmeTest(unittest.TestCase):
    """D13 (B274, 30.09 child 060218): on the plan fallback (plain engine) path the judge said DUZELT, and the run ended at the first acceptance FAIL."""
    def setUp(self):
        import test_sonuc
        self.f = test_sonuc.CompletionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_sade_motor_kabulu_duserse_ayni_g_c16_dongusu_calisir(self):
        rc = self.f.dispatch_fixture('GECER', worker=SADE_MOTOR_ISCI, faz='motor', env={'FIXTURE_VERDICTS': 'DUZELT,GECER'})
        kayit = self.f.store.read(self.f.rid)
        self.assertEqual((rc, kayit['state']), (0, 'completed'), kayit.get('final_result', {}).get('reason'))
        self.assertEqual([t['tur'] for t in kayit.get('duzeltme') or []], [1])
        self.assertIn('duzeltme-1', (self.f.wt / 'product.txt').read_text())
        self.assertEqual(len((self.f.home / 'reviewer-calls').read_text()), 2, 'ilk kabul tekrar kosmaz (B193)')
        self.assertEqual(len((self.f.home / 'dispatcher' / 'sade-motor-cagri').read_text()), 1)

    def test_basarisiz_duzeltme_fazi_yeniden_denemeyi_motora_birakir(self):
        rc = self.f.dispatch_fixture('GECER', worker=SADE_MOTOR_ISCI, faz='motor', env={'FIXTURE_VERDICTS': 'DUZELT,DUZELT,DUZELT'})
        kayit = self.f.store.read(self.f.rid)
        self.assertEqual(rc, 1)
        self.assertGreaterEqual(len(kayit.get('duzeltme') or []), 1)
        self.assertEqual(kayit['phase'], 'motor')

    def test_duzeltilemez_sade_motor_hatasinda_dongu_acilmaz(self):
        rc = self.f.dispatch_fixture('GECER', worker=SADE_MOTOR_ISCI, faz='motor', env={'FIXTURE_VERDICTS': 'GECER'})
        self.assertEqual((rc, self.f.store.read(self.f.rid)['state']), (0, 'completed'))
        self.assertNotIn('SADE MOTOR KABULU DUSTU', (self.f.store.path(self.f.rid) / 'coordinator.log').read_text())


if __name__ == '__main__':
    unittest.main()
