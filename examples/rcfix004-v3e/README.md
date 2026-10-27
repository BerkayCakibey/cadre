# Example run record: RC-FIX-004 (26 October 2026)

The first end-to-end acceptance of new code. Job: in the Feedloom project, make the AlphaFeed adapter fall back to a canonical item URL when a record has no `sourceUrl` and its raw id is verified, with tests. All roles ran on `openrouter/deepseek/deepseek-v4-pro`.

The record is edited for publication: project, source and path names are changed, free-text values (request, plan, review and judge evidence) are translated from Turkish, and session, approval and receipt identifiers are removed. Digests and hashes refer to the original text. JSON keys and enum values are the engine's own Turkish identifiers; the most common ones are listed below.

| File | What it is |
| --- | --- |
| `1-request.json` | The typed request as submitted: target, expected output, acceptance text, scope files and tests, duration, cap, budget shares. The 14 KB `prompt` field is omitted because it quotes the target project's source |
| `2-proposal.json` | The proposal the operator approved twice: pinned source head and digest, QA command hash, models per role, review limits, cap; `reasons` lists why each route was eligible |
| `3-plan.json` | The planner's plan: one task with measurable acceptance, estimates and assumptions |
| `4-plan-review.json` | The independent plan review: PASS on the first revision, criterion by criterion with evidence |
| `5-qa.json` | Project QA under the evidence observer: format, lint, type check and the 396 tests of the target test file collected and executed, product binding PASS |
| `6-judge.json` | The independent judge: verdict GECER (accept), five criteria with evidence, independent session |
| `7-outcome.json` | Final state, product revision, cost (1.40 USD of a 3.20 USD cap), 45.5 minutes of active time, timeline |
| `8-ledger.json` | Ledger summary: 143 reservations, 140 settled with a provider receipt and 3 released without a call (2 judge, 1 QA), summing to the 1.404924 USD in `7-outcome.json`; totals per role and the first rows. Reservations are released as each call settles, so their sum is not a concurrent hold |

## What the judge checked

The judge received the real source-to-product diff and the engine's structural evidence, not the model's own summary. Its five criteria, in short:

1. **Original request.** The fallback URL is built only when the record has no `sourceUrl` and the raw id was neither sanitized nor already in hash form; every other path still rejects the record, and nothing outside the two allowed files changed.
2. **Acceptance items.** Each numbered item of the request, (1) to (6) plus (4b), (5a) and (5b), is matched to a test and a line range in the test file.
3. **Id checks.** Missing, boolean, sanitized (`../../etc/passwd`, `token=…`) and hash-form ids are rejected without a fallback; ids such as `sha256-public-item` that only look like hashes still get one.
4. **Changed files and QA.** Exactly two files changed; Ruff format and lint, BasedPyright and 396 of 396 tests passed, and the product revision digest matches the QA record.
5. **Changed tests.** Eight new tests were added, one fixture assertion changed from `rejected` to `normalized` as the request required, and no assertion was loosened, deleted or skipped.

## Key glossary

| Key or value | Meaning |
| --- | --- |
| `gorevler`, `tanim`, `kabul`, `dosyalar`, `bagimli` | plan tasks, task description, acceptance, files, dependencies |
| `tahmin_dk`, `tahmin_usd`, `zorluk` | estimated minutes, estimated USD, difficulty |
| `seritler`, `hakem`, `yardimci` | the budget pockets: lanes (implementation), judge, helper |
| `kalite`, `kurtarici`, `birlestirme` | roles: judge, rescuer (repair), merge |
| `GECER`, `DUZELT` | judge verdicts: accept, fix |
| `TAM`, `KORUNDU` | process evidence complete; carried patch preserved |

The source was a prepared worktree that already carried an uncommitted test completion (+30/−2 lines) from an earlier accepted run. Against the source head the product diff adds 351 lines; the model wrote 20 lines of product code and 301 lines of tests, the rest is the carried change.

The judge's first attempt in this run was rejected by the engine for a malformed field (`"eksik": null`); the parser was made tolerant during the run and the acceptance step re-run, which is why the judge timestamp is nine minutes after QA.
