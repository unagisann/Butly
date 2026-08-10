"""
routers/settings.py
───────────────────
グローバル設定・APIキー・config・prompts エンドポイント。
"""

import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Body, BackgroundTasks
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict

from butly_core.config import (
    AI_CONFIG,
    SYSTEM_CONFIG,
    USER_CONFIG_PATH,
    _recursive_update,
)
from butly_core.io_utils import (
    atomic_write_text,
    remove_env_vars,
    upsert_env_var,
)
from butly_core.settings import clear_settings_cache
from butly_core.settings.connections import LLMConnection
import butly_core.prompts as prompts_module
from butly_core.prompts import USER_PROMPTS_PATH

import dependencies as deps

router = APIRouter()

_MODEL_CATALOG_TTL_SECONDS = 600.0
_MODEL_CATALOG_CACHE: dict[str, tuple[float, tuple[str, ...]]] = {}
_MODEL_CATALOG_LOCK = threading.RLock()


def _invalidate_model_catalog(connection_id: str | None = None) -> None:
    """Invalidate dynamic model discovery without touching static presets."""
    with _MODEL_CATALOG_LOCK:
        if connection_id is None:
            _MODEL_CATALOG_CACHE.clear()
        else:
            _MODEL_CATALOG_CACHE.pop(connection_id, None)


def _discover_connection_models(conn) -> list[str]:
    """Fetch one Connection's raw model IDs exactly once per cache fill."""
    if conn.api_key_env and not conn.resolve_api_key():
        return []

    if conn.protocol == "gemini_native":
        try:
            from google import genai

            api_key = conn.resolve_api_key()
            if not api_key:
                return []
            client = genai.Client(api_key=api_key)
            models = []
            for model in client.models.list():
                model_id = model.name
                if model_id and model_id.startswith("models/"):
                    model_id = model_id[len("models/") :]
                if model_id:
                    models.append(model_id)
            return list(dict.fromkeys(models))
        except Exception:
            return []

    if conn.protocol == "openai_compat":
        try:
            import urllib.request

            base_url = conn.resolve_base_url()
            if not base_url:
                return []
            url = base_url.rstrip("/") + "/models"
            headers = {"Accept": "application/json"}
            api_key = conn.resolve_api_key()
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            if conn.extra_headers:
                headers.update(conn.extra_headers)
            request = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(request, timeout=5) as response:
                data = json.loads(response.read())
            models = [
                item.get("id")
                for item in (data.get("data") or [])
                if isinstance(item, dict) and item.get("id")
            ]
            return list(dict.fromkeys(models))
        except Exception:
            return []

    return []


def _connection_model_catalog(conn) -> list[str]:
    """Return a cached raw model catalog for one Connection."""
    with _MODEL_CATALOG_LOCK:
        now = time.monotonic()
        cached = _MODEL_CATALOG_CACHE.get(conn.id)
        if cached and cached[0] > now:
            return list(cached[1])

        models = tuple(_discover_connection_models(conn))
        _MODEL_CATALOG_CACHE[conn.id] = (
            time.monotonic() + _MODEL_CATALOG_TTL_SECONDS,
            models,
        )
        return list(models)


# --- Pydantic Models ---


class SettingsRequest(BaseModel):
    pass


class ApiKeyRequest(BaseModel):
    api_key: str
    key_type: str = "gemini"


class ConnectionApiKeyRequest(BaseModel):
    api_key: str


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
    atomic_write_text(sf, json.dumps(settings, indent=2))


def _env_file_path() -> Path:
    """Return the runtime ``.env`` path, including direct-test fallback."""
    data_dir = deps.DATA_DIR
    if data_dir is None:
        data_dir = USER_CONFIG_PATH.parent
    return data_dir / ".env"


def _validate_api_key_value(value: str) -> str:
    if any(
        ord(char) < 32 or 127 <= ord(char) <= 159
        for char in value
    ):
        raise HTTPException(
            status_code=400,
            detail="APIキーに制御文字を含めることはできません",
        )
    key = value.strip()
    if not key:
        raise HTTPException(status_code=400, detail="APIキーが空です")
    return key


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
    key = _validate_api_key_value(request.api_key)

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

    env_path = _env_file_path()
    upsert_env_var(env_path, env_name, key)
    os.environ[env_name] = key
    _invalidate_model_catalog()

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


OLLAMA_BASE_URL_ENV = "OLLAMA_BASE_URL"


def _ollama_root_url() -> str:
    """built-in ollama connection の実効 URL を root 形（/v1 無し）で返す。"""
    from butly_core.llm.connections import get_connection

    resolved = get_connection("ollama").resolve_base_url() or ""
    return _strip_v1(resolved)


def _strip_v1(url: str) -> str:
    trimmed = url.rstrip("/")
    if trimmed.endswith("/v1"):
        trimmed = trimmed[: -len("/v1")]
    return trimmed


@router.get("/settings/ollama_url")
def get_ollama_url():
    """現在の Ollama 接続先を返す。

    UI は root 形（``http://host:11434``）を扱う。接続テストが Ollama ネイティブ
    API (``/api/tags``) を叩くためで、保存時に OpenAI 互換の ``/v1`` を付ける。
    """
    return {
        "url": _ollama_root_url(),
        "source": "env" if os.getenv(OLLAMA_BASE_URL_ENV) else "default",
    }


@router.post("/settings/ollama_url")
def set_ollama_url(url: str = Body(..., embed=True)):
    """Ollama の接続先を DATA_DIR/.env に保存し、即座に反映する。

    built-in connection は上書き不可なので、正規の逃げ道である
    ``base_url_env`` (= ``OLLAMA_BASE_URL``) に書き込む。
    ``Connection.resolve_base_url()`` は毎回 env を読むため再起動は不要。
    """
    from butly_core.llm.connections import validate_base_url

    root = _strip_v1(str(url or "").strip())
    if not root:
        raise HTTPException(status_code=400, detail="接続先URLが空です")
    try:
        validate_base_url(root)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # 保存するのは OpenAI 互換ベース。UI/接続テストが使う root 形に /v1 を足す。
    base_url = f"{root}/v1"
    env_path = _env_file_path()
    upsert_env_var(env_path, OLLAMA_BASE_URL_ENV, base_url)
    os.environ[OLLAMA_BASE_URL_ENV] = base_url
    _invalidate_model_catalog("ollama")

    print(f"[Server] Ollama base URL updated to {base_url} (saved to {env_path})")
    return {"message": "Ollama の接続先を保存しました", "url": root}


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
    """Update config sections without erasing unrelated top-level settings."""
    try:
        persisted = _load_user_config()
        for section in ("AI_CONFIG", "SYSTEM_CONFIG"):
            if section in config_data:
                persisted[section] = config_data[section]
        atomic_write_text(
            USER_CONFIG_PATH,
            json.dumps(persisted, indent=4, ensure_ascii=False),
        )

        if "AI_CONFIG" in config_data:
            _recursive_update(AI_CONFIG, config_data["AI_CONFIG"])
        if "SYSTEM_CONFIG" in config_data:
            _recursive_update(SYSTEM_CONFIG, config_data["SYSTEM_CONFIG"])
        clear_settings_cache()

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

        atomic_write_text(
            USER_PROMPTS_PATH,
            json.dumps(safe_data, indent=4, ensure_ascii=False),
        )

        for key, value in safe_data.items():
            if hasattr(prompts_module, key):
                setattr(prompts_module, key, value)

        return {"message": "Prompts updated", "prompts": get_prompts()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save prompts: {e}")


# =====================================================================
# Connection 管理 (Phase 3)
# =====================================================================


class ConnectionPayload(LLMConnection):
    """user_config.json の LLM_CONNECTIONS 1 エントリ。"""

    model_config = ConfigDict(extra="forbid")

    protocol: str = "openai_compat"


def _load_user_config() -> dict:
    if not USER_CONFIG_PATH.exists():
        return {}
    try:
        with open(USER_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_user_config(data: dict) -> None:
    atomic_write_text(
        USER_CONFIG_PATH,
        json.dumps(data, indent=4, ensure_ascii=False),
    )
    clear_settings_cache()


def _connection_to_dict(conn) -> dict:
    """Connection dataclass を JSON 化可能な dict に変換。"""
    return {
        "id": conn.id,
        "protocol": conn.protocol,
        "base_url": conn.base_url,
        "base_url_env": conn.base_url_env,
        "api_key_env": conn.api_key_env,
        "api_key_fallback_envs": list(conn.api_key_fallback_envs),
        "label": conn.label,
        "extra_headers": dict(conn.extra_headers),
        "embeddings_supported": conn.embeddings_supported,
        "embedding_model_env": conn.embedding_model_env,
        "default_embedding_model": conn.default_embedding_model,
        "model_name_strip_prefix": conn.model_name_strip_prefix,
    }


@router.get("/settings/connections")
def list_connections_endpoint():
    """全 Connection (built-in + user 定義) を返す。"""
    from butly_core.llm.connections import list_connections, is_builtin_connection

    items = []
    for conn in list_connections():
        d = _connection_to_dict(conn)
        d["is_builtin"] = is_builtin_connection(conn.id)
        d["api_key_set"] = bool(conn.resolve_api_key()) if conn.api_key_env else None
        items.append(d)
    return {"connections": items}


@router.get("/settings/connection_templates")
def list_connection_templates():
    """Return safe, non-secret templates for common compatible providers."""
    from butly_core.llm.provider_catalog import list_provider_templates

    return {
        "templates": [
            template.to_dict() for template in list_provider_templates()
        ]
    }


@router.post("/settings/connections")
def add_connection(payload: ConnectionPayload):
    """user 定義 Connection を追加する。built-in id は拒否。"""
    from butly_core.llm.connections import (
        Connection,
        is_builtin_connection,
        register_connection,
    )

    if is_builtin_connection(payload.id):
        raise HTTPException(
            status_code=400,
            detail=f"built-in connection は上書きできません: {payload.id}",
        )
    if payload.protocol not in ("openai_compat", "gemini_native"):
        raise HTTPException(
            status_code=400,
            detail=f"未対応の protocol: {payload.protocol}",
        )

    # registry に反映 (即時)
    try:
        conn = Connection(
            id=payload.id,
            protocol=payload.protocol,
            base_url=payload.base_url,
            base_url_env=payload.base_url_env,
            api_key_env=payload.api_key_env,
            api_key_fallback_envs=tuple(payload.api_key_fallback_envs or ()),
            label=payload.label,
            extra_headers=dict(payload.extra_headers or {}),
            embeddings_supported=payload.embeddings_supported,
            embedding_model_env=payload.embedding_model_env,
            default_embedding_model=payload.default_embedding_model,
            model_name_strip_prefix=payload.model_name_strip_prefix,
        )
        register_connection(conn, overwrite_user=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Connection 登録失敗: {e}")

    # user_config.json にも永続化
    cfg = _load_user_config()
    existing = cfg.get("LLM_CONNECTIONS", []) or []
    if not isinstance(existing, list):
        existing = []
    entry = payload.model_dump(mode="json", exclude_none=False)
    # 既存 id は置換、なければ追加
    replaced = False
    for i, e in enumerate(existing):
        if isinstance(e, dict) and e.get("id") == payload.id:
            existing[i] = entry
            replaced = True
            break
    if not replaced:
        existing.append(entry)
    cfg["LLM_CONNECTIONS"] = existing
    _save_user_config(cfg)
    _invalidate_model_catalog(payload.id)

    return {
        "message": f"Connection {payload.id!r} を登録しました",
        "connection": _connection_to_dict(conn),
    }


def _find_connection_references(
    value: Any,
    connection_id: str,
    prefix: str,
) -> list[str]:
    references: list[str] = []
    if isinstance(value, dict):
        if value.get("connection") == connection_id:
            references.append(prefix)
        for key, child in value.items():
            references.extend(
                _find_connection_references(
                    child,
                    connection_id,
                    f"{prefix}.{key}",
                )
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            references.extend(
                _find_connection_references(
                    child,
                    connection_id,
                    f"{prefix}[{index}]",
                )
            )
    return references


def _connection_references(connection_id: str) -> list[str]:
    references = _find_connection_references(
        AI_CONFIG,
        connection_id,
        "AI_CONFIG",
    )

    instances_dir = deps.INSTANCES_DIR
    if instances_dir is None or not instances_dir.exists():
        return sorted(set(references))

    for config_path in sorted(instances_dir.glob("*/config.json")):
        try:
            instance_config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        references.extend(
            _find_connection_references(
                instance_config,
                connection_id,
                f"instance:{config_path.parent.name}",
            )
        )
    return sorted(set(references))


@router.delete("/settings/connections/{connection_id}")
def delete_connection(connection_id: str, force: bool = False):
    """user 定義 Connection を削除する。built-in は不可。"""
    from butly_core.llm.connections import get_registry, is_builtin_connection

    if is_builtin_connection(connection_id):
        raise HTTPException(
            status_code=400,
            detail=f"built-in connection は削除できません: {connection_id}",
        )
    reg = get_registry()
    if reg.get(connection_id) is None:
        raise HTTPException(
            status_code=404, detail=f"Connection 未登録: {connection_id}"
        )
    references = _connection_references(connection_id)
    if references and not force:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"Connection {connection_id!r} はモデル設定から参照中です"
                ),
                "references": references,
            },
        )

    # user_config.json から除去
    cfg = _load_user_config()
    existing = cfg.get("LLM_CONNECTIONS", []) or []
    if isinstance(existing, list):
        cfg["LLM_CONNECTIONS"] = [
            e
            for e in existing
            if not (isinstance(e, dict) and e.get("id") == connection_id)
        ]
        _save_user_config(cfg)
    reg.unregister(connection_id)
    _invalidate_model_catalog(connection_id)
    return {"message": f"Connection {connection_id!r} を削除しました"}


def _require_connection(connection_id: str):
    from butly_core.llm.connections import try_get_connection

    conn = try_get_connection(connection_id)
    if conn is None:
        raise HTTPException(
            status_code=404,
            detail=f"Connection 未登録: {connection_id}",
        )
    return conn


def _connection_api_key_envs(conn) -> tuple[str, ...]:
    env_names: list[str] = []
    if conn.api_key_env:
        env_names.append(conn.api_key_env)
    env_names.extend(conn.api_key_fallback_envs)
    return tuple(dict.fromkeys(env_names))


def _connections_using_env_names(env_names: tuple[str, ...]) -> list[str]:
    from butly_core.llm.connections import list_connections

    target = set(env_names)
    affected = []
    for conn in list_connections():
        if target.intersection(_connection_api_key_envs(conn)):
            affected.append(conn.id)
    return affected


@router.post("/settings/connections/{connection_id}/api_key")
def set_connection_api_key(
    connection_id: str,
    request: ConnectionApiKeyRequest,
):
    """Persist a Connection API key without accepting an env name from UI."""
    conn = _require_connection(connection_id)
    if not conn.api_key_env:
        raise HTTPException(
            status_code=400,
            detail=f"Connection {connection_id!r} はAPIキーを使用しません",
        )

    key = _validate_api_key_value(request.api_key)
    upsert_env_var(_env_file_path(), conn.api_key_env, key)
    os.environ[conn.api_key_env] = key
    affected = _connections_using_env_names((conn.api_key_env,))
    for affected_connection in affected:
        _invalidate_model_catalog(affected_connection)
    return {
        "message": f"Connection {connection_id!r} のAPIキーを保存しました",
        "api_key_set": True,
        "affected_connections": affected,
    }


@router.delete("/settings/connections/{connection_id}/api_key")
def delete_connection_api_key(connection_id: str):
    """Remove a Connection's primary and fallback API-key env values."""
    conn = _require_connection(connection_id)
    env_names = _connection_api_key_envs(conn)
    if not env_names:
        raise HTTPException(
            status_code=400,
            detail=f"Connection {connection_id!r} はAPIキーを使用しません",
        )

    remove_env_vars(_env_file_path(), env_names)
    for env_name in env_names:
        os.environ.pop(env_name, None)
    affected = _connections_using_env_names(env_names)
    for affected_connection in affected:
        _invalidate_model_catalog(affected_connection)
    return {
        "message": f"Connection {connection_id!r} のAPIキーを削除しました",
        "api_key_set": False,
        "affected_connections": affected,
    }


@router.post("/settings/test_connection")
def test_connection(connection_id: str = Body(..., embed=True)):
    """指定 Connection の /models を叩いて疎通確認する。"""
    from butly_core.llm.connections import try_get_connection

    conn = try_get_connection(connection_id)
    if conn is None:
        raise HTTPException(
            status_code=404, detail=f"Connection 未登録: {connection_id}"
        )

    if conn.protocol == "gemini_native":
        # Gemini は SDK 経由で list_models
        try:
            from google import genai

            api_key = conn.resolve_api_key()
            if not api_key:
                return {"status": "error", "message": "API キー未設定", "models": []}
            client = genai.Client(api_key=api_key)
            models = []
            for m in client.models.list():
                mid = m.name
                if mid and mid.startswith("models/"):
                    mid = mid[len("models/") :]
                if mid:
                    models.append(mid)
                if len(models) >= 50:
                    break
            return {"status": "ok", "models": models}
        except Exception as e:
            return {"status": "error", "message": str(e), "models": []}

    # openai_compat: /models を直叩き
    import urllib.request

    base_url = conn.resolve_base_url()
    if not base_url:
        return {"status": "error", "message": "base_url 未設定", "models": []}
    url = base_url.rstrip("/") + "/models"
    headers = {"Accept": "application/json"}
    api_key = conn.resolve_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if conn.extra_headers:
        for k, v in conn.extra_headers.items():
            headers[k] = v

    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        models = []
        for m in data.get("data") or []:
            mid = m.get("id") if isinstance(m, dict) else None
            if mid:
                models.append(mid)
        return {"status": "ok", "models": models}
    except Exception as e:
        return {"status": "error", "message": str(e), "models": []}


@router.post("/settings/model_catalog/refresh")
def refresh_model_catalog(
    connection_id: str | None = Body(default=None, embed=True),
):
    """Invalidate cached provider model lists.

    The next ``model_candidates`` request performs discovery again. Omitting
    ``connection_id`` refreshes every Connection.
    """
    if connection_id is not None:
        _require_connection(connection_id)
    _invalidate_model_catalog(connection_id)
    return {
        "message": (
            f"Connection {connection_id!r} のモデル一覧を更新します"
            if connection_id
            else "全Connectionのモデル一覧を更新します"
        ),
        "connection_id": connection_id,
    }


@router.get("/settings/model_candidates")
def model_candidates(
    role: str,
    include_deprecated: bool = False,
    connection_id: str | None = None,
):
    """role に対するモデル候補を返す。

    内訳:
      - MODEL_PRESETS から role を満たすもの
      - 現在 AI_CONFIG に保存されている model_name (preset に無くても入れる)
      - 利用可能 Connection で /models が成功したものの動的取得結果
        (Ollama / Groq 等)
    """
    from butly_core.llm.model_registry import (
        get_presets_for_role,
        find_preset,
        RoleId,
    )
    from butly_core.llm.connections import list_connections, is_builtin_connection

    role_lower = role.lower().strip()
    valid_roles = (
        "chat",
        "summary",
        "gatekeeper",
        "knowledge",
        "embedding",
        "context_classifier",
    )
    if role_lower not in valid_roles:
        raise HTTPException(status_code=400, detail=f"未対応の role: {role}")
    if connection_id is not None:
        _require_connection(connection_id)

    candidates: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _push(connection_id: str, model_name: str, *, available: bool = True):
        key = (connection_id, model_name)
        if key in seen:
            return
        seen.add(key)
        preset = find_preset(connection_id, model_name)
        candidates.append(
            {
                "connection_id": connection_id,
                "model_name": model_name,
                "label": preset.label if preset else model_name,
                "available": available,
                "deprecated": preset.deprecated if preset else False,
                "preview": preset.preview if preset else False,
                "replacement": preset.replacement if preset else None,
                "capabilities": list(preset.capabilities) if preset else [],
                "source": "preset" if preset else "dynamic",
                "is_builtin_connection": is_builtin_connection(connection_id),
            }
        )

    def _dynamic_model_matches_role(role_id: str, model_name: str) -> bool:
        """Best-effort role filter for provider-discovered model IDs."""
        lower = model_name.lower()
        is_embedding_name = "embed" in lower or "embedding" in lower

        if role_id == "embedding":
            return is_embedding_name
        if is_embedding_name:
            return False
        return True

    # 1. preset
    for p in get_presets_for_role(role_lower, include_deprecated=include_deprecated):
        _push(p.connection_id, p.model_name, available=True)

    # 2. 現在 AI_CONFIG に保存されているもの
    saved = (
        AI_CONFIG.get(role_lower, {})
        if isinstance(AI_CONFIG.get(role_lower), dict)
        else {}
    )
    saved_model = saved.get("model_name") if saved else None
    saved_connection = saved.get("connection") if saved else None
    if saved_model and saved_connection:
        _push(saved_connection, saved_model, available=True)

    # 3. Connection単位でキャッシュした動的一覧をrole別に絞り込む。
    #    roleごとに外部 /models を再取得しないことが重要。
    for conn in list_connections():
        if connection_id is not None and conn.id != connection_id:
            continue
        # auth が必要なのに env 未設定なら skip
        if conn.api_key_env and not conn.resolve_api_key():
            continue
        # embedding role には embeddings_supported=True の connection だけ
        if role_lower == "embedding" and not conn.embeddings_supported:
            continue

        for model_id in _connection_model_catalog(conn):
            if _dynamic_model_matches_role(role_lower, model_id):
                _push(conn.id, model_id, available=True)

    return {
        "role": role_lower,
        "candidates": candidates,
        "catalog_ttl_seconds": _MODEL_CATALOG_TTL_SECONDS,
    }
