"""
connections.py
--------------
LLM Connection registry。

Connection は「どこに繋ぐか」「どの protocol で喋るか」「どの auth env を見るか」
を表現する一級の値オブジェクト。

- ``openai`` / ``xai`` / ``ollama`` / ``google`` は built-in (常駐)。
- それ以外 (``groq`` 等の OpenAI 互換 provider) は user_config.json の
  ``LLM_CONNECTIONS`` ブロックから register() される (Phase 2 で対応)。

Phase 1 範囲では built-in 4 件のみ。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal, Optional

ProtocolId = Literal["openai_compat", "gemini_native"]


# =====================================================================
# Connection データクラス
# =====================================================================

@dataclass(frozen=True)
class Connection:
    """LLM 接続情報。

    Attributes
    ----------
    id
        Connection 識別子。"openai" / "xai" / "ollama" / "google" / 任意の user 定義。
    protocol
        "openai_compat" or "gemini_native"。Adapter 種別を決定する。
    base_url
        SDK に渡す base URL。None → SDK default を使う。
    base_url_env
        実行時にこの env が設定されていれば base_url を上書きする。
    api_key_env
        API キーが入っている env 変数名。None → auth 不要 (Ollama)。
    api_key_fallback_envs
        api_key_env が未設定の場合に試す代替 env (例: GEMINI → GOOGLE)。
    label
        UI 表示用ラベル。None なら id を使う。
    extra_headers
        SDK 経由で追加するヘッダ (将来 OpenRouter HTTP-Referer 等に使う)。
    embeddings_supported
        False の Connection (xAI) は embed() で常に None を返す。
    embedding_model_env
        embed 時のフォールバック env (例: OPENAI_EMBEDDING_MODEL)。
    default_embedding_model
        embed の最終フォールバック。
    model_name_strip_prefix
        API に渡す前に除去するモデル名 prefix (例: "xai/" / "ollama/")。
    """

    id: str
    protocol: ProtocolId
    base_url: Optional[str] = None
    base_url_env: Optional[str] = None
    api_key_env: Optional[str] = None
    api_key_fallback_envs: tuple[str, ...] = ()
    label: Optional[str] = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    embeddings_supported: bool = True
    embedding_model_env: Optional[str] = None
    default_embedding_model: Optional[str] = None
    model_name_strip_prefix: Optional[str] = None

    @property
    def display_label(self) -> str:
        return self.label or self.id

    def resolve_base_url(self) -> Optional[str]:
        """env > base_url の優先順位で実効 URL を返す。"""
        if self.base_url_env:
            env_val = os.environ.get(self.base_url_env)
            if env_val:
                return env_val
        return self.base_url

    def resolve_api_key(self) -> Optional[str]:
        """env チェーンを辿って api_key を返す。auth 不要 connection は None。"""
        if not self.api_key_env:
            return None
        val = os.environ.get(self.api_key_env)
        if val:
            return val
        for fallback in self.api_key_fallback_envs:
            val = os.environ.get(fallback)
            if val:
                return val
        return None

    def strip_model_prefix(self, model_name: str) -> str:
        """API 渡す前に prefix を除去する。"""
        if self.model_name_strip_prefix and model_name.startswith(self.model_name_strip_prefix):
            return model_name[len(self.model_name_strip_prefix):]
        return model_name


# =====================================================================
# Built-in connections
# =====================================================================

_BUILTIN_CONNECTIONS: tuple[Connection, ...] = (
    Connection(
        id="google",
        protocol="gemini_native",
        api_key_env="GEMINI_API_KEY",
        api_key_fallback_envs=("GOOGLE_API_KEY",),
        label="Google Gemini",
        embeddings_supported=True,
        default_embedding_model="gemini-embedding-2",
    ),
    Connection(
        id="openai",
        protocol="openai_compat",
        base_url="https://api.openai.com/v1",
        base_url_env="OPENAI_BASE_URL",
        api_key_env="OPENAI_API_KEY",
        label="OpenAI",
        embeddings_supported=True,
        embedding_model_env="OPENAI_EMBEDDING_MODEL",
        default_embedding_model="text-embedding-3-small",
    ),
    Connection(
        id="xai",
        protocol="openai_compat",
        base_url="https://api.x.ai/v1",
        base_url_env="XAI_BASE_URL",
        api_key_env="XAI_API_KEY",
        label="xAI Grok",
        embeddings_supported=False,
        model_name_strip_prefix="xai/",
    ),
    Connection(
        id="ollama",
        protocol="openai_compat",
        base_url="http://localhost:11434/v1",
        base_url_env="OLLAMA_BASE_URL",
        api_key_env=None,
        label="Ollama (local)",
        embeddings_supported=True,
        embedding_model_env="OLLAMA_EMBEDDING_MODEL",
        default_embedding_model="nomic-embed-text",
        model_name_strip_prefix="ollama/",
    ),
)


# =====================================================================
# Registry
# =====================================================================

class ConnectionRegistry:
    """Connection レジストリ。

    built-in を初期登録した状態でモジュールロード時に singleton を作る。
    user 定義 connection は ``register()`` で追加する (built-in 上書き不可)。
    """

    def __init__(self) -> None:
        self._builtin_ids: set[str] = {c.id for c in _BUILTIN_CONNECTIONS}
        self._connections: dict[str, Connection] = {c.id: c for c in _BUILTIN_CONNECTIONS}

    def get(self, connection_id: str) -> Optional[Connection]:
        return self._connections.get(connection_id)

    def require(self, connection_id: str) -> Connection:
        c = self.get(connection_id)
        if c is None:
            raise KeyError(f"Unknown connection_id: {connection_id!r}")
        return c

    def list_all(self) -> list[Connection]:
        return list(self._connections.values())

    def list_user_defined(self) -> list[Connection]:
        return [c for cid, c in self._connections.items() if cid not in self._builtin_ids]

    def is_builtin(self, connection_id: str) -> bool:
        return connection_id in self._builtin_ids

    def register(self, conn: Connection, *, overwrite_user: bool = False) -> None:
        """user 定義 connection を追加する。

        built-in id (google/openai/xai/ollama) は上書き不可で ValueError。
        既存の user 定義 id は overwrite_user=True なら上書き。
        """
        if conn.id in self._builtin_ids:
            raise ValueError(f"Cannot overwrite built-in connection: {conn.id!r}")
        if conn.id in self._connections and not overwrite_user:
            raise ValueError(
                f"Connection {conn.id!r} already exists. Use overwrite_user=True to replace."
            )
        self._connections[conn.id] = conn

    def unregister(self, connection_id: str) -> None:
        if connection_id in self._builtin_ids:
            raise ValueError(f"Cannot unregister built-in connection: {connection_id!r}")
        self._connections.pop(connection_id, None)

    def reset_to_builtin(self) -> None:
        """テスト用: built-in 状態に戻す。"""
        self._connections = {c.id: c for c in _BUILTIN_CONNECTIONS}


# =====================================================================
# Module-level singleton + 簡易アクセサ
# =====================================================================

_registry = ConnectionRegistry()


def get_connection(connection_id: str) -> Connection:
    """singleton から Connection を取得。未登録なら KeyError。"""
    return _registry.require(connection_id)


def try_get_connection(connection_id: str) -> Optional[Connection]:
    return _registry.get(connection_id)


def register_connection(conn: Connection, *, overwrite_user: bool = False) -> None:
    _registry.register(conn, overwrite_user=overwrite_user)


def list_connections() -> list[Connection]:
    return _registry.list_all()


def is_builtin_connection(connection_id: str) -> bool:
    return _registry.is_builtin(connection_id)


def get_registry() -> ConnectionRegistry:
    """テスト用: singleton 自体を返す (reset_to_builtin が必要な場合)。"""
    return _registry
