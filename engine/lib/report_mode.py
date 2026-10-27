#!/usr/bin/env python3
"""Report/analysis mode (G-R1..G-R7): a report is a set of evidenced claims, not prose. Single owner: evidence appendix, deterministic report QA,
judge probe, plan rule and report-writer lane definition; no model call, evidence commands are read-only."""

from __future__ import annotations

import json
import os
import pathlib
import random
import re
import shutil
import signal
import subprocess
import sys
import time
from typing import Any, Callable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from run_store import report_declaration as declaration  # noqa: E402,F401  (the single definition of the mode is in the acceptance record)

EVIDENCE_KINDS = ("file", "command", "query", "url", "bug")
EXPECTED_MANDATORY = ("file", "command", "query", "bug")
ID_PATTERN = r"K(?:-[A-Za-z]{1,8}-?)?\d{1,4}"   # B224: lane-prefixed (K-tz1); B236: prefix-dash-number (K-line-1) is valid too, K-1 is not
ID = re.compile(ID_PATTERN)
def id_order(id: str) -> tuple[str, int]:
    """B229: evidence id order (prefix, number) from one place; a prefixed id (K-u1) is sorted too, int(id[1:]) would crash."""
    m = re.fullmatch(r"(K(?:-[A-Za-z]{1,8}-?)?)(\d{1,4})", id or "")
    return (m.group(1), int(m.group(2))) if m else (id or "", 0)


REF = re.compile(rf"(?<![\w-]){ID_PATTERN}(?![\w-])")   # B235: every evidence id in a sentence/table row is a citation ([K1], (K4, K6), K1..K4, bare K-dn5)
POLL_OUTCOME = ("verified", "wrong", "unverifiable")
POLL_AT_LEAST = 5
UNVERIFIABLE_RATIO = 0.4      # G-R5: if more than this cannot be verified the report is not proven
BUG_CAP = 10
BIG_FILE = 20 * 1024 * 1024
LINE_INTERVAL_CAP = 200   # at most this many lines in one file evidence (inclusive)
SENSITIVE_DIRECTORY = (".ssh", ".aws", ".gnupg", ".docker", ".kube", ".config/opencode", ".local/share/opencode", "Library/Keychains")
SENSITIVE_NAME = re.compile(r"(^|/)(\.env(\.[^/]*)?|[^/]*\.pem|[^/]*\.key|id_rsa[^/]*|id_ed25519[^/]*|credentials[^/]*|[^/]*-key[^/]*)$")
TITLE = re.compile(r"^\s{0,3}#{1,6}(\s|$)")
TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
UNVERIFIED = re.compile(r"unverified|unverifiable|not verified|cannot be verified|insufficient data")
NUMBER_EXCLUSIONS = [re.compile(p) for p in (
    r"`[^`]*`", r"https?://\S+", r"\]\([^)]*\)",
    r"[\w./~-]*\w\.[a-z][a-z0-9]{0,5}(?::\d+(?:-\d+)?)?\b",                   # file path (+line)
    r"\b\d{4}-\d{2}-\d{2}(?:[t ]\d{2}:\d{2}(?::\d{2})?)?\b",                  # ISO date
    r"\b(?:0?[1-9]|[12]\d|3[01])[./](?:0[1-9]|1[0-2])(?:[./](?:\d{4}|\d{2}))?\b",   # day.month(.year)
    r"\b\d{1,2}:\d{2}(?::\d{2})?\b", r"\bv?\d+(?:\.\d+){2,}\b",
    r"\b(?=\w*[^\W\d_])(?=\w*\d)\w+\b",                                       # letter+digit id (R1, B203, en-v30)
    r"\b[a-z]+(?:[-_][a-z]+)*[-_]\d+\s*(?:\.{2,3}|\u2026|-)\s*\d+\b",                 # id range (oss-001...004)
    r"\b[a-z]+(?:[-_][a-z]+)*[-_]\d+\b",                                        # B227: ticket/standard id (ops-001, utf-8)
    r"(?<![\w&#])#\d{1,5}\b",
    r"\b(?:priority|proposal|step|item|section|stage_label|phase|round|no|order|question|task|run|error|bug|finding|risk)\s*[:#.]?\s*\d{1,3}\b",
)]
LIST = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+")
DATED_KINDS = ("url", "query")


def _date_error(value: Any) -> str | None:
    """ISO day (YYYY-MM-DD), cannot be in the future; a missing/invalid date is data of unknown freshness."""
    import datetime
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return "date is absent or not YYYY-MM-DD"
    try:
        day = datetime.date.fromisoformat(value.strip())
    except ValueError:
        return "invalid calendar day"
    if day > datetime.date.today():
        return "date is in the future"
    return None


ANALYST_NOTE = "Analyst note (G-R7)"


def analyst_note(mode: str, reason: str = "") -> str:
    """N-REP03 (CR-30): without a strong second model a single analyst is stated explicitly in the report; the reconciliation shows on the same line."""
    if mode == "reconciliation":
        return f"{ANALYST_NOTE}: two independent analysts and a reconciler; diverging claims verified from their sources."
    return f"{ANALYST_NOTE}: single analyst; no second independent analyst ran ({reason or 'no second strong model available'})."


def analyst_record(run_dir: pathlib.Path, wt: pathlib.Path, mode: str, models: list[str], reason: str = "") -> dict[str, Any]:
    """CR-30: the analyst setup is written to the run record (`report_mode.analyst`) and as one line at the end of the report file; rewriting does not duplicate it."""
    from run_store import FileLock, atomic_json
    if mode not in ("single", "reconciliation"):
        raise ValueError("analyst-mode-invalid")
    entry = {"mode": mode, "first": models[0] if models else "", "second": models[1] if len(models) > 1 and models[1] else None,
             "reconciler": models[2] if len(models) > 2 and models[2] else None, "reason": reason, "ts": time.time()}
    run_dir = pathlib.Path(run_dir)
    with FileLock(run_dir / ".record.lock"):
        record = json.loads((run_dir / "run.json").read_text())
        declaration_ = record.get("report") or {}
        if not declaration_.get("path"):
            raise ValueError("no-report-declaration")
        declaration_["analyst"] = entry
        record["report"] = declaration_
        atomic_json(run_dir / "run.json", record)
    path = pathlib.Path(wt) / declaration_["path"]
    if path.is_file():
        lines = [s for s in path.read_text(errors="replace").rstrip("\n").splitlines() if not s.startswith(ANALYST_NOTE)]
        path.write_text("\n".join([*lines, "", analyst_note(mode, reason)]).strip("\n") + "\n")
    return entry


def analyst_note_apply(wt: pathlib.Path, declaration_: dict[str, Any]) -> bool:
    """CR-30: the note belongs to the engine; even if the correction round rewrites the report from scratch, the same line is added again before QA (idempotent)."""
    analyst = declaration_.get("analyst") if isinstance(declaration_.get("analyst"), dict) else None
    if not analyst or analyst.get("mode") not in ("single", "reconciliation") or not declaration_.get("path"):
        return False
    path = pathlib.Path(wt) / declaration_["path"]
    if not path.is_file():
        return False
    text = path.read_text(errors="replace")
    line = analyst_note(analyst["mode"], analyst.get("reason") or "")
    if line in text.splitlines():
        return False
    lines = [s for s in text.rstrip("\n").splitlines() if not s.startswith(ANALYST_NOTE)]
    path.write_text("\n".join([*lines, "", line]).strip("\n") + "\n")
    return True


def analyst_check(declaration_: dict[str, Any], text: str) -> list[str]:
    analyst = declaration_.get("analyst") if isinstance(declaration_.get("analyst"), dict) else None
    if not analyst or analyst.get("mode") not in ("single", "reconciliation"):
        return []
    lines = [s for s in text.splitlines() if s.startswith(ANALYST_NOTE)]
    if not lines:
        return [f"analyst-note-missing: report '{ANALYST_NOTE}' line is absent (N-REP03; recorded mode: {analyst['mode']})"]
    expected = "two independent analyst" if analyst["mode"] == "reconciliation" else "single analyst"
    if not any(expected in s for s in lines):
        return [f"analyst-note-contradicts: recorded mode '{analyst['mode']}' does not match the report line"]
    return []


def date_and_contradiction(records: list[dict[str, Any]], text: str) -> list[str]:
    """N-REP01: url/query evidence cannot be undated; the same source cannot carry different expected values for the same claim (FIX-20: different claims are separate evidence); with external data the report has a data date line."""
    findings = []
    for k in records:
        if k["round"] in DATED_KINDS:
            error = _date_error(k.get("date"))
            if error:
                findings.append(f"source-date: {k['id']}: {error}")
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for k in records:
        if k["round"] in DATED_KINDS:
            groups.setdefault((k["round"], _gap(k["source"]), _gap(str(k.get("claim") or ""))), []).append(k)
    for (round, _source, _claim), group in groups.items():
        if len({_gap(k.get("expected") or "") for k in group}) > 1:
            findings.append(f"contradicting-evidence: {', '.join(k['id'] for k in group)}: the same {round} source carries different expected values for the same claim; "
                            "explain the contradiction in the report or separate the evidence")
    if text.strip() and any(k["round"] in DATED_KINDS for k in records) and not re.search(r"(data|source) date", _ascii(text)):
        findings.append("without-data-date: external source/query evidence exists but the report has no Data date or Source date line (N-REP01)")
    return findings



def _ascii(text: str) -> str:
    return text.lower()


def _gap(text: str) -> str:
    return " ".join(text.split())


def record_declaration(run_dir: str | None = None) -> dict[str, Any] | None:
    try:
        return json.loads((pathlib.Path(run_dir or os.environ["CADRE_RUN_DIR"]) / "run.json").read_text()).get("report") or None
    except (KeyError, OSError, ValueError, AttributeError):
        return None


def evidence_read(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """G-R2: every line is a claim {id, claim, round, source, expected}; a broken line stays out of the record and becomes a finding."""
    records, findings, seen = [], [], set()
    for no, line in enumerate((text or "").splitlines(), 1):
        if not line.strip():
            continue
        try:
            k = json.loads(line)
        except ValueError:
            findings.append(f"evidence-broken: line {no}: not JSON")
            continue
        id = k.get("id") if isinstance(k, dict) else None
        if not isinstance(id, str) or not ID.fullmatch(id):
            findings.append(f"evidence-broken: line {no}: id must use K<number> or a lane-prefixed K-<name><number> form")
            continue
        if id in seen:
            findings.append(f"evidence-broken: line {no}: {id} repeat")
            continue
        if k.get("round") not in EVIDENCE_KINDS:
            findings.append(f"evidence-broken: line {no}: {id} unknown round {k.get('round')!r} ({'|'.join(EVIDENCE_KINDS)})")
            continue
        field_list = ("claim", "source", "expected") if k["round"] in EXPECTED_MANDATORY else ("claim", "source")
        missing = [a for a in field_list if not isinstance(k.get(a), str) or not k[a].strip()]
        if missing:
            findings.append(f"evidence-broken: line {no}: {id} missing fields: {', '.join(missing)}")
            continue
        seen.add(id)
        records.append(k)
    return records, findings


def evidence_ids(wt, declaration_: dict[str, Any]) -> list[str]:
    try:
        return [k["id"] for k in evidence_read((pathlib.Path(wt) / declaration_["evidence"]).read_text(errors="replace"))[0]]
    except OSError:
        return []


def unevidenced_numbers(text: str) -> list[tuple[int, str]]:
    """G-R3: every sentence or table row containing a number carries an evidence id or says 'unverified' explicitly. Dates, times, versions,
    ids, file:line, inline code, headings, list markers and table headers are not number claims. Unit: a sentence in prose
    (wrapped lines join inside a paragraph/item, B227), a row in a table. Result: the line where the sentence starts."""
    lines = (text or "").splitlines()
    outcome, code, block = [], False, []

    def finish():
        if block:
            outcome.extend(_block_unevidenced(block))
            block.clear()

    for i, line in enumerate(lines):
        plain = line.strip()
        if plain.startswith(("```", "~~~")):
            finish()
            code = not code
            continue
        if (code or not plain or TITLE.match(line) or TABLE_SEPARATOR.match(line) or plain.startswith("<!--")
                or (plain.startswith("|") and i + 1 < len(lines) and TABLE_SEPARATOR.match(lines[i + 1]))):
            finish()
            continue
        if plain.startswith("|"):
            finish()
            if not REF.search(line) and _number_claim(line, table=True):
                outcome.append((i + 1, line))
            continue
        if LIST.match(line):
            finish()
        block.append((i + 1, line))
    finish()
    return outcome


SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(\[*`])")
ABBREVIATION = re.compile(r"(?:^|[\s(\[\"“])(?:e\.g|i\.e|etc|cf|approx)\.$", re.I | re.ASCII)


def sentence_ends(text: str) -> list[re.Match[str]]:
    return [m for m in SENTENCE_END.finditer(text) if not ABBREVIATION.search(text[max(0, m.start() - 8):m.start()])]


REVIEW_SECTIONS = ('Findings', 'Uncertainties', 'Method')
EMPTY_ALLOWED_SECTIONS = frozenset({'Uncertainties'})


class ReportFormatError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__('; '.join(errors))
        self.errors = errors


def review_text(content: Any, evidence: list[dict[str, Any]]) -> str:
    """N-REP01: the writer chooses the sentence–evidence link; the engine only builds the display and produces no evidence or claim."""
    if isinstance(content, dict) and all(isinstance(k, str) for k in content):
        normalized = {next((name for name in REVIEW_SECTIONS if name.lower() == k.lower()), k): v for k, v in content.items()}
        if len(normalized) != len(content):
            raise ReportFormatError(['report cannot repeat the same section with different spellings'])
        content = normalized
    if not isinstance(content, dict) or set(content) != set(REVIEW_SECTIONS):
        raise ReportFormatError(['report must be an object containing Findings, Uncertainties and Method lists'])
    known = {k['id'] for k in evidence if isinstance(k, dict) and isinstance(k.get('id'), str)
               and ID.fullmatch(k['id'])}
    parts: list[str] = []
    errors: list[str] = []
    for section in REVIEW_SECTIONS:
        elements = content[section]
        if not isinstance(elements, list) or (not elements and section not in EMPTY_ALLOWED_SECTIONS):
            errors.append(f'{section}: at least one {{text, evidence_ids}} element is required')
            continue
        parts.append('# ' + section)
        for no, element in enumerate(elements, 1):
            location = f'{section}[{no}]'
            if not isinstance(element, dict) or set(element) != {'text', 'evidence_ids'}:
                errors.append(f'{location}: only text and evidence_ids fields are allowed')
                continue
            text, ids = element['text'], element['evidence_ids']
            element_error = len(errors)
            if not isinstance(text, str) or not text.strip() or text.splitlines() != [text]:
                errors.append(f'{location}: text must be a nonempty single line')
            elif (TITLE.match(text.strip()) or LIST.match(text.strip())
                  or text.strip().startswith(('|', '```', '~~~', '<!--')) or REF.search(text) or sentence_ends(text.strip())):
                errors.append(f'{location}: text must be a single sentence; the engine adds headings, lists and citations')
            if (not isinstance(ids, list) or any(not isinstance(k, str) or k not in known for k in ids)
                    or len(set(ids)) != len(ids)):
                errors.append(f'{location}: evidence_ids must contain distinct ids from the evidence appendix')
            elif section == 'Findings' and not ids:
                errors.append(f'{location}: every finding must carry its own evidence id')
            if len(errors) > element_error:
                continue
            text = text.strip()
            last = text[-1] if text[-1] in '.!?' else '.'
            body = text[:-1] if text[-1] in '.!?' else text
            citation = ' ' + ''.join('[' + k + ']' for k in ids) if ids else ''
            parts.append('- ' + body + citation + last)
        parts.append('')
    if errors:
        raise ReportFormatError(errors)
    return '\n'.join(parts)


def _block_unevidenced(block: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Paragraph or list item: lines join and are checked sentence by sentence; the first line of the sentence is reported."""
    parts, starts, position = [], [], 0
    for j, (no, line) in enumerate(block):
        text = LIST.sub("", line, count=1) if j == 0 else line
        starts.append((position, no, line))
        parts.append(text.strip())
        position += len(parts[-1]) + 1
    combined = " ".join(parts)
    outcome, start = [], 0
    for sentence_end in [m.start() for m in sentence_ends(combined)] + [len(combined)]:
        sentence = combined[start:sentence_end]
        if sentence.strip() and not REF.search(sentence) and _number_claim(sentence):
            no, line = next((n, st) for k, n, st in reversed(starts) if k <= start)
            if not outcome or outcome[-1][0] != no:
                outcome.append((no, line))
        start = sentence_end + 1
    return outcome


def _number_claim(part: str, table: bool = False) -> bool:
    f = _ascii(part)
    if UNVERIFIED.search(f):
        return False
    if table:
        cells = f.strip().strip("|").split("|")
        if cells and re.fullmatch(r"\s*\d{1,3}\s*", cells[0]):   # order column
            f = "|".join(cells[1:])
    for pattern in NUMBER_EXCLUSIONS:
        f = pattern.sub(" ", f)
    return bool(re.search(r"\d", f))


def _sensitive(path: pathlib.Path) -> bool:
    try:
        real = path.expanduser().resolve()
    except (OSError, RuntimeError):
        return True
    home_dir = pathlib.Path.home().resolve()
    return any(real == home_dir / d or (home_dir / d) in real.parents for d in SENSITIVE_DIRECTORY) or bool(SENSITIVE_NAME.search(real.as_posix()))


def _parse(wt, entry_name: str) -> pathlib.Path:
    path = pathlib.Path(os.path.expanduser(entry_name))
    return (path if path.is_absolute() else pathlib.Path(wt) / path).resolve()


def _file_evidence(wt, k: dict[str, Any], base_same: bool = False) -> str | None:
    m = re.match(r"^(?P<path>.+?)(?::(?P<start>\d+)(?:-(?P<last>\d+))?)?$", k["source"].strip())
    entry_name = m.group("path")
    path = pathlib.Path(os.path.expanduser(entry_name))
    path = path if path.is_absolute() else pathlib.Path(wt) / path
    if _sensitive(path):
        return f"sensitive path cannot be read ({entry_name})"
    root = pathlib.Path(wt).resolve()
    try:   # B267: places the writer can write itself (outside the tree, files git ignores/does not track) cannot be evidence
        relative = path.resolve().relative_to(root)
    except ValueError:
        return f"outside the working tree: {entry_name}"
    if not path.is_file():
        return f"file absent: {entry_name}"
    git = lambda *a: subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, timeout=60).returncode
    if git("ls-files", "--error-unmatch", "--", str(relative)) != 0:
        return f"not tracked by git: {entry_name} (untracked or ignored files cannot be evidence)"
    if not base_same and git("diff", "--quiet", "HEAD", "--", str(relative)) != 0:
        return f"changed during the task: {entry_name}"
    if m.group("start"):
        start, last = int(m.group("start")), int(m.group("last") or m.group("start"))
        if start < 1 or last < start or last - start + 1 > LINE_INTERVAL_CAP:   # B298: inclusive line count
            return f"invalid line range: {k['source']}"
        part = []
        with path.open(errors="replace") as f:
            for no, line in enumerate(f, 1):
                if no >= start:
                    part.append(line)
                if no >= last:
                    break
        if len(part) < last - start + 1:
            return f"line absent: {k['source']}"
        text, location = "".join(part), k["source"]
    else:
        if path.stat().st_size > BIG_FILE:
            return f"file too large; specify lines: {entry_name}"
        text, location = path.read_text(errors="replace"), entry_name
    if _gap(k["expected"]) not in _gap(text):
        hint = ""
        searched = _gap(k["expected"])
        if m.group("start") and searched and path.stat().st_size <= 5_000_000:
            with path.open(errors="replace") as f:
                lines = [no for no, line in enumerate(f, 1) if searched in _gap(line)][:3]
            if lines:
                hint = f" (quote found on file line {', '.join(map(str, lines))}; cite this line number)"
        return f"quote absent from {location}: {k['expected'][:80]!r}{hint}"
    return None


def _sbpl(text: str) -> str:
    return json.dumps(str(text))


def _profile(wt, temporary, source_read_only: bool = False) -> str:
    """Read-only evidence sandbox: no network; writes only to the working tree + a temporary directory; sensitive directories/files are not read."""
    home_dir = pathlib.Path.home().resolve()
    reading = " ".join(f"(subpath {_sbpl(home_dir / d)})" for d in SENSITIVE_DIRECTORY)
    tree = "" if source_read_only else f"(subpath {_sbpl(pathlib.Path(wt).resolve())}) "
    return ("(version 1)(allow default)(deny network*)(deny file-write* (subpath \"/\"))"
            f"(allow file-write* {tree}(subpath {_sbpl(pathlib.Path(temporary).resolve())})"
            " (literal \"/dev/null\") (literal \"/dev/tty\") (literal \"/dev/dtracehelper\") (regex #\"^/dev/fd/\"))"
            f"(deny file-read* {reading} (regex #\"/\\.env(\\.[^/]*)?$\") (regex #\"\\.(pem|key)$\")"
            " (regex #\"/id_(rsa|ed25519)[^/]*$\") (regex #\"/credentials[^/]*$\") (regex #\"-key[^/]*$\"))")


def _wait(proc: subprocess.Popen, duration: float) -> int:
    try:
        return proc.wait(timeout=duration)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait(timeout=10)
        raise


COMMAND_OUTPUT_CAP = 1_000_000


def command_execute(command: str, wt, temporary, duration: float, wait: Callable | None = None,
              source_read_only: bool = False) -> tuple[int | None, str]:
    """(rc, output); rc None = could not run/timeout (the output says why). The environment is built from an allow-list (no key leaks)."""
    sandbox = shutil.which("sandbox-exec") or "/usr/bin/sandbox-exec"
    if not os.access(sandbox, os.X_OK):
        return None, "sandbox-exec is absent: evidence command was not run (fail-closed)"
    g = pathlib.Path(temporary)
    g.mkdir(parents=True, exist_ok=True)
    order = len(list(g.glob("command-*.sh")))
    script = g / f"command-{order}.sh"
    script.write_text(command + "\n")
    env = {a: os.environ[a] for a in ("PATH", "HOME", "LANG", "LC_ALL", "USER", "LOGNAME", "SHELL") if a in os.environ}
    env.update(TMPDIR=str(g.resolve()), PYTHONDONTWRITEBYTECODE="1", TERM="dumb",
               UV_CACHE_DIR=str(g.resolve() / "uv-cache"), UV_OFFLINE="1")   # B232: `uv run` cannot write to ~/.cache/uv in the sandbox; the network is closed anyway
    import threading
    proc = subprocess.Popen([sandbox, "-p", _profile(wt, g, source_read_only), "/bin/bash", str(script)], cwd=str(wt), env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    buffer = bytearray()
    stop = threading.Event()
    fd = proc.stdout.fileno()
    os.set_blocking(fd, False)

    def read() -> bool | None:
        try:
            part = os.read(fd, 65536)
        except BlockingIOError:
            return None
        if len(buffer) < COMMAND_OUTPUT_CAP:
            buffer.extend(part[:COMMAND_OUTPUT_CAP - len(buffer)])
        return bool(part)

    def sum() -> None:   # B298: output above the cap is not written to disk either, it is read and discarded (it used to flow unbounded into a file)
        while not stop.is_set():
            was_read = read()
            if was_read is False:
                break
            if was_read is None:
                stop.wait(.01)
    reader = threading.Thread(target=sum, daemon=True)
    reader.start()
    try:
        rc = (wait or _wait)(proc, duration)
    except subprocess.TimeoutExpired:
        return None, f"time limit exceeded ({int(duration)} seconds)"
    finally:
        # B306: when the main command ends, also close children that inherited stdout; no close/join waiting for EOF.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):   # on timeout the group may already have been closed by _wait (EPERM)
            pass
        stop.set()
        reader.join()
        for _ in range((COMMAND_OUTPUT_CAP + 65535) // 65536):
            if len(buffer) >= COMMAND_OUTPUT_CAP or not read():
                break
        proc.stdout.close()
        proc.wait(timeout=1)
    return rc, bytes(buffer).decode(errors="replace")


def changed_product(wt, source_sha: str | None, excluded=()) -> list[str]:
    from outcome import CACHE_SURPLUS

    def git(*args):
        return subprocess.run(["git", "-C", str(wt), *args], capture_output=True, text=True, timeout=60).stdout
    names = set(git("diff", "--name-only", "-z", "HEAD", "--", ".", *excluded).split("\0"))
    names |= set(git("ls-files", "--others", "--exclude-standard", "-z", "--", ".", *excluded).split("\0"))
    if source_sha:
        names |= set(git("diff", "--name-only", "-z", source_sha, "--", ".", *excluded).split("\0"))
    return sorted(a for a in names if a and not CACHE_SURPLUS.search(a))


EVIDENCE_LIMIT = re.compile(r'at\s+most\s+(\d{1,3})\s+(?:direct\s+)?file:line\s+(?:citation|evidence|reference)', re.I | re.ASCII)


def evidence_limit(*texts: str) -> int | None:
    numbers = [int(m.group(1)) for text in texts for m in EVIDENCE_LIMIT.finditer(text or '')]
    return min(numbers) if numbers else None


COMMAND_EVIDENCE_FORBIDDEN = re.compile(r'(?:no\s+command\s+evidence|do\s+not\s+(?:provide|use|produce)\s+command\s+evidence|command\s+evidence[^.;\n]{0,160}?\b(?:is\s+(?:forbidden|prohibited|absent)|must\s+not\s+be\s+(?:provided|used|produced)|is\s+not\s+(?:provided|used)))\b', re.I | re.ASCII)
EXECUTED_EVIDENCE_KINDS = frozenset({"command", "query", "bug"})


def evidence_kind_bans(*texts: str) -> frozenset[str]:
    return EXECUTED_EVIDENCE_KINDS if any(COMMAND_EVIDENCE_FORBIDDEN.search(_ascii(m or '')) for m in texts) else frozenset()


def _section_inside(source: str, intervals: list[tuple[int, int]]) -> bool:
    m = re.search(r":(\d+)(?:-(\d+))?$", source.strip())
    return bool(m) and any(a <= int(m[1]) <= int(m[2] or m[1]) <= b for a, b in intervals)


def report_qa(wt, declaration_: dict[str, Any], source_sha: str | None, temporary, seed: str = "", excluded=(),
             wait: Callable | None = None, *, texts: tuple[str, str] | None = None,
             start: dict[str, str] | None = None, source_read_only: bool = False,
             evidence_limit: int | None = None, source_paths: list[pathlib.Path] | None = None,
             source_sections: dict[pathlib.Path, list[tuple[int, int]]] | None = None,
             forbidden_kinds: frozenset[str] = frozenset()) -> dict[str, Any]:
    """G-R3/G-R4: report + evidence appendix are checked mechanically; a single finding is red and goes to the correction round as a concrete list."""
    wt = pathlib.Path(wt)
    if source_paths is not None:
        source_paths = [pathlib.Path(p).resolve() for p in source_paths]
    sections = {pathlib.Path(p).resolve(): list(intervals) for p, intervals in (source_sections or {}).items()}
    path, evidence = declaration_["path"], declaration_["evidence"]
    findings: list[str] = []
    text = texts[0] if texts is not None else ((wt / path).read_text(errors="replace") if (wt / path).is_file() else "")
    evidence_text = texts[1] if texts is not None else ((wt / evidence).read_text(errors="replace") if (wt / evidence).is_file() else "")
    if not text.strip():
        findings.append(f"no-report: {path} absent or empty")
    if not evidence_text.strip():
        findings.append(f"without-evidence: {evidence} absent or empty (G-R2)")
    records, broken = evidence_read(evidence_text)
    findings += broken
    file_count = sum(1 for k in records if k['round'] == 'file')
    if evidence_limit is not None and file_count > evidence_limit:
        findings.append(f'evidence-limit: request allows at most {evidence_limit} file:line references, but {file_count} were supplied; '
                        'merge or remove claims to meet the limit')
    if text.strip():
        titles = [_ascii(s) for s in text.splitlines() if TITLE.match(s)]
        findings += [f"section-missing: {b}" for b in declaration_.get("sections") or [] if not any(_ascii(b) in s for s in titles)]
        missing = unevidenced_numbers(text)
        findings += [f"unsupported-number: {path}:{no}: {line.strip()[:160]}" for no, line in missing[:20]]
        if len(missing) > 20:
            findings.append(f"unsupported-number: total {len(missing)} lines (first 20 listed)")
        known = {k["id"] for k in records}
        used = set(ID.findall(" ".join(REF.findall(text))))
        unknown = sorted(used - known, key=id_order)
        if unknown:
            findings.append(f"unknown-evidence: {', '.join(unknown[:20])} absent from the evidence appendix")
    base_same = False
    if texts is not None and start is not None:
        from run_store import plan_product
        base_same = plan_product(wt) == start
    own = set() if texts is not None else {(wt / declaration_["path"]).resolve(), (wt / declaration_["evidence"]).resolve()}   # B261: resolved path (absolute/symlink included)
    for k in records:
        file = _parse(wt, re.sub(r":\d+(?:-\d+)?$", "", k["source"].strip())) if k['round'] == 'file' else None
        if (file is not None and source_paths is not None
                and not any(file == p or (p.is_dir() and file.is_relative_to(p)) for p in source_paths)):
            findings.append(f"file-scope: {k['id']}: {k['source']} outside the accepted review plan’s reading scope")
        elif file is not None and file in sections and not _section_inside(k["source"], sections[file]):
            allowed = ", ".join(f"{a}-{b}" for a, b in sections[file])
            findings.append(f"section-scope: {k['id']}: {k['source']} outside the selected source section; allowed lines {allowed}")
        elif file in own:
            findings.append(f"own-citation: {k['id']}: the report or appendix cannot cite itself; use reproducible command evidence for counts (B228)")
        elif k["round"] == "file":
            error = _file_evidence(wt, k, base_same)
            if error:
                findings.append(f"file-source: {k['id']}: {error}")
        elif k["round"] == "url" and not re.fullmatch(r"https?://\S+", k["source"].strip()):
            findings.append(f"url-source: {k['id']}: invalid URL")
    findings += date_and_contradiction(records, text)
    findings += analyst_check(declaration_, text)
    forbidden = [k for k in records if k["round"] in forbidden_kinds]
    if forbidden:
        findings.append(f"evidence-kind-forbidden: {', '.join(k['id'] for k in forbidden[:20])}: request "
                        f"{'/'.join(sorted({k['round'] for k in forbidden}))} evidence is forbidden; it was not executed; remove it or replace it with file:line evidence")
    duration = float(os.environ.get("CADRE_REPORT_COMMAND_SEC", "120"))
    commands = sorted((k for k in records if k["round"] in ("command", "query") and k["round"] not in forbidden_kinds),
                      key=lambda k: id_order(k["id"]))
    sample = sorted(random.Random(seed).sample(commands, min(int(os.environ.get("CADRE_REPORT_COMMAND_SAMPLE", "5")), len(commands))),
                   key=lambda k: id_order(k["id"]))
    for k in sample:
        rc, output = command_execute(k["source"], wt, temporary, duration, wait, source_read_only)
        if rc is None:
            findings.append(f"command-source: {k['id']}: {output}")
        elif _gap(k["expected"]) not in _gap(output):
            findings.append(f"command-source: {k['id']}: expected {k['expected'][:80]!r} absent from output (rc={rc}; output: {_gap(output)[:200]!r})")
    bugs = [k for k in records if k["round"] == "bug" and "bug" not in forbidden_kinds]
    bug_cap = int(os.environ.get("CADRE_REPORT_BUG_CAP", BUG_CAP))
    if len(bugs) > bug_cap:   # B262: a claim above the cap cannot silently stay unchecked
        findings.append(f"bug-cap: {len(bugs)} bug claims, cap {bug_cap} (CADRE_REPORT_BUG_CAP); "
                        f"{', '.join(k['id'] for k in bugs[bug_cap:][:20])} were not checked; merge claims or remove them from the evidence (G-R4)")
    for k in bugs[:bug_cap]:
        rc, output = command_execute(k["source"], wt, temporary, duration, wait, source_read_only)
        if rc is None:
            findings.append(f"bug-again: {k['id']}: {output}")
        elif rc == 0:
            findings.append(f"bug-again: {k['id']}: reproduction command succeeded (rc=0); the bug is not visible in the existing code (G-R4)")
        elif _gap(k["expected"]) not in _gap(output):
            findings.append(f"bug-again: {k['id']}: command failed but expected error {k['expected'][:80]!r} absent (output: {_gap(output)[:200]!r})")
    if texts is not None:
        from run_store import plan_product
        if start is None:
            raise ValueError('review-source-identity-required')
        outside = ['source-changed'] if plan_product(wt) != start else []
    else:
        outside = [a for a in changed_product(wt, source_sha, excluded) if a not in (path, evidence)]
    if outside:
        findings.append(f"report-outside: {', '.join(outside[:20])} changed; only {path} and {evidence} may change in a report task (G-R1)")
    return {"status": "FAIL" if findings else "PASS", "findings": findings,
            "counter": {"evidence": len(records), "command_sample": len(sample), "command_total": len(commands)}}


def _poll_find(raw: str, digest: str | None) -> list[Any]:
    from outcome import _json_candidates
    found = [a["poll"] for a in _json_candidates(raw or "") if isinstance(a, dict) and a.get("revision") == digest and "poll" in a]
    if found:
        if len({json.dumps(b, sort_keys=True) for b in found}) > 1:
            raise ValueError("conflicting poll lists")
        return found[-1]
    return [{"id": "K" + m.group(1)[1:], "outcome": m.group(2).lower(), "evidence": m.group(3)} for m in re.finditer(
        rf"^\s*POLL\s+({ID_PATTERN})\s*:\s*(verified|wrong|unverifiable)\s*[-—:]\s*(.+?)\s*$", raw or "", re.M | re.I | re.ASCII)]


def poll_apply(raw: str, review: dict[str, Any], ids: list[str] | None, at_least: int | None = None) -> dict[str, Any]:
    """G-R5: a valid review must probe at least min(5, evidence count) different, existing ids; a missing/invented probe
    is a malformed review. A single 'wrong' or an unverifiable ratio above the threshold produces REVISE and a `report_poll` FAIL item."""
    if ids is None or review.get("status") not in ("PASS", "FAIL"):
        return review
    from outcome import placeholder
    at_least = min(POLL_AT_LEAST, len(ids)) if at_least is None else at_least
    try:
        poll = _poll_find(raw, (review.get("revision") or {}).get("digest"))
        if not isinstance(poll, list):
            raise ValueError("poll is not a list")
        seen = set()
        for y in poll:
            id = y.get("id") if isinstance(y, dict) else None
            if id not in ids or id in seen:
                raise ValueError(f"unknown or duplicate evidence id: {id!r}")
            if y.get("outcome") not in POLL_OUTCOME:
                raise ValueError(f"{id}: outcome {'|'.join(POLL_OUTCOME)} is required")
            if not isinstance(y.get("evidence"), str) or placeholder(y["evidence"]):
                raise ValueError(f"{id}: poll evidence is absent")
            seen.add(id)
        if len(seen) < at_least:
            raise ValueError(f"poll missing: {len(seen)} claim < {at_least}")
    except ValueError as exc:
        return {"status": "ERROR", "revision": review.get("revision"), "reason": f"invalid report probe: {exc}"}
    problematic = [y for y in poll if y["outcome"] == "wrong"]
    uncertain = [y for y in poll if y["outcome"] == "unverifiable"]
    outcome = {**review, "poll": poll}
    if problematic or len(uncertain) > UNVERIFIABLE_RATIO * len(poll):
        summary = "; ".join(f"{y['id']} {y['outcome']}: {y['evidence'][:200]}" for y in problematic + uncertain)
        outcome.update(status="FAIL", verdict="REVISE",
                     acceptance=[*(review.get("acceptance") or []), {"id": "report_poll", "status": "FAIL", "evidence": summary}])
    return outcome


def poll_prompt(declaration_: dict[str, Any], ids: list[str], line: bool = False) -> str:
    at_least = min(POLL_AT_LEAST, len(ids))
    text = (f"REPORT PROBE (G-R5): this is a report; evidence appendix {declaration_['evidence']} ({len(ids)} claims). Choose a stratified sample "
             f"(numbers, bugs, proposals; at least {at_least} distinct evidence ids) and independently verify each FROM ITS SOURCE: open the file, "
             "run the command or query. Do not trust the report’s own sentence. Add a poll field to the JSON: "
             "[{\"id\": \"K3\", \"outcome\": \"verified|wrong|unverifiable\", \"evidence\": \"what you observed\"}]. "
             "If any outcome is wrong, use verdict=REVISE.\n")
    if line:
        text += "When using line format, place each probe on one line: POLL <id>: verified|wrong|unverifiable - <what you observed>\n"
    return text


CLAIM_SUPPORT = ("full", "partial", "absent")
TERM_STATUS = ("negative", "context", "equivalent", "unsupported")
REQUIREMENT_STATUS = ("satisfied", "missing", "report_outside")
TERM_OUTSIDE = frozenset({"script", "scripts", "file", "module", "function", "class", "method", "line", "code", "source", "and", "the", "with", "from", "for", "that", "this", "into", "its", "are", "has", "not", "does", "only", "then", "same", "than", "was", "were"})
FILE_NAME = re.compile(r"[\w./~-]*\w\.[A-Za-z][A-Za-z0-9]{0,5}\b")
CARD_ID = re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\b")
EXTRA_REQUIREMENT = re.compile(r"extra-\d{1,3}")
PROCESS_WORDS = frozenset({
    'budget', 'duration', 'constant', 'stays', 'remains', 'expense', 'cap', 'usd', 'minute', 'minutes', 'min', 'source',
    'unchanged', 'read', 'only', 'independent', 'final', 'clean', 'separate', 'review', 'judge', 'session',
    'is', 'are', 'will', 'be', 'preserved', 'commit', 'push', 'absent', 'and', 'or', 'at', 'most', 'fixed'})


def process_item(text: str) -> bool:
    words = re.findall(r'[a-z0-9]+', text.lower())
    return bool(words) and all(word.isdigit() or word in PROCESS_WORDS for word in words)


MECHANICAL_EVIDENCE = (re.compile(r'at\s+most\s+(\d{1,3})\s+(?:direct\s+)?file:line\s+(?:citations?|evidence(?:\s+references?)?|references?)'),
                      COMMAND_EVIDENCE_FORBIDDEN)
MECHANICAL_PADDING = frozenset({'use', 'used', 'is', 'are', 'provided', 'and', 'must', 'be', 'not', 'exceeded'})


def mechanical_item(text: str) -> bool:
    remaining, found = _ascii(text), False
    for pattern in MECHANICAL_EVIDENCE:
        remaining, count = pattern.subn(' ', remaining)
        found = found or bool(count)
    return found and all(s in MECHANICAL_PADDING for s in re.findall(r'[a-z0-9]+', remaining))


def mechanical_decision(definition: dict[str, str], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    text = _ascii(definition['text'])
    file = sum(1 for k in evidence if isinstance(k, dict) and k.get('round') == 'file')
    problems = []
    limit = MECHANICAL_EVIDENCE[0].search(text)
    if limit and file > int(limit.group(1)):
        problems.append(f'file:line evidence limit exceeded ({file}>{limit.group(1)})')
    executed = [k.get('id') for k in evidence if isinstance(k, dict) and k.get('round') in EXECUTED_EVIDENCE_KINDS]
    if MECHANICAL_EVIDENCE[1].search(text) and executed:
        problems.append('forbidden executed evidence: ' + ', '.join(map(str, executed)))
    return {**definition, 'status': 'missing' if problems else 'satisfied', 'report': [],
            'rationale': 'engine evidence appendix check: ' + ('; '.join(problems) or 'limit and evidence kinds conform')}
SEMANTIC_JUDGE_RULE = (
    'Make three separate decisions. (1) poll: whether the quote exists in the source. '
    '(2) claims: assign support=full|partial|absent to every evidenced sentence. Use full only when evidence_texts '
    'within that sentence’s evidence_ids ranges show every claimed step or term. evidence_non_terms are word warnings: '
    'the term occurs in scope files but not verbatim in the cited ranges. term_warnings show its location and whether '
    'it came from the original request. A missing literal word alone does not reduce support, and a matching word '
    'does not establish the claimed behavior. Each sentence appears once in report; report_elements id=Section-N '
    'identifies item N of that section and links it to evidence_ids. Each claim id identifies the same report element. '
    'Judge calls, conditions, values and targets against their own cited evidence_texts ranges. Term decisions are optional; '
    'warnings are not decisions, and support is the decision field. If a term claims a call, condition or value absent '
    'from the range, write terms:[{term,status:unsupported}] and support=partial|absent. Optional statuses are negative '
    '(the sentence states absence), context (unclaimed context or naming from the request), and equivalent '
    '(the range shows the same behavior with different words or structure). Audit claims without warnings separately '
    'against their own ranges too: out-of-range calls or conditions, wrong values, invisible implementation details '
    'and missing expected values are unsupported. (3) requirements: assign status=satisfied|missing|report_outside '
    'and report:[report_elements ids] to each requirement. Satisfied requirements must be demonstrated by delivered '
    'report elements; your explanation, facts read in the source but absent from the report, and plan text do not count. '
    'The report list cannot be empty for satisfied. A content requirement requesting the absence of a behavior needs '
    'a report element explicitly stating that absence; otherwise it is missing. Whole-delivery prohibitions and '
    'presentation constraints differ: if you checked that no test counts or execution PASS claims appear anywhere, '
    'list the actual report elements inspected; do not demand an extra sentence about the report’s own omissions. '
    'The engine resolves round=mechanical requirements (evidence count/kind) by inspecting the appendix; do not decide them. '
    'A ban on test counts does not forbid expected assertion values (for example len(lines) == 3 and expected package order); '
    'requirements requesting concrete expected values are missing when those values are absent from the report. '
    'When requirements conflict, original-request R items take priority over plan items. QA is only a mechanical check '
    'and does not establish missing findings or correct source interpretation. Use report_outside only for requirements '
    'the engine marked round=process (budget, duration, unchanged source, independent review). Content requirements '
    'are satisfied in the report or missing. You may add omitted request requirements as extra-1, extra-2 with a text field. ')


def review_elements(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, dict):
        content = {next((name for name in REVIEW_SECTIONS if name.lower() == k.lower()), k): v for k, v in content.items()}
    elements = []
    for section in REVIEW_SECTIONS:
        for no, element in enumerate((content or {}).get(section) or [], 1):
            elements.append({'id': f'{section}-{no}', 'section': section, 'text': str(element['text']).strip(),
                           'evidence_ids': list(element['evidence_ids'])})
    return elements


def acceptance_items(text: str) -> list[str]:
    parts = (p.strip().rstrip('.!?').strip() for p in re.split(r'[;\n]+|(?<=[.!?])\s+', text or ''))
    return [p for p in parts if len(p) >= 3]


def _item_kind(text: str) -> str:
    return 'mechanical' if mechanical_item(text) else 'process' if process_item(text) else 'content'


def _item_key(text: str) -> str:
    return ' '.join(re.findall(r'[a-z0-9]+', _ascii(text)))


def review_requirements(acceptance: str, plan: dict[str, Any]) -> list[dict[str, str]]:
    outcome = [{'id': f'R{no}', 'criterion': 'request', 'round': _item_kind(m), 'text': m}
             for no, m in enumerate(acceptance_items(acceptance), 1)]
    seen = {_item_key(g['text']) for g in outcome}
    for task in plan.get('tasks') or []:
        for no, m in enumerate(acceptance_items(str(task.get('acceptance') or '')), 1):
            if _item_key(m) in seen:
                continue
            seen.add(_item_key(m))
            outcome.append({'id': f"{task['id']}.{no}", 'criterion': 'task:' + str(task['id']), 'round': _item_kind(m), 'text': m})
    return outcome


REVIEW_EVIDENCE_TEXT_CAP = 8_000
REVIEW_CATALOG_TEXT_CAP = 48_000


def review_evidence_catalog(wt: pathlib.Path, paths: list[pathlib.Path],
                           sections: dict[pathlib.Path, list[tuple[int, int]]] | None = None) -> dict[str, Any]:
    """N-REP01: immutable ranges from allowed sources; selection belongs to the writer, numbering to the engine."""
    from run_store import bounded_bytes
    root = pathlib.Path(wt).resolve()
    sections = {pathlib.Path(p).resolve(): ranges for p, ranges in (sections or {}).items()}
    evidence: list[dict[str, Any]] = []
    missing: list[str] = []
    total = 0
    for path in sorted({pathlib.Path(p).resolve() for p in paths}):
        relative = str(path.relative_to(root))
        if _sensitive(path):
            raise ValueError('sensitive sources cannot enter the evidence catalog')
        if not path.is_file() or path.stat().st_size > 2_000_000:
            missing.append(relative)
            continue
        try:
            lines = bounded_bytes(path, 2_000_000).decode(errors='replace').splitlines()
        except (OSError, ValueError) as exc:
            raise ValueError('review-source-packet-unavailable') from exc
        for start, last in sections.get(path, [(1, len(lines))]):
            if start < 1 or last > len(lines) or last < start:
                if lines:
                    raise ValueError('invalid source section in the evidence catalog')
                continue
            while start <= last:
                part: list[str] = []
                size = 0
                for line in lines[start - 1:min(last, start - 1 + LINE_INTERVAL_CAP)]:
                    fresh = size + len(line) + int(bool(part))
                    if fresh > REVIEW_EVIDENCE_TEXT_CAP or total + fresh > REVIEW_CATALOG_TEXT_CAP:
                        break
                    part.append(line)
                    size = fresh
                if not part:
                    missing.append(relative)
                    if total >= REVIEW_CATALOG_TEXT_CAP:
                        break
                    start += 1
                    continue
                end = start + len(part) - 1
                quote = next((s for s in part if s.strip()), '')
                if quote:
                    evidence.append({'id': f'K{len(evidence) + 1}', 'round': 'file',
                                     'source': f'{relative}:{start}-{end}', 'expected': quote,
                                     'text': '\n'.join(part)})
                    total += size
                start = end + 1
    return {'evidence': evidence, 'missing_paths': sorted(set(missing))}


def review_evidence_selection(answer: dict[str, Any], catalog: dict[str, Any]) -> list[dict[str, Any]]:
    if 'evidence_refs' not in answer:
        evidence = answer.get('evidence')
        if not isinstance(evidence, list) or not evidence:
            raise ValueError('review-output-invalid')
        return evidence
    choice = answer['evidence_refs']
    if 'evidence' in answer or not isinstance(choice, list) or not choice or len(choice) > 20:
        raise ValueError('evidence_refs must be a single nonempty evidence selection')
    candidates = {k['id']: k for k in catalog['evidence']}
    seen: set[str] = set()
    outcome: list[dict[str, Any]] = []
    for selected in choice:
        if (not isinstance(selected, dict) or set(selected) != {'id', 'claim'}
                or not isinstance(selected['id'], str) or selected['id'] not in candidates
                or selected['id'] in seen or not isinstance(selected['claim'], str) or not selected['claim'].strip()):
            raise ValueError('evidence_refs may contain only distinct catalog ids and claim fields')
        seen.add(selected['id'])
        outcome.append({k: v for k, v in candidates[selected['id']].items() if k != 'text'} | {'claim': selected['claim']})
    return outcome


def evidence_text(wt, evidence: dict[str, Any], cap: int = REVIEW_EVIDENCE_TEXT_CAP) -> str:
    if evidence.get('round') != 'file':
        return str(evidence.get('expected') or '')[:cap]
    m = re.match(r"^(?P<path>.+?)(?::(?P<start>\d+)(?:-(?P<last>\d+))?)?$", str(evidence.get('source') or '').strip())
    try:
        path = _parse(wt, m.group('path'))
        path.relative_to(pathlib.Path(wt).resolve())
        if _sensitive(path) or not path.is_file() or path.stat().st_size > BIG_FILE:
            return ''
        lines = path.read_text(errors='replace').splitlines()
    except (AttributeError, OSError, ValueError):
        return ''
    if m.group('start'):
        lines = lines[int(m.group('start')) - 1:int(m.group('last') or m.group('start'))]
    return '\n'.join(lines)[:cap]


def _code_similar(term: str) -> bool:
    return '_' in term or '-' in term or any(c.isdigit() for c in term) or any(c.isupper() for c in term[1:])


def _term_pattern(term: str) -> re.Pattern[str]:
    root = term if len(term) < 6 else term[:max(6, len(term) - 2)]
    return re.compile(r'(?<![A-Za-z0-9])' + re.escape(root) + (r'(?![A-Za-z])' if len(term) < 6 else ''), re.I | re.ASCII)


def claim_terms(text: str) -> list[str]:
    text = FILE_NAME.sub(' ', text)
    terms = CARD_ID.findall(text)
    for word in re.findall(r'\w+', CARD_ID.sub(' ', text)):
        if (word.isascii() and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{2,}', word)
                and word.lower() not in TERM_OUTSIDE and not word.lower().startswith('test')):
            terms.append(word)
    single: dict[str, str] = {}
    for term in terms:
        single.setdefault(term.lower(), term)
    return list(single.values())


def evidence_non_terms(text: str, evidence: list[str], scope: list[str], section_scope: list[str] = ()) -> list[str]:
    outcome = []
    for term in claim_terms(text):
        pattern = _term_pattern(term)
        in_scope = any(pattern.search(k) for k in scope) or (_code_similar(term) and any(pattern.search(k) for k in section_scope))
        if in_scope and not any(pattern.search(k) for k in evidence):
            outcome.append(term)
    return outcome


TERM_LINE_CAP = 3
CODE_LINE_CAP = 2


SECTION_LINE = re.compile(r'^(\d+)\t')


def _lines(text: str, pattern: re.Pattern[str], location: Callable[[int, str], str], cap: int) -> list[str]:
    found = []
    for no, line in enumerate(text.splitlines()):
        if pattern.search(line):
            found.append(f'{location(no, line)}: {SECTION_LINE.sub("", line).strip()[:200]}')
            if len(found) == cap:
                break
    return found


def _line_no(i: int, line: str) -> str:
    m = SECTION_LINE.match(line)
    return m.group(1) if m else str(i + 1)


def term_warnings(terms: list[str], scope: list[str], names: list[str] = (), request: str = '') -> list[dict[str, Any]]:
    request_ = (request or '')
    outcome = []
    for term in terms:
        pattern, places = _term_pattern(term), []
        for no, text in enumerate(scope):
            entry_name = names[no] + ':' if no < len(names) and names[no] else ''
            places += _lines(text, pattern, lambda i, s, entry_name=entry_name: entry_name + _line_no(i, s), TERM_LINE_CAP - len(places))
            if len(places) >= TERM_LINE_CAP:
                break
        outcome.append({'term': term, 'request_term': bool(pattern.search(request_)), 'scope_lines': places})
    return outcome


def code_lines(text: str, selected: dict[str, str], starts: dict[str, int | None]) -> dict[str, list[str]]:
    outcome = {}
    for term in claim_terms(text):
        if not _code_similar(term):
            continue
        pattern, lines = re.compile(r'(?<![A-Za-z0-9_])' + re.escape(term) + r'(?![A-Za-z0-9_])'), []
        for id, body in selected.items():
            start = starts.get(id)
            lines += _lines(body, pattern, lambda i, s, k=id, b=start: f'{k}:{b + i}' if b else k,
                                  CODE_LINE_CAP - len(lines))
            if len(lines) >= CODE_LINE_CAP:
                break
        if lines:
            outcome[term] = lines
    return outcome


def claim_package(wt, elements: list[dict[str, Any]], evidence: list[dict[str, Any]], scope: list[str],
                 section_scope: list[str] = (), *, scope_names: list[str] = (), section_names: list[str] = (),
                 request: str = '') -> list[dict[str, Any]]:
    names = [*(list(scope_names) + [''] * len(scope))[:len(scope)],
             *(list(section_names) + ['card'] * len(section_scope))[:len(section_scope)]]
    raw = {k['id']: evidence_text(wt, k, REVIEW_EVIDENCE_TEXT_CAP + 1)
           for k in evidence if isinstance(k, dict) and isinstance(k.get('id'), str)}
    trimmed = {k for k, v in raw.items() if len(v) > REVIEW_EVIDENCE_TEXT_CAP}
    texts = {k: v[:REVIEW_EVIDENCE_TEXT_CAP] for k, v in raw.items()}
    starts = {}
    for k in evidence:
        if isinstance(k, dict) and isinstance(k.get('id'), str):
            m = re.search(r':(\d+)(?:-\d+)?$', str(k.get('source') or '').strip())
            starts[k['id']] = int(m.group(1)) if k.get('round') == 'file' and m else None
    package = []
    for element in elements:
        if element['evidence_ids']:
            selected = {k: texts.get(k, '') for k in element['evidence_ids']}
            marked = [] if trimmed.intersection(selected) else evidence_non_terms(
                element['text'], list(selected.values()), scope, list(section_scope))
            package.append({'id': element['id'], 'text': element['text'], 'evidence_ids': element['evidence_ids'], 'evidence_texts': selected,
                          'trimmed_evidence': sorted(trimmed.intersection(selected)),
                          'evidence_non_terms': marked,
                          'term_warnings': term_warnings(marked, [*scope, *section_scope], names, request),
                          'code_lines': code_lines(element['text'], selected, starts)})
    return package


def concrete_corrections(raw: str, digest: str | None, claims: list[dict[str, Any]], requirements: list[dict[str, str]],
                      criteria: list[str]) -> list[dict[str, Any]]:
    from outcome import _json_candidates, same_revision, placeholder
    bodies = [a for a in _json_candidates(raw or '') if isinstance(a, dict) and same_revision(a.get('revision'), digest or '')]
    if len({json.dumps(g, sort_keys=True) for g in bodies}) != 1 or bodies[0].get('verdict') != 'REVISE':
        return []
    body = bodies[0]

    def rationale(text: Any) -> str | None:
        return text.strip()[:1000] if isinstance(text, str) and text.strip() and not placeholder(text) else None

    def single(items_list: Any) -> dict[str, dict[str, Any]]:
        counter: dict[str, list[dict[str, Any]]] = {}
        for k in items_list if isinstance(items_list, list) else []:
            if isinstance(k, dict) and isinstance(k.get('id'), str):
                counter.setdefault(k['id'], []).append(k)
        return {id: k[0] for id, k in counter.items() if len(k) == 1}

    findings = []
    marked = {i['id']: {t.lower() for t in i['evidence_non_terms']} for i in claims}
    for id, k in single(body.get('claims')).items():
        reason = rationale(k.get('rationale'))
        if id not in marked or reason is None:
            continue
        terms = sorted({t['term'] for t in k.get('terms') or [] if isinstance(t, dict) and t.get('status') == 'unsupported'
                           and isinstance(t.get('term'), str) and t['term'].lower() in marked[id]})
        if k.get('support') in ('partial', 'absent') or terms:
            findings.append({'round': 'claim', 'id': id, 'support': k.get('support'), 'terms': terms, 'rationale': reason})
    known = {g['id'] for g in requirements if g.get('round') != 'mechanical'}
    for id, k in single(body.get('requirements')).items():
        reason = rationale(k.get('rationale'))
        if id in known and k.get('status') == 'missing' and reason:
            findings.append({'round': 'requirement', 'id': id, 'rationale': reason})
    for id, k in single(body.get('acceptance')).items():
        reason = rationale(k.get('evidence'))
        if id in criteria and k.get('status') == 'FAIL' and reason:
            findings.append({'round': 'criterion', 'id': id, 'rationale': reason})
    return findings[:50]


def _semantic_body(raw: str, digest: str | None) -> dict[str, Any]:
    from outcome import _json_candidates, same_revision
    found = [a for a in _json_candidates(raw or '') if isinstance(a, dict) and same_revision(a.get('revision'), digest or '')
               and ('claims' in a or 'requirements' in a)]
    if not found:
        raise ValueError('claims and requirements decisions are absent')
    if len({json.dumps([b.get('claims'), b.get('requirements')], sort_keys=True) for b in found}) > 1:
        raise ValueError('conflicting semantic decisions')
    return found[-1]


def _rationale(decision: dict[str, Any], id: str) -> str:
    from outcome import placeholder
    if not isinstance(decision.get('rationale'), str) or placeholder(decision['rationale']):
        raise ValueError(f'{id}: rationale absent')
    return decision['rationale'].strip()


def _claim_decisions(given: Any, claims: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    expected = {i['id']: i for i in claims}
    if not isinstance(given, list) or any(not isinstance(k, dict) for k in given):
        raise ValueError('claims list is required')
    decisions: dict[str, dict[str, Any]] = {}
    for decision in given:
        id = decision.get('id')
        if id not in expected or id in decisions:
            raise ValueError(f'unknown or duplicate claim: {id!r}')
        if decision.get('support') not in CLAIM_SUPPORT:
            raise ValueError(f"{id}: support {'|'.join(CLAIM_SUPPORT)} is required")
        marked = {t.lower(): t for t in expected[id]['evidence_non_terms']}
        terms = decision.get('terms', [])
        if (not isinstance(terms, list) or any(not isinstance(t, dict) or not isinstance(t.get('term'), str)
                                                  or t.get('status') not in TERM_STATUS for t in terms)):
            raise ValueError(f"{id}: term status {'|'.join(TERM_STATUS)} is required")
        given_terms = [t['term'].lower() for t in terms]
        if len(set(given_terms)) != len(given_terms):
            raise ValueError(f'{id}: the same term cannot receive multiple decisions')
        decisions[id] = {'id': id, 'support': decision['support'], 'rationale': _rationale(decision, id),
                            'terms': [{'term': marked.get(t['term'].lower(), t['term']), 'status': t['status']}
                                         for t in terms if t['term'].lower() in marked or t['status'] == 'unsupported']}
    if set(decisions) != set(expected):
        raise ValueError(f'claim decision missing: {", ".join(sorted(set(expected) - set(decisions)))}')
    return decisions


def _requirement_decisions(given: Any, requirements: list[dict[str, str]], elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expected = {g['id']: g for g in requirements}
    element_ids = {o['id'] for o in elements}
    if not isinstance(given, list) or any(not isinstance(k, dict) for k in given):
        raise ValueError('requirements must be a list')
    decisions: dict[str, dict[str, Any]] = {}
    for decision in given:
        id = decision.get('id')
        if not isinstance(id, str) or id in decisions:
            raise ValueError(f'duplicate or invalid requirement: {id!r}')
        if id in expected and expected[id].get('round') == 'mechanical':
            continue
        if id in expected:
            definition = expected[id]
        elif EXTRA_REQUIREMENT.fullmatch(id) and isinstance(decision.get('text'), str) and decision['text'].strip():
            definition = {'id': id, 'criterion': 'request', 'round': 'content', 'text': decision['text'].strip()}
        else:
            raise ValueError(f'unknown requirement: {id!r}')
        status, report_ = decision.get('status'), decision.get('report', [])
        if status not in REQUIREMENT_STATUS:
            raise ValueError(f"{id}: status {'|'.join(REQUIREMENT_STATUS)} is required")
        if status == 'report_outside' and definition.get('round') != 'process':
            raise ValueError(f'{id}: only requirements classified as process by the engine may be outside the report')
        if (not isinstance(report_, list) or len(set(map(str, report_))) != len(report_)
                or any(o not in element_ids for o in report_) or (status == 'satisfied' and not report_)):
            raise ValueError(f'{id}: satisfaction must be demonstrated by delivered report element ids')
        decisions[id] = {**definition, 'status': status, 'report': list(report_), 'rationale': _rationale(decision, id)}
    mandatory = {k for k, g in expected.items() if g.get('round') != 'mechanical'}
    if not mandatory <= set(decisions):
        raise ValueError(f'requirement decision missing: {", ".join(sorted(mandatory - set(decisions)))}')
    return list(decisions.values())


def semantic_decision_apply(raw: str, review: dict[str, Any], entries: list[dict[str, Any]], claims: list[dict[str, Any]],
                          requirements: list[dict[str, str]], evidence: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if review.get('status') not in ('PASS', 'FAIL'):
        return review
    try:
        body = _semantic_body(raw, (review.get('revision') or {}).get('digest'))
        decisions = _claim_decisions(body.get('claims'), claims)
        scope = _requirement_decisions(body.get('requirements'), requirements, entries)
    except ValueError as exc:
        return {'status': 'ERROR', 'revision': review.get('revision'), 'reason': f'invalid semantic decision: {exc}'}
    scope += [mechanical_decision(g, evidence or []) for g in requirements if g.get('round') == 'mechanical']
    unsupported = {}
    for decision in decisions.values():
        terms = [t['term'] for t in decision['terms'] if t['status'] == 'unsupported']
        if decision['support'] != 'full' or terms:
            unsupported[decision['id']] = (f"{decision['id']} support={decision['support']}"
                                      + (f" term absent from evidence: {', '.join(terms)}" if terms else '') + f": {decision['rationale']}")
    missing = []
    for decision in scope:
        if decision['status'] == 'missing':
            missing.append(f"{decision['id']} missing ({decision['text']}): {decision['rationale']}")
        basis = [o for o in decision['report'] if o in unsupported]
        if decision['status'] == 'satisfied' and basis:
            missing.append(f"{decision['id']} relies on unsupported elements: {', '.join(basis)} ({decision['text']})")
    items = [
        {'id': 'claim_support', 'status': 'FAIL' if unsupported else 'PASS',
         'evidence': '; '.join(unsupported.values())[:6000] or 'Every evidenced sentence is separately supported by its cited range'},
        {'id': 'request_scope', 'status': 'FAIL' if missing else 'PASS',
         'evidence': '; '.join(missing)[:6000] or 'Every requirement is satisfied by delivered report elements'}]
    outcome = {**review, 'claims': list(decisions.values()), 'requirements': scope,
             'acceptance': [*(review.get('acceptance') or []), *items]}
    if unsupported or missing:
        outcome.update(status='FAIL', verdict='REVISE')
    return outcome


def plan_audit(plan: dict[str, Any], declaration_: dict[str, Any]) -> str | None:
    """G-R1: in a report job every task's files are a subset of report + evidence appendix and one task writes the report."""
    allowed = (declaration_["path"], declaration_["evidence"])
    tasks = plan.get("tasks") or []
    for g in tasks:
        outside = [f for f in g.get("files") or [] if f not in allowed]
        if outside:
            return (f"report mode (G-R1): task {g.get('id')} lists files outside the report ({', '.join(outside[:5])}); "
                    f"files may contain only {allowed[0]} and {allowed[1]}")
    if not any(allowed[0] in (g.get("files") or []) for g in tasks):
        return f"report mode (G-R1): no task writes the report; one task’s files must include {allowed[0]}"
    return None


def is_writer(plan: dict[str, Any], gid: str, declaration_: dict[str, Any]) -> bool:
    """G-R6: the lane that writes the report (interpretation and results) goes to a strong model; a data-gathering lane that only writes the evidence appendix does not."""
    return any(g.get("id") == gid and declaration_["path"] in (g.get("files") or []) for g in plan.get("tasks") or [])


def lane_rules(declaration_: dict[str, Any]) -> str:
    return (f"REPORT MODE (G-R1..G-R4): write only {declaration_['path']} and {declaration_['evidence']}; do not change code, tests or config. "
            "Read the `report-evidence` skill first (appendix format; checked mechanically by the gate).\n"
            + evidence_rules(f"Evidence appendix {declaration_['evidence']} is JSONL: each line"))


def evidence_rules(target: str) -> str:
    return (f'- {target} {{"id": "K1", "claim": "...", "round": "file|command|query|url|bug", '
            '"source": "path:line | command | url", "expected": "..."}. For file evidence, expected is a quote on that line; '
            'for command/query evidence, expected is text in the output; for bug evidence, source is a reproduction command '
            'that fails against the existing code, and expected is the error text (such as AssertionError). '
            '`round` identifies the EVIDENCE kind; write the finding class (bug, risk, design choice, test gap) in the report.\n'
            '- Every report sentence containing a number or finding, and every table row, carries evidence ids: '
            '[K1], [K1, K2] or [K1..K4]. When multiple lanes share an appendix, prefix ids by lane (K-tz1, K-yd1). '
            'Mark numbers you cannot establish as unverified; do not invent them. Evidence commands rerun in a read-only '
            'sandbox without network access.\n')


def plan_prompt(declaration_: dict[str, Any]) -> str:
    return (f"REPORT MODE (G-R1/G-R6): this is a REPORT/ANALYSIS task; source code remains unchanged. Each task’s files list may contain ONLY "
            f"{declaration_['path']} (report) and {declaration_['evidence']} (appendix); any other file invalidates the plan. "
            f"Data collection tasks (measurement, queries, file scans) append lines only to {declaration_['evidence']}. "
            f"Interpretation and conclusions belong to ONE final task with files [{declaration_['path']}, {declaration_['evidence']}], "
            "depending on the data tasks; the coordinator assigns it to a strong model. "
            + lane_rules(declaration_).split("\n", 1)[1])


def reconciliation_prompt(declaration_: dict[str, Any]) -> str:
    """G-R7: a third model reconciles two independent drafts; a diverging claim does not enter the final report without verification from its source."""
    report_name, appendix_name = pathlib.PurePosixPath(declaration_["path"]).name, pathlib.PurePosixPath(declaration_["evidence"]).name
    return (f"RECONCILIATION (G-R7): two independent analysts examined the same question separately. "
            f"A: .cadre-plan/analyst-a/{report_name} and .cadre-plan/analyst-a/{appendix_name}; "
            f"B: .cadre-plan/analyst-b/{report_name} and .cadre-plan/analyst-b/{appendix_name}. "
            f"Write the final report to {declaration_['path']} and the appendix to {declaration_['evidence']}. "
            "Include shared claims with their evidence. Independently verify each diverging claim FROM ITS SOURCE; "
            "include verified claims, and preserve unverified claims with both views and an unverified label in Divergences. "
            "Renumber evidence from K1; do not write under .cadre-plan.\n" + lane_rules(declaration_))


ANALYST_B = ("INDEPENDENT SECOND ANALYST (G-R7): another analyst is examining the same question separately; you will not see their draft. "
             "Write from scratch using your own measurements and reading. Do not guess; evidence every claim.")


def main(argv: list[str]) -> int:
    command = argv[0] if argv else ""
    b = record_declaration()
    if command == "mode":
        print("report" if b else "")
        return 0 if b else 1
    if command == "plan-prompt":
        print(plan_prompt(b) if b else "")
        return 0
    if command in ("paths", "reconciliation-prompt", "analyst-prompt") and b:
        print({"paths": f"{b['path']} {b['evidence']}", "reconciliation-prompt": reconciliation_prompt(b) if command == "reconciliation-prompt" else "",
               "analyst-prompt": ANALYST_B}[command])
        return 0
    if command == "analyst-record" and len(argv) >= 4:
        try:
            analyst_record(pathlib.Path(os.environ["CADRE_RUN_DIR"]), pathlib.Path(argv[2]), argv[1], argv[3:6], argv[6] if len(argv) > 6 else "")
        except (KeyError, OSError, ValueError) as exc:
            print(f"could not write analyst record: {exc}", file=sys.stderr)
            return 1
        return 0
    if command == "writer" and len(argv) > 1:
        try:
            plan = json.loads((pathlib.Path(os.environ["CADRE_RUN_DIR"]) / "plan.json").read_text())
        except (KeyError, OSError, ValueError):
            return 1
        return 0 if b and is_writer(plan, argv[1], b) else 1
    if command == "recover" and len(argv) > 1 and b:
        # B238: the writer chain is exhausted but the product may be on disk (the writer looped in the last round and was cut); if it passes the gate it produced.
        from run_store import RunStore
        from qe_core import PRODUCT_EXCLUDED
        try:
            record = RunStore().read(os.environ["CADRE_RUN_ID"])
            temporary = pathlib.Path(os.environ["CADRE_RUN_DIR"]) / "tasks" / argv[1] / "recovery"
            r = report_qa(record["worktree"], b, record.get("source_sha"), temporary, seed="recovery", excluded=PRODUCT_EXCLUDED)
        except (KeyError, OSError, ValueError) as exc:
            print(f"recovery could not be attempted: {exc}", file=sys.stderr)
            return 1
        print(f"recovery report QA: {r['status']} ({len(r['findings'])} findings)")
        return 0 if r["status"] == "PASS" else 1
    print("usage: report_mode.py mode | plan-prompt | writer <task> | paths | reconciliation-prompt | analyst-prompt | analyst-record <mode> <wt> <m1> [m2] [m3] [reason]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
