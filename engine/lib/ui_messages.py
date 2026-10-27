"""Panel preferences and message helpers for operator-facing text."""
from __future__ import annotations

import os
from pathlib import Path


def home_dir() -> Path:
    return Path(os.environ.get('CADRE_HOME') or Path.home() / '.local/share/cadre-engineering')


COLORS = ('Gruvbox', 'Everforest', 'Rosé Pine', 'Tokyo Night', 'Dracula', 'Catppuccin Mocha')


def color_file() -> Path:
    return home_dir() / 'panel-color'


def color_read() -> str:
    choice = os.environ.get('CADRE_COLOR', '').strip()
    if choice not in COLORS:
        try:
            choice = color_file().read_text().strip()
        except (OSError, UnicodeError):
            choice = ''
    return choice if choice in COLORS else COLORS[0]


def color_write(choice: str) -> str:
    choice = choice if choice in COLORS else COLORS[0]
    color_file().parent.mkdir(parents=True, exist_ok=True)
    color_file().write_text(choice + '\n')
    return choice


def tests_read(home: Path | None = None) -> bool:
    try:
        return ((home or home_dir()) / 'panel-tests').read_text().strip() == '1'
    except (OSError, UnicodeError):
        return False


def tests_write(show: bool, home: Path | None = None) -> None:
    path = (home or home_dir()) / 'panel-tests'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('1\n' if show else '0\n')


def t(text: str, **values: object) -> str:
    if values and str(values.get('n', values.get('f'))) == '1':
        text = SINGULAR.get(text, text)
    return text.format(**values) if values else text


SINGULAR: dict[str, str] = {
    '… {n} more tasks · Tasks': '… {n} more task · Tasks',
    '… {n} more alerts · Attention': '… {n} more alert · Attention',
    '{n} records': '{n} record',
    '{n} requests in an unknown unit, amount unknown': '{n} request in an unknown unit, amount unknown',
    '{n} requests in an unknown unit': '{n} request in an unknown unit',
    'Parent and {n} children · all attempts': 'Parent and {n} child · all attempts',
    '{n} samples': '{n} sample',
    '{n} samples · {b} malformed': '{n} sample · {b} malformed',
    '{k} accepted / {n} samples · {t} fresh · {m}': '{k} accepted / {n} sample · {t} fresh · {m}',
    '{n} test tasks hidden': '{n} test task hidden',
    '{n} days ago': '{n} day ago',
    '{f} files · +{e} −{s}': '{f} file · +{e} −{s}',
    '{n} subtasks · {b} done': '{n} subtask · {b} done',
    '{n} events': '{n} event',
    '{n} parts · {g} passed': '{n} part · {g} passed',
    '{n} roles': '{n} role',
    '{n} settings': '{n} setting',
    'Today {n} tasks': 'Today {n} task',
    '{n} tasks': '{n} task',
    '{n} agents': '{n} agent',
    '{n} items': '{n} item',
    '{n} tasks running': '{n} task running',
    '{n} tasks waiting': '{n} task waiting',
    '{n} task inputs awaiting planning': '{n} task input awaiting planning',
    '{n} revisions': '{n} revision',
    '{n} points left': '{n} point left',
    '{n} attempts': '{n} attempt',
    '{n} groups': '{n} group',
    '{n} requests': '{n} request',
    '{n} parts': '{n} part',
    '(JSON output · {n} characters)': '(JSON output · {n} character)',
    '(tool output · {n} characters)': '(tool output · {n} character)',
    'In {a}/{n} requests': 'In {a}/{n} request',
    'Measured in {a}/{n} requests': 'Measured in {a}/{n} request',
    '{n} points': '{n} point',
    '{n} requests · {p}': '{n} request · {p}',
    '… and {n} more tasks · Tasks': '… and {n} more task · Tasks',
    '{p} · {n} requests': '{p} · {n} request',
    '… and {n} more lines': '… and {n} more line',
}
