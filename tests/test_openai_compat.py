"""
test_openai_compat.py
---------------------
_openai_compat ヘルパー関数のユニットテスト。
"""

import os
from unittest.mock import patch, MagicMock

import pytest

from butly_core.llm._openai_compat import (
    is_reasoning_model,
    resolve_position,
    resolve_system_instruction,
    resolve_context_prefix,
    build_user_content,
    build_messages,
    convert_history,
    build_debug_messages,
    build_full_messages,
    extract_token_usage,
    merge_chat_config,
    build_chat_completion_kwargs,
    build_chat_response,
    load_env_file,
)
from butly_core.chat.types import Attachment, ChatResponse


# ===================================================================
# is_reasoning_model
# ===================================================================

class TestIsReasoningModel:
    """reasoning モデル判定のテスト（B1 対応）。"""

    def test_o1_is_reasoning(self):
        assert is_reasoning_model("o1") is True

    def test_o1_mini_is_reasoning(self):
        assert is_reasoning_model("o1-mini") is True

    def test_o3_is_reasoning(self):
        assert is_reasoning_model("o3") is True

    def test_o3_mini_is_reasoning(self):
        assert is_reasoning_model("o3-mini") is True

    def test_o4_mini_is_reasoning(self):
        assert is_reasoning_model("o4-mini") is True

    def test_gpt4o_is_not_reasoning(self):
        assert is_reasoning_model("gpt-4o") is False

    def test_gpt4o_mini_is_not_reasoning(self):
        assert is_reasoning_model("gpt-4o-mini") is False

    def test_grok4_is_not_reasoning(self):
        assert is_reasoning_model("grok-4.20-reasoning") is False

    def test_gemini_is_not_reasoning(self):
        assert is_reasoning_model("gemini-3-flash-preview") is False

    def test_ollama_is_not_reasoning(self):
        assert is_reasoning_model("ollama/llama3.2") is False


# ===================================================================
# resolve_position
# ===================================================================

class TestResolvePosition:
    """system_instruction 配置位置の解決テスト。"""

    def test_context_levels_top(self):
        ctx = {"context_levels": {"system_instruction_position": "top"}}
        assert resolve_position(ctx) == "top"

    def test_context_levels_bottom(self):
        ctx = {"context_levels": {"system_instruction_position": "bottom"}}
        assert resolve_position(ctx) == "bottom"

    def test_context_order_fallback(self):
        ctx = {"context_order": {"system_instruction_position": "bottom"}}
        assert resolve_position(ctx) == "bottom"

    def test_default_is_top(self):
        assert resolve_position({}) == "top"

    def test_context_levels_takes_priority(self):
        ctx = {
            "context_levels": {"system_instruction_position": "top"},
            "context_order": {"system_instruction_position": "bottom"},
        }
        assert resolve_position(ctx) == "top"


# ===================================================================
# resolve_system_instruction / resolve_context_prefix
# ===================================================================

class TestResolveInstructionAndPrefix:
    """system_instruction / context_prefix 構築テスト。"""

    @patch("butly_core.core.gatekeeper.build_system_instruction_from_blocks")
    def test_resolve_system_instruction(self, mock_build):
        mock_build.return_value = "sys_inst"
        ctx = {"memory_blocks": {"a": 1}, "memory_manager": "mgr"}
        result = resolve_system_instruction(ctx)
        assert result == "sys_inst"
        mock_build.assert_called_once()

    @patch("butly_core.core.gatekeeper.build_context_prefix")
    def test_resolve_context_prefix(self, mock_build):
        mock_build.return_value = "ctx_prefix"
        ctx = {"memory_blocks": {"a": 1}, "memory_manager": "mgr"}
        result = resolve_context_prefix(ctx)
        assert result == "ctx_prefix"


# ===================================================================
# build_user_content
# ===================================================================

class TestBuildUserContent:
    """ユーザーコンテンツ構築テスト。"""

    def test_text_only(self):
        result = build_user_content("hello", [])
        assert result == "hello"

    def test_with_attachments(self):
        att = Attachment(mime_type="image/jpeg", data_base64="abc123")
        result = build_user_content("hello", [att])
        assert isinstance(result, list)
        assert result[0] == {"type": "text", "text": "hello"}
        assert result[1]["type"] == "image_url"
        assert "data:image/jpeg;base64,abc123" in result[1]["image_url"]["url"]


# ===================================================================
# build_messages
# ===================================================================

class TestBuildMessages:
    """messages 配列構築テスト。"""

    def test_top_position(self):
        msgs = build_messages(
            system_instruction="sys",
            context_prefix="prefix",
            history=[{"role": "user", "parts": ["hi"]}, {"role": "model", "parts": ["hello"]}],
            user_content="question",
            position="top",
        )
        assert msgs[0] == {"role": "system", "content": "sys"}
        assert msgs[1] == {"role": "user", "content": "prefix"}
        assert msgs[-1] == {"role": "user", "content": "question"}

    def test_bottom_position(self):
        msgs = build_messages(
            system_instruction="sys",
            context_prefix="prefix",
            history=[],
            user_content="question",
            position="bottom",
        )
        assert msgs[0] == {"role": "system", "content": "prefix"}
        assert msgs[1] == {"role": "system", "content": "sys"}
        assert msgs[-1] == {"role": "user", "content": "question"}

    def test_no_prefix(self):
        msgs = build_messages(
            system_instruction="sys",
            context_prefix="",
            history=[],
            user_content="question",
            position="top",
        )
        assert len(msgs) == 2  # sys + user
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"


# ===================================================================
# convert_history
# ===================================================================

class TestConvertHistory:
    """履歴変換テスト。"""

    def test_basic_conversion(self):
        history = [
            {"role": "user", "parts": ["hi"]},
            {"role": "model", "parts": ["hello"]},
        ]
        result = convert_history(history)
        assert result == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]

    def test_empty_parts(self):
        history = [{"role": "user", "parts": []}]
        result = convert_history(history)
        assert result[0]["content"] == ""


# ===================================================================
# build_debug_messages
# ===================================================================

class TestBuildDebugMessages:
    """デバッグメッセージの truncate テスト。"""

    def test_short_message(self):
        msgs = [{"role": "user", "content": "short"}]
        result = build_debug_messages(msgs)
        assert result[0]["content"] == "short"

    def test_long_message_truncated(self):
        long_text = "x" * 1000
        msgs = [{"role": "user", "content": long_text}]
        result = build_debug_messages(msgs, max_len=500)
        assert len(result[0]["content"]) <= 503  # 500 + "..."

    def test_multimodal_content(self):
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "hello"},
            {"type": "image_url", "image_url": {"url": "data:..."}},
        ]}]
        result = build_debug_messages(msgs)
        assert "hello" in result[0]["content"]


# ===================================================================
# merge_chat_config
# ===================================================================

class TestMergeChatConfig:
    """chat 設定マージテスト。"""

    def test_no_override(self):
        base = {"model_name": "gpt-4o", "generation_config": {"temperature": 0.7}}
        result = merge_chat_config(base, None)
        assert result == base

    def test_with_override(self):
        base = {"model_name": "gpt-4o"}
        override = {"chat": {"model_name": "o3"}}
        result = merge_chat_config(base, override)
        assert result["model_name"] == "o3"

    def test_override_without_chat_key(self):
        base = {"model_name": "gpt-4o"}
        override = {"summary": {"model_name": "gpt-4o-mini"}}
        result = merge_chat_config(base, override)
        assert result["model_name"] == "gpt-4o"


# ===================================================================
# build_chat_completion_kwargs (B1 対応)
# ===================================================================

class TestBuildChatCompletionKwargs:
    """API 呼び出しパラメータ構築テスト（reasoning 分岐含む）。"""

    def test_normal_model_includes_temperature(self):
        conf = {"generation_config": {"temperature": 0.7, "max_output_tokens": 8192}}
        kwargs = build_chat_completion_kwargs(conf, [{"role": "user", "content": "hi"}])
        assert "temperature" in kwargs
        assert "max_tokens" in kwargs
        assert "reasoning_effort" not in kwargs

    def test_reasoning_model_excludes_temperature(self):
        conf = {
            "model_name": "o3",
            "generation_config": {"temperature": 0.7, "max_output_tokens": 8192},
            "reasoning_effort": "medium",
        }
        kwargs = build_chat_completion_kwargs(conf, [{"role": "user", "content": "hi"}], model_name="o3")
        assert "temperature" not in kwargs
        assert "max_tokens" not in kwargs
        assert kwargs["reasoning_effort"] == "medium"
        assert "max_completion_tokens" in kwargs

    def test_reasoning_model_default_effort(self):
        """reasoning_effort 未指定時はデフォルト medium。"""
        conf = {"generation_config": {"max_output_tokens": 4096}}
        kwargs = build_chat_completion_kwargs(conf, [{"role": "user", "content": "hi"}], model_name="o3")
        assert kwargs["reasoning_effort"] == "medium"
        assert "temperature" not in kwargs

    def test_o4_mini_is_reasoning(self):
        conf = {"generation_config": {"max_output_tokens": 4096}, "reasoning_effort": "low"}
        kwargs = build_chat_completion_kwargs(conf, [{"role": "user", "content": "hi"}], model_name="o4-mini")
        assert kwargs["reasoning_effort"] == "low"
        assert "temperature" not in kwargs

    def test_gpt4o_is_normal(self):
        conf = {"generation_config": {"temperature": 0.5, "max_output_tokens": 4096}}
        kwargs = build_chat_completion_kwargs(conf, [{"role": "user", "content": "hi"}], model_name="gpt-4o")
        assert kwargs["temperature"] == 0.5
        assert "reasoning_effort" not in kwargs


# ===================================================================
# build_chat_response
# ===================================================================

class TestBuildChatResponse:
    """ChatResponse 組み立てテスト。"""

    def test_basic_response(self):
        result = build_chat_response("hello", [], None)
        assert isinstance(result, ChatResponse)
        assert result.text == "hello"
        assert result.refs == []

    def test_with_rag_results(self):
        rag = [{"title": "doc1", "score": 0.9}]
        result = build_chat_response("hello", rag, None)
        assert len(result.refs) == 1

    def test_with_web_sources(self):
        web = [{"title": "page1", "url": "http://example.com"}]
        result = build_chat_response("hello", [], web)
        assert len(result.sources) == 1


# ===================================================================
# load_env_file
# ===================================================================

class TestLoadEnvFile:
    """環境変数ロードのテスト。"""

    @patch("butly_core.llm._openai_compat.load_dotenv")
    @patch("butly_core.llm._openai_compat._find_env_path")
    def test_loads_env(self, mock_find, mock_load_dotenv):
        mock_find.return_value = "/some/path/.env"
        load_env_file()
        mock_load_dotenv.assert_called_once_with("/some/path/.env")

    @patch("butly_core.llm._openai_compat._find_env_path")
    def test_no_env_file(self, mock_find):
        mock_find.return_value = None
        # Should not raise
        load_env_file()


# ===================================================================
# build_full_messages / extract_token_usage テスト
# ===================================================================

class TestBuildFullMessages:
    def test_full_text_not_truncated(self):
        """500 文字超でも全文を保持する（build_debug_messages と対照）"""
        long_text = "あ" * 2000
        messages = [{"role": "user", "content": long_text}]
        full = build_full_messages(messages)
        assert full[0]["content"] == long_text

    def test_multimodal_keeps_text_parts_only(self):
        """画像 part（base64）は含めず text part のみ結合する"""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "この画像を見て"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ],
            }
        ]
        full = build_full_messages(messages)
        assert full[0]["content"] == "この画像を見て"
        assert "base64" not in full[0]["content"]


class TestExtractTokenUsage:
    class _Usage:
        def __init__(self, prompt=None, completion=None, total=None):
            if prompt is not None:
                self.prompt_tokens = prompt
            if completion is not None:
                self.completion_tokens = completion
            if total is not None:
                self.total_tokens = total

    class _Resp:
        def __init__(self, usage):
            if usage is not None:
                self.usage = usage

    def test_extracts_counts(self):
        resp = self._Resp(self._Usage(prompt=1200, completion=45, total=1245))
        usage = extract_token_usage(resp)
        assert usage == {
            "prompt_tokens": 1200,
            "completion_tokens": 45,
            "source": "api",
            "total_tokens": 1245,
        }

    def test_none_when_no_usage_attr(self):
        assert extract_token_usage(self._Resp(None)) is None

    def test_none_when_counts_missing(self):
        assert extract_token_usage(self._Resp(self._Usage())) is None

    def test_non_int_values_ignored(self):
        """MagicMock 等の非数値属性は None 扱い（既存モックを壊さない）"""
        resp = self._Resp(MagicMock())
        assert extract_token_usage(resp) is None
