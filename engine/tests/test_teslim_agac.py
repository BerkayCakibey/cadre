"""G-C8 delivery (23.09 live T2, B175): after QA PASS + judge PASS (big-pickle, independent) the delivery commit failed:
`agac_sha` runs `git add -A -- . :(exclude…)` in a temporary index; in feedloom `var/` is .gitignored and the URUN_HARIC pathspec
`:(exclude,glob)var/audit/OTOPSI-*.md` names `var`, so git says "paths are ignored by .gitignore" and returns rc=1
→ CalledProcessError → `teslim-commit-hatasi` (an unfair FAIL). Contract: the product tree identity is not affected by ignored directories;
harness files (URUN_HARIC) do not enter the identity; neither do ignored files. No model/network."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import qe_core as core  # noqa: E402


class AgacShaTest(unittest.TestCase):
    def setUp(self):
        self.wt = Path(tempfile.mkdtemp(prefix="agac-"))
        self.g("init", "-q")
        (self.wt / ".gitignore").write_text("var/\n")
        (self.wt / "a.py").write_text("x = 1\n")
        self.g("add", ".gitignore", "a.py"); self.g("commit", "-qm", "taban")

    def g(self, *a):
        return subprocess.run(["git", "-C", str(self.wt), "-c", "user.email=t@t", "-c", "user.name=t", *a],
                              check=True, capture_output=True, text=True).stdout.strip()

    def test_yoksayilan_var_dizini_varken_agac_kimligi_hesaplanir(self):
        (self.wt / "var" / "audit").mkdir(parents=True)
        (self.wt / "var" / "audit" / "OTOPSI-1.md").write_text("otopsi\n")   # .gitignore: var/
        (self.wt / "b.py").write_text("y = 2\n")                            # product (untracked)
        sha = core.agac_sha(self.wt)
        self.assertRegex(sha, r"^[0-9a-f]{40}$")

    def test_harness_ve_yoksayilan_dosyalar_kimlige_girmez(self):
        (self.wt / "b.py").write_text("y = 2\n")
        temiz = core.agac_sha(self.wt)
        (self.wt / ".gorevler.json").write_text("[]")
        (self.wt / ".cadre-plan").mkdir(); (self.wt / ".cadre-plan" / "p.md").write_text("plan")
        (self.wt / "var").mkdir(); (self.wt / "var" / "x.txt").write_text("ignored")
        self.assertEqual(core.agac_sha(self.wt), temiz, "harness/yoksayilan dosyalar urun kimligini degistirmez")
        (self.wt / "b.py").write_text("y = 3\n")
        self.assertNotEqual(core.agac_sha(self.wt), temiz, "urun degisince kimlik degisir")


class CommitWipTest(unittest.TestCase):
    """B175-b (live T3 23.09): `qe_commit_wip` also uses `git add -A -- . :(exclude,glob)var/...`; with an ignored `var/`
    rc=1 -> `return 1` -> no commit -> `teslim-commit-eksik` (a product with QA+judge PASS could not be delivered). Checkpoints fail silently the same way."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="qe-home-"))
        self.wt = self.home / "worktrees" / "proj" / "x"
        self.wt.mkdir(parents=True)
        self.g("init", "-q"); self.g("checkout", "-q", "-b", "cadre/x")
        (self.wt / ".gitignore").write_text("var/\n"); (self.wt / "a.py").write_text("x = 1\n")
        self.g("add", ".gitignore", "a.py"); self.g("commit", "-qm", "taban")

    def g(self, *a):
        return subprocess.run(["git", "-C", str(self.wt), "-c", "user.email=t@t", "-c", "user.name=t", *a],
                              check=True, capture_output=True, text=True).stdout.strip()

    def commit_wip(self):
        common = LIB / "common.sh"
        import izin
        from kosu import RunStore
        store = RunStore(self.home)
        rid = store.accept('proj', 'fixture checkpoint')['run_id']
        store.update(rid, worktree=str(self.wt))
        action = izin.commit_proposal(store, rid, self.wt, 'test')
        first = izin.approve(store, rid, action['token'])
        izin.approve(store, rid, action['token'], first['confirmation'])
        r = subprocess.run(["bash", "-c", '. "$1"; qe_commit_wip "$2" "$3" "test"', "qe", str(common), str(self.wt), "proj"],
                           env={**os.environ, "CADRE_HOME": str(self.home), "CADRE_ENGINE_ROOT": str(LIB.parent),
                                "CADRE_RUN_ID": rid, "CADRE_COMMIT_ACTION": action['token'], "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
                           capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout.strip()

    def test_yoksayilan_var_dizini_varken_urun_commitlenir_harness_disarida_kalir(self):
        (self.wt / "var" / "audit").mkdir(parents=True); (self.wt / "var" / "audit" / "OTOPSI-1.md").write_text("o")
        (self.wt / "b.py").write_text("y = 2\n")
        (self.wt / ".gorevler.json").write_text("[]"); (self.wt / ".cadre-plan").mkdir(); (self.wt / ".cadre-plan" / "p.md").write_text("plan")
        rc, n = self.commit_wip()
        self.assertEqual(rc, 0); self.assertTrue(n.isdigit() and int(n) >= 1, n)
        self.assertIn("b.py", self.g("show", "--stat", "--format=", "HEAD"))
        agac = self.g("ls-tree", "-r", "--name-only", "HEAD")
        for yasak in (".gorevler.json", ".cadre-plan/p.md", "var/audit/OTOPSI-1.md"):
            self.assertNotIn(yasak, agac)
        self.assertEqual(self.g("status", "--porcelain", "--", "b.py"), "", "urun commit'lendi, kirli kalmadi")


if __name__ == "__main__":
    unittest.main()
