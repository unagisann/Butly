#!/usr/bin/env bash
# OpenAPI 3.1 snapshot (openapi/butly.openapi.json) を再生成する。
# `--check` を渡すと差分検出のみ行う（CI 用）。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -x "$ROOT_DIR/venv/bin/python" ]]; then
  PYTHON="$ROOT_DIR/venv/bin/python"
else
  PYTHON="python3"
fi

exec "$PYTHON" "$ROOT_DIR/scripts/generate_openapi.py" "$@"
