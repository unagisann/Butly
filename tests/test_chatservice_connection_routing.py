"""
tests/test_chatservice_connection_routing.py
--------------------------------------------
ChatService の Phase 2 ModelRef ルーティング動作検証。

検証項目:
  - _resolve_chat_model_ref の優先順位: request > instance > AI_CONFIG
  - request.model_name のみ指定時に connection が再推定されること
  - request.connection 単独指定で AI_CONFIG.model_name が維持されること
  - Web search 分岐が connection_id == "google" 判定で動くこと
  - LLM_CONNECTIONS に Groq を追加して chat に割り当てた場合に動くこと
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from butly_core.chat.service import _resolve_chat_model_ref, _uses_google_connection
from butly_core.chat.types import ChatRequest
from butly_core.llm.connections import (
    Connection,
    get_registry,
    register_connection,
    try_get_connection,
)
from butly_core.llm.model_registry import ModelRef


@pytest.fixture(autouse=True)
def reset_registry():
    get_registry().reset_to_builtin()
    yield
    get_registry().reset_to_builtin()


# ===================================================================
# _resolve_chat_model_ref: 優先順位
# ===================================================================

class TestResolveChatModelRef:
    def _make_request(self, model_name=None, connection=None):
        return ChatRequest(
            text="hi",
            instance_name="t",
            model_name=model_name,
            connection=connection,
        )

    def test_ai_config_only(self):
        ref = _resolve_chat_model_ref(
            instance_config={},
            request=self._make_request(),
            ai_config_chat={"connection": "google", "model_name": "gemini-3.5-flash"},
        )
        assert ref.connection_id == "google"
        assert ref.model_name == "gemini-3.5-flash"

    def test_instance_overrides_ai_config(self):
        ref = _resolve_chat_model_ref(
            instance_config={"chat": {"connection": "openai", "model_name": "gpt-4o"}},
            request=self._make_request(),
            ai_config_chat={"connection": "google", "model_name": "gemini-3.5-flash"},
        )
        assert ref.connection_id == "openai"
        assert ref.model_name == "gpt-4o"

    def test_instance_model_name_only_reinfers_connection(self):
        """instance_config が旧形式 model_name only なら AI_CONFIG の connection を引き継がない。"""
        ref = _resolve_chat_model_ref(
            instance_config={"chat": {"model_name": "grok-4.20-0309-non-reasoning"}},
            request=self._make_request(),
            ai_config_chat={"connection": "google", "model_name": "gemini-3.5-flash"},
        )
        assert ref.connection_id == "xai"
        assert ref.model_name == "grok-4.20-0309-non-reasoning"

    def test_request_model_name_only_reinfers_connection(self):
        """request.model_name だけ指定 → 古い connection を捨てて推定し直す。"""
        ref = _resolve_chat_model_ref(
            instance_config={"chat": {"connection": "google", "model_name": "gemini-3.5-flash"}},
            request=self._make_request(model_name="gpt-4o"),
            ai_config_chat={"connection": "google", "model_name": "gemini-3.5-flash"},
        )
        assert ref.connection_id == "openai"  # 推定し直した
        assert ref.model_name == "gpt-4o"

    def test_request_both_connection_and_model(self):
        ref = _resolve_chat_model_ref(
            instance_config={"chat": {"connection": "google", "model_name": "gemini-3.5-flash"}},
            request=self._make_request(connection="xai", model_name="grok-4.3"),
            ai_config_chat={"connection": "google", "model_name": "gemini-3.5-flash"},
        )
        assert ref.connection_id == "xai"
        assert ref.model_name == "grok-4.3"

    def test_request_connection_only_keeps_ai_config_model(self):
        """request.connection 単独指定 → model_name は instance/AI_CONFIG から。"""
        ref = _resolve_chat_model_ref(
            instance_config={"chat": {"connection": "openai", "model_name": "gpt-4o"}},
            request=self._make_request(connection="openai"),
            ai_config_chat={"connection": "google", "model_name": "gemini-3.5-flash"},
        )
        assert ref.connection_id == "openai"
        assert ref.model_name == "gpt-4o"

    def test_request_with_user_connection(self):
        """user 定義 connection (Groq) を request で指定。"""
        register_connection(
            Connection(id="groq", protocol="openai_compat",
                       base_url="https://api.groq.com/openai/v1",
                       api_key_env="GROQ_API_KEY")
        )
        ref = _resolve_chat_model_ref(
            instance_config={},
            request=self._make_request(connection="groq", model_name="llama-3.3-70b-versatile"),
            ai_config_chat={"connection": "google", "model_name": "gemini-3.5-flash"},
        )
        assert ref.connection_id == "groq"
        assert ref.model_name == "llama-3.3-70b-versatile"


# ===================================================================
# Web search routing
# ===================================================================

class TestUsesGoogleConnection:
    def test_google_returns_true(self):
        assert _uses_google_connection("google") is True

    def test_openai_returns_false(self):
        assert _uses_google_connection("openai") is False

    def test_xai_returns_false(self):
        assert _uses_google_connection("xai") is False

    def test_user_connection_returns_false(self):
        assert _uses_google_connection("groq") is False


# ===================================================================
# Factory routing for Groq via config
# ===================================================================

class TestGroqViaConfig:
    """LLM_CONNECTIONS に Groq を登録 → factory 経由で OpenAICompatAdapter が動く。"""

    def test_register_then_factory(self):
        from butly_core.config import _register_user_connections
        from butly_core.llm.factory import ProviderFactory
        from butly_core.llm.protocols.openai_compat import OpenAICompatAdapter

        _register_user_connections([
            {
                "id": "groq",
                "protocol": "openai_compat",
                "base_url": "https://api.groq.com/openai/v1",
                "api_key_env": "GROQ_API_KEY",
                "label": "Groq",
                "embeddings_supported": False,
            }
        ])

        # connection を明示してモデルを呼ぶ
        provider = ProviderFactory.create({
            "connection": "groq",
            "model_name": "llama-3.3-70b-versatile",
        })
        assert isinstance(provider, OpenAICompatAdapter)
        # built-in シムには該当しない
        from butly_core.llm.providers.openai import OpenAIProvider
        from butly_core.llm.providers.xai import XaiProvider
        assert not isinstance(provider, (OpenAIProvider, XaiProvider))

    def test_groq_classify_via_mocked_client(self):
        """Groq connection で classify が呼び出せること (mock 経由)。"""
        from butly_core.config import _register_user_connections
        from butly_core.llm.factory import ProviderFactory

        _register_user_connections([
            {
                "id": "groq",
                "protocol": "openai_compat",
                "base_url": "https://api.groq.com/openai/v1",
                "api_key_env": "GROQ_API_KEY",
            }
        ])
        provider = ProviderFactory.create({
            "connection": "groq",
            "model_name": "llama-3.3-70b-versatile",
        })

        # mock client を注入
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="from-groq"))]
        )
        provider._client = mock_client

        result = provider.classify("hi", {
            "connection": "groq",
            "model_name": "llama-3.3-70b-versatile",
        })
        assert result == "from-groq"

        args, kwargs = mock_client.chat.completions.create.call_args
        # Groq には prefix strip なし
        assert kwargs["model"] == "llama-3.3-70b-versatile"


# ===================================================================
# ChatService execute() end-to-end (connection routing)
# ===================================================================

def _run_async(coro):
    return asyncio.run(coro)


class TestChatServiceConnectionInDebugInfo:
    """ChatService.execute() / execute_stream() の debug_info に
    connection_id が記録されることを確認する。
    """

    def _make_env(self, tmp_path):
        from butly_core.chat.types import ChatRequest as CR
        instances_dir = tmp_path / "instances"
        (instances_dir / "TestInst").mkdir(parents=True)

        memory = MagicMock()
        memory.get_last_interaction_time.return_value = None
        memory.load_recent_sessions.return_value = ([], None)
        brain = MagicMock()
        chronos = MagicMock()
        chronos.get_system_note.return_value = "TIME"

        instance_manager = MagicMock()
        instance_manager.get_instance_config.return_value = {
            "chat": {"connection": "openai", "model_name": "gpt-4o"},
            "brain": {"use_rag": False},
            "gatekeeper": {"enabled": True},
        }

        gatekeeper = MagicMock()
        gatekeeper.classify.return_value = {
            "tier": "mid", "topic": "", "need": None, "need_intent": None,
            "search_targets": None, "state_delta": {}, "llm_scoring": {},
            "memory_probe": {"status": "skipped", "candidates": [], "glossary_hits": []},
        }
        gatekeeper.update_state.return_value = {}

        mem_builder = MagicMock()
        mem_builder.build.return_value = {"tier": "mid", "topic": "t", "short_term": []}

        req = CR(text="hello", instance_name="TestInst")

        return {
            "instances_dir": instances_dir,
            "components": {"memory": memory, "brain": brain, "chronos": chronos},
            "instance_manager": instance_manager,
            "gatekeeper": gatekeeper,
            "mem_builder": mem_builder,
            "req": req,
        }

    def test_execute_records_connection_id(self, tmp_path):
        from butly_core.chat.service import ChatService
        from butly_core.chat.types import ChatResponse

        env = self._make_env(tmp_path)

        provider = MagicMock()
        provider.supports_vision = MagicMock(return_value=True)
        provider.generate.return_value = ChatResponse(text="ok", debug_info={})

        session_state_mock = MagicMock()
        session_state_mock.to_dict.return_value = {"topic": "", "mood": "n", "turn_count": 0}

        with patch("butly_core.chat.service.ProviderFactory") as PF, \
             patch("butly_core.core.gatekeeper.SessionState", return_value=session_state_mock):
            PF.create.return_value = provider
            result = _run_async(ChatService.execute(
                request=env["req"],
                get_instance_components=lambda _n: env["components"],
                instance_manager=env["instance_manager"],
                instances_dir=env["instances_dir"],
                gatekeeper=env["gatekeeper"],
                mem_block_builder=env["mem_builder"],
            ))

        # debug_info に connection_id が記録されている
        assert result.debug_info["connection_id"] == "openai"
        assert result.debug_info["model"] == "gpt-4o"

    def test_execute_request_model_name_overrides(self, tmp_path):
        """request.model_name が指定されたら connection 再推定される。"""
        from butly_core.chat.service import ChatService
        from butly_core.chat.types import ChatRequest as CR, ChatResponse

        env = self._make_env(tmp_path)
        # AI_CONFIG / instance は openai/gpt-4o だが、request で xai/grok-4.3
        env["req"] = CR(
            text="hi", instance_name="TestInst",
            model_name="grok-4.3",  # connection 省略 → xai に推定されるはず
        )

        provider = MagicMock()
        provider.supports_vision = MagicMock(return_value=True)
        provider.generate.return_value = ChatResponse(text="ok", debug_info={})

        session_state_mock = MagicMock()
        session_state_mock.to_dict.return_value = {"topic": "", "mood": "n", "turn_count": 0}

        with patch("butly_core.chat.service.ProviderFactory") as PF, \
             patch("butly_core.core.gatekeeper.SessionState", return_value=session_state_mock):
            PF.create.return_value = provider
            result = _run_async(ChatService.execute(
                request=env["req"],
                get_instance_components=lambda _n: env["components"],
                instance_manager=env["instance_manager"],
                instances_dir=env["instances_dir"],
                gatekeeper=env["gatekeeper"],
                mem_block_builder=env["mem_builder"],
            ))

        assert result.debug_info["connection_id"] == "xai"
        assert result.debug_info["model"] == "grok-4.3"
