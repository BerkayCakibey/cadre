"""4c-19: reality probes — an assumption deviation becomes a PROBLEM; no network and no money (fake API cache, fake opencode, temporary DB)."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import reality  # noqa: E402


class RealityTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(prefix='qe-reality-')
        self.addCleanup(t.cleanup)
        self.home = Path(t.name)
        (self.home / 'chain').mkdir()

    def test_go_windows_are_compared_one_by_one_with_rounding(self):
        api = {'ts': time.time(), 'rolling': 3, 'weekly': 12, 'monthly': 27, 'resets': {}}
        with patch.object(reality.go_window, '_cache', return_value=api), \
             patch.object(reality.go_window, 'own_percentages', return_value={'rolling': 4.9, 'weekly': 14.2, 'monthly': 25.8}):
            # the API truncates down: Σ − API ∈ [−1, 2] is accepted (rolling 1.9 passes); weekly +2.2 and monthly −1.2 are deviations
            self.assertEqual(reality.go_windows(self.home, self.home / 'db', 0),
                             ['go-window weekly: Σ %14.2 ≠ API %12', 'go-window monthly: Σ %25.8 ≠ API %27'])
        with patch.object(reality.go_window, '_cache', return_value={}):
            self.assertEqual(reality.go_windows(self.home, self.home / 'db', 0), ['go-window: API observation missing'])

    def test_every_chain_model_must_carry_its_role_output_and_or_needs_one_fitting_endpoint(self):
        catalog = {'opencode-go': {'models': {'glm-5.3': {'limit': {'output': 131072}}, 'abridged': {'limit': {'output': 16384}}}}}
        price = {'models': {'deepseek/deepseek-v4-pro': {'tiers': {
            'DeepInfra': {'max_output': 16384, 'params': ['max_tokens']}, 'StreamLake': {'max_output': 384000, 'params': ['max_tokens']}}},
            'z-ai/glm-5.3': {'tiers': {'Novita': {'max_output': 16384, 'params': ['max_tokens']}}}}}
        chains = {'impl': ['opencode-go/glm-5.3', 'openrouter/deepseek/deepseek-v4-pro', 'openrouter/z-ai/glm-5.3'],
                     'plan': ['claude/opus#high|claude/sonnet#high', 'opencode-go/abridged', 'opencode-go/absent']}
        self.assertEqual(reality.endpoint_limits(price, catalog, chains), [
            'endpoint impl: openrouter/z-ai/glm-5.3 no allowed endpoint supports 32768 tokens',
            'endpoint plan: opencode-go/abridged output 16384 < 65536', 'endpoint plan: opencode-go/absent absent from the catalog'])

    def test_sol_tiers_follow_the_policy_and_the_price_cap(self):
        policy = {'openai/gpt-6.1-sol': {'closed': True, 'tiers': ['openai/flex', 'openai', 'azure'],
                                           'max_price': {'prompt': 2, 'completion': 10}}}
        price = {'models': {'openai/gpt-6.1-sol': {'tiers': {
            'openai': {'input': 2, 'output': 10, 'params': ['max_tokens']},
            'azure': {'input': 2.2, 'output': 11, 'params': []}}}}}
        self.assertEqual(reality.sol_tiers(price, policy), [
            'sol: openai/gpt-6.1-sol tier missing: openai/flex',
            'sol: openai/gpt-6.1-sol azure token parameter unknown',
            'sol: openai/gpt-6.1-sol azure exceeds the maximum price'])

    def test_a_catalogue_price_change_is_reported_against_the_last_canary(self):
        catalog = {'opencode-go': {'models': {'glm-5.3': {'cost': {'input': 1.4, 'output': 4.4}}}}}
        chains = {'quality': ['opencode-go/glm-5.3']}
        problem, now_ts = reality.prices(self.home, catalog, chains)
        self.assertEqual(problem, [])
        (self.home / 'chain/reality.json').write_text(json.dumps({'prices': now_ts}))
        catalog['opencode-go']['models']['glm-5.3']['cost']['output'] = 5.5
        self.assertEqual(reality.prices(self.home, catalog, chains)[0], ['price: opencode-go/glm-5.3 1.4/4.4 -> 1.4/5.5'])

    def test_thinking_must_leave_text_in_the_opencode_database(self):
        db = self.home / 'opencode.db'
        with closing(sqlite3.connect(db)) as con:
            con.execute('create table message(id text, session_id text)')
            con.execute('create table part(id text, message_id text, data text)')
            con.execute("insert into message values('m1', 'ses_d')")
            con.execute("insert into part values('p1', 'm1', ?)", (json.dumps({'type': 'reasoning', 'text': 'I reasoned through it'}),))
            con.commit()
        fake = self.home / 'opencode'
        environment_file = self.home / 'environment.json'
        fake.write_text(f'#!{sys.executable}\nimport json, os, sys\n'
                         f'open({str(environment_file)!r}, "w").write(json.dumps(dict(os.environ)))\n'
                         'print(json.dumps({"type": "text", "sessionID": sys.argv[sys.argv.index("-m") + 1].split("/")[-1]}))\n')
        fake.chmod(0o755)
        with patch.dict('os.environ', {'CADRE_RUN_ID': 'other-run', 'CADRE_BUDGET_OBSERVATION': '1'}):
            self.assertEqual(reality.thinking_text('opencode-go/ses_d', str(fake), db), [])
        environment = json.loads(environment_file.read_text())
        self.assertTrue(all(environment.get(k) for k in ('CADRE_PYTHON', 'CADRE_HOME', 'CADRE_ENGINE_ROOT')), 'The gate is configured in the cron environment')
        self.assertNotIn('CADRE_RUN_ID', environment)
        self.assertNotIn('CADRE_BUDGET_OBSERVATION', environment, 'The canary passes through the gate instead of observation mode')
        self.assertEqual(environment.get('CADRE_ROLE'), 'plan', 'Request thinking explicitly; a roleless call leaves the decision to the model')
        self.assertEqual(reality.thinking_text('opencode-go/ses_empty', str(fake), db),
                         ['thinking: opencode-go/ses_empty produced no thinking text (2 attempts)'])


if __name__ == '__main__':
    unittest.main()
