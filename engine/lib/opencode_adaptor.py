"""OpenCode transport boundary: command, session, cancellation and a read-only receipt store."""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import time
from typing import Any, Iterator


def connect(path: Path | str, *, timeout: float = 15) -> sqlite3.Connection:
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=timeout)


def command(real: str, directory: str, args: list[str], *, json_events: bool = False) -> list[str]:
    return [real, 'run', '--dir', str(directory), *(['--format', 'json'] if json_events else []), *args]


def session_options(options: list[str], *, session_id: str = '', title: str = '') -> list[str]:
    if any(arg in ('-c', '--continue', '-s', '--session', '--title') for arg in options):
        raise ValueError('session selection belongs to the run record')
    return [*(['--session', session_id] if session_id else ['--title', title]), *options]


def launch(command: list[str], **kwargs: Any) -> subprocess.Popen:
    return subprocess.Popen(command, **kwargs)


def capture(real: str, directory: str, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(command(real, directory, args), cwd=directory, **kwargs)


def cancel(pid: int, grace: float, *, children, alive) -> bool:
    tree = children(pid) + [pid]
    for target in tree:
        try:
            os.kill(target, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.time() + grace
    while time.time() < deadline:
        pending = [target for target in tree if alive(target)]
        if not pending:
            return True
        time.sleep(.2)
    pending = [target for target in tree if alive(target)]
    for target in pending:
        try:
            os.kill(target, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return not pending


def session_by_title(db: Path, title: str) -> set[str]:
    with closing(connect(db)) as con:
        return {row[0] for row in con.execute('select id from session where title=?', (title,))}


def events(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("rb") as stream:
        for line in stream:
            if not line.endswith(b"\n"):
                break  # An interrupted last event is not evidence.
            try:
                value = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(value, dict):
                yield value


def session_rows(con: sqlite3.Connection, ids: set[str]) -> list[sqlite3.Row]:
    """Only explicit roots and their descendants; never a directory/time guess."""
    seen: set[str] = set()
    rows = []
    frontier = set(ids)
    while frontier:
        current = sorted(frontier - seen)
        if not current:
            break
        seen.update(current)
        for start in range(0, len(current), 400):
            chunk = current[start:start + 400]
            query = ",".join("?" for _ in chunk)
            rows.extend(con.execute(f"select * from session where id in ({query})", chunk).fetchall())
        frontier = set()
        for start in range(0, len(current), 400):
            chunk = current[start:start + 400]
            query = ",".join("?" for _ in chunk)
            frontier.update(r[0] for r in con.execute(f"select id from session where parent_id in ({query})", chunk))
    return sorted(rows, key=lambda r: r["time_created"])


def spending_tokens(db: Path, provider: str, since: float) -> int | None:
    """Total tokens spent on this provider today (input + output + reasoning).
    B106: the `tpd` daily token cap is also a decision input (groq free: 200K TPD ~ 25 full-size calls)."""
    try:
        with closing(connect(db)) as con:
            rows = con.execute("""select coalesce(json_extract(p.data,'$.info.tokens.input'),0)
                + coalesce(json_extract(p.data,'$.info.tokens.output'),0)
                + coalesce(json_extract(p.data,'$.info.tokens.reasoning'),0) from part p
                join message m on m.id=p.message_id
                where p.time_created >= ? and json_extract(p.data,'$.type')='step-finish'
                and json_extract(m.data,'$.providerID')=?""", (since * 1000, provider)).fetchall()
        return int(sum(int(r[0] or 0) for r in rows))
    except (OSError, sqlite3.Error):
        return None


def spending_model(db: Path, provider: str, since: float) -> dict[str, int] | None:
    """Request count per model (for the daily MODEL bucket). Same source and same window as `spending`."""
    try:
        with closing(connect(db)) as con:
            rows = con.execute("""select json_extract(m.data,'$.modelID'), count(*) from part p
                join message m on m.id=p.message_id
                where p.time_created >= ? and json_extract(p.data,'$.type')='step-finish'
                and json_extract(m.data,'$.providerID')=? group by 1""", (since * 1000, provider)).fetchall()
        return {str(ad): int(n) for ad, n in rows if ad}
    except (OSError, sqlite3.Error):
        return None


def spending(db: Path, provider: str, since: float) -> tuple[float, int] | None:
    """Every completed step belongs to its own message's provider and its own time (a session may start yesterday and switch model today).
    Session totals cannot answer this; pending calls are covered by reservations."""
    from butce_kapisi import adim_maliyeti, BudgetDenied
    try:
        with closing(connect(db)) as con:
            rows = con.execute('''select json_extract(p.data,'$.cost'), json_extract(m.data,'$.modelID'),
                json_extract(p.data,'$.tokens') from part p
                join message m on m.id=p.message_id
                where p.time_created >= ? and json_extract(p.data,'$.type')='step-finish'
                and json_extract(m.data,'$.providerID')=?''', (since * 1000, provider)).fetchall()
        return sum(adim_maliyeti(provider, model, c, tokens) for c, model, tokens in rows), len(rows)
    except (OSError, sqlite3.Error, BudgetDenied):
        return None


