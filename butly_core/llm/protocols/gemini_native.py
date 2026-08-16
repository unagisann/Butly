"""Canonical Request と Google Gemini native SDK payload の変換。"""

from __future__ import annotations

import base64
from typing import Any, Optional

from butly_core.llm.canonical import (
    CanonicalGenerationRequest,
    CanonicalMessage,
    ImagePart,
    TextPart,
    UNSET,
)
from butly_core.llm.capabilities import ModelCapabilities


class GeminiCanonicalParameterError(ValueError):
    """Canonical parameterをGemini SDK契約へ安全に写像できない。"""


def canonical_messages_from_gemini_history(
    history: list[Any],
) -> list[CanonicalMessage]:
    """Butly/Gemini形式のhistoryをCanonical Messageへ変換する。"""
    converted: list[CanonicalMessage] = []
    for item in history:
        if isinstance(item, CanonicalMessage):
            converted.append(item)
            continue
        if isinstance(item, dict):
            role = str(item.get("role") or "user")
            raw_parts = item.get("parts") or []
            converted.append(
                _canonical_history_message(role, raw_parts)
            )
            continue

        role = str(getattr(item, "role", None) or "user")
        raw_parts = getattr(item, "parts", None) or []
        converted.append(_canonical_history_message(role, raw_parts))
    return converted


def _canonical_history_message(
    role: str,
    raw_parts: list[Any],
) -> CanonicalMessage:
    normalized_role = "assistant" if role == "model" else role
    if normalized_role not in {"system", "user", "assistant"}:
        normalized_role = "user"
    parts: list[TextPart] = []
    for part in raw_parts:
        if isinstance(part, dict) and "text" in part:
            text = part.get("text")
        else:
            text = getattr(part, "text", part)
        parts.append(TextPart(str(text or "")))
    if not parts:
        parts.append(TextPart(""))
    return CanonicalMessage(
        role=normalized_role,
        parts=tuple(parts),
    )


class GeminiNativeRequestAdapter:
    """Capabilityを使ってGemini ``GenerateContentConfig`` を構築する。"""

    def build_config_kwargs(
        self,
        request: CanonicalGenerationRequest,
        capabilities: ModelCapabilities,
        *,
        system_instruction: Optional[str] = None,
        tools_override: Any = UNSET,
    ) -> dict[str, Any]:
        from google.genai import types

        options = request.options
        kwargs: dict[str, Any] = {}

        if options.temperature is not UNSET and (
            capabilities.temperature_supported is True
            or request.is_explicit("temperature")
        ):
            kwargs["temperature"] = options.temperature
        if options.top_p is not UNSET:
            kwargs["top_p"] = options.top_p
        if options.top_k is not UNSET:
            kwargs["top_k"] = options.top_k
        if (
            options.max_output_tokens is not UNSET
            and options.max_output_tokens
        ):
            kwargs["max_output_tokens"] = options.max_output_tokens

        if request.safety_settings is not UNSET:
            kwargs["safety_settings"] = request.safety_settings
        tools = request.tools if tools_override is UNSET else tools_override
        if tools is not UNSET and tools is not None:
            kwargs["tools"] = tools
        if system_instruction:
            kwargs["system_instruction"] = system_instruction

        if request.response_format is not UNSET:
            if capabilities.structured_outputs_supported is False:
                raise GeminiCanonicalParameterError(
                    "structured output was configured for a model that does "
                    "not advertise schema support"
                )
            response_format = request.response_format
            if not isinstance(response_format, dict):
                raise GeminiCanonicalParameterError(
                    "response_format must be a mapping"
                )
            response_type = response_format.get("type")
            if response_type == "json_schema":
                json_schema = response_format.get("json_schema")
                if not isinstance(json_schema, dict) or not isinstance(
                    json_schema.get("schema"), dict
                ):
                    raise GeminiCanonicalParameterError(
                        "response_format.json_schema.schema must be a mapping"
                    )
                kwargs["response_mime_type"] = "application/json"
                kwargs["response_json_schema"] = json_schema["schema"]
            elif response_type == "json_object":
                kwargs["response_mime_type"] = "application/json"
            else:
                raise GeminiCanonicalParameterError(
                    "unsupported canonical response_format type: "
                    f"{response_type!r}"
                )

        reasoning_effort = options.reasoning_effort
        if reasoning_effort is UNSET:
            if (
                request.reasoning_effort_policy == "medium_if_supported"
                and capabilities.supports_reasoning is True
            ):
                reasoning_effort = (
                    capabilities.default_reasoning_effort or "medium"
                )
        elif capabilities.supports_reasoning is False:
            raise GeminiCanonicalParameterError(
                "reasoning_effort was explicitly configured for a model that "
                "does not advertise thinking support"
            )

        if reasoning_effort is not UNSET:
            effort = str(reasoning_effort)
            supported = capabilities.reasoning_efforts
            if supported and effort not in supported:
                raise GeminiCanonicalParameterError(
                    f"reasoning_effort={effort!r} is unsupported; "
                    f"supported={list(supported)!r}"
                )
            if effort == "none":
                kwargs["thinking_config"] = types.ThinkingConfig(
                    thinking_budget=0
                )
            elif effort in {"minimal", "low", "medium", "high"}:
                kwargs["thinking_config"] = types.ThinkingConfig(
                    thinking_level=effort.upper()
                )
            else:
                raise GeminiCanonicalParameterError(
                    f"Gemini cannot map reasoning_effort={effort!r}"
                )
        return kwargs

    def build_config(
        self,
        request: CanonicalGenerationRequest,
        capabilities: ModelCapabilities,
        *,
        system_instruction: Optional[str] = None,
        tools_override: Any = UNSET,
    ):
        from google.genai import types

        return types.GenerateContentConfig(
            **self.build_config_kwargs(
                request,
                capabilities,
                system_instruction=system_instruction,
                tools_override=tools_override,
            )
        )

    def split_messages(
        self,
        request: CanonicalGenerationRequest,
    ) -> tuple[str, list[Any]]:
        """system instructionとGemini Content historyへ分離する。"""
        from google.genai import types

        system_texts: list[str] = []
        contents: list[Any] = []
        for message in request.messages:
            if message.role == "system":
                system_texts.extend(
                    part.text
                    for part in message.parts
                    if isinstance(part, TextPart) and part.text
                )
                continue
            parts: list[Any] = []
            for part in message.parts:
                if isinstance(part, TextPart):
                    parts.append(types.Part(text=part.text))
                elif isinstance(part, ImagePart):
                    parts.append(
                        types.Part.from_bytes(
                            data=base64.b64decode(part.data_base64),
                            mime_type=part.mime_type,
                        )
                    )
            contents.append(
                types.Content(
                    role="model" if message.role == "assistant" else "user",
                    parts=parts,
                )
            )
        return "\n\n".join(system_texts), contents

    def build_direct_kwargs(
        self,
        request: CanonicalGenerationRequest,
        capabilities: ModelCapabilities,
    ) -> dict[str, Any]:
        system_instruction, contents = self.split_messages(request)
        return {
            "model": request.model.model_name,
            "contents": contents,
            "config": self.build_config(
                request,
                capabilities,
                system_instruction=system_instruction,
            ),
        }

    def build_chat_kwargs(
        self,
        request: CanonicalGenerationRequest,
        capabilities: ModelCapabilities,
        *,
        tools_override: Any = UNSET,
    ) -> dict[str, Any]:
        system_instruction, history = self.split_messages(request)
        return {
            "model": request.model.model_name,
            "config": self.build_config(
                request,
                capabilities,
                system_instruction=system_instruction,
                tools_override=tools_override,
            ),
            "history": history,
        }


# 外部import互換。実行Providerとしての旧名は残し、新規コードは上の
# GeminiNativeRequestAdapterを利用する。
from butly_core.llm.providers.gemini import GeminiProvider  # noqa: E402


class GeminiNativeAdapter(GeminiProvider):
    def __init__(self, connection=None, default_model_name=None):
        super().__init__(default_model_name=default_model_name)
        if connection is not None:
            self.connection = connection


__all__ = [
    "GeminiCanonicalParameterError",
    "GeminiNativeAdapter",
    "GeminiNativeRequestAdapter",
    "canonical_messages_from_gemini_history",
]
