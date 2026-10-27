"""Acceptance package driver (acceptance_package.py + acceptance_fake.py): frozen fixture verification, check/decision rules and the real engine path
with a fake provider (typed request -> two engine approvals -> plan/separate review -> worker/QA/judge -> delivery). A fake PASS is not a live
acceptance (LOCAL_PASS); a judge rejection cannot force the driver to PASS. Fixtures are produced from live_scenario constants; no campaign directory is needed."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from contextlib import closing, contextmanager

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import live_scenario  # noqa: E402
import acceptance_package  # noqa: E402

FROZEN_BACKLOG = '# Frozen synthetic acceptance backlog\n\n' + ''.join(
    f"## [ ] {mid} — {definition}\nDependencies: {', '.join(bag) or 'absent'}\nAcceptance: python3 -m unittest -v tests.test_{mid.lower()}\n\n"
    for mid, (definition, bag, _) in live_scenario.BACKLOG_TESTS.items())
ORDERS = 'order_id,date,status,amount\no1,2026-10-14,completed,12.30\no2,2026-10-14,cancelled,4.50\no3,2026-10-14,completed,7.70\n'


def fixture_tree(root: Path) -> Path:
    fixtures = root / 'fixtures'
    r01 = fixtures / 'R01'
    r01.mkdir(parents=True)
    (r01 / 'calc.py').write_text(live_scenario.CALC)
    (r01 / 'test_calc.py').write_text(live_scenario.TEST_CALC)
    (r01 / 'request.txt').write_text(live_scenario.PROMPT_CALC)
    r04 = fixtures / 'R04'
    (r04 / 'tests').mkdir(parents=True)
    (r04 / 'tests/__init__.py').write_text('')
    for mid, (_, _, body) in live_scenario.BACKLOG_TESTS.items():
        (r04 / f'tests/test_{mid.lower()}.py').write_text('import unittest\n' + body)
    (r04 / 'text.py').write_text(''.join(f'def {entry_name}(text):\n    raise NotImplementedError\n\n' for entry_name in ('words', 'word_count', 'reverse_convert', 'longest_word', 'summary')))
    (r04 / 'text_cli.py').write_text('raise NotImplementedError("B005 pending")\n')
    (r04 / 'BACKLOG.md').write_text(FROZEN_BACKLOG)
    (r04 / 'request.txt').write_text('PLAN ONLY: produce a plan that preserves the source ids, criteria and dependencies in BACKLOG.md and submit it for a separate clean review. '
                                     'Do not start implementation or child runs; do not change product code or the tests/ directory.\n')
    r07 = fixtures / 'R07'
    r07.mkdir()
    (r07 / 'orders.csv').write_text(ORDERS)
    (r07 / 'metadata.json').write_text(json.dumps({'synthetic': True, 'as_of': '2026-10-15', 'source_date': '2026-10-14', 'max_age_days': 7}, indent=2) + '\n')
    (r07 / 'contradictory-summary.txt').write_text('SYNTHETIC conflicting claim: completed total = 21.00\n')
    (r07 / 'request.txt').write_text('Offline operational report: separate dated sources, calculations and comments for orders.csv. Link numbers to source lines.\n')
    manifest = {str(p.relative_to(fixtures)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(fixtures.rglob('*')) if p.is_file()}
    (root / 'fixture-manifest.json').write_text(json.dumps(manifest, indent=2))
    return fixtures


class AcceptancePackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory(prefix='acceptance-package-')
        cls.root = Path(cls.temp.name).resolve()
        cls.fixtures = fixture_tree(cls.root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def driver(self, id: str, *extra: str, home_dir: str | None = None) -> tuple[subprocess.CompletedProcess, dict | None]:
        home_dir_path = self.root / (home_dir or f'home-dir-{id}-{len(extra)}')
        output = home_dir_path / 'evidence'
        env = {k: v for k, v in os.environ.items() if not k.startswith(('CADRE_', 'OPENCODE_', 'E2E_'))}
        env.update(CADRE_HOME=str(home_dir_path), CADRE_ENGINE_ROOT=str(TESTS.parent))
        result = subprocess.run([sys.executable, str(TESTS / 'acceptance_package.py'), id, '--fixture-dir', str(self.fixtures), '--path', 'fake',
                                 '--minute', '6', '--cap', '2', '--output', str(output), *extra], env=env, capture_output=True, text=True, timeout=500)
        summary = json.loads((output / 'summary.json').read_text()) if (output / 'summary.json').exists() else None
        return result, summary

    def test_runtime_home_and_fixture_manifest_are_enforced(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith('CADRE_')}
        fallback = subprocess.run([sys.executable, str(TESTS / 'acceptance_package.py'), 'R01', '--fixture-dir', str(self.fixtures)],
                                    env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(fallback.returncode, 0)
        self.assertIn('isolated', fallback.stderr)
        broken = self.root / 'broken'
        broken.mkdir()
        (broken / 'fixtures/R01').mkdir(parents=True)
        for entry_name in ('calc.py', 'test_calc.py', 'request.txt'):
            (broken / 'fixtures/R01' / entry_name).write_bytes((self.fixtures / 'R01' / entry_name).read_bytes())
        (broken / 'fixture-manifest.json').write_text(json.dumps({'R01/calc.py': '0' * 64}))
        env.update(CADRE_HOME=str(self.root / 'home-dir-broken'), CADRE_ENGINE_ROOT=str(TESTS.parent))
        result = subprocess.run([sys.executable, str(TESTS / 'acceptance_package.py'), 'R01', '--fixture-dir', str(broken / 'fixtures'), '--path', 'fake'],
                                env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('does not match the manifest', result.stderr)
        self.assertFalse((self.root / 'home-dir-broken' / 'runs').exists(), 'no request is recorded after a manifest mismatch')

    def test_small_fix_scenario_passes_locally_with_positive_and_negative_checks(self):
        result, summary = self.driver('R01')
        self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:])
        self.assertIsNotNone(summary)
        self.assertEqual(summary['decision'], 'LOCAL_PASS', summary['check_count'])
        self.assertFalse(summary['live'])
        outcomes = {c['id']: c['outcome'] for c in summary['checks']}
        self.assertEqual(outcomes['R01-N1'], 'PASS')
        self.assertEqual(outcomes['R01-O2'], 'PASS')
        self.assertEqual(outcomes['R01-O3'], 'PASS', 'a product change after QA must invalidate acceptance')
        self.assertEqual(outcomes['R01-L1'], 'NOT_APPLICABLE', 'receipt gates do not apply in fake mode')
        self.assertTrue(any(c['outcome'] == 'GATE' for c in summary['checks']), 'negative behavior is bound to existing test families')
        self.assertGreaterEqual(summary['check_count']['normal'], 1)
        self.assertGreaterEqual(summary['check_count']['negative_limit'], 1)
        self.assertEqual(summary['fixture_verification']['difference'], [])
        self.assertEqual(summary['fake_calls'][:2], ['plan', 'plan-review'])
        self.assertIn('worker', summary['fake_calls'])
        self.assertTrue(Path(summary['runs'][0]).name.startswith('request-'))
        lines = (Path(result.stdout.strip().splitlines()[-1].split('SUMMARY: ')[-1]).parent.parent / 'acceptance-package.jsonl').read_text().splitlines()
        self.assertEqual(json.loads(lines[-1])['decision'], 'LOCAL_PASS')

    def test_plan_only_delivers_a_reviewed_plan_without_implementation(self):
        result, summary = self.driver('R04')
        self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:])
        self.assertEqual(summary['decision'], 'LOCAL_PASS', summary['check_count'])
        outcomes = {c['id']: c for c in summary['checks']}
        self.assertEqual(outcomes['R04-N2']['outcome'], 'PASS')
        self.assertEqual(outcomes['R04-O1']['outcome'], 'PASS')
        self.assertEqual(outcomes['R04-O2']['outcome'], 'PASS')
        self.assertEqual(outcomes['R04-O3']['outcome'], 'PASS')
        self.assertEqual(summary['fake_calls'], ['plan', 'plan-review'], 'plan only: no worker or final judge calls')

    def test_evidence_report_passes_and_local_report_qa_rejects_contradiction_and_missing_source(self):
        result, summary = self.driver('R07')
        self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:])
        self.assertEqual(summary['decision'], 'LOCAL_PASS', summary['check_count'])
        outcomes = {c['id']: c for c in summary['checks']}
        self.assertEqual(outcomes['R07-N3']['outcome'], 'PASS', 'the independent calculation must match the report')
        self.assertEqual(outcomes['R07-N5']['outcome'], 'PASS', 'single-analyst mode must appear in the report (N-REP03)')
        self.assertEqual(outcomes['R07-O1']['outcome'], 'PASS')
        self.assertEqual(outcomes['R07-O3-contradiction']['outcome'], 'PASS')
        self.assertEqual(outcomes['R07-O3-missing-source']['outcome'], 'PASS')
        self.assertEqual(summary['independent_account'], {'completed': '20.00', 'cancelled': '4.50', 'all': '24.50'})

    def test_final_reviewer_rejection_cannot_be_reported_as_pass(self):
        result, summary = self.driver('R01', '--mode', 'judge-rejection', home_dir='home-dir-r01-rejection')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(summary['decision'], 'FAIL')
        outcomes = {c['id']: c for c in summary['checks']}
        self.assertEqual(outcomes['R01-N1']['outcome'], 'FAIL')
        self.assertNotEqual(outcomes['R01-N1']['observed'][2], 'PASS')
        self.assertIn('R01-N1', summary['check_count']['failed'])


    def test_obligation_refund_flag_keeps_the_unverified_dry_run_share_reserved(self):
        result, summary = self.driver('R01', '--liability-refund', '--package-cap', '5', home_dir='home-dir-refund')
        self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:])
        self.assertEqual(summary['decision'], 'LOCAL_PASS')
        self.assertTrue(summary['package_share']['liability_refund'])
        self.assertEqual((summary['package_share']['brut_usd'], summary['package_share']['refund_usd'], summary['package_share']['remaining_usd']), (2.0, 0.0, 3.0))
        self.assertEqual([o['outcome'] for o in summary['package_refund']], ['waiting'])
        self.assertIn('receipt', summary['package_refund'][0]['reason'])

    def test_proposal_only_mode_honours_operator_chain_and_makes_no_approval_or_call(self):
        """CXR-04: CADRE_CHAIN_<ROLE> is carried to the subprocess and the proposal lists only that model; --only-proposal makes no approval/call."""
        home_dir_k, home_dir = self.root / 'home-dir-proposal-small', self.root / 'home-dir-proposal'
        output = home_dir / 'evidence'
        env = {k: v for k, v in os.environ.items() if not k.startswith(('CADRE_', 'OPENCODE_', 'E2E_'))}
        env.update(CADRE_ENGINE_ROOT=str(TESTS.parent), CADRE_CHAIN_REVIEW='opencode-go/glm-5.3', CADRE_CHAIN_QUALITY='opencode-go/glm-5.3')
        small = subprocess.run([sys.executable, str(TESTS / 'acceptance_package.py'), 'R01', '--fixture-dir', str(self.fixtures), '--path', 'fake',
                                '--minute', '6', '--cap', '0.25', '--package-cap', '4', '--output', str(home_dir_k / 'evidence'), '--only-proposal'],
                               env={**env, 'CADRE_HOME': str(home_dir_k)}, capture_output=True, text=True, timeout=300)
        self.assertNotEqual(small.returncode, 0, 'fixture catalog and minimum reservation cannot produce a $0.25 proposal; stops without approval or calls')
        self.assertIn('plan-review-no-capacity', small.stdout)
        self.assertFalse((home_dir_k / 'acceptance-calls.jsonl').exists())
        self.assertEqual([t['usd'] for t in json.loads((home_dir_k / 'acceptance-package-pay.json').read_text())['allocations']], [0.25],
                         'a rejected request remains in the allocation ledger; its allocation is not reset')
        result = subprocess.run([sys.executable, str(TESTS / 'acceptance_package.py'), 'R01', '--fixture-dir', str(self.fixtures), '--path', 'fake',
                                 '--minute', '6', '--cap', '2', '--package-cap', '4', '--output', str(output), '--only-proposal'],
                                env={**env, 'CADRE_HOME': str(home_dir)}, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout[-1500:] + result.stderr[-1500:])
        summary = json.loads((output / 'summary.json').read_text())
        self.assertEqual(summary['decision'], 'PROPOSAL')
        self.assertEqual(summary['proposal_models']['review'], ['opencode-go/glm-5.3'])
        self.assertEqual(summary['proposal_models']['quality'], ['opencode-go/glm-5.3'])
        self.assertEqual(summary['chain_environment'], {'CADRE_CHAIN_REVIEW': 'opencode-go/glm-5.3', 'CADRE_CHAIN_QUALITY': 'opencode-go/glm-5.3'})
        self.assertEqual(summary['proposal_total_usd'], '2')
        self.assertFalse((home_dir / 'acceptance-calls.jsonl').exists(), 'no model call during the proposal phase')
        record = json.loads((home_dir / 'runs' / summary['runs'][0] / 'run.json').read_text())
        self.assertNotIn('request_permission', record)
        allocation = json.loads((output.parent / 'acceptance-package-pay.json').read_text())
        self.assertEqual([t['usd'] for t in allocation['allocations']], [2.0])


class SafeCommandTests(unittest.TestCase):
    """CXR-01: product acceptance commands run in the engine's evidence sandbox; external file writes, the parent environment and the network are denied on the same real path."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix='acceptance-scratch-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.wt = self.root / 'wt'
        self.wt.mkdir()
        self.temporary = self.root / 'temporary'
        self.outside = self.root / 'outside.txt'
        self.outside.write_text('synthetic-mark')

    def test_product_code_cannot_write_outside_read_parent_env_or_use_the_network(self):
        script = self.wt / 'product.py'
        script.write_text(f"""import json, os, socket, pathlib
outcome = {{}}
p = pathlib.Path({str(self.outside)!r})
try:
    p.write_text(p.read_text() + '-written'); outcome['outside_write'] = 'allowed'
except OSError as exc:
    outcome['outside_write'] = 'denied:' + type(exc).__name__
outcome['parent_environment'] = os.environ.get('ACCEPTANCE_SYNTHETIC_SECRET')
outcome['cadre_environment'] = sorted(k for k in os.environ if k.startswith('CADRE_'))
try:
    s = socket.create_connection(('127.0.0.1', 9), timeout=1); s.close(); outcome['net'] = 'allowed'
except OSError as exc:
    outcome['net'] = 'denied:' + type(exc).__name__
pathlib.Path('inside.txt').write_text('ok'); outcome['inner_write'] = pathlib.Path('inside.txt').read_text()
print(json.dumps(outcome))
""")
        with patch.dict(os.environ, {'ACCEPTANCE_SYNTHETIC_SECRET': 'synthetic', 'CADRE_RUN_ID': 'must-not-leak'}):
            rc, output = acceptance_package.safe_command(f'{sys.executable} product.py', self.wt, self.temporary, 60)
        self.assertEqual(rc, 0, output)
        data = acceptance_package.json_line(output)
        self.assertIsNotNone(data, output)
        self.assertTrue(data['outside_write'].startswith('denied:'), data)
        self.assertEqual(self.outside.read_text(), 'synthetic-mark')
        self.assertIsNone(data['parent_environment'])
        self.assertEqual(data['cadre_environment'], [])
        self.assertTrue(data['net'].startswith('denied:'), data)
        self.assertEqual(data['inner_write'], 'ok')

    def test_preexisting_external_hardlink_alias_blocks_the_command_and_new_links_are_refused(self):
        """CXR2-01: a pre-existing external inode alias is denied before the command runs (the FIX-25 preflight on the same path); creating a new
        hardlink inside the sandbox fails too; normal worktree writes stay allowed."""
        os.link(self.outside, self.wt / 'alias.txt')
        (self.wt / 'alias.py').write_text("from pathlib import Path\nPath('alias.txt').write_text('written')\n")
        rc, output = acceptance_package.safe_command(f'{sys.executable} alias.py', self.wt, self.temporary, 30)
        self.assertIsNone(rc)
        self.assertIn('hardlink-untrusted', output)
        self.assertEqual(self.outside.read_text(), 'synthetic-mark')
        (self.wt / 'alias.txt').unlink()
        (self.wt / 'link.py').write_text(f"""import json, os
try:
    os.link({str(self.outside)!r}, 'fresh-alias.txt'); print(json.dumps({{'link': 'created'}}))
except OSError as exc:
    print(json.dumps({{'link': 'denied:' + type(exc).__name__}}))
""")
        rc, output = acceptance_package.safe_command(f'{sys.executable} link.py', self.wt, self.temporary, 30)
        self.assertEqual(rc, 0, output)
        self.assertTrue(acceptance_package.json_line(output)['link'].startswith('denied:'), output)
        self.assertFalse((self.wt / 'fresh-alias.txt').exists())
        self.assertEqual(os.stat(self.outside).st_nlink, 1)
        (self.wt / 'normal.py').write_text("from pathlib import Path\nPath('normal.txt').write_text('ok'); print('ok')\n")
        rc, output = acceptance_package.safe_command(f'{sys.executable} normal.py', self.wt, self.temporary, 30)
        self.assertEqual((rc, (self.wt / 'normal.txt').read_text()), (0, 'ok'))
        temporary_alias = self.temporary / 'temporary-alias.txt'
        os.link(self.outside, temporary_alias)
        rc, output = acceptance_package.safe_command(f'{sys.executable} normal.py', self.wt, self.temporary, 30)
        self.assertIsNone(rc, 'an external alias in the temporary area also stops the command')

    def test_unrunnable_or_timed_out_command_is_not_a_pass(self):
        (self.wt / 'infinite.py').write_text('import time\nwhile True: time.sleep(.1)\n')
        rc, output = acceptance_package.safe_command(f'{sys.executable} infinite.py', self.wt, self.temporary, 1)
        self.assertIsNone(rc)
        self.assertIn('time limit exceeded', output)
        self.assertIsNone(acceptance_package.json_line('partial {"a": 1\nlast'))


class PackageShareTests(unittest.TestCase):
    """CXR-02: shared package cap; N/N+1, retry/new process and the R06 two-variant split."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix='acceptance-pay-')
        self.addCleanup(temp.cleanup)
        self.file = Path(temp.name) / 'acceptance-package-pay.json'

    def test_allocations_stop_at_the_common_cap_and_survive_a_new_process(self):
        allocation = acceptance_package.PackageShare(self.file, 2.0)
        for scenario, usd in (('R01', .25), ('R02', .5), ('R03', .25), ('R04', .25), ('R05', .25), ('R06', .125), ('R06', .125), ('R07', .25)):
            allocation.allocation(scenario, scenario + '-' + str(usd), usd)
        self.assertAlmostEqual(allocation.allocation_done(), 2.0)
        self.assertAlmostEqual(allocation.remaining(), 0.0)
        again = acceptance_package.PackageShare(self.file, 2.0)   # a new process does not reset the remaining share
        self.assertAlmostEqual(again.remaining(), 0.0)
        with self.assertRaisesRegex(RuntimeError, 'insufficient shared package allocation'):
            again.allocation('R01', 'R01-repeat', .25)
        self.assertEqual(len(json.loads(self.file.read_text())['allocations']), 8)
        with self.assertRaisesRegex(ValueError, 'does not match'):
            acceptance_package.PackageShare(self.file, 3.0)
        for bad in (0.0, -1.0, float('nan')):
            with self.assertRaises(ValueError):
                acceptance_package.PackageShare(Path(self.file.parent / 'empty.json'), 1.0).allocation('R01', 'x', bad)

    def test_overlapping_allocators_and_processes_cannot_exceed_the_cap(self):
        """CXR2-02: two objects opened beforehand and two separate processes; the ledger is re-read under the lock, the second allocation is denied,
        the first line is not lost; an old memory snapshot cannot allocate."""
        one = acceptance_package.PackageShare(self.file, 2.0)
        two = acceptance_package.PackageShare(self.file, 2.0)
        self.assertEqual(one.allocation('R01', 'first', 1.25), 1.25)
        with self.assertRaisesRegex(RuntimeError, 'insufficient shared package allocation'):
            two.allocation('R02', 'second', 1.25)
        disk = json.loads(self.file.read_text())
        self.assertEqual([(t['label'], t['usd']) for t in disk['allocations']], [('first', 1.25)])
        self.assertEqual(two.allocation('R02', 'second', 0.75), 0.75)
        two.bind('second', 'request-x')
        one.bind('first', 'request-y')
        disk = json.loads(self.file.read_text())
        self.assertEqual([(t['label'], t['usd'], t['rid']) for t in disk['allocations']], [('first', 1.25, 'request-y'), ('second', 0.75, 'request-x')])
        with self.assertRaisesRegex(ValueError, 'does not match'):
            acceptance_package.PackageShare(self.file, 2.5)
        contest = self.file.parent / 'contest.json'
        script = (f"import sys, time; sys.path.insert(0, {str(TESTS)!r}); sys.path.insert(0, {str(TESTS.parent / 'lib')!r}); import acceptance_package as k\n"
                 f"from pathlib import Path\np = k.PackageShare(Path({str(contest)!r}), 2.0)\n"
                 "time.sleep(0.3)\n"
                 "try:\n    print(p.allocation('R', sys.argv[1], 1.25))\nexcept RuntimeError as exc:\n    print('RET')\n")
        procs = [subprocess.Popen([sys.executable, '-c', script, f's{i}'], stdout=subprocess.PIPE, text=True) for i in range(2)]
        outs = sorted(pr.communicate(timeout=60)[0].strip() for pr in procs)
        self.assertEqual(outs, ['1.25', 'RET'], outs)
        last = json.loads(contest.read_text())
        self.assertEqual(len(last['allocations']), 1)
        self.assertLessEqual(sum(t['usd'] for t in last['allocations']), 2.0)

    def test_r06_variants_share_one_family_allocation(self):
        self.assertEqual(acceptance_package.r06_shares(0.25, None), (0.125, 0.125))
        self.assertEqual(acceptance_package.r06_shares(0.25, 0.05), (0.125, 0.125))
        self.assertEqual(acceptance_package.r06_shares(0.25, 0.2), (0.125, 0.05))
        self.assertEqual(acceptance_package.r06_shares(0.25, 0.3), (0.125, 0.0))
        a, b = acceptance_package.r06_shares(0.25, None)
        self.assertLessEqual(a + b, 0.25)


class ReceiptGatesTests(unittest.TestCase):
    """CXR-03: a missing/broken/empty/open/unallowed receipt cannot produce PASS; two-ledger parity."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix='acceptance-receipt-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        (self.home / 'chain').mkdir()
        self.db = self.home / 'chain/request-budget.db'
        self.monthly = self.home / 'chain/monthly-budget.db'

    def line(self, con, ident, provider, reserved, actual, receipt, run_id='run-1', extra=None):
        con.execute('insert into reservation values(?,?,?,?,?,?,?)', (provider, reserved, actual, 1.0,
                    json.dumps({'run_id': run_id, **(extra or {})}), receipt, ident))

    @contextmanager
    def ledger(self, path=None, schema=None):
        con = sqlite3.connect(path or self.db)
        try:
            con.execute(schema or 'create table if not exists reservation (provider text, reserved integer, actual integer, created real, metadata text, receipt text, id text)')
            yield con
            con.commit()
        finally:
            con.close()

    def test_missing_unreadable_and_empty_ledgers_block_instead_of_passing(self):
        absent = acceptance_package.receipt_gates(self.home, ['run-1'], 'opencode-go/')
        self.assertEqual((absent['status'], absent['readable']), ('BLOCKED', False))
        with self.ledger(schema='create table wrong_schema (id text)'):
            pass
        broken = acceptance_package.receipt_gates(self.home, ['run-1'], 'opencode-go/')
        self.assertEqual(broken['status'], 'BLOCKED')
        self.assertIn('unreadable', broken['error'])
        self.db.unlink()
        with self.ledger() as con:
            self.line(con, 'r9', 'opencode-go', 1000, 500, 'r9:x', run_id='other-run')
        empty = acceptance_package.receipt_gates(self.home, ['run-1'], 'opencode-go/')
        self.assertEqual((empty['status'], empty['line']), ('BLOCKED', 0))
        self.assertIn('zero record', empty['reason'])
        self.assertEqual(acceptance_package.receipt_gates(self.home, [], 'opencode-go/')['status'], 'BLOCKED')

    def test_open_reservation_foreign_provider_and_guarantee_fail_while_closed_go_rows_pass(self):
        with self.ledger() as con:
            self.line(con, 'r1', 'opencode-go#glm-5.3', 2000, 1500, 'r1:ok')
        self.assertEqual(acceptance_package.receipt_gates(self.home, ['run-1'], 'opencode-go/')['status'], 'PASS')
        with self.ledger() as con:
            self.line(con, 'r2', 'opencode-go', 3000, None, None)
        open = acceptance_package.receipt_gates(self.home, ['run-1'], 'opencode-go/')
        self.assertEqual((open['status'], open['pending']), ('FAIL', 1))
        with self.ledger() as con:
            con.execute("update reservation set actual=0, receipt='unused:r2' where id='r2'")
            self.line(con, 'r3', 'openrouter#z-ai/glm-5.3', 900, 900, 'r3:ok')
        foreign = acceptance_package.receipt_gates(self.home, ['run-1'], 'opencode-go/')
        self.assertEqual(foreign['status'], 'FAIL')
        self.assertIn('openrouter#z-ai/glm-5.3', foreign['unpermitted'])
        with self.ledger() as con:
            con.execute("delete from reservation where id='r3'")
            self.line(con, 'r4', 'opencode-go', 800, None, None, extra={'assurance': True})
        assurance = acceptance_package.receipt_gates(self.home, ['run-1'], 'opencode-go/')
        self.assertEqual((assurance['status'], assurance['open_assurance']), ('FAIL', 1))
        with self.ledger() as con:
            con.execute("update reservation set actual=0, receipt='claimed:r4' where id='r4'")
        self.assertEqual(acceptance_package.receipt_gates(self.home, ['run-1'], 'opencode-go/')['status'], 'PASS')
        with self.ledger() as con:
            con.execute("delete from reservation where id='r1'")
        only_assurance = acceptance_package.receipt_gates(self.home, ['run-1'], 'opencode-go/')
        self.assertEqual(only_assurance['status'], 'BLOCKED')
        self.assertIn('closed receipt', only_assurance['reason'])

    def test_cash_rows_must_match_the_monthly_ledger_and_go_only_runs_must_not_have_monthly_rows(self):
        with self.ledger() as con:
            self.line(con, 'c1', 'openrouter#z-ai/glm-5.3', 700, 650, 'c1:ok')
        missing = acceptance_package.receipt_gates(self.home, ['run-1'], 'openrouter/')
        self.assertEqual(missing['status'], 'BLOCKED')
        self.assertIn('monthly ledger absent', missing['reason'])
        with self.ledger(self.monthly, 'create table reservation(id text primary key, provider text, period text, created real, reserved integer, actual integer, receipt text unique, metadata text)') as con:
            con.execute('insert into reservation values(?,?,?,?,?,?,?,?)', ('m1', 'openrouter', '2026-10', 1.0, 700, 600, 'c1:ok', json.dumps({'run_id': 'run-1'})))
        mismatch = acceptance_package.receipt_gates(self.home, ['run-1'], 'openrouter/')
        self.assertEqual(mismatch['status'], 'FAIL')
        self.assertIn('two ledgers mismatch', mismatch['reason'])
        with self.ledger(self.monthly, "update reservation set actual=650 where id='m1'"):
            pass
        self.assertEqual(acceptance_package.receipt_gates(self.home, ['run-1'], 'openrouter/')['status'], 'PASS')
        with self.ledger() as con:
            con.execute("delete from reservation where id='c1'")
            self.line(con, 'g1', 'opencode-go', 500, 400, 'g1:ok')
        go_only = acceptance_package.receipt_gates(self.home, ['run-1'], 'opencode-go/')
        self.assertEqual(go_only['status'], 'FAIL')
        self.assertIn('monthly ledger has entries for this family without a cash call', go_only['reason'])

    def test_monthly_rows_link_through_monthly_id_and_session_like_the_live_plugin_writes_them(self):
        with self.ledger() as con:
            self.line(con, 'c1', 'openrouter#z-ai/glm-5.3', 700, 650, 'c1:gen-1', extra={'monthly_id': 'm1', 'session_id': 'ses_a'})
            self.line(con, 'c2', 'openrouter#z-ai/glm-5.3', 500, 100, 'c2:gen-2', extra={'monthly_id': 'm2', 'session_id': 'ses_b'})
        schema = ('create table reservation(id text primary key, provider text, period text, created real, reserved integer, actual integer, '
                'receipt text unique, metadata text)')
        with self.ledger(self.monthly, schema) as con:
            for ident, actual, receipt, session in (('m1', 650, 'm1:gen-1', 'ses_a'), ('m2', 100, 'm2:gen-2', 'ses_b'),
                                                   ('m3', 0, 'm3:provider-budget-denied', 'ses_b'), ('m9', 5000, 'm9:gen-9', 'ses_other')):
                con.execute('insert into reservation values(?,?,?,?,?,?,?,?)', (ident, 'openrouter', '2026-10', 1.0, 900, actual, receipt,
                            json.dumps({'model': 'z-ai/glm-5.3', 'session_id': session, 'role': 'build'})))
        outcome = acceptance_package.receipt_gates(self.home, ['run-1'], 'openrouter/')
        self.assertEqual((outcome['status'], outcome['monthly']['line'], outcome['monthly']['real_micro']), ('PASS', 3, 750),
                         'monthly rows are linked to the family by monthly_id and session; other sessions do not count')
        with self.ledger(self.monthly, "update reservation set actual=null, receipt=null where id='m3'"):
            pass
        open = acceptance_package.receipt_gates(self.home, ['run-1'], 'openrouter/')
        self.assertEqual((open['status'], open['monthly']['open']), ('FAIL', 1), 'an open monthly reservation in a family session does not prove closure')
        with self.ledger(self.monthly, "update reservation set actual=0, receipt='m3:provider-budget-denied' where id='m3'"):
            pass
        with self.ledger(self.monthly, "update reservation set actual=600 where id='m1'"):
            pass
        mismatch = acceptance_package.receipt_gates(self.home, ['run-1'], 'openrouter/')
        self.assertEqual(mismatch['status'], 'FAIL')
        self.assertIn('two ledgers mismatch', mismatch['reason'])


class LiabilityRefundTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix='acceptance-refund-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / 'home_dir'
        (self.home / 'chain').mkdir(parents=True)
        (self.home / 'runs').mkdir()
        self.file = self.root / 'evidence' / 'acceptance-package-pay.json'
        self.db = self.home / 'chain/request-budget.db'
        with closing(sqlite3.connect(self.db)) as con:
            con.execute('create table reservation (provider text, reserved integer, actual integer, created real, metadata text, receipt text, id text)')
            con.commit()

    def execute_run(self, rid: str, status: str = 'completed', **extra) -> None:
        path = self.home / 'runs' / rid
        path.mkdir(parents=True, exist_ok=True)
        now_ts = time.time()
        (path / 'run.json').write_text(json.dumps({'version': 1, 'run_id': rid, 'project': 'acceptance-refund', 'state': status, 'tasks': {},
                                                  'created': now_ts, 'updated': now_ts, **extra}))

    def receipt(self, ident: str, rid: str, reserved: int, real: int | None, provider: str = 'opencode-go#glm-5.3',
               receipt: str | None = None, extra: dict | None = None) -> None:
        with closing(sqlite3.connect(self.db)) as con:
            con.execute('insert into reservation values(?,?,?,?,?,?,?)', (provider, reserved, real, 1.0, json.dumps({'run_id': rid, **(extra or {})}),
                        receipt if receipt is not None else (f'{ident}:ok' if real is not None else None), ident))
            con.commit()

    def allocation(self, cap: float = 5.0) -> acceptance_package.PackageShare:
        return acceptance_package.PackageShare(self.file, cap)

    def audit(self, allocation: acceptance_package.PackageShare) -> list[dict]:
        return allocation.refund_audit(self.home, 'opencode-go/')

    def test_closed_terminal_runs_release_only_their_unused_share_once_and_survive_a_new_process(self):
        allocation = self.allocation()
        allocation.allocation('R01', 'r01', 2.10)
        allocation.bind('r01', 'request-a')
        allocation.allocation('R02', 'r02', 2.10)
        allocation.bind('r02', 'request-b')
        with self.assertRaisesRegex(RuntimeError, 'insufficient shared package allocation'):
            allocation.allocation('R03', 'r03', 2.10)
        self.execute_run('request-a')
        self.receipt('m1', 'request-a', 200_180, 150_000)
        self.receipt('m2', 'request-a', 104_481, 150_000, provider='opencode-go#deepseek-v4-pro')
        self.execute_run('request-b', 'running')
        self.receipt('m3', 'request-b', 300_000, 120_000)
        events = self.audit(self.allocation())
        self.assertEqual([(o['rid'], o['outcome']) for o in events], [('request-a', 'chargeback'), ('request-b', 'waiting')])
        self.assertIn('not terminal', events[1]['reason'])
        fresh = self.allocation()
        self.assertAlmostEqual(fresh.remaining(), 2.60)
        self.assertAlmostEqual(fresh.brut(), 4.20)
        self.assertEqual(fresh.allocation('R03', 'r03', 2.10), 2.10)
        self.assertAlmostEqual(fresh.remaining(), 0.50)
        self.assertNotIn('chargeback', [o['outcome'] for o in self.audit(fresh) if o['rid'] == 'request-a'])
        self.assertAlmostEqual(self.allocation().remaining(), 0.50, msg='the same allocation is not refunded twice')
        self.execute_run('request-b', 'cancelled')
        self.assertIn(('request-b', 'chargeback'), [(o['rid'], o['outcome']) for o in self.audit(self.allocation())])
        self.assertAlmostEqual(self.allocation().remaining(), 2.48)
        a = next(t for t in json.loads(self.file.read_text())['allocations'] if t['rid'] == 'request-a')
        self.assertEqual((a['usd'], a['closing']['liability_usd'], a['closing']['refund_usd'], a['closing']['runs']),
                         (2.1, 0.3, 1.8, ['request-a']))

    def test_held_guarantee_foreign_unknown_missing_and_conflicting_receipts_keep_the_full_share(self):
        allocation = self.allocation(20.0)
        cases = {
            'request-pending': [('k1', 300_000, None, 'opencode-go#glm-5.3', None, None)],
            'request-assurance': [('k2', 100_000, 50_000, 'opencode-go#glm-5.3', None, None), ('k3', 400_000, None, 'opencode-go', None, {'assurance': True})],
            'request-foreign': [('k4', 100_000, 90_000, 'openrouter#z-ai/glm-5.3', None, None)],
            'request-unknown': [('k5', 100_000, 90_000, 'mystery#x', None, None)],
            'request-empty': [],
            'request-contradiction': [('k7', 100_000, 90_000, 'opencode-go#glm-5.3', 'other:k7', None)],
        }
        for rid, lines in cases.items():
            allocation.allocation('R', rid, 2.10)
            allocation.bind(rid, rid)
            self.execute_run(rid)
            for ident, reserved, real, provider, receipt, extra in lines:
                self.receipt(ident, rid, reserved, real, provider, receipt, extra)
        once = self.allocation(20.0).remaining()
        events = {o['rid']: o for o in self.audit(self.allocation(20.0))}
        self.assertEqual({r: o['outcome'] for r, o in events.items()}, dict.fromkeys(cases, 'waiting'))
        self.assertTrue(all('receipt' in o['reason'] for o in events.values()), events)
        self.assertAlmostEqual(self.allocation(20.0).remaining(), once)
        self.assertFalse(any('closing' in t for t in json.loads(self.file.read_text())['allocations']))

    def test_verified_obligation_above_the_share_is_charged_not_released(self):
        allocation = self.allocation()
        allocation.allocation('R01', 'r01', 2.10)
        allocation.bind('r01', 'request-overrun')
        self.execute_run('request-overrun')
        self.receipt('m1', 'request-overrun', 3_000_000, 2_500_000)
        events = self.audit(allocation)
        self.assertEqual([o['outcome'] for o in events], ['overrun'])
        self.assertAlmostEqual(self.allocation().remaining(), 2.50)
        self.assertEqual([o['outcome'] for o in self.audit(self.allocation())], ['closed'])
        self.assertAlmostEqual(self.allocation().remaining(), 2.50)

    def test_active_owner_live_attempt_or_unfinished_child_blocks_release_until_they_end(self):
        from run_store import process_identity
        allocation = self.allocation()
        for rid in ('request-owner', 'request-attempt', 'request-parent'):
            allocation.allocation('R', rid, 1.0)
            allocation.bind(rid, rid)
            self.execute_run(rid)
            self.receipt('m-' + rid, rid, 100_000, 50_000)
        holder = subprocess.Popen([sys.executable, '-c', f"import sys, time; sys.path.insert(0, {str(TESTS.parent / 'lib')!r}); from pathlib import Path; "
                                   f"from run_store import FileLock; lock = FileLock(Path({str(self.home / 'runs/request-owner/.owner.lock')!r})); "
                                   "lock.acquire(); print('ok', flush=True); time.sleep(60)"], stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(lambda: holder.poll() is None and holder.kill())
        self.assertEqual(holder.stdout.readline().strip(), 'ok')
        worker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(lambda: worker.poll() is None and worker.kill())
        id = None
        while id is None:
            id = process_identity(worker.pid)
        attempt = self.home / 'runs/request-attempt/tasks/g1/attempts/a1'
        attempt.mkdir(parents=True)
        (attempt / 'process.json').write_text(json.dumps({'run_id': 'request-attempt', 'task_id': 'g1', 'attempt_id': 'a1', 'process_identity': id}))
        self.execute_run('request-parent', project_run_ids=['request-child'])
        self.execute_run('request-child', 'running')
        events = {o['rid']: o for o in self.audit(allocation)}
        self.assertEqual({r: o['outcome'] for r, o in events.items()}, dict.fromkeys(('request-owner', 'request-attempt', 'request-parent'), 'waiting'))
        self.assertIn('owner lock', events['request-owner']['reason'])
        self.assertIn('live', events['request-attempt']['reason'])
        self.assertIn('request-child', events['request-parent']['reason'])
        holder.kill()
        holder.communicate()
        worker.kill()
        worker.wait()
        self.execute_run('request-child')
        self.assertEqual({o['rid']: o['outcome'] for o in self.audit(allocation)}, dict.fromkeys(('request-owner', 'request-attempt', 'request-parent'), 'chargeback'))
        self.assertAlmostEqual(self.allocation().remaining(), 5.0 - 0.15)

    def test_late_receipt_raises_the_charge_and_an_unverifiable_closed_share_returns_in_full_without_second_refund(self):
        allocation = self.allocation()
        allocation.allocation('R01', 'r01', 2.10)
        allocation.bind('r01', 'request-a')
        self.execute_run('request-a')
        self.receipt('m1', 'request-a', 400_000, 300_000)
        self.assertEqual([o['outcome'] for o in self.audit(allocation)], ['chargeback'])
        self.assertAlmostEqual(self.allocation().remaining(), 4.70)
        self.receipt('m2', 'request-a', 200_000, 100_000)
        self.assertEqual([o['outcome'] for o in self.audit(self.allocation())], ['late_receipt'])
        self.assertAlmostEqual(self.allocation().remaining(), 4.60)
        self.receipt('m3', 'request-a', 500_000, None)
        self.assertEqual([o['outcome'] for o in self.audit(self.allocation())], ['go_back_taken'])
        self.assertAlmostEqual(self.allocation().remaining(), 2.90)
        with closing(sqlite3.connect(self.db)) as con:
            con.execute("update reservation set actual=0, receipt='unused:m3' where id='m3'")
            con.commit()
        self.assertEqual([o['outcome'] for o in self.audit(self.allocation())], ['closed'])
        self.assertAlmostEqual(self.allocation().remaining(), 2.90, msg='a refunded allocation is not refunded a second time')

    def test_two_objects_and_two_processes_cannot_refund_the_same_share_twice(self):
        one, two = self.allocation(), self.allocation()
        one.allocation('R01', 'r01', 2.10)
        one.bind('r01', 'request-a')
        self.execute_run('request-a')
        self.receipt('m1', 'request-a', 400_000, 300_000)
        self.assertEqual([o['outcome'] for o in self.audit(one)] + [o['outcome'] for o in self.audit(two)], ['chargeback', 'closed'])
        self.assertAlmostEqual(self.allocation().remaining(), 4.70)
        self.allocation().allocation('R02', 'r02', 2.10)
        self.allocation().bind('r02', 'request-b')
        self.execute_run('request-b')
        self.receipt('m2', 'request-b', 400_000, 300_000)
        script = (f"import sys, time; sys.path.insert(0, {str(TESTS)!r}); sys.path.insert(0, {str(TESTS.parent / 'lib')!r}); import acceptance_package as k\n"
                 f"from pathlib import Path\np = k.PackageShare(Path({str(self.file)!r}), 5.0)\ntime.sleep(0.3)\n"
                 f"print(','.join(o['outcome'] for o in p.refund_audit(Path({str(self.home)!r}), 'opencode-go/')))\n")
        processes = [subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        outputs = sorted(s.communicate(timeout=60)[0].strip() for s in processes)
        self.assertEqual(outputs, ['closed,chargeback', 'closed,closed'], outputs)
        self.assertAlmostEqual(self.allocation().remaining(), 4.40)

    def test_boundary_after_refund_and_an_unbound_allocation_stays_reserved(self):
        allocation = self.allocation(4.20)
        allocation.allocation('R01', 'r01', 2.10)
        allocation.bind('r01', 'request-a')
        allocation.allocation('R02', 'r02', 2.10)
        self.execute_run('request-a')
        self.receipt('m1', 'request-a', 100_000, 0)
        events = self.audit(allocation)
        self.assertEqual([o['outcome'] for o in events], ['chargeback', 'waiting'])
        self.assertIn('run id absent', events[1]['reason'])
        self.assertAlmostEqual(self.allocation(4.20).remaining(), 2.10)
        self.assertEqual(self.allocation(4.20).allocation('R03', 'r03', 2.10), 2.10)
        with self.assertRaisesRegex(RuntimeError, 'insufficient shared package allocation'):
            self.allocation(4.20).allocation('R04', 'r04', 0.000001)

    def test_corrupt_refund_records_are_rejected(self):
        allocation = self.allocation()
        allocation.allocation('R01', 'r01', 2.10)
        allocation.bind('r01', 'request-a')
        data = json.loads(self.file.read_text())
        for bad in ({'liability_usd': -0.1, 'runs': ['request-a']}, {'liability_usd': float('nan'), 'runs': ['request-a']},
                     {'refund_usd': 1.0}, 'x'):
            data['allocations'][0]['closing'] = bad
            self.file.write_text(json.dumps(data))
            with self.subTest(closing=str(bad)), self.assertRaisesRegex(ValueError, 'broken'):
                self.allocation()


class ChainEnvironmentTests(unittest.TestCase):
    """CXR-04: only CADRE_CHAIN_* is carried; a chain outside the allowed provider is denied in live preparation."""

    def test_only_chain_variables_are_forwarded_and_foreign_models_are_listed(self):
        env = {'CADRE_CHAIN_PLAN': 'opencode-go/glm-5.3', 'CADRE_CHAIN_REVIEW': 'opencode-go/glm-5.3,opencode/glm-5.3', 'CADRE_HOME': '/x',
               'CADRE_CHAIN_EMPTY': '', 'OPENCODE_CONFIG_CONTENT': '{}'}
        chain = acceptance_package.select_chain_environment(env)
        self.assertEqual(sorted(chain), ['CADRE_CHAIN_PLAN', 'CADRE_CHAIN_REVIEW'])
        self.assertEqual(acceptance_package.chain_verify(chain, 'opencode-go/'), ['opencode/glm-5.3'])
        self.assertEqual(acceptance_package.chain_verify({'CADRE_CHAIN_PLAN': 'opencode-go/glm-5.3'}, 'opencode-go/'), [])

    def test_live_preparation_installs_the_guard_hooks_the_engine_requires_before_editing(self):
        package = object.__new__(acceptance_package.Package)
        package.path, package.chain = 'or', {}
        package.directory = Path(tempfile.mkdtemp(prefix='acceptance-live-', dir=acceptance_package.HOME))
        for name in ('pre-push', 'pre-commit', 'commit-msg'):
            (acceptance_package.HOME / 'hooks' / name).unlink(missing_ok=True)
        acceptance_package.Package.live_prepare(package)
        for name in ('pre-push', 'pre-commit', 'commit-msg'):
            hook = acceptance_package.HOME / 'hooks' / name
            self.assertTrue(hook.is_file() and os.access(hook, os.X_OK), name)
            self.assertEqual(hook.read_bytes(), (acceptance_package.ENGINE / 'hooks' / name).read_bytes())

    def test_live_receipt_gates_first_reconcile_cut_stream_reservations_through_the_engine(self):
        package = object.__new__(acceptance_package.Package)
        package.root, package.wrapper, package.env_extra, package.chain, package.events = Path('/runner'), Path('/wrapper'), {}, {}, []
        with patch.object(acceptance_package.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '2\n', '')) as execute:
            package.path = 'fake'
            package.pending_close()
            self.assertFalse(execute.called, 'no provider reconciliation in fake mode')
            package.path = 'or'
            package.pending_close()
        command = execute.call_args.args[0]
        self.assertIn('or_pending_close', command[2])
        self.assertEqual(command[3], '/runner/lib')
        self.assertIn('2 rows', package.events[-1])

    def test_operator_chain_overrides_the_live_default_chain_for_the_same_roles(self):
        import live_scenario
        package = object.__new__(acceptance_package.Package)
        package.root, package.wrapper = Path('/runner'), Path('/wrapper')
        package.env_extra = dict(live_scenario.chain_environment('or', 'cheap'))
        package.chain = {'CADRE_CHAIN_IMPL': 'openrouter/deepseek/deepseek-v4-pro,openrouter/z-ai/glm-5.3',
                        'CADRE_CHAIN_REVIEW': 'openrouter/z-ai/glm-5.3,openrouter/deepseek/deepseek-v4-pro'}
        env = package.environment()
        for role in ('CADRE_CHAIN_IMPL', 'CADRE_CHAIN_REVIEW'):
            self.assertEqual(env[role], package.chain[role], 'the operator chain overrides the fallback chain for the same role')
        self.assertEqual(env['CADRE_CHAIN_PLAN'], package.env_extra['CADRE_CHAIN_PLAN'], 'roles not specified by the operator retain their defaults')
        self.assertEqual((env['CADRE_REAL_OPENCODE'], env['CADRE_ENGINE_ROOT']), ('/wrapper', '/runner'))


STANCE_RUN = '''import os, shutil, sys
from pathlib import Path
ROOT, MODE = Path(sys.argv[1]), sys.argv[2]
sys.path.insert(0, {tests!r})
import acceptance_package as kp
SLOW = """#!{{py}}
import os, sys, time
if '[QE-TASK v1]' in (sys.argv[-1] if len(sys.argv) > 1 else ''):
    open({{pidf!r}}, 'w').write(str(os.getpid()))
    time.sleep(300)
    raise SystemExit(0)
os.execv({{py!r}}, [{{py!r}}, {{real!r}}, *sys.argv[1:]])
"""
prepare = kp.Package.fake_prepare
def slow_prepare(self):
    prepare(self)
    real = kp.HOME / 'acceptance-fake-real.py'
    shutil.copyfile(self.wrapper, real)
    self.wrapper.write_text(SLOW.format(py=sys.executable, pidf=str(ROOT / 'worker.pid'), real=str(real)))
kp.Package.fake_prepare = slow_prepare
if MODE == 'duration':
    duration = kp.Package.duration_remained
    kp.Package.duration_remained = lambda self: 0.0 if (ROOT / 'worker.pid').exists() else duration(self)
sys.argv = ['acceptance_package.py', 'R01', '--fixture-dir', str(ROOT / 'fixtures'), '--path', 'fake', '--minute', '5', '--cap', '2',
            '--output', str(ROOT / 'evidence')]
raise SystemExit(kp.main())
'''


class StancePathTests(unittest.TestCase):
    def root_prepare(self, entry_name: str) -> Path:
        temp = tempfile.TemporaryDirectory(prefix=f'acceptance-stance-{entry_name}-')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        fixture_tree(root)
        self.addCleanup(self.processes_sum, root)
        return root

    @staticmethod
    def remaining_processes(root: Path) -> list[tuple[int, str]]:
        output = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True, timeout=30).stdout
        outcome = []
        for line in output.splitlines():
            pid, _, command = line.strip().partition(' ')
            if str(root) in command and int(pid) != os.getpid():
                outcome.append((int(pid), command))
        return outcome

    def processes_sum(self, root: Path) -> None:
        for pid, _ in self.remaining_processes(root):
            try:
                os.kill(pid, 9)
            except OSError:
                pass

    def execute_run(self, root: Path, mode: str, signal: int | None) -> tuple[int, dict | None, dict]:
        harness = root / 'harness.py'
        harness.write_text(STANCE_RUN.format(tests=str(TESTS)))
        env = {k: v for k, v in os.environ.items() if not k.startswith(('CADRE_', 'OPENCODE_', 'E2E_'))}
        env.update(CADRE_HOME=str(root / 'home_dir'), CADRE_ENGINE_ROOT=str(TESTS.parent))
        driver = subprocess.Popen([sys.executable, str(harness), str(root), mode], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: driver.poll() is None and driver.kill())
        last = time.monotonic() + 120
        while not (root / 'worker.pid').exists() and driver.poll() is None and time.monotonic() < last:
            time.sleep(0.2)
        self.assertTrue((root / 'worker.pid').exists(), 'slow worker did not start')
        if signal is not None:
            driver.send_signal(signal)
        try:
            output, _ = driver.communicate(timeout=240)
        except subprocess.TimeoutExpired:
            driver.kill()
            output, _ = driver.communicate()
            self.fail('driver did not stop: ' + output[-1500:])
        summary = json.loads((root / 'evidence/summary.json').read_text()) if (root / 'evidence/summary.json').exists() else None
        runs = sorted((root / 'home_dir/runs').glob('request-*/run.json'))
        record = json.loads(runs[0].read_text()) if runs else {}
        worker = int((root / 'worker.pid').read_text())
        last = time.monotonic() + 15
        while (self.remaining_processes(root) or self.live(worker)) and time.monotonic() < last:
            time.sleep(0.2)
        self.assertFalse(self.live(worker), 'executor (worker) left orphaned: ' + output[-1500:])
        self.assertEqual(self.remaining_processes(root), [], 'engine process remains after the driver exits')
        return driver.returncode, summary, record

    @staticmethod
    def live(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def test_operator_interrupt_cancels_the_run_through_the_engine_and_leaves_no_process(self):
        for indicator in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=indicator.name):
                root = self.root_prepare(indicator.name.lower())
                rc, summary, record = self.execute_run(root, 'cutting', indicator)
                self.assertEqual(rc, 130)
                self.assertIsNotNone(summary, 'summary is written even after interruption')
                self.assertEqual(summary['decision'], 'STOPPED')
                self.assertEqual(record.get('state'), 'cancelled')
                self.assertIn('cancel', {m.get('kind') for m in record.get('operator_interventions') or []})

    def test_unresponsive_coordinator_is_killed_and_its_recorded_phase_process_is_reclaimed(self):
        root = self.root_prepare('deaf')
        home_dir, rid = root / 'home_dir', 'request-deaf'
        (home_dir / 'runs' / rid).mkdir(parents=True)
        now_ts = time.time()
        (home_dir / 'runs' / rid / 'run.json').write_text(json.dumps({'version': 1, 'run_id': rid, 'project': 'acceptance-deaf', 'state': 'running',
                                                               'tasks': {}, 'created': now_ts, 'updated': now_ts}))
        (root / 'root/lib').mkdir(parents=True)
        (root / 'root/lib/run_store.py').write_text(f'''import json, os, signal, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, {str(TESTS.parent / 'lib')!r})
from run_store import process_identity
command, rid = sys.argv[1], sys.argv[2]
run = Path(os.environ['CADRE_HOME']) / 'runs' / rid
if command == 'run':
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signal_number, signal.SIG_IGN)
    phase = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)', str(run)], start_new_session=True)
    while process_identity(phase.pid) is None:
        time.sleep(0.05)
    record = json.loads((run / 'run.json').read_text())
    record.update(phase_process=process_identity(phase.pid), owner_pid=os.getpid())
    (run / 'run.json').write_text(json.dumps(record))
    (run / 'phase.pid').write_text(str(phase.pid))
    time.sleep(300)
elif command == 'cancel':
    (run / 'cancel.flag').write_text('requested\\n')
    print(rid)
''')
        p = acceptance_package.Package.__new__(acceptance_package.Package)
        p.minute, p.root, p.directory, p.events, p.runs = 5, root / 'root', root / 'evidence', [], [rid]
        p.wrapper, p.env_extra, p.chain, p.STANCE_WAIT_SEC = root / 'absent', {}, {}, 1.0
        p.directory.mkdir()
        with patch.object(acceptance_package, 'HOME', home_dir):
            p.last_monotonic = time.monotonic() + 3
            p.execute(rid)
        self.assertTrue((home_dir / 'runs' / rid / 'cancel.flag').exists(), 'the engine cancel interface is tried first')
        self.assertTrue(any('SIGKILL' in o for o in p.events), p.events)
        phase = int((home_dir / 'runs' / rid / 'phase.pid').read_text())
        last = time.monotonic() + 10
        while self.live(phase) and time.monotonic() < last:
            time.sleep(0.1)
        self.assertFalse(self.live(phase), 'the recorded phase process is reaped through the engine’s reclaim_orphans path')

    def test_deadline_stops_the_run_through_the_engine_and_finalizes_it(self):
        root = self.root_prepare('duration')
        rc, summary, record = self.execute_run(root, 'duration', None)
        self.assertNotEqual(rc, 0)
        self.assertIsNotNone(summary)
        self.assertNotIn(summary['decision'], ('PASS', 'LOCAL_PASS'))
        self.assertEqual(record.get('state'), 'cancelled', 'the engine cancel path closes an expired run; it does not remain running')
        self.assertIn('cancel', {m.get('kind') for m in record.get('operator_interventions') or []})


if __name__ == '__main__':
    unittest.main()
