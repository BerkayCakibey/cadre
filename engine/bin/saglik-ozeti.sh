#!/usr/bin/env bash
# Local health summary: one-shot, never interrupts or restarts work.
set -uo pipefail
QE="${CADRE_HOME:-$HOME/.local/share/cadre-engineering}"
KOD="${CADRE_ENGINE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
DB="${OPENCODE_DB:-$HOME/.local/share/opencode/opencode.db}"
BUTCE=$(python3 "$KOD/lib/butce.py" 2>/dev/null)
GO_DOGRULAMA=$(PYTHONPATH="$KOD/lib" CADRE_HOME="$QE" OPENCODE_DB="$DB" python3 - "$BUTCE" <<'PY'
import json, sys
import butce
import go_pencere

try:   # 4c-13: API percentages and the Σ formula (opencode DB) are compared at the same time, in three windows
    go = json.loads(sys.argv[1])["go"]
    api = go_pencere._onbellek(butce.QE)
    if go.get("db") != "ok" or not api.get("ts"):
        raise ValueError("Go gozlemi yok")
    kendi = go_pencere.kendi_yuzdeler(butce.DB, float(api["ts"]), api.get("resets"), butce.go_donem()[0])
    if kendi is None:
        raise ValueError("DB okunamadi")
    fark = {w: kendi[w] - float(api.get(w) or 0) for w in go_pencere.PENCERELER}
    if any(abs(v) > 1.5 for v in fark.values()):   # the API rounds to an integer
        print("go-puan-sapmasi(" + " ".join(f"{w}:Σ{kendi[w]:.1f}/api{float(api.get(w) or 0):.0f}" for w in go_pencere.PENCERELER) + ")")
    else:
        print(f"go=%{float(api.get('monthly') or 0):.0f} (Σ {kendi['monthly']:.1f})")
except (OSError, ValueError, KeyError, TypeError):
    print("go-hesabi-dogrulanamadi")
PY
)
case "$GO_DOGRULAMA" in
  go=*) echo "DOGRULAYICI: $GO_DOGRULAMA";;
  *) echo "DOGRULAYICI UYARI: ${GO_DOGRULAMA:-go-hesabi-dogrulanamadi}";;
esac
printf 'IZLEYICI:'
for p in saglik-nobet kota-izle; do
  if python3 "$KOD/lib/kosu.py" bekci-canli "$p" >/dev/null 2>&1; then printf ' %s:OK' "$p"
  else printf ' %s:OLU' "$p"; fi
done
printf '\n'
if [ -f "$QE/logs/saglik-nobet.log" ]; then
  echo "saglik son tur: $(stat -f %Sm -t '%F %T' "$QE/logs/saglik-nobet.log")"
fi
if [ "${1:-}" = --ayrinti ]; then
BUL=$(find "$QE/logs" "$QE/zincir/calisan" -name '*.log' -mmin -60 2>/dev/null | while read -r f; do
  tail -c 60000 "$f" 2>/dev/null | tr -d '\000' | grep -aoE 'Traceback|SyntaxError|unbound variable|No such file|BOZUK|payment_required|invalid_api_key|rate.?limit|"code":4[0-9][0-9]|Error: [A-Za-z]+'
done | sort | uniq -c | sort -rn | head -8)
if [ -n "$BUL" ]; then
  echo "HATA-TARAMA (60dk):"; printf '%s\n' "$BUL" | sed 's/^/  /'
else
  echo "HATA-TARAMA (60dk): temiz"
fi

fi
