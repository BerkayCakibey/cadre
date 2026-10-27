"""Deterministic project skeleton for supported project types with locked dependency lists (N-ENV01, CR-33), plus an install/run/test record.
The skeleton is part of the first commit permission; the file list appears in the approval text, and the tree is verified against the manifest."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

MANIFEST = 'cadre-iskelet.json'
SURUM = 1
DESTEK = {
    'python': 'Python 3.12+ standart kutuphane; bagimlilik yok (requirements.lock bos ve kilitli); '
              'make install (.venv, indirme yok) / make run / make test (unittest)',
}
ADIMLAR = ('install', 'test', 'run')
TR = str.maketrans('çğıöşüÇĞİÖŞÜ', 'cgiosuCGIOSU')


def sha256(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def paket_adi(ad: str) -> str:
    temiz = re.sub(r'[^a-z0-9_]+', '_', ad.translate(TR).lower()).strip('_')
    if not temiz or temiz[0].isdigit():
        temiz = 'uygulama' + ('_' + temiz if temiz else '')
    return temiz


def dosyalar(ad: str, tur: str) -> dict[str, str]:
    if tur not in DESTEK:
        raise ValueError('iskelet-tur-desteklenmiyor: ' + tur)
    paket = paket_adi(ad)
    return {
        'README.md': (f'# {ad}\n\nCadre `python` iskeleti ({DESTEK["python"]}).\n\n'
                      '| Adim | Komut | Not |\n| --- | --- | --- |\n'
                      '| Kurulum | `make install` | yerel `.venv`; `requirements.lock` bos oldugu icin indirme yok |\n'
                      f'| Calistirma | `make run` | `python -m {paket}` |\n'
                      '| Test | `make test` | `unittest discover -s tests` |\n\n'
                      'Yeni bagimlilik `requirements.lock` dosyasina hash ile eklenir; `make install` yalniz kilitli listeyi, '
                      'agsiz (`--no-index`) kurar. Desteklenmeyen tur ya da hesap/anahtar gerektiren adim burada yoktur.\n'),
        'pyproject.toml': (f'[project]\nname = "{paket}"\nversion = "0.1.0"\nrequires-python = ">=3.12"\ndependencies = []\n\n'
                           '[tool.cadre]\niskelet = "python"\n'),
        'requirements.lock': '# kilitli bagimlilik listesi (pip --require-hashes --no-index); bos = yalniz standart kutuphane\n',
        'Makefile': ('PYTHON ?= python3\nVENV := .venv\nPY := $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,$(PYTHON))\n\n'
                     '.PHONY: install run test\n\n'
                     'install:\n\t$(PYTHON) -m venv $(VENV)\n'
                     "\t@if grep -qv '^#' requirements.lock; then $(VENV)/bin/python -m pip install --require-hashes --no-index -r requirements.lock; "
                     'else echo "requirements.lock bos: indirme yok"; fi\n\n'
                     f'run:\n\tPYTHONPATH=src$(if $(PYTHONPATH),:$(PYTHONPATH)) $(PY) -m {paket}\n\n'
                     'test:\n\tPYTHONPATH=src$(if $(PYTHONPATH),:$(PYTHONPATH)) $(PY) -m unittest discover -q -s tests\n'),
        '.gitignore': '.venv/\n__pycache__/\n*.pyc\n',
        f'src/{paket}/__init__.py': f'"""{ad}: Cadre python iskeleti."""\n\n__version__ = "0.1.0"\n\n\ndef selam(kime: str = "dunya") -> str:\n    return f"merhaba {{kime}}"\n',
        f'src/{paket}/__main__.py': (f'from {paket} import __version__, selam\n\n\n'
                                     f'def main() -> int:\n    print(f"{paket} {{__version__}}: {{selam()}}")\n    return 0\n\n\n'
                                     'if __name__ == "__main__":\n    raise SystemExit(main())\n'),
        'tests/test_smoke.py': (f'import unittest\n\nfrom {paket} import __version__, selam\n\n\n'
                                'class SmokeTests(unittest.TestCase):\n'
                                '    def test_version_and_greeting(self):\n'
                                '        self.assertEqual(__version__, "0.1.0")\n'
                                '        self.assertEqual(selam("cadre"), "merhaba cadre")\n\n\n'
                                'if __name__ == "__main__":\n    unittest.main()\n'),
    }


def manifest(ad: str, tur: str) -> dict[str, Any]:
    icerik = dosyalar(ad, tur)
    return {'surum': SURUM, 'tur': tur, 'ad': ad, 'paket': paket_adi(ad), 'destek': DESTEK[tur],
            'dosyalar': {rel: sha256(text) for rel, text in sorted(icerik.items())},
            'komutlar': {adim: f'make {adim}' for adim in ADIMLAR}}


def manifest_metni(ad: str, tur: str) -> str:
    return json.dumps(manifest(ad, tur), ensure_ascii=False, indent=2, sort_keys=True) + '\n'


def yaz(path: Path, ad: str, tur: str) -> list[str]:
    """Writes the files deterministically; writes nothing if an existing file has different content."""
    path = Path(path)
    icerik = {**dosyalar(ad, tur), MANIFEST: manifest_metni(ad, tur)}
    for rel, text in icerik.items():
        hedef = path / rel
        if hedef.is_symlink() or (hedef.exists() and (not hedef.is_file() or hedef.read_text() != text)):
            raise FileExistsError('iskelet-dosya-cakisiyor: ' + rel)
    for rel, text in icerik.items():
        hedef = path / rel
        hedef.parent.mkdir(parents=True, exist_ok=True)
        hedef.write_text(text)
    return sorted(icerik)


def _manifest_dogrula(raw: str | bytes) -> dict[str, Any] | None:
    try:
        m = json.loads(raw)
        if (m.get('surum') != SURUM or m.get('tur') not in DESTEK or not isinstance(m.get('ad'), str)
                or manifest_metni(m['ad'], m['tur']) != (raw.decode() if isinstance(raw, bytes) else raw)):
            return None
        return m
    except (ValueError, TypeError, AttributeError, UnicodeDecodeError):
        return None


def manifest_oku(path: Path) -> dict[str, Any] | None:
    hedef = Path(path) / MANIFEST
    if hedef.is_symlink() or not hedef.is_file():
        return None
    try:
        return _manifest_dogrula(hedef.read_bytes())
    except OSError:
        return None


def calisma_agaci_uyuyor(path: Path) -> dict[str, Any] | None:
    """Returns the manifest if the working tree outside `.git` consists exactly of the manifest's files and their contents match."""
    path = Path(path)
    m = manifest_oku(path)
    if not m:
        return None
    gorulen: set[str] = set()
    for kok, dizinler, adlar in os.walk(path):
        dizinler[:] = [d for d in dizinler if d != '.git']
        for ad in adlar:
            gorulen.add((Path(kok) / ad).relative_to(path).as_posix())
    if gorulen != set(m['dosyalar']) | {MANIFEST}:
        return None
    for rel, digest in m['dosyalar'].items():
        hedef = path / rel
        if hedef.is_symlink() or not hedef.is_file() or sha256(hedef.read_bytes()) != digest:
            return None
    return m


def commit_agaci_uyuyor(path: Path, head: str) -> dict[str, Any] | None:
    """Returns the manifest if the commit tree consists exactly of manifest + files and the blobs match."""
    def git(*args: str) -> bytes:
        return subprocess.run(['git', '-c', 'core.fsmonitor=false', '-C', str(path), *args], capture_output=True, check=True, timeout=30).stdout
    try:
        adlar = set(git('ls-tree', '-r', '--name-only', head).decode().splitlines())
        if MANIFEST not in adlar:
            return None
        m = _manifest_dogrula(git('show', f'{head}:{MANIFEST}'))
        if not m or adlar != set(m['dosyalar']) | {MANIFEST}:
            return None
        for rel, digest in m['dosyalar'].items():
            if sha256(git('show', f'{head}:{rel}')) != digest:
                return None
        return m
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return None


def dogrula(path: Path, kayit_dizini: Path, zaman: float = 300) -> dict[str, Any]:
    """Install/test/run in one record: each step's rc/duration is written to `iskelet.json`; running again does not duplicate the product."""
    from kosu import atomic_json
    path, kayit_dizini = Path(path), Path(kayit_dizini)
    m = manifest_oku(path)
    sonuc: dict[str, Any] = {'surum': SURUM, 'ts': time.time(), 'tur': m['tur'] if m else None, 'ad': m['ad'] if m else None,
                             'python': sys.version.split()[0], 'adimlar': [], 'durum': 'FAIL'}
    if not m:
        sonuc['neden'] = 'iskelet-manifesti-yok'
        atomic_json(kayit_dizini / 'iskelet.json', sonuc)
        return sonuc
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': os.environ.get('HOME', str(path)), 'PYTHON': sys.executable,
           'LANG': os.environ.get('LANG', 'C.UTF-8'), 'PYTHONDONTWRITEBYTECODE': '1'}
    for adim in ADIMLAR:
        bas = time.time()
        try:
            r = subprocess.run(['make', '-s', adim], cwd=path, capture_output=True, text=True, timeout=zaman, env=env)
            rc, cikti = r.returncode, (r.stdout + r.stderr)[-2000:]
        except (OSError, subprocess.SubprocessError) as exc:
            rc, cikti = 124 if isinstance(exc, subprocess.TimeoutExpired) else 127, str(exc)[-2000:]
        sonuc['adimlar'].append({'ad': adim, 'komut': m['komutlar'][adim], 'rc': rc, 'sn': round(time.time() - bas, 2), 'cikti': cikti})
        if rc != 0:
            sonuc['neden'] = f'{adim}-basarisiz'
            break
    else:
        sonuc['durum'] = 'PASS'
    atomic_json(kayit_dizini / 'iskelet.json', sonuc)
    return sonuc


def main(argv: list[str]) -> int:
    komut = argv[0] if argv else ''
    if komut == 'turler':
        for tur, aciklama in DESTEK.items():
            print(f'{tur}: {aciklama}')
        return 0
    if komut == 'yaz' and len(argv) == 4:
        try:
            for rel in yaz(Path(argv[1]), argv[2], argv[3]):
                print(rel)
        except (OSError, ValueError, FileExistsError) as exc:
            print(f'iskelet yazilamadi: {exc}', file=sys.stderr)
            return 1
        return 0
    if komut == 'dogrula' and len(argv) == 3:
        sonuc = dogrula(Path(argv[1]), Path(argv[2]))
        for adim in sonuc['adimlar']:
            print(f"{adim['ad']}: rc={adim['rc']} {adim['sn']} sn")
        print(f"iskelet: {sonuc['durum']}" + (f" ({sonuc.get('neden')})" if sonuc.get('neden') else ''))
        return 0 if sonuc['durum'] == 'PASS' else 1
    print('kullanim: iskelet.py turler | yaz <klasor> <ad> <tur> | dogrula <klasor> <kayit-dizini>', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main(sys.argv[1:]))
