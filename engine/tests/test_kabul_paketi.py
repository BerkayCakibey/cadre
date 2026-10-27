"""Acceptance package driver (kabul_paketi.py + kabul_sahte.py): frozen fixture verification, check/decision rules and the real engine path
with a fake provider (typed request -> two engine approvals -> plan/separate review -> worker/QA/judge -> delivery). A fake PASS is not a live
acceptance (LOCAL_PASS); a judge rejection cannot force the driver to PASS. Fixtures are produced from canli_senaryo constants; no campaign directory is needed."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from contextlib import closing, contextmanager

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import canli_senaryo  # noqa: E402
import kabul_paketi  # noqa: E402

FROZEN_BACKLOG = '# Frozen synthetic acceptance backlog\n\n' + ''.join(
    f"## [ ] {mid} — {tanim}\nBağımlılık: {', '.join(bag) or 'yok'}\nKabul: python3 -m unittest -v tests.test_{mid.lower()}\n\n"
    for mid, (tanim, bag, _) in canli_senaryo.BACKLOG_TESTLER.items())
ORDERS = 'order_id,date,status,amount\no1,2026-10-14,completed,12.30\no2,2026-10-14,cancelled,4.50\no3,2026-10-14,completed,7.70\n'


def fixture_agaci(kok: Path) -> Path:
    fixtures = kok / 'fixtures'
    r01 = fixtures / 'R01'
    r01.mkdir(parents=True)
    (r01 / 'calc.py').write_text(canli_senaryo.CALC)
    (r01 / 'test_calc.py').write_text(canli_senaryo.TEST_CALC)
    (r01 / 'request.txt').write_text(canli_senaryo.ISTEM_CALC)
    r04 = fixtures / 'R04'
    (r04 / 'tests').mkdir(parents=True)
    (r04 / 'tests/__init__.py').write_text('')
    for mid, (_, _, govde) in canli_senaryo.BACKLOG_TESTLER.items():
        (r04 / f'tests/test_{mid.lower()}.py').write_text('import unittest\n' + govde)
    (r04 / 'metin.py').write_text(''.join(f'def {ad}(metin):\n    raise NotImplementedError\n\n' for ad in ('kelimeler', 'kelime_say', 'ters_cevir', 'en_uzun_kelime', 'ozet')))
    (r04 / 'metin_cli.py').write_text('raise NotImplementedError("B005 pending")\n')
    (r04 / 'BACKLOG.md').write_text(FROZEN_BACKLOG)
    (r04 / 'request.txt').write_text('YALNIZ PLAN: BACKLOG.md için kaynak kimlikleri/kriterleri/bağımlılıkları taşıyan plan üret ve ayrı temiz incelemeye sun. '
                                     'Uygulama/çocuk başlatma; ürün kodunu veya tests/ dizinini değiştirme.\n')
    r07 = fixtures / 'R07'
    r07.mkdir()
    (r07 / 'orders.csv').write_text(ORDERS)
    (r07 / 'metadata.json').write_text(json.dumps({'synthetic': True, 'as_of': '2026-10-15', 'source_date': '2026-10-14', 'max_age_days': 7}, indent=2) + '\n')
    (r07 / 'contradictory-summary.txt').write_text('SYNTHETIC conflicting claim: completed total = 21.00\n')
    (r07 / 'request.txt').write_text('Ağsız operasyon raporu: orders.csv için tarihli kaynak, hesap ve yorumları ayır. Sayılar kaynak satırlarına bağlı olsun.\n')
    manifest = {str(p.relative_to(fixtures)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(fixtures.rglob('*')) if p.is_file()}
    (kok / 'fixture-manifest.json').write_text(json.dumps(manifest, indent=2))
    return fixtures


class KabulPaketiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory(prefix='kabul-paketi-')
        cls.kok = Path(cls.temp.name).resolve()
        cls.fixtures = fixture_agaci(cls.kok)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def surucu(self, kimlik: str, *ek: str, ev: str | None = None) -> tuple[subprocess.CompletedProcess, dict | None]:
        ev_yolu = self.kok / (ev or f'ev-{kimlik}-{len(ek)}')
        cikti = ev_yolu / 'kanit'
        env = {k: v for k, v in os.environ.items() if not k.startswith(('CADRE_', 'OPENCODE_', 'E2E_'))}
        env.update(CADRE_HOME=str(ev_yolu), CADRE_ENGINE_ROOT=str(TESTS.parent))
        result = subprocess.run([sys.executable, str(TESTS / 'kabul_paketi.py'), kimlik, '--fixture-dir', str(self.fixtures), '--yol', 'sahte',
                                 '--dakika', '6', '--tavan', '2', '--cikti', str(cikti), *ek], env=env, capture_output=True, text=True, timeout=500)
        ozet = json.loads((cikti / 'ozet.json').read_text()) if (cikti / 'ozet.json').exists() else None
        return result, ozet

    def test_runtime_home_and_fixture_manifest_are_enforced(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith('CADRE_')}
        varsayilan = subprocess.run([sys.executable, str(TESTS / 'kabul_paketi.py'), 'R01', '--fixture-dir', str(self.fixtures)],
                                    env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(varsayilan.returncode, 0)
        self.assertIn('izole', varsayilan.stderr)
        bozuk = self.kok / 'bozuk'
        bozuk.mkdir()
        (bozuk / 'fixtures/R01').mkdir(parents=True)
        for ad in ('calc.py', 'test_calc.py', 'request.txt'):
            (bozuk / 'fixtures/R01' / ad).write_bytes((self.fixtures / 'R01' / ad).read_bytes())
        (bozuk / 'fixture-manifest.json').write_text(json.dumps({'R01/calc.py': '0' * 64}))
        env.update(CADRE_HOME=str(self.kok / 'ev-bozuk'), CADRE_ENGINE_ROOT=str(TESTS.parent))
        result = subprocess.run([sys.executable, str(TESTS / 'kabul_paketi.py'), 'R01', '--fixture-dir', str(bozuk / 'fixtures'), '--yol', 'sahte'],
                                env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('manifestle uyusmuyor', result.stderr)
        self.assertFalse((self.kok / 'ev-bozuk' / 'runs').exists(), 'manifest uyusmazliginda hic istek kaydedilmez')

    def test_small_fix_scenario_passes_locally_with_positive_and_negative_checks(self):
        result, ozet = self.surucu('R01')
        self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:])
        self.assertIsNotNone(ozet)
        self.assertEqual(ozet['karar'], 'LOCAL_PASS', ozet['kontrol_sayisi'])
        self.assertFalse(ozet['canli'])
        sonuclar = {c['id']: c['sonuc'] for c in ozet['kontroller']}
        self.assertEqual(sonuclar['R01-N1'], 'PASS')
        self.assertEqual(sonuclar['R01-O2'], 'PASS')
        self.assertEqual(sonuclar['R01-O3'], 'PASS', 'QA sonrasi urun degisikligi kabulu gecersiz kilmali')
        self.assertEqual(sonuclar['R01-L1'], 'NOT_APPLICABLE', 'sahte yolda makbuz kapisi uygulanamaz')
        self.assertTrue(any(c['sonuc'] == 'GATE' for c in ozet['kontroller']), 'olumsuz davranislar mevcut test ailesine baglanir')
        self.assertGreaterEqual(ozet['kontrol_sayisi']['normal'], 1)
        self.assertGreaterEqual(ozet['kontrol_sayisi']['olumsuz_sinir'], 1)
        self.assertEqual(ozet['fixture_dogrulama']['fark'], [])
        self.assertEqual(ozet['sahte_cagrilar'][:2], ['plan', 'plan-review'])
        self.assertIn('worker', ozet['sahte_cagrilar'])
        self.assertTrue(Path(ozet['kosular'][0]).name.startswith('request-'))
        satirlar = (Path(result.stdout.strip().splitlines()[-1].split('OZET: ')[-1]).parent.parent / 'kabul-paketi.jsonl').read_text().splitlines()
        self.assertEqual(json.loads(satirlar[-1])['karar'], 'LOCAL_PASS')

    def test_plan_only_delivers_a_reviewed_plan_without_implementation(self):
        result, ozet = self.surucu('R04')
        self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:])
        self.assertEqual(ozet['karar'], 'LOCAL_PASS', ozet['kontrol_sayisi'])
        sonuclar = {c['id']: c for c in ozet['kontroller']}
        self.assertEqual(sonuclar['R04-N2']['sonuc'], 'PASS')
        self.assertEqual(sonuclar['R04-O1']['sonuc'], 'PASS')
        self.assertEqual(sonuclar['R04-O2']['sonuc'], 'PASS')
        self.assertEqual(sonuclar['R04-O3']['sonuc'], 'PASS')
        self.assertEqual(ozet['sahte_cagrilar'], ['plan', 'plan-review'], 'yalniz plan: isci ve son hakem cagrisi yok')

    def test_evidence_report_passes_and_local_report_qa_rejects_contradiction_and_missing_source(self):
        result, ozet = self.surucu('R07')
        self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:])
        self.assertEqual(ozet['karar'], 'LOCAL_PASS', ozet['kontrol_sayisi'])
        sonuclar = {c['id']: c for c in ozet['kontroller']}
        self.assertEqual(sonuclar['R07-N3']['sonuc'], 'PASS', 'bagimsiz hesap raporla eslesmeli')
        self.assertEqual(sonuclar['R07-N5']['sonuc'], 'PASS', 'tek analist karari raporda gorunmeli (N-REP03)')
        self.assertEqual(sonuclar['R07-O1']['sonuc'], 'PASS')
        self.assertEqual(sonuclar['R07-O3-celiski']['sonuc'], 'PASS')
        self.assertEqual(sonuclar['R07-O3-eksik-kaynak']['sonuc'], 'PASS')
        self.assertEqual(ozet['bagimsiz_hesap'], {'completed': '20.00', 'cancelled': '4.50', 'all': '24.50'})

    def test_final_reviewer_rejection_cannot_be_reported_as_pass(self):
        result, ozet = self.surucu('R01', '--kip', 'hakem-ret', ev='ev-r01-ret')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(ozet['karar'], 'FAIL')
        sonuclar = {c['id']: c for c in ozet['kontroller']}
        self.assertEqual(sonuclar['R01-N1']['sonuc'], 'FAIL')
        self.assertNotEqual(sonuclar['R01-N1']['gozlenen'][2], 'PASS')
        self.assertIn('R01-N1', ozet['kontrol_sayisi']['basarisiz'])


    def test_obligation_refund_flag_keeps_the_unverified_dry_run_share_reserved(self):
        result, ozet = self.surucu('R01', '--yukumluluk-iadesi', '--paket-tavan', '5', ev='ev-iade')
        self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:])
        self.assertEqual(ozet['karar'], 'LOCAL_PASS')
        self.assertTrue(ozet['paket_payi']['yukumluluk_iadesi'])
        self.assertEqual((ozet['paket_payi']['brut_usd'], ozet['paket_payi']['iade_usd'], ozet['paket_payi']['kalan_usd']), (2.0, 0.0, 3.0))
        self.assertEqual([o['sonuc'] for o in ozet['paket_iadesi']], ['bekliyor'])
        self.assertIn('makbuz', ozet['paket_iadesi'][0]['neden'])

    def test_proposal_only_mode_honours_operator_chain_and_makes_no_approval_or_call(self):
        """CXR-04: CADRE_ZINCIR_<ROL> is carried to the subprocess and the proposal lists only that model; --yalniz-oneri makes no approval/call."""
        ev_k, ev = self.kok / 'ev-oneri-kucuk', self.kok / 'ev-oneri'
        cikti = ev / 'kanit'
        env = {k: v for k, v in os.environ.items() if not k.startswith(('CADRE_', 'OPENCODE_', 'E2E_'))}
        env.update(CADRE_ENGINE_ROOT=str(TESTS.parent), CADRE_ZINCIR_REVIEW='opencode-go/glm-5.3', CADRE_ZINCIR_KALITE='opencode-go/glm-5.3')
        kucuk = subprocess.run([sys.executable, str(TESTS / 'kabul_paketi.py'), 'R01', '--fixture-dir', str(self.fixtures), '--yol', 'sahte',
                                '--dakika', '6', '--tavan', '0.25', '--paket-tavan', '4', '--cikti', str(ev_k / 'kanit'), '--yalniz-oneri'],
                               env={**env, 'CADRE_HOME': str(ev_k)}, capture_output=True, text=True, timeout=300)
        self.assertNotEqual(kucuk.returncode, 0, 'fikstur katalogu/asgari rezervasyonla 0,25 USD oneri alamaz; onay ve cagri olmadan durur')
        self.assertIn('plan-review-no-capacity', kucuk.stdout)
        self.assertFalse((ev_k / 'kabul-calls.jsonl').exists())
        self.assertEqual([t['usd'] for t in json.loads((ev_k / 'kabul-paketi-pay.json').read_text())['tahsisler']], [0.25],
                         'reddedilen istek de tahsis defterinde kalir; pay sifirlanmaz')
        result = subprocess.run([sys.executable, str(TESTS / 'kabul_paketi.py'), 'R01', '--fixture-dir', str(self.fixtures), '--yol', 'sahte',
                                 '--dakika', '6', '--tavan', '2', '--paket-tavan', '4', '--cikti', str(cikti), '--yalniz-oneri'],
                                env={**env, 'CADRE_HOME': str(ev)}, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout[-1500:] + result.stderr[-1500:])
        ozet = json.loads((cikti / 'ozet.json').read_text())
        self.assertEqual(ozet['karar'], 'ONERI')
        self.assertEqual(ozet['oneri_modelleri']['review'], ['opencode-go/glm-5.3'])
        self.assertEqual(ozet['oneri_modelleri']['kalite'], ['opencode-go/glm-5.3'])
        self.assertEqual(ozet['zincir_ortami'], {'CADRE_ZINCIR_REVIEW': 'opencode-go/glm-5.3', 'CADRE_ZINCIR_KALITE': 'opencode-go/glm-5.3'})
        self.assertEqual(ozet['oneri_toplam_usd'], '2')
        self.assertFalse((ev / 'kabul-calls.jsonl').exists(), 'oneri asamasinda model cagrisi yok')
        kayit = json.loads((ev / 'runs' / ozet['kosular'][0] / 'run.json').read_text())
        self.assertNotIn('request_permission', kayit)
        pay = json.loads((cikti.parent / 'kabul-paketi-pay.json').read_text())
        self.assertEqual([t['usd'] for t in pay['tahsisler']], [2.0])


class GuvenliKomutTests(unittest.TestCase):
    """CXR-01: product acceptance commands run in the engine's evidence sandbox; external file writes, the parent environment and the network are denied on the same real path."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix='kabul-kum-')
        self.addCleanup(temp.cleanup)
        self.kok = Path(temp.name).resolve()
        self.wt = self.kok / 'wt'
        self.wt.mkdir()
        self.gecici = self.kok / 'gecici'
        self.dis = self.kok / 'disarida.txt'
        self.dis.write_text('sentetik-isaret')

    def test_product_code_cannot_write_outside_read_parent_env_or_use_the_network(self):
        betik = self.wt / 'urun.py'
        betik.write_text(f"""import json, os, socket, pathlib
sonuc = {{}}
p = pathlib.Path({str(self.dis)!r})
try:
    p.write_text(p.read_text() + '-yazildi'); sonuc['dis_yazma'] = 'izinli'
except OSError as exc:
    sonuc['dis_yazma'] = 'ret:' + type(exc).__name__
sonuc['ust_ortam'] = os.environ.get('KABUL_SENTETIK_SIR')
sonuc['cadre_ortam'] = sorted(k for k in os.environ if k.startswith('CADRE_'))
try:
    s = socket.create_connection(('127.0.0.1', 9), timeout=1); s.close(); sonuc['ag'] = 'izinli'
except OSError as exc:
    sonuc['ag'] = 'ret:' + type(exc).__name__
pathlib.Path('icerde.txt').write_text('ok'); sonuc['ic_yazma'] = pathlib.Path('icerde.txt').read_text()
print(json.dumps(sonuc))
""")
        with patch.dict(os.environ, {'KABUL_SENTETIK_SIR': 'sentetik', 'CADRE_RUN_ID': 'sizmamali'}):
            rc, cikti = kabul_paketi.guvenli_komut(f'{sys.executable} urun.py', self.wt, self.gecici, 60)
        self.assertEqual(rc, 0, cikti)
        veri = kabul_paketi.json_satiri(cikti)
        self.assertIsNotNone(veri, cikti)
        self.assertTrue(veri['dis_yazma'].startswith('ret:'), veri)
        self.assertEqual(self.dis.read_text(), 'sentetik-isaret')
        self.assertIsNone(veri['ust_ortam'])
        self.assertEqual(veri['cadre_ortam'], [])
        self.assertTrue(veri['ag'].startswith('ret:'), veri)
        self.assertEqual(veri['ic_yazma'], 'ok')

    def test_preexisting_external_hardlink_alias_blocks_the_command_and_new_links_are_refused(self):
        """CXR2-01: a pre-existing external inode alias is denied before the command runs (the FIX-25 preflight on the same path); creating a new
        hardlink inside the sandbox fails too; normal worktree writes stay allowed."""
        os.link(self.dis, self.wt / 'alias.txt')
        (self.wt / 'alias.py').write_text("from pathlib import Path\nPath('alias.txt').write_text('yazildi')\n")
        rc, cikti = kabul_paketi.guvenli_komut(f'{sys.executable} alias.py', self.wt, self.gecici, 30)
        self.assertIsNone(rc)
        self.assertIn('hardlink-untrusted', cikti)
        self.assertEqual(self.dis.read_text(), 'sentetik-isaret')
        (self.wt / 'alias.txt').unlink()
        (self.wt / 'link.py').write_text(f"""import json, os
try:
    os.link({str(self.dis)!r}, 'yeni-alias.txt'); print(json.dumps({{'link': 'olustu'}}))
except OSError as exc:
    print(json.dumps({{'link': 'ret:' + type(exc).__name__}}))
""")
        rc, cikti = kabul_paketi.guvenli_komut(f'{sys.executable} link.py', self.wt, self.gecici, 30)
        self.assertEqual(rc, 0, cikti)
        self.assertTrue(kabul_paketi.json_satiri(cikti)['link'].startswith('ret:'), cikti)
        self.assertFalse((self.wt / 'yeni-alias.txt').exists())
        self.assertEqual(os.stat(self.dis).st_nlink, 1)
        (self.wt / 'normal.py').write_text("from pathlib import Path\nPath('normal.txt').write_text('ok'); print('ok')\n")
        rc, cikti = kabul_paketi.guvenli_komut(f'{sys.executable} normal.py', self.wt, self.gecici, 30)
        self.assertEqual((rc, (self.wt / 'normal.txt').read_text()), (0, 'ok'))
        gecici_alias = self.gecici / 'gecici-alias.txt'
        os.link(self.dis, gecici_alias)
        rc, cikti = kabul_paketi.guvenli_komut(f'{sys.executable} normal.py', self.wt, self.gecici, 30)
        self.assertIsNone(rc, 'gecici alanda dis alias da komutu durdurur')

    def test_unrunnable_or_timed_out_command_is_not_a_pass(self):
        (self.wt / 'sonsuz.py').write_text('import time\nwhile True: time.sleep(.1)\n')
        rc, cikti = kabul_paketi.guvenli_komut(f'{sys.executable} sonsuz.py', self.wt, self.gecici, 1)
        self.assertIsNone(rc)
        self.assertIn('zaman asimi', cikti)
        self.assertIsNone(kabul_paketi.json_satiri('kismi {"a": 1\nson'))


class PaketPayiTests(unittest.TestCase):
    """CXR-02: shared package cap; N/N+1, retry/new process and the R06 two-variant split."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix='kabul-pay-')
        self.addCleanup(temp.cleanup)
        self.dosya = Path(temp.name) / 'kabul-paketi-pay.json'

    def test_allocations_stop_at_the_common_cap_and_survive_a_new_process(self):
        pay = kabul_paketi.PaketPayi(self.dosya, 2.0)
        for senaryo, usd in (('R01', .25), ('R02', .5), ('R03', .25), ('R04', .25), ('R05', .25), ('R06', .125), ('R06', .125), ('R07', .25)):
            pay.tahsis(senaryo, senaryo + '-' + str(usd), usd)
        self.assertAlmostEqual(pay.tahsis_edilen(), 2.0)
        self.assertAlmostEqual(pay.kalan(), 0.0)
        yeniden = kabul_paketi.PaketPayi(self.dosya, 2.0)   # a new process does not reset the remaining share
        self.assertAlmostEqual(yeniden.kalan(), 0.0)
        with self.assertRaisesRegex(RuntimeError, 'ortak paket payi yetersiz'):
            yeniden.tahsis('R01', 'R01-tekrar', .25)
        self.assertEqual(len(json.loads(self.dosya.read_text())['tahsisler']), 8)
        with self.assertRaisesRegex(ValueError, 'uyusmuyor'):
            kabul_paketi.PaketPayi(self.dosya, 3.0)
        for kotu in (0.0, -1.0, float('nan')):
            with self.assertRaises(ValueError):
                kabul_paketi.PaketPayi(Path(self.dosya.parent / 'bos.json'), 1.0).tahsis('R01', 'x', kotu)

    def test_overlapping_allocators_and_processes_cannot_exceed_the_cap(self):
        """CXR2-02: two objects opened beforehand and two separate processes; the ledger is re-read under the lock, the second allocation is denied,
        the first line is not lost; an old memory snapshot cannot allocate."""
        bir = kabul_paketi.PaketPayi(self.dosya, 2.0)
        iki = kabul_paketi.PaketPayi(self.dosya, 2.0)
        self.assertEqual(bir.tahsis('R01', 'ilk', 1.25), 1.25)
        with self.assertRaisesRegex(RuntimeError, 'ortak paket payi yetersiz'):
            iki.tahsis('R02', 'ikinci', 1.25)
        disk = json.loads(self.dosya.read_text())
        self.assertEqual([(t['etiket'], t['usd']) for t in disk['tahsisler']], [('ilk', 1.25)])
        self.assertEqual(iki.tahsis('R02', 'ikinci', 0.75), 0.75)
        iki.bagla('ikinci', 'request-x')
        bir.bagla('ilk', 'request-y')
        disk = json.loads(self.dosya.read_text())
        self.assertEqual([(t['etiket'], t['usd'], t['rid']) for t in disk['tahsisler']], [('ilk', 1.25, 'request-y'), ('ikinci', 0.75, 'request-x')])
        with self.assertRaisesRegex(ValueError, 'uyusmuyor'):
            kabul_paketi.PaketPayi(self.dosya, 2.5)
        yarisma = self.dosya.parent / 'yarisma.json'
        betik = (f"import sys, time; sys.path.insert(0, {str(TESTS)!r}); sys.path.insert(0, {str(TESTS.parent / 'lib')!r}); import kabul_paketi as k\n"
                 f"from pathlib import Path\np = k.PaketPayi(Path({str(yarisma)!r}), 2.0)\n"
                 "time.sleep(0.3)\n"
                 "try:\n    print(p.tahsis('R', sys.argv[1], 1.25))\nexcept RuntimeError as exc:\n    print('RET')\n")
        procs = [subprocess.Popen([sys.executable, '-c', betik, f's{i}'], stdout=subprocess.PIPE, text=True) for i in range(2)]
        outs = sorted(pr.communicate(timeout=60)[0].strip() for pr in procs)
        self.assertEqual(outs, ['1.25', 'RET'], outs)
        son = json.loads(yarisma.read_text())
        self.assertEqual(len(son['tahsisler']), 1)
        self.assertLessEqual(sum(t['usd'] for t in son['tahsisler']), 2.0)

    def test_r06_variants_share_one_family_allocation(self):
        self.assertEqual(kabul_paketi.r06_paylari(0.25, None), (0.125, 0.125))
        self.assertEqual(kabul_paketi.r06_paylari(0.25, 0.05), (0.125, 0.125))
        self.assertEqual(kabul_paketi.r06_paylari(0.25, 0.2), (0.125, 0.05))
        self.assertEqual(kabul_paketi.r06_paylari(0.25, 0.3), (0.125, 0.0))
        a, b = kabul_paketi.r06_paylari(0.25, None)
        self.assertLessEqual(a + b, 0.25)


class MakbuzKapilariTests(unittest.TestCase):
    """CXR-03: a missing/broken/empty/open/unallowed receipt cannot produce PASS; two-ledger parity."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix='kabul-makbuz-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        (self.home / 'zincir').mkdir()
        self.db = self.home / 'zincir/request-budget.db'
        self.aylik = self.home / 'zincir/aylik-butce.db'

    def satir(self, con, ident, provider, reserved, actual, receipt, run_id='kosu-1', ek=None):
        con.execute('insert into reservation values(?,?,?,?,?,?,?)', (provider, reserved, actual, 1.0,
                    json.dumps({'run_id': run_id, **(ek or {})}), receipt, ident))

    @contextmanager
    def defter(self, yol=None, sema=None):
        con = sqlite3.connect(yol or self.db)
        try:
            con.execute(sema or 'create table if not exists reservation (provider text, reserved integer, actual integer, created real, metadata text, receipt text, id text)')
            yield con
            con.commit()
        finally:
            con.close()

    def test_missing_unreadable_and_empty_ledgers_block_instead_of_passing(self):
        yok = kabul_paketi.makbuz_kapilari(self.home, ['kosu-1'], 'opencode-go/')
        self.assertEqual((yok['durum'], yok['okunabilir']), ('BLOCKED', False))
        with self.defter(sema='create table wrong_schema (id text)'):
            pass
        bozuk = kabul_paketi.makbuz_kapilari(self.home, ['kosu-1'], 'opencode-go/')
        self.assertEqual(bozuk['durum'], 'BLOCKED')
        self.assertIn('okunamadi', bozuk['hata'])
        self.db.unlink()
        with self.defter() as con:
            self.satir(con, 'r9', 'opencode-go', 1000, 500, 'r9:x', run_id='baska-kosu')
        bos = kabul_paketi.makbuz_kapilari(self.home, ['kosu-1'], 'opencode-go/')
        self.assertEqual((bos['durum'], bos['satir']), ('BLOCKED', 0))
        self.assertIn('sifir kayit', bos['neden'])
        self.assertEqual(kabul_paketi.makbuz_kapilari(self.home, [], 'opencode-go/')['durum'], 'BLOCKED')

    def test_open_reservation_foreign_provider_and_guarantee_fail_while_closed_go_rows_pass(self):
        with self.defter() as con:
            self.satir(con, 'r1', 'opencode-go#glm-5.3', 2000, 1500, 'r1:ok')
        self.assertEqual(kabul_paketi.makbuz_kapilari(self.home, ['kosu-1'], 'opencode-go/')['durum'], 'PASS')
        with self.defter() as con:
            self.satir(con, 'r2', 'opencode-go', 3000, None, None)
        acik = kabul_paketi.makbuz_kapilari(self.home, ['kosu-1'], 'opencode-go/')
        self.assertEqual((acik['durum'], acik['askida']), ('FAIL', 1))
        with self.defter() as con:
            con.execute("update reservation set actual=0, receipt='unused:r2' where id='r2'")
            self.satir(con, 'r3', 'openrouter#z-ai/glm-5.3', 900, 900, 'r3:ok')
        yabanci = kabul_paketi.makbuz_kapilari(self.home, ['kosu-1'], 'opencode-go/')
        self.assertEqual(yabanci['durum'], 'FAIL')
        self.assertIn('openrouter#z-ai/glm-5.3', yabanci['izinsiz'])
        with self.defter() as con:
            con.execute("delete from reservation where id='r3'")
            self.satir(con, 'r4', 'opencode-go', 800, None, None, ek={'guvence': True})
        guvence = kabul_paketi.makbuz_kapilari(self.home, ['kosu-1'], 'opencode-go/')
        self.assertEqual((guvence['durum'], guvence['acik_guvence']), ('FAIL', 1))
        with self.defter() as con:
            con.execute("update reservation set actual=0, receipt='claimed:r4' where id='r4'")
        self.assertEqual(kabul_paketi.makbuz_kapilari(self.home, ['kosu-1'], 'opencode-go/')['durum'], 'PASS')
        with self.defter() as con:
            con.execute("delete from reservation where id='r1'")
        yalniz_guvence = kabul_paketi.makbuz_kapilari(self.home, ['kosu-1'], 'opencode-go/')
        self.assertEqual(yalniz_guvence['durum'], 'BLOCKED')
        self.assertIn('kapanmis makbuz yok', yalniz_guvence['neden'])

    def test_cash_rows_must_match_the_monthly_ledger_and_go_only_runs_must_not_have_monthly_rows(self):
        with self.defter() as con:
            self.satir(con, 'c1', 'openrouter#z-ai/glm-5.3', 700, 650, 'c1:ok')
        eksik = kabul_paketi.makbuz_kapilari(self.home, ['kosu-1'], 'openrouter/')
        self.assertEqual(eksik['durum'], 'BLOCKED')
        self.assertIn('aylik defter yok', eksik['neden'])
        with self.defter(self.aylik, 'create table reservation(id text primary key, provider text, period text, created real, reserved integer, actual integer, receipt text unique, metadata text)') as con:
            con.execute('insert into reservation values(?,?,?,?,?,?,?,?)', ('m1', 'openrouter', '2026-10', 1.0, 700, 600, 'c1:ok', json.dumps({'run_id': 'kosu-1'})))
        uyusmaz = kabul_paketi.makbuz_kapilari(self.home, ['kosu-1'], 'openrouter/')
        self.assertEqual(uyusmaz['durum'], 'FAIL')
        self.assertIn('iki defter uyusmuyor', uyusmaz['neden'])
        with self.defter(self.aylik, "update reservation set actual=650 where id='m1'"):
            pass
        self.assertEqual(kabul_paketi.makbuz_kapilari(self.home, ['kosu-1'], 'openrouter/')['durum'], 'PASS')
        with self.defter() as con:
            con.execute("delete from reservation where id='c1'")
            self.satir(con, 'g1', 'opencode-go', 500, 400, 'g1:ok')
        go_only = kabul_paketi.makbuz_kapilari(self.home, ['kosu-1'], 'opencode-go/')
        self.assertEqual(go_only['durum'], 'FAIL')
        self.assertIn('nakit cagri yokken aylik defterde', go_only['neden'])

    def test_monthly_rows_link_through_monthly_id_and_session_like_the_live_plugin_writes_them(self):
        with self.defter() as con:
            self.satir(con, 'c1', 'openrouter#z-ai/glm-5.3', 700, 650, 'c1:gen-1', ek={'aylik_id': 'm1', 'session_id': 'ses_a'})
            self.satir(con, 'c2', 'openrouter#z-ai/glm-5.3', 500, 100, 'c2:gen-2', ek={'aylik_id': 'm2', 'session_id': 'ses_b'})
        sema = ('create table reservation(id text primary key, provider text, period text, created real, reserved integer, actual integer, '
                'receipt text unique, metadata text)')
        with self.defter(self.aylik, sema) as con:
            for ident, actual, receipt, oturum in (('m1', 650, 'm1:gen-1', 'ses_a'), ('m2', 100, 'm2:gen-2', 'ses_b'),
                                                   ('m3', 0, 'm3:provider-budget-denied', 'ses_b'), ('m9', 5000, 'm9:gen-9', 'ses_baska')):
                con.execute('insert into reservation values(?,?,?,?,?,?,?,?)', (ident, 'openrouter', '2026-10', 1.0, 900, actual, receipt,
                            json.dumps({'model': 'z-ai/glm-5.3', 'session_id': oturum, 'role': 'build'})))
        sonuc = kabul_paketi.makbuz_kapilari(self.home, ['kosu-1'], 'openrouter/')
        self.assertEqual((sonuc['durum'], sonuc['aylik']['satir'], sonuc['aylik']['gercek_micro']), ('PASS', 3, 750),
                         'aylik satirlar aylik_id ve oturumla aileye baglanir; baska oturum sayilmaz')
        with self.defter(self.aylik, "update reservation set actual=null, receipt=null where id='m3'"):
            pass
        acik = kabul_paketi.makbuz_kapilari(self.home, ['kosu-1'], 'openrouter/')
        self.assertEqual((acik['durum'], acik['aylik']['acik']), ('FAIL', 1), 'ailenin oturumunda acik aylik rezerv kapanis degildir')
        with self.defter(self.aylik, "update reservation set actual=0, receipt='m3:provider-budget-denied' where id='m3'"):
            pass
        with self.defter(self.aylik, "update reservation set actual=600 where id='m1'"):
            pass
        uyusmaz = kabul_paketi.makbuz_kapilari(self.home, ['kosu-1'], 'openrouter/')
        self.assertEqual(uyusmaz['durum'], 'FAIL')
        self.assertIn('iki defter uyusmuyor', uyusmaz['neden'])


class YukumlulukIadesiTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix='kabul-iade-')
        self.addCleanup(temp.cleanup)
        self.kok = Path(temp.name).resolve()
        self.home = self.kok / 'ev'
        (self.home / 'zincir').mkdir(parents=True)
        (self.home / 'runs').mkdir()
        self.dosya = self.kok / 'kanit' / 'kabul-paketi-pay.json'
        self.db = self.home / 'zincir/request-budget.db'
        with closing(sqlite3.connect(self.db)) as con:
            con.execute('create table reservation (provider text, reserved integer, actual integer, created real, metadata text, receipt text, id text)')
            con.commit()

    def kosu(self, rid: str, durum: str = 'completed', **ek) -> None:
        yol = self.home / 'runs' / rid
        yol.mkdir(parents=True, exist_ok=True)
        simdi = time.time()
        (yol / 'run.json').write_text(json.dumps({'version': 1, 'run_id': rid, 'project': 'kabul-iade', 'state': durum, 'tasks': {},
                                                  'created': simdi, 'updated': simdi, **ek}))

    def makbuz(self, ident: str, rid: str, rezerve: int, gercek: int | None, saglayici: str = 'opencode-go#glm-5.3',
               makbuz: str | None = None, ek: dict | None = None) -> None:
        with closing(sqlite3.connect(self.db)) as con:
            con.execute('insert into reservation values(?,?,?,?,?,?,?)', (saglayici, rezerve, gercek, 1.0, json.dumps({'run_id': rid, **(ek or {})}),
                        makbuz if makbuz is not None else (f'{ident}:ok' if gercek is not None else None), ident))
            con.commit()

    def pay(self, tavan: float = 5.0) -> kabul_paketi.PaketPayi:
        return kabul_paketi.PaketPayi(self.dosya, tavan)

    def denetle(self, pay: kabul_paketi.PaketPayi) -> list[dict]:
        return pay.iade_denetimi(self.home, 'opencode-go/')

    def test_closed_terminal_runs_release_only_their_unused_share_once_and_survive_a_new_process(self):
        pay = self.pay()
        pay.tahsis('R01', 'r01', 2.10)
        pay.bagla('r01', 'request-a')
        pay.tahsis('R02', 'r02', 2.10)
        pay.bagla('r02', 'request-b')
        with self.assertRaisesRegex(RuntimeError, 'ortak paket payi yetersiz'):
            pay.tahsis('R03', 'r03', 2.10)
        self.kosu('request-a')
        self.makbuz('m1', 'request-a', 200_180, 150_000)
        self.makbuz('m2', 'request-a', 104_481, 150_000, saglayici='opencode-go#deepseek-v4-pro')
        self.kosu('request-b', 'running')
        self.makbuz('m3', 'request-b', 300_000, 120_000)
        olaylar = self.denetle(self.pay())
        self.assertEqual([(o['rid'], o['sonuc']) for o in olaylar], [('request-a', 'iade'), ('request-b', 'bekliyor')])
        self.assertIn('terminal degil', olaylar[1]['neden'])
        yeni = self.pay()
        self.assertAlmostEqual(yeni.kalan(), 2.60)
        self.assertAlmostEqual(yeni.brut(), 4.20)
        self.assertEqual(yeni.tahsis('R03', 'r03', 2.10), 2.10)
        self.assertAlmostEqual(yeni.kalan(), 0.50)
        self.assertNotIn('iade', [o['sonuc'] for o in self.denetle(yeni) if o['rid'] == 'request-a'])
        self.assertAlmostEqual(self.pay().kalan(), 0.50, msg='ayni pay ikinci kez iade edilmez')
        self.kosu('request-b', 'cancelled')
        self.assertIn(('request-b', 'iade'), [(o['rid'], o['sonuc']) for o in self.denetle(self.pay())])
        self.assertAlmostEqual(self.pay().kalan(), 2.48)
        a = next(t for t in json.loads(self.dosya.read_text())['tahsisler'] if t['rid'] == 'request-a')
        self.assertEqual((a['usd'], a['kapanis']['yukumluluk_usd'], a['kapanis']['iade_usd'], a['kapanis']['kosular']),
                         (2.1, 0.3, 1.8, ['request-a']))

    def test_held_guarantee_foreign_unknown_missing_and_conflicting_receipts_keep_the_full_share(self):
        pay = self.pay(20.0)
        vakalar = {
            'request-askida': [('k1', 300_000, None, 'opencode-go#glm-5.3', None, None)],
            'request-guvence': [('k2', 100_000, 50_000, 'opencode-go#glm-5.3', None, None), ('k3', 400_000, None, 'opencode-go', None, {'guvence': True})],
            'request-yabanci': [('k4', 100_000, 90_000, 'openrouter#z-ai/glm-5.3', None, None)],
            'request-bilinmeyen': [('k5', 100_000, 90_000, 'gizemli#x', None, None)],
            'request-bos': [],
            'request-celiski': [('k7', 100_000, 90_000, 'opencode-go#glm-5.3', 'baska:k7', None)],
        }
        for rid, satirlar in vakalar.items():
            pay.tahsis('R', rid, 2.10)
            pay.bagla(rid, rid)
            self.kosu(rid)
            for ident, rezerve, gercek, saglayici, makbuz, ek in satirlar:
                self.makbuz(ident, rid, rezerve, gercek, saglayici, makbuz, ek)
        once = self.pay(20.0).kalan()
        olaylar = {o['rid']: o for o in self.denetle(self.pay(20.0))}
        self.assertEqual({r: o['sonuc'] for r, o in olaylar.items()}, dict.fromkeys(vakalar, 'bekliyor'))
        self.assertTrue(all('makbuz' in o['neden'] for o in olaylar.values()), olaylar)
        self.assertAlmostEqual(self.pay(20.0).kalan(), once)
        self.assertFalse(any('kapanis' in t for t in json.loads(self.dosya.read_text())['tahsisler']))

    def test_verified_obligation_above_the_share_is_charged_not_released(self):
        pay = self.pay()
        pay.tahsis('R01', 'r01', 2.10)
        pay.bagla('r01', 'request-asim')
        self.kosu('request-asim')
        self.makbuz('m1', 'request-asim', 3_000_000, 2_500_000)
        olaylar = self.denetle(pay)
        self.assertEqual([o['sonuc'] for o in olaylar], ['asim'])
        self.assertAlmostEqual(self.pay().kalan(), 2.50)
        self.assertEqual([o['sonuc'] for o in self.denetle(self.pay())], ['kapali'])
        self.assertAlmostEqual(self.pay().kalan(), 2.50)

    def test_active_owner_live_attempt_or_unfinished_child_blocks_release_until_they_end(self):
        from kosu import process_identity
        pay = self.pay()
        for rid in ('request-sahip', 'request-deneme', 'request-ust'):
            pay.tahsis('R', rid, 1.0)
            pay.bagla(rid, rid)
            self.kosu(rid)
            self.makbuz('m-' + rid, rid, 100_000, 50_000)
        tutucu = subprocess.Popen([sys.executable, '-c', f"import sys, time; sys.path.insert(0, {str(TESTS.parent / 'lib')!r}); from pathlib import Path; "
                                   f"from kosu import FileLock; kilit = FileLock(Path({str(self.home / 'runs/request-sahip/.owner.lock')!r})); "
                                   "kilit.acquire(); print('ok', flush=True); time.sleep(60)"], stdout=subprocess.PIPE, text=True)
        self.addCleanup(tutucu.stdout.close)
        self.addCleanup(lambda: tutucu.poll() is None and tutucu.kill())
        self.assertEqual(tutucu.stdout.readline().strip(), 'ok')
        isci = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(lambda: isci.poll() is None and isci.kill())
        kimlik = None
        while kimlik is None:
            kimlik = process_identity(isci.pid)
        deneme = self.home / 'runs/request-deneme/tasks/g1/attempts/a1'
        deneme.mkdir(parents=True)
        (deneme / 'process.json').write_text(json.dumps({'run_id': 'request-deneme', 'task_id': 'g1', 'attempt_id': 'a1', 'process_identity': kimlik}))
        self.kosu('request-ust', project_run_ids=['request-cocuk'])
        self.kosu('request-cocuk', 'running')
        olaylar = {o['rid']: o for o in self.denetle(pay)}
        self.assertEqual({r: o['sonuc'] for r, o in olaylar.items()}, dict.fromkeys(('request-sahip', 'request-deneme', 'request-ust'), 'bekliyor'))
        self.assertIn('sahip kilidi', olaylar['request-sahip']['neden'])
        self.assertIn('canli', olaylar['request-deneme']['neden'])
        self.assertIn('request-cocuk', olaylar['request-ust']['neden'])
        tutucu.kill()
        tutucu.communicate()
        isci.kill()
        isci.wait()
        self.kosu('request-cocuk')
        self.assertEqual({o['rid']: o['sonuc'] for o in self.denetle(pay)}, dict.fromkeys(('request-sahip', 'request-deneme', 'request-ust'), 'iade'))
        self.assertAlmostEqual(self.pay().kalan(), 5.0 - 0.15)

    def test_late_receipt_raises_the_charge_and_an_unverifiable_closed_share_returns_in_full_without_second_refund(self):
        pay = self.pay()
        pay.tahsis('R01', 'r01', 2.10)
        pay.bagla('r01', 'request-a')
        self.kosu('request-a')
        self.makbuz('m1', 'request-a', 400_000, 300_000)
        self.assertEqual([o['sonuc'] for o in self.denetle(pay)], ['iade'])
        self.assertAlmostEqual(self.pay().kalan(), 4.70)
        self.makbuz('m2', 'request-a', 200_000, 100_000)
        self.assertEqual([o['sonuc'] for o in self.denetle(self.pay())], ['gec_makbuz'])
        self.assertAlmostEqual(self.pay().kalan(), 4.60)
        self.makbuz('m3', 'request-a', 500_000, None)
        self.assertEqual([o['sonuc'] for o in self.denetle(self.pay())], ['geri_alindi'])
        self.assertAlmostEqual(self.pay().kalan(), 2.90)
        with closing(sqlite3.connect(self.db)) as con:
            con.execute("update reservation set actual=0, receipt='unused:m3' where id='m3'")
            con.commit()
        self.assertEqual([o['sonuc'] for o in self.denetle(self.pay())], ['kapali'])
        self.assertAlmostEqual(self.pay().kalan(), 2.90, msg='geri alinan pay ikinci kez iade edilmez')

    def test_two_objects_and_two_processes_cannot_refund_the_same_share_twice(self):
        bir, iki = self.pay(), self.pay()
        bir.tahsis('R01', 'r01', 2.10)
        bir.bagla('r01', 'request-a')
        self.kosu('request-a')
        self.makbuz('m1', 'request-a', 400_000, 300_000)
        self.assertEqual([o['sonuc'] for o in self.denetle(bir)] + [o['sonuc'] for o in self.denetle(iki)], ['iade', 'kapali'])
        self.assertAlmostEqual(self.pay().kalan(), 4.70)
        self.pay().tahsis('R02', 'r02', 2.10)
        self.pay().bagla('r02', 'request-b')
        self.kosu('request-b')
        self.makbuz('m2', 'request-b', 400_000, 300_000)
        betik = (f"import sys, time; sys.path.insert(0, {str(TESTS)!r}); sys.path.insert(0, {str(TESTS.parent / 'lib')!r}); import kabul_paketi as k\n"
                 f"from pathlib import Path\np = k.PaketPayi(Path({str(self.dosya)!r}), 5.0)\ntime.sleep(0.3)\n"
                 f"print(','.join(o['sonuc'] for o in p.iade_denetimi(Path({str(self.home)!r}), 'opencode-go/')))\n")
        surecler = [subprocess.Popen([sys.executable, '-c', betik], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        ciktilar = sorted(s.communicate(timeout=60)[0].strip() for s in surecler)
        self.assertEqual(ciktilar, ['kapali,iade', 'kapali,kapali'], ciktilar)
        self.assertAlmostEqual(self.pay().kalan(), 4.40)

    def test_boundary_after_refund_and_an_unbound_allocation_stays_reserved(self):
        pay = self.pay(4.20)
        pay.tahsis('R01', 'r01', 2.10)
        pay.bagla('r01', 'request-a')
        pay.tahsis('R02', 'r02', 2.10)
        self.kosu('request-a')
        self.makbuz('m1', 'request-a', 100_000, 0)
        olaylar = self.denetle(pay)
        self.assertEqual([o['sonuc'] for o in olaylar], ['iade', 'bekliyor'])
        self.assertIn('kosu kimligi yok', olaylar[1]['neden'])
        self.assertAlmostEqual(self.pay(4.20).kalan(), 2.10)
        self.assertEqual(self.pay(4.20).tahsis('R03', 'r03', 2.10), 2.10)
        with self.assertRaisesRegex(RuntimeError, 'ortak paket payi yetersiz'):
            self.pay(4.20).tahsis('R04', 'r04', 0.000001)

    def test_corrupt_refund_records_are_rejected(self):
        pay = self.pay()
        pay.tahsis('R01', 'r01', 2.10)
        pay.bagla('r01', 'request-a')
        veri = json.loads(self.dosya.read_text())
        for kotu in ({'yukumluluk_usd': -0.1, 'kosular': ['request-a']}, {'yukumluluk_usd': float('nan'), 'kosular': ['request-a']},
                     {'iade_usd': 1.0}, 'x'):
            veri['tahsisler'][0]['kapanis'] = kotu
            self.dosya.write_text(json.dumps(veri))
            with self.subTest(kapanis=str(kotu)), self.assertRaisesRegex(ValueError, 'bozuk'):
                self.pay()


class ZincirOrtamiTests(unittest.TestCase):
    """CXR-04: only CADRE_ZINCIR_* is carried; a chain outside the allowed provider is denied in live preparation."""

    def test_only_chain_variables_are_forwarded_and_foreign_models_are_listed(self):
        env = {'CADRE_ZINCIR_PLAN': 'opencode-go/glm-5.3', 'CADRE_ZINCIR_REVIEW': 'opencode-go/glm-5.3,opencode/glm-5.3', 'CADRE_HOME': '/x',
               'CADRE_ZINCIR_BOS': '', 'OPENCODE_CONFIG_CONTENT': '{}'}
        zincir = kabul_paketi.zincir_ortami_al(env)
        self.assertEqual(sorted(zincir), ['CADRE_ZINCIR_PLAN', 'CADRE_ZINCIR_REVIEW'])
        self.assertEqual(kabul_paketi.zincir_dogrula(zincir, 'opencode-go/'), ['opencode/glm-5.3'])
        self.assertEqual(kabul_paketi.zincir_dogrula({'CADRE_ZINCIR_PLAN': 'opencode-go/glm-5.3'}, 'opencode-go/'), [])

    def test_live_preparation_installs_the_guard_hooks_the_engine_requires_before_editing(self):
        paket = object.__new__(kabul_paketi.Paket)
        paket.yol, paket.zincir = 'or', {}
        paket.dizin = Path(tempfile.mkdtemp(prefix='kabul-canli-', dir=kabul_paketi.HOME))
        for name in ('pre-push', 'pre-commit', 'commit-msg'):
            (kabul_paketi.HOME / 'hooks' / name).unlink(missing_ok=True)
        kabul_paketi.Paket.canli_hazirla(paket)
        for name in ('pre-push', 'pre-commit', 'commit-msg'):
            kanca = kabul_paketi.HOME / 'hooks' / name
            self.assertTrue(kanca.is_file() and os.access(kanca, os.X_OK), name)
            self.assertEqual(kanca.read_bytes(), (kabul_paketi.ENGINE / 'hooks' / name).read_bytes())

    def test_live_receipt_gates_first_reconcile_cut_stream_reservations_through_the_engine(self):
        paket = object.__new__(kabul_paketi.Paket)
        paket.root, paket.sarmal, paket.env_ek, paket.zincir, paket.olaylar = Path('/motor'), Path('/sarmal'), {}, {}, []
        with patch.object(kabul_paketi.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '2\n', '')) as calistir:
            paket.yol = 'sahte'
            paket.askida_kapat()
            self.assertFalse(calistir.called, 'sahte yolda saglayici mutabakati yok')
            paket.yol = 'or'
            paket.askida_kapat()
        komut = calistir.call_args.args[0]
        self.assertIn('or_askida_kapat', komut[2])
        self.assertEqual(komut[3], '/motor/lib')
        self.assertIn('2 satir', paket.olaylar[-1])

    def test_operator_chain_overrides_the_live_default_chain_for_the_same_roles(self):
        import canli_senaryo
        paket = object.__new__(kabul_paketi.Paket)
        paket.root, paket.sarmal = Path('/motor'), Path('/sarmal')
        paket.env_ek = dict(canli_senaryo.zincir_ortami('or', 'ucuz'))
        paket.zincir = {'CADRE_ZINCIR_IMPL': 'openrouter/deepseek/deepseek-v4-pro,openrouter/z-ai/glm-5.3',
                        'CADRE_ZINCIR_REVIEW': 'openrouter/z-ai/glm-5.3,openrouter/deepseek/deepseek-v4-pro'}
        env = paket.ortam()
        for rol in ('CADRE_ZINCIR_IMPL', 'CADRE_ZINCIR_REVIEW'):
            self.assertEqual(env[rol], paket.zincir[rol], 'operator zinciri ayni roldeki varsayilan zinciri ezer')
        self.assertEqual(env['CADRE_ZINCIR_PLAN'], paket.env_ek['CADRE_ZINCIR_PLAN'], 'operatorun vermedigi rol varsayilanda kalir')
        self.assertEqual((env['CADRE_REAL_OPENCODE'], env['CADRE_ENGINE_ROOT']), ('/sarmal', '/motor'))


DURUS_KOSUMU = '''import os, shutil, sys
from pathlib import Path
KOK, KIP = Path(sys.argv[1]), sys.argv[2]
sys.path.insert(0, {testler!r})
import kabul_paketi as kp
YAVAS = """#!{{py}}
import os, sys, time
if '[QE-GOREV v1]' in (sys.argv[-1] if len(sys.argv) > 1 else ''):
    open({{pidf!r}}, 'w').write(str(os.getpid()))
    time.sleep(300)
    raise SystemExit(0)
os.execv({{py!r}}, [{{py!r}}, {{gercek!r}}, *sys.argv[1:]])
"""
hazirla = kp.Paket.sahte_hazirla
def yavas_hazirla(self):
    hazirla(self)
    gercek = kp.HOME / 'kabul-sahte-gercek.py'
    shutil.copyfile(self.sarmal, gercek)
    self.sarmal.write_text(YAVAS.format(py=sys.executable, pidf=str(KOK / 'isci.pid'), gercek=str(gercek)))
kp.Paket.sahte_hazirla = yavas_hazirla
if KIP == 'sure':
    sure = kp.Paket.sure_kaldi
    kp.Paket.sure_kaldi = lambda self: 0.0 if (KOK / 'isci.pid').exists() else sure(self)
sys.argv = ['kabul_paketi.py', 'R01', '--fixture-dir', str(KOK / 'fixtures'), '--yol', 'sahte', '--dakika', '5', '--tavan', '2',
            '--cikti', str(KOK / 'kanit')]
raise SystemExit(kp.main())
'''


class DurusYoluTests(unittest.TestCase):
    def kok_hazirla(self, ad: str) -> Path:
        temp = tempfile.TemporaryDirectory(prefix=f'kabul-durus-{ad}-')
        self.addCleanup(temp.cleanup)
        kok = Path(temp.name).resolve()
        fixture_agaci(kok)
        self.addCleanup(self.surecleri_topla, kok)
        return kok

    @staticmethod
    def kalan_surecler(kok: Path) -> list[tuple[int, str]]:
        cikti = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True, timeout=30).stdout
        sonuc = []
        for satir in cikti.splitlines():
            pid, _, komut = satir.strip().partition(' ')
            if str(kok) in komut and int(pid) != os.getpid():
                sonuc.append((int(pid), komut))
        return sonuc

    def surecleri_topla(self, kok: Path) -> None:
        for pid, _ in self.kalan_surecler(kok):
            try:
                os.kill(pid, 9)
            except OSError:
                pass

    def kos(self, kok: Path, kip: str, sinyal: int | None) -> tuple[int, dict | None, dict]:
        harness = kok / 'kosum.py'
        harness.write_text(DURUS_KOSUMU.format(testler=str(TESTS)))
        env = {k: v for k, v in os.environ.items() if not k.startswith(('CADRE_', 'OPENCODE_', 'E2E_'))}
        env.update(CADRE_HOME=str(kok / 'ev'), CADRE_ENGINE_ROOT=str(TESTS.parent))
        surucu = subprocess.Popen([sys.executable, str(harness), str(kok), kip], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: surucu.poll() is None and surucu.kill())
        son = time.monotonic() + 120
        while not (kok / 'isci.pid').exists() and surucu.poll() is None and time.monotonic() < son:
            time.sleep(0.2)
        self.assertTrue((kok / 'isci.pid').exists(), 'yavas isci baslamadi')
        if sinyal is not None:
            surucu.send_signal(sinyal)
        try:
            cikti, _ = surucu.communicate(timeout=240)
        except subprocess.TimeoutExpired:
            surucu.kill()
            cikti, _ = surucu.communicate()
            self.fail('surucu durmadi: ' + cikti[-1500:])
        ozet = json.loads((kok / 'kanit/ozet.json').read_text()) if (kok / 'kanit/ozet.json').exists() else None
        kosular = sorted((kok / 'ev/runs').glob('request-*/run.json'))
        kayit = json.loads(kosular[0].read_text()) if kosular else {}
        isci = int((kok / 'isci.pid').read_text())
        son = time.monotonic() + 15
        while (self.kalan_surecler(kok) or self.canli(isci)) and time.monotonic() < son:
            time.sleep(0.2)
        self.assertFalse(self.canli(isci), 'yurutucu (isci) sahipsiz kaldi: ' + cikti[-1500:])
        self.assertEqual(self.kalan_surecler(kok), [], 'surucu bittikten sonra motor sureci kaldi')
        return surucu.returncode, ozet, kayit

    @staticmethod
    def canli(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def test_operator_interrupt_cancels_the_run_through_the_engine_and_leaves_no_process(self):
        for sinyal in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(sinyal=sinyal.name):
                kok = self.kok_hazirla(sinyal.name.lower())
                rc, ozet, kayit = self.kos(kok, 'kesme', sinyal)
                self.assertEqual(rc, 130)
                self.assertIsNotNone(ozet, 'kesmede de ozet yazilir')
                self.assertEqual(ozet['karar'], 'DURDURULDU')
                self.assertEqual(kayit.get('state'), 'cancelled')
                self.assertIn('cancel', {m.get('kind') for m in kayit.get('operator_interventions') or []})

    def test_unresponsive_coordinator_is_killed_and_its_recorded_phase_process_is_reclaimed(self):
        kok = self.kok_hazirla('sagir')
        ev, rid = kok / 'ev', 'request-sagir'
        (ev / 'runs' / rid).mkdir(parents=True)
        simdi = time.time()
        (ev / 'runs' / rid / 'run.json').write_text(json.dumps({'version': 1, 'run_id': rid, 'project': 'kabul-sagir', 'state': 'running',
                                                               'tasks': {}, 'created': simdi, 'updated': simdi}))
        (kok / 'root/lib').mkdir(parents=True)
        (kok / 'root/lib/kosu.py').write_text(f'''import json, os, signal, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, {str(TESTS.parent / 'lib')!r})
from kosu import process_identity
komut, rid = sys.argv[1], sys.argv[2]
kosu = Path(os.environ['CADRE_HOME']) / 'runs' / rid
if komut == 'run':
    for sinyal in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sinyal, signal.SIG_IGN)
    faz = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)', str(kosu)], start_new_session=True)
    while process_identity(faz.pid) is None:
        time.sleep(0.05)
    kayit = json.loads((kosu / 'run.json').read_text())
    kayit.update(phase_process=process_identity(faz.pid), owner_pid=os.getpid())
    (kosu / 'run.json').write_text(json.dumps(kayit))
    (kosu / 'faz.pid').write_text(str(faz.pid))
    time.sleep(300)
elif komut == 'cancel':
    (kosu / 'cancel.flag').write_text('requested\\n')
    print(rid)
''')
        p = kabul_paketi.Paket.__new__(kabul_paketi.Paket)
        p.dakika, p.root, p.dizin, p.olaylar, p.kosular = 5, kok / 'root', kok / 'kanit', [], [rid]
        p.sarmal, p.env_ek, p.zincir, p.DURUS_BEKLEME_SN = kok / 'yok', {}, {}, 1.0
        p.dizin.mkdir()
        with patch.object(kabul_paketi, 'HOME', ev):
            p.son_monotonic = time.monotonic() + 3
            p.kos(rid)
        self.assertTrue((ev / 'runs' / rid / 'cancel.flag').exists(), 'once motorun iptal arayuzu denenir')
        self.assertTrue(any('SIGKILL' in o for o in p.olaylar), p.olaylar)
        faz = int((ev / 'runs' / rid / 'faz.pid').read_text())
        son = time.monotonic() + 10
        while self.canli(faz) and time.monotonic() < son:
            time.sleep(0.1)
        self.assertFalse(self.canli(faz), 'kayitli faz sureci motorun reclaim_orphans yoluyla toplanir')

    def test_deadline_stops_the_run_through_the_engine_and_finalizes_it(self):
        kok = self.kok_hazirla('sure')
        rc, ozet, kayit = self.kos(kok, 'sure', None)
        self.assertNotEqual(rc, 0)
        self.assertIsNotNone(ozet)
        self.assertNotIn(ozet['karar'], ('PASS', 'LOCAL_PASS'))
        self.assertEqual(kayit.get('state'), 'cancelled', 'sure dolunca kosu motorun iptal yoluyla kapanir; running kalmaz')
        self.assertIn('cancel', {m.get('kind') for m in kayit.get('operator_interventions') or []})


if __name__ == '__main__':
    unittest.main()
