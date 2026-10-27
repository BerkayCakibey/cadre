"""4c-17: code model fallback + a model breaker at error level. The error is written to the router state at (model, route) level (it does not
enter the task bans); that route closes first, and if the model fails on all routes the next model in the chain is used (through the hand-off counter),
the provider closes only when >=2 of its models fail or on an account signal; share/window exhaustion is not a failure."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import qe_core as core  # noqa: E402
import yonlendirici as yon  # noqa: E402
from katalog import kanonik_model  # noqa: E402
from kosu import RunStore  # noqa: E402

DS_GO, DS_OR = 'opencode-go/deepseek-v4-pro', 'openrouter/deepseek/deepseek-v4-pro'
GLM_GO, QWEN_GO, QWEN_OR = 'opencode-go/glm-5.3', 'opencode-go/qwen3.8-max', 'openrouter/qwen/qwen3.8-max-0902'


class KesiciTabani(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-kesici-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'zincir').mkdir()
        for ctx in (patch.object(yon, 'DURUM', self.home / 'zincir/saglayici-durum.json'),
                    patch.object(yon, 'KAYIT', self.home / 'zincir/yonlendirici.jsonl'),
                    patch.object(yon, 'QE', self.home)):
            ctx.__enter__()
            self.addCleanup(ctx.__exit__, None, None, None)

    def acik(self, *yollar):
        """Budget/account filter: only the given routes are open (the breaker is the chain's own filter, not faked separately)."""
        acik = set(yollar)
        return patch.multiple(yon, butce_var=lambda sag, m, *a, **k: (m in acik, 'test'), merdiven=lambda *a, **k: 'aktif')


class RouterKesiciTests(KesiciTabani):
    def test_bir_yolun_hatasi_yalniz_o_yolu_kapatir(self):
        yon.ariza('opencode-go', 'requires Global regions', allowed_fails=1, model=DS_GO)
        with self.acik(DS_GO, DS_OR, GLM_GO):
            z, neden = yon.zincir('impl', b={'go': {}}, d=None)
        self.assertEqual(z, [DS_OR, GLM_GO], 'ayni modelin OR yolu ve Go glm acik; Go saglayicisi kapanmadi')
        self.assertTrue(any('model cooldown' in n for n in neden), neden)
        self.assertFalse(yon.cooldownda('opencode-go'))
        self.assertTrue(yon.cooldownda('opencode-go', model=DS_GO))

    def test_ayni_saglayicida_iki_model_duserse_saglayici_kapanir(self):
        yon.ariza('opencode-go', 'requires Global regions', allowed_fails=1, model=DS_GO)
        self.assertFalse(yon.cooldownda('opencode-go'))
        yon.ariza('opencode-go', '500 internal', allowed_fails=1, model=GLM_GO)
        self.assertTrue(yon.cooldownda('opencode-go'), 'iki model dustu: Go yasak')
        with self.acik(DS_GO, DS_OR, GLM_GO, QWEN_GO):
            self.assertEqual(yon.zincir('impl', b={'go': {}})[0], [DS_OR])

    def test_hesap_sinyali_saglayiciyi_hemen_kapatir(self):
        for hata in ('402 Payment Required', 'insufficient balance', 'monthly spend limit reached', '401 Unauthorized'):
            with self.subTest(hata=hata):
                yon.durum_yaz({})
                yon.ariza('openrouter', hata, model=DS_OR)
                self.assertTrue(yon.cooldownda('openrouter'), hata)

    def test_basari_model_sayacini_sifirlar_ve_anahtar_tam_kimliktir(self):
        yon.ariza('opencode-go', 'timeout', model=DS_GO)
        yon.ariza('opencode-go', 'timeout', model=DS_GO)
        d = yon.durum_oku()
        self.assertEqual(d[DS_GO]['hata'], 2)
        self.assertNotIn('opencode-go', d, 'model hatasi saglayici sayacina yazilmaz')
        yon.basari('opencode-go', model=DS_GO)
        self.assertEqual(yon.durum_oku()[DS_GO]['hata'], 0)
        self.assertEqual(yon.model_anahtari('claude', 'claude/opus#high'), 'claude/opus')
        self.assertEqual(yon.model_anahtari('openrouter', 'deepseek/deepseek-v4-pro'), DS_OR)

    def test_kod_zinciri_ds_glm_her_model_icinde_go_once_qwen_yalniz_hakem(self):
        with self.acik(*yon.KOD_ZINCIRI):
            z = yon.zincir('impl', b={'go': {}})[0]
        self.assertEqual(z, [DS_GO, DS_OR, GLM_GO, 'openrouter/z-ai/glm-5.3'])
        for rol in ('impl', 'impl-hafif', 'birlestirme', 'kurtarici'):
            self.assertEqual(yon.ROLLER[rol], yon.KOD_ZINCIRI)
        self.assertFalse([m for m in yon.KOD_ZINCIRI if 'qwen' in m], 'qwen kod yazmaz, bagimsiz hakem olarak kalir')
        self.assertTrue({QWEN_GO, QWEN_OR} <= set(yon.ROLLER['review']))

    def test_qwen_or_kimligi_takma_adla_ayni_modeldir(self):
        self.assertEqual(kanonik_model(QWEN_OR), kanonik_model(QWEN_GO))
        with self.acik(QWEN_GO, QWEN_OR):
            self.assertEqual(yon.ayni_model_yollari(QWEN_GO, rol='review', b={'go': {}}), [QWEN_OR])

    def test_alternatifler_dusen_yolu_eler_saglayiciyi_degil(self):
        with self.acik(DS_GO, DS_OR, GLM_GO, 'openrouter/z-ai/glm-5.3'):
            self.assertEqual(yon.alternatifler('impl', DS_GO, 3, b={'go': {}}), [DS_OR, GLM_GO, 'openrouter/z-ai/glm-5.3'])

    def test_zen_sol_yalniz_or_sol_sogumadayken_aday(self):
        with self.acik(yon.OR_SOL, yon.ZEN_SOL), patch.dict(yon.ROLLER, {'plan': [yon.OR_SOL, yon.ZEN_SOL]}):
            self.assertEqual(yon.zincir('plan', b={'go': {}})[0], [yon.OR_SOL])
            yon.ariza('openrouter', 'upstream error', allowed_fails=1, model=yon.OR_SOL)
            self.assertEqual(yon.zincir('plan', b={'go': {}})[0], [yon.ZEN_SOL])

    def test_cli_saglayici_model_kimligini_alir(self):
        env = {**os.environ, 'CADRE_HOME': str(self.home), 'CADRE_YON_DURUM': str(yon.DURUM), 'CADRE_YON_KAYIT': str(yon.KAYIT)}
        subprocess.run([sys.executable, str(LIB / 'yonlendirici.py'), 'ariza', DS_OR, 'plan bos cikti'], env=env, check=True,
                       capture_output=True, timeout=30)
        self.assertEqual(json.loads(yon.DURUM.read_text())[DS_OR]['hata'], 1)


class DevirTests(KesiciTabani):
    def setUp(self):
        super().setUp()
        self.store = RunStore(self.home)
        self.rid = self.store.accept('a', 'is')['run_id']
        for ctx in (patch.object(core, 'QE', self.home), patch.object(core, 'YON', yon),
                    patch.dict(os.environ, {'CADRE_RUN_ID': self.rid, 'CADRE_GOREV_ID': 'g1'})):
            ctx.__enter__()
            self.addCleanup(ctx.__exit__, None, None, None)
        self.log = self.home / 'motor.log'

    def gorev(self):
        return self.store.read(self.rid)['tasks']['g1']

    def arizalar(self):
        return {k: v for k, v in yon.durum_oku().items() if not k.startswith('_')}   # '_sira' is the hysteresis memory

    def test_ayni_modelin_yolu_once_devir_sayilmaz(self):
        yon.ariza('opencode-go', 'requires Global regions', allowed_fails=1, model=DS_GO)
        with self.acik(DS_GO, DS_OR, GLM_GO):
            self.assertEqual(core.yol_devret(DS_GO, self.log, 'hata'), DS_OR)
        son = self.gorev()['yol_devirleri'][-1]
        self.assertEqual((son['tur'], son['sayildi'], self.gorev()['yol_bekleyen']), ('yol', False, ''))
        self.assertEqual(core.devir_sayaci(), (0, 0))

    def test_bir_cagriya_yetmeyen_yola_devredilmez_go_beklenir(self):
        """C05 live (08.10): with $0.14 on OR, when Go closed the session was handed to the OR route and the first call was denied; when Go opened again
        the lane stayed bound to OR and failed. A route without enough breathing room cannot be a hand-off target; the waiting route is Go."""
        with self.acik(DS_GO, DS_OR, GLM_GO), patch.object(core, 'butce_json', return_value={'or': {'db': 'ok'}}), \
             patch.object(yon, 'yazar_nefesi', side_effect=lambda m, b: (0.13, 0.4) if m.startswith('openrouter/') else None):
            self.assertEqual(core.yol_devret(DS_GO, self.log, 'Go pencere reddi', model_devri=False), '')
        self.assertEqual(self.gorev()['yol_bekleyen'], DS_GO)
        with self.acik(DS_GO, DS_OR, GLM_GO), patch.object(core, 'butce_json', return_value={'or': {'db': 'ok'}}), \
             patch.object(yon, 'yazar_nefesi', return_value=None):
            self.assertEqual(core.yol_devret(DS_GO, self.log, 'Go pencere reddi', model_devri=False), DS_OR)

    def test_go_penceresi_yeniden_acilinca_bekleyen_yol_ayni_oturumla_surer(self):
        with self.acik(GLM_GO, QWEN_GO):
            self.assertEqual(core.kota_dusur(DS_GO, self.log, neden='pay'), '')
        self.assertEqual(self.gorev()['yol_bekleyen'], DS_GO)
        with self.acik(DS_GO, GLM_GO, QWEN_GO):
            self.assertEqual(core.yol_devret(DS_GO, self.log, 'bekleyen ayni model yolu', model_devri=False, bekleyen=True), DS_GO)
        self.assertEqual(self.gorev()['yol_bekleyen'], '')

    def test_model_butun_yollarda_duserse_sonraki_model_sayilarak_gelir(self):
        yon.ariza('opencode-go', 'requires Global regions', allowed_fails=1, model=DS_GO)
        yon.ariza('openrouter', 'upstream error', allowed_fails=1, model=DS_OR)
        with self.acik(DS_GO, DS_OR, GLM_GO):
            self.assertEqual(core.yol_devret(DS_GO, self.log, 'hata'), GLM_GO)
        son = self.gorev()['yol_devirleri'][-1]
        self.assertEqual((son['tur'], son['sayildi'], son['sonraki']), ('model', True, GLM_GO))
        self.assertEqual(self.gorev()['yol_bekleyen'], '', 'sonraki modele gecildi: bekleyen yol yok (motor onu ezmez)')
        self.assertEqual(core.devir_sayaci(), (1, 1))
        self.assertIn('MODEL DEVRI', self.log.read_text())

    def test_devir_tavani_dolunca_model_gezintisi_durur(self):
        yon.ariza('opencode-go', 'x', allowed_fails=1, model=DS_GO)
        with patch.object(core, 'DEVIR_SERIT_TAVAN', 0), self.acik(DS_GO, GLM_GO):
            self.assertEqual(core.yol_devret(DS_GO, self.log, 'hata'), '')
        self.assertEqual(self.gorev()['yol_bekleyen'], DS_GO)
        self.assertIn('DEVIR SINIRI', self.log.read_text())

    def test_pay_bitmesi_ariza_yazmaz_ve_ayni_havuzu_atlar(self):
        with self.acik(DS_GO, DS_OR, GLM_GO):
            self.assertEqual(core.kota_dusur(DS_GO, self.log, neden='pay'), DS_OR)
        self.assertEqual(self.arizalar(), {}, 'pay/pencere bitmesi ariza degildir')
        with self.acik(DS_GO, GLM_GO, QWEN_GO, 'openrouter/z-ai/glm-5.3'):   # budget: the model does not change, only the same model's route
            self.assertEqual(core.kota_dusur(DS_GO, self.log, neden='pay'), '')
        self.assertEqual(self.gorev()['yol_bekleyen'], DS_GO)

    def test_or_limiti_model_yolunu_kapatir_go_penceresi_kapatmaz(self):
        with self.acik(DS_GO, DS_OR, GLM_GO):
            self.assertEqual(core.kota_dusur(DS_OR, self.log), DS_GO, 'OR yolu dustu: ayni modelin Go yolu once')
        self.assertTrue(yon.cooldownda('openrouter', model=DS_OR))
        yon.durum_yaz({})
        with self.acik(DS_GO, DS_OR, GLM_GO):
            self.assertEqual(core.kota_dusur(DS_GO, self.log), DS_OR)
        self.assertEqual(self.arizalar(), {}, 'Go limit imzasi pencere duvaridir, ariza degil (GO1)')

    def test_yardimci_cagrisi_model_degistirmez(self):
        yon.ariza('opencode-go', 'x', allowed_fails=1, model=GLM_GO)
        with self.acik(GLM_GO, QWEN_GO):
            self.assertEqual(core.yol_devret(GLM_GO, self.log, 'Go limit', 'kalite', model_devri=False), '')


class HataPatlamasiTests(unittest.TestCase):
    def test_baslik_ajani_ve_butce_reddi_sayilmaz_model_kimligi_cikar(self):
        now = time.time()
        stamp = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(now))
        satir = (f'timestamp={stamp} level=ERROR message="stream error" providerID=opencode-go modelID=deepseek-v4-pro '
                 f'session.id=ses_a agent=build error.error="requires Global regions"\n')
        baslik = (f'timestamp={stamp} level=ERROR message="stream error" providerID=opencode-go modelID=glm-5.3 '
                  f'session.id=ses_a agent=title error.error="x"\n')
        red = (f'timestamp={stamp} level=ERROR message="stream error" providerID=opencode-go modelID=glm-5.3 '
               f'session.id=ses_a agent=build error.error="Error: CADRE_BUDGET_DENIED: monthly request context missing"\n')
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / 'opencode.log'
            log.write_text(baslik * 6 + red * 6)
            with patch.object(core, 'OC_LOG', log):
                self.assertIsNone(core.hata_patlamasi(oturumlar={'ses_a'}, baslangic=now - 30))
                log.write_text(baslik * 6 + red * 6 + satir * 6)
                self.assertEqual(core.hata_patlamasi(oturumlar={'ses_a'}, baslangic=now - 30)[2:], ('opencode-go', 'deepseek-v4-pro'))


import test_uctan_uca as e2e  # noqa: E402

PATLAMA_ISCISI = """
import json, os, pathlib, sys, time
args = sys.argv[1:]
home = pathlib.Path(os.environ['CADRE_HOME'])
model = args[args.index('-m') + 1] if '-m' in args else ''
isaret = home / 'ds-patladi'
if os.environ.get('CADRE_GOREV_ID') == 'g1' and model == 'opencode-go/deepseek-v4-pro' and not isaret.exists():
    isaret.write_text('1')
    sid = 'ses_patlama'
    print(json.dumps({'type': 'step_start', 'sessionID': sid, 'part': {'type': 'step-start'}}), flush=True)
    damga = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime())
    with open(os.environ['CADRE_OC_LOG'], 'a') as f:
        for _ in range(6):
            f.write(f'timestamp={damga} level=ERROR message="stream error" providerID=opencode-go modelID=deepseek-v4-pro '
                    f'session.id={sid} agent=build error.error="requires Global regions"\\n')
    time.sleep(20)   # motor patlamayi tikte gorup keser; kesmezse deneme uzun surer ve test zaman asimina duser
    sys.exit(1)
"""


class MotorModelDevriTests(e2e.E2EFixture):
    """16(20): if Go DS-pro (model, route) fails, the same session goes through the engine loop and is handed to the next model in the chain (glm-5.3);
    `yol_bekleyen` does not override the next model, it counts as a hand-off, the Go provider does not close."""

    def test_patlayan_model_yolu_sonraki_modele_devredilir_ve_is_biter(self):
        sahte = self.fake.read_text()
        cagri_kaydi = "sid = args[args.index('--session')+1]"   # after the call record: the failed attempt must also show in the record
        self.assertIn(cagri_kaydi, sahte)
        self.fake.write_text(sahte.replace(cagri_kaydi, PATLAMA_ISCISI + cagri_kaydi, 1))
        rid = self.submit()
        record = self.wait_terminal(rid, timeout=240)
        log = (self.store.path(rid) / 'coordinator.log').read_text()
        self.assertEqual(record['state'], 'completed', log[-3000:])
        g1 = record['tasks']['g1']
        modeller = [c['args'][c['args'].index('-m') + 1] for c in self.calls() if c['task'] == 'g1' and '-m' in c['args']]
        self.assertEqual(modeller[0], DS_GO, modeller)
        self.assertIn(GLM_GO, modeller, 'ayni oturum sonraki modelle surer')
        devir = [d for d in g1.get('yol_devirleri') or [] if d.get('tur') == 'model']
        self.assertTrue(devir and devir[0]['model'] == DS_GO and devir[0]['sonraki'] == GLM_GO, g1.get('yol_devirleri'))
        self.assertEqual(g1.get('yol_bekleyen'), '')
        durum = json.loads((self.home / 'zincir/saglayici-durum.json').read_text())
        self.assertGreater(float(durum[DS_GO]['cooldown_bitis']), time.time(), 'dusen (model, yol) sogumada')
        self.assertLessEqual(float((durum.get('opencode-go') or {}).get('cooldown_bitis', 0)), time.time(), 'Go saglayicisi kapanmadi')


if __name__ == '__main__':
    unittest.main()


class HesapSinyaliYoluTests(unittest.TestCase):
    """4c QA (Fable, 07.10): an account signal (401/402/insufficient balance) never reached the classifier — the burst sample came from HATA_KALIP,
    the engine only passed the matching word. The sample and signature line now carry the class; the Go pool wall does not move to another Go model."""

    def test_the_burst_sample_carries_the_account_signal(self):
        import tempfile
        now = time.time()
        damga = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(now))
        bas = f'timestamp={damga} level=ERROR message="stream error" providerID=openrouter modelID=z-ai/glm-5.3 session.id=ses_h '
        for hata in ('error.error="402 Insufficient credits. Add more using https://openrouter.ai"',
                     'error.error.code=402 error.error.message="This request requires more credits" '
                     'error.error.metadata.error_type=payment_required'):
            with self.subTest(hata=hata), tempfile.TemporaryDirectory() as d:
                log = Path(d) / 'opencode.log'
                log.write_text((bas + hata + '\n') * 6)
                with patch.object(core, 'OC_LOG', log):
                    hp = core.hata_patlamasi(oturumlar={'ses_h'}, baslangic=now - 30)
                self.assertEqual(yon.hata_sinifi(hp[1]), 'hesap', hp)
                self.assertEqual(hp[2:], ('openrouter', 'z-ai/glm-5.3'))

    def test_bare_numbers_are_not_account_signals(self):
        """4c QA2 (Fable): because "401"/"402" were searched as substrings, "(401 s)" and "140100 tokens" in the engine log
        counted as account signals and closed the provider for 60 min. A code is an account signal only in an HTTP context (status/code/error/HTTP)."""
        for metin, sinif in (('QA ONARIM: bitti (401 sn)', 'hata'), ('context length 140100 exceeded', 'baglam'),
                             ('1429 tokens used', 'hata'), ('Error: 402 Insufficient credits', 'hesap'),
                             ('"code":401', 'hesap'), ('HTTP 401 Unauthorized', 'hesap'), ('rate limit 429', 'kota')):
            with self.subTest(metin=metin):
                self.assertEqual(yon.hata_sinifi(metin), sinif)

    def test_account_signal_line_reaches_the_breaker_and_go_pool_wall_stays_on_the_model(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            logf = Path(d) / 'm.log'
            with patch.object(yon, 'ariza') as ariza, patch.object(core, 'yol_devret', return_value='') as devir:
                core.kota_dusur('openrouter/z-ai/glm-5.3', logf, hata='Error: 402 Insufficient credits for this request')
                self.assertEqual(yon.hata_sinifi(ariza.call_args.args[1]), 'hesap')
                core.kota_dusur('opencode-go/glm-5.3', logf, hata='Error: 429 rate limit reached')
                self.assertFalse(devir.call_args.kwargs.get('model_devri', True), 'Go duvari: havuz ortak, baska Go modeline gecilmez')
                self.assertEqual(ariza.call_count, 1, 'Go pencere duvari ariza yazmaz')

    def test_a_pending_route_resumes_on_itself_once_it_reopens(self):
        import tempfile
        ds, glm = 'opencode-go/deepseek-v4-pro', 'opencode-go/glm-5.3'
        with tempfile.TemporaryDirectory() as d, patch.object(yon, 'zincir', return_value=([ds, glm], [])), \
             patch.dict(os.environ, {'CADRE_RUN_ID': ''}):
            logf = Path(d) / 'm.log'
            self.assertEqual(core.yol_devret(ds, logf, 'bekleyen ayni model yolu', model_devri=False, bekleyen=True), ds)
            self.assertEqual(core.yol_devret(ds, logf, 'Go limit', model_devri=False), '')

    def test_burst_on_the_go_pool_is_a_window_wall_and_bare_numbers_are_not_limit_signatures(self):
        self.assertFalse(core.patlama_arizasi_mi('opencode-go', 'status 429 Too Many Requests'))
        self.assertTrue(core.patlama_arizasi_mi('opencode-go', 'requires Global regions'))
        self.assertTrue(core.patlama_arizasi_mi('openrouter', 'status 429 Too Many Requests'))
        for metin, beklenen in (('{"output":401}', None), ('QA ONARIM: bitti (401 sn)', None), ('{"tokens":413}', None),
                                (' HATA-DONGUSU:status 429 Too Many Requests', 'status 429'), ('"code":429', 'code":429'),
                                ('Error: 402 Insufficient credits', 'Error: 402'), ('HTTP 401 Unauthorized', 'HTTP 401')):
            with self.subTest(metin=metin):
                eslesme = core.LIMIT_IMZASI.search(metin)
                self.assertEqual(eslesme.group(0) if eslesme else None, beklenen)
