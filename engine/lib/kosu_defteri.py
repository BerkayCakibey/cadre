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
from kosu import FileLock, RunStore, atomic_json, atomic_write
from arsiv import capture, index as archive_index, verify as verify_archive
from oturum import events, run_for_worktree, session_ids, session_rows

QE = pathlib.Path(os.environ.get("CADRE_HOME", os.path.expanduser("~/.local/share/cadre-engineering")))
DB = pathlib.Path(os.environ.get("OPENCODE_DB", os.path.expanduser("~/.local/share/opencode/opencode.db")))
CFG = pathlib.Path(os.environ.get("OPENCODE_CFG", os.path.expanduser("~/.config/opencode")))
WATERMARK = QE / "reports" / ".kosu-defteri-watermark"
KES = {"reasoning": 20000, "text": 20000, "input": 3000, "output": 4000}  # only for .md; .jsonl is NOT truncated


def ts(ms):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ms) / 1000))
    except Exception:
        return str(ms)


# .patch and files/ are PRODUCT copies (restore/apply needs byte-for-byte equality) — not masked; text sinks are masked.
REDAKTE_UZANTI = {".md", ".jsonl", ".log", ".txt", ".json", ".hata"}


SIR_KALIBI = re.compile(
    r'(sk-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{12,}|xox[abprs]-[A-Za-z0-9-]{10,}'
    r'|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----'
    r'|-----BEGIN [A-Z ]*PRIVATE KEY-----[A-Za-z0-9+/=\s\\]*|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})')


def redakte(text):
    """Key patterns are masked in the ledger's plain-text sinks. This is NOT a policy/gate —
    only log hygiene: it keeps a key from being written in plain text to a log file."""
    return SIR_KALIBI.sub("[REDACTED]", text)


def redakte_dizin(out):
    """Mask the text files in the ledger folder. `files/` (the worktree archive) is NOT TOUCHED: restore needs byte-for-byte equality;
    before any public sharing that folder is handled separately (the 'redaksiyon' field in meta.json says so)."""
    n = 0
    for path in out.rglob("*"):
        if not path.is_file() or path.is_symlink() or path.suffix not in REDAKTE_UZANTI:
            continue
        if "files" in path.relative_to(out).parts[:1] or path.name in ("worktree-archive.json",):
            continue
        try:
            raw = path.read_text(errors="replace")
        except OSError:
            continue
        clean = redakte(raw)
        if clean != raw:
            path.write_text(clean)
            n += 1
    return n


def _ms(s):
    try:
        return int(time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S")) * 1000)
    except Exception:
        return 0


def kes(s, n):
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + f"\n…[{len(s) - n} karakter kesildi — tamami transcript.jsonl'de]"


def j(s):
    try:
        return json.loads(s)
    except Exception:
        return {}


def db_ro():
    con = OC.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def cocuklari_ekle(con, rows):
    """sub-agent sessions (task tool) are linked by parent_id — collect them up to the close"""
    ids = {r["id"] for r in rows}
    while ids:
        q = ",".join("?" * len(ids))
        yeni = [r for r in con.execute(f"select * from session where parent_id in ({q})", tuple(ids)).fetchall() if r["id"] not in ids]
        if not yeni:
            break
        rows += yeni
        ids = {r["id"] for r in yeni}
    return sorted(rows, key=lambda r: r["time_created"])


def sessions_for_dir(con, d, proje=None):
    record = run_for_worktree(RunStore(QE), str(d), proje)
    if record is not None:
        return session_rows(con, session_ids(record))
    rows = con.execute("select * from session where directory = ? or directory like ?", (d, d.rstrip("/") + "/%")).fetchall()
    # observation finding: for registered projects opencode writes the session directory as the MAIN REPO path;
    # sessions started on that path after the worktree opened also belong to this job.
    if proje:
        try:
            repo = (QE / "projects" / proje / "repo-path").read_text().strip()
            t0 = int(pathlib.Path(d).stat().st_ctime * 1000) - 120000 if pathlib.Path(d).exists() else None
            if t0 is not None:
                ids = {r["id"] for r in rows}
                rows += [r for r in con.execute("select * from session where directory = ? and time_created >= ?", (repo, t0)).fetchall() if r["id"] not in ids]
        except Exception:
            pass
    return cocuklari_ekle(con, sorted(rows, key=lambda r: r["time_created"]))


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
    surum = {"qe_core_mtime": ts(core.stat().st_mtime * 1000) if core.exists() else None}
    try:
        surum["opencode"] = subprocess.run(["opencode", "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        pass
    for r in (pathlib.Path(os.environ.get("CADRE_ENGINE_ROOT", str(QE))).resolve().parent,):
        if (r / ".git").exists():
            try:
                surum["cadre_sha"] = subprocess.run(["git", "-C", str(r), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=20).stdout.strip()
            except Exception:
                pass
    (cs / "surum.json").write_text(json.dumps(surum, ensure_ascii=False, indent=1))


LEDGER_SKIP_DIRS = {"__pycache__", "node_modules", ".venv", "venv", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".cache", ".eggs"}


def defter_atla(relative):
    """B18: the ledger archive is for product+memory; build/cache/dependency directories (uv-cache, node_modules...) were re-digested every 30 min
    and pushed the scan of a lab clone to minutes. The rotation archive (C4) does NOT USE this filter."""
    name = relative.rsplit("/", 1)[-1]
    return name in LEDGER_SKIP_DIRS or "cache" in name.lower()


def git_kaydi(out, wt, base_sha):
    if not pathlib.Path(wt).is_dir():
        return []
    # Includes untracked bytes and task memory; a name-only Git status is not
    # a recoverable product. Failure propagates and prevents watermark advance.
    manifest = capture(pathlib.Path(wt), out/'files', skip=defter_atla)
    atomic_json(out/'worktree-archive.json', {'manifest':str(manifest)})
    def g(*a):
        try:
            return subprocess.run(["git", "-C", wt, *a], capture_output=True, text=True, timeout=120).stdout
        except Exception as e:
            return f"(git hata: {e})"
    if not g("rev-parse", "--is-inside-work-tree").strip():
        return ['worktree-archive.json', 'files/']
    md = ["## dal\n" + g("branch", "--show-current"), "## HEAD\n" + g("rev-parse", "HEAD")]
    if base_sha:
        md += [f"## commitler (base {base_sha}..HEAD)\n" + g("log", "--oneline", f"{base_sha}..HEAD"),
               "## degisen dosyalar\n" + g("diff", "--stat", base_sha, "HEAD")]
        # FULL PATCH — even if the worktree is deleted by rotation, the job's product stays in the ledger
        (out / "degisiklik.patch").write_text(g("diff", "--binary", base_sha, "HEAD"))
    else:
        md.append("## son 30 commit\n" + g("log", "--oneline", "-30"))
    md.append("## commit'lenmemis\n" + g("status", "--porcelain"))
    unc = g("diff", "--binary", "HEAD")
    if unc.strip():
        (out / "commitlenmemis.patch").write_text(unc)
    (out / "git.md").write_text("\n".join(md))
    return ['worktree-archive.json', 'files/', "git.md"] + (["degisiklik.patch"] if base_sha else []) + (["commitlenmemis.patch"] if unc.strip() else [])


IZLEYICI_LOGLAR = ["guard.log", "saglik-nobet.log", "kota-gidisat.log", "rotasyon.log", "worktrees.log"]
IZLEYICI_KALIP = ["nobet-*.log", "kanarya*.log", "dispatch-*.log", "zincir-*.log", "orkestra-*.log", "baslatma-*.out"]
_TS = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})")


def izleyici_dilimleri(out, t0_ms, t1_ms, pay_dk=15, proje=None):
    """What the guard/hook/watcher scripts went through in the job window (±margin): a time slice from every log.
    A line without a stamp inherits the last seen stamp (like the health watch block headers)."""
    if not t0_ms:
        return []
    t0 = t0_ms / 1000 - pay_dk * 60
    t1 = (t1_ms or t0_ms) / 1000 + pay_dk * 60
    d = out / "izleyiciler"
    if d.exists():
        shutil.rmtree(d)  # no old slices may remain on regeneration
    d.mkdir()
    yaz = []
    dosyalar = [QE / "logs" / f for f in IZLEYICI_LOGLAR]
    for k in IZLEYICI_KALIP:
        dosyalar += sorted((QE / "logs").glob(k))
    for f in dosyalar:
        if not f.exists():
            continue
        try:
            if f.suffix == ".out" or f.name.startswith(("nobet-", "dispatch-", "zincir-", "orkestra-", "kanarya")):
                # file-based: the last write must be inside the job window; project-named logs only for this project
                if not (t0 <= f.stat().st_mtime <= t1):
                    continue
                if f.name.startswith(("dispatch-", "zincir-", "orkestra-", "baslatma-")) and proje and proje not in f.name:
                    continue
            son = None
            sec = []
            for l in f.read_text(errors="replace").splitlines():
                m = _TS.search(l[:40])
                if m:
                    try:
                        son = time.mktime(time.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M"))
                    except Exception:
                        pass
                if son is None:
                    continue
                if t0 <= son <= t1:
                    sec.append(l)
            if not sec and (f.name.startswith(("nobet-", "dispatch-", "zincir-", "orkestra-")) or f.suffix == ".out"):
                sec = f.read_text(errors="replace").splitlines()  # small file without stamps: all of it
            if sec:
                (d / f.name).write_text("\n".join(sec) + "\n")
                yaz.append(f.name)
        except Exception as e:
            (d / (f.name + ".hata")).write_text(str(e))
    yaz += harici_dilimler(d, t0, t1)
    return yaz


# Stage 10 ledger appendices: non-harness sources are also sliced by the job window (zero-loss principle)
OPENCODE_LOG_DIR = pathlib.Path(os.environ.get("OPENCODE_LOG_DIR", os.path.expanduser("~/.local/share/opencode/log")))
_TS_UTC = re.compile(r"timestamp=(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})")


def harici_dilimler(d, t0, t1, satir_tavan=20000):
    """opencode.log (UTC stamps; provider/error/compaction flow), zincir/yonlendirici.jsonl (model selection decisions).
    All best-effort: on error <name>.hata."""
    import calendar, json as _json
    yaz = []
    # opencode.log: timestamp=YYYY-MM-DDTHH:MM:SS.sssZ (UTC) -> epoch
    try:
        sec = []
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
                    sec.append(l)
        if sec:
            (d / "opencode.log").write_text("\n".join(sec[-satir_tavan:]) + "\n"); yaz.append("opencode.log")
    except Exception as e:
        (d / "opencode.log.hata").write_text(str(e))
    # router decisions: {"ts": "YYYY-MM-DDTHH:MM:SS", ...} (local time)
    try:
        yj = QE / "zincir/yonlendirici.jsonl"
        if yj.exists():
            sec = []
            for l in yj.read_text(errors="replace").splitlines():
                try:
                    ts = time.mktime(time.strptime(_json.loads(l)["ts"][:19], "%Y-%m-%dT%H:%M:%S"))
                except Exception:
                    continue
                if t0 <= ts <= t1:
                    sec.append(l)
            if sec:
                (d / "yonlendirici.jsonl").write_text("\n".join(sec) + "\n"); yaz.append("yonlendirici.jsonl")
    except Exception as e:
        (d / "yonlendirici.jsonl.hata").write_text(str(e))
    return yaz


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


def dump(proje, wt, motor_log=None, prompt=None, ad=None, sessions=None, run_id=None):
    name = ad or (pathlib.Path(wt).name if wt else run_id)
    if not name:
        raise ValueError("ledger requires a worktree or run identity")
    target = QE / "reports" / f"kosu-{proje}-{name}"
    with FileLock(target / ".write.lock"):
        return _dump(proje, wt, motor_log, prompt, name, sessions, run_id)


def record_activity(record):
    root = RunStore(QE).path(record["run_id"])
    return max([record["updated"]] + [p.stat().st_mtime for name in ("stdout.jsonl", "attempt.json")
                                     for p in root.glob(f"tasks/*/attempts/*/{name}")])


def _dump(proje, wt, motor_log=None, prompt=None, ad=None, sessions=None, run_id=None):
    wt = str(pathlib.Path(wt).expanduser()) if wt else ""
    ad = ad or pathlib.Path(wt).name
    out = QE / "reports" / f"kosu-{proje}-{ad}"
    out.mkdir(parents=True, exist_ok=True)
    record = RunStore(QE).read(run_id) if run_id else (run_for_worktree(RunStore(QE), wt, proje) if sessions is None else None)
    if record and (record["project"] != proje or record["worktree"] != wt):
        raise ValueError("ledger identity does not match run")
    activity = record_activity(record) if record else None
    meta = {"proje": proje, "dizin": wt, "uretim": time.strftime("%Y-%m-%d %H:%M:%S"), "son_guncelleme_ms": 0, "oturumlar": [],
            "arac_sayaci": {}, "compaction": 0, "izin_reddi": 0, "hata_arac": 0, "arac_ciktisi_kesik": 0, "modeller": {},
            "toplam": {"input": 0, "output": 0, "reasoning": 0, "cost": 0.0}}
    md = [f"# KOSU DEFTERI — {proje} / {ad}", f"uretim: {meta['uretim']}  ·  dizin: `{wt}`", ""]
    jl = []

    if not DB.exists():
        md.append(f"UYARI: opencode DB yok: {DB}")
    else:
        con = db_ro()
        ses = sessions if sessions is not None else (session_rows(con, session_ids(record)) if record else sessions_for_dir(con, wt, proje))
        if not ses:
            md.append("UYARI: bu dizin icin opencode oturumu bulunamadi (henuz baslamadi mi?)")
        for s in ses:
            model = j(s["model"]) if s["model"] else {}
            mname = f"{model.get('providerID', '?')}/{model.get('id', '?')}" if model else "?"
            so = {"id": s["id"], "parent": s["parent_id"], "agent": s["agent"], "model": mname, "title": s["title"],
                  "baslangic": ts(s["time_created"]), "son": ts(s["time_updated"]),
                  "tokens": {"input": s["tokens_input"], "output": s["tokens_output"], "reasoning": s["tokens_reasoning"], "cache_read": s["tokens_cache_read"]},
                  "cost": round(s["cost"] or 0, 4), "mesaj": 0, "arac": 0}
            meta["son_guncelleme_ms"] = max(meta["son_guncelleme_ms"], int(s["time_updated"] or 0))
            meta["modeller"][mname] = meta["modeller"].get(mname, 0) + 1
            for k in ("input", "output", "reasoning"):
                meta["toplam"][k] += so["tokens"][k] or 0
            meta["toplam"]["cost"] += so["cost"]
            rol = "ALT-AJAN" if s["parent_id"] else "ANA"
            md += ["", f"## [{rol}] oturum {s['id']} · agent={s['agent']} · model={mname}",
                   f"baslik: {s['title']}  ·  {so['baslangic']} → {so['son']}  ·  tokens in/out/reason={so['tokens']['input']}/{so['tokens']['output']}/{so['tokens']['reasoning']}  ·  cost=${so['cost']}",
                   (f"parent: {s['parent_id']}" if s["parent_id"] else ""), ""]
            msgs = con.execute("select * from message where session_id=? order by time_created", (s["id"],)).fetchall()
            for m in msgs:
                md_ = j(m["data"])
                so["mesaj"] += 1
                rolm = md_.get("role", "?")
                etiket = rolm.upper()
                if rolm == "assistant":
                    etiket += f" · {md_.get('agent', '')} · {md_.get('providerID', '')}/{md_.get('modelID', '')}"
                    if md_.get("finish"):
                        etiket += f" · finish={md_['finish']}"
                md.append(f"### {ts(m['time_created'])} {etiket}")
                parts = con.execute("select * from part where message_id=? order by time_created", (m["id"],)).fetchall()
                for p in parts:
                    d = j(p["data"])
                    t = d.get("type")
                    rec = {"session": s["id"], "message": m["id"], "role": rolm, "agent": md_.get("agent"),
                           "model": f"{md_.get('providerID', '')}/{md_.get('modelID', '')}", "time": ts(p["time_created"]), "time_ms": p["time_created"], "type": t}
                    if t == "reasoning":
                        md.append("🧠 DUSUNME:\n" + "\n".join("> " + l for l in kes(d.get("text", ""), KES["reasoning"]).splitlines()))
                        rec["text"] = d.get("text", "")
                    elif t == "text":
                        md.append(kes(d.get("text", ""), KES["text"]))
                        rec["text"] = d.get("text", "")
                    elif t == "tool":
                        st = d.get("state", {}) or {}
                        so["arac"] += 1
                        meta["arac_sayaci"][d.get("tool")] = meta["arac_sayaci"].get(d.get("tool"), 0) + 1
                        durum = st.get("status")
                        err = st.get("error", "")
                        if durum == "error":
                            meta["hata_arac"] += 1
                            if "permission" in (err or "").lower():
                                meta["izin_reddi"] += 1
                        if (st.get("metadata") or {}).get("truncated"):
                            meta["arac_ciktisi_kesik"] += 1
                        md.append(f"🔧 ARAC `{d.get('tool')}` [{durum}] {st.get('title', '')}")
                        md.append("```json\n" + kes(st.get("input", {}), KES["input"]) + "\n```")
                        if err:
                            md.append(f"HATA: {kes(err, 1000)}")
                        if st.get("output"):
                            md.append("```\n" + kes(st.get("output", ""), KES["output"]) + "\n```")
                        rec.update({"tool": d.get("tool"), "status": durum, "input": st.get("input"), "output": st.get("output"),
                                    "error": err, "title": st.get("title"), "metadata": st.get("metadata")})
                    elif t == "patch":
                        md.append(f"📝 YAMA {(d.get('hash') or '')[:8]} dosyalar: {d.get('files')}")
                        rec.update({"hash": d.get("hash"), "files": d.get("files")})
                    elif t == "compaction":
                        meta["compaction"] += 1
                        md.append(f"♻️ COMPACTION (auto={d.get('auto')}, overflow={d.get('overflow')}) — context sikistirildi")
                        rec["data"] = d
                    elif t == "step-finish":
                        tk = d.get("tokens", {}) or {}
                        md.append(f"— adim bitti: reason={d.get('reason')} tokens total={tk.get('total')} in={tk.get('input')} out={tk.get('output')} reason={tk.get('reasoning')} cost={d.get('cost')}")
                        rec.update({"reason": d.get("reason"), "tokens": tk, "cost": d.get("cost"), "snapshot": d.get("snapshot")})
                    elif t == "step-start":
                        rec["snapshot"] = d.get("snapshot")
                    else:
                        md.append(f"· part {t}: {kes(d, 500)}")
                        rec["data"] = d
                    jl.append(rec)
                md.append("")
            meta["oturumlar"].append(so)
        con.close()

    ekler = []
    if record:
        meta["run_id"] = record["run_id"]
        meta["run_updated"] = record["updated"]
        meta["archive_input_mtime"] = activity
        root = RunStore(QE).path(record["run_id"])
        atomic_json(out / "run.json", record)
        ekler.append("run.json")
        meta["claude_oturumlari"] = []
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
                                meta["arac_sayaci"][tool] = meta["arac_sayaci"].get(tool, 0) + 1
                                md.append(f"🔧 Claude {task.name} ARAC `{tool}`\n```json\n" + kes(part.get("input", {}), KES["input"]) + "\n```")
                    if event.get("type") == "result":
                        cost = event.get("total_cost_usd", event.get("cost_usd"))
                        if isinstance(cost, (int, float)) and math.isfinite(cost) and cost >= 0:
                            summary["usd"] = cost
                        summary["usage"] = event.get("usage")
                        md.append(f"## Claude {task.name} / {receipt.get('session_id')}\n" + kes(event.get("result", ""), KES["text"]))
                if summary["usd"] is not None:
                    meta["toplam"]["cost"] += summary["usd"]
                else:
                    meta["claude_maliyeti_bilinmeyen"] = meta.get("claude_maliyeti_bilinmeyen", 0) + 1
                meta["modeller"][receipt["model"]] = meta["modeller"].get(receipt["model"], 0) + 1
                meta["claude_oturumlari"].append(summary)
        if (out / "attempts").exists():
            ekler.append("attempts/")
        prompt = prompt or record["prompt"]
        motor_log = motor_log or str(root / "coordinator.log")
    if motor_log and pathlib.Path(motor_log).exists():
        shutil.copy(motor_log, out / "motor.log"); ekler.append("motor.log")
    if prompt and pathlib.Path(prompt).exists():
        shutil.copy(prompt, out / "gorev-prompt.md"); ekler.append("gorev-prompt.md")
    base_sha = None
    wl = QE / "logs/worktrees.log"
    if wt and wl.exists():
        satirlar = wl.read_text(errors="replace").splitlines()
        dil = []; son_base = None
        for i, l in enumerate(satirlar):
            if "base_sha=" in l:
                son_base = l.split("base_sha=")[1].split()[0]
            if pathlib.Path(wt).name in l:
                dil.append(l)
                if "worktree=" in l and base_sha is None:
                    base_sha = son_base  # the base line does not carry the worktree name; take it from the block
                    if i > 0 and "base_sha=" in satirlar[i - 1]:
                        dil.insert(len(dil) - 1, satirlar[i - 1])
        if dil:
            (out / "worktree-kaydi.txt").write_text("\n".join(dil) + "\n"); ekler.append("worktree-kaydi.txt")
    for f in (() if record else (f"zincir/{proje}-gorev-durumu.json", f"zincir/{proje}.json")):
        pth = QE / f
        if pth.exists():
            shutil.copy(pth, out / pathlib.Path(f).name); ekler.append(pathlib.Path(f).name)
    for f in ("atama-gecmisi.jsonl", "sinif-gecmisi.jsonl", "claude-harcama.jsonl"):
        pth = QE / "zincir" / f
        if pth.exists():
            dil = [l for l in pth.read_text(errors="replace").splitlines() if (record["run_id"] in l if record else proje in l or ad in l)]
            if dil:
                (out / f).write_text("\n".join(dil[-200:]) + "\n"); ekler.append(f)
    if wt:
        ekler += git_kaydi(out, wt, base_sha)
    config_snapshot(out); ekler.append("config-snapshot/")
    # WATCHERS: what the guard/hook/quota/watch/rotation scripts went through in the job window
    t0 = min([o_ms for o_ms in (_ms(o["baslangic"]) for o in meta["oturumlar"]) if o_ms] or [0])
    t1 = meta["son_guncelleme_ms"]
    if motor_log and pathlib.Path(motor_log).exists():
        st = pathlib.Path(motor_log).stat()
        t0 = min(t0 or int(st.st_birthtime * 1000), int(st.st_birthtime * 1000)) if hasattr(st, "st_birthtime") else t0
        t1 = max(t1, int(st.st_mtime * 1000))
    iz = izleyici_dilimleri(out, t0, t1, proje=proje)
    meta["izleyiciler"] = iz
    if iz:
        ekler.append("izleyiciler/(" + ", ".join(iz) + ")")

    meta["toplam"]["cost"] = round(meta["toplam"]["cost"], 4)
    meta["ekler"] = ekler
    md.insert(3, "## OZET\n" + "\n".join([
        f"- OpenCode oturum: {len(meta['oturumlar'])} (alt-ajan: {sum(1 for o in meta['oturumlar'] if o['parent'])}); Claude deneme: {len(meta.get('claude_oturumlari', []))}",
        f"- modeller: {meta['modeller']}",
        f"- arac cagrisi: {sum(meta['arac_sayaci'].values())} → {meta['arac_sayaci']}",
        f"- hatali arac: {meta['hata_arac']} (izin reddi: {meta['izin_reddi']})  ·  compaction: {meta['compaction']}  ·  kaynakta kesik arac ciktisi: {meta['arac_ciktisi_kesik']}",
        f"- tokens in/out/reason: {meta['toplam']['input']}/{meta['toplam']['output']}/{meta['toplam']['reasoning']}  ·  cost=${meta['toplam']['cost']}",
        f"- token toplami OpenCode kapsamindadir; Claude usage deneme kaydindadir. Maliyeti bilinmeyen Claude denemesi: {meta.get('claude_maliyeti_bilinmeyen', 0)}",
        f"- ekler: {', '.join(ekler) or '-'}",
        "- NOT: .md okunurluk icin kisaltilir; transcript.jsonl EKSIKSIZDIR.",
    ]) + "\n")
    (out / "transcript.md").write_text("\n".join(md) + "\n")
    with open(out / "transcript.jsonl", "w") as f:
        for r in jl:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    meta["redaksiyon"] = {"maskelenen_dosya": redakte_dizin(out), "kapsam": "metin sink'leri; files/ (worktree arsivi) ham"}
    atomic_json(out / "meta.json", meta)
    return out


def proje_bul(d):
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
    return "diger", None, d


def ev_baslangici_yaz(managed):
    bas = min((float(r['created']) for r in managed if isinstance(r.get('created'), (int, float))), default=time.time())
    WATERMARK.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(WATERMARK, str(int(bas * 1000)))


def tara():
    with FileLock(WATERMARK.with_suffix('.lock')):
        return _tara()


def _tara():
    """Write all sessions updated since the last scan to the ledger. Worktree sessions to the job ledger,
    others (TUI inside the repo / manual run / another directory) to a separate per-session ledger. Output: number of ledgers written."""
    managed = RunStore(QE).list()
    n = 0
    failed = False
    for record in managed:
        name = pathlib.Path(record['worktree']).name if record.get('worktree') else record['run_id']
        target = QE / "reports" / f"kosu-{record['project']}-{name}"
        try:
            archived = json.loads((target / "meta.json").read_text()) if (target / "meta.json").exists() else {}
            tree_changed = False
            if record.get('worktree') and pathlib.Path(record['worktree']).exists():
                try:
                    receipt = json.loads((target/'worktree-archive.json').read_text())
                    tree_changed = verify_archive(pathlib.Path(receipt['manifest'])) != archive_index(pathlib.Path(record['worktree']), defter_atla)
                except (OSError, ValueError, KeyError):
                    tree_changed = True
            if tree_changed or record_activity(record) > archived.get("archive_input_mtime", 0):
                dump(record["project"], record["worktree"], run_id=record["run_id"])
                n += 1
        except Exception as e:
            failed = True
            print(f"defter hatasi {record['run_id']}: {e}", file=sys.stderr)
    if not DB.exists():
        if failed:
            raise OSError('one or more run ledgers failed')
        print("opencode DB yok", file=sys.stderr)
        return n
    if not WATERMARK.exists():
        ev_baslangici_yaz(managed)
    try:
        wm = int(WATERMARK.read_text().strip()) if WATERMARK.exists() else 0
    except Exception:
        wm = 0  # broken watermark = scan from scratch (no loss, only rewriting)
    con = db_ro()
    guncel = con.execute("select * from session where time_updated > ? order by time_updated", (wm,)).fetchall()
    if not guncel:
        con.close()
        if failed:
            raise OSError('one or more run ledgers failed')
        return n
    yeni_wm = max(int(r["time_updated"]) for r in guncel)
    # if a sub-agent session was updated, go up to the root session (the ledger opens by the root directory)
    def kok(r):
        while r["parent_id"]:
            p = con.execute("select * from session where id=?", (r["parent_id"],)).fetchone()
            if not p:
                break
            r = p
        return r
    isler = {}
    owners = {sid: record for record in managed for sid in session_ids(record)}
    for r in guncel:
        r = kok(r)
        if r["id"] in owners:
            record = owners[r["id"]]
            wt = record["worktree"]
            if wt:
                isler[(record["project"], pathlib.Path(wt).name, wt)] = None
            continue
        proje, ad, dizin = proje_bul(r["directory"])
        if run_for_worktree(RunStore(QE), dizin, proje):
            ad = None  # Unowned root in a managed directory gets its own legacy ledger.
        if ad:  # worktree: the whole job is one ledger
            isler[(proje, ad, dizin)] = None
        else:   # single-session ledger
            isler[(proje, f"ses-{time.strftime('%Y%m%d-%H%M', time.localtime(r['time_created'] / 1000))}-{r['id'][-6:]}", dizin)] = r["id"]
    con.close()
    for (proje, ad, dizin), sid in isler.items():
        kaynak = pathlib.Path(dizin).resolve()
        if kaynak == QE.resolve() or kaynak in QE.resolve().parents:
            # B276: the archive of a session opened in/above the QE home stays inside the source and cannot be archived; skipped, the scan continues.
            print(f"defter atlandi {proje}/{ad}: arsiv kaynagin icinde ({dizin})", file=sys.stderr)
            continue
        try:
            if sid:
                c = db_ro()
                ses = cocuklari_ekle(c, [c.execute("select * from session where id=?", (sid,)).fetchone()])
                c.close()
                dump(proje, dizin, ad=ad, sessions=ses)
            else:
                # engine log: is-*.log matching the worktree name (the engine writes the worktree on its BITTI line)
                ml = None
                for f in sorted((QE / "logs").glob(f"is-{proje}-*.log"), reverse=True):
                    try:
                        if ad in f.read_text(errors="replace"):
                            ml = str(f); break
                    except Exception:
                        pass
                dump(proje, dizin, motor_log=ml, ad=ad)
            n += 1
        except Exception as e:
            failed = True
            print(f"defter hatasi {proje}/{ad}: {e}", file=sys.stderr)
    if failed:
        raise OSError('one or more ledgers failed; watermark retained')
    WATERMARK.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(WATERMARK, str(yeni_wm))
    return n


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "tara":
        print(f"kosu defteri tara: {tara()} defter yazildi/yenilendi")
        sys.exit(0)
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr); sys.exit(2)
    print(dump(sys.argv[1], sys.argv[2], *(sys.argv[3:5])))
