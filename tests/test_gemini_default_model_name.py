"""
tests/test_gemini_default_model_name.py
---------------------------------------
Phase 3: GeminiProvider が default_model_name (ProviderFactory.create(ModelRef) 経由)
を受け取り、_start_chat の API 呼び出しに反映することを確認する。

Phase 2 までは AI_CONFIG["chat"]["model_name"] 固定で、UI/request の per-request
切替が無効化されていた。Phase 3 でこのバグを修正したことの回帰防止。
"""

from unittest.mock import MagicMock, patch

import pytest


class TestGeminiProviderDefaultModelName:
    def test_constructor_accepts_default_model_name(self):
        from butly_core.llm.providers.gemini import GeminiProvider
        p = GeminiProvider(default_model_name="gemini-2.5-pro")
        assert p.default_model_name == "gemini-2.5-pro"

    def test_constructor_without_args_is_backward_compatible(self):
        from butly_core.llm.providers.gemini import GeminiProvider
        p = GeminiProvider()
        assert p.default_model_name is None

    def test_start_chat_uses_default_model_name(self):
        """ProviderFactory(ModelRef("google","gemini-2.5-pro")) で送る前提。"""
        from butly_core.llm.providers.gemini import GeminiProvider

        p = GeminiProvider(default_model_name="gemini-2.5-pro")

        mock_client = MagicMock()
        mock_client.chats.create.return_value = MagicMock()
        p._client = mock_client

        # gemini._start_chat は butly_core.core.gatekeeper からの遅延 import なので
        # そちらを差し替える。
        with patch("butly_core.core.gatekeeper.build_system_instruction_from_blocks",
                   return_value="SI"), \
             patch("butly_core.core.gatekeeper.build_context_prefix",
                   return_value=""):
            p._start_chat(history=[], memory_manager=MagicMock())

        # client.chats.create に渡された model 名を確認
        args, kwargs = mock_client.chats.create.call_args
        assert kwargs.get("model") == "gemini-2.5-pro"

    def test_start_chat_falls_back_to_ai_config_when_no_default(self):
        from butly_core.llm.providers.gemini import GeminiProvider

        p = GeminiProvider()  # default なし
        mock_client = MagicMock()
        mock_client.chats.create.return_value = MagicMock()
        p._client = mock_client

        with patch("butly_core.core.gatekeeper.build_system_instruction_from_blocks",
                   return_value="SI"), \
             patch("butly_core.core.gatekeeper.build_context_prefix",
                   return_value=""):
            p._start_chat(history=[], memory_manager=MagicMock())

        # default なし → AI_CONFIG["chat"]["model_name"] にフォールバック
        from butly_core.config import AI_CONFIG
        expected = AI_CONFIG["chat"]["model_name"]
        args, kwargs = mock_client.chats.create.call_args
        assert kwargs.get("model") == expected


class TestFactoryPropagatesModelRefToGemini:
    def test_factory_passes_model_name_to_gemini(self):
        """ProviderFactory.create(ModelRef("google", "gemini-2.5-pro")) → default_model_name 設定。"""
        from butly_core.llm.factory import ProviderFactory
        from butly_core.llm.model_registry import ModelRef
        from butly_core.llm.providers.gemini import GeminiProvider

        provider = ProviderFactory.create(ModelRef(
            connection_id="google", model_name="gemini-2.5-pro",
        ))
        assert isinstance(provider, GeminiProvider)
        assert provider.default_model_name == "gemini-2.5-pro"

    def test_factory_passes_model_name_via_dict(self):
        from butly_core.llm.factory import ProviderFactory
        from butly_core.llm.providers.gemini import GeminiProvider

        provider = ProviderFactory.create({
            "connection": "google",
            "model_name": "gemini-3.5-flash",
        })
        assert isinstance(provider, GeminiProvider)
        assert provider.default_model_name == "gemini-3.5-flash"
