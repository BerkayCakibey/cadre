"""RCA code28/29: a provider outage (OpenRouter FLEX provider_unavailable) goes to a recorded wait; the reserve is kept,
no uncertain reserve accumulates on a closed route, the account summary keeps the receipted/estimated/pending classes separate."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))

SOL = 'openrouter/openai/gpt-6.1-sol'
OUTAGE_REJECTION = {'providers': ['openrouter'], 'refusals': [
    {'provider': 'openrouter', 'reason': f'provider unavailable: {SOL} until 1792406700', 'slice': None, 'scope': 'provider'}]}
BUDGET_REJECTION = {'providers': ['openrouter'], 'refusals': [
    {'provider': 'openrouter', 'reason': 'run budget exhausted: reservation $0.22 > remaining $0.00', 'slice': 'helper', 'scope': 'role'}]}


class ProviderWaitClassTests(unittest.TestCase):
    def test_only_a_bound_stopped_process_scope_can_pause_a_provider_failed_review(self):
        import tempfile
        from types import SimpleNamespace

        import qe_core as core
        from budget_gate import BudgetLedger, ProviderWait
        from run_store import RunStore, atomic_json
        with tempfile.TemporaryDirectory(prefix='provider-pause-') as td, patch.dict(
                os.environ, {'CADRE_HOME': td, 'CADRE_RUN_CAP_USD': '3', 'CADRE_NO_NET': '1'}):
            store = RunStore(Path(td))
            ledger = BudgetLedger(store.home / 'chain/request-budget.db')
            for variant in ('missing', 'unobserved', 'foreign', 'open-scope', 'verified'):
                with self.subTest(variant=variant):
                    rid = store.accept('lab', 'ROUND: review\nSource review')['run_id']
                    task, aid = 'plan-review-1-abcdef01', 'a' * 32
                    directory = store.task_dir(rid, task) / 'attempts' / aid
                    directory.mkdir(parents=True)
                    identity = {'pid': 123, 'started': 'fixture:123', 'pgid': 123}
                    receipt = {'run_id': rid, 'task_id': task, 'attempt_id': aid,
                               'process_identity': identity, 'stop_observed': identity,
                               'scope_digest': 'b' * 64, 'scope_stopped': 'b' * 64}
                    if variant == 'unobserved': receipt.pop('stop_observed')
                    if variant == 'foreign': receipt['run_id'] = 'other-run'
                    if variant == 'open-scope': receipt.pop('scope_stopped')
                    if variant != 'missing': atomic_json(directory / 'process.json', receipt)
                    process = Mock(spec=core.RecordedProcess)
                    process.attempt = SimpleNamespace(run_id=rid, task_id=task, directory=directory, store=store,
                                                      path=directory / 'attempt.json',
                                                      data={'model': SOL, 'attempt_id': aid})
                    process.output.return_value = ''
                    with patch.dict(os.environ, {'CADRE_RUN_ID': rid, 'CADRE_TASK_ID': task}), \
                         patch.object(core, 'QE', store.home), patch.object(core, 'run_opencode', return_value=process), \
                         patch.object(core, 'wait_process', return_value=75), \
                         patch.object(core, 'budget_denial_receipt', return_value=OUTAGE_REJECTION), \
                         patch.object(core, 'retire_owned_tools', return_value=variant == 'verified') as stopped, \
                         self.assertRaises(ProviderWait):
                        core.capture_opencode('fixture', td, ['-m', SOL, 'REVIEW PLAN ALIGNMENT WITH THE REQUEST\n{}'], 300, role='review')
                    if variant in ('open-scope', 'verified'):
                        stopped.assert_called_once_with(store, process.attempt.path, process.attempt.data)
                    else:
                        stopped.assert_not_called()
                    with ledger.transaction() as con:
                        pause = con.execute('select paused_at from review_pause where run_id=?', (rid,)).fetchone()
                    self.assertEqual(bool(pause and pause[0] is not None), variant == 'verified')

    def test_outage_rejection_gives_the_route_and_its_end_budget_rejection_does_not(self):
        from budget_gate import provider_wait_rejection
        self.assertEqual(provider_wait_rejection(OUTAGE_REJECTION), (SOL, 1792406700.0))
        self.assertIsNone(provider_wait_rejection(BUDGET_REJECTION))
        self.assertIsNone(provider_wait_rejection(None))

    def test_outage_rejection_does_not_ban_the_provider_in_the_task_budget_rejection_does(self):
        import qe_core as core

        class Forbidden:
            def __init__(self):
                self.obstacle = []

            def block(self, provider, reason, snapshot, slice=None):
                self.obstacle.append((provider, reason))

        outage, budget = Forbidden(), Forbidden()
        core.apply_budget_denial(outage, OUTAGE_REJECTION, {})
        core.apply_budget_denial(budget, {'providers': ['openrouter'], 'refusals': [
            {'provider': 'openrouter', 'reason': 'provider share exhausted', 'slice': None, 'scope': 'provider'}]}, {})
        self.assertEqual(outage.obstacle, [], 'Only the affected model route closes in the breaker; the whole provider remains available')
        self.assertEqual(budget.obstacle, [('openrouter', 'budget')])

    def test_final_judge_outage_waits_for_the_breaker_end_the_budget_rejection_keeps_the_midnight_rule(self):
        from run_store import capacity_wait
        now_ts = 1792406000.0
        outage = {'review': {'status': 'ERROR', 'reason': f'ProviderWait: provider-wait: {SOL} until 1792406700'}}
        self.assertEqual(capacity_wait(outage, now_ts), 1792406700.0)
        budget = {'review': {'status': 'ERROR', 'reason': 'BudgetDenied: run budget exhausted'}}
        self.assertGreater(capacity_wait(budget, now_ts), now_ts + 3600)
        self.assertIsNone(capacity_wait({'review': {'status': 'FAIL', 'reason': 'REVISE'}}, now_ts))


class OpenRouterEndpointOutageTests(unittest.TestCase):
    def setUp(self):
        from budget_gate import BudgetLedger
        from test_budget_period import snapshot
        from test_model_breaker import BreakerBase
        self.fixture = BreakerBase()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.home = self.fixture.home
        self.ledger = BudgetLedger(self.home / 'chain/request-budget.db')
        self.snapshot = {**snapshot(5), 'or': {'db': 'ok', 'pay': 5, 'available': 5}}
        self.model = 'deepseek/deepseek-v4-pro'
        self.full_model = 'openrouter/' + self.model
        self.endpoints = ['streamlake/fp8', 'deepinfra/fp8']
        (self.home / 'chain/openrouter-price.json').write_text(json.dumps({'models': {
            self.model: {'providers': self.endpoints}}}))

    def failed_request(self, endpoints, *, code=502, kind='provider_unavailable'):
        from budget_gate import provider_error_save
        ident = self.ledger.reserve('openrouter', 100, self.snapshot,
                                    {'model': self.model, 'endpoint_selection': {'order': endpoints}})
        provider_error_save(self.ledger, {'id': ident, 'code': code, 'round': kind,
                                            'message': 'provider temporarily unavailable' if code != 401 else '401 Unauthorized'})
        return ident

    def test_a_known_endpoint_outage_does_not_close_a_healthy_endpoint(self):
        import model_router as router
        from budget_gate import closed_path
        ident = self.failed_request(self.endpoints[:1])
        self.assertFalse(router.in_cooldown('openrouter', model=self.full_model))
        self.assertIsNotNone(closed_path('openrouter', self.model, self.endpoints[:1]))
        self.assertIsNone(closed_path('openrouter', self.model, self.endpoints[1:]))
        self.failed_request(self.endpoints[1:])
        self.assertTrue(router.in_cooldown('openrouter', model=self.full_model))
        self.assertFalse(router.in_cooldown('openrouter'), 'two endpoints of one model are not two failed models')
        with self.ledger.transaction() as con:
            self.assertEqual(con.execute('select reserved,actual from reservation where id=?', (ident,)).fetchone(), (100, None))
        self.assertFalse(router.in_cooldown('openrouter', model=self.full_model, now_ts=time.time() + 1801))

    def test_an_unknown_endpoint_or_an_account_error_retains_the_wider_breaker(self):
        import model_router as router
        self.failed_request(self.endpoints)
        self.assertTrue(router.in_cooldown('openrouter', model=self.full_model), 'an ambiguous outage cannot be assigned to one endpoint')
        router.status_write({})
        self.failed_request(self.endpoints[:1], code=401)
        self.assertTrue(router.in_cooldown('openrouter'), 'an account error still applies to every endpoint')

    def test_the_reservation_boundary_checks_the_endpoints_actually_selected(self):
        import io

        import budget_gate as budget
        self.failed_request(self.endpoints[:1])
        env = {'CADRE_HOME': str(self.home), 'CADRE_ENGINE_ROOT': str(LIB.parent), 'CADRE_RUN_ID': ''}
        for endpoints, expected in ((self.endpoints[:1], 75), (self.endpoints, 75), (self.endpoints[1:], 0)):
            with self.subTest(endpoints=endpoints), patch.dict(os.environ, env), \
                    patch.object(budget, 'observed_budget', return_value=self.snapshot) as observation, \
                    patch.object(sys, 'stdin', io.StringIO(json.dumps({'action': 'reserve', 'provider': 'openrouter',
                        'maximum': 100, 'metadata': {'model': self.model, 'endpoint_selection': {'order': endpoints}}}))), \
                    patch.object(sys, 'stdout', io.StringIO()), patch.object(sys, 'stderr', io.StringIO()):
                self.assertEqual(budget.main(), expected)
                if expected == 75:
                    observation.assert_not_called()


FAKE_PROVIDER = r'''
import { readFileSync, appendFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
const [root, catalogPath, model, record] = process.argv.slice(2);
const catalog = JSON.parse(readFileSync(catalogPath, 'utf8'));
const [provider, ...part] = model.split('/');
const id = part.join('/');
const error = { code: 502, message: 'Flex processing is temporarily unavailable. Please try again later or use standard processing.',
  metadata: { error_type: 'provider_unavailable' } };
let call = 0;
const config = { provider: { [provider]: { options: { fetch: async () => {
  call += 1;
  appendFileSync(record, JSON.stringify({ call, ts: Date.now() / 1000 }) + '\n');
  return new Response(`data: ${JSON.stringify({ id: `gen-fake-${call}`, error: error })}\n\n`, { headers: { 'content-type': 'text/event-stream' } });
} } } } };
const hooks = [await (await import(pathToFileURL(join(root, 'lib', 'cadre-plugin.mjs')).href)).default()];
await hooks[0].config(config);
const m = catalog[provider].models[id];
for (let attempt = 0; attempt < 4; attempt++) {
  const input = { sessionID: 'ses_fake_outage', agent: 'build', model: { providerID: provider, id, api: { id }, limit: m.limit, cost: m.cost } };
  const params = { maxOutputTokens: 32768 };
  for (const k of hooks) await k['chat.params']?.(input, params);
  const headers = { headers: {} };
  for (const k of hooks) await k['chat.headers']?.(input, headers);
  try {
    const res = await config.provider[provider].options.fetch('https://fixture.invalid/chat/completions', { method: 'POST',
      headers: headers.headers, body: JSON.stringify({ model: id, max_tokens: params.maxOutputTokens, stream: true,
      messages: [{ role: 'user', content: 'plan review' }] }) });
    await res.text();
  } catch (error) {
    console.log(JSON.stringify({ type: 'error', error: { message: String(error.message) } }));
    process.exit(1);
  }
}
process.exit(1);
'''


class Code29SequenceTests(unittest.TestCase):
    def test_outage_repeat_reserve_correct_wait_estimate_closing_and_three_class_summary(self):
        from test_end_to_end import E2EFixture, FAKE_OPENCODE
        fixture = E2EFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        script = fixture.home.parent / 'provider-fake.mjs'
        script.write_text(FAKE_PROVIDER)
        calls = fixture.home / 'provider-calls.jsonl'
        gate = "if prompt.startswith('REVIEW PLAN ALIGNMENT WITH THE REQUEST'):"
        extra = (f"if role == 'plan-review' and os.environ.get('E2E_PROVIDER_OUTAGE') == '1':\n"
              f"    import subprocess\n"
              f"    sys.exit(subprocess.run(['node', {str(script)!r}, os.environ['CADRE_ENGINE_ROOT'], os.environ['CADRE_MODEL_CATALOG'],"
              f" args[args.index('-m') + 1], {str(calls)!r}]).returncode)\n")
        self.assertEqual(FAKE_OPENCODE.count(gate), 1)
        fixture.fake.write_text(FAKE_OPENCODE.replace(gate, extra + gate))
        (fixture.home / 'chain/openrouter-price.json').write_text(json.dumps({'version': 2, 'ts': time.time(), 'models': {
            'openai/gpt-6.1-sol': {'input': 1, 'output': 5, 'providers': ['openai'], 'fp8_cache': True}}}))
        env = {**fixture.env, 'CADRE_CHAIN_REVIEW': SOL, 'E2E_PROVIDER_OUTAGE': '1'}
        command = [sys.executable, str(fixture.root / 'lib/run_store.py')]

        def cli(*args, data=None, timeout=60):
            return subprocess.run([*command, *args], input=json.dumps(data) if data is not None else None,
                                  text=True, capture_output=True, env=env, timeout=timeout)

        raw = dict(submission_id='outage', project='e2e', target_path=str(fixture.repo), target_kind='existing', input_mode='goal',
                   prompt=fixture.PROMPT, output='Working a.py and b.py', acceptance='Both files compile and contain COMPLETE',
                   preference='economic', difficulty_override='easy', duration_minutes='60', budget_usd='3', backlog_path='', source_token='')
        submitted = cli('request', data=raw)
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        rid = json.loads(submitted.stdout)['run_id']
        proposal = cli('request-proposal', rid)
        self.assertEqual(proposal.returncode, 0, proposal.stderr)
        proposal = json.loads(proposal.stdout)
        self.assertIn(SOL, proposal['action']['models']['review'])
        first = cli('request-approve', rid, data=proposal)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = cli('request-approve', rid, json.loads(first.stdout)['confirmation'], data=proposal)
        self.assertEqual(second.returncode, 0, second.stderr)
        planned = cli('request-plan', rid, timeout=120)
        self.assertEqual(planned.returncode, 78, (planned.stdout[-800:], planned.stderr[-800:], fixture.store.read(rid).get('reason')))

        record = fixture.store.read(rid)
        self.assertTrue(record['reason'].startswith(f'provider-wait: {SOL}'), record['reason'])
        self.assertEqual(record['planning']['status'], 'provider-wait')
        first_end = float(json.loads((fixture.home / 'chain/provider-status.json').read_text())[SOL]['endpoints']['openai']['cooldown_until'])
        active = float((record.get('request_clock') or {}).get('active_sec') or 0)
        cli('run', rid, timeout=120)
        record = fixture.store.read(rid)
        provide = [json.loads(s) for s in calls.read_text().splitlines()]
        self.assertEqual(len(provide), 1, 'An OpenCode retry must not send a second request down a closed route')
        status = json.loads((fixture.home / 'chain/provider-status.json').read_text())
        end = float(status[SOL]['endpoints']['openai']['cooldown_until'])
        self.assertGreater(end, time.time())
        self.assertFalse(float(status[SOL].get('cooldown_until') or 0) > time.time(), 'A known FLEX outage does not close the whole GPT model')
        self.assertFalse(float((status.get('openrouter') or {}).get('cooldown_until') or 0) > time.time(), 'Do not close the whole provider')
        self.assertEqual(record['state'], 'retryable', (record.get('reason'), record.get('planning'), [(r.get('status'), r.get('reason')) for r in record.get('plan_reviews') or []], record.get('retry_after')))
        self.assertTrue(record['reason'].startswith(f'provider-wait: {SOL}'), record['reason'])
        self.assertNotIn('BudgetDenied', record['reason'])
        self.assertAlmostEqual(float(record['retry_after']), end, delta=1)
        self.assertEqual(record['plan_reviews'][-1]['status'], 'ERROR')
        self.assertEqual(record['planning']['status'], 'provider-wait', 'An unavailable review is not a plan rejection')

        import sqlite3
        ledger = fixture.home / 'chain/request-budget.db'
        from contextlib import closing
        with closing(sqlite3.connect(ledger.resolve().as_uri() + '?mode=ro', uri=True)) as con:
            lines = [(r, a, rc, json.loads(m)) for r, a, rc, m in con.execute(
                "select reserved,actual,receipt,metadata from reservation where json_extract(metadata,'$.run_id')=?", (rid,))]
            clock = con.execute('select started,deadline,calls,tools from review_control where run_id=? and phase=?',
                                (rid, 'plan-review')).fetchone()
            pause = con.execute('select paused_at from review_pause where run_id=? and phase=?',
                                (rid, 'plan-review')).fetchone()
        self.assertIsNotNone(pause, 'A provider wait pauses both the main job clock and the review phase')
        self.assertIsNotNone(pause[0])
        self.assertEqual(clock[2:], (1, 0), 'An outage does not reset model-request or tool counters')
        from budget_gate import BudgetLedger
        future = end + 60
        with patch.object(time, 'time', return_value=future), closing(sqlite3.connect(
                ledger.resolve().as_uri() + '?mode=ro', uri=True)) as con:
            after_wait = BudgetLedger(ledger).review_context(con, rid, 'plan-review-2-abcdef01', 'review')
        self.assertAlmostEqual(after_wait['deadline'] - future, clock[1] - pause[0], places=3)
        sol = [s for s in lines if s[3].get('model') == 'openai/gpt-6.1-sol']
        self.assertEqual(len(sol), 1, 'Uncertain reservations must not accumulate on a closed route')
        self.assertIsNone(sol[0][1], 'A 502 does not prove zero cost; preserve the reservation')
        self.assertEqual(sol[0][3]['provider_error']['round'], 'provider_unavailable')
        limit = max(sol[0][0], sol[0][3].get('worst') or 0, sol[0][3].get('verified_bound_micro') or 0)
        self.assertEqual(end, first_end, 'Restarting does not renew the breaker deadline')
        self.assertGreaterEqual(float(record['request_clock']['active_sec']), active, 'Restarting does not reset elapsed time')
        self.assertEqual(int(record.get('retry_count') or 0), 0, 'A recorded wait does not consume an attempt')

        import budget_gate
        import project_mode
        with patch.dict(os.environ, {'CADRE_HOME': str(fixture.home)}), \
             patch.object(budget_gate.time, 'time', return_value=time.time() + 31 * 60):
            closure = budget_gate.phantom_close(fixture.home, age_min=30)
        self.assertIn(rid, closure['runs'])
        m = project_mode.project_ref_cost(fixture.home, [rid])
        self.assertEqual(m['estimated_usd'], round(limit / 1e6, 6), 'A phantom closure uses the worst bound and stays ESTIMATED')
        self.assertEqual(m['pending_usd'], 0.0)
        self.assertEqual(m['liability_usd'], round(m['measured_usd'] + m['estimated_usd'], 6))
        summary = project_mode.account_summary(m, 3.0)
        self.assertEqual(summary['remaining_usd'], round(3.0 - m['liability_usd'], 6))
        self.assertIn('estimated $', project_mode._spend_line({'expense': m}))


if __name__ == '__main__':
    unittest.main()
