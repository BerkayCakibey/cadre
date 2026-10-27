#!/usr/bin/env bash
# Keep the repo and the live system in step. The live locations are the ones the
# tools actually read; this repo is the versioned copy of them.
#   ./sync.sh pull   live -> repo   (before committing)
#   ./sync.sh push   repo -> live   (after pulling someone else's change)
#   ./sync.sh fark   read-only comparison (repo == live?)
#   ./sync.sh adopt [token]   adopt a live file without a manifest in two steps (does not touch the file)
# The API key is never written to the repo: config/opencode.jsonc keeps a
# placeholder, and `push` refuses to overwrite a live file that has a real key.
set -euo pipefail
R="$(cd "$(dirname "$0")" && pwd)"
QE="${CADRE_HOME:-$HOME/.local/share/cadre-engineering}"          # tests may pass a temporary home directory
CFG="${OPENCODE_CFG:-$HOME/.config/opencode}"
BIN_DIR="${CADRE_BIN_DIR:-$HOME/.local/bin}"
KEYLINE='"apiKey": "REDACTED-SET-LOCALLY"'

pull() {
  # 12.09 night RCA: pull silently overwrote UNCOMMITTED engine/config edits in the repo twice (the E10 test ran pull).
  # Now it stops on a dirty working tree; `pull --zorla` to overwrite deliberately. `fark` for a read-only comparison.
  if [ "${1:-}" != "--zorla" ] && [ -n "$(git -C "$R" status --short -- engine config 2>/dev/null)" ]; then
    echo "pull DURDU: repoda commit'lenmemiş engine/config değişikliği var (ezilirdi). Önce commit et ya da: ./sync.sh pull --zorla" >&2
    git -C "$R" status --short -- engine config >&2; return 2
  fi
  rsync -a --delete --exclude __pycache__ "$QE/lib/" "$R/engine/lib/"
  rsync -a --delete "$QE/hooks/" "$R/engine/hooks/"
  rsync -a --delete --exclude __pycache__ "$QE/tests/" "$R/engine/tests/"
  mkdir -p "$R/engine/bin"; rsync -a --delete "$QE/bin/" "$R/engine/bin/"
  cp "$QE/USAGE.md" "$R/engine/USAGE.md" 2>/dev/null || true
  rsync -a --delete "$CFG/agents/" "$R/config/agents/"
  rsync -a --delete "$CFG/skills/" "$R/config/skills/"
  rsync -a --delete "$CFG/command/" "$R/config/command/" 2>/dev/null || true
  cp "$CFG/AGENTS.md" "$R/config/AGENTS.md"
  # config with the key stripped
  sed -E 's/"apiKey"[[:space:]]*:[[:space:]]*"[^"]*"/'"$KEYLINE"'/' \
    "$CFG/opencode.jsonc" > "$R/config/opencode.jsonc"
  echo "pull tamam — repo canlı sistemle eşitlendi (anahtar ayıklandı)"
}

push() {
  if [ -n "${1:-}" ]; then
    python3 "$R/engine/lib/runtime_sync.py" push "$1"
  else
    python3 "$R/engine/lib/runtime_sync.py" push
  fi

}

fark() {
  python3 "$R/engine/lib/runtime_sync.py" fark

}

case "${1:-}" in
  pull) pull "${2:-}" ;;
  fark) fark ;;
  push) push "${2:-}" ;;
  plan) python3 "$R/engine/lib/runtime_sync.py" plan ;;
  adopt) if [ -n "${2:-}" ]; then python3 "$R/engine/lib/runtime_sync.py" adopt "$2"; else python3 "$R/engine/lib/runtime_sync.py" adopt; fi ;;
  *) echo "kullanım: ./sync.sh pull [--zorla] | push | plan | fark | adopt [belirteç]" >&2; exit 1 ;;
esac
