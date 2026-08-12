"""
test_api_v1_system.py
---------------------
/api/v1 の system endpoint（health / ready / app-info / capabilities）と
request ID middleware のテスト。
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from butly_api import create_app
from butly_api.context import ApiContext
from butly_api.version import API_VERSION, BACKEND_VERSION


@pytest.fixture
def bare_client() -> TestClient:
    """context なし（OpenAPI 生成と同条件）の app。"""
    return TestClient(create_app())


@pytest.fixture
def ready_client(tmp_path: Path) -> TestClient:
    instances_dir = tmp_path / "instances"
    instances_dir.mkdir()
    context = ApiContext(
        data_dir=tmp_path,
        instances_dir=instances_dir,
        runtime_supplier=lambda: object(),
        settings_loaded=True,
    )
    return TestClient(create_app(context=context))


# ===================================================================
# health / app-info
# ===================================================================


def test_health_returns_versions_only(bare_client):
    res = bare_client.get("/api/v1/health")
    assert res.status_code == 200
    assert res.json() == {
        "status": "ok",
        "backend_version": BACKEND_VERSION,
        "api_version": API_VERSION,
    }


def test_app_info(bare_client):
    res = bare_client.get("/api/v1/app-info")
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "Butly"
    assert body["backend_version"] == BACKEND_VERSION
    assert body["api_version"] == API_VERSION
    assert isinstance(body["platform"], str)
    assert isinstance(body["feature_flags"], dict)


# ===================================================================
# ready
# ===================================================================


def test_ready_false_without_context(bare_client):
    res = bare_client.get("/api/v1/ready")
    assert res.status_code == 200
    body = res.json()
    assert body["ready"] is False
    names = [c["name"] for c in body["checks"]]
    assert "runtime_initialized" in names


def test_ready_true_with_full_context(ready_client):
    res = ready_client.get("/api/v1/ready")
    assert res.status_code == 200
    body = res.json()
    assert body["ready"] is True
    assert all(c["ok"] for c in body["checks"])
    names = {c["name"] for c in body["checks"]}
    assert names == {
        "runtime_initialized",
        "data_dir_writable",
        "instances_dir_exists",
        "settings_loaded",
    }


def test_ready_false_when_runtime_missing(tmp_path):
    instances_dir = tmp_path / "instances"
    instances_dir.mkdir()
    context = ApiContext(
        data_dir=tmp_path,
        instances_dir=instances_dir,
        runtime_supplier=lambda: None,
        settings_loaded=True,
    )
    client = TestClient(create_app(context=context))
    body = client.get("/api/v1/ready").json()
    assert body["ready"] is False
    failed = {c["name"] for c in body["checks"] if not c["ok"]}
    assert failed == {"runtime_initialized"}


# ===================================================================
# capabilities
# ===================================================================

_PROVIDER_ENV_VARS = [
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "OLLAMA_WEB_SEARCH_API_KEY",
    "TAVILY_API_KEY",
]


def _clear_provider_env(monkeypatch):
    for name in _PROVIDER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_capabilities_without_keys(bare_client, monkeypatch):
    _clear_provider_env(monkeypatch)
    body = bare_client.get("/api/v1/capabilities").json()

    assert body["chat"]["available"] is False
    assert body["chat"]["reason"] == "runtime_not_initialized"
    assert body["native_google_search"]["available"] is False
    assert body["native_google_search"]["reason"] == "runtime_not_initialized"
    assert body["generic_web_search"]["available"] is False
    assert body["vision"]["available"] is False

    from butly_core.chat.types import (
        ALLOWED_IMAGE_MIMES,
        MAX_ATTACHMENT_SIZE,
        MAX_ATTACHMENTS,
    )

    att = body["attachments"]
    assert att["max_count"] == MAX_ATTACHMENTS
    assert att["max_size_bytes"] == MAX_ATTACHMENT_SIZE
    assert att["allowed_mime_types"] == sorted(ALLOWED_IMAGE_MIMES)


def test_capabilities_with_gemini_key(ready_client, monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-for-test")
    body = ready_client.get("/api/v1/capabilities").json()

    assert body["chat"]["available"] is True
    assert body["chat"]["reason"] is None
    assert body["streaming"]["available"] is True
    assert body["streaming"]["mode"] == "incremental"
    assert body["native_google_search"]["available"] is True
    assert body["vision"]["available"] is True
    # API キーそのものが response に漏れていないこと
    import json

    assert "dummy-for-test" not in json.dumps(body, ensure_ascii=False)


def test_capabilities_follow_active_ollama_model(tmp_path, monkeypatch):
    from butly_api.routers.system import get_capabilities
    from butly_core.settings import clear_settings_cache

    _clear_provider_env(monkeypatch)
    (tmp_path / "user_config.json").write_text(
        json.dumps(
            {
                "AI_CONFIG": {
                    "chat": {
                        "connection": "ollama",
                        "model_name": "ollama/llama3.2",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    clear_settings_cache()
    try:
        context = ApiContext(
            data_dir=tmp_path,
            runtime_supplier=lambda: object(),
        )
        request = Request(
            {
                "type": "http",
                "app": SimpleNamespace(
                    state=SimpleNamespace(api_context=context)
                ),
            }
        )
        body = get_capabilities(request)
        assert body.active_connection == "ollama"
        assert body.active_model == "ollama/llama3.2"
        assert body.chat.available is True
        assert body.native_google_search.available is False
        assert body.native_google_search.reason == (
            "active_connection_does_not_support_google_search"
        )
        assert body.vision.available is False
        assert body.vision.reason == "active_model_does_not_support_vision"
    finally:
        clear_settings_cache()


def test_chat_debug_capability_matches_developer_mode(tmp_path):
    from butly_api.routers.system import get_capabilities

    def capability(token):
        context = ApiContext(
            data_dir=tmp_path,
            runtime_supplier=lambda: object(),
            developer_mode=True,
            auth_token=token,
        )
        request = Request(
            {
                "type": "http",
                "app": SimpleNamespace(
                    state=SimpleNamespace(api_context=context)
                ),
            }
        )
        return get_capabilities(request).chat_debug

    without_token = capability(None)
    assert without_token.available is True
    assert without_token.reason is None
    with_token = capability("desktop-test-token")
    assert with_token.available is True
    assert with_token.reason is None


# ===================================================================
# request ID
# ===================================================================


def test_request_id_header_generated(bare_client):
    res = bare_client.get("/api/v1/health")
    assert res.headers.get("X-Request-ID")


def test_request_id_header_echoed(bare_client):
    res = bare_client.get("/api/v1/health", headers={"X-Request-ID": "req-123"})
    assert res.headers["X-Request-ID"] == "req-123"
