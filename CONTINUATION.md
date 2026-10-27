# Suggested continuation

Development is frozen. If you pick Cadre up, this is the order that the recorded evidence supports. Items come from the original 23-item backlog; the Turkish identifiers are kept so the history can be traced.

## First: make unattended runs real

1. **Operator paths after every fail-closed stop (CR-32, CR-35).** Every gate that stops a run must leave a recorded operator action that continues it without a new paid plan: plan correction after a rejected automatic fix (done 26.10), scope relative to the pinned source (done), task time floor of 30 minutes (done), test-maintenance decisions that accept a function id as covering its parametrizations (open), plan-review quota that does not exhaust on the first revision (open).
2. **Budget estimates from measurement (CR-12, CR-50, CR-52).** The planner's cost and time estimates were about 2x too low for DeepSeek with a bash-only tool set. Feed `tahmin_gercek` with real runs and let the cap derive from it.
3. **Edit tool inside the sandbox (CR-47).** Most implementation cost was the model fighting file edits through heredocs. A scoped edit tool inside the write scope would cut cost and time.
4. **Five unattended runs in a row** with zero interventions on a real project. Publish the records. Until this exists, the engine is a prototype.

## Second: cost and providers

5. Cheap provider selection measured by session success cost (CR-48, CR-49); OpenRouter reasoning and Zen tool loops (CR-10); continuation after spend alarms and when subscription capacity reopens (CR-03, CR-04).
6. Monthly accounting with pending receipts (CR-08) and the usage measurement that the contract calls "real savings" (CR-52, CR-17: compare with a direct interactive agent on the same jobs).

## Third: product surface

7. Report, research and review job types end to end (CR-11, CR-30); project mode from an empty folder (CR-33); long multi-hour runs with checkpoints (CR-16).
8. Consistent run and project states in the panel (CR-06); judge policy history (CR-18).

## Fourth: portability and release

9. OpenCode V2 plugin migration (the engine pins 1.18.x and the V1 plugin API; Homebrew now ships V2).
10. Linux sandbox (macOS `sandbox-exec` is deprecated by Apple).
11. Reproducible install with a portable provider setup (CR-28), English identifiers and runtime messages (documents, comments and the CLI reference are done, CR-29), real public release acceptance on a second machine (CR-37).
12. Simplify the five large decision paths (`motor._motor`, `kabul.finalize_run`, `kabul.deliver_review`, `kosu._execute_run`, `arayuz_veri.olay_izi`) only after the unattended pilot passes (CR-51).

## What not to do

- Do not add another audit round before a live run shows the next defect; audits produced hundreds of findings and little acceptance.
- Do not raise caps to hide estimate errors; fix the estimate.
- Do not weaken a gate silently; record an operator decision the judge can see.
