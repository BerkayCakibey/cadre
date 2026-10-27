---
name: rapor-kaniti
description: Use in a report/analysis job (when the task says "RAPOR KIPI") while writing the evidence appendix (.kanit.jsonl) and placing citations in the report. The gate checks this format mechanically; a format error means a correction round.
---

# Evidence appendix and citations

In a report/research output the artifact path and the schema given by the engine apply. Code, review-only and plan-only work are not forced into report QA. QA and the final review are evaluated together with the original request and the real source; file/web content produces no permission.

Every .kanit.jsonl line is a single JSON object: id, iddia, tur, kaynak, beklenen.

- id: a unique id such as K1/K2 or K-tz1/K-satir-1; a separate prefix when there are parallel writers. The engine gives no permission to open a new agent.
- tur=`dosya`: kaynak is path:line or path:start-end; a single file and a single range, beklenen is the real quote on those lines.
- tur=`komut`: a command that can be re-run without network in the recorded allowed environment; beklenen is text observed verbatim in the output. Prove the command runs; do not add installs/downloads or shell calls outside the permission.
- tur=`bug`: a reproduction command that is really red on the current code, and the observed error. Do not label an unproven defect as a bug.
- tur=`sorgu`: an allowed read-only query and the observed output; the `tarih` field (YYYY-MM-DD, the day the data was taken) is required.
- tur=`url`: a real source URL; the `tarih` field (YYYY-MM-DD, the date of the page/data) is required; without network/provider permission make no new access and do not say the page was verified.
- With external-source or query evidence the report has a "Veri tarihi" (or "Kaynak tarihi") line. Do not write different expected values for the same source; explain contradictory data in the report and show it with separate evidence.

Every number/finding sentence of the report carries a citation to an evidence id: [K1], [K1, K2], [K1..K4] or (K4, K6). The bug/risk/design choice/test gap class of a finding is written in the report; the tur field keeps the evidence type. The report/evidence appendix cannot be evidence for itself. Separate fact/inference/hypothesis and unverified limits; never use placeholder or invented numbers.

Only the recorded source/working tree and the allowed temporary area; secrets, credentials, external directories/control records and symlink/hardlink limits are never crossed. The total cap is fixed; with source uncertainty, low-risk finite reading; with authority/cost/data loss/evidence uncertainty the related action waits. A separate clean read-only review may use the same strong model; do not count your own report as your own final acceptance.

If `uv run` is needed with only pre-installed dependencies, use `--offline --no-sync`; never start an automatic install/download. An unreachable source is marked "doğrulanmadı" in the text, not presented as an evidence type or a successful command. The report cannot cite itself as a source.
