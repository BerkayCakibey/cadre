#!/usr/bin/env bash
CADRE_ENGINE_ROOT="${CADRE_ENGINE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
export CADRE_ENGINE_ROOT
exec python3 "$CADRE_ENGINE_ROOT/lib/rotasyon.py"
