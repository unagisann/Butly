#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -x "$ROOT_DIR/venv/bin/python" ]]; then
  PYTHON="$ROOT_DIR/venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="python3"
else
  PYTHON="python"
fi

echo "Using Python: $($PYTHON --version)"

echo
echo "1/4 compileall"
"$PYTHON" -m compileall -q app.py main.py dependencies.py butly_api butly_core routers tests

echo
echo "2/4 flake8 fatal checks"
"$PYTHON" -m flake8 --select=E9,F63,F7,F82 app.py main.py dependencies.py butly_api butly_core routers tests

echo
echo "3/4 pytest without integration tests"
"$PYTHON" -m pytest -m "not integration"

echo
echo "4/4 pip dependency consistency"
"$PYTHON" -m pip check

echo
echo "All pre-push checks passed."
