#!/usr/bin/env python3
"""Scenario-aware fake `opencode` for the acceptance package (R01-R07) dry run: no network, the real engine/gates/QA/judge path runs.

Used only with `kabul_paketi.py --yol sahte`, reading `CADRE_HOME/kabul-senaryo.json` ({"senaryo": "R03", "kip": ""}).
Fake output is not a live provider acceptance; it proves the driver and the gates work locally. Negative modes (the kip field)
trigger the existing rejection paths with the same driver: "yazar-kaynak-yazar" (the review writer writes to the source), "hakem-ret" (the final judge says DUZELT).
"""
from __future__ import annotations

import csv
import decimal
import json
import os
import pathlib
import re
import sys
import uuid

HOME = pathlib.Path(os.environ['CADRE_HOME'])
ARGS = sys.argv[1:]
PROMPT = ARGS[-1] if ARGS else ''
GOREV = os.environ.get('CADRE_GOREV_ID', '')
if PROMPT.startswith('Kaldigin yerden devam et') and os.environ.get('CADRE_RUN_DIR') and GOREV:
    # the real CLI keeps the task in context in a recorded session; the fake re-reads the task's recorded prompt (same as test_uctan_uca)
    try:
        PROMPT = (pathlib.Path(os.environ['CADRE_RUN_DIR']) / 'tasks' / GOREV / 'prompt.md').read_text()
    except OSError:
        pass
try:
    AYAR = json.loads((HOME / 'kabul-senaryo.json').read_text())
except (OSError, ValueError):
    AYAR = {}
SENARYO, KIP = AYAR.get('senaryo', ''), AYAR.get('kip', '')
SID = ARGS[ARGS.index('--session') + 1] if '--session' in ARGS else 'ses_' + uuid.uuid4().hex[:12]

_REQ = os.environ.get('CADRE_REQUEST_DIR')
if _REQ:
    # In the real CLI cadre-plugin.mjs writes the tools-armed.json receipt while arming the tools; the process check (TAM) requires
    # this receipt. The fake CLI leaves the same receipt in the same form as test_uctan_uca.FAKE_OPENCODE (0400, single write).
    _ADIR = pathlib.Path(_REQ).parent
    _POL, _MARKER = _ADIR / 'tool-policy.json', _ADIR / 'tools-armed.json'
    if _POL.exists() and not _MARKER.exists():
        try:
            import hashlib
            _P = json.loads(_POL.read_text())
            _FD = os.open(_MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
            os.write(_FD, json.dumps({'owner': _P.get('owner'), 'pid': os.getpid(),
                                      'profile_sha256': hashlib.sha256(str(_P.get('profile', '')).encode()).hexdigest()}).encode())
            os.close(_FD)
        except (OSError, ValueError):
            pass


def kayit(rol: str, ek: dict | None = None) -> None:
    with (HOME / 'kabul-calls.jsonl').open('a') as f:
        f.write(json.dumps({'senaryo': SENARYO, 'rol': rol, 'gorev': GOREV, 'cwd': os.getcwd(), 'session': SID, **(ek or {})}) + '\n')


def olay(metin: str) -> None:
    print(json.dumps({'type': 'text', 'sessionID': SID, 'part': {'type': 'text', 'text': metin}}), flush=True)


def soru() -> dict:
    """The prompt may have several JSON lines (question + context); the line carrying kind/schema/criteria is the question."""
    secilen = None
    for satir in PROMPT.splitlines():
        if not satir.startswith('{'):
            continue
        try:
            aday = json.loads(satir)
        except ValueError:
            continue
        if isinstance(aday, dict) and ('kind' in aday or 'schema' in aday or 'criteria' in aday):
            secilen = aday
        elif secilen is None:
            secilen = aday
    if secilen is None:
        raise SystemExit('kabul_sahte: istemde JSON soru yok')
    return secilen


def gorev(gid: str, tanim: str, dosyalar: list[str], kabul: str, bagimli: list[str] | None = None, grup: int = 0) -> dict:
    return {'id': gid, 'tanim': tanim, 'dosyalar': dosyalar, 'kabul': kabul, 'bagimli': bagimli or [], 'zorluk': 2,
            'model': 'auto', 'paralel_grup': grup, 'tahmin_dk': 2, 'tahmin_usd': 0}


def kaynak_eslemesi(items: list[dict], tamam: dict[str, list[str]], dogrulanmis: set[str]) -> list[dict]:
    sonuc = []
    for item in items:
        if item['id'] in dogrulanmis:
            sonuc.append({'id': item['id'], 'source_sha256': item['sha256'], 'tasks': [], 'status': 'verified',
                          'evidence': f"{item['id']} kaynakta [x]; metin.py icinde uygulanmis, tests/test_{item['id'].lower()}.py yesil"})
        else:
            sonuc.append({'id': item['id'], 'source_sha256': item['sha256'], 'tasks': tamam.get(item['id'], ['g1']),
                          'status': 'pending', 'evidence': f"{item['id']} kaynakta [ ]; kabul komutu kirmizi"})
    return sonuc


def plan(q: dict) -> dict:
    p = dict(q['schema'])
    p['is_turu'] = (q.get('request') or {}).get('job_type', 'code')
    p['assumptions'], p['decisions'] = [], []
    items = (q.get('backlog') or {}).get('items') or []
    ids = {i['id'] for i in items}
    if SENARYO == 'R01':
        p['gorevler'] = [gorev('g1', 'calc.py add() toplama dondursun', ['calc.py'], 'python3 -m unittest -v test_calc gecer')]
        p['source_items'] = []
    elif SENARYO in ('R02', 'R03', 'R04'):
        tamam = {'B001': ['g1'], 'B002': ['g1'], 'B003': ['g1'], 'B004': ['g1'], 'B005': ['g2']}
        dogrulanmis = {i['id'] for i in items if i.get('completed')}
        kalan = [b for b in ('B001', 'B002', 'B003', 'B004') if b not in dogrulanmis]
        gorevler = []
        if kalan:
            gorevler.append(gorev('g1', 'metin.py: ' + ', '.join(kalan) + ' (B003 B001e, B004 B001/B003e bagli)', ['metin.py'],
                                  ' && '.join(f'python3 -m unittest -v tests.test_{b.lower()}' for b in kalan)))
        gorevler.append(gorev('g2', 'metin_cli.py: B005 (B004e bagli)', ['metin_cli.py'], 'python3 -m unittest -v tests.test_b005',
                              bagimli=['g1'] if kalan else [], grup=1 if kalan else 0))
        p['gorevler'] = gorevler
        p['source_items'] = kaynak_eslemesi(items, tamam, dogrulanmis) if ids else []
        if not ids:
            p['gorevler'][-1]['tanim'] += '; kaynak kimlikleri B001-B005 BACKLOG.md'
    elif SENARYO == 'R05':
        p['gorevler'] = [gorev('g1', 'uc dosyada isci ayari aktarimi ve sayac anlamini salt-okur incele',
                               ['engine/tests/regresyon.py', 'engine/tests/paralel_kos.py', 'engine/USAGE.md'], 'dosya/satir kanitli rapor')]
        p['source_items'] = []
    elif SENARYO == 'R06':
        p['gorevler'] = [gorev('g1', 'harcama_ozeti CLI: kategori,tutar CSV -> iki ondalikli JSON (Decimal)',
                               ['src/harcama_ozeti/ozet.py', 'src/harcama_ozeti/__main__.py', 'tests/test_harcama_ozeti.py'],
                               'make test gecer; valid.csv market 20.00 ulasim 4.50 toplam 24.50; gecersiz girdi nonzero')]
        p['source_items'] = [{'id': i['id'], 'source_sha256': i['sha256'], 'tasks': ['g1'], 'status': 'pending',
                              'evidence': 'BACKLOG maddesi acik; src altinda CLI yok'} for i in items]
    elif SENARYO == 'R07':
        p['gorevler'] = [gorev('g1', 'orders.csv operasyon raporu: kaynak/hesap/yorum ayri, iddia basina kanit',
                               ['RAPOR.md', 'RAPOR.kanit.jsonl'], 'rapor QA ve hakem yoklamasi gecer')]
        p['source_items'] = []
    else:
        raise SystemExit('kabul_sahte: bilinmeyen senaryo ' + repr(SENARYO))
    if q.get('change'):
        p['gorevler'][0]['tanim'] = q['change']
    return p


METIN_TAM = '''def kelimeler(metin):
    return metin.split()

def kelime_say(metin):
    return len(kelimeler(metin))

def ters_cevir(metin):
    return " ".join(reversed(kelimeler(metin)))

def en_uzun_kelime(metin):
    kelimeler_ = kelimeler(metin)
    if not kelimeler_:
        return None
    return max(kelimeler_, key=len)

def ozet(metin):
    return {"kelime": kelime_say(metin), "en_uzun": en_uzun_kelime(metin)}
'''
METIN_CLI = '''import json
import sys

from metin import ozet

if __name__ == "__main__":
    print(json.dumps(ozet(sys.argv[1]), ensure_ascii=False))
'''
OZET_PY = '''"""harcama_ozeti: kategori,tutar CSV'sinden kategori ve genel toplam (Decimal, iki ondalik)."""
import csv
import json
from decimal import Decimal, InvalidOperation


class GirdiHatasi(ValueError):
    pass


def ozetle(yol):
    toplamlar = {}
    genel = Decimal("0")
    with open(yol, newline="", encoding="utf-8") as f:
        okuyucu = csv.DictReader(f)
        if okuyucu.fieldnames is None or "kategori" not in okuyucu.fieldnames or "tutar" not in okuyucu.fieldnames:
            raise GirdiHatasi("beklenen sutunlar: kategori,tutar")
        for no, satir in enumerate(okuyucu, 2):
            kategori = (satir.get("kategori") or "").strip()
            ham = (satir.get("tutar") or "").strip()
            if not kategori:
                raise GirdiHatasi(f"satir {no}: kategori bos")
            try:
                tutar = Decimal(ham)
            except InvalidOperation:
                raise GirdiHatasi(f"satir {no}: gecersiz tutar {ham!r}") from None
            if not tutar.is_finite():
                raise GirdiHatasi(f"satir {no}: gecersiz tutar {ham!r}")
            toplamlar[kategori] = toplamlar.get(kategori, Decimal("0")) + tutar
            genel += tutar
    sonuc = {k: format(v.quantize(Decimal("0.01")), "f") for k, v in sorted(toplamlar.items())}
    sonuc["toplam"] = format(genel.quantize(Decimal("0.01")), "f")
    return sonuc


def ozet_json(yol):
    return json.dumps(ozetle(yol), ensure_ascii=False)
'''
MAIN_PY = '''import sys

from harcama_ozeti.ozet import GirdiHatasi, ozet_json


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("kullanim: python3 -m harcama_ozeti <CSV>", file=sys.stderr)
        return 2
    try:
        cikti = ozet_json(argv[0])
    except (GirdiHatasi, OSError) as exc:
        print(f"hata: {exc}", file=sys.stderr)
        return 1
    print(cikti)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''
TEST_OZET = '''import json
import os
import subprocess
import sys
import tempfile
import unittest

from harcama_ozeti.ozet import GirdiHatasi, ozetle


class HarcamaOzetiTests(unittest.TestCase):
    def dosya(self, icerik):
        f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
        f.write(icerik)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_gecerli_girdi(self):
        yol = self.dosya("kategori,tutar\\nmarket,12.30\\nulasim,4.50\\nmarket,7.70\\n")
        self.assertEqual(ozetle(yol), {"market": "20.00", "ulasim": "4.50", "toplam": "24.50"})

    def test_gecersiz_tutar_ve_eksik_sutun(self):
        for icerik in ("kategori,tutar\\nmarket,yanlis\\n", "kategori\\nmarket\\n"):
            with self.assertRaises(GirdiHatasi):
                ozetle(self.dosya(icerik))

    def test_cli_nonzero_ve_kismi_cikti_yok(self):
        yol = self.dosya("kategori,tutar\\nmarket,yanlis\\n")
        env = {**os.environ, "PYTHONPATH": "src"}
        r = subprocess.run([sys.executable, "-m", "harcama_ozeti", yol], capture_output=True, text=True, env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        iyi = self.dosya("kategori,tutar\\nmarket,1.005\\n")
        r = subprocess.run([sys.executable, "-m", "harcama_ozeti", iyi], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout), {"market": "1.00", "toplam": "1.00"})
'''


def satir_no(yol: str, parca: str) -> tuple[int, str]:
    for no, satir in enumerate(pathlib.Path(yol).read_text().splitlines(), 1):
        if parca in satir:
            return no, satir.strip()
    raise SystemExit(f'kabul_sahte: {yol} icinde {parca!r} bulunamadi')


def rapor_urunu() -> None:
    satirlar = pathlib.Path('orders.csv').read_text().splitlines()
    toplam = {'completed': decimal.Decimal(0), 'cancelled': decimal.Decimal(0), 'all': decimal.Decimal(0)}
    with open('orders.csv', newline='') as f:
        for r in csv.DictReader(f):
            toplam[r['status']] += decimal.Decimal(r['amount'])
            toplam['all'] += decimal.Decimal(r['amount'])
    komut = ("python3 -c \"import csv,decimal;print(sum(decimal.Decimal(r['amount']) for r in csv.DictReader(open('orders.csv'))"
             " if {kosul}))\"")
    kaynak_no, kaynak_satir = satir_no('metadata.json', 'source_date')
    celiski_satir = pathlib.Path('contradictory-summary.txt').read_text().splitlines()[0].strip()
    kanit = [
        {'id': 'K1', 'iddia': 'o1 completed 12.30', 'tur': 'dosya', 'kaynak': 'orders.csv:2', 'beklenen': satirlar[1]},
        {'id': 'K2', 'iddia': 'o2 cancelled 4.50', 'tur': 'dosya', 'kaynak': 'orders.csv:3', 'beklenen': satirlar[2]},
        {'id': 'K3', 'iddia': 'o3 completed 7.70', 'tur': 'dosya', 'kaynak': 'orders.csv:4', 'beklenen': satirlar[3]},
        {'id': 'K4', 'iddia': 'completed toplami', 'tur': 'komut', 'kaynak': komut.format(kosul="r['status']=='completed'"), 'beklenen': '20.00'},
        {'id': 'K5', 'iddia': 'cancelled toplami', 'tur': 'komut', 'kaynak': komut.format(kosul="r['status']=='cancelled'"), 'beklenen': '4.50'},
        {'id': 'K6', 'iddia': 'butun satirlarin toplami', 'tur': 'komut', 'kaynak': komut.format(kosul='True'), 'beklenen': '24.50'},
        {'id': 'K7', 'iddia': 'kaynak tarihi', 'tur': 'dosya', 'kaynak': f'metadata.json:{kaynak_no}', 'beklenen': kaynak_satir},
        {'id': 'K8', 'iddia': 'celiskili ozet iddiasi', 'tur': 'dosya', 'kaynak': 'contradictory-summary.txt:1', 'beklenen': celiski_satir},
    ]
    rapor = (
        '# Kaynak\n'
        f'orders.csv {len(satirlar) - 1} siparis satiri icerir [K1..K3]; kaynak tarihi metadata.json icinde 2026-10-14 olarak kayitli [K7].\n\n'
        '# Hesap\n'
        f"completed toplami {toplam['completed']:.2f} [K4]; cancelled toplami {toplam['cancelled']:.2f} [K5]; "
        f"butun satirlarin toplami {toplam['all']:.2f} [K6].\n\n"
        '# Yorum\n'
        'contradictory-summary.txt completed toplamini 21.00 ilan eder [K8]; bu iddia kaynak satirlariyla uyusmaz, dogrulanmadi; '
        'celiski cozulmeden kabul verilmez. Hesaplar yalniz orders.csv satirlarina dayanir [K1..K6].\n\n'
        'Veri tarihi: 2026-10-14\n')
    pathlib.Path('RAPOR.md').write_text(rapor)
    pathlib.Path('RAPOR.kanit.jsonl').write_text(''.join(json.dumps(k, ensure_ascii=False) + '\n' for k in kanit))


def yaz(yol: str, icerik: str) -> None:
    pathlib.Path(yol).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(yol).write_text(icerik)


def isci() -> None:
    dosyalar = re.search(r'DOSYALAR \(SADECE bunlara dokun\): (.*)', PROMPT).group(1).split(', ')
    kayit('worker', {'dosyalar': dosyalar})
    if SENARYO == 'R01':
        pathlib.Path('calc.py').write_text('def add(left, right):\n    return left + right\n')
    elif SENARYO in ('R02', 'R03'):
        if 'metin.py' in dosyalar:
            pathlib.Path('metin.py').write_text(METIN_TAM)
        if 'metin_cli.py' in dosyalar:
            pathlib.Path('metin_cli.py').write_text(METIN_CLI)
    elif SENARYO == 'R06':
        yaz('src/harcama_ozeti/ozet.py', OZET_PY)
        yaz('src/harcama_ozeti/__main__.py', MAIN_PY)
        yaz('tests/test_harcama_ozeti.py', TEST_OZET)
    elif SENARYO == 'R07':
        rapor_urunu()
    else:
        raise SystemExit('kabul_sahte: bu senaryoda isci beklenmiyor')
    olay('yazildi\nNE DEGISTI: canned urun\nNASIL DOGRULANDI: kabul komutu\nRISK+NEDEN: sahte isci')


def inceleme_yazari(q: dict) -> dict:
    no1, s1 = satir_no('engine/tests/regresyon.py', "if not k.startswith(('CADRE_', 'OPENCODE_'")
    no2, s2 = satir_no('engine/tests/regresyon.py', "CADRE_TEST_PYTEST_PYTHON'] = os.environ")
    no3, s3 = satir_no('engine/tests/paralel_kos.py', 'CADRE_TEST_ISCI')
    no4, s4 = satir_no('engine/USAGE.md', 'CADRE_TEST_ARSIVI')
    rapor = ('# Bulgular\n'
             f'regresyon.py alt surec ortamindan CADRE_ ile baslayan butun degiskenleri siliyor [K1]; geri konan beyaz listede yalniz '
             f'CADRE_TEST_PYTEST_PYTHON var, CADRE_TEST_ISCI yok [K2]. paralel_kos.py isci sayisini CADRE_TEST_ISCI ortamindan okuyor [K3]; '
             'dolayisiyla ortak kapidan verilen isci ayari alt surece ulasmaz ve varsayilan CPU sayisi kullanilir (kusur). '
             'USAGE.md yalniz CADRE_TEST_PYTEST_PYTHON ve CADRE_TEST_ARSIVI degiskenlerini anlatir, isci ayarini anlatmaz [K4] (belge boslugu).\n'
             '# Belirsizlikler\n'
             'Zamanlama hatalarinin yalniz bu kusurdan kaynaklandigi bu incelemeyle kanitlanamaz; yuk etkisi olasi, dogrulanmadi.\n'
             '# Yontem\n'
             'Uc dosya salt-okur okundu; satirlar dosya kanitiyla baglandi; test calistirilmadi, kaynak degistirilmedi [K1..K4].\n')
    if KIP == 'yazar-kaynak-yazar':
        pathlib.Path('yasak.txt').write_text('inceleme kaynaga yazdi')
    return {'report': rapor, 'assumptions': [], 'decisions': [],
            'evidence': [{'id': 'K1', 'iddia': 'CADRE_ ortam suzgeci', 'tur': 'dosya', 'kaynak': f'engine/tests/regresyon.py:{no1}', 'beklenen': s1},
                         {'id': 'K2', 'iddia': 'beyaz liste yalniz pytest python', 'tur': 'dosya', 'kaynak': f'engine/tests/regresyon.py:{no2}', 'beklenen': s2},
                         {'id': 'K3', 'iddia': 'isci sayisi ortamdan', 'tur': 'dosya', 'kaynak': f'engine/tests/paralel_kos.py:{no3}', 'beklenen': s3},
                         {'id': 'K4', 'iddia': 'belge isci ayarini anlatmiyor', 'tur': 'dosya', 'kaynak': f'engine/USAGE.md:{no4}', 'beklenen': s4}]}


def hakem(q: dict, kanit_idleri: list[str] | None = None) -> dict:
    cevap = {'verdict': 'GECER', 'revision': q['revision'], 'decisions': [],
             'acceptance': [{'id': c['id'], 'status': 'PASS', 'evidence': 'ozgun istek, plan, urun ve kaynak ayri oturumda okundu'}
                            for c in q['criteria']]}
    r = q.get('rapor') or {}
    ids = kanit_idleri if kanit_idleri is not None else (r.get('kanit_idleri') or [])
    en_az = r.get('en_az') or min(5, len(ids))
    if ids:
        cevap['yoklama'] = [{'id': k, 'sonuc': 'dogrulandi', 'kanit': k + ' kaynagindan ayrica okundu'} for k in ids[:max(en_az, min(5, len(ids)))]]
    if KIP == 'hakem-ret':
        cevap['verdict'] = 'DUZELT'
        cevap['acceptance'][0]['status'] = 'FAIL'
    return cevap


def main() -> int:
    print(json.dumps({'type': 'step_start', 'sessionID': SID, 'part': {'type': 'step-start'}}), flush=True)
    if PROMPT.startswith('Kaynak dosyalarini oku;'):
        q = soru()
        kayit('plan')
        olay(json.dumps(plan(q), ensure_ascii=False))
    elif PROMPT.startswith('PLAN ISTEK UYUMU'):
        q = soru()
        kayit('plan-review')
        olay(json.dumps(hakem(q), ensure_ascii=False))
    elif PROMPT.startswith('YALNIZ INCELEME'):
        q = soru()
        kayit('review-author')
        olay(json.dumps(inceleme_yazari(q), ensure_ascii=False))
    elif PROMPT.startswith('AYRI TEMIZ SALT-OKUR SON INCELEME'):
        q = soru()
        kayit('review-final')
        olay(json.dumps(hakem(q, [e['id'] for e in q.get('evidence') or []]), ensure_ascii=False))
    elif '[QE-GOREV v1]' in PROMPT:
        isci()
    elif 'QA KAPISI KIRMIZI' in PROMPT:
        kayit('repair')
        olay('duzeltme denendi (sahte isci dokunmaz)')
    elif 'KALITE VE KABUL KONTROLU' in PROMPT:
        q = soru()
        kayit('review')
        olay(json.dumps(hakem(q), ensure_ascii=False))
    else:
        kayit('unknown', {'prompt_bas': PROMPT[:80]})
        olay('bilinmeyen rol')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
