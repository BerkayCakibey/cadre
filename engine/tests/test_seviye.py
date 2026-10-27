"""G-E9 scope score and level: points 0–100 bound to human-time bands;
the source has two layers (rule floor lower bound + planner estimate), final = max. The level gives the cap, the correction round and the plan
ceiling from one place. No model/network."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import seviye  # noqa: E402


class AutoTahminTest(unittest.TestCase):
    def test_untitled_auto_tasks_count_a_default_estimate_toward_the_plan_cap(self):
        """06.10 QA: `auto` always resolves to a paid model; with an estimate of 0 the cap fell to the level floor."""
        plan = {'gorevler': [{'model': 'auto'}] * 8}
        self.assertEqual(seviye.plan_tavani(plan, 1.0), round(8 * seviye.AUTO_TAHMIN_USD * 1.5, 2))
        self.assertEqual(seviye.plan_tavani({'gorevler': [{'model': 'auto', 'tahmin_usd': 2.0}]}, 1.0), 3.0)
        self.assertEqual(seviye.plan_tavani({'gorevler': [{'model': 'gem-flash'}]}, 1.0), 1.0)


class SeviyeTablosuTest(unittest.TestCase):
    def test_plan_estimates_set_the_initial_cap_and_manual_override_wins(self):
        import json
        import os
        import tempfile
        from unittest.mock import patch
        from kosu import RunStore
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'CADRE_HOME': d, 'CADRE_KOSU_TAVAN_USD': ''}):
            store = RunStore()
            rid = store.accept('fixture', 'README.md yazim hatasini duzelt')['run_id']
            plan = store.path(rid) / 'plan.json'
            plan.write_text(json.dumps({'kapsam_puani': 25, 'gorevler': [{'tahmin_usd': 2}, {'tahmin_usd': 1}]}))
            self.assertEqual(seviye.uygula(rid, 'fixture')['tavan_usd'], 2)
            self.assertEqual(store.read(rid)['budget_plan_cap_usd'], 4.5)
            plan.write_text(json.dumps({'kapsam_puani': 25, 'gorevler': [{'tahmin_usd': .1}]}))
            self.assertEqual(seviye.uygula(rid, 'fixture')['tavan_usd'], 2)
            store.update(rid, budget_spent_usd=5)
            self.assertEqual(seviye.uygula(rid, 'fixture')['tavan_usd'], 2)
            store.update(rid, budget_cap_usd=7)
            with patch.dict(os.environ, {'CADRE_KOSU_TAVAN_USD': '7'}):
                self.assertEqual(seviye.uygula(rid, 'fixture')['tavan_usd'], 7)
                self.assertEqual(store.read(rid)['operator_budget_usd'], 2)
            for value in (-1, True, float('inf'), 'bad'):
                with self.assertRaises(ValueError):
                    seviye.plan_tavani({'gorevler': [{'tahmin_usd': value}]}, 2)

    def test_bant_sinirlari(self):
        for puan, ad in ((0, 'basit'), (15, 'basit'), (16, 'orta'), (40, 'orta'), (41, 'zor'), (65, 'zor'),
                         (66, 'cok-zor'), (85, 'cok-zor'), (86, 'ultra'), (100, 'ultra')):
            with self.subTest(puan=puan):
                self.assertEqual(seviye.seviye(puan)['ad'], ad)

    def test_seviye_tavan_tur_ve_plan_siniri_verir(self):
        self.assertEqual({k: seviye.seviye(10)[k] for k in ('tavan_usd', 'duzeltme_tur', 'plan_en_cok')}, {'tavan_usd': 1.0, 'duzeltme_tur': 1, 'plan_en_cok': 1})
        self.assertEqual(seviye.seviye(30)['tavan_usd'], 2.0)
        self.assertEqual(seviye.seviye(50)['tavan_usd'], 5.0)
        self.assertEqual(seviye.seviye(70)['tavan_usd'], 10.0)
        self.assertEqual(seviye.seviye(70)['duzeltme_tur'], 2, 'arastirma: 2 turdan fazlasi verimsiz')

    def test_gecersiz_puan_sinira_kirpilir(self):
        self.assertEqual(seviye.seviye(-5)['ad'], 'basit')
        self.assertEqual(seviye.seviye(250)['ad'], 'ultra')


class KuralTabaniTest(unittest.TestCase):
    def test_kisa_tek_dosya_basit(self):
        self.assertLessEqual(seviye.kural_tabani('README.md dosyasindaki yazim hatasini duzelt'), 15)

    def test_premium_kelimesi_zor_tabanina_ceker(self):
        self.assertGreaterEqual(seviye.kural_tabani('auth katmanini yeniden tasarla'), 41)

    def test_sifirdan_proje_ultra(self):
        self.assertGreaterEqual(seviye.kural_tabani('bu fikirle sifirdan yeni bir proje kur: web arayuzu, api, veritabani'), 86)

    def test_sayisal_sifirdan_ifadesi_proje_sayilmaz(self):
        """B215 (24.09 R6-easy): 'sifirdan farkli ofset' (a non-zero offset) made an easy test job ultra. Comparison patterns
        (farkli/buyuk/kucuk/fazla/az/esit) are not project markers; a size adjective like 'sifirdan kucuk BIR arac yaz' is a marker."""
        for istek in ('RFC-2822 ofseti sifirdan farkli olan durumlari test et', 'sıfırdan farklı ofsetli tarih',
                      'deger sifirdan buyuk olmali', 'sıfırdan küçük sayılar reddedilir', 'Sıfırdan küçük ya da eşit değer',
                      'bakiye sifirdan az ise uyar'):
            with self.subTest(istek=istek):
                self.assertLess(seviye.kural_tabani(istek), 86)
        for istek in ('Sıfırdan küçük bir komut satırı not aracı yaz', 'sifirdan buyuk bir proje kur',
                      'sifirdan bir web uygulamasi yaz', 'write it from scratch'):
            with self.subTest(istek=istek):
                self.assertGreaterEqual(seviye.kural_tabani(istek), 86)

    def test_kod_kimligindeki_anahtar_sozcuk_seviye_yukseltmez(self):
        """B216 (24.09 R6-easy): the name `normalize_timestamp` tripped the pre-filter's hard word 'normalize', a test-only job became hard (50).
        Code identifiers (backticks or underscored names) do not count as keywords; a word in plain text does."""
        for istek in ("`tests/unit/test_canonical.py`'ye `normalize_timestamp` icin eksik durumlari test et",
                      'country_registry.py dosyasina normalize_alpha2 ekle ve test et',
                      '`sifirdan_baslat` fonksiyonundaki hatayi duzelt'):
            with self.subTest(istek=istek):
                self.assertLess(seviye.kural_tabani(istek), 41)
        self.assertGreaterEqual(seviye.kural_tabani('veritabani semasini normalize et, sonra testleri guncelle'), 41)
        self.assertEqual(seviye.kural_tabani('`a.py`, `b.py` dosyalarini guncelle'), 25, 'boyut sezgisi (iki dosya adi -> GECER) degismez')

    def test_proje_cocugunun_baglami_kural_tabanini_sisirmez(self):
        """G-P1: the original request (context) at the end of a feature prompt is not scored; otherwise every child would be very hard because of 'sifirdan'."""
        import proje
        d = {'ozellikler': [{'id': 'f1', 'baslik': 'b', 'durum': 'bekliyor'}]}
        f = {'id': 'f1', 'baslik': 'kayit listesi', 'aciklama': 'README.md dosyasina kurulum satiri ekle', 'kabul': 'satir var'}
        istem = proje.ozellik_istemi('sifirdan yeni bir proje kur: web arayuzu, api, veritabani', d, f)
        self.assertIn(seviye.BAGLAM_ISARETI, istem)
        self.assertLess(seviye.kural_tabani(istem), 86)

    def test_cok_dosyali_uzun_istek_orta_ustu(self):
        istek = ('src/a.py, src/b.py ve tests/test_a.py dosyalarini guncelle; ' * 3) + 'sonra belgeyi duzelt ve testleri ekle.'
        self.assertGreaterEqual(seviye.kural_tabani(istek), 16)


class OnElekTest(unittest.TestCase):
    """B222: the dispatch PREMIUM flag (the B199 hard cap, the 'zor' class chain) does not trip over code names and substrings; 9 of the
    10 PREMIUMs on 23-24.09 came from here (7 `normalize_*`, 2 'race' inside 'traceback'). The whole golden set runs here too (RC51)."""
    def test_premium_kod_adina_ve_alt_dizgiye_takilmaz(self):
        import onelek
        self.assertEqual(onelek.karar("`tests/unit/test_canonical.py`'ye `normalize_timestamp` icin 4 test ekle ve mevcut testleri yesil tut")[1], '-')
        self.assertEqual(onelek.karar('hata ciktisinda traceback yerine anlasilir mesaj goster ve cli testlerini guncelle')[1], '-')
        self.assertEqual(onelek.karar("`cache.py` icindeki race condition'i bul ve coz")[1], 'PREMIUM')

    def test_llm_tek_basina_premium_veremez(self):
        """OBSERVATION-1 (25.09, user approval): a free LLM raises the rule floor at most to DISPATCH; for PREMIUM (the B199 $5 cap, the 'zor'
        class) the rule must say PREMIUM too. On the night of 25.09 the LLM alone made 2/2 medium jobs PREMIUM (two Opus plans, $0.91)."""
        import json, os, shutil, subprocess, tempfile
        with tempfile.TemporaryDirectory() as t:
            kok = Path(t)
            for ad in ('onelek.py', 'kosu.py', 'runtime_sync.py', 'seviye.py'):
                shutil.copyfile(LIB / ad, kok / ad)
            (kok / 'plan_ds.py').write_text("print('PREMIUM')\n")   # the LLM says PREMIUM for every job
            butce = json.dumps({'go': {'oran': 1}, 'ds': {'oran': 1}, 'claude': {'pay': 50}})

            def sinifla(istek):
                (kok / 'istek.md').write_text(istek)
                env = {**os.environ, 'CADRE_HOME': t, 'CADRE_ONELEK_LLM': '1', 'CADRE_ONELEK_ARALIK_SN': '0'}
                env.pop('CADRE_RUN_ID', None)
                r = subprocess.run([sys.executable, str(kok / 'onelek.py'), str(kok / 'istek.md')], input=butce, env=env,
                                   capture_output=True, text=True, timeout=30)
                return r.stdout.split()
            self.assertEqual(sinifla("`src/feedloom/timezones.py`'ye çakışma hesabı ekle ve 6 test yaz"), ['GECER', '-'])
            self.assertEqual(sinifla('versiyonu 1.2.3 yap'), ['GECER', '-'], 'LLM kisa isi dagitima yukseltebilir, PREMIUM yapamaz')
            self.assertEqual(sinifla('auth katmanini yeniden tasarla'), ['GECER', 'PREMIUM'], 'kural PREMIUM ise PREMIUM kalir')

    def test_groq_istekleri_arasi_en_az_bes_saniye(self):
        """4c-11: the pre-filter is only Groq gpt-oss-120b; for the free tier >=5 s between requests (stamp file, locked)."""
        import tempfile
        import onelek
        uykular = []
        with tempfile.TemporaryDirectory() as t:
            onelek.aralik(t, simdi=100.0, uyu=uykular.append)
            onelek.aralik(t, simdi=102.0, uyu=uykular.append)     # after 2 s: waits 3 s
            onelek.aralik(t, simdi=200.0, uyu=uykular.append)     # much later: does not wait
            onelek.aralik(t, simdi=50.0, uyu=uykular.append)      # a clock going backwards: the stamp is ignored
        self.assertEqual([round(u, 3) for u in uykular], [3.0])

    def test_altin_set_tamami(self):
        import onelek
        from altin_set import VAKALAR
        for istek, gecer, prem in VAKALAR:
            with self.subTest(istek=istek[:40]):
                self.assertEqual(onelek.karar(istek), ('GECER' if gecer else 'ATLA', 'PREMIUM' if prem else '-'))


class BirlestirmeTest(unittest.TestCase):
    def test_nihai_puan_kural_ile_planlayicinin_buyugu(self):
        self.assertEqual(seviye.birlestir(20, 55), 55)
        self.assertEqual(seviye.birlestir(50, 10), 50, 'planlayici dusuk tahminle kural tabaninin altina indiremez')
        self.assertEqual(seviye.birlestir(20, None), 20)
        self.assertEqual(seviye.birlestir(20, 'x'), 20)
        self.assertEqual(seviye.birlestir(20, 130), 100)



class PlanSiniriTest(unittest.TestCase):
    """G-E9 plan ceiling: the task count cannot exceed what the final level (max(rule, planner)) allows;
    a violation goes back to the planner like a schema error (merge, or raise the score with a justification)."""
    def plan(self, n, puan=None):
        p = {'gorevler': [{'id': f'g{i}'} for i in range(1, n + 1)]}
        if puan is not None:
            p['kapsam_puani'] = puan
        return p

    def test_orta_plan_ucten_fazla_gorev_tasiyamaz(self):
        hata = seviye.plan_siniri(self.plan(4, 30), kural=25)
        self.assertIn('4 gorev', hata)
        self.assertIn('en cok 3', hata)
        self.assertIn('kapsam_puani', hata, 'planlayiciya iki cikis yolu soylenir')
        self.assertIsNone(seviye.plan_siniri(self.plan(3, 30), kural=25))

    def test_kural_tabani_seviyeyi_yukseltirse_sinir_da_yukselir(self):
        self.assertIsNone(seviye.plan_siniri(self.plan(5, 30), kural=50))

    def test_puansiz_plan_kural_tabanina_gore_sinirlanir(self):
        self.assertIsNotNone(seviye.plan_siniri(self.plan(2), kural=10))
        self.assertIsNone(seviye.plan_siniri(self.plan(1), kural=10))

    def test_planlayici_istemi_seviye_tablosuyla_ayni_sayilari_soyler(self):
        """Single source seviye.py: the bands and task limits in the dispatcher prompt must not drift from the table."""
        kaynak = (LIB.parent / 'bin' / 'cadre-dispatch.sh').read_text()
        alt = 0
        for s in seviye.SEVIYELER:
            self.assertIn(f"{alt}-{s['ust']} ", kaynak, s['ad'])
            alt = s['ust'] + 1
        adlar = {'basit': 'basit', 'orta': 'orta', 'zor': 'zor', 'cok-zor': 'cok zor'}
        beklenen = ', '.join(f"{adlar[s['ad']]} {'' if s['plan_en_cok'] == 1 else 'en cok '}{s['plan_en_cok']}"
                             for s in seviye.SEVIYELER if s['ad'] in adlar)
        self.assertIn(f'Gorev sayisi puana uysun ({beklenen})', kaynak)


class SeviyeUygulaUctanUcaTest(unittest.TestCase):
    """Real dispatcher: after the plan the level is written to the record, the cap comes from the level, the log gets a KAPSAM line."""
    def setUp(self):
        import os
        import test_sonuc
        self.f = test_sonuc.CompletionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.eski = os.environ.pop('CADRE_KOSU_TAVAN_USD', None)
        self.addCleanup(lambda: self.eski is not None and os.environ.__setitem__('CADRE_KOSU_TAVAN_USD', self.eski))

    def kos(self, **plan_ek):
        self.f.dispatch_fixture('GECER', plan_ek=plan_ek)
        return self.f.store.read(self.f.rid), (self.f.store.path(self.f.rid) / 'dispatch.log').read_text()

    def test_planlayici_puani_seviyeyi_ve_tavani_belirler(self):
        kayit, log = self.kos(kapsam_puani=70, kapsam_gerekce='modul')
        self.assertEqual((kayit['kapsam']['seviye'], kayit['kapsam']['planlayici'], kayit['budget_cap_usd']), ('cok-zor', 70, 2.0))
        self.assertIn('KAPSAM: puan 70 -> seviye cok-zor', log)

    def test_proje_cocugu_ultra_olamaz(self):
        """G-P1: a project-mode child run does not enter project mode again; its level is at most very hard."""
        parent = self.f.store.accept('lab', 'parent project')['run_id']
        self.f.store.update(self.f.rid, proje_ust=parent)
        kayit, _log = self.kos(kapsam_puani=95)
        self.assertEqual((kayit['kapsam']['seviye'], kayit['kapsam']['puan']), ('cok-zor', 85))

    def test_puansiz_planda_kural_tabani_gecerlidir(self):
        kayit, log = self.kos()
        self.assertEqual(kayit['kapsam']['planlayici'], None)
        self.assertEqual(kayit['kapsam']['puan'], kayit['kapsam']['kural'])
        self.assertIn('KAPSAM: puan', log)


if __name__ == '__main__':
    unittest.main()
