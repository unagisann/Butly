"""
dependencies.py
───────────────
ルーター間で共有するグローバル状態・ヘルパー。
main.py の lifespan で初期化し、各ルーターから import する。
"""
from pathlib import Path
from typing import Dict, Any

from fastapi import HTTPException

from butly_core.config import SYSTEM_CONFIG
from butly_core.core.memory import ButlyMemory
from butly_core.core.brain import ButlyBrain
from butly_core.core.chronos import ButlyChronos
from butly_core.core.instance_manager import InstanceManager
from butly_core.core.gatekeeper import Gatekeeper, MemoryBlockBuilder, SessionState

# =====================================================================
# パス定義  —  main.py で確定後に設定される
# =====================================================================
DATA_DIR: Path = None   # type: ignore[assignment]
BASE_DIR: Path = None   # type: ignore[assignment]
INSTANCES_DIR: Path = None  # type: ignore[assignment]

# =====================================================================
# シングルトンインスタンス  —  main.py の lifespan で初期化
# =====================================================================
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
    """インスタンスのコンポーネントを取得（遅延初期化）。"""
    if instance_name in instance_store:
        return instance_store[instance_name]

    if not (INSTANCES_DIR / instance_name).exists():
        raise HTTPException(status_code=404, detail=f"Instance '{instance_name}' not found.")

    print(f"[System] Initializing instance: {instance_name}")
    memory = ButlyMemory(BASE_DIR, instance_name=instance_name)
    brain = ButlyBrain(BASE_DIR)
    chronos = ButlyChronos()

    components = {
        "memory": memory,
        "brain": brain,
        "chronos": chronos,
    }
    instance_store[instance_name] = components
    return components
