"""G-B5/N-TS01: review cost, tools and repeats are finite on restart too."""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import butce_kapisi as bk
import qe_core as core
import sinirlar
from kosu import RunStore, atomic_json
from test_butce_donemi import snapshot


class IncelemeSiniriTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='review-limits-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        env = patch.dict(os.environ, {'CADRE_HOME': str(self.home), 'CADRE_KOSU_TAVAN_USD': '10',
                                     'CADRE_HARCAMA_ALARMI': '0', 'CADRE_AG_YOK': '1', 'CADRE_GOREV_ID': ''})
        env.start()
        self.addCleanup(env.stop)
        self.store = RunStore(self.home)
        self.rid = self.store.accept('lab', 'TUR: inceleme\nInspect the selected public source')['run_id']
        self.ledger = bk.BudgetLedger(self.home / 'zincir/request-budget.db')
        self.budget = snapshot(50)
        self.budget.update(zen={'db': 'ok', 'pay': 50, 'kullanilabilir': 50},
                           **{'or': {'db': 'ok', 'pay': 50, 'kullanilabilir': 50}})
        self.task, self.role = 'review-author-abcdef012345', 'impl'
        self.yazar = sinirlar.inceleme_politikasi()['review-author']

    def reserve(self, value=10_000, provider='openrouter', task=None, role=None):
        role = role or self.role
        cap, dilim = bk.rol_butcesi(self.home, self.rid, role, provider)
        return self.ledger.reserve(provider, value, self.budget, {'run_id': self.rid,
                                   'task_id': task or self.task, 'phase_role': role}, cap, dilim)

    def control(self, phase='review-author'):
        with self.ledger.transaction() as con:
            return con.execute('select started,deadline,calls,tools,policy from review_control where run_id=? and phase=?',
                               (self.rid, phase)).fetchone()

    def test_defaults_are_snapshotted_and_cannot_be_expanded_through_run_update_or_environment(self):
        policy = self.store.read(self.rid)['review_limits']
        self.assertEqual(policy, sinirlar.inceleme_politikasi())
        self.assertEqual([(policy[p]['istek'], policy[p]['arac'], policy[p]['sure_sn'], policy[p]['pay_yuzde'])
                          for p in ('plan-review', 'review-author', 'review-final', 'review-recheck')],
                         [(7, 12, 300, 20), (9, 16, 480, 15), (6, 10, 510, 30), (6, 10, 510, 30)])
        self.assertEqual(policy['cepler'], {'seritler': 0.40, 'hakem': 0.50, 'yardimci': 0.10})
        fazlar = [policy[p] for p in policy if p not in ('surum', 'cepler')]
        self.assertEqual([sum(f[k] for f in fazlar) for k in ('istek', 'arac', 'sure_sn', 'pay_yuzde')], [28, 48, 1800, 95],
                         'v1 toplam istek/arac/sure/pay korunur; yalniz fazlar arasinda yeniden dagitilir')
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.store.update(self.rid, review_limits={'surum': 'expanded'})
        with patch.dict(os.environ, {'CADRE_REVIEW_REQUESTS': '10000', 'CADRE_REVIEW_SECONDS': '10000'}):
            deadline = self.ledger.begin_review(self.rid, self.task, self.role)['deadline']
        row = self.control()
        self.assertEqual(deadline, row[0] + 480)
        self.assertEqual(json.loads(row[4])['istek'], 9)

    def test_admitted_requests_count_across_settlement_new_tasks_models_providers_and_restart(self):
        for n in range(self.yazar['istek']):
            ident = self.reserve(task=f'review-author-{n:012x}', provider='opencode-go' if n % 2 else 'openrouter')
            self.ledger.settle(ident, 0, ident)
        self.ledger = bk.BudgetLedger(self.ledger.path)
        with self.assertRaisesRegex(bk.BudgetDenied, 'review-author: requests'):
            self.reserve(task='review-author-ffffffffffff', provider='opencode')
        with self.assertRaisesRegex(bk.BudgetDenied, 'requests'):
            self.ledger.begin_review(self.rid, self.task, self.role)
        self.assertEqual(self.control()[2], self.yazar['istek'])
        # The final judge is a different phase; the writer limit does not prevent independent acceptance.
        self.reserve(task='review-final-abcdef012345', role='kalite')
        self.assertEqual(self.control('review-final')[2], 1)

    def test_code_plan_review_uses_its_role_pocket_without_a_second_cash_partition(self):
        from sozlesme import olustur
        with patch.dict(os.environ, {'CADRE_KOSU_TAVAN_USD': '1.221243'}):
            record = self.store.accept('lab', 'Add the missing last-page assertion')
        rid = record['run_id']
        olustur(self.home, rid, paylar={'seritler': 0.36, 'hakem': 0.24, 'yardimci': 0.40})
        policy = record['review_limits']
        self.assertEqual((policy['plan-review']['istek'], policy['plan-review']['arac'],
                          policy['plan-review']['sure_sn']), (7, 12, 300))
        cap, pocket = bk.rol_butcesi(self.home, rid, 'review', 'openrouter')
        final_before = bk.rol_butcesi(self.home, rid, 'kalite', 'openrouter')[0]
        self.assertEqual((cap, pocket), (488_497, 'yardimci'))
        first = self.ledger.reserve('openrouter', 212_572, self.budget,
                                    {'run_id': rid, 'task_id': 'plan-review-1-abcdef01', 'phase_role': 'review'}, cap, pocket)
        self.ledger.settle(first, 15_995, 'first-code-plan-call')
        # The second call fits the fixed role pocket but not the report job's 20% phase partition.
        self.ledger.reserve('openrouter', 260_000, self.budget,
                            {'run_id': rid, 'task_id': 'plan-review-1-abcdef01', 'phase_role': 'review'}, cap, pocket)
        self.assertEqual(bk.rol_butcesi(self.home, rid, 'kalite', 'openrouter')[0], final_before)
        with self.assertRaisesRegex(bk.BudgetDenied, 'run budget exhausted'):
            self.ledger.reserve('openrouter', 212_503, self.budget,
                                {'run_id': rid, 'task_id': 'plan-review-2-abcdef02', 'phase_role': 'review'}, cap, pocket)
        with self.ledger.transaction() as con:
            context = self.ledger.review_context(con, rid, 'plan-review-2-abcdef02', 'review')
        self.assertEqual(context['calls'], 2)
        self.assertEqual(context['limit'], 1_221_243)

    def test_legacy_code_review_keeps_its_frozen_phase_share(self):
        from sozlesme import olustur
        with patch.dict(os.environ, {'CADRE_KOSU_TAVAN_USD': '1.221243'}):
            record = self.store.accept('lab', 'Legacy code review')
        rid = record['run_id']
        record['review_limits'] = sinirlar.inceleme_politikasi()
        atomic_json(self.store.path(rid) / 'run.json', record)
        olustur(self.home, rid, paylar={'seritler': 0.36, 'hakem': 0.24, 'yardimci': 0.40})
        cap, pocket = bk.rol_butcesi(self.home, rid, 'review', 'openrouter')
        with self.assertRaisesRegex(bk.BudgetDenied, 'plan-review: cost'):
            self.ledger.reserve('openrouter', 260_000, self.budget,
                                {'run_id': rid, 'task_id': 'plan-review-1-abcdef01', 'phase_role': 'review'}, cap, pocket)
        self.assertEqual(self.store.read(rid)['review_limits']['plan-review']['pay_yuzde'], 20)

    def test_post_repair_judge_has_its_own_share_and_never_resets_the_first_judge_counters(self):
        self.assertEqual(bk.inceleme_fazi('review-recheck-abcdef012345', 'kalite'), 'review-recheck')
        self.assertIsNone(bk.inceleme_fazi('review-recheck-abcdef012345', 'impl'))
        ilk = sinirlar.inceleme_politikasi()['review-final']
        for n in range(ilk['istek']):
            ident = self.reserve(task=f'review-final-{n:012x}', role='kalite')
            self.ledger.settle(ident, 0, ident)
        with self.assertRaisesRegex(bk.BudgetDenied, 'review-final: requests'):
            self.reserve(task='review-final-ffffffffffff', role='kalite')
        self.reserve(task='review-recheck-abcdef012345', role='kalite')
        self.assertEqual((self.control('review-final')[2], self.control('review-recheck')[2]), (ilk['istek'], 1))
        with self.assertRaisesRegex(bk.BudgetDenied, 'review-final: requests'):
            self.reserve(task='review-final-eeeeeeeeeeee', role='kalite')
        yeniden = sinirlar.inceleme_politikasi()['review-recheck']
        for n in range(yeniden['istek'] - 1):
            ident = self.reserve(task=f'review-recheck-{n:012x}', role='kalite')
            self.ledger.settle(ident, 0, ident)
        with self.assertRaisesRegex(bk.BudgetDenied, 'review-recheck: requests'):
            self.reserve(task='review-recheck-ffffffffffff', role='kalite')

    def test_pending_verified_upper_bounds_and_settled_cost_enforce_phase_cap_without_adding_pools(self):
        pay = 10_000_000 * self.yazar['pay_yuzde'] // 100
        with patch.object(self.ledger, 'kalibre', return_value=1):
            ident = self.reserve(pay - 1_000_000)
            self.assertEqual(self.control()[2], 1)
            with self.assertRaisesRegex(bk.BudgetDenied, 'review-author: cost'):
                self.reserve(1_000_001)
            with patch.dict(os.environ, {'CADRE_GOREV_ID': self.task, 'CADRE_ROL': self.role}):
                before = self.ledger.path.read_bytes()
                remaining = self.ledger.available('openrouter', self.budget, 7_500_000, self.rid,
                                                  'seritler', 1_000_001, self.role, salt_okur=True)
                self.assertEqual(remaining, 1_000_000)
                self.assertEqual(self.ledger.path.read_bytes(), before)
        # Unused cash is not opened a second time on another cash provider.
        with self.assertRaisesRegex(bk.BudgetDenied, 'cost'):
            self.reserve(1_000_001, 'opencode')
        self.reserve(pay - 1_000_000, 'opencode-go')
        self.ledger.settle(ident, pay - 1_000_000, ident)
        with self.assertRaisesRegex(bk.BudgetDenied, 'cost'):
            self.reserve(1_000_001)
        self.assertEqual(self.control()[2], 2)

    def test_denied_provider_and_role_reservations_do_not_consume_request_count_or_create_liabilities(self):
        self.ledger.begin_review(self.rid, self.task, self.role)
        no_credit = {**self.budget, 'or': {'db': 'ok', 'pay': 0, 'kullanilabilir': 0}}
        with self.assertRaises(bk.BudgetDenied):
            self.ledger.reserve('openrouter', 1, no_credit,
                                {'run_id': self.rid, 'task_id': self.task, 'phase_role': self.role}, 7_500_000, 'seritler')
        with self.assertRaisesRegex(bk.BudgetDenied, 'run budget exhausted'):
            self.ledger.reserve('openrouter', 1, self.budget,
                                {'run_id': self.rid, 'task_id': self.task, 'phase_role': self.role}, 0, 'unknown-role')
        self.assertEqual(self.control()[2], 0)
        with self.ledger.transaction() as con:
            self.assertEqual(con.execute('select count(*) from reservation').fetchone()[0], 0)

    def test_concurrent_last_request_is_atomic_and_tool_limit_survives_restart(self):
        for _ in range(self.yazar['istek'] - 1):
            ident = self.reserve()
            self.ledger.settle(ident, 0, ident)
        def reserve(_):
            try:
                return self.reserve()
            except bk.BudgetDenied:
                return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(v is not None for v in pool.map(reserve, range(4))), 1)
        for _ in range(self.yazar['arac'] - 1):
            self.ledger.review_tool(self.rid, self.task, self.role)
        def tool(_):
            try:
                self.ledger.review_tool(self.rid, 'review-author-eeeeeeeeeeee', self.role)
                return True
            except bk.BudgetDenied:
                return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(tool, range(4))), 1)
        self.ledger = bk.BudgetLedger(self.ledger.path)
        with self.assertRaisesRegex(bk.BudgetDenied, 'tools'):
            self.ledger.review_tool(self.rid, self.task, self.role)
        self.assertEqual(self.control()[2:4], (self.yazar['istek'], self.yazar['arac']))

    def test_tool_limit_blocks_only_tools_and_leaves_no_attempt_failure_receipt(self):
        import io
        from contextlib import redirect_stdout
        deneme = self.store.task_dir(self.rid, self.task) / 'attempts' / 'a1'
        deneme.mkdir(parents=True)
        ortam = {'CADRE_RUN_ID': self.rid, 'CADRE_GOREV_ID': self.task, 'CADRE_ROL': self.role, 'CADRE_BUDGET_ATTEMPT': 'a1'}
        def kos(istek):
            cikti = io.StringIO()
            with patch.dict(os.environ, ortam), patch('sys.stdin', io.StringIO(json.dumps(istek))), redirect_stdout(cikti):
                kod = bk.main()
            return kod, cikti.getvalue().strip()
        for _ in range(self.yazar['arac']):
            self.assertEqual(kos({'action': 'review-tool'}), (0, '{}'))
        kod, cikti = kos({'action': 'review-tool'})
        self.assertEqual((kod, json.loads(cikti)), (0, {'tool_limit': 'review limit: review-author: tools'}))
        self.assertFalse((deneme / 'budget-denied.json').exists(), 'arac tavani denemeyi dusuren makbuz yazmaz')
        self.assertIn('review limit: review-author: tools', [d['reason'] for d in self.store.read(self.rid).get('denials', [])])
        with patch.object(bk.BudgetLedger, 'review_tool', side_effect=bk.BudgetDenied('review limit: review-author: time')):
            self.assertEqual(kos({'action': 'review-tool'})[0], 75, 'sure/istek/maliyet reddi gecersiz kilinmaz')
        self.assertTrue((deneme / 'budget-denied.json').exists())

    def test_typed_review_wait_uses_root_work_without_resetting_limits_counters_or_liabilities(self):
        started = time.time()
        record = self.store.read(self.rid)
        record.update(request={'duration_minutes': 40}, request_clock={
            'baslangic': started - 100, 'aktif_sn': 80, 'oturum': None})
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        with patch.object(bk.time, 'time', return_value=started):
            self.reserve()
            self.ledger.review_tool(self.rid, self.task, self.role)
        original = self.control()
        now = started + 1800
        self.ledger = bk.BudgetLedger(self.ledger.path)
        before = self.ledger.path.read_bytes()
        with patch.object(bk.time, 'time', return_value=now), closing(sqlite3.connect(
                self.ledger.path.resolve().as_uri() + '?mode=ro', uri=True)) as con:
            context = self.ledger.review_context(con, self.rid, self.task, self.role)
            self.assertEqual(context['deadline'], now + self.yazar['sure_sn'] - 80)
            self.assertEqual((context['calls'], context['tools']), (1, 1))
            self.assertEqual(sum(row[2] for row in context['rows']), 10_000)
        self.assertEqual(self.ledger.path.read_bytes(), before)
        self.assertEqual(self.control(), original)

        for work in (float('nan'), float('inf'), -1):
            record['request_clock']['aktif_sn'] = work
            atomic_json(self.store.path(self.rid) / 'run.json', record)
            with patch.object(bk.time, 'time', return_value=now), self.assertRaisesRegex(bk.BudgetDenied, 'work clock'):
                self.ledger.begin_review(self.rid, self.task, self.role)
            self.assertEqual(self.control(), original)
        record['request_clock'].update(aktif_sn=80, oturum=now)
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        with patch.object(bk.time, 'time', return_value=now):
            self.ledger.begin_review(self.rid, self.task, self.role)
        with patch.object(bk.time, 'time', return_value=now + 100):
            running = self.ledger.begin_review(self.rid, self.task, self.role)
        self.assertEqual(running['deadline'], now + self.yazar['sure_sn'] - 80)
        now += 100
        record['request_clock'].update(aktif_sn=180, oturum=None)
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        with patch.object(bk.time, 'time', return_value=now):
            self.ledger.pause_review(self.rid, self.task, self.role)
        now += 3600
        with patch.object(bk.time, 'time', return_value=now):
            resumed = self.ledger.begin_review(self.rid, self.task, self.role)
        self.assertEqual(resumed['deadline'], now + self.yazar['sure_sn'] - 180)
        self.assertEqual((resumed['calls'], resumed['tools']), (1, 1))
        self.assertEqual((self.control()[0], self.control()[4]), (original[0], original[4]))
        expected = self.control()
        now += self.yazar['sure_sn'] - 180

        for work in (self.yazar['sure_sn'], 40 * 60):
            with self.subTest(work=work):
                record['request_clock']['aktif_sn'] = work
                atomic_json(self.store.path(self.rid) / 'run.json', record)
                with patch.object(bk.time, 'time', return_value=now), self.assertRaisesRegex(bk.BudgetDenied, 'time'):
                    self.ledger.begin_review(self.rid, self.task, self.role)
                self.assertEqual(self.control(), expected)

    def test_child_review_is_clipped_by_root_work_limit_and_unstarted_clock_keeps_wall_expiry(self):
        started = time.time()
        record = self.store.read(self.rid)
        record.update(request={'duration_minutes': 1}, request_clock={
            'baslangic': started, 'aktif_sn': 40, 'oturum': None})
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        child = self.store.accept('lab', 'TUR: inceleme\nChild review')['run_id']
        self.store.update(child, proje_ust=self.rid)
        with patch.object(bk.time, 'time', return_value=started):
            self.ledger.begin_review(child, self.task, self.role)
        now = started + 1800
        record['request_clock']['baslangic'] = None
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        with patch.object(bk.time, 'time', return_value=now), self.assertRaisesRegex(bk.BudgetDenied, 'time'):
            self.ledger.begin_review(child, self.task, self.role)
        record['request_clock']['baslangic'] = started
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        with patch.object(bk.time, 'time', return_value=now):
            context = self.ledger.begin_review(child, self.task, self.role)
        self.assertEqual(context['deadline'], now + 20)

    def test_deadline_is_shared_by_repairs_and_resumes_and_all_entry_points_refuse_after_expiry(self):
        started = time.time()
        with patch.object(bk.time, 'time', return_value=started):
            deadline = self.ledger.begin_review(self.rid, self.task, self.role)['deadline']
        with patch.object(bk.time, 'time', return_value=started + 100):
            self.assertEqual(self.ledger.begin_review(self.rid, 'review-author-000000000002', self.role)['deadline'], deadline)
        with patch.object(bk.time, 'time', return_value=deadline):
            now = datetime.fromtimestamp(deadline - 1, tz=timezone.utc).astimezone()
            self.budget.update(ts=now.isoformat(), bugun=now.date().isoformat())
            for operation in (lambda: self.ledger.begin_review(self.rid, self.task, self.role),
                              lambda: self.reserve(), lambda: self.ledger.review_tool(self.rid, self.task, self.role),
                              lambda: self.ledger.available('openrouter', self.budget, 7_500_000, self.rid, 'seritler',
                                                            rol=self.role, salt_okur=True)):
                with patch.dict(os.environ, {'CADRE_GOREV_ID': self.task}), self.assertRaisesRegex(bk.BudgetDenied, 'time'):
                    operation()
        self.assertEqual(self.control()[2:4], (0, 0))

    def test_finished_phase_pause_excludes_other_roles_and_resume_is_atomic_without_resetting_limits(self):
        started = time.time()
        with patch.object(bk.time, 'time', return_value=started):
            self.ledger.begin_review(self.rid, self.task, self.role)
            ident = self.reserve()
            self.ledger.settle(ident, 0, ident)
            self.ledger.review_tool(self.rid, self.task, self.role)
        with patch.object(bk.time, 'time', return_value=started + 100):
            self.ledger.pause_review(self.rid, self.task, self.role)
        with patch.object(bk.time, 'time', return_value=started + 1000):
            restarted = bk.BudgetLedger(self.ledger.path)
            with ThreadPoolExecutor(max_workers=4) as pool:
                deadlines = list(pool.map(lambda _: restarted.begin_review(
                    self.rid, 'review-author-000000000002', self.role)['deadline'], range(4)))
            self.assertEqual(deadlines, [started + 900 + self.yazar['sure_sn']] * 4)
        self.assertEqual(self.control()[2:4], (1, 1))
        with patch.object(bk.time, 'time', return_value=started + 900 + self.yazar['sure_sn']), self.assertRaisesRegex(bk.BudgetDenied, 'time'):
            restarted.begin_review(self.rid, self.task, self.role)

    def test_phase_pause_is_idempotent_and_cannot_reclaim_expired_or_unverified_time(self):
        started = time.time()
        with patch.object(bk.time, 'time', return_value=started):
            self.ledger.begin_review(self.rid, self.task, self.role)
        with patch.object(bk.time, 'time', return_value=started + 100):
            self.ledger.pause_review(self.rid, self.task, self.role)
        with patch.object(bk.time, 'time', return_value=started + 200):
            self.ledger.pause_review(self.rid, self.task, self.role)
        with patch.object(bk.time, 'time', return_value=started + 1000):
            self.assertEqual(self.ledger.begin_review(self.rid, self.task, self.role)['deadline'], started + 900 + self.yazar['sure_sn'])
        with patch.object(bk.time, 'time', return_value=started + 900 + self.yazar['sure_sn']), self.assertRaisesRegex(bk.BudgetDenied, 'time'):
            self.ledger.pause_review(self.rid, self.task, self.role)

    def test_paused_clock_readonly_query_is_pure_and_corrupt_clock_fails_closed(self):
        started = time.time()
        with patch.object(bk.time, 'time', return_value=started):
            self.ledger.begin_review(self.rid, self.task, self.role)
        with patch.object(bk.time, 'time', return_value=started + 100):
            self.ledger.pause_review(self.rid, self.task, self.role)
        with patch.object(bk.time, 'time', return_value=started + 1000), closing(sqlite3.connect(self.ledger.path)) as con:
            before = self.ledger.path.read_bytes()
            self.assertEqual(self.ledger.review_context(con, self.rid, self.task, self.role)['deadline'], started + 900 + self.yazar['sure_sn'])
            self.assertEqual(self.ledger.path.read_bytes(), before)
        with self.ledger.transaction() as con:
            con.execute('update review_pause set paused_total=-1')
        with self.assertRaisesRegex(bk.BudgetDenied, 'changed or unreadable'):
            self.ledger.begin_review(self.rid, self.task, self.role)

    def test_source_reading_review_phases_do_not_launch_blind_when_tools_are_exhausted(self):
        for gorev, rol, beklenen in (('plan-review-1-abcdef01', 'review', 'plan-review'), ('review-final-abcdef012345', 'kalite', 'review-final'),
                                     ('review-recheck-abcdef012345', 'kalite', 'review-recheck')):
            for _ in range(sinirlar.inceleme_politikasi()[beklenen]['arac']):
                self.ledger.review_tool(self.rid, gorev, rol)
            with patch.dict(os.environ, {'CADRE_RUN_ID': self.rid, 'CADRE_GOREV_ID': gorev}), patch.object(core, 'QE', self.home), \
                    patch.object(core, 'run_opencode') as launch:
                with self.assertRaisesRegex(bk.BudgetDenied, beklenen + ': tools'):
                    core.capture_opencode('fixture', self.home, ['-m', 'opencode/model', 'review'], 1800, role=rol)
                launch.assert_not_called()
        for _ in range(self.yazar['arac']):
            self.ledger.review_tool(self.rid, self.task, self.role)
        with patch.dict(os.environ, {'CADRE_RUN_ID': self.rid, 'CADRE_GOREV_ID': self.task}), patch.object(core, 'QE', self.home), \
                patch.object(core, 'run_opencode', side_effect=RuntimeError('yazar baslatildi')) as launch:
            with self.assertRaisesRegex(RuntimeError, 'yazar baslatildi'):
                core.capture_opencode('fixture', self.home, ['-m', 'opencode/model', 'report'], 1800, role=self.role)
        self.assertTrue(launch.called, 'rapor yazari arac hakki bitse de eldeki kanitla son cevabi verebilir')

    def test_engine_wait_uses_remaining_durable_time_and_expired_restart_launches_nothing(self):
        deadline = self.ledger.begin_review(self.rid, self.task, self.role)['deadline']
        env = {'CADRE_RUN_ID': self.rid, 'CADRE_GOREV_ID': self.task}
        with patch.dict(os.environ, env), patch.object(core, 'QE', self.home), \
                patch.object(core.time, 'time', return_value=deadline - 7) as clock, \
                patch.object(core, 'run_opencode') as launch, \
                patch.object(core, 'wait_process', return_value=0) as wait:
            def slow_launch(*args, **kwargs):
                clock.return_value = deadline - 2
                return SimpleNamespace(args=[])
            launch.side_effect = slow_launch
            core.capture_opencode('fixture', self.home, ['-m', 'opencode/model', 'PLAN ISTEK UYUMU\n{"criteria":[]}'], 1800, role=self.role)
            self.assertEqual(wait.call_args.args[1], 2, 'executor startup consumes the same durable deadline')
            self.assertEqual(float(launch.call_args.args[4]['CADRE_SURE_SON_TS']), deadline)
            self.assertIn(f"kalan {self.yazar['istek']} model istegi, {self.yazar['arac']} arac cagrisi", launch.call_args.args[2][-1])
            self.assertTrue(launch.call_args.args[2][-1].startswith('PLAN ISTEK UYUMU'))
            self.assertEqual(json.loads(launch.call_args.args[2][-1].splitlines()[-1]), {'criteria': []})
        with patch.dict(os.environ, env), patch.object(core, 'QE', self.home), \
                patch.object(core.time, 'time', return_value=deadline), patch.object(core, 'run_opencode') as launch:
            with self.assertRaisesRegex(bk.BudgetDenied, 'time'):
                core.capture_opencode('fixture', self.home, ['-m', 'opencode/model', 'review'], 1800, role=self.role)
            launch.assert_not_called()

    def test_schema_upgrade_and_corrupt_control_fail_closed_without_resetting_counters(self):
        ident = self.reserve()
        self.ledger.settle(ident, 0, ident)
        with self.ledger.transaction() as con:
            con.execute("update review_control set policy='{}'")
        with self.assertRaisesRegex(bk.BudgetDenied, 'changed or unreadable'):
            self.reserve()
        self.assertEqual(self.control()[2], 1)
        # If the old ledger has no table, the count of past requests is kept.
        with closing(sqlite3.connect(self.ledger.path)) as con:
            con.execute('drop table review_control')
        self.ledger.begin_review(self.rid, self.task, self.role)
        self.assertEqual(self.control()[2], 1)

    def test_transaction_interruption_rolls_back_counter_and_reservation_together(self):
        self.ledger.begin_review(self.rid, self.task, self.role)
        with self.assertRaises(KeyboardInterrupt), self.ledger.transaction() as con:
            self.ledger.reserve('openrouter', 10_000, self.budget,
                                {'run_id': self.rid, 'task_id': self.task, 'phase_role': self.role},
                                7_500_000, 'seritler', _connection=con)
            raise KeyboardInterrupt()
        self.assertEqual(self.control()[2], 0)
        with self.ledger.transaction() as con:
            self.assertEqual(bk.run_usage(con, self.rid), (0, 0))

    def test_unrelated_tasks_and_spoofed_phase_metadata_cannot_consume_or_reset_review_limits(self):
        self.ledger.reserve('openrouter', 10_000, self.budget, {'run_id': self.rid, 'task_id': 'g1',
                            'phase_role': 'impl', 'review_phase': 'review-author'}, 7_500_000, 'seritler')
        self.assertIsNone(self.control())
        with self.ledger.transaction() as con:
            meta = json.loads(con.execute('select metadata from reservation').fetchone()[0])
            self.assertNotIn('review_phase', meta)
        self.assertIsNone(self.ledger.begin_review(self.rid, self.task, 'qa'))
        self.assertIsNone(self.ledger.begin_review(None, self.task, self.role))
        self.assertIsNone(self.control())

    def test_preview_before_first_request_has_no_filesystem_side_effects(self):
        self.assertFalse(self.ledger.path.exists())
        with patch.dict(os.environ, {'CADRE_GOREV_ID': self.task, 'CADRE_ROL': self.role}):
            pay = 10_000_000 * self.yazar['pay_yuzde'] // 100
            value = self.ledger.available('openrouter', self.budget, 7_500_000, self.rid,
                                          'seritler', pay + 1, self.role, salt_okur=True)
        self.assertEqual(value, pay)
        self.assertFalse(self.ledger.path.exists())

    def test_review_job_pockets_fit_judge_reservation_refuse_overrun_and_old_snapshot_keeps_old_limits(self):
        with patch.dict(os.environ, {'CADRE_KOSU_TAVAN_USD': '1.5'}):
            rid = self.store.accept('lab', 'TUR: inceleme\nInspect the selected public source')['run_id']
        self.assertEqual([bk.rol_butcesi(self.home, rid, rol, 'openrouter') for rol in ('kalite', 'review', 'impl')],
                         [(750_000, 'hakem'), (150_000, 'yardimci'), (600_000, 'seritler')])

        def judge(value, n, run=rid):
            cap, dilim = bk.rol_butcesi(self.home, run, 'kalite', 'openrouter')
            return self.ledger.reserve('openrouter', value, self.budget, {'run_id': run, 'task_id': f'review-final-{n:012x}',
                                       'phase_role': 'kalite'}, cap, dilim)
        with patch.object(self.ledger, 'kalibre', return_value=1):
            for n, (reserved, actual) in enumerate(((258_000, 13_000), (263_000, 13_000), (285_000, 15_000))):
                ident = judge(reserved, n)
                self.ledger.settle(ident, actual, ident)
            with self.assertRaisesRegex(bk.BudgetDenied, 'review-final: cost'):
                judge(450_000 - 41_000 + 1, 3)
            judge(450_000 - 41_000, 4)
            with patch.dict(os.environ, {'CADRE_KOSU_TAVAN_USD': '1.5'}):
                eski = self.store.accept('lab', 'TUR: inceleme\nInspect the selected public source')['run_id']
            record = self.store.read(eski)
            v2 = {'surum': 'sinirli-inceleme-v2', 'plan-review': {'istek': 7, 'arac': 12, 'sure_sn': 300, 'pay_yuzde': 25},
                  'review-author': {'istek': 9, 'arac': 16, 'sure_sn': 480, 'pay_yuzde': 50},
                  'review-final': {'istek': 6, 'arac': 10, 'sure_sn': 510, 'pay_yuzde': 10},
                  'review-recheck': {'istek': 6, 'arac': 10, 'sure_sn': 510, 'pay_yuzde': 10}}
            atomic_json(self.store.path(eski) / 'run.json', {**record, 'review_limits': v2})
            self.assertEqual(bk.rol_butcesi(self.home, eski, 'kalite', 'openrouter'), (225_000, 'hakem'))
            with self.assertRaisesRegex(bk.BudgetDenied, 'review-final: cost'):
                judge(178_760, 0, eski)

    def test_invalid_recorded_policy_never_falls_back_to_a_larger_allowance(self):
        record = self.store.read(self.rid)
        for value in (None, {}, {'surum': 'bad', 'review-author': {'istek': True}}):
            atomic_json(self.store.path(self.rid) / 'run.json', {**record, 'review_limits': value})
            with self.assertRaisesRegex(bk.BudgetDenied, 'limits unavailable'):
                self.reserve()


if __name__ == '__main__':
    unittest.main()
