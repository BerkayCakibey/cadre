#!/usr/bin/env python3
"""Textual full-screen operator panel (real data and a sample-data preview)."""
from __future__ import annotations

import os
import json
import re
import shlex
import subprocess
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rich import box  # noqa: E402
from rich.console import Group  # noqa: E402
from rich.markdown import Markdown  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.progress_bar import ProgressBar as Bar  # noqa: E402
from rich.syntax import Syntax  # noqa: E402
from rich.table import Table  # noqa: E402
from rich.text import Text  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.containers import Horizontal, Vertical, VerticalScroll  # noqa: E402
from textual.screen import ModalScreen  # noqa: E402
from textual.widgets import Input, Select, Static, TextArea  # noqa: E402

import panel_data  # noqa: E402
import ui_messages  # noqa: E402
import design_sample  # noqa: E402
from ui_messages import t  # noqa: E402

PALETTES = {
    'Gruvbox': {'back': '#32302f', 'surface': '#3c3836', 'border': '#504945', 'prose': '#ebdbb2', 'muted': '#a89984', 'accent': '#d8a657',
                'good': '#a9b665', 'warning': '#ef965b', 'bad': '#ff8880', 'money': '#a9b665', 'model': '#df96af', 'moment': '#89b482', 'code': 'gruvbox-dark'},
    'Everforest': {'back': '#2d353b', 'surface': '#343f44', 'border': '#475258', 'prose': '#d3c6aa', 'muted': '#9da9a0', 'accent': '#dbbc7f',
                   'good': '#a7c080', 'warning': '#e69875', 'bad': '#f59395', 'money': '#a7c080', 'model': '#d699b6', 'moment': '#83c092', 'code': 'gruvbox-dark'},
    'Rosé Pine': {'back': '#191724', 'surface': '#1f1d2e', 'border': '#403d52', 'prose': '#e0def4', 'muted': '#908caa', 'accent': '#f6c177',
                  'good': '#9ccfd8', 'warning': '#ebbcba', 'bad': '#eb6f92', 'money': '#9ccfd8', 'model': '#c4a7e7', 'moment': '#ebbcba', 'code': 'dracula'},
    'Tokyo Night': {'back': '#1a1b26', 'surface': '#24283b', 'border': '#414868', 'prose': '#c0caf5', 'muted': '#9aa5ce', 'accent': '#7aa2f7',
                    'good': '#9ece6a', 'warning': '#e0af68', 'bad': '#f7768e', 'money': '#9ece6a', 'model': '#bb9af7', 'moment': '#7dcfff', 'code': 'monokai'},
    'Dracula': {'back': '#282a36', 'surface': '#343746', 'border': '#6272a4', 'prose': '#f8f8f2', 'muted': '#a6accd', 'accent': '#bd93f9',
                'good': '#50fa7b', 'warning': '#f1fa8c', 'bad': '#ff8585', 'money': '#50fa7b', 'model': '#ff79c6', 'moment': '#8be9fd', 'code': 'dracula'},
    'Catppuccin Mocha': {'back': '#1e1e2e', 'surface': '#313244', 'border': '#585b70', 'prose': '#cdd6f4', 'muted': '#a6adc8', 'accent': '#cba6f7',
                         'good': '#a6e3a1', 'warning': '#f9e2af', 'bad': '#f38ba8', 'money': '#a6e3a1', 'model': '#f5c2e7', 'moment': '#89dceb', 'code': 'dracula'},
}
R: dict[str, str] = {'entry_name': 'Gruvbox', **PALETTES['Gruvbox']}
MARK = {'finished': '●', 'now_ts': '◉', 'waiting': '○', 'error': '✗'}
SECTIONS = (('panel', 'Panel'), ('tasks', 'Tasks'), ('budget', 'Budget'), ('agents', 'Agents'), ('settings', 'Settings'))
RUNNER = Path(os.environ.get('CADRE_ENGINE_ROOT') or Path(__file__).resolve().parents[1])
LINE_PER = 3
MEANING = {
    'money': re.compile(r'\$\d+(?:[.,]\d+)?'),
    'model': re.compile(r'\b(?:[\w.-]+/)*(?:gpt|glm|deepseek|qwen|claude|kimi|gemini|ds|opus|sonnet)[\w./:-]*\b', re.I),
    'moment': re.compile(r'\b\d{1,2}:\d{2}(?::\d{2})?\b|\b\d+\s*(?:min|h|s)\b'),
    'good': re.compile(r'^(?:finished|complete|done|passed|pass|completed|applied)$', re.I),
    'bad': re.compile(r'^(?:error|failed|fail|problem)$', re.I),
    'warning': re.compile(r'^(?:waiting|queued|blocked|cancelled|stuck)$', re.I),
    'accent': re.compile(r'^(?:running|starting|in progress)$', re.I),
}


def palette_setup(entry_name: str | None = None) -> str:
    entry_name = entry_name if entry_name in PALETTES else ui_messages.color_read()
    R.clear()
    R.update({'entry_name': entry_name, **PALETTES[entry_name]})
    return entry_name


def prose(text: str, style: str = '') -> Text:
    outcome = Text(text, style=style or R['prose'])
    for round, pattern in MEANING.items():
        for match in pattern.finditer(text):
            outcome.stylize(R[round], match.start(), match.end())
    return outcome


def color(status: str) -> str:
    return {'finished': R['good'], 'now_ts': R['accent'], 'error': R['bad']}.get(status, R['muted'])


def single_line(text: Text) -> Text:
    text.no_wrap, text.overflow = True, 'ellipsis'
    return text


def bar_text(percent: float, span_width: int = 20) -> Text:
    full = round(span_width * max(0.0, min(float(percent), 100.0)) / 100)
    return Text.assemble(('━' * full, R['accent']), ('━' * (span_width - full), R['border']))


def bar(entry_name: str, percent: float, span_width: int = 24) -> Table:
    line = Table.grid(padding=(0, 2))
    line.add_column(width=16, no_wrap=True)
    line.add_column(width=span_width)
    line.add_column()
    line.add_row(prose(entry_name, R['muted']), Bar(total=100, completed=max(0.0, min(float(percent), 100.0)), width=span_width, style=R['border'],
                                              complete_style=R['accent'], finished_style=R['accent']), prose(panel_data.ratio(percent)))
    return line


def section_line(title: str) -> Text:
    return Text.assemble(('\n── ', R['border']), (title, f"bold {R['muted']}"), (' ' + '─' * 60, R['border']), no_wrap=True, overflow='crop')


def estimated_line(blocks: list) -> tuple[int, int]:
    line = field = 0
    for round, value in blocks:
        if round == 'record':
            line += max(1, len(value or []))
        elif round == 'section':
            line += 2
        elif round == 'line':
            line += 1 + len(str(value)) // 70
        else:
            line += 1
            field += round == 'field'
    return line, field


def form(blocks: list, roomy: bool = False) -> list:
    parts: list = []
    field_list: list = []

    def fields_empty() -> None:
        if field_list:
            table = Table.grid(padding=(0, 2))
            table.add_column(width=16, no_wrap=True)
            table.add_column(ratio=1)
            for order, (label, value) in enumerate(field_list):
                if order and roomy:
                    table.add_row('', '')
                round = 'model' if 'model' in str(label).lower() else ''
                table.add_row(prose(str(label), R['muted']), prose(str(value), R[round] if round else ''))
            parts.append(table)
            field_list.clear()
    for round, value in blocks:
        if round == 'field':
            field_list.append(value)
            continue
        fields_empty()
        if round == 'title':
            parts.append(prose(value, f"bold {R['accent']}"))
        elif round == 'line':
            parts.append(prose(value))
        elif round == 'line-short':
            parts.append(single_line(prose(value)))
        elif round == 'muted':
            parts.append(prose(value, R['muted']))
        elif round == 'code':
            parts.append(prose(str(value), R.get('code', '')))
        elif round == 'bar':
            parts.append(bar(*value))
        elif round == 'section':
            parts.append(section_line(value))
        elif round == 'section-short':
            parts.append(single_line(section_line(value)[1:]))
        elif round == 'flow':
            line = Text()
            for order, (entry_name, status) in enumerate(value):
                if order:
                    line.append('  →  ', style=R['border'])
                line.append(f"{MARK.get(status, '·')} ", style=color(status))
                line.append(entry_name, style=f"bold {R['prose']}" if status == 'now_ts' else R['muted'])
            parts += [line, prose('')]
        elif round == 'record':
            table = Table.grid()
            table.add_column(width=2)
            table.add_column(ratio=1)
            for line_ in value or ['—']:
                table.add_row('', prose(str(line_), R['muted']))
            parts.append(table)
    fields_empty()
    return parts


def behind_start(command: list[str], env: dict[str, str], record: Path) -> subprocess.Popen:
    record.parent.mkdir(parents=True, exist_ok=True)
    with open(record, 'ab') as output:
        return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=output, env=env, start_new_session=True)


class Quiet(VerticalScroll, can_focus=False):
    pass


def quiet_paint(w: VerticalScroll) -> None:
    w.styles.scrollbar_background = R['back']
    w.styles.scrollbar_background_hover = R['back']
    w.styles.scrollbar_background_active = R['back']
    w.styles.scrollbar_color = R['border']
    w.styles.scrollbar_color_hover = R['muted']
    w.styles.scrollbar_color_active = R['accent']
    w.styles.scrollbar_size_vertical = 1


class Window(ModalScreen):
    def box_paint(self, id: str) -> None:
        self.styles.background = R['back']
        box = self.query_one(id)
        box.styles.background = R['surface']
        box.styles.border = ('round', R['accent'])
        box.styles.border_title_color = R['accent']


class Approval(Window):
    """FIX-51: permission windows require a typed approval (`written=True`): Enter alone, a repeated held key or queued
    events cannot pass the step; `yes`/`yes` must be typed followed by Enter. The plain Approve (cancel/plan) carries a short dead time after opening."""
    BINDINGS = [Binding('enter', 'yes', show=False), Binding('q', 'app.pop_screen', show=False, priority=True),
                Binding('escape', 'app.pop_screen', show=False, priority=True), Binding('up', 'scroll(-1)', show=False, priority=True),
                Binding('down', 'scroll(1)', show=False, priority=True), Binding('pageup', 'scroll(-10)', show=False, priority=True),
                Binding('pagedown', 'scroll(10)', show=False, priority=True), Binding('end', 'scroll(100000)', show=False, priority=True)]
    DEFAULT_CSS = '''
    Approval { align: center middle; }
    #approval { width: 72; max-width: 94%; height: auto; max-height: 94%; padding: 1 3; }
    #approval.long { height: 90%; }
    #approval-content { height: auto; }
    #approval.long #approval-content { height: 1fr; }
    #approval-prose { margin-top: 1; }
    #approval-hint { height: auto; padding-top: 1; }
    '''

    DEAD_DURATION_SEC = float(os.environ.get('CADRE_APPROVAL_DEAD_TIME_SEC', '0.3'))
    APPROVAL_WORDS = ('yes', 'y')

    def __init__(self, question: str, detail: str, action, written: bool = False) -> None:
        super().__init__()
        self.question, self.detail, self.action, self.written = question, detail, action, written
        self.opening = time.monotonic()

    def compose(self) -> ComposeResult:
        with Vertical(id='approval', classes='long' if len(self.detail) > 700 else ''):
            yield Static(prose(self.question, f"bold {R['prose']}"))
            with VerticalScroll(id='approval-content'):
                yield Static(prose(self.detail, R['muted']))
            if self.written:
                yield Input(id='approval-prose', placeholder=t('Type yes to approve'))
            yield Static(prose(t('Type yes, then Enter      q  no') if self.written else t('Enter  yes      q  no'), R['muted']), id='approval-hint')

    def on_mount(self) -> None:
        self.box_paint('#approval')
        self.opening = time.monotonic()
        if self.written:
            self.query_one('#approval-prose', Input).focus()

    def written_approval(self) -> bool:
        return not self.written or self.query_one('#approval-prose', Input).value.strip().lower() in self.APPROVAL_WORDS

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_yes()

    def action_yes(self) -> None:
        if time.monotonic() - self.opening < self.DEAD_DURATION_SEC or not self.written_approval():
            return
        self.app.pop_screen()
        self.action()

    def action_scroll(self, amount: int) -> None:
        self.query_one('#approval-content').scroll_relative(y=amount, animate=False)


class BudgetSetting(Window):
    BINDINGS = [Binding('enter', 'forward', priority=True, show=False), Binding('escape', 'app.pop_screen', priority=True, show=False),
                Binding('ctrl+e', 'edit', priority=True, show=False)]
    DEFAULT_CSS = '''
    BudgetSetting { align: center middle; }
    #ba-box { width: 76; max-width: 94%; height: auto; max-height: 100%; padding: 1 2; }
    #ba-value { margin: 1 0; }
    '''

    def __init__(self, home: Path, state: dict) -> None:
        super().__init__()
        self.home, self.state, self.preview = home, state, False
        self.micro = 0

    def compose(self) -> ComposeResult:
        with Vertical(id='ba-box'):
            yield Static(id='ba-info')
            yield Input(value=format(Decimal(self.state['micro']) / 1_000_000, 'f'), id='ba-value')
            yield Static(id='ba-error')
            yield Static(id='ba-hint')

    def on_mount(self) -> None:
        self.box_paint('#ba-box')
        self.draw()
        self.query_one(Input).focus()

    def draw(self) -> None:
        scope = self.state['project_ref'] if self.state['round'] == 'run' else t('Shared monthly cash cap')
        lines = [prose(scope, f"bold {R['accent']}"), prose(t('Current: {v} USD', v=self.state['value']))]
        if self.preview:
            lines.append(prose(t('New: {v} USD', v=format(Decimal(self.micro) / 1_000_000, 'f')), R['money']))
            if self.micro > self.state['micro']:
                lines.append(prose(t('Cap increase: Enter explicitly approves this increase.'), R['warning']))
        lines.append(prose(t('Existing run caps stay as recorded. A new task needs its own action permissions.') if self.state['round'] == 'run'
                          else t('Applies to subsequent cash calls. Spending and reservations are preserved.'), R['muted']))
        self.query_one('#ba-info', Static).update(Group(*lines))
        self.query_one(Input).display = not self.preview
        self.query_one('#ba-hint', Static).update(prose(t('Enter  save      Ctrl+E  edit      Esc  cancel') if self.preview
                                                        else t('USD · Enter  review      Esc  cancel'), R['muted']))

    def action_edit(self) -> None:
        self.preview = False
        self.draw()
        self.query_one(Input).focus()

    def action_forward(self) -> None:
        from monthly_budget import MonthlyBudgetDenied, usd_micro
        from run_store import budget_setting_write
        try:
            if not self.preview:
                self.micro = usd_micro(self.query_one(Input).value, zero=self.state['round'] == 'run')
                self.preview = True
                self.query_one('#ba-error', Static).update('')
                self.draw()
                return
            budget_setting_write(self.home, self.state['round'], self.state['project_ref'], format(Decimal(self.micro) / 1_000_000, 'f'), self.state)
        except (MonthlyBudgetDenied, ValueError, OSError):
            self.query_one('#ba-error', Static).update(prose(t('Not saved: check the amount, environment override and changed source.'), R['bad']))
            return
        self.app.pop_screen()
        panel_data.clean()
        self.app.reload(True)


class TaskFallbacks(Window):
    BINDINGS = [Binding('ctrl+s', 'save', priority=True, show=False), Binding('escape', 'app.pop_screen', priority=True, show=False)]
    DEFAULT_CSS = '''
    TaskFallbacks { align: center middle; }
    #gv-box { width: 76; max-width: 94%; height: auto; max-height: 100%; padding: 1 2; }
    #gv-box Static { height: auto; }
    '''

    def __init__(self, home: Path, state: dict) -> None:
        super().__init__()
        self.home, self.state = home, state

    def compose(self) -> ComposeResult:
        with Vertical(id='gv-box'):
            yield Static(t('New task defaults'))
            yield Static(t('Cost preference'))
            yield Select([(t('Economic'), 'economic'), (t('Balanced'), 'balanced'), (t('Strong'), 'strong')],
                         value=self.state['preference'], allow_blank=False, id='gv-preference')
            yield Static(t('Difficulty override'))
            yield Select([(t('Automatic estimate pending'), 'auto'), (t('Easy'), 'easy'), (t('Medium'), 'medium'),
                          (t('Hard'), 'hard'), (t('Very hard'), 'very_hard')], value=self.state['difficulty_override'] or 'auto',
                         allow_blank=False, id='gv-difficulty')
            yield Static(t('Fills new task forms. Existing tasks, permissions and budgets stay unchanged.'))
            yield Static(id='gv-error')
            yield Static(t('Ctrl+S  save      Esc  cancel'))

    def on_mount(self) -> None:
        self.box_paint('#gv-box')

    def action_save(self) -> None:
        from run_store import task_fallbacks_write
        difficulty = str(self.query_one('#gv-difficulty', Select).value)
        try:
            task_fallbacks_write(self.home, str(self.query_one('#gv-preference', Select).value),
                                    None if difficulty == 'auto' else difficulty, self.state)
        except (ValueError, OSError):
            self.query_one('#gv-error', Static).update(prose(t('Not saved. Settings changed or cannot be read. Reopen the editor.'), R['bad']))
            return
        self.app.pop_screen()
        panel_data.clean()
        self.app.reload(True)


class Reader(Window):
    BINDINGS = [Binding('q', 'app.pop_screen', show=False), Binding('escape', 'app.pop_screen', show=False),
                Binding('left', 'app.pop_screen', show=False), Binding('o', 'app.pop_screen', show=False),
                Binding('up', 'scroll(-1)', show=False), Binding('down', 'scroll(1)', show=False),
                Binding('shift+up', 'page(-1)', show=False), Binding('shift+down', 'page(1)', show=False),
                Binding('pageup', 'page(-1)', show=False), Binding('pagedown', 'page(1)', show=False), Binding('space', 'page(1)', show=False),
                Binding('r', 'raw', show=False)]
    DEFAULT_CSS = """
    Reader { align: center middle; }
    #ok-box { width: 100%; height: 100%; padding: 0 1; }
    #ok-body { height: 1fr; }
    #ok-hint { height: 1; }
    """

    def __init__(self, heading: str, round: str, text: str, source: dict | None = None) -> None:
        super().__init__()
        self.title, self.heading, self.round, self.text = heading, heading, round, text
        self.source, self.raw = source, False

    def compose(self) -> ComposeResult:
        with Vertical(id='ok-box'):
            yield Quiet(Static(id='ok-text'), id='ok-body')
            yield Static(id='ok-hint')

    def on_mount(self) -> None:
        self.box_paint('#ok-box')
        quiet_paint(self.query_one('#ok-body', Quiet))
        self.paint()

    def paint(self) -> None:
        self.query_one('#ok-box').border_title = self.heading
        if self.round == 'md':
            content = Markdown(self.text)
        elif self.round in ('diff', 'json'):
            content = Syntax(self.text, self.round, theme=R['code'], word_wrap=True, background_color=R['surface'])
        else:
            content = Text(self.text, style=R['prose'])
        self.query_one('#ok-text', Static).update(content)
        hint = t('↑ ↓  scroll      Shift+↑ ↓  page      q  close')
        if self.source and self.source.get('round') in ('brain', 'jsonl', 'log', 'trace'):
            hint = t('Redacted raw log' if self.raw else 'Readable log') + ' · ' + t('r  view') + ' · ' + hint
        self.query_one('#ok-hint', Static).update(prose(hint, R['muted']))

    def action_raw(self) -> None:
        if self.source and self.source.get('round') in ('brain', 'jsonl', 'log', 'trace'):
            self.raw = not self.raw
            self.heading, self.round, self.text = panel_data.file_read(self.source, raw=self.raw)
            self.title = self.heading
            self.paint()
            self.query_one('#ok-body', Quiet).scroll_home(animate=False)

    def action_scroll(self, d: int) -> None:
        self.query_one('#ok-body', Quiet).scroll_relative(y=d, animate=False)

    def action_page(self, d: int) -> None:
        box = self.query_one('#ok-body', Quiet)
        (box.scroll_page_down if d > 0 else box.scroll_page_up)(animate=False)


class LiveMonitoring(Window):
    BINDINGS = [Binding('q', 'app.pop_screen', show=False, priority=True),
                Binding('escape', 'app.pop_screen', show=False, priority=True),
                Binding('up', 'select(-1)', show=False), Binding('down', 'select(1)', show=False),
                Binding('o', 'read', show=False), Binding('pageup', 'page(-1)', show=False), Binding('pagedown', 'page(1)', show=False)]
    DEFAULT_CSS = '''
    LiveMonitoring { align: center middle; }
    #trace-box { width: 100%; height: 100%; padding: 0 1; }
    #trace-status { height: auto; max-height: 6; }
    #trace-content { height: 1fr; }
    #trace-list { width: 35%; height: 1fr; margin-right: 1; }
    #trace-detail { width: 1fr; height: 1fr; }
    #trace-hint { height: 1; }
    '''

    def __init__(self, home: Path, run_id: str) -> None:
        super().__init__()
        self.home, self.run_id = home, run_id
        self.data: dict = {}
        self.elements: list[tuple[tuple[str, ...], dict]] = []
        self.choice, self.was_read, self.error, self.busy = 0, 0.0, '', False

    def compose(self) -> ComposeResult:
        with Vertical(id='trace-box'):
            yield Static(id='trace-status')
            with Horizontal(id='trace-content'):
                yield Quiet(Static(id='trace-menu'), id='trace-list')
                yield Quiet(Static(id='trace-text'), id='trace-detail')
            yield Static(id='trace-hint')

    def on_mount(self) -> None:
        self.box_paint('#trace-box')
        self.query_one('#trace-box').border_title = f"{t('Live monitor')} · {self.run_id}"
        for name in ('#trace-list', '#trace-detail'):
            quiet_paint(self.query_one(name, Quiet))
        self.query_one('#trace-list').styles.border_right = ('solid', R['border'])
        self.set_interval(2, self.reload)
        self.set_interval(1, self.paint)
        self.reload()

    def reload(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.run_worker(self.load, thread=True, exit_on_error=False)

    def load(self) -> None:
        try:
            data, error = panel_data.monitoring_data(self.home, self.run_id), ''
        except Exception as exc:
            data, error = {}, panel_data.error_prose(exc)
        if self.is_mounted:
            self.app.call_from_thread(self.data_setup, data, error)

    def data_setup(self, data: dict, error: str) -> None:
        if not self.is_mounted:
            return
        self.busy, self.error = False, error
        if not error:
            selected = self.elements[self.choice][0] if self.elements else None
            self.data, self.was_read, self.elements = data, time.time(), []

            def add(node: dict, path: tuple[str, ...]) -> None:
                self.elements.append((path, node))
                for child in node['child']:
                    add(child, (*path, str(child.get('id') or child['entry_name'])))

            add(data, (self.run_id,))
            self.choice = next((i for i, (path, _) in enumerate(self.elements) if path == selected), min(self.choice, len(self.elements) - 1))
        self.paint()

    def paint(self) -> None:
        if not self.is_mounted:
            return
        status = []
        if self.error:
            status.append(prose(t('Stale data · read error: {e}', e=self.error), R['bad']))
        if self.data:
            status.append(single_line(prose(self.data['entry_name'] + ' · ' + self.data['summary'])))
            def age(ts: float | None) -> str:
                if not ts:
                    return t('Time unknown')
                seconds = max(0, time.time() - ts)
                return t('{s} s', s=int(seconds)) if seconds < 60 else panel_data.duration_text(seconds)
            status.append(single_line(prose(t('Last read: {o} · Run record age: {k} · Last action: {s}',
                                                       o=age(self.was_read), k=age(self.data.get('source_time')), s=age(self.data.get('last_action_time'))), R['muted'])))
            status += form(self.data.get('context', [])[-3:-1])
        else:
            status.append(prose(t('Waiting for a record'), R['muted']))
        self.query_one('#trace-status', Static).update(Group(*status))
        menu = []
        width = max(1, self.query_one('#trace-list').size.width - 2)
        for i, (path, node) in enumerate(self.elements):
            style = f"bold {R['back']} on {R['accent']}" if i == self.choice else R['prose']
            line = Text('  ' * (len(path) - 1) + f"{MARK.get(node['status'], '·')} {node['entry_name']}", style=style)
            line.truncate(width, overflow='ellipsis', pad=True)
            menu.append(single_line(line))
        self.query_one('#trace-menu', Static).update(Group(*menu))
        if self.elements:
            node = self.elements[self.choice][1]
            self.query_one('#trace-text', Static).update(Group(*form(node['detail']),
                *form([('section', t('Latest actions')), ('field', (t('Source'), self.data['last_action_source'])), ('record', self.data['last_actions'])])))
        self.query_one('#trace-hint', Static).update(prose(t('↑ ↓  select      o  record      PgUp/PgDn  details      q/Esc  close · work continues'), R['muted']))

    def action_select(self, delta: int) -> None:
        if self.elements:
            self.choice = (self.choice + delta) % len(self.elements)
            self.paint()
            self.query_one('#trace-detail', Quiet).scroll_home(animate=False)
            self.query_one('#trace-list', Quiet).scroll_to(y=max(0, self.choice - 2), animate=False)

    def action_read(self) -> None:
        spec = self.elements[self.choice][1].get('file') if self.elements else None
        if spec:
            self.app.push_screen(Reader(*panel_data.file_read(spec), source=spec))

    def action_page(self, delta: int) -> None:
        view = self.query_one('#trace-detail', Quiet)
        (view.scroll_page_down if delta > 0 else view.scroll_page_up)(animate=False)


class PlanChange(Window):
    BINDINGS = [Binding('ctrl+s', 'send', show=False), Binding('escape', 'give_up', show=False)]
    DEFAULT_CSS = 'PlanChange { align: center middle; } #plan-change-form { width: 78; max-width: 94%; height: auto; max-height: 90%; padding: 1 2; } #plan-change-context { height: auto; max-height: 8; } #plan-change-text { height: 7; }'

    def __init__(self, home: Path, run_id: str, revision: int, decision: dict[str, str] | None = None) -> None:
        super().__init__()
        self.home, self.run_id, self.revision = home, run_id, revision
        self.change_id = uuid.uuid4().hex
        self.decision = decision

    def compose(self) -> ComposeResult:
        with Vertical(id='plan-change-form'):
            yield Static(t('Answer critical decision') if self.decision else t('Request a plan change'))
            with Quiet(id='plan-change-context'):
                yield Static(panel_data.text_redacted(self.decision['question']) if self.decision else
                             t('New steps are blocked first. The actual stop is verified separately.'), markup=False, id='plan-change-question')
            if self.decision:
                yield Static(t('An answer does not grant new permission or budget. The new plan is reviewed separately.'))
            yield TextArea(id='plan-change-text')
            yield Static('', id='plan-change-error')
            yield Static(t('Ctrl+S submit · Esc cancel'))

    def on_mount(self) -> None:
        self.box_paint('#plan-change-form')
        self.query_one('#plan-change-text', TextArea).focus()

    def action_send(self) -> None:
        text = self.query_one('#plan-change-text', TextArea).text
        base = ['python3', str(RUNNER / 'lib/run_store.py')]
        command = ([*base, 'plan-answer', self.run_id, str(self.revision), self.decision['id']] if self.decision
                   else [*base, 'plan-change', self.run_id, self.change_id, str(self.revision)])
        try:
            result = subprocess.run(command, input=text, capture_output=True, text=True, timeout=30,
                                    env={**os.environ, 'CADRE_ENGINE_ROOT': str(RUNNER), 'CADRE_HOME': str(self.home)})
        except (OSError, subprocess.SubprocessError) as exc:
            self.query_one('#plan-change-error', Static).update(t('The command did not respond: {e}', e=panel_data.error_prose(exc)))
            return
        if result.returncode != 0:
            self.query_one('#plan-change-error', Static).update(
                t('Request not saved. Check the text, plan revision and any pending change.') + '\n' + (result.stderr or result.stdout).strip()[-200:])
            return
        self.dismiss()
        self.app.reload(True)

    def action_give_up(self) -> None:
        self.dismiss()


class FreshTask(Window):
    BINDINGS = [Binding('escape', 'give_up', priority=True), Binding('ctrl+s', 'finish', priority=True),
                Binding('ctrl+b', 'go_back', priority=True),
                Binding('up', 'scroll(-1)', show=False), Binding('down', 'scroll(1)', show=False),
                Binding('enter', 'forward', show=False), Binding('q', 'edit', show=False)]
    DEFAULT_CSS = """
    FreshTask { align: center middle; }
    #nt-box { width: 90; max-width: 96%; height: auto; max-height: 100%; padding: 1 2; }
    #nt-text { height: 8; margin: 1 0; }
    #nt-folder { margin: 1 0; }
    #nt-hint { margin-top: 1; }
    #nt-scroll { height: 1fr; }
    #nt-fields, #nt-source { height: auto; }
    #nt-fields Static, #nt-source Static { height: auto; }
    #nt-box { height: 90%; }
    """

    def __init__(self, options: list[dict], selected: int = 0) -> None:
        super().__init__()
        self.options = options
        self.step: int | str = 1
        self.choice = selected
        self.folder = ''
        self.submission_id = uuid.uuid4().hex
        self.request: dict = {}
        self.sending = False
        self.budget_loaded = False
        self.fallback_state: dict | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id='nt-box'):
            yield Static(id='nt-title')
            with VerticalScroll(id='nt-scroll'):
                yield Static(id='nt-body')
                yield Input(id='nt-folder', placeholder=t('Folder name or full path'))
                with Vertical(id='nt-source'):
                    yield Static(t('Input mode'))
                    yield Select([(t('Plan my goal'), 'goal'), (t('Existing backlog'), 'backlog'),
                                  (t('Create a plan only'), 'plan_only')], value='goal', allow_blank=False, id='nt-mode')
                    yield Input(id='nt-backlog', placeholder=t('Source section (optional): file.md#CARD-ID'))
                    yield Input(id='nt-sources', placeholder=t('Readable sources (optional, review): path, path'))
                yield TextArea(id='nt-text')
                with Vertical(id='nt-fields'):
                    yield Static(id='nt-defaults')
                    yield Static(t('Expected output'))
                    yield Input(id='nt-output', value=t('The output described in the request'))
                    yield Static(t('Acceptance criteria (optional)'))
                    yield Input(id='nt-acceptance')
                    yield Static(t('Permitted files (optional, narrow acceptance)'))
                    yield Input(id='nt-scope-files', placeholder=t('Path, path — empty means full-suite acceptance'))
                    yield Static(t('Required tests (optional, narrow acceptance)'))
                    yield Input(id='nt-scope-tests', placeholder=t('Test file, test file'))
                    yield Static(t('Cost preference'))
                    yield Select([(t('Economic'), 'economic'), (t('Balanced'), 'balanced'), (t('Strong'), 'strong')], value='balanced', allow_blank=False, id='nt-preference')
                    yield Static(t('Difficulty override'))
                    yield Select([(t('Automatic estimate pending'), 'auto'), (t('Easy'), 'easy'), (t('Medium'), 'medium'),
                                  (t('Hard'), 'hard'), (t('Very hard'), 'very_hard')], value='auto', allow_blank=False, id='nt-difficulty')
                    yield Static(t('Total task budget (USD)'))
                    yield Input(id='nt-budget', value='2')
                    yield Static(t('Time limit (minutes)'))
                    yield Input(id='nt-duration', value='60')
            yield Static(id='nt-hint')

    def on_mount(self) -> None:
        self.box_paint('#nt-box')
        from run_store import task_fallbacks
        try:
            self.fallback_state = task_fallbacks(self.app.home or panel_data.home_dir())
            self.query_one('#nt-preference', Select).value = self.fallback_state['preference']
            self.query_one('#nt-difficulty', Select).value = self.fallback_state['difficulty_override'] or 'auto'
            preference = {'economic': 'Economic', 'balanced': 'Balanced', 'strong': 'Strong'}[self.fallback_state['preference']]
            difficulty = {None: 'Automatic estimate pending', 'easy': 'Easy', 'medium': 'Medium', 'hard': 'Hard', 'very_hard': 'Very hard'}[self.fallback_state['difficulty_override']]
            self.query_one('#nt-defaults', Static).update(prose(t('Saved default: {p} · {d}', p=preference, d=difficulty) + '\n' +
                                                             t('Visible defaults are shown below. You can edit them, but they grant no permission.'), R['muted']))
        except (ValueError, OSError):
            self.query_one('#nt-defaults', Static).update(prose(t('Task defaults cannot be read. Check the settings.'), R['bad']))
        self.draw()

    @property
    def target(self) -> dict:
        return self.options[self.choice]

    def folder_path(self) -> str:
        path = Path(self.folder).expanduser()
        return str(path if path.is_absolute() else Path(os.getcwd()) / path)

    def target_text(self) -> str:
        if self.target['round'] == 'fresh':
            return t('New folder · {y}', y=self.folder_path())
        if self.target['round'] == 'here':
            return t('This folder · {y}', y=self.target['path'])
        return self.target['entry_name']

    def draw(self) -> None:
        text = self.query_one('#nt-text', TextArea)
        folder = self.query_one('#nt-folder', Input)
        title = {1: t('1 / 3   Goal and source'), 'entry_name': t('1 / 3   Name of the new folder'), 2: t('2 / 3   Expectations and limits'), 3: t('3 / 3   Summary and submit')}[self.step]
        self.query_one('#nt-title', Static).update(prose(t('New task') + '   ·   ' + title, f"bold {R['prose']}"))
        if self.step == 1:
            body = Group(*self.section([single_line(Text.assemble(('› ' if i == self.choice else '  ', R['accent']),
                                                               (s['label'], f"bold {R['accent']}" if i == self.choice else R['prose']),
                                                               (f"   {s.get('note', '')}", R['muted']))) for i, s in enumerate(self.options)],
                                      self.field(15)))
            hint = t('↑ ↓  project · Tab  field · Ctrl+S  next · Esc  cancel')
        elif self.step == 'entry_name':
            body = prose(t('A short name is created inside your current folder: {y}', y=os.getcwd()), R['muted'])
            hint = t('Type      Enter  continue      Esc  cancel')
        elif self.step == 2:
            body = prose(t('Where: {h}   ·   write as many lines as you like', h=self.target_text()), R['muted'])
            hint = t('Tab  field · Ctrl+S  summary · Ctrl+B  back · Esc  cancel')
        else:
            raw = self.request or self.input()
            body = Group(prose(t('Where: {h}', h=self.target_text())),
                          prose(self.shorten(text.text.strip() or t('(empty)'), max(3, self.field(17))), f"bold {R['prose']}"),
                          *form(self.summary_fields(raw)),
                          prose(t('This summary contains your inputs. A model plan has not been produced.'), R['muted']),
                          prose(t('The request waits for planning and permission gates. No executor starts.'), R['warning']))
            hint = t('Enter  send      q  back to writing      Esc  cancel')
        self.query_one('#nt-body', Static).update(body)
        folder.display = self.step == 'entry_name'
        self.query_one('#nt-source').display = self.step == 1
        self.query_one('#nt-fields').display = self.step == 2
        self.query_one('#nt-backlog').display = self.query_one('#nt-mode', Select).value in ('goal', 'backlog')
        self.query_one('#nt-sources').display = self.query_one('#nt-mode', Select).value == 'goal'
        text.display = self.step == 2
        text.styles.height = max(3, min(8, self.field(11)))
        self.query_one('#nt-hint', Static).update(prose(hint, R['muted']))
        if self.step == 'entry_name':
            folder.focus()
        elif self.step == 2:
            text.focus()
        else:
            self.set_focus(None)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == 'nt-mode' and self.is_mounted:
            field = self.query_one('#nt-backlog', Input)
            field.display = event.value in ('goal', 'backlog')
            field.placeholder = t('Backlog path (inside the repo)' if event.value == 'backlog'
                                 else 'Source section (optional): file.md#CARD-ID')
            field.value = ''
            sources = self.query_one('#nt-sources', Input)
            sources.display = event.value == 'goal'
            sources.value = ''

    def input(self) -> dict:
        home = self.app.home or panel_data.home_dir()
        target = self.folder_path() if self.target['round'] == 'fresh' else self.target.get('path', '')
        project = self.target.get('entry_name', '')
        if self.target['round'] == 'project_ref':
            try:
                target = (home / 'projects' / project / 'repo-path').read_text().strip()
            except OSError:
                target = ''
        elif self.target['round'] == 'here':
            import project_registry
            project = project_registry.enrolled(home, Path(target).resolve()) or ''
        else:
            project = re.sub(r'[^a-z0-9]+', '-', Path(target).name.lower()).strip('-') or 'project_ref'
        difficulty = str(self.query_one('#nt-difficulty', Select).value)
        return {'submission_id': self.submission_id, 'project': project, 'target_path': target,
                'target_kind': 'new' if self.target['round'] == 'fresh' else 'existing',
                'input_mode': str(self.query_one('#nt-mode', Select).value), 'backlog_path': self.query_one('#nt-backlog', Input).value,
                'source_paths': self.query_one('#nt-sources', Input).value,
                'prompt': self.query_one('#nt-text', TextArea).text, 'output': self.query_one('#nt-output', Input).value,
                'acceptance': self.query_one('#nt-acceptance', Input).value, 'preference': str(self.query_one('#nt-preference', Select).value),
                'scope_files': self.query_one('#nt-scope-files', Input).value, 'scope_tests': self.query_one('#nt-scope-tests', Input).value,
                'difficulty_override': None if difficulty == 'auto' else difficulty, 'budget_usd': self.query_one('#nt-budget', Input).value,
                'duration_minutes': self.query_one('#nt-duration', Input).value, 'source_token': '',
                'defaults_token': self.fallback_state['token'] if self.fallback_state else ''}

    def summary_fields(self, raw: dict) -> list:
        from panel_data import scope_text
        return [('field', (t('Input mode'), t({'goal': 'Plan my goal', 'backlog': 'Existing backlog', 'plan_only': 'Create a plan only'}[raw['input_mode']]))),
                ('field', (t('Source section' if raw['input_mode'] == 'goal' else 'Backlog'), raw['backlog_path'] or '—')),
                ('field', (t('Readable sources'), raw.get('source_paths') or t('Unrestricted (project source)'))),
                ('field', (t('Expected output'), raw['output'])),
                ('field', (t('Acceptance criteria'), raw['acceptance'] or t('To be derived from the original request in the plan'))),
                ('field', (t('Narrow acceptance scope'), scope_text(raw.get('scope_files'), raw.get('scope_tests')))),
                ('field', (t('Cost preference'), t({'economic': 'Economic', 'balanced': 'Balanced', 'strong': 'Strong'}[raw['preference']]))),
                ('field', (t('Difficulty override'), t({None: 'Awaiting automatic estimate', 'easy': 'Easy', 'medium': 'Medium', 'hard': 'Hard', 'very_hard': 'Very hard'}[raw['difficulty_override']]))),
                ('field', (t('Task budget'), raw['budget_usd'] + ' USD · ' + t('Total'))),
                ('muted', t('Provider scope and exact permissions will be verified at the planning gate.')),
                ('field', (t('Duration'), t('{d} min', d=raw['duration_minutes'])))]

    def field(self, constant: int) -> int:
        return max(3, int(self.app.size.height * 0.96) - constant)

    def span_width(self) -> int:
        return max(20, min(90, int(self.app.size.width * 0.96)) - 8)

    def section(self, lines: list, fitting: int) -> list:
        if len(lines) <= fitting:
            return lines
        fitting = max(fitting - 2, 1)
        start = min(max(0, self.choice - fitting // 2), len(lines) - fitting)
        remaining = len(lines) - start - fitting
        return ([prose(t('  ▲ {n} more above', n=start), R['muted'])] if start else [prose('')]) + lines[start:start + fitting] + (
            [prose(t('  ▼ {n} more below', n=remaining), R['muted'])] if remaining else [prose('')])

    def shorten(self, text: str, fitting: int) -> str:
        span_width, fitting = self.span_width(), max(fitting, 2)
        lines = text.splitlines() or ['']
        sizes = [max(1, -(-len(line) // span_width)) for line in lines]
        if sum(sizes) <= fitting:
            return text
        selected, used = [], 0
        for line, size in zip(lines, sizes):
            if used + size > fitting - 1:
                break
            selected.append(line)
            used += size
        if not selected:
            selected = [lines[0][:(fitting - 1) * span_width - 1] + '…']
        remaining = len(lines) - len(selected)
        return '\n'.join(selected + ([t('… and {n} more lines', n=remaining)] if remaining else []))

    def on_resize(self) -> None:
        if self.is_mounted:
            self.draw()

    def action_scroll(self, d: int) -> None:
        if self.step == 1:
            self.choice = (self.choice + d) % len(self.options)
            self.draw()

    def action_forward(self) -> None:
        if self.step == 1:
            self.step = 'entry_name' if self.target['round'] == 'fresh' else 2
            if self.step == 2:
                self.budget_load()
            self.draw()
            self.query_one('#nt-scroll', VerticalScroll).scroll_home(animate=False)
        elif self.step == 3:
            if self.sending:
                return
            if self.app.sample:
                self.app.notify(t('Preview: task not sent (sample data).'), title='cadre')
                return
            self.sending = True
            self.app.request_send(self.request, self)

    def is_full(self, text: str) -> bool:
        if text:
            return True
        self.app.notify(t('The task text is empty. Write the task first.'), title='cadre', severity='warning')
        self.step = 2
        self.draw()
        return False

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self.step == 'entry_name' and event.value.strip():
            self.folder = event.value.strip()
            self.step = 2
            self.budget_load()
            self.draw()
        elif self.step == 1 and event.input.id == 'nt-backlog':
            self.action_forward()

    def budget_load(self) -> None:
        if self.budget_loaded:
            return
        from run_store import budget_option
        try:
            value = budget_option(self.app.home or panel_data.home_dir(), 'run', self.target.get('entry_name', 'project_ref'))['value']
            self.query_one('#nt-budget', Input).value = str(value)
        except (OSError, ValueError, KeyError, RuntimeError):
            self.query_one('#nt-budget', Input).value = ''
        self.budget_loaded = True

    def action_finish(self) -> None:
        if self.step == 1:
            self.action_forward()
            return
        if self.step == 2 and self.is_full(self.query_one('#nt-text', TextArea).text.strip()):
            from run_store import RequestError, validate_request
            try:
                raw = self.input()
                checked, _ = validate_request(self.app.home or panel_data.home_dir(), raw)
                raw['source_token'] = checked['source_token']
            except RequestError as exc:
                self.app.notify(t(str(exc)), title=t(exc.field), severity='error')
                self.query_one('#nt-body', Static).update(prose(t(exc.field) + ': ' + t(str(exc)), R['bad']))
                return
            except (OSError, ValueError):
                self.app.notify(t('The source could not be verified. Check the target and file permissions.'), title='cadre', severity='error')
                return
            self.request = raw
            self.step = 3
            self.draw()
            self.query_one('#nt-scroll', VerticalScroll).scroll_home(animate=False)

    def action_edit(self) -> None:
        if self.step == 3 and not self.sending:
            self.step = 2
            self.draw()

    def action_go_back(self) -> None:
        if not self.sending:
            self.step = 2 if self.step == 3 else 1
            self.request = {}
            self.draw()

    def action_give_up(self) -> None:
        if not self.sending:
            self.app.pop_screen()


def empty_tree() -> dict:
    return panel_data.d('cadre', '', [], [panel_data.d(t(entry_name), id=id) for id, entry_name in SECTIONS], id='root')


HELP_KEYS = (
    ('← → / ↑ ↓ / Enter', 'Select a section at the top. Select and open an item inside.'),
    ('q / ← / Backspace', 'Go back one menu.'),
    ('1–5', 'Panel, Tasks, Budget, Agents, Settings.'),
    ('o', 'Read the selected item record.'),
    ('Shift+↑ ↓ / PgUp/PgDn', 'Scroll the details.'),
    ('n', 'Open the new task form.'),
    ('x', 'Open cancellation confirmation for an active task.'),
    ('i', 'Monitor an active task. q/Esc closes only the monitor.'),
    ('p / m', 'Open the push / merge terminal flow for a delivery candidate.'),
    ('? / Esc', 'Open the guide / exit the app.'),
)


def help_text() -> str:
    lines = [t('Usage guide'), ''] + [f'{key:<24} {t(description)}' for key, description in HELP_KEYS]
    lines += ['', t('Reader: ↑ ↓ scroll, PgUp/PgDn/Space page, q/Esc/←/o close. r redacted raw log.'),
        t('Confirmation window: Enter yes, permission windows need yes typed then Enter (a held key cannot pass), q/Esc cancel, ↑ ↓ / PgUp/PgDn / End scroll the text.'),
        t('Budget editor: Enter review/save, Ctrl+E edit, Esc cancel.'),
        t('New task: Tab field, Ctrl+S next/summary, Ctrl+B back, Enter submit, Esc cancel.'),
        t('New task input waits for the planning gate. No model or Git operation starts.'), '',
        t('Finished task: Tasks › History › task › Result / Delivery. Report for report work.'),
        t('Terminal confirmations do not replace explicit user permission.'),
        t('The legacy line menu does not support these Textual shortcuts.')]
    return '\n'.join(lines)


class Cadre(App):
    AUTO_FOCUS = None
    CSS = """
    Screen { padding: 0 2; }
    #start { height: 1; margin-top: 1; }
    #menu { height: auto; margin-top: 1; }
    #inner { height: 1fr; margin-top: 1; }
    #left { width: 42; height: 1fr; margin-right: 2; }
    #list { height: 1fr; padding: 1 1; }
    #legend { height: 4; margin-top: 1; }
    #right { width: 1fr; height: 1fr; }
    #parent-right { height: 1fr; }
    #middle { width: 3fr; height: 1fr; margin-right: 2; }
    #detail { height: 3fr; padding: 1 2; }
    #context { height: 2fr; padding: 0 2; margin-top: 1; }
    #description { width: 2fr; height: 1fr; padding: 1 2; }
    #sub { height: 11; margin-top: 1; }
    #live { width: 1fr; height: 1fr; padding: 0 2; margin-right: 2; }
    #fresh { width: 30; height: 1fr; padding: 0 2; }
    #hint { height: 1; margin: 1 0; }
    """
    BINDINGS = [Binding('up', 'up', show=False), Binding('down', 'down', show=False), Binding('left', 'left', show=False),
                Binding('right', 'right', show=False), Binding('enter', 'open', show=False), Binding('q', 'go_back', show=False),
                Binding('backspace', 'go_back', show=False), Binding('escape', 'quit', show=False), Binding('n', 'fresh', show=False),
                Binding('o', 'read', show=False), Binding('x', 'cancel', show=False), Binding('i', 'watch', show=False),
                Binding('question_mark', 'help', show=False),
                Binding('p', 'delivery("push")', show=False), Binding('m', 'delivery("merge")', show=False),
                Binding('shift+up', 'scroll_detail(-3)', show=False), Binding('shift+down', 'scroll_detail(3)', show=False),
                Binding('pageup', 'page(-1)', show=False), Binding('pagedown', 'page(1)', show=False),
                *[Binding(str(i), f'section({i - 1})', show=False) for i in range(1, 6)]]

    def __init__(self, sample: bool = False, home: Path | None = None, project_ref: str = '', simulation: bool = True) -> None:
        super().__init__()
        palette_setup()
        self.sample, self.fallback_project_ref, self.simulation = sample, project_ref, simulation
        self.home = home
        self.sim = None
        if sample:
            self.sample_setup()
        self.root = empty_tree()
        self.loaded = False
        self.busy = False
        self.pending = False
        self.last_error = ''
        self.last_measurement_request = 0.0
        self.path: list[int] = []
        self.choice = 0
        self.animation_level = 'none'

    def sample_setup(self) -> None:
        old = self.home
        self.home, scene = design_sample.setup()
        design_sample.probes_setup()
        self.sim = design_sample.Simulation(scene)
        if old is not None:
            design_sample.clean(old)

    def chain(self) -> list[dict]:
        nodes = [self.root]
        for i in self.path:
            nodes.append(nodes[-1]['child'][i])
        return nodes

    @property
    def elements(self) -> list[dict]:
        return self.chain()[-1]['child']

    @property
    def selected(self) -> dict:
        return self.elements[self.choice] if self.elements else panel_data.d('')

    @staticmethod
    def ident(node: dict) -> str:
        return str(node.get('id') or node.get('entry_name'))

    def section_no(self) -> int:
        return self.path[0] if self.path else self.choice

    def section(self, ident: str) -> dict:
        return next((b for b in self.root['child'] if b.get('id') == ident), panel_data.d(ident))

    def cap_amount(self) -> dict:
        return self.chain()[-1] if self.path else self.root['child'][self.section_no()]

    def action_up(self) -> None:
        if self.path and self.elements:
            self.choice = (self.choice - 1) % len(self.elements)
            self.draw()

    def action_down(self) -> None:
        if not self.path:
            self.action_open()
        elif self.elements:
            self.choice = (self.choice + 1) % len(self.elements)
            self.draw()

    def action_left(self) -> None:
        if self.path:
            self.action_go_back()
        else:
            self.choice = (self.choice - 1) % len(self.root['child'])
            self.draw()

    def action_right(self) -> None:
        if self.path:
            self.action_open()
        else:
            self.choice = (self.choice + 1) % len(self.root['child'])
            self.draw()

    def action_open(self) -> None:
        s = self.selected
        if s.get('action') == 'request-authorize':
            if not self.sample:
                self.request_approval(s['run'])
        elif s.get('action') == 'operator-approve':
            if not self.sample:
                self.operator_approval(s['run'], s['token'])
        elif s.get('action') in ('plan-answer', 'plan-change', 'plan-stop', 'plan-withdraw', 'plan-resume', 'start', 'spend-approval', 'request-plan'):
            if self.sample:
                return
            if s['action'] in ('plan-change', 'plan-answer'):
                self.push_screen(PlanChange(self.home or panel_data.home_dir(), s['run'], s['revision'], s.get('decision')))
            else:
                command = ['python3', str(RUNNER / 'lib/run_store.py'), s['action'], s['run']]
                if s.get('change_id'):
                    command.append(s['change_id'])
                if s['action'] == 'plan-resume':
                    command.append(str(s['revision']))
                if s['action'] == 'request-plan':
                    action = lambda: self.behind_execute(command, t('Plan generation started in the background. The result appears in the task status.'),
                                                    f"request-plan-{s['run']}")
                else:
                    action = lambda: self.execute(command, t('Plan operation recorded.'))
                self.push_screen(Approval(s['entry_name'], t('The engine rechecks plan, stop, permission and cap gates.'), action))
        elif s.get('action') == 'color':
            self.color_change()
        elif s.get('action') == 'tests':
            ui_messages.tests_write(not ui_messages.tests_read(self.home), self.home)
            panel_data.clean()
            self.reload(True)
        elif s.get('action') == 'budget-setting':
            if self.sample:
                self.notify(t('Preview: setting not saved (sample data).'), title='cadre')
                return
            self.push_screen(BudgetSetting(self.home or panel_data.home_dir(), s['budget_setting']))
        elif s.get('action') == 'task-fallbacks':
            if self.sample:
                self.notify(t('Preview: setting not saved (sample data).'), title='cadre')
                return
            self.push_screen(TaskFallbacks(self.home or panel_data.home_dir(), s['fallback_state']))
        elif s['child']:
            self.path.append(self.choice)
            self.choice = 0
            self.draw()
        elif s.get('git'):
            self.git(s['git'])

    def action_help(self) -> None:
        self.push_screen(Reader(t('Usage guide'), 'text', help_text()))

    def git(self, ids: list[str]) -> None:
        path, node = [], self.root
        for ident in ids:
            i = next((j for j, c in enumerate(node['child']) if self.ident(c) == ident), None)
            if i is None:
                break
            if not node['child'][i]['child']:
                self.path, self.choice = path, i
                self.draw()
                return
            path.append(i)
            node = node['child'][i]
        self.path, self.choice = path, 0
        self.draw()

    def action_go_back(self) -> None:
        if self.path:
            self.choice = self.path.pop()
            self.draw()

    def action_section(self, i: int) -> None:
        self.path, self.choice = ([i], 0) if self.root['child'][i]['child'] else ([], i)
        self.draw()

    def action_scroll_detail(self, d: int) -> None:
        self.query_one('#detail', Quiet).scroll_relative(y=d, animate=False)

    def action_page(self, d: int) -> None:
        box = self.query_one('#detail', Quiet)
        (box.scroll_page_down if d > 0 else box.scroll_page_up)(animate=False)

    def action_read(self) -> None:
        file = self.selected.get('file') if self.path else None
        if file:
            title, round, text = panel_data.file_read(file)
            self.push_screen(Reader(title, round, text, file))

    def color_change(self) -> None:
        order = list(ui_messages.COLORS)
        fresh = order[(order.index(R['entry_name']) + 1) % len(order)] if R['entry_name'] in order else order[0]
        ui_messages.color_write(fresh)
        palette_setup(fresh)
        self.palette_apply()
        self.draw()
        self.reload(True)
        self.notify(t('Color palette: {r}', r=fresh), title='cadre')

    def palette_apply(self) -> None:
        self.screen.styles.background = R['back']
        self.screen.styles.color = R['prose']
        for w in self.screen.query('*'):
            w.styles.background = R['back']
        for w in self.query(Quiet):
            quiet_paint(w)

    def fresh_options(self) -> list[dict]:
        home = self.home or panel_data.home_dir()
        projects = panel_data.projects(home)
        here = Path(os.getcwd()).resolve()
        current_project_ref = None
        for entry_name in projects:
            try:
                enrolled = Path((home / 'projects' / entry_name / 'repo-path').read_text().strip()).resolve()
            except OSError:
                continue
            if here == enrolled or entry_name == self.fallback_project_ref:
                current_project_ref = entry_name
                break
        options: list[dict] = []
        if current_project_ref:
            options.append({'round': 'project_ref', 'entry_name': current_project_ref, 'label': current_project_ref, 'note': t('This folder')})
        elif here not in (Path.home().resolve(), Path('/')):
            options.append({'round': 'here', 'path': str(here), 'label': t('This folder'), 'note': str(here)})
        options += [{'round': 'project_ref', 'entry_name': entry_name, 'label': entry_name, 'note': t('Registered project')} for entry_name in projects if entry_name != current_project_ref]
        options.append({'round': 'fresh', 'label': t('Create a new folder'), 'note': t('Creating the folder and the initial commit requires separate permission.')})
        return options

    def action_fresh(self) -> None:
        self.push_screen(FreshTask(self.fresh_options()))

    def request_send(self, request: dict, screen: FreshTask) -> None:
        def complete(result: dict, failed: bool) -> None:
            screen.sending = False
            if failed:
                message = t(result['message'])
                self.notify(message, title=t(result['error']), severity='error')
                if screen.is_mounted:
                    screen.query_one('#nt-body', Static).update(prose(t(result['error']) + ': ' + message, R['bad']))
                return
            if screen.is_mounted and self.screen is screen:
                self.pop_screen()
            message = t('Task saved: {r} · waiting for the planning gate.', r=result['run_id']) if result['status'] == 'waiting' else \
                t('Existing task: {r} · {s}', r=result['run_id'], s=t(panel_data.STATUS_NAME.get(result['status'], result['status'])))
            self.notify(message, title='cadre')
            self.reload(True)

        def send() -> None:
            home = self.home or panel_data.home_dir()
            try:
                result = subprocess.run(['python3', str(RUNNER / 'lib/run_store.py'), 'request'], input=json.dumps(request),
                                        text=True, capture_output=True, timeout=30,
                                        env={**os.environ, 'CADRE_HOME': str(home), 'CADRE_ENGINE_ROOT': str(RUNNER)})
                data = json.loads(result.stderr if result.returncode else result.stdout)
                failed = result.returncode != 0
            except (OSError, subprocess.SubprocessError, ValueError):
                data, failed = {'error': 'request', 'message': 'The task could not be saved. You can retry the same submission.'}, True
            self.call_from_thread(complete, data, failed)
        self.run_worker(send, thread=True, group='request', exit_on_error=False)

    def task_send(self, target: dict, text: str) -> None:
        if self.sample:
            self.notify(t('Preview: task not sent (sample data).'), title='cadre')
            return
        self.notify(t('Sending the task…'), title='cadre')
        self.run_worker(lambda: self.send(target, text), thread=True, group='send', exit_on_error=False)

    def send(self, target: dict, text: str) -> None:
        import project_registry
        try:
            message, error = self.task_save(target, text)
        except project_registry.RecordPermission as exc:
            self.call_from_thread(self.operator_approval, 'system', exc.token,
                                  lambda: self.task_send({**target, 'round': 'here'}, text))
            return
        self.call_from_thread(self.notify, message, title='cadre', severity='error' if error else 'information')
        self.call_from_thread(self.reload, True)

    def task_save(self, target: dict, text: str) -> tuple[str, bool]:
        import project_registry
        home = self.home or panel_data.home_dir()
        entry_name = target.get('entry_name')
        try:
            if target.get('round') in ('here', 'fresh'):
                entry_name = project_registry.save(target['path'], create=target['round'] == 'fresh', home=home)
        except project_registry.RecordPermission:
            raise
        except (project_registry.RecordError, OSError, subprocess.SubprocessError) as exc:
            return str(exc), True
        try:
            repo = (home / 'projects' / str(entry_name) / 'repo-path').read_text().strip()
        except OSError:
            return t('Could not read the repo path of {p}.', p=entry_name), True
        try:
            r = subprocess.run(['bash', str(RUNNER / 'bin/cadre.sh'), 'task', text], cwd=repo, capture_output=True, text=True, timeout=120,
                               env={**os.environ, 'CADRE_WATCH': '0', 'CADRE_ENGINE_ROOT': str(RUNNER), 'CADRE_HOME': str(home)})
        except (OSError, subprocess.SubprocessError) as exc:
            return t('The command did not respond: {e}', e=panel_data.error_prose(exc)), True
        outcome = (r.stdout + r.stderr).strip().splitlines()
        return next((s for s in outcome if 'saved' in s or 'QUEUE' in s), outcome[-1] if outcome else t('Sent')), r.returncode != 0

    def action_cancel(self) -> None:
        s = self.selected
        if not s.get('active'):
            return
        if self.sample:
            self.notify(t('Preview: not cancelled (sample data).'), title='cadre')
            return
        self.push_screen(Approval(t('Cancel task {a}?', a=s['entry_name']), t('The running step stops and the partial work is kept.'),
                              lambda: self.execute(['python3', str(RUNNER / 'lib/run_store.py'), 'cancel', s['run']], t('Cancel requested'))))

    def execute(self, command: list[str], message: str, moment: int = 120) -> None:
        try:
            r = subprocess.run(command, capture_output=True, text=True, timeout=moment,
                               env={**os.environ, 'CADRE_ENGINE_ROOT': str(RUNNER), 'CADRE_HOME': str(self.home or panel_data.home_dir())})
        except (OSError, subprocess.SubprocessError) as exc:
            self.notify(t('The command did not respond: {e}', e=panel_data.error_prose(exc)), title='cadre', severity='error')
        else:
            self.notify(message if r.returncode == 0 else (r.stderr or r.stdout).strip()[-200:], title='cadre',
                        severity='information' if r.returncode == 0 else 'error')
        self.reload(True)

    def behind_execute(self, command: list[str], message: str, entry_name: str) -> None:
        home = Path(self.home or panel_data.home_dir())
        try:
            behind_start(command, {**os.environ, 'CADRE_ENGINE_ROOT': str(RUNNER), 'CADRE_HOME': str(home)}, home / 'logs' / f'{entry_name}.log')
        except OSError as exc:
            self.notify(t('The command did not respond: {e}', e=panel_data.error_prose(exc)), title='cadre', severity='error')
        else:
            self.notify(message, title='cadre')
        self.reload(True)

    def operator_approval(self, rid: str, token: str, after=None) -> None:
        from permission import approve, read_action
        from run_store import RunStore
        store = RunStore(self.home or panel_data.home_dir())
        try:
            action = read_action(store, rid, token)
        except (OSError, ValueError, KeyError) as exc:
            self.notify(panel_data.error_prose(exc), title='cadre', severity='error')
            return

        def record(confirmation: str = '') -> None:
            try:
                receipt = approve(store, rid, token, confirmation)
            except (OSError, ValueError, KeyError) as exc:
                self.notify(panel_data.error_prose(exc), title='cadre', severity='error')
                return
            if receipt['status'] == 'first-approved':
                self.push_screen(Approval(t('Second explicit approval'), receipt['display'],
                                      lambda: record(receipt['confirmation']), written=True))
            else:
                try:
                    if receipt['identity']['kind'] == 'init-commit':
                        from permission import initialization_execute
                        initialization_execute(store, token)
                    elif receipt['identity']['kind'] == 'model-calls':
                        from permission import model_activate
                        model_activate(store, rid, token)
                except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                    self.notify(panel_data.error_prose(exc), title='cadre', severity='error')
                    self.reload(True)
                    return
                self.reload(True)
                if after is not None:
                    after()
        if action['status'] == 'first-approved':
            self.push_screen(Approval(t('Second explicit approval'), action['display'], lambda: record(action['confirmation']), written=True))
        elif action['status'] == 'pending':
            self.push_screen(Approval(t('Exact action permission'), action['identity']['kind'] + '\n' + action['display'], record, written=True))

    def request_approval(self, rid: str) -> None:
        base = ['python3', str(RUNNER / 'lib/run_store.py')]
        env = {**os.environ, 'CADRE_ENGINE_ROOT': str(RUNNER), 'CADRE_HOME': str(self.home or panel_data.home_dir())}
        try:
            response = subprocess.run([*base, 'request-proposal', rid], capture_output=True, text=True, env=env, timeout=15)
            if response.returncode != 0:
                self.notify((response.stderr or response.stdout).strip()[-200:] or t('The request proposal could not be read.'), title='cadre', severity='error')
                return
            proposal = json.loads(response.stdout)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self.notify(panel_data.error_prose(exc), title='cadre', severity='error')
            return
        detail = panel_data.request_approval_text(rid, proposal)

        def approve(confirmation: str = '') -> None:
            command = [*base, 'request-approve', rid, *([confirmation] if confirmation else [])]
            try:
                result = subprocess.run(command, input=json.dumps(proposal), capture_output=True, text=True, env=env, timeout=15)
                if result.returncode != 0:
                    self.notify((result.stderr or result.stdout).strip()[-200:] or t('The request approval was not recorded.'), title='cadre', severity='error')
                    return
                receipt = json.loads(result.stdout)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                self.notify(panel_data.error_prose(exc), title='cadre', severity='error')
                return
            if receipt['status'] == 'first-approved':
                exact = ("printf '%s' " + shlex.quote(json.dumps(proposal, ensure_ascii=False)) + ' | ' +
                         shlex.join([*base, 'request-approve', rid, receipt['confirmation']]))
                self.push_screen(Approval(t('Second explicit approval'), panel_data.request_approval_text(rid, proposal, exact),
                                      lambda: approve(receipt['confirmation']), written=True))
            else:
                self.reload(True)
        self.push_screen(Approval(t('Task-specific call permission'), detail, approve, written=True))

    def action_watch(self) -> None:
        s = self.selected
        if not s.get('active') or not s.get('project_ref'):
            return
        if self.sample:
            self.notify(t('Preview: live watch opens only for real tasks.'), title='cadre')
            return
        self.push_screen(LiveMonitoring(self.home or panel_data.home_dir(), str(s['run'])))

    def action_delivery(self, round: str) -> None:
        s = self.selected
        if not s.get('delivery'):
            return
        if self.sample:
            self.notify(t('Preview: {t} not done (sample data).', t=round), title='cadre')
            return
        with self.suspend():
            try:
                subprocess.run(['bash', str(RUNNER / 'bin/cadre.sh'), round, s['project_ref'], s['run']])
            except OSError as exc:
                print(panel_data.error_prose(exc))
            input(t('\nPress Enter to return… '))
        self.reload(True)

    def mark(self, node: dict) -> Text:
        status = node.get('status') or ''
        return Text(f"{MARK.get(status, '·')} ", style=color(status))

    def menu_r(self) -> Table:
        grid = Table.grid(expand=True, padding=(0, 1))
        for _ in self.root['child']:
            grid.add_column(ratio=1)
        boxes = []
        for i, b in enumerate(self.root['child']):
            selected = i == self.section_no()
            boxes.append(Panel(Text(b['entry_name'], style=f"bold {R['accent']}" if selected else R['muted'], justify='center'), box=box.ROUNDED,
                                 height=3, style=f"on {R['surface']}" if selected else '', border_style=R['accent'] if selected else R['border']))
        grid.add_row(*boxes)
        return grid

    def list_r(self, elements: list[dict], choice: int | None, height: int) -> Group:
        if not elements:
            return Group(prose(''), prose(t('Nothing here') if self.loaded else '', R['muted']))
        fitting = max(1, (height - 2) // LINE_PER)
        start = 0 if choice is None else min(max(0, choice - fitting // 2), max(0, len(elements) - fitting))
        lines = Table.grid(expand=True, padding=0)
        lines.add_column(ratio=1)
        lines.add_row(prose(t('  ▲ {n} more above', n=start), R['muted']) if start else prose(''))
        for i, c in enumerate(elements[start:start + fitting], start):
            selected = i == choice
            back = f" on {R['surface']}" if selected else ''
            parent = Text('› ' if selected else '  ', style=R['accent'] + back)
            parent.append_text(self.mark(c))
            parent.append(c['entry_name'], style=(f"bold {R['accent']}" if selected else (R['prose'] if choice is not None else R['muted'])) + back)
            if c['child']:
                parent.append('  ›', style=R['muted'] + back)
            elif c.get('git') or c.get('action'):
                parent.append('  →', style=R['muted'] + back)
            summary = f"{c['status_prose']} · {panel_data.relative_age(c.get('created'))}" if 'age' in c else c['summary']
            project_ref = f"     {c.get('project_ref') or '?'}" if 'age' in c else ''
            for line in (single_line(parent), single_line(prose(f"     {summary}" if summary else '', R['muted'])), single_line(prose(project_ref, R['muted']))):
                lines.add_row(line, style=f"on {R['surface']}" if selected else '')
        remaining = len(elements) - start - fitting
        if remaining > 0:
            lines.add_row(prose(t('  ▼ {n} more below', n=remaining), R['muted']))
        return Group(lines)

    def detail_r(self, node: dict, parent_level: bool) -> Group:
        if node.get('compact'):
            return Group(*form(node['detail']))
        preview = bool(node['child']) and not parent_level
        line, field = estimated_line(node['detail'])
        line += (min(len(node['child']), 6) + 4) if preview else 3
        height = (self.query_one('#detail').size.height or 30) if self.is_mounted else 30
        parts = form(node['detail'], roomy=line + field <= height)
        hints = []
        if parent_level:
            if node['child']:
                hints.append(t('↓ Enter  open {b}', b=node['entry_name']))
        elif preview:
            parts.append(section_line(t('Inside · {n}', n=len(node['child']))))
            for c in node['child'][:6]:
                line_ = Text('  ')
                line_.append_text(self.mark(c))
                line_.append(c['entry_name'], style=R['prose'])
                if c['summary']:
                    line_.append(f"   {c['summary']}", style=R['muted'])
                parts.append(single_line(line_))
            if len(node['child']) > 6:
                parts.append(prose(t('  … and {n} more', n=len(node['child']) - 6), R['muted']))
            hints.append(t('→ Enter  open'))
        elif node.get('git'):
            hints.append(t('→ Enter  {y}', y=node.get('git_prose') or ''))
        elif node.get('action') == 'color':
            hints.append(t('→ Enter  next palette'))
        if node.get('file') and not parent_level:
            hints.append(t('o  read {b}', b=node['file'].get('title') or ''))
        if hints:
            parts += [prose('')] + [prose(i, R['accent']) for i in hints]
        return Group(*parts)

    def description_r(self) -> Group:
        cap_amount = self.cap_amount()
        blocks = cap_amount.get('description') or ([('title', cap_amount['entry_name'])] + ([('section', t('Inside'))] + [('field', (c['entry_name'], c['summary'])) for c in cap_amount['child']]
                                                                     if cap_amount['child'] else []))
        return Group(*form(blocks, roomy=True))

    def compose(self) -> ComposeResult:
        yield Static(id='start')
        yield Static(id='menu')
        with Horizontal(id='inner'):
            with Vertical(id='left'):
                yield Static(id='list')
                yield Static(id='legend')
            with Vertical(id='right'):
                with Horizontal(id='parent-right'):
                    with Vertical(id='middle'):
                        yield Quiet(Static(id='detail-text'), id='detail')
                        yield Quiet(Static(id='context-text'), id='context')
                    yield Quiet(Static(id='description-text'), id='description')
                with Horizontal(id='sub'):
                    yield Static(id='live')
                    yield Static(id='fresh')
        yield Static(id='hint')

    def on_mount(self) -> None:
        self.palette_apply()
        self.draw()
        self.set_interval(1, self.tick)
        self.reload()
        self.set_interval(2, self.reload)
        if self.sample and self.simulation:
            self.set_interval(4, self.simulate)

    def on_unmount(self) -> None:
        if self.sample:
            design_sample.probes_remove()
            if self.home is not None:
                design_sample.clean(self.home)

    def simulate(self) -> None:
        if self.sim is not None:
            self.sim.step()

    def reload(self, force: bool = False) -> None:
        if self.busy:
            self.pending = self.pending or force
            return
        self.busy, self.pending = True, False
        self.run_worker(self.load, thread=True, group='data', exit_on_error=False)

    def measurement_refresh(self) -> None:
        if self.sample or os.environ.get('CADRE_NO_NET') == '1' or time.time() - self.last_measurement_request < 60:
            return
        home = self.home or panel_data.home_dir()
        if panel_data.budget_view(home)[1] >= 60:
            import screen_render
            self.last_measurement_request = time.time()
            screen_render.behind_refresh(home)

    def load(self) -> None:
        try:
            self.measurement_refresh()
            self.call_from_thread(self.root_change, panel_data.tree(self.home, agents=False))
            self.call_from_thread(self.root_change, panel_data.tree(self.home, agents=True))
        except Exception as exc:
            self.call_from_thread(self.loading_error, exc)
        finally:
            self.busy = False
            if self.pending:
                self.call_from_thread(self.reload)

    def loading_error(self, exc: Exception) -> None:
        text = panel_data.error_prose(exc)
        if text != self.last_error:
            self.last_error = text
            self.notify(t('Data could not be read: {e}', e=text), title='cadre', severity='error')

    def root_change(self, root: dict) -> None:
        ids = [self.ident(d) for d in self.chain()[1:]]
        selected = self.ident(self.selected)
        self.root, self.path, node, self.loaded = root, [], root, True
        for ident in ids:
            i = next((j for j, c in enumerate(node['child']) if self.ident(c) == ident), None)
            if i is None or not node['child'][i]['child']:
                break
            self.path.append(i)
            node = node['child'][i]
        self.choice = next((j for j, c in enumerate(node['child']) if self.ident(c) == selected), min(self.choice, max(len(node['child']) - 1, 0)))
        self.draw()

    def tick(self) -> None:
        if not self.query('#start'):
            return
        self.query_one('#start', Static).update(self.header_r())
        self.query_one('#live', Static).update(self.live_r())
        self.query_one('#hint', Static).update(self.hint_r())

    def active_jobs(self) -> list[dict]:
        return [j for j in self.section('tasks').get('child', []) if j.get('active')]

    def header_r(self) -> Text:
        pending = sum(bool(j.get('input_waiting')) for j in self.active_jobs())
        waiting = sum(bool(j.get('waiting')) and not j.get('input_waiting') for j in self.active_jobs())
        parts = ['cadre', t('{n} tasks running', n=len(self.active_jobs()) - pending - waiting) if self.loaded else '',
                    t('{n} tasks waiting', n=waiting) if waiting else '',
                    t('{n} task inputs awaiting planning', n=pending) if pending else '', self.section('budget').get('summary', ''),
                    t('Preview (sample data)') if self.sample else t('Live'), time.strftime('%H:%M:%S')]
        return single_line(prose('   ·   '.join(p for p in parts if p), R['muted']))

    def live_r(self) -> Group:
        active = self.active_jobs()
        if not active:
            return Group(prose(t('No task is running.'), R['muted']), prose(t('Press n for a new task.'), R['muted']))
        fitting = max(1, ((self.query_one('#live').size.height or 9) + 1) // 3)
        show = active if len(active) <= fitting else active[:max(fitting - 1, 1)]
        lines: list = []
        for order, j in enumerate(show):
            if order:
                lines.append(prose(''))
            def format(duration: int) -> str:
                return (t('{s} h {d:02d} min', s=duration // 3600, d=duration % 3600 // 60) if duration >= 3600
                        else t('{d} min {sec:02d} s', d=duration // 60, sec=duration % 60))
            hour = j.get('hour')
            duration_prose = (t('Work {c} · waiting {b}', c=format(int(hour['operation_sec'])), b=format(int(hour['wait_sec'])))
                         if isinstance(hour, dict) else format(int(time.time() - float(j.get('created') or time.time()))))
            waiting = j.get('input_waiting') or j.get('waiting')
            lines.append(single_line(Text.assemble(('○ ' if waiting else '◉ ', R['muted'] if waiting else R['accent']),
                                                   (j['entry_name'], f"bold {R['prose']}"), prose(f'   {duration_prose}', R['muted']))))
            sub = Text('  ')
            sub.append(str(j.get('stage_label') or ''), style=R['prose'])
            sub.append('   ')
            sub.append(str(j.get('now') or j.get('summary') or ''), style=R['muted'])
            lines.append(single_line(sub))
        if len(active) > len(show):
            lines.append(prose(t('… and {n} more tasks · Tasks', n=len(active) - len(show)), R['muted']))
        return Group(*lines)

    def context_r(self) -> tuple[Group | None, str]:
        candidates = [self.selected] + list(reversed(self.chain()[1:])) if self.path else [self.selected]
        source = next((d['context'] for d in candidates if d.get('context')), None)
        if not source:
            return None, t('Context')
        title = source[0][1] if source[0][0] == 'title' else t('Context')
        blocks = [('section', v) if round == 'title' else (round, v) for round, v in (source[1:] if source[0][0] == 'title' else source)]
        return Group(*form(blocks)), title

    def box(self, ident: str, title: str, enabled: bool = False) -> None:
        w = self.query_one(ident)
        color_ = R['accent'] if enabled else R['border']
        w.styles.border = ('round', color_)
        w.styles.border_title_color = R['accent'] if enabled else R['muted']
        w.styles.background = R['back']
        w.border_title = title

    def trace(self) -> str:
        names = [d['entry_name'] for d in self.chain()[1:]]
        text = '  ›  '.join(names)
        while len(text) > 36 and len(names) > 1:
            names.pop(0)
            text = '…  ›  ' + '  ›  '.join(names)
        return text if len(text) <= 36 else text[:35] + '…'

    def hint_r(self) -> Text:
        s = self.selected
        if not self.path:
            parts = [t('?  guide'), t('← →  section'), t('↓ Enter  open'), t('1–5  section'), t('n  new task'), t('Esc  quit')]
        else:
            sliding = self.query_one('#detail', Quiet).max_scroll_y > 0
            open = t('→ Enter  change') if s.get('action') else (t('→ Enter  open') if s['child'] or s.get('git') else '')
            parts = [t('?  guide'), t('↑ ↓  select'), open, t('← q  back'), t('o  read') if s.get('file') else '',
                        t('Shift+↑ ↓  details') if sliding else '', t('n  new task')]
            if s.get('active'):
                parts += [t('x  cancel'), t('i  live watch')]
            if s.get('delivery'):
                parts += [t('p  push'), t('m  merge')]
            parts.append(t('Esc  quit'))
        return single_line(prose('      '.join(p for p in parts if p), R['muted']))

    def draw(self) -> None:
        if not self.query('#start'):
            return
        while self.path and not self.elements:
            self.choice = self.path.pop()
        self.choice = min(self.choice, max(len(self.elements) - 1, 0))
        section = self.root['child'][self.section_no()]
        compact = not self.path and section.get('compact', False)
        self.query_one('#sub').display = not compact
        self.query_one('#start', Static).update(self.header_r())
        menu = self.query_one('#menu', Static)
        menu.display = not self.path
        menu.update(self.menu_r())
        height = max(6, self.query_one('#list').size.height or 30)
        if self.path:
            list, title = self.list_r(self.elements, self.choice, height), self.trace()
        else:
            list, title = self.list_r(section['child'], None, height), t('{b}  ·  Enter to open', b=section['entry_name'])
        hidden = self.section('tasks').get('hidden_test', 0)
        if hidden and self.path == [1]:
            title += ' · ' + t('{n} test tasks hidden', n=hidden)
        self.box('#list', title, enabled=bool(self.path))
        self.query_one('#list', Static).update(list)
        self.query_one('#legend', Static).update(Group(
            Text.assemble(('● ', R['good']), (t('Done') + '       ', R['muted']), ('◉ ', R['accent']), (t('Running'), R['muted'])),
            Text.assemble(('○ ', R['muted']), (t('Queued') + '      ', R['muted']), ('✗ ', R['bad']), (t('Problem'), R['muted'])),
            Text.assemble(('›  ', R['muted']), (t('Has items, Enter opens'), R['muted'])),
            Text.assemble(('→  ', R['muted']), (t('Enter jumps to the related place'), R['muted']))))
        self.box('#detail', t('Details'))
        self.query_one('#detail-text', Static).update(self.detail_r(self.selected, not self.path))
        context, b_title = self.context_r()
        self.query_one('#context').display = context is not None and not compact
        if context is not None:
            self.box('#context', b_title)
            self.query_one('#context-text', Static).update(context)
        self.box('#description', t('This menu'))
        self.query_one('#description-text', Static).update(self.description_r())
        k = min(max(len(self.active_jobs()), 1), 3 if self.size.height >= 36 else 1)
        self.query_one('#sub').styles.height = 2 + 3 * k
        self.box('#live', t('Open tasks') if any(j.get('input_waiting') or j.get('waiting') for j in self.active_jobs()) else t('Running now'))
        self.query_one('#live', Static).update(self.live_r())
        self.box('#fresh', t('New task'), enabled=True)
        self.query_one('#fresh', Static).update(Group(Text.assemble(('n', f"bold {R['accent']}"), ('   ' + t('Give a new task'), f"bold {R['prose']}")),
                                                     prose(t('Save the request and limits'), R['muted']), prose(t('Waits for the planning gate'), R['muted'])))
        self.query_one('#hint', Static).update(self.hint_r())

    def on_resize(self, event) -> None:
        wide = event.size.width >= 130
        self.query_one('#description').display = wide
        self.query_one('#fresh').display = wide
        self.query_one('#left').styles.width = 42 if wide else 36
        self.query_one('#middle').styles.margin = (0, 2 if wide else 0, 0, 0)
        self.query_one('#live').styles.margin = (0, 2 if wide else 0, 0, 0)
        self.call_after_refresh(self.draw)


if __name__ == '__main__':
    palette_setup()
    Cadre(sample='--sample' in sys.argv[1:], project_ref=os.environ.get('CADRE_PROJECT_REF', '')).run()
