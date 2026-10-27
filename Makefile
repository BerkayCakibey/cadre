FAST_MODULES = test_kosu test_kosu_sessions test_kosu_kimligi test_dispatch test_olay_izi test_butce_kapisi test_bolme \
  test_model_secimi test_katalog test_seviye test_kabul_guvencesi test_hakem_bicim test_qa_bicim test_go_pencere \
  test_claude_politika test_s1_moduller test_canlilik test_rotasyon test_kurulum_kontrol

.PHONY: test-fast test-node gate

test-fast:
	mkdir -p .tmp
	TMPDIR="$(CURDIR)/.tmp" TZ=Europe/Istanbul PYTHONDONTWRITEBYTECODE=1 python3 engine/tests/paralel_kos.py $(FAST_MODULES)
	node --test engine/tests/butce-plugin.test.mjs

test-node:
	node --test engine/tests/butce-plugin.test.mjs

gate:
	mkdir -p .tmp
	TMPDIR="$(CURDIR)/.tmp" TZ=Europe/Istanbul bash engine/tests/regression.sh
