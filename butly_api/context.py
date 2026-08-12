"""
context.py
──────────
`create_app()` に注入する実行時コンテキスト。

app factory 自体を user data / Runtime から切り離すため、
readiness 判定に必要な参照だけをここへ集約する。
OpenAPI 生成時は context を渡さない（None のまま）ため、
schema 生成が実 instance や `.env` に依存しない。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


def _no_runtime() -> Optional[Any]:
    return None


@dataclass
class ApiContext:
    """`/api/v1/ready` などが参照する backend 実行時状態。

    runtime_supplier: 呼び出し時点の ButlyRuntime を返す callable。
        main.py の初期化順序（deps.runtime 代入前に factory を呼ぶケース）に
        依存しないよう、直接参照ではなく supplier で受け取る。
    settings_loaded: 起動時設定（apply_startup_settings）適用済みか。
        entrypoint 側が適用完了後に True にする。
    auth_token: desktop token（butly_api.auth）。None なら認証無効
        （開発 / Streamlit 併用モード）。entrypoint が環境変数から渡す。
    """

    data_dir: Optional[Path] = None
    instances_dir: Optional[Path] = None
    runtime_supplier: Callable[[], Optional[Any]] = field(default=_no_runtime)
    settings_loaded: bool = False
    auth_token: Optional[str] = None
    developer_mode: bool = False
