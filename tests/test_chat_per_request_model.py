"""
tests/test_chat_per_request_model.py
------------------------------------
Phase 3: REST / WebSocket payload から model_name / connection が
ChatService まで届くことを確認する。
"""

import pytest


# ===================================================================
# WebSocket: normalize_ws_payload
# ===================================================================

class TestNormalizeWsPayloadModelOverride:
    def test_model_name_only_propagates(self):
        from butly_core.chat.types import normalize_ws_payload
        req = normalize_ws_payload({
            "text": "hi",
            "instance_name": "00_master",
            "model_name": "gpt-4o",
        })
        assert req.model_name == "gpt-4o"
        assert req.connection is None

    def test_connection_only_propagates(self):
        from butly_core.chat.types import normalize_ws_payload
        req = normalize_ws_payload({
            "text": "hi",
            "instance_name": "00_master",
            "connection": "openai",
        })
        assert req.connection == "openai"
        assert req.model_name is None

    def test_both_propagate(self):
        from butly_core.chat.types import normalize_ws_payload
        req = normalize_ws_payload({
            "text": "hi",
            "instance_name": "00_master",
            "model_name": "llama-3.3-70b",
            "connection": "groq",
        })
        assert req.model_name == "llama-3.3-70b"
        assert req.connection == "groq"

    def test_missing_fields_default_to_none(self):
        from butly_core.chat.types import normalize_ws_payload
        req = normalize_ws_payload({"text": "hi"})
        assert req.model_name is None
        assert req.connection is None

    def test_empty_string_treated_as_none(self):
        """空文字列は per-request override 意図がないとみなして None 扱い。"""
        from butly_core.chat.types import normalize_ws_payload
        req = normalize_ws_payload({"text": "hi", "model_name": "", "connection": ""})
        assert req.model_name is None
        assert req.connection is None

    def test_legacy_string_payload_no_model(self):
        """旧形式 (text 文字列) は model_name/connection なしのまま動く。"""
        from butly_core.chat.types import normalize_ws_payload
        req = normalize_ws_payload("hi")
        assert req.text == "hi"
        assert req.model_name is None
        assert req.connection is None


# ===================================================================
# REST /chat: Pydantic ChatRequest
# ===================================================================

class TestRestChatRequestPerRequestModel:
    def test_pydantic_chat_request_accepts_model_and_connection(self):
        from routers.chat import ChatRequest
        r = ChatRequest(
            message="hello",
            instance_name="00_master",
            model_name="gpt-4o",
            connection="openai",
        )
        assert r.model_name == "gpt-4o"
        assert r.connection == "openai"

    def test_pydantic_chat_request_defaults_to_none(self):
        from routers.chat import ChatRequest
        r = ChatRequest(message="hello")
        assert r.model_name is None
        assert r.connection is None

    def test_internal_request_built_with_overrides(self):
        """REST が ChatRequest → _InternalChatRequest 変換時に override を保つことを
        ChatService 視点で確認する。"""
        from routers.chat import ChatRequest
        from butly_core.chat.types import ChatRequest as InternalReq

        r = ChatRequest(
            message="hi", instance_name="x",
            model_name="grok-4.20-0309-reasoning",
            connection="xai",
        )
        internal = InternalReq(
            text=r.message,
            attachments=[],
            instance_name=r.instance_name,
            use_rag=r.use_rag,
            use_google_search=r.use_google_search,
            use_web_search=r.use_web_search,
            model_name=r.model_name,
            connection=r.connection,
        )
        assert internal.model_name == "grok-4.20-0309-reasoning"
        assert internal.connection == "xai"


# ===================================================================
# ChatService 連携: model_name + connection → ModelRef 解決
# ===================================================================

class TestPerRequestRoutesToProvider:
    """request.model_name / connection が _resolve_chat_model_ref を経由して
    ModelRef を正しく組み立てることを確認する。"""

    def test_request_model_only_reinfers(self):
        from butly_core.chat.service import _resolve_chat_model_ref
        from butly_core.chat.types import ChatRequest as CR
        ref = _resolve_chat_model_ref(
            instance_config={},
            request=CR(text="x", instance_name="t", model_name="gpt-4o"),
            ai_config_chat={"connection": "google", "model_name": "gemini-3.5-flash"},
        )
        assert ref.connection_id == "openai"
        assert ref.model_name == "gpt-4o"

    def test_request_groq_with_explicit_connection(self):
        from butly_core.chat.service import _resolve_chat_model_ref
        from butly_core.chat.types import ChatRequest as CR
        from butly_core.llm.connections import (
            Connection, get_registry, register_connection,
        )
        get_registry().reset_to_builtin()
        try:
            register_connection(
                Connection(
                    id="groq", protocol="openai_compat",
                    base_url="https://api.groq.com/openai/v1",
                    api_key_env="GROQ_API_KEY",
                )
            )
            ref = _resolve_chat_model_ref(
                instance_config={},
                request=CR(
                    text="x", instance_name="t",
                    model_name="llama-3.3-70b-versatile",
                    connection="groq",
                ),
                ai_config_chat={"connection": "google", "model_name": "gemini-3.5-flash"},
            )
            assert ref.connection_id == "groq"
            assert ref.model_name == "llama-3.3-70b-versatile"
        finally:
            get_registry().reset_to_builtin()
