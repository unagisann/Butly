"""
tests/test_embed_uses_config_model.py
-------------------------------------
embedding バグ修正の回帰テスト。

旧 OpenAIProvider.embed() は `OPENAI_EMBEDDING_MODEL` 環境変数を直接読み、
UI で AI_CONFIG["embedding"]["model_name"] に設定した値を **無視** していた。

Phase 1 リファクタ後:
  - ProviderFactory.create(model_name) で default_model_name が伝搬する
  - OpenAICompatAdapter.embed(text, config) で config["model_name"] が最優先
  - env var はあくまで last-resort fallback
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from butly_core.llm.connections import get_connection
from butly_core.llm.factory import ProviderFactory
from butly_core.llm.protocols.openai_compat import OpenAICompatAdapter
from butly_core.llm.providers.openai import OpenAIProvider


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.embeddings.create.return_value = MagicMock(
        data=[MagicMock(embedding=[0.1, 0.2, 0.3, 0.4, 0.5])]
    )
    return client


def _resolved_model(mock_client) -> str:
    args, kwargs = mock_client.embeddings.create.call_args
    return kwargs["model"]


# ===================================================================
# 優先順位検証
# ===================================================================

class TestEmbedPriority:
    """config > default_model_name > env > connection default の順を検証する。"""

    def test_config_model_name_has_top_priority(self, mock_client):
        adapter = OpenAICompatAdapter(
            connection=get_connection("openai"),
            default_model_name="text-embedding-3-small",  # これは負ける想定
        )
        adapter._client = mock_client

        with patch.dict(os.environ, {"OPENAI_EMBEDDING_MODEL": "this-should-lose"}):
            adapter.embed("hello", config={"model_name": "text-embedding-3-large"})

        assert _resolved_model(mock_client) == "text-embedding-3-large"

    def test_default_model_name_over_env(self, mock_client):
        adapter = OpenAICompatAdapter(
            connection=get_connection("openai"),
            default_model_name="text-embedding-3-large",
        )
        adapter._client = mock_client

        with patch.dict(os.environ, {"OPENAI_EMBEDDING_MODEL": "this-should-lose"}):
            adapter.embed("hello")  # config なし

        assert _resolved_model(mock_client) == "text-embedding-3-large"

    def test_env_var_used_as_fallback(self, mock_client):
        adapter = OpenAICompatAdapter(connection=get_connection("openai"))  # default なし
        adapter._client = mock_client

        with patch.dict(os.environ, {"OPENAI_EMBEDDING_MODEL": "env-set-model"}):
            adapter.embed("hello")

        assert _resolved_model(mock_client) == "env-set-model"

    def test_connection_default_as_last_fallback(self, mock_client):
        adapter = OpenAICompatAdapter(connection=get_connection("openai"))
        adapter._client = mock_client

        env_clean = {k: v for k, v in os.environ.items() if k != "OPENAI_EMBEDDING_MODEL"}
        with patch.dict(os.environ, env_clean, clear=True):
            adapter.embed("hello")

        # OpenAI Connection の default_embedding_model
        assert _resolved_model(mock_client) == "text-embedding-3-small"


# ===================================================================
# Factory 経由 (Brain.get_embedding の挙動シミュレーション)
# ===================================================================

class TestFactoryToEmbed:
    """ProviderFactory.create(model_name) → embed(text) のフロー検証。"""

    def test_text_embedding_large_goes_through(self, mock_client):
        provider = ProviderFactory.create("text-embedding-3-large")
        assert isinstance(provider, OpenAIProvider)
        provider._client = mock_client  # mock を注入

        provider.embed("text")  # Brain は config を渡さない (Phase 1 では)

        assert _resolved_model(mock_client) == "text-embedding-3-large"

    def test_text_embedding_small_goes_through(self, mock_client):
        provider = ProviderFactory.create("text-embedding-3-small")
        provider._client = mock_client
        provider.embed("text")
        assert _resolved_model(mock_client) == "text-embedding-3-small"

    def test_env_no_longer_overrides_factory_choice(self, mock_client):
        """旧バグ: env var が factory の選択を握りつぶしていた。
        修正後: default_model_name (factory が設定) が env より強い。"""
        provider = ProviderFactory.create("text-embedding-3-large")
        provider._client = mock_client

        with patch.dict(os.environ, {"OPENAI_EMBEDDING_MODEL": "text-embedding-ada-002"}):
            provider.embed("text")

        # 旧実装ではここが "text-embedding-ada-002" になっていた (バグ)
        assert _resolved_model(mock_client) == "text-embedding-3-large"


# ===================================================================
# Gemini embedding も config を尊重するか
# ===================================================================

class TestGeminiEmbedConfig:
    def test_gemini_embed_uses_config_model_when_provided(self):
        from butly_core.llm.providers.gemini import GeminiProvider

        provider = GeminiProvider()
        mock_client = MagicMock()
        mock_client.models.embed_content.return_value = MagicMock(
            embeddings=[MagicMock(values=[0.1, 0.2])]
        )
        provider._client = mock_client

        provider.embed("hello", config={"model_name": "custom-embedding-model"})

        args, kwargs = mock_client.models.embed_content.call_args
        assert kwargs["model"] == "custom-embedding-model"

    def test_gemini_embed_falls_back_to_ai_config(self):
        from butly_core.llm.providers.gemini import GeminiProvider

        provider = GeminiProvider()
        mock_client = MagicMock()
        mock_client.models.embed_content.return_value = MagicMock(
            embeddings=[MagicMock(values=[0.1])]
        )
        provider._client = mock_client

        # config なし → AI_CONFIG["embedding"]["model_name"] が使われる
        provider.embed("hello")

        args, kwargs = mock_client.models.embed_content.call_args
        # config.py の現在のデフォルト
        from butly_core.config import AI_CONFIG
        assert kwargs["model"] == AI_CONFIG["embedding"]["model_name"]
