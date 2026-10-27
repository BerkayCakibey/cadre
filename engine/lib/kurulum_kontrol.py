"""Read-only installation check: installs, downloads and writes nothing (N-PUB01, CR-28). A missing dependency is a real error,
never reported as installed. Personal fixed paths and version prerequisites are reported explicitly."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

ASGARI = {'python': (3, 12), 'node': (22, 0), 'git': (2, 30)}
MOTOR_OPENCODE = '/opt/homebrew/bin/opencode'
OPENCODE_ARALIK = ((1, 18), (2, 0))


def _surum(command: list[str]) -> str | None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return (result.stdout or result.stderr).strip().splitlines()[0][:80] if result.returncode == 0 and (result.stdout or result.stderr).strip() else None


def _sayilar(metin: str | None) -> tuple[int, ...]:
    found = re.search(r'(\d+)\.(\d+)', metin or '')
    return (int(found.group(1)), int(found.group(2))) if found else ()


def kontrol(home: Path | None = None, config: Path | None = None, engine: Path | None = None,
            environment: dict[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environment is None else environment
    home = Path(home or env.get('CADRE_HOME', str(Path.home() / '.local/share/cadre-engineering')))
    config = Path(config or env.get('OPENCODE_CFG', str(Path.home() / '.config/opencode')))
    engine = Path(engine or env.get('CADRE_ENGINE_ROOT', str(Path(__file__).resolve().parents[1])))
    path = env.get('PATH', '')
    items: list[dict[str, Any]] = []

    def madde(ad: str, durum: str, ayrinti: str, gerekli: bool = True) -> None:
        items.append({'ad': ad, 'durum': durum, 'ayrinti': ayrinti, 'gerekli': gerekli})

    for name, command in (('git', ['git', '--version']), ('bash', ['bash', '--version']), ('python', [sys.executable, '--version']),
                          ('node', ['node', '--version'])):
        binary = shutil.which(command[0], path=path) if command[0] != sys.executable else sys.executable
        version = _surum([binary, *command[1:]]) if binary else None
        if not binary or version is None:
            madde(name, 'EKSIK', f'{command[0]} PATH icinde yok ya da surum okunamadi')
            continue
        low = _sayilar(version)
        need = ASGARI.get(name)
        if need and low and low < need:
            madde(name, 'EKSIK', f'{version} < asgari {need[0]}.{need[1]}')
        else:
            madde(name, 'VAR', version)
    opencode = env.get('CADRE_REAL_OPENCODE') or MOTOR_OPENCODE
    version = _surum([opencode, '--version']) if Path(opencode).is_file() else None
    major = _sayilar(version)
    compatible = bool(major and OPENCODE_ARALIK[0] <= major < OPENCODE_ARALIK[1])
    detail = f'{opencode}: {version or "bulunamadi"}'
    if not version:
        elsewhere = shutil.which('opencode', path=path)
        detail += (f'; PATH icindeki {elsewhere} motor tarafindan kullanilmaz, CADRE_REAL_OPENCODE={elsewhere} ver'
                   if elsewhere and elsewhere != opencode else '; motor CADRE_REAL_OPENCODE ya da ' + MOTOR_OPENCODE + ' calistirir')
    elif not compatible:
        detail += '; Cadre eklentileri V1 API ister ve 1.18.x ile dogrulandi; bu surum desteklenmiyor (CR-10/47)'
    elif major[:2] != OPENCODE_ARALIK[0]:
        detail += '; V1 API, ancak yalniz 1.18.x ile dogrulandi'
    madde('opencode', 'VAR' if compatible else 'EKSIK', detail)
    sandbox = sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file()
    madde('sandbox-exec (macOS)', 'VAR' if sandbox else 'EKSIK',
          '/usr/bin/sandbox-exec' if sandbox else 'gorev sandbox profili yalniz macOS sandbox-exec ile kurulur; motor bu ortamda kod yazdirmaz')
    ui = Path(env.get('CADRE_UI_PYTHON') or str(Path.home() / '.local/share/cadre-ui-venv/bin/python'))
    if ui.is_file():
        textual = _surum([str(ui), '-c', 'import textual, rich; print(textual.__version__)'])
        madde('panel (Textual)', 'VAR' if textual else 'EKSIK', f'{ui}: {("textual " + textual) if textual else "textual/rich yok"}', gerekli=False)
    else:
        madde('panel (Textual)', 'EKSIK', f'{ui} yok; eski menu kullanilir (istege bagli)', gerekli=False)
    madde('caffeinate (macOS)', 'VAR' if shutil.which('caffeinate', path=path) else 'EKSIK',
          'uyku engeli; baska isletim sistemi dogrulanmis degil', gerekli=sys.platform == 'darwin')
    for label, folder in (('CADRE_HOME', home), ('OpenCode config', config), ('CADRE_ENGINE_ROOT', engine)):
        if folder.is_dir():
            madde(label, 'VAR' if os.access(folder, os.R_OK) else 'EKSIK', str(folder))
        else:
            madde(label, 'EKSIK', f'{folder} yok (olusturulmadi; bu kontrol yazmaz)', gerekli=label != 'OpenCode config')
    personal = []
    for file in (config / 'opencode.jsonc', engine.parent / 'config/opencode.jsonc'):
        try:
            text = file.read_text(errors='replace')
        except OSError:
            continue
        for match in re.finditer(r'(file://)?(/Users/[^/"\s]+|/home/[^/"\s]+)[^"\s]*', text):
            if str(Path.home()) not in match.group(0) or 'cadre-engineering' not in match.group(0):
                personal.append(f'{file.name}: {match.group(0)[:80]}')
            elif not (Path(match.group(0).replace('file://', ''))).exists():
                personal.append(f'{file.name}: {match.group(0)[:80]} (yol yok)')
    madde('kisisel sabit yol', 'EKSIK' if personal else 'VAR', '; '.join(personal) if personal else 'config icinde baska kullaniciya ait ya da olmayan mutlak yol yok', gerekli=False)
    catalog = Path(env.get('CADRE_MODEL_KATALOG') or str(Path.home() / '.cache/opencode/models.json'))
    if catalog.is_file():
        madde('model katalogu', 'VAR', f'{catalog} ({catalog.stat().st_size} bayt); yoksa ucretli yollar fiyat tavani hesaplanamadigi icin elenir')
    else:
        madde('model katalogu', 'EKSIK', f'{catalog} yok; opencode bir kez calisinca yazar (CADRE_MODEL_KATALOG ile yol verilebilir); ucretli yollar kapali kalir', gerekli=False)
    for name in ('OPENROUTER_API_KEY', 'ANTHROPIC_API_KEY', 'OPENCODE_API_KEY'):
        madde(name, 'VAR' if env.get(name) else 'YOK', 'ortamda anahtar var (degeri okunmadi)' if env.get(name) else 'ortamda yok; hesap OpenCode auth ile de verilebilir', gerekli=False)
    required_missing = [i['ad'] for i in items if i['gerekli'] and i['durum'] == 'EKSIK']
    return {'surum': 1, 'sonuc': 'EKSIK' if required_missing else 'HAZIR', 'eksik': required_missing, 'maddeler': items,
            'not': 'salt-okur kontrol; kurulum/indirme yapilmadi, temiz makine kabulu ayri (CR-28)'}


def main(argv: list[str]) -> int:
    result = kontrol()
    if '--json' in argv:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for item in result['maddeler']:
            print(f"{item['durum']:<6} {item['ad']:<22} {item['ayrinti']}")
        print(f"SONUC: {result['sonuc']}" + (f" — eksik: {', '.join(result['eksik'])}" if result['eksik'] else ''))
    return 0 if result['sonuc'] == 'HAZIR' else 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
