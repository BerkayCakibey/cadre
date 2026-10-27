"""Panel data model and the redacted, replayable event trace of a run."""
from __future__ import annotations

import json
import hashlib
import math
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import dil
from dil import t

TERMINAL = ('completed', 'failed', 'cancelled', 'implemented')
YARDIMCI = ('plan', 'qa', 'kalite', 'birlestirme', 'main', 'helper', 'proje-liste')
DURUM_ADI = {'accepted': 'başlıyor', 'queued': 'kuyrukta', 'running': 'çalışıyor', 'retryable': 'bekliyor', 'completed': 'bitti',
             'failed': 'düştü', 'cancelled': 'iptal edildi', 'implemented': 'uygulandı'}
GOREV_ISARETI = {'uretti': 'bitti', 'tamam': 'bitti', 'running': 'simdi', 'yarim': 'hata', 'basarisiz': 'hata', 'bloke': 'hata'}
GOREV_YAZI = {'uretti': 'bitti', 'tamam': 'bitti', 'running': 'çalışıyor', 'yarim': 'yarım kaldı', 'basarisiz': 'başarısız',
              'bloke': 'engellendi'}
PARCA_ISARETI = {'gecti': 'bitti', 'uygulandi': 'bitti', 'takildi': 'hata', 'bekliyor': 'bekliyor'}
PARCA_YAZI = {'gecti': 'geçti', 'uygulandi': 'uygulandı', 'takildi': 'takıldı', 'bekliyor': 'sırada'}
ISARET_OZET = {'bitti': 'tamam', 'simdi': 'sürüyor', 'bekliyor': 'sırada', 'hata': 'sorun'}
SEVIYE_ADI = {'basit': 'basit', 'orta': 'orta', 'zor': 'zor', 'cok-zor': 'çok zor', 'ultra': 'ultra'}
ROL_BILGI = {
    'plan': ('Planlayıcı', 'Her görevin başında planı yapar. Büyük görevde parçaları da o çıkarır.'),
    'impl': ('Geliştirici', 'Her alt görevi yazar. Test kırmızıysa düzeltme turu da bu sırayla çalışır.'),
    'kalite': ('Hakem', 'Test geçince bağımsız olarak inceler. Yazan modelin ailesinden olamaz.'),
    'impl-hafif': ('Geliştirici', 'Zorluğu 1 ya da 2 olan küçük alt görevleri yazar.'),
    'birlestirme': ('Geliştirici', 'Üçten fazla alt görev aynı anda yazıldıysa çıktıları birleştirir.'),
    'kurtarici': ('Geliştirici', 'Takılan bir görev temiz baştan başlatılırken çalışır.'),
}
SAGLAYICI_ADI = {'opencode-go': 'Go (abonelik)', 'openrouter': 'OpenRouter (bakiye)', 'opencode': 'Zen (bakiye)',
                 'claude': 'Claude (abonelik)', 'groq': 'Groq (ücretsiz)'}
GOREV_COCUKLARI = {'plan': 'planlayıcının alt görevleri, baştan listelenir', 'test': 'tüm alt görevler bitince bir kez',
                   'hakem': 'test geçince bağımsız inceleme, bir kez', 'son-kontrol': 'tüm testler ve hakem incelemesi, bir kez',
                   'teslim': 'dal, değişen dosyalar, push ve merge', 'rapor': 'rapor dosyası, o ile okunur', 'sonuc': 'kayıtlı kabul sonucu, test ve hakem',
                   'ajanlar': 'her ajanın düşünceleri ve araç çağrıları', 'harcama': 'alt göreve, modele ve sağlayıcıya göre',
                   'olaylar': 'önemli olaylar, o ile tam günlük'}
OLAY = re.compile(r'plan kabul|gorev .* (BASARISIZ|KURTARILDI|uretti)|QA KAPISI|HAKEM|KABUL SONUCU|BUTCE BEKLEME|BITTI|harcama alarmi|'
                  r'PARALEL|KAPSAM|proje|devral|iptal', re.I)
GRAFIK = '▁▂▃▄▅▆▇█'
ONBELLEK: dict[str, tuple[float, Any]] = {}
BITEN: dict[tuple, dict] = {}
KAYIT: dict[str, tuple[int, dict]] = {}
IZ: dict[str, tuple[float, tuple]] = {}
SONDA: dict[str, Callable[..., Any]] = {}


def d(ad: str, ozet: str = '', ayrinti: list | None = None, cocuk: list | None = None, durum: str = '', baglam: list | None = None,
      **ek: Any) -> dict:
    dugum = {'ad': ad, 'ozet': ozet, 'ayrinti': ayrinti or [], 'cocuk': cocuk or [], 'durum': durum, 'baglam': baglam or []}
    dugum.update({a: v for a, v in ek.items() if v is not None})
    return dugum


def aciklama(baslik: str, metin: str, icinde: list[tuple[str, str]] | None = None, ek: list | None = None) -> list:
    bloklar = [('baslik', baslik), ('satir', metin)]
    if icinde:
        bloklar += [('bolum', t('İçinde'))] + [('alan', (a, b)) for a, b in icinde]
    return bloklar + (ek or [])


def temizle() -> None:
    ONBELLEK.clear()
    BITEN.clear()
    IZ.clear()


def sayi(x: Any) -> float:
    try:
        deger = float(x or 0)
    except (TypeError, ValueError):
        return 0.0
    return deger if math.isfinite(deger) else 0.0


def para(x: Any) -> str:
    metin = f'${sayi(x):.2f}'
    return metin if dil.DIL == 'en' else metin.replace('.', ',')


def oran(x: Any) -> str:
    return f'{sayi(x):.0f}%' if dil.DIL == 'en' else f'%{sayi(x):.0f}'


def token_yazi(n: Any) -> str:
    deger = sayi(n)
    if not deger:
        return '—'
    metin = (f'{deger / 1e6:.2f}'.rstrip('0').rstrip('.') + 'M') if deger >= 1e6 else f'{round(deger / 1000)}K'
    return t('{n} token', n=metin if dil.DIL == 'en' else metin.replace('.', ','))


def liste_yazi(deger: Any) -> str:
    if isinstance(deger, (list, tuple)):
        return ', '.join(map(str, deger)) or '—'
    return str(deger or '') or '—'


def sure_metni(sn: float) -> str:
    dakika = max(0, int(sn)) // 60
    return t('{s} sa {d:02d} dk', s=dakika // 60, d=dakika % 60) if dakika >= 60 else t('{d} dk', d=dakika)


def zaman_yazi(bicim: str, ts: Any) -> str:
    try:
        return datetime.fromtimestamp(sayi(ts), timezone.utc).astimezone().strftime(bicim) if sayi(ts) else '—'
    except (OverflowError, OSError, ValueError):
        return '—'


def saat(ts: Any) -> str:
    return zaman_yazi('%H:%M', ts)


def tarih(ts: Any) -> str:
    return zaman_yazi('%d.%m %H:%M', ts)


def goreli_yas(ts: Any) -> str:
    deger = sayi(ts)
    if isinstance(ts, bool) or deger <= 0 or deger > time.time():
        return t('zaman bilinmiyor')
    sn = time.time() - deger
    if sn < 60:
        return t('az önce')
    if sn < 3600:
        return t('{n} dk önce', n=int(sn // 60))
    if sn < 86400:
        return t('{n} sa önce', n=int(sn // 3600))
    return t('{n} gün önce', n=int(sn // 86400))


def mutlak_zaman(ts: Any) -> str:
    metin = zaman_yazi('%Y-%m-%d %H:%M:%S %z', ts) if not isinstance(ts, bool) and sayi(ts) > 0 else '—'
    return metin if metin != '—' else t('zaman bilinmiyor')


def kimlik_ozeti(k: dict) -> list:
    """CR-36: the run's recorded engine/config/environment identity and the operator intervention count; nothing invented for old records."""
    from kosu import mudahale_ozeti
    kimlik = sozluk(k.get('provenance'))
    if not kimlik:
        satirlar = [('alan', (t('Kaynak kimliği'), t('kayıt yok (eski koşu)')))]
    else:
        motor, ortam = sozluk(kimlik.get('engine')), sozluk(kimlik.get('runtime'))
        sha = str(motor.get('git_head') or '')[:12] or t('bilinmiyor')
        kirli = {True: t('kirli diff'), False: t('temiz')}.get(motor.get('git_dirty'), t('bilinmiyor'))
        satirlar = [('alan', (t('Motor kaynağı'), f"{sha} · {kirli} · " + str(motor.get('guard_version') or t('bilinmiyor'))[:12])),
                    ('alan', (t('Yürütücü sürümü'), str(ortam.get('opencode_version') or t('bilinmiyor')))),
                    ('alan', (t('Ortam'), f"{ortam.get('platform') or t('bilinmiyor')} · python {ortam.get('python') or '?'}")),
                    ('alan', (t('Kimlik kaydı'), mutlak_zaman(kimlik.get('recorded'))))]
    ozet = mudahale_ozeti(k)
    if ozet['count'] is None:
        satirlar.append(('alan', (t('Operatör müdahalesi'), t('kayıt yok (eski koşu)'))))
    else:
        satirlar.append(('alan', (t('Operatör müdahalesi'), t('{n} kayıt · dakika ölçülmedi', n=ozet['count'])
                                  + (f" · {', '.join(ozet['kinds'])}" if ozet['kinds'] else ''))))
    return satirlar


def test_projesi(k: dict) -> bool:
    return k.get('test_run') is True or bool(re.fullmatch(r'canli-c\d{2}-\d{8}-\d{6}(?:--p[\w-]+)?', str(k.get('project') or '')))


def sozluk(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def olay_saati(deger: Any) -> str:
    if isinstance(deger, (int, float)) and deger:
        return saat(deger / 1000 if deger > 1e11 else deger)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(deger).replace('Z', '+00:00')).astimezone().strftime('%H:%M')
    except (OverflowError, OSError, ValueError):
        return '--:--'


def iso_yerel(deger: Any) -> str:
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(deger).replace('Z', '+00:00')).astimezone().strftime('%d.%m %H:%M')
    except (OverflowError, OSError, ValueError):
        return '—'


def grafik(degerler: list[float]) -> str:
    if not degerler:
        return '—'
    taban, tavan = min(degerler), max(degerler)
    aralik = (tavan - taban) or 1
    return ''.join(GRAFIK[max(0, min(len(GRAFIK) - 1, round((len(GRAFIK) - 1) * (v - taban) / aralik)))] for v in degerler)


def gidisat_yazi(degerler: list[float]) -> str:
    return f'{grafik(degerler)}   {oran(degerler[0])} → {oran(degerler[-1])}' if degerler else '—'


def ev() -> Path:
    return dil.ev()


def _oku(yol: Path) -> dict:
    try:
        veri = json.loads(yol.read_text())
    except (OSError, ValueError):
        return {}
    return veri if isinstance(veri, dict) else {}


def kayit_oku(yol: Path) -> dict:
    try:
        damga = yol.stat().st_mtime_ns
    except OSError:
        return {}
    onceki = KAYIT.get(str(yol))
    if onceki and onceki[0] == damga:
        return onceki[1]
    deger = _oku(yol)
    KAYIT[str(yol)] = (damga, deger)
    return deger


def hata_yazi(exc: BaseException) -> str:
    return f'{type(exc).__name__}: {str(exc)[:120]}' if str(exc) else type(exc).__name__


def istek_saati(k: dict) -> dict | None:
    if not isinstance(k.get('request'), dict):
        return None
    try:
        from kosu import istek_saat_ozeti
        return istek_saat_ozeti(k)
    except (KeyError, TypeError, ValueError):
        return None


def kapsam_metni(files, tests) -> str:
    """CR-35: the narrow acceptance scope bound to the request/approval; empty means acceptance of the whole package."""
    if isinstance(files, (list, tuple)):
        files = ', '.join(files)
    if isinstance(tests, (list, tuple)):
        tests = ', '.join(tests)
    if not files:
        return t('Yok (bütün paket kabulü)')
    return t('izinli dosyalar {f} · gerekli testler {g}', f=files, g=tests or '—')


def istek_onay_metni(rid: str, proposal: dict, komut: str | None = None) -> str:
    action = proposal['action']
    metin = (rid + '\n' + action['output'] + '\n' + action['target_path'] + '\n' +
             ', '.join(action['providers']) + '\n' + str(action['duration_minutes']) + ' min · ' + action['budget_usd'] + ' USD\n' +
             '\n'.join(role + ': ' + ', '.join(models) for role, models in action['models'].items() if models) + '\n' +
             t('Commit, push, merge, kurulum ve yayın bu izne dahil değildir.'))
    qa = action.get('qa_command')
    if qa:
        metin += '\n' + t('QA komutu') + ': ' + (qa['command'] or t('başlangıçta kaynaktan türetilir'))
    kapsam = action.get('acceptance_scope')
    if kapsam:
        metin += '\n' + t('Dar kabul kapsamı') + ': ' + kapsam_metni(kapsam.get('files'), kapsam.get('tests'))
    if komut is None:
        return metin
    return (metin + '\n\n' + t('Onay kimliği') + ': ' + proposal['token'] + '\n\n' +
            t('Tam komut (CLI eşdeğeri, yalnız kayıt için):') + '\n' + komut)


def _onbellekli(anahtar: str, sure: float, uret: Callable[[], Any], kimlik: str) -> Any:
    zaman, deger = ONBELLEK.get(anahtar, (0.0, None))
    if deger is None or time.time() - zaman > sure:
        try:
            deger = uret()
        except Exception as exc:
            ad = anahtar.split(':')[0]
            deger = d(t(ad), t('okunamadı'), [('baslik', t(ad)), ('soluk', t('okunamadı: {e}', e=hata_yazi(exc)))], id=kimlik,
                      aciklama=aciklama(t(ad), t('Bu bölüm şu an okunamadı. Bir dakika içinde yeniden denenir.')))
        bolum = anahtar.split(':')[0] + ':'
        for eski in [a for a in list(ONBELLEK) if a.startswith(bolum) and a != anahtar]:
            del ONBELLEK[eski]
        ONBELLEK[anahtar] = (time.time(), deger)
    return deger


def kosu_dizini(home: Path, rid: Any) -> Path:
    return home / 'runs' / str(rid)


def proje_adi(k: dict) -> str:
    return str(k.get('project') or '?').split('--p')[0]


def ilk_satir(home: Path, k: dict) -> str:
    try:
        metin = Path(k.get('prompt') or kosu_dizini(home, k['run_id']) / 'prompt.md').read_text(errors='replace')
    except OSError:
        return t('(istem okunamadı)')
    return next((s.strip().lstrip('#').strip() for s in metin.splitlines() if s.strip()), t('(boş istem)'))


def son_satirlar(yol: str | Path, n: int) -> list[str]:
    try:
        with Path(yol).open('rb') as f:
            f.seek(0, 2)
            ofset = max(0, f.tell() - 65536)
            f.seek(ofset)
            if ofset:
                f.readline()
            metin = f.read().decode('utf-8', 'replace')
    except OSError:
        return []
    return [s.rstrip()[:160] for s in metin_redakte(metin).splitlines() if s.strip()][-n:]


def olay_kayitlari(home: Path, rid: str, suzgec: str = '') -> list[tuple[float, str, str]]:
    secilen = []
    for s in dict.fromkeys(son_satirlar(kosu_dizini(home, rid) / 'dispatch.log', 500)):
        m = re.match(r'^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})?)\s+(.+)$', s)
        if not m or not (suzgec in m[2] if suzgec else OLAY.search(m[2])):
            continue
        try:
            ts = datetime.fromisoformat(m[1].replace('Z', '+00:00')).timestamp()
        except (ValueError, OverflowError, OSError):
            continue
        secilen.append((ts, m[1], m[2]))
    return sorted(secilen)


def olaylar(home: Path, rid: str, n: int = 6, suzgec: str = '', mutlak: bool = False) -> list[str]:
    if n <= 0:
        return []
    secilen = olay_kayitlari(home, rid, suzgec)[-n:]
    if mutlak:
        return [f"{damga}{' (' + t('saat dilimi kayıtsız') + ')' if len(damga) == 19 else ''} {metin}" for _, damga, metin in secilen]
    return [f'{damga[11:16]} {metin[:110]}' for _, damga, metin in secilen]


def son_olaylar(home: Path, kayitlar: list[dict], n: int = 6) -> list[str]:
    if n <= 0:
        return []
    satirlar = []
    for k in kayitlar:
        satirlar += [(ts, str(k['run_id']), damga, metin, proje_adi(k)) for ts, damga, metin in olay_kayitlari(home, str(k['run_id']))[-n:]]
    return [f'{damga[11:16]} {proje}: {metin[:80]}' for _, _, damga, metin, proje in sorted(satirlar, reverse=True)[:min(n, 6)]]


def kosular(home: Path, n: int | None = None) -> list[dict]:
    yollar = sorted((home / 'runs').glob('*/run.json'), key=lambda p: p.parent.name)
    return [k for k in (kayit_oku(p) for p in (yollar[-n:] if n else yollar)) if k.get('run_id')]


def _onay(k: dict) -> bool:
    from sonuc import approved
    return bool(approved(k))


def _butce(home: Path) -> tuple[dict, float]:
    import ekran
    return ekran.butce_gorunumu(home, tazele=False)


def _zincir(rol: str, b: dict, durum: dict) -> tuple[list, list]:
    import yonlendirici
    return yonlendirici.zincir(rol, b=b, d=durum, kaydet=False)


def _ariza() -> dict:
    import yonlendirici
    return yonlendirici.durum_oku()


def _claude_kapali() -> bool:
    import yonlendirici
    return bool(yonlendirici.claude_kapali())


VARSAYILAN_SONDA = {'onay': _onay, 'butce': _butce, 'zincir': _zincir, 'ariza': _ariza, 'claude_kapali': _claude_kapali}


def sonda(ad: str) -> Callable[..., Any]:
    return SONDA.get(ad) or VARSAYILAN_SONDA[ad]


def claude_kapali() -> bool:
    try:
        return bool(sonda('claude_kapali')())
    except Exception:
        return False


def teslim_adayi(k: dict) -> bool:
    from izin import delivery_candidate
    try:
        return delivery_candidate(k, sonda('onay'))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def urun_izi(k: dict) -> tuple:
    if not teslim_adayi(k):
        return ()
    wt = str(k['worktree'])
    zaman, iz = IZ.get(wt, (0.0, ()))
    if iz and time.time() - zaman < 15:
        return iz
    head = git_cikti(wt, '--no-optional-locks', 'rev-parse', 'HEAD').strip()
    durum = git_cikti(wt, '--no-optional-locks', 'status', '--porcelain=v1', '-z', '--untracked-files=all')
    damgalar = []
    for kayit in durum.split('\0'):
        yol = Path(wt) / kayit[3:] if len(kayit) > 3 else None
        try:
            st = yol.stat() if yol else None
        except OSError:
            st = None
        damgalar.append((kayit, st.st_mtime_ns if st else 0, st.st_size if st else -1))
    iz = (head, tuple(damgalar))
    IZ[wt] = (time.time(), iz)
    return iz


def teslim_edilebilir(k: dict) -> bool:
    return teslim_adayi(k)


def metin_redakte(metin: str) -> str:
    from kosu_defteri import redakte
    if metin.lstrip().startswith(('{', '[')):
        try:
            return json.dumps(veri_redakte(json.loads(metin)), ensure_ascii=False)
        except ValueError:
            pass
    metin = redakte(metin)
    metin = re.sub(r'(?i)(\b(?:authorization)\s*["\']?\s*[:=]\s*["\']?)(?:Bearer\s+|Basic\s+)?[^\s"\',}]+', r'\1[REDACTED]', metin)
    if ':' in metin or '=' in metin:
        metin = re.sub(r'(?i)(\b(?:[\w-]{0,64}(?:password|passwd|secret|api[_-]?key|private[_-]?key)[\w-]{0,64}|[\w-]{0,64}token)["\']?\s*[:=]\s*)("(?:\\.|[^"\\])*"|\'[^\']*\'|[^\s,}]+)', r'\1"[REDACTED]"', metin)
    if '://' in metin:
        metin = re.sub(r'([a-zA-Z][a-zA-Z0-9+.-]{0,64}://)[^\s/@]+:[^\s/@]+@', r'\1[REDACTED]@', metin)
    return metin


def veri_redakte(veri: Any) -> Any:
    if isinstance(veri, dict):
        return {str(k): '[REDACTED]' if re.search(r'(?i)password|passwd|secret|token$|api[_-]?key|authorization|private[_-]?key|^(env|environment)$', str(k))
                else veri_redakte(v) for k, v in veri.items()}
    if isinstance(veri, list):
        return [veri_redakte(v) for v in veri]
    return metin_redakte(veri) if isinstance(veri, str) else veri


def log_kaynaklari(home: Path, rid: str) -> list[dict]:
    root = kosu_dizini(home, rid)
    paths = [root / 'coordinator.log', root / 'dispatch.log']
    paths += sorted(root.glob('tasks/*/attempts/*/stdout.jsonl'))
    paths += sorted(root.glob('tasks/*/attempts/*/stderr*'))
    return [{'tur': 'jsonl' if p.suffix == '.jsonl' else 'log', 'yol': str(p), 'baslik': str(p.relative_to(root))}
            for p in paths if p.is_file() and not any(x.is_symlink() for x in (p, *p.parents) if x == root or root in x.parents)]


def kayit_olaylari(yol: Path, tam: bool = False) -> list[tuple[str, str, str]]:
    ofset = 0
    try:
        if yol.is_symlink() or yol.parent.is_symlink():
            return [('--:--', 'hata', t('Bağlantılı kayıt okunmadı'))]
        with yol.open('rb') as f:
            if not tam:
                f.seek(0, 2)
                ofset = max(0, f.tell() - 262144)
                f.seek(ofset)
                if ofset:
                    f.readline()
            veri = f.read().decode('utf-8', 'replace')
    except OSError:
        return []
    cikti = []
    for no, satir in enumerate(veri.splitlines(), 1):
        if not satir.strip():
            continue
        kaynak = f'{yol.name}:{no}' if not ofset else f'{yol.name}@>{ofset}+{no}'
        try:
            e = veri_redakte(json.loads(satir))
        except ValueError:
            cikti.append(('--:--', 'ham', kaynak + ' · ' + t('Bozuk JSON') + ' · ' + metin_redakte(satir)[:240]))
            continue
        if not isinstance(e, dict):
            cikti.append(('--:--', 'ham', f'{kaynak} · JSON · {str(e)[:240]}'))
            continue
        zaman = olay_saati(e.get('timestamp'))
        tur = str(e.get('type') or '?')
        bas = len(cikti)
        if tur in ('assistant', 'user'):
            bloklar = sozluk(e.get('message')).get('content')
            bas = len(cikti)
            for blok in bloklar if isinstance(bloklar, list) else []:
                if not isinstance(blok, dict):
                    continue
                if blok.get('type') in ('thinking', 'text'):
                    cikti += dusunce_olayi(zaman, blok.get('thinking') or blok.get('text'))
                elif blok.get('type') == 'tool_use':
                    cikti.append(arac_olayi(zaman, blok.get('name'), blok.get('input')))
                else:
                    cikti.append((zaman, 'sonuc' if blok.get('type') == 'tool_result' else 'ham', str(blok.get('type') or '?') + ' · ' + json.dumps(blok.get('content', blok), ensure_ascii=False)[:240]))
            if len(cikti) == bas:
                cikti.append((zaman, 'ham', f'{kaynak} · {tur} · ' + json.dumps(e, ensure_ascii=False)[:240]))
            continue
        parca = sozluk(e.get('part'))
        if tur in ('text', 'reasoning'):
            cikti += dusunce_olayi(zaman, parca.get('text'))
        elif tur == 'tool_use':
            state = sozluk(parca.get('state'))
            cikti.append(arac_olayi(zaman, parca.get('tool'), state.get('input')))
            if state.get('output') is not None:
                output = str(state['output'])
                cikti.append((zaman, 'sonuc', t('(araç çıktısı · {n} karakter)', n=len(output)) + ' · ' + ' '.join(output.split())[:240]))
        else:
            cikti.append((zaman, 'hata' if tur == 'error' else 'ham', f'{kaynak} · {tur} · ' + json.dumps(e, ensure_ascii=False)[:240]))
        if len(cikti) == bas:
            cikti.append((zaman, 'ham', f'{kaynak} · {tur} · ' + json.dumps(e, ensure_ascii=False)[:240]))
    return cikti


def beyin_olaylari(deneme: Path, tam: bool = False) -> list[tuple[str, str, str]]:
    return kayit_olaylari(deneme / 'stdout.jsonl', tam)


def dusunce_olayi(zaman: str, metin: Any) -> list[tuple[str, str, str]]:
    metin = str(metin or '').strip()
    if not metin:
        return []
    return [(zaman, 'dusunce', t('(JSON çıktı · {n} karakter)', n=len(metin)) if metin[0] in '{[' else ' '.join(metin.split()))]


def arac_olayi(zaman: str, ad: Any, girdi: Any) -> tuple[str, str, str]:
    girdi = sozluk(girdi)
    ozet = (girdi.get('command') or girdi.get('filePath') or girdi.get('file_path') or girdi.get('path') or girdi.get('pattern')
            or girdi.get('url') or '')
    return zaman, 'arac', f"{str(ad or '?'):<8} {' '.join(str(ozet).split())}"


def beyin(deneme: Path, n: int = 5) -> tuple[list[str], list[str]]:
    olaylar_ = beyin_olaylari(deneme)
    dusunce = [f'{z} {m[:150]}' for z, tur, m in olaylar_ if tur == 'dusunce'][-n:]
    arac = [f'{z} {m[:110]}' for z, tur, m in olaylar_ if tur == 'arac'][-n:]
    return dusunce, arac


def beyin_metni(deneme: Path) -> str:
    adlar = {'dusunce': t('düşünce'), 'arac': t('araç'), 'sonuc': t('Araç sonucu'), 'ham': t('Ham olay'), 'hata': t('Hata')}
    satirlar = [f"{z}  {adlar[tur]}  {m}" for z, tur, m in beyin_olaylari(deneme, tam=True)]
    return '\n'.join(satirlar) or t('(kayıt yok)')


def mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def son_deneme(home: Path, rid: str, gid: str) -> Path | None:
    try:
        denemeler = [p for p in (kosu_dizini(home, rid) / 'tasks' / gid / 'attempts').iterdir() if p.is_dir()]
    except OSError:
        return None
    return max(denemeler, key=mtime) if denemeler else None


def atamalar(home: Path, rid: str) -> dict[str, dict]:
    cikti: dict[str, dict] = {}
    try:
        satirlar = (kosu_dizini(home, rid) / 'atama-gecmisi.jsonl').read_text(errors='replace').splitlines()
    except OSError:
        return cikti
    for satir in satirlar:
        try:
            e = json.loads(satir)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get('gorev_id'):
            cikti[str(e['gorev_id'])] = e
    return cikti


def gorev_harcamalari(k: dict) -> dict[str, float]:
    cikti: dict[str, float] = {}
    for satir in k.get('budget_breakdown') or []:
        if isinstance(satir, dict):
            cikti[str(satir.get('task') or '?')] = cikti.get(str(satir.get('task') or '?'), 0.0) + sayi(satir.get('actual_usd'))
    return cikti


def gorev_sirasi(ad: str) -> tuple:
    m = re.fullmatch(r'([a-z-]+?)(\d+)', ad)
    grup = 0 if ad in ('plan', 'proje-liste') else (1 if m and m.group(1) == 'g' else 2)
    return grup, m.group(1) if m else ad, int(m.group(2)) if m else 0


def model_kisa(m: Any) -> str:
    return str(m or '').split('/')[-1] or '—'


def kosu_etkin(k: dict, ust_durum: str | None = None) -> bool:
    return k.get('state') == 'running' and ust_durum in (None, 'running')


def kosu_sonlandi(k: dict, ust_durum: str | None = None) -> bool:
    return k.get('state') in TERMINAL or ust_durum in TERMINAL


def asama_sonucu(home: Path, k: dict, gid: str, kod: str, ust_durum: str | None = None) -> tuple[str, str]:
    sonuc = {'PASS': ('bitti', 'geçti'), 'FAIL': ('hata', 'kaldı'), 'ERROR': ('hata', 'hata'),
             'CANCELLED': ('hata', 'yarım kaldı'), 'SKIPPED': ('', 'atlanmış')}.get(kod)
    if sonuc:
        return sonuc[0], t(sonuc[1])
    if kod:
        return 'hata', t('durum bilinmiyor')
    gorev = sozluk(sozluk(k.get('tasks')).get(gid))
    basladi = bool(gorev) or (kosu_dizini(home, str(k['run_id'])) / 'tasks' / gid).is_dir()
    if kosu_sonlandi(k, ust_durum):
        return ('hata', t('yarım kaldı')) if basladi else ('', t('yapılmadı'))
    if kosu_etkin(k, ust_durum) and gorev.get('state') == 'running':
        return 'simdi', t('sürüyor')
    return 'bekliyor', t('bekliyor') if basladi else t('sırada')


def ajan_dugumleri(home: Path, k: dict, onek: str = '', ust_durum: str | None = None) -> list[dict]:
    rid = str(k.get('run_id'))
    try:
        adlar = sorted((p.name for p in (kosu_dizini(home, rid) / 'tasks').iterdir() if (p / 'attempts').is_dir()), key=gorev_sirasi)
    except OSError:
        return []
    cikti = []
    for gid in adlar:
        son = son_deneme(home, rid, gid)
        if son is None:
            continue
        bilgi = kayit_oku(son / 'attempt.json')
        rol_kodu = str(bilgi.get('role') or '')
        rol = t(ROL_BILGI.get(rol_kodu, (rol_kodu or gid,))[0])
        model = str(bilgi.get('model') or '?')
        try:
            hareket = (son / 'stdout.jsonl').stat().st_mtime
        except OSError:
            hareket = 0.0
        durum = ((k.get('tasks') or {}).get(gid) or {}).get('state')
        etkin = kosu_etkin(k, ust_durum) and durum == 'running'
        yarim = kosu_sonlandi(k, ust_durum) and durum in (None, 'running', 'yarim')
        durum_yazi = t('çalışıyor') if etkin else (t('yarım kaldı') if yarim else t('bekliyor' if durum == 'running' else GOREV_YAZI.get(str(durum), 'bekliyor')))
        isaret = 'simdi' if etkin else ('hata' if yarim else ('bekliyor' if durum == 'running' else GOREV_ISARETI.get(str(durum), 'bekliyor')))
        sonuc = {}
        if gid == 'qa':
            sonuc = sozluk(sozluk(k.get('final_result')).get('qa')) or sozluk(sozluk(sozluk(k.get('tasks')).get(gid)).get('qa_result'))
        elif gid == 'kalite':
            sonuc = sozluk(sozluk(k.get('final_result')).get('review'))
        if sonuc.get('status'):
            isaret, durum_yazi = asama_sonucu(home, k, gid, str(sonuc['status']).upper(), ust_durum)
        try:
            deneme_sayisi = sum(1 for p in son.parent.iterdir() if p.is_dir())
        except OSError:
            deneme_sayisi = 1
        dusunce, arac = beyin(son)
        cikti.append(d(f'{onek}{rol} · {gid} · {model_kisa(model)}', durum_yazi, [
            ('baslik', f'{onek}{rol} · {gid}'), ('alan', (t('Rol'), rol)), ('alan', (t('Motor rolü'), rol_kodu or '—')), ('alan', (t('Model'), model)),
            ('alan', (t('Durum'), durum_yazi)), ('alan', (t('Kaydedilen durum'), str(durum or '—'))), ('alan', (t('Deneme'), str(deneme_sayisi))),
            ('alan', (t('Son hareket'), saat(hareket))), ('alan', (t('Oturum'), str(bilgi.get('session_id') or '—'))),
            ('bolum', t('Son düşünceler')), ('kayit', dusunce), ('bolum', t('Son araç çağrıları')), ('kayit', arac)],
            durum=isaret, id=f'ajan-{onek}{gid}',
            dosya={'tur': 'beyin', 'yol': str(son), 'baslik': t('{a} tam kaydı', a=f'{onek}{rol} · {gid}')}))
    return cikti


def alt_gorev_dugumu(home: Path, k: dict, g: dict, atama_: dict, harcama_: dict, ust_durum: str | None = None) -> dict:
    rid, gid = str(k['run_id']), str(g.get('id'))
    gorev = (k.get('tasks') or {}).get(gid) or {}
    st = str(gorev.get('state') or '')
    sonlandi = kosu_sonlandi(k, ust_durum)
    isaret = GOREV_ISARETI.get(st, '' if sonlandi else 'bekliyor')
    if st == 'running' and sonlandi:
        isaret, st = 'hata', 'yarim'
    elif st == 'running' and not kosu_etkin(k, ust_durum):
        isaret, st = 'bekliyor', 'bekliyor'
    elif st in ('bekliyor', 'pending', 'sirada') and sonlandi:
        isaret, st = '', ''
    tanim = ' '.join(str(g.get('tanim') or t('(tanım yok)')).split())
    atama = atama_.get(gid) or {}
    model = atama.get('fiili_model') or gorev.get('last_executor') or g.get('model')
    yazi = t(GOREV_YAZI.get(st, st)) if st else (t('yapılmadı') if sonlandi else t('sırada'))
    ayrinti = [('baslik', f'{gid} · {tanim}'), ('alan', (t('Durum'), yazi)), ('alan', (t('Ne yapılacak'), tanim)),
               ('alan', (t('Kaydedilen durum'), str(gorev.get('state') or '—'))),
               ('alan', (t('Dosyalar'), liste_yazi(g.get('dosyalar')))), ('alan', (t('Kabul'), str(g.get('kabul') or '—'))),
               ('alan', (t('Kapsam dışı'), liste_yazi(g.get('kapsam_disi')))), ('alan', (t('Bağımlı'), liste_yazi(g.get('bagimli')))),
               ('alan', (t('Zorluk'), str(g.get('zorluk', '—')))), ('alan', (t('Planlanan model'), str(g.get('model') or '—'))),
               ('alan', (t('Tahmin'), t('{dk} dk · {usd}', dk=g.get('tahmin_dk', '?'), usd=para(g.get('tahmin_usd')))))]
    ayrinti += [('bolum', t('Çalışma')), ('alan', (t('Çalışan model'), str(model or '—'))),
                ('alan', (t('Denenen modeller'), liste_yazi(atama.get('denenen_modeller')))),
                ('alan', (t('Deneme'), str(gorev.get('attempt_count') or 0))),
                ('alan', (t('Süre'), t('{d} dk', d=atama['sure_dk']) if atama.get('sure_dk') is not None else '—')),
                ('alan', (t('Harcama'), para(harcama_.get(gid, 0))))]
    if gorev.get('reason'):
        ayrinti.append(('alan', (t('Not'), str(gorev['reason'])[:200])))
    son = son_deneme(home, rid, gid)
    if son is not None:
        dusunce, arac = beyin(son)
        ayrinti += [('bolum', t('Son düşünceler')), ('kayit', dusunce), ('bolum', t('Son araç çağrıları')), ('kayit', arac)]
    istem = kosu_dizini(home, rid) / 'tasks' / gid / 'prompt.md'
    ozet = t('{d} · {m}', d=yazi, m=model_kisa(model)) if st else yazi
    return d(f'{gid} {tanim[:38]}', ozet, ayrinti, durum=isaret, id=gid,
             dosya={'tur': 'metin', 'yol': str(istem), 'baslik': t('{g} görev metni', g=gid)} if istem.exists() else None)


def alt_gorev_dugumleri(home: Path, k: dict, plan: dict, ust_durum: str | None = None) -> list[dict]:
    planli = [g for g in plan.get('gorevler') or [] if isinstance(g, dict) and g.get('id')]
    kimlikler = {str(g['id']) for g in planli}
    ek = [{'id': g} for g in (k.get('tasks') or {}) if g not in kimlikler and g not in YARDIMCI and not g.startswith(('duzeltme', 'plan-input-'))]
    atama_, harcama_ = atamalar(home, str(k['run_id'])), gorev_harcamalari(k)
    return [alt_gorev_dugumu(home, k, g, atama_, harcama_, ust_durum) for g in sorted(planli + ek, key=lambda g: gorev_sirasi(str(g['id'])))]


def plan_dugumu(home: Path, k: dict, plan: dict, alt: list[dict], ust_durum: str | None = None) -> dict:
    rid = str(k['run_id'])
    kapsam = k.get('kapsam') or {}
    biten = sum(1 for c in alt if c['durum'] == 'bitti')
    planlandi = bool(plan.get('gorevler'))
    aktif = kosu_etkin(k, ust_durum)
    gruplar = {g.get('paralel_grup') for g in plan.get('gorevler') or [] if isinstance(g, dict) and g.get('paralel_grup') is not None}
    seviye = str(kapsam.get('seviye') or '')
    ayrinti = [('baslik', t('Plan')), ('alan', (t('Planlayan'), str(k.get('plan_modeli') or '—'))),
               ('alan', (t('Kapsam puanı'), f"{kapsam.get('puan', '—')} · {t(SEVIYE_ADI.get(seviye, seviye))}" if seviye
                         else str(kapsam.get('puan', '—')))),
               ('alan', (t('Üst sınır'), para(k.get('budget_cap_usd')))),
               ('alan', (t('Alt görev'), t('{n} · {b} bitti', n=len(alt), b=biten))),
               ('alan', (t('Aynı anda'), t('{n} grup', n=len(gruplar)) if gruplar else '—'))]
    gerekce = kapsam.get('gerekce') or plan.get('kapsam_gerekce')
    if gerekce:
        ayrinti += [('bolum', t('Planlayıcının notu')), ('satir', ' '.join(str(gerekce).split())[:500])]
    if not planlandi:
        son = son_deneme(home, rid, 'plan')
        if son is not None:
            dusunce, arac = beyin(son)
            ayrinti += [('bolum', t('Planlayıcı kaydı')), ('kayit', dusunce + arac)]
    yol = kosu_dizini(home, rid) / 'plan.json'
    if planlandi and biten == len(alt):
        durum = 'bitti'
    elif aktif and (any(c['durum'] == 'simdi' for c in alt) or sozluk(sozluk(k.get('tasks')).get('plan')).get('state') == 'running'):
        durum = 'simdi'
    else:
        durum = 'hata' if planlandi and kosu_sonlandi(k, ust_durum) else ('' if kosu_sonlandi(k, ust_durum) else 'bekliyor')
    ozet = t('{n} alt görev · {b} bitti', n=len(alt), b=biten) if planlandi else (t('planlanıyor') if durum == 'simdi' else t('plan yok'))
    return d(t('Plan'), ozet, ayrinti, alt, durum, id='plan',
             aciklama=aciklama(t('Plan'), t('Planlayıcının verdiği alt görevler. Hepsi baştan listelenir, yazıldıkça işaretlenir. o ile plan dosyası okunur.')),
             dosya={'tur': 'plan', 'yol': str(yol), 'baslik': t('Plan dosyası')} if yol.exists() else None)


def test_dugumu(home: Path, k: dict, ust_durum: str | None = None) -> dict:
    gorevler = k.get('tasks') or {}
    qa = (k.get('final_result') or {}).get('qa') or (gorevler.get('qa') or {}).get('qa_result') or {}
    denemeler = [a for a in qa.get('attempts') or [] if isinstance(a, dict)]
    son = denemeler[-1] if denemeler else {}
    kod = str(qa.get('status') or '').upper()
    isaret, sonuc = asama_sonucu(home, k, 'qa', kod, ust_durum)
    cikti = str(son.get('output') or '')
    ayrinti = [('baslik', t('Test')), ('alan', (t('Sonuç'), sonuc)), ('alan', (t('Kaydedilen durum'), kod or str(sozluk(gorevler.get('qa')).get('state') or '—'))),
               ('alan', (t('Komut'), str(qa.get('command') or (k.get('qa') or {}).get('command') or '—'))),
               ('alan', (t('Deneme'), str(len(denemeler)))),
               ('alan', (t('Süre'), t('{s} sn', s=round(sayi(son.get('seconds')))) if son.get('seconds') is not None else '—')),
               ('alan', (t('Çıkış kodu'), str(son.get('exit_code', '—'))))]
    satirlar = son_satirlar(cikti, 12) if cikti else []
    if satirlar:
        ayrinti += [('bolum', t('Son çıktı')), ('kayit', satirlar)]
    atama_, harcama_ = atamalar(home, str(k['run_id'])), gorev_harcamalari(k)
    duzeltmeler = [alt_gorev_dugumu(home, k, {'id': g, 'tanim': t('Test kırmızıydı, düzeltme turu')}, atama_, harcama_, ust_durum)
                   for g in sorted(gorevler, key=gorev_sirasi) if g.startswith('duzeltme')]
    return d(t('Test'), sonuc, ayrinti, duzeltmeler, isaret, id='test',
             aciklama=aciklama(t('Test'), t('Tüm alt görevler bitince bir kez koşar. Kırmızıysa düzeltme turu açılır, turlar burada listelenir.')),
             dosya={'tur': 'log', 'yol': cikti, 'baslik': t('Test çıktısı')} if cikti and Path(cikti).is_file() else None)


def hakem_dugumu(home: Path, k: dict, ust_durum: str | None = None) -> dict:
    rv = (k.get('final_result') or {}).get('review') or {}
    kod = str(rv.get('status') or '').upper()
    isaret, sonuc = asama_sonucu(home, k, 'kalite', kod, ust_durum)
    karar = str(rv.get('verdict') or '').upper()
    if karar == 'GECER' and kod == 'PASS':
        sonuc = t('geçer')
    bagimsiz = str(rv.get('independent')).lower() in ('true', '1')
    ayrinti = [('baslik', t('Hakem')), ('alan', (t('Karar'), sonuc)), ('alan', (t('Kaydedilen durum'), kod or str(sozluk(sozluk(k.get('tasks')).get('kalite')).get('state') or '—'))), ('alan', (t('Hakem modeli'), str(rv.get('model') or '—'))),
               ('alan', (t('Bağımsız'), (t('evet') if bagimsiz else t('hayır')) if rv else '—')),
               ('alan', (t('Yazan'), liste_yazi(rv.get('writers')))), ('alan', (t('Dışarıda bırakılan'), liste_yazi(rv.get('excluded_writers')))),
               ('alan', (t('Onarım'), f"{rv.get('repairs', 0)} / {rv.get('repair_limit', '—')}" if rv else '—'))]
    maddeler = [m for m in rv.get('acceptance') or [] if isinstance(m, dict)]
    if maddeler:
        ayrinti += [('bolum', t('Kabul maddeleri')), ('kayit', [
            f"{'●' if str(m.get('status')).upper() == 'PASS' else '✗'} {m.get('id')} · {m.get('status')}   "
            f"{' '.join(str(m.get('evidence') or '').split())[:160]}" for m in maddeler])]
    try:
        metinler = sorted((kosu_dizini(home, k['run_id']) / 'tasks' / 'kalite').glob('review-*.txt'), key=mtime)
    except OSError:
        metinler = []
    return d(t('Hakem'), sonuc, ayrinti, durum=isaret, id='hakem',
             dosya={'tur': 'metin', 'yol': str(metinler[-1]), 'baslik': t('Hakem metni')} if metinler else None)


def git_cikti(wt: str, *args: str, zaman: float = 10) -> str:
    try:
        r = subprocess.run(['git', '-C', wt, *args], capture_output=True, text=True, timeout=zaman)
    except (OSError, subprocess.SubprocessError):
        return ''
    return r.stdout if r.returncode == 0 else ''


def teslim_bilgisi(k: dict) -> dict:
    wt, taban = str(k.get('worktree') or ''), str(k.get('source_sha') or '')
    if not wt or not taban or not Path(wt).is_dir():
        return {}
    return {'dal': git_cikti(wt, 'branch', '--show-current').strip(), 'kisa': git_cikti(wt, 'diff', '--shortstat', f'{taban}..HEAD').strip(),
            'stat': git_cikti(wt, 'diff', '--stat', f'{taban}..HEAD').splitlines()[-15:],
            'log': git_cikti(wt, 'log', '--oneline', '-10', f'{taban}..HEAD').splitlines(), 'wt': wt, 'taban': taban}


def degisiklik_ozeti(kisa: str) -> str:
    sayilar = {tur: int(n) for n, tur in re.findall(r'(\d+) (file|insertion|deletion)', kisa or '')}
    if not sayilar:
        return '—'
    return t('{f} dosya · +{e} −{s}', f=sayilar.get('file', 0), e=sayilar.get('insertion', 0), s=sayilar.get('deletion', 0))


def teslim_dugumu(k: dict, teslim: bool, bilgi: dict) -> dict:
    if 'teslim_merge' in k:
        yazi, isaret = t('merge edildi · {z}', z=k['teslim_merge']), 'bitti'
    elif 'teslim_push' in k:
        yazi, isaret = t('push edildi · {z}', z=k['teslim_push']), 'bitti'
    elif teslim:
        yazi, isaret = t('teslim bekliyor'), 'bekliyor'
    else:
        yazi, isaret = t('teslim edilemez'), ''
    ayrinti = [('baslik', t('Teslim')), ('alan', (t('Durum'), yazi)), ('alan', (t('Dal'), bilgi.get('dal') or '—')),
               ('alan', (t('Taban'), str(k.get('source_sha') or '—')[:10])), ('alan', (t('Değişiklik'), degisiklik_ozeti(bilgi.get('kisa', ''))))]
    if not teslim and not bilgi:
        ayrinti.append(('soluk', t('Çalışma ağacı silinmiş ya da görev onay almamış.')))
    if bilgi.get('stat'):
        ayrinti += [('bolum', t('Değişen dosyalar')), ('kayit', bilgi['stat'])]
    if bilgi.get('log'):
        ayrinti += [('bolum', t('Commitler')), ('kayit', bilgi['log'])]
    if teslim:
        ayrinti += [('bolum', t('Ne yapmalısın')), ('satir', t('p push eder, m merge eder. İkisi de önce komutu gösterir, sonra iki onay ister.'))]
    dosya = {'tur': 'diff', 'komut': ['git', '-C', bilgi['wt'], 'diff', f"{bilgi['taban']}..HEAD"], 'baslik': t('Değişiklikler')} if bilgi else None
    return d(t('Teslim'), yazi, ayrinti, durum=isaret, id='teslim', teslim=teslim or None, kosu=k['run_id'], proje=k.get('project'), dosya=dosya)


def rapor_dugumu(home: Path, k: dict) -> dict | None:
    rapor = k.get('rapor') if isinstance(k.get('rapor'), dict) else {}
    if k.get('mode') == 'rapor' and rapor.get('yol'):
        yol, bolumler = Path(str(k.get('worktree') or '')) / str(rapor['yol']), rapor.get('bolumler') or []
    elif (kosu_dizini(home, k['run_id']) / 'proje-raporu.md').exists():
        yol, bolumler = kosu_dizini(home, k['run_id']) / 'proje-raporu.md', []
    else:
        return None
    var = yol.is_file()
    ayrinti = [('baslik', t('Rapor')), ('alan', (t('Dosya'), str(yol))), ('alan', (t('Durum'), t('hazır') if var else t('henüz yazılmadı')))]
    if bolumler:
        ayrinti.append(('alan', (t('Bölümler'), liste_yazi(bolumler))))
    analist = rapor.get('analist') if isinstance(rapor.get('analist'), dict) else None
    if analist:
        if analist.get('kip') == 'mutabakat':
            yazi = t('iki bağımsız analist ({a}, {b}) ve uzlaştırıcı ({c})', a=analist.get('birinci') or '?', b=analist.get('ikinci') or '?',
                     c=analist.get('uzlastirici') or '?')
        else:
            yazi = t('tek analist ({m}): {n}', m=analist.get('birinci') or '?', n=analist.get('neden') or t('ikinci güçlü model yok'))
        ayrinti.append(('alan', (t('Analist'), yazi)))
    if var:
        try:
            ilk = [s.strip() for s in yol.read_text(errors='replace').splitlines() if s.strip()][:8]
        except OSError:
            ilk = []
        ayrinti += [('bolum', t('Başı')), ('kayit', [s[:150] for s in ilk])]
    return d(t('Rapor'), t('hazır') if var else t('yazılıyor'), ayrinti, durum='bitti' if var else 'bekliyor', id='rapor',
             dosya={'tur': 'md', 'yol': str(yol), 'baslik': t('Rapor')} if var else None)


def proje_kosulari(proje: dict) -> list[str]:
    kimlikler = {str(c['kosu']) for c in proje.get('cocuklar') or [] if isinstance(c, dict) and c.get('kosu')}
    for f in proje.get('ozellikler') or []:
        if isinstance(f, dict) and isinstance(f.get('kosu'), list):
            kimlikler.update(str(x) for x in f['kosu'] if x)
    return sorted(kimlikler)


def bilinen_micro(value: Any) -> int | None:
    from aylik_butce import MonthlyBudgetDenied, usd_micro
    if value is None:
        return None
    try:
        return usd_micro(value, zero=True)
    except MonthlyBudgetDenied:
        return None


def para_bilinen(micro: int | None) -> str:
    return para(micro / 1e6) if micro is not None else t('bilinmiyor')


def ekonomi_kapsami(home: Path, k: dict, proje: dict) -> list[str]:
    ids = {str(k['run_id']), *proje_kosulari(proje)}
    ids.update(str(r['run_id']) for r in kosular(home) if r.get('proje_ust') == k['run_id'])
    return sorted(ids)


def ekonomi_oku(home: Path, ids: list[str]) -> dict:
    from butce_kapisi import USD_PROVIDERS
    db = home / 'zincir/request-budget.db'
    rows = []
    known = True
    if db.exists():
        source = t('İstek defteri')
        try:
            with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as con:
                found = con.execute("select provider,reserved,actual,receipt,metadata from reservation where "
                                    "json_extract(metadata,'$.run_id') in (%s) and provider in (%s)"
                                    % (','.join('?' * len(ids)), ','.join('?' * len(USD_PROVIDERS))), (*ids, *sorted(USD_PROVIDERS))).fetchall()
            for provider, reserved, actual, receipt, metadata in found:
                if type(reserved) is not int or not 0 <= reserved < 2**53 or (actual is not None and (type(actual) is not int or not 0 <= actual < 2**53)):
                    raise ValueError('invalid ledger amount')
                meta = json.loads(metadata)
                rows.append({'run': str(meta['run_id']), 'task': str(meta.get('task') or meta.get('task_id') or '?'),
                             'attempt': str(meta.get('attempt_id') or '?'), 'model': str(meta.get('model') or '?'),
                             'provider': provider, 'actual': actual, 'held': reserved if actual is None else 0,
                             'kind': 'estimated' if str(receipt or '').startswith('hayalet:') else 'measured'})
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            known, rows = False, []
    else:
        source = t('Koşu özeti · defter yok')
        for rid in ids:
            record = kayit_oku(kosu_dizini(home, rid) / 'run.json')
            spent, held = bilinen_micro(record.get('budget_spent_usd')), bilinen_micro(record.get('budget_held_usd'))
            rows.append({'run': rid, 'task': '?', 'attempt': '?', 'model': '?', 'provider': '?',
                         'actual': spent, 'held': held, 'kind': 'summary', 'valid': spent is not None and held is not None})
    return {'rows': rows, 'known': known, 'source': source, 'ids': ids}


def ekonomi_topla(data: dict, rows: list[dict] | None = None) -> dict:
    rows = data['rows'] if rows is None else rows
    known = data['known'] and all(r.get('valid', True) for r in rows)
    split = known and all(r['kind'] != 'summary' for r in rows)
    return {'spent': sum(r['actual'] or 0 for r in rows) if known else None,
            'held': sum(r['held'] or 0 for r in rows) if known else None,
            'measured': sum(r['actual'] or 0 for r in rows if r['kind'] == 'measured') if split else None,
            'estimated': sum(r['actual'] or 0 for r in rows if r['kind'] == 'estimated') if split else None,
            'cash': sum(r['actual'] or 0 for r in rows if r['provider'] in ('openrouter', 'opencode')) if split else None,
            'equivalent': sum(r['actual'] or 0 for r in rows if r['provider'] in ('opencode-go', 'claude')) if split else None}


def harcama_dugumu(home: Path, k: dict, proje: dict) -> dict:
    ids = ekonomi_kapsami(home, k, proje)
    data = ekonomi_oku(home, ids)
    cap = next((bilinen_micro(x) for x in (k.get('project_budget_cap_usd'), proje.get('tavan_usd'), k.get('budget_cap_usd')) if x is not None), None)

    def node(name: str, key: str, rows: list[dict], scope: str, children: list[dict] | None = None, ceiling: int | None = None) -> dict:
        totals = ekonomi_topla(data, rows)
        detail = [('baslik', name), ('alan', (t('Harcanan'), para_bilinen(totals['spent']))), ('alan', (t('Askıda'), para_bilinen(totals['held']))),
                  ('alan', (t('Kapsam'), scope)), ('alan', (t('Kaynak'), data['source'])),
                  ('alan', (t('Ölçülen'), para_bilinen(totals['measured']))), ('alan', (t('Tahmini'), para_bilinen(totals['estimated'])))]
        detail += [('alan', (t('Nakit'), para_bilinen(totals['cash']))), ('alan', (t('Kullanım eşdeğeri'), para_bilinen(totals['equivalent'])))]
        if ceiling is not None or key == 'harcama':
            detail.append(('alan', (t('Üst sınır'), para_bilinen(ceiling))))
        if data['known']:
            for label, group_key in (('Sağlayıcıya göre', lambda r: t(SAGLAYICI_ADI.get(r['provider'], 'bilinmiyor'))),
                                     ('Alt göreve ve modele göre', lambda r: f"{model_kisa(r['model'])} · {r['task']}")):
                groups: dict[str, list[dict]] = {}
                for row in rows:
                    groups.setdefault(group_key(row), []).append(row)
                detail.append(('bolum', t(label)))
                for group, entries in sorted(groups.items()):
                    cost = ekonomi_topla(data, entries)
                    detail.append(('alan', (group, t('{s} · askıda {h}', s=para_bilinen(cost['spent']), h=para_bilinen(cost['held'])))))
        else:
            detail.append(('satir', t('Maliyet kaydı eksik veya bozuk. Bilinmeyen tutar sıfır sayılmaz.')))
        summary = para_bilinen(totals['spent']) + (f' / {para_bilinen(ceiling)}' if ceiling is not None or key == 'harcama' else '')
        return d(name, summary, detail, children, id=key, ekonomi=totals, kapsam=scope,
                 aciklama=aciklama(t('Harcama'), t('Defter toplamı: nakit USD ve Go/Claude kullanım eşdeğeri ayrı gösterilir. Abonelik bedelleri dahil değildir. Koşu, çocuk ve deneme aynı satırları kullanır.')))

    children = []
    for rid in ids:
        rows = [r for r in data['rows'] if r['run'] == rid]
        attempts = []
        for task, attempt in sorted({(r['task'], r['attempt']) for r in rows}):
            selected = [r for r in rows if (r['task'], r['attempt']) == (task, attempt)]
            attempts.append(node(f'{task} · {attempt}', f'{task}:{attempt}', selected, f'{rid} / {task} / {attempt}'))
        children.append(node(rid, rid, rows, rid, attempts))
    result = node(t('Harcama'), 'harcama', data['rows'], t('Ana koşu ve {n} çocuk · bütün denemeler', n=len(ids) - 1), children, cap)
    allocation = k.get('budget_allocation') or {}
    if allocation:
        result['ayrinti'] += [('bolum', t('İlk iç pay tahmini')),
                             ('alan', (t('Sabit operatör toplamı'), para_bilinen(bilinen_micro(allocation.get('amount_usd'))))),
                             ('soluk', t('Nakit, Go ve Claude ayrı sınırları paylaşır. İç pay harcanmış para değildir.'))]
        for name, value in (allocation.get('initial_slice_estimates_micro') or {}).items():
            result['ayrinti'].append(('alan', (t({'seritler': 'Ana iş', 'hakem': 'Son kabul', 'yardimci': 'Yardımcı'}.get(name, name)),
                                              para_bilinen(value if type(value) is int and value >= 0 else None))))
        for stage in allocation.get('stages') or []:
            label = {'plan': 'Planlama', 'plan-review': 'Plan incelemesi', 'impl': 'Ana iş', 'qa': 'Test', 'kalite': 'Son kabul', 'repair': 'Onarım'}.get(stage.get('role'), '?')
            value = para_bilinen(stage.get('estimate_micro'))
            if stage.get('provider'):
                value += ' · ' + str(stage['provider'])
            result['ayrinti'].append(('alan', (t(label), value)))
        result['ayrinti'].append(('soluk', t('Aşama maliyeti ölçülmediyse bilinmiyor olarak kalır.')))
        if allocation.get('estimate_source'):
            result['ayrinti'].append(('soluk', t('Aşama tutarı tek çağrı üst sınırıdır. Toplam çağrı sayısı henüz bilinmez.')))
    result['ayrinti'] += [('bolum', t('Koşular')), ('kayit', ids), ('soluk', t('? kimlik, defterde görev/deneme/model bağı bulunmadığını gösterir.'))]
    try:
        from verim import kirilim, ozet_satirlari
        result['ayrinti'] += [('bolum', t('Aşama kırılımı')), ('kayit', ozet_satirlari(kirilim(home, str(k['run_id'])))),
                              ('soluk', t('Token/cache defterde yoksa bilinmiyor. Bekleme süresi model yavaşlığı değildir.'))]
    except (OSError, ValueError, KeyError, TypeError):
        result['ayrinti'].append(('soluk', t('Aşama kırılımı okunamadı.')))
    if (home / 'zincir/request-budget.db').exists() and data['known']:
        try:
            with closing(sqlite3.connect((home / 'zincir/request-budget.db').resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as con:
                if con.execute("select 1 from sqlite_master where name='role_allocation'").fetchone():
                    for rid, pool, raw in con.execute('select run_id,pool,caps from role_allocation order by run_id,pool'):
                        if rid in ids:
                            result['ayrinti'].append(('bolum', t('İç pay aktarımı') + f' · {rid} · {pool}'))
                            for name, value in json.loads(raw).items():
                                result['ayrinti'].append(('alan', (t({'seritler': 'Ana iş', 'hakem': 'Son kabul', 'yardimci': 'Yardımcı'}.get(name, name)),
                                                                  para_bilinen(value))))
        except (OSError, sqlite3.Error, ValueError, TypeError):
            result['ayrinti'].append(('soluk', t('İç pay kaydı okunamadı.')))
        snapshots = [kayit_oku(kosu_dizini(home, rid) / 'run.json') for rid in ids]
        recorded = [bilinen_micro(r.get('budget_spent_usd')) for r in snapshots]
        if all(x is not None for x in recorded) and sum(recorded) != result['ekonomi']['spent']:
            result['ayrinti'].append(('satir', t('Koşu özeti ile defter farklı. Bu görünüm defteri kullanır.')))
    return result


def olaylar_dugumu(home: Path, rid: str) -> dict:
    yol = kosu_dizini(home, rid) / 'dispatch.log'
    satirlar = olaylar(home, rid, 30, mutlak=True)
    kaynaklar = [d(s['baslik'], t('Maskeli kayıt'), [('alan', (t('Kaynak'), s['baslik']))], id=s['baslik'], dosya=s)
                 for s in log_kaynaklari(home, rid)]
    return d(t('Olaylar'), t('{n} olay', n=len(satirlar)), [('baslik', t('Olaylar')), ('kayit', satirlar)], cocuk=kaynaklar, id='olaylar',
             dosya={'tur': 'iz', 'home': str(home), 'run_id': rid, 'baslik': t('Birleştirilmiş olay izi')})


def _iz_kaynagi(home: Path, relative: Path) -> tuple[bytes, float, bool]:
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('unsafe trace path')
    directory = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in relative.parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 2_000_000:
            raise ValueError('trace source type or size limit')
        raw = stream.read(2_000_001)
        after = os.fstat(stream.fileno())
    if len(raw) > 2_000_000:
        raise ValueError('trace source size limit')
    return raw, after.st_mtime, (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)


def olay_izi(home: Path, rid: str) -> dict:
    from kosu import identifier, plan_digest
    identifier(rid)
    sources, events, issues = [], {}, []
    records: dict[str, dict] = {}
    attempts: dict[tuple[str, str, str], dict] = {}
    used_bytes = 0

    def read(relative: Path, required: bool = True) -> bytes | None:
        nonlocal used_bytes
        if len(sources) >= 1000 or used_bytes >= 4_000_000:
            if not any(i['issue'] == 'trace-limit' for i in issues):
                issues.append({'source': 'trace', 'issue': 'trace-limit'})
            return None
        try:
            raw, modified, changed = _iz_kaynagi(home, relative)
        except FileNotFoundError:
            if required:
                issues.append({'source': str(relative), 'issue': 'missing'})
            return None
        except (OSError, ValueError):
            issues.append({'source': str(relative), 'issue': 'unreadable-or-limit'})
            return None
        if used_bytes + len(raw) > 4_000_000:
            issues.append({'source': str(relative), 'issue': 'trace-limit'})
            return None
        sources.append({'path': str(relative), 'mtime': modified, 'sha256': hashlib.sha256(raw).hexdigest(), 'stable': not changed})
        used_bytes += len(raw)
        if changed:
            issues.append({'source': str(relative), 'issue': 'changed-during-read'})
        return raw

    def add(run: str, relative: Path, line: int, payload: Any, kind: str, task: str | None = None,
            attempt: str | None = None, location: str | None = None) -> None:
        if len(events) >= 5000:
            if not any(i['issue'] == 'event-limit' for i in issues):
                issues.append({'source': 'trace', 'issue': 'event-limit'})
            return
        clean = veri_redakte(payload)
        data = clean if isinstance(clean, dict) else {}
        task = task or data.get('task_id') or data.get('gorev_id')
        attempt = attempt or data.get('attempt_id')
        context = attempts.get((run, str(task), str(attempt)), {})
        revision = data.get('revision', data.get('plan_revision', context.get('plan_revision')))
        request_hash = data.get('request_hash', context.get('request_hash', records.get(run, {}).get('request_input_hash')))
        evidence = data.get('evidence_status', 'recorded') if kind in ('plan_change', 'plan_review', 'plan_revision') else 'recorded'
        correlation = 'recorded' if revision is not None and request_hash else 'historical-unknown'
        if ((revision is not None and context.get('plan_revision') is not None and revision != context['plan_revision'])
                or (request_hash and records.get(run, {}).get('request_input_hash') and request_hash != records[run]['request_input_hash'])):
            issues.append({'source': str(relative), 'issue': 'correlation-conflict'})
            evidence = 'conflict'
        stamp = data.get('timestamp', data.get('ts', data.get('updated') if kind == 'snapshot' else None))
        timestamp = None
        if type(stamp) in (int, float) and 0 < stamp < 10**16:
            timestamp = float(stamp) / (1000 if stamp > 100_000_000_000 else 1)
            if not timestamp < 253_402_300_800:
                timestamp = None
        elif isinstance(stamp, str):
            try:
                parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                if parsed.tzinfo is not None:
                    timestamp = parsed.timestamp()
            except (ValueError, OverflowError, OSError):
                pass
        identity = [run, str(relative), kind, clean]
        key = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
        if key in events:
            events[key]['lines'].append(line)
            if location:
                events[key]['locations'].append(location)
            return
        events[key] = {'id': key, 'run_id': run, 'root_run_id': rid, 'parent_run_id': records.get(run, {}).get('proje_ust') or rid,
                       'child_id': run if run != rid else None, 'project': records.get(run, {}).get('project'), 'task_id': task,
                       'attempt_id': attempt, 'request_id': data.get('request_id'), 'request_hash': request_hash,
                       'source_sha': records.get(run, {}).get('source_sha'),
                       'revision': revision, 'change_id': data.get('change_id'), 'evidence_status': evidence,
                       'correlation_status': correlation, 'source_kind': kind,
                       'timestamp': timestamp, 'type': str(data.get('type') or kind), 'source': str(relative),
                       'lines': [line], 'locations': [location] if location else [], 'data': clean}

    def journal(run: str, relative: Path, record: dict) -> None:
        history, reviews = record.get('plan_history', []), record.get('plan_reviews', [])
        if not isinstance(history, list) or not isinstance(reviews, list):
            issues.append({'source': str(relative), 'issue': 'malformed-plan-history'})
            return
        if len(history) + len(reviews) > 5000:
            issues.append({'source': str(relative), 'issue': 'event-limit'})
            return
        valid = set()
        valid_reviews = []
        for index, entry in enumerate(history):
            if not isinstance(entry, dict):
                issues.append({'source': str(relative), 'issue': 'malformed-plan-revision'})
                continue
            revision = entry.get('revision')
            good = type(revision) is int and revision == index + 1 and isinstance(entry.get('plan'), dict)
            if good:
                good = entry.get('digest') == plan_digest(entry['plan']) and entry.get('request_hash') == record.get('request_input_hash')
            if good:
                valid.add(revision)
            else:
                issues.append({'source': str(relative), 'issue': 'plan-revision-conflict'})
            add(run, relative, 1, {**entry, 'type': 'plan_revision', 'timestamp': entry.get('created'),
                'evidence_status': 'recorded' if good else 'conflict'}, 'plan_revision', location=f'/plan_history/{index}')
        for index, review in enumerate(reviews):
            if not isinstance(review, dict):
                issues.append({'source': str(relative), 'issue': 'malformed-plan-review'})
                continue
            revision = review.get('revision_number')
            good = type(revision) is int and (revision in valid or revision == 0 and review.get('status') == 'ERROR')
            if review.get('status') == 'PASS':
                good = good and bool(review.get('attempt_id') and review.get('session_id'))
                if good:
                    good = review.get('plan_digest') == history[revision - 1].get('digest')
            if not good:
                issues.append({'source': str(relative), 'issue': 'plan-review-conflict'})
            else:
                valid_reviews.append(review)
            add(run, relative, 1, {**review, 'type': 'plan_review', 'review_identity': review.get('revision'), 'revision': revision, 'timestamp': review.get('created'),
                'evidence_status': 'recorded' if good else 'conflict'}, 'plan_review', location=f'/plan_reviews/{index}')
        changes = record.get('plan_changes', [])
        if not isinstance(changes, list):
            issues.append({'source': str(relative), 'issue': 'malformed-plan-changes'})
            return
        for index, change in enumerate(changes):
            if not isinstance(change, dict) or not isinstance(change.get('events'), list):
                issues.append({'source': str(relative), 'issue': 'malformed-plan-change'})
                continue
            revision, stage, previous, seen = change.get('base_revision'), None, None, {}
            for number, event in enumerate(change['events']):
                if not isinstance(event, dict):
                    issues.append({'source': str(relative), 'issue': 'malformed-plan-change-event'})
                    continue
                encoded = json.dumps(event, sort_keys=True, allow_nan=False)
                action, stamp = event.get('action'), event.get('ts')
                duplicate = encoded in seen
                good = type(revision) is int and revision in valid and type(stamp) in (int, float) and 0 < stamp < 253_402_300_800
                if good and previous is not None and stamp < previous:
                    good = False
                target = event.get('revision', revision)
                stop = change.get('stop')
                if action == 'requested':
                    good = good and stage is None
                elif action == 'stopped':
                    good = good and stage == 'requested' and isinstance(stop, dict) and isinstance(stop.get('runs'), list) and run in stop['runs'] and isinstance(stop.get('cost'), dict)
                    good = good and type(stop.get('ts')) in (int, float) and previous <= stop['ts'] <= stamp
                elif action == 'revision_saved':
                    good = good and stage in ('stopped', 'review_rejected') and type(target) is int and target in valid and target > revision
                elif action == 'review_rejected':
                    good = good and stage == 'revision_saved' and any(r.get('status') in ('FAIL', 'ERROR') and r.get('revision_number') == revision
                        and type(r.get('created')) in (int, float) and r['created'] <= stamp for r in valid_reviews)
                elif action == 'withdrawn':
                    good = good and stage in ('stopped', 'review_rejected')
                elif action == 'resumed':
                    good = good and stage in ('revision_saved', 'review_rejected', 'withdrawn') and target == revision
                    good = good and any(isinstance(r, dict) and r.get('status') == 'PASS' and r.get('revision_number') == target
                                        and type(r.get('created')) in (int, float) and r['created'] <= stamp for r in valid_reviews)
                else:
                    good = False
                if duplicate:
                    add(run, relative, 1, seen[encoded], 'plan_change', location=f'/plan_changes/{index}/events/{number}')
                    continue
                if not good:
                    issues.append({'source': str(relative), 'issue': 'plan-change-order-or-evidence-conflict'})
                payload = {**event, 'type': 'plan_change:' + str(action), 'change_id': change.get('id'),
                           'revision': target, 'base_revision': change.get('base_revision'), 'origin': change.get('origin'),
                           'request_hash': record.get('request_input_hash'), 'evidence_status': 'recorded' if good else 'conflict'}
                if action == 'requested':
                    payload['text'] = change.get('text')
                if action == 'stopped':
                    payload['stop'] = stop
                seen[encoded] = payload
                add(run, relative, 1, payload, 'plan_change', location=f'/plan_changes/{index}/events/{number}')
                if good:
                    stage, revision, previous = action, target, stamp

    candidates = [rid]
    descendants: dict[str, list[str]] = {}
    runs_root = home / 'runs'
    discovery_bytes = 0
    if not runs_root.is_symlink():
        for index, candidate in enumerate(sorted(runs_root.glob('*/run.json'))):
            if index >= 1000 or discovery_bytes >= 4_000_000:
                issues.append({'source': 'runs', 'issue': 'discovery-limit'})
                break
            if candidate.parent.name == rid:
                continue
            try:
                identifier(candidate.parent.name)
                raw, _, _ = _iz_kaynagi(home, candidate.relative_to(home))
                discovery_bytes += len(raw)
                record = json.loads(raw)
            except (OSError, ValueError):
                continue
            if isinstance(record, dict) and isinstance(record.get('proje_ust'), str):
                descendants.setdefault(record['proje_ust'], []).append(candidate.parent.name)
    candidates.extend(descendants.get(rid, []))
    visited: set[str] = set()
    for run in candidates:
        if run in visited:
            continue
        try:
            identifier(run)
        except ValueError:
            issues.append({'source': 'proje.json', 'issue': 'invalid-child-id'})
            continue
        if len(visited) >= 200:
            issues.append({'source': 'runs', 'issue': 'run-limit'})
            break
        visited.add(run)
        candidates.extend(child for child in descendants.get(run, []) if child not in candidates)
        root = Path('runs') / run
        mirror = None
        for name in ('run.json', 'proje.json', 'plan.json'):
            relative = root / name
            raw = read(relative, required=name == 'run.json')
            if raw is None:
                continue
            try:
                record = json.loads(raw, parse_constant=lambda value: {'invalid_json_number': value})
                if not isinstance(record, dict):
                    raise ValueError('trace snapshot must be an object')
            except ValueError:
                add(run, relative, 1, raw.decode('utf-8', 'replace'), 'malformed')
                issues.append({'source': str(relative), 'issue': 'malformed-json'})
                continue
            if name == 'run.json':
                records[run] = record
                if record.get('run_id') != run:
                    issues.append({'source': str(relative), 'issue': 'run-id-mismatch'})
                journal(run, relative, record)
            elif name == 'proje.json':
                candidates.extend(child for child in proje_kosulari(record) if child not in candidates)
            elif name == 'plan.json':
                mirror = record
            add(run, relative, 1, record, 'snapshot')
        history = records.get(run, {}).get('plan_history')
        if isinstance(history, list) and history and isinstance(history[-1], dict):
            if mirror is None or plan_digest(mirror) != history[-1].get('digest'):
                issues.append({'source': str(root / 'plan.json'), 'issue': 'execution-plan-mismatch'})
        base = home / root
        paths = [root / 'coordinator.log', root / 'dispatch.log']
        if not base.is_symlink():
            paths += [p.relative_to(home) for pattern in ('tasks/*/attempts/*/attempt.json', 'tasks/*/attempts/*/stdout.jsonl',
                                                         'tasks/*/attempts/*/stderr.log') for p in sorted(base.glob(pattern))]
        for relative in paths:
            raw = read(relative, required=False)
            if raw is None:
                continue
            parts = relative.parts
            task, attempt = (parts[3], parts[5]) if len(parts) == 7 else (None, None)
            text = raw.decode('utf-8', 'replace')
            text = re.sub(r'-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)',
                          lambda match: '[REDACTED]' + '\n' * match.group(0).count('\n'), text)
            lines = [text] if relative.name == 'attempt.json' else text.splitlines()
            for number, line in enumerate(lines, 1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line, parse_constant=lambda value: {'invalid_json_number': value})
                    kind = 'snapshot' if relative.name == 'attempt.json' else 'event'
                except ValueError:
                    match = re.match(r'^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})?)\s+(.+)$', line)
                    payload = {'timestamp': match[1], 'text': match[2]} if match else line
                    kind = 'log' if relative.suffix == '.log' else 'malformed'
                if relative.name == 'attempt.json' and isinstance(payload, dict):
                    if (payload.get('run_id'), payload.get('task_id'), payload.get('attempt_id')) == (run, task, attempt):
                        attempts[(run, str(task), str(attempt))] = payload
                    else:
                        issues.append({'source': str(relative), 'issue': 'attempt-identity-conflict'})
                add(run, relative, number, payload, kind, task, attempt)
    for event in events.values():
        if event['attempt_id'] and not event['task_id']:
            matches = [context for (run, _, attempt), context in attempts.items() if run == event['run_id'] and attempt == event['attempt_id']]
            if len(matches) == 1:
                event['task_id'] = matches[0]['task_id']
                if event['revision'] is not None and matches[0].get('plan_revision') is not None and event['revision'] != matches[0]['plan_revision']:
                    event['evidence_status'] = 'conflict'
                    issues.append({'source': event['source'], 'issue': 'correlation-conflict'})
    route = Path('zincir/yonlendirici.jsonl')
    raw = read(route, required=False)
    if raw is not None:
        for number, line in enumerate(raw.decode('utf-8', 'replace').splitlines(), 1):
            if number > 5000:
                issues.append({'source': str(route), 'issue': 'route-event-limit'})
                break
            try:
                payload = json.loads(line, parse_constant=lambda value: {'invalid_json_number': value})
                if not isinstance(payload, dict):
                    raise ValueError('invalid route event')
            except ValueError:
                issues.append({'source': str(route), 'issue': 'malformed-route-event'})
                continue
            if payload.get('run_id') in visited:
                add(payload['run_id'], route, number, payload, str(payload.get('olay') or 'route'))
    ledger = Path('zincir/request-budget.db')
    raw = read(ledger)
    if raw is not None:
        wal = read(Path(str(ledger) + '-wal'), required=False)
        ledger_sources = [s for s in sources if s['path'] in (str(ledger), str(ledger) + '-wal')]
        unsafe = any(not s['stable'] for s in ledger_sources) or any(i['source'] == str(ledger) + '-wal' for i in issues)
        try:
            if unsafe:
                raise ValueError('unstable ledger')
            with tempfile.TemporaryDirectory(prefix='cadre-trace-') as temporary:
                copy = Path(temporary) / ledger.name
                copy.write_bytes(raw)
                if wal is not None:
                    copy.with_name(copy.name + '-wal').write_bytes(wal)
                with closing(sqlite3.connect(copy.as_uri() + '?mode=ro', uri=True)) as con:
                    con.execute('PRAGMA query_only=ON')
                    work = 0
                    def bounded_query():
                        nonlocal work
                        work += 1
                        return work > 1000
                    con.set_progress_handler(bounded_query, 1000)
                    placeholders = ','.join('?' for _ in visited)
                    rows = con.execute('select id,provider,created,reserved,actual,receipt,metadata from reservation '
                        f"where case when json_valid(metadata) then json_extract(metadata,'$.run_id') end in ({placeholders}) order by created,id limit 5001",
                        sorted(visited)).fetchall()
                    malformed = con.execute('select count(*) from reservation where not json_valid(metadata)').fetchone()[0]
                if malformed:
                    issues.append({'source': str(ledger), 'issue': 'malformed-ledger-metadata'})
                for source in ledger_sources:
                    current, _, changed = _iz_kaynagi(home, Path(source['path']))
                    if changed or hashlib.sha256(current).hexdigest() != source['sha256']:
                        raise ValueError('ledger changed')
                if wal is None:
                    try:
                        _iz_kaynagi(home, Path(str(ledger) + '-wal'))
                    except FileNotFoundError:
                        pass
                    else:
                        raise ValueError('ledger WAL appeared')
            if len(rows) > 5000:
                issues.append({'source': str(ledger), 'issue': 'ledger-event-limit'})
            for ident, provider, created, reserved, actual, receipt, metadata in rows[:5000]:
                meta = json.loads(metadata)
                common = {**meta, 'request_id': ident, 'provider': provider, 'reserved_micro': reserved,
                          'actual_micro': actual, 'receipt': receipt, 'provider_request_id': meta.get('request_id')}
                add(meta['run_id'], ledger, 1, {**common, 'type': 'request_reserved', 'timestamp': created},
                    'request_reserved', location='/reservation/' + str(ident))
                if actual is not None:
                    add(meta['run_id'], ledger, 1, {**common, 'type': 'request_settled', 'timestamp': meta.get('kesin_ts')},
                        'request_settled', location='/reservation/' + str(ident))
        except (OSError, ValueError, sqlite3.Error):
            issues.append({'source': str(ledger), 'issue': 'ledger-unreadable-or-changed'})
    ordered = sorted(events.values(), key=lambda e: (e['timestamp'] is None, e['timestamp'] or 0, e['source'], e['lines'][0]))
    return veri_redakte({'schema': 'cadre-trace-v1', 'run_id': rid, 'sources': sources, 'issues': issues, 'events': ordered,
                         'scope': 'run-child-attempt-plan-route-ledger', 'history': 'snapshots-are-current-state'})


def iz_metni(trace: dict) -> str:
    lines = [t('Birleştirilmiş olay izi') + ' · ' + str(trace['run_id']), t('Anlık kayıt geçmiş geçiş kanıtı değildir. Bilinmeyen zaman sonda gösterilir.'), '']
    for source in trace['sources']:
        lines.append(source['path'] + ' · ' + t('Son güncelleme') + ': ' + mutlak_zaman(source['mtime']))
    for issue in trace['issues']:
        lines.append('! ' + issue['source'] + ' · ' + issue['issue'])
    for event in trace['events']:
        lines.extend(['', mutlak_zaman(event['timestamp']) if event['timestamp'] is not None else t('bilinmiyor'),
                      ' · '.join(str(event.get(k) if event.get(k) is not None else '—') for k in ('run_id', 'task_id', 'attempt_id', 'request_id', 'request_hash', 'revision', 'change_id', 'evidence_status', 'correlation_status')),
                      event['source'] + ':' + ','.join(map(str, event['lines'])) + ' '.join(event.get('locations', [])) + ' · ' + event['type'],
                      json.dumps(event['data'], ensure_ascii=False, indent=2, allow_nan=False)])
    return metin_redakte('\n'.join(lines))


def kosu_su_an(k: dict, alt: list[dict]) -> str:
    if not kosu_etkin(k):
        return ''
    gorevler = k.get('tasks') or {}
    calisan = [c['ad'] for c in alt if c['durum'] == 'simdi']
    if not alt and not k.get('plan_ready') and sozluk(gorevler.get('plan')).get('state') == 'running':
        return t('Planlama')
    if calisan:
        return t('Kodlama · {g}', g=', '.join(calisan[:2]))
    if sozluk(gorevler.get('kalite')).get('state') == 'running' and not sozluk(sozluk(k.get('final_result')).get('review')).get('status'):
        return t('Hakem')
    if sozluk(gorevler.get('qa')).get('state') == 'running' and not (sozluk(sozluk(k.get('final_result')).get('qa')) or sozluk(sozluk(gorevler.get('qa')).get('qa_result'))).get('status'):
        return t('Test')
    return ''


def normal_ilerleme(k: dict, alt: list[dict], test_: dict, hakem_: dict, teslim: bool) -> tuple[int, str, list, str]:
    aktif = kosu_etkin(k)
    n, biten = len(alt), sum(1 for c in alt if c['durum'] == 'bitti')
    planlandi = bool(alt) or bool(k.get('plan_ready'))
    kodlama = 'bitti' if n and biten == n else ('simdi' if any(c['durum'] == 'simdi' for c in alt) else (
        'hata' if any(c['durum'] == 'hata' for c in alt) else ('' if kosu_sonlandi(k) else 'bekliyor')))
    if 'teslim_merge' in k or 'teslim_push' in k:
        teslim_isareti = 'bitti'
    else:
        teslim_isareti = 'bekliyor' if teslim or aktif else ''
    plan_aktif = aktif and sozluk(sozluk(k.get('tasks')).get('plan')).get('state') == 'running'
    akis = [(t('Planlama'), 'bitti' if planlandi else ('simdi' if plan_aktif else ('' if kosu_sonlandi(k) else 'bekliyor'))), (t('Kodlama {b}/{n}', b=biten, n=n), kodlama),
            (t('Test'), test_['durum']), (t('Hakem'), hakem_['durum']), (t('Teslim'), teslim_isareti)]
    if (k.get('final_result') or {}).get('status'):
        yuzde, asama = 100, t('sonuç')
    elif hakem_['durum'] == 'simdi':
        yuzde, asama = 90, t('hakem')
    elif test_['durum'] in ('simdi', 'hata') and kodlama == 'bitti':
        yuzde, asama = 78, t('test')
    elif planlandi:
        yuzde, asama = 10 + int(60 * biten / max(n, 1)), t('kodlama {b}/{n}', b=biten, n=n)
    else:
        yuzde, asama = 5, t('planlama')
    return yuzde, asama, akis, kosu_su_an(k, alt)


def proje_yapisi(home: Path, k: dict, proje: dict) -> tuple[list, int, str, list, str, list]:
    from proje import durum_gorunumu
    proje = durum_gorunumu(k, proje)
    rid = str(k['run_id'])
    aktif = kosu_etkin(k)
    ust_durum = str(k.get('state') or '')
    ozl = [f for f in proje.get('ozellikler') or [] if isinstance(f, dict) and f.get('id')]
    etkin = k.get('project_active_feature')
    active_id = k.get('project_active_run')
    try:
        from kosu import RunStore, bounded_json
        active_child = bounded_json(RunStore(home).path(active_id) / 'run.json') if active_id else {}
    except (OSError, ValueError, TypeError):
        active_child = {}
    active_owned = active_child.get('run_id') == active_id and active_child.get('proje_ust') == rid and active_child.get('proje_ozellik') == etkin
    ajanlar_ = ajan_dugumleri(home, k)
    parcalar, su_an = [], ''
    parca_aciklamasi = aciklama(t('Bu parça'), t('Bu parça kendi planı, alt görevleri ve testiyle ayrı bir koşudur. Hakem incelemesi Son kontrolde bir kez yapılır.'),
                                [(t('Plan'), t('parçanın alt görevleri')), (t('Test'), t('parçanın testi'))])
    riskli_aciklamasi = aciklama(t('Bu parça'), t('Riskli parça. Planı, alt görevleri ve testinden sonra hakem incelemesi bu koşuda da yapılır.'),
                                 [(t('Plan'), t('parçanın alt görevleri')), (t('Test'), t('parçanın testi')), (t('Hakem'), t('parçanın incelemesi'))])
    for f in ozl:
        kosu_listesi = f.get('kosu')
        kosu_id = kosu_listesi[-1] if isinstance(kosu_listesi, list) and kosu_listesi else None
        if f['id'] == etkin and active_owned:
            kosu_id = active_id
        alt_k = kayit_oku(kosu_dizini(home, kosu_id) / 'run.json') if kosu_id else {}
        icerik: list[dict] = []
        alt_su_an = ''
        hakemli = bool(f.get('riskli'))
        if alt_k.get('run_id'):
            alt_plan = kayit_oku(kosu_dizini(home, kosu_id) / 'plan.json')
            alt = alt_gorev_dugumleri(home, alt_k, alt_plan, ust_durum)
            icerik = [plan_dugumu(home, alt_k, alt_plan, alt, ust_durum), test_dugumu(home, alt_k, ust_durum)]
            inceleme = str(sozluk(sozluk(alt_k.get('final_result')).get('review')).get('status') or '').upper()
            hakemli = hakemli or 'kalite' in sozluk(alt_k.get('tasks')) or inceleme not in ('', 'SKIPPED')
            if hakemli:
                icerik.append(hakem_dugumu(home, alt_k, ust_durum))
            ajanlar_ += ajan_dugumleri(home, alt_k, f"{f['id']} · ", ust_durum)
            alt_su_an = kosu_su_an(alt_k, alt) if aktif else ''
        isaret = PARCA_ISARETI.get(str(f.get('durum')), 'bekliyor')
        yazi = t(PARCA_YAZI.get(str(f.get('durum')), str(f.get('durum') or '')))
        if kosu_sonlandi(k) and f.get('durum') == 'bekliyor':
            basladi = bool(alt_k.get('tasks')) or alt_k.get('state') in ('running', 'retryable', *TERMINAL)
            isaret, yazi = ('hata', t('yarım kaldı')) if basladi else ('', t('yapılmadı'))
        elif f['id'] == etkin and f.get('durum') == 'bekliyor' and aktif and (not alt_k or kosu_etkin(alt_k)):
            isaret, yazi = 'simdi', alt_su_an or t('sürüyor')
            su_an = f"{f['id']} {f.get('baslik', '')}" + (f' · {alt_su_an}' if alt_su_an else '')
        if f.get('durum') in ('gecti', 'uygulandi') and alt_k and alt_k.get('state') not in ('completed', 'implemented'):
            isaret, yazi = 'hata', t('durum çelişkisi')
        if kosu_id and not alt_k:
            isaret, yazi = 'hata', t('durum bilinmiyor')
        if f['id'] == etkin and active_id and not active_owned:
            isaret, yazi, su_an = 'hata', t('durum çelişkisi'), ''
        istem = kosu_dizini(home, kosu_id) / 'prompt.md' if kosu_id else None
        parcalar.append(d(f"{f['id']} {f.get('baslik', '')}", yazi, [
            ('baslik', f"{f['id']} · {f.get('baslik', '')}"), ('alan', (t('Durum'), yazi)),
            ('alan', (t('Kaydedilen durum'), f"{f.get('durum') or '—'} · {alt_k.get('state') or '—'}")),
            ('alan', (t('Ne yapılacak'), ' '.join(str(f.get('aciklama') or '—').split())[:400])),
            ('alan', (t('Kabul'), str(f.get('kabul') or '—')[:300])), ('alan', (t('Test komutu'), str(f.get('qa_komut') or '—'))),
            ('alan', (t('Bağımlı'), liste_yazi(f.get('bagimli')))), ('alan', (t('Öncelik'), str(f.get('oncelik', '—')))),
            ('alan', (t('Kapsam puanı'), str(f.get('kapsam_puani', '—')))), ('alan', (t('Tahmin'), para(f.get('tahmin_usd')))),
            ('alan', (t('Riskli'), t('evet') if f.get('riskli') else t('hayır'))), ('alan', (t('Deneme'), str(f.get('deneme', 0)))),
            ('alan', (t('Koşu'), str(kosu_id or '—')))], icerik, isaret, id=str(f['id']),
            aciklama=riskli_aciklamasi if hakemli else parca_aciklamasi,
            dosya={'tur': 'metin', 'yol': str(istem), 'baslik': t('{g} görev metni', g=f['id'])} if istem and istem.exists() else None))
    gecen = sum(1 for f in ozl if f.get('durum') in ('gecti', 'uygulandi'))
    planlayici = son_deneme(home, rid, 'proje-liste')
    plan_modeli = kayit_oku(planlayici / 'attempt.json').get('model') if planlayici else None
    plan_ayrinti = [('baslik', t('Plan')), ('alan', (t('Planlayan'), str(plan_modeli or k.get('plan_modeli') or '—'))),
                    ('alan', (t('Parça'), t('{n} · {g} geçti', n=len(ozl), g=gecen))),
                    ('alan', (t('Üst sınır'), para(proje.get('tavan_usd') or k.get('budget_cap_usd')))),
                    ('alan', (t('Harcanan'), para(proje.get('harcama_usd')))), ('alan', (t('Ara dal'), str(proje.get('ara_dal') or '—')))]
    if proje.get('istek_ozeti'):
        plan_ayrinti += [('bolum', t('İstek özeti')), ('satir', ' '.join(str(proje['istek_ozeti']).split())[:500])]
    plan = d(t('Plan'), t('{n} parça · {g} geçti', n=len(ozl), g=gecen), plan_ayrinti, parcalar,
             'bitti' if ozl and gecen == len(ozl) else ('simdi' if aktif else ('hata' if kosu_sonlandi(k) else 'bekliyor')), id='plan',
             aciklama=aciklama(t('Plan'), t('Planlayıcı büyük görevi parçalara böldü. Parçalar sırayla ayrı koşu olarak yapılır. o ile proje planı okunur.')),
             dosya={'tur': 'json', 'yol': str(kosu_dizini(home, rid) / 'proje.json'), 'baslik': t('Proje planı')})
    son_id = next((c.get('kosu') for c in proje.get('cocuklar') or [] if isinstance(c, dict) and c.get('ozellik') == 'son-kabul'), None)
    if etkin is None and active_owned:
        son_id = active_id
    son_k = kayit_oku(kosu_dizini(home, son_id) / 'run.json') if son_id else {}
    son_icerik: list[dict] = []
    if son_k.get('run_id'):
        son_plan = kayit_oku(kosu_dizini(home, son_id) / 'plan.json')
        son_alt = alt_gorev_dugumleri(home, son_k, son_plan, ust_durum)
        son_icerik = [plan_dugumu(home, son_k, son_plan, son_alt, ust_durum), test_dugumu(home, son_k, ust_durum), hakem_dugumu(home, son_k, ust_durum)]
        ajanlar_ += ajan_dugumleri(home, son_k, t('Son kontrol') + ' · ', ust_durum)
        if kosu_etkin(son_k, ust_durum):
            adim = kosu_su_an(son_k, son_alt)
            su_an = t('Son kontrol') + (f' · {adim}' if adim else '')
    proje_durum = str(proje.get('durum') or '')
    son_kod = str(sozluk(son_k.get('final_result')).get('status') or '').upper()
    if son_kod == 'PASS':
        son_isaret, son_yazi = 'bitti', t('geçti')
    elif son_kod in ('FAIL', 'ERROR', 'CANCELLED'):
        son_isaret, son_yazi = 'hata', t('yarım kaldı') if son_kod == 'CANCELLED' else t('kaldı')
    elif kosu_etkin(son_k, ust_durum):
        son_isaret, son_yazi = 'simdi', t('sürüyor')
    elif son_k and (kosu_sonlandi(son_k, ust_durum)):
        son_isaret, son_yazi = 'hata', t('yarım kaldı')
    elif son_id and not son_k:
        son_isaret, son_yazi = 'hata', t('durum bilinmiyor')
    elif son_k:
        son_isaret, son_yazi = 'bekliyor', t('bekliyor')
    else:
        son_isaret, son_yazi = ('', t('yapılmadı')) if kosu_sonlandi(k) else ('bekliyor', t('sırada'))
    if etkin is None and active_id and not active_owned:
        son_isaret, son_yazi, su_an = 'hata', t('durum çelişkisi'), ''
    son_kontrol = d(t('Son kontrol'), son_yazi, [
        ('baslik', t('Son kontrol')), ('alan', (t('Proje durumu'), proje_durum or '—')), ('alan', (t('Neden'), str(proje.get('neden') or '—'))),
        ('alan', (t('Kaydedilen durum'), f"{son_k.get('state') or '—'} · {son_kod or '—'}")),
        ('alan', (t('Son kabul turu'), str(proje.get('son_kabul_turu', '—')))), ('alan', (t('Koşu'), str(son_id or '—')))],
        son_icerik, son_isaret, id='son-kontrol',
        aciklama=aciklama(t('Son kontrol'), t('Tüm parçalar bitince bütün testler ve hakem incelemesi bir kez daha yapılır.'),
                          [(t('Plan'), t('gerekirse son düzeltmeler')), (t('Test'), t('bütün testler')), (t('Hakem'), t('bağımsız inceleme'))]))
    if proje_durum == 'tamam' and son_kod == 'PASS' and ust_durum == 'completed':
        yuzde, asama = 100, t('sonuç')
    elif gecen == len(ozl) and ozl:
        yuzde, asama = 92, t('son kontrol')
    else:
        yuzde, asama = int(90 * gecen / max(len(ozl), 1)), t('parça {g}/{n}', g=gecen, n=len(ozl))
    akis = [(str(f['id']), p['durum']) for f, p in zip(ozl, parcalar)] + [(t('Son kontrol'), son_isaret)]
    return [plan, son_kontrol], yuzde, asama, akis, su_an, ajanlar_


def sonuc_dugumu(home: Path, k: dict) -> dict:
    result = sozluk(k.get('final_result'))
    status = str(result.get('status') or t('durum bilinmiyor'))
    return d(t('Sonuç'), status, [('baslik', t('Sonuç')), ('alan', (t('Durum'), status)),
        ('alan', (t('Kaydedilen durum'), str(k.get('state') or '—'))),
        ('alan', (t('Test'), str(sozluk(result.get('qa')).get('status') or '—'))),
        ('alan', (t('Hakem'), str(sozluk(result.get('review')).get('status') or '—'))),
        ('alan', (t('Neden'), metin_redakte(str(result.get('reason') or k.get('reason') or '—')))),
        ('soluk', t('Ara uygulama bütün ürünün son kabulü değildir.'))],
        durum='bitti' if result.get('status') == 'PASS' else '', id='sonuc',
        dosya={'tur': 'json', 'yol': str(kosu_dizini(home, k['run_id']) / 'run.json'), 'baslik': t('Koşu kaydı')})


def harcama_uyarisi_ayrinti(k: dict) -> list:
    warning = sozluk(k.get('harcama_uyarisi'))
    if warning.get('kind') != 'speed-warning':
        return []
    return [('baslik', t('Harcama hızı uyarısı')),
            ('alan', (t('Neden'), metin_redakte(str(warning.get('neden') or '—')))),
            ('alan', (t('İlerleme'), t('Kanıtlı ilerleme') if warning.get('ilerleme') == 'productive'
                      else t('İlerleme yok (ürün değişmedi, kayıtlı adım yok)') if warning.get('ilerleme') == 'none' else t('İlerleme belirsiz'))),
            ('alan', (t('Son güncelleme'), mutlak_zaman(warning.get('ts')))),
            ('soluk', t('Hız uyarısı onay istemez. Toplam, aylık, kapasite ve izin sınırları geçerlidir.'))]


def operasyon_dugumu(home: Path, k: dict, plan: dict) -> dict:
    from kosu import RunStore
    rid = str(k['run_id'])
    cap = sozluk(k.get('request')).get('budget_usd')
    if cap is None and k.get('budget_manual'):
        cap = k.get('budget_cap_usd')
    budget = sozluk(k.get('request_budget'))
    detail = [('baslik', t('Karar ve operasyon')), ('alan', (t('Bekleme nedeni'), metin_redakte(str(k.get('reason') or t('Yok'))))),
              ('alan', (t('Yeniden deneme'), mutlak_zaman(k.get('retry_after')))),
              ('alan', (t('Kesin toplam tavan'), str(cap) + ' USD' if cap is not None else t('Kayıtlı sabit toplam tavan yok'))),
              ('alan', (t('Bütçe kapsamı'), str(budget['scope']) if budget.get('scope') else t('Koşu defteri'))),
              ('soluk', t('Tavan izin veya harcama hedefi değildir.'))]
    if cap is None:
        detail.append(('alan', (t('Geçerli aşama tavanı'), str(k.get('budget_cap_usd', '—')) + ' USD')))
    if budget.get('providers'):
        detail.append(('alan', (t('Sağlayıcılar'), metin_redakte(str(budget['providers'])))))
    remaining = [g.get('id') for g in plan.get('gorevler', []) if isinstance(g, dict)
                 and sozluk(sozluk(k.get('tasks')).get(g.get('id'))).get('state') not in ('tamam', 'gecti', 'uygulandi')]
    detail += [('alan', (t('Kalan kapsam'), liste_yazi(remaining) if plan else t('Plan henüz yok')))]
    if k.get('harcama_alarmi'):
        detail.append(('satir', t('Harcama alarmı ayrı karar bekler. Onay toplam veya aylık tavanı büyütmez.')))
    detail += harcama_uyarisi_ayrinti(k)
    if k.get('butce_aktarimlari'):
        detail.append(('alan', (t('İç pay aktarımı'), metin_redakte(json.dumps(k['butce_aktarimlari'], ensure_ascii=False)))))
    decisions = []
    try:
        raw, changed, unstable = _iz_kaynagi(home, Path('zincir/yonlendirici.jsonl'))
        if unstable:
            raise ValueError('decision-source-changing')
        for line in raw.splitlines(keepends=True):
            if not line.endswith(b'\n'):
                continue
            try:
                event = json.loads(line)
                if isinstance(event, dict) and event.get('run_id') == rid and event.get('olay') == 'zincir':
                    decisions.append(event)
            except (ValueError, UnicodeError):
                continue
        if decisions:
            event = decisions[-1]
            detail += [('bolum', t('Son kayıtlı yönlendirme')),
                       ('alan', (t('Kaynak'), 'zincir/yonlendirici.jsonl')),
                       ('alan', (t('Karar zamanı'), metin_redakte(str(event.get('ts') or '—')))),
                       ('alan', (t('Son güncelleme'), mutlak_zaman(changed))),
                       ('alan', (t('Rol'), str(event.get('rol') or '—'))),
                       ('alan', (t('Uygun aday sırası'), liste_yazi(event.get('zincir')))),
                       ('kayit', [metin_redakte(str(reason)) for reason in event.get('neden', [])])]
        else:
            detail.append(('satir', t('Bu koşuya bağlı yönlendirme kararı henüz yok.')))
    except FileNotFoundError:
        detail.append(('satir', t('Bu koşuya bağlı yönlendirme kararı henüz yok.')))
    except (OSError, ValueError, TypeError, KeyError):
        detail.append(('satir', t('Yönlendirme kaydı okunamadı. Güncel karar bilinmiyor.')))
    actions = []
    attempts = []
    for path in kosu_dizini(home, rid).glob('tasks/*/attempts/*/attempt.json'):
        try:
            raw, _, unstable = _iz_kaynagi(home, path.relative_to(home))
            attempt = json.loads(raw)
            if not unstable and attempt.get('run_id') == rid and isinstance(attempt.get('created'), (float, int)):
                attempts.append(attempt)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if attempts:
        actual = max(attempts, key=lambda a: a['created'])
        detail += [('alan', (t('Son deneme modeli'), metin_redakte(str(actual.get('model') or '—')))),
                   ('alan', (t('Deneme durumu'), str(actual.get('outcome') or 'unknown')))]
    try:
        view = RunStore(home).plan_view(rid)
        if not view['execution_plan_matches']:
            detail.append(('satir', t('Yürütme planı geçmişle uyuşmuyor. İşlem engellenir.')))
        history = view['history']
        answers = {a['decision']['id']: a['answer'] for a in view['answers']}
        if view['decisions']:
            detail[1] = ('alan', (t('Bekleme nedeni'), t('Yanıt kaydedildi, yeni plan bekleniyor') if view['answer_ready'] else
                                  t('Kritik karar bekleniyor')))
        for assumption in history[-1]['plan'].get('assumptions', []) if history else []:
            detail.append(('alan', (t('Kayıtlı varsayım'), metin_redakte(assumption['choice'] + ' — ' + assumption['reason']))))
        for decision in view['decisions']:
            detail += [('alan', (t('Kritik karar bekleniyor'), metin_redakte(decision['question']))),
                       ('alan', (t('Gerekçe'), metin_redakte(decision['reason'])))]
            if decision['id'] in answers:
                detail.append(('alan', (t('Kayıtlı yanıt'), metin_redakte(answers[decision['id']]))))
            elif k.get('state') not in TERMINAL:
                actions.append(d(t('Kritik kararı yanıtla'), metin_redakte(decision['question']),
                                 id='plan-answer-' + decision['id'], eylem='plan-answer', kosu=rid,
                                 revision=view['revision'], decision=decision))
        if history:
            review = view['reviews'][-1] if view['reviews'] and view['reviews'][-1].get('revision_number') == view['revision'] else history[-1]['review']
            detail += [('alan', (t('Plan revizyonu'), str(view['revision']))),
                       ('alan', (t('Plan incelemesi'), str(review.get('status') or '—')))]
            actions.append(d(t('Plan geçmişi'), t('{n} revizyon', n=view['revision']), id='plan-history',
                             dosya={'tur': 'plan-history', 'home': str(home), 'run_id': rid, 'baslik': t('Plan geçmişi')}))
            if k.get('state') not in TERMINAL:
                if not view['held'] and not view['decisions']:
                    actions.append(d(t('Plan değişikliği iste'), '', id='plan-change', eylem='plan-change',
                                     kosu=rid, revision=view['revision']))
                elif view['held']:
                    change = view['changes'][-1]
                    detail += [('alan', (t('Değişiklik talebi'), metin_redakte(change['text']))),
                               ('alan', (t('Duruş / karar'), t({'stop_requested': 'Duruş teyidi bekleniyor', 'stopped': 'Duruş doğrulandı',
                                                             'withdrawn': 'Geri çekildi, devam kararı gerekiyor',
                                                             'rejected': 'Plan incelemesi reddedildi',
                                                             'review_pending': 'Yeni plan inceleme bekliyor'}.get(change['status'], change['status']))))]
                    if change['status'] == 'stop_requested':
                        actions.append(d(t('Duruşu doğrula'), '', id='plan-stop', eylem='plan-stop', kosu=rid, change_id=change['id']))
                    if change['status'] == 'stopped':
                        actions.append(d(t('Değişikliği geri çek'), '', id='plan-withdraw', eylem='plan-withdraw', kosu=rid, change_id=change['id']))
                    if (k.get('request') and change['status'] in ('stopped', 'rejected', 'review_pending')
                            and (not view['decisions'] or view['answer_ready'])):
                        actions.append(d(t('Planı üret / incele'), '', id='request-plan', eylem='request-plan', kosu=rid))
                    if review.get('status') == 'PASS' and not view['decisions']:
                        actions.append(d(t('Devam kararını kaydet'), '', id='plan-resume', eylem='plan-resume', kosu=rid,
                                         change_id=change['id'], revision=view['revision']))
                    if change.get('stop'):
                        detail.append(('alan', (t('Duruştaki maliyet'), json.dumps(change['stop']['cost'], ensure_ascii=False))))
                    detail.append(('soluk', t('Devam kararı ve güncel plan incelemesi olmadan yürütme açılmaz.')))
        if not view['held'] and not view['decisions'] and k.get('state') not in ('completed', 'implemented', 'cancelled'):
            from kosu import request_authorized
            if k.get('request') and not request_authorized(k):
                actions.append(d(t('Göreve özel çağrı izni'), '', id='request-authorize', eylem='request-authorize', kosu=rid))
            else:
                actions.append(d(t('Başlat / devam et'), '', id='start', eylem='start', kosu=rid))
            if k.get('request') and sozluk(k.get('planning')).get('status') == 'rejected':
                actions.append(d(t('Planı üret / incele'), '', id='request-plan', eylem='request-plan', kosu=rid))
            if k.get('harcama_alarmi'):
                actions.append(d(t('Harcama alarmını onayla'), '', id='spending-approval', eylem='harcama-onay', kosu=rid))
    except (OSError, ValueError, KeyError, TypeError):
        detail.append(('satir', t('Plan geçmişi okunamadı. İşlem bekler.')))
    try:
        from izin import pending
        for action in pending(RunStore(home), rid):
            detail += [('alan', (t('İzin kararı'), action['identity']['kind'] + ' · ' + action['status'])),
                       ('kod', action['display'])]
            if action['status'] in ('pending', 'first-approved'):
                actions.append(d(t('Kesin eylem izni'), action['identity']['kind'], id='operator-' + action['token'],
                                 eylem='operator-approve', kosu=rid, token=action['token']))
    except (OSError, ValueError, KeyError, TypeError):
        detail.append(('satir', t('İzin kaydı okunamadı. Eylem bekler.')))
    return d(t('Karar ve operasyon'), t('Motor kaydı'), detail, actions, id='operasyon')


def gorev_dugumu(home: Path, k: dict) -> dict:
    rid = str(k['run_id'])
    dizin = kosu_dizini(home, rid)
    ilk = ilk_satir(home, k)
    plan, proje = kayit_oku(dizin / 'plan.json'), kayit_oku(dizin / 'proje.json')
    durum = str(k.get('state') or '')
    aktif = durum not in TERMINAL
    teslim = teslim_edilebilir(k)
    bilgi = teslim_bilgisi(k) if durum in ('completed', 'implemented') else {}
    fr = k.get('final_result') or {}
    local_delivery = sozluk(fr.get('delivery'))
    ekonomi = harcama_dugumu(home, k, proje)
    if proje.get('ozellikler'):
        cocuklar, yuzde, asama, akis, su_an, ajanlar_ = proje_yapisi(home, k, proje)
        tur = t('büyük görev (parçalı)')
        metin = t('Büyük görev parçalara bölünür. Her parça kendi planı, alt görevleri ve testiyle ayrı bir koşudur. Hakem incelemesi en sonda, '
                  'Son kontrolde bir kez yapılır.')
        cocuk_yazi = dict(GOREV_COCUKLARI, plan='parçalar, her biri ayrı koşu')
    else:
        alt = alt_gorev_dugumleri(home, k, plan)
        test_, hakem_ = test_dugumu(home, k), hakem_dugumu(home, k)
        cocuklar = [plan_dugumu(home, k, plan, alt), test_, hakem_]
        yuzde, asama, akis, su_an = normal_ilerleme(k, alt, test_, hakem_, teslim)
        ajanlar_ = ajan_dugumleri(home, k)
        if k.get('mode') == 'rapor':
            tur, metin = t('rapor görevi'), t('Rapor görevi bir rapor dosyası yazar. Test ve hakem raporu denetler.')
        else:
            tur = t('görev')
            metin = t('Görev planla başlar. Planlayıcının alt görevleri yazılır, sonra tek test ve tek hakem incelemesi yapılır. Teslim senin kararın.')
        cocuk_yazi = dict(GOREV_COCUKLARI)
    if durum in TERMINAL:
        cocuklar.append(sonuc_dugumu(home, k))
        cocuklar.append(teslim_dugumu(k, teslim, bilgi))
    rapor = rapor_dugumu(home, k)
    if rapor:
        cocuklar.append(rapor)
    calisan_ajan = sum(1 for a in ajanlar_ if a['durum'] == 'simdi')
    cocuklar += [d(t('Ajanlar'), t('{n} ajan', n=len(ajanlar_)), [('baslik', t('Bu görevde çalışan ajanlar')), ('alan', (t('Ajan'), str(len(ajanlar_)))),
                                                                   ('alan', (t('Şu an çalışan'), str(calisan_ajan)))],
                   ajanlar_, 'simdi' if calisan_ajan else '', id='ajanlar',
                   aciklama=aciklama(t('Ajanlar'), t('Bu görevde çalışan ajanlar. Her birinin son düşünceleri ve araç çağrıları. o ile tam kaydı oku.'))),
                 ekonomi, olaylar_dugumu(home, rid), operasyon_dugumu(home, k, plan)]
    cocuk_yazi['operasyon'] = 'Bekleme, yönlendirme, plan geçmişi ve güvenli işlemler'
    bitis = (k.get('updated') if not aktif else time.time()) or time.time()
    request = sozluk(k.get('request'))
    if request and aktif:
        planning = sozluk(k.get('planning'))
        status = planning.get('status')
        if status != 'accepted':
            asama = (t('Plan incelemesi reddedildi') if status == 'rejected' else t('Plan incelemesi') if status == 'review-pending' else
                     t('Plan') if status == 'generating' else t('Plan kapısı bekleniyor'))
            su_an = metin_redakte(str(k.get('reason') or asama))
    from kosu import plan_hold, plan_decisions, plan_answer_ready, run_job_type
    try:
        job_type = run_job_type(k)
    except ValueError:
        job_type = '—'
    if job_type == 'review' and aktif and sozluk(k.get('planning')).get('status') == 'accepted':
        review_phase = {'review-author': t('İnceleme raporu'), 'review-evidence': t('Kanıt kontrolü'),
                        'review-final': t('Son inceleme'), 'review-recheck': t('Onarım sonrası son inceleme')}.get(k.get('phase'))
        if review_phase:
            asama = su_an = review_phase
    held = plan_hold(k)
    if held and aktif:
        status = k['plan_changes'][-1]['status']
        asama = t('Duruş teyidi bekleniyor') if status == 'stop_requested' else t('Plan incelemesi') if status == 'review_pending' else t('Plan değişikliği bekleniyor')
        su_an = asama
    decision_wait = bool(plan_decisions(k))
    if decision_wait and aktif:
        asama = t('Yanıt kaydedildi, yeni plan bekleniyor') if plan_answer_ready(k) else t('Kritik karar bekleniyor')
        su_an = asama
    if proje:
        from proje import durum_gorunumu
        project_phase = durum_gorunumu(k, proje).get('asama')
        if project_phase and not decision_wait:
            asama, su_an = t(project_phase), t(project_phase) if aktif else ''
    durum_yazi = asama if (held or request or decision_wait) and aktif else t(DURUM_ADI.get(durum, durum))
    ayrinti = [('baslik', ilk), ('soluk', f'{proje_adi(k)} · {tur}'), ('akis', akis), ('alan', (t('Durum'), durum_yazi)),
               ('alan', (t('Proje'), str(k.get('project') or '?'))),
               ('alan', (t('Başlangıç'), mutlak_zaman(k.get('created')))),
               ('alan', (t('Son güncelleme'), mutlak_zaman(k.get('updated')))),
               *kimlik_ozeti(k),
               ('alan', (t('Süre'), sure_metni(sayi(bitis) - sayi(k.get('created') or bitis)))), ('alan', (t('Aşama'), asama)),
               ('soluk', t('Aşama göstergesi ürün tamamlanma yüzdesi değildir.')), ('alan', (t('Harcama'), ekonomi['ozet']))]
    if request:
        review = next((r for r in reversed(k.get('plan_reviews') or []) if r.get('revision_number') == len(k.get('plan_history') or [])), {})
        explanation = metin_redakte(str(k.get('reason') or sozluk(k.get('planning')).get('status') or '—'))
        ayrinti += [('bolum', t('Görev girdisi')), ('alan', (t('Girdi kipi'), t({'goal': 'Hedefim var, planla', 'backlog': 'Hazır backlog', 'plan_only': 'Yalnız plan oluştur'}.get(request.get('input_mode'), 'bilinmiyor')))),
                    ('alan', (t('İstenen çıktı'), metin_redakte(str(request.get('output') or '—')))),
                    ('alan', (t('Kabul kriterleri'), metin_redakte(str(request.get('acceptance') or t('Planda ilk istekten türetilecek'))))),
                    ('alan', (t('Dar kabul kapsamı'), kapsam_metni(request.get('scope_files'), request.get('scope_tests')))),
                    ('alan', (t('Maliyet tercihi'), t({'economic': 'Ekonomik', 'balanced': 'Dengeli', 'strong': 'Güçlü'}.get(request.get('preference'), 'bilinmiyor')))),
                    ('alan', (t('Zorluk düzeltmesi'), t({None: 'Otomatik tahmin bekleniyor', 'easy': 'Kolay', 'medium': 'Orta', 'hard': 'Zor', 'very_hard': 'Çok zor'}.get(request.get('difficulty_override'), 'bilinmiyor')))),
                    ('alan', (t('İş bütçesi'), str(request.get('budget_usd') or '—') + ' USD · ' + t('Toplam'))),
                    ('alan', (t('Süre'), t('{d} dk', d=request.get('duration_minutes') or '—'))),
                    ('alan', (t('Plan incelemesi'), str(review.get('status') or t('Henüz yapılmadı')))), ('satir', explanation),
                    ('alan', (t('İş türü'), job_type)),
                    ('alan', (t('Özgün istek'), metin_redakte(str(request.get('prompt') or '—'))))]
        plan_status = (t('Doğrulanmış plan çıktısı') if sozluk(k.get('plan_delivery')).get('status') == 'PASS' else
                       t('Plan kabul edildi') if review.get('status') == 'PASS' else
                       t('Plan incelemesi reddedildi') if review.get('status') == 'FAIL' else t('Plan kapısı bekleniyor'))
        girdi = d(t('Görev girdisi'), plan_status, ayrinti[-12:], id='girdi', durum='bitti' if review.get('status') == 'PASS' else 'bekliyor',
                  dosya={'tur': 'json', 'yol': str(dizin / 'run.json'), 'baslik': t('Koşu kaydı')})
        cocuklar.append(girdi)
        defaults = sozluk(request.get('selection_defaults'))
        if sozluk(k.get('plan_delivery')).get('status') == 'PASS':
            cocuklar.append(d(t('Doğrulanmış plan çıktısı'), '', id='plan-delivery',
                             dosya={'tur': 'json', 'yol': k['plan_delivery']['artifact'], 'baslik': t('Doğrulanmış plan çıktısı')}))
        if sozluk(k.get('review_delivery')).get('status') == 'PASS':
            cocuklar.append(d(t('İnceleme raporu'), '', id='review-delivery',
                             dosya={'tur': 'json', 'yol': k['review_delivery']['artifact'], 'baslik': t('İnceleme raporu')}))
        if local_delivery.get('kind') == 'reviewed-worktree':
            ayrinti += [('alan', (t('Doğrulanmış yerel çıktı'), metin_redakte(str(local_delivery.get('path') or '—')))),
                        ('soluk', t('Git işlemleri ayrı kesin izin bekler.'))]
        if defaults:
            detail = [('bolum', t('Kaydedilmiş görev varsayılanı')),
                      ('alan', (t('Maliyet tercihi'), t({'economic': 'Ekonomik', 'balanced': 'Dengeli', 'strong': 'Güçlü'}.get(defaults.get('preference'), 'bilinmiyor')))),
                      ('alan', (t('Zorluk düzeltmesi'), t({None: 'Otomatik tahmin bekleniyor', 'easy': 'Kolay', 'medium': 'Orta', 'hard': 'Zor', 'very_hard': 'Çok zor'}.get(defaults.get('difficulty_override'), 'bilinmiyor')))),
                      ('soluk', t('Görevdeki tercih ve zorluk model sırasına uygulanır. Plan ve izin kapıları ayrıca korunur.'))]
            ayrinti.extend(detail)
            girdi['ayrinti'].extend(detail)
    if 'model_selection' in k:
        from model_secimi import kosu_secimi
        try:
            selection = kosu_secimi(home, rid)
            levels = {'easy': 'Kolay', 'medium': 'Orta', 'hard': 'Zor', 'very_hard': 'Çok zor'}
            detail = [('bolum', t('Görevdeki model seçimi')),
                      ('alan', (t('Maliyet tercihi'), t({'economic': 'Ekonomik', 'balanced': 'Dengeli', 'strong': 'Güçlü'}[selection['preference']]))),
                      ('alan', (t('İlk zorluk tahmini'), t(levels[selection['difficulty_estimate']]))),
                      ('alan', (t('Zorluk düzeltmesi'), t(levels.get(selection['difficulty_override'], 'Yok')))),
                      ('alan', (t('Etkin zorluk'), t(levels[selection['difficulty_effective']]))),
                      ('alan', (t('Tahmin gerekçesi'), selection['estimate_reason'])),
                      ('alan', (t('Seçim politikası'), selection['policy_version'])),
                      ('soluk', t('Görevdeki tercih ve zorluk model sırasına uygulanır. Plan ve izin kapıları ayrıca korunur.'))]
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            detail = [('bolum', t('Görevdeki model seçimi')), ('satir', t('Model seçimi okunamadı.'))]
        ayrinti.extend(detail)
        if request:
            girdi['ayrinti'].extend(detail)
        cocuklar.append(d(t('Görevdeki model seçimi'), t('Seçim politikası'), detail, id='secim'))
        cocuk_yazi['secim'] = 'Seçim politikası'
    if proje.get('ozellikler'):
        features = [f for f in proje['ozellikler'] if isinstance(f, dict)]
        count = sum(f.get('durum') in ('gecti', 'uygulandi') for f in features)
        ayrinti += [('alan', (t('Ara uygulama'), f'{count}/{len(features)}')), ('soluk', t('Parça testi, son kabul ve teslim ayrı kararlardır.'))]
    uyarilar = []
    if durum in TERMINAL:
        for gid, gorev in sozluk(k.get('tasks')).items():
            if sozluk(gorev).get('state') == 'running':
                uyarilar.append(t('Terminal koşuda kayıtlı aktif adım: {g}', g=f'{gid}=running'))
    cocuk_idleri = {str(c['kosu']) for c in proje.get('cocuklar') or [] if isinstance(c, dict) and c.get('kosu')}
    cocuk_idleri |= {str(cid) for f in proje.get('ozellikler') or [] if isinstance(f, dict) and isinstance(f.get('kosu'), list) for cid in f['kosu'] if cid}
    for cid in sorted(cocuk_idleri):
        alt_k = kayit_oku(kosu_dizini(home, cid) / 'run.json')
        if not alt_k or (durum in TERMINAL and alt_k.get('state') not in TERMINAL) or (durum != 'running' and alt_k.get('state') == 'running'):
            uyarilar.append(t('Parent/çocuk durum çelişkisi: {p} · {c}', p=f'{rid}={durum}', c=f"{cid}={alt_k.get('state') or '?'}"))
    if proje and ((proje.get('durum') in ('stopped', 'durdu', 'dustu', 'takildi') and kosu_etkin(k)) or
                  (proje.get('durum') == 'calisiyor' and durum in TERMINAL) or
                  (proje.get('durum') == 'tamam' and durum not in ('completed',))):
        uyarilar.append(t('Proje/koşu durum çelişkisi: {p} · {r}', p=str(proje.get('durum')), r=durum))
    if uyarilar:
        ayrinti += [('bolum', t('Durum çelişkisi'))] + [('satir', uyari) for uyari in uyarilar]
    if su_an:
        ayrinti.append(('alan', (t('Şu an'), su_an)))
    if k.get('reason'):
        ayrinti.append(('alan', (t('Not'), str(k['reason'])[:200])))
    if k.get('reason') == 'new-project-init-permission-required':
        komut = 'cadre init --olustur ' + str(sozluk(k.get('request')).get('target_path') or '')
        ayrinti.append(('alan', (t('Sonraki adım'), t('İlk commit izni için {k} çalıştır, sonra görevi yeniden başlat.', k=komut))))
    elif k.get('reason') == 'new-project-registration-mismatch':
        import proje_kayit
        hedef = str(sozluk(k.get('request')).get('target_path') or '')
        kayitli = proje_kayit.kayitli(home, Path(hedef).resolve()) if hedef else None
        istek_adi = str(k.get('project') or '—')
        baska = ''
        if kayitli is None and istek_adi != '—':
            try:
                baska = (home / 'projects' / istek_adi / 'repo-path').read_text().strip()
            except OSError:
                baska = ''
        if baska:
            ayrinti.append(('alan', (t('Sonraki adım'), t('Proje adı {a} başka klasöre kayıtlı: {b}. Görevi başka proje adıyla yeniden ver ya da o kaydı kaldır, sonra görevi yeniden başlat.',
                                                          a=istek_adi, b=baska))))
        else:
            ayrinti.append(('alan', (t('Sonraki adım'), t('Proje adı eşleşmiyor: istek {a}, kayıtlı {b}. Klasörü istek adıyla kaydet ya da kaydı düzelt, sonra görevi yeniden başlat.',
                                                          a=istek_adi, b=kayitli or t('yok')))))
    if not aktif:
        rv = fr.get('review') or {}
        ayrinti += [('bolum', t('Sonuç')), ('alan', (t('Test'), str((fr.get('qa') or {}).get('status') or '—'))),
                    ('alan', (t('Hakem'), f"{rv.get('verdict') or rv.get('status') or '—'} · {model_kisa(rv.get('model'))}")),
                    ('alan', (t('Değişiklik'), degisiklik_ozeti(bilgi.get('kisa', '')))),
                    ('alan', (t('Teslim'), next((c['ozet'] for c in cocuklar if c.get('id') == 'teslim'), '—')))]
    baglam = [('baslik', t('Son olaylar')), ('kayit', olaylar(home, rid)), ('baslik', t('Harcama')),
              ('alan', (t('Toplam'), ekonomi['ozet'])), ('alan', (t('Kayıt'), f'runs/{rid}/'))]
    if durum == 'running':
        isaret = 'simdi'
    elif durum == 'failed' or str(fr.get('status') or '').upper() == 'FAIL':
        isaret = 'hata'
    elif durum in ('completed', 'implemented'):
        isaret = 'bitti'
    else:
        isaret = 'bekliyor'
    if held and aktif:
        ayrinti += [('alan', (t('Duruş / karar'), asama))]
    ozet = f"{durum_yazi} · {proje_adi(k)} · {goreli_yas(k.get('created'))}"
    icinde = [(c['ad'], t(cocuk_yazi[c['id']])) for c in cocuklar if c.get('id') in cocuk_yazi]
    return d(ilk, ozet, ayrinti, cocuklar, isaret, baglam, id=rid, kosu=rid, proje=k.get('project'),
             teslim=teslim or None, aktif=aktif, beklemede=aktif and durum != 'running',
             girdi_bekliyor=bool(request) and aktif and sozluk(k.get('planning')).get('status') != 'accepted',
             yuzde=yuzde, asama=asama, created=sayi(k.get('created')), su_an=su_an, saat=istek_saati(k),
             durum_yazi=durum_yazi, yas=goreli_yas(k.get('created')), uyarilar=uyarilar,
             aciklama=aciklama(t('Bu görev'), metin, icinde), dosya={'tur': 'metin', 'yol': str(k.get('prompt') or dizin / 'prompt.md'), 'baslik': t('Görev metni')})


def gecmis_dugumu(home: Path, k: dict) -> dict:
    directory = kosu_dizini(home, k['run_id'])
    scope = ekonomi_kapsami(home, k, kayit_oku(directory / 'proje.json'))
    paths = [directory / 'proje.json', directory / 'operator-actions', *sorted((directory / 'operator-actions').glob('*.json')),
             *(kosu_dizini(home, rid) / 'run.json' for rid in scope),
             home / 'zincir/request-budget.db', home / 'zincir/request-budget.db-wal']
    stamps = tuple((str(p), p.stat().st_mtime_ns if p.exists() else 0) for p in paths)
    anahtar = (k['run_id'], str(home.resolve()), k.get('updated'), k.get('teslim_merge'), k.get('teslim_push'), dil.DIL, urun_izi(k), stamps)
    if anahtar not in BITEN:
        for eski in [a for a in list(BITEN) if a[0] == k['run_id']]:
            BITEN.pop(eski, None)
        dugum = gorev_dugumu(home, k)
        BITEN[anahtar] = dugum
    dugum = BITEN[anahtar]
    fr = k.get('final_result') or {}
    sonuc = {'PASS': t('geçti'), 'FAIL': t('düştü'), 'UYGULANDI': t('uygulandı')}.get(str(fr.get('status') or '').upper()) or t(
        DURUM_ADI.get(str(k.get('state') or ''), str(k.get('state') or '—')))
    dugum['ozet'] = f"{sonuc} · {proje_adi(k)} · {goreli_yas(k.get('created'))}" + (f" · {t('teslim bekliyor')}" if dugum.get('teslim') else '')
    dugum.update(durum_yazi=sonuc, yas=goreli_yas(k.get('created')))
    return BITEN[anahtar]


def defter_satirlari(home: Path, sorgu: str, parametre: tuple) -> list:
    db = home / 'zincir/request-budget.db'
    if not db.exists():
        return []
    try:
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as con:
            return con.execute(sorgu, parametre).fetchall()
    except sqlite3.Error:
        return []


def nakit_siniri(home: Path | None = None) -> float | None:
    from aylik_butce import cap_micro
    try:
        return cap_micro(home) / 1e6
    except Exception:
        return None


def nakit_durumu(home: Path) -> dict:
    from aylik_butce import PROVIDERS, MonthlyLedger, period
    sinir = nakit_siniri(home)
    db = home / 'zincir/aylik-butce.db'
    harcanan = askida = 0.0
    biliniyor = True
    paylar: dict[str, float] = {}
    if db.exists():
        try:
            with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as con:
                h, a = MonthlyLedger.usage(con, period())
                isaretler = ','.join('?' * len(PROVIDERS))
                for saglayici, tutar in con.execute(f'select provider, coalesce(sum(actual),0) from reservation where period=? and provider in '
                                                    f'({isaretler}) and actual is not null group by provider', (period(), *sorted(PROVIDERS))):
                    paylar[saglayici] = tutar / 1e6
            harcanan, askida = h / 1e6, a / 1e6
        except sqlite3.Error:
            biliniyor = False
    return {'sinir': sinir, 'harcanan': harcanan, 'askida': askida, 'paylar': paylar, 'donem': period(), 'biliniyor': biliniyor}


def olcum_yazi(gorunum: tuple[dict, float]) -> str:
    g, yas = gorunum
    if not g or yas >= 10 ** 8:
        return t('ölçüm yok')
    return t('ölçüm az önce') if yas < 60 else t('ölçüm {n} dk önce', n=int(yas // 60))


def olcum_taze(gorunum: tuple[dict, float]) -> bool:
    g, yas = gorunum
    return bool(g) and yas <= 90 and g.get('bugun') == time.strftime('%Y-%m-%d')


def butce_gorunumu(home: Path) -> tuple[dict, float]:
    try:
        g, yas = sonda('butce')(home)
    except Exception:
        return {}, 1e9
    return (g, sayi(yas)) if isinstance(g, dict) and g else ({}, 1e9)


def go_gecmisi(home: Path) -> list[dict]:
    return [g for g in _oku(home / 'zincir/go-pencere.json').get('gecmis') or [] if isinstance(g, dict)][-24:]


def or_gunluk(home: Path) -> list[tuple[str, float, int]]:
    satirlar = defter_satirlari(home, "select date(created, 'unixepoch', 'localtime'), coalesce(sum(actual),0), count(*) from reservation "
                                      "where provider='openrouter' and actual is not null and created >= ? group by 1 order by 1",
                                (time.time() - 7 * 86400,))
    return [(str(gun), sayi(tutar) / 1e6, int(adet)) for gun, tutar, adet in satirlar]


def butce_bolumu(home: Path, kayitlar: list[dict], gorunum: tuple[dict, float]) -> dict:
    g, yas = gorunum
    go, orr, zen, cl = (sozluk(g.get(x)) for x in ('go', 'or', 'zen', 'claude'))
    kendi = sozluk(go.get('kendi'))
    sifir = sozluk(_oku(home / 'zincir/go-pencere.json').get('resets'))
    olcum = olcum_yazi(gorunum)

    def yuzde(x: Any) -> str:
        return oran(x) if g and x is not None else '—'

    def tutar(x: Any) -> str:
        return para(x) if g and x is not None else '—'

    def cubuk(ad: str, x: Any) -> tuple:
        return ('cubuk', (ad, sayi(x))) if g and x is not None else ('alan', (ad, '—'))
    nakit = nakit_durumu(home)

    def nakit_tutar(value: float) -> str:
        return para(value) if nakit['biliniyor'] else t('bilinmiyor')

    sinir = nakit['sinir']
    sinir_yazi = para(sinir) if sinir is not None else t('geçersiz')
    harcayan: dict[str, float] = {}
    for k in kayitlar:
        harcayan[proje_adi(k)] = harcayan.get(proje_adi(k), 0.0) + sayi(k.get('budget_spent_usd'))
    baglam = [('baslik', t('En çok harcayan'))] + ([('alan', (p[:16], para(v))) for p, v in sorted(harcayan.items(), key=lambda x: -x[1])[:6] if v > 0]
                                                 or [('soluk', t('harcama yok'))]) + [('soluk', olcum)]
    gecmis = go_gecmisi(home)
    gidisat = [('bolum', t('Gidişat')), ('alan', (t('Aylık'), gidisat_yazi([sayi(x.get('monthly')) for x in gecmis]))),
               ('alan', (t('Haftalık'), gidisat_yazi([sayi(x.get('weekly')) for x in gecmis]))),
               ('alan', (t('5 saatlik'), gidisat_yazi([sayi(x.get('rolling')) for x in gecmis]))),
               ('bolum', t('Son ölçümler')), ('kayit', [f"{saat(x.get('ts'))}   5s {oran(x.get('rolling'))}   {t('hafta')} {oran(x.get('weekly'))}   "
                                                      f"{t('ay')} {oran(x.get('monthly'))}" for x in reversed(gecmis[-6:])])] if gecmis else []

    def pencere(ad: str, anahtar: str, kimlik: str) -> dict:
        y = kendi.get(anahtar)
        return d(t(ad), yuzde(y), [('baslik', t(ad)), cubuk(t('Kullanım'), y), ('alan', (t('Sıfırlanma'), iso_yerel(sifir.get(anahtar)))),
                                 ('bolum', t('Gidişat')), ('alan', (t('Son ölçümler'), gidisat_yazi([sayi(x.get(anahtar)) for x in gecmis])))],
                 baglam=baglam, id=kimlik)

    def cl_pencere(ad: str, anahtar: str, kimlik: str) -> dict:
        y = cl.get(anahtar)
        return d(t(ad), yuzde(y), [('baslik', t(ad)), cubuk(t('Kullanım'), y), ('alan', (t('Üst sınır'), yuzde(cl.get('tavan'))))],
                 baglam=baglam, id=kimlik)
    son_or = defter_satirlari(home, "select created, json_extract(metadata,'$.model'), json_extract(metadata,'$.task_id'), actual from reservation "
                                    "where provider='openrouter' and actual is not null and actual > 0 order by created desc limit 10", ())
    gunluk = or_gunluk(home)
    or_kalan = orr.get('kalan')
    go_aylik = kendi.get('monthly')
    go_kalan = go.get('kullanilabilir')
    go_az = go_kalan is not None and sayi(go_kalan) < 10
    or_az = orr.get('kullanilabilir') is not None and sayi(orr.get('kullanilabilir')) < 5
    claude_yazi = t('kapalı') if claude_kapali() else t('haftalık {y}', y=yuzde(cl.get('used7')))
    paylar = nakit['paylar']
    yedi_gun = [('bolum', t('Son 7 gün')), ('alan', (t('Gidişat'), grafik([a for _, a, _ in gunluk])))] + [
        ('alan', (f'{gun[8:10]}.{gun[5:7]}', t('{p} · {n} istek', p=para(a), n=n))) for gun, a, n in gunluk] if gunluk else []
    cocuk = [
        d(t('Go'), t('aylık {y}', y=yuzde(go_aylik)), [
            ('baslik', t('Go (abonelik)')), cubuk(t('Aylık'), go_aylik), ('alan', (t('Haftalık'), yuzde(kendi.get('weekly')))),
            ('alan', (t('5 saatlik'), yuzde(kendi.get('rolling')))),
            ('alan', (t('Cadre için kalan'), t('{n} puan', n=f'{sayi(go_kalan):.0f}') if go_kalan is not None else '—')),
            ('alan', (t('Pencere'), '—' if not go else (t('açık') if go.get('pencere_acik', True) else t('kapalı · {n}', n=go.get('pencere_neden') or '—')))),
            ('alan', (t('Aylık sıfırlanma'), iso_yerel(sifir.get('monthly')))), ('soluk', olcum)] + gidisat,
          [pencere('5 saatlik', 'rolling', 'go-5s'), pencere('Haftalık', 'weekly', 'go-hafta'), pencere('Aylık', 'monthly', 'go-ay')],
          'hata' if go_az else ('bitti' if go else ''), baglam, id='go',
          aciklama=aciklama(t('Go'), t('Aylık abonelik. Kullanım puanla ölçülür. Pencereler sıfırlanınca yeniden açılır.'),
                            [(t('5 saatlik'), t('kısa pencere')), (t('Haftalık'), t('haftalık pencere')), (t('Aylık'), t('aylık pencere'))])),
        d(t('OpenRouter'), tutar(or_kalan), [
            ('baslik', t('OpenRouter')), ('alan', (t('Bakiye'), tutar(or_kalan))), ('alan', (t('Kullanılabilir'), tutar(orr.get('kullanilabilir')))),
            ('alan', (t('Bu ay'), nakit_tutar(paylar.get('openrouter', 0)))), ('soluk', olcum)] + yedi_gun, [
            d(t('Bakiye'), tutar(or_kalan), [('baslik', t('Bakiye')), ('alan', (t('Şu an'), tutar(or_kalan))),
                                            ('alan', (t('Kullanılabilir'), tutar(orr.get('kullanilabilir')))), ('soluk', olcum)], baglam=baglam, id='or-bakiye'),
            d(t('Bu ay'), nakit_tutar(paylar.get('openrouter', 0)), [('baslik', t('Bu ay')), ('alan', (t('Harcanan'), nakit_tutar(paylar.get('openrouter', 0)))),
                                                              ('alan', (t('Dönem'), nakit['donem']))] + yedi_gun, baglam=baglam, id='or-ay'),
            d(t('Son harcamalar'), t('{n} istek', n=len(son_or)), [('baslik', t('Son harcamalar')), ('kayit', [
                f"{saat(c)} {model_kisa(m):<18} {str(g_ or '-'):<8} {para((a or 0) / 1e6)}" for c, m, g_, a in son_or])], baglam=baglam, id='or-son')],
          'hata' if or_az else ('bitti' if orr else ''), baglam, id='openrouter',
          aciklama=aciklama(t('OpenRouter'), t('Ön ödemeli bakiye. Harcama aylık nakit sınırından da düşer.'),
                            [(t('Bakiye'), t('şu anki bakiye')), (t('Bu ay'), t('bu ayki harcama ve son 7 gün')), (t('Son harcamalar'), t('son 10 istek'))])),
        d(t('Aylık nakit sınırı'), f"{nakit_tutar(nakit['harcanan'])} / {sinir_yazi}", [
            ('baslik', t('Aylık nakit sınırı')),
            ('cubuk', (t('Bu ay'), 100 * (nakit['harcanan'] + nakit['askida']) / sinir)) if sinir and nakit['biliniyor'] else ('alan', (t('Bu ay'), '—')),
            ('alan', (t('Harcanan'), nakit_tutar(nakit['harcanan']))), ('alan', (t('Askıda'), nakit_tutar(nakit['askida']))),
            ('alan', (t('Sınır'), sinir_yazi)),
            ('alan', (t('Kalan'), nakit_tutar(max(0.0, sinir - nakit['harcanan'] - nakit['askida'])) if sinir is not None else '—')),
            ('alan', (t('Dönem'), nakit['donem']))], [
            d(t('OpenRouter payı'), nakit_tutar(paylar.get('openrouter', 0)), [('baslik', t('OpenRouter payı')),
                                                                        ('alan', (t('Harcanan'), nakit_tutar(paylar.get('openrouter', 0))))], baglam=baglam, id='nakit-or'),
            d(t('Zen payı'), nakit_tutar(paylar.get('opencode', 0)), [('baslik', t('Zen payı')), ('alan', (t('Harcanan'), nakit_tutar(paylar.get('opencode', 0))))],
              baglam=baglam, id='nakit-zen')], baglam=baglam, id='nakit',
          aciklama=aciklama(t('Aylık nakit sınırı'), t('OpenRouter ve Zen bu ay en çok bu kadar harcar. Go ve Claude abonelik olduğu için sayılmaz.'),
                            [(t('OpenRouter payı'), t('bu ay OpenRouter')), (t('Zen payı'), t('bu ay Zen'))])),
        d(t('Zen'), tutar(zen.get('kalan')), [
            ('baslik', t('Zen (yedek)')), ('alan', (t('Bakiye'), tutar(zen.get('kalan')))), ('alan', (t('Bu ay'), nakit_tutar(paylar.get('opencode', 0))))], [
            d(t('Bakiye'), tutar(zen.get('kalan')), [('baslik', t('Bakiye')), ('alan', (t('Şu an'), tutar(zen.get('kalan'))))], baglam=baglam, id='zen-bakiye'),
            d(t('Ne zaman kullanılır'), t('yedek'), [('baslik', t('Ne zaman kullanılır')),
                                                     ('satir', t('Zincirin son halkasıdır. Harcaması aylık nakit sınırından düşer.'))],
              baglam=baglam, id='zen-ne-zaman')], baglam=baglam, id='zen',
          aciklama=aciklama(t('Zen'), t('Ön ödemeli yedek. Yalnız OpenRouter ve Go kullanılamazsa devreye girer.'))),
        d(t('Claude'), claude_yazi, [
            ('baslik', t('Claude')), ('alan', (t('Durum'), t('cadre içinde kapalı') if claude_kapali() else t('cadre içinde açık'))),
            ('alan', (t('Haftalık'), yuzde(cl.get('used7')))), ('alan', (t('5 saatlik'), yuzde(cl.get('used5')))),
            ('alan', (t('Üst sınır'), yuzde(cl.get('tavan'))))],
          [cl_pencere('5 saatlik', 'used5', 'claude-5s'), cl_pencere('Haftalık', 'used7', 'claude-hafta')], baglam=baglam, id='claude',
          aciklama=aciklama(t('Claude'), t('Abonelik. Cadre içinde açıksa planlama zincirinin başına girer.'))),
    ]
    ayrinti = [('baslik', t('Bütçe')), ('alan', (t('Go aylık'), yuzde(go_aylik))), ('alan', (t('OpenRouter'), tutar(or_kalan))),
               ('alan', (t('Bu ay nakit'), f"{nakit_tutar(nakit['harcanan'])} / {sinir_yazi}")), ('alan', (t('Zen'), tutar(zen.get('kalan')))),
               ('alan', (t('Claude'), claude_yazi)), ('soluk', olcum)] + yedi_gun
    return d(t('Bütçe'), t('OR {p}', p=tutar(or_kalan)) if g else t('ölçüm yok'), ayrinti, cocuk, baglam=baglam, id='butce',
             aciklama=aciklama(t('Bütçe'), t('Para ve kota. Go aboneliği, OpenRouter bakiyesi, aylık nakit sınırı, Zen yedeği ve Claude kotası.'),
                               [(t('Go'), t('aylık abonelik, puanla ölçülür')), (t('OpenRouter'), t('ön ödemeli bakiye')),
                                (t('Aylık nakit sınırı'), t('OpenRouter ve Zen için aylık tavan')), (t('Zen'), t('yedek bakiye')),
                                (t('Claude'), t('abonelik kotası'))]))


def katalog_bilgisi(model: str) -> tuple[str, str, str]:
    import katalog
    import yonlendirici as Y
    saglayici = Y.saglayici(model)
    ad = (model.split('/', 1)[1] if '/' in model else model).split('#')[0]
    baglam = katalog.baglam(saglayici, ad)
    fiyat = katalog.fiyat(saglayici, ad)
    if saglayici == 'claude':
        fiyat_yazi = t('abonelik kotasından düşer')
    elif fiyat and ('input' in fiyat or 'output' in fiyat):
        fiyat_yazi = t('giriş {g} · çıkış {c} / 1M token', g=para(fiyat.get('input')), c=para(fiyat.get('output')))
        if saglayici == 'opencode-go':
            fiyat_yazi += ' · ' + t('liste fiyatı, Go puanından düşer')
        elif saglayici == 'groq':
            fiyat_yazi += ' · ' + t('ücretsiz katmanda ödenmez')
    else:
        fiyat_yazi = t('katalogda yok')
    return t(SAGLAYICI_ADI.get(saglayici, saglayici)), token_yazi(baglam) if baglam else t('katalogda yok'), fiyat_yazi


def ajan_bolumu(home: Path, gorunum: tuple[dict, float]) -> dict:
    import copy

    import yonlendirici as Y
    b = gorunum[0]
    olcum = olcum_yazi(gorunum)
    taze = olcum_taze(gorunum)
    bekleme = (t('ölçüm eski'), t('bütçe ölçümü eski, sıra yeni ölçümle hesaplanır')) if b else (t('ölçüm yok'), t('bütçe ölçümü yok, sıra hesaplanamadı'))
    bugun = time.mktime(time.strptime(time.strftime('%Y-%m-%d'), '%Y-%m-%d'))
    sayac = {str(m): (n, a, son) for m, n, a, son in defter_satirlari(
        home, "select json_extract(metadata,'$.model'), count(*), coalesce(sum(actual),0), max(created) from reservation "
              "where created >= ? group by 1", (bugun,))}
    try:
        ariza = sozluk(sonda('ariza')())
    except Exception:
        ariza = {}
    kapali = claude_kapali()
    cocuk = []
    for rol, (rol_adi, rol_metni) in ROL_BILGI.items():
        adaylar = list(dict.fromkeys(str(a) for a in Y.ROLLER.get(rol, [])))
        if not adaylar:
            continue
        zincir, nedenler = sonda('zincir')(rol, b, copy.deepcopy(ariza)) if taze else ([], [])
        zincir, nedenler = list(zincir), [str(x) for x in nedenler]
        halkalar = []
        for i, aday in enumerate(adaylar, 1):
            secenekler = aday.split('|')
            m = next((x for x in secenekler if x in zincir), secenekler[0])
            gorunen = m if m in zincir else ' | '.join(secenekler)
            kisa = m.split('/', 1)[-1].split('#')[0]
            n, a, son = sayac.get(kisa) or sayac.get(m) or (0, 0, 0)
            hata = sozluk(ariza.get(m) or ariza.get(kisa))
            if not taze:
                durum, (hal, neden) = 'bekliyor', bekleme
            elif m in zincir:
                sira = zincir.index(m)
                durum, hal = ('simdi' if sira == 0 else 'bitti'), (t('ilk tercih') if sira == 0 else t('{n}. sırada', n=sira + 1))
                neden = t('zincirde, payı ve bağlamı uygun')
            elif kapali and all(x.startswith('claude/') for x in secenekler):
                durum, hal, neden = 'hata', t('elendi'), t('Claude cadre içinde kapalı')
            else:
                durum, hal = 'hata', t('elendi')
                neden = next((x.split(':', 1)[1].strip() for x in reversed(nedenler) for y in secenekler if x.startswith(y + ':')),
                             t('bu çağrı için uygun değil'))
            saglayici, baglam, fiyat = katalog_bilgisi(m)
            secim = [('alan', (t('Seçenekler'), t('{a} Claude payı 4 puan ve üstündeyse, değilse {b}', a=secenekler[0], b=secenekler[1])))] \
                if len(secenekler) == 2 else []
            halkalar.append(d(f'{i}. {gorunen}', hal, [
                ('baslik', gorunen), ('alan', (t('Durum'), hal)), ('alan', (t('Neden'), neden))] + secim + [
                ('alan', (t('Sağlayıcı'), saglayici)), ('alan', (t('Context'), baglam)), ('alan', (t('Fiyat'), fiyat)),
                ('alan', (t('Son kullanım'), saat(son))), ('alan', (t('Bugün istek'), str(n))), ('alan', (t('Bugün harcama'), para(sayi(a) / 1e6))),
                ('alan', (t('Hata sayısı'), str(hata.get('hata', 0)))), ('alan', (t('Son hata'), str(hata.get('son') or '—')[:120]))],
                durum=durum, id=aday))
        sira_listesi = zincir if taze else [a.replace('|', ' | ') for a in adaylar]
        if taze:
            ozet, ilk = (model_kisa(zincir[0]), zincir[0]) if zincir else (t('zincir boş'), t('yok, bütçe bekler'))
        else:
            ozet = ilk = bekleme[0]
        cocuk.append(d(t(rol_adi), ozet, [
            ('baslik', t(rol_adi)), ('alan', (t('İlk tercih'), ilk)),
            ('alan', (t('Kullanılabilir halka'), f'{len(zincir)} / {len(adaylar)}' if taze else '—')), ('soluk', olcum), ('bolum', t('Zincir sırası'))] +
            [('satir', f'{j}. {m}') for j, m in enumerate(sira_listesi, 1)], halkalar, ('simdi' if zincir else 'hata') if taze else 'bekliyor',
            [('baslik', t('Zincir sırası')), ('satir', t('Üstten aşağı denenir. Payı biten ya da düşen halka atlanır.'))] +
            [('soluk', f'{j}. {m}') for j, m in enumerate(sira_listesi, 1)], id=rol,
            aciklama=aciklama(t(rol_adi), t(rol_metni), ek=[('soluk', t('Zincir üstten aşağı denenir. Payı biten ya da düşen halka atlanır.'))])))
    hafif = next((node for node in cocuk if node['id'] == 'impl-hafif'), None)
    developer = next((node for node in cocuk if node['id'] == 'impl'), None)
    if hafif and developer:
        hafif['ad'] = t('Hafif geliştirme')
        developer['cocuk'].append(hafif)
    cocuk = [node for node in cocuk if node['id'] in ('plan', 'impl', 'kalite')]
    cocuk.append(on_degerlendirme_dugumu(home))
    en_cok = sorted(sayac.items(), key=lambda x: -x[1][0])[:6]
    ayrinti = [('baslik', t('Ajanlar'))] + [('alan', (c['ad'], c['ozet'])) for c in cocuk] + [('soluk', t('ilk tercih, rolün şu an kullanacağı model')),
                                                                                           ('soluk', olcum)]
    ayrinti += sonuc_olcum_dugumu(home)['ayrinti'][1:]
    return d(t('Ajanlar'), t('{n} rol', n=len(cocuk)), ayrinti, cocuk, id='ajanlar',
             aciklama=aciklama(t('Ajanlar'), t('Hangi işi hangi model yapar. Her rolün model sırası, elenenler ve nedeni, bugünkü kullanım. Dakikada bir tazelenir.'),
                               [(c['ad'], t(ROL_BILGI[c['id']][1]) if c['id'] in ROL_BILGI else t('Görevi zorluğuna göre sınıflar.')) for c in cocuk]),
             baglam=[('baslik', t('Bugün en çok kullanılan'))] + ([('alan', (model_kisa(m)[:16], t('{n} istek · {p}', n=n, p=para(a / 1e6))))
                                                                    for m, (n, a, _) in en_cok] or [('soluk', t('bugün istek yok'))]))


def sonuc_olcum_dugumu(home: Path) -> dict:
    """CR-39: outcome measures written from real acceptance decisions; does not change the selection, missing data stays unknown."""
    from model_secimi import sonuc_ozeti
    try:
        ozet = sonuc_ozeti(home)
    except (OSError, ValueError, TypeError):
        return d(t('Sonuç ölçümleri'), t('okunamadı'), [('baslik', t('Sonuç ölçümleri')), ('satir', t('Ölçüm kaydı okunamadı.'))], durum='hata', id='sonuc-olcum')
    satirlar = []
    for g in ozet['gruplar']:
        maliyet = para(g['maliyet_micro'] / 1e6) + (f" (+{g['maliyet_bilinmeyen']} {t('bilinmiyor')})" if g['maliyet_bilinmeyen'] else '')
        satirlar.append(('alan', (f"{g['rol']} · {g['model']}", t('{k} kabul / {n} örnek · {t} taze · {m}', k=g['kabul'], n=g['ornek'], t=g['taze'], m=maliyet)
                                  + (' · ' + ', '.join(g['belirsizlik']) if g['belirsizlik'] else ''))))
    ayrinti = [('baslik', t('Sonuç ölçümleri')), ('satir', t('Gerçek kabul kararından yazılır ve seçim sırasını değiştirmez. Politika değişikliği açık ayar kararıdır.')),
               ('alan', (t('Kayıt'), t('{n} örnek · {b} bozuk', n=ozet['toplam'], b=ozet['bozuk']))),
               ('alan', (t('Politika'), ', '.join(ozet['politikalar']) or '—'))] + (satirlar or [('soluk', t('Henüz ölçüm yok.'))])
    return d(t('Sonuç ölçümleri'), t('{n} örnek', n=ozet['toplam']), ayrinti, id='sonuc-olcum',
             aciklama=aciklama(t('Sonuç ölçümleri'), t('Rol/model/politika bazında kabul, başarısızlık, maliyet ve süre örnekleri. Az veya eski örnek belirsizlik olarak işaretlenir.')))


def on_degerlendirme_dugumu(home: Path) -> dict:
    from butce_kapisi import GUNLUK_ISTEK
    from onelek import MODEL
    model = '/'.join(MODEL)
    try:
        gun, adet = (home / 'zincir/groq-gun-sayac.txt').read_text().split()[:2]
        bugun = int(adet) if gun == time.strftime('%Y-%m-%d') else 0
    except (OSError, ValueError):
        bugun = 0
    saglayici, baglam, fiyat = katalog_bilgisi(model)
    return d(t('Ön değerlendirici'), model_kisa(model), [
        ('baslik', t('Ön değerlendirici')), ('alan', (t('Model'), model)), ('alan', (t('Sağlayıcı'), saglayici)), ('alan', (t('Context'), baglam)),
        ('alan', (t('Fiyat'), fiyat)), ('alan', (t('Günlük sınır'), t('{n} istek', n=GUNLUK_ISTEK.get('groq', '—')))),
        ('alan', (t('Bugün'), t('{n} istek', n=bugun))), ('bolum', t('Ne yapar')),
        ('satir', t('Görev gelince zorluğunu BASİT, DAĞITIM ya da PREMIUM diye sınıflar. Para harcamaz.'))], durum='bitti', id='on-degerlendirme')


def ayar_listesi(home: Path | None = None) -> list[tuple]:
    from butce_kapisi import ALARM_PENCERE_SN
    from kosu import DISK_MIN_MB_VARSAYILAN

    def ortam(ad: str, varsayilan: str) -> tuple[str, str]:
        return (os.environ.get(ad) or varsayilan, t('ortam değişkeni') if os.environ.get(ad) else t('varsayılan'))
    tavan = t('Proje bazında')
    paralel, p_kaynak = ortam('CADRE_PARALEL_SERIT', '2')
    disk, d_kaynak = ortam('CADRE_DISK_MIN_MB', str(DISK_MIN_MB_VARSAYILAN))
    pencere = int(ALARM_PENCERE_SN // 60)
    sinir = nakit_siniri(home)
    return [
        ('ust-sinir', t('Üst sınır'), tavan, t('ortam değişkeni') if os.environ.get('CADRE_KOSU_TAVAN_USD') else t('varsayılan'),
         t('Yeni koşuların başlangıç tavanı. Mevcut koşu kayıtlarını değiştirmez.'),
         t('Enter ile kayıtlı projeyi seç.'), 'CADRE_KOSU_TAVAN_USD'),
        ('nakit', t('Aylık nakit sınırı'), para(sinir) if sinir is not None else t('geçersiz'), t('ortam değişkeni') if os.environ.get('CADRE_AYLIK_TAVAN_USD') else t('varsayılan'),
         t('OpenRouter ve Zen bu ay en çok bu kadar harcar.'), t('Enter ile düzenle. Ortam değişkeni dosyadan önce gelir.'), 'CADRE_AYLIK_TAVAN_USD'),
        ('alarm', t('Harcama alarmı'), t('kapalı') if os.environ.get('CADRE_HARCAMA_ALARMI') == '0' else t('açık'),
         t('ortam değişkeni') if os.environ.get('CADRE_HARCAMA_ALARMI') else t('varsayılan'),
         t('{d} dakikalık yüksek harcama hızını uyarı olarak gösterir. Kesin alarm ayrıca onay bekler.', d=pencere),
         t('CADRE_HARCAMA_ALARMI=0 kapatır'), 'cadre harcama-onay <run_id>'),
        ('paralel', t('Aynı anda kodlama'), paralel, p_kaynak, t('Birbirinden bağımsız alt görevler aynı anda yazılır.'), 'CADRE_PARALEL_SERIT',
         'CADRE_PARALEL_SERIT=1'),
        ('claude', t('Claude'), t('kapalı') if claude_kapali() else t('açık'), t('zincir/claude-kapali dosyası'),
         t('Claude planlama zincirine girer mi.'), t('dosyayı silmek açar, oluşturmak kapatır'), 'touch zincir/claude-kapali'),
        ('teslim', t('Teslim'), t('iki onay'), t('kural'), t('Push ve merge önce komutu gösterir, sonra ikinci onayı ister.'), t('değişmez'), '—'),
        ('disk', t('Disk eşiği'), f'{disk} MB', d_kaynak, t('Boş disk bu eşiğin altına inerse yeni görev başlamaz.'), 'CADRE_DISK_MIN_MB',
         'CADRE_DISK_MIN_MB=4096'),
    ]


def butce_ayar_dugumu(home: Path, tur: str, proje: str = '') -> dict:
    from kosu import butce_ayari
    ad = proje if tur == 'kosu' else t('Aylık nakit sınırı')
    try:
        state = butce_ayari(home, tur, proje)
    except (ValueError, OSError):
        return d(ad, t('geçersiz'), [('baslik', ad), ('alan', (t('Değer'), t('geçersiz'))),
                 ('soluk', t('Bütçe ayarı okunamadı veya geçersiz. Ücretli işlem bekler.'))],
                 durum='hata', id='nakit' if tur == 'aylik' else f'butce-ayar-{tur}-{proje}')
    source = {'ortam': t('ortam değişkeni'), 'dosya': t('dosya'), 'varsayilan': t('varsayılan')}[state['kaynak']]
    return d(ad, para(state['deger']), [('baslik', ad), ('alan', (t('Değer'), para(state['deger']))), ('alan', (t('Kesin değer (USD)'), str(state['deger']))),
             ('alan', (t('Kaynak'), source)), ('satir', t('Yeni koşuların başlangıç tavanı. Mevcut koşu kayıtlarını değiştirmez.') if tur == 'kosu'
              else t('Ortak aylık nakit tavanı. Harcanan ve askıdaki tutarlar korunur.')),
             ('satir', t('Ortam etkin: dosyayı değiştirmek bu değeri değiştirmez.') if state['kaynak'] == 'ortam' else t('Enter ile düzenle. Ortam değişkeni dosyadan önce gelir.')),
             ('bolum', t('Teknik ayrıntı')), ('alan', (t('Dosya'), state['yol'])), ('alan', (t('Ortam değişkeni'), state['degisken']))],
             id='nakit' if tur == 'aylik' else f'butce-ayar-{tur}-{proje}', eylem='butce-ayari' if state['kaynak'] != 'ortam' else '', butce_ayar=state)


def ayar_bolumu(home: Path | None = None) -> dict:
    home = home or ev()
    try:
        cocuk = [d(ad, deger[:24], [('baslik', ad), ('alan', (t('Değer'), deger)), ('alan', (t('Kaynak'), kaynak)), ('alan', (t('Ne yapar'), ne)),
                                    ('alan', (t('Nasıl değişir'), nasil)), ('alan', (t('Örnek'), ornek))],
                   baglam=[('baslik', t('Nasıl değişir')), ('satir', nasil), ('soluk', t('ayrıntı: engine/USAGE.md'))], id=kimlik)
                 for kimlik, ad, deger, kaynak, ne, nasil, ornek in ayar_listesi(home)]
        cocuk[1] = butce_ayar_dugumu(home, 'aylik')
        cocuk[0]['cocuk'] = [butce_ayar_dugumu(home, 'kosu', p) for p in projeler(home)]
        cocuk[0]['ayrinti'].append(('soluk', t('Plan tahmini sabit operatör toplamını büyütmez. İç paylar aynı toplam içinde aktarılır.')))
        if not cocuk[0]['cocuk']:
            cocuk[0]['ayrinti'].append(('soluk', t('Kayıtlı proje yok.')))
    except Exception as exc:
        cocuk = [d(t('Sistem ayarları'), t('okunamadı'), [('baslik', t('Sistem ayarları')), ('soluk', t('okunamadı: {e}', e=hata_yazi(exc)))],
                   durum='hata', id='ayar-hata')]
    dil_adi = 'Türkçe' if dil.DIL == 'tr' else 'English'
    renk = dil.renk_oku()
    cocuk.append(d(t('Dil'), dil_adi, [('baslik', t('Dil')), ('alan', (t('Değer'), dil_adi)), ('alan', (t('Kaynak'), t('arayuz-dil dosyası ya da CADRE_DIL'))),
                                       ('alan', (t('Ne yapar'), t('Arayüzün dili. Kayıtlardaki metinler olduğu gibi kalır.'))),
                                       ('alan', (t('Nasıl değişir'), t('Enter ile Türkçe ve İngilizce arasında geçer.')))],
                   id='dil', eylem='dil'))
    cocuk.append(d(t('Renk paleti'), renk, [('baslik', t('Renk paleti')), ('alan', (t('Değer'), renk)),
                                             ('alan', (t('Seçenekler'), ', '.join(dil.RENKLER))),
                                             ('alan', (t('Kaynak'), t('arayuz-renk dosyası ya da CADRE_RENK'))),
                                             ('alan', (t('Ne yapar'), t('Arayüzün renkleri.'))),
                                             ('alan', (t('Nasıl değişir'), t('Enter ile sıradaki palete geçer.')))],
                   id='renk', eylem='renk'))
    cocuk.append(d(t('Test görevlerini göster'), t('açık') if dil.testleri_oku(home) else t('kapalı'), [
        ('baslik', t('Test görevleri')), ('alan', (t('Kaynak'), t('arayuz-testler dosyası'))),
        ('satir', t('Test koşularını görev ve olay listelerinde gösterir. Ürün başarı özetine katmaz.')),
        ('satir', t('Enter ile göster veya gizle.'))], id='testler', eylem='testler'))
    from kosu import gorev_varsayilanlari
    try:
        state = gorev_varsayilanlari(home)
        preference = t({'economic': 'Ekonomik', 'balanced': 'Dengeli', 'strong': 'Güçlü'}[state['preference']])
        difficulty = t({None: 'Otomatik tahmin bekleniyor', 'easy': 'Kolay', 'medium': 'Orta', 'hard': 'Zor', 'very_hard': 'Çok zor'}[state['difficulty_override']])
        cocuk.append(d(t('Maliyet tercihi ve zorluk'), preference + ' · ' + difficulty, [
            ('baslik', t('Yeni görev varsayılanları')), ('alan', (t('Maliyet tercihi'), preference)), ('alan', (t('Zorluk düzeltmesi'), difficulty)),
            ('alan', (t('Kaynak'), t('dosya') if state['kaynak'] == 'dosya' else t('varsayılan'))),
            ('satir', t('Yeni görev formunu doldurur. Mevcut görev, izin ve bütçe değişmez.')),
            ('satir', t('Görevdeki tercih ve zorluk model sırasına uygulanır. Plan ve izin kapıları ayrıca korunur.')),
            ('satir', t('Enter ile düzenle.')), ('bolum', t('Teknik ayrıntı')), ('alan', (t('Dosya'), state['yol']))],
            id='tercih', eylem='gorev-varsayilanlari', varsayilan_state=state))
    except (ValueError, OSError):
        cocuk.append(d(t('Maliyet tercihi ve zorluk'), t('okunamadı'), [('baslik', t('Yeni görev varsayılanları')),
            ('satir', t('Görev varsayılanları okunamadı. Ayarları kontrol et.'))], id='tercih', durum='hata'))
    ayrinti = [('baslik', t('Ayarlar'))] + [('alan', (c['ad'], c['ozet'])) for c in cocuk]
    return d(t('Ayarlar'), t('{n} ayar', n=len(cocuk)), ayrinti, cocuk, id='ayarlar',
             aciklama=aciklama(t('Ayarlar'), t('Para sınırları, dil, palet ve test görünürlüğü Enter ile düzenlenir. Teknik kaynak ayrıntıdadır.')),
             baglam=[('baslik', t('Nereden okunur')), ('satir', t('Önce ortam değişkeni, sonra proje dosyası, yoksa varsayılan.')),
                     ('soluk', t('ayrıntı: engine/USAGE.md'))])


def kosu_uyarilari(home: Path, k: dict) -> list[dict]:
    rid = str(k['run_id'])
    uyarilar = []
    warning = sozluk(k.get('harcama_uyarisi'))
    if warning.get('kind') == 'speed-warning' and 0 <= time.time() - sayi(warning.get('ts')) <= 600:
        uyarilar.append(d(t('Harcama hızı uyarısı · {p}', p=proje_adi(k)), t('uyarı'), harcama_uyarisi_ayrinti(k),
                         id=f'hiz-{rid}', git=['gorevler', rid], git_yazi=t('görevi açar')))
    alarm = sozluk(k.get('harcama_alarmi'))
    if alarm and sayi(alarm.get('ts')) > sayi(k.get('harcama_onayi_ts')):
        uyarilar.append(d(t('Harcama alarmı · {p}', p=proje_adi(k)), t('onay bekliyor'), [
            ('baslik', t('Harcama alarmı')), ('alan', (t('Neden'), str(alarm.get('neden') or ''))), ('alan', (t('Onay'), f'cadre harcama-onay {rid}')),
            ('soluk', t('Enter görevi açar.'))], durum='hata', id=f'alarm-{rid}', git=['gorevler', rid], git_yazi=t('görevi açar')))
    if k.get('state') == 'retryable' and str(k.get('reason') or '').startswith('butce-bekleme'):
        uyarilar.append(d(t('Bütçe bekleniyor · {p}', p=proje_adi(k)), t('bekliyor'), [
            ('baslik', t('Bütçe bekleniyor')), ('alan', (t('Not'), str(k.get('reason'))[:160])), ('soluk', t('Bütçe dönünce kendiliğinden sürer.'))],
            durum='bekliyor', id=f'butce-{rid}', git=['gorevler', rid], git_yazi=t('görevi açar')))
    ozellikler = kayit_oku(kosu_dizini(home, rid) / 'proje.json').get('ozellikler')
    takilan = [f for f in (ozellikler if isinstance(ozellikler, list) else []) if isinstance(f, dict) and f.get('durum') == 'takildi']
    if takilan:
        uyarilar.append(d(t('Takılan parça · {p}', p=proje_adi(k)), t('{n} parça', n=len(takilan)), [
            ('baslik', t('Takılan parça')), ('alan', (t('Parçalar'), ', '.join(f"{f.get('id')} {f.get('baslik', '')}" for f in takilan)[:200])),
            ('soluk', t('Son kontrol bu parçaları ayrıca dener. Olmazsa görev düşer.'))], durum='hata', id=f'takilan-{rid}',
            git=['gorevler', rid], git_yazi=t('görevi açar')))
    return uyarilar


def dikkat_dugumu(home: Path, ust: list[dict], biten: list[dict], gorunum: tuple[dict, float]) -> dict:
    uyarilar = []
    teslim = [b for b in biten if b.get('teslim')]
    if teslim:
        uyarilar.append(d(t('Teslim bekleyen görevler'), t('{n} görev', n=len(teslim)), [('baslik', t('Teslim bekleyen görevler')),
                                                                                       ('alan', (t('Görev'), str(len(teslim))))],
                          teslim, 'bekliyor', id='dikkat-teslim',
                          aciklama=aciklama(t('Teslim bekleyen görevler'), t('Bitti ve geçti. Push ya da merge senin kararın. İçine girip p ya da m.'))))
    dusen = [b for b in biten if b['durum'] == 'hata' and time.time() - b.get('created', 0) < 86400]
    if dusen:
        uyarilar.append(d(t('Düşen görevler (24 saat)'), t('{n} görev', n=len(dusen)), [('baslik', t('Düşen görevler')),
                                                                                      ('alan', (t('Görev'), str(len(dusen))))],
                          dusen, 'hata', id='dikkat-dusen',
                          aciklama=aciklama(t('Düşen görevler'), t('Son 24 saatte düşen görevler. İçine girip nedenine bak.'))))
    for k in ust:
        if k.get('state') in TERMINAL:
            continue
        try:
            uyarilar += kosu_uyarilari(home, k)
        except Exception:
            continue
    g, _ = gorunum
    go, orr = sozluk(g.get('go')), sozluk(g.get('or'))
    if go.get('kullanilabilir') is not None and sayi(go.get('kullanilabilir')) < 10:
        uyarilar.append(d(t('Go bu ay bitiyor'), t('kalan {n} puan', n=f"{sayi(go.get('kullanilabilir')):.0f}"), [
            ('baslik', t('Go bu ay neredeyse bitti')), ('alan', (t('Kalan'), t('{n} puan', n=f"{sayi(go.get('kullanilabilir')):.0f}"))),
            ('soluk', t('Görevler OpenRouter ile sürer.'))], durum='hata', id='dikkat-go', git=['butce', 'go'], git_yazi=t('Bütçe › Go açılır')))
    if orr.get('kullanilabilir') is not None and sayi(orr.get('kullanilabilir')) < 5:
        uyarilar.append(d(t('OpenRouter bakiyesi az'), para(orr.get('kullanilabilir')), [
            ('baslik', t('OpenRouter bakiyesi az')), ('alan', (t('Kullanılabilir'), para(orr.get('kullanilabilir'))))],
            durum='hata', id='dikkat-or', git=['butce', 'openrouter'], git_yazi=t('Bütçe › OpenRouter açılır')))
    olasi = [t('teslim bekleyen görev'), t('son 24 saatte düşen görev'), t('onay bekleyen harcama alarmı'), t('bütçe bekleyen görev'),
             t('takılan parça'), t('Go puanı ya da OpenRouter bakiyesi azalınca uyarı')]
    return d(t('Dikkat'), t('{n} konu', n=len(uyarilar)) if uyarilar else t('yok'),
             [('baslik', t('Dikkat'))] + ([('satir', f"{u['ad']}  ·  {u['ozet']}") for u in uyarilar] or [('soluk', t('bakman gereken bir şey yok'))]),
             uyarilar, 'hata' if uyarilar else '', id='dikkat',
             aciklama=aciklama(t('Dikkat'), t('Senin bakman ya da karar vermen gerekenler.'), ek=[('bolum', t('Burada çıkabilecekler')), ('kayit', olasi)]))


def ozet_dugumu(home: Path, ust: list[dict]) -> dict:
    bugun = time.strftime('%Y-%m-%d')
    hafta = time.time() - 7 * 86400

    def say(label: str, secim: Callable[[dict], bool]) -> tuple[int, list]:
        selected = [k for k in ust if secim(k)]
        terminal = [k for k in selected if k.get('state') in TERMINAL]
        passed = sum(k.get('state') == 'completed' and str(sozluk(k.get('final_result')).get('status') or '').upper() == 'PASS' for k in terminal)
        failed = sum(k.get('state') == 'failed' for k in terminal)
        ids = {rid for k in selected for rid in ekonomi_kapsami(home, k, kayit_oku(kosu_dizini(home, k['run_id']) / 'proje.json'))}
        totals = ekonomi_topla(ekonomi_oku(home, sorted(ids)))
        return len(terminal), [('bolum', t(label)), ('alan', (t('Biten'), str(len(terminal)))), ('alan', (t('Geçen'), str(passed))),
            ('alan', (t('Düşen'), str(failed))), ('alan', (t('Açık'), str(len(selected) - len(terminal)))),
            ('alan', (t('Başarı'), f'{passed}/{len(terminal)} · {oran(100 * passed / len(terminal))}' if terminal else t('bilinmiyor'))),
            ('alan', (t('Harcama'), para_bilinen(totals['spent']))), ('alan', (t('Askıda'), para_bilinen(totals['held'])))]
    count, today = say('Bugün', lambda k: zaman_yazi('%Y-%m-%d', k.get('updated')) == bugun)
    _, week = say('Son 7 gün', lambda k: sayi(k.get('updated')) >= hafta)
    return d(t('Özet'), t('bugün {n} görev', n=count), [
        ('baslik', t('Özet')), ('satir', t('Örneklem: bu dönemde güncellenmiş ürün işleri. Başarı: son kabulü PASS olan / terminal ana koşular.')),
        ('soluk', t('Testler ve çocuklar dışarıda. Aynı koşudaki tekrarlar tek iş. Yeni kabul ayrı iştir.')),
        ('soluk', t('Harcama: bu işlerin çocuklar ve bütün denemeler dahil birikimli tutarı. Açık işler ve askıdaki tutar dahildir.'))] + today + week,
        id='ozet', git=['gorevler', 'gecmis'], git_yazi=t('Geçmiş açılır'))


def okunamayan_gorev(home: Path, k: dict, exc: BaseException) -> dict:
    rid = str(k.get('run_id'))
    proje = k.get('project')
    return d(f'{proje_adi(k)} · {rid}', t('okunamadı'), [
        ('baslik', t('Bu görevin kaydı okunamadı')), ('alan', (t('Hata'), hata_yazi(exc))), ('alan', (t('Kayıt'), f'runs/{rid}/'))],
        durum='hata', id=rid, kosu=rid, proje=proje if isinstance(proje, str) else None, aktif=k.get('state') not in TERMINAL,
        created=sayi(k.get('created')),
        aciklama=aciklama(t('Bu görev'), t('Kayıt beklenmeyen biçimde. Diğer görevler etkilenmez. o ile ham kayıt okunur.')),
        dosya={'tur': 'json', 'yol': str(kosu_dizini(home, rid) / 'run.json'), 'baslik': t('Koşu kaydı')})


def guvenli(uret: Callable[[Path, dict], dict], home: Path, k: dict) -> dict:
    try:
        return uret(home, k)
    except Exception as exc:
        return okunamayan_gorev(home, k, exc)


def panel_dugumu(home: Path, aktif: list[dict], ozet: dict, dikkat: dict, gorunum: tuple[dict, float], olay: list) -> dict:
    nakit = nakit_durumu(home)
    known = nakit['biliniyor'] and nakit['sinir'] is not None
    cash = t('Aylık kalan {k} / {s}', k=para(max(0, nakit['sinir'] - nakit['harcanan'] - nakit['askida'])),
             s=para(nakit['sinir'])) if known else t('Kalan bilinmiyor')
    held = t('Askıda: {a}', a=para(nakit['askida'])) if nakit['biliniyor'] else t('Askıda bilinmiyor')
    attention = dikkat['cocuk']
    attention.sort(key=lambda n: (0 if n['id'].startswith(('alarm-', 'dikkat-kayit')) else 1, n['id']))
    current = [f"{'○' if a.get('beklemede') else '◉'} {a['ozet']} · {a['ad']}" for a in aktif[:1]] or [t('çalışan görev yok')]
    if not aktif and any(n['id'] == 'dikkat-kayit' for n in attention):
        current = [t('Aktif işler bilinmiyor')]
    if len(aktif) > 1:
        current.append(t('… {n} görev daha · Görevler', n=len(aktif) - 1))
    warnings = [f"{n['ozet']} · {n['ad']}" for n in attention[:2]] or [t('bakman gereken bir şey yok')]
    if len(attention) > 2:
        warnings.append(t('… {n} uyarı daha · Dikkat', n=len(attention) - 2))
    girdi = sum(bool(a.get('girdi_bekliyor')) for a in aktif)
    waiting = sum(bool(a.get('beklemede')) and not a.get('girdi_bekliyor') for a in aktif)
    summary = (t('{n} görev sürüyor', n=len(aktif) - waiting - girdi) + (f" · {t('{n} görev bekliyor', n=waiting)}" if waiting else '')
               + (f" · {t('{n} girdi plan bekliyor', n=girdi)}" if girdi else ''))
    su_an = d(t('Şu an'), summary if aktif else t('çalışan görev yok'),
              [('baslik', t('Şu an'))] + [('satir', f"{a['ad']} · {a['ozet']}") for a in aktif],
              durum='simdi' if len(aktif) > waiting + girdi else 'bekliyor' if waiting or girdi else '', id='su-an', git=['gorevler'], git_yazi=t('Görevler açılır'))
    budget = d(t('Bütçe'), cash, [('baslik', t('Bütçe')), ('satir', cash), ('satir', held), ('soluk', olcum_yazi(gorunum))],
               id='panel-butce', git=['butce'], git_yazi=t('Bütçe açılır'))
    blocks = [('baslik', t('Panel')), ('bolum-kisa', t('Şu an'))] + [('satir-kisa', line) for line in current]
    blocks += [('bolum-kisa', t('Dikkat'))] + [('satir-kisa', line) for line in warnings]
    blocks += [('bolum-kisa', t('Bütçe')), ('satir-kisa', cash), ('satir-kisa', held), ('satir-kisa', olcum_yazi(gorunum))]
    return d(t('Panel'), t('genel durum'), blocks, [su_an, ozet, dikkat, budget], baglam=olay, id='panel', kompakt=True,
             aciklama=aciklama(t('Panel'), t('Şu an, Dikkat ve Bütçe en çok on iki satır. Ayrıntı için Enter ile ilgili bölüme gir.')))


def agac(home: Path | None = None, ajanlar: bool = True) -> dict:
    home = home or ev()
    tum_kayitlar = kosular(home)
    test_kimlikleri = {str(k['run_id']) for k in tum_kayitlar if test_projesi(k)}
    urun_kayitlari = [k for k in tum_kayitlar if str(k['run_id']) not in test_kimlikleri and str(k.get('proje_ust')) not in test_kimlikleri]
    testler = dil.testleri_oku(home)
    kayitlar = tum_kayitlar if testler else urun_kayitlari
    gizli = sum(not k.get('proje_ust') and test_projesi(k) for k in tum_kayitlar) if not testler else 0
    ust = [k for k in kayitlar if not k.get('proje_ust')]
    aktif = [guvenli(gorev_dugumu, home, k) for k in reversed(ust) if k.get('state') not in TERMINAL]
    bitenler = [k for k in reversed(ust) if k.get('state') in TERMINAL]
    with ThreadPoolExecutor(max_workers=8) as havuz:
        biten = list(havuz.map(lambda k: guvenli(gecmis_dugumu, home, k), bitenler[:25]))
        eski_teslim = [b for b in havuz.map(lambda k: guvenli(gecmis_dugumu, home, k), [k for k in bitenler[25:] if teslim_adayi(k)]) if b.get('teslim')]
    gorunum = butce_gorunumu(home)
    teslim_n = sum(1 for b in biten + eski_teslim if b.get('teslim'))
    gecmis = d(t('Geçmiş'), t('{n} görev', n=len(biten)), [
        ('baslik', t('Geçmiş')), ('alan', (t('Görev'), str(len(biten)))), ('alan', (t('Geçen'), str(sum(1 for b in biten if b['durum'] == 'bitti')))),
        ('alan', (t('Teslim bekleyen'), str(teslim_n)))], biten, id='gecmis',
        aciklama=aciklama(t('Geçmiş'), t('Son 25 biten görev. Teslim bekleyenlerde p ve m çalışır.')))
    olay = [('baslik', t('Son olaylar')), ('kayit', son_olaylar(home, kayitlar))]
    calisanlar = [('satir', f"◉ {a['ad']}   {a['ozet']}") for a in aktif] or [('soluk', t('çalışan görev yok'))]
    girdi = sum(bool(a.get('girdi_bekliyor')) for a in aktif)
    waiting = sum(bool(a.get('beklemede')) and not a.get('girdi_bekliyor') for a in aktif)
    gorevler = d(t('Görevler'), t('{n} çalışıyor', n=len(aktif) - waiting - girdi) + (f" · {t('{n} görev bekliyor', n=waiting)}" if waiting else '')
                 + (f" · {t('{n} girdi plan bekliyor', n=girdi)}" if girdi else ''), [
        ('baslik', t('Görevler')), ('alan', (t('Çalışan'), str(len(aktif) - waiting - girdi))), ('alan', (t('Bekliyor'), str(waiting))),
        ('alan', (t('Girdi bekleyen'), str(girdi))), ('alan', (t('Teslim bekleyen'), str(teslim_n))),
        ('alan', (t('Geçmiş'), t('{n} görev', n=len(biten)))), ('soluk', t('{n} test görevi gizli', n=gizli)), ('bolum', t('Çalışan görevler'))] + calisanlar,
        aktif + [gecmis], baglam=olay, id='gorevler', gizli_test=gizli,
        aciklama=aciklama(t('Görevler'), t('Verdiğin görevler. Çalışanlar üstte, bitenler Geçmiş içinde.'),
                          [(t('Çalışan görev'), t('plan, test, hakem, ajanlar, harcama ve olaylar')),
                           (t('Geçmiş'), t('son 25 biten görev ve teslim bekleyenler'))]))
    dikkat = dikkat_dugumu(home, ust, biten + eski_teslim, gorunum)
    broken = [p for p in (home / 'runs').glob('*/run.json') if not kayit_oku(p).get('run_id')]
    if broken:
        dikkat['cocuk'].append(d(t('Koşu kayıtları okunamadı'), t('{n} kayıt', n=len(broken)),
            [('baslik', t('Koşu kayıtları okunamadı'))] + [('satir', str(p)) for p in broken], durum='hata', id='dikkat-kayit',
            dosya={'tur': 'json', 'yol': str(broken[0]), 'baslik': t('Koşu kaydı')}))
        dikkat['ozet'] = t('{n} konu', n=len(dikkat['cocuk']))
        dikkat['durum'] = 'hata'
        dikkat['ayrinti'].append(('satir', t('Koşu kayıtları okunamadı')))
    ozet = ozet_dugumu(home, [k for k in urun_kayitlari if not k.get('proje_ust')])
    nakit_surumu = (mtime(home / 'zincir/aylik-butce.db'), mtime(home / 'zincir/aylik-butce.db-wal'))
    butce = _onbellekli(f"Bütçe:{home}:{dil.DIL}:{gorunum[0].get('ts')}:{nakit_siniri(home)}:{nakit_surumu}", 60,
                      lambda: butce_bolumu(home, tum_kayitlar, gorunum), 'butce')
    panel = panel_dugumu(home, aktif, ozet, dikkat, gorunum, olay)
    if ajanlar:
        surum = gorunum[0].get('ts') if olcum_taze(gorunum) else 'eski'
        ajan = _onbellekli(f'Ajanlar:{dil.DIL}:{surum}', 60, lambda: ajan_bolumu(home, gorunum), 'ajanlar')
    else:
        onceki = next((v for a, (_, v) in ONBELLEK.items() if a.startswith(f'Ajanlar:{dil.DIL}:')), None)
        ajan = onceki or d(t('Ajanlar'), '', [('baslik', t('Ajanlar'))], id='ajanlar')
    ayarlar = ayar_bolumu(home)
    return d('cadre', '', [], [panel, gorevler, butce, ajan, ayarlar], id='kok')


def projeler(home: Path | None = None) -> list[str]:
    home = home or ev()
    gecici = ('/private/tmp/', '/scratch/canli/')
    adaylar = []
    for p in (home / 'projects').glob('*/repo-path'):
        try:
            yol = p.read_text().strip()
        except OSError:
            continue
        if ('--' not in p.parent.name and not p.parent.name.startswith(('canli-', 'codex-')) and not any(g in yol for g in gecici)
                and Path(yol).is_dir()):
            adaylar.append(p.parent.name)
    son: dict[str, float] = {}
    for k in kosular(home):
        son[str(k.get('project'))] = max(son.get(str(k.get('project')), 0.0), sayi(k.get('created')))
    return sorted(adaylar, key=lambda p: -son.get(p, 0.0))


def plan_metni(yol: Path) -> str:
    plan = _oku(yol)
    if not plan:
        return t('Dosya okunamadı: {y}', y=yol)
    satirlar = [t('Plan · {i}', i=plan.get('is_id') or yol.parent.name), '']
    if plan.get('kapsam_puani') is not None:
        satirlar.append(t('Kapsam puanı: {p}', p=plan['kapsam_puani']))
    if plan.get('kapsam_gerekce'):
        satirlar.append(t('Gerekçe: {g}', g=plan['kapsam_gerekce']))
    if plan.get('birlestirme_sirasi'):
        satirlar.append(t('Birleştirme sırası: {s}', s=liste_yazi(plan['birlestirme_sirasi'])))
    for g in plan.get('gorevler') or []:
        if not isinstance(g, dict):
            continue
        satirlar += ['', f"{g.get('id')} · {g.get('tanim')}",
                     '  ' + t('Dosyalar: {v}', v=liste_yazi(g.get('dosyalar'))), '  ' + t('Kabul: {v}', v=g.get('kabul') or '—'),
                     '  ' + t('Kapsam dışı: {v}', v=liste_yazi(g.get('kapsam_disi'))), '  ' + t('Bağımlı: {v}', v=liste_yazi(g.get('bagimli'))),
                     '  ' + t('Zorluk: {z} · Model: {m} · Paralel grup: {p}', z=g.get('zorluk', '—'), m=g.get('model') or '—',
                              p=g.get('paralel_grup', '—')),
                     '  ' + t('Tahmin: {dk} dk · {usd}', dk=g.get('tahmin_dk', '?'), usd=para(g.get('tahmin_usd')))]
    satirlar += ['', t('Ham dosya'), json.dumps(plan, ensure_ascii=False, indent=2)]
    return '\n'.join(satirlar)


def izleme_verisi(home: Path, rid: str) -> dict:
    yol = kosu_dizini(home, rid) / 'run.json'
    k = kayit_oku(yol)
    if k.get('run_id') != rid:
        raise ValueError(t('Koşu kaydı okunamadı'))
    node = gorev_dugumu(home, k)
    node['kaynak_zamani'] = mtime(yol) or None
    kaynaklar = log_kaynaklari(home, rid)
    son = max(kaynaklar, key=lambda s: mtime(Path(s['yol'])), default=None)
    node['son_eylem_kaynagi'] = son['baslik'] if son else '—'
    node['son_eylem_zamani'] = (mtime(Path(son['yol'])) or None) if son else None
    if son and son['tur'] == 'jsonl':
        node['son_eylemler'] = [f'{z} · {m}' for z, _, m in kayit_olaylari(Path(son['yol']))[-3:]]
    else:
        node['son_eylemler'] = son_satirlar(son['yol'], 3) if son else []
    return node


def dosya_oku(ozellik: dict, ham: bool = False) -> tuple[str, str, str]:
    if ozellik.get('tur') == 'plan-history':
        from kosu import RunStore
        try:
            view = RunStore(Path(ozellik['home'])).plan_view(ozellik['run_id'])
            return t('Plan geçmişi'), 'json', json.dumps(veri_redakte(view), ensure_ascii=False, indent=2)
        except (OSError, ValueError, TypeError, KeyError):
            return t('Plan geçmişi'), 'metin', t('Plan geçmişi okunamadı. İşlem bekler.')
    tur, baslik = str(ozellik.get('tur') or 'metin'), str(ozellik.get('baslik') or '')
    if tur == 'iz':
        trace = olay_izi(Path(ozellik['home']), ozellik['run_id'])
        return baslik, 'json' if ham else 'metin', json.dumps(trace, ensure_ascii=False, indent=2) if ham else iz_metni(trace)
    if tur == 'beyin' and not ham:
        return baslik, 'metin', beyin_metni(Path(ozellik['yol']))
    if tur == 'diff':
        try:
            r = subprocess.run(ozellik['komut'], capture_output=True, text=True, timeout=20)
            metin = r.stdout if r.returncode == 0 else r.stderr
        except (OSError, subprocess.SubprocessError) as exc:
            metin = str(exc)
        return baslik, 'diff', metin_redakte(metin.strip()) or t('(fark yok)')
    if tur == 'plan':
        return baslik, 'metin', metin_redakte(plan_metni(Path(ozellik['yol'])))
    yol = Path(str(ozellik.get('yol') or ''))
    if tur == 'beyin':
        yol = yol / 'stdout.jsonl'
        tur = 'jsonl'
    try:
        if yol.is_symlink() or yol.parent.is_symlink():
            return baslik, 'metin', t('Bağlantılı kayıt okunmadı')
        metin = yol.read_text(errors='replace')
    except OSError:
        return baslik, 'metin', t('Dosya okunamadı: {y}', y=yol)
    if tur == 'json':
        try:
            metin = json.dumps(veri_redakte(json.loads(metin)), ensure_ascii=False, indent=2)
            return baslik, tur, metin
        except ValueError:
            return baslik, 'metin', t('Bozuk JSON') + '\n' + metin_redakte(metin)
    elif tur == 'jsonl':
        if not ham:
            adlar = {'dusunce': t('düşünce'), 'arac': t('araç'), 'sonuc': t('Araç sonucu'), 'ham': t('Ham olay'), 'hata': t('Hata')}
            return baslik, 'metin', '\n'.join(f'{z}  {adlar[tip]}  {m}' for z, tip, m in kayit_olaylari(yol, tam=True)) or t('(kayıt yok)')
        satirlar = []
        for no, satir in enumerate(metin.splitlines(), 1):
            try:
                icerik = json.dumps(veri_redakte(json.loads(satir)), ensure_ascii=False, indent=2)
            except ValueError:
                icerik = metin_redakte(satir)
            satirlar.append(f'{yol.name}:{no}\n{icerik}')
        return baslik, 'metin', '\n\n'.join(satirlar)
    return baslik, tur, metin_redakte(metin)


def iz_replay(raw: str) -> dict:
    if len(raw) > 32_000_000:
        raise ValueError('trace replay size limit')
    trace = json.loads(raw)
    if not isinstance(trace, dict) or trace.get('schema') != 'cadre-trace-v1' or not isinstance(trace.get('run_id'), str):
        raise ValueError('invalid trace schema')
    from kosu import identifier
    identifier(trace['run_id'])
    for key in ('sources', 'issues', 'events'):
        if not isinstance(trace.get(key), list) or len(trace[key]) > 10000 or any(not isinstance(v, dict) for v in trace[key]):
            raise ValueError('invalid trace entries')
    for source in trace['sources']:
        if not isinstance(source.get('path'), str) or type(source.get('mtime')) not in (int, float) or not -62_135_596_800 <= source['mtime'] < 253_402_300_800:
            raise ValueError('invalid trace source')
        if 'stable' in source and type(source['stable']) is not bool:
            raise ValueError('invalid trace freshness')
    for issue in trace['issues']:
        if not isinstance(issue.get('source'), str) or not isinstance(issue.get('issue'), str):
            raise ValueError('invalid trace issue')
    for event in trace['events']:
        if any(not isinstance(event.get(k), str) for k in ('id', 'run_id', 'source', 'type')) or 'data' not in event:
            raise ValueError('invalid trace event')
        if event.get('timestamp') is not None and (type(event['timestamp']) not in (int, float) or not -62_135_596_800 <= event['timestamp'] < 253_402_300_800):
            raise ValueError('invalid trace time')
        if not isinstance(event.get('lines'), list) or not event['lines'] or any(type(n) is not int or n < 1 for n in event['lines']):
            raise ValueError('invalid trace lines')
        if 'locations' in event and (not isinstance(event['locations'], list) or any(not isinstance(p, str) for p in event['locations'])):
            raise ValueError('invalid trace locations')
        if 'evidence_status' in event and event['evidence_status'] not in ('recorded', 'conflict', 'unknown'):
            raise ValueError('invalid trace evidence')
    json.dumps(trace, allow_nan=False)
    return veri_redakte(trace)


def iz_main() -> int:
    import argparse
    import sys
    parser = argparse.ArgumentParser(description='Redacted, read-only Cadre event trace')
    parser.add_argument('run_id', nargs='?')
    parser.add_argument('--format', choices=('json', 'markdown'), default='json')
    parser.add_argument('--replay', action='store_true')
    args = parser.parse_args()
    if bool(args.run_id) == args.replay:
        parser.error('provide a run ID or --replay')
    try:
        trace = iz_replay(sys.stdin.read(32_000_001)) if args.replay else olay_izi(ev(), args.run_id)
        if args.format == 'json':
            print(json.dumps(trace, ensure_ascii=False, indent=2, allow_nan=False))
        else:
            body = iz_metni(trace)
            fence = '`' * max(3, max((len(m) + 1 for m in re.findall(r'`+', body)), default=3))
            print('# Cadre trace\n\n' + fence + 'text\n' + body + '\n' + fence)
    except (ValueError, OSError, TypeError, KeyError, RecursionError):
        print('Trace input or source is invalid.', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(iz_main())
