#!/usr/bin/env python3
"""Live monitor: a colored stream of an unattended run, in the style of Claude Code / opencode.

Read-only: opencode DB (mode=ro) + engine log. Never touches the job; closing it leaves the job running.
  izle.py <project> [--wt PATH|latest] [--duz] [--tek] [--sure N] [--dusunce]
    --duz      no color, no sticky bottom line (test/pipe)
    --tek      print what exists and exit
    --sure N   exit after N seconds
    --ayrinti  show raw events and the engine record (--dusunce is the old alias)
Keys: q quit (the job continues) · x cancel the job (flag) · d toggle detail
"""
import opencode_adaptor as OC
import json
import os
import pathlib
import re
import select
import signal
import sqlite3
import sys
import time
from collections import deque
from ekran import simge, kosu_ozeti, arac_ozeti, sar, temiz
from kosu import RunStore, atomic_write, taban_qa_satiri
from oturum import session_ids, session_rows

QE = pathlib.Path(os.environ.get("CADRE_HOME", os.path.expanduser("~/.local/share/cadre-engineering")))
DB = pathlib.Path(os.environ.get("OPENCODE_DB", os.path.expanduser("~/.local/share/opencode/opencode.db")))

# ---- renkler (256) --------------------------------------------------------
class R:
    on = True
    @classmethod
    def c(cls, kod, s):
        return f"\033[{kod}m{s}\033[0m" if cls.on else s
    @classmethod
    def dim(cls, s): return cls.c("38;5;245", s)
    @classmethod
    def yesil(cls, s): return cls.c("38;5;42", s)
    @classmethod
    def kirmizi(cls, s): return cls.c("38;5;203", s)
    @classmethod
    def sari(cls, s): return cls.c("38;5;220", s)
    @classmethod
    def cyan(cls, s): return cls.c("38;5;44", s)
    @classmethod
    def kalin(cls, s): return cls.c("1", s)
    @classmethod
    def italik(cls, s): return cls.c("3;38;5;245", s)


def j(s):
    try:
        return json.loads(s)
    except Exception:
        return {}


def ts(ms):
    try:
        return time.strftime("%H:%M:%S", time.localtime(int(ms) / 1000))
    except Exception:
        return "--:--:--"


def sure_str(sn):
    sn = int(sn); return f"{sn // 3600:02d}:{(sn % 3600) // 60:02d}:{sn % 60:02d}"


def kisalt(s, n):
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


class Izleyici:
    def __init__(self, proje, wt, duz=False, tek=False, sure=None, dusunce=False, motor_log=None, run_id=None):
        self.proje, self.wt, self.duz, self.tek, self.sure, self.dusunce = proje, wt, duz, tek, sure, dusunce
        self.ayrinti = dusunce
        self.gecmis = deque(maxlen=200)
        self._toplanan = None
        self.son_ozet = ''
        self.motor_log = motor_log
        self.run_id = run_id
        self.tty = (not duz) and sys.stdout.isatty()
        R.on = self.tty and not os.environ.get('NO_COLOR')
        self.gorulen = {}       # part_id -> "tam" | "canli"
        self.mesaj_gorulen = set()
        self.oturum_gorulen = set()
        self.log_ofset = 0
        self.canli_satir = False
        self.duraklat = False
        self.bitti = False
        self.rc = None
        self.defter = None
        self.t0 = time.time()
        self.say = {"adim": 0, "arac": 0, "hata": 0, "comp": 0, "in": 0, "out": 0, "cost": 0.0}
        self.model = "?"
        self.izlenen = []       # B308: child runs watched in project mode
        self.cocuk_log = {}     # child coordinator.log -> offset read
        self.taban_uyarisi = ''
        self.rows, self.cols = self._boyut()

    # ---- terminal ---------------------------------------------------------
    def _boyut(self):
        try:
            s = os.get_terminal_size(); return s.lines, s.columns
        except Exception:
            return 40, 100

    def _bolge_kur(self):
        if not self.tty:
            return
        self.rows, self.cols = self._boyut()
        sys.stdout.write(f"\033[1;{self.rows - 1}r\033[{self.rows - 1};1H")
        sys.stdout.flush()

    def _bolge_sifirla(self):
        if self.tty:
            sys.stdout.write("\033[r\033[%d;1H\n" % self.rows); sys.stdout.flush()

    def yaz(self, s=""):
        if self._toplanan is not None:
            self._toplanan.append(s)
            return
        if self.canli_satir and self.tty:
            sys.stdout.write("\r\033[2K"); self.canli_satir = False
        sys.stdout.write(s + "\n")
        sys.stdout.flush()

    def ayrinti_yaz(self, s):
        self.gecmis.append(s)
        if self.ayrinti:
            self.yaz(s)

    def ayrinti_degistir(self):
        self.ayrinti = not self.ayrinti
        if self.tty:
            sys.stdout.write('\033[2J\033[H')
        self.baslik()
        if self.ayrinti:
            for line in self.gecmis:
                self.yaz(line)
        elif self.son_ozet:
            self.yaz(self.son_ozet)
        self.yaz(R.dim('Ayrıntı açık · d ile özete dön' if self.ayrinti else 'Özet · d ayrıntı'))

    def canli(self, s):
        """one-line progress line updated in place (spinner)"""
        if not self.tty:
            return
        sys.stdout.write("\r\033[2K" + kisalt(s, self.cols - 2)); sys.stdout.flush()
        self.canli_satir = True

    def alt_satir(self):
        if not self.tty:
            return
        d = (f'{simge("completed")} bitti' if self.rc == 0 else f'{simge("failed")} durdu') if self.bitti else ("○ duraklatıldı" if self.duraklat else "● çalışıyor")
        renk = (R.yesil if self.rc == 0 else R.sari) if self.bitti else (R.sari if self.duraklat else R.cyan)
        sol = f'{renk(d)} · {sure_str(time.time() - self.t0)}'
        if self.ayrinti:
            sol += (f" · adım {self.say['adim']} · araç {self.say['arac']}"
               + (f" · {R.kirmizi(str(self.say['hata']) + ' hata')}" if self.say['hata'] else "")
               + f" · in/out {self.say['in'] // 1000}k/{self.say['out'] // 1000}k · ♻{self.say['comp']} · ${self.say['cost']:.2f}")
        sag_metin = "q çık · x iptal · d ayrıntı"
        sag = R.dim(sag_metin)
        gorunur = re.sub(r"\033\[[0-9;]*m", "", sol)
        bosluk = self.cols - len(gorunur) - len(sag_metin) - 1
        if bosluk < 1:  # narrow terminal: drop the shortcuts, fit to the left
            sag, bosluk = "", 0
            gorunur = gorunur[: self.cols - 1]; sol = gorunur
        sys.stdout.write(f"\0337\033[{self.rows};1H\033[2K{sol}{' ' * bosluk}{sag}\0338"); sys.stdout.flush()

    def taban_uyar(self, record):
        uyari = taban_qa_satiri(record)
        if uyari and uyari != self.taban_uyarisi:
            self.yaz(R.sari(uyari))
        self.taban_uyarisi = uyari

    # ---- data -------------------------------------------------------------
    def db(self):
        con = OC.connect(DB); con.row_factory = sqlite3.Row
        return con

    def _repo_yolu(self):
        try:
            return (QE / "projects" / self.proje / "repo-path").read_text().strip()
        except Exception:
            return None

    def _wt_baslangic_ms(self):
        try:
            return int(pathlib.Path(self.wt).stat().st_ctime * 1000) - 120000
        except Exception:
            return 0

    def _cocuk_izle(self, record):
        # B308: in project mode the main run does no work; the active child's worktree, sessions and log are watched.
        if record.get("worktree"):
            return record["worktree"]
        try:
            cocuk = json.loads((RunStore(QE).path(self.run_id) / "proje-aktif.json").read_text()).get("cocuk")
            crec = RunStore(QE).read(cocuk)
        except Exception:
            return ""
        if cocuk not in self.izlenen:
            self.izlenen.append(cocuk)
            self.cocuk_log[str(RunStore(QE).path(cocuk) / "coordinator.log")] = 0
        return crec.get("worktree", "")

    def oturumlar(self, con):
        if self.run_id:
            ids = set()
            for rid in (self.run_id, *self.izlenen):
                try:
                    ids |= session_ids(RunStore(QE).read(rid))
                except Exception:
                    pass
            return session_rows(con, ids)
        # observation finding: for registered projects opencode writes the session `directory` as the MAIN REPO path,
        # not the worktree. Matching: worktree path OR project repo path (sessions started after the worktree opened).
        repo = self._repo_yolu(); t0 = self._wt_baslangic_ms()
        if repo:
            rows = con.execute("select * from session where (directory = ? or directory like ?) or (directory = ? and time_created >= ?) order by time_created",
                               (self.wt, self.wt.rstrip("/") + "/%", repo, t0)).fetchall()
        else:
            rows = con.execute("select * from session where directory = ? or directory like ? order by time_created",
                               (self.wt, self.wt.rstrip("/") + "/%")).fetchall()
        ids = {r["id"] for r in rows}
        while ids:
            q = ",".join("?" * len(ids))
            yeni = [r for r in con.execute(f"select * from session where parent_id in ({q})", tuple(ids)).fetchall() if r["id"] not in {x["id"] for x in rows}]
            if not yeni:
                break
            rows += yeni; ids = {r["id"] for r in yeni}
        return rows

    # ---- drawing ------------------------------------------------------------
    def baslik(self):
        dal = ""
        try:
            import subprocess
            if self.wt:
                dal = subprocess.run(["git", "-C", self.wt, "branch", "--show-current"], capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            pass
        self.yaz(R.cyan("┃ ") + R.kalin(f"cadre · {temiz(self.proje)}"))
        if self.ayrinti:
            self.yaz(R.dim(f'{dal} · {self.wt}'))
        if self.run_id and self.ayrinti:
            self.yaz(R.dim(f"run: {self.run_id}" + (" · worktree hazırlanıyor" if not self.wt else "")))
        self.yaz(R.cyan("┃ ") + R.dim("salt-okur izleyici — kapatınca iş sürer · iptal için x"))
        self.yaz()

    def gorev_blok(self, metin):
        satirlar = sar(metin.strip(), self.cols - 4)
        for i, l in enumerate(satirlar[:12]):
            self.yaz(R.cyan("▌ ") + (R.kalin(l) if i == 0 else l))
        if len(satirlar) > 12:
            self.yaz(R.cyan("▌ ") + R.dim(f"… (+{len(satirlar) - 12} satır)"))
        self.yaz()

    def part_ciz(self, s, m, d, girinti):
        lines = []
        self._toplanan = lines
        try:
            self._part_ayrinti(s, m, d, girinti)
        finally:
            self._toplanan = None
        for line in lines:
            self.ayrinti_yaz(line)
        if self.ayrinti:
            return
        kind = d.get('type')
        if kind == 'tool':
            self.yaz(girinti + arac_ozeti(d))
        elif kind in ('text', 'reasoning') and d.get('text', '').strip():
            self.yaz(girinti + (f'{simge("dusunme")} görev değerlendiriliyor' if kind == 'reasoning'
                              else f'{simge("completed")} yanıt hazır · ayrıntı: d'))
        elif kind == 'patch':
            self.yaz(f'{simge("yama")} {len(d.get("files") or [])} dosya değişti')
        elif kind == 'compaction':
            self.yaz(f'{simge("sikistirma")} konuşma kısaltıldı')

    def _part_ayrinti(self, s, m, d, girinti):
        t = d.get("type"); g = girinti
        if t == "reasoning":
            metin = (d.get("text") or "").strip()
            if not metin:
                return
            satirlar = metin.splitlines()
            gost = satirlar[:200]
            self.yaz(g + R.italik("✻ Düşünme"))
            for l in gost:
                for line in sar(l, self.cols - len(g) - 4):
                    self.yaz(g + '  ' + R.italik(line))
            if len(satirlar) > len(gost):
                self.yaz(g + "  " + R.dim(f"… (+{len(satirlar) - len(gost)} satır; tamamı defterde)"))
        elif t == "text":
            metin = (d.get("text") or "").strip()
            if not metin:
                return
            for l in sar(metin, self.cols - len(g) - 2)[:200]:
                self.yaz(g + l)
            if metin.count("\n") > 60:
                self.yaz(g + R.dim("… (uzun metin kısaltıldı; tamamı defterde)"))
        elif t == "tool":
            st = d.get("state") or {}; durum = st.get("status"); inp = st.get("input") or {}
            ad = d.get("tool") or "?"
            ozet = inp.get("command") or inp.get("filePath") or inp.get("pattern") or inp.get("description") or st.get("title") or ""
            if ad == "task":
                ozet = f"{inp.get('subagent_type', '?')} — {kisalt(inp.get('description') or inp.get('prompt') or '', 60)}"
            self.say["arac"] += 1
            basla = (st.get("time") or {}).get("start"); bit = (st.get("time") or {}).get("end")
            sure = f"{(bit - basla) / 1000:.1f}s" if basla and bit else ""
            for line in sar(f"⏺ {ad.capitalize()} {ozet}", self.cols - len(g) - 2):
                self.yaz(g + line)
            if durum == "error":
                self.say["hata"] += 1
                err = (st.get("error") or "hata").splitlines()[0]
                self.yaz(g + "  ⎿ " + R.kirmizi("✗ ") + R.kirmizi(kisalt(err, self.cols - len(g) - 8)))
            elif durum == 'completed':
                out = st.get("output") or ""
                n = out.count("\n") + (1 if out and not out.endswith("\n") else 0)
                meta = st.get("metadata") or {}
                ek = f" · exit {meta.get('exit')}" if isinstance(meta, dict) and meta.get("exit") not in (None, 0) else ""
                self.yaz(g + "  ⎿ " + R.yesil("✓ ") + R.dim(f"{sure} · {n} satır{ek}".strip(" ·")))
                for l in sar(out, self.cols - len(g) - 6)[:200]:
                    self.yaz(g + "    " + R.dim(l))
                if n > 200:
                    self.yaz(g + "    " + R.dim("… (tamamı defterde)"))
        elif t == "patch":
            dosyalar = d.get("files") or []
            self.yaz(g + R.cyan("📝 yama ") + R.dim(", ".join(pathlib.Path(f).name for f in dosyalar[:5])))
        elif t == "compaction":
            self.say["comp"] += 1
            self.yaz(g + R.sari(f"♻ context sıkıştırıldı (#{self.say['comp']})"))
        elif t == "step-finish":
            tk = d.get("tokens") or {}
            self.say["adim"] += 1
            self.say["in"] += int(tk.get("input") or 0); self.say["out"] += int(tk.get("output") or 0)
            self.say["cost"] += float(d.get("cost") or 0)

    # ---- dongu ------------------------------------------------------------
    def tur(self):
        if not DB.exists():
            return
        con = self.db()
        try:
            ses = self.oturumlar(con)
            for s in ses:
                if s["id"] not in self.oturum_gorulen:
                    self.oturum_gorulen.add(s["id"])
                    mdl = j(s["model"]) if s["model"] else {}
                    mname = f"{mdl.get('providerID', '?')}/{mdl.get('id', '?')}" if mdl else "?"
                    if s["parent_id"]:
                        self.ayrinti_yaz("  " + R.cyan(f"↳ {s['agent'] or 'alt-ajan'} · {mname}"))
                    else:
                        self.model = mname
                        self.ayrinti_yaz(R.dim(f"oturum {s['id'][:12]} · {s['agent']} · {mname}"))
            if not ses:
                return
            q = ",".join("?" * len(ses))
            parts = con.execute(
                f"select p.*, m.data as mdata, s.parent_id as parent from part p join message m on m.id=p.message_id "
                f"join session s on s.id=p.session_id where p.session_id in ({q}) order by p.time_created", tuple(r["id"] for r in ses)).fetchall()
            son_id = parts[-1]["id"] if parts else None
            for p in parts:
                if self.gorulen.get(p["id"]) == "tam":
                    continue
                d = j(p["data"]); md = j(p["mdata"]); g = "    " if p["parent"] else ""
                if p["message_id"] not in self.mesaj_gorulen:
                    self.mesaj_gorulen.add(p["message_id"])
                    if md.get("role") == "user":
                        # task text: the text parts of this message
                        metin = "\n".join((j(x["data"]).get("text") or "") for x in con.execute(
                            "select data from part where message_id=? order by time_created", (p["message_id"],)))
                        self.gorev_blok(metin) if not p["parent"] else self.ayrinti_yaz(g + R.cyan("▌ ") + R.dim(kisalt(temiz(metin), self.cols - 8)))
                if md.get("role") == "user":
                    self.gorulen[p["id"]] = "tam"; continue
                t = d.get("type")
                bitmis = True
                # C25: completion is decided by STATE, not by TIME. EVERY running tool (not just the last part) stays live;
                # a long `make check` is not painted green because it stayed quiet for 20 s. Text/thinking: live if it is the last part with no end stamp.
                if not self.tek:
                    if t == "tool" and (d.get("state") or {}).get("status") not in ("completed", "error"):
                        bitmis = False
                    elif p["id"] == son_id and t in ("reasoning", "text") and not md.get("finish") and not (d.get("time") or {}).get("end"):
                        bitmis = False
                if not bitmis:
                    if t == "tool":
                        st = d.get("state") or {}; inp = st.get("input") or {}
                        basla = (st.get("time") or {}).get("start") or p["time_created"]
                        if self.ayrinti:
                            self.canli(g + R.kalin(f"⏺ {(d.get('tool') or '?').capitalize()} ") + kisalt(temiz(inp.get("command") or inp.get("filePath") or st.get("title") or ""), 60)
                                       + R.dim(f"  … çalışıyor {int(time.time() - basla / 1000)}s"))
                        else:
                            self.canli(g + arac_ozeti(d))
                    else:
                        etiket = "✻ düşünüyor" if t == "reasoning" else "✎ yazıyor"
                        self.canli(g + R.italik(f"{etiket} … {len(d.get('text') or '')} karakter"))
                    self.gorulen[p["id"]] = "canli"
                    continue
                self.part_ciz(None, md, d, g)
                self.gorulen[p["id"]] = "tam"
        finally:
            con.close()

    def motor_tur(self):
        if not self.motor_log:
            # find the engine log of the wt; otherwise the dispatcher/orchestra log (stage 10: lanes write to calisan/*.log)
            for kalip in (f"is-{self.proje}-*.log", f"dispatch-{self.proje}-*.log", f"orkestra-{self.proje}-*.log"):
                for f in sorted((QE / "logs").glob(kalip), key=lambda x: x.stat().st_mtime, reverse=True)[:5]:
                    try:
                        if self.wt in f.read_text(errors="replace") or kalip.startswith("orkestra"):
                            self.motor_log = str(f); break
                    except Exception:
                        pass
                if self.motor_log:
                    break
            if not self.motor_log:
                return
        try:
            with open(self.motor_log, errors="replace") as f:
                f.seek(self.log_ofset); yeni = f.read(); self.log_ofset = f.tell()
        except Exception:
            yeni = ""
        for yol, ofs in list(self.cocuk_log.items()):
            try:
                with open(yol, errors="replace") as f:
                    f.seek(ofs); yeni += f.read(); self.cocuk_log[yol] = f.tell()
            except Exception:
                pass
        for l in yeni.splitlines():
            m = re.match(r"\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2}) (.*)", l)
            if not m:
                self.ayrinti_yaz(temiz(l))
                continue
            self.ayrinti_yaz(R.dim(temiz(l)))
            saat, msg = m.group(1), m.group(2)
            if msg.startswith("BITTI") and not self.run_id:
                self.bitti = True
            if "kosu defteri:" in msg:
                self.defter = msg.split("kosu defteri:", 1)[1].strip()
            if msg.startswith("kosu kendi bitti") and not self.run_id:
                self.rc = 0
            if re.search(r"cadre-guard|BEKCI|KURTARICI|KOTA|HATA-DONGUSU|TAKILDI|IPTAL|deneme siniri|devam ediliyor|kosu dustu|ara commit|son commit|model kapali|model geri|kosu basliyor|worktree:|prompt  :", msg):
                renk = R.kirmizi if re.search(r"TAKILDI|KESTI|HATA|dustu|birak", msg) else (R.yesil if re.search(r"commit|geri geldi|basliyor", msg) else R.sari)
                if not self.ayrinti:
                    if re.search(r'TAKILDI|HATA|dustu|birak', msg):
                        self.yaz(R.kirmizi(f'{simge("failed")} iş durdu; ayrıntı: d'))
                    elif 'IPTAL' in msg:
                        self.yaz(R.sari(f'{simge("iptal")} iptal işleniyor'))

    def tus(self):
        if not self.tty:
            return
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if not r:
            return
        try:
            k = os.read(sys.stdin.fileno(), 1).decode(errors="ignore")
        except Exception:
            return
        if k in ("q", "Q"):
            raise KeyboardInterrupt
        if k in ("x", "X"):
            path = RunStore(QE).path(self.run_id) / "cancel.flag" if self.run_id else QE / "zincir" / f"{self.proje}-iptal.flag"
            atomic_write(path, "requested\n")
            self.yaz(R.kirmizi("⚑ iptal bayrağı bırakıldı — motor birkaç saniye içinde kesecek"))
        if k in ('d', 'D', 't', 'T'):
            self.ayrinti_degistir()

    def kos(self):
        import termios, tty
        eski = None
        if self.tty:
            try:
                eski = termios.tcgetattr(sys.stdin); tty.setcbreak(sys.stdin.fileno())
            except Exception:
                self.tty = False
        signal.signal(signal.SIGWINCH, lambda *_: self._bolge_kur())
        if self.tty:
            sys.stdout.write("\033[2J\033[H")  # clean page: no leftover screen clutter
        self._bolge_kur()
        self.baslik()
        try:
            bas = time.time()
            while True:
                if self.run_id:
                    record = RunStore(QE).read(self.run_id)
                    summary = kosu_ozeti(record)
                    if summary != self.son_ozet:
                        self.yaz(summary)
                        self.son_ozet = summary
                    self.taban_uyar(record)
                    self.wt = self._cocuk_izle(record)
                    self.bitti = record["state"] in ("completed", "cancelled", "failed", "retryable")
                    self.rc = record.get("exit_code")
                if not self.duraklat:
                    if self.wt:
                        self.tur()
                    self.motor_tur()
                self.alt_satir(); self.tus()
                if self.bitti:
                    self.yaz(); self.yaz((R.yesil("✔ BİTTİ") if self.rc == 0 else R.sari("⚑ DURDU")) + (R.dim(f" rc={self.rc}") if self.rc is not None else "") + (R.dim(f" · defter: {self.defter}") if self.defter and self.ayrinti else ""))
                    self.alt_satir()
                    if not self.tek:
                        time.sleep(1.5)
                    break
                if self.tek or (self.sure and time.time() - bas >= self.sure):
                    break
                time.sleep(1.0)
        except KeyboardInterrupt:
            self.yaz(R.dim("izleyici kapandı — iş arka planda sürüyor"))
        finally:
            if eski is not None:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, eski)
            self._bolge_sifirla()


def son_worktree(proje, bekle=0):
    kok = QE / "worktrees" / proje
    son = time.time() + bekle
    while True:
        adaylar = sorted([d for d in kok.glob("*/") if d.is_dir()], key=lambda d: d.stat().st_mtime, reverse=True) if kok.exists() else []
        if adaylar:
            return str(adaylar[0]).rstrip("/")
        if time.time() >= son:
            return None
        time.sleep(1)


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__, file=sys.stderr); sys.exit(2)
    proje = a[0]; wt = "latest"; duz = "--duz" in a; tek = "--tek" in a; dusunce = "--dusunce" in a or "--ayrinti" in a; sure = None; ml = None; run_id = None
    for i, x in enumerate(a):
        if x == "--wt": wt = a[i + 1]
        if x == "--sure": sure = float(a[i + 1])
        if x == "--motor-log": ml = a[i + 1]
        if x == "--run": run_id = a[i + 1]
    if run_id:
        record = RunStore(QE).read(run_id)
        if record["project"] != proje:
            raise ValueError("run/project mismatch")
        wt = record.get("worktree", "")
        ml = str(RunStore(QE).path(run_id) / "coordinator.log")
    if wt == "latest":
        wt = son_worktree(proje, bekle=0 if tek else 90)
        if not wt:
            print("worktree bulunamadı (iş henüz başlamadı mı?)", file=sys.stderr); sys.exit(1)
    Izleyici(proje, wt, duz=duz, tek=tek, sure=sure, dusunce=dusunce, motor_log=ml, run_id=run_id).kos()
