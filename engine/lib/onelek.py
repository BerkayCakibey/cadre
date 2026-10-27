#!/usr/bin/env python3
"""Job classification, single source. stdin: budget JSON; argv[1]: prompt file; output: 'GECER PREMIUM|GECER -|ATLA -' + a jsonl trace.
Short-but-hard jobs are caught with a hard-word dictionary; read-only jobs do not enter dispatch."""
import json, os, pathlib, re, sys, time
from kosu import calisma_kipi  # C19: the read-only definition is a SINGLE source (acceptance record + classifier use the same regex)
from seviye import KOD_KIMLIGI  # B216/B222: the code-name definition is a single source (same as the level floor)

ZOR = (r'mimari|refactor|tasarl?a|tasarim|normalize|guvenlik|acig|\brace\b|eszamanl|concurrency'
       r'|deadlock|sizinti|leak|olcekle|asenkron|async|migration|protokol|kripto|auth|kimlik'
       r'|event-driven|idempoten|karmasik')
PREM = (r'mimari|refactor|tasarl?a|tasarim|event-driven|idempoten|karmasik|olcekle|guvenlik acig'
        r'|\brace\b|eszamanl|deadlock|kripto|normalize|asenkron mimari|auth katman|sizinti|leak')   # B222: 'traceback' is not hard


def karar(p, butce_var=True):
    kip = calisma_kipi(p)
    if kip == "inceleme":
        return "ATLA", "-"
    prem = bool(re.search(PREM, KOD_KIMLIGI.sub(' ', p), re.I))   # B222: a code name like `normalize_timestamp` does not count as a hard word
    if kip == "rapor":   # G-R6: a short report request also enters dispatch
        return ("GECER" if butce_var else "ATLA"), ("PREMIUM" if prem else "-")
    zor = bool(re.search(ZOR, p, re.I))
    boyut = (len(p.split()) >= 40 or len(re.findall(r'[A-Za-z0-9_./-]+\.[a-z0-9]{1,4}', p)) >= 2
             or bool(re.search(r'\b(ve|sonra|ayrica)\b|\d\)', p, re.I)) or zor)
    gec = boyut and butce_var
    return ("GECER" if gec else "ATLA"), ("PREMIUM" if prem else "-")


SEVIYE = {("ATLA", "-"): 0, ("GECER", "-"): 1, ("GECER", "PREMIUM"): 2}
TERS = {0: ("ATLA", "-"), 1: ("GECER", "-"), 2: ("GECER", "PREMIUM")}


MODEL = ("groq", "openai/gpt-oss-120b")
ISTEK_ARALIGI_SN = float(os.environ.get('CADRE_ONELEK_ARALIK_SN', '5'))   # 4c-11: Groq free tier; at least 5 s between requests


def aralik(home, simdi=None, uyu=time.sleep):
    """If 5 s have not passed since the previous pre-filter request, waits for the remainder; writes the stamp for the new request."""
    from kosu import FileLock
    damga = pathlib.Path(home) / "zincir/onelek-son-istek"
    damga.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(damga.with_suffix(".lock")):
        try:
            once = float(damga.read_text())
        except (OSError, ValueError):
            once = 0.0
        simdi = time.time() if simdi is None else simdi
        bekle = ISTEK_ARALIGI_SN - (simdi - once) if once <= simdi else 0.0   # a stamp in the future (clock skew) is ignored
        if bekle > 0:
            uyu(bekle)
        damga.write_text(str(simdi + max(bekle, 0.0)))


def llm_karar(p, repo=None):
    """Task + repo map to Groq gpt-oss-120b: BASIT|DAGITIM|PREMIUM. None if unreachable (the rule decides; 4c-11)."""
    import os, subprocess
    home = pathlib.Path(os.environ.get('CADRE_HOME', str(pathlib.Path.home()/'.local/share/cadre-engineering')))
    if os.environ.get("CADRE_ONELEK_LLM", "1") != "1":
        return None
    harita = ""
    try:
        if repo and pathlib.Path(repo).is_dir():
            r = subprocess.run(["find", repo, "-maxdepth", "2", "-type", "f", "-not", "-path", "*/.git/*"],
                               capture_output=True, text=True, timeout=5)
            harita = "\n".join(r.stdout.splitlines()[:40])
    except Exception:
        pass
    pp = ("Gorevi ve repoyu ANLA, TEK KELIME sinif yaz: BASIT (tek kucuk dokunus/salt-okuma), "
          "DAGITIM (coklu dosya/alt-gorev), PREMIUM (mimari derinlik/yuksek risk).\nGOREV:\n" + p[:1500]
          + ("\nREPO HARITASI:\n" + harita if harita else ""))
    for prov, dm in (MODEL,):
        try:
            aralik(home)
            env = dict(os.environ, PP=pp, DM=dm, PROV=prov, DUZ="1")
            r = subprocess.run([sys.executable, str(pathlib.Path(__file__).with_name('plan_ds.py'))],
                               capture_output=True, text=True, timeout=12, env=env)
            c = r.stdout.upper()
            for etiket, sonuc in (("PREMIUM", 2), ("DAGITIM", 1), ("BASIT", 0)):
                if etiket in c:
                    return sonuc
        except Exception:
            continue
    return None


if __name__ == "__main__":
    b = json.load(sys.stdin)
    p = open(sys.argv[1]).read()
    go = b.get('go') or {}
    # 4c QA: the Go ratio measures the narrowest window (the wall); the dispatch decision uses the open pool and at least one point
    butce = ((go.get('pencere_acik') is True and float(go.get('kullanilabilir') or 0) >= 1.0)
             or b.get('or', {}).get('kullanilabilir', 0) > 0.1
             or b.get('zen', {}).get('kullanilabilir', 0) > 0.1 or b.get('claude', {}).get('pay', 0) > 4)
    g, pr = karar(p, butce)
    kural_sev = SEVIYE[(g, pr)]
    llm_sev = llm_karar(p, sys.argv[2] if len(sys.argv) > 2 else None)
    # rule = FLOOR: the LLM may raise to dispatch, only the rule gives PREMIUM
    son_sev = max(kural_sev, min(llm_sev, 1)) if llm_sev is not None else kural_sev
    if calisma_kipi(p) == "inceleme":
        son_sev = 0  # C19: a read-only request CANNOT enter dispatch; the LLM cannot upgrade this mode (write permission is off in the engine config)
    if not butce and son_sev > 0:
        son_sev = 0  # no dispatch without a budget (rule-based path)
    g, pr = TERS[son_sev]
    try:  # tracing: every decision goes to the ledger (the verifier looks for leaks) — respects CADRE_HOME (tests must not write to live)
        import os
        iz = pathlib.Path(os.environ.get('CADRE_HOME', str(pathlib.Path.home()/'.local/share/cadre-engineering'))) / "zincir/sinif-gecmisi.jsonl"
        iz.parent.mkdir(parents=True, exist_ok=True)
        iz.open("a").write(json.dumps({"ts": time.strftime("%FT%T"), "karar": g, "premium": pr, "kural": kural_sev, "llm": llm_sev,
                                       "kelime": len(p.split()), "ozet": p[:80]}, ensure_ascii=False) + "\n")
    except Exception:
        pass
    print(g, pr)
