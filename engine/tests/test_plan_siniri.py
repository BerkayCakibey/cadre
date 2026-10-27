"""C9: reject untrusted plan paths (symlink escape, type/schema violation, two-plan ambiguity)."""

import json
import os
from pathlib import Path
import unittest
import time
from unittest.mock import patch

import test_kosu_sessions as fixtures
import test_dispatch as dispatch_fixtures
import qe_core as core
import dispatch_util


SECRET = 'sk-' + 'Q' * 32  # Synthetic marker, never a credential.


class PlanBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SessionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def plan(self):
        return {'is_id':'work', 'gorevler':[{'id':'g1', 'model':'auto', 'dosyalar':['new.py'],
                'tanim':'create a function', 'kabul':'function returns 1', 'bagimli':[],
                'zorluk':1, 'paralel_grup':0, 'tahmin_dk':1, 'tahmin_usd':0}]}

    def test_invalid_paths_ids_and_types_are_rejected(self):
        changes = [('dosyalar', ['../outside']), ('dosyalar', ['/tmp/outside']),
                   ('dosyalar', ['.git/config']), ('dosyalar', ['dir/../../outside']),
                   ('dosyalar', ['dir\\outside']), ('dosyalar', ['*.py']),
                   ('dosyalar', ['a,b.py']), ('dosyalar', 'file.py'), ('dosyalar', [None]),
                   ('id', 'g1/../../outside'), ('zorluk', True), ('tahmin_dk', '1; false'),
                   ('tahmin_usd', float('nan')), ('kabul', {}), ('model', 'unknown')]
        for field, value in changes:
            with self.subTest(field=field, value=value):
                plan = self.plan()
                plan['gorevler'][0][field] = value
                with self.assertRaises(ValueError):
                    dispatch_util.validate_plan(plan, self.f.wt)

    def test_harmless_path_shapes_are_normalized_instead_of_killing_the_whole_plan(self):
        """B95 (18.09 live): deepseek -> cerebras -> gemini, all three plans were thrown away with `file path must be explicit and
        relative`. Throwing away a structurally correct plan because of one path DETAIL is our defect."""
        plan = self.plan()
        plan['gorevler'][0]['dosyalar'] = ['./a.py', 'a.py', 'dir//b.py', 'dir/', ' "c.py" ']
        dispatch_util.validate_plan(plan, self.f.wt)
        self.assertEqual(plan['gorevler'][0]['dosyalar'], ['a.py', 'dir/b.py', 'dir', 'c.py'])

    def test_annotated_plan_paths_keep_the_module_the_plan_named(self):
        """D01 (30.09 rehearsal 080502/134757): `path (description)` was accepted as the path as is, and the scope check counted the real
        file as outside the plan and removed it -> ModuleNotFoundError. The description is separated from the path; the path stays in scope."""
        import test_sonuc
        plan = self.plan()
        plan['gorevler'][0]['dosyalar'] = [
            'src/butcem/ (mevcut göç/şema modülü — scout belirler)',
            'src/butcem/islemler.py (yeni depo modülü; projede depo modülleri farklı adlandırılmışsa o adla)',
            'src/butcem/cli.py (ya da mevcut CLI giriş modülü / __main__.py)', 'tests/test_islem.py']
        dispatch_util.validate_plan(plan, self.f.wt)
        self.assertEqual(plan['gorevler'][0]['dosyalar'],
                         ['src/butcem', 'src/butcem/islemler.py', 'src/butcem/cli.py', 'tests/test_islem.py'])
        f = test_sonuc.CompletionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        once = core.serit_durumu(f.wt)
        modul = f.wt/'src/butcem/islemler.py'
        modul.parent.mkdir(parents=True)
        modul.write_text('VALUE = 1\n')
        dar = {'gorevler': [{'id': 'g1', 'dosyalar': [dispatch_util.plan_path(
            'src/butcem/islemler.py (yeni depo modülü)', f.wt)]}]}
        cikan, _ = core.kapsam_denetimi(f.wt, dar, 'g1', once, f.home/'yedek')
        self.assertEqual(cikan, [])
        self.assertTrue(modul.exists())

    def test_schema_violation_is_reported_as_a_correctable_schema_error(self):
        """A schema violation is NOT A MODEL FAILURE: a fixed format + exit code 2 so the caller can tell them apart."""
        import subprocess
        import sys as _sys
        plan = self.plan()
        plan['gorevler'][0]['dosyalar'] = ['/etc/passwd']
        arac = Path(dispatch_util.__file__)
        r = subprocess.run([_sys.executable, str(arac), 'extract'], input=json.dumps(plan),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)
        self.assertTrue(r.stderr.startswith('SEMA: '), r.stderr)
        self.assertEqual(r.stdout.strip(), '')

    def test_symlink_escape_is_rejected_before_any_writer(self):
        f = dispatch_fixtures.SchedulerTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        outside = f.fixture.home/'outside'
        outside.mkdir()
        (f.fixture.wt/'link').symlink_to(outside, target_is_directory=True)
        task = f.task('g1')
        task['dosyalar'] = ['link/product.txt']
        self.assertNotEqual(f.fixture.dispatch_fixture('GECER', tasks=[task], worker=dispatch_fixtures.WORKER), 0)
        self.assertEqual(f.events(), [])
        self.assertEqual(list(outside.iterdir()), [])

    def test_json_braces_in_text_are_data_and_two_plans_are_ambiguous(self):
        plan = self.plan()
        plan['gorevler'][0]['tanim'] = 'parse {a:{b:1}} safely'
        text = '```json\n' + json.dumps(plan) + '\n```'
        self.assertEqual(dispatch_util.extract_plan(text), plan)
        with self.assertRaises(ValueError):
            dispatch_util.extract_plan(text + text)



class SeviyePlanSiniriTests(unittest.TestCase):
    """G-E9: `dispatch_util.py extract <rule>` rejects a plan exceeding the level limit with a SCHEMA error; the dispatcher gives it back
    to the planner as a correction request (not counted as a model failure)."""
    def cagir(self, n, puan, *arg):
        import subprocess
        import sys
        def gorev(i):
            return {'id': f'g{i}', 'model': 'auto', 'dosyalar': [f'f{i}.py'], 'tanim': 't', 'kabul': 'k', 'bagimli': [],
                    'zorluk': 1, 'paralel_grup': 0, 'tahmin_dk': 1}
        plan = {'is_id': 'work', 'kapsam_puani': puan, 'gorevler': [gorev(i) for i in range(1, n + 1)]}
        r = subprocess.run([sys.executable, dispatch_util.__file__, 'extract', *arg], input=json.dumps(plan),
                           capture_output=True, text=True, timeout=30)
        return r.returncode, r.stderr

    def test_seviye_sinirini_asan_plan_sema_hatasi_verir(self):
        rc, err = self.cagir(4, 30, '25')
        self.assertEqual(rc, 2, err)
        self.assertTrue(err.startswith('SEMA: '), err)
        self.assertIn('en cok 3', err)

    def test_sinir_icindeki_ve_kuralsiz_cagri_kabul_edilir(self):
        self.assertEqual(self.cagir(3, 30, '25')[0], 0)
        self.assertEqual(self.cagir(4, 30)[0], 0, 'kural verilmeyen eski cagiranlar etkilenmez')

if __name__ == '__main__':
    unittest.main()
