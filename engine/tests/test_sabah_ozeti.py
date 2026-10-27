"""Item 10 — the morning summary. The user's rule: the state is read only with `cadre`. Information produced overnight
was scattered across log lines and run.json fields; there was no single answer in the morning to "what happened, how much was spent, where did it get stuck".
The summary computes nothing, it only collects — and suppresses the noise."""

from __future__ import annotations

import json
import os
from pathlib import Path
from contextlib import closing
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import sabah_ozeti  # noqa: E402
from kosu import RunStore  # noqa: E402

BUTCE = {
    'go': {'db': 'ok', 'pay': 77.0, 'kullanilabilir': 50.0, 'ay_puan': 27.0, 'pencere_acik': True, 'pencere_neden': 'acik',
           'pencereler': {'rolling': 3, 'weekly': 12, 'monthly': 27, 'resets': {'monthly': '2026-10-02T19:17Z'}}, 'birim': 'puan'},
    'or': {'pay': 0.9, 'kullanilabilir': 0.9, 'kalan': 4.0},
    'zen': {'pay': 0.35, 'kullanilabilir': 0.3, 'kalan': 8.39},
    'claude': {'pay': 4.8, 'used7': 63.0, 'tavan': 77.5},
}


class OzetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-ozet-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'projects/p').mkdir(parents=True)
        (self.home / 'projects/p/repo-path').write_text(str(self.home))
        (self.home / 'zincir').mkdir(exist_ok=True)
        self.store = RunStore(self.home)

    def kosu(self, state, harcama=0.0, denials=None, phase='motor'):
        r = self.store.accept('p', 'is')
        self.store.update(r['run_id'], state=state, budget_spent_usd=harcama, phase=phase,
                          **({'denials': denials} if denials else {}))
        return r['run_id']

    def uret(self):
        with patch.object(sabah_ozeti, 'butce', return_value=BUTCE):
            return sabah_ozeti.uret(self.home, ayrinti=True)

    def test_runs_that_spent_nothing_are_collapsed_into_one_line(self):
        for _ in range(5):
            self.kosu('cancelled')
        calisan = self.kosu('completed', harcama=0.356)
        metin = self.uret()
        self.assertIn('(5 koşu hiçbir şey harcamadan iptal edildi)', metin)
        self.assertIn(calisan[:19], metin)
        self.assertIn('toplam harcanan $0.356', metin)

    def test_a_cancelled_run_that_did_spend_is_still_shown(self):
        self.kosu('cancelled', harcama=0.435)
        metin = self.uret()
        self.assertIn('$0.435', metin)
        self.assertNotIn('harcamadan iptal', metin)

    def test_denials_are_carried_so_the_morning_shows_what_blocked_work(self):
        self.kosu('failed', harcama=0.02, denials=[{'provider': 'opencode-go', 'reason': 'allocation exhausted'}])
        metin = self.uret()
        self.assertIn('red: opencode-go', metin)
        self.assertIn('allocation exhausted', metin)

    def test_the_three_windows_and_every_pool_are_reported(self):
        self.kosu('completed', harcama=0.1)
        metin = self.uret()
        self.assertIn('rolling %3', metin)
        self.assertIn('weekly %12', metin)
        self.assertIn('monthly %27', metin)
        self.assertIn('monthly reset: 2026-10-02T19:17Z', metin)
        for ad in ('Go:', 'OpenRouter:', 'Zen:', 'Claude:'):
            self.assertIn(ad, metin)
        self.assertIn('bakiye $8.39', metin)

    def test_stale_reservations_appear_under_attention(self):
        db = self.home / 'zincir/request-budget.db'
        with closing(sqlite3.connect(db)) as con, con:
            con.execute('create table reservation(id text primary key, provider text, period text, created real, '
                        'reserved integer, actual integer, receipt text, metadata text)')
            con.execute('insert into reservation values(?,?,?,?,?,null,null,?)',
                        ('a', 'opencode-go', 'p', time.time() - 9 * 3600, 1_230_000, '{}'))
        metin = self.uret()
        self.assertIn('1 askıda rezervasyon ($1.23)', metin)
        self.assertIn('cadre kurtar', metin)

    def test_a_closed_window_is_called_out(self):
        self.kosu('completed')
        kapali = json.loads(json.dumps(BUTCE))
        kapali['go']['pencere_acik'] = False
        kapali['go']['pencere_neden'] = 'monthly %100 — havuz kapali'
        with patch.object(sabah_ozeti, 'butce', return_value=kapali):
            metin = sabah_ozeti.uret(self.home, ayrinti=True)
        self.assertIn('Go penceresi kapalı', metin)
        self.assertIn('havuz kapali', metin)

    def test_writing_produces_a_dated_file(self):
        self.kosu('completed', harcama=0.5)
        with patch.object(sabah_ozeti, 'butce', return_value=BUTCE):
            yol = sabah_ozeti.yaz(self.home)
        self.assertTrue(yol.is_file())
        self.assertIn(time.strftime('%Y%m%d'), yol.name)
        self.assertIn('Gece özeti', yol.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
