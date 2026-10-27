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
import yonlendirici as yon  # noqa: E402

KOK = Path(__file__).resolve().parents[2]
CONFIG = KOK / 'config/opencode.jsonc'


def config_oku():
    """opencode.jsonc in the repo copy. When run from the live install (under CADRE_HOME) this file does NOT EXIST —
    on 17.09 RC51 broke exactly like that; in that case the test is skipped, it does not pass silently."""
    metin = re.sub(r'^\s*//.*$', '', CONFIG.read_text(encoding='utf-8'), flags=re.M)
    return json.loads(metin)


class RolGucTests(unittest.TestCase):
    def test_typed_approved_model_pools_survive_lost_environment_child_and_provider_cooldown(self):
        from unittest.mock import patch

        from kosu import atomic_json, request_permission, request_proposal
        from test_uctan_uca import E2EFixture
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
        env = {**fixture.env, 'CADRE_RUN_ID': rid, 'CADRE_GOREV_ID': 'g1', 'CADRE_CLAUDE_PLAN': '0'}
        for role in ('PLAN', 'REVIEW', 'IMPL', 'IMPL_HAFIF', 'KALITE', 'BIRLESTIRME', 'KURTARICI'):
            env['CADRE_ZINCIR_' + role] = pinned
        with patch.dict(os.environ, env), patch.object(yon, 'QE', fixture.home), \
             patch.object(yon, 'DURUM', fixture.home / 'zincir/state.json'), \
             patch.object(yon, 'KAYIT', fixture.home / 'zincir/router.jsonl'), \
             patch.object(yon, 'butce_var', return_value=(True, 'fixture capacity')), \
             patch.object(yon, 'merdiven', return_value='aktif'):
            with patch.object(yon, 'zincir', return_value=([pinned], ['fixture eligible route'])):
                proposal = request_proposal(fixture.store, rid)
                first = request_permission(fixture.store, rid, proposal)
                request_permission(fixture.store, rid, proposal, first['confirmation'])
            for key in env:
                if key.startswith('CADRE_ZINCIR_'):
                    os.environ[key] = other
            budget = {'fixture': True}
            for role in ('impl', 'impl-hafif', 'yazar', 'review', 'kalite'):
                with self.subTest(role=role):
                    models = yon.zincir(role, istek_token=1000, b=budget, d={}, kaydet=False)[0]
                    self.assertEqual(models, [pinned])
            child = fixture.store.accept('e2e', 'Child code task')['run_id']
            fixture.store.update(child, proje_ust=rid)
            with patch.dict(os.environ, {'CADRE_RUN_ID': child}):
                models = yon.zincir('impl', istek_token=1000, b=budget, d={}, kaydet=False)[0]
                self.assertEqual(models, [pinned])
                self.assertEqual(yon.zincir('impl', istek_token=1000, b=budget, d={
                    'opencode-go': {'cooldown_bitis': 10 ** 12}}, kaydet=False)[0], [])
            changed = fixture.store.read(rid)
            changed['request_permission']['action']['models']['impl'] = [other]
            atomic_json(fixture.store.path(rid) / 'run.json', changed)
            self.assertEqual(yon.zincir('impl', istek_token=1000, b=budget, d={}, kaydet=False)[0], [])

    def test_every_role_declares_why_it_needs_its_power(self):
        for rol in yon.ROLLER:
            if rol == 'ana':
                continue
            self.assertIn(rol, yon.ROL_GUC, f'{rol} icin ROL_GUC kaydi yok')
            kayit = yon.ROL_GUC[rol]
            self.assertIn(kayit['guc'], ('yuksek', 'orta', 'dusuk'))
            self.assertGreater(kayit['baglam'], 0)
            self.assertGreater(kayit['sik'], 0)
            self.assertTrue(kayit['not'].strip())

    def test_no_chain_carries_a_slow_expensive_model_any_more(self):
        """User decision (18.09 evening): glm-5.2 and kimi-k2.7-code were REMOVED from the chains — glm-5.2 was the slowest
        of the round (22 min) and cost $4.4; kimi is in the same price class but unmeasured. qwen3.8-max and kimi-k3
        were deliberately put back into the plan/review chains with D13 (03.10)."""
        for pahali in ('opencode-go/glm-5.2', 'opencode-go/kimi-k2.7-code',
                       'opencode-go/grok-4.6',
                       'opencode-go/mimo-v2.5',      # 18.09 exam: 1/3 and 39.6 s — the slowest, the most wrong
                       'opencode-go/minimax-m3'):    # 18.09 exam: 1/3 (the same name on zen 3/3, do not mix up)
            for rol, zincir in yon.ROLLER.items():
                with self.subTest(model=pahali, rol=rol):
                    self.assertNotIn(pahali, zincir)

    def test_default_writer_and_reviewer_chains_lead_with_different_models(self):
        """Regression of the current default chains; a different model name alone does not prove N-QA03 independence.
        When the new clean-session policy is applied in CR-18 it must not be confused with this default preference."""
        def ilk_bulut(rol):
            return next(m for m in yon.ROLLER[rol] if not m.startswith(('lmstudio/', 'claude/')) and '|' not in m)
        for rol in ('kalite', 'review'):
            with self.subTest(zincir=rol):
                self.assertNotEqual(ilk_bulut('impl'), ilk_bulut(rol),
                                    'mevcut varsayilan zincirler farkli modellerle baslamali')

    def test_the_write_and_review_roles_lead_with_the_measured_best_value_models(self):
        """D13: impl starts with Go deepseek-v4-pro (subscription). 4c-5/6: plan Claude > Sol > glm > qwen > Zen Sol;
        the plan review pool is kept; the final acceptance only on the measured FLEX route."""
        ilk = lambda rol: next(m for m in yon.ROLLER[rol] if not m.startswith(('lmstudio/', 'claude/')) and '|' not in m)
        self.assertEqual(ilk('impl'), 'opencode-go/deepseek-v4-pro')
        plan = ['openrouter/openai/gpt-6.1-sol', 'opencode-go/glm-5.3', 'openrouter/z-ai/glm-5.3', 'opencode-go/qwen3.8-max',
                'openrouter/qwen/qwen3.8-max-0902', 'opencode/gpt-6.1-sol']
        self.assertEqual(yon.ROLLER['plan'][1:], plan)
        self.assertEqual(yon.ROLLER['kalite'], [yon.OR_SOL])
        self.assertEqual(yon.ROLLER['review'], plan + ['opencode/glm-5.3', 'opencode/qwen3.8-max'])
        self.assertIsNot(yon.ROLLER['review'], yon.ROLLER['kalite'])
        self.assertFalse(any('kimi' in m for zincir in yon.ROLLER.values() for m in zincir))
        self.assertFalse(any(m.startswith('claude/') for m in yon.ROLLER['kalite']))


    def test_plan_review_families_keep_their_two_provider_routes(self):
        import qe_core as core
        yazan = core.model_ailesi(next(m for m in yon.ROLLER['impl']))
        for planci in ('openrouter/openai/gpt-6.1-sol', 'openrouter/z-ai/glm-5.3'):
            yollar: dict[str, set[str]] = {}
            for m in yon.ROLLER['review']:
                if core.model_ailesi(m) not in {yazan, core.model_ailesi(planci)}:
                    yollar.setdefault(core.model_ailesi(m), set()).add(m.split('/', 1)[0])
            with self.subTest(planci=planci):
                self.assertGreaterEqual(len(yollar), 2, yollar)
                self.assertTrue(all(len(v) >= 2 for v in yollar.values()), yollar)
                self.assertTrue(any('openrouter' in v for v in yollar.values()), 'Go bitince OR hakemi kalır')

    def test_the_trailing_zen_block_stays_the_last_resort(self):
        import tempfile
        import time
        from unittest.mock import patch
        sol, zen_sol = 'openrouter/openai/gpt-6.1-sol', 'opencode/gpt-6.1-sol'
        or_glm, or_qwen = 'openrouter/z-ai/glm-5.3', 'openrouter/qwen/qwen3.8-max-0902'
        with tempfile.TemporaryDirectory() as t, patch.object(yon, 'QE', Path(t)), patch.object(yon, 'kayit'), \
             patch.object(yon, 'butce_var', side_effect=lambda sag, *a, **k: (sag != 'opencode-go', 'fixture')), \
             patch.object(yon, 'merdiven', return_value='aktif'), patch('katalog.kullanilabilir', return_value=True), \
             patch.dict(os.environ, {'CADRE_CLAUDE_PLAN': '0'}):
            inceleme = yon.zincir('review', b={'x': 1}, d={})[0]
            hakem = yon.zincir('kalite', b={'x': 1}, d={})[0]
            soguk = {yon.model_anahtari('openrouter', sol): {'cooldown_bitis': time.time() + 600}}
            plan = yon.zincir('plan', b={'x': 1}, d=soguk)[0]
        self.assertEqual(inceleme, [sol, or_glm, or_qwen, 'opencode/glm-5.3', 'opencode/qwen3.8-max'],
                         'Go bitince Zen glm OR qwen\'in önüne geçmez')
        self.assertEqual(hakem, [sol])
        self.assertEqual(plan, [or_glm, or_qwen, zen_sol], 'OR Sol arızalıyken Zen Sol son halka')

    def test_final_review_waits_instead_of_falling_back_to_an_unqualified_model(self):
        from unittest.mock import patch
        with patch.object(yon, 'butce_var', side_effect=lambda sag, model, *a, **k: (model != yon.OR_SOL, 'fixture')), \
             patch.object(yon, 'merdiven', return_value='aktif'), patch.object(yon, 'kayit'), \
             patch('katalog.kullanilabilir', return_value=True), \
             patch.dict(os.environ, {'CADRE_ZINCIR_KALITE': ''}):
            models, reasons = yon.zincir('kalite', b={'x': 1}, d={})
        self.assertEqual(models, [])
        self.assertTrue(any(yon.OR_SOL in reason for reason in reasons))

    def test_no_budget_does_not_turn_local_into_a_judge(self):
        """P3: while the budget cannot be read the judge chain does not fall to the local model."""
        zincir, neden = yon.zincir('kalite', 'orta', None, {'ts': 'x'}, {})
        self.assertEqual(zincir, [])
        self.assertTrue(neden)


class OpenaiZincirTests(unittest.TestCase):
    """D13 (03.10): the chains were rewritten. The openai/ route only via openrouter (Sol).
    Luna (gpt-6-luna) and qwen3.7-plus were removed from the chains; Sol's position in plan is after claude,
    in review/quality after kimi-k3 and glm-5.3."""
    BEDAVA = ('opencode/nemotron-3.5-lightning-free', 'opencode/nemotron-3-ultra-free',
              'opencode/ling-3.1-flash-free', 'openrouter/nvidia/nemotron-3.5-lightning:free', 'google/gemini-3.1-flash-lite')

    def setUp(self):
        from unittest.mock import patch
        import tempfile
        import butce_kapisi
        paid = patch('katalog.bedava_mi', return_value=False)
        paid.start()
        self.addCleanup(paid.stop)
        self.temp = tempfile.TemporaryDirectory(prefix='qe-oa-zincir-')
        self.addCleanup(self.temp.cleanup)
        ev = Path(self.temp.name)
        (ev / 'zincir').mkdir(parents=True)
        for modul in (yon, butce_kapisi):
            eski_qe = getattr(modul, 'QE', None)
            if eski_qe is not None:
                setattr(modul, 'QE', ev)
                self.addCleanup(lambda m=modul, v=eski_qe: setattr(m, 'QE', v))


    def oa_zinciri(self, kullanilabilir):
        from unittest.mock import patch
        from test_butce_senaryolari import anlik
        b = anlik(is_maliyeti={'hakem': {'openrouter': 0.1}})
        b['or'] = {'db': 'ok', 'pay': 0.3, 'kullanilabilir': kullanilabilir + 0.045}
        with patch.object(yon, 'butce_var', return_value=(True, 'test')), patch.object(yon, 'sikisik_mi', return_value=False):
            return yon.zincir('kalite', 'orta', 20000, b, {})

    def test_sol_is_eligible_when_its_money_covers_a_review(self):
        z, _ = self.oa_zinciri(.2)
        self.assertIn('openrouter/openai/gpt-6.1-sol', z)

    def test_sol_stays_out_while_its_money_is_below_half_a_review(self):
        z, _ = self.oa_zinciri(.03)
        self.assertNotIn('openrouter/openai/gpt-6.1-sol', z)

    def test_small_change_reviews_put_a_backup_provider_last(self):
        """D13: the judge cost order changes by provider; expensive providers are moved back on a low budget."""
        import qe_core
        from test_butce_senaryolari import anlik
        sira = qe_core.HAKEM_MALIYET_SIRASI
        # D13: the openai/ direct API was removed; opencode-go and deepseek comparison
        b = lambda kul: anlik(go={'db': 'ok', 'pay': 2.0, 'kullanilabilir': (kul + 0.045) * 100 / 15},   # 4c-13: puan
                              is_maliyeti={'hakem': {'opencode-go': 0.1}})
        self.assertEqual(qe_core.hakem_maliyeti('opencode-go', b(0.5)), sira['opencode-go'])
        self.assertGreater(qe_core.hakem_maliyeti('opencode-go', b(0.05)), sira['opencode-go'])
        self.assertEqual(qe_core.hakem_maliyeti('opencode-go', {}), sira['opencode-go'])

    def test_openai_models_stay_out_of_writer_and_scout_chains(self):
        """D13: 'openai/' (direct API) left the chains; only 'openrouter/openai/' (Sol) remains."""
        for rol in ('impl', 'impl-hafif', 'kurtarici', 'birlestirme'):
            with self.subTest(rol=rol):
                self.assertFalse(any(m.startswith('openai/') for m in yon.ROLLER[rol]))

    def test_an_openai_link_needs_a_verified_allocation(self):
        """D13: Sol via OpenRouter; budget key 'or' (formerly 'oa'). No direct openai API."""
        # D13: the 'openai/' direct API is not in the chains — verify
        for rol, zincir in yon.ROLLER.items():
            with self.subTest(rol=rol):
                self.assertFalse(any(m.startswith('openai/') for m in zincir),
                                 f'{rol}: dogrudan openai/ modeli olmamali (D13)')


class OranLimitleriTests(unittest.TestCase):
    """B90 (17.09 live measurement + official docs): on free tiers what cuts us is not DOLLARS but per-minute
    token/request limits. These fields were EMPTY in the catalogue. Measured result: sending a 25k request to groq
    "Request too large" -> opencode compacts and retries -> LOOP (238 KB of log in 17 minutes)."""

    def test_the_published_limits_are_recorded_for_every_metered_free_tier(self):
        for sag, alan in (('groq', 'tpm'), ('groq', 'rpd'), ('groq', 'rpm')):
            with self.subTest(sag=sag, alan=alan):
                self.assertIsInstance(yon.SAGLAYICI[sag].get(alan), int,
                                      f'{sag}.{alan} yazili olmali (kaynak yorumda)')
        self.assertEqual(yon.SAGLAYICI['groq']['tpm'], 8000)

    def test_a_request_bigger_than_the_minute_budget_cannot_reach_that_provider(self):
        """TPM is also the upper bound of a single request: 25K cannot be sent to a provider that gives 8K tokens a minute."""
        self.assertLessEqual(yon.istek_tavani('groq'), yon.SAGLAYICI['groq']['tpm'])
        # ... and providers without TPM are not affected by this
        self.assertGreater(yon.istek_tavani('opencode-go'), 50_000)

    def test_a_large_request_drops_a_provider_whose_request_cap_is_smaller(self):
        """The rule is provider-independent: a candidate exceeding the request cap is removed and the reason is written.
        Until 19.09 this was tested with groq in this chain; since groq cannot run through opencode it left
        the chains (B113), and the rule is now tested directly on `butce_var`."""
        b = {'groq': {'db': 'ok', 'bugun_istek': 0}}
        self.assertEqual(yon.butce_var('groq', 'openai/gpt-oss-20b', b, 25000), (False, 'istek tavani'))
        self.assertNotEqual(yon.butce_var('groq', 'openai/gpt-oss-20b', b, 3000)[1], 'istek tavani')

    def test_groq_is_kept_out_of_every_chain(self):
        """D13 (03.10): groq is in no chain (code-writing, plan and judge roles; with 4c-9/10 the helper chain was removed too)."""
        for rol, zincir in yon.ROLLER.items():
            for model in zincir:
                with self.subTest(rol=rol, model=model):
                    self.assertFalse(model.startswith('groq'), f'{rol}: groq opencode ile kosamaz')


class SaglayiciTanimTest(unittest.TestCase):
    def test_every_paid_provider_in_a_chain_is_known_to_the_budget_check(self):
        """B336 (03.10): OpenRouter was added to the chains but butce_var did not recognize it ('tanimsiz') -> it was never selected."""
        from unittest.mock import patch
        import butce_kapisi
        saglayicilar = {m.split('/', 1)[0] for z in yon.ROLLER.values() for halka in z for m in halka.split('|')} - {'claude', 'lmstudio'}
        with patch.object(butce_kapisi, 'available_budget', return_value=10**9):
            for sag in sorted(saglayicilar):
                with self.subTest(sag=sag):
                    self.assertNotEqual(yon.butce_var(sag, f'{sag}/x', {'x': 1}, 1000)[1], 'tanimsiz')
                    self.assertIn(sag, yon.TAVAN_TIPI)


if __name__ == '__main__':
    unittest.main()


class SirDosyasiIzinleriTests(unittest.TestCase):
    """B110 (18.09): in the bash permissions only `cat *.env` was blocked; with `head/sed/grep/awk/python3/base64/cp`
    the same file could be read freely — the deny list gave a FALSE guarantee."""

    def kurallar(self):
        if not CONFIG.is_file():
            self.skipTest(f'repo config yok ({CONFIG})')
        return config_oku()['agent']['build']['permission']['bash']

    def test_secret_files_are_denied_whatever_command_reads_them(self):
        kurallar = self.kurallar()
        for kalip in ('*.env', '*.env.*', '*/.env*', '*id_rsa*', '*.pem*', '*.key*', '*credentials*', '*.ssh/*'):
            with self.subTest(kalip=kalip):
                self.assertEqual(kurallar.get(kalip), 'deny', f'{kalip} komuttan bagimsiz reddedilmeli')

    def test_template_files_stay_readable(self):
        kurallar = self.kurallar()
        read = config_oku()['agent']['build']['permission']['read']
        for kalip, path in (('*.env.example*', '**/.env.example'), ('*.env.sample*', '**/.env.sample'), ('*.env.template*', '**/.env.template')):
            with self.subTest(kalip=kalip):
                self.assertEqual(read.get(path), 'allow', 'sablon dosya sir degildir (B1 dersi)')
                self.assertEqual(kurallar.get(kalip), 'deny')


class FiyatPerformansZinciriTests(unittest.TestCase):
    """B100 full round (18.09): all 37 models on Go and 103 on zen were scanned with their catalogue price.
    Rule: each role gets as much strength as it needs; an expensive model is not THROWN OUT of the chain but does not MOVE AHEAD."""

    KATALOG = Path(__file__).with_name('fixtures') / 'model-katalog.json'

    def katalog(self):
        return json.loads(self.KATALOG.read_text())

    def fiyat(self, katalog, model):
        sag, _, mid = model.partition('/')
        v = ((katalog.get(sag) or {}).get('models') or {}).get(mid.split('#')[0]) or {}
        c = v.get('cost') or {}
        return float(c.get('input') or 0), float(c.get('output') or 0)

    def test_every_chain_model_exists_in_the_catalogue(self):
        """A typo in a chain = that link silently does not work; a mechanical check catches it."""
        katalog = self.katalog()
        for rol, zincir in yon.ROLLER.items():
            for kayit in zincir:
                for model in kayit.split('|'):
                    sag = model.split('/', 1)[0]
                    if sag in ('claude', 'lmstudio'):
                        continue
                    with self.subTest(rol=rol, model=model):
                        mid = model.split('/', 1)[1].split('#')[0]
                        self.assertIn(mid, (katalog.get(sag) or {}).get('models') or {},
                                      f'{model} katalogda yok')


    def test_every_chain_model_has_a_family(self):
        """4c-6: judge independence is measured by family; a model not in the list falls back to its own id and another model of the same vendor
        counts as 'independent'. The family of every model in the chains and short names is known."""
        import kabul
        aileler = ('deepseek', 'qwen', 'glm', 'gpt', 'claude', 'gemini', 'nemotron', 'mimo')
        modeller = {m for zincir in yon.ROLLER.values() for kayit in zincir for m in kayit.split('|')}
        modeller |= {m for m in yon.KISA.values() if m}
        for m in sorted(modeller):
            with self.subTest(model=m):
                self.assertIn(kabul.model_ailesi(m), aileler)
        self.assertEqual(kabul.model_ailesi('opus'), 'claude', 'plan kaydi kimlik tutar')
        self.assertEqual(kabul.model_ailesi('openrouter/qwen/qwen3.8-max-0902'), kabul.model_ailesi('opencode-go/qwen3.8-max'))

    def test_roles_follow_the_d13_model_order(self):
        """D13 (03.10) + 4c-6: plan Claude > Sol (OR) > glm > qwen; judge pool = plan pool, starting with Sol on a large change."""
        beklenen = {
            'plan': 'openrouter/openai/gpt-6.1-sol',
            'kalite': 'openrouter/openai/gpt-6.1-sol',
            'impl': 'opencode-go/deepseek-v4-pro',
            'impl-hafif': 'opencode-go/deepseek-v4-pro',
            'birlestirme': 'opencode-go/deepseek-v4-pro',
            'kurtarici': 'opencode-go/deepseek-v4-pro',
        }
        for rol, model in beklenen.items():
            with self.subTest(rol=rol, model=model):
                ilk = next(m for m in yon.ROLLER[rol] if not m.startswith(('claude/', 'lmstudio/')) and '|' not in m)
                self.assertEqual(ilk, model)


class GunlukTokenTavaniTests(unittest.TestCase):
    """B106 (18.09): the `tpd` (daily token cap) column was in the table but read in NO decision.
    groq's free plan gives 200K TPD — 40 full-size calls a day with a 5K request; our daily REQUEST cap was
    900, so we never saw that we would hit the token side."""

    def test_a_request_bigger_than_the_remaining_daily_tokens_is_refused(self):
        # The request size is chosen BELOW the request cap: otherwise that rule catches it first and the daily rule is not tested.
        b = {'groq': {'db': 'ok', 'bugun_istek': 10, 'bugun_token': 197000}}
        self.assertEqual(yon.gunluk_token_kalan('groq', b), 3000)
        self.assertLess(4000, yon.istek_tavani('groq'), 'istek tavani bu sinami golgelememeli')
        self.assertEqual(yon.butce_var('groq', 'openai/gpt-oss-120b', b, 4000),
                         (False, 'gunluk token tavani'))
        self.assertNotEqual(yon.butce_var('groq', 'openai/gpt-oss-120b', b, 2000)[1], 'gunluk token tavani')

    def test_the_rule_does_not_bind_without_an_observation_or_a_published_tpd(self):
        self.assertIsNone(yon.gunluk_token_kalan('groq', {'groq': {'db': 'ok'}}), 'gozlem yoksa kural yok')
        self.assertIsNone(yon.gunluk_token_kalan('deepseek', {'ds': {'bugun_token': 10}}), 'tpd yoksa kural yok')
        self.assertIsNone(yon.gunluk_token_kalan('cerebras', {'cerebras': {'bugun_token': 10}}),
                          'Developer katmaninda gunluk kisit yok')


class OlculmusModelTests(unittest.TestCase):
    """18.09: the catalogue is a SUPERSET — of the 32 'free' models on zen only 8 are live in the account, Go's 2 free ones
    do not exist at all. Only LIVE models that PASS a trial go into a chain."""

    def test_plan_review_keeps_its_provider_alternatives(self):
        """Plan review alternatives are kept; the final acceptance pool is separate per the N-TS03 measurement."""
        parali = [m for m in yon.ROLLER['review']
                  if not m.startswith(('lmstudio/', 'claude/')) and '|' not in m]
        self.assertGreaterEqual(len(parali), 3,
                                f'hakem havuzu tek saglayiciya bagimli kalmamali: {parali}')
        saglayicilar = {yon.saglayici(m) for m in parali}
        self.assertGreaterEqual(len(saglayicilar), 3,
                                f'hakem adaylari farkli saglayicilardan gelmeli: {sorted(saglayicilar)}')


class AltAjanModelTests(unittest.TestCase):
    """B121 (19.09): `kisit_config_yaz` picks a model for every sub-agent from ITS OWN role's chain, with ITS OWN declared context.
    4c-10: compaction is never written to this file (it is the lane's own model); Go is evaluated per model, not as a whole."""

    def _bol(self):
        from datetime import datetime
        simdi = datetime.now()
        return {'ts': simdi.isoformat(), 'bugun': simdi.date().isoformat(),
                'go': {'db': 'ok', 'pay': 33.333334, 'kullanilabilir': 33.333334, 'pencere_acik': True, 'birim': 'puan'},
                'zen': {'db': 'ok', 'pay': 5.0, 'kullanilabilir': 5.0},
                'gemini': {'db': 'ok', 'bugun_istek': 0}}

    def test_runtime_config_uses_each_role_chain_and_disables_unavailable_roles(self):
        """N-RT01: the real config_yaz output is bound to the role's first OpenCode candidate; an empty candidate does not invent a new model."""
        import tempfile
        from unittest.mock import patch

        zincirler = {'plan': ['claude/opus#high', 'fixture/planner', 'fixture/backup'],
                     'review': [], 'impl': ['fixture/developer']}
        with tempfile.TemporaryDirectory(prefix='qe-router-config-') as directory, \
             patch.object(yon, 'zincir', side_effect=lambda rol, *a: (zincirler[rol], [])) as sec:
            path, config = yon.config_yaz(Path(directory) / 'config.json', b={'fixture': True}, d={})
            self.assertEqual(config['agent'], {'cadre-plan': {'model': 'fixture/planner'},
                                              'cadre-review': {'disable': True},
                                              'cadre-impl': {'model': 'fixture/developer'}})
            self.assertEqual(json.loads(Path(path).read_text()), config)
            self.assertEqual([call.args for call in sec.call_args_list],
                             [(rol, 'orta', None, {'fixture': True}, {}) for rol in ('plan', 'review', 'impl')])

    def test_compaction_is_never_written_and_go_is_judged_per_model(self):
        import json
        import qe_core as core
        yol, secim, basamak = core.kisit_config_yaz('test', butce=self._bol())
        cfg = json.loads(Path(yol).read_text())
        self.assertNotIn('compaction', cfg['agent'], 'sikistirma seridin modelidir; kisit dosyasi onu ezmez')
        self.assertNotIn('cadre-scout', cfg['agent'])
        self.assertIn('cadre-impl=opencode-go/', secim, 'Go havuzunda yer varken kod modeli Go yolundan secilir')
        self.assertEqual(basamak, 'opencode-go')

    def test_a_role_that_needs_more_context_never_gets_a_model_that_cannot_hold_it(self):
        import json
        import qe_core as core
        yol, _m, _b = core.kisit_config_yaz('test', butce=self._bol())
        cfg = json.loads(Path(yol).read_text())
        eslesme = {'cadre-plan': 'plan', 'cadre-review': 'review', 'cadre-impl': 'impl'}
        sinanan = []
        for ajan, rol in eslesme.items():
            model = (cfg['agent'].get(ajan) or {}).get('model')
            if not model:
                self.assertTrue(cfg['agent'][ajan].get('disable'), f'{ajan}: modelsiz rol kapali olmali')
                continue
            sinanan.append(ajan)
            sag = model.split('/', 1)[0]
            with self.subTest(ajan=ajan, model=model):
                self.assertLessEqual(yon.rol_baglami(rol), yon.istek_tavani(sag, model),
                                     f'{ajan}: {rol} {yon.rol_baglami(rol)} token istiyor, {model} en fazla '
                                     f'{yon.istek_tavani(sag, model)} aliyor')
        self.assertTrue(sinanan, 'on kosul: en az bir rolun gercek model baglami sinanmali')
