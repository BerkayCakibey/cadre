"""Liveness: stall detection for running executors. S1: liveness responsibility; shared services come from the qe_core interface."""
from __future__ import annotations
import re
import math
import hashlib


class HizliDusus:
    """B179: 12 attempts failing with rc!=0 at 15 s intervals (helper/compaction model zen 404) used up the attempt limit in 3 min;
    the error-loop detector counted per attempt so it did not see it, and rc!=0 was not counted as stuck so the rescuer did not step in.
    When `esik` consecutive attempts end with rc!=0 in less than `esik_sn` it is a `dongu`; `seri_baslangic` is the first attempt of the series."""

    def __init__(self, esik_sn=60, esik=3):
        import qe_core as core
        self.esik_sn, self.esik = float(esik_sn), int(esik)
        self.seri, self.seri_baslangic, self.dongu = 0, None, False

    def kaydet(self, rc, sure_sn, baslangic, kontrollu=False):
        import qe_core as core
        if kontrollu:   # B184: the engine's own interruption / a budget denial is not a failure; it neither extends nor breaks the streak
            return 0
        if rc != 0 and sure_sn < self.esik_sn:
            self.seri += 1
            if self.seri_baslangic is None:
                self.seri_baslangic = baslangic
        else:
            self.seri, self.seri_baslangic = 0, None
        self.dongu = self.seri >= self.esik
        return self.seri


class AracTekrari:
    """B200: the model called the same tool with the same input and the same output over and over (6+ times, ~23 s apart); G-F9 only
    counted the breaking test and the artefact guard waited 45 min. `esik` identical consecutive calls are a loop (OpenHands: 3-4 repeats)."""

    def __init__(self, logf, esik):
        import qe_core as core
        self.logf, self.esik = str(logf), int(esik)
        self.onceki, self.seri, self.artik = None, 0, b""
        try:
            self.ofset = core.pathlib.Path(self.logf).stat().st_size
        except OSError:
            self.ofset = 0

    def tik(self):
        import qe_core as core
        if self.esik <= 0:
            return ""
        try:
            with open(self.logf, "rb") as f:
                f.seek(self.ofset)
                yeni = f.read()
            self.ofset += len(yeni)
        except OSError:
            return ""
        satirlar = (self.artik + yeni).split(b"\n")
        self.artik = satirlar.pop()
        for ham in satirlar:
            try:
                event = core.json.loads(ham)
                if not isinstance(event, dict):
                    continue
                parca = event.get('part') or {}
                if parca.get('type') != 'tool' and event.get('type') != 'tool_use':
                    continue
            except (ValueError, AttributeError):
                continue
            durum = parca.get("state") or {}
            if durum.get("status") not in ("completed", "error"):
                continue
            imza = (parca.get("tool"), core.json.dumps(durum.get("input"), sort_keys=True, default=str), str(durum.get("output"))[:4000])
            self.seri = self.seri + 1 if imza == self.onceki else 1
            self.onceki = imza
            if self.seri >= self.esik:
                return str(parca.get("tool"))
        return ""


class TestDongusu:
    """G-F9: if the SAME test id shows `FAILED` in `esik` separate log events within one attempt's log slice, returns that id.
    Reads incrementally from the end of the file at setup (earlier attempts do not count). 23.09 T1/T1c: the same test 10+ times,
    a 41 min unbounded loop — the artefact guard did not trigger because artefacts were produced, and STALL did not because there was no silence."""
    KALIP = re.compile(r"FAILED ([\w./-]+::[\w\[\]-]+)")

    def __init__(self, logf, esik):
        import qe_core as core
        self.logf, self.esik = str(logf), int(esik)
        self.sayac = {}
        self.artik = b""
        try:
            self.ofset = core.pathlib.Path(self.logf).stat().st_size
        except OSError:
            self.ofset = 0

    def tik(self):
        import qe_core as core
        if self.esik <= 0:
            return ""
        try:
            with open(self.logf, "rb") as f:
                f.seek(self.ofset)
                yeni = f.read()
            self.ofset += len(yeni)
        except OSError:
            return ""
        satirlar = (self.artik + yeni).split(b"\n")
        self.artik = satirlar.pop()   # B185: an incomplete line waits for the next tick; a half id does not count
        for ham in satirlar:
            satir = ham.decode("utf-8", "replace")
            if satir.startswith('{"type":"text"'):
                continue   # B185: the model's own text echoes the id; only tool output counts
            for kimlik in set(self.KALIP.findall(satir)):   # B185: a single pytest output appears twice in the same event
                self.sayac[kimlik] = self.sayac.get(kimlik, 0) + 1
                if self.sayac[kimlik] >= self.esik:
                    return kimlik
        return ""


class Canlilik:
    """CANLI1: a session does not count as stuck until four independent signals go quiet."""

    def __init__(self, logf, proc, wt=None, min_sn=600.0):
        import qe_core as core
        self.proc, self.wt = proc, wt
        self.min_sn = float(min_sn)
        from sinirlar import son_sure
        self.hareket_siniri = float(son_sure('qa'))
        self.logf = (proc.attempt.stdout_path if isinstance(proc, core.RecordedProcess) else core.pathlib.Path(logf))
        self.kayit = proc.attempt.root / 'canlilik.json' if isinstance(proc, core.RecordedProcess) else None
        self.son = core.time.monotonic()
        self.en_uzun = 0.0
        self.durum = 'unknown'
        self.neden = 'initial-observation'
        self.hareket_son = None
        self.acik_adim = False
        self.acik_araclar: set[str] = set()
        self.ofset = 0
        self.artik = b''
        self.adimlar = set()
        if self.kayit and self.kayit.exists():
            try:
                value = float(core.json.loads(self.kayit.read_text()).get('en_uzun', 0))
                if math.isfinite(value):
                    self.en_uzun = min(self.min_sn, max(0.0, value))
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        self.boy, self.agac, self.cpu = self._boy(), self._agac(), self._cpu()

    def _boy(self):
        import qe_core as core
        try:
            return self.logf.stat().st_size
        except OSError:
            return None

    def _agac(self):
        import qe_core as core
        if not self.wt:
            return ()
        rows = []
        try:
            def hata(exc):
                raise exc
            for kok, dirs, files in core.os.walk(self.wt, onerror=hata):
                dirs[:] = [d for d in dirs if d not in ('.git', 'node_modules', '.venv', '__pycache__')]
                for name in files:
                    path = core.pathlib.Path(kok) / name
                    if name == '.git' or path == self.logf:
                        continue
                    st = path.lstat()
                    rows.append((str(path), st.st_size, st.st_mtime_ns))
            return tuple(sorted(rows))
        except OSError:
            return None

    def _cpu(self):
        import qe_core as core
        if self.proc is None:
            return None
        try:
            result = core.subprocess.run(['ps', '-A', '-o', 'pid=,ppid=,pgid=,time='],
                                    capture_output=True, text=True, timeout=5, check=True)
            rows = {}
            for line in result.stdout.splitlines():
                pid, parent, group, value = line.split()
                seconds = 0.0
                days, sep, clock = value.partition('-')
                if sep:
                    seconds = float(days) * 86400
                else:
                    clock = days
                ticks = 0.0
                for part in clock.split(':'):
                    ticks = ticks * 60 + float(part)
                rows[int(pid)] = (int(parent), int(group), seconds + ticks)
            own = {self.proc.pid}
            group = rows.get(self.proc.pid, (0, 0, 0))[1]
            if group == self.proc.pid:
                own.update(pid for pid, row in rows.items() if row[1] == group)
            while True:
                children = {pid for pid, row in rows.items() if row[0] in own}
                if children <= own:
                    break
                own.update(children)
            return {pid: rows[pid][2] for pid in own if pid in rows}
        except (OSError, ValueError, core.subprocess.SubprocessError):
            return None

    def _istek(self, esik, simdi):
        """An open request is alive only if its last token/usage chunk (or its start) is newer than the threshold (4c-4). A request with only SSE
        comments flowing is 'connection open', not progress; past the threshold it counts as stuck (B313)."""
        import qe_core as core
        if not isinstance(self.proc, core.RecordedProcess):
            return False
        try:
            for path in (self.proc.attempt.directory / 'requests').glob('*.json'):
                isaret = core.json.loads(path.read_text())
                pid = isaret.get('pid')
                if not (type(pid) is int and core.pid_yasiyor(pid)):
                    continue
                son = max(float(isaret.get('ilerleme_at') or 0), float(isaret.get('started') or 0)) / 1000
                if not math.isfinite(son) or son > simdi:
                    return None
                if 0 <= simdi - son <= esik:
                    return True
            return False
        except (OSError, ValueError, TypeError, AttributeError):
            return None  # unreadable evidence is not permission to cut

    def _adim(self):
        import qe_core as core
        try:
            with self.logf.open('rb') as stream:
                stream.seek(self.ofset)
                chunk = stream.read(1024 * 1024)
            self.ofset += len(chunk)
        except OSError:
            return None
        lines = (self.artik + chunk).split(b'\n')
        self.artik = lines.pop()[-65536:]
        progress = False
        for raw in lines:
            try:
                event = core.json.loads(raw)
                part = event.get('part') or {}
                state = part.get('state') or {}
                if part.get('type') == 'tool':
                    anahtar = str(part.get('callID') or part.get('id') or hashlib.sha256(
                        core.json.dumps([part.get('tool'), state.get('input')], sort_keys=True).encode()).hexdigest())
                    if state.get('status') in ('running', 'pending'):
                        if len(self.acik_araclar) < 1024:
                            self.acik_araclar.add(anahtar)
                    else:
                        self.acik_araclar.discard(anahtar)
                    self.acik_adim = bool(self.acik_araclar)
                if part.get('type') != 'tool' or state.get('status') != 'completed':
                    continue
                identity = hashlib.sha256(core.json.dumps([part.get('tool'), state.get('input'), state.get('output')],
                                                         sort_keys=True).encode()).hexdigest()
                if identity not in self.adimlar and len(self.adimlar) < 1024:
                    self.adimlar.add(identity)
                    progress = True
            except (ValueError, TypeError, AttributeError):
                continue
        return progress

    def takildi(self):
        import qe_core as core
        simdi = core.time.monotonic()
        boy, agac, cpu = self._boy(), self._agac(), self._cpu()
        belirsiz = boy is None or agac is None or cpu is None
        hareket = (boy != self.boy or agac != self.agac or
                   any(value > (self.cpu or {}).get(pid, 0) for pid, value in (cpu or {}).items()))
        if hareket:
            self.hareket_son = simdi
        yakin_hareket = self.hareket_son is not None and simdi - self.hareket_son <= self.min_sn
        esik = max(self.min_sn, 3 * self.en_uzun)
        acik = self._istek(esik, core.time.time())
        adim = self._adim()
        if adim:
            self.en_uzun = min(self.min_sn, max(self.en_uzun, simdi - self.son))
            self.son = simdi
            self.durum, self.neden = 'productive', 'completed-tool-step'
        elif belirsiz or yakin_hareket or acik or acik is None or adim is None:
            self.durum, self.neden = 'unknown', 'bounded-observation'
        else:
            self.durum, self.neden = 'stalled', 'no-observed-activity'
        expired = simdi - self.son > esik
        if expired and self.durum == 'unknown' and yakin_hareket and self.acik_adim:
            expired = simdi - self.son > max(esik, self.hareket_siniri)
            self.neden = 'active-time-limit' if expired else 'bounded-active-step'
        elif expired:
            self.neden = 'uncertain-time-limit' if self.durum == 'unknown' else 'stall-time-limit'
        if self.kayit:
            core.atomic_json(self.kayit, {'en_uzun': self.en_uzun, 'durum': self.durum, 'neden': self.neden,
                                        'gozlem_siniri_sn': esik, 'hareket_siniri_sn': self.hareket_siniri,
                                        'son_kanit_monotonic': self.son, 'karar': 'stop' if expired else 'observe'})
        self.boy, self.agac, self.cpu = boy, agac, cpu
        return expired


class StallGuard:
    """STALL DETECTOR: log silent + model idle + no tool running -> cut, leave a marker.
    G-C13: if a worktree is given it also checks the approval block on every tick (all items BLOCKED -> "onay", not counted as a stall)."""

    def __init__(self, logf, proc, esik, wt=None, dongu_esik=None):
        import qe_core as core
        self.logf, self.proc, self.esik = logf, proc, esik
        self.kesti = False
        self.ertelendi = False
        self.wt = wt
        self.canlilik = core.Canlilik(logf, proc, wt, min_sn=esik)
        self.onay_sorusu = ""
        self.dongu_testi = ""
        self.dongu = core.TestDongusu(logf, int(core.ENV("CADRE_TEST_DONGU_ESIK", "5")) if dongu_esik is None else dongu_esik)  # G-F9
        self.tekrar = core.AracTekrari(logf, int(core.ENV("CADRE_ARAC_TEKRAR_ESIK", "5")))  # B200; CANLI1: the same tool 5 times = loop

    def tik(self, proje):
        # cancel flag
        import qe_core as core
        if core.cancel_path(proje).exists():
            core.log(self.logf, "cadre-guard: IPTAL bayragi gorüldu, kosu kesiliyor")
            try:
                if not core.ENV("CADRE_RUN_ID"):
                    core.cancel_path(proje).unlink()  # Legacy project-wide flag only.
            except Exception:
                pass
            core.kill_tree(self.proc.pid)
            return "iptal"
        if self.wt:
            soru = core.onay_blokesi(self.wt)
            if soru:
                self.onay_sorusu = soru
                core.log(self.logf, f"cadre-guard: ONAY-BLOKE — tum maddeler kullanici karari bekliyor: {soru[:160]}; kosu kesiliyor, teslim yok (G-C13)")
                core.kill_tree(self.proc.pid)
                return "onay"
        kimlik = self.dongu.tik()
        if kimlik:
            self.dongu_testi = kimlik
            core.log(self.logf, f"cadre-guard: TEST-DONGUSU — {kimlik} bu denemede {self.dongu.esik}+ kez kirildi; deneme kesiliyor, takilma sayilir (G-F9)")
            core.kill_tree(self.proc.pid)
            return "dongu"
        arac = self.tekrar.tik()
        if arac:
            self.dongu_testi = f"arac-tekrari:{arac}"
            core.log(self.logf, f"cadre-guard: ARAC-TEKRARI — {arac} ayni girdi ve ciktiyla {self.tekrar.esik} kez ust uste; deneme kesiliyor, takilma sayilir (B200)")
            core.kill_tree(self.proc.pid)
            return "dongu"
        if not self.canlilik.takildi():
            return None
        core.log(self.logf, f"cadre-guard: STALL — {self.canlilik.neden}; sonlu gozlem esigi asildi; ayni oturumla yol devri")
        core.pathlib.Path(str(self.logf) + ".stall").touch()
        self.kesti = True
        core.kill_tree(self.proc.pid)
        return "stall"


