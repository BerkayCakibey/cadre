"""4c-1/11: direct DeepSeek, Cerebras, direct OpenAI, Mistral, Gemini and a second Groq account were removed; no code path, constant or
endpoint address remains (Groq only in the pre-filter, direct HTTP).
'deepseek' as a model family/prefix (DeepSeek models on Go/OR) and the OR tier tag 'openai' are not providers."""
from __future__ import annotations

from pathlib import Path
import re
import sys
import unittest

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE / 'lib'))
import monthly_budget  # noqa: E402
import budget_gate  # noqa: E402
import model_catalog  # noqa: E402
import plan_ds  # noqa: E402
import qe_core  # noqa: E402
import model_router as router  # noqa: E402

REMOVED = router.REMOVED_PROVIDERS
LEGITIMATE = re.compile(r"[sS]tarts[wW]ith\(['\"]deepseek['\"]\)|for a in \('deepseek', 'qwen'|^REMOVED_PROVIDERS = ")   # model prefix / family name / definition


def provider(route: str) -> str:
    return str(route).split('|')[0].split('/', 1)[0]


class CodePathTests(unittest.TestCase):
    def test_nothing_removed_in_the_provider_constants(self):
        sets = {
            'PAID': budget_gate.PAID, 'FREE': budget_gate.FREE, 'DAILY_REQUEST': budget_gate.DAILY_REQUEST,
            'PROVIDER': router.PROVIDER, 'CAP_TYPE': router.CAP_TYPE,
            'OBSERVATION_KEY': router.OBSERVATION_KEY, 'PAID': router.PAID, 'USD_PROVIDERS': qe_core.USD_PROVIDERS,
            'JUDGE_EXPENSE_ORDER': qe_core.JUDGE_EXPENSE_ORDER, 'monthly PROVIDERS': monthly_budget.PROVIDERS,
            'catalog ID': model_catalog.ID, 'plan_ds ENDPOINTS': plan_ds.ENDPOINTS,
        }
        for entry_name, collection in sets.items():
            with self.subTest(constant=entry_name):
                self.assertFalse(set(collection) & set(REMOVED), entry_name)
        routes = {*router.QUALITY_STRICT, *router.EXPENSIVE, *router.SHORT.values(), *(r for z in router.ROLES.values() for r in z)}
        self.assertFalse({provider(r) for r in routes if r} & set(REMOVED), 'chain, short name and multiplier route')

    def test_nothing_is_removed_in_addon_sets(self):
        for file in ('budget-plugin.mjs', 'monthly-budget-plugin.mjs'):
            text = (ENGINE / 'lib' / file).read_text()
            for collection in re.findall(r"new Set\(\[([^\]]*)\]\)", text):
                with self.subTest(file=file, set=collection):
                    self.assertFalse(set(re.findall(r"'([\w-]+)'", collection)) & set(REMOVED))

    def test_observation_key_of_the_provider_removed_from_the_code_is_not_read(self):
        """07.10 live (16b): the dispatcher still read ['ds']['ratio'] from the budget observation (a KeyError trace); the identity guard
        only looked for the provider name. The observation keys of removed providers are not read in the code either."""
        key = re.compile(r"['\"](ds|oa|gemini|cerebras|mistral)['\"]")   # after 16b: the screen summary also carried ('DS', 'ds')
        findings = []
        for path in [*(ENGINE / 'lib').glob('*.py'), *(ENGINE / 'lib').glob('*.mjs'), *(ENGINE / 'bin').iterdir()]:
            if path.is_file():
                findings += [f'{path.name}:{no}' for no, line in enumerate(path.read_text(errors='replace').splitlines(), 1)
                             if key.search(line) and not LEGITIMATE.search(line)]
        self.assertEqual(findings, [])

    def test_provider_id_and_endpoint_address_no_longer_remain_in_the_code(self):
        id = re.compile(r"(^|[^/\w.-])['\"](" + '|'.join(REMOVED) + r")['\"]")
        route = re.compile(r"['\"](" + '|'.join(k for k in REMOVED if k != 'openai') + r")/")
        address = re.compile(r"api\.(" + '|'.join(REMOVED) + r")\.")
        roots = [*(ENGINE / 'lib').glob('*.py'), *(ENGINE / 'lib').glob('*.mjs'), *(ENGINE / 'bin').iterdir(),
                  *(ENGINE.parent / 'config' / 'agents').glob('*.md')]
        findings = []
        for path in roots:
            relative = str(path.relative_to(ENGINE)) if path.is_relative_to(ENGINE) else str(path)
            if not path.is_file():
                continue
            for no, line in enumerate(path.read_text(errors='replace').splitlines(), 1):
                if LEGITIMATE.search(line):
                    continue
                if id.search(line) or route.search(line) or address.search(line):
                    findings.append(f'{relative}:{no}: {line.strip()[:100]}')
        self.assertEqual(findings, [])


if __name__ == '__main__':
    unittest.main()
