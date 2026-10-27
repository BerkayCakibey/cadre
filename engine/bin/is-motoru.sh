#!/usr/bin/env bash
# is-motoru.sh — THIN SHELL: all logic lives in lib/qe_core.py.
# this move closed the bash 3.2 trap class (quotes/heredoc/signals) at the root.
# The old bash version was kept outside this tree for emergency rollback.
set -uo pipefail
CADRE_ENGINE_ROOT="${CADRE_ENGINE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
export CADRE_ENGINE_ROOT
if [ -n "${CADRE_GOREV_ID:-}" ]; then
  exec python3 "$CADRE_ENGINE_ROOT/lib/qe_core.py" serit "$@"
fi
exec bash "$CADRE_ENGINE_ROOT/bin/cadre-dispatch.sh" "$@"
