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
echo "1/5 compileall"
"$PYTHON" -m compileall -q \
  app.py main.py dependencies.py butly_api butly_core routers tests \
  scripts/build_backend_sidecar.py scripts/smoke_test_sidecar.py \
  packaging/pyinstaller/sidecar_entry.py

echo
echo "2/5 flake8 fatal checks"
"$PYTHON" -m flake8 --select=E9,F63,F7,F82 \
  app.py main.py dependencies.py butly_api butly_core routers tests \
  scripts/build_backend_sidecar.py scripts/smoke_test_sidecar.py \
  packaging/pyinstaller/sidecar_entry.py

echo
echo "3/5 pytest without integration tests"
"$PYTHON" -m pytest -m "not integration"

echo
echo "4/5 pip dependency consistency"
"$PYTHON" -m pip check

echo
echo "5/5 frontend checks (lint / typecheck / test / build)"
if command -v pnpm >/dev/null 2>&1; then
  pushd "$ROOT_DIR/frontend" >/dev/null
  pnpm install --frozen-lockfile
  pnpm generate:api
  if ! git diff --quiet -- src/api/generated; then
    echo "ERROR: generated API client is out of date."
    exit 1
  fi
  pnpm lint
  pnpm typecheck
  pnpm test
  pnpm build
  if command -v cargo >/dev/null 2>&1; then
    cargo fmt --manifest-path src-tauri/Cargo.toml --all -- --check
  else
    echo "WARNING: cargo not found — Rust formatting was SKIPPED (NOT verified)."
  fi
  popd >/dev/null
else
  echo "WARNING: pnpm not found — frontend checks were SKIPPED (NOT verified)."
  echo "         CI (.github/workflows/windows-desktop.yml) must be green before merge."
fi

echo
echo "All pre-push checks passed."
