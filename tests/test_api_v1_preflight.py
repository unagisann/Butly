"""Connection/embedding preflight contract tests without real providers."""

import asyncio
import json
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from starlette.requests import Request

from butly_api.context import ApiContext
from butly_api.routers import preflight as module
from butly_core.llm.connections import Connection
from butly_core.llm.model_registry import ModelRef


def _run(coro):
    return asyncio.run(coro)


def _request(tmp_path: Path) -> Request:
    app = SimpleNamespace(
        state=SimpleNamespace(
            api_context=ApiContext(data_dir=tmp_path),
        )
    )
    return Request({"type": "http", "app": app})


def test_optional_unconfigured_connection_does_not_degrade_required_roles(
    tmp_path, monkeypatch
):
    chat = Connection(
        id="chat_local",
        protocol="openai_compat",
        base_url="http://127.0.0.1:11434/v1",
        api_key_env=None,
    )
    embedding = Connection(
        id="embed_remote",
        protocol="openai_compat",
        base_url="https://embedding.invalid/v1",
        api_key_env="EMBED_TEST_KEY",
    )
    optional = Connection(
        id="optional_remote",
        protocol="openai_compat",
        base_url="https://optional.invalid/v1",
        api_key_env="OPTIONAL_TEST_KEY",
    )
    monkeypatch.setenv("EMBED_TEST_KEY", "test-only")
    monkeypatch.delenv("OPTIONAL_TEST_KEY", raising=False)
    monkeypatch.setattr(
        module,
        "_role_refs",
        lambda _ctx: (
            ModelRef("chat_local", "llama-test"),
            ModelRef("embed_remote", "embed-test"),
        ),
    )
    monkeypatch.setattr(
        module, "list_connections", lambda: [chat, embedding, optional]
    )
    monkeypatch.setattr(
        module,
        "try_get_connection",
        lambda connection_id: embedding
        if connection_id == "embed_remote"
        else None,
    )

    async def probe_connection(conn, _semaphore):
        if conn.id == "optional_remote":
            return (
                module._ProbeOutcome(
                    configured=False,
                    reachable=False,
                    reason="api_key_not_configured",
                ),
                1,
            )
        models = ("embed-test",) if conn.id == "embed_remote" else ("llama-test",)
        return module._ProbeOutcome(True, True, None, models), 2

    async def probe_embedding(_conn, _model_name):
        return module._EmbeddingOutcome(True, True, None, 3), 4

    monkeypatch.setattr(module, "_probe_connection", probe_connection)
    monkeypatch.setattr(module, "_probe_embedding", probe_embedding)

    response = _run(module.get_preflight(_request(tmp_path), refresh=True))
    by_id = {item.connection_id: item for item in response.connections}
    assert response.status == "ready"
    assert by_id["optional_remote"].status == "not_configured"
    assert by_id["chat_local"].required_for == ["chat"]
    assert by_id["embed_remote"].required_for == ["embedding"]
    assert response.embedding.reachable is True
    assert response.embedding.dimension == 3
    assert response.embedding.model_available is True


def test_ollama_without_key_is_configured(monkeypatch):
    connection = Connection(
        id="ollama_test",
        protocol="openai_compat",
        base_url="http://127.0.0.1:11434/v1",
        api_key_env=None,
    )
    monkeypatch.setattr(module, "_openai_models", lambda _conn: ("llama3",))

    outcome = module._probe_connection_sync(connection)
    assert outcome.configured is True
    assert outcome.reachable is True
    assert outcome.models == ("llama3",)


def test_ollama_uses_native_tags_and_openai_embedding_url(monkeypatch):
    connection = Connection(
        id="ollama",
        protocol="openai_compat",
        base_url="http://127.0.0.1:11434/v1",
        api_key_env=None,
        model_name_strip_prefix="ollama/",
    )
    requests = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    def urlopen(request, timeout):
        requests.append((request, timeout))
        if request.full_url.endswith("/api/tags"):
            return Response({"models": [{"name": "llama3"}]})
        return Response({"data": [{"embedding": [0.1, 0.2, 0.3]}]})

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)

    assert module._openai_models(connection) == ("llama3",)
    assert module._openai_embedding(
        connection, "ollama/nomic-embed-text"
    ) == 3
    assert requests[0][0].full_url == "http://127.0.0.1:11434/api/tags"
    assert requests[1][0].full_url == (
        "http://127.0.0.1:11434/v1/embeddings"
    )
    body = json.loads(requests[1][0].data)
    assert body["model"] == "nomic-embed-text"
    assert body["input"] == "Butly embedding connectivity preflight."


def test_embedding_probe_requires_nonempty_finite_vector(monkeypatch):
    connection = Connection(
        id="embed_test",
        protocol="openai_compat",
        base_url="http://127.0.0.1:11434/v1",
        api_key_env=None,
    )
    monkeypatch.setattr(module, "_openai_embedding", lambda *_args: None)
    invalid = module._probe_embedding_sync(connection, "embed")
    assert invalid.reachable is False
    assert invalid.reason == "invalid_embedding_response"

    monkeypatch.setattr(module, "_openai_embedding", lambda *_args: 768)
    valid = module._probe_embedding_sync(connection, "embed")
    assert valid.reachable is True
    assert valid.dimension == 768

    assert module._validate_embedding_vector([]) is None
    assert module._validate_embedding_vector([0.1, float("nan")]) is None
    assert module._validate_embedding_vector([0.1, float("inf")]) is None


def test_model_matching_never_conflates_distinct_ollama_tags():
    assert module._matches_model(
        "ollama/llama3:70b", ("llama3:70b",)
    )
    assert not module._matches_model(
        "ollama/llama3:70b", ("llama3:8b",)
    )
    assert module._matches_model("ollama/llama3", ("llama3:latest",))


def test_safe_reason_maps_sdk_status_without_raw_message():
    class SdkError(Exception):
        status_code = 403

    assert module._safe_reason(
        SdkError("secret-token at /private/provider/path")
    ) == "authentication_failed"


def test_async_probe_returns_at_deadline_without_raw_error(monkeypatch):
    connection = Connection(
        id="slow_test",
        protocol="openai_compat",
        base_url="http://127.0.0.1:9/v1",
        api_key_env=None,
    )
    monkeypatch.setattr(module, "_PROBE_TIMEOUT_SECONDS", 0.01)

    async def slow_to_thread(_func, *_args):
        await asyncio.sleep(10)

    monkeypatch.setattr(module.asyncio, "to_thread", slow_to_thread)

    async def scenario():
        started = time.monotonic()
        outcome, _latency = await module._probe_connection(
            connection, asyncio.Semaphore(1)
        )
        elapsed = time.monotonic() - started
        assert elapsed < 0.08
        assert outcome.reason == "connection_timeout"
        assert "secret" not in (outcome.reason or "")

    _run(scenario())


def test_preflight_cache_and_refresh(tmp_path, monkeypatch):
    connection = Connection(
        id="cache_local",
        protocol="openai_compat",
        base_url="http://127.0.0.1:11434/v1",
        api_key_env=None,
    )
    calls = 0
    monkeypatch.setattr(
        module,
        "_role_refs",
        lambda _ctx: (
            ModelRef("cache_local", "chat"),
            ModelRef("cache_local", "embed"),
        ),
    )
    monkeypatch.setattr(module, "list_connections", lambda: [connection])
    monkeypatch.setattr(module, "try_get_connection", lambda _id: connection)

    async def probe_connection(_connection, _semaphore):
        nonlocal calls
        calls += 1
        return module._ProbeOutcome(True, True, None, ("chat", "embed")), 1

    async def probe_embedding(_connection, _model):
        return module._EmbeddingOutcome(True, True, None, 4), 1

    monkeypatch.setattr(module, "_probe_connection", probe_connection)
    monkeypatch.setattr(module, "_probe_embedding", probe_embedding)
    request = _request(tmp_path)

    first = _run(module.get_preflight(request, refresh=False))
    cached = _run(module.get_preflight(request, refresh=False))
    refreshed = _run(module.get_preflight(request, refresh=True))
    assert calls == 2
    assert cached.checked_at == first.checked_at
    assert refreshed.status == "ready"


def test_missing_active_chat_model_makes_preflight_unavailable(
    tmp_path, monkeypatch
):
    connection = Connection(
        id="ollama_missing",
        protocol="openai_compat",
        base_url="http://127.0.0.1:11434/v1",
        api_key_env=None,
    )
    monkeypatch.setattr(
        module,
        "_role_refs",
        lambda _ctx: (
            ModelRef("ollama_missing", "ollama/llama3:70b"),
            ModelRef("ollama_missing", "ollama/nomic-embed-text"),
        ),
    )
    monkeypatch.setattr(module, "list_connections", lambda: [connection])
    monkeypatch.setattr(module, "try_get_connection", lambda _id: connection)

    async def probe_connection(_connection, _semaphore):
        return module._ProbeOutcome(
            True,
            True,
            None,
            ("llama3:8b", "nomic-embed-text"),
        ), 1

    async def probe_embedding(_connection, _model):
        return module._EmbeddingOutcome(True, True, None, 8), 1

    monkeypatch.setattr(module, "_probe_connection", probe_connection)
    monkeypatch.setattr(module, "_probe_embedding", probe_embedding)

    response = _run(module.get_preflight(_request(tmp_path), refresh=True))
    assert response.status == "unavailable"
    chat = response.connections[0]
    assert chat.reachable is True
    assert chat.model_available is False
    assert chat.status == "unavailable"
    assert chat.reason == "chat_model_not_available"


def test_data_dir_settings_drive_legacy_runtime_and_api_resolution(
    tmp_path, monkeypatch
):
    from butly_core import config as legacy_config
    from butly_core.llm.connections import get_registry, try_get_connection
    from butly_core.settings import apply_runtime_settings, clear_settings_cache
    from butly_api.routers.system import _active_chat

    original_ai = deepcopy(legacy_config.AI_CONFIG)
    original_system = deepcopy(legacy_config.SYSTEM_CONFIG)
    registry = get_registry()
    original_custom = list(registry.list_user_defined())
    config = {
        "LLM_CONNECTIONS": [
            {
                "id": "sidecar_custom",
                "protocol": "openai_compat",
                "base_url": "http://127.0.0.1:18080/v1",
                "api_key_env": None,
                "embeddings_supported": True,
            }
        ],
        "AI_CONFIG": {
            "chat": {
                "connection": "sidecar_custom",
                "model_name": "custom-chat",
            },
            "embedding": {
                "connection": "sidecar_custom",
                "model_name": "custom-embed",
            },
        },
    }
    (tmp_path / "user_config.json").write_text(
        json.dumps(config), encoding="utf-8"
    )
    clear_settings_cache()
    try:
        settings = apply_runtime_settings(tmp_path)
        assert settings.ai.chat["connection"] == "sidecar_custom"
        assert legacy_config.AI_CONFIG["chat"]["model_name"] == "custom-chat"
        connection = try_get_connection("sidecar_custom")
        assert connection is not None
        assert connection.resolve_base_url() == "http://127.0.0.1:18080/v1"

        context = ApiContext(data_dir=tmp_path, runtime_supplier=lambda: object())
        chat_ref, embedding_ref = module._role_refs(context)
        assert chat_ref == ModelRef("sidecar_custom", "custom-chat")
        assert embedding_ref == ModelRef("sidecar_custom", "custom-embed")

        request = Request(
            {
                "type": "http",
                "app": SimpleNamespace(
                    state=SimpleNamespace(api_context=context)
                ),
            }
        )
        active_ref, active_connection = _active_chat(request)
        assert active_ref == chat_ref
        assert active_connection is connection
    finally:
        legacy_config.AI_CONFIG.clear()
        legacy_config.AI_CONFIG.update(original_ai)
        legacy_config.SYSTEM_CONFIG.clear()
        legacy_config.SYSTEM_CONFIG.update(original_system)
        registry.reset_to_builtin()
        for connection in original_custom:
            registry.register(connection, overwrite_user=True)
        clear_settings_cache()
