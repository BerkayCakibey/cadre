"""C31: weekly reserve curve and difficulty-bound Claude arguments."""
from datetime import datetime, timedelta
import json
import os
import time
import unittest
from unittest.mock import patch

import test_kosu_sessions as fixtures
import qe_core as core
import butce


class ClaudePolicyTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.fake = self.f.claude_fixture()
        self.f.enter(patch.dict(os.environ, {'CLAUDE_BIN':str(self.fake)}))

    def budget(self, used=0):
        return {'ts':time.strftime('%Y-%m-%dT%H:%M:%S'), 'bugun':time.strftime('%Y-%m-%d'),
                'claude':{'kaynak':'taze','pay':40,'used7':used,'yuzde1_usd':10,
                          'havuz_kalan_usd':400,'pay_usd':0,'fable_bugun':0,'fable_attempts':[]}}

    def test_weekly_reserve_curve_and_tuesday_reset_boundary(self):
        # Item 4 (17.09): one formula — min(92.5, 100 - kalan_rezerv_gunu*7.5); the day table and MARGIN were removed.
        # Three examples given by the user: Wed 70%, Fri 85%, Mon 92.5%. Before Tuesday 19:00 it is now 92.5%, not 95%.
        for date, ceiling in [('2026-09-15T18:59:59',92.5), ('2026-09-15T19:00:00',62.5),
                              ('2026-09-16T23:00:00',70.0), ('2026-09-17T23:00:00',77.5),
                              ('2026-09-18T23:00:00',85.0), ('2026-09-19T23:00:00',85.0),
                              ('2026-09-20T23:00:00',85.0), ('2026-09-21T23:00:00',92.5)]:
            with self.subTest(date=date):
                now = datetime.fromisoformat(date)
                self.assertEqual(butce.claude_tavan(now), ceiling)
                self.assertEqual(butce.claude_pay(ceiling, 0, now), 0)
        self.assertEqual(100-butce.claude_tavan(datetime(2026,9,20,23)), 15.0)   # Paz: Pzt+Sal = 2 rezerv gunu x 7.5

    def test_weekly_budget_does_not_need_a_daily_usd_allowance(self):
        self.assertTrue(core.YON.butce_var('claude', 'claude/sonnet#high', self.budget())[0])

    def test_easy_task_cannot_launch_fable_or_max_even_if_requested(self):
        with patch.object(core, 'butce_json', return_value=self.budget()):
            core.run_claude(self.f.wt, 'claude/fable#max', 'small edit', self.f.log, difficulty=3).wait(timeout=5)
        call = json.loads((self.f.home/'claude-calls.jsonl').read_text())
        self.assertNotIn('fable', call)
        self.assertEqual(call[call.index('--effort')+1], 'high')

    def test_easy_or_medium_task_uses_sonnet_even_when_opus_is_requested(self):
        """06.10 smoke: the medium job's (difficulty 3) plan was made by opus, chosen by the router ($0.43); easy/medium work is sonnet#high."""
        with patch.object(core, 'butce_json', return_value=self.budget()):
            core.run_claude(self.f.wt, 'claude/opus#max', 'small plan', self.f.log, difficulty=3).wait(timeout=5)
        call = json.loads((self.f.home/'claude-calls.jsonl').read_text())
        self.assertEqual((call[call.index('--model')+1], call[call.index('--effort')+1]), ('sonnet', 'high'))

    def test_hard_task_can_use_fable_twice_then_falls_back_to_opus(self):
        with patch.object(core, 'butce_json', return_value=self.budget()):
            for _ in range(3):
                core.run_claude(self.f.wt, 'claude/fable#max', 'hard architecture', self.f.log, difficulty=5).wait(timeout=5)
        calls = [json.loads(line) for line in (self.f.home/'claude-calls.jsonl').read_text().splitlines()]
        self.assertEqual([a[a.index('--model')+1] for a in calls], ['fable','fable','opus'])
        self.assertTrue(all(a[a.index('--effort')+1] == 'max' for a in calls))

    def test_tuesday_reset_creates_a_new_reservation_period(self):
        from butce_kapisi import allowance
        before = datetime(2026,9,15,18,59)
        after = datetime(2026,9,15,19)
        b = self.budget()
        b.update(bugun=before.date().isoformat(), ts=before.isoformat())
        first = allowance(b, 'claude', now=before.timestamp())[0]
        b['ts'] = after.isoformat()
        second = allowance(b, 'claude', now=after.timestamp())[0]
        self.assertNotEqual(first, second)

    def test_the_ceiling_matches_the_three_numbers_the_user_gave(self):
        """Item 4: the user described the cap from their own usage — Wed 70-75%, Fri 85-90%, Mon 92-93%.
        The single formula must reproduce all three exactly and close the 100% hole before Tuesday 19:00."""
        from claude_politikasi import tavan, kalan_rezerv_gunu
        for tarih, beklenen, gun in [('2026-09-16T21:02:00', 70.0, 4),    # Carsamba gece
                                     ('2026-09-18T21:00:00', 85.0, 2),    # Cuma gece
                                     ('2026-09-21T10:00:00', 92.5, 1)]:   # Monday morning
            now = datetime.fromisoformat(tarih)
            with self.subTest(tarih=tarih):
                self.assertEqual(kalan_rezerv_gunu(now), gun)
                self.assertEqual(tavan(now), beklenen)
        # Just before the Tuesday reset: no reserve days left but the cap still does not exceed 92.5
        self.assertEqual(tavan(datetime(2026, 9, 22, 10)), 92.5)
        self.assertEqual(tavan(datetime(2026, 9, 15, 18, 59)), 92.5)
        # At the reset the week starts over: 5 reserve days, the lowest cap
        self.assertEqual(tavan(datetime(2026, 9, 15, 19, 0)), 62.5)
        # On the weekend it holds a reserve for Mon+Tue
        self.assertEqual(tavan(datetime(2026, 9, 19, 12)), 85.0)

    def test_the_ceiling_never_leaves_the_user_less_than_seven_and_a_half_points_per_workday(self):
        from claude_politikasi import tavan, kalan_rezerv_gunu, TAVAN_UST, GUN_BASI_REZERV
        now = datetime(2026, 9, 15, 19)
        for _ in range(8 * 24):                       # every hour of a week
            t = tavan(now)
            self.assertLessEqual(t, TAVAN_UST)
            self.assertEqual(t, min(TAVAN_UST, 100 - kalan_rezerv_gunu(now) * GUN_BASI_REZERV))
            self.assertGreaterEqual(100 - t, 0)
            now += timedelta(hours=1)

    # --- B2/B3 (15.09): stale observation compensation and session counter -------------------------------------------------
    def test_stale_observation_costs_one_margin_step_and_expires_after_a_reset_period(self):
        from claude_politikasi import pay, STALE_LIMIT_MINUTES
        wednesday = datetime(2026, 9, 16, 23)            # Item 4: cap 70 (4 reserve days x 7.5)
        self.assertEqual(pay(30, 10, wednesday), 40)      # taze
        self.assertEqual(pay(30, 10, wednesday, stale_minutes=600), 35)   # stale: -5 margin, but NOT ZERO
        self.assertEqual(pay(30, 10, wednesday, stale_minutes=STALE_LIMIT_MINUTES + 1), 0)  # unknown after one reset period
        self.assertEqual(pay(30, 10, wednesday, stale_minutes=float('nan')), 0)
        self.assertEqual(pay(30, 85, wednesday, stale_minutes=5), 0)      # still 0 if the 5-hour window is full

    def test_global_user_weekly_limit_precedes_the_engine_reserve_curve(self):
        from claude_politikasi import pay, tavan
        monday = datetime(2026, 9, 21, 23)
        self.assertEqual(tavan(monday), 92.5)
        self.assertEqual(pay(89, 0, monday), 1)
        self.assertEqual(pay(90, 0, monday), 0)
        self.assertEqual(pay(91, 0, monday), 0)
        self.assertEqual(pay(0, 85, monday), 0)

    def test_allowance_accepts_compensated_stale_observation_but_not_expired_one(self):
        from butce_kapisi import allowance, BudgetDenied
        stale = self.budget()
        stale['claude'].update(kaynak='bayat(600dk)', gecerli=True)
        self.assertGreater(allowance(stale, 'claude')[1], 0)
        expired = self.budget()
        expired['claude'].update(kaynak='bayat(20000dk)', gecerli=False)
        with self.assertRaises(BudgetDenied):
            allowance(expired, 'claude')

    def test_process_count_uses_executable_basename_never_below_one(self):
        from claude_politikasi import oturum_say
        self.assertEqual(oturum_say(['/opt/homebrew/Caskroom/claude-code@latest/2.1.272/claude', 'node', 'claude', 'claude-helper', '/usr/bin/ps']), 2)
        self.assertEqual(oturum_say([]), 1)
        self.assertEqual(oturum_say(['bash', 'python3']), 1)

    def test_budget_report_compensates_stale_observation_with_own_ledger(self):
        """butce.py's real main(): observation 10 hours stale -> share = cap - 5 - (observed + own ledger); if the observation is before the reset
        only own spending after the reset counts; an 8-day observation is invalid. Fake home directory, no network."""
        import io
        from pathlib import Path
        import butce
        from claude_politikasi import last_reset, tavan
        home = self.f.home
        (home/'.claude').mkdir()
        vq = home/'.claude/claude_quota.json'
        vq.write_text(json.dumps({'rate_limits':{'seven_day':{'used_percentage':30},'five_hour':{'used_percentage':10}}}))
        (home/'zincir/claude-havuz-usd.txt').write_text('3000\n')   # %1 = $30
        fixed = datetime(2026, 9, 17, 3, 0, 0)                      # Thursday 03:00 -> cap 66
        reset = last_reset(fixed.astimezone())
        self.assertEqual(reset, datetime(2026, 9, 15, 19).astimezone(reset.tzinfo))
        stamp = lambda dt: dt.strftime('%Y-%m-%dT%H:%M:%S')
        ledger = home/'zincir/claude-harcama.jsonl'
        ledger.write_text('\n'.join(json.dumps(r) for r in [
            {'ts':stamp(datetime(2026,9,13,12)), 'model':'claude/opus#max', 'usd':300.0},   # previous week: counted in no scenario
            {'ts':stamp(datetime(2026,9,16,12)), 'model':'claude/opus#max', 'usd':60.0},    # after the reset, before the 10-hour observation
            {'ts':stamp(datetime(2026,9,16,20)), 'model':'claude/sonnet#high', 'usd':30.0}, # after the observation
            {'ts':stamp(datetime(2026,9,17,1)), 'model':'claude/sonnet#high', 'usd':30.0},  # after the observation
        ]) + '\n')

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(fixed.timestamp(), tz) if tz else cls.fromtimestamp(fixed.timestamp())

        def report(observation_age_hours):
            observed = fixed.timestamp() - observation_age_hours*3600
            os.utime(vq, (observed, observed))
            out = io.StringIO()
            env = {k:v for k,v in os.environ.items() if not k.startswith('CADRE_TEST_') and k != 'CADRE_SIM_YUK'}
            with patch.dict(os.environ, env, clear=True), patch.object(butce, 'QE', home), patch.object(butce, 'DB', home/'missing.db'), \
                 patch.object(Path, 'home', return_value=home), \
                 patch.object(butce, 'oturum_say', return_value=1), \
                 patch.object(butce.datetime, 'datetime', FixedDateTime), patch('sys.stdout', out):
                # B41: the session count came from the real `ps`; with a second Claude Code session open the share was halved and the test broke (K19).
                butce.main()
            return json.loads(out.getvalue())['claude']

        stale = report(10)   # Wednesday 17:00 observation: after the reset, 600 min stale
        self.assertTrue(stale['gecerli'])
        self.assertFalse(stale['gozlem_onceki_hafta'])
        self.assertEqual(stale['used7'], 32.0)                       # 30 gozlenen + 2 x $30/$30
        self.assertEqual(stale['pay'], tavan(fixed) - 5 - 32)        # 29: stale margin, not zero
        self.assertEqual(stale['kullanilabilir_usd'], (tavan(fixed) - 5 - 32) * 30)
        self.assertTrue(stale['kaynak'].startswith('bayat('))

        previous = report(3*24)   # Sunday 03:00 observation: BEFORE the last weekly reset
        self.assertTrue(previous['gozlem_onceki_hafta'])
        self.assertEqual(previous['used7'], 4.0)                     # only the $120 / $30 after the reset
        self.assertEqual(previous['pay'], tavan(fixed) - 5 - 4)

        expired = report(8*24)
        self.assertFalse(expired['gecerli'])
        self.assertEqual(expired['pay'], 0)


if __name__ == '__main__':
    unittest.main()
