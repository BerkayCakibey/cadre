"""C2: real scheduler, fake writers, measurable ordering and exclusion."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import test_sonuc as fixtures
from dispatch_util import sirali_gorevler

import os
from unittest.mock import patch

def setUpModule():
    ortam = patch.dict(os.environ, {'CADRE_ZINCIR_KALITE': 'opencode-go/qwen3.8-max'})
    ortam.start()
    unittest.addModuleCleanup(ortam.stop)



WORKER = '''
import json,os,pathlib,sys,time
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT']+'/lib')
from kosu import RunStore
p=pathlib.Path(os.environ['CADRE_RUN_DIR'])
gid=os.environ['CADRE_GOREV_ID']
def event(kind):
    with (p/'writer-events.jsonl').open('a') as f:
        f.write(json.dumps({'id':gid,'event':kind,'time':time.monotonic()})+'\\n')
event('start')
product=pathlib.Path(os.environ['CADRE_WORKTREE'])/'product.txt'
before=product.read_text()
time.sleep(.3)
product.write_text(before+gid+'\\n')
event('end')
RunStore().task_update(os.environ['CADRE_RUN_ID'],gid,state='uretti')
'''


class SchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.CompletionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def task(self, gid, dependencies=(), model='ds-pro'):
        return {'id':gid,'tanim':'edit product','model':model,'paralel_grup':1 if model != 'yerel' else 0,
                'dosyalar':['product.txt'],'kabul':'product works','zorluk':2,'tahmin_dk':1,'tahmin_usd':0.01,'bagimli':list(dependencies)}

    def events(self):
        path=self.fixture.store.path(self.fixture.rid)/'writer-events.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_same_file_writers_are_serial_and_dependencies_precede_consumers(self) -> None:
        tasks=[self.task('g2', ['g1']), self.task('g1'), self.task('g3', model='auto')]
        self.assertEqual(self.fixture.dispatch_fixture('GECER', tasks=tasks, worker=WORKER, plan_ek={'kapsam_puani': 30, 'kapsam_gerekce': 'uc bagimli adim'}), 0, (self.fixture.store.path(self.fixture.rid) / 'dispatch.log').read_text())
        events=self.events()
        starts={e['id']:e['time'] for e in events if e['event']=='start'}
        ends={e['id']:e['time'] for e in events if e['event']=='end'}
        self.assertGreaterEqual(starts['g2'], ends['g1'])
        ordered=sorted(starts,key=starts.get)
        self.assertTrue(all(starts[b]>=ends[a] for a,b in zip(ordered,ordered[1:])))
        product=(self.fixture.wt/'product.txt').read_text().splitlines()
        for gid in ('g1','g2','g3'):
            self.assertEqual(product.count(gid),1)

    def test_cycle_or_unknown_dependency_is_rejected_before_any_writer(self) -> None:
        tasks=[self.task('g1',['g2']),self.task('g2',['g1'])]
        self.assertNotEqual(self.fixture.dispatch_fixture('GECER', tasks=tasks, worker=WORKER, plan_ek={'kapsam_puani': 30, 'kapsam_gerekce': 'uc bagimli adim'}),0)
        self.assertEqual(self.events(),[])

    def test_failed_dependency_never_starts_its_consumer(self) -> None:
        tasks=[self.task('g2',['g1'],model='auto'),self.task('g1',model='auto')]
        worker=WORKER.replace("event('start')", "event('start')\nif gid=='g1':\n    RunStore().task_update(os.environ['CADRE_RUN_ID'],gid,state='yarim')\n    raise SystemExit(1)")
        self.assertNotEqual(self.fixture.dispatch_fixture('GECER', tasks=tasks, worker=worker),0)
        self.assertNotIn('g2',[e['id'] for e in self.events()])

    def test_invalid_dependency_shapes_are_rejected(self) -> None:
        for needs in (['absent'], 'g1', ['g1'], [None]):
            with self.subTest(needs=needs):
                task=self.task('g1')
                task['bagimli']=needs
                with self.assertRaises(ValueError):
                    sirali_gorevler({'gorevler':[task]})


if __name__ == '__main__':
    unittest.main()


class GorevSureTavaniTests(unittest.TestCase):
    """The task time cap is derived from tahmin_dk in the plan; the floor is 30 minutes so a low estimate does not cut the executor
    in the middle of finished work (26.10 pilot 2c: a comment edit estimated at 3 min was cut at a 10 min cap, the work had finished in production)."""

    def tavan(self, tahmin_dk):
        import subprocess, sys
        plan = {'gorevler': [{'id': 'g1', 'tahmin_dk': tahmin_dk, 'model': 'auto', 'paralel_grup': 0, 'zorluk': 1}]}
        out = subprocess.run([sys.executable, str(Path(__file__).resolve().parent.parent / 'lib/dispatch_util.py'), 'abort', 'g1'],
                             input=json.dumps(plan), capture_output=True, text=True, check=True, timeout=60).stdout
        return int(out.strip())

    def test_abort_seconds_have_a_thirty_minute_floor_and_scale_with_the_estimate(self):
        self.assertEqual(self.tavan(3), 1800)
        self.assertEqual(self.tavan(10), 1800)
        self.assertEqual(self.tavan(20), 3600)


class PlanRedAyrimiTests(unittest.TestCase):
    """B126 (19.09, caught in the first real run): on a budget denial the model is NEVER called, the output comes back EMPTY
    and `extract` counted empty input as a "schema violation". The upper layer took this for a model defect and went back to the SAME
    model with a correction request — since the budget does not change in two seconds that attempt was guaranteed to be
    wasted. Live evidence: in two separate runs deepseek-v4-pro burned two attempts and fell to minimax-m3.

    B128: in the same file the word `alt_ajanlar` was inside backticks; in double-quoted text that triggers command
    substitution and every run printed `alt_ajanlar: command not found`."""

    KAYNAK = Path(__file__).resolve().parents[1] / 'bin/cadre-dispatch.sh'

    def test_the_script_has_no_command_substitution_inside_the_plan_prompt(self):
        """B128 negative guard: if a backtick stays in the prompt text the shell takes it for a command."""
        import subprocess
        r = subprocess.run(['bash', '-n', str(self.KAYNAK)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('`alt_ajanlar`', self.KAYNAK.read_text(),
                         'ters tirnak komut ikamesi tetikler; tek tirnak kullan')

    def test_light_merge_runs_no_project_tests_outside_the_sandboxed_qa_gate(self):
        metin = self.KAYNAK.read_text()
        blok = metin[metin.index('HAFIF BIRLESTIRME'):metin.index('One final predicate owns QA')]
        for yasak in ('make test', 'tests/*.sh', 'uv run', 'pytest'):
            self.assertNotIn(yasak, blok, f'hafif birlestirme sandbox disinda proje testi kosmamali: {yasak}')
        self.assertIn('git diff --stat', blok)

    def test_the_plan_prompt_forbids_a_qa_only_task(self):
        """B197 (23.09 R1-medium-v2): the planner opened a local lane that only ran `make check` (~20 min); the QA gate and its repair already do that."""
        self.assertIn('yalniz dogrulama/qa gorevi', self.KAYNAK.read_text().lower(), 'plan istemi QA-only gorevi yasaklamali')

    def test_the_plan_prompt_scores_the_whole_request_and_never_narrows_it(self):
        metin = self.KAYNAK.read_text().lower()
        self.assertIn('istegin tamamina 0-100 puan ver, kendi planina degil', metin)
        self.assertIn('8 goreve sigmayan cok maddeli backlog', metin)
        self.assertIn('kapsami kendiliginden daraltma', metin)
        duzeltme = metin.split('onceki deneme reddedildi', 1)[1].split('continue', 1)[0]
        self.assertIn('kayitli kapsami koru', duzeltme)
        self.assertIn('rutin karari gerekceli varsayimla kaydet', duzeltme)
        self.assertIn('kritik amac/yetki/tavan belirsizligini karar icin bildir', duzeltme)
        self.assertIn('kapsami kendiliginden genisletme', duzeltme)
        self.assertNotIn('en genis makul', duzeltme)


class ProjeKipiDevirTests(unittest.TestCase):
    """G-P1: ultra level + planner approval -> the dispatcher hands over to the project coordinator before moving to plan lanes and returns its exit
    code; without approval it continues like very hard. The real script, with fake level/project/helpers."""
    STUBS = {
        'common.sh': 'qe_repo_path(){ printf "%s\\n" "$FAKE_REPO"; }\n',
        'dispatch_util.py': 'import sys\nsys.stdin.read()\nprint("1 gorev")\n',
        'seviye.py': 'print("ultra 95 10.0 2 8")\n',
        'proje.py': ('import os, sys\na = sys.argv[1:]\n'
                     'if a[0] == "uygun": sys.exit(0 if os.environ["FAKE_UYGUN"] == "1" else 1)\n'
                     'open(os.environ["FAKE_PROJE"], "a").write(" ".join(a) + "\\n"); sys.exit(7)\n'),
        'kosu.py': 'import sys\nsys.exit(0 if sys.argv[1] == "plan-ready" else 1)\n',
    }

    def kos(self, uygun):
        import os, subprocess, tempfile
        t = Path(tempfile.mkdtemp(prefix='gp1-'))
        (t / 'eng/lib').mkdir(parents=True); (t / 'eng/bin').mkdir(); (t / 'run').mkdir(); (t / 'repo').mkdir()
        for ad, icerik in self.STUBS.items():
            (t / 'eng/lib' / ad).write_text(icerik)
        for ad in ('sinirlar.py', 'sinirlar.json'):   # 4c-4: plan deadline from a single source
            (t / 'eng/lib' / ad).write_text((Path(__file__).resolve().parents[1] / 'lib' / ad).read_text())
        (t / 'eng/bin/cadre-dispatch.sh').write_text(PlanRedAyrimiTests.KAYNAK.read_text())
        (t / 'run/plan.json').write_text('{"is_id":"x","gorevler":[]}')
        (t / 'is.md').write_text('sifirdan proje\n')
        env = dict(os.environ, CADRE_ENGINE_ROOT=str(t / 'eng'), CADRE_HOME=str(t), CADRE_RUN_ID='ana', CADRE_RUN_DIR=str(t / 'run'),
                   CADRE_REAL_OPENCODE='/usr/bin/true', FAKE_REPO=str(t / 'repo'), FAKE_UYGUN=uygun, FAKE_PROJE=str(t / 'proje-cagri'))
        r = subprocess.run(['bash', str(t / 'eng/bin/cadre-dispatch.sh'), 'p', str(t / 'is.md')], env=env,
                           capture_output=True, text=True, timeout=60)
        cagri = (t / 'proje-cagri').read_text() if (t / 'proje-cagri').exists() else ''
        return r.returncode, cagri, (t / 'run/dispatch.log').read_text()

    def test_ultra_ve_onayli_is_proje_koordinatorune_devredilir(self):
        rc, cagri, log = self.kos('1')
        self.assertEqual((rc, cagri), (7, 'baslat ana p\n'), log)
        self.assertIn('PROJE KIPI', log)

    def test_planlayici_onaylamazsa_proje_kipi_acilmaz(self):
        rc, cagri, log = self.kos('0')
        self.assertEqual(cagri, '')
        self.assertIn('proje kipi yok', log)


class PlanDenemeSayimiTests(unittest.TestCase):
    """B171 (23.09 T1/T1b/T1v3): the plan loop also counted a budget denial as an "attempt"; three attempts ended before reaching a link
    and the run fell to the rule-based path. The real script is run up to the plan section with a fake budget/router/model."""

    STUBS = {
        'common.sh': 'qe_repo_path(){ printf "%s\\n" "$FAKE_REPO"; }\n',
        'butce.py': 'import json; print(json.dumps({"go": {"oran": 0.5, "pencere_acik": True}, '
                    '"claude": {"pay": 0, "kullanilabilir_usd": 0}, "ds": {"oran": 0.5}}))\n',
        'onelek.py': 'import os; print(os.environ.get("FAKE_ONELEK", "GECER STANDART"))\n',
        'yonlendirici.py': ('import os, sys\na = sys.argv[1:]\n'
                            'if a[0] == "ilk":\n'
                            '    open(os.environ["FAKE_ILK_SAYAC"], "a").write("x")\n'
                            '    s = os.environ["FAKE_ILK"].split("|")\n'
                            '    print(s[min(len(open(os.environ["FAKE_ILK_SAYAC"]).read()), len(s)) - 1])\n'
                            'elif a[0] == "bekleme": print(os.environ.get("FAKE_BEKLEME", "0"))\n'
                            'elif a[0] == "sonraki": print(dict(x.split(">") for x in os.environ["FAKE_SONRAKI"].split(",")).get(a[2], ""))\n'),
        'qe_core.py': ('import json, os\n'
                       'def capture_opencode(rb, cwd, args, timeout):\n'
                       '    model = args[1]\n'
                       '    if os.environ.get("FAKE_ISTEM"): open(os.environ["FAKE_ISTEM"], "a").write(args[2] + "\\n")\n'
                       '    with open(os.environ["FAKE_CAGRI"], "a") as f: f.write(model + "\\n")\n'
                       '    kez = open(os.environ["FAKE_CAGRI"]).read().split().count(model)\n'
                       '    sira = dict(x.split("=") for x in os.environ["FAKE_DAVRANIS"].split(","))[model].split("+")\n'
                       '    d = sira[min(kez, len(sira)) - 1]\n'
                       '    if d == "red": return "CADRE_BUDGET_DENIED: allocation exhausted"\n'
                       '    if d == "bozuk": return "plan degil"\n'
                       '    return json.dumps({"gorevler": [{"id": "g1"}]})\n'),
        'dispatch_util.py': ('import json, os, sys\nt = sys.stdin.read()\n'
                             'open(os.environ["FAKE_EXTRACT"], "a").write(" ".join(sys.argv[1:]) + "\\n")\n'
                             'try: d = json.loads(t); assert d["gorevler"]\n'
                             'except Exception: print("SEMA: plan JSON degil", file=sys.stderr); sys.exit(1)\n'
                             'print(json.dumps(d))\n'),
        'kosu.py': ('import os, pathlib, sys\n'
                    'class RunStore: pass\n'
                    'def capture_source_plan(store, rid, real, model, text, task, timeout, difficulty):\n'
                    '    from qe_core import capture_opencode\n'
                    '    return capture_opencode(real, os.environ["FAKE_REPO"], ["-m", model, text], timeout)\n'
                    'a = sys.argv[1:] if __name__ == "__main__" else [""]\nz = pathlib.Path(os.environ["CADRE_RUN_DIR"])\n'
                    'if a[0] == "task-path": d = z / "tasks" / a[2]; d.mkdir(parents=True, exist_ok=True); print(d)\n'
                    'elif a[0] == "save-plan": (z / "kaydedilen-plan.json").write_text(sys.stdin.read()); sys.exit(1)\n'
                    'elif a[0] == "zor-tavan": (z / "zor-tavan").write_text(" ".join(a[1:])); print("2.00 5.00")\n'),
        'seviye.py': 'import sys\nprint(37 if sys.argv[1:2] == ["kural"] else "")\n',
    }

    def kos(self, ilk, sonraki, davranis, onelek='GECER STANDART', bekleme='0', kayit=None):
        import os, subprocess, tempfile
        t = Path(tempfile.mkdtemp(prefix='b171-'))
        (t / 'eng/lib').mkdir(parents=True); (t / 'eng/bin').mkdir(); (t / 'run').mkdir(); (t / 'repo').mkdir()
        for ad, icerik in self.STUBS.items():
            (t / 'eng/lib' / ad).write_text(icerik)
        for ad in ('sinirlar.py', 'sinirlar.json'):   # 4c-4: plan deadline from a single source
            (t / 'eng/lib' / ad).write_text((Path(__file__).resolve().parents[1] / 'lib' / ad).read_text())
        (t / 'eng/bin/cadre-dispatch.sh').write_text(PlanRedAyrimiTests.KAYNAK.read_text())
        (t / 'is.md').write_text('gorev\n')
        if kayit is not None:
            (t / 'run/run.json').write_text(json.dumps(kayit))
        env = dict(os.environ, CADRE_ENGINE_ROOT=str(t / 'eng'), CADRE_HOME=str(t), CADRE_RUN_ID='fixture', CADRE_RUN_DIR=str(t / 'run'),
                   CADRE_REAL_OPENCODE='/usr/bin/true', FAKE_REPO=str(t / 'repo'), FAKE_ILK=ilk, FAKE_SONRAKI=sonraki,
                   FAKE_DAVRANIS=davranis, FAKE_CAGRI=str(t / 'cagri'), FAKE_ONELEK=onelek,
                   FAKE_EXTRACT=str(t / 'extract-argv'), FAKE_ILK_SAYAC=str(t / 'ilk-sayac'), FAKE_BEKLEME=bekleme,
                   FAKE_ISTEM=str(t / 'istem'))
        env.pop('CADRE_KOSU_TAVAN_USD', None)
        self.son_kok = t
        subprocess.run(['bash', str(t / 'eng/bin/cadre-dispatch.sh'), 'p', str(t / 'is.md')], env=env,
                       capture_output=True, text=True, timeout=120)
        cagri = (t / 'cagri').read_text().split() if (t / 'cagri').exists() else []
        return (t / 'run/kaydedilen-plan.json').exists(), cagri, (t / 'run/dispatch.log').read_text()

    def test_a_project_feature_plan_is_told_where_its_acceptance_tests_must_live(self):
        self.kos('m1', 'm1>m2', 'm1=plan', kayit={'feature_acceptance': {'qa_komut': 'uv run pytest tests/unit/test_ozellik.py'}})
        istem = (self.son_kok / 'istem').read_text()
        self.assertIn('OZELLIK KABUL KOMUTU (QA kapisi YALNIZ bunu kosar): uv run pytest tests/unit/test_ozellik.py', istem)
        self.assertIn("ilgili gorevin 'dosyalar' listesine ekle", istem)

    def test_a_plain_run_plan_carries_no_feature_acceptance_line(self):
        self.kos('m1', 'm1>m2', 'm1=plan')
        self.assertNotIn('OZELLIK KABUL KOMUTU', (self.son_kok / 'istem').read_text())

    def test_t1_sequence_reaches_the_ring_after_the_denials(self):
        """The same as the T1 log: claude denial, deepseek schema violation, deepseek denial → minimax must be tried."""
        plan, cagri, log = self.kos('m1', 'm1>m2,m2>m3', 'm1=red,m2=bozuk+red,m3=plan')
        self.assertEqual(cagri, ['m1', 'm2', 'm2', 'm3'], log)
        self.assertTrue(plan, log)

    def test_a_hard_task_raises_the_run_cap_before_planning(self):
        """B199: a hard job (pre-filter PREMIUM) is raised to its own cap before planning; not called for easy/medium work."""
        _p, _c, log = self.kos('m1', 'm1>m2', 'm1=plan', onelek='GECER PREMIUM')
        self.assertEqual((self.son_kok / 'run/zor-tavan').read_text(), 'fixture p')
        self.assertIn('kosu tavani $2.00 -> $5.00', log)
        self.assertLess(log.index('kosu tavani'), log.index('plan deneme 1'))
        self.kos('m1', 'm1>m2', 'm1=plan')
        self.assertFalse((self.son_kok / 'run/zor-tavan').exists())

    def test_plan_output_is_validated_against_the_rule_base(self):
        """G-E9 plan limit: the dispatcher computes the rule floor once and gives it to plan extraction; the level limit is applied there."""
        self.kos('m1', 'm1>m2', 'm1=plan')
        self.assertEqual((self.son_kok / 'extract-argv').read_text().splitlines()[0], 'extract 37')

    def test_a_hard_task_waits_for_renewal_when_no_strong_cloud_can_plan(self):
        """Design 3.3: without a strong cloud, if the renewal is near (router wait > 0) the plan waits and the chain is asked again."""
        plan, cagri, log = self.kos('|m1', 'm1>m2', 'm1=plan', bekleme='1')
        self.assertIn('BUTCE BEKLEME (plan)', log)
        self.assertEqual(cagri, ['m1'], log)
        self.assertTrue(plan, log)

    def test_no_wait_when_the_router_sees_no_reason(self):
        _p, _c, log = self.kos('m1', 'm1>m2', 'm1=plan')
        self.assertNotIn('BUTCE BEKLEME', log)

    def test_a_budget_denial_is_not_logged_as_a_schema_violation(self):
        """23.09 R1-medium-v2: the denial was written as "JSON broken/schema violation" before being classified, and the watcher counted it as an anomaly."""
        _plan, _cagri, log = self.kos('m1', 'm1>m2', 'm1=red,m2=plan')
        self.assertIn('plan BUTCE REDDI (m1)', log)
        self.assertNotIn('sema-ihlali (deneme 1); RAW son 300: CADRE_BUDGET_DENIED', log)

    def test_real_failures_still_stop_after_three_attempts(self):
        plan, cagri, log = self.kos('m1', 'm1>m2,m2>m3', 'm1=bozuk,m2=bozuk,m3=plan')
        self.assertEqual(cagri, ['m1', 'm1', 'm2'], log)
        self.assertFalse(plan)
        self.assertIn('plan uretilemedi', log)

    def test_a_chain_that_cycles_through_denials_terminates(self):
        plan, cagri, log = self.kos('m1', 'm1>m2,m2>m1', 'm1=red,m2=red')
        self.assertFalse(plan)
        self.assertIn('plan uretilemedi', log)
        self.assertLessEqual(len(cagri), 10, cagri)


class AltKomutSozlesmesiTests(unittest.TestCase):
    """B130 (20.09, caught in a live run): the `qe_core.py sir` subcommand had been deleted together with the secret policy
    but `cadre-dispatch.sh` kept calling it. The unknown subcommand printed the usage
    text and exited, and the returned garbage text went to the next command as an argument.

    This test binds the contract: EVERY subcommand the shell scripts call must really be recognized
    on the Python side. If a subcommand is deleted its call must be deleted too."""

    KOK = Path(__file__).resolve().parents[1]

    def _cagrilan(self, modul):
        """The `<module>" <subcommand>` form in shell scripts. Comment lines do not count."""
        import re
        cagrilar = set()
        for betik in (self.KOK / 'bin').glob('*.sh'):
            for satir in betik.read_text().split('\n'):
                if satir.lstrip().startswith('#'):
                    continue
                for m in re.finditer(rf'{re.escape(modul)}"?\s+([a-z][a-z0-9_-]*)', satir):
                    cagrilar.add(m.group(1))
        return cagrilar

    def _taninan(self, dosya):
        import re
        kaynak = (self.KOK / 'lib' / dosya).read_text()
        desenler = (r'sys\.argv\[1:2\] == \[[\'"]([a-z0-9_-]+)[\'"]\]',
                    r'sys\.argv\[1\] == [\'"]([a-z0-9_-]+)[\'"]',
                    r'cmd == [\'"]([a-z0-9_-]+)[\'"]', r'kom == [\'"]([a-z0-9_-]+)[\'"]')
        taninan = set()
        for d in desenler:
            taninan |= set(re.findall(d, kaynak))
        import ast
        for node in ast.walk(ast.parse(kaynak)):
            if isinstance(node, ast.Compare) and ast.unparse(node.left) == 'sys.argv[1]':
                for operator, value in zip(node.ops, node.comparators):
                    if isinstance(operator, ast.In) and isinstance(value, (ast.Tuple, ast.List)):
                        taninan.update(item.value for item in value.elts
                                       if isinstance(item, ast.Constant) and isinstance(item.value, str))
        return taninan

    def test_every_subcommand_the_shell_calls_actually_exists(self):
        for modul, dosya in (('qe_core.py', 'qe_core.py'),
                             ('dispatch_util.py', 'dispatch_util.py'),
                             ('yonlendirici.py', 'yonlendirici.py')):
            cagrilan, taninan = self._cagrilan(modul), self._taninan(dosya)
            eksik = {k for k in cagrilan if k not in taninan}
            with self.subTest(modul=modul):
                self.assertFalse(eksik, f'{modul}: kabuk bu alt komutlari cagiriyor ama Python tanimiyor: {sorted(eksik)}')


KAPSAM_WORKER = '''
import os,pathlib,subprocess,sys
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT']+'/lib')
from kosu import RunStore
wt=pathlib.Path(os.environ['CADRE_WORKTREE']); gid=os.environ['CADRE_GOREV_ID']
(wt/'product.txt').write_text((wt/'product.txt').read_text()+gid+'\\n')
(wt/'ekstra_test.py').write_text('def test_x():\\n    assert True\\n')
(wt/'ekstra_commitli.py').write_text('X = 1\\n')
subprocess.run(['git','-C',str(wt),'add','ekstra_commitli.py'],check=True)
subprocess.run(['git','-C',str(wt),'-c','user.name=ajan','-c','user.email=a@x','commit','-qm','ajan commit'],check=True)
if (wt/'notes.txt').exists():
    (wt/'notes.txt').write_text('ajan degistirdi\\n')
RunStore().task_update(os.environ['CADRE_RUN_ID'],gid,state='uretti')
'''


class SeritKapsamTests(unittest.TestCase):
    """B192 (23.09 R1-medium): lane g1 should only have touched evidence_ledger.py but wrote a new test file outside the plan and put it in its own
    commit; no gate caught it, the judge rejected it 70 min later. A NEW file outside the plan is removed from the product (with a backup),
    an EXISTING file changed outside the plan goes to the record and to the judge as a separate criterion."""

    def setUp(self) -> None:
        self.fixture = fixtures.CompletionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def gorev(self):
        return [{'id': 'g1', 'tanim': 'edit product', 'model': 'auto', 'paralel_grup': 0, 'dosyalar': ['product.txt'],
                 'kabul': 'product works', 'zorluk': 2, 'tahmin_dk': 1}]

    def test_missing_plan_warns_and_still_enforces_scope(self):
        import os
        import subprocess
        import sys
        import qe_core
        f = self.fixture
        run = f.store.path(f.rid)
        before = run / 'before.json'
        before.write_text(json.dumps(qe_core.serit_durumu(f.wt)))
        (f.wt / 'unexpected.py').write_text('print(1)\n')
        (f.wt / 'product.txt').write_text('repair\n')
        r = subprocess.run([sys.executable, qe_core.__file__, 'kapsam', str(f.wt), str(run / 'plan.json'),
                            'duzeltme-1', str(before), str(run / 'kapsam-disi')],
                           env={**os.environ, 'CADRE_HOME': str(f.home)}, capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('plan.json yok; bos planla kapsam denetimi', r.stdout)
        self.assertFalse((f.wt / 'unexpected.py').exists())
        self.assertTrue((run / 'kapsam-disi/duzeltme-1/unexpected.py').exists())
        task = f.store.read(f.rid)['tasks']['duzeltme-1']
        self.assertEqual(task['kapsam_disi_degisen'], ['product.txt'])
        self.assertEqual(task['kapsam_disi_yeni'], ['unexpected.py'])

    def test_plan_disi_yeni_dosyalar_urunden_cikarilir_ve_yedeklenir(self) -> None:
        self.assertEqual(self.fixture.dispatch_fixture('GECER', tasks=self.gorev(), worker=KAPSAM_WORKER), 0)
        f = self.fixture
        for ad in ('ekstra_test.py', 'ekstra_commitli.py'):
            self.assertFalse((f.wt / ad).exists(), ad)
            self.assertTrue((f.store.path(f.rid) / 'kapsam-disi' / 'g1' / ad).is_file(), ad)
        self.assertEqual(f.store.read(f.rid)['tasks']['g1'].get('kapsam_disi_yeni'), ['ekstra_commitli.py', 'ekstra_test.py'])
        self.assertIn('KAPSAM DISI', (f.store.path(f.rid) / 'dispatch.log').read_text())
        self.assertIn('g1', (f.wt / 'product.txt').read_text())

    def test_plan_disi_degisen_mevcut_dosya_kalir_ve_hakem_kriteri_olur(self) -> None:
        f = self.fixture
        (f.wt / 'notes.txt').write_text('once\n')
        f.git('add', 'notes.txt')
        f.git('-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-qm', 'notes')
        self.assertEqual(f.dispatch_fixture('GECER', tasks=self.gorev(), worker=KAPSAM_WORKER), 0)
        self.assertEqual((f.wt / 'notes.txt').read_text(), 'ajan degistirdi\n', 'mevcut dosya geri alinmaz')
        self.assertEqual(f.store.read(f.rid)['tasks']['g1'].get('kapsam_disi_degisen'), ['notes.txt'])
        kriterler = [c['id'] for c in f.store.read(f.rid)['final_result']['review'].get('acceptance', [])]
        self.assertIn('kapsam_disi', kriterler)


class KapsamKlasorTests(unittest.TestCase):  # B192: a folder declaration covers the new file under it
    def test_beyan_edilen_klasorun_altindaki_yeni_dosya_plan_icidir(self) -> None:
        import subprocess, tempfile, sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
        import qe_core
        wt = Path(tempfile.mkdtemp(prefix='kapsam-'))
        g = lambda *a: subprocess.run(['git', '-C', str(wt), *a], check=True, capture_output=True)
        g('init', '-q'); (wt / 'a.txt').write_text('a\n'); g('add', '.'); g('-c', 'user.name=t', '-c', 'user.email=t@x', 'commit', '-qm', 'b')
        once = qe_core.serit_durumu(wt)
        (wt / 'pkg').mkdir(); (wt / 'pkg' / 'yeni.py').write_text('x = 1\n'); (wt / 'disari.py').write_text('y = 2\n')
        cik, dis = qe_core.kapsam_denetimi(wt, {'gorevler': [{'dosyalar': ['pkg/']}]}, 'g1', once, wt.parent / (wt.name + '-yedek'))
        self.assertEqual((cik, dis), (['disari.py'], []))
        self.assertTrue((wt / 'pkg' / 'yeni.py').exists())


class AltAjansizKabloTests(unittest.TestCase):  # B196 + 4c-9: the lane starts without sub-agents; the old plan field is not rejected
    def test_serit_alt_ajansiz_baslar(self) -> None:
        f = fixtures.CompletionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        isci = WORKER.replace("event('start')", "event('start')\n(p/'alt.txt').write_text(os.environ.get('CADRE_ALT_AJANSIZ', '<yok>'))")
        gorev = {'id': 'g1', 'tanim': 'edit product', 'model': 'auto', 'paralel_grup': 0, 'dosyalar': ['product.txt'],
                 'kabul': 'product works', 'zorluk': 2, 'tahmin_dk': 1, 'alt_ajanlar': [{'rol': 'scout', 'neden': 'eski'}]}
        self.assertEqual(f.dispatch_fixture('GECER', tasks=[gorev], worker=isci), 0)
        self.assertEqual((f.store.path(f.rid) / 'alt.txt').read_text(), '1')
