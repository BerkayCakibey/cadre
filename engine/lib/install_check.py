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

MINIMUM = {'python': (3, 12), 'node': (22, 0), 'git': (2, 30)}
RUNNER_OPENCODE = '/opt/homebrew/bin/opencode'
OPENCODE_INTERVAL = ((1, 18), (2, 0))


def _version(command: list[str]) -> str | None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return (result.stdout or result.stderr).strip().splitlines()[0][:80] if result.returncode == 0 and (result.stdout or result.stderr).strip() else None


def _numbers(text: str | None) -> tuple[int, ...]:
    found = re.search(r'(\d+)\.(\d+)', text or '')
    return (int(found.group(1)), int(found.group(2))) if found else ()


def check(home: Path | None = None, config: Path | None = None, engine: Path | None = None,
            environment: dict[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environment is None else environment
    home = Path(home or env.get('CADRE_HOME', str(Path.home() / '.local/share/cadre-engineering')))
    config = Path(config or env.get('OPENCODE_CFG', str(Path.home() / '.config/opencode')))
    engine = Path(engine or env.get('CADRE_ENGINE_ROOT', str(Path(__file__).resolve().parents[1])))
    path = env.get('PATH', '')
    items: list[dict[str, Any]] = []

    def item(entry_name: str, status: str, detail: str, required: bool = True) -> None:
        items.append({'entry_name': entry_name, 'status': status, 'detail': detail, 'required': required})

    for name, command in (('git', ['git', '--version']), ('bash', ['bash', '--version']), ('python', [sys.executable, '--version']),
                          ('node', ['node', '--version'])):
        binary = shutil.which(command[0], path=path) if command[0] != sys.executable else sys.executable
        version = _version([binary, *command[1:]]) if binary else None
        if not binary or version is None:
            item(name, 'MISSING', f'{command[0]} is absent from PATH or its version is unreadable')
            continue
        low = _numbers(version)
        need = MINIMUM.get(name)
        if need and low and low < need:
            item(name, 'MISSING', f'{version} < minimum {need[0]}.{need[1]}')
        else:
            item(name, 'PRESENT', version)
    opencode = env.get('CADRE_REAL_OPENCODE') or RUNNER_OPENCODE
    version = _version([opencode, '--version']) if Path(opencode).is_file() else None
    major = _numbers(version)
    compatible = bool(major and OPENCODE_INTERVAL[0] <= major < OPENCODE_INTERVAL[1])
    detail = f'{opencode}: {version or "not found"}'
    if not version:
        elsewhere = shutil.which('opencode', path=path)
        detail += (f'; the engine does not use {elsewhere} from PATH; set CADRE_REAL_OPENCODE={elsewhere}'
                   if elsewhere and elsewhere != opencode else '; the engine runs CADRE_REAL_OPENCODE or ' + RUNNER_OPENCODE + '')
    elif not compatible:
        detail += '; Cadre plugins require the V1 API and are verified with 1.18.x; this version is unsupported (CR-10/47)'
    elif major[:2] != OPENCODE_INTERVAL[0]:
        detail += '; V1 API; only 1.18.x has been verified'
    item('opencode', 'PRESENT' if compatible else 'MISSING', detail)
    sandbox = sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file()
    item('sandbox-exec (macOS)', 'PRESENT' if sandbox else 'MISSING',
          '/usr/bin/sandbox-exec' if sandbox else 'task sandbox profiles require macOS sandbox-exec; the engine cannot run code writers in this environment')
    ui = Path(env.get('CADRE_UI_PYTHON') or str(Path.home() / '.local/share/cadre-ui-venv/bin/python'))
    if ui.is_file():
        textual = _version([str(ui), '-c', 'import textual, rich; print(textual.__version__)'])
        item('panel (Textual)', 'PRESENT' if textual else 'MISSING', f'{ui}: {("textual " + textual) if textual else "textual/rich absent"}', required=False)
    else:
        item('panel (Textual)', 'MISSING', f'{ui} absent; using the legacy menu (optional)', required=False)
    item('caffeinate (macOS)', 'PRESENT' if shutil.which('caffeinate', path=path) else 'MISSING',
          'prevents sleep; other operating systems have not been verified', required=sys.platform == 'darwin')
    for label, folder in (('CADRE_HOME', home), ('OpenCode config', config), ('CADRE_ENGINE_ROOT', engine)):
        if folder.is_dir():
            item(label, 'PRESENT' if os.access(folder, os.R_OK) else 'MISSING', str(folder))
        else:
            item(label, 'MISSING', f'{folder} absent (not created; this check does not write)', required=label != 'OpenCode config')
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
                personal.append(f'{file.name}: {match.group(0)[:80]} (path absent)')
    item('personal fixed path', 'MISSING' if personal else 'PRESENT', '; '.join(personal) if personal else 'config contains no absolute path belonging to another user or missing from disk', required=False)
    catalog = Path(env.get('CADRE_MODEL_CATALOG') or str(Path.home() / '.cache/opencode/models.json'))
    if catalog.is_file():
        item('model catalog', 'PRESENT', f'{catalog} ({catalog.stat().st_size} bytes); without it paid routes are excluded because their price caps cannot be calculated')
    else:
        item('model catalog', 'MISSING', f'{catalog} absent; opencode writes it on its first run (set CADRE_MODEL_CATALOG to override the path); paid routes remain closed', required=False)
    for name in ('OPENROUTER_API_KEY', 'ANTHROPIC_API_KEY', 'OPENCODE_API_KEY'):
        item(name, 'PRESENT' if env.get(name) else 'ABSENT', 'key present in the environment (value not read)' if env.get(name) else 'absent from the environment; the account may also use OpenCode auth', required=False)
    required_missing = [i['entry_name'] for i in items if i['required'] and i['status'] == 'MISSING']
    return {'version': 1, 'outcome': 'MISSING' if required_missing else 'READY', 'missing': required_missing, 'items': items,
            'note': 'read-only check; no installs or downloads; clean-machine acceptance is separate (CR-28)'}


def main(argv: list[str]) -> int:
    result = check()
    if '--json' in argv:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for item in result['items']:
            print(f"{item['status']:<6} {item['entry_name']:<22} {item['detail']}")
        print(f"OUTCOME: {result['outcome']}" + (f" — missing: {', '.join(result['missing'])}" if result['missing'] else ''))
    return 0 if result['outcome'] == 'READY' else 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
