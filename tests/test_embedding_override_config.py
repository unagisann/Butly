"""QA 経路のベクトル検索が instance/profile の embedding 設定を使う回帰テスト。

以前は Brain.get_embedding がグローバル AI_CONFIG["embedding"]（既定 Gemini）
固定で、instance ごとに profile で指定した embedding connection を無視していた。
そのため書き込み(Sleeptime)は local_embedding、読み出し(QA)は Gemini という
食い違いが起き、ローカルLLM評価で RAG が発火しなかった。
"""

from unittest.mock import MagicMock

import pytest

from butly_core.config import AI_CONFIG
from butly_core.core.brain import ButlyBrain


@pytest.fixture
def brain(tmp_path):
    return ButlyBrain(tmp_path)


class TestResolveEmbeddingConf:
    def test_override_embedding_wins_over_global(self, brain):
        conf = brain._resolve_embedding_conf(
            {"embedding": {"connection": "local_embedding", "model_name": "nomic"}}
        )
        assert conf["connection"] == "local_embedding"
        assert conf["model_name"] == "nomic"

    def test_defaults_to_global_when_no_override(self, brain):
        conf = brain._resolve_embedding_conf(None)
        assert conf == AI_CONFIG.get("embedding", {})

    def test_ignores_override_without_embedding_section(self, brain):
        conf = brain._resolve_embedding_conf({"brain": {"readable_instances": ["self"]}})
        assert conf == AI_CONFIG.get("embedding", {})


class TestGetEmbeddingUsesConf:
    def test_get_embedding_resolves_provider_from_passed_conf(self, brain, monkeypatch):
        captured = []

        def fake_create(model):
            captured.append(model)
            provider = MagicMock()
            provider.embed.return_value = [0.1, 0.2, 0.3]
            return provider

        monkeypatch.setattr(
            "butly_core.llm.factory.ProviderFactory.create", fake_create
        )

        result = brain.get_embedding(
            "hello",
            {"connection": "local_embedding", "model_name": "nomic-embed-text"},
        )

        assert result == [0.1, 0.2, 0.3]
        assert captured, "provider was never created"
        assert captured[0]["connection"] == "local_embedding"
        assert captured[0]["model_name"] == "nomic-embed-text"

    def test_get_embedding_falls_back_to_global(self, brain, monkeypatch):
        captured = []

        def fake_create(model):
            captured.append(model)
            provider = MagicMock()
            provider.embed.return_value = [0.5]
            return provider

        monkeypatch.setattr(
            "butly_core.llm.factory.ProviderFactory.create", fake_create
        )

        brain.get_embedding("hello")  # embedding_conf 未指定

        assert captured[0] == AI_CONFIG.get("embedding", {})


class TestVectorSearchPropagatesEmbeddingConf:
    def test_quick_vector_search_forwards_embedding_conf(self, brain, monkeypatch):
        """override_config の embedding が get_embedding まで伝わる。"""
        captured_confs = []

        def spy_get_embedding(text, embedding_conf=None):
            captured_confs.append(embedding_conf)
            return None  # None で _quick_vector_search_single_diag が早期 return

        monkeypatch.setattr(brain, "get_embedding", spy_get_embedding)
        monkeypatch.setattr(brain, "_get_db_path", lambda name: _FakePath())

        brain.quick_vector_search_diag(
            "When did Caroline study nursing?",
            "locomo_conv_1",
            override_config={
                "embedding": {
                    "connection": "local_embedding",
                    "model_name": "nomic-embed-text",
                }
            },
        )

        assert captured_confs, "get_embedding was never reached"
        assert captured_confs[0]["connection"] == "local_embedding"
        assert captured_confs[0]["model_name"] == "nomic-embed-text"

    def test_search_knowledge_forwards_embedding_conf(self, brain, monkeypatch):
        """deep_search (search_knowledge) 経路も instance embedding を伝える。"""
        captured_confs = []

        def spy_get_embedding(text, embedding_conf=None):
            captured_confs.append(embedding_conf)
            return None

        monkeypatch.setattr(brain, "get_embedding", spy_get_embedding)
        monkeypatch.setattr(brain, "_get_db_path", lambda name: _FakePath())
        # _search_single_db が rows を得た後に get_embedding を呼ぶよう、
        # keyword フィルタ結果を1件返すダミーに差し替える。
        monkeypatch.setattr(
            brain, "_search_single_db",
            lambda kw, uq, inst, lim, bc, ec=None: (
                captured_confs.append(ec) or []
            ),
        )

        brain.search_knowledge(
            ["nursing"],
            "What did Caroline study?",
            instance_name="locomo_conv_1",
            override_config={
                "embedding": {"connection": "local_embedding", "model_name": "nomic"}
            },
        )

        assert captured_confs, "embedding conf was not forwarded to single-db search"
        assert captured_confs[0]["connection"] == "local_embedding"


class _FakePath:
    """exists() だけ True を返すダミー（sqlite には到達させない）。"""

    def exists(self):
        return True
