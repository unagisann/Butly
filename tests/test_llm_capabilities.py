"""Canonical LLM capability解決・補正・永続化の回帰テスト。"""

import asyncio
from unittest.mock import MagicMock

import pytest

from butly_core.llm.capabilities import (
    CapabilityStore,
    ModelCapabilities,
    configure_capability_runtime,
    get_capability_resolver,
    invalidate_provider_metadata,
    register_provider_metadata,
)
from butly_core.llm.connections import Connection, get_connection
from butly_core.llm.model_registry import ModelRef
from butly_core.llm.protocols.openai_compat import OpenAICompatAdapter


class UnsupportedParameterError(RuntimeError):
    status_code = 400

    def __init__(self, parameter: str, message: str):
        super().__init__(message)
        self.body = {
            "message": message,
            "type": "invalid_request_error",
            "param": parameter,
            "code": "unsupported_parameter",
        }


def _response(text: str = "ok"):
    return MagicMock(
        choices=[MagicMock(message=MagicMock(content=text))]
    )


@pytest.fixture(autouse=True)
def isolated_capability_runtime(tmp_path):
    # Provider method内のlazy importでruntime pathが後から上書きされないよう、
    # legacy config初期化を先に完了させる。
    import butly_core.config as config_module
    from butly_core.settings import get_settings

    runtime_settings = get_settings(config_module.USER_CONFIG_PATH)
    configure_capability_runtime(tmp_path, {})
    yield tmp_path
    configure_capability_runtime(
        config_module.USER_CONFIG_PATH.parent,
        runtime_settings.LLM_CAPABILITY_OVERRIDES,
    )


def test_official_openai_unknown_model_uses_current_protocol_defaults():
    adapter = OpenAICompatAdapter(connection=get_connection("openai"))
    adapter._client = MagicMock()
    adapter._client.chat.completions.create.return_value = _response()

    adapter.classify(
        "judge",
        {
            "model_name": "gpt-5.6-luna",
            "generation_config": {"max_output_tokens": 2048},
            "_purpose": "evaluation",
            "_reasoning_effort_policy": "medium_if_supported",
        },
    )

    kwargs = adapter._client.chat.completions.create.call_args.kwargs
    assert kwargs["max_completion_tokens"] == 2048
    # Capability不明ならProvider公式defaultを使い、推測parameterは送らない。
    assert "temperature" not in kwargs
    assert "reasoning_effort" not in kwargs


def test_provider_metadata_enables_middle_reasoning_policy():
    model = ModelRef("openai", "metadata-reasoner")
    register_provider_metadata(
        model,
        {
            "supported_parameters": [
                "max_completion_tokens",
                "reasoning_effort",
            ],
            "reasoning_efforts": ["low", "medium", "high"],
            "default_reasoning_effort": "medium",
        },
    )

    resolved = get_capability_resolver().resolve(
        get_connection("openai"),
        model,
    )

    assert resolved.supports_reasoning is True
    assert resolved.reasoning_efforts == ("low", "medium", "high")
    assert resolved.default_reasoning_effort == "medium"
    assert resolved.temperature_supported is False

    invalidate_provider_metadata("openai")
    refreshed = get_capability_resolver().resolve(
        get_connection("openai"),
        model,
    )
    assert refreshed.supports_reasoning is None


def test_advertised_reasoning_default_implies_parameter_support():
    model = ModelRef("openai", "metadata-default-only")
    register_provider_metadata(
        model,
        {"default_reasoning_effort": "medium"},
    )

    resolved = get_capability_resolver().resolve(
        get_connection("openai"),
        model,
    )

    assert resolved.supports_reasoning is True
    assert resolved.default_reasoning_effort == "medium"


def test_clear_parameter_error_is_corrected_once_and_success_is_cached(
    isolated_capability_runtime,
):
    connection = Connection(id="compat-test", protocol="openai_compat")
    adapter = OpenAICompatAdapter(connection=connection)
    adapter._client = MagicMock()
    adapter._client.chat.completions.create.side_effect = [
        UnsupportedParameterError(
            "max_tokens",
            "Unsupported parameter: 'max_tokens'. Use "
            "'max_completion_tokens' instead.",
        ),
        _response("recovered"),
    ]

    result = adapter.classify(
        "judge",
        {
            "model_name": "future-reasoner",
            "generation_config": {"max_output_tokens": 1024},
            "_reasoning_effort_policy": "medium_if_supported",
        },
    )

    assert result == "recovered"
    assert adapter._client.chat.completions.create.call_count == 2
    first, second = [
        call.kwargs
        for call in adapter._client.chat.completions.create.call_args_list
    ]
    assert first["max_tokens"] == 1024
    assert second["max_completion_tokens"] == 1024
    # token parameter名だけではreasoning対応とは断定しない。
    assert "reasoning_effort" not in second
    assert "temperature" not in second

    cache = CapabilityStore(
        isolated_capability_runtime / "llm_capabilities.json"
    ).get(ModelRef("compat-test", "future-reasoner"))
    assert cache is not None
    assert cache.token_limit_parameter == "max_completion_tokens"
    assert cache.supports_reasoning is None

    next_adapter = OpenAICompatAdapter(connection=connection)
    next_adapter._client = MagicMock()
    next_adapter._client.chat.completions.create.return_value = _response()
    next_adapter.classify(
        "judge",
        {
            "model_name": "future-reasoner",
            "generation_config": {"max_output_tokens": 64},
            "_reasoning_effort_policy": "medium_if_supported",
        },
    )
    next_kwargs = next_adapter._client.chat.completions.create.call_args.kwargs
    assert next_kwargs["max_completion_tokens"] == 64
    assert "max_tokens" not in next_kwargs


def test_ambiguous_or_explicit_parameter_errors_are_not_retried():
    connection = Connection(id="no-guess", protocol="openai_compat")
    adapter = OpenAICompatAdapter(connection=connection)
    adapter._client = MagicMock()
    adapter._client.chat.completions.create.side_effect = RuntimeError(
        "Unsupported parameter: 'temperature'"
    )

    with pytest.raises(RuntimeError, match="Unsupported parameter"):
        adapter.classify("x", {"model_name": "m"})
    assert adapter._client.chat.completions.create.call_count == 1

    adapter._client.reset_mock()
    adapter._client.chat.completions.create.side_effect = (
        UnsupportedParameterError(
            "temperature",
            "Unsupported parameter: 'temperature'",
        )
    )
    with pytest.raises(UnsupportedParameterError):
        adapter.classify(
            "x",
            {
                "model_name": "m",
                "generation_config": {"temperature": 0.2},
            },
        )
    assert adapter._client.chat.completions.create.call_count == 1


def test_failed_correction_is_not_cached(isolated_capability_runtime):
    connection = Connection(id="failed-correction", protocol="openai_compat")
    adapter = OpenAICompatAdapter(connection=connection)
    adapter._client = MagicMock()
    adapter._client.chat.completions.create.side_effect = [
        UnsupportedParameterError(
            "max_tokens",
            "Unsupported parameter: 'max_tokens'. Use "
            "'max_completion_tokens' instead.",
        ),
        RuntimeError("still failed"),
    ]

    with pytest.raises(RuntimeError, match="still failed"):
        adapter.classify(
            "x",
            {
                "model_name": "m",
                "generation_config": {"max_output_tokens": 64},
            },
        )

    stored = CapabilityStore(
        isolated_capability_runtime / "llm_capabilities.json"
    ).get(ModelRef("failed-correction", "m"))
    assert stored is None


def test_stream_uses_same_one_correction_and_cache(
    isolated_capability_runtime,
    monkeypatch,
):
    connection = Connection(id="stream-correction", protocol="openai_compat")
    adapter = OpenAICompatAdapter(
        connection=connection,
        default_model_name="future-stream-model",
    )
    adapter._client = MagicMock()
    chunk = MagicMock()
    chunk.choices = [MagicMock()]
    chunk.choices[0].delta.content = "ok"
    adapter._client.chat.completions.create.side_effect = [
        UnsupportedParameterError(
            "max_tokens",
            "Unsupported parameter: 'max_tokens'. Use "
            "'max_completion_tokens' instead.",
        ),
        iter([chunk]),
    ]
    monkeypatch.setattr(
        "butly_core.llm._openai_compat.resolve_system_instruction",
        lambda _context: "system",
    )
    monkeypatch.setattr(
        "butly_core.llm._openai_compat.resolve_context_prefix",
        lambda _context: "",
    )

    async def _collect():
        return [
            event
            async for event in adapter.async_generate_stream(
                "hello",
                [],
                {
                    "history": [],
                    "override_config": {
                        "chat": {
                            "generation_config": {
                                "max_output_tokens": 64,
                            }
                        }
                    },
                },
            )
        ]

    events = asyncio.run(_collect())

    assert [event["type"] for event in events] == ["chunk", "done"]
    calls = adapter._client.chat.completions.create.call_args_list
    assert calls[0].kwargs["max_tokens"] == 64
    assert calls[1].kwargs["max_completion_tokens"] == 64
    stored = CapabilityStore(
        isolated_capability_runtime / "llm_capabilities.json"
    ).get(ModelRef("stream-correction", "future-stream-model"))
    assert stored is not None
    assert stored.token_limit_parameter == "max_completion_tokens"


def test_manual_override_has_highest_precedence(isolated_capability_runtime):
    configure_capability_runtime(
        isolated_capability_runtime,
        {
            "manual-connection": {
                "m": {
                    "token_limit_parameter": "max_completion_tokens",
                    "supports_reasoning": True,
                    "temperature_supported": False,
                }
            }
        },
    )
    connection = Connection(
        id="manual-connection",
        protocol="openai_compat",
    )
    resolved = get_capability_resolver().resolve(
        connection,
        ModelRef("manual-connection", "m"),
    )

    assert resolved.token_limit_parameter == "max_completion_tokens"
    assert resolved.supports_reasoning is True
    assert resolved.temperature_supported is False
    assert "manual_override" in resolved.source


def test_store_merges_observations_atomically(tmp_path):
    store = CapabilityStore(tmp_path / "capabilities.json")
    model = ModelRef("c", "m")
    store.put(
        model,
        ModelCapabilities(token_limit_parameter="max_completion_tokens"),
    )
    store.put(
        model,
        ModelCapabilities(temperature_supported=False),
    )

    loaded = store.get(model)
    assert loaded is not None
    assert loaded.token_limit_parameter == "max_completion_tokens"
    assert loaded.temperature_supported is False
    assert (tmp_path / "capabilities.json").read_text().endswith("\n")
