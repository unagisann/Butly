"""
test_memory_probe.py
--------------------
MemoryProbe の単体テスト。
LLM 呼び出しなし — ルールベース判定・マッチングロジックを検証。
"""

import pytest
from unittest.mock import MagicMock, patch

from butly_core.core.gatekeeper.memory_probe import (
    MemoryProbe,
    should_deep_search,
    asks_for_specific_past_detail,
)


# ===================================================================
# asks_for_specific_past_detail テスト
# ===================================================================

class TestAsksForSpecificPastDetail:
    """過去参照パターンの検出テスト"""

    @pytest.mark.parametrize("text", [
        "前に話したプロジェクトの件",
        "以前教えてくれたレシピ",
        "あの時の会議の内容",
        "この前のミーティング",
        "前回の打ち合わせ",
        "覚えてる？あの話",
    ])
    def test_ja_patterns_detected(self, text):
        assert asks_for_specific_past_detail(text) is True

    @pytest.mark.parametrize("text", [
        "remember what we discussed?",
        "last time we talked about it",
        "you mentioned something before",
        "we talked about the project",
    ])
    def test_en_patterns_detected(self, text):
        assert asks_for_specific_past_detail(text) is True

    @pytest.mark.parametrize("text", [
        "あれってどうなった？",
        "プロジェクトの件はどうした",
        "締め切りだっけ？",
        "明日でしたっけ？",
    ])
    def test_ja_suffix_patterns_detected(self, text):
        assert asks_for_specific_past_detail(text) is True

    @pytest.mark.parametrize("text", [
        "今日の天気は？",
        "こんにちは",
        "新しいプロジェクトを始めよう",
        "Hello, how are you?",
        "ありがとう",
    ])
    def test_no_past_reference(self, text):
        assert asks_for_specific_past_detail(text) is False


# ===================================================================
# should_deep_search テスト
# ===================================================================

class TestShouldDeepSearch:
    """Layer 2 トリガー判定のテスト"""

    def test_layer1_hit_skips_deep(self):
        """Layer 1 でヒットがあれば deep search しない"""
        assert should_deep_search("前に話した件", layer1_hits=True, headline_match=False, glossary_match=False) is False

    def test_no_past_ref_no_deep(self):
        """過去参照なし → deep search しない"""
        assert should_deep_search("今日の天気は？", layer1_hits=False, headline_match=False, glossary_match=False) is False

    def test_past_ref_triggers_deep(self):
        """過去参照あり + Layer 1 ミス → deep search する"""
        assert should_deep_search("前に話したプロジェクトの件", layer1_hits=False, headline_match=False, glossary_match=False) is True

    def test_headline_match_suppresses_deep(self):
        """headline でカバーできるなら掘らない（過去参照なし）"""
        assert should_deep_search("プロジェクトの話", layer1_hits=False, headline_match=True, glossary_match=False) is False

    def test_glossary_match_suppresses_deep(self):
        """glossary でカバーできるなら掘らない（過去参照なし）"""
        assert should_deep_search("プロジェクトの話", layer1_hits=False, headline_match=False, glossary_match=True) is False

    def test_past_ref_overrides_headline(self):
        """過去参照ありなら headline があっても deep search する"""
        assert should_deep_search("前に話したプロジェクトの件", layer1_hits=False, headline_match=True, glossary_match=False) is True


# ===================================================================
# MemoryProbe._match_glossary テスト
# ===================================================================

class TestGlossaryMatch:
    """Glossary マッチングのテスト"""

    @pytest.fixture
    def probe(self):
        return MemoryProbe()

    @pytest.fixture
    def mock_memory_with_glossary(self):
        mm = MagicMock()
        mm.get_glossary_raw.return_value = {
            "version": 1,
            "entries": [
                {
                    "term": "Gatekeeper",
                    "definition": "入力分類システム",
                    "aliases": ["GK", "ゲートキーパー"],
                    "status": "active",
                },
                {
                    "term": "RAG",
                    "definition": "Retrieval-Augmented Generation",
                    "aliases": ["検索拡張生成"],
                    "status": "active",
                },
                {
                    "term": "Deprecated",
                    "definition": "非推奨の項目",
                    "aliases": [],
                    "status": "inactive",
                },
            ],
        }
        return mm

    def test_term_match(self, probe, mock_memory_with_glossary):
        hits = probe._match_glossary("Gatekeeperの改修方針", mock_memory_with_glossary)
        assert len(hits) == 1
        assert hits[0]["term"] == "Gatekeeper"
        assert hits[0]["match_type"] == "term"

    def test_alias_match(self, probe, mock_memory_with_glossary):
        hits = probe._match_glossary("GKの設定を変更", mock_memory_with_glossary)
        assert len(hits) == 1
        assert hits[0]["term"] == "Gatekeeper"
        assert hits[0]["match_type"] == "alias"

    def test_multiple_matches(self, probe, mock_memory_with_glossary):
        hits = probe._match_glossary("GatekeeperのRAG検索", mock_memory_with_glossary)
        assert len(hits) == 2
        terms = {h["term"] for h in hits}
        assert terms == {"Gatekeeper", "RAG"}

    def test_inactive_not_matched(self, probe, mock_memory_with_glossary):
        hits = probe._match_glossary("Deprecatedの件", mock_memory_with_glossary)
        assert len(hits) == 0

    def test_no_match(self, probe, mock_memory_with_glossary):
        hits = probe._match_glossary("今日の天気は？", mock_memory_with_glossary)
        assert len(hits) == 0

    def test_case_insensitive_match(self, probe, mock_memory_with_glossary):
        hits = probe._match_glossary("ragについて教えて", mock_memory_with_glossary)
        assert len(hits) == 1
        assert hits[0]["term"] == "RAG"

    def test_empty_glossary(self, probe):
        mm = MagicMock()
        mm.get_glossary_raw.return_value = {"version": 1, "entries": []}
        hits = probe._match_glossary("何でもいい", mm)
        assert hits == []


# ===================================================================
# MemoryProbe._check_headline_match テスト
# ===================================================================

class TestHeadlineMatch:
    """Headline マッチングのテスト"""

    @pytest.fixture
    def probe(self):
        return MemoryProbe()

    def test_match_found(self, probe):
        headlines = "- [Topic] Gatekeeper 改修方針\n- [Event] Phase3 完了"
        assert probe._check_headline_match("Gatekeeper の改修", headlines) is True

    def test_no_match(self, probe):
        headlines = "- [Topic] Gatekeeper 改修方針\n- [Event] Phase3 完了"
        assert probe._check_headline_match("今日の天気", headlines) is False

    def test_empty_headlines(self, probe):
        assert probe._check_headline_match("何か", "(no recent headlines)") is False

    def test_no_headlines(self, probe):
        assert probe._check_headline_match("何か", "") is False


# ===================================================================
# MemoryProbe.probe テスト（統合）
# ===================================================================

class TestProbe:
    """probe() メソッドの統合テスト"""

    @pytest.fixture
    def probe(self):
        return MemoryProbe()

    @pytest.fixture
    def mock_brain_with_vector(self):
        brain = MagicMock()
        brain.quick_vector_search.return_value = [
            {"id": "1", "title": "テスト記憶", "summary": "テスト内容", "episode": "", "score": 0.75, "source": "vector"},
        ]
        brain.extract_keywords.return_value = {"keywords": ["テスト"]}
        brain.search_knowledge.return_value = []
        return brain

    @pytest.fixture
    def mock_brain_no_hit(self):
        brain = MagicMock()
        brain.quick_vector_search.return_value = []
        brain.extract_keywords.return_value = {"keywords": ["テスト"]}
        brain.search_knowledge.return_value = []
        return brain

    @pytest.fixture
    def mock_brain_deep_hit(self):
        brain = MagicMock()
        brain.quick_vector_search.return_value = []
        brain.extract_keywords.return_value = {"keywords": ["プロジェクト"]}
        brain.search_knowledge.return_value = [
            {"id": "2", "title": "プロジェクト計画", "summary": "計画内容", "episode": "", "score": 0.7},
        ]
        return brain

    @pytest.fixture
    def mock_memory_empty_glossary(self):
        mm = MagicMock()
        mm.get_glossary_raw.return_value = {"version": 1, "entries": []}
        return mm

    def test_layer1_hit(self, probe, mock_brain_with_vector, mock_memory_empty_glossary):
        result = probe.probe(
            user_input="テストの件",
            brain=mock_brain_with_vector,
            memory_manager=mock_memory_empty_glossary,
        )
        assert result["status"] == "hit"
        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["title"] == "テスト記憶"

    def test_no_hit(self, probe, mock_brain_no_hit, mock_memory_empty_glossary):
        result = probe.probe(
            user_input="今日の天気は？",
            brain=mock_brain_no_hit,
            memory_manager=mock_memory_empty_glossary,
        )
        assert result["status"] == "no_hit"
        assert result["candidates"] == []

    def test_deep_search_triggered(self, probe, mock_brain_deep_hit, mock_memory_empty_glossary):
        result = probe.probe(
            user_input="前に話したプロジェクトの件",
            brain=mock_brain_deep_hit,
            memory_manager=mock_memory_empty_glossary,
        )
        assert result["status"] == "deep_search"
        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["source"] == "keyword"

    def test_deep_search_no_hit(self, probe, mock_brain_no_hit, mock_memory_empty_glossary):
        """deep_search 発動するが結果なし"""
        result = probe.probe(
            user_input="前に話した何かの件",
            brain=mock_brain_no_hit,
            memory_manager=mock_memory_empty_glossary,
        )
        assert result["status"] == "no_hit"
        assert result["candidates"] == []

    def test_glossary_hits_returned(self, probe, mock_brain_no_hit):
        mm = MagicMock()
        mm.get_glossary_raw.return_value = {
            "version": 1,
            "entries": [
                {"term": "Gatekeeper", "definition": "入力分類", "aliases": [], "status": "active"},
            ],
        }
        result = probe.probe(
            user_input="Gatekeeperについて",
            brain=mock_brain_no_hit,
            memory_manager=mm,
        )
        assert len(result["glossary_hits"]) == 1
        assert result["glossary_hits"][0]["term"] == "Gatekeeper"

    def test_deep_search_disabled(self, probe, mock_brain_no_hit, mock_memory_empty_glossary):
        """deep_search_enabled=False の場合"""
        with patch("butly_core.core.gatekeeper.memory_probe.SYSTEM_CONFIG", {
            "memory_probe": {"vector_search_limit": 3, "vector_search_threshold": 0.6, "deep_search_enabled": False},
            "brain": {"search_limit": 3},
        }):
            result = probe.probe(
                user_input="前に話した件",
                brain=mock_brain_no_hit,
                memory_manager=mock_memory_empty_glossary,
            )
            assert result["status"] == "no_hit"
            mock_brain_no_hit.extract_keywords.assert_not_called()


# ===================================================================
# Gatekeeper 統合テスト（MemoryProbe 経由）
# ===================================================================

class TestGatekeeperIntegration:
    """Gatekeeper.classify() が probe 結果を正しく返すかのテスト"""

    @pytest.fixture
    def mock_gatekeeper(self, test_instance_dir):
        from butly_core.core.gatekeeper import Gatekeeper
        gk = Gatekeeper()

        # ContextClassifier と StateUpdater をモック
        gk.context_classifier = MagicMock()
        gk.state_updater = MagicMock()
        gk.memory_probe = MagicMock()

        gk.context_classifier.classify.return_value = {
            "tier": "mid",
            "llm_scoring": {"response_complexity": 0.5, "emotional_weight": 0.3, "continuity_need": 0.4},
        }
        gk.state_updater.update.return_value = {
            "topic": "テスト話題",
        }

        return gk

    def test_probe_hit_sets_need(self, mock_gatekeeper, test_instance_dir):
        """probe hit → tier は mid のまま、need が設定される"""
        mock_gatekeeper.memory_probe.probe.return_value = {
            "status": "hit",
            "candidates": [{"title": "テスト", "summary": "内容", "score": 0.8}],
            "glossary_hits": [],
        }

        brain = MagicMock()
        result = mock_gatekeeper.classify(
            user_input="テスト",
            history_msgs=[],
            session_state={},
            instance_dir=test_instance_dir,
            brain=brain,
        )

        assert result["tier"] == "mid"
        assert result["need"] == "memory_probe_hit"
        assert result["memory_probe"]["status"] == "hit"

    def test_probe_no_hit_stays_mid(self, mock_gatekeeper, test_instance_dir):
        """probe no_hit → mid のまま"""
        mock_gatekeeper.memory_probe.probe.return_value = {
            "status": "no_hit",
            "candidates": [],
            "glossary_hits": [],
        }

        brain = MagicMock()
        result = mock_gatekeeper.classify(
            user_input="こんにちは",
            history_msgs=[],
            session_state={},
            instance_dir=test_instance_dir,
            brain=brain,
        )

        assert result["tier"] == "mid"
        assert result["need"] is None
        assert result["memory_probe"]["status"] == "no_hit"

    def test_no_brain_returns_no_hit(self, mock_gatekeeper, test_instance_dir):
        """brain=None → probe は no_hit"""
        result = mock_gatekeeper.classify(
            user_input="テスト",
            history_msgs=[],
            session_state={},
            instance_dir=test_instance_dir,
            brain=None,
        )

        assert result["need"] is None
        assert result["memory_probe"]["status"] == "no_hit"

    def test_deep_search_sets_need(self, mock_gatekeeper, test_instance_dir):
        """deep_search hit → tier は mid のまま、need が設定される"""
        mock_gatekeeper.memory_probe.probe.return_value = {
            "status": "deep_search",
            "candidates": [{"title": "過去の会話", "summary": "内容", "score": 0.65}],
            "glossary_hits": [],
        }

        brain = MagicMock()
        result = mock_gatekeeper.classify(
            user_input="前に話した件",
            history_msgs=[],
            session_state={},
            instance_dir=test_instance_dir,
            brain=brain,
        )

        assert result["tier"] == "mid"
        assert result["need"] == "memory_probe_deep_search"
        assert result["search_targets"] == ["過去の会話"]
