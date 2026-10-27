#!/usr/bin/env bash
# kosu-defteri.sh — dumps the job's BRAIN to disk (opencode DB + engine log + git + chain + config snapshot).
#   kosu-defteri.sh <project> <worktree|latest> [engine-log] [prompt-file]   single job
#   kosu-defteri.sh tara                                                     zero-loss scan (all current sessions)
# The engine calls it automatically at the end of a job; the health watch (10 min) and the daily rotation run `tara`. Read-only.
# P1: code root from its own directory (CADRE_ENGINE_ROOT), no fixed path to the live home.
set -uo pipefail
CADRE_ENGINE_ROOT="${CADRE_ENGINE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"; export CADRE_ENGINE_ROOT
. "$CADRE_ENGINE_ROOT/lib/common.sh"
[ "${1:-}" = "tara" ] && exec python3 "$CADRE_ENGINE_ROOT/lib/kosu_defteri.py" tara
proje="${1:?proje|tara}"; wt="${2:?worktree|latest}"
[ "$wt" = "latest" ] && wt="$(qe_latest_worktree "$proje")"
[ -n "$wt" ] || qe_die "worktree bulunamadi"
exec python3 "$CADRE_ENGINE_ROOT/lib/kosu_defteri.py" "$proje" "$wt" "${3:-}" "${4:-}"
