from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import threading
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import model_router  # noqa: E402


class ChainEnvironmentTests(unittest.TestCase):
    def test_environment_overrides_a_role_chain_and_the_report_writer_follows_plan(self):
        with patch.dict(os.environ, {'CADRE_CHAIN_PLAN': 'openrouter/deepseek/deepseek-v4-pro', 'CADRE_CHAIN_QUALITY': 'a/b, c/d ,'}):
            self.assertEqual((model_router.role_candidates('plan'), model_router.role_candidates('writer'), model_router.role_candidates('quality')),
                             (['openrouter/deepseek/deepseek-v4-pro'], ['openrouter/deepseek/deepseek-v4-pro'], ['a/b', 'c/d']))
            self.assertEqual(model_router.role_candidates('impl'), model_router.ROLES['impl'])
        with patch.dict(os.environ, {'CADRE_CHAIN_PLAN': ''}):
            self.assertEqual(model_router.role_candidates('plan'), model_router.ROLES['plan'])

    def test_cheap_or_test_chain_keeps_the_judge_independent(self):
        with tempfile.TemporaryDirectory() as t, patch.dict(os.environ, {'CADRE_HOME': t}):
            import live_scenario as scenarios
            import qe_core as core
            cheap, production = scenarios.chain_environment('or', 'cheap'), scenarios.chain_environment('or', 'production')
            self.assertEqual(scenarios.chain_environment('go', 'cheap'), {})
            for environment in (cheap, production):
                plan = environment['CADRE_CHAIN_PLAN'].split(',')
                writing = environment['CADRE_CHAIN_IMPL'].split(',')[0]
                judge = [m for m in environment['CADRE_CHAIN_QUALITY'].split(',') if core.model_family(m) not in
                         {core.model_family(writing), core.model_family(plan[0])}]
                self.assertTrue(judge, environment)
                self.assertTrue(all(m.startswith('openrouter/') for v in environment.values() for m in v.split(',')), environment)
            self.assertEqual((cheap['CADRE_CHAIN_PLAN'], production['CADRE_CHAIN_PLAN']), (scenarios.OR_DS, scenarios.OR_SOL))

    def test_or_wrapper_only_lets_openrouter_through(self):
        with tempfile.TemporaryDirectory() as t, patch.dict(os.environ, {'CADRE_HOME': t}):
            import live_scenario as scenarios
            fake = Path(t) / 'fake-opencode'
            fake.write_text(f'#!{sys.executable}\nimport json, os, sys\nprint(json.dumps(json.loads(os.environ["OPENCODE_CONFIG_CONTENT"])))\n')
            fake.chmod(0o700)
            wrapper = Path(t) / 'or-only-opencode.py'
            wrapper.write_text(scenarios.wrapper_text('or', str(fake)))
            wrapper.chmod(0o700)
            config = json.dumps({'plugin': ['file:///x/cadre-plugin.mjs'], 'provider': {'openrouter': {}, 'opencode-go': {}},
                                 'agent': {'build': {'model': 'opencode-go/glm-5.3'}}})
            env = {**os.environ, 'OPENCODE_CONFIG_CONTENT': config}
            red = subprocess.run([str(wrapper), 'run', '-m', 'opencode-go/glm-5.3', 'x'], env=env, capture_output=True, text=True, timeout=30)
            passes = subprocess.run([str(wrapper), 'run', '-m', 'openrouter/deepseek/deepseek-v4-pro', 'x'], env=env, capture_output=True, text=True,
                                   timeout=30)
        self.assertEqual(red.returncode, 75)
        self.assertIn('unpermitted route', red.stderr)
        self.assertEqual(passes.returncode, 0, passes.stderr)
        outcome = json.loads(passes.stdout)
        self.assertEqual((outcome['enabled_providers'], list(outcome['provider']), outcome['agent']['build'].get('disable')),
                         (['openrouter'], ['openrouter'], True))
        self.assertIn('opencode-go', outcome['disabled_providers'])


class LiveLimitTests(unittest.TestCase):
    def test_invalid_limits_fail_before_creating_runtime_files(self):
        import live_scenario as scenarios
        with tempfile.TemporaryDirectory() as t, patch.object(scenarios, 'ARCHIVE', Path(t) / 'archive'):
            for minutes, cap in ((0, 1), (-1, 1), (float('nan'), 1), (float('inf'), 1),
                                 (1, -1), (1, float('nan')), (1, float('inf'))):
                with self.subTest(minutes=minutes, cap=cap), self.assertRaises(ValueError):
                    scenarios.Scenario('C06', minutes, cap)
            self.assertFalse((Path(t) / 'archive').exists())

    def test_repeated_waits_and_resume_keep_the_original_deadline(self):
        import live_scenario as scenarios
        with tempfile.TemporaryDirectory() as t, patch.object(scenarios, 'ARCHIVE', Path(t)), \
             patch.object(scenarios.time, 'monotonic', return_value=100) as clock:
            scenario = scenarios.Scenario('C06', 1, 1)
            proc = Mock()
            proc.poll.return_value = None
            self.assertFalse(scenario.wait(proc, lambda: True))
            clock.return_value = 161
            with patch.object(scenario, 'stop') as stop, patch.object(scenarios.subprocess, 'Popen') as start, \
                 patch.object(scenarios.subprocess, 'run') as accept, \
                 patch.object(scenarios.time, 'sleep', side_effect=AssertionError('deadline already exhausted')):
                self.assertTrue(scenario.wait(proc))
                stop.assert_called_once_with(proc)
                with self.assertRaises(TimeoutError):
                    scenario.start('a', '--proceed')
                with self.assertRaises(TimeoutError):
                    scenario.acceptance('No paid request after the deadline')
                start.assert_not_called()
                accept.assert_not_called()

    def test_elapsed_deadline_stops_actual_child_and_rejects_another_start(self):
        import live_scenario as scenarios
        with tempfile.TemporaryDirectory() as t, patch.object(scenarios, 'HOME', Path(t)), \
             patch.object(scenarios, 'ROOT', Path(t)), patch.object(scenarios, 'ARCHIVE', Path(t) / 'archive'):
            (Path(t) / 'lib').mkdir()
            (Path(t) / 'lib/run_store.py').write_text('import time\ntime.sleep(30)\n')
            scenario = scenarios.Scenario('C06', 0.003, 1)
            started = time.monotonic()
            proc = scenario.start('a', '--proceed')
            try:
                self.assertTrue(scenario.wait(proc, interval=0.02))
                self.assertIsNotNone(proc.returncode)
                self.assertLess(time.monotonic() - started, 3)
                with self.assertRaises(TimeoutError):
                    scenario.start('a', '--proceed')
            finally:
                if proc.poll() is None:
                    scenario.stop(proc)

    def test_alarm_probe_rejects_false_success_and_preserves_unapproved_wait(self):
        import live_scenario as scenarios
        for variant in ('clean', 'changed-test', 'wrong-product', 'same-review', 'unexpected-provider',
                        'alarm-call', 'unapproved-pass', 'retry-growth', 'over-cap', 'unknown-cost'):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as t, \
                 patch.object(scenarios, 'ARCHIVE', Path(t)), patch.object(scenarios.subprocess, 'run') as commands:
                scenario = scenarios.Scenario('C06', 10, 1)
                scenario.acceptance = Mock(return_value='a')
                scenario.start = Mock()
                scenario.wait = Mock(return_value=True)
                scenario.again_wait = Mock(return_value=True)
                scenario.tree = Mock(return_value=['a'])
                waiting = {'state': 'retryable', 'retry_count': 0, 'revive_count': 0}
                unapproved = dict(waiting)
                if variant == 'unapproved-pass':
                    unapproved['final_result'] = {'status': 'PASS'}
                if variant == 'retry-growth':
                    unapproved['retry_count'] = 1
                scenario.record = Mock(side_effect=[waiting, unapproved, {'state': 'completed'}])
                scenario.receipts = Mock(return_value={'providers': {}, 'calls': [{
                    'round': 'paid', 'start': time.time(), 'end': None}] if variant == 'alarm-call' else []})
                proof = {'final': 'PASS', 'pending': int(variant == 'unknown-cost'), 'product': {
                    'rc': 0, 'protected_same': variant != 'changed-test',
                    'changed': ['other.py'] if variant == 'wrong-product' else ['calc.py']},
                    'judge_independent': variant != 'same-review',
                    'unpermitted_path': ['other'] if variant == 'unexpected-provider' else [],
                    'receipt': {'opencode-go': {'real_micro': 1_000_001 if variant == 'over-cap' else 1,
                                             'pending_micro': 0}}}
                with patch.object(scenarios, 'evidence', return_value=proof):
                    result = scenarios.alarm_continuation(scenario, 'qa', {})
                self.assertEqual(result['passed'], variant == 'clean', result)
                self.assertEqual(scenario.start.call_args_list[1].args, ('a', '--proceed'))
                self.assertEqual(scenario.start.call_count, 3)
                self.assertTrue(any('spend-approval' in call.args[0] for call in commands.call_args_list))

    def test_c06_total_cap_is_shared_and_alarm_only_uses_one_run(self):
        import live_scenario as scenarios
        for cap, alarm_only in ((1.0, False), (0.001, False), (0, False), (1.0, True)):
            with self.subTest(cap=cap, alarm_only=alarm_only), tempfile.TemporaryDirectory() as t, \
                 patch.object(scenarios, 'ARCHIVE', Path(t)), patch.object(scenarios, 'evidence', return_value={
                     'final': 'PASS', 'pending': 0, 'product': {'rc': 0, 'protected_same': True, 'changed': ['calc.py']},
                     'judge_independent': True, 'unpermitted_path': [], 'receipt': {}}), patch.object(scenarios.subprocess, 'run'):
                scenario = scenarios.Scenario('C06', 10, cap)
                scenario.repo = Path(t)
                (scenario.repo / 'test_calc.py').write_text(scenarios.TEST_CALC)
                scenario.repo_setup = Mock(return_value='base')
                limits = []
                def accept(prompt):
                    limits.append(float(scenario.environment()['CADRE_RUN_CAP_USD']))
                    return str(len(limits))
                scenario.acceptance = accept
                scenario.start = Mock()
                scenario.wait = Mock(return_value=True)
                scenario.again_wait = Mock(return_value=True)
                scenario.tree = Mock(return_value=['a'])
                scenario.record = Mock(return_value={'state': 'completed', 'final_result': {'status': 'PASS'}})
                scenario.receipts = Mock(return_value={'providers': {}, 'lines': [], 'calls': []})
                scenarios.c06(scenario, only_alarm=alarm_only)
                self.assertEqual(len(limits), 1 if alarm_only else 3)
                self.assertLessEqual(sum(round(v * 1e6) for v in limits), int(cap * 1e6))
                self.assertTrue(all(v >= 0 for v in limits))
                if alarm_only:
                    self.assertEqual(limits, [cap])


class LiveMeasurementTests(unittest.TestCase):
    def setUp(self):
        import live_scenario as scenarios
        from budget_gate import BudgetLedger
        from datetime import datetime
        self.temp = tempfile.TemporaryDirectory(prefix='cadre-call-measurement-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        for name, value in (('HOME', self.home), ('ROOT', self.home), ('ARCHIVE', self.home / 'archive')):
            p = patch.object(scenarios, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.scenario = scenarios.Scenario('C06', 1, 1, path='or')
        self.ledger = BudgetLedger(self.home / 'chain/request-budget.db')
        with self.ledger.transaction():
            pass
        now = datetime.now()
        self.snapshot = {'ts': now.isoformat(), 'today': now.date().isoformat(),
                         'or': {'db': 'ok', 'pay': 5, 'available': 5},
                         'go': {'db': 'ok', 'pay': 6.666667, 'available': 6.666667, 'window_open': True, 'unit': 'points'}}

    def reservation(self, provider='openrouter', actual=None, receipt='', run='a', **metadata):
        ident = self.ledger.reserve(provider, 1 if ':free' in provider else 100, self.snapshot, metadata)
        with self.ledger.transaction() as con:
            con.execute("update reservation set metadata=json_set(metadata,'$.run_id',?,'$.session_id',?) where id=?",
                        (run, 'session-' + run, ident))
        if actual is not None:
            self.ledger.settle(ident, actual, receipt + ident if receipt else ident + ':response')
        return ident

    def test_real_ledger_separates_calls_guarantees_unused_and_unknown_without_mixing_units(self):
        paid = self.reservation(actual=20)
        self.reservation(provider='opencode:free', actual=1)
        self.reservation(assurance=True)
        self.reservation(actual=0, receipt='claimed:', assurance=True)
        self.reservation(actual=0, receipt='unused:', assurance=True)
        self.reservation(worst=300)
        self.reservation(actual=70, receipt='phantom:')
        self.reservation(actual=50, run='unrelated')
        before = self.ledger.path.read_bytes()
        result = self.scenario.receipts(['a'])
        self.assertEqual(self.ledger.path.read_bytes(), before)
        paid_rows = result['providers']['openrouter']
        self.assertEqual({k: paid_rows[k] for k in ('paid_request', 'assurance', 'assurance_consumed', 'unused', 'uncertain')},
                         {'paid_request': 1, 'assurance': 1, 'assurance_consumed': 1, 'unused': 1, 'uncertain': 2})
        self.assertEqual(paid_rows['real_micro'], 20)
        self.assertEqual(paid_rows['pending_micro'], 500)
        free = result['providers']['opencode:free']
        self.assertEqual((free['free_request'], free['free_count'], free['real_micro'], free['pending_micro']), (1, 1, 0, 0))
        self.assertEqual({r['round'] for r in result['calls']}, {'paid', 'free', 'uncertain'})
        self.assertEqual(len(result['calls']), 4)
        self.assertEqual(next(r for r in result['calls'] if r['id'] == paid)['run_id'], 'a')
        # CXR2-03: a bucket name with a '#model' suffix (budget_gate.MODEL_SUFFIX) counts as paid with its base provider; a direct row is written
        import json as _json
        import time as _time
        with self.ledger.transaction() as con:
            for ident, provider, actual in (('bucket-go', 'opencode-go#glm-5.3', 500), ('bucket-or', 'openrouter#z-ai/glm-5.3', 90)):
                con.execute('insert into reservation(id, provider, period, reserved, actual, created, receipt, metadata) values(?,?,?,?,?,?,?,?)',
                            (ident, provider, _time.strftime('%Y-%m-%d'), 1000, actual, _time.time(), ident + ':ok',
                             _json.dumps({'run_id': 'bucket', 'session_id': 'ses-bucket'})))
        bucket = self.scenario.receipts(['bucket'])['providers']
        self.assertEqual({k: (v['unit'], v['paid_request'], v['uncertain'], v['real_micro']) for k, v in bucket.items()},
                         {'opencode-go#glm-5.3': ('subscription_equivalent_micro', 1, 0, 500), 'openrouter#z-ai/glm-5.3': ('cash_micro', 1, 0, 90)})
        broken = self.reservation(actual=20, run='bad')
        with self.ledger.transaction() as con:
            con.execute('update reservation set actual=-1 where id=?', (broken,))
        with self.assertRaisesRegex(ValueError, 'receipt'):
            self.scenario.receipts(['bad'])
        import sqlite3
        saved = self.ledger.path.with_suffix('.saved')
        self.ledger.path.rename(saved)
        with self.assertRaises(sqlite3.OperationalError):
            self.scenario.receipts(['a'])
        self.assertFalse(self.ledger.path.exists())

    def test_interval_boundaries_keep_overlap_unknown_and_free_calls_out_of_paid_alarm(self):
        rows = []
        for start, end, kind in ((1, 5, 'paid'), (10, 11, 'paid'), (20, 21, 'paid'),
                                 (5, 15, 'paid'), (5, None, 'uncertain'), (12, 13, 'free')):
            rows.append({'start': start, 'end': end, 'round': kind})
        result = self.scenario.window_calls({'calls': rows}, 10, 20)
        self.assertEqual(len(result), 3)
        self.assertEqual([r['moment'] for r in result], ['within range', 'uncertain', 'uncertain'])
        self.assertEqual(len(self.scenario.window_calls({'calls': rows}, 10, 20, only_paid=False)), 4)

    def test_alarm_driver_uses_real_receipts_and_never_accepts_a_nonterminal_pass(self):
        import live_scenario as scenarios
        for variant in ('clean', 'free', 'guarantee', 'unused', 'paid', 'unknown', 'nonterminal'):
            with self.subTest(variant=variant):
                run_id = variant
                scenario = self.scenario
                scenario.acceptance = Mock(return_value=run_id)
                scenario.tree = Mock(return_value=[run_id])
                scenario.wait = Mock(return_value=True)
                scenario.again_wait = Mock(return_value=True)
                waiting = {'state': 'retryable'}
                end = {'state': 'running' if variant == 'nonterminal' else 'completed'}
                scenario.record = Mock(side_effect=[waiting, waiting, end])
                def launch(*args):
                    if len(launcher.call_args_list) == 1:
                        if variant == 'free':
                            self.reservation(provider='opencode:free', actual=1, run=run_id)
                        elif variant == 'guarantee':
                            for _ in range(4):
                                self.reservation(assurance=True, run=run_id)
                        elif variant == 'unused':
                            self.reservation(assurance=True, actual=0, receipt='unused:', run=run_id)
                        elif variant == 'paid':
                            self.reservation(actual=20, run=run_id)
                        elif variant == 'unknown':
                            self.reservation(run=run_id)
                launcher = Mock(side_effect=launch)
                scenario.start = launcher
                def proof(*args):
                    m = scenario.receipts([run_id])['providers']
                    return {'final': 'PASS', 'pending': sum(v['pending'] for v in m.values()),
                            'receipt': m, 'judge_independent': True, 'unpermitted_path': [],
                            'product': {'rc': 0, 'protected_same': True, 'changed': ['calc.py']}}
                with patch.object(scenarios.subprocess, 'run'), patch.object(scenarios, 'evidence', side_effect=proof):
                    result = scenarios.alarm_continuation(scenario, 'qa', {})
                self.assertEqual(result['passed'], variant in ('clean', 'free', 'guarantee', 'unused'), result)
                self.assertEqual(result['alarm_request'], int(variant in ('paid', 'unknown')))

    def test_interrupted_window_restores_only_its_own_marker(self):
        import live_scenario as scenarios
        marker = self.home / 'chain/go-429-last.txt'
        marker.parent.mkdir(exist_ok=True)
        for foreign in (False, True):
            with self.subTest(foreign=foreign):
                marker.write_text('previous')
                self.scenario.path = 'go'
                self.scenario.acceptance = Mock(return_value='a')
                self.scenario.start = Mock()
                def interrupted(*args, **kwargs):
                    if marker.read_text() == 'previous':
                        return False
                    if foreign:
                        marker.write_text('another-owner')
                    raise RuntimeError('interrupted window')
                self.scenario.wait = Mock(side_effect=interrupted)
                with patch.object(scenarios, 'two_file_repo', return_value=('qa', {})), self.assertRaisesRegex(RuntimeError, 'interrupted'):
                    scenarios.c05(self.scenario)
                self.assertEqual(marker.read_text(), 'another-owner' if foreign else 'previous')

    def test_closed_window_driver_rejects_paid_free_go_unknown_and_nonterminal_results(self):
        import live_scenario as scenarios
        self.snapshot['go'] = {'db': 'ok', 'pay': 1000, 'available': 1000, 'unit': 'points', 'window_open': True}
        for variant in ('clean', 'guarantee', 'helper-free', 'go-free', 'paid', 'unknown-overlap', 'nonterminal', 'bad-denial', 'bad-router'):
            with self.subTest(variant=variant):
                rid = 'c05-' + variant
                now = time.time()
                if variant not in ('clean', 'nonterminal', 'bad-denial', 'bad-router'):
                    provider = 'opencode-go:free' if variant == 'go-free' else 'opencode:free' if variant == 'helper-free' else 'openrouter'
                    actual = 1 if variant in ('go-free', 'helper-free') else 20 if variant == 'paid' else None
                    ident = self.reservation(provider=provider, actual=actual, run=rid, assurance=variant == 'guarantee')
                    start = now - 10 if variant == 'unknown-overlap' else now + 6
                    with self.ledger.transaction() as con:
                        con.execute("update reservation set created=?,metadata=json_set(metadata,'$.settled_ts',?) where id=?",
                                    (start, now + 7 if actual is not None else None, ident))
                deny = self.home / 'runs' / rid / 'tasks/g1/attempts/1/budget-denied.json'
                deny.parent.mkdir(parents=True)
                deny.write_text(json.dumps({'time': now + 10, 'refusals': [{'provider': 'opencode-go',
                    'scope': 'provider', 'reason': 'provider window closed'}]}))
                if variant == 'bad-denial':
                    deny.write_text('{}')
                router = self.home / 'chain/provider-status.json'
                if variant == 'bad-router':
                    router.write_text('broken')
                elif router.exists():
                    router.unlink()
                scenario = self.scenario
                scenario.path = 'go'
                scenario.acceptance = Mock(return_value=rid)
                scenario.tree = Mock(return_value=[rid])
                scenario.start = Mock()
                scenario.wait = Mock(side_effect=[False, True])
                scenario.record = Mock(return_value={'state': 'running' if variant == 'nonterminal' else 'completed'})
                scenario.again_wait = Mock(return_value=False)
                proof = {'final': 'PASS', 'pending': 0, 'unpermitted_path': [], 'product': {'rc': 0, 'protected_same': True}}
                with patch.object(scenarios, 'two_file_repo', return_value=('qa', {})), patch.object(scenarios, 'evidence', return_value=proof), \
                     patch.object(scenarios.time, 'time', return_value=now):
                    result = scenarios.c05(scenario)
                self.assertEqual(result['decision'], 'PASS' if variant in ('clean', 'guarantee', 'helper-free') else 'FAIL', result)

    def test_retry_extension_waits_before_actual_child_and_deadline_never_starts_it(self):
        from run_store import RunStore
        store = RunStore(self.home)
        run = store.accept('a', 'Inspect the source')
        rid = run['run_id']
        retry = time.time() + .12
        store.update(rid, state='retryable', retry_after=retry)
        extended = retry + .15
        def extend():
            time.sleep(.04)
            store.update(rid, retry_after=extended)
        thread = threading.Thread(target=extend)
        thread.start()
        self.addCleanup(thread.join)
        (self.home / 'lib').mkdir()
        marker = self.home / 'launched.json'
        (self.home / 'lib/run_store.py').write_text(f'import pathlib,time\npathlib.Path({str(marker)!r}).write_text(str(time.time()))\n')
        self.assertTrue(self.scenario.again_wait(rid))
        proc = self.scenario.start(rid, '--proceed')
        self.assertEqual(proc.wait(timeout=3), 0)
        self.assertGreaterEqual(float(marker.read_text()), extended)
        marker.unlink()
        store.update(rid, retry_after=time.time() + 30)
        self.scenario.last_monotonic = time.monotonic() + .05
        self.assertFalse(self.scenario.again_wait(rid))
        with self.assertRaises(TimeoutError):
            self.scenario.start(rid, '--proceed')
        self.assertFalse(marker.exists())
        store.update(rid, retry_after='unknown')
        self.scenario.last_monotonic = time.monotonic() + 1
        with self.assertRaisesRegex(ValueError, 'retry'):
            self.scenario.again_wait(rid)

    def test_low_cap_needs_a_budget_denial_and_a_stopped_run(self):
        import live_scenario as scenarios
        (self.home / 'test_calc.py').write_text(scenarios.TEST_CALC)
        for variant in ('budget', 'permission', 'running'):
            with self.subTest(variant=variant):
                scenario = self.scenario
                scenario.repo = self.home
                scenario.repo_setup = Mock(return_value='base')
                scenario.acceptance = Mock(side_effect=['low', 'cancel'])
                scenario.tree = Mock(return_value=['low'])
                scenario.start = Mock()
                scenario.wait = Mock(side_effect=[True, False, True])
                low = {'state': 'running' if variant == 'running' else 'failed',
                       'denials': [] if variant == 'permission' else [{'reason': 'run budget exhausted: project total cap'}]}
                scenario.record = Mock(side_effect=[low, {'state': 'cancelled'}])
                with patch.object(scenarios.subprocess, 'run'), patch.object(scenarios, 'alarm_continuation', return_value={'run': 'alarm', 'passed': True}):
                    result = scenarios.c06(scenario)
                self.assertEqual(result['decision'], 'PASS' if variant == 'budget' else 'FAIL', result)


if __name__ == '__main__':
    unittest.main()
