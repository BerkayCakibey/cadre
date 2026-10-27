"""Night scenarios updated with the 03.10 decisions — fake data, real decision code (no network/model/Windows):
S1  compaction/sub-agent: cloud if there is budget, otherwise the helper closes; the decision is made on EVERY executor call (opencode_budget_env).
S2  GO1: if the Go share runs out IN THE MIDDLE of a job the in-flight lane is NOT CUT (it runs to the end); the brake only stops a new push.
S3  P3: when the Go share runs out the main model does NOT FALL to local — a budget-wait denial at the gate; when the budget returns the next call is cloud again.
    B93: when the budget observation cannot be read the engine goes to a budget wait, not to local (retryable); the read error is logged as 'okunamadi'.
Every test measures a behaviour; no source-text grep."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import test_kosu_sessions as fixtures  # noqa: E402
import test_uctan_uca as e2e  # noqa: E402
import qe_core as core  # noqa: E402
from test_gece_bekleme import full_budget  # noqa: E402

GO_IMPL, GO_KUCUK, DS, GEMINI = 'opencode-go/kimi-k2.7-code', 'opencode-go/glm-5.3-flash', 'opencode/deepseek-v4-flash', 'google/gemini-3-flash-preview'
# 4c-1: DS = the second paid route (DeepSeek flash on Zen); the direct DeepSeek provider was removed.
HAKEMLER = ['opencode-go/glm-5.3', 'opencode-go/qwen3.8-max']   # P6: two families independent of the writer families (kimi, deepseek)


def zincir_sahte(rol, *_a, **_k):
    if rol == 'kalite':   # review (an in-lane helper) uses the writer chain; the ban test looks at it
        return list(HAKEMLER), []
    return [GO_IMPL, DS], []


def butce(go=5.0, zen=5.0, gemini=450):
    b = full_budget(cloud=True)
    b['go'].update(kullanilabilir=go * 100 / 15, oran=go / 5.0)   # 4c-13: Go points; the parameter is the dollars of the $15 model
    b['zen'].update(kullanilabilir=zen, kalan=zen, oran=zen / 5.0)
    b['gemini'].update(kalan_istek=gemini, bugun_istek=500 - gemini, oran=gemini / 500)
    if gemini == 0:
        b['groq'].update(bugun_istek=900, oran=0.0)
    return b


class ConfigPerCallTests(unittest.TestCase):
    """S1 + S3: opencode_budget_env reads the budget on every call; compaction/sub-agent and the main model are chosen accordingly."""

    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.enter(patch.object(core, 'project_memory', return_value=[]))
        self.f.enter(patch.object(core.YON, 'zincir', side_effect=zincir_sahte))

    def config_for(self, b, model_arg):
        self.f.enter(patch.object(core, 'butce_json', return_value=b))
        env = {'OPENCODE_CONFIG': '/nonexistent'}
        args = core.opencode_budget_env(env, ['-m', model_arg, 'gorev metni'])
        cfg = json.loads(env['OPENCODE_CONFIG_CONTENT'])
        return args, cfg


    def test_compaction_follows_the_session_model_when_no_helper_candidate_fits(self):
        """A disabled compaction agent is DELETED from the opencode registry; when compaction runs the agent is not found and
        the session fails. Without a candidate, compaction is written to the session model that already holds the context."""
        def zincir_compaction_bos(rol, *_a, **_k):
            return ([], []) if rol == 'compaction' else zincir_sahte(rol)
        self.f.enter(patch.object(core.YON, 'zincir', side_effect=zincir_compaction_bos))
        args, cfg = self.config_for(butce(), GO_IMPL)
        self.assertEqual(args[args.index('-m') + 1], GO_IMPL)
        compaction = cfg['agent']['compaction']
        self.assertEqual((compaction['model'], compaction['disable']), (GO_IMPL, False))
        self.assertEqual(compaction['permission']['bash'], 'allow')
        self.assertEqual(compaction['permission']['read'], 'deny')


    def test_s3_main_model_follows_the_budget_call_by_call(self):
        args, _ = self.config_for(butce(), GO_IMPL)
        self.assertIn(GO_IMPL, args, 'butce var: bulut serit bulutta baslar')
        # P3 (03.10): Go share exhausted -> the main model does NOT FALL to local; a budget-wait denial at the gate (the model identity is kept).
        with self.assertRaises(core.BudgetDenied):
            self.config_for(butce(go=0.0), GO_IMPL)
        args, _ = self.config_for(butce(), GO_IMPL)
        self.assertIn(GO_IMPL, args, 'butce geri geldi (yeni gun/reset): sonraki cagri yine bulut')


class MidRunBudgetHandoverTests(unittest.TestCase):
    """S2: the Go share runs out while a cloud lane runs -> brake, controlled interruption, next provider, same session, the part is kept."""

    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_s2_go_budget_exhausted_mid_lane_does_not_cut_the_inflight_lane(self):
        home, wt = self.f.home, self.f.wt
        dolu, bitti = butce(), butce(go=0.0)
        self.f.enter(patch.object(core, 'butce_json', side_effect=lambda: bitti if (home / 'calls.jsonl').exists() else dolu))
        self.f.enter(patch.object(core, 'project_memory', return_value=[]))
        self.f.enter(patch.object(core.YON, 'zincir', side_effect=zincir_sahte))
        self.f.fake.write_text(self.f.fake.read_text() + f"""
import time
n = sum(1 for _ in (p/'calls.jsonl').open())
if n == 1:
    pathlib.Path({str(wt)!r}, 'parca.txt').write_text('bulut seridin yarim isi')
    time.sleep(3)   # GO1: ucustaki serit KESILMEZ; kesilseydi CADRE_PROGRESS_POLL=1 tikinde kesilir ve ikinci cagri yazilirdi
    raise SystemExit(0)
""")
        rc, _gate = self.f.motor_fixture(env={'CADRE_RUN_MODEL': GO_IMPL, 'CADRE_MAX_RESUME': '3', 'CADRE_PROGRESS_POLL': '1'})
        calls = self.f.calls()
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(calls[0][calls[0].index('-m') + 1], GO_IMPL, 'serit bulutta basladi')
        self.assertEqual((wt / 'parca.txt').read_text(), 'bulut seridin yarim isi')
        self.assertEqual(self.f.store.read(self.f.rid)['tasks']['g1']['state'], 'uretti')
        log = (self.f.store.task_dir(self.f.rid, 'g1') / 'motor.log').read_text()
        self.assertNotIn('GO-KISIT', log, 'GO1: is ortasinda Go payi bitse de ucustaki serit kesilmez; fren yalniz yeni itisi durdurur')


class ButceGozlemiGeriGelirTests(unittest.TestCase):
    """B93 (18.09 live): when the budget observation could not be read the restriction ladder fell to local WITHOUT looking at the chain and this decision
    stayed PERMANENT FOR THE WHOLE RUN. P3 (03.10): an unreadable budget no longer DROPS to local — the engine leaves the job partial with a budget wait
    (the coordinator marks it retryable, the same run continues when the budget returns). Fail-closed is right, being PERMANENT is not."""

    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.enter(patch.object(core, 'project_memory', return_value=[]))
        self.f.enter(patch.object(core.YON, 'zincir', side_effect=zincir_sahte))

    def test_unreadable_budget_marks_lane_budget_wait_not_local(self):
        home, wt = self.f.home, self.f.wt
        bayat = butce()
        bayat['ts'] = '2020-01-01T00:00:00'      # stale observation -> allowance 'stale budget' -> step 'butce-yok'
        taze = butce()
        self.f.enter(patch.object(core, 'butce_json',
                                  side_effect=lambda: taze if (home / 'calls.jsonl').exists() else bayat))
        rc, _gate = self.f.motor_fixture(env={'CADRE_MAX_RESUME': '3', 'CADRE_PROGRESS_POLL': '1',
                                              'CADRE_YUKSELME_TUR': '1'})
        log = (self.f.store.task_dir(self.f.rid, 'g1') / 'motor.log').read_text()
        # B94 + B131: the message must say "unreadable", it must NOT say "share exhausted". The MEANING is tested.
        self.assertIn('okunamadi', log, 'B94: is basi mesaji okuma hatasini yazmali')
        self.assertNotIn('gunluk Go payi bitti', log,
                         'B131: okuma hatasi "pay bitti" diye raporlanamaz — yanlis teshis')
        # P3: an unreadable budget does not fall to local — it is left partial with a budget wait (retryable, not permanent).
        self.assertIn('BUTCE BEKLEME', log, 'P3: butce gozlemi yok -> yerele degil butce-beklemeye')
        task = self.f.store.read(self.f.rid)['tasks']['g1']
        self.assertEqual((task['state'], task.get('reason')), ('yarim', 'butce-bekleme'))
        self.assertFalse((self.f.home / 'calls.jsonl').exists(), 'butce gozlemi okunamazken yurutucu baslamaz')


if __name__ == '__main__':
    unittest.main()
