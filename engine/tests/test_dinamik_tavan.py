"""G-B5/P2: cap from the plan, evidenced progress, a hard provider limit and the project total."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import butce_kapisi as bk
from kosu import RunStore, kosu_tavani_tukendi
import proje
from test_butce_donemi import snapshot


class DinamikTavanTest(unittest.TestCase):
    def test_healthy_caps_repeat_but_stale_manual_and_provider_limits_hold(self):
        # A setup where cap growth spends $2 within seconds; the spending alarm (4b) is a separate contract, off here.
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'CADRE_HOME': tmp, 'CADRE_KOSU_TAVAN_USD': '', 'CADRE_GOREV_ID': '',
                                                                            'CADRE_HARCAMA_ALARMI': '0'}):
            home = Path(tmp)
            store = RunStore(home)
            ledger = bk.BudgetLedger(home / 'zincir/request-budget.db')
            rid = store.accept('fixture', 'task')['run_id']
            store.update(rid, budget_cap_usd=2, budget_plan_cap_usd=2)
            bk.ilerleme_yaz(home, rid, 'qa-pass', 'revision1')
            from types import SimpleNamespace
            stamp = int(time.time())
            commit_record = {'worktree': str(home), 'source_sha': 'old', 'created': stamp + .5}
            with patch.object(bk.subprocess, 'run', return_value=SimpleNamespace(stdout=f'new {stamp}')):
                self.assertIsNone(bk.tavan_ilerlemesi(home, commit_record), 'ham commit dogrulanmis ilerleme degildir')
            with patch.object(bk.subprocess, 'run', return_value=SimpleNamespace(stdout=f'old {stamp}')):
                self.assertIsNone(bk.tavan_ilerlemesi(home, commit_record))
            for expected in (2,):
                ident = ledger.reserve('opencode-go', 1_000_000, snapshot(50), {'run_id': rid},
                                       run_cap=bk.rol_tavani(home, rid), dilim='seritler')
                ledger.settle(ident, 1_000_000, ident)
                self.assertEqual(store.read(rid)['budget_cap_usd'], expected)
            with self.assertRaises(bk.BudgetDenied):
                ledger.reserve('opencode-go', 1_000_000, snapshot(50), {'run_id': rid},
                               run_cap=bk.rol_tavani(home, rid), dilim='seritler')
            self.assertEqual(store.read(rid).get('budget_extensions', []), [])
            store.update(rid, budget_spent_usd=4.5, denials=[{'reason': 'run budget exhausted'}])
            bk.ilerleme_yaz(home, rid, 'qa-pass', 'revision3')
            self.assertTrue(kosu_tavani_tukendi(store.read(rid), home))
            for reason in ('provider', 'stale', 'manual'):
                store.update(rid, budget_manual=reason == 'manual', budget_progress={
                    'ts': time.time() - (1801 if reason == 'stale' else 0), 'neden': 'qa-pass', 'kimlik': 'revision1'})
                with self.assertRaises(bk.BudgetDenied):
                    ledger.reserve('opencode-go', 2_000_000, snapshot(0 if reason == 'provider' else 50),
                                   {'run_id': rid}, run_cap=bk.rol_tavani(home, rid))
                self.assertEqual(store.read(rid)['budget_cap_usd'], 2, reason)
                if reason != 'provider':
                    self.assertTrue(kosu_tavani_tukendi(store.read(rid), home))
            # A read-only re-run/QA cannot present the same product as new progress.
            stamp = store.read(rid)['budget_progress']['ts']
            bk.ilerleme_yaz(home, rid, 'qa-pass', 'revision1')
            self.assertEqual(store.read(rid)['budget_progress']['ts'], stamp)

            with patch.dict(os.environ, {'CADRE_KOSU_TAVAN_USD': '4'}):
                parent = store.accept('project', 'project')['run_id']
            proje.proje_tavani_yaz(store, parent, [{'id': 'f1', 'kapsam_puani': 25, 'tahmin_usd': 2},
                                                 {'id': 'f2', 'kapsam_puani': 15}])
            self.assertEqual(store.read(parent)['project_budget_cap_usd'], 4)
            children = [store.accept('child', 'feature')['run_id'] for _ in range(2)]
            store.update(parent, project_run_ids=[parent, *children], project_active_run=children[-1])
            cash_budget = {**snapshot(50), 'or': {'db': 'ok', 'pay': 50, 'kullanilabilir': 50}}
            for child, amount in zip(children, (2_000_000, 1_500_000)):
                store.update(child, proje_ust=parent)
                ident = ledger.reserve('openrouter', amount, cash_budget, {'run_id': child}, run_cap=10_000_000)
                ledger.settle(ident, amount, ident)
            bk.ilerleme_yaz(home, children[-1], 'qa-pass', 'child-revision')
            ident = ledger.reserve('openrouter', 500_000, cash_budget, {'run_id': parent}, run_cap=10_000_000)
            self.assertEqual(store.read(parent)['project_budget_cap_usd'], 4)
            cost = proje.proje_maliyeti(home, [parent])
            self.assertEqual(cost['olculen_usd'], 3.5)
            self.assertEqual(cost['tahmini_usd'], 0)
            self.assertEqual(cost['askida_usd'], .5)
            self.assertEqual(cost['yukumluluk_usd'], 4)
            store.update(parent, budget_progress={'ts': 0})
            store.update(children[-1], budget_progress={'ts': 0})
            with self.assertRaisesRegex(bk.BudgetDenied, 'project total cap'):
                ledger.reserve('openrouter', 2_000_000, cash_budget, {'run_id': children[-1]}, run_cap=10_000_000)
            self.assertEqual(store.read(parent)['project_budget_cap_usd'], 4)
            # If the detailed child plan is larger than the first estimate the total cap carries it too.
            proje.ozellik_tavani_guncelle(store, parent, 'f1', 8)
            proje.proje_tavani_yaz(store, parent, [{'id': 'f1', 'kapsam_puani': 25}, {'id': 'f2', 'kapsam_puani': 15}])
            self.assertEqual(store.read(parent)['project_budget_cap_usd'], 4)
            import yonlendirici as yon
            store.update(rid, budget_manual=False)
            bk.ilerleme_yaz(home, rid, 'qa-pass', 'revision4')
            with patch.dict(os.environ, {'CADRE_RUN_ID': rid}), patch.object(yon, 'QE', home), \
                 patch.object(yon, 'kapi_rezervasyonu', return_value=.5):
                self.assertEqual(yon.yazar_nefesi('opencode-go/glm-5.3-flash', snapshot(50)), (.5, 1.0, 'cep'))
            self.assertEqual(store.read(rid)['budget_cap_usd'], 2)
            with patch.object(bk, 'seritler_bitti', return_value=True):
                with self.assertRaises(bk.BudgetDenied):
                    ledger.reserve('opencode-go', 100_000, snapshot(50), {'run_id': rid}, run_cap=bk.rol_tavani(home, rid))
            self.assertEqual(store.read(rid)['budget_cap_usd'], 2)


class ProjectOperatorBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='project-bound-')
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        manager = patch.dict(os.environ, {'CADRE_HOME': str(self.home), 'CADRE_KOSU_TAVAN_USD': '1',
                                         'CADRE_HARCAMA_ALARMI': '0', 'CADRE_GOREV_ID': ''})
        manager.start()
        self.addCleanup(manager.stop)
        self.store = RunStore(self.home)
        self.parent = self.store.accept('project', 'selected work')['run_id']
        proje.proje_butcesi_sabitle(self.store, self.parent)
        self.ledger = bk.BudgetLedger(self.home / 'zincir/request-budget.db')
        self.budget = snapshot(50)
        self.budget.update(zen={'db': 'ok', 'pay': 50, 'kullanilabilir': 50},
                           **{'or': {'db': 'ok', 'pay': 50, 'kullanilabilir': 50}})

    def reserve(self, rid, value, provider='openrouter', role='seritler'):
        return self.ledger.reserve(provider, value, self.budget, {'run_id': rid, 'role': role},
                                   run_cap=10_000_000, dilim=role)

    def test_all_descendants_retries_and_controls_share_fixed_cash_without_mixing_subscription_usage(self):
        child = self.store.accept('child', 'implementation', parent_run_id=self.parent)['run_id']
        retry = self.store.accept('retry', 'review', parent_run_id=child)['run_id']
        for rid, value, provider, role in ((self.parent, 200_000, 'openrouter', 'plan'),
                                          (child, 300_000, 'opencode', 'seritler')):
            ident = self.reserve(rid, value, provider, role)
            self.ledger.settle(ident, value, ident)
        self.reserve(retry, 200_000, role='kalite')
        self.reserve(child, 800_000, 'opencode-go')
        with self.assertRaisesRegex(bk.BudgetDenied, 'project total cap'):
            self.reserve(self.parent, 300_001, role='kalite')
        self.reserve(self.parent, 300_000, role='kalite')
        proje.proje_tavani_yaz(self.store, self.parent, [{'id': 'f1', 'tahmin_usd': 100}])
        proje.ozellik_tavani_guncelle(self.store, self.parent, 'f1', 1000)
        self.assertEqual(self.store.read(self.parent)['project_budget_cap_usd'], 1)
        for rid in (self.parent, child, retry):
            with self.assertRaisesRegex(bk.BudgetDenied, 'project total cap'):
                self.reserve(rid, 1)
        self.assertEqual(self.store.read(self.parent)['project_operator_budget']['scope'], 'operator-total')
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.store.update(self.parent, operator_budget_usd=100)

    def test_concurrent_reservations_hold_the_verified_upper_bound_and_restart_cannot_refund_it(self):
        from concurrent.futures import ThreadPoolExecutor
        children = [self.store.accept('child', 'implementation', parent_run_id=self.parent)['run_id'] for _ in range(2)]
        def reserve(rid):
            try:
                return self.reserve(rid, 700_000)
            except bk.BudgetDenied:
                return None
        with patch.object(self.ledger, 'kalibre', return_value=200_000), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, children))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.ledger = bk.BudgetLedger(self.ledger.path)
        with self.assertRaisesRegex(bk.BudgetDenied, 'project total cap'):
            self.reserve(self.parent, 300_001)
        self.reserve(self.parent, 300_000)
        with self.assertRaisesRegex(bk.BudgetDenied, 'project total cap'):
            self.reserve(children[-1], 1)
        before = self.ledger.path.read_bytes()
        import sqlite3
        with bk.closing(sqlite3.connect(self.ledger.path)) as connection:
            self.assertEqual(bk.dinamik_tavanlar(self.home, connection, self.parent, None, 'kalite', 1, False, 'openrouter')[1], 0)
        self.assertEqual(self.ledger.path.read_bytes(), before)

    def test_over_bound_receipt_preserves_actual_and_stops_every_paid_path_of_the_project(self):
        from kosu import approve_spending
        child = self.store.accept('child', 'implementation', parent_run_id=self.parent)['run_id']
        ident = self.reserve(child, 100_000, 'openrouter')
        self.ledger.settle(ident, 100_001, 'unexpected-overrun')
        self.ledger.settle(ident, 100_001, 'unexpected-overrun')
        self.ledger = bk.BudgetLedger(self.ledger.path)
        approve_spending(self.store, self.parent)
        for provider in ('opencode', 'opencode-go'):
            with self.assertRaisesRegex(bk.BudgetDenied, 'receipt bound exceeded'):
                self.reserve(self.parent, 1, provider)
        with self.ledger.transaction() as con:
            self.assertEqual(bk.run_usage(con, child)[0], 100_001)
            self.assertEqual(con.execute('select count(*) from reservation').fetchone()[0], 1)
        independent = self.store.accept('independent', 'other work')['run_id']
        self.reserve(independent, 1, 'opencode')

    def test_calibrated_receipt_within_verified_bound_is_not_a_project_overrun(self):
        child = self.store.accept('child', 'implementation', parent_run_id=self.parent)['run_id']
        with patch.object(self.ledger, 'kalibre', return_value=20_000):
            ident = self.reserve(child, 100_000, 'openrouter')
        self.ledger.settle(ident, 90_000, 'within-verified-bound')
        self.ledger.settle(ident, 90_000, 'within-verified-bound')
        self.reserve(self.parent, 910_000, 'opencode')
        with self.assertRaisesRegex(bk.BudgetDenied, 'project total cap'):
            self.reserve(child, 1, 'opencode')

    def guarantees(self):
        child = self.store.accept('child', 'implementation', parent_run_id=self.parent)['run_id']
        spent = self.reserve(child, 800_000)
        self.ledger.settle(spent, 800_000, spent)
        metadata = {'run_id': self.parent, 'role': 'kalite', 'phase_role': 'kalite',
                    'guvence': True, 'guvence_rol': 'kalite', 'model': 'old', 'dilim': 'hakem'}
        old = [self.ledger.reserve('openrouter', 100_000, self.budget, metadata, run_cap=200_000)
               for _ in range(2)]
        return old, {**metadata, 'model': 'new'}

    def guarantee_rows(self):
        with self.ledger.transaction() as connection:
            return connection.execute('select * from reservation order by id').fetchall()

    def transfer(self, metadata, maximum=100_000, budget=None, **kwargs):
        return self.ledger.guvence_tasi(self.parent, 'kalite', 'opencode', maximum,
                                       self.budget if budget is None else budget, metadata,
                                       run_cap=200_000, dilim='hakem', **kwargs)

    def test_acceptance_transfer_reuses_only_unused_guarantees_at_the_fixed_project_cap(self):
        import json
        old, metadata = self.guarantees()
        result = self.transfer(metadata)
        self.assertIsNotNone(result, 'equal replacement must fit without additional operator budget')
        self.assertEqual(result['eski'], old)
        self.assertEqual(len(result['yeni']), 2)
        repeated = self.transfer(metadata)
        self.assertEqual(repeated['yeni'], result['yeni'], 'same replacement must keep the reservation identities')
        rows = self.guarantee_rows()
        self.assertEqual(len(rows), 5)
        self.assertEqual(sum(row[5] or 0 for row in rows), 800_000)
        self.assertEqual(sum(row[4] for row in rows if row[5] is None), 200_000)
        for row in rows:
            if row[0] in result['yeni']:
                self.assertEqual(json.loads(row[7])['guvence_onceki'], old)
        self.assertEqual(self.store.read(self.parent)['project_budget_cap_usd'], 1)
        with self.assertRaisesRegex(bk.BudgetDenied, 'project total cap'):
            self.reserve(self.parent, 1)

    def test_partial_replacement_denial_rolls_back_every_new_row_and_keeps_old_guarantees(self):
        old, metadata = self.guarantees()
        before = self.guarantee_rows()
        budget = {**self.budget, 'zen': {'db': 'ok', 'pay': .15, 'kullanilabilir': .15}}
        self.assertIsNone(self.transfer(metadata, budget=budget))
        self.assertEqual(self.guarantee_rows(), before)
        self.assertIsNone(self.transfer(metadata, maximum=100_001))
        self.assertEqual(self.guarantee_rows(), before)
        self.assertEqual(self.transfer(metadata, budget=budget, once_birak=True)['yeni'], [])
        rows = self.guarantee_rows()
        self.assertEqual(len(rows), len(before))
        self.assertTrue(all(row[5] == 0 for row in rows if row[0] in old))

    def test_concurrent_identical_transfers_share_one_replacement_and_a_partial_failure_preserves_it(self):
        from concurrent.futures import ThreadPoolExecutor
        old, metadata = self.guarantees()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.transfer(metadata), range(4)))
        self.assertTrue(all(result is not None for result in results))
        self.assertTrue(all(result['yeni'] == results[0]['yeni'] for result in results))
        self.assertEqual(len(self.guarantee_rows()), 5)
        before = self.guarantee_rows()
        original = self.ledger.reserve
        attempts = 0
        def fail_second(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise OSError('fixture disk failure')
            return original(*args, **kwargs)
        with patch.object(self.ledger, 'reserve', side_effect=fail_second):
            with self.assertRaisesRegex(OSError, 'fixture disk failure'):
                self.transfer({**metadata, 'model': 'next'})
        self.ledger = bk.BudgetLedger(self.ledger.path)
        self.assertEqual(self.guarantee_rows(), before)
        with self.assertRaises(bk.BudgetDenied):
            self.transfer({**metadata, 'run_id': 'unrelated'})
        self.assertEqual(self.guarantee_rows(), before)

    def test_acceptance_entry_serializes_transfers_and_recovers_a_failed_record_write(self):
        from concurrent.futures import ThreadPoolExecutor
        import kabul
        import qe_core as core
        old, metadata = self.guarantees()
        self.store.update(self.parent, kabul_guvencesi={'modeller': {'kalite': 'openrouter/old'}, 'rezervasyonlar': old})
        original = self.store.update
        def fail_record(rid, **values):
            if 'kabul_guvencesi' in values:
                raise OSError('fixture record failure')
            return original(rid, **values)
        log = self.home / 'transfer.log'
        with patch.object(core, 'QE', self.home), patch.object(kabul, '_guvence_tutari', return_value=100_000):
            with patch.object(self.store, 'update', side_effect=fail_record):
                with self.assertRaisesRegex(OSError, 'fixture record failure'):
                    kabul.guvence_tasi(self.store, self.parent, 'opencode/new', self.budget, log)
            self.assertEqual(self.store.read(self.parent)['kabul_guvencesi']['rezervasyonlar'], old)
            self.assertEqual(len(self.guarantee_rows()), 5)
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: kabul.guvence_tasi(
                    self.store, self.parent, 'opencode/new', self.budget, log), range(4)))
            self.assertTrue(all(results))
        current = self.store.read(self.parent)
        opened = [row[0] for row in self.guarantee_rows() if row[5] is None]
        self.assertEqual(set(current['kabul_guvencesi']['rezervasyonlar']), set(opened))
        self.assertEqual(current['kabul_guvencesi']['modeller']['kalite'], 'opencode/new')
        self.assertEqual(len(self.guarantee_rows()), 5)
        self.assertEqual((current['budget_spent_usd'], current['budget_held_usd']), (0, .2))

    def test_process_death_during_replacement_keeps_old_guarantees_until_restart(self):
        import json
        import subprocess
        old, metadata = self.guarantees()
        before = self.guarantee_rows()
        marker = self.home / 'inside-transfer'
        script = self.home / 'interrupt-transfer.py'
        script.write_text('''import json,sys,time
from pathlib import Path
import butce_kapisi as bk
class InterruptedLedger(bk.BudgetLedger):
    def reserve(self,*args,**kwargs):
        ident=super().reserve(*args,**kwargs)
        Path(sys.argv[2]).write_text(ident)
        time.sleep(30)
        return ident
data=json.loads(sys.argv[3])
InterruptedLedger(Path(sys.argv[1])).guvence_tasi(data['rid'],'kalite','opencode',100000,
    data['budget'],data['metadata'],run_cap=200000,dilim='hakem')
''')
        env = {**os.environ, 'PYTHONPATH': str(Path(bk.__file__).parent)}
        with subprocess.Popen([sys.executable, str(script), str(self.ledger.path), str(marker),
                               json.dumps({'rid': self.parent, 'budget': self.budget, 'metadata': metadata})],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True) as process:
            try:
                deadline = time.monotonic() + 5
                while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(marker.exists(), 'process must reach the uncommitted replacement')
                process.kill()
                process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)
        self.ledger = bk.BudgetLedger(self.ledger.path)
        self.assertEqual(self.guarantee_rows(), before)
        result = self.transfer(metadata)
        self.assertEqual(result['eski'], old)
        self.assertEqual(len(result['yeni']), 2)


class HarcamaAlarmiTest(unittest.TestCase):
    def test_high_rate_warning_does_not_disable_the_real_repeated_tool_process_guard(self):
        import json
        import subprocess
        import sys
        import qe_core as core
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                'CADRE_HOME': tmp, 'CADRE_KOSU_TAVAN_USD': '4', 'CADRE_GOREV_ID': '', 'CADRE_ARAC_TEKRAR_ESIK': '3'}):
            home, store = Path(tmp), RunStore(tmp)
            rid = store.accept('task', 'task')['run_id']
            ledger = bk.BudgetLedger(home / 'zincir/request-budget.db')
            ident = ledger.reserve('opencode-go', 1_700_000, snapshot(50), {'run_id': rid})
            ledger.settle(ident, 1_700_000, ident)
            ledger.reserve('opencode-go', 1, snapshot(50), {'run_id': rid})
            log = home / 'repeated-tools.jsonl'
            log.write_text('')
            proc = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'], start_new_session=True)
            try:
                guard = core.StallGuard(str(log), proc, 600)
                with log.open('a') as stream:
                    stream.write((json.dumps({'type': 'tool_use', 'part': {'tool': 'bash', 'state': {
                        'status': 'completed', 'input': {'command': 'same failing test'}, 'output': 'same failure'}}}) + '\n') * 3)
                with patch.object(core, 'cancel_path', return_value=home / 'no-cancel'):
                    self.assertEqual(guard.tik('task'), 'dongu')
                self.assertIsNotNone(proc.wait(timeout=5))
                self.assertEqual(store.read(rid)['harcama_uyarisi']['ilerleme'], 'unknown')
                self.assertNotIn('budget_progress', store.read(rid))
                self.assertIn('ARAC-TEKRARI', log.read_text())
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=5)

    def test_speed_only_is_a_warning_with_productive_or_unknown_progress_and_hard_caps_hold(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                'CADRE_HOME': tmp, 'CADRE_KOSU_TAVAN_USD': '4', 'CADRE_GOREV_ID': ''}):
            home, store = Path(tmp), RunStore(tmp)
            ledger = bk.BudgetLedger(home / 'zincir/request-budget.db')
            budget = snapshot(50)
            for evidence in ('qa-pass', 'missing', 'cpu', 'log', 'connection', 'stale', 'future'):
                rid = store.accept(evidence, 'task')['run_id']
                progress = {'neden': 'qa-pass' if evidence in ('qa-pass', 'stale', 'future') else evidence,
                            'kimlik': 'revision-' + evidence, 'ts': time.time()}
                if evidence == 'stale':
                    progress['ts'] -= 1801
                elif evidence == 'future':
                    progress['ts'] += 3600
                if evidence != 'missing':
                    store.update(rid, budget_progress=progress)
                first = ledger.reserve('opencode-go', 1_700_000, budget, {'run_id': rid})
                ledger.settle(first, 1_700_000, first)
                for _ in range(2):
                    ident = ledger.reserve('opencode-go', 10_000, budget, {'run_id': rid})
                    ledger.settle(ident, 10_000, ident)
                record = store.read(rid)
                self.assertNotIn('harcama_alarmi', record)
                warning = record['harcama_uyarisi']
                self.assertEqual(warning['kind'], 'speed-warning')
                self.assertEqual(warning['ilerleme'], 'productive' if evidence == 'qa-pass' else 'unknown')
                self.assertNotIn('harcama_onayi_ts', record)
                with self.assertRaises(bk.BudgetDenied):
                    ledger.reserve('opencode-go', 1, snapshot(0), {'run_id': rid})
                with self.assertRaisesRegex(bk.BudgetDenied, 'run budget exhausted'):
                    ledger.reserve('opencode-go', 10_000, budget, {'run_id': rid}, run_cap=1_720_000)

    def test_cash_and_subscription_speed_are_not_added_and_legacy_alarm_still_requires_approval(self):
        from kosu import approve_spending
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                'CADRE_HOME': tmp, 'CADRE_KOSU_TAVAN_USD': '4', 'CADRE_GOREV_ID': ''}):
            home, store = Path(tmp), RunStore(tmp)
            rid = store.accept('task', 'task')['run_id']
            ledger = bk.BudgetLedger(home / 'zincir/request-budget.db')
            budget = snapshot(50)
            budget['or'] = {'db': 'ok', 'pay': 50, 'kullanilabilir': 50}
            for provider in ('opencode-go', 'openrouter'):
                ident = ledger.reserve(provider, 900_000, budget, {'run_id': rid})
                ledger.settle(ident, 900_000, ident)
            ledger.reserve('openrouter', 1, budget, {'run_id': rid})
            self.assertNotIn('harcama_uyarisi', store.read(rid), 'different units cannot create a speed warning')
            store.update(rid, harcama_alarmi={'ts': time.time(), 'neden': 'operator stop'})
            with self.assertRaisesRegex(bk.BudgetDenied, 'onay bekleniyor'):
                ledger.reserve('openrouter', 1, budget, {'run_id': rid})
            approve_spending(store, rid)
            ledger.reserve('openrouter', 1, budget, {'run_id': rid})
            with self.assertRaises(bk.BudgetDenied):
                ledger.reserve('opencode-go', 1, snapshot(0), {'run_id': rid})

    def test_spending_spike_thresholds_warn_without_an_approval_loop(self):
        """N-BU06 v8: the 40% / 3x thresholds are observations; the definite alarm is tested on a separate path."""
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'CADRE_HOME': tmp, 'CADRE_KOSU_TAVAN_USD': '', 'CADRE_GOREV_ID': ''}):
            home = Path(tmp)
            store = RunStore(home)
            ledger = bk.BudgetLedger(home / 'zincir/request-budget.db')
            rid = store.accept('fixture', 'task')['run_id']
            store.update(rid, budget_cap_usd=3, budget_manual=True)
            ilk = ledger.reserve('opencode-go', 1_400_000, snapshot(50), {'run_id': rid})
            ledger.settle(ilk, 1_300_000, 'makbuz')                     # $1,30 > $3 x %40
            for _ in range(2):
                ledger.reserve('opencode-go', 10_000, snapshot(50), {'run_id': rid})
            self.assertIn('%40', store.read(rid)['harcama_uyarisi']['neden'])
            self.assertNotIn('harcama_alarmi', store.read(rid))
            self.assertNotIn('harcama_onayi_ts', store.read(rid))
            # 3 times the average speed (only in a run older than 30 min and above the base amount)
            rid2 = store.accept('fixture2', 'task')['run_id']
            simdi = time.time()
            store.update(rid2, budget_cap_usd=2, budget_manual=True, created=simdi - 3600)
            with ledger.transaction() as con:
                for ne_zaman, usd in ((simdi - 3000, 0.10), (simdi - 60, 0.15)):
                    con.execute('insert into reservation values(?,?,?,?,?,?,?,?)', (f'r{ne_zaman}', 'opencode-go', 'p', ne_zaman,
                                int(usd * 1e6), int(usd * 1e6), f'makbuz-{ne_zaman}', '{"run_id": "%s"}' % rid2))
                self.assertIsNone(bk.harcama_alarmi(home, con, rid2, simdi)[0], '$0,15 tabanin ($0,20) altinda')
                con.execute('update reservation set actual=500000, reserved=500000 where created=?', (simdi - 60,))
                denial, warning = bk.harcama_alarmi(home, con, rid2, simdi)
                self.assertIsNone(denial)
                self.assertIn('3 kati', warning['neden'])
                store.update(rid2, budget_cap_usd=5)
                self.assertIsNone(bk.harcama_alarmi(home, con, rid2, simdi)[0], '07.10 rapor yazari: $0,50 tavanin %20sinin altinda')
                con.execute('update reservation set actual=900000, reserved=900000 where created=?', (simdi - 60,))
                con.execute('update reservation set actual=1600000, reserved=1600000 where created=?', (simdi - 3000,))
                store.update(rid2, budget_cap_usd=2)
                self.assertIsNone(bk.harcama_alarmi(home, con, rid2, simdi)[0], '$0,90 > %40 ama mutlak taban $1')
