"""17.09 user request: when typing `cadre` ALL subcommands show up in the menu, chosen with the arrow keys.
The menu runs in a real pseudo-terminal (termios/key reading cannot be tested through a pipe)."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

KOK = Path(__file__).resolve().parents[1]
PTY = Path(__file__).with_name('pty_kos.py')

BEKLENEN = ['Yeni görev ver', 'Ekranı geri aç', 'İşi iptal et', 'Gece özeti', 'İş kaydı klasörü',
            'Ürünü push et', 'Kanonik dala merge et', 'Kurtarma', 'Bakiye gir/gör', 'Çık']   # 4c-11: radar was removed


class MenuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-menu-', ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        (self.home / 'zincir').mkdir(parents=True)
        self.root = Path(self.temp.name) / 'kok'
        (self.root / 'bin').mkdir(parents=True)      # empty bin: the status screen must not start guards
        (self.root / 'lib').symlink_to(KOK / 'lib')

    def menu(self, sure, tuslar):
        env = {**os.environ, 'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(self.root)}
        env.pop('CADRE_RUN_ID', None)
        r = subprocess.run([sys.executable, str(PTY), str(sure), tuslar, '--', 'bash', str(KOK / 'bin/cadre.sh')],
                           cwd=self.temp.name, env=env, capture_output=True, text=True, timeout=sure + 30)
        return r.stdout

    def test_every_command_is_on_the_menu(self):
        cikti = self.menu(8, '3:q')
        for ad in BEKLENEN:
            with self.subTest(ad=ad):
                self.assertIn(ad, cikti)
        self.assertIn('↑/↓ ile seç, Enter ile onayla', cikti)

    def test_arrow_navigation_selects_the_highlighted_row(self):
        # j = down (the arrow key equivalent); down 3 times -> line 4 "Night summary", selected with Enter
        cikti = self.menu(15, '3:j,4:j,5:j,6:\r,12:q')
        self.assertIn('Gece özeti', cikti)
        self.assertIn('Gece özeti — ', cikti, 'ozet gercekten uretilmis olmali (baslik satiri)')

    def test_the_recovery_submenu_lists_its_own_choices(self):
        cikti = self.menu(17, '3:8,6:1,11:q')          # 8 = Recovery, 1 = list
        self.assertIn('Askıda rezervasyonları listele', cikti)
        self.assertIn('Fiyat arızasını temizle', cikti)
        self.assertIn('askida rezervasyon yok', cikti.replace('ı', 'i'))

    def _menu_sirasi(self, etiket):
        """The ORDER NUMBER of a menu entry is read from the code. On 19.09, when a new entry was inserted, this test
        broke because it hard-coded '10'; the number is not written by hand, it is derived from the list in `cadre.sh`."""
        import re
        kaynak = (Path(__file__).resolve().parents[1] / 'bin/cadre.sh').read_text()
        blok = kaynak.split('local secenek; local -a ops=(', 1)[1].split('"Çık")', 1)[0]
        girdiler = re.findall(r'"([^"]+)"', blok)
        for i, g in enumerate(girdiler, start=1):
            if etiket in g:
                return i
        self.fail(f'menude {etiket!r} girdisi yok: {girdiler}')


    def test_the_opencode_budget_view_shows_both_pools(self):
        """B124 addition: the Go subscription (expiring allowance) and credit/zen (non-expiring balance) shown separately on one screen."""
        n = self._menu_sirasi('Bakiye gir/gör')
        cikti = self.menu(15, f'3:{n},11:q')
        self.assertIn('GO (abonelik', cikti)
        self.assertIn('KREDİ / ZEN', cikti)
        self.assertIn('YANAR', cikti, 'iki havuzun farki ekranda yazmali')

    def test_quitting_touches_nothing(self):
        cikti = self.menu(9, f"3:{self._menu_sirasi('Bakiye gir/gör') + 1}")   # Quit is always the last entry
        self.assertIn('Çık', cikti)
        self.assertFalse((self.home / 'runs').exists())


class TTYDisiTests(unittest.TestCase):
    """17.09 audit: the guide said "without a TTY, help is printed" while the code entered the task flow.
    The menu's behaviour outside a TTY and the subcommand names are now bound by tests."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-menu-tty-', ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        (self.home / 'zincir').mkdir(parents=True)
        self.root = Path(self.temp.name) / 'kok'
        (self.root / 'bin').mkdir(parents=True)
        (self.root / 'lib').symlink_to(KOK / 'lib')

    def kos(self, *args, girdi=''):
        env = {**os.environ, 'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(self.root)}
        env.pop('CADRE_RUN_ID', None)
        return subprocess.run(['bash', str(KOK / 'bin/cadre.sh'), *args], cwd=self.temp.name, env=env,
                              input=girdi, capture_output=True, text=True, timeout=30)

    def test_without_a_terminal_it_takes_a_task_instead_of_printing_help(self):
        r = self.kos(girdi='')
        self.assertNotIn('↑/↓ ile seç', r.stdout)              # the menu did not open
        self.assertNotIn('Yeni görev ver', r.stdout)
        # the task flow ran: it stops there because there is no project record
        self.assertIn('proje', (r.stdout + r.stderr).lower())

    def test_help_is_reachable_by_name_and_dur_is_not_a_command(self):
        self.assertIn('cadre', self.kos('yardim').stdout.lower())
        r = self.kos('dur')
        self.assertEqual(r.returncode, 2)
        self.assertIn('bilinmeyen: dur', r.stderr)
        self.assertIn('iptal', r.stderr, 'isi durduran komutun adi hata metninde gecmeli')


if __name__ == '__main__':
    unittest.main()
