"""D14: raw records are optional; state and money come from measurement."""
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import ekran
import izle
import sabah_ozeti


class EkranTests(unittest.TestCase):
    def test_summary_does_not_mark_failed_work_as_good_or_unknown_money_as_zero(self):
        text = ekran.kosu_ozeti({'state': 'failed', 'tasks': {'a': {'state': 'uretti'}, 'b': {}}})
        self.assertIn('1/2 parça', text)
        self.assertIn('durdu', text)
        self.assertIn('ölçüm yok', text)
        self.assertNotIn('sorun yok', text)
        self.assertNotIn('$0.00', ekran.butce_ozeti({}))

    def test_harness_steps_are_not_counted_as_plan_parts(self):
        """07.10 smoke: a finished 2-task run showed '2/5 parts done' (plan/qa/kalite steps were counted as parts)."""
        text = ekran.kosu_ozeti({'state': 'completed', 'budget_spent_usd': 0.68, 'tasks': {
            'plan': {}, 'g1': {'state': 'uretti'}, 'g2': {'state': 'uretti'}, 'qa': {'qa_result': {}}, 'kalite': {},
            'duzeltme-1': {}}})
        self.assertIn('2/2 parça', text)
        self.assertIn('sorun yok', text)

    def test_an_unapproved_spending_alarm_is_the_status(self):
        """4b (07.10): a spending alarm waits for user approval; the status line shows it and the approval command."""
        kayit = {'state': 'running', 'run_id': 'r1', 'budget_spent_usd': 1.3, 'tasks': {'g1': {'state': 'running'}},
                 'harcama_alarmi': {'ts': 20.0, 'neden': "son 10 dk $1.30 > tavanin %40'i ($1.20)"}, 'harcama_onayi_ts': 10.0}
        self.assertIn('harcama alarmı', ekran.kosu_ozeti(kayit))
        self.assertIn('cadre harcama-onay r1', ekran.kosu_ozeti(kayit))
        self.assertNotIn('harcama alarmı', ekran.kosu_ozeti({**kayit, 'harcama_onayi_ts': 30.0}))

    def test_unicode_wrapping_preserves_text_and_terminal_width(self):
        original = 'Türkçe e\u0301 中文 ' * 30
        lines = ekran.sar(original, 19)
        self.assertEqual(''.join(lines), original)
        self.assertTrue(all(ekran.genislik(line) <= 19 for line in lines))
        self.assertNotIn('\x1b', ekran.temiz('\x1b[2Jmetin\x1b]0;kotu\x07'))

    def test_details_replay_without_counting_events_twice(self):
        viewer = izle.Izleyici('test', '', duz=True)
        out = io.StringIO()
        event = {'type': 'tool', 'tool': 'bash', 'state': {'status': 'completed',
                 'input': {'command': 'printf internal-command'}, 'output': 'raw-output'}}
        with redirect_stdout(out):
            viewer.part_ciz(None, {}, event, '')
            viewer.part_ciz(None, {}, {'type': 'reasoning', 'text': 'raw-reasoning'}, '')
        self.assertIn('komut çalıştırma tamamlandı', out.getvalue())
        for text in ('internal-command', 'raw-output', 'raw-reasoning'):
            self.assertNotIn(text, out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            viewer.ayrinti_degistir()
            viewer.ayrinti_degistir()
        for text in ('internal-command', 'raw-output', 'raw-reasoning'):
            self.assertIn(text, out.getvalue())
        self.assertEqual(viewer.say['arac'], 1)
        self.assertFalse(viewer.ayrinti)

    def test_running_tool_is_not_shown_as_success_even_in_single_frame(self):
        viewer = izle.Izleyici('test', '', duz=True, tek=True)
        out = io.StringIO()
        with redirect_stdout(out):
            viewer.part_ciz(None, {}, {'type': 'tool', 'tool': 'bash', 'state': {'status': 'running'}}, '')
        self.assertIn('sürüyor', out.getvalue())
        self.assertNotIn('✔', out.getvalue())

    def test_motor_start_is_quiet_and_failure_has_a_human_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'motor.log'
            log.write_text('2026-10-04 10:00:00 kosu basliyor RAW\n2026-10-04 10:00:01 kosu dustu (rc=9)\n')
            viewer = izle.Izleyici('test', '', duz=True, motor_log=str(log))
            out = io.StringIO()
            with redirect_stdout(out):
                viewer.motor_tur()
            self.assertNotIn('RAW', out.getvalue())
            self.assertNotIn('rc=9', out.getvalue())
            self.assertIn('iş durdu', out.getvalue())
            with redirect_stdout(out):
                viewer.ayrinti_degistir()
            self.assertIn('RAW', out.getvalue())

    def test_morning_summary_hides_ids_but_keeps_failures_and_openrouter(self):
        record = {'run_id': 'private-id', 'project': 'demo', 'state': 'failed', 'budget_spent_usd': .25}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(sabah_ozeti, 'kosular', return_value=[record]), \
                patch.object(sabah_ozeti, 'butce', return_value={'or': {'kullanilabilir': 2}}):
            text = sabah_ozeti.uret(Path(directory))
        self.assertIn('1 iş durdu', text)
        self.assertIn('OR $2.00', text)
        self.assertNotIn('private-id', text)
        self.assertNotIn('sorun yok', text)
        self.assertNotIn('gece yarısı', text)

    def test_stale_budget_is_unknown_and_status_does_not_query_the_network(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'zincir').mkdir()
            path = home / 'zincir/request-budget.json'
            path.write_text(json.dumps({'observed': time.time() - 90, 'budget': {'go': {}}}))
            self.assertEqual(sabah_ozeti.butce(home), {})
            path.write_text(json.dumps({'observed': time.time(), 'budget': {'go': {'kullanilabilir': 1}}}))
            self.assertEqual(sabah_ozeti.butce(home)['go']['kullanilabilir'], 1)


class AcilisHiziTests(unittest.TestCase):
    """07.10: opening `cadre` took 5-15 s; the status line ran butce.py synchronously and waited for the network (measured 3-8 s)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name); (self.home / 'zincir').mkdir()

    def yaz(self, yas):
        (self.home / 'zincir/request-budget.json').write_text(json.dumps(
            {'observed': time.time() - yas, 'budget': {'go': {'kullanilabilir': 3}}}))

    def test_an_old_observation_is_shown_with_its_age_and_refreshed_in_the_background(self):
        self.yaz(300)
        with patch.object(ekran, 'butce_taze') as esli, patch.object(ekran, 'arkada_tazele') as arka:
            butce, yas = ekran.butce_gorunumu(self.home)
        self.assertEqual(butce['go']['kullanilabilir'], 3)
        self.assertGreaterEqual(yas, 300)
        esli.assert_not_called(); arka.assert_called_once()

    def test_a_fresh_observation_needs_no_refresh_and_no_observation_is_measured_once(self):
        self.yaz(5)
        with patch.object(ekran, 'butce_taze') as esli, patch.object(ekran, 'arkada_tazele') as arka:
            ekran.butce_gorunumu(self.home)
        esli.assert_not_called(); arka.assert_not_called()
        (self.home / 'zincir/request-budget.json').unlink()
        with patch.object(ekran, 'butce_taze', return_value={'go': {}}) as esli:
            self.assertEqual(ekran.butce_gorunumu(self.home), ({'go': {}}, 0.0))
        esli.assert_called_once()

    def test_a_run_without_its_worktree_costs_no_git_call(self):
        import sonuc
        kayit = {'state': 'completed', 'worktree': str(self.home / 'silinmis')}
        with patch.object(sonuc, 'approved', return_value=True) as onay:
            self.assertEqual(ekran.teslim_bekleyen([kayit]), 0)
        onay.assert_not_called()
        (self.home / 'silinmis').mkdir()
        with patch.object(sonuc, 'approved', return_value=True):
            self.assertEqual(ekran.teslim_bekleyen([kayit]), 1)


if __name__ == '__main__':
    unittest.main()
