"""4c-17: code model fallback + a model breaker at error level. The error is written to the router state at (model, route) level (it does not
enter the task bans); that route closes first, and if the model fails on all routes the next model in the chain is used (through the hand-off counter),
the provider closes only when >=2 of its models fail or on an account signal; share/window exhaustion is not a failure."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import qe_core as core  # noqa: E402
import model_router as router  # noqa: E402
from model_catalog import canonical_model  # noqa: E402
from run_store import RunStore  # noqa: E402

DS_GO, DS_OR = 'opencode-go/deepseek-v4-pro', 'openrouter/deepseek/deepseek-v4-pro'
GLM_GO, QWEN_GO, QWEN_OR = 'opencode-go/glm-5.3', 'opencode-go/qwen3.8-max', 'openrouter/qwen/qwen3.8-max-0902'


class BreakerBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-breaker-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'chain').mkdir()
        for ctx in (patch.object(router, 'STATUS', self.home / 'chain/provider-status.json'),
                    patch.object(router, 'RECORD', self.home / 'chain/model_router.jsonl'),
                    patch.object(router, 'QE', self.home)):
            ctx.__enter__()
            self.addCleanup(ctx.__exit__, None, None, None)

    def open(self, *paths):
        """Budget/account filter: only the given routes are open (the breaker is the chain's own filter, not faked separately)."""
        open = set(paths)
        return patch.multiple(router, has_budget=lambda provider_key, m, *a, **k: (m in open, 'test'), ladder=lambda *a, **k: 'active')


class RouterBreakerTests(BreakerBase):
    def test_a_route_failure_closes_only_that_route(self):
        router.failure('opencode-go', 'requires Global regions', allowed_fails=1, model=DS_GO)
        with self.open(DS_GO, DS_OR, GLM_GO):
            z, reason = router.chain('impl', b={'go': {}}, d=None)
        self.assertEqual(z, [DS_OR, GLM_GO], 'The OR route for the same model and Go GLM remain available; Go is not closed')
        self.assertTrue(any('model cooldown' in n for n in reason), reason)
        self.assertFalse(router.in_cooldown('opencode-go'))
        self.assertTrue(router.in_cooldown('opencode-go', model=DS_GO))

    def test_provider_closes_if_two_models_of_the_same_provider_fall(self):
        router.failure('opencode-go', 'requires Global regions', allowed_fails=1, model=DS_GO)
        self.assertFalse(router.in_cooldown('opencode-go'))
        router.failure('opencode-go', '500 internal', allowed_fails=1, model=GLM_GO)
        self.assertTrue(router.in_cooldown('opencode-go'), 'Two model failures close Go')
        with self.open(DS_GO, DS_OR, GLM_GO, QWEN_GO):
            self.assertEqual(router.chain('impl', b={'go': {}})[0], [DS_OR])

    def test_account_signal_closes_the_provider_immediately(self):
        for error in ('402 Payment Required', 'insufficient balance', 'monthly spend limit reached', '401 Unauthorized'):
            with self.subTest(error=error):
                router.status_write({})
                router.failure('openrouter', error, model=DS_OR)
                self.assertTrue(router.in_cooldown('openrouter'), error)

    def test_success_resets_the_model_counter_and_the_key_is_the_full_id(self):
        router.failure('opencode-go', 'timeout', model=DS_GO)
        router.failure('opencode-go', 'timeout', model=DS_GO)
        d = router.status_read()
        self.assertEqual(d[DS_GO]['error'], 2)
        self.assertNotIn('opencode-go', d, 'A model failure does not increment the provider counter')
        router.success('opencode-go', model=DS_GO)
        self.assertEqual(router.status_read()[DS_GO]['error'], 0)
        self.assertEqual(router.model_key('claude', 'claude/opus#high'), 'claude/opus')
        self.assertEqual(router.model_key('openrouter', 'deepseek/deepseek-v4-pro'), DS_OR)

    def test_code_chain_ds_glm_go_first_within_each_model_qwen_only_judge(self):
        with self.open(*router.CODE_CHAIN):
            z = router.chain('impl', b={'go': {}})[0]
        self.assertEqual(z, [DS_GO, DS_OR, GLM_GO, 'openrouter/z-ai/glm-5.3'])
        for role in ('impl', 'impl-light', 'merge', 'recovery'):
            self.assertEqual(router.ROLES[role], router.CODE_CHAIN)
        self.assertFalse([m for m in router.CODE_CHAIN if 'qwen' in m], 'Qwen does not write code and remains an independent judge')
        self.assertTrue({QWEN_GO, QWEN_OR} <= set(router.ROLES['review']))

    def test_qwen_or_id_is_the_same_model_under_an_alias(self):
        self.assertEqual(canonical_model(QWEN_OR), canonical_model(QWEN_GO))
        with self.open(QWEN_GO, QWEN_OR):
            self.assertEqual(router.same_model_paths(QWEN_GO, role='review', b={'go': {}}), [QWEN_OR])

    def test_alternatives_drop_the_falling_route_not_the_provider(self):
        with self.open(DS_GO, DS_OR, GLM_GO, 'openrouter/z-ai/glm-5.3'):
            self.assertEqual(router.alternatives('impl', DS_GO, 3, b={'go': {}}), [DS_OR, GLM_GO, 'openrouter/z-ai/glm-5.3'])

    def test_zen_left_only_when_or_left_is_cooling_is_a_candidate(self):
        with self.open(router.OR_SOL, router.ZEN_SOL), patch.dict(router.ROLES, {'plan': [router.OR_SOL, router.ZEN_SOL]}):
            self.assertEqual(router.chain('plan', b={'go': {}})[0], [router.OR_SOL])
            router.failure('openrouter', 'upstream error', allowed_fails=1, model=router.OR_SOL)
            self.assertEqual(router.chain('plan', b={'go': {}})[0], [router.ZEN_SOL])

    def test_cli_takes_the_provider_model_id(self):
        env = {**os.environ, 'CADRE_HOME': str(self.home), 'CADRE_ROUTER_STATUS': str(router.STATUS), 'CADRE_ROUTER_RECORD': str(router.RECORD)}
        subprocess.run([sys.executable, str(LIB / 'model_router.py'), 'failure', DS_OR, 'plan empty output'], env=env, check=True,
                       capture_output=True, timeout=30)
        self.assertEqual(json.loads(router.STATUS.read_text())[DS_OR]['error'], 1)


class HandoverTests(BreakerBase):
    def setUp(self):
        super().setUp()
        self.store = RunStore(self.home)
        self.rid = self.store.accept('a', 'task')['run_id']
        for ctx in (patch.object(core, 'QE', self.home), patch.object(core, 'ROUTER', router),
                    patch.dict(os.environ, {'CADRE_RUN_ID': self.rid, 'CADRE_TASK_ID': 'g1'})):
            ctx.__enter__()
            self.addCleanup(ctx.__exit__, None, None, None)
        self.log = self.home / 'runner.log'

    def task(self):
        return self.store.read(self.rid)['tasks']['g1']

    def failures(self):
        return {k: v for k, v in router.status_read().items() if not k.startswith('_')}   # '_order' is the hysteresis memory

    def test_same_model_route_first_is_not_a_handover(self):
        router.failure('opencode-go', 'requires Global regions', allowed_fails=1, model=DS_GO)
        with self.open(DS_GO, DS_OR, GLM_GO):
            self.assertEqual(core.path_hand_over(DS_GO, self.log, 'error'), DS_OR)
        last = self.task()['path_handovers'][-1]
        self.assertEqual((last['round'], last['counted'], self.task()['path_pending']), ('path', False, ''))
        self.assertEqual(core.handover_counter(), (0, 0))

    def test_no_handover_to_a_route_that_cannot_afford_one_call_go_waits(self):
        """C05 live (08.10): with $0.14 on OR, when Go closed the session was handed to the OR route and the first call was denied; when Go opened again
        the lane stayed bound to OR and failed. A route without enough breathing room cannot be a hand-off target; the waiting route is Go."""
        with self.open(DS_GO, DS_OR, GLM_GO), patch.object(core, 'budget_json', return_value={'or': {'db': 'ok'}}), \
             patch.object(router, 'writer_breath', side_effect=lambda m, b: (0.13, 0.4) if m.startswith('openrouter/') else None):
            self.assertEqual(core.path_hand_over(DS_GO, self.log, 'Go window denial', model_handover=False), '')
        self.assertEqual(self.task()['path_pending'], DS_GO)
        with self.open(DS_GO, DS_OR, GLM_GO), patch.object(core, 'budget_json', return_value={'or': {'db': 'ok'}}), \
             patch.object(router, 'writer_breath', return_value=None):
            self.assertEqual(core.path_hand_over(DS_GO, self.log, 'Go window denial', model_handover=False), DS_OR)

    def test_when_the_go_window_reopens_the_waiting_route_continues_with_the_same_session(self):
        with self.open(GLM_GO, QWEN_GO):
            self.assertEqual(core.quota_drop(DS_GO, self.log, reason='pay'), '')
        self.assertEqual(self.task()['path_pending'], DS_GO)
        with self.open(DS_GO, GLM_GO, QWEN_GO):
            self.assertEqual(core.path_hand_over(DS_GO, self.log, 'pending route for the same model', model_handover=False, pending=True), DS_GO)
        self.assertEqual(self.task()['path_pending'], '')

    def test_if_a_model_falls_on_all_routes_the_next_model_comes_counted(self):
        router.failure('opencode-go', 'requires Global regions', allowed_fails=1, model=DS_GO)
        router.failure('openrouter', 'upstream error', allowed_fails=1, model=DS_OR)
        with self.open(DS_GO, DS_OR, GLM_GO):
            self.assertEqual(core.path_hand_over(DS_GO, self.log, 'error'), GLM_GO)
        last = self.task()['path_handovers'][-1]
        self.assertEqual((last['round'], last['counted'], last['next']), ('model', True, GLM_GO))
        self.assertEqual(self.task()['path_pending'], '', 'Moved to the next model; no pending route for the engine to overwrite')
        self.assertEqual(core.handover_counter(), (1, 1))
        self.assertIn('MODEL HANDOFF', self.log.read_text())

    def test_model_wandering_stops_when_the_handover_cap_is_full(self):
        router.failure('opencode-go', 'x', allowed_fails=1, model=DS_GO)
        with patch.object(core, 'HANDOVER_LANE_CAP', 0), self.open(DS_GO, GLM_GO):
            self.assertEqual(core.path_hand_over(DS_GO, self.log, 'error'), '')
        self.assertEqual(self.task()['path_pending'], DS_GO)
        self.assertIn('HANDOVER LIMIT', self.log.read_text())

    def test_share_exhaustion_writes_no_failure_and_skips_the_same_pool(self):
        with self.open(DS_GO, DS_OR, GLM_GO):
            self.assertEqual(core.quota_drop(DS_GO, self.log, reason='pay'), DS_OR)
        self.assertEqual(self.failures(), {}, 'Allocation or window exhaustion is not a failure')
        with self.open(DS_GO, GLM_GO, QWEN_GO, 'openrouter/z-ai/glm-5.3'):   # budget: the model does not change, only the same model's route
            self.assertEqual(core.quota_drop(DS_GO, self.log, reason='pay'), '')
        self.assertEqual(self.task()['path_pending'], DS_GO)

    def test_or_limit_closes_the_model_route_the_go_window_does_not(self):
        with self.open(DS_GO, DS_OR, GLM_GO):
            self.assertEqual(core.quota_drop(DS_OR, self.log), DS_GO, 'The OR route failed; try Go for the same model first')
        self.assertTrue(router.in_cooldown('openrouter', model=DS_OR))
        router.status_write({})
        with self.open(DS_GO, DS_OR, GLM_GO):
            self.assertEqual(core.quota_drop(DS_GO, self.log), DS_OR)
        self.assertEqual(self.failures(), {}, 'The Go limit signature is a window boundary, not a failure (GO1)')

    def test_helper_call_does_not_change_the_model(self):
        router.failure('opencode-go', 'x', allowed_fails=1, model=GLM_GO)
        with self.open(GLM_GO, QWEN_GO):
            self.assertEqual(core.path_hand_over(GLM_GO, self.log, 'Go limit', 'quality', model_handover=False), '')


class ErrorBurstTests(unittest.TestCase):
    def test_title_agent_and_budget_rejection_do_not_count_model_id_is_extracted(self):
        now = time.time()
        stamp = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(now))
        line = (f'timestamp={stamp} level=ERROR message="stream error" providerID=opencode-go modelID=deepseek-v4-pro '
                 f'session.id=ses_a agent=build error.error="requires Global regions"\n')
        title = (f'timestamp={stamp} level=ERROR message="stream error" providerID=opencode-go modelID=glm-5.3 '
                  f'session.id=ses_a agent=title error.error="x"\n')
        red = (f'timestamp={stamp} level=ERROR message="stream error" providerID=opencode-go modelID=glm-5.3 '
               f'session.id=ses_a agent=build error.error="Error: CADRE_BUDGET_DENIED: monthly request context missing"\n')
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / 'opencode.log'
            log.write_text(title * 6 + red * 6)
            with patch.object(core, 'OC_LOG', log):
                self.assertIsNone(core.error_burst(sessions={'ses_a'}, start=now - 30))
                log.write_text(title * 6 + red * 6 + line * 6)
                self.assertEqual(core.error_burst(sessions={'ses_a'}, start=now - 30)[2:], ('opencode-go', 'deepseek-v4-pro'))


import test_end_to_end as e2e  # noqa: E402

BURST_WORKER = """
import json, os, pathlib, sys, time
args = sys.argv[1:]
home = pathlib.Path(os.environ['CADRE_HOME'])
model = args[args.index('-m') + 1] if '-m' in args else ''
mark = home / 'ds-burst'
if os.environ.get('CADRE_TASK_ID') == 'g1' and model == 'opencode-go/deepseek-v4-pro' and not mark.exists():
    mark.write_text('1')
    sid = 'ses_burst'
    print(json.dumps({'type': 'step_start', 'sessionID': sid, 'part': {'type': 'step-start'}}), flush=True)
    stamp = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime())
    with open(os.environ['CADRE_OC_LOG'], 'a') as f:
        for _ in range(6):
            f.write(f'timestamp={stamp} level=ERROR message="stream error" providerID=opencode-go modelID=deepseek-v4-pro '
                    f'session.id={sid} agent=build error.error="requires Global regions"\\n')
    time.sleep(20)   # The engine detects the burst on a tick and stops it; otherwise the attempt would cause a test timeout
    sys.exit(1)
"""


class RunnerModelHandoverTests(e2e.E2EFixture):
    """16(20): if Go DS-pro (model, route) fails, the same session goes through the engine loop and is handed to the next model in the chain (glm-5.3);
    `path_pending` does not override the next model, it counts as a hand-off, the Go provider does not close."""

    def test_a_bursting_model_route_is_handed_to_the_next_model_and_the_job_finishes(self):
        fake = self.fake.read_text()
        call_record = "sid = args[args.index('--session')+1]"   # after the call record: the failed attempt must also show in the record
        self.assertIn(call_record, fake)
        self.fake.write_text(fake.replace(call_record, BURST_WORKER + call_record, 1))
        rid = self.submit()
        record = self.wait_terminal(rid, timeout=240)
        log = (self.store.path(rid) / 'coordinator.log').read_text()
        self.assertEqual(record['state'], 'completed', log[-3000:])
        g1 = record['tasks']['g1']
        models = [c['args'][c['args'].index('-m') + 1] for c in self.calls() if c['task'] == 'g1' and '-m' in c['args']]
        self.assertEqual(models[0], DS_GO, models)
        self.assertIn(GLM_GO, models, 'The same session continues with the next model')
        handover = [d for d in g1.get('path_handovers') or [] if d.get('round') == 'model']
        self.assertTrue(handover and handover[0]['model'] == DS_GO and handover[0]['next'] == GLM_GO, g1.get('path_handovers'))
        self.assertEqual(g1.get('path_pending'), '')
        status = json.loads((self.home / 'chain/provider-status.json').read_text())
        self.assertGreater(float(status[DS_GO]['cooldown_until']), time.time(), 'The failed model route is cooling down')
        self.assertLessEqual(float((status.get('opencode-go') or {}).get('cooldown_until', 0)), time.time(), 'The Go provider remains open')


if __name__ == '__main__':
    unittest.main()


class AccountSignalPathTests(unittest.TestCase):
    """4c QA (Fable, 07.10): an account signal (401/402/insufficient balance) never reached the classifier — the burst sample came from ERROR_PATTERN,
    the engine only passed the matching word. The sample and signature line now carry the class; the Go pool wall does not move to another Go model."""

    def test_the_burst_sample_carries_the_account_signal(self):
        import tempfile
        now = time.time()
        stamp = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(now))
        start = f'timestamp={stamp} level=ERROR message="stream error" providerID=openrouter modelID=z-ai/glm-5.3 session.id=ses_h '
        for error in ('error.error="402 Insufficient credits. Add more using https://openrouter.ai"',
                     'error.error.code=402 error.error.message="This request requires more credits" '
                     'error.error.metadata.error_type=payment_required'):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as d:
                log = Path(d) / 'opencode.log'
                log.write_text((start + error + '\n') * 6)
                with patch.object(core, 'OC_LOG', log):
                    hp = core.error_burst(sessions={'ses_h'}, start=now - 30)
                self.assertEqual(router.error_category(hp[1]), 'account', hp)
                self.assertEqual(hp[2:], ('openrouter', 'z-ai/glm-5.3'))

    def test_bare_numbers_are_not_account_signals(self):
        """4c QA2 (Fable): because "401"/"402" were searched as substrings, "(401 s)" and "140100 tokens" in the engine log
        counted as account signals and closed the provider for 60 min. A code is an account signal only in an HTTP context (status/code/error/HTTP)."""
        for text, category in (('QA REPAIR: finished (401 s)', 'error'), ('context length 140100 exceeded', 'context'),
                             ('1429 tokens used', 'error'), ('Error: 402 Insufficient credits', 'account'),
                             ('"code":401', 'account'), ('HTTP 401 Unauthorized', 'account'), ('rate limit 429', 'quota')):
            with self.subTest(text=text):
                self.assertEqual(router.error_category(text), category)

    def test_account_signal_line_reaches_the_breaker_and_go_pool_wall_stays_on_the_model(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            logf = Path(d) / 'm.log'
            with patch.object(router, 'failure') as failure, patch.object(core, 'path_hand_over', return_value='') as handover:
                core.quota_drop('openrouter/z-ai/glm-5.3', logf, error='Error: 402 Insufficient credits for this request')
                self.assertEqual(router.error_category(failure.call_args.args[1]), 'account')
                core.quota_drop('opencode-go/glm-5.3', logf, error='Error: 429 rate limit reached')
                self.assertFalse(handover.call_args.kwargs.get('model_handover', True), 'The Go pool is shared; do not switch to another Go model at the window boundary')
                self.assertEqual(failure.call_count, 1, 'A Go window limit does not record a failure')

    def test_a_pending_route_resumes_on_itself_once_it_reopens(self):
        import tempfile
        ds, glm = 'opencode-go/deepseek-v4-pro', 'opencode-go/glm-5.3'
        with tempfile.TemporaryDirectory() as d, patch.object(router, 'chain', return_value=([ds, glm], [])), \
             patch.dict(os.environ, {'CADRE_RUN_ID': ''}):
            logf = Path(d) / 'm.log'
            self.assertEqual(core.path_hand_over(ds, logf, 'pending route for the same model', model_handover=False, pending=True), ds)
            self.assertEqual(core.path_hand_over(ds, logf, 'Go limit', model_handover=False), '')

    def test_burst_on_the_go_pool_is_a_window_wall_and_bare_numbers_are_not_limit_signatures(self):
        self.assertFalse(core.is_burst_failure('opencode-go', 'status 429 Too Many Requests'))
        self.assertTrue(core.is_burst_failure('opencode-go', 'requires Global regions'))
        self.assertTrue(core.is_burst_failure('openrouter', 'status 429 Too Many Requests'))
        for text, expected in (('{"output":401}', None), ('QA REPAIR: finished (401 s)', None), ('{"tokens":413}', None),
                                (' ERROR-LOOP:status 429 Too Many Requests', 'status 429'), ('"code":429', 'code":429'),
                                ('Error: 402 Insufficient credits', 'Error: 402'), ('HTTP 401 Unauthorized', 'HTTP 401')):
            with self.subTest(text=text):
                match = core.LIMIT_SIGNATURE.search(text)
                self.assertEqual(match.group(0) if match else None, expected)
