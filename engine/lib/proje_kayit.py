"""Project registration records: repository path, base branch, policies."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dil  # noqa: E402
from dil import t  # noqa: E402


class KayitHatasi(Exception):
    pass


class KayitIzni(KayitHatasi):
    def __init__(self, token: str) -> None:
        super().__init__(t('İlk commit ayrı kesin izin bekliyor. Mevcut ürün korunuyor.'))
        self.token = token


def _git(yol: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(['git', '-C', str(yol), *args], capture_output=True, text=True, timeout=30)


def kayitli(home: Path, kok: Path) -> str | None:
    for p in sorted((home / 'projects').glob('*/repo-path')):
        if '--' in p.parent.name:
            continue
        try:
            if Path(p.read_text().strip()).resolve() == kok:
                return p.parent.name
        except OSError:
            continue
    return None


def uygun_ad(home: Path, kok: Path) -> str:
    taban = re.sub(r'[^a-z0-9]+', '-', kok.name.lower()).strip('-') or 'proje'
    ad, sira = taban, 2
    while (home / 'projects' / ad).exists():
        ad, sira = f'{taban}-{sira}', sira + 1
    return ad


def ilk_commit(yol: Path) -> None:
    raise KayitHatasi(t('İlk commit ayrı kesin izin bekliyor. Mevcut ürün korunuyor.'))


def bos_baslatilmis(yol: Path) -> bool:
    """FIX-46: a folder containing only `.git` and no commit (interrupted between init and commit) continues like an empty folder.
    CR-33: `.git` + uncommitted skeleton files (matching the manifest) is the same state."""
    from izin import initialized_empty
    return initialized_empty(yol)


def iskelet_secimi(yol: Path, tur: str | None) -> dict[str, str] | None:
    """CR-33: if `--tur` is given or a half-written skeleton manifest exists, the first commit contains the skeleton."""
    import iskelet
    if tur is not None:
        if tur not in iskelet.DESTEK:
            raise KayitHatasi(t('Desteklenmeyen proje türü: {t}. Desteklenen: {d}', t=tur, d=', '.join(iskelet.DESTEK)))
        return {'tur': tur, 'ad': yol.name}
    bekleyen = iskelet.manifest_oku(yol) if yol.is_dir() else None
    return {'tur': bekleyen['tur'], 'ad': bekleyen['ad']} if bekleyen else None


def hazirla(yol: Path, olustur: bool, home: Path | None = None, tur: str | None = None) -> Path:
    yol = yol.expanduser()
    if tur is not None and not olustur:
        raise KayitHatasi(t('--tur yalnız --olustur ile kullanılır; mevcut depoya iskelet yazılmaz.'))
    if tur is not None:
        iskelet_secimi(yol, tur)
    if olustur:
        if yol.exists() and (not yol.is_dir() or (any(yol.iterdir()) and not bos_baslatilmis(yol))):
            raise KayitHatasi(t('Bu klasör zaten var ve boş değil: {y}', y=yol))
        ata = next((a for a in yol.absolute().parents if a.is_dir()), None)
        ust_depo = _git(ata, 'rev-parse', '--show-toplevel') if ata else None
        if ust_depo is not None and ust_depo.returncode == 0:
            raise KayitHatasi(t('Yeni klasör başka bir git deposunun içinde kalıyor ({d}). Tam yolla başka bir yer seç.', d=ust_depo.stdout.strip()))
        yol.mkdir(parents=True, exist_ok=True)
    if not yol.is_dir():
        raise KayitHatasi(t('Klasör bulunamadı: {y}', y=yol))
    yol = yol.resolve()
    ust = _git(yol, 'rev-parse', '--show-toplevel')
    yarim = ust.returncode == 0 and Path(ust.stdout.strip()).resolve() == yol and bos_baslatilmis(yol)
    if ust.returncode != 0 or yarim:
        if not yarim and any(yol.iterdir()):
            raise KayitHatasi(t('Bu klasör git deposu değil ve boş değil. Önce git init ve ilk commit, sonra tekrar dene.'))
        from izin import initialization_proposal, initialization_recover
        from kosu import RunStore
        store = RunStore(home or dil.ev())
        try:
            if yarim:
                initialization_recover(store, yol)
            action = initialization_proposal(store, yol, iskelet_secimi(yol, tur))
        except (subprocess.SubprocessError, PermissionError):
            raise KayitHatasi(t('Git kullanıcı kimliği gerekli. İlk commit oluşturulmadı.')) from None
        raise KayitIzni(action['token'])
    kok = Path(ust.stdout.strip()).resolve()
    if _git(kok, 'rev-parse', '--verify', '-q', 'HEAD').returncode != 0:
        raise KayitHatasi(t('Depoda hiç commit yok. Önce bir commit at, sonra tekrar dene.'))
    return kok


def iskelet_kaydi(home: Path, ad: str, kok: Path) -> dict | None:
    """N-ENV01: in a skeleton project install/test/run go in one record (`projects/<ad>/iskelet.json`); running again does not duplicate the product."""
    import iskelet
    if iskelet.manifest_oku(kok) is None:
        return None
    sonuc = iskelet.dogrula(kok, home / 'projects' / ad)
    ozet = ' · '.join(f"{a['ad']} rc={a['rc']}" for a in sonuc['adimlar'])
    print(t('İskelet akışı {d}: {o}', d=sonuc['durum'], o=ozet), file=sys.stderr)
    return sonuc


def kaydet(yol: str | Path, olustur: bool = False, home: Path | None = None, ad: str | None = None, tur: str | None = None) -> str:
    home = home or dil.ev()
    kok = hazirla(Path(yol), olustur, home, tur)
    from izin import initialization_recover
    from kosu import RunStore
    initialization_recover(RunStore(home), kok)
    var = kayitli(home, kok)
    if var:
        iskelet_kaydi(home, var, kok)
        return var
    if ad and (home / 'projects' / ad).exists():
        raise KayitHatasi(t('Bu proje adı kullanımda: {a}', a=ad))
    ad = ad or uygun_ad(home, kok)
    dal = _git(kok, 'branch', '--show-current').stdout.strip() or 'main'
    dizin = home / 'projects' / ad
    dizin.mkdir(parents=True)
    (dizin / 'repo-path').write_text(f'{kok}\n')
    (dizin / 'base-branch').write_text(f'{dal}\n')
    (dizin / 'PROJECT.md').write_text(t('# {a}\n\nCadre bu projeyi {z} tarihinde kaydetti. Mimariyi, kuralları ve dikkat edilecekleri buraya yaz.\n',
                                        a=ad, z=time.strftime('%Y-%m-%d')))
    (dizin / 'POLICY.md').write_text(t('# POLICY: {a}\n\n- Korunan dal: `{d}`. Push ve merge yalnız kullanıcı onayıyla.\n'
                                       '- Sır dosyalarına (.env, anahtarlar) dokunulmaz.\n', a=ad, d=dal))
    iskelet_kaydi(home, ad, kok)
    return ad


def main(argv: list[str]) -> int:
    dil.kur()
    tur = None
    if '--tur' in argv:
        konum = argv.index('--tur')
        if konum + 1 >= len(argv) or argv[konum + 1].startswith('--'):
            print(t('--tur bir proje türü ister (python).'), file=sys.stderr)
            return 2
        tur = argv[konum + 1]
        argv = argv[:konum] + argv[konum + 2:]
    yollar = [a for a in argv if not a.startswith('--')]
    try:
        path = yollar[0] if yollar else os.getcwd()
        try:
            registered = kaydet(path, olustur='--olustur' in argv, tur=tur)
        except KayitIzni as requested:
            from izin import initialization_execute, obtain, read_action
            from kosu import RunStore
            store = RunStore(dil.ev())
            action = read_action(store, 'system', requested.token)
            initialization_execute(store, obtain(store, 'system', action))
            registered = kaydet(path)
        print(registered)
    except (KayitHatasi, OSError, subprocess.SubprocessError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
