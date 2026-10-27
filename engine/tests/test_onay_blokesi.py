"""G-C13 (23.09 live T1 finding): the briefing bound all items to an "ONAY:" decision, the executor marked all of them `BLOKE: onay`
but kept editing the product for 40 min and breaking the same test; the guard had no exit path (since it produced artefacts
"guard cut" did not trigger either). Contract: if no workable item remains in `.gorevler.json` the run is CUT, the state is
`yarim onay-bekliyor: <question>`, the coordinator does not retry, no delivery; the decision falls to the user. No model/network."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import qe_core as core  # noqa: E402
import kosu  # noqa: E402

SORU = "ONAY: --version çıktısı 'package_version()' mi yoksa 'feedloom <version>' mı olacak?"


def _madde(mid, baslik, status):
    return {"id": mid, "baslik": baslik, "kaynak": "x", "kabul": "y", "kapsam_disi": "", "bagimli": "", "status": status, "passes": False}


def _yaz(wt, maddeler):
    (Path(wt) / ".gorevler.json").write_text(json.dumps(maddeler, ensure_ascii=False), encoding="utf-8")


class OnayBlokesiTest(unittest.TestCase):
    def setUp(self):
        self.wt = tempfile.mkdtemp(prefix="onay-bloke-")

    def test_tum_maddeler_bloke_ise_onay_sorusu_doner(self):
        _yaz(self.wt, [_madde("v01", SORU, "BLOKE: onay"), _madde("v02", "cli.py duzenle", "BLOKE: onay (v01)")])
        self.assertEqual(core.onay_blokesi(self.wt), SORU)

    def test_yapilabilir_madde_varsa_bloke_yok(self):
        _yaz(self.wt, [_madde("v01", SORU, "BLOKE: onay"), _madde("v02", "test yaz", "todo")])
        self.assertEqual(core.onay_blokesi(self.wt), "")

    def test_dosya_yok_bozuk_veya_bos_liste_bloke_yok(self):
        self.assertEqual(core.onay_blokesi(self.wt), "")
        (Path(self.wt) / ".gorevler.json").write_text("{bozuk", encoding="utf-8")
        self.assertEqual(core.onay_blokesi(self.wt), "")
        _yaz(self.wt, [])
        self.assertEqual(core.onay_blokesi(self.wt), "")

    def test_bekci_tik_onay_blokesinde_isciyi_keser(self):
        _yaz(self.wt, [_madde("v01", SORU, "BLOKE: onay"), _madde("v02", "uygula", "BLOKE: onay (v01)")])
        logf = Path(self.wt) / "motor.log"
        logf.write_text("")
        proc = subprocess.Popen(["sleep", "30"])
        try:
            with patch.object(core, "cancel_path", return_value=Path(self.wt) / "yok.iptal"):
                guard = core.StallGuard(str(logf), proc, 9999, wt=self.wt)
                self.assertEqual(guard.tik("sahte-proje"), "onay")
            self.assertEqual(guard.onay_sorusu, SORU)
            self.assertIsNotNone(proc.wait(timeout=5), "isci kesilmedi")
            self.assertIn("ONAY-BLOKE", logf.read_text())
            self.assertFalse(guard.kesti, "onay kesmesi stall sayilmaz (yardimci politikasi tetiklenmez)")
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)  # B142b: the Popen object must not be collected with an open child (ResourceWarning)

    def test_bekci_tik_bloke_yokken_eski_yolu_izler(self):
        _yaz(self.wt, [_madde("v01", SORU, "BLOKE: onay"), _madde("v02", "uygula", "yapiliyor")])
        logf = Path(self.wt) / "motor.log"
        logf.write_text("")
        proc = subprocess.Popen(["sleep", "30"])
        try:
            with patch.object(core, "cancel_path", return_value=Path(self.wt) / "yok.iptal"):
                guard = core.StallGuard(str(logf), proc, 9999, wt=self.wt)
                self.assertIsNone(guard.tik("sahte-proje"))
            self.assertIsNone(proc.poll(), "bloke yokken isci kesilmemeli")
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_koordinator_onay_bekliyor_nedenini_kalici_sayar(self):
        # kosu.py:624 — `reason.split(':')[0] in KALICI_NEDENLER` -> failed (no retry)
        self.assertIn("onay-bekliyor", kosu.KALICI_NEDENLER)

BRIFING = """# BRIFING (motor on-plan) — model: x

## MADDELER

```json
[
  {"id": "G01", "baslik": "cli.py duzenle", "bagimli": "G05"},
  {"id": "G05", "baslik": "ONAY: mevcut test cakismasi — cikti bicimi?", "bagimli": ""}
]
```

## TUZAKLAR
"""


class BrifingOnayTest(unittest.TestCase):
    """T1b (23.09 02:28): the executor stopped for approval but did not write BLOKE to `.gorevler.json` and exited with rc=0; the run
    went to QA with an empty product (FAIL rc=2). Second trigger: rc=0 + product unchanged + an ONAY item in the briefing -> onay-bekliyor."""

    def setUp(self):
        self.wt = tempfile.mkdtemp(prefix="onay-brifing-")
        self.bp = Path(self.wt) / "plan" / "brifing-motor.md"
        self.bp.parent.mkdir()

    def test_brifing_onay_sorusu_maddelerden_okunur(self):
        self.bp.write_text(BRIFING, encoding="utf-8")
        self.assertEqual(core.brifing_onay_sorusu(self.bp), "ONAY: mevcut test cakismasi — cikti bicimi?")

    def test_brifing_onay_yoksa_veya_dosya_yoksa_bos(self):
        self.assertEqual(core.brifing_onay_sorusu(self.bp), "")
        self.bp.write_text(BRIFING.replace("ONAY: ", "KARAR VERILDI: "), encoding="utf-8")
        self.assertEqual(core.brifing_onay_sorusu(self.bp), "")
        self.bp.write_text("## MADDELER\n```json\n[bozuk\n```", encoding="utf-8")
        self.assertEqual(core.brifing_onay_sorusu(self.bp), "")

    def test_onay_karari_urun_degismemis_ve_brifingde_onay_varsa_soru(self):
        self.bp.write_text(BRIFING, encoding="utf-8")
        self.assertEqual(core.onay_karari(self.wt, self.bp, urun_degisti=False), "ONAY: mevcut test cakismasi — cikti bicimi?")

    def test_onay_karari_urun_degistiyse_brifing_tek_basina_yetmez(self):
        self.bp.write_text(BRIFING, encoding="utf-8")
        self.assertEqual(core.onay_karari(self.wt, self.bp, urun_degisti=True), "")

    def test_onay_karari_json_blokesi_urun_degisse_de_gecerli(self):
        _yaz(self.wt, [_madde("v01", SORU, "BLOKE: onay"), _madde("v02", "uygula", "BLOKE: onay (v01)")])
        self.assertEqual(core.onay_karari(self.wt, self.bp, urun_degisti=True), SORU)


class OnayYanlisPozitifTest(unittest.TestCase):
    """T1v3 (23.09 04:21): the briefing headers were "ONAY: yok | <test>" (meaning NO approval is needed) and the agent committed the product
    in the worktree; `startswith("ONAY")` + a clean `git status` -> a wrong 'onay-bekliyor', the right product could not reach the acceptance gate.
    Contract: "ONAY: yok/gerekmez/..." is not an approval item; the product change is measured against the SOURCE SHA (committed changes included)."""

    def setUp(self):
        self.wt = tempfile.mkdtemp(prefix="onay-yp-")
        self.bp = Path(tempfile.mkdtemp(prefix="onay-yp-run-")) / "plan" / "brifing-motor.md"  # the briefing is in the run directory, not in the worktree
        self.bp.parent.mkdir()
        g = lambda *a: subprocess.run(["git", "-C", self.wt, "-c", "user.email=t@t", "-c", "user.name=t", *a], check=True, capture_output=True, text=True)
        g("init", "-q"); (Path(self.wt) / "a.py").write_text("x = 1\n"); g("add", "a.py"); g("commit", "-qm", "taban")
        self.kaynak = g("rev-parse", "HEAD").stdout.strip()
        self.g = g

    def test_onay_yok_basligi_onay_maddesi_degildir(self):
        self.bp.write_text(BRIFING.replace('"ONAY: mevcut test cakismasi — cikti bicimi?"', '"ONAY: yok | test_x (kabul a)"'), encoding="utf-8")
        self.assertEqual(core.brifing_onay_sorusu(self.bp), "")
        for olumsuz in ("ONAY: gerekmez — x", "ONAY: hayır", "ONAY: gereksiz (a)"):
            self.bp.write_text(BRIFING.replace("ONAY: mevcut test cakismasi — cikti bicimi?", olumsuz), encoding="utf-8")
            self.assertEqual(core.brifing_onay_sorusu(self.bp), "", olumsuz)

    def test_bloke_yok_durumu_bloke_degildir(self):
        _yaz(self.wt, [_madde("v01", "x", "BLOKE: yok"), _madde("v02", "y", "BLOKE: yok")])
        self.assertEqual(core.onay_blokesi(self.wt), "")

    def test_commitli_urun_degisikligi_kaynak_shaya_gore_degisiklik_sayilir(self):
        self.bp.write_text(BRIFING, encoding="utf-8")
        (Path(self.wt) / "b.py").write_text("y = 2\n"); self.g("add", "b.py"); self.g("commit", "-qm", "ajan commit'i")
        (Path(self.wt) / ".gorevler.json").write_text("[]"); (Path(self.wt) / ".cadre-plan").mkdir(); (Path(self.wt) / ".cadre-plan" / "p.md").write_text("plan")
        self.assertEqual(core.onay_karari(self.wt, self.bp, source_sha=self.kaynak), "", "commit'li urun = degisiklik; onay degil")

    def test_yalniz_harness_dosyalari_varsa_urun_degismemis_sayilir(self):
        self.bp.write_text(BRIFING, encoding="utf-8")
        (Path(self.wt) / ".gorevler.json").write_text("[]"); (Path(self.wt) / ".cadre-plan").mkdir(); (Path(self.wt) / ".cadre-plan" / "p.md").write_text("plan")
        self.assertEqual(core.onay_karari(self.wt, self.bp, source_sha=self.kaynak), "ONAY: mevcut test cakismasi — cikti bicimi?")


if __name__ == "__main__":
    unittest.main()
