"""Claude budget enforcement at the outbound messages request boundary."""
from __future__ import annotations

from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch, Mock
import os
import sys

import test_run_sessions as fixtures
import qe_core as core
from budget_gate import BudgetDenied


class ClaudeBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix='qe-claude-budget-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def budget(self, dollars=1):
        return {'ts':time.strftime('%Y-%m-%dT%H:%M:%S'), 'today':time.strftime('%Y-%m-%d'),
                'claude':{'source':'fresh', 'pay':1, 'percent1_usd':dollars, 'pool_remaining_usd':dollars}}

    def relay(self, snapshot, upstream):
        from claude_transport import Relay
        catalog = {'anthropic':{'models':{'fixture-model':{'cost':{'input':1,'output':2},
                                                        'limit':{'context':100, 'output':10}}}}}
        return Relay(self.home, catalog, lambda:snapshot, upstream)

    def test_unknown_budget_never_starts_claude_binary(self) -> None:
        f = fixtures.SessionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        with patch.object(core, 'budget_json', return_value=None), patch.object(core, 'Attempt') as attempt:
            with self.assertRaises(BudgetDenied):
                core.run_claude(str(f.wt), 'claude/sonnet', 'task', f.log)
        attempt.assert_not_called()

    def test_unknown_budget_or_unknown_model_never_reaches_network(self) -> None:
        for snapshot, model in ((None,'fixture-model'), (self.budget(),'unpriced')):
            calls = []
            relay = self.relay(snapshot, lambda *args: calls.append(args))
            with self.assertRaises(BudgetDenied):
                relay.open({'model':model,'max_tokens':10,'messages':[]}, {})
            self.assertEqual(calls, [])

    def test_reserve_before_provider_and_keep_interrupted_outcome(self) -> None:
        def upstream(*args):
            with closing(sqlite3.connect(self.home/'chain/request-budget.db')) as con:
                self.assertEqual(con.execute('select reserved,actual from reservation').fetchall(), [(220,None)])
            raise ConnectionError('provider received request; outcome lost')
        relay = self.relay(self.budget(.00022), upstream)
        with self.assertRaises(ConnectionError):
            relay.open({'model':'fixture-model','max_tokens':10,'messages':[]}, {})
        with self.assertRaises(BudgetDenied):
            relay.open({'model':'fixture-model','max_tokens':10,'messages':[]}, {})

    def test_a_claude_reviewer_spends_from_the_reviewer_pocket_not_the_lanes(self) -> None:
        """B79 (17.09): the Claude path saw the WHOLE run via `run_cap_micro` and wrote spending without a slice.
        Result: judge/helper calls coming through Claude were charged to the LANE pocket,
        and when the lanes were full the judge was denied — exactly what the hard split is meant to prevent."""
        import os
        from run_store import RunStore
        import budget_gate as budget
        rid = RunStore(self.home).accept('b79', 'task')['run_id']
        RunStore(self.home).update(rid, budget_cap_usd=1.0)
        os.environ['CADRE_RUN_ID'] = rid
        self.addCleanup(lambda: os.environ.pop('CADRE_RUN_ID', None))
        self.addCleanup(lambda: os.environ.pop('CADRE_CLAUDE_ROLE', None))
        # the lanes fill their own pockets completely
        ledger = budget.BudgetLedger(self.home/'chain/request-budget.db')
        lane, lane_slice = budget.role_budget(self.home, rid, 'impl')
        ledger.reserve('claude', lane, self.budget(10), {'run_id': rid}, run_cap=lane, slice=lane_slice)
        # a Claude call in the judge role must still pass
        os.environ['CADRE_CLAUDE_ROLE'] = 'quality'
        relay = self.relay(self.budget(10), lambda *args: (_ for _ in ()).throw(ConnectionError('parent akim absent')))
        with self.assertRaises(ConnectionError):
            relay.open({'model':'fixture-model','max_tokens':10,'messages':[]}, {})
        with closing(sqlite3.connect(self.home/'chain/request-budget.db')) as con:
            settled, pending = budget.run_usage(con, rid, 'judge')
        self.assertGreater(pending, 0)        # written to the judge pocket
        with closing(sqlite3.connect(self.home/'chain/request-budget.db')) as con:
            self.assertEqual(budget.run_usage(con, rid, 'lanes')[1], lane)   # the lane pocket did not change

    def test_reservation_uses_the_calibrated_cap_when_the_snapshot_carries_one(self) -> None:
        """B253 (OBSERVATION-6): if the observation has a calibrated cap for the bucket, the limit reserves it (the smaller one) instead of the worst case;
        without calibration the old worst case. The receipt still closes with real usage."""
        import os
        import budget_gate as budget
        os.environ['CADRE_CLAUDE_ROLE'] = 'quality'
        self.addCleanup(lambda: os.environ.pop('CADRE_CLAUDE_ROLE', None))
        snapshot = {**self.budget(10), 'calibrated': {'judge': {'claude': 0.0001}}}
        relay = self.relay(snapshot, lambda *args: (_ for _ in ()).throw(ConnectionError('parent akim absent')))
        with self.assertRaises(ConnectionError):
            relay.open({'model':'fixture-model','max_tokens':10,'messages':[]}, {})
        with closing(sqlite3.connect(self.home/'chain/request-budget.db')) as con:
            reserved = [r[0] for r in con.execute('select reserved from reservation')]
        self.assertEqual(reserved, [100], 'Calibrated cap 0.0001 USD is 100 micro; the worst case of 220 was larger')
        snapshot2 = {**self.budget(10), 'calibrated': {'judge': {'claude': 5.0}}}
        relay = self.relay(snapshot2, lambda *args: (_ for _ in ()).throw(ConnectionError('parent akim absent')))
        with self.assertRaises(ConnectionError):
            relay.open({'model':'fixture-model','max_tokens':10,'messages':[]}, {})
        with closing(sqlite3.connect(self.home/'chain/request-budget.db')) as con:
            last = con.execute('select reserved from reservation order by created desc limit 1').fetchone()[0]
        self.assertLess(last, 5_000_000, 'If calibration exceeds the worst case, keep the smaller worst-case amount')

    def test_anthropic_stream_receipt_preserves_cache_and_reasoning_cost(self) -> None:
        from claude_transport import Receipt
        relay = self.relay(self.budget(), lambda *args: io.BytesIO())
        reservation, pricing, _ = relay.open({'model':'fixture-model','max_tokens':10,'messages':[]}, {})
        parser = Receipt(relay.ledger, reservation, pricing, True)
        events = [
            {'type':'message_start','message':{'id':'msg_fixture','usage':{'input_tokens':2,'cache_creation_input_tokens':3,'cache_read_input_tokens':4,'output_tokens':0}}},
            {'type':'message_delta','usage':{'output_tokens':5}}, {'type':'message_stop'}]
        for event in events:
            raw = ('data: '+json.dumps(event)+'\n\n').encode()
            parser.feed(raw[:7]); parser.feed(raw[7:])
        parser.finish()
        with closing(sqlite3.connect(self.home/'chain/request-budget.db')) as con:
            # B73 (16.09): the receipt now closes with the REAL price (input $1/M), while the reservation is computed with the 2x safe rate ($2/M).
            # Input 2+3+4=9 tokens x $1/M + output 5 x $2/M = 19 µ$ (the old inflated value was 28).
            self.assertEqual(con.execute('select actual from reservation').fetchone()[0], 19)
            self.assertGreater(pricing.maximum, 19)  # the reservation is still at the safe upper bound

    def test_a_one_hour_cache_write_is_settled_at_twice_the_input_rate(self) -> None:
        """17.09 LIVE MEASUREMENT: the real invoice of the plan call was $0.4738, our ledger recorded $0.3193 (32% short).
        Cause: 41204 tokens were written to a ONE-HOUR cache. A one-hour write is TWICE the input price,
        while the catalogue's `cache_write` is the FIVE-MINUTE rate. The API gives the breakdown under `usage.cache_creation`."""
        from claude_transport import Receipt
        relay = self.relay(self.budget(), lambda *args: io.BytesIO())
        reservation, pricing, _ = relay.open({'model':'fixture-model','max_tokens':10,'messages':[]}, {})
        self.assertEqual(pricing.cache_write_1h, pricing.settle_input * 2)
        parser = Receipt(relay.ledger, reservation, pricing, True)
        events = [
            {'type':'message_start','message':{'id':'msg_1h','usage':{
                'input_tokens':0,'cache_creation_input_tokens':10,'cache_read_input_tokens':0,'output_tokens':0,
                'cache_creation':{'ephemeral_1h_input_tokens':10,'ephemeral_5m_input_tokens':0}}}},
            {'type':'message_delta','usage':{'output_tokens':0}}, {'type':'message_stop'}]
        for event in events:
            raw = ('data: '+json.dumps(event)+'\n\n').encode()
            parser.feed(raw)
        parser.finish()
        with closing(sqlite3.connect(self.home/'chain/request-budget.db')) as con:
            # fixture: input $1/M -> one-hour write $2/M; 10 tokens x $2/M = 20 µ$ (it would be 10 at the five-minute rate)
            self.assertEqual(con.execute('select actual from reservation').fetchone()[0], 20)

    def test_without_a_breakdown_the_cache_write_keeps_the_old_five_minute_rate(self) -> None:
        from claude_transport import Receipt
        relay = self.relay(self.budget(), lambda *args: io.BytesIO())
        reservation, pricing, _ = relay.open({'model':'fixture-model','max_tokens':10,'messages':[]}, {})
        parser = Receipt(relay.ledger, reservation, pricing, True)
        for event in [{'type':'message_start','message':{'id':'msg_5m','usage':{
                           'input_tokens':0,'cache_creation_input_tokens':10,'cache_read_input_tokens':0,'output_tokens':0}}},
                      {'type':'message_delta','usage':{'output_tokens':0}}, {'type':'message_stop'}]:
            parser.feed(('data: '+json.dumps(event)+'\n\n').encode())
        parser.finish()
        with closing(sqlite3.connect(self.home/'chain/request-budget.db')) as con:
            self.assertEqual(con.execute('select actual from reservation').fetchone()[0], 10)

    def test_server_side_paid_tools_and_fast_mode_require_a_price_bound(self) -> None:
        relay = self.relay(self.budget(), lambda *args: self.fail('network reached'))
        for extra in ({'speed':'fast'}, {'tools':[{'type':'web_search_20250305','name':'web_search'}]}):
            with self.assertRaises(BudgetDenied):
                relay.open({'model':'fixture-model','max_tokens':10,'messages':[],**extra}, {})

    def test_http_boundary_rejects_unknown_budget_without_exposing_request_headers(self) -> None:
        from claude_transport import handler_for
        relay = self.relay(None, lambda *args: self.fail('network reached'))
        output = io.BytesIO()
        raw = json.dumps({'model':'fixture-model','max_tokens':10,'messages':[]}).encode()
        request = b'POST /capability/v1/messages HTTP/1.1\r\nHost: localhost\r\nAuthorization: fixture-private\r\nContent-Length: '+str(len(raw)).encode()+b'\r\n\r\n'+raw
        connection = Mock()
        connection.makefile.return_value = io.BytesIO(request)
        connection.sendall.side_effect = output.write
        handler_for(relay, 'capability')(connection, ('127.0.0.1',1000), Mock())
        self.assertIn(b'403', output.getvalue())
        self.assertIn(b'CADRE_BUDGET_DENIED', output.getvalue())
        self.assertNotIn(b'fixture-private', output.getvalue())

    def test_http_boundary_logs_the_denial_reason_for_the_lane_log(self) -> None:
        """B244 (26.09 live EXTRA1 writer): the 6th call got 403 CADRE_BUDGET_DENIED but the REASON was written nowhere (pocket, share,
        stale observation?) — RCA was impossible. The limit writes the reason as one line to stderr (the engine log); request headers still do not leak."""
        import contextlib
        from claude_transport import handler_for
        relay = self.relay(None, lambda *args: self.fail('network reached'))
        output = io.BytesIO()
        raw = json.dumps({'model':'fixture-model','max_tokens':10,'messages':[]}).encode()
        request = b'POST /capability/v1/messages HTTP/1.1\r\nHost: localhost\r\nAuthorization: fixture-private\r\nContent-Length: '+str(len(raw)).encode()+b'\r\n\r\n'+raw
        connection = Mock()
        connection.makefile.return_value = io.BytesIO(request)
        connection.sendall.side_effect = output.write
        with contextlib.redirect_stderr(io.StringIO()) as err:
            handler_for(relay, 'capability')(connection, ('127.0.0.1',1000), Mock())
        self.assertIn(b'403', output.getvalue())
        line = [l for l in err.getvalue().splitlines() if l.startswith('CADRE_BUDGET_DENIED:')]
        self.assertEqual(len(line), 1, err.getvalue())
        self.assertIn('budget', line[0])
        self.assertNotIn('fixture-private', err.getvalue())

    def test_wrapper_passes_owned_endpoint_and_closes_listener_with_child(self) -> None:
        import claude_transport
        catalog = self.home/'catalog.json'
        catalog.write_text('{}')
        listener = Mock(server_port=12345)
        child = [sys.executable, '-c', 'import os;assert os.environ["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:12345/")']
        env = {'CADRE_HOME':str(self.home),'CADRE_ENGINE_ROOT':str(self.home), 'CADRE_MODEL_CATALOG':str(catalog),
               'CADRE_OWNER_FDS':'', 'ANTHROPIC_BASE_URL':'https://api.anthropic.com',
               'CLAUDE_CODE_USE_BEDROCK':'0','CLAUDE_CODE_USE_VERTEX':'0','CLAUDE_CODE_USE_FOUNDRY':'0'}
        with patch.dict(os.environ, env), patch.object(sys, 'argv', ['wrapper',*child]), \
             patch.object(claude_transport, 'server', return_value=listener), patch.object(claude_transport.signal, 'signal'):
            self.assertEqual(claude_transport.main(), 0)
        listener.shutdown.assert_called_once()
        listener.server_close.assert_called_once()

    def test_http_success_preserves_beta_query_and_settles_only_after_delivery(self) -> None:
        from claude_transport import handler_for
        calls = []
        class Response(io.BytesIO):
            status = 200
            headers = {'Content-Type':'application/json'}
        def upstream(body, headers, query):
            calls.append((body,headers,query))
            return Response(json.dumps({'type':'message','id':'msg','usage':{'input_tokens':2,'output_tokens':1}}).encode())
        relay = self.relay(self.budget(), upstream)
        output = io.BytesIO()
        body = json.dumps({'model':'fixture-model','max_tokens':10,'messages':[]}).encode()
        request = b'POST /capability/v1/messages?beta=true HTTP/1.1\r\nHost: localhost\r\nAuthorization: fixture-private\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body
        connection = Mock()
        connection.makefile.return_value = io.BytesIO(request)
        connection.sendall.side_effect = output.write
        handler_for(relay, 'capability')(connection, ('127.0.0.1',1000), Mock())
        self.assertIn(b'200', output.getvalue())
        self.assertNotIn(b'fixture-private', output.getvalue())
        self.assertEqual(calls[0][0]['service_tier'], 'standard_only')
        self.assertEqual(calls[0][2], 'beta=true')
        self.assertNotIn('Host', calls[0][1])
        with closing(sqlite3.connect(self.home/'chain/request-budget.db')) as con:
            self.assertEqual(con.execute('select actual from reservation').fetchone()[0], 4)  # B73: real price (old 2x inflated: 6)

    def test_an_upstream_5xx_closes_the_reservation_like_a_4xx(self) -> None:
        """B110 (18.09): 4xx was closed at zero but 5xx (500/502/503/529-overloaded) left the FULL reservation
        pending — no body was produced, no charge; the pending amount locked the Claude share for nothing."""
        from claude_transport import handler_for

        class Response(io.BytesIO):
            status = 529
            headers = {'Content-Type': 'application/json'}

        relay = self.relay(self.budget(), lambda *a: Response(b'{"type":"error","error":{"type":"overloaded_error"}}'))
        output = io.BytesIO()
        body = json.dumps({'model': 'fixture-model', 'max_tokens': 10, 'messages': []}).encode()
        request = (b'POST /capability/v1/messages HTTP/1.1\r\nHost: localhost\r\nAuthorization: fixture-private\r\n'
                   b'Content-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
        connection = Mock()
        connection.makefile.return_value = io.BytesIO(request)
        connection.sendall.side_effect = output.write
        handler_for(relay, 'capability')(connection, ('127.0.0.1', 1000), Mock())
        with closing(sqlite3.connect(self.home / 'chain/request-budget.db')) as con:
            real, slip = con.execute('select actual, receipt from reservation').fetchone()
        self.assertEqual(real, 0, 'A 5xx is not billed; settle the reservation at zero')
        self.assertIn('http-529', slip)

    def test_partial_claude_stream_keeps_the_full_reservation(self) -> None:
        from claude_transport import Receipt
        relay = self.relay(self.budget(), lambda *args: None)
        ident, pricing, _ = relay.open({'model':'fixture-model','max_tokens':10,'messages':[]}, {})
        receipt = Receipt(relay.ledger, ident, pricing, True)
        receipt.feed(b'data: {"type":"message_start","message":{"id":"partial","usage":{"input_tokens":2,"output_tokens":0}}}\n\n')
        receipt.finish()
        with closing(sqlite3.connect(self.home/'chain/request-budget.db')) as con:
            self.assertIsNone(con.execute('select actual from reservation').fetchone()[0])


if __name__ == '__main__':
    unittest.main()


class PreCheckRecordTests(unittest.TestCase):
    """B114-3 (19.09 audit): when `run_claude`'s budget pre-check denied, only the engine log
    was written. Since the gate was never reached `note_denial` was not called either, and the denial did not land in `run.json denials[]`.
    Result: the morning summary (`morning_summary.py`) could not read "was claude denied today" from the
    record — the silent closure stayed only in a log line."""

    def test_a_precheck_denial_is_written_to_the_run_record(self):
        f = fixtures.SessionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        from run_store import RunStore
        store = RunStore(f.home)
        previous = list(store.read(f.rid).get('denials') or [])
        with patch.dict(os.environ, {'CADRE_RUN_ID': f.rid, 'CADRE_HOME': str(f.home)}, clear=False), \
             patch.object(core, 'QE', f.home), \
             patch.object(core, 'budget_json', return_value=None):
            with self.assertRaises(BudgetDenied):
                core.run_claude(str(f.wt), 'claude/sonnet', 'task', f.log, role='quality')
        next = list(store.read(f.rid).get('denials') or [])
        self.assertGreater(len(next), len(previous), 'The on-check denial was not recorded in run.json denials[]')
        self.assertEqual(next[-1].get('provider'), 'claude')
        self.assertTrue(next[-1].get('reason'), 'A denial row must include a rationale')
        # B249 (26.09 live EXTRA1 writer): 'Claude allowance unavailable' alone is not enough for the RCA — stale observation, ban,
        # or pocket? The sub-reason is carried inside the record.
        self.assertRegex(next[-1]['reason'], r'^Claude allowance unavailable: .+', next[-1]['reason'])
