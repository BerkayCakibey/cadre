# Architecture

Cadre is a pipeline of recorded decisions around model calls. Every stage writes to `~/.local/share/cadre-engineering/runs/<run-id>/run.json`; nothing is kept only in memory, so a run can be stopped, inspected and resumed from disk.

## Pipeline

| Stage | What happens | Where |
| --- | --- | --- |
| Request | Typed JSON (project, target path, prompt, acceptance text, scope files and tests, duration, cap, budget shares) is stored with a request hash | `run_store.py request` |
| Proposal | Source snapshot (HEAD + uncommitted patch + untracked copies), QA command hash, model route per role, review limits, cap and shares; no model call | `run_store.py request-proposal`, `snapshot_source`, `model_router.py`, `limits.py` |
| Approval | First approval returns a confirmation; the second, given separately, authorizes the same proposal | `run_store.py request-approve` |
| Plan | Planner model reads the pinned source and writes a measurable plan (tasks, acceptance, estimates); structural validation and one precise retry | `generate_request_plan`, `dispatch_util.validate_plan` |
| Plan review | A clean session reviews the plan against the original request; verdict `PASS` or `REVISE` per criterion; one bounded automatic correction, then operator `plan-change` → `plan-stop` → `request-plan` → `plan-resume` | `review_plan`, `plan_correction_open`, `RunStore.change_plan` |
| Dispatch | Tasks run in a git worktree created from the snapshot; each task gets a time cap (`max(estimate x 3, 30 min)`) and a budget pocket; executors run OpenCode with only the shell tool, and the plugin wraps every command in a per-task `sandbox-exec` profile; `restricted-shell.sh` restores the OpenCode config and `auth.json` if a run changed them | `cadre-dispatch.sh`, `runner.py`, `session.project_tool_policy`, `budget-plugin.mjs`, `restricted-shell.sh` |
| QA | The project's QA command runs under an observer (`qa_evidence.py`) that records collected and executed tests and verifies format, lint and type checks were real; one bounded repair round from a separate pocket | `acceptance.qa_gate`, `qa_evidence.py`, `qa_derive.py` |
| Judge | Independent session gets the real diff, structural evidence and the acceptance criteria; the answer must be JSON with evidence per criterion; protocol errors get one fresh judge | `acceptance.finalize_run`, `outcome.parse_review` |
| Acceptance predicate | QA PASS, judge PASS, same product revision, scope respected relative to the pinned source, no deleted or weakened tests without a recorded `test-maintenance` decision, no install traces, process evidence complete | `outcome.decision`, `outcome.approved`, `qa_maintenance.py` |
| Ledger | Reservations per role before each call, receipts after, provider cost reconciliation (OpenRouter generation receipts), stale-reservation recovery with operator confirmation | `budget_gate.py`, `provider_budget.py`, `run_ledger.py` |
| Delivery | `cadre push` and `cadre merge` with two typed answers; the engine never pushes on its own | `run_store.py`, `cadre.sh` |

## Module map

- **Run state and locks:** `run_store.py` (RunStore, CLI, plan journal), `project_mode.py` and `project_registry.py` (project mode), `liveness.py` (liveness), `rotation.py` (worktree archiving), `worktree_archive.py` (verified snapshots).
- **Money:** `provider_budget.py` (provider observation), `budget_gate.py` (ledger, allowances, reservations), `monthly_budget.py`, `go_window.py`, `quota_trend.py`, `opencode_budget.py`, `cost_report.py` (cost and time per stage), `estimate_vs_actual.py` (estimate vs actual), `claude_policy.py`, `claude_transport.py`.
- **Routing and models:** `model_router.py`, `model_selection.py`, `model_catalog.py`, `level.py`, `prefilter.py`, `plan_ds.py`.
- **Execution:** `qe_core.py` (core loop, worktree, guards), `runner.py`, `session.py` (executor identity, sandbox profile, read roots), `opencode_adaptor.py`, `dispatch_util.py`, `task_split.py` (adaptive splitting), `contract.py` (task contract), `limits.py` (limits).
- **Acceptance:** `acceptance.py`, `outcome.py`, `qa_evidence.py`, `qa_maintenance.py`, `qa_derive.py`, `report_mode.py` (report mode with evidenced claims).
- **Operator surface:** `panel_app.py` and `panel_data.py` (Textual panel, event trace), `watch.py` (live monitor), `screen_render.py`, `morning_summary.py` (morning summary), `permission.py` (typed approvals and allow-lists), `design_sample.py` (panel preview data), `install_check.py`, `runtime_sync.py`, `reality.py` (weekly reality checks), `ui_messages.py`, `project_skeleton.py` (project skeleton).

## Invariants worth copying

1. A paid call needs a reservation; a reservation needs an approved cap; the cap is frozen at approval.
2. The source is pinned as a digest; the product is identified by revision digest; the judge's verdict binds to that revision and is never reused for a different one.
3. Permission is typed and two-step; free text from a model or a prompt never grants anything.
4. Every stop has a reason code from a known set and a recorded operator path.
5. Evidence is structural (collected/executed test ids, command hashes, receipts); prose is never evidence.

## Data layout

```
~/.local/share/cadre-engineering/
  runs/<run-id>/run.json plan.json prompt.md source/ tasks/<task>/attempts/<id>/ tool-recovery.json
  worktrees/<project>/<run-id>/      isolated git worktree per run
  projects/<project>/{PROJECT.md,POLICY.md,qa-command,base-branch,qa-profile.json}
  chain/request-budget.db           ledger (SQLite)
  reports/, logs/                    run ledgers, guards
```
