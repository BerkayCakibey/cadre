"""23.09 afternoon audit — defects of three fixes written the same day:
B183 if the judge output had two contradictory valid JSONs, the first one found won (draft PASS + final REVISE → wrong acceptance);
B184 the fast-failure guard also counted the engine's own interruptions and budget-denial exits as failures, the error count did not look at
the session id; B185 the loop guard counted a single pytest output twice, also counted the model's own text, and could produce a wrong id
on a half line. No model/network; the engine tests are the real `_runner` + a fake executor."""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import qe_core as core  # noqa: E402
import outcome  # noqa: E402
import test_run_sessions as fixtures  # noqa: E402
from test_night_scenarios import budget, chain_fake  # noqa: E402

D = 'a' * 64
PASS = '{"revision": "%s", "verdict": "PASS", "acceptance": [{"id": "main", "status": "PASS", "evidence": "tests/x.py:1 initial check passes"}]}' % D
REVISE = '{"revision": "%s", "verdict": "REVISE", "acceptance": [{"id": "main", "status": "FAIL", "evidence": "tests/x.py:9 edge case fails"}]}' % D


class JudgeContradictionTest(unittest.TestCase):  # B183
    def test_two_contradicting_fenced_decisions_are_an_error_not_acceptance(self):
        text = "Draft:\n```json\n" + PASS + "\n```\nFinal:\n```json\n" + REVISE + "\n```\n"
        r = outcome.parse_review(text, {'digest': D}, ['main'])
        self.assertEqual(r['status'], 'ERROR', r)
        self.assertIn('conflicting', r['reason'])

    def test_contradiction_between_unfenced_draft_and_fenced_final_is_also_an_error(self):
        text = "Once: " + PASS + "\nThen:\n```json\n" + REVISE + "\n```\n"
        self.assertEqual(outcome.parse_review(text, {'digest': D}, ['main'])['status'], 'ERROR')

    def test_same_decision_written_twice_stays_valid(self):
        text = "Decision:\n```json\n" + PASS + "\n```\nSummary: " + PASS + "\n"
        r = outcome.parse_review(text, {'digest': D}, ['main'])
        self.assertEqual((r['status'], r['verdict']), ('PASS', 'PASS'))


class FastDropCountTest(unittest.TestCase):  # B184 (class)
    def test_controlled_exit_neither_counts_nor_breaks_the_series(self):
        hd = core.FastDrop(threshold_sec=60, threshold=3)
        hd.save(rc=1, duration_sec=10, start=1.0); hd.save(rc=1, duration_sec=10, start=20.0)
        self.assertEqual(hd.save(rc=-15, duration_sec=5, start=30.0, controlled=True), 0)
        self.assertEqual(hd.series, 2, 'A controlled interruption does not reset the streak')
        self.assertEqual(hd.save(rc=1, duration_sec=10, start=40.0), 3)
        self.assertTrue(hd.loop)
        self.assertEqual(hd.series_start, 1.0)


class FastDropRunnerTest(unittest.TestCase):  # B184 (real engine wiring)
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.enter(patch.object(core, 'project_memory', return_value=[]))
        self.f.enter(patch.object(core.ROUTER, 'chain', side_effect=chain_fake))
        self.f.enter(patch.object(core, 'budget_json', return_value=budget()))
        self.f.enter(patch.object(core, 'tool_running', return_value=False))

    def runner_log(self):
        return (self.f.store.task_dir(self.f.rid, 'g1') / 'runner.log').read_text()

    def attempt_count(self):
        return len(list((self.f.store.task_dir(self.f.rid, 'g1') / 'attempts').glob('*/attempt.json')))

    def test_budget_rejection_exits_do_not_count_as_fast_drops(self):
        self.f.fake.write_text(f"#!{sys.executable}\n" + """
import json, os, pathlib, sys
p = pathlib.Path(os.environ['CADRE_HOME']); n = len(list(p.glob('attempt-*'))) + 1; (p / f'attempt-{n}').write_text('x')
args = sys.argv[1:]; sid = args[args.index('--session') + 1] if '--session' in args else 'ses_b'
print(json.dumps({'type': 'step_start', 'sessionID': sid, 'part': {'type': 'step-start'}}), flush=True)
if n <= 3:   # Three consecutive attempts end promptly in budget denial
    a = os.environ['CADRE_BUDGET_ATTEMPT']; d = pathlib.Path(os.environ['CADRE_REQUEST_DIR']).parent
    (d / 'budget-denied.json').write_text(json.dumps({'attempt_id': a, 'providers': ['opencode-go'],
        'refusals': [{'provider': 'opencode-go', 'scope': 'provider', 'reason': 'allocation exhausted'}]}))
    sys.exit(1)
print(json.dumps({'type': 'text', 'sessionID': sid, 'part': {'type': 'text', 'text': 'finished'}}), flush=True)
""")
        rc, _gate = self.f.runner_fixture(accept=True, env={'CADRE_MAX_RESUME': '6'})
        # P2/P3 (03.10): a budget denial becomes 'retryable' — the engine STOPS with a budget wait (no retry is burned),
        # the 4th attempt is never reached in the same run. FAST-FAILURE must not trigger at all.
        self.assertNotEqual(rc, 0)
        log = self.runner_log()
        self.assertIn('BUDGET: the attempt record', log)
        self.assertNotIn('FAST-DROP', log, 'A budget denial is controlled engine behavior, not a failure streak')

    def test_consecutive_fast_crashes_are_cut_as_a_series(self):
        self.f.fake.write_text(f"#!{sys.executable}\n" + """
import json, sys
args = sys.argv[1:]; sid = args[args.index('--session') + 1] if '--session' in args else 'ses_c'
print(json.dumps({'type': 'step_start', 'sessionID': sid, 'part': {'type': 'step-start'}}), flush=True)
sys.exit(1)
""")
        rc, _gate = self.f.runner_fixture(accept=True, env={'CADRE_MAX_RESUME': '12'})
        self.assertNotEqual(rc, 0)
        self.assertIn('FAST-DROP', self.runner_log())
        self.assertLess(self.attempt_count(), 13, 'Consecutive attempts enter the stall path before exhausting the attempt limit')


class TestLoopSingleTest(unittest.TestCase):  # B185
    def setUp(self):
        self.logf = Path(tempfile.mkdtemp(prefix='loop-single-')) / 'runner.log'
        self.logf.write_text('')

    def write(self, s):
        with self.logf.open('a') as f:
            f.write(s)

    def test_two_matches_in_the_same_event_count_as_one_execution(self):
        d = core.TestLoop(str(self.logf), threshold=3)
        event = '{"type":"tool_use","part":{"state":{"output":"FAILED tests/a.py::t - x","metadata":{"output":"FAILED tests/a.py::t - x"}}}}\n'
        self.write(event * 2)
        self.assertEqual(d.tick(), '', 'Two pytest executions and four matches do not reach the threshold of three')
        self.write(event)
        self.assertEqual(d.tick(), 'tests/a.py::t')

    def test_the_models_own_text_does_not_count(self):
        d = core.TestLoop(str(self.logf), threshold=3)
        self.write('{"type":"text","part":{"text":"FAILED tests/a.py::t still broken"}}\n' * 5)
        self.assertEqual(d.tick(), '')

    def test_half_line_with_a_full_id_counts_once(self):
        d = core.TestLoop(str(self.logf), threshold=2)
        self.write('FAILED tests/unit/test_cli.py::test_cli_ver')
        self.assertEqual(d.tick(), '')
        self.write('sion_uses - AssertionError\n')
        self.assertEqual(d.tick(), '')
        self.write('FAILED tests/unit/test_cli.py::test_cli_version_uses - AssertionError\n')
        self.assertEqual(d.tick(), 'tests/unit/test_cli.py::test_cli_version_uses')

    def test_default_threshold_is_five_runs(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CADRE_TEST_LOOP_THRESHOLD', None)
            self.assertEqual(core.StallGuard(str(self.logf), None, 9999).loop.threshold, 5)


class ToolRepeatTest(unittest.TestCase):  # B200
    """B200 (23.09 R2-medium): the local model called the same bash command with the same output 6+ times ~23 s apart; G-F9 only counted the
    breaking test, the artefact guard waited 45 min. Identical consecutive tool calls at the threshold (default 4) are a loop."""
    def setUp(self):
        self.logf = Path(tempfile.mkdtemp(prefix='tool-repeat-')) / 'runner.log'
        self.logf.write_text('')

    def call(self, command='python3 -c "print(1)"', output='1', tool='bash'):
        return json.dumps({'type': 'tool_use', 'part': {'tool': tool, 'state': {'status': 'completed',
                           'input': {'command': command}, 'output': output}}}, separators=(',', ':')) + '\n'   # the opencode log writes compactly

    def write(self, s):
        with self.logf.open('a') as f:
            f.write(s)

    def test_four_identical_consecutive_calls_is_a_loop(self):
        d = core.ToolRepeat(str(self.logf), threshold=4)
        self.write(self.call() * 3)
        self.assertEqual(d.tick(), '')
        self.write(self.call())
        self.assertEqual(d.tick(), 'bash')

    def test_different_output_or_an_intervening_call_breaks_the_series(self):
        d = core.ToolRepeat(str(self.logf), threshold=3)
        self.write(self.call(output='1') + self.call(output='2') + self.call(output='2'))
        self.assertEqual(d.tick(), '')
        self.write(self.call(command='ls') + self.call(output='2') + self.call(output='2'))
        self.assertEqual(d.tick(), '')

    def test_model_text_and_a_half_line_do_not_count(self):
        d = core.ToolRepeat(str(self.logf), threshold=2)
        self.write(self.call() + '{"type":"text","part":{"text":"x"}}\n')
        full = self.call()
        self.write(full[:20])
        self.assertEqual(d.tick(), '')
        self.write(full[20:])
        self.assertEqual(d.tick(), 'bash', 'A text line does not break the streak; count a partial line once completed')

    def test_guard_default_threshold_is_five_and_tied_to_the_loop_decision(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CADRE_TOOL_REPEAT_THRESHOLD', None)
            self.assertEqual(core.StallGuard(str(self.logf), None, 9999).repeat.threshold, 5)


class BudgetRejectionNarrowTest(unittest.TestCase):  # B187
    def test_a_long_plan_text_containing_the_phrase_is_not_a_rejection(self):
        plan = "## ITEMS\n" + ("- Fix CADRE_BUDGET_DENIED logging; description of the remaining item.\n" * 20)
        self.assertFalse(core.budget_rejection_text(plan))

    def test_carrier_json_even_if_long_the_result_field_is_a_short_rejection(self):
        carrier = json.dumps({'type': 'result', 'is_error': True, 'result': 'Failed to authenticate. API Error: 403 CADRE_BUDGET_DENIED',
                              'usage': {'input_tokens': 0, 'output_tokens': 0, 'padding': 'x' * 900}})
        self.assertTrue(core.budget_rejection_text(carrier))
        self.assertTrue(core.budget_rejection_text("Failed to authenticate. API Error: 403 CADRE_BUDGET_DENIED"))


class ControlledCutCapTest(unittest.TestCase):  # B188
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.enter(patch.object(core, 'project_memory', return_value=[]))
        self.f.enter(patch.object(core.ROUTER, 'chain', side_effect=chain_fake))
        self.f.enter(patch.object(core, 'budget_json', return_value=budget()))
        self.f.enter(patch.object(core, 'tool_running', return_value=False))

    def test_never_ending_budget_rejection_series_hits_the_attempt_limit(self):
        import threading
        self.f.fake.write_text(f"#!{sys.executable}\n" + """
import json, os, pathlib, sys
p = pathlib.Path(os.environ['CADRE_HOME'])
args = sys.argv[1:]; sid = args[args.index('--session') + 1] if '--session' in args else 'ses_t'
print(json.dumps({'type': 'step_start', 'sessionID': sid, 'part': {'type': 'step-start'}}), flush=True)
a = os.environ['CADRE_BUDGET_ATTEMPT']; d = pathlib.Path(os.environ['CADRE_REQUEST_DIR']).parent
(d / 'budget-denied.json').write_text(json.dumps({'attempt_id': a, 'providers': ['opencode-go'],
    'refusals': [{'provider': 'opencode-go', 'scope': 'provider', 'reason': 'allocation exhausted'}]}))
sys.exit(1)
""")
        outcome_box = {}
        thread = threading.Thread(target=lambda: outcome_box.update(rc=self.f.runner_fixture(
            accept=True, env={'CADRE_MAX_RESUME': '2', 'CADRE_CONTROLLED_CAP': '3'})[0]), daemon=True)
        thread.start(); thread.join(timeout=60)
        self.assertFalse(thread.is_alive(), 'If every attempt ends in a budget denial, the engine would loop forever (B188)')
        self.assertNotEqual(outcome_box.get('rc'), 0)
        # P2/P3 (03.10): budget denial -> budget wait -> retryable (the endless loop is broken this way).
        self.assertIn('BUDGET WAIT', (self.f.store.task_dir(self.f.rid, 'g1') / 'runner.log').read_text())


class VerificationCountTest(unittest.TestCase):  # B158(d)
    def test_assert_in_a_python_comment_and_string_does_not_count(self):
        text = ("def test_x():\n    assert f() == 1\n    assert g() == 2\n"
                 "    # assert h() == 3\n    # assert h() == 4\n    # assert h() == 5\n"
                 "    s = 'assert inside string'\n    \"\"\"assert inside docstring\"\"\"\n")
        self.assertEqual(core.verification_count(text, 'tests/test_x.py'), 2)

    def test_js_line_and_block_comments_are_not_counted(self):
        text = "test('x', () => {\n  expect(a).toBe(1);\n  // expect(b).toBe(2);\n  /* expect(c).toBe(3); */\n});\n"
        self.assertEqual(core.verification_count(text, 'web/x.test.js'), 1)


class ExcludeIdTest(unittest.TestCase):  # B158(e)
    def test_product_id_changes_if_info_exclude_changes(self):
        import subprocess as sp
        wt = Path(tempfile.mkdtemp(prefix='exclude-'))
        g = lambda *a: sp.run(['git', '-C', str(wt), '-c', 'user.email=t@t', '-c', 'user.name=t', *a], check=True, capture_output=True)
        g('init', '-q'); (wt / 'a.py').write_text('x = 1\n'); g('add', 'a.py'); g('commit', '-qm', 'base')
        once = outcome.revision(wt)['digest']
        exclude = wt / '.git' / 'info' / 'exclude'; exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open('a') as f:
            f.write('fresh.py\n')
        (wt / 'fresh.py').write_text('y = 2\n')   # a file written and hidden in the same step
        self.assertNotEqual(outcome.revision(wt)['digest'], once, 'Hiding a file must not produce the same id: QA sees it while delivery does not')


class HelperFixture:
    """Shared setup; the test method is inherited, not discovered a second time."""
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.enter(patch.object(core, 'project_memory', return_value=[]))
        self.f.enter(patch.object(core.ROUTER, 'chain', side_effect=chain_fake))
        self.f.enter(patch.object(core, 'budget_json', return_value=budget()))

    def helpers(self):
        env = dict(os.environ)
        core.opencode_budget_env(env, ['task'])
        return {entry_name: str(line.get('model', '')) for entry_name, line in json.loads(env['OPENCODE_CONFIG_CONTENT'])['agent'].items()}


class TaskBanHelperTest(HelperFixture, unittest.TestCase):  # B148
    """N-RT04: a task ban also closes the provider of the helper candidates."""

    def test_task_ban_reaches_helper_selection(self):
        import budget_gate
        once = self.helpers()
        self.assertTrue(any(m.startswith('opencode-go/') for m in once.values()), f'Precondition: Go is selected without restrictions {once}')
        budget_gate.task_bans(self.f.home).block('opencode-go', 'budget', budget())
        then = self.helpers()
        self.assertFalse(any(m.startswith('opencode-go/') for m in then.values()), then)



class SubAgentlessTest(HelperFixture, unittest.TestCase):  # B196 + 4c-9/10
    """B196 (23.09 R1-medium-v2): the plan did not ask for a sub-agent for the lane, the prompt said "do NOT hand off with task()"; the helpers were still open and
    the executor called cadre-review 7 times and cadre-plan once. 4c-9: no sub-agent at all in the dispatcher lane and the judge
    (`CADRE_SUB_AGENTLESS=1`); 4c-10: compaction is always the lane's own model."""
    def agents(self, sub_agentless):
        env = dict(os.environ)
        env.pop('CADRE_SUB_AGENTLESS', None)
        if sub_agentless:
            env['CADRE_SUB_AGENTLESS'] = '1'
        args = core.opencode_budget_env(env, ['task'])
        return json.loads(env['OPENCODE_CONFIG_CONTENT'])['agent'], args[args.index('-m') + 1]

    def test_no_sub_agent_in_the_lane_compaction_is_the_lanes_model(self):
        a, model = self.agents(True)
        for entry_name in ('cadre-plan', 'cadre-impl', 'cadre-review'):
            self.assertTrue(a[entry_name].get('disable'), (entry_name, a[entry_name]))
        self.assertNotIn('cadre-scout', a)
        self.assertEqual((a['compaction'].get('model'), a['compaction'].get('disable')), (model, False))

    def test_single_runner_behavior_is_unchanged_without_the_variable(self):
        a, model = self.agents(False)
        self.assertFalse(a['cadre-plan'].get('disable'), a['cadre-plan'])
        self.assertTrue(a['cadre-review'].get('disable'), a['cadre-review'])   # 4b (07.10): the intermediate review was removed
        self.assertEqual(a['compaction'].get('model'), model)

class ReservationCalibratedTest(unittest.TestCase):  # B198
    """B198 (user decision 23.09): the allocation was 7-9 times the real cost (deepseek allocated ~$0.07 per request and spent ~$0.009);
    a $0.10 daily share covered ~4 requests a day. 2x the p95 of the provider's real/worst-case ratio over the last 7 days is allocated
    (at least 30 receipts). The ratio is computed against the WORST case; against the calibrated allocation, feedback would push the coefficient to 1."""
    def setUp(self):
        import budget_gate
        self.t = Path(tempfile.mkdtemp(prefix='calibrated-'))
        self.ledger = budget_gate.BudgetLedger(self.t / 'budget.db')
        self.BudgetDenied = budget_gate.BudgetDenied

    def snapshot(self, amount=5):
        import time
        return {'today': time.strftime('%Y-%m-%d'), 'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
                'go': {'db': 'ok', 'pay': (amount) * 100 / 15, 'available': (amount) * 100 / 15, 'window_open': True, 'unit': 'points'}}

    def history(self, ratios, worst=1_000_000, reserved=None, meta=None, provider='opencode-go'):
        import sqlite3, time, uuid
        self.ledger.reserve('opencode-go', 1, self.snapshot(), {})   # creates the tables
        once = time.time() - 2 * 86400
        from contextlib import closing
        with closing(sqlite3.connect(self.t / 'budget.db')) as con, con:
            for i, ratio in enumerate(ratios):
                con.execute('insert into reservation values(?,?,?,?,?,?,?,?)', (uuid.uuid4().hex, provider, time.strftime('%Y-%m-%d', time.localtime(once)),
                            once + i, reserved or worst, int(worst * ratio), uuid.uuid4().hex, json.dumps(meta or {})))

    def allocated(self, rid):
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.t / 'budget.db')) as con:
            r, m = con.execute('select reserved, metadata from reservation where id=?', (rid,)).fetchone()
        return r, json.loads(m)

    def test_with_few_receipts_the_worst_case_is_reserved(self):
        self.history([0.05] * 10)
        self.assertEqual(self.allocated(self.ledger.reserve('opencode-go', 1_000_000, self.snapshot(), {}))[0], 1_000_000)

    def test_with_enough_receipts_twice_the_p95_is_reserved_in_the_worst_record(self):
        self.history([0.2] * 3 + [0.05] * 37)
        r, m = self.allocated(self.ledger.reserve('opencode-go', 1_000_000, self.snapshot(), {}))
        self.assertEqual((r, m.get('worst')), (400_000, 1_000_000))

    def test_claude_calibrates_with_ten_receipts_others_need_thirty(self):
        """Claude threshold (25.09, user approval): Claude could not reach 30 receipts in 7 days (23); because it allocated the worst case ($3.6-4.6)
        most requests did not fit the run pocket and were denied, and since they did not fit no receipts accumulated. Claude calibrates with 10 receipts."""
        import sqlite3
        from contextlib import closing
        self.history([0.1] * 9, provider='claude')
        self.history([0.1] * 12)
        with closing(sqlite3.connect(self.t / 'budget.db')) as con:
            self.assertEqual(self.ledger.calibrated(con, 'claude', 1_000_000), 1_000_000, 'Nine receipts use the worst-case status')
            self.assertEqual(self.ledger.calibrated(con, 'opencode-go', 1_000_000), 1_000_000, 'The threshold stays 30 for the others')
        self.history([0.1] * 3, provider='claude')
        with closing(sqlite3.connect(self.t / 'budget.db')) as con:
            self.assertEqual(self.ledger.calibrated(con, 'claude', 1_000_000), 200_000, '12 receipt: p95 x 2')

    def test_calibrated_divergences_do_not_push_the_coefficient_to_one_by_feedback(self):
        self.history([0.2] * 3 + [0.05] * 37)
        self.history([0.2] * 3 + [0.05] * 37, reserved=400_000, meta={'worst': 1_000_000})   # the same distribution; against the allocated amount p95 would be 0.5
        self.assertEqual(self.allocated(self.ledger.reserve('opencode-go', 1_000_000, self.snapshot(), {}))[0], 400_000)

    def test_small_daily_share_accepts_the_job_with_a_calibrated_reservation(self):
        self.history([0.2] * 3 + [0.05] * 37, worst=70_000)
        once = self.ledger.reserve('opencode-go', 36_000, self.snapshot(0.1), {})
        self.ledger.settle(once, 36_000, 'today-1')
        self.ledger.reserve('opencode-go', 70_000, self.snapshot(0.1), {})   # the worst case of 70k would not fit: 36k + 70k > 100k

    def test_overrun_below_the_worst_limit_writes_no_price_error_above_it_does(self):
        import sqlite3
        self.history([0.2] * 3 + [0.05] * 37)
        a = self.ledger.reserve('opencode-go', 1_000_000, self.snapshot(), {})      # 400k allocated, limit 1M
        self.ledger.settle(a, 700_000, 'overrun-limit-under')
        self.assertEqual(self.allocated(a)[0], 700_000, 'Do not hide an overrun; record the actual amount')
        self.ledger.reserve('opencode-go', 1_000_000, self.snapshot(), {})          # the provider was not locked
        b = self.ledger.reserve('opencode-go', 1_000_000, self.snapshot(), {})
        self.ledger.settle(b, 1_200_000, 'limit-top')
        from contextlib import closing
        with closing(sqlite3.connect(self.t / 'budget.db')) as con:
            self.assertTrue(con.execute("select 1 from pricing_fault where provider='opencode-go'").fetchone(), 'Exceeding the worst-case bound is a pricing fault')


class HardCapTest(unittest.TestCase):  # B199
    """B199 (user decision 23.09): easy/medium work at the project cap ($2), hard work $5 — the Claude plan never fit in a $2 cap
    (reserve ~$3 > lane share $1.50). The cap is written at acceptance, the difficulty is known from the dispatcher's pre-filter; the environment variable wins."""
    def setUp(self):
        import run_store
        self.run = run_store
        self.home = Path(tempfile.mkdtemp(prefix='hard-cap-'))
        (self.home / 'projects' / 'p').mkdir(parents=True)
        (self.home / 'projects' / 'p' / 'run-cap-usd').write_text('2.0\n')
        self.store = run_store.RunStore(self.home)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CADRE_RUN_CAP_USD', None)
            self.rid = self.store.accept('p', 'task')['run_id']

    def test_on_a_hard_job_the_cap_rises_default_five_dollars(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CADRE_RUN_CAP_USD', None)
            self.assertEqual(self.run.hard_cap_apply(self.store, self.rid, 'p'), (2.0, 2.0))
        self.assertEqual(self.store.read(self.rid)['budget_cap_usd'], 2.0)

    def test_project_hard_cap_and_an_already_high_cap(self):
        (self.home / 'projects' / 'p' / 'run-cap-hard-usd').write_text('4\n')
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CADRE_RUN_CAP_USD', None)
            self.assertEqual(self.run.hard_cap_apply(self.store, self.rid, 'p'), (2.0, 2.0))
            self.assertEqual(self.run.hard_cap_apply(self.store, self.rid, 'p'), (2.0, 2.0))

    def test_in_a_fileless_project_easy_medium_default_is_two_dollars(self):
        (self.home / 'projects' / 'q').mkdir()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CADRE_RUN_CAP_USD', None)
            self.assertEqual(self.run.run_cap_usd(self.home, 'q'), 2.0)

    def test_command_line_prints_the_old_and_new_cap(self):
        import subprocess as sp
        env = {k: v for k, v in os.environ.items() if k != 'CADRE_RUN_CAP_USD'}
        env['CADRE_HOME'] = str(self.home)
        r = sp.run([sys.executable, str(LIB / 'run_store.py'), 'hard-cap', self.rid, 'p'], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, '2.00 2.00'), r.stderr[-300:])

    def test_environment_variable_is_left_alone_if_given(self):
        with patch.dict(os.environ, {'CADRE_RUN_CAP_USD': '1.5'}):
            self.assertEqual(self.run.hard_cap_apply(self.store, self.rid, 'p'), (2.0, 2.0))
        self.assertEqual(self.store.read(self.rid)['budget_cap_usd'], 2.0)


class CatalogRingTest(unittest.TestCase):  # B161 (401/not-in-account cell)
    def test_model_not_in_the_account_does_not_enter_the_chain_reason_in_the_record(self):
        import model_catalog
        import model_router
        target = model_router.ROLES['impl'][0]
        with patch.object(model_catalog, 'available', side_effect=lambda provider_key, mid: f'{provider_key}/{mid}' != target):
            chain, reason = model_router.chain('impl', 'middle', 30000, b=budget(), d={})
        self.assertNotIn(target, chain)
        self.assertIn(f'{target}: unavailable on this account', reason)


class GuardOwnerTest(unittest.TestCase):  # B191
    """B191 (23.09 pre-check): guards of interrupted test runs lived for 3–7 days; the watch revived capacity-watch in its own home
    and asked DeepSeek on every tick with the real auth.json key. When the owner process dies the guard exits; no network in test mode."""

    def setUp(self):
        import subprocess as sp
        import time
        self.sp, self.t = sp, Path(tempfile.mkdtemp(prefix='b191-'))
        (self.t / 'home/.local/share/opencode').mkdir(parents=True)
        (self.t / 'home/.local/share/opencode/auth.json').write_text(json.dumps({'deepseek': {'key': 'fake-key'}}))
        (self.t / 'qe/reports').mkdir(parents=True)
        (self.t / 'qe/reports' / time.strftime('SUMMARY-%Y%m%d.md')).write_text('ready\n')   # the morning summary is skipped
        (self.t / 'bin').mkdir()
        (self.t / 'bin/curl').write_text(f'#!/bin/sh\necho "$@" >> "{self.t / "curl-call"}"\n')
        (self.t / 'bin/curl').chmod(0o755)
        self.owner = sp.Popen(['sleep', '120'])
        self.addCleanup(self.clean)
        self.env = dict(os.environ, HOME=str(self.t / 'home'), CADRE_HOME=str(self.t / 'qe'), CADRE_ENGINE_ROOT=str(LIB.parent),
                        PATH=f"{self.t / 'bin'}:{os.environ['PATH']}", CADRE_WATCH_INTERVAL='1', CADRE_QUOTA_INTERVAL='1',
                        CADRE_GUARD_OWNER_PID=str(self.owner.pid), OPENCODE_DB=str(self.t / 'absent.db'), CADRE_NO_NET='1')

    def clean(self):
        import signal
        self.owner.kill()
        self.owner.wait()
        for entry_name in ('health-watch', 'capacity-watch'):
            try:
                pid = int((self.t / 'qe/chain' / f'{entry_name}.pid').read_text())
                os.killpg(os.getpgid(pid), signal.SIGKILL)   # B266: the sleeping child dies too
            except (OSError, ValueError):
                pass

    def live(self, entry_name):
        try:
            os.kill(int((self.t / 'qe/chain' / f'{entry_name}.pid').read_text()), 0)
            return True
        except (OSError, ValueError):
            return False

    def wait(self, condition, sec=30):
        import time
        last = time.time() + sec
        while time.time() < last:
            if condition():
                return True
            time.sleep(0.2)
        return False

    def start(self, entry_name):
        process = self.sp.Popen(['bash', str(LIB.parent / f'bin/{entry_name}.sh')], env=self.env, stdout=self.sp.DEVNULL,
                              stderr=self.sp.DEVNULL, start_new_session=True)
        self.addCleanup(lambda: process.poll() is None and os.killpg(process.pid, signal.SIGKILL))   # B266
        return process

    def test_guard_sleep_is_not_orphaned_even_if_it_dies_by_sigkill(self):
        """B266 (29.09): when the guard died with SIGKILL (tests kill it that way) the foreground `sleep 3600` stayed for hours with PPID 1
        (14 orphans after one regression). Sleep is split into chunks of <=5 s (`sleep_fn`): the group empties within ~5 s at the latest."""
        import signal
        self.env['CADRE_QUOTA_INTERVAL'] = '3600'   # D15: a 1 s interval masked the old `sleep 3600` bug (the old code was green too)
        quota = self.start('capacity-watch')
        self.assertTrue(self.wait(lambda: (self.t / 'qe/chain/capacity-watch.tick').exists()))
        os.kill(quota.pid, signal.SIGKILL)
        quota.wait(timeout=10)

        def group_empty():
            try:
                os.killpg(quota.pid, 0)
                return False
            except ProcessLookupError:
                return True
            except PermissionError:   # macOS: killpg returns EPERM if only zombies are left in the group; wait until they are reaped
                return False
        self.assertTrue(self.wait(group_empty, sec=8), 'An orphaned sleep remains in the guard process group after 8s')

    def test_guards_exit_on_ownership_and_no_network_call_in_an_isolated_home(self):
        """The quota guard starts first so the watch does not revive it (a revived guard ticks at 1 h intervals in the persistent environment).
        06.10 QA: with CADRE_NO_NET=1 a test home with a fake key contacts no provider endpoint (the new form of B191)."""
        (self.t / 'qe/chain').mkdir(parents=True, exist_ok=True)
        (self.t / 'qe/chain/openai-admin-key').write_text('sk-admin-fake')
        quota = self.start('capacity-watch')
        self.assertTrue(self.wait(lambda: (self.t / 'qe/chain/capacity-watch.tick').exists()))
        watch = self.start('health-watch')
        self.assertTrue(self.wait(lambda: (self.t / 'qe/logs/health-watch.log').exists()
                                   and 'run ledger' in (self.t / 'qe/logs/health-watch.log').read_text()), 'The first tick must finish')
        self.assertIsNone(watch.poll(), 'The guard runs while its owner is alive')
        self.owner.kill()
        self.owner.wait()
        self.assertTrue(self.wait(lambda: watch.poll() is not None and quota.poll() is not None),
                        'Both guards must exit when their owner dies, leaving no orphans')
        call = (self.t / 'curl-call').read_text() if (self.t / 'curl-call').exists() else ''
        self.assertNotIn('deepseek', call, 'CADRE_NO_NET=1 prevents provider endpoint access with the fake key')
        self.assertFalse((self.t / 'qe/chain/openai-setup.json').exists(), 'CADRE_NO_NET=1 prevents OpenAI organization endpoint calls')


if __name__ == '__main__':
    unittest.main()
