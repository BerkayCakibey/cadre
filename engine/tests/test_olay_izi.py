from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))

import arayuz_veri as ui
from butce_kapisi import BudgetLedger
from kosu import RunStore, atomic_json, generate_request_plan, review_plan


class TraceProducerTests(unittest.TestCase):
    def setUp(self):
        from test_request_pipeline import RequestPipelineTests
        self.f = RequestPipelineTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.home, self.store, self.rid = self.f.home, self.f.store, self.f.rid
        self.ledger = BudgetLedger(self.home / 'zincir/request-budget.db')

    def plan(self):
        self.f.approve()
        generate_request_plan(self.store, self.rid, str(self.f.fake))
        self.assertEqual(review_plan(self.store, self.rid, 1, str(self.f.fake))['status'], 'PASS')

    def revise(self):
        self.store.change_plan(self.rid, 'trace-change', 1, 'Preserve README and improve wording')
        self.store.stop_plan(self.rid, 'trace-change')
        generate_request_plan(self.store, self.rid, str(self.f.fake))
        self.assertEqual(review_plan(self.store, self.rid, 2, str(self.f.fake))['status'], 'PASS')
        self.store.resume_plan(self.rid, 'trace-change', 2)

    def reserve(self, attempt, **extra):
        from test_butce_donemi import snapshot
        return self.ledger.reserve('opencode-go', 1000, snapshot(5), {
            'run_id': self.rid, 'task_id': attempt['task_id'], 'attempt_id': attempt['attempt_id'],
            'model': attempt['model'], 'phase_role': attempt['role'], **extra})

    def attempt(self, revision):
        paths = self.store.path(self.rid).glob('tasks/*/attempts/*/attempt.json')
        return next(json.loads(p.read_text()) for p in paths if json.loads(p.read_text()).get('role') == 'review'
                    and json.loads(p.read_text()).get('plan_revision') == revision)

    def files(self):
        return {str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}

    def test_real_plan_stop_revision_review_resume_and_requests_have_immutable_correlations(self):
        self.plan()
        old = self.attempt(1)
        first = self.reserve(old)
        self.ledger.settle(first, 400, 'trace-receipt-1')
        self.revise()
        new = self.attempt(2)
        second = self.reserve(new)
        import yonlendirici as router
        with patch.object(router, 'QE', self.home), patch.object(router, 'KAYIT', self.home / 'zincir/yonlendirici.jsonl'):
            router.kayit(olay='zincir', run_id=self.rid, gorev_id='g1', neden=['approved model pool'], zincir=[new['model']])
        before = self.files()
        with patch('subprocess.run', side_effect=AssertionError('read-only trace cannot execute')):
            trace = ui.olay_izi(self.home, self.rid)
            self.assertEqual(trace, ui.olay_izi(self.home, self.rid))
            self.assertEqual(ui.iz_replay(json.dumps(trace)), trace)
        self.assertEqual(before, self.files())
        decisions = [e for e in trace['events'] if e['type'].startswith('plan_change:')]
        self.assertEqual([e['type'] for e in decisions], ['plan_change:' + a for a in ('requested', 'stopped', 'revision_saved', 'resumed')])
        self.assertEqual([e['revision'] for e in decisions], [1, 1, 2, 2])
        self.assertTrue(all(e['change_id'] == 'trace-change' and e['evidence_status'] == 'recorded' and e['locations'] for e in decisions))
        requests = {e['request_id']: e for e in trace['events'] if e['type'] == 'request_reserved'}
        self.assertEqual((requests[first]['revision'], requests[second]['revision']), (1, 2))
        self.assertEqual(requests[first]['attempt_id'], old['attempt_id'])
        self.assertEqual(requests[first]['request_hash'], self.store.read(self.rid)['request_input_hash'])
        self.assertEqual(sum(e['type'] == 'request_settled' for e in trace['events']), 1)
        self.assertEqual(next(e for e in trace['events'] if e['type'] == 'zincir')['revision'], 2)
        self.assertTrue(all(e['data'].get('acceptance') and e['data'].get('reasons') for e in trace['events'] if e['type'] == 'plan_review'))
        self.assertFalse(any(i['issue'].endswith('conflict') for i in trace['issues']), trace['issues'])

    def test_legacy_attempt_revision_is_unknown_and_explicit_conflicts_are_not_repaired_from_current_plan(self):
        self.plan()
        old = self.attempt(1)
        path = self.store.task_dir(self.rid, old['task_id']) / 'attempts' / old['attempt_id'] / 'attempt.json'
        legacy = {k: v for k, v in old.items() if k not in ('plan_revision', 'plan_digest', 'request_hash')}
        atomic_json(path, legacy)
        request = self.reserve(old)
        self.revise()
        new = self.attempt(2)
        self.reserve(new, plan_revision=999)
        trace = ui.olay_izi(self.home, self.rid)
        historical = next(e for e in trace['events'] if e['type'] == 'request_reserved' and e['request_id'] == request)
        self.assertIsNone(historical['revision'])
        self.assertEqual(historical['correlation_status'], 'historical-unknown')
        bad = next(e for e in trace['events'] if e['type'] == 'request_reserved' and e['revision'] == 999)
        self.assertEqual(bad['evidence_status'], 'conflict')
        self.assertIn('correlation-conflict', [i['issue'] for i in trace['issues']])

    def test_duplicate_out_of_order_unknown_and_forged_stop_events_never_mutate_or_claim_success(self):
        self.plan()
        self.store.change_plan(self.rid, 'trace-change', 1, 'Preserve README')
        record = self.store.read(self.rid)
        change = record['plan_changes'][0]
        requested = change['events'][0]
        change['events'] += [requested.copy(), {'action': 'stopped', 'ts': requested['ts'] - 1},
                             {'action': 'resumed', 'revision': 1, 'ts': requested['ts'] + 1},
                             {'action': 'future-event', 'ts': requested['ts'] + 2}]
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        before = self.files()
        with (patch.object(RunStore, 'stop_plan', side_effect=AssertionError('reader must not stop')),
              patch.object(RunStore, 'resume_plan', side_effect=AssertionError('reader must not resume'))):
            trace = ui.olay_izi(self.home, self.rid)
        events = [e for e in trace['events'] if e['type'].startswith('plan_change:')]
        request = next(e for e in events if e['type'] == 'plan_change:requested')
        self.assertEqual(len(request['locations']), 2)
        self.assertTrue(all(e['evidence_status'] == 'conflict' for e in events if e is not request))
        self.assertIn('future-event', ui.iz_metni(trace))
        self.assertEqual(before, self.files())
        self.assertEqual(self.store.read(self.rid)['plan_changes'][0]['status'], 'stop_requested')
        self.assertEqual(len(self.f.calls()), 2)

    def test_live_wal_receipts_are_read_without_source_writes_and_unrelated_requests_are_excluded(self):
        self.plan()
        self.reserve(self.attempt(1))
        with closing(sqlite3.connect(self.ledger.path)) as writer:
            self.assertEqual(writer.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute('BEGIN')
            writer.execute('select count(*) from reservation').fetchone()
            request = self.reserve(self.attempt(1))
            self.ledger.settle(request, 300, 'wal-receipt')
            with self.ledger.transaction() as con:
                con.execute('insert into reservation values(?,?,?,?,?,?,?,?)', ('unrelated', 'opencode-go', 'p', 1, 100, None, None, json.dumps({'run_id': 'unrelated', 'password': 'SYNTHETIC_OTHER'})))
                con.execute('insert into reservation values(?,?,?,?,?,?,?,?)', ('broken', 'opencode-go', 'p', 1, 100, None, None, '{broken'))
            before = self.files()
            trace = ui.olay_izi(self.home, self.rid)
            self.assertEqual(before, self.files())
            settled = next(e for e in trace['events'] if e['type'] == 'request_settled')
            self.assertEqual((settled['request_id'], settled['data']['actual_micro']), (request, 300))
            self.assertTrue(settled['timestamp'])
            self.assertTrue(any(s['path'].endswith('.db-wal') for s in trace['sources']))
            self.assertNotIn('SYNTHETIC_OTHER', json.dumps(trace))
            self.assertIn('malformed-ledger-metadata', [i['issue'] for i in trace['issues']])

    def test_missing_malformed_changing_and_symlinked_ledger_never_produce_false_zero_or_old_receipts(self):
        self.plan()
        # The review counter opens the ledger beforehand; set up the missing ledger entry explicitly.
        with closing(sqlite3.connect(self.ledger.path)) as con:
            self.assertEqual(con.execute('select count(*) from reservation').fetchone()[0], 0)
        self.ledger.path.unlink()
        trace = ui.olay_izi(self.home, self.rid)
        self.assertTrue(any(i['source'].endswith('request-budget.db') and i['issue'] == 'missing' for i in trace['issues']))
        self.ledger.path.parent.mkdir(exist_ok=True)
        self.ledger.path.write_bytes(b'broken SQLite')
        self.assertIn('ledger-unreadable-or-changed', [i['issue'] for i in ui.olay_izi(self.home, self.rid)['issues']])
        self.ledger.path.unlink()
        self.reserve(self.attempt(1))
        original = ui._iz_kaynagi
        reads = 0
        def changed(home, relative):
            nonlocal reads
            raw, modified, moving = original(home, relative)
            if str(relative).endswith('request-budget.db'):
                reads += 1
                if reads > 1:
                    raw += b'changed'
            return raw, modified, moving
        with patch.object(ui, '_iz_kaynagi', side_effect=changed):
            trace = ui.olay_izi(self.home, self.rid)
        self.assertFalse(any(e['type'].startswith('request_') for e in trace['events']))
        self.assertIn('ledger-unreadable-or-changed', [i['issue'] for i in trace['issues']])
        wal = self.ledger.path.with_name(self.ledger.path.name + '-wal')
        wal.symlink_to(self.f.repo / 'README.md')
        trace = ui.olay_izi(self.home, self.rid)
        self.assertFalse(any(e['type'].startswith('request_') for e in trace['events']))
        self.assertIn('unreadable-or-limit', [i['issue'] for i in trace['issues']])

    def test_plan_mirror_conflict_unknown_review_and_nested_child_sources_remain_visible_after_restart(self):
        self.plan()
        record = self.store.read(self.rid)
        record['plan_reviews'].append({'status': 'ERROR', 'reason': 'interrupted-review', 'revision_number': 1, 'created': record['updated']})
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        (self.store.path(self.rid) / 'plan.json').write_text('{}')
        child = self.store.accept('child', 'child task')['run_id']
        grandchild = self.store.accept('grandchild', 'nested child task')['run_id']
        self.store.update(child, proje_ust=self.rid)
        self.store.update(grandchild, proje_ust=child)
        (self.store.path(grandchild) / 'dispatch.log').write_text('2026-10-11T12:00:00Z nested output\n')
        before = self.files()
        trace = ui.olay_izi(self.home, self.rid)
        self.assertIn('execution-plan-mismatch', [i['issue'] for i in trace['issues']])
        self.assertTrue(any(e['data'].get('reason') == 'interrupted-review' for e in trace['events'] if isinstance(e['data'], dict)))
        nested = next(e for e in trace['events'] if e['run_id'] == grandchild and e['type'] == 'log')
        self.assertEqual(nested['parent_run_id'], child)
        self.assertEqual(nested['root_run_id'], self.rid)
        env = {**os.environ, 'CADRE_HOME': str(self.home)}
        exported = subprocess.run([sys.executable, str(LIB / 'arayuz_veri.py'), self.rid], env=env, text=True, capture_output=True, timeout=20)
        self.assertEqual(exported.returncode, 0, exported.stderr)
        self.assertEqual(json.loads(exported.stdout), trace)
        self.assertEqual(before, self.files())

    def test_json_markdown_and_offline_replay_redact_new_journal_route_and_ledger_fields(self):
        self.plan()
        secret = 'SYNTHETIC_TRACE_PASSWORD'
        self.reserve(self.attempt(1), password=secret, request_id='provider-call-1')
        route = self.home / 'zincir/yonlendirici.jsonl'
        route.write_text(json.dumps({'run_id': self.rid, 'olay': 'zincir', 'password': secret, 'neden': ['approved route']}) + '\n{broken\n')
        env = {**os.environ, 'CADRE_HOME': str(self.home)}
        command = [sys.executable, str(LIB / 'arayuz_veri.py')]
        exported = subprocess.run([*command, self.rid], env=env, text=True, capture_output=True, timeout=20)
        self.assertEqual(exported.returncode, 0, exported.stderr)
        trace = json.loads(exported.stdout)
        self.assertNotIn(secret, exported.stdout)
        self.assertIn('[REDACTED]', exported.stdout)
        self.assertTrue(any(e['data'].get('provider_request_id') == 'provider-call-1' for e in trace['events'] if isinstance(e['data'], dict)))
        before = self.files()
        with patch.object(ui, '_iz_kaynagi', side_effect=AssertionError('replay must not open sources')):
            self.assertEqual(ui.iz_replay(exported.stdout), trace)
        replay = subprocess.run([*command, '--replay', '--format', 'markdown'], input=exported.stdout,
                                env={**env, 'CADRE_HOME': str(self.home / 'absent')}, text=True, capture_output=True, timeout=20)
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertNotIn(secret, replay.stdout)
        self.assertIn('approved route', replay.stdout)
        self.assertIn('/reservation/', replay.stdout)
        self.assertEqual(before, self.files())

    def test_withdrawal_and_rejected_revision_need_recorded_review_before_resume(self):
        self.plan()
        self.store.change_plan(self.rid, 'withdraw-change', 1, 'Withdraw this request')
        self.store.stop_plan(self.rid, 'withdraw-change')
        self.store.withdraw_plan(self.rid, 'withdraw-change')
        self.store.resume_plan(self.rid, 'withdraw-change', 1)
        self.store.change_plan(self.rid, 'reject-change', 1, 'Improve README wording')
        self.store.stop_plan(self.rid, 'reject-change')
        generate_request_plan(self.store, self.rid, str(self.f.fake))
        (self.home / 'pipeline-mode').write_text('reject')
        self.assertEqual(review_plan(self.store, self.rid, 2, str(self.f.fake))['status'], 'FAIL')
        with self.assertRaisesRegex(ValueError, 'current-plan-review-required'):
            self.store.resume_plan(self.rid, 'reject-change', 2)
        trace = ui.olay_izi(self.home, self.rid)
        self.assertTrue(any(e['type'] == 'plan_change:review_rejected' and e['evidence_status'] == 'recorded' for e in trace['events']))
        self.assertFalse(any(e['type'] == 'plan_change:resumed' and e['change_id'] == 'reject-change' for e in trace['events']))
        (self.home / 'pipeline-mode').write_text('routine')
        self.assertEqual(review_plan(self.store, self.rid, 2, str(self.f.fake))['status'], 'FAIL', 'reddedilen revizyon ayni ortamda yeniden incelenmez')
        generate_request_plan(self.store, self.rid, str(self.f.fake))
        self.assertEqual(self.store.plan_view(self.rid)['revision'], 3)
        self.assertEqual(review_plan(self.store, self.rid, 3, str(self.f.fake))['status'], 'PASS')
        self.store.resume_plan(self.rid, 'reject-change', 3)
        trace = ui.olay_izi(self.home, self.rid)
        self.assertTrue(any(e['type'] == 'plan_change:resumed' and e['change_id'] == 'reject-change' and e['revision'] == 3 for e in trace['events']))
        self.assertFalse(any(i['issue'].endswith('conflict') for i in trace['issues']), trace['issues'])

    def test_missing_review_identity_and_malformed_replay_cannot_promote_a_resume_decision(self):
        from copy import deepcopy
        self.plan()
        self.revise()
        record = self.store.read(self.rid)
        record['plan_reviews'][-1].pop('attempt_id')
        record['plan_changes'][0]['stop']['runs'] = None
        atomic_json(self.store.path(self.rid) / 'run.json', record)
        route = self.home / 'zincir/yonlendirici.jsonl'
        route.parent.mkdir(exist_ok=True)
        route.write_text(json.dumps({'run_id': self.rid, 'olay': 'future-route', 'timestamp': float('nan'), 'evidence_status': 'PASS'}) + '\n')
        trace = ui.olay_izi(self.home, self.rid)
        self.assertEqual(ui.iz_replay(json.dumps(trace, allow_nan=False)), trace)
        self.assertIn('plan-review-conflict', [i['issue'] for i in trace['issues']])
        self.assertEqual(next(e for e in trace['events'] if e['type'] == 'plan_change:resumed')['evidence_status'], 'conflict')
        for field, value in (('locations', [123]), ('evidence_status', 'PASS')):
            bad = deepcopy(trace)
            bad['events'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                ui.iz_replay(json.dumps(bad))
        bad = deepcopy(trace)
        bad['sources'][0]['stable'] = 'yes'
        with self.assertRaises(ValueError):
            ui.iz_replay(json.dumps(bad))
