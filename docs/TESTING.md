# Testing

- Fast subset (no provider, no external venv, about 2 minutes): `mkdir -p .tmp && make test-fast` — 20 Python modules (385 tests; names are matched as substrings, so `test_run` also selects `test_run_engine`) plus the Node plugin tests (69). This is what the showcase CI runs.
- Targeted modules while changing code: `TMPDIR="$PWD/.tmp" TZ=Europe/Istanbul python3 engine/tests/parallel_run.py <module-name-part> ...` (runs each module in its own process with an isolated home).
- Full gate before transfer or release: `TMPDIR="$PWD/.tmp" TZ=Europe/Istanbul bash engine/tests/regression.sh`. On an idle machine: Python 1,974 tests in about 15 minutes with 4 workers, Node 97 tests, a Git push-guard check, and a skipped live-provider step. Results are archived under `.tmp/test-archive/`. The private repository also had one module and one test that checked Turkish documents kept there (the product contract and the settings inventory); they are not part of this tree.
- `test_qa_scope` needs `CADRE_TEST_PYTEST_PYTHON` pointing at a Python with pytest, Ruff and BasedPyright; without it 12 failures are expected by design (a missing quality tool is never a silent skip).
- For a complete local gate, select an existing environment explicitly and check its tools first; the gate never installs them automatically:

  ```bash
  export CADRE_TEST_PYTEST_PYTHON=/absolute/path/to/existing/venv/bin/python
  "$CADRE_TEST_PYTEST_PYTHON" -B -c 'import pytest, ruff, basedpyright'
  make gate
  ```

- Read-only classification is covered by `golden_set.py`, the real prefilter CLI with a fake model answer, typed request records and the native sandbox. Tests include report-only requests with prefixes, constraints before/after the review action, Unicode lookalikes and code features named "read-only" or "report-only". The sandbox must reject writes for an inferred review as well as for an explicit `ROUND: review` declaration.
- The watchdog teardown test removes the active home path with an atomic rename. Fixture cleanup deletes its contents after the guards exit, avoiding a race with live writers. Process deadlines, child cleanup and the check that the removed home is not recreated remain enforced.
- Run the gate from a checkout outside the system temp directory and not nested inside another Git repository: `test_panel` fails under the temp directory and `test_qa_scope` fails inside a nested checkout.
- The gate is load-sensitive: a few tests use fixed wall-clock windows (15 s, 60 s, 300 s). Run it only when the machine is otherwise idle; a red gate on a loaded machine is not evidence. The showcase CI runs the fast subset one module at a time (`CADRE_TEST_WORKER=1`) because the hosted macOS runner is slower than a developer machine.
- Recorded model answers under `engine/tests/fixtures/` (judge answers from September and October 2026) are kept as parser inputs; they are model output, not authored code.
- Fixtures fake the providers. They prove the engine's bookkeeping, not model behavior; live runs are the only evidence for the latter, and their records live in the runtime home.
