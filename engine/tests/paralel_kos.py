#!/usr/bin/env python3
"""Runs unit tests in a separate process per module, in parallel (02.10: `unittest discover` in one process took ~11 min).

Each module gets its own isolated HOME/CADRE_HOME. Output is in unittest form ('Ran N tests' + 'OK' / 'FAILED'),
the regression's RC51/RC51b lines read it as is. Usage: `python3 paralel_kos.py [module-part ...]`;
by default at most four workers, explicit choice with `CADRE_TEST_ISCI`.
"""
import concurrent.futures
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'lib'))
import qa_kaniti

KOK = pathlib.Path(__file__).resolve().parent


def isci_sayisi(env: dict[str, str], cpu: int | None) -> int:
    """N-OBS05: heavy modules that spawn subprocesses must not spread over all CPUs and trigger their own timeouts."""
    setting = env.get('CADRE_TEST_ISCI')
    if not setting:
        return max(1, min(4, cpu or 4))
    workers = int(setting)
    if workers < 1:
        raise ValueError('positive worker count required')
    return workers


def kos(mod: str, taban: str) -> tuple[str, int, str, float]:
    ev = pathlib.Path(tempfile.mkdtemp(prefix=f"ut-{mod}-", dir=taban))
    (ev / "ev").mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(('CADRE_', 'OPENCODE_', 'E2E_'))}
    env.update(HOME=str(ev / 'ev'), CADRE_HOME=str(ev), CADRE_ENGINE_ROOT=str(KOK.parent), CADRE_BEKLEME_ESIK_SN='0')
    # N-OBS05: an empty fixture under .tmp must not take the parent source repository for its own Git identity.
    env['GIT_CEILING_DIRECTORIES'] = os.pathsep.join(filter(None, (os.environ.get('GIT_CEILING_DIRECTORIES'), tempfile.gettempdir())))
    for key in ('CADRE_QA_EVIDENCE', 'CADRE_QA_TOKEN', 'CADRE_TEST_PYTEST_PYTHON'):
        if key in os.environ:
            env[key] = os.environ[key]
    arayuz = os.environ.get('CADRE_UI_PYTHON') or str(pathlib.Path.home() / '.local/share/cadre-ui-venv/bin/python')
    if pathlib.Path(arayuz).exists():
        env['CADRE_TEST_ARAYUZ_PYTHON'] = arayuz
    external_evidence = bool(env.get('CADRE_QA_EVIDENCE'))
    evidence, token = ev / 'test-evidence.jsonl', uuid.uuid4().hex
    if not external_evidence:
        env = qa_kaniti.environment(env, evidence, token)
    t0 = time.time()
    with subprocess.Popen([sys.executable, '-W', 'default::ResourceWarning', "-m", "unittest", mod],
                          cwd=KOK, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, start_new_session=True) as process:
        try:
            out, err = process.communicate(timeout=1800)
            rc, output = process.returncode, out + err
            if not external_evidence:
                expected = qa_kaniti.inventory(KOK)
                expected = {p: names for p, names in expected.items() if p == mod + '.py'}
                proof = qa_kaniti.coverage(KOK, evidence, token, expected, True)
                if proof['status'] != 'PASS':
                    rc = rc or 1
                    output += '\nQA DISCOVERY: ' + str(proof) + '\n'
            return mod, rc, output, time.time() - t0
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            out, err = process.communicate()
            return mod, 124, out + err + '\nHATA: test modulu 1800 sn sure sinirini asti\n', time.time() - t0


def main() -> int:
    moduller = sorted(f.stem for f in KOK.glob("test_*.py"))
    if len(sys.argv) > 1:
        moduller = [m for m in moduller if any(a in m for a in sys.argv[1:])]
    if not moduller:
        print('HATA: filtreyle eslesen test modulu yok', file=sys.stderr)
        return 2
    if len(sys.argv) == 1:
        expected = qa_kaniti.inventory(KOK.parent.parent)
        missing = sorted(p for p in expected if p.endswith('.py') and p not in {'engine/tests/' + m + '.py' for m in moduller})
        if missing:
            print('HATA: ortak kapida kesfedilmeyen test dosyalari: ' + ', '.join(missing), file=sys.stderr)
            return 2
    ayar = os.environ.get("CADRE_TEST_ISCI")
    try:
        isci = isci_sayisi(dict(os.environ), os.cpu_count())
    except ValueError:
        print(f'HATA: CADRE_TEST_ISCI pozitif tam sayi olmali: {ayar!r}', file=sys.stderr)
        return 2
    print(f"isci: {isci} (CADRE_TEST_ISCI={ayar or '-'}, cpu={os.cpu_count()})", flush=True)
    taban = tempfile.mkdtemp(prefix="qe-paralel-")
    toplam, atlanan, kotu, sureler, t0 = 0, 0, [], [], time.time()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=isci) as ex:
            for mod, rc, cikti, sure in ex.map(lambda m: kos(m, taban), moduller):
                n = re.findall(r"^Ran (\d+) tests?", cikti, re.M)
                toplam += int(n[-1]) if n else 0
                skipped = re.findall(r'^OK \(skipped=(\d+)\)', cikti, re.M)
                atlanan += int(skipped[-1]) if skipped else 0
                sureler.append((sure, mod))
                bad = rc != 0 or not n or int(n[-1]) == 0 or "ResourceWarning:" in cikti
                if bad:
                    print(cikti)
                if skipped:
                    print(f'SKIP: {mod}: {skipped[-1]} test')
                if bad:
                    kotu.append(mod)
    finally:
        shutil.rmtree(taban, ignore_errors=True)
    print("en yavaş modüller: " + ", ".join(f"{m} {s:.0f}sn" for s, m in sorted(sureler, reverse=True)[:8]))
    print(f"Ran {toplam} tests in {time.time() - t0:.1f}s")
    print(f'Atlanan: {atlanan}; uygulanan: {toplam - atlanan}')
    print("OK" if not kotu else f"FAILED (modüller: {' '.join(kotu)})")
    return 1 if kotu else 0


if __name__ == "__main__":
    sys.exit(main())
