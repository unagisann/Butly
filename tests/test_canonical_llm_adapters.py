"""Canonical Requestのprotocol別変換テスト。"""

from butly_core.llm.canonical import CanonicalMessage, request_from_config
from butly_core.llm.capabilities import ModelCapabilities
from butly_core.llm.model_registry import ModelRef
from butly_core.llm.protocols.gemini_native import (
    GeminiNativeRequestAdapter,
    canonical_messages_from_gemini_history,
)
from butly_core.llm.protocols.openai_chat import (
    OpenAIChatCompletionsRequestAdapter,
)


def test_default_values_are_not_mistaken_for_user_parameters():
    request = request_from_config(
        model=ModelRef("openai", "m"),
        messages=[CanonicalMessage.text("user", "hello")],
        config={"generation_config": {"max_output_tokens": 100}},
        default_temperature=0.0,
    )

    assert request.is_explicit("max_output_tokens") is True
    assert request.is_explicit("temperature") is False

    kwargs = OpenAIChatCompletionsRequestAdapter().build_kwargs(
        request,
        ModelCapabilities(
            token_limit_parameter="max_completion_tokens",
        ),
    )
    assert kwargs["max_completion_tokens"] == 100
    assert "temperature" not in kwargs


def test_openai_reasoning_policy_uses_capability_default():
    request = request_from_config(
        model=ModelRef("openai", "m"),
        messages=[CanonicalMessage.text("user", "hello")],
        config={},
        default_temperature=0.0,
        reasoning_effort_policy="medium_if_supported",
    )
    kwargs = OpenAIChatCompletionsRequestAdapter().build_kwargs(
        request,
        ModelCapabilities(
            supports_reasoning=True,
            reasoning_efforts=("low", "medium", "high"),
            default_reasoning_effort="medium",
            temperature_supported=False,
        ),
    )

    assert kwargs["reasoning_effort"] == "medium"
    assert "temperature" not in kwargs


def test_gemini_maps_json_schema_and_thinking_level():
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    request = request_from_config(
        model=ModelRef("google", "gemini-test"),
        messages=[
            CanonicalMessage.text("system", "system"),
            CanonicalMessage.text("user", "judge"),
        ],
        config={
            "generation_config": {"max_output_tokens": 512},
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": schema},
            },
        },
        default_temperature=0.0,
        reasoning_effort_policy="medium_if_supported",
    )
    adapter = GeminiNativeRequestAdapter()
    payload = adapter.build_direct_kwargs(
        request,
        ModelCapabilities(
            token_limit_parameter="max_output_tokens",
            supports_reasoning=True,
            reasoning_efforts=("low", "medium", "high"),
            default_reasoning_effort="medium",
            temperature_supported=True,
            structured_outputs_supported=True,
        ),
    )

    config = payload["config"]
    assert payload["model"] == "gemini-test"
    assert config.system_instruction == "system"
    assert config.max_output_tokens == 512
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == schema
    assert config.thinking_config.thinking_level.value == "MEDIUM"
    assert payload["contents"][0].role == "user"


def test_gemini_non_reasoning_model_uses_provider_default():
    request = request_from_config(
        model=ModelRef("google", "gemini-lite"),
        messages=[CanonicalMessage.text("user", "judge")],
        config={},
        default_temperature=0.0,
        reasoning_effort_policy="medium_if_supported",
    )
    kwargs = GeminiNativeRequestAdapter().build_config_kwargs(
        request,
        ModelCapabilities(
            supports_reasoning=False,
            temperature_supported=True,
        ),
    )

    assert kwargs["temperature"] == 0.0
    assert "thinking_config" not in kwargs


def test_gemini_history_preserves_parts_and_normalizes_model_role():
    messages = canonical_messages_from_gemini_history(
        [{"role": "model", "parts": ["first", {"text": "second"}]}]
    )

    assert messages[0].role == "assistant"
    assert [part.text for part in messages[0].parts] == ["first", "second"]
