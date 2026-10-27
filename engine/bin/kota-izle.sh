#!/usr/bin/env bash
# hourly Go spending record (5-hour window) -> logs/kota-gidisat.log
# C29: the line comes from lib/kota_gidisat.py (Σ formula, Go points — same measure as butce.py).
# P1/P2: code root CADRE_ENGINE_ROOT (from its own directory), data home CADRE_HOME; liveness zincir/kota-izle.{pid,tik}.
QE="${CADRE_HOME:-$HOME/.local/share/cadre-engineering}"; CADRE_ENGINE_ROOT="${CADRE_ENGINE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
export CADRE_HOME="$QE" CADRE_ENGINE_ROOT
. "$CADRE_ENGINE_ROOT/lib/common.sh"
qe_bekci_omur || exit 75
mkdir -p "$QE/logs" "$QE/zincir"
PIDF="$QE/zincir/kota-izle.pid"
uyu(){ local k=$1 s; while [ "$k" -gt 0 ]; do qe_bekci_devam || exit 0; s=$(( k > 5 ? 5 : k )); sleep "$s"; k=$(( k - s )); done; }   # B266: even on SIGKILL an orphaned sleep lasts <=5 s
if [ "$(cat "$PIDF" 2>/dev/null)" != "$$" ] && python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" bekci-canli kota-izle >/dev/null 2>&1; then
  exit 0
fi
echo $$ > "$PIDF"
python3 "$CADRE_ENGINE_ROOT/lib/runtime_sync.py" register kota-izle "$$" || exit 75
while :; do
  [ "$(cat "$PIDF" 2>/dev/null)" = "$$" ] || exit 0  # B47: the pidfile moved to another guard -> only one guard remains
  [ -z "${CADRE_BEKCI_SAHIP_PID:-}" ] || kill -0 "$CADRE_BEKCI_SAHIP_PID" 2>/dev/null || exit 0   # B191: test guard whose owner died
  [ -d "$CADRE_ENGINE_ROOT" ] && [ -d "$QE" ] || exit 0
  date +%s > "$QE/zincir/kota-izle.tik"
  python3 "$CADRE_ENGINE_ROOT/lib/kota_gidisat.py" >> "$QE/logs/kota-gidisat.log" 2>/dev/null
  uyu "${CADRE_KOTA_ARALIK:-3600}"
done
