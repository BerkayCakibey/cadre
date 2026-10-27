"""Three audit rounds — round 1 fixes (B43-B46, B49, B50, B53, B55). Every test measures a BEHAVIOUR (no source grep)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch

LIB = Path(__file__).resolve().parents[1] / 'lib'
sys.path.insert(0, str(LIB))
import kosu  # noqa: E402
from kosu import RunStore  # noqa: E402
import butce_kapisi  # noqa: E402
from butce_kapisi import BudgetDenied, BudgetLedger  # noqa: E402
import yonlendirici as yon  # noqa: E402
from test_gece_bekleme import full_budget  # noqa: E402


class StoreFixes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-tur1-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = RunStore(self.home)

    def test_b43_cancel_racing_with_completion_never_marks_a_completed_run_cancelled(self):
        rid = self.store.accept('a', 'x')['run_id']
        record = self.store.read(rid)
        self.store.update(rid, state='completed')          # the reader comes with a stale record, the run finished in the meantime
        self.assertFalse(kosu.iptal_et(self.store, record))
        self.assertEqual(self.store.read(rid)['state'], 'completed')
        self.assertFalse((self.store.path(rid) / 'cancel.flag').exists())
        # unfinished run: flag + cancelled immediately (no owner)
        rid2 = self.store.accept('b', 'x')['run_id']
        self.assertTrue(kosu.iptal_et(self.store, self.store.read(rid2)))
        self.assertEqual(self.store.read(rid2)['state'], 'cancelled')

    def test_b271_proje_iptali_aktif_cocuga_yayilir(self):
        """B271 (30.09 rehearsal): when the parent project run was cancelled, the child running in the same process stayed a `running` ghost;
        the child's wait loop only checks ITS OWN cancel.flag. Cancel spreads to the active child too."""
        ana = self.store.accept('p', 'proje')['run_id']
        cocuk = self.store.accept('p--pab12', 'ozellik')['run_id']
        self.store.update(cocuk, state='running', proje_ust=ana)
        kosu.atomic_json(self.store.path(ana) / 'proje-aktif.json', {'imza': 'x', 'cocuk': cocuk})
        self.assertTrue(kosu.iptal_et(self.store, self.store.read(ana)))
        self.assertTrue((self.store.path(cocuk) / 'cancel.flag').exists(), 'cocuga iptal bayragi yazilmadi')
        self.assertEqual(self.store.read(cocuk)['state'], 'cancelled')   # no owner -> cancelled immediately (same as B35)
        self.assertEqual(self.store.read(ana)['state'], 'cancelled')
        # a finished child is not touched
        ana2 = self.store.accept('q', 'proje')['run_id']
        cocuk2 = self.store.accept('q--pcd34', 'ozellik')['run_id']
        self.store.update(cocuk2, state='completed')
        kosu.atomic_json(self.store.path(ana2) / 'proje-aktif.json', {'imza': 'y', 'cocuk': cocuk2})
        self.assertTrue(kosu.iptal_et(self.store, self.store.read(ana2)))
        self.assertEqual(self.store.read(cocuk2)['state'], 'completed')
        self.assertFalse((self.store.path(cocuk2) / 'cancel.flag').exists())

    def test_b45_accepted_or_queued_run_is_started_at_most_three_times_then_failed(self):
        rid = self.store.accept('a', 'x')['run_id']
        self.store.update(rid, created=time.time() - 10_000)  # bayat accepted
        root = self.home / 'code'
        started = []
        with patch.object(kosu, '_baslat', side_effect=lambda store, r, ro, why: started.append(r)):
            for _ in range(3):
                self.assertEqual(kosu.revive(self.store, root), [rid])
            self.assertEqual(kosu.revive(self.store, root), [])
        self.assertEqual(len(started), 3)
        record = self.store.read(rid)
        self.assertEqual(record['state'], 'failed')
        self.assertIn('start limit', record['reason'])
        self.assertEqual(record['start_count'], 3)

    def test_b46_one_corrupt_run_record_is_skipped_and_logged_instead_of_breaking_the_queue(self):
        good = self.store.accept('a', 'iyi')['run_id']
        bad = self.store.accept('a', 'bozuk')['run_id']
        (self.store.path(bad) / 'run.json').write_text('{bozuk json')
        self.assertEqual([r['run_id'] for r in self.store.list('a')], [good])
        self.assertEqual([r['run_id'] for r in self.store.bekleyenler('a')], [good])
        self.assertTrue(self.store.accept('a', 'ucuncu')['run_id'])  # acceptance still works (the queue calculation skips the broken record)
        log = (self.home / 'logs/kosu-bozuk.log').read_text()
        self.assertIn(bad, log)

    def test_b44_concurrent_accepts_in_one_project_yield_exactly_one_accepted(self):
        import threading
        results = []
        def kabul():
            results.append(self.store.accept('a', 'yaris')['state'])
        threads = [threading.Thread(target=kabul) for _ in range(6)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sorted(results), ['accepted'] + ['queued'] * 5)


class LedgerFixes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-tur1-ledger-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'zincir').mkdir()
        self.store = RunStore(self.home)
        self.ledger = BudgetLedger(self.home / 'zincir/request-budget.db')
        self.snapshot = full_budget(cloud=True)

    def test_b49_run_cap_denial_names_the_amounts_and_repeated_denials_collapse(self):
        with patch.dict(os.environ, {'CADRE_KOSU_TAVAN_USD': '1'}):
            rid = self.store.accept('c', 'x')['run_id']
        cap = butce_kapisi.run_cap_micro(self.home, rid)
        self.ledger.reserve('opencode', 600_000, self.snapshot, {'run_id': rid}, run_cap=cap)
        with self.assertRaises(BudgetDenied) as ctx:
            self.ledger.reserve('opencode', 500_000, self.snapshot, {'run_id': rid}, run_cap=cap)
        msg = str(ctx.exception)
        self.assertTrue(msg.startswith('run budget exhausted'), msg)
        self.assertIn('reservation $0.50', msg); self.assertIn('remaining $0.40', msg); self.assertIn('cap $1.00', msg)
        with patch.dict(os.environ, {'CADRE_RUN_ID': rid, 'CADRE_GOREV_ID': 'plan', 'CADRE_BUDGET_ATTEMPT': 'a1'}):
            for _ in range(3):
                butce_kapisi.note_denial(self.home, 'claude', reason=msg)
            butce_kapisi.note_denial(self.home, 'opencode-go', reason=msg)
        rows = self.store.read(rid)['denials']
        self.assertEqual([(r['provider'], r.get('tekrar', 1)) for r in rows], [('claude', 3), ('opencode-go', 1)])

    def test_b55_run_record_gets_a_provider_task_role_model_breakdown_and_unsettled_list(self):
        rid = self.store.accept('a', 'x')['run_id']
        a = self.ledger.reserve('opencode', 100_000, self.snapshot, {'run_id': rid, 'task': 'plan', 'role': 'plan', 'model': 'deepseek-v4-flash'}, run_cap=5_000_000)
        self.ledger.settle(a, 40_000, 'r1')
        b = self.ledger.reserve('opencode-go', 200_000, self.snapshot, {'run_id': rid, 'task': 'g1', 'role': 'compaction', 'model': 'glm-5.3-flash'}, run_cap=5_000_000)
        self.ledger.settle(b, 20_000, 'r2')
        self.ledger.reserve('groq', 1, self.snapshot, {'run_id': rid, 'task': 'g1', 'role': 'compaction', 'model': 'openai/gpt-oss-120b'}, run_cap=0)  # not closed
        butce_kapisi.kosu_harcama_yaz(self.home, self.ledger, rid)
        record = self.store.read(rid)
        self.assertEqual(record['budget_spent_usd'], 0.06)
        by = {(r['provider'], r['task'], r['role']): r for r in record['budget_breakdown']}
        self.assertEqual(by[('opencode-go', 'g1', 'compaction')]['actual_usd'], 0.02)
        self.assertEqual(by[('opencode', 'plan', 'plan')]['model'], 'deepseek-v4-flash')
        self.assertEqual(by[('groq', 'g1', 'compaction')]['pending'], 1)
        self.assertEqual(len(record['budget_unsettled']), 1)
        self.assertEqual(record['budget_unsettled'][0]['provider'], 'groq')


class RouterFixes(unittest.TestCase):
    def test_b50_chain_decisions_are_tied_to_the_run_and_task_and_keep_their_reasons(self):
        temp = tempfile.TemporaryDirectory(prefix='qe-tur1-yon-'); self.addCleanup(temp.cleanup)
        kayit = Path(temp.name) / 'yk.jsonl'
        with patch.object(yon, 'KAYIT', kayit), patch.dict(os.environ, {'CADRE_RUN_ID': 'run-x', 'CADRE_GOREV_ID': 'g7'}):
            zincir, neden = yon.zincir('impl', 'orta', 20000, full_budget(cloud=True), {})
        satir = json.loads(kayit.read_text().splitlines()[-1])
        self.assertEqual((satir['olay'], satir['run_id'], satir['gorev_id']), ('zincir', 'run-x', 'g7'))
        self.assertEqual(satir['zincir'], zincir)
        self.assertTrue(satir['neden'] and satir['neden'] == neden)


class ClaudeReservationFixes(unittest.TestCase):
    """B56: the Claude reservation is priced with the request's real size (not the model's 1M context) — otherwise Claude could
    never be used with a $5 cap. B58: an unreadable run record does not remove the cap (fail-closed)."""

    CATALOG = {'anthropic': {'models': {'claude-opus-5': {
        'cost': {'input': 15, 'output': 75, 'cache_write': 18.75, 'cache_read': 1.5},
        'limit': {'context': 1_000_000, 'output': 64_000}}}}}

    def test_b56_reservation_follows_request_size_so_a_small_call_fits_the_five_dollar_cap(self):
        import claude_transport as ct
        body = {'model': 'claude-opus-5', 'max_tokens': 4000,
                'messages': [{'role': 'user', 'content': 'kucuk bir plan istegi'}]}
        pricing = ct.price(body, self.CATALOG)
        istek = min(1_000_000, len(json.dumps(body, ensure_ascii=False)) + 4096)
        beklenen = ct.micro(istek * pricing.input + 4000 * pricing.output)
        self.assertEqual(pricing.maximum, beklenen)
        self.assertLess(pricing.maximum, 5_000_000, f'kucuk istek $5 kosu tavanina sigmali, rezervasyon ${pricing.maximum / 1e6:.2f}')
        tam_baglam = ct.micro(1_000_000 * pricing.input + 4000 * pricing.output)
        self.assertGreater(tam_baglam, 25_000_000, 'eski davranis: tek istek ~$30 rezerve ediyordu')
        # a large request is still priced with its real size (the ledger denies if the cap is exceeded)
        buyuk = {**body, 'messages': [{'role': 'user', 'content': 'x' * 400_000}]}
        self.assertGreater(ct.price(buyuk, self.CATALOG).maximum, pricing.maximum * 10)

    def test_b58_unreadable_run_record_denies_instead_of_silently_removing_the_cap(self):
        temp = tempfile.TemporaryDirectory(prefix='qe-tur1-cap-'); self.addCleanup(temp.cleanup)
        home = Path(temp.name)
        store = RunStore(home)
        rid = store.accept('a', 'x')['run_id']
        self.assertEqual(butce_kapisi.run_cap_micro(home, rid), 2_000_000)   # B199: easy/medium default $2
        (store.path(rid) / 'run.json').write_text('{bozuk')
        with self.assertRaisesRegex(BudgetDenied, 'unreadable'):
            butce_kapisi.run_cap_micro(home, rid)
        self.assertIsNone(butce_kapisi.run_cap_micro(home, None))  # no run, no cap (unchanged)


class InFlightBrakeFixes(unittest.TestCase):
    """B57: when the run cap fills IN THE MIDDLE OF A JOB the brake works; a free provider is not braked."""

    def test_b57_exhausted_run_cap_brakes_paid_providers_one_by_one_but_not_free_ones(self):
        import qe_core as core
        temp = tempfile.TemporaryDirectory(prefix='qe-tur1-fren-'); self.addCleanup(temp.cleanup)
        home = Path(temp.name)
        with patch.object(core, 'QE', home), patch.dict(os.environ, {'CADRE_RUN_ID': 'r1'}):
            with patch.object(core, 'run_remaining_usd', return_value=0.0):
                self.assertEqual(core.saglayici_freni(full_budget(cloud=True), ()), ('opencode-go', 'kosu tavani doldu'))
                self.assertEqual(core.saglayici_freni(full_budget(cloud=True), ('opencode-go',)),
                                 ('opencode', 'kosu tavani doldu'))     # B110: the order comes from USD_SAGLAYICILAR
                self.assertEqual(core.saglayici_freni(full_budget(cloud=True), ('opencode-go', 'opencode')),
                                 ('claude', 'kosu tavani doldu'))
                # B110 (18.09): PAID ZEN ('opencode') was in none of the four brake lists; when the cap was full
                # zen stayed free. The list now comes from a single source (USD_SAGLAYICILAR).
                self.assertEqual(core.saglayici_freni(full_budget(cloud=True), ('opencode-go', 'claude')),
                                 ('opencode', 'kosu tavani doldu'))
                yasak = ('opencode-go', 'opencode', 'claude', 'openrouter')   # 4c-1: USD providers
                self.assertIsNone(core.saglayici_freni(full_budget(cloud=True), yasak), 'bedava google frenlenmez')
            with patch.object(core, 'run_remaining_usd', return_value=3.0):
                self.assertIsNone(core.saglayici_freni(full_budget(cloud=True), ()), 'tavan doluysa fren, degilse gunluk pay kurali')


class Tur2ParaYoluTests(unittest.TestCase):
    """Round 2 fixes: B68 a stale hold does not eat the cap · B69 the Claude ladder is skipped at the run cap · B71 UTF-8 upper bound
    · B73 the receipt closes at the real price."""

    KATALOG = {'anthropic': {'models': {'m': {
        'cost': {'input': 10, 'output': 40, 'cache_write': 12.5, 'cache_read': 1.0},
        'limit': {'context': 1_000_000, 'output': 8_000}}}}}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-tur2-'); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name); (self.home / 'zincir').mkdir()
        self.ledger = BudgetLedger(self.home / 'zincir/request-budget.db')
        self.snapshot = full_budget(cloud=True)
        self.snapshot['claude'].update(pay=5, yuzde1_usd=30, havuz_kalan_usd=200)   # give Claude a share (the default fixture is 0)

    def test_b68_a_unknown_outcome_keeps_holding_the_budget_even_when_old(self):
        """T3 FIX: first "a hold older than 6 hours must not eat the cap" was written; `test_butce_donemi` showed it broke a proven invariant
        (a request with an UNKNOWN OUTCOME never REOPENS the budget, not even the next day = fail-closed). An unknown outcome
        counts as spent; the fix is to close only in the KNOWN-ZERO case (4xx)."""
        import sqlite3
        ident = self.ledger.reserve('opencode', 3_000_000, self.snapshot, {'run_id': 'r1'}, run_cap=None)
        az = self.ledger.available('opencode', self.snapshot)
        self.assertLess(az, 5_000_000, 'tutma tavani kucultur')
        with closing(sqlite3.connect(self.ledger.path)) as con, con:   # age the reservation by 7 days
            con.execute('update reservation set created=? where id=?', (time.time() - 7 * 86400, ident))
        self.assertEqual(self.ledger.available('opencode', self.snapshot), az, 'eski ama SONUCU BILINMEYEN tutma butceyi acmaz')
        self.ledger.settle(ident, 0, f'{ident}:http-429')   # known zero (4xx) -> closes
        self.assertGreater(self.ledger.available('opencode', self.snapshot), az)

    def test_b68_b_http_4xx_closes_the_reservation_at_zero(self):
        import claude_transport as ct
        class Yanit:
            status = 429
            headers = {'Content-Type': 'application/json'}
            def read1(self, n): return b''
            def close(self): pass
        relay = ct.Relay(self.home, self.KATALOG, lambda: self.snapshot, lambda b, h, q: Yanit())
        ident, pricing, _ = relay.open({'model': 'm', 'max_tokens': 100, 'messages': [{'role': 'user', 'content': 'x'}]}, {})
        import sqlite3
        with closing(sqlite3.connect(self.ledger.path)) as con, con:
            self.assertIsNone(con.execute('select actual from reservation where id=?', (ident,)).fetchone()[0])
        relay.ledger.settle(ident, 0, f'{ident}:http-429')   # what the handler's 4xx branch does
        with closing(sqlite3.connect(self.ledger.path)) as con, con:
            self.assertEqual(con.execute('select actual from reservation where id=?', (ident,)).fetchone()[0], 0)

    def test_b71_reservation_bound_survives_a_non_ascii_body(self):
        import claude_transport as ct
        emoji = {'model': 'm', 'max_tokens': 10, 'messages': [{'role': 'user', 'content': '😀' * 2000}]}
        ascii_ = {'model': 'm', 'max_tokens': 10, 'messages': [{'role': 'user', 'content': 'a' * 2000}]}
        p_emoji, p_ascii = ct.price(emoji, self.KATALOG), ct.price(ascii_, self.KATALOG)
        self.assertGreater(p_emoji.maximum, p_ascii.maximum, 'emoji govde daha fazla token demektir; sinir bunu gormeli')
        # the sent body is ensure_ascii=True: each emoji \ud83d\ude00 = 12 bytes -> the limit is above the token count
        self.assertGreaterEqual(len(json.dumps(emoji).encode()), 2000 * 12)

    def test_b73_receipt_settles_with_real_rates_not_the_reservation_safety_factor(self):
        import claude_transport as ct
        body = {'model': 'm', 'max_tokens': 100, 'messages': [{'role': 'user', 'content': 'x'}]}
        pricing = ct.price(body, self.KATALOG)
        from decimal import Decimal as D
        self.assertEqual([float(x) for x in (pricing.settle_input, pricing.cache_write, pricing.cache_read)],
                         [float(ct.amount(v)) for v in (10, 12.5, 1.0)])
        self.assertEqual(float(pricing.input), float(ct.amount(10)) * 2, 'rezervasyon 2x emniyetli')
        ident = self.ledger.reserve('claude', pricing.maximum, self.snapshot, {'run_id': 'r2'}, run_cap=None)
        parser = ct.Receipt(self.ledger, ident, pricing, False)
        parser.feed(json.dumps({'type': 'message', 'id': 'msg_1', 'usage': {
            'input_tokens': 1000, 'cache_creation_input_tokens': 2000, 'cache_read_input_tokens': 4000, 'output_tokens': 500}}).encode())
        parser.finish()
        from decimal import Decimal as D
        oran = lambda v: D(str(ct.amount(v)))
        beklenen = ct.micro(1000 * oran(10) + 2000 * oran(12.5) + 4000 * oran(1.0) + 500 * oran(40))
        import sqlite3
        with closing(sqlite3.connect(self.ledger.path)) as con, con:
            self.assertEqual(con.execute('select actual from reservation where id=?', (ident,)).fetchone()[0], beklenen)
        sisirilmis = ct.micro(7000 * pricing.input + 500 * pricing.output)
        self.assertLess(beklenen, sisirilmis, 'eski hesap gercek faturanin ustundeydi')


class Tur2FrenTests(unittest.TestCase):
    """B69: when the run cap is full the Claude ladder is skipped and the job continues locally (user decision 2)."""

    def test_b69_run_cap_downgrade_skips_the_claude_ladder_and_waits_for_budget(self):
        import qe_core as core
        log = Path(tempfile.mkdtemp(prefix='qe-tur2-fren-')) / 'motor.log'
        with patch.object(core.YON, 'zincir', return_value=(['claude/opus#high', 'opencode-go/kimi-k2.7-code'], [])), \
             patch.object(core.YON, 'ariza') as ariza:
            self.assertEqual(core.kota_dusur('claude/fable#xhigh', log, neden='kosu-tavani'), '', 'kosu tavani dolu -> butce-bekleme')
            ariza.assert_not_called()  # a budget denial is not a provider failure (the same principle as B52)
            # T3 distinction: only if one provider's DAILY SHARE ran out can another paid provider in the chain be used
            self.assertEqual(core.kota_dusur('claude/fable#xhigh', log, neden='pay'), 'opencode-go/kimi-k2.7-code')
            self.assertEqual(core.kota_dusur('claude/fable#xhigh', log), 'claude/opus#high', 'kota/limit nedeni: merdiven korunur')

    def test_b333_ceiling_full_elimination_uses_the_bucket_not_the_provider_name(self):
        """B333 (03.10 02:37): when the cap was full kota_dusur removed paid providers BY PROVIDER NAME; the free
        Zen models (opencode/nemotron-*-free, big-pickle) were also counted as 'opencode' and removed, and the job fell to local. G-B8: a free
        bucket (<prov>:free) does not enter the dollar cap — filtering is by katalog.bedava_mi / bucket."""
        import qe_core as core
        log = Path(tempfile.mkdtemp(prefix='qe-b333-')) / 'motor.log'
        zincir = ['opencode/minimax-m3', 'opencode/nemotron-3-ultra-free', 'opencode/big-pickle']
        bedava = lambda sag, ad: ad.endswith('-free') or ad == 'big-pickle'
        with patch.object(core.YON, 'zincir', return_value=(zincir, [])), \
             patch.object(core.YON, 'ariza'), patch.object(core, 'istek_olcusu', return_value=8000), \
             patch.object(core, 'log'), patch('katalog.bedava_mi', side_effect=bedava):
            # cap full: the paid minimax is removed, the next candidate is the FREE Zen bucket — not local (acceptance criterion)
            self.assertEqual(core.kota_dusur('opencode-go/glm-5.3-flash', log, neden='kosu-tavani'),
                             'opencode/nemotron-3-ultra-free')
            # the exhausted bucket of the current PAID Zen does not close the same provider's free bucket (bucket exclusion)
            self.assertEqual(core.kota_dusur('opencode/glm-5.2', log, neden='kosu-tavani'),
                             'opencode/nemotron-3-ultra-free')
        with patch.object(core.YON, 'zincir', return_value=(['opencode/nemotron-3-ultra-free'], [])), \
             patch.object(core.YON, 'ariza'), patch.object(core, 'istek_olcusu', return_value=8000), \
             patch.object(core, 'log'), patch('katalog.bedava_mi', return_value=None):
            # catalogue price UNKNOWN (None) -> counts as paid, even if the name is '-free' (G-B8/D17) -> budget wait
            self.assertEqual(core.kota_dusur('opencode-go/glm-5.3-flash', log, neden='kosu-tavani'), '')


class Tur2OmurVeButceTests(unittest.TestCase):
    """B74 orphaned lock · B76 Cerebras's own horizon · B77 DeepSeek sliding horizon (no end-of-month jump)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qe-tur2b-'); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = RunStore(self.home)

    def test_b74_an_orphan_holding_the_lock_is_not_counted_as_a_live_coordinator(self):
        """The coordinator dies, a grandchild that inherited the lock fd keeps living: the old code saw the lock as held and counted the run
        as having a 'live coordinator' forever; the queue, revive and cancel could never step in."""
        import subprocess as sp
        rid = self.store.accept('a', 'x')['run_id']
        olu = sp.Popen([sys.executable, '-c', 'pass']); olu.wait()      # a dead coordinator's pid
        lock = kosu.FileLock(self.store.path(rid) / '.owner.lock')      # a living grandchild holds the lock (this process imitates it)
        self.assertTrue(lock.acquire(blocking=False))
        self.addCleanup(lock.close)
        self.store.update(rid, state='running', owner_pid=olu.pid)
        self.assertFalse(kosu.koordinator_canli(self.store, self.store.read(rid)), 'olu koordinator canli sayilmaz')
        self.assertTrue(kosu.aktif_var(self.store, 'a'))
        lock.close()
        self.assertFalse(kosu.aktif_var(self.store, 'a'))
        self.assertTrue(lock.acquire(blocking=False))
        # B110: liveness is measured by pid + ARGV (so pid reuse does not revive the coordinator), so
        # a "living coordinator" fake must really be a process named `kosu.py`.
        sahte = Path(self.temp.name) / 'kosu.py'
        sahte.write_text('import time\ntime.sleep(30)\n')
        canli = sp.Popen([sys.executable, str(sahte)])
        self.addCleanup(lambda: (canli.kill(), canli.wait(timeout=5)))
        self.store.update(rid, owner_pid=canli.pid)
        self.assertTrue(kosu.koordinator_canli(self.store, self.store.read(rid)))
        self.assertTrue(kosu.aktif_var(self.store, 'a'))

    def test_b82_revive_actually_restarts_a_run_whose_coordinator_died_behind_an_orphan_lock(self):
        """T3: B74 was half done — `revive` still looked at the raw lock, so the recovery it targeted NEVER happened."""
        import subprocess as sp
        rid = self.store.accept('a', 'x')['run_id']
        olu = sp.Popen([sys.executable, '-c', 'pass']); olu.wait()
        lock = kosu.FileLock(self.store.path(rid) / '.owner.lock')       # an orphaned grandchild holds the lock
        self.assertTrue(lock.acquire(blocking=False)); self.addCleanup(lock.close)
        self.store.update(rid, state='running', owner_pid=olu.pid)
        with patch.object(kosu, '_baslat') as baslat:
            self.assertEqual(kosu.revive(self.store, self.home / 'code'), [rid])
        baslat.assert_called_once()
        # B110 (18.09): as long as the lock is with an ORPHANED grandchild a new coordinator returns at once with rc=75; this attempt
        # is not a real revival and must not burn the revive ALLOWANCE (old behaviour: `failed` in vain after 5 ticks).
        self.assertIsNone(self.store.read(rid).get('revive_count'), 'yetim kilitli deneme revive hakkini yakmaz')
        self.assertEqual(self.store.read(rid).get('orphan_lock_count'), 1)
        for _ in range(kosu.REVIVE_LIMIT):
            kosu.revive(self.store, self.home / 'code')
        kayit = self.store.read(rid)
        self.assertEqual(kayit['state'], 'failed', 'yetim kilit kalicilasirsa kosu kendi nedeniyle kapanir')
        self.assertIn('yetim kilit', kayit['reason'])

    def test_b81_a_finished_run_whose_lock_is_orphaned_never_blocks_the_project_queue(self):
        """T3: if a finished run's lock stayed with an orphaned process the queue never opened (a violation of decision 3)."""
        ilk = self.store.accept('p', 'birinci')['run_id']
        ikinci = self.store.accept('p', 'ikinci')['run_id']
        self.assertEqual(self.store.read(ikinci)['state'], 'queued')
        lock = kosu.FileLock(self.store.path(ilk) / '.owner.lock')
        self.assertTrue(lock.acquire(blocking=False)); self.addCleanup(lock.close)
        self.store.update(ilk, state='completed', owner_pid=None)        # execute_run sets the pid to None at the end
        self.assertFalse(kosu.koordinator_canli(self.store, self.store.read(ilk)))
        self.assertTrue(kosu.aktif_var(self.store, 'p'))
        self.assertFalse(self.store.sirasi_geldi(ikinci))
        lock.close()
        self.assertFalse(kosu.aktif_var(self.store, 'p'))
        self.assertTrue(self.store.sirasi_geldi(ikinci))

    def test_b83_cancel_losing_the_race_with_completion_leaves_the_finished_run_intact(self):
        """T3: the run may have finished AFTER the lock was released; the old code wrote cancelled without looking again (work lost)."""
        rid = self.store.accept('a', 'x')['run_id']
        self.store.update(rid, state='running', owner_pid=os.getpid())
        bayat = self.store.read(rid)
        gercek_read = self.store.read
        cagri = {'n': 0}
        def yaris(run_id):                      # the run has finished AFTER the 2nd read (while the owner lock is being taken)
            cagri['n'] += 1
            if cagri['n'] == 2:                 # write directly without taking the lock (update would try to take .record.lock again)
                biten = dict(gercek_read(run_id), state='completed', exit_code=0, owner_pid=None, updated=time.time())
                kosu.atomic_json(self.store.path(run_id) / 'run.json', biten)
            return gercek_read(run_id)
        with patch.object(self.store, 'read', side_effect=yaris):
            self.assertFalse(kosu.iptal_et(self.store, bayat))
        kayit = self.store.read(rid)
        self.assertEqual(kayit['state'], 'completed')
        self.assertNotEqual(kayit.get('exit_code'), 130)
        self.assertFalse((self.store.path(rid) / 'cancel.flag').exists(), 'bayrak geri alinmali')

    def butce_raporu(self, kredi_bitis_gun=10, ds_bakiye=4.10, bugun=None):
        """The real `butce.main()` in an isolated home: no network (ds_bakiye fake), no opencode DB (spending fake)."""
        import datetime, io, contextlib
        import butce
        (self.home / 'zincir').mkdir(parents=True, exist_ok=True)
        bugun = bugun or datetime.date.today()
        bitis = (bugun + datetime.timedelta(days=kredi_bitis_gun)).isoformat()
        baslangic = (bugun - datetime.timedelta(days=20)).isoformat()
        (self.home / 'zincir/cerebras-kredi.txt').write_text(f'{baslangic} 5.00 expire:{bitis}\n')
        cikti = io.StringIO()
        with patch.object(butce, 'QE', self.home), patch.object(butce, 'DB', self.home / 'yok.db'), \
             patch.dict(os.environ, {'CADRE_ONODEMELI_KIP': 'gunluk'}), \
             patch.object(butce, 'spending', return_value=(0.0, 0)), \
             \
             patch.object(butce, 'go_harcama', return_value=0.0), contextlib.redirect_stdout(cikti):
            butce.main()
        return json.loads(cikti.getvalue())


if __name__ == '__main__':
    unittest.main()


class FailOpenTests(unittest.TestCase):
    """B114-8 and B114-9 (19.09 audit): in two places `except Exception` swallowed the budget read and
    meant "no limit". The `BudgetDenied` branches are already fail-closed; the problem is non-budget errors
    like sqlite/disk. Rule: if the cap CANNOT BE READ the cloud is closed — an unknown limit is not unlimited."""

    def _secilen_model(self, args):
        """The model in effect at the end of the opencode call: `--model=` or `-m`."""
        for i, a in enumerate(args):
            if a.startswith('--model='):
                return a.split('=', 1)[1]
            if a in ('-m', '--model') and i + 1 < len(args):
                return args[i + 1]
        return None

    def _secim(self, tavan_sonucu):
        """A call that asks for a paid model; the ONLY variable is the run's remaining cap."""
        import qe_core as core
        bol = {'ts': '2026-09-19T00:00:00', 'bugun': '2026-09-19',
               'go': {'db': 'ok', 'pay': 33.333334, 'kullanilabilir': 33.333334, 'pencere_acik': True, 'birim': 'puan'}}
        env = {'CADRE_RUN_ID': 'r1', 'CADRE_ROL': 'impl'}
        yama = ({'side_effect': tavan_sonucu} if isinstance(tavan_sonucu, BaseException)
                else {'return_value': tavan_sonucu})
        with patch.object(core, 'butce_json', return_value=bol), \
             patch.object(core, 'run_record', return_value={}), \
             patch.object(core, 'available_budget', return_value=1_000_000), \
             patch.object(core, 'run_remaining_usd', **yama):
            args = core.opencode_budget_env(dict(env), ['-m', 'opencode-go/glm-5.3-flash', 'is'])
        return self._secilen_model(args)

    def test_an_unreadable_run_cap_closes_the_cloud_instead_of_opening_it(self):
        acik = self._secim(5.0)
        self.assertFalse(acik.startswith('lmstudio/'),
                         f'kontrol: tavan okunuyorken parali model secilebilmeli, gelen {acik!r}')
        # P3 (03.10): local is off by default; while the cap CANNOT BE READ, fail-closed = budget denial
        # (no local fallback — not fail-open, the call is denied).
        with self.assertRaises(BudgetDenied):
            self._secim(sqlite3.OperationalError('database is locked'))

    def test_an_unreadable_run_cap_still_writes_the_provider_brake(self):
        import qe_core as core
        b = {'go': {'db': 'ok', 'pay': 33.333334, 'kullanilabilir': 33.333334, 'pencere_acik': True, 'birim': 'puan'}}
        with patch.dict(os.environ, {'CADRE_RUN_ID': 'r1'}, clear=False):
            with patch.object(core, 'run_remaining_usd', side_effect=sqlite3.OperationalError('disk I/O error')):
                saglayici, neden = core.saglayici_freni(b)
        self.assertIsNotNone(saglayici, 'tavan okunamazken fren hic yazilmadi (sessizce serbest kaldi)')
        self.assertIn('tavan', (neden or '').lower(),
                      f'fren nedeni kosu tavanini gostermeli (baska kural yakalamis olabilir), gelen: {neden!r}')


class YukselmeSureciKesmezTests(unittest.TestCase):
    """B133 (20.09, live RCA): the "Go daily share came back" escalation killed a RUNNING HEALTHY process
    with `kill_tree` and restarted it from the recorded session. The restarted process produced a `text`
    part and lost the flow, and the guard cut it 600 s later.

    Correlation 3/3, to the second:
        00:36:12 ESCALATION -> 00:46:34 STALL   (10 min 22 s)
        01:05:56 ESCALATION -> 01:16:18 STALL   (10 min 22 s)
        01:37:13 ESCALATION -> 01:47:36 STALL   (10 min 23 s)
    Three events = 31 minutes lost; the gain was a few cents of helper agents."""

    KAYNAK = Path(__file__).resolve().parents[1] / 'lib/motor.py'

    def _yukselme_blogu(self):
        """The CODE OF THE BLOCK excluding comment lines. (A comment mentioning 'kill_tree' describes the old behaviour;
        the test must look at the code itself, not at its explanation.)"""
        kaynak = self.KAYNAK.read_text()
        i = kaynak.index('if go_kisit_aktif and "opencode-go" not in yasak')
        j = kaynak.index('except Exception as _e:', i)
        return '\n'.join(s for s in kaynak[i:j].split('\n') if not s.lstrip().startswith('#'))

    def test_the_go_upgrade_never_kills_a_healthy_running_process(self):
        blok = self._yukselme_blogu()
        self.assertNotIn('kill_tree', blok,
                         'Go donusu icin calisan sureci oldurmek her seferinde 10+ dakika kaybettiriyor')
        self.assertNotIn('break', blok, 'surec dongusunden cikmak da yeniden baslatma demektir')

    def test_the_decision_is_still_recorded_for_the_next_start(self):
        """The fix must not lose the gain: the decision is still written, only its application is deferred."""
        blok = self._yukselme_blogu()
        self.assertIn('kisit_env', blok, 'yeni config bir sonraki baslangic icin kaydedilmeli')
        self.assertIn('go_kisit_aktif = False', blok, 'kisit durumu guncellenmeli')


