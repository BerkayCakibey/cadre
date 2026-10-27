#!/usr/bin/env python3
"""V3: code + config + hook + Python/Node behaviour, only in a temporary home.

The gate does not claim live model and provider acceptance.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE / 'lib'))
import qa_kaniti


def arsiv_yolu() -> Path:
    if os.environ.get('CADRE_TEST_ARSIVI'):
        return Path(os.environ['CADRE_TEST_ARSIVI'])
    return ENGINE.parent / '.tmp/test-arsivi'


def arsivle(arsiv: Path, adimlar: list[dict], gunluk: str, sonuc: str) -> None:
    git = lambda *a: subprocess.run(['git', '-C', str(ENGINE.parent), *a], capture_output=True, text=True).stdout.strip()
    ran = re.findall(r'^Ran (\d+) tests', gunluk, re.M)
    isci = re.findall(r'^isci: (\d+)', gunluk, re.M)
    atlanan = re.findall(r'^Atlanan: (\d+)', gunluk, re.M)
    node = dict(re.findall(r'^\u2139 (tests|pass|fail|skipped) (\d+)', gunluk, re.M))
    damga = time.strftime('%Y%m%d-%H%M%S')
    kayit = {'ts': damga, 'commit': git('rev-parse', '--short', 'HEAD'), 'kirli': bool(git('status', '--porcelain')),
             'sonuc': sonuc, 'adimlar': adimlar, 'python_test': int(ran[-1]) if ran else None,
             'python_atlanan': int(atlanan[-1]) if atlanan else None, 'node': {k: int(v) for k, v in node.items()},
             'isci': int(isci[-1]) if isci else None}
    try:
        (arsiv / 'regresyon').mkdir(parents=True, exist_ok=True)
        (arsiv / 'regresyon' / f"{damga}-{kayit['commit'] or 'commitsiz'}.log").write_text(gunluk)
        with open(arsiv / 'regresyon.jsonl', 'a') as f:
            f.write(json.dumps(kayit, ensure_ascii=False) + '\n')
        print(f'ARSIV: {arsiv}/regresyon.jsonl')
    except OSError as exc:
        print(f'UYARI arsivlenemedi: {exc}', file=sys.stderr)


def ortam_uyarisi(env: dict) -> str:
    if env.get('CADRE_TEST_PYTEST_PYTHON'):
        return ''
    return ('UYARI: CADRE_TEST_PYTEST_PYTHON tanımsız — test_qa_kapsami pytest+ruff+basedpyright içeren bir Python ister; '
            'ortak kapı bu ortamda kırmızı olur (FIX-18). Örnek: CADRE_TEST_PYTEST_PYTHON=/path/to/venv/bin/python')


def main() -> int:
    uyari = ortam_uyarisi(dict(os.environ))
    if uyari:
        print(uyari, flush=True)
    passed = failed = 0
    arsiv, adimlar, gunluk = arsiv_yolu(), [], []
    arayuz = Path(os.environ.get('CADRE_UI_PYTHON') or Path.home() / '.local/share/cadre-ui-venv/bin/python')
    with tempfile.TemporaryDirectory(prefix='qe-regresyon-') as directory:
        home = Path(directory)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('CADRE_', 'OPENCODE_', 'E2E_', 'XDG_', 'GIT_'))}
        env.update(HOME=str(home), CADRE_HOME=str(home / 'data'), CADRE_ENGINE_ROOT=str(ENGINE),
                   GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0',
                   CADRE_BEKLEME_ESIK_SN='0', PYTHONDONTWRITEBYTECODE='1')
        if arayuz.exists():
            env['CADRE_UI_PYTHON'] = str(arayuz)
        for key in ('CADRE_TEST_PYTEST_PYTHON', 'CADRE_TEST_ISCI'):
            if os.environ.get(key):
                env[key] = os.environ[key]
        commands = [
            ('sozdizimi', [sys.executable, str(ENGINE / 'lib/qa_turet.py'), 'sozdizim', str(ENGINE.parent)]),
            ('Python davranis + sozlesme + config', [sys.executable, str(ENGINE / 'tests/paralel_kos.py')]),
            ('Node butce eklentisi + gercek-eklenti entegrasyonu', ['node', '--test', str(ENGINE / 'tests/butce-plugin.test.mjs'),
                                                                  str(ENGINE / 'tests/entegrasyon.test.mjs'),
                                                                  str(ENGINE / 'tests/dusunme.test.mjs')]),
            ('Git teslim korumasi (yerel bare repo)', ['bash', str(ENGINE / 'tests/pushguard.sh')]),
        ]
        for name, command in commands:
            print(f'== {name} ==', flush=True)
            bas = time.time()
            step_env, proof = env, None
            if name.startswith('Node'):
                arsiv.mkdir(parents=True, exist_ok=True)
                token = uuid.uuid4().hex
                evidence = arsiv / ('node-evidence-' + token + '.jsonl')
                step_env = {**env, 'CADRE_QA_EVIDENCE': str(evidence), 'CADRE_QA_TOKEN': token}
                command = [*command[:2], '--test-reporter=spec', '--test-reporter-destination=stdout',
                           '--test-reporter=' + str(ENGINE / 'lib/qa-node-reporter.cjs'),
                           '--test-reporter-destination=stdout', *command[2:]]
            try:
                with subprocess.Popen(command, cwd=ENGINE.parent, env=step_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, errors='replace') as proc:
                    saat = threading.Timer(2400, proc.kill)
                    saat.start()
                    try:
                        for satir in proc.stdout:
                            print(satir, end='', flush=True)
                            gunluk.append(satir)
                        success = proc.wait() == 0
                        if name.startswith('Python'):
                            skipped = re.findall(r'^Atlanan: (\d+)', ''.join(gunluk), re.M)
                            success = success and bool(skipped) and int(skipped[-1]) == 0
                        if name.startswith('Node'):
                            expected = {p: names for p, names in qa_kaniti.inventory(ENGINE.parent).items() if not p.endswith('.py')}
                            proof = qa_kaniti.coverage(ENGINE.parent, evidence, token, expected, False)
                            success = success and proof['status'] == 'PASS'
                            print('Node discovery: ' + json.dumps(proof, ensure_ascii=False), flush=True)
                    finally:
                        saat.cancel()
            except (OSError, ValueError, SyntaxError, subprocess.TimeoutExpired) as exc:
                print(f'HATA: {name}: {exc}', file=sys.stderr)
                success = False
            adimlar.append({'ad': name, 'sonuc': 'PASS' if success else 'FAIL', 'sn': round(time.time() - bas, 1)})
            if proof is not None:
                adimlar[-1]['discovery'] = proof
                adimlar[-1]['evidence'] = str(evidence)
            if success:
                passed += 1
            else:
                failed += 1
            print(f'  {"PASS" if success else "FAIL"}  {name}', flush=True)
    print('  SKIP  Canli model/saglayici kabul kosusu (HAZIR-1)')
    sonuc = f'SONUC: {passed} PASS, {failed} FAIL, 1 SKIP'
    adimlar.append({'ad': 'Canli model/saglayici kabul kosusu (HAZIR-1)', 'sonuc': 'SKIP', 'sn': 0})
    arsivle(arsiv, adimlar, ''.join(gunluk), sonuc)
    print(sonuc)
    return int(failed > 0)


if __name__ == '__main__':
    raise SystemExit(main())
