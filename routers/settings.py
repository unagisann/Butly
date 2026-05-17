"""
routers/settings.py
───────────────────
グローバル設定・APIキー・config・prompts エンドポイント。
"""
import json
import os
from typing import Dict, Any, Optional

from fastapi import APIRouter, HTTPException, Body, BackgroundTasks
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from butly_core.config import AI_CONFIG, SYSTEM_CONFIG, USER_CONFIG_PATH, _recursive_update
import butly_core.prompts as prompts_module
from butly_core.prompts import USER_PROMPTS_PATH

import dependencies as deps

router = APIRouter()

# --- Pydantic Models ---

class SettingsRequest(BaseModel):
    pass

class ApiKeyRequest(BaseModel):
    api_key: str
    key_type: str = "gemini"

# --- Settings Persistence Helpers ---
SETTINGS_FILE = None  # set during init_settings()

def _get_settings_file():
    global SETTINGS_FILE
    if SETTINGS_FILE is None:
        SETTINGS_FILE = deps.BASE_DIR / "system_config.json"
    return SETTINGS_FILE

def load_settings_from_file():
    sf = _get_settings_file()
    if sf.exists():
        try:
            with open(sf, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_settings_to_file(settings: dict):
    sf = _get_settings_file()
    with open(sf, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)

def apply_startup_settings():
    """起動時に永続化設定を適用する。main.py から呼ばれる。"""
    pass

# --- Editable Prompts ---
EDITABLE_PROMPTS = [
    "SLEEPTIME_SUMMARIZE_PROMPT",
    "BRAIN_EXTRACT_KEYWORDS_PROMPT",
    "BRAIN_SUMMARIZE_CONVERSATION_PROMPT",
    "WEB_UI_DEFAULT_TEMPLATE",
]

# --- Endpoints ---

@router.get("/settings")
def get_settings():
    """Get current system settings."""
    return {}

@router.post("/settings")
def update_settings(request: SettingsRequest):
    """Update system settings."""
    return {"message": "Settings updated", "settings": get_settings()}

@router.post("/settings/api_key")
def set_api_key(request: ApiKeyRequest):
    """APIキーをDATA_DIR/.envに書き込み、即座にos.environに反映する。"""
    key = request.api_key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="APIキーが空です")

    # key_type → 環境変数名の明示的マッピング
    _KEY_TYPE_MAP = {
        "gemini": "GOOGLE_API_KEY",
        "openai": "OPENAI_API_KEY",
        "xai": "XAI_API_KEY",
        "ollama_web_search": "OLLAMA_WEB_SEARCH_API_KEY",
    }

    env_name = _KEY_TYPE_MAP.get(request.key_type)
    if env_name is None:
        raise HTTPException(
            status_code=400,
            detail=f"不明な key_type: {request.key_type} (有効値: {', '.join(_KEY_TYPE_MAP)})",
        )

    env_path = deps.DATA_DIR / ".env"

    env_vars = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, _, v = line.partition('=')
                env_vars[k.strip()] = v.strip()

    env_vars[env_name] = key
    os.environ[env_name] = key

    lines = [f"{k}={v}" for k, v in env_vars.items()]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"[Server] {request.key_type} API key updated and saved to {env_path}")
    return {"message": f"{request.key_type} APIキーを保存しました"}

@router.get("/settings/api_key_status")
def get_api_key_status():
    """各プロバイダーのAPIキー設定状況を返す。"""
    return {
        "gemini": bool(os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")),
        "openai": bool(os.getenv("OPENAI_API_KEY")),
        "xai": bool(os.getenv("XAI_API_KEY")),
        "ollama_web_search": bool(os.getenv("OLLAMA_WEB_SEARCH_API_KEY")),
    }

@router.post("/settings/ollama_test")
def test_ollama_connection(url: str = Body(..., embed=True)):
    """Ollama の接続テストを行う。"""
    import urllib.request
    try:
        req = urllib.request.Request(f"{url.rstrip('/')}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            models = [m["name"] for m in data.get("models", [])]
            return {"status": "ok", "models": models}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.post("/settings/reindex_embeddings")
async def reindex_embeddings(
    instance_name: str = Body("__all__", embed=True),
    background_tasks: BackgroundTasks = None,
):
    """Embedding再生成をバックグラウンドで実行。"""
    from migrate_embeddings import migrate_instance as _migrate_inst

    def _run_reindex(target: str):
        if target == "__all__":
            instances_dir = deps.BASE_DIR / "butly_core" / "instances"
            for d in sorted(instances_dir.iterdir()):
                if d.is_dir():
                    db_p = d / SYSTEM_CONFIG["paths"]["db_name"]
                    if db_p.exists():
                        _migrate_inst(d.name)
        else:
            _migrate_inst(target)

    background_tasks.add_task(run_in_threadpool, _run_reindex, instance_name)
    return {"message": "Embedding再生成を開始しました", "target": instance_name}

# --- Config & Prompts ---

@router.get("/config")
def get_config():
    """Get current effective configuration."""
    return {"AI_CONFIG": AI_CONFIG, "SYSTEM_CONFIG": SYSTEM_CONFIG}

@router.post("/config")
def update_config(config_data: Dict[str, Any] = Body(...)):
    """Update configuration and save to user_config.json."""
    try:
        with open(USER_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=4, ensure_ascii=False)

        if "AI_CONFIG" in config_data:
            _recursive_update(AI_CONFIG, config_data["AI_CONFIG"])
        if "SYSTEM_CONFIG" in config_data:
            _recursive_update(SYSTEM_CONFIG, config_data["SYSTEM_CONFIG"])

        return {"message": "Config updated", "config": get_config()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save config: {e}")

@router.get("/prompts")
def get_prompts():
    """Get editable prompts (locale-aware)."""
    from butly_core.prompts import PromptLoader, _REVERSE_LEGACY_MAP
    loader = PromptLoader()
    result = {}
    for key in EDITABLE_PROMPTS:
        name = _REVERSE_LEGACY_MAP.get(key)
        if name:
            try:
                result[key] = loader.get_template(name)
            except FileNotFoundError:
                result[key] = getattr(prompts_module, key, "")
        else:
            result[key] = getattr(prompts_module, key, "")
    return result

@router.post("/prompts")
def update_prompts(prompts_data: Dict[str, str] = Body(...)):
    """Update prompts and save to user_prompts.json."""
    try:
        safe_data = {k: v for k, v in prompts_data.items() if k in EDITABLE_PROMPTS}

        with open(USER_PROMPTS_PATH, "w", encoding="utf-8") as f:
            json.dump(safe_data, f, indent=4, ensure_ascii=False)

        for key, value in safe_data.items():
            if hasattr(prompts_module, key):
                setattr(prompts_module, key, value)

        return {"message": "Prompts updated", "prompts": get_prompts()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save prompts: {e}")
