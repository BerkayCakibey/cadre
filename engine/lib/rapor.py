#!/usr/bin/env python3
"""Report/analysis mode (G-R1..G-R7): a report is a set of evidenced claims, not prose. Single owner: evidence appendix, deterministic report QA,
judge probe, plan rule and report-writer lane definition; no model call, evidence commands are read-only."""

from __future__ import annotations

import json
import os
import pathlib
import random
import re
import shutil
import signal
import subprocess
import sys
import time
from typing import Any, Callable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from kosu import rapor_beyani as beyan  # noqa: E402,F401  (the single definition of the mode is in the acceptance record)

KANIT_TURLERI = ("dosya", "komut", "sorgu", "url", "bug")
BEKLENEN_ZORUNLU = ("dosya", "komut", "sorgu", "bug")
KIMLIK_DESEN = r"K(?:-[A-Za-z]{1,8}-?)?\d{1,4}"   # B224: lane-prefixed (K-tz1); B236: prefix-dash-number (K-satir-1) is valid too, K-1 is not
KIMLIK = re.compile(KIMLIK_DESEN)
def kimlik_sirasi(kimlik: str) -> tuple[str, int]:
    """B229: evidence id order (prefix, number) from one place; a prefixed id (K-u1) is sorted too, int(id[1:]) would crash."""
    m = re.fullmatch(r"(K(?:-[A-Za-z]{1,8}-?)?)(\d{1,4})", kimlik or "")
    return (m.group(1), int(m.group(2))) if m else (kimlik or "", 0)


REF = re.compile(rf"(?<![\w-]){KIMLIK_DESEN}(?![\w-])")   # B235: every evidence id in a sentence/table row is a citation ([K1], (K4, K6), K1..K4, bare K-dn5)
YOKLAMA_SONUC = ("dogrulandi", "yanlis", "dogrulanamadi")
YOKLAMA_EN_AZ = 5
DOGRULANAMADI_ORAN = 0.4      # G-R5: if more than this cannot be verified the report is not proven
BUG_TAVAN = 10
BUYUK_DOSYA = 20 * 1024 * 1024
SATIR_ARALIGI_TAVANI = 200   # at most this many lines in one file evidence (inclusive)
HASSAS_DIZIN = (".ssh", ".aws", ".gnupg", ".docker", ".kube", ".config/opencode", ".local/share/opencode", "Library/Keychains")
HASSAS_AD = re.compile(r"(^|/)(\.env(\.[^/]*)?|[^/]*\.pem|[^/]*\.key|id_rsa[^/]*|id_ed25519[^/]*|credentials[^/]*|[^/]*-anahtar[^/]*)$")
TR = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
BASLIK = re.compile(r"^\s{0,3}#{1,6}(\s|$)")
TABLO_AYRAC = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
DOGRULANMADI = re.compile(r"dogrulanmadi|dogrulanamadi|dogrulanamaz|veri yetersiz")
SIYIR = [re.compile(p) for p in (
    r"`[^`]*`", r"https?://\S+", r"\]\([^)]*\)",
    r"[\w./~-]*\w\.[a-z][a-z0-9]{0,5}(?::\d+(?:-\d+)?)?\b",                   # file path (+line)
    r"\b\d{4}-\d{2}-\d{2}(?:[t ]\d{2}:\d{2}(?::\d{2})?)?\b",                  # ISO date
    r"\b(?:0?[1-9]|[12]\d|3[01])[./](?:0[1-9]|1[0-2])(?:[./](?:\d{4}|\d{2}))?\b",   # day.month(.year)
    r"\b\d{1,2}:\d{2}(?::\d{2})?\b", r"\bv?\d+(?:\.\d+){2,}\b",
    r"\b(?=\w*[^\W\d_])(?=\w*\d)\w+\b",                                       # letter+digit id (R1, B203, en-v30)
    r"\b[a-z]+(?:[-_][a-z]+)*[-_]\d+\s*(?:\.{2,3}|\u2026|-)\s*\d+\b",                 # id range (oss-001...004)
    r"\b[a-z]+(?:[-_][a-z]+)*[-_]\d+\b",                                        # B227: ticket/standard id (ops-001, utf-8)
    r"(?<![\w&#])#\d{1,5}\b",
    r"\b(?:oncelik|oneri|adim|madde|bolum|asama|faz|tur|no|sira|soru|gorev|kosu|hata|bug|bulgu|risk)\s*[:#.]?\s*\d{1,3}\b",
)]
LISTE = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+")
TARIHLI_TURLER = ("url", "sorgu")


def _tarih_hatasi(deger: Any) -> str | None:
    """ISO day (YYYY-MM-DD), cannot be in the future; a missing/invalid date is data of unknown freshness."""
    import datetime
    if not isinstance(deger, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", deger.strip()):
        return "tarih yok ya da YYYY-MM-DD degil"
    try:
        gun = datetime.date.fromisoformat(deger.strip())
    except ValueError:
        return "gecersiz takvim gunu"
    if gun > datetime.date.today():
        return "tarih gelecekte"
    return None


ANALIST_NOTU = "Analist notu (G-R7)"


def analist_notu(kip: str, neden: str = "") -> str:
    """N-REP03 (CR-30): without a strong second model a single analyst is stated explicitly in the report; the reconciliation shows on the same line."""
    if kip == "mutabakat":
        return f"{ANALIST_NOTU}: iki bagimsiz analist ve uzlastirici; ayrisan iddialar kaynagindan dogrulandi."
    return f"{ANALIST_NOTU}: tek analist; ikinci bagimsiz analist calismadi ({neden or 'ikinci guclu model yok'})."


def analist_kaydi(run_dir: pathlib.Path, wt: pathlib.Path, kip: str, modeller: list[str], neden: str = "") -> dict[str, Any]:
    """CR-30: the analyst setup is written to the run record (`rapor.analist`) and as one line at the end of the report file; rewriting does not duplicate it."""
    from kosu import FileLock, atomic_json
    if kip not in ("tek", "mutabakat"):
        raise ValueError("analist-kip-gecersiz")
    kayit = {"kip": kip, "birinci": modeller[0] if modeller else "", "ikinci": modeller[1] if len(modeller) > 1 and modeller[1] else None,
             "uzlastirici": modeller[2] if len(modeller) > 2 and modeller[2] else None, "neden": neden, "ts": time.time()}
    run_dir = pathlib.Path(run_dir)
    with FileLock(run_dir / ".record.lock"):
        record = json.loads((run_dir / "run.json").read_text())
        beyan_ = record.get("rapor") or {}
        if not beyan_.get("yol"):
            raise ValueError("rapor-beyani-yok")
        beyan_["analist"] = kayit
        record["rapor"] = beyan_
        atomic_json(run_dir / "run.json", record)
    yol = pathlib.Path(wt) / beyan_["yol"]
    if yol.is_file():
        satirlar = [s for s in yol.read_text(errors="replace").rstrip("\n").splitlines() if not s.startswith(ANALIST_NOTU)]
        yol.write_text("\n".join([*satirlar, "", analist_notu(kip, neden)]).strip("\n") + "\n")
    return kayit


def analist_notu_uygula(wt: pathlib.Path, beyan_: dict[str, Any]) -> bool:
    """CR-30: the note belongs to the engine; even if the correction round rewrites the report from scratch, the same line is added again before QA (idempotent)."""
    analist = beyan_.get("analist") if isinstance(beyan_.get("analist"), dict) else None
    if not analist or analist.get("kip") not in ("tek", "mutabakat") or not beyan_.get("yol"):
        return False
    yol = pathlib.Path(wt) / beyan_["yol"]
    if not yol.is_file():
        return False
    metin = yol.read_text(errors="replace")
    satir = analist_notu(analist["kip"], analist.get("neden") or "")
    if satir in metin.splitlines():
        return False
    satirlar = [s for s in metin.rstrip("\n").splitlines() if not s.startswith(ANALIST_NOTU)]
    yol.write_text("\n".join([*satirlar, "", satir]).strip("\n") + "\n")
    return True


def analist_kontrol(beyan_: dict[str, Any], metin: str) -> list[str]:
    analist = beyan_.get("analist") if isinstance(beyan_.get("analist"), dict) else None
    if not analist or analist.get("kip") not in ("tek", "mutabakat"):
        return []
    satirlar = [s for s in metin.splitlines() if s.startswith(ANALIST_NOTU)]
    if not satirlar:
        return [f"analist-notu-eksik: rapor '{ANALIST_NOTU}' satiri tasimiyor (N-REP03; kayitli kip: {analist['kip']})"]
    beklenen = "iki bagimsiz analist" if analist["kip"] == "mutabakat" else "tek analist"
    if not any(beklenen in s for s in satirlar):
        return [f"analist-notu-celisiyor: kayitli kip '{analist['kip']}' ile rapor satiri uyusmuyor"]
    return []


def tarih_ve_celiski(kayitlar: list[dict[str, Any]], metin: str) -> list[str]:
    """N-REP01: url/query evidence cannot be undated; the same source cannot carry different expected values for the same claim (FIX-20: different claims are separate evidence); with external data the report has a data date line."""
    bulgular = []
    for k in kayitlar:
        if k["tur"] in TARIHLI_TURLER:
            hata = _tarih_hatasi(k.get("tarih"))
            if hata:
                bulgular.append(f"kaynak-tarihi: {k['id']}: {hata}")
    gruplar: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for k in kayitlar:
        if k["tur"] in TARIHLI_TURLER:
            gruplar.setdefault((k["tur"], _bosluk(k["kaynak"]), _bosluk(str(k.get("iddia") or ""))), []).append(k)
    for (tur, _kaynak, _iddia), grup in gruplar.items():
        if len({_bosluk(k.get("beklenen") or "") for k in grup}) > 1:
            bulgular.append(f"celisen-kanit: {', '.join(k['id'] for k in grup)}: ayni {tur} kaynagi ayni iddia icin farkli beklenen tasiyor; "
                            "celiskiyi raporda acikla ya da kaniti ayir")
    if metin.strip() and any(k["tur"] in TARIHLI_TURLER for k in kayitlar) and not re.search(r"(veri|kaynak) tarihi", _ascii(metin)):
        bulgular.append("veri-tarihi-yok: dis kaynak/sorgu kaniti var ama raporda 'Veri tarihi' ya da 'Kaynak tarihi' satiri yok (N-REP01)")
    return bulgular



def _ascii(metin: str) -> str:
    return metin.translate(TR).lower()


def _bosluk(metin: str) -> str:
    return " ".join(metin.split())


def kayit_beyani(run_dir: str | None = None) -> dict[str, Any] | None:
    try:
        return json.loads((pathlib.Path(run_dir or os.environ["CADRE_RUN_DIR"]) / "run.json").read_text()).get("rapor") or None
    except (KeyError, OSError, ValueError, AttributeError):
        return None


def kanit_oku(metin: str) -> tuple[list[dict[str, Any]], list[str]]:
    """G-R2: every line is a claim {id, iddia, tur, kaynak, beklenen}; a broken line stays out of the record and becomes a finding."""
    kayitlar, bulgular, gorulen = [], [], set()
    for no, satir in enumerate((metin or "").splitlines(), 1):
        if not satir.strip():
            continue
        try:
            k = json.loads(satir)
        except ValueError:
            bulgular.append(f"kanit-bozuk: satir {no}: JSON degil")
            continue
        kimlik = k.get("id") if isinstance(k, dict) else None
        if not isinstance(kimlik, str) or not KIMLIK.fullmatch(kimlik):
            bulgular.append(f"kanit-bozuk: satir {no}: kimlik K<sayi> ya da serit onekli K-<ad><sayi> biciminde degil")
            continue
        if kimlik in gorulen:
            bulgular.append(f"kanit-bozuk: satir {no}: {kimlik} tekrar")
            continue
        if k.get("tur") not in KANIT_TURLERI:
            bulgular.append(f"kanit-bozuk: satir {no}: {kimlik} bilinmeyen tur {k.get('tur')!r} ({'|'.join(KANIT_TURLERI)})")
            continue
        alanlar = ("iddia", "kaynak", "beklenen") if k["tur"] in BEKLENEN_ZORUNLU else ("iddia", "kaynak")
        eksik = [a for a in alanlar if not isinstance(k.get(a), str) or not k[a].strip()]
        if eksik:
            bulgular.append(f"kanit-bozuk: satir {no}: {kimlik} eksik alan: {', '.join(eksik)}")
            continue
        gorulen.add(kimlik)
        kayitlar.append(k)
    return kayitlar, bulgular


def kanit_idleri(wt, beyan_: dict[str, Any]) -> list[str]:
    try:
        return [k["id"] for k in kanit_oku((pathlib.Path(wt) / beyan_["kanit"]).read_text(errors="replace"))[0]]
    except OSError:
        return []


def kanitsiz_sayilar(metin: str) -> list[tuple[int, str]]:
    """G-R3: every sentence or table row containing a number carries an evidence id or says 'dogrulanmadi' explicitly. Dates, times, versions,
    ids, file:line, inline code, headings, list markers and table headers are not number claims. Unit: a sentence in prose
    (wrapped lines join inside a paragraph/item, B227), a row in a table. Result: the line where the sentence starts."""
    satirlar = (metin or "").splitlines()
    sonuc, kod, blok = [], False, []

    def bitir():
        if blok:
            sonuc.extend(_blok_kanitsiz(blok))
            blok.clear()

    for i, satir in enumerate(satirlar):
        yalin = satir.strip()
        if yalin.startswith(("```", "~~~")):
            bitir()
            kod = not kod
            continue
        if (kod or not yalin or BASLIK.match(satir) or TABLO_AYRAC.match(satir) or yalin.startswith("<!--")
                or (yalin.startswith("|") and i + 1 < len(satirlar) and TABLO_AYRAC.match(satirlar[i + 1]))):
            bitir()
            continue
        if yalin.startswith("|"):
            bitir()
            if not REF.search(satir) and _sayi_iddiasi(satir, tablo=True):
                sonuc.append((i + 1, satir))
            continue
        if LISTE.match(satir):
            bitir()
        blok.append((i + 1, satir))
    bitir()
    return sonuc


CUMLE_SONU = re.compile(r"(?<=[.!?])\s+(?=[A-ZÇĞİÖŞÜ0-9\"“(\[*`])")
KISALTMA = re.compile(r"(?:^|[\s(\[\"“])(?:örn|ör|orn|vb|vs|bkz|yak|krş|krs|vd|e\.g|i\.e)\.$", re.I)


def cumle_sonlari(metin: str) -> list[re.Match[str]]:
    return [m for m in CUMLE_SONU.finditer(metin) if not KISALTMA.search(metin[max(0, m.start() - 8):m.start()])]


INCELEME_BOLUMLERI = ('Bulgular', 'Belirsizlikler', 'Yontem')
BOS_OLABILIR_BOLUMLER = frozenset({'Belirsizlikler'})


class RaporBicimHatasi(ValueError):
    def __init__(self, hatalar: list[str]):
        super().__init__('; '.join(hatalar))
        self.hatalar = hatalar


def inceleme_metni(icerik: Any, kanitlar: list[dict[str, Any]]) -> str:
    """N-REP01: the writer chooses the sentence–evidence link; the engine only builds the display and produces no evidence or claim."""
    if isinstance(icerik, dict) and all(isinstance(k, str) for k in icerik):
        normalized = {k.translate(TR): v for k, v in icerik.items()}
        if len(normalized) != len(icerik):
            raise RaporBicimHatasi(['report ayni bolumu farkli yazimlarla tekrarlayamaz'])
        icerik = normalized
    if not isinstance(icerik, dict) or set(icerik) != set(INCELEME_BOLUMLERI):
        raise RaporBicimHatasi(['report Bulgular, Belirsizlikler ve Yontem listelerini tasiyan bir nesne olmali'])
    bilinen = {k['id'] for k in kanitlar if isinstance(k, dict) and isinstance(k.get('id'), str)
               and KIMLIK.fullmatch(k['id'])}
    parcalar: list[str] = []
    hatalar: list[str] = []
    for bolum in INCELEME_BOLUMLERI:
        ogeler = icerik[bolum]
        if not isinstance(ogeler, list) or (not ogeler and bolum not in BOS_OLABILIR_BOLUMLER):
            hatalar.append(f'{bolum}: en az bir {{text, evidence_ids}} ogesi gerekli')
            continue
        parcalar.append('# ' + bolum)
        for no, oge in enumerate(ogeler, 1):
            yer = f'{bolum}[{no}]'
            if not isinstance(oge, dict) or set(oge) != {'text', 'evidence_ids'}:
                hatalar.append(f'{yer}: yalniz text ve evidence_ids alanlari gerekli')
                continue
            metin, kimlikler = oge['text'], oge['evidence_ids']
            oge_hatasi = len(hatalar)
            if not isinstance(metin, str) or not metin.strip() or metin.splitlines() != [metin]:
                hatalar.append(f'{yer}: text bos olmayan tek satir olmali')
            elif (BASLIK.match(metin.strip()) or LISTE.match(metin.strip())
                  or metin.strip().startswith(('|', '```', '~~~', '<!--')) or REF.search(metin) or cumle_sonlari(metin.strip())):
                hatalar.append(f'{yer}: text tek cumle olmali; baslik, liste ve atiflari motor ekler')
            if (not isinstance(kimlikler, list) or any(not isinstance(k, str) or k not in bilinen for k in kimlikler)
                    or len(set(kimlikler)) != len(kimlikler)):
                hatalar.append(f'{yer}: evidence_ids yalniz kanit ekindeki farkli kimlikleri tasimali')
            elif bolum == 'Bulgular' and not kimlikler:
                hatalar.append(f'{yer}: her bulgu kendi kanit kimligini tasimali')
            if len(hatalar) > oge_hatasi:
                continue
            metin = metin.strip()
            son = metin[-1] if metin[-1] in '.!?' else '.'
            govde = metin[:-1] if metin[-1] in '.!?' else metin
            atif = ' ' + ''.join('[' + k + ']' for k in kimlikler) if kimlikler else ''
            parcalar.append('- ' + govde + atif + son)
        parcalar.append('')
    if hatalar:
        raise RaporBicimHatasi(hatalar)
    return '\n'.join(parcalar)


def _blok_kanitsiz(blok: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Paragraph or list item: lines join and are checked sentence by sentence; the first line of the sentence is reported."""
    parcalar, baslar, konum = [], [], 0
    for j, (no, satir) in enumerate(blok):
        metin = LISTE.sub("", satir, count=1) if j == 0 else satir
        baslar.append((konum, no, satir))
        parcalar.append(metin.strip())
        konum += len(parcalar[-1]) + 1
    birlesik = " ".join(parcalar)
    sonuc, bas = [], 0
    for cumle_sonu in [m.start() for m in cumle_sonlari(birlesik)] + [len(birlesik)]:
        cumle = birlesik[bas:cumle_sonu]
        if cumle.strip() and not REF.search(cumle) and _sayi_iddiasi(cumle):
            no, satir = next((n, st) for k, n, st in reversed(baslar) if k <= bas)
            if not sonuc or sonuc[-1][0] != no:
                sonuc.append((no, satir))
        bas = cumle_sonu + 1
    return sonuc


def _sayi_iddiasi(parca: str, tablo: bool = False) -> bool:
    f = _ascii(parca)
    if DOGRULANMADI.search(f):
        return False
    if tablo:
        hucreler = f.strip().strip("|").split("|")
        if hucreler and re.fullmatch(r"\s*\d{1,3}\s*", hucreler[0]):   # order column
            f = "|".join(hucreler[1:])
    for kalip in SIYIR:
        f = kalip.sub(" ", f)
    return bool(re.search(r"\d", f))


def _hassas(yol: pathlib.Path) -> bool:
    try:
        gercek = yol.expanduser().resolve()
    except (OSError, RuntimeError):
        return True
    ev = pathlib.Path.home().resolve()
    return any(gercek == ev / d or (ev / d) in gercek.parents for d in HASSAS_DIZIN) or bool(HASSAS_AD.search(gercek.as_posix()))


def _cozumle(wt, ad: str) -> pathlib.Path:
    yol = pathlib.Path(os.path.expanduser(ad))
    return (yol if yol.is_absolute() else pathlib.Path(wt) / yol).resolve()


def _dosya_kanit(wt, k: dict[str, Any], taban_ayni: bool = False) -> str | None:
    m = re.match(r"^(?P<yol>.+?)(?::(?P<bas>\d+)(?:-(?P<son>\d+))?)?$", k["kaynak"].strip())
    ad = m.group("yol")
    yol = pathlib.Path(os.path.expanduser(ad))
    yol = yol if yol.is_absolute() else pathlib.Path(wt) / yol
    if _hassas(yol):
        return f"hassas yol okunmaz ({ad})"
    kok = pathlib.Path(wt).resolve()
    try:   # B267: places the writer can write itself (outside the tree, files git ignores/does not track) cannot be evidence
        goreli = yol.resolve().relative_to(kok)
    except ValueError:
        return f"calisma agaci disinda: {ad}"
    if not yol.is_file():
        return f"dosya yok: {ad}"
    git = lambda *a: subprocess.run(["git", "-C", str(kok), *a], capture_output=True, text=True, timeout=60).returncode
    if git("ls-files", "--error-unmatch", "--", str(goreli)) != 0:
        return f"git izlemiyor: {ad} (izlenmeyen ya da yok sayilan dosya kanit olamaz)"
    if not taban_ayni and git("diff", "--quiet", "HEAD", "--", str(goreli)) != 0:
        return f"is sirasinda degismis: {ad}"
    if m.group("bas"):
        bas, son = int(m.group("bas")), int(m.group("son") or m.group("bas"))
        if bas < 1 or son < bas or son - bas + 1 > SATIR_ARALIGI_TAVANI:   # B298: inclusive line count
            return f"gecersiz satir araligi: {k['kaynak']}"
        parca = []
        with yol.open(errors="replace") as f:
            for no, satir in enumerate(f, 1):
                if no >= bas:
                    parca.append(satir)
                if no >= son:
                    break
        if len(parca) < son - bas + 1:
            return f"satir yok: {k['kaynak']}"
        metin, yer = "".join(parca), k["kaynak"]
    else:
        if yol.stat().st_size > BUYUK_DOSYA:
            return f"dosya cok buyuk, satir belirt: {ad}"
        metin, yer = yol.read_text(errors="replace"), ad
    if _bosluk(k["beklenen"]) not in _bosluk(metin):
        ipucu = ""
        aranan = _bosluk(k["beklenen"])
        if m.group("bas") and aranan and yol.stat().st_size <= 5_000_000:
            with yol.open(errors="replace") as f:
                satirlar = [no for no, satir in enumerate(f, 1) if aranan in _bosluk(satir)][:3]
            if satirlar:
                ipucu = f" (alinti dosyada satir {', '.join(map(str, satirlar))}; kaynagi bu numarayla yaz)"
        return f"alinti {yer} icinde yok: {k['beklenen'][:80]!r}{ipucu}"
    return None


def _sbpl(metin: str) -> str:
    return json.dumps(str(metin))


def _profil(wt, gecici, kaynak_salt_okur: bool = False) -> str:
    """Read-only evidence sandbox: no network; writes only to the working tree + a temporary directory; sensitive directories/files are not read."""
    ev = pathlib.Path.home().resolve()
    okuma = " ".join(f"(subpath {_sbpl(ev / d)})" for d in HASSAS_DIZIN)
    agac = "" if kaynak_salt_okur else f"(subpath {_sbpl(pathlib.Path(wt).resolve())}) "
    return ("(version 1)(allow default)(deny network*)(deny file-write* (subpath \"/\"))"
            f"(allow file-write* {agac}(subpath {_sbpl(pathlib.Path(gecici).resolve())})"
            " (literal \"/dev/null\") (literal \"/dev/tty\") (literal \"/dev/dtracehelper\") (regex #\"^/dev/fd/\"))"
            f"(deny file-read* {okuma} (regex #\"/\\.env(\\.[^/]*)?$\") (regex #\"\\.(pem|key)$\")"
            " (regex #\"/id_(rsa|ed25519)[^/]*$\") (regex #\"/credentials[^/]*$\") (regex #\"-anahtar[^/]*$\"))")


def _bekle(proc: subprocess.Popen, sure: float) -> int:
    try:
        return proc.wait(timeout=sure)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait(timeout=10)
        raise


KOMUT_CIKTI_TAVANI = 1_000_000


def komut_kos(komut: str, wt, gecici, sure: float, bekle: Callable | None = None,
              kaynak_salt_okur: bool = False) -> tuple[int | None, str]:
    """(rc, output); rc None = could not run/timeout (the output says why). The environment is built from an allow-list (no key leaks)."""
    sandbox = shutil.which("sandbox-exec") or "/usr/bin/sandbox-exec"
    if not os.access(sandbox, os.X_OK):
        return None, "sandbox-exec yok: kanit komutu kosulmadi (fail-closed)"
    g = pathlib.Path(gecici)
    g.mkdir(parents=True, exist_ok=True)
    sira = len(list(g.glob("komut-*.sh")))
    betik = g / f"komut-{sira}.sh"
    betik.write_text(komut + "\n")
    env = {a: os.environ[a] for a in ("PATH", "HOME", "LANG", "LC_ALL", "USER", "LOGNAME", "SHELL") if a in os.environ}
    env.update(TMPDIR=str(g.resolve()), PYTHONDONTWRITEBYTECODE="1", TERM="dumb",
               UV_CACHE_DIR=str(g.resolve() / "uv-cache"), UV_OFFLINE="1")   # B232: `uv run` cannot write to ~/.cache/uv in the sandbox; the network is closed anyway
    import threading
    proc = subprocess.Popen([sandbox, "-p", _profil(wt, g, kaynak_salt_okur), "/bin/bash", str(betik)], cwd=str(wt), env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    tampon = bytearray()
    dur = threading.Event()
    fd = proc.stdout.fileno()
    os.set_blocking(fd, False)

    def oku() -> bool | None:
        try:
            parca = os.read(fd, 65536)
        except BlockingIOError:
            return None
        if len(tampon) < KOMUT_CIKTI_TAVANI:
            tampon.extend(parca[:KOMUT_CIKTI_TAVANI - len(tampon)])
        return bool(parca)

    def topla() -> None:   # B298: output above the cap is not written to disk either, it is read and discarded (it used to flow unbounded into a file)
        while not dur.is_set():
            okundu = oku()
            if okundu is False:
                break
            if okundu is None:
                dur.wait(.01)
    okuyucu = threading.Thread(target=topla, daemon=True)
    okuyucu.start()
    try:
        rc = (bekle or _bekle)(proc, sure)
    except subprocess.TimeoutExpired:
        return None, f"zaman asimi ({int(sure)} sn)"
    finally:
        # B306: when the main command ends, also close children that inherited stdout; no close/join waiting for EOF.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):   # on timeout the group may already have been closed by _bekle (EPERM)
            pass
        dur.set()
        okuyucu.join()
        for _ in range((KOMUT_CIKTI_TAVANI + 65535) // 65536):
            if len(tampon) >= KOMUT_CIKTI_TAVANI or not oku():
                break
        proc.stdout.close()
        proc.wait(timeout=1)
    return rc, bytes(tampon).decode(errors="replace")


def degisen_urun(wt, source_sha: str | None, haric=()) -> list[str]:
    from sonuc import ONBELLEK_ARTIGI

    def git(*args):
        return subprocess.run(["git", "-C", str(wt), *args], capture_output=True, text=True, timeout=60).stdout
    adlar = set(git("diff", "--name-only", "-z", "HEAD", "--", ".", *haric).split("\0"))
    adlar |= set(git("ls-files", "--others", "--exclude-standard", "-z", "--", ".", *haric).split("\0"))
    if source_sha:
        adlar |= set(git("diff", "--name-only", "-z", source_sha, "--", ".", *haric).split("\0"))
    return sorted(a for a in adlar if a and not ONBELLEK_ARTIGI.search(a))


KANIT_SINIRI = re.compile(r'en\s+(?:fazla|[çc]ok)\s+(\d{1,3})\s+(?:do[ğg]rudan\s+)?dosya:sat[ıi]r\s+(?:al[ıi]nt|kan[ıi]t)', re.I)


def kanit_siniri(*metinler: str) -> int | None:
    sayilar = [int(m.group(1)) for metin in metinler for m in KANIT_SINIRI.finditer(metin or '')]
    return min(sayilar) if sayilar else None


KOMUT_KANITI_YASAK = re.compile(r'komut\s+kanit\w*[^.;\n]{0,160}?\b(?:verilmez|verme|yok|yasak\w*|kullanilmaz|kullanma|uretilmez)\b')
YURUTULEN_KANIT_TURLERI = frozenset({"komut", "sorgu", "bug"})


def kanit_turu_yasaklari(*metinler: str) -> frozenset[str]:
    return YURUTULEN_KANIT_TURLERI if any(KOMUT_KANITI_YASAK.search(_ascii(m or '')) for m in metinler) else frozenset()


def _bolum_icinde(kaynak: str, araliklar: list[tuple[int, int]]) -> bool:
    m = re.search(r":(\d+)(?:-(\d+))?$", kaynak.strip())
    return bool(m) and any(a <= int(m[1]) <= int(m[2] or m[1]) <= b for a, b in araliklar)


def rapor_qa(wt, beyan_: dict[str, Any], source_sha: str | None, gecici, tohum: str = "", haric=(),
             bekle: Callable | None = None, *, metinler: tuple[str, str] | None = None,
             baslangic: dict[str, str] | None = None, kaynak_salt_okur: bool = False,
             kanit_siniri: int | None = None, kaynak_yollari: list[pathlib.Path] | None = None,
             kaynak_bolumleri: dict[pathlib.Path, list[tuple[int, int]]] | None = None,
             yasak_turler: frozenset[str] = frozenset()) -> dict[str, Any]:
    """G-R3/G-R4: report + evidence appendix are checked mechanically; a single finding is red and goes to the correction round as a concrete list."""
    wt = pathlib.Path(wt)
    if kaynak_yollari is not None:
        kaynak_yollari = [pathlib.Path(p).resolve() for p in kaynak_yollari]
    bolumler = {pathlib.Path(p).resolve(): list(araliklar) for p, araliklar in (kaynak_bolumleri or {}).items()}
    yol, kanit = beyan_["yol"], beyan_["kanit"]
    bulgular: list[str] = []
    metin = metinler[0] if metinler is not None else ((wt / yol).read_text(errors="replace") if (wt / yol).is_file() else "")
    kanit_metni = metinler[1] if metinler is not None else ((wt / kanit).read_text(errors="replace") if (wt / kanit).is_file() else "")
    if not metin.strip():
        bulgular.append(f"rapor-yok: {yol} yok ya da bos")
    if not kanit_metni.strip():
        bulgular.append(f"kanit-yok: {kanit} yok ya da bos (G-R2)")
    kayitlar, bozuk = kanit_oku(kanit_metni)
    bulgular += bozuk
    dosya_sayisi = sum(1 for k in kayitlar if k['tur'] == 'dosya')
    if kanit_siniri is not None and dosya_sayisi > kanit_siniri:
        bulgular.append(f'kanit-siniri: istek en fazla {kanit_siniri} dosya:satir kaniti izin verir, {dosya_sayisi} verildi; '
                        'iddialari bu sinira gore birlestir ya da cikar')
    if metin.strip():
        basliklar = [_ascii(s) for s in metin.splitlines() if BASLIK.match(s)]
        bulgular += [f"bolum-eksik: {b}" for b in beyan_.get("bolumler") or [] if not any(_ascii(b) in s for s in basliklar)]
        eksik = kanitsiz_sayilar(metin)
        bulgular += [f"kanitsiz-sayi: {yol}:{no}: {satir.strip()[:160]}" for no, satir in eksik[:20]]
        if len(eksik) > 20:
            bulgular.append(f"kanitsiz-sayi: toplam {len(eksik)} satir (ilk 20 listelendi)")
        bilinen = {k["id"] for k in kayitlar}
        kullanilan = set(KIMLIK.findall(" ".join(REF.findall(metin))))
        bilinmeyen = sorted(kullanilan - bilinen, key=kimlik_sirasi)
        if bilinmeyen:
            bulgular.append(f"bilinmeyen-kanit: {', '.join(bilinmeyen[:20])} kanit ekinde yok")
    taban_ayni = False
    if metinler is not None and baslangic is not None:
        from kosu import plan_product
        taban_ayni = plan_product(wt) == baslangic
    oz = set() if metinler is not None else {(wt / beyan_["yol"]).resolve(), (wt / beyan_["kanit"]).resolve()}   # B261: resolved path (absolute/symlink included)
    for k in kayitlar:
        dosya = _cozumle(wt, re.sub(r":\d+(?:-\d+)?$", "", k["kaynak"].strip())) if k['tur'] == 'dosya' else None
        if (dosya is not None and kaynak_yollari is not None
                and not any(dosya == p or (p.is_dir() and dosya.is_relative_to(p)) for p in kaynak_yollari)):
            bulgular.append(f"dosya-kapsam: {k['id']}: {k['kaynak']} kabul edilen inceleme planinin okuma kapsaminda degil")
        elif dosya is not None and dosya in bolumler and not _bolum_icinde(k["kaynak"], bolumler[dosya]):
            izinli = ", ".join(f"{a}-{b}" for a, b in bolumler[dosya])
            bulgular.append(f"bolum-kapsam: {k['id']}: {k['kaynak']} secili kaynak bolumu disinda; izinli satirlar {izinli}")
        elif dosya in oz:
            bulgular.append(f"oz-atif: {k['id']}: rapor ya da kanit eki kendini kanitlayamaz; sayim icin yeniden kosulabilir komut kaniti kullan (B228)")
        elif k["tur"] == "dosya":
            hata = _dosya_kanit(wt, k, taban_ayni)
            if hata:
                bulgular.append(f"dosya-kaynak: {k['id']}: {hata}")
        elif k["tur"] == "url" and not re.fullmatch(r"https?://\S+", k["kaynak"].strip()):
            bulgular.append(f"url-kaynak: {k['id']}: gecersiz URL")
    bulgular += tarih_ve_celiski(kayitlar, metin)
    bulgular += analist_kontrol(beyan_, metin)
    yasakli = [k for k in kayitlar if k["tur"] in yasak_turler]
    if yasakli:
        bulgular.append(f"kanit-turu-yasak: {', '.join(k['id'] for k in yasakli[:20])}: istek "
                        f"{'/'.join(sorted({k['tur'] for k in yasakli}))} kanitini yasakliyor; yurutulmedi, kaldir ya da dosya:satir kanitina cevir")
    sure = float(os.environ.get("CADRE_RAPOR_KOMUT_SN", "120"))
    komutlar = sorted((k for k in kayitlar if k["tur"] in ("komut", "sorgu") and k["tur"] not in yasak_turler),
                      key=lambda k: kimlik_sirasi(k["id"]))
    ornek = sorted(random.Random(tohum).sample(komutlar, min(int(os.environ.get("CADRE_RAPOR_KOMUT_ORNEK", "5")), len(komutlar))),
                   key=lambda k: kimlik_sirasi(k["id"]))
    for k in ornek:
        rc, cikti = komut_kos(k["kaynak"], wt, gecici, sure, bekle, kaynak_salt_okur)
        if rc is None:
            bulgular.append(f"komut-kaynak: {k['id']}: {cikti}")
        elif _bosluk(k["beklenen"]) not in _bosluk(cikti):
            bulgular.append(f"komut-kaynak: {k['id']}: beklenen {k['beklenen'][:80]!r} ciktida yok (rc={rc}; cikti: {_bosluk(cikti)[:200]!r})")
    buglar = [k for k in kayitlar if k["tur"] == "bug" and "bug" not in yasak_turler]
    bug_tavan = int(os.environ.get("CADRE_RAPOR_BUG_TAVAN", BUG_TAVAN))
    if len(buglar) > bug_tavan:   # B262: a claim above the cap cannot silently stay unchecked
        bulgular.append(f"bug-tavan: {len(buglar)} bug iddiasi, tavan {bug_tavan} (CADRE_RAPOR_BUG_TAVAN); "
                        f"{', '.join(k['id'] for k in buglar[bug_tavan:][:20])} denetlenmedi — iddialari birlestir ya da kanittan cikar (G-R4)")
    for k in buglar[:bug_tavan]:
        rc, cikti = komut_kos(k["kaynak"], wt, gecici, sure, bekle, kaynak_salt_okur)
        if rc is None:
            bulgular.append(f"bug-yeniden: {k['id']}: {cikti}")
        elif rc == 0:
            bulgular.append(f"bug-yeniden: {k['id']}: yeniden uretim komutu basarili bitti (rc=0) — bug mevcut kodda gorunmuyor (G-R4)")
        elif _bosluk(k["beklenen"]) not in _bosluk(cikti):
            bulgular.append(f"bug-yeniden: {k['id']}: komut kirmizi ama beklenen hata {k['beklenen'][:80]!r} yok (cikti: {_bosluk(cikti)[:200]!r})")
    if metinler is not None:
        from kosu import plan_product
        if baslangic is None:
            raise ValueError('review-source-identity-required')
        disari = ['source-changed'] if plan_product(wt) != baslangic else []
    else:
        disari = [a for a in degisen_urun(wt, source_sha, haric) if a not in (yol, kanit)]
    if disari:
        bulgular.append(f"rapor-disi: {', '.join(disari[:20])} degisti; rapor isinde yalniz {yol} ve {kanit} degisebilir (G-R1)")
    return {"status": "FAIL" if bulgular else "PASS", "bulgular": bulgular,
            "sayac": {"kanit": len(kayitlar), "komut_ornek": len(ornek), "komut_toplam": len(komutlar)}}


def _yoklama_bul(raw: str, digest: str | None) -> list[Any]:
    from sonuc import _json_adaylari
    bulunan = [a["yoklama"] for a in _json_adaylari(raw or "") if isinstance(a, dict) and a.get("revision") == digest and "yoklama" in a]
    if bulunan:
        if len({json.dumps(b, sort_keys=True) for b in bulunan}) > 1:
            raise ValueError("celisen yoklama listeleri")
        return bulunan[-1]
    return [{"id": "K" + m.group(1)[1:], "sonuc": m.group(2).lower(), "kanit": m.group(3)} for m in re.finditer(
        rf"^\s*YOKLAMA\s+({KIMLIK_DESEN})\s*:\s*(dogrulandi|yanlis|dogrulanamadi)\s*[-—:]\s*(.+?)\s*$", raw or "", re.M | re.I)]


def yoklama_uygula(raw: str, review: dict[str, Any], ids: list[str] | None, en_az: int | None = None) -> dict[str, Any]:
    """G-R5: a valid review must probe at least min(5, evidence count) different, existing ids; a missing/invented probe
    is a malformed review. A single 'yanlis' or an unverifiable ratio above the threshold produces DUZELT and a `rapor_yoklama` FAIL item."""
    if ids is None or review.get("status") not in ("PASS", "FAIL"):
        return review
    from sonuc import yer_tutucu
    en_az = min(YOKLAMA_EN_AZ, len(ids)) if en_az is None else en_az
    try:
        yoklama = _yoklama_bul(raw, (review.get("revision") or {}).get("digest"))
        if not isinstance(yoklama, list):
            raise ValueError("yoklama liste degil")
        gorulen = set()
        for y in yoklama:
            kimlik = y.get("id") if isinstance(y, dict) else None
            if kimlik not in ids or kimlik in gorulen:
                raise ValueError(f"bilinmeyen ya da tekrar kanit kimligi: {kimlik!r}")
            if y.get("sonuc") not in YOKLAMA_SONUC:
                raise ValueError(f"{kimlik}: sonuc {'|'.join(YOKLAMA_SONUC)} olmali")
            if not isinstance(y.get("kanit"), str) or yer_tutucu(y["kanit"]):
                raise ValueError(f"{kimlik}: yoklama kaniti yok")
            gorulen.add(kimlik)
        if len(gorulen) < en_az:
            raise ValueError(f"yoklama eksik: {len(gorulen)} iddia < {en_az}")
    except ValueError as exc:
        return {"status": "ERROR", "revision": review.get("revision"), "reason": f"rapor yoklamasi gecersiz: {exc}"}
    sorunlu = [y for y in yoklama if y["sonuc"] == "yanlis"]
    belirsiz = [y for y in yoklama if y["sonuc"] == "dogrulanamadi"]
    sonuc = {**review, "yoklama": yoklama}
    if sorunlu or len(belirsiz) > DOGRULANAMADI_ORAN * len(yoklama):
        ozet = "; ".join(f"{y['id']} {y['sonuc']}: {y['kanit'][:200]}" for y in sorunlu + belirsiz)
        sonuc.update(status="FAIL", verdict="DUZELT",
                     acceptance=[*(review.get("acceptance") or []), {"id": "rapor_yoklama", "status": "FAIL", "evidence": ozet}])
    return sonuc


def yoklama_istemi(beyan_: dict[str, Any], ids: list[str], satir: bool = False) -> str:
    en_az = min(YOKLAMA_EN_AZ, len(ids))
    metin = (f"RAPOR YOKLAMASI (G-R5): bu is bir rapor; kanit eki {beyan_['kanit']} ({len(ids)} iddia). Katmanli orneklem sec "
             f"(sayilar, buglar, oneriler; en az {en_az} farkli kanit kimligi) ve her birini KAYNAGINDAN bagimsiz dogrula: dosyayi ac, "
             "komutu kos, sorguyu calistir. Raporun kendi cumlesine guvenme. JSON'a 'yoklama' alani ekle: "
             "[{\"id\": \"K3\", \"sonuc\": \"dogrulandi|yanlis|dogrulanamadi\", \"kanit\": \"ne gordun\"}]. "
             "Tek bir 'yanlis' varsa verdict=DUZELT.\n")
    if satir:
        metin += "Satir bicimi kullanirsan her yoklama bir satir: YOKLAMA <kimlik>: dogrulandi|yanlis|dogrulanamadi - <ne gordun>\n"
    return metin


IDDIA_DESTEK = ("tam", "kismi", "yok")
TERIM_DURUM = ("olumsuz", "baglam", "esdeger", "desteksiz")
GEREKSINIM_DURUM = ("karsilandi", "eksik", "rapor_disi")
TERIM_DISI = frozenset({"script", "scripts", "file", "module", "function", "class", "method", "line", "code", "source"})
DOSYA_ADI = re.compile(r"[\w./~-]*\w\.[A-Za-z][A-Za-z0-9]{0,5}\b")
KART_KIMLIGI = re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\b")
EK_GEREKSINIM = re.compile(r"ek-\d{1,3}")
SUREC_SOZCUKLERI = frozenset({
    'butce', 'sure', 'sabit', 'sabittir', 'kalir', 'maliyet', 'tavan', 'tavani', 'usd', 'dakika', 'dk', 'kaynak', 'degismez',
    'degismeyecek', 'degistirilmez', 'degismemeli', 'salt', 'okur', 'bagimsiz', 'son', 'temiz', 'ayri', 'inceleme', 'incelemesi',
    'hakem', 'hakemi', 'oturum', 'oturumu', 'yapilir', 'yapilacak', 'korunur', 'commit', 'push', 'yok', 've', 'veya', 'ile', 'en',
    'fazla', 'cok'})


def surec_maddesi(metin: str) -> bool:
    sozcukler = re.findall(r'[a-z0-9]+', metin.translate(TR).casefold())
    return bool(sozcukler) and all(s.isdigit() or s in SUREC_SOZCUKLERI for s in sozcukler)


MEKANIK_KANIT = (re.compile(r'en\s+(?:fazla|cok)\s+(\d{1,3})\s+(?:dogrudan\s+)?dosya:satir\s+(?:alinti|kanit)\w*'),
                 re.compile(r'komut\s+kanit\w*\s+(?:verilmez|yok|yasak\w*|kullanilmaz|uretilmez)'))
MEKANIK_DOLGU = frozenset({'kullanilir', 'kullanilmaz', 'verilir', 've', 'ile', 'asilmaz', 'gecmez'})


def mekanik_maddesi(metin: str) -> bool:
    kalan, bulundu = _ascii(metin), False
    for desen in MEKANIK_KANIT:
        kalan, adet = desen.subn(' ', kalan)
        bulundu = bulundu or bool(adet)
    return bulundu and all(s in MEKANIK_DOLGU for s in re.findall(r'[a-z0-9]+', kalan))


def mekanik_karar(tanim: dict[str, str], kanitlar: list[dict[str, Any]]) -> dict[str, Any]:
    metin = _ascii(tanim['metin'])
    dosya = sum(1 for k in kanitlar if isinstance(k, dict) and k.get('tur') == 'dosya')
    sorunlar = []
    sinir = MEKANIK_KANIT[0].search(metin)
    if sinir and dosya > int(sinir.group(1)):
        sorunlar.append(f'dosya:satir kaniti siniri asildi ({dosya}>{sinir.group(1)})')
    yurutulen = [k.get('id') for k in kanitlar if isinstance(k, dict) and k.get('tur') in YURUTULEN_KANIT_TURLERI]
    if MEKANIK_KANIT[1].search(metin) and yurutulen:
        sorunlar.append('yasak yurutulen kanit: ' + ', '.join(map(str, yurutulen)))
    return {**tanim, 'durum': 'eksik' if sorunlar else 'karsilandi', 'rapor': [],
            'gerekce': 'motor kanit eki denetimi: ' + ('; '.join(sorunlar) or 'sinir ve kanit turu uygun')}
ANLAMSAL_HAKEM_KURALI = (
    'Uc karari ayri ver. (1) yoklama: alintinin kaynakta bulunmasi. (2) iddialar: iddialar listesindeki her kanitli cumle '
    'icin destek=tam|kismi|yok; motorun okudugu kanit_metinleri cumlenin evidence_ids araliklarinda cumledeki her '
    'adimi/terimi gercekten gosteriyorsa tam. '
    'kanitta_olmayan_terimler yalniz sozcuk uyarisidir: terim kapsam dosyalarinda gecer, atif yapilan araliklarda aynen gecmez; '
    'terim_uyarilari nerede gectigini ve ozgun istekten gelip gelmedigini gosterir. Sozcugun aynen bulunmamasi tek basina '
    'destek dusurmez; sozcugun bulunmasi da o terim hakkindaki davranisi kanitlamaz. Cumleler report icinde bir kez verilir; '
    'rapor_ogeleri id=Bolum-N o bolumun N. maddesini gosterir ve evidence_ids ile kanita baglar. '
    'Her iddianin id alani ayni rapor ogesini gosterir. destek kararini cumledeki cagri, kosul, '
    'deger ve hedef iddialarinin kanit_metinleri icindeki kendi atif araliginda gercekten gorulmesine gore ver. '
    'Terim karari zorunlu degildir; uyari karar yerine gecmez, karar destek alanindadir. Bir terimin cumlede aralikta '
    'gorunmeyen bir cagri, kosul veya deger iddia ettigini saptarsan terimler:[{terim,durum:desteksiz}] yaz ve destek=kismi|yok ver; '
    'olumsuz|baglam|esdeger durumlarini istersen yazabilirsin (olumsuz: cumle yoklugunu soyler, baglam: iddia edilmeyen baglam '
    'ya da istekten alinan adlandirma, esdeger: aralik ayni davranisi baska sozcuk veya yapiyla gosterir). '
    'Uyari uretmeyen iddiayi da kendi araligiyla ayri denetle: aralik disindaki cagri/kosul, yanlis deger, kaynakta gorunmeyen '
    'uygulama ayrintisi ve eksik beklenen deger desteksizdir. (3) gereksinimler: her gereksinim icin '
    'durum=karsilandi|eksik|rapor_disi ve rapor:[rapor_ogeleri kimlikleri]. karsilandi yalniz teslim edilen rapor ogeleriyle '
    'gosterilir; kendi aciklaman, kaynakta gordugun ama raporda olmayan bilgi veya plan metni karsilama sayilmaz. '
    'karsilandi icin rapor listesi bos olamaz. Kaynaktaki bir davranisin yoklugunu isteyen icerik maddesi bu yoklugu '
    'acikca soyleyen rapor ogesiyle karsilanir, boyle oge yoksa eksik olur. '
    'Teslimin geneline ait yasak ve sunum kosullari farklidir: test sayimi veya yurutme PASS iddiasi yazilmamasi '
    'gibi bir kosulu tum metinde denetlediysen inceledigin gercek rapor ogelerinin kimliklerini rapor listesinde ver; '
    'raporun kendisi hakkinda ayrica bir yokluk cumlesi urettirme. tur=mekanik gereksinim (kanit sayisi/turu) motorun '
    'kanit eki denetimiyle kapanir; ona karar yazma. Test adedi/sayimi yasagi assert beklenen degerlerini (ornegin '
    'len(lines) == 3 ve beklenen paket sirasi) yasaklamaz; somut beklenen deger isteyen madde bu degerler raporda yoksa eksiktir. '
    'Gereksinimler celisirse ozgun istekten gelen R maddeleri plan maddelerinden onceliklidir. '
    'qa yalniz mekanik denetimdir; somut bulgunun eksikligini ya da yanlis kaynak yorumunu karsilamaz. '
    'rapor_disi yalniz motorun tur=surec isaretledigi gereksinim (butce, sure, kaynak degismezligi, bagimsiz inceleme) '
    'icindir; tur=icerik madde raporda karsilanir ya da eksik olur. '
    'Raporda olmayan istek maddesini ek-1, ek-2 kimligi ve metin alaniyla gereksinimlere ekleyebilirsin. ')


def inceleme_ogeleri(icerik: Any) -> list[dict[str, Any]]:
    if isinstance(icerik, dict):
        icerik = {k.translate(TR): v for k, v in icerik.items()}
    ogeler = []
    for bolum in INCELEME_BOLUMLERI:
        for no, oge in enumerate((icerik or {}).get(bolum) or [], 1):
            ogeler.append({'id': f'{bolum}-{no}', 'bolum': bolum, 'text': str(oge['text']).strip(),
                           'evidence_ids': list(oge['evidence_ids'])})
    return ogeler


def kabul_maddeleri(metin: str) -> list[str]:
    parcalar = (p.strip().rstrip('.!?').strip() for p in re.split(r'[;\n]+|(?<=[.!?])\s+', metin or ''))
    return [p for p in parcalar if len(p) >= 3]


def _madde_turu(metin: str) -> str:
    return 'mekanik' if mekanik_maddesi(metin) else 'surec' if surec_maddesi(metin) else 'icerik'


def _madde_anahtari(metin: str) -> str:
    return ' '.join(re.findall(r'[a-z0-9]+', _ascii(metin)))


def inceleme_gereksinimleri(kabul: str, plan: dict[str, Any]) -> list[dict[str, str]]:
    sonuc = [{'id': f'R{no}', 'kriter': 'request', 'tur': _madde_turu(m), 'metin': m}
             for no, m in enumerate(kabul_maddeleri(kabul), 1)]
    gorulen = {_madde_anahtari(g['metin']) for g in sonuc}
    for gorev in plan.get('gorevler') or []:
        for no, m in enumerate(kabul_maddeleri(str(gorev.get('kabul') or '')), 1):
            if _madde_anahtari(m) in gorulen:
                continue
            gorulen.add(_madde_anahtari(m))
            sonuc.append({'id': f"{gorev['id']}.{no}", 'kriter': 'task:' + str(gorev['id']), 'tur': _madde_turu(m), 'metin': m})
    return sonuc


INCELEME_KANIT_METIN_TAVANI = 8_000
INCELEME_KATALOG_METIN_TAVANI = 48_000


def inceleme_kanit_katalogu(wt: pathlib.Path, yollar: list[pathlib.Path],
                           bolumler: dict[pathlib.Path, list[tuple[int, int]]] | None = None) -> dict[str, Any]:
    """N-REP01: immutable ranges from allowed sources; selection belongs to the writer, numbering to the engine."""
    from kosu import bounded_bytes
    kok = pathlib.Path(wt).resolve()
    bolumler = {pathlib.Path(p).resolve(): ranges for p, ranges in (bolumler or {}).items()}
    kanitlar: list[dict[str, Any]] = []
    eksik: list[str] = []
    toplam = 0
    for path in sorted({pathlib.Path(p).resolve() for p in yollar}):
        goreli = str(path.relative_to(kok))
        if _hassas(path):
            raise ValueError('hassas kaynak kanit kataloguna alinamaz')
        if not path.is_file() or path.stat().st_size > 2_000_000:
            eksik.append(goreli)
            continue
        try:
            satirlar = bounded_bytes(path, 2_000_000).decode(errors='replace').splitlines()
        except (OSError, ValueError) as exc:
            raise ValueError('review-source-packet-unavailable') from exc
        for bas, son in bolumler.get(path, [(1, len(satirlar))]):
            if bas < 1 or son > len(satirlar) or son < bas:
                if satirlar:
                    raise ValueError('kanit katalogu kaynak bolumu gecersiz')
                continue
            while bas <= son:
                parca: list[str] = []
                boyut = 0
                for satir in satirlar[bas - 1:min(son, bas - 1 + SATIR_ARALIGI_TAVANI)]:
                    yeni = boyut + len(satir) + int(bool(parca))
                    if yeni > INCELEME_KANIT_METIN_TAVANI or toplam + yeni > INCELEME_KATALOG_METIN_TAVANI:
                        break
                    parca.append(satir)
                    boyut = yeni
                if not parca:
                    eksik.append(goreli)
                    if toplam >= INCELEME_KATALOG_METIN_TAVANI:
                        break
                    bas += 1
                    continue
                bitis = bas + len(parca) - 1
                alinti = next((s for s in parca if s.strip()), '')
                if alinti:
                    kanitlar.append({'id': f'K{len(kanitlar) + 1}', 'tur': 'dosya',
                                     'kaynak': f'{goreli}:{bas}-{bitis}', 'beklenen': alinti,
                                     'metin': '\n'.join(parca)})
                    toplam += boyut
                bas = bitis + 1
    return {'kanitlar': kanitlar, 'eksik_yollar': sorted(set(eksik))}


def inceleme_kanit_secimi(cevap: dict[str, Any], katalog: dict[str, Any]) -> list[dict[str, Any]]:
    if 'evidence_refs' not in cevap:
        kanitlar = cevap.get('evidence')
        if not isinstance(kanitlar, list) or not kanitlar:
            raise ValueError('review-output-invalid')
        return kanitlar
    secim = cevap['evidence_refs']
    if 'evidence' in cevap or not isinstance(secim, list) or not secim or len(secim) > 20:
        raise ValueError('evidence_refs tek ve bos olmayan kanit secimi olmali')
    adaylar = {k['id']: k for k in katalog['kanitlar']}
    gorulen: set[str] = set()
    sonuc: list[dict[str, Any]] = []
    for secilen in secim:
        if (not isinstance(secilen, dict) or set(secilen) != {'id', 'iddia'}
                or not isinstance(secilen['id'], str) or secilen['id'] not in adaylar
                or secilen['id'] in gorulen or not isinstance(secilen['iddia'], str) or not secilen['iddia'].strip()):
            raise ValueError('evidence_refs yalniz katalogdaki tekil id ve iddia alanlarini tasir')
        gorulen.add(secilen['id'])
        sonuc.append({k: v for k, v in adaylar[secilen['id']].items() if k != 'metin'} | {'iddia': secilen['iddia']})
    return sonuc


def kanit_metni(wt, kanit: dict[str, Any], tavan: int = INCELEME_KANIT_METIN_TAVANI) -> str:
    if kanit.get('tur') != 'dosya':
        return str(kanit.get('beklenen') or '')[:tavan]
    m = re.match(r"^(?P<yol>.+?)(?::(?P<bas>\d+)(?:-(?P<son>\d+))?)?$", str(kanit.get('kaynak') or '').strip())
    try:
        yol = _cozumle(wt, m.group('yol'))
        yol.relative_to(pathlib.Path(wt).resolve())
        if _hassas(yol) or not yol.is_file() or yol.stat().st_size > BUYUK_DOSYA:
            return ''
        satirlar = yol.read_text(errors='replace').splitlines()
    except (AttributeError, OSError, ValueError):
        return ''
    if m.group('bas'):
        satirlar = satirlar[int(m.group('bas')) - 1:int(m.group('son') or m.group('bas'))]
    return '\n'.join(satirlar)[:tavan]


def _kod_benzeri(terim: str) -> bool:
    return '_' in terim or '-' in terim or any(c.isdigit() for c in terim) or any(c.isupper() for c in terim[1:])


def _terim_deseni(terim: str) -> re.Pattern[str]:
    kok = terim if len(terim) < 6 else terim[:max(6, len(terim) - 2)]
    return re.compile(r'(?<![A-Za-z0-9])' + re.escape(kok) + (r'(?![A-Za-z])' if len(terim) < 6 else ''), re.I)


def iddia_terimleri(metin: str) -> list[str]:
    metin = DOSYA_ADI.sub(' ', metin)
    terimler = KART_KIMLIGI.findall(metin)
    for kelime in re.findall(r'\w+', KART_KIMLIGI.sub(' ', metin)):
        if (kelime.isascii() and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{2,}', kelime)
                and kelime.casefold() not in TERIM_DISI and not kelime.casefold().startswith('test')):
            terimler.append(kelime)
    tekil: dict[str, str] = {}
    for terim in terimler:
        tekil.setdefault(terim.casefold(), terim)
    return list(tekil.values())


def kanitta_olmayan_terimler(metin: str, kanitlar: list[str], kapsam: list[str], bolum_kapsami: list[str] = ()) -> list[str]:
    sonuc = []
    for terim in iddia_terimleri(metin):
        desen = _terim_deseni(terim)
        kapsamda = any(desen.search(k) for k in kapsam) or (_kod_benzeri(terim) and any(desen.search(k) for k in bolum_kapsami))
        if kapsamda and not any(desen.search(k) for k in kanitlar):
            sonuc.append(terim)
    return sonuc


TERIM_SATIRI_TAVANI = 3
KOD_SATIRI_TAVANI = 2


BOLUM_SATIRI = re.compile(r'^(\d+)\t')


def _satirlar(metin: str, desen: re.Pattern[str], yer: Callable[[int, str], str], tavan: int) -> list[str]:
    bulunan = []
    for no, satir in enumerate(metin.splitlines()):
        if desen.search(satir):
            bulunan.append(f'{yer(no, satir)}: {BOLUM_SATIRI.sub("", satir).strip()[:200]}')
            if len(bulunan) == tavan:
                break
    return bulunan


def _satir_no(i: int, satir: str) -> str:
    m = BOLUM_SATIRI.match(satir)
    return m.group(1) if m else str(i + 1)


def terim_uyarilari(terimler: list[str], kapsam: list[str], adlar: list[str] = (), istek: str = '') -> list[dict[str, Any]]:
    istek_ = (istek or '').translate(TR)
    sonuc = []
    for terim in terimler:
        desen, yerler = _terim_deseni(terim), []
        for no, metin in enumerate(kapsam):
            ad = adlar[no] + ':' if no < len(adlar) and adlar[no] else ''
            yerler += _satirlar(metin, desen, lambda i, s, ad=ad: ad + _satir_no(i, s), TERIM_SATIRI_TAVANI - len(yerler))
            if len(yerler) >= TERIM_SATIRI_TAVANI:
                break
        sonuc.append({'terim': terim, 'istek_terimi': bool(desen.search(istek_)), 'kapsam_satirlari': yerler})
    return sonuc


def kod_satirlari(metin: str, secili: dict[str, str], baslar: dict[str, int | None]) -> dict[str, list[str]]:
    sonuc = {}
    for terim in iddia_terimleri(metin):
        if not _kod_benzeri(terim):
            continue
        desen, satirlar = re.compile(r'(?<![A-Za-z0-9_])' + re.escape(terim) + r'(?![A-Za-z0-9_])'), []
        for kimlik, govde in secili.items():
            bas = baslar.get(kimlik)
            satirlar += _satirlar(govde, desen, lambda i, s, k=kimlik, b=bas: f'{k}:{b + i}' if b else k,
                                  KOD_SATIRI_TAVANI - len(satirlar))
            if len(satirlar) >= KOD_SATIRI_TAVANI:
                break
        if satirlar:
            sonuc[terim] = satirlar
    return sonuc


def iddia_paketi(wt, ogeler: list[dict[str, Any]], kanitlar: list[dict[str, Any]], kapsam: list[str],
                 bolum_kapsami: list[str] = (), *, kapsam_adlari: list[str] = (), bolum_adlari: list[str] = (),
                 istek: str = '') -> list[dict[str, Any]]:
    adlar = [*(list(kapsam_adlari) + [''] * len(kapsam))[:len(kapsam)],
             *(list(bolum_adlari) + ['kart'] * len(bolum_kapsami))[:len(bolum_kapsami)]]
    ham = {k['id']: kanit_metni(wt, k, INCELEME_KANIT_METIN_TAVANI + 1)
           for k in kanitlar if isinstance(k, dict) and isinstance(k.get('id'), str)}
    kirpilan = {k for k, v in ham.items() if len(v) > INCELEME_KANIT_METIN_TAVANI}
    metinler = {k: v[:INCELEME_KANIT_METIN_TAVANI] for k, v in ham.items()}
    baslar = {}
    for k in kanitlar:
        if isinstance(k, dict) and isinstance(k.get('id'), str):
            m = re.search(r':(\d+)(?:-\d+)?$', str(k.get('kaynak') or '').strip())
            baslar[k['id']] = int(m.group(1)) if k.get('tur') == 'dosya' and m else None
    paket = []
    for oge in ogeler:
        if oge['evidence_ids']:
            secili = {k: metinler.get(k, '') for k in oge['evidence_ids']}
            isaretli = [] if kirpilan.intersection(secili) else kanitta_olmayan_terimler(
                oge['text'], list(secili.values()), kapsam, list(bolum_kapsami))
            paket.append({'id': oge['id'], 'text': oge['text'], 'evidence_ids': oge['evidence_ids'], 'kanit_metinleri': secili,
                          'kirpilan_kanitlar': sorted(kirpilan.intersection(secili)),
                          'kanitta_olmayan_terimler': isaretli,
                          'terim_uyarilari': terim_uyarilari(isaretli, [*kapsam, *bolum_kapsami], adlar, istek),
                          'kod_satirlari': kod_satirlari(oge['text'], secili, baslar)})
    return paket


def somut_duzeltmeler(raw: str, digest: str | None, iddialar: list[dict[str, Any]], gereksinimler: list[dict[str, str]],
                      olcutler: list[str]) -> list[dict[str, Any]]:
    from sonuc import _json_adaylari, ayni_revizyon, yer_tutucu
    govdeler = [a for a in _json_adaylari(raw or '') if isinstance(a, dict) and ayni_revizyon(a.get('revision'), digest or '')]
    if len({json.dumps(g, sort_keys=True) for g in govdeler}) != 1 or govdeler[0].get('verdict') != 'DUZELT':
        return []
    govde = govdeler[0]

    def gerekce(metin: Any) -> str | None:
        return metin.strip()[:1000] if isinstance(metin, str) and metin.strip() and not yer_tutucu(metin) else None

    def tekil(liste: Any) -> dict[str, dict[str, Any]]:
        sayac: dict[str, list[dict[str, Any]]] = {}
        for k in liste if isinstance(liste, list) else []:
            if isinstance(k, dict) and isinstance(k.get('id'), str):
                sayac.setdefault(k['id'], []).append(k)
        return {kimlik: k[0] for kimlik, k in sayac.items() if len(k) == 1}

    bulgular = []
    isaretli = {i['id']: {t.casefold() for t in i['kanitta_olmayan_terimler']} for i in iddialar}
    for kimlik, k in tekil(govde.get('iddialar')).items():
        neden = gerekce(k.get('gerekce'))
        if kimlik not in isaretli or neden is None:
            continue
        terimler = sorted({t['terim'] for t in k.get('terimler') or [] if isinstance(t, dict) and t.get('durum') == 'desteksiz'
                           and isinstance(t.get('terim'), str) and t['terim'].casefold() in isaretli[kimlik]})
        if k.get('destek') in ('kismi', 'yok') or terimler:
            bulgular.append({'tur': 'iddia', 'id': kimlik, 'destek': k.get('destek'), 'terimler': terimler, 'gerekce': neden})
    bilinen = {g['id'] for g in gereksinimler if g.get('tur') != 'mekanik'}
    for kimlik, k in tekil(govde.get('gereksinimler')).items():
        neden = gerekce(k.get('gerekce'))
        if kimlik in bilinen and k.get('durum') == 'eksik' and neden:
            bulgular.append({'tur': 'gereksinim', 'id': kimlik, 'gerekce': neden})
    for kimlik, k in tekil(govde.get('acceptance')).items():
        neden = gerekce(k.get('evidence'))
        if kimlik in olcutler and k.get('status') == 'FAIL' and neden:
            bulgular.append({'tur': 'olcut', 'id': kimlik, 'gerekce': neden})
    return bulgular[:50]


def _anlamsal_govde(raw: str, digest: str | None) -> dict[str, Any]:
    from sonuc import _json_adaylari, ayni_revizyon
    bulunan = [a for a in _json_adaylari(raw or '') if isinstance(a, dict) and ayni_revizyon(a.get('revision'), digest or '')
               and ('iddialar' in a or 'gereksinimler' in a)]
    if not bulunan:
        raise ValueError('iddialar ve gereksinimler kararlari yok')
    if len({json.dumps([b.get('iddialar'), b.get('gereksinimler')], sort_keys=True) for b in bulunan}) > 1:
        raise ValueError('celisen anlamsal kararlar')
    return bulunan[-1]


def _gerekce(karar: dict[str, Any], kimlik: str) -> str:
    from sonuc import yer_tutucu
    if not isinstance(karar.get('gerekce'), str) or yer_tutucu(karar['gerekce']):
        raise ValueError(f'{kimlik}: gerekce yok')
    return karar['gerekce'].strip()


def _iddia_kararlari(verilen: Any, iddialar: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    beklenen = {i['id']: i for i in iddialar}
    if not isinstance(verilen, list) or any(not isinstance(k, dict) for k in verilen):
        raise ValueError('iddialar liste olmali')
    kararlar: dict[str, dict[str, Any]] = {}
    for karar in verilen:
        kimlik = karar.get('id')
        if kimlik not in beklenen or kimlik in kararlar:
            raise ValueError(f'bilinmeyen ya da tekrar iddia: {kimlik!r}')
        if karar.get('destek') not in IDDIA_DESTEK:
            raise ValueError(f"{kimlik}: destek {'|'.join(IDDIA_DESTEK)} olmali")
        isaretli = {t.casefold(): t for t in beklenen[kimlik]['kanitta_olmayan_terimler']}
        terimler = karar.get('terimler', [])
        if (not isinstance(terimler, list) or any(not isinstance(t, dict) or not isinstance(t.get('terim'), str)
                                                  or t.get('durum') not in TERIM_DURUM for t in terimler)):
            raise ValueError(f"{kimlik}: terim durumu {'|'.join(TERIM_DURUM)} olmali")
        verilen_terimler = [t['terim'].casefold() for t in terimler]
        if len(set(verilen_terimler)) != len(verilen_terimler):
            raise ValueError(f'{kimlik}: ayni terim birden fazla kararlanamaz')
        kararlar[kimlik] = {'id': kimlik, 'destek': karar['destek'], 'gerekce': _gerekce(karar, kimlik),
                            'terimler': [{'terim': isaretli.get(t['terim'].casefold(), t['terim']), 'durum': t['durum']}
                                         for t in terimler if t['terim'].casefold() in isaretli or t['durum'] == 'desteksiz']}
    if set(kararlar) != set(beklenen):
        raise ValueError(f'iddia karari eksik: {", ".join(sorted(set(beklenen) - set(kararlar)))}')
    return kararlar


def _gereksinim_kararlari(verilen: Any, gereksinimler: list[dict[str, str]], ogeler: list[dict[str, Any]]) -> list[dict[str, Any]]:
    beklenen = {g['id']: g for g in gereksinimler}
    oge_idleri = {o['id'] for o in ogeler}
    if not isinstance(verilen, list) or any(not isinstance(k, dict) for k in verilen):
        raise ValueError('gereksinimler liste olmali')
    kararlar: dict[str, dict[str, Any]] = {}
    for karar in verilen:
        kimlik = karar.get('id')
        if not isinstance(kimlik, str) or kimlik in kararlar:
            raise ValueError(f'tekrar ya da gecersiz gereksinim: {kimlik!r}')
        if kimlik in beklenen and beklenen[kimlik].get('tur') == 'mekanik':
            continue
        if kimlik in beklenen:
            tanim = beklenen[kimlik]
        elif EK_GEREKSINIM.fullmatch(kimlik) and isinstance(karar.get('metin'), str) and karar['metin'].strip():
            tanim = {'id': kimlik, 'kriter': 'request', 'tur': 'icerik', 'metin': karar['metin'].strip()}
        else:
            raise ValueError(f'bilinmeyen gereksinim: {kimlik!r}')
        durum, rapor_ = karar.get('durum'), karar.get('rapor', [])
        if durum not in GEREKSINIM_DURUM:
            raise ValueError(f"{kimlik}: durum {'|'.join(GEREKSINIM_DURUM)} olmali")
        if durum == 'rapor_disi' and tanim.get('tur') != 'surec':
            raise ValueError(f'{kimlik}: yalniz motorun surec maddesi saydigi gereksinim rapor disi olabilir')
        if (not isinstance(rapor_, list) or len(set(map(str, rapor_))) != len(rapor_)
                or any(o not in oge_idleri for o in rapor_) or (durum == 'karsilandi' and not rapor_)):
            raise ValueError(f'{kimlik}: karsilama yalniz teslim edilen rapor ogesi kimlikleriyle gosterilir')
        kararlar[kimlik] = {**tanim, 'durum': durum, 'rapor': list(rapor_), 'gerekce': _gerekce(karar, kimlik)}
    zorunlu = {k for k, g in beklenen.items() if g.get('tur') != 'mekanik'}
    if not zorunlu <= set(kararlar):
        raise ValueError(f'gereksinim karari eksik: {", ".join(sorted(zorunlu - set(kararlar)))}')
    return list(kararlar.values())


def anlamsal_karar_uygula(raw: str, review: dict[str, Any], ogeler: list[dict[str, Any]], iddialar: list[dict[str, Any]],
                          gereksinimler: list[dict[str, str]], kanitlar: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if review.get('status') not in ('PASS', 'FAIL'):
        return review
    try:
        govde = _anlamsal_govde(raw, (review.get('revision') or {}).get('digest'))
        kararlar = _iddia_kararlari(govde.get('iddialar'), iddialar)
        kapsam = _gereksinim_kararlari(govde.get('gereksinimler'), gereksinimler, ogeler)
    except ValueError as exc:
        return {'status': 'ERROR', 'revision': review.get('revision'), 'reason': f'anlamsal karar gecersiz: {exc}'}
    kapsam += [mekanik_karar(g, kanitlar or []) for g in gereksinimler if g.get('tur') == 'mekanik']
    desteksiz = {}
    for karar in kararlar.values():
        terimler = [t['terim'] for t in karar['terimler'] if t['durum'] == 'desteksiz']
        if karar['destek'] != 'tam' or terimler:
            desteksiz[karar['id']] = (f"{karar['id']} destek={karar['destek']}"
                                      + (f" kanitta olmayan terim: {', '.join(terimler)}" if terimler else '') + f": {karar['gerekce']}")
    eksik = []
    for karar in kapsam:
        if karar['durum'] == 'eksik':
            eksik.append(f"{karar['id']} eksik ({karar['metin']}): {karar['gerekce']}")
        dayanak = [o for o in karar['rapor'] if o in desteksiz]
        if karar['durum'] == 'karsilandi' and dayanak:
            eksik.append(f"{karar['id']} desteklenmeyen ogeye dayaniyor: {', '.join(dayanak)} ({karar['metin']})")
    maddeler = [
        {'id': 'iddia_destegi', 'status': 'FAIL' if desteksiz else 'PASS',
         'evidence': '; '.join(desteksiz.values())[:6000] or 'Her kanitli cumle kendi kanit araligiyla ayri desteklendi'},
        {'id': 'istek_kapsami', 'status': 'FAIL' if eksik else 'PASS',
         'evidence': '; '.join(eksik)[:6000] or 'Her gereksinim teslim edilen rapor ogeleriyle karsilandi'}]
    sonuc = {**review, 'iddialar': list(kararlar.values()), 'gereksinimler': kapsam,
             'acceptance': [*(review.get('acceptance') or []), *maddeler]}
    if desteksiz or eksik:
        sonuc.update(status='FAIL', verdict='DUZELT')
    return sonuc


def plan_denetle(plan: dict[str, Any], beyan_: dict[str, Any]) -> str | None:
    """G-R1: in a report job every task's files are a subset of report + evidence appendix and one task writes the report."""
    izinli = (beyan_["yol"], beyan_["kanit"])
    gorevler = plan.get("gorevler") or []
    for g in gorevler:
        disari = [f for f in g.get("dosyalar") or [] if f not in izinli]
        if disari:
            return (f"rapor kipi (G-R1): gorev {g.get('id')} rapor disi dosya listeliyor ({', '.join(disari[:5])}); "
                    f"dosyalar yalniz {izinli[0]} ve {izinli[1]} olabilir")
    if not any(izinli[0] in (g.get("dosyalar") or []) for g in gorevler):
        return f"rapor kipi (G-R1): raporu yazan gorev yok; bir gorevin dosyalari {izinli[0]} icermeli"
    return None


def yazar_mi(plan: dict[str, Any], gid: str, beyan_: dict[str, Any]) -> bool:
    """G-R6: the lane that writes the report (interpretation and results) goes to a strong model; a data-gathering lane that only writes the evidence appendix does not."""
    return any(g.get("id") == gid and beyan_["yol"] in (g.get("dosyalar") or []) for g in plan.get("gorevler") or [])


def serit_kurallari(beyan_: dict[str, Any]) -> str:
    return (f"RAPOR KIPI (G-R1..G-R4): yalniz {beyan_['yol']} ve {beyan_['kanit']} dosyalarini yaz; kod, test, config degistirme. "
            "ONCE `rapor-kaniti` skill'ini oku (kanit eki bicimi; kapi mekanik denetler).\n"
            + kanit_kurallari(f"Kanit eki {beyan_['kanit']} JSONL: her satir"))


def kanit_kurallari(hedef: str) -> str:
    return (f"- {hedef} {{\"id\": \"K1\", \"iddia\": \"...\", \"tur\": \"dosya|komut|sorgu|url|bug\", "
            "\"kaynak\": \"yol:satir | komut | url\", \"beklenen\": \"...\"}. dosya: beklenen o satirdaki alinti; komut/sorgu: beklenen "
            "ciktida gecen metin; bug: kaynak mevcut kodda KIRMIZI biten yeniden uretim komutu, beklenen hata metni (AssertionError gibi). "
            "`tur` KANITIN turudur; bulgunun sinifi (bug / risk / tasarim tercihi / test boslugu) raporda yazilir, tur alanina yazilmaz.\n"
            "- Raporda sayi ya da bulgu iceren her cumle (tabloda her satir) kanit kimligi tasir: [K1], [K1, K2] ya da [K1..K4]; ayni eke birden cok serit yaziyorsa "
            "kimlik serit oneklidir (K-tz1, K-yd1). Kanitlayamadigin sayiyi "
            "'dogrulanmadi' diye isaretle; uydurma. Kanit komutlari salt-okur, agsiz bir kumda yeniden kosulur.\n")


def plan_istemi(beyan_: dict[str, Any]) -> str:
    return (f"RAPOR KIPI (G-R1/G-R6): bu is RAPOR/ANALIZ isidir, kod degismez. Her gorevin 'dosyalar' listesi YALNIZ "
            f"{beyan_['yol']} (rapor) ve {beyan_['kanit']} (kanit eki) icerebilir; baska dosya plani gecersiz kilar. "
            f"Veri toplama gorevleri (olcum, sorgu, dosya tarama) yalniz {beyan_['kanit']}'a satir ekler; yorum ve sonuc TEK gorevde: "
            f"dosyalar [{beyan_['yol']}, {beyan_['kanit']}], veri gorevlerine bagimli, son gorev — koordinator bu gorevi guclu modele verir. "
            + serit_kurallari(beyan_).split("\n", 1)[1])


def mutabakat_istemi(beyan_: dict[str, Any]) -> str:
    """G-R7: a third model reconciles two independent drafts; a diverging claim does not enter the final report without verification from its source."""
    ra, ka = pathlib.PurePosixPath(beyan_["yol"]).name, pathlib.PurePosixPath(beyan_["kanit"]).name
    return (f"MUTABAKAT (G-R7): iki bagimsiz analist ayni soruyu ayri analiz etti. A: .cadre-plan/analist-a/{ra} ve "
            f".cadre-plan/analist-a/{ka} · B: .cadre-plan/analist-b/{ra} ve .cadre-plan/analist-b/{ka}. Nihai raporu {beyan_['yol']}, "
            f"kanit ekini {beyan_['kanit']} dosyasina yaz. Iki analistin ortak iddialarini kanitlariyla al; ayrisan her iddiayi "
            "KAYNAGINDAN kendin dogrula: dogrulanan girer, dogrulanamayan 'Ayrismalar' bolumunde iki gorusle ve 'dogrulanmadi' "
            "isaretiyle kalir. Kanit kimliklerini K1'den yeniden numarala; .cadre-plan altina yazma.\n" + serit_kurallari(beyan_))


ANALIST_B = ("BAGIMSIZ IKINCI ANALIST (G-R7): ayni soruyu baska bir analist de ayri analiz ediyor; onun taslagi sana verilmez. "
             "Kendi olcum ve okumanla bastan yaz; tahmin yurutme, her iddiayi kanitla.")


def main(argv: list[str]) -> int:
    komut = argv[0] if argv else ""
    b = kayit_beyani()
    if komut == "kip":
        print("rapor" if b else "")
        return 0 if b else 1
    if komut == "plan-istemi":
        print(plan_istemi(b) if b else "")
        return 0
    if komut in ("yollar", "mutabakat-istemi", "analist-istemi") and b:
        print({"yollar": f"{b['yol']} {b['kanit']}", "mutabakat-istemi": mutabakat_istemi(b) if komut == "mutabakat-istemi" else "",
               "analist-istemi": ANALIST_B}[komut])
        return 0
    if komut == "analist-kaydi" and len(argv) >= 4:
        try:
            analist_kaydi(pathlib.Path(os.environ["CADRE_RUN_DIR"]), pathlib.Path(argv[2]), argv[1], argv[3:6], argv[6] if len(argv) > 6 else "")
        except (KeyError, OSError, ValueError) as exc:
            print(f"analist kaydi yazilamadi: {exc}", file=sys.stderr)
            return 1
        return 0
    if komut == "yazar" and len(argv) > 1:
        try:
            plan = json.loads((pathlib.Path(os.environ["CADRE_RUN_DIR"]) / "plan.json").read_text())
        except (KeyError, OSError, ValueError):
            return 1
        return 0 if b and yazar_mi(plan, argv[1], b) else 1
    if komut == "kurtar" and len(argv) > 1 and b:
        # B238: the writer chain is exhausted but the product may be on disk (the writer looped in the last round and was cut); if it passes the gate it produced.
        from kosu import RunStore
        from qe_core import URUN_HARIC
        try:
            kayit = RunStore().read(os.environ["CADRE_RUN_ID"])
            gecici = pathlib.Path(os.environ["CADRE_RUN_DIR"]) / "tasks" / argv[1] / "kurtarma"
            r = rapor_qa(kayit["worktree"], b, kayit.get("source_sha"), gecici, tohum="kurtarma", haric=URUN_HARIC)
        except (KeyError, OSError, ValueError) as exc:
            print(f"kurtarma denenemedi: {exc}", file=sys.stderr)
            return 1
        print(f"kurtarma rapor QA: {r['status']} ({len(r['bulgular'])} bulgu)")
        return 0 if r["status"] == "PASS" else 1
    print("kullanim: rapor.py kip | plan-istemi | yazar <gorev> | yollar | mutabakat-istemi | analist-istemi | analist-kaydi <kip> <wt> <m1> [m2] [m3] [neden]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
