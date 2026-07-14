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

    # --- no cortex (ContextClassifier は reflex/mid の 2 値のみ) ---

    def test_no_cortex_tier(self, classifier):
        """ContextClassifier は cortex を返さない (RAG 判定は MemoryProbe の責務)"""
        # 全スコア最大でも mid
        result = classifier._determine_tier_from_scores({
            "response_complexity": 1.0,
            "emotional_weight": 1.0,
            "continuity_need": 1.0,
        })
        assert result in ("reflex", "mid")


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

    def test_parse_unclosed_code_fence(self, classifier):
        """閉じ ``` 欠落（トークン切れ）でも JSON を抽出できる — v8 qa_007/qa_010 の実障害パターン"""
        raw = (
            '```json\n{"llm_scoring": {"response_complexity": 0.6, '
            '"emotional_weight": 0.1, "continuity_need": 0.2}, '
            '"need_intent": "past_fact"}'
        )
        result = classifier._parse_response(raw, user_input="When did she move?")
        assert result["llm_scoring"]["response_complexity"] == 0.6
        assert result["need_intent"] == "past_fact"

    def test_parse_prose_wrapped_json(self, classifier):
        """地の文に埋め込まれた JSON も抽出できる"""
        raw = (
            'Here is my analysis:\n'
            '{"llm_scoring": {"response_complexity": 0.5, '
            '"emotional_weight": 0.0, "continuity_need": 0.4}, '
            '"need_intent": null}\nHope this helps.'
        )
        result = classifier._parse_response(raw)
        assert result["tier"] == "mid"
        assert result["need_intent"] is None


class TestNeedIntentParsing:
    """need_intent の parse とフォールバックのテスト"""

    @pytest.fixture
    def classifier(self):
        return ContextClassifier()

    @pytest.mark.parametrize("intent", ["past_fact", "glossary", "relationship"])
    def test_parse_valid_intent(self, classifier, intent):
        raw = f'{{"llm_scoring": {{"response_complexity": 0.5, "emotional_weight": 0.2, "continuity_need": 0.3}}, "need_intent": "{intent}"}}'
        result = classifier._parse_response(raw, user_input="any")
        assert result["need_intent"] == intent

    @pytest.mark.parametrize("raw_null", ['null', '"null"'])
    def test_parse_null_intent(self, classifier, raw_null):
        raw = f'{{"llm_scoring": {{"response_complexity": 0.1, "emotional_weight": 0.0, "continuity_need": 0.0}}, "need_intent": {raw_null}}}'
        result = classifier._parse_response(raw, user_input="おはよう")
        assert result["need_intent"] is None

    def test_missing_field_falls_back_to_rule_null(self, classifier):
        """need_intent フィールド欠落 + 過去参照パターンなし → null"""
        raw = '{"llm_scoring": {"response_complexity": 0.2, "emotional_weight": 0.0, "continuity_need": 0.0}}'
        result = classifier._parse_response(raw, user_input="今日の天気はどう")
        assert result["need_intent"] is None

    def test_missing_field_falls_back_to_rule_past_fact(self, classifier):
        """need_intent フィールド欠落 + 過去参照パターンあり → past_fact"""
        raw = '{"llm_scoring": {"response_complexity": 0.5, "emotional_weight": 0.2, "continuity_need": 0.4}}'
        result = classifier._parse_response(raw, user_input="前に話したあれってどうなった？")
        assert result["need_intent"] == "past_fact"

    def test_invalid_intent_falls_back_to_rule(self, classifier):
        """不正な need_intent 値 → ルール fallback"""
        raw = '{"llm_scoring": {"response_complexity": 0.5, "emotional_weight": 0.2, "continuity_need": 0.4}, "need_intent": "unknown_intent"}'
        result = classifier._parse_response(raw, user_input="前回の話の続きだけど")
        assert result["need_intent"] == "past_fact"

    def test_invalid_intent_no_pattern_returns_null(self, classifier):
        """不正な need_intent 値 + パターンなし → null"""
        raw = '{"llm_scoring": {"response_complexity": 0.3, "emotional_weight": 0.0, "continuity_need": 0.1}, "need_intent": 12345}'
        result = classifier._parse_response(raw, user_input="ありがとう")
        assert result["need_intent"] is None

    def test_default_output_uses_rule_fallback(self, classifier):
        """LLM 呼び出し失敗時の default_output もルール fallback を通る"""
        result = classifier._default_output(user_input="前に話したあれってどうなった？")
        assert result["need_intent"] == "past_fact"
        assert result["tier"] == "mid"

    def test_default_output_no_pattern_returns_null(self, classifier):
        result = classifier._default_output(user_input="今日も頑張ろう")
        assert result["need_intent"] is None


class TestClassifierDiagnostics:
    """classifier_status / fallback_reason の観測フィールドのテスト"""

    @pytest.fixture
    def classifier(self):
        return ContextClassifier()

    def test_valid_response_reports_ok(self, classifier):
        raw = (
            '{"llm_scoring": {"response_complexity": 0.5, "emotional_weight": 0.1, '
            '"continuity_need": 0.4}, "need_intent": "past_fact"}'
        )
        result = classifier._parse_response(raw, user_input="any")
        assert result["classifier_status"] == "ok"
        assert result["fallback_reason"] is None

    def test_empty_response_reason(self, classifier):
        result = classifier._parse_response("", user_input="any")
        assert result["classifier_status"] == "fallback"
        assert result["fallback_reason"] == "empty_response"

    def test_whitespace_response_reason(self, classifier):
        result = classifier._parse_response("   \n", user_input="any")
        assert result["fallback_reason"] == "empty_response"

    def test_parse_error_reason(self, classifier):
        result = classifier._parse_response("total garbage", user_input="any")
        assert result["classifier_status"] == "fallback"
        assert result["fallback_reason"] == "parse_error"

    def test_missing_need_intent_reason_keeps_llm_tier(self, classifier):
        """need_intent 欠落は部分 fallback — tier/scores は LLM 由来のまま"""
        raw = (
            '{"llm_scoring": {"response_complexity": 0.9, "emotional_weight": 0.1, '
            '"continuity_need": 0.8}}'
        )
        result = classifier._parse_response(raw, user_input="こんにちは")
        assert result["classifier_status"] == "fallback"
        assert result["fallback_reason"] == "missing_need_intent"
        assert result["tier"] == "mid"
        assert result["llm_scoring"]["response_complexity"] == 0.9

    def test_invalid_need_intent_reason(self, classifier):
        raw = (
            '{"llm_scoring": {"response_complexity": 0.2, "emotional_weight": 0.0, '
            '"continuity_need": 0.1}, "need_intent": "bogus_value"}'
        )
        result = classifier._parse_response(raw, user_input="ありがとう")
        assert result["fallback_reason"] == "invalid_need_intent"

    def test_default_output_reports_provider_error_by_default(self, classifier):
        result = classifier._default_output(user_input="any")
        assert result["classifier_status"] == "fallback"
        assert result["fallback_reason"] == "provider_error"


class TestNeedIntentFloor:
    """need_intent floor（LLM null + 明示的過去参照 → past_fact）のテスト"""

    @pytest.fixture
    def classifier(self):
        return ContextClassifier()

    def test_floor_promotes_null_on_when_question(self, classifier):
        """LLM が null でも時点質問なら past_fact に引き上げ — v8 qa_006 の実障害パターン"""
        result = {"tier": "reflex", "llm_scoring": {}, "need_intent": None}
        floored = classifier._apply_need_intent_floor(
            result, "When did Melanie run a charity race?"
        )
        assert floored["need_intent"] == "past_fact"

    def test_floor_promotes_null_on_ja_past_reference(self, classifier):
        result = {"tier": "mid", "llm_scoring": {}, "need_intent": None}
        floored = classifier._apply_need_intent_floor(result, "前に話したあの店の名前は？")
        assert floored["need_intent"] == "past_fact"

    def test_floor_keeps_null_without_pattern(self, classifier):
        """パターンなしなら LLM の null 判定を尊重する"""
        result = {"tier": "reflex", "llm_scoring": {}, "need_intent": None}
        floored = classifier._apply_need_intent_floor(result, "おはよう！")
        assert floored["need_intent"] is None

    def test_floor_preserves_existing_intent(self, classifier):
        """LLM が glossary 等を出していれば floor は上書きしない"""
        result = {"tier": "mid", "llm_scoring": {}, "need_intent": "glossary"}
        floored = classifier._apply_need_intent_floor(
            result, "remember the TierClassifier we discussed?"
        )
        assert floored["need_intent"] == "glossary"

    def test_floor_does_not_mutate_input(self, classifier):
        """floor は入力 dict を破壊しない"""
        result = {"tier": "mid", "llm_scoring": {}, "need_intent": None}
        classifier._apply_need_intent_floor(result, "When was the concert?")
        assert result["need_intent"] is None

    def test_floor_records_original_and_applied_flag(self, classifier):
        """floor 適用時は original_need_intent=None と applied=True を残す"""
        result = {"tier": "mid", "llm_scoring": {}, "need_intent": None}
        floored = classifier._apply_need_intent_floor(result, "When was the concert?")
        assert floored["original_need_intent"] is None
        assert floored["intent_floor_applied"] is True

    def test_no_floor_still_stamps_fields(self, classifier):
        """floor 不適用でも観測フィールドは常に付与される"""
        result = {"tier": "mid", "llm_scoring": {}, "need_intent": "glossary"}
        floored = classifier._apply_need_intent_floor(result, "何それ？")
        assert floored["original_need_intent"] == "glossary"
        assert floored["intent_floor_applied"] is False


class TestConfigurableTierThresholds:
    """tier 判定閾値の override テスト"""

    @pytest.fixture
    def classifier(self):
        return ContextClassifier()

    def test_default_thresholds(self, classifier):
        """override 無し → SYSTEM_CONFIG のデフォルト (rc<=0.4, cn<=0.3)"""
        # 境界近傍
        assert classifier._determine_tier_from_scores(
            {"response_complexity": 0.4, "continuity_need": 0.3}
        ) == "reflex"
        assert classifier._determine_tier_from_scores(
            {"response_complexity": 0.41, "continuity_need": 0.0}
        ) == "mid"

    def test_relaxed_thresholds_override(self, classifier):
        """instance_config で閾値を緩和 → reflex の範囲が広がる"""
        override = {"gatekeeper": {"tier_rc_threshold": 0.6, "tier_cn_threshold": 0.6}}
        # デフォルトなら mid 確定のスコアでも、緩和した閾値で reflex に
        result = classifier._determine_tier_from_scores(
            {"response_complexity": 0.5, "continuity_need": 0.5},
            override_config=override,
        )
        assert result == "reflex"

    def test_strict_thresholds_override(self, classifier):
        """閾値を厳格化 → reflex の範囲が狭まる"""
        override = {"gatekeeper": {"tier_rc_threshold": 0.1, "tier_cn_threshold": 0.1}}
        # デフォルトなら reflex でも、厳格化で mid に
        result = classifier._determine_tier_from_scores(
            {"response_complexity": 0.2, "continuity_need": 0.0},
            override_config=override,
        )
        assert result == "mid"

    def test_partial_override(self, classifier):
        """片方だけ override すれば、もう片方はデフォルト"""
        override = {"gatekeeper": {"tier_cn_threshold": 0.6}}
        # rc は デフォルト (0.4) のまま、cn は 0.6 まで OK
        assert classifier._determine_tier_from_scores(
            {"response_complexity": 0.4, "continuity_need": 0.6},
            override_config=override,
        ) == "reflex"
        # rc が 0.4 超なら mid
        assert classifier._determine_tier_from_scores(
            {"response_complexity": 0.5, "continuity_need": 0.6},
            override_config=override,
        ) == "mid"

    def test_parse_response_uses_override(self, classifier):
        """_parse_response 経由でも override が伝播する"""
        raw = '{"llm_scoring": {"response_complexity": 0.5, "emotional_weight": 0.0, "continuity_need": 0.5}, "need_intent": null}'
        # 緩和 override
        result = classifier._parse_response(
            raw, user_input="test",
            override_config={"gatekeeper": {"tier_rc_threshold": 0.6, "tier_cn_threshold": 0.6}},
        )
        assert result["tier"] == "reflex"
        # override 無し → mid
        result_default = classifier._parse_response(raw, user_input="test")
        assert result_default["tier"] == "mid"


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
