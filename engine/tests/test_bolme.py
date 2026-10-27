"""G-E10 adaptive splitting: at very hard+ level a lane whose chain is exhausted (failed) is split once into 2–3 sequential sub-tasks;
a sub-task is not split again, sub-task files cannot leave the parent task's list, a user decision / environment failure
is not split. No model/network; the end-to-end tests use the real dispatcher + coordinator with a fake worker/planner/judge."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import bolme  # noqa: E402
import dispatch_util  # noqa: E402

import os
from unittest.mock import patch

def setUpModule():
    ortam = patch.dict(os.environ, {'CADRE_ZINCIR_KALITE': 'opencode-go/qwen3.8-max'})
    ortam.start()
    unittest.addModuleCleanup(ortam.stop)



class BolmeOrnegiTest(unittest.TestCase):
    def test_the_example_sub_plan_passes_plan_validation(self):
        """06.10 QA: the example did not carry `tahmin_usd`; a sub-plan copying it was rejected by D12 and G-E10 died."""
        plan = {'is_id': 'x', 'gorevler': [{'id': 'g1', 'tanim': 't', 'kabul': 'k', 'dosyalar': ['a.py'], 'bagimli': [],
                                            'zorluk': 4, 'model': 'ds-pro', 'paralel_grup': 1, 'tahmin_dk': 30, 'tahmin_usd': 1.0}]}
        ornek = bolme.bolme_ornegi(plan, 'g1')
        dispatch_util.validate_plan(ornek)
        self.assertIn('"tahmin_usd"', bolme.bolme_istemi(plan, 'g1', 'neden'))


def plan():
    return {'is_id': 'is', 'gorevler': [
        {'id': 'g1', 'tanim': 'modul yaz', 'dosyalar': ['src/a.py', 'tests/test_a.py'], 'kabul': 'testler yesil', 'bagimli': [],
         'zorluk': 4, 'model': 'auto', 'paralel_grup': 1, 'tahmin_dk': 60},
        {'id': 'g2', 'tanim': 'belge', 'dosyalar': ['README.md'], 'kabul': 'satir var', 'bagimli': ['g1'],
         'zorluk': 2, 'model': 'auto', 'paralel_grup': 1, 'tahmin_dk': 10}]}


def alt(id_, dosyalar, bagimli=()):
    return {'id': id_, 'tanim': f'parca {id_}', 'dosyalar': dosyalar, 'kabul': f'{id_} olculur', 'bagimli': list(bagimli),
            'zorluk': 3, 'model': 'auto', 'paralel_grup': 1, 'tahmin_dk': 20, 'alt_ajanlar': []}


def alt_cikti(*gorevler):
    return 'aciklama\n```json\n' + json.dumps({'is_id': 'g1-bol', 'gorevler': list(gorevler)}) + '\n```'


class BolunebilirTest(unittest.TestCase):
    def test_cok_zor_ve_ultra_seviyede_yakinsamayan_serit_bolunur(self):
        for sev in ('cok-zor', 'ultra'):
            self.assertTrue(bolme.bolunebilir(plan(), 'g1', sev, 'rc=1')[0], sev)
        self.assertTrue(bolme.bolunebilir(plan(), 'g1', 'cok-zor', '')[0], 'neden yazilmamis serit de bolunur')

    def test_zor_ve_alti_bolunmez(self):
        for sev in ('basit', 'orta', 'zor', ''):
            self.assertFalse(bolme.bolunebilir(plan(), 'g1', sev, 'rc=1')[0], sev)

    def test_kullanici_karari_ve_ortam_arizasi_bolunmez(self):
        for neden in ('no-capacity', 'disk-low', 'kurulum-hatasi', 'kritik-dosya-ihlali', 'onay-bekliyor: X mi Y mi'):
            self.assertFalse(bolme.bolunebilir(plan(), 'g1', 'cok-zor', neden)[0], neden)

    def test_alt_gorev_ve_bolunmus_gorev_yeniden_bolunmez(self):
        p = plan()
        p['gorevler'][0]['ust'] = 'g0'
        self.assertFalse(bolme.bolunebilir(p, 'g1', 'cok-zor', 'rc=1')[0])
        p = plan()
        p['gorevler'][0]['bolundu'] = ['g1-1', 'g1-2']
        self.assertFalse(bolme.bolunebilir(p, 'g1', 'cok-zor', 'rc=1')[0])


class BolmeIstemiTest(unittest.TestCase):
    def test_istem_ust_gorevi_nedeni_dosyalari_ve_semayi_icerir(self):
        istem = bolme.bolme_istemi(plan(), 'g1', 'rc=1', '[{"id":"m1","passes":true}]')
        for parca in ('ALT GOREVLERE BOL', 'modul yaz', 'testler yesil', 'src/a.py', 'tests/test_a.py', 'rc=1', '2-3', '"gorevler"', '"m1"'):
            self.assertIn(parca, istem)


class AltPlanBirlestirTest(unittest.TestCase):
    def test_gecerli_alt_plan_ust_gorevin_arkasina_sirali_eklenir(self):
        yeni, ids = bolme.alt_plan_birlestir(plan(), 'g1', alt_cikti(alt('a', ['src/a.py']), alt('b', ['./tests/test_a.py'], ['a'])))
        self.assertEqual(ids, ['g1-1', 'g1-2'])
        g = {t['id']: t for t in yeni['gorevler']}
        self.assertEqual([t['id'] for t in yeni['gorevler']], ['g1', 'g1-1', 'g1-2', 'g2'])
        self.assertEqual(g['g1']['bolundu'], ids)
        self.assertEqual((g['g1-1']['ust'], g['g1-1']['bagimli'], g['g1-2']['bagimli']), ('g1', [], ['g1-1']))
        self.assertEqual(g['g1-2']['dosyalar'], ['tests/test_a.py'], 'yol normalize edilir')
        self.assertNotIn('alt_ajanlar', g['g1-1'], 'alt gorev yardimci cagirmaz')

    def test_sinir_disi_sayida_alt_gorev_reddedilir(self):
        for n in (1, 4):
            with self.subTest(n=n), self.assertRaises(ValueError):
                bolme.alt_plan_birlestir(plan(), 'g1', alt_cikti(*[alt(f'x{i}', ['src/a.py']) for i in range(n)]))

    def test_ust_gorevin_disinda_dosya_reddedilir(self):
        with self.assertRaises(ValueError):
            bolme.alt_plan_birlestir(plan(), 'g1', alt_cikti(alt('a', ['src/a.py']), alt('b', ['src/yeni.py'])))

    def test_json_olmayan_cikti_reddedilir(self):
        with self.assertRaises(ValueError):
            bolme.alt_plan_birlestir(plan(), 'g1', 'plan yapamadim')

    def test_ana_dongu_sirasi_alt_gorevleri_icermez(self):
        yeni, _ = bolme.alt_plan_birlestir(plan(), 'g1', alt_cikti(alt('a', ['src/a.py']), alt('b', ['src/a.py'])))
        self.assertEqual(dispatch_util.ana_sira(yeni), ['g1', 'g2'])



class BolmePlanlayiciSuresiTest(unittest.TestCase):
    """The splitting planner may fall to a local model (~10 tokens/s); the plan loop's 300 s are not enough for it, splitting gives 900 s."""
    def test_bolme_cagrisi_uzun_zaman_asimiyla_yapilir(self):
        kaynak = (LIB.parent / 'bin' / 'cadre-dispatch.sh').read_text()
        self.assertTrue('"bol-$gid" "$bp/sandbox" 4 bolme' in kaynak, 'bolme cagrisi bolme son suresiyle')
        self.assertTrue("son_sure(os.environ['SURE_ROL'])" in kaynak and 'SURE_ROL="${6:-plan}"' in kaynak, 'plan varsayilani plan suresi')
        import sinirlar
        self.assertEqual((sinirlar.son_sure('bolme'), sinirlar.son_sure('plan')), (900, 1800))   # 4c-4: single source

BOLME_ISCI = """
import os, pathlib, sys
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
from kosu import RunStore
gid = os.environ['CADRE_GOREV_ID']; wt = pathlib.Path(os.environ['CADRE_WORKTREE'])
if gid == 'g1' or gid == os.environ.get('FIXTURE_ALT_BASARISIZ'):
    sys.exit(1)
(wt / 'product.txt').write_text((wt / 'product.txt').read_text() + gid + '\\n')
RunStore().task_update(os.environ['CADRE_RUN_ID'], gid, state='uretti')
"""
ALT_PLAN = json.dumps({'is_id': 'g1-bol', 'gorevler': [alt('a', ['product.txt']), alt('b', ['product.txt'], ['a'])]})


class BolmeUctanUcaTest(unittest.TestCase):
    """Real dispatcher: the main lane exhausts the chain -> split at very hard level -> produces sub-tasks -> acceptance."""
    def setUp(self):
        import test_sonuc
        self.f = test_sonuc.CompletionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def kos(self, plan_ek=None, **env):
        rc = self.f.dispatch_fixture('GECER', worker=BOLME_ISCI, env={'FIXTURE_ALT_PLAN': ALT_PLAN, **env}, plan_ek=plan_ek)
        kayit = self.f.store.read(self.f.rid)
        log = (self.f.store.path(self.f.rid) / 'dispatch.log').read_text()
        return rc, kayit, log

    def test_cok_zor_seviyede_basarisiz_serit_bolunup_tamamlanir(self):
        rc, kayit, log = self.kos(plan_ek={'kapsam_puani': 70})
        self.assertEqual((rc, kayit['state']), (0, 'completed'), log[-3000:])
        self.assertIn('BOLUNUYOR', log)
        sonuc = {g: kayit['tasks'][g].get('dispatch_result') for g in ('g1', 'g1-1', 'g1-2')}
        self.assertEqual(sonuc, {'g1': 'uretti', 'g1-1': 'uretti', 'g1-2': 'uretti'})
        yeni = json.loads((self.f.store.path(self.f.rid) / 'plan.json').read_text())
        self.assertEqual(yeni['gorevler'][0]['bolundu'], ['g1-1', 'g1-2'])
        self.assertIn('g1-2', (self.f.wt / 'product.txt').read_text())

    def test_alt_gorev_basarisizsa_ust_gorev_basarisiz_kalir(self):
        rc, kayit, log = self.kos(plan_ek={'kapsam_puani': 70}, FIXTURE_ALT_BASARISIZ='g1-2')
        self.assertEqual(rc, 1)
        self.assertEqual(kayit['tasks']['g1'].get('dispatch_result'), 'basarisiz')
        self.assertIn('alt gorev g1-2', log)

    def test_orta_seviyede_bolme_denenmez(self):
        rc, _kayit, log = self.kos()
        self.assertEqual(rc, 1)
        self.assertNotIn('BOLUNUYOR', log)


if __name__ == '__main__':
    unittest.main()
