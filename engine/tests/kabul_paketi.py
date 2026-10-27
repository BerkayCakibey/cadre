#!/usr/bin/env python3
"""Small acceptance package driver (scenarios R01-R07): frozen fixture, typed request path, the real engine/gates.

Usage: CADRE_HOME=<isolated root> CADRE_ENGINE_ROOT=<source engine/> \\
          python3 engine/tests/kabul_paketi.py R03 --fixture-dir <campaign>/fixtures [--yol sahte|go|or] [--dakika 20] [--tavan 0.15]
`--yol sahte` is a dry run: kabul_sahte.py writes a canned product per scenario, the real gates run with a fake budget/catalogue; it is not a live acceptance.
`--yol go|or` uses the same single-provider wrapper as canli_senaryo.py and is subject to the existing strict permission rules (no permission is produced here).
Product acceptance commands run in the engine's evidence sandbox (CXR-01); the whole package's shared cap is protected by the `--paket-tavan` ledger (CXR-02);
live receipt gates separate readability/scope/close/two-ledger states (CXR-03); CADRE_ZINCIR_<ROL> is carried to the subprocess (CXR-04).
Each scenario: the normal path + the related rejection/limit checks (the driver tests locally or binds to an existing test family); the summary
`<output>/ozet.json` and `<output>/../kabul-paketi.jsonl`. The R id is not passed to canli_senaryo.py.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time

TESTS = Path(__file__).resolve().parent
ENGINE = TESTS.parent
sys.path.insert(0, str(ENGINE / 'lib'))
sys.path.insert(0, str(TESTS))
HOME = Path(os.environ['CADRE_HOME']).resolve() if os.environ.get('CADRE_HOME') else None
ROOT = Path(os.environ.get('CADRE_ENGINE_ROOT') or ENGINE).resolve()
PY = sys.executable
VARSAYILAN_EV = (Path.home() / '.local/share/cadre-engineering').resolve()
KIMLIKLER = ('R01', 'R02', 'R03', 'R04', 'R05', 'R06', 'R07')
GIT_KIMLIK = {'GIT_AUTHOR_NAME': 'cadre-kabul', 'GIT_AUTHOR_EMAIL': 'kabul@cadre.invalid',
              'GIT_COMMITTER_NAME': 'cadre-kabul', 'GIT_COMMITTER_EMAIL': 'kabul@cadre.invalid'}
R06_BACKLOG = ('# Hazir backlog\n\n## [ ] H001 — harcama_ozeti CLI: kategori,tutar CSV -> kategori ve toplam (iki ondalik, Decimal)\n'
               'Bağımlılık: yok\nKabul: make test; PYTHONPATH=src python3 -m harcama_ozeti <CSV>\n')


def sha(yol: Path) -> str:
    return hashlib.sha256(yol.read_bytes()).hexdigest()


def git(repo: Path, *args: str) -> str:
    return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=cadre-kabul', '-c', 'user.email=kabul@cadre.invalid', *args],
                          capture_output=True, text=True, check=True, timeout=60).stdout.strip()


def agac_hashleri(kok: Path, haric: tuple[str, ...] = ('.git', '__pycache__', '.venv')) -> dict[str, str]:
    sonuc = {}
    for yol in sorted(kok.rglob('*')):
        if yol.is_file() and not any(p in haric for p in yol.relative_to(kok).parts):
            sonuc[str(yol.relative_to(kok))] = sha(yol)
    return sonuc


def kontrol(liste: list[dict], kimlik: str, tur: str, beklenen: str, gozlenen, gecti: bool | None,
            yontem: str = 'surucu', kanit: str = '') -> bool:
    liste.append({'id': kimlik, 'tur': tur, 'yontem': yontem, 'beklenen': beklenen, 'gozlenen': gozlenen,
                  'sonuc': 'PASS' if gecti else 'BLOCKED' if gecti is None else 'FAIL', 'kanit': kanit})
    return bool(gecti)


def bagli(liste: list[dict], kimlik: str, tur: str, beklenen: str, testler: list[str]) -> None:
    """Binds negative/limit behaviour to an existing free test family; the result is read from the shared local gate (no separate model run)."""
    liste.append({'id': kimlik, 'tur': tur, 'yontem': 'test', 'beklenen': beklenen, 'testler': testler,
                  'sonuc': 'GATE', 'kanit': 'engine/tests/regression.sh arsivi'})


def guvenli_komut(komut: str, wt: Path, gecici: Path, sure: float = 300) -> tuple[int | None, str]:
    """CXR-01: every acceptance command that runs the model's product goes through the engine's evidence sandbox (rapor.komut_kos): no network, writes only to the
    worktree + a temporary directory, sensitive directories/files are not read, the environment is an allow-list (the parent's CADRE_*/secret variables are not visible), the process
    group is reaped. CXR2-01: writable areas go through the engine's FIX-25 preflight (`oturum.urun_hardlink_var`) before the command;
    if a pre-existing external inode alias exists the command never runs. rc None = could not run/timeout/denied; the caller does not count it as success."""
    import oturum
    import rapor
    gecici = Path(gecici)
    gecici.mkdir(parents=True, exist_ok=True)
    for alan in (Path(wt), gecici):
        if oturum.urun_hardlink_var(alan):
            return None, f'hardlink-untrusted: yazilabilir alanda hardlink/dis inode aliasi var ({alan}); komut kosulmadi (FIX-25 preflight)'
    return rapor.komut_kos(komut, str(wt), str(gecici), sure)


def json_satiri(cikti: str) -> dict | None:
    """The last JSON object line in the combined stdout/stderr; None if none (partial/broken output does not count as JSON)."""
    for satir in reversed(cikti.splitlines()):
        satir = satir.strip()
        if satir.startswith('{'):
            try:
                veri = json.loads(satir)
            except ValueError:
                continue
            if isinstance(veri, dict):
                return veri
    return None


def zincir_ortami_al(env: dict[str, str]) -> dict[str, str]:
    """CXR-04: CADRE_ZINCIR_<ROL> chains given by the operator are carried to the subprocess as is (no other CADRE_* is carried)."""
    return {k: v for k, v in env.items() if k.startswith('CADRE_ZINCIR_') and v}


def zincir_dogrula(zincir: dict[str, str], onek: str) -> list[str]:
    """Lists models outside the allowed provider; an empty list = suitable. Does not silently enable another provider."""
    return sorted({m.strip() for v in zincir.values() for m in v.split(',') if m.strip() and not m.strip().startswith(onek)})


class PaketPayi:
    """CXR-02: the shared cap of the whole small package lives in one ledger (kabul-paketi-pay.json). Allocation BEFORE every request; a retry, a new process
    or a second variant does not reset the remaining share; an allocation exceeding the total cap is denied before the request is recorded.
    CXR2-02: every allocation/binding re-reads the ledger under the engine's file lock (kosu.FileLock), verifies the cap and the total,
    writes atomically; two overlapping objects/processes cannot allocate with an old memory snapshot, the first line is never lost."""

    def __init__(self, dosya: Path, tavan_usd: float) -> None:
        if not math.isfinite(tavan_usd) or tavan_usd < 0:
            raise ValueError('paket tavani negatif olmayan sonlu USD olmali')
        self.dosya = Path(dosya)
        self.tavan_usd = round(tavan_usd, 6)
        with self.kilit():
            self.veri = self.yukle()
            if self.veri is None:
                self.veri = {'paket_tavan_usd': self.tavan_usd, 'tahsisler': []}
                self.kaydet()
            self.dogrula()

    def kilit(self):
        from kosu import FileLock
        self.dosya.parent.mkdir(parents=True, exist_ok=True)
        return FileLock(self.dosya.with_suffix('.lock'))

    def dogrula(self) -> None:
        if abs(float(self.veri['paket_tavan_usd']) - self.tavan_usd) > 1e-9:
            raise ValueError(f"paket tavani defterle uyusmuyor: defter {self.veri['paket_tavan_usd']}, verilen {self.tavan_usd}")

    def yukle(self) -> dict | None:
        try:
            veri = json.loads(self.dosya.read_text())
        except FileNotFoundError:
            return None
        if (not isinstance(veri, dict) or not isinstance(veri.get('tahsisler'), list) or not math.isfinite(float(veri.get('paket_tavan_usd', 'nan')))
                or any(not isinstance(t, dict) or not math.isfinite(float(t.get('usd', 'nan'))) or float(t['usd']) <= 0 for t in veri['tahsisler'])
                or any('kapanis' in t and not self.kapanis_gecerli(t['kapanis']) for t in veri['tahsisler'])):
            raise ValueError('paket pay defteri bozuk')
        return veri

    @staticmethod
    def kapanis_gecerli(kapanis) -> bool:
        if not isinstance(kapanis, dict) or not isinstance(kapanis.get('kosular'), list) or not all(isinstance(k, str) for k in kapanis['kosular']):
            return False
        try:
            yukumluluk = float(kapanis['yukumluluk_usd'])
        except (KeyError, TypeError, ValueError):
            return False
        return math.isfinite(yukumluluk) and yukumluluk >= 0

    @staticmethod
    def etkin(t: dict) -> float:
        return float(t['kapanis']['yukumluluk_usd']) if 'kapanis' in t else float(t['usd'])

    def tahsis_edilen(self) -> float:
        return round(sum(self.etkin(t) for t in self.veri['tahsisler']), 6)

    def brut(self) -> float:
        return round(sum(float(t['usd']) for t in self.veri['tahsisler']), 6)

    def iade_edilen(self) -> float:
        return round(sum(max(0.0, float(t['usd']) - self.etkin(t)) for t in self.veri['tahsisler'] if 'kapanis' in t), 6)

    def kalan(self) -> float:
        return round(float(self.veri['paket_tavan_usd']) - self.tahsis_edilen(), 6)

    def tahsis(self, senaryo: str, etiket: str, istenen_usd: float) -> float:
        if not math.isfinite(istenen_usd) or istenen_usd <= 0:
            raise ValueError('tahsis pozitif sonlu USD olmali')
        istenen_usd = round(istenen_usd, 6)
        with self.kilit():
            self.veri = self.yukle() or {'paket_tavan_usd': self.tavan_usd, 'tahsisler': []}   # fresh snapshot under the lock
            self.dogrula()
            if istenen_usd > self.kalan() + 1e-9:
                raise RuntimeError(f'ortak paket payi yetersiz: istenen {istenen_usd:.6f}, kalan {self.kalan():.6f} '
                                   f"(tavan {float(self.veri['paket_tavan_usd']):.6f}, tahsis {self.tahsis_edilen():.6f}); istek kaydedilmedi")
            self.veri['tahsisler'].append({'senaryo': senaryo, 'etiket': etiket, 'usd': istenen_usd, 'rid': None, 'ts': time.time()})
            self.kaydet()
        return istenen_usd

    def bagla(self, etiket: str, rid: str) -> None:
        with self.kilit():
            self.veri = self.yukle() or {'paket_tavan_usd': self.tavan_usd, 'tahsisler': []}
            self.dogrula()
            for t in self.veri['tahsisler']:
                if t['etiket'] == etiket and t['rid'] is None:
                    t['rid'] = rid
            self.kaydet()

    def iade_denetimi(self, home: Path, onek: str | None) -> list[dict]:
        olaylar = []
        with self.kilit():
            self.veri = self.yukle() or {'paket_tavan_usd': self.tavan_usd, 'tahsisler': []}
            self.dogrula()
            for t in self.veri['tahsisler']:
                olay = {'etiket': t['etiket'], 'rid': t.get('rid'), 'usd': float(t['usd'])}
                if not t.get('rid'):
                    olaylar.append({**olay, 'sonuc': 'bekliyor', 'neden': 'kosu kimligi yok (istek baglanmadi)'})
                    continue
                kanit = kapanis_kaniti(home, t['rid'], onek)
                kapanis = t.get('kapanis')
                if kapanis is None:
                    if not kanit['kapanabilir']:
                        olaylar.append({**olay, 'sonuc': 'bekliyor', 'neden': kanit['neden']})
                        continue
                    yukumluluk = kanit['yukumluluk_usd']
                    asim = yukumluluk > float(t['usd']) + 1e-9
                    t['kapanis'] = {'yukumluluk_usd': yukumluluk, 'iade_usd': 0.0 if asim else round(float(t['usd']) - yukumluluk, 6),
                                    'kosular': kanit['kosular'], 'makbuz': kanit['makbuz'], 'ts': time.time(), **({'asim': True} if asim else {})}
                    olaylar.append({**olay, 'sonuc': 'asim' if asim else 'iade', 'yukumluluk_usd': yukumluluk, 'neden': kanit['neden']})
                    continue
                onceki = float(kapanis['yukumluluk_usd'])
                if not kanit['kapanabilir'] or kanit['kosular'] != kapanis['kosular']:
                    hedef = max(onceki, float(t['usd']))
                    if hedef > onceki + 1e-9:
                        kapanis.update(yukumluluk_usd=hedef, iade_usd=0.0,
                                       geri_alindi={'neden': kanit['neden'] or 'kosu kapsami degisti', 'ts': time.time()})
                        olaylar.append({**olay, 'sonuc': 'geri_alindi', 'yukumluluk_usd': hedef, 'neden': kanit['neden'] or 'kosu kapsami degisti'})
                    else:
                        olaylar.append({**olay, 'sonuc': 'kapali', 'yukumluluk_usd': onceki, 'neden': kanit['neden']})
                    continue
                if kanit['yukumluluk_usd'] > onceki + 1e-9:
                    yeni = kanit['yukumluluk_usd']
                    kapanis.update(yukumluluk_usd=yeni, iade_usd=max(0.0, round(float(t['usd']) - yeni, 6)), makbuz=kanit['makbuz'],
                                   gec_makbuz={'onceki_usd': onceki, 'ts': time.time()}, **({'asim': True} if yeni > float(t['usd']) + 1e-9 else {}))
                    olaylar.append({**olay, 'sonuc': 'gec_makbuz', 'yukumluluk_usd': yeni, 'neden': kanit['neden']})
                else:
                    olaylar.append({**olay, 'sonuc': 'kapali', 'yukumluluk_usd': onceki, 'neden': kanit['neden']})
            self.kaydet()
        return olaylar

    def kaydet(self) -> None:
        self.dosya.parent.mkdir(parents=True, exist_ok=True)
        gecici = self.dosya.with_suffix('.tmp')
        gecici.write_text(json.dumps(self.veri, ensure_ascii=False, indent=2))
        gecici.replace(self.dosya)


def r06_paylari(aile_tavani: float, a_yukumluluk_usd: float | None) -> tuple[float, float]:
    """CXR-02: R06's two variants share one family share: A = share/2; B = min(share/2, share - A's settled+pending obligation).
    If A's obligation is unknown (no receipt) A's whole cap counts as spent (conservative)."""
    a = round(aile_tavani / 2, 6)
    bilinen = a if a_yukumluluk_usd is None else max(0.0, a_yukumluluk_usd)
    b = round(min(a, aile_tavani - bilinen), 6)
    return a, max(0.0, b)


def makbuz_oku(db: Path, ids: list[str]) -> dict:
    """Reads request-budget.db rows with the same classification as canli_senaryo; 'hata' if unreadable, an empty set without rows."""
    from butce_kapisi import FREE, PAID, USD_PROVIDERS, taban_saglayici
    sonuc: dict = {'saglayicilar': {}, 'satir': 0, 'hata': None, 'aylik_idler': set(), 'oturumlar': set()}
    if not ids:
        return {**sonuc, 'hata': 'kosu kimligi yok'}
    if not db.exists():
        return {**sonuc, 'hata': 'request-budget.db yok'}
    isaret = ','.join('?' * len(ids))
    try:
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)) as con:
            satirlar = con.execute(f"select provider, reserved, actual, created, json_extract(metadata,'$.run_id'), receipt, metadata, id "
                                   f"from reservation where json_extract(metadata,'$.run_id') in ({isaret})", ids).fetchall()
    except sqlite3.Error as exc:
        return {**sonuc, 'hata': f'defter okunamadi: {type(exc).__name__}: {exc}'}
    sonuc['satir'] = len(satirlar)
    for saglayici, rezerve, gercek, baslangic, run_id, makbuz, raw, ident in satirlar:
        try:
            metadata = json.loads(raw) if raw else {}
        except ValueError:
            return {**sonuc, 'hata': f'makbuz metadatasi bozuk: {ident}'}
        if not isinstance(metadata, dict) or type(rezerve) is not int or rezerve < 0 or (gercek is not None and (type(gercek) is not int or gercek < 0)):
            return {**sonuc, 'hata': f'makbuz tutari belirsiz: {ident}'}
        for alan, kume in (('aylik_id', 'aylik_idler'), ('session_id', 'oturumlar')):
            if isinstance(metadata.get(alan), str) and metadata[alan]:
                sonuc[kume].add(metadata[alan])
        base = taban_saglayici(saglayici)
        bedava = (str(saglayici).split('#', 1)[0].endswith(':free') and base in {*PAID, *FREE}) or base in FREE
        birim = 'istek' if bedava else 'nakit_micro' if base in ('opencode', 'openrouter') else 'abonelik_esdeger_micro' if base in ('opencode-go', 'claude') else 'bilinmeyen'
        s = sonuc['saglayicilar'].setdefault(saglayici, {'birim': birim, 'istek': 0, 'gercek_micro': 0, 'askida': 0, 'askida_micro': 0,
                                                            'guvence_acik': 0, 'guvence_tuketilen': 0, 'kullanilmayan': 0, 'belirsiz': 0})
        if gercek == 0 and makbuz == 'unused:' + ident:
            s['kullanilmayan'] += 1
            continue
        if metadata.get('guvence') and gercek == 0 and makbuz == 'claimed:' + ident:
            s['guvence_tuketilen'] += 1
            continue
        bound = max(rezerve, gercek or 0, *[v for v in (metadata.get('en_kotu'), metadata.get('verified_bound_micro')) if type(v) is int and v >= 0])
        if metadata.get('guvence') and gercek is None:
            s['guvence_acik'] += 1
            s['askida_micro'] += bound
            continue
        bilinen = (gercek is not None and isinstance(makbuz, str) and makbuz.startswith(ident + ':') and not metadata.get('guvence')
                   and not metadata.get('gozlem') and (bedava or base in USD_PROVIDERS))   # bucket name 'provider[#model|:free]'; the base decides
        if bilinen:
            s['istek'] += 1
            s['gercek_micro'] += gercek
        else:
            s['askida'] += 1
            s['belirsiz'] += 1
            s['askida_micro'] += bound
    return sonuc


def aylik_nakit(db: Path, ids: list[str], aylik_idler: set[str] | None = None, oturumlar: set[str] | None = None) -> dict:
    """Second ledger (zincir/aylik-butce.db): cash rows written for this run family; 'yok' if the file does not exist."""
    if not ids:
        return {'durum': 'yok', 'satir': 0, 'gercek_micro': 0, 'acik': 0}
    if not db.exists():
        return {'durum': 'yok', 'satir': 0, 'gercek_micro': 0, 'acik': 0}
    kosullar, degerler = [], []
    for ifade, kume in (("json_extract(metadata,'$.run_id')", list(ids)), ('id', sorted(aylik_idler or ())),
                        ("json_extract(metadata,'$.session_id')", sorted(oturumlar or ()))):
        if kume:
            kosullar.append(f"{ifade} in ({','.join('?' * len(kume))})")
            degerler.extend(kume)
    try:
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)) as con:
            satirlar = con.execute(f"select provider, reserved, actual from reservation where {' or '.join(kosullar)}", degerler).fetchall()
    except sqlite3.Error as exc:
        return {'durum': f'okunamadi: {type(exc).__name__}: {exc}', 'satir': 0, 'gercek_micro': 0, 'acik': 0}
    return {'durum': 'ok', 'satir': len(satirlar), 'gercek_micro': sum(int(a) for _, _, a in satirlar if a is not None),
            'acik': sum(1 for _, _, a in satirlar if a is None)}


def makbuz_kapilari(home: Path, ids: list[str], onek: str | None) -> dict:
    """CXR-03: separate mandatory states for live gates. An unreadable/out-of-scope/empty ledger cannot produce PASS; without a matching record
    'zero receipts in a run that expected a real call' is BLOCKED; an open reserve/guarantee/uncertain amount is FAIL; a provider outside the allowed one is FAIL;
    cash rows must match in both ledgers (in a Go-only run the monthly cash ledger must have no rows for this family)."""
    from butce_kapisi import taban_saglayici
    m = makbuz_oku(home / 'zincir/request-budget.db', ids)
    sonuc = {'okunabilir': m['hata'] is None, 'hata': m['hata'], 'satir': m['satir'], 'saglayicilar': m['saglayicilar'],
             'izinsiz': [], 'askida': 0, 'acik_guvence': 0, 'istek': 0, 'nakit_micro': 0, 'esdeger_micro': 0, 'aylik': None, 'durum': 'BLOCKED', 'neden': ''}
    if m['hata'] is not None:
        sonuc['neden'] = m['hata']
        return sonuc
    if m['satir'] == 0:
        sonuc['neden'] = 'eslesen makbuz yok: gercek cagri beklenen kosuda sifir kayit basarili kapanis degildir'
        return sonuc
    for sag, v in m['saglayicilar'].items():
        base = taban_saglayici(sag)
        if onek and base != onek.rstrip('/') and base != 'groq' and (v['istek'] or v['askida'] or v['guvence_acik']):
            sonuc['izinsiz'].append(sag)
        sonuc['askida'] += v['askida']
        sonuc['acik_guvence'] += v['guvence_acik']
        sonuc['istek'] += v['istek']
        if v['birim'] == 'nakit_micro':
            sonuc['nakit_micro'] += v['gercek_micro'] + v['askida_micro']
        elif v['birim'] == 'abonelik_esdeger_micro':
            sonuc['esdeger_micro'] += v['gercek_micro'] + v['askida_micro']
    sonuc['izinsiz'].sort()
    aylik = aylik_nakit(home / 'zincir/aylik-butce.db', ids, m['aylik_idler'], m['oturumlar'])
    sonuc['aylik'] = aylik
    if sonuc['izinsiz']:   # order: unallowed route > unclosed reserve > two ledgers > presence of a closed receipt
        sonuc['durum'] = 'FAIL'
        sonuc['neden'] = 'izinli saglayici disi makbuz: ' + ', '.join(sonuc['izinsiz'])
        return sonuc
    if sonuc['askida'] or sonuc['acik_guvence']:
        sonuc['durum'] = 'FAIL'
        sonuc['neden'] = f"kapanmamis rezerv/guvence: askida {sonuc['askida']}, acik guvence {sonuc['acik_guvence']}"
        return sonuc
    nakit_satir = sum(1 for v in m['saglayicilar'].values() if v['birim'] == 'nakit_micro' and (v['istek'] or v['askida']))
    if aylik['durum'].startswith('okunamadi'):
        sonuc['neden'] = 'aylik nakit defteri ' + aylik['durum']
        return sonuc
    if nakit_satir and aylik['durum'] == 'yok':
        sonuc['neden'] = 'nakit makbuz var ama aylik defter yok (iki defter esligi kurulamadi)'
        return sonuc
    if nakit_satir and (aylik['acik'] or aylik['gercek_micro'] != sum(v['gercek_micro'] for v in m['saglayicilar'].values() if v['birim'] == 'nakit_micro')):
        sonuc['durum'] = 'FAIL'
        sonuc['neden'] = f"iki defter uyusmuyor: istek defteri nakit {sonuc['nakit_micro']} micro, aylik defter {aylik['gercek_micro']} micro, acik {aylik['acik']}"
        return sonuc
    if not nakit_satir and aylik['durum'] == 'ok' and (aylik['satir'] or aylik['gercek_micro']):
        sonuc['durum'] = 'FAIL'
        sonuc['neden'] = f"nakit cagri yokken aylik defterde bu aileye {aylik['satir']} satir var"
        return sonuc
    if sonuc['istek'] == 0:
        sonuc['neden'] = 'dogrulanmis kapanmis makbuz yok (yalniz kullanilmayan/tuketilen guvence)'
        return sonuc
    sonuc['durum'] = 'PASS'
    sonuc['neden'] = f"{sonuc['istek']} kapanmis makbuz; nakit {sonuc['nakit_micro']} micro, esdeger {sonuc['esdeger_micro']} micro"
    return sonuc


def kapanis_kaniti(home: Path, rid: str, onek: str | None) -> dict:
    from kosu import TERMINAL, FileLock, RunStore, process_identity
    sonuc: dict = {'kapanabilir': False, 'neden': '', 'kosular': [rid], 'yukumluluk_usd': None, 'makbuz': None}
    store = RunStore(home)
    try:
        kayit = store.read(rid)
    except (OSError, ValueError) as exc:
        sonuc['neden'] = f'{rid} kaydi okunamadi: {type(exc).__name__}'
        return sonuc
    cocuklar = kayit.get('project_run_ids') or []
    if not isinstance(cocuklar, list) or not all(isinstance(c, str) for c in cocuklar):
        sonuc['neden'] = f'{rid} cocuk kosu listesi belirsiz'
        return sonuc
    ids = sorted({rid, *cocuklar})
    sonuc['kosular'] = ids
    for kimlik in ids:
        try:
            k = kayit if kimlik == rid else store.read(kimlik)
        except (OSError, ValueError) as exc:
            sonuc['neden'] = f'{kimlik} kaydi okunamadi: {type(exc).__name__}'
            return sonuc
        if k.get('state') not in TERMINAL:
            sonuc['neden'] = f"{kimlik} terminal degil ({k.get('state')})"
            return sonuc
        kilit = FileLock(store.path(kimlik) / '.owner.lock')
        if not kilit.acquire(blocking=False):
            sonuc['neden'] = f'{kimlik} sahip kilidi tutuluyor (aktif yazici)'
            return sonuc
        kilit.close()
        surecler = [k.get('phase_process')]
        for yol in [*sorted(store.path(kimlik).glob('tasks/*/attempts/*/process.json')), *sorted(store.path(kimlik).glob('tasks/qa/tool-checks/*/process.json'))]:
            try:
                surecler.append(json.loads(yol.read_text()).get('process_identity'))
            except (OSError, ValueError, AttributeError):
                sonuc['neden'] = f'{kimlik} surec kaydi okunamadi: {yol.parent.name}'
                return sonuc
        for kimlik_bilgisi in surecler:
            if (isinstance(kimlik_bilgisi, dict) and type(kimlik_bilgisi.get('pid')) is int and kimlik_bilgisi['pid'] > 1
                    and process_identity(kimlik_bilgisi['pid']) == kimlik_bilgisi):
                sonuc['neden'] = f"{kimlik} sureci canli (pid {kimlik_bilgisi['pid']})"
                return sonuc
    m = makbuz_kapilari(home, ids, onek)
    sonuc['makbuz'] = {a: m[a] for a in ('durum', 'satir', 'istek', 'nakit_micro', 'esdeger_micro', 'askida', 'acik_guvence')}
    if m['durum'] != 'PASS':
        sonuc['neden'] = f"makbuz kapisi {m['durum']}: {m['neden']}"
        return sonuc
    bilinmeyen = sorted(s for s, v in m['saglayicilar'].items() if v['birim'] == 'bilinmeyen')
    if bilinmeyen:
        sonuc['neden'] = 'makbuz birimi bilinmeyen saglayici: ' + ', '.join(bilinmeyen)
        return sonuc
    sonuc.update(kapanabilir=True, yukumluluk_usd=round((m['nakit_micro'] + m['esdeger_micro']) / 1e6, 6), neden=m['neden'])
    return sonuc


def kesme_kur() -> None:
    def kes(signum, frame):
        raise KeyboardInterrupt(signal.Signals(signum).name)
    for sinyal in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sinyal, kes)


def kesmeleri_ertele() -> None:
    for sinyal in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sinyal, signal.SIG_IGN)


def kancalari_kur() -> None:
    for d in ('zincir', 'logs', 'hooks'):
        (HOME / d).mkdir(parents=True, exist_ok=True)
    for name in ('pre-push', 'pre-commit', 'commit-msg'):
        if not (HOME / 'hooks' / name).exists():
            shutil.copy(ENGINE / 'hooks' / name, HOME / 'hooks' / name)
            (HOME / 'hooks' / name).chmod(0o700)


class OneriDurusu(Exception):
    """--yalniz-oneri: the engine's permission proposal was taken, no approval/call was made (for feasibility)."""

    def __init__(self, oneri: dict) -> None:
        super().__init__('yalniz oneri')
        self.oneri = oneri


class Paket:
    DURUS_BEKLEME_SN = 60.0

    def __init__(self, kimlik: str, fixture_dir: Path, dakika: float, tavan: float, yol: str, cikti: Path | None, kip: str,
                 hash_kontrol: bool = True, paket_tavan: float = 2.0, yalniz_oneri: bool = False, yukumluluk_iadesi: bool = False) -> None:
        if HOME is None or HOME == VARSAYILAN_EV or str(HOME).startswith(str(VARSAYILAN_EV) + os.sep):
            raise SystemExit('CADRE_HOME izole bir test koku olmali (varsayilan runtime kullanilamaz)')
        if not math.isfinite(dakika) or dakika < 1 or not math.isfinite(tavan) or tavan < 0:
            raise SystemExit('sure >= 1 dakika ve tavan negatif olmayan sonlu USD olmali')
        self.kimlik, self.dakika, self.tavan, self.yol, self.kip = kimlik, dakika, tavan, yol, kip
        self.fixture = (fixture_dir / kimlik).resolve()
        if not self.fixture.is_dir():
            raise SystemExit(f'fixture yok: {self.fixture}')
        self.damga = time.strftime('%Y%m%d-%H%M%S')
        self.son_monotonic = time.monotonic() + dakika * 60
        self.dizin = (cikti or HOME / 'reports/test-arsivi' / f'{kimlik}-{yol}-{self.damga}').resolve()
        self.dizin.mkdir(parents=True, exist_ok=True)
        self.calisma = HOME / 'scratch/kabul' / f'{kimlik}-{self.damga}'
        self.calisma.mkdir(parents=True, exist_ok=True)
        self.olaylar: list[str] = []
        self.kontroller: list[dict] = []
        self.kosular: list[str] = []
        self.fixture_hash = self.fixture_dogrula(fixture_dir, hash_kontrol)
        self.root = ROOT
        self.env_ek: dict[str, str] = {}
        self.yalniz_oneri = yalniz_oneri
        self.oneriler: list[dict] = []
        self.zincir = zincir_ortami_al(dict(os.environ))
        self.pay = PaketPayi(self.dizin.parent / 'kabul-paketi-pay.json', paket_tavan)
        self.iade = yukumluluk_iadesi
        self.iade_olaylari: list[dict] = []
        self.sahte_hazirla() if yol == 'sahte' else self.canli_hazirla()

    # --- preparation -------------------------------------------------------------------------------------------------
    def fixture_dogrula(self, fixture_dir: Path, zorunlu: bool) -> dict:
        manifest = fixture_dir.parent / 'fixture-manifest.json'
        gozlenen = {str(p.relative_to(fixture_dir)): sha(p) for p in sorted(self.fixture.rglob('*')) if p.is_file()}
        if not manifest.exists():
            if zorunlu:
                raise SystemExit(f'fixture-manifest.json yok: {manifest}')
            return {'manifest': None, 'dosyalar': gozlenen}
        beklenen = {k: v for k, v in json.loads(manifest.read_text()).items() if k.startswith(self.kimlik + '/')}
        fark = sorted(k for k in set(beklenen) | set(gozlenen) if beklenen.get(k) != gozlenen.get(k))
        if fark and zorunlu:
            raise SystemExit('fixture manifestle uyusmuyor: ' + ', '.join(fark))
        return {'manifest': str(manifest), 'manifest_sha256': sha(manifest), 'dosyalar': gozlenen, 'fark': fark}

    def sahte_hazirla(self) -> None:
        import test_uctan_uca
        from qa_fixture import instructions
        from test_kosu_sessions import KATALOG
        root = HOME / 'motor'
        if not root.exists():
            instructions(root)
            for sub in ('bin', 'lib', 'hooks'):
                (root / sub).mkdir(parents=True)
                for path in (ENGINE / sub).iterdir():
                    if path.is_file():
                        shutil.copy(path, root / sub / path.name)
                    elif path.is_dir() and path.name != '__pycache__':
                        shutil.copytree(path, root / sub / path.name, ignore=shutil.ignore_patterns('__pycache__'))
            (root / 'lib/butce.py').write_text(test_uctan_uca.FAKE_BUTCE)
            shutil.copy(TESTS / 'qa_fixture.py', root / 'lib/qa_fixture.py')
        self.root = root
        kancalari_kur()
        (HOME / 'zincir/maliyet-katsayi.txt').write_text('1.0\n')
        (HOME / 'zincir/go-gunluk-tavan.txt').write_text('2.0\n')
        (HOME / 'models.json').write_text(json.dumps(KATALOG))
        cfg = HOME / 'opencode-cfg'
        cfg.mkdir(exist_ok=True)
        (cfg / 'opencode.jsonc').write_text('{}')
        (HOME / 'kabul-senaryo.json').write_text(json.dumps({'senaryo': self.kimlik, 'kip': self.kip}))
        self.sarmal = HOME / 'kabul-sahte-opencode.py'
        shutil.copyfile(TESTS / 'kabul_sahte.py', self.sarmal)
        self.sarmal.chmod(0o700)
        self.env_ek = {'CADRE_MODEL_KATALOG': str(HOME / 'models.json'), 'OPENCODE_DB': str(HOME / 'absent.db'),
                       'CADRE_ZINCIR_KALITE': 'opencode-go/qwen3.8-max',
                       'OPENCODE_CFG': str(cfg), 'CLAUDE_BIN': '/nonexistent/claude', 'CADRE_OC_LOG': str(HOME / 'oc.log'),
                       'CADRE_PROGRESS_POLL': '1', 'CADRE_STALL_SEC': '600', 'CADRE_ABORT_SEC': '600', 'CADRE_MAX_RESUME': '1',
                       'CADRE_BEKLEME_ESIK_SN': '0', 'CADRE_BEKCI_SAHIP_PID': str(os.getpid()),
                       'CADRE_KRITIK_DOSYALAR': f"{HOME / 'zincir/maliyet-katsayi.txt'}:{HOME / 'zincir/go-gunluk-tavan.txt'}",
                       'CADRE_YON_KAYIT': str(HOME / 'zincir/yonlendirici.jsonl'), 'CADRE_YON_DURUM': str(HOME / 'zincir/saglayici-durum.json')}

    def canli_hazirla(self) -> None:
        import canli_senaryo
        kancalari_kur()
        self.sarmal = self.dizin / f'{self.yol}-yalniz-opencode.py'
        self.sarmal.write_text(canli_senaryo.sarmal_metni(self.yol))
        self.sarmal.chmod(0o700)
        self.env_ek = dict(canli_senaryo.zincir_ortami(self.yol, 'ucuz'))
        onek = canli_senaryo.SAGLAYICI[self.yol] + '/'
        disari = zincir_dogrula(self.zincir, onek)
        if disari:
            raise SystemExit(f'CADRE_ZINCIR_* izinli yol disinda model iceriyor ({onek}* bekleniyordu): {disari}; hicbir istek kaydedilmedi')
        self.go_once = canli_senaryo.Senaryo.go_durumu(self)

    def go_durumu(self) -> dict:
        import canli_senaryo
        return canli_senaryo.Senaryo.go_durumu(self)

    def ortam(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(('CADRE_', 'OPENCODE_', 'E2E_'))}
        env.update(CADRE_HOME=str(HOME), CADRE_ENGINE_ROOT=str(self.root), CADRE_REAL_OPENCODE=str(self.sarmal), CADRE_CLAUDE_PLAN='0',
                   CADRE_RADAR='0', CADRE_IZLE='0', CADRE_ONELEK_LLM='0', **GIT_KIMLIK)
        env.update({**self.env_ek, **self.zincir})
        return env

    # --- basic operations --------------------------------------------------------------------------------------------
    def olay(self, metin: str) -> None:
        satir = f"{time.strftime('%H:%M:%S')} {metin}"
        self.olaylar.append(satir)
        print(satir, flush=True)

    def sure_kaldi(self) -> float:
        return max(0.0, self.son_monotonic - time.monotonic())

    def cli(self, *args: str, data: dict | None = None, girdi: str | None = None, timeout: float | None = None,
            betik: str = 'lib/kosu.py') -> subprocess.CompletedProcess:
        if not self.sure_kaldi():
            raise TimeoutError('senaryo suresi doldu; yeni komut verilmedi')
        return subprocess.run([PY, str(self.root / betik), *args], input=json.dumps(data) if data is not None else girdi,
                              capture_output=True, text=True, env=self.ortam(), timeout=min(timeout or 300, max(1.0, self.sure_kaldi())))

    def proje_kaydet(self, repo: Path, ad: str, qa: str, proje_md: str, politika: str) -> None:
        pd = HOME / 'projects' / ad
        pd.mkdir(parents=True)
        for dosya, icerik in (('repo-path', str(repo)), ('base-branch', 'main'), ('qa-komut', qa), ('PROJECT.md', proje_md), ('POLICY.md', politika)):
            (pd / dosya).write_text(icerik + '\n')

    def repo_kur(self, ad: str, qa: str, proje_md: str, politika: str, haric: tuple[str, ...] = ('request.txt',)) -> tuple[Path, str]:
        repo = self.calisma / ad / 'repo'
        shutil.copytree(self.fixture, repo, ignore=shutil.ignore_patterns(*haric, '__pycache__'))
        git(repo, 'init', '-q', '-b', 'main')
        git(repo, 'add', '.')
        git(repo, 'commit', '-qm', 'kabul paketi tabani')
        self.proje_kaydet(repo.resolve(), ad, qa, proje_md, politika)
        return repo.resolve(), git(repo, 'rev-parse', 'HEAD')

    def istek_json(self, proje: str, hedef: Path, kip: str, istem: str, cikti: str, kabul: str, *, tur: str = 'existing',
                   backlog: str = '', zorluk: str | None = 'easy', ek: str = '', tavan: float | None = None) -> dict:
        return dict(submission_id=f'{self.kimlik.lower()}-{self.damga}{ek}', project=proje, target_path=str(hedef), target_kind=tur,
                    input_mode=kip, prompt=istem, output=cikti, acceptance=kabul, preference='economic', difficulty_override=zorluk,
                    duration_minutes=str(int(self.dakika)), budget_usd=format(round(self.tavan if tavan is None else tavan, 6), 'f'),
                    backlog_path=backlog, source_token='')

    def onek(self) -> str | None:
        if self.yol == 'sahte':
            return None
        import canli_senaryo
        return canli_senaryo.SAGLAYICI[self.yol] + '/'

    def askida_kapat(self) -> None:
        if self.yol == 'sahte':
            return
        try:
            r = subprocess.run([PY, '-c', 'import sys; sys.path.insert(0, sys.argv[1]); import butce; print(butce.or_askida_kapat())',
                                str(self.root / 'lib')], env=self.ortam(), capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            return
        if r.stdout.strip() not in ('', '0'):
            self.olay(f'askidaki makbuz mutabakati: {r.stdout.strip()} satir uretim kaydindan kapandi')

    def iade_denetle(self) -> list[dict]:
        self.askida_kapat()
        olaylar = self.pay.iade_denetimi(HOME, self.onek())
        for o in olaylar:
            if o['sonuc'] in ('iade', 'asim', 'gec_makbuz', 'geri_alindi'):
                self.olay(f"paket payi {o['sonuc']}: {o['rid']} pay {o['usd']} yukumluluk {o.get('yukumluluk_usd')} ({o['neden']})")
        self.iade_olaylari.extend(olaylar)
        return olaylar

    def istek(self, raw: dict) -> str:
        etiket = raw['submission_id']
        if self.iade:
            self.iade_denetle()
        self.pay.tahsis(self.kimlik, etiket, float(raw['budget_usd']))   # CXR-02: the shared package share comes before the request record
        r = self.cli('request', data=raw)
        if r.returncode != 0:
            raise RuntimeError(f'istek reddedildi: {r.stderr.strip()}')
        rid = json.loads(r.stdout)['run_id']
        self.pay.bagla(etiket, rid)
        self.kosular.append(rid)
        (self.dizin / f'istek-{rid[-8:]}.json').write_text(json.dumps(raw, ensure_ascii=False, indent=2))
        self.olay(f'istek kaydedildi: {rid}')
        return rid

    def onayla(self, rid: str) -> dict:
        r = self.cli('request-proposal', rid)
        if r.returncode != 0:
            raise RuntimeError(f'oneri uretilemedi: {r.stderr.strip()}')
        oneri = json.loads(r.stdout)
        (self.dizin / f'oneri-{rid[-8:]}.json').write_text(json.dumps(oneri, ensure_ascii=False, indent=2))
        if self.yol != 'sahte':
            import canli_senaryo
            onek = canli_senaryo.SAGLAYICI[self.yol] + '/'
            disari = sorted({m for havuz in (oneri.get('action', {}).get('models') or {}).values() for m in havuz if not str(m).startswith(onek)})
            if disari:
                raise RuntimeError(f'oneri izinli yol disinda model listeliyor ({onek}* bekleniyordu): {disari}; onay verilmedi, cagri yok. '
                                   'CADRE_ZINCIR_<ROL> ile yalniz izinli saglayici zincirini sabitle (surucu aynen alt surece tasir) ya da oneriyi kullaniciya goster.')
        if self.yalniz_oneri:
            self.oneriler.append({'rid': rid, 'budget_usd': oneri.get('action', {}).get('budget_usd'), 'models': oneri.get('action', {}).get('models'),
                                  'duration_minutes': oneri.get('action', {}).get('duration_minutes')})
            raise OneriDurusu(oneri)
        ilk = self.cli('request-approve', rid, data=oneri)
        if ilk.returncode != 0:
            raise RuntimeError(f'ilk onay reddedildi: {ilk.stderr.strip()}')
        ikinci = self.cli('request-approve', rid, json.loads(ilk.stdout)['confirmation'], data=oneri)
        if ikinci.returncode != 0:
            raise RuntimeError(f'ikinci onay reddedildi: {ikinci.stderr.strip()}')
        self.olay(f"motor izni baglandi: modeller={oneri.get('action', {}).get('models')} sure={oneri.get('action', {}).get('duration_minutes')} "
                  f"toplam={oneri.get('action', {}).get('budget_usd')}")
        return oneri

    def planla(self, rid: str) -> int:
        r = self.cli('request-plan', rid, timeout=self.sure_kaldi())
        (self.dizin / f'plan-{rid[-8:]}.log').write_text(r.stdout + r.stderr)
        self.olay(f'plan rc={r.returncode}')
        return r.returncode

    def kos(self, rid: str, devam: bool = False) -> int:
        if not self.sure_kaldi():
            raise TimeoutError('senaryo suresi doldu; yurutme baslatilmadi')
        komut = [PY, str(self.root / 'lib/kosu.py'), 'run', rid, *(['--devam'] if devam else [])]
        with open(self.dizin / f'koordinator-{rid[-8:]}.log', 'a') as log:
            proc = subprocess.Popen(komut, env=self.ortam(), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while proc.poll() is None and self.sure_kaldi():
                time.sleep(min(2.0, self.sure_kaldi()))
        except KeyboardInterrupt:
            kesmeleri_ertele()
            self.olay('KESME: kosu motorun iptal yoluyla durduruluyor')
            self.durdur(proc, rid)
            raise
        if proc.poll() is None:
            self.olay('SURE DOLDU: kosu motorun iptal yoluyla durduruluyor')
            self.durdur(proc, rid)
        self.olay(f"run{' --devam' if devam else ''} rc={proc.returncode} durum={self.kayit(rid).get('state')} neden={self.kayit(rid).get('reason')}")
        return proc.returncode if proc.returncode is not None else -1

    def iptal(self, rid: str) -> bool:
        try:
            r = subprocess.run([PY, str(self.root / 'lib/kosu.py'), 'cancel', rid], env=self.ortam(), capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            self.olay(f'iptal komutu kosulamadi: {rid}: {type(exc).__name__}: {exc}')
            return False
        return rid in r.stdout

    def sahipsizleri_topla(self, rid: str) -> None:
        from kosu import RunStore, reclaim_orphans
        try:
            reclaim_orphans(RunStore(HOME), {'run_id': rid})
        except (OSError, ValueError, KeyError, TypeError, ImportError, subprocess.SubprocessError) as exc:
            self.olay(f'sahipsiz surec toplanamadi: {rid}: {type(exc).__name__}: {exc}')

    def durdur(self, proc: subprocess.Popen, rid: str) -> None:
        self.iptal(rid)
        try:
            proc.wait(timeout=self.DURUS_BEKLEME_SN)
        except subprocess.TimeoutExpired:
            self.olay(f'koordinator {self.DURUS_BEKLEME_SN:.0f} sn icinde iptali bitirmedi: SIGKILL ve sahipsiz surec toplama')
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            self.sahipsizleri_topla(rid)

    # --- okuma -----------------------------------------------------------------------------------------------------
    def kayit(self, rid: str) -> dict:
        try:
            return json.loads((HOME / 'runs' / rid / 'run.json').read_text())
        except (OSError, ValueError):
            return {}

    def plan_gorunumu(self, rid: str) -> dict:
        from kosu import RunStore
        try:
            return RunStore(HOME).plan_view(rid)
        except Exception as exc:  # noqa: BLE001 - evidence gathering, not an acceptance decision
            return {'hata': f'{type(exc).__name__}: {exc}'}

    def denemeler(self, rid: str) -> list[dict]:
        sonuc = []
        for yol in sorted((HOME / 'runs' / rid).glob('tasks/*/attempts/*/attempt.json')):
            try:
                d = json.loads(yol.read_text())
            except (OSError, ValueError):
                continue
            sonuc.append({'gorev': yol.parents[2].name, 'rol': d.get('role'), 'model': d.get('model'), 'session_id': d.get('session_id'),
                          'outcome': d.get('outcome'), 'exit_code': d.get('exit_code')})
        return sonuc

    def cagrilar(self) -> list[dict]:
        yol = HOME / 'kabul-calls.jsonl'
        return [json.loads(s) for s in yol.read_text().splitlines()] if yol.exists() else []

    def onayli(self, kayit: dict) -> bool:
        from sonuc import approved
        try:
            return bool(approved(kayit))
        except Exception as exc:  # noqa: BLE001
            self.olay(f'approved() hata: {type(exc).__name__}: {exc}')
            return False

    def urun_kontrol(self, wt: str, komut: str, taban: str, korunan: dict[str, str]) -> dict:
        if not wt or not Path(wt).is_dir():
            return {'worktree': wt, 'rc': None, 'degisen': [], 'korunan_ayni': False}
        rc, cikti = guvenli_komut(komut, Path(wt), self.kum('urun'), 300)
        degisen = git(Path(wt), 'diff', '--name-only', taban, 'HEAD').splitlines() + \
            [x[2:].strip() for x in git(Path(wt), 'status', '--porcelain').splitlines() if '__pycache__' not in x]
        return {'worktree': wt, 'rc': rc, 'cikti': cikti[-1500:], 'degisen': sorted(set(degisen)),
                'korunan_ayni': all((Path(wt) / d).is_file() and sha(Path(wt) / d) == h for d, h in korunan.items())}

    def kum(self, ad: str) -> Path:
        """Sandbox temporary area (the only writable external directory); separate for every call."""
        import uuid
        yol = self.calisma / 'kum' / f'{ad}-{uuid.uuid4().hex[:8]}'
        yol.mkdir(parents=True, exist_ok=True)
        return yol

    def ortak_kanit(self, rid: str, qa: str, taban: str, korunan: dict[str, str]) -> dict:
        self.askida_kapat()
        k = self.kayit(rid)
        fr = k.get('final_result') or {}
        ids = sorted({rid, *(k.get('project_run_ids') or [])})
        onek = None
        if self.yol != 'sahte':
            import canli_senaryo
            onek = canli_senaryo.SAGLAYICI[self.yol] + '/'
        m = makbuz_kapilari(HOME, ids, onek)
        return {'kosular': ids, 'durum': k.get('state'), 'faz': k.get('phase'), 'neden': k.get('reason'), 'final': fr.get('status'),
                'qa': (fr.get('qa') or {}).get('status'), 'qa_tur': (fr.get('qa') or {}).get('kind'),
                'hakem': (fr.get('review') or {}).get('status'), 'hakem_bagimsiz': (fr.get('review') or {}).get('independent'),
                'teslim': (fr.get('delivery') or {}).get('kind'), 'denemeler': self.denemeler(rid), 'makbuz': m['saglayicilar'],
                'makbuz_durum': m, 'izinsiz_yol': m['izinsiz'], 'askida': m['askida'] + m['acik_guvence'],
                'urun': self.urun_kontrol(k.get('worktree') or '', qa, taban, korunan) if qa else {'worktree': k.get('worktree')},
                'onayli': self.onayli(k)}

    def canli_kapilar(self, e: dict, on: str) -> None:
        """Required on the live path: no unallowed provider, no pending receipt. The fake path produces no receipts; NOT_APPLICABLE is recorded."""
        if self.yol == 'sahte':
            self.kontroller.append({'id': f'{on}-L1', 'tur': 'sinir', 'yontem': 'surucu', 'beklenen': 'izinsiz yol yok; askida makbuz 0',
                                    'gozlenen': 'sahte saglayici makbuz uretmez', 'sonuc': 'NOT_APPLICABLE', 'kanit': 'yalniz canli yolda'})
            return
        m = e['makbuz_durum']
        kontrol(self.kontroller, f'{on}-L0', 'sinir', 'makbuz defteri okunabilir ve aileye ait kayit var (sifir kayit kapanis degildir)',
                {'okunabilir': m['okunabilir'], 'satir': m['satir'], 'hata': m['hata']}, True if m['okunabilir'] and m['satir'] else None)
        kontrol(self.kontroller, f'{on}-L1', 'sinir', 'yalniz izinli saglayici yolu', m['izinsiz'], (not m['izinsiz']) if m['okunabilir'] and m['satir'] else None)
        kontrol(self.kontroller, f'{on}-L2', 'sinir', 'askida/belirsiz makbuz ve acik guvence 0; en az bir dogrulanmis kapanmis makbuz',
                {'askida': m['askida'], 'acik_guvence': m['acik_guvence'], 'istek': m['istek']},
                (m['askida'] == 0 and m['acik_guvence'] == 0 and m['istek'] > 0) if m['okunabilir'] and m['satir'] else None)
        kontrol(self.kontroller, f'{on}-L3', 'sinir', 'iki defter esligi (nakit satirlari aylik defterde; Go-only kosuda aylik nakit satiri yok)',
                {'aylik': m['aylik'], 'durum': m['durum'], 'neden': m['neden']},
                (m['durum'] == 'PASS') if m['okunabilir'] and m['satir'] else None)

    def rolleri_kontrol(self, on: str, e: dict, beklenen: set[str], isci_var: bool) -> None:
        roller = {d['rol'] for d in e['denemeler']}
        kontrol(self.kontroller, f'{on}-R', 'normal', f'deneme rolleri {sorted(beklenen)} ' + ('(isci dahil)' if isci_var else '(isci yok)'),
                sorted(roller), beklenen <= roller and (isci_var == any(d['rol'] not in ('plan', 'review') for d in e['denemeler'])))
        oturumlar = [d['session_id'] for d in e['denemeler'] if d['rol'] == 'review']
        kontrol(self.kontroller, f'{on}-S', 'sinir', 'inceleme oturumlari ayri ve tekil', oturumlar, len(oturumlar) == len(set(oturumlar)) and bool(oturumlar))

    # --- senaryolar ------------------------------------------------------------------------------------------------
    def r01(self) -> dict:
        qa = f'{PY} -m unittest -v test_calc'
        repo, taban = self.repo_kur('kabul-r01', qa, 'Tamsayi toplama, yalniz standart unittest.',
                                    'Kullan-at kabul. Yalniz calc.py degisir; test_calc.py degismez. Kurulum, indirme, uzak Git yok.')
        korunan = {'test_calc.py': sha(repo / 'test_calc.py')}
        rid = self.istek(self.istek_json('kabul-r01', repo, 'goal', (self.fixture / 'request.txt').read_text(), 'Duzeltilmis calc.py',
                                         'test_calc.py icindeki uc test gecer; yalniz calc.py degisir'))
        self.onayla(rid)
        self.planla(rid)
        rc = self.kos(rid)
        e = self.ortak_kanit(rid, qa, taban, korunan)
        k = self.kontroller
        kontrol(k, 'R01-N1', 'normal', 'kosu completed, son kabul PASS, teslim reviewed-worktree', (rc, e['durum'], e['final'], e['teslim']),
                rc == 0 and e['durum'] == 'completed' and e['final'] == 'PASS' and e['teslim'] == 'reviewed-worktree')
        kontrol(k, 'R01-N2', 'normal', 'urun kabul komutu yesil', e['urun'].get('rc'), e['urun'].get('rc') == 0)
        kontrol(k, 'R01-N3', 'normal', 'gercek QA ve temiz hakem PASS, hakem bagimsiz', (e['qa'], e['hakem'], e['hakem_bagimsiz']),
                e['qa'] == 'PASS' and e['hakem'] == 'PASS' and bool(e['hakem_bagimsiz']))
        self.rolleri_kontrol('R01', e, {'plan', 'review'}, True)
        kontrol(k, 'R01-O1', 'olumsuz', 'test_calc.py degismez (kapsam/test degistirme yok)', e['urun'].get('korunan_ayni'), e['urun'].get('korunan_ayni'))
        kontrol(k, 'R01-O2', 'olumsuz', 'yalniz calc.py degisir', e['urun'].get('degisen'), e['urun'].get('degisen') == ['calc.py'])
        kayit = self.kayit(rid)
        wt = Path(kayit.get('worktree') or '')
        if wt.is_dir() and e['onayli']:
            orijinal = (wt / 'calc.py').read_bytes()
            (wt / 'calc.py').write_bytes(orijinal + b'# QA sonrasi degisiklik\n')
            try:
                kontrol(k, 'R01-O3', 'olumsuz', 'QA/hakem sonrasi urun degisirse kabul gecersiz', self.onayli(kayit), not self.onayli(kayit))
            finally:
                (wt / 'calc.py').write_bytes(orijinal)
            kontrol(k, 'R01-O4', 'sinir', 'urun geri gelince ayni kabul tekrar gecerli', self.onayli(kayit), self.onayli(kayit))
        else:
            kontrol(k, 'R01-O3', 'olumsuz', 'QA/hakem sonrasi urun degisirse kabul gecersiz', 'worktree/kabul yok', None)
        bagli(k, 'R01-O5', 'olumsuz', 'kirmizi/bos/atlanmis zorunlu QA ve test zayiflatma kabul uretemez',
              ['test_qa_kapsami', 'test_sonuc', 'test_hakem_politikasi'])
        self.canli_kapilar(e, 'R01')
        return {'kanit': e}

    def backlog_kurulum(self, ad: str) -> tuple[Path, str, str, dict[str, str]]:
        qa = f'{PY} -m unittest discover -s tests -t . -v'
        repo, taban = self.repo_kur(ad, qa, 'Kucuk metin kutuphanesi (metin.py, metin_cli.py), yalniz standart kutuphane.',
                                    'Kullan-at kabul. tests/ ve BACKLOG.md degismez. Kurulum, indirme, uzak Git yok.')
        korunan = {str(p.relative_to(repo)): sha(p) for p in sorted(repo.rglob('*.py')) if 'tests' in p.parts}
        korunan['BACKLOG.md'] = sha(repo / 'BACKLOG.md')
        return repo, taban, qa, korunan

    def madde_sonuclari(self, wt: str, maddeler: list[str]) -> dict[str, int | None]:
        if not wt or not Path(wt).is_dir():
            return {m: None for m in maddeler}
        return {m: guvenli_komut(f'{PY} -m unittest -v tests.test_{m.lower()}', Path(wt), self.kum('madde'), 120)[0] for m in maddeler}

    def r02(self) -> dict:
        return self.backlog_senaryosu('R02', 'kabul-r02', dogrulanmis=set())

    def r03(self) -> dict:
        return self.backlog_senaryosu('R03', 'kabul-r03', dogrulanmis={'B001', 'B002'})

    def backlog_senaryosu(self, on: str, ad: str, dogrulanmis: set[str]) -> dict:
        maddeler = ['B001', 'B002', 'B003', 'B004', 'B005']
        repo, taban, qa, korunan = self.backlog_kurulum(ad)
        k = self.kontroller
        once = self.madde_sonuclari(str(repo), maddeler)
        kontrol(k, f'{on}-P0', 'sinir', 'baslangic: ' + (f'{sorted(dogrulanmis)} yesil, kalan kirmizi' if dogrulanmis else 'bes madde kirmizi'), once,
                all((once[m] == 0) == (m in dogrulanmis) for m in maddeler))
        eski_metin = (repo / 'metin.py').read_text()
        rid = self.istek(self.istek_json(ad, repo, 'backlog', (self.fixture / 'request.txt').read_text(),
                                         'B001-B005 davranislari ve CLI', 'her maddenin kabul komutu ve butun testler yesil', backlog='BACKLOG.md'))
        self.onayla(rid)
        self.planla(rid)
        rc = self.kos(rid)
        e = self.ortak_kanit(rid, qa, taban, korunan)
        gorunum = self.plan_gorunumu(rid)
        plan = ((gorunum.get('history') or [{}])[-1].get('plan') or {})
        kaynak = {s.get('id'): s for s in plan.get('source_items') or []}
        e['madde_rc'] = self.madde_sonuclari(e['urun'].get('worktree') or '', maddeler)
        e['source_items'] = {i: {'status': s.get('status'), 'tasks': s.get('tasks')} for i, s in kaynak.items()}
        kontrol(k, f'{on}-N1', 'normal', 'kosu completed, son kabul PASS, reviewed-worktree', (rc, e['durum'], e['final'], e['teslim']),
                rc == 0 and e['durum'] == 'completed' and e['final'] == 'PASS' and e['teslim'] == 'reviewed-worktree')
        kontrol(k, f'{on}-N2', 'normal', 'her madde kendi kabul komutuyla yesil', e['madde_rc'], all(v == 0 for v in e['madde_rc'].values()))
        kontrol(k, f'{on}-N3', 'normal', 'butun urun kabulu (discover) yesil ve hakem bagimsiz', (e['urun'].get('rc'), e['hakem_bagimsiz']),
                e['urun'].get('rc') == 0 and bool(e['hakem_bagimsiz']))
        kontrol(k, f'{on}-N4', 'normal', 'bes kaynak kimligi planda hash ile esleniyor', sorted(kaynak), sorted(kaynak) == maddeler)
        if dogrulanmis:
            gorevli = {t for m in dogrulanmis for t in (kaynak.get(m) or {}).get('tasks') or []}
            kontrol(k, f'{on}-N5', 'normal', 'onceden yapilmis maddeler verified ve gorevsiz', {m: e['source_items'].get(m) for m in sorted(dogrulanmis)},
                    all((kaynak.get(m) or {}).get('status') == 'verified' and not (kaynak.get(m) or {}).get('tasks') for m in dogrulanmis))
            kontrol(k, f'{on}-O1', 'olumsuz', 'yapilmis is yeniden uretilmez/faturalanmaz (verified maddeye isci denemesi yok)',
                    {'verified_gorevler': sorted(gorevli), 'isci_gorevleri': sorted({d['gorev'] for d in e['denemeler'] if d['rol'] not in ('plan', 'review')})},
                    not gorevli)
            wt = Path(e['urun'].get('worktree') or '')
            yeni_metin = (wt / 'metin.py').read_text() if (wt / 'metin.py').is_file() else ''
            eski_govde = eski_metin.split('def en_uzun_kelime')[0].strip()
            kontrol(k, f'{on}-O2', 'olumsuz', 'B001/B002 uygulamasi (kelimeler/kelime_say/ters_cevir) aynen korunur', eski_govde in yeni_metin, eski_govde in yeni_metin)
        else:
            kontrol(k, f'{on}-N5', 'normal', 'bagimliliklar planda (B003->B001, B004->B001/B003, B005->B004) gorev bagimliligiyla korunur',
                    [(g.get('id'), g.get('bagimli')) for g in plan.get('gorevler') or []],
                    any(g.get('bagimli') for g in plan.get('gorevler') or []) or len(plan.get('gorevler') or []) == 1)
        self.rolleri_kontrol(on, e, {'plan', 'review'}, True)
        kontrol(k, f'{on}-O3', 'olumsuz', 'tests/ ve BACKLOG.md degismez', e['urun'].get('korunan_ayni'), e['urun'].get('korunan_ayni'))
        bagli(k, f'{on}-O4', 'olumsuz', 'eksik madde, kaynaksiz ek is veya yalniz cocuk PASS butun urun tamamlandi sayilmaz',
              ['test_request_pipeline.RequestPipelineTests.test_omitted_backlog_item_blocks_plan_publication',
               'test_request_pipeline.RequestCLITests.test_verified_backlog_red_qa_cannot_trigger_reimplementation_or_claim_completion',
               'test_proje_uctan_uca'])
        if dogrulanmis:
            bagli(k, f'{on}-O5', 'olumsuz', 'kesinti sonrasi cift is/fatura yok', ['test_kurtarma', 'test_gece_senaryolar', 'test_kosu_sessions'])
            kayit = self.kayit(rid)
            wt = Path(e['urun'].get('worktree') or '')
            if (wt / 'metin.py').is_file() and e['onayli']:
                orijinal = (wt / 'metin.py').read_bytes()
                (wt / 'metin.py').write_bytes(orijinal + b'\n# kabul sonrasi degisiklik\n')
                try:
                    kontrol(k, f'{on}-O6', 'olumsuz', 'kabul sonrasi urun degisirse eski kabul devralinmaz', self.onayli(kayit), not self.onayli(kayit))
                finally:
                    (wt / 'metin.py').write_bytes(orijinal)
            else:
                kontrol(k, f'{on}-O6', 'olumsuz', 'kabul sonrasi urun degisirse eski kabul devralinmaz', 'worktree/kabul yok', None)
            once_deneme = len(e['denemeler'])
            r = self.cli('run', rid)
            kontrol(k, f'{on}-O7', 'sinir', 'tamamlanmis is yeniden baslatilinca yeni cagri/fatura yok',
                    (r.returncode, len(self.denemeler(rid)) - once_deneme), r.returncode == 0 and len(self.denemeler(rid)) == once_deneme)
            bagli(k, f'{on}-O8', 'olumsuz', 'degisen kaynak/ozgun istek eski incelemeyle kabul edilmez',
                  ['test_request_pipeline.RequestCLITests.test_verified_backlog_zero_budget_and_changed_source_do_not_start_another_call',
                   'test_request_pipeline.RequestPipelineTests.test_cached_legacy_review_cannot_accept_a_changed_original_request'])
        self.canli_kapilar(e, on)
        return {'kanit': e, 'plan': plan}

    def r04(self) -> dict:
        repo, taban, qa, korunan = self.backlog_kurulum('kabul-r04')
        once = agac_hashleri(repo)
        rid = self.istek(self.istek_json('kabul-r04', repo, 'plan_only', (self.fixture / 'request.txt').read_text(), 'Incelenmis plan',
                                         'B001-B005 kimlikleri, kriterleri ve bagimliliklari planda; uygulama yok'))
        self.onayla(rid)
        self.planla(rid)
        rc = self.kos(rid)
        e = self.ortak_kanit(rid, '', taban, korunan)
        k = self.kontroller
        kayit = self.kayit(rid)
        teslim = kayit.get('plan_delivery') or {}
        try:
            urun = json.loads(Path(teslim['artifact']).read_text())
        except (KeyError, OSError, ValueError, TypeError):
            urun = {}
        metin = json.dumps(urun, ensure_ascii=False)
        kontrol(k, 'R04-N1', 'normal', 'plan teslim edildi: completed/plan-delivered, ayri inceleme PASS', (rc, e['durum'], e['faz'], (urun.get('review') or {}).get('status')),
                rc == 0 and e['durum'] == 'completed' and e['faz'] == 'plan-delivered' and (urun.get('review') or {}).get('status') == 'PASS')
        kontrol(k, 'R04-N2', 'normal', 'plan bes kaynak kimligini tasir', [b for b in ('B001', 'B002', 'B003', 'B004', 'B005') if b in metin],
                all(b in metin for b in ('B001', 'B002', 'B003', 'B004', 'B005')))
        kontrol(k, 'R04-N3', 'normal', 'teslim ozgun istek hashine bagli', urun.get('request_hash') == kayit.get('request_input_hash'),
                bool(urun) and urun.get('request_hash') == kayit.get('request_input_hash'))
        self.rolleri_kontrol('R04', e, {'plan', 'review'}, False)
        kontrol(k, 'R04-O1', 'olumsuz', 'uygulama/cocuk baslamaz: isci denemesi yok, worktree yok',
                (sorted({d['rol'] for d in e['denemeler']}), kayit.get('worktree')),
                all(d['rol'] in ('plan', 'review') for d in e['denemeler']) and not kayit.get('worktree'))
        kontrol(k, 'R04-O2', 'olumsuz', 'kaynak repo ve tests/ degismez', agac_hashleri(repo) == once, agac_hashleri(repo) == once)
        once_deneme = len(e['denemeler'])
        r = self.cli('run', rid)
        kontrol(k, 'R04-O3', 'sinir', 'tekrar baslatma yeni ucretli cagri/uygulama uretmez', (r.returncode, len(self.denemeler(rid)) - once_deneme),
                r.returncode == 0 and len(self.denemeler(rid)) == once_deneme)
        bagli(k, 'R04-O4', 'olumsuz', 'kritik karar yanitsizken inceleme/dagitim/diriltme yok',
              ['test_request_pipeline.RequestPipelineTests.test_critical_boundaries_wait_without_review_dispatch_retry_or_revival',
               'test_request_pipeline.RequestPipelineTests.test_answer_requires_a_new_plan_and_clean_review_and_does_not_grant_spending_permission'])
        bagli(k, 'R04-O5', 'olumsuz', 'degisen kaynak eski plan/incelemeyle kabul edilmez',
              ['test_request_pipeline.RequestPipelineTests.test_cached_legacy_review_rejects_changed_source_without_another_paid_review',
               'test_request_pipeline.RequestPipelineTests.test_real_request_delivery_cannot_reuse_a_pass_for_changed_plan_content'])
        self.canli_kapilar(e, 'R04')
        return {'kanit': e, 'teslim': {kk: urun.get(kk) for kk in ('request_hash', 'review')}}

    def r05(self) -> dict:
        repo, taban = self.repo_kur('kabul-r05', 'true', 'Cadre kaynak kopyasi (uc dosya): yalniz inceleme.',
                                    'Kullan-at kabul. Kaynak degismez; kod onarimi yok. Kurulum, indirme, uzak Git yok.')
        once = agac_hashleri(repo)
        istem = 'TUR: inceleme\n' + (self.fixture / 'request.txt').read_text()
        rid = self.istek(self.istek_json('kabul-r05', repo, 'goal', istem, 'Dosya/satir kanitli inceleme raporu',
                                         'her iddia kaynak satiriyla bagli; kaynak degismedi; bilinmeyen acik'))
        self.onayla(rid)
        self.planla(rid)
        rc = self.kos(rid)
        e = self.ortak_kanit(rid, '', taban, {})
        k = self.kontroller
        kayit = self.kayit(rid)
        teslim = kayit.get('review_delivery') or {}
        try:
            urun = json.loads(Path(teslim['artifact']).read_bytes())
        except (KeyError, OSError, ValueError, TypeError):
            urun = {}
        kanitlar = urun.get('evidence') or []
        kontrol(k, 'R05-N1', 'normal', 'inceleme teslim edildi: completed/review-delivered, son kabul PASS', (rc, e['durum'], e['faz'], e['final']),
                rc == 0 and e['durum'] == 'completed' and e['faz'] == 'review-delivered' and e['final'] == 'PASS')
        kontrol(k, 'R05-N2', 'normal', 'kod QA uygulanmaz (NOT_APPLICABLE), rapor QA PASS', ((((kayit.get('final_result') or {}).get('qa') or {}).get('code_qa') or {}).get('status'), e['qa']),
                (((kayit.get('final_result') or {}).get('qa') or {}).get('code_qa') or {}).get('status') == 'NOT_APPLICABLE' and e['qa'] == 'PASS')
        dosya_kanitlari = [c.get('kaynak', '') for c in kanitlar if c.get('tur') == 'dosya']
        kontrol(k, 'R05-N3', 'normal', 'iddialar kaynak dosya/satir kanitiyla bagli (uc dosyadan en az iki tanesi)', dosya_kanitlari,
                bool(kanitlar) and len({c.split(':')[0] for c in dosya_kanitlari}) >= 2)
        kontrol(k, 'R05-N4', 'normal', 'yazar ve son hakem oturumlari ayri', (urun.get('session_id'), ((urun.get('result') or {}).get('review') or {}).get('session_id')),
                bool(urun) and urun.get('session_id') != ((urun.get('result') or {}).get('review') or {}).get('session_id'))
        kontrol(k, 'R05-O1', 'olumsuz', 'kaynak agaci degismez (inceleme kod onarimina donusmez)', agac_hashleri(repo) == once, agac_hashleri(repo) == once)
        if urun and teslim.get('artifact'):
            yol = Path(teslim['artifact'])
            ham = yol.read_bytes()
            yol.write_bytes(ham.replace(b'"report"', b'"report_x"', 1))
            try:
                kontrol(k, 'R05-O2', 'olumsuz', 'rapor/kanit sonradan degisirse kabul gecersiz', self.onayli(kayit), not self.onayli(kayit))
            finally:
                yol.write_bytes(ham)
            kontrol(k, 'R05-O3', 'sinir', 'orijinal rapor geri gelince kabul gecerli', self.onayli(kayit), self.onayli(kayit))
        if self.yol == 'sahte':
            (HOME / 'kabul-senaryo.json').write_text(json.dumps({'senaryo': 'R05', 'kip': 'yazar-kaynak-yazar'}))
            try:
                repo2, taban2 = self.repo_kur('kabul-r05-ret', 'true', 'Inceleme ret denemesi.', 'Kaynak degismez.')
                rid2 = self.istek(self.istek_json('kabul-r05-ret', repo2, 'goal', istem, 'Inceleme', 'kaynak degismez', ek='-ret'))
                self.onayla(rid2)
                self.planla(rid2)
                rc2 = self.kos(rid2)
                k2 = self.kayit(rid2)
                kontrol(k, 'R05-O4', 'olumsuz', 'yazar kaynaga yazarsa teslim reddedilir (salt-okur rol korunur)', (rc2, k2.get('state'), k2.get('reason')),
                        rc2 != 0 and k2.get('state') != 'completed' and k2.get('reason') == 'plan-review-source-modified')
            finally:
                (HOME / 'kabul-senaryo.json').write_text(json.dumps({'senaryo': 'R05', 'kip': ''}))
        bagli(k, 'R05-O5', 'olumsuz', 'uydurma kanit, ret, tekrar kullanilan oturum, kritik soru ve kaynaga yazma teslim uretmez',
              ['test_request_pipeline.RequestPipelineTests.test_review_product_rejects_false_evidence_rejection_reused_session_and_critical_questions',
               'test_request_pipeline.RequestPipelineTests.test_review_product_delivers_source_bound_evidence_without_code_qa_or_source_edits'])
        self.canli_kapilar(e, 'R05')
        return {'kanit': e, 'kanit_sayisi': len(kanitlar), 'dosya_kanitlari': dosya_kanitlari}

    def iskelet_kur(self, hedef: Path, tur: str = 'python') -> subprocess.CompletedProcess:
        return self.cli('--olustur', str(hedef), '--tur', tur, girdi='evet\nevet\n', betik='lib/proje_kayit.py', timeout=300)

    def urun_dene(self, wt: Path, csv_yolu: Path) -> tuple[int | None, str, dict | None]:
        """Runs the model's product in the sandbox; (rc, combined output, last JSON line|None). rc None = could not run."""
        import shlex
        rc, cikti = guvenli_komut(f'PYTHONPATH=src {shlex.quote(PY)} -m harcama_ozeti {shlex.quote(str(csv_yolu))}', wt, self.kum('cli'), 60)
        return rc, cikti, json_satiri(cikti)

    def r06_kontroller(self, on: str, rid: str, rc: int, e: dict, beklenen: dict) -> None:
        k = self.kontroller
        wt = Path(e['urun'].get('worktree') or '')
        kontrol(k, f'{on}-N1', 'normal', 'kosu completed, son kabul PASS, reviewed-worktree', (rc, e['durum'], e['final'], e['teslim']),
                rc == 0 and e['durum'] == 'completed' and e['final'] == 'PASS' and e['teslim'] == 'reviewed-worktree')
        kontrol(k, f'{on}-N2', 'normal', 'iskelet QA (make test) yesil', e['urun'].get('rc'), e['urun'].get('rc') == 0)
        if wt.is_dir():
            rcv, out, gozlenen = self.urun_dene(wt, self.fixture / 'valid.csv')
            kontrol(k, f'{on}-N3', 'normal', f"valid.csv -> {beklenen['valid']} (sandbox)", (rcv, gozlenen if gozlenen is not None else out[-200:]),
                    rcv == 0 and gozlenen == beklenen['valid'])
            for ad in ('invalid', 'missing-column'):
                rci, outi, jsoni = self.urun_dene(wt, self.fixture / f'{ad}.csv')
                kontrol(k, f'{on}-O-{ad}', 'olumsuz', f'{ad}.csv -> nonzero, kismi JSON cikti yok (sandbox)', (rci, outi[-200:]),
                        rci is not None and rci != 0 and jsoni is None)
            kilit = (wt / 'requirements.lock').read_text() if (wt / 'requirements.lock').is_file() else None
            kontrol(k, f'{on}-N4', 'normal', 'bagimlilik yok: requirements.lock yalniz yorum', kilit,
                    kilit is not None and all(s.startswith('#') or not s.strip() for s in kilit.splitlines()))
            kontrol(k, f'{on}-N5', 'normal', 'iskelet manifesti ve kurulum/test/calistirma kaydi var',
                    ((wt / 'cadre-iskelet.json').is_file(), (HOME / 'projects' / self.kayit(rid)['project'] / 'iskelet.json').is_file()),
                    (wt / 'cadre-iskelet.json').is_file() and (HOME / 'projects' / self.kayit(rid)['project'] / 'iskelet.json').is_file())
            once_dosyalar = sorted(agac_hashleri(wt))
            once_deneme = len(e['denemeler'])
            r = self.cli('run', rid)
            kontrol(k, f'{on}-O1', 'olumsuz', 'tekrar baslatma dosya/urun cogaltmaz, yeni isci yok',
                    (r.returncode, len(self.denemeler(rid)) - once_deneme, sorted(agac_hashleri(wt)) == once_dosyalar),
                    r.returncode == 0 and len(self.denemeler(rid)) == once_deneme and sorted(agac_hashleri(wt)) == once_dosyalar)
        else:
            for kimlik in (f'{on}-N3', f'{on}-N4', f'{on}-N5', f'{on}-O1'):
                kontrol(k, kimlik, 'normal', 'worktree urunu', 'worktree yok', None)
        self.rolleri_kontrol(on, e, {'plan', 'review'}, True)

    def r06(self) -> dict:
        beklenen = json.loads((self.fixture / 'expected.json').read_text())
        istem = (self.fixture / 'request.txt').read_text()
        k = self.kontroller
        kabul = 'make test yesil; valid.csv market 20.00 ulasim 4.50 toplam 24.50; gecersiz tutar/sutun nonzero ve kismi cikti yok'
        # (a) target only: an empty new target, the first commit with the skeleton goes through two-step permission
        hedef = self.calisma / 'yeni' / 'harcama-ozeti'
        hedef.parent.mkdir(parents=True)
        pay_a, _ = r06_paylari(self.tavan, None)
        rid = self.istek(self.istek_json('harcama-ozeti', hedef, 'goal', istem, 'Calisan harcama_ozeti CLI', kabul, tur='new', tavan=pay_a))
        if self.yalniz_oneri:   # feasibility: the proposals of both variants are taken, no approval/call
            try:
                self.onayla(rid)
            except OneriDurusu:
                pass
            hedef_b = self.calisma / 'backlog' / 'harcama-ozeti'
            hedef_b.parent.mkdir(parents=True)
            kur_b = self.iskelet_kur(hedef_b, 'python')
            if kur_b.returncode != 0:
                raise RuntimeError('iskelet kurulamadi: ' + kur_b.stderr[-300:])
            proje_b = kur_b.stdout.split()[-1]
            (hedef_b / 'BACKLOG.md').write_text(R06_BACKLOG)
            git(hedef_b, 'add', 'BACKLOG.md')
            git(hedef_b, 'commit', '-qm', 'hazir backlog')
            _, pay_b = r06_paylari(self.tavan, None)
            rid_b = self.istek(self.istek_json(proje_b, hedef_b.resolve(), 'backlog', istem, 'Calisan harcama_ozeti CLI', kabul, backlog='BACKLOG.md', ek='-b', tavan=pay_b))
            self.onayla(rid_b)
            raise OneriDurusu(self.oneriler[-1])
        self.onayla(rid)
        rc0 = self.kos(rid)
        k0 = self.kayit(rid)
        kontrol(k, 'R06A-P1', 'sinir', 'bos hedefte uygulama ilk commit iznini bekler (retryable/new-project-init-permission-required)',
                (rc0, k0.get('state'), k0.get('reason'), hedef.exists()),
                rc0 != 0 and k0.get('reason') == 'new-project-init-permission-required' and not hedef.exists())
        kotu = self.iskelet_kur(hedef, 'go')
        kontrol(k, 'R06A-O2', 'olumsuz', 'desteklenmeyen tur reddedilir, hedef olusmaz', (kotu.returncode, hedef.exists()), kotu.returncode != 0 and not hedef.exists())
        kur = self.iskelet_kur(hedef, 'python')
        kontrol(k, 'R06A-P2', 'normal', 'python iskeleti iki yazili yanitla ilk commit olarak kaydedilir', (kur.returncode, kur.stdout.strip()[-40:]),
                kur.returncode == 0 and (hedef / 'cadre-iskelet.json').is_file())
        taban = git(hedef, 'rev-parse', 'HEAD') if (hedef / '.git').is_dir() else ''
        rc = self.kos(rid, devam=True)
        e = self.ortak_kanit(rid, 'make test', taban, {})
        self.r06_kontroller('R06A', rid, rc, e, beklenen)
        self.canli_kapilar(e, 'R06A')
        # (b) ready backlog: the same request, an existing skeleton repo + BACKLOG.md in a separate copy
        hedef_b = self.calisma / 'backlog' / 'harcama-ozeti'   # same folder name: the package name stays harcama_ozeti, the project name differs
        hedef_b.parent.mkdir(parents=True)
        kur_b = self.iskelet_kur(hedef_b, 'python')
        if kur_b.returncode != 0:
            kontrol(k, 'R06B-P1', 'normal', 'backlog kopyasi icin iskelet reposu', kur_b.stderr[-300:], False)
            return {'kanit_a': e}
        proje_b = kur_b.stdout.split()[-1]   # approval prompts are printed on the same line; the recorded name is the last word
        kontrol(k, 'R06B-P1', 'normal', 'ikinci kopya ayri proje adiyla kaydedilir', proje_b, bool(proje_b) and proje_b != 'harcama-ozeti')
        (hedef_b / 'BACKLOG.md').write_text(R06_BACKLOG)
        git(hedef_b, 'add', 'BACKLOG.md')
        git(hedef_b, 'commit', '-qm', 'hazir backlog')
        taban_b = git(hedef_b, 'rev-parse', 'HEAD')
        a_m = e['makbuz_durum']
        a_yukumluluk = ((a_m['nakit_micro'] + a_m['esdeger_micro']) / 1e6) if a_m['okunabilir'] and a_m['satir'] else None
        pay_a, pay_b = r06_paylari(self.tavan, a_yukumluluk)
        kontrol(k, 'R06B-P0', 'sinir', 'R06 aile payi iki varyant arasinda bolusulur (A+B <= pay; A yukumlulugu bilinmiyorsa B = pay - A tavani)',
                {'aile_payi': self.tavan, 'A': pay_a, 'A_yukumluluk': a_yukumluluk, 'B': pay_b}, pay_a + pay_b <= self.tavan + 1e-9 and pay_b > 0)
        if pay_b <= 0:
            return {'kanit_a': e}
        rid_b = self.istek(self.istek_json(proje_b, hedef_b.resolve(), 'backlog', istem, 'Calisan harcama_ozeti CLI', kabul, backlog='BACKLOG.md', ek='-b', tavan=pay_b))
        self.onayla(rid_b)
        self.planla(rid_b)
        rc_b = self.kos(rid_b)
        e_b = self.ortak_kanit(rid_b, 'make test', taban_b, {'BACKLOG.md': sha(hedef_b / 'BACKLOG.md')})
        self.r06_kontroller('R06B', rid_b, rc_b, e_b, beklenen)
        plan_b = ((self.plan_gorunumu(rid_b).get('history') or [{}])[-1].get('plan') or {})
        kontrol(k, 'R06B-N6', 'normal', 'backlog kimligi H001 planda esleniyor ve BACKLOG.md korunur',
                ([s.get('id') for s in plan_b.get('source_items') or []], e_b['urun'].get('korunan_ayni')),
                [s.get('id') for s in plan_b.get('source_items') or []] == ['H001'] and bool(e_b['urun'].get('korunan_ayni')))
        bagli(k, 'R06-O3', 'olumsuz', 'yabanci kayit/dolu hedef/eksik izinle devam yok; iskelet kesintisi tek commit',
              ['test_request_pipeline.RequestPipelineTests.test_new_target_with_stray_files_or_foreign_registration_does_not_continue',
               'test_qa3_fixleri.IskeletTests', 'test_proje_kayit'])
        self.canli_kapilar(e_b, 'R06B')
        return {'kanit_a': e, 'kanit_b': e_b}

    def r07(self) -> dict:
        import csv
        from decimal import Decimal
        repo, taban = self.repo_kur('kabul-r07', 'true', 'Agsiz operasyon raporu: orders.csv, metadata.json, contradictory-summary.txt.',
                                    'Kullan-at kabul. Yalniz RAPOR.md ve RAPOR.kanit.jsonl yazilir; kaynak degismez.')
        once = agac_hashleri(repo)
        meta = json.loads((self.fixture / 'metadata.json').read_text())
        toplam = {'completed': Decimal(0), 'cancelled': Decimal(0), 'all': Decimal(0)}
        with open(self.fixture / 'orders.csv', newline='') as f:
            for satir in csv.DictReader(f):
                toplam[satir['status']] += Decimal(satir['amount'])
                toplam['all'] += Decimal(satir['amount'])
        bagimsiz = {kk: f'{v:.2f}' for kk, v in toplam.items()}
        istem = 'TUR: rapor\nRAPOR: RAPOR.md\nBÖLÜMLER: Kaynak; Hesap; Yorum\n' + (self.fixture / 'request.txt').read_text()
        rid = self.istek(self.istek_json('kabul-r07', repo, 'goal', istem, 'RAPOR.md + RAPOR.kanit.jsonl',
                                         'toplamlar bagimsiz hesapla eslesir; celiski ve kaynak tarihi gorunur; kaynak degismez'))
        self.onayla(rid)
        self.planla(rid)
        rc = self.kos(rid)
        e = self.ortak_kanit(rid, '', taban, {})
        k = self.kontroller
        kayit = self.kayit(rid)
        wt = Path(kayit.get('worktree') or '')
        rapor = (wt / 'RAPOR.md').read_text() if (wt / 'RAPOR.md').is_file() else ''
        fr = kayit.get('final_result') or {}
        kontrol(k, 'R07-N1', 'normal', 'kosu completed, son kabul PASS, rapor QA tipi rapor', (rc, e['durum'], e['final'], e['qa_tur']),
                rc == 0 and e['durum'] == 'completed' and e['final'] == 'PASS' and e['qa_tur'] == 'rapor')
        kontrol(k, 'R07-N2', 'normal', 'hakem yoklamasi kayitta', bool((fr.get('review') or {}).get('yoklama')), bool((fr.get('review') or {}).get('yoklama')))
        kontrol(k, 'R07-N3', 'normal', f'bagimsiz hesap {bagimsiz} raporda kanit kimligiyle gecer',
                {kk: bool(re.search(re.escape(v) + r'[^\n]*\[K', rapor)) for kk, v in bagimsiz.items()},
                all(re.search(re.escape(v) + r'[^\n]*\[K', rapor) for v in bagimsiz.values()))
        kontrol(k, 'R07-N4', 'normal', 'kaynak tarihi raporda (Veri tarihi) ve metadata ile uyumlu', meta['source_date'] in rapor and 'Veri tarihi' in rapor,
                meta['source_date'] in rapor and 'Veri tarihi' in rapor)
        kontrol(k, 'R07-N5', 'normal', 'analist karari (tek analist/mutabakat) raporda gorunur', bool(re.search(r'(?i)analist', rapor)), bool(re.search(r'(?i)analist', rapor)))
        celiski = bool(re.search(r'21\.00[^\n]*(dogrulanmadi|çelişk|celisk|uyusmaz|uyuşmaz)', rapor, re.I))
        kontrol(k, 'R07-O1', 'olumsuz', 'celiskili ozet (21.00) uydurma veriyle kapatilmaz; celiski acikca yazilir', celiski, celiski)
        degisen = sorted(agac_hashleri(wt).keys() - once.keys()) if wt.is_dir() else []
        kontrol(k, 'R07-O2', 'olumsuz', 'rapor disi kaynak degismez; yalniz rapor + kanit eki (+ analist notu) yazilir',
                (agac_hashleri(repo) == once, degisen),
                agac_hashleri(repo) == once and set(degisen) <= {'RAPOR.md', 'RAPOR.kanit.jsonl', '.cadre-analist.md', 'ANALIST.md'})
        self.r07_yerel_olumsuzlar(repo, kayit.get('source_sha') or taban)
        self.rolleri_kontrol('R07', e, {'plan', 'review'}, True)
        bagli(k, 'R07-O5', 'olumsuz', 'eski/tarihsiz kaynak, celisen kanit, olmayan dosya kirmizi; butce beklemesinde ayni taslakla devam',
              ['test_rapor_kipi.RaporQaTest', 'test_rapor_yazar_tukenme', 'test_rapor_kipi.RaporUctanUcaTest'])
        self.canli_kapilar(e, 'R07')
        return {'kanit': e, 'bagimsiz_hesap': bagimsiz, 'rapor_uzunluk': len(rapor)}

    def r07_yerel_olumsuzlar(self, repo: Path, source_sha: str) -> None:
        """The same report QA gate without a model call: a contradictory claim and a missing required source must be red."""
        import rapor
        k = self.kontroller
        beyan = {'yol': 'RAPOR.md', 'kanit': 'RAPOR.kanit.jsonl', 'bolumler': ['Kaynak', 'Hesap', 'Yorum']}
        for ad, metin, kanit, beklenen_bulgu in (
                ('celiski', '# Kaynak\norders.csv okundu [K1].\n# Hesap\ncompleted toplami 21.00 [K2].\n# Yorum\nyok.\n',
                 [{'id': 'K1', 'iddia': 'kaynak', 'tur': 'dosya', 'kaynak': 'orders.csv:1', 'beklenen': 'order_id,date,status,amount'},
                  {'id': 'K2', 'iddia': 'toplam', 'tur': 'komut', 'beklenen': '21.00',
                   'kaynak': "python3 -c \"import csv,decimal;print(sum(decimal.Decimal(r['amount']) for r in csv.DictReader(open('orders.csv')) if r['status']=='completed'))\""}],
                 'ciktida yok'),
                ('eksik-kaynak', '# Kaynak\neksik.csv 3 satir [K1].\n# Hesap\nyok [K1].\n# Yorum\nyok.\n',
                 [{'id': 'K1', 'iddia': 'kaynak', 'tur': 'dosya', 'kaynak': 'eksik.csv:1', 'beklenen': 'x'}], 'dosya yok')):
            kopya = self.calisma / f'r07-olumsuz-{ad}'
            shutil.copytree(repo, kopya)
            (kopya / 'RAPOR.md').write_text(metin)
            (kopya / 'RAPOR.kanit.jsonl').write_text(''.join(json.dumps(x) + '\n' for x in kanit))
            try:
                sonuc = rapor.rapor_qa(str(kopya), beyan, source_sha, str(self.calisma / f'r07-gecici-{ad}'), tohum='kabul')
                gozlenen = (sonuc['status'], sonuc['bulgular'][:4])
                gecti = sonuc['status'] == 'FAIL' and any(beklenen_bulgu in b for b in sonuc['bulgular'])
            except Exception as exc:  # noqa: BLE001
                gozlenen, gecti = f'{type(exc).__name__}: {exc}', None
            kontrol(k, f'R07-O3-{ad}', 'olumsuz', f'{ad}: rapor QA kirmizi ({beklenen_bulgu})', gozlenen, gecti)

    # --- closing ---------------------------------------------------------------------------------------------------
    def kapat(self) -> list[str]:
        from kosu import TERMINAL
        kapanan = []
        for rid in self.kosular:
            k = self.kayit(rid)
            if k and k.get('state') not in TERMINAL:
                self.sahipsizleri_topla(rid)
                if self.iptal(rid):
                    kapanan.append(f"{rid} ({k.get('state')})")
        for pidfile in (HOME / 'zincir').glob('*.pid'):
            try:
                pid = int(pidfile.read_text().strip())
                komut = subprocess.run(['ps', '-o', 'command=', '-p', str(pid)], capture_output=True, text=True).stdout
                if str(HOME) in komut or str(self.root) in komut:
                    os.kill(pid, signal.SIGTERM)
                    kapanan.append(f'bekci {pidfile.stem} pid {pid}')
            except (OSError, ValueError):
                continue
        if kapanan:
            self.olay(f'kendi kosu/bekcileri kapatildi: {kapanan}')
        return kapanan

    def motor(self) -> str:
        try:
            return git(ENGINE.parent, 'rev-parse', '--short', 'HEAD')
        except (subprocess.CalledProcessError, OSError):
            return '-'

    def yaz(self, sonuc: dict) -> dict:
        normal = [c for c in self.kontroller if c['tur'] == 'normal']
        olumsuz = [c for c in self.kontroller if c['tur'] != 'normal']
        basarisiz = [c['id'] for c in self.kontroller if c['sonuc'] in ('FAIL', 'BLOCKED')]
        karar = sonuc.get('karar') or ('PASS' if not basarisiz and normal and olumsuz else 'FAIL')
        if self.yol == 'sahte' and karar == 'PASS':
            karar = 'LOCAL_PASS'
        if karar == 'ONERI':
            sonuc = {**sonuc, 'oneri_modelleri': (sonuc.get('oneri') or {}).get('action', {}).get('models'),
                     'oneri_sure_dk': (sonuc.get('oneri') or {}).get('action', {}).get('duration_minutes'),
                     'oneri_toplam_usd': (sonuc.get('oneri') or {}).get('action', {}).get('budget_usd')}
        ozet = {'kimlik': self.kimlik, 'damga': self.damga, 'yol': self.yol, 'canli': self.yol != 'sahte', 'motor': self.motor(),
                'motor_koku': str(self.root), 'ev': str(HOME), 'dakika': self.dakika, 'tavan_usd': self.tavan, 'fixture': str(self.fixture),
                'fixture_dogrulama': self.fixture_hash, 'karar': karar, 'kontrol_sayisi': {'normal': len(normal), 'olumsuz_sinir': len(olumsuz),
                'basarisiz': basarisiz}, 'kontroller': self.kontroller, 'kosular': self.kosular, 'olaylar': self.olaylar,
                'go_once': getattr(self, 'go_once', None), 'go_sonra': self.go_durumu() if self.yol != 'sahte' else None,
                'zincir_ortami': self.zincir, 'paket_payi': {'dosya': str(self.pay.dosya), 'tavan_usd': self.pay.veri['paket_tavan_usd'],
                                                              'tahsis_usd': self.pay.tahsis_edilen(), 'kalan_usd': self.pay.kalan(),
                                                              'brut_usd': self.pay.brut(), 'iade_usd': self.pay.iade_edilen(),
                                                              'yukumluluk_iadesi': self.iade},
                'sahte_cagrilar': [c['rol'] for c in self.cagrilar()] if self.yol == 'sahte' else None, **{kk: v for kk, v in sonuc.items() if kk != 'karar'}}
        (self.dizin / 'ozet.json').write_text(json.dumps(ozet, ensure_ascii=False, indent=2, default=str))
        with open(self.dizin.parent / 'kabul-paketi.jsonl', 'a') as f:
            f.write(json.dumps({kk: ozet.get(kk) for kk in ('kimlik', 'damga', 'yol', 'motor', 'karar', 'kontrol_sayisi', 'kosular')},
                               ensure_ascii=False, default=str) + '\n')
        print(json.dumps({kk: ozet.get(kk) for kk in ('kimlik', 'yol', 'karar', 'kontrol_sayisi', 'kosular')}, ensure_ascii=False, default=str))
        print(f'OZET: {self.dizin / "ozet.json"}')
        return ozet


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('kimlik', choices=KIMLIKLER)
    ap.add_argument('--fixture-dir', required=True, type=Path, help='kampanya fixtures/ dizini (R01..R07 alt dizinleri)')
    ap.add_argument('--yol', choices=('sahte', 'go', 'or'), default='sahte')
    ap.add_argument('--dakika', type=float, default=20.0, help='senaryonun ortak sure siniri (istek suresi de bu olur)')
    ap.add_argument('--tavan', type=float, default=0.0, help='tipli istegin sabit operator toplami (USD); sahte yolda anlamsiz')
    ap.add_argument('--cikti', type=Path, default=None, help='kanit dizini (varsayilan CADRE_HOME/reports/test-arsivi/<R>-<yol>-<damga>)')
    ap.add_argument('--kip', default='', help='sahte yolda olumsuz kip (kabul_sahte.py)')
    ap.add_argument('--hash-kontrol-yok', action='store_true', help='fixture-manifest.json dogrulamasini atla (yalniz gelistirme)')
    ap.add_argument('--paket-tavan', type=float, default=2.0, help='butun paketin ortak tavani (USD); defter <cikti>/../kabul-paketi-pay.json')
    ap.add_argument('--yalniz-oneri', action='store_true', help='istek + motor izin onerisi; onay/cagri yok (fizibilite, model/sure/tavan)')
    ap.add_argument('--yukumluluk-iadesi', action='store_true',
                    help='terminal, sureci kalmamis ve makbuzu dogrulanmis istegin kullanilmayan payini ortak paket payina bir kez iade eder')
    a = ap.parse_args()
    if a.yol != 'sahte' and a.kip:
        ap.error('--kip yalniz sahte yolda kullanilir')
    kesme_kur()
    p = Paket(a.kimlik, a.fixture_dir.resolve(), a.dakika, a.tavan, a.yol, a.cikti, a.kip, hash_kontrol=not a.hash_kontrol_yok,
              paket_tavan=a.paket_tavan, yalniz_oneri=a.yalniz_oneri, yukumluluk_iadesi=a.yukumluluk_iadesi)
    p.olay(f'{a.kimlik} basladi: yol={a.yol} dakika={a.dakika} tavan={a.tavan} paket_tavan={a.paket_tavan} motor={p.root} ev={HOME} '
           f'zincir={sorted(p.zincir)}')
    try:
        sonuc = getattr(p, a.kimlik.lower())()
    except OneriDurusu as durus:
        sonuc = {'karar': 'ONERI', 'oneri': durus.oneri, 'oneriler': p.oneriler, 'neden': 'yalniz oneri: onay verilmedi, cagri yapilmadi'}
        p.olay('yalniz oneri alindi; onay/cagri yok')
    except KeyboardInterrupt as kesme:
        kesmeleri_ertele()
        sonuc = {'karar': 'DURDURULDU', 'neden': f"operator kesmesi ({kesme.args[0] if kesme.args else 'SIGINT'}): kosular motorun iptal yoluyla kapatildi"}
        p.olay(f"KESME: {sonuc['neden']}")
    except Exception as exc:  # noqa: BLE001 - a driver error is recorded too and produces no PASS
        sonuc = {'karar': 'HATA', 'neden': f'{type(exc).__name__}: {exc}'}
        p.olay(f'HATA: {sonuc["neden"]}')
    sonuc['kapanis'] = p.kapat()
    if p.iade:
        sonuc['paket_iadesi'] = p.iade_denetle()
    ozet = p.yaz(sonuc)
    return 0 if ozet['karar'] in ('PASS', 'LOCAL_PASS', 'ONERI') else 130 if ozet['karar'] == 'DURDURULDU' else 1


if __name__ == '__main__':
    raise SystemExit(main())
