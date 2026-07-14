"""
test_xai_provider.py
--------------------
xAI (Grok) Provider のユニットテスト。
API 呼び出しはモックで代替。
"""

import os
from unittest.mock import patch, MagicMock

import pytest

from butly_core.llm.providers.xai import XaiProvider, _NON_VISION_PREFIXES


# ===================================================================
# supports_vision
# ===================================================================

class TestXaiSupportsVision:
    """xAI Vision 判定テスト。"""

    def test_grok4_has_vision(self):
        assert XaiProvider.supports_vision("grok-4.20-reasoning") is True

    def test_grok4_fast_has_vision(self):
        assert XaiProvider.supports_vision("grok-4-1-fast-reasoning") is True

    def test_grok_code_fast_no_vision(self):
        assert XaiProvider.supports_vision("grok-code-fast-1") is False

    def test_xai_prefix_has_vision(self):
        assert XaiProvider.supports_vision("xai/grok-4.20") is True

    def test_xai_prefix_code_no_vision(self):
        assert XaiProvider.supports_vision("xai/grok-code-fast-1") is False


# ===================================================================
# embed
# ===================================================================

class TestXaiEmbed:
    """xAI embedding テスト: 常に None を返す。"""

    def test_embed_returns_none(self):
        provider = XaiProvider()
        result = provider.embed("some text")
        assert result is None


# ===================================================================
# classify (モック)
# ===================================================================

class TestXaiClassify:
    """classify テスト。"""

    @patch("butly_core.llm.providers.xai._get_client")
    def test_classify_normal(self, mock_get_client):
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content='{"tier": "mid"}'))]
        mock_client.chat.completions.create.return_value = mock_resp
        mock_get_client.return_value = mock_client

        provider = XaiProvider()
        result = provider.classify("classify this", {"model_name": "grok-4-1-fast-non-reasoning"})
        assert '{"tier": "mid"}' in result

    @patch("butly_core.llm.providers.xai._get_client")
    def test_classify_error_raises(self, mock_get_client):
        """エラーは握りつぶさず送出する（呼び出し側が provider_error として fallback）"""
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception("API error")
        mock_get_client.return_value = mock_client

        provider = XaiProvider()
        with pytest.raises(Exception, match="API error"):
            provider.classify("classify this", {"model_name": "grok-4-1-fast-non-reasoning"})


# ===================================================================
# generate (モック)
# ===================================================================

class TestXaiGenerate:
    """generate テスト。"""

    @patch("butly_core.llm.providers.xai._get_client")
    @patch("butly_core.core.gatekeeper.build_system_instruction_from_blocks", return_value="sys")
    @patch("butly_core.core.gatekeeper.build_context_prefix", return_value="prefix")
    def test_generate_basic(self, mock_prefix, mock_sys, mock_get_client):
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="Hello from Grok!"))]
        mock_client.chat.completions.create.return_value = mock_resp
        mock_get_client.return_value = mock_client

        provider = XaiProvider()
        from butly_core.chat.types import ChatResponse
        with patch("butly_core.config.AI_CONFIG", {"chat": {"model_name": "grok-4.20-reasoning", "generation_config": {"temperature": 0.7}}}):
            result = provider.generate("test", [], {"history": [], "rag_results": []})
        assert isinstance(result, ChatResponse)
        assert result.text == "Hello from Grok!"

    @patch("butly_core.llm.providers.xai._get_client")
    @patch("butly_core.core.gatekeeper.build_system_instruction_from_blocks", return_value="sys")
    @patch("butly_core.core.gatekeeper.build_context_prefix", return_value="")
    def test_generate_error(self, mock_prefix, mock_sys, mock_get_client):
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception("Timeout")
        mock_get_client.return_value = mock_client

        provider = XaiProvider()
        with patch("butly_core.config.AI_CONFIG", {"chat": {"model_name": "grok-4.20-reasoning", "generation_config": {"temperature": 0.7}}}):
            result = provider.generate("test", [], {"history": [], "rag_results": []})
        assert "Error" in result.text


# ===================================================================
# Factory 経由生成
# ===================================================================

class TestXaiFactory:
    """Factory 経由の xAI Provider 生成テスト。"""

    def test_grok_prefix(self):
        from butly_core.llm.factory import ProviderFactory
        provider = ProviderFactory.create("grok-4.20-reasoning")
        assert isinstance(provider, XaiProvider)

    def test_xai_prefix(self):
        from butly_core.llm.factory import ProviderFactory
        provider = ProviderFactory.create("xai/grok-4.20")
        assert isinstance(provider, XaiProvider)
