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
        "When did Melanie run a charity race?",
        "when was the concert?",
        "When were they in Paris?",
        "What day did we meet?",
        "What year was that trip?",
        "What time did the party start?",
        "マラソンはいつ走ったの？",
        "引っ越しはいつでしたか",
        "彼女がいつ卒業したか知りたい",
    ])
    def test_when_question_patterns_detected(self, text):
        assert asks_for_specific_past_detail(text) is True

    @pytest.mark.parametrize("text", [
        "When is Melanie planning on going camping?",
        "When are we planning to meet again?",
        "When will the trip be scheduled?",
        "旅行はいつ行く予定？",
        "彼はいつ来るつもりなの",
    ])
    def test_plan_question_patterns_detected(self, text):
        """以前語られた予定の時期を尋ねる疑問文（予定語を要求）"""
        assert asks_for_specific_past_detail(text) is True

    @pytest.mark.parametrize("text", [
        "What time is it?",
        "When is the meeting?",  # 予定語なしの一般疑問は floor 対象外（LLM に委ねる）
        "いつもありがとう",
    ])
    def test_plan_words_required_for_when_is(self, text):
        assert asks_for_specific_past_detail(text) is False

    @pytest.mark.parametrize("text", [
        "今日の天気は？",
        "こんにちは",
        "新しいプロジェクトを始めよう",
        "Hello, how are you?",
        "ありがとう",
    ])
    def test_no_past_reference(self, text):
        assert asks_for_specific_past_detail(text) is False

    @pytest.mark.parametrize("text", [
        "When will we arrive?",
        "When should I call you?",
        "What time is it?",
        "What time should we meet tomorrow?",
        "いつも通りだよ",
        "いつか行きたいね、その島",
        "いつの間にか終わってた",
    ])
    def test_future_and_idiom_not_detected(self, text):
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

    def test_llm_past_fact_intent_triggers_deep_without_pattern(self):
        """LLM が past_fact 判定なら正規表現パターン不一致でも deep search する
        (二重ゲート解消: パターンは fallback 用の安全網であってフィルタではない)"""
        assert should_deep_search(
            "How did we end up resolving the logo issue?",
            layer1_hits=False, headline_match=False, glossary_match=False,
            need_intent="past_fact",
        ) is True

    def test_llm_past_fact_intent_overrides_headline(self):
        assert should_deep_search(
            "How did we end up resolving the logo issue?",
            layer1_hits=False, headline_match=True, glossary_match=False,
            need_intent="past_fact",
        ) is True

    def test_relationship_intent_still_requires_pattern(self):
        """relationship は常時 Deep 送りの判断前なのでパターン必須のまま"""
        assert should_deep_search(
            "How have I seemed lately?",
            layer1_hits=False, headline_match=False, glossary_match=False,
            need_intent="relationship",
        ) is False

    def test_layer1_hit_skips_deep_even_with_past_fact_intent(self):
        assert should_deep_search(
            "How did we end up resolving it?",
            layer1_hits=True, headline_match=False, glossary_match=False,
            need_intent="past_fact",
        ) is False


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
        hit = {"id": "1", "title": "テスト記憶", "summary": "テスト内容", "episode": "", "score": 0.75, "source": "vector"}
        brain.quick_vector_search.return_value = [hit]
        brain.quick_vector_search_diag.return_value = {
            "results": [hit],
            "diagnostics": {"threshold": 0.4, "fetched_count": 1, "passed_threshold": 1,
                            "top_raw_scores": [0.75], "top_final_scores": [0.75]},
        }
        brain.extract_keywords.return_value = {"keywords": ["テスト"]}
        brain.search_knowledge.return_value = []
        return brain

    @pytest.fixture
    def mock_brain_no_hit(self):
        brain = MagicMock()
        brain.quick_vector_search.return_value = []
        brain.quick_vector_search_diag.return_value = {
            "results": [],
            "diagnostics": {"threshold": 0.4, "fetched_count": 50, "passed_threshold": 0,
                            "top_raw_scores": [0.3, 0.25], "top_final_scores": [0.2, 0.15]},
        }
        brain.extract_keywords.return_value = {"keywords": ["テスト"]}
        brain.search_knowledge.return_value = []
        return brain

    @pytest.fixture
    def mock_brain_deep_hit(self):
        brain = MagicMock()
        brain.quick_vector_search.return_value = []
        brain.quick_vector_search_diag.return_value = {
            "results": [],
            "diagnostics": {"threshold": 0.4, "fetched_count": 50, "passed_threshold": 0,
                            "top_raw_scores": [], "top_final_scores": []},
        }
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
            need_intent="past_fact",
        )
        assert result["status"] == "hit"
        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["title"] == "テスト記憶"

    def test_no_hit(self, probe, mock_brain_no_hit, mock_memory_empty_glossary):
        result = probe.probe(
            user_input="今日の天気は？",
            brain=mock_brain_no_hit,
            memory_manager=mock_memory_empty_glossary,
            need_intent="past_fact",
        )
        assert result["status"] == "no_hit"
        assert result["candidates"] == []

    def test_deep_search_triggered(self, probe, mock_brain_deep_hit, mock_memory_empty_glossary):
        result = probe.probe(
            user_input="前に話したプロジェクトの件",
            brain=mock_brain_deep_hit,
            memory_manager=mock_memory_empty_glossary,
            need_intent="past_fact",
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
            need_intent="past_fact",
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
            need_intent="past_fact",
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
                need_intent="past_fact",
            )
            assert result["status"] == "no_hit"
            mock_brain_no_hit.extract_keywords.assert_not_called()


class TestNeedIntentGating:
    """need_intent によるゲートのテスト"""

    @pytest.fixture
    def probe(self):
        return MemoryProbe()

    @pytest.fixture
    def brain_with_vector(self):
        brain = MagicMock()
        hit = {"id": "1", "title": "X", "summary": "Y", "episode": "", "score": 0.8, "source": "vector"}
        brain.quick_vector_search.return_value = [hit]
        brain.quick_vector_search_diag.return_value = {
            "results": [hit],
            "diagnostics": {"threshold": 0.4, "fetched_count": 1, "passed_threshold": 1},
        }
        return brain

    @pytest.fixture
    def mm_with_glossary(self):
        mm = MagicMock()
        mm.get_glossary_raw.return_value = {
            "version": 1,
            "entries": [
                {"term": "Gatekeeper", "definition": "...", "aliases": [], "status": "active"},
            ],
        }
        return mm

    def test_need_intent_none_retrieves_but_does_not_inject(
        self, probe, brain_with_vector, mm_with_glossary
    ):
        """need_intent=None でも検索は走る。ただし注入はしない（§3.3）。

        検索実行と注入判定を分けたので、候補は retrieval に残り candidates は空。
        """
        result = probe.probe(
            user_input="Gatekeeperの話",
            brain=brain_with_vector,
            memory_manager=mm_with_glossary,
            need_intent=None,
        )
        assert result["status"] == "hit"  # glossary ヒット由来
        assert result["candidates"] == []
        assert len(result["glossary_hits"]) == 1
        brain_with_vector.quick_vector_search_diag.assert_called_once()
        assert result["retrieval"]["executed"] is True
        assert result["retrieval"]["candidate_count"] == 1
        assert result["retrieval"]["injection_allowed"] is False
        assert result["retrieval"]["injection_reason"] == "intent_gated"

    def test_need_intent_none_no_glossary_returns_no_hit(self, probe, brain_with_vector):
        """need_intent=None かつ glossary マッチなし → status=no_hit（注入なし）"""
        mm = MagicMock()
        mm.get_glossary_raw.return_value = {"version": 1, "entries": []}
        result = probe.probe(
            user_input="未知の話題",
            brain=brain_with_vector,
            memory_manager=mm,
            need_intent=None,
        )
        assert result["status"] == "no_hit"
        assert result["candidates"] == []
        assert result["glossary_hits"] == []

    def test_retrieval_execution_intent_gated_restores_old_behavior(
        self, probe, brain_with_vector, mm_with_glossary
    ):
        """retrieval_execution=intent_gated で旧挙動（検索自体をスキップ）へ戻せる"""
        result = probe.probe(
            user_input="Gatekeeperの話",
            brain=brain_with_vector,
            memory_manager=mm_with_glossary,
            need_intent=None,
            override_config={"memory_probe": {"retrieval_execution": "intent_gated"}},
        )
        assert result["status"] == "hit"
        assert result["candidates"] == []
        brain_with_vector.quick_vector_search_diag.assert_not_called()
        assert result["retrieval"]["executed"] is False
        assert result["retrieval"]["reason"] == "intent_gated:None"

    def test_need_intent_glossary_does_not_inject_cards(
        self, probe, brain_with_vector, mm_with_glossary
    ):
        """need_intent=glossary → カードは注入されない（検索は走ってよい）"""
        result = probe.probe(
            user_input="Gatekeeperって何？",
            brain=brain_with_vector,
            memory_manager=mm_with_glossary,
            need_intent="glossary",
        )
        assert result["candidates"] == []
        assert len(result["glossary_hits"]) == 1
        assert result["status"] == "hit"
        assert result["retrieval"]["injection_allowed"] is False

    def test_need_intent_glossary_no_hit(self, probe, brain_with_vector):
        """need_intent=glossary でマッチなし → no_hit"""
        mm = MagicMock()
        mm.get_glossary_raw.return_value = {"version": 1, "entries": []}
        result = probe.probe(
            user_input="未知の用語",
            brain=brain_with_vector,
            memory_manager=mm,
            need_intent="glossary",
        )
        assert result["status"] == "no_hit"
        assert result["candidates"] == []

    def test_retrieval_assisted_promotes_strong_evidence(
        self, probe, brain_with_vector, mm_with_glossary
    ):
        """injection_policy=retrieval_assisted: vector と BM25 の双方が支持した
        候補は、分類器が null でも注入候補へ昇格する（§3.3 Phase 1B）。"""
        hit = {
            "id": "1",
            "title": "X",
            "summary": "Y",
            "episode": "",
            "score": 0.03,
            "retrieval_source": "both",
        }
        brain_with_vector.quick_vector_search_diag.return_value = {
            "results": [hit],
            "diagnostics": {"mode": "hybrid"},
        }
        result = probe.probe(
            user_input="Gatekeeperの話",
            brain=brain_with_vector,
            memory_manager=mm_with_glossary,
            need_intent=None,
            override_config={
                "memory_probe": {"injection_policy": "retrieval_assisted"}
            },
        )
        assert len(result["candidates"]) == 1
        assert result["retrieval"]["injection_reason"] == "retrieval_assisted"
        assert result["retrieval"]["need_hint"] == "past_fact"

    def test_candidates_policy_injects_without_intent(
        self, probe, brain_with_vector, mm_with_glossary
    ):
        """injection_policy=candidates: 分類器 null でも候補があれば注入する。

        v26 実測で cosine・順位差・BM25 一致のどれも cat5 の adversarial 問を
        分離できず、検索側のゲートが作れなかったための policy（§3.3）。
        """
        result = probe.probe(
            user_input="Gatekeeperの話",
            brain=brain_with_vector,
            memory_manager=mm_with_glossary,
            need_intent=None,
            override_config={"memory_probe": {"injection_policy": "candidates"}},
        )

        assert len(result["candidates"]) == 1
        assert result["retrieval"]["injection_reason"] == "candidates"
        assert result["retrieval"]["need_hint"] == "past_fact"

    def test_candidates_policy_still_needs_candidates(
        self, probe, brain_with_vector, mm_with_glossary
    ):
        brain_with_vector.quick_vector_search_diag.return_value = {
            "results": [],
            "diagnostics": {"mode": "vector"},
        }
        brain_with_vector.extract_keywords.return_value = {"keywords": []}

        result = probe.probe(
            user_input="Gatekeeperの話",
            brain=brain_with_vector,
            memory_manager=mm_with_glossary,
            need_intent=None,
            override_config={"memory_probe": {"injection_policy": "candidates"}},
        )

        assert result["candidates"] == []
        assert result["retrieval"]["injection_reason"] == "no_candidates"

    def test_retrieval_assisted_rejects_weak_evidence(
        self, probe, brain_with_vector, mm_with_glossary
    ):
        """片側（vector のみ）の支持では昇格させない"""
        result = probe.probe(
            user_input="Gatekeeperの話",
            brain=brain_with_vector,
            memory_manager=mm_with_glossary,
            need_intent=None,
            override_config={
                "memory_probe": {"injection_policy": "retrieval_assisted"}
            },
        )
        assert result["candidates"] == []
        assert result["retrieval"]["injection_reason"] == "weak_evidence"

    def test_need_intent_past_fact_runs_vector(self, probe, brain_with_vector, mm_with_glossary):
        """need_intent=past_fact → vector も走る"""
        result = probe.probe(
            user_input="前回の話",
            brain=brain_with_vector,
            memory_manager=mm_with_glossary,
            need_intent="past_fact",
        )
        assert result["status"] == "hit"
        assert len(result["candidates"]) == 1
        brain_with_vector.quick_vector_search_diag.assert_called_once()

    def test_need_intent_relationship_runs_vector(self, probe, brain_with_vector, mm_with_glossary):
        result = probe.probe(
            user_input="最近のわたしどう？",
            brain=brain_with_vector,
            memory_manager=mm_with_glossary,
            need_intent="relationship",
        )
        assert result["status"] == "hit"
        brain_with_vector.quick_vector_search_diag.assert_called_once()


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
            "need_intent": "past_fact",
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
        assert result["need"] == "past_fact"
        assert result["need_intent"] == "past_fact"
        assert result["memory_probe"]["status"] == "hit"

    def test_probe_no_hit_stays_mid(self, mock_gatekeeper, test_instance_dir):
        """probe no_hit → mid のまま、need=null (事実裏付け失敗)"""
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

    def test_no_brain_glossary_no_hit_returns_null_need(self, mock_gatekeeper, test_instance_dir):
        """brain=None → probe は glossary のみ実行。glossary も無ければ need=null"""
        mock_gatekeeper.memory_probe.probe.return_value = {
            "status": "no_hit",
            "candidates": [],
            "glossary_hits": [],
        }
        result = mock_gatekeeper.classify(
            user_input="テスト",
            history_msgs=[],
            session_state={},
            instance_dir=test_instance_dir,
            brain=None,
        )

        assert result["need"] is None
        assert result["memory_probe"]["status"] == "no_hit"

    def test_need_intent_null_runs_probe_glossary_only(
        self, mock_gatekeeper, test_instance_dir
    ):
        """need_intent=None でも probe は呼ばれる (glossary scan は常時実行)。
        ただし need は立たない (LLM 意図が None のため)"""
        mock_gatekeeper.context_classifier.classify.return_value = {
            "tier": "reflex",
            "llm_scoring": {"response_complexity": 0.1, "emotional_weight": 0.0, "continuity_need": 0.0},
            "need_intent": None,
        }
        mock_gatekeeper.memory_probe.probe.return_value = {
            "status": "hit",
            "candidates": [],
            "glossary_hits": [
                {"term": "Gatekeeper", "definition": "...", "aliases": [],
                 "match_type": "term", "match_source": "user", "priority": 100, "_yaml_index": 0},
            ],
        }

        brain = MagicMock()
        result = mock_gatekeeper.classify(
            user_input="Gatekeeperってどう",
            history_msgs=[],
            session_state={},
            instance_dir=test_instance_dir,
            brain=brain,
        )

        assert result["tier"] == "reflex"
        # need_intent=None なので need は立たない
        assert result["need"] is None
        assert result["need_intent"] is None
        # probe 自体は呼ばれている (glossary scan のため)
        mock_gatekeeper.memory_probe.probe.assert_called_once()
        # glossary_hits は返却された
        assert len(result["memory_probe"]["glossary_hits"]) == 1

    def test_retrieval_assisted_candidates_set_need(
        self, mock_gatekeeper, test_instance_dir
    ):
        """need_intent=None でも probe が注入を許した候補は need を立てる。

        probe 側の injection policy を通った候補だけが candidates に載るので、
        Gatekeeper は「候補がある = 注入してよい」として扱ってよい。
        """
        mock_gatekeeper.context_classifier.classify.return_value = {
            "tier": "mid",
            "llm_scoring": {"response_complexity": 0.5, "emotional_weight": 0.1,
                            "continuity_need": 0.2},
            "need_intent": None,
        }
        mock_gatekeeper.memory_probe.probe.return_value = {
            "status": "hit",
            "candidates": [{"title": "陶芸教室", "summary": "内容", "score": 0.03}],
            "glossary_hits": [],
            "retrieval": {"injection_reason": "retrieval_assisted",
                          "need_hint": "past_fact"},
        }

        result = mock_gatekeeper.classify(
            user_input="陶芸の話",
            history_msgs=[],
            session_state={},
            instance_dir=test_instance_dir,
            brain=MagicMock(),
        )

        assert result["need"] == "past_fact"
        assert result["need_intent"] is None
        assert result["search_targets"] == ["陶芸教室"]

    def test_deep_search_sets_need(self, mock_gatekeeper, test_instance_dir):
        """deep_search hit → tier は mid のまま、need=past_fact"""
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
        assert result["need"] == "past_fact"
        assert result["search_targets"] == ["過去の会話"]

    def test_past_fact_intent_with_only_glossary_returns_null(
        self, mock_gatekeeper, test_instance_dir
    ):
        """intent=past_fact だが vector 空振り + glossary のみ → need=None
        (glossary は past_fact の事実裏付けにはならない)
        """
        mock_gatekeeper.memory_probe.probe.return_value = {
            "status": "no_hit",
            "candidates": [],
            "glossary_hits": [
                {"term": "Gatekeeper", "definition": "...", "aliases": [],
                 "match_type": "term", "match_source": "user", "priority": 100, "_yaml_index": 0},
            ],
        }

        brain = MagicMock()
        result = mock_gatekeeper.classify(
            user_input="Gatekeeperどう？",
            history_msgs=[],
            session_state={},
            instance_dir=test_instance_dir,
            brain=brain,
        )

        assert result["tier"] == "mid"
        assert result["need"] is None
        assert result["need_intent"] == "past_fact"
        assert result["search_targets"] is None
        assert len(result["memory_probe"]["glossary_hits"]) == 1

    def test_glossary_intent_with_glossary_hits_sets_need(
        self, mock_gatekeeper, test_instance_dir
    ):
        """intent=glossary + glossary_hits → need=glossary、search_targets は用語"""
        mock_gatekeeper.context_classifier.classify.return_value = {
            "tier": "mid",
            "llm_scoring": {"response_complexity": 0.4, "emotional_weight": 0.0, "continuity_need": 0.2},
            "need_intent": "glossary",
        }
        mock_gatekeeper.memory_probe.probe.return_value = {
            "status": "hit",
            "candidates": [],
            "glossary_hits": [
                {"term": "Sleeptime", "definition": "...", "aliases": [],
                 "match_type": "term", "match_source": "user", "priority": 100, "_yaml_index": 0},
            ],
        }

        brain = MagicMock()
        result = mock_gatekeeper.classify(
            user_input="Sleeptimeって何？",
            history_msgs=[],
            session_state={},
            instance_dir=test_instance_dir,
            brain=brain,
        )

        assert result["need"] == "glossary"
        assert result["search_targets"] == ["Sleeptime"]

    def test_glossary_intent_no_glossary_hit_returns_null(
        self, mock_gatekeeper, test_instance_dir
    ):
        """intent=glossary だが glossary_hits 無し → need=None"""
        mock_gatekeeper.context_classifier.classify.return_value = {
            "tier": "mid",
            "llm_scoring": {"response_complexity": 0.4, "emotional_weight": 0.0, "continuity_need": 0.2},
            "need_intent": "glossary",
        }
        mock_gatekeeper.memory_probe.probe.return_value = {
            "status": "no_hit",
            "candidates": [],
            "glossary_hits": [],
        }

        brain = MagicMock()
        result = mock_gatekeeper.classify(
            user_input="未知の用語って何？",
            history_msgs=[],
            session_state={},
            instance_dir=test_instance_dir,
            brain=brain,
        )

        assert result["need"] is None
        assert result["search_targets"] is None

    def test_classify_does_not_call_state_updater(self, mock_gatekeeper, test_instance_dir):
        """classify() は StateUpdater を呼ばない (post-response で別途実行)"""
        mock_gatekeeper.memory_probe.probe.return_value = {
            "status": "skipped", "candidates": [], "glossary_hits": [],
        }
        mock_gatekeeper.context_classifier.classify.return_value = {
            "tier": "mid",
            "llm_scoring": {"response_complexity": 0.5, "emotional_weight": 0.3, "continuity_need": 0.4},
            "need_intent": None,
        }

        result = mock_gatekeeper.classify(
            user_input="test",
            history_msgs=[],
            session_state={"topic": "前ターンの話題"},
            instance_dir=test_instance_dir,
            brain=MagicMock(),
        )

        # StateUpdater は呼ばれない
        mock_gatekeeper.state_updater.update.assert_not_called()
        # state_delta は空
        assert result["state_delta"] == {}
        # topic は session_state からのフォールバック (1 ターン前)
        assert result["topic"] == "前ターンの話題"

    def test_update_state_calls_state_updater(self, mock_gatekeeper, test_instance_dir):
        """update_state() は StateUpdater を呼んで state_delta を返す"""
        mock_gatekeeper.state_updater.update.return_value = {
            "topic": "新しい話題", "mood": "focused",
        }

        result = mock_gatekeeper.update_state(
            user_input="何かの発話",
            history_msgs=[],
            session_state={"topic": "古い話題"},
            instance_dir=test_instance_dir,
        )

        mock_gatekeeper.state_updater.update.assert_called_once()
        assert result["topic"] == "新しい話題"
        assert result["mood"] == "focused"

    def test_session_state_topic_passed_to_classifier(
        self, mock_gatekeeper, test_instance_dir
    ):
        """session_state の topic が ContextClassifier の current_topic に配線される。
        (v8 評価で発覚: 戻り値にしか使われず classifier は常に「(未設定)」だった)"""
        mock_gatekeeper.memory_probe.probe.return_value = {
            "status": "no_hit", "candidates": [], "glossary_hits": [],
        }

        mock_gatekeeper.classify(
            user_input="test",
            history_msgs=[],
            session_state={"topic": "前ターンの話題"},
            instance_dir=test_instance_dir,
            brain=MagicMock(),
        )

        call = mock_gatekeeper.context_classifier.classify.call_args
        assert call.args[2] == "前ターンの話題"

    def test_explicit_current_topic_wins_over_session_state(
        self, mock_gatekeeper, test_instance_dir
    ):
        """引数 current_topic 明示時はそちらを優先 (classify_tier_only 互換)"""
        mock_gatekeeper.memory_probe.probe.return_value = {
            "status": "no_hit", "candidates": [], "glossary_hits": [],
        }

        result = mock_gatekeeper.classify(
            user_input="test",
            history_msgs=[],
            session_state={"topic": "state側の話題"},
            current_topic="明示された話題",
            instance_dir=test_instance_dir,
            brain=MagicMock(),
        )

        call = mock_gatekeeper.context_classifier.classify.call_args
        assert call.args[2] == "明示された話題"
        assert result["topic"] == "明示された話題"

    def test_non_dict_session_state_keeps_empty_topic(
        self, mock_gatekeeper, test_instance_dir
    ):
        """session_state が dict でなくても落ちない"""
        mock_gatekeeper.memory_probe.probe.return_value = {
            "status": "no_hit", "candidates": [], "glossary_hits": [],
        }

        result = mock_gatekeeper.classify(
            user_input="test",
            history_msgs=[],
            session_state=None,
            instance_dir=test_instance_dir,
            brain=MagicMock(),
        )

        call = mock_gatekeeper.context_classifier.classify.call_args
        assert call.args[2] == ""
        assert result["topic"] == ""


# ===================================================================
# instance/profile による probe 設定の上書き
# ===================================================================

class TestProbeConfigOverride:
    """memory_probe / brain 設定が override_config で上書きされるか。

    従来 probe は SYSTEM_CONFIG 直読みで、memory / brain セクションと違い
    instance ごとの調整ができなかった（LoCoMo profile でも効かない）。
    """

    @pytest.fixture
    def probe(self):
        return MemoryProbe()

    @pytest.fixture
    def brain(self):
        brain = MagicMock()
        brain.quick_vector_search_diag.return_value = {
            "results": [],
            "diagnostics": {"threshold": 0.4, "fetched_count": 0,
                            "passed_threshold": 0, "top_raw_scores": [],
                            "top_final_scores": []},
        }
        brain.extract_keywords.return_value = {"keywords": []}
        brain.search_knowledge.return_value = []
        return brain

    def test_vector_search_limit_overridden(self, probe, brain):
        probe.probe(
            user_input="前に話したあの件どうなった？",
            brain=brain,
            memory_manager=MagicMock(),
            need_intent="past_fact",
            override_config={"memory_probe": {"vector_search_limit": 8}},
        )

        assert brain.quick_vector_search_diag.call_args.kwargs["limit"] == 8

    def test_falls_back_to_system_config(self, probe, brain):
        probe.probe(
            user_input="前に話したあの件どうなった？",
            brain=brain,
            memory_manager=MagicMock(),
            need_intent="past_fact",
        )

        # 既定 (SYSTEM_CONFIG["memory_probe"]["vector_search_limit"]) を使う
        assert brain.quick_vector_search_diag.call_args.kwargs["limit"] == 3

    def test_deep_search_limit_overridden(self, probe, brain):
        """Layer 2 の brain.search_limit も override を尊重する"""
        brain.extract_keywords.return_value = {"keywords": ["日曜"]}
        probe.probe(
            user_input="先週の日曜に何をしたか教えて",
            brain=brain,
            memory_manager=MagicMock(),
            need_intent="past_fact",
            override_config={"brain": {"search_limit": 7}},
        )

        assert brain.search_knowledge.call_args.kwargs["limit"] == 7
