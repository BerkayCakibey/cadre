#!/usr/bin/env bash
# cadre — ONE COMMAND. Menu: 1 task · 2 watch · 3 status · 4 cancel · 5 push · 6 merge · 7 ledger · 8 summary · 9 balance.
# Usage: `cadre [task|watch|status|cancel|push|merge|proceed ID]`
set -uo pipefail
CADRE_ENGINE_ROOT="${CADRE_ENGINE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
export CADRE_ENGINE_ROOT
. "$CADRE_ENGINE_ROOT/lib/common.sh"
C=$'\033[38;5;44m'; D=$'\033[38;5;245m'; G=$'\033[38;5;42m'; Y=$'\033[38;5;220m'; RD=$'\033[38;5;203m'; B=$'\033[1m'; R=$'\033[0m'
{ [ -n "${NO_COLOR:-}" ] || [ ! -t 1 ]; } && C='' D='' G='' Y='' RD='' B='' R=''

root="$(qe_repo_root)"; project=""
[ -n "$root" ] && project="$(qe_project_for_dir "$root" 2>/dev/null || true)"

has_running(){ pgrep -f "qe_core.py runner ${1:-}|cadre-dispatch.sh ${1:-}|orchestrator.sh ${1:-}" >/dev/null 2>&1; }
# P2: every time `cadre` is called, dead guards are revived from the code root (one command brings everything up; a live pidfile is left alone)
guards_standing(){ python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" guard-revive >/dev/null 2>&1 || true; }
last_log(){
  local latest
  latest="$(python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" latest-log "$1")" || return 1
  if [ -n "$latest" ]; then printf '%s\n' "$latest"; else ls -t "$CADRE_LOGS"/job-"$1"-*.log 2>/dev/null | head -1; fi
}
last_ledger(){ ls -td "$CADRE_HOME"/reports/run-"$1"-* 2>/dev/null | head -1; }

symbol(){ python3 "$CADRE_ENGINE_ROOT/lib/screen_render.py" symbol "$1"; }
title(){ printf '\n%s%scadre · %s%s\n\n' "$C" "$B" "${project:-projects}" "$R"; }

# Item 0a: a stuck reservation / price failure silently locked the budget; the status screen now says so.
recovery_warning(){
  python3 - "$CADRE_HOME/chain/request-budget.db" <<'PY' 2>/dev/null
import sqlite3, sys, time
try:
    con = sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True)
    stale, amount = con.execute('select count(*),coalesce(sum(reserved),0) from reservation '
                               'where actual is null and created < ?', (time.time() - 6 * 3600,)).fetchone()
    try:
        failure = [r[0] for r in con.execute('select provider from pricing_fault')]
    except sqlite3.Error:
        failure = []
except sqlite3.Error:
    raise SystemExit(0)
if stale:
    print('%d stuck reservation(s) ($%.2f) older than 6 hours → cadre recover --stale-list' % (stale, amount / 1e6))
if failure:
    print('price failure, provider closed: %s → cadre recover --failure-list' % ', '.join(failure))
PY
}

# 0c: the Zen balance CANNOT BE READ by a program. The user enters it, and we subtract the spending we measured ourselves.
# `cadre zen-balance` -> 1 query, 2 top up. CADRE_ZEN_CHOICE / CADRE_ZEN_AMOUNT drive it from tests.
zen_file(){ printf '%s\n' "$CADRE_HOME/chain/zen-balance.txt"; }
status(){
  guards_standing
  python3 "$CADRE_ENGINE_ROOT/lib/screen_render.py" status "${1:-$project}"
}

zen_status(){
  python3 "$CADRE_ENGINE_ROOT/lib/provider_budget.py" 2>/dev/null | python3 -c '
import json,sys
try: z=json.load(sys.stdin).get("zen") or {}
except Exception: z={}
if not z or z.get("db")=="absent":
    print("no record — first enter the balance with option 2 of the `cadre zen-balance` menu"); raise SystemExit(0)
print("entered   : $%.2f  (entered on %s)" % (z["balance_entry"], z["entry_date"]))
print("spent     : $%.4f  (since that date, our own measurement)" % z["spent"])
print("remaining : $%.2f" % z["remaining"])
print("available : $%.3f" % z["available"])
if z.get("db")!="ok": print("WARNING: measurement state " + str(z.get("db")))
'
}
zen_load(){
  local f amount remaining fresh
  f="$(zen_file)"
  remaining="$(python3 "$CADRE_ENGINE_ROOT/lib/provider_budget.py" 2>/dev/null | python3 -c 'import json,sys
try: print((json.load(sys.stdin).get("zen") or {}).get("remaining") or 0)
except Exception: print(0)')"
  printf 'Current remaining (our own calculation): $%s\n' "$remaining"
  if [ -n "${CADRE_ZEN_AMOUNT:-}" ]; then amount="$CADRE_ZEN_AMOUNT"; else
    printf 'Amount you topped up (USD, number only; empty = give up): '; read -r amount || amount=""
  fi
  [[ "$amount" =~ ^[0-9]+(\.[0-9]+)?$ ]] || { echo "invalid amount, nothing was written" >&2; return 1; }
  fresh="$(python3 -c "print(f'{float('$remaining')+float('$amount'):.2f}')" 2>/dev/null)" || fresh=""
  [ -n "$fresh" ] || { echo "the balance could not be calculated, nothing was written" >&2; return 1; }
  printf 'Fresh balance: $%s  — spending from this date on will be subtracted from it.\n' "$fresh"
  mkdir -p "$(dirname "$f")"
  # B92: the baseline must be the ENTRY TIME. Without a timestamp provider_budget.py also subtracted what was spent EARLIER that day
  # and entering a fresh balance SHRANK the share (measured: $9.96 entered, the system said $2.34).
  printf '%sT%s %s\n' "$(date +%Y-%m-%d)" "$(date +%H:%M:%S)" "$fresh" > "$f.fresh" && mv -f "$f.fresh" "$f" || { echo "the balance could not be written" >&2; return 1; }
  printf 'written: %s\n' "$f"
}
balance_menu(){
  python3 "$CADRE_ENGINE_ROOT/lib/opencode_budget.py"
  local s
  if [ -n "${CADRE_BALANCE_CHOICE:-}" ]; then s="$CADRE_BALANCE_CHOICE"; else
    printf '\n%sBalance%s\n  1) zen: query\n  2) zen: top up\nChoice: ' "$B" "$R"
    read -r s || s=""
  fi
  case "$s" in
    1|"") zen_status ;;
    2)    zen_load ;;
    *)    echo "unknown choice" >&2; return 2 ;;
  esac
}

zen_balance(){
  local s
  if [ -n "${CADRE_ZEN_CHOICE:-}" ]; then s="$CADRE_ZEN_CHOICE"; else
    printf '\n%szen balance%s\n  1) query\n  2) top up\nChoice: ' "$B" "$R"; read -r s || s=""
  fi
  case "$s" in
    1|"") zen_status ;;
    2)    zen_load ;;
    *)    echo "unknown choice" >&2; return 2 ;;
  esac
}

status_detail(){
  local p="${1:-$project}"
  guards_standing
  printf '%s── STATUS ──────────────────────────────────────%s\n' "$D" "$R"
  if [ -n "$p" ]; then printf '  project_ref    : %s%s%s  (%s)\n' "$B" "$p" "$R" "$(qe_repo_path "$p")"
    printf '  active branch: %s\n' "$(git -C "$(qe_repo_path "$p")" branch --show-current 2>/dev/null)"
  else printf '  project_ref    : %s(this directory is not a registered project_ref)%s\n' "$Y" "$R"; fi
  local n; n=$(pgrep -f 'qe_core.py runner' | wc -l | tr -d ' ')
  if [ "$n" -gt 0 ]; then printf '  job      : %s%s engine(s) running%s — %s\n' "$G" "$n" "$R" "$(pgrep -fl 'qe_core.py runner' | awk '{print $NF}' | sort -u | tr '\n' ' ')"
  elif [ -n "$(python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" busy 2>/dev/null)" ]; then printf '  job      : %srun in progress%s (details: the run lines)\n' "$G" "$R"
  else printf '  job      : %snone%s\n' "$D" "$R"; fi
  # P2: liveness = pidfile + argv + heartbeat (run_store.py guard-live); P1: helpers from the code root
  printf '  guards   : health-watch %s · capacity-watch %s\n' "$(python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" guard-live health-watch >/dev/null 2>&1 && printf '%sOK%s' "$G" "$R" || printf '%sABSENT%s' "$RD" "$R")" "$(python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" guard-live capacity-watch >/dev/null 2>&1 && printf '%sOK%s' "$G" "$R" || printf '%sABSENT%s' "$RD" "$R")"
  if [ -n "$p" ]; then local tb; tb="$(delivery_candidates "$p" 2>/dev/null | grep -c . || true)"
    if [ "${tb:-0}" -gt 0 ]; then
      printf '  delivery : %sfinished jobs awaiting approval: %s%s  → menu 6 push · 7 merge\n' "$Y" "$tb" "$R"
      # finding: every run branches FROM THE MAIN BRANCH. Undelivered work does not accumulate; a new job does not see
      # the old fixes and QA may turn red because of that. The user should not hand out work without knowing this.
      printf '             %sthese do not accumulate if you hand out new work — every run starts from the main branch%s\n' "$D" "$R"
    fi; fi
  local bj; bj="$(python3 "$CADRE_ENGINE_ROOT/lib/provider_budget.py" 2>/dev/null)"
  [ -n "$bj" ] && printf '%s' "$bj" | python3 -c 'import json,sys,os;sys.path.insert(0,os.environ["CADRE_ENGINE_ROOT"]+"/lib");from screen_render import budget_summary;print("  budget   : "+budget_summary(json.load(sys.stdin)))'

  # Item 5: three windows on the status screen; visible before hitting the wall (from cache, no network)
  local pj; pj="$(python3 "$CADRE_ENGINE_ROOT/lib/go_window.py" --summary 2>/dev/null)"
  [ -n "$pj" ] && printf '  Go window: %s\n' "$pj"
  # Item 9: show the contract (budget split) of the running run
  local sz; sz="$(python3 "$CADRE_ENGINE_ROOT/lib/contract.py" summary 2>/dev/null)"
  [ -n "$sz" ] && printf '  %s\n' "$sz"
  if [ -n "$p" ]; then local pk; pk="$(python3 "$CADRE_ENGINE_ROOT/lib/project_mode.py" summary "$p" 2>/dev/null)"   # G-P6: project mode progress
    [ -n "$pk" ] && printf '  project_ref mode: %s\n' "$pk"; fi
  recovery_warning | sed "s/^/  ${Y}recovery${R} : /"
  if [ -n "$p" ]; then
    local L; L="$(last_log "$p")"
    # if the old engine log line is not found, no empty 'last job' line is printed; the structured state is on the 'run' line
    local SL; [ -n "$L" ] && SL="$(grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]{8} (FINISHED|STUCK|proceeding|RECOVERY|run starting|run finished|CANCEL|GUARD|cadre-guard)' "$L" | tail -1 | cut -c12-100)"
    [ -n "${SL:-}" ] && printf '  last job   : %s\n' "$SL"
    local K; K="$(last_ledger "$p")"; [ -n "$K" ] && printf '  last record: %s\n' "$(basename "$K")"
    # P4/P5: structured state (run.json): last run state/stage/reason, pending queue, spending — not from the log line
    python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" summary "$p" 2>/dev/null | sed 's/^/  run      : /'
  fi
  printf '%s───────────────────────────────────────────────%s\n' "$D" "$R"
}

task(){
  [ -n "$project" ] || qe_die "this directory is not a registered project_ref (no repo-path under projects/)"
  if has_running "$project"; then
    printf '%s⚑ A job is already running for %s; the new task is QUEUED and starts by itself when that one finishes (single writer per repo).%s\n' "$Y" "$project" "$R"
  fi
  local task="" line empty=0
  if [ -n "${1:-}" ]; then task="$1"$'\n'; else
  printf '\n%s[%s]%s Type or paste the task. To finish, press %sEnter twice%s on an empty line or write %sgo%s on a line of its own:\n\n' "$B" "$project" "$R" "$B" "$R" "$B" "$R"
  # Ctrl-D was confusing on the Mac (Cmd-D splits the terminal). End: a "go" line, two empty lines or EOF.
  [ -t 1 ] && printf '\033[r\033[?7h'
  # `|| [ -n "$line" ]`: also take the LAST line when it ends without a newline
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in go|GO|finished|FINISHED|.) break ;; esac
    if [ -z "$line" ]; then empty=$((empty+1)); [ "$empty" -ge 2 ] && [ -n "$task" ] && break; else empty=0; fi
    task="${task}${line}"$'\n'
  done
  fi
  printf '%s' "$task" | grep -q '[^[:space:]]' || { echo "empty task — exiting" >&2; return 1; }
  printf '\n%s%d-line task received.%s\n' "$D" "$(printf '%s\n' "$task" | wc -l | tr -d ' ')" "$R"
  local run_id pf
  run_id="$(printf '%s' "$task" | python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" accept "$project" -)" || return 1
  pf="$CADRE_HOME/runs/$run_id/prompt.md"
  if ! env -u CADRE_RUN_ID python3 "$CADRE_ENGINE_ROOT/lib/permission.py" model-consent "$run_id"; then
    printf 'Task saved: %s; waiting for permission to make paid model calls.\n' "$run_id"
    return 0
  fi
  export CADRE_RUN_ID="$run_id"
  guards_standing
  # P4: if the same project has unfinished work the record is `queued` — a second coordinator is NOT opened; the coordinator/guard takes over when the one ahead finishes.
  local st; st="$(python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" show "$run_id" | python3 -c 'import json,sys;print(json.load(sys.stdin)["state"])' 2>/dev/null)"
  if [ "$st" = "queued" ]; then
    printf '\n%s⏳ Task QUEUED: %s.%s A job in the same project has to finish first; this one starts by itself after that. You can close the terminal.\n' "$Y" "$run_id" "$R"
  else
    nohup bash "$CADRE_ENGINE_ROOT/bin/orchestrator.sh" "$project" "$pf" > "$CADRE_HOME/runs/$run_id/start.log" 2>&1 </dev/null &
    disown 2>/dev/null || true
    sleep 1
    printf '\n%s✓ Task saved: %s.%s You can close the terminal.\n' "$G" "$run_id" "$R"
  fi
  printf '  %swatch: cadre  ·  cancel: cadre cancel  ·  ledger (when the job ends): cadre ledger%s\n\n' "$D" "$R"
  [ "${CADRE_WATCH:-1}" = "1" ] && [ -t 1 ] && watch "$project"
  return 0
}

watch(){
  local p="${1:-$project}"; [ -n "$p" ] || qe_die "project_ref absent"
  local rid; rid="${CADRE_RUN_ID:-$(python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" latest-id "$p")}" || return 1
  if [ -n "$rid" ]; then
    # decision 9: the watcher closes itself when the job ends (watch.py "FINISHED" -> 1.5 s) -> delivery offer on the same screen
    python3 "$CADRE_ENGINE_ROOT/lib/watch.py" "$p" --run "$rid" ${CADRE_WATCH_ARGS:-}
    delivery_offer "$p" "$rid"
    return 0
  fi
  if ! has_running "$p" && [ -z "$(last_log "$p")" ]; then echo "no job to watch" >&2; return 1; fi
  exec python3 "$CADRE_ENGINE_ROOT/lib/watch.py" "$p" ${CADRE_WATCH_ARGS:-}
}

cancel(){
  local p="${1:-$project}"; [ -n "$p" ] || qe_die "project_ref absent"
  bash "$CADRE_ENGINE_ROOT/bin/cadre-cancel" "$p"
}

# --- DELIVERY: the system never pushes/merges on its own; the user picks from the menu, gives two separate explicit approvals, and the system does the work.
delivery_candidates(){ # $1=project -> lines "run_id|worktree|label": completed runs not MERGED yet (pushed ones are labelled)
  python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" list "$1" 2>/dev/null | python3 -c '
import json,os,sys
sys.path.insert(0, os.environ["CADRE_ENGINE_ROOT"] + "/lib")
from outcome import approved
from permission import delivery_candidate
for x in json.load(sys.stdin):
    # B110-3: "completed" is the evidence AT acceptance time; the tree may have changed AT delivery time (manual edit,
    # `cadre proceed`, an unfinished repair). approved() re-binds the current revision to the QA/judge evidence:
    # delivered product = reviewed product.
    if delivery_candidate(x):
        et = ("pushed " + x["delivery_push"]) if x.get("delivery_push") else ("finished " + str(x.get("finished_at") or x.get("updated_at") or x.get("created") or ""))
        print(x["run_id"] + "|" + x["worktree"] + "|" + et)'
}
pick_run(){ # $1=project [$2=run_id] -> "run_id|worktree"; that one if a single candidate, a numbered choice if several (CADRE_CHOICE for tests; empty = the latest)
  local p="$1" requested="${2:-}" line n i; local -a a=()
  while IFS= read -r line; do [ -n "$line" ] && a+=("$line"); done < <(delivery_candidates "$p")
  [ "${#a[@]}" -gt 0 ] || return 1
  if [ -n "$requested" ]; then
    for line in "${a[@]}"; do case "$line" in "$requested|"*) printf '%s' "$line" | cut -d'|' -f1-2; return 0;; esac; done
    return 1
  fi
  if [ "${#a[@]}" -eq 1 ]; then printf '%s' "${a[0]}" | cut -d'|' -f1-2; return 0; fi
  printf '%sFinished jobs not yet delivered:%s\n' "$B" "$R" >&2
  i=0; for line in "${a[@]}"; do i=$((i+1)); printf '  %d) %s  %s\n' "$i" "$(printf '%s' "$line" | cut -d'|' -f1 | cut -c1-15)" "$(printf '%s' "$line" | cut -d'|' -f3-)" >&2; done
  n="${CADRE_CHOICE:-}"
  if [ -z "$n" ]; then printf '%sWhich one? (number, empty = the latest): %s' "$Y" "$R" >&2; IFS= read -r n || n=""; fi
  [ -n "$n" ] || n="${#a[@]}"
  case "$n" in *[!0-9]*) qe_err "invalid choice: $n"; return 1;; esac
  [ "$n" -ge 1 ] && [ "$n" -le "${#a[@]}" ] || { qe_err "invalid choice: $n"; return 1; }
  printf '%s' "${a[$((n-1))]}" | cut -d'|' -f1-2
}
delivery_offer(){ # $1=project $2=run_id — when the watcher closes: if the job is completed and not merged, a one-key delivery offer (decision 9)
  local p="$1" rid="$2" key
  [ -n "$rid" ] || return 0
  pick_run "$p" "$rid" >/dev/null 2>&1 || return 0
  printf '\n%s✔ Job completed (QA + judge passed): %s%s\n' "$G" "$(printf '%s' "$rid" | cut -c1-15)" "$R"
  printf '%s[p]%s push   %s[m]%s merge   %s[q]%s quit : ' "$B" "$R" "$B" "$R" "$B" "$R"
  key="${CADRE_DELIVERY_KEY:-}"
  if [ -z "$key" ]; then
    if [ -t 0 ]; then IFS= read -r -n1 key || key=q; echo; else echo; return 0; fi
  else echo "$key"; fi
  case "$key" in p|P) push_product "$p" "$rid" ;; m|M) merge_product "$p" "$rid" ;; *) printf '%snot delivered — you can do it later from menu 5/6.%s\n' "$D" "$R" ;; esac
}
approval(){ # $1=question $2=env-name -> 0 yes / 1 no. Tests answer with CADRE_APPROVAL*.
  local answer="${!2:-}"
  if [ -z "$answer" ]; then printf '%s%s (yes/no): %s' "$Y" "$1" "$R" >&2; IFS= read -r answer || answer=""; fi
  case "$answer" in yes|YES|y|Y) return 0;; *) printf '%sgiven up.%s\n' "$D" "$R"; return 1;; esac
}
two_approval(){
  approval "$1" "$3" || return 1
  printf '%sCommand to run:%s %s\n' "$B" "$R" "$2"
  approval "Second approval for this command" "${3}_2"
}
delivery_save(){ printf '{"delivery_%s": "%s"}' "$2" "$(date '+%F %T')" | python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" update "$1" >/dev/null 2>&1 || true; }
approved_sha(){
  python3 - "$1" <<'PY'
import os, sys
sys.path.insert(0, os.environ["CADRE_ENGINE_ROOT"] + "/lib")
from run_store import RunStore
from outcome import approved
k = RunStore().read(sys.argv[1])
if (k.get('final_result') or {}).get('delivery', {}).get('git_permission') == 'required':
    raise SystemExit(1)
if k.get("state") != "completed" or not k.get("worktree") or "delivery_merge" in k or not approved(k):
    raise SystemExit(1)
print(k["final_result"]["qa"]["revision"]["head"])
PY
}
still_approved(){
  [ "$(approved_sha "$1" 2>/dev/null)" = "$2" ] && return 0
  qe_err "the product changed after approval; the reviewed commit is not $2, nothing was delivered"
  return 1
}
main_repo_ready(){
  [ "$(git -C "$1" branch --show-current 2>/dev/null)" = "$2" ] || { qe_err "the main repo is on branch '$(git -C "$1" branch --show-current 2>/dev/null)'; it must be on '$2' to merge (git -C \"$1\" checkout $2)"; return 1; }
  [ -z "$(git -C "$1" status --porcelain 2>/dev/null)" ] || { qe_err "the main repo has uncommitted changes; no merge"; return 1; }
}
push_product(){
  local p="${1:-$project}" info rid
  info="$(pick_run "$p" "${2:-}")" || { qe_err "no COMPLETED run to deliver"; return 1; }
  rid="${info%%|*}"
  python3 "$CADRE_ENGINE_ROOT/lib/permission.py" deliver "$rid" push || return 1
}
merge_product(){
  local p="${1:-$project}" info rid
  info="$(pick_run "$p" "${2:-}")" || { qe_err "no COMPLETED run to deliver"; return 1; }
  rid="${info%%|*}"
  python3 "$CADRE_ENGINE_ROOT/lib/permission.py" deliver "$rid" merge || return 1
}

ledger(){
  local p="${1:-$project}"; local K; K="$(last_ledger "$p")"
  [ -n "$K" ] || { echo "no ledger"; return 1; }
  printf '%s\n' "$K"; printf '%s  transcript.md · meta.json · runner.log · watchers/ · change.patch%s\n' "$D" "$R"
  command -v code >/dev/null && printf '%s  open: code "%s/transcript.md"%s\n' "$D" "$K" "$R"
}

menu(){
  title; status
  # user request: ONE command — typing `cadre` shows every subcommand here, picked with the arrow keys.
  local option; local -a ops=(
    "$(symbol task) Give a new task         — paste the task and go; the live stream opens"
    "$(symbol watch) Reopen the screen       — back to the running job's live stream (q closes it, the job goes on)"
    "$(symbol cancel) Cancel the job          — stop the running job safely (the code stays in the worktree)"
    "$(symbol summary) Night summary           — runs, spend, the three windows, things that need attention"
    "$(symbol watch) Job record folder       — the full record of a finished job (hand it to Claude for analysis)"
    "$(symbol push) Push the product        — pick a finished job, send its cadre branch to origin (asks for approval)"
    "$(symbol merge) Merge into the canonical branch — pick a finished job, merge it into the main branch (asks for approval)"
    "$(symbol recovery) Recovery                — stuck reservation / price failure: list, confirm, close"
    "$(symbol balance) Enter/view balance      — subscription and credits; balance entry"
    "Quit")
  while :; do
    option="$(qe_select "${B}What shall we do?${R}" "${ops[@]}")"
    case "$option" in
      1) task; return $? ;;
      2) watch "$project"; return $? ;;
      3) cancel "$project" ;;
      4) summary_screen ;;
      5) ledger "$project" ;;
      6) push_product "$project" ;;
      7) merge_product "$project" ;;
      8) recovery_menu ;;
      9) balance_menu ;;
      0|10|"") return 0 ;;
    esac
  done
}

summary_screen(){
  local key
  python3 "$CADRE_ENGINE_ROOT/lib/morning_summary.py"
  while [ -t 0 ]; do
    printf '\nd details · q back: '
    IFS= read -r -s -n1 key || return 0
    printf '\n'
    case "$key" in
      d|D) python3 "$CADRE_ENGINE_ROOT/lib/morning_summary.py" --detail | ${PAGER:-less -R} ;;
      q|Q|"") return 0 ;;
    esac
  done
}

recovery_menu(){
  local s
  s="$(qe_select "${B}Recovery${R}" \
      "List stuck reservations" \
      "Close stuck reservations   — the reserved amount counts as spent" \
      "RESET stuck reservations   — if the calls were never made (the money comes back)" \
      "List price failures" \
      "Clear price failures" \
      "Back")"
  case "$s" in
    1) python3 "$CADRE_ENGINE_ROOT/lib/budget_gate.py" --stale-list ;;
    2) approval "Close the remaining stuck reservations? (the reserved amount counts as spent)" CADRE_APPROVAL \
         && python3 "$CADRE_ENGINE_ROOT/lib/budget_gate.py" --stale-close --yes ;;
    3) approval "RESET the reservations? Pick this only if you know the calls were never made." CADRE_APPROVAL \
         && python3 "$CADRE_ENGINE_ROOT/lib/budget_gate.py" --stale-close --zero --yes ;;
    4) python3 "$CADRE_ENGINE_ROOT/lib/budget_gate.py" --failure-list ;;
    5) approval "Clear the price failure? If its cause is not fixed it comes right back." CADRE_APPROVAL \
         && python3 "$CADRE_ENGINE_ROOT/lib/budget_gate.py" --failure-clean --yes ;;
  esac
}

case "${1:-}" in
  "")      if [ -t 0 ] && [ -t 1 ]; then
             py="${CADRE_UI_PYTHON:-$HOME/.local/share/cadre-ui-venv/bin/python}"
             if [ -z "${CADRE_OLD_MENU:-}" ] && [ -x "$py" ]; then guards_standing; CADRE_PROJECT_REF="$project" "$py" "$CADRE_ENGINE_ROOT/lib/panel_app.py"; else menu; fi
           else task; fi ;;
  task)   shift; task "$*" ;;
  request)   shift; exec python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" request "$@" ;;
  project-step-verify|request-proposal|request-approve|request-plan|plan-show|plan-backlog|plan-answer|plan-change|plan-stop|plan-withdraw|plan-review|plan-resume|test-maintenance)
    action="$1"; shift; exec python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" "$action" "$@" ;;
  trace)      shift; exec python3 "$CADRE_ENGINE_ROOT/lib/panel_data.py" "$@" ;;
  init)    shift; if [ $# -eq 0 ]; then set -- "$PWD"; fi; python3 "$CADRE_ENGINE_ROOT/lib/project_registry.py" "$@" ;;
  permission-parse) shift; env -u CADRE_RUN_ID python3 "$CADRE_ENGINE_ROOT/lib/permission.py" resolve "$@" ;;
  watch)    watch "${2:-$project}" ;;
  status)   if [ "${2:-}" = "--detail" ]; then status_detail "${3:-$project}"; else status "${2:-$project}"; fi ;;
  cancel)   cancel "${2:-$project}" ;;
  ledger)  ledger "${2:-$project}" ;;
  cleanup-backup) python3 "$CADRE_ENGINE_ROOT/lib/permission.py" "$1" "${2:-}" ;;
  cleanup|restore) python3 "$CADRE_ENGINE_ROOT/lib/permission.py" "$1" "${2:-}" ;;
  push)    push_product "${2:-$project}" "${3:-}" ;;
  merge)   merge_product "${2:-$project}" "${3:-}" ;;
  runs) python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" list ${project:+"$project"} ;;
  recover)  shift; python3 "$CADRE_ENGINE_ROOT/lib/budget_gate.py" "${@:---stale-list}" ;;
  cost_report)   shift; exec python3 "$CADRE_ENGINE_ROOT/lib/cost_report.py" "$@" ;;
  install-check) shift; exec python3 "$CADRE_ENGINE_ROOT/lib/install_check.py" "$@" ;;
  summary)   shift; python3 "$CADRE_ENGINE_ROOT/lib/morning_summary.py" "$@" ;;
  design) py="${CADRE_UI_PYTHON:-$HOME/.local/share/cadre-ui-venv/bin/python}"; [ -x "$py" ] || qe_die "no Textual environment: python3 -m venv ~/.local/share/cadre-ui-venv && ~/.local/share/cadre-ui-venv/bin/pip install textual"; "$py" "$CADRE_ENGINE_ROOT/lib/panel_app.py" --sample ;;
  zen-balance) zen_balance ;;
  balance)     balance_menu ;;
  budget)      python3 "$CADRE_ENGINE_ROOT/lib/opencode_budget.py" ;;
  proceed)
    [ -n "${2:-}" ] || qe_die "usage: cadre proceed RUN_ID"
    python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" start "$2" || exit 1
    # B110: `--proceed` is the operator's EXPLICIT request; it puts a failed run back in. Automatic paths cannot do this.
    printf 'Proceed request saved: %s (no second executor starts if it has an owner)\n' "$2"
    ;;
  spend-approval)   # spending alarm approval; the stopped run restarts (no second executor if it has an owner)
    [ -n "${2:-}" ] || qe_die "usage: cadre spend-approval RUN_ID"
    python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" spend-approval "$2" || exit 1
    python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" start "$2" || exit 1
    ;;
  -h|--help|help) cat <<'HELP'
cadre — a single command. Run without arguments to open the menu.

  cadre                  menu (task, watch, cancel, summary, delivery, recovery, balance)
  cadre task [text]      new job; without text the editor opens
  cadre request          stdin JSON: a record of the same shape as the panel's; waits at the plan gate, opens no executor
  cadre trace ID         read-only masked event trace; --format json|markdown, --replay stdin JSON
  cadre init [--create] [--round python] [dir]   registers this directory (or the given one) as a Cadre project; --create opens an empty directory and asks permission for the first commit; --round puts a deterministic skeleton into the first commit and records make install/test/run
  cadre permission-parse ID TOKEN ineffective|completed   closes a half-finished (in-flight/uncertain) allowed action by operator decision
  cadre watch            live watcher of the running job
  cadre status [--detail] summary of the last run
  cadre cancel [ID]      stop the running job
  cadre proceed ID       restart a failed/half-finished run as the operator
  cadre spend-approval ID approve and continue a run stopped by the spending alarm
  cadre push|merge       deliver a finished job (both ask for approval)
  cadre ledger|summary   run ledger · morning summary (--detail / --write)
  cadre balance          enter or view the zen balance
  cadre budget           opencode: Go subscription + credit/zen balance on one screen
  cadre recover [...]    stuck reservation/phantom recovery tools; --receipt-list [ID] / --receipt-acceptance RES --yes accepts an excess receipt
  cadre cost_report ID   stage_label/pool expense and duration breakdown (settled/estimated/pending; unknown is not written as zero)
  cadre install-check    read-only prerequisite/version/path check; installs and downloads nothing (--json)
  cadre design           the same interface with fake data (cadre: real data; CADRE_OLD_MENU=1 old menu)
HELP
    python3 - <<'PY'
import os, sys
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
from limits import runner_durations
try:
    warning, limit = runner_durations()
    print(f'Engine duration: warning {warning} s (0=off), hard limit {limit} s; the clock is not reset on proceed.')
    print('CADRE_WARN_SEC / CADRE_ABORT_SEC; the warning does not cut the job, the hard limit applies even when progress is being made.')
except ValueError:
    print('Invalid engine duration setting: warning must be >=0 and the hard limit >0, in whole seconds.', file=sys.stderr)
    raise SystemExit(2)
PY
    ;;
  *) echo "unknown: $1 (cadre | task | init | watch | status | cancel | ledger | push | merge | recover | zen-balance | spend-approval | summary | design)" >&2; exit 2 ;;
esac
