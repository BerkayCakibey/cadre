#!/usr/bin/env python3
"""P7 — isolated MARATHON. Fake executor + fake budget; the REAL cadre.sh/run_store.py/qe_core/
health-watch.sh/capacity-watch.sh/run_ledger.py/rotation.py, isolated home + isolated code root + a sabotaged fake HOME + argv shims
(test_night_lifetime.IsolatedNight). Usage:
    python3 engine/tests/marathon.py --hour 8 --report reports/p7-marathon-<date>.json --doc docs/MARATHON-<date>.md
Thresholds are written here BEFORE THE RUN (THRESHOLDS); a measurement without a threshold is narrative, not acceptance. Injected failures:
guard SIGKILL, coordinator SIGKILL, QA red (retryable -> 2 attempts -> failed), disk threshold, a second task in the same project
(queue). The report is rewritten every round."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'lib'))
import test_night_lifetime as lifetime  # noqa: E402
import run_store  # noqa: E402
from run_store import RunStore  # noqa: E402

# --- THRESHOLDS WRITTEN BEFORE THE RUN (acceptance criteria) ---------------------------------------------------------------
THRESHOLDS = {
    'coordinator_rss_mb_max': 200,          # peak RSS of any coordinator process (python run_store.py execute)
    'guard_rss_mb_max': 60,                 # RSS of the health-watch.sh bash process
    'growth_mb_hour_max': 25,               # hourly growth of the isolated home (logs+runs+worktrees+reports)
    'ledger_scan_sec_max': 20,               # run_ledger.py scan duration (last measurement)
    'completed_hour_min': 10,              # minimum completed runs per hour (normal scenario)
    'fake_cloud_cost_usd': 0.0,         # total settled USD in the ledger (request-budget.db) == 0
    'guard_revival_sec_max': 120,            # guard SIGKILL -> a new live pidfile (CADRE_GUARD_CHECK=60 + tick)
    'coordinator_revival_sec_max': 240,      # coordinator SIGKILL -> run COMPLETED (guard tick 60 s + continue from the recorded plan)
    'retry_count': 2,                      # a QA-red run is retried exactly 2 times, then failed
    'disk_low_executor_call': 0,           # executor Popen count below the disk threshold
    'same_project_concurrent_coordinator_max': 1,
    'live_root_leak': 0,               # no LIVE-LEAK file, no lib/bin path outside the isolated root in argv
    # B62 (T1 audit): the threshold enforced the opposite of DECISION 8 (it counted escalation as "PASSED"). Decision 8: local model
}
SCENARIOS = ['normal', 'normal', 'guard_kill', 'normal', 'qa_red', 'normal', 'disk_low', 'queue', 'normal',
              'coordinator_kill', 'normal']
SCENARIO_ORDER = list(SCENARIOS)  # can be changed with --scenarios (smoke test)


def sampled(kinds, scenario, condition):
    """B63 (T1 audit): the old thresholds were `all(... for t in kinds if t['scenario']=='X')`; if the scenario never ran, `all([])`
    returned True and the threshold wrote PASSED by itself. Now at least one sample is required."""
    samples = [t for t in kinds if t['scenario'] == scenario]
    return bool(samples) and all(condition(t) for t in samples)


class Marathon:
    def __init__(self, hour: float, report: Path, doc: Path):
        self.hour, self.report, self.doc = hour, report, doc
        self.f = lifetime.IsolatedNight()
        self.f.setUp()
        self.f.env.update(CADRE_WATCH_INTERVAL='60', CADRE_GUARD_CHECK='60', CADRE_COORD_TICK='0.5', CADRE_QUOTA_INTERVAL='300')
        self.home, self.root, self.store = self.f.home, self.f.root, self.f.store
        self.t0 = time.time()
        self.measurement: dict = {'start': time.strftime('%F %T'), 'hour_target': hour, 'kinds': [], 'events': [],
                            'rss_coordinator_max_mb': 0, 'rss_guard_max_mb': 0, 'ledger_scan_sec': [], 'size_mb': [],
                            'guard_revival_sec': [], 'coordinator_revival_sec': [], 'concurrent_max': 0, 'rotation': []}
        self.task = 0
        self.first_size = None
        import threading
        self.stop = threading.Event()
        threading.Thread(target=self._concurrent_watch, daemon=True).start()

    def _concurrent_watch(self):
        while not self.stop.is_set():
            try:
                self.measurement['concurrent_max'] = max(self.measurement['concurrent_max'], len(run_store.active_runs(self.store)))
            except (OSError, ValueError):
                pass
            time.sleep(.2)

    # --- helpers -------------------------------------------------------------------------------------------
    def log(self, msg):
        line = f"{time.strftime('%F %T')} {msg}"
        print(line, flush=True)
        self.measurement['events'].append(line)

    def submit(self, extra=None, text='Create a.py and b.py, verify that both compile and pass the test'):
        self.task += 1
        result = subprocess.run(['bash', str(self.root / 'bin/cadre.sh'), 'task'], input=f'{text} #{self.task}\ngo\n',
                                cwd=self.f.repo, env={**self.f.env, **(extra or {})}, text=True, capture_output=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(f'cadre.sh task rc={result.returncode}: {result.stderr[-500:]}')
        return self.store.list('e2e')[-1]['run_id'], result.stdout

    def wait_terminal(self, rid, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = self.store.read(rid)
            if record['state'] in ('completed', 'cancelled', 'failed'):
                return record
            self.sample()
            time.sleep(1)
        return self.store.read(rid)

    def wait_for(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(.5)
        return False

    def guard_pid(self):
        return run_store.guard_pid(self.home, 'health-watch')

    def rss_mb(self, pattern):
        out = subprocess.run(['bash', '-c', f"pgrep -f '{pattern}' | xargs -I{{}} ps -o rss= -p {{}} 2>/dev/null"], capture_output=True, text=True).stdout
        values = [int(v) for v in out.split() if v.isdigit()]
        return max(values) / 1024 if values else 0.0

    def size_mb(self):
        total = 0
        for sub in ('logs', 'runs', 'worktrees', 'reports', 'archives', 'chain'):
            path = self.home / sub
            if path.exists():
                total += sum(p.stat().st_size for p in path.rglob('*') if p.is_file())
        return total / 1_048_576

    def sample(self):
        self.measurement['rss_coordinator_max_mb'] = max(self.measurement['rss_coordinator_max_mb'], self.rss_mb(f'{self.root}/lib/run_store.py execute'))
        self.measurement['rss_guard_max_mb'] = max(self.measurement['rss_guard_max_mb'], self.rss_mb(f'{self.root}/bin/health-watch.sh'))
        # concurrent coordinator count ONLY in the watcher thread: when two pollers tried the same lock at once, flock
        # (same process, different fd) showed each other as 'held' -> a fake '2' in the 16.09 marathon (overlap in the coordinator
        # timelines 0/122). A single poller + it closes as soon as the lock is released.

    def ledger_scan(self):
        t = time.monotonic()
        subprocess.run([sys.executable, str(self.root / 'lib/run_ledger.py'), 'scan'], env=self.f.env, capture_output=True, timeout=600)
        self.measurement['ledger_scan_sec'].append(round(time.monotonic() - t, 2))

    def ledger_usd(self):
        db = self.home / 'chain/request-budget.db'
        if not db.exists():
            return 0.0
        import sqlite3
        with sqlite3.connect(db) as con:
            row = con.execute('select coalesce(sum(actual),0), coalesce(sum(reserved),0) from reservation').fetchone()
        return row[0] / 1e6

    def rotation(self):
        """Real rotation with a `now` moved forward (the 2-day rule): completed worktrees are archived, rounds are counted."""
        import rotation as rot
        counts = rot.rotate(self.home, keep=5, now=time.time() + 3 * 86400)
        self.measurement['rotation'].append({'ts': time.strftime('%T'), **counts})
        return counts

    def write(self, finished=False):
        elapsed_h = (time.time() - self.t0) / 3600
        kinds = self.measurement['kinds']
        normal = [t for t in kinds if t['scenario'] == 'normal']
        size = self.size_mb()
        if self.first_size is None:
            self.first_size = size
        self.measurement['size_mb'].append(round(size, 2))
        growth = (size - self.first_size) / max(elapsed_h, 1 / 60)
        leak = (self.home / 'LIVE-LEAK').exists()
        outside = 0
        live = str(self.f.fake_home / '.local/share/cadre-engineering')
        for entry in self.f.argv():
            for arg in entry['args']:
                for piece in arg.replace('"', ' ').replace("'", ' ').split():
                    if ('/lib/' in piece or '/bin/' in piece) and not piece.startswith(('/bin/', '/usr/', '/opt/', '/private/', '/System/')) or live in piece:
                        if not piece.startswith(str(self.root)):
                            outside += 1
        outcome = {
            'coordinator_rss_mb_max': (round(self.measurement['rss_coordinator_max_mb'], 1), self.measurement['rss_coordinator_max_mb'] <= THRESHOLDS['coordinator_rss_mb_max']),
            'guard_rss_mb_max': (round(self.measurement['rss_guard_max_mb'], 1), self.measurement['rss_guard_max_mb'] <= THRESHOLDS['guard_rss_mb_max']),
            'growth_mb_hour_max': (round(growth, 2), growth <= THRESHOLDS['growth_mb_hour_max']),
            'ledger_scan_sec_max': (self.measurement['ledger_scan_sec'][-1] if self.measurement['ledger_scan_sec'] else None,
                                   bool(self.measurement['ledger_scan_sec']) and self.measurement['ledger_scan_sec'][-1] <= THRESHOLDS['ledger_scan_sec_max']),
            'completed_hour_min': (round(sum(1 for t in normal if t['state'] == 'completed') / max(elapsed_h, 1 / 60), 1),
                                    (not finished) or sum(1 for t in normal if t['state'] == 'completed') / max(elapsed_h, 1 / 60) >= THRESHOLDS['completed_hour_min']),
            'fake_cloud_cost_usd': (self.ledger_usd(), self.ledger_usd() == 0.0),
            'guard_revival_sec_max': (max(self.measurement['guard_revival_sec'], default=None), all(s <= THRESHOLDS['guard_revival_sec_max'] for s in self.measurement['guard_revival_sec'])),
            'coordinator_revival_sec_max': (max(self.measurement['coordinator_revival_sec'], default=None), all(s <= THRESHOLDS['coordinator_revival_sec_max'] for s in self.measurement['coordinator_revival_sec'])),
            'retry_count': ([t.get('retry_count') for t in kinds if t['scenario'] == 'qa_red'],
                             sampled(kinds, 'qa_red', lambda t: t.get('retry_count') == 2 and t['state'] == 'failed')),
            'disk_low_executor_call': ([t.get('executor_call') for t in kinds if t['scenario'] == 'disk_low'],
                                        sampled(kinds, 'disk_low', lambda t: t.get('executor_call') == 0 and t['state'] == 'failed')),
            'same_project_concurrent_coordinator_max': (self.measurement['concurrent_max'], self.measurement['concurrent_max'] <= 1),
            'live_root_leak': (int(leak) + outside, not leak and outside == 0),
        }
        normal_completed = sum(1 for t in normal if t['state'] == 'completed')
        report = {'run_id': f"marathon-{time.strftime('%Y%m%d-%H%M%S', time.localtime(self.t0))}", 'finished': finished, 'hour_passing': round(elapsed_h, 3),
                  'thresholds': THRESHOLDS, 'outcome': {k: {'measurement': v[0], 'passed': v[1]} for k, v in outcome.items()},
                  'status': 'PASS' if finished and all(v[1] for v in outcome.values()) else ('RUNNING' if not finished else 'FAIL'),
                  'round_count': len(kinds), 'normal_completed': normal_completed,
                  'statuses': {s: sum(1 for t in kinds if t['state'] == s) for s in ('completed', 'failed', 'cancelled', 'retryable')},
                  'isolated_home_dir': str(self.home), 'code_tree_root': str(self.root), 'measurement': self.measurement}
        self.report.parent.mkdir(parents=True, exist_ok=True)
        self.report.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str) + '\n')
        return report

    # --- scenarios --------------------------------------------------------------------------------------------
    def round_normal(self):
        t = time.monotonic()
        rid, _ = self.submit()
        record = self.wait_terminal(rid, 300)
        fr = record.get('final_result') or {}
        return {'rid': rid, 'state': record['state'], 'sec': round(time.monotonic() - t, 1), 'reason': record.get('reason'),
                'final': fr.get('status'), 'final_reason': (fr.get('reason') or '')[:120], 'retry_count': record.get('retry_count'), 'revive_count': record.get('revive_count')}

    def round_guard_kill(self):
        """Guard SIGKILL during a long run (the worker sleeps 120 s): the coordinator's 60 s tick must bring the guard back
        (while the coordinator is still alive) — this is how it happens live."""
        pid = self.guard_pid()
        if not pid or not run_store.pid_alive(pid):
            self.log('guard already absent; normal round')
            return {**self.round_normal(), 'note': 'guard was absent'}
        t = time.monotonic()
        (self.home / 'worker-started').unlink(missing_ok=True)
        rid, _ = self.submit({'E2E_SLOW': '1'})
        # kill when the worker falls asleep (no new engine will start): revival can come ONLY from the coordinator's 60 s tick
        # the run may wait in the previous round's retryable/failed queue (P4): first wait for it to become running (the 05:10 round
        # was left without a coordinator because of this and had no coordinator.log)
        self.wait_for(lambda: self.store.read(rid)['state'] == 'running', 600)
        self.wait_for(lambda: self.store.read(rid).get('owner_pid') and (self.home / 'worker-started').exists(), 90)
        owner = self.store.read(rid).get('owner_pid')
        os.kill(pid, signal.SIGKILL)
        t = time.monotonic()
        self.log(f'guard SIGKILL pid={pid} (coordinator {owner} live, worker sleeping)')
        ok = self.wait_for(lambda: self.guard_pid() not in (None, pid) and run_store.guard_live(self.home, 'health-watch'), THRESHOLDS['guard_revival_sec_max'] + 60)
        revival = round(time.monotonic() - t, 1)
        coordinator_live = bool(owner) and run_store.pid_alive(int(owner))
        self.measurement['guard_revival_sec'].append(revival if ok else 10 ** 6)
        (self.home / 'worker-started').unlink(missing_ok=True)
        record = self.wait_terminal(rid, 400)
        return {'rid': rid, 'state': record['state'], 'sec': round(time.monotonic() - t, 1), 'guard_revival_sec': revival, 'revived': ok,
                'coordinator_live_while': coordinator_live,
                'revived': ((self.store.path(rid) / 'coordinator.log').read_text().count('GUARD REVIVED') if (self.store.path(rid) / 'coordinator.log').exists() else None)}

    def round_coordinator_kill(self):
        """Coordinator SIGKILL at job start: the children (dispatcher/engine) carry the owner lock, the lock frees when they finish;
        the guard tick (60 s) brings the coordinator back with `revive`, the recorded plan/tasks are not repeated, the run becomes terminal."""
        t = time.monotonic()
        rid, _ = self.submit()
        ok = self.wait_for(lambda: self.store.read(rid).get('owner_pid'), 60)
        pid = self.store.read(rid).get('owner_pid')
        if ok and pid:
            time.sleep(1.5)
            os.kill(int(pid), signal.SIGKILL)
            self.log(f'coordinator SIGKILL pid={pid} (run {rid[:15]})')
        record = self.wait_terminal(rid, THRESHOLDS['coordinator_revival_sec_max'] + 120)
        revival = round(time.monotonic() - t, 1)
        self.measurement['coordinator_revival_sec'].append(revival if record['state'] == 'completed' else 10 ** 6)
        return {'rid': rid, 'state': record['state'], 'sec': revival, 'revive_count': record.get('revive_count'), 'retry_count': record.get('retry_count'),
                'reason': record.get('reason'), 'final': (record.get('final_result') or {}).get('status')}

    def round_qa_red(self):
        t = time.monotonic()
        rid, _ = self.submit({'E2E_BROKEN': 'b'})
        record = self.wait_terminal(rid, 60 * 3 * 4 + 120)   # 2 retries x (guard 60 s + run) + failed
        return {'rid': rid, 'state': record['state'], 'sec': round(time.monotonic() - t, 1), 'retry_count': record.get('retry_count'), 'reason': record.get('reason')}

    def round_disk_low(self):
        t = time.monotonic()
        rid, _ = self.submit({'CADRE_DISK_MIN_MB': str(10 ** 9), 'CADRE_DISK_WAIT': '5', 'CADRE_DISK_POLL': '1'})
        record = self.wait_terminal(rid, 120)
        calls = [c for c in self.f.calls() if c.get('task') and rid in c.get('cwd', '')] if (self.home / 'opencode-calls.jsonl').exists() else []
        # executor call: number of fake opencode processes opened in this run's worktree (cwd)
        return {'rid': rid, 'state': record['state'], 'sec': round(time.monotonic() - t, 1), 'reason': record.get('reason'), 'executor_call': len(calls)}

    def round_queue(self):
        t = time.monotonic()
        a, _ = self.submit()
        b, out = self.submit()
        queue = 'QUEUE' in out and self.store.read(b)['state'] == 'queued'
        ra = self.wait_terminal(a, 400)
        rb = self.wait_terminal(b, 400)
        return {'rid': a, 'rid_b': b, 'state': rb['state'], 'state_a': ra['state'], 'queue': queue, 'sec': round(time.monotonic() - t, 1)}

    def execute(self):
        self.doc.parent.mkdir(parents=True, exist_ok=True)
        self.log(f'MARATHON started: {self.hour} hour, home_dir={self.home}, root={self.root}')
        last_rotation = time.time()
        i = 0
        try:
            while (time.time() - self.t0) < self.hour * 3600:
                scenario = SCENARIO_ORDER[i % len(SCENARIO_ORDER)]
                i += 1
                self.log(f'round {i}: {scenario}')
                try:
                    round = getattr(self, f'round_{scenario}')()
                except Exception as error:  # noqa: BLE001 — the marathon does not stop, it records the error
                    round = {'rid': None, 'state': 'ERROR', 'error': f'{type(error).__name__}: {error}'}
                round.update(scenario=scenario, ts=time.strftime('%T'))
                self.measurement['kinds'].append(round)
                self.log(f'round {i} result: {json.dumps(round, ensure_ascii=False, default=str)[:300]}')
                self.sample()
                if i % 3 == 0:
                    self.ledger_scan()
                if time.time() - last_rotation > 3600:
                    self.log(f'rotation: {self.rotation()}')
                    last_rotation = time.time()
                self.write()
        finally:
            self.ledger_scan()
            self.log(f'last rotation: {self.rotation()}')
            report = self.write(finished=True)
            self.doc_write(report)
            self.stop.set()
            self.f.kill_guards()
            for name in ('health-watch.log',):
                src = self.home / 'logs' / name
                if src.exists():
                    shutil.copy(src, self.report.with_suffix('.' + name))
            self.log(f"MARATHON finished: {report['status']}")
            if report['status'] != 'PASS':
                # 16.09: in the 8 h run the home was cleaned, so 8 'normal failed' rounds could not be inspected; in the 2 h run the temporary
                # directory said to be 'kept' was still deleted by the TemporaryDirectory finalizer at process exit -> with REMAINED the home is COPIED next to the report.
                copy = self.report.with_suffix('') .parent / (self.report.stem + '-home_dir')
                try:
                    self.f.kill_guards()
                    shutil.copytree(self.home, copy, symlinks=True, dirs_exist_ok=True)
                    self.log(f"isolated home copied for inspection: {copy}")
                except OSError as error:
                    self.log(f"could not copy home: {error}")
            self.f.doCleanups()

    def doc_write(self, report):
        lines = [f"# MARATHON {time.strftime('%F', time.localtime(self.t0))} — isolated {self.hour} hour", '',
                 f"Report: `{self.report}` · status: **{report['status']}** · elapsed: {report['hour_passing']} h · round: {report['round_count']} · "
                 f"normally completed: {report['normal_completed']} · statuses: {report['statuses']}", '',
                 'Thresholds were recorded in engine/tests/marathon.py THRESHOLDS before the run. Fakes: opencode, provider_budget.py and HOME with a sabotaged live root.',
                 'Real components: cadre.sh, run_store.py coordinator, qe_core runner, cadre-dispatch.sh, health-watch.sh (60s tick), capacity-watch.sh, run_ledger.py, rotation.py, worktree_archive.py.', '',
                 '| Threshold | Limit | Measurement | Outcome |', '|---|---|---|---|']
        for key, value in report['outcome'].items():
            lines.append(f"| {key} | {THRESHOLDS[key]} | {value['measurement']} | {'PASSED' if value['passed'] else 'REMAINED'} |")
        lines += ['', '## Kinds', '', '| # | hour | scenario | status | s | note |', '|---|---|---|---|---|---|']
        for n, t in enumerate(report['measurement']['kinds'], 1):
            extra = {k: v for k, v in t.items() if k not in ('rid', 'scenario', 'ts', 'state', 'sec')}
            lines.append(f"| {n} | {t.get('ts')} | {t['scenario']} | {t['state']} | {t.get('sec')} | {json.dumps(extra, ensure_ascii=False, default=str)[:120]} |")
        lines += ['', '## Rotation', '', json.dumps(report['measurement']['rotation'], ensure_ascii=False), '',
                  '## Ledger scan times (s)', '', str(report['measurement']['ledger_scan_sec']), '',
                  '## Size (MB, isolated home)', '', str(report['measurement']['size_mb']), '',
                  '## Not verified', '', 'Real providers, models, Windows, sleep and power loss were not tested; the fake executor finishes in seconds while real work takes minutes.',
                  'Guard tick is 60s here and 1800s live; interpret revival durations on a live scale 30 times longer.']
        self.doc.write_text('\n'.join(lines) + '\n')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hour', type=float, default=8.0)
    parser.add_argument('--report', type=Path, default=Path(f"reports/p7-marathon-{time.strftime('%Y%m%d')}.json"))
    parser.add_argument('--doc', type=Path, default=Path(f"docs/MARATHON-{time.strftime('%Y%m%d')}.md"))
    parser.add_argument('--scenarios', default='', help='comma-separated scenario order for a smoke test')
    args = parser.parse_args()
    if args.scenarios:
        SCENARIO_ORDER[:] = [s.strip() for s in args.scenarios.split(',') if s.strip()]
    Marathon(args.hour, args.report, args.doc).execute()
    report = json.loads(args.report.read_text())
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
