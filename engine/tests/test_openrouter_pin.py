"""OR-PIN: the price observation opens only approved fp8 and cached routes."""
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import provider_budget


class OpenRouterPinTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        (self.home / 'chain').mkdir()
        (self.home / 'chain/openrouter-key').write_text('fixture-not-a-key')
        self.enterContext(patch.object(provider_budget, 'QE', self.home))
        self.enterContext(patch.object(provider_budget, 'EXECUTOR_ID', self.home / 'absent/auth.json'))

    def test_only_allowed_cached_fp8_endpoints_set_prices(self):
        def endpoint(name, quant='fp8', cache='0.0000001', price='0.000001'):
            return {'provider_name': name, 'tag': name.lower() + ('/' + quant if quant in ('fp8', 'fp4') else ''),
                    'quantization': quant, 'pricing': {
                'prompt': price, 'completion': '0.000002', 'input_cache_read': cache}}
        rows = [endpoint('StreamLake'), endpoint('DeepInfra', price='0.0000015'),
                endpoint('Relace', price='0.01'), endpoint('DeepInfra', quant='fp4', price='0.01'),
                endpoint('StreamLake', cache=None), endpoint('StreamLake', price='Infinity')]
        requests = []
        def fetch(request, timeout):
            requests.append(request.full_url)
            return io.StringIO(json.dumps({'data': {'endpoints': rows}}))
        with patch.object(provider_budget.urllib.request, 'urlopen', side_effect=fetch):
            models = provider_budget.or_price()
        ds = models['deepseek/deepseek-v4-pro']
        self.assertEqual((ds['input'], ds['output'], ds['providers'], ds['fp8_cache']), (1.5, 2, ['streamlake/fp8', 'deepinfra/fp8'], True))
        self.assertEqual({endpoint: (round(k['cache'], 9), k['entry_name']) for endpoint, k in ds['tiers'].items()},
                         {'streamlake/fp8': (0.1, 'StreamLake'), 'deepinfra/fp8': (0.1, 'DeepInfra')},
                         'CR-48: per-endpoint cache read prices and response display names measure cache loss during session handoff')
        # 4c-7: glm by rule (fp8 + maximum price $1.4/$4.4) — every suitable endpoint without a list; for DS-flash both endpoints are above the maximum price
        self.assertEqual(models['z-ai/glm-5.3']['providers'], ['streamlake/fp8'], 'Rule: use the endpoint slug, not its display name')
        self.assertEqual(sorted(models), ['deepseek/deepseek-v4-pro', 'z-ai/glm-5.3'])
        self.assertTrue(all(url.endswith('/endpoints') for url in requests))

    def test_the_router_never_offers_an_openrouter_model_the_plugin_cannot_price(self):
        """07.10 smoke: the plan chain offered OR gpt-6.1-sol, the plugin denied it for lack of an OR-PIN price (an empty attempt on every run)."""
        import model_router
        (self.home / 'chain/openrouter-price.json').write_text(json.dumps({'version': 2, 'ts': time.time(), 'models': {
            'deepseek/deepseek-v4-pro': {'input': 1.3, 'output': 2.6, 'providers': ['streamlake/fp8'], 'fp8_cache': True}}}))
        with patch.object(model_router, 'QE', self.home), patch('budget_gate.available_budget', return_value=5_000_000):
            self.assertFalse(model_router.has_budget('openrouter', 'openrouter/openai/gpt-6.1-sol', {'or': {}}, 1000, role='plan')[0])
            self.assertTrue(model_router.has_budget('openrouter', 'openrouter/deepseek/deepseek-v4-pro', {'or': {}}, 1000, role='impl')[0])
            (self.home / 'chain/openrouter-price.json').write_text(json.dumps({'version': 2, 'ts': time.time() - 4000, 'models': {
                'deepseek/deepseek-v4-pro': {'input': 1.3, 'output': 2.6, 'providers': ['streamlake/fp8'], 'fp8_cache': True}}}))
            self.assertFalse(model_router.has_budget('openrouter', 'openrouter/deepseek/deepseek-v4-pro', {'or': {}}, 1000, role='impl')[0])

    def test_closed_weight_gpt_uses_only_the_permitted_standard_tier_within_the_price_cap(self):
        """N-TS01/N-RT04: the price observation opens only the allowed standard `openai` endpoint; FLEX, Azure and fast tiers do not change the reserve."""
        def endpoint(tag, input, output, param):
            return {'provider_name': tag.split('/')[0], 'tag': tag, 'quantization': 'unknown', 'max_completion_tokens': 128000,
                    'supported_parameters': [param, 'reasoning', 'tools'],
                    'pricing': {'prompt': str(input / 1e6), 'completion': str(output / 1e6), 'input_cache_read': str(input / 1e7)}}
        rows = {'openai/gpt-6.1-sol': [endpoint('openai/flex', 1, 5, 'max_tokens'), endpoint('azure', 2, 10, 'max_completion_tokens'),
                                       endpoint('openai', 2, 10, 'max_tokens'), endpoint('azure/us', 2.2, 11, 'max_completion_tokens'),
                                       endpoint('amazon-bedrock/us-east-1', 2.2, 11, 'max_tokens'), endpoint('openai/fast', 4, 20, 'max_tokens')]}
        def fetch(request, timeout):
            entry_name = request.full_url.split('/models/', 1)[1].rsplit('/endpoints', 1)[0]
            return io.StringIO(json.dumps({'data': {'endpoints': rows.get(entry_name, [
                {'provider_name': 'StreamLake', 'quantization': 'unknown', 'pricing': {'prompt': '0.000001', 'completion': '0.000002', 'input_cache_read': '0.0000001'}}])}}))
        with patch.object(provider_budget.urllib.request, 'urlopen', side_effect=fetch):
            models = provider_budget.or_price()
        sol = models['openai/gpt-6.1-sol']
        self.assertEqual(sol['providers'], ['openai'])
        self.assertEqual((sol['input'], sol['output'], sol['closed'], sol['max_price']), (2, 10, True, {'prompt': 2, 'completion': 10}))
        self.assertEqual(sol['tiers']['openai']['params'], ['max_tokens'])
        self.assertNotIn('openai/flex', sol['tiers'])
        self.assertNotIn('deepseek/deepseek-v4-pro', models, 'An open-weight endpoint with unknown quantization remains closed')

    def test_failed_refresh_keeps_the_last_good_price_for_a_while_and_retries_soon(self):
        """4c-7: a failed refresh wrote an empty file and closed OR for an hour. The last good price stays within the age limit (6 h),
        it is retried after 5 min; a price past the limit drops."""
        path = self.home / 'chain/openrouter-price.json'
        good = {'input': 1.3, 'output': 2.6, 'providers': ['streamlake/fp8'], 'fp8_cache': True}
        path.write_text(json.dumps({'version': 2, 'id': 'label', 'ts': time.time() - 4000, 'models': {
            'deepseek/deepseek-v4-pro': {**good, 'price_ts': time.time() - 4000},
            'z-ai/glm-5.3': {**good, 'price_ts': time.time() - 7 * 3600}}}))
        with patch.object(provider_budget.urllib.request, 'urlopen', side_effect=OSError('offline')):
            self.assertEqual(list(provider_budget.or_price()), ['deepseek/deepseek-v4-pro'])
        d = json.loads(path.read_text())
        self.assertAlmostEqual(d['next'] - d['ts'], 300)
        with patch.object(provider_budget.urllib.request, 'urlopen', side_effect=AssertionError('Retried before five minutes elapsed')):
            self.assertEqual(list(provider_budget.or_price()), ['deepseek/deepseek-v4-pro'])

    def test_prices_are_refreshed_before_the_plugin_considers_them_stale(self):
        path = self.home / 'chain/openrouter-price.json'
        good = {'input': 1.3, 'output': 2.6, 'providers': ['streamlake/fp8'], 'fp8_cache': True}
        ts = time.time() - 3400
        path.write_text(json.dumps({'version': 2, 'id': 'label', 'ts': ts, 'next': ts + 3600,
                                    'models': {'deepseek/deepseek-v4-pro': {**good, 'price_ts': ts}}}))
        with patch.object(provider_budget.urllib.request, 'urlopen', side_effect=OSError('refresh attempted')) as call:
            provider_budget.or_price()
        self.assertTrue(call.called, 'Refresh five minutes before expiry')
        rows = [{'provider_name': 'StreamLake', 'tag': 'streamlake/fp8', 'quantization': 'fp8',
                 'pricing': {'prompt': '0.0000013', 'completion': '0.0000026', 'input_cache_read': '0.0000001'}}]
        path.write_text(json.dumps({'version': 2, 'id': 'label', 'ts': ts, 'models': {}}))
        with patch.object(provider_budget.urllib.request, 'urlopen', side_effect=lambda r, timeout: io.StringIO(json.dumps({'data': {'endpoints': rows}}))):
            provider_budget.or_price()
        d = json.loads(path.read_text())
        self.assertAlmostEqual(d['next'] - d['ts'], 3300, delta=1)

    def test_a_price_file_with_old_endpoint_names_is_refetched_at_once_and_never_carried(self):
        path = self.home / 'chain/openrouter-price.json'
        path.write_text(json.dumps({'version': 2, 'ts': time.time(), 'next': time.time() + 3600, 'models': {
            'deepseek/deepseek-v4-pro': {'input': 1.3, 'output': 2.6, 'providers': ['StreamLake'], 'fp8_cache': True}}}))
        with patch.object(provider_budget.urllib.request, 'urlopen', side_effect=OSError('offline')) as net:
            self.assertEqual(provider_budget.or_price(), {})
        self.assertTrue(net.called, 'An old format does not count as fresh')
        self.assertEqual(json.loads(path.read_text())['id'], 'label')

    def test_no_cache_price_or_quantization_means_no_route(self):
        rows = [{'provider_name': 'DeepInfra', 'pricing': {'prompt': '.1', 'completion': '.1'}}]
        with patch.object(provider_budget.urllib.request, 'urlopen', side_effect=lambda *a, **k:
                          io.StringIO(json.dumps({'data': {'endpoints': rows}}))):
            self.assertEqual(provider_budget.or_price(), {})

    def test_unreadable_ledger_and_panel_disagreement_close_the_provider(self):
        with patch.object(provider_budget, '_or_ledger_usd', return_value=None):
            self.assertEqual(provider_budget._or_deviation(1)['reason'], 'ledger-unreadable')
        with patch.object(provider_budget, '_or_ledger_usd', return_value=1):
            self.assertIsNone(provider_budget._or_deviation(1), 'The first observation becomes the baseline')
            self.assertEqual(provider_budget._or_deviation(3)['deviation'], 1, 'panel +2, ledger +0 -> closed')
            self.assertIsNotNone(provider_budget._or_deviation(float('nan')))

    def test_divergence_is_measured_from_a_baseline_not_lifetime_totals(self):
        """06.10 review: the lifetime total comparison counted spending before the ledger (the $9.8 loss) as a permanent drift and never
        opened OR. The first observation becomes the baseline; drift is measured only on increases after the baseline."""
        ledger = {'v': 0.0}
        with patch.object(provider_budget, '_or_ledger_usd', side_effect=lambda: ledger['v']):
            self.assertIsNone(provider_budget._or_deviation(9.8))
            base = json.loads((self.home / 'chain/openrouter-base.json').read_text())
            self.assertEqual((base['api_usd'], base['ledger_usd']), (9.8, 0.0))
            ledger['v'] = 1.0
            self.assertIsNone(provider_budget._or_deviation(10.8), 'panel +1.0, ledger +1.0 -> consistent')
            ledger['v'] = 1.5
            deviation = provider_budget._or_deviation(13.0)
            self.assertIsNotNone(deviation, 'panel +3.2, ledger +1.5 -> %53 deviation, $1.7 difference -> closed')
            self.assertEqual((deviation['api_usd'], deviation['ledger_usd']), (3.2, 1.5))

    def test_estimated_closures_are_a_tolerance_band_not_spend(self):
        measured, estimated = {'v': 0.0}, {'v': 0.0}
        with patch.object(provider_budget, '_or_ledger_usd', side_effect=lambda: measured['v']), \
             patch.object(provider_budget, '_or_estimated_usd', side_effect=lambda: estimated['v']):
            self.assertIsNone(provider_budget._or_deviation(5.0))
            measured['v'], estimated['v'] = 0.02, 0.46
            self.assertIsNone(provider_budget._or_deviation(5.02), 'Phantom call never happened; invoice equals measured spending')
            self.assertIsNone(provider_budget._or_deviation(5.47), 'Phantom call happened; invoice equals the upper bound')
            measured['v'], estimated['v'] = 0.5, 0.66
            self.assertIsNotNone(provider_budget._or_deviation(8.0), 'invoice +3, ledger range 0.5-1.16: deviation')

    def test_ledger_ahead_of_a_lagging_credit_api_closes_only_after_the_reporting_window(self):
        all_items, old = {'v': 0.0}, {'v': 0.0}
        with patch.object(provider_budget, '_or_ledger_usd', side_effect=lambda: all_items['v']), \
             patch.object(provider_budget, '_or_ledger_old_usd', side_effect=lambda limit: old['v']):
            self.assertIsNone(provider_budget._or_deviation(22.6))
            all_items['v'] = 0.58
            self.assertIsNone(provider_budget._or_deviation(22.64), 'The credit API reports recent spending late; the ledger is ahead, so do not close within the window')
            old['v'] = 0.58
            deviation = provider_budget._or_deviation(22.64)
            self.assertIsNotNone(deviation, 'Spending older than the window still missing from the API is a deviation')
            self.assertEqual((deviation['api_usd'], deviation['ledger_usd']), (0.04, 0.58))
            self.assertIsNotNone(provider_budget._or_deviation(23.9), 'If API spending exceeds the ledger, untracked spending still closes the provider')

    def test_cut_stream_reservations_close_from_the_generation_record_after_their_attempt_ended(self):
        import budget_gate
        import monthly_budget
        request = budget_gate.BudgetLedger(self.home / 'chain/request-budget.db')
        monthly = monthly_budget.MonthlyLedger(self.home / 'chain/monthly-budget.db', cap=100_000_000)
        old = time.time() - 120

        def attempt(entry_name, outcome):
            path = self.home / 'runs/run-1/tasks/g1/attempts' / entry_name
            path.mkdir(parents=True, exist_ok=True)
            (path / 'attempt.json').write_text(json.dumps({'attempt_id': entry_name, 'outcome': outcome}))

        lines = {'A': ('finished-a', 'finished', 'gen-aaa'), 'B': ('lasting-b', 'unknown', 'gen-bbb'),
                    'C': ('finished-c', 'finished', 'gen-ccc'), 'D': ('finished-d', 'finished', None)}
        with request.transaction() as con, monthly.transaction() as open:
            for ident, (entry_name, outcome, production) in lines.items():
                attempt(entry_name, outcome)
                meta = {'run_id': 'run-1', 'task_id': 'g1', 'attempt_id': entry_name, 'monthly_id': 'M' + ident,
                        **({'generation': production} if production else {})}
                con.execute('insert into reservation values(?,?,?,?,?,?,?,?)',
                            (ident, 'openrouter', 'p', old, 300_000, None, None, json.dumps(meta)))
                open.execute('insert into reservation values(?,?,?,?,?,?,?,?)',
                             ('M' + ident, 'openrouter', '2026-10', old, 300_000, None, None, json.dumps({'session_id': 's'})))
        requested = []

        def breakdown(request, timeout):
            requested.append(request.full_url)
            if request.full_url.endswith('gen-ccc'):
                raise OSError('breakdown not yet available')
            return io.StringIO(json.dumps({'data': {'total_cost': 0.0123}}))
        with patch.object(provider_budget.urllib.request, 'urlopen', side_effect=breakdown):
            self.assertEqual(provider_budget.or_pending_close(), 1)
            self.assertEqual(provider_budget.or_pending_close(), 0, 'Do not query a closed row again')
        self.assertEqual(sorted(u.rsplit('=', 1)[1] for u in requested), ['gen-aaa', 'gen-ccc', 'gen-ccc'],
                         'Do not request a breakdown for an active attempt or a row without a production identity')
        with request.transaction() as con, monthly.transaction() as open:
            status = {r[0]: (r[1], r[2]) for r in con.execute('select id, actual, receipt from reservation')}
            monthly_status = {r[0]: (r[1], r[2]) for r in open.execute('select id, actual, receipt from reservation')}
        self.assertEqual(status['A'], (12_300, 'A:gen-aaa'))
        self.assertEqual(monthly_status['MA'], (12_300, 'MA:gen-aaa'), 'The monthly ledger settles with the same amount')
        for ident in ('B', 'C', 'D'):
            self.assertEqual(status[ident], (None, None), ident)
            self.assertEqual(monthly_status['M' + ident], (None, None), ident)

    def test_generation_records_and_credits_are_read_with_the_executor_key_that_made_the_calls(self):
        import budget_gate
        request = budget_gate.BudgetLedger(self.home / 'chain/request-budget.db')
        path = self.home / 'runs/run-1/tasks/g1/attempts/finished'
        path.mkdir(parents=True)
        (path / 'attempt.json').write_text(json.dumps({'attempt_id': 'finished', 'outcome': 'finished'}))
        with request.transaction() as con:
            con.execute('insert into reservation values(?,?,?,?,?,?,?,?)', ('A', 'openrouter', 'p', time.time() - 120, 300_000, None, None,
                        json.dumps({'run_id': 'run-1', 'task_id': 'g1', 'attempt_id': 'finished', 'generation': 'gen-aaa'})))
        id = self.home / 'opencode/auth.json'
        id.parent.mkdir()
        id.write_text(json.dumps({'openrouter': {'type': 'api', 'key': 'fixture-executor-key'}}))
        titles = []

        def answer(request, timeout):
            titles.append((request.full_url.split('?')[0].rsplit('/', 1)[1], request.get_header('Authorization')))
            if 'generation' in request.full_url:
                return io.StringIO(json.dumps({'data': {'total_cost': 0.01}}))
            return io.StringIO(json.dumps({'data': {'total_credits': 10, 'total_usage': 1}}))
        with patch.object(provider_budget, 'EXECUTOR_ID', id), patch.object(provider_budget.urllib.request, 'urlopen', side_effect=answer):
            self.assertEqual(provider_budget.or_pending_close(), 1)
            provider_budget.or_balance()
        self.assertEqual(titles, [('generation', 'Bearer fixture-executor-key'), ('credits', 'Bearer fixture-executor-key')])
        titles.clear()
        (self.home / 'chain/openrouter-credit.json').unlink(missing_ok=True)
        with patch.object(provider_budget.urllib.request, 'urlopen', side_effect=answer):
            provider_budget.or_balance()
        self.assertEqual(titles, [('credits', 'Bearer fixture-not-a-key')], 'Use the engine key when there is no executor identity')

    def test_small_absolute_differences_do_not_close_the_provider(self):
        ledger = {'v': 0.0}
        with patch.object(provider_budget, '_or_ledger_usd', side_effect=lambda: ledger['v']):
            self.assertIsNone(provider_budget._or_deviation(5.0))
            ledger['v'] = 0.1
            self.assertIsNone(provider_budget._or_deviation(5.3), 'A 67 percent ratio with a 0.20 USD difference below 0.50 USD is noise; do not close')
            ledger['v'] = 0.0   # the ledger was rolled back (reset) -> the baseline is renewed, no closing
            self.assertIsNone(provider_budget._or_deviation(4.0))
            self.assertEqual(json.loads((self.home / 'chain/openrouter-base.json').read_text())['api_usd'], 4.0)

    def test_nonfinite_credit_observation_never_opens_the_wallet(self):
        path = self.home / 'chain/openrouter-credit.json'
        path.write_text(json.dumps({'ts': time.time(), 'remaining': float('inf')}))
        self.assertIsNone(provider_budget.or_balance())
        path.unlink()
        with patch.object(provider_budget.urllib.request, 'urlopen', side_effect=lambda *a, **k:
                          io.StringIO(json.dumps({'data': {'total_credits': 'Infinity', 'total_usage': 0}}))):
            self.assertIsNone(provider_budget.or_balance())

    def test_fault_clear_resets_the_openrouter_baseline_and_closure(self):
        import budget_gate
        for entry_name in ('openrouter-deviation.json', 'openrouter-base.json'):
            (self.home / 'chain' / entry_name).write_text('{}')
        (self.home / 'chain/openrouter-credit.json').write_text(json.dumps({'closed': True}))
        with patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(budget_gate.recovery(self.home, ['--failure-clean', 'openrouter', '--yes']), 0)
        for entry_name in ('openrouter-deviation.json', 'openrouter-base.json', 'openrouter-credit.json'):
            self.assertFalse((self.home / 'chain' / entry_name).exists(), entry_name)

    def test_an_ended_deviation_neither_lists_nor_closes_openrouter(self):
        """4c-7: the drift file left from 04.10 listed OR as failed even though the drift had ended. A drift-free credit observation cleans
        the file; a persistent alarm (receipt ↔ breakdown) keeps OR closed until cleared manually."""
        import budget_gate
        deviation = self.home / 'chain/openrouter-deviation.json'
        deviation.write_text(json.dumps({'api_usd': 9.84, 'ledger_usd': 1.25, 'deviation': 0.87}))
        (self.home / 'chain/openrouter-credit.json').write_text(json.dumps({'ts': time.time() - 600, 'remaining': 5.0}))
        self.assertFalse(budget_gate.or_deviation_active(self.home))
        with patch('sys.stdout', new_callable=io.StringIO) as out:
            budget_gate.recovery(self.home, ['--failure-list'])
        self.assertIn('pricing_fault is empty', out.getvalue())
        credit = {'data': {'total_credits': 10, 'total_usage': 4}}
        with patch.object(provider_budget, 'net_open', return_value=True), patch.object(provider_budget, '_or_deviation', return_value=None), \
             patch.object(provider_budget.urllib.request, 'urlopen', side_effect=lambda *a, **k: io.StringIO(json.dumps(credit))):
            self.assertEqual(provider_budget.or_balance(), 6.0)
            self.assertFalse(deviation.exists(), 'Deviation resolved; remove the old file')
            deviation.write_text(json.dumps({'reason': 'receipt-breakdown', 'permanent': True, 'samples': [{}]}))
            (self.home / 'chain/openrouter-credit.json').unlink()
            self.assertIsNone(provider_budget.or_balance(), 'permanent alarm OR yi closed amount')
        self.assertTrue(budget_gate.or_deviation_active(self.home))

    def test_sampled_receipts_must_match_the_openrouter_generation_log(self):
        """4c-7(a): ledger receipts are sample-compared with OR's call breakdown; on a mismatch, a persistent drift, OR closes."""
        import sqlite3
        from contextlib import closing
        db = self.home / 'chain/request-budget.db'
        with closing(sqlite3.connect(db)) as con:
            con.execute('create table reservation(id text primary key, provider text, period text, created real, reserved integer, '
                        'actual integer, receipt text unique, metadata text)')
            for i, (actual, gen) in enumerate(((1000, 'gen-a'), (2000, 'gen-b'), (3000, 'free-request'))):
                con.execute('insert into reservation values(?,?,?,?,?,?,?,?)',
                            (f'r{i}', 'openrouter', 'p', time.time() - 600 - i, 5000, actual, f'r{i}:{gen}', '{}'))
            con.commit()
        breakdown = {'gen-a': 0.001, 'gen-b': 0.0021}
        def fetch(request, timeout):
            gen = request.full_url.rsplit('id=', 1)[1]
            return io.StringIO(json.dumps({'data': {'total_cost': breakdown[gen]}}))
        with patch.object(provider_budget, 'net_open', return_value=True), patch.object(provider_budget.urllib.request, 'urlopen', side_effect=fetch):
            self.assertIsNone(provider_budget.or_receipt_audit(), 'A 0.0001 USD difference is within tolerance')
            self.assertFalse((self.home / 'chain/openrouter-deviation.json').exists())
            self.assertIsNone(provider_budget.or_receipt_audit(), 'Do not sample again within 10 minutes')
        record = self.home / 'chain/openrouter-receipt-audit.json'
        record.write_text(json.dumps({'ts': 0, 'tried': []}))
        breakdown['gen-b'] = 0.009
        with patch.object(provider_budget, 'net_open', return_value=True), patch.object(provider_budget.urllib.request, 'urlopen', side_effect=fetch):
            nonconforming = provider_budget.or_receipt_audit()
        self.assertEqual(([u['reservation'] for u in nonconforming['samples']], nonconforming['closed']), (['r1'], False))
        self.assertFalse((self.home / 'chain/openrouter-deviation.json').exists(), 'One sample produces a warning, not closure')
        k = json.loads(record.read_text())
        k.update(ts=0, tried=[d for d in k['tried'] if d != 'r0'])
        record.write_text(json.dumps(k))
        breakdown['gen-a'] = 0.005
        with patch.object(provider_budget, 'net_open', return_value=True), patch.object(provider_budget.urllib.request, 'urlopen', side_effect=fetch):
            nonconforming = provider_budget.or_receipt_audit()
        self.assertEqual(([u['reservation'] for u in nonconforming['samples']], nonconforming['closed']), (['r0'], True))
        deviation = json.loads((self.home / 'chain/openrouter-deviation.json').read_text())
        self.assertEqual((deviation['reason'], deviation['permanent']), ('receipt-breakdown', True))
        # 07.10 live: the first check sampled receipts from the 04.10 incident night (already judged) and closed OR.
        # Receipts before the baseline (first observation / failure cleanup) are not sampled.
        (self.home / 'chain/openrouter-deviation.json').unlink()
        record.write_text(json.dumps({'ts': 0, 'tried': []}))
        (self.home / 'chain/openrouter-base.json').write_text(json.dumps({'ts': time.time() - 300, 'api_usd': 1, 'ledger_usd': 1}))
        with patch.object(provider_budget, 'net_open', return_value=True), patch.object(provider_budget.urllib.request, 'urlopen', side_effect=fetch):
            self.assertIsNone(provider_budget.or_receipt_audit())
        self.assertFalse((self.home / 'chain/openrouter-deviation.json').exists())

    def test_openrouter_admission_uses_the_worst_case_like_openrouter_itself(self):
        """07.10 16(b) live: a Sol plan call that passed with a calibrated $0.09 reservation failed OR's own worst-case check
        (balance $0.16 < $0.78) and the reservation was left pending. On OR admission is measured with the worst case; the allocated amount stays calibrated."""
        import datetime
        import sqlite3
        from contextlib import closing
        import budget_gate as bg
        now_ts = datetime.datetime.now()
        instant = {'ts': now_ts.isoformat(), 'today': now_ts.date().isoformat(),
                 'or': {'db': 'ok', 'pay': 0.16, 'available': 0.16, 'remaining': 0.16}}   # remaining with margin $0.136
        ledger = bg.BudgetLedger(self.home / 'chain/request-budget.db')
        with patch.object(bg.BudgetLedger, 'calibrated', return_value=91_693):
            with self.assertRaises(bg.BudgetDenied):
                ledger.reserve('openrouter', 782_292, instant, {'model': 'openai/gpt-6.1-sol'})
            id = ledger.reserve('openrouter', 100_000, instant, {'model': 'openai/gpt-6.1-sol'})
        with closing(sqlite3.connect(self.home / 'chain/request-budget.db')) as con:
            self.assertEqual(con.execute('select reserved from reservation where id=?', (id,)).fetchone()[0], 91_693)
            self.assertEqual(con.execute('select count(*) from reservation').fetchone()[0], 1, 'A denied request leaves no row')

    def test_the_router_skips_an_or_model_whose_endpoints_cannot_carry_the_role_output(self):
        """4c QA (Fable): the plugin removed an endpoint that could not carry the request at request time, yet the router still offered it (an empty link)."""
        import model_router
        endpoint = lambda n: {'input': 1.3, 'output': 2.6, 'params': ['max_tokens'], 'max_output': n, 'quantization': 'fp8'}
        (self.home / 'chain/openrouter-price.json').write_text(json.dumps({'version': 2, 'ts': time.time(), 'models': {
            'deepseek/deepseek-v4-pro': {'input': 1.3, 'output': 2.6, 'providers': ['deepinfra/fp8'], 'fp8_cache': True,
                                         'price_ts': time.time(), 'tiers': {'deepinfra/fp8': endpoint(16384)}}}}))
        with patch.object(model_router, 'QE', self.home), patch('budget_gate.available_budget', return_value=5_000_000):
            self.assertFalse(model_router.has_budget('openrouter', 'openrouter/deepseek/deepseek-v4-pro', {'or': {}}, 1000, role='impl')[0])

    def test_an_endpoint_without_a_parameter_list_is_unknown_not_empty_and_old_prices_keep_their_age(self):
        """4c QA (Fable): an endpoint with no parameter list was written as [] and removed in the plugin; an old record without a stamp was refreshed as it was carried over."""
        rows = [{'provider_name': 'StreamLake', 'tag': 'streamlake/fp8', 'quantization': 'fp8', 'max_completion_tokens': 384000,
                 'pricing': {'prompt': '0.0000002', 'completion': '0.0000004', 'input_cache_read': '0.00000002'}}]
        with patch.object(provider_budget.urllib.request, 'urlopen', side_effect=lambda *a, **k: io.StringIO(json.dumps({'data': {'endpoints': rows}}))):
            ds = provider_budget.or_price()['deepseek/deepseek-v4-pro']
        self.assertIsNone(ds['tiers']['streamlake/fp8']['params'])
        path = self.home / 'chain/openrouter-price.json'
        old_ts = time.time() - 3 * 3600
        path.write_text(json.dumps({'version': 2, 'id': 'label', 'ts': old_ts,
                                    'models': {'deepseek/deepseek-v4-pro': {'input': 1, 'output': 2}}}))
        with patch.object(provider_budget.urllib.request, 'urlopen', side_effect=OSError('offline')):
            provider_budget.or_price()
        self.assertAlmostEqual(json.loads(path.read_text())['models']['deepseek/deepseek-v4-pro']['price_ts'], old_ts, 3)


if __name__ == '__main__':
    unittest.main()
