from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import threading
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yonlendirici as Y  # noqa: E402


class ZincirOrtamiTests(unittest.TestCase):
    def test_environment_overrides_a_role_chain_and_the_report_writer_follows_plan(self):
        with patch.dict(os.environ, {'CADRE_ZINCIR_PLAN': 'openrouter/deepseek/deepseek-v4-pro', 'CADRE_ZINCIR_KALITE': 'a/b, c/d ,'}):
            self.assertEqual((Y.rol_adaylari('plan'), Y.rol_adaylari('yazar'), Y.rol_adaylari('kalite')),
                             (['openrouter/deepseek/deepseek-v4-pro'], ['openrouter/deepseek/deepseek-v4-pro'], ['a/b', 'c/d']))
            self.assertEqual(Y.rol_adaylari('impl'), Y.ROLLER['impl'])
        with patch.dict(os.environ, {'CADRE_ZINCIR_PLAN': ''}):
            self.assertEqual(Y.rol_adaylari('plan'), Y.ROLLER['plan'])

    def test_cheap_or_test_chain_keeps_the_judge_independent(self):
        with tempfile.TemporaryDirectory() as t, patch.dict(os.environ, {'CADRE_HOME': t}):
            import canli_senaryo as cs
            import qe_core as core
            ucuz, uretim = cs.zincir_ortami('or', 'ucuz'), cs.zincir_ortami('or', 'uretim')
            self.assertEqual(cs.zincir_ortami('go', 'ucuz'), {})
            for ortam in (ucuz, uretim):
                plan = ortam['CADRE_ZINCIR_PLAN'].split(',')
                yazan = ortam['CADRE_ZINCIR_IMPL'].split(',')[0]
                hakem = [m for m in ortam['CADRE_ZINCIR_KALITE'].split(',') if core.model_ailesi(m) not in
                         {core.model_ailesi(yazan), core.model_ailesi(plan[0])}]
                self.assertTrue(hakem, ortam)
                self.assertTrue(all(m.startswith('openrouter/') for v in ortam.values() for m in v.split(',')), ortam)
            self.assertEqual((ucuz['CADRE_ZINCIR_PLAN'], uretim['CADRE_ZINCIR_PLAN']), (cs.OR_DS, cs.OR_SOL))

    def test_or_wrapper_only_lets_openrouter_through(self):
        with tempfile.TemporaryDirectory() as t, patch.dict(os.environ, {'CADRE_HOME': t}):
            import canli_senaryo as cs
            sahte = Path(t) / 'sahte-opencode'
            sahte.write_text(f'#!{sys.executable}\nimport json, os, sys\nprint(json.dumps(json.loads(os.environ["OPENCODE_CONFIG_CONTENT"])))\n')
            sahte.chmod(0o700)
            sarmal = Path(t) / 'or-yalniz-opencode.py'
            sarmal.write_text(cs.sarmal_metni('or', str(sahte)))
            sarmal.chmod(0o700)
            config = json.dumps({'plugin': ['file:///x/cadre-plugin.mjs'], 'provider': {'openrouter': {}, 'opencode-go': {}},
                                 'agent': {'build': {'model': 'opencode-go/glm-5.3'}}})
            env = {**os.environ, 'OPENCODE_CONFIG_CONTENT': config}
            red = subprocess.run([str(sarmal), 'run', '-m', 'opencode-go/glm-5.3', 'x'], env=env, capture_output=True, text=True, timeout=30)
            gecer = subprocess.run([str(sarmal), 'run', '-m', 'openrouter/deepseek/deepseek-v4-pro', 'x'], env=env, capture_output=True, text=True,
                                   timeout=30)
        self.assertEqual(red.returncode, 75)
        self.assertIn('izinsiz yol', red.stderr)
        self.assertEqual(gecer.returncode, 0, gecer.stderr)
        sonuc = json.loads(gecer.stdout)
        self.assertEqual((sonuc['enabled_providers'], list(sonuc['provider']), sonuc['agent']['build'].get('disable')),
                         (['openrouter'], ['openrouter'], True))
        self.assertIn('opencode-go', sonuc['disabled_providers'])


class CanliSinirTests(unittest.TestCase):
    def test_invalid_limits_fail_before_creating_runtime_files(self):
        import canli_senaryo as cs
        with tempfile.TemporaryDirectory() as t, patch.object(cs, 'ARSIV', Path(t) / 'archive'):
            for minutes, cap in ((0, 1), (-1, 1), (float('nan'), 1), (float('inf'), 1),
                                 (1, -1), (1, float('nan')), (1, float('inf'))):
                with self.subTest(minutes=minutes, cap=cap), self.assertRaises(ValueError):
                    cs.Senaryo('C06', minutes, cap)
            self.assertFalse((Path(t) / 'archive').exists())

    def test_repeated_waits_and_resume_keep_the_original_deadline(self):
        import canli_senaryo as cs
        with tempfile.TemporaryDirectory() as t, patch.object(cs, 'ARSIV', Path(t)), \
             patch.object(cs.time, 'monotonic', return_value=100) as clock:
            scenario = cs.Senaryo('C06', 1, 1)
            proc = Mock()
            proc.poll.return_value = None
            self.assertFalse(scenario.bekle(proc, lambda: True))
            clock.return_value = 161
            with patch.object(scenario, 'durdur') as stop, patch.object(cs.subprocess, 'Popen') as start, \
                 patch.object(cs.subprocess, 'run') as accept, \
                 patch.object(cs.time, 'sleep', side_effect=AssertionError('deadline already exhausted')):
                self.assertTrue(scenario.bekle(proc))
                stop.assert_called_once_with(proc)
                with self.assertRaises(TimeoutError):
                    scenario.baslat('a', '--devam')
                with self.assertRaises(TimeoutError):
                    scenario.kabul('No paid request after the deadline')
                start.assert_not_called()
                accept.assert_not_called()

    def test_elapsed_deadline_stops_actual_child_and_rejects_another_start(self):
        import canli_senaryo as cs
        with tempfile.TemporaryDirectory() as t, patch.object(cs, 'HOME', Path(t)), \
             patch.object(cs, 'ROOT', Path(t)), patch.object(cs, 'ARSIV', Path(t) / 'archive'):
            (Path(t) / 'lib').mkdir()
            (Path(t) / 'lib/kosu.py').write_text('import time\ntime.sleep(30)\n')
            scenario = cs.Senaryo('C06', 0.003, 1)
            started = time.monotonic()
            proc = scenario.baslat('a', '--devam')
            try:
                self.assertTrue(scenario.bekle(proc, aralik=0.02))
                self.assertIsNotNone(proc.returncode)
                self.assertLess(time.monotonic() - started, 3)
                with self.assertRaises(TimeoutError):
                    scenario.baslat('a', '--devam')
            finally:
                if proc.poll() is None:
                    scenario.durdur(proc)

    def test_alarm_probe_rejects_false_success_and_preserves_unapproved_wait(self):
        import canli_senaryo as cs
        for variant in ('clean', 'changed-test', 'wrong-product', 'same-review', 'unexpected-provider',
                        'alarm-call', 'unapproved-pass', 'retry-growth', 'over-cap', 'unknown-cost'):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as t, \
                 patch.object(cs, 'ARSIV', Path(t)), patch.object(cs.subprocess, 'run') as commands:
                scenario = cs.Senaryo('C06', 10, 1)
                scenario.kabul = Mock(return_value='a')
                scenario.baslat = Mock()
                scenario.bekle = Mock(return_value=True)
                scenario.yeniden_bekle = Mock(return_value=True)
                scenario.agac = Mock(return_value=['a'])
                waiting = {'state': 'retryable', 'retry_count': 0, 'revive_count': 0}
                unapproved = dict(waiting)
                if variant == 'unapproved-pass':
                    unapproved['final_result'] = {'status': 'PASS'}
                if variant == 'retry-growth':
                    unapproved['retry_count'] = 1
                scenario.kayit = Mock(side_effect=[waiting, unapproved, {'state': 'completed'}])
                scenario.makbuzlar = Mock(return_value={'saglayicilar': {}, 'cagrilar': [{
                    'tur': 'ucretli', 'baslangic': time.time(), 'bitis': None}] if variant == 'alarm-call' else []})
                proof = {'final': 'PASS', 'askida': int(variant == 'unknown-cost'), 'urun': {
                    'rc': 0, 'korunan_ayni': variant != 'changed-test',
                    'degisen': ['other.py'] if variant == 'wrong-product' else ['calc.py']},
                    'hakem_bagimsiz': variant != 'same-review',
                    'izinsiz_yol': ['other'] if variant == 'unexpected-provider' else [],
                    'makbuz': {'opencode-go': {'gercek_micro': 1_000_001 if variant == 'over-cap' else 1,
                                             'askida_micro': 0}}}
                with patch.object(cs, 'kanit', return_value=proof):
                    result = cs.alarm_devami(scenario, 'qa', {})
                self.assertEqual(result['gecti'], variant == 'clean', result)
                self.assertEqual(scenario.baslat.call_args_list[1].args, ('a', '--devam'))
                self.assertEqual(scenario.baslat.call_count, 3)
                self.assertTrue(any('harcama-onay' in call.args[0] for call in commands.call_args_list))

    def test_c06_total_cap_is_shared_and_alarm_only_uses_one_run(self):
        import canli_senaryo as cs
        for cap, alarm_only in ((1.0, False), (0.001, False), (0, False), (1.0, True)):
            with self.subTest(cap=cap, alarm_only=alarm_only), tempfile.TemporaryDirectory() as t, \
                 patch.object(cs, 'ARSIV', Path(t)), patch.object(cs, 'kanit', return_value={
                     'final': 'PASS', 'askida': 0, 'urun': {'rc': 0, 'korunan_ayni': True, 'degisen': ['calc.py']},
                     'hakem_bagimsiz': True, 'izinsiz_yol': [], 'makbuz': {}}), patch.object(cs.subprocess, 'run'):
                scenario = cs.Senaryo('C06', 10, cap)
                scenario.repo = Path(t)
                (scenario.repo / 'test_calc.py').write_text(cs.TEST_CALC)
                scenario.repo_kur = Mock(return_value='base')
                limits = []
                def accept(prompt):
                    limits.append(float(scenario.ortam()['CADRE_KOSU_TAVAN_USD']))
                    return str(len(limits))
                scenario.kabul = accept
                scenario.baslat = Mock()
                scenario.bekle = Mock(return_value=True)
                scenario.yeniden_bekle = Mock(return_value=True)
                scenario.agac = Mock(return_value=['a'])
                scenario.kayit = Mock(return_value={'state': 'completed', 'final_result': {'status': 'PASS'}})
                scenario.makbuzlar = Mock(return_value={'saglayicilar': {}, 'satirlar': [], 'cagrilar': []})
                cs.c06(scenario, yalniz_alarm=alarm_only)
                self.assertEqual(len(limits), 1 if alarm_only else 3)
                self.assertLessEqual(sum(round(v * 1e6) for v in limits), int(cap * 1e6))
                self.assertTrue(all(v >= 0 for v in limits))
                if alarm_only:
                    self.assertEqual(limits, [cap])


class CanliOlcumTests(unittest.TestCase):
    def setUp(self):
        import canli_senaryo as cs
        from butce_kapisi import BudgetLedger
        from datetime import datetime
        self.temp = tempfile.TemporaryDirectory(prefix='cadre-call-measurement-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        for name, value in (('HOME', self.home), ('ROOT', self.home), ('ARSIV', self.home / 'archive')):
            p = patch.object(cs, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.scenario = cs.Senaryo('C06', 1, 1, yol='or')
        self.ledger = BudgetLedger(self.home / 'zincir/request-budget.db')
        with self.ledger.transaction():
            pass
        now = datetime.now()
        self.snapshot = {'ts': now.isoformat(), 'bugun': now.date().isoformat(),
                         'or': {'db': 'ok', 'pay': 5, 'kullanilabilir': 5},
                         'go': {'db': 'ok', 'pay': 6.666667, 'kullanilabilir': 6.666667, 'pencere_acik': True, 'birim': 'puan'}}

    def reservation(self, provider='openrouter', actual=None, receipt='', run='a', **metadata):
        ident = self.ledger.reserve(provider, 1 if ':free' in provider else 100, self.snapshot, metadata)
        with self.ledger.transaction() as con:
            con.execute("update reservation set metadata=json_set(metadata,'$.run_id',?,'$.session_id',?) where id=?",
                        (run, 'session-' + run, ident))
        if actual is not None:
            self.ledger.settle(ident, actual, receipt + ident if receipt else ident + ':response')
        return ident

    def test_real_ledger_separates_calls_guarantees_unused_and_unknown_without_mixing_units(self):
        paid = self.reservation(actual=20)
        self.reservation(provider='opencode:free', actual=1)
        self.reservation(guvence=True)
        self.reservation(actual=0, receipt='claimed:', guvence=True)
        self.reservation(actual=0, receipt='unused:', guvence=True)
        self.reservation(en_kotu=300)
        self.reservation(actual=70, receipt='hayalet:')
        self.reservation(actual=50, run='unrelated')
        before = self.ledger.path.read_bytes()
        result = self.scenario.makbuzlar(['a'])
        self.assertEqual(self.ledger.path.read_bytes(), before)
        paid_rows = result['saglayicilar']['openrouter']
        self.assertEqual({k: paid_rows[k] for k in ('ucretli_istek', 'guvence', 'guvence_tuketilen', 'kullanilmayan', 'belirsiz')},
                         {'ucretli_istek': 1, 'guvence': 1, 'guvence_tuketilen': 1, 'kullanilmayan': 1, 'belirsiz': 2})
        self.assertEqual(paid_rows['gercek_micro'], 20)
        self.assertEqual(paid_rows['askida_micro'], 500)
        free = result['saglayicilar']['opencode:free']
        self.assertEqual((free['ucretsiz_istek'], free['ucretsiz_adet'], free['gercek_micro'], free['askida_micro']), (1, 1, 0, 0))
        self.assertEqual({r['tur'] for r in result['cagrilar']}, {'ucretli', 'ucretsiz', 'belirsiz'})
        self.assertEqual(len(result['cagrilar']), 4)
        self.assertEqual(next(r for r in result['cagrilar'] if r['id'] == paid)['run_id'], 'a')
        # CXR2-03: a bucket name with a '#model' suffix (butce_kapisi.MODEL_SONEK) counts as paid with its base provider; a direct row is written
        import json as _json
        import time as _time
        with self.ledger.transaction() as con:
            for ident, provider, actual in (('kova-go', 'opencode-go#glm-5.3', 500), ('kova-or', 'openrouter#z-ai/glm-5.3', 90)):
                con.execute('insert into reservation(id, provider, period, reserved, actual, created, receipt, metadata) values(?,?,?,?,?,?,?,?)',
                            (ident, provider, _time.strftime('%Y-%m-%d'), 1000, actual, _time.time(), ident + ':ok',
                             _json.dumps({'run_id': 'kova', 'session_id': 'ses-kova'})))
        kova = self.scenario.makbuzlar(['kova'])['saglayicilar']
        self.assertEqual({k: (v['birim'], v['ucretli_istek'], v['belirsiz'], v['gercek_micro']) for k, v in kova.items()},
                         {'opencode-go#glm-5.3': ('abonelik_esdeger_micro', 1, 0, 500), 'openrouter#z-ai/glm-5.3': ('nakit_micro', 1, 0, 90)})
        broken = self.reservation(actual=20, run='bad')
        with self.ledger.transaction() as con:
            con.execute('update reservation set actual=-1 where id=?', (broken,))
        with self.assertRaisesRegex(ValueError, 'makbuz'):
            self.scenario.makbuzlar(['bad'])
        import sqlite3
        saved = self.ledger.path.with_suffix('.saved')
        self.ledger.path.rename(saved)
        with self.assertRaises(sqlite3.OperationalError):
            self.scenario.makbuzlar(['a'])
        self.assertFalse(self.ledger.path.exists())

    def test_interval_boundaries_keep_overlap_unknown_and_free_calls_out_of_paid_alarm(self):
        rows = []
        for start, end, kind in ((1, 5, 'ucretli'), (10, 11, 'ucretli'), (20, 21, 'ucretli'),
                                 (5, 15, 'ucretli'), (5, None, 'belirsiz'), (12, 13, 'ucretsiz')):
            rows.append({'baslangic': start, 'bitis': end, 'tur': kind})
        result = self.scenario.pencere_cagrilari({'cagrilar': rows}, 10, 20)
        self.assertEqual(len(result), 3)
        self.assertEqual([r['zaman'] for r in result], ['aralikta', 'belirsiz', 'belirsiz'])
        self.assertEqual(len(self.scenario.pencere_cagrilari({'cagrilar': rows}, 10, 20, yalniz_ucretli=False)), 4)

    def test_alarm_driver_uses_real_receipts_and_never_accepts_a_nonterminal_pass(self):
        import canli_senaryo as cs
        for variant in ('clean', 'free', 'guarantee', 'unused', 'paid', 'unknown', 'nonterminal'):
            with self.subTest(variant=variant):
                run_id = variant
                scenario = self.scenario
                scenario.kabul = Mock(return_value=run_id)
                scenario.agac = Mock(return_value=[run_id])
                scenario.bekle = Mock(return_value=True)
                scenario.yeniden_bekle = Mock(return_value=True)
                waiting = {'state': 'retryable'}
                end = {'state': 'running' if variant == 'nonterminal' else 'completed'}
                scenario.kayit = Mock(side_effect=[waiting, waiting, end])
                def launch(*args):
                    if len(launcher.call_args_list) == 1:
                        if variant == 'free':
                            self.reservation(provider='opencode:free', actual=1, run=run_id)
                        elif variant == 'guarantee':
                            for _ in range(4):
                                self.reservation(guvence=True, run=run_id)
                        elif variant == 'unused':
                            self.reservation(guvence=True, actual=0, receipt='unused:', run=run_id)
                        elif variant == 'paid':
                            self.reservation(actual=20, run=run_id)
                        elif variant == 'unknown':
                            self.reservation(run=run_id)
                launcher = Mock(side_effect=launch)
                scenario.baslat = launcher
                def proof(*args):
                    m = scenario.makbuzlar([run_id])['saglayicilar']
                    return {'final': 'PASS', 'askida': sum(v['askida'] for v in m.values()),
                            'makbuz': m, 'hakem_bagimsiz': True, 'izinsiz_yol': [],
                            'urun': {'rc': 0, 'korunan_ayni': True, 'degisen': ['calc.py']}}
                with patch.object(cs.subprocess, 'run'), patch.object(cs, 'kanit', side_effect=proof):
                    result = cs.alarm_devami(scenario, 'qa', {})
                self.assertEqual(result['gecti'], variant in ('clean', 'free', 'guarantee', 'unused'), result)
                self.assertEqual(result['alarmda_istek'], int(variant in ('paid', 'unknown')))

    def test_interrupted_window_restores_only_its_own_marker(self):
        import canli_senaryo as cs
        marker = self.home / 'zincir/go-429-son.txt'
        marker.parent.mkdir(exist_ok=True)
        for foreign in (False, True):
            with self.subTest(foreign=foreign):
                marker.write_text('previous')
                self.scenario.yol = 'go'
                self.scenario.kabul = Mock(return_value='a')
                self.scenario.baslat = Mock()
                def interrupted(*args, **kwargs):
                    if marker.read_text() == 'previous':
                        return False
                    if foreign:
                        marker.write_text('another-owner')
                    raise RuntimeError('interrupted window')
                self.scenario.bekle = Mock(side_effect=interrupted)
                with patch.object(cs, 'iki_dosya_repo', return_value=('qa', {})), self.assertRaisesRegex(RuntimeError, 'interrupted'):
                    cs.c05(self.scenario)
                self.assertEqual(marker.read_text(), 'another-owner' if foreign else 'previous')

    def test_closed_window_driver_rejects_paid_free_go_unknown_and_nonterminal_results(self):
        import canli_senaryo as cs
        self.snapshot['go'] = {'db': 'ok', 'pay': 1000, 'kullanilabilir': 1000, 'birim': 'puan', 'pencere_acik': True}
        for variant in ('clean', 'guarantee', 'helper-free', 'go-free', 'paid', 'unknown-overlap', 'nonterminal', 'bad-denial', 'bad-router'):
            with self.subTest(variant=variant):
                rid = 'c05-' + variant
                now = time.time()
                if variant not in ('clean', 'nonterminal', 'bad-denial', 'bad-router'):
                    provider = 'opencode-go:free' if variant == 'go-free' else 'opencode:free' if variant == 'helper-free' else 'openrouter'
                    actual = 1 if variant in ('go-free', 'helper-free') else 20 if variant == 'paid' else None
                    ident = self.reservation(provider=provider, actual=actual, run=rid, guvence=variant == 'guarantee')
                    start = now - 10 if variant == 'unknown-overlap' else now + 6
                    with self.ledger.transaction() as con:
                        con.execute("update reservation set created=?,metadata=json_set(metadata,'$.kesin_ts',?) where id=?",
                                    (start, now + 7 if actual is not None else None, ident))
                deny = self.home / 'runs' / rid / 'tasks/g1/attempts/1/budget-denied.json'
                deny.parent.mkdir(parents=True)
                deny.write_text(json.dumps({'time': now + 10, 'refusals': [{'provider': 'opencode-go',
                    'scope': 'provider', 'reason': 'provider window closed'}]}))
                if variant == 'bad-denial':
                    deny.write_text('{}')
                router = self.home / 'zincir/saglayici-durum.json'
                if variant == 'bad-router':
                    router.write_text('broken')
                elif router.exists():
                    router.unlink()
                scenario = self.scenario
                scenario.yol = 'go'
                scenario.kabul = Mock(return_value=rid)
                scenario.agac = Mock(return_value=[rid])
                scenario.baslat = Mock()
                scenario.bekle = Mock(side_effect=[False, True])
                scenario.kayit = Mock(return_value={'state': 'running' if variant == 'nonterminal' else 'completed'})
                scenario.yeniden_bekle = Mock(return_value=False)
                proof = {'final': 'PASS', 'askida': 0, 'izinsiz_yol': [], 'urun': {'rc': 0, 'korunan_ayni': True}}
                with patch.object(cs, 'iki_dosya_repo', return_value=('qa', {})), patch.object(cs, 'kanit', return_value=proof), \
                     patch.object(cs.time, 'time', return_value=now):
                    result = cs.c05(scenario)
                self.assertEqual(result['karar'], 'PASS' if variant in ('clean', 'guarantee', 'helper-free') else 'FAIL', result)

    def test_retry_extension_waits_before_actual_child_and_deadline_never_starts_it(self):
        from kosu import RunStore
        store = RunStore(self.home)
        run = store.accept('a', 'Inspect the source')
        rid = run['run_id']
        retry = time.time() + .12
        store.update(rid, state='retryable', retry_after=retry)
        extended = retry + .15
        def extend():
            time.sleep(.04)
            store.update(rid, retry_after=extended)
        thread = threading.Thread(target=extend)
        thread.start()
        self.addCleanup(thread.join)
        (self.home / 'lib').mkdir()
        marker = self.home / 'launched.json'
        (self.home / 'lib/kosu.py').write_text(f'import pathlib,time\npathlib.Path({str(marker)!r}).write_text(str(time.time()))\n')
        self.assertTrue(self.scenario.yeniden_bekle(rid))
        proc = self.scenario.baslat(rid, '--devam')
        self.assertEqual(proc.wait(timeout=3), 0)
        self.assertGreaterEqual(float(marker.read_text()), extended)
        marker.unlink()
        store.update(rid, retry_after=time.time() + 30)
        self.scenario.son_monotonic = time.monotonic() + .05
        self.assertFalse(self.scenario.yeniden_bekle(rid))
        with self.assertRaises(TimeoutError):
            self.scenario.baslat(rid, '--devam')
        self.assertFalse(marker.exists())
        store.update(rid, retry_after='unknown')
        self.scenario.son_monotonic = time.monotonic() + 1
        with self.assertRaisesRegex(ValueError, 'retry'):
            self.scenario.yeniden_bekle(rid)

    def test_low_cap_needs_a_budget_denial_and_a_stopped_run(self):
        import canli_senaryo as cs
        (self.home / 'test_calc.py').write_text(cs.TEST_CALC)
        for variant in ('budget', 'permission', 'running'):
            with self.subTest(variant=variant):
                scenario = self.scenario
                scenario.repo = self.home
                scenario.repo_kur = Mock(return_value='base')
                scenario.kabul = Mock(side_effect=['low', 'cancel'])
                scenario.agac = Mock(return_value=['low'])
                scenario.baslat = Mock()
                scenario.bekle = Mock(side_effect=[True, False, True])
                low = {'state': 'running' if variant == 'running' else 'failed',
                       'denials': [] if variant == 'permission' else [{'reason': 'run budget exhausted: project total cap'}]}
                scenario.kayit = Mock(side_effect=[low, {'state': 'cancelled'}])
                with patch.object(cs.subprocess, 'run'), patch.object(cs, 'alarm_devami', return_value={'kosu': 'alarm', 'gecti': True}):
                    result = cs.c06(scenario)
                self.assertEqual(result['karar'], 'PASS' if variant == 'budget' else 'FAIL', result)


if __name__ == '__main__':
    unittest.main()
