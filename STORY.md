# The story of Cadre

Cadre is a personal experiment that ran from mid-September to late October 2026: can a single developer hand a real coding job to a cheap model, walk away, and get back a change that an independent judge has verified, inside a fixed budget, without the model ever touching the main branch?

This file records what was built, what it proved, what it did not, and why development stopped. It is written for people who want to learn from the attempt, not to sell it.

## What was built

A Python engine, a bash dispatcher, a Node budget plugin for the OpenCode CLI, a Textual terminal panel, and a test suite. The engine turns a typed request into a pipeline:

1. **Proposal.** The request is bound to a source digest, a QA command hash, a model route per role and a money cap. Nothing runs before two separate operator approvals.
2. **Plan and independent review.** A planner model writes a measurable plan; a separate clean session reviews it against the original request. A rejected plan gets exactly one bounded correction.
3. **Sandboxed implementation.** The job runs in an isolated git worktree. The model gets only a shell, and every command runs under a per-task `sandbox-exec` profile: writes to the planned files only (the QA repair round may write anywhere in the worktree), no network, configured read roots and known credential-path denials. The roots include system directories and interpreter locations; see [SECURITY.md](SECURITY.md) for the boundaries.
4. **Project QA with evidence.** The project's own format, lint, type and test commands run under an observer that records which tests were collected and executed. Lint that was weakened by flags counts as unverified.
5. **Independent judge.** A separate model session receives the real diff and the engine's structural evidence and must answer criterion by criterion. Deleted or weakened tests block acceptance unless the operator records an explicit test-maintenance decision.
6. **Ledger and delivery.** Every reservation and receipt is written to a local ledger with provider receipts; commit, push and merge remain operator decisions.

The private working repository also held a product contract and a traceability matrix from requirements to code, tests and recorded evidence; both are in Turkish and are not part of this showcase.

## What the recorded runs showed

On 26 October 2026 the engine completed its first end-to-end acceptance of new code on a real project (a source adapter fix with tests in Feedloom): plan, independent plan review, 20 lines of product code and 301 lines of tests written by the model, 396 passing tests in the target test file, and an independent judge that passed all five criteria. Cost 1.40 USD, 45 minutes of active time, all roles on DeepSeek V4 Pro through OpenRouter. The judge step ran twice: the engine rejected the first answer for a malformed field, the parser was made tolerant during the run and the step re-run. A second, smaller job (a test-debt cleanup) produced a correct change that passed the project's full QA, without formal acceptance.

Earlier, on 21 October, a narrower verification-only run had passed for 0.25 USD. No typed-request run exceeded its cap (53 runs, 45 of them paid, 7.49 USD in total, at most 44% of a cap used). In the older free-form mode the engine could raise a run's cap up to an operator ceiling, and did so in 9 runs. Across all 424 recorded runs the engine pushed or merged nothing.

## What it did not prove

- **Unattended operation.** Every accepted run needed the operator (or an assistant) to watch, interpret a stop and intervene. Eight runs on 26 October produced one accepted result. The other seven stopped on a rejected plan correction with no operator path, a budget estimate that was 2x too low, a carried change counted as out of scope, a per-task time cap, a test-maintenance rule that did not cover parametrized tests, one operator mistake in the QA command, and one operator cancel.
- **Economics.** The accepted job cost 1.40 USD, or 2.64 USD counting the two earlier failed attempts at the same job, plus supervision. No comparison on the same jobs measured cost, time and operator intervention against an interactive subscription assistant, so an economic advantage was not established.
- **Scale.** Jobs were small (one to three files). No multi-hour job was attempted in the final configuration.

## Why development stopped

Live runs repeatedly surfaced places where a fail-closed gate stopped correct work: a rejected automatic plan correction left no operator path; a carried uncommitted change in the source counted as out of scope; a 10-minute task cap cut an edit that was already finished; a judge answer with `"eksik": null` was rejected as malformed. Each fix was small and tested, but these interventions kept the engine from delivering the intended unattended workflow. With a subscription assistant covering daily work, further engine maintenance did not justify the developer's time, and development was frozen. The repository preserves the working controls and the evidence from the attempt.

## What I would change

The scope grew before a repeatable unattended path had been demonstrated. Starting again, I would measure cost, time and operator intervention on a few small real jobs before adding providers, job types or more acceptance rules. Broader support would follow evidence of useful delivery.

## What is worth taking from it

The ideas that transfer to other agent setups are listed at the top of [README.md](README.md#ideas-worth-taking).

## Numbers

| Measure | Value |
| --- | --- |
| Engine / tests | 34.5k / 46.6k lines, 1,974 + 97 tests, full gate ~15 min on an idle machine |
| Accepted real-code runs | 2 (one verification-only on 21 Oct, one new code on 26 Oct) plus one QA-green product without formal acceptance |
| Cost of the first new-code acceptance | 1.40 USD, 45 min active; 143 ledger rows (140 settled by provider receipts, 3 released without a call) sum to 1.404924 USD |
| Runs on the last day | 8, of which 1 accepted; 3.84 USD spent, 2.43 USD of it on the 7 runs that stopped (0.16 USD after an operator error) |
| Open backlog at freeze | 23 items, see `CONTINUATION.md` |
