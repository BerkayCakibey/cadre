from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import budget_gate as bg
import acceptance
from run_store import FileLock, RunStore, atomic_json
import level
import qe_core as core
from test_budget_period import snapshot


class OperatorBudgetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='operator-budget-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        env = patch.dict(os.environ, {'CADRE_HOME': str(self.home), 'CADRE_RUN_CAP_USD': '1',
                                     'CADRE_SPEND_ALARM': '0', 'CADRE_TASK_ID': '', 'CADRE_NO_NET': '1'})
        env.start()
        self.addCleanup(env.stop)
        self.store = RunStore(self.home)
        self.rid = self.store.accept('work', 'selected work')['run_id']
        self.ledger = bg.BudgetLedger(self.home / 'chain/request-budget.db')
        self.budget = snapshot(50)
        self.budget.update(zen={'db': 'ok', 'pay': 50, 'available': 50},
                           **{'or': {'db': 'ok', 'pay': 50, 'available': 50}})

    def reserve(self, rid, value, provider='openrouter', role='lanes'):
        cap = bg.role_cap(self.home, rid, slice_name=role) if role in ('lanes', 'judge', 'helper') else 10_000_000
        return self.ledger.reserve(provider, value, self.budget, {'run_id': rid, 'phase_role': role},
                                   run_cap=cap, slice=role)

    def test_every_work_kind_and_nested_control_share_the_initial_total_without_project_mode(self):
        for prompt in ('implement selected fix', 'ROUND: report\nWrite report.md', 'ROUND: review\nInspect existing source'):
            root = self.store.accept('work', prompt)['run_id']
            child = self.store.accept('work', 'child', parent_run_id=root)['run_id']
            retry = self.store.accept('work', 'review retry', parent_run_id=child)['run_id']
            self.assertNotIn('project_budget_cap_usd', self.store.read(root))
            self.reserve(root, 400_000, role='plan')
            self.reserve(child, 400_000, 'opencode')
            self.reserve(retry, 200_000, role='quality')
            with self.assertRaisesRegex(bg.BudgetDenied, 'project total cap'):
                self.reserve(root, 1, role='plan-review')
            pocket = bg.role_cap(self.home, child, slice_name='lanes')
            self.assertEqual(pocket, 400_000 if prompt.startswith('ROUND: review') else 700_000)
            if pocket < 700_000:
                with self.assertRaisesRegex(bg.BudgetDenied, 'run budget exhausted'):
                    self.reserve(child, 700_000, 'opencode-go')
            self.reserve(child, min(700_000, pocket), 'opencode-go')
            self.assertEqual(self.store.read(root)['operator_budget_usd'], 1)

    def test_estimate_difficulty_guarantee_and_qa_progress_cannot_increase_operator_authority(self):
        self.store.update(self.rid, budget_manual=False)
        (self.store.path(self.rid) / 'plan.json').write_text(json.dumps({'scope_points': 100,
            'tasks': [{'estimate_usd': 1000, 'files': ['a.py']}]}))
        with patch.dict(os.environ, {'CADRE_RUN_CAP_USD': ''}):
            level.apply(self.rid, 'work')
        bg.progress_write(self.home, self.rid, 'qa-pass', 'product-a')
        self.assertFalse(bg.cap_extend(self.home, self.rid, 'budget_cap_usd'))
        record = self.store.read(self.rid)
        self.assertEqual(record['budget_plan_cap_usd'], 1500)
        self.assertEqual(record['budget_cap_usd'], 1)
        log = self.home / 'guarantee.log'
        acceptance._cap_assurance_escalate(self.store, self.rid, record, 2_000_000, log)
        self.assertEqual(self.store.read(self.rid)['budget_cap_usd'], 1)

    def test_calibrated_unknown_bounds_and_concurrent_children_remain_charged_after_restart(self):
        children = [self.store.accept('work', 'child', parent_run_id=self.rid)['run_id'] for _ in range(2)]
        def attempt(rid):
            try:
                return self.reserve(rid, 700_000)
            except bg.BudgetDenied:
                return None
        with patch.object(self.ledger, 'calibrated', return_value=70_000), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, children))
        self.assertEqual(sum(x is not None for x in results), 1)
        self.ledger = bg.BudgetLedger(self.ledger.path)
        with self.assertRaisesRegex(bg.BudgetDenied, 'project total cap'):
            self.reserve(self.rid, 300_001, role='qa')
        self.reserve(self.rid, 300_000, role='qa')
        with self.assertRaises(bg.BudgetDenied):
            self.reserve(children[0], 1, role='qa')

    def test_progress_transfers_only_known_unused_pay_and_preserves_final_acceptance_under_race(self):
        self.store.update(self.rid, budget_need_micro={'helper': 0, 'judge': 200_000})
        first = self.reserve(self.rid, 700_000)
        self.ledger.settle(first, 700_000, 'implemented-part')
        bg.progress_write(self.home, self.rid, 'qa-pass', 'verified-part')
        def attempt(_):
            try:
                return self.reserve(self.rid, 60_000)
            except bg.BudgetDenied:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(sum(x is not None for x in results), 1)
        with self.ledger.transaction() as con:
            caps = json.loads(con.execute('select caps from role_allocation').fetchone()[0])
            self.assertEqual(sum(caps.values()), 1_000_000)
            self.assertEqual(caps['judge'], 200_000)
            self.assertEqual(con.execute('select count(*) from role_transfer').fetchone()[0], 1)
        self.ledger = bg.BudgetLedger(self.ledger.path)
        self.reserve(self.rid, 200_000, role='judge')
        with self.assertRaises(bg.BudgetDenied):
            self.reserve(self.rid, 40_001)
        self.reserve(self.rid, 40_000)

    def test_missing_or_stale_progress_and_unknown_or_reserved_donors_do_not_transfer(self):
        for mode in ('unknown', 'stale', 'reserved', 'needed'):
            rid = self.store.accept('work', 'isolated boundary')['run_id']
            first = self.reserve(rid, 700_000)
            self.ledger.settle(first, 700_000, first)
            bg.progress_write(self.home, rid, 'qa-pass', rid)
            if mode == 'stale':
                self.store.update(rid, budget_progress={'ts': 0, 'reason': 'qa-pass', 'id': rid})
            if mode != 'unknown':
                self.store.update(rid, budget_need_micro={'helper': 100_000 if mode == 'needed' else 0, 'judge': 200_000})
            if mode == 'reserved':
                self.reserve(rid, 100_000, role='helper')
            with self.assertRaises(bg.BudgetDenied, msg=mode):
                self.reserve(rid, 1)
        with self.ledger.transaction() as con:
            self.assertEqual(con.execute('select count(*) from role_transfer').fetchone()[0], 0)

    def test_reviewed_current_plan_releases_finished_planning_pay_without_a_qa_claim(self):
        from test_request_pipeline import RequestPipelineTests
        from run_store import generate_request_plan, review_plan
        fixture = RequestPipelineTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.approve()
        generate_request_plan(fixture.store, fixture.rid, str(fixture.fake))
        self.assertEqual(review_plan(fixture.store, fixture.rid, 1, str(fixture.fake))['status'], 'PASS')
        fixture.store.update(fixture.rid, budget_need_micro={'lanes': 100_000})
        self.assertNotIn('budget_progress', fixture.store.read(fixture.rid))
        ledger = bg.BudgetLedger(fixture.home / 'chain/request-budget.db')
        cap, role = bg.role_budget(fixture.home, fixture.rid, 'impl', 'opencode-go')
        reservation = ledger.reserve('opencode-go', cap, self.budget, {'run_id': fixture.rid}, cap, role)
        ledger.settle(reservation, cap, reservation)
        ledger.reserve('opencode-go', 10_000, self.budget, {'run_id': fixture.rid}, cap, role)
        with ledger.transaction() as con:
            metadata = json.loads(con.execute('select metadata from role_transfer').fetchone()[0])
            self.assertEqual(metadata['reason'], 'reviewed-plan')
            self.assertEqual(metadata['evidence'], {'revision': 1})
            self.assertEqual(sum(metadata['before'].values()), sum(metadata['after'].values()))

    def test_allocation_is_visible_before_any_paid_call_and_reading_does_not_reserve(self):
        record = self.store.read(self.rid)
        plan = record['budget_allocation']
        self.assertEqual((plan['amount_usd'], plan['currency'], plan['scope']), (1, 'USD', 'operator-total'))
        self.assertEqual({s['role'] for s in plan['stages']}, {'plan', 'plan-review', 'impl', 'qa', 'quality', 'repair'})
        self.assertTrue(all(s['uncertainty'] == 'unmeasured' and s['estimate_micro'] is None for s in plan['stages']))
        self.assertFalse(self.ledger.path.exists())

    def pending_review(self, estimate=0.5):
        with patch.dict(os.environ, {'CADRE_RUN_CAP_USD': '2.1'}):
            self.rid = self.store.accept('work', 'bounded review')['run_id']
        self.store.update(self.rid, state='running')
        plan = {'job_id': 'p1', 'tasks': [{'id': 'g1', 'definition': 'Fix the selected defect',
                'acceptance': 'Existing tests pass', 'files': ['README.md'], 'model': 'auto',
                'difficulty': 1, 'parallel_group': 0, 'estimate_min': 1, 'estimate_usd': estimate, 'dependent': []}]}
        self.store.save_plan(self.rid, plan)
        task = 'plan-review-1-abcdef01'
        self.review_claim({'id': 'owned-review', 'revision': 1, 'task_id': task})
        return task

    def review_claim(self, claim):
        with FileLock(self.store.path(self.rid) / '.record.lock'):
            record = self.store.read(self.rid)
            atomic_json(self.store.path(self.rid) / 'run.json', {**record, 'plan_review_claim': claim})

    def test_pending_plan_review_borrows_only_its_shortfall_inside_the_same_total_and_preview_is_read_only(self):
        task = self.pending_review()
        with patch.dict(os.environ, {'CADRE_TASK_ID': task, 'CADRE_ROLE': 'review'}):
            first = self.reserve(self.rid, 200_200, role='helper')
            self.ledger.settle(first, 4_000, 'first-review-step')
            before = self.ledger.path.read_bytes()
            available = self.ledger.available('openrouter', self.budget, 210_000, self.rid,
                                               'helper', 206_900, 'review', read_only=True)
            self.assertGreaterEqual(available, 206_900)
            self.assertEqual(self.ledger.path.read_bytes(), before)
            second = self.reserve(self.rid, 206_900, role='helper')
        with self.ledger.transaction() as con:
            caps = json.loads(con.execute('select caps from role_allocation').fetchone()[0])
            transfer = json.loads(con.execute('select metadata from role_transfer').fetchone()[0])
            self.assertEqual(sum(caps.values()), 2_100_000)
            self.assertEqual(caps['helper'], 210_900)
            self.assertEqual(caps['judge'], 315_000)
            self.assertEqual(transfer['reason'], 'owned-plan-review')
            self.assertEqual(bg.run_usage(con, self.rid, 'helper'), (4_000, 206_900))
        self.assertEqual(self.store.read(self.rid)['operator_budget_usd'], 2.1)
        self.assertTrue(second)

    def test_pending_review_cannot_take_unknown_main_work_or_final_acceptance_or_survive_a_stale_claim(self):
        for mode in ('unknown', 'needed', 'wrong-task', 'stale', 'cancelled', 'terminal', 'changed-plan', 'stop-requested', 'protected-work-ceiling'):
            task = self.pending_review(0 if mode == 'unknown' else 1.575 if mode == 'needed' else 0.5)
            if mode == 'stale':
                self.review_claim({'id': 'stale', 'revision': 0, 'task_id': task})
            if mode == 'cancelled':
                (self.store.path(self.rid) / 'cancel.flag').touch()
            if mode == 'terminal':
                self.store.update(self.rid, state='completed')
            if mode == 'changed-plan':
                (self.store.path(self.rid) / 'plan.json').write_text('{}')
            if mode == 'stop-requested':
                with FileLock(self.store.path(self.rid) / '.record.lock'):
                    record = self.store.read(self.rid)
                    atomic_json(self.store.path(self.rid) / 'run.json', {**record, 'plan_changes': [{'status': 'stop_requested'}]})
            with patch.dict(os.environ, {'CADRE_TASK_ID': 'g1' if mode == 'wrong-task' else task, 'CADRE_ROLE': 'review'}), \
                    self.assertRaises(bg.BudgetDenied, msg=mode):
                self.reserve(self.rid, 1_285_001 if mode == 'protected-work-ceiling' else 211_000, role='helper')
        with self.ledger.transaction() as con:
            self.assertEqual(con.execute('select count(*) from role_transfer').fetchone()[0], 0)

    def test_pending_review_concurrent_shortfalls_preserve_one_allocation_and_bounded_holds_after_restart(self):
        task = self.pending_review(estimate=1.2)
        def attempt(_):
            try:
                return self.reserve(self.rid, 300_000, role='helper')
            except bg.BudgetDenied:
                return None
        with patch.dict(os.environ, {'CADRE_TASK_ID': task, 'CADRE_ROLE': 'review'}), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.ledger = bg.BudgetLedger(self.ledger.path)
        with self.ledger.transaction() as con:
            caps = json.loads(con.execute('select caps from role_allocation').fetchone()[0])
            self.assertEqual(sum(caps.values()), 2_100_000)
            self.assertEqual(caps['helper'], 300_000)
            self.assertEqual(caps['judge'], 315_000)
            self.assertGreaterEqual(caps['lanes'], 1_200_000)
            self.assertEqual(bg.run_usage(con, self.rid, 'helper'), (0, 300_000))

    def test_first_review_step_previews_transfer_without_creating_a_ledger(self):
        task = self.pending_review()
        self.assertFalse(self.ledger.path.exists())
        with patch.dict(os.environ, {'CADRE_TASK_ID': task, 'CADRE_ROLE': 'review'}):
            value = self.ledger.available('openrouter', self.budget, 210_000, self.rid,
                                          'helper', 300_000, 'review', read_only=True)
        self.assertEqual(value, 300_000)
        self.assertFalse(self.ledger.path.exists())

    def test_controls_choose_an_affordable_candidate_and_never_starve_main_work_or_raise_the_total(self):
        self.store.update(self.rid, budget_need_micro={'lanes': 600_000})
        chains = lambda role, *args, **kwargs: (['opencode/expensive', 'opencode/cheap'] if role == 'quality' else ['opencode/writer'], [])
        cost = lambda role, model: 250_000 if model.endswith('/expensive') else 100_000
        with patch.object(core, 'QE', self.home), patch.object(core, 'budget_json', return_value=self.budget), \
             patch.object(core.ROUTER, 'chain', side_effect=chains), patch.object(acceptance, '_assurance_amount', side_effect=cost), \
             patch.dict(os.environ, {'CADRE_RUN_ID': self.rid}):
            selected = acceptance.acceptance_assurance(self.home / 'guarantee.log')
            self.assertEqual(selected['models']['quality'], 'opencode/cheap')
            self.assertEqual(len(selected['reservations']), 3)
            self.assertEqual(acceptance.acceptance_assurance(self.home / 'guarantee.log'), selected)
            self.reserve(self.rid, 600_000, 'opencode')
            self.assertEqual(self.store.read(self.rid)['budget_cap_usd'], 1)
            other = self.store.accept('work', 'preserve work on insufficient budget')['run_id']
            self.store.update(other, budget_need_micro={'lanes': 950_000})
            with patch.dict(os.environ, {'CADRE_RUN_ID': other}), self.assertRaises(bg.BudgetDenied):
                acceptance.acceptance_assurance(self.home / 'guarantee.log')
            self.assertEqual(self.store.read(other)['budget_cap_usd'], 1)
            with self.ledger.transaction() as con:
                self.assertEqual(bg.run_usage(con, other), (0, 0))

    def test_cash_and_subscription_guarantees_protect_only_their_own_pool(self):
        self.store.update(self.rid, budget_need_micro={'lanes': 100_000})
        chains = lambda role, *args, **kwargs: (['opencode/checker'] if role == 'quality' else ['opencode-go/writer'], [])
        cost = lambda role, model: 300_000 if role == 'quality' else 100_000
        with patch.object(core, 'QE', self.home), patch.object(core, 'budget_json', return_value=self.budget), \
             patch.object(core.ROUTER, 'chain', side_effect=chains), patch.object(acceptance, '_assurance_amount', side_effect=cost), \
             patch.dict(os.environ, {'CADRE_RUN_ID': self.rid}):
            acceptance.acceptance_assurance(self.home / 'guarantee.log')
        self.assertEqual(self.store.read(self.rid)['acceptance_assurance_bases_micro'], {'cash': 600_000, 'opencode-go': 100_000})
        self.ledger.reserve('opencode-go', 700_000, self.budget, {'run_id': self.rid},
                            *bg.role_budget(self.home, self.rid, 'impl', 'opencode-go'))
        self.ledger.reserve('openrouter', 300_000, self.budget, {'run_id': self.rid},
                            *bg.role_budget(self.home, self.rid, 'impl', 'openrouter'))
        with self.assertRaises(bg.BudgetDenied):
            self.reserve(self.rid, 1)

    def test_early_stage_estimates_use_permitted_candidates_with_separate_units_without_calls_or_reservations(self):
        chains = lambda role, *args, **kwargs: (['opencode-go/writer'] if role == 'impl' else ['opencode/checker'], [])
        with patch.object(core, 'QE', self.home), patch.object(core.ROUTER, 'chain', side_effect=chains), \
             patch.object(acceptance, '_assurance_amount', return_value=100_000):
            acceptance.budget_estimated_save(self.store, self.rid, self.budget)
            before = (self.store.path(self.rid) / 'run.json').read_bytes()
            acceptance.budget_estimated_save(self.store, self.rid, self.budget)
            self.assertEqual(before, (self.store.path(self.rid) / 'run.json').read_bytes())
        stages = self.store.read(self.rid)['budget_allocation']['stages']
        self.assertEqual(len(stages), 6)
        self.assertTrue(all(s['estimate_micro'] == 100_000 and s['estimate_scope'] == 'one-request-upper-bound' for s in stages))
        self.assertEqual({s['provider'] for s in stages}, {'opencode', 'opencode-go'})
        self.assertFalse(self.ledger.path.exists())

    def test_plan_cost_prechecks_do_not_add_cash_and_subscription_units_or_reopen_exhausted_pools(self):
        from run_store import stop_cost, budget_cost_exhausted
        cash = self.reserve(self.rid, 600_000, role='plan')
        go = self.reserve(self.rid, 600_000, 'opencode-go')
        self.ledger.settle(cash, 600_000, cash)
        record = self.store.read(self.rid)
        cost = stop_cost(self.store, [self.rid])
        self.assertEqual((cost['settled_micro'], cost['held_micro']), (600_000, 0))
        self.assertEqual(cost['pools']['opencode-go'], [0, 600_000])
        self.assertFalse(budget_cost_exhausted(record, cost))
        self.ledger.settle(go, 600_000, go)
        self.reserve(self.rid, 400_000, role='plan-review')
        cost = stop_cost(self.store, [self.rid])
        self.assertTrue(budget_cost_exhausted({**record, 'request_budget': {'providers': ['openrouter']}}, cost))
        self.assertFalse(budget_cost_exhausted({**record, 'request_budget': {'providers': ['opencode-go']}}, cost))

    def test_read_only_admission_matches_the_fixed_total_and_never_creates_ledger_or_transfer_records(self):
        child = self.store.accept('work', 'child', parent_run_id=self.rid)['run_id']
        self.assertEqual(self.ledger.available('openrouter', self.budget, 10_000_000, child, read_only=True), 1_000_000)
        self.assertFalse(self.ledger.path.exists())
        self.reserve(self.rid, 700_000)
        self.reserve(child, 300_000, role='qa')
        before = self.ledger.path.read_bytes()
        self.assertEqual(self.ledger.available('openrouter', self.budget, 10_000_000, child, read_only=True), 0)
        self.assertEqual(self.ledger.path.read_bytes(), before)

    def test_interrupted_and_failed_role_transactions_preserve_pay_and_restart_transfers_once(self):
        import subprocess
        self.store.update(self.rid, budget_need_micro={'helper': 0, 'judge': 200_000})
        first = self.reserve(self.rid, 700_000)
        self.ledger.settle(first, 700_000, 'first-part')
        bg.progress_write(self.home, self.rid, 'qa-pass', 'part-proof')
        with self.assertRaises(OSError):
            with self.ledger.transaction() as con:
                self.ledger.reserve('openrouter', 100_000, self.budget, {'run_id': self.rid},
                                    run_cap=700_000, slice='lanes', _connection=con)
                raise OSError('disk interruption')
        script = """
import json,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from budget_gate import BudgetLedger
home,rid=Path(sys.argv[2]),sys.argv[3]
ledger=BudgetLedger(home/'chain/request-budget.db')
with ledger.transaction() as con:
    ledger.reserve('openrouter',100000,json.loads(sys.argv[4]),{'run_id':rid},run_cap=700000,slice='lanes',_connection=con)
    print('reserved',flush=True)
    time.sleep(60)
"""
        with subprocess.Popen([sys.executable, '-c', script, str(Path(bg.__file__).parent), str(self.home), self.rid,
                               json.dumps(self.budget)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
            try:
                self.assertEqual(process.stdout.readline().strip(), 'reserved')
            finally:
                process.kill()
                process.communicate(timeout=10)
        with self.ledger.transaction() as con:
            self.assertEqual(con.execute('select count(*) from role_transfer').fetchone()[0], 0)
            self.assertEqual(con.execute('select count(*) from reservation').fetchone()[0], 1)
        self.ledger = bg.BudgetLedger(self.ledger.path)
        self.reserve(self.rid, 100_000)
        with self.ledger.transaction() as con:
            self.assertEqual(con.execute('select count(*) from role_transfer').fetchone()[0], 1)

    def test_panel_shows_initial_uncertainty_and_transferred_pay_in_english_at_both_sizes_without_writes(self):
        import subprocess
        python = os.environ.get('CADRE_TEST_PANEL_PYTHON')
        self.assertTrue(python and Path(python).is_file(), 'Textual environment required')
        self.store.update(self.rid, budget_need_micro={'helper': 0, 'judge': 200_000})
        first = self.reserve(self.rid, 700_000)
        self.ledger.settle(first, 700_000, 'first')
        bg.progress_write(self.home, self.rid, 'qa-pass', 'part-proof')
        self.reserve(self.rid, 60_000)
        self.store.update(self.rid, state='implemented')
        script = """
import asyncio,json,os,sys,xml.etree.ElementTree as ET
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import panel_app,panel_data,ui_messages
home,rid=Path(sys.argv[2]),sys.argv[3]
async def run():
    before={str(p):p.read_bytes() for p in home.rglob('*') if p.is_file()}
    language = 'en'
    panel_data.clean()
    for size in ((200,50),(100,30)):
        app=panel_app.Cadre(home=home)
        async with app.run_test(size=size) as pilot:
            for _ in range(100):
                if app.loaded: break
                await pilot.pause(.03)
            await app.workers.wait_for_complete()
            app.git(['tasks','history',rid])
            app.choice=next(i for i,n in enumerate(app.elements) if n['id']=='spend')
            app.draw(); await pilot.pause()
            detail=str(app.selected['detail'])
            assert ui_messages.t('Initial allocation estimate') in detail,detail
            assert ui_messages.t('Internal allocation transfer') in detail,detail
            assert ui_messages.t('Unmeasured stage costs remain unknown.') in detail,detail
            if os.environ.get('CADRE_OPERATOR_EVIDENCE'):
                folder=Path(os.environ['CADRE_OPERATOR_EVIDENCE']); folder.mkdir(exist_ok=True,parents=True)
                (folder/f'{language}-{size[0]}.svg').write_text(app.export_screenshot())
                for key,title in (('details','Initial allocation estimate'),('transfer','Internal allocation transfer')):
                    view=app.query_one('#detail'); view.scroll_home(animate=False); await pilot.pause()
                    for _ in range(100):
                        svg=app.export_screenshot()
                        visible=' '.join(''.join(ET.fromstring(svg).itertext()).split())
                        if ui_messages.t(title) in visible: break
                        previous=view.scroll_y
                        await pilot.press('shift+down'); await pilot.pause()
                        assert view.scroll_y>previous,(title,visible)
                    assert ui_messages.t(title) in visible,(title,visible)
                    (folder/f'{language}-{size[0]}-{key}.svg').write_text(svg)
    after={str(p):p.read_bytes() for p in home.rglob('*') if p.is_file()}
    assert before==after,sorted(p for p in before.keys()|after.keys() if before.get(p)!=after.get(p))
asyncio.run(run())
"""
        result = subprocess.run([python, '-c', script, str(Path(bg.__file__).parent), str(self.home), self.rid],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_completed_lanes_free_their_transferred_pay_for_acceptance_and_cannot_reopen(self):
        import contract
        self.store.update(self.rid, budget_need_micro={'helper': 0, 'judge': 200_000})
        first = self.reserve(self.rid, 700_000)
        self.ledger.settle(first, 700_000, first)
        bg.progress_write(self.home, self.rid, 'qa-pass', 'part')
        second = self.reserve(self.rid, 60_000)
        self.ledger.settle(second, 30_000, second)
        contract.create(self.home, self.rid, {'tasks': [{'id': 'g1'}]})
        self.store.update(self.rid, tasks={'g1': {'state': 'produced'}})
        self.assertEqual(bg.role_cap(self.home, self.rid, 'impl', provider='openrouter'), 730_000)
        self.assertEqual(bg.role_cap(self.home, self.rid, 'quality', provider='openrouter'), 230_000)
        with self.assertRaises(bg.BudgetDenied):
            self.reserve(self.rid, 1)
        self.reserve(self.rid, 230_000, role='judge')


if __name__ == '__main__':
    unittest.main()
