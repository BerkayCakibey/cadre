"""Panel data model and the redacted, replayable event trace of a run."""
from __future__ import annotations

import json
import hashlib
import math
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import ui_messages
from ui_messages import t

TERMINAL = ('completed', 'failed', 'cancelled', 'implemented')
HELPER = ('plan', 'qa', 'quality', 'merge', 'main', 'helper', 'project-ref-list')
STATUS_NAME = {'accepted': 'Starting', 'queued': 'Queued', 'running': 'Running', 'retryable': 'Waiting', 'completed': 'Done',
             'failed': 'Failed', 'cancelled': 'Cancelled', 'implemented': 'Applied'}
TASK_MARK = {'produced': 'finished', 'complete': 'finished', 'running': 'now_ts', 'half': 'error', 'failed': 'error', 'blocked': 'error'}
TASK_PROSE = {'produced': 'Done', 'complete': 'Done', 'running': 'Running', 'half': 'Left unfinished', 'failed': 'Failed',
              'blocked': 'Blocked', 'waiting': 'Waiting'}
PART_MARK = {'passed': 'finished', 'applied': 'finished', 'stuck': 'error', 'waiting': 'waiting'}
PART_PROSE = {'passed': 'Passed', 'applied': 'Applied', 'stuck': 'Stuck', 'waiting': 'Queued'}
MARK_SUMMARY = {'finished': 'Complete', 'now_ts': 'Running', 'waiting': 'Queued', 'error': 'Problem'}
LEVEL_NAME = {'simple': 'Simple', 'middle': 'Medium', 'hard': 'Hard', 'very-hard': 'Very hard', 'ultra': 'Ultra'}
ROLE_INFO = {
    'plan': ('Planner', 'Makes the plan at the start of each task. For a large task it also defines the parts.'),
    'impl': ('Developer', 'Writes each subtask. If the test is red, the fix round uses this order too.'),
    'quality': ('Judge', 'Reviews independently once the test passes. Cannot be from the writer model family.'),
    'impl-light': ('Developer', 'Writes small subtasks with difficulty 1 or 2.'),
    'merge': ('Developer', 'Merges the outputs when more than three subtasks were written in parallel.'),
    'recovery': ('Developer', 'Runs when a stuck task is restarted from a clean state.'),
}
PROVIDER_NAME = {'opencode-go': 'Go (subscription)', 'openrouter': 'OpenRouter (balance)', 'opencode': 'Zen (balance)',
                 'claude': 'Claude (subscription)', 'groq': 'Groq (free)'}
TASK_CHILDREN = {'plan': 'Planner subtasks, listed from the start', 'test': 'Once after all subtasks are done',
                   'judge': 'Independent review once the test passes', 'last-check': 'All tests and the judge review, once',
                   'delivery': 'Branch, changed files, push and merge', 'report': 'Report file, read with o', 'outcome': 'Recorded acceptance result, test and judge',
                   'agents': 'Each agent with thoughts and tool calls', 'spend': 'By subtask, model and provider',
                   'events': 'Key events, full log with o'}
EVENT = re.compile(r'plan acceptance|task .* (FAILED|RECOVERED|produced)|QA GATE|JUDGE|ACCEPTANCE RESULT|BUDGET WAIT|FINISHED|spend alarm|'
                  r'PARALLEL|SCOPE|project_ref|takeover|cancel', re.I | re.ASCII)
GRAPH = '▁▂▃▄▅▆▇█'
CACHE: dict[str, tuple[float, Any]] = {}
FINISHED: dict[tuple, dict] = {}
RECORD: dict[str, tuple[int, dict]] = {}
TRACE: dict[str, tuple[float, tuple]] = {}
AT_END: dict[str, Callable[..., Any]] = {}


def d(entry_name: str, summary: str = '', detail: list | None = None, child: list | None = None, status: str = '', context: list | None = None,
      **extra: Any) -> dict:
    node = {'entry_name': entry_name, 'summary': summary, 'detail': detail or [], 'child': child or [], 'status': status, 'context': context or []}
    node.update({a: v for a, v in extra.items() if v is not None})
    return node


def description(title: str, text: str, inside: list[tuple[str, str]] | None = None, extra: list | None = None) -> list:
    blocks = [('title', title), ('line', text)]
    if inside:
        blocks += [('section', t('Inside'))] + [('field', (a, b)) for a, b in inside]
    return blocks + (extra or [])


def clean() -> None:
    CACHE.clear()
    FINISHED.clear()
    TRACE.clear()


def number(x: Any) -> float:
    try:
        value = float(x or 0)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def money(x: Any) -> str:
    return f'${number(x):.2f}'


def ratio(x: Any) -> str:
    return f'{number(x):.0f}%'


def token_prose(n: Any) -> str:
    value = number(n)
    if not value:
        return '—'
    text = (f'{value / 1e6:.2f}'.rstrip('0').rstrip('.') + 'M') if value >= 1e6 else f'{round(value / 1000)}K'
    return t('{n} tokens', n=text)


def list_prose(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return ', '.join(map(str, value)) or '—'
    return str(value or '') or '—'


def duration_text(sec: float) -> str:
    minute = max(0, int(sec)) // 60
    return t('{s} h {d:02d} min', s=minute // 60, d=minute % 60) if minute >= 60 else t('{d} min', d=minute)


def moment_prose(format: str, ts: Any) -> str:
    try:
        return datetime.fromtimestamp(number(ts), timezone.utc).astimezone().strftime(format) if number(ts) else '—'
    except (OverflowError, OSError, ValueError):
        return '—'


def hour(ts: Any) -> str:
    return moment_prose('%H:%M', ts)


def date(ts: Any) -> str:
    return moment_prose('%d.%m %H:%M', ts)


def relative_age(ts: Any) -> str:
    value = number(ts)
    if isinstance(ts, bool) or value <= 0 or value > time.time():
        return t('Time unknown')
    sec = time.time() - value
    if sec < 60:
        return t('Just now')
    if sec < 3600:
        return t('{n} min ago', n=int(sec // 60))
    if sec < 86400:
        return t('{n} h ago', n=int(sec // 3600))
    return t('{n} days ago', n=int(sec // 86400))


def absolute_moment(ts: Any) -> str:
    text = moment_prose('%Y-%m-%d %H:%M:%S %z', ts) if not isinstance(ts, bool) and number(ts) > 0 else '—'
    return text if text != '—' else t('Time unknown')


def id_summary(k: dict) -> list:
    """CR-36: the run's recorded engine/config/environment identity and the operator intervention count; nothing invented for old records."""
    from run_store import intervention_summary
    id = dictionary(k.get('provenance'))
    if not id:
        lines = [('field', (t('Source identity'), t('Not recorded (older run)')))]
    else:
        runner, environment = dictionary(id.get('engine')), dictionary(id.get('runtime'))
        sha = str(runner.get('git_head') or '')[:12] or t('Unknown')
        dirty = {True: t('Dirty diff'), False: t('Clean')}.get(runner.get('git_dirty'), t('Unknown'))
        lines = [('field', (t('Engine source'), f"{sha} · {dirty} · " + str(runner.get('guard_version') or t('Unknown'))[:12])),
                    ('field', (t('Executor version'), str(environment.get('opencode_version') or t('Unknown')))),
                    ('field', (t('Environment'), f"{environment.get('platform') or t('Unknown')} · python {environment.get('python') or '?'}")),
                    ('field', (t('Identity recorded'), absolute_moment(id.get('recorded'))))]
    summary = intervention_summary(k)
    if summary['count'] is None:
        lines.append(('field', (t('Operator interventions'), t('Not recorded (older run)'))))
    else:
        lines.append(('field', (t('Operator interventions'), t('{n} recorded · minutes not measured', n=summary['count'])
                                  + (f" · {', '.join(summary['kinds'])}" if summary['kinds'] else ''))))
    return lines


def test_project(k: dict) -> bool:
    return k.get('test_run') is True or bool(re.fullmatch(r'live-c\d{2}-\d{8}-\d{6}(?:--p[\w-]+)?', str(k.get('project') or '')))


def dictionary(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def event_hour(value: Any) -> str:
    if isinstance(value, (int, float)) and value:
        return hour(value / 1000 if value > 1e11 else value)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone().strftime('%H:%M')
    except (OverflowError, OSError, ValueError):
        return '--:--'


def iso_local(value: Any) -> str:
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone().strftime('%d.%m %H:%M')
    except (OverflowError, OSError, ValueError):
        return '—'


def graph(values: list[float]) -> str:
    if not values:
        return '—'
    base, cap = min(values), max(values)
    interval = (cap - base) or 1
    return ''.join(GRAPH[max(0, min(len(GRAPH) - 1, round((len(GRAPH) - 1) * (v - base) / interval)))] for v in values)


def trend_prose(values: list[float]) -> str:
    return f'{graph(values)}   {ratio(values[0])} → {ratio(values[-1])}' if values else '—'


def home_dir() -> Path:
    return ui_messages.home_dir()


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record_read(path: Path) -> dict:
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return {}
    previous = RECORD.get(str(path))
    if previous and previous[0] == stamp:
        return previous[1]
    value = _read(path)
    RECORD[str(path)] = (stamp, value)
    return value


def error_prose(exc: BaseException) -> str:
    return f'{type(exc).__name__}: {str(exc)[:120]}' if str(exc) else type(exc).__name__


def request_hour(k: dict) -> dict | None:
    if not isinstance(k.get('request'), dict):
        return None
    try:
        from run_store import request_hour_summary
        return request_hour_summary(k)
    except (KeyError, TypeError, ValueError):
        return None


def scope_text(files, tests) -> str:
    """CR-35: the narrow acceptance scope bound to the request/approval; empty means acceptance of the whole package."""
    if isinstance(files, (list, tuple)):
        files = ', '.join(files)
    if isinstance(tests, (list, tuple)):
        tests = ', '.join(tests)
    if not files:
        return t('None (full-suite acceptance)')
    return t('Permitted files {f} · required tests {g}', f=files, g=tests or '—')


def request_approval_text(rid: str, proposal: dict, command: str | None = None) -> str:
    action = proposal['action']
    text = (rid + '\n' + action['output'] + '\n' + action['target_path'] + '\n' +
             ', '.join(action['providers']) + '\n' + str(action['duration_minutes']) + ' min · ' + action['budget_usd'] + ' USD\n' +
             '\n'.join(role + ': ' + ', '.join(models) for role, models in action['models'].items() if models) + '\n' +
             t('This permission excludes commit, push, merge, installation and publication.'))
    qa = action.get('qa_command')
    if qa:
        text += '\n' + t('QA command') + ': ' + (qa['command'] or t('Derived from the source at start'))
    scope = action.get('acceptance_scope')
    if scope:
        text += '\n' + t('Narrow acceptance scope') + ': ' + scope_text(scope.get('files'), scope.get('tests'))
    if command is None:
        return text
    return (text + '\n\n' + t('Approval token') + ': ' + proposal['token'] + '\n\n' +
            t('Exact command (CLI equivalent, for the record only):') + '\n' + command)


def _cached(key: str, duration: float, produce: Callable[[], Any], id: str) -> Any:
    moment, value = CACHE.get(key, (0.0, None))
    if value is None or time.time() - moment > duration:
        try:
            value = produce()
        except Exception as exc:
            entry_name = key.split(':')[0]
            value = d(t(entry_name), t('Unreadable'), [('title', t(entry_name)), ('muted', t('Unreadable: {e}', e=error_prose(exc)))], id=id,
                      description=description(t(entry_name), t('This section could not be read right now. It is retried within a minute.')))
        section = key.split(':')[0] + ':'
        for old in [a for a in list(CACHE) if a.startswith(section) and a != key]:
            del CACHE[old]
        CACHE[key] = (time.time(), value)
    return value


def run_directory(home: Path, rid: Any) -> Path:
    return home / 'runs' / str(rid)


def project_ref_name(k: dict) -> str:
    return str(k.get('project') or '?').split('--p')[0]


def first_line(home: Path, k: dict) -> str:
    try:
        text = Path(k.get('prompt') or run_directory(home, k['run_id']) / 'prompt.md').read_text(errors='replace')
    except OSError:
        return t('(request unreadable)')
    return next((s.strip().lstrip('#').strip() for s in text.splitlines() if s.strip()), t('(empty request)'))


def last_lines(path: str | Path, n: int) -> list[str]:
    try:
        with Path(path).open('rb') as f:
            f.seek(0, 2)
            offset = max(0, f.tell() - 65536)
            f.seek(offset)
            if offset:
                f.readline()
            text = f.read().decode('utf-8', 'replace')
    except OSError:
        return []
    return [s.rstrip()[:160] for s in text_redacted(text).splitlines() if s.strip()][-n:]


def event_records(home: Path, rid: str, filter: str = '') -> list[tuple[float, str, str]]:
    selected = []
    for s in dict.fromkeys(last_lines(run_directory(home, rid) / 'dispatch.log', 500)):
        m = re.match(r'^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})?)\s+(.+)$', s)
        if not m or not (filter in m[2] if filter else EVENT.search(m[2])):
            continue
        try:
            ts = datetime.fromisoformat(m[1].replace('Z', '+00:00')).timestamp()
        except (ValueError, OverflowError, OSError):
            continue
        selected.append((ts, m[1], m[2]))
    return sorted(selected)


def events(home: Path, rid: str, n: int = 6, filter: str = '', absolute: bool = False) -> list[str]:
    if n <= 0:
        return []
    selected = event_records(home, rid, filter)[-n:]
    if absolute:
        return [f"{stamp}{' (' + t('Timezone unrecorded') + ')' if len(stamp) == 19 else ''} {text}" for _, stamp, text in selected]
    return [f'{stamp[11:16]} {text[:110]}' for _, stamp, text in selected]


def last_events(home: Path, records: list[dict], n: int = 6) -> list[str]:
    if n <= 0:
        return []
    lines = []
    for k in records:
        lines += [(ts, str(k['run_id']), stamp, text, project_ref_name(k)) for ts, stamp, text in event_records(home, str(k['run_id']))[-n:]]
    return [f'{stamp[11:16]} {project_ref}: {text[:80]}' for _, _, stamp, text, project_ref in sorted(lines, reverse=True)[:min(n, 6)]]


def runs(home: Path, n: int | None = None) -> list[dict]:
    paths = sorted((home / 'runs').glob('*/run.json'), key=lambda p: p.parent.name)
    return [k for k in (record_read(p) for p in (paths[-n:] if n else paths)) if k.get('run_id')]


def _approval(k: dict) -> bool:
    from outcome import approved
    return bool(approved(k))


def _budget(home: Path) -> tuple[dict, float]:
    import screen_render
    return screen_render.budget_view(home, refresh=False)


def _chain(role: str, b: dict, status: dict) -> tuple[list, list]:
    import model_router
    return model_router.chain(role, b=b, d=status, save=False)


def _failure() -> dict:
    import model_router
    return model_router.status_read()


def _claude_closed() -> bool:
    import model_router
    return bool(model_router.claude_closed())


FALLBACK_AT_END = {'approval': _approval, 'budget': _budget, 'chain': _chain, 'failure': _failure, 'claude_closed': _claude_closed}


def at_end(entry_name: str) -> Callable[..., Any]:
    return AT_END.get(entry_name) or FALLBACK_AT_END[entry_name]


def claude_closed() -> bool:
    try:
        return bool(at_end('claude_closed')())
    except Exception:
        return False


def delivery_candidate(k: dict) -> bool:
    from permission import delivery_candidate
    try:
        return delivery_candidate(k, at_end('approval'))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def product_trace(k: dict) -> tuple:
    if not delivery_candidate(k):
        return ()
    wt = str(k['worktree'])
    moment, trace = TRACE.get(wt, (0.0, ()))
    if trace and time.time() - moment < 15:
        return trace
    head = git_output(wt, '--no-optional-locks', 'rev-parse', 'HEAD').strip()
    status = git_output(wt, '--no-optional-locks', 'status', '--porcelain=v1', '-z', '--untracked-files=all')
    stamps = []
    for record in status.split('\0'):
        path = Path(wt) / record[3:] if len(record) > 3 else None
        try:
            st = path.stat() if path else None
        except OSError:
            st = None
        stamps.append((record, st.st_mtime_ns if st else 0, st.st_size if st else -1))
    trace = (head, tuple(stamps))
    TRACE[wt] = (time.time(), trace)
    return trace


def delivery_doable(k: dict) -> bool:
    return delivery_candidate(k)


def text_redacted(text: str) -> str:
    from run_ledger import redacted
    if text.lstrip().startswith(('{', '[')):
        try:
            return json.dumps(data_redacted(json.loads(text)), ensure_ascii=False)
        except ValueError:
            pass
    text = redacted(text)
    text = re.sub(r'(?i)(\b(?:authorization)\s*["\']?\s*[:=]\s*["\']?)(?:Bearer\s+|Basic\s+)?[^\s"\',}]+', r'\1[REDACTED]', text)
    if ':' in text or '=' in text:
        text = re.sub(r'(?i)(\b(?:[\w-]{0,64}(?:password|passwd|secret|api[_-]?key|private[_-]?key)[\w-]{0,64}|[\w-]{0,64}token)["\']?\s*[:=]\s*)("(?:\\.|[^"\\])*"|\'[^\']*\'|[^\s,}]+)', r'\1"[REDACTED]"', text)
    if '://' in text:
        text = re.sub(r'([a-zA-Z][a-zA-Z0-9+.-]{0,64}://)[^\s/@]+:[^\s/@]+@', r'\1[REDACTED]@', text)
    return text


def data_redacted(data: Any) -> Any:
    if isinstance(data, dict):
        return {str(k): '[REDACTED]' if re.search(r'(?i)password|passwd|secret|token$|api[_-]?key|authorization|private[_-]?key|^(env|environment)$', str(k))
                else data_redacted(v) for k, v in data.items()}
    if isinstance(data, list):
        return [data_redacted(v) for v in data]
    return text_redacted(data) if isinstance(data, str) else data


def log_sources(home: Path, rid: str) -> list[dict]:
    root = run_directory(home, rid)
    paths = [root / 'coordinator.log', root / 'dispatch.log']
    paths += sorted(root.glob('tasks/*/attempts/*/stdout.jsonl'))
    paths += sorted(root.glob('tasks/*/attempts/*/stderr*'))
    return [{'round': 'jsonl' if p.suffix == '.jsonl' else 'log', 'path': str(p), 'title': str(p.relative_to(root))}
            for p in paths if p.is_file() and not any(x.is_symlink() for x in (p, *p.parents) if x == root or root in x.parents)]


def record_events(path: Path, full: bool = False) -> list[tuple[str, str, str]]:
    offset = 0
    try:
        if path.is_symlink() or path.parent.is_symlink():
            return [('--:--', 'error', t('Linked log was not read'))]
        with path.open('rb') as f:
            if not full:
                f.seek(0, 2)
                offset = max(0, f.tell() - 262144)
                f.seek(offset)
                if offset:
                    f.readline()
            data = f.read().decode('utf-8', 'replace')
    except OSError:
        return []
    printed = []
    for no, line in enumerate(data.splitlines(), 1):
        if not line.strip():
            continue
        source = f'{path.name}:{no}' if not offset else f'{path.name}@>{offset}+{no}'
        try:
            e = data_redacted(json.loads(line))
        except ValueError:
            printed.append(('--:--', 'raw', source + ' · ' + t('Malformed JSON') + ' · ' + text_redacted(line)[:240]))
            continue
        if not isinstance(e, dict):
            printed.append(('--:--', 'raw', f'{source} · JSON · {str(e)[:240]}'))
            continue
        moment = event_hour(e.get('timestamp'))
        round = str(e.get('type') or '?')
        start = len(printed)
        if round in ('assistant', 'user'):
            blocks = dictionary(e.get('message')).get('content')
            start = len(printed)
            for block in blocks if isinstance(blocks, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get('type') in ('thinking', 'text'):
                    printed += thought_event(moment, block.get('thinking') or block.get('text'))
                elif block.get('type') == 'tool_use':
                    printed.append(tool_event(moment, block.get('name'), block.get('input')))
                else:
                    printed.append((moment, 'outcome' if block.get('type') == 'tool_result' else 'raw', str(block.get('type') or '?') + ' · ' + json.dumps(block.get('content', block), ensure_ascii=False)[:240]))
            if len(printed) == start:
                printed.append((moment, 'raw', f'{source} · {round} · ' + json.dumps(e, ensure_ascii=False)[:240]))
            continue
        part = dictionary(e.get('part'))
        if round in ('text', 'reasoning'):
            printed += thought_event(moment, part.get('text'))
        elif round == 'tool_use':
            state = dictionary(part.get('state'))
            printed.append(tool_event(moment, part.get('tool'), state.get('input')))
            if state.get('output') is not None:
                output = str(state['output'])
                printed.append((moment, 'outcome', t('(tool output · {n} characters)', n=len(output)) + ' · ' + ' '.join(output.split())[:240]))
        else:
            printed.append((moment, 'error' if round == 'error' else 'raw', f'{source} · {round} · ' + json.dumps(e, ensure_ascii=False)[:240]))
        if len(printed) == start:
            printed.append((moment, 'raw', f'{source} · {round} · ' + json.dumps(e, ensure_ascii=False)[:240]))
    return printed


def brain_events(attempt: Path, full: bool = False) -> list[tuple[str, str, str]]:
    return record_events(attempt / 'stdout.jsonl', full)


def thought_event(moment: str, text: Any) -> list[tuple[str, str, str]]:
    text = str(text or '').strip()
    if not text:
        return []
    return [(moment, 'thought', t('(JSON output · {n} characters)', n=len(text)) if text[0] in '{[' else ' '.join(text.split()))]


def tool_event(moment: str, entry_name: Any, input: Any) -> tuple[str, str, str]:
    input = dictionary(input)
    summary = (input.get('command') or input.get('filePath') or input.get('file_path') or input.get('path') or input.get('pattern')
            or input.get('url') or '')
    return moment, 'tool', f"{str(entry_name or '?'):<8} {' '.join(str(summary).split())}"


def brain(attempt: Path, n: int = 5) -> tuple[list[str], list[str]]:
    events_ = brain_events(attempt)
    thought = [f'{z} {m[:150]}' for z, round, m in events_ if round == 'thought'][-n:]
    tool = [f'{z} {m[:110]}' for z, round, m in events_ if round == 'tool'][-n:]
    return thought, tool


def brain_text(attempt: Path) -> str:
    names = {'thought': t('Thought'), 'tool': t('Tool'), 'outcome': t('Tool result'), 'raw': t('Raw event'), 'error': t('Error')}
    lines = [f"{z}  {names[round]}  {m}" for z, round, m in brain_events(attempt, full=True)]
    return '\n'.join(lines) or t('(no records)')


def mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def last_attempt(home: Path, rid: str, gid: str) -> Path | None:
    try:
        attempts = [p for p in (run_directory(home, rid) / 'tasks' / gid / 'attempts').iterdir() if p.is_dir()]
    except OSError:
        return None
    return max(attempts, key=mtime) if attempts else None


def assignments(home: Path, rid: str) -> dict[str, dict]:
    output: dict[str, dict] = {}
    try:
        lines = (run_directory(home, rid) / 'assignment-history.jsonl').read_text(errors='replace').splitlines()
    except OSError:
        return output
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get('task_id'):
            output[str(e['task_id'])] = e
    return output


def task_spends(k: dict) -> dict[str, float]:
    output: dict[str, float] = {}
    for line in k.get('budget_breakdown') or []:
        if isinstance(line, dict):
            output[str(line.get('task') or '?')] = output.get(str(line.get('task') or '?'), 0.0) + number(line.get('actual_usd'))
    return output


def task_order(entry_name: str) -> tuple:
    m = re.fullmatch(r'([a-z-]+?)(\d+)', entry_name)
    group = 0 if entry_name in ('plan', 'project-ref-list') else (1 if m and m.group(1) == 'g' else 2)
    return group, m.group(1) if m else entry_name, int(m.group(2)) if m else 0


def model_short(m: Any) -> str:
    return str(m or '').split('/')[-1] or '—'


def run_enabled(k: dict, parent_status: str | None = None) -> bool:
    return k.get('state') == 'running' and parent_status in (None, 'running')


def run_ended(k: dict, parent_status: str | None = None) -> bool:
    return k.get('state') in TERMINAL or parent_status in TERMINAL


def stage_label_outcome(home: Path, k: dict, gid: str, code: str, parent_status: str | None = None) -> tuple[str, str]:
    outcome = {'PASS': ('finished', 'Passed'), 'FAIL': ('error', 'Failed'), 'ERROR': ('error', 'error'),
             'CANCELLED': ('error', 'Left unfinished'), 'SKIPPED': ('', 'Skipped')}.get(code)
    if outcome:
        return outcome[0], t(outcome[1])
    if code:
        return 'error', t('State unknown')
    task = dictionary(dictionary(k.get('tasks')).get(gid))
    started = bool(task) or (run_directory(home, str(k['run_id'])) / 'tasks' / gid).is_dir()
    if run_ended(k, parent_status):
        return ('error', t('Left unfinished')) if started else ('', t('Not run'))
    if run_enabled(k, parent_status) and task.get('state') == 'running':
        return 'now_ts', t('Running')
    return 'waiting', t('Waiting') if started else t('Queued')


def agent_nodes(home: Path, k: dict, prefix: str = '', parent_status: str | None = None) -> list[dict]:
    rid = str(k.get('run_id'))
    try:
        names = sorted((p.name for p in (run_directory(home, rid) / 'tasks').iterdir() if (p / 'attempts').is_dir()), key=task_order)
    except OSError:
        return []
    output = []
    for gid in names:
        last = last_attempt(home, rid, gid)
        if last is None:
            continue
        info = record_read(last / 'attempt.json')
        role_code = str(info.get('role') or '')
        role = t(ROLE_INFO.get(role_code, (role_code or gid,))[0])
        model = str(info.get('model') or '?')
        try:
            movement = (last / 'stdout.jsonl').stat().st_mtime
        except OSError:
            movement = 0.0
        status = ((k.get('tasks') or {}).get(gid) or {}).get('state')
        enabled = run_enabled(k, parent_status) and status == 'running'
        half = run_ended(k, parent_status) and status in (None, 'running', 'half')
        status_prose = t('Running') if enabled else (t('Left unfinished') if half else t('waiting' if status == 'running' else TASK_PROSE.get(str(status), 'waiting')))
        mark = 'now_ts' if enabled else ('error' if half else ('waiting' if status == 'running' else TASK_MARK.get(str(status), 'waiting')))
        outcome = {}
        if gid == 'qa':
            outcome = dictionary(dictionary(k.get('final_result')).get('qa')) or dictionary(dictionary(dictionary(k.get('tasks')).get(gid)).get('qa_result'))
        elif gid == 'quality':
            outcome = dictionary(dictionary(k.get('final_result')).get('review'))
        if outcome.get('status'):
            mark, status_prose = stage_label_outcome(home, k, gid, str(outcome['status']).upper(), parent_status)
        try:
            attempt_count = sum(1 for p in last.parent.iterdir() if p.is_dir())
        except OSError:
            attempt_count = 1
        thought, tool = brain(last)
        output.append(d(f'{prefix}{role} · {gid} · {model_short(model)}', status_prose, [
            ('title', f'{prefix}{role} · {gid}'), ('field', (t('Role'), role)), ('field', (t('Engine role'), role_code or '—')), ('field', (t('Model'), model)),
            ('field', (t('Status'), status_prose)), ('field', (t('Recorded state'), str(status or '—'))), ('field', (t('Attempts'), str(attempt_count))),
            ('field', (t('Last activity'), hour(movement))), ('field', (t('Session'), str(info.get('session_id') or '—'))),
            ('section', t('Latest thoughts')), ('record', thought), ('section', t('Latest tool calls')), ('record', tool)],
            status=mark, id=f'agent-{prefix}{gid}',
            file={'round': 'brain', 'path': str(last), 'title': t('{a} full record', a=f'{prefix}{role} · {gid}')}))
    return output


def sub_task_node(home: Path, k: dict, g: dict, assignment_: dict, spend_: dict, parent_status: str | None = None) -> dict:
    rid, gid = str(k['run_id']), str(g.get('id'))
    task = (k.get('tasks') or {}).get(gid) or {}
    st = str(task.get('state') or '')
    ended = run_ended(k, parent_status)
    mark = TASK_MARK.get(st, '' if ended else 'waiting')
    if st == 'running' and ended:
        mark, st = 'error', 'half'
    elif st == 'running' and not run_enabled(k, parent_status):
        mark, st = 'waiting', 'waiting'
    elif st in ('waiting', 'pending', 'queued') and ended:
        mark, st = '', ''
    definition = ' '.join(str(g.get('definition') or t('(no description)')).split())
    assignment = assignment_.get(gid) or {}
    model = assignment.get('actual_model') or task.get('last_executor') or g.get('model')
    prose = t(TASK_PROSE.get(st, st)) if st else (t('Not run') if ended else t('Queued'))
    detail = [('title', f'{gid} · {definition}'), ('field', (t('Status'), prose)), ('field', (t('What to do'), definition)),
               ('field', (t('Recorded state'), str(task.get('state') or '—'))),
               ('field', (t('Files'), list_prose(g.get('files')))), ('field', (t('Acceptance'), str(g.get('acceptance') or '—'))),
               ('field', (t('Out of scope'), list_prose(g.get('scope_outside')))), ('field', (t('Depends on'), list_prose(g.get('dependent')))),
               ('field', (t('Difficulty'), str(g.get('difficulty', '—')))), ('field', (t('Planned model'), str(g.get('model') or '—'))),
               ('field', (t('Estimate'), t('{min} min · {usd}', min=g.get('estimate_min', '?'), usd=money(g.get('estimate_usd')))))]
    detail += [('section', t('Work')), ('field', (t('Working model'), str(model or '—'))),
                ('field', (t('Models tried'), list_prose(assignment.get('tried_models')))),
                ('field', (t('Attempts'), str(task.get('attempt_count') or 0))),
                ('field', (t('Duration'), t('{d} min', d=assignment['duration_min']) if assignment.get('duration_min') is not None else '—')),
                ('field', (t('Spend'), money(spend_.get(gid, 0))))]
    if task.get('reason'):
        detail.append(('field', (t('Note'), str(task['reason'])[:200])))
    last = last_attempt(home, rid, gid)
    if last is not None:
        thought, tool = brain(last)
        detail += [('section', t('Latest thoughts')), ('record', thought), ('section', t('Latest tool calls')), ('record', tool)]
    prompt = run_directory(home, rid) / 'tasks' / gid / 'prompt.md'
    summary = t('{d} · {m}', d=prose, m=model_short(model)) if st else prose
    return d(f'{gid} {definition[:38]}', summary, detail, status=mark, id=gid,
             file={'round': 'text', 'path': str(prompt), 'title': t('{g} task text', g=gid)} if prompt.exists() else None)


def sub_task_nodes(home: Path, k: dict, plan: dict, parent_status: str | None = None) -> list[dict]:
    planned = [g for g in plan.get('tasks') or [] if isinstance(g, dict) and g.get('id')]
    ids = {str(g['id']) for g in planned}
    extra = [{'id': g} for g in (k.get('tasks') or {}) if g not in ids and g not in HELPER and not g.startswith(('correction', 'plan-input-'))]
    assignment_, spend_ = assignments(home, str(k['run_id'])), task_spends(k)
    return [sub_task_node(home, k, g, assignment_, spend_, parent_status) for g in sorted(planned + extra, key=lambda g: task_order(str(g['id'])))]


def plan_node(home: Path, k: dict, plan: dict, sub: list[dict], parent_status: str | None = None) -> dict:
    rid = str(k['run_id'])
    scope = k.get('scope') or {}
    finished = sum(1 for c in sub if c['status'] == 'finished')
    planned = bool(plan.get('tasks'))
    active = run_enabled(k, parent_status)
    groups = {g.get('parallel_group') for g in plan.get('tasks') or [] if isinstance(g, dict) and g.get('parallel_group') is not None}
    level = str(scope.get('level') or '')
    detail = [('title', t('Plan')), ('field', (t('Planner'), str(k.get('plan_model') or '—'))),
               ('field', (t('Scope score'), f"{scope.get('points', '—')} · {t(LEVEL_NAME.get(level, level))}" if level
                         else str(scope.get('points', '—')))),
               ('field', (t('Cap'), money(k.get('budget_cap_usd')))),
               ('field', (t('Subtasks'), t('{n} · {b} done', n=len(sub), b=finished))),
               ('field', (t('In parallel'), t('{n} groups', n=len(groups)) if groups else '—'))]
    rationale = scope.get('rationale') or plan.get('scope_rationale')
    if rationale:
        detail += [('section', t("Planner's note")), ('line', ' '.join(str(rationale).split())[:500])]
    if not planned:
        last = last_attempt(home, rid, 'plan')
        if last is not None:
            thought, tool = brain(last)
            detail += [('section', t('Planner record')), ('record', thought + tool)]
    path = run_directory(home, rid) / 'plan.json'
    if planned and finished == len(sub):
        status = 'finished'
    elif active and (any(c['status'] == 'now_ts' for c in sub) or dictionary(dictionary(k.get('tasks')).get('plan')).get('state') == 'running'):
        status = 'now_ts'
    else:
        status = 'error' if planned and run_ended(k, parent_status) else ('' if run_ended(k, parent_status) else 'waiting')
    summary = t('{n} subtasks · {b} done', n=len(sub), b=finished) if planned else (t('Planning') if status == 'now_ts' else t('No plan'))
    return d(t('Plan'), summary, detail, sub, status, id='plan',
             description=description(t('Plan'), t('Subtasks given by the planner. All are listed from the start and marked as they are written. Press o to read the plan file.')),
             file={'round': 'plan', 'path': str(path), 'title': t('Plan file')} if path.exists() else None)


def test_node(home: Path, k: dict, parent_status: str | None = None) -> dict:
    tasks = k.get('tasks') or {}
    qa = (k.get('final_result') or {}).get('qa') or (tasks.get('qa') or {}).get('qa_result') or {}
    attempts = [a for a in qa.get('attempts') or [] if isinstance(a, dict)]
    last = attempts[-1] if attempts else {}
    code = str(qa.get('status') or '').upper()
    mark, outcome = stage_label_outcome(home, k, 'qa', code, parent_status)
    output = str(last.get('output') or '')
    detail = [('title', t('Test')), ('field', (t('Result'), outcome)), ('field', (t('Recorded state'), code or str(dictionary(tasks.get('qa')).get('state') or '—'))),
               ('field', (t('Command'), str(qa.get('command') or (k.get('qa') or {}).get('command') or '—'))),
               ('field', (t('Attempts'), str(len(attempts)))),
               ('field', (t('Duration'), t('{s} s', s=round(number(last.get('seconds')))) if last.get('seconds') is not None else '—')),
               ('field', (t('Exit code'), str(last.get('exit_code', '—'))))]
    lines = last_lines(output, 12) if output else []
    if lines:
        detail += [('section', t('Last output')), ('record', lines)]
    assignment_, spend_ = assignments(home, str(k['run_id'])), task_spends(k)
    corrections = [sub_task_node(home, k, {'id': g, 'definition': t('Test was red, fix round')}, assignment_, spend_, parent_status)
                   for g in sorted(tasks, key=task_order) if g.startswith('correction')]
    return d(t('Test'), outcome, detail, corrections, mark, id='test',
             description=description(t('Test'), t('Runs once after all subtasks are done. If red, a fix round opens and the rounds are listed here.')),
             file={'round': 'log', 'path': output, 'title': t('Test output')} if output and Path(output).is_file() else None)


def judge_node(home: Path, k: dict, parent_status: str | None = None) -> dict:
    rv = (k.get('final_result') or {}).get('review') or {}
    code = str(rv.get('status') or '').upper()
    mark, outcome = stage_label_outcome(home, k, 'quality', code, parent_status)
    decision = str(rv.get('verdict') or '').upper()
    if decision == 'PASS' and code == 'PASS':
        outcome = t('Pass')
    independent = str(rv.get('independent')).lower() in ('true', '1')
    detail = [('title', t('Judge')), ('field', (t('Verdict'), outcome)), ('field', (t('Recorded state'), code or str(dictionary(dictionary(k.get('tasks')).get('quality')).get('state') or '—'))), ('field', (t('Judge model'), str(rv.get('model') or '—'))),
               ('field', (t('Independent'), (t('Yes') if independent else t('No')) if rv else '—')),
               ('field', (t('Writers'), list_prose(rv.get('writers')))), ('field', (t('Excluded'), list_prose(rv.get('excluded_writers')))),
               ('field', (t('Repairs'), f"{rv.get('repairs', 0)} / {rv.get('repair_limit', '—')}" if rv else '—'))]
    items = [m for m in rv.get('acceptance') or [] if isinstance(m, dict)]
    if items:
        detail += [('section', t('Acceptance items')), ('record', [
            f"{'●' if str(m.get('status')).upper() == 'PASS' else '✗'} {m.get('id')} · {m.get('status')}   "
            f"{' '.join(str(m.get('evidence') or '').split())[:160]}" for m in items])]
    try:
        texts = sorted((run_directory(home, k['run_id']) / 'tasks' / 'quality').glob('review-*.txt'), key=mtime)
    except OSError:
        texts = []
    return d(t('Judge'), outcome, detail, status=mark, id='judge',
             file={'round': 'text', 'path': str(texts[-1]), 'title': t('Judge text')} if texts else None)


def git_output(wt: str, *args: str, moment: float = 10) -> str:
    try:
        r = subprocess.run(['git', '-C', wt, *args], capture_output=True, text=True, timeout=moment)
    except (OSError, subprocess.SubprocessError):
        return ''
    return r.stdout if r.returncode == 0 else ''


def delivery_info(k: dict) -> dict:
    wt, base = str(k.get('worktree') or ''), str(k.get('source_sha') or '')
    if not wt or not base or not Path(wt).is_dir():
        return {}
    return {'branch': git_output(wt, 'branch', '--show-current').strip(), 'short': git_output(wt, 'diff', '--shortstat', f'{base}..HEAD').strip(),
            'stat': git_output(wt, 'diff', '--stat', f'{base}..HEAD').splitlines()[-15:],
            'log': git_output(wt, 'log', '--oneline', '-10', f'{base}..HEAD').splitlines(), 'wt': wt, 'base': base}


def change_summary(short: str) -> str:
    numbers = {round: int(n) for n, round in re.findall(r'(\d+) (file|insertion|deletion)', short or '')}
    if not numbers:
        return '—'
    return t('{f} files · +{e} −{s}', f=numbers.get('file', 0), e=numbers.get('insertion', 0), s=numbers.get('deletion', 0))


def delivery_node(k: dict, delivery: bool, info: dict) -> dict:
    if 'delivery_merge' in k:
        prose, mark = t('Merged · {z}', z=k['delivery_merge']), 'finished'
    elif 'delivery_push' in k:
        prose, mark = t('Pushed · {z}', z=k['delivery_push']), 'finished'
    elif delivery:
        prose, mark = t('Awaiting delivery'), 'waiting'
    else:
        prose, mark = t('Cannot be delivered'), ''
    detail = [('title', t('Delivery')), ('field', (t('Status'), prose)), ('field', (t('Branch'), info.get('branch') or '—')),
               ('field', (t('Base'), str(k.get('source_sha') or '—')[:10])), ('field', (t('Changes'), change_summary(info.get('short', ''))))]
    if not delivery and not info:
        detail.append(('muted', t('The worktree was removed or the task was not approved.')))
    if info.get('stat'):
        detail += [('section', t('Changed files')), ('record', info['stat'])]
    if info.get('log'):
        detail += [('section', t('Commits')), ('record', info['log'])]
    if delivery:
        detail += [('section', t('What to do')), ('line', t('p pushes, m merges. Both show the command first, then ask for two confirmations.'))]
    file = {'round': 'diff', 'command': ['git', '-C', info['wt'], 'diff', f"{info['base']}..HEAD"], 'title': t('Changes')} if info else None
    return d(t('Delivery'), prose, detail, status=mark, id='delivery', delivery=delivery or None, run=k['run_id'], project_ref=k.get('project'), file=file)


def report_node(home: Path, k: dict) -> dict | None:
    report = k.get('report') if isinstance(k.get('report'), dict) else {}
    if k.get('mode') == 'report' and report.get('path'):
        path, sections = Path(str(k.get('worktree') or '')) / str(report['path']), report.get('sections') or []
    elif (run_directory(home, k['run_id']) / 'project-ref-report.md').exists():
        path, sections = run_directory(home, k['run_id']) / 'project-ref-report.md', []
    else:
        return None
    present = path.is_file()
    detail = [('title', t('Report')), ('field', (t('File'), str(path))), ('field', (t('Status'), t('Ready') if present else t('Not written yet')))]
    if sections:
        detail.append(('field', (t('Sections'), list_prose(sections))))
    analyst = report.get('analyst') if isinstance(report.get('analyst'), dict) else None
    if analyst:
        if analyst.get('mode') == 'reconciliation':
            prose = t('Two independent analysts ({a}, {b}) and a reconciler ({c})', a=analyst.get('first') or '?', b=analyst.get('second') or '?',
                     c=analyst.get('reconciler') or '?')
        else:
            prose = t('Single analyst ({m}): {n}', m=analyst.get('first') or '?', n=analyst.get('reason') or t('No second strong model'))
        detail.append(('field', (t('Analyst'), prose)))
    if present:
        try:
            first = [s.strip() for s in path.read_text(errors='replace').splitlines() if s.strip()][:8]
        except OSError:
            first = []
        detail += [('section', t('Beginning')), ('record', [s[:150] for s in first])]
    return d(t('Report'), t('Ready') if present else t('Being written'), detail, status='finished' if present else 'waiting', id='report',
             file={'round': 'md', 'path': str(path), 'title': t('Report')} if present else None)


def project_ref_conditions(project_ref: dict) -> list[str]:
    ids = {str(c['run']) for c in project_ref.get('children') or [] if isinstance(c, dict) and c.get('run')}
    for f in project_ref.get('features') or []:
        if isinstance(f, dict) and isinstance(f.get('run'), list):
            ids.update(str(x) for x in f['run'] if x)
    return sorted(ids)


def known_micro(value: Any) -> int | None:
    from monthly_budget import MonthlyBudgetDenied, usd_micro
    if value is None:
        return None
    try:
        return usd_micro(value, zero=True)
    except MonthlyBudgetDenied:
        return None


def money_known(micro: int | None) -> str:
    return money(micro / 1e6) if micro is not None else t('Unknown')


def economy_scope(home: Path, k: dict, project_ref: dict) -> list[str]:
    ids = {str(k['run_id']), *project_ref_conditions(project_ref)}
    ids.update(str(r['run_id']) for r in runs(home) if r.get('project_ref_parent') == k['run_id'])
    return sorted(ids)


def economy_read(home: Path, ids: list[str]) -> dict:
    from budget_gate import USD_PROVIDERS
    db = home / 'chain/request-budget.db'
    rows = []
    known = True
    if db.exists():
        source = t('Request ledger')
        try:
            with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as con:
                found = con.execute("select provider,reserved,actual,receipt,metadata from reservation where "
                                    "json_extract(metadata,'$.run_id') in (%s) and provider in (%s)"
                                    % (','.join('?' * len(ids)), ','.join('?' * len(USD_PROVIDERS))), (*ids, *sorted(USD_PROVIDERS))).fetchall()
            for provider, reserved, actual, receipt, metadata in found:
                if type(reserved) is not int or not 0 <= reserved < 2**53 or (actual is not None and (type(actual) is not int or not 0 <= actual < 2**53)):
                    raise ValueError('invalid ledger amount')
                meta = json.loads(metadata)
                rows.append({'run': str(meta['run_id']), 'task': str(meta.get('task') or meta.get('task_id') or '?'),
                             'attempt': str(meta.get('attempt_id') or '?'), 'model': str(meta.get('model') or '?'),
                             'provider': provider, 'actual': actual, 'held': reserved if actual is None else 0,
                             'kind': 'estimated' if str(receipt or '').startswith('phantom:') else 'measured'})
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            known, rows = False, []
    else:
        source = t('Run snapshot · no ledger')
        for rid in ids:
            record = record_read(run_directory(home, rid) / 'run.json')
            spent, held = known_micro(record.get('budget_spent_usd')), known_micro(record.get('budget_held_usd'))
            rows.append({'run': rid, 'task': '?', 'attempt': '?', 'model': '?', 'provider': '?',
                         'actual': spent, 'held': held, 'kind': 'summary', 'valid': spent is not None and held is not None})
    return {'rows': rows, 'known': known, 'source': source, 'ids': ids}


def economy_sum(data: dict, rows: list[dict] | None = None) -> dict:
    rows = data['rows'] if rows is None else rows
    known = data['known'] and all(r.get('valid', True) for r in rows)
    split = known and all(r['kind'] != 'summary' for r in rows)
    return {'spent': sum(r['actual'] or 0 for r in rows) if known else None,
            'held': sum(r['held'] or 0 for r in rows) if known else None,
            'measured': sum(r['actual'] or 0 for r in rows if r['kind'] == 'measured') if split else None,
            'estimated': sum(r['actual'] or 0 for r in rows if r['kind'] == 'estimated') if split else None,
            'cash': sum(r['actual'] or 0 for r in rows if r['provider'] in ('openrouter', 'opencode')) if split else None,
            'equivalent': sum(r['actual'] or 0 for r in rows if r['provider'] in ('opencode-go', 'claude')) if split else None}


def spend_node(home: Path, k: dict, project_ref: dict) -> dict:
    ids = economy_scope(home, k, project_ref)
    data = economy_read(home, ids)
    cap = next((known_micro(x) for x in (k.get('project_budget_cap_usd'), project_ref.get('cap_usd'), k.get('budget_cap_usd')) if x is not None), None)

    def node(name: str, key: str, rows: list[dict], scope: str, children: list[dict] | None = None, ceiling: int | None = None) -> dict:
        totals = economy_sum(data, rows)
        detail = [('title', name), ('field', (t('Spent'), money_known(totals['spent']))), ('field', (t('Held'), money_known(totals['held']))),
                  ('field', (t('Scope'), scope)), ('field', (t('Source'), data['source'])),
                  ('field', (t('Measured'), money_known(totals['measured']))), ('field', (t('Estimated'), money_known(totals['estimated'])))]
        detail += [('field', (t('Cash'), money_known(totals['cash']))), ('field', (t('Usage equivalent'), money_known(totals['equivalent'])))]
        if ceiling is not None or key == 'spend':
            detail.append(('field', (t('Cap'), money_known(ceiling))))
        if data['known']:
            for label, group_key in (('By provider', lambda r: t(PROVIDER_NAME.get(r['provider'], 'unknown'))),
                                     ('By subtask and model', lambda r: f"{model_short(r['model'])} · {r['task']}")):
                groups: dict[str, list[dict]] = {}
                for row in rows:
                    groups.setdefault(group_key(row), []).append(row)
                detail.append(('section', t(label)))
                for group, entries in sorted(groups.items()):
                    cost = economy_sum(data, entries)
                    detail.append(('field', (group, t('{s} · held {h}', s=money_known(cost['spent']), h=money_known(cost['held'])))))
        else:
            detail.append(('line', t('Cost record is missing or corrupt. Unknown amounts are not zero.')))
        summary = money_known(totals['spent']) + (f' / {money_known(ceiling)}' if ceiling is not None or key == 'spend' else '')
        return d(name, summary, detail, children, id=key, economy=totals, scope=scope,
                 description=description(t('Spend'), t('Ledger total: cash USD and Go/Claude usage equivalent are shown separately. Excludes subscription fees. Run, child and attempt use the same rows.')))

    children = []
    for rid in ids:
        rows = [r for r in data['rows'] if r['run'] == rid]
        attempts = []
        for task, attempt in sorted({(r['task'], r['attempt']) for r in rows}):
            selected = [r for r in rows if (r['task'], r['attempt']) == (task, attempt)]
            attempts.append(node(f'{task} · {attempt}', f'{task}:{attempt}', selected, f'{rid} / {task} / {attempt}'))
        children.append(node(rid, rid, rows, rid, attempts))
    result = node(t('Spend'), 'spend', data['rows'], t('Parent and {n} children · all attempts', n=len(ids) - 1), children, cap)
    allocation = k.get('budget_allocation') or {}
    if allocation:
        result['detail'] += [('section', t('Initial allocation estimate')),
                             ('field', (t('Fixed operator total'), money_known(known_micro(allocation.get('amount_usd'))))),
                             ('muted', t('Cash, Go and Claude have separate limits. Internal allocations are not spending.'))]
        for name, value in (allocation.get('initial_slice_estimates_micro') or {}).items():
            result['detail'].append(('field', (t({'lanes': 'Main work', 'judge': 'Final acceptance', 'helper': 'Helper'}.get(name, name)),
                                              money_known(value if type(value) is int and value >= 0 else None))))
        for stage in allocation.get('stages') or []:
            label = {'plan': 'Planning', 'plan-review': 'Plan review', 'impl': 'Main work', 'qa': 'Test', 'quality': 'Final acceptance', 'repair': 'Repairs'}.get(stage.get('role'), '?')
            value = money_known(stage.get('estimate_micro'))
            if stage.get('provider'):
                value += ' · ' + str(stage['provider'])
            result['detail'].append(('field', (t(label), value)))
        result['detail'].append(('muted', t('Unmeasured stage costs remain unknown.')))
        if allocation.get('estimate_source'):
            result['detail'].append(('muted', t('Stage amounts bound one request. Total request counts remain unknown.')))
    result['detail'] += [('section', t('Runs')), ('record', ids), ('muted', t('? means the ledger has no task/attempt/model link.'))]
    try:
        from cost_report import breakdown, summary_lines
        result['detail'] += [('section', t('Stage breakdown')), ('record', summary_lines(breakdown(home, str(k['run_id'])))),
                              ('muted', t('Token/cache usage is unknown unless the ledger has it. Waiting time is not model slowness.'))]
    except (OSError, ValueError, KeyError, TypeError):
        result['detail'].append(('muted', t('Stage breakdown could not be read.')))
    if (home / 'chain/request-budget.db').exists() and data['known']:
        try:
            with closing(sqlite3.connect((home / 'chain/request-budget.db').resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as con:
                if con.execute("select 1 from sqlite_master where name='role_allocation'").fetchone():
                    for rid, pool, raw in con.execute('select run_id,pool,caps from role_allocation order by run_id,pool'):
                        if rid in ids:
                            result['detail'].append(('section', t('Internal allocation transfer') + f' · {rid} · {pool}'))
                            for name, value in json.loads(raw).items():
                                result['detail'].append(('field', (t({'lanes': 'Main work', 'judge': 'Final acceptance', 'helper': 'Helper'}.get(name, name)),
                                                                  money_known(value))))
        except (OSError, sqlite3.Error, ValueError, TypeError):
            result['detail'].append(('muted', t('Allocation record could not be read.')))
        snapshots = [record_read(run_directory(home, rid) / 'run.json') for rid in ids]
        recorded = [known_micro(r.get('budget_spent_usd')) for r in snapshots]
        if all(x is not None for x in recorded) and sum(recorded) != result['economy']['spent']:
            result['detail'].append(('line', t('Run snapshot differs from the ledger. This view uses the ledger.')))
    return result


def events_node(home: Path, rid: str) -> dict:
    path = run_directory(home, rid) / 'dispatch.log'
    lines = events(home, rid, 30, absolute=True)
    sources = [d(s['title'], t('Redacted log'), [('field', (t('Source'), s['title']))], id=s['title'], file=s)
                 for s in log_sources(home, rid)]
    return d(t('Events'), t('{n} events', n=len(lines)), [('title', t('Events')), ('record', lines)], child=sources, id='events',
             file={'round': 'trace', 'home': str(home), 'run_id': rid, 'title': t('Combined event trace')})


def _trace_source(home: Path, relative: Path) -> tuple[bytes, float, bool]:
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('unsafe trace path')
    directory = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in relative.parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 2_000_000:
            raise ValueError('trace source type or size limit')
        raw = stream.read(2_000_001)
        after = os.fstat(stream.fileno())
    if len(raw) > 2_000_000:
        raise ValueError('trace source size limit')
    return raw, after.st_mtime, (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)


def event_trace(home: Path, rid: str) -> dict:
    from run_store import identifier, plan_digest
    identifier(rid)
    sources, events, issues = [], {}, []
    records: dict[str, dict] = {}
    attempts: dict[tuple[str, str, str], dict] = {}
    used_bytes = 0

    def read(relative: Path, required: bool = True) -> bytes | None:
        nonlocal used_bytes
        if len(sources) >= 1000 or used_bytes >= 4_000_000:
            if not any(i['issue'] == 'trace-limit' for i in issues):
                issues.append({'source': 'trace', 'issue': 'trace-limit'})
            return None
        try:
            raw, modified, changed = _trace_source(home, relative)
        except FileNotFoundError:
            if required:
                issues.append({'source': str(relative), 'issue': 'missing'})
            return None
        except (OSError, ValueError):
            issues.append({'source': str(relative), 'issue': 'unreadable-or-limit'})
            return None
        if used_bytes + len(raw) > 4_000_000:
            issues.append({'source': str(relative), 'issue': 'trace-limit'})
            return None
        sources.append({'path': str(relative), 'mtime': modified, 'sha256': hashlib.sha256(raw).hexdigest(), 'stable': not changed})
        used_bytes += len(raw)
        if changed:
            issues.append({'source': str(relative), 'issue': 'changed-during-read'})
        return raw

    def add(run: str, relative: Path, line: int, payload: Any, kind: str, task: str | None = None,
            attempt: str | None = None, location: str | None = None) -> None:
        if len(events) >= 5000:
            if not any(i['issue'] == 'event-limit' for i in issues):
                issues.append({'source': 'trace', 'issue': 'event-limit'})
            return
        clean = data_redacted(payload)
        data = clean if isinstance(clean, dict) else {}
        task = task or data.get('task_id') or data.get('task_id')
        attempt = attempt or data.get('attempt_id')
        context = attempts.get((run, str(task), str(attempt)), {})
        revision = data.get('revision', data.get('plan_revision', context.get('plan_revision')))
        request_hash = data.get('request_hash', context.get('request_hash', records.get(run, {}).get('request_input_hash')))
        evidence = data.get('evidence_status', 'recorded') if kind in ('plan_change', 'plan_review', 'plan_revision') else 'recorded'
        correlation = 'recorded' if revision is not None and request_hash else 'historical-unknown'
        if ((revision is not None and context.get('plan_revision') is not None and revision != context['plan_revision'])
                or (request_hash and records.get(run, {}).get('request_input_hash') and request_hash != records[run]['request_input_hash'])):
            issues.append({'source': str(relative), 'issue': 'correlation-conflict'})
            evidence = 'conflict'
        stamp = data.get('timestamp', data.get('ts', data.get('updated') if kind == 'snapshot' else None))
        timestamp = None
        if type(stamp) in (int, float) and 0 < stamp < 10**16:
            timestamp = float(stamp) / (1000 if stamp > 100_000_000_000 else 1)
            if not timestamp < 253_402_300_800:
                timestamp = None
        elif isinstance(stamp, str):
            try:
                parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                if parsed.tzinfo is not None:
                    timestamp = parsed.timestamp()
            except (ValueError, OverflowError, OSError):
                pass
        identity = [run, str(relative), kind, clean]
        key = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
        if key in events:
            events[key]['lines'].append(line)
            if location:
                events[key]['locations'].append(location)
            return
        events[key] = {'id': key, 'run_id': run, 'root_run_id': rid, 'parent_run_id': records.get(run, {}).get('project_ref_parent') or rid,
                       'child_id': run if run != rid else None, 'project': records.get(run, {}).get('project'), 'task_id': task,
                       'attempt_id': attempt, 'request_id': data.get('request_id'), 'request_hash': request_hash,
                       'source_sha': records.get(run, {}).get('source_sha'),
                       'revision': revision, 'change_id': data.get('change_id'), 'evidence_status': evidence,
                       'correlation_status': correlation, 'source_kind': kind,
                       'timestamp': timestamp, 'type': str(data.get('type') or kind), 'source': str(relative),
                       'lines': [line], 'locations': [location] if location else [], 'data': clean}

    def journal(run: str, relative: Path, record: dict) -> None:
        history, reviews = record.get('plan_history', []), record.get('plan_reviews', [])
        if not isinstance(history, list) or not isinstance(reviews, list):
            issues.append({'source': str(relative), 'issue': 'malformed-plan-history'})
            return
        if len(history) + len(reviews) > 5000:
            issues.append({'source': str(relative), 'issue': 'event-limit'})
            return
        valid = set()
        valid_reviews = []
        for index, entry in enumerate(history):
            if not isinstance(entry, dict):
                issues.append({'source': str(relative), 'issue': 'malformed-plan-revision'})
                continue
            revision = entry.get('revision')
            good = type(revision) is int and revision == index + 1 and isinstance(entry.get('plan'), dict)
            if good:
                good = entry.get('digest') == plan_digest(entry['plan']) and entry.get('request_hash') == record.get('request_input_hash')
            if good:
                valid.add(revision)
            else:
                issues.append({'source': str(relative), 'issue': 'plan-revision-conflict'})
            add(run, relative, 1, {**entry, 'type': 'plan_revision', 'timestamp': entry.get('created'),
                'evidence_status': 'recorded' if good else 'conflict'}, 'plan_revision', location=f'/plan_history/{index}')
        for index, review in enumerate(reviews):
            if not isinstance(review, dict):
                issues.append({'source': str(relative), 'issue': 'malformed-plan-review'})
                continue
            revision = review.get('revision_number')
            good = type(revision) is int and (revision in valid or revision == 0 and review.get('status') == 'ERROR')
            if review.get('status') == 'PASS':
                good = good and bool(review.get('attempt_id') and review.get('session_id'))
                if good:
                    good = review.get('plan_digest') == history[revision - 1].get('digest')
            if not good:
                issues.append({'source': str(relative), 'issue': 'plan-review-conflict'})
            else:
                valid_reviews.append(review)
            add(run, relative, 1, {**review, 'type': 'plan_review', 'review_identity': review.get('revision'), 'revision': revision, 'timestamp': review.get('created'),
                'evidence_status': 'recorded' if good else 'conflict'}, 'plan_review', location=f'/plan_reviews/{index}')
        changes = record.get('plan_changes', [])
        if not isinstance(changes, list):
            issues.append({'source': str(relative), 'issue': 'malformed-plan-changes'})
            return
        for index, change in enumerate(changes):
            if not isinstance(change, dict) or not isinstance(change.get('events'), list):
                issues.append({'source': str(relative), 'issue': 'malformed-plan-change'})
                continue
            revision, stage, previous, seen = change.get('base_revision'), None, None, {}
            for number, event in enumerate(change['events']):
                if not isinstance(event, dict):
                    issues.append({'source': str(relative), 'issue': 'malformed-plan-change-event'})
                    continue
                encoded = json.dumps(event, sort_keys=True, allow_nan=False)
                action, stamp = event.get('action'), event.get('ts')
                duplicate = encoded in seen
                good = type(revision) is int and revision in valid and type(stamp) in (int, float) and 0 < stamp < 253_402_300_800
                if good and previous is not None and stamp < previous:
                    good = False
                target = event.get('revision', revision)
                stop = change.get('stop')
                if action == 'requested':
                    good = good and stage is None
                elif action == 'stopped':
                    good = good and stage == 'requested' and isinstance(stop, dict) and isinstance(stop.get('runs'), list) and run in stop['runs'] and isinstance(stop.get('cost'), dict)
                    good = good and type(stop.get('ts')) in (int, float) and previous <= stop['ts'] <= stamp
                elif action == 'revision_saved':
                    good = good and stage in ('stopped', 'review_rejected') and type(target) is int and target in valid and target > revision
                elif action == 'review_rejected':
                    good = good and stage == 'revision_saved' and any(r.get('status') in ('FAIL', 'ERROR') and r.get('revision_number') == revision
                        and type(r.get('created')) in (int, float) and r['created'] <= stamp for r in valid_reviews)
                elif action == 'withdrawn':
                    good = good and stage in ('stopped', 'review_rejected')
                elif action == 'resumed':
                    good = good and stage in ('revision_saved', 'review_rejected', 'withdrawn') and target == revision
                    good = good and any(isinstance(r, dict) and r.get('status') == 'PASS' and r.get('revision_number') == target
                                        and type(r.get('created')) in (int, float) and r['created'] <= stamp for r in valid_reviews)
                else:
                    good = False
                if duplicate:
                    add(run, relative, 1, seen[encoded], 'plan_change', location=f'/plan_changes/{index}/events/{number}')
                    continue
                if not good:
                    issues.append({'source': str(relative), 'issue': 'plan-change-order-or-evidence-conflict'})
                payload = {**event, 'type': 'plan_change:' + str(action), 'change_id': change.get('id'),
                           'revision': target, 'base_revision': change.get('base_revision'), 'origin': change.get('origin'),
                           'request_hash': record.get('request_input_hash'), 'evidence_status': 'recorded' if good else 'conflict'}
                if action == 'requested':
                    payload['text'] = change.get('text')
                if action == 'stopped':
                    payload['stop'] = stop
                seen[encoded] = payload
                add(run, relative, 1, payload, 'plan_change', location=f'/plan_changes/{index}/events/{number}')
                if good:
                    stage, revision, previous = action, target, stamp

    candidates = [rid]
    descendants: dict[str, list[str]] = {}
    runs_root = home / 'runs'
    discovery_bytes = 0
    if not runs_root.is_symlink():
        for index, candidate in enumerate(sorted(runs_root.glob('*/run.json'))):
            if index >= 1000 or discovery_bytes >= 4_000_000:
                issues.append({'source': 'runs', 'issue': 'discovery-limit'})
                break
            if candidate.parent.name == rid:
                continue
            try:
                identifier(candidate.parent.name)
                raw, _, _ = _trace_source(home, candidate.relative_to(home))
                discovery_bytes += len(raw)
                record = json.loads(raw)
            except (OSError, ValueError):
                continue
            if isinstance(record, dict) and isinstance(record.get('project_ref_parent'), str):
                descendants.setdefault(record['project_ref_parent'], []).append(candidate.parent.name)
    candidates.extend(descendants.get(rid, []))
    visited: set[str] = set()
    for run in candidates:
        if run in visited:
            continue
        try:
            identifier(run)
        except ValueError:
            issues.append({'source': 'project_mode.json', 'issue': 'invalid-child-id'})
            continue
        if len(visited) >= 200:
            issues.append({'source': 'runs', 'issue': 'run-limit'})
            break
        visited.add(run)
        candidates.extend(child for child in descendants.get(run, []) if child not in candidates)
        root = Path('runs') / run
        mirror = None
        for name in ('run.json', 'project_mode.json', 'plan.json'):
            relative = root / name
            raw = read(relative, required=name == 'run.json')
            if raw is None:
                continue
            try:
                record = json.loads(raw, parse_constant=lambda value: {'invalid_json_number': value})
                if not isinstance(record, dict):
                    raise ValueError('trace snapshot must be an object')
            except ValueError:
                add(run, relative, 1, raw.decode('utf-8', 'replace'), 'malformed')
                issues.append({'source': str(relative), 'issue': 'malformed-json'})
                continue
            if name == 'run.json':
                records[run] = record
                if record.get('run_id') != run:
                    issues.append({'source': str(relative), 'issue': 'run-id-mismatch'})
                journal(run, relative, record)
            elif name == 'project_mode.json':
                candidates.extend(child for child in project_ref_conditions(record) if child not in candidates)
            elif name == 'plan.json':
                mirror = record
            add(run, relative, 1, record, 'snapshot')
        history = records.get(run, {}).get('plan_history')
        if isinstance(history, list) and history and isinstance(history[-1], dict):
            if mirror is None or plan_digest(mirror) != history[-1].get('digest'):
                issues.append({'source': str(root / 'plan.json'), 'issue': 'execution-plan-mismatch'})
        base = home / root
        paths = [root / 'coordinator.log', root / 'dispatch.log']
        if not base.is_symlink():
            paths += [p.relative_to(home) for pattern in ('tasks/*/attempts/*/attempt.json', 'tasks/*/attempts/*/stdout.jsonl',
                                                         'tasks/*/attempts/*/stderr.log') for p in sorted(base.glob(pattern))]
        for relative in paths:
            raw = read(relative, required=False)
            if raw is None:
                continue
            parts = relative.parts
            task, attempt = (parts[3], parts[5]) if len(parts) == 7 else (None, None)
            text = raw.decode('utf-8', 'replace')
            text = re.sub(r'-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)',
                          lambda match: '[REDACTED]' + '\n' * match.group(0).count('\n'), text)
            lines = [text] if relative.name == 'attempt.json' else text.splitlines()
            for number, line in enumerate(lines, 1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line, parse_constant=lambda value: {'invalid_json_number': value})
                    kind = 'snapshot' if relative.name == 'attempt.json' else 'event'
                except ValueError:
                    match = re.match(r'^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})?)\s+(.+)$', line)
                    payload = {'timestamp': match[1], 'text': match[2]} if match else line
                    kind = 'log' if relative.suffix == '.log' else 'malformed'
                if relative.name == 'attempt.json' and isinstance(payload, dict):
                    if (payload.get('run_id'), payload.get('task_id'), payload.get('attempt_id')) == (run, task, attempt):
                        attempts[(run, str(task), str(attempt))] = payload
                    else:
                        issues.append({'source': str(relative), 'issue': 'attempt-identity-conflict'})
                add(run, relative, number, payload, kind, task, attempt)
    for event in events.values():
        if event['attempt_id'] and not event['task_id']:
            matches = [context for (run, _, attempt), context in attempts.items() if run == event['run_id'] and attempt == event['attempt_id']]
            if len(matches) == 1:
                event['task_id'] = matches[0]['task_id']
                if event['revision'] is not None and matches[0].get('plan_revision') is not None and event['revision'] != matches[0]['plan_revision']:
                    event['evidence_status'] = 'conflict'
                    issues.append({'source': event['source'], 'issue': 'correlation-conflict'})
    route = Path('chain/model_router.jsonl')
    raw = read(route, required=False)
    if raw is not None:
        for number, line in enumerate(raw.decode('utf-8', 'replace').splitlines(), 1):
            if number > 5000:
                issues.append({'source': str(route), 'issue': 'route-event-limit'})
                break
            try:
                payload = json.loads(line, parse_constant=lambda value: {'invalid_json_number': value})
                if not isinstance(payload, dict):
                    raise ValueError('invalid route event')
            except ValueError:
                issues.append({'source': str(route), 'issue': 'malformed-route-event'})
                continue
            if payload.get('run_id') in visited:
                add(payload['run_id'], route, number, payload, str(payload.get('event') or 'route'))
    ledger = Path('chain/request-budget.db')
    raw = read(ledger)
    if raw is not None:
        wal = read(Path(str(ledger) + '-wal'), required=False)
        ledger_sources = [s for s in sources if s['path'] in (str(ledger), str(ledger) + '-wal')]
        unsafe = any(not s['stable'] for s in ledger_sources) or any(i['source'] == str(ledger) + '-wal' for i in issues)
        try:
            if unsafe:
                raise ValueError('unstable ledger')
            with tempfile.TemporaryDirectory(prefix='cadre-trace-') as temporary:
                copy = Path(temporary) / ledger.name
                copy.write_bytes(raw)
                if wal is not None:
                    copy.with_name(copy.name + '-wal').write_bytes(wal)
                with closing(sqlite3.connect(copy.as_uri() + '?mode=ro', uri=True)) as con:
                    con.execute('PRAGMA query_only=ON')
                    work = 0
                    def bounded_query():
                        nonlocal work
                        work += 1
                        return work > 1000
                    con.set_progress_handler(bounded_query, 1000)
                    placeholders = ','.join('?' for _ in visited)
                    rows = con.execute('select id,provider,created,reserved,actual,receipt,metadata from reservation '
                        f"where case when json_valid(metadata) then json_extract(metadata,'$.run_id') end in ({placeholders}) order by created,id limit 5001",
                        sorted(visited)).fetchall()
                    malformed = con.execute('select count(*) from reservation where not json_valid(metadata)').fetchone()[0]
                if malformed:
                    issues.append({'source': str(ledger), 'issue': 'malformed-ledger-metadata'})
                for source in ledger_sources:
                    current, _, changed = _trace_source(home, Path(source['path']))
                    if changed or hashlib.sha256(current).hexdigest() != source['sha256']:
                        raise ValueError('ledger changed')
                if wal is None:
                    try:
                        _trace_source(home, Path(str(ledger) + '-wal'))
                    except FileNotFoundError:
                        pass
                    else:
                        raise ValueError('ledger WAL appeared')
            if len(rows) > 5000:
                issues.append({'source': str(ledger), 'issue': 'ledger-event-limit'})
            for ident, provider, created, reserved, actual, receipt, metadata in rows[:5000]:
                meta = json.loads(metadata)
                common = {**meta, 'request_id': ident, 'provider': provider, 'reserved_micro': reserved,
                          'actual_micro': actual, 'receipt': receipt, 'provider_request_id': meta.get('request_id')}
                add(meta['run_id'], ledger, 1, {**common, 'type': 'request_reserved', 'timestamp': created},
                    'request_reserved', location='/reservation/' + str(ident))
                if actual is not None:
                    add(meta['run_id'], ledger, 1, {**common, 'type': 'request_settled', 'timestamp': meta.get('settled_ts')},
                        'request_settled', location='/reservation/' + str(ident))
        except (OSError, ValueError, sqlite3.Error):
            issues.append({'source': str(ledger), 'issue': 'ledger-unreadable-or-changed'})
    ordered = sorted(events.values(), key=lambda e: (e['timestamp'] is None, e['timestamp'] or 0, e['source'], e['lines'][0]))
    return data_redacted({'schema': 'cadre-trace-v1', 'run_id': rid, 'sources': sources, 'issues': issues, 'events': ordered,
                         'scope': 'run-child-attempt-plan-route-ledger', 'history': 'snapshots-are-current-state'})


def trace_text(trace: dict) -> str:
    lines = [t('Combined event trace') + ' · ' + str(trace['run_id']), t('Snapshots do not prove past transitions. Unknown times appear last.'), '']
    for source in trace['sources']:
        lines.append(source['path'] + ' · ' + t('Last updated') + ': ' + absolute_moment(source['mtime']))
    for issue in trace['issues']:
        lines.append('! ' + issue['source'] + ' · ' + issue['issue'])
    for event in trace['events']:
        lines.extend(['', absolute_moment(event['timestamp']) if event['timestamp'] is not None else t('Unknown'),
                      ' · '.join(str(event.get(k) if event.get(k) is not None else '—') for k in ('run_id', 'task_id', 'attempt_id', 'request_id', 'request_hash', 'revision', 'change_id', 'evidence_status', 'correlation_status')),
                      event['source'] + ':' + ','.join(map(str, event['lines'])) + ' '.join(event.get('locations', [])) + ' · ' + event['type'],
                      json.dumps(event['data'], ensure_ascii=False, indent=2, allow_nan=False)])
    return text_redacted('\n'.join(lines))


def run_now(k: dict, sub: list[dict]) -> str:
    if not run_enabled(k):
        return ''
    tasks = k.get('tasks') or {}
    running = [c['entry_name'] for c in sub if c['status'] == 'now_ts']
    if not sub and not k.get('plan_ready') and dictionary(tasks.get('plan')).get('state') == 'running':
        return t('Planning')
    if running:
        return t('Coding · {g}', g=', '.join(running[:2]))
    if dictionary(tasks.get('quality')).get('state') == 'running' and not dictionary(dictionary(k.get('final_result')).get('review')).get('status'):
        return t('Judge')
    if dictionary(tasks.get('qa')).get('state') == 'running' and not (dictionary(dictionary(k.get('final_result')).get('qa')) or dictionary(dictionary(tasks.get('qa')).get('qa_result'))).get('status'):
        return t('Test')
    return ''


def normal_progress(k: dict, sub: list[dict], test_: dict, judge_: dict, delivery: bool) -> tuple[int, str, list, str]:
    active = run_enabled(k)
    n, finished = len(sub), sum(1 for c in sub if c['status'] == 'finished')
    planned = bool(sub) or bool(k.get('plan_ready'))
    encoding = 'finished' if n and finished == n else ('now_ts' if any(c['status'] == 'now_ts' for c in sub) else (
        'error' if any(c['status'] == 'error' for c in sub) else ('' if run_ended(k) else 'waiting')))
    if 'delivery_merge' in k or 'delivery_push' in k:
        delivery_mark = 'finished'
    else:
        delivery_mark = 'waiting' if delivery or active else ''
    plan_active = active and dictionary(dictionary(k.get('tasks')).get('plan')).get('state') == 'running'
    flow = [(t('Planning'), 'finished' if planned else ('now_ts' if plan_active else ('' if run_ended(k) else 'waiting'))), (t('Coding {b}/{n}', b=finished, n=n), encoding),
            (t('Test'), test_['status']), (t('Judge'), judge_['status']), (t('Delivery'), delivery_mark)]
    if (k.get('final_result') or {}).get('status'):
        percent, stage_label = 100, t('Result')
    elif judge_['status'] == 'now_ts':
        percent, stage_label = 90, t('Judge')
    elif test_['status'] in ('now_ts', 'error') and encoding == 'finished':
        percent, stage_label = 78, t('Test')
    elif planned:
        percent, stage_label = 10 + int(60 * finished / max(n, 1)), t('Coding {b}/{n}', b=finished, n=n)
    else:
        percent, stage_label = 5, t('Planning')
    return percent, stage_label, flow, run_now(k, sub)


def project_ref_structure(home: Path, k: dict, project_ref: dict) -> tuple[list, int, str, list, str, list]:
    from project_mode import status_view
    project_ref = status_view(k, project_ref)
    rid = str(k['run_id'])
    active = run_enabled(k)
    parent_status = str(k.get('state') or '')
    feature = [f for f in project_ref.get('features') or [] if isinstance(f, dict) and f.get('id')]
    enabled = k.get('project_active_feature')
    active_id = k.get('project_active_run')
    try:
        from run_store import RunStore, bounded_json
        active_child = bounded_json(RunStore(home).path(active_id) / 'run.json') if active_id else {}
    except (OSError, ValueError, TypeError):
        active_child = {}
    active_owned = active_child.get('run_id') == active_id and active_child.get('project_ref_parent') == rid and active_child.get('project_ref_feature') == enabled
    agents_ = agent_nodes(home, k)
    parts, now = [], ''
    part_description = description(t('This part'), t('This part is a separate run with its own plan, subtasks and test. The judge review happens once in the Final check.'),
                                [(t('Plan'), t('Subtasks of the part')), (t('Test'), t('Test of the part'))])
    risky_description = description(t('This part'), t('Risky part. After its plan, subtasks and test, the reviewer also checks it in this run.'),
                                 [(t('Plan'), t('Subtasks of the part')), (t('Test'), t('Test of the part')), (t('Judge'), t("The part's review"))])
    for f in feature:
        run_list = f.get('run')
        run_id = run_list[-1] if isinstance(run_list, list) and run_list else None
        if f['id'] == enabled and active_owned:
            run_id = active_id
        sub_k = record_read(run_directory(home, run_id) / 'run.json') if run_id else {}
        content: list[dict] = []
        sub_now = ''
        judged = bool(f.get('risky'))
        if sub_k.get('run_id'):
            sub_plan = record_read(run_directory(home, run_id) / 'plan.json')
            sub = sub_task_nodes(home, sub_k, sub_plan, parent_status)
            content = [plan_node(home, sub_k, sub_plan, sub, parent_status), test_node(home, sub_k, parent_status)]
            review = str(dictionary(dictionary(sub_k.get('final_result')).get('review')).get('status') or '').upper()
            judged = judged or 'quality' in dictionary(sub_k.get('tasks')) or review not in ('', 'SKIPPED')
            if judged:
                content.append(judge_node(home, sub_k, parent_status))
            agents_ += agent_nodes(home, sub_k, f"{f['id']} · ", parent_status)
            sub_now = run_now(sub_k, sub) if active else ''
        mark = PART_MARK.get(str(f.get('status')), 'waiting')
        prose = t(PART_PROSE.get(str(f.get('status')), str(f.get('status') or '')))
        if run_ended(k) and f.get('status') == 'waiting':
            started = bool(sub_k.get('tasks')) or sub_k.get('state') in ('running', 'retryable', *TERMINAL)
            mark, prose = ('error', t('Left unfinished')) if started else ('', t('Not run'))
        elif f['id'] == enabled and f.get('status') == 'waiting' and active and (not sub_k or run_enabled(sub_k)):
            mark, prose = 'now_ts', sub_now or t('Running')
            now = f"{f['id']} {f.get('title', '')}" + (f' · {sub_now}' if sub_now else '')
        if f.get('status') in ('passed', 'applied') and sub_k and sub_k.get('state') not in ('completed', 'implemented'):
            mark, prose = 'error', t('State conflict')
        if run_id and not sub_k:
            mark, prose = 'error', t('State unknown')
        if f['id'] == enabled and active_id and not active_owned:
            mark, prose, now = 'error', t('State conflict'), ''
        prompt = run_directory(home, run_id) / 'prompt.md' if run_id else None
        parts.append(d(f"{f['id']} {f.get('title', '')}", prose, [
            ('title', f"{f['id']} · {f.get('title', '')}"), ('field', (t('Status'), prose)),
            ('field', (t('Recorded state'), f"{f.get('status') or '—'} · {sub_k.get('state') or '—'}")),
            ('field', (t('What to do'), ' '.join(str(f.get('description') or '—').split())[:400])),
            ('field', (t('Acceptance'), str(f.get('acceptance') or '—')[:300])), ('field', (t('Test command'), str(f.get('qa_command') or '—'))),
            ('field', (t('Depends on'), list_prose(f.get('dependent')))), ('field', (t('Priority'), str(f.get('priority', '—')))),
            ('field', (t('Scope score'), str(f.get('scope_points', '—')))), ('field', (t('Estimate'), money(f.get('estimate_usd')))),
            ('field', (t('Risky'), t('Yes') if f.get('risky') else t('No'))), ('field', (t('Attempts'), str(f.get('attempt', 0)))),
            ('field', (t('Run'), str(run_id or '—')))], content, mark, id=str(f['id']),
            description=risky_description if judged else part_description,
            file={'round': 'text', 'path': str(prompt), 'title': t('{g} task text', g=f['id'])} if prompt and prompt.exists() else None))
    passing = sum(1 for f in feature if f.get('status') in ('passed', 'applied'))
    planner = last_attempt(home, rid, 'project-ref-list')
    plan_model = record_read(planner / 'attempt.json').get('model') if planner else None
    plan_detail = [('title', t('Plan')), ('field', (t('Planner'), str(plan_model or k.get('plan_model') or '—'))),
                    ('field', (t('Parts'), t('{n} · {g} passed', n=len(feature), g=passing))),
                    ('field', (t('Cap'), money(project_ref.get('cap_usd') or k.get('budget_cap_usd')))),
                    ('field', (t('Spent'), money(project_ref.get('spend_usd')))), ('field', (t('Staging branch'), str(project_ref.get('intermediate_branch') or '—')))]
    if project_ref.get('request_summary'):
        plan_detail += [('section', t('Request summary')), ('line', ' '.join(str(project_ref['request_summary']).split())[:500])]
    plan = d(t('Plan'), t('{n} parts · {g} passed', n=len(feature), g=passing), plan_detail, parts,
             'finished' if feature and passing == len(feature) else ('now_ts' if active else ('error' if run_ended(k) else 'waiting')), id='plan',
             description=description(t('Plan'), t('The planner split the large task into parts. Parts run in turn as separate runs. Press o to read the project plan.')),
             file={'round': 'json', 'path': str(run_directory(home, rid) / 'project_mode.json'), 'title': t('Project plan')})
    last_id = next((c.get('run') for c in project_ref.get('children') or [] if isinstance(c, dict) and c.get('feature') == 'final-acceptance'), None)
    if enabled is None and active_owned:
        last_id = active_id
    last_k = record_read(run_directory(home, last_id) / 'run.json') if last_id else {}
    last_content: list[dict] = []
    if last_k.get('run_id'):
        last_plan = record_read(run_directory(home, last_id) / 'plan.json')
        last_sub = sub_task_nodes(home, last_k, last_plan, parent_status)
        last_content = [plan_node(home, last_k, last_plan, last_sub, parent_status), test_node(home, last_k, parent_status), judge_node(home, last_k, parent_status)]
        agents_ += agent_nodes(home, last_k, t('Final check') + ' · ', parent_status)
        if run_enabled(last_k, parent_status):
            step = run_now(last_k, last_sub)
            now = t('Final check') + (f' · {step}' if step else '')
    project_ref_status = str(project_ref.get('status') or '')
    last_code = str(dictionary(last_k.get('final_result')).get('status') or '').upper()
    if last_code == 'PASS':
        last_mark, last_prose = 'finished', t('Passed')
    elif last_code in ('FAIL', 'ERROR', 'CANCELLED'):
        last_mark, last_prose = 'error', t('Left unfinished') if last_code == 'CANCELLED' else t('Failed')
    elif run_enabled(last_k, parent_status):
        last_mark, last_prose = 'now_ts', t('Running')
    elif last_k and (run_ended(last_k, parent_status)):
        last_mark, last_prose = 'error', t('Left unfinished')
    elif last_id and not last_k:
        last_mark, last_prose = 'error', t('State unknown')
    elif last_k:
        last_mark, last_prose = 'waiting', t('Waiting')
    else:
        last_mark, last_prose = ('', t('Not run')) if run_ended(k) else ('waiting', t('Queued'))
    if enabled is None and active_id and not active_owned:
        last_mark, last_prose, now = 'error', t('State conflict'), ''
    last_check = d(t('Final check'), last_prose, [
        ('title', t('Final check')), ('field', (t('Project status'), project_ref_status or '—')), ('field', (t('Reason'), str(project_ref.get('reason') or '—'))),
        ('field', (t('Recorded state'), f"{last_k.get('state') or '—'} · {last_code or '—'}")),
        ('field', (t('Final acceptance round'), str(project_ref.get('final_acceptance_round', '—')))), ('field', (t('Run'), str(last_id or '—')))],
        last_content, last_mark, id='last-check',
        description=description(t('Final check'), t('When all parts are done, all tests and the judge review run once more.'),
                          [(t('Plan'), t('Final fixes if needed')), (t('Test'), t('All tests')), (t('Judge'), t('Independent review'))]))
    if project_ref_status == 'complete' and last_code == 'PASS' and parent_status == 'completed':
        percent, stage_label = 100, t('Result')
    elif passing == len(feature) and feature:
        percent, stage_label = 92, t('Final check')
    else:
        percent, stage_label = int(90 * passing / max(len(feature), 1)), t('Part {g}/{n}', g=passing, n=len(feature))
    flow = [(str(f['id']), p['status']) for f, p in zip(feature, parts)] + [(t('Final check'), last_mark)]
    return [plan, last_check], percent, stage_label, flow, now, agents_


def outcome_node(home: Path, k: dict) -> dict:
    result = dictionary(k.get('final_result'))
    status = str(result.get('status') or t('State unknown'))
    return d(t('Result'), status, [('title', t('Result')), ('field', (t('Status'), status)),
        ('field', (t('Recorded state'), str(k.get('state') or '—'))),
        ('field', (t('Test'), str(dictionary(result.get('qa')).get('status') or '—'))),
        ('field', (t('Judge'), str(dictionary(result.get('review')).get('status') or '—'))),
        ('field', (t('Reason'), text_redacted(str(result.get('reason') or k.get('reason') or '—')))),
        ('muted', t('Intermediate implementation is not final product acceptance.'))],
        status='finished' if result.get('status') == 'PASS' else '', id='outcome',
        file={'round': 'json', 'path': str(run_directory(home, k['run_id']) / 'run.json'), 'title': t('Run record')})


def spend_warning_detail(k: dict) -> list:
    warning = dictionary(k.get('spend_warning'))
    if warning.get('kind') != 'speed-warning':
        return []
    return [('title', t('Spending rate warning')),
            ('field', (t('Reason'), text_redacted(str(warning.get('reason') or '—')))),
            ('field', (t('Progress'), t('Recorded progress') if warning.get('progress') == 'productive'
                      else t('No progress (product unchanged, no recorded step)') if warning.get('progress') == 'none' else t('Progress unknown'))),
            ('field', (t('Last updated'), absolute_moment(warning.get('ts')))),
            ('muted', t('Rate warnings need no approval. Total, monthly, capacity and permission limits still apply.'))]


def operation_node(home: Path, k: dict, plan: dict) -> dict:
    from run_store import RunStore
    rid = str(k['run_id'])
    cap = dictionary(k.get('request')).get('budget_usd')
    if cap is None and k.get('budget_manual'):
        cap = k.get('budget_cap_usd')
    budget = dictionary(k.get('request_budget'))
    detail = [('title', t('Decision and operations')), ('field', (t('Waiting reason'), text_redacted(str(k.get('reason') or t('None'))))),
              ('field', (t('Retry time'), absolute_moment(k.get('retry_after')))),
              ('field', (t('Exact total cap'), str(cap) + ' USD' if cap is not None else t('No fixed total cap is recorded'))),
              ('field', (t('Budget scope'), str(budget['scope']) if budget.get('scope') else t('Run ledger'))),
              ('muted', t('The cap is neither permission nor a spending target.'))]
    if cap is None:
        detail.append(('field', (t('Current stage cap'), str(k.get('budget_cap_usd', '—')) + ' USD')))
    if budget.get('providers'):
        detail.append(('field', (t('Providers'), text_redacted(str(budget['providers'])))))
    remaining = [g.get('id') for g in plan.get('tasks', []) if isinstance(g, dict)
                 and dictionary(dictionary(k.get('tasks')).get(g.get('id'))).get('state') not in ('complete', 'passed', 'applied')]
    detail += [('field', (t('Remaining scope'), list_prose(remaining) if plan else t('No plan yet')))]
    if k.get('spend_alarm'):
        detail.append(('line', t('The spending alarm needs a separate decision. Approval does not increase total or monthly caps.')))
    detail += spend_warning_detail(k)
    if k.get('budget_transfers'):
        detail.append(('field', (t('Internal allocation transfer'), text_redacted(json.dumps(k['budget_transfers'], ensure_ascii=False)))))
    decisions = []
    try:
        raw, changed, unstable = _trace_source(home, Path('chain/model_router.jsonl'))
        if unstable:
            raise ValueError('decision-source-changing')
        for line in raw.splitlines(keepends=True):
            if not line.endswith(b'\n'):
                continue
            try:
                event = json.loads(line)
                if isinstance(event, dict) and event.get('run_id') == rid and event.get('event') == 'chain':
                    decisions.append(event)
            except (ValueError, UnicodeError):
                continue
        if decisions:
            event = decisions[-1]
            detail += [('section', t('Last recorded routing decision')),
                       ('field', (t('Source'), 'chain/model_router.jsonl')),
                       ('field', (t('Decision time'), text_redacted(str(event.get('ts') or '—')))),
                       ('field', (t('Last updated'), absolute_moment(changed))),
                       ('field', (t('Role'), str(event.get('role') or '—'))),
                       ('field', (t('Eligible candidate order'), list_prose(event.get('chain')))),
                       ('record', [text_redacted(str(reason)) for reason in event.get('reason', [])])]
        else:
            detail.append(('line', t('No routing decision is recorded for this run yet.')))
    except FileNotFoundError:
        detail.append(('line', t('No routing decision is recorded for this run yet.')))
    except (OSError, ValueError, TypeError, KeyError):
        detail.append(('line', t('Routing records are unreadable. The current decision is unknown.')))
    actions = []
    attempts = []
    for path in run_directory(home, rid).glob('tasks/*/attempts/*/attempt.json'):
        try:
            raw, _, unstable = _trace_source(home, path.relative_to(home))
            attempt = json.loads(raw)
            if not unstable and attempt.get('run_id') == rid and isinstance(attempt.get('created'), (float, int)):
                attempts.append(attempt)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if attempts:
        actual = max(attempts, key=lambda a: a['created'])
        detail += [('field', (t('Last attempt model'), text_redacted(str(actual.get('model') or '—')))),
                   ('field', (t('Attempt outcome'), str(actual.get('outcome') or 'unknown')))]
    try:
        view = RunStore(home).plan_view(rid)
        if not view['execution_plan_matches']:
            detail.append(('line', t('The execution plan differs from its history. The operation is blocked.')))
        history = view['history']
        answers = {a['decision']['id']: a['answer'] for a in view['answers']}
        if view['decisions']:
            detail[1] = ('field', (t('Waiting reason'), t('Answer recorded, awaiting a new plan') if view['answer_ready'] else
                                  t('Critical decision pending')))
        for assumption in history[-1]['plan'].get('assumptions', []) if history else []:
            detail.append(('field', (t('Recorded assumption'), text_redacted(assumption['choice'] + ' — ' + assumption['reason']))))
        for decision in view['decisions']:
            detail += [('field', (t('Critical decision pending'), text_redacted(decision['question']))),
                       ('field', (t('Reason'), text_redacted(decision['reason'])))]
            if decision['id'] in answers:
                detail.append(('field', (t('Recorded answer'), text_redacted(answers[decision['id']]))))
            elif k.get('state') not in TERMINAL:
                actions.append(d(t('Answer critical decision'), text_redacted(decision['question']),
                                 id='plan-answer-' + decision['id'], action='plan-answer', run=rid,
                                 revision=view['revision'], decision=decision))
        if history:
            review = view['reviews'][-1] if view['reviews'] and view['reviews'][-1].get('revision_number') == view['revision'] else history[-1]['review']
            detail += [('field', (t('Plan revision'), str(view['revision']))),
                       ('field', (t('Plan review'), str(review.get('status') or '—')))]
            actions.append(d(t('Plan history'), t('{n} revisions', n=view['revision']), id='plan-history',
                             file={'round': 'plan-history', 'home': str(home), 'run_id': rid, 'title': t('Plan history')}))
            if k.get('state') not in TERMINAL:
                if not view['held'] and not view['decisions']:
                    actions.append(d(t('Request a plan change'), '', id='plan-change', action='plan-change',
                                     run=rid, revision=view['revision']))
                elif view['held']:
                    change = view['changes'][-1]
                    detail += [('field', (t('Change request'), text_redacted(change['text']))),
                               ('field', (t('Stop / decision'), t({'stop_requested': 'Awaiting stop confirmation', 'stopped': 'Stop verified',
                                                             'withdrawn': 'Go Back was withdrawn, a proceed decision is needed',
                                                             'rejected': 'Plan review rejected',
                                                             'review_pending': 'Fresh plan review waiting'}.get(change['status'], change['status']))))]
                    if change['status'] == 'stop_requested':
                        actions.append(d(t('Verify stop'), '', id='plan-stop', action='plan-stop', run=rid, change_id=change['id']))
                    if change['status'] == 'stopped':
                        actions.append(d(t('Withdraw change'), '', id='plan-withdraw', action='plan-withdraw', run=rid, change_id=change['id']))
                    if (k.get('request') and change['status'] in ('stopped', 'rejected', 'review_pending')
                            and (not view['decisions'] or view['answer_ready'])):
                        actions.append(d(t('Generate / review plan'), '', id='request-plan', action='request-plan', run=rid))
                    if review.get('status') == 'PASS' and not view['decisions']:
                        actions.append(d(t('Record resume decision'), '', id='plan-resume', action='plan-resume', run=rid,
                                         change_id=change['id'], revision=view['revision']))
                    if change.get('stop'):
                        detail.append(('field', (t('Cost at stop'), json.dumps(change['stop']['cost'], ensure_ascii=False))))
                    detail.append(('muted', t('Execution remains blocked until a resume decision and a current plan review.')))
        if not view['held'] and not view['decisions'] and k.get('state') not in ('completed', 'implemented', 'cancelled'):
            from run_store import request_authorized
            if k.get('request') and not request_authorized(k):
                actions.append(d(t('Task-specific call permission'), '', id='request-authorize', action='request-authorize', run=rid))
            else:
                actions.append(d(t('Start / resume'), '', id='start', action='start', run=rid))
            if k.get('request') and dictionary(k.get('planning')).get('status') == 'rejected':
                actions.append(d(t('Generate / review plan'), '', id='request-plan', action='request-plan', run=rid))
            if k.get('spend_alarm'):
                actions.append(d(t('Approve spending alarm'), '', id='spending-approval', action='spend-approval', run=rid))
    except (OSError, ValueError, KeyError, TypeError):
        detail.append(('line', t('Plan history is unreadable. The operation waits.')))
    try:
        from permission import pending
        for action in pending(RunStore(home), rid):
            detail += [('field', (t('Permission decision'), action['identity']['kind'] + ' · ' + action['status'])),
                       ('code', action['display'])]
            if action['status'] in ('pending', 'first-approved'):
                actions.append(d(t('Exact action permission'), action['identity']['kind'], id='operator-' + action['token'],
                                 action='operator-approve', run=rid, token=action['token']))
    except (OSError, ValueError, KeyError, TypeError):
        detail.append(('line', t('Permission record unavailable. The action waits.')))
    return d(t('Decision and operations'), t('Engine record'), detail, actions, id='operation')


def task_node(home: Path, k: dict) -> dict:
    rid = str(k['run_id'])
    directory = run_directory(home, rid)
    first = first_line(home, k)
    plan, project_ref = record_read(directory / 'plan.json'), record_read(directory / 'project_mode.json')
    state_name = str(k.get('state') or '')
    active = state_name not in TERMINAL
    delivery = delivery_doable(k)
    info = delivery_info(k) if state_name in ('completed', 'implemented') else {}
    fr = k.get('final_result') or {}
    local_delivery = dictionary(fr.get('delivery'))
    economy = spend_node(home, k, project_ref)
    if project_ref.get('features'):
        children, percent, stage_label, flow, now, agents_ = project_ref_structure(home, k, project_ref)
        round = t('Large task (in parts)')
        text = t('A large task is split into parts. Each part is a separate run with its own plan, subtasks and test. The judge review happens once at the end, in the Final check.')
        child_prose = dict(TASK_CHILDREN, plan='Parts, each a separate run')
    else:
        sub = sub_task_nodes(home, k, plan)
        test_, judge_ = test_node(home, k), judge_node(home, k)
        children = [plan_node(home, k, plan, sub), test_, judge_]
        percent, stage_label, flow, now = normal_progress(k, sub, test_, judge_, delivery)
        agents_ = agent_nodes(home, k)
        if k.get('mode') == 'report':
            round, text = t('Report task'), t('A report task writes a report file. The test and the judge check the report.')
        else:
            round = t('Task')
            text = t('A task starts with a plan. The planner subtasks are written, then one test and one judge review run. Delivery is your call.')
        child_prose = dict(TASK_CHILDREN)
    if state_name in TERMINAL:
        children.append(outcome_node(home, k))
        children.append(delivery_node(k, delivery, info))
    report = report_node(home, k)
    if report:
        children.append(report)
    running_agent = sum(1 for a in agents_ if a['status'] == 'now_ts')
    children += [d(t('Agents'), t('{n} agents', n=len(agents_)), [('title', t('Agents working on this task')), ('field', (t('Agents'), str(len(agents_)))),
                                                                   ('field', (t('Running now'), str(running_agent)))],
                   agents_, 'now_ts' if running_agent else '', id='agents',
                   description=description(t('Agents'), t('Agents working on this task. Each one with its latest thoughts and tool calls. Press o to read the full record.'))),
                 economy, events_node(home, rid), operation_node(home, k, plan)]
    child_prose['operation'] = 'Waiting, routing, plan history and safe operations'
    end = (k.get('updated') if not active else time.time()) or time.time()
    request = dictionary(k.get('request'))
    if request and active:
        planning = dictionary(k.get('planning'))
        status = planning.get('status')
        if status != 'accepted':
            stage_label = (t('Plan review rejected') if status == 'rejected' else t('Plan review') if status == 'review-pending' else
                     t('Plan') if status == 'generating' else t('Waiting for the planning gate'))
            now = text_redacted(str(k.get('reason') or stage_label))
    from run_store import plan_hold, plan_decisions, plan_answer_ready, run_job_type
    try:
        job_type = run_job_type(k)
    except ValueError:
        job_type = '—'
    if job_type == 'review' and active and dictionary(k.get('planning')).get('status') == 'accepted':
        review_phase = {'review-author': t('Review report'), 'review-evidence': t('Evidence check'),
                        'review-final': t('Final review'), 'review-recheck': t('Final review after repair')}.get(k.get('phase'))
        if review_phase:
            stage_label = now = review_phase
    held = plan_hold(k)
    if held and active:
        status = k['plan_changes'][-1]['status']
        stage_label = t('Awaiting stop confirmation') if status == 'stop_requested' else t('Plan review') if status == 'review_pending' else t('Awaiting plan change')
        now = stage_label
    decision_wait = bool(plan_decisions(k))
    if decision_wait and active:
        stage_label = t('Answer recorded, awaiting a new plan') if plan_answer_ready(k) else t('Critical decision pending')
        now = stage_label
    if project_ref:
        from project_mode import status_view
        project_phase = status_view(k, project_ref).get('stage_label')
        if project_phase and not decision_wait:
            stage_label, now = t(project_phase), t(project_phase) if active else ''
    status_prose = stage_label if (held or request or decision_wait) and active else t(STATUS_NAME.get(state_name, state_name))
    specifics = [('title', first), ('muted', f'{project_ref_name(k)} · {round}'), ('flow', flow), ('field', (t('Status'), status_prose)),
               ('field', (t('Project'), str(k.get('project') or '?'))),
               ('field', (t('Started'), absolute_moment(k.get('created')))),
               ('field', (t('Last updated'), absolute_moment(k.get('updated')))),
               *id_summary(k),
               ('field', (t('Duration'), duration_text(number(end) - number(k.get('created') or end)))), ('field', (t('Stage'), stage_label)),
               ('muted', t('The stage indicator is not a product completion percentage.')), ('field', (t('Spend'), economy['summary']))]
    if request:
        review = next((r for r in reversed(k.get('plan_reviews') or []) if r.get('revision_number') == len(k.get('plan_history') or [])), {})
        explanation = text_redacted(str(k.get('reason') or dictionary(k.get('planning')).get('status') or '—'))
        specifics += [('section', t('Task input')), ('field', (t('Input mode'), t({'goal': 'Plan my goal', 'backlog': 'Existing backlog', 'plan_only': 'Create a plan only'}.get(request.get('input_mode'), 'unknown')))),
                    ('field', (t('Expected output'), text_redacted(str(request.get('output') or '—')))),
                    ('field', (t('Acceptance criteria'), text_redacted(str(request.get('acceptance') or t('To be derived from the original request in the plan'))))),
                    ('field', (t('Narrow acceptance scope'), scope_text(request.get('scope_files'), request.get('scope_tests')))),
                    ('field', (t('Cost preference'), t({'economic': 'Economic', 'balanced': 'Balanced', 'strong': 'Strong'}.get(request.get('preference'), 'unknown')))),
                    ('field', (t('Difficulty override'), t({None: 'Awaiting automatic estimate', 'easy': 'Easy', 'medium': 'Medium', 'hard': 'Hard', 'very_hard': 'Very hard'}.get(request.get('difficulty_override'), 'unknown')))),
                    ('field', (t('Task budget'), str(request.get('budget_usd') or '—') + ' USD · ' + t('Total'))),
                    ('field', (t('Duration'), t('{d} min', d=request.get('duration_minutes') or '—'))),
                    ('field', (t('Plan review'), str(review.get('status') or t('Not performed yet')))), ('line', explanation),
                    ('field', (t('Job type'), job_type)),
                    ('field', (t('Original request'), text_redacted(str(request.get('prompt') or '—'))))]
        plan_status = (t('Verified plan output') if dictionary(k.get('plan_delivery')).get('status') == 'PASS' else
                       t('Plan accepted') if review.get('status') == 'PASS' else
                       t('Plan review rejected') if review.get('status') == 'FAIL' else t('Waiting for the planning gate'))
        input = d(t('Task input'), plan_status, specifics[-12:], id='input', status='finished' if review.get('status') == 'PASS' else 'waiting',
                  file={'round': 'json', 'path': str(directory / 'run.json'), 'title': t('Run record')})
        children.append(input)
        defaults = dictionary(request.get('selection_defaults'))
        if dictionary(k.get('plan_delivery')).get('status') == 'PASS':
            children.append(d(t('Verified plan output'), '', id='plan-delivery',
                             file={'round': 'json', 'path': k['plan_delivery']['artifact'], 'title': t('Verified plan output')}))
        if dictionary(k.get('review_delivery')).get('status') == 'PASS':
            children.append(d(t('Review report'), '', id='review-delivery',
                             file={'round': 'json', 'path': k['review_delivery']['artifact'], 'title': t('Review report')}))
        if local_delivery.get('kind') == 'reviewed-worktree':
            specifics += [('field', (t('Verified local output'), text_redacted(str(local_delivery.get('path') or '—')))),
                        ('muted', t('Git operations require separate explicit permission.'))]
        if defaults:
            detail = [('section', t('Task defaults at submission')),
                      ('field', (t('Cost preference'), t({'economic': 'Economic', 'balanced': 'Balanced', 'strong': 'Strong'}.get(defaults.get('preference'), 'unknown')))),
                      ('field', (t('Difficulty override'), t({None: 'Awaiting automatic estimate', 'easy': 'Easy', 'medium': 'Medium', 'hard': 'Hard', 'very_hard': 'Very hard'}.get(defaults.get('difficulty_override'), 'unknown')))),
                      ('muted', t('Task preference and difficulty determine model order. Planning and permission gates still apply.'))]
            specifics.extend(detail)
            input['detail'].extend(detail)
    if 'model_selection' in k:
        from model_selection import run_selection
        try:
            selection = run_selection(home, rid)
            levels = {'easy': 'Easy', 'medium': 'Medium', 'hard': 'Hard', 'very_hard': 'Very hard'}
            detail = [('section', t('Task model selection')),
                      ('field', (t('Cost preference'), t({'economic': 'Economic', 'balanced': 'Balanced', 'strong': 'Strong'}[selection['preference']]))),
                      ('field', (t('Initial difficulty estimate'), t(levels[selection['difficulty_estimate']]))),
                      ('field', (t('Difficulty override'), t(levels.get(selection['difficulty_override'], 'Absent')))),
                      ('field', (t('Effective difficulty'), t(levels[selection['difficulty_effective']]))),
                      ('field', (t('Estimate reason'), selection['estimate_reason'])),
                      ('field', (t('Selection policy'), selection['policy_version'])),
                      ('muted', t('Task preference and difficulty determine model order. Planning and permission gates still apply.'))]
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            detail = [('section', t('Task model selection')), ('line', t('Model selection could not be read.'))]
        specifics.extend(detail)
        if request:
            input['detail'].extend(detail)
        children.append(d(t('Task model selection'), t('Selection policy'), detail, id='choice'))
        child_prose['choice'] = 'Selection policy'
    if project_ref.get('features'):
        features = [f for f in project_ref['features'] if isinstance(f, dict)]
        count = sum(f.get('status') in ('passed', 'applied') for f in features)
        specifics += [('field', (t('Intermediate implementation'), f'{count}/{len(features)}')), ('muted', t('Feature QA, final acceptance and delivery are separate decisions.'))]
    warnings = []
    if state_name in TERMINAL:
        for gid, task in dictionary(k.get('tasks')).items():
            if dictionary(task).get('state') == 'running':
                warnings.append(t('Active step recorded in a terminal run: {g}', g=f'{gid}=running'))
    child_ids = {str(c['run']) for c in project_ref.get('children') or [] if isinstance(c, dict) and c.get('run')}
    child_ids |= {str(cid) for f in project_ref.get('features') or [] if isinstance(f, dict) and isinstance(f.get('run'), list) for cid in f['run'] if cid}
    for cid in sorted(child_ids):
        sub_k = record_read(run_directory(home, cid) / 'run.json')
        if not sub_k or (state_name in TERMINAL and sub_k.get('state') not in TERMINAL) or (state_name != 'running' and sub_k.get('state') == 'running'):
            warnings.append(t('Parent/child state conflict: {p} · {c}', p=f'{rid}={state_name}', c=f"{cid}={sub_k.get('state') or '?'}"))
    if project_ref and ((project_ref.get('status') in ('stopped', 'stopped', 'fell', 'stuck') and run_enabled(k)) or
                  (project_ref.get('status') == 'running' and state_name in TERMINAL) or
                  (project_ref.get('status') == 'complete' and state_name not in ('completed',))):
        warnings.append(t('Project/run state conflict: {p} · {r}', p=str(project_ref.get('status')), r=state_name))
    if warnings:
        specifics += [('section', t('State conflict'))] + [('line', warning) for warning in warnings]
    if now:
        specifics.append(('field', (t('Now'), now)))
    if k.get('reason'):
        specifics.append(('field', (t('Note'), str(k['reason'])[:200])))
    if k.get('reason') == 'new-project-init-permission-required':
        command = 'cadre init --create ' + str(dictionary(k.get('request')).get('target_path') or '')
        specifics.append(('field', (t('Next step'), t('Run {k} to grant the initial commit permission, then start the task again.', k=command))))
    elif k.get('reason') == 'new-project-registration-mismatch':
        import project_registry
        target = str(dictionary(k.get('request')).get('target_path') or '')
        enrolled = project_registry.enrolled(home, Path(target).resolve()) if target else None
        request_name = str(k.get('project') or '—')
        other = ''
        if enrolled is None and request_name != '—':
            try:
                other = (home / 'projects' / request_name / 'repo-path').read_text().strip()
            except OSError:
                other = ''
        if other:
            specifics.append(('field', (t('Next step'), t('Project name {a} is registered to another folder: {b}. Submit the task under another project name or remove that registration, then restart the task.',
                                                          a=request_name, b=other))))
        else:
            specifics.append(('field', (t('Next step'), t('Project name mismatch: request {a}, registered {b}. Register the folder under the requested name or fix the registration, then start the task again.',
                                                          a=request_name, b=enrolled or t('None')))))
    if not active:
        rv = fr.get('review') or {}
        specifics += [('section', t('Result')), ('field', (t('Test'), str((fr.get('qa') or {}).get('status') or '—'))),
                    ('field', (t('Judge'), f"{rv.get('verdict') or rv.get('status') or '—'} · {model_short(rv.get('model'))}")),
                    ('field', (t('Changes'), change_summary(info.get('short', '')))),
                    ('field', (t('Delivery'), next((c['summary'] for c in children if c.get('id') == 'delivery'), '—')))]
    context = [('title', t('Recent events')), ('record', events(home, rid)), ('title', t('Spend')),
              ('field', (t('Total'), economy['summary'])), ('field', (t('Record'), f'runs/{rid}/'))]
    if state_name == 'running':
        mark = 'now_ts'
    elif state_name == 'failed' or str(fr.get('status') or '').upper() == 'FAIL':
        mark = 'error'
    elif state_name in ('completed', 'implemented'):
        mark = 'finished'
    else:
        mark = 'waiting'
    if held and active:
        specifics += [('field', (t('Stop / decision'), stage_label))]
    summary = f"{status_prose} · {project_ref_name(k)} · {relative_age(k.get('created'))}"
    inside = [(c['entry_name'], t(child_prose[c['id']])) for c in children if c.get('id') in child_prose]
    return d(first, summary, specifics, children, mark, context, id=rid, run=rid, project_ref=k.get('project'),
             delivery=delivery or None, active=active, waiting=active and state_name != 'running',
             input_waiting=bool(request) and active and dictionary(k.get('planning')).get('status') != 'accepted',
             percent=percent, stage_label=stage_label, created=number(k.get('created')), now=now, hour=request_hour(k),
             status_prose=status_prose, age=relative_age(k.get('created')), warnings=warnings,
             description=description(t('This task'), text, inside), file={'round': 'text', 'path': str(k.get('prompt') or directory / 'prompt.md'), 'title': t('Task text')})


def history_node(home: Path, k: dict) -> dict:
    directory = run_directory(home, k['run_id'])
    scope = economy_scope(home, k, record_read(directory / 'project_mode.json'))
    paths = [directory / 'project_mode.json', directory / 'operator-actions', *sorted((directory / 'operator-actions').glob('*.json')),
             *(run_directory(home, rid) / 'run.json' for rid in scope),
             home / 'chain/request-budget.db', home / 'chain/request-budget.db-wal']
    stamps = tuple((str(p), p.stat().st_mtime_ns if p.exists() else 0) for p in paths)
    key = (k['run_id'], str(home.resolve()), k.get('updated'), k.get('delivery_merge'), k.get('delivery_push'), product_trace(k), stamps)
    if key not in FINISHED:
        for old in [a for a in list(FINISHED) if a[0] == k['run_id']]:
            FINISHED.pop(old, None)
        node = task_node(home, k)
        FINISHED[key] = node
    node = FINISHED[key]
    fr = k.get('final_result') or {}
    outcome = {'PASS': t('Passed'), 'FAIL': t('Failed'), 'APPLIED': t('Applied')}.get(str(fr.get('status') or '').upper()) or t(
        STATUS_NAME.get(str(k.get('state') or ''), str(k.get('state') or '—')))
    node['summary'] = f"{outcome} · {project_ref_name(k)} · {relative_age(k.get('created'))}" + (f" · {t('Awaiting delivery')}" if node.get('delivery') else '')
    node.update(status_prose=outcome, age=relative_age(k.get('created')))
    return FINISHED[key]


def ledger_lines(home: Path, query: str, parameter: tuple) -> list:
    db = home / 'chain/request-budget.db'
    if not db.exists():
        return []
    try:
        with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as con:
            return con.execute(query, parameter).fetchall()
    except sqlite3.Error:
        return []


def cash_limit(home: Path | None = None) -> float | None:
    from monthly_budget import cap_micro
    try:
        return cap_micro(home) / 1e6
    except Exception:
        return None


def cash_status(home: Path) -> dict:
    from monthly_budget import PROVIDERS, MonthlyLedger, period
    limit = cash_limit(home)
    db = home / 'chain/monthly-budget.db'
    spent = pending = 0.0
    known = True
    shares: dict[str, float] = {}
    if db.exists():
        try:
            with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as con:
                h, a = MonthlyLedger.usage(con, period())
                marks = ','.join('?' * len(PROVIDERS))
                for provider, amount in con.execute(f'select provider, coalesce(sum(actual),0) from reservation where period=? and provider in '
                                                    f'({marks}) and actual is not null group by provider', (period(), *sorted(PROVIDERS))):
                    shares[provider] = amount / 1e6
            spent, pending = h / 1e6, a / 1e6
        except sqlite3.Error:
            known = False
    return {'limit': limit, 'spent': spent, 'pending': pending, 'shares': shares, 'period': period(), 'known': known}


def measurement_prose(view: tuple[dict, float]) -> str:
    g, age = view
    if not g or age >= 10 ** 8:
        return t('No measurement')
    return t('Measured just now') if age < 60 else t('Measured {n} min ago', n=int(age // 60))


def measurement_fresh(view: tuple[dict, float]) -> bool:
    g, age = view
    return bool(g) and age <= 90 and g.get('today') == time.strftime('%Y-%m-%d')


def budget_view(home: Path) -> tuple[dict, float]:
    try:
        g, age = at_end('budget')(home)
    except Exception:
        return {}, 1e9
    return (g, number(age)) if isinstance(g, dict) and g else ({}, 1e9)


def go_history(home: Path) -> list[dict]:
    return [g for g in _read(home / 'chain/go-window.json').get('history') or [] if isinstance(g, dict)][-24:]


def or_daily(home: Path) -> list[tuple[str, float, int]]:
    lines = ledger_lines(home, "select date(created, 'unixepoch', 'localtime'), coalesce(sum(actual),0), count(*) from reservation "
                                      "where provider='openrouter' and actual is not null and created >= ? group by 1 order by 1",
                                (time.time() - 7 * 86400,))
    return [(str(day), number(amount) / 1e6, int(count)) for day, amount, count in lines]


def budget_section(home: Path, records: list[dict], view: tuple[dict, float]) -> dict:
    g, age = view
    go, orr, zen, cl = (dictionary(g.get(x)) for x in ('go', 'or', 'zen', 'claude'))
    own = dictionary(go.get('own'))
    zero = dictionary(_read(home / 'chain/go-window.json').get('resets'))
    measurement = measurement_prose(view)

    def percent(x: Any) -> str:
        return ratio(x) if g and x is not None else '—'

    def amount(x: Any) -> str:
        return money(x) if g and x is not None else '—'

    def bar(entry_name: str, x: Any) -> tuple:
        return ('bar', (entry_name, number(x))) if g and x is not None else ('field', (entry_name, '—'))
    cash = cash_status(home)

    def cash_amount(value: float) -> str:
        return money(value) if cash['known'] else t('Unknown')

    limit = cash['limit']
    limit_prose = money(limit) if limit is not None else t('Invalid')
    spending: dict[str, float] = {}
    for k in records:
        spending[project_ref_name(k)] = spending.get(project_ref_name(k), 0.0) + number(k.get('budget_spent_usd'))
    context = [('title', t('Top spenders'))] + ([('field', (p[:16], money(v))) for p, v in sorted(spending.items(), key=lambda x: -x[1])[:6] if v > 0]
                                                 or [('muted', t('No spending'))]) + [('muted', measurement)]
    history = go_history(home)
    trend = [('section', t('Trend')), ('field', (t('Monthly'), trend_prose([number(x.get('monthly')) for x in history]))),
               ('field', (t('Weekly'), trend_prose([number(x.get('weekly')) for x in history]))),
               ('field', (t('5 hour'), trend_prose([number(x.get('rolling')) for x in history]))),
               ('section', t('Recent measurements')), ('record', [f"{hour(x.get('ts'))}   5h {ratio(x.get('rolling'))}   {t('Week')} {ratio(x.get('weekly'))}   "
                                                      f"{t('Month')} {ratio(x.get('monthly'))}" for x in reversed(history[-6:])])] if history else []

    def window(entry_name: str, key: str, id: str) -> dict:
        y = own.get(key)
        return d(t(entry_name), percent(y), [('title', t(entry_name)), bar(t('Usage'), y), ('field', (t('Resets'), iso_local(zero.get(key)))),
                                 ('section', t('Trend')), ('field', (t('Recent measurements'), trend_prose([number(x.get(key)) for x in history])))],
                 context=context, id=id)

    def cl_window(entry_name: str, key: str, id: str) -> dict:
        y = cl.get(key)
        return d(t(entry_name), percent(y), [('title', t(entry_name)), bar(t('Usage'), y), ('field', (t('Cap'), percent(cl.get('cap'))))],
                 context=context, id=id)
    last_or = ledger_lines(home, "select created, json_extract(metadata,'$.model'), json_extract(metadata,'$.task_id'), actual from reservation "
                                    "where provider='openrouter' and actual is not null and actual > 0 order by created desc limit 10", ())
    daily = or_daily(home)
    or_remaining = orr.get('remaining')
    go_monthly = own.get('monthly')
    go_remaining = go.get('available')
    go_least = go_remaining is not None and number(go_remaining) < 10
    or_least = orr.get('available') is not None and number(orr.get('available')) < 5
    claude_prose = t('Off') if claude_closed() else t('Weekly {y}', y=percent(cl.get('used7')))
    shares = cash['shares']
    seven_day = [('section', t('Last 7 days')), ('field', (t('Trend'), graph([a for _, a, _ in daily])))] + [
        ('field', (f'{day[8:10]}.{day[5:7]}', t('{p} · {n} requests', p=money(a), n=n))) for day, a, n in daily] if daily else []
    child = [
        d(t('Go'), t('Monthly {y}', y=percent(go_monthly)), [
            ('title', t('Go (subscription)')), bar(t('Monthly'), go_monthly), ('field', (t('Weekly'), percent(own.get('weekly')))),
            ('field', (t('5 hour'), percent(own.get('rolling')))),
            ('field', (t('Left for cadre'), t('{n} points', n=f'{number(go_remaining):.0f}') if go_remaining is not None else '—')),
            ('field', (t('Window'), '—' if not go else (t('On') if go.get('window_open', True) else t('Closed · {n}', n=go.get('window_reason') or '—')))),
            ('field', (t('Monthly reset'), iso_local(zero.get('monthly')))), ('muted', measurement)] + trend,
          [window('5 hour', 'rolling', 'go-5h'), window('Weekly', 'weekly', 'go-week'), window('Monthly', 'monthly', 'go-month')],
          'error' if go_least else ('finished' if go else ''), context, id='go',
          description=description(t('Go'), t('Monthly subscription. Usage is measured in points. Windows reopen when they reset.'),
                            [(t('5 hour'), t('Short window')), (t('Weekly'), t('Weekly window')), (t('Monthly'), t('Monthly window'))])),
        d(t('OpenRouter'), amount(or_remaining), [
            ('title', t('OpenRouter')), ('field', (t('Balance'), amount(or_remaining))), ('field', (t('Usable'), amount(orr.get('available')))),
            ('field', (t('This month'), cash_amount(shares.get('openrouter', 0)))), ('muted', measurement)] + seven_day, [
            d(t('Balance'), amount(or_remaining), [('title', t('Balance')), ('field', (t('Now'), amount(or_remaining))),
                                            ('field', (t('Usable'), amount(orr.get('available')))), ('muted', measurement)], context=context, id='or-balance'),
            d(t('This month'), cash_amount(shares.get('openrouter', 0)), [('title', t('This month')), ('field', (t('Spent'), cash_amount(shares.get('openrouter', 0)))),
                                                              ('field', (t('Period'), cash['period']))] + seven_day, context=context, id='or-month'),
            d(t('Recent spending'), t('{n} requests', n=len(last_or)), [('title', t('Recent spending')), ('record', [
                f"{hour(c)} {model_short(m):<18} {str(g_ or '-'):<8} {money((a or 0) / 1e6)}" for c, m, g_, a in last_or])], context=context, id='or-last')],
          'error' if or_least else ('finished' if orr else ''), context, id='openrouter',
          description=description(t('OpenRouter'), t('Prepaid balance. Spending also counts against the monthly cash cap.'),
                            [(t('Balance'), t('Current balance')), (t('This month'), t('Spending this month and the last 7 days')), (t('Recent spending'), t('Last 10 requests'))])),
        d(t('Monthly cash cap'), f"{cash_amount(cash['spent'])} / {limit_prose}", [
            ('title', t('Monthly cash cap')),
            ('bar', (t('This month'), 100 * (cash['spent'] + cash['pending']) / limit)) if limit and cash['known'] else ('field', (t('This month'), '—')),
            ('field', (t('Spent'), cash_amount(cash['spent']))), ('field', (t('Held'), cash_amount(cash['pending']))),
            ('field', (t('Cap'), limit_prose)),
            ('field', (t('Left'), cash_amount(max(0.0, limit - cash['spent'] - cash['pending'])) if limit is not None else '—')),
            ('field', (t('Period'), cash['period']))], [
            d(t('OpenRouter share'), cash_amount(shares.get('openrouter', 0)), [('title', t('OpenRouter share')),
                                                                        ('field', (t('Spent'), cash_amount(shares.get('openrouter', 0))))], context=context, id='cash-or'),
            d(t('Zen share'), cash_amount(shares.get('opencode', 0)), [('title', t('Zen share')), ('field', (t('Spent'), cash_amount(shares.get('opencode', 0))))],
              context=context, id='cash-zen')], context=context, id='cash',
          description=description(t('Monthly cash cap'), t('OpenRouter and Zen spend at most this much this month. Go and Claude are subscriptions, so they do not count.'),
                            [(t('OpenRouter share'), t('OpenRouter this month')), (t('Zen share'), t('Zen this month'))])),
        d(t('Zen'), amount(zen.get('remaining')), [
            ('title', t('Zen (backup)')), ('field', (t('Balance'), amount(zen.get('remaining')))), ('field', (t('This month'), cash_amount(shares.get('opencode', 0))))], [
            d(t('Balance'), amount(zen.get('remaining')), [('title', t('Balance')), ('field', (t('Now'), amount(zen.get('remaining'))))], context=context, id='zen-balance'),
            d(t('When it is used'), t('Backup'), [('title', t('When it is used')),
                                                     ('line', t('It is the last link of the chain. Its spend counts against the monthly cash cap.'))],
              context=context, id='zen-usage')], context=context, id='zen',
          description=description(t('Zen'), t('Prepaid backup. Used only when OpenRouter and Go cannot be used.'))),
        d(t('Claude'), claude_prose, [
            ('title', t('Claude')), ('field', (t('Status'), t('Off inside cadre') if claude_closed() else t('On inside cadre'))),
            ('field', (t('Weekly'), percent(cl.get('used7')))), ('field', (t('5 hour'), percent(cl.get('used5')))),
            ('field', (t('Cap'), percent(cl.get('cap'))))],
          [cl_window('5 hour', 'used5', 'claude-5h'), cl_window('Weekly', 'used7', 'claude-week')], context=context, id='claude',
          description=description(t('Claude'), t('Subscription. If enabled inside cadre it heads the planning chain.'))),
    ]
    detail = [('title', t('Budget')), ('field', (t('Go monthly'), percent(go_monthly))), ('field', (t('OpenRouter'), amount(or_remaining))),
               ('field', (t('Cash this month'), f"{cash_amount(cash['spent'])} / {limit_prose}")), ('field', (t('Zen'), amount(zen.get('remaining')))),
               ('field', (t('Claude'), claude_prose)), ('muted', measurement)] + seven_day
    return d(t('Budget'), t('OR {p}', p=amount(or_remaining)) if g else t('No measurement'), detail, child, context=context, id='budget',
             description=description(t('Budget'), t('Money and quota. Go subscription, OpenRouter balance, monthly cash cap, Zen backup and Claude quota.'),
                               [(t('Go'), t('Monthly subscription, measured in points')), (t('OpenRouter'), t('Prepaid balance')),
                                (t('Monthly cash cap'), t('Monthly cap for OpenRouter and Zen')), (t('Zen'), t('Backup balance')),
                                (t('Claude'), t('Subscription quota'))]))


def catalog_info(model: str) -> tuple[str, str, str]:
    import model_catalog
    import model_router
    provider = model_router.provider(model)
    entry_name = (model.split('/', 1)[1] if '/' in model else model).split('#')[0]
    context = model_catalog.context(provider, entry_name)
    price = model_catalog.price(provider, entry_name)
    if provider == 'claude':
        price_prose = t('Counts against the subscription quota')
    elif price and ('input' in price or 'output' in price):
        price_prose = t('Input {g} · output {c} / 1M tokens', g=money(price.get('input')), c=money(price.get('output')))
        if provider == 'opencode-go':
            price_prose += ' · ' + t('List price, counted in Go points')
        elif provider == 'groq':
            price_prose += ' · ' + t('Not paid on the free tier')
    else:
        price_prose = t('Not in catalog')
    return t(PROVIDER_NAME.get(provider, provider)), token_prose(context) if context else t('Not in catalog'), price_prose


def agent_section(home: Path, view: tuple[dict, float]) -> dict:
    import copy

    import model_router
    b = view[0]
    measurement = measurement_prose(view)
    fresh = measurement_fresh(view)
    wait = (t('Stale measurement'), t('The budget measurement is stale, the order is computed with a new measurement')) if b else (t('No measurement'), t('No budget measurement, the order could not be computed'))
    today = time.mktime(time.strptime(time.strftime('%Y-%m-%d'), '%Y-%m-%d'))
    counter = {str(m): (n, a, last) for m, n, a, last in ledger_lines(
        home, "select json_extract(metadata,'$.model'), count(*), coalesce(sum(actual),0), max(created) from reservation "
              "where created >= ? group by 1", (today,))}
    try:
        failure = dictionary(at_end('failure')())
    except Exception:
        failure = {}
    closed = claude_closed()
    child = []
    for role, (role_name, role_text) in ROLE_INFO.items():
        candidates = list(dict.fromkeys(str(a) for a in model_router.ROLES.get(role, [])))
        if not candidates:
            continue
        chain, reasons = at_end('chain')(role, b, copy.deepcopy(failure)) if fresh else ([], [])
        chain, reasons = list(chain), [str(x) for x in reasons]
        rings = []
        for i, candidate in enumerate(candidates, 1):
            options = candidate.split('|')
            m = next((x for x in options if x in chain), options[0])
            visible = m if m in chain else ' | '.join(options)
            short = m.split('/', 1)[-1].split('#')[0]
            n, a, last = counter.get(short) or counter.get(m) or (0, 0, 0)
            error = dictionary(failure.get(m) or failure.get(short))
            if not fresh:
                status, (state_view, reason) = 'waiting', wait
            elif m in chain:
                order = chain.index(m)
                status, state_view = ('now_ts' if order == 0 else 'finished'), (t('First choice') if order == 0 else t('Position {n}', n=order + 1))
                reason = t('In the chain, share and context fit')
            elif closed and all(x.startswith('claude/') for x in options):
                status, state_view, reason = 'error', t('Eliminated'), t('Claude is off inside cadre')
            else:
                status, state_view = 'error', t('Eliminated')
                reason = next((x.split(':', 1)[1].strip() for x in reversed(reasons) for y in options if x.startswith(y + ':')),
                             t('Not eligible for this call'))
            provider, context, price = catalog_info(m)
            choice = [('field', (t('Options'), t('{a} if the Claude share is 4 points or more, otherwise {b}', a=options[0], b=options[1])))] \
                if len(options) == 2 else []
            rings.append(d(f'{i}. {visible}', state_view, [
                ('title', visible), ('field', (t('Status'), state_view)), ('field', (t('Reason'), reason))] + choice + [
                ('field', (t('Provider'), provider)), ('field', (t('Context'), context)), ('field', (t('Price'), price)),
                ('field', (t('Last used'), hour(last))), ('field', (t('Requests today'), str(n))), ('field', (t('Spend today'), money(number(a) / 1e6))),
                ('field', (t('Errors'), str(error.get('error', 0)))), ('field', (t('Last error'), str(error.get('last') or '—')[:120]))],
                status=status, id=candidate))
        order_list = chain if fresh else [a.replace('|', ' | ') for a in candidates]
        if fresh:
            summary, first = (model_short(chain[0]), chain[0]) if chain else (t('Chain empty'), t('None, waits for budget'))
        else:
            summary = first = wait[0]
        child.append(d(t(role_name), summary, [
            ('title', t(role_name)), ('field', (t('First choice'), first)),
            ('field', (t('Usable links'), f'{len(chain)} / {len(candidates)}' if fresh else '—')), ('muted', measurement), ('section', t('Chain order'))] +
            [('line', f'{j}. {m}') for j, m in enumerate(order_list, 1)], rings, ('now_ts' if chain else 'error') if fresh else 'waiting',
            [('title', t('Chain order')), ('line', t('Tried top to bottom. A link that is out of share or down is skipped.'))] +
            [('muted', f'{j}. {m}') for j, m in enumerate(order_list, 1)], id=role,
            description=description(t(role_name), t(role_text), extra=[('muted', t('The chain is tried top to bottom. A link that is out of share or down is skipped.'))])))
    light = next((node for node in child if node['id'] == 'impl-light'), None)
    developer = next((node for node in child if node['id'] == 'impl'), None)
    if light and developer:
        light['entry_name'] = t('Lightweight development')
        developer['child'].append(light)
    child = [node for node in child if node['id'] in ('plan', 'impl', 'quality')]
    child.append(on_evaluation_node(home))
    most = sorted(counter.items(), key=lambda x: -x[1][0])[:6]
    detail = [('title', t('Agents'))] + [('field', (c['entry_name'], c['summary'])) for c in child] + [('muted', t('First choice, the model this role uses now')),
                                                                                           ('muted', measurement)]
    detail += outcome_measurement_node(home)['detail'][1:]
    return d(t('Agents'), t('{n} roles', n=len(child)), detail, child, id='agents',
             description=description(t('Agents'), t('Which model does which job. Each role with its model order, eliminated links and why, and usage today. Refreshed every minute.'),
                               [(c['entry_name'], t(ROLE_INFO[c['id']][1]) if c['id'] in ROLE_INFO else t('Classifies the task by difficulty.')) for c in child]),
             context=[('title', t('Most used today'))] + ([('field', (model_short(m)[:16], t('{n} requests · {p}', n=n, p=money(a / 1e6))))
                                                                    for m, (n, a, _) in most] or [('muted', t('No requests today'))]))


def outcome_measurement_node(home: Path) -> dict:
    """CR-39: outcome measures written from real acceptance decisions; does not change the selection, missing data stays unknown."""
    from model_selection import outcome_summary
    try:
        summary = outcome_summary(home)
    except (OSError, ValueError, TypeError):
        return d(t('Outcome measurements'), t('Unreadable'), [('title', t('Outcome measurements')), ('line', t('Measurement records could not be read.'))], status='error', id='outcome-measurement')
    lines = []
    for g in summary['groups']:
        expense = money(g['expense_micro'] / 1e6) + (f" (+{g['expense_unknown']} {t('Unknown')})" if g['expense_unknown'] else '')
        lines.append(('field', (f"{g['role']} · {g['model']}", t('{k} accepted / {n} samples · {t} fresh · {m}', k=g['acceptance'], n=g['sample'], t=g['fresh'], m=expense)
                                  + (' · ' + ', '.join(g['uncertainty']) if g['uncertainty'] else ''))))
    detail = [('title', t('Outcome measurements')), ('line', t('Written from real acceptance decisions and never reorders selection. Policy changes are explicit configuration decisions.')),
               ('field', (t('Record'), t('{n} samples · {b} malformed', n=summary['total'], b=summary['broken']))),
               ('field', (t('Policy'), ', '.join(summary['policies']) or '—'))] + (lines or [('muted', t('No measurements yet.'))])
    return d(t('Outcome measurements'), t('{n} samples', n=summary['total']), detail, id='outcome-measurement',
             description=description(t('Outcome measurements'), t('Acceptance, failure, cost and duration samples per role/model/policy. Few or stale samples are flagged as uncertain.')))


def on_evaluation_node(home: Path) -> dict:
    from budget_gate import DAILY_REQUEST
    from prefilter import MODEL
    model = '/'.join(MODEL)
    try:
        day, count = (home / 'chain/groq-day-counter.txt').read_text().split()[:2]
        today = int(count) if day == time.strftime('%Y-%m-%d') else 0
    except (OSError, ValueError):
        today = 0
    provider, context, price = catalog_info(model)
    return d(t('Pre-screener'), model_short(model), [
        ('title', t('Pre-screener')), ('field', (t('Model'), model)), ('field', (t('Provider'), provider)), ('field', (t('Context'), context)),
        ('field', (t('Price'), price)), ('field', (t('Daily limit'), t('{n} requests', n=DAILY_REQUEST.get('groq', '—')))),
        ('field', (t('Today'), t('{n} requests', n=today))), ('section', t('What it does')),
        ('line', t('When a task arrives it classifies it as SIMPLE, DISTRIBUTED or PREMIUM. It spends no money.'))], status='finished', id='on-evaluation')


def setting_list(home: Path | None = None) -> list[tuple]:
    from budget_gate import ALARM_WINDOW_SEC
    from run_store import DISK_MIN_MB_DEFAULT

    def environment(entry_name: str, fallback: str) -> tuple[str, str]:
        return (os.environ.get(entry_name) or fallback, t('Environment variable') if os.environ.get(entry_name) else t('Default'))
    cap = t('Per project')
    parallel, p_source = environment('CADRE_PARALLEL_LANE', '2')
    disk, d_source = environment('CADRE_DISK_MIN_MB', str(DISK_MIN_MB_DEFAULT))
    window = int(ALARM_WINDOW_SEC // 60)
    limit = cash_limit(home)
    return [
        ('upper-limit', t('Cap'), cap, t('Environment variable') if os.environ.get('CADRE_RUN_CAP_USD') else t('Default'),
         t('Initial cap for new runs. Existing run records are unchanged.'),
         t('Press Enter to select a registered project.'), 'CADRE_RUN_CAP_USD'),
        ('cash', t('Monthly cash cap'), money(limit) if limit is not None else t('Invalid'), t('Environment variable') if os.environ.get('CADRE_MONTHLY_CAP_USD') else t('Default'),
         t('OpenRouter and Zen spend at most this much this month.'), t('Press Enter to edit. Environment overrides the file.'), 'CADRE_MONTHLY_CAP_USD'),
        ('alarm', t('Spend alarm'), t('Off') if os.environ.get('CADRE_SPEND_ALARM') == '0' else t('On'),
         t('Environment variable') if os.environ.get('CADRE_SPEND_ALARM') else t('Default'),
         t('Shows high spending rates over {d} minutes as warnings. An explicit alarm awaits separate approval.', d=window),
         t('CADRE_SPEND_ALARM=0 turns it off'), 'cadre spend-approval <run_id>'),
        ('parallel', t('Parallel coding'), parallel, p_source, t('Independent subtasks are written at the same time.'), 'CADRE_PARALLEL_LANE',
         'CADRE_PARALLEL_LANE=1'),
        ('claude', t('Claude'), t('Off') if claude_closed() else t('On'), t('chain/claude-closed file'),
         t('Whether Claude joins the planning chain.'), t('Deleting the file turns it on, creating it turns it off'), 'touch chain/claude-closed'),
        ('delivery', t('Delivery'), t('Two confirmations'), t('Rule'), t('Push and merge show the command first, then ask for a second confirmation.'), t('Fixed'), '—'),
        ('disk', t('Disk threshold'), f'{disk} MB', d_source, t('No new task starts if free disk drops below this.'), 'CADRE_DISK_MIN_MB',
         'CADRE_DISK_MIN_MB=4096'),
    ]


def budget_setting_node(home: Path, round: str, project_ref: str = '') -> dict:
    from run_store import budget_option
    entry_name = project_ref if round == 'run' else t('Monthly cash cap')
    try:
        state = budget_option(home, round, project_ref)
    except (ValueError, OSError):
        return d(entry_name, t('Invalid'), [('title', entry_name), ('field', (t('Value'), t('Invalid'))),
                 ('muted', t('Budget setting unreadable or invalid. Paid work must wait.'))],
                 status='error', id='cash' if round == 'monthly' else f'budget-setting-{round}-{project_ref}')
    source = {'environment': t('Environment variable'), 'file': t('File'), 'fallback': t('Default')}[state['source']]
    return d(entry_name, money(state['value']), [('title', entry_name), ('field', (t('Value'), money(state['value']))), ('field', (t('Exact value (USD)'), str(state['value']))),
             ('field', (t('Source'), source)), ('line', t('Initial cap for new runs. Existing run records are unchanged.') if round == 'run'
              else t('Shared monthly cash cap. Spent and held amounts are preserved.')),
             ('line', t('Environment override active: editing the file will not change this value.') if state['source'] == 'environment' else t('Press Enter to edit. Environment overrides the file.')),
             ('section', t('Technical details')), ('field', (t('File'), state['path'])), ('field', (t('Environment variable'), state['variable']))],
             id='cash' if round == 'monthly' else f'budget-setting-{round}-{project_ref}', action='budget-setting' if state['source'] != 'environment' else '', budget_setting=state)


def setting_section(home: Path | None = None) -> dict:
    home = home or home_dir()
    try:
        child = [d(entry_name, value[:24], [('title', entry_name), ('field', (t('Value'), value)), ('field', (t('Source'), source)), ('field', (t('What it does'), purpose)),
                                    ('field', (t('How to change'), how)), ('field', (t('Example'), sample))],
                   context=[('title', t('How to change')), ('line', how), ('muted', t('Details: engine/USAGE.md'))], id=id)
                 for id, entry_name, value, source, purpose, how, sample in setting_list(home)]
        child[1] = budget_setting_node(home, 'monthly')
        child[0]['child'] = [budget_setting_node(home, 'run', p) for p in projects(home)]
        child[0]['detail'].append(('muted', t('Plan estimates do not raise the fixed operator total. Internal allocations transfer within that total.')))
        if not child[0]['child']:
            child[0]['detail'].append(('muted', t('No registered project.')))
    except Exception as exc:
        child = [d(t('System settings'), t('Unreadable'), [('title', t('System settings')), ('muted', t('Unreadable: {e}', e=error_prose(exc)))],
                   status='error', id='setting-error')]
    color = ui_messages.color_read()
    child.append(d(t('Color palette'), color, [('title', t('Color palette')), ('field', (t('Value'), color)),
                                             ('field', (t('Options'), ', '.join(ui_messages.COLORS))),
                                             ('field', (t('Source'), t('panel-color file or CADRE_COLOR'))),
                                             ('field', (t('What it does'), t('The interface colors.'))),
                                             ('field', (t('How to change'), t('Enter switches to the next palette.')))],
                   id='color', action='color'))
    child.append(d(t('Show test tasks'), t('On') if ui_messages.tests_read(home) else t('Off'), [
        ('title', t('Test tasks')), ('field', (t('Source'), t('panel-tests file'))),
        ('line', t('Shows test runs in task and event lists. Excludes them from product success summaries.')),
        ('line', t('Enter to show or hide.'))], id='tests', action='tests'))
    from run_store import task_fallbacks
    try:
        state = task_fallbacks(home)
        preference = {'economic': 'Economic', 'balanced': 'Balanced', 'strong': 'Strong'}[state['preference']]
        difficulty = {None: 'Automatic estimate pending', 'easy': 'Easy', 'medium': 'Medium', 'hard': 'Hard', 'very_hard': 'Very hard'}[state['difficulty_override']]
        child.append(d(t('Cost preference and difficulty'), preference + ' · ' + difficulty, [
            ('title', t('New task defaults')), ('field', (t('Cost preference'), preference)), ('field', (t('Difficulty override'), difficulty)),
            ('field', (t('Source'), t('File') if state['source'] == 'file' else t('Default'))),
            ('line', t('Fills new task forms. Existing tasks, permissions and budgets stay unchanged.')),
            ('line', t('Task preference and difficulty determine model order. Planning and permission gates still apply.')),
            ('line', t('Enter to edit.')), ('section', t('Technical details')), ('field', (t('File'), state['path']))],
            id='preference', action='task-fallbacks', fallback_state=state))
    except (ValueError, OSError):
        child.append(d(t('Cost preference and difficulty'), t('Unreadable'), [('title', t('New task defaults')),
            ('line', t('Task defaults cannot be read. Check the settings.'))], id='preference', status='error'))
    detail = [('title', t('Settings'))] + [('field', (c['entry_name'], c['summary'])) for c in child]
    return d(t('Settings'), t('{n} settings', n=len(child)), detail, child, id='settings',
             description=description(t('Settings'), t('Edit money caps, palette and test visibility with Enter. Source paths are in the details.')),
             context=[('title', t('Where it is read from')), ('line', t('Environment variable first, then project file, else the default.')),
                     ('muted', t('Details: engine/USAGE.md'))])


def run_warnings(home: Path, k: dict) -> list[dict]:
    rid = str(k['run_id'])
    warnings = []
    warning = dictionary(k.get('spend_warning'))
    if warning.get('kind') == 'speed-warning' and 0 <= time.time() - number(warning.get('ts')) <= 600:
        warnings.append(d(t('Spending rate warning · {p}', p=project_ref_name(k)), t('Warning'), spend_warning_detail(k),
                         id=f'speed-{rid}', git=['tasks', rid], git_prose=t('Opens the task')))
    alarm = dictionary(k.get('spend_alarm'))
    if alarm and number(alarm.get('ts')) > number(k.get('spend_approval_ts')):
        warnings.append(d(t('Spend alarm · {p}', p=project_ref_name(k)), t('Awaiting approval'), [
            ('title', t('Spend alarm')), ('field', (t('Reason'), str(alarm.get('reason') or ''))), ('field', (t('Approve'), f'cadre spend-approval {rid}')),
            ('muted', t('Enter opens the task.'))], status='error', id=f'alarm-{rid}', git=['tasks', rid], git_prose=t('Opens the task')))
    if k.get('state') == 'retryable' and str(k.get('reason') or '').startswith('budget-wait'):
        warnings.append(d(t('Waiting for budget · {p}', p=project_ref_name(k)), t('Waiting'), [
            ('title', t('Waiting for budget')), ('field', (t('Note'), str(k.get('reason'))[:160])), ('muted', t('It resumes on its own when budget returns.'))],
            status='waiting', id=f'budget-{rid}', git=['tasks', rid], git_prose=t('Opens the task')))
    features = record_read(run_directory(home, rid) / 'project_mode.json').get('features')
    stuck = [f for f in (features if isinstance(features, list) else []) if isinstance(f, dict) and f.get('status') == 'stuck']
    if stuck:
        warnings.append(d(t('Stuck part · {p}', p=project_ref_name(k)), t('{n} parts', n=len(stuck)), [
            ('title', t('Stuck part')), ('field', (t('Parts'), ', '.join(f"{f.get('id')} {f.get('title', '')}" for f in stuck)[:200])),
            ('muted', t('The final check retries these parts. If that fails the task fails.'))], status='error', id=f'stuck-{rid}',
            git=['tasks', rid], git_prose=t('Opens the task')))
    return warnings


def attention_node(home: Path, parent: list[dict], finished: list[dict], view: tuple[dict, float]) -> dict:
    warnings = []
    delivery = [b for b in finished if b.get('delivery')]
    if delivery:
        warnings.append(d(t('Tasks awaiting delivery'), t('{n} tasks', n=len(delivery)), [('title', t('Tasks awaiting delivery')),
                                                                                       ('field', (t('Tasks'), str(len(delivery))))],
                          delivery, 'waiting', id='attention-delivery',
                          description=description(t('Tasks awaiting delivery'), t('Finished and passed. Push or merge is your call. Go inside and press p or m.'))))
    falling = [b for b in finished if b['status'] == 'error' and time.time() - b.get('created', 0) < 86400]
    if falling:
        warnings.append(d(t('Failed tasks (24 hours)'), t('{n} tasks', n=len(falling)), [('title', t('Failed tasks')),
                                                                                      ('field', (t('Tasks'), str(len(falling))))],
                          falling, 'error', id='attention-falling',
                          description=description(t('Failed tasks'), t('Tasks that failed in the last 24 hours. Go inside to see why.'))))
    for k in parent:
        if k.get('state') in TERMINAL:
            continue
        try:
            warnings += run_warnings(home, k)
        except Exception:
            continue
    g, _ = view
    go, orr = dictionary(g.get('go')), dictionary(g.get('or'))
    if go.get('available') is not None and number(go.get('available')) < 10:
        warnings.append(d(t('Go running out this month'), t('{n} points left', n=f"{number(go.get('available')):.0f}"), [
            ('title', t('Go is almost used up this month')), ('field', (t('Left'), t('{n} points', n=f"{number(go.get('available')):.0f}"))),
            ('muted', t('Tasks continue on OpenRouter.'))], status='error', id='attention-go', git=['budget', 'go'], git_prose=t('Opens Budget › Go')))
    if orr.get('available') is not None and number(orr.get('available')) < 5:
        warnings.append(d(t('OpenRouter balance low'), money(orr.get('available')), [
            ('title', t('OpenRouter balance low')), ('field', (t('Usable'), money(orr.get('available'))))],
            status='error', id='attention-or', git=['budget', 'openrouter'], git_prose=t('Opens Budget › OpenRouter')))
    possible = [t('Task awaiting delivery'), t('Task failed in the last 24 hours'), t('Spend alarm awaiting approval'), t('Task waiting for budget'),
             t('Stuck part'), t('Warning when Go points or OpenRouter balance run low')]
    return d(t('Attention'), t('{n} items', n=len(warnings)) if warnings else t('None'),
             [('title', t('Attention'))] + ([('line', f"{u['entry_name']}  ·  {u['summary']}") for u in warnings] or [('muted', t('Nothing needs you'))]),
             warnings, 'error' if warnings else '', id='attention',
             description=description(t('Attention'), t('Things that need your look or your decision.'), extra=[('section', t('What can show up here')), ('record', possible)]))


def summary_node(home: Path, parent: list[dict]) -> dict:
    this_day = time.strftime('%Y-%m-%d')
    weekly = time.time() - 7 * 86400

    def summarize(label: str, choice: Callable[[dict], bool]) -> tuple[int, list]:
        selected = [k for k in parent if choice(k)]
        terminal = [k for k in selected if k.get('state') in TERMINAL]
        passed = sum(k.get('state') == 'completed' and str(dictionary(k.get('final_result')).get('status') or '').upper() == 'PASS' for k in terminal)
        failed = sum(k.get('state') == 'failed' for k in terminal)
        ids = {rid for k in selected for rid in economy_scope(home, k, record_read(run_directory(home, k['run_id']) / 'project_mode.json'))}
        totals = economy_sum(economy_read(home, sorted(ids)))
        return len(terminal), [('section', t(label)), ('field', (t('Finished'), str(len(terminal)))), ('field', (t('Passed'), str(passed))),
            ('field', (t('Failed'), str(failed))), ('field', (t('Open'), str(len(selected) - len(terminal)))),
            ('field', (t('Success'), f'{passed}/{len(terminal)} · {ratio(100 * passed / len(terminal))}' if terminal else t('Unknown'))),
            ('field', (t('Spend'), money_known(totals['spent']))), ('field', (t('Held'), money_known(totals['held'])))]
    count, today = summarize('Today', lambda k: moment_prose('%Y-%m-%d', k.get('updated')) == this_day)
    _, week = summarize('Last 7 days', lambda k: number(k.get('updated')) >= weekly)
    return d(t('Summary'), t('Today {n} tasks', n=count), [
        ('title', t('Summary')), ('line', t('Sample: product jobs updated in this period. Success: final acceptance PASS / terminal parent runs.')),
        ('muted', t('Tests and children are excluded. Retries in one run count as one job. A new acceptance is a separate job.')),
        ('muted', t('Cost: cumulative for these jobs, including children and all attempts. Includes open jobs and held amounts.'))] + today + week,
        id='summary', git=['tasks', 'history'], git_prose=t('Opens History'))


def unreadable_task(home: Path, k: dict, exc: BaseException) -> dict:
    rid = str(k.get('run_id'))
    project_ref = k.get('project')
    return d(f'{project_ref_name(k)} · {rid}', t('Unreadable'), [
        ('title', t("This task's record could not be read")), ('field', (t('Error'), error_prose(exc))), ('field', (t('Record'), f'runs/{rid}/'))],
        status='error', id=rid, run=rid, project_ref=project_ref if isinstance(project_ref, str) else None, active=k.get('state') not in TERMINAL,
        created=number(k.get('created')),
        description=description(t('This task'), t('The record has an unexpected shape. Other tasks are not affected. Press o to read the raw record.')),
        file={'round': 'json', 'path': str(run_directory(home, rid) / 'run.json'), 'title': t('Run record')})


def safe(produce: Callable[[Path, dict], dict], home: Path, k: dict) -> dict:
    try:
        return produce(home, k)
    except Exception as exc:
        return unreadable_task(home, k, exc)


def panel_node(home: Path, active: list[dict], overview: dict, alerts: dict, view: tuple[dict, float], event: list) -> dict:
    cash_funds = cash_status(home)
    known = cash_funds['known'] and cash_funds['limit'] is not None
    cash = t('Monthly left {k} / {s}', k=money(max(0, cash_funds['limit'] - cash_funds['spent'] - cash_funds['pending'])),
             s=money(cash_funds['limit'])) if known else t('Remaining unknown')
    held = t('Held: {a}', a=money(cash_funds['pending'])) if cash_funds['known'] else t('Held amount unknown')
    attention = alerts['child']
    attention.sort(key=lambda n: (0 if n['id'].startswith(('alarm-', 'attention-record')) else 1, n['id']))
    current = [f"{'○' if a.get('waiting') else '◉'} {a['summary']} · {a['entry_name']}" for a in active[:1]] or [t('No running task')]
    if not active and any(n['id'] == 'attention-record' for n in attention):
        current = [t('Active work unknown')]
    if len(active) > 1:
        current.append(t('… {n} more tasks · Tasks', n=len(active) - 1))
    warnings = [f"{n['summary']} · {n['entry_name']}" for n in attention[:2]] or [t('Nothing needs you')]
    if len(attention) > 2:
        warnings.append(t('… {n} more alerts · Attention', n=len(attention) - 2))
    input = sum(bool(a.get('input_waiting')) for a in active)
    waiting = sum(bool(a.get('waiting')) and not a.get('input_waiting') for a in active)
    summary = (t('{n} tasks running', n=len(active) - waiting - input) + (f" · {t('{n} tasks waiting', n=waiting)}" if waiting else '')
               + (f" · {t('{n} task inputs awaiting planning', n=input)}" if input else ''))
    now = d(t('Now'), summary if active else t('No running task'),
              [('title', t('Now'))] + [('line', f"{a['entry_name']} · {a['summary']}") for a in active],
              status='now_ts' if len(active) > waiting + input else 'waiting' if waiting or input else '', id='current', git=['tasks'], git_prose=t('Opens Tasks'))
    budget = d(t('Budget'), cash, [('title', t('Budget')), ('line', cash), ('line', held), ('muted', measurement_prose(view))],
               id='panel-budget', git=['budget'], git_prose=t('Opens Budget'))
    blocks = [('title', t('Panel')), ('section-short', t('Now'))] + [('line-short', line) for line in current]
    blocks += [('section-short', t('Attention'))] + [('line-short', line) for line in warnings]
    blocks += [('section-short', t('Budget')), ('line-short', cash), ('line-short', held), ('line-short', measurement_prose(view))]
    return d(t('Panel'), t('Overview'), blocks, [now, overview, alerts, budget], context=event, id='panel', compact=True,
             description=description(t('Panel'), t('Now, Attention and Budget use at most twelve lines. Press Enter to open the relevant details.')))


def tree(home: Path | None = None, agents: bool = True) -> dict:
    home = home or home_dir()
    all_records = runs(home)
    test_ids = {str(k['run_id']) for k in all_records if test_project(k)}
    product_records = [k for k in all_records if str(k['run_id']) not in test_ids and str(k.get('project_ref_parent')) not in test_ids]
    tests = ui_messages.tests_read(home)
    records = all_records if tests else product_records
    hidden = sum(not k.get('project_ref_parent') and test_project(k) for k in all_records) if not tests else 0
    parent = [k for k in records if not k.get('project_ref_parent')]
    active = [safe(task_node, home, k) for k in reversed(parent) if k.get('state') not in TERMINAL]
    finished_ones = [k for k in reversed(parent) if k.get('state') in TERMINAL]
    with ThreadPoolExecutor(max_workers=8) as pool:
        finished = list(pool.map(lambda k: safe(history_node, home, k), finished_ones[:25]))
        old_delivery = [b for b in pool.map(lambda k: safe(history_node, home, k), [k for k in finished_ones[25:] if delivery_candidate(k)]) if b.get('delivery')]
    view = budget_view(home)
    delivery_n = sum(1 for b in finished + old_delivery if b.get('delivery'))
    history = d(t('History'), t('{n} tasks', n=len(finished)), [
        ('title', t('History')), ('field', (t('Tasks'), str(len(finished)))), ('field', (t('Passed'), str(sum(1 for b in finished if b['status'] == 'finished')))),
        ('field', (t('Awaiting delivery'), str(delivery_n)))], finished, id='history',
        description=description(t('History'), t('The last 25 finished tasks. p and m work on those awaiting delivery.')))
    event = [('title', t('Recent events')), ('record', last_events(home, records))]
    workers = [('line', f"◉ {a['entry_name']}   {a['summary']}") for a in active] or [('muted', t('No running task'))]
    input = sum(bool(a.get('input_waiting')) for a in active)
    waiting = sum(bool(a.get('waiting')) and not a.get('input_waiting') for a in active)
    tasks = d(t('Tasks'), t('{n} running', n=len(active) - waiting - input) + (f" · {t('{n} tasks waiting', n=waiting)}" if waiting else '')
                 + (f" · {t('{n} task inputs awaiting planning', n=input)}" if input else ''), [
        ('title', t('Tasks')), ('field', (t('Running'), str(len(active) - waiting - input))), ('field', (t('Waiting'), str(waiting))),
        ('field', (t('Awaiting input'), str(input))), ('field', (t('Awaiting delivery'), str(delivery_n))),
        ('field', (t('History'), t('{n} tasks', n=len(finished)))), ('muted', t('{n} test tasks hidden', n=hidden)), ('section', t('Running tasks'))] + workers,
        active + [history], context=event, id='tasks', hidden_test=hidden,
        description=description(t('Tasks'), t('Tasks you gave. Running ones on top, finished ones inside History.'),
                          [(t('Running task'), t('Plan, test, judge, agents, spend and events')),
                           (t('History'), t('The last 25 finished tasks and those awaiting delivery'))]))
    attention = attention_node(home, parent, finished + old_delivery, view)
    broken = [p for p in (home / 'runs').glob('*/run.json') if not record_read(p).get('run_id')]
    if broken:
        attention['child'].append(d(t('Run records could not be read'), t('{n} records', n=len(broken)),
            [('title', t('Run records could not be read'))] + [('line', str(p)) for p in broken], status='error', id='attention-record',
            file={'round': 'json', 'path': str(broken[0]), 'title': t('Run record')}))
        attention['summary'] = t('{n} items', n=len(attention['child']))
        attention['status'] = 'error'
        attention['detail'].append(('line', t('Run records could not be read')))
    summary = summary_node(home, [k for k in product_records if not k.get('project_ref_parent')])
    cash_version = (mtime(home / 'chain/monthly-budget.db'), mtime(home / 'chain/monthly-budget.db-wal'))
    budget = _cached(f"Budget:{home}:{view[0].get('ts')}:{cash_limit(home)}:{cash_version}", 60,
                      lambda: budget_section(home, all_records, view), 'budget')
    panel = panel_node(home, active, summary, attention, view, event)
    if agents:
        version = view[0].get('ts') if measurement_fresh(view) else 'old'
        agent = _cached(f'Agents:{version}', 60, lambda: agent_section(home, view), 'agents')
    else:
        previous = next((v for a, (_, v) in CACHE.items() if a.startswith('Agents:')), None)
        agent = previous or d(t('Agents'), '', [('title', t('Agents'))], id='agents')
    settings = setting_section(home)
    return d('cadre', '', [], [panel, tasks, budget, agent, settings], id='root')


def projects(home: Path | None = None) -> list[str]:
    home = home or home_dir()
    temporary = ('/private/tmp/', '/scratch/live/')
    candidates = []
    for p in (home / 'projects').glob('*/repo-path'):
        try:
            path = p.read_text().strip()
        except OSError:
            continue
        if ('--' not in p.parent.name and not p.parent.name.startswith(('live-', 'codex-')) and not any(g in path for g in temporary)
                and Path(path).is_dir()):
            candidates.append(p.parent.name)
    last: dict[str, float] = {}
    for k in runs(home):
        last[str(k.get('project'))] = max(last.get(str(k.get('project')), 0.0), number(k.get('created')))
    return sorted(candidates, key=lambda p: -last.get(p, 0.0))


def plan_text(path: Path) -> str:
    plan = _read(path)
    if not plan:
        return t('Could not read file: {y}', y=path)
    lines = [t('Plan · {i}', i=plan.get('job_id') or path.parent.name), '']
    if plan.get('scope_points') is not None:
        lines.append(t('Scope score: {p}', p=plan['scope_points']))
    if plan.get('scope_rationale'):
        lines.append(t('Reason: {g}', g=plan['scope_rationale']))
    if plan.get('merge_order'):
        lines.append(t('Merge order: {s}', s=list_prose(plan['merge_order'])))
    for g in plan.get('tasks') or []:
        if not isinstance(g, dict):
            continue
        lines += ['', f"{g.get('id')} · {g.get('definition')}",
                     '  ' + t('Files: {v}', v=list_prose(g.get('files'))), '  ' + t('Acceptance: {v}', v=g.get('acceptance') or '—'),
                     '  ' + t('Out of scope: {v}', v=list_prose(g.get('scope_outside'))), '  ' + t('Depends on: {v}', v=list_prose(g.get('dependent'))),
                     '  ' + t('Difficulty: {z} · Model: {m} · Parallel group: {p}', z=g.get('difficulty', '—'), m=g.get('model') or '—',
                              p=g.get('parallel_group', '—')),
                     '  ' + t('Estimate: {min} min · {usd}', min=g.get('estimate_min', '?'), usd=money(g.get('estimate_usd')))]
    lines += ['', t('Raw file'), json.dumps(plan, ensure_ascii=False, indent=2)]
    return '\n'.join(lines)


def monitoring_data(home: Path, rid: str) -> dict:
    path = run_directory(home, rid) / 'run.json'
    k = record_read(path)
    if k.get('run_id') != rid:
        raise ValueError(t('Run record could not be read'))
    node = task_node(home, k)
    node['source_time'] = mtime(path) or None
    sources = log_sources(home, rid)
    last = max(sources, key=lambda s: mtime(Path(s['path'])), default=None)
    node['last_action_source'] = last['title'] if last else '—'
    node['last_action_time'] = (mtime(Path(last['path'])) or None) if last else None
    if last and last['round'] == 'jsonl':
        node['last_actions'] = [f'{z} · {m}' for z, _, m in record_events(Path(last['path']))[-3:]]
    else:
        node['last_actions'] = last_lines(last['path'], 3) if last else []
    return node


def file_read(feature: dict, raw: bool = False) -> tuple[str, str, str]:
    if feature.get('round') == 'plan-history':
        from run_store import RunStore
        try:
            view = RunStore(Path(feature['home'])).plan_view(feature['run_id'])
            return t('Plan history'), 'json', json.dumps(data_redacted(view), ensure_ascii=False, indent=2)
        except (OSError, ValueError, TypeError, KeyError):
            return t('Plan history'), 'text', t('Plan history is unreadable. The operation waits.')
    round, title = str(feature.get('round') or 'text'), str(feature.get('title') or '')
    if round == 'trace':
        trace = event_trace(Path(feature['home']), feature['run_id'])
        return title, 'json' if raw else 'text', json.dumps(trace, ensure_ascii=False, indent=2) if raw else trace_text(trace)
    if round == 'brain' and not raw:
        return title, 'text', brain_text(Path(feature['path']))
    if round == 'diff':
        try:
            r = subprocess.run(feature['command'], capture_output=True, text=True, timeout=20)
            text = r.stdout if r.returncode == 0 else r.stderr
        except (OSError, subprocess.SubprocessError) as exc:
            text = str(exc)
        return title, 'diff', text_redacted(text.strip()) or t('(no difference)')
    if round == 'plan':
        return title, 'text', text_redacted(plan_text(Path(feature['path'])))
    path = Path(str(feature.get('path') or ''))
    if round == 'brain':
        path = path / 'stdout.jsonl'
        round = 'jsonl'
    try:
        if path.is_symlink() or path.parent.is_symlink():
            return title, 'text', t('Linked log was not read')
        text = path.read_text(errors='replace')
    except OSError:
        return title, 'text', t('Could not read file: {y}', y=path)
    if round == 'json':
        try:
            text = json.dumps(data_redacted(json.loads(text)), ensure_ascii=False, indent=2)
            return title, round, text
        except ValueError:
            return title, 'text', t('Malformed JSON') + '\n' + text_redacted(text)
    elif round == 'jsonl':
        if not raw:
            names = {'thought': t('Thought'), 'tool': t('Tool'), 'outcome': t('Tool result'), 'raw': t('Raw event'), 'error': t('Error')}
            return title, 'text', '\n'.join(f'{z}  {names[kind]}  {m}' for z, kind, m in record_events(path, full=True)) or t('(no records)')
        lines = []
        for no, line in enumerate(text.splitlines(), 1):
            try:
                content = json.dumps(data_redacted(json.loads(line)), ensure_ascii=False, indent=2)
            except ValueError:
                content = text_redacted(line)
            lines.append(f'{path.name}:{no}\n{content}')
        return title, 'text', '\n\n'.join(lines)
    return title, round, text_redacted(text)


def trace_replay(raw: str) -> dict:
    if len(raw) > 32_000_000:
        raise ValueError('trace replay size limit')
    trace = json.loads(raw)
    if not isinstance(trace, dict) or trace.get('schema') != 'cadre-trace-v1' or not isinstance(trace.get('run_id'), str):
        raise ValueError('invalid trace schema')
    from run_store import identifier
    identifier(trace['run_id'])
    for key in ('sources', 'issues', 'events'):
        if not isinstance(trace.get(key), list) or len(trace[key]) > 10000 or any(not isinstance(v, dict) for v in trace[key]):
            raise ValueError('invalid trace entries')
    for source in trace['sources']:
        if not isinstance(source.get('path'), str) or type(source.get('mtime')) not in (int, float) or not -62_135_596_800 <= source['mtime'] < 253_402_300_800:
            raise ValueError('invalid trace source')
        if 'stable' in source and type(source['stable']) is not bool:
            raise ValueError('invalid trace freshness')
    for issue in trace['issues']:
        if not isinstance(issue.get('source'), str) or not isinstance(issue.get('issue'), str):
            raise ValueError('invalid trace issue')
    for event in trace['events']:
        if any(not isinstance(event.get(k), str) for k in ('id', 'run_id', 'source', 'type')) or 'data' not in event:
            raise ValueError('invalid trace event')
        if event.get('timestamp') is not None and (type(event['timestamp']) not in (int, float) or not -62_135_596_800 <= event['timestamp'] < 253_402_300_800):
            raise ValueError('invalid trace time')
        if not isinstance(event.get('lines'), list) or not event['lines'] or any(type(n) is not int or n < 1 for n in event['lines']):
            raise ValueError('invalid trace lines')
        if 'locations' in event and (not isinstance(event['locations'], list) or any(not isinstance(p, str) for p in event['locations'])):
            raise ValueError('invalid trace locations')
        if 'evidence_status' in event and event['evidence_status'] not in ('recorded', 'conflict', 'unknown'):
            raise ValueError('invalid trace evidence')
    json.dumps(trace, allow_nan=False)
    return data_redacted(trace)


def trace_main() -> int:
    import argparse
    import sys
    parser = argparse.ArgumentParser(description='Redacted, read-only Cadre event trace')
    parser.add_argument('run_id', nargs='?')
    parser.add_argument('--format', choices=('json', 'markdown'), default='json')
    parser.add_argument('--replay', action='store_true')
    args = parser.parse_args()
    if bool(args.run_id) == args.replay:
        parser.error('provide a run ID or --replay')
    try:
        trace = trace_replay(sys.stdin.read(32_000_001)) if args.replay else event_trace(home_dir(), args.run_id)
        if args.format == 'json':
            print(json.dumps(trace, ensure_ascii=False, indent=2, allow_nan=False))
        else:
            body = trace_text(trace)
            fence = '`' * max(3, max((len(m) + 1 for m in re.findall(r'`+', body)), default=3))
            print('# Cadre trace\n\n' + fence + 'text\n' + body + '\n' + fence)
    except (ValueError, OSError, TypeError, KeyError, RecursionError):
        print('Trace input or source is invalid.', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(trace_main())
