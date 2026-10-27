#!/usr/bin/env bash
# cadre-dispatch.sh — DISPATCHER V1 coordinator
#   cadre-dispatch.sh <project> <prompt-file>
# Exit: 0 = job DONE via dispatch, 1 = job PARTIAL/cancelled via dispatch, 3 = DISPATCH SKIPPED (fall back)
# Default OFF: returns 3 immediately unless CADRE_DISPATCH=1. Single writer: only this process writes the state JSON files.
set -uo pipefail
CADRE_ENGINE_ROOT="${CADRE_ENGINE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
export CADRE_ENGINE_ROOT
. "$CADRE_ENGINE_ROOT/lib/common.sh"
REAL="${CADRE_REAL_OPENCODE:-/opt/homebrew/bin/opencode}"
[ -x "$REAL" ] || REAL="$(PATH=/opt/homebrew/bin:/usr/local/bin command -v opencode)"
project_ref="${1:?project_ref}"; pf="${2:?prompt file}"
[ -n "${CADRE_RUN_ID:-}" ] || exec bash "$CADRE_ENGINE_ROOT/bin/orchestrator.sh" "$project_ref" "$pf"
if [ "${CADRE_SINGLE_LANE:-0}" = 1 ]; then
  exec python3 "$CADRE_ENGINE_ROOT/lib/qe_core.py" lane "$project_ref" "$pf"
fi
[ "${CADRE_DISPATCH:-1}" = "1" ] || [ "${CADRE_CORRECTION_ONLY:-0}" = 1 ] || exit 3   # B274: the correction loop is part of acceptance
[ -s "$pf" ] || exit 3
Z="$CADRE_RUN_DIR"; CAL="$Z/tasks"; mkdir -p "$CAL"
DLOG="$Z/dispatch.log"
dlog(){ printf '%s %s\n' "$(date '+%F %T')" "$*" | tee -a "$DLOG" >&2; }
task_dir(){ python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" task-path "$CADRE_RUN_ID" "$1"; }
task_state(){ python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" task-state "$CADRE_RUN_ID" "$1"; }
# Shared functions must be defined even when the run continues with a recorded plan (the plan branch is skipped).
cloud_wait(){ # design 3.3: $1=role $2=class $3=score; on hard+ work without a strong cloud, waits if the share/window renews within <=2 h (once per dispatcher)
  [ -z "${CLOUD_WAITED:-}" ] || return 1
  local sec s; sec="$(python3 "$CADRE_ENGINE_ROOT/lib/model_router.py" wait "$1" "$2" "${3:-0}" 2>>"$DLOG")"
  [ "${sec:-0}" -gt 0 ] 2>/dev/null || return 1
  CLOUD_WAITED=1
  dlog "BUDGET WAIT ($1): hard+ job, no strong cloud model within budget; waiting $(( (sec + 59) / 60 )) min for share/window renewal"
  while [ "$sec" -gt 0 ]; do
    [ -f "$CADRE_RUN_DIR/cancel.flag" ] && return 1
    s=$(( sec > 300 ? 300 : sec )); sleep "$s"; sec=$(( sec - s ))
    [ "$sec" -gt 0 ] && dlog "BUDGET WAIT: $(( (sec + 59) / 60 )) min remaining"
  done
  dlog "BUDGET WAIT finished ($1)"
}
planner_call(){ # $1=model $2=prompt $3=task id $4=sandbox $5=difficulty [$6=deadline role: plan|split, limits.json] -> raw output
  CADRE_TASK_ID="$3" RB="$REAL" DM="$1" PP="$2" PLAN_DIFFICULTY="$5" DURATION_ROLE="${6:-plan}" python3 -c "
import os,sys
sys.path.insert(0, os.environ['CADRE_ENGINE_ROOT'] + '/lib')
from run_store import RunStore, capture_source_plan
from limits import deadline
try:
    print(capture_source_plan(RunStore(), os.environ['CADRE_RUN_ID'], os.environ['RB'], os.environ['DM'],
          os.environ['PP'], os.environ['CADRE_TASK_ID'], deadline(os.environ['DURATION_ROLE']), int(os.environ['PLAN_DIFFICULTY'])))
except (ValueError,PermissionError) as e:
    print('ERROR',e,file=sys.stderr); sys.exit(78)
except Exception as e: print('ERROR',e,file=sys.stderr)
" 2>>"$DLOG"
}

extra_lane_execute(){ # $1=lane id $2=model(empty=the router chooses) $3=prompt file -> produced|half|cancel; no helper, the same scope check (G-C15)
  local sid="$1" once="$Z/.lane-once-$1.json"
  python3 "$CADRE_ENGINE_ROOT/lib/qe_core.py" lane-status "$WT" > "$once" 2>>"$DLOG" || : > "$once"
  env CADRE_WORKTREE="$WT" CADRE_TASK_ID="$sid" CADRE_RUN_MODEL="$2" CADRE_SUB_AGENTLESS=1 \
      bash "$CADRE_ENGINE_ROOT/bin/job-runner.sh" "$project_ref" "$3" >>"$DLOG" 2>&1
  [ -s "$once" ] && python3 "$CADRE_ENGINE_ROOT/lib/qe_core.py" scope "$WT" "$Z/plan.json" "$sid" "$once" "$Z/scope-outside" 2>>"$DLOG" | while IFS= read -r s; do dlog "$s"; done
  local d; d="$(task_state "$sid")"; echo "${d:-half}"
}
acceptance(){ env CADRE_WORKTREE="$WT" CADRE_TASK_ID=quality CADRE_REAL_OPENCODE="$REAL" \
    python3 "$CADRE_ENGINE_ROOT/lib/qe_core.py" finish "$project_ref" "$WT" "$KM" >>"$DLOG" 2>&1; }
correction_execute(){ # G-C16: $1=round $2=model(empty=the router chooses) -> produced|half|cancel; findings in the prompt, no helper, the same scope check
  local gid="correction-$1" GP
  GP="$(task_dir "$gid")/prompt.md" || { echo half; return; }
  python3 "$CADRE_ENGINE_ROOT/lib/qe_core.py" correction-prompt "$CADRE_RUN_ID" "$1" "$PLAIN_PARENT" > "$GP" 2>>"$DLOG" || { echo half; return; }
  extra_lane_execute "$gid" "$2" "$GP"
}
correction_loop(){ # G-C16: acceptance + limited correction rounds; $1=skip -> the first acceptance already failed (B274: the plain engine path)
  local first="${1:-}" decision dkind dsig dmodel drole dresult n_commit
  PLAIN_PARENT="${LEVEL_ROUND:-2}"   # G-C16 round allowance comes from the level (G-E9); 2 when there is no level
  while :; do
    if [ "$first" = skip ]; then first=""; else acceptance && break; fi
    [ -f "$Z/cancel.flag" ] && { dlog "JOB CANCELLED (flag)"; return 130; }
    PLAIN_AGAIN=0; case "${LEVEL_NAME:-}" in hard|very-hard|ultra) PLAIN_AGAIN=1;; esac   # hard+: if it does not converge, one clean restart
    decision="$(python3 "$CADRE_ENGINE_ROOT/lib/qe_core.py" correction-decision "$CADRE_RUN_ID" "$PLAIN_PARENT" "$PLAIN_AGAIN" 2>>"$DLOG")"
    if [ "${decision%% *}" != "PROCEED" ]; then
      dlog "CORRECTION ABSENT: ${decision#* }"; dlog "DISPATCH IS HALF (QA/acceptance/quality did not pass). worktree=$WT"
      printf '%s\n' "$WT"; return 1
    fi
    read -r _ dkind dsig <<<"$decision"
    drole=impl
    if [ -n "$REPORT_MODE" ]; then drole=plan   # G-R6: report correction also writes interpretation/results -> strong model
    elif [ "$dkind" = again ]; then drole=recovery; else drole=impl; fi   # every round in the cloud (P3); clean restart with the fallback chain
    if [ -n "$drole" ]; then
      CADRE_TASK_ID="correction-$dkind" cloud_wait "$drole" middle "${LEVEL_POINTS:-0}" || true   # B82 hand-off: the task id opens the acceptance pocket
      dmodel="$(CADRE_TASK_ID="correction-$dkind" python3 "$CADRE_ENGINE_ROOT/lib/model_router.py" first "$drole" middle 30000 2>/dev/null)"
    fi
    if [ -z "$dmodel" ]; then
      dlog "NO CORRECTION: waiting for cloud capacity"; dlog "DISPATCH IS HALF (QA/acceptance/quality did not pass). worktree=$WT"
      printf '%s\n' "$WT"; return 1
    fi
    dlog "CORRECTION ROUND $dkind/$PLAIN_PARENT ($dsig) model=$dmodel"
    dresult="$(correction_execute "$dkind" "$dmodel")"
    n_commit="$(qe_commit_wip "$WT" "$project_ref" "correction round $dkind" 2>>"$DLOG")"; dlog "correction $dkind: $dresult, commit ${n_commit:-0} file"
  done
  dlog "DISPATCH IS COMPLETE (QA/acceptance/quality passed). worktree=$WT"
  printf '%s\n' "$WT"; return 0
}
# B274: if the plain engine (no plan / pre-filter skip / no budget) fell with a fixable FAIL, the coordinator
# calls the dispatcher only for this loop; no plan and no lanes, same G-C16 rules.
if [ "${CADRE_CORRECTION_ONLY:-0}" = 1 ]; then
  WT="${CADRE_WORKTREE:?worktree}"; KM=""; REPORT_MODE=""
  dlog "PLAIN ENGINE ACCEPTANCE FAILED -> G-C16 correction loop (B274) worktree=$WT"
  correction_loop "$([ "${CADRE_CORRECTION_SKIP:-0}" = 1 ] && echo skip)"; exit $?
fi
if [ -f "$Z/plan.json" ]; then
  plan_json="$(cat "$Z/plan.json")" || exit 1
  dlog "proceed with recorded plan: $CADRE_RUN_ID"
else

BUDGET_JSON="$(python3 "$CADRE_ENGINE_ROOT/lib/provider_budget.py" 2>/dev/null)"
[ -n "$BUDGET_JSON" ] || { dlog "budget could not be calculated -> skip"; exit 3; }

# --- PRE-FILTER (§2): size + budget rule; a free LLM (groq) may raise the class, never lower it (prefilter.py) ---
repo="$(qe_repo_path "$project_ref")" || exit 3
read -r PREFILTER PREFILTER_PREM <<EOF2
$(printf '%s' "$BUDGET_JSON" | python3 "$CADRE_ENGINE_ROOT/lib/prefilter.py" "$pf" "$repo")
EOF2
[ "$PREFILTER" = "PASS" ] || { dlog "on-sieve: SKIP (size/budget) -> rule-based"; exit 3; }
if [ "$PREFILTER_PREM" = "PREMIUM" ]; then   # B199: a hard job is planned within its own cap (the Claude plan and role shares see this cap)
  read -r OLD_CAP FRESH_CAP <<<"$(python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" hard-cap "$CADRE_RUN_ID" "$project_ref" 2>>"$DLOG")"
  [ "${OLD_CAP:-x}" = "${FRESH_CAP:-x}" ] || dlog "hard job: run cap \$${OLD_CAP} -> \$${FRESH_CAP}"
fi

# --- PLAN GENERATION (§2): pick the dispatcher model, run it in an empty sandbox ---
GO_RATIO=$(printf '%s' "$BUDGET_JSON" | python3 -c "import json,sys;print(json.load(sys.stdin)['go']['ratio'])")
CLAUDE_PAY=$(printf '%s' "$BUDGET_JSON" | python3 -c "import json,sys;c=json.load(sys.stdin).get('claude',{});print(f\"%{c.get('pay',0)} (~\${c.get('available_usd',0)})\")")   # C31: remaining share of the weekly reserve; no daily dollar cap
WINDOW=$(printf '%s' "$BUDGET_JSON" | python3 -c "import json,sys;print(json.load(sys.stdin)['go']['window_open'])")
# PLAN MODEL: ROUTER (stage 10) — plan chain (if there is a Claude share -> Sol -> Go -> Zen)
CATEGORY="middle"; [ "$PREFILTER_PREM" = "PREMIUM" ] && CATEGORY="hard"
SCOPE_RULE="$(python3 "$CADRE_ENGINE_ROOT/lib/level.py" rule "$pf" 2>>"$DLOG")"   # G-E9: plan limit from the max(rule, planner) level
DMODEL="$(python3 "$CADRE_ENGINE_ROOT/lib/model_router.py" first plan "$CATEGORY" 30000 2>/dev/null)"
if cloud_wait plan "$CATEGORY" "$SCOPE_RULE"; then DMODEL="$(python3 "$CADRE_ENGINE_ROOT/lib/model_router.py" first plan "$CATEGORY" 30000 2>/dev/null)"; fi
case "$DMODEL" in claude/*) DMODEL="${DMODEL%%#*}";; esac
[ -n "$DMODEL" ] || { dlog "no cloud model within planning budget -> fallback while waiting for cloud capacity"; exit 3; }   # S5: no planning with Go when there is no budget
dlog "plan model (router): $DMODEL"

MAP_ALL=$(cd "$repo" && find . -maxdepth 2 -type f \( -name '*.py' -o -name '*.sh' -o -name '*.md' -o -name 'Makefile' -o -name '*.toml' \) 2>/dev/null | grep -v '/\.git/')
MAP_TOTAL=$(printf '%s\n' "$MAP_ALL" | grep -c . || true)
MAP=$(printf '%s\n' "$MAP_ALL" | head -60)
MAP_NOT="first 60 / $MAP_TOTAL files total"
if [ "${MAP_TOTAL:-0}" -gt 60 ]; then MAP_NOT="$MAP_NOT; TRIMMED, read remaining files with read-only tools"; fi
dlog "repo map: $MAP_NOT"
SANDBOX="$(task_dir plan)/sandbox" || exit 1
mkdir -p "$SANDBOX" || exit 1
PLAN_PROMPT="YOU ARE A TASK PLANNER. Inspect the real source files with read-only tools; output a single JSON block.
Split the following job into 1-8 independent tasks according to its scope. Rules:
- SCOPE SCORE (required, G-E9): score the ENTIRE REQUEST from 0 to 100, rather than just your plan: 0-15 <15 min single file/line; 16-40 15-60 min function+test; 41-65 1-4 h feature across several files; 66-85 >4 h module/refactor; 86-100 days: a project from scratch or a backlog with too many items for 8 tasks. Do not narrow or expand scope on your own; record routine decisions as justified assumptions, and report critical uncertainty about purpose/authority/cap for an operator decision. Score 86+ if the request does not fit in 8 tasks; the system divides it into features in project mode. Task count must match the score (simple 1, middle at most 3, hard at most 5, very hard at most 8); scope_rationale is one sentence.
- 'files' contains ONLY paths (no descriptions/parentheses; use the directory for a new file whose name is not yet known).
- State file overlaps explicitly; put prerequisite task ids in dependent. The coordinator runs independent cloud tasks CONCURRENTLY when dependent is EMPTY and files are disjoint; it runs the rest serially.
- SELECTION RULE: model first, then an available provider route. Code-writing roles use deepseek-v4-pro › glm-5.3 › qwen3.8-max (Go first, then OpenRouter). No free models or subagents.
- CLAUDE is used only in the plan chain under the existing quota rule (share: $CLAUDE_PAY).
- candidates: ds-pro=code-writing model; auto=the router chooses by difficulty/budget. No local models; every task runs in the cloud.
- parallel_group: 0..100 (03.10 B340: this field does NOT determine concurrency; the condition is 'empty dependent + disjoint files'). Split tasks that touch the same file into disjoint files, or add a dependency.
- DIFFICULTY->MODEL: code tasks use ds-pro; difficulty 1-2 uses the cheap chain. Wait for budget if capacity is unavailable.
- SHARED DOCUMENTS (BACKLOG.md, README) are updated only in the FINAL task; other tasks must not touch them. A task that adds a NEW engine file must add its row to the code map in docs/CONTRACT.md itself (the contract guard test requires it). Do NOT add dependencies between independent code tasks with disjoint files (empty dependent permits parallel execution; 03.10 B340).
- files must list FILES rather than DIRECTORIES (engine/lib/provider_budget.py rather than 'engine'): broad directory lists make every task appear to overlap and prevent concurrency.
- Do NOT add merge/review except in the final task (the coordinator adds it).
- Do NOT add a verification/QA-only task (such as running make check/test): the coordinator's QA gate and repair round handle it.
- PLAN STANDARD (REQUIRED for every task): acceptance=measurable criterion (which passing test/output establishes acceptance); scope_outside=what must remain untouched; dependent=prerequisite task ids (empty if none). The executing agent sees ONLY the plan; write precise descriptions.
JSON schema (OUTPUT NOTHING ELSE):
{\"job_id\":\"$(basename "$pf" .md)\",\"scope_points\":30,\"scope_rationale\":\"...\",\"tasks\":[{\"id\":\"g1\",\"definition\":\"...\",\"files\":[\"path\"],\"acceptance\":\"measurable acceptance criterion (test/output)\",\"scope_outside\":[\"untouched scope\"],\"dependent\":[],\"difficulty\":3,\"model\":\"ds-pro\",\"parallel_group\":1,\"estimate_min\":15,\"estimate_usd\":0.02}],\"merge_order\":[\"g1\"],\"quality\":{\"model\":\"glm-5.2|ds-pro|oz\",\"scope\":\"...\"}}

HISTORICAL COST (ledger: actual average per task in previous runs; keep estimate_usd for paid tasks close to this average, never zero):
$(python3 "$CADRE_ENGINE_ROOT/lib/estimate_vs_actual.py" expense 2>/dev/null || echo 'no measurements yet')

REPO MAP ($MAP_NOT):
$MAP

JOB:
$(cat "$pf")"
REPORT_EXTRA="$(python3 "$CADRE_ENGINE_ROOT/lib/report_mode.py" plan-prompt 2>>"$DLOG")"
[ -z "$REPORT_EXTRA" ] || PLAN_PROMPT="$PLAN_PROMPT

$REPORT_EXTRA"
ACCEPTANCE_QA="$(python3 -c "
import json,os
try: print(((json.load(open(os.environ['CADRE_RUN_DIR']+'/run.json')).get('feature_acceptance') or {}).get('qa_command') or '').strip())
except Exception: pass" 2>/dev/null)"
[ -z "$ACCEPTANCE_QA" ] || PLAN_PROMPT="$PLAN_PROMPT

FEATURE ACCEPTANCE COMMAND (the QA gate runs ONLY this command): $ACCEPTANCE_QA
Write new tests in the test files this command runs, and add those files to the task's 'files' list; tests elsewhere do not establish acceptance."

plan_json=""
RED_ONCE=$(python3 -c "
import json,os
try: print(len(json.load(open(os.environ['CADRE_RUN_DIR']+'/run.json')).get('denials') or []))
except Exception: print(0)" 2>/dev/null || echo 0)
# B171: only attempts where the model was called count (a budget denial is refunded); the round cap stops a chain cycling between denying links.
python3 "$CADRE_ENGINE_ROOT/lib/qe_core.py" plan-assurance >>"$DLOG" 2>/dev/null || true   # 4b: plan reserve on Zen/OR (non-blocking)
attempt=0; round=0
while [ "$attempt" -lt 3 ] && [ "$round" -lt 10 ]; do
  round=$((round + 1)); attempt=$((attempt + 1))
  dlog "plan attempt $attempt (round $round), dispatcher=$DMODEL"
  DIFFICULTY=3; [ "$CATEGORY" = "hard" ] && DIFFICULTY=4   # C31: the policy derives effort from difficulty (max only at difficulty 5)
  [ "${DMODEL#claude/}" = "$DMODEL" ] || dlog "claude-plan route (request: $DMODEL, difficulty=$DIFFICULTY; effort from policy)"
  RAW=$(planner_call "$DMODEL" "$PLAN_PROMPT" plan "$SANDBOX" "$DIFFICULTY") || exit $?
  SCHEMA_FILE="${DLOG%/*}/.plan-schema-error"
  plan_json=$(printf '%s' "$RAW" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" extract ${SCOPE_RULE:+"$SCOPE_RULE"} 2>"$SCHEMA_FILE") || plan_json=""
  # B126: on a budget denial the model is not called, output comes back EMPTY and `extract` counts it as a "schema violation";
  # the correction branch went back to the SAME model and retried in vain. Signal: denial text in RAW + growth of `denials[]`.
  RED_THEN=$(python3 -c "
import json,os,sys
try: print(len(json.load(open(os.environ['CADRE_RUN_DIR']+'/run.json')).get('denials') or []))
except Exception: print(0)" 2>/dev/null || echo 0)
  BUDGET_REJECTION=0
  printf '%s' "$RAW" | grep -q 'CADRE_BUDGET_DENIED\|allocation exhausted\|run budget exhausted\|no verified allocation' && BUDGET_REJECTION=1
  [ "${RED_THEN:-0}" -gt "${RED_ONCE:-0}" ] && BUDGET_REJECTION=1
  RED_ONCE="$RED_THEN"
  [ "$BUDGET_REJECTION" -eq 1 ] || { [ -s "$SCHEMA_FILE" ] && cat "$SCHEMA_FILE" >>"$DLOG"; }   # a denial is not a schema violation; it has its own line below
  SCHEMA_ERROR="$(grep -m1 '^SCHEMA: ' "$SCHEMA_FILE" 2>/dev/null || true)"
  [ -n "$plan_json" ] && break
  [ "$BUDGET_REJECTION" -eq 1 ] || dlog "plan JSON broken/schema-violation (attempt $attempt); RAW last 300: $(printf '%s' "$RAW" | tail -c 300 | tr '\n' ' ')"
  # B95: three plan models got stuck on the SAME schema rule and their plans were thrown away.
  # A schema violation is NOT a provider failure: instead of switching links, the error goes back to the model and the same model gets
  # one correction chance; it is not written to the circuit breaker either.
  if [ "$BUDGET_REJECTION" -eq 0 ] && [ -n "$RAW" ] && [ -n "$SCHEMA_ERROR" ] && [ "${SCHEMA_CORRECTION:-0}" -eq 0 ]; then
    SCHEMA_CORRECTION=1
    dlog "plan ${SCHEMA_ERROR} — schema violation; not counted as a model failure, retry the same model with a correction request"
    PLAN_PROMPT="$PLAN_PROMPT

PREVIOUS ATTEMPT REJECTED: ${SCHEMA_ERROR#SCHEMA: }
Preserve the schema and recorded scope; fix ONLY this error. Record routine decisions as justified assumptions; report critical purpose/authority/cap uncertainty for an operator decision, and do not expand scope on your own.
File paths must be RELATIVE to the worktree root: no absolute paths,
no '..', no globs (*, ?, []), no ':' or ','. Every task needs a nonempty 'files' list."
    continue
  fi
  # B52: a budget/cap denial is NOT a provider FAILURE — not written to the circuit breaker (live run 1: a Claude 403 denial was counted as a failure)
  if [ "$BUDGET_REJECTION" -eq 1 ]; then
    dlog "plan BUDGET REJECTION ($DMODEL) — no schema violation or provider failure; try the next link"
    attempt=$((attempt - 1))
  elif [ -z "$RAW" ]; then
    dlog "plan attempt EMPTY output ($DMODEL) — no schema violation; try the next link"
    python3 "$CADRE_ENGINE_ROOT/lib/model_router.py" failure "$DMODEL" "plan empty output" >/dev/null 2>&1
  else
    python3 "$CADRE_ENGINE_ROOT/lib/model_router.py" failure "$DMODEL" "plan broken/empty" >/dev/null 2>&1
  fi
  SCHEMA_CORRECTION=0   # one more correction chance for the new link
  NXT="$(python3 "$CADRE_ENGINE_ROOT/lib/model_router.py" next plan "$DMODEL" 2>/dev/null)"
  [ -n "$NXT" ] || { dlog "plan chain exhausted -> plain engine"; break; }
  DMODEL="$NXT"
  dlog "next planning link: $DMODEL"
done
[ -n "$plan_json" ] || [ "$round" -lt 10 ] || dlog "plan round cap (10) reached"
# Retain the planner directory: Claude resume is tied to its original cwd.
[ -n "$plan_json" ] || { dlog "no plan produced -> cloud rule-based fallback"; exit 3; }

printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" save-plan "$CADRE_RUN_ID" "$DMODEL" || exit 1
python3 "$CADRE_ENGINE_ROOT/lib/qe_core.py" plan-assurance-leave >>"$DLOG" 2>/dev/null || true
fi
dlog "plan acceptance: $(printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" summary)"
python3 "$CADRE_ENGINE_ROOT/lib/budget_gate.py" --progress plan-acceptance "$(printf '%s' "$plan_json" | shasum -a 256 | cut -c1-64)" 2>>"$DLOG" || true
python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" plan-ready "$CADRE_RUN_ID" >>"$DLOG" 2>&1 || exit 78
REPORT_MODE="$(python3 "$CADRE_ENGINE_ROOT/lib/report_mode.py" mode 2>/dev/null)"   # G-R1: report declaration in the acceptance record
rm -f "$Z/extra-lane.flag"   # B226: if the previous dispatcher died in the middle of G-R7, a stale hand-off marker must not stop it forever
[ -z "$REPORT_MODE" ] || dlog "REPORT MODE (G-R1): only the report and evidence appendix may change; report QA and judge check"
# G-E9: final scope = max(rule floor, planner); the level gives the cap, the correction round and the plan ceiling (before role shares)
read -r LEVEL_NAME LEVEL_POINTS LEVEL_CAP LEVEL_ROUND LEVEL_PLAN <<<"$(python3 "$CADRE_ENGINE_ROOT/lib/level.py" apply "$CADRE_RUN_ID" "$project_ref" 2>>"$DLOG")"
if [ -n "${LEVEL_NAME:-}" ]; then
  dlog "SCOPE: points $LEVEL_POINTS -> level $LEVEL_NAME (cap \$$LEVEL_CAP, correction $LEVEL_ROUND round, plan at most $LEVEL_PLAN tasks)"
  if [ "$LEVEL_NAME" = ultra ] && [ "${CADRE_TYPED_REQUEST:-0}" != 1 ]; then   # G-P1: if the planner also found it large (>=66), the project coordinator instead of plan lanes
    if python3 "$CADRE_ENGINE_ROOT/lib/project_mode.py" suitable "$CADRE_RUN_ID" 2>>"$DLOG"; then
      dlog "SCOPE: ultra — PROJECT MODE (G-P1): feature list and child runs, intermediate branch cadre/project-ref-*"
      python3 "$CADRE_ENGINE_ROOT/lib/project_mode.py" start "$CADRE_RUN_ID" "$project_ref" >>"$DLOG" 2>&1
      exit $?
    fi
    dlog "SCOPE: ultra but the planner did not find the job large (<66) — no project mode, process as very hard"
  fi
fi

GIDS=$(printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" validate) || { dlog "invalid plan dependencies"; exit 1; }

# TASK CONTRACT: the budget split is written AT plan time and does not change during the run.
# Lanes 75% · judge 15% · helper 10%. Written once; a second call returns the existing one.
printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/contract.py" write "$CADRE_RUN_ID" >/dev/null 2>>"$DLOG" \
  && dlog "contract: $(python3 "$CADRE_ENGINE_ROOT/lib/contract.py" summary "$CADRE_RUN_ID" 2>/dev/null)" \
  || dlog "WARNING contract could not be written — fallback shares apply"

python3 "$CADRE_ENGINE_ROOT/lib/qe_core.py" assurance >>"$DLOG" 2>&1 || exit 75

# --- EXECUTION (§4): one worktree, one writer ---
ts="$(date +%Y%m%d-%H%M%S)"
WT="${CADRE_WORKTREE:-}"
if [ -z "$WT" ]; then
  WT="$(qe_make_worktree "$project_ref" "dispatch $ts")" || { dlog "worktree could not be created"; exit 1; }
fi
if ! guard_output="$( (qe_verify_ai_worktree "$WT" "$project_ref") 2>&1 )"; then
  dlog "$guard_output"
  python3 -c 'import json,sys;print(json.dumps({"reason": "worktree-guard: " + sys.argv[1].removeprefix("cadre: ")[:300]}))' "$guard_output" \
    | python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" update "$CADRE_RUN_ID" >/dev/null 2>&1
  exit 1
fi
python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" worktree "$CADRE_RUN_ID" "$WT" || exit 1
printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" path-verify "$WT" || { dlog "plan paths could not be verified"; exit 1; }
dlog "worktree: $WT"
if [ "$(printf '%s' "$plan_json" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("kind", ""))')" = verified-backlog ]; then
  KM=""
  acceptance
  exit $?
fi
model_map(){ python3 "$CADRE_ENGINE_ROOT/lib/model_router.py" short "$1" 2>/dev/null; }   # Stage 10: short name -> model (router)
other_provider(){ # $1=model $2=lane; stage 10: the next healthy link. B52/B300: a lane left partial by a budget denial is not a failure
  python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" budget-rejection "$CADRE_RUN_ID" "${2:-}" "${1%%/*}" >/dev/null 2>&1 \
    || python3 "$CADRE_ENGINE_ROOT/lib/model_router.py" failure "$1" "task half" >/dev/null 2>&1
  python3 "$CADRE_ENGINE_ROOT/lib/model_router.py" alternatives impl "$1" 3 2>/dev/null; }

task_pf(){ # $1=id -> create the prompt file
  local gid="$1" GP; GP="$(task_dir "$1")/prompt.md" || return 1
  [ -s "$GP" ] && { echo "$GP"; return; }
  printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" task_pf "$gid" | python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" write-prompt "$CADRE_RUN_ID" "$gid" || return 1
  echo "$GP"
}

lane_start(){ # $1=id $2=model(empty=the router chooses); puts the pid in LANE_PID (for wait in the same shell)
  local gid="$1" mdl="$2" GP GIDP="$1"
  printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" path-verify "$WT" || return 1
  GP=$(task_pf "$gid") || return 1
  env CADRE_WORKTREE="$WT" CADRE_TASK_ID="$GIDP" CADRE_RUN_MODEL="$mdl" \
      CADRE_SUB_AGENTLESS=1 \
      CADRE_ABORT_SEC="$(printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" abort "$gid")" \
      bash "$CADRE_ENGINE_ROOT/bin/job-runner.sh" "$project_ref" "$GP" >>"$DLOG" 2>&1 &
  LANE_PID=$!
}

# The locked run.json is the authoritative task table.

history_write(){ # id start_model state duration_min; B294: actual_model is an old field (= start), hand-offs come from the attempt records
  local mod; mod="$(python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" task-models "$CADRE_RUN_ID" "$1" 2>>"$DLOG")"
  printf '{"ts":"%s","project_ref":"%s","job_id":"%s","task_id":"%s","actual_model":"%s","start_model":"%s",%s,"status":"%s","duration_min":%s,"reason":"%s"}\n' \
    "$(date +%FT%T)" "$project_ref" "$(printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" job_id)" \
    "$1" "${2:-auto}" "${2:-auto}" "${mod:-\"tried_models\":[]}" "$3" "${4:-0}" "${5:-}" >> "$Z/assignment-history.jsonl"
}

run_single(){ # $1=id $2=model -> wait, print the result (produced/half/cancel)
  local gid="$1" mdl="$2" s0 s1 i worker_rc
  s0=$(date +%s); lane_start "$gid" "$mdl" || { echo half; return; }
  wait "$LANE_PID" 2>/dev/null; worker_rc=$?
  s1=$(date +%s)
  local d n; d="$(task_state "$gid")"; d="${d:-half}"
  [ "$worker_rc" = 0 ] || { [ "$d" = cancel ] || d=half; }
  n=$(awk '{print $2}' "$(task_dir "$gid")/status.txt" 2>/dev/null)
  history_write "$gid" "${mdl:-auto}" "$d" "$(( (s1-s0)/60 ))" "$n"
  echo "$d"
}

task_execute_chained(){ # threshold chain: assigned -> other provider -> failed; $3=report: plan chain (G-R6)
  local gid="$1" mdl="$2" mode="${3:-}" d dfiles
  tool_waiting(){ python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" show "$CADRE_RUN_ID" | jq -e '.tool_boundary_wait // empty' >/dev/null; }
  case "$(python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" task-result "$CADRE_RUN_ID" "$gid")" in
    produced|complete) echo produced; return;;
  esac
  dfiles=$(printf '%s' "$plan_json" | GID="$gid" python3 -c "import json,sys,os;print(','.join([g['files'] for g in json.load(sys.stdin)['tasks'] if g['id']==os.environ['GID']][0]))" 2>/dev/null)
  # P2 + B17: cheap syntax check after EVERY attempt — the old code only validated the first attempt,
  # so a fallback provider's broken output counted as 'produced' and went all the way to QA.
  verified_run(){ # $1=id $2=model -> produced|half|cancel (a broken product = half)
    local r once="$Z/.lane-once-$1.json"
    python3 "$CADRE_ENGINE_ROOT/lib/qe_core.py" lane-status "$WT" > "$once" 2>>"$DLOG" || : > "$once"
    r=$(run_single "$1" "$2")
    if [ "$r" = "produced" ] && [ -s "$once" ]; then   # B192: a new file outside the plan is removed from the product, a changed existing file goes to the judge
      python3 "$CADRE_ENGINE_ROOT/lib/qe_core.py" scope "$WT" "$Z/plan.json" "$1" "$once" "$Z/scope-outside" 2>>"$DLOG" | while IFS= read -r s; do dlog "$s"; done
    fi
    if [ "$r" = "produced" ] && [ -n "$dfiles" ] && ! python3 "$CADRE_ENGINE_ROOT/lib/qe_core.py" verify "$WT" "$dfiles" 2>>"$DLOG"; then
      dlog "task $1: fast-verify FAILED (broken output from ${2:-auto}) -> escalate"; r=half
    fi
    echo "$r"
  }
  d=$(verified_run "$gid" "$mdl")
  [ "$d" = "produced" ] || [ "$d" = "cancel" ] || tool_waiting && { echo "$d"; return; }
  if [ "$mode" = report ]; then   # G-R6: the report writer moves to the next link of the plan chain; the local 27B model does not write results
    local sub="$mdl" tried=" $mdl " n=0
    while [ "$n" -lt 4 ]; do
      n=$((n + 1))
      sub="$(python3 "$CADRE_ENGINE_ROOT/lib/model_router.py" next writer "$sub" 2>/dev/null)" || break
      [ -n "$sub" ] || break
      case "$tried" in *" $sub "*) break;; esac
      tried="$tried$sub "
      dlog "task $gid (report writer) half -> next plan-chain link: $sub"
      d=$(verified_run "$gid" "$sub"); [ "$d" = "produced" ] || [ "$d" = "cancel" ] || tool_waiting && { echo "$d"; return; }
    done
    if python3 "$CADRE_ENGINE_ROOT/lib/report_mode.py" recover "$gid" >>"$DLOG" 2>&1; then   # B238: the product is on disk and passes the gate
      dlog "task $gid RECOVERED (B238): writer chain exhausted but the report and evidence appendix on disk passed report QA -> produced"
      python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" task "$CADRE_RUN_ID" "$gid" produced || exit 1; echo produced; return
    fi
    python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" last-attempt-budget "$CADRE_RUN_ID" "$gid" 2>/dev/null && CADRE_TASK_ID="$gid" python3 -c 'import os,sys; sys.path.insert(0,os.environ["CADRE_ENGINE_ROOT"]+"/lib"); from qe_core import budget_wait_save; from pathlib import Path; budget_wait_save(Path(os.environ["CADRE_RUN_DIR"])/"dispatch.log")'
    dlog "task $gid FAILED (report writer: plan chain exhausted, G-R6)"; echo failed; return
  fi
  if [ -n "$mdl" ]; then
    local sub
    for sub in $(other_provider "$mdl" "$gid"); do   # B281: not a single alternative; the next providers of the chain (free ones included)
      dlog "task $gid half ($mdl) -> another provider: $sub"
      d=$(verified_run "$gid" "$sub"); [ "$d" = "produced" ] || [ "$d" = "cancel" ] || tool_waiting && { echo "$d"; return; }
      # B331: the failure record is written to the model that fell (other_provider only records the first model)
      python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" budget-rejection "$CADRE_RUN_ID" "${gid}" "${sub%%/*}" >/dev/null 2>&1 \
        || python3 "$CADRE_ENGINE_ROOT/lib/model_router.py" failure "$sub" "task half" >/dev/null 2>&1
    done
    dlog "task $gid: cloud chain exhausted"
    python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" last-attempt-budget "$CADRE_RUN_ID" "$gid" 2>/dev/null && CADRE_TASK_ID="$gid" python3 -c 'import os,sys; sys.path.insert(0,os.environ["CADRE_ENGINE_ROOT"]+"/lib"); from qe_core import budget_wait_save; from pathlib import Path; budget_wait_save(Path(os.environ["CADRE_RUN_DIR"])/"dispatch.log")'

  fi
  dlog "task $gid FAILED (chain exhausted)"
  echo failed
}

lane_model(){ # $1=id -> the model given to the lane (empty=the router chooses); the plan model goes through the budget/difficulty preflight
  local gm gmdl gz gmdl2=""
  gm=$(printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" model "$1")
  gz=$(printf '%s' "$plan_json" | GID="$1" python3 -c "import json,sys,os;print([g.get('difficulty',3) for g in json.load(sys.stdin)['tasks'] if g['id']==os.environ['GID']][0])")
  # the plan model goes through the difficulty/budget preflight (model_router.difficulty_model)
  if [ -n "$gm" ] && [ "$gm" != auto ]; then gmdl="$(model_map "$gm")"; else gmdl=""; fi
  gmdl2="$(CADRE_TASK_ID="$1" python3 "$CADRE_ENGINE_ROOT/lib/model_router.py" suitable "$gmdl" impl "$gz" 2>/dev/null)"   # B130: the old `secret` argument was removed
  [ "$gmdl2" = "$gmdl" ] || dlog "task $1: $gm (${gmdl:-auto}) -> ${gmdl2:-auto} (difficulty=$gz/budget)"
  printf '%s' "$gmdl2"
}

report_writer_execute(){ # G-R6: $1=id -> produced|failed|cancel; the lane that writes the report runs on the strong model of the plan chain
  local gid="$1" m
  m="$(python3 "$CADRE_ENGINE_ROOT/lib/model_router.py" first writer middle 30000 2>/dev/null)"   # B239: plan chain + breathing filter
  if [ -z "$m" ] && cloud_wait plan middle "${LEVEL_POINTS:-0}"; then m="$(python3 "$CADRE_ENGINE_ROOT/lib/model_router.py" first writer middle 30000 2>/dev/null)"; fi
  if [ -z "$m" ]; then
    CADRE_TASK_ID="$gid" python3 -c 'import os,sys; sys.path.insert(0,os.environ["CADRE_ENGINE_ROOT"]+"/lib"); from qe_core import budget_wait_save; from pathlib import Path; budget_wait_save(Path(os.environ["CADRE_RUN_DIR"])/"dispatch.log")'
    dlog "task $gid FAILED (report writer: no strong model available, G-R6)"; echo failed; return
  fi
  dlog "task $gid: report writer (G-R6) -> $m"
  local d; d=$(task_execute_chained "$gid" "$m" report)
  if [ "$d" = produced ]; then
    case "${LEVEL_NAME:-}" in   # B226: while extra lanes are running, the lane pocket is not handed to the judge (budget_gate.lanes_finished)
      hard|very-hard|ultra) : > "$Z/extra-lane.flag"; report_reconciliation "$gid" "$m"; rm -f "$Z/extra-lane.flag";;
      *) python3 "$CADRE_ENGINE_ROOT/lib/report_mode.py" analyst-record single "$WT" "$m" "" "" "difficulty below reconciliation threshold (G-R7 applies only to hard+)" 2>>"$DLOG";;
    esac
  fi
  echo "$d"
}


report_reconciliation(){ # G-R7: $1=id $2=first analyst; in a hard+ report the second analyst writes without seeing the first draft, a third model reconciles (no stdout)
  local gid="$1" m1="$2" m2 m3 ry ky A="$Z/analyst-a" B="$Z/analyst-b" GP d
  # B258: on re-approval (--proceed/retry), if the reconciler already produced or was discarded, G-R7 does not run again (~$0.3 + 5 min).
  if [ "$(task_state "$gid-reconciliation" 2>/dev/null)" = produced ] || [ -f "$(task_dir "$gid-reconciliation")/rejected.flag" ]; then
    dlog "G-R7: reconciliation already concluded — extra lanes skipped (B258)"; return 0
  fi
  read -r ry ky <<<"$(python3 "$CADRE_ENGINE_ROOT/lib/report_mode.py" paths 2>>"$DLOG")"
  m2="$(python3 "$CADRE_ENGINE_ROOT/lib/model_router.py" next plan "$m1" 2>/dev/null)"
  if [ -z "$ry" ] || [ -z "$m2" ] || [ "$m2" = "$m1" ]; then
    dlog "G-R7: no second strong analyst available -> single analyst ($m1)"
    python3 "$CADRE_ENGINE_ROOT/lib/report_mode.py" analyst-record single "$WT" "$m1" "" "" "no second strong analyst available" 2>>"$DLOG"
    return 0
  fi
  mkdir -p "$A" "$B" || return 0
  mv -f "$WT/$ry" "$A/" 2>>"$DLOG"; [ -f "$WT/$ky" ] && mv -f "$WT/$ky" "$A/" 2>>"$DLOG"   # B cannot see the first draft
  GP="$(task_dir "$gid-analyst-b")/prompt.md" && cat "$(task_pf "$gid")" > "$GP" \
    && printf '\n%s\n' "$(python3 "$CADRE_ENGINE_ROOT/lib/report_mode.py" analyst-prompt 2>>"$DLOG")" >> "$GP" || GP=""
  dlog "G-R7: second independent analyst -> $m2"
  d="$( [ -n "$GP" ] && extra_lane_execute "$gid-analyst-b" "$m2" "$GP")"
  if [ "$d" != produced ] || [ ! -s "$WT/$ry" ]; then
    dlog "G-R7: second analyst produced no output (${d:-no prompt}) -> use the first analyst report"; cp -f "$A/"* "$(dirname "$WT/$ry")/" 2>>"$DLOG"
    : > "$(task_dir "$gid-analyst-b")/rejected.flag"   # B256: a lane whose output was discarded does not count as a writer (judge pool)
    python3 "$CADRE_ENGINE_ROOT/lib/report_mode.py" analyst-record single "$WT" "$m1" "$m2" "" "second analyst produced no output" 2>>"$DLOG"
    return 0
  fi
  mv -f "$WT/$ry" "$B/" 2>>"$DLOG"; [ -f "$WT/$ky" ] && mv -f "$WT/$ky" "$B/" 2>>"$DLOG"
  mkdir -p "$WT/.cadre-plan/analyst-a" "$WT/.cadre-plan/analyst-b" && cp -f "$A/"* "$WT/.cadre-plan/analyst-a/" && cp -f "$B/"* "$WT/.cadre-plan/analyst-b/"
  m3="$(python3 "$CADRE_ENGINE_ROOT/lib/model_router.py" next plan "$m2" 2>/dev/null)"; [ -n "$m3" ] || m3="$m1"
  GP="$(task_dir "$gid-reconciliation")/prompt.md" && { python3 "$CADRE_ENGINE_ROOT/lib/report_mode.py" reconciliation-prompt 2>>"$DLOG"; echo; cat "$(task_pf "$gid")"; } > "$GP"
  dlog "G-R7: reconciliation -> $m3"
  d="$(extra_lane_execute "$gid-reconciliation" "$m3" "$GP")"
  if [ "$d" != produced ] || [ ! -s "$WT/$ry" ]; then
    dlog "G-R7: reconciliation produced no output (${d}) -> use the first analyst report"; cp -f "$A/"* "$(dirname "$WT/$ry")/" 2>>"$DLOG"
    : > "$(task_dir "$gid-analyst-b")/rejected.flag"; : > "$(task_dir "$gid-reconciliation")/rejected.flag"   # B256
    python3 "$CADRE_ENGINE_ROOT/lib/report_mode.py" analyst-record single "$WT" "$m1" "$m2" "$m3" "reconciliation produced no output" 2>>"$DLOG"
    return 0
  fi
  python3 "$CADRE_ENGINE_ROOT/lib/report_mode.py" analyst-record reconciliation "$WT" "$m1" "$m2" "$m3" "" 2>>"$DLOG"
  return 0
}

task_divide(){ # G-E10: $1=id -> produced|failed|cancel; a lane whose chain is exhausted is split once into 2-3 sequential sub-tasks (task_split.py)
  local gid="$1" ids sid r bm bp raw
  ids="$(python3 "$CADRE_ENGINE_ROOT/lib/task_split.py" sub "$CADRE_RUN_ID" "$gid" 2>>"$DLOG")"
  if [ -z "$ids" ]; then
    bm="$(python3 "$CADRE_ENGINE_ROOT/lib/model_router.py" first plan hard 30000 2>/dev/null)" || { dlog "task $gid: no planner available for splitting"; echo failed; return; }
    [ -n "$bm" ] || { dlog "split: no cloud capacity available"; echo failed; return; }
    bp="$(task_dir "divide-$gid")" && mkdir -p "$bp/sandbox" || { echo failed; return; }
    dlog "task $gid SPLIT INTO SUBTASKS (G-E10, level ${LEVEL_NAME:-?}, planner $bm)"
    raw="$(planner_call "$bm" "$(python3 "$CADRE_ENGINE_ROOT/lib/task_split.py" prompt "$CADRE_RUN_ID" "$gid" 2>>"$DLOG")" "divide-$gid" "$bp/sandbox" 4 split)" || { echo failed; return; }
    ids="$(printf '%s' "$raw" | python3 "$CADRE_ENGINE_ROOT/lib/task_split.py" merge "$CADRE_RUN_ID" "$gid" "$WT" 2>>"$DLOG")" || ids=""
    [ -n "$ids" ] || { dlog "task $gid: subplan invalid or empty -> no split"; echo failed; return; }
    plan_json="$(cat "$Z/plan.json")"
    dlog "task $gid subtasks: $ids"
  fi
  for sid in $ids; do
    [ -f "$Z/cancel.flag" ] && { echo cancel; return; }
    r=$(task_execute_chained "$sid" "$(lane_model "$sid")")
    python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" task-result "$CADRE_RUN_ID" "$sid" "$r" >/dev/null || { echo failed; return; }
    if [ "$r" != produced ]; then
      dlog "task $gid: subtask $sid $r -> split failed"
      if [ "$r" = cancel ]; then echo cancel; else echo failed; fi
      return
    fi
  done
  echo produced
}

parallel_go(){ # DESIGN-5(3): $1=model $2=previous pairs; if the same small provider is needed a second time in parallel, Go if there is a Go share (B100 was relaxed for this)
  local m="$1" go
  case "$m" in opencode-go/*|claude/*) printf '%s' "$m"; return;; esac
  case " $2" in *"=${m%%/*}/"*) ;; *) printf '%s' "$m"; return;; esac
  go="$(python3 "$CADRE_ENGINE_ROOT/lib/model_router.py" suitable opencode-go/deepseek-v4-pro impl 3 2>/dev/null)"
  if [ "$go" = opencode-go/deepseek-v4-pro ]; then dlog "PARALLEL: ${m%%/*} requested a second time -> Go ($go)"; printf '%s' "$go"; else printf '%s' "$m"; fi
}

parallel_run(){ # DESIGN-5(2): $@=id=model; each lane in the background with its own threshold chain, result in $Z/.parallel-<id>; all are waited for
  local c pids=""
  for c in "$@"; do
    ( task_execute_chained "${c%%=*}" "${c#*=}" > "$Z/.parallel-${c%%=*}" ) &
    pids="$pids $!"
  done
  for c in $pids; do wait "$c"; done
}

# One worktree: dependent lanes run serially; independent cloud lanes (DESIGN-5) run concurrently when their files are disjoint.
PARALLEL="${CADRE_PARALLEL_LANE:-2}"
PARALLEL_FINISHED=" "
OUTCOMES=""
ALL_PRODUCED=1
for gid in $GIDS; do
  [ -f "$Z/cancel.flag" ] && { dlog "JOB CANCELLED (flag)"; exit 130; }
  case "$PARALLEL_FINISHED" in *" $gid "*) continue;; esac
  ready=1
  needs=$(printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" dependent "$gid") || exit 1
  for dep in $needs; do
    ds=$(python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" task-result "$CADRE_RUN_ID" "$dep") || exit 1
    [ "$ds" = produced ] || ready=0
  done
  if [ "$ready" = 0 ]; then
    r=blocked
    python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" task "$CADRE_RUN_ID" "$gid" blocked || exit 1
    dlog "task $gid: prerequisite failed; task was not started"
  elif [ -n "$REPORT_MODE" ] && python3 "$CADRE_ENGINE_ROOT/lib/report_mode.py" writer "$gid" 2>>"$DLOG"; then
    r=$(report_writer_execute "$gid")
  elif [ -n "$(python3 "$CADRE_ENGINE_ROOT/lib/task_split.py" sub "$CADRE_RUN_ID" "$gid" 2>>"$DLOG")" ]; then
    r=$(task_divide "$gid")   # G-E10: a task split in an earlier attempt continues with its sub-tasks
  else
    gm="$(lane_model "$gid")"; pairs="$gid=$gm"
    if [ -n "$gm" ] && [ "$PARALLEL" -gt 1 ] 2>/dev/null; then
      for og in $(printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" parallel "$gid" "$PARALLEL" 2>>"$DLOG"); do
        om="$(lane_model "$og")"; [ -n "$om" ] && pairs="$pairs $og=$(parallel_go "$om" "$pairs")"
      done
    fi
    [ "$pairs" = "$gid=$gm" ] || { dlog "PARALLEL (DESIGN-5): $pairs"; parallel_run $pairs; }
    for c in $pairs; do
      og="${c%%=*}"
      if [ "$pairs" = "$gid=$gm" ]; then ro=$(task_execute_chained "$gid" "$gm"); else ro="$(cat "$Z/.parallel-$og" 2>/dev/null)"; ro="${ro:-failed}"; fi
      if [ "$ro" = failed ] && python3 "$CADRE_ENGINE_ROOT/lib/task_split.py" splittable "$CADRE_RUN_ID" "$og" "${LEVEL_NAME:-}" >/dev/null 2>>"$DLOG"; then
        ro=$(task_divide "$og")
      fi
      if [ "$og" = "$gid" ]; then r="$ro"; continue; fi
      python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" task-result "$CADRE_RUN_ID" "$og" "$ro" || exit 1
      OUTCOMES="$OUTCOMES $og=$ro"; PARALLEL_FINISHED="$PARALLEL_FINISHED$og "
      [ "$ro" = produced ] || ALL_PRODUCED=0
    done
  fi
  python3 "$CADRE_ENGINE_ROOT/lib/run_store.py" task-result "$CADRE_RUN_ID" "$gid" "$r" || exit 1
  OUTCOMES="$OUTCOMES $gid=$r"
  [ "$r" = produced ] || ALL_PRODUCED=0
done
dlog "task outcomes:$OUTCOMES"
[ -f "$Z/cancel.flag" ] && { dlog "JOB CANCELLED (flag)"; exit 130; }
[ "$ALL_PRODUCED" = 1 ] || { dlog "blocked/missing task present; product preserved, merge was not started"; exit 1; }

# --- MERGE + QUALITY ---
# LIGHT PATH: on a small job (<=3 tasks) the LLM merge is SKIPPED;
# on a large job the merge uses the cloud chain of the code-writing role.
QUALITY_MODEL=$(printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" quality_model)
KM=$(model_map "$QUALITY_MODEL")
KM_Y="$(python3 "$CADRE_ENGINE_ROOT/lib/model_router.py" first quality middle 30000 2>/dev/null)"; [ -n "$KM_Y" ] && { KM="$KM_Y"; QUALITY_MODEL="router"; }   # Stage 10
TASK_COUNT=$(printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" gids | wc -w | tr -d ' ')
if [ "$TASK_COUNT" -le 3 ] || [ -n "$REPORT_MODE" ]; then   # a report job has no code to merge
  dlog "LIGHT MERGE: $TASK_COUNT tasks, all produced — verification without an LLM"
  # C28: commit only through qe_commit_wip with the shared EXC list (harness files never enter the product branch); no raw `git add -A`.
  n_commit="$(qe_commit_wip "$WT" "$project_ref" "dispatch: task outcomes" 2>>"$DLOG")"; dlog "merge commit: ${n_commit:-0} file"
  ( cd "$WT" && echo "== diff summary:"; git diff --stat HEAD~1 2>/dev/null | tail -5 ) >>"$DLOG" 2>&1
  B1="produced"
else
BP="$(task_dir merge)/prompt.md"
{ printf 'MERGE TASK: use git log/diff to review this worktree'"'"' changes from ALL tasks in order (%s).\n' \
    "$(printf '%s' "$plan_json" | python3 "$CADRE_ENGINE_ROOT/lib/dispatch_util.py" order)"
  printf 'LIMITS: at most 5 actions; no deep analysis; run existing tests; if there are no conflicts or defects, write a three-line summary and STOP IMMEDIATELY.\n'
  if [ -n "$KM" ]; then printf 'NOTE: quality review runs separately; only merge and test.\n'; else printf 'QUALITY (self-audit): state the edge cases of the change, run the tests and complete a three-item checklist.\n'; fi
} > "$BP"
MERGE_MODEL="$(python3 "$CADRE_ENGINE_ROOT/lib/model_router.py" first merge middle 30000 2>/dev/null)"   # Stage 10
[ -n "$MERGE_MODEL" ] || { dlog "merge: waiting for cloud capacity"; exit 1; }
dlog "merge model: $MERGE_MODEL"
B1=$(env CADRE_WORKTREE="$WT" CADRE_TASK_ID="merge" CADRE_RUN_MODEL="$MERGE_MODEL" bash "$CADRE_ENGINE_ROOT/bin/job-runner.sh" "$project_ref" "$BP" >>"$DLOG" 2>&1 && awk '{print $1}' "$(task_dir merge)/status.txt" 2>/dev/null)
if [ "${B1:-half}" != "produced" ]; then
  dlog "merge half -> 2. attempt"
  B1=$(env CADRE_WORKTREE="$WT" CADRE_TASK_ID="merge" CADRE_RUN_MODEL="$MERGE_MODEL" bash "$CADRE_ENGINE_ROOT/bin/job-runner.sh" "$project_ref" "$BP" >>"$DLOG" 2>&1 && awk '{print $1}' "$(task_dir merge)/status.txt" 2>/dev/null)
  if [ "${B1:-half}" != "produced" ] && [ -n "$KM$QUALITY_MODEL" ]; then
    SUB=$(python3 "$CADRE_ENGINE_ROOT/lib/model_router.py" alternatives merge "$MERGE_MODEL" 1 2>/dev/null | head -1)
    if [ -n "$SUB" ]; then
      dlog "MERGE STALLED -> next model in chain: $SUB"
      B1=$(env CADRE_WORKTREE="$WT" CADRE_TASK_ID="merge" CADRE_RUN_MODEL="$SUB" bash "$CADRE_ENGINE_ROOT/bin/job-runner.sh" "$project_ref" "$BP" >>"$DLOG" 2>&1 && awk '{print $1}' "$(task_dir merge)/status.txt" 2>/dev/null)
    else
      dlog "MERGE STALLED -> no other model in chain"
    fi
  fi
fi
fi
# One final predicate owns QA + acceptance + quality for both execution paths.
if [ "${B1:-half}" != "produced" ]; then
  dlog "DISPATCH IS HALF (merge=$B1). worktree=$WT"; exit 1
fi
correction_loop; exit $?
