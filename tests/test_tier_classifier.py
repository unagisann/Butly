"""
test_tier_classifier.py
-----------------------
TierClassifier._determine_tier() のユニットテスト。
LLM呼び出し不要 — シグナル→tier変換のロジックのみ検証。
"""

import pytest

from butly_core.core.gatekeeper.tier_classifier import TierClassifier


class TestDetermineTier:
    """_determine_tier のシグナル→tier変換テスト"""

    @pytest.fixture
    def classifier(self):
        """TierClassifier インスタンス（LLMは呼ばない）"""
        return TierClassifier()

    def test_needs_long_term_search_returns_cortex(self, classifier):
        """needs_long_term_search=True → cortex"""
        result = classifier._determine_tier({
            "tier": "mid",
            "llm_reasoning": {
                "recent_context_sufficient": False,
                "needs_user_memory": False,
                "needs_relationship_state": False,
                "needs_long_term_search": True,
            }
        })
        assert result == "cortex"

    def test_recent_ok_no_memory_needed_returns_reflex(self, classifier):
        """recent_ok=True, 他全false → reflex"""
        result = classifier._determine_tier({
            "tier": "mid",
            "llm_reasoning": {
                "recent_context_sufficient": True,
                "needs_user_memory": False,
                "needs_relationship_state": False,
                "needs_long_term_search": False,
            }
        })
        assert result == "reflex"

    def test_needs_user_memory_returns_mid(self, classifier):
        """needs_user_memory=True → mid"""
        result = classifier._determine_tier({
            "tier": "reflex",
            "llm_reasoning": {
                "recent_context_sufficient": False,
                "needs_user_memory": True,
                "needs_relationship_state": False,
                "needs_long_term_search": False,
            }
        })
        assert result == "mid"

    def test_needs_relationship_state_returns_cortex(self, classifier):
        """needs_relationship_state=True → cortex"""
        result = classifier._determine_tier({
            "tier": "mid",
            "llm_reasoning": {
                "recent_context_sufficient": False,
                "needs_user_memory": False,
                "needs_relationship_state": True,
                "needs_long_term_search": False,
            }
        })
        assert result == "cortex"

    def test_all_false_falls_back_to_llm_tier(self, classifier):
        """全シグナルfalse → LLMのtierをフォールバック"""
        result = classifier._determine_tier({
            "tier": "mid",
            "llm_reasoning": {
                "recent_context_sufficient": False,
                "needs_user_memory": False,
                "needs_relationship_state": False,
                "needs_long_term_search": False,
            }
        })
        assert result == "mid"

    def test_all_false_with_cortex_llm_tier(self, classifier):
        """全シグナルfalse, LLM tier=cortex → cortex"""
        result = classifier._determine_tier({
            "tier": "cortex",
            "llm_reasoning": {
                "recent_context_sufficient": False,
                "needs_user_memory": False,
                "needs_relationship_state": False,
                "needs_long_term_search": False,
            }
        })
        assert result == "cortex"

    def test_empty_reasoning_falls_back_to_llm_tier(self, classifier):
        """llm_reasoning が空 → LLMのtierをフォールバック"""
        result = classifier._determine_tier({
            "tier": "reflex",
            "llm_reasoning": {}
        })
        assert result == "reflex"

    def test_long_term_search_overrides_recent_ok(self, classifier):
        """needs_long_term_search=True は recent_ok=True よりも優先"""
        result = classifier._determine_tier({
            "tier": "reflex",
            "llm_reasoning": {
                "recent_context_sufficient": True,
                "needs_user_memory": False,
                "needs_relationship_state": False,
                "needs_long_term_search": True,
            }
        })
        assert result == "cortex"

    def test_relationship_overrides_user_memory(self, classifier):
        """needs_relationship_state=True は needs_user_memory=True よりも優先（cortex）"""
        result = classifier._determine_tier({
            "tier": "mid",
            "llm_reasoning": {
                "recent_context_sufficient": False,
                "needs_user_memory": True,
                "needs_relationship_state": True,
                "needs_long_term_search": False,
            }
        })
        assert result == "cortex"

    def test_recent_ok_with_user_memory_returns_mid(self, classifier):
        """recent_ok=True + needs_user_memory=True → mid (memoryが優先)"""
        result = classifier._determine_tier({
            "tier": "reflex",
            "llm_reasoning": {
                "recent_context_sufficient": True,
                "needs_user_memory": True,
                "needs_relationship_state": False,
                "needs_long_term_search": False,
            }
        })
        # recent_ok=True but needs_user_memory=True → needs_mem check comes after reflex check
        # reflex requires recent_ok AND NOT needs_mem, so it won't be reflex
        # then needs_rel is false, needs_mem is true → mid
        assert result == "mid"


class TestParseResponse:
    """_parse_response のJSONパーステスト"""

    @pytest.fixture
    def classifier(self):
        return TierClassifier()

    def test_parse_json_with_code_fence(self, classifier):
        """```json ... ``` 形式の応答を正しくパースする"""
        raw = '```json\n{"tier": "mid", "llm_reasoning": {"recent_context_sufficient": true, "needs_user_memory": false, "needs_relationship_state": false, "needs_long_term_search": false}}\n```'
        result = classifier._parse_response(raw)
        assert result["llm_tier"] == "mid"
        assert result["llm_reasoning"]["recent_context_sufficient"] is True

    def test_parse_plain_json(self, classifier):
        """プレーンJSON応答をパースする"""
        raw = '{"tier": "cortex", "llm_reasoning": {"recent_context_sufficient": false, "needs_user_memory": false, "needs_relationship_state": false, "needs_long_term_search": true}}'
        result = classifier._parse_response(raw)
        assert result["tier"] == "cortex"

    def test_parse_empty_returns_default(self, classifier):
        """空の応答はデフォルト（mid）を返す"""
        result = classifier._parse_response("")
        assert result["tier"] == "mid"

    def test_parse_invalid_json_returns_default(self, classifier):
        """不正JSONはデフォルト（mid）を返す"""
        result = classifier._parse_response("not json at all")
        assert result["tier"] == "mid"
