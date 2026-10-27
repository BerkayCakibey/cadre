"""Item 7 — pool ordering: budget surplus + the CURRENT UNIT PRICE, with hysteresis.

Because the order was recomputed on every call, even the day's first spending could flip the order and change model
in the middle of a job. Hysteresis: a change must be both CLEAR and SUSTAINED. (DeepSeek's peak hours are in Go accounting since 4c-1.)"""

from __future__ import annotations

import calendar
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))


def utc(iso):
    return calendar.timegm(time.strptime(iso, '%Y-%m-%d %H:%M'))


class YogunSaatTests(unittest.TestCase):
    """4c-1: the direct DeepSeek route was removed; peak hours moved to Go accounting (Go charges 2× points for DeepSeek models)."""

    def test_the_peak_window_matches_the_official_table(self):
        import go_pencere
        for iso, peak in [('2026-09-17 00:59', False), ('2026-09-17 01:00', True), ('2026-09-17 03:59', True),
                          ('2026-09-17 04:00', False), ('2026-09-17 05:59', False), ('2026-09-17 06:00', True),
                          ('2026-09-17 09:59', True), ('2026-09-17 10:00', False),
                          ('2026-09-19 02:00', False), ('2026-09-20 07:00', False)]:   # hafta sonu hep off-peak
            with self.subTest(iso=iso):
                self.assertEqual(go_pencere.yogun_saat(utc(iso)), peak)

    def test_only_go_deepseek_models_double_at_peak(self):
        import go_pencere
        peak = utc('2026-09-17 02:00')
        self.assertEqual(go_pencere.zaman_carpani('deepseek-v4-pro', peak), 2)
        self.assertEqual(go_pencere.zaman_carpani('deepseek-v4-pro', utc('2026-09-17 12:00')), 1)
        for model in ('glm-5.3', 'qwen3.8-max', 'kimi-k3'):
            self.assertEqual(go_pencere.zaman_carpani(model, peak), 1)


class HisterezisTests(unittest.TestCase):
    """An order change must be both CLEAR and SUSTAINED; the abundance measure is pinned here (the topic is hysteresis)."""
    ZEN, GO = 'opencode/deepseek-v4-flash', 'opencode-go/glm-5.2'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-sira-')
        self.addCleanup(self.temp.cleanup)
        ev = Path(self.temp.name)
        self.eski = {k: os.environ.get(k) for k in ('CADRE_YON_DURUM', 'CADRE_YON_KAYIT')}
        os.environ['CADRE_YON_DURUM'] = str(ev / 'durum.json')
        os.environ['CADRE_YON_KAYIT'] = os.devnull
        self.addCleanup(self.geri)
        import importlib
        from unittest.mock import patch
        import yonlendirici
        importlib.reload(yonlendirici)
        self.y = yonlendirici
        bolluk = patch.object(self.y, 'bolluk', side_effect=lambda sag, b: (b or {}).get(sag, 1.0))
        bolluk.start(); self.addCleanup(bolluk.stop)

    def geri(self):
        for k, v in self.eski.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def butce(self, zen_oran=1.0):
        return {'opencode': zen_oran, 'opencode-go': 1.0}

    def test_the_first_reading_is_taken_at_face_value(self):
        """Without a memory there is no CHANGE to damp: the current measure is applied directly."""
        d = {}
        sirali = self.y.bolluga_gore_sirala([self.ZEN, self.GO], self.butce(zen_oran=0.01), d, utc('2026-09-17 12:00'))
        self.assertEqual(sirali[0], self.GO)
        self.assertTrue(d['_sira']['opencode']['sikisik'])

    def test_an_established_order_does_not_flip_on_a_brief_change(self):
        d = {}
        t0 = utc('2026-09-17 12:00')
        adaylar = [self.ZEN, self.GO]
        self.y.bolluga_gore_sirala(adaylar, self.butce(), d, t0)                     # before: plentiful, normal order
        self.assertFalse(d['_sira']['opencode']['sikisik'])
        erken = self.y.bolluga_gore_sirala(adaylar, self.butce(zen_oran=0.01), d, t0 + 60)
        self.assertEqual(erken, adaylar, 'bir dakikalik dusus sirayi devirmemeli')
        # the timer is set when the NEW TREND is first seen (t0+60); five minutes later it fills
        gec = self.y.bolluga_gore_sirala(adaylar, self.butce(zen_oran=0.01), d, t0 + 361)
        self.assertEqual(gec[0], self.GO, 'bes dakika surduyse sira degisir')

    def test_a_change_that_is_not_clear_enough_never_flips(self):
        """A value wandering just below the threshold (0.25 -> 0.20) does not flip the order; the difference must be at least 0.20 points."""
        d = {}
        t0 = utc('2026-09-17 12:00')
        adaylar = [self.ZEN, self.GO]
        self.y.bolluga_gore_sirala(adaylar, self.butce(), d, t0)
        for gecen in (60, 301, 900):
            sirali = self.y.bolluga_gore_sirala(adaylar, self.butce(zen_oran=0.20), d, t0 + gecen)
            self.assertEqual(sirali, adaylar, f'{gecen}s sonra bile belirgin degil')

    def test_the_memory_survives_a_new_process(self):
        """If the hysteresis memory is not written to disk every call is the 'first call' and hysteresis never works."""
        from unittest.mock import patch
        t0 = utc('2026-09-17 12:00')
        with patch.object(self.y, 'butce_var', return_value=(True, 'test')), \
             patch.object(self.y, 'cooldownda', return_value=False):
            self.y.zincir('impl', b=self.butce(), d=self.y.durum_oku(), simdi=t0)
        kayitli = json.loads(Path(os.environ['CADRE_YON_DURUM']).read_text())
        self.assertIn('_sira', kayitli)
        self.assertLessEqual({'opencode-go', 'openrouter'}, set(kayitli['_sira']), 'kod zincirinin ayni kattaki yollari')


if __name__ == '__main__':
    unittest.main()


class ArizaSiniflandirmaTests(unittest.TestCase):
    """B110 (18.09): `_ariza_kilitli` counted a CONTEXT error and a NETWORK outage as quota too, with a fixed "rate/limit/too large" string;
    the provider closed for 60 minutes. A context error means our request is too big,
    a network error is temporary; neither has anything to do with the provider's quota."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-ariza-')
        self.addCleanup(self.temp.cleanup)
        os.environ['CADRE_HOME'] = self.temp.name
        import yonlendirici
        self.y = yonlendirici

    def sinif(self, hata):
        durum = self.y._ariza_kilitli('saglayici', hata, d={}, simdi=1000.0)['saglayici']
        return durum['cooldown_bitis'], durum['son']

    def test_real_quota_signals_still_close_the_provider_for_an_hour(self):
        for hata in ('429 Too Many Requests', 'You exceeded your current quota',
                     'Request too large for model on tokens per minute (TPM)', 'daily limit reached'):
            with self.subTest(hata=hata):
                bitis, son = self.sinif(hata)
                self.assertEqual(bitis, 1000.0 + 3600)
                self.assertIn('[kota]', son)

    def test_context_and_network_errors_are_not_quota_and_do_not_close_the_provider(self):
        for hata, sinif in (('context length limit exceeded: 40000 > 32768', '[baglam]'),
                            ('prompt is too long', '[baglam]'),
                            ('connection timed out after 30s', '[ag]'),
                            ('503 Service Unavailable', '[ag]'),
                            ('fetch failed', '[ag]')):
            with self.subTest(hata=hata):
                bitis, son = self.sinif(hata)
                self.assertEqual(bitis, 0, f'{hata}: 60 dk kapatma kota disinda olmamali')
                self.assertIn(sinif, son)
