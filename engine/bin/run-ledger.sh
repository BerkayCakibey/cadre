#!/usr/bin/env bash
# run-ledger.sh — dumps the job's BRAIN to disk (opencode DB + engine log + git + chain + config snapshot).
#   run-ledger.sh <project> <worktree|latest> [engine-log] [prompt-file]   single job
#   run-ledger.sh scan                                                     zero-loss scan (all current sessions)
# The engine calls it automatically at the end of a job; the health watch (10 min) and the daily rotation run `scan`. Read-only.
# P1: code root from its own directory (CADRE_ENGINE_ROOT), no fixed path to the live home.
set -uo pipefail
CADRE_ENGINE_ROOT="${CADRE_ENGINE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"; export CADRE_ENGINE_ROOT
. "$CADRE_ENGINE_ROOT/lib/common.sh"
[ "${1:-}" = "scan" ] && exec python3 "$CADRE_ENGINE_ROOT/lib/run_ledger.py" scan
project_ref="${1:?project_ref|scan}"; wt="${2:?worktree|latest}"
[ "$wt" = "latest" ] && wt="$(qe_latest_worktree "$project_ref")"
[ -n "$wt" ] || qe_die "worktree not found"
exec python3 "$CADRE_ENGINE_ROOT/lib/run_ledger.py" "$project_ref" "$wt" "${3:-}" "${4:-}"
