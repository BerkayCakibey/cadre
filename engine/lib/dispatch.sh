# dispatch.sh — dispatch helpers called from inside the engine (V1 §5).
# Active only when CADRE_GOREV_ID is set.

# 30 min recalculation: cut if the running task's provider budget is ZERO (authority is in the lane).
# Usage: qe_dispatch_recheck <project> <task_id> <model> <run_pid>
qe_dispatch_recheck() {
  # GO1/CANLI1: the provider gate denies the new request; a second guard does not kill the running process.
  return 0
}
