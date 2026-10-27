#!/usr/bin/env python3
"""Live monitor: a colored stream of an unattended run, in the style of Claude Code / opencode.

Read-only: opencode DB (mode=ro) + engine log. Never touches the job; closing it leaves the job running.
  watch.py <project> [--wt PATH|latest] [--plain] [--single] [--duration N] [--thought]
    --plain    no color, no sticky bottom line (test/pipe)
    --single   print what exists and exit
    --duration N   exit after N seconds
    --detail  show raw events and the engine record (--thought is the old alias)
Keys: q quit (the job continues) · x cancel the job (flag) · d toggle detail
"""
import opencode_adaptor as OC
import json
import os
import pathlib
import re
import select
import signal
import sqlite3
import sys
import time
from collections import deque
from screen_render import symbol, run_summary, tool_summary, wrap_text, clean
from run_store import RunStore, atomic_write, base_qa_line
from session import session_ids, session_rows

QE = pathlib.Path(os.environ.get("CADRE_HOME", os.path.expanduser("~/.local/share/cadre-engineering")))
DB = pathlib.Path(os.environ.get("OPENCODE_DB", os.path.expanduser("~/.local/share/opencode/opencode.db")))

# ---- colors (256) --------------------------------------------------------
class R:
    on = True
    @classmethod
    def c(cls, code, s):
        return f"\033[{code}m{s}\033[0m" if cls.on else s
    @classmethod
    def dim(cls, s): return cls.c("38;5;245", s)
    @classmethod
    def green(cls, s): return cls.c("38;5;42", s)
    @classmethod
    def red(cls, s): return cls.c("38;5;203", s)
    @classmethod
    def yellow(cls, s): return cls.c("38;5;220", s)
    @classmethod
    def cyan(cls, s): return cls.c("38;5;44", s)
    @classmethod
    def bold(cls, s): return cls.c("1", s)
    @classmethod
    def italic(cls, s): return cls.c("3;38;5;245", s)


def j(s):
    try:
        return json.loads(s)
    except Exception:
        return {}


def ts(ms):
    try:
        return time.strftime("%H:%M:%S", time.localtime(int(ms) / 1000))
    except Exception:
        return "--:--:--"


def duration_str(sec):
    sec = int(sec); return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def shorten(s, n):
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


class Watcher:
    def __init__(self, project_ref, wt, plain=False, single=False, duration=None, thought=False, runner_log=None, run_id=None):
        self.project_ref, self.wt, self.plain, self.single, self.duration, self.thought = project_ref, wt, plain, single, duration, thought
        self.detail = thought
        self.history = deque(maxlen=200)
        self._collected = None
        self.last_summary = ''
        self.runner_log = runner_log
        self.run_id = run_id
        self.tty = (not plain) and sys.stdout.isatty()
        R.on = self.tty and not os.environ.get('NO_COLOR')
        self.seen = {}       # part_id -> "done" | "live"
        self.message_seen = set()
        self.session_seen = set()
        self.log_offset = 0
        self.live_line = False
        self.pause = False
        self.finished = False
        self.rc = None
        self.ledger = None
        self.t0 = time.time()
        self.count = {"step": 0, "tool": 0, "error": 0, "comp": 0, "in": 0, "out": 0, "cost": 0.0}
        self.model = "?"
        self.followed = []       # B308: child runs watched in project mode
        self.child_log = {}     # child coordinator.log -> offset read
        self.base_warning = ''
        self.rows, self.cols = self._size()

    # ---- terminal ---------------------------------------------------------
    def _size(self):
        try:
            s = os.get_terminal_size(); return s.lines, s.columns
        except Exception:
            return 40, 100

    def _region_setup(self):
        if not self.tty:
            return
        self.rows, self.cols = self._size()
        sys.stdout.write(f"\033[1;{self.rows - 1}r\033[{self.rows - 1};1H")
        sys.stdout.flush()

    def _region_reset(self):
        if self.tty:
            sys.stdout.write("\033[r\033[%d;1H\n" % self.rows); sys.stdout.flush()

    def write(self, s=""):
        if self._collected is not None:
            self._collected.append(s)
            return
        if self.live_line and self.tty:
            sys.stdout.write("\r\033[2K"); self.live_line = False
        sys.stdout.write(s + "\n")
        sys.stdout.flush()

    def detail_write(self, s):
        self.history.append(s)
        if self.detail:
            self.write(s)

    def detail_change(self):
        self.detail = not self.detail
        if self.tty:
            sys.stdout.write('\033[2J\033[H')
        self.title()
        if self.detail:
            for line in self.history:
                self.write(line)
        elif self.last_summary:
            self.write(self.last_summary)
        self.write(R.dim('Detail on · d returns to the summary' if self.detail else 'Summary · d for detail'))

    def live(self, s):
        """one-line progress line updated in place (spinner)"""
        if not self.tty:
            return
        sys.stdout.write("\r\033[2K" + shorten(s, self.cols - 2)); sys.stdout.flush()
        self.live_line = True

    def sub_line(self):
        if not self.tty:
            return
        d = (f'{symbol("completed")} finished' if self.rc == 0 else f'{symbol("failed")} stopped') if self.finished else ("○ paused" if self.pause else "● running")
        color = (R.green if self.rc == 0 else R.yellow) if self.finished else (R.yellow if self.pause else R.cyan)
        left = f'{color(d)} · {duration_str(time.time() - self.t0)}'
        if self.detail:
            left += (f" · step {self.count['step']} · tool {self.count['tool']}"
               + (f" · {R.red(str(self.count['error']) + ' error')}" if self.count['error'] else "")
               + f" · in/out {self.count['in'] // 1000}k/{self.count['out'] // 1000}k · ♻{self.count['comp']} · ${self.count['cost']:.2f}")
        right_text = "q quit · x cancel · d detail"
        right = R.dim(right_text)
        visible = re.sub(r"\033\[[0-9;]*m", "", left)
        gap = self.cols - len(visible) - len(right_text) - 1
        if gap < 1:  # narrow terminal: drop the shortcuts, fit to the left
            right, gap = "", 0
            visible = visible[: self.cols - 1]; left = visible
        sys.stdout.write(f"\0337\033[{self.rows};1H\033[2K{left}{' ' * gap}{right}\0338"); sys.stdout.flush()

    def base_warn(self, record):
        warning = base_qa_line(record)
        if warning and warning != self.base_warning:
            self.write(R.yellow(warning))
        self.base_warning = warning

    # ---- data -------------------------------------------------------------
    def db(self):
        con = OC.connect(DB); con.row_factory = sqlite3.Row
        return con

    def _repo_path(self):
        try:
            return (QE / "projects" / self.project_ref / "repo-path").read_text().strip()
        except Exception:
            return None

    def _wt_start_ms(self):
        try:
            return int(pathlib.Path(self.wt).stat().st_ctime * 1000) - 120000
        except Exception:
            return 0

    def _child_watch(self, record):
        # B308: in project mode the main run does no work; the active child's worktree, sessions and log are watched.
        if record.get("worktree"):
            return record["worktree"]
        try:
            child = json.loads((RunStore(QE).path(self.run_id) / "project-ref-active.json").read_text()).get("child")
            crec = RunStore(QE).read(child)
        except Exception:
            return ""
        if child not in self.followed:
            self.followed.append(child)
            self.child_log[str(RunStore(QE).path(child) / "coordinator.log")] = 0
        return crec.get("worktree", "")

    def sessions(self, con):
        if self.run_id:
            ids = set()
            for rid in (self.run_id, *self.followed):
                try:
                    ids |= session_ids(RunStore(QE).read(rid))
                except Exception:
                    pass
            return session_rows(con, ids)
        # observation finding: for registered projects opencode writes the session `directory` as the MAIN REPO path,
        # not the worktree. Matching: worktree path OR project repo path (sessions started after the worktree opened).
        repo = self._repo_path(); t0 = self._wt_start_ms()
        if repo:
            rows = con.execute("select * from session where (directory = ? or directory like ?) or (directory = ? and time_created >= ?) order by time_created",
                               (self.wt, self.wt.rstrip("/") + "/%", repo, t0)).fetchall()
        else:
            rows = con.execute("select * from session where directory = ? or directory like ? order by time_created",
                               (self.wt, self.wt.rstrip("/") + "/%")).fetchall()
        ids = {r["id"] for r in rows}
        while ids:
            q = ",".join("?" * len(ids))
            fresh = [r for r in con.execute(f"select * from session where parent_id in ({q})", tuple(ids)).fetchall() if r["id"] not in {x["id"] for x in rows}]
            if not fresh:
                break
            rows += fresh; ids = {r["id"] for r in fresh}
        return rows

    # ---- drawing ------------------------------------------------------------
    def title(self):
        branch = ""
        try:
            import subprocess
            if self.wt:
                branch = subprocess.run(["git", "-C", self.wt, "branch", "--show-current"], capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            pass
        self.write(R.cyan("┃ ") + R.bold(f"cadre · {clean(self.project_ref)}"))
        if self.detail:
            self.write(R.dim(f'{branch} · {self.wt}'))
        if self.run_id and self.detail:
            self.write(R.dim(f"run: {self.run_id}" + (" · preparing the worktree" if not self.wt else "")))
        self.write(R.cyan("┃ ") + R.dim("read-only monitor — closing it leaves the job running · x to cancel"))
        self.write()

    def task_block(self, text):
        lines = wrap_text(text.strip(), self.cols - 4)
        for i, l in enumerate(lines[:12]):
            self.write(R.cyan("▌ ") + (R.bold(l) if i == 0 else l))
        if len(lines) > 12:
            self.write(R.cyan("▌ ") + R.dim(f"… (+{len(lines) - 12} lines)"))
        self.write()

    def part_draw(self, s, m, d, indent):
        lines = []
        self._collected = lines
        try:
            self._part_detail(s, m, d, indent)
        finally:
            self._collected = None
        for line in lines:
            self.detail_write(line)
        if self.detail:
            return
        kind = d.get('type')
        if kind == 'tool':
            self.write(indent + tool_summary(d))
        elif kind in ('text', 'reasoning') and d.get('text', '').strip():
            self.write(indent + (f'{symbol("thinking")} evaluating the task' if kind == 'reasoning'
                              else f'{symbol("completed")} answer ready · details: d'))
        elif kind == 'patch':
            self.write(f'{symbol("patch")} {len(d.get("files") or [])} files changed')
        elif kind == 'compaction':
            self.write(f'{symbol("compaction")} conversation shortened')

    def _part_detail(self, s, m, d, indent):
        t = d.get("type"); g = indent
        if t == "reasoning":
            text = (d.get("text") or "").strip()
            if not text:
                return
            lines = text.splitlines()
            shown = lines[:200]
            self.write(g + R.italic("✻ Thinking"))
            for l in shown:
                for line in wrap_text(l, self.cols - len(g) - 4):
                    self.write(g + '  ' + R.italic(line))
            if len(lines) > len(shown):
                self.write(g + "  " + R.dim(f"… (+{len(lines) - len(shown)} lines; the full text is in the ledger)"))
        elif t == "text":
            text = (d.get("text") or "").strip()
            if not text:
                return
            for l in wrap_text(text, self.cols - len(g) - 2)[:200]:
                self.write(g + l)
            if text.count("\n") > 60:
                self.write(g + R.dim("… (long text shortened; the full text is in the ledger)"))
        elif t == "tool":
            st = d.get("state") or {}; status = st.get("status"); inp = st.get("input") or {}
            name = d.get("tool") or "?"
            summary = inp.get("command") or inp.get("filePath") or inp.get("pattern") or inp.get("description") or st.get("title") or ""
            if name == "task":
                summary = f"{inp.get('subagent_type', '?')} — {shorten(inp.get('description') or inp.get('prompt') or '', 60)}"
            self.count["tool"] += 1
            begin = (st.get("time") or {}).get("start"); end = (st.get("time") or {}).get("end")
            duration = f"{(end - begin) / 1000:.1f}s" if begin and end else ""
            for line in wrap_text(f"⏺ {name.capitalize()} {summary}", self.cols - len(g) - 2):
                self.write(g + line)
            if status == "error":
                self.count["error"] += 1
                err = (st.get("error") or "error").splitlines()[0]
                self.write(g + "  ⎿ " + R.red("✗ ") + R.red(shorten(err, self.cols - len(g) - 8)))
            elif status == 'completed':
                out = st.get("output") or ""
                n = out.count("\n") + (1 if out and not out.endswith("\n") else 0)
                meta = st.get("metadata") or {}
                extra = f" · exit {meta.get('exit')}" if isinstance(meta, dict) and meta.get("exit") not in (None, 0) else ""
                self.write(g + "  ⎿ " + R.green("✓ ") + R.dim(f"{duration} · {n} lines{extra}".strip(" ·")))
                for l in wrap_text(out, self.cols - len(g) - 6)[:200]:
                    self.write(g + "    " + R.dim(l))
                if n > 200:
                    self.write(g + "    " + R.dim("… (the full output is in the ledger)"))
        elif t == "patch":
            files = d.get("files") or []
            self.write(g + R.cyan("📝 patch ") + R.dim(", ".join(pathlib.Path(f).name for f in files[:5])))
        elif t == "compaction":
            self.count["comp"] += 1
            self.write(g + R.yellow(f"♻ context compacted (#{self.count['comp']})"))
        elif t == "step-finish":
            tk = d.get("tokens") or {}
            self.count["step"] += 1
            self.count["in"] += int(tk.get("input") or 0); self.count["out"] += int(tk.get("output") or 0)
            self.count["cost"] += float(d.get("cost") or 0)

    # ---- loop ------------------------------------------------------------
    def round(self):
        if not DB.exists():
            return
        con = self.db()
        try:
            rows = self.sessions(con)
            for s in rows:
                if s["id"] not in self.session_seen:
                    self.session_seen.add(s["id"])
                    mdl = j(s["model"]) if s["model"] else {}
                    mname = f"{mdl.get('providerID', '?')}/{mdl.get('id', '?')}" if mdl else "?"
                    if s["parent_id"]:
                        self.detail_write("  " + R.cyan(f"↳ {s['agent'] or 'sub-agent'} · {mname}"))
                    else:
                        self.model = mname
                        self.detail_write(R.dim(f"session {s['id'][:12]} · {s['agent']} · {mname}"))
            if not rows:
                return
            q = ",".join("?" * len(rows))
            parts = con.execute(
                f"select p.*, m.data as mdata, s.parent_id as parent from part p join message m on m.id=p.message_id "
                f"join session s on s.id=p.session_id where p.session_id in ({q}) order by p.time_created", tuple(r["id"] for r in rows)).fetchall()
            last_id = parts[-1]["id"] if parts else None
            for p in parts:
                if self.seen.get(p["id"]) == "done":
                    continue
                d = j(p["data"]); md = j(p["mdata"]); g = "    " if p["parent"] else ""
                if p["message_id"] not in self.message_seen:
                    self.message_seen.add(p["message_id"])
                    if md.get("role") == "user":
                        # task text: the text parts of this message
                        text = "\n".join((j(x["data"]).get("text") or "") for x in con.execute(
                            "select data from part where message_id=? order by time_created", (p["message_id"],)))
                        self.task_block(text) if not p["parent"] else self.detail_write(g + R.cyan("▌ ") + R.dim(shorten(clean(text), self.cols - 8)))
                if md.get("role") == "user":
                    self.seen[p["id"]] = "done"; continue
                t = d.get("type")
                finished = True
                # C25: completion is decided by STATE, not by TIME. EVERY running tool (not just the last part) stays live;
                # a long `make check` is not painted green because it stayed quiet for 20 s. Text/thinking: live if it is the last part with no end stamp.
                if not self.single:
                    if t == "tool" and (d.get("state") or {}).get("status") not in ("completed", "error"):
                        finished = False
                    elif p["id"] == last_id and t in ("reasoning", "text") and not md.get("finish") and not (d.get("time") or {}).get("end"):
                        finished = False
                if not finished:
                    if t == "tool":
                        st = d.get("state") or {}; inp = st.get("input") or {}
                        begin = (st.get("time") or {}).get("start") or p["time_created"]
                        if self.detail:
                            self.live(g + R.bold(f"⏺ {(d.get('tool') or '?').capitalize()} ") + shorten(clean(inp.get("command") or inp.get("filePath") or st.get("title") or ""), 60)
                                       + R.dim(f"  … running {int(time.time() - begin / 1000)}s"))
                        else:
                            self.live(g + tool_summary(d))
                    else:
                        label = "✻ thinking" if t == "reasoning" else "✎ writing"
                        self.live(g + R.italic(f"{label} … {len(d.get('text') or '')} characters"))
                    self.seen[p["id"]] = "live"
                    continue
                self.part_draw(None, md, d, g)
                self.seen[p["id"]] = "done"
        finally:
            con.close()

    def runner_round(self):
        if not self.runner_log:
            # find the engine log of the wt; otherwise the dispatcher/orchestra log (stage 10: lanes write to running/*.log)
            for pattern in (f"job-{self.project_ref}-*.log", f"dispatch-{self.project_ref}-*.log", f"orchestrator-{self.project_ref}-*.log"):
                for f in sorted((QE / "logs").glob(pattern), key=lambda x: x.stat().st_mtime, reverse=True)[:5]:
                    try:
                        if self.wt in f.read_text(errors="replace") or pattern.startswith("orchestrator"):
                            self.runner_log = str(f); break
                    except Exception:
                        pass
                if self.runner_log:
                    break
            if not self.runner_log:
                return
        try:
            with open(self.runner_log, errors="replace") as f:
                f.seek(self.log_offset); fresh = f.read(); self.log_offset = f.tell()
        except Exception:
            fresh = ""
        for path, ofs in list(self.child_log.items()):
            try:
                with open(path, errors="replace") as f:
                    f.seek(ofs); fresh += f.read(); self.child_log[path] = f.tell()
            except Exception:
                pass
        for l in fresh.splitlines():
            m = re.match(r"\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2}) (.*)", l)
            if not m:
                self.detail_write(clean(l))
                continue
            self.detail_write(R.dim(clean(l)))
            hour, msg = m.group(1), m.group(2)
            if msg.startswith("FINISHED") and not self.run_id:
                self.finished = True
            if "run ledger:" in msg:
                self.ledger = msg.split("run ledger:", 1)[1].strip()
            if msg.startswith("run finished on its own") and not self.run_id:
                self.rc = 0
            if re.search(r"cadre-guard|WATCHDOG|RECOVERY|QUOTA|ERROR-LOOP|STUCK|CANCEL|attempt limit|resuming|run fell|interim commit|last commit|model closed|model go_back|run starting|worktree:|prompt  :", msg):
                color = R.red if re.search(r"STUCK|CUT|ERROR|fell|abandon|dropp", msg) else (R.green if re.search(r"commit|came back|starting", msg) else R.yellow)
                if not self.detail:
                    if re.search(r'STUCK|ERROR|fell|abandon|dropp', msg):
                        self.write(R.red(f'{symbol("failed")} job stopped; details: d'))
                    elif 'CANCEL' in msg:
                        self.write(R.yellow(f'{symbol("cancel")} cancel in progress'))

    def key(self):
        if not self.tty:
            return
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if not r:
            return
        try:
            k = os.read(sys.stdin.fileno(), 1).decode(errors="ignore")
        except Exception:
            return
        if k in ("q", "Q"):
            raise KeyboardInterrupt
        if k in ("x", "X"):
            path = RunStore(QE).path(self.run_id) / "cancel.flag" if self.run_id else QE / "chain" / f"{self.project_ref}-cancel.flag"
            atomic_write(path, "requested\n")
            self.write(R.red("⚑ cancel flag left — the engine will stop within a few seconds"))
        if k in ('d', 'D', 't', 'T'):
            self.detail_change()

    def execute(self):
        import termios, tty
        old = None
        if self.tty:
            try:
                old = termios.tcgetattr(sys.stdin); tty.setcbreak(sys.stdin.fileno())
            except Exception:
                self.tty = False
        signal.signal(signal.SIGWINCH, lambda *_: self._region_setup())
        if self.tty:
            sys.stdout.write("\033[2J\033[H")  # clean page: no leftover screen clutter
        self._region_setup()
        self.title()
        try:
            start = time.time()
            while True:
                if self.run_id:
                    record = RunStore(QE).read(self.run_id)
                    summary = run_summary(record)
                    if summary != self.last_summary:
                        self.write(summary)
                        self.last_summary = summary
                    self.base_warn(record)
                    self.wt = self._child_watch(record)
                    self.finished = record["state"] in ("completed", "cancelled", "failed", "retryable")
                    self.rc = record.get("exit_code")
                if not self.pause:
                    if self.wt:
                        self.round()
                    self.runner_round()
                self.sub_line(); self.key()
                if self.finished:
                    self.write(); self.write((R.green("✔ DONE") if self.rc == 0 else R.yellow("⚑ STOPPED")) + (R.dim(f" rc={self.rc}") if self.rc is not None else "") + (R.dim(f" · ledger: {self.ledger}") if self.ledger and self.detail else ""))
                    self.sub_line()
                    if not self.single:
                        time.sleep(1.5)
                    break
                if self.single or (self.duration and time.time() - start >= self.duration):
                    break
                time.sleep(1.0)
        except KeyboardInterrupt:
            self.write(R.dim("monitor closed — the job keeps running in the background"))
        finally:
            if old is not None:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
            self._region_reset()


def last_worktree(project_ref, wait=0):
    root = QE / "worktrees" / project_ref
    last = time.time() + wait
    while True:
        candidates = sorted([d for d in root.glob("*/") if d.is_dir()], key=lambda d: d.stat().st_mtime, reverse=True) if root.exists() else []
        if candidates:
            return str(candidates[0]).rstrip("/")
        if time.time() >= last:
            return None
        time.sleep(1)


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__, file=sys.stderr); sys.exit(2)
    project_ref = a[0]; wt = "latest"; plain = "--plain" in a; single = "--single" in a; thought = "--thought" in a or "--detail" in a; duration = None; ml = None; run_id = None
    for i, x in enumerate(a):
        if x == "--wt": wt = a[i + 1]
        if x == "--duration": duration = float(a[i + 1])
        if x == "--runner-log": ml = a[i + 1]
        if x == "--run": run_id = a[i + 1]
    if run_id:
        record = RunStore(QE).read(run_id)
        if record["project"] != project_ref:
            raise ValueError("run/project mismatch")
        wt = record.get("worktree", "")
        ml = str(RunStore(QE).path(run_id) / "coordinator.log")
    if wt == "latest":
        wt = last_worktree(project_ref, wait=0 if single else 90)
        if not wt:
            print("worktree not found (has the job not started yet?)", file=sys.stderr); sys.exit(1)
    Watcher(project_ref, wt, plain=plain, single=single, duration=duration, thought=thought, runner_log=ml, run_id=run_id).execute()
