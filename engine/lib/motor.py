"""Engine: task clocks, progress watch and executor lifecycle. S1: engine responsibility; shared services come from the qe_core interface."""
from __future__ import annotations
import re
import math


def sure_kaydi(store, run_id, task_id, warn, abort):
    import qe_core as core
    now = core.time.time()
    def bind(task):
        clock = task.setdefault('motor_clock', {'started': now, 'warning_seconds': warn, 'limit_seconds': abort})
        if (not isinstance(clock, dict) or any(type(clock.get(key)) not in (int, float)
                or not math.isfinite(clock[key]) for key in ('started', 'warning_seconds', 'limit_seconds'))
                or not 0 < clock['started'] <= now or clock['warning_seconds'] < 0 or clock['limit_seconds'] <= 0):
            raise ValueError('motor-clock-invalid')
        clock['limit_seconds'] = min(clock['limit_seconds'], abort)
        clock['warning_seconds'] = min(clock['warning_seconds'], warn, clock['limit_seconds'])
    task = store.task_mutate(run_id, task_id, bind)
    clock = task['motor_clock']
    record = store.read(run_id)
    parents = set()
    pauses = []
    while True:
        # N-RUN02/N-RUN04: only owned processes confirmed closed
        # separate the stop from the recorded continue; retry/wait does not reset the clock.
        for change in record.get('plan_changes', []):
            if not isinstance(change, dict):
                raise ValueError('motor-clock-pause-invalid')
            if change.get('status') != 'resumed':
                continue
            stop = change.get('stop') or {}
            if (not isinstance(stop, dict) or not isinstance(stop.get('runs'), list)
                    or not isinstance(change.get('events'), list)
                    or any(not isinstance(event, dict) for event in change['events'])):
                raise ValueError('motor-clock-pause-invalid')
            if run_id not in stop['runs']:
                continue
            resumed = [event.get('ts') for event in change.get('events', []) if event.get('action') == 'resumed']
            began = stop.get('ts')
            if (len(resumed) != 1 or any(type(value) not in (int, float) or not math.isfinite(value)
                                       for value in (began, *resumed))
                    or not 0 < began <= resumed[0] <= now):
                raise ValueError('motor-clock-pause-invalid')
            pauses.append((max(clock['started'], began), resumed[0]))
        if not record.get('proje_ust'):
            break
        if record['run_id'] in parents:
            raise ValueError('motor-clock-parent-cycle')
        parents.add(record['run_id'])
        record = store.read(record['proje_ust'])
    paused, cursor = 0, clock['started']
    for began, resumed in sorted(pauses):
        paused += max(0, resumed - max(cursor, began))
        cursor = max(cursor, resumed)
    end = clock['started'] + clock['limit_seconds'] + paused
    warning_end = clock['started'] + clock['warning_seconds'] + paused
    if record.get('request'):
        from kosu import istek_kalan_sn
        remaining = istek_kalan_sn(record, now)
        authorized_end = now + remaining
        if not math.isfinite(authorized_end):
            raise ValueError('motor-clock-invalid')
        if (record.get('request_clock') or {}).get('oturum') is not None:
            # A sub-task's work cannot be longer than the request's whole accumulated
            # work. The smaller of the two upper bounds keeps the old cap without counting waits.
            total_work = record['request']['duration_minutes'] * 60 - remaining
            if not math.isfinite(total_work) or total_work < 0:
                raise ValueError('motor-clock-invalid')
            elapsed = min(max(0, now - clock['started'] - paused), total_work)
            end = now + clock['limit_seconds'] - elapsed
            warning_end = now + clock['warning_seconds'] - elapsed
        end = min(end, authorized_end)
    elif record.get('operator_model_permission'):
        from izin import read_action
        action = read_action(store, record['run_id'], record['operator_model_permission'])
        authorized_end = action['finished'] + action['identity']['target']['duration_minutes'] * 60
        if not math.isfinite(authorized_end):
            raise ValueError('motor-clock-invalid')
        end = min(end, authorized_end)
    if record.get('project_deadline') is not None:
        if not math.isfinite(record['project_deadline']):
            raise ValueError('motor-clock-invalid')
        end = min(end, record['project_deadline'])
    mono = core.time.monotonic()
    return (mono + max(0, warning_end - now),
            mono + max(0, end - now), clock)


def motor(proje, pf):
    import qe_core as core
    if not core.ENV("CADRE_RUN_ID"):
        # Legacy direct invocation also enters the durable coordinator.
        store = core.RunStore(core.QE)
        record = store.accept(proje, core.pathlib.Path(pf).read_text())
        previous = core.os.environ.get("CADRE_DISPATCH")
        core.os.environ["CADRE_DISPATCH"] = "0"
        try:
            return core.execute_run(store, record["run_id"])
        finally:
            if previous is None:
                core.os.environ.pop("CADRE_DISPATCH", None)
            else:
                core.os.environ["CADRE_DISPATCH"] = previous
    run_id = core.ENV("CADRE_RUN_ID")
    store = core.RunStore(core.QE)
    if store.read(run_id)["project"] != proje:
        raise ValueError("run/project mismatch")
    task_id = core.ENV("CADRE_GOREV_ID", "") or "main"
    lock = core.FileLock(store.task_dir(run_id, task_id) / ".owner.lock")
    if not lock.acquire(blocking=False):
        return 75
    previous = core.os.environ.get("CADRE_OWNER_FDS", "")
    try:
        core.os.environ["CADRE_OWNER_FDS"] = ",".join(str(fd) for fd in (*core.owner_fds(), lock.fd))
        store.task_update(run_id, task_id, owner_pid=core.os.getpid(), state="running")
        return core._motor(proje, pf)
    finally:
        store.task_update(run_id, task_id, owner_pid=None)
        core.os.environ["CADRE_OWNER_FDS"] = previous
        lock.close()


def _motor(proje, pf):
    import qe_core as core
    real = core.ENV("CADRE_REAL_OPENCODE", "/opt/homebrew/bin/opencode")
    gorev_id = core.ENV("CADRE_GOREV_ID", "")
    model = core.ENV("CADRE_RUN_MODEL", "")
    try:
        warn, abort_s = core.SINIR.motor_sureleri()
    except (ValueError, TypeError):
        print('MOTOR SURE AYARI/KAYDI GECERSIZ', file=core.sys.stderr)
        return 2
    max_resume = int(core.ENV("CADRE_MAX_RESUME", "12"))
    stuck_max = int(core.ENV("CADRE_STUCK_MAX", "2"))
    stall_sec = int(core.ENV("CADRE_STALL_SEC", "600"))
    # Tick 15 s: the cheap layer (brake, quota, cancel, stuck) on every tick; the budget observation via `observed_budget` with a 60 s
    # cache, the escalation check every 4 ticks (~60 s).
    poll = int(core.ENV("CADRE_PROGRESS_POLL", "15"))

    pf_p = core.pathlib.Path(pf)
    if not pf_p.is_file() or not pf_p.read_text().strip():
        print(f"BOS PROMPT: {pf}", file=core.sys.stderr)
        return 2

    ts = core.time.strftime("%Y%m%d-%H%M%S")
    logf = core.task_root(gorev_id) / "motor.log"
    pidp = core.task_root(gorev_id) / "worker.pid"
    core.atomic_write(pidp, str(core.os.getpid()))
    logf.parent.mkdir(parents=True, exist_ok=True)
    logf.touch()

    # WORKTREE
    wt = core.RunStore(core.QE).read(core.ENV("CADRE_RUN_ID")).get("worktree") or core.ENV("CADRE_WORKTREE", "")
    if wt:
        if not (core.pathlib.Path(wt) / ".git").exists():
            print(f"CADRE_WORKTREE gecersiz: {wt}", file=core.sys.stderr)
            return 2
    else:
        r = core.subprocess.run(["bash", "-c",
                            '. "$1"; qe_make_worktree "$2" "$3"', "qe", str(core.CODE / "lib/common.sh"), proje, f"is {ts}"],
                           capture_output=True, text=True)
        wt = r.stdout.strip()
        if not wt:
            core.log(logf, "worktree acilamadi, 30sn sonra WT RETRY")
            try:
                core.wait_pause(30, proje)
            except core.CancelledRun:
                core.qe_durum(gorev_id, "iptal", "kullanici")
                return 1
            r = core.subprocess.run(["bash", "-c",
                                '. "$1"; qe_make_worktree "$2" "$3"', "qe", str(core.CODE / "lib/common.sh"), proje, f"is {ts}"],
                               capture_output=True, text=True)
            wt = r.stdout.strip()
            if not wt:
                print("worktree acilamadi (2 deneme)", file=core.sys.stderr)
                return 2
    g = core.subprocess.run(["bash", "-c", '. "$1"; qe_verify_ai_worktree "$2" "$3"',
                        "qe", str(core.CODE / "lib/common.sh"), wt, proje],
                       capture_output=True)
    if g.returncode != 0:
        print("guard dogrulanamadi", file=core.sys.stderr)
        return 2
    core.RunStore(core.QE).update(core.ENV("CADRE_RUN_ID"), worktree=wt)
    if core.ENV("CADRE_RUN_ID"):  # G-C14: install command + baseline QA information, once per run, while there is no product
        _tb = core.taban_hazirlik(proje, wt, logf, core.RunStore(core.QE).path(core.ENV("CADRE_RUN_ID")) / "taban-qa.json", core.QE / "projects" / proje)
        from kosu import taban_qa_kaydet
        taban_qa_kaydet(core.RunStore(core.QE), core.ENV("CADRE_RUN_ID"), _tb)
        if _tb.get("status") == "FAIL":
            core.log(logf, f"BASLANGIC KAPISI: {_tb.get('reason') or 'baseline-unavailable'} — koşu bırakılıyor")
            core.qe_durum(gorev_id, "yarim", _tb.get('reason') or 'baseline-unavailable')
            core.RunStore(core.QE).update(core.ENV("CADRE_RUN_ID"), reason=f"{_tb.get('reason') or 'baseline-unavailable'}: rc={(_tb.get('kurulum') or {}).get('exit_code')}")
            return 1

    try:
        warning_at, deadline, clock = sure_kaydi(core.RunStore(core.QE), core.ENV('CADRE_RUN_ID'),
                                               gorev_id or 'main', warn, abort_s)
    except (ValueError, TypeError):
        core.log(logf, 'MOTOR SURE KAYDI GECERSIZ: yeni yurutucu yok')
        core.qe_durum(gorev_id, 'yarim', 'motor-clock-invalid')
        return 2
    core.bekci_dirilt()
    # C30: -s = do not sleep when the lid is closed either. P2: under a coordinator (CADRE_KOORDINATOR_PID) caffeinate is single IN THE COORDINATOR and
    # covers the whole lifetime including plan/lane/acceptance; an independent engine (old path/test) keeps its own.
    caf = None if core.ENV("CADRE_KOORDINATOR_PID") else core.subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(core.os.getpid())])
    # PERIODIC CKPT: an intermediate commit every 10 min (crash protection) — in task-id-less (single-run) mode.
    ckpt = None
    if not gorev_id:
        ckpt = core.subprocess.Popen(["bash", "-c",
            f'while sleep 600; do . "{core.CODE}/lib/common.sh"; n="$(qe_commit_wip "{wt}" "{proje}" "ara kayit" 2>/dev/null)"; '
            f'[ -n "$n" ] && echo "$(date "+%F %T") ara commit: $n dosya" >> "{logf}"; done'])
    imza_manifest = core.subprocess.run(["bash", str(core.CODE / "bin/kisitli-kabuk.sh"), "imza", str(core.os.getpid())],
                                   capture_output=True, text=True).stdout   # B288: the root of trust is memory the executor cannot write
    core.AKTIF_WT = wt
    _eski = core.QE / "zincir" / f"{proje}-iptal.flag"
    if _eski.exists() and not gorev_id and not core.ENV("CADRE_RUN_ID"):  # a new job starts: a cancel flag left from the previous job is invalid (except dispatcher lanes)
        try:
            _eski.unlink(); core.log(logf, "eski iptal bayragi temizlendi (onceki ise aitti)")
        except Exception:
            pass
    core.log(logf, f"worktree: {wt}")
    core.log(logf, f"prompt  : {pf} ({pf_p.stat().st_size} bayt)")

    saved_task = core.RunStore(core.QE).read(core.ENV("CADRE_RUN_ID"))["tasks"].get(gorev_id or "main", {})
    tries = int(saved_task.get("failure_count", 0))
    stuck = 0
    GO_RESCUE_USED = False  # one-shot recovery (quota kept); for later jobs the chain decides on Go
    rc = 0
    produced = False
    proc = None
    # RESTRICTED MODE: Go share exhausted -> sub-agents from the provider ladder (next links of the chain).
    # NO privacy restriction: cloud is used while there is budget.
    kisit_env = None
    bans = core.task_bans(core.QE)
    bans.refresh(core.butce_json())
    yasak = bans.blocked()
    izin = core.izin_ayikla(pf_p.read_text())
    if izin:
        core.log(logf, f"İZİN (gorev metni): {', '.join(sorted(izin))} — bu kosuda serbest")
    _k, _bt, _kn = core.go_kisit_gerekli()
    is_basi_basamak = None
    if _k:
        _p, _m, _b = core.kisit_config_yaz("go", butce=_bt, izin=izin)
        kisit_env = {"OPENCODE_CONFIG": _p}
        is_basi_basamak = _b
        # B94 + B131: the brake closes either on share-exhausted or accounting-unreadable; the message
        # said "share exhausted" in EVERY case. The reason now comes from the brake ITSELF (`_kn`).
        _sebep = ("Go payi bitti" if _kn == "pay-bitti"
                  else f"Go muhasebesi okunamadi ({_kn.split(':', 1)[1].strip() if ':' in _kn else _kn}) — gecici olabilir, periyodik yeniden denenir"
                  if _kn.startswith("okunamadi") else
                  "butce gozlemi okunamadi (gecici olabilir; periyodik yeniden denenir)"
                  if _b == "butce-yok" else "Go payi bitti")
        core.log(logf, f"GO-KISIT (is basi): {_sebep} — alt-ajanlar merdiven: {_b} -> {_m}")
    elif izin:
        _p, _, _ = core.kisit_config_yaz('izin', izin=izin, go_var=True)
        kisit_env = {'OPENCODE_CONFIG': _p}
    kisit_kes = False
    butce_cache = None  # Shared observation is cached by observed_budget for 60s.
    kontrollu_kes = False  # brake/escalation interruptions are not written to the attempt count (lossless -c)
    son_kisit_model = None
    son_kisit_basamak = is_basi_basamak   # B93: if 'butce-yok', the restriction is treated as TEMPORARY and retried periodically
    if kisit_env and is_basi_basamak is not None:
        son_kisit_model = _m
    go_kisit_aktif = kisit_env is not None and not izin  # if a Go restriction was set up at job start
    if kisit_env is not None:
        _kk, _, _ = core.go_kisit_gerekli()
        go_kisit_aktif = bool(_kk)
    hata_prov = hata_model = ""
    cikti_ikiye = False   # 4c-4: on truncation, the 2× cap allowance once in this task
    hizli = core.HizliDusus(int(core.ENV("CADRE_HIZLI_DUSUS_SN", "60")), 3)   # B179
    kontrollu_tavan = int(core.ENV("CADRE_KONTROLLU_TAVAN", "6"))   # B188: consecutive controlled interruptions count as an attempt after this number
    ardisik_kontrollu = 0
    stall_env = {}   # addition to the continue prompt and one-off output multiplier (4c-4); no helper agent (4c-9)
    birakma_nedeni = ""  # controlled release reason (like session-id); written with rc if empty
    def kritik_dogrula():
        guard = core.subprocess.run(["bash", str(core.CODE / "bin/kisitli-kabuk.sh"), "dogrula", str(core.os.getpid()), "-"], input=imza_manifest,
                                    capture_output=True, text=True)
        for l in guard.stdout.splitlines():
            core.log(logf, l)
        return guard.returncode
    try:
        core.kabul_guvencesi(logf)
    except core.BudgetDenied:
        core.qe_durum(gorev_id, 'yarim', 'butce-bekleme')
        if caf:
            caf.terminate()
            core.reap(caf, timeout=2)
        if ckpt:
            core.kill_tree(ckpt.pid, grace=1)
            core.reap(ckpt, timeout=2)
        pidp.unlink(missing_ok=True)
        kritik_dogrula()
        return 75
    try:
        while True:
            if core.time.monotonic() >= deadline:
                birakma_nedeni = 'sure-tavani'
                core.log(logf, 'SURE TAVANI: kayitli motor suresi doldu; yeni yurutucu yok')
                core.RunStore(core.QE).task_update(core.ENV('CADRE_RUN_ID'), gorev_id or 'main',
                                                 time_decision='sure-tavani')
                break
            task = core.RunStore(core.QE).read(core.ENV('CADRE_RUN_ID'))['tasks'].get(gorev_id or 'main', {})
            if task.get('yol_bekleyen'):
                model = core.yol_devret(task['yol_bekleyen'], logf, 'bekleyen ayni model yolu', model_devri=False, bekleyen=True)
                if not model:
                    birakma_nedeni = 'butce-bekleme'
                    core.butce_bekleme_kaydet(logf)
                    break
            if not model:
                model = core.bulut_aday('impl', yasak, wt)
                if not model:
                    birakma_nedeni = 'butce-bekleme'
                    core.butce_bekleme_kaydet(logf)
                    break
            # P2 DISK THRESHOLD: if low, no new executor starts (cancel-aware, capped wait)
            _disk = core.disk_bekle(logf, proje, deadline=deadline)
            if _disk is None:
                core.qe_durum(gorev_id, "iptal", "kullanici")
                core.log(logf, "IPTAL: disk beklenirken kullanici bayragi — is birakiliyor")
                return 1
            if core.time.monotonic() >= deadline:
                continue
            if _disk is False:
                core.qe_durum(gorev_id, "yarim", "disk-low")
                birakma_nedeni = "disk-low"
                break

            # The adapter decides continuation from its durable session receipt,
            # independently of the failure limit. Preserve the original task for
            # an explicit handoff when changing between Claude and OpenCode.
            started = core.RunStore(core.QE).read(core.ENV("CADRE_RUN_ID"))["tasks"].get(gorev_id or "main", {}).get("attempt_count", 0) > 0
            gorev_metni = pf_p.read_text()
            if not started:
                if not model.startswith("claude/") and not gorev_id:  # in lane mode the plan is in the dispatcher
                    _kb, _bt, _kbn = core.go_kisit_gerekli()
                    bp = core.on_plan(real, wt, proje, gorev_metni, logf, _bt, env_ek=kisit_env)
                    if bp:
                        gorev_metni = (f"BRIFING HAZIR: `{bp}` — once bunu oku, MADDELER'i `.gorevler.json`'a aktar (ONAY: ile baslayanlari "
                                       f"ve karara bagli her maddeye `\"status\": \"BLOKE: onay\"` yaz; TUM maddeler BLOKE ise urune DOKUNMA, tek satir 'BLOKE: onay — <soru>' yazip BITIR), "
                                       f"sonra yapilabilir maddeleri sirayla yap. cadre-plan'dan YENIDEN brifing isteme.\n\n") + gorev_metni
                core.log(logf, "kosu basliyor (opencode run)")
            else:
                core.log(logf, "devam: adaptorde kayitli oturum veya acik yurutucu devri; hata sayacindan bagimsiz")
            task = core.RunStore(core.QE).read(core.ENV("CADRE_RUN_ID"))["tasks"].get(gorev_id or "main", {})
            if task.get('dongu_yonlendirildi'):
                stall_env['CADRE_DEVAM_EK'] = "\nAyni arac cagrisini tekrarlama. Farkli bir yol dene; yalniz kalan isi bitir."
            args = (["-m", model] if model else []) + [gorev_metni]

            try:
                if model.startswith("claude/"):
                    _m, _, _e = model.partition("#")
                    _mm=_m.split('/',1)[1]; _tavan=core.ENV("CADRE_CLAUDE_BUDGET", {"opus":"5.0","fable":"8.0"}.get(_mm.split("-")[0],"2.0"))
                    core.log(logf, f"claude seridi basliyor: istek model={_mm} effort={_e or 'varsayilan'} butce=${_tavan}")
                    try:
                        proc = core.run_claude(wt, model, pf_p.read_text(), logf)
                    except core.BudgetDenied as exc:
                        core.log(logf, f'Claude butce bilinmiyor/tukendi ({exc}); bulut kapasitesi bekleniyor')
                        model = ''
                        continue
                else:
                    proc = core.run_opencode(real, wt, args, logf, env_ek={**(kisit_env or {}), **stall_env,
                                                                       'CADRE_SURE_SON_TS': core.sure_sonu(deadline - core.time.monotonic())})
                    stall_env.pop('CADRE_CIKTI_KAT', None)   # the 2× cap is only for the single attempt after a truncation (4c-4)
            except core.BudgetDenied:
                if model.startswith('opencode-go/'):
                    model = core.yol_devret(model, logf, 'Go yeni is kabul etmedi', model_devri=False)
                    if model:
                        continue
                birakma_nedeni = 'butce-bekleme'
                core.butce_bekleme_kaydet(logf)
                break
            except core.CancelledRun:
                core.qe_durum(gorev_id, "iptal", "kullanici")
                core.log(logf, "IPTAL: yurutucu slotu beklenirken kullanici bayragi — is birakiliyor")
                return 1
            attempt_model = proc.attempt.data["model"] if isinstance(proc, core.RecordedProcess) else model  # A budget brake can change the next model before this child exits.
            stall = core.StallGuard(str(logf), proc, stall_sec, wt=wt)
            onay_sorusu = ""
            pw = 0
            baslangic = deneme_bas = core.time.time()   # deneme_bas stays fixed; the start shifts with the artefact (B184 duration measure)
            sure_doldu = False
            recheck_tur = 0
            hata_dongu = hata_prov = hata_model = ""   # 4c-17: the failure must not be written to the previous attempt's (model, route)
            while proc.poll() is None:
                try:
                    core.wait_pause(max(0, min(poll, 60, deadline - core.time.monotonic())), proje)
                except core.CancelledRun:
                    core.kill_tree(proc.pid)
                    core.reap(proc)
                    core.qe_durum(gorev_id, "iptal", "kullanici")
                    core.log(logf, "IPTAL: kullanici bayragi — is birakiliyor")
                    return 1
                if proc.poll() is not None:
                    break  # Do not restart work that completed during the wait.
                if core.time.monotonic() >= deadline:
                    sure_doldu = True
                    birakma_nedeni = 'sure-tavani'
                    core.RunStore(core.QE).task_update(core.ENV('CADRE_RUN_ID'), gorev_id or 'main',
                                                     time_decision='sure-tavani')
                    core.log(logf, 'SURE TAVANI: ilerleme olsa da kayitli motor suresi doldu; surec kapatiliyor')
                    core.kill_tree(proc.pid)
                    break
                if warn and not clock.get('warned') and core.time.monotonic() >= warning_at:
                    clock['warned'] = True
                    core.RunStore(core.QE).task_update(core.ENV('CADRE_RUN_ID'), gorev_id or 'main', motor_clock=clock)
                    core.log(logf, 'SURE UYARISI: motor uyari esigi doldu; izinli ilerleme surer')
                recheck_tur += 1
                hp = core.hata_patlamasi(oturumlar=core.wt_oturumlari(wt), baslangic=proc.attempt.data["created"] if isinstance(proc, core.RecordedProcess) else baslangic)
                if hp:
                    hata_dongu = hp[1]
                    hata_prov = hp[2] if len(hp) > 2 else ""
                    hata_model = hp[3] if len(hp) > 3 else ""
                    core.log(logf, f"cadre-guard: HATA-DONGUSU — {hp[0]} ERROR/3dk ({hp[1]}); kosu kesiliyor, model/yol degisecek")
                    core.kill_tree(proc.pid)
                    break
                s = stall.tik(proje)
                if s == "iptal":
                    core.reap(proc)
                    core.qe_durum(gorev_id, "iptal", "kullanici")
                    core.log(logf, "IPTAL: kullanici bayragi — is birakiliyor")
                    return 1
                if s == "stall":
                    break  # the process was cut; do not sleep one more poll (06:25: 2 min lost)
                if s == "onay":
                    onay_sorusu = stall.onay_sorusu
                    break
                if s == "dongu":
                    pw = 1  # G-F9: the stuck path (rescuer -> release) sets the limit
                    break
                butce_cache = core.butce_json()
                bans.refresh(butce_cache)
                yasak = bans.blocked()
                # One observed policy for brake and reopening. The request gate
                # owns admission; an in-flight reservation never kills itself.
                if not go_kisit_aktif:
                    _k2, _bt2, _k2n = core.go_kisit_gerekli(in_flight=True, budget=butce_cache)
                    if _k2:
                        bans.block('opencode-go', 'budget', butce_cache)
                        yasak = bans.blocked()
                        _p, _m, _b = core.kisit_config_yaz("go", butce=_bt2, izin=izin, yasak=yasak)
                        kisit_env = {"OPENCODE_CONFIG": _p}
                        go_kisit_aktif = True
                        kisit_kes = True
                        son_kisit_model, son_kisit_basamak = _m, _b
                        if model and model.startswith("opencode-go/"):
                            model = core.kota_dusur(model, logf, neden="pay")  # Go share exhausted -> next provider in the chain
                        core.log(logf, f"cadre-guard: GO-KISIT — ortak butce esigi kapali; alt-ajanlar {_b} -> {_m}; kayitli oturumla devam")
                        core.kill_tree(proc.pid)
                        break
                # Budget exhaustion is not a provider error/circuit-breaker event.
                frenler = []
                while True:
                    fren = core.saglayici_freni(butce_cache, yasak)
                    if not fren:
                        break
                    # B110: a ban written because of the run CAP binds only the LANE pocket; the judge being unable to reach the cloud
                    # while its own pocket still had money was the opposite of the hard split.
                    bans.block(fren[0], 'budget', butce_cache,
                               dilim='seritler' if 'kosu tavani' in (fren[1] or '') else None)
                    yasak = bans.blocked()
                    frenler.append(fren)
                if frenler:
                    _k5, _bt5, _k5n = core.go_kisit_gerekli()
                    _p, _m, _b = core.kisit_config_yaz("fren", butce=_bt5, izin=izin, yasak=yasak, go_var=not _k5)
                    ana_etkilendi = bool(model and core.YON and core.YON.saglayici(model) in {f[0] for f in frenler})
                    config_degisti = (_m != son_kisit_model)
                    son_kisit_model, son_kisit_basamak = _m, _b
                    ozet = "; ".join(f"{f[0]}: {f[1]}" for f in frenler)
                    if config_degisti:
                        kisit_env = {"OPENCODE_CONFIG": _p}
                    if ana_etkilendi:
                        kisit_env = {"OPENCODE_CONFIG": _p}
                        if ana_etkilendi:
                            # B69/T3: if the RUN CAP triggered the brake, no paid provider is offered; only if that
                            # provider's SHARE ran out can the other paid providers in the chain still be used.
                            model = core.kota_dusur(model, logf, neden=("kosu-tavani" if any("tavan" in f[1] for f in frenler) else "pay"))
                        kisit_kes = True
                        core.log(logf, f"cadre-guard: FREN ({ozet}) — kosu kesiliyor, yasakli={sorted(yasak)}; alt-ajanlar {_b} -> {_m}; ana model {model or 'YEREL Cadre'}; kayitli oturum veya acik devirle devam")
                        core.kill_tree(proc.pid)
                        break
                    core.log(logf, f"cadre-guard: FREN ({ozet}) — yasakli={sorted(yasak)}; kosu ETKILENMEDI (ana model ve alt-ajan config ayni), kesilmedi")
                # A helper config refresh does not interrupt the running main session.
                if recheck_tur > 0 and recheck_tur % 4 == 0:
                    try:
                        # (c) When the budget observation comes back, the restriction ladder is rewritten with the real chain (B93).
                        if son_kisit_basamak == "butce-yok" and isinstance(butce_cache, dict) and butce_cache:
                            _p6, _m6, _b6 = core.kisit_config_yaz("go", butce=butce_cache, izin=izin, yasak=yasak)
                            if _b6 != "butce-yok" and _m6 != son_kisit_model:
                                kisit_env = {"OPENCODE_CONFIG": _p6}
                                son_kisit_model, son_kisit_basamak = _m6, _b6
                                core.log(logf, f"BUTCE GOZLEMI GERI GELDI: alt-ajanlar {_b6} -> {_m6} (kayitli oturumla devam)")
                        if go_kisit_aktif and "opencode-go" not in yasak and not core.go_kisit_gerekli()[0]:
                            # B133: when the process was killed with `kill_tree` and restarted it lost
                            # the flow (3 ESCALATIONS -> 3 STALLS, ~31 min). It is no longer killed: the decision is recorded
                            # and applied at the next natural start when `kisit_env` is read.
                            _p, _m, _b = core.kisit_config_yaz("yukselme", butce=butce_cache, izin=izin, yasak=yasak, go_var=True)
                            kisit_env = {"OPENCODE_CONFIG": _p}
                            go_kisit_aktif = False
                            core.log(logf, "YARDIMCI: Go kapasitesi geri geldi -> alt-ajanlar Go'ya "
                                      "donecek (calisan surec kesilmiyor; bir sonraki baslangicta gecerli)")
                    except Exception as _e:
                        core.log(logf, f"YARDIMCI kontrolu hata: {_e}")
            if sure_doldu:
                core.reap(proc)
                break
            if stall.dongu_testi:
                task = core.RunStore(core.QE).read(core.ENV("CADRE_RUN_ID"))["tasks"].get(gorev_id or "main", {})
                if task.get('dongu_yonlendirildi'):
                    core.reap(proc)
                    birakma_nedeni = "dongu-tekrari"
                    core.log(logf, "KALDI: yonlendirmeden sonra ayni arac dongusu tekrarlandi")
                    break
                core.RunStore(core.QE).task_update(core.ENV("CADRE_RUN_ID"), gorev_id or "main", dongu_yonlendirildi=True)
                pw, kisit_kes = 0, True
            elif stall.kesti:
                model = core.yol_devret(attempt_model, logf, 'takildi')   # 4c-9: no helper agent, the stuck one is the main model
                kisit_kes = True
            cl_out = ""
            if not isinstance(proc, core.RecordedProcess) and attempt_model.startswith("claude/") and proc.stdout:
                cl_out = proc.stdout.read()
            core.reap(proc)  # limited wait if cut; immediately if it finished by itself (C7)
            if isinstance(proc, core.RecordedProcess):
                cl_out = proc.output() if attempt_model.startswith("claude/") else ""
            rc = proc.returncode or 0
            ozet = (core.cikti_denetimi(proc, attempt_model, logf) if isinstance(proc, core.RecordedProcess)
                    else {"finish": ""})   # 4c-4: truncation and thinking checks in one place
            _dogus = proc.attempt.data.get("created", deneme_bas) if isinstance(proc, core.RecordedProcess) else deneme_bas
            _kontrollu = bool(kisit_kes or onay_sorusu or core.budget_denial_receipt(proc) is not None)   # B184
            if hizli.kaydet(rc, core.time.time() - _dogus, _dogus, kontrollu=_kontrollu) and hizli.dongu:   # B179: a series of fast failures = loop
                _hp = core.hata_patlamasi(pencere_sn=3600, esik=1, oturumlar=core.wt_oturumlari(wt), baslangic=hizli.seri_baslangic)
                hata_dongu = hata_dongu or (_hp[1] if _hp else "hizli-dusus")
                hata_prov = hata_prov or (_hp[2] if _hp else "")
                hata_model = hata_model or (_hp[3] if _hp and len(_hp) > 3 else "")
                pw = 1
                core.log(logf, f"cadre-guard: HIZLI-DUSUS — {hizli.seri} ardisik deneme {int(hizli.esik_sn)} sn altinda rc!=0 ({hata_dongu}; saglayici {hata_prov or '?'}); takilma sayilir, ariza yolu (G-F10)")
            if rc == 0 and not stall.kesti and not onay_sorusu and not core.budget_denial_receipt(proc):
                onay_sorusu = core.onay_karari(wt, core.task_root(gorev_id) / "plan" / "brifing-motor.md",
                                          source_sha=(core.RunStore(core.QE).read(core.ENV("CADRE_RUN_ID")) or {}).get("source_sha"))  # G-C13 second trigger (T1b); T1v3: source SHA
            if onay_sorusu:  # G-C13: the decision is the user's — no retry, QA, judge or delivery
                birakma_nedeni = f"onay-bekliyor: {onay_sorusu[:200]}"
                if core.ENV("CADRE_RUN_ID"):
                    core.RunStore(core.QE).update(core.ENV("CADRE_RUN_ID"), reason=birakma_nedeni)
                core.log(logf, f"ONAY-BEKLIYOR: {onay_sorusu[:200]} — kosu birakildi; `cadre durum` soruyu gosterir, karar sonrasi yeni gorev")
                break
            if rc == 0 and attempt_model and core.YON:  # C21: a successful cloud call resets the circuit breaker counter
                try:
                    core.YON.basari(core.YON.saglayici(attempt_model), model=attempt_model)
                except Exception:
                    pass
            denial_receipt = core.budget_denial_receipt(proc)
            budget_denied = denial_receipt is not None
            if budget_denied:
                core.apply_budget_denial(bans, denial_receipt, core.butce_json())
                hata_dongu = ''  # ERROR events of the budget plugin cannot poison the circuit breaker.
                yasak = bans.blocked()
                ana_sag = core.YON.saglayici(model) if (core.YON and model) else ''
                if model and core.yardimci_reddi_mi(denial_receipt, ana_sag):
                    # B231: a helper provider denial does not change the main model; helpers skip the banned one.
                    core.log(logf, f"BUTCE: yardimci saglayici reddi ({', '.join(sorted(yasak))}); ana model {model} korunur, yardimcilar yasakliyi atlar (B231)")
                else:
                    # B339: a cloud link with budget instead of the denied provider; otherwise budget-wait
                    bulut = (core.yol_devret(model, logf, 'Go pencere reddi', yasak=yasak, model_devri=False)
                             if model.startswith('opencode-go/') else
                             core.kurtarici_sec(set(yasak) | {ana_sag}) if model else '')
                    model = bulut or ''
                    core.log(logf, f'BUTCE: deneme kaydi yeni bulut cagrilarini reddetti; kayitli oturum {bulut or "butce-bekleme"} ile surdurulecek')
                _p, _, _ = core.kisit_config_yaz('budget-denied', butce=core.butce_json() or {}, izin=izin, yasak=yasak)  # B51: an empty budget closes the helpers
                kisit_env = {'OPENCODE_CONFIG':_p}
                kisit_kes = True
                go_kisit_aktif = True
            if attempt_model.startswith("claude/"):
                core.claude_maliyet_kaydet(cl_out, attempt_model, logf, proc.attempt.data if isinstance(proc, core.RecordedProcess) else None)
                if rc == 0 and not budget_denied:
                    core.log(logf, "kosu kendi bitti (rc=0)")
                    produced = True
                    core.qe_durum(gorev_id, "uretti", "rc0-kabul-bekliyor")
                    break

            # KOTA FALLBACK
            son = ""
            try:
                son = logf.read_text()[-4000:]
            except Exception:
                pass
            if kisit_kes:
                kisit_kes = False
                kontrollu_kes = True
                stall.kesti = True  # controlled interruption: not counted as stuck, continue with -c
            if hata_dongu:
                son += " HATA-DONGUSU:" + hata_dongu
                # 4c-17: the error is written to the router state at (model, route) level, not to the task bans; a provider closes only
                # when >=2 of its models fail or on an account signal. If the main model is cooling down the same session continues with the next route/model.
                if hata_prov:
                    go_duvari = not core.patlama_arizasi_mi(hata_prov, hata_dongu)
                    if core.YON and not go_duvari:
                        core.YON.ariza(hata_prov, hata_dongu, allowed_fails=1, model=hata_model or None)
                    _k3, _bt3, _k3n = core.go_kisit_gerekli()
                    _p, _m, _b = core.kisit_config_yaz("go", butce=_bt3, izin=izin, yasak=yasak, go_var=False)
                    kisit_env = {"OPENCODE_CONFIG": _p}
                    core.log(logf, f"{'GO PENCERE DUVARI' if go_duvari else '(MODEL, YOL) ARIZASI'} ({hata_prov}/{hata_model or '?'}: "
                                   f"{hata_dongu}) -> {'ariza sayilmaz' if go_duvari else 'sogumada'}; alt-ajanlar merdiven: {_b} -> {_m}")
                    if model and core.YON and core.YON.cooldownda(core.YON.saglayici(model), None, None, model):
                        model = core.yol_devret(model, logf, f"hata: {hata_dongu}")
                        kontrollu_kes, pw = True, 0
                    stall.kesti = True  # controlled: not counted as stuck, continue with the recorded session
                elif not model:
                    pw = 1  # a permanent error without a model = counted as stuck -> rescuer/release path
            limit_imzasi = core.LIMIT_IMZASI.search(son)
            if not kontrollu_kes and (rc != 0 or hata_dongu) and limit_imzasi:
                if model:
                    go_yol = model.startswith('opencode-go/')
                    # the class comes from the error sample in the opencode log (hata_patlamasi); the engine's own log only gives the keyword (4c QA2)
                    model = core.kota_dusur(model, logf, hata=hata_dongu or limit_imzasi.group(0))
                    if go_yol:
                        kontrollu_kes, stall.kesti, pw = True, True, 0

            if pw == 1:
                stuck += 1
                if stuck >= stuck_max and not model and not GO_RESCUE_USED:
                    GO_RESCUE_USED = True
                    stuck = 0
                    # C8/C10: the rescuer also goes through the SAME router + task bans + budget gate; no fixed model.
                    aday = core.kurtarici_sec(yasak)
                    if aday:
                        model = aday
                        core.log(logf, f"KURTARICI VURUS: yerel takildi -> tek deneme {model} ile (yonlendirici; yasakli={sorted(yasak)})")
                        continue
                    core.log(logf, "KURTARICI: butceli/izinli bulut yok -> yerelde kalindi (hepsi-bitti: yalniz yerel)")
                if stuck >= stuck_max:
                    core.log(logf, "TAKILDI: {} kez ust uste hicbir sey uretmedi — bu is birakiliyor (resume futile)".format(stuck))
                    core.qe_otopsi(wt, proje, logf, "takildi", real)
                    break
            elif rc == 0 and not stall.kesti and ozet["finish"] == "length" and not cikti_ikiye:
                cikti_ikiye, kontrollu_kes = True, True   # 4c-4: the same session once with the 2× cap (clipped to the model cap)
                stall_env = {**stall_env, 'CADRE_CIKTI_KAT': '2'}
                core.log(logf, "KESILME -> ayni oturum bir kez 2× cikti tavaniyla surduruluyor")
            elif rc == 0 and not stall.kesti and ozet["finish"] == "length":
                core.log(logf, "BOS BITIS (B347): 2× tavanla da kesildi -> yarim; kayitli urun korunur")
                core.qe_durum(gorev_id, "yarim", "bos-bitis")
                break
            elif rc == 0 and not stall.kesti:
                core.log(logf, "kosu kendi bitti (rc=0)")
                produced = True
                core.qe_durum(gorev_id, "uretti", "rc0-kabul-bekliyor")
                break
            else:
                if not stall.kesti:
                    stuck = 0
                    core.log(logf, f"kosu dustu (rc={rc})")

            n = core.commit_wip(wt, proje, "kesinti oncesi") if not gorev_id else ""  # H4: lanes share one worktree; commits happen in the coordinator
            if n:
                core.log(logf, f"ara commit: {n} dosya")
            ardisik_kontrollu = ardisik_kontrollu + 1 if kontrollu_kes else 0
            if kontrollu_kes and ardisik_kontrollu > kontrollu_tavan:   # B188: a never-ending series of budget denials/brakes does not loop without limit
                core.log(logf, f"cadre-guard: KONTROLLU-KESME TAVANI — {ardisik_kontrollu} art arda kontrollu kesme; deneme sayiliyor (B188)")
            task_devirleri = core.RunStore(core.QE).read(core.ENV('CADRE_RUN_ID'))['tasks'].get(gorev_id or 'main', {}).get('yol_devirleri') or []
            yol_kes = bool(task_devirleri and task_devirleri[-1].get('ts', 0) >= deneme_bas)
            if not yol_kes and (not kontrollu_kes or ardisik_kontrollu > kontrollu_tavan):
                tries += 1  # brake/escalation interruptions (controlled) do not count as attempts up to the cap
                core.RunStore(core.QE).task_update(core.ENV("CADRE_RUN_ID"), gorev_id or "main", failure_count=tries)
            kontrollu_kes = False
            if tries > max_resume:
                core.log(logf, f"deneme siniri ({max_resume}) doldu, birakiliyor")
                core.qe_otopsi(wt, proje, logf, "deneme-bitti", real)
                break
    except PermissionError as exc:
        if not str(exc).startswith('project-tool-'):
            raise
        core.log(logf, f"ARAC SINIRI: {exc} — yurutucu baslamadi; urun ve kabul kayitlari korundu")
        produced = False
        birakma_nedeni = str(exc)
    except core.SessionRecoveryError as exc:
        # Identity unclear/contradictory: NO blind restart, the record is kept, a controlled partial (not a traceback).
        core.log(logf, f"OTURUM KIMLIGI: {exc} — kosu birakildi, deneme kayitlari korunuyor; `cadre devam` onarim sonrasi")
        produced = False
        birakma_nedeni = "oturum-kimligi"
    except BaseException:
        produced = False
        birakma_nedeni = 'yurutucu-hatasi'
        raise
    finally:
        if proc is not None and isinstance(getattr(proc, 'pid', None), int):
            native = proc.proc if isinstance(proc, core.RecordedProcess) else proc
            if native.poll() is None:
                core.kill_tree(proc.pid)
                core.reap(proc)
        if caf:
            caf.terminate()
            core.reap(caf, timeout=2)
        if ckpt:
            if isinstance(getattr(ckpt, "pid", None), int):
                core.kill_tree(ckpt.pid, grace=1)  # B32: TERMing only bash left the `sleep 600` child orphaned (holding the pipes for 10 min)
                core.reap(ckpt, timeout=2)         # B142b: the exit status is reaped (same path as caffeinate)
            else:
                ckpt.terminate()
        n = core.commit_wip(wt, proje, "kosu sonu") if not gorev_id else ""
        if n:
            core.log(logf, f"son commit: {n} dosya")
        # close states
        d = core.task_root(gorev_id) / "status.txt"
        # injection finding (E15): the 'iptal' state was OVERWRITTEN with 'yarim' here -> the chain could take a cancelled job
        # for a partial one and retry it. Definite states (tamam/iptal) are kept.
        if not (d.exists() and d.read_text().split(" ")[0] in ("uretti", "iptal")):
            core.qe_durum(gorev_id, "yarim", birakma_nedeni or f"rc={rc}")
        p = core.task_root(gorev_id) / "worker.pid"
        if p.exists():
            p.unlink()
        # B110: only rc==1 counted as a violation; when the manifest was DELETED (rc=2) the run went silently to
        # success — so removing the protection was easier than passing it. Every non-zero result is a violation.
        if kritik_dogrula() != 0:  # C23: a critical file changed/was deleted or the manifest was corrupted -> the product does not count as a success
            core.log(logf, "KRITIK DOSYA IHLALI: kosu butce/config dosyasina dokundu; basari yazilmaz (dosyalar imzadan geri alindi)")
            produced = False
            if not (d.exists() and d.read_text().split(" ")[0] == "iptal"):
                core.qe_durum(gorev_id, "yarim", "kritik-dosya-ihlali")
    outcome = 0 if produced else 1
    if core.cancel_path(proje).exists():
        outcome = 130
    if not gorev_id and outcome == 0:
        acceptance = core.finalize_run(proje, wt, logf, real, kisit_env=kisit_env)
        outcome = 0 if acceptance["status"] in ("PASS", "UYGULANDI") else 1
        core.qe_durum(gorev_id, "tamam" if outcome == 0 else "yarim", "final-kabul")
    # RUN LEDGER: the job's brain (opencode sessions, thinking, tool calls,
    # sub-agents) + engine log + git are dumped into one folder; third-eye analysis reads from here.
    try:
        r = core.subprocess.run([core.sys.executable, str(core.CODE / "lib/kosu_defteri.py"), proje, str(wt), str(logf), str(pf)],
                           capture_output=True, text=True, timeout=300)
        core.log(logf, f"kosu defteri: {r.stdout.strip() or r.stderr.strip()[-300:]}")
    except Exception as e:
        core.log(logf, f"kosu defteri yazilamadi: {e}")
    core.log(logf, f"BITTI. worktree={wt}  log={logf}")
    print(wt)
    return outcome


