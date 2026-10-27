"""Engine: task clocks, progress watch and executor lifecycle. S1: engine responsibility; shared services come from the qe_core interface."""
from __future__ import annotations
import re
import math


def duration_record(store, run_id, task_id, warn, abort):
    import qe_core as core
    now = core.time.time()
    def bind(task):
        clock = task.setdefault('runner_clock', {'started': now, 'warning_seconds': warn, 'limit_seconds': abort})
        if (not isinstance(clock, dict) or any(type(clock.get(key)) not in (int, float)
                or not math.isfinite(clock[key]) for key in ('started', 'warning_seconds', 'limit_seconds'))
                or not 0 < clock['started'] <= now or clock['warning_seconds'] < 0 or clock['limit_seconds'] <= 0):
            raise ValueError('runner-clock-invalid')
        clock['limit_seconds'] = min(clock['limit_seconds'], abort)
        clock['warning_seconds'] = min(clock['warning_seconds'], warn, clock['limit_seconds'])
    task = store.task_mutate(run_id, task_id, bind)
    clock = task['runner_clock']
    record = store.read(run_id)
    parents = set()
    pauses = []
    while True:
        # N-RUN02/N-RUN04: only owned processes confirmed closed
        # separate the stop from the recorded continue; retry/wait does not reset the clock.
        for change in record.get('plan_changes', []):
            if not isinstance(change, dict):
                raise ValueError('runner-clock-pause-invalid')
            if change.get('status') != 'resumed':
                continue
            stop = change.get('stop') or {}
            if (not isinstance(stop, dict) or not isinstance(stop.get('runs'), list)
                    or not isinstance(change.get('events'), list)
                    or any(not isinstance(event, dict) for event in change['events'])):
                raise ValueError('runner-clock-pause-invalid')
            if run_id not in stop['runs']:
                continue
            resumed = [event.get('ts') for event in change.get('events', []) if event.get('action') == 'resumed']
            began = stop.get('ts')
            if (len(resumed) != 1 or any(type(value) not in (int, float) or not math.isfinite(value)
                                       for value in (began, *resumed))
                    or not 0 < began <= resumed[0] <= now):
                raise ValueError('runner-clock-pause-invalid')
            pauses.append((max(clock['started'], began), resumed[0]))
        if not record.get('project_ref_parent'):
            break
        if record['run_id'] in parents:
            raise ValueError('runner-clock-parent-cycle')
        parents.add(record['run_id'])
        record = store.read(record['project_ref_parent'])
    paused, cursor = 0, clock['started']
    for began, resumed in sorted(pauses):
        paused += max(0, resumed - max(cursor, began))
        cursor = max(cursor, resumed)
    end = clock['started'] + clock['limit_seconds'] + paused
    warning_end = clock['started'] + clock['warning_seconds'] + paused
    if record.get('request'):
        from run_store import request_remaining_sec
        remaining = request_remaining_sec(record, now)
        authorized_end = now + remaining
        if not math.isfinite(authorized_end):
            raise ValueError('runner-clock-invalid')
        if (record.get('request_clock') or {}).get('session') is not None:
            # A sub-task's work cannot be longer than the request's whole accumulated
            # work. The smaller of the two upper bounds keeps the old cap without counting waits.
            total_work = record['request']['duration_minutes'] * 60 - remaining
            if not math.isfinite(total_work) or total_work < 0:
                raise ValueError('runner-clock-invalid')
            elapsed = min(max(0, now - clock['started'] - paused), total_work)
            end = now + clock['limit_seconds'] - elapsed
            warning_end = now + clock['warning_seconds'] - elapsed
        end = min(end, authorized_end)
    elif record.get('operator_model_permission'):
        from permission import read_action
        action = read_action(store, record['run_id'], record['operator_model_permission'])
        authorized_end = action['finished'] + action['identity']['target']['duration_minutes'] * 60
        if not math.isfinite(authorized_end):
            raise ValueError('runner-clock-invalid')
        end = min(end, authorized_end)
    if record.get('project_deadline') is not None:
        if not math.isfinite(record['project_deadline']):
            raise ValueError('runner-clock-invalid')
        end = min(end, record['project_deadline'])
    mono = core.time.monotonic()
    return (mono + max(0, warning_end - now),
            mono + max(0, end - now), clock)


def runner(project_ref, pf):
    import qe_core as core
    if not core.ENV("CADRE_RUN_ID"):
        # Legacy direct invocation also enters the durable coordinator.
        store = core.RunStore(core.QE)
        record = store.accept(project_ref, core.pathlib.Path(pf).read_text())
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
    if store.read(run_id)["project"] != project_ref:
        raise ValueError("run/project mismatch")
    task_id = core.ENV("CADRE_TASK_ID", "") or "main"
    lock = core.FileLock(store.task_dir(run_id, task_id) / ".owner.lock")
    if not lock.acquire(blocking=False):
        return 75
    previous = core.os.environ.get("CADRE_OWNER_FDS", "")
    try:
        core.os.environ["CADRE_OWNER_FDS"] = ",".join(str(fd) for fd in (*core.owner_fds(), lock.fd))
        store.task_update(run_id, task_id, owner_pid=core.os.getpid(), state="running")
        return core._runner(project_ref, pf)
    finally:
        store.task_update(run_id, task_id, owner_pid=None)
        core.os.environ["CADRE_OWNER_FDS"] = previous
        lock.close()


def _runner(project_ref, pf):
    import qe_core as core
    real = core.ENV("CADRE_REAL_OPENCODE", "/opt/homebrew/bin/opencode")
    task_id = core.ENV("CADRE_TASK_ID", "")
    model = core.ENV("CADRE_RUN_MODEL", "")
    try:
        warn, abort_s = core.LIMIT.runner_durations()
    except (ValueError, TypeError):
        print('ENGINE DURATION SETTING/RECORD INVALID', file=core.sys.stderr)
        return 2
    max_resume = int(core.ENV("CADRE_MAX_RESUME", "12"))
    stuck_max = int(core.ENV("CADRE_STUCK_MAX", "2"))
    stall_sec = int(core.ENV("CADRE_STALL_SEC", "600"))
    # Tick 15 s: the cheap layer (brake, quota, cancel, stuck) on every tick; the budget observation via `observed_budget` with a 60 s
    # cache, the escalation check every 4 ticks (~60 s).
    poll = int(core.ENV("CADRE_PROGRESS_POLL", "15"))

    pf_p = core.pathlib.Path(pf)
    if not pf_p.is_file() or not pf_p.read_text().strip():
        print(f"EMPTY PROMPT: {pf}", file=core.sys.stderr)
        return 2

    ts = core.time.strftime("%Y%m%d-%H%M%S")
    logf = core.task_root(task_id) / "runner.log"
    pidp = core.task_root(task_id) / "worker.pid"
    core.atomic_write(pidp, str(core.os.getpid()))
    logf.parent.mkdir(parents=True, exist_ok=True)
    logf.touch()

    # WORKTREE
    wt = core.RunStore(core.QE).read(core.ENV("CADRE_RUN_ID")).get("worktree") or core.ENV("CADRE_WORKTREE", "")
    if wt:
        if not (core.pathlib.Path(wt) / ".git").exists():
            print(f"CADRE_WORKTREE invalid: {wt}", file=core.sys.stderr)
            return 2
    else:
        r = core.subprocess.run(["bash", "-c",
                            '. "$1"; qe_make_worktree "$2" "$3"', "qe", str(core.CODE / "lib/common.sh"), project_ref, f"job {ts}"],
                           capture_output=True, text=True)
        wt = r.stdout.strip()
        if not wt:
            core.log(logf, "could not create worktree; WT RETRY after 30s")
            try:
                core.wait_pause(30, project_ref)
            except core.CancelledRun:
                core.qe_status(task_id, "cancel", "user")
                return 1
            r = core.subprocess.run(["bash", "-c",
                                '. "$1"; qe_make_worktree "$2" "$3"', "qe", str(core.CODE / "lib/common.sh"), project_ref, f"job {ts}"],
                               capture_output=True, text=True)
            wt = r.stdout.strip()
            if not wt:
                print("could not create worktree after 2 attempts", file=core.sys.stderr)
                return 2
    g = core.subprocess.run(["bash", "-c", '. "$1"; qe_verify_ai_worktree "$2" "$3"',
                        "qe", str(core.CODE / "lib/common.sh"), wt, project_ref],
                       capture_output=True)
    if g.returncode != 0:
        print("guard unverifiable", file=core.sys.stderr)
        return 2
    core.RunStore(core.QE).update(core.ENV("CADRE_RUN_ID"), worktree=wt)
    if core.ENV("CADRE_RUN_ID"):  # G-C14: install command + baseline QA information, once per run, while there is no product
        _tb = core.base_preparation(project_ref, wt, logf, core.RunStore(core.QE).path(core.ENV("CADRE_RUN_ID")) / "base-qa.json", core.QE / "projects" / project_ref)
        from run_store import base_qa_save
        base_qa_save(core.RunStore(core.QE), core.ENV("CADRE_RUN_ID"), _tb)
        if _tb.get("status") == "FAIL":
            core.log(logf, f"START GATE: {_tb.get('reason') or 'baseline-unavailable'} — stopping the run")
            core.qe_status(task_id, "half", _tb.get('reason') or 'baseline-unavailable')
            core.RunStore(core.QE).update(core.ENV("CADRE_RUN_ID"), reason=f"{_tb.get('reason') or 'baseline-unavailable'}: rc={(_tb.get('install') or {}).get('exit_code')}")
            return 1

    try:
        warning_at, deadline, clock = duration_record(core.RunStore(core.QE), core.ENV('CADRE_RUN_ID'),
                                               task_id or 'main', warn, abort_s)
    except (ValueError, TypeError):
        core.log(logf, 'INVALID ENGINE DEADLINE RECORD: no new executor')
        core.qe_status(task_id, 'half', 'runner-clock-invalid')
        return 2
    core.guard_revive()
    # C30: -s = do not sleep when the lid is closed either. P2: under a coordinator (CADRE_COORDINATOR_PID) caffeinate is single IN THE COORDINATOR and
    # covers the whole lifetime including plan/lane/acceptance; an independent engine (old path/test) keeps its own.
    caf = None if core.ENV("CADRE_COORDINATOR_PID") else core.subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(core.os.getpid())])
    # PERIODIC CKPT: an intermediate commit every 10 min (crash protection) — in task-id-less (single-run) mode.
    ckpt = None
    if not task_id:
        ckpt = core.subprocess.Popen(["bash", "-c",
            f'while sleep 600; do . "{core.CODE}/lib/common.sh"; n="$(qe_commit_wip "{wt}" "{project_ref}" "intermediate checkpoint" 2>/dev/null)"; '
            f'[ -n "$n" ] && echo "$(date "+%F %T") intermediate commit: $n file" >> "{logf}"; done'])
    signature_manifest = core.subprocess.run(["bash", str(core.CODE / "bin/restricted-shell.sh"), "signature", str(core.os.getpid())],
                                   capture_output=True, text=True).stdout   # B288: the root of trust is memory the executor cannot write
    core.ACTIVE_WT = wt
    _old = core.QE / "chain" / f"{project_ref}-cancel.flag"
    if _old.exists() and not task_id and not core.ENV("CADRE_RUN_ID"):  # a new job starts: a cancel flag left from the previous job is invalid (except dispatcher lanes)
        try:
            _old.unlink(); core.log(logf, "Cleared the cancel flag from the previous job")
        except Exception:
            pass
    core.log(logf, f"worktree: {wt}")
    core.log(logf, f"prompt  : {pf} ({pf_p.stat().st_size} bytes)")

    saved_task = core.RunStore(core.QE).read(core.ENV("CADRE_RUN_ID"))["tasks"].get(task_id or "main", {})
    tries = int(saved_task.get("failure_count", 0))
    stuck = 0
    GO_RESCUE_USED = False  # one-shot recovery (quota kept); for later jobs the chain decides on Go
    rc = 0
    produced = False
    proc = None
    # RESTRICTED MODE: Go share exhausted -> sub-agents from the provider ladder (next links of the chain).
    # NO privacy restriction: cloud is used while there is budget.
    restriction_env = None
    bans = core.task_bans(core.QE)
    bans.refresh(core.budget_json())
    forbidden = bans.blocked()
    permission = core.permission_sift(pf_p.read_text())
    if permission:
        core.log(logf, f"PERMISSION (task text): {', '.join(sorted(permission))} — allowed in this run")
    _k, _bt, _kn = core.go_restriction_required()
    is_beginning_digit = None
    if _k:
        _p, _m, _b = core.restriction_config_write("go", budget=_bt, permission=permission)
        restriction_env = {"OPENCODE_CONFIG": _p}
        is_beginning_digit = _b
        # B94 + B131: the brake closes either on share-exhausted or accounting-unreadable; the message
        # said "share exhausted" in EVERY case. The reason now comes from the brake ITSELF (`_kn`).
        _cause = ("Go allocation exhausted" if _kn == "pay-finished"
                  else f"Go accounting unreadable ({_kn.split(':', 1)[1].strip() if ':' in _kn else _kn}) — may be temporary; checked periodically"
                  if _kn.startswith("unreadable") else
                  "budget observation unreadable (may be temporary; checked periodically)"
                  if _b == "no-budget" else "Go allocation exhausted")
        core.log(logf, f"GO-RESTRICTION (job start): {_cause} — sub-agents ladder: {_b} -> {_m}")
    elif permission:
        _p, _, _ = core.restriction_config_write('permission', permission=permission, has_go=True)
        restriction_env = {'OPENCODE_CONFIG': _p}
    restriction_cut = False
    budget_cache = None  # Shared observation is cached by observed_budget for 60s.
    controlled_cut = False  # brake/escalation interruptions are not written to the attempt count (lossless -c)
    last_restriction_model = None
    last_restriction_digit = is_beginning_digit   # B93: if 'no-budget', the restriction is treated as TEMPORARY and retried periodically
    if restriction_env and is_beginning_digit is not None:
        last_restriction_model = _m
    go_restriction_active = restriction_env is not None and not permission  # if a Go restriction was set up at job start
    if restriction_env is not None:
        _kk, _, _ = core.go_restriction_required()
        go_restriction_active = bool(_kk)
    error_prov = error_model = ""
    output_two = False   # 4c-4: on truncation, the 2× cap allowance once in this task
    fast = core.FastDrop(int(core.ENV("CADRE_FAST_DROP_SEC", "60")), 3)   # B179
    controlled_cap = int(core.ENV("CADRE_CONTROLLED_CAP", "6"))   # B188: consecutive controlled interruptions count as an attempt after this number
    consecutive_controlled = 0
    stall_env = {}   # addition to the continue prompt and one-off output multiplier (4c-4); no helper agent (4c-9)
    leaving_reason = ""  # controlled release reason (like session-id); written with rc if empty
    def critical_verify():
        guard = core.subprocess.run(["bash", str(core.CODE / "bin/restricted-shell.sh"), "verify", str(core.os.getpid()), "-"], input=signature_manifest,
                                    capture_output=True, text=True)
        for l in guard.stdout.splitlines():
            core.log(logf, l)
        return guard.returncode
    try:
        core.acceptance_assurance(logf)
    except core.BudgetDenied:
        core.qe_status(task_id, 'half', 'budget-wait')
        if caf:
            caf.terminate()
            core.reap(caf, timeout=2)
        if ckpt:
            core.kill_tree(ckpt.pid, grace=1)
            core.reap(ckpt, timeout=2)
        pidp.unlink(missing_ok=True)
        critical_verify()
        return 75
    try:
        while True:
            if core.time.monotonic() >= deadline:
                leaving_reason = 'duration-cap'
                core.log(logf, 'TIME CAP: the recorded engine deadline expired; no new executor')
                core.RunStore(core.QE).task_update(core.ENV('CADRE_RUN_ID'), task_id or 'main',
                                                 time_decision='duration-cap')
                break
            task = core.RunStore(core.QE).read(core.ENV('CADRE_RUN_ID'))['tasks'].get(task_id or 'main', {})
            if task.get('path_pending'):
                model = core.path_hand_over(task['path_pending'], logf, 'pending route for the same model', model_handover=False, pending=True)
                if not model:
                    leaving_reason = 'budget-wait'
                    core.budget_wait_save(logf)
                    break
            if not model:
                model = core.cloud_candidate('impl', forbidden, wt)
                if not model:
                    leaving_reason = 'budget-wait'
                    core.budget_wait_save(logf)
                    break
            # P2 DISK THRESHOLD: if low, no new executor starts (cancel-aware, capped wait)
            _disk = core.disk_wait(logf, project_ref, deadline=deadline)
            if _disk is None:
                core.qe_status(task_id, "cancel", "user")
                core.log(logf, "CANCEL: user flag while waiting for disk space; stopping the job")
                return 1
            if core.time.monotonic() >= deadline:
                continue
            if _disk is False:
                core.qe_status(task_id, "half", "disk-low")
                leaving_reason = "disk-low"
                break

            # The adapter decides continuation from its durable session receipt,
            # independently of the failure limit. Preserve the original task for
            # an explicit handoff when changing between Claude and OpenCode.
            started = core.RunStore(core.QE).read(core.ENV("CADRE_RUN_ID"))["tasks"].get(task_id or "main", {}).get("attempt_count", 0) > 0
            task_text = pf_p.read_text()
            if not started:
                if not model.startswith("claude/") and not task_id:  # in lane mode the plan is in the dispatcher
                    _kb, _bt, _kbn = core.go_restriction_required()
                    bp = core.on_plan(real, wt, project_ref, task_text, logf, _bt, env_extra=restriction_env)
                    if bp:
                        task_text = (f"BRIEFING READY: `{bp}` — read this first and copy ITEMS into `.tasks.json` (mark items starting with APPROVAL: "
                                       f"and every item awaiting a decision with `\"status\": \"BLOCKED: approval\"` and if ALL items are BLOCKED, leave the product untouched, write one line 'BLOCKED: approval — <question>' and FINISH), "
                                       f"then do the executable items in order. Do not request another briefing from cadre-plan.\n\n") + task_text
                core.log(logf, "starting the run (opencode run)")
            else:
                core.log(logf, "proceed: recorded adapter session or explicit executor handoff; independent of the error counter")
            task = core.RunStore(core.QE).read(core.ENV("CADRE_RUN_ID"))["tasks"].get(task_id or "main", {})
            if task.get('loop_routed'):
                stall_env['CADRE_PROCEED_EXTRA'] = "\nDo not repeat the same tool call. Try a different approach and finish only the remaining work."
            args = (["-m", model] if model else []) + [task_text]

            try:
                if model.startswith("claude/"):
                    _m, _, _e = model.partition("#")
                    _mm=_m.split('/',1)[1]; _cap=core.ENV("CADRE_CLAUDE_BUDGET", {"opus":"5.0","fable":"8.0"}.get(_mm.split("-")[0],"2.0"))
                    core.log(logf, f"starting the Claude lane: request model={_mm} effort={_e or 'fallback'} budget=${_cap}")
                    try:
                        proc = core.run_claude(wt, model, pf_p.read_text(), logf)
                    except core.BudgetDenied as exc:
                        core.log(logf, f'Claude budget unknown/exhausted ({exc}); waiting for cloud capacity')
                        model = ''
                        continue
                else:
                    proc = core.run_opencode(real, wt, args, logf, env_extra={**(restriction_env or {}), **stall_env,
                                                                       'CADRE_DEADLINE_TS': core.duration_end(deadline - core.time.monotonic())})
                    stall_env.pop('CADRE_OUTPUT_MULTIPLIER', None)   # the 2× cap is only for the single attempt after a truncation (4c-4)
            except core.BudgetDenied:
                if model.startswith('opencode-go/'):
                    model = core.path_hand_over(model, logf, 'Go rejected new work', model_handover=False)
                    if model:
                        continue
                leaving_reason = 'budget-wait'
                core.budget_wait_save(logf)
                break
            except core.CancelledRun:
                core.qe_status(task_id, "cancel", "user")
                core.log(logf, "CANCEL: user flag while waiting for an executor slot; stopping the job")
                return 1
            attempt_model = proc.attempt.data["model"] if isinstance(proc, core.RecordedProcess) else model  # A budget brake can change the next model before this child exits.
            stall = core.StallGuard(str(logf), proc, stall_sec, wt=wt)
            approval_question = ""
            pw = 0
            start = attempt_start = core.time.time()   # attempt_start stays fixed; the start shifts with the artefact (B184 duration measure)
            duration_filled = False
            recheck_round = 0
            error_loop = error_prov = error_model = ""   # 4c-17: the failure must not be written to the previous attempt's (model, route)
            while proc.poll() is None:
                try:
                    core.wait_pause(max(0, min(poll, 60, deadline - core.time.monotonic())), project_ref)
                except core.CancelledRun:
                    core.kill_tree(proc.pid)
                    core.reap(proc)
                    core.qe_status(task_id, "cancel", "user")
                    core.log(logf, "CANCEL: user flag; stopping the job")
                    return 1
                if proc.poll() is not None:
                    break  # Do not restart work that completed during the wait.
                if core.time.monotonic() >= deadline:
                    duration_filled = True
                    leaving_reason = 'duration-cap'
                    core.RunStore(core.QE).task_update(core.ENV('CADRE_RUN_ID'), task_id or 'main',
                                                     time_decision='duration-cap')
                    core.log(logf, 'TIME CAP: the recorded deadline applies despite progress; stopping the process')
                    core.kill_tree(proc.pid)
                    break
                if warn and not clock.get('warned') and core.time.monotonic() >= warning_at:
                    clock['warned'] = True
                    core.RunStore(core.QE).task_update(core.ENV('CADRE_RUN_ID'), task_id or 'main', runner_clock=clock)
                    core.log(logf, 'TIME WARNING: engine warning threshold reached; allowed progress continues')
                recheck_round += 1
                hp = core.error_burst(sessions=core.wt_sessions(wt), start=proc.attempt.data["created"] if isinstance(proc, core.RecordedProcess) else start)
                if hp:
                    error_loop = hp[1]
                    error_prov = hp[2] if len(hp) > 2 else ""
                    error_model = hp[3] if len(hp) > 3 else ""
                    core.log(logf, f"cadre-guard: ERROR-LOOP — {hp[0]} ERROR/3min ({hp[1]}); stopping the run to change model or route")
                    core.kill_tree(proc.pid)
                    break
                s = stall.tick(project_ref)
                if s == "cancel":
                    core.reap(proc)
                    core.qe_status(task_id, "cancel", "user")
                    core.log(logf, "CANCEL: user flag; stopping the job")
                    return 1
                if s == "stall":
                    break  # the process was cut; do not sleep one more poll (06:25: 2 min lost)
                if s == "approval":
                    approval_question = stall.approval_question
                    break
                if s == "loop":
                    pw = 1  # G-F9: the stuck path (rescuer -> release) sets the limit
                    break
                budget_cache = core.budget_json()
                bans.refresh(budget_cache)
                forbidden = bans.blocked()
                # One observed policy for brake and reopening. The request gate
                # owns admission; an in-flight reservation never kills itself.
                if not go_restriction_active:
                    _k2, _bt2, _k2n = core.go_restriction_required(in_flight=True, budget=budget_cache)
                    if _k2:
                        bans.block('opencode-go', 'budget', budget_cache)
                        forbidden = bans.blocked()
                        _p, _m, _b = core.restriction_config_write("go", budget=_bt2, permission=permission, forbidden=forbidden)
                        restriction_env = {"OPENCODE_CONFIG": _p}
                        go_restriction_active = True
                        restriction_cut = True
                        last_restriction_model, last_restriction_digit = _m, _b
                        if model and model.startswith("opencode-go/"):
                            model = core.quota_drop(model, logf, reason="pay")  # Go share exhausted -> next provider in the chain
                        core.log(logf, f"cadre-guard: GO-RESTRICTION — the shared budget gate closed; sub-agents {_b} -> {_m}; continue the recorded session")
                        core.kill_tree(proc.pid)
                        break
                # Budget exhaustion is not a provider error/circuit-breaker event.
                brakes = []
                while True:
                    brake = core.provider_brake(budget_cache, forbidden)
                    if not brake:
                        break
                    # B110: a ban written because of the run CAP binds only the LANE pocket; the judge being unable to reach the cloud
                    # while its own pocket still had money was the opposite of the hard split.
                    bans.block(brake[0], 'budget', budget_cache,
                               slice='lanes' if 'run cap' in (brake[1] or '') else None)
                    forbidden = bans.blocked()
                    brakes.append(brake)
                if brakes:
                    _k5, _bt5, _k5n = core.go_restriction_required()
                    _p, _m, _b = core.restriction_config_write("brake", budget=_bt5, permission=permission, forbidden=forbidden, has_go=not _k5)
                    main_affected = bool(model and core.ROUTER and core.ROUTER.provider(model) in {f[0] for f in brakes})
                    config_changed = (_m != last_restriction_model)
                    last_restriction_model, last_restriction_digit = _m, _b
                    summary = "; ".join(f"{f[0]}: {f[1]}" for f in brakes)
                    if config_changed:
                        restriction_env = {"OPENCODE_CONFIG": _p}
                    if main_affected:
                        restriction_env = {"OPENCODE_CONFIG": _p}
                        if main_affected:
                            # B69/T3: if the RUN CAP triggered the brake, no paid provider is offered; only if that
                            # provider's SHARE ran out can the other paid providers in the chain still be used.
                            model = core.quota_drop(model, logf, reason=("run-cap" if any("cap" in f[1] for f in brakes) else "pay"))
                        restriction_cut = True
                        core.log(logf, f"cadre-guard: BRAKE ({summary}) — stopping the run, forbidden={sorted(forbidden)}; sub-agents {_b} -> {_m}; main model {model or 'LOCAL Cadre'}; continue through the recorded session or an explicit handoff")
                        core.kill_tree(proc.pid)
                        break
                    core.log(logf, f"cadre-guard: BRAKE ({summary}) — forbidden={sorted(forbidden)}; run UNAFFECTED (main model and sub-agent config unchanged), continuing")
                # A helper config refresh does not interrupt the running main session.
                if recheck_round > 0 and recheck_round % 4 == 0:
                    try:
                        # (c) When the budget observation comes back, the restriction ladder is rewritten with the real chain (B93).
                        if last_restriction_digit == "no-budget" and isinstance(budget_cache, dict) and budget_cache:
                            _p6, _m6, _b6 = core.restriction_config_write("go", budget=budget_cache, permission=permission, forbidden=forbidden)
                            if _b6 != "no-budget" and _m6 != last_restriction_model:
                                restriction_env = {"OPENCODE_CONFIG": _p6}
                                last_restriction_model, last_restriction_digit = _m6, _b6
                                core.log(logf, f"BUDGET OBSERVATION RECOVERED: sub-agents {_b6} -> {_m6} (continue the recorded session)")
                        if go_restriction_active and "opencode-go" not in forbidden and not core.go_restriction_required()[0]:
                            # B133: when the process was killed with `kill_tree` and restarted it lost
                            # the flow (3 ESCALATIONS -> 3 STALLS, ~31 min). It is no longer killed: the decision is recorded
                            # and applied at the next natural start when `restriction_env` is read.
                            _p, _m, _b = core.restriction_config_write("rise", budget=budget_cache, permission=permission, forbidden=forbidden, has_go=True)
                            restriction_env = {"OPENCODE_CONFIG": _p}
                            go_restriction_active = False
                            core.log(logf, "HELPER: Go capacity recovered; sub-agents will return to Go "
                                      "(the running process continues; effective at the next start)")
                    except Exception as _e:
                        core.log(logf, f"HELPER check error: {_e}")
            if duration_filled:
                core.reap(proc)
                break
            if stall.loop_test:
                task = core.RunStore(core.QE).read(core.ENV("CADRE_RUN_ID"))["tasks"].get(task_id or "main", {})
                if task.get('loop_routed'):
                    core.reap(proc)
                    leaving_reason = "loop-repeat"
                    core.log(logf, "STALLED: the same tool loop recurred after rerouting")
                    break
                core.RunStore(core.QE).task_update(core.ENV("CADRE_RUN_ID"), task_id or "main", loop_routed=True)
                pw, restriction_cut = 0, True
            elif stall.cut:
                model = core.path_hand_over(attempt_model, logf, 'stuck')   # 4c-9: no helper agent, the stuck one is the main model
                restriction_cut = True
            cl_out = ""
            if not isinstance(proc, core.RecordedProcess) and attempt_model.startswith("claude/") and proc.stdout:
                cl_out = proc.stdout.read()
            core.reap(proc)  # limited wait if cut; immediately if it finished by itself (C7)
            if isinstance(proc, core.RecordedProcess):
                cl_out = proc.output() if attempt_model.startswith("claude/") else ""
            rc = proc.returncode or 0
            summary = (core.output_audit(proc, attempt_model, logf) if isinstance(proc, core.RecordedProcess)
                    else {"finish": ""})   # 4c-4: truncation and thinking checks in one place
            _birth = proc.attempt.data.get("created", attempt_start) if isinstance(proc, core.RecordedProcess) else attempt_start
            _controlled = bool(restriction_cut or approval_question or core.budget_denial_receipt(proc) is not None)   # B184
            if fast.save(rc, core.time.time() - _birth, _birth, controlled=_controlled) and fast.loop:   # B179: a series of fast failures = loop
                _hp = core.error_burst(window_sec=3600, threshold=1, sessions=core.wt_sessions(wt), start=fast.series_start)
                error_loop = error_loop or (_hp[1] if _hp else "fast-drop")
                error_prov = error_prov or (_hp[2] if _hp else "")
                error_model = error_model or (_hp[3] if _hp and len(_hp) > 3 else "")
                pw = 1
                core.log(logf, f"cadre-guard: FAST-DROP — {fast.series} consecutive attempts ended with rc!=0 in under {int(fast.threshold_sec)}s ({error_loop}; provider {error_prov or '?'}); counted as a stall, failure path (G-F10)")
            if rc == 0 and not stall.cut and not approval_question and not core.budget_denial_receipt(proc):
                approval_question = core.approval_decision(wt, core.task_root(task_id) / "plan" / "briefing-runner.md",
                                          source_sha=(core.RunStore(core.QE).read(core.ENV("CADRE_RUN_ID")) or {}).get("source_sha"))  # G-C13 second trigger (T1b); T1v3: source SHA
            if approval_question:  # G-C13: the decision is the user's — no retry, QA, judge or delivery
                leaving_reason = f"approval-waiting: {approval_question[:200]}"
                if core.ENV("CADRE_RUN_ID"):
                    core.RunStore(core.QE).update(core.ENV("CADRE_RUN_ID"), reason=leaving_reason)
                core.log(logf, f"APPROVAL-WAITING: {approval_question[:200]} — run stopped; `cadre status` shows the question; submit a new task after the decision")
                break
            if rc == 0 and attempt_model and core.ROUTER:  # C21: a successful cloud call resets the circuit breaker counter
                try:
                    core.ROUTER.success(core.ROUTER.provider(attempt_model), model=attempt_model)
                except Exception:
                    pass
            denial_receipt = core.budget_denial_receipt(proc)
            budget_denied = denial_receipt is not None
            if budget_denied:
                core.apply_budget_denial(bans, denial_receipt, core.budget_json())
                error_loop = ''  # ERROR events of the budget plugin cannot poison the circuit breaker.
                forbidden = bans.blocked()
                main_provider_key = core.ROUTER.provider(model) if (core.ROUTER and model) else ''
                if model and core.is_helper_rejection(denial_receipt, main_provider_key):
                    # B231: a helper provider denial does not change the main model; helpers skip the banned one.
                    core.log(logf, f"BUDGET: helper provider denial ({', '.join(sorted(forbidden))}); main model {model} preserved; helpers skip the forbidden provider (B231)")
                else:
                    # B339: a cloud link with budget instead of the denied provider; otherwise budget-wait
                    cloud = (core.path_hand_over(model, logf, 'Go window denial', forbidden=forbidden, model_handover=False)
                             if model.startswith('opencode-go/') else
                             core.recovery_select(set(forbidden) | {main_provider_key}) if model else '')
                    model = cloud or ''
                    core.log(logf, f'BUDGET: the attempt record rejected new cloud calls; continue the recorded session through {cloud or "budget-wait"}')
                _p, _, _ = core.restriction_config_write('budget-denied', budget=core.budget_json() or {}, permission=permission, forbidden=forbidden)  # B51: an empty budget closes the helpers
                restriction_env = {'OPENCODE_CONFIG':_p}
                restriction_cut = True
                go_restriction_active = True
            if attempt_model.startswith("claude/"):
                core.claude_expense_save(cl_out, attempt_model, logf, proc.attempt.data if isinstance(proc, core.RecordedProcess) else None)
                if rc == 0 and not budget_denied:
                    core.log(logf, "run finished on its own (rc=0)")
                    produced = True
                    core.qe_status(task_id, "produced", "rc0-acceptance-waiting")
                    break

            # QUOTA FALLBACK
            last = ""
            try:
                last = logf.read_text()[-4000:]
            except Exception:
                pass
            if restriction_cut:
                restriction_cut = False
                controlled_cut = True
                stall.cut = True  # controlled interruption: not counted as stuck, continue with -c
            if error_loop:
                last += " ERROR-LOOP:" + error_loop
                # 4c-17: the error is written to the router state at (model, route) level, not to the task bans; a provider closes only
                # when >=2 of its models fail or on an account signal. If the main model is cooling down the same session continues with the next route/model.
                if error_prov:
                    go_wall = not core.is_burst_failure(error_prov, error_loop)
                    if core.ROUTER and not go_wall:
                        core.ROUTER.failure(error_prov, error_loop, allowed_fails=1, model=error_model or None)
                    _k3, _bt3, _k3n = core.go_restriction_required()
                    _p, _m, _b = core.restriction_config_write("go", budget=_bt3, permission=permission, forbidden=forbidden, has_go=False)
                    restriction_env = {"OPENCODE_CONFIG": _p}
                    core.log(logf, f"{'GO WINDOW LIMIT' if go_wall else '(MODEL, PATH) FAILURE'} ({error_prov}/{error_model or '?'}: "
                                   f"{error_loop}) -> {'not counted as a failure' if go_wall else 'cooling down'}; sub-agents ladder: {_b} -> {_m}")
                    if model and core.ROUTER and core.ROUTER.in_cooldown(core.ROUTER.provider(model), None, None, model):
                        model = core.path_hand_over(model, logf, f"error: {error_loop}")
                        controlled_cut, pw = True, 0
                    stall.cut = True  # controlled: not counted as stuck, continue with the recorded session
                elif not model:
                    pw = 1  # a permanent error without a model = counted as stuck -> rescuer/release path
            limit_signature = core.LIMIT_SIGNATURE.search(last)
            if not controlled_cut and (rc != 0 or error_loop) and limit_signature:
                if model:
                    go_path = model.startswith('opencode-go/')
                    # the class comes from the error sample in the opencode log (error_burst); the engine's own log only gives the keyword (4c QA2)
                    model = core.quota_drop(model, logf, error=error_loop or limit_signature.group(0))
                    if go_path:
                        controlled_cut, stall.cut, pw = True, True, 0

            if pw == 1:
                stuck += 1
                if stuck >= stuck_max and not model and not GO_RESCUE_USED:
                    GO_RESCUE_USED = True
                    stuck = 0
                    # C8/C10: the rescuer also goes through the SAME router + task bans + budget gate; no fixed model.
                    candidate = core.recovery_select(forbidden)
                    if candidate:
                        model = candidate
                        core.log(logf, f"RECOVERY ATTEMPT: local executor stuck; trying {model} once (router; forbidden={sorted(forbidden)})")
                        continue
                    core.log(logf, "RECOVERY: no cloud capacity within budget and permissions; stayed local (all-finished: local only)")
                if stuck >= stuck_max:
                    core.log(logf, "STUCK: no output in {} consecutive attempts; stopping the job (resume futile)".format(stuck))
                    core.qe_autopsy(wt, project_ref, logf, "stuck", real)
                    break
            elif rc == 0 and not stall.cut and summary["finish"] == "length" and not output_two:
                output_two, controlled_cut = True, True   # 4c-4: the same session once with the 2× cap (clipped to the model cap)
                stall_env = {**stall_env, 'CADRE_OUTPUT_MULTIPLIER': '2'}
                core.log(logf, "TRUNCATION: continuing the same session once with twice the output cap")
            elif rc == 0 and not stall.cut and summary["finish"] == "length":
                core.log(logf, "TRUNCATED FINISH (B347): cut off even with the doubled cap; partial result, recorded product preserved")
                core.qe_status(task_id, "half", "empty-end")
                break
            elif rc == 0 and not stall.cut:
                core.log(logf, "run finished on its own (rc=0)")
                produced = True
                core.qe_status(task_id, "produced", "rc0-acceptance-waiting")
                break
            else:
                if not stall.cut:
                    stuck = 0
                    core.log(logf, f"run fell (rc={rc})")

            n = core.commit_wip(wt, project_ref, "before outage") if not task_id else ""  # H4: lanes share one worktree; commits happen in the coordinator
            if n:
                core.log(logf, f"intermediate commit: {n} file")
            consecutive_controlled = consecutive_controlled + 1 if controlled_cut else 0
            if controlled_cut and consecutive_controlled > controlled_cap:   # B188: a never-ending series of budget denials/brakes does not loop without limit
                core.log(logf, f"cadre-guard: CONTROLLED-CUTTING CAP — {consecutive_controlled} consecutive controlled interruptions; counted as an attempt (B188)")
            task_handovers = core.RunStore(core.QE).read(core.ENV('CADRE_RUN_ID'))['tasks'].get(task_id or 'main', {}).get('path_handovers') or []
            path_cut = bool(task_handovers and task_handovers[-1].get('ts', 0) >= attempt_start)
            if not path_cut and (not controlled_cut or consecutive_controlled > controlled_cap):
                tries += 1  # brake/escalation interruptions (controlled) do not count as attempts up to the cap
                core.RunStore(core.QE).task_update(core.ENV("CADRE_RUN_ID"), task_id or "main", failure_count=tries)
            controlled_cut = False
            if tries > max_resume:
                core.log(logf, f"attempt limit ({max_resume}) reached; stopping")
                core.qe_autopsy(wt, project_ref, logf, "attempt-finished", real)
                break
    except PermissionError as exc:
        if not str(exc).startswith('project-tool-'):
            raise
        core.log(logf, f"TOOL BOUNDARY: {exc} — executor did not start; product and acceptance records preserved")
        produced = False
        leaving_reason = str(exc)
    except core.SessionRecoveryError as exc:
        # Identity unclear/contradictory: NO blind restart, the record is kept, a controlled partial (not a traceback).
        core.log(logf, f"SESSION IDENTITY: {exc} — run stopped, attempt records preserved; use `cadre proceed` after repair")
        produced = False
        leaving_reason = "session-id"
    except BaseException:
        produced = False
        leaving_reason = 'executor-error'
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
        n = core.commit_wip(wt, project_ref, "end of run") if not task_id else ""
        if n:
            core.log(logf, f"last commit: {n} file")
        # close states
        d = core.task_root(task_id) / "status.txt"
        # injection finding (E15): the 'cancel' state was OVERWRITTEN with 'half' here -> the chain could take a cancelled job
        # for a partial one and retry it. Definite states (complete/cancel) are kept.
        if not (d.exists() and d.read_text().split(" ")[0] in ("produced", "cancel")):
            core.qe_status(task_id, "half", leaving_reason or f"rc={rc}")
        p = core.task_root(task_id) / "worker.pid"
        if p.exists():
            p.unlink()
        # B110: only rc==1 counted as a violation; when the manifest was DELETED (rc=2) the run went silently to
        # success — so removing the protection was easier than passing it. Every non-zero result is a violation.
        if critical_verify() != 0:  # C23: a critical file changed/was deleted or the manifest was corrupted -> the product does not count as a success
            core.log(logf, "CRITICAL FILE VIOLATION: the run changed budget/config files; success denied (files restored from the signed snapshot)")
            produced = False
            if not (d.exists() and d.read_text().split(" ")[0] == "cancel"):
                core.qe_status(task_id, "half", "critical-file-violation")
    outcome = 0 if produced else 1
    if core.cancel_path(project_ref).exists():
        outcome = 130
    if not task_id and outcome == 0:
        acceptance = core.finalize_run(project_ref, wt, logf, real, restriction_env=restriction_env)
        outcome = 0 if acceptance["status"] in ("PASS", "APPLIED") else 1
        core.qe_status(task_id, "complete" if outcome == 0 else "half", "final-acceptance")
    # RUN LEDGER: the job's brain (opencode sessions, thinking, tool calls,
    # sub-agents) + engine log + git are dumped into one folder; third-eye analysis reads from here.
    try:
        r = core.subprocess.run([core.sys.executable, str(core.CODE / "lib/run_ledger.py"), project_ref, str(wt), str(logf), str(pf)],
                           capture_output=True, text=True, timeout=300)
        core.log(logf, f"run ledger: {r.stdout.strip() or r.stderr.strip()[-300:]}")
    except Exception as e:
        core.log(logf, f"Could not write the run ledger: {e}")
    core.log(logf, f"FINISHED. worktree={wt}  log={logf}")
    print(wt)
    return outcome
