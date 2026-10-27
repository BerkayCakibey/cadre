#!/usr/bin/env bash
# kisitli-kabuk.sh — S2/C23: protection of budget/config files during a cloud-worker run.
# sign: SHA-256 of the critical files + a content-addressed backup. verify: a changed OR DELETED file is a VIOLATION ->
# only that file comes back from its copy at signing time; rc=1 (the engine processes it, success is not recorded).
# Usage: kisitli-kabuk.sh imza|dogrula <label> [-]  (CADRE_KRITIK_DOSYALAR=a:b:c changes the list in tests)
# B288: sign prints the manifest to stdout; `dogrula <label> -` takes the manifest ONLY from stdin (the engine's memory).
set -uo pipefail
QE="${CADRE_HOME:-$HOME/.local/share/cadre-engineering}"
KRITIK=("$HOME/.config/opencode/opencode.jsonc" "$HOME/.local/share/opencode/auth.json")
if [ -n "${CADRE_KRITIK_DOSYALAR:-}" ]; then IFS=: read -r -a KRITIK <<< "$CADRE_KRITIK_DOSYALAR"; fi
ETIKET="${2:?etiket}"
# N-ENV02 (audit C-15/R-23): the backup is kept outside the data root (the area the sandbox can read), in a 0700 root;
# old backups left by an interrupted run are cleaned at the next signing.
YEDEK_KOK="${CADRE_IMZA_YEDEK_KOKU:-${TMPDIR:-/tmp}/cadre-imza-$(id -u)}"
IMZA="$QE/zincir/.imza-$ETIKET.txt"; YEDEK="$YEDEK_KOK/$ETIKET.yedek"
case "${1:-}" in
  imza)
    mkdir -p "$YEDEK_KOK"; chmod 700 "$YEDEK_KOK"
    find "$YEDEK_KOK" -mindepth 1 -maxdepth 1 -name '*.yedek' -mmin +1440 -exec rm -rf {} + 2>/dev/null
    mkdir -p "$YEDEK"; chmod 700 "$YEDEK"; : > "$IMZA"
    N=0
    for f in "${KRITIK[@]}"; do
      [ -f "$f" ] || continue
      h="$(shasum -a 256 "$f" | cut -d" " -f1)"
      printf '%s  %s\n' "$h" "$f" >> "$IMZA"
      cp -p "$f" "$YEDEK/$h"
      N=$((N+1))
    done
    # B110: the manifest sits where the protected agent CAN WRITE; a deleted manifest passed with "WARNING" (rc=2),
    # an emptied manifest passed SILENTLY (rc=0). The line count is written into the manifest itself:
    # a missing/corrupted manifest is now a VIOLATION.
    printf '#SATIR %s\n' "$N" >> "$IMZA"
    cat "$IMZA" ;;
  dogrula)
    # A file in a writable area can be forged: the root of trust is the signing output kept by the caller (the engine); no verification without stdin.
    [ "${3:-}" = "-" ] || { echo "IHLAL: imza manifesti stdin'den verilmedi ($ETIKET) — dosyadaki manifest guvenilmez"; exit 1; }
    # B305: putting stdin back into a writable file would bring back the concurrent forged-manifest race.
    MANIFEST="$(cat)" || { echo "IHLAL: imza manifesti okunamadi ($ETIKET)"; exit 1; }
    BEKLENEN="$(awk '$1=="#SATIR"{print $2}' <<< "$MANIFEST" | tail -1)"
    GORULEN="$(grep -cv '^#' <<< "$MANIFEST" | head -1)"; GORULEN="${GORULEN:-0}"
    if [ -z "$BEKLENEN" ] || [ "$GORULEN" != "$BEKLENEN" ]; then
      echo "IHLAL: imza manifesti bozulmus ($ETIKET): beklenen=${BEKLENEN:-yok} gorulen=$GORULEN"
      exit 1
    fi
    BOZUK=0
    while read -r h f; do
      [ -n "$h" ] || continue
      case "$h" in '#'*) continue ;; esac
      if [ ! -f "$f" ]; then BOZUK=1; echo "IHLAL: kosu kritik dosyayi SILDI: $f"
      elif [ "$(shasum -a 256 "$f" | cut -d" " -f1)" != "$h" ]; then BOZUK=1; echo "IHLAL: kosu kritik dosyayi degistirdi: $f"
      else continue; fi
      if [ ! -f "$YEDEK/$h" ]; then echo "GERI ALINAMADI (yedek yok): $f"
      elif [ "$(shasum -a 256 "$YEDEK/$h" | cut -d" " -f1)" != "$h" ]; then echo "GERI ALINAMADI (yedek bozuk): $f"
      else mkdir -p "$(dirname "$f")" && cp -p "$YEDEK/$h" "$f" && echo "GERI ALINDI: $f"; fi
    done <<< "$MANIFEST"
    rm -f "$IMZA" "$YEDEK"/* 2>/dev/null; rmdir "$YEDEK" 2>/dev/null
    exit "$BOZUK" ;;
  *) echo "kullanim: kisitli-kabuk.sh imza|dogrula <etiket>" >&2; exit 2 ;;
esac
