---
name: self-check
description: Use after implementing any non-trivial change, before committing or claiming success. Mechanical post-change verification pass over the diff ending in a PASS / PASS WITH WARNINGS / REQUIRES FIXES verdict.
---

# Final check

Check your own change for scope, wiring, quality and evidence; do not use it in place of the separate clean final review. Keep the output short and fit for the job; do not produce unmeasured speed/token estimates or a fixed round limit.

1. Scope: does the changed behaviour match the original request and criteria; are the user's changes kept? Do not remove an unrelated change with revert/reset/clean/stash; report its owner and the difference.
2. Wiring: does the real entry/producer/call path reach the new behaviour, or is it only a fixture or a view?
3. Invariants: are the authority, single-writer, fixed scope/total, secret and data protection gates the same? An internal share transfer and a speed warning are not new permission.
4. QA: real discovery and meaningful positive/negative/limit/interruption checks; do not weaken existing tests to pass. Report/review work is not forced into code QA; the evidence of the job type is what counts.
5. Silent failure: is there a swallowed exception, an ignored exit code, an uncertain effect presented as success? Without definite permission the related action waits; complexity alone is no reason to stop.
6. Limits: are changed source, repetition/concurrency and missing/broken/unknown outcomes verified with the related decision code? Low-risk observation is finite; with a high-risk unknown the effect does not start.
7. Evidence: command/exit code and source version; local fixtures, the real provider and the runtime are separate. A task without evidence is not closed. Do not add recursive delegation or automatic learning.

If there is an engine output schema, use it. Manual final verdict: VERDICT: PASS / PASS WITH WARNINGS / REQUIRES FIXES; add the observed reason and the missing acceptance. Complete the needed repair and verification; use new evidence/the root cause instead of blind repetition.
