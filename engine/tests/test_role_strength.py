"""Item 1 — role -> strength table and chain/config drift.

17.09 measurement (opencode database, the current Go billing period): 69% of Go spending was concentrated in ONE model
— qwen3.8-max $2.7349 / 126 calls, total Go $3.9494. Cause: the plan role runs once per run but with the LARGEST
context and was bound to the most expensive model. These tests keep that concentration from coming back and keep the
role->model mapping written in two separate places from drifting silently."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import model_router as router  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / 'config/opencode.jsonc'


def config_read():
    """opencode.jsonc in the repo copy. When run from the live install (under CADRE_HOME) this file does NOT EXIST —
    on 17.09 RC51 broke exactly like that; in that case the test is skipped, it does not pass silently."""
    text = re.sub(r'^\s*//.*$', '', CONFIG.read_text(encoding='utf-8'), flags=re.M)
    return json.loads(text)


class RoleStrengthTests(unittest.TestCase):
    def test_typed_approved_model_pools_survive_lost_environment_child_and_provider_cooldown(self):
        from unittest.mock import patch

        from run_store import atomic_json, request_permission, request_proposal
        from test_end_to_end import E2EFixture
        fixture = E2EFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        pinned, other = 'opencode-go/deepseek-v4-pro', 'opencode-go/glm-5.3'
        raw = {'submission_id': 'frozen-pools', 'project': 'e2e', 'target_path': str(fixture.repo), 'target_kind': 'existing',
               'input_mode': 'goal', 'prompt': 'Fix README wording', 'output': 'Correct README', 'acceptance': 'README retained',
               'preference': 'economic', 'difficulty_override': 'easy', 'duration_minutes': 40, 'budget_usd': '3',
               'backlog_path': '', 'source_token': ''}
        record = fixture.store.request(raw)
        rid = record['run_id']
        env = {**fixture.env, 'CADRE_RUN_ID': rid, 'CADRE_TASK_ID': 'g1', 'CADRE_CLAUDE_PLAN': '0'}
        for role in ('PLAN', 'REVIEW', 'IMPL', 'IMPL_LIGHT', 'QUALITY', 'MERGE', 'RECOVERY'):
            env['CADRE_CHAIN_' + role] = pinned
        with patch.dict(os.environ, env), patch.object(router, 'QE', fixture.home), \
             patch.object(router, 'STATUS', fixture.home / 'chain/state.json'), \
             patch.object(router, 'RECORD', fixture.home / 'chain/router.jsonl'), \
             patch.object(router, 'has_budget', return_value=(True, 'fixture capacity')), \
             patch.object(router, 'ladder', return_value='active'):
            with patch.object(router, 'chain', return_value=([pinned], ['fixture eligible route'])):
                proposal = request_proposal(fixture.store, rid)
                first = request_permission(fixture.store, rid, proposal)
                request_permission(fixture.store, rid, proposal, first['confirmation'])
            for key in env:
                if key.startswith('CADRE_CHAIN_'):
                    os.environ[key] = other
            budget = {'fixture': True}
            for role in ('impl', 'impl-light', 'writer', 'review', 'quality'):
                with self.subTest(role=role):
                    models = router.chain(role, request_token=1000, b=budget, d={}, save=False)[0]
                    self.assertEqual(models, [pinned])
            child = fixture.store.accept('e2e', 'Child code task')['run_id']
            fixture.store.update(child, project_ref_parent=rid)
            with patch.dict(os.environ, {'CADRE_RUN_ID': child}):
                models = router.chain('impl', request_token=1000, b=budget, d={}, save=False)[0]
                self.assertEqual(models, [pinned])
                self.assertEqual(router.chain('impl', request_token=1000, b=budget, d={
                    'opencode-go': {'cooldown_until': 10 ** 12}}, save=False)[0], [])
            changed = fixture.store.read(rid)
            changed['request_permission']['action']['models']['impl'] = [other]
            atomic_json(fixture.store.path(rid) / 'run.json', changed)
            self.assertEqual(router.chain('impl', request_token=1000, b=budget, d={}, save=False)[0], [])

    def test_every_role_declares_why_it_needs_its_power(self):
        for role in router.ROLES:
            if role == 'main':
                continue
            self.assertIn(role, router.ROLE_STRENGTH, f'{role} has no ROLE_STRENGTH record')
            record = router.ROLE_STRENGTH[role]
            self.assertIn(record['strength'], ('high', 'middle', 'low'))
            self.assertGreater(record['context'], 0)
            self.assertGreater(record['frequency'], 0)
            self.assertTrue(record['note'].strip())

    def test_no_chain_carries_a_slow_expensive_model_any_more(self):
        """User decision (18.09 evening): glm-5.2 and kimi-k2.7-code were REMOVED from the chains — glm-5.2 was the slowest
        of the round (22 min) and cost $4.4; kimi is in the same price class but unmeasured. qwen3.8-max and kimi-k3
        were deliberately put back into the plan/review chains with D13 (03.10)."""
        for expensive in ('opencode-go/glm-5.2', 'opencode-go/kimi-k2.7-code',
                       'opencode-go/grok-4.6',
                       'opencode-go/mimo-v2.5',      # 18.09 exam: 1/3 and 39.6 s — the slowest, the most wrong
                       'opencode-go/minimax-m3'):    # 18.09 exam: 1/3 (the same name on zen 3/3, do not mix up)
            for role, chain in router.ROLES.items():
                with self.subTest(model=expensive, role=role):
                    self.assertNotIn(expensive, chain)

    def test_default_writer_and_reviewer_chains_lead_with_different_models(self):
        """Regression of the current default chains; a different model name alone does not prove N-QA03 independence.
        When the new clean-session policy is applied in CR-18 it must not be confused with this default preference."""
        def first_cloud(role):
            return next(m for m in router.ROLES[role] if not m.startswith(('lmstudio/', 'claude/')) and '|' not in m)
        for role in ('quality', 'review'):
            with self.subTest(chain=role):
                self.assertNotEqual(first_cloud('impl'), first_cloud(role),
                                    'Fallback chains must start with different models')

    def test_the_write_and_review_roles_lead_with_the_measured_best_value_models(self):
        """D13: impl starts with Go deepseek-v4-pro (subscription). 4c-5/6: plan Claude > Sol > glm > qwen > Zen Sol;
        the plan review pool is kept; the final acceptance only on the measured FLEX route."""
        first = lambda role: next(m for m in router.ROLES[role] if not m.startswith(('lmstudio/', 'claude/')) and '|' not in m)
        self.assertEqual(first('impl'), 'opencode-go/deepseek-v4-pro')
        plan = ['openrouter/openai/gpt-6.1-sol', 'opencode-go/glm-5.3', 'openrouter/z-ai/glm-5.3', 'opencode-go/qwen3.8-max',
                'openrouter/qwen/qwen3.8-max-0902', 'opencode/gpt-6.1-sol']
        self.assertEqual(router.ROLES['plan'][1:], plan)
        self.assertEqual(router.ROLES['quality'], [router.OR_SOL])
        self.assertEqual(router.ROLES['review'], plan + ['opencode/glm-5.3', 'opencode/qwen3.8-max'])
        self.assertIsNot(router.ROLES['review'], router.ROLES['quality'])
        self.assertFalse(any('kimi' in m for chain in router.ROLES.values() for m in chain))
        self.assertFalse(any(m.startswith('claude/') for m in router.ROLES['quality']))


    def test_plan_review_families_keep_their_two_provider_routes(self):
        import qe_core as core
        writing = core.model_family(next(m for m in router.ROLES['impl']))
        for planner in ('openrouter/openai/gpt-6.1-sol', 'openrouter/z-ai/glm-5.3'):
            paths: dict[str, set[str]] = {}
            for m in router.ROLES['review']:
                if core.model_family(m) not in {writing, core.model_family(planner)}:
                    paths.setdefault(core.model_family(m), set()).add(m.split('/', 1)[0])
            with self.subTest(planner=planner):
                self.assertGreaterEqual(len(paths), 2, paths)
                self.assertTrue(all(len(v) >= 2 for v in paths.values()), paths)
                self.assertTrue(any('openrouter' in v for v in paths.values()), 'the OpenRouter judge remains when Go is exhausted')

    def test_the_trailing_zen_block_stays_the_last_resort(self):
        import tempfile
        import time
        from unittest.mock import patch
        sol, zen_sol = 'openrouter/openai/gpt-6.1-sol', 'opencode/gpt-6.1-sol'
        or_glm, or_qwen = 'openrouter/z-ai/glm-5.3', 'openrouter/qwen/qwen3.8-max-0902'
        with tempfile.TemporaryDirectory() as t, patch.object(router, 'QE', Path(t)), patch.object(router, 'record'), \
             patch.object(router, 'has_budget', side_effect=lambda provider_key, *a, **k: (provider_key != 'opencode-go', 'fixture')), \
             patch.object(router, 'ladder', return_value='active'), patch('model_catalog.available', return_value=True), \
             patch.dict(os.environ, {'CADRE_CLAUDE_PLAN': '0'}):
            review = router.chain('review', b={'x': 1}, d={})[0]
            judge = router.chain('quality', b={'x': 1}, d={})[0]
            cold = {router.model_key('openrouter', sol): {'cooldown_until': time.time() + 600}}
            plan = router.chain('plan', b={'x': 1}, d=cold)[0]
        self.assertEqual(review, [sol, or_glm, or_qwen, 'opencode/glm-5.3', 'opencode/qwen3.8-max'],
                         'Zen GLM does not precede OpenRouter Qwen when Go is exhausted')
        self.assertEqual(judge, [sol])
        self.assertEqual(plan, [or_glm, or_qwen, zen_sol], 'Zen Sol is the last fallback when OpenRouter Sol fails')

    def test_final_review_waits_instead_of_falling_back_to_an_unqualified_model(self):
        from unittest.mock import patch
        with patch.object(router, 'has_budget', side_effect=lambda provider_key, model, *a, **k: (model != router.OR_SOL, 'fixture')), \
             patch.object(router, 'ladder', return_value='active'), patch.object(router, 'record'), \
             patch('model_catalog.available', return_value=True), \
             patch.dict(os.environ, {'CADRE_CHAIN_QUALITY': ''}):
            models, reasons = router.chain('quality', b={'x': 1}, d={})
        self.assertEqual(models, [])
        self.assertTrue(any(router.OR_SOL in reason for reason in reasons))

    def test_no_budget_does_not_turn_local_into_a_judge(self):
        """P3: while the budget cannot be read the judge chain does not fall to the local model."""
        chain, reason = router.chain('quality', 'middle', None, {'ts': 'x'}, {})
        self.assertEqual(chain, [])
        self.assertTrue(reason)


class OpenaiChainTests(unittest.TestCase):
    """D13 (03.10): the chains were rewritten. The openai/ route only via openrouter (Sol).
    Luna (gpt-6-luna) and qwen3.7-plus were removed from the chains; Sol's position in plan is after claude,
    in review/quality after kimi-k3 and glm-5.3."""
    FREE = ('opencode/nemotron-3.5-lightning-free', 'opencode/nemotron-3-ultra-free',
              'opencode/ling-3.1-flash-free', 'openrouter/nvidia/nemotron-3.5-lightning:free', 'google/gemini-3.1-flash-lite')

    def setUp(self):
        from unittest.mock import patch
        import tempfile
        import budget_gate
        paid = patch('model_catalog.is_free', return_value=False)
        paid.start()
        self.addCleanup(paid.stop)
        self.temp = tempfile.TemporaryDirectory(prefix='qe-oa-chain-')
        self.addCleanup(self.temp.cleanup)
        home_dir = Path(self.temp.name)
        (home_dir / 'chain').mkdir(parents=True)
        for module in (router, budget_gate):
            old_qe = getattr(module, 'QE', None)
            if old_qe is not None:
                setattr(module, 'QE', home_dir)
                self.addCleanup(lambda m=module, v=old_qe: setattr(m, 'QE', v))


    def oa_chain(self, available):
        from unittest.mock import patch
        from test_budget_scenarios import instant
        b = instant(job_cost={'judge': {'openrouter': 0.1}})
        b['or'] = {'db': 'ok', 'pay': 0.3, 'available': available + 0.045}
        with patch.object(router, 'has_budget', return_value=(True, 'test')), patch.object(router, 'is_compact', return_value=False):
            return router.chain('quality', 'middle', 20000, b, {})

    def test_sol_is_eligible_when_its_money_covers_a_review(self):
        z, _ = self.oa_chain(.2)
        self.assertIn('openrouter/openai/gpt-6.1-sol', z)

    def test_sol_stays_out_while_its_money_is_below_half_a_review(self):
        z, _ = self.oa_chain(.03)
        self.assertNotIn('openrouter/openai/gpt-6.1-sol', z)

    def test_small_change_reviews_put_a_backup_provider_last(self):
        """D13: the judge cost order changes by provider; expensive providers are moved back on a low budget."""
        import qe_core
        from test_budget_scenarios import instant
        order = qe_core.JUDGE_EXPENSE_ORDER
        # D13: the openai/ direct API was removed; opencode-go and deepseek comparison
        b = lambda used_amount: instant(go={'db': 'ok', 'pay': 2.0, 'available': (used_amount + 0.045) * 100 / 15},   # 4c-13: points
                              job_cost={'judge': {'opencode-go': 0.1}})
        self.assertEqual(qe_core.judge_cost('opencode-go', b(0.5)), order['opencode-go'])
        self.assertGreater(qe_core.judge_cost('opencode-go', b(0.05)), order['opencode-go'])
        self.assertEqual(qe_core.judge_cost('opencode-go', {}), order['opencode-go'])

    def test_openai_models_stay_out_of_writer_and_scout_chains(self):
        """D13: 'openai/' (direct API) left the chains; only 'openrouter/openai/' (Sol) remains."""
        for role in ('impl', 'impl-light', 'recovery', 'merge'):
            with self.subTest(role=role):
                self.assertFalse(any(m.startswith('openai/') for m in router.ROLES[role]))

    def test_an_openai_link_needs_a_verified_allocation(self):
        """D13: Sol via OpenRouter; budget key 'or' (formerly 'oa'). No direct openai API."""
        # D13: the 'openai/' direct API is not in the chains — verify
        for role, chain in router.ROLES.items():
            with self.subTest(role=role):
                self.assertFalse(any(m.startswith('openai/') for m in chain),
                                 f'{role}: must not use a direct openai model (D13)')


class RatioLimitsTests(unittest.TestCase):
    """B90 (17.09 live measurement + official docs): on free tiers what cuts us is not DOLLARS but per-minute
    token/request limits. These fields were EMPTY in the catalogue. Measured result: sending a 25k request to groq
    "Request too large" -> opencode compacts and retries -> LOOP (238 KB of log in 17 minutes)."""

    def test_the_published_limits_are_recorded_for_every_metered_free_tier(self):
        for provider_key, field in (('groq', 'tpm'), ('groq', 'rpd'), ('groq', 'rpm')):
            with self.subTest(provider_key=provider_key, field=field):
                self.assertIsInstance(router.PROVIDER[provider_key].get(field), int,
                                      f'{provider_key}.{field} must be recorded in the source comment')
        self.assertEqual(router.PROVIDER['groq']['tpm'], 8000)

    def test_a_request_bigger_than_the_minute_budget_cannot_reach_that_provider(self):
        """TPM is also the upper bound of a single request: 25K cannot be sent to a provider that gives 8K tokens a minute."""
        self.assertLessEqual(router.request_cap('groq'), router.PROVIDER['groq']['tpm'])
        # ... and providers without TPM are not affected by this
        self.assertGreater(router.request_cap('opencode-go'), 50_000)

    def test_a_large_request_drops_a_provider_whose_request_cap_is_smaller(self):
        """The rule is provider-independent: a candidate exceeding the request cap is removed and the reason is written.
        Until 19.09 this was tested with groq in this chain; since groq cannot run through opencode it left
        the chains (B113), and the rule is now tested directly on `has_budget`."""
        b = {'groq': {'db': 'ok', 'today_request': 0}}
        self.assertEqual(router.has_budget('groq', 'openai/gpt-oss-20b', b, 25000), (False, 'request cap'))
        self.assertNotEqual(router.has_budget('groq', 'openai/gpt-oss-20b', b, 3000)[1], 'request cap')

    def test_groq_is_kept_out_of_every_chain(self):
        """D13 (03.10): groq is in no chain (code-writing, plan and judge roles; with 4c-9/10 the helper chain was removed too)."""
        for role, chain in router.ROLES.items():
            for model in chain:
                with self.subTest(role=role, model=model):
                    self.assertFalse(model.startswith('groq'), f'{role}: Groq cannot run through opencode')


class ProviderDefinitionTest(unittest.TestCase):
    def test_every_paid_provider_in_a_chain_is_known_to_the_budget_check(self):
        """B336 (03.10): OpenRouter was added to the chains but has_budget did not recognize it ('undefined') -> it was never selected."""
        from unittest.mock import patch
        import budget_gate
        providers = {m.split('/', 1)[0] for z in router.ROLES.values() for ring in z for m in ring.split('|')} - {'claude', 'lmstudio'}
        with patch.object(budget_gate, 'available_budget', return_value=10**9):
            for provider_key in sorted(providers):
                with self.subTest(provider_key=provider_key):
                    self.assertNotEqual(router.has_budget(provider_key, f'{provider_key}/x', {'x': 1}, 1000)[1], 'undefined')
                    self.assertIn(provider_key, router.CAP_TYPE)


if __name__ == '__main__':
    unittest.main()


class SecretFilePermissionsTests(unittest.TestCase):
    """B110 (18.09): in the bash permissions only `cat *.env` was blocked; with `head/sed/grep/awk/python3/base64/cp`
    the same file could be read freely — the deny list gave a FALSE guarantee."""

    def rules(self):
        if not CONFIG.is_file():
            self.skipTest(f'repo config absent ({CONFIG})')
        return config_read()['agent']['build']['permission']['bash']

    def test_secret_files_are_denied_whatever_command_reads_them(self):
        rules = self.rules()
        for pattern in ('*.env', '*.env.*', '*/.env*', '*id_rsa*', '*.pem*', '*.key*', '*credentials*', '*.ssh/*'):
            with self.subTest(pattern=pattern):
                self.assertEqual(rules.get(pattern), 'deny', f'{pattern} must be rejected independently of the command')

    def test_template_files_stay_readable(self):
        rules = self.rules()
        read = config_read()['agent']['build']['permission']['read']
        for pattern, path in (('*.env.example*', '**/.env.example'), ('*.env.sample*', '**/.env.sample'), ('*.env.template*', '**/.env.template')):
            with self.subTest(pattern=pattern):
                self.assertEqual(read.get(path), 'allow', 'A template file is not a secret (B1 lesson)')
                self.assertEqual(rules.get(pattern), 'deny')


class PricePerformanceChainTests(unittest.TestCase):
    """B100 full round (18.09): all 37 models on Go and 103 on zen were scanned with their catalogue price.
    Rule: each role gets as much strength as it needs; an expensive model is not THROWN OUT of the chain but does not MOVE AHEAD."""

    CATALOG = Path(__file__).with_name('fixtures') / 'model-catalog.json'

    def catalog(self):
        return json.loads(self.CATALOG.read_text())

    def price(self, catalog, model):
        provider_key, _, mid = model.partition('/')
        v = ((catalog.get(provider_key) or {}).get('models') or {}).get(mid.split('#')[0]) or {}
        c = v.get('cost') or {}
        return float(c.get('input') or 0), float(c.get('output') or 0)

    def test_every_chain_model_exists_in_the_catalogue(self):
        """A typo in a chain = that link silently does not work; a mechanical check catches it."""
        catalog = self.catalog()
        for role, chain in router.ROLES.items():
            for record in chain:
                for model in record.split('|'):
                    provider_key = model.split('/', 1)[0]
                    if provider_key in ('claude', 'lmstudio'):
                        continue
                    with self.subTest(role=role, model=model):
                        mid = model.split('/', 1)[1].split('#')[0]
                        self.assertIn(mid, (catalog.get(provider_key) or {}).get('models') or {},
                                      f'{model} absent from the catalog')


    def test_every_chain_model_has_a_family(self):
        """4c-6: judge independence is measured by family; a model not in the list falls back to its own id and another model of the same vendor
        counts as 'independent'. The family of every model in the chains and short names is known."""
        import acceptance
        families = ('deepseek', 'qwen', 'glm', 'gpt', 'claude', 'gemini', 'nemotron', 'mimo')
        models = {m for chain in router.ROLES.values() for record in chain for m in record.split('|')}
        models |= {m for m in router.SHORT.values() if m}
        for m in sorted(models):
            with self.subTest(model=m):
                self.assertIn(acceptance.model_family(m), families)
        self.assertEqual(acceptance.model_family('opus'), 'claude', 'The plan record retains the id')
        self.assertEqual(acceptance.model_family('openrouter/qwen/qwen3.8-max-0902'), acceptance.model_family('opencode-go/qwen3.8-max'))

    def test_roles_follow_the_d13_model_order(self):
        """D13 (03.10) + 4c-6: plan Claude > Sol (OR) > glm > qwen; judge pool = plan pool, starting with Sol on a large change."""
        expected = {
            'plan': 'openrouter/openai/gpt-6.1-sol',
            'quality': 'openrouter/openai/gpt-6.1-sol',
            'impl': 'opencode-go/deepseek-v4-pro',
            'impl-light': 'opencode-go/deepseek-v4-pro',
            'merge': 'opencode-go/deepseek-v4-pro',
            'recovery': 'opencode-go/deepseek-v4-pro',
        }
        for role, model in expected.items():
            with self.subTest(role=role, model=model):
                first = next(m for m in router.ROLES[role] if not m.startswith(('claude/', 'lmstudio/')) and '|' not in m)
                self.assertEqual(first, model)


class DailyTokenCapTests(unittest.TestCase):
    """B106 (18.09): the `tpd` (daily token cap) column was in the table but read in NO decision.
    groq's free plan gives 200K TPD — 40 full-size calls a day with a 5K request; our daily REQUEST cap was
    900, so we never saw that we would hit the token side."""

    def test_a_request_bigger_than_the_remaining_daily_tokens_is_refused(self):
        # The request size is chosen BELOW the request cap: otherwise that rule catches it first and the daily rule is not tested.
        b = {'groq': {'db': 'ok', 'today_request': 10, 'today_token': 197000}}
        self.assertEqual(router.daily_token_remaining('groq', b), 3000)
        self.assertLess(4000, router.request_cap('groq'), 'The request cap must not mask this check')
        self.assertEqual(router.has_budget('groq', 'openai/gpt-oss-120b', b, 4000),
                         (False, 'daily token cap'))
        self.assertNotEqual(router.has_budget('groq', 'openai/gpt-oss-120b', b, 2000)[1], 'daily token cap')

    def test_the_rule_does_not_bind_without_an_observation_or_a_published_tpd(self):
        self.assertIsNone(router.daily_token_remaining('groq', {'groq': {'db': 'ok'}}), 'No rule without observation')
        self.assertIsNone(router.daily_token_remaining('deepseek', {'ds': {'today_token': 10}}), 'No rule without tpd')
        self.assertIsNone(router.daily_token_remaining('cerebras', {'cerebras': {'today_token': 10}}),
                          'No daily restriction in the Developer tier')


class MeasuredModelTests(unittest.TestCase):
    """18.09: the catalogue is a SUPERSET — of the 32 'free' models on zen only 8 are live in the account, Go's 2 free ones
    do not exist at all. Only LIVE models that PASS a trial go into a chain."""

    def test_plan_review_keeps_its_provider_alternatives(self):
        """Plan review alternatives are kept; the final acceptance pool is separate per the N-TS03 measurement."""
        paid = [m for m in router.ROLES['review']
                  if not m.startswith(('lmstudio/', 'claude/')) and '|' not in m]
        self.assertGreaterEqual(len(paid), 3,
                                f'The judge pool must not depend on one provider: {paid}')
        providers = {router.provider(m) for m in paid}
        self.assertGreaterEqual(len(providers), 3,
                                f'Judge candidates must come from different providers: {sorted(providers)}')


class SubAgentModelTests(unittest.TestCase):
    """B121 (19.09): `restriction_config_write` picks a model for every sub-agent from ITS OWN role's chain, with ITS OWN declared context.
    4c-10: compaction is never written to this file (it is the lane's own model); Go is evaluated per model, not as a whole."""

    def _divide(self):
        from datetime import datetime
        now_ts = datetime.now()
        return {'ts': now_ts.isoformat(), 'today': now_ts.date().isoformat(),
                'go': {'db': 'ok', 'pay': 33.333334, 'available': 33.333334, 'window_open': True, 'unit': 'points'},
                'zen': {'db': 'ok', 'pay': 5.0, 'available': 5.0},
                'gemini': {'db': 'ok', 'today_request': 0}}

    def test_runtime_config_uses_each_role_chain_and_disables_unavailable_roles(self):
        """N-RT01: the real config_write output is bound to the role's first OpenCode candidate; an empty candidate does not invent a new model."""
        import tempfile
        from unittest.mock import patch

        chains = {'plan': ['claude/opus#high', 'fixture/planner', 'fixture/backup'],
                     'review': [], 'impl': ['fixture/developer']}
        with tempfile.TemporaryDirectory(prefix='qe-router-config-') as directory, \
             patch.object(router, 'chain', side_effect=lambda role, *a: (chains[role], [])) as select_chain:
            path, config = router.config_write(Path(directory) / 'config.json', b={'fixture': True}, d={})
            self.assertEqual(config['agent'], {'cadre-plan': {'model': 'fixture/planner'},
                                              'cadre-review': {'disable': True},
                                              'cadre-impl': {'model': 'fixture/developer'}})
            self.assertEqual(json.loads(Path(path).read_text()), config)
            self.assertEqual([call.args for call in select_chain.call_args_list],
                             [(role, 'middle', None, {'fixture': True}, {}) for role in ('plan', 'review', 'impl')])

    def test_compaction_is_never_written_and_go_is_judged_per_model(self):
        import json
        import qe_core as core
        path, choice, digit = core.restriction_config_write('test', budget=self._divide())
        cfg = json.loads(Path(path).read_text())
        self.assertNotIn('compaction', cfg['agent'], 'Compaction uses the lane model and is not overwritten by restriction config')
        self.assertNotIn('cadre-scout', cfg['agent'])
        self.assertIn('cadre-impl=opencode-go/', choice, 'Select the Go route for the code model when Go has capacity')
        self.assertEqual(digit, 'opencode-go')

    def test_a_role_that_needs_more_context_never_gets_a_model_that_cannot_hold_it(self):
        import json
        import qe_core as core
        path, _m, _b = core.restriction_config_write('test', budget=self._divide())
        cfg = json.loads(Path(path).read_text())
        match = {'cadre-plan': 'plan', 'cadre-review': 'review', 'cadre-impl': 'impl'}
        tested = []
        for agent, role in match.items():
            model = (cfg['agent'].get(agent) or {}).get('model')
            if not model:
                self.assertTrue(cfg['agent'][agent].get('disable'), f'{agent}: A role without a model must be disabled')
                continue
            tested.append(agent)
            provider_key = model.split('/', 1)[0]
            with self.subTest(agent=agent, model=model):
                self.assertLessEqual(router.role_context(role), router.request_cap(provider_key, model),
                                     f'{agent}: {role} {router.role_context(role)} tokens required; {model} supports at most '
                                     f'{router.request_cap(provider_key, model)} supports')
        self.assertTrue(tested, 'At least one actual role model context must be tested')
