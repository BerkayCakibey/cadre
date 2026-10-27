FAST_MODULES = test_run test_run_sessions test_run_id test_dispatch test_event_trace test_budget_gate test_split \
  test_model_selection test_catalog test_level test_acceptance_assurance test_judge_format test_qa_format test_go_window \
  test_claude_policy test_s1_modules test_liveness test_rotation test_install_check

.PHONY: test-fast test-node gate

test-fast:
	mkdir -p .tmp
	TMPDIR="$(CURDIR)/.tmp" TZ=Europe/Istanbul PYTHONDONTWRITEBYTECODE=1 python3 engine/tests/parallel_run.py $(FAST_MODULES)
	node --test engine/tests/budget-plugin.test.mjs

test-node:
	node --test engine/tests/budget-plugin.test.mjs

gate:
	mkdir -p .tmp
	TMPDIR="$(CURDIR)/.tmp" TZ=Europe/Istanbul bash engine/tests/regression.sh
