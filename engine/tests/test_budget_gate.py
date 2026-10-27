"""C3: missing accounting cannot authorize a paid provider."""

from __future__ import annotations

import unittest
from unittest.mock import patch
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
import time
import os
import shutil
import io
from contextlib import closing

import test_run_sessions as fixtures
import qe_core as core
import budget_gate as budget


def setUpModule():
    environment = patch.dict(os.environ, {'CADRE_CHAIN_QUALITY': 'opencode-go/qwen3.8-max'})
    environment.start()
    unittest.addModuleCleanup(environment.stop)



class BudgetBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.SessionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_unknown_budget_requires_restriction(self) -> None:
        with patch.object(core, 'budget_json', return_value=None):
            restricted, _, reason = core.go_restriction_required()
        self.assertTrue(restricted)
        # B131: the restriction is needed BUT the reason is not "share exhausted" — the observation could not be read. They are separate things.
        self.assertTrue(reason.startswith('unreadable'), f'A read failure cannot be reported as allocation exhaustion: {reason!r}')

    def test_plugin_named_reviewer_uses_the_helper_pocket_through_the_request_entrypoint(self):
        f = self.fixture
        f.store.update(f.rid, budget_cap_usd=1.0)
        from test_budget_period import snapshot
        with patch.dict(os.environ, {'CADRE_HOME': str(f.home), 'CADRE_ENGINE_ROOT': str(fixtures.LIB.parent), 'CADRE_ROLE': 'impl'}), \
             patch.object(budget, 'observed_budget', return_value=snapshot(5)), \
             patch.object(sys, 'argv', ['budget_gate.py']):
            for maximum, expected in ((90_000, 0), (20_000, 75)):
                request = {'action': 'reserve', 'provider': 'opencode-go', 'maximum': maximum,
                           'metadata': {'role': 'cadre-review', 'model': 'glm-5.2'}}
                with patch.object(sys, 'stdin', io.StringIO(json.dumps(request))), \
                     patch.object(sys, 'stdout', io.StringIO()), patch.object(sys, 'stderr', io.StringIO()):
                    self.assertEqual(budget.main(), expected)
        with closing(sqlite3.connect(f.home/'chain/request-budget.db')) as con:
            self.assertEqual(budget.run_usage(con, f.rid, 'helper'), (0, 90_000))
            self.assertEqual(budget.run_usage(con, f.rid, 'lanes'), (0, 0))

    def test_final_child_config_preserves_context_limits_including_existing_overrides(self):
        f = self.fixture
        env = {'OPENCODE_CONFIG_CONTENT': json.dumps({'agent': {'compaction': {'model': 'opencode-go/glm-5.3'}}})}
        with patch.object(core, 'budget_json', return_value=None):
            # P3 (03.10): no budget -> BudgetDenied at the gate; no config is produced.
            with self.assertRaises(budget.BudgetDenied):
                core.opencode_budget_env(env, ['task'])

    def test_reservation_records_engine_phase_and_attempt_without_trusting_request_metadata(self):
        f = self.fixture
        from test_budget_period import snapshot
        request = {'action': 'reserve', 'provider': 'opencode-go', 'maximum': 1,
                   'metadata': {'role': 'build', 'model': 'fixture/planner',
                                'phase_role': 'impl', 'attempt_id': 'wrong'}}
        env = {'CADRE_HOME': str(f.home), 'CADRE_ENGINE_ROOT': str(fixtures.LIB.parent),
               'CADRE_ROLE': 'plan', 'CADRE_BUDGET_ATTEMPT': 'actual-attempt'}
        with patch.dict(os.environ, env), \
             patch.object(budget, 'observed_budget', return_value=snapshot(5)), \
             patch.object(sys, 'argv', ['budget_gate.py']), \
             patch.object(sys, 'stdin', io.StringIO(json.dumps(request))), \
             patch.object(sys, 'stdout', io.StringIO()):
            self.assertEqual(budget.main(), 0)
        with closing(sqlite3.connect(f.home/'chain/request-budget.db')) as con:
            metadata = json.loads(con.execute('select metadata from reservation').fetchone()[0])
        self.assertEqual(metadata['role'], 'build')
        self.assertEqual(metadata['phase_role'], 'plan')
        self.assertEqual(metadata['attempt_id'], 'actual-attempt')

    def test_zero_dollar_cap_still_admits_a_verified_free_model_in_router_and_executor(self):
        f = self.fixture
        f.store.update(f.rid, budget_cap_usd=0)
        from test_budget_period import snapshot
        b = snapshot(0)
        with patch.object(core, 'budget_json', return_value=b), \
             patch('model_catalog.is_free', side_effect=lambda provider, model: model == 'free-model'):
            self.assertTrue(core.ROUTER.has_budget('opencode', 'opencode/free-model', b, 8000, 'impl')[0])
            env = {'CADRE_ROLE': 'impl'}
            args = core.opencode_budget_env(env, ['-m', 'opencode/free-model', 'task'])
        self.assertIn('opencode/free-model', args)
        self.assertTrue(json.loads(env['OPENCODE_CONFIG_CONTENT'])['plugin'])


    def test_budget_exception_requires_restriction(self) -> None:
        with patch.object(core, 'budget_json', side_effect=OSError('unreadable')):
            self.assertTrue(core.go_restriction_required()[0])

    def test_unknown_budget_never_launches_paid_model(self) -> None:
        f = self.fixture
        with patch.object(core, 'budget_json', return_value=None), patch.dict(os.environ, {'CADRE_REAL_OPENCODE':str(f.fake)}):
            # P3 (03.10): budget unknown -> BudgetDenied at the gate; a paid model never starts.
            with self.assertRaises(budget.BudgetDenied):
                core.run_opencode(str(f.fake), str(f.wt), ['-m','opencode-go/test','task'], f.log)

    def test_unknown_budget_disables_cloud_helpers_in_child_config(self) -> None:
        f = self.fixture
        with patch.object(core, 'budget_json', return_value=None):
            # P3 (03.10): budget unknown + local closed -> BudgetDenied at the gate; no child config is produced.
            with self.assertRaises(budget.BudgetDenied):
                core.run_opencode(str(f.fake), str(f.wt), ['task'], f.log)

    def test_budget_denial_resumes_session_without_consuming_failure_limit(self) -> None:
        f = self.fixture
        f.fake.write_text(f.fake.read_text() + '''
calls=(p/'calls.jsonl').read_text().splitlines()
if len(calls)==1:
 root=p/'runs'/os.environ['CADRE_RUN_ID']/'tasks'/os.environ['CADRE_TASK_ID']/'attempts'/os.environ['CADRE_BUDGET_ATTEMPT']
 (root/'budget-denied.json').write_text(json.dumps({'refusals': [{'provider': 'opencode-go', 'reason': 'allocation exhausted or outcome unknown', 'slice': 'lanes'}]}))
 pathlib.Path('product.txt').write_text('partial')
 raise SystemExit(75)
assert pathlib.Path('product.txt').read_text()=='partial'
pathlib.Path('product.txt').write_text('complete')
''')
        # P6 (03.10): acceptance_assurance reserves the judge+QA calls in the real ledger before the lanes;
        # without a Go budget the engine returns 75. B339: the same session continues via ds-v4-pro's OpenRouter route,
        # so the observation also has an 'or' entry.
        from test_budget_period import snapshot
        b = snapshot(5)
        b['or'] = {'db': 'ok', 'pay': 5, 'available': 5}
        (f.home / 'chain/openrouter-price.json').write_text(json.dumps({'version': 2, 'ts': time.time(), 'models': {   # OR-PIN observation
            'deepseek/deepseek-v4-pro': {'input': 1.3, 'output': 2.6, 'providers': ['streamlake/fp8'], 'fp8_cache': True}}}))
        with patch.object(core, 'budget_json', return_value=b), \
             patch('model_router.budget_read', return_value=b):
            rc, _ = f.runner_fixture(env={'CADRE_RUN_MODEL': 'opencode-go/deepseek-v4-pro'})
        self.assertEqual(rc, 0)
        self.assertEqual(len(f.calls()), 2)
        self.assertIn('--session', f.calls()[1])
        self.assertEqual((f.wt/'product.txt').read_text(), 'complete')
        self.assertEqual(f.store.read(f.rid)['tasks']['g1'].get('failure_count', 0), 0)

    def test_a_helper_providers_budget_denial_keeps_the_lanes_main_model(self) -> None:
        """B231 (25.09 live R13-v2): while the writer ran on Go, a helper call on zen got a budget denial; the engine switched the whole lane to local
        (partial for the report writer -> chain exhausted -> run failed). If the denied provider is not the main model's, the main model
        is kept and only the helpers skip that provider."""
        f = self.fixture
        f.fake.write_text(f.fake.read_text() + '''
calls=(p/'calls.jsonl').read_text().splitlines()
if len(calls)==1:
 root=p/'runs'/os.environ['CADRE_RUN_ID']/'tasks'/os.environ['CADRE_TASK_ID']/'attempts'/os.environ['CADRE_BUDGET_ATTEMPT']
 (root/'budget-denied.json').write_text(json.dumps({'providers': ['opencode'], 'refusals': [{'provider': 'opencode', 'reason': 'allocation exhausted or outcome unknown', 'slice': 'lanes'}]}))
 pathlib.Path('product.txt').write_text('partial')
 raise SystemExit(75)
pathlib.Path('product.txt').write_text('complete')
''')
        from test_budget_period import snapshot
        with patch.object(core, 'budget_json', return_value=snapshot(5)):   # there is a Go share: the cloud model really starts
            rc, _ = f.runner_fixture(env={'CADRE_RUN_MODEL': 'opencode-go/test'})
        self.assertEqual(rc, 0)
        self.assertEqual(len(f.calls()), 2)
        self.assertIn('opencode-go/test', f.calls()[0], 'The lane started in the cloud')
        self.assertIn('opencode-go/test', f.calls()[1], 'The main model is preserved without falling back locally')
        self.assertIn('--session', f.calls()[1])
        self.assertEqual((f.wt/'product.txt').read_text(), 'complete')

    def test_report_writer_lane_never_continues_locally_after_a_budget_denial(self) -> None:
        """B225 (25.09 live R12-report): the report writer got a budget denial on Claude, the engine continued the session locally (a G-R6 violation).
        On a budget denial the lane ends 'partial' and the dispatcher moves to the next in the plan chain."""
        f = self.fixture
        f.fake.write_text(f.fake.read_text() + '''
root=p/'runs'/os.environ['CADRE_RUN_ID']/'tasks'/os.environ['CADRE_TASK_ID']/'attempts'/os.environ['CADRE_BUDGET_ATTEMPT']
(root/'budget-denied.json').write_text('{}')
raise SystemExit(75)
''')
        # B225 + P6 (03.10): a single executor call; the denied Go model finds no alternative session route,
        # so the engine leaves it partial with a budget wait — the local lane does NOT CONTINUE and no second call is written.
        from test_budget_period import snapshot
        with patch.object(core, 'budget_json', return_value=snapshot(5)):
            rc, _ = f.runner_fixture(env={'CADRE_RUN_MODEL': 'opencode-go/test'})
        self.assertNotEqual(rc, 0)
        log = (f.store.task_dir(f.rid, 'g1')/'runner.log').read_text()
        self.assertEqual(len((f.home / 'calls.jsonl').read_text().splitlines()) if (f.home / 'calls.jsonl').exists() else 0, 1,
                         'No second local call after a budget denial')
        self.assertEqual(f.store.read(f.rid)['tasks']['g1'].get('state'), 'half')
        self.assertIn('continue the recorded session through budget-wait', log, 'No local continuation of a report lane (B225/G-R6)')


    def test_without_local_a_budget_denial_moves_the_session_to_another_budgeted_cloud(self) -> None:
        """B339 (03.10 11:16 live): DeepSeek denial -> the engine fell to the closed local model, the correction round died. Without local, the recorded
        session continues with another cloud that has budget (rescuer selection, excluding the denied provider)."""
        f = self.fixture
        f.fake.write_text(f.fake.read_text() + '''
calls=(p/'calls.jsonl').read_text().splitlines()
if len(calls)==1:
 root=p/'runs'/os.environ['CADRE_RUN_ID']/'tasks'/os.environ['CADRE_TASK_ID']/'attempts'/os.environ['CADRE_BUDGET_ATTEMPT']
 (root/'budget-denied.json').write_text(json.dumps({'refusals': [{'provider': 'opencode', 'reason': 'allocation exhausted or outcome unknown', 'slice': 'lanes'}]}))
 pathlib.Path('product.txt').write_text('partial')
 raise SystemExit(75)
pathlib.Path('product.txt').write_text('complete')
''')
        # P6: without a Go budget the engine returns 75 in the acceptance guarantee; also, for the model that comes to dispatch first,
        # the budgeted rescuer route is chosen before the session is limited (B339).
        from test_budget_period import snapshot
        b = snapshot(5)
        b['zen'] = {'db': 'ok', 'pay': 5, 'available': 5}   # the first call starts on this provider
        b['or'] = {'db': 'ok', 'pay': 5, 'available': 5}    # the second call is the rescuer route
        with patch.object(core, 'recovery_select', return_value='openrouter/test') as sec, \
             patch.object(core, 'budget_json', return_value=b):
            rc, _ = f.runner_fixture(env={'CADRE_RUN_MODEL': 'opencode/test'})
        self.assertEqual(rc, 0)
        self.assertTrue(sec.called, 'Without local models, a budget denial seeks another cloud candidate')
        self.assertIn('continue the recorded session through openrouter/test', (f.store.task_dir(f.rid, 'g1')/'runner.log').read_text())
        self.assertIn('--session', f.calls()[1])
        self.assertEqual((f.wt/'product.txt').read_text(), 'complete')


class SpendingCacheTests(unittest.TestCase):
    """B230 (25.09 panel audit): OpenCode's `cost` field counts cache reads as $0 for a model with no cache price in the catalogue;
    Cerebras charges the full input price (4.1M x $0.99 = $4.06 = the panel difference). An unknown discount is not a discount."""
    def test_without_a_cache_price_in_the_catalog_the_read_cache_is_added_at_the_input_price(self):
        from test_zen import fake_db
        t = tempfile.TemporaryDirectory(prefix='qe-cache-')
        self.addCleanup(t.cleanup)
        root = Path(t.name)
        (root / 'models.json').write_text(json.dumps({
            'cerebras': {'models': {'qwen-3.8-27b': {'cost': {'input': 0.99, 'output': 1.49}}}},
            'deepseek': {'models': {'deepseek-v4-flash': {'cost': {'input': 0.15, 'output': 0.6, 'cache_read': 0.003}}}}}))
        fake_db(root / 'opencode.db', [
            ('cerebras', 1000.0, 0.01, 'qwen-3.8-27b', {'input': 100, 'output': 5, 'cache': {'read': 1_000_000, 'write': 0}}),
            ('deepseek', 1000.0, 0.01, 'deepseek-v4-flash', {'input': 100, 'output': 5, 'cache': {'read': 1_000_000, 'write': 0}}),
            ('cerebras', 1000.0, 0.02)])   # old record: no model/tokens, as is
        with patch.dict(os.environ, {'CADRE_MODEL_CATALOG': str(root / 'models.json')}):
            full = ((100 + 1_000_000) * 0.99 + 5 * 1.49) / 1e6   # B280: a step is not counted below token x list price (cache = input)
            self.assertAlmostEqual(budget.spending(root / 'opencode.db', 'cerebras', 0)[0], full + 0.02, places=6)
            self.assertAlmostEqual(budget.spending(root / 'opencode.db', 'deepseek', 0)[0], 0.01, places=6, msg='Cache with a known price is already included in cost')

    def test_in_a_discounted_cache_a_zero_recorded_step_is_counted_at_the_token_price(self):
        """D16 (01.10 Codex): with a real discounted cache the recorded `cost`=0 came back as is (1M reads x $0.10 = $0.10 lost).
        A non-zero record is not re-priced (provider discounts like DeepSeek's night discount are kept)."""
        with patch('model_catalog.price', return_value={'input': 1.0, 'output': 2.0, 'cache_read': 0.1}):
            token = {'input': 0, 'output': 0, 'cache': {'read': 1_000_000}}
            self.assertAlmostEqual(budget.step_cost('opencode', 'discounted', 0, token), 0.1, places=9)
            self.assertAlmostEqual(budget.step_cost('opencode', 'discounted', 0.05, token), 0.05, places=9)
            self.assertEqual(budget.step_cost('opencode', 'discounted', 0, {'input': 0, 'output': 0}), 0)

    def test_correction_does_not_close_when_the_catalog_writes_the_cache_price_equal_to_the_input(self):
        """B280 (01.10 panel RCA): the 30.09 07:34 catalogue refresh added `cache_read: 0.99` (= input) to Cerebras; the B230 fix
        closed as 'there is a cache price' but OpenCode's `cost` still counted the cache as $0. System credit $7.58, panel $2.95
        (the difference exactly 6.6M cache x $0.99). Without a real discount a step is not counted below token x list price; no double counting either."""
        from test_zen import fake_db
        t = tempfile.TemporaryDirectory(prefix='qe-cache2-')
        self.addCleanup(t.cleanup)
        root = Path(t.name)
        (root / 'models.json').write_text(json.dumps({'cerebras': {'models': {'qwen-3.8-27b': {'cost': {'input': 0.99, 'output': 1.49, 'cache_read': 0.99}}}}}))
        full = ((100 + 1_000_000) * 0.99 + 5 * 1.49) / 1e6
        fake_db(root / 'opencode.db', [
            ('cerebras', 1000.0, 0.0001, 'qwen-3.8-27b', {'input': 100, 'output': 5, 'cache': {'read': 1_000_000, 'write': 0}}),   # cache counted as $0
            ('cerebras', 1000.0, full, 'qwen-3.8-27b', {'input': 100, 'output': 5, 'cache': {'read': 1_000_000, 'write': 0}})])    # counted correctly
        with patch.dict(os.environ, {'CADRE_MODEL_CATALOG': str(root / 'models.json')}):
            self.assertAlmostEqual(budget.spending(root / 'opencode.db', 'cerebras', 0)[0], 2 * full, places=6)


class ReservationTests(unittest.TestCase):
    def setUp(self) -> None:
        import budget_gate
        self.module = budget_gate
        self.temp = tempfile.TemporaryDirectory(prefix='qe-budget-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = budget_gate.BudgetLedger(self.root/'budget.db')

    def snapshot(self, amount=1):
        return {'today': time.strftime('%Y-%m-%d'), 'ts':time.strftime('%Y-%m-%dT%H:%M:%S'),
                'go': {'db':'ok', 'pay':6.666667, 'available':(amount) * 100 / 15, 'window_open':True, 'unit': 'points'}}

    def test_two_processes_cannot_reserve_the_same_last_dollar(self) -> None:
        code = '''import json,sys
sys.path.insert(0, sys.argv[1])
from budget_gate import BudgetLedger, BudgetDenied
try:
 BudgetLedger(sys.argv[2]).reserve('opencode-go', 700000, json.loads(sys.argv[3]), {'model':'go/model'})
 print('allowed')
except BudgetDenied: print('denied')
'''
        cmds = [subprocess.Popen([sys.executable,'-c',code,str(fixtures.LIB),str(self.root/'budget.db'),json.dumps(self.snapshot())],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        results = [p.communicate(timeout=10) for p in cmds]
        self.assertEqual([p.returncode for p in cmds], [0,0], results)
        self.assertEqual(sorted(out.strip() for out, _ in results), ['allowed','denied'])

    def test_restart_and_new_day_do_not_release_unknown_outcome(self) -> None:
        rid = self.ledger.reserve('opencode-go', 700000, self.snapshot(), {'model':'go/model'})
        restarted = self.module.BudgetLedger(self.root/'budget.db')
        with self.assertRaises(self.module.BudgetDenied):
            restarted.reserve('opencode-go', 700000, self.snapshot(), {})
        with closing(sqlite3.connect(self.root/'budget.db')) as con, con:
            con.execute("update reservation set period='2000-01-01' where id=?", (rid,))
        with self.assertRaises(self.module.BudgetDenied):
            restarted.reserve('opencode-go', 700000, self.snapshot(), {})

    def test_exact_receipt_settles_once_and_unknown_does_not(self) -> None:
        rid = self.ledger.reserve('opencode-go', 700000, self.snapshot(), {'model':'go/model'})
        self.ledger.settle(rid, 100000, 'response-1')
        self.ledger.settle(rid, 100000, 'response-1')
        with self.assertRaises(self.module.BudgetDenied):
            self.ledger.settle(rid, 0, 'another-response')
        self.ledger.reserve('opencode-go', 750000, self.snapshot(), {})

    def test_cr52_receipt_usage_is_stored_once_on_the_same_row_without_changing_money(self) -> None:
        rid = self.ledger.reserve('opencode-go', 700000, self.snapshot(), {'model': 'go/model'})
        usage = {'input': 1200, 'output': 300, 'thinking': 120, 'cache_reading': 1000, 'cache_write': 0,
                 'fee_usd': 0.0004, 'cache_discount_usd': 0.0002, 'byok': False, 'endpoint': 'StreamLake',
                 'endpoint_label': 'streamlake/fp8', 'model_response': 'deepseek/deepseek-v4-pro', 'end': 'tool_calls',
                 'format': 'chat', 'price_source': 'openrouter-endpoints', 'price_ts': 1760570000.5, 'duration_ms': 1500,
                 'prompt': 'Do not record the raw prompt', 'input_raw': [1], 'output_neg': -1}
        self.ledger.settle(rid, 100000, 'response-1', usage)
        self.ledger.settle(rid, 100000, 'response-1', {'input': 1})
        with self.assertRaises(self.module.BudgetDenied):
            self.ledger.settle(rid, 5, 'late-other-receipt', {'input': 9})
        with closing(sqlite3.connect(self.root/'budget.db')) as con:
            rows = con.execute('select actual, receipt, metadata from reservation').fetchall()
        self.assertEqual([(r[0], r[1]) for r in rows], [(100000, 'response-1')])
        stored = json.loads(rows[0][2])['usage']
        self.assertEqual(stored, {k: v for k, v in usage.items() if k not in ('prompt', 'input_raw', 'output_neg')})

    def test_cr52_invalid_or_missing_usage_fields_stay_unknown_never_zero(self) -> None:
        bad = {'input': -5, 'output': 1.5, 'thinking': True, 'cache_reading': 2**60, 'endpoint': 'bad\nad',
               'fee_usd': float('nan'), 'duration_ms': '12', 'end': 'x' * 200, 'price_ts': -1}
        for usage in (bad, None, 'text', [], {}):
            with self.subTest(usage=usage):
                rid = self.ledger.reserve('opencode-go', 1000, self.snapshot(), {'model': 'go/model'})
                self.ledger.settle(rid, 10, 'response-' + rid, usage)
                with closing(sqlite3.connect(self.root/'budget.db')) as con:
                    actual, meta = con.execute('select actual, metadata from reservation where id=?', (rid,)).fetchone()
                self.assertEqual(actual, 10)
                self.assertNotIn('usage', json.loads(meta))
        rid = self.ledger.reserve('opencode-go', 1000, self.snapshot(), {'model': 'go/model'})
        self.ledger.settle(rid, 10, 'partial', {'output': 7, 'cache_reading': None, 'endpoint': None})
        with closing(sqlite3.connect(self.root/'budget.db')) as con:
            meta = json.loads(con.execute('select metadata from reservation where id=?', (rid,)).fetchone()[0])
        self.assertEqual(meta['usage'], {'output': 7})

    def test_cr52_entrypoint_settle_carries_usage_and_a_late_receipt_after_recovery_is_not_double_counted(self) -> None:
        rid = self.ledger.reserve('opencode-go', 700000, self.snapshot(), {'model': 'go/model'})
        self.ledger.settle(rid, 700000, 'phantom:' + rid)
        env = {'CADRE_HOME': str(self.root), 'CADRE_ENGINE_ROOT': str(fixtures.LIB.parent)}
        (self.root / 'chain').mkdir(exist_ok=True)
        late = {'action': 'settle', 'id': rid, 'actual': 1000, 'receipt': rid + ':gen-1', 'usage': {'input': 10, 'output': 5}}
        fresh = self.ledger.reserve('opencode-go', 1000, self.snapshot(), {'model': 'go/model'})
        shutil.copy(self.root / 'budget.db', self.root / 'chain/request-budget.db')
        on_time = {'action': 'settle', 'id': fresh, 'actual': 900, 'receipt': fresh + ':gen-2',
                   'usage': {'input': 30, 'output': 4, 'cache_reading': 20}}
        for request, expected in ((late, 75), (on_time, 0)):
            with patch.dict(os.environ, env), patch.object(sys, 'argv', ['budget_gate.py']), \
                 patch.object(sys, 'stdin', io.StringIO(json.dumps(request))), \
                 patch.object(sys, 'stdout', io.StringIO()), patch.object(sys, 'stderr', io.StringIO()):
                self.assertEqual(self.module.main(), expected)
        with closing(sqlite3.connect(self.root/'chain/request-budget.db')) as con:
            rows = dict((r[0], r[1:]) for r in con.execute('select id, actual, receipt, metadata from reservation'))
        self.assertEqual(rows[rid][:2], (700000, 'phantom:' + rid), 'A late receipt does not add a second amount to an estimated closure')
        self.assertNotIn('usage', json.loads(rows[rid][2]))
        self.assertEqual(rows[fresh][:2], (900, fresh + ':gen-2'))
        self.assertEqual(json.loads(rows[fresh][2])['usage'], {'input': 30, 'output': 4, 'cache_reading': 20})

    def test_missing_stale_nonfinite_or_future_budget_refuses(self) -> None:
        for snapshot in (None, {}, self.snapshot(float('nan')), self.snapshot(float('inf')),
                         {**self.snapshot(), 'ts':'2000-01-01T00:00:00'},
                         {**self.snapshot(), 'ts':'2999-01-01T00:00:00'}):
            with self.subTest(snapshot=snapshot), self.assertRaises(self.module.BudgetDenied):
                self.ledger.reserve('opencode-go', 1, snapshot, {})

    def test_usage_exceeding_the_price_bound_blocks_further_paid_calls(self) -> None:
        rid = self.ledger.reserve('opencode-go', 1, self.snapshot(), {})
        self.ledger.settle(rid, 2, 'provider-overrun')
        with self.assertRaises(self.module.BudgetDenied):
            self.ledger.reserve('opencode-go', 1, self.snapshot(), {})

    def test_router_cannot_promote_unknown_or_nonfinite_budget(self) -> None:
        import model_router
        for provider in ('claude','opencode-go','deepseek','cerebras','google','groq','mistral'):
            for data in ({}, {'claude':{'pay':float('nan'),'pay_usd':2}}, {'go':{'available':6.666667, 'unit': 'points'}}):
                with self.subTest(provider=provider, data=data):
                    self.assertFalse(model_router.has_budget(provider, provider+'/model', data)[0])
        with patch.object(model_router, 'QE', self.root):
            self.assertTrue(model_router.has_budget('opencode-go', 'opencode-go/model', self.snapshot())[0])

    def test_provider_and_call_time_determine_spending(self) -> None:
        from budget_gate import spending
        db = self.root/'opencode.db'
        with closing(sqlite3.connect(db)) as con, con:
            con.execute('create table message(id text, data text)')
            con.execute('create table part(id text, message_id text, time_created integer, data text)')
            con.execute('insert into message values(?,?)', ('m1',json.dumps({'providerID':'deepseek','modelID':'same-model'})))
            con.execute('insert into message values(?,?)', ('m2',json.dumps({'providerID':'opencode-go','modelID':'same-model'})))
            for ident, message, stamp, cost in [('old','m1',100,7),('new','m1',2000,2),('other','m2',2000,9)]:
                con.execute('insert into part values(?,?,?,?)', (ident,message,stamp,json.dumps({'type':'step-finish','cost':cost})))
        self.assertEqual(spending(db, 'deepseek', 1), (2.0,1))
        self.assertIsNone(spending(self.root/'absent.db','deepseek',0))

    def test_budget_report_does_not_restore_credit_when_database_is_missing(self) -> None:
        import provider_budget
        (self.root/'chain').mkdir()
        stdout = io.StringIO()
        with patch.object(provider_budget, 'QE', self.root), patch.object(provider_budget, 'DB', self.root/'missing.db'), \
             patch.object(Path, 'home', return_value=self.root), \
             patch('sys.stdout', stdout):
            provider_budget.main()
        snapshot = json.loads(stdout.getvalue())
        for provider in ('go','groq'):
            self.assertEqual(snapshot[provider]['db'], 'unreachable')
        self.assertEqual(snapshot['groq']['ratio'], 0)

    def test_direct_helper_reserves_before_network_and_records_unknown_failure(self) -> None:
        import plan_ds
        snapshot = {**self.snapshot(), 'groq':{'db':'ok','today_request':899}}
        def refused(_request, **_kwargs):
            with closing(sqlite3.connect(self.root/'chain/request-budget.db')) as con:
                self.assertEqual(con.execute('select reserved,actual from reservation').fetchall(), [(1,None)])
            raise ConnectionError('interrupted')
        with patch.object(plan_ds, 'QE', self.root), patch.object(plan_ds, 'observed_budget', return_value=snapshot), \
             patch.object(plan_ds, 'credentials', return_value='fixture-placeholder'):
            with self.assertRaises(ConnectionError):
                plan_ds.generate('groq', 'fixture', 'prompt', opener=refused)
            with self.assertRaises(self.module.BudgetDenied):
                plan_ds.generate('groq', 'fixture', 'prompt', opener=refused)

    def test_unknown_or_paid_direct_call_never_reads_credentials_or_invokes_provider(self) -> None:
        import plan_ds
        with patch.object(plan_ds, 'QE', self.root), patch.object(plan_ds, 'observed_budget', return_value=None), \
             patch.object(plan_ds, 'credentials') as credentials, patch.object(plan_ds.urllib.request, 'urlopen') as network:
            for provider in ('deepseek','cerebras','groq','mistral'):
                with self.assertRaises(self.module.BudgetDenied):
                    plan_ds.generate(provider, 'fixture', 'prompt')
        credentials.assert_not_called()
        network.assert_not_called()


class OpenRouterBalanceTests(unittest.TestCase):
    """CR-08: the net balance does not subtract the same receipt again; obligations after the observation are not lost."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-or-balance-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.ledger = budget.BudgetLedger(self.home / 'chain/request-budget.db')
        self.old = self.ledger.reserve('openrouter', 10_000_000, self.raw_snapshot(100), {})
        self.ledger.settle(self.old, 10_000_000, self.old + ':gen-old')

    @staticmethod
    def raw_snapshot(balance):
        now = datetime.now().astimezone()
        return {'today': now.strftime('%Y-%m-%d'), 'ts': now.isoformat(),
                'or': {'db': 'ok', 'pay': balance, 'available': balance}}

    def observe(self, balance=44.8, usage=10):
        import provider_budget
        payload = {'data': {'total_credits': balance + usage, 'total_usage': usage}}
        with patch.object(provider_budget, 'QE', self.home), patch.object(provider_budget, 'net_open', return_value=True), \
             patch.object(provider_budget, 'or_key', return_value='fixture-not-a-key'), \
             patch.object(provider_budget, 'or_price'), patch.object(provider_budget, 'or_free_limit', return_value=2000), \
             patch.object(provider_budget, '_or_deviation', return_value=None), \
             patch.object(provider_budget.urllib.request, 'urlopen', side_effect=lambda *a, **k: io.StringIO(json.dumps(payload))):
            row = provider_budget._or_status()
        return {**self.raw_snapshot(balance), 'or': row}

    def test_current_balance_does_not_subtract_included_receipt_again(self):
        snapshot = self.observe()
        self.assertEqual(self.ledger.available('openrouter', snapshot), 38_080_000)
        self.assertEqual(budget.allowance(snapshot, 'openrouter')[1], 38_080_000)

    def test_account_stamp_can_follow_the_start_of_snapshot_collection(self):
        started = datetime.now().astimezone().isoformat(timespec='seconds')
        snapshot = self.observe()
        snapshot['ts'] = started  # provider_budget.main keeps the start stamp while summing all providers.
        self.assertEqual(self.ledger.available('openrouter', snapshot), 38_080_000)

    def test_cached_balance_keeps_original_ledger_observation(self):
        before = self.observe()
        rid = self.ledger.reserve('openrouter', 1_000_000, before, {})
        self.ledger.settle(rid, 1_000_000, rid + ':gen-after')
        after = self.observe()
        self.assertIn('credit_observation', before['or'])
        self.assertEqual(before['or']['credit_observation'], after['or']['credit_observation'])
        self.assertEqual(self.ledger.available('openrouter', after), 37_080_000)

    def test_pending_estimated_and_late_receipt_remain_liabilities_across_days(self):
        initial = self.raw_snapshot(100)
        late = self.ledger.reserve('openrouter', 2_000_000, initial, {})
        self.ledger.reserve('openrouter', 1_500_000, initial, {})
        estimated = self.ledger.reserve('openrouter', 2_000_000, initial, {})
        self.ledger.settle(estimated, 2_000_000, 'phantom:' + estimated)
        with self.ledger.transaction() as con:
            con.execute("update reservation set period='2000-01-01' where id=?", (estimated,))
        snapshot = self.observe()
        self.ledger.settle(late, 1_000_000, late + ':gen-late')
        self.assertEqual(self.ledger.available('openrouter', snapshot), 33_580_000)
        restarted = budget.BudgetLedger(self.ledger.path)
        self.assertEqual(restarted.available('openrouter', snapshot, read_only=True), 33_580_000)

    def test_unproven_or_changed_observation_never_restores_old_spending(self):
        original = self.observe()
        self.assertIn('credit_observation', original['or'])
        for field, value in (('ts', time.time() + 60), ('ts', time.time() - 301),
                             ('ts', float('nan')), ('ts', float('inf')), ('ts', True),
                             ('receipt_sha256', '0' * 64), ('spent_micro', 1), ('spent_micro', True)):
            snapshot = json.loads(json.dumps(original))
            snapshot['or']['credit_observation'][field] = value
            with self.subTest(field=field, value=value):
                self.assertLessEqual(self.ledger.available('openrouter', snapshot), 28_080_000)
        missing = json.loads(json.dumps(original))
        del missing['or']['credit_observation']
        self.assertLessEqual(self.ledger.available('openrouter', missing), 28_080_000)
        with self.ledger.transaction() as con:
            con.execute('update reservation set receipt=? where id=?', (self.old + ':gen-other', self.old))
        self.assertLessEqual(self.ledger.available('openrouter', original), 28_080_000)

    def test_balance_is_never_rounded_up_to_an_extra_reservation(self):
        snapshot = self.observe(balance=0.00006)
        self.assertEqual(snapshot['or']['remaining'], 0.00006)
        self.assertEqual(self.ledger.available('openrouter', snapshot), 51)
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('openrouter', 52, snapshot, {})

    def test_concurrent_admission_and_reservation_share_net_capacity(self):
        from concurrent.futures import ThreadPoolExecutor
        snapshot = self.observe(balance=1)
        self.assertEqual(self.ledger.available('openrouter', snapshot), 850_000)
        def reserve(_):
            try:
                self.ledger.reserve('openrouter', 600_000, snapshot, {})
                return 'allowed'
            except budget.BudgetDenied:
                return 'denied'
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(reserve, range(2))), ['allowed', 'denied'])
        self.assertEqual(self.ledger.available('openrouter', snapshot), 250_000)


class OpenCodePluginTests(unittest.TestCase):
    def test_provider_transport_contract_without_network(self) -> None:
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is not installed')
        result = subprocess.run([node, '--test', str(Path(__file__).with_name('budget-plugin.test.mjs'))],
                                env={**os.environ, 'CADRE_PYTHON':sys.executable}, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()


class StaleClosingTests(unittest.TestCase):
    """B110 (18.09): `--stale-close` also closed the reservation of a run whose owner was ALIVE; the in-flight call's
    receipt was later rejected with 'conflicting receipt' and the real spending disappeared from the ledger."""

    def setUp(self) -> None:
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        import budget_gate
        self.m = budget_gate
        self.db = self.f.home / 'chain/request-budget.db'
        self.ledger = budget_gate.BudgetLedger(self.db)

    def snapshot(self):
        return {'today': time.strftime('%Y-%m-%d'), 'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
                'go': {'db': 'ok', 'pay': 6.666667, 'available': 6.666667, 'window_open': True, 'unit': 'points'}}

    def old_reservation(self, run_id):
        ident = self.ledger.reserve('opencode-go', 100000, self.snapshot(), {'run_id': run_id})
        with closing(sqlite3.connect(self.db)) as con:
            con.execute('update reservation set created=? where id=?', (time.time() - 5 * 3600, ident))
            con.commit()
        return ident

    def is_closed(self, ident):
        with closing(sqlite3.connect(self.db)) as con:
            return con.execute('select actual from reservation where id=?', (ident,)).fetchone()[0] is not None

    def test_the_operator_sweep_skips_a_live_run_and_closes_a_dead_one(self):
        from run_store import FileLock, process_identity
        live = self.f.store.accept('a', 'live job')['run_id']
        self.f.store.update(live, owner_pid=os.getpid(), owner_identity=process_identity(os.getpid()))
        dead = self.f.store.accept('a', 'dead job')['run_id']
        self.f.store.update(dead, owner_pid=4_000_000)      # a pid that does not exist
        i_live, i_dead = self.old_reservation(live), self.old_reservation(dead)
        with FileLock(self.f.store.path(live) / '.owner.lock'), \
             patch('run_store.process_command', return_value=f'python3 run_store.py run {live}'), \
             patch('sys.stdout', new=io.StringIO()) as output:
            rc = self.m.recovery(self.f.home, ['--stale-close', '--yes', '--hour', '1'])
        self.assertEqual(rc, 0)
        self.assertFalse(self.is_closed(i_live), 'A live run reservation MUST NOT BE CLOSED')
        self.assertTrue(self.is_closed(i_dead), 'Close the reservation of a run whose owner died')
        self.assertIn('1 live runs untouched', output.getvalue())


class AddonSingleTests(unittest.TestCase):
    """B124: the budget plugin may now also be in the global opencode config (manual-use observation mode).
    If the same plugin loads twice every call writes TWO reservations and spending is counted twice."""

    def test_the_plugin_is_listed_once_even_when_the_global_config_already_has_it(self):
        import json as _json
        import os as _os
        import qe_core as core
        addon = (Path(core.__file__).with_name('cadre-plugin.mjs')).as_uri()
        old = (Path(core.__file__).with_name('budget-plugin.mjs')).as_uri()   # 06.10: the old name is deduped too (no double gate)
        env = {'OPENCODE_CONFIG_CONTENT': _json.dumps({'plugin': [addon, old, 'file:///other.mjs']})}
        with patch.object(core, 'budget_json', return_value={'ts': '2026-09-19T00:00:00', 'today': '2026-09-19',
                                                           'go': {'db': 'ok', 'pay': 33.333334, 'available': 33.333334,
                                                                  'window_open': True, 'unit': 'points'}}), \
             patch.object(core, 'run_record', return_value={}), \
             patch.object(core, 'available_budget', return_value=1_000_000), \
             patch.object(core, 'run_remaining_usd', return_value=5.0):
            core.opencode_budget_env(env, ['-m', 'opencode-go/glm-5.3-flash', 'task'])
        written = _json.loads(env['OPENCODE_CONFIG_CONTENT'])['plugin']
        self.assertEqual(written.count(addon), 1, f'Loading the plugin twice would double-count spending: {written}')
        self.assertIn('file:///other.mjs', written, 'Preserve the other user plugins')


class DailyCapLatchTests(unittest.TestCase):
    """B129 (19.09, caught in the first real run): the `allocation` cap was written with `min()` within the day,
    so it could ONLY GO DOWN. Once a provider's daily share dropped to zero
    it could not open again that day — even if its share later legitimately grew (pull-forward/pace).

    Live evidence: the go panel at rolling 0% (provider fully open), `allowance()` says $0.715,
    but the cap in the ledger was pinned at $0.000 so the run went into a 7-day wait."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-latch-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'chain').mkdir(parents=True)
        self.ledger = budget.BudgetLedger(self.home / 'chain/request-budget.db')

    def _divide(self, available):
        now = datetime.now().isoformat()   # with microseconds like a real observation (provider_budget.py isoformat)
        return {'ts': now, 'today': time.strftime('%Y-%m-%d'),
                'go': {'db': 'ok', 'pay': 13.333334, 'available': (available) * 100 / 15,
                       'window_open': True, 'unit': 'points'}}

    def test_a_daily_ceiling_that_dropped_to_zero_recovers_when_the_allowance_grows_again(self):
        # 1) A small reservation while the share is plentiful: the cap is set by this observation.
        self.ledger.reserve('opencode-go', 10_000, self._divide(2.0), {'run_id': 'r1'})
        # 2) The share dropped to zero (the day's slice was spent): a new call is denied — CORRECT.
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('opencode-go', 10_000, self._divide(0.0), {'run_id': 'r1'})
        # 3) The share LEGITIMATELY reopened (pull-forward/pace): it must pass now.
        # GO1 (03.10): no daily margin on Go — remaining 1.5 * 1e6 = 1_500_000 minus the pending 10_000.
        self.assertEqual(budget.available_budget(self.home, self._divide(1.5), 'opencode-go'), 1_490_000)
        try:
            self.ledger.reserve('opencode-go', 10_000, self._divide(1.5), {'run_id': 'r1'})
        except budget.BudgetDenied as exc:
            self.fail(f'The allocation reopened but the cap stayed pinned to zero: {exc}')

    def _claude(self, pay, percent1_usd=1.0, pool=100.0):
        now = time.strftime('%Y-%m-%dT%H:%M:%S')
        return {'ts': now, 'today': time.strftime('%Y-%m-%d'),
                'claude': {'source': 'fresh', 'pay': pay, 'percent1_usd': percent1_usd, 'pool_remaining_usd': pool}}

    def test_claude_ceiling_follows_the_observation_in_both_directions(self):
        """B150 (22.09, caught live): Claude has no `daily_base`; `capacity()` fell into the latch branch and a cap that
        dropped once could not rise during the 5-hour period (cap pinned at $0.51 in the ledger, observation $18.34 ->
        `available` 0, Claude dropped out of all chains). In the dollar bucket the cap follows the observation both ways."""
        ident = self.ledger.reserve('claude', 500_000, self._claude(2.0), {'run_id': 'c1'})      # pay %2 -> $2
        self.ledger.settle(ident, 500_000, 'paid')
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('claude', 500_000, self._claude(0.0), {'run_id': 'c1'})          # share exhausted: correct denial
        self.assertEqual(budget.available_budget(self.home, self._claude(18.0), 'claude'), 18_000_000,
                         'The allocation returned but the cap stayed pinned to a low value')
        self.ledger.reserve('claude', 500_000, self._claude(18.0), {'run_id': 'c1'})
        with self.assertRaises(budget.BudgetDenied):                                              # a reduced cap applies immediately
            self.ledger.reserve('claude', 900_000, self._claude(0.5), {'run_id': 'c1'})

    def test_a_shrinking_allowance_still_binds_immediately(self):
        """While loosening the latch, do not lose safety: when the share SHRINKS it must bind immediately."""
        self.ledger.reserve('opencode-go', 10_000, self._divide(2.0), {'run_id': 'r2'})
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('opencode-go', 900_000, self._divide(0.5), {'run_id': 'r2'})

    def test_go_spending_settled_after_the_observation_is_deducted(self):
        """4c QA2 (Fable): the observation may be ≤90 s stale; Go spending opened and settled AFTER the observation is not in Σ, and without
        subtracting it from the wall parallel lanes could exceed the wall. Spending settled before the observation is in Σ and not subtracted twice."""
        snapshot = self._divide(1)
        ident = self.ledger.reserve('opencode-go', 300_000, snapshot, {})
        self.ledger.settle(ident, 300_000, 'paid')
        self.assertEqual(budget.available_budget(self.home, snapshot, 'opencode-go'), 700_000)
        self.assertEqual(budget.available_budget(self.home, self._divide(1), 'opencode-go'), 1_000_000, 'Fresh observation is included in the sum')

    def test_go_spending_in_flight_at_the_observation_is_deducted_once_settled(self):
        ident = self.ledger.reserve('opencode-go', 300_000, self._divide(1), {})
        snapshot = self._divide(1)
        self.assertEqual(budget.available_budget(self.home, snapshot, 'opencode-go'), 700_000)
        self.ledger.settle(ident, 300_000, 'paid')
        self.assertEqual(budget.available_budget(self.home, snapshot, 'opencode-go'), 700_000)
        self.assertEqual(budget.available_budget(self.home, self._divide(1), 'opencode-go'), 1_000_000)

    def test_admission_and_reservation_share_the_measured_daily_ceiling(self):
        # 4c-13: the Go observation already includes the receipt (API + Σ after the reading); the ledger subtracts only the pending, no double subtraction
        ident = self.ledger.reserve('opencode-go', 700_000, self._divide(2), {})
        self.ledger.settle(ident, 700_000, 'paid')
        snapshot = self._divide(1)
        self.assertEqual(budget.available_budget(self.home, snapshot, 'opencode-go'), 1_000_000)
        self.ledger.reserve('opencode-go', 400_000, snapshot, {})
        available = budget.available_budget(self.home, snapshot, 'opencode-go')
        self.assertEqual(available, 600_000)
        self.ledger.reserve('opencode-go', available, snapshot, {})
        with self.assertRaises(budget.BudgetDenied):
            self.ledger.reserve('opencode-go', 1, snapshot, {})

    def test_counted_previous_period_hold_does_not_block_the_next_period_reservation(self):
        with patch.dict(os.environ, {'CADRE_FREE_DAY_CAP': '2'}):
            ident = self.ledger.reserve('opencode:free', 1, self._divide(2), {})
            with self.ledger.transaction() as con:
                con.execute("update reservation set period='2000-01-01' where id=?", (ident,))
            self.assertEqual(budget.available_budget(self.home, self._divide(2), 'opencode:free'), 2)
            self.ledger.reserve('opencode:free', 2, self._divide(2), {})


class MidnightCacheTest(unittest.TestCase):
    def test_yesterdays_observation_is_measured_again_even_within_60_sec(self):
        """B327 (03.10 00:00:04, live): an observation cached at 23:59 was reused after midnight; its date was
        yesterday so the gate said 'stale budget', the Go brake closed and a large job fell to local. The cache is invalid when the day changes."""
        import datetime as _dt, json as _json, tempfile as _tf, time as _t
        from pathlib import Path as _P
        import budget_gate as bg
        with _tf.TemporaryDirectory(prefix='qe-night-') as d:
            home, code = _P(d) / 'home_dir', _P(d) / 'code'
            (home / 'chain').mkdir(parents=True); (code / 'lib').mkdir(parents=True)
            today = _dt.date.today().isoformat()
            yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
            (code / 'lib/provider_budget.py').write_text(f'import json;print(json.dumps({{"today": "{today}", "source": "fresh"}}))\n')
            (home / 'chain/request-budget.json').write_text(_json.dumps({'observed': _t.time() - 30, 'budget': {'today': yesterday}}))
            self.assertEqual(bg.observed_budget(home, code)['today'], today)


class CalibrationSampleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='calibration-receipts-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.ledger = budget.BudgetLedger(self.home / 'chain/request-budget.db')
        self.now = time.time()
        self.index = 0

    def row(self, con, provider='opencode', *, age=0, receipt=None, actual=50_000, **metadata):
        self.index += 1
        ident = f'c{self.index}'
        receipt = receipt if receipt is not None else 'response-' + ident
        meta = {'run_id': 'sample', 'worst': 1_000_000, 'slice': 'judge', **metadata}
        con.execute('insert into reservation values(?,?,?,?,?,?,?,?)',
                    (ident, provider, 'period', self.now-age, 100_000, actual, receipt, json.dumps(meta)))

    def test_seven_day_ratio_requires_30_real_receipts_or_10_for_claude_without_estimated_unused_or_future_samples(self):
        for provider, minimum in (('opencode', 30), ('claude', 10)):
            with self.ledger.transaction() as con, patch('budget_gate.time.time', return_value=self.now):
                for _ in range(minimum-1):
                    self.row(con, provider)
                for receipt in ('phantom:' + provider, 'recovery:' + provider):
                    self.row(con, provider, receipt=receipt)
                if provider == 'opencode':
                    self.row(con, provider, receipt='')
                self.row(con, provider, assurance=True, actual=0)
                self.row(con, provider, observation=True)
                self.row(con, provider, age=-60)
                self.row(con, provider, age=7*86400)
                self.assertEqual(self.ledger.calibrated(con, provider, 1_000_000), 1_000_000)
                self.row(con, provider)
                self.assertEqual(self.ledger.calibrated(con, provider, 1_000_000), 100_000)

    def test_fourteen_day_bucket_needs_five_real_samples_and_is_independent_of_the_seven_day_ratio(self):
        import provider_budget
        with self.ledger.transaction() as con:
            for _ in range(4):
                self.row(con, age=8*86400, actual=100_000)
            for receipt in ('phantom:old', 'recovery:old', ''):
                self.row(con, receipt=receipt, actual=100_000)
            self.row(con, actual=100_000, assurance=True)
            self.row(con, actual=100_000, observation=True)
            self.row(con, age=-60, actual=100_000)
            self.row(con, age=14*86400, actual=100_000)
        with patch.object(provider_budget, 'QE', self.home):
            self.assertEqual(provider_budget.calibrated_measure(self.now), {})
            with self.ledger.transaction() as con:
                self.row(con, actual=100_000)
            self.assertEqual(provider_budget.calibrated_measure(self.now), {'judge': {'opencode': 0.15}})
        with self.ledger.transaction() as con, patch('budget_gate.time.time', return_value=self.now):
            self.assertEqual(self.ledger.calibrated(con, 'opencode', 1_000_000), 1_000_000)
