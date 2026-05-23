"""
tests/test_config_normalize.py
------------------------------
config.py の _normalize_ai_config() / _register_user_connections() 動作確認。

旧形式 (model_name only) → 新形式 (connection + model_name) の補完、
model_name と connection の不整合検出、user 定義 connection 登録など。
"""

import pytest

from butly_core.config import _normalize_ai_config, _register_user_connections
from butly_core.llm.connections import (
    get_registry,
    is_builtin_connection,
    try_get_connection,
)


@pytest.fixture(autouse=True)
def reset_registry():
    """テストごとに registry を built-in 状態に戻す。"""
    get_registry().reset_to_builtin()
    yield
    get_registry().reset_to_builtin()


# ===================================================================
# _normalize_ai_config: 旧形式 → 新形式
# ===================================================================

class TestNormalizeAiConfig:
    def test_legacy_chat_gets_connection_inferred(self):
        ai = {"chat": {"model_name": "gpt-4o"}}
        _normalize_ai_config(ai)
        assert ai["chat"]["connection"] == "openai"

    def test_legacy_gemini_gets_google(self):
        ai = {"chat": {"model_name": "gemini-3.5-flash"}}
        _normalize_ai_config(ai)
        assert ai["chat"]["connection"] == "google"

    def test_legacy_grok_gets_xai(self):
        ai = {"gatekeeper": {"model_name": "grok-4.3"}}
        _normalize_ai_config(ai)
        assert ai["gatekeeper"]["connection"] == "xai"

    def test_legacy_ollama_gets_ollama(self):
        ai = {"chat": {"model_name": "ollama/llama3.2"}}
        _normalize_ai_config(ai)
        assert ai["chat"]["connection"] == "ollama"

    def test_existing_connection_is_kept_when_matching(self):
        ai = {"chat": {"connection": "openai", "model_name": "gpt-4o"}}
        _normalize_ai_config(ai)
        assert ai["chat"]["connection"] == "openai"

    def test_mismatch_overridden_with_inferred(self):
        """user_config.json で model_name だけ書き換えたケース対応。

        AI_CONFIG default は connection=google だったが、user が
        model_name=gpt-4o に書き換えたら connection も openai になるべき。
        """
        ai = {"chat": {"connection": "google", "model_name": "gpt-4o"}}
        _normalize_ai_config(ai)
        assert ai["chat"]["connection"] == "openai"

    def test_user_connection_not_overridden(self):
        """user 定義 connection (built-in でない) は推定で握りつぶさない。

        例: connection=groq + model_name=llama-3-70b の場合、
        llama-3-70b は推定不能なのでそもそも上書き候補にならない。
        connection=groq + model_name=gpt-4o のような明示指定もユーザー意図として残す。
        """
        from butly_core.llm.connections import Connection, register_connection
        register_connection(
            Connection(id="groq", protocol="openai_compat",
                       base_url="https://api.groq.com/openai/v1",
                       api_key_env="GROQ_API_KEY")
        )
        ai = {"chat": {"connection": "groq", "model_name": "gpt-4o"}}
        _normalize_ai_config(ai)
        # user 定義 connection は built-in でないので保持される
        assert ai["chat"]["connection"] == "groq"

    def test_missing_model_name_skipped(self):
        ai = {"chat": {"generation_config": {"temperature": 0.7}}}
        _normalize_ai_config(ai)
        # model_name 不在 → スキップ (connection は付与されない)
        assert "connection" not in ai["chat"]

    def test_non_dict_role_skipped(self):
        ai = {"chat": "not a dict", "summary": {"model_name": "gpt-4o-mini"}}
        _normalize_ai_config(ai)
        assert ai["summary"]["connection"] == "openai"
        assert ai["chat"] == "not a dict"

    def test_unknown_model_left_alone(self):
        ai = {"chat": {"model_name": "completely-unknown-xyz"}}
        _normalize_ai_config(ai)
        # 推定不能 → connection 未設定のまま (ログだけ出る)
        assert "connection" not in ai["chat"]

    def test_multiple_roles_normalized(self):
        ai = {
            "chat": {"model_name": "gpt-4o"},
            "summary": {"model_name": "gemini-3.1-flash-lite"},
            "embedding": {"model_name": "text-embedding-3-small"},
            "gatekeeper": {"connection": "openai", "model_name": "gpt-4o-mini"},
        }
        _normalize_ai_config(ai)
        assert ai["chat"]["connection"] == "openai"
        assert ai["summary"]["connection"] == "google"
        assert ai["embedding"]["connection"] == "openai"
        assert ai["gatekeeper"]["connection"] == "openai"


# ===================================================================
# _register_user_connections
# ===================================================================

class TestRegisterUserConnections:
    def test_register_single_connection(self):
        _register_user_connections([
            {
                "id": "groq",
                "protocol": "openai_compat",
                "base_url": "https://api.groq.com/openai/v1",
                "api_key_env": "GROQ_API_KEY",
                "label": "Groq",
            }
        ])
        conn = try_get_connection("groq")
        assert conn is not None
        assert conn.protocol == "openai_compat"
        assert conn.base_url == "https://api.groq.com/openai/v1"
        assert conn.label == "Groq"

    def test_register_multiple(self):
        _register_user_connections([
            {"id": "groq", "protocol": "openai_compat", "base_url": "https://api.groq.com/openai/v1", "api_key_env": "GROQ_API_KEY"},
            {"id": "together", "protocol": "openai_compat", "base_url": "https://api.together.xyz/v1", "api_key_env": "TOGETHER_API_KEY"},
        ])
        assert try_get_connection("groq") is not None
        assert try_get_connection("together") is not None

    def test_register_skips_builtin_overwrite(self):
        before = try_get_connection("openai")
        _register_user_connections([
            {
                "id": "openai",  # built-in 上書き試行
                "protocol": "openai_compat",
                "base_url": "https://evil.example.com/v1",
            }
        ])
        after = try_get_connection("openai")
        assert after is before  # 変わっていない
        assert after.base_url == "https://api.openai.com/v1"

    def test_register_skips_missing_required_fields(self):
        _register_user_connections([
            {"protocol": "openai_compat"},  # id 欠落
            {"id": "incomplete"},            # protocol 欠落
        ])
        assert try_get_connection("incomplete") is None

    def test_register_handles_invalid_list_type(self):
        # list 以外 → ログ出力のみで no-op
        _register_user_connections({"id": "groq"})  # dict instead of list
        assert try_get_connection("groq") is None

    def test_register_with_all_optional_fields(self):
        _register_user_connections([
            {
                "id": "openrouter",
                "protocol": "openai_compat",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_API_KEY",
                "label": "OpenRouter",
                "embeddings_supported": False,
                "extra_headers": {"HTTP-Referer": "https://butly.local"},
            }
        ])
        conn = try_get_connection("openrouter")
        assert conn is not None
        assert conn.embeddings_supported is False
        assert conn.extra_headers == {"HTTP-Referer": "https://butly.local"}
