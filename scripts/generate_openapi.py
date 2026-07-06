#!/usr/bin/env python
"""
generate_openapi.py
───────────────────
`/api/v1` の OpenAPI 3.1 document を deterministic に生成する。

- butly_api.create_app() を context なしで呼ぶため、user data / `.env` /
  実 instance / 外部 API に依存しない。
- 出力は key sort + indent 2 + 末尾改行で正規化し、再実行しても
  同一入力なら byte 単位で一致する。
- snapshot（openapi/butly.openapi.json）との一致は
  tests/test_openapi_snapshot.py が検証する。

Usage:
    venv/bin/python scripts/generate_openapi.py            # snapshot を更新
    venv/bin/python scripts/generate_openapi.py --check    # 差分があれば exit 1
"""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

OUTPUT_PATH = PROJECT_ROOT / "openapi" / "butly.openapi.json"


def render_openapi_json() -> str:
    from butly_api import create_app

    spec = create_app().openapi()
    return json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the Butly OpenAPI snapshot")
    parser.add_argument(
        "--check",
        action="store_true",
        help="ファイルを更新せず、既存 snapshot との差分があれば exit 1",
    )
    args = parser.parse_args()

    rendered = render_openapi_json()

    if args.check:
        current = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.exists() else ""
        if current != rendered:
            print(f"OpenAPI snapshot is out of date: {OUTPUT_PATH}")
            print("Run: venv/bin/python scripts/generate_openapi.py")
            return 1
        print(f"OpenAPI snapshot is up to date: {OUTPUT_PATH}")
        return 0

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(rendered, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
