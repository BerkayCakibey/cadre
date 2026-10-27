"""Liveness: stall detection for running executors. S1: liveness responsibility; shared services come from the qe_core interface."""
from __future__ import annotations
import re
import math
import hashlib


class FastDrop:
    """B179: 12 attempts failing with rc!=0 at 15 s intervals (helper/compaction model zen 404) used up the attempt limit in 3 min;
    the error-loop detector counted per attempt so it did not see it, and rc!=0 was not counted as stuck so the rescuer did not step in.
    When `threshold` consecutive attempts end with rc!=0 in less than `threshold_sec` it is a `loop`; `series_start` is the first attempt of the series."""

    def __init__(self, threshold_sec=60, threshold=3):
        import qe_core as core
        self.threshold_sec, self.threshold = float(threshold_sec), int(threshold)
        self.series, self.series_start, self.loop = 0, None, False

    def save(self, rc, duration_sec, start, controlled=False):
        import qe_core as core
        if controlled:   # B184: the engine's own interruption / a budget denial is not a failure; it neither extends nor breaks the streak
            return 0
        if rc != 0 and duration_sec < self.threshold_sec:
            self.series += 1
            if self.series_start is None:
                self.series_start = start
        else:
            self.series, self.series_start = 0, None
        self.loop = self.series >= self.threshold
        return self.series


class ToolRepeat:
    """B200: the model called the same tool with the same input and the same output over and over (6+ times, ~23 s apart); G-F9 only
    counted the breaking test and the artefact guard waited 45 min. `threshold` identical consecutive calls are a loop (OpenHands: 3-4 repeats)."""

    def __init__(self, logf, threshold):
        import qe_core as core
        self.logf, self.threshold = str(logf), int(threshold)
        self.previous, self.series, self.remainder = None, 0, b""
        try:
            self.offset = core.pathlib.Path(self.logf).stat().st_size
        except OSError:
            self.offset = 0

    def tick(self):
        import qe_core as core
        if self.threshold <= 0:
            return ""
        try:
            with open(self.logf, "rb") as f:
                f.seek(self.offset)
                fresh = f.read()
            self.offset += len(fresh)
        except OSError:
            return ""
        lines = (self.remainder + fresh).split(b"\n")
        self.remainder = lines.pop()
        for raw in lines:
            try:
                event = core.json.loads(raw)
                if not isinstance(event, dict):
                    continue
                part = event.get('part') or {}
                if part.get('type') != 'tool' and event.get('type') != 'tool_use':
                    continue
            except (ValueError, AttributeError):
                continue
            status = part.get("state") or {}
            if status.get("status") not in ("completed", "error"):
                continue
            signature = (part.get("tool"), core.json.dumps(status.get("input"), sort_keys=True, default=str), str(status.get("output"))[:4000])
            self.series = self.series + 1 if signature == self.previous else 1
            self.previous = signature
            if self.series >= self.threshold:
                return str(part.get("tool"))
        return ""


class TestLoop:
    """G-F9: if the SAME test id shows `FAILED` in `threshold` separate log events within one attempt's log slice, returns that id.
    Reads incrementally from the end of the file at setup (earlier attempts do not count). 23.09 T1/T1c: the same test 10+ times,
    a 41 min unbounded loop — the artefact guard did not trigger because artefacts were produced, and STALL did not because there was no silence."""
    PATTERN = re.compile(r"FAILED ([\w./-]+::[\w\[\]-]+)")

    def __init__(self, logf, threshold):
        import qe_core as core
        self.logf, self.threshold = str(logf), int(threshold)
        self.counter = {}
        self.remainder = b""
        try:
            self.offset = core.pathlib.Path(self.logf).stat().st_size
        except OSError:
            self.offset = 0

    def tick(self):
        import qe_core as core
        if self.threshold <= 0:
            return ""
        try:
            with open(self.logf, "rb") as f:
                f.seek(self.offset)
                fresh = f.read()
            self.offset += len(fresh)
        except OSError:
            return ""
        lines = (self.remainder + fresh).split(b"\n")
        self.remainder = lines.pop()   # B185: an incomplete line waits for the next tick; a half id does not count
        for raw in lines:
            line = raw.decode("utf-8", "replace")
            if line.startswith('{"type":"text"'):
                continue   # B185: the model's own text echoes the id; only tool output counts
            for id in set(self.PATTERN.findall(line)):   # B185: a single pytest output appears twice in the same event
                self.counter[id] = self.counter.get(id, 0) + 1
                if self.counter[id] >= self.threshold:
                    return id
        return ""


class Liveness:
    """LIVE1: a session does not count as stuck until four independent signals go quiet."""

    def __init__(self, logf, proc, wt=None, min_sec=600.0):
        import qe_core as core
        self.proc, self.wt = proc, wt
        self.min_sec = float(min_sec)
        from limits import deadline
        self.movement_limit = float(deadline('qa'))
        self.logf = (proc.attempt.stdout_path if isinstance(proc, core.RecordedProcess) else core.pathlib.Path(logf))
        self.record = proc.attempt.root / 'liveness.json' if isinstance(proc, core.RecordedProcess) else None
        self.last = core.time.monotonic()
        self.longest = 0.0
        self.status = 'unknown'
        self.reason = 'initial-observation'
        self.movement_last = None
        self.open_step = False
        self.open_tools: set[str] = set()
        self.offset = 0
        self.remainder = b''
        self.steps = set()
        if self.record and self.record.exists():
            try:
                value = float(core.json.loads(self.record.read_text()).get('longest', 0))
                if math.isfinite(value):
                    self.longest = min(self.min_sec, max(0.0, value))
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        self.size, self.tree, self.cpu = self._size(), self._tree(), self._cpu()

    def _size(self):
        import qe_core as core
        try:
            return self.logf.stat().st_size
        except OSError:
            return None

    def _tree(self):
        import qe_core as core
        if not self.wt:
            return ()
        rows = []
        try:
            def error(exc):
                raise exc
            for root, dirs, files in core.os.walk(self.wt, onerror=error):
                dirs[:] = [d for d in dirs if d not in ('.git', 'node_modules', '.venv', '__pycache__')]
                for name in files:
                    path = core.pathlib.Path(root) / name
                    if name == '.git' or path == self.logf:
                        continue
                    st = path.lstat()
                    rows.append((str(path), st.st_size, st.st_mtime_ns))
            return tuple(sorted(rows))
        except OSError:
            return None

    def _cpu(self):
        import qe_core as core
        if self.proc is None:
            return None
        try:
            result = core.subprocess.run(['ps', '-A', '-o', 'pid=,ppid=,pgid=,time='],
                                    capture_output=True, text=True, timeout=5, check=True)
            rows = {}
            for line in result.stdout.splitlines():
                pid, parent, group, value = line.split()
                seconds = 0.0
                days, sep, clock = value.partition('-')
                if sep:
                    seconds = float(days) * 86400
                else:
                    clock = days
                ticks = 0.0
                for part in clock.split(':'):
                    ticks = ticks * 60 + float(part)
                rows[int(pid)] = (int(parent), int(group), seconds + ticks)
            own = {self.proc.pid}
            group = rows.get(self.proc.pid, (0, 0, 0))[1]
            if group == self.proc.pid:
                own.update(pid for pid, row in rows.items() if row[1] == group)
            while True:
                children = {pid for pid, row in rows.items() if row[0] in own}
                if children <= own:
                    break
                own.update(children)
            return {pid: rows[pid][2] for pid in own if pid in rows}
        except (OSError, ValueError, core.subprocess.SubprocessError):
            return None

    def _request(self, threshold, now_ts):
        """An open request is alive only if its last token/usage chunk (or its start) is newer than the threshold (4c-4). A request with only SSE
        comments flowing is 'connection open', not progress; past the threshold it counts as stuck (B313)."""
        import qe_core as core
        if not isinstance(self.proc, core.RecordedProcess):
            return False
        try:
            for path in (self.proc.attempt.directory / 'requests').glob('*.json'):
                mark = core.json.loads(path.read_text())
                pid = mark.get('pid')
                if not (type(pid) is int and core.pid_alive(pid)):
                    continue
                last = max(float(mark.get('progress_at') or 0), float(mark.get('started') or 0)) / 1000
                if not math.isfinite(last) or last > now_ts:
                    return None
                if 0 <= now_ts - last <= threshold:
                    return True
            return False
        except (OSError, ValueError, TypeError, AttributeError):
            return None  # unreadable evidence is not permission to cut

    def _step(self):
        import qe_core as core
        try:
            with self.logf.open('rb') as stream:
                stream.seek(self.offset)
                chunk = stream.read(1024 * 1024)
            self.offset += len(chunk)
        except OSError:
            return None
        lines = (self.remainder + chunk).split(b'\n')
        self.remainder = lines.pop()[-65536:]
        progress = False
        for raw in lines:
            try:
                event = core.json.loads(raw)
                part = event.get('part') or {}
                state = part.get('state') or {}
                if part.get('type') == 'tool':
                    key = str(part.get('callID') or part.get('id') or hashlib.sha256(
                        core.json.dumps([part.get('tool'), state.get('input')], sort_keys=True).encode()).hexdigest())
                    if state.get('status') in ('running', 'pending'):
                        if len(self.open_tools) < 1024:
                            self.open_tools.add(key)
                    else:
                        self.open_tools.discard(key)
                    self.open_step = bool(self.open_tools)
                if part.get('type') != 'tool' or state.get('status') != 'completed':
                    continue
                identity = hashlib.sha256(core.json.dumps([part.get('tool'), state.get('input'), state.get('output')],
                                                         sort_keys=True).encode()).hexdigest()
                if identity not in self.steps and len(self.steps) < 1024:
                    self.steps.add(identity)
                    progress = True
            except (ValueError, TypeError, AttributeError):
                continue
        return progress

    def stuck(self):
        import qe_core as core
        now_ts = core.time.monotonic()
        size, tree, cpu = self._size(), self._tree(), self._cpu()
        uncertain = size is None or tree is None or cpu is None
        movement = (size != self.size or tree != self.tree or
                   any(value > (self.cpu or {}).get(pid, 0) for pid, value in (cpu or {}).items()))
        if movement:
            self.movement_last = now_ts
        recent_movement = self.movement_last is not None and now_ts - self.movement_last <= self.min_sec
        threshold = max(self.min_sec, 3 * self.longest)
        open = self._request(threshold, core.time.time())
        step = self._step()
        if step:
            self.longest = min(self.min_sec, max(self.longest, now_ts - self.last))
            self.last = now_ts
            self.status, self.reason = 'productive', 'completed-tool-step'
        elif uncertain or recent_movement or open or open is None or step is None:
            self.status, self.reason = 'unknown', 'bounded-observation'
        else:
            self.status, self.reason = 'stalled', 'no-observed-activity'
        expired = now_ts - self.last > threshold
        if expired and self.status == 'unknown' and recent_movement and self.open_step:
            expired = now_ts - self.last > max(threshold, self.movement_limit)
            self.reason = 'active-time-limit' if expired else 'bounded-active-step'
        elif expired:
            self.reason = 'uncertain-time-limit' if self.status == 'unknown' else 'stall-time-limit'
        if self.record:
            core.atomic_json(self.record, {'longest': self.longest, 'status': self.status, 'reason': self.reason,
                                        'observation_limit_sec': threshold, 'movement_limit_sec': self.movement_limit,
                                        'last_evidence_monotonic': self.last, 'decision': 'stop' if expired else 'observe'})
        self.size, self.tree, self.cpu = size, tree, cpu
        return expired


class StallGuard:
    """STALL DETECTOR: log silent + model idle + no tool running -> cut, leave a marker.
    G-C13: if a worktree is given it also checks the approval block on every tick (all items BLOCKED -> "approval", not counted as a stall)."""

    def __init__(self, logf, proc, threshold, wt=None, loop_threshold=None):
        import qe_core as core
        self.logf, self.proc, self.threshold = logf, proc, threshold
        self.cut = False
        self.deferred = False
        self.wt = wt
        self.liveness = core.Liveness(logf, proc, wt, min_sec=threshold)
        self.approval_question = ""
        self.loop_test = ""
        self.loop = core.TestLoop(logf, int(core.ENV("CADRE_TEST_LOOP_THRESHOLD", "5")) if loop_threshold is None else loop_threshold)  # G-F9
        self.repeat = core.ToolRepeat(logf, int(core.ENV("CADRE_TOOL_REPEAT_THRESHOLD", "5")))  # B200; LIVE1: the same tool 5 times = loop

    def tick(self, project_ref):
        # cancel flag
        import qe_core as core
        if core.cancel_path(project_ref).exists():
            core.log(self.logf, "cadre-guard: CANCEL flag seen, stopping the run")
            try:
                if not core.ENV("CADRE_RUN_ID"):
                    core.cancel_path(project_ref).unlink()  # Legacy project-wide flag only.
            except Exception:
                pass
            core.kill_tree(self.proc.pid)
            return "cancel"
        if self.wt:
            question = core.approval_block(self.wt)
            if question:
                self.approval_question = question
                core.log(self.logf, f"cadre-guard: APPROVAL-BLOCKED — all items wait for the user's decision: {question[:160]}; stopping the run, no delivery (G-C13)")
                core.kill_tree(self.proc.pid)
                return "approval"
        id = self.loop.tick()
        if id:
            self.loop_test = id
            core.log(self.logf, f"cadre-guard: TEST-LOOP — {id} failed {self.loop.threshold}+ times in this attempt; stopping the attempt, counted as a stall (G-F9)")
            core.kill_tree(self.proc.pid)
            return "loop"
        tool = self.repeat.tick()
        if tool:
            self.loop_test = f"tool-repeat:{tool}"
            core.log(self.logf, f"cadre-guard: TOOL-REPEAT — {tool} called {self.repeat.threshold} times in a row with the same input and output; stopping the attempt, counted as a stall (B200)")
            core.kill_tree(self.proc.pid)
            return "loop"
        if not self.liveness.stuck():
            return None
        core.log(self.logf, f"cadre-guard: STALL — {self.liveness.reason}; bounded observation threshold exceeded; handing over the path with the same session")
        core.pathlib.Path(str(self.logf) + ".stall").touch()
        self.cut = True
        core.kill_tree(self.proc.pid)
        return "stall"
