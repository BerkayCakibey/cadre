#!/usr/bin/env bash
# saglik-nobet.sh — THE ONLY EXTERNAL SUPERVISOR (10 min tick). Data home = CADRE_HOME, code root = CADRE_ENGINE_ROOT.
# P1: NO fixed path to the live root — every helper runs from the code root (same behaviour as an isolated test/home).
# P2: liveness = zincir/saglik-nobet.pid + zincir/saglik-nobet.tik (heartbeat); not `pgrep -f`.
#   Tick: revive guards (kota-izle), revive a crashed/retryable coordinator (kosu.py revive), disk threshold + rotation,
#   ledger scan. CADRE_NOBET_ARALIK seconds, default 600 (same as kosu.BEKCILER). CADRE_DISK_MIN_MB threshold (default 2048).
QE="${CADRE_HOME:-$HOME/.local/share/cadre-engineering}"; export CADRE_HOME="$QE"
CADRE_ENGINE_ROOT="${CADRE_ENGINE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"; export CADRE_ENGINE_ROOT
. "$CADRE_ENGINE_ROOT/lib/common.sh"
qe_bekci_omur || exit 75
L="$QE/logs/saglik-nobet.log"; ARALIK="${CADRE_NOBET_ARALIK:-600}"
uyu(){ local k=$1 s; while [ "$k" -gt 0 ]; do qe_bekci_devam || exit 0; s=$(( k > 5 ? 5 : k )); sleep "$s"; k=$(( k - s )); done; }   # B266: even on SIGKILL an orphaned sleep lasts <=5 s
mkdir -p "$QE/logs" "$QE/zincir"
PIDF="$QE/zincir/saglik-nobet.pid"; TIK="$QE/zincir/saglik-nobet.tik"
if [ "$(cat "$PIDF" 2>/dev/null)" != "$$" ] && python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" bekci-canli saglik-nobet >/dev/null 2>&1; then
  echo "saglik-nobet zaten calisiyor (pid $(cat "$PIDF"))" >&2; exit 0
fi
echo $$ > "$PIDF"
python3 "$CADRE_ENGINE_ROOT/lib/runtime_sync.py" register saglik-nobet "$$" || exit 75
OCDB="${OPENCODE_DB:-$HOME/.local/share/opencode/opencode.db}"
while :; do
  # B47: if the pidfile moved to another guard (race) this loop exits quietly — a single guard
  [ "$(cat "$PIDF" 2>/dev/null)" = "$$" ] || { echo "$(date '+%F %T') pidfile baska bekciye gecti ($(cat "$PIDF" 2>/dev/null)); bu dongu cikiyor" >> "$L"; exit 0; }
  # B191: a guard whose test/audit owner died is not left orphaned (no variable in the live guard)
  [ -z "${CADRE_BEKCI_SAHIP_PID:-}" ] || kill -0 "$CADRE_BEKCI_SAHIP_PID" 2>/dev/null || { echo "$(date '+%F %T') sahip $CADRE_BEKCI_SAHIP_PID yok; cikiyor" >> "$L"; exit 0; }
  [ -d "$CADRE_ENGINE_ROOT" ] && [ -d "$QE" ] || exit 0
  date +%s > "$TIK"
  {
  echo "== $(date '+%F %H:%M') =="
  # system
  python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" health-logs 2>/dev/null || echo 'log:BELIRSIZ neden=okuma-hatasi'
  # CLAUDE ERROR CLASSES scan (in the code root)
  grep -l 'MARGS\[@\]' "$CADRE_ENGINE_ROOT"/bin/*.sh 2>/dev/null && echo "UYARI bash3.2 bos-dizi tuzagi geri gelmis"
  grep -lE 'read[^;|]*-t +[0-9]*\.[0-9]' "$CADRE_ENGINE_ROOT"/bin/*.sh "$CADRE_ENGINE_ROOT"/lib/*.sh 2>/dev/null && echo "UYARI kesirli -t (bash3.2)"
  for f in "${OPENCODE_CFG:-$HOME/.config/opencode}"/agents/*.md; do
    [ -f "$f" ] || continue
    python3 - "$f" <<'PY2' 2>/dev/null
import sys,re,collections
fm=open(sys.argv[1]).read().split('---')[1]
for b in re.finditer(r'\n  (\w+):\n((?:    .*\n)+)',fm):
    ks=[l.strip().split(':')[0] for l in b.group(2).split('\n') if l.strip() and not l.strip().startswith('#')]
    d={k:v for k,v in collections.Counter(ks).items() if v>1}
    if d: print(f"UYARI tekrar-anahtar {sys.argv[1].split('/')[-1]}:{b.group(1)}")
PY2
  done
  bash -n "$CADRE_ENGINE_ROOT/bin/is-motoru.sh" 2>/dev/null || echo "UYARI is-motoru sozdizimi BOZUK"
  bash -n "$CADRE_ENGINE_ROOT/bin/cadre-dispatch.sh" 2>/dev/null || echo "UYARI dispatcher sozdizimi BOZUK"
  # 4c-7(a): OR receipt ↔ breakdown sampling separated from the observation tick (must not push the 30 s observation limit; QA finding)
  python3 -c 'import sys,os; sys.path.insert(0, os.environ["CADRE_ENGINE_ROOT"] + "/lib"); import butce; r = butce.or_makbuz_denetimi(); r and print("OR MAKBUZ SAPMASI (" + ("OR kapandi" if r["kapandi"] else "ilk ornek; ikincisinde OR kapanir") + "): " + str(r["ornekler"])[:200])' 2>/dev/null
  python3 -c 'import sys,os; sys.path.insert(0, os.environ["CADRE_ENGINE_ROOT"] + "/lib"); import butce; n = butce.or_askida_kapat(); n and print("OR ASKIDA MUTABAKAT: " + str(n) + " satir uretim kaydindan kapandi")' 2>/dev/null
  # Single source butce.py: the remaining Go pool points (up to the wall) are read from there; no separate calculation here.
  python3 "$CADRE_ENGINE_ROOT/lib/butce.py" 2>/dev/null | python3 -c '
import json,sys,os,sqlite3
from pathlib import Path
sys.path.insert(0, os.environ["CADRE_ENGINE_ROOT"] + "/lib")
from butce_kapisi import gunluk_mutabakat
try: b=json.load(sys.stdin); g=b["go"]
except Exception: raise SystemExit(0)
try:
    m=gunluk_mutabakat(Path(os.environ["CADRE_HOME"]), b)
    if m: print(m)
except (OSError, ValueError, sqlite3.Error) as e: print("mutabakat okunamadi: " + type(e).__name__)
kalan=float(g.get("kullanilabilir") or 0); ay=float(g.get("ay_puan") or 0)
not_ = " UYARI: Go kapasitesi bitti — bulut yolu/butce beklenir" if kalan<=0 else (" (bol yer var)" if kalan>20 else "")
print(f"go-havuz: duvara {kalan:.1f} puan · bu ay %{ay:.1f}" + not_)
if not g.get("pencere_acik", True): print("UYARI Go penceresi KAPALI: " + str(g.get("pencere_neden")))
' 2>/dev/null
  # MORNING SUMMARY (item 10): written once a day, after 06:00. Ready when the user says `cadre ozet`
  # in the morning; left alone if the file already exists.
  OZETD="$QE/reports/OZET-$(date +%Y%m%d).md"
  if [ ! -f "$OZETD" ] && [ "$(date +%H)" -ge 06 ] 2>/dev/null; then
    python3 "$CADRE_ENGINE_ROOT/lib/sabah_ozeti.py" --yaz >/dev/null 2>&1 && echo "sabah ozeti yazildi: $OZETD"
  fi
  # P2 DISK THRESHOLD + ROTATION: when low, rotate now (archived cleanup); otherwise rotate once a day (04:xx).
  read -r DDURUM DBOS DESIK <<<"$(python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" disk 2>/dev/null)"
  echo "disk: ${DBOS:-?} MB bos (esik ${DESIK:-?} MB)"
  ROTF="$QE/zincir/rotasyon-$(date +%F).flag"
  if [ "${DDURUM:-ok}" = "dusuk" ]; then
    echo "UYARI disk dusuk: ${DBOS} MB < ${DESIK} MB — rotasyon calistiriliyor"
    echo "rotasyon: $(python3 "$CADRE_ENGINE_ROOT/lib/rotasyon.py" 2>&1 | tail -1)"
  elif [ "$(date +%H)" = "04" ] && [ ! -f "$ROTF" ]; then
    touch "$ROTF"; echo "rotasyon (gunluk): $(python3 "$CADRE_ENGINE_ROOT/lib/rotasyon.py" 2>&1 | tail -1)"
  fi
  # REVIVE: dead watchers (P2: pidfile/heartbeat measure; from the code root)
  R2=$(python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" bekci-dirilt kota-izle 2>/dev/null); [ -n "$R2" ] && echo "DIRILTILDI $R2"
  tail -1 "$QE/logs/kota-gidisat.log" 2>/dev/null
  # RUN LEDGER ZERO-LOSS: every updated session, including an engine killed before writing its ledger, the TUI and manual
  # `opencode run` sessions, is written to the ledger (read-only, ~seconds).
  python3 "$CADRE_ENGINE_ROOT/lib/kosu_defteri.py" tara 2>&1
  # C7/P4: a crashed coordinator (state=running, empty owner lock) and a retryable night run (retryable, limited)
  # are restarted a limited number of times; a queued run starts when the one ahead finishes — this is the only external supervisor.
  R=$(python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" revive 2>/dev/null); [ -n "$R" ] && echo "DIRILTILDI kosu: $R"
  } >> "$L" 2>&1
  uyu "$ARALIK"
done
