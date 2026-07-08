#!/usr/bin/env bash
# frontend 契約 artifact を再生成する:
#   - OpenAPI 3.1 snapshot (openapi/butly.openapi.json)
#   - SSE parser contract fixture (openapi/sse_fixtures/)
# `--check` を渡すと OpenAPI の差分検出のみ行う（CI 用。fixture の
# 差分検出は tests/test_sse_fixture.py が担う）。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -x "$ROOT_DIR/venv/bin/python" ]]; then
  PYTHON="$ROOT_DIR/venv/bin/python"
else
  PYTHON="python3"
fi

if [[ "${1:-}" != "--check" ]]; then
  "$PYTHON" "$ROOT_DIR/scripts/generate_sse_fixture.py"
fi

exec "$PYTHON" "$ROOT_DIR/scripts/generate_openapi.py" "$@"
