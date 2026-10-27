#!/usr/bin/env bash
# Repo gate: needs no personal install, live model or provider account.
set -uo pipefail
QEC="${CADRE_ENGINE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
exec python3 "$QEC/tests/regresyon.py"
