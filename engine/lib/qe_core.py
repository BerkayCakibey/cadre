#!/usr/bin/env python3
"""Python core of the engine: run loop, worktree, stall and error guards, Go brake, restricted config, QA and ledger.
Log lines and the env interface are a contract; tests look at these texts."""
import contextlib
import hashlib
import json
import os
import pathlib
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.request
import uuid

if __name__ == "__main__":
    sys.modules["qe_core"] = sys.modules[__name__]

from kosu import FileLock, RunStore, atomic_json, atomic_write, bounded_json, cancel_path, execute_run, owner_fds, task_root, run_job_type
from kosu import bekci_dirilt as kosu_bekci_dirilt, disk_durumu
import opencode_adaptor as OC
from oturum import Attempt, RecordedProcess, SessionRecoveryError, result_json, retire_owned_tools, session_ids, session_rows
from butce_kapisi import (BudgetDenied, BudgetLedger, SaglayiciBekleme, saglayici_bekleme_reddi, FREE as BUTCE_FREE, PAID as BUTCE_PAID, allowance, available_budget,
                          observed_budget, run_remaining_usd, spending, task_bans, note_denial)
import sinirlar as SINIR

QE = pathlib.Path(os.environ.get("CADRE_HOME", str(pathlib.Path.home() / ".local/share/cadre-engineering")))
ENV = os.environ.get
CODE = pathlib.Path(ENV("CADRE_ENGINE_ROOT", str(pathlib.Path(__file__).resolve().parents[1])))


def log(logf, msg):
    line = f"{time.strftime('%F %T')} {msg}"
    print(line, file=sys.stderr)
    with open(logf, "a") as f:
        f.write(line + "\n")


class CancelledRun(RuntimeError):
    """The run's durable cancel flag was seen while waiting; no new executor may start."""


class SessionStalled(RuntimeError):
    pass


class SessionLoop(SessionStalled):
    pass


def wait_process(proc, timeout: float, proje=None, wt=None, logf=None):
    """Long model/QA wait: cancel, stuck and deadline are seen from the same loop; the deadline applies even while there is liveness (4c-4)."""
    deadline = time.monotonic() + timeout
    canlilik = Canlilik(logf or proc.log_path, proc, wt) if isinstance(proc, RecordedProcess) or logf else None
    tekrar = AracTekrari(proc.attempt.stdout_path, 5) if isinstance(proc, RecordedProcess) else None
    if tekrar:
        tekrar.ofset = 0
    kontrol = 0.0
    try:
        while True:
            if iptal_istendi(proje):
                raise CancelledRun('iptal')
            remaining = deadline - time.monotonic()
            if canlilik and time.monotonic() >= kontrol:
                kontrol = time.monotonic() + 15
                if tekrar and tekrar.tik():
                    raise SessionLoop('dongu: ayni arac cagrisinin bes tekrari')
                if canlilik.takildi():
                    raise SessionStalled('CANLI1: tum canlilik isaretleri durdu')
            if remaining <= 0:
                raise subprocess.TimeoutExpired(proc.args, timeout)
            try:
                return proc.wait(timeout=min(.5, remaining))
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        native = proc.proc if isinstance(proc, RecordedProcess) else proc
        if native.poll() is None:
            kill_tree(proc.pid)
        reap(proc)
        raise


def wait_pause(seconds: float, proje=None):
    deadline = time.monotonic() + seconds
    while True:
        if iptal_istendi(proje):
            raise CancelledRun('iptal')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(.5, remaining))


def iptal_istendi(proje=None):
    """Persistent cancel flag: runs/<id>/cancel.flag if the run is recorded; zincir/<project>-iptal.flag for the old path."""
    try:
        if ENV("CADRE_RUN_ID"):
            return (RunStore(QE).path(ENV("CADRE_RUN_ID")) / "cancel.flag").exists()
        return bool(proje) and cancel_path(proje).exists()
    except Exception:
        return False


def _cocuklar(pid):
    """All child processes including grandchildren (collected BEFORE TERM; when the parent dies the children are re-parented)."""
    try:
        out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=5).stdout.split()
    except Exception:
        return []
    result = []
    for child in out:
        try:
            child = int(child)
        except ValueError:
            continue
        result.append(child)
        result += _cocuklar(child)
    return result


def pid_yasiyor(pid):
    """A zombie (Z) counts as dead: a direct child stays in the table until reaped, kill(0) is misleading."""
    try:
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return False
    return bool(state) and not state.startswith("Z")


def kill_tree(pid, grace=None):
    """Close the process tree (defect #12 + C7): TERM from the grandchildren up to the top -> wait at most `grace` s -> KILL the rest.
    Returns: True = cleaned with TERM, False = KILL was needed. A child that ignores TERM is cleaned within a limited time."""
    grace = float(ENV("CADRE_KILL_GRACE", "10")) if grace is None else float(grace)
    return OC.cancel(pid, grace, children=_cocuklar, alive=pid_yasiyor)


def reap(proc, timeout: float = 15):
    """Limited wait: the child is reaped after TERM/KILL; never an unbounded `wait()` (C7)."""
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait(timeout=timeout)


def durdur(proc, grace=None):
    """kill_tree + reap in one step; all of the engine's interruption points use this."""
    kill_tree(proc.pid, grace)
    return reap(proc)


def bulut_aday(rol="impl", yasak=(), wt=None):
    """The first CLOUD model with budget/permission from the router's `rol` chain (except claude; task bans dropped).
    If the budget is unknown the chain comes back empty -> ''. A single decision point (kurtarici_sec uses it too)."""
    if not YON:
        return ""
    try:
        b = butce_json()
        chain, _ = YON.zincir(rol, "orta", istek_olcusu(wt) if wt else 30000, b if isinstance(b, dict) else {}, YON.durum_oku())
    except Exception:
        return ""
    for candidate in chain:
        if candidate.startswith("claude/") or YON.saglayici(candidate) in set(yasak):
            continue
        if rol == "kurtarici" and getattr(YON, "kurtarici_nefesi", lambda *_: None)(candidate, b if isinstance(b, dict) else {}):
            continue   # B277: a share hand-off that cannot carry the session falls back after ~2 min
        return candidate
    return ""


def disk_bekle(logf, proje=None, deadline=None):
    """P2: if free space is below the threshold (CADRE_DISK_MIN_MB, 2048) no NEW executor starts; polled up to the cap (CADRE_DISK_WAIT, 1 day).
    True = enough · None = cancel flag while waiting · False = cap reached (reason written to a structured field)."""
    ok, bos, esik = disk_durumu(QE)
    if ok:
        return True
    tavan = float(ENV("CADRE_DISK_WAIT", "86400"))  # disk: 1 day (rotation frees space daily)
    poll = float(ENV("CADRE_DISK_POLL", "300"))
    son = min(time.monotonic() + tavan, deadline) if deadline is not None else time.monotonic() + tavan
    etkin = max(0, son - time.monotonic())
    log(logf, f"DISK DUSUK: {bos} MB < {esik} MB — yurutucu baslatilmiyor, en cok {etkin:.1f} sn beklenir (iptal bayragi izlenir)")
    while time.monotonic() < son:
        if iptal_istendi(proje):
            log(logf, "disk beklenirken IPTAL bayragi goruldu — bekleme kesildi")
            return None
        try:
            wait_pause(min(poll, max(0.0, son - time.monotonic())), proje)
        except CancelledRun:
            return None
        ok, bos, esik = disk_durumu(QE)
        if ok:
            log(logf, f"disk yeterli ({bos} MB) — devam")
            return True
    log(logf, f"disk bekleme suresi {etkin:.1f} sn doldu ({bos} MB < {esik} MB) — birakiliyor")
    if ENV("CADRE_RUN_ID"):
        RunStore(QE).update(ENV("CADRE_RUN_ID"), reason=f"disk-low: {bos} MB < {esik} MB (motor bekleme tavani doldu)")
    return False


def claude_command(command):
    return [sys.executable, str(pathlib.Path(__file__).with_name('claude_transport.py')), *command]


def run_claude(wt, model_effort, pf_text, logf, tools=None, budget=None, turns=None, role="impl", difficulty=None):
    """claude -p lane: claude/<model>#<effort>; the cost from the JSON goes to the ledger. tools/budget/turns: read-only, small-budget use (plan).
    C31: the requested model/effort is a request; the weekly reserve + task difficulty + the daily Fable slot decide
    (easy/medium -> sonnet#high, hard -> opus#xhigh, hardest -> max; Fable only at difficulty 5 + slot + share>=10)."""
    if iptal_istendi():
        raise CancelledRun('iptal')
    observed = butce_json()
    neden = ''   # B249: the sub-reason (stale observation / ban / pocket / policy) is recorded; 'unavailable' alone was not enough for the RCA
    try:
        pay_var = available_budget(QE, observed, 'claude', rol=role) > 0   # B112: the pocket comes from the ROLE
        if not pay_var:
            neden = 'pay 0 (cep yasagi, kosu cebi ya da politika payi)'
    except BudgetDenied as exc:
        pay_var = False          # no/broken observation: closed-failure, but still recorded
        neden = str(exc) or 'gozlem yok'
    if not pay_var:
        # B114-3: this denial never reached the gate, so it was not written to `denials[]` and the morning summary could not read
        # the silent closure from the record. The record must be in the same place as the denial itself.
        note_denial(QE, 'claude', reason=f'Claude allowance unavailable: {neden}')
        raise BudgetDenied(f'Claude allowance unavailable: {neden}')
    from claude_politikasi import difficulty as task_difficulty, fable_slots, model as policy_model
    try:
        level = task_difficulty(QE, role, difficulty)
        model_effort = policy_model(model_effort, observed, level, fable_slots(QE, observed))
    except (ValueError, IndexError, TypeError, OSError) as exc:
        raise BudgetDenied(f'Claude policy refused the request: {exc}') from exc
    log(logf, f"claude politika: zorluk={level} -> {model_effort}")
    bin_ = ENV("CLAUDE_BIN", "claude")
    m, _, eff = model_effort.partition("#")
    m = m.split("/", 1)[1]
    tavan = float(budget or ENV("CADRE_CLAUDE_BUDGET", {"opus": "5.0", "fable": "8.0"}.get(m.split("-")[0], "2.0")))
    kalan = run_remaining_usd(QE, ENV("CADRE_RUN_ID"), role, 'claude')  # P5+B112: the cap comes from the CALLING ROLE's pocket
    if kalan is not None:
        if kalan <= 0:
            raise BudgetDenied('run budget exhausted')
        tavan = min(tavan, kalan)
    cmd = [bin_, "-p", "--model", m, "--output-format", "stream-json", "--verbose",
           "--max-turns", turns or ENV("CADRE_CLAUDE_TURNS", "20"),
           "--max-budget-usd", f"{tavan:g}",
           "--allowedTools", tools or CLAUDE_YAZAN_TOOLS]
    if eff:
        cmd += ["--effort", eff]
    record = run_record()
    # B110-2: the Claude lane had NO path restriction for `.env`/key files; both reading and
    # carrying were open. The body check at the boundary is the second gate, the first is never reading.
    yasak = [CLAUDE_SECRET_PATHS]
    if run_job_type(record) == "review" or tools == CLAUDE_READ_ONLY_TOOLS:  # C19: in a read-only run the write tools are TECHNICALLY closed
        cmd[cmd.index("--allowedTools") + 1] = CLAUDE_READ_ONLY_TOOLS
        yasak.insert(0, CLAUDE_WRITE_TOOLS)
    cmd += ["--disallowedTools", ",".join(yasak)]
    memory = "\n\n".join(f"# {pathlib.Path(p).name}\n{pathlib.Path(p).read_text(errors='replace')}" for p in project_memory(record))
    if memory:  # C17: project memory also goes to the Claude lane
        cmd += ["--append-system-prompt", memory[:60000]]
    if ENV("CADRE_RUN_ID"):
        attempt = Attempt("claude", role, model_effort, pf_text)
        if attempt.resuming:
            cmd += ["--resume", attempt.data["session_id"]]
        else:
            cmd += ["--session-id", attempt.data["session_id"]]
        cmd += ["--", attempt.prompt]
        env = {**os.environ, 'CADRE_HOME':str(QE), 'CADRE_ENGINE_ROOT':str(CODE),
               'CADRE_BUDGET_ATTEMPT':attempt.data['attempt_id'],
               'CADRE_CLAUDE_ROL':role,   # B72: role of Claude lines in the spending breakdown (plan/kalite/impl)
               'CADRE_OWNER_FDS':','.join(map(str, (*owner_fds(), attempt.lease.fd)))}
        return attempt.launch(claude_command(cmd), str(wt), pathlib.Path(logf), env, owner_fds())
    raise ValueError("Claude execution requires an accepted run")


def claude_maliyet_kaydet(proc_stdout, model, logf, attempt=None):
    try:
        j = result_json(proc_stdout)
        import math
        raw_cost = j.get("total_cost_usd", j.get("cost_usd"))
        if raw_cost is None:
            raise ValueError("cost receipt missing")
        cost = float(raw_cost)
        if not math.isfinite(cost) or cost < 0:
            raise ValueError("invalid cost receipt")
        satir = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": model, "usd": cost,
                 "run_id": ENV("CADRE_RUN_ID", ""), "task_id": ENV("CADRE_GOREV_ID", "") or "main",
                 "session_id": j.get("session_id"), "attempt_id": (attempt or {}).get("attempt_id")}
        try:
            pool = float((QE / "zincir/claude-havuz-usd.txt").read_text().strip())
            if math.isfinite(pool) and pool > 0:
                satir["yuzde_tahmin"] = round(cost / pool * 100, 4)
        except (OSError, ValueError):
            pass  # Missing quota calibration must never discard actual USD.
        path = QE / "zincir/claude-harcama.jsonl"
        with FileLock(path.with_suffix(".lock")):
            prior = path.read_text().splitlines() if path.exists() else []
            for line in prior:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue  # Preserve damaged history, but still record the new receipt.
                if isinstance(entry, dict) and satir["attempt_id"] and entry.get("attempt_id") == satir["attempt_id"]:
                    return
            with path.open("a") as f:
                if path.stat().st_size and prior and not path.read_bytes().endswith(b"\n"):
                    f.write("\n")  # An interrupted last line must not swallow the new JSON record.
                f.write(json.dumps(satir) + "\n")
                f.flush()
                os.fsync(f.fileno())
        log(logf, f"claude maliyet: ${cost} ({model})")
        if j.get("result"):
            with open(logf, "a") as f:
                f.write(str(j["result"])[-3000:] + "\n")
    except Exception:
        log(logf, "claude maliyet okunamadi")


def kisit_model_sec(b, hassas=False, yasak=None, rol="impl"):
    """Model for a sub-agent: asked from the chain of that agent's OWN role, with the context size that role declares (B121).
    A model that cannot hold the context is not written to the agent. 4c-10: Go per model, not as a whole (the chain measures budget per model)."""
    yasak = set(yasak or ())
    # If the budget observation cannot be read no candidate is selected (fail-closed, B93).
    if YON is None:
        return None, "kapasite-yok"
    if not isinstance(b, dict) or not b:
        return None, 'butce-yok'
    d = YON.durum_oku()
    z, _ = YON.zincir(rol, "orta", None, b, d)   # B121: size comes from the role's declaration, not hand-written
    for m in z:
        sag = YON.saglayici(m)
        if sag in yasak or sag == "claude":   # claude is not a sub-agent provider
            continue
        # 0b: the bucket unit is the model. A '<provider>:free' ban removes only that provider's ZERO-PRICED models;
        # the same provider's paid models keep working (previously the two could not be told apart).
        if f"{sag}:free" in yasak:
            try:
                from katalog import bedava_mi
                if bedava_mi(sag, m.split("/", 1)[1] if "/" in m else m) is True:
                    continue
            except Exception:
                pass
        return m, sag
    return None, "kapasite-yok"


IZIN_KURALLARI = {
    "deploy": {"*deploy_release.sh*": "allow", "*deploy_permissions.sh*": "allow", "rsync *": "allow", "scp *": "allow"},
    "rsync": {"rsync *": "allow"},
    "scp": {"scp *": "allow"},
    "push": {},   # git push is already open (except force); meaning: push to the canonical branch is allowed (build.md text)
    "merge": {},  # meaning: merge to the canonical branch is allowed (build.md text)
}


def izin_ayikla(prompt_metni):
    """An `IZIN: deploy, push` line in the task text -> {'deploy','push'}."""
    izin = set()
    for l in (prompt_metni or "").splitlines():
        m = re.match(r"^\s*[İIi][Zz][İIi][Nn]\s*:\s*(.+)$", l.strip())
        if m:
            for tok in re.split(r"[,\s]+", m.group(1).strip().lower()):
                if tok in IZIN_KURALLARI:
                    izin.add(tok)
    return izin


def kisit_config_yaz(neden, butce=None, hassas=False, izin=None, yasak=None, go_var=False):
    """Per-run OPENCODE_CONFIG: (a) the model picked from the ladder for sub-agents (if Go is absent or failing),
    (b) build permission extensions if there is an IZIN line. The agent md files have NO model/permission, so the override applies.
    Compaction is not written here: it is the lane's own model (4c-10, `opencode_budget_env`). Returns: (file, selection|None, step)."""
    cfg = {"$schema": "https://opencode.ai/config.json", "agent": {}}
    model, basamak = None, "go"
    if not go_var:
        # B140: not writing a model is not enough; close the inherited default too.
        secim = []
        for k, rol in (("cadre-plan", "plan"), ("cadre-review", "review"), ("cadre-impl", "impl")):
            aday, adim = kisit_model_sec(butce, hassas=hassas, yasak=yasak, rol=rol)
            cfg["agent"][k] = {"model": aday} if aday else {"disable": True}
            secim.append(f"{k}={aday or '-'}")
            if k == "cadre-impl":
                basamak = adim
        model = ",".join(secim)
    if izin:
        bash = {}
        for tok in sorted(izin):
            bash.update(IZIN_KURALLARI.get(tok, {}))
        if bash:
            cfg["agent"]["build"] = {"permission": {"bash": bash}}
    p = task_root(ENV("CADRE_GOREV_ID", "")) / "configs" / f"{uuid.uuid4().hex}.json"
    atomic_json(p, cfg)
    return str(p), model, basamak


def on_plan(real, wt, proje, pf_text, logf, butce, env_ek=None):
    """Engine pre-plan: before the run starts we have the best model on the PLAN LADDER split the task; the briefing is written into the worktree,
    build starts with that briefing and does not ask for another. On failure one step lower is tried; at the very end it is skipped."""
    hafiza = ", ".join(str(QE / "projects" / proje / f) for f in ("PROJECT.md", "POLICY.md") if (QE / "projects" / proje / f).exists()) or "(yok)"
    prompt = PLAN_PROMPT.format(hafiza=hafiza, gorev=pf_text)
    denemeler = []
    if YON is not None and isinstance(butce, dict):
        z, _ = YON.zincir("plan", "orta", 30000, butce, YON.durum_oku())
        denemeler = [("claude" if m.startswith("claude/") else "opencode", m, YON.saglayici(m)) for m in z][:4]
    if not denemeler:
        log(logf, "PLAN: butceli plan modeli yok — motor on-plan atlandi, build kendi planlar")
        return None
    out_dir = task_root(ENV("CADRE_GOREV_ID", "")) / "plan"
    out_dir.mkdir(parents=True, exist_ok=True)
    if (out_dir / "brifing-motor.md").is_file():
        return str(out_dir / "brifing-motor.md")
    for tur, model, bas in denemeler:
        t0 = time.time()
        log(logf, f"PLAN: {bas} -> {model} ile brifing uretiliyor")
        try:
            if tur == "claude":
                proc = run_claude(wt, model, prompt, logf, tools="Read,Grep,Glob,Bash(git log:*),Bash(git show:*),Bash(git diff:*),Bash(ls:*),Bash(cat:*),Bash(sed -n:*)",
                                  budget=ENV("CADRE_CLAUDE_PLAN_BUDGET", "1.0"), turns="25", role="plan")
                wait_process(proc, SINIR.son_sure('plan'), proje)
                out = proc.output()
                claude_maliyet_kaydet(out or "", model, logf, proc.attempt.data if isinstance(proc, RecordedProcess) else None)
                try:
                    j = result_json(out); metin = str(j.get("result") or "")
                except Exception:
                    metin = out or ""
            else:
                output = capture_opencode(real, wt, ["--agent", "cadre-plan", "-m", model, prompt], SINIR.son_sure('plan'), env_ek)
                metin = re.sub(r"\x1b\[[0-9;]*m", "", output)
            if butce_reddi_metni(metin):
                log(logf, f"PLAN: {model} butce reddi (CADRE_BUDGET_DENIED) -> bir alt basamak")   # B177: not counted as a failure, the breaker is not poisoned
                continue
            if len(metin.strip()) < 200 or "MADDELER" not in metin:
                log(logf, f"PLAN: {model} yetersiz cikti ({len(metin)} kar), bir alt basamak")
                if YON: YON.ariza(YON.saglayici(model), "plan yetersiz/hata", model=model)
                continue
            bp = out_dir / "brifing-motor.md"
            atomic_write(bp, f"# BRIFING (motor on-plan) — model: {model} — {time.strftime('%F %T')}\n\n{metin.strip()}\n")
            log(logf, f"PLAN: tamam — {model}, {int(time.time() - t0)} sn, {len(metin)} kar -> {bp}")
            return str(bp)
        except CancelledRun:
            raise
        except Exception as e:
            log(logf, f"PLAN: {model} hata: {e}")
    log(logf, "PLAN: hicbir bulut model brifing uretemedi — build kendi planlar")
    return None


def worktree_base_sha(wt):
    """The base commit this worktree was opened from, from worktrees.log. The record is two lines: 'base=X base_sha=Y' then 'branch=... worktree=<path>';
    the base_sha line does NOT carry the worktree name -> the block is read."""
    try:
        son = ""
        for l in (QE / "logs/worktrees.log").read_text(errors="replace").splitlines():
            if "base_sha=" in l:
                son = l.split("base_sha=")[1].split()[0]
            if "worktree=" in l and l.split("worktree=")[1].strip() == str(wt).rstrip("/"):
                return son
    except Exception:
        pass
    return ""


PLAN_PROMPT = """Sen PLANLAYICISIN (salt-okur). Kod, test, config, git DEGISTIRME. Amacin: asagidaki gorevi bu repoda uygulanabilir,
kabul-kriterli maddelere bolmek. Once proje hafizasini oku: {hafiza}. Sonra repoyu gerektigi kadar incele (git log, dokumanlar, ilgili dosyalar).
CIKTI (Turkce, markdown):
## ANLADIGIM — tek cumle
## MADDELER — JSON kod blogu, `.gorevler.json` formati: [{{"id":"...","baslik":"...","kaynak":"dosya/satir veya dokuman","kabul":"olculebilir kriter","kapsam_disi":"...","bagimli":"...","passes":false}}]
   (3-8 madde; kullanici onayi gerektiren madde varsa baslik basina "ONAY:" yaz)
## TUZAKLAR — repoya ozgu 3-6 madde (kanit: dosya:satir)
## SIRA — hangi madde once, neden
## UYARI — gorev metninde yanlis/imkansiz olan varsa
GOREV:
{gorev}"""


def go_kisit_gerekli(*, in_flight=False, budget=None):
    """Go brake; returns (restriction_needed, budget, reason). `in_flight=True` is for an in-flight call and looks at the provider's periodic
    allocation (does not count reservations, its share is already set aside); `in_flight=False` is for new admission and looks at the run/role pocket (B114-5)."""
    try:
        b = butce_json() if budget is None else budget
        if in_flight:
            return False, b, ''  # GO1: the percentage gate only stops a new request.
        remaining = allowance(b, 'opencode-go')[1] if in_flight else available_budget(QE, b, 'opencode-go')
        return remaining <= 0, b, ('pay-bitti' if remaining <= 0 else '')
    except Exception as exc:
        return True, {}, f'okunamadi:{type(exc).__name__}: {exc}'.strip()


READ_ONLY_BASH = ("git log *", "git diff *", "git show *", "git status *", "git blame *", "ls *", "cat *", "head *", "tail *",
                  "grep *", "wc *", "pwd", "echo *")
READ_ONLY_BASH_DENY = ("*>*", "*--output*")
READ_ONLY_PERMISSION = {"edit": "deny", "bash": {"*": "deny", **{pattern: "allow" for pattern in READ_ONLY_BASH},
                                                 **{pattern: "deny" for pattern in READ_ONLY_BASH_DENY}}}
# B246: there is no approval in -p mode; a read-only command not on the allow-list (`| head`) wastes the round.
CLAUDE_SALT_OKUR_KABUK = "Bash(ls:*),Bash(cat:*),Bash(sed -n:*),Bash(head:*),Bash(tail:*),Bash(rg:*),Bash(grep:*),Bash(find:*),Bash(wc:*),Bash(diff:*)"
CLAUDE_READ_ONLY_TOOLS = ("Read,Grep,Glob,Bash(git log:*),Bash(git show:*),Bash(git diff:*),Bash(git status:*),"
                          "Bash(ls:*),Bash(cat:*),Bash(head:*),Bash(tail:*),Bash(grep:*),Bash(wc:*),Bash(diff:*)")
CLAUDE_YAZAN_TOOLS = "Read,Edit,Write,Grep,Glob,Bash(git:*),Bash(python3:*),Bash(uv:*),Bash(make:*),Bash(pytest:*)," + CLAUDE_SALT_OKUR_KABUK
CLAUDE_WRITE_TOOLS = ("Edit,Write,MultiEdit,NotebookEdit,Bash(git commit:*),Bash(git add:*),Bash(git checkout:*),"
                      "Bash(git reset:*),Bash(git push:*),Bash(rm:*),Bash(mv:*),Bash(cp:*),Bash(find:*),Bash(rg:*),Bash(sed:*),Bash(tree:*)")
# B110-2: secret files are closed to read tools in every Claude run (in review mode and in a normal run).
CLAUDE_SECRET_PATHS = ("Read(./.env),Read(./.env.*),Read(./**/.env),Read(./**/.env.*),Read(./**/*.pem),"
                       "Read(./**/*.key),Read(./**/id_rsa*),Read(./**/id_ed25519*),Read(./**/credentials*),"
                       "Read(./**/.ssh/**),Read(./**/.aws/**)")


def run_record():
    return RunStore(QE).read(ENV("CADRE_RUN_ID")) if ENV("CADRE_RUN_ID") else {}


def project_memory(record):
    """C17: the PROJECT/POLICY/STATE copies from acceptance time go to EVERY model call; if a copy is missing the call does not start."""
    paths = []
    for name, path in (record.get("constraints") or {}).items():
        if name == "qa-komut":
            continue
        if not pathlib.Path(path).is_file():
            raise ValueError(f"project memory missing: {name} ({path})")
        paths.append(str(path))
    return paths


SAGLAYICI_KIMLIKLERI = tuple(sorted({*BUTCE_PAID, *BUTCE_FREE}))   # except claude: it goes through its own transport


def global_opencode_ajanlari():
    """B96: agent names in the global `~/.config/opencode/opencode.jsonc` are overridden in the run config; a sub-agent's provider
    is decided by our budget decision, not by the user's global file."""
    yol = pathlib.Path(os.environ.get('XDG_CONFIG_HOME') or (pathlib.Path.home()/'.config'))/'opencode/opencode.jsonc'
    try:
        metin = re.sub(r'(?m)^\s*//.*$', '', yol.read_text(errors='replace'))
        ajanlar = json.loads(metin).get('agent', {})
        return sorted(k for k in ajanlar if isinstance(k, str)) if isinstance(ajanlar, dict) else []
    except (OSError, ValueError, TypeError):
        return []


def ajan_rolu(ad):
    return next((r for r in ('plan', 'review', 'compaction') if r in ad), 'impl')


def opencode_budget_env(env, args):
    """Every CLI path starts with an empty provider list and the same request gate; it opens no provider without installing the plugin's fetch wrappers,
    so a missing plugin/config cannot silently authorize a paid provider. C17: project memory via `instructions`; C19: writes closed in review mode."""
    budget = butce_json()
    record = run_record()
    # while the global config loaded `cadre-plugin.mjs` (composite), the run config added `butce-plugin.mjs` ->
    # the same gate was installed twice, the header token was not found in the other copy ('request context missing'), no call passed.
    plugin = pathlib.Path(__file__).with_name('cadre-plugin.mjs')
    args = list(args)
    if not args:
        raise ValueError('executor prompt required')
    selected = ''
    options = []
    index = 0
    while index < len(args) - 1:
        option = args[index]
        if option in ('-m', '--model'):
            index += 1
            if index >= len(args) - 1:
                raise ValueError('model option has no value')
            selected = args[index]
        elif option.startswith('--model='):
            selected = option.split('=', 1)[1]
        else:
            options.append(option)
        index += 1
    def uygun(aday, role=None, context=None):
        from butce_kapisi import model_kovasi, USD_PROVIDERS
        sag, _, mid = aday.partition('/')
        if context is not None and (not YON or context > YON.istek_tavani(sag, aday)):
            return False
        if not plugin.is_file() or sag not in SAGLAYICI_KIMLIKLERI:
            return False
        role = role or env.get('CADRE_ROL')
        rid = env.get('CADRE_RUN_ID') or ENV('CADRE_RUN_ID')
        try:
            bucket = model_kovasi(sag, mid)
            if bucket in USD_PROVIDERS:
                remaining = run_remaining_usd(QE, rid, role, bucket)
                if remaining is not None and remaining < float(ENV('CADRE_TAVAN_ASGARI_USD', '0.02')):
                    return False
            return available_budget(QE, budget, bucket, role, rid) > 0
        except Exception:
            return False

    acik_secim = any(o in ('-m', '--model') or o.startswith('--model=') for o in args[:-1])
    if not acik_secim and not selected and YON:
        # P3: an executor without a selected model goes to the first cloud model with budget in the role's chain
        zincir, _ = YON.zincir(env.get('CADRE_ROL', 'impl'), 'orta', None, budget, YON.durum_oku())
        selected = next((m for m in zincir if uygun(m)), '')
    if not selected or not uygun(selected):
        # P3: changing provider/model at the gate hides the session's identity.
        raise BudgetDenied(f'butce-bekleme: {selected} icin dogrulanmis kapasite yok')
    config = {}
    scoped = pathlib.Path(env.get('OPENCODE_CONFIG', '/nonexistent'))
    if scoped.is_relative_to(QE/'runs'):
        config = json.loads(scoped.read_text())
    config.update(json.loads(env.get('OPENCODE_CONFIG_CONTENT') or '{}'))
    config.update(enabled_providers=[], small_model=selected, model=selected)
    if (env.get('CADRE_ROL') or 'impl') in SERIT_ROLLERI:
        # 4c-4: the lane's effective context is min(256k, model context − 20k); opencode triggers compaction at this limit
        from katalog import model as katalog_model
        sag, _, mid = selected.partition('/')
        limit = dict((katalog_model(sag, mid.split('#')[0]) or {}).get('limit') or {})
        if limit.get('context'):
            limit['context'] = SINIR.serit_baglami(int(limit['context']))
            if limit.get('input'):
                limit['input'] = min(int(limit['input']), limit['context'])
            limit.setdefault('output', model_cikti_siniri(sag, mid.split('#')[0]))
            config.setdefault('provider', {}).setdefault(sag, {}).setdefault('models', {}).setdefault(mid, {})['limit'] = limit
    agents = config.setdefault('agent', {})
    bilinen = (('build', 'impl'), ('cadre-plan', 'plan'), ('cadre-impl', 'impl'),
               ('cadre-review', 'review'), ('compaction', 'compaction'))
    # B96: OTHER agent names defined in the global config are also overridden by our decision; otherwise a sub-agent
    # uses the provider from the user's global file and the spending never sees the budget gate.
    ekstra = tuple((ad, ajan_rolu(ad)) for ad in global_opencode_ajanlari()
                   if ad not in {a for a, _ in bilinen})
    primary = options[options.index('--agent') + 1] if '--agent' in options else 'build'
    alt_ajansiz = env.get('CADRE_ALT_AJANSIZ') == '1'   # 4c-9: the dispatcher lane and the judge do not call sub-agents
    for agent, role in bilinen + ekstra:
        existing = agents.get(agent, {}).get('model')
        context = YON.rol_baglami(role) if YON else None
        chain = YON.zincir(role, 'orta', None, budget or {}, YON.durum_oku())[0] if YON else []
        candidates = [m for m in ([existing] if existing else []) + chain]
        candidate = next((m for m in candidates if uygun(m, role, context)), None)
        if agent in ('build', primary, 'compaction'):
            # 4c-10: the lane's own model does the compaction (single source). opencode deletes a `disable`d agent from the registry and
            # automatic compaction reads that agent unconditionally (1.18.31); this also overrides the inherited cloud default (B140).
            candidate = selected
        elif agent == 'cadre-review' or alt_ajansiz:   # 4b: no intermediate review inside a lane; the second pair of eyes is the final independent judge
            candidate = None
        row = agents.setdefault(agent, {})
        if candidate:
            row.update(model=candidate, disable=False)
        else:
            row.pop('model', None)
            row['disable'] = True
    # Plugin RULE: every executor is in the cloud, the request guard is mandatory (B110: the free bucket is counted by the same plugin).
    if True:
        # B124: the plugin may now also be in the GLOBAL config (for the manual `opencode` observation mode). If the same
        # plugin loads twice every call writes TWO reservations and spending is counted twice — dedupe.
        mevcut = [p for p in config.get('plugin', []) if not str(p).endswith(('/cadre-plugin.mjs', '/butce-plugin.mjs'))]
        config['plugin'] = [*mevcut, plugin.as_uri()]
    memory = project_memory(record)
    if memory:
        config['instructions'] = [*config.get('instructions', []), *memory]
    if record:
        run_job_type(record)
        for holder in (config, *agents.values()):
            holder['permission'] = {**holder.get('permission', {}), 'bash': 'allow', 'edit': 'deny', 'read': 'deny',
                                    'glob': 'deny', 'grep': 'deny', 'task': 'deny',
                                    'webfetch': 'deny', 'websearch': 'deny', 'external_directory': 'deny'}
        for agent in agents.values():
            agent['prompt'] = ((agent.get('prompt') or '') + '\nProje dosyalarini yalniz bash araci ile oku ve degistir; '
                'Git metadata, onay kayitlari, ag ve dis dizin yazimi kapalidir. Git veya kurulum izni uretme.')
    env.update(OPENCODE_CONFIG_CONTENT=json.dumps(config), OPENCODE_EXPERIMENTAL_NATIVE_LLM='false',
               CADRE_PYTHON=sys.executable, CADRE_ENGINE_ROOT=str(CODE), CADRE_HOME=str(QE))
    return [*options, '-m', selected, args[-1]]


def model_cikti_siniri(sag, mid):
    try:
        kademeler = json.loads((QE / 'zincir/openrouter-fiyat.json').read_text())['modeller'][mid]['kademeler'].values()
        en_buyuk = max(int(k.get('max_cikti') or 0) for k in kademeler)
        if sag == 'openrouter' and en_buyuk > 0:
            return en_buyuk
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return 2 * SINIR.cikti('serit')


def rol_ortami(env, role):
    """Item 3: the budget gate reads the role from the ENVIRONMENT. The cap a role may spend and the pocket its spending is written to
    come from one decision: `butce_kapisi.rol_butcesi(home, run_id, rol) -> (cap, dilim)`. Per the HARD SPLIT
    each role spends only from its own pocket; lanes cannot eat the whole cap and leave the judge hungry."""
    env['CADRE_ROL'] = role or 'impl'
    return env


SERIT_ROLLERI = ("impl", "qa")   # code-writing roles: the lane's effective context applies to these


def efor_ortami(role):
    """N-TS03 (CR-50): explicit effort policy from the run's recorded selection and the task's error count; default effort if unreadable."""
    try:
        from model_secimi import efor_secimi, kosu_secimi
        run_id, gorev = ENV("CADRE_RUN_ID"), ENV("CADRE_GOREV_ID") or "main"
        task = (RunStore(QE).read(run_id).get("tasks") or {}).get(gorev) or {}
        return efor_secimi(kosu_secimi(QE, run_id), role, int(task.get("failure_count") or 0), gorev)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def run_opencode(real, wt, args, logf, env_ek=None, role="impl"):
    """Start an opencode run; returns a Popen. stdout/err flow to the log file. env_ek: extra environment such as OPENCODE_CONFIG."""
    if iptal_istendi():
        raise CancelledRun('iptal')
    env = rol_ortami(dict(os.environ), role)
    if env_ek:
        env.update(env_ek)
    # --dir is REQUIRED: cwd is not enough; opencode treats the worktree as a sandbox and makes the session directory the MAIN REPO.
    # TASARIM-5: independent cloud lanes run concurrently.
    args = opencode_budget_env(env, args)
    with open(logf, "a") as lf:
        fds = owner_fds()
        if ENV("CADRE_RUN_ID"):
            args = list(args)
            if not args:
                raise ValueError("executor prompt required")
            options = [arg for arg in args[:-1] if arg not in ("-c", "--continue")]
            if any(arg in ("--session", "-s") or arg.startswith("--session=") for arg in options):
                raise ValueError("session selection belongs to the run record")
            model = options[options.index("-m") + 1] if "-m" in options else ""
            efor = efor_ortami(role)
            for key in ('CADRE_EFOR', 'CADRE_EFOR_POLITIKASI'):
                env.pop(key, None)
            if efor:
                env.update(CADRE_EFOR=efor['seviye'], CADRE_EFOR_POLITIKASI=efor['politika'])
            attempt = Attempt("opencode", role, model, args[-1], env.get("OPENCODE_CONFIG", ""),
                              devam_ek=env.get('CADRE_DEVAM_EK', ''), devam_zorunlu=env.get('CADRE_YOL_DEVAM') == '1')
            env['CADRE_BUDGET_ATTEMPT'] = attempt.data['attempt_id']
            env['CADRE_REQUEST_DIR'] = str(attempt.directory / 'requests')
            try:
                ajanlar = json.loads(env.get("OPENCODE_CONFIG_CONTENT") or "{}").get("agent") or {}
            except ValueError:
                ajanlar = {}
            # The helper map goes into the attempt record: which model is left out after a stall is read from here (§0a/10b).
            attempt.save(agents={ad: (None if v.get("disable") else v.get("model"))
                                 for ad, v in ajanlar.items() if isinstance(v, dict)}, efor_politikasi=efor)
            if efor:
                RunStore(QE).task_update(ENV("CADRE_RUN_ID"), env.get("CADRE_GOREV_ID") or "main", efor_politikasi=efor)
            options = OC.session_options(options, session_id=attempt.data['session_id'] if attempt.resuming else '',
                                         title=attempt.data['title'])
            args = [*options, "--", attempt.prompt]
            if iptal_istendi():   # G-C9: a cancel that arrives while the attempt is being prepared (B278 lock wait) does not start the executor
                raise CancelledRun("cancelled before executor launch")
            return attempt.launch(OC.command(real, str(wt), args, json_events=True),
                                  str(wt), pathlib.Path(logf), env, fds)
        return OC.launch(OC.command(real, str(wt), args), cwd=wt,
                                stdout=lf, stderr=subprocess.STDOUT, env=env,
                                pass_fds=fds)


class ModelOutput(str):
    """Identity and attempt record of the model run AFTER the budget gate, together with the text."""

    model: str

    def __new__(cls, text: str, model: str, attempt=None):
        value = super().__new__(cls, text)
        value.model = model
        value.attempt = attempt
        return value


def budget_denial_receipt(proc):
    if not isinstance(proc, RecordedProcess):
        return None
    path = proc.attempt.directory/'budget-denied.json'
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def yardimci_reddi_mi(receipt, ana_sag):
    """B231: is the denial only for providers OTHER than the main model? A pocket (role) denial or a denial without a provider covers the main model too."""
    satirlar = (receipt or {}).get('refusals') or []
    return bool(satirlar) and all(r.get('scope') != 'role' and r.get('provider') and r.get('provider') != ana_sag for r in satirlar)


GECICI_RET = ('pricing unavailable', 'pricing unknown', 'ledger refused or unavailable', 'request context missing')


def apply_budget_denial(bans, receipt, snapshot):
    """The ledger enforces the role pocket; this denial does not close the provider or the other pockets."""
    dilimler, gecici = {}, set()
    if receipt.get('refusals'):
        rows = [row for row in receipt['refusals'] if row.get('scope') != 'role' and not saglayici_bekleme_reddi({'refusals': [row]})]
        providers = {row['provider'] for row in rows if row.get('provider')}
        # a run POCKET denial (the line has `dilim`) is not a provider share denial; it is written pocket-stamped so it is re-checked when the cap
        # rises (P6) or after recheck. An unstamped ban only opened when the share renewed -> the job never started.
        dilimler = {row['provider']: row.get('dilim') for row in rows if row.get('provider') and row.get('dilim')}
        if rows and not providers:   # G-B5 (B154): a provider denial without a provider -> cautious fallback, not a silent pass
            providers = {*BUTCE_PAID, *BUTCE_FREE, 'claude'}
        gecici = {p for p in providers if (kendi := [row for row in rows if row.get('provider') == p])
                  and all(any(g in str(row.get('reason') or '') for g in GECICI_RET) for row in kendi)}
    else:
        providers = receipt.get('providers') or (*BUTCE_PAID, *BUTCE_FREE, 'claude')
    for provider in providers:
        bans.block(provider, 'unavailable' if provider in gecici else 'budget', snapshot, dilim=dilimler.get(provider))


def sure_sonu(kalan_sn):
    """N-TS01 (CR-48): wall-clock deadline read by the provider gate; endpoint selection does not prefer a route that does not fit the time."""
    return repr(time.time() + max(0.0, float(kalan_sn)))


def capture_opencode(real, wt, args, timeout, env_ek=None, role="plan"):
    """Bounded helper call through the same executor lock as the main worker."""
    review = BudgetLedger(QE / 'zincir/request-budget.db').begin_review(
        ENV('CADRE_RUN_ID'), ENV('CADRE_GOREV_ID', ''), role)
    review_deadline = min(review['deadline'], time.time() + timeout) if review else None
    if review:
        rule = review['rule']
        notice = (f"INCELEME SINIRI: {review['phase']}; onceki deneme ve onarimlar dahil kalan "
                  f"{rule['istek'] - review['calls']} model istegi, {rule['arac'] - review['tools']} arac cagrisidir. "
                  f"Faz toplam maliyet tavani her ayri butce havuzunda ${review['limit'] / 1e6:.6f}; "
                  f"kalan sure en fazla {max(0, int(review_deadline - time.time()))} sn. "
                  "Secili kapsami kisa ve dogrudan kanitla incele; eksik kaniti uydurma.\n")
        if review['phase'] in ('plan-review', 'review-final', 'review-recheck') and rule['arac'] - review['tools'] <= 0:
            raise BudgetDenied(f"review limit: {review['phase']}: tools")
        first, separator, rest = args[-1].partition('\n')
        args = [*args[:-1], first + ' ' + notice.strip() + separator + rest]
    output = task_root(ENV("CADRE_GOREV_ID", "")) / f"output-{uuid.uuid4().hex}.log"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("")   # empty file: a mock run_opencode may not open the file (for test fixtures)
    env_ek = dict(env_ek or {})
    yollar = set()
    while True:
        remaining = min(timeout, review_deadline - time.time()) if review_deadline is not None else timeout
        if remaining <= 0:
            raise BudgetDenied('review limit: time')
        env_ek['CADRE_SURE_SON_TS'] = sure_sonu(remaining)
        proc = run_opencode(real, wt, args, output, env_ek, role=role)
        model = proc.attempt.data['model'] if isinstance(proc, RecordedProcess) else ''
        stalled = kesik = False
        try:
            wait_timeout = max(0, min(remaining, review_deadline - time.time())) if review_deadline is not None else remaining
            rc = wait_process(proc, wait_timeout, wt=wt, logf=output)
        except SessionLoop:
            if not isinstance(proc, RecordedProcess):
                raise
            task = proc.attempt.store.read(proc.attempt.run_id)['tasks'].get(proc.attempt.task_id, {})
            if task.get('dongu_yonlendirildi'):
                raise RuntimeError('KALDI: yonlendirmeden sonra arac dongusu')
            proc.attempt.store.task_update(proc.attempt.run_id, proc.attempt.task_id, dongu_yonlendirildi=True)
            env_ek.update(CADRE_YOL_DEVAM='1', CADRE_DEVAM_EK='\nAyni arac cagrisini tekrarlama; farkli bir yol dene.')
            args = ['-m', model, args[-1] + env_ek['CADRE_DEVAM_EK']]
            continue
        except SessionStalled:
            stalled, rc = True, 1
        refusal = budget_denial_receipt(proc)
        raw = proc.output() if isinstance(proc, RecordedProcess) else output.read_text(errors='replace')
        if isinstance(proc, RecordedProcess) and not stalled and rc == 0:
            ozet = cikti_denetimi(proc, model, output, role)   # 4c-4: in a helper call too, a truncation continues once with 2×
            kesik = ozet["finish"] == "length"
            if kesik and env_ek.get('CADRE_CIKTI_KAT') != '2':
                env_ek.update(CADRE_CIKTI_KAT='2', CADRE_YOL_DEVAM='1', CADRE_DEVAM_EK=KESIK_CEVAP)
                continue
        limit = model.startswith('opencode-go/') and (refusal is not None or rc == 75 or
            (rc != 0 and re.search(r'\b429\b|rate.?limit|quota|allocation exhausted', raw, re.I)))
        if not stalled and not limit:
            break
        yollar.add(YON.saglayici(model))
        aday = yol_devret(model, output, 'takildi' if stalled else 'Go limit', role, yollar, model_devri=False)
        if not aday:
            raise BudgetDenied('butce-bekleme: ayni model icin acik yol yok')
        env_ek.pop('CADRE_CIKTI_KAT', None)   # 2× only on the truncated call's own route (4c QA2)
        args = ['-m', aday, args[-1]]
        env_ek['CADRE_YOL_DEVAM'] = '1'
    if refusal is not None or rc == 75:
        kesinti = saglayici_bekleme_reddi(refusal)
        if kesinti:
            if review and isinstance(proc, RecordedProcess):
                attempt = proc.attempt
                try:
                    receipt = bounded_json(attempt.directory / 'process.json')
                except (OSError, ValueError):
                    receipt = {}
                identity = receipt.get('process_identity')
                if (identity and receipt.get('stop_observed') == identity
                        and re.fullmatch('[a-f0-9]{64}', str(receipt.get('scope_digest') or ''))
                        and attempt.run_id == ENV('CADRE_RUN_ID') and attempt.task_id == ENV('CADRE_GOREV_ID')
                        and all(receipt.get(k) == v for k, v in (('run_id', attempt.run_id), ('task_id', attempt.task_id),
                                                               ('attempt_id', attempt.data['attempt_id'])))):
                    try:
                        stopped = retire_owned_tools(attempt.store, attempt.path, attempt.data)
                    except (OSError, ValueError):
                        stopped = False
                    if stopped:
                        # N-RUN04: the close of the owned area is confirmed through the existing
                        # cleanup path; only the phase clock stops, the request/money record is kept.
                        BudgetLedger(QE / 'zincir/request-budget.db').pause_review(attempt.run_id, attempt.task_id, role)
            raise SaglayiciBekleme(f'saglayici-bekleme: {kesinti[0]} until {kesinti[1]:.0f}')
        raise BudgetDenied('executor request refused by budget gate')
    if rc:
        raise subprocess.CalledProcessError(rc, proc.args)
    if kesik:
        raise RuntimeError(f'output-truncated: {model} yaniti etkin cikti tavaninda kesildi')
    if isinstance(proc, RecordedProcess):
        text_events = []
        for line in proc.output().splitlines():
            try:
                event = json.loads(line)
                if event.get("type") == "text":
                    text_events.append(event.get("part", {}).get("text", ""))
            except (ValueError, AttributeError):
                continue
        return ModelOutput("\n".join(text_events), proc.attempt.data['model'], proc.attempt)
    return output.read_text(errors="replace")


def progress_watch(wt, logf, proc, warn, abort_s, poll):
    """Warn/cut if there is no artefact (file change).
    Returns: 1 = ABORT cut (guard), 0 = the process ended by itself."""
    canlilik = Canlilik(logf, proc, wt)
    while proc.poll() is None:
        wait_pause(min(poll, 60))
        if canlilik.takildi():
            log(logf, "BEKCI: tum canlilik isaretleri durdu; ayni oturumla devam gerekli")
            durdur(proc)
            return 1
    return 0


try:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import yonlendirici as YON  # Stage 10: a single router
except Exception:
    YON = None
OC_LOG = pathlib.Path(os.environ.get("CADRE_OC_LOG", os.path.expanduser("~/.local/share/opencode/log/opencode.log")))  # override for test injection
# 4c-17 QA: account signatures (401/402/insufficient balance) are samples too; otherwise the classifier never sees them and the provider does not close
HATA_KALIP = re.compile(r"insufficient (?:credits?|balance|funds)|payment required|invalid api key|unauthorized|\b40[12]\b|"
                        r"Request too large|context.?length|maximum context|too many tokens|exceeded your current quota|"
                        r"Rate limit reached|\b429\b|\b413\b|Cannot connect to API", re.I)
LIMIT_IMZASI = re.compile(r'quota|rate.?limit|insufficient|exceeded|max_budget|budget asildi|too large|context.?length|maximum context|too many tokens|Cannot connect|(?:status(?:code)?|code|http|error)["\'\s:=]*(?:40[12]|413|429)\b|\b40[12]\s+(?:unauthorized|payment|forbidden)|payment required|unauthorized|invalid api key', re.I)


def wt_oturumlari(wt):
    """opencode session ids opened in this worktree (DB read-only). The detector counts only THIS job's errors;
    in parallel cloud lanes one lane blowing up must not cut another."""
    try:
        import sqlite3
        db = pathlib.Path(ENV("OPENCODE_DB", os.path.expanduser("~/.local/share/opencode/opencode.db")))
        roots = None
        if ENV("CADRE_RUN_ID"):
            roots = session_ids(RunStore(QE).read(ENV("CADRE_RUN_ID")), ENV("CADRE_GOREV_ID", "") or "main")
        if not db.exists():
            return roots or set()
        with contextlib.closing(OC.connect(db)) as con:   # B142b: closes on exception too
            con.row_factory = sqlite3.Row
            # The recorded root session receipt is authoritative: even if the DB row is not written yet (or is in another DB) this job's errors are counted.
            ids = (set(roots) | {r["id"] for r in session_rows(con, roots)}) if roots is not None else {r[0] for r in con.execute("select id from session where directory = ? or directory like ?", (str(wt), str(wt).rstrip("/") + "/%"))}
        return ids
    except Exception:
        return set()


def hata_patlamasi(pencere_sn=180, esik=6, oturumlar=None, baslangic=0):
    """Error-loop detector: on a permanent error (request too large / quota / rate limit) opencode enters a compaction->retry->error loop.
    ERROR lines within the last `pencere_sn` of opencode.log are counted; if over the threshold a sample error is returned."""
    try:
        if not OC_LOG.exists():
            return None
        with OC_LOG.open("rb") as f:
            f.seek(max(0, OC_LOG.stat().st_size - 200_000))
            satirlar = f.read().decode(errors="replace").splitlines()
        simdi = time.time()
        n, ornek, prov, model = 0, "", "", ""
        for l in satirlar:
            # 4c-17: the header agent and a budget denial are not a provider/model failure
            if "level=ERROR" not in l or "agent=title" in l or "CADRE_BUDGET_DENIED" in l:
                continue
            if oturumlar is not None:
                match = re.search(r"(?:session\.id|sessionID|session_id)=(\S+)", l)
                if not match or match.group(1) not in oturumlar:
                    continue  # No identity is not permission to blame another task.
            m = re.match(r"timestamp=(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})", l)
            if not m:
                continue
            ts = time.mktime(time.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M:%S")) - time.timezone
            if ts + 1 > baslangic and simdi - ts <= pencere_sn:   # the log stamp is truncated to the second: the start second counts too
                n += 1
                mm = HATA_KALIP.search(l)
                if not ornek:   # the full sample error text: the classifier sees "402" with its context (4c QA2)
                    kod = re.search(r'error\.error\.code=(\d+)', l)
                    em = re.search(r'error\.error(?:\.message)?="([^"]{1,300})', l)
                    ornek = " ".join(x for x in (f"status {kod.group(1)}" if kod else "", em.group(1) if em else "") if x) or (mm.group(0) if mm else "bilinmeyen")
                    pm = re.search(r"providerID=(\S+)", l)
                    if pm:
                        prov = pm.group(1)  # the failing provider
                        mm = re.search(r"modelID=(\S+)", l)
                        model = mm.group(1) if mm else ""
        return (n, ornek or "bilinmeyen", prov, model) if n >= esik else None
    except Exception:
        return None


KABUK_KOMUTU = re.compile(r"^(/\S*/)?(ba|z|da)?sh (-\w+ )*-\w*c ")   # opencode bash tool; LSP servers start directly


def arac_calisiyor(pid, esik_cpu=3.0):
    """Is any opencode child process consuming CPU? During a long tool call like `make check`/pytest
    the model is IDLE and stdout is SILENT; mistaking that for a stall and cutting sends the job into endless repetition.
    Idle helpers like LSP do not count (CPU ~0)."""
    try:
        kuyruk = [str(pid)]; torun = []
        while kuyruk:
            out = subprocess.run(["pgrep", "-P", kuyruk.pop()], capture_output=True, text=True).stdout.split()
            torun += out; kuyruk += out
        for c in torun:
            cpu = subprocess.run(["ps", "-o", "%cpu=", "-p", c], capture_output=True, text=True).stdout.strip()
            if cpu and float(cpu.replace(",", ".")) >= esik_cpu:
                return True
            # B342: a long test suite sleeps most of the time (%CPU<3); a live shell command (tool) is still running
            komut = subprocess.run(["ps", "-o", "command=", "-p", c], capture_output=True, text=True).stdout.strip()
            if KABUK_KOMUTU.match(komut):
                return True
    except Exception:
        pass
    return False


OLUMSUZ_ISARET = re.compile(r"^(ONAY|BLOKE)\s*:\s*(yok|gerekmez|gereksiz|hay[iı]r|no|none|de[gğ]il)\b", re.I)


def _olumsuz(metin):
    """T1v3: the planner wrote "ONAY: yok | ..." (meaning NO approval is needed) — this is not an approval/block marker."""
    return bool(OLUMSUZ_ISARET.match(str(metin or "").strip()))


def urun_degisti_mi(wt, source_sha=None):
    """Did the product change? The working tree is dirty OR there is a committed diff since the source SHA (an agent/checkpoint commit hides the product
    from `git status` — T1v3). Harness files (URUN_HARIC) do not count. True if it cannot be measured (fail-safe: do not say approval)."""
    try:
        if urun_kirli(wt):
            return True
        if source_sha:
            out = subprocess.run(["git", "-C", str(wt), "diff", "--name-only", source_sha, "--", ".", *URUN_HARIC],
                                 capture_output=True, text=True, timeout=60)
            return bool(out.stdout.strip()) or out.returncode != 0
        return False
    except Exception:
        return True


def onay_blokesi(wt):
    """G-C13: if no workable item remains in `.gorevler.json` (all `BLOKE: ...`), returns the first ONAY question; otherwise "".
    23.09 T1: the executor marked all items BLOCKED and edited the product for 40 min — the decision is the user's, the run does not wait."""
    try:
        maddeler = json.loads((pathlib.Path(wt) / ".gorevler.json").read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(maddeler, list) or not maddeler or not all(isinstance(m, dict) for m in maddeler):
        return ""
    def _bloke(m):
        return any(str(m.get(k) or "").strip().upper().startswith("BLOKE") and not _olumsuz(m.get(k))
                   for k in ("status", "durum", "state", "passes"))
    if not all(_bloke(m) for m in maddeler):
        return ""
    for m in maddeler:
        if str(m.get("baslik") or "").strip().upper().startswith("ONAY"):
            return str(m["baslik"]).strip()
    return str(maddeler[0].get("baslik") or "onay bekleyen madde").strip()


def brifing_onay_sorusu(bp):
    """G-C13 second trigger: the first item starting with "ONAY:" in the briefing's MADDELER JSON; "" if none/unreadable."""
    try:
        metin = pathlib.Path(bp).read_text(encoding="utf-8")
        m = re.search(r"## MADDELER.*?```json\s*(\[.*?\])\s*```", metin, re.S)
        maddeler = json.loads(m.group(1)) if m else []
    except Exception:
        return ""
    for md in maddeler if isinstance(maddeler, list) else []:
        if isinstance(md, dict) and str(md.get("baslik") or "").strip().upper().startswith("ONAY") and not _olumsuz(md.get("baslik")):
            return str(md["baslik"]).strip()
    return ""


def onay_karari(wt, bp, urun_degisti=None, source_sha=None):
    """G-C13: is the run 'waiting for approval'? (1) all items BLOCKED in the executor record -> question (even if the product changed);
    (2) the executor exited with rc=0, the PRODUCT DID NOT CHANGE and the briefing has an ONAY item -> question. Otherwise ""."""
    soru = onay_blokesi(wt)
    if soru:
        return soru
    if urun_degisti is None:
        urun_degisti = urun_degisti_mi(wt, source_sha)  # T1v3: a committed product is also a change
    return "" if urun_degisti else brifing_onay_sorusu(bp)


def butce_reddi_metni(metin):
    """B177: the Claude transport returns a budget denial as plain text ("403 CADRE_BUDGET_DENIED");
    the plan path counted it as "insufficient output" and triggered the Claude breaker, the judge path mislabelled it. A denial is not a failure."""
    m = str(metin or "").strip()
    try:
        m = str(result_json(m).get("result") or m).strip()   # in the Claude transport JSON the verdict field is 'result'
    except Exception:
        pass
    return bool(m) and "CADRE_BUDGET_DENIED" in m and len(m) <= 400   # B187: a long text (plan) may contain the phrase; a transport denial is short


AKTIF_WT = None  # active worktree while the engine runs (for the kota_dusur context measure)


def oturum_baglam_token(wt):
    """Context size of the last opencode session in this worktree (input+cache.read tokens of the last assistant message). 0 = unknown.
    The chain is filtered by this measure (a session is not moved to a provider that cannot hold the context)."""
    if not wt:
        return 0
    try:
        import sqlite3
        db = pathlib.Path(os.path.expanduser(os.environ.get("OPENCODE_DB", "~/.local/share/opencode/opencode.db")))
        with contextlib.closing(OC.connect(db)) as con:   # B142b
            r = con.execute("""select coalesce(json_extract(m.data,'$.tokens.input'),0) + coalesce(json_extract(m.data,'$.tokens.cache.read'),0)
                               from message m join session s on s.id = m.session_id
                               where s.directory = ? and json_extract(m.data,'$.role')='assistant' and json_extract(m.data,'$.tokens.input') is not null
                               order by m.time_created desc limit 1""", (str(wt).rstrip("/"),)).fetchone()
        return int(r[0]) if r and r[0] else 0
    except Exception:
        return 0


def istek_olcusu(wt=None, taban=30000):
    """Request size for chain selection: session context x1.2 (for new context), at least the floor."""
    return max(taban, int(oturum_baglam_token(wt or AKTIF_WT) * 1.2))


DEVIR_SERIT_TAVAN = int(ENV("CADRE_DEVIR_SERIT", "2"))   # Item 2: a lane switches provider at most 2 times
DEVIR_KOSU_TAVAN = int(ENV("CADRE_DEVIR_KOSU", "5"))     # ... and a run 5 times in total


def devir_sayaci(artir=False):
    """(lane_handoffs, run_handoffs). Item 2: today's only expensive event was a LOOP — the same job was handed from provider to provider
    and restarted from scratch on each hand-off, burning $1.15 (22 medium jobs). The counter is written to disk: it is not reset even if the engine
    crashes and restarts. Without a record (call outside a run) the counter does nothing and returns (0,0) — the old behaviour."""
    run = ENV("CADRE_RUN_ID", "")
    if not run:
        return (0, 0)
    gorev = ENV("CADRE_GOREV_ID", "") or "main"
    yol = QE / "runs" / run / "devir.json"
    try:
        from kosu import FileLock
        yol.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(yol.with_suffix(".lock")):
            try:
                veri = json.loads(yol.read_text())
            except (OSError, ValueError):
                veri = {}
            seritler = veri.setdefault("seritler", {})
            if artir:
                seritler[gorev] = int(seritler.get(gorev, 0)) + 1
                veri["kosu"] = int(veri.get("kosu", 0)) + 1
                # B110: there was NO fsync here — `write_text` + `replace` could leave an EMPTY or half file
                # on a crash/power cut and the hand-off counter was reset (the very reason the counter exists
                # is that the hand-off cap must survive an engine crash and restart). atomic_json fsyncs.
                from kosu import atomic_json
                atomic_json(yol, veri)
            return int(seritler.get(gorev, 0)), int(veri.get("kosu", 0))
    except OSError:
        return (0, 0)


def yol_devret(mevcut, logf, neden="", rol="impl", yasak=(), model_devri=True, bekleyen=False):
    """First the same model and session continue via another route (not counted as a hand-off); if the same model has no route, the next model in the chain
    (through the hand-off counter, 4c-17). If none, '' and `yol_bekleyen` = current (the budget is awaited). A judge/plan helper
    call does not change model (`model_devri=False`): the independence selection is in kabul.py."""
    zincir_rolu = 'impl' if rol == 'qa' else rol
    nefes = getattr(YON, 'hakem_nefesi' if zincir_rolu == 'kalite' else 'yazar_nefesi', None) if YON else None
    b = butce_json() if nefes else None
    yetmez = (lambda m: bool(b) and m != mevcut and nefes(m, b) is not None)
    yollar = YON.ayni_model_yollari(mevcut, zincir_rolu, istek_olcusu(), yasak=yasak, kendisi=bekleyen) if YON else []
    yollar = [y for y in yollar if not yetmez(y)]
    aday, tur = (yollar[0], 'yol') if yollar else ('', '')
    if not aday and YON and model_devri:
        sonraki = YON.sonraki_model(mevcut, zincir_rolu, istek_olcusu(), yasak=yasak) or ''
        if sonraki and yetmez(sonraki):
            sonraki = ''
        if sonraki:
            serit, kosu_devri = devir_sayaci()
            if serit >= DEVIR_SERIT_TAVAN or kosu_devri >= DEVIR_KOSU_TAVAN:
                log(logf, f"DEVIR SINIRI (serit {serit}/{DEVIR_SERIT_TAVAN}, kosu {kosu_devri}/{DEVIR_KOSU_TAVAN}): {mevcut} -> "
                          f"{sonraki} yapilmadi; butce-bekleme")
            else:
                devir_sayaci(artir=True)
                aday, tur = sonraki, 'model'
    if ENV("CADRE_RUN_ID"):
        def kaydet(task):
            task.setdefault('yol_devirleri', []).append({'model': mevcut, 'sonraki': aday,
                'neden': neden, 'ts': time.time(), 'sayildi': tur == 'model', 'tur': tur or 'bekleme'})
            task['yol_bekleyen'] = '' if aday else mevcut
        try:
            RunStore(QE).task_mutate(ENV("CADRE_RUN_ID"), ENV("CADRE_GOREV_ID") or "main", kaydet)
        except (OSError, ValueError, KeyError) as exc:   # an unrecorded caller (helper, unit) still gets the hand-off
            log(logf, f"devir kaydi yazilamadi: {type(exc).__name__}")
    if tur == 'model':
        log(logf, f"MODEL DEVRI (sonraki modele devredildi, sayildi): {mevcut} -> {aday}; {neden}")
    else:
        log(logf, f"YOL DEVRI (sayilmayan): {mevcut} -> {aday or 'butce-bekleme'}; ayni oturum; {neden}")
    return aday


KESIK_CEVAP = "\nBitmemis adim son cevabin: cikti sinirinda kesildi. Son cevabini bastan, eksiksiz ve tek parca yeniden yaz."


def patlama_arizasi_mi(saglayici, hata):
    return not (saglayici == "opencode-go" and YON is not None and YON.hata_sinifi(hata) == "kota")


def kota_dusur(mevcut, logf, neden="", hata=""):
    """Main model quota/limit/failure: the (model, route) failure is written, first another route of the same model, otherwise the next model in the chain
    (yol_devret, 4c-17); when the chain ends, budget-wait (''). Share/window exhaustion and a budget denial are not failures (B52).
    B69: if the reason is the RUN CAP, paid routes are skipped — they all hit the same cap."""
    butce_nedeni = neden in ("kosu-tavani", "pay", "butce")
    tavan_doldu = neden in ("kosu-tavani", "butce")
    sinif = YON.hata_sinifi(hata) if (YON and hata) else ""
    # on Go the limit signature is the window wall (GO1): no failure is written, and since the pool is shared there is no switch to another Go model (4c QA)
    go_penceresi = mevcut.startswith('opencode-go/') and not neden and sinif != "hesap"
    if YON and mevcut and not butce_nedeni and not go_penceresi:
        try:   # the Claude quota is account-level; for the others (model, route)
            YON.ariza(YON.saglayici(mevcut), hata or "kota/limit", model=None if mevcut.startswith("claude/") else mevcut)
        except Exception:
            pass
    if not butce_nedeni and mevcut.startswith("claude/fable"):
        return "claude/opus#high"
    if not butce_nedeni and mevcut.startswith("claude/opus"):
        return "claude/sonnet#high"
    if YON and tavan_doldu:
        z, _ = YON.zincir("kurtarici", "orta", istek_olcusu())   # a provider whose context does not fit (istek_tavan) does not enter the chain
        # B333/G-B8: filtering by bucket — a free bucket (<prov>:free) does not enter the fill cap, a paid one is filtered.
        from katalog import bedava_mi

        def _bedava(m):
            try:
                return bedava_mi(YON.saglayici(m), m.split("/", 1)[1].split("#")[0]) is True
            except Exception:
                return False

        def _kova(m):
            return YON.saglayici(m) + (":free" if _bedava(m) else "")

        z = [m for m in z if not m.startswith("claude/") and _kova(m) != _kova(mevcut)
             and (YON.saglayici(m) not in USD_SAGLAYICILAR or _bedava(m))]
        nxt = z[0] if z else ""
        log(logf, f"KOTA/LIMIT ({mevcut}, neden={neden}) -> {nxt or 'butce-bekleme'} (yonlendirici)")
        return nxt
    if YON and mevcut and (neden == "pay" or mevcut.startswith("claude/")):
        # share exhausted / Claude ladder exhausted: filtering at provider level (T3); on Go only another route of the same model (GO1)
        return yol_devret(mevcut, logf, neden or "kota/limit", yasak=(YON.saglayici(mevcut),),
                          model_devri=not mevcut.startswith("opencode-go/"))
    if YON and mevcut:
        return yol_devret(mevcut, logf, neden or "kota/limit", model_devri=not (go_penceresi and sinif in ("kota", "")))
    log(logf, "KOTA/LIMIT algilandi -> butce-bekleme")
    return ""


def kurtarici_sec(yasak=()):
    """RESCUER candidate (C8/C10): the router's 'kurtarici' chain × task bans × the shared budget gate; except
    claude. If the budget is unknown the chain comes back empty -> '' (the all-exhausted contract is not broken). A single decision point."""
    return bulut_aday("kurtarici", yasak)


def butce_bekleme_kaydet(logf):
    if ENV('CADRE_RUN_ID'):
        store = RunStore(QE)
        now = time.time()
        waits = store.read(ENV('CADRE_RUN_ID')).get('budget_waits')
        waits = [w for w in waits if isinstance(w, dict)] if isinstance(waits, list) else []
        store.update(ENV('CADRE_RUN_ID'), reason='butce-bekleme', retry_after=now + 60, budget_waits=[*waits, {'ts': now, 'until': now + 60}][-200:])
    log(logf, 'BUTCE BEKLEME: acik bulut kapasitesi yok; oturum ve urun korunuyor')


def butce_json():
    """Shared fresh observation. Unknown does not preserve a stale authorization."""
    try:
        return observed_budget(QE, CODE)
    except Exception:
        return None


def saglayici_bugun(prov):
    """Today's (local day) spending and request count from the opencode DB: (usd, requests). Light; can be called on every tick."""
    try:
        import datetime
        db = pathlib.Path(os.path.expanduser(os.environ.get("OPENCODE_DB", "~/.local/share/opencode/opencode.db")))
        since = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        measured = spending(db, prov, since)
        if measured is None:
            return None, None
        usd, n = measured
        return float(usd or 0), int(n or 0)
    except Exception:
        return None, None


USD_SAGLAYICILAR = ('opencode-go', 'opencode', 'claude', 'openrouter')   # free providers count requests and do not enter the run cap


def saglayici_freni(b, yasak=()):
    """Running-call brake uses admission policy, excluding held reservations.
    B57: the brake also works if the run-wide cap fills mid-job (so sub-agents/compaction do not get a 403 on every request)."""
    run_id = ENV("CADRE_RUN_ID")
    if run_id:
        kalan, neden = None, 'kosu tavani doldu'
        remaining_by_provider = {}
        try:
            remaining_by_provider = {provider: run_remaining_usd(QE, run_id, provider=provider)
                                     for provider in USD_SAGLAYICILAR if provider not in yasak}
            values = [v for v in remaining_by_provider.values() if v is not None]
            kalan = min(values) if values else None
        except BudgetDenied as exc:  # B70 (T2): B58's fail-closed behaviour was swallowed by a broad except (the brake lifted silently)
            kalan, neden = 0.0, f'kosu tavani okunamadi: {exc}'
        except Exception as exc:
            # B114-9: there was `kalan = None` here; because of the `kalan is not None` condition below the run
            # cap brake was lifted SILENTLY. An unreadable cap counts as zero, the reason is recorded.
            kalan, neden = 0.0, f'kosu tavani okunamadi: {exc}'
        if kalan is not None and kalan <= 0:
            # B110: this list was hand-written and PAID ZEN ('opencode') was NOT in it; when the run cap
            # was full zen stayed free. Single source USD_SAGLAYICILAR (0c: zen is paid too).
            for provider in USD_SAGLAYICILAR:
                if provider not in yasak and (not remaining_by_provider or remaining_by_provider.get(provider) == 0):
                    return provider, neden
    for provider in ('opencode',):
        if provider in yasak:
            continue
        if provider == 'opencode' and not (isinstance(b, dict) and b.get('zen')):
            continue        # without a zen observation zen is not in use; no brake is written for a provider that does not exist
        try:
            if allowance(b, provider)[1] <= 0:
                return provider, 'dogrulanmis pay bitti'
        except BudgetDenied:
            return provider, 'butce bilinmiyor'
    return None


def serit_ilerlemesi(gorev_id):
    """N-BU06 (FIX-53): a producing lane is a recorded step; if the product digest cannot be read it is written with the task id."""
    try:
        from butce_kapisi import ilerleme_yaz, urun_kimligi
        kayit = RunStore(QE).read(ENV("CADRE_RUN_ID"))
        ozet = urun_kimligi({**kayit, 'worktree': ENV("CADRE_WORKTREE") or kayit.get('worktree')}) or 'urun-yok'
        ilerleme_yaz(QE, ENV("CADRE_RUN_ID"), 'serit-uretti', f"{gorev_id or 'main'}:{ozet}")
    except (OSError, ValueError, KeyError, ImportError):
        pass


def qe_durum(gorev_id, durum, neden=""):
    if not gorev_id and not ENV("CADRE_RUN_ID"):
        return
    if ENV("CADRE_RUN_ID"):
        RunStore(QE).task_update(ENV("CADRE_RUN_ID"), gorev_id or "main", state=durum, reason=neden)
        if durum == "uretti":
            serit_ilerlemesi(gorev_id)
        p = task_root(gorev_id) / "status.txt"
    else:
        p = QE / "zincir/calisan" / f"{gorev_id}.durum"
    atomic_write(p, f"{durum} {neden} {int(time.time())}\n")


def commit_wip(wt, proje, mesaj):
    """qe_commit_wip bridge: commit intermediate work (only cadre/* branches, the guard is in common.sh)."""
    try:
        r = subprocess.run(["bash", "-c", '. "$1/lib/common.sh"; qe_commit_wip "$2" "$3" "$4"',
                            'cadre-checkpoint', str(CODE), str(wt), str(proje), str(mesaj)],
                           capture_output=True, text=True, timeout=60)
        return r.stdout.strip()
    except Exception:
        return ""


def qe_otopsi(wt, proje, logf, sebep, real):
    if "CADRE_REAL_OPENCODE" in os.environ:  # in a fake run the autopsy = 300 s of waiting in vain (the cause of the 306 s in E1a/E3a)
        log(logf, f"otopsi atlandi (sahte opencode, sebep={sebep})")
        return
    """Is birakilinca modelin KENDI basarisizligini analizi (bounded, best-effort)."""
    out = pathlib.Path(wt) / "var/audit" / f"OTOPSI-{time.strftime('%H%M%S')}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        kuyruk = "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", l) for l in pathlib.Path(logf).read_text().splitlines()
                           if l.strip().startswith(("$", "→", "✱")))[-3000:]
        soru = (f"Bir gorevde takildin ({sebep}) ve is birakildi. KENDI basarisizligini analiz et, "
                f"5 satiri gecme, TURKCE: 1) Neye takildin? 2) Neden? 3) Sonraki denemede FARKLI ne yapmali?\n\nSON HAMLELER:\n{kuyruk}")
        result = capture_opencode(real, wt, [soru], 300, role="otopsi")
        if result.strip():
            out.write_text(result)
            log(logf, f"OTOPSI yazildi: {out}")
        else:
            log(logf, "otopsi uretilemedi (atlandi)")
    except Exception:
        log(logf, "otopsi uretilemedi (atlandi)")


def bekci_dirilt():
    """Bring up dead guards at job start (autostart without launchd): saglik-nobet + kota-izle.
    P1: FROM THE CODE ROOT (CODE/bin), not from the data home. P2: liveness = pidfile+argv+heartbeat (kosu.bekci_canli), not pgrep -f."""
    return kosu_bekci_dirilt(QE, CODE)


BICIM_KIRMIZI = re.compile(r"would reformat|would be reformatted|code style issues found|run prettier", re.I)   # B206


HAKEM_MALIYET_SIRASI = {"opencode-go": 4, "openrouter": 6, "opencode": 7, "claude": 9}   # Go subscription first, cash after
HAKEM_KUCUK_DOSYA = 10


HAKEM_KUCUK_SATIR = 300


# the same as the common.sh `qe_commit_wip` EXC list: harness leftovers (QA output, plan ledger, autopsy, pyc)
# are NOT PRODUCT and are not committed — so the "is it clean" question after the delivery commit must exclude them too.
# `test_teslim_commit_pathspec_listesi_common_sh_ile_ayni` mechanically catches the two lists diverging.
URUN_HARIC = (':(exclude).gorevler.json', ':(exclude).veri-defteri.md', ':(exclude)STATE',
              ':(exclude,glob).cadre-plan/**', ':(exclude).qa-sonuc.txt', ':(exclude,glob)var/audit/OTOPSI-*.md',
              ':(exclude,glob)**/__pycache__/**', ':(exclude,glob)**/*.pyc', ':(exclude,glob)**/.pytest_cache/**',
              ':(exclude,glob)**/.mypy_cache/**', ':(exclude,glob)**/.ruff_cache/**')


TEST_KALIBI = re.compile(r'(^|/)(tests?/|spec/|__tests__/|test_[^/]*\.py$|[^/]*_test\.(py|go|js|ts)$|[^/]*\.(test|spec)\.(js|ts|mjs|jsx|tsx)$'
                         r'|conftest\.py$|pytest\.ini$|[^/]*Tests?\.(java|kt|cs)$)', re.I)   # B157: common names


# B144: assertion counter — a rough language-independent measure (Python assert*, JS expect/should, Go t.Error*, testify require/assert).
DOGRULAMA_KALIBI = re.compile(r'\bassert\w*\b|\bexpect\s*\(|\bshould\b|\bt\.(?:Error|Errorf|Fatal|Fatalf|Fail|FailNow)\s*\('
                              r'|\brequire\.\w+\s*\(', re.I)
SABIT_DOGRU_KALIBI = re.compile(r'\bassert\s+(?:True|1)\s*(?:,|$)|\bassertTrue\(\s*(?:True|1)\s*\)', re.I | re.M)
ATLAMA_KALIBI = re.compile(r'@[\w.]*\b(?:skip\w*|xfail)\b|\bskipTest\(|\bpytest\.skip\(|\bt\.Skip\w*\(', re.I)   # B152


from kabul import DUZELTME_TUR_VARSAYILAN
KIRIK_TEST = re.compile(r"FAILED ([\w./-]+::[\w\[\]-]+)")


# ==================== P2 fast verification, P4 compaction ====================
def hizli_dogrula(wt, dosyalar):
    """P2 — a cheap syntax check BEFORE escalating: .py->py_compile, .sh->bash -n, .json->json.loads.
    Catches bad output early. Returns (ok, detail). Types that cannot be checked pass. Needs no model/network."""
    kok = pathlib.Path(wt)
    for d in dosyalar:
        d = str(d).strip()
        if not d:
            continue
        p = kok / d
        if not p.is_file():
            continue
        try:
            if d.endswith(".py"):
                # B16: py_compile wrote __pycache__/*.pyc into the worktree and the LIGHT merge committed them as PRODUCT.
                # The syntax check is done without writing bytecode (ast).
                r = subprocess.run([sys.executable, "-c", "import ast,sys; ast.parse(open(sys.argv[1], 'rb').read(), sys.argv[1])", str(p)],
                                   capture_output=True, text=True, timeout=30)
                if r.returncode != 0:
                    return False, f"{d}: sozdizimi {r.stderr.strip().splitlines()[-1][-160:] if r.stderr.strip() else 'hata'}"
            elif d.endswith(".sh"):
                r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True, timeout=30)
                if r.returncode != 0:
                    return False, f"{d}: bash -n {r.stderr.strip()[-160:]}"
            elif d.endswith(".json"):
                json.loads(p.read_text(errors="replace"))
        except json.JSONDecodeError as e:
            return False, f"{d}: json {e}"
        except Exception:
            pass
    return True, "ok"


def sikistir(metin, tavan_token=None, gorev=False):
    """P4 context/prompt compaction. gorev=False (context): empty lines and repeats are deduped, past the cap the MIDDLE is cut (head+tail kept).
    gorev=True (C18, task text): no line is deleted, the middle is not cut; only trailing whitespace is trimmed."""
    if not metin:
        return metin
    if gorev:
        cikti, bos = [], 0
        for l in (x.rstrip() for x in metin.splitlines()):
            bos = bos + 1 if l == "" else 0
            if bos > 2:
                continue
            cikti.append(l)
        s = "\n".join(cikti)
        if tavan_token and len(s) > tavan_token * 4:
            print(f"sikistir: gorev metni {len(s)} kar > tavan {tavan_token*4}; anlam korunmasi icin KESILMEDI", file=sys.stderr)
        return s
    cikti, bos, onceki = [], 0, None
    for l in (x.rstrip() for x in metin.splitlines()):
        if l == "":
            bos += 1
            if bos > 1:
                continue
        else:
            bos = 0
        if l == onceki and l != "":
            continue
        cikti.append(l); onceki = l
    s = "\n".join(cikti)
    if tavan_token:
        tk = tavan_token * 4
        if len(s) > tk:
            bas, son = s[: tk // 2], s[-(tk // 2):]
            s = bas + f"\n\n[... {len(s) - len(bas) - len(son)} karakter baglam sikistirildi ...]\n\n" + son
    return s


from canlilik import (
    HizliDusus,
    AracTekrari,
    TestDongusu,
    Canlilik,
    StallGuard,
)
from kabul import (
    cikti_denetimi,
    deneme_ozeti,
    qa_atlanmis_mi,
    _bicim_kos,
    rapor_qa_kapisi,
    hedefli_qa_komutu,
    ozellik_riskli,
    qa_kapisi,
    bos_bitis,
    qa_ortami,
    giris_kabugu,
    qa_sureci as qa_sureci,
    _taban_komut,
    taban_hazirlik,
    _taban_hazirlik,
    taban_uyarisi,
    degisen_dosyalar,
    hakem_maliyeti,
    hakem_kucuk_mu,
    urun_kirli,
    agac_sha,
    silinen_testler,
    _kod_govdesi,
    dogrulama_sayisi,
    _qa_ciktisi,
    hata_imzasi,
    duzeltme_karari,
    duzeltme_istemi,
    serit_durumu,
    kapsam_denetimi,
    degisen_testler,
    zayiflatilan_testler,
    model_ailesi,
    kabul_guvencesi,
    guvence_tasi,
    tahmini_kucuk,
    plan_guvencesi,
    plan_guvencesi_birak,
    model_kimligi,
    hakem_kayitlari,
    dusen_hakemi_kaydet,
    urunu_yazan_modeller,
    ajan_commitleri,
    istek_kapsami,
    kurulum_izleri,
    kurulum_izinli,
    urun_farki,
    teslim_commit,
    hakem_bicim_onarimi,
    finalize_run,
)
from motor import (
    motor,
    _motor,
)

if __name__ == "__main__":
    if sys.argv[1:2] == ["plan-guvence"]:   # 4b: plan reserve at run start (non-blocking)
        plan_guvencesi(RunStore(QE).path(ENV('CADRE_RUN_ID')) / 'dispatch.log')
        sys.exit(0)
    if sys.argv[1:2] == ["plan-guvence-birak"]:
        plan_guvencesi_birak()
        sys.exit(0)
    if sys.argv[1:2] == ["guvence"]:
        try:
            kabul_guvencesi(RunStore(QE).path(ENV('CADRE_RUN_ID')) / 'dispatch.log')
        except BudgetDenied:
            sys.exit(75)
        sys.exit(0)
    if sys.argv[1:2] == ["finish"] and len(sys.argv) in (4, 5):
        lf = task_root("kalite") / "finalize.log"
        lf.parent.mkdir(parents=True, exist_ok=True)
        result = finalize_run(sys.argv[2], sys.argv[3], lf, ENV("CADRE_REAL_OPENCODE", "/opt/homebrew/bin/opencode"),
                              review_model=sys.argv[4] if len(sys.argv) == 5 else None)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0 if result["status"] in ("PASS", "UYGULANDI") else 1)
    if sys.argv[1:2] == ["claude-plan"] and len(sys.argv) == 5:
        # CADRE_ZORLUK: the dispatcher's pre-filter class (medium=3, hard=4); the single source of difficulty while plan.json does not exist yet.
        proc = run_claude(sys.argv[2], sys.argv[3], sys.stdin.read(), sys.argv[4],
                          tools="Read,Grep,Glob", budget="1.00", turns="3", role="plan",
                          difficulty=int(ENV("CADRE_ZORLUK", "3")))
        try:
            wait_process(proc, SINIR.son_sure('plan'))
        except CancelledRun:
            sys.exit(130)
        except subprocess.TimeoutExpired:
            sys.exit(1)
        raw = proc.output()
        claude_maliyet_kaydet(raw, sys.argv[3], sys.argv[4], proc.attempt.data)
        print(result_json(raw).get("result", ""))
        sys.exit(proc.returncode or 0)
    if sys.argv[1:2] == ["duzeltme-karari"] and len(sys.argv) >= 3:  # duzeltme-karari <run_id> [parent] (G-C16)
        _ust = int(sys.argv[3]) if len(sys.argv) > 3 else DUZELTME_TUR_VARSAYILAN
        _kayit = RunStore(QE).read(sys.argv[2])
        _karar, _tur, _bilgi = duzeltme_karari(_kayit, _ust, yeniden=sys.argv[4:5] == ["1"])
        if _karar == "DEVAM":
            RunStore(QE).update(sys.argv[2], duzeltme=[*(_kayit.get("duzeltme") or []),
                                {"tur": _tur, "imza": _bilgi, "neden": (_kayit.get("final_result") or {}).get("reason"), "ts": time.time()}])
            print(f"DEVAM {_tur} {_bilgi}")
        else:
            print(f"DUR {_bilgi}")
        sys.exit(0)
    if sys.argv[1:2] == ["duzeltme-istemi"] and len(sys.argv) >= 4:  # duzeltme-istemi <run_id> <round> [parent] (G-C16)
        print(duzeltme_istemi(RunStore(QE).read(sys.argv[2]), sys.argv[3] if sys.argv[3] == "yeniden" else int(sys.argv[3]),
                              int(sys.argv[4]) if len(sys.argv) > 4 else DUZELTME_TUR_VARSAYILAN))
        sys.exit(0)
    if sys.argv[1:2] == ["serit-durumu"] and len(sys.argv) >= 3:  # serit-durumu <wt> -> JSON (B192)
        print(json.dumps(serit_durumu(sys.argv[2])))
        sys.exit(0)
    if sys.argv[1:2] == ["kapsam"] and len(sys.argv) >= 7:  # kapsam <wt> <plan.json> <gid> <before.json> <backup> (B192)
        try:
            _plan = json.loads(pathlib.Path(sys.argv[3]).read_text())
        except FileNotFoundError:
            _plan = {}
            print(f"UYARI KAPSAM ({sys.argv[4]}): plan.json yok; bos planla kapsam denetimi uygulanacak (G-C15/B320)")
        _cik, _dis = kapsam_denetimi(sys.argv[2], _plan, sys.argv[4],
                                     json.loads(pathlib.Path(sys.argv[5]).read_text()), sys.argv[6])
        if _cik or _dis:
            _eski = RunStore(QE).read(ENV("CADRE_RUN_ID"))["tasks"].get(sys.argv[4], {})
            RunStore(QE).task_update(ENV("CADRE_RUN_ID"), sys.argv[4],
                                     kapsam_disi_yeni=sorted(set(_eski.get("kapsam_disi_yeni") or []) | set(_cik)),
                                     kapsam_disi_degisen=sorted(set(_eski.get("kapsam_disi_degisen") or []) | set(_dis)))
        for _y in _cik:
            print(f"KAPSAM DISI ({sys.argv[4]}): plan disi yeni dosya {_y} urunden cikarildi (yedek: kapsam-disi/{sys.argv[4]}/{_y})")
        for _y in _dis:
            print(f"KAPSAM DISI ({sys.argv[4]}): plan disi degisen mevcut dosya {_y} — hakeme ayri kriter")
        sys.exit(0)
    if sys.argv[1:2] == ["dogrula"] and len(sys.argv) >= 4:  # dogrula <wt> <dosya1,dosya2>  -> rc 0/1
        _ok, _det = hizli_dogrula(sys.argv[2], sys.argv[3].split(","))
        if not _ok:
            print(_det, file=sys.stderr)
        sys.exit(0 if _ok else 1)
    if sys.argv[1:2] == ["sikistir"]:  # sikistir [--gorev] [token_cap]: stdin -> stdout
        _a = sys.argv[2:]
        _g = "--gorev" in _a
        _t = [x for x in _a if x != "--gorev"]
        print(sikistir(sys.stdin.read(), int(_t[0]) if _t else None, gorev=_g)); sys.exit(0)
    if len(sys.argv) >= 4 and sys.argv[1] in ("motor", "serit"):
        if sys.argv[1] == 'motor' and not ENV('CADRE_GOREV_ID'):
            os.execv('/bin/bash', ['bash', str(CODE / 'bin/cadre-dispatch.sh'), *sys.argv[2:]])
        sys.exit(motor(sys.argv[2], sys.argv[3]))
    if len(sys.argv) >= 4 and sys.argv[1] == "qa":  # Stage 10: the dispatcher coordinator calls the QA gate once
        _p, _wt = sys.argv[2], sys.argv[3]
        _lf = task_root(ENV("CADRE_GOREV_ID", "qa")) / f"qa-{uuid.uuid4().hex}.log"
        _lf.parent.mkdir(parents=True, exist_ok=True)
        _lf.touch()
        result = qa_kapisi(_p, _wt, _lf, ENV("CADRE_REAL_OPENCODE", "/opt/homebrew/bin/opencode"))
        print(json.dumps(result, ensure_ascii=False)); sys.exit(0 if result["status"] in ("PASS", "UYGULANDI") else 1)
    print("kullanim: qe_core.py motor <proje> <prompt-dosyasi>", file=sys.stderr)
    sys.exit(2)
