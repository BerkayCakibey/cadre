#!/usr/bin/env python3
"""Job classification, single source. stdin: budget JSON; argv[1]: prompt file; output: 'PASS PREMIUM|PASS -|SKIP -' + a jsonl trace.
Short-but-hard jobs are caught with a hard-word dictionary; read-only jobs do not enter dispatch."""
import json, os, pathlib, re, sys, time
from run_store import operation_mode  # C19: the read-only definition is a SINGLE source (acceptance record + prefilter use the same classifier)
from level import CODE_ID  # B216/B222: the code-name definition is a single source (same as the level floor)

HARD = (r'architect|refactor|\b(?:re)?design|normali[sz]|security|vulnerab|\brace\b|concurren'
       r'|deadlock|\bleak|scal(?:e|ing|ab)|async|migrat|protocol|crypto|auth|identity'
       r'|event-driven|idempoten|complex')
PREM = (r'architect|refactor|\b(?:re)?design|event-driven|idempoten|complex|scal(?:e|ing|ab)|security (?:vulnerab|hole|flaw|gap)'
        r'|\brace\b|concurren|deadlock|crypto|normali[sz]|async(?:hronous)? architecture|auth\w* layer|\bleak')   # B222: 'traceback' is not hard (word boundary on 'race')


def decision(p, has_budget=True):
    mode = operation_mode(p)
    if mode == "review":
        return "SKIP", "-"
    prem = bool(re.search(PREM, CODE_ID.sub(' ', p), re.I | re.ASCII))   # B222: a code name like `normalize_timestamp` does not count as a hard word
    if mode == "report":   # G-R6: a short report request also enters dispatch
        return ("PASS" if has_budget else "SKIP"), ("PREMIUM" if prem else "-")
    hard = bool(re.search(HARD, p, re.I | re.ASCII))
    size = (len(p.split()) >= 40 or len(re.findall(r'[A-Za-z0-9_./-]+\.[a-z0-9]{1,4}', p)) >= 2
             or bool(re.search(r'\b(and|then|also|additionally)\b|\d\)', p, re.I | re.ASCII)) or hard)
    late = size and has_budget
    return ("PASS" if late else "SKIP"), ("PREMIUM" if prem else "-")


LEVEL = {("SKIP", "-"): 0, ("PASS", "-"): 1, ("PASS", "PREMIUM"): 2}
REVERSE = {0: ("SKIP", "-"), 1: ("PASS", "-"), 2: ("PASS", "PREMIUM")}


MODEL = ("groq", "openai/gpt-oss-120b")
REQUEST_INTERVAL_SEC = float(os.environ.get('CADRE_PREFILTER_INTERVAL_SEC', '5'))   # 4c-11: Groq free tier; at least 5 s between requests


def interval(home, now_ts=None, sleep_fn=time.sleep):
    """If 5 s have not passed since the previous pre-filter request, waits for the remainder; writes the stamp for the new request."""
    from run_store import FileLock
    stamp = pathlib.Path(home) / "chain/prefilter-last-request"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(stamp.with_suffix(".lock")):
        try:
            previous = float(stamp.read_text())
        except (OSError, ValueError):
            previous = 0.0
        now_ts = time.time() if now_ts is None else now_ts
        wait = REQUEST_INTERVAL_SEC - (now_ts - previous) if previous <= now_ts else 0.0   # a stamp in the future (clock skew) is ignored
        if wait > 0:
            sleep_fn(wait)
        stamp.write_text(str(now_ts + max(wait, 0.0)))


def llm_decision(p, repo=None):
    """Task + repo map to Groq gpt-oss-120b: SIMPLE|DISTRIBUTION|PREMIUM. None if unreachable (the rule decides; 4c-11)."""
    import os, subprocess
    home = pathlib.Path(os.environ.get('CADRE_HOME', str(pathlib.Path.home()/'.local/share/cadre-engineering')))
    if os.environ.get("CADRE_PREFILTER_LLM", "1") != "1":
        return None
    map = ""
    try:
        if repo and pathlib.Path(repo).is_dir():
            r = subprocess.run(["find", repo, "-maxdepth", "2", "-type", "f", "-not", "-path", "*/.git/*"],
                               capture_output=True, text=True, timeout=5)
            map = "\n".join(r.stdout.splitlines()[:40])
    except Exception:
        pass
    pp = ("UNDERSTAND the task and the repo, then write a SINGLE WORD class: SIMPLE (one small touch / read-only), "
          "DISTRIBUTION (multiple files / sub-tasks), PREMIUM (architectural depth / high risk).\nTASK:\n" + p[:1500]
          + ("\nREPO MAP:\n" + map if map else ""))
    for prov, dm in (MODEL,):
        try:
            interval(home)
            env = dict(os.environ, PP=pp, DM=dm, PROV=prov, PLAIN="1")
            r = subprocess.run([sys.executable, str(pathlib.Path(__file__).with_name('plan_ds.py'))],
                               capture_output=True, text=True, timeout=12, env=env)
            c = r.stdout.strip()
            c = c.upper() if c.isascii() else ''
            for label, outcome in (("PREMIUM", 2), ("DISTRIBUTION", 1), ("SIMPLE", 0)):
                if label in c:
                    return outcome
        except Exception:
            continue
    return None


if __name__ == "__main__":
    b = json.load(sys.stdin)
    p = open(sys.argv[1]).read()
    go = b.get('go') or {}
    # 4c QA: the Go ratio measures the narrowest window (the wall); the dispatch decision uses the open pool and at least one point
    budget = ((go.get('window_open') is True and float(go.get('available') or 0) >= 1.0)
             or b.get('or', {}).get('available', 0) > 0.1
             or b.get('zen', {}).get('available', 0) > 0.1 or b.get('claude', {}).get('pay', 0) > 4)
    g, pr = decision(p, budget)
    rule_level = LEVEL[(g, pr)]
    llm_level = llm_decision(p, sys.argv[2] if len(sys.argv) > 2 else None)
    # rule = FLOOR: the LLM may raise to dispatch, only the rule gives PREMIUM
    last_level = max(rule_level, min(llm_level, 1)) if llm_level is not None else rule_level
    if operation_mode(p) == "review":
        last_level = 0  # C19: a read-only request CANNOT enter dispatch; the LLM cannot upgrade this mode (write permission is off in the engine config)
    if not budget and last_level > 0:
        last_level = 0  # no dispatch without a budget (rule-based path)
    g, pr = REVERSE[last_level]
    try:  # tracing: every decision goes to the ledger (the verifier looks for leaks) — respects CADRE_HOME (tests must not write to live)
        import os
        trace = pathlib.Path(os.environ.get('CADRE_HOME', str(pathlib.Path.home()/'.local/share/cadre-engineering'))) / "chain/category-history.jsonl"
        trace.parent.mkdir(parents=True, exist_ok=True)
        trace.open("a").write(json.dumps({"ts": time.strftime("%FT%T"), "decision": g, "premium": pr, "rule": rule_level, "llm": llm_level,
                                       "word": len(p.split()), "summary": p[:80]}, ensure_ascii=False) + "\n")
    except Exception:
        pass
    print(g, pr)
