"""4c-4: liveness = token/usage progress; an SSE comment only means 'connection open'. The deadline applies even while there is liveness (B313)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import qe_core as core  # noqa: E402
from liveness import Liveness  # noqa: E402


class OpenRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-live-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        (self.directory / 'requests').mkdir()
        attempt = type('Attempt', (), {'directory': self.directory})()
        proc = object.__new__(core.RecordedProcess)
        proc.attempt = attempt
        self.guard = object.__new__(Liveness)
        self.guard.proc = proc

    def mark(self, **field_list):
        (self.directory / 'requests/i.json').write_text(json.dumps({'pid': os.getpid(), **field_list}))

    def test_a_request_streaming_tokens_is_live(self):
        now_ts = time.time()
        self.mark(started=(now_ts - 3600) * 1000, progress_at=(now_ts - 30) * 1000, round='token')
        self.assertTrue(self.guard._request(600, now_ts))

    def test_a_request_streaming_only_comments_is_not_live_after_the_threshold(self):
        now_ts = time.time()
        self.mark(started=(now_ts - 3600) * 1000, last_chunk_at=(now_ts - 1) * 1000, progress_at=None, round='comment')
        self.assertFalse(self.guard._request(600, now_ts), 'An OR processing comment is not progress')
        self.mark(started=(now_ts - 60) * 1000, last_chunk_at=(now_ts - 1) * 1000, progress_at=None, round='comment')
        self.assertTrue(self.guard._request(600, now_ts), 'A new request is live until the threshold expires')

    def test_marker_of_a_dead_process_does_not_count(self):
        (self.directory / 'requests/i.json').write_text(json.dumps({'pid': 2**22 + 7, 'started': time.time() * 1000}))
        self.assertFalse(self.guard._request(600, time.time()))


class MovingStepTests(unittest.TestCase):
    def guard(self, last_once, moving, open_step=True):
        guard = object.__new__(Liveness)
        guard.proc, guard.wt, guard.record, guard.logf = None, None, None, self.directory / 'out.log'
        guard.min_sec, guard.longest, guard.movement_limit, guard.open_step = 600.0, 0.0, 2700.0, open_step
        guard.status, guard.reason, guard.offset, guard.remainder, guard.steps = 'unknown', '', 0, b'', set()
        guard.last = time.monotonic() - last_once
        guard.movement_last = None
        counter = [0]

        def size():
            counter[0] += 1
            return counter[0] if moving else 5
        guard._size, guard._tree, guard._cpu = size, lambda: (), lambda: {}
        guard._request, guard._step = lambda threshold, now_ts: False, lambda: False
        guard.size, guard.tree, guard.cpu = guard._size(), (), {}
        return guard

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-movement-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def test_moving_uncertain_step_continues_with_a_separate_finite_limit(self):
        guard = self.guard(700, moving=True)
        self.assertFalse(guard.stuck(), 'CPU or file movement prevents the silence cutoff')
        self.assertEqual((guard.status, guard.reason), ('unknown', 'bounded-active-step'))
        guard = self.guard(2800, moving=True)
        self.assertTrue(guard.stuck(), 'A moving step also has a finite limit')
        self.assertEqual(guard.reason, 'active-time-limit')

    def test_motionless_silence_threshold_does_not_change(self):
        guard = self.guard(700, moving=False)
        self.assertTrue(guard.stuck())
        self.assertEqual((guard.status, guard.reason), ('stalled', 'stall-time-limit'))

    def test_movement_threshold_does_not_extend_without_an_open_tool_step(self):
        guard = self.guard(700, moving=True, open_step=False)
        self.assertTrue(guard.stuck(), 'Log or CPU movement without an open tool step does not extend the CR-14 threshold')
        self.assertEqual((guard.status, guard.reason), ('unknown', 'uncertain-time-limit'))


class DeadlineTests(unittest.TestCase):
    def test_deadline_applies_even_while_liveness_holds(self):
        with tempfile.TemporaryDirectory(prefix='qe-duration-') as d:
            log = Path(d) / 'out.log'
            log.write_text('')
            with log.open('a') as output:
                proc = subprocess.Popen([sys.executable, '-c', 'import time\nwhile True:\n print("x", flush=True); time.sleep(0.2)'],
                                        stdout=output, start_new_session=True)
            start = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                core.wait_process(proc, 2, logf=log)
            self.assertLess(time.monotonic() - start, 15)
            self.assertIsNotNone(proc.poll(), 'The process is stopped at the deadline')

    def test_role_deadlines_from_a_single_source(self):
        import limits
        self.assertEqual({r: limits.deadline(r) for r in ('plan', 'quality', 'qa', 'split')},
                         {'plan': 1800, 'quality': 2400, 'qa': 2700, 'split': 900})



import test_run_sessions as fixtures  # noqa: E402


class CuttingTests(unittest.TestCase):
    """16(2): opencode `finish=length` -> the same session continues once with a 2× output cap; a second truncation is partial; every truncation is recorded."""

    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.fake.write_text(self.f.fake.read_text() + '''
multiplier = os.environ.get('CADRE_OUTPUT_MULTIPLIER', '1')
(p/'layers.txt').open('a').write(multiplier + '\\n')
reason = 'length' if (multiplier == '1' or os.environ.get('ALWAYS_CUT') == '1') else 'stop'
print(json.dumps({'type': 'step_finish', 'sessionID': sid, 'part': {'type': 'step-finish', 'reason': reason}}), flush=True)
''')

    def test_cut_response_continues_once_in_the_same_session_with_double_cap(self):
        rc, _ = self.f.runner_fixture(accept=True)
        self.assertEqual((self.f.home / 'layers.txt').read_text().split(), ['1', '2'], 'The doubled cap applies to only one retry')
        self.assertIn('--session', self.f.calls()[1], 'same session')
        warnings = self.f.store.read(self.f.rid).get('output_warnings') or []
        self.assertEqual([u['round'] for u in warnings], ['cutting'])
        self.assertEqual(self.f.store.read(self.f.rid)['tasks']['g1'].get('failure_count', 0), 0, 'Truncation does not count as a failed attempt')

    def test_job_is_left_half_done_if_cut_even_with_double_cap(self):
        with unittest.mock.patch.dict(os.environ, {'ALWAYS_CUT': '1'}):
            rc, _ = self.f.runner_fixture(accept=True)
        self.assertNotEqual(rc, 0)
        self.assertEqual(len(self.f.calls()), 2)
        self.assertEqual([u['round'] for u in self.f.store.read(self.f.rid).get('output_warnings') or []], ['cutting', 'cutting'])


class ThinkingAlarmTests(unittest.TestCase):
    """16(3): an alarm if a thinking model has no thinking TEXT; usage.reasoning 0 alone is not an alarm."""

    def summary(self, parts, model='opencode-go/deepseek-v4-pro'):
        import sqlite3
        from contextlib import closing
        import acceptance
        with tempfile.TemporaryDirectory(prefix='qe-thinking-') as d:
            home_dir = Path(d)
            stdout = home_dir / 'stdout.jsonl'
            stdout.write_text(json.dumps({'type': 'step_finish', 'part': {'type': 'step-finish', 'reason': 'stop',
                                                                          'tokens': {'reasoning': 0}}}) + '\n')
            db = home_dir / 'oc.db'
            with closing(sqlite3.connect(db)) as con, con:
                con.execute('create table message(id text, session_id text, data text)')
                con.execute('create table part(id text, message_id text, time_created integer, data text)')
                con.execute("insert into message values('m1','ses_d','{}')")
                for i, (round, text) in enumerate(parts):
                    con.execute('insert into part values(?,?,?,?)', (f'p{i}', 'm1', int(time.time() * 1000),
                                                                     json.dumps({'type': round, 'text': text})))
            attempt = type('D', (), {'data': {'stdout': str(stdout), 'session_id': 'ses_d', 'created': time.time() - 60}})()
            proc = type('P', (), {'attempt': attempt})()
            log = home_dir / 'm.log'
            log.write_text('')
            catalog = home_dir / 'models.json'
            catalog.write_text(json.dumps({'opencode-go': {'models': {'deepseek-v4-pro': {'reasoning': True}}}}))
            with unittest.mock.patch.dict(os.environ, {'OPENCODE_DB': str(db), 'CADRE_MODEL_CATALOG': str(catalog), 'CADRE_RUN_ID': ''}):
                return acceptance.output_audit(proc, model, log), log.read_text()

    def test_alarm_usage_is_zero_without_text_but_absent_with_text(self):
        summary, log = self.summary([('text', 'answer')])
        self.assertEqual(summary['thinking'], 0)
        self.assertIn('no thinking text', log)
        summary, log = self.summary([('reasoning', 'I reasoned step by step'), ('text', 'answer')])
        self.assertGreater(summary['thinking'], 0)
        self.assertNotIn('no thinking text', log, 'Zero usage.reasoning alone is not an alarm')


class LaneContextTests(unittest.TestCase):
    """4c-4: the lane's effective context is min(256k, model context − 20k); plan/judge use the model's full context."""

    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def limit(self, role, model):
        env = {'CADRE_ROLE': role}
        core.opencode_budget_env(env, ['-m', model, 'task'])
        provider, _, entry_name = model.partition('/')
        return (json.loads(env['OPENCODE_CONFIG_CONTENT']).get('provider') or {}).get(provider, {}).get('models', {}).get(entry_name, {}).get('limit')

    def test_lane_context_upper_limit_and_share(self):
        self.assertEqual(self.limit('impl', 'opencode-go/glm-5.3')['context'], 262144, '1M context -> 256k')
        self.assertEqual(self.limit('impl', 'opencode-go/deepseek-v4-pro')['context'], 262144 - 20000, 'A reserve is deducted from a small context')
        self.assertIsNone(self.limit('plan', 'opencode-go/glm-5.3'), 'The planner uses the full model context')

    def test_or_lane_model_carries_the_output_limit_in_the_opencode_setting(self):
        price = self.f.home / 'chain/openrouter-price.json'
        price.write_text(json.dumps({'models': {'deepseek/deepseek-v4-pro': {'tiers': {
            'streamlake/fp8': {'max_output': 384000}, 'deepinfra/fp8': {'max_output': 16384}}}}}))
        outcome = []
        for has_file in (True, False):
            if not has_file:
                price.unlink()
            with unittest.mock.patch('model_catalog.model', return_value={'limit': {'context': 1048576}}), \
                 unittest.mock.patch.object(core, 'available_budget', return_value=10 ** 6):
                outcome.append(self.limit('impl', 'openrouter/deepseek/deepseek-v4-pro'))
        self.assertEqual([x['output'] for x in outcome], [384000, 2 * core.LIMIT.output('lane')],
                         'The OR catalog lacks an output limit; opencode rejects the setting without it and the lane cannot start')
        self.assertEqual(outcome[0]['context'], 262144)


import unittest.mock  # noqa: E402

if __name__ == '__main__':
    unittest.main()
