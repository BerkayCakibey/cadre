"""23.09 afternoon audit — defects of three fixes written the same day:
B183 if the judge output had two contradictory valid JSONs, the first one found won (draft GECER + final DUZELT → wrong acceptance);
B184 the fast-failure guard also counted the engine's own interruptions and budget-denial exits as failures, the error count did not look at
the session id; B185 the loop guard counted a single pytest output twice, also counted the model's own text, and could produce a wrong id
on a half line. No model/network; the engine tests are the real `_motor` + a fake executor."""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import qe_core as core  # noqa: E402
import sonuc  # noqa: E402
import test_kosu_sessions as fixtures  # noqa: E402
from test_gece_senaryolar import butce, zincir_sahte  # noqa: E402

D = 'a' * 64
GECER = '{"revision": "%s", "verdict": "GECER", "acceptance": [{"id": "main", "status": "PASS", "evidence": "tests/x.py:1 ilk bakis yesil"}]}' % D
DUZELT = '{"revision": "%s", "verdict": "DUZELT", "acceptance": [{"id": "main", "status": "FAIL", "evidence": "tests/x.py:9 kenar durum kirik"}]}' % D


class HakemCeliskiTest(unittest.TestCase):  # B183
    def test_citli_iki_celiskili_karar_kabul_degil_hatadir(self):
        metin = "Taslak:\n```json\n" + GECER + "\n```\nNihai:\n```json\n" + DUZELT + "\n```\n"
        r = sonuc.parse_review(metin, {'digest': D}, ['main'])
        self.assertEqual(r['status'], 'ERROR', r)
        self.assertIn('conflicting', r['reason'])

    def test_citsiz_taslak_ve_citli_nihai_celiskisi_de_hatadir(self):
        metin = "Once: " + GECER + "\nSonra:\n```json\n" + DUZELT + "\n```\n"
        self.assertEqual(sonuc.parse_review(metin, {'digest': D}, ['main'])['status'], 'ERROR')

    def test_ayni_karar_iki_kez_yazilirsa_gecerli_kalir(self):
        metin = "Karar:\n```json\n" + GECER + "\n```\nOzet: " + GECER + "\n"
        r = sonuc.parse_review(metin, {'digest': D}, ['main'])
        self.assertEqual((r['status'], r['verdict']), ('PASS', 'GECER'))


class HizliDususSayimiTest(unittest.TestCase):  # B184 (class)
    def test_kontrollu_cikis_ne_sayilir_ne_seriyi_bozar(self):
        hd = core.HizliDusus(esik_sn=60, esik=3)
        hd.kaydet(rc=1, sure_sn=10, baslangic=1.0); hd.kaydet(rc=1, sure_sn=10, baslangic=20.0)
        self.assertEqual(hd.kaydet(rc=-15, sure_sn=5, baslangic=30.0, kontrollu=True), 0)
        self.assertEqual(hd.seri, 2, 'kontrollu kesme seriyi sifirlamaz')
        self.assertEqual(hd.kaydet(rc=1, sure_sn=10, baslangic=40.0), 3)
        self.assertTrue(hd.dongu)
        self.assertEqual(hd.seri_baslangic, 1.0)


class HizliDususMotorTest(unittest.TestCase):  # B184 (real engine wiring)
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.enter(patch.object(core, 'project_memory', return_value=[]))
        self.f.enter(patch.object(core.YON, 'zincir', side_effect=zincir_sahte))
        self.f.enter(patch.object(core, 'butce_json', return_value=butce()))
        self.f.enter(patch.object(core, 'arac_calisiyor', return_value=False))

    def motor_log(self):
        return (self.f.store.task_dir(self.f.rid, 'g1') / 'motor.log').read_text()

    def deneme_sayisi(self):
        return len(list((self.f.store.task_dir(self.f.rid, 'g1') / 'attempts').glob('*/attempt.json')))

    def test_butce_reddi_cikislari_hizli_dusus_sayilmaz(self):
        self.f.fake.write_text(f"#!{sys.executable}\n" + """
import json, os, pathlib, sys
p = pathlib.Path(os.environ['CADRE_HOME']); n = len(list(p.glob('deneme-*'))) + 1; (p / f'deneme-{n}').write_text('x')
args = sys.argv[1:]; sid = args[args.index('--session') + 1] if '--session' in args else 'ses_b'
print(json.dumps({'type': 'step_start', 'sessionID': sid, 'part': {'type': 'step-start'}}), flush=True)
if n <= 3:   # uc deneme art arda butce reddiyle hizli biter
    a = os.environ['CADRE_BUDGET_ATTEMPT']; d = pathlib.Path(os.environ['CADRE_REQUEST_DIR']).parent
    (d / 'budget-denied.json').write_text(json.dumps({'attempt_id': a, 'providers': ['opencode-go'],
        'refusals': [{'provider': 'opencode-go', 'scope': 'provider', 'reason': 'allocation exhausted'}]}))
    sys.exit(1)
print(json.dumps({'type': 'text', 'sessionID': sid, 'part': {'type': 'text', 'text': 'bitti'}}), flush=True)
""")
        rc, _gate = self.f.motor_fixture(accept=True, env={'CADRE_MAX_RESUME': '6'})
        # P2/P3 (03.10): a budget denial becomes 'retryable' — the engine STOPS with a budget wait (no retry is burned),
        # the 4th attempt is never reached in the same run. FAST-FAILURE must not trigger at all.
        self.assertNotEqual(rc, 0)
        log = self.motor_log()
        self.assertIn('BUTCE: deneme kaydi', log)
        self.assertNotIn('HIZLI-DUSUS', log, 'butce reddi motorun kontrollu yoludur, dusus serisi degildir')

    def test_ardisik_hizli_cokusler_seri_olarak_kesilir(self):
        self.f.fake.write_text(f"#!{sys.executable}\n" + """
import json, sys
args = sys.argv[1:]; sid = args[args.index('--session') + 1] if '--session' in args else 'ses_c'
print(json.dumps({'type': 'step_start', 'sessionID': sid, 'part': {'type': 'step-start'}}), flush=True)
sys.exit(1)
""")
        rc, _gate = self.f.motor_fixture(accept=True, env={'CADRE_MAX_RESUME': '12'})
        self.assertNotEqual(rc, 0)
        self.assertIn('HIZLI-DUSUS', self.motor_log())
        self.assertLess(self.deneme_sayisi(), 13, 'seri deneme sinirini tuketmeden takilma yoluna girer')


class TestDongusuTekilTest(unittest.TestCase):  # B185
    def setUp(self):
        self.logf = Path(tempfile.mkdtemp(prefix='dongu-tekil-')) / 'motor.log'
        self.logf.write_text('')

    def yaz(self, s):
        with self.logf.open('a') as f:
            f.write(s)

    def test_ayni_olaydaki_iki_eslesme_bir_calistirma_sayilir(self):
        d = core.TestDongusu(str(self.logf), esik=3)
        olay = '{"type":"tool_use","part":{"state":{"output":"FAILED tests/a.py::t - x","metadata":{"output":"FAILED tests/a.py::t - x"}}}}\n'
        self.yaz(olay * 2)
        self.assertEqual(d.tik(), '', 'iki pytest calistirmasi, dort eslesme: esik 3 dolmadi')
        self.yaz(olay)
        self.assertEqual(d.tik(), 'tests/a.py::t')

    def test_modelin_kendi_yazisi_sayilmaz(self):
        d = core.TestDongusu(str(self.logf), esik=3)
        self.yaz('{"type":"text","part":{"text":"FAILED tests/a.py::t hala kirik"}}\n' * 5)
        self.assertEqual(d.tik(), '')

    def test_yarim_satir_tam_kimlikle_bir_kez_sayilir(self):
        d = core.TestDongusu(str(self.logf), esik=2)
        self.yaz('FAILED tests/unit/test_cli.py::test_cli_ver')
        self.assertEqual(d.tik(), '')
        self.yaz('sion_uses - AssertionError\n')
        self.assertEqual(d.tik(), '')
        self.yaz('FAILED tests/unit/test_cli.py::test_cli_version_uses - AssertionError\n')
        self.assertEqual(d.tik(), 'tests/unit/test_cli.py::test_cli_version_uses')

    def test_varsayilan_esik_bes_calistirma(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CADRE_TEST_DONGU_ESIK', None)
            self.assertEqual(core.StallGuard(str(self.logf), None, 9999).dongu.esik, 5)


class AracTekrariTest(unittest.TestCase):  # B200
    """B200 (23.09 R2-medium): the local model called the same bash command with the same output 6+ times ~23 s apart; G-F9 only counted the
    breaking test, the artefact guard waited 45 min. Identical consecutive tool calls at the threshold (default 4) are a loop."""
    def setUp(self):
        self.logf = Path(tempfile.mkdtemp(prefix='arac-tekrar-')) / 'motor.log'
        self.logf.write_text('')

    def cagri(self, komut='python3 -c "print(1)"', cikti='1', arac='bash'):
        return json.dumps({'type': 'tool_use', 'part': {'tool': arac, 'state': {'status': 'completed',
                           'input': {'command': komut}, 'output': cikti}}}, separators=(',', ':')) + '\n'   # the opencode log writes compactly

    def yaz(self, s):
        with self.logf.open('a') as f:
            f.write(s)

    def test_dort_ozdes_ardisik_cagri_donguduer(self):
        d = core.AracTekrari(str(self.logf), esik=4)
        self.yaz(self.cagri() * 3)
        self.assertEqual(d.tik(), '')
        self.yaz(self.cagri())
        self.assertEqual(d.tik(), 'bash')

    def test_farkli_cikti_ya_da_araya_giren_cagri_seriyi_bozar(self):
        d = core.AracTekrari(str(self.logf), esik=3)
        self.yaz(self.cagri(cikti='1') + self.cagri(cikti='2') + self.cagri(cikti='2'))
        self.assertEqual(d.tik(), '')
        self.yaz(self.cagri(komut='ls') + self.cagri(cikti='2') + self.cagri(cikti='2'))
        self.assertEqual(d.tik(), '')

    def test_model_yazisi_ve_yarim_satir_sayilmaz(self):
        d = core.AracTekrari(str(self.logf), esik=2)
        self.yaz(self.cagri() + '{"type":"text","part":{"text":"x"}}\n')
        tam = self.cagri()
        self.yaz(tam[:20])
        self.assertEqual(d.tik(), '')
        self.yaz(tam[20:])
        self.assertEqual(d.tik(), 'bash', 'metin satiri seriyi bozmaz; yarim satir tamamlaninca sayilir')

    def test_bekci_varsayilan_esik_bes_ve_dongu_kararina_baglanir(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CADRE_ARAC_TEKRAR_ESIK', None)
            self.assertEqual(core.StallGuard(str(self.logf), None, 9999).tekrar.esik, 5)


class ButceReddiDarTest(unittest.TestCase):  # B187
    def test_uzun_plan_metni_ifadeyi_icerse_de_red_degildir(self):
        plan = "## MADDELER\n" + ("- CADRE_BUDGET_DENIED loglamasini duzelt; kalan madde aciklamasi.\n" * 20)
        self.assertFalse(core.butce_reddi_metni(plan))

    def test_tasiyici_json_uzun_olsa_da_result_alani_kisa_red(self):
        tasiyici = json.dumps({'type': 'result', 'is_error': True, 'result': 'Failed to authenticate. API Error: 403 CADRE_BUDGET_DENIED',
                              'usage': {'input_tokens': 0, 'output_tokens': 0, 'dolgu': 'x' * 900}})
        self.assertTrue(core.butce_reddi_metni(tasiyici))
        self.assertTrue(core.butce_reddi_metni("Failed to authenticate. API Error: 403 CADRE_BUDGET_DENIED"))


class KontrolluKesmeTavaniTest(unittest.TestCase):  # B188
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.enter(patch.object(core, 'project_memory', return_value=[]))
        self.f.enter(patch.object(core.YON, 'zincir', side_effect=zincir_sahte))
        self.f.enter(patch.object(core, 'butce_json', return_value=butce()))
        self.f.enter(patch.object(core, 'arac_calisiyor', return_value=False))

    def test_hic_bitmeyen_butce_reddi_serisi_deneme_sinirina_takilir(self):
        import threading
        self.f.fake.write_text(f"#!{sys.executable}\n" + """
import json, os, pathlib, sys
p = pathlib.Path(os.environ['CADRE_HOME'])
args = sys.argv[1:]; sid = args[args.index('--session') + 1] if '--session' in args else 'ses_t'
print(json.dumps({'type': 'step_start', 'sessionID': sid, 'part': {'type': 'step-start'}}), flush=True)
a = os.environ['CADRE_BUDGET_ATTEMPT']; d = pathlib.Path(os.environ['CADRE_REQUEST_DIR']).parent
(d / 'budget-denied.json').write_text(json.dumps({'attempt_id': a, 'providers': ['opencode-go'],
    'refusals': [{'provider': 'opencode-go', 'scope': 'provider', 'reason': 'allocation exhausted'}]}))
sys.exit(1)
""")
        sonuc_kutusu = {}
        is_parcacigi = threading.Thread(target=lambda: sonuc_kutusu.update(rc=self.f.motor_fixture(
            accept=True, env={'CADRE_MAX_RESUME': '2', 'CADRE_KONTROLLU_TAVAN': '3'})[0]), daemon=True)
        is_parcacigi.start(); is_parcacigi.join(timeout=60)
        self.assertFalse(is_parcacigi.is_alive(), 'her deneme butce reddiyle biterse motor sonsuza dek doner (B188)')
        self.assertNotEqual(sonuc_kutusu.get('rc'), 0)
        # P2/P3 (03.10): budget denial -> budget wait -> retryable (the endless loop is broken this way).
        self.assertIn('BUTCE BEKLEME', (self.f.store.task_dir(self.f.rid, 'g1') / 'motor.log').read_text())


class DogrulamaSayisiTest(unittest.TestCase):  # B158(d)
    def test_python_yorum_ve_dizgideki_assert_sayilmaz(self):
        metin = ("def test_x():\n    assert f() == 1\n    assert g() == 2\n"
                 "    # assert h() == 3\n    # assert h() == 4\n    # assert h() == 5\n"
                 "    s = 'assert inside string'\n    \"\"\"assert docstring icinde\"\"\"\n")
        self.assertEqual(core.dogrulama_sayisi(metin, 'tests/test_x.py'), 2)

    def test_js_satir_ve_blok_yorumu_sayilmaz(self):
        metin = "test('x', () => {\n  expect(a).toBe(1);\n  // expect(b).toBe(2);\n  /* expect(c).toBe(3); */\n});\n"
        self.assertEqual(core.dogrulama_sayisi(metin, 'web/x.test.js'), 1)


class ExcludeKimlikTest(unittest.TestCase):  # B158(e)
    def test_info_exclude_degisirse_urun_kimligi_degisir(self):
        import subprocess as sp
        wt = Path(tempfile.mkdtemp(prefix='exclude-'))
        g = lambda *a: sp.run(['git', '-C', str(wt), '-c', 'user.email=t@t', '-c', 'user.name=t', *a], check=True, capture_output=True)
        g('init', '-q'); (wt / 'a.py').write_text('x = 1\n'); g('add', 'a.py'); g('commit', '-qm', 'taban')
        once = sonuc.revision(wt)['digest']
        exclude = wt / '.git' / 'info' / 'exclude'; exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open('a') as f:
            f.write('yeni.py\n')
        (wt / 'yeni.py').write_text('y = 2\n')   # a file written and hidden in the same step
        self.assertNotEqual(sonuc.revision(wt)['digest'], once, 'gizleme ayni kimlik uretmemeli: QA dosyayi gorur, teslim gormez')


class YardimciFixture:
    """Shared setup; the test method is inherited, not discovered a second time."""
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.enter(patch.object(core, 'project_memory', return_value=[]))
        self.f.enter(patch.object(core.YON, 'zincir', side_effect=zincir_sahte))
        self.f.enter(patch.object(core, 'butce_json', return_value=butce()))

    def yardimcilar(self):
        env = dict(os.environ)
        core.opencode_budget_env(env, ['gorev'])
        return {ad: str(satir.get('model', '')) for ad, satir in json.loads(env['OPENCODE_CONFIG_CONTENT'])['agent'].items()}


class GorevYasagiYardimciTest(YardimciFixture, unittest.TestCase):  # B148
    """N-RT04: a task ban also closes the provider of the helper candidates."""

    def test_gorev_yasagi_yardimci_secimine_ulasir(self):
        import butce_kapisi
        once = self.yardimcilar()
        self.assertTrue(any(m.startswith('opencode-go/') for m in once.values()), f'on kosul: yasaksizken Go secilir {once}')
        butce_kapisi.task_bans(self.f.home).block('opencode-go', 'budget', butce())
        sonra = self.yardimcilar()
        self.assertFalse(any(m.startswith('opencode-go/') for m in sonra.values()), sonra)



class AltAjansizTest(YardimciFixture, unittest.TestCase):  # B196 + 4c-9/10
    """B196 (23.09 R1-medium-v2): the plan did not ask for a sub-agent for the lane, the prompt said "do NOT hand off with task()"; the helpers were still open and
    the executor called cadre-review 7 times and cadre-plan once. 4c-9: no sub-agent at all in the dispatcher lane and the judge
    (`CADRE_ALT_AJANSIZ=1`); 4c-10: compaction is always the lane's own model."""
    def ajanlar(self, alt_ajansiz):
        env = dict(os.environ)
        env.pop('CADRE_ALT_AJANSIZ', None)
        if alt_ajansiz:
            env['CADRE_ALT_AJANSIZ'] = '1'
        args = core.opencode_budget_env(env, ['gorev'])
        return json.loads(env['OPENCODE_CONFIG_CONTENT'])['agent'], args[args.index('-m') + 1]

    def test_seritte_alt_ajan_yok_sikistirma_seridin_modeli(self):
        a, model = self.ajanlar(True)
        for ad in ('cadre-plan', 'cadre-impl', 'cadre-review'):
            self.assertTrue(a[ad].get('disable'), (ad, a[ad]))
        self.assertNotIn('cadre-scout', a)
        self.assertEqual((a['compaction'].get('model'), a['compaction'].get('disable')), (model, False))

    def test_degisken_yoksa_tek_motor_davranisi_degismez(self):
        a, model = self.ajanlar(False)
        self.assertFalse(a['cadre-plan'].get('disable'), a['cadre-plan'])
        self.assertTrue(a['cadre-review'].get('disable'), a['cadre-review'])   # 4b (07.10): the intermediate review was removed
        self.assertEqual(a['compaction'].get('model'), model)

class RezervasyonKalibreTest(unittest.TestCase):  # B198
    """B198 (user decision 23.09): the allocation was 7-9 times the real cost (deepseek allocated ~$0.07 per request and spent ~$0.009);
    a $0.10 daily share covered ~4 requests a day. 2x the p95 of the provider's real/worst-case ratio over the last 7 days is allocated
    (at least 30 receipts). The ratio is computed against the WORST case; against the calibrated allocation, feedback would push the coefficient to 1."""
    def setUp(self):
        import butce_kapisi
        self.t = Path(tempfile.mkdtemp(prefix='kalibre-'))
        self.ledger = butce_kapisi.BudgetLedger(self.t / 'budget.db')
        self.BudgetDenied = butce_kapisi.BudgetDenied

    def snapshot(self, amount=5):
        import time
        return {'bugun': time.strftime('%Y-%m-%d'), 'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
                'go': {'db': 'ok', 'pay': (amount) * 100 / 15, 'kullanilabilir': (amount) * 100 / 15, 'pencere_acik': True, 'birim': 'puan'}}

    def gecmis(self, oranlar, en_kotu=1_000_000, reserved=None, meta=None, saglayici='opencode-go'):
        import sqlite3, time, uuid
        self.ledger.reserve('opencode-go', 1, self.snapshot(), {})   # creates the tables
        once = time.time() - 2 * 86400
        from contextlib import closing
        with closing(sqlite3.connect(self.t / 'budget.db')) as con, con:
            for i, oran in enumerate(oranlar):
                con.execute('insert into reservation values(?,?,?,?,?,?,?,?)', (uuid.uuid4().hex, saglayici, time.strftime('%Y-%m-%d', time.localtime(once)),
                            once + i, reserved or en_kotu, int(en_kotu * oran), uuid.uuid4().hex, json.dumps(meta or {})))

    def ayrilan(self, rid):
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.t / 'budget.db')) as con:
            r, m = con.execute('select reserved, metadata from reservation where id=?', (rid,)).fetchone()
        return r, json.loads(m)

    def test_az_makbuzda_en_kotu_durum_ayrilir(self):
        self.gecmis([0.05] * 10)
        self.assertEqual(self.ayrilan(self.ledger.reserve('opencode-go', 1_000_000, self.snapshot(), {}))[0], 1_000_000)

    def test_yeterli_makbuzda_p95_in_iki_kati_ayrilir_en_kotu_kayitta(self):
        self.gecmis([0.2] * 3 + [0.05] * 37)
        r, m = self.ayrilan(self.ledger.reserve('opencode-go', 1_000_000, self.snapshot(), {}))
        self.assertEqual((r, m.get('en_kotu')), (400_000, 1_000_000))

    def test_claude_on_makbuzla_kalibre_olur_digerleri_otuz_ister(self):
        """Claude threshold (25.09, user approval): Claude could not reach 30 receipts in 7 days (23); because it allocated the worst case ($3.6-4.6)
        most requests did not fit the run pocket and were denied, and since they did not fit no receipts accumulated. Claude calibrates with 10 receipts."""
        import sqlite3
        from contextlib import closing
        self.gecmis([0.1] * 9, saglayici='claude')
        self.gecmis([0.1] * 12)
        with closing(sqlite3.connect(self.t / 'budget.db')) as con:
            self.assertEqual(self.ledger.kalibre(con, 'claude', 1_000_000), 1_000_000, '9 makbuz: en kotu durum')
            self.assertEqual(self.ledger.kalibre(con, 'opencode-go', 1_000_000), 1_000_000, 'digerleri icin esik 30 kalir')
        self.gecmis([0.1] * 3, saglayici='claude')
        with closing(sqlite3.connect(self.t / 'budget.db')) as con:
            self.assertEqual(self.ledger.kalibre(con, 'claude', 1_000_000), 200_000, '12 makbuz: p95 x 2')

    def test_kalibre_ayirmalar_katsayiyi_geri_beslemeyle_bire_itmez(self):
        self.gecmis([0.2] * 3 + [0.05] * 37)
        self.gecmis([0.2] * 3 + [0.05] * 37, reserved=400_000, meta={'en_kotu': 1_000_000})   # the same distribution; against the allocated amount p95 would be 0.5
        self.assertEqual(self.ayrilan(self.ledger.reserve('opencode-go', 1_000_000, self.snapshot(), {}))[0], 400_000)

    def test_kucuk_gunluk_pay_kalibre_ayirmayla_isi_kabul_eder(self):
        self.gecmis([0.2] * 3 + [0.05] * 37, en_kotu=70_000)
        once = self.ledger.reserve('opencode-go', 36_000, self.snapshot(0.1), {})
        self.ledger.settle(once, 36_000, 'bugun-1')
        self.ledger.reserve('opencode-go', 70_000, self.snapshot(0.1), {})   # the worst case of 70k would not fit: 36k + 70k > 100k

    def test_en_kotu_sinirin_altindaki_asim_fiyat_hatasi_yazmaz_ustundeki_yazar(self):
        import sqlite3
        self.gecmis([0.2] * 3 + [0.05] * 37)
        a = self.ledger.reserve('opencode-go', 1_000_000, self.snapshot(), {})      # 400k allocated, limit 1M
        self.ledger.settle(a, 700_000, 'asim-sinir-alti')
        self.assertEqual(self.ayrilan(a)[0], 700_000, 'asim gizlenmez: gercek tutar kayitta')
        self.ledger.reserve('opencode-go', 1_000_000, self.snapshot(), {})          # the provider was not locked
        b = self.ledger.reserve('opencode-go', 1_000_000, self.snapshot(), {})
        self.ledger.settle(b, 1_200_000, 'sinir-ustu')
        from contextlib import closing
        with closing(sqlite3.connect(self.t / 'budget.db')) as con:
            self.assertTrue(con.execute("select 1 from pricing_fault where provider='opencode-go'").fetchone(), 'en kotu siniri asmak fiyat hatasidir')


class ZorTavanTest(unittest.TestCase):  # B199
    """B199 (user decision 23.09): easy/medium work at the project cap ($2), hard work $5 — the Claude plan never fit in a $2 cap
    (reserve ~$3 > lane share $1.50). The cap is written at acceptance, the difficulty is known from the dispatcher's pre-filter; the environment variable wins."""
    def setUp(self):
        import kosu
        self.kosu = kosu
        self.home = Path(tempfile.mkdtemp(prefix='zortavan-'))
        (self.home / 'projects' / 'p').mkdir(parents=True)
        (self.home / 'projects' / 'p' / 'kosu-tavan-usd').write_text('2.0\n')
        self.store = kosu.RunStore(self.home)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CADRE_KOSU_TAVAN_USD', None)
            self.rid = self.store.accept('p', 'gorev')['run_id']

    def test_zor_iste_tavan_yukselir_varsayilan_bes_dolar(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CADRE_KOSU_TAVAN_USD', None)
            self.assertEqual(self.kosu.zor_tavan_uygula(self.store, self.rid, 'p'), (2.0, 2.0))
        self.assertEqual(self.store.read(self.rid)['budget_cap_usd'], 2.0)

    def test_proje_zor_tavani_ve_zaten_yuksek_tavan(self):
        (self.home / 'projects' / 'p' / 'kosu-tavan-zor-usd').write_text('4\n')
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CADRE_KOSU_TAVAN_USD', None)
            self.assertEqual(self.kosu.zor_tavan_uygula(self.store, self.rid, 'p'), (2.0, 2.0))
            self.assertEqual(self.kosu.zor_tavan_uygula(self.store, self.rid, 'p'), (2.0, 2.0))

    def test_dosyasiz_projede_kolay_orta_varsayilan_iki_dolar(self):
        (self.home / 'projects' / 'q').mkdir()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CADRE_KOSU_TAVAN_USD', None)
            self.assertEqual(self.kosu.kosu_tavani_usd(self.home, 'q'), 2.0)

    def test_komut_satiri_eski_ve_yeni_tavani_basar(self):
        import subprocess as sp
        env = {k: v for k, v in os.environ.items() if k != 'CADRE_KOSU_TAVAN_USD'}
        env['CADRE_HOME'] = str(self.home)
        r = sp.run([sys.executable, str(LIB / 'kosu.py'), 'zor-tavan', self.rid, 'p'], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, '2.00 2.00'), r.stderr[-300:])

    def test_ortam_degiskeni_verilmisse_dokunulmaz(self):
        with patch.dict(os.environ, {'CADRE_KOSU_TAVAN_USD': '1.5'}):
            self.assertEqual(self.kosu.zor_tavan_uygula(self.store, self.rid, 'p'), (2.0, 2.0))
        self.assertEqual(self.store.read(self.rid)['budget_cap_usd'], 2.0)


class KatalogHalkaTest(unittest.TestCase):  # B161 (401/not-in-account cell)
    def test_hesapta_olmayan_model_zincire_girmez_nedeni_kayitta(self):
        import katalog
        import yonlendirici
        hedef = yonlendirici.ROLLER['impl'][0]
        with patch.object(katalog, 'kullanilabilir', side_effect=lambda sag, mid: f'{sag}/{mid}' != hedef):
            zincir, neden = yonlendirici.zincir('impl', 'orta', 30000, b=butce(), d={})
        self.assertNotIn(hedef, zincir)
        self.assertIn(f'{hedef}: hesapta yok', neden)


class BekciSahipTest(unittest.TestCase):  # B191
    """B191 (23.09 pre-check): guards of interrupted test runs lived for 3–7 days; the watch revived kota-izle in its own home
    and asked DeepSeek on every tick with the real auth.json key. When the owner process dies the guard exits; no network in test mode."""

    def setUp(self):
        import subprocess as sp
        import time
        self.sp, self.t = sp, Path(tempfile.mkdtemp(prefix='b191-'))
        (self.t / 'home/.local/share/opencode').mkdir(parents=True)
        (self.t / 'home/.local/share/opencode/auth.json').write_text(json.dumps({'deepseek': {'key': 'sahte-anahtar'}}))
        (self.t / 'qe/reports').mkdir(parents=True)
        (self.t / 'qe/reports' / time.strftime('OZET-%Y%m%d.md')).write_text('hazir\n')   # the morning summary is skipped
        (self.t / 'bin').mkdir()
        (self.t / 'bin/curl').write_text(f'#!/bin/sh\necho "$@" >> "{self.t / "curl-cagri"}"\n')
        (self.t / 'bin/curl').chmod(0o755)
        self.sahip = sp.Popen(['sleep', '120'])
        self.addCleanup(self.temizle)
        self.env = dict(os.environ, HOME=str(self.t / 'home'), CADRE_HOME=str(self.t / 'qe'), CADRE_ENGINE_ROOT=str(LIB.parent),
                        PATH=f"{self.t / 'bin'}:{os.environ['PATH']}", CADRE_NOBET_ARALIK='1', CADRE_KOTA_ARALIK='1',
                        CADRE_BEKCI_SAHIP_PID=str(self.sahip.pid), OPENCODE_DB=str(self.t / 'yok.db'), CADRE_AG_YOK='1')

    def temizle(self):
        import signal
        self.sahip.kill()
        self.sahip.wait()
        for ad in ('saglik-nobet', 'kota-izle'):
            try:
                pid = int((self.t / 'qe/zincir' / f'{ad}.pid').read_text())
                os.killpg(os.getpgid(pid), signal.SIGKILL)   # B266: the sleeping child dies too
            except (OSError, ValueError):
                pass

    def canli(self, ad):
        try:
            os.kill(int((self.t / 'qe/zincir' / f'{ad}.pid').read_text()), 0)
            return True
        except (OSError, ValueError):
            return False

    def bekle(self, kosul, sn=30):
        import time
        son = time.time() + sn
        while time.time() < son:
            if kosul():
                return True
            time.sleep(0.2)
        return False

    def baslat(self, ad):
        surec = self.sp.Popen(['bash', str(LIB.parent / f'bin/{ad}.sh')], env=self.env, stdout=self.sp.DEVNULL,
                              stderr=self.sp.DEVNULL, start_new_session=True)
        self.addCleanup(lambda: surec.poll() is None and os.killpg(surec.pid, signal.SIGKILL))   # B266
        return surec

    def test_bekci_sigkill_ile_olse_de_uykusu_oksuz_kalmaz(self):
        """B266 (29.09): when the guard died with SIGKILL (tests kill it that way) the foreground `sleep 3600` stayed for hours with PPID 1
        (14 orphans after one regression). Sleep is split into chunks of <=5 s (`uyu`): the group empties within ~5 s at the latest."""
        import signal
        self.env['CADRE_KOTA_ARALIK'] = '3600'   # D15: a 1 s interval masked the old `sleep 3600` bug (the old code was green too)
        kota = self.baslat('kota-izle')
        self.assertTrue(self.bekle(lambda: (self.t / 'qe/zincir/kota-izle.tik').exists()))
        os.kill(kota.pid, signal.SIGKILL)
        kota.wait(timeout=10)

        def grup_bos():
            try:
                os.killpg(kota.pid, 0)
                return False
            except ProcessLookupError:
                return True
            except PermissionError:   # macOS: killpg returns EPERM if only zombies are left in the group; wait until they are reaped
                return False
        self.assertTrue(self.bekle(grup_bos, sn=8), 'bekcinin surec grubunda oksuz sleep kaldi (>8 sn)')

    def test_sahip_olunce_bekciler_cikar_ve_yalitilmis_evde_ag_cagrisi_yok(self):
        """The quota guard starts first so the watch does not revive it (a revived guard ticks at 1 h intervals in the persistent environment).
        06.10 QA: with CADRE_AG_YOK=1 a test home with a fake key contacts no provider endpoint (the new form of B191)."""
        (self.t / 'qe/zincir').mkdir(parents=True, exist_ok=True)
        (self.t / 'qe/zincir/openai-admin-anahtar').write_text('sk-admin-sahte')
        kota = self.baslat('kota-izle')
        self.assertTrue(self.bekle(lambda: (self.t / 'qe/zincir/kota-izle.tik').exists()))
        nobet = self.baslat('saglik-nobet')
        self.assertTrue(self.bekle(lambda: (self.t / 'qe/logs/saglik-nobet.log').exists()
                                   and 'kosu defteri' in (self.t / 'qe/logs/saglik-nobet.log').read_text()), 'ilk tik bitmeli')
        self.assertIsNone(nobet.poll(), 'sahip yasarken nobetci calisir')
        self.sahip.kill()
        self.sahip.wait()
        self.assertTrue(self.bekle(lambda: nobet.poll() is not None and kota.poll() is not None),
                        'sahip olunce iki bekci de cikmali (yetim kalmaz)')
        cagri = (self.t / 'curl-cagri').read_text() if (self.t / 'curl-cagri').exists() else ''
        self.assertNotIn('deepseek', cagri, 'CADRE_AG_YOK=1: sahte anahtarla saglayici ucuna gidilmez')
        self.assertFalse((self.t / 'qe/zincir/openai-kurulus.json').exists(), 'CADRE_AG_YOK=1: OpenAI kurulus ucu cagrilmaz')


if __name__ == '__main__':
    unittest.main()
