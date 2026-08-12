"""
tests/test_openai_compat_adapter.py
-----------------------------------
OpenAICompatAdapter (protocols/openai_compat.py) のユニットテスト。

責務:
  - Connection を切替えるだけで OpenAI / xAI / Ollama 同等動作になるか
  - embeddings_supported=False のとき embed が None を返すか
  - model_name_strip_prefix が API 呼び出し時に効くか
  - user 定義 Connection (Groq 等) でも動くか
"""

from unittest.mock import MagicMock, patch

import pytest

from butly_core.llm.connections import (
    Connection,
    get_connection,
    get_registry,
    register_connection,
)
from butly_core.llm.protocols.openai_compat import OpenAICompatAdapter


# ===================================================================
# Fixtures
# ===================================================================

@pytest.fixture
def reset_registry():
    """各テスト前後で registry を built-in 状態に戻す。"""
    get_registry().reset_to_builtin()
    yield
    get_registry().reset_to_builtin()


@pytest.fixture
def mock_openai_client():
    """OpenAI 互換 client の MagicMock。"""
    client = MagicMock()
    return client


def _attach_client(adapter: OpenAICompatAdapter, client) -> None:
    """Adapter に client を直接挿入する (テスト用)。"""
    adapter._client = client


# ===================================================================
# Built-in connection で classify
# ===================================================================

class TestClassifyAcrossConnections:
    def test_openai_classify(self, mock_openai_client):
        adapter = OpenAICompatAdapter(connection=get_connection("openai"))
        _attach_client(adapter, mock_openai_client)
        mock_openai_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="hello"))]
        )
        result = adapter.classify("hi", {"model_name": "gpt-4o-mini"})
        assert result == "hello"
        # model_name はそのまま (OpenAI は prefix strip なし)
        args, kwargs = mock_openai_client.chat.completions.create.call_args
        assert kwargs["model"] == "gpt-4o-mini"
        assert "response_format" not in kwargs

    def test_classify_forwards_json_schema_response_format(
        self, mock_openai_client
    ):
        adapter = OpenAICompatAdapter(connection=get_connection("openai"))
        _attach_client(adapter, mock_openai_client)
        mock_openai_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content='{"ok":true}'))]
        )
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "test_result",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
            },
        }

        adapter.classify(
            "prompt",
            {
                "model_name": "gpt-4o-mini",
                "response_format": response_format,
            },
        )

        _args, kwargs = mock_openai_client.chat.completions.create.call_args
        assert kwargs["response_format"] == response_format

    def test_xai_classify_strips_prefix(self, mock_openai_client):
        adapter = OpenAICompatAdapter(connection=get_connection("xai"))
        _attach_client(adapter, mock_openai_client)
        mock_openai_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="ok"))]
        )
        adapter.classify("prompt", {"model_name": "xai/grok-4.3"})
        args, kwargs = mock_openai_client.chat.completions.create.call_args
        assert kwargs["model"] == "grok-4.3"

    def test_ollama_classify_strips_prefix(self, mock_openai_client):
        adapter = OpenAICompatAdapter(connection=get_connection("ollama"))
        _attach_client(adapter, mock_openai_client)
        mock_openai_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="ok"))]
        )
        adapter.classify("prompt", {"model_name": "ollama/llama3.2"})
        args, kwargs = mock_openai_client.chat.completions.create.call_args
        assert kwargs["model"] == "llama3.2"


# ===================================================================
# Embedding
# ===================================================================

class TestEmbedAcrossConnections:
    def test_openai_embed_with_config(self, mock_openai_client):
        adapter = OpenAICompatAdapter(connection=get_connection("openai"))
        _attach_client(adapter, mock_openai_client)
        mock_openai_client.embeddings.create.return_value = MagicMock(
            data=[MagicMock(embedding=[0.1, 0.2, 0.3])]
        )
        result = adapter.embed("hello", config={"model_name": "text-embedding-3-large"})
        assert result == [0.1, 0.2, 0.3]
        args, kwargs = mock_openai_client.embeddings.create.call_args
        assert kwargs["model"] == "text-embedding-3-large"

    def test_openai_embed_uses_default_when_no_config(self, mock_openai_client):
        adapter = OpenAICompatAdapter(
            connection=get_connection("openai"),
            default_model_name="text-embedding-3-large",
        )
        _attach_client(adapter, mock_openai_client)
        mock_openai_client.embeddings.create.return_value = MagicMock(
            data=[MagicMock(embedding=[1.0])]
        )
        adapter.embed("hi")
        args, kwargs = mock_openai_client.embeddings.create.call_args
        assert kwargs["model"] == "text-embedding-3-large"

    def test_xai_embed_returns_none(self):
        """xAI は embeddings_supported=False なので client に触れず None を返す。"""
        adapter = OpenAICompatAdapter(connection=get_connection("xai"))
        # client は未設定 (もし呼ばれたら例外になる)
        result = adapter.embed("text")
        assert result is None

    def test_ollama_embed_uses_default(self, mock_openai_client):
        adapter = OpenAICompatAdapter(connection=get_connection("ollama"))
        _attach_client(adapter, mock_openai_client)
        mock_openai_client.embeddings.create.return_value = MagicMock(
            data=[MagicMock(embedding=[0.5])]
        )
        adapter.embed("text")
        args, kwargs = mock_openai_client.embeddings.create.call_args
        # default_embedding_model の "nomic-embed-text" にフォールバック
        assert kwargs["model"] == "nomic-embed-text"


# ===================================================================
# User-defined connection (Groq 風)
# ===================================================================

class TestUserDefinedConnection:
    def test_groq_classify_via_adapter(self, mock_openai_client, reset_registry):
        groq = Connection(
            id="groq",
            protocol="openai_compat",
            base_url="https://api.groq.com/openai/v1",
            api_key_env="GROQ_API_KEY",
            label="Groq",
        )
        register_connection(groq)

        adapter = OpenAICompatAdapter(connection=get_connection("groq"))
        _attach_client(adapter, mock_openai_client)
        mock_openai_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="from groq"))]
        )

        result = adapter.classify("test", {"model_name": "llama-3.3-70b-versatile"})
        assert result == "from groq"

        args, kwargs = mock_openai_client.chat.completions.create.call_args
        # Groq は prefix strip なし
        assert kwargs["model"] == "llama-3.3-70b-versatile"


# ===================================================================
# Factory routing
# ===================================================================

class TestFactoryRouting:
    def test_string_to_provider(self):
        from butly_core.llm.factory import ProviderFactory
        from butly_core.llm.providers.openai import OpenAIProvider
        provider = ProviderFactory.create("gpt-4o")
        assert isinstance(provider, OpenAIProvider)

    def test_modelref_to_provider(self):
        from butly_core.llm.factory import ProviderFactory
        from butly_core.llm.model_registry import ModelRef
        from butly_core.llm.providers.xai import XaiProvider
        provider = ProviderFactory.create(ModelRef(connection_id="xai", model_name="grok-4.3"))
        assert isinstance(provider, XaiProvider)

    def test_dict_to_provider(self):
        from butly_core.llm.factory import ProviderFactory
        from butly_core.llm.providers.ollama import OllamaProvider
        provider = ProviderFactory.create({"connection": "ollama", "model_name": "llama3.2"})
        assert isinstance(provider, OllamaProvider)

    def test_user_connection_to_adapter(self, reset_registry):
        from butly_core.llm.factory import ProviderFactory
        register_connection(Connection(id="groq", protocol="openai_compat"))
        provider = ProviderFactory.create(
            {"connection": "groq", "model_name": "llama-3.3-70b-versatile"}
        )
        # Groq は user 定義 → built-in シムではなく adapter 直
        assert isinstance(provider, OpenAICompatAdapter)
        # built-in 4 シムには該当しない
        from butly_core.llm.providers.openai import OpenAIProvider
        from butly_core.llm.providers.xai import XaiProvider
        from butly_core.llm.providers.ollama import OllamaProvider
        assert not isinstance(provider, (OpenAIProvider, XaiProvider, OllamaProvider))

    def test_gemini_string_to_gemini_provider(self):
        from butly_core.llm.factory import ProviderFactory
        from butly_core.llm.providers.gemini import GeminiProvider
        provider = ProviderFactory.create("gemini-3.5-flash")
        assert isinstance(provider, GeminiProvider)

    def test_text_embedding_routes_to_openai(self):
        from butly_core.llm.factory import ProviderFactory
        from butly_core.llm.providers.openai import OpenAIProvider
        provider = ProviderFactory.create("text-embedding-3-large")
        assert isinstance(provider, OpenAIProvider)
        # default_model_name が伝搬しているか
        assert provider.default_model_name == "text-embedding-3-large"

    def test_unknown_model_raises(self):
        from butly_core.llm.factory import ProviderFactory
        with pytest.raises(NotImplementedError):
            ProviderFactory.create("totally-unknown-model")


# ===================================================================
# generate: token_usage / messages_full の debug_info 伝搬
# ===================================================================

class TestGenerateTokenUsage:
    @patch(
        "butly_core.core.gatekeeper.build_system_instruction_from_blocks",
        return_value="sys",
    )
    @patch(
        "butly_core.core.gatekeeper.build_context_prefix",
        return_value="",
    )
    def test_generate_raises_sanitized_provider_error(
        self, _mock_prefix, _mock_sys, mock_openai_client
    ):
        adapter = OpenAICompatAdapter(connection=get_connection("openai"))
        _attach_client(adapter, mock_openai_client)
        raw = "secret-token at /private/provider/path"
        mock_openai_client.chat.completions.create.side_effect = RuntimeError(raw)

        with pytest.raises(RuntimeError) as caught:
            adapter.generate("question", [], {"history": []})

        assert str(caught.value) == "Provider generation failed"
        assert raw not in str(caught.value)

    @patch("butly_core.core.gatekeeper.build_system_instruction_from_blocks", return_value="sys")
    @patch("butly_core.core.gatekeeper.build_context_prefix", return_value="長いコンテキスト" * 100)
    def test_generate_captures_api_usage_and_full_messages(
        self, mock_prefix, mock_sys, mock_openai_client
    ):
        adapter = OpenAICompatAdapter(connection=get_connection("openai"))
        _attach_client(adapter, mock_openai_client)

        usage = MagicMock(spec=["prompt_tokens", "completion_tokens", "total_tokens"])
        usage.prompt_tokens = 1200
        usage.completion_tokens = 45
        usage.total_tokens = 1245
        mock_openai_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="answer"))],
            usage=usage,
        )

        result = adapter.generate("question", [], {"history": [], "rag_results": []})

        assert result.debug_info["token_usage"] == {
            "prompt_tokens": 1200,
            "completion_tokens": 45,
            "source": "api",
            "total_tokens": 1245,
        }
        # messages_full は truncate されない全文（messages は 500 字で切られる）
        full_prefix = next(
            m["content"] for m in result.debug_info["messages_full"]
            if m["content"].startswith("長いコンテキスト")
        )
        assert len(full_prefix) == len("長いコンテキスト") * 100

    @patch("butly_core.core.gatekeeper.build_system_instruction_from_blocks", return_value="sys")
    @patch("butly_core.core.gatekeeper.build_context_prefix", return_value="")
    def test_generate_without_usage_omits_key(
        self, mock_prefix, mock_sys, mock_openai_client
    ):
        """usage が取れない（MagicMock 汚染含む）場合は token_usage キー無し"""
        adapter = OpenAICompatAdapter(connection=get_connection("openai"))
        _attach_client(adapter, mock_openai_client)
        mock_openai_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="answer"))]
        )

        result = adapter.generate("question", [], {"history": [], "rag_results": []})

        assert "token_usage" not in result.debug_info


class TestClassifyTokenUsage:
    def test_classify_sets_poppable_usage(self, mock_openai_client):
        """classify 後に pop_last_token_usage で実測値を取り出せる（1回限り）"""
        adapter = OpenAICompatAdapter(connection=get_connection("openai"))
        _attach_client(adapter, mock_openai_client)

        usage = MagicMock(spec=["prompt_tokens", "completion_tokens"])
        usage.prompt_tokens = 800
        usage.completion_tokens = 120
        mock_openai_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="ok"))],
            usage=usage,
        )

        adapter.classify("prompt", {"model_name": "gpt-4o-mini"})
        popped = adapter.pop_last_token_usage()
        assert popped == {
            "prompt_tokens": 800,
            "completion_tokens": 120,
            "source": "api",
        }
        # 1 スロット方式: 2 回目は None
        assert adapter.pop_last_token_usage() is None
