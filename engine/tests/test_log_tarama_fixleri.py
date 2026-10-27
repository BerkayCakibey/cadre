"""23.09 morning static log scan — four defects from the logs of 10 runs:
B169 the same denial was recorded twice (one without a provider, 'plugin refusal'); if only the provider-less line remains the engine bans all providers
(T2 also had the line with a provider, only zen was banned — the 16:20 audit fix); B177 a Claude budget denial was logged as "insufficient output" on the plan path
and triggered the Claude breaker, and was mislabelled "executor failed" on the judge path; B178 the reason of a run ending with a missing lane was `rc=1`; B179 a series of attempts
failing with rc≠0 at 15 s intervals (zen 404 helper model) used up the 12-attempt limit in 3 min, and the error-loop detector did not see it because it counted per attempt.
No model/network."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import qe_core as core  # noqa: E402
import kosu  # noqa: E402
import butce_kapisi  # noqa: E402


class ButceReddiMetniTest(unittest.TestCase):  # B177
    def test_claude_403_metni_butce_reddidir(self):
        self.assertTrue(core.butce_reddi_metni("Failed to authenticate. API Error: 403 CADRE_BUDGET_DENIED"))
        self.assertTrue(core.butce_reddi_metni('{"result": "CADRE_BUDGET_DENIED: no verified request allowance"}'))

    def test_normal_cikti_ve_bos_red_degildir(self):
        self.assertFalse(core.butce_reddi_metni("## MADDELER\n```json\n[]\n```"))
        self.assertFalse(core.butce_reddi_metni(""))
        self.assertFalse(core.butce_reddi_metni(None))


class HizliDususTest(unittest.TestCase):  # B179
    def test_uc_hizli_dusus_seri_sayar_ve_ilk_baslangici_korur(self):
        hd = core.HizliDusus(esik_sn=60, esik=3)
        self.assertEqual(hd.kaydet(rc=1, sure_sn=15, baslangic=100.0), 1)
        self.assertEqual(hd.kaydet(rc=1, sure_sn=15, baslangic=130.0), 2)
        self.assertEqual(hd.kaydet(rc=1, sure_sn=16, baslangic=160.0), 3)
        self.assertTrue(hd.dongu)
        self.assertEqual(hd.seri_baslangic, 100.0)

    def test_basarili_veya_uzun_deneme_seriyi_sifirlar(self):
        hd = core.HizliDusus(esik_sn=60, esik=3)
        hd.kaydet(rc=1, sure_sn=10, baslangic=1.0); hd.kaydet(rc=1, sure_sn=10, baslangic=20.0)
        self.assertEqual(hd.kaydet(rc=1, sure_sn=600, baslangic=40.0), 0, "uzun suren deneme hizli dusus degildir")
        hd.kaydet(rc=1, sure_sn=10, baslangic=700.0)
        self.assertEqual(hd.kaydet(rc=0, sure_sn=5, baslangic=720.0), 0)
        self.assertFalse(hd.dongu)


class SeritSonucuTest(unittest.TestCase):  # B178
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="serit-"))
        self.store = kosu.RunStore(self.home)
        (self.home / "projects" / "p").mkdir(parents=True)
        self.rid = self.store.accept("p", "gorev")["run_id"]

    def test_uretmeyen_serit_yapisal_neden_yazar(self):
        kosu.serit_sonucu_kaydet(self.store, self.rid, "g2", "basarisiz")
        r = self.store.read(self.rid)
        self.assertEqual(r["tasks"]["g2"]["dispatch_result"], "basarisiz")
        self.assertEqual(r.get("reason"), "serit-basarisiz: g2 (basarisiz)")

    def test_ureten_serit_nedeni_degistirmez_ve_rc_nedeni_ezilir(self):
        kosu.serit_sonucu_kaydet(self.store, self.rid, "g1", "uretti")
        self.assertFalse(self.store.read(self.rid).get("reason"))
        self.store.update(self.rid, reason="rc=1")
        kosu.serit_sonucu_kaydet(self.store, self.rid, "g4", "bloke")
        self.assertEqual(self.store.read(self.rid).get("reason"), "serit-basarisiz: g4 (bloke)")
        kosu.serit_sonucu_kaydet(self.store, self.rid, "g5", "basarisiz")
        self.assertEqual(self.store.read(self.rid).get("reason"), "serit-basarisiz: g4 (bloke), g5 (basarisiz)")


class RedTekilTest(unittest.TestCase):  # B169 (ledger side)
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="red-"))
        self.store = kosu.RunStore(self.home)
        (self.home / "projects" / "p").mkdir(parents=True)
        self.rid = self.store.accept("p", "gorev")["run_id"]
        os.environ["CADRE_RUN_ID"] = self.rid

    def tearDown(self):
        os.environ.pop("CADRE_RUN_ID", None)

    def test_ayni_saglayicinin_ayni_saniyedeki_ikinci_reddi_tek_kayit(self):
        butce_kapisi.note_denial(self.home, "deepseek", reason="allocation exhausted")
        butce_kapisi.note_denial(self.home, "deepseek", reason="ledger refused or unavailable")
        rows = self.store.read(self.rid).get("denials") or []
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["reason"], "allocation exhausted")

    def test_farkli_saglayici_ayri_kayittir(self):
        butce_kapisi.note_denial(self.home, "deepseek", reason="x")
        butce_kapisi.note_denial(self.home, "opencode", reason="y")
        self.assertEqual(len(self.store.read(self.rid).get("denials") or []), 2)


if __name__ == "__main__":
    unittest.main()
