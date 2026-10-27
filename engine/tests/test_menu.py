"""17.09 user request: when typing `cadre` ALL subcommands show up in the menu, chosen with the arrow keys.
The menu runs in a real pseudo-terminal (termios/key reading cannot be tested through a pipe)."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PTY = Path(__file__).with_name('pty_run.py')

EXPECTED = ['Give a new task', 'Reopen the screen', 'Cancel the job', 'Night summary', 'Job record folder',
            'Push the product', 'Merge into the canonical branch', 'Recovery', 'Enter/view balance', 'Quit']   # 4c-11: radar was removed


class MenuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-menu-', ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        (self.home / 'chain').mkdir(parents=True)
        self.root = Path(self.temp.name) / 'root'
        (self.root / 'bin').mkdir(parents=True)      # empty bin: the status screen must not start guards
        (self.root / 'lib').symlink_to(ROOT / 'lib')

    def menu(self, duration, keys):
        env = {**os.environ, 'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(self.root)}
        env.pop('CADRE_RUN_ID', None)
        r = subprocess.run([sys.executable, str(PTY), str(duration), keys, '--', 'bash', str(ROOT / 'bin/cadre.sh')],
                           cwd=self.temp.name, env=env, capture_output=True, text=True, timeout=duration + 30)
        return r.stdout

    def test_every_command_is_on_the_menu(self):
        output = self.menu(8, '3:q')
        for entry_name in EXPECTED:
            with self.subTest(entry_name=entry_name):
                self.assertIn(entry_name, output)
        self.assertIn('↑/↓ to select, Enter to confirm', output)

    def test_arrow_navigation_selects_the_highlighted_row(self):
        # j = down (the arrow key equivalent); down 3 times -> line 4 "Night summary", selected with Enter
        output = self.menu(15, '3:j,4:j,5:j,6:\r,12:q')
        self.assertIn('Night summary', output)
        self.assertIn('Night summary — ', output, 'the summary must really have been produced (title line)')

    def test_the_recovery_submenu_lists_its_own_choices(self):
        output = self.menu(17, '3:8,6:1,11:q')          # 8 = Recovery, 1 = list
        self.assertIn('List stuck reservations', output)
        self.assertIn('Clear price failure', output)
        self.assertIn('No pending reservation', output)

    def _menu_order(self, label):
        """The ORDER NUMBER of a menu entry is read from the code. On 19.09, when a new entry was inserted, this test
        broke because it hard-coded '10'; the number is not written by hand, it is derived from the list in `cadre.sh`."""
        import re
        source = (Path(__file__).resolve().parents[1] / 'bin/cadre.sh').read_text()
        block = source.split('local option; local -a ops=(', 1)[1].split('"Quit")', 1)[0]
        inputs = re.findall(r'"([^"]+)"', block)
        for i, g in enumerate(inputs, start=1):
            if label in g:
                return i
        self.fail(f'menu entry {label!r} is absent: {inputs}')


    def test_the_opencode_budget_view_shows_both_pools(self):
        """B124 addition: the Go subscription (expiring allowance) and credit/zen (non-expiring balance) shown separately on one screen."""
        n = self._menu_order('Enter/view balance')
        output = self.menu(15, f'3:{n},11:q')
        self.assertIn('GO (subscription', output)
        self.assertIn('CREDIT / ZEN', output)
        self.assertIn('BURNS', output, 'the screen explains the difference between the two pools')

    def test_quitting_touches_nothing(self):
        output = self.menu(9, f"3:{self._menu_order('Enter/view balance') + 1}")   # Quit is always the last entry
        self.assertIn('Quit', output)
        self.assertFalse((self.home / 'runs').exists())


class TTYOutsideTests(unittest.TestCase):
    """17.09 audit: the guide said "without a TTY, help is printed" while the code entered the task flow.
    The menu's behaviour outside a TTY and the subcommand names are now bound by tests."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-menu-tty-', ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        (self.home / 'chain').mkdir(parents=True)
        self.root = Path(self.temp.name) / 'root'
        (self.root / 'bin').mkdir(parents=True)
        (self.root / 'lib').symlink_to(ROOT / 'lib')

    def execute_run(self, *args, input=''):
        env = {**os.environ, 'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(self.root)}
        env.pop('CADRE_RUN_ID', None)
        return subprocess.run(['bash', str(ROOT / 'bin/cadre.sh'), *args], cwd=self.temp.name, env=env,
                              input=input, capture_output=True, text=True, timeout=30)

    def test_without_a_terminal_it_takes_a_task_instead_of_printing_help(self):
        r = self.execute_run(input='')
        self.assertNotIn('↑/↓ to select', r.stdout)              # the menu did not open
        self.assertNotIn('Give a new task', r.stdout)
        # the task flow ran: it stops there because there is no project record
        self.assertIn('project_ref', (r.stdout + r.stderr).lower())

    def test_help_is_reachable_by_name_and_stop_is_not_a_command(self):
        self.assertIn('cadre', self.execute_run('help').stdout.lower())
        r = self.execute_run('stop')
        self.assertEqual(r.returncode, 2)
        self.assertIn('unknown: stop', r.stderr)
        self.assertIn('cancel', r.stderr, 'the error names the command that stops the job')


if __name__ == '__main__':
    unittest.main()
