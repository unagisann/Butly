"""
test_tier_classifier.py
-----------------------
TierClassifier._determine_tier_from_scores() のユニットテスト。
LLM呼び出し不要 — スコア→tier変換のロジックのみ検証。
"""

import pytest

from butly_core.core.gatekeeper.tier_classifier import TierClassifier


class TestDetermineTierFromScores:
    """_determine_tier_from_scores のスコア→tier変換テスト"""

    @pytest.fixture
    def classifier(self):
        """TierClassifier インスタンス（LLMは呼ばない）"""
        return TierClassifier()

    def test_high_memory_reference_returns_cortex(self, classifier):
        """memory_reference_likelihood >= 0.7 → cortex"""
        result = classifier._determine_tier_from_scores({
            "response_complexity": 0.5,
            "emotional_weight": 0.3,
            "memory_reference_likelihood": 0.8,
            "continuity_need": 0.4,
        })
        assert result == "cortex"

    def test_low_scores_returns_reflex(self, classifier):
        """rc低・ml低・cn低 → reflex"""
        result = classifier._determine_tier_from_scores({
            "response_complexity": 0.1,
            "emotional_weight": 0.2,
            "memory_reference_likelihood": 0.1,
            "continuity_need": 0.1,
        })
        assert result == "reflex"

    def test_greeting_returns_reflex(self, classifier):
        """「おはよう」相当のスコア → reflex"""
        result = classifier._determine_tier_from_scores({
            "response_complexity": 0.05,
            "emotional_weight": 0.0,
            "memory_reference_likelihood": 0.0,
            "continuity_need": 0.0,
        })
        assert result == "reflex"

    def test_moderate_complexity_returns_mid(self, classifier):
        """中程度の複雑さ → mid"""
        result = classifier._determine_tier_from_scores({
            "response_complexity": 0.5,
            "emotional_weight": 0.3,
            "memory_reference_likelihood": 0.4,
            "continuity_need": 0.3,
        })
        assert result == "mid"

    def test_high_continuity_prevents_reflex(self, classifier):
        """cn > 0.3 → reflex にはならない（mid）"""
        result = classifier._determine_tier_from_scores({
            "response_complexity": 0.1,
            "emotional_weight": 0.0,
            "memory_reference_likelihood": 0.1,
            "continuity_need": 0.5,
        })
        assert result == "mid"

    def test_high_complexity_prevents_reflex(self, classifier):
        """rc > 0.2 → reflex にはならない（mid）"""
        result = classifier._determine_tier_from_scores({
            "response_complexity": 0.7,
            "emotional_weight": 0.2,
            "memory_reference_likelihood": 0.2,
            "continuity_need": 0.1,
        })
        assert result == "mid"

    def test_boundary_memory_reference_0_7_returns_cortex(self, classifier):
        """ml == 0.7 → cortex（境界値）"""
        result = classifier._determine_tier_from_scores({
            "response_complexity": 0.1,
            "emotional_weight": 0.0,
            "memory_reference_likelihood": 0.7,
            "continuity_need": 0.0,
        })
        assert result == "cortex"

    def test_boundary_memory_reference_0_69_returns_mid(self, classifier):
        """ml == 0.69 → cortex にはならない"""
        result = classifier._determine_tier_from_scores({
            "response_complexity": 0.5,
            "emotional_weight": 0.0,
            "memory_reference_likelihood": 0.69,
            "continuity_need": 0.0,
        })
        assert result == "mid"

    def test_empty_scores_returns_reflex(self, classifier):
        """スコアが空 → 全て0扱い → reflex"""
        result = classifier._determine_tier_from_scores({})
        assert result == "reflex"

    def test_boundary_rc_0_2_ml_0_3_cn_0_3_returns_reflex(self, classifier):
        """rc=0.2, ml=0.3, cn=0.3 の境界 → reflex"""
        result = classifier._determine_tier_from_scores({
            "response_complexity": 0.2,
            "emotional_weight": 0.9,
            "memory_reference_likelihood": 0.3,
            "continuity_need": 0.3,
        })
        assert result == "reflex"

    def test_explicit_past_reference_returns_cortex(self, classifier):
        """「前に話してたプロジェクト」相当 → cortex"""
        result = classifier._determine_tier_from_scores({
            "response_complexity": 0.6,
            "emotional_weight": 0.1,
            "memory_reference_likelihood": 0.95,
            "continuity_need": 0.3,
        })
        assert result == "cortex"


class TestParseResponse:
    """_parse_response のJSONパーステスト"""

    @pytest.fixture
    def classifier(self):
        return TierClassifier()

    def test_parse_json_with_code_fence(self, classifier):
        """```json ... ``` 形式の応答を正しくパースする"""
        raw = '```json\n{"llm_scoring": {"response_complexity": 0.5, "emotional_weight": 0.3, "memory_reference_likelihood": 0.8, "continuity_need": 0.4}}\n```'
        result = classifier._parse_response(raw)
        assert result["tier"] == "cortex"
        assert result["llm_scoring"]["response_complexity"] == 0.5

    def test_parse_plain_json(self, classifier):
        """プレーンJSON応答をパースする"""
        raw = '{"llm_scoring": {"response_complexity": 0.1, "emotional_weight": 0.0, "memory_reference_likelihood": 0.0, "continuity_need": 0.0}}'
        result = classifier._parse_response(raw)
        assert result["tier"] == "reflex"

    def test_parse_empty_returns_default(self, classifier):
        """空の応答はデフォルト（mid）を返す"""
        result = classifier._parse_response("")
        assert result["tier"] == "mid"

    def test_parse_invalid_json_returns_default(self, classifier):
        """不正JSONはデフォルト（mid）を返す"""
        result = classifier._parse_response("not json at all")
        assert result["tier"] == "mid"

    def test_parse_clamps_scores(self, classifier):
        """範囲外のスコアは 0-1 にクランプされる"""
        raw = '{"llm_scoring": {"response_complexity": 1.5, "emotional_weight": -0.3, "memory_reference_likelihood": 0.5, "continuity_need": 0.5}}'
        result = classifier._parse_response(raw)
        assert result["llm_scoring"]["response_complexity"] == 1.0
        assert result["llm_scoring"]["emotional_weight"] == 0.0


class TestRecentHeadlines:
    """recent_headlines 引数のテスト"""

    @pytest.fixture
    def classifier(self):
        return TierClassifier()

    def test_classify_accepts_recent_headlines(self, classifier, monkeypatch):
        """recent_headlines 引数ありでプロンプトに注入される"""
        captured_prompt = {}

        def mock_get(name, **kwargs):
            captured_prompt["kwargs"] = kwargs
            return "mock prompt"

        monkeypatch.setattr(classifier._prompt_loader, "get", mock_get)

        # classify の LLM 呼び出しもモック
        def mock_classify(prompt, config):
            return '{"llm_scoring": {"response_complexity": 0.5, "emotional_weight": 0.0, "memory_reference_likelihood": 0.5, "continuity_need": 0.0}}'

        from unittest.mock import MagicMock
        mock_provider = MagicMock()
        mock_provider.classify = mock_classify
        monkeypatch.setattr(
            "butly_core.llm.factory.ProviderFactory.create",
            lambda model_name: mock_provider,
        )

        result = classifier.classify(
            "テスト", [],
            current_topic="",
            recent_headlines="- [Topic] Gatekeeper改修方針",
        )

        assert captured_prompt["kwargs"]["recent_headlines"] == "- [Topic] Gatekeeper改修方針"
        assert result["tier"] in ("reflex", "mid", "cortex")

    def test_classify_empty_headlines_no_error(self, classifier, monkeypatch):
        """recent_headlines 空文字でもエラーにならない"""
        def mock_get(name, **kwargs):
            return "mock prompt"

        monkeypatch.setattr(classifier._prompt_loader, "get", mock_get)

        def mock_classify(prompt, config):
            return '{"llm_scoring": {"response_complexity": 0.1, "emotional_weight": 0.0, "memory_reference_likelihood": 0.0, "continuity_need": 0.0}}'

        from unittest.mock import MagicMock
        mock_provider = MagicMock()
        mock_provider.classify = mock_classify
        monkeypatch.setattr(
            "butly_core.llm.factory.ProviderFactory.create",
            lambda model_name: mock_provider,
        )

        result = classifier.classify("おはよう", [], recent_headlines="")

        assert result["tier"] == "reflex"
