"""
dependencies.py
───────────────
ルーター間で共有するグローバル状態・ヘルパー。
main.py の lifespan で初期化し、各ルーターから import する。

中核状態は ``butly_core.runtime.ButlyRuntime`` が保持する。ここで公開する
``instance_manager`` / ``gatekeeper`` / ``mem_block_builder`` / ``instance_store``
は Runtime と同一オブジェクトを指す薄い別名であり、既存ルーターの import を
壊さないための後方互換レイヤーである。
"""
from pathlib import Path
from typing import Dict, Any

from butly_core.core.instance_manager import InstanceManager
from butly_core.core.gatekeeper import Gatekeeper, MemoryBlockBuilder
from butly_core.runtime import ButlyRuntime

# =====================================================================
# パス定義  —  main.py で確定後に設定される
# =====================================================================
DATA_DIR: Path = None   # type: ignore[assignment]
BASE_DIR: Path = None   # type: ignore[assignment]
INSTANCES_DIR: Path = None  # type: ignore[assignment]

# =====================================================================
# 中核 Runtime  —  main.py の lifespan で初期化
# =====================================================================
runtime: ButlyRuntime = None  # type: ignore[assignment]

# 以下は Runtime と同一オブジェクトを指す後方互換の別名。
# main.py が runtime 初期化後に各属性を割り当てる。
instance_manager: InstanceManager = None  # type: ignore[assignment]
instance_store: Dict[str, Any] = {}

gatekeeper: Gatekeeper = None  # type: ignore[assignment]
mem_block_builder: MemoryBlockBuilder = None  # type: ignore[assignment]

# Fire TV モジュール (ADB 未インストール環境でも起動できるように try import)
try:
    from butly_core.core.fire_tv import (
        get_status as tv_get_status,
        send_key,
        launch_app,
        KEYCODES,
        APPS,
    )
    FIRE_TV_AVAILABLE = True
except Exception as _e:
    print(f"[FireTV] Module unavailable: {_e}")
    FIRE_TV_AVAILABLE = False
    tv_get_status = None
    send_key = None
    launch_app = None
    KEYCODES = {}
    APPS = {}


# =====================================================================
# 共通ヘルパー
# =====================================================================
def get_instance_components(instance_name: str) -> dict:
    """インスタンスのコンポーネントを取得（遅延初期化）。

    実体は ``runtime.get_instance_components`` に委譲する。Runtime 未初期化時
    （想定外）は明示的に例外を送出する。
    """
    if runtime is None:
        raise RuntimeError("ButlyRuntime is not initialized.")
    return runtime.get_instance_components(instance_name)
