#!/usr/bin/env bash
# health-watch.sh — THE ONLY EXTERNAL SUPERVISOR (10 min tick). Data home = CADRE_HOME, code root = CADRE_ENGINE_ROOT.
# P1: NO fixed path to the live root — every helper runs from the code root (same behaviour as an isolated test/home).
# P2: liveness = chain/health-watch.pid + chain/health-watch.tik (heartbeat); not `pgrep -f`.
#   Tick: revive guards (capacity-watch), revive a crashed/retryable coordinator (run_store.py revive), disk threshold + rotation,
#   ledger scan. CADRE_WATCH_INTERVAL seconds, default 600 (same as run_store.GUARDS). CADRE_DISK_MIN_MB threshold (default 2048).
QE="${CADRE_HOME:-$HOME/.local/share/cadre-engineering}"; export CADRE_HOME="$QE"
CADRE_ENGINE_ROOT="${CADRE_ENGINE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"; export CADRE_ENGINE_ROOT
. "$CADRE_ENGINE_ROOT/lib/common.sh"
qe_guard_lifetime || exit 75
L="$QE/logs/health-watch.log"; INTERVAL="${CADRE_WATCH_INTERVAL:-600}"
nap(){ local k=$1 s; while [ "$k" -gt 0 ]; do qe_guard_proceed || exit 0; s=$(( k > 5 ? 5 : k )); sleep "$s"; k=$(( k - s )); done; }   # B266: even on SIGKILL an orphaned sleep lasts <=5 s
mkdir -p "$QE/logs" "$QE/chain"
PIDF="$QE/chain/health-watch.pid"; TICK="$QE/chain/health-watch.tick"
if [ "$(cat "$PIDF" 2>/dev/null)" != "$$" ] && python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" guard-live health-watch >/dev/null 2>&1; then
  echo "health-watch already running (pid $(cat "$PIDF"))" >&2; exit 0
fi
echo $$ > "$PIDF"
python3 "$CADRE_ENGINE_ROOT/lib/runtime_sync.py" register health-watch "$$" || exit 75
OCDB="${OPENCODE_DB:-$HOME/.local/share/opencode/opencode.db}"
while :; do
  # B47: if the pidfile moved to another guard (race) this loop exits quietly — a single guard
  [ "$(cat "$PIDF" 2>/dev/null)" = "$$" ] || { echo "$(date '+%F %T') pidfile passed to another guard ($(cat "$PIDF" 2>/dev/null)); this loop exits" >> "$L"; exit 0; }
  # B191: a guard whose test/audit owner died is not left orphaned (no variable in the live guard)
  [ -z "${CADRE_GUARD_OWNER_PID:-}" ] || kill -0 "$CADRE_GUARD_OWNER_PID" 2>/dev/null || { echo "$(date '+%F %T') owner $CADRE_GUARD_OWNER_PID is gone; exiting" >> "$L"; exit 0; }
  [ -d "$CADRE_ENGINE_ROOT" ] && [ -d "$QE" ] || exit 0
  date +%s > "$TICK"
  {
  echo "== $(date '+%F %H:%M') =="
  # system
  python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" health-logs 2>/dev/null || echo 'log:UNCERTAIN reason=reading-error'
  # CLAUDE ERROR CLASSES scan (in the code root)
  grep -l 'MARGS\[@\]' "$CADRE_ENGINE_ROOT"/bin/*.sh 2>/dev/null && echo "WARNING the bash 3.2 empty-array trap is back"
  grep -lE 'read[^;|]*-t +[0-9]*\.[0-9]' "$CADRE_ENGINE_ROOT"/bin/*.sh "$CADRE_ENGINE_ROOT"/lib/*.sh 2>/dev/null && echo "WARNING fractional -t (bash 3.2)"
  for f in "${OPENCODE_CFG:-$HOME/.config/opencode}"/agents/*.md; do
    [ -f "$f" ] || continue
    python3 - "$f" <<'PY2' 2>/dev/null
import sys,re,collections
fm=open(sys.argv[1]).read().split('---')[1]
for b in re.finditer(r'\n  (\w+):\n((?:    .*\n)+)',fm):
    ks=[l.strip().split(':')[0] for l in b.group(2).split('\n') if l.strip() and not l.strip().startswith('#')]
    d={k:v for k,v in collections.Counter(ks).items() if v>1}
    if d: print(f"WARNING repeat-key {sys.argv[1].split('/')[-1]}:{b.group(1)}")
PY2
  done
  bash -n "$CADRE_ENGINE_ROOT/bin/job-runner.sh" 2>/dev/null || echo "WARNING job-runner syntax BROKEN"
  bash -n "$CADRE_ENGINE_ROOT/bin/cadre-dispatch.sh" 2>/dev/null || echo "WARNING dispatcher syntax BROKEN"
  # 4c-7(a): OR receipt ↔ breakdown sampling separated from the observation tick (must not push the 30 s observation limit; QA finding)
  python3 -c 'import sys,os; sys.path.insert(0, os.environ["CADRE_ENGINE_ROOT"] + "/lib"); import provider_budget; r = provider_budget.or_receipt_audit(); r and print("OR RECEIPT DEVIATION (" + ("OR closed" if r["closed"] else "first sample; OR closes on the second") + "): " + str(r["samples"])[:200])' 2>/dev/null
  python3 -c 'import sys,os; sys.path.insert(0, os.environ["CADRE_ENGINE_ROOT"] + "/lib"); import provider_budget; n = provider_budget.or_pending_close(); n and print("OR PENDING RECONCILIATION: " + str(n) + " row(s) closed from the production record")' 2>/dev/null
  # Single source provider_budget.py: the remaining Go pool points (up to the wall) are read from there; no separate calculation here.
  python3 "$CADRE_ENGINE_ROOT/lib/provider_budget.py" 2>/dev/null | python3 -c '
import json,sys,os,sqlite3
from pathlib import Path
sys.path.insert(0, os.environ["CADRE_ENGINE_ROOT"] + "/lib")
from budget_gate import daily_reconciliation
try: b=json.load(sys.stdin); g=b["go"]
except Exception: raise SystemExit(0)
try:
    m=daily_reconciliation(Path(os.environ["CADRE_HOME"]), b)
    if m: print(m)
except (OSError, ValueError, sqlite3.Error) as e: print("reconciliation unreadable: " + type(e).__name__)
remaining=float(g.get("available") or 0); month=float(g.get("month_points") or 0)
note = " WARNING: Go capacity exhausted — expect the cloud path/budget" if remaining<=0 else (" (plenty of room)" if remaining>20 else "")
print(f"go-pool: {remaining:.1f} points to the wall · this month {month:.1f}%" + note)
if not g.get("window_open", True): print("WARNING Go window CLOSED: " + str(g.get("window_reason")))
' 2>/dev/null
  # MORNING SUMMARY (item 10): written once a day, after 06:00. Ready when the user says `cadre summary`
  # in the morning; left alone if the file already exists.
  SUMMARY_FILE="$QE/reports/SUMMARY-$(date +%Y%m%d).md"
  if [ ! -f "$SUMMARY_FILE" ] && [ "$(date +%H)" -ge 06 ] 2>/dev/null; then
    python3 "$CADRE_ENGINE_ROOT/lib/morning_summary.py" --write >/dev/null 2>&1 && echo "morning summary written: $SUMMARY_FILE"
  fi
  # P2 DISK THRESHOLD + ROTATION: when low, rotate now (archived cleanup); otherwise rotate once a day (04:xx).
  read -r DISK_STATE DISK_FREE DISK_THRESHOLD <<<"$(python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" disk 2>/dev/null)"
  echo "disk: ${DISK_FREE:-?} MB free (threshold ${DISK_THRESHOLD:-?} MB)"
  ROTATION_FLAG="$QE/chain/rotation-$(date +%F).flag"
  if [ "${DISK_STATE:-ok}" = "low" ]; then
    echo "WARNING disk low: ${DISK_FREE} MB < ${DISK_THRESHOLD} MB — running rotation"
    echo "rotation: $(python3 "$CADRE_ENGINE_ROOT/lib/rotation.py" 2>&1 | tail -1)"
  elif [ "$(date +%H)" = "04" ] && [ ! -f "$ROTATION_FLAG" ]; then
    touch "$ROTATION_FLAG"; echo "rotation (daily): $(python3 "$CADRE_ENGINE_ROOT/lib/rotation.py" 2>&1 | tail -1)"
  fi
  # REVIVE: dead watchers (P2: pidfile/heartbeat measure; from the code root)
  R2=$(python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" guard-revive capacity-watch 2>/dev/null); [ -n "$R2" ] && echo "REVIVED $R2"
  tail -1 "$QE/logs/quota-trend.log" 2>/dev/null
  # RUN LEDGER ZERO-LOSS: every updated session, including an engine killed before writing its ledger, the TUI and manual
  # `opencode run` sessions, is written to the ledger (read-only, ~seconds).
  python3 "$CADRE_ENGINE_ROOT/lib/run_ledger.py" scan 2>&1
  # C7/P4: a crashed coordinator (state=running, empty owner lock) and a retryable night run (retryable, limited)
  # are restarted a limited number of times; a queued run starts when the one ahead finishes — this is the only external supervisor.
  R=$(python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" revive 2>/dev/null); [ -n "$R" ] && echo "REVIVED run: $R"
  } >> "$L" 2>&1
  nap "$INTERVAL"
done
