"""
test_context_classifier.py
--------------------------
ContextClassifier._determine_tier_from_scores() のユニットテスト。
LLM呼び出し不要 — スコア→tier変換のロジックのみ検証（reflex/mid の 2 値）。
"""

import pytest

from butly_core.core.gatekeeper.context_classifier import ContextClassifier


class TestDetermineTierFromScores:
    """_determine_tier_from_scores のスコア→tier変換テスト (reflex/mid 2値)"""

    @pytest.fixture
    def classifier(self):
        return ContextClassifier()

    # --- reflex ---

    def test_greeting_returns_reflex(self, classifier):
        """「おはよう」相当のスコア → reflex"""
        result = classifier._determine_tier_from_scores({
            "response_complexity": 0.05,
            "emotional_weight": 0.0,
            "continuity_need": 0.0,
        })
        assert result == "reflex"

    def test_low_rc_low_cn_returns_reflex(self, classifier):
        """rc <= 0.4 AND cn <= 0.3 → reflex"""
        result = classifier._determine_tier_from_scores({
            "response_complexity": 0.3,
            "emotional_weight": 0.5,
            "continuity_need": 0.2,
        })
        assert result == "reflex"

    def test_empty_scores_returns_reflex(self, classifier):
        """スコア空 → デフォルト値(0) → reflex"""
        result = classifier._determine_tier_from_scores({})
        assert result == "reflex"

    def test_boundary_rc_0_4_cn_0_3_returns_reflex(self, classifier):
        """rc == 0.4, cn == 0.3 (境界値) → reflex"""
        result = classifier._determine_tier_from_scores({
            "response_complexity": 0.4,
            "emotional_weight": 0.8,
            "continuity_need": 0.3,
        })
        assert result == "reflex"

    def test_high_ew_alone_returns_reflex(self, classifier):
        """ew が高くても rc/cn が低ければ → reflex (ew は tier 判定に不使用)"""
        result = classifier._determine_tier_from_scores({
            "response_complexity": 0.2,
            "emotional_weight": 0.9,
            "continuity_need": 0.1,
        })
        assert result == "reflex"

    # --- mid ---

    def test_high_rc_returns_mid(self, classifier):
        """rc > 0.4 → mid"""
        result = classifier._determine_tier_from_scores({
            "response_complexity": 0.7,
            "emotional_weight": 0.2,
            "continuity_need": 0.1,
        })
        assert result == "mid"

    def test_high_cn_returns_mid(self, classifier):
        """cn > 0.3 → mid"""
        result = classifier._determine_tier_from_scores({
            "response_complexity": 0.1,
            "emotional_weight": 0.0,
            "continuity_need": 0.5,
        })
        assert result == "mid"

    def test_high_rc_and_cn_returns_mid(self, classifier):
        """rc > 0.4 AND cn > 0.3 → mid"""
        result = classifier._determine_tier_from_scores({
            "response_complexity": 0.6,
            "emotional_weight": 0.3,
            "continuity_need": 0.6,
        })
        assert result == "mid"

    def test_boundary_rc_0_41_returns_mid(self, classifier):
        """rc == 0.41 (境界値超え) → mid"""
        result = classifier._determine_tier_from_scores({
            "response_complexity": 0.41,
            "emotional_weight": 0.0,
            "continuity_need": 0.0,
        })
        assert result == "mid"

    def test_boundary_cn_0_31_returns_mid(self, classifier):
        """cn == 0.31 (境界値超え) → mid"""
        result = classifier._determine_tier_from_scores({
            "response_complexity": 0.0,
            "emotional_weight": 0.0,
            "continuity_need": 0.31,
        })
        assert result == "mid"

    # --- no cortex ---

    def test_no_cortex_tier(self, classifier):
        """ContextClassifier は cortex を返さない (ml 判定は MemoryJudge の責務)"""
        # 全スコア最大でも mid
        result = classifier._determine_tier_from_scores({
            "response_complexity": 1.0,
            "emotional_weight": 1.0,
            "continuity_need": 1.0,
        })
        assert result in ("reflex", "mid")
        assert result != "cortex"


class TestParseResponse:
    """_parse_response のテスト"""

    @pytest.fixture
    def classifier(self):
        return ContextClassifier()

    def test_parse_json_with_code_fence(self, classifier):
        raw = '```json\n{"llm_scoring": {"response_complexity": 0.3, "emotional_weight": 0.1, "continuity_need": 0.2}}\n```'
        result = classifier._parse_response(raw)
        assert result["tier"] == "reflex"
        assert result["llm_scoring"]["response_complexity"] == 0.3

    def test_parse_plain_json(self, classifier):
        raw = '{"llm_scoring": {"response_complexity": 0.8, "emotional_weight": 0.5, "continuity_need": 0.6}}'
        result = classifier._parse_response(raw)
        assert result["tier"] == "mid"

    def test_parse_empty_returns_default(self, classifier):
        result = classifier._parse_response("")
        assert result["tier"] == "mid"
        assert result["llm_scoring"] == {}

    def test_parse_invalid_json_returns_default(self, classifier):
        result = classifier._parse_response("not json at all")
        assert result["tier"] == "mid"

    def test_parse_clamps_scores(self, classifier):
        raw = '{"llm_scoring": {"response_complexity": 1.5, "emotional_weight": -0.3, "continuity_need": 0.5}}'
        result = classifier._parse_response(raw)
        assert result["llm_scoring"]["response_complexity"] == 1.0
        assert result["llm_scoring"]["emotional_weight"] == 0.0

    def test_three_scores_only(self, classifier):
        """3 スコアのみをクランプ処理する (ml は不要)"""
        raw = '{"llm_scoring": {"response_complexity": 0.5, "emotional_weight": 0.3, "continuity_need": 0.4}}'
        result = classifier._parse_response(raw)
        assert "memory_reference_likelihood" not in result["llm_scoring"]


class TestConfigResolution:
    """config 解決ロジックのテスト"""

    @pytest.fixture
    def classifier(self):
        return ContextClassifier()

    def test_override_context_classifier_takes_priority(self, classifier):
        override = {
            "context_classifier": {
                "model_name": "custom-model",
                "generation_config": {"temperature": 0.5},
            }
        }
        model, config = classifier._resolve_config(override)
        assert model == "custom-model"

    def test_override_gatekeeper_fallback(self, classifier):
        override = {
            "gatekeeper": {
                "model_name": "gatekeeper-model",
                "generation_config": {"temperature": 0.2},
            }
        }
        model, config = classifier._resolve_config(override)
        assert model == "gatekeeper-model"

    def test_no_override_uses_default(self, classifier):
        model, config = classifier._resolve_config(None)
        assert model == classifier.model_name
