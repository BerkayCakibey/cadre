"""4c-4: liveness = token/usage progress; an SSE comment only means 'connection open'. The deadline applies even while there is liveness (B313)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import qe_core as core  # noqa: E402
from canlilik import Canlilik  # noqa: E402


class AcikIstekTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-canli-')
        self.addCleanup(self.temp.cleanup)
        self.dizin = Path(self.temp.name)
        (self.dizin / 'requests').mkdir()
        deneme = type('Deneme', (), {'directory': self.dizin})()
        proc = object.__new__(core.RecordedProcess)
        proc.attempt = deneme
        self.bekci = object.__new__(Canlilik)
        self.bekci.proc = proc

    def isaret(self, **alanlar):
        (self.dizin / 'requests/i.json').write_text(json.dumps({'pid': os.getpid(), **alanlar}))

    def test_token_akan_istek_canlidir(self):
        simdi = time.time()
        self.isaret(started=(simdi - 3600) * 1000, ilerleme_at=(simdi - 30) * 1000, tur='token')
        self.assertTrue(self.bekci._istek(600, simdi))

    def test_yalniz_yorum_akan_istek_esikten_sonra_canli_sayilmaz(self):
        simdi = time.time()
        self.isaret(started=(simdi - 3600) * 1000, last_chunk_at=(simdi - 1) * 1000, ilerleme_at=None, tur='yorum')
        self.assertFalse(self.bekci._istek(600, simdi), 'OR isleniyor yorumu ilerleme degildir')
        self.isaret(started=(simdi - 60) * 1000, last_chunk_at=(simdi - 1) * 1000, ilerleme_at=None, tur='yorum')
        self.assertTrue(self.bekci._istek(600, simdi), 'yeni baslayan istek esik dolana kadar canlidir')

    def test_olu_surecin_isareti_sayilmaz(self):
        (self.dizin / 'requests/i.json').write_text(json.dumps({'pid': 2**22 + 7, 'started': time.time() * 1000}))
        self.assertFalse(self.bekci._istek(600, time.time()))


class HareketliAdimTests(unittest.TestCase):
    def bekci(self, son_once, hareketli, acik_adim=True):
        bekci = object.__new__(Canlilik)
        bekci.proc, bekci.wt, bekci.kayit, bekci.logf = None, None, None, self.dizin / 'out.log'
        bekci.min_sn, bekci.en_uzun, bekci.hareket_siniri, bekci.acik_adim = 600.0, 0.0, 2700.0, acik_adim
        bekci.durum, bekci.neden, bekci.ofset, bekci.artik, bekci.adimlar = 'unknown', '', 0, b'', set()
        bekci.son = time.monotonic() - son_once
        bekci.hareket_son = None
        sayac = [0]

        def boy():
            sayac[0] += 1
            return sayac[0] if hareketli else 5
        bekci._boy, bekci._agac, bekci._cpu = boy, lambda: (), lambda: {}
        bekci._istek, bekci._adim = lambda esik, simdi: False, lambda: False
        bekci.boy, bekci.agac, bekci.cpu = bekci._boy(), (), {}
        return bekci

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-hareket-')
        self.addCleanup(self.temp.cleanup)
        self.dizin = Path(self.temp.name)

    def test_hareketli_belirsiz_adim_ayri_sonlu_sinirla_surer(self):
        bekci = self.bekci(700, hareketli=True)
        self.assertFalse(bekci.takildi(), 'CPU/dosya hareketi varken sessizlik esigi kesmez')
        self.assertEqual((bekci.durum, bekci.neden), ('unknown', 'bounded-active-step'))
        bekci = self.bekci(2800, hareketli=True)
        self.assertTrue(bekci.takildi(), 'hareketli adim da sonludur')
        self.assertEqual(bekci.neden, 'active-time-limit')

    def test_hareketsiz_sessizlik_esigi_degismez(self):
        bekci = self.bekci(700, hareketli=False)
        self.assertTrue(bekci.takildi())
        self.assertEqual((bekci.durum, bekci.neden), ('stalled', 'stall-time-limit'))

    def test_acik_arac_adimi_yokken_hareket_esigi_uzatmaz(self):
        bekci = self.bekci(700, hareketli=True, acik_adim=False)
        self.assertTrue(bekci.takildi(), 'yalniz log/CPU hareketi (acik arac adimi yok) CR-14 esigini uzatmaz')
        self.assertEqual((bekci.durum, bekci.neden), ('unknown', 'uncertain-time-limit'))


class SonSureTests(unittest.TestCase):
    def test_son_sure_canlilik_varken_de_uygulanir(self):
        with tempfile.TemporaryDirectory(prefix='qe-sure-') as d:
            log = Path(d) / 'out.log'
            log.write_text('')
            with log.open('a') as cikti:
                proc = subprocess.Popen([sys.executable, '-c', 'import time\nwhile True:\n print("x", flush=True); time.sleep(0.2)'],
                                        stdout=cikti, start_new_session=True)
            bas = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                core.wait_process(proc, 2, logf=log)
            self.assertLess(time.monotonic() - bas, 15)
            self.assertIsNotNone(proc.poll(), 'son surede surec kesilir')

    def test_rol_son_sureleri_tek_kaynaktan(self):
        import sinirlar
        self.assertEqual({r: sinirlar.son_sure(r) for r in ('plan', 'kalite', 'qa', 'bolme')},
                         {'plan': 1800, 'kalite': 2400, 'qa': 2700, 'bolme': 900})



import test_kosu_sessions as fixtures  # noqa: E402


class KesilmeTests(unittest.TestCase):
    """16(2): opencode `finish=length` -> the same session continues once with a 2× output cap; a second truncation is partial; every truncation is recorded."""

    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.fake.write_text(self.f.fake.read_text() + '''
kat = os.environ.get('CADRE_CIKTI_KAT', '1')
(p/'katlar.txt').open('a').write(kat + '\\n')
neden = 'length' if (kat == '1' or os.environ.get('HEP_KESIK') == '1') else 'stop'
print(json.dumps({'type': 'step_finish', 'sessionID': sid, 'part': {'type': 'step-finish', 'reason': neden}}), flush=True)
''')

    def test_kesilen_yanit_ayni_oturumda_bir_kez_iki_kat_tavanla_surer(self):
        rc, _ = self.f.motor_fixture(accept=True)
        self.assertEqual((self.f.home / 'katlar.txt').read_text().split(), ['1', '2'], '2× yalniz bir sonraki denemede')
        self.assertIn('--session', self.f.calls()[1], 'ayni oturum')
        uyarilar = self.f.store.read(self.f.rid).get('cikti_uyarilari') or []
        self.assertEqual([u['tur'] for u in uyarilar], ['kesilme'])
        self.assertEqual(self.f.store.read(self.f.rid)['tasks']['g1'].get('failure_count', 0), 0, 'kesilme deneme sayilmaz')

    def test_iki_kat_tavanla_da_kesilirse_is_yarim_kalir(self):
        with unittest.mock.patch.dict(os.environ, {'HEP_KESIK': '1'}):
            rc, _ = self.f.motor_fixture(accept=True)
        self.assertNotEqual(rc, 0)
        self.assertEqual(len(self.f.calls()), 2)
        self.assertEqual([u['tur'] for u in self.f.store.read(self.f.rid).get('cikti_uyarilari') or []], ['kesilme', 'kesilme'])


class DusunmeAlarmiTests(unittest.TestCase):
    """16(3): an alarm if a thinking model has no thinking TEXT; usage.reasoning 0 alone is not an alarm."""

    def ozet(self, parcalar, model='opencode-go/deepseek-v4-pro'):
        import sqlite3
        from contextlib import closing
        import kabul
        with tempfile.TemporaryDirectory(prefix='qe-dusunme-') as d:
            ev = Path(d)
            stdout = ev / 'stdout.jsonl'
            stdout.write_text(json.dumps({'type': 'step_finish', 'part': {'type': 'step-finish', 'reason': 'stop',
                                                                          'tokens': {'reasoning': 0}}}) + '\n')
            db = ev / 'oc.db'
            with closing(sqlite3.connect(db)) as con, con:
                con.execute('create table message(id text, session_id text, data text)')
                con.execute('create table part(id text, message_id text, time_created integer, data text)')
                con.execute("insert into message values('m1','ses_d','{}')")
                for i, (tur, metin) in enumerate(parcalar):
                    con.execute('insert into part values(?,?,?,?)', (f'p{i}', 'm1', int(time.time() * 1000),
                                                                     json.dumps({'type': tur, 'text': metin})))
            deneme = type('D', (), {'data': {'stdout': str(stdout), 'session_id': 'ses_d', 'created': time.time() - 60}})()
            proc = type('P', (), {'attempt': deneme})()
            log = ev / 'm.log'
            log.write_text('')
            katalog = ev / 'models.json'
            katalog.write_text(json.dumps({'opencode-go': {'models': {'deepseek-v4-pro': {'reasoning': True}}}}))
            with unittest.mock.patch.dict(os.environ, {'OPENCODE_DB': str(db), 'CADRE_MODEL_KATALOG': str(katalog), 'CADRE_RUN_ID': ''}):
                return kabul.cikti_denetimi(proc, model, log), log.read_text()

    def test_metin_yoksa_alarm_usage_sifir_ama_metin_varsa_yok(self):
        ozet, log = self.ozet([('text', 'cevap')])
        self.assertEqual(ozet['dusunme'], 0)
        self.assertIn('dusunme metni yok', log)
        ozet, log = self.ozet([('reasoning', 'adim adim dusundum'), ('text', 'cevap')])
        self.assertGreater(ozet['dusunme'], 0)
        self.assertNotIn('dusunme metni yok', log, 'usage.reasoning 0 tek basina alarm degil')


class SeritBaglamiTests(unittest.TestCase):
    """4c-4: the lane's effective context is min(256k, model context − 20k); plan/judge use the model's full context."""

    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def limit(self, rol, model):
        env = {'CADRE_ROL': rol}
        core.opencode_budget_env(env, ['-m', model, 'is'])
        saglayici, _, ad = model.partition('/')
        return (json.loads(env['OPENCODE_CONFIG_CONTENT']).get('provider') or {}).get(saglayici, {}).get('models', {}).get(ad, {}).get('limit')

    def test_serit_baglami_ust_sinir_ve_pay(self):
        self.assertEqual(self.limit('impl', 'opencode-go/glm-5.3')['context'], 262144, '1M baglam -> 256k')
        self.assertEqual(self.limit('impl', 'opencode-go/deepseek-v4-pro')['context'], 262144 - 20000, 'kucuk baglamda pay dusulur')
        self.assertIsNone(self.limit('plan', 'opencode-go/glm-5.3'), 'plan modelin tam baglamini kullanir')

    def test_or_serit_modeli_opencode_ayarinda_cikti_sinirini_tasir(self):
        fiyat = self.f.home / 'zincir/openrouter-fiyat.json'
        fiyat.write_text(json.dumps({'modeller': {'deepseek/deepseek-v4-pro': {'kademeler': {
            'streamlake/fp8': {'max_cikti': 384000}, 'deepinfra/fp8': {'max_cikti': 16384}}}}}))
        sonuc = []
        for dosya_var in (True, False):
            if not dosya_var:
                fiyat.unlink()
            with unittest.mock.patch('katalog.model', return_value={'limit': {'context': 1048576}}), \
                 unittest.mock.patch.object(core, 'available_budget', return_value=10 ** 6):
                sonuc.append(self.limit('impl', 'openrouter/deepseek/deepseek-v4-pro'))
        self.assertEqual([x['output'] for x in sonuc], [384000, 2 * core.SINIR.cikti('serit')],
                         'OR katalog kaydinda cikti siniri yok; opencode ayari onsuz gecersiz sayar ve serit hic baslamaz')
        self.assertEqual(sonuc[0]['context'], 262144)


import unittest.mock  # noqa: E402

if __name__ == '__main__':
    unittest.main()
