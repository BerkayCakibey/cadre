#!/usr/bin/env bash
# Local health summary: one-shot, never interrupts or restarts work.
set -uo pipefail
QE="${CADRE_HOME:-$HOME/.local/share/cadre-engineering}"
CODE="${CADRE_ENGINE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
DB="${OPENCODE_DB:-$HOME/.local/share/opencode/opencode.db}"
BUDGET=$(python3 "$CODE/lib/provider_budget.py" 2>/dev/null)
GO_VERIFICATION=$(PYTHONPATH="$CODE/lib" CADRE_HOME="$QE" OPENCODE_DB="$DB" python3 - "$BUDGET" <<'PY'
import json, sys
import provider_budget
import go_window

try:   # 4c-13: API percentages and the Σ formula (opencode DB) are compared at the same time, in three windows
    go = json.loads(sys.argv[1])["go"]
    api = go_window._cache(provider_budget.QE)
    if go.get("db") != "ok" or not api.get("ts"):
        raise ValueError("no Go observation")
    own = go_window.own_percentages(provider_budget.DB, float(api["ts"]), api.get("resets"), provider_budget.go_period()[0])
    if own is None:
        raise ValueError("DB unreadable")
    difference = {w: own[w] - float(api.get(w) or 0) for w in go_window.WINDOWS}
    if any(abs(v) > 1.5 for v in difference.values()):   # the API rounds to an integer
        print("go-points-deviation(" + " ".join(f"{w}:Σ{own[w]:.1f}/api{float(api.get(w) or 0):.0f}" for w in go_window.WINDOWS) + ")")
    else:
        print(f"go=%{float(api.get('monthly') or 0):.0f} (Σ {own['monthly']:.1f})")
except (OSError, ValueError, KeyError, TypeError):
    print("go-account-unverifiable")
PY
)
case "$GO_VERIFICATION" in
  go=*) echo "VERIFIER: $GO_VERIFICATION";;
  *) echo "VERIFIER WARNING: ${GO_VERIFICATION:-go-account-unverifiable}";;
esac
printf 'WATCHER:'
for p in health-watch capacity-watch; do
  if python3 "$CODE/lib/run_store.py" guard-live "$p" >/dev/null 2>&1; then printf ' %s:OK' "$p"
  else printf ' %s:DEAD' "$p"; fi
done
printf '\n'
if [ -f "$QE/logs/health-watch.log" ]; then
  echo "health last round: $(stat -f %Sm -t '%F %T' "$QE/logs/health-watch.log")"
fi
if [ "${1:-}" = --detail ]; then
FIND=$(find "$QE/logs" "$QE/chain/running" -name '*.log' -mmin -60 2>/dev/null | while read -r f; do
  tail -c 60000 "$f" 2>/dev/null | tr -d '\000' | grep -aoE 'Traceback|SyntaxError|unbound variable|No such file|BROKEN|payment_required|invalid_api_key|rate.?limit|"code":4[0-9][0-9]|Error: [A-Za-z]+'
done | sort | uniq -c | sort -rn | head -8)
if [ -n "$FIND" ]; then
  echo "ERROR-SCAN (60min):"; printf '%s\n' "$FIND" | sed 's/^/  /'
else
  echo "ERROR-SCAN (60min): clean"
fi

fi
