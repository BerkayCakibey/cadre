"""G-C14 (23.09 live T1v2 finding): the feedloom worktree's `.venv` did not contain the optional `browser` extra (playwright);
`basedpyright` went red independently of the product, and the system blamed the product and started a local repair attempt. Contract:
(1) if `projects/<p>/kurulum-komut` exists it runs once in the worktree before the run starts; on failure the run stops with `kurulum-hatasi`
(permanent: no retry). (2) The QA command runs once while there is no product; its result is recorded as INFORMATION (the baseline may be red —
end-to-end acceptance commands are red before the product), a warning is added to the repair prompt; it does not stop the run. Both once per run
(marker file). No model/network."""

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


class TabanHazirlikTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="taban-"))
        self.wt = self.d / "wt"
        self.wt.mkdir()
        subprocess.run(["git", "-C", str(self.wt), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.wt), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "bos"], check=True)
        self.proje_dir = self.d / "projects" / "p"
        self.proje_dir.mkdir(parents=True)
        self.logf = self.d / "motor.log"
        self.logf.write_text("")
        self.isaret = self.d / "taban-qa.json"
        self.store = kosu.RunStore(self.d)
        self.rid = self.store.accept('p', 'fixture baseline')['run_id']
        self.store.update(self.rid, worktree=str(self.wt))
        environment = patch.dict(os.environ, {**self.store.environment(self.rid), 'CADRE_ENGINE_ROOT': str(LIB.parent)})
        environment.start()
        self.addCleanup(environment.stop)
        home = patch.object(core, 'QE', self.d)
        home.start()
        self.addCleanup(home.stop)

    def authorize_setup(self):
        import izin
        action = izin.setup_proposal(self.store, self.rid, self.wt, (self.proje_dir / 'kurulum-komut').read_text().strip())
        with patch.dict(os.environ, CADRE_RUN_ID=''):
            izin.approve(self.store, self.rid, action['token'])

    def hazirla(self, kurulum=None, qa=None):
        if kurulum is not None:
            (self.proje_dir / "kurulum-komut").write_text(kurulum + "\n")
        if qa is not None:
            (self.proje_dir / "qa-komut").write_text(qa + "\n")
        if kurulum is not None:
            self.authorize_setup()
        return core.taban_hazirlik("p", self.wt, self.logf, self.isaret, self.proje_dir)

    def test_kurulum_kosar_ve_taban_qa_kirmizisi_bilgi_olarak_kaydedilir(self):
        r = self.hazirla(kurulum="echo kurulum > kurulum.txt", qa="exit 2")
        self.assertEqual(r["kurulum"]["status"], "PASS")
        self.assertTrue((self.wt / "kurulum.txt").exists())
        self.assertEqual((r["qa"]["status"], r["qa"]["exit_code"]), ("FAIL", 2))
        self.assertEqual(r["status"], "PASS", "taban QA kirmizisi kosuyu DURDURMAZ (kabul komutu urun oncesi kirmizi olabilir)")
        self.assertEqual(json.loads(self.isaret.read_text())["qa"]["status"], "FAIL")
        self.assertIn("TABAN QA: FAIL", self.logf.read_text())
        from unittest.mock import patch
        from izle import Izleyici
        store = kosu.RunStore(self.d)
        parent = store.accept('parent', 'project')['run_id']
        rid = store.accept('p', 'feature')['run_id']
        store.update(rid, worktree=str(self.wt), proje_ust=parent)
        (self.wt / 'uv.lock').touch()
        kosu.taban_qa_kaydet(store, rid, r)
        for project in ('p', 'parent'):
            self.assertIn('TABAN QA KIRMIZI', kosu.ozet(store, project))
            self.assertIn('uv lock', kosu.ozet(store, project))
        izleyici = Izleyici('p', str(self.wt), duz=True, tek=True, run_id=rid)
        with patch.object(izleyici, 'yaz') as yaz:
            izleyici.taban_uyar(store.read(rid))
            izleyici.taban_uyar(store.read(rid))
            self.assertEqual(yaz.call_count, 1, 'ayni uyari her turda log yagdirmaz')
        self.assertEqual(store.read(rid)['baseline_warning']['exit_code'], 2)
        kosu.taban_qa_kaydet(store, rid, {'qa': {'status': 'PASS'}})
        self.assertNotIn('TABAN QA KIRMIZI', kosu.ozet(store, 'parent'))

    def test_kurulum_hatasi_kosuyu_durdurur_ve_qa_kosmaz(self):
        r = self.hazirla(kurulum="exit 3", qa="echo QA-KOSTU > qa.txt")
        self.assertEqual((r["status"], r["reason"]), ("FAIL", "kurulum-hatasi"))
        self.assertEqual(r["kurulum"]["exit_code"], 3)
        self.assertFalse((self.wt / "qa.txt").exists())
        self.assertIn("kurulum-hatasi", kosu.KALICI_NEDENLER)

    def test_kurulum_hatasi_yeni_onayla_yeniden_denenir_onaysiz_bekler(self):
        import izin
        calls = []

        def sahte_komut(wt, komut, cikti, zaman_asimi):
            calls.append(komut)
            Path(cikti).write_text("fixture\n")
            return 7 if len(calls) == 1 else 0
        (self.proje_dir / "kurulum-komut").write_text("make install\n")
        self.authorize_setup()
        with patch.object(core, "_taban_komut", side_effect=sahte_komut):
            r = core.taban_hazirlik("p", self.wt, self.logf, self.isaret, self.proje_dir)
            self.assertEqual((r["status"], r["reason"], len(calls)), ("FAIL", "kurulum-hatasi", 1))
            again = core.taban_hazirlik("p", self.wt, self.logf, self.isaret, self.proje_dir)
            self.assertEqual((again["status"], again["reason"], len(calls)), ("FAIL", "kurulum-hatasi", 1))
            self.assertEqual(again["kurulum"]["retry"]["status"], "pending")
            token = again["kurulum"]["retry"]["action_token"]
            self.assertEqual(izin.read_action(self.store, self.rid, token)["identity"]["generation"], 2)
            self.assertIn("yeniden deneme onay bekliyor", self.logf.read_text())
            with patch.dict(os.environ, CADRE_RUN_ID=""):
                izin.approve(self.store, self.rid, token)
            fixed = core.taban_hazirlik("p", self.wt, self.logf, self.isaret, self.proje_dir)
        self.assertEqual((fixed["status"], fixed["kurulum"]["status"], len(calls)), ("PASS", "PASS", 2))
        self.assertEqual(json.loads(self.isaret.read_text())["kurulum"]["status"], "PASS")
        self.assertEqual(izin.read_action(self.store, self.rid, token)["status"], "completed")

    def test_komut_yoksa_atlanir(self):
        r = self.hazirla()
        self.assertEqual((r["kurulum"]["status"], r["qa"]["status"], r["status"]), ("SKIPPED", "SKIPPED", "PASS"))

    def test_kosu_basina_bir_kez_isaret_dosyasi(self):
        self.hazirla(kurulum="echo x >> sayac.txt", qa="exit 0")
        self.hazirla()
        self.assertEqual((self.wt / "sayac.txt").read_text().count("x"), 1)

    def test_paralel_seritler_kurulumu_ve_taban_qayi_bir_kez_kosar(self):
        """B223 (25.09 live R10-hard): two parallel lanes started in the same worktree at the same time; both saw no marker and
        ran `uv sync` and `make check` twice concurrently. The second lane waits for the first to finish and shares the result."""
        import threading
        scratch = self.wt / '.pytest_cache'
        scratch.mkdir()
        setup_count, qa_count = scratch / 'kurulum-sayac', scratch / 'qa-sayac'
        (self.proje_dir / "kurulum-komut").write_text(f"echo k >> {setup_count}; sleep 1\n")
        (self.proje_dir / "qa-komut").write_text(f"echo q >> {qa_count}\n")
        self.authorize_setup()
        sonuclar = []
        seritler = [threading.Thread(target=lambda: sonuclar.append(
            core.taban_hazirlik("p", self.wt, self.logf, self.isaret, self.proje_dir))) for _ in range(2)]
        for s in seritler:
            s.start()
        for s in seritler:
            s.join(timeout=30)
        self.assertEqual(setup_count.read_text().split(), ['k'])
        self.assertEqual(qa_count.read_text().split(), ['q'])
        self.assertEqual(len(sonuclar), 2)
        self.assertEqual(sonuclar[0]["kurulum"], sonuclar[1]["kurulum"])

    def test_urun_zaten_degismisse_taban_qa_atlanir(self):
        (self.wt / "urun.py").write_text("x = 1\n")
        r = self.hazirla(qa="exit 2")
        self.assertEqual((r["qa"]["status"], r["qa"]["reason"]), ("SKIPPED", "urun-var"))

    def test_tasinan_kaynak_goruntusu_urun_sayilmaz_ve_eski_olcumsuz_isaret_yenilenir(self):
        """C16/N-ENV03 (21.10 live request-9c7a9d76): an uncommitted patch carried from the active branch makes the worktree dirty; the engine took that as
        "there is a product", skipped baseline QA and the start gate dropped the run. The accepted source snapshot (HEAD + the carried patch)
        is the baseline itself: while the worktree is identical to the snapshot baseline QA runs; a change deviating from the snapshot still counts as product.
        An old marker that took no measurement (urun-var) is re-evaluated; a measured marker is kept."""
        (self.wt / "urun.py").write_text("x = 0\n")
        subprocess.run(["git", "-C", str(self.wt), "add", "urun.py"], check=True)
        subprocess.run(["git", "-C", str(self.wt), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "kaynak"], check=True)
        (self.wt / "urun.py").write_text("x = 1\n")
        (self.wt / "yeni.txt").write_text("tasinan\n")
        kaynak = self.d / "source"
        (kaynak / "untracked").mkdir(parents=True)
        (kaynak / "degisiklik.patch").write_bytes(subprocess.run(["git", "-C", str(self.wt), "diff", "--binary", "--no-ext-diff", "HEAD", "--"],
                                                                 check=True, capture_output=True).stdout)
        (kaynak / "untracked/yeni.txt").write_text("tasinan\n")
        goruntu = {"sha": subprocess.run(["git", "-C", str(self.wt), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip(),
                   "branch": "cr35-kabul", "carried": True, "dirty_files": 2, "patch": str(kaynak / "degisiklik.patch"), "untracked": str(kaynak / "untracked")}
        self.store.update(self.rid, source=goruntu)
        (self.wt / "urun.py").write_text("x = 2\n")   # a change deviating from the snapshot = product
        r = self.hazirla(qa="exit 0")
        self.assertEqual((r["status"], r["reason"], r["qa"]["reason"]), ("FAIL", "baseline-unavailable", "urun-var"))
        (self.wt / "urun.py").write_text("x = 1\n")   # identical to the snapshot
        r = self.hazirla()
        self.assertEqual((r["status"], r["qa"]["status"], r["qa"]["exit_code"]), ("PASS", "PASS", 0), r)
        self.assertIn("kaynak goruntusuyle ayni", self.logf.read_text())
        self.assertEqual(json.loads(self.isaret.read_text())["qa"]["status"], "PASS")
        r2 = self.hazirla()
        self.assertEqual(r2["qa"]["status"], "PASS", "olculmus isaret yeniden olculmez")
        self.assertEqual(self.logf.read_text().count("TABAN QA:"), 1)

    def test_taban_uyarisi_yalniz_taban_kirmiziyken(self):
        self.assertEqual(core.taban_uyarisi(self.isaret), "")
        self.hazirla(qa="exit 2")
        self.assertIn("TABAN DA KIRMIZI", core.taban_uyarisi(self.isaret))
        self.assertIn("rc=2", core.taban_uyarisi(self.isaret))


if __name__ == "__main__":
    unittest.main()
