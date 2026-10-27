"""Budget ladder (budget QA 24.09, user decision): on a prepaid provider (DeepSeek, zen, OpenAI) the unused share
carries over to the next day (K1: daily share = fresh share + accumulation, fixed at the start of the day; no floor). The position in the chain comes from the ratio of
the money that can be spent today to the typical job cost in that role (ledger p75 of the last 7 days): not enough for half a job stays out and accumulates,
up to one job it is a fallback (before the last resort), covering 1.25 jobs it is active. No model/network."""

from __future__ import annotations

import datetime
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import call, patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import butce  # noqa: E402
import yonlendirici as yon  # noqa: E402
from test_zen import sahte_db  # noqa: E402
from test_butce_senaryolari import anlik  # noqa: E402

KATALOG = {'opencode': {'models': {'deepseek-v4-flash': {'cost': {'input': 0.14, 'output': 0.28}},
                                   'minimax-m3': {'cost': {'input': 0.3, 'output': 1.2}},
                                   'big-pickle': {'cost': {'input': 0, 'output': 0}},
                                   'nemotron-3-ultra-free': {'cost': {'input': 0, 'output': 0}}}},
           'deepseek': {'models': {'deepseek-v4-pro': {'cost': {'input': 0.5, 'output': 2}},
                                   'deepseek-v4-flash': {'cost': {'input': 0.14, 'output': 0.28}}}},
           'opencode-go': {'models': {'glm-5.3-flash': {'cost': {'input': 0.1, 'output': 0.4}},
                                      'qwen3.8-flash': {'cost': {'input': 0.1, 'output': 0.4}},
                                      'qwen3.7-plus': {'cost': {'input': 0.4, 'output': 1.6}}}},
           'cerebras': {'models': {'qwen-3.8-27b': {'cost': {'input': 0.6, 'output': 1.2}}}},
           'openai': {'models': {'gpt-6.1-sol': {'cost': {'input': 1, 'output': 8}}}}}


def katalog_ortami(test):
    t = tempfile.TemporaryDirectory(prefix='merdiven-katalog-')
    test.addCleanup(t.cleanup)
    (Path(t.name) / 'models.json').write_text(json.dumps(KATALOG))
    ctx = patch.dict(os.environ, {'CADRE_MODEL_KATALOG': str(Path(t.name) / 'models.json')})
    ctx.__enter__()
    test.addCleanup(ctx.__exit__, None, None, None)


def snapshot(**saglayici):
    """anlik() observation: provider=(share, available) dollars; Go dollars are converted to the points of the $15 model (4c-13)."""
    ek = {k: {'pay': v[0], 'kullanilabilir': v[1]} if k != 'go' else {'pay': v[0] * 100 / 15, 'kullanilabilir': v[1] * 100 / 15}
          for k, v in saglayici.items() if k != 'is_maliyeti'}
    if 'oa' in ek:
        ek['oa'].update(db='ok')
    if 'or' in ek:
        ek['or'].update(db='ok')
    return anlik(**ek, is_maliyeti=saglayici.get('is_maliyeti', {}))




class MerdivenTest(unittest.TestCase):
    def setUp(self):
        katalog_ortami(self)

    def test_uc_seviye_is_maliyetine_gore(self):
        is_m = {'seritler': {'opencode': 0.1}}
        for kul, beklenen in ((0.049 + 0.0525, 'disarida'), (0.08 + 0.0525, 'yedek'), (0.13 + 0.0525, 'aktif')):
            with self.subTest(beklenen=beklenen):
                b = snapshot(zen=(0.35, kul), is_maliyeti=is_m)
                self.assertEqual(yon.merdiven('opencode/deepseek-v4-flash', 'impl', b), beklenen)

    def test_kapinin_tek_cagrisini_karsilamayan_para_disarida(self):
        """B221 (25.09 live R10-hard): OpenAI's money for today was $0.048, a plan job $0.027 -> counted as 'aktif'; the gate however holds a
        ~$0.33 calibrated reservation for a single call and denied the attempt. Unit = max(typical job, typical reservation); money that does not cover the
        reservation stays out and accumulates. Without a measurement in the role bucket the provider-wide ('*') one is used."""
        is_m = {'plan': {'opencode': 0.027}}
        for rez in ({'plan': {'opencode': 0.33}}, {'*': {'opencode': 0.33}}):
            for kul, beklenen in ((0.063, 'disarida'), (0.30, 'disarida'), (0.37, 'yedek'), (0.5, 'aktif')):
                with self.subTest(rez=rez, kul=kul):
                    b = snapshot(zen=(0.1, kul), is_maliyeti=is_m)
                    b['rezervasyon'] = rez
                    self.assertEqual(yon.saglayici_merdiveni('opencode', 'plan', b), beklenen)
        b = snapshot(zen=(0.1, 0.063), is_maliyeti=is_m)
        self.assertEqual(yon.saglayici_merdiveni('opencode', 'plan', b), 'aktif', 'rezervasyon olcumu yoksa eski olcu')


    def test_bedava_ve_claude_merdivene_tabi_degil(self):
        b = snapshot(zen=(0.35, 0.0), is_maliyeti={'seritler': {'opencode': 1.0}})
        self.assertIsNone(yon.merdiven('opencode/big-pickle', 'impl', b))
        self.assertIsNone(yon.merdiven('claude/sonnet#high', 'plan', b))

    def test_olcum_yoksa_rol_varsayilani(self):
        self.assertEqual(yon.is_maliyeti('kalite', 'openai', {}), yon.IS_MALIYETI_VARSAYILAN['hakem'])
        self.assertEqual(yon.is_maliyeti('impl', 'opencode', {'is_maliyeti': {'seritler': {'_': 0.2}}}), 0.2)

    def test_zincirde_disaridaki_elenir_ve_zincir_yerel_sonda_degil(self):
        # D13 (03.10): impl chain = the three provider routes of ds-v4-pro; there is NO local link in the chain (P3).
        # abundance on Go is not enough for half a job (job $0.05 > remaining $0.02) -> out, money accumulates;
        # the OpenRouter/DeepSeek routes stay active. No fallback: today's money in the container is at full-job level.
        from unittest.mock import patch as _p
        t = tempfile.TemporaryDirectory(prefix='merdiven-qe-')
        self.addCleanup(t.cleanup)
        b = snapshot(go=(0.02, 0.02), ds=(0.2, 0.1), is_maliyeti={'seritler': {'opencode-go': 0.05, 'deepseek': 0.05}})
        with _p.object(yon, 'QE', Path(t.name)), _p.object(yon, 'butce_var', return_value=(True, 'test')), \
             _p.object(yon, 'sikisik_mi', return_value=False):
            z, neden = yon.zincir('impl', 'orta', 20000, b, {})
        self.assertNotIn('opencode-go/deepseek-v4-pro', z, 'go yarim ise bile yetmiyor: disarida, para birikir')
        self.assertEqual(set(z), {'openrouter/deepseek/deepseek-v4-pro', 'openrouter/z-ai/glm-5.3'})
        self.assertTrue(any('merdiven' in n for n in neden), neden)

    def test_yeterli_butceli_modellerde_rol_onceligi_korunur(self):
        # D16: if both models cover the call's money, the model priority
        # does not change with the size of the wallet; without a measurement the role decision applies.
        from unittest.mock import patch as _p
        t = tempfile.TemporaryDirectory(prefix='merdiven-qe-')
        self.addCleanup(t.cleanup)
        b = snapshot(**{'or': (0.3, 0.075)}, go=(9.0, 0.05))
        secilir = {'openrouter/openai/gpt-6.1-sol', 'opencode-go/qwen3.8-max'}
        with _p.object(yon, 'QE', Path(t.name)), _p.object(yon, 'butce_var',
                side_effect=lambda sag, m, *a, **k: (m in secilir, 'test')), \
             _p.object(yon, 'sikisik_mi', return_value=False):
            z = yon.zincir('plan', 'orta', 20000, b, {})[0]
        self.assertEqual(z, ['openrouter/openai/gpt-6.1-sol', 'opencode-go/qwen3.8-max'], z)   # 4c-6: role order Sol > qwen

    def test_yazar_zinciri_iki_cagriyi_karsilamayan_saglayiciyi_atlar(self):
        """B239's D13 adaptation: the report writer is a long lane; trying a provider whose daily share barely covers a single lane call
        and failing at the gate burns time and chain allowance. The 'yazar' role = plan
        chain + breathing filter; with enough share for Sol (openrouter) in the plan chain the writer skips the breathless provider."""
        from unittest.mock import patch as _p
        t = tempfile.TemporaryDirectory(prefix='merdiven-qe-')
        self.addCleanup(t.cleanup)
        b = snapshot(go=(8.5, 8.2), is_maliyeti={'plan': {'opencode-go': 0.07, 'openrouter': 0.03}})
        b['or'] = {'db': 'ok', 'pay': 0.2, 'kullanilabilir': 0.079}
        b['rezervasyon'] = {'seritler': {'openrouter': 0.042, 'opencode-go': 0.05}}
        secilir = {'openrouter/openai/gpt-6.1-sol', 'opencode-go/qwen3.8-max'}
        with _p.object(yon, 'QE', Path(t.name)), _p.object(yon, 'butce_var',
                side_effect=lambda sag, m, *a, **k: (m in secilir, 'test')), \
             _p.object(yon, 'sikisik_mi', return_value=False):
            yazar, neden = yon.zincir('yazar', 'orta', 30000, b, {})
            plan = yon.zincir('plan', 'orta', 30000, b, {})[0]
        self.assertIn('openrouter/openai/gpt-6.1-sol', plan, 'plan zinciri degismez: tek cagri plana yeter')
        self.assertNotIn('openrouter/openai/gpt-6.1-sol', yazar, 'yazar icin 0,049 < 2 x 0,042')
        self.assertEqual(yazar[0], 'opencode-go/qwen3.8-max', yazar)
        self.assertTrue(any('yazar' in n and 'openrouter' in n for n in neden), neden)

    def test_yazar_zinciri_kosu_cebine_iki_cagri_sigmayan_saglayiciyi_atlar(self):
        """B239b (D13 adaptation): even if the provider's daily share is plentiful, if the RUN POCKET (lane slice) does not cover two Claude calls
        the first call empties the pocket and the writer is left partial. The breathing filter measures the run pocket too; in the D13 plan
        chain the paid counterpart of this scenario is Go qwen3.8-max."""
        from unittest.mock import patch as _p
        t = tempfile.TemporaryDirectory(prefix='merdiven-qe-')
        self.addCleanup(t.cleanup)
        b = snapshot(go=(8.5, 8.2))
        b['rezervasyon'] = {'seritler': {'opencode-go': 0.05, 'claude': 1.31}}
        b['claude'] = {'kaynak': 'taze', 'pay': 3, 'yuzde1_usd': 10, 'havuz_kalan_usd': 20.0}   # share<4: sonnet is chosen
        secilir = {'claude/sonnet#high', 'opencode-go/qwen3.8-max'}
        with _p.object(yon, 'QE', Path(t.name)), _p.object(yon, 'butce_var',
                side_effect=lambda sag, m, *a, **k: (m in secilir, 'test')), \
             _p.object(yon, 'kosu_cebi_kalan', return_value=1.29), \
             _p.object(yon, 'allowance_usd', side_effect=lambda b_, sag, *_: 50.0):
            yazar, neden = yon.zincir('yazar', 'orta', 30000, b, {})
        self.assertNotIn('claude/sonnet#high', yazar, 'kosu cebi 1,29 < 2 x 1,31')
        self.assertEqual(yazar[0], 'opencode-go/qwen3.8-max', yazar)
        self.assertTrue(any('yazar' in n and 'claude' in n and 'kosu cebi' in n for n in neden), neden)

    def test_kapi_rezervasyonu_kalibre_degeri_varsa_onu_kullanir(self):
        """B253: if the gate now reserves with the calibrated cap, the breathing filters (B239/B242) must measure the same number; the old p50 is only a fallback."""
        b = snapshot(go=(8.5, 8.2))
        b['rezervasyon'] = {'hakem': {'claude': 1.72}, '*': {'claude': 1.6}}
        b['kalibre'] = {'hakem': {'claude': 0.615}}
        self.assertAlmostEqual(yon.kapi_rezervasyonu('kalite', 'claude', b), 0.615, places=4)
        self.assertAlmostEqual(yon.kapi_rezervasyonu('impl', 'claude', b), 1.6, places=4, msg='kalibre yoksa p50 yedegi')
        b['kalibre']['hakem']['opencode-go'] = 0.02
        b['rezervasyon']['hakem']['opencode-go'] = 0.05
        self.assertAlmostEqual(yon.kapi_rezervasyonu('kalite', 'opencode-go', b), 0.05, places=4, msg='Go kapisi kalibre tutmaz: p50 kalir')

    def test_hakem_nefesi_kosu_hakem_cebine_iki_cagri_sigmayan_saglayiciyi_isaretler(self):
        """B242 (26.09 live R14-easy): the Claude judge's first call passed ($0.18), the second did not fit the run's judge pocket -> the link
        was wasted. The judge also makes at least two calls (tool + verdict); if the pocket does not cover two gate reservations the candidate is never called."""
        from unittest.mock import patch as _p
        b = snapshot(go=(8.5, 8.2))
        b['rezervasyon'] = {'hakem': {'claude': 3.636, 'opencode-go': 0.05}}
        with _p.object(yon, 'kosu_cebi_kalan', side_effect=lambda rol='impl', provider='': {'kalite': 1.42}.get(rol, 9.0)) as kalan, \
             _p.object(yon, 'allowance_usd', side_effect=lambda b_, sag, *_: 50.0):
            dar = yon.hakem_nefesi('claude/opus#max', b)
            bol = yon.hakem_nefesi('opencode-go/glm-5.2', b)
        self.assertEqual(dar[:2], (1.42, 2 * 3.636), dar)
        self.assertEqual(dar[2], 'cep')
        self.assertIsNone(bol, 'go rezervasyonu 0,05: 1,42 iki cagriyi karsilar')
        self.assertEqual(kalan.call_args_list, [call('kalite', 'claude'), call('kalite', 'opencode-go')])

    def test_yazar_nefesi_openrouterda_kapinin_en_kotu_tutarini_olcer(self):
        import json
        import tempfile
        from pathlib import Path as _P
        from unittest.mock import patch as _p
        b = snapshot(go=(8.5, 8.2))
        b['rezervasyon'] = {'*': {'openrouter': 0.02}}
        with tempfile.TemporaryDirectory() as d:
            ev = _P(d)
            (ev / 'zincir').mkdir()
            (ev / 'zincir/openrouter-fiyat.json').write_text(json.dumps({'modeller': {'openai/gpt-6.1-sol': {'input': 2, 'output': 10}}}))
            with _p.object(yon, 'QE', ev), _p.object(yon, 'kosu_cebi_kalan', return_value=None):
                with _p.object(yon, 'net_kapasite_usd', return_value=0.14):
                    self.assertIsNotNone(yon.yazar_nefesi('openrouter/openai/gpt-6.1-sol', b), 'OR $0,14 en kotu iki cagriyi karsilamaz')
                with _p.object(yon, 'net_kapasite_usd', return_value=65.0):
                    self.assertIsNone(yon.yazar_nefesi('openrouter/openai/gpt-6.1-sol', b))
            with _p.object(yon, 'QE', ev), _p.object(yon, 'kosu_cebi_kalan', return_value=0.46), \
                 _p.object(yon, 'net_kapasite_usd', return_value=65.0):
                self.assertIsNone(yon.yazar_nefesi('openrouter/openai/gpt-6.1-sol', b), 'cep kapinin kalibre tutariyla olculur')

    def test_kucuk_degisiklik_hakem_sirasinda_yedek_sonda(self):
        import qe_core as core
        b = snapshot(zen=(0.2, 0.05), is_maliyeti={'hakem': {'opencode': 0.07}})
        self.assertEqual(core.hakem_maliyeti('opencode', b), 9)
        b = snapshot(zen=(4.0, 4.0), is_maliyeti={'hakem': {'opencode': 0.07}})
        self.assertEqual(core.hakem_maliyeti('opencode', b), core.HAKEM_MALIYET_SIRASI['opencode'])


class ButceKuralTest(unittest.TestCase):
    """Three rules missing tests in the budget matrix (budget QA 24.09): the Claude plan on a small job, an easy job on local, the cap source."""
    def setUp(self):
        katalog_ortami(self)
        t = tempfile.TemporaryDirectory(prefix='merdiven-kural-')
        self.addCleanup(t.cleanup)
        self.ev = Path(t.name)
        ctx = patch.dict(os.environ, {'CADRE_HOME': str(self.ev), 'CADRE_YON_DURUM': str(self.ev / 'd.json'), 'CADRE_YON_KAYIT': '/dev/null'})
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def test_kucuk_iste_claude_plan_yapmaz(self):
        b = snapshot()
        with patch.object(yon, 'butce_var', return_value=(True, 'test')), patch.object(yon, 'sikisik_mi', return_value=False):
            kucuk = yon.zincir('plan', 'kucuk', 20000, b, {})[0]
            orta = yon.zincir('plan', 'orta', 20000, b, {})[0]
        self.assertFalse(any(m.startswith('claude/') for m in kucuk), kucuk)
        self.assertTrue(orta[0].startswith('claude/'), orta)

    def test_kolay_is_odenmis_modele_verilmez(self):
        # D13/P3 (03.10): difficulty 1 -> impl-light; NO local link in the chain. If the budget is not visible, paid links
        # are removed and katilik_modeli returns None (rc 76 'no-capacity' in the subprocess); the old behaviour (difficulty 1 + paid
        # model -> local) was reverted — the user decision (P3) is kept, as the test dispatch showed.
        with patch.object(yon, 'butce_oku', return_value={}):
            self.assertEqual(yon.zorluk_modeli('opencode-go/kimi-k3', 1, 'impl'), None,
                             'zorluk 1 + odenmis model; yerel kapali, butce yok -> kapasite yok')

    def test_tavani_planlayici_belirler_harcanan_ve_operator_korunur(self):
        import seviye
        from kosu import RunStore
        (self.ev / 'projects/a').mkdir(parents=True)
        (self.ev / 'projects/a/kosu-tavan-usd').write_text('4.0')
        store = RunStore(self.ev)
        kos = lambda puan, harcanan=0.0: (lambda rid: (store.update(rid, budget_spent_usd=harcanan),
                                                       (store.path(rid) / 'plan.json').write_text(json.dumps({'kapsam_puani': puan})),
                                                       seviye.uygula(rid, 'a'), store.read(rid)['budget_cap_usd'])[-1])(
            store.accept('a', 'bir fonksiyon ekle')['run_id'])
        self.assertEqual(kos(30), 4.0, 'kayitli kullanici tavani korunur')
        self.assertEqual(kos(80), 4.0, 'plan kullanici tavanini buyutemez')
        self.assertEqual(kos(30, harcanan=3.0), 4.0, 'harcama tavani degistirmez')
        with patch.dict(os.environ, {'CADRE_KOSU_TAVAN_USD': '7'}):
            self.assertEqual(kos(30), 7.0, 'operator ayari korunur')


if __name__ == '__main__':
    unittest.main()
