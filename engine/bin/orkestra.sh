#!/usr/bin/env bash
# orkestra.sh — the `cadre` task path (stage 10): first the DISPATCHER (plan -> task/model assignment -> lanes -> merge -> QA -> quality),
# if the dispatcher returns 3 (pre-filter SKIP / no plan / no worktree) the PLAIN ENGINE (the single-lane path). CADRE_DISPATCH=0 forces the plain path.
#   orkestra.sh <project> <prompt-file>
set -uo pipefail
CADRE_ENGINE_ROOT="${CADRE_ENGINE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
export CADRE_ENGINE_ROOT
exec python3 "$CADRE_ENGINE_ROOT/lib/kosu.py" execute "$@"
