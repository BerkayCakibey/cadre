#!/usr/bin/env python3
"""Run ledger: thinking, tool calls, sub-agent sessions and compactions of every opencode run dumped into one folder
(readable transcript.md + a complete machine record); the engine log only holds external decisions."""
import opencode_adaptor as OC
import json
import math
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from run_store import FileLock, RunStore, atomic_json, atomic_write
from worktree_archive import capture, index as archive_index, verify as verify_archive
from session import events, run_for_worktree, session_ids, session_rows

QE = pathlib.Path(os.environ.get("CADRE_HOME", os.path.expanduser("~/.local/share/cadre-engineering")))
DB = pathlib.Path(os.environ.get("OPENCODE_DB", os.path.expanduser("~/.local/share/opencode/opencode.db")))
CFG = pathlib.Path(os.environ.get("OPENCODE_CFG", os.path.expanduser("~/.config/opencode")))
WATERMARK = QE / "reports" / ".run-ledger-watermark"
CUT = {"reasoning": 20000, "text": 20000, "input": 3000, "output": 4000}  # only for .md; .jsonl is NOT truncated


def ts(ms):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ms) / 1000))
    except Exception:
        return str(ms)


# .patch and files/ are PRODUCT copies (restore/apply needs byte-for-byte equality) — not masked; text sinks are masked.
REDACTED_EXTENSION = {".md", ".jsonl", ".log", ".txt", ".json", ".error"}


SECRET_PATTERN = re.compile(
    r'(sk-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{12,}|xox[abprs]-[A-Za-z0-9-]{10,}'
    r'|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----'
    r'|-----BEGIN [A-Z ]*PRIVATE KEY-----[A-Za-z0-9+/=\s\\]*|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})')


def redacted(text):
    """Key patterns are masked in the ledger's plain-text sinks. This is NOT a policy/gate —
    only log hygiene: it keeps a key from being written in plain text to a log file."""
    return SECRET_PATTERN.sub("[REDACTED]", text)


def redacted_directory(out):
    """Mask the text files in the ledger folder. `files/` (the worktree archive) is NOT TOUCHED: restore needs byte-for-byte equality;
    before any public sharing that folder is handled separately (the 'redaction' field in meta.json says so)."""
    n = 0
    for path in out.rglob("*"):
        if not path.is_file() or path.is_symlink() or path.suffix not in REDACTED_EXTENSION:
            continue
        if "files" in path.relative_to(out).parts[:1] or path.name in ("worktree-archive.json",):
            continue
        try:
            raw = path.read_text(errors="replace")
        except OSError:
            continue
        clean = redacted(raw)
        if clean != raw:
            path.write_text(clean)
            n += 1
    return n


def _ms(s):
    try:
        return int(time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S")) * 1000)
    except Exception:
        return 0


def cut(s, n):
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + f"\n…[{len(s) - n} characters cut — the full text is in transcript.jsonl]"


def j(s):
    try:
        return json.loads(s)
    except Exception:
        return {}


def db_ro():
    con = OC.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def children_add(con, rows):
    """sub-agent sessions (task tool) are linked by parent_id — collect them up to the close"""
    ids = {r["id"] for r in rows}
    while ids:
        q = ",".join("?" * len(ids))
        fresh = [r for r in con.execute(f"select * from session where parent_id in ({q})", tuple(ids)).fetchall() if r["id"] not in ids]
        if not fresh:
            break
        rows += fresh
        ids = {r["id"] for r in fresh}
    return sorted(rows, key=lambda r: r["time_created"])


def sessions_for_dir(con, d, project_ref=None):
    record = run_for_worktree(RunStore(QE), str(d), project_ref)
    if record is not None:
        return session_rows(con, session_ids(record))
    rows = con.execute("select * from session where directory = ? or directory like ?", (d, d.rstrip("/") + "/%")).fetchall()
    # observation finding: for registered projects opencode writes the session directory as the MAIN REPO path;
    # sessions started on that path after the worktree opened also belong to this job.
    if project_ref:
        try:
            repo = (QE / "projects" / project_ref / "repo-path").read_text().strip()
            t0 = int(pathlib.Path(d).stat().st_ctime * 1000) - 120000 if pathlib.Path(d).exists() else None
            if t0 is not None:
                ids = {r["id"] for r in rows}
                rows += [r for r in con.execute("select * from session where directory = ? and time_created >= ?", (repo, t0)).fetchall() if r["id"] not in ids]
        except Exception:
            pass
    return children_add(con, sorted(rows, key=lambda r: r["time_created"]))


def config_snapshot(out):
    """The brain in force at that moment: AGENTS.md, agents, commands, skills, opencode.jsonc (keys STRIPPED), engine version."""
    cs = out / "config-snapshot"
    cs.mkdir(exist_ok=True)
    for f in ("AGENTS.md",):
        p = CFG / f
        if p.exists():
            shutil.copy(p, cs / f)
    for d in ("agents", "command", "skills"):
        p = CFG / d
        if p.is_dir():
            shutil.copytree(p, cs / d, dirs_exist_ok=True)
    p = CFG / "opencode.jsonc"
    if p.exists():
        t = re.sub(r'("apiKey"\s*:\s*)"[^"]*"', r'\1"REDACTED"', p.read_text(errors="replace"))
        (cs / "opencode.jsonc").write_text(t)
    core = QE / "lib/qe_core.py"
    version = {"qe_core_mtime": ts(core.stat().st_mtime * 1000) if core.exists() else None}
    try:
        version["opencode"] = subprocess.run(["opencode", "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        pass
    for r in (pathlib.Path(os.environ.get("CADRE_ENGINE_ROOT", str(QE))).resolve().parent,):
        if (r / ".git").exists():
            try:
                version["cadre_sha"] = subprocess.run(["git", "-C", str(r), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=20).stdout.strip()
            except Exception:
                pass
    (cs / "version.json").write_text(json.dumps(version, ensure_ascii=False, indent=1))


LEDGER_SKIP_DIRS = {"__pycache__", "node_modules", ".venv", "venv", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".cache", ".eggs"}


def ledger_skip(relative):
    """B18: the ledger archive is for product+memory; build/cache/dependency directories (uv-cache, node_modules...) were re-digested every 30 min
    and pushed the scan of a lab clone to minutes. The rotation archive (C4) does NOT USE this filter."""
    name = relative.rsplit("/", 1)[-1]
    return name in LEDGER_SKIP_DIRS or "cache" in name.lower()


def git_record(out, wt, base_sha):
    if not pathlib.Path(wt).is_dir():
        return []
    # Includes untracked bytes and task memory; a name-only Git status is not
    # a recoverable product. Failure propagates and prevents watermark advance.
    manifest = capture(pathlib.Path(wt), out/'files', skip=ledger_skip)
    atomic_json(out/'worktree-archive.json', {'manifest':str(manifest)})
    def g(*a):
        try:
            return subprocess.run(["git", "-C", wt, *a], capture_output=True, text=True, timeout=120).stdout
        except Exception as e:
            return f"(git error: {e})"
    if not g("rev-parse", "--is-inside-work-tree").strip():
        return ['worktree-archive.json', 'files/']
    md = ["## branch\n" + g("branch", "--show-current"), "## HEAD\n" + g("rev-parse", "HEAD")]
    if base_sha:
        md += [f"## commits (base {base_sha}..HEAD)\n" + g("log", "--oneline", f"{base_sha}..HEAD"),
               "## changed files\n" + g("diff", "--stat", base_sha, "HEAD")]
        # FULL PATCH — even if the worktree is deleted by rotation, the job's product stays in the ledger
        (out / "change.patch").write_text(g("diff", "--binary", base_sha, "HEAD"))
    else:
        md.append("## last 30 commit\n" + g("log", "--oneline", "-30"))
    md.append("## uncommitted\n" + g("status", "--porcelain"))
    unc = g("diff", "--binary", "HEAD")
    if unc.strip():
        (out / "uncommitted.patch").write_text(unc)
    (out / "git.md").write_text("\n".join(md))
    return ['worktree-archive.json', 'files/', "git.md"] + (["change.patch"] if base_sha else []) + (["uncommitted.patch"] if unc.strip() else [])


WATCHER_LOGS = ["guard.log", "health-watch.log", "quota-trend.log", "rotation.log", "worktrees.log"]
WATCHER_PATTERN = ["watch-*.log", "canary*.log", "dispatch-*.log", "chain-*.log", "orchestrator-*.log", "starting-*.out"]
_TS = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})")


def watcher_slices(out, t0_ms, t1_ms, pay_min=15, project_ref=None):
    """What the guard/hook/watcher scripts went through in the job window (±margin): a time slice from every log.
    A line without a stamp inherits the last seen stamp (like the health watch block headers)."""
    if not t0_ms:
        return []
    t0 = t0_ms / 1000 - pay_min * 60
    t1 = (t1_ms or t0_ms) / 1000 + pay_min * 60
    d = out / "watchers"
    if d.exists():
        shutil.rmtree(d)  # no old slices may remain on regeneration
    d.mkdir()
    write = []
    files = [QE / "logs" / f for f in WATCHER_LOGS]
    for k in WATCHER_PATTERN:
        files += sorted((QE / "logs").glob(k))
    for f in files:
        if not f.exists():
            continue
        try:
            if f.suffix == ".out" or f.name.startswith(("watch-", "dispatch-", "chain-", "orchestrator-", "canary")):
                # file-based: the last write must be inside the job window; project-named logs only for this project
                if not (t0 <= f.stat().st_mtime <= t1):
                    continue
                if f.name.startswith(("dispatch-", "chain-", "orchestrator-", "starting-")) and project_ref and project_ref not in f.name:
                    continue
            last = None
            selected_lines = []
            for l in f.read_text(errors="replace").splitlines():
                m = _TS.search(l[:40])
                if m:
                    try:
                        last = time.mktime(time.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M"))
                    except Exception:
                        pass
                if last is None:
                    continue
                if t0 <= last <= t1:
                    selected_lines.append(l)
            if not selected_lines and (f.name.startswith(("watch-", "dispatch-", "chain-", "orchestrator-")) or f.suffix == ".out"):
                selected_lines = f.read_text(errors="replace").splitlines()  # small file without stamps: all of it
            if selected_lines:
                (d / f.name).write_text("\n".join(selected_lines) + "\n")
                write.append(f.name)
        except Exception as e:
            (d / (f.name + ".error")).write_text(str(e))
    write += external_slices(d, t0, t1)
    return write


# Stage 10 ledger appendices: non-harness sources are also sliced by the job window (zero-loss principle)
OPENCODE_LOG_DIR = pathlib.Path(os.environ.get("OPENCODE_LOG_DIR", os.path.expanduser("~/.local/share/opencode/log")))
_TS_UTC = re.compile(r"timestamp=(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})")


def external_slices(d, t0, t1, line_cap=20000):
    """opencode.log (UTC stamps; provider/error/compaction flow), chain/model_router.jsonl (model selection decisions).
    All best-effort: on error <name>.error."""
    import calendar, json as _json
    write = []
    # opencode.log: timestamp=YYYY-MM-DDTHH:MM:SS.sssZ (UTC) -> epoch
    try:
        selected_lines = []
        for f in sorted(OPENCODE_LOG_DIR.glob("*.log"), key=lambda x: x.stat().st_mtime) if OPENCODE_LOG_DIR.is_dir() else []:
            if f.stat().st_mtime < t0:
                continue
            for l in f.read_text(errors="replace").splitlines():
                m = _TS_UTC.match(l)
                if not m:
                    continue
                try:
                    ts = calendar.timegm(time.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M:%S"))
                except Exception:
                    continue
                if t0 <= ts <= t1:
                    selected_lines.append(l)
        if selected_lines:
            (d / "opencode.log").write_text("\n".join(selected_lines[-line_cap:]) + "\n"); write.append("opencode.log")
    except Exception as e:
        (d / "opencode.log.error").write_text(str(e))
    # router decisions: {"ts": "YYYY-MM-DDTHH:MM:SS", ...} (local time)
    try:
        yj = QE / "chain/model_router.jsonl"
        if yj.exists():
            selected_lines = []
            for l in yj.read_text(errors="replace").splitlines():
                try:
                    ts = time.mktime(time.strptime(_json.loads(l)["ts"][:19], "%Y-%m-%dT%H:%M:%S"))
                except Exception:
                    continue
                if t0 <= ts <= t1:
                    selected_lines.append(l)
            if selected_lines:
                (d / "model_router.jsonl").write_text("\n".join(selected_lines) + "\n"); write.append("model_router.jsonl")
    except Exception as e:
        (d / "model_router.jsonl.error").write_text(str(e))
    return write


def copy_attempt(source: pathlib.Path, destination: pathlib.Path) -> None:
    def writable_directories() -> None:
        if destination.is_symlink():
            raise PermissionError('ledger-attempt-destination-untrusted')
        for directory, children, _ in os.walk(destination, followlinks=False):
            if any((pathlib.Path(directory) / name).is_symlink() for name in children):
                raise PermissionError('ledger-attempt-destination-untrusted')
            pathlib.Path(directory).chmod(0o700, follow_symlinks=False)

    def copy_file(source_file: str, destination_file: str) -> str:
        target = pathlib.Path(destination_file)
        fd, temporary = tempfile.mkstemp(prefix=f'.{target.name}.', dir=target.parent)
        os.close(fd)
        try:
            shutil.copyfile(source_file, temporary)
            os.replace(temporary, destination_file)
        finally:
            pathlib.Path(temporary).unlink(missing_ok=True)
        return destination_file

    writable_directories()
    try:
        shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True, copy_function=copy_file)
    finally:
        writable_directories()


def dump(project_ref, wt, runner_log=None, prompt=None, entry_name=None, sessions=None, run_id=None):
    name = entry_name or (pathlib.Path(wt).name if wt else run_id)
    if not name:
        raise ValueError("ledger requires a worktree or run identity")
    target = QE / "reports" / f"run-{project_ref}-{name}"
    with FileLock(target / ".write.lock"):
        return _dump(project_ref, wt, runner_log, prompt, name, sessions, run_id)


def record_activity(record):
    root = RunStore(QE).path(record["run_id"])
    return max([record["updated"]] + [p.stat().st_mtime for name in ("stdout.jsonl", "attempt.json")
                                     for p in root.glob(f"tasks/*/attempts/*/{name}")])


def _dump(project_ref, wt, runner_log=None, prompt=None, entry_name=None, sessions=None, run_id=None):
    wt = str(pathlib.Path(wt).expanduser()) if wt else ""
    entry_name = entry_name or pathlib.Path(wt).name
    out = QE / "reports" / f"run-{project_ref}-{entry_name}"
    out.mkdir(parents=True, exist_ok=True)
    record = RunStore(QE).read(run_id) if run_id else (run_for_worktree(RunStore(QE), wt, project_ref) if sessions is None else None)
    if record and (record["project"] != project_ref or record["worktree"] != wt):
        raise ValueError("ledger identity does not match run")
    activity = record_activity(record) if record else None
    meta = {"project_ref": project_ref, "directory": wt, "production": time.strftime("%Y-%m-%d %H:%M:%S"), "last_update_ms": 0, "sessions": [],
            "tool_counter": {}, "compaction": 0, "permission_rejection": 0, "error_tool": 0, "tool_output_cut": 0, "models": {},
            "total": {"input": 0, "output": 0, "reasoning": 0, "cost": 0.0}}
    md = [f"# RUN LEDGER — {project_ref} / {entry_name}", f"production: {meta['production']}  ·  directory: `{wt}`", ""]
    jl = []

    if not DB.exists():
        md.append(f"WARNING: opencode DB absent: {DB}")
    else:
        con = db_ro()
        rows = sessions if sessions is not None else (session_rows(con, session_ids(record)) if record else sessions_for_dir(con, wt, project_ref))
        if not rows:
            md.append("WARNING: no opencode session found for this directory (has it not started yet?)")
        for s in rows:
            model = j(s["model"]) if s["model"] else {}
            mname = f"{model.get('providerID', '?')}/{model.get('id', '?')}" if model else "?"
            so = {"id": s["id"], "parent": s["parent_id"], "agent": s["agent"], "model": mname, "title": s["title"],
                  "start": ts(s["time_created"]), "last": ts(s["time_updated"]),
                  "tokens": {"input": s["tokens_input"], "output": s["tokens_output"], "reasoning": s["tokens_reasoning"], "cache_read": s["tokens_cache_read"]},
                  "cost": round(s["cost"] or 0, 4), "message": 0, "tool": 0}
            meta["last_update_ms"] = max(meta["last_update_ms"], int(s["time_updated"] or 0))
            meta["models"][mname] = meta["models"].get(mname, 0) + 1
            for k in ("input", "output", "reasoning"):
                meta["total"][k] += so["tokens"][k] or 0
            meta["total"]["cost"] += so["cost"]
            role = "SUB-AGENT" if s["parent_id"] else "MAIN"
            md += ["", f"## [{role}] session {s['id']} · agent={s['agent']} · model={mname}",
                   f"title: {s['title']}  ·  {so['start']} → {so['last']}  ·  tokens in/out/reason={so['tokens']['input']}/{so['tokens']['output']}/{so['tokens']['reasoning']}  ·  cost=${so['cost']}",
                   (f"parent: {s['parent_id']}" if s["parent_id"] else ""), ""]
            msgs = con.execute("select * from message where session_id=? order by time_created", (s["id"],)).fetchall()
            for m in msgs:
                md_ = j(m["data"])
                so["message"] += 1
                role_model = md_.get("role", "?")
                label = role_model.upper()
                if role_model == "assistant":
                    label += f" · {md_.get('agent', '')} · {md_.get('providerID', '')}/{md_.get('modelID', '')}"
                    if md_.get("finish"):
                        label += f" · finish={md_['finish']}"
                md.append(f"### {ts(m['time_created'])} {label}")
                parts = con.execute("select * from part where message_id=? order by time_created", (m["id"],)).fetchall()
                for p in parts:
                    d = j(p["data"])
                    t = d.get("type")
                    rec = {"session": s["id"], "message": m["id"], "role": role_model, "agent": md_.get("agent"),
                           "model": f"{md_.get('providerID', '')}/{md_.get('modelID', '')}", "time": ts(p["time_created"]), "time_ms": p["time_created"], "type": t}
                    if t == "reasoning":
                        md.append("🧠 THINKING:\n" + "\n".join("> " + l for l in cut(d.get("text", ""), CUT["reasoning"]).splitlines()))
                        rec["text"] = d.get("text", "")
                    elif t == "text":
                        md.append(cut(d.get("text", ""), CUT["text"]))
                        rec["text"] = d.get("text", "")
                    elif t == "tool":
                        st = d.get("state", {}) or {}
                        so["tool"] += 1
                        meta["tool_counter"][d.get("tool")] = meta["tool_counter"].get(d.get("tool"), 0) + 1
                        status = st.get("status")
                        err = st.get("error", "")
                        if status == "error":
                            meta["error_tool"] += 1
                            if "permission" in (err or "").lower():
                                meta["permission_rejection"] += 1
                        if (st.get("metadata") or {}).get("truncated"):
                            meta["tool_output_cut"] += 1
                        md.append(f"🔧 TOOL `{d.get('tool')}` [{status}] {st.get('title', '')}")
                        md.append("```json\n" + cut(st.get("input", {}), CUT["input"]) + "\n```")
                        if err:
                            md.append(f"ERROR: {cut(err, 1000)}")
                        if st.get("output"):
                            md.append("```\n" + cut(st.get("output", ""), CUT["output"]) + "\n```")
                        rec.update({"tool": d.get("tool"), "status": status, "input": st.get("input"), "output": st.get("output"),
                                    "error": err, "title": st.get("title"), "metadata": st.get("metadata")})
                    elif t == "patch":
                        md.append(f"📝 PATCH {(d.get('hash') or '')[:8]} files: {d.get('files')}")
                        rec.update({"hash": d.get("hash"), "files": d.get("files")})
                    elif t == "compaction":
                        meta["compaction"] += 1
                        md.append(f"♻️ COMPACTION (auto={d.get('auto')}, overflow={d.get('overflow')}) — context compacted")
                        rec["data"] = d
                    elif t == "step-finish":
                        tk = d.get("tokens", {}) or {}
                        md.append(f"— step finished: reason={d.get('reason')} tokens total={tk.get('total')} in={tk.get('input')} out={tk.get('output')} reason={tk.get('reasoning')} cost={d.get('cost')}")
                        rec.update({"reason": d.get("reason"), "tokens": tk, "cost": d.get("cost"), "snapshot": d.get("snapshot")})
                    elif t == "step-start":
                        rec["snapshot"] = d.get("snapshot")
                    else:
                        md.append(f"· part {t}: {cut(d, 500)}")
                        rec["data"] = d
                    jl.append(rec)
                md.append("")
            meta["sessions"].append(so)
        con.close()

    extras = []
    if record:
        meta["run_id"] = record["run_id"]
        meta["run_updated"] = record["updated"]
        meta["archive_input_mtime"] = activity
        root = RunStore(QE).path(record["run_id"])
        atomic_json(out / "run.json", record)
        extras.append("run.json")
        meta["claude_sessions"] = []
        for task in sorted((root / "tasks").glob("*")):
            for attempt in sorted((task / "attempts").glob("*")):
                if not (attempt / "attempt.json").is_file():
                    continue
                receipt = json.loads((attempt / "attempt.json").read_text())
                dest = out / "attempts" / task.name / attempt.name
                copy_attempt(attempt, dest)
                if receipt["adapter"] != "claude":
                    continue
                summary = {k: receipt.get(k) for k in ("attempt_id", "session_id", "model", "role", "outcome", "exit_code")}
                summary["task_id"] = task.name
                summary["usd"] = None  # Missing receipt is unknown, not a free invocation.
                for event in events(attempt / "stdout.jsonl"):
                    jl.append({"adapter": "claude", "run_id": record["run_id"], "task_id": task.name, "attempt_id": attempt.name, "event": event})
                    if event.get("type") == "assistant":
                        for part in (event.get("message") or {}).get("content", []):
                            if isinstance(part, dict) and part.get("type") == "tool_use":
                                tool = part.get("name", "?")
                                meta["tool_counter"][tool] = meta["tool_counter"].get(tool, 0) + 1
                                md.append(f"🔧 Claude {task.name} TOOL `{tool}`\n```json\n" + cut(part.get("input", {}), CUT["input"]) + "\n```")
                    if event.get("type") == "result":
                        cost = event.get("total_cost_usd", event.get("cost_usd"))
                        if isinstance(cost, (int, float)) and math.isfinite(cost) and cost >= 0:
                            summary["usd"] = cost
                        summary["usage"] = event.get("usage")
                        md.append(f"## Claude {task.name} / {receipt.get('session_id')}\n" + cut(event.get("result", ""), CUT["text"]))
                if summary["usd"] is not None:
                    meta["total"]["cost"] += summary["usd"]
                else:
                    meta["claude_cost_unknown"] = meta.get("claude_cost_unknown", 0) + 1
                meta["models"][receipt["model"]] = meta["models"].get(receipt["model"], 0) + 1
                meta["claude_sessions"].append(summary)
        if (out / "attempts").exists():
            extras.append("attempts/")
        prompt = prompt or record["prompt"]
        runner_log = runner_log or str(root / "coordinator.log")
    if runner_log and pathlib.Path(runner_log).exists():
        shutil.copy(runner_log, out / "runner.log"); extras.append("runner.log")
    if prompt and pathlib.Path(prompt).exists():
        shutil.copy(prompt, out / "task-prompt.md"); extras.append("task-prompt.md")
    base_sha = None
    wl = QE / "logs/worktrees.log"
    if wt and wl.exists():
        lines = wl.read_text(errors="replace").splitlines()
        language = []; last_base = None
        for i, l in enumerate(lines):
            if "base_sha=" in l:
                last_base = l.split("base_sha=")[1].split()[0]
            if pathlib.Path(wt).name in l:
                language.append(l)
                if "worktree=" in l and base_sha is None:
                    base_sha = last_base  # the base line does not carry the worktree name; take it from the block
                    if i > 0 and "base_sha=" in lines[i - 1]:
                        language.insert(len(language) - 1, lines[i - 1])
        if language:
            (out / "worktree-record.txt").write_text("\n".join(language) + "\n"); extras.append("worktree-record.txt")
    for f in (() if record else (f"chain/{project_ref}-task-status.json", f"chain/{project_ref}.json")):
        pth = QE / f
        if pth.exists():
            shutil.copy(pth, out / pathlib.Path(f).name); extras.append(pathlib.Path(f).name)
    for f in ("assignment-history.jsonl", "category-history.jsonl", "claude-spend.jsonl"):
        pth = QE / "chain" / f
        if pth.exists():
            language = [l for l in pth.read_text(errors="replace").splitlines() if (record["run_id"] in l if record else project_ref in l or entry_name in l)]
            if language:
                (out / f).write_text("\n".join(language[-200:]) + "\n"); extras.append(f)
    if wt:
        extras += git_record(out, wt, base_sha)
    config_snapshot(out); extras.append("config-snapshot/")
    # WATCHERS: what the guard/hook/quota/watch/rotation scripts went through in the job window
    t0 = min([o_ms for o_ms in (_ms(o["start"]) for o in meta["sessions"]) if o_ms] or [0])
    t1 = meta["last_update_ms"]
    if runner_log and pathlib.Path(runner_log).exists():
        st = pathlib.Path(runner_log).stat()
        t0 = min(t0 or int(st.st_birthtime * 1000), int(st.st_birthtime * 1000)) if hasattr(st, "st_birthtime") else t0
        t1 = max(t1, int(st.st_mtime * 1000))
    trace = watcher_slices(out, t0, t1, project_ref=project_ref)
    meta["watchers"] = trace
    if trace:
        extras.append("watchers/(" + ", ".join(trace) + ")")

    meta["total"]["cost"] = round(meta["total"]["cost"], 4)
    meta["extras"] = extras
    md.insert(3, "## SUMMARY\n" + "\n".join([
        f"- OpenCode session: {len(meta['sessions'])} (sub-agent: {sum(1 for o in meta['sessions'] if o['parent'])}); Claude attempt: {len(meta.get('claude_sessions', []))}",
        f"- models: {meta['models']}",
        f"- tool calls: {sum(meta['tool_counter'].values())} → {meta['tool_counter']}",
        f"- failed tools: {meta['error_tool']} (permission denials: {meta['permission_rejection']})  ·  compaction: {meta['compaction']}  ·  tool output truncated at the source: {meta['tool_output_cut']}",
        f"- tokens in/out/reason: {meta['total']['input']}/{meta['total']['output']}/{meta['total']['reasoning']}  ·  cost=${meta['total']['cost']}",
        f"- the token total covers OpenCode; Claude usage is in the attempt record. Claude attempts with unknown cost: {meta.get('claude_cost_unknown', 0)}",
        f"- extras: {', '.join(extras) or '-'}",
        "- NOTE: the .md is shortened for readability; transcript.jsonl is COMPLETE.",
    ]) + "\n")
    (out / "transcript.md").write_text("\n".join(md) + "\n")
    with open(out / "transcript.jsonl", "w") as f:
        for r in jl:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    meta["redaction"] = {"masked_file": redacted_directory(out), "scope": "text sinks; files/ (worktree archive) is raw"}
    atomic_json(out / "meta.json", meta)
    return out


def project_ref_find(d):
    """directory -> (project, ledger name, is worktree). Worktree: QE/worktrees/<p>/<name>; registered repo/scratch: projects/*/repo-path."""
    d = str(d)
    wroot = str(QE / "worktrees") + "/"
    if d.startswith(wroot):
        rest = d[len(wroot):].split("/")
        if len(rest) >= 2:
            return rest[0], rest[1], str(QE / "worktrees" / rest[0] / rest[1])
    pr = QE / "projects"
    if pr.is_dir():
        for p in pr.iterdir():
            rp = (p / "repo-path")
            if rp.exists():
                r = rp.read_text().strip()
                if d == r or d.startswith(r.rstrip("/") + "/"):
                    return p.name, None, d
    return "other", None, d


def home_dir_start_write(managed):
    start = min((float(r['created']) for r in managed if isinstance(r.get('created'), (int, float))), default=time.time())
    WATERMARK.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(WATERMARK, str(int(start * 1000)))


def scan():
    with FileLock(WATERMARK.with_suffix('.lock')):
        return _scan()


def _scan():
    """Write all sessions updated since the last scan to the ledger. Worktree sessions to the job ledger,
    others (TUI inside the repo / manual run / another directory) to a separate per-session ledger. Output: number of ledgers written."""
    managed = RunStore(QE).list()
    n = 0
    failed = False
    for record in managed:
        name = pathlib.Path(record['worktree']).name if record.get('worktree') else record['run_id']
        target = QE / "reports" / f"run-{record['project']}-{name}"
        try:
            archived = json.loads((target / "meta.json").read_text()) if (target / "meta.json").exists() else {}
            tree_changed = False
            if record.get('worktree') and pathlib.Path(record['worktree']).exists():
                try:
                    receipt = json.loads((target/'worktree-archive.json').read_text())
                    tree_changed = verify_archive(pathlib.Path(receipt['manifest'])) != archive_index(pathlib.Path(record['worktree']), ledger_skip)
                except (OSError, ValueError, KeyError):
                    tree_changed = True
            if tree_changed or record_activity(record) > archived.get("archive_input_mtime", 0):
                dump(record["project"], record["worktree"], run_id=record["run_id"])
                n += 1
        except Exception as e:
            failed = True
            print(f"ledger error {record['run_id']}: {e}", file=sys.stderr)
    if not DB.exists():
        if failed:
            raise OSError('one or more run ledgers failed')
        print("opencode DB absent", file=sys.stderr)
        return n
    if not WATERMARK.exists():
        home_dir_start_write(managed)
    try:
        wm = int(WATERMARK.read_text().strip()) if WATERMARK.exists() else 0
    except Exception:
        wm = 0  # broken watermark = scan from scratch (no loss, only rewriting)
    con = db_ro()
    current = con.execute("select * from session where time_updated > ? order by time_updated", (wm,)).fetchall()
    if not current:
        con.close()
        if failed:
            raise OSError('one or more run ledgers failed')
        return n
    fresh_wm = max(int(r["time_updated"]) for r in current)
    # if a sub-agent session was updated, go up to the root session (the ledger opens by the root directory)
    def root(r):
        while r["parent_id"]:
            p = con.execute("select * from session where id=?", (r["parent_id"],)).fetchone()
            if not p:
                break
            r = p
        return r
    jobs = {}
    owners = {sid: record for record in managed for sid in session_ids(record)}
    for r in current:
        r = root(r)
        if r["id"] in owners:
            record = owners[r["id"]]
            wt = record["worktree"]
            if wt:
                jobs[(record["project"], pathlib.Path(wt).name, wt)] = None
            continue
        project_ref, entry_name, directory = project_ref_find(r["directory"])
        if run_for_worktree(RunStore(QE), directory, project_ref):
            entry_name = None  # Unowned root in a managed directory gets its own legacy ledger.
        if entry_name:  # worktree: the whole job is one ledger
            jobs[(project_ref, entry_name, directory)] = None
        else:   # single-session ledger
            jobs[(project_ref, f"ses-{time.strftime('%Y%m%d-%H%M', time.localtime(r['time_created'] / 1000))}-{r['id'][-6:]}", directory)] = r["id"]
    con.close()
    for (project_ref, entry_name, directory), sid in jobs.items():
        source = pathlib.Path(directory).resolve()
        if source == QE.resolve() or source in QE.resolve().parents:
            # B276: the archive of a session opened in/above the QE home stays inside the source and cannot be archived; skipped, the scan continues.
            print(f"ledger skipped {project_ref}/{entry_name}: the archive would sit inside its source ({directory})", file=sys.stderr)
            continue
        try:
            if sid:
                c = db_ro()
                rows = children_add(c, [c.execute("select * from session where id=?", (sid,)).fetchone()])
                c.close()
                dump(project_ref, directory, entry_name=entry_name, sessions=rows)
            else:
                # engine log: job-*.log matching the worktree name (the engine writes the worktree on its FINISHED line)
                ml = None
                for f in sorted((QE / "logs").glob(f"job-{project_ref}-*.log"), reverse=True):
                    try:
                        if entry_name in f.read_text(errors="replace"):
                            ml = str(f); break
                    except Exception:
                        pass
                dump(project_ref, directory, runner_log=ml, entry_name=entry_name)
            n += 1
        except Exception as e:
            failed = True
            print(f"ledger error {project_ref}/{entry_name}: {e}", file=sys.stderr)
    if failed:
        raise OSError('one or more ledgers failed; watermark retained')
    WATERMARK.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(WATERMARK, str(fresh_wm))
    return n


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "scan":
        print(f"run ledger scan: {scan()} ledger(s) written/renewed")
        sys.exit(0)
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr); sys.exit(2)
    print(dump(sys.argv[1], sys.argv[2], *(sys.argv[3:5])))
