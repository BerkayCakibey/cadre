#!/usr/bin/env bash
# restricted-shell.sh — S2/C23: protection of budget/config files during a cloud-worker run.
# sign: SHA-256 of the critical files + a content-addressed backup. verify: a changed OR DELETED file is a VIOLATION ->
# only that file comes back from its copy at signing time; rc=1 (the engine processes it, success is not recorded).
# Usage: restricted-shell.sh signature|verify <label> [-]  (CADRE_CRITICAL_FILES=a:b:c changes the list in tests)
# B288: sign prints the manifest to stdout; `verify <label> -` takes the manifest ONLY from stdin (the engine's memory).
set -uo pipefail
QE="${CADRE_HOME:-$HOME/.local/share/cadre-engineering}"
CRITICAL=("$HOME/.config/opencode/opencode.jsonc" "$HOME/.local/share/opencode/auth.json")
if [ -n "${CADRE_CRITICAL_FILES:-}" ]; then IFS=: read -r -a CRITICAL <<< "$CADRE_CRITICAL_FILES"; fi
LABEL="${2:?label}"
# N-ENV02 (audit C-15/R-23): the backup is kept outside the data root (the area the sandbox can read), in a 0700 root;
# old backups left by an interrupted run are cleaned at the next signing.
RESERVE_ROOT="${CADRE_SIGNATURE_BACKUP_ROOT:-${TMPDIR:-/tmp}/cadre-signature-$(id -u)}"
SIGNATURE="$QE/chain/.signature-$LABEL.txt"; RESERVE="$RESERVE_ROOT/$LABEL.reserve"
case "${1:-}" in
  signature)
    mkdir -p "$RESERVE_ROOT"; chmod 700 "$RESERVE_ROOT"
    find "$RESERVE_ROOT" -mindepth 1 -maxdepth 1 -name '*.reserve' -mmin +1440 -exec rm -rf {} + 2>/dev/null
    mkdir -p "$RESERVE"; chmod 700 "$RESERVE"; : > "$SIGNATURE"
    N=0
    for f in "${CRITICAL[@]}"; do
      [ -f "$f" ] || continue
      h="$(shasum -a 256 "$f" | cut -d" " -f1)"
      printf '%s  %s\n' "$h" "$f" >> "$SIGNATURE"
      cp -p "$f" "$RESERVE/$h"
      N=$((N+1))
    done
    # B110: the manifest sits where the protected agent CAN WRITE; a deleted manifest passed with "WARNING" (rc=2),
    # an emptied manifest passed SILENTLY (rc=0). The line count is written into the manifest itself:
    # a missing/corrupted manifest is now a VIOLATION.
    printf '#LINE %s\n' "$N" >> "$SIGNATURE"
    cat "$SIGNATURE" ;;
  verify)
    # A file in a writable area can be forged: the root of trust is the signing output kept by the caller (the engine); no verification without stdin.
    [ "${3:-}" = "-" ] || { echo "VIOLATION: the signature manifest was not given on stdin ($LABEL) — the manifest in the file cannot be trusted"; exit 1; }
    # B305: putting stdin back into a writable file would bring back the concurrent forged-manifest race.
    MANIFEST="$(cat)" || { echo "VIOLATION: the signature manifest is unreadable ($LABEL)"; exit 1; }
    EXPECTED="$(awk '$1=="#LINE"{print $2}' <<< "$MANIFEST" | tail -1)"
    SEEN="$(grep -cv '^#' <<< "$MANIFEST" | head -1)"; SEEN="${SEEN:-0}"
    if [ -z "$EXPECTED" ] || [ "$SEEN" != "$EXPECTED" ]; then
      echo "VIOLATION: the signature manifest is corrupted ($LABEL): expected=${EXPECTED:-none} seen=$SEEN"
      exit 1
    fi
    BROKEN=0
    while read -r h f; do
      [ -n "$h" ] || continue
      case "$h" in '#'*) continue ;; esac
      if [ ! -f "$f" ]; then BROKEN=1; echo "VIOLATION: the run DELETED a critical file: $f"
      elif [ "$(shasum -a 256 "$f" | cut -d" " -f1)" != "$h" ]; then BROKEN=1; echo "VIOLATION: the run changed a critical file: $f"
      else continue; fi
      if [ ! -f "$RESERVE/$h" ]; then echo "COULD NOT RESTORE (no backup): $f"
      elif [ "$(shasum -a 256 "$RESERVE/$h" | cut -d" " -f1)" != "$h" ]; then echo "COULD NOT RESTORE (backup corrupted): $f"
      else mkdir -p "$(dirname "$f")" && cp -p "$RESERVE/$h" "$f" && echo "RESTORED: $f"; fi
    done <<< "$MANIFEST"
    rm -f "$SIGNATURE" "$RESERVE"/* 2>/dev/null; rmdir "$RESERVE" 2>/dev/null
    exit "$BROKEN" ;;
  *) echo "usage: restricted-shell.sh signature|verify <label>" >&2; exit 2 ;;
esac
