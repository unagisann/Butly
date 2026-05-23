"""
tests/test_connections.py
-------------------------
Connection / ConnectionRegistry の単体テスト。
"""

import os
from unittest.mock import patch

import pytest

from butly_core.llm.connections import (
    Connection,
    ConnectionRegistry,
    get_connection,
    get_registry,
    is_builtin_connection,
    list_connections,
    register_connection,
    try_get_connection,
)


# ===================================================================
# built-in connection 解決
# ===================================================================

class TestBuiltinConnections:
    def test_google_exists(self):
        c = get_connection("google")
        assert c.id == "google"
        assert c.protocol == "gemini_native"
        assert c.api_key_env == "GEMINI_API_KEY"
        assert "GOOGLE_API_KEY" in c.api_key_fallback_envs

    def test_openai_exists(self):
        c = get_connection("openai")
        assert c.protocol == "openai_compat"
        assert c.api_key_env == "OPENAI_API_KEY"
        assert c.embeddings_supported is True
        assert c.embedding_model_env == "OPENAI_EMBEDDING_MODEL"

    def test_xai_exists(self):
        c = get_connection("xai")
        assert c.protocol == "openai_compat"
        assert c.api_key_env == "XAI_API_KEY"
        assert c.embeddings_supported is False
        assert c.model_name_strip_prefix == "xai/"

    def test_ollama_exists(self):
        c = get_connection("ollama")
        assert c.protocol == "openai_compat"
        assert c.api_key_env is None
        assert c.model_name_strip_prefix == "ollama/"
        assert c.default_embedding_model == "nomic-embed-text"

    def test_unknown_raises(self):
        with pytest.raises(KeyError):
            get_connection("nonexistent_provider")

    def test_try_get_returns_none(self):
        assert try_get_connection("nonexistent_provider") is None

    def test_is_builtin(self):
        for cid in ("google", "openai", "xai", "ollama"):
            assert is_builtin_connection(cid) is True
        assert is_builtin_connection("custom_xyz") is False


# ===================================================================
# strip_model_prefix
# ===================================================================

class TestStripPrefix:
    def test_xai_strips(self):
        c = get_connection("xai")
        assert c.strip_model_prefix("xai/grok-4.3") == "grok-4.3"
        assert c.strip_model_prefix("grok-4.3") == "grok-4.3"

    def test_ollama_strips(self):
        c = get_connection("ollama")
        assert c.strip_model_prefix("ollama/llama3.2") == "llama3.2"
        assert c.strip_model_prefix("llama3.2") == "llama3.2"

    def test_openai_no_strip(self):
        c = get_connection("openai")
        assert c.strip_model_prefix("gpt-4o") == "gpt-4o"


# ===================================================================
# resolve_base_url / resolve_api_key
# ===================================================================

class TestResolve:
    def test_base_url_env_wins(self):
        c = get_connection("openai")
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "http://custom/v1"}):
            assert c.resolve_base_url() == "http://custom/v1"

    def test_base_url_default(self):
        c = get_connection("openai")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_BASE_URL", None)
            assert c.resolve_base_url() == "https://api.openai.com/v1"

    def test_api_key_primary_env(self):
        c = get_connection("google")
        with patch.dict(os.environ, {"GEMINI_API_KEY": "primary_key"}, clear=False):
            assert c.resolve_api_key() == "primary_key"

    def test_api_key_fallback_env(self):
        c = get_connection("google")
        # primary を消し、fallback だけセット
        env = {k: v for k, v in os.environ.items() if k not in ("GEMINI_API_KEY", "GOOGLE_API_KEY")}
        env["GOOGLE_API_KEY"] = "fallback_key"
        with patch.dict(os.environ, env, clear=True):
            assert c.resolve_api_key() == "fallback_key"

    def test_api_key_none_when_unset(self):
        c = get_connection("xai")
        env = {k: v for k, v in os.environ.items() if k != "XAI_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            assert c.resolve_api_key() is None

    def test_no_auth_connection_returns_none(self):
        c = get_connection("ollama")
        assert c.resolve_api_key() is None


# ===================================================================
# Register / unregister user-defined connections
# ===================================================================

class TestRegisterUserConnection:
    def setup_method(self):
        # 各テスト前に singleton をリセット
        get_registry().reset_to_builtin()

    def teardown_method(self):
        get_registry().reset_to_builtin()

    def test_register_new(self):
        groq = Connection(
            id="groq",
            protocol="openai_compat",
            base_url="https://api.groq.com/openai/v1",
            api_key_env="GROQ_API_KEY",
            label="Groq",
        )
        register_connection(groq)
        assert get_connection("groq").label == "Groq"

    def test_cannot_overwrite_builtin(self):
        bad = Connection(
            id="openai",
            protocol="openai_compat",
            base_url="https://malicious.example.com/v1",
        )
        with pytest.raises(ValueError, match="built-in"):
            register_connection(bad)

    def test_duplicate_user_requires_overwrite_flag(self):
        groq = Connection(id="groq", protocol="openai_compat")
        register_connection(groq)
        with pytest.raises(ValueError, match="already exists"):
            register_connection(groq)
        register_connection(groq, overwrite_user=True)  # OK

    def test_unregister_user(self):
        register_connection(Connection(id="groq", protocol="openai_compat"))
        get_registry().unregister("groq")
        assert try_get_connection("groq") is None

    def test_unregister_builtin_forbidden(self):
        with pytest.raises(ValueError):
            get_registry().unregister("openai")

    def test_list_includes_user_defined(self):
        register_connection(Connection(id="groq", protocol="openai_compat"))
        ids = {c.id for c in list_connections()}
        assert {"google", "openai", "xai", "ollama", "groq"}.issubset(ids)
