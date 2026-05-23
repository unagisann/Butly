"""
model_registry.py
-----------------
モデルプリセットと ModelRef (Connection + ModelName) の管理。

責務:
  - Butly がサポートする「役割 (chat/summary/...) ごとに薦めるモデル」を一元管理
  - 文字列 ``model_name`` から connection を推定 (``infer_connection_id``)
  - role 別フィルタリング (``get_presets_for_role``)
  - 旧形式 (model_name only) と新形式 (connection + model_name) の正規化

config.py は import しない (循環防止)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from butly_core.llm.connections import (
    Connection,
    get_connection,
    is_builtin_connection,
    list_connections,
    try_get_connection,
)


RoleId = Literal[
    "chat",
    "summary",
    "gatekeeper",
    "knowledge",
    "embedding",
    "context_classifier",
]

Capability = Literal[
    "chat",
    "embedding",
    "vision",
    "tool_calling",
    "structured_outputs",
    "reasoning",
    "web_search",
]


# =====================================================================
# ModelRef / ModelPreset
# =====================================================================

@dataclass(frozen=True)
class ModelRef:
    """Role が指す具体的なモデルへの参照。

    connection_id + model_name の組で一意。
    """

    connection_id: str
    model_name: str

    def to_dict(self) -> dict[str, str]:
        return {"connection": self.connection_id, "model_name": self.model_name}


@dataclass(frozen=True)
class ModelPreset:
    """Butly 側で持つ「推奨モデル」エントリ。"""

    connection_id: str
    model_name: str
    roles: tuple[RoleId, ...]
    capabilities: tuple[Capability, ...]
    label: str
    stable: bool = True
    preview: bool = False
    deprecated: bool = False
    replacement: Optional[str] = None
    default_params: dict[str, Any] = field(default_factory=dict)

    def ref(self) -> ModelRef:
        return ModelRef(connection_id=self.connection_id, model_name=self.model_name)


# =====================================================================
# Provider prefix 推定 (旧 model_name only config 互換用)
# =====================================================================

# 順序が意味を持つ: 上から順に prefix match で判定する。
# embedding 系 OpenAI モデル (text-embedding-*) は OpenAI に倒す。
_BUILTIN_PREFIX_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("google", ("gemini", "models/gemini")),
    ("openai", ("gpt-", "o1", "o3", "o4", "text-embedding-")),
    ("xai", ("grok-", "xai/")),
    ("ollama", ("ollama/",)),
)


def infer_connection_id(model_name: str) -> Optional[str]:
    """モデル名 prefix から built-in connection を推定する。

    マッチしなければ None。user 定義 connection (Groq 等) は
    モデル名から推定できないので、必ず ``connection`` を明示すること。
    """
    if not model_name:
        return None
    lower = model_name.lower()
    for connection_id, prefixes in _BUILTIN_PREFIX_RULES:
        for p in prefixes:
            if lower.startswith(p):
                return connection_id
    return None


# =====================================================================
# Normalize / Resolve
# =====================================================================

def normalize_model_ref(
    raw: Any,
    *,
    default_connection_id: Optional[str] = None,
) -> ModelRef:
    """色々な形式の入力を ModelRef に正規化する。

    受け付ける形式:
      - ModelRef (そのまま)
      - {"connection": "openai", "model_name": "gpt-4o"} (新形式)
      - {"model_name": "gpt-4o"} (旧形式 — connection は推定)
      - "gpt-4o" (旧形式 string)

    Raises
    ------
    ValueError
      model_name が空 / connection 推定不能で default_connection_id も無い場合
    """
    if isinstance(raw, ModelRef):
        return raw

    if isinstance(raw, dict):
        # 新形式 / 旧形式 dict 両対応
        model_name = (raw.get("model_name") or raw.get("model") or "").strip()
        connection_id = (raw.get("connection") or raw.get("connection_id") or "").strip() or None
    else:
        model_name = str(raw or "").strip()
        connection_id = None

    if not model_name:
        raise ValueError("model_name is empty")

    if not connection_id:
        connection_id = infer_connection_id(model_name) or default_connection_id

    if not connection_id:
        raise ValueError(
            f"Cannot infer connection for model_name={model_name!r}. "
            f"Specify 'connection' explicitly."
        )

    return ModelRef(connection_id=connection_id, model_name=model_name)


def resolve_role_model_ref(
    role_config: dict,
    fallback_model_name: Optional[str] = None,
    fallback_connection_id: Optional[str] = None,
) -> ModelRef:
    """``AI_CONFIG[role]`` 形式の dict から ModelRef を解決する。

    優先順位 (connection):
      1. role_config["connection"] (明示) — 最優先
      2. role_config["model_name"] からの prefix 推定
      3. fallback_connection_id

    優先順位 (model_name):
      1. role_config["model_name"]
      2. fallback_model_name
    """
    model_name = (role_config or {}).get("model_name") or fallback_model_name
    explicit_connection_id = (role_config or {}).get("connection")

    if not model_name:
        raise ValueError("model_name not found in role_config nor fallback")

    # 明示があれば fallback より優先。
    # 明示がなければ normalize_model_ref が prefix 推定し、それでも駄目なら fallback。
    return normalize_model_ref(
        {"connection": explicit_connection_id, "model_name": model_name},
        default_connection_id=fallback_connection_id,
    )


# =====================================================================
# MODEL_PRESETS (静的に推奨するモデル群)
# =====================================================================

# 2026-05 時点の公開モデル準拠。
# - stable: 安定リリース。新規ユーザーはこれを使う想定
# - preview: experimental。stable 移行待ち
# - deprecated: 引き続き動くが代替を案内
MODEL_PRESETS: tuple[ModelPreset, ...] = (
    # ---------- Google / Gemini ----------
    ModelPreset(
        connection_id="google",
        model_name="gemini-3.5-flash",
        roles=("chat", "summary", "knowledge"),
        capabilities=("chat", "vision", "tool_calling", "structured_outputs", "reasoning"),
        label="Gemini 3.5 Flash",
        stable=True,
    ),
    ModelPreset(
        connection_id="google",
        model_name="gemini-3.1-pro-preview",
        roles=("chat", "knowledge"),
        capabilities=("chat", "vision", "tool_calling", "structured_outputs", "reasoning"),
        label="Gemini 3.1 Pro (Preview)",
        stable=False,
        preview=True,
    ),
    ModelPreset(
        connection_id="google",
        model_name="gemini-3.1-flash-lite",
        roles=("summary", "gatekeeper", "context_classifier", "chat"),
        capabilities=("chat", "tool_calling", "structured_outputs"),
        label="Gemini 3.1 Flash-Lite",
        stable=True,
    ),
    ModelPreset(
        connection_id="google",
        model_name="gemini-2.5-pro",
        roles=("chat", "knowledge"),
        capabilities=("chat", "vision", "tool_calling", "structured_outputs", "reasoning"),
        label="Gemini 2.5 Pro",
        stable=True,
    ),
    ModelPreset(
        connection_id="google",
        model_name="gemini-2.5-flash",
        roles=("chat", "summary", "gatekeeper", "context_classifier"),
        capabilities=("chat", "vision", "tool_calling", "structured_outputs"),
        label="Gemini 2.5 Flash",
        stable=True,
    ),
    ModelPreset(
        connection_id="google",
        model_name="gemini-2.5-flash-lite",
        roles=("summary", "gatekeeper", "context_classifier"),
        capabilities=("chat", "tool_calling", "structured_outputs"),
        label="Gemini 2.5 Flash-Lite",
        stable=True,
    ),
    ModelPreset(
        connection_id="google",
        model_name="gemini-embedding-2",
        roles=("embedding",),
        capabilities=("embedding",),
        label="Gemini Embedding 2",
        stable=True,
    ),

    # ---------- OpenAI ----------
    ModelPreset(
        connection_id="openai",
        model_name="gpt-4o",
        roles=("chat", "knowledge"),
        capabilities=("chat", "vision", "tool_calling", "structured_outputs"),
        label="GPT-4o",
        stable=True,
    ),
    ModelPreset(
        connection_id="openai",
        model_name="gpt-4o-mini",
        roles=("chat", "summary", "gatekeeper", "context_classifier"),
        capabilities=("chat", "vision", "tool_calling", "structured_outputs"),
        label="GPT-4o mini",
        stable=True,
    ),
    ModelPreset(
        connection_id="openai",
        model_name="o3",
        roles=("chat", "knowledge"),
        capabilities=("chat", "vision", "tool_calling", "structured_outputs", "reasoning"),
        label="o3 (reasoning)",
        stable=True,
    ),
    ModelPreset(
        connection_id="openai",
        model_name="o4-mini",
        roles=("chat", "summary", "gatekeeper"),
        capabilities=("chat", "vision", "tool_calling", "structured_outputs", "reasoning"),
        label="o4-mini (reasoning)",
        stable=True,
    ),
    ModelPreset(
        connection_id="openai",
        model_name="text-embedding-3-small",
        roles=("embedding",),
        capabilities=("embedding",),
        label="text-embedding-3-small",
        stable=True,
    ),
    ModelPreset(
        connection_id="openai",
        model_name="text-embedding-3-large",
        roles=("embedding",),
        capabilities=("embedding",),
        label="text-embedding-3-large",
        stable=True,
    ),

    # ---------- xAI ----------
    ModelPreset(
        connection_id="xai",
        model_name="grok-4.20-0309-reasoning",
        roles=("chat", "knowledge"),
        capabilities=("chat", "vision", "tool_calling", "structured_outputs", "reasoning"),
        label="Grok 4.20 Reasoning",
        stable=True,
    ),
    ModelPreset(
        connection_id="xai",
        model_name="grok-4.20-0309-non-reasoning",
        roles=("chat", "summary", "gatekeeper", "context_classifier"),
        capabilities=("chat", "vision", "tool_calling", "structured_outputs"),
        label="Grok 4.20 Non-Reasoning",
        stable=True,
    ),
    # 後継案内付き deprecated。selector からは除外 (include_deprecated=True で出る)。
    ModelPreset(
        connection_id="xai",
        model_name="grok-4-1-fast-reasoning",
        roles=("chat", "knowledge"),
        capabilities=("chat", "reasoning"),
        label="Grok 4.1 Fast Reasoning (deprecated)",
        stable=False,
        deprecated=True,
        replacement="grok-4.20-0309-reasoning",
    ),
    ModelPreset(
        connection_id="xai",
        model_name="grok-4-1-fast-non-reasoning",
        roles=("chat", "summary", "gatekeeper", "context_classifier"),
        capabilities=("chat",),
        label="Grok 4.1 Fast Non-Reasoning (deprecated)",
        stable=False,
        deprecated=True,
        replacement="grok-4.20-0309-non-reasoning",
    ),
)


# =====================================================================
# Lookup helpers
# =====================================================================

def get_presets_for_role(
    role: RoleId,
    *,
    include_deprecated: bool = False,
) -> list[ModelPreset]:
    """role に対応する preset を返す (新→旧、stable→preview→deprecated)。"""
    items = [p for p in MODEL_PRESETS if role in p.roles]
    if not include_deprecated:
        items = [p for p in items if not p.deprecated]
    # ソート: stable 優先 → 非 preview 優先 → 元の順序維持
    items.sort(key=lambda p: (not p.stable, p.preview, p.deprecated))
    return items


def find_preset(connection_id: str, model_name: str) -> Optional[ModelPreset]:
    """connection_id + model_name で preset を引く。未登録は None。"""
    for p in MODEL_PRESETS:
        if p.connection_id == connection_id and p.model_name == model_name:
            return p
    return None


def is_deprecated(connection_id: str, model_name: str) -> bool:
    p = find_preset(connection_id, model_name)
    return bool(p and p.deprecated)


def get_replacement(connection_id: str, model_name: str) -> Optional[str]:
    p = find_preset(connection_id, model_name)
    return p.replacement if (p and p.deprecated) else None


__all__ = [
    "ModelRef",
    "ModelPreset",
    "MODEL_PRESETS",
    "RoleId",
    "Capability",
    "infer_connection_id",
    "normalize_model_ref",
    "resolve_role_model_ref",
    "get_presets_for_role",
    "find_preset",
    "is_deprecated",
    "get_replacement",
]
