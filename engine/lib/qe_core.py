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

from run_store import FileLock, RunStore, atomic_json, atomic_write, bounded_json, cancel_path, execute_run, owner_fds, task_root, run_job_type
from run_store import guard_revive as run_guard_revive, disk_status
import opencode_adaptor as OC
from session import Attempt, RecordedProcess, SessionRecoveryError, result_json, retire_owned_tools, session_ids, session_rows
from budget_gate import (BudgetDenied, BudgetLedger, ProviderWait, provider_wait_rejection, FREE as BUDGET_FREE, PAID as BUDGET_PAID, allowance, available_budget,
                          observed_budget, run_remaining_usd, spending, task_bans, note_denial)
import limits as LIMIT

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


def wait_process(proc, timeout: float, project_ref=None, wt=None, logf=None):
    """Long model/QA wait: cancel, stuck and deadline are seen from the same loop; the deadline applies even while there is liveness (4c-4)."""
    deadline = time.monotonic() + timeout
    liveness = Liveness(logf or proc.log_path, proc, wt) if isinstance(proc, RecordedProcess) or logf else None
    repeat = ToolRepeat(proc.attempt.stdout_path, 5) if isinstance(proc, RecordedProcess) else None
    if repeat:
        repeat.offset = 0
    check = 0.0
    try:
        while True:
            if cancel_requested(project_ref):
                raise CancelledRun('cancel')
            remaining = deadline - time.monotonic()
            if liveness and time.monotonic() >= check:
                check = time.monotonic() + 15
                if repeat and repeat.tick():
                    raise SessionLoop('loop: five repetitions of the same tool call')
                if liveness.stuck():
                    raise SessionStalled('LIVE1: all liveness signals stopped')
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


def wait_pause(seconds: float, project_ref=None):
    deadline = time.monotonic() + seconds
    while True:
        if cancel_requested(project_ref):
            raise CancelledRun('cancel')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(.5, remaining))


def cancel_requested(project_ref=None):
    """Persistent cancel flag: runs/<id>/cancel.flag if the run is recorded; chain/<project>-cancel.flag for the old path."""
    try:
        if ENV("CADRE_RUN_ID"):
            return (RunStore(QE).path(ENV("CADRE_RUN_ID")) / "cancel.flag").exists()
        return bool(project_ref) and cancel_path(project_ref).exists()
    except Exception:
        return False


def _children(pid):
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
        result += _children(child)
    return result


def pid_alive(pid):
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
    return OC.cancel(pid, grace, children=_children, alive=pid_alive)


def reap(proc, timeout: float = 15):
    """Limited wait: the child is reaped after TERM/KILL; never an unbounded `wait()` (C7)."""
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait(timeout=timeout)


def stop(proc, grace=None):
    """kill_tree + reap in one step; all of the engine's interruption points use this."""
    kill_tree(proc.pid, grace)
    return reap(proc)


def cloud_candidate(role="impl", forbidden=(), wt=None):
    """The first CLOUD model with budget/permission from the router's `role` chain (except claude; task bans dropped).
    If the budget is unknown the chain comes back empty -> ''. A single decision point (recovery_select uses it too)."""
    if not ROUTER:
        return ""
    try:
        b = budget_json()
        chain, _ = ROUTER.chain(role, "middle", request_measure(wt) if wt else 30000, b if isinstance(b, dict) else {}, ROUTER.status_read())
    except Exception:
        return ""
    for candidate in chain:
        if candidate.startswith("claude/") or ROUTER.provider(candidate) in set(forbidden):
            continue
        if role == "recovery" and getattr(ROUTER, "recovery_breath", lambda *_: None)(candidate, b if isinstance(b, dict) else {}):
            continue   # B277: a share hand-off that cannot carry the session falls back after ~2 min
        return candidate
    return ""


def disk_wait(logf, project_ref=None, deadline=None):
    """P2: if free space is below the threshold (CADRE_DISK_MIN_MB, 2048) no NEW executor starts; polled up to the cap (CADRE_DISK_WAIT, 1 day).
    True = enough · None = cancel flag while waiting · False = cap reached (reason written to a structured field)."""
    ok, empty, threshold = disk_status(QE)
    if ok:
        return True
    cap = float(ENV("CADRE_DISK_WAIT", "86400"))  # disk: 1 day (rotation frees space daily)
    poll = float(ENV("CADRE_DISK_POLL", "300"))
    last = min(time.monotonic() + cap, deadline) if deadline is not None else time.monotonic() + cap
    enabled = max(0, last - time.monotonic())
    log(logf, f"DISK LOW: {empty} MB < {threshold} MB — executor not started; waiting at most {enabled:.1f}s (monitoring the cancel flag)")
    while time.monotonic() < last:
        if cancel_requested(project_ref):
            log(logf, "CANCEL flag seen while waiting for disk space; wait interrupted")
            return None
        try:
            wait_pause(min(poll, max(0.0, last - time.monotonic())), project_ref)
        except CancelledRun:
            return None
        ok, empty, threshold = disk_status(QE)
        if ok:
            log(logf, f"sufficient disk space ({empty} MB) — proceed")
            return True
    log(logf, f"disk wait expired after {enabled:.1f}s ({empty} MB < {threshold} MB); stopping")
    if ENV("CADRE_RUN_ID"):
        RunStore(QE).update(ENV("CADRE_RUN_ID"), reason=f"disk-low: {empty} MB < {threshold} MB (engine wait cap expired)")
    return False


def claude_command(command):
    return [sys.executable, str(pathlib.Path(__file__).with_name('claude_transport.py')), *command]


def run_claude(wt, model_effort, pf_text, logf, tools=None, budget=None, turns=None, role="impl", difficulty=None):
    """claude -p lane: claude/<model>#<effort>; the cost from the JSON goes to the ledger. tools/budget/turns: read-only, small-budget use (plan).
    C31: the requested model/effort is a request; the weekly reserve + task difficulty + the daily Fable slot decide
    (easy/medium -> sonnet#high, hard -> opus#xhigh, hardest -> max; Fable only at difficulty 5 + slot + share>=10)."""
    if cancel_requested():
        raise CancelledRun('cancel')
    observed = budget_json()
    reason = ''   # B249: the sub-reason (stale observation / ban / pocket / policy) is recorded; 'unavailable' alone was not enough for the RCA
    try:
        has_pay = available_budget(QE, observed, 'claude', role=role) > 0   # B112: the pocket comes from the ROLE
        if not has_pay:
            reason = 'zero allocation (pocket restriction, run pocket or policy allocation)'
    except BudgetDenied as exc:
        has_pay = False          # no/broken observation: closed-failure, but still recorded
        reason = str(exc) or 'observation absent'
    if not has_pay:
        # B114-3: this denial never reached the gate, so it was not written to `denials[]` and the morning summary could not read
        # the silent closure from the record. The record must be in the same place as the denial itself.
        note_denial(QE, 'claude', reason=f'Claude allowance unavailable: {reason}')
        raise BudgetDenied(f'Claude allowance unavailable: {reason}')
    from claude_policy import difficulty as task_difficulty, fable_slots, model as policy_model
    try:
        level = task_difficulty(QE, role, difficulty)
        model_effort = policy_model(model_effort, observed, level, fable_slots(QE, observed))
    except (ValueError, IndexError, TypeError, OSError) as exc:
        raise BudgetDenied(f'Claude policy refused the request: {exc}') from exc
    log(logf, f"claude policy: difficulty={level} -> {model_effort}")
    bin_ = ENV("CLAUDE_BIN", "claude")
    m, _, eff = model_effort.partition("#")
    m = m.split("/", 1)[1]
    cap = float(budget or ENV("CADRE_CLAUDE_BUDGET", {"opus": "5.0", "fable": "8.0"}.get(m.split("-")[0], "2.0")))
    remaining = run_remaining_usd(QE, ENV("CADRE_RUN_ID"), role, 'claude')  # P5+B112: the cap comes from the CALLING ROLE's pocket
    if remaining is not None:
        if remaining <= 0:
            raise BudgetDenied('run budget exhausted')
        cap = min(cap, remaining)
    cmd = [bin_, "-p", "--model", m, "--output-format", "stream-json", "--verbose",
           "--max-turns", turns or ENV("CADRE_CLAUDE_TURNS", "20"),
           "--max-budget-usd", f"{cap:g}",
           "--allowedTools", tools or CLAUDE_WRITING_TOOLS]
    if eff:
        cmd += ["--effort", eff]
    record = run_record()
    # B110-2: the Claude lane had NO path restriction for `.env`/key files; both reading and
    # carrying were open. The body check at the boundary is the second gate, the first is never reading.
    forbidden = [CLAUDE_SECRET_PATHS]
    if run_job_type(record) == "review" or tools == CLAUDE_READ_ONLY_TOOLS:  # C19: in a read-only run the write tools are TECHNICALLY closed
        cmd[cmd.index("--allowedTools") + 1] = CLAUDE_READ_ONLY_TOOLS
        forbidden.insert(0, CLAUDE_WRITE_TOOLS)
    cmd += ["--disallowedTools", ",".join(forbidden)]
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
               'CADRE_CLAUDE_ROLE':role,   # B72: role of Claude lines in the spending breakdown (plan/quality/impl)
               'CADRE_OWNER_FDS':','.join(map(str, (*owner_fds(), attempt.lease.fd)))}
        return attempt.launch(claude_command(cmd), str(wt), pathlib.Path(logf), env, owner_fds())
    raise ValueError("Claude execution requires an accepted run")


def claude_expense_save(proc_stdout, model, logf, attempt=None):
    try:
        j = result_json(proc_stdout)
        import math
        raw_cost = j.get("total_cost_usd", j.get("cost_usd"))
        if raw_cost is None:
            raise ValueError("cost receipt missing")
        cost = float(raw_cost)
        if not math.isfinite(cost) or cost < 0:
            raise ValueError("invalid cost receipt")
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": model, "usd": cost,
                 "run_id": ENV("CADRE_RUN_ID", ""), "task_id": ENV("CADRE_TASK_ID", "") or "main",
                 "session_id": j.get("session_id"), "attempt_id": (attempt or {}).get("attempt_id")}
        try:
            pool = float((QE / "chain/claude-pool-usd.txt").read_text().strip())
            if math.isfinite(pool) and pool > 0:
                row["percent_estimate"] = round(cost / pool * 100, 4)
        except (OSError, ValueError):
            pass  # Missing quota calibration must never discard actual USD.
        path = QE / "chain/claude-spend.jsonl"
        with FileLock(path.with_suffix(".lock")):
            prior = path.read_text().splitlines() if path.exists() else []
            for line in prior:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue  # Preserve damaged history, but still record the new receipt.
                if isinstance(entry, dict) and row["attempt_id"] and entry.get("attempt_id") == row["attempt_id"]:
                    return
            with path.open("a") as f:
                if path.stat().st_size and prior and not path.read_bytes().endswith(b"\n"):
                    f.write("\n")  # An interrupted last line must not swallow the new JSON record.
                f.write(json.dumps(row) + "\n")
                f.flush()
                os.fsync(f.fileno())
        log(logf, f"claude expense: ${cost} ({model})")
        if j.get("result"):
            with open(logf, "a") as f:
                f.write(str(j["result"])[-3000:] + "\n")
    except Exception:
        log(logf, "claude expense unreadable")


def restriction_model_select(b, sensitive=False, forbidden=None, role="impl"):
    """Model for a sub-agent: asked from the chain of that agent's OWN role, with the context size that role declares (B121).
    A model that cannot hold the context is not written to the agent. 4c-10: Go per model, not as a whole (the chain measures budget per model)."""
    forbidden = set(forbidden or ())
    # If the budget observation cannot be read no candidate is selected (fail-closed, B93).
    if ROUTER is None:
        return None, "no-capacity"
    if not isinstance(b, dict) or not b:
        return None, 'no-budget'
    d = ROUTER.status_read()
    z, _ = ROUTER.chain(role, "middle", None, b, d)   # B121: size comes from the role's declaration, not hand-written
    for m in z:
        provider_key = ROUTER.provider(m)
        if provider_key in forbidden or provider_key == "claude":   # claude is not a sub-agent provider
            continue
        # 0b: the bucket unit is the model. A '<provider>:free' ban removes only that provider's ZERO-PRICED models;
        # the same provider's paid models keep working (previously the two could not be told apart).
        if f"{provider_key}:free" in forbidden:
            try:
                from model_catalog import is_free
                if is_free(provider_key, m.split("/", 1)[1] if "/" in m else m) is True:
                    continue
            except Exception:
                pass
        return m, provider_key
    return None, "no-capacity"


PERMISSION_RULES = {
    "deploy": {"*deploy_release.sh*": "allow", "*deploy_permissions.sh*": "allow", "rsync *": "allow", "scp *": "allow"},
    "rsync": {"rsync *": "allow"},
    "scp": {"scp *": "allow"},
    "push": {},   # git push is already open (except force); meaning: push to the canonical branch is allowed (build.md text)
    "merge": {},  # meaning: merge to the canonical branch is allowed (build.md text)
}


def permission_sift(prompt_text):
    """A `PERMISSION: deploy, push` line in the task text -> {'deploy','push'}."""
    permission = set()
    for l in (prompt_text or "").splitlines():
        m = re.match(r"^\s*PERMISSION\s*:\s*(.+)$", l.strip(), re.I | re.ASCII)
        if m:
            for tok in re.split(r"[,\s]+", m.group(1).strip().lower()):
                if tok in PERMISSION_RULES:
                    permission.add(tok)
    return permission


def restriction_config_write(reason, budget=None, sensitive=False, permission=None, forbidden=None, has_go=False):
    """Per-run OPENCODE_CONFIG: (a) the model picked from the ladder for sub-agents (if Go is absent or failing),
    (b) build permission extensions if there is a PERMISSION line. The agent md files have NO model/permission, so the override applies.
    Compaction is not written here: it is the lane's own model (4c-10, `opencode_budget_env`). Returns: (file, selection|None, step)."""
    cfg = {"$schema": "https://opencode.ai/config.json", "agent": {}}
    model, digit = None, "go"
    if not has_go:
        # B140: not writing a model is not enough; close the inherited default too.
        choice = []
        for k, role in (("cadre-plan", "plan"), ("cadre-review", "review"), ("cadre-impl", "impl")):
            candidate, step = restriction_model_select(budget, sensitive=sensitive, forbidden=forbidden, role=role)
            cfg["agent"][k] = {"model": candidate} if candidate else {"disable": True}
            choice.append(f"{k}={candidate or '-'}")
            if k == "cadre-impl":
                digit = step
        model = ",".join(choice)
    if permission:
        bash = {}
        for tok in sorted(permission):
            bash.update(PERMISSION_RULES.get(tok, {}))
        if bash:
            cfg["agent"]["build"] = {"permission": {"bash": bash}}
    p = task_root(ENV("CADRE_TASK_ID", "")) / "configs" / f"{uuid.uuid4().hex}.json"
    atomic_json(p, cfg)
    return str(p), model, digit


def on_plan(real, wt, project_ref, pf_text, logf, budget, env_extra=None):
    """Engine pre-plan: before the run starts we have the best model on the PLAN LADDER split the task; the briefing is written into the worktree,
    build starts with that briefing and does not ask for another. On failure one step lower is tried; at the very end it is skipped."""
    memory = ", ".join(str(QE / "projects" / project_ref / f) for f in ("PROJECT.md", "POLICY.md") if (QE / "projects" / project_ref / f).exists()) or "(absent)"
    prompt = PLAN_PROMPT.format(memory=memory, task=pf_text)
    attempts = []
    if ROUTER is not None and isinstance(budget, dict):
        z, _ = ROUTER.chain("plan", "middle", 30000, budget, ROUTER.status_read())
        attempts = [("claude" if m.startswith("claude/") else "opencode", m, ROUTER.provider(m)) for m in z][:4]
    if not attempts:
        log(logf, "PLAN: no planner within budget; skipping the engine pre-plan, build plans independently")
        return None
    out_dir = task_root(ENV("CADRE_TASK_ID", "")) / "plan"
    out_dir.mkdir(parents=True, exist_ok=True)
    if (out_dir / "briefing-runner.md").is_file():
        return str(out_dir / "briefing-runner.md")
    for round, model, start in attempts:
        t0 = time.time()
        log(logf, f"PLAN: {start} -> generating a briefing with {model}")
        try:
            if round == "claude":
                proc = run_claude(wt, model, prompt, logf, tools="Read,Grep,Glob,Bash(git log:*),Bash(git show:*),Bash(git diff:*),Bash(ls:*),Bash(cat:*),Bash(sed -n:*)",
                                  budget=ENV("CADRE_CLAUDE_PLAN_BUDGET", "1.0"), turns="25", role="plan")
                wait_process(proc, LIMIT.deadline('plan'), project_ref)
                out = proc.output()
                claude_expense_save(out or "", model, logf, proc.attempt.data if isinstance(proc, RecordedProcess) else None)
                try:
                    j = result_json(out); text = str(j.get("result") or "")
                except Exception:
                    text = out or ""
            else:
                output = capture_opencode(real, wt, ["--agent", "cadre-plan", "-m", model, prompt], LIMIT.deadline('plan'), env_extra)
                text = re.sub(r"\x1b\[[0-9;]*m", "", output)
            if budget_rejection_text(text):
                log(logf, f"PLAN: {model} budget denied (CADRE_BUDGET_DENIED); trying the next rung")   # B177: not counted as a failure, the breaker is not poisoned
                continue
            if len(text.strip()) < 200 or "ITEMS" not in text:
                log(logf, f"PLAN: {model} insufficient output ({len(text)} chars); trying the next rung")
                if ROUTER: ROUTER.failure(ROUTER.provider(model), "insufficient plan or error", model=model)
                continue
            bp = out_dir / "briefing-runner.md"
            atomic_write(bp, f"# BRIEFING (engine pre-plan) — model: {model} — {time.strftime('%F %T')}\n\n{text.strip()}\n")
            log(logf, f"PLAN: complete — {model}, {int(time.time() - t0)} s, {len(text)} chars -> {bp}")
            return str(bp)
        except CancelledRun:
            raise
        except Exception as e:
            log(logf, f"PLAN: {model} error: {e}")
    log(logf, "PLAN: no cloud model could produce a briefing; build plans independently")
    return None


def worktree_base_sha(wt):
    """The base commit this worktree was opened from, from worktrees.log. The record is two lines: 'base=X base_sha=Y' then 'branch=... worktree=<path>';
    the base_sha line does NOT carry the worktree name -> the block is read."""
    try:
        last = ""
        for l in (QE / "logs/worktrees.log").read_text(errors="replace").splitlines():
            if "base_sha=" in l:
                last = l.split("base_sha=")[1].split()[0]
            if "worktree=" in l and l.split("worktree=")[1].strip() == str(wt).rstrip("/"):
                return last
    except Exception:
        pass
    return ""


PLAN_PROMPT = """You are a PLANNER (read-only). Do not change code, tests, config or Git. Split the task below into feasible items
with acceptance criteria for this repo. First read project memory: {memory}. Then inspect the repo as needed (git log, documentation, relevant files).
OUTPUT (English, Markdown):
## UNDERSTANDING — one sentence
## ITEMS — JSON code block, `.tasks.json` format: [{{"id":"...","title":"...","source":"file/line or document","acceptance":"measurable criterion","scope_outside":"...","dependent":"...","passes":false}}]
   (3-8 items; prefix titles of items requiring user approval with "APPROVAL:")
## PITFALLS — 3-6 repo-specific items (evidence: file:line)
## ORDER — which item comes first and why
## WARNING — anything wrong or impossible in the task
TASK:
{task}"""


def go_restriction_required(*, in_flight=False, budget=None):
    """Go brake; returns (restriction_needed, budget, reason). `in_flight=True` is for an in-flight call and looks at the provider's periodic
    allocation (does not count reservations, its share is already set aside); `in_flight=False` is for new admission and looks at the run/role pocket (B114-5)."""
    try:
        b = budget_json() if budget is None else budget
        if in_flight:
            return False, b, ''  # GO1: the percentage gate only stops a new request.
        remaining = allowance(b, 'opencode-go')[1] if in_flight else available_budget(QE, b, 'opencode-go')
        return remaining <= 0, b, ('pay-finished' if remaining <= 0 else '')
    except Exception as exc:
        return True, {}, f'unreadable:{type(exc).__name__}: {exc}'.strip()


READ_ONLY_BASH = ("git log *", "git diff *", "git show *", "git status *", "git blame *", "ls *", "cat *", "head *", "tail *",
                  "grep *", "wc *", "pwd", "echo *")
READ_ONLY_BASH_DENY = ("*>*", "*--output*")
READ_ONLY_PERMISSION = {"edit": "deny", "bash": {"*": "deny", **{pattern: "allow" for pattern in READ_ONLY_BASH},
                                                 **{pattern: "deny" for pattern in READ_ONLY_BASH_DENY}}}
# B246: there is no approval in -p mode; a read-only command not on the allow-list (`| head`) wastes the round.
CLAUDE_READ_ONLY_SHELL = "Bash(ls:*),Bash(cat:*),Bash(sed -n:*),Bash(head:*),Bash(tail:*),Bash(rg:*),Bash(grep:*),Bash(find:*),Bash(wc:*),Bash(diff:*)"
CLAUDE_READ_ONLY_TOOLS = ("Read,Grep,Glob,Bash(git log:*),Bash(git show:*),Bash(git diff:*),Bash(git status:*),"
                          "Bash(ls:*),Bash(cat:*),Bash(head:*),Bash(tail:*),Bash(grep:*),Bash(wc:*),Bash(diff:*)")
CLAUDE_WRITING_TOOLS = "Read,Edit,Write,Grep,Glob,Bash(git:*),Bash(python3:*),Bash(uv:*),Bash(make:*),Bash(pytest:*)," + CLAUDE_READ_ONLY_SHELL
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
        if name == "qa-command":
            continue
        if not pathlib.Path(path).is_file():
            raise ValueError(f"project memory missing: {name} ({path})")
        paths.append(str(path))
    return paths


PROVIDER_IDS = tuple(sorted({*BUDGET_PAID, *BUDGET_FREE}))   # except claude: it goes through its own transport


def global_opencode_agents():
    """B96: agent names in the global `~/.config/opencode/opencode.jsonc` are overridden in the run config; a sub-agent's provider
    is decided by our budget decision, not by the user's global file."""
    path = pathlib.Path(os.environ.get('XDG_CONFIG_HOME') or (pathlib.Path.home()/'.config'))/'opencode/opencode.jsonc'
    try:
        text = re.sub(r'(?m)^\s*//.*$', '', path.read_text(errors='replace'))
        agents = json.loads(text).get('agent', {})
        return sorted(k for k in agents if isinstance(k, str)) if isinstance(agents, dict) else []
    except (OSError, ValueError, TypeError):
        return []


def agent_role(entry_name):
    return next((r for r in ('plan', 'review', 'compaction') if r in entry_name), 'impl')


def opencode_budget_env(env, args):
    """Every CLI path starts with an empty provider list and the same request gate; it opens no provider without installing the plugin's fetch wrappers,
    so a missing plugin/config cannot silently authorize a paid provider. C17: project memory via `instructions`; C19: writes closed in review mode."""
    budget = budget_json()
    record = run_record()
    # while the global config loaded `cadre-plugin.mjs` (composite), the run config added `budget-plugin.mjs` ->
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
    def suitable(candidate, role=None, context=None):
        from budget_gate import model_bucket, USD_PROVIDERS
        provider_key, _, mid = candidate.partition('/')
        if context is not None and (not ROUTER or context > ROUTER.request_cap(provider_key, candidate)):
            return False
        if not plugin.is_file() or provider_key not in PROVIDER_IDS:
            return False
        role = role or env.get('CADRE_ROLE')
        rid = env.get('CADRE_RUN_ID') or ENV('CADRE_RUN_ID')
        try:
            bucket = model_bucket(provider_key, mid)
            if bucket in USD_PROVIDERS:
                remaining = run_remaining_usd(QE, rid, role, bucket)
                if remaining is not None and remaining < float(ENV('CADRE_CAP_MIN_USD', '0.02')):
                    return False
            return available_budget(QE, budget, bucket, role, rid) > 0
        except Exception:
            return False

    open_choice = any(o in ('-m', '--model') or o.startswith('--model=') for o in args[:-1])
    if not open_choice and not selected and ROUTER:
        # P3: an executor without a selected model goes to the first cloud model with budget in the role's chain
        route, _ = ROUTER.chain(env.get('CADRE_ROLE', 'impl'), 'middle', None, budget, ROUTER.status_read())
        selected = next((m for m in route if suitable(m)), '')
    if not selected or not suitable(selected):
        # P3: changing provider/model at the gate hides the session's identity.
        raise BudgetDenied(f'budget-wait: {selected} has no verified capacity')
    config = {}
    scoped = pathlib.Path(env.get('OPENCODE_CONFIG', '/nonexistent'))
    if scoped.is_relative_to(QE/'runs'):
        config = json.loads(scoped.read_text())
    config.update(json.loads(env.get('OPENCODE_CONFIG_CONTENT') or '{}'))
    config.update(enabled_providers=[], small_model=selected, model=selected)
    if (env.get('CADRE_ROLE') or 'impl') in LANE_ROLES:
        # 4c-4: the lane's effective context is min(256k, model context − 20k); opencode triggers compaction at this limit
        from model_catalog import model as catalog_model
        provider_key, _, mid = selected.partition('/')
        limit = dict((catalog_model(provider_key, mid.split('#')[0]) or {}).get('limit') or {})
        if limit.get('context'):
            limit['context'] = LIMIT.lane_context(int(limit['context']))
            if limit.get('input'):
                limit['input'] = min(int(limit['input']), limit['context'])
            limit.setdefault('output', model_output_limit(provider_key, mid.split('#')[0]))
            config.setdefault('provider', {}).setdefault(provider_key, {}).setdefault('models', {}).setdefault(mid, {})['limit'] = limit
    agents = config.setdefault('agent', {})
    known = (('build', 'impl'), ('cadre-plan', 'plan'), ('cadre-impl', 'impl'),
               ('cadre-review', 'review'), ('compaction', 'compaction'))
    # B96: OTHER agent names defined in the global config are also overridden by our decision; otherwise a sub-agent
    # uses the provider from the user's global file and the spending never sees the budget gate.
    extra = tuple((entry_name, agent_role(entry_name)) for entry_name in global_opencode_agents()
                   if entry_name not in {a for a, _ in known})
    primary = options[options.index('--agent') + 1] if '--agent' in options else 'build'
    sub_agentless = env.get('CADRE_SUB_AGENTLESS') == '1'   # 4c-9: the dispatcher lane and the judge do not call sub-agents
    for agent, role in known + extra:
        existing = agents.get(agent, {}).get('model')
        context = ROUTER.role_context(role) if ROUTER else None
        chain = ROUTER.chain(role, 'middle', None, budget or {}, ROUTER.status_read())[0] if ROUTER else []
        candidates = [m for m in ([existing] if existing else []) + chain]
        candidate = next((m for m in candidates if suitable(m, role, context)), None)
        if agent in ('build', primary, 'compaction'):
            # 4c-10: the lane's own model does the compaction (single source). opencode deletes a `disable`d agent from the registry and
            # automatic compaction reads that agent unconditionally (1.18.31); this also overrides the inherited cloud default (B140).
            candidate = selected
        elif agent == 'cadre-review' or sub_agentless:   # 4b: no intermediate review inside a lane; the second pair of eyes is the final independent judge
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
        present = [p for p in config.get('plugin', []) if not str(p).endswith(('/cadre-plugin.mjs', '/budget-plugin.mjs'))]
        config['plugin'] = [*present, plugin.as_uri()]
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
            agent['prompt'] = ((agent.get('prompt') or '') + '\nRead and change project files only through the bash tool; '
                'Git metadata, approval records, network access and external directory writes are blocked. Do not invent Git or install permission.')
    env.update(OPENCODE_CONFIG_CONTENT=json.dumps(config), OPENCODE_EXPERIMENTAL_NATIVE_LLM='false',
               CADRE_PYTHON=sys.executable, CADRE_ENGINE_ROOT=str(CODE), CADRE_HOME=str(QE))
    return [*options, '-m', selected, args[-1]]


def model_output_limit(provider_key, mid):
    try:
        tiers = json.loads((QE / 'chain/openrouter-price.json').read_text())['models'][mid]['tiers'].values()
        max_output = max(int(k.get('max_output') or 0) for k in tiers)
        if provider_key == 'openrouter' and max_output > 0:
            return max_output
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return 2 * LIMIT.output('lane')


def role_environment(env, role):
    """Item 3: the budget gate reads the role from the ENVIRONMENT. The cap a role may spend and the pocket its spending is written to
    come from one decision: `budget_gate.role_budget(home, run_id, role) -> (cap, slice)`. Per the HARD SPLIT
    each role spends only from its own pocket; lanes cannot eat the whole cap and leave the judge hungry."""
    env['CADRE_ROLE'] = role or 'impl'
    return env


LANE_ROLES = ("impl", "qa")   # code-writing roles: the lane's effective context applies to these


def effort_environment(role):
    """N-TS03 (CR-50): explicit effort policy from the run's recorded selection and the task's error count; default effort if unreadable."""
    try:
        from model_selection import effort_selection, run_selection
        run_id, task_name = ENV("CADRE_RUN_ID"), ENV("CADRE_TASK_ID") or "main"
        task = (RunStore(QE).read(run_id).get("tasks") or {}).get(task_name) or {}
        return effort_selection(run_selection(QE, run_id), role, int(task.get("failure_count") or 0), task_name)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def run_opencode(real, wt, args, logf, env_extra=None, role="impl"):
    """Start an opencode run; returns a Popen. stdout/err flow to the log file. env_extra: extra environment such as OPENCODE_CONFIG."""
    if cancel_requested():
        raise CancelledRun('cancel')
    env = role_environment(dict(os.environ), role)
    if env_extra:
        env.update(env_extra)
    # --dir is REQUIRED: cwd is not enough; opencode treats the worktree as a sandbox and makes the session directory the MAIN REPO.
    # DESIGN-5: independent cloud lanes run concurrently.
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
            effort = effort_environment(role)
            for key in ('CADRE_EFFORT', 'CADRE_EFFORT_POLICY'):
                env.pop(key, None)
            if effort:
                env.update(CADRE_EFFORT=effort['level'], CADRE_EFFORT_POLICY=effort['policy'])
            attempt = Attempt("opencode", role, model, args[-1], env.get("OPENCODE_CONFIG", ""),
                              proceed_extra=env.get('CADRE_PROCEED_EXTRA', ''), proceed_mandatory=env.get('CADRE_PATH_PROCEED') == '1')
            env['CADRE_BUDGET_ATTEMPT'] = attempt.data['attempt_id']
            env['CADRE_REQUEST_DIR'] = str(attempt.directory / 'requests')
            try:
                agents = json.loads(env.get("OPENCODE_CONFIG_CONTENT") or "{}").get("agent") or {}
            except ValueError:
                agents = {}
            # The helper map goes into the attempt record: which model is left out after a stall is read from here (§0a/10b).
            attempt.save(agents={entry_name: (None if v.get("disable") else v.get("model"))
                                 for entry_name, v in agents.items() if isinstance(v, dict)}, effort_policy=effort)
            if effort:
                RunStore(QE).task_update(ENV("CADRE_RUN_ID"), env.get("CADRE_TASK_ID") or "main", effort_policy=effort)
            options = OC.session_options(options, session_id=attempt.data['session_id'] if attempt.resuming else '',
                                         title=attempt.data['title'])
            args = [*options, "--", attempt.prompt]
            if cancel_requested():   # G-C9: a cancel that arrives while the attempt is being prepared (B278 lock wait) does not start the executor
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


def is_helper_rejection(receipt, main_provider_key):
    """B231: is the denial only for providers OTHER than the main model? A pocket (role) denial or a denial without a provider covers the main model too."""
    lines = (receipt or {}).get('refusals') or []
    return bool(lines) and all(r.get('scope') != 'role' and r.get('provider') and r.get('provider') != main_provider_key for r in lines)


TEMPORARY_RET = ('pricing unavailable', 'pricing unknown', 'ledger refused or unavailable', 'request context missing')


def apply_budget_denial(bans, receipt, snapshot):
    """The ledger enforces the role pocket; this denial does not close the provider or the other pockets."""
    slices, temporary = {}, set()
    if receipt.get('refusals'):
        rows = [row for row in receipt['refusals'] if row.get('scope') != 'role' and not provider_wait_rejection({'refusals': [row]})]
        providers = {row['provider'] for row in rows if row.get('provider')}
        # a run POCKET denial (the line has `slice`) is not a provider share denial; it is written pocket-stamped so it is re-checked when the cap
        # rises (P6) or after recheck. An unstamped ban only opened when the share renewed -> the job never started.
        slices = {row['provider']: row.get('slice') for row in rows if row.get('provider') and row.get('slice')}
        if rows and not providers:   # G-B5 (B154): a provider denial without a provider -> cautious fallback, not a silent pass
            providers = {*BUDGET_PAID, *BUDGET_FREE, 'claude'}
        temporary = {p for p in providers if (own := [row for row in rows if row.get('provider') == p])
                  and all(any(g in str(row.get('reason') or '') for g in TEMPORARY_RET) for row in own)}
    else:
        providers = receipt.get('providers') or (*BUDGET_PAID, *BUDGET_FREE, 'claude')
    for provider in providers:
        bans.block(provider, 'unavailable' if provider in temporary else 'budget', snapshot, slice=slices.get(provider))


def duration_end(remaining_sec):
    """N-TS01 (CR-48): wall-clock deadline read by the provider gate; endpoint selection does not prefer a route that does not fit the time."""
    return repr(time.time() + max(0.0, float(remaining_sec)))


def capture_opencode(real, wt, args, timeout, env_extra=None, role="plan"):
    """Bounded helper call through the same executor lock as the main worker."""
    review = BudgetLedger(QE / 'chain/request-budget.db').begin_review(
        ENV('CADRE_RUN_ID'), ENV('CADRE_TASK_ID', ''), role)
    review_deadline = min(review['deadline'], time.time() + timeout) if review else None
    if review:
        rule = review['rule']
        notice = (f"REVIEW LIMIT: {review['phase']}; remaining, including prior attempts and repairs: "
                  f"{rule['request'] - review['calls']} model requests, {rule['tool'] - review['tools']} tool calls. "
                  f"The phase spending cap in each separate budget pool is ${review['limit'] / 1e6:.6f}; "
                  f"Time remaining: at most {max(0, int(review_deadline - time.time()))}s. "
                  "Review the selected scope briefly with direct evidence; do not invent missing evidence.\n")
        if review['phase'] in ('plan-review', 'review-final', 'review-recheck') and rule['tool'] - review['tools'] <= 0:
            raise BudgetDenied(f"review limit: {review['phase']}: tools")
        first, separator, rest = args[-1].partition('\n')
        args = [*args[:-1], first + ' ' + notice.strip() + separator + rest]
    output = task_root(ENV("CADRE_TASK_ID", "")) / f"output-{uuid.uuid4().hex}.log"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("")   # empty file: a mock run_opencode may not open the file (for test fixtures)
    env_extra = dict(env_extra or {})
    paths = set()
    while True:
        remaining = min(timeout, review_deadline - time.time()) if review_deadline is not None else timeout
        if remaining <= 0:
            raise BudgetDenied('review limit: time')
        env_extra['CADRE_DEADLINE_TS'] = duration_end(remaining)
        proc = run_opencode(real, wt, args, output, env_extra, role=role)
        model = proc.attempt.data['model'] if isinstance(proc, RecordedProcess) else ''
        stalled = cut = False
        try:
            wait_timeout = max(0, min(remaining, review_deadline - time.time())) if review_deadline is not None else remaining
            rc = wait_process(proc, wait_timeout, wt=wt, logf=output)
        except SessionLoop:
            if not isinstance(proc, RecordedProcess):
                raise
            task = proc.attempt.store.read(proc.attempt.run_id)['tasks'].get(proc.attempt.task_id, {})
            if task.get('loop_routed'):
                raise RuntimeError('STALLED: tool loop after rerouting')
            proc.attempt.store.task_update(proc.attempt.run_id, proc.attempt.task_id, loop_routed=True)
            env_extra.update(CADRE_PATH_PROCEED='1', CADRE_PROCEED_EXTRA='\nDo not repeat the same tool call; try a different approach.')
            args = ['-m', model, args[-1] + env_extra['CADRE_PROCEED_EXTRA']]
            continue
        except SessionStalled:
            stalled, rc = True, 1
        refusal = budget_denial_receipt(proc)
        raw = proc.output() if isinstance(proc, RecordedProcess) else output.read_text(errors='replace')
        if isinstance(proc, RecordedProcess) and not stalled and rc == 0:
            summary = output_audit(proc, model, output, role)   # 4c-4: in a helper call too, a truncation continues once with 2×
            cut = summary["finish"] == "length"
            if cut and env_extra.get('CADRE_OUTPUT_MULTIPLIER') != '2':
                env_extra.update(CADRE_OUTPUT_MULTIPLIER='2', CADRE_PATH_PROCEED='1', CADRE_PROCEED_EXTRA=CUT_ANSWER)
                continue
        limit = model.startswith('opencode-go/') and (refusal is not None or rc == 75 or
            (rc != 0 and re.search(r'\b429\b|rate.?limit|quota|allocation exhausted', raw, re.I)))
        if not stalled and not limit:
            break
        paths.add(ROUTER.provider(model))
        candidate = path_hand_over(model, output, 'stuck' if stalled else 'Go limit', role, paths, model_handover=False)
        if not candidate:
            raise BudgetDenied('budget-wait: same model for open path absent')
        env_extra.pop('CADRE_OUTPUT_MULTIPLIER', None)   # 2× only on the truncated call's own route (4c QA2)
        args = ['-m', candidate, args[-1]]
        env_extra['CADRE_PATH_PROCEED'] = '1'
    if refusal is not None or rc == 75:
        outage = provider_wait_rejection(refusal)
        if outage:
            if review and isinstance(proc, RecordedProcess):
                attempt = proc.attempt
                try:
                    receipt = bounded_json(attempt.directory / 'process.json')
                except (OSError, ValueError):
                    receipt = {}
                identity = receipt.get('process_identity')
                if (identity and receipt.get('stop_observed') == identity
                        and re.fullmatch('[a-f0-9]{64}', str(receipt.get('scope_digest') or ''))
                        and attempt.run_id == ENV('CADRE_RUN_ID') and attempt.task_id == ENV('CADRE_TASK_ID')
                        and all(receipt.get(k) == v for k, v in (('run_id', attempt.run_id), ('task_id', attempt.task_id),
                                                               ('attempt_id', attempt.data['attempt_id'])))):
                    try:
                        stopped = retire_owned_tools(attempt.store, attempt.path, attempt.data)
                    except (OSError, ValueError):
                        stopped = False
                    if stopped:
                        # N-RUN04: the close of the owned area is confirmed through the existing
                        # cleanup path; only the phase clock stops, the request/money record is kept.
                        BudgetLedger(QE / 'chain/request-budget.db').pause_review(attempt.run_id, attempt.task_id, role)
            raise ProviderWait(f'provider-wait: {outage[0]} until {outage[1]:.0f}')
        raise BudgetDenied('executor request refused by budget gate')
    if rc:
        raise subprocess.CalledProcessError(rc, proc.args)
    if cut:
        raise RuntimeError(f'output-truncated: {model} answer was truncated at the effective output cap')
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
    liveness = Liveness(logf, proc, wt)
    while proc.poll() is None:
        wait_pause(min(poll, 60))
        if liveness.stuck():
            log(logf, "GUARD: all liveness signals stopped; continue in the same session")
            stop(proc)
            return 1
    return 0


try:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import model_router as ROUTER  # Stage 10: a single router
except Exception:
    ROUTER = None
OC_LOG = pathlib.Path(os.environ.get("CADRE_OC_LOG", os.path.expanduser("~/.local/share/opencode/log/opencode.log")))  # override for test injection
# 4c-17 QA: account signatures (401/402/insufficient balance) are samples too; otherwise the classifier never sees them and the provider does not close
ERROR_PATTERN = re.compile(r"insufficient (?:credits?|balance|funds)|payment required|invalid api key|unauthorized|\b40[12]\b|"
                        r"Request too large|context.?length|maximum context|too many tokens|exceeded your current quota|"
                        r"Rate limit reached|\b429\b|\b413\b|Cannot connect to API", re.I)
LIMIT_SIGNATURE = re.compile(r'quota|rate.?limit|insufficient|exceeded|max_budget|budget exceeded|too large|context.?length|maximum context|too many tokens|Cannot connect|(?:status(?:code)?|code|http|error)["\'\s:=]*(?:40[12]|413|429)\b|\b40[12]\s+(?:unauthorized|payment|forbidden)|payment required|unauthorized|invalid api key', re.I)


def wt_sessions(wt):
    """opencode session ids opened in this worktree (DB read-only). The detector counts only THIS job's errors;
    in parallel cloud lanes one lane blowing up must not cut another."""
    try:
        import sqlite3
        db = pathlib.Path(ENV("OPENCODE_DB", os.path.expanduser("~/.local/share/opencode/opencode.db")))
        roots = None
        if ENV("CADRE_RUN_ID"):
            roots = session_ids(RunStore(QE).read(ENV("CADRE_RUN_ID")), ENV("CADRE_TASK_ID", "") or "main")
        if not db.exists():
            return roots or set()
        with contextlib.closing(OC.connect(db)) as con:   # B142b: closes on exception too
            con.row_factory = sqlite3.Row
            # The recorded root session receipt is authoritative: even if the DB row is not written yet (or is in another DB) this job's errors are counted.
            ids = (set(roots) | {r["id"] for r in session_rows(con, roots)}) if roots is not None else {r[0] for r in con.execute("select id from session where directory = ? or directory like ?", (str(wt), str(wt).rstrip("/") + "/%"))}
        return ids
    except Exception:
        return set()


def error_burst(window_sec=180, threshold=6, sessions=None, start=0):
    """Error-loop detector: on a permanent error (request too large / quota / rate limit) opencode enters a compaction->retry->error loop.
    ERROR lines within the last `window_sec` of opencode.log are counted; if over the threshold a sample error is returned."""
    try:
        if not OC_LOG.exists():
            return None
        with OC_LOG.open("rb") as f:
            f.seek(max(0, OC_LOG.stat().st_size - 200_000))
            lines = f.read().decode(errors="replace").splitlines()
        now_ts = time.time()
        n, sample, prov, model = 0, "", "", ""
        for l in lines:
            # 4c-17: the header agent and a budget denial are not a provider/model failure
            if "level=ERROR" not in l or "agent=title" in l or "CADRE_BUDGET_DENIED" in l:
                continue
            if sessions is not None:
                match = re.search(r"(?:session\.id|sessionID|session_id)=(\S+)", l)
                if not match or match.group(1) not in sessions:
                    continue  # No identity is not permission to blame another task.
            m = re.match(r"timestamp=(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})", l)
            if not m:
                continue
            ts = time.mktime(time.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M:%S")) - time.timezone
            if ts + 1 > start and now_ts - ts <= window_sec:   # the log stamp is truncated to the second: the start second counts too
                n += 1
                mm = ERROR_PATTERN.search(l)
                if not sample:   # the full sample error text: the classifier sees "402" with its context (4c QA2)
                    code = re.search(r'error\.error\.code=(\d+)', l)
                    em = re.search(r'error\.error(?:\.message)?="([^"]{1,300})', l)
                    sample = " ".join(x for x in (f"status {code.group(1)}" if code else "", em.group(1) if em else "") if x) or (mm.group(0) if mm else "unknown")
                    pm = re.search(r"providerID=(\S+)", l)
                    if pm:
                        prov = pm.group(1)  # the failing provider
                        mm = re.search(r"modelID=(\S+)", l)
                        model = mm.group(1) if mm else ""
        return (n, sample or "unknown", prov, model) if n >= threshold else None
    except Exception:
        return None


SHELL_COMMAND = re.compile(r"^(/\S*/)?(ba|z|da)?sh (-\w+ )*-\w*c ")   # opencode bash tool; LSP servers start directly


def tool_running(pid, threshold_cpu=3.0):
    """Is any opencode child process consuming CPU? During a long tool call like `make check`/pytest
    the model is IDLE and stdout is SILENT; mistaking that for a stall and cutting sends the job into endless repetition.
    Idle helpers like LSP do not count (CPU ~0)."""
    try:
        queue = [str(pid)]; grandchild = []
        while queue:
            out = subprocess.run(["pgrep", "-P", queue.pop()], capture_output=True, text=True).stdout.split()
            grandchild += out; queue += out
        for c in grandchild:
            cpu = subprocess.run(["ps", "-o", "%cpu=", "-p", c], capture_output=True, text=True).stdout.strip()
            if cpu and float(cpu.replace(",", ".")) >= threshold_cpu:
                return True
            # B342: a long test suite sleeps most of the time (%CPU<3); a live shell command (tool) is still running
            command = subprocess.run(["ps", "-o", "command=", "-p", c], capture_output=True, text=True).stdout.strip()
            if SHELL_COMMAND.match(command):
                return True
    except Exception:
        pass
    return False


NEGATIVE_MARK = re.compile(r"^(APPROVAL|BLOCKED)\s*:\s*(absent|not required|unnecessary|no|none|not needed)\b", re.I | re.ASCII)


def _negative(text):
    """T1v3: the planner wrote "APPROVAL: none | ..." (meaning NO approval is needed) — this is not an approval/block marker."""
    return bool(NEGATIVE_MARK.match(str(text or "").strip()))


def is_product_changed(wt, source_sha=None):
    """Did the product change? The working tree is dirty OR there is a committed diff since the source SHA (an agent/checkpoint commit hides the product
    from `git status` — T1v3). Harness files (PRODUCT_EXCLUDED) do not count. True if it cannot be measured (fail-safe: do not say approval)."""
    try:
        if product_dirty(wt):
            return True
        if source_sha:
            out = subprocess.run(["git", "-C", str(wt), "diff", "--name-only", source_sha, "--", ".", *PRODUCT_EXCLUDED],
                                 capture_output=True, text=True, timeout=60)
            return bool(out.stdout.strip()) or out.returncode != 0
        return False
    except Exception:
        return True


def approval_block(wt):
    """G-C13: if no workable item remains in `.tasks.json` (all `BLOCKED: ...`), returns the first APPROVAL question; otherwise "".
    23.09 T1: the executor marked all items BLOCKED and edited the product for 40 min — the decision is the user's, the run does not wait."""
    try:
        items = json.loads((pathlib.Path(wt) / ".tasks.json").read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(items, list) or not items or not all(isinstance(m, dict) for m in items):
        return ""
    def _blocked(m):
        return any(str(m.get(k) or "").strip().upper().startswith("BLOCKED") and not _negative(m.get(k))
                   for k in ("status", "status", "state", "passes"))
    if not all(_blocked(m) for m in items):
        return ""
    for m in items:
        if str(m.get("title") or "").strip().upper().startswith("APPROVAL"):
            return str(m["title"]).strip()
    return str(items[0].get("title") or "approval pending item").strip()


def briefing_approval_question(bp):
    """G-C13 second trigger: the first item starting with "APPROVAL:" in the briefing's ITEMS JSON; "" if none/unreadable."""
    try:
        text = pathlib.Path(bp).read_text(encoding="utf-8")
        m = re.search(r"## ITEMS.*?```json\s*(\[.*?\])\s*```", text, re.S)
        items = json.loads(m.group(1)) if m else []
    except Exception:
        return ""
    for md in items if isinstance(items, list) else []:
        if isinstance(md, dict) and str(md.get("title") or "").strip().upper().startswith("APPROVAL") and not _negative(md.get("title")):
            return str(md["title"]).strip()
    return ""


def approval_decision(wt, bp, product_changed=None, source_sha=None):
    """G-C13: is the run 'waiting for approval'? (1) all items BLOCKED in the executor record -> question (even if the product changed);
    (2) the executor exited with rc=0, the PRODUCT DID NOT CHANGE and the briefing has an APPROVAL item -> question. Otherwise ""."""
    question = approval_block(wt)
    if question:
        return question
    if product_changed is None:
        product_changed = is_product_changed(wt, source_sha)  # T1v3: a committed product is also a change
    return "" if product_changed else briefing_approval_question(bp)


def budget_rejection_text(text):
    """B177: the Claude transport returns a budget denial as plain text ("403 CADRE_BUDGET_DENIED");
    the plan path counted it as "insufficient output" and triggered the Claude breaker, the judge path mislabelled it. A denial is not a failure."""
    m = str(text or "").strip()
    try:
        m = str(result_json(m).get("result") or m).strip()   # in the Claude transport JSON the verdict field is 'result'
    except Exception:
        pass
    return bool(m) and "CADRE_BUDGET_DENIED" in m and len(m) <= 400   # B187: a long text (plan) may contain the phrase; a transport denial is short


ACTIVE_WT = None  # active worktree while the engine runs (for the quota_drop context measure)


def session_context_token(wt):
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


def request_measure(wt=None, base=30000):
    """Request size for chain selection: session context x1.2 (for new context), at least the floor."""
    return max(base, int(session_context_token(wt or ACTIVE_WT) * 1.2))


HANDOVER_LANE_CAP = int(ENV("CADRE_HANDOVER_LANE", "2"))   # Item 2: a lane switches provider at most 2 times
HANDOVER_RUN_CAP = int(ENV("CADRE_HANDOVER_RUN", "5"))     # ... and a run 5 times in total


def handover_counter(increase=False):
    """(lane_handoffs, run_handoffs). Item 2: today's only expensive event was a LOOP — the same job was handed from provider to provider
    and restarted from scratch on each hand-off, burning $1.15 (22 medium jobs). The counter is written to disk: it is not reset even if the engine
    crashes and restarts. Without a record (call outside a run) the counter does nothing and returns (0,0) — the old behaviour."""
    run = ENV("CADRE_RUN_ID", "")
    if not run:
        return (0, 0)
    task = ENV("CADRE_TASK_ID", "") or "main"
    path = QE / "runs" / run / "handover.json"
    try:
        from run_store import FileLock
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(path.with_suffix(".lock")):
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                data = {}
            lanes = data.setdefault("lanes", {})
            if increase:
                lanes[task] = int(lanes.get(task, 0)) + 1
                data["run"] = int(data.get("run", 0)) + 1
                # B110: there was NO fsync here — `write_text` + `replace` could leave an EMPTY or half file
                # on a crash/power cut and the hand-off counter was reset (the very reason the counter exists
                # is that the hand-off cap must survive an engine crash and restart). atomic_json fsyncs.
                from run_store import atomic_json
                atomic_json(path, data)
            return int(lanes.get(task, 0)), int(data.get("run", 0))
    except OSError:
        return (0, 0)


def path_hand_over(existing, logf, reason="", role="impl", forbidden=(), model_handover=True, pending=False):
    """First the same model and session continue via another route (not counted as a hand-off); if the same model has no route, the next model in the chain
    (through the hand-off counter, 4c-17). If none, '' and `path_pending` = current (the budget is awaited). A judge/plan helper
    call does not change model (`model_handover=False`): the independence selection is in acceptance.py."""
    chain_role = 'impl' if role == 'qa' else role
    breath = getattr(ROUTER, 'judge_breath' if chain_role == 'quality' else 'writer_breath', None) if ROUTER else None
    b = budget_json() if breath else None
    insufficient = (lambda m: bool(b) and m != existing and breath(m, b) is not None)
    paths = ROUTER.same_model_paths(existing, chain_role, request_measure(), forbidden=forbidden, itself=pending) if ROUTER else []
    paths = [y for y in paths if not insufficient(y)]
    candidate, round = (paths[0], 'path') if paths else ('', '')
    if not candidate and ROUTER and model_handover:
        next = ROUTER.next_model(existing, chain_role, request_measure(), forbidden=forbidden) or ''
        if next and insufficient(next):
            next = ''
        if next:
            lane, run_handover = handover_counter()
            if lane >= HANDOVER_LANE_CAP or run_handover >= HANDOVER_RUN_CAP:
                log(logf, f"HANDOVER LIMIT (lane {lane}/{HANDOVER_LANE_CAP}, run {run_handover}/{HANDOVER_RUN_CAP}): {existing} -> "
                          f"{next} was not performed; budget-wait")
            else:
                handover_counter(increase=True)
                candidate, round = next, 'model'
    if ENV("CADRE_RUN_ID"):
        def save(task):
            task.setdefault('path_handovers', []).append({'model': existing, 'next': candidate,
                'reason': reason, 'ts': time.time(), 'counted': round == 'model', 'round': round or 'wait'})
            task['path_pending'] = '' if candidate else existing
        try:
            RunStore(QE).task_mutate(ENV("CADRE_RUN_ID"), ENV("CADRE_TASK_ID") or "main", save)
        except (OSError, ValueError, KeyError) as exc:   # an unrecorded caller (helper, unit) still gets the hand-off
            log(logf, f"Could not write the handoff record: {type(exc).__name__}")
    if round == 'model':
        log(logf, f"MODEL HANDOFF (next model, counted): {existing} -> {candidate}; {reason}")
    else:
        log(logf, f"PATH HANDOFF (not counted): {existing} -> {candidate or 'budget-wait'}; same session; {reason}")
    return candidate


CUT_ANSWER = "\nYour final answer was truncated at the output limit. Rewrite it from the start, complete and in one piece."


def is_burst_failure(provider, error):
    return not (provider == "opencode-go" and ROUTER is not None and ROUTER.error_category(error) == "quota")


def quota_drop(existing, logf, reason="", error=""):
    """Main model quota/limit/failure: the (model, route) failure is written, first another route of the same model, otherwise the next model in the chain
    (path_hand_over, 4c-17); when the chain ends, budget-wait (''). Share/window exhaustion and a budget denial are not failures (B52).
    B69: if the reason is the RUN CAP, paid routes are skipped — they all hit the same cap."""
    budget_reason = reason in ("run-cap", "pay", "budget")
    cap_filled = reason in ("run-cap", "budget")
    category = ROUTER.error_category(error) if (ROUTER and error) else ""
    # on Go the limit signature is the window wall (GO1): no failure is written, and since the pool is shared there is no switch to another Go model (4c QA)
    go_window = existing.startswith('opencode-go/') and not reason and category != "account"
    if ROUTER and existing and not budget_reason and not go_window:
        try:   # the Claude quota is account-level; for the others (model, route)
            ROUTER.failure(ROUTER.provider(existing), error or "quota/limit", model=None if existing.startswith("claude/") else existing)
        except Exception:
            pass
    if not budget_reason and existing.startswith("claude/fable"):
        return "claude/opus#high"
    if not budget_reason and existing.startswith("claude/opus"):
        return "claude/sonnet#high"
    if ROUTER and cap_filled:
        z, _ = ROUTER.chain("recovery", "middle", request_measure())   # a provider whose context does not fit (request_cap) does not enter the chain
        # B333/G-B8: filtering by bucket — a free bucket (<prov>:free) does not enter the fill cap, a paid one is filtered.
        from model_catalog import is_free

        def _free(m):
            try:
                return is_free(ROUTER.provider(m), m.split("/", 1)[1].split("#")[0]) is True
            except Exception:
                return False

        def _bucket(m):
            return ROUTER.provider(m) + (":free" if _free(m) else "")

        z = [m for m in z if not m.startswith("claude/") and _bucket(m) != _bucket(existing)
             and (ROUTER.provider(m) not in USD_PROVIDERS or _free(m))]
        nxt = z[0] if z else ""
        log(logf, f"QUOTA/LIMIT ({existing}, reason={reason}) -> {nxt or 'budget-wait'} (router)")
        return nxt
    if ROUTER and existing and (reason == "pay" or existing.startswith("claude/")):
        # share exhausted / Claude ladder exhausted: filtering at provider level (T3); on Go only another route of the same model (GO1)
        return path_hand_over(existing, logf, reason or "quota/limit", forbidden=(ROUTER.provider(existing),),
                          model_handover=not existing.startswith("opencode-go/"))
    if ROUTER and existing:
        return path_hand_over(existing, logf, reason or "quota/limit", model_handover=not (go_window and category in ("quota", "")))
    log(logf, "QUOTA/LIMIT detected -> budget-wait")
    return ""


def recovery_select(forbidden=()):
    """RESCUER candidate (C8/C10): the router's 'recovery' chain × task bans × the shared budget gate; except
    claude. If the budget is unknown the chain comes back empty -> '' (the all-exhausted contract is not broken). A single decision point."""
    return cloud_candidate("recovery", forbidden)


def budget_wait_save(logf):
    if ENV('CADRE_RUN_ID'):
        store = RunStore(QE)
        now = time.time()
        waits = store.read(ENV('CADRE_RUN_ID')).get('budget_waits')
        waits = [w for w in waits if isinstance(w, dict)] if isinstance(waits, list) else []
        store.update(ENV('CADRE_RUN_ID'), reason='budget-wait', retry_after=now + 60, budget_waits=[*waits, {'ts': now, 'until': now + 60}][-200:])
    log(logf, 'BUDGET WAIT: no available cloud capacity; session and product preserved')


def budget_json():
    """Shared fresh observation. Unknown does not preserve a stale authorization."""
    try:
        return observed_budget(QE, CODE)
    except Exception:
        return None


def provider_today(prov):
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


USD_PROVIDERS = ('opencode-go', 'opencode', 'claude', 'openrouter')   # free providers count requests and do not enter the run cap


def provider_brake(b, forbidden=()):
    """Running-call brake uses admission policy, excluding held reservations.
    B57: the brake also works if the run-wide cap fills mid-job (so sub-agents/compaction do not get a 403 on every request)."""
    run_id = ENV("CADRE_RUN_ID")
    if run_id:
        remaining, reason = None, 'run cap exhausted'
        remaining_by_provider = {}
        try:
            remaining_by_provider = {provider: run_remaining_usd(QE, run_id, provider=provider)
                                     for provider in USD_PROVIDERS if provider not in forbidden}
            values = [v for v in remaining_by_provider.values() if v is not None]
            remaining = min(values) if values else None
        except BudgetDenied as exc:  # B70 (T2): B58's fail-closed behaviour was swallowed by a broad except (the brake lifted silently)
            remaining, reason = 0.0, f'run cap unreadable: {exc}'
        except Exception as exc:
            # B114-9: there was `remaining = None` here; because of the `remaining is not None` condition below the run
            # cap brake was lifted SILENTLY. An unreadable cap counts as zero, the reason is recorded.
            remaining, reason = 0.0, f'run cap unreadable: {exc}'
        if remaining is not None and remaining <= 0:
            # B110: this list was hand-written and PAID ZEN ('opencode') was NOT in it; when the run cap
            # was full zen stayed free. Single source USD_PROVIDERS (0c: zen is paid too).
            for provider in USD_PROVIDERS:
                if provider not in forbidden and (not remaining_by_provider or remaining_by_provider.get(provider) == 0):
                    return provider, reason
    for provider in ('opencode',):
        if provider in forbidden:
            continue
        if provider == 'opencode' and not (isinstance(b, dict) and b.get('zen')):
            continue        # without a zen observation zen is not in use; no brake is written for a provider that does not exist
        try:
            if allowance(b, provider)[1] <= 0:
                return provider, 'verified allocation exhausted'
        except BudgetDenied:
            return provider, 'budget unknown'
    return None


def lane_progress(task_id):
    """N-BU06 (FIX-53): a producing lane is a recorded step; if the product digest cannot be read it is written with the task id."""
    try:
        from budget_gate import progress_write, product_id
        record = RunStore(QE).read(ENV("CADRE_RUN_ID"))
        summary = product_id({**record, 'worktree': ENV("CADRE_WORKTREE") or record.get('worktree')}) or 'no-product'
        progress_write(QE, ENV("CADRE_RUN_ID"), 'lane-produced', f"{task_id or 'main'}:{summary}")
    except (OSError, ValueError, KeyError, ImportError):
        pass


def qe_status(task_id, status, reason=""):
    if not task_id and not ENV("CADRE_RUN_ID"):
        return
    if ENV("CADRE_RUN_ID"):
        RunStore(QE).task_update(ENV("CADRE_RUN_ID"), task_id or "main", state=status, reason=reason)
        if status == "produced":
            lane_progress(task_id)
        p = task_root(task_id) / "status.txt"
    else:
        p = QE / "chain/running" / f"{task_id}.status"
    atomic_write(p, f"{status} {reason} {int(time.time())}\n")


def commit_wip(wt, project_ref, message):
    """qe_commit_wip bridge: commit intermediate work (only cadre/* branches, the guard is in common.sh)."""
    try:
        r = subprocess.run(["bash", "-c", '. "$1/lib/common.sh"; qe_commit_wip "$2" "$3" "$4"',
                            'cadre-checkpoint', str(CODE), str(wt), str(project_ref), str(message)],
                           capture_output=True, text=True, timeout=60)
        return r.stdout.strip()
    except Exception:
        return ""


def qe_autopsy(wt, project_ref, logf, cause, real):
    if "CADRE_REAL_OPENCODE" in os.environ:  # in a fake run the autopsy = 300 s of waiting in vain (the cause of the 306 s in E1a/E3a)
        log(logf, f"autopsy skipped (fake opencode, cause={cause})")
        return
    """The model analyzes its own failure when the job stops (bounded, best-effort)."""
    out = pathlib.Path(wt) / "present/audit" / f"AUTOPSY-{time.strftime('%H%M%S')}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        queue = "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", l) for l in pathlib.Path(logf).read_text().splitlines()
                           if l.strip().startswith(("$", "→", "✱")))[-3000:]
        question = (f"You got stuck on a task ({cause}) and the job stopped. Analyze your own failure, "
                f"in at most 5 English lines: 1) What blocked you? 2) Why? 3) What should the next attempt do differently?\n\nLAST ACTIONS:\n{queue}")
        result = capture_opencode(real, wt, [question], 300, role="autopsy")
        if result.strip():
            out.write_text(result)
            log(logf, f"AUTOPSY written: {out}")
        else:
            log(logf, "could not produce the autopsy (skipped)")
    except Exception:
        log(logf, "could not produce the autopsy (skipped)")


def guard_revive():
    """Bring up dead guards at job start (autostart without launchd): health-watch + capacity-watch.
    P1: FROM THE CODE ROOT (CODE/bin), not from the data home. P2: liveness = pidfile+argv+heartbeat (run_store.guard_live), not pgrep -f."""
    return run_guard_revive(QE, CODE)


FORMAT_RED = re.compile(r"would reformat|would be reformatted|code style issues found|run prettier", re.I)   # B206


JUDGE_EXPENSE_ORDER = {"opencode-go": 4, "openrouter": 6, "opencode": 7, "claude": 9}   # Go subscription first, cash after
JUDGE_SMALL_FILE = 10


JUDGE_SMALL_LINE = 300


# the same as the common.sh `qe_commit_wip` EXC list: harness leftovers (QA output, plan ledger, autopsy, pyc)
# are NOT PRODUCT and are not committed — so the "is it clean" question after the delivery commit must exclude them too.
# `test_delivery_commit_pathspec_list_is_the_same_as_common_sh` mechanically catches the two lists diverging.
PRODUCT_EXCLUDED = (':(exclude).tasks.json', ':(exclude).data-ledger.md', ':(exclude)STATE',
              ':(exclude,glob).cadre-plan/**', ':(exclude).qa-outcome.txt', ':(exclude,glob)present/audit/AUTOPSY-*.md',
              ':(exclude,glob)**/__pycache__/**', ':(exclude,glob)**/*.pyc', ':(exclude,glob)**/.pytest_cache/**',
              ':(exclude,glob)**/.mypy_cache/**', ':(exclude,glob)**/.ruff_cache/**')


TEST_PATTERN = re.compile(r'(^|/)(tests?/|spec/|__tests__/|test_[^/]*\.py$|[^/]*_test\.(py|go|js|ts)$|[^/]*\.(test|spec)\.(js|ts|mjs|jsx|tsx)$'
                         r'|conftest\.py$|pytest\.ini$|[^/]*Tests?\.(java|kt|cs)$)', re.I)   # B157: common names


# B144: assertion counter — a rough language-independent measure (Python assert*, JS expect/should, Go t.Error*, testify require/assert).
VERIFICATION_PATTERN = re.compile(r'\bassert\w*\b|\bexpect\s*\(|\bshould\b|\bt\.(?:Error|Errorf|Fatal|Fatalf|Fail|FailNow)\s*\('
                              r'|\brequire\.\w+\s*\(', re.I)
CONSTANT_CORRECT_PATTERN = re.compile(r'\bassert\s+(?:True|1)\s*(?:,|$)|\bassertTrue\(\s*(?:True|1)\s*\)', re.I | re.M)
SKIPPING_PATTERN = re.compile(r'@[\w.]*\b(?:skip\w*|xfail)\b|\bskipTest\(|\bpytest\.skip\(|\bt\.Skip\w*\(', re.I)   # B152


from acceptance import CORRECTION_ROUND_FALLBACK
BROKEN_TEST = re.compile(r"FAILED ([\w./-]+::[\w\[\]-]+)")


# ==================== P2 fast verification, P4 compaction ====================
def fast_verify(wt, files):
    """P2 — a cheap syntax check BEFORE escalating: .py->py_compile, .sh->bash -n, .json->json.loads.
    Catches bad output early. Returns (ok, detail). Types that cannot be checked pass. Needs no model/network."""
    root = pathlib.Path(wt)
    for d in files:
        d = str(d).strip()
        if not d:
            continue
        p = root / d
        if not p.is_file():
            continue
        try:
            if d.endswith(".py"):
                # B16: py_compile wrote __pycache__/*.pyc into the worktree and the LIGHT merge committed them as PRODUCT.
                # The syntax check is done without writing bytecode (ast).
                r = subprocess.run([sys.executable, "-c", "import ast,sys; ast.parse(open(sys.argv[1], 'rb').read(), sys.argv[1])", str(p)],
                                   capture_output=True, text=True, timeout=30)
                if r.returncode != 0:
                    return False, f"{d}: syntax {r.stderr.strip().splitlines()[-1][-160:] if r.stderr.strip() else 'error'}"
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


def compress(text, cap_token=None, task=False):
    """P4 context/prompt compaction. task=False (context): empty lines and repeats are deduped, past the cap the MIDDLE is cut (head+tail kept).
    task=True (C18, task text): no line is deleted, the middle is not cut; only trailing whitespace is trimmed."""
    if not text:
        return text
    if task:
        output, empty = [], 0
        for l in (x.rstrip() for x in text.splitlines()):
            empty = empty + 1 if l == "" else 0
            if empty > 2:
                continue
            output.append(l)
        s = "\n".join(output)
        if cap_token and len(s) > cap_token * 4:
            print(f"compress: task text {len(s)} chars > cap {cap_token*4}; NOT TRUNCATED to preserve meaning", file=sys.stderr)
        return s
    output, empty, previous = [], 0, None
    for l in (x.rstrip() for x in text.splitlines()):
        if l == "":
            empty += 1
            if empty > 1:
                continue
        else:
            empty = 0
        if l == previous and l != "":
            continue
        output.append(l); previous = l
    s = "\n".join(output)
    if cap_token:
        tk = cap_token * 4
        if len(s) > tk:
            start, last = s[: tk // 2], s[-(tk // 2):]
            s = start + f"\n\n[... {len(s) - len(start) - len(last)} context characters compressed ...]\n\n" + last
    return s


from liveness import (
    FastDrop,
    ToolRepeat,
    TestLoop,
    Liveness,
    StallGuard,
)
from acceptance import (
    output_audit,
    attempt_summary,
    is_qa_skipped,
    _format_execute,
    report_qa_gate,
    targeted_qa_command,
    feature_risky,
    qa_gate,
    empty_finish,
    qa_environment,
    entry_shell,
    qa_process as qa_process,
    _base_command,
    base_preparation,
    _base_preparation,
    base_warning,
    changed_files,
    judge_cost,
    is_judge_small,
    product_dirty,
    tree_sha,
    deleted_tests,
    _code_body,
    verification_count,
    _qa_output,
    error_signature,
    correction_decision,
    correction_prompt,
    lane_status,
    scope_audit,
    changed_tests,
    weakened_tests,
    model_family,
    acceptance_assurance,
    assurance_carry,
    estimated_small,
    plan_assurance,
    plan_assurance_leave,
    model_id,
    judge_records,
    falling_judge_save,
    product_writing_models,
    agent_commits,
    request_scope,
    install_traces,
    install_allowed,
    product_difference,
    delivery_commit,
    judge_format_repair,
    finalize_run,
)
from runner import (
    runner,
    _runner,
)

if __name__ == "__main__":
    if sys.argv[1:2] == ["plan-assurance"]:   # 4b: plan reserve at run start (non-blocking)
        plan_assurance(RunStore(QE).path(ENV('CADRE_RUN_ID')) / 'dispatch.log')
        sys.exit(0)
    if sys.argv[1:2] == ["plan-assurance-leave"]:
        plan_assurance_leave()
        sys.exit(0)
    if sys.argv[1:2] == ["assurance"]:
        try:
            acceptance_assurance(RunStore(QE).path(ENV('CADRE_RUN_ID')) / 'dispatch.log')
        except BudgetDenied:
            sys.exit(75)
        sys.exit(0)
    if sys.argv[1:2] == ["finish"] and len(sys.argv) in (4, 5):
        lf = task_root("quality") / "finalize.log"
        lf.parent.mkdir(parents=True, exist_ok=True)
        result = finalize_run(sys.argv[2], sys.argv[3], lf, ENV("CADRE_REAL_OPENCODE", "/opt/homebrew/bin/opencode"),
                              review_model=sys.argv[4] if len(sys.argv) == 5 else None)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0 if result["status"] in ("PASS", "APPLIED") else 1)
    if sys.argv[1:2] == ["claude-plan"] and len(sys.argv) == 5:
        # CADRE_DIFFICULTY: the dispatcher's pre-filter class (medium=3, hard=4); the single source of difficulty while plan.json does not exist yet.
        proc = run_claude(sys.argv[2], sys.argv[3], sys.stdin.read(), sys.argv[4],
                          tools="Read,Grep,Glob", budget="1.00", turns="3", role="plan",
                          difficulty=int(ENV("CADRE_DIFFICULTY", "3")))
        try:
            wait_process(proc, LIMIT.deadline('plan'))
        except CancelledRun:
            sys.exit(130)
        except subprocess.TimeoutExpired:
            sys.exit(1)
        raw = proc.output()
        claude_expense_save(raw, sys.argv[3], sys.argv[4], proc.attempt.data)
        print(result_json(raw).get("result", ""))
        sys.exit(proc.returncode or 0)
    if sys.argv[1:2] == ["correction-decision"] and len(sys.argv) >= 3:  # correction-decision <run_id> [parent] (G-C16)
        _parent = int(sys.argv[3]) if len(sys.argv) > 3 else CORRECTION_ROUND_FALLBACK
        _record = RunStore(QE).read(sys.argv[2])
        _decision, _round, _info = correction_decision(_record, _parent, again=sys.argv[4:5] == ["1"])
        if _decision == "PROCEED":
            RunStore(QE).update(sys.argv[2], correction=[*(_record.get("correction") or []),
                                {"round": _round, "signature": _info, "reason": (_record.get("final_result") or {}).get("reason"), "ts": time.time()}])
            print(f"PROCEED {_round} {_info}")
        else:
            print(f"STOP {_info}")
        sys.exit(0)
    if sys.argv[1:2] == ["correction-prompt"] and len(sys.argv) >= 4:  # correction-prompt <run_id> <round> [parent] (G-C16)
        print(correction_prompt(RunStore(QE).read(sys.argv[2]), sys.argv[3] if sys.argv[3] == "again" else int(sys.argv[3]),
                              int(sys.argv[4]) if len(sys.argv) > 4 else CORRECTION_ROUND_FALLBACK))
        sys.exit(0)
    if sys.argv[1:2] == ["lane-status"] and len(sys.argv) >= 3:  # lane-status <wt> -> JSON (B192)
        print(json.dumps(lane_status(sys.argv[2])))
        sys.exit(0)
    if sys.argv[1:2] == ["scope"] and len(sys.argv) >= 7:  # scope <wt> <plan.json> <gid> <before.json> <backup> (B192)
        try:
            _plan = json.loads(pathlib.Path(sys.argv[3]).read_text())
        except FileNotFoundError:
            _plan = {}
            print(f"WARNING SCOPE ({sys.argv[4]}): plan.json missing; scope audit will use an empty plan (G-C15/B320)")
        _exit, _outside_changed = scope_audit(sys.argv[2], _plan, sys.argv[4],
                                     json.loads(pathlib.Path(sys.argv[5]).read_text()), sys.argv[6])
        if _exit or _outside_changed:
            _old = RunStore(QE).read(ENV("CADRE_RUN_ID"))["tasks"].get(sys.argv[4], {})
            RunStore(QE).task_update(ENV("CADRE_RUN_ID"), sys.argv[4],
                                     scope_outside_fresh=sorted(set(_old.get("scope_outside_fresh") or []) | set(_exit)),
                                     scope_outside_changed=sorted(set(_old.get("scope_outside_changed") or []) | set(_outside_changed)))
        for _y in _exit:
            print(f"OUTSIDE SCOPE ({sys.argv[4]}): removed unplanned new file {_y} from the product (backup: scope-outside/{sys.argv[4]}/{_y})")
        for _y in _outside_changed:
            print(f"OUTSIDE SCOPE ({sys.argv[4]}): changed unplanned existing file {_y}; separate criterion for the judge")
        sys.exit(0)
    if sys.argv[1:2] == ["verify"] and len(sys.argv) >= 4:  # verify <wt> <file1,file2>  -> rc 0/1
        _ok, _det = fast_verify(sys.argv[2], sys.argv[3].split(","))
        if not _ok:
            print(_det, file=sys.stderr)
        sys.exit(0 if _ok else 1)
    if sys.argv[1:2] == ["compress"]:  # compress [--task] [token_cap]: stdin -> stdout
        _a = sys.argv[2:]
        _g = "--task" in _a
        _t = [x for x in _a if x != "--task"]
        print(compress(sys.stdin.read(), int(_t[0]) if _t else None, task=_g)); sys.exit(0)
    if len(sys.argv) >= 4 and sys.argv[1] in ("runner", "lane"):
        if sys.argv[1] == 'runner' and not ENV('CADRE_TASK_ID'):
            os.execv('/bin/bash', ['bash', str(CODE / 'bin/cadre-dispatch.sh'), *sys.argv[2:]])
        sys.exit(runner(sys.argv[2], sys.argv[3]))
    if len(sys.argv) >= 4 and sys.argv[1] == "qa":  # Stage 10: the dispatcher coordinator calls the QA gate once
        _p, _wt = sys.argv[2], sys.argv[3]
        _lf = task_root(ENV("CADRE_TASK_ID", "qa")) / f"qa-{uuid.uuid4().hex}.log"
        _lf.parent.mkdir(parents=True, exist_ok=True)
        _lf.touch()
        result = qa_gate(_p, _wt, _lf, ENV("CADRE_REAL_OPENCODE", "/opt/homebrew/bin/opencode"))
        print(json.dumps(result, ensure_ascii=False)); sys.exit(0 if result["status"] in ("PASS", "APPLIED") else 1)
    print("usage: qe_core.py runner <project_ref> <prompt-file>", file=sys.stderr)
    sys.exit(2)
