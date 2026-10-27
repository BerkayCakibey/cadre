#!/usr/bin/env bash
# hourly Go spending record (5-hour window) -> logs/quota-trend.log
# C29: the line comes from lib/quota_trend.py (Σ formula, Go points — same measure as provider_budget.py).
# P1/P2: code root CADRE_ENGINE_ROOT (from its own directory), data home CADRE_HOME; liveness chain/capacity-watch.{pid,tik}.
QE="${CADRE_HOME:-$HOME/.local/share/cadre-engineering}"; CADRE_ENGINE_ROOT="${CADRE_ENGINE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
export CADRE_HOME="$QE" CADRE_ENGINE_ROOT
. "$CADRE_ENGINE_ROOT/lib/common.sh"
qe_guard_lifetime || exit 75
mkdir -p "$QE/logs" "$QE/chain"
PIDF="$QE/chain/capacity-watch.pid"
nap(){ local k=$1 s; while [ "$k" -gt 0 ]; do qe_guard_proceed || exit 0; s=$(( k > 5 ? 5 : k )); sleep "$s"; k=$(( k - s )); done; }   # B266: even on SIGKILL an orphaned sleep lasts <=5 s
if [ "$(cat "$PIDF" 2>/dev/null)" != "$$" ] && python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" guard-live capacity-watch >/dev/null 2>&1; then
  exit 0
fi
echo $$ > "$PIDF"
python3 "$CADRE_ENGINE_ROOT/lib/runtime_sync.py" register capacity-watch "$$" || exit 75
while :; do
  [ "$(cat "$PIDF" 2>/dev/null)" = "$$" ] || exit 0  # B47: the pidfile moved to another guard -> only one guard remains
  [ -z "${CADRE_GUARD_OWNER_PID:-}" ] || kill -0 "$CADRE_GUARD_OWNER_PID" 2>/dev/null || exit 0   # B191: test guard whose owner died
  [ -d "$CADRE_ENGINE_ROOT" ] && [ -d "$QE" ] || exit 0
  date +%s > "$QE/chain/capacity-watch.tick"
  python3 "$CADRE_ENGINE_ROOT/lib/quota_trend.py" >> "$QE/logs/quota-trend.log" 2>/dev/null
  nap "${CADRE_QUOTA_INTERVAL:-3600}"
done
