"""Canonical Request と OpenAI Chat Completions payload の相互変換。"""

from __future__ import annotations

import re
from typing import Any, Optional

from butly_core.llm.canonical import (
    CanonicalGenerationRequest,
    CanonicalMessage,
    ImagePart,
    TextPart,
    UNSET,
)
from butly_core.llm.capabilities import ModelCapabilities

_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>[^;,]+);base64,(?P<data>.*)$",
    flags=re.DOTALL,
)


class CanonicalParameterError(ValueError):
    """Canonical parameterをProvider契約へ安全に写像できない。"""


def canonical_messages_from_openai(
    messages: list[dict[str, Any]],
) -> list[CanonicalMessage]:
    canonical: list[CanonicalMessage] = []
    for message in messages:
        role = message.get("role", "user")
        normalized_role = "assistant" if role == "model" else role
        if normalized_role not in {"system", "user", "assistant"}:
            normalized_role = "user"
        content = message.get("content", "")
        parts: list[Any] = []
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    parts.append(TextPart(str(part)))
                    continue
                if part.get("type") == "text":
                    parts.append(TextPart(str(part.get("text") or "")))
                    continue
                if part.get("type") == "image_url":
                    image_url = part.get("image_url") or {}
                    url = (
                        image_url.get("url")
                        if isinstance(image_url, dict)
                        else None
                    )
                    matched = _DATA_URL_RE.match(str(url or ""))
                    if matched:
                        parts.append(
                            ImagePart(
                                mime_type=matched.group("mime"),
                                data_base64=matched.group("data"),
                            )
                        )
                        continue
                parts.append(TextPart(str(part)))
        else:
            parts.append(TextPart(str(content or "")))
        canonical.append(
            CanonicalMessage(role=normalized_role, parts=tuple(parts))
        )
    return canonical


def canonical_messages_to_openai(
    messages: tuple[CanonicalMessage, ...],
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in messages:
        if len(message.parts) == 1 and isinstance(message.parts[0], TextPart):
            content: Any = message.parts[0].text
        else:
            content = []
            for part in message.parts:
                if isinstance(part, TextPart):
                    content.append({"type": "text", "text": part.text})
                elif isinstance(part, ImagePart):
                    content.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": (
                                    f"data:{part.mime_type};base64,"
                                    f"{part.data_base64}"
                                )
                            },
                        }
                    )
        converted.append({"role": message.role, "content": content})
    return converted


class OpenAIChatCompletionsRequestAdapter:
    """Capabilityを使ってOpenAI互換payloadを構築する。"""

    def build_kwargs(
        self,
        request: CanonicalGenerationRequest,
        capabilities: ModelCapabilities,
    ) -> dict[str, Any]:
        options = request.options
        kwargs: dict[str, Any] = {
            "messages": canonical_messages_to_openai(request.messages),
        }

        if (
            options.max_output_tokens is not UNSET
            and options.max_output_tokens
        ):
            token_parameter = (
                capabilities.token_limit_parameter or "max_tokens"
            )
            kwargs[token_parameter] = options.max_output_tokens

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
            raise CanonicalParameterError(
                "reasoning_effort was explicitly configured for a model that "
                "does not advertise reasoning support"
            )

        if reasoning_effort is not UNSET:
            supported_efforts = capabilities.reasoning_efforts
            if (
                supported_efforts
                and str(reasoning_effort) not in supported_efforts
            ):
                raise CanonicalParameterError(
                    f"reasoning_effort={reasoning_effort!r} is unsupported; "
                    f"supported={list(supported_efforts)!r}"
                )
            kwargs["reasoning_effort"] = reasoning_effort

        if capabilities.temperature_supported is not False:
            if options.temperature is not UNSET and (
                capabilities.temperature_supported is True
                or request.is_explicit("temperature")
            ):
                kwargs["temperature"] = options.temperature
            if options.top_p is not UNSET and (
                capabilities.temperature_supported is True
                or request.is_explicit("top_p")
            ):
                kwargs["top_p"] = options.top_p

        if request.response_format is not UNSET:
            if capabilities.structured_outputs_supported is False:
                raise CanonicalParameterError(
                    "response_format was configured for a model that does not "
                    "advertise structured-output support"
                )
            if not isinstance(request.response_format, dict):
                raise CanonicalParameterError(
                    "response_format must be a mapping"
                )
            kwargs["response_format"] = request.response_format

        if request.tools is not UNSET and request.tools is not None:
            kwargs["tools"] = request.tools
        if request.stream:
            kwargs["stream"] = True
        return kwargs

    def correction_for_error(
        self,
        error: Exception,
        request: CanonicalGenerationRequest,
    ) -> Optional[ModelCapabilities]:
        parsed = _unsupported_parameter(error)
        if parsed is None:
            return None
        parameter, message = parsed

        if parameter == "max_tokens" and "max_completion_tokens" in message:
            return ModelCapabilities(
                token_limit_parameter="max_completion_tokens",
                temperature_supported=(
                    None
                    if request.is_explicit("temperature")
                    else False
                ),
                source=("unsupported_parameter_correction",),
            )
        if (
            parameter == "max_completion_tokens"
            and "max_tokens" in message
        ):
            return ModelCapabilities(
                token_limit_parameter="max_tokens",
                source=("unsupported_parameter_correction",),
            )
        if (
            parameter == "temperature"
            and not request.is_explicit("temperature")
        ):
            return ModelCapabilities(
                temperature_supported=False,
                source=("unsupported_parameter_correction",),
            )
        if (
            parameter == "reasoning_effort"
            and not request.is_explicit("reasoning_effort")
        ):
            return ModelCapabilities(
                supports_reasoning=False,
                source=("unsupported_parameter_correction",),
            )
        return None


def _unsupported_parameter(error: Exception) -> Optional[tuple[str, str]]:
    status_code = getattr(error, "status_code", None)
    body = getattr(error, "body", None)
    response = getattr(error, "response", None)
    status_code = status_code or getattr(response, "status_code", None)

    detail: Any = body
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        detail = body["error"]
    if not isinstance(detail, dict):
        detail = {}

    code = detail.get("code")
    error_type = detail.get("type")
    parameter = detail.get("param")
    message = str(detail.get("message") or str(error))

    # 文字列が偶然 "unsupported parameter" を含むローカル例外は補正しない。
    # Providerが400として明示した契約エラーだけを対象にする。
    if status_code != 400:
        return None
    if code not in {"unsupported_parameter", "unknown_parameter", None}:
        return None
    if error_type not in {"invalid_request_error", None}:
        return None

    if not parameter:
        matched = re.search(
            r"(?:Unsupported|Unknown) parameter:\s*['\"]([^'\"]+)['\"]",
            message,
            flags=re.IGNORECASE,
        )
        if matched:
            parameter = matched.group(1)
    if not isinstance(parameter, str) or not parameter:
        return None
    if (
        "unsupported" not in message.lower()
        and "unknown parameter" not in message.lower()
    ):
        if code not in {"unsupported_parameter", "unknown_parameter"}:
            return None
    return parameter, message


def is_unsupported_parameter_error(error: Exception) -> bool:
    """Provider が明示した unsupported/unknown parameter の 400 か。"""
    return _unsupported_parameter(error) is not None


__all__ = [
    "CanonicalParameterError",
    "OpenAIChatCompletionsRequestAdapter",
    "canonical_messages_from_openai",
    "canonical_messages_to_openai",
    "is_unsupported_parameter_error",
]
