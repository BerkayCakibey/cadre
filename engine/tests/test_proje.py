"""G-P project mode (stage 2) — the pure core: feature list validation, selection, state transitions,
stop conditions, re-plan merging, report. No model/network/git."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import proje  # noqa: E402

GUN = 86400


def oz(id_, bagimli=(), oncelik=2, durum='bekliyor', deneme=0):
    return {'id': id_, 'baslik': f'{id_} basligi', 'aciklama': f'{id_} yap', 'kabul': f'{id_} testi yesil',
            'bagimli': list(bagimli), 'oncelik': oncelik, 'durum': durum, 'deneme': deneme, 'kosu': [], 'qa_komut': f'test -e {id_}'}


def cikti(*ozellikler):
    return 'liste:\n```json\n' + json.dumps({'ozellikler': list(ozellikler)}) + '\n```'


class OzellikListesiTest(unittest.TestCase):
    def test_gecerli_liste_normalize_edilir(self):
        liste = proje.ozellik_listesi(cikti({'id': 'f1', 'baslik': 'cekirdek', 'aciklama': 'veri modeli', 'kabul': 'birim testleri',
                                             'bagimli': [], 'oncelik': 1, 'qa_komut': ' python3 -m unittest tests.test_model '}))
        self.assertEqual(liste, [{'id': 'f1', 'baslik': 'cekirdek', 'aciklama': 'veri modeli', 'kabul': 'birim testleri',
                                  'bagimli': [], 'oncelik': 1, 'durum': 'bekliyor', 'deneme': 0, 'kosu': [],
                                  'qa_komut': 'python3 -m unittest tests.test_model'}])

    def test_bozuk_listeler_reddedilir(self):
        cok = [oz(f'f{i}') for i in range(proje.OZELLIK_TAVANI + 1)]
        durumlar = {
            'bos': cikti(),
            'tavan': cikti(*cok),
            'tekrar kimlik': cikti(oz('f1'), oz('f1')),
            'bilinmeyen bagimlilik': cikti(oz('f1', ['f9'])),
            'dongu': cikti(oz('f1', ['f2']), oz('f2', ['f1'])),
            'kabulsuz': cikti({**oz('f1'), 'kabul': ''}),
            'kabul komutsuz': cikti({**oz('f1'), 'qa_komut': ' '}),
            'yer tutucu komut': cikti({**oz('f1'), 'qa_komut': 'hedefli test komutu'}),
            'gecersiz kimlik': cikti(oz('F 1')),
            'json degil': 'yapamadim',
        }
        for ad, ham in durumlar.items():
            with self.subTest(ad), self.assertRaises(ValueError):
                proje.ozellik_listesi(ham)


class PlanlayiciHalkaTest(unittest.TestCase):
    """B250 (26.09 live B203c, $2.15 wasted): (a) when the first link's list passed the rough check but failed full validation
    (big-pickle's truncated 15th item) the project stopped with 'liste-uretilemedi' — the next link was never tried;
    (b) the list went to Claude with #max effort + difficulty 5, and opus#max cut the long output at the budget cap ($1.84)."""
    def test_tam_dogrulamada_dusen_halka_sonrakine_gecer(self):
        from unittest.mock import patch
        cevaplar = {'m1': cikti({**oz('f1'), 'kabul': ''}, oz('f2')), 'm2': cikti(oz('f1'), oz('f2', ['f1']))}
        cagrilar = []

        def sahte(store, rid, model, istem, gid):
            cagrilar.append(model)
            return cevaplar[model]

        with patch.object(proje, '_planlayici_cagir', side_effect=sahte), \
             patch('yonlendirici.zincir', return_value=(['m1', 'm2', 'm3'], [])):
            ham = proje.planla_gercek(None, 'ana')('istem', 'proje-liste')
        self.assertEqual(cagrilar, ['m1', 'm1', 'm2'], 'gecersiz liste veren halka bir duzeltme sansindan (B252b) sonra atlanir, gecerli veren doner')
        self.assertEqual([f['id'] for f in proje.ozellik_listesi(ham)], ['f1', 'f2'])

    def test_yeniden_plan_halkasi_korunan_kimliklere_bagimliligi_kabul_eder(self):
        """B252 (26.09 live B203d, 09:05): B250's link validation did not know the kept (passed/stuck) ids; in a re-plan
        a new item depending on f17 (stuck) was rejected as an 'unknown dependency' and the project stayed blocked."""
        from unittest.mock import MagicMock, patch
        kok = Path(tempfile.mkdtemp(prefix='yeniden-')); store = MagicMock(); store.path.return_value = kok
        (kok / 'proje.json').write_text(json.dumps({'ozellikler': [oz('f16', durum='gecti'), oz('f17', durum='uygulandi', deneme=2), oz('f18', ['f17'])]}))
        cagrilar = []

        def sahte(store_, rid, model, istem, gid):
            cagrilar.append(model)
            return cikti(oz('f18', ['f17']), oz('f24', ['f16', 'f18']))

        with patch.object(proje, '_planlayici_cagir', side_effect=sahte), \
             patch('yonlendirici.zincir', return_value=(['m1', 'm2'], [])):
            ham = proje.planla_gercek(store, 'ana')('istem', 'proje-yeniden')
        self.assertEqual(cagrilar, ['m1'], 'tamamlanmis korunan kimlige bagimlilik gecerli; ikinci halka gerekmez')
        yeni = proje.yeniden_plan_birlestir(json.loads((kok / 'proje.json').read_text())['ozellikler'], ham)
        self.assertEqual([f['id'] for f in yeni], ['f16', 'f17', 'f18', 'f24'])

    def test_dogrulama_hatasi_ayni_halkaya_bir_kez_geri_verilir(self):
        """B252b: a real validation error (unknown id) goes back to the same link ONCE with the error text; if fixed, accepted."""
        from unittest.mock import patch
        cagrilar = []

        def sahte(store, rid, model, istem, gid):
            cagrilar.append((model, 'ONCEKI CEVABIN GECERSIZDI' in istem))
            return cikti(oz('f1'), oz('f2', ['f1'])) if len(cagrilar) > 1 else cikti(oz('f1'), oz('f2', ['f9']))

        with patch.object(proje, '_planlayici_cagir', side_effect=sahte), \
             patch('yonlendirici.zincir', return_value=(['m1', 'm2'], [])):
            ham = proje.planla_gercek(None, 'ana')('istem', 'proje-liste')
        self.assertEqual(cagrilar, [('m1', False), ('m1', True)])
        self.assertEqual([f['id'] for f in proje.ozellik_listesi(ham)], ['f1', 'f2'])

    def test_claude_liste_istegi_yuksek_eforla_ve_zorluk_4_ile_gider(self):
        from test_kosu_sessions import SessionTests, motor_source
        import qe_core as core
        f = SessionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        motor_source(f.home, f.wt)
        (f.wt / 'README.md').write_text('source at the real project root')
        fake = f.home / 'source-claude'
        fake.write_text('#!' + sys.executable + '\n' + r'''
import json,os,pathlib,sys
a=sys.argv[1:]; sid=a[a.index('--session-id')+1]
home=pathlib.Path(os.environ['CADRE_HOME'])
(home/'source-read.json').write_text(json.dumps({'args':a,'cwd':os.getcwd(),'source':pathlib.Path('README.md').read_text()}))
print(json.dumps({'type':'result','session_id':sid,'result':'source read','total_cost_usd':0}))
''')
        fake.chmod(0o700)
        f.allow_fixture_claude(fake)
        with patch.dict(os.environ, {'CLAUDE_BIN': str(fake)}), patch.object(core, 'run_claude', wraps=core.run_claude) as call:
            answer = proje._planlayici_cagir(f.store, f.rid, 'claude/opus#high', 'istem', 'proje-liste')
        self.assertEqual(answer, 'source read')
        self.assertEqual(call.call_args.args[1], 'claude/opus#high')
        self.assertEqual(call.call_args.kwargs['difficulty'], 4)
        read = json.loads((f.home / 'source-read.json').read_text())
        self.assertEqual(Path(read['cwd']).resolve(), f.wt.resolve())
        self.assertEqual(read['source'], (f.wt / 'README.md').read_text())
        args = read['args']
        self.assertEqual(args[args.index('--allowedTools') + 1], core.CLAUDE_READ_ONLY_TOOLS)
        self.assertIn(core.CLAUDE_WRITE_TOOLS, args[args.index('--disallowedTools') + 1])

    def test_source_or_permission_failure_never_retries_another_model(self):
        for error in (ValueError('plan-review-source-modified'), ValueError('plan-review-session-unverified'),
                      ValueError('request-or-plan-change-gate'), PermissionError('permission-required')):
            with self.subTest(error=str(error)), patch.object(proje, '_planlayici_cagir', side_effect=error) as call, \
                 patch('yonlendirici.zincir', return_value=(['m1', 'm2'], [])), self.assertRaises(type(error)):
                proje.planla_gercek(None, 'ana')('istem', 'proje-liste')
            self.assertEqual(call.call_count, 1)

    def test_real_project_planner_repairs_structure_in_its_own_session_with_full_source(self):
        from test_kosu_sessions import SessionTests, motor_source
        import qe_core as core
        f = SessionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        motor_source(f.home, f.wt)
        (f.wt / 'README.md').write_text('project source acceptance')
        answer = cikti(oz('f1'))
        f.fake.write_text('#!' + sys.executable + '\n' + 'answer=' + repr(answer) + '\n' + r'''
import json,os,pathlib,sys
a=sys.argv[1:]; p=a[-1]; home=pathlib.Path(os.environ['CADRE_HOME'])
sid=a[a.index('--session')+1] if '--session' in a else 'ses_project_source'
with (home/'project-source-calls.jsonl').open('a') as out:
    out.write(json.dumps({'prompt':p,'cwd':os.getcwd(),'source':pathlib.Path('README.md').read_text(),
                         'session':sid,'config':json.loads(os.environ['OPENCODE_CONFIG_CONTENT'])})+'\n')
print(json.dumps({'type':'step_start','sessionID':sid,'part':{'type':'step-start'}}))
print(json.dumps({'type':'text','sessionID':sid,'part':{'type':'text','text':answer if 'ONCEKI CEVABIN GECERSIZDI' in p else 'invalid list'}}))
''')
        before = (f.wt / 'README.md').read_bytes()
        with patch.dict(os.environ, {'CADRE_REAL_OPENCODE': str(f.fake)}), \
             patch.object(core.YON, 'zincir', return_value=(['opencode-go/glm-5.3'], [])):
            result = proje.planla_gercek(f.store, f.rid)(proje.liste_istemi('Preserve README.md'), 'proje-liste')
        self.assertEqual(proje.ozellik_listesi(result)[0]['id'], 'f1')
        calls = [json.loads(line) for line in (f.home / 'project-source-calls.jsonl').read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertEqual(len({call['session'] for call in calls}), 1)
        for call in calls:
            self.assertEqual(Path(call['cwd']).resolve(), f.wt.resolve())
            self.assertEqual(call['source'], before.decode())
            self.assertEqual(call['config']['permission']['edit'], 'deny')
            self.assertNotIn('Hicbir arac kullanma', call['prompt'])
        self.assertEqual((f.wt / 'README.md').read_bytes(), before)
        attempts = [json.loads(path.read_text()) for path in f.store.task_dir(f.rid, 'proje-liste').glob('attempts/*/attempt.json')]
        self.assertEqual(sorted(a['resumed'] for a in attempts), [False, True])
        self.assertTrue(all(a['role'] == 'plan' and not a['product_changed'] for a in attempts))

    def test_zero_budget_project_planning_starts_no_executor(self):
        from test_kosu_sessions import SessionTests, motor_source
        import qe_core as core
        f = SessionTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        motor_source(f.home, f.wt)
        f.store.update(f.rid, budget_cap_usd=0)
        with patch.dict(os.environ, {'CADRE_REAL_OPENCODE': str(f.fake)}), \
             patch.object(core.YON, 'zincir', return_value=(['opencode-go/glm-5.3'], [])), self.assertRaises(RuntimeError):
            proje.planla_gercek(f.store, f.rid)('source plan', 'proje-liste')
        self.assertFalse((f.home / 'calls.jsonl').exists())


class SecimTest(unittest.TestCase):
    def test_bagimliligi_bitmis_en_yuksek_oncelikli_ozellik_secilir(self):
        liste = [oz('f1', oncelik=2), oz('f2', ['f1'], oncelik=1), oz('f3', oncelik=1)]
        self.assertEqual(proje.siradaki(liste)['id'], 'f3', 'f2 bagimliligi bitmedigi icin once f3')
        liste[0]['durum'] = 'gecti'
        self.assertEqual(proje.siradaki(liste)['id'], 'f2')

    def test_takilan_bagimlilik_bagimli_ozelligi_bloke_eder(self):
        liste = [oz('f1', durum='takildi'), oz('f2', ['f1'])]
        self.assertIsNone(proje.siradaki(liste))

    def test_hepsi_bitince_secim_yok(self):
        self.assertIsNone(proje.siradaki([oz('f1', durum='gecti'), oz('f2', durum='takildi')]))


class DurumGecisiTest(unittest.TestCase):
    def test_basari_gecti_yazar_ve_kosuyu_kaydeder(self):
        f = proje.sonuc_isle(oz('f1'), 'r1', True)
        self.assertEqual((f['durum'], f['deneme'], f['kosu']), ('uygulandi', 1, ['r1']))  # B346: passed -> implemented [03.10]

    def test_ikinci_basarisizlik_takildi_yapar(self):
        f = proje.sonuc_isle(oz('f1'), 'r1', False)
        self.assertEqual((f['durum'], f['deneme']), ('bekliyor', 1))
        f = proje.sonuc_isle(f, 'r2', False)
        self.assertEqual((f['durum'], f['deneme'], f['kosu']), ('takildi', 2, ['r1', 'r2']))


class DurKosuluTest(unittest.TestCase):
    def durum(self, liste, **ek):
        return {'ozellikler': liste, 'baslangic': 0.0, 'son_gecti': 0.0, 'ardisik_basarisiz': 0, 'son_kabul_turu': 0, **ek}

    def test_calisilacak_ozellik_varsa_devam(self):
        self.assertEqual(proje.dur_karari(self.durum([oz('f1')]), simdi=3600), (False, ''))

    def test_hepsi_bitti_son_kabule_gecer(self):
        self.assertEqual(proje.dur_karari(self.durum([oz('f1', durum='gecti')]), simdi=3600), (False, 'son-kabul'))

    def test_kabul_komutu_olmayan_takilan_ozellik_son_kabulle_gecmis_sayilmaz(self):
        eski = {**oz('f2', durum='takildi'), 'qa_komut': ''}
        self.assertEqual(proje.dur_karari(self.durum([oz('f1', durum='gecti'), eski]), simdi=3600), (True, 'kanitsiz-takilan-ozellik: f2'))

    def test_yedi_gun_tavani_son_kabulden_once_uygulanir(self):
        """B322 [IMPLEMENTED 03.10]: if nothing remains, a time overrun does not block the final acceptance; it stops only if something remains.
        D12 (01.10 Codex, G-P5): when the features were done the final acceptance opened before the 7-day check."""
        gun = 86400
        # Nothing remaining → final acceptance ignores the time (B322)
        for liste in ([oz('f1', durum='gecti')], [oz('f1', durum='gecti'), oz('f2', durum='takildi')]):
            with self.subTest(liste=[f['durum'] for f in liste]):
                self.assertEqual(proje.dur_karari(self.durum(liste, son_gecti=7 * gun), simdi=7 * gun), (True, 'sure-tavani'))
                self.assertEqual(proje.dur_karari(self.durum(liste, son_gecti=7 * gun), simdi=8 * gun + 1), (True, 'sure-tavani'))
        # Something remaining → time cap (B322: with pending work the time condition applies)
        self.assertEqual(proje.dur_karari(self.durum([oz('f1')], son_gecti=7 * gun), simdi=7 * gun), (True, 'sure-tavani'))
        self.assertEqual(proje.dur_karari(self.durum([oz('f1', durum='gecti')], son_gecti=7 * gun - 1), simdi=7 * gun - 1),
                         (False, 'son-kabul'))

    def test_ardisik_uc_basarisizlik_ilerleme_yok(self):
        self.assertEqual(proje.dur_karari(self.durum([oz('f1')], ardisik_basarisiz=3), simdi=3600), (True, 'ilerleme-yok'))

    def test_yirmi_dort_saattir_yeni_gecti_yoksa_ilerleme_yok(self):
        self.assertEqual(proje.dur_karari(self.durum([oz('f1')]), simdi=GUN + 1), (True, 'ilerleme-yok'))

    def test_sure_tavani(self):
        d = self.durum([oz('f1')], son_gecti=7 * GUN)
        self.assertEqual(proje.dur_karari(d, simdi=7 * GUN + 1), (True, 'sure-tavani'))

    def test_secilebilir_yok_ama_bekleyen_var_bloke(self):
        self.assertEqual(proje.dur_karari(self.durum([oz('f1', durum='takildi'), oz('f2', ['f1'])]), simdi=3600), (True, 'bloke'))

    def test_son_kabul_turlari_bitti(self):
        d = self.durum([oz('f1', durum='gecti')], son_kabul_turu=proje.SON_KABUL_TURU)
        self.assertEqual(proje.dur_karari(d, simdi=3600), (True, 'son-kabul-turlari-bitti'))


class YenidenPlanTest(unittest.TestCase):
    def test_stalled_dependency_is_rejected_by_planner_and_merger_without_rewriting_history(self):
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory(prefix='cadre-replan-') as directory:
            root = Path(directory)
            store = MagicMock()
            store.path.return_value = root
            old = [oz('f16', durum='gecti'), oz('f17', durum='takildi', deneme=2)]
            path = root/'proje.json'
            path.write_text(json.dumps({'ozellikler': old}))
            before = path.read_bytes()
            invalid = cikti(oz('f18', ['f17']))
            with self.assertRaisesRegex(ValueError, 'tamamlanmamis korunan bagimlilik'):
                proje.yeniden_plan_birlestir(old, invalid)
            calls = []
            def planner(store_, rid, model, prompt, task):
                calls.append((model, prompt))
                return invalid if model == 'm1' else cikti(oz('f18', ['f16']))
            with patch.object(proje, '_planlayici_cagir', side_effect=planner), \
                    patch('yonlendirici.zincir', return_value=(['m1', 'm2'], [])):
                accepted = proje.planla_gercek(store, 'main')('istem', 'proje-yeniden')
            self.assertEqual([c[0] for c in calls], ['m1', 'm1', 'm2'])
            self.assertIn('tamamlanmamis korunan bagimlilik', calls[1][1])
            merged = proje.yeniden_plan_birlestir(old, accepted)
            self.assertEqual(merged[:2], old)
            self.assertEqual(proje.siradaki(merged)['id'], 'f18')
            self.assertEqual(proje.dur_karari({'ozellikler': merged, 'baslangic': 100, 'son_gecti': 100}, 101), (False, ''))
            self.assertEqual(path.read_bytes(), before)

    def test_explicit_stalled_repair_preserves_criteria_and_has_no_false_dependency(self):
        old = [oz('f17', durum='takildi', deneme=2)]
        repair = {**oz('repair'), 'repair_of': 'f17', 'kabul': old[0]['kabul']}
        merged = proje.yeniden_plan_birlestir(old, cikti(repair))
        self.assertEqual(merged[0], old[0])
        self.assertEqual(proje.siradaki(merged)['id'], 'repair')
        for invalid in ({**repair, 'bagimli': ['f17']}, {**repair, 'kabul': 'weaker criterion'},
                        {**repair, 'repair_of': 'missing'}):
            with self.assertRaises(ValueError):
                proje.yeniden_plan_birlestir(old, cikti(invalid))

    def test_unknown_replan_history_never_calls_paid_planner(self):
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory(prefix='cadre-replan-') as directory:
            store = MagicMock()
            store.path.return_value = Path(directory)
            for content in ('{broken', json.dumps({'ozellikler': [{'id': 'f17', 'durum': 'unknown'}]})):
                (Path(directory)/'proje.json').write_text(content)
                with patch.object(proje, '_planlayici_cagir') as planner, \
                        patch('yonlendirici.zincir', return_value=(['m1'], [])):
                    with self.assertRaisesRegex(ValueError, 'yeniden-plan-gecmisi-okunamadi'):
                        proje.planla_gercek(store, 'main')('request', 'proje-yeniden')
                planner.assert_not_called()

    def test_gecen_ozellik_korunur_yeniler_eklenir_kaldirilan_bekleyen_duser(self):
        eski = [oz('f1', durum='gecti'), oz('f2'), oz('f3', durum='takildi', deneme=2)]
        eski[0]['kosu'] = ['r1']
        yeni = proje.yeniden_plan_birlestir(eski, cikti({**oz('f1'), 'aciklama': 'degistirilmis'}, oz('f4', ['f1']), oz('f3')))
        g = {f['id']: f for f in yeni}
        self.assertEqual(sorted(g), ['f1', 'f3', 'f4'])
        self.assertEqual((g['f1']['durum'], g['f1']['aciklama'], g['f1']['kosu']), ('gecti', 'f1 yap', ['r1']), 'gecen ozellige dokunulmaz')
        self.assertEqual((g['f3']['durum'], g['f3']['deneme']), ('takildi', 2), 'takilan ozelligin gecmisi korunur')
        self.assertEqual(g['f4']['durum'], 'bekliyor')

    def test_yeni_liste_gecen_ozelligi_silemez(self):
        yeni = proje.yeniden_plan_birlestir([oz('f1', durum='gecti'), oz('f2')], cikti(oz('f2')))
        self.assertIn('f1', [f['id'] for f in yeni])


class RaporTest(unittest.TestCase):
    def test_rapor_sayilari_tabloyu_ve_nedeni_icerir(self):
        d = {'kimlik': 'p1', 'istek_ozeti': 'kucuk arac', 'ozellikler': [oz('f1', durum='gecti'), oz('f2', durum='takildi'), oz('f3')],
             'durum': 'durdu', 'neden': 'ilerleme-yok', 'ara_dal': 'cadre/proje-p1', 'harcama_usd': 1.25}
        metin = proje.rapor(d)
        for parca in ('p1', 'kucuk arac', '1 gecti', '1 takildi', '1 bekliyor', 'cadre/proje-p1', 'ilerleme-yok', '| f2 |', '$1.25'):
            self.assertIn(parca, metin)



class StepVerificationTests(unittest.TestCase):
    def setUp(self):
        from kosu import RunStore, atomic_json, plan_digest
        self.temp = tempfile.TemporaryDirectory(prefix='project-step-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.project = self.home / 'projects/lab'
        self.project.mkdir(parents=True)
        self.policy = self.project / 'supervision.json'
        atomic_json(self.policy, {'version': 1, 'mode': 'step'})
        self.store = RunStore(self.home)
        self.parent = self.store.accept('lab', 'Complete the backlog in sequence.')['run_id']
        self.child = self.store.accept('child', 'Complete the first feature.', parent_run_id=self.parent)['run_id']
        final = {'status': 'PASS', 'revision': {'digest': 'selected-child-product'}}
        self.store.update(self.child, state='completed', final_result=final)
        state = proje._yeni_durum(self.parent, 'Complete the backlog in sequence.', 1000)
        state.update(ozellikler=[oz('f1', durum='uygulandi'), oz('f2', ['f1'])], checkpoint={
            'id': 'a' * 32, 'run_id': self.child, 'feature': 'f1', 'status': 'waiting',
            'success': True, 'final_digest': plan_digest(final)})
        self.state_path = self.store.path(self.parent) / 'proje.json'
        atomic_json(self.state_path, state)
        self.store.update(self.parent, state='retryable', reason='proje-durdu: project-step-verification-required')
        self.enter(patch.dict(os.environ, {'CADRE_RUN_ID': '', 'CADRE_AG_YOK': '1'}))

    def enter(self, manager):
        result = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        return result

    def test_project_policy_is_frozen_and_inherited_and_bad_policy_stops_admission(self):
        from kosu import atomic_json
        self.policy.unlink()
        for rid in (self.parent, self.child):
            self.assertEqual(self.store.read(rid)['supervision'], {'version': 1, 'mode': 'step'})
            with self.assertRaisesRegex(ValueError, 'immutable'):
                self.store.update(rid, supervision=None)
        self.assertIsNone(self.store.accept('lab', 'New independent task.')['supervision'])
        for value in ({'version': True, 'mode': 'step'}, {'version': 1, 'mode': 'automatic'}, []):
            atomic_json(self.policy, value)
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'supervision-policy-invalid'):
                self.store.accept('lab', 'Invalid policy must not start.')

    def test_project_feature_cannot_combine_two_bound_backlog_cards(self):
        from kosu import atomic_json
        plan = {'gorevler': [{'id': 't1', 'kabul': 'First acceptance'}, {'id': 't2', 'kabul': 'Second acceptance'}],
                'source_items': [{'id': item, 'source_sha256': 'a' * 64, 'evidence': '', 'tasks': [task]}
                                 for item, task in (('B001', 't1'), ('B002', 't2'))]}
        atomic_json(self.store.path(self.parent) / 'plan.json', plan)
        with self.assertRaisesRegex(ValueError, 'project-step-multiple-source-items'):
            proje.proje_plani_kaydet(self.store, self.parent, [{**oz('mixed'), 'source_tasks': ['t1', 't2']}])

    def test_checkpoint_blocks_manual_execution_revival_and_child_launch(self):
        from kosu import execute_run, launch_guard, revive
        with patch('kosu.subprocess.Popen') as spawn, patch('kosu._baslat') as restart:
            self.assertEqual(execute_run(self.store, self.parent, devam=True), 78)
            self.assertEqual(revive(self.store, LIB.parent), [])
            spawn.assert_not_called()
            restart.assert_not_called()
        for rid in (self.parent, self.child):
            with (self.subTest(rid=rid), self.assertRaisesRegex(ValueError, 'project-step-verification-required'),
                  launch_guard(self.store, rid)):
                self.fail('held project admitted a new operation')

    def test_supervised_roots_wait_for_explicit_start_instead_of_automatic_start_or_retry(self):
        import time

        from kosu import ACCEPT_STALE_SN, atomic_json, revive
        for n, prompt in enumerate(('TUR: inceleme\nInspect sources', 'TUR: rapor\nWrite a report', 'Implement one small feature')):
            project = 'root' + str(n)
            path = self.home / 'projects' / project
            path.mkdir(parents=True)
            atomic_json(path / 'supervision.json', proje.STEP_POLICY)
            rid = self.store.accept(project, prompt)['run_id']
            for state in ('accepted', 'queued', 'retryable'):
                self.store.update(rid, state=state, reason='first-attempt-needs-review')
                record = self.store.read(rid)
                record['created'] = time.time() - ACCEPT_STALE_SN - 10
                atomic_json(self.store.path(rid) / 'run.json', record)
                with (self.subTest(kind=n, state=state), patch('kosu._baslat') as launch,
                      patch.object(self.store, 'sirasi_geldi', return_value=True)):
                    self.assertEqual(revive(self.store, LIB.parent), [])
                    launch.assert_not_called()
                    after = self.store.read(rid)
                    for key in ('start_count', 'retry_count', 'created', 'budget_cap_usd'):
                        self.assertEqual(after.get(key), record.get(key))
            self.store.update(rid, state='cancelled')
        current = self.store.accept('root0', 'TUR: inceleme\nRecover the current interrupted job')['run_id']
        self.store.update(current, state='running')
        with patch('kosu._baslat') as launch, patch.object(self.store, 'sirasi_geldi', return_value=True):
            revive(self.store, LIB.parent)
            self.assertEqual(launch.call_args.args[1], current)
            launch.assert_called_once()

    def test_automatic_queue_handoff_skips_supervised_jobs_and_preserves_the_default(self):
        from kosu import kuyruk_baslat
        for n, supervised in enumerate((True, False)):
            project = 'queue' + str(n)
            if supervised:
                from kosu import atomic_json
                path = self.home / 'projects' / project
                path.mkdir(parents=True)
                atomic_json(path / 'supervision.json', proje.STEP_POLICY)
            rid = self.store.accept(project, 'TUR: inceleme\nInspect the next source')['run_id']
            self.store.update(rid, state='queued')
            with self.subTest(supervised=supervised), patch('kosu._baslat') as launch, patch('kosu.aktif_var', return_value=False):
                self.assertEqual(kuyruk_baslat(self.store, project, LIB.parent), None if supervised else rid)
                self.assertEqual(launch.call_count, 0 if supervised else 1)
            self.store.update(rid, state='cancelled')

    def test_missing_project_state_or_checkpoint_cannot_reopen_a_finished_step(self):
        from kosu import atomic_json, launch_guard, revive
        original = json.loads(self.state_path.read_text())
        for missing in ('file', 'checkpoint', 'empty', 'corrupt'):
            if missing == 'file':
                self.state_path.unlink()
            elif missing == 'empty':
                atomic_json(self.state_path, {})
            elif missing == 'corrupt':
                self.state_path.write_text('{broken')
            else:
                atomic_json(self.state_path, {k: v for k, v in original.items() if k != 'checkpoint'})
            with self.subTest(missing=missing):
                with self.assertRaisesRegex(ValueError, 'supervision-checkpoint-invalid'), launch_guard(self.store, self.parent):
                    self.fail('missing checkpoint admitted an operation')
                with patch('kosu._baslat') as restart:
                    self.assertEqual(revive(self.store, LIB.parent), [])
                    restart.assert_not_called()

    def test_explicit_release_runs_only_the_next_feature_and_waits_again(self):
        from kosu import atomic_json, plan_digest
        with patch('sonuc.approved', return_value=True):
            proje.verify_step(self.store, self.parent, 'a' * 32, note='First feature independently verified.')
        child = self.store.accept('child2', 'Complete the second feature.', parent_run_id=self.parent)
        self.store.update(child['run_id'], state='completed', final_result={'status': 'PASS', 'revision': {'digest': 'second-product'}})
        child = self.store.read(child['run_id'])
        calls = []
        def run_child(prompt, branch):
            calls.append(prompt)
            return child
        self.assertEqual(proje.yurut(self.store, self.parent, planla=lambda *_: self.fail('unexpected replan'),
                                    cocuk_calistir=run_child, birlestir=lambda *_: True, simdi=lambda: 1001), 78)
        state = json.loads(self.state_path.read_text())
        self.assertEqual(len(calls), 1)
        self.assertIn('ozellik f2:', calls[0])
        self.assertEqual((state['checkpoint']['feature'], state['checkpoint']['status']), ('f2', 'waiting'))
        self.assertEqual(state['checkpoint']['final_digest'], plan_digest(child['final_result']))
        self.assertNotEqual(state['checkpoint']['id'], 'a' * 32)
        self.assertEqual([f['durum'] for f in state['ozellikler']], ['uygulandi', 'uygulandi'])
        self.assertTrue(proje.supervision_waiting(self.store, self.store.read(self.parent)))
        state.pop('checkpoint')
        atomic_json(self.state_path, state)
        with self.assertRaisesRegex(ValueError, 'supervision-checkpoint-invalid'):
            proje.supervision_waiting(self.store, self.store.read(self.parent))

    def test_release_is_bound_idempotent_and_does_not_renew_budget_or_deadline(self):
        before = self.store.read(self.parent)
        with patch('sonuc.approved', return_value=True):
            verified = proje.verify_step(self.store, self.parent, 'a' * 32, note='Independent product and cost checks passed.')
            self.assertEqual(verified['status'], 'verified')
            self.assertEqual(proje.verify_step(self.store, self.parent, 'a' * 32), verified)
        after = self.store.read(self.parent)
        for key in ('created', 'budget_cap_usd', 'operator_budget_usd', 'budget_allocation', 'review_limits'):
            self.assertEqual(after[key], before[key])
        self.assertFalse(proje.supervision_waiting(self.store, after))
        self.assertEqual(after['reason'], 'project-step-resume-required')
        self.assertEqual(len(after['operator_interventions']), 1)
        with patch('kosu._baslat') as restart:
            self.assertEqual(__import__('kosu').revive(self.store, LIB.parent), [])
            restart.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'project-step-verification-required'), __import__('kosu').launch_guard(self.store, self.child):
            self.fail('child resumed before explicit parent start')

    def test_stale_missing_acceptance_changed_result_and_worker_release_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'stale-checkpoint'):
            proje.verify_step(self.store, self.parent, 'b' * 32)
        with patch('sonuc.approved', return_value=False), self.assertRaisesRegex(ValueError, 'acceptance-required'):
            proje.verify_step(self.store, self.parent, 'a' * 32)
        self.store.update(self.child, final_result={'status': 'PASS', 'revision': {'digest': 'changed-product'}})
        with patch('sonuc.approved', return_value=True), self.assertRaisesRegex(ValueError, 'result-changed'):
            proje.verify_step(self.store, self.parent, 'a' * 32)
        with patch.dict(os.environ, {'CADRE_RUN_ID': self.child}), self.assertRaisesRegex(ValueError, 'operator-command-required'):
            proje.verify_step(self.store, self.parent, 'a' * 32)
        self.assertTrue(proje.supervision_waiting(self.store, self.store.read(self.parent)))

    def test_active_owner_cannot_be_released(self):
        from kosu import FileLock
        with FileLock(self.store.path(self.parent) / '.owner.lock'), self.assertRaisesRegex(ValueError, 'owner-active'):
            proje.verify_step(self.store, self.parent, 'a' * 32)

    def test_unsettled_call_blocks_success_release_while_future_guarantee_is_preserved(self):
        from butce_kapisi import BudgetLedger
        ledger = BudgetLedger(self.home / 'zincir/request-budget.db')
        with ledger.transaction() as con:
            con.execute('insert into reservation(id,provider,period,created,reserved,metadata) values(?,?,?,?,?,?)',
                        ('call', 'openrouter', 'test', 1000, 5000, json.dumps({'run_id': self.child})))
            con.execute('insert into reservation(id,provider,period,created,reserved,metadata) values(?,?,?,?,?,?)',
                        ('future-final', 'openrouter', 'test', 1000, 10000, json.dumps({'run_id': self.parent, 'guvence': True})))
        with patch('sonuc.approved', return_value=True):
            with self.assertRaisesRegex(ValueError, 'unsettled-cost'):
                proje.verify_step(self.store, self.parent, 'a' * 32)
            ledger.settle('call', 5000, 'known-receipt')
            self.assertEqual(proje.verify_step(self.store, self.parent, 'a' * 32)['status'], 'verified')
        with ledger.transaction() as con:
            self.assertIsNone(con.execute("select actual from reservation where id='future-final'").fetchone()[0])

    def test_failed_step_needs_an_explicit_retry_record_and_keeps_the_same_feature(self):
        from kosu import atomic_json, plan_digest
        final = {'status': 'FAIL', 'reason': 'review-not-passed'}
        self.store.update(self.child, state='failed', final_result=final)
        state = json.loads(self.state_path.read_text())
        state['checkpoint'].update(success=False, final_digest=plan_digest(final))
        state['ozellikler'][0]['durum'] = 'bekliyor'
        atomic_json(self.state_path, state)
        with self.assertRaisesRegex(ValueError, 'retry-evidence-required'):
            proje.verify_step(self.store, self.parent, 'a' * 32, 'retry')
        with self.assertRaisesRegex(ValueError, 'acceptance-required'):
            proje.verify_step(self.store, self.parent, 'a' * 32)
        result = proje.verify_step(self.store, self.parent, 'a' * 32, 'retry', 'Fixed the recorded parser failure; targeted regression passed.')
        self.assertEqual(result['status'], 'retry-approved')
        self.assertEqual(json.loads(self.state_path.read_text())['retry_feature'], 'f1')


class SahteStore:
    """The face of RunStore the project loop uses: record, path, update."""
    def __init__(self, kok, istek):
        self.kok = Path(kok)
        (self.kok / 'runs' / 'ana').mkdir(parents=True)
        (self.kok / 'runs' / 'ana' / 'prompt.md').write_text(istek)
        self.kayitlar = {'ana': {'run_id': 'ana', 'prompt': str(self.kok / 'runs' / 'ana' / 'prompt.md'), 'source_sha': 'abc'}}

    def path(self, rid):
        return self.kok / 'runs' / rid

    def read(self, rid):
        return dict(self.kayitlar[rid])

    def update(self, rid, **d):
        self.kayitlar[rid].update(d)
        return dict(self.kayitlar[rid])


class YurutTest(unittest.TestCase):
    """The project loop with a fake planner / child run / merge: selection, state transitions, re-plan, final acceptance, report."""
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory(prefix='proje-')
        self.addCleanup(self.tmp.cleanup)
        self.store = SahteStore(self.tmp.name, 'sifirdan kucuk bir not araci yaz')
        self.planlar = []
        self.cocuklar = []
        self.saat = [1000.0]

    def planla(self, *listeler):
        sira = list(listeler)
        def f(istem, gorev_id):
            self.planlar.append((gorev_id, istem))
            return cikti(*sira.pop(0)) if sira else 'plan yok'
        return f

    def cocuk(self, sonuclar):
        """results: feature id (or 'son-kabul') -> list of success bools (consumed in order)."""
        def f(istem, base_ref):
            anahtar = 'son-kabul' if istem.startswith('PROJE SON KABUL') else istem.split('ozellik ', 1)[1].split(' ', 1)[0].rstrip(':')
            basari = sonuclar[anahtar].pop(0)
            rid = f'c{len(self.cocuklar) + 1}'
            self.cocuklar.append((anahtar, base_ref))
            self.saat[0] += 600
            return {'run_id': rid, 'state': 'completed' if basari else 'failed', 'worktree': f'/wt/{rid}',
                    'final_result': {'status': 'PASS' if basari else 'FAIL', 'reason': None if basari else 'review-not-passed'},
                    'budget_spent_usd': 0.5}
        return f

    def yurut(self, planla, cocuk):
        return proje.yurut(self.store, 'ana', planla=planla, cocuk_calistir=cocuk, birlestir=lambda dal, k: k['state'] == 'completed',
                           simdi=lambda: self.saat[0])

    def durum(self):
        return json.loads((self.store.path('ana') / 'proje.json').read_text())

    def test_supervised_project_stops_after_one_feature_and_restart_keeps_the_gate(self):
        self.store.update('ana', supervision={'version': 1, 'mode': 'step'})
        child = self.cocuk({'f1': [True], 'f2': [True], 'son-kabul': [True]})
        rc = self.yurut(self.planla([oz('f1', oncelik=1), oz('f2', ['f1'])]), child)
        self.assertEqual(rc, 78)
        self.assertEqual([item[0] for item in self.cocuklar], ['f1'])
        checkpoint = self.durum()['checkpoint']
        self.assertEqual((checkpoint['feature'], checkpoint['status'], checkpoint['success']), ('f1', 'waiting', True))
        self.assertEqual(self.yurut(self.planla(), child), 78)
        self.assertEqual([item[0] for item in self.cocuklar], ['f1'])
        self.assertEqual(self.durum()['checkpoint'], checkpoint)

    def test_supervision_stops_at_the_first_failed_attempt_without_retry_or_replanning(self):
        self.store.update('ana', supervision={'version': 1, 'mode': 'step'})
        rc = self.yurut(self.planla([oz('f1')]), self.cocuk({'f1': [False, True], 'son-kabul': [True]}))
        self.assertEqual(rc, 78)
        self.assertEqual([item[0] for item in self.cocuklar], ['f1'])
        self.assertFalse(self.durum()['checkpoint']['success'])
        self.assertEqual(len(self.planlar), 1)

    def test_supervision_requires_verification_before_the_whole_project_final_review(self):
        self.store.update('ana', supervision={'version': 1, 'mode': 'step'})
        rc = self.yurut(self.planla([oz('f1')]), self.cocuk({'f1': [True], 'son-kabul': [True]}))
        self.assertEqual(rc, 78)
        self.assertEqual([item[0] for item in self.cocuklar], ['f1'])
        self.assertNotEqual(self.durum()['durum'], 'tamam')

    def test_supervision_rejects_a_cached_feature_combining_backlog_cards_before_execution(self):
        from kosu import atomic_json
        self.store.update('ana', supervision=dict(proje.STEP_POLICY))
        state = proje._yeni_durum('ana', 'Complete the backlog.', self.saat[0])
        state['ozellikler'] = [{**oz('mixed'), 'source_items': [{'id': 'B001'}, {'id': 'B002'}]}]
        atomic_json(self.store.path('ana') / 'proje.json', state)
        with self.assertRaisesRegex(ValueError, 'project-step-multiple-source-items'):
            self.yurut(self.planla(), self.cocuk({'mixed': [True]}))
        self.assertEqual(self.cocuklar, [])
        self.assertEqual(self.planlar, [])

    def test_uc_ozellik_ve_son_kabul_gecerse_proje_tamamlanir(self):
        rc = self.yurut(self.planla([oz('f1', oncelik=1), oz('f2', ['f1']), oz('f3', ['f1'])]),
                        self.cocuk({'f1': [True], 'f2': [True], 'f3': [True], 'son-kabul': [True]}))
        d = self.durum()
        self.assertEqual((rc, d['durum'], d['neden']), (0, 'tamam', 'son-kabul-gecti'))
        self.assertEqual([c[0] for c in self.cocuklar], ['f1', 'f2', 'f3', 'son-kabul'])
        self.assertTrue(all(c[1] == d['ara_dal'] for c in self.cocuklar), 'her cocuk ara dalin ucundan baslar')
        self.assertTrue(d['ara_dal'].startswith('cadre/proje-'))
        self.assertEqual(self.store.read('ana')['final_result']['status'], 'PASS', 'son kabul cocugunun kaniti ana kosuya gecer')
        self.assertEqual(self.store.read('ana')['worktree'], '/wt/c4')
        self.assertIn('3 gecti', (self.store.path('ana') / 'proje-raporu.md').read_text())
        self.assertAlmostEqual(d['harcama_usd'], 2.0)

    def test_iki_kez_dusen_ozellik_takilir_digerleri_surer(self):
        rc = self.yurut(self.planla([oz('f1', oncelik=1), oz('f2')], [oz('f2')]),
                        self.cocuk({'f1': [False, False], 'f2': [True], 'son-kabul': [True]}))
        d = self.durum()
        g = {f['id']: f for f in d['ozellikler']}
        self.assertEqual((g['f1']['durum'], g['f1'].get('son_kabulde'), g['f1']['deneme'], g['f2']['durum']), ('gecti', True, 2, 'gecti'))
        self.assertEqual(rc, 0)
        self.assertEqual([p[0] for p in self.planlar], ['proje-liste'])
        self.assertEqual(d['plan_degerlendirmeleri'][-1]['decision'], 'unchanged')
        self.assertIn('f1', d['plan_degerlendirmeleri'][-1]['evidence']['stalled'])

    def test_art_arda_uc_basarisizlik_projeyi_durdurur(self):
        rc = self.yurut(self.planla([oz('f1', oncelik=1), oz('f2', oncelik=2), oz('f3', oncelik=3)]),
                        self.cocuk({'f1': [False, False], 'f2': [False, False], 'f3': [False, False]}))
        d = self.durum()
        self.assertEqual((rc, d['durum'], d['neden']), (1, 'durdu', 'ilerleme-yok'))
        self.assertEqual(len(self.cocuklar), 3)

    def test_son_kabul_duserse_duzeltme_ozelligi_eklenir_ve_ikinci_turda_gecer(self):
        rc = self.yurut(self.planla([oz('f1')], [oz('f1'), oz('d1', ['f1'])]),
                        self.cocuk({'f1': [True], 'd1': [True], 'son-kabul': [False, True]}))
        d = self.durum()
        self.assertEqual((rc, d['durum'], d['son_kabul_turu']), (0, 'tamam', 1))
        self.assertEqual([c[0] for c in self.cocuklar], ['f1', 'son-kabul', 'd1', 'son-kabul'])

    def test_kayitli_durumdan_devam_liste_yeniden_uretilmez(self):
        (self.store.path('ana') / 'proje.json').write_text(json.dumps({
            'kimlik': 'x', 'istek_ozeti': 'arac', 'ara_dal': 'cadre/proje-x', 'baslangic': 1000.0, 'son_gecti': 1000.0,
            'ardisik_basarisiz': 0, 'son_kabul_turu': 0, 'durum': 'calisiyor', 'neden': '', 'harcama_usd': 0.0, 'bitirilen': 1,
            'ozellikler': [oz('f1', durum='gecti'), oz('f2', ['f1'])]}))
        rc = self.yurut(self.planla(), self.cocuk({'f2': [True], 'son-kabul': [True]}))
        self.assertEqual(rc, 0)
        self.assertEqual([c[0] for c in self.cocuklar], ['f2', 'son-kabul'])
        self.assertEqual(self.planlar, [], 'devam eden projede planlayici cagrilmaz')

    def test_iptal_bayragi_projeyi_durdurur(self):
        (self.store.path('ana') / 'cancel.flag').write_text('')
        rc = self.yurut(self.planla([oz('f1')]), self.cocuk({}))
        self.assertEqual((rc, self.durum()['neden']), (1, 'iptal'))
        self.assertEqual(self.cocuklar, [])

    def test_gecersiz_ozellik_listesi_projeyi_baslatmaz(self):
        rc = self.yurut(self.planla(), self.cocuk({}))
        self.assertEqual((rc, self.durum()['neden']), (1, 'liste-uretilemedi'))

    def test_takilan_ozellik_son_kabulde_zorunlu_tutulur_ve_ancak_gecerse_proje_tamam(self):
        istemler = []
        normal = self.cocuk({'f1': [False, False], 'f2': [True], 'son-kabul': [True]})
        def cocuk(istem, dal):
            istemler.append(istem)
            return normal(istem, dal)
        f1 = {**oz('f1', oncelik=1), 'qa_komut': 'python3 -m pytest -q tests/test_f1.py'}
        rc = self.yurut(self.planla([f1, oz('f2')], [oz('f2')]), cocuk)
        son = istemler[-1]
        self.assertTrue(son.startswith('PROJE SON KABUL'))
        for parca in ('TAKILAN OZELLIKLER', 'f1 testi yesil', 'tests/test_f1.py'):
            self.assertIn(parca, son)
        d = self.durum()
        f = {g['id']: g for g in d['ozellikler']}['f1']
        self.assertEqual((rc, d['durum'], f['durum'], f.get('son_kabulde')), (0, 'tamam', 'gecti', True))
        self.assertIn('(son kabulde)', (self.store.path('ana') / 'proje-raporu.md').read_text())

    def test_uzun_istegin_sonu_planlayiciya_yeniden_plana_ve_son_kabule_ulasir(self):
        yol = self.store.kayitlar['ana']['prompt']
        Path(yol).write_text('sifirdan arac yaz\n' + 'x' * 20000 + '\nZORUNLU-SON-MADDE: disa aktarma\n')
        istemler = []
        normal = self.cocuk({'f1': [True], 'son-kabul': [False, True], 'd1': [True]})
        def cocuk(istem, dal):
            istemler.append(istem)
            return normal(istem, dal)
        rc = self.yurut(self.planla([oz('f1')], [oz('f1'), oz('d1', ['f1'])]), cocuk)
        self.assertEqual(rc, 0)
        self.assertEqual([g for g, _ in self.planlar], ['proje-liste', 'proje-yeniden'])
        self.assertTrue(all('ZORUNLU-SON-MADDE' in p for _, p in self.planlar))
        self.assertIn('ZORUNLU-SON-MADDE', istemler[-1])
        ozellik = istemler[0]
        self.assertNotIn('ZORUNLU-SON-MADDE', ozellik)
        self.assertIn(f'istegin tamami 20051 karakter: {yol}', ozellik)

    def test_buyuk_ek_belge_projeyi_durdurmaz(self):
        rc = proje.yurut(self.store, 'ana', planla=self.planla([oz('f1')]), cocuk_calistir=self.cocuk({'f1': [True], 'son-kabul': [True]}),
                         birlestir=lambda dal, k: k['state'] == 'completed', simdi=lambda: self.saat[0], ekler='x' * (proje.PLAN_ISTEK_TAVANI + 1))
        self.assertEqual(rc, 0)

    def test_tavani_asan_istek_kirpilmaz_proje_nedenle_durur(self):
        Path(self.store.kayitlar['ana']['prompt']).write_text('x' * (proje.PLAN_ISTEK_TAVANI + 1))
        rc = self.yurut(self.planla([oz('f1')]), self.cocuk({}))
        self.assertEqual(rc, 1)
        self.assertTrue(self.durum()['neden'].startswith('istek-cok-uzun'))
        self.assertEqual((self.planlar, self.cocuklar), ([], []))



def git(repo, *args):
    import subprocess
    return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=f', '-c', 'user.email=f@x', *args],
                          check=True, capture_output=True, text=True, timeout=30).stdout.strip()


_BIRLESTIR = proje.birlestir_ff


def izinli_birlestir(repo, dal, child, *, store=None, run_id=''):
    from kosu import RunStore
    if store is None:
        store = RunStore(Path(repo).parent / 'operator-fixture')
        run_id = store.accept('git-fixture', 'transfer fixture')['run_id']
    try:
        return _BIRLESTIR(repo, dal, child, store=store, run_id=run_id)
    except PermissionError as exc:
        if str(exc) != 'git-transfer-permission-required':
            raise
    action = store.read(run_id)['project_git_action']
    first = proje.git_aktarim_onayi(store, run_id, action['token'])
    proje.git_aktarim_onayi(store, run_id, action['token'], first['confirmation'])
    return _BIRLESTIR(repo, dal, child, store=store, run_id=run_id)


class ProjeGitTest(unittest.TestCase):
    """G-P3 intermediate branch: only cadre/proje-* is written; the child product fast-forwards (CAS); the canonical branch and a dirty product are rejected."""
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory(prefix='proje-git-')
        self.addCleanup(self.tmp.cleanup)
        self.kok = Path(self.tmp.name).resolve()
        self.repo = self.kok / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '-b', 'main')
        (self.repo / 'a.txt').write_text('v1\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'base')
        self.taban = git(self.repo, 'rev-parse', 'HEAD')

    def cocuk_dali(self, ad, baslangic, icerik, kirli=False):
        wt = self.kok / ad
        git(self.repo, 'worktree', 'add', '-q', '-b', f'cadre/{ad}', str(wt), baslangic)
        (wt / 'a.txt').write_text(icerik)
        git(wt, 'commit', '-qam', ad)
        if kirli:
            (wt / 'a.txt').write_text(icerik + 'commit edilmemis\n')
        return {'run_id': ad, 'state': 'completed', 'worktree': str(wt)}

    def test_ara_dal_kabul_edilen_kaynaktan_acilir_ve_kanonik_dal_reddedilir(self):
        proje.ara_dal_hazirla(self.repo, 'cadre/proje-x', self.taban)
        self.assertEqual(git(self.repo, 'rev-parse', 'cadre/proje-x'), self.taban)
        proje.ara_dal_hazirla(self.repo, 'cadre/proje-x', 'HEAD')   # an existing branch stays in place
        with patch.object(proje, 'kabul_goruntusu', side_effect=AssertionError('existing branch must not request a snapshot')):
            proje.ara_dal_hazirla(self.repo, 'cadre/proje-x', self.taban, {'carried': True, 'patch': 'carried.patch'})
        self.assertEqual(git(self.repo, 'rev-parse', 'cadre/proje-x'), self.taban)
        for kotu in ('main', 'feature/x', 'cadre/baska'):
            with self.subTest(kotu), self.assertRaises(ValueError):
                proje.ara_dal_hazirla(self.repo, kotu, self.taban)

    def test_istek_ekleri_yalniz_belge_alir_hassas_ve_yasaklanani_almaz(self):
        for ad, icerik in (('.env', 'SAHTE_SIR=1\n'), ('BACKLOG.md', '- madde-1\n'), ('README.md', 'icerik-r\n'),
                           ('secrets.md', 'icerik-s\n'), ('config.json', '{"x": 1}\n')):
            (self.repo / ad).write_text(icerik)
        git(self.repo, 'add', '-f', '.')
        git(self.repo, 'commit', '-qm', 'belgeler')
        ek = proje.istek_ekleri(self.repo, 'main', "BACKLOG.md maddelerini uygula. .env dosyasini okuma, README.md'yi okuma; "
                                                   "secrets.md ve config.json'u kullan")
        self.assertIn('madde-1', ek)
        for yok in ('SAHTE_SIR', 'icerik-r', 'icerik-s', '"x"', 'v1'):
            self.assertNotIn(yok, ek)
        (self.repo / 'NOTLAR.md').write_text('icerik-n\n')
        (self.repo / 'DEVIR.md').write_text('icerik-d\n')
        (self.repo / 'BUYUK.md').write_text('B' * (proje.EK_DOSYA_TAVANI + 5000) + 'SON-SATIR\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'notlar')
        ek = proje.istek_ekleri(self.repo, 'main', "Once NOTLAR.md dosyasini okumalisin. DEVIR.md dosyasini okumayin. BUYUK.md'deki sifre "
                                                   "degistirme maddesini uygula.")
        self.assertIn('icerik-n', ek)
        self.assertNotIn('icerik-d', ek)
        self.assertNotIn('SON-SATIR', ek)
        self.assertIn(f'dosyanin tamami {proje.EK_DOSYA_TAVANI + 5010} karakter: BUYUK.md', ek)

    def test_cocuk_urunu_hizli_ileri_birlesir(self):
        proje.ara_dal_hazirla(self.repo, 'cadre/proje-x', self.taban)
        k = self.cocuk_dali('c1', 'cadre/proje-x', 'ozellik 1\n')
        self.assertTrue(izinli_birlestir(self.repo, 'cadre/proje-x', k))
        self.assertEqual(git(self.repo, 'show', 'cadre/proje-x:a.txt'), 'ozellik 1')
        self.assertEqual(git(self.repo, 'rev-parse', 'main'), self.taban, 'kanonik dala dokunulmaz')

    def test_harness_artigi_kirli_sayilmaz_ama_commit_edilmemis_urun_sayilir(self):
        """B234 (25.09 live B203b): the child finished PASS but the merge was rejected because the engine's `.gorevler.json` leftover stayed in the worktree;
        the main run counted f1 as failed and restarted the same feature (an endless loop). The dirt measure is the same as the engine's (URUN_HARIC)."""
        proje.ara_dal_hazirla(self.repo, 'cadre/proje-x', self.taban)
        k = self.cocuk_dali('c1', 'cadre/proje-x', 'ozellik 1\n')
        wt = Path(k['worktree'])
        (wt / '.gorevler.json').write_text('[]')
        (wt / '__pycache__').mkdir(); (wt / '__pycache__' / 'a.pyc').write_bytes(b'x')
        (wt / '.cadre-plan').mkdir(); (wt / '.cadre-plan' / 'brifing.md').write_text('x')
        self.assertTrue(izinli_birlestir(self.repo, 'cadre/proje-x', k), 'harness artiklari urun degildir')
        self.assertEqual(git(self.repo, 'show', 'cadre/proje-x:a.txt'), 'ozellik 1')
        k2 = self.cocuk_dali('c2', 'cadre/proje-x', 'ozellik 2\n')
        (Path(k2['worktree']) / 'yeni.py').write_text('print(1)\n')   # an uncommitted REAL product: not merged
        with self.assertRaisesRegex(PermissionError, 'commit-permission-required'):
            izinli_birlestir(self.repo, 'cadre/proje-x', k2)

    def test_ara_dal_ilerlediyse_ya_da_urun_kirliyse_birlesmez(self):
        proje.ara_dal_hazirla(self.repo, 'cadre/proje-x', self.taban)
        eski = self.cocuk_dali('c1', 'cadre/proje-x', 'eski\n')
        yeni = self.cocuk_dali('c2', 'cadre/proje-x', 'yeni\n')
        self.assertTrue(izinli_birlestir(self.repo, 'cadre/proje-x', yeni))
        self.assertFalse(izinli_birlestir(self.repo, 'cadre/proje-x', eski), 'ara dalin ucundan baslamayan urun birlesmez')
        kirli = self.cocuk_dali('c3', 'cadre/proje-x', 'kirli\n', kirli=True)
        with self.assertRaisesRegex(PermissionError, 'commit-permission-required'):
            izinli_birlestir(self.repo, 'cadre/proje-x', kirli)
        self.assertFalse(izinli_birlestir(self.repo, 'cadre/proje-x', {**yeni, 'state': 'failed'}))

    def test_turetilmis_proje_ayarlari_kopyalar(self):
        home = self.kok / 'qe'
        ana = home / 'projects' / 'p'
        ana.mkdir(parents=True)
        for ad, icerik in (('repo-path', str(self.repo)), ('base-branch', 'main'), ('qa-komut', 'make check'), ('POLICY.md', 'kural'),
                           ('kosu-tavan-usd', '5'), ('kosu-tavan-zor-usd', '10'), ('bicim-komut', 'ruff format')):
            (ana / ad).write_text(icerik)
        ad = proje.turet_proje(home, 'p', 'ab12')
        self.assertEqual(ad, 'p--pab12')
        self.assertEqual((home / 'projects' / ad / 'qa-komut').read_text(), 'make check')
        self.assertEqual((home / 'projects' / ad / 'repo-path').read_text(), str(self.repo))
        # B272 (30.09 rehearsal): the hard cap and the format command are inherited too; otherwise a child run falls to the default $5 and the format repair is lost
        for ad2, icerik in (('kosu-tavan-usd', '5'), ('kosu-tavan-zor-usd', '10'), ('bicim-komut', 'ruff format')):
            self.assertEqual((home / 'projects' / ad / ad2).read_text(), icerik, ad2)

    def test_transfer_requires_two_operator_cli_messages_and_never_reuses_an_approval(self):
        import os
        import subprocess
        from kosu import RunStore
        store = RunStore(self.kok / 'gate')
        rid = store.accept('fixture', 'transfer')['run_id']
        proje.ara_dal_hazirla(self.repo, 'cadre/proje-x', self.taban)
        child = self.cocuk_dali('c1', 'cadre/proje-x', 'accepted\n')
        with self.assertRaisesRegex(PermissionError, 'git-transfer-permission-required'):
            proje.birlestir_ff(self.repo, 'cadre/proje-x', child, store=store, run_id=rid)
        action = store.read(rid)['project_git_action']
        self.assertEqual(action['command'], ['git', '-C', str(self.repo), 'update-ref', 'refs/heads/cadre/proje-x',
                                           git(Path(child['worktree']), 'rev-parse', 'HEAD'), self.taban])
        env = {k: v for k, v in os.environ.items() if not k.startswith(('CADRE_', 'OPENCODE_'))}
        env['CADRE_HOME'] = str(store.home)
        command = [sys.executable, str(LIB / 'kosu.py'), 'project-git-approve', rid, action['token']]
        denied = subprocess.run(command, env={**env, 'CADRE_RUN_ID': rid}, capture_output=True, text=True, timeout=10)
        self.assertEqual(denied.returncode, 78)
        first = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(first.returncode, 0, first.stderr)
        confirmation = json.loads(first.stdout)['confirmation']
        with self.assertRaisesRegex(PermissionError, 'git-transfer-permission-required'):
            proje.birlestir_ff(self.repo, 'cadre/proje-x', child, store=store, run_id=rid)
        for suffix in ([], ['wrong']):
            denied = subprocess.run(command + suffix, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(denied.returncode, 78)
        self.assertEqual(git(self.repo, 'rev-parse', 'cadre/proje-x'), self.taban)
        second = subprocess.run(command + [confirmation], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(proje.birlestir_ff(self.repo, 'cadre/proje-x', child, store=RunStore(store.home), run_id=rid))
        self.assertEqual(store.read(rid)['project_git_action']['status'], 'consumed')
        another = self.cocuk_dali('c2', 'cadre/proje-x', 'next\n')
        with self.assertRaisesRegex(PermissionError, 'git-transfer-permission-required'):
            proje.birlestir_ff(self.repo, 'cadre/proje-x', another, store=store, run_id=rid)
        denied = subprocess.run(command + [confirmation], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(denied.returncode, 78)

    def test_transfer_race_and_interrupted_consumption_never_run_the_command_twice(self):
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        from kosu import RunStore
        store = RunStore(self.kok / 'race')
        rid = store.accept('fixture', 'transfer')['run_id']
        ref = 'refs/heads/cadre/proje-x'
        proje.ara_dal_hazirla(self.repo, 'cadre/proje-x', self.taban)
        child = self.cocuk_dali('c1', 'cadre/proje-x', 'next\n')
        new = git(child['worktree'], 'rev-parse', 'HEAD')
        with self.assertRaises(PermissionError):
            proje.git_aktar(store, rid, self.repo, ref, new, self.taban)
        action = store.read(rid)['project_git_action']
        first = proje.git_aktarim_onayi(store, rid, action['token'])
        proje.git_aktarim_onayi(store, rid, action['token'], first['confirmation'])
        with patch('kosu.atomic_json', side_effect=OSError('disk')), patch.object(proje, '_git') as command:
            with self.assertRaises(OSError):
                proje.git_aktar(store, rid, self.repo, ref, new, self.taban)
            command.assert_not_called()
        self.assertEqual(store.read(rid)['project_git_action']['status'], 'approved')
        with patch.object(proje, '_git', side_effect=OSError('interrupted')) as command:
            with self.assertRaises(OSError):
                proje.git_aktar(store, rid, self.repo, ref, new, self.taban)
            self.assertEqual(command.call_count, 1)
        self.assertEqual(store.read(rid)['project_git_action']['status'], 'consumed')
        with self.assertRaises(PermissionError):
            proje.git_aktar(store, rid, self.repo, ref, new, self.taban)
        action = store.read(rid)['project_git_action']
        first = proje.git_aktarim_onayi(store, rid, action['token'])
        proje.git_aktarim_onayi(store, rid, action['token'], first['confirmation'])
        def transfer():
            try:
                return proje.git_aktar(RunStore(store.home), rid, self.repo, ref, new, self.taban)
            except PermissionError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(lambda _: transfer(), range(2))), [False, True])
        self.assertEqual(git(self.repo, 'rev-parse', ref), new)

    def test_stale_plan_and_terminal_actions_cannot_use_the_second_confirmation(self):
        from kosu import RunStore
        store = RunStore(self.kok / 'stale')
        rid = store.accept('fixture', 'transfer')['run_id']
        proje.ara_dal_hazirla(self.repo, 'cadre/proje-x', self.taban)
        child = self.cocuk_dali('c1', 'cadre/proje-x', 'next\n')
        with self.assertRaises(PermissionError):
            proje.birlestir_ff(self.repo, 'cadre/proje-x', child, store=store, run_id=rid)
        action = store.read(rid)['project_git_action']
        first = proje.git_aktarim_onayi(store, rid, action['token'])
        store.save_plan(rid, {'is_id': rid, 'kind': 'project', 'gorevler': [], 'project_features': [oz('f1')]})
        with self.assertRaisesRegex(ValueError, 'stale-git-action'):
            proje.git_aktarim_onayi(store, rid, action['token'], first['confirmation'])
        for state in ('cancelled', 'failed', 'completed'):
            store.update(rid, state=state)
            with self.assertRaises(ValueError):
                proje.git_aktarim_onayi(store, rid, action['token'], first['confirmation'])
        self.assertEqual(git(self.repo, 'rev-parse', 'cadre/proje-x'), self.taban)



class DiriltmeTest(unittest.TestCase):
    """G-P4: the main project run continues the child run; the guard does not revive it independently, and closes the child if the main one finished."""
    def test_cocuk_bagimsiz_diriltilmez_ana_bitince_kapanir(self):
        import tempfile
        from kosu import FileLock, RunStore, revive
        tmp = tempfile.TemporaryDirectory(prefix='proje-dirilt-')
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        store = RunStore(home)
        root = home / 'kok'
        (root / 'bin').mkdir(parents=True)
        (root / 'bin' / 'cadre-dispatch.sh').write_text('echo dispatch >> "$CADRE_RUN_DIR/cagri"\nexit 1\n')
        ana = store.accept('p', 'ana')
        store.update(ana['run_id'], state='running', phase='dispatch')
        cocuk = store.accept('p--pab', 'cocuk')
        store.update(cocuk['run_id'], state='running', phase='dispatch', owner_pid=999999, proje_ust=ana['run_id'])
        with FileLock(store.path(ana['run_id']) / '.owner.lock'):   # the main run's coordinator is alive
            self.assertEqual(revive(store, root), [])
        self.assertEqual(store.read(cocuk['run_id'])['state'], 'running')
        self.assertFalse((store.path(cocuk['run_id']) / 'cagri').exists())
        store.update(ana['run_id'], state='failed')
        revive(store, root)
        son = store.read(cocuk['run_id'])
        self.assertEqual((son['state'], son.get('reason')), ('cancelled', 'proje-ust-bitti'))



class UygunTest(unittest.TestCase):
    def test_proje_kipi_ultra_ve_planlayici_onayi_ister(self):
        self.assertTrue(proje.uygun({'kapsam': {'seviye': 'ultra', 'planlayici': 90}}))
        self.assertFalse(proje.uygun({'kapsam': {'seviye': 'ultra', 'planlayici': 20}}), 'yalniz sozcuk (kural 90) yetmez')
        self.assertFalse(proje.uygun({'kapsam': {'seviye': 'ultra', 'planlayici': None}}))
        self.assertFalse(proje.uygun({'kapsam': {'seviye': 'cok-zor', 'planlayici': 80}}))
        self.assertFalse(proje.uygun({'kapsam': {'seviye': 'ultra', 'planlayici': 95}, 'proje_ust': 'x'}))

    def test_supervision_uses_project_steps_for_small_code_tasks_only(self):
        record = {'supervision': dict(proje.STEP_POLICY), 'job_type': 'code',
                  'kapsam': {'seviye': 'kolay', 'planlayici': 10}}
        self.assertTrue(proje.uygun(record))
        self.assertFalse(proje.uygun({**record, 'proje_ust': 'parent'}))
        for kind in ('review', 'research'):
            with self.subTest(kind=kind):
                self.assertFalse(proje.uygun({**record, 'job_type': kind}))

    def test_supervised_project_planning_preserves_protocol_and_separate_card_instruction(self):
        from unittest.mock import Mock
        store = Mock()
        store.read.return_value = {'supervision': dict(proje.STEP_POLICY)}
        prompt = 'PROJE LISTESI\nOriginal scope\n' + json.dumps({'ozellikler': []})
        with patch('kosu.capture_source_plan', return_value='result') as capture:
            self.assertEqual(proje._planlayici_cagir(store, 'parent', 'model', prompt, 'proje-liste'), 'result')
        forwarded = capture.call_args.args[4]
        self.assertEqual(forwarded.splitlines()[0], 'PROJE LISTESI')
        self.assertEqual(json.loads(forwarded.splitlines()[-1]), {'ozellikler': []})
        self.assertIn('farkli backlog maddelerini', forwarded)
        self.assertIn('operator dogrulamasi', forwarded)


class BaslatUctanUcaTest(unittest.TestCase):
    """Real RunStore + real git: the intermediate branch, the derived project, child runs open from the intermediate branch, the product fast-forwards,
    the evidence of the final acceptance passes to the main run. Only the child's execution (execute_run) is fake: it opens a worktree and makes a commit."""
    def setUp(self):
        import os
        import tempfile
        from unittest.mock import patch
        self.tmp = tempfile.TemporaryDirectory(prefix='proje-e2e-')
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.repo = self.home / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '-b', 'main')
        (self.repo / 'README.md').write_text('bos\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'base')
        pd = self.home / 'projects' / 'p'
        pd.mkdir(parents=True)
        (pd / 'repo-path').write_text(str(self.repo))
        (pd / 'base-branch').write_text('main')
        (pd / 'qa-komut').write_text('exit 0')
        ortam = patch.dict(os.environ, {'CADRE_HOME': str(self.home)})
        ortam.start()
        self.addCleanup(ortam.stop)
        from kosu import RunStore
        self.store = RunStore(self.home)
        self.ana = self.store.accept('p', 'sifirdan kucuk bir not araci yaz')['run_id']
        self.calisan = []
        import qe_core as core
        from test_kosu_sessions import KATALOG
        from test_uctan_uca import FAKE_OPENCODE
        import time
        fake = self.home / 'plan-review-fixture'
        fake.write_text(FAKE_OPENCODE)
        fake.chmod(0o700)
        (self.home / 'models.json').write_text(json.dumps(KATALOG))
        budget = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'bugun': time.strftime('%Y-%m-%d'),
                  'go': {'db': 'ok', 'pay': 33.333334, 'kullanilabilir': 33.333334, 'pencere_acik': True, 'birim': 'puan'}}
        for manager in (patch.object(proje, 'birlestir_ff', side_effect=izinli_birlestir), patch.dict(os.environ, {'CADRE_REAL_OPENCODE': str(fake), 'CADRE_MODEL_KATALOG': str(self.home / 'models.json'),
                                               'OPENCODE_DB': str(self.home / 'absent.db')}),
                        patch.object(core, 'QE', self.home), patch.object(core.YON, 'QE', self.home),
                        patch.object(core, 'butce_json', return_value=budget), patch.object(core.YON, 'butce_oku', return_value=budget),
                        patch.object(core.YON, 'zincir', return_value=(['opencode-go/glm-5.3'], ['fixture plan review']))):
            manager.start()
            self.addCleanup(manager.stop)

    def calistir(self, basarisiz=()):
        def f(store, cid):
            k = store.read(cid)
            self.calisan.append(cid)
            istem = Path(k['prompt']).read_text()
            etiket = 'son-kabul' if istem.startswith('PROJE SON KABUL') else istem.split('ozellik ', 1)[1].split(':', 1)[0]
            if etiket in basarisiz:
                store.update(cid, state='retryable', reason='kabul FAIL')
                return 1
            wt = self.home / 'worktrees' / k['project'] / cid
            wt.parent.mkdir(parents=True, exist_ok=True)
            git(self.repo, 'worktree', 'add', '-q', '-b', f'cadre/{cid}', str(wt), k['source_sha'])
            (wt / f'{etiket}.txt').write_text(etiket + '\n')
            git(wt, 'add', '.')
            git(wt, 'commit', '-qm', etiket)
            store.update(cid, state='completed', worktree=str(wt), final_result={'status': 'PASS', 'etiket': etiket}, budget_spent_usd=0.25)
            return 0
        return f

    def planla(self, *listeler):
        sira = list(listeler)
        return lambda istem, gorev_id: cikti(*sira.pop(0)) if sira else 'yok'

    def test_alarm_approval_waits_and_real_resume_updates_project_and_active_child_views(self):
        import time
        import arayuz_veri as view
        from kosu import approve_spending
        normal = self.calistir()
        seen = []
        def alarm(store, cid):
            seen.append(cid)
            store.update(cid, state='retryable', reason='butce-bekleme', retry_after=time.time() + 60,
                         harcama_alarmi={'ts': time.time(), 'neden': 'fixture'})
            return 75
        self.assertEqual(proje.baslat(self.ana, 'p', planla=self.planla([oz('f1')]), calistir=alarm), 1)
        self.store.update(self.ana, state='retryable')
        before = (self.store.path(self.ana) / 'proje.json').read_bytes()
        approve_spending(self.store, self.ana)
        self.assertEqual((self.store.path(self.ana) / 'proje.json').read_bytes(), before)
        self.assertNotIn('calisiyor', proje.durum_ozeti(self.store, 'p'))
        self.store.update(self.ana, state='running')
        snapshots = []
        def resume(store, cid):
            parent = store.read(self.ana)
            state = json.loads((store.path(self.ana) / 'proje.json').read_text())
            self.assertEqual((state['durum'], state['neden'], parent.get('reason')), ('calisiyor', '', None))
            store.update(cid, state='running')
            files = {p: p.read_bytes() for p in store.path(self.ana).glob('*.json')}
            tree = view.proje_yapisi(self.home, parent, state)
            final = Path(store.read(cid)['prompt']).read_text().startswith('PROJE SON KABUL')
            node = tree[0][1] if final else tree[0][0]['cocuk'][0]
            self.assertIn(cid, str(node['ayrinti']))
            self.assertEqual(node['durum'], 'simdi')
            self.assertIn('calisiyor', proje.durum_ozeti(store, 'p'))
            self.assertEqual({p:p.read_bytes() for p in files}, files)
            snapshots.append((cid, final))
            return normal(store, cid)
        self.assertEqual(proje.baslat(self.ana, 'p', planla=self.planla(), calistir=resume), 0)
        self.assertEqual(snapshots[0], (seen[0], False))
        self.assertTrue(snapshots[-1][1])
        state = json.loads((self.store.path(self.ana) / 'proje.json').read_text())
        self.assertEqual((state['durum'], state['neden']), ('tamam', 'son-kabul-gecti'))

    def test_terminal_project_entry_never_reopens_or_performs_git_or_calls(self):
        from unittest.mock import Mock
        for status in ('completed', 'implemented', 'failed', 'cancelled'):
            with self.subTest(status=status):
                self.store.update(self.ana, state=status)
                before = (self.store.path(self.ana) / 'run.json').read_bytes()
                planner, executor = Mock(), Mock()
                with patch.object(proje, '_git', side_effect=AssertionError('terminal Git I/O')):
                    rc = proje.baslat(self.ana, 'p', planla=planner, calistir=executor)
                self.assertEqual(rc, 130 if status == 'cancelled' else 78)
                self.assertEqual((self.store.path(self.ana) / 'run.json').read_bytes(), before)
                planner.assert_not_called()
                executor.assert_not_called()
                self.assertFalse((self.store.path(self.ana) / 'proje.json').exists())

    def test_active_child_pointer_requires_owned_identity_and_never_reads_outside_runs(self):
        import arayuz_veri as view
        self.store.update(self.ana, state='running', project_active_feature='f1')
        foreign = self.store.accept('other', 'unrelated')['run_id']
        self.store.update(foreign, state='running', proje_ust='other', proje_ozellik='f1')
        outside = self.home / 'outside'
        outside.mkdir()
        (outside / 'run.json').write_text(json.dumps({'run_id':'linked', 'proje_ust':self.ana, 'proje_ozellik':'f1', 'state':'running'}))
        (self.home / 'runs/linked').symlink_to(outside, target_is_directory=True)
        state = {**proje._yeni_durum(self.ana, 'fixture', 1), 'ozellikler':[oz('f1')]}
        read = view.kayit_oku
        for pointer in (foreign, '../../outside', 'linked'):
            with self.subTest(pointer=pointer):
                record = self.store.update(self.ana, project_active_run=pointer)
                paths = []
                def checked(path):
                    paths.append(Path(path))
                    return read(path)
                with patch.object(view, 'kayit_oku', side_effect=checked):
                    tree = view.proje_yapisi(self.home, record, state)
                node = tree[0][0]['cocuk'][0]
                self.assertEqual(node['durum'], 'hata')
                self.assertNotIn('simdi', str(node))
                self.assertTrue(all(path.resolve().is_relative_to(self.home / 'runs') for path in paths))

    def test_project_plan_phases_use_recorded_stop_review_and_resume_without_reader_writes(self):
        import copy
        import arayuz_veri as view
        import qe_core as core
        from kosu import FileLock, review_plan
        def alarm(store, cid):
            store.update(cid, state='retryable', harcama_alarmi={'ts': 1}, reason='butce-bekleme')
            return 75
        self.assertEqual(proje.baslat(self.ana, 'p', planla=self.planla([oz('f1')]), calistir=alarm), 1)
        self.store.update(self.ana, state='retryable', harcama_onayi_ts=2)
        revision = self.store.plan_view(self.ana)['revision']
        self.store.change_plan(self.ana, 'change', revision, 'kabul aciklamasini duzelt')
        def observe(label):
            record = self.store.read(self.ana)
            data = json.loads((self.store.path(self.ana) / 'proje.json').read_text())
            before = {p:p.read_bytes() for p in self.store.path(self.ana).glob('*.json')}
            self.assertIn(label, proje.durum_ozeti(self.store, 'p'))
            node = view.gorev_dugumu(self.home, record)
            self.assertTrue(node['beklemede'])
            self.assertIn(label, str(node))
            self.assertEqual({p:p.read_bytes() for p in before}, before)
            return data
        observe('Duruş teyidi bekleniyor')
        child = self.store.read(self.ana)['project_active_run']
        with FileLock(self.store.path(child) / '.owner.lock'), self.assertRaisesRegex(ValueError, 'owner-active'):
            self.store.stop_plan(self.ana, 'change')
        observe('Duruş teyidi bekleniyor')
        self.store.stop_plan(self.ana, 'change')
        observe('Plan değişikliği bekleniyor')
        plan = copy.deepcopy(self.store.plan_view(self.ana)['history'][-1]['plan'])
        plan['project_features'][0]['aciklama'] += ' guncel'
        self.store.save_plan(self.ana, plan, expected=revision)
        observe('Plan incelemesi')
        capture = core.capture_opencode
        def reviewing(*args, **kwargs):
            observe('Plan incelemesi')
            return capture(*args, **kwargs)
        with patch.object(core, 'capture_opencode', side_effect=reviewing):
            review = review_plan(self.store, self.ana, revision + 1, os.environ['CADRE_REAL_OPENCODE'])
        self.assertEqual(review['status'], 'PASS', review)
        self.store.resume_plan(self.ana, 'change', revision + 1)
        data = observe('İzinli devam bekleniyor')
        self.assertEqual(data['durum'], 'durdu')
        self.assertEqual(self.store.read(self.ana)['state'], 'retryable')

    def test_kabulde_tasinan_kirli_is_ara_dalin_baslangicidir_ve_kullanicinin_agaci_degismez(self):
        (self.repo / 'README.md').write_text('kirli satir\n')
        (self.repo / 'alt').mkdir()
        (self.repo / 'alt' / 'yeni.py').write_text('print(1)\n')
        git(self.repo, 'add', 'README.md')
        before = {str(p.relative_to(self.repo)): p.read_bytes() for p in self.repo.rglob('*') if p.is_file()}
        ana = self.store.accept('p', 'sifirdan kucuk bir not araci yaz')['run_id']
        source = self.store.read(ana)['source']
        patch_bytes = Path(source['patch']).read_bytes()
        rc = proje.baslat(ana, 'p', planla=self.planla([oz('f1')]), calistir=self.calistir())
        self.assertEqual(rc, 78)
        self.assertEqual(self.calisan, [])
        self.assertEqual(self.store.read(ana)['reason'], 'proje-durdu: source-commit-permission-required')
        self.assertEqual(Path(source['patch']).read_bytes(), patch_bytes)
        self.assertEqual((Path(source['untracked']) / 'alt/yeni.py').read_text(), 'print(1)\n')
        after = {str(p.relative_to(self.repo)): p.read_bytes() for p in self.repo.rglob('*') if p.is_file()}
        self.assertEqual(before, after)


    def test_istekte_adi_gecen_dosya_ve_ilk_plan_ozellik_planlayicisina_ulasir(self):
        from kosu import atomic_json
        (self.repo / 'docs').mkdir()
        (self.repo / 'docs' / 'BACKLOG.md').write_text('# Backlog\n- MADDE-X: csv disa aktarma\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'backlog')
        ana = self.store.accept('p', "BACKLOG.md'deki isleri yap")['run_id']
        atomic_json(self.store.path(ana) / 'plan.json', {'is_id': 'initial', 'kapsam_gerekce': 'buyuk', 'gorevler': [
            {'id': 'g1', 'tanim': 'ILK-PLAN-GOREVI', 'kabul': 'k', 'dosyalar': ['README.md'],
             'model': 'auto', 'zorluk': 1, 'paralel_grup': 0, 'tahmin_dk': 1}]})
        istemler = []
        plan = self.planla([{**oz('f1'), 'source_tasks': ['g1']}])
        def planla(istem, gorev_id):
            istemler.append(istem)
            return plan(istem, gorev_id)
        self.assertEqual(proje.baslat(ana, 'p', planla=planla, calistir=self.calistir()), 0)
        for parca in ('--- docs/BACKLOG.md ---', 'MADDE-X: csv disa aktarma', 'ILK-PLAN-GOREVI'):
            self.assertIn(parca, istemler[0])
        self.assertNotIn('--- README.md ---', istemler[0])
        self.assertIn('MADDE-X', Path(self.store.read(self.calisan[-1])['prompt']).read_text(), 'son kabul de dosyayi gorur')
        self.assertEqual(self.store.plan_view(ana)['history'][-1]['plan']['project_features'][0]['source_tasks'], ['g1'])

    def test_permission_wait_preserves_accepted_child_and_resume_does_not_repeat_implementation(self):
        from kosu import RunStore, execute_run, revive
        with patch.object(proje, 'birlestir_ff', side_effect=_BIRLESTIR):
            self.assertEqual(proje.baslat(self.ana, 'p', planla=self.planla([oz('f1')]), calistir=self.calistir()), 78)
            d = json.loads((self.store.path(self.ana) / 'proje.json').read_text())
            self.assertEqual((d['neden'], d['bitirilen'], d['ozellikler'][0]['deneme'], d['son_kabul_turu']),
                             ('git-transfer-permission-required', 0, 0, 0))
            self.assertEqual(len(self.calisan), 1)
            child = self.store.read(self.calisan[0])
            self.assertEqual(self.store.environment(child['run_id'])['CADRE_NO_AUTO_COMMIT'], '1')
            product = Path(child['worktree']) / 'f1.txt'
            before = product.read_bytes()
            self.assertEqual(execute_run(self.store, self.ana, devam=True), 78)
            self.assertEqual(revive(self.store, LIB.parent), [])
            self.assertEqual(len(self.calisan), 1)
            action = self.store.read(self.ana)['project_git_action']
            first = proje.git_aktarim_onayi(self.store, self.ana, action['token'])
            proje.git_aktarim_onayi(self.store, self.ana, action['token'], first['confirmation'])
            self.store = RunStore(self.home)
            self.assertEqual(proje.baslat(self.ana, 'p', planla=self.planla(), calistir=self.calistir()), 78)
            self.assertEqual(self.calisan.count(child['run_id']), 1)
            self.assertEqual(product.read_bytes(), before)
            d = json.loads((self.store.path(self.ana) / 'proje.json').read_text())
            self.assertEqual((d['bitirilen'], d['ozellikler'][0]['durum'], d['son_kabul_turu']), (1, 'uygulandi', 0))
            self.assertEqual(d['neden'], 'git-transfer-permission-required')

    def test_operator_committed_source_is_adopted_only_when_it_matches_the_preserved_snapshot(self):
        import shutil
        import subprocess
        from kosu import FileLock
        (self.repo / 'README.md').write_text('accepted dirty source\n')
        (self.repo / 'extra.txt').write_text('accepted untracked source\n')
        git(self.repo, 'add', 'README.md')
        rid = self.store.accept('p', 'sifirdan kucuk bir not araci yaz')['run_id']
        source = self.store.read(rid)['source']
        original_index = (self.repo / '.git/index').read_bytes()
        self.assertEqual(proje.baslat(rid, 'p', planla=self.planla([oz('f1')]), calistir=self.calistir()), 78)
        pending = self.store.read(rid)['project_git_wait']
        with self.assertRaisesRegex(ValueError, 'parent-mismatch'):
            proje.git_commit_devral(self.store, rid, source['sha'])
        wt = self.home / 'operator-source'
        git(self.repo, 'worktree', 'add', '-q', '--detach', str(wt), source['sha'])
        git(wt, 'apply', source['patch'])
        shutil.copytree(source['untracked'], wt, dirs_exist_ok=True)
        git(wt, 'add', '.')
        git(wt, 'commit', '-qm', 'fixture separately authorized accepted source')
        sha = git(wt, 'rev-parse', 'HEAD')
        wrong = git(wt, 'commit-tree', git(wt, 'rev-parse', f"{source['sha']}^{{tree}}"), '-p', source['sha'], '-m', 'fixture wrong source')
        with self.assertRaisesRegex(ValueError, 'content-mismatch'):
            proje.git_commit_devral(self.store, rid, wrong)
        flag = self.store.path(rid) / 'cancel.flag'
        flag.touch()
        with self.assertRaisesRegex(ValueError, 'stale-project-commit'):
            proje.git_commit_devral(self.store, rid, sha)
        flag.unlink()
        with FileLock(self.store.path(rid) / '.owner.lock'), self.assertRaisesRegex(ValueError, 'owner-active'):
            proje.git_commit_devral(self.store, rid, sha)
        env = {k: v for k, v in os.environ.items() if not k.startswith(('CADRE_', 'OPENCODE_'))}
        env['CADRE_HOME'] = str(self.home)
        cmd = [sys.executable, str(LIB / 'kosu.py'), 'project-git-adopt', rid, sha]
        missing = subprocess.run(cmd[:-1] + ['0' * 40], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(missing.returncode, 78)
        denied = subprocess.run(cmd, env={**env, 'CADRE_RUN_ID': rid}, capture_output=True, text=True, timeout=10)
        self.assertEqual(denied.returncode, 78)
        adopted = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(adopted.returncode, 0, adopted.stderr)
        self.assertEqual(json.loads(adopted.stdout)['tree'], pending['tree'])
        before = {str(p.relative_to(self.repo)): p.read_bytes() for p in (self.repo / '.git').rglob('*') if p.is_file()}
        self.assertEqual(proje.git_commit_devral(self.store, rid, sha)['adopted_sha'], sha)
        self.assertEqual(before, {str(p.relative_to(self.repo)): p.read_bytes() for p in (self.repo / '.git').rglob('*') if p.is_file()})
        self.assertEqual(proje.baslat(rid, 'p', planla=self.planla([oz('f1')]), calistir=self.calistir()), 0)
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), source['sha'])
        self.assertEqual((self.repo / '.git/index').read_bytes(), original_index)
        self.assertEqual((self.repo / 'extra.txt').read_text(), 'accepted untracked source\n')
        self.assertEqual((self.store.path(rid) / 'baseline-source/extra.txt').read_text(), 'accepted untracked source\n')

    def test_operator_product_commit_reanchors_identical_qa_and_resumes_the_same_child_after_partial_record_failure(self):
        import qe_core as core
        from qa_fixture import command
        from sonuc import implemented
        normal = self.calistir()
        def worker(store, cid):
            k = store.read(cid)
            if not k.get('feature_acceptance'):
                return normal(store, cid)
            self.calisan.append(cid)
            wt = self.home / 'worktrees' / k['project'] / cid
            wt.parent.mkdir(parents=True, exist_ok=True)
            git(self.repo, 'worktree', 'add', '-q', '-b', f'cadre/{cid}', str(wt), k['source_sha'])
            (wt / 'f1.txt').write_text('reviewed dirty product\n')
            store.update(cid, worktree=str(wt))
            with patch.dict(os.environ, store.environment(cid)):
                qa = core.qa_kapisi(k['project'], str(wt), self.home / 'adopt-qa.log', '/forbidden/provider', model='claude/fixture')
            self.assertEqual(qa['status'], 'PASS', qa)
            result = {'status': 'UYGULANDI', 'revision': qa['revision'], 'qa': qa, 'riskli': False,
                      'review': {'status': 'PASS', 'revision': qa['revision'], 'independent': True}}
            store.update(cid, state='implemented', final_result=result)
            self.assertTrue(implemented(store.read(cid)))
            return 0
        with patch.object(proje, 'birlestir_ff', side_effect=_BIRLESTIR):
            self.assertEqual(proje.baslat(self.ana, 'p', planla=self.planla([{**oz('f1'), 'qa_komut': command('test -f f1.txt')}]), calistir=worker), 78)
            cid = self.calisan[0]
            child = self.store.read(cid)
            old_result = child['final_result']
            wt = Path(child['worktree'])
            git(wt, 'add', 'f1.txt')
            git(wt, 'commit', '-qm', 'fixture separately authorized reviewed product')
            sha = git(wt, 'rev-parse', 'HEAD')
            self.assertFalse(implemented(self.store.read(cid)))
            from kosu import FileLock
            with FileLock(self.store.path(cid) / '.owner.lock'), self.assertRaisesRegex(ValueError, 'owner-active'):
                proje.git_commit_devral(self.store, self.ana, sha)
            product = wt / 'f1.txt'
            content = product.read_bytes()
            product.write_text('changed after review\n')
            with self.assertRaisesRegex(ValueError, 'content-mismatch'):
                proje.git_commit_devral(self.store, self.ana, sha)
            product.write_bytes(content)
            self.store.update(cid, final_result={**old_result, 'status': 'FAIL'})
            with self.assertRaisesRegex(ValueError, 'evidence-mismatch'):
                proje.git_commit_devral(self.store, self.ana, sha)
            self.store.update(cid, final_result=old_result)
            for field in ('project_git_wait', 'project_source_commit', 'project_deadline'):
                with self.assertRaisesRegex(ValueError, 'immutable'):
                    self.store.update(self.ana, **{field: 'changed'})
            before_git = {str(p.relative_to(self.repo)): p.read_bytes() for p in (self.repo / '.git').rglob('*') if p.is_file()}
            import kosu
            write = kosu.atomic_json
            def failing(path, value):
                if path == self.store.path(self.ana) / 'run.json':
                    raise OSError('fixture parent record disk failure')
                return write(path, value)
            with patch('kosu.atomic_json', side_effect=failing), self.assertRaises(OSError):
                proje.git_commit_devral(self.store, self.ana, sha)
            self.assertTrue(implemented(self.store.read(cid)))
            self.assertEqual(self.store.read(self.ana)['reason'], 'proje-durdu: commit-permission-required')
            self.assertEqual(proje.git_commit_devral(self.store, self.ana, sha)['adopted_sha'], sha)
            after_git = {str(p.relative_to(self.repo)): p.read_bytes() for p in (self.repo / '.git').rglob('*') if p.is_file()}
            self.assertEqual(before_git, after_git, [p for p in before_git.keys() | after_git.keys() if before_git.get(p) != after_git.get(p)])
            self.assertEqual(self.store.read(cid)['final_result']['qa']['attempts'], old_result['qa']['attempts'])
            self.assertEqual(proje.baslat(self.ana, 'p', planla=self.planla(), calistir=worker), 78)
            action = self.store.read(self.ana)['project_git_action']
            first = proje.git_aktarim_onayi(self.store, self.ana, action['token'])
            proje.git_aktarim_onayi(self.store, self.ana, action['token'], first['confirmation'])
            self.assertEqual(proje.baslat(self.ana, 'p', planla=self.planla(), calistir=worker), 78)
            self.assertEqual(self.calisan.count(cid), 1)
            d = json.loads((self.store.path(self.ana) / 'proje.json').read_text())
            self.assertEqual((d['bitirilen'], d['ozellikler'][0]['deneme']), (1, 1))
            self.assertEqual(d['neden'], 'git-transfer-permission-required')

    def test_three_passes_preserve_reviewed_plan_with_a_local_decision_and_no_planner_repeat(self):
        from unittest.mock import Mock
        planner = Mock(side_effect=self.planla([oz(f'f{i}') for i in range(1, 5)]))
        self.assertEqual(proje.baslat(self.ana, 'p', planla=planner, calistir=self.calistir()), 0)
        d = json.loads((self.store.path(self.ana) / 'proje.json').read_text())
        self.assertEqual(planner.call_count, 1)
        self.assertEqual(d['plan_degerlendirmeleri'][0]['decision'], 'unchanged')
        self.assertEqual(d['plan_degerlendirmeleri'][0]['evidence']['completed'], 3)
        self.assertEqual(self.store.plan_view(self.ana)['revision'], 1)
        self.assertEqual(len(self.store.plan_view(self.ana)['reviews']), 1)

    def test_replan_cannot_add_or_drop_selected_acceptance_and_rejected_review_cannot_execute(self):
        self.assertEqual(proje.baslat(self.ana, 'p', planla=self.planla([oz('f1'), oz('f2')]), calistir=self.calistir()), 0)
        view = self.store.plan_view(self.ana)
        self.store.update(self.ana, state='running')
        features = json.loads((self.store.path(self.ana) / 'proje.json').read_text())['ozellikler']
        for proposal in ([*features, oz('new')], features[:1], [{**features[0], 'kabul': 'other goal'}, features[1]]):
            with self.assertRaisesRegex(ValueError, 'project-scope'):
                proje.proje_plani_kaydet(self.store, self.ana, proposal)
            self.assertEqual(self.store.plan_view(self.ana)['revision'], view['revision'])
        changed = [{**features[0], 'qa_komut': 'test -e updated'}, features[1]]
        with patch('kosu.review_plan', return_value={'status': 'FAIL'}):
            with self.assertRaisesRegex(ValueError, 'current-plan-review-required'):
                proje.proje_plani_kaydet(self.store, self.ana, changed)
        next_view = self.store.plan_view(self.ana)
        self.assertEqual(next_view['revision'], 2)
        self.assertTrue(next_view['held'])
        from kosu import execute_run
        before = list(self.calisan)
        self.assertEqual(execute_run(self.store, self.ana, devam=True), 78)
        self.assertEqual(self.calisan, before)
        proje.proje_plani_kaydet(self.store, self.ana, changed)
        accepted = self.store.plan_view(self.ana)
        self.assertEqual(accepted['revision'], 2)
        self.assertFalse(accepted['held'])
        self.assertEqual(accepted['reviews'][-1]['status'], 'PASS')
        self.assertEqual(accepted['reviews'][-1]['revision_number'], 2)

    def test_expired_project_cannot_launch_planning_children_or_final_acceptance_after_restart(self):
        from kosu import atomic_json, launch_guard
        from unittest.mock import Mock
        state = proje._yeni_durum(self.ana, 'request', 0)
        state['ozellikler'] = [oz('f1', durum='uygulandi')]
        atomic_json(self.store.path(self.ana) / 'proje.json', state)
        planner, child = Mock(), Mock()
        for _ in range(2):
            self.assertNotEqual(proje.yurut(self.store, self.ana, planla=planner, cocuk_calistir=child,
                                           birlestir=Mock()), 0)
            self.assertEqual(json.loads((self.store.path(self.ana) / 'proje.json').read_text())['neden'], 'sure-tavani')
        planner.assert_not_called()
        child.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'project-time-limit'):
            with launch_guard(self.store, self.ana):
                self.fail('expired parent launched')
        cid = self.store.accept('fixture-child', 'child', parent_run_id=self.ana)['run_id']
        with self.assertRaisesRegex(ValueError, 'project-time-limit'):
            with launch_guard(self.store, cid):
                self.fail('expired child launched')

    def test_final_acceptance_repair_keeps_original_criteria_and_requires_a_reviewed_revision(self):
        initial = oz('f1')
        initial['kabul'] = 'ayni secilen urun kabul kriteri korunur'
        repair = {**oz('repair', ['f1']), 'repair_of': 'f1', 'kabul': initial['kabul']}
        normal = self.calistir()
        finals = []
        def child(store, cid):
            if Path(store.read(cid)['prompt']).read_text().startswith('PROJE SON KABUL'):
                finals.append(cid)
                if len(finals) == 1:
                    store.update(cid, state='failed', final_result={'status': 'FAIL', 'reason': 'missing-output'})
                    return 1
            return normal(store, cid)
        self.assertEqual(proje.baslat(self.ana, 'p', planla=self.planla([initial], [initial, repair]), calistir=child), 0)
        view = self.store.plan_view(self.ana)
        self.assertEqual(view['revision'], 2)
        self.assertEqual(view['reviews'][-1]['status'], 'PASS')
        state = json.loads((self.store.path(self.ana) / 'proje.json').read_text())
        self.assertEqual([f['id'] for f in state['ozellikler']], ['f1', 'repair'])
        self.assertEqual({f['kabul'] for f in state['ozellikler']}, {initial['kabul']})
        self.assertEqual(state['son_kabul_turu'], 1)

    def test_son_kabul_cocugu_takilan_ozelligin_kabul_komutunu_qa_da_kosar(self):
        f1 = {**oz('f1', oncelik=1), 'qa_komut': 'python3 -m pytest -q tests/test_f1.py'}
        rc = proje.baslat(self.ana, 'p', planla=self.planla([f1, oz('f2')], [oz('f2')]), calistir=self.calistir(basarisiz=('f1',)))
        son = self.store.read(self.calisan[-1])
        self.assertTrue(Path(son['prompt']).read_text().startswith('PROJE SON KABUL'))
        self.assertEqual(son.get('ek_kabul_komutlari'), ['python3 -m pytest -q tests/test_f1.py'])
        self.assertEqual(rc, 0)

    def test_proje_ara_dalda_birikir_ve_son_kabul_kaniti_ana_kosuya_gecer(self):
        rc = proje.baslat(self.ana, 'p', planla=self.planla([oz('f1', oncelik=1), oz('f2', ['f1'])]), calistir=self.calistir())
        self.assertEqual(rc, 0)
        ana = self.store.read(self.ana)
        dal = ana['proje']['ara_dal']
        self.assertEqual(git(self.repo, 'log', '--format=%s', dal).splitlines()[:3], ['son-kabul', 'f2', 'f1'])
        self.assertEqual(git(self.repo, 'rev-parse', 'main'), git(self.repo, 'rev-list', '--max-parents=0', 'main'), 'kanonik dal yerinde')
        self.assertEqual(ana['final_result'], {'status': 'PASS', 'etiket': 'son-kabul'})
        self.assertEqual(Path(ana['worktree']).name, self.calisan[-1])
        cocuklar = [self.store.read(c) for c in self.calisan]
        self.assertTrue(all(c['proje_ust'] == self.ana and c['project'] == ana['proje']['cocuk_proje'] for c in cocuklar))
        self.assertIn('2 gecti', (self.store.path(self.ana) / 'proje-raporu.md').read_text())

    def test_yarim_kalan_cocuk_basarisiz_kapanir_ve_proje_kalici_nedenle_durur(self):
        rc = proje.baslat(self.ana, 'p', planla=self.planla([oz('f1')]), calistir=self.calistir(basarisiz=('f1',)))
        self.assertEqual(rc, 1)
        self.assertTrue(all(self.store.read(c)['state'] == 'failed' for c in self.calisan), 'retryable cocuk sonraki cocugu tikamasin')
        self.assertEqual(self.store.read(self.ana)['reason'], 'proje-durdu: ilerleme-yok')

    def test_hakem_kapasitesi_bekleyen_cocuk_basarisiz_sayilmaz_beklenip_surdurulur(self):
        """B241 (26.09 live B203b): the child passed the acceptance gate but the judge was waiting for budget ('retryable' + retry_after); the main run counted it
        as 'feature failed' and restarted the same feature from scratch ($1.16 wasted). A waiting child is waited for and the SAME run continues."""
        import time
        normal = self.calistir()
        goruldu = []
        def f(store, cid):
            if cid not in goruldu and not Path(store.read(cid)['prompt']).read_text().startswith('PROJE SON KABUL'):
                goruldu.append(cid); self.calisan.append(cid)
                store.update(cid, state='retryable', reason='hakem-kapasitesi: paylar yenilenince yeniden denenecek', retry_after=time.time() + 1,
                             final_result={'status': 'FAIL', 'qa': {'status': 'PASS'}, 'review': {'status': 'ERROR', 'reason': 'hakem zinciri tukendi: claude/opus#max: BudgetDenied'}})
                return 1
            return normal(store, cid)
        rc = proje.baslat(self.ana, 'p', planla=self.planla([oz('f1')]), calistir=f)
        self.assertEqual(rc, 0)
        self.assertEqual(self.calisan[0], self.calisan[1], 'bekleyen cocuk ayni kosu olarak surduruldu, yeni cocuk acilmadi')
        d = json.loads((self.store.path(self.ana) / 'proje.json').read_text())
        self.assertEqual([x['durum'] for x in d['ozellikler']], ['gecti'])
        self.assertEqual(d['ardisik_basarisiz'], 0)

    def test_harcama_alarmi_bekleyen_cocuk_basarisiz_sayilmaz_ana_kosu_onay_bekler(self):
        import time
        def f(store, cid):
            self.calisan.append(cid)
            store.update(cid, state='retryable', reason='butce-bekleme', retry_after=time.time() + 60,
                         harcama_alarmi={'ts': time.time(), 'neden': 'son 10 dk'})
            return 75
        rc = proje.baslat(self.ana, 'p', planla=self.planla([oz('f1')]), calistir=f)
        self.assertNotEqual(rc, 0)
        self.assertEqual(len(self.calisan), 1, 'alarm beklerken cocuk yeniden calistirilmaz')
        d = json.loads((self.store.path(self.ana) / 'proje.json').read_text())
        self.assertEqual((d['neden'], d['ardisik_basarisiz']), ('harcama-alarmi', 0))
        self.assertTrue(self.store.read(self.ana).get('harcama_alarmi'))
        self.assertEqual(self.store.read(self.calisan[0])['state'], 'retryable')

    def test_son_kabulde_harcama_alarmi_tur_tuketmez_onay_bekler(self):
        import time
        normal = self.calistir()
        son_kabul = []
        def f(store, cid):
            if Path(store.read(cid)['prompt']).read_text().startswith('PROJE SON KABUL'):
                son_kabul.append(cid)
                store.update(cid, state='retryable', reason='butce-bekleme', retry_after=time.time() + 60,
                             harcama_alarmi={'ts': time.time(), 'neden': 'son 10 dk'})
                return 75
            return normal(store, cid)
        rc = proje.baslat(self.ana, 'p', planla=self.planla([oz('f1')]), calistir=f)
        self.assertNotEqual(rc, 0)
        d = json.loads((self.store.path(self.ana) / 'proje.json').read_text())
        self.assertEqual((d['neden'], d['son_kabul_turu']), ('harcama-alarmi', 0))
        self.assertEqual(len(son_kabul), 1, 'alarm beklerken son kabul yeniden kosmaz')

    def test_retry_aftersiz_retryable_cocuk_bir_kez_daha_denenir(self):
        """B273 (30.09 rehearsal, child 060218/064142): the coordinator left `retryable` with rc!=0 (no retry_after); project mode did not take it for
        a capacity wait and counted the feature as failed after a SINGLE attempt. Normal runs are retried overnight via P4; a child is retried once too."""
        normal = self.calistir()
        goruldu = []
        def f(store, cid):
            if cid not in goruldu and not Path(store.read(cid)['prompt']).read_text().startswith('PROJE SON KABUL'):
                goruldu.append(cid); self.calisan.append(cid)
                store.update(cid, state='retryable', reason='rc!=0',
                             final_result={'status': 'FAIL', 'qa': {'status': 'PASS'}, 'review': {'status': 'FAIL', 'reason': 'review-repair-limit'}})
                return 1
            return normal(store, cid)
        rc = proje.baslat(self.ana, 'p', planla=self.planla([oz('f1')]), calistir=f)
        self.assertEqual(rc, 0)
        self.assertEqual(self.calisan[0], self.calisan[1], 'ayni cocuk kosu bir kez daha denendi, yeni cocuk acilmadi')
        d = json.loads((self.store.path(self.ana) / 'proje.json').read_text())
        self.assertEqual([x['durum'] for x in d['ozellikler']], ['gecti'])

    def test_supervised_native_project_does_not_retry_a_failed_child_or_launch_another_after_restart(self):
        from kosu import atomic_json
        self.store.update(self.ana, state='cancelled')
        atomic_json(self.home / 'projects/p/supervision.json', {'version': 1, 'mode': 'step'})
        self.ana = self.store.accept('p', 'Complete the feature list under supervision.')['run_id']
        worker = self.calistir(basarisiz=('f1',))
        planner = self.planla([oz('f1', oncelik=1), oz('f2', ['f1'])])
        self.assertEqual(proje.baslat(self.ana, 'p', planla=planner, calistir=worker), 78)
        self.assertEqual(len(self.calisan), 1)
        state = json.loads((self.store.path(self.ana) / 'proje.json').read_text())
        self.assertEqual((state['checkpoint']['feature'], state['checkpoint']['success']), ('f1', False))
        self.assertEqual(proje.baslat(self.ana, 'p', planla=planner, calistir=worker), 78)
        self.assertEqual(len(self.calisan), 1)

    def test_kesilen_cocuk_ayni_istemle_yeni_kosu_acilmadan_surer(self):
        from kosu import atomic_json
        plan = self.planla([oz('f1')])
        proje.baslat(self.ana, 'p', planla=plan, calistir=lambda store, cid: 0)   # the child coordinator failed: the record stayed 'accepted'
        ilk = json.loads((self.store.path(self.ana) / 'proje-aktif.json').read_text())['cocuk']
        self.store.update(ilk, state='running')
        d = json.loads((self.store.path(self.ana) / 'proje.json').read_text())
        d.update(durum='calisiyor', neden='', ardisik_basarisiz=0,
                 ozellikler=[{**f, 'durum': 'bekliyor', 'deneme': 0, 'kosu': []} for f in d['ozellikler']], cocuklar=[])
        atomic_json(self.store.path(self.ana) / 'proje.json', d)
        proje.baslat(self.ana, 'p', planla=plan, calistir=self.calistir())
        self.assertEqual(self.calisan[0], ilk, 'yarim cocuk surduruldu, yeni kosu acilmadi')



class EbeveynKayitCokmesiTest(unittest.TestCase):
    def test_biten_cocuk_ebeveyn_kaydetmeden_kesilirse_ayni_is_ikinci_kez_kosmaz(self):
        """D11 (01.10 Codex, G-P4): when the process was cut after the child became terminal but before the parent wrote proje.json, a restart opened
        a NEW child with the same prompt (two executions, two costs). A terminal child result the parent has not consumed is returned as is."""
        import tempfile
        from kosu import RunStore, atomic_json
        tmp = tempfile.TemporaryDirectory(prefix='proje-cokme-')
        self.addCleanup(tmp.cleanup)
        store = RunStore(Path(tmp.name))
        ana = store.accept('p', 'ana')['run_id']
        cagri = []
        def calistir(st, rid):
            cagri.append(rid)
            st.update(rid, state='completed', budget_spent_usd=.1)
            return 0
        ilk = proje.cocuk_calistir_gercek(store, ana, 'p--x', 'ayni ozellik', 'main', calistir=calistir)
        ikinci = proje.cocuk_calistir_gercek(store, ana, 'p--x', 'ayni ozellik', 'main', calistir=calistir)
        self.assertEqual((ikinci['run_id'], cagri), (ilk['run_id'], [ilk['run_id']]))
        atomic_json(store.path(ana) / 'proje.json', {'cocuklar': [{'ozellik': 'f1', 'kosu': ilk['run_id']}]})   # the parent consumed it
        ucuncu = proje.cocuk_calistir_gercek(store, ana, 'p--x', 'ayni ozellik', 'main', calistir=calistir)
        self.assertNotEqual(ucuncu['run_id'], ilk['run_id'], 'kaydedilmis sonucun yeniden denemesi yeni cocuktur')
        self.assertEqual(len(cagri), 2)


class ProjeSureTavaniBeklemeTest(unittest.TestCase):
    def test_butce_beklemesi_proje_suresini_asamaz(self):
        import time
        from kosu import RunStore, atomic_json
        tmp = tempfile.TemporaryDirectory(prefix='proje-sure-')
        self.addCleanup(tmp.cleanup)
        store = RunStore(Path(tmp.name))
        ana = store.accept('p', 'ana')['run_id']
        atomic_json(store.path(ana) / 'proje.json', {'baslangic': time.time() - proje.SURE_TAVANI_SN - 86400, 'cocuklar': []})
        cagri = []
        def calistir(st, rid):
            cagri.append(rid)
            if len(cagri) == 1:
                st.update(rid, state='retryable', reason='butce-bekleme: pay yok', retry_after=time.time() + 1)
            else:
                st.update(rid, state='completed')
            return 0
        with patch.dict(os.environ, {'CADRE_BEKLEME_ESIK_SN': '30'}):
            son = proje.cocuk_calistir_gercek(store, ana, 'p--x', 'ozellik', 'main', calistir=calistir)
        self.assertEqual(len(cagri), 1, 'sure dolduktan sonra cocuk yeniden calistirilmaz')
        self.assertEqual(son['state'], 'failed')
        self.assertTrue(son['reason'].startswith('proje-sure-tavani'), son['reason'])


class ProjeMaliyetiTest(unittest.TestCase):
    def test_proje_harcamasi_ana_ve_cocuk_defterinden_olculen_tahmini_askida_ayri(self):
        """D10 (01.10 Codex): the report wrote only the children's total at their end ($20.36); the main planning ($2.75) and reservations closed
        later were missing (ledger $25.52, of which $2.58 was a ghost estimate)."""
        import datetime, tempfile
        import butce_kapisi as bk
        tmp = tempfile.TemporaryDirectory(prefix='proje-maliyet-')
        self.addCleanup(tmp.cleanup)
        self.enterContext(patch.dict(os.environ, {'CADRE_HARCAMA_ALARMI': '0', 'CADRE_KOSU_TAVAN_USD': '50'}))   # a $25-per-second ledger setup; the alarm is a separate contract
        home = Path(tmp.name)
        simdi = datetime.datetime.now()
        b = {'ts': simdi.isoformat(), 'bugun': simdi.date().isoformat(),
             'zen': {'db': 'ok', 'pay': 50.0, 'kullanilabilir': 50.0}}
        defter = bk.BudgetLedger(home / 'zincir/request-budget.db')
        def kayit(rid, usd, makbuz=None):
            ident = defter.reserve('opencode', int(usd * 1e6), b, {'run_id': rid})
            if makbuz:
                defter.settle(ident, int(usd * 1e6), makbuz + ident)
        from kosu import RunStore, atomic_json
        store = RunStore(home)
        ana = store.accept('p', 'project')['run_id']
        c1 = store.accept('p-child', 'active feature')['run_id']
        c2 = store.accept('p-child', 'cancelled feature')['run_id']
        store.update(ana, proje={'kimlik': 'x'})
        store.update(c1, proje_ust=ana, state='running')
        store.update(c2, proje_ust=ana, state='cancelled')
        atomic_json(store.path(ana) / 'proje.json', {'kimlik': 'x', 'ozellikler': [], 'harcama_usd': 0})
        kayit(ana, 2.0, 'olcum:')
        kayit(c1, 1.0, 'olcum:')
        kayit(c1, 0.5, 'hayalet:')
        kayit(c2, 0.25)
        kayit('baska', 9.0, 'olcum:')
        m = proje.proje_maliyeti(home, [ana])
        self.assertEqual(m, {'olculen_usd': 3.0, 'tahmini_usd': 0.5, 'askida_usd': 0.25, 'yukumluluk_usd': 3.75})
        self.assertIn('kesin $3.00', proje.durum_ozeti(store, 'p'))
        self.assertIn('askida ust sinir $0.25', proje.durum_ozeti(store, 'p'))
        metin = proje.rapor({'kimlik': 'x', 'ozellikler': [], 'harcama_usd': 1.0, 'maliyet': m})
        self.assertIn('kesin $3.00 · tahmini $0.50 · askida ust sinir $0.25 · yukumluluk $3.75', metin)

    def test_hesap_ozeti_askida_guvenilir_ust_sinir_tahmini_kesinden_ayri_ve_kalan_yukumlulukten(self):
        """N-BU03/N-OBS02 (RCA code28/29): pending showed only the reserve (0.05) on the panel while the trusted upper bound was 0.22;
        the ghost estimate was reported as 'definite spending'. The remainder is subtracted from the total obligation of the three classes."""
        import datetime, tempfile
        import butce_kapisi as bk
        tmp = tempfile.TemporaryDirectory(prefix='hesap-ozeti-')
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        simdi = datetime.datetime.now()
        b = {'ts': simdi.isoformat(), 'bugun': simdi.date().isoformat(), 'zen': {'db': 'ok', 'pay': 50.0, 'kullanilabilir': 50.0}}
        defter = bk.BudgetLedger(home / 'zincir/request-budget.db')
        from kosu import RunStore
        rid = RunStore(home).accept('p', 'kod')['run_id']
        olcum = defter.reserve('opencode', 60_000, b, {'run_id': rid})
        defter.settle(olcum, 10_000, 'olcum:' + olcum)
        hayalet = defter.reserve('opencode', 40_000, b, {'run_id': rid})
        defter.settle(hayalet, 40_000, 'hayalet:' + hayalet)
        kurtarma = defter.reserve('opencode', 30_000, b, {'run_id': rid})
        defter.settle(kurtarma, 30_000, 'kurtarma:' + kurtarma)
        defter.reserve('opencode', 50_000, b, {'run_id': rid, 'en_kotu': 220_000, 'verified_bound_micro': 220_000})
        m = proje.proje_maliyeti(home, [rid])
        self.assertEqual(m, {'olculen_usd': 0.01, 'tahmini_usd': 0.07, 'askida_usd': 0.22, 'yukumluluk_usd': 0.3})
        self.assertEqual(proje.hesap_ozeti(m, 1.0)['kalan_usd'], 0.7)
        satir = proje._harcama_satiri({'maliyet': m})
        self.assertIn('kesin $0.01', satir)
        self.assertIn('tahmini $0.07', satir)
        self.assertIn('askida ust sinir $0.22', satir)
        self.assertNotIn('harcama $0.08', satir)


class DurumOzetiTest(unittest.TestCase):
    """The `cadre durum` line: progress of the project's last project run (empty if none)."""
    def test_son_proje_kosusunun_ilerlemesi_tek_satirda(self):
        import tempfile
        from kosu import RunStore, atomic_json
        tmp = tempfile.TemporaryDirectory(prefix='proje-ozet-')
        self.addCleanup(tmp.cleanup)
        store = RunStore(Path(tmp.name))
        self.assertEqual(proje.durum_ozeti(store, 'p'), '')
        rid = store.accept('p', 'sifirdan arac')['run_id']
        store.update(rid, proje={'kimlik': 'ab12', 'ara_dal': 'cadre/proje-ab12', 'cocuk_proje': 'p--pab12'})
        atomic_json(store.path(rid) / 'proje.json', {'durum': 'calisiyor', 'neden': '', 'ara_dal': 'cadre/proje-ab12', 'harcama_usd': 0.75,
                                                    'ozellikler': [oz('f1', durum='gecti'), oz('f2'), oz('f3')]})
        satir = proje.durum_ozeti(store, 'p')
        for parca in ('cadre/proje-ab12', '1/3 gecti', 'calisiyor', '$0.75', 'proje-raporu.md'):
            self.assertIn(parca, satir)


if __name__ == '__main__':
    unittest.main()
