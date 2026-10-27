#!/usr/bin/env bash
# kanarya.sh — weekly liveness probe: one mini request to each provider (~$0.01 in total).
# Goal: catch outside-world changes (API/flag/model name) beforehand, not in the MIDDLE of a job.
# B110: home was fixed; must run in an isolated home (CADRE_HOME).
# cron PATH is /usr/bin:/bin: python3 3.9 (not the engine's 3.14), no claude/opencode -> since 14.09 claude was a PROBLEM and the new probe crashed
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
QE="${CADRE_HOME:-$HOME/.local/share/cadre-engineering}"; L="$QE/logs/kanarya.log"
KOD="${CADRE_ENGINE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
mkdir -p "$QE/logs" "$QE/zincir"
k(){ printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$L"; }
k "=== kanarya basladi ==="
probe(){ Y=$(PP="Tek kelime yaz: TAMAM" DM="$2" PROV="$1" DUZ=1 python3 "$KOD/lib/plan_ds.py" 2>&1 | head -c 60)
  case "$Y" in *TAMAM*) k "$1: SAGLAM";; *) k "$1: SORUN -> $Y";; esac; }
probe groq openai/gpt-oss-120b
CJ=$("${CLAUDE_BIN:-claude}" -p "Tek kelime yaz: TAMAM" --model haiku --output-format json --max-turns 1 --max-budget-usd 0.05 </dev/null 2>/dev/null)
printf '%s' "$CJ" | grep -q TAMAM && k "claude: SAGLAM" || k "claude: SORUN -> $(printf '%s' "$CJ" | head -c 80)"
# 4c-19: reality probes (Go window/Σ, endpoint limits, Sol tiers, thinking text, price change); cash $0
GERCEK=$(cd "$KOD/lib" && CADRE_HOME="$QE" python3 gerceklik.py 2>&1); GRC=$?
printf '%s\n' "$GERCEK" | grep '^gerceklik ' | while IFS= read -r satir; do k "$satir"; done
[ "$GRC" = 0 ] || k "gerceklik: SORUN -> calismadi (rc=$GRC): $(printf '%s' "$GERCEK" | tail -1 | head -c 160)"
SORUNLU=$(awk '/=== kanarya basladi ===/{n=0} /SORUN/{n++} END{print n+0}' "$L")
if [ "$SORUNLU" -gt 0 ]; then touch "$QE/zincir/kanarya-uyari.flag"; else rm -f "$QE/zincir/kanarya-uyari.flag"; fi   # a green run clears the old warning
k "=== kanarya bitti (sorunlu: $SORUNLU) ==="
