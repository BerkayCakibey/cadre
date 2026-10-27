#!/usr/bin/env python3
"""P7 — isolated MARATHON. Fake executor + fake budget; the REAL cadre.sh/kosu.py/qe_core/
saglik-nobet.sh/kota-izle.sh/kosu_defteri.py/rotasyon.py, isolated home + isolated code root + a sabotaged fake HOME + argv shims
(test_gece_omur.IsolatedNight). Usage:
    python3 engine/tests/maraton.py --saat 8 --rapor reports/p7-maraton-<date>.json --doc docs/MARATON-<date>.md
Thresholds are written here BEFORE THE RUN (ESIKLER); a measurement without a threshold is narrative, not acceptance. Injected failures:
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
import test_gece_omur as omur  # noqa: E402
import kosu  # noqa: E402
from kosu import RunStore  # noqa: E402

# --- THRESHOLDS WRITTEN BEFORE THE RUN (acceptance criteria) ---------------------------------------------------------------
ESIKLER = {
    'koordinator_rss_mb_max': 200,          # peak RSS of any coordinator process (python kosu.py execute)
    'bekci_rss_mb_max': 60,                 # RSS of the saglik-nobet.sh bash process
    'buyume_mb_saat_max': 25,               # hourly growth of the isolated home (logs+runs+worktrees+reports)
    'defter_tara_sn_max': 20,               # kosu_defteri.py tara duration (last measurement)
    'tamamlanan_saat_min': 10,              # minimum completed runs per hour (normal scenario)
    'sahte_bulut_maliyet_usd': 0.0,         # total settled USD in the ledger (request-budget.db) == 0
    'bekci_dirilme_sn_max': 120,            # guard SIGKILL -> a new live pidfile (CADRE_BEKCI_KONTROL=60 + tick)
    'koordinator_dirilme_sn_max': 240,      # coordinator SIGKILL -> run COMPLETED (guard tick 60 s + continue from the recorded plan)
    'retry_sayisi': 2,                      # a QA-red run is retried exactly 2 times, then failed
    'disk_low_yurutucu_cagri': 0,           # executor Popen count below the disk threshold
    'ayni_proje_eszamanli_koordinator_max': 1,
    'canli_kok_sizintisi': 0,               # no LIVE-LEAK file, no lib/bin path outside the isolated root in argv
    # B62 (T1 audit): the threshold enforced the opposite of DECISION 8 (it counted escalation as "PASSED"). Decision 8: local model
}
SENARYOLAR = ['normal', 'normal', 'bekci_kill', 'normal', 'qa_kirmizi', 'normal', 'disk_low', 'kuyruk', 'normal',
              'koordinator_kill', 'normal']
SENARYO_SIRASI = list(SENARYOLAR)  # can be changed with --senaryolar (smoke test)


def ornekli(turlar, senaryo, kosul):
    """B63 (T1 audit): the old thresholds were `all(... for t in turlar if t['senaryo']=='X')`; if the scenario never ran, `all([])`
    returned True and the threshold wrote PASSED by itself. Now at least one sample is required."""
    ornekler = [t for t in turlar if t['senaryo'] == senaryo]
    return bool(ornekler) and all(kosul(t) for t in ornekler)


class Maraton:
    def __init__(self, saat: float, rapor: Path, doc: Path):
        self.saat, self.rapor, self.doc = saat, rapor, doc
        self.f = omur.IsolatedNight()
        self.f.setUp()
        self.f.env.update(CADRE_NOBET_ARALIK='60', CADRE_BEKCI_KONTROL='60', CADRE_KOORD_TIK='0.5', CADRE_KOTA_ARALIK='300')
        self.home, self.root, self.store = self.f.home, self.f.root, self.f.store
        self.t0 = time.time()
        self.olcum: dict = {'baslangic': time.strftime('%F %T'), 'saat_hedef': saat, 'turlar': [], 'olaylar': [],
                            'rss_koordinator_max_mb': 0, 'rss_bekci_max_mb': 0, 'defter_tara_sn': [], 'boyut_mb': [],
                            'bekci_dirilme_sn': [], 'koordinator_dirilme_sn': [], 'eszamanli_max': 0, 'rotasyon': []}
        self.gorev = 0
        self.ilk_boyut = None
        import threading
        self.stop = threading.Event()
        threading.Thread(target=self._eszamanli_izle, daemon=True).start()

    def _eszamanli_izle(self):
        while not self.stop.is_set():
            try:
                self.olcum['eszamanli_max'] = max(self.olcum['eszamanli_max'], len(kosu.active_runs(self.store)))
            except (OSError, ValueError):
                pass
            time.sleep(.2)

    # --- helpers -------------------------------------------------------------------------------------------
    def log(self, msg):
        line = f"{time.strftime('%F %T')} {msg}"
        print(line, flush=True)
        self.olcum['olaylar'].append(line)

    def submit(self, extra=None, text='a.py ve b.py dosyalarini olustur, derlendigini dogrula ve testi gecir'):
        self.gorev += 1
        result = subprocess.run(['bash', str(self.root / 'bin/cadre.sh'), 'gorev'], input=f'{text} #{self.gorev}\ngo\n',
                                cwd=self.f.repo, env={**self.f.env, **(extra or {})}, text=True, capture_output=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(f'cadre.sh gorev rc={result.returncode}: {result.stderr[-500:]}')
        return self.store.list('e2e')[-1]['run_id'], result.stdout

    def wait_terminal(self, rid, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = self.store.read(rid)
            if record['state'] in ('completed', 'cancelled', 'failed'):
                return record
            self.orneklem()
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
        return kosu.bekci_pid(self.home, 'saglik-nobet')

    def rss_mb(self, pattern):
        out = subprocess.run(['bash', '-c', f"pgrep -f '{pattern}' | xargs -I{{}} ps -o rss= -p {{}} 2>/dev/null"], capture_output=True, text=True).stdout
        values = [int(v) for v in out.split() if v.isdigit()]
        return max(values) / 1024 if values else 0.0

    def boyut_mb(self):
        total = 0
        for sub in ('logs', 'runs', 'worktrees', 'reports', 'archives', 'zincir'):
            path = self.home / sub
            if path.exists():
                total += sum(p.stat().st_size for p in path.rglob('*') if p.is_file())
        return total / 1_048_576

    def orneklem(self):
        self.olcum['rss_koordinator_max_mb'] = max(self.olcum['rss_koordinator_max_mb'], self.rss_mb(f'{self.root}/lib/kosu.py execute'))
        self.olcum['rss_bekci_max_mb'] = max(self.olcum['rss_bekci_max_mb'], self.rss_mb(f'{self.root}/bin/saglik-nobet.sh'))
        # concurrent coordinator count ONLY in the watcher thread: when two pollers tried the same lock at once, flock
        # (same process, different fd) showed each other as 'held' -> a fake '2' in the 16.09 marathon (overlap in the coordinator
        # timelines 0/122). A single poller + it closes as soon as the lock is released.

    def defter_tara(self):
        t = time.monotonic()
        subprocess.run([sys.executable, str(self.root / 'lib/kosu_defteri.py'), 'tara'], env=self.f.env, capture_output=True, timeout=600)
        self.olcum['defter_tara_sn'].append(round(time.monotonic() - t, 2))

    def ledger_usd(self):
        db = self.home / 'zincir/request-budget.db'
        if not db.exists():
            return 0.0
        import sqlite3
        with sqlite3.connect(db) as con:
            row = con.execute('select coalesce(sum(actual),0), coalesce(sum(reserved),0) from reservation').fetchone()
        return row[0] / 1e6

    def rotasyon(self):
        """Real rotation with a `now` moved forward (the 2-day rule): completed worktrees are archived, rounds are counted."""
        import rotasyon as rot
        counts = rot.rotate(self.home, keep=5, now=time.time() + 3 * 86400)
        self.olcum['rotasyon'].append({'ts': time.strftime('%T'), **counts})
        return counts

    def yaz(self, bitti=False):
        elapsed_h = (time.time() - self.t0) / 3600
        turlar = self.olcum['turlar']
        normal = [t for t in turlar if t['senaryo'] == 'normal']
        boyut = self.boyut_mb()
        if self.ilk_boyut is None:
            self.ilk_boyut = boyut
        self.olcum['boyut_mb'].append(round(boyut, 2))
        buyume = (boyut - self.ilk_boyut) / max(elapsed_h, 1 / 60)
        leak = (self.home / 'LIVE-LEAK').exists()
        outside = 0
        live = str(self.f.fake_home / '.local/share/cadre-engineering')
        for entry in self.f.argv():
            for arg in entry['args']:
                for piece in arg.replace('"', ' ').replace("'", ' ').split():
                    if ('/lib/' in piece or '/bin/' in piece) and not piece.startswith(('/bin/', '/usr/', '/opt/', '/private/', '/System/')) or live in piece:
                        if not piece.startswith(str(self.root)):
                            outside += 1
        sonuc = {
            'koordinator_rss_mb_max': (round(self.olcum['rss_koordinator_max_mb'], 1), self.olcum['rss_koordinator_max_mb'] <= ESIKLER['koordinator_rss_mb_max']),
            'bekci_rss_mb_max': (round(self.olcum['rss_bekci_max_mb'], 1), self.olcum['rss_bekci_max_mb'] <= ESIKLER['bekci_rss_mb_max']),
            'buyume_mb_saat_max': (round(buyume, 2), buyume <= ESIKLER['buyume_mb_saat_max']),
            'defter_tara_sn_max': (self.olcum['defter_tara_sn'][-1] if self.olcum['defter_tara_sn'] else None,
                                   bool(self.olcum['defter_tara_sn']) and self.olcum['defter_tara_sn'][-1] <= ESIKLER['defter_tara_sn_max']),
            'tamamlanan_saat_min': (round(sum(1 for t in normal if t['state'] == 'completed') / max(elapsed_h, 1 / 60), 1),
                                    (not bitti) or sum(1 for t in normal if t['state'] == 'completed') / max(elapsed_h, 1 / 60) >= ESIKLER['tamamlanan_saat_min']),
            'sahte_bulut_maliyet_usd': (self.ledger_usd(), self.ledger_usd() == 0.0),
            'bekci_dirilme_sn_max': (max(self.olcum['bekci_dirilme_sn'], default=None), all(s <= ESIKLER['bekci_dirilme_sn_max'] for s in self.olcum['bekci_dirilme_sn'])),
            'koordinator_dirilme_sn_max': (max(self.olcum['koordinator_dirilme_sn'], default=None), all(s <= ESIKLER['koordinator_dirilme_sn_max'] for s in self.olcum['koordinator_dirilme_sn'])),
            'retry_sayisi': ([t.get('retry_count') for t in turlar if t['senaryo'] == 'qa_kirmizi'],
                             ornekli(turlar, 'qa_kirmizi', lambda t: t.get('retry_count') == 2 and t['state'] == 'failed')),
            'disk_low_yurutucu_cagri': ([t.get('yurutucu_cagri') for t in turlar if t['senaryo'] == 'disk_low'],
                                        ornekli(turlar, 'disk_low', lambda t: t.get('yurutucu_cagri') == 0 and t['state'] == 'failed')),
            'ayni_proje_eszamanli_koordinator_max': (self.olcum['eszamanli_max'], self.olcum['eszamanli_max'] <= 1),
            'canli_kok_sizintisi': (int(leak) + outside, not leak and outside == 0),
        }
        normal_completed = sum(1 for t in normal if t['state'] == 'completed')
        report = {'run_id': f"maraton-{time.strftime('%Y%m%d-%H%M%S', time.localtime(self.t0))}", 'bitti': bitti, 'saat_gecen': round(elapsed_h, 3),
                  'esikler': ESIKLER, 'sonuc': {k: {'olcum': v[0], 'gecti': v[1]} for k, v in sonuc.items()},
                  'status': 'PASS' if bitti and all(v[1] for v in sonuc.values()) else ('RUNNING' if not bitti else 'FAIL'),
                  'tur_sayisi': len(turlar), 'normal_tamamlanan': normal_completed,
                  'durumlar': {s: sum(1 for t in turlar if t['state'] == s) for s in ('completed', 'failed', 'cancelled', 'retryable')},
                  'izole_ev': str(self.home), 'kod_koku': str(self.root), 'olcum': self.olcum}
        self.rapor.parent.mkdir(parents=True, exist_ok=True)
        self.rapor.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str) + '\n')
        return report

    # --- senaryolar --------------------------------------------------------------------------------------------
    def tur_normal(self):
        t = time.monotonic()
        rid, _ = self.submit()
        record = self.wait_terminal(rid, 300)
        fr = record.get('final_result') or {}
        return {'rid': rid, 'state': record['state'], 'sn': round(time.monotonic() - t, 1), 'reason': record.get('reason'),
                'final': fr.get('status'), 'final_reason': (fr.get('reason') or '')[:120], 'retry_count': record.get('retry_count'), 'revive_count': record.get('revive_count')}

    def tur_bekci_kill(self):
        """Guard SIGKILL during a long run (the worker sleeps 120 s): the coordinator's 60 s tick must bring the guard back
        (while the coordinator is still alive) — this is how it happens live."""
        pid = self.guard_pid()
        if not pid or not kosu.pid_yasiyor(pid):
            self.log('bekci zaten yok; normal tur')
            return {**self.tur_normal(), 'not': 'bekci yoktu'}
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
        self.log(f'bekci SIGKILL pid={pid} (koordinator {owner} canli, isci uykuda)')
        ok = self.wait_for(lambda: self.guard_pid() not in (None, pid) and kosu.bekci_canli(self.home, 'saglik-nobet'), ESIKLER['bekci_dirilme_sn_max'] + 60)
        dirilme = round(time.monotonic() - t, 1)
        koordinator_canli = bool(owner) and kosu.pid_yasiyor(int(owner))
        self.olcum['bekci_dirilme_sn'].append(dirilme if ok else 10 ** 6)
        (self.home / 'worker-started').unlink(missing_ok=True)
        record = self.wait_terminal(rid, 400)
        return {'rid': rid, 'state': record['state'], 'sn': round(time.monotonic() - t, 1), 'bekci_dirilme_sn': dirilme, 'dirildi': ok,
                'koordinator_canli_iken': koordinator_canli,
                'diriltilen': ((self.store.path(rid) / 'coordinator.log').read_text().count('BEKCI DIRILTILDI') if (self.store.path(rid) / 'coordinator.log').exists() else None)}

    def tur_koordinator_kill(self):
        """Coordinator SIGKILL at job start: the children (dispatcher/engine) carry the owner lock, the lock frees when they finish;
        the guard tick (60 s) brings the coordinator back with `revive`, the recorded plan/tasks are not repeated, the run becomes terminal."""
        t = time.monotonic()
        rid, _ = self.submit()
        ok = self.wait_for(lambda: self.store.read(rid).get('owner_pid'), 60)
        pid = self.store.read(rid).get('owner_pid')
        if ok and pid:
            time.sleep(1.5)
            os.kill(int(pid), signal.SIGKILL)
            self.log(f'koordinator SIGKILL pid={pid} (run {rid[:15]})')
        record = self.wait_terminal(rid, ESIKLER['koordinator_dirilme_sn_max'] + 120)
        dirilme = round(time.monotonic() - t, 1)
        self.olcum['koordinator_dirilme_sn'].append(dirilme if record['state'] == 'completed' else 10 ** 6)
        return {'rid': rid, 'state': record['state'], 'sn': dirilme, 'revive_count': record.get('revive_count'), 'retry_count': record.get('retry_count'),
                'reason': record.get('reason'), 'final': (record.get('final_result') or {}).get('status')}

    def tur_qa_kirmizi(self):
        t = time.monotonic()
        rid, _ = self.submit({'E2E_BROKEN': 'b'})
        record = self.wait_terminal(rid, 60 * 3 * 4 + 120)   # 2 retries x (guard 60 s + run) + failed
        return {'rid': rid, 'state': record['state'], 'sn': round(time.monotonic() - t, 1), 'retry_count': record.get('retry_count'), 'reason': record.get('reason')}

    def tur_disk_low(self):
        t = time.monotonic()
        rid, _ = self.submit({'CADRE_DISK_MIN_MB': str(10 ** 9), 'CADRE_DISK_WAIT': '5', 'CADRE_DISK_POLL': '1'})
        record = self.wait_terminal(rid, 120)
        calls = [c for c in self.f.calls() if c.get('task') and rid in c.get('cwd', '')] if (self.home / 'opencode-calls.jsonl').exists() else []
        # executor call: number of fake opencode processes opened in this run's worktree (cwd)
        return {'rid': rid, 'state': record['state'], 'sn': round(time.monotonic() - t, 1), 'reason': record.get('reason'), 'yurutucu_cagri': len(calls)}

    def tur_kuyruk(self):
        t = time.monotonic()
        a, _ = self.submit()
        b, out = self.submit()
        kuyruk = 'SIRAYA' in out and self.store.read(b)['state'] == 'queued'
        ra = self.wait_terminal(a, 400)
        rb = self.wait_terminal(b, 400)
        return {'rid': a, 'rid_b': b, 'state': rb['state'], 'state_a': ra['state'], 'kuyruk': kuyruk, 'sn': round(time.monotonic() - t, 1)}

    def kos(self):
        self.doc.parent.mkdir(parents=True, exist_ok=True)
        self.log(f'MARATON basladi: {self.saat} saat, ev={self.home}, kok={self.root}')
        son_rotasyon = time.time()
        i = 0
        try:
            while (time.time() - self.t0) < self.saat * 3600:
                senaryo = SENARYO_SIRASI[i % len(SENARYO_SIRASI)]
                i += 1
                self.log(f'tur {i}: {senaryo}')
                try:
                    tur = getattr(self, f'tur_{senaryo}')()
                except Exception as error:  # noqa: BLE001 — the marathon does not stop, it records the error
                    tur = {'rid': None, 'state': 'HATA', 'hata': f'{type(error).__name__}: {error}'}
                tur.update(senaryo=senaryo, ts=time.strftime('%T'))
                self.olcum['turlar'].append(tur)
                self.log(f'tur {i} sonucu: {json.dumps(tur, ensure_ascii=False, default=str)[:300]}')
                self.orneklem()
                if i % 3 == 0:
                    self.defter_tara()
                if time.time() - son_rotasyon > 3600:
                    self.log(f'rotasyon: {self.rotasyon()}')
                    son_rotasyon = time.time()
                self.yaz()
        finally:
            self.defter_tara()
            self.log(f'son rotasyon: {self.rotasyon()}')
            report = self.yaz(bitti=True)
            self.doc_yaz(report)
            self.stop.set()
            self.f.kill_guards()
            for name in ('saglik-nobet.log',):
                src = self.home / 'logs' / name
                if src.exists():
                    shutil.copy(src, self.rapor.with_suffix('.' + name))
            self.log(f"MARATON bitti: {report['status']}")
            if report['status'] != 'PASS':
                # 16.09: in the 8 h run the home was cleaned, so 8 'normal failed' rounds could not be inspected; in the 2 h run the temporary
                # directory said to be 'kept' was still deleted by the TemporaryDirectory finalizer at process exit -> with KALDI the home is COPIED next to the report.
                kopya = self.rapor.with_suffix('') .parent / (self.rapor.stem + '-ev')
                try:
                    self.f.kill_guards()
                    shutil.copytree(self.home, kopya, symlinks=True, dirs_exist_ok=True)
                    self.log(f"izole ev inceleme icin kopyalandi: {kopya}")
                except OSError as error:
                    self.log(f"ev kopyalanamadi: {error}")
            self.f.doCleanups()

    def doc_yaz(self, report):
        lines = [f"# MARATON {time.strftime('%F', time.localtime(self.t0))} — izole {self.saat} saat", '',
                 f"Rapor: `{self.rapor}` · durum: **{report['status']}** · gecen: {report['saat_gecen']} sa · tur: {report['tur_sayisi']} · "
                 f"normal tamamlanan: {report['normal_tamamlanan']} · durumlar: {report['durumlar']}", '',
                 'Esikler kosudan ONCE `engine/tests/maraton.py` ESIKLER sozlugunde yazildi. Sahte: opencode, butce.py, HOME (sabotajli canli kok).',
                 'Gercek: cadre.sh, kosu.py koordinator, qe_core motor, cadre-dispatch.sh, saglik-nobet.sh (60 sn tik), kota-izle.sh, kosu_defteri.py, rotasyon.py, arsiv.py.', '',
                 '| Esik | Sinir | Olcum | Sonuc |', '|---|---|---|---|']
        for key, value in report['sonuc'].items():
            lines.append(f"| {key} | {ESIKLER[key]} | {value['olcum']} | {'GECTI' if value['gecti'] else 'KALDI'} |")
        lines += ['', '## Turlar', '', '| # | saat | senaryo | durum | sn | not |', '|---|---|---|---|---|---|']
        for n, t in enumerate(report['olcum']['turlar'], 1):
            extra = {k: v for k, v in t.items() if k not in ('rid', 'senaryo', 'ts', 'state', 'sn')}
            lines.append(f"| {n} | {t.get('ts')} | {t['senaryo']} | {t['state']} | {t.get('sn')} | {json.dumps(extra, ensure_ascii=False, default=str)[:120]} |")
        lines += ['', '## Rotasyon', '', json.dumps(report['olcum']['rotasyon'], ensure_ascii=False), '',
                  '## Defter tarama sureleri (sn)', '', str(report['olcum']['defter_tara_sn']), '',
                  '## Boyut (MB, izole ev)', '', str(report['olcum']['boyut_mb']), '',
                  '## Dogrulanmayan', '', 'Gercek saglayici/model/Windows/uyku/elektrik yok; sahte yurutucu saniyeler icinde biter (gercek is dakikalar).',
                  'Bekci tiki 60 sn (canlida 1800): dirilme sureleri canlida 30 kat daha uzun olcek ile okunmali.']
        self.doc.write_text('\n'.join(lines) + '\n')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--saat', type=float, default=8.0)
    parser.add_argument('--rapor', type=Path, default=Path(f"reports/p7-maraton-{time.strftime('%Y%m%d')}.json"))
    parser.add_argument('--doc', type=Path, default=Path(f"docs/MARATON-{time.strftime('%Y%m%d')}.md"))
    parser.add_argument('--senaryolar', default='', help='virgullu senaryo sirasi (duman testi icin)')
    args = parser.parse_args()
    if args.senaryolar:
        SENARYO_SIRASI[:] = [s.strip() for s in args.senaryolar.split(',') if s.strip()]
    Maraton(args.saat, args.rapor, args.doc).kos()
    report = json.loads(args.rapor.read_text())
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
