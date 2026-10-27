# Architecture

Cadre is a pipeline of recorded decisions around model calls. Every stage writes to `~/.local/share/cadre-engineering/runs/<run-id>/run.json`; nothing is kept only in memory, so a run can be stopped, inspected and resumed from disk.

## Pipeline

| Stage | What happens | Where |
| --- | --- | --- |
| Request | Typed JSON (project, target path, prompt, acceptance text, scope files and tests, duration, cap, budget shares) is stored with a request hash | `kosu.py request` |
| Proposal | Source snapshot (HEAD + uncommitted patch + untracked copies), QA command hash, model route per role, review limits, cap and shares; no model call | `kosu.py request-proposal`, `snapshot_source`, `yonlendirici.py`, `sinirlar.py` |
| Approval | First approval returns a confirmation; the second, given separately, authorizes the same proposal | `kosu.py request-approve` |
| Plan | Planner model reads the pinned source and writes a measurable plan (tasks, acceptance, estimates); structural validation and one precise retry | `generate_request_plan`, `dispatch_util.validate_plan` |
| Plan review | A clean session reviews the plan against the original request; verdict `GECER` or `DUZELT` per criterion; one bounded automatic correction, then operator `plan-change` → `plan-stop` → `request-plan` → `plan-resume` | `review_plan`, `plan_duzeltme_ac`, `RunStore.change_plan` |
| Dispatch | Tasks run in a git worktree created from the snapshot; each task gets a time cap (`max(estimate x 3, 30 min)`) and a budget pocket; executors run OpenCode with only the shell tool, and the plugin wraps every command in a per-task `sandbox-exec` profile; `kisitli-kabuk.sh` restores the OpenCode config and `auth.json` if a run changed them | `cadre-dispatch.sh`, `motor.py`, `oturum.project_tool_policy`, `butce-plugin.mjs`, `kisitli-kabuk.sh` |
| QA | The project's QA command runs under an observer (`qa_kaniti.py`) that records collected and executed tests and verifies format, lint and type checks were real; one bounded repair round from a separate pocket | `kabul.qa_kapisi`, `qa_kaniti.py`, `qa_turet.py` |
| Judge | Independent session gets the real diff, structural evidence and the acceptance criteria; the answer must be JSON with evidence per criterion; protocol errors get one fresh judge | `kabul.finalize_run`, `sonuc.parse_review` |
| Acceptance predicate | QA PASS, judge PASS, same product revision, scope respected relative to the pinned source, no deleted or weakened tests without a recorded `test-maintenance` decision, no install traces, process evidence complete | `sonuc.decision`, `sonuc.approved`, `qa_bakim.py` |
| Ledger | Reservations per role before each call, receipts after, provider cost reconciliation (OpenRouter generation receipts), stale-reservation recovery with operator confirmation | `butce_kapisi.py`, `butce.py`, `kosu_defteri.py` |
| Delivery | `cadre push` and `cadre merge` with two typed answers; the engine never pushes on its own | `kosu.py`, `cadre.sh` |

## Module map

- **Run state and locks:** `kosu.py` (RunStore, CLI, plan journal), `proje.py` and `proje_kayit.py` (project mode), `canlilik.py` (liveness), `rotasyon.py` (worktree archiving), `arsiv.py` (verified snapshots).
- **Money:** `butce.py` (provider observation), `butce_kapisi.py` (ledger, allowances, reservations), `aylik_butce.py`, `go_pencere.py`, `kota_gidisat.py`, `opencode_butce.py`, `verim.py` (cost and time per stage), `tahmin_gercek.py` (estimate vs actual), `claude_politikasi.py`, `claude_transport.py`.
- **Routing and models:** `yonlendirici.py`, `model_secimi.py`, `katalog.py`, `seviye.py`, `onelek.py`, `plan_ds.py`.
- **Execution:** `qe_core.py` (core loop, worktree, guards), `motor.py`, `oturum.py` (executor identity, sandbox profile, read roots), `opencode_adaptor.py`, `dispatch_util.py`, `bolme.py` (adaptive splitting), `sozlesme.py` (task contract), `sinirlar.py` (limits).
- **Acceptance:** `kabul.py`, `sonuc.py`, `qa_kaniti.py`, `qa_bakim.py`, `qa_turet.py`, `rapor.py` (report mode with evidenced claims).
- **Operator surface:** `arayuz.py` and `arayuz_veri.py` (Textual panel, event trace), `izle.py` (live monitor), `ekran.py`, `sabah_ozeti.py` (morning summary), `izin.py` (typed approvals and allow-lists), `tasarim_ornek.py` (panel preview data), `kurulum_kontrol.py`, `runtime_sync.py`, `gerceklik.py` (weekly reality checks), `dil.py`, `iskelet.py` (project skeleton).

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
  projects/<project>/{PROJECT.md,POLICY.md,qa-komut,base-branch,qa-profile.json}
  zincir/request-budget.db           ledger (SQLite)
  reports/, logs/                    run ledgers, guards
```

## Glossary

| Turkish | Meaning |
| --- | --- |
| `kosu`, `gorev`, `istek` | run, task, request |
| `kabul`, `hakem`, `kalite` | acceptance, judge, the judge's role name in routes and the ledger |
| `GECER`, `DUZELT`, `eksik` | pass, revise, missing (fields of a review or judge answer) |
| `butce`, `tavan`, `defter`, `kanit` | budget, cap, ledger or journal, evidence |
| `seritler`, `hakem`, `yardimci` | budget shares: implementation lanes, judge, helper roles |
| `birlestirme`, `kurtarici` | merge role, recovery role |
| `zincir` | model route per role (`CADRE_ZINCIR_*`) and the runtime directory that holds the ledger |
| `bekci`, `izle`, `verim`, `kurulum-kontrol` | guard, live monitor, cost and time report, prerequisite check |
