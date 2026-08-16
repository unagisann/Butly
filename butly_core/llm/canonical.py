"""Provider に依存しない LLM 生成リクエスト。

Core / 評価コードが OpenAI の ``max_tokens`` や Gemini の
``max_output_tokens`` を直接選ばないための、小さな正規化境界を提供する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Union

from butly_core.llm.model_registry import ModelRef


class _UnsetType:
    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


UNSET = _UnsetType()
UnsetOr = Union[_UnsetType, Any]


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ImagePart:
    mime_type: str
    data_base64: str
    name: Optional[str] = None


CanonicalPart = Union[TextPart, ImagePart]


@dataclass(frozen=True)
class CanonicalMessage:
    role: Literal["system", "user", "assistant"]
    parts: tuple[CanonicalPart, ...]

    @classmethod
    def text(cls, role: str, text: str) -> "CanonicalMessage":
        normalized = "assistant" if role == "model" else role
        if normalized not in {"system", "user", "assistant"}:
            normalized = "user"
        return cls(role=normalized, parts=(TextPart(str(text)),))


@dataclass(frozen=True)
class CanonicalGenerationOptions:
    temperature: UnsetOr = UNSET
    top_p: UnsetOr = UNSET
    top_k: UnsetOr = UNSET
    max_output_tokens: UnsetOr = UNSET
    reasoning_effort: UnsetOr = UNSET


@dataclass(frozen=True)
class CanonicalGenerationRequest:
    model: ModelRef
    messages: tuple[CanonicalMessage, ...]
    options: CanonicalGenerationOptions = field(
        default_factory=CanonicalGenerationOptions
    )
    response_format: UnsetOr = UNSET
    safety_settings: UnsetOr = UNSET
    tools: UnsetOr = UNSET
    stream: bool = False
    purpose: str = "chat"
    reasoning_effort_policy: Literal[
        "provider_default", "medium_if_supported"
    ] = "provider_default"
    explicit_parameters: frozenset[str] = field(default_factory=frozenset)

    def is_explicit(self, parameter: str) -> bool:
        return parameter in self.explicit_parameters


def _first_present(
    generation: dict[str, Any],
    config: dict[str, Any],
    key: str,
    *,
    default: UnsetOr = UNSET,
) -> tuple[UnsetOr, bool]:
    if key in generation:
        return generation[key], True
    if key in config:
        return config[key], True
    return default, False


def request_from_config(
    *,
    model: ModelRef,
    messages: list[CanonicalMessage] | tuple[CanonicalMessage, ...],
    config: Optional[dict[str, Any]] = None,
    default_temperature: UnsetOr = UNSET,
    default_max_output_tokens: UnsetOr = UNSET,
    stream: bool = False,
    purpose: str = "chat",
    reasoning_effort_policy: str = "provider_default",
) -> CanonicalGenerationRequest:
    """既存 role config を Canonical Request へ正規化する。

    ``explicit_parameters`` はユーザー・role設定由来か、呼び出し側の既定値かを
    区別する。Adapter はこの情報を使い、明示指定を黙って捨てない。
    """

    raw = dict(config or {})
    generation = dict(raw.get("generation_config") or {})
    explicit: set[str] = set()

    temperature, is_explicit = _first_present(
        generation,
        raw,
        "temperature",
        default=default_temperature,
    )
    if is_explicit:
        explicit.add("temperature")

    top_p, is_explicit = _first_present(generation, raw, "top_p")
    if is_explicit:
        explicit.add("top_p")

    top_k, is_explicit = _first_present(generation, raw, "top_k")
    if is_explicit:
        explicit.add("top_k")

    max_output_tokens, is_explicit = _first_present(
        generation,
        raw,
        "max_output_tokens",
        default=default_max_output_tokens,
    )
    if is_explicit:
        explicit.add("max_output_tokens")

    reasoning_effort, is_explicit = _first_present(
        generation,
        raw,
        "reasoning_effort",
    )
    if is_explicit:
        explicit.add("reasoning_effort")

    response_format = raw.get("response_format", UNSET)
    if response_format is not UNSET:
        explicit.add("response_format")

    safety_settings = raw.get("safety_settings", UNSET)
    tools = raw.get("tools", UNSET)

    normalized_policy = (
        "medium_if_supported"
        if reasoning_effort_policy == "medium_if_supported"
        else "provider_default"
    )
    return CanonicalGenerationRequest(
        model=model,
        messages=tuple(messages),
        options=CanonicalGenerationOptions(
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
        ),
        response_format=response_format,
        safety_settings=safety_settings,
        tools=tools,
        stream=stream,
        purpose=purpose,
        reasoning_effort_policy=normalized_policy,
        explicit_parameters=frozenset(explicit),
    )


__all__ = [
    "CanonicalGenerationOptions",
    "CanonicalGenerationRequest",
    "CanonicalMessage",
    "CanonicalPart",
    "ImagePart",
    "TextPart",
    "UNSET",
    "request_from_config",
]
