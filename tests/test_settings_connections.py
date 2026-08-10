"""
tests/test_settings_connections.py
----------------------------------
Phase 3: /settings/connections CRUD + /settings/test_connection の単体テスト。

route 関数を直接呼ぶ (FastAPI TestClient なしのまま) ことで HTTP モックを排除。
user_config.json の I/O は monkeypatch で tmp_path に向ける。
"""

import json
import os
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


@pytest.fixture
def tmp_runtime_paths(tmp_path: Path, monkeypatch):
    """API key と instance 参照の永続化先を tmp 配下に差し替える。"""
    import routers.settings as settings_mod

    data_dir = tmp_path / "data"
    instances_dir = tmp_path / "instances"
    data_dir.mkdir()
    instances_dir.mkdir()
    monkeypatch.setattr(settings_mod.deps, "DATA_DIR", data_dir)
    monkeypatch.setattr(settings_mod.deps, "INSTANCES_DIR", instances_dir)
    return {
        "env_path": data_dir / ".env",
        "instances_dir": instances_dir,
    }


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
            base_url="https://example.com",
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

    def test_openai_compat_reports_more_than_200_models(self, monkeypatch):
        from routers.settings import test_connection
        from butly_core.llm.connections import Connection, register_connection

        register_connection(Connection(
            id="large-catalog",
            protocol="openai_compat",
            base_url="https://large-catalog.example/v1",
        ))
        model_ids = [f"model-{index}" for index in range(201)]

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return json.dumps({
                    "data": [{"id": model_id} for model_id in model_ids],
                }).encode()

        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda request, timeout=10: _FakeResp(),
        )

        result = test_connection(connection_id="large-catalog")

        assert result["status"] == "ok"
        assert result["models"] == model_ids

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


# ===================================================================
# POST/DELETE /settings/connections/{id}/api_key
# ===================================================================

class TestConnectionApiKey:
    @staticmethod
    def _register_shared_connections():
        from butly_core.llm.connections import Connection, register_connection

        register_connection(Connection(
            id="nanogpt",
            protocol="openai_compat",
            base_url="https://nano-gpt.com/api/v1",
            api_key_env="NANOGPT_API_KEY",
        ))
        register_connection(Connection(
            id="nanogpt-sub",
            protocol="openai_compat",
            base_url="https://nano-gpt.com/api/subscription/v1",
            api_key_env="NANOGPT_API_KEY",
        ))

    def test_set_key_preserves_env_and_updates_shared_connections(
        self,
        tmp_runtime_paths,
        monkeypatch,
    ):
        from routers.settings import (
            ConnectionApiKeyRequest,
            set_connection_api_key,
        )

        self._register_shared_connections()
        env_path = tmp_runtime_paths["env_path"]
        env_path.write_text(
            "# Provider keys\n"
            "OPENAI_API_KEY=keep-me\n"
            "\n"
            "NANOGPT_API_KEY=old-value\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("NANOGPT_API_KEY", raising=False)
        secret = "new-secret-value"

        result = set_connection_api_key(
            "nanogpt-sub",
            ConnectionApiKeyRequest(api_key=secret),
        )

        assert env_path.read_text(encoding="utf-8") == (
            "# Provider keys\n"
            "OPENAI_API_KEY=keep-me\n"
            "\n"
            "NANOGPT_API_KEY=new-secret-value\n"
        )
        assert os.environ["NANOGPT_API_KEY"] == secret
        assert result["api_key_set"] is True
        assert set(result["affected_connections"]) == {
            "nanogpt",
            "nanogpt-sub",
        }
        assert secret not in json.dumps(result)

    def test_set_key_ignores_client_supplied_env_name(
        self,
        tmp_runtime_paths,
        monkeypatch,
    ):
        from routers.settings import (
            ConnectionApiKeyRequest,
            set_connection_api_key,
        )

        self._register_shared_connections()
        monkeypatch.delenv("NANOGPT_API_KEY", raising=False)
        request = ConnectionApiKeyRequest.model_validate({
            "api_key": "connection-secret",
            "env_name": "PATH",
        })

        set_connection_api_key("nanogpt", request)

        assert os.environ["NANOGPT_API_KEY"] == "connection-secret"
        assert "PATH=connection-secret" not in tmp_runtime_paths[
            "env_path"
        ].read_text(encoding="utf-8")

    def test_delete_key_preserves_unrelated_env_and_clears_process_env(
        self,
        tmp_runtime_paths,
        monkeypatch,
    ):
        from routers.settings import delete_connection_api_key

        self._register_shared_connections()
        env_path = tmp_runtime_paths["env_path"]
        env_path.write_text(
            "# Provider keys\n"
            "NANOGPT_API_KEY=remove-me\n"
            "\n"
            "OPENAI_API_KEY=keep-me\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("NANOGPT_API_KEY", "remove-me")

        result = delete_connection_api_key("nanogpt")

        assert env_path.read_text(encoding="utf-8") == (
            "# Provider keys\n"
            "\n"
            "OPENAI_API_KEY=keep-me\n"
        )
        assert "NANOGPT_API_KEY" not in os.environ
        assert result["api_key_set"] is False
        assert set(result["affected_connections"]) == {
            "nanogpt",
            "nanogpt-sub",
        }
        assert "remove-me" not in json.dumps(result)

    @pytest.mark.parametrize(
        "operation",
        ("set", "delete"),
    )
    def test_unknown_connection_returns_404(
        self,
        operation,
        tmp_runtime_paths,
    ):
        from fastapi import HTTPException
        from routers.settings import (
            ConnectionApiKeyRequest,
            delete_connection_api_key,
            set_connection_api_key,
        )

        with pytest.raises(HTTPException) as exc:
            if operation == "set":
                set_connection_api_key(
                    "missing",
                    ConnectionApiKeyRequest(api_key="secret"),
                )
            else:
                delete_connection_api_key("missing")

        assert exc.value.status_code == 404

    @pytest.mark.parametrize(
        "operation",
        ("set", "delete"),
    )
    def test_authless_connection_returns_400(
        self,
        operation,
        tmp_runtime_paths,
    ):
        from fastapi import HTTPException
        from routers.settings import (
            ConnectionApiKeyRequest,
            delete_connection_api_key,
            set_connection_api_key,
        )

        with pytest.raises(HTTPException) as exc:
            if operation == "set":
                set_connection_api_key(
                    "ollama",
                    ConnectionApiKeyRequest(api_key="secret"),
                )
            else:
                delete_connection_api_key("ollama")

        assert exc.value.status_code == 400

    @pytest.mark.parametrize(
        "invalid_key",
        (
            "",
            "   ",
            "\nsecret",
            "secret\n",
            "line1\nline2",
            "line1\rline2",
            "nul\x00byte",
        ),
    )
    def test_set_rejects_empty_and_control_characters(
        self,
        invalid_key,
        tmp_runtime_paths,
    ):
        from fastapi import HTTPException
        from routers.settings import (
            ConnectionApiKeyRequest,
            set_connection_api_key,
        )

        self._register_shared_connections()

        with pytest.raises(HTTPException) as exc:
            set_connection_api_key(
                "nanogpt",
                ConnectionApiKeyRequest(api_key=invalid_key),
            )

        assert exc.value.status_code == 400
        assert not tmp_runtime_paths["env_path"].exists()


# ===================================================================
# ConnectionPayload validation
# ===================================================================

class TestConnectionPayloadValidation:
    def test_rejects_secret_as_connection_metadata(self):
        from pydantic import ValidationError
        from routers.settings import ConnectionPayload

        with pytest.raises(ValidationError):
            ConnectionPayload(
                id="custom",
                protocol="openai_compat",
                api_key="must-not-be-persisted",
            )

    @pytest.mark.parametrize(
        "connection_id",
        ("UpperCase", "has space", "../escape", "-leading", "a" * 65),
    )
    def test_rejects_invalid_connection_id(self, connection_id):
        from pydantic import ValidationError
        from routers.settings import ConnectionPayload

        with pytest.raises(ValidationError):
            ConnectionPayload(
                id=connection_id,
                protocol="openai_compat",
            )

    @pytest.mark.parametrize(
        ("field", "value"),
        (
            ("api_key_env", "lowercase"),
            ("api_key_env", "PATH"),
            ("base_url_env", "HOME"),
            ("embedding_model_env", "HAS-HYPHEN"),
            ("api_key_fallback_envs", ("GOOD_ENV", "PYTHONPATH")),
        ),
    )
    def test_rejects_invalid_or_reserved_env_names(self, field, value):
        from pydantic import ValidationError
        from routers.settings import ConnectionPayload

        values = {
            "id": "custom",
            "protocol": "openai_compat",
            field: value,
        }
        with pytest.raises(ValidationError):
            ConnectionPayload(**values)

    @pytest.mark.parametrize(
        "base_url",
        (
            "ftp://example.com/v1",
            "example.com/v1",
            "/relative/v1",
            "https://example.com/v1\nInjected: value",
        ),
    )
    def test_rejects_invalid_base_url(self, base_url):
        from pydantic import ValidationError
        from routers.settings import ConnectionPayload

        with pytest.raises(ValidationError):
            ConnectionPayload(
                id="custom",
                protocol="openai_compat",
                base_url=base_url,
            )

    @pytest.mark.parametrize(
        "headers",
        (
            {"X-Safe\nInjected": "value"},
            {"X-Safe": "value\r\nInjected: true"},
        ),
    )
    def test_rejects_newlines_in_extra_headers(self, headers):
        from pydantic import ValidationError
        from routers.settings import ConnectionPayload

        with pytest.raises(ValidationError):
            ConnectionPayload(
                id="custom",
                protocol="openai_compat",
                extra_headers=headers,
            )


# ===================================================================
# GET /settings/connection_templates
# ===================================================================

class TestConnectionTemplates:
    def test_lists_nanogpt_subscription_and_payg_templates(self):
        from routers.settings import list_connection_templates

        result = list_connection_templates()
        templates = {
            template["id"]: template for template in result["templates"]
        }

        subscription = templates["nanogpt-sub"]
        assert subscription["label"] == "NanoGPT Pro (Subscription)"
        assert subscription["base_url"] == (
            "https://nano-gpt.com/api/subscription/v1"
        )
        assert subscription["api_key_env"] == "NANOGPT_API_KEY"
        assert subscription["protocol"] == "openai_compat"
        assert subscription["embeddings_supported"] is False
        assert subscription["extra_headers"] == {}
        assert "provider-selection headers" in subscription["notes"]
        assert templates["nanogpt"]["base_url"] == (
            "https://nano-gpt.com/api/v1"
        )
        assert templates["nanogpt"]["embeddings_supported"] is True
        assert all(
            "api_key" not in template
            for template in result["templates"]
        )


# ===================================================================
# POST /config persistence and referenced Connection deletion
# ===================================================================

class TestConfigAndConnectionReferences:
    def test_update_config_preserves_llm_connections(
        self,
        tmp_user_config,
        monkeypatch,
    ):
        import routers.settings as settings_mod

        original_connections = [
            {
                "id": "nanogpt",
                "protocol": "openai_compat",
                "base_url": "https://nano-gpt.com/api/v1",
                "api_key_env": "NANOGPT_API_KEY",
            }
        ]
        tmp_user_config.write_text(
            json.dumps({
                "LLM_CONNECTIONS": original_connections,
                "OTHER_SETTING": {"preserve": True},
            }),
            encoding="utf-8",
        )
        monkeypatch.setitem(settings_mod.AI_CONFIG, "_test_value", "old")

        result = settings_mod.update_config({
            "AI_CONFIG": {"_test_value": "new"},
        })

        persisted = json.loads(tmp_user_config.read_text(encoding="utf-8"))
        assert persisted["LLM_CONNECTIONS"] == original_connections
        assert persisted["OTHER_SETTING"] == {"preserve": True}
        assert persisted["AI_CONFIG"] == {"_test_value": "new"}
        assert result["message"] == "Config updated"

    def test_delete_referenced_connection_requires_force(
        self,
        tmp_user_config,
        tmp_runtime_paths,
        monkeypatch,
    ):
        from fastapi import HTTPException
        import routers.settings as settings_mod
        from butly_core.llm.connections import try_get_connection

        settings_mod.add_connection(settings_mod.ConnectionPayload(
            id="groq",
            protocol="openai_compat",
            base_url="https://api.groq.com/openai/v1",
        ))
        monkeypatch.setitem(
            settings_mod.AI_CONFIG,
            "_test_connection",
            {"connection": "groq", "model_name": "test-model"},
        )
        instance_dir = tmp_runtime_paths["instances_dir"] / "assistant"
        instance_dir.mkdir()
        (instance_dir / "config.json").write_text(
            json.dumps({
                "AI_CONFIG": {
                    "chat": {
                        "connection": "groq",
                        "model_name": "test-model",
                    }
                }
            }),
            encoding="utf-8",
        )

        with pytest.raises(HTTPException) as exc:
            settings_mod.delete_connection("groq")

        assert exc.value.status_code == 409
        assert exc.value.detail["message"].startswith("Connection 'groq'")
        references = exc.value.detail["references"]
        assert "AI_CONFIG._test_connection" in references
        assert any(ref.startswith("instance:assistant") for ref in references)
        assert try_get_connection("groq") is not None

        result = settings_mod.delete_connection("groq", force=True)

        assert "削除" in result["message"]
        assert try_get_connection("groq") is None
        persisted = json.loads(tmp_user_config.read_text(encoding="utf-8"))
        assert persisted["LLM_CONNECTIONS"] == []
