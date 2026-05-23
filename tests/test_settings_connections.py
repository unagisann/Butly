"""
tests/test_settings_connections.py
----------------------------------
Phase 3: /settings/connections CRUD + /settings/test_connection の単体テスト。

route 関数を直接呼ぶ (FastAPI TestClient なしのまま) ことで HTTP モックを排除。
user_config.json の I/O は monkeypatch で tmp_path に向ける。
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_registry():
    from butly_core.llm.connections import get_registry
    get_registry().reset_to_builtin()
    yield
    get_registry().reset_to_builtin()


@pytest.fixture
def tmp_user_config(tmp_path: Path, monkeypatch):
    """settings.py の USER_CONFIG_PATH を tmp 配下に差し替える。"""
    cfg = tmp_path / "user_config.json"
    cfg.write_text("{}", encoding="utf-8")
    import routers.settings as settings_mod
    monkeypatch.setattr(settings_mod, "USER_CONFIG_PATH", cfg)
    return cfg


# ===================================================================
# GET /settings/connections
# ===================================================================

class TestListConnections:
    def test_lists_builtin(self):
        from routers.settings import list_connections_endpoint
        res = list_connections_endpoint()
        ids = {c["id"] for c in res["connections"]}
        assert {"google", "openai", "xai", "ollama"}.issubset(ids)

        # built-in フラグが付く
        for c in res["connections"]:
            if c["id"] in ("google", "openai", "xai", "ollama"):
                assert c["is_builtin"] is True

    def test_lists_user_defined(self, tmp_user_config):
        from butly_core.llm.connections import Connection, register_connection
        register_connection(
            Connection(
                id="groq", protocol="openai_compat",
                base_url="https://api.groq.com/openai/v1",
                api_key_env="GROQ_API_KEY", label="Groq",
            )
        )
        from routers.settings import list_connections_endpoint
        res = list_connections_endpoint()
        groq = next(c for c in res["connections"] if c["id"] == "groq")
        assert groq["is_builtin"] is False
        assert groq["base_url"] == "https://api.groq.com/openai/v1"


# ===================================================================
# POST /settings/connections
# ===================================================================

class TestAddConnection:
    def test_add_user_connection(self, tmp_user_config):
        from routers.settings import add_connection, ConnectionPayload
        from butly_core.llm.connections import try_get_connection

        payload = ConnectionPayload(
            id="groq", protocol="openai_compat",
            base_url="https://api.groq.com/openai/v1",
            api_key_env="GROQ_API_KEY", label="Groq",
            embeddings_supported=False,
        )
        res = add_connection(payload)
        assert "Connection" in res["message"]
        assert res["connection"]["id"] == "groq"

        conn = try_get_connection("groq")
        assert conn is not None
        assert conn.label == "Groq"

        # user_config.json に永続化されている
        data = json.loads(tmp_user_config.read_text(encoding="utf-8"))
        assert any(
            e.get("id") == "groq" for e in data.get("LLM_CONNECTIONS", [])
        )

    def test_add_rejects_builtin_id(self, tmp_user_config):
        from fastapi import HTTPException
        from routers.settings import add_connection, ConnectionPayload
        payload = ConnectionPayload(
            id="openai", protocol="openai_compat",
            base_url="https://evil.example.com/v1",
        )
        with pytest.raises(HTTPException) as exc:
            add_connection(payload)
        assert exc.value.status_code == 400

    def test_add_rejects_unknown_protocol(self, tmp_user_config):
        from fastapi import HTTPException
        from routers.settings import add_connection, ConnectionPayload
        payload = ConnectionPayload(
            id="weird", protocol="ftp",
            base_url="ftp://example.com",
        )
        with pytest.raises(HTTPException) as exc:
            add_connection(payload)
        assert exc.value.status_code == 400

    def test_add_replaces_existing_user_entry(self, tmp_user_config):
        from routers.settings import add_connection, ConnectionPayload
        add_connection(ConnectionPayload(
            id="groq", protocol="openai_compat",
            base_url="https://api.groq.com/openai/v1", label="Old",
        ))
        add_connection(ConnectionPayload(
            id="groq", protocol="openai_compat",
            base_url="https://api.groq.com/openai/v1", label="New",
        ))
        data = json.loads(tmp_user_config.read_text(encoding="utf-8"))
        groq_entries = [
            e for e in data["LLM_CONNECTIONS"] if e["id"] == "groq"
        ]
        assert len(groq_entries) == 1
        assert groq_entries[0]["label"] == "New"


# ===================================================================
# DELETE /settings/connections/{id}
# ===================================================================

class TestDeleteConnection:
    def test_delete_user_connection(self, tmp_user_config):
        from routers.settings import add_connection, delete_connection, ConnectionPayload
        from butly_core.llm.connections import try_get_connection

        add_connection(ConnectionPayload(
            id="groq", protocol="openai_compat",
            base_url="https://api.groq.com/openai/v1",
        ))
        assert try_get_connection("groq") is not None

        res = delete_connection("groq")
        assert "削除" in res["message"]
        assert try_get_connection("groq") is None

        # user_config.json からも消えている
        data = json.loads(tmp_user_config.read_text(encoding="utf-8"))
        assert not any(
            e.get("id") == "groq" for e in data.get("LLM_CONNECTIONS", [])
        )

    def test_delete_rejects_builtin(self):
        from fastapi import HTTPException
        from routers.settings import delete_connection
        with pytest.raises(HTTPException) as exc:
            delete_connection("openai")
        assert exc.value.status_code == 400

    def test_delete_rejects_unknown(self):
        from fastapi import HTTPException
        from routers.settings import delete_connection
        with pytest.raises(HTTPException) as exc:
            delete_connection("nope")
        assert exc.value.status_code == 404


# ===================================================================
# POST /settings/test_connection
# ===================================================================

class TestTestConnection:
    def test_unknown_connection_404(self):
        from fastapi import HTTPException
        from routers.settings import test_connection
        with pytest.raises(HTTPException) as exc:
            test_connection(connection_id="nope")
        assert exc.value.status_code == 404

    def test_openai_compat_mocks_models_endpoint(self, monkeypatch):
        """openai_compat connection: /models を URL-fetch する。"""
        from routers.settings import test_connection
        from butly_core.llm.connections import Connection, register_connection

        register_connection(Connection(
            id="groq", protocol="openai_compat",
            base_url="https://api.groq.com/openai/v1",
            api_key_env="GROQ_API_KEY",
        ))
        monkeypatch.setenv("GROQ_API_KEY", "test-key")

        class _FakeResp:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self):
                return json.dumps({
                    "data": [{"id": "llama-3.3-70b"}, {"id": "mixtral"}]
                }).encode()

        def _fake_urlopen(req, timeout=5):
            assert "groq.com" in req.full_url
            assert req.get_header("Authorization") == "Bearer test-key"
            return _FakeResp()

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        res = test_connection(connection_id="groq")
        assert res["status"] == "ok"
        assert "llama-3.3-70b" in res["models"]

    def test_openai_compat_http_failure_returns_error(self, monkeypatch):
        from routers.settings import test_connection
        from butly_core.llm.connections import Connection, register_connection

        register_connection(Connection(
            id="groq", protocol="openai_compat",
            base_url="https://api.groq.com/openai/v1",
        ))

        from urllib.error import URLError

        def _fake_urlopen(req, timeout=5):
            raise URLError("conn refused")
        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

        res = test_connection(connection_id="groq")
        assert res["status"] == "error"
        assert res["models"] == []
