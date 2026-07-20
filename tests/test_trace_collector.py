"""
test_trace_collector.py
-----------------------
Trace Graph の LLM 呼び出し収集 (issue #51 拡張) のユニットテスト。

- collector: start/reset/record/get、並列タスク・thread からの記録、no-op
- 記録ポイント: ContextClassifier / StateUpdater / Brain (embedding, keywords)
- builder: llm_calls → 補助 LLM ノード生成、chat_generate の llm_call 統合
- filters: detail="summary" / hidden_nodes (purpose・node id)
- 設定: SYSTEM_CONFIG["trace"].enabled=False で trace.json を保存しない
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from butly_core.trace import (
    build_chat_trace,
    filter_trace,
    get_collected,
    is_collecting,
    record_llm_call,
    render_mermaid,
    reset_collection,
    start_collection,
)


def _base_trace_kwargs(**overrides):
    base = dict(
        instance_name="x",
        user_input="hi",
        assistant_response="ok",
        gk_result={
            "tier": "mid",
            "need": "past_fact",
            "memory_probe": {"status": "hit", "candidates": [], "glossary_hits": []},
            "state_delta": {"topic": "t"},
        },
        tier="mid",
        memory_blocks={"short_term": [1]},
        gk_enabled=True,
        connection_id="openai",
        model_name="gpt-4o",
        provider_name="OpenAIProvider",
        timing={"generation_ms": 1500},
        token_estimate={},
        turn_id=1,
    )
    base.update(overrides)
    return base


# ===================================================================
# collector
# ===================================================================


class TestCollector:
    def test_record_is_noop_when_not_started(self):
        record_llm_call(purpose="embedding", model="m")
        assert get_collected() == []
        assert not is_collecting()

    def test_start_record_reset(self):
        token = start_collection()
        try:
            assert is_collecting()
            record_llm_call(
                purpose="context_classifier",
                model="flash",
                duration_ms=100,
                prompt_chars=500,
            )
            calls = get_collected()
            assert len(calls) == 1
            assert calls[0]["purpose"] == "context_classifier"
            assert calls[0]["duration_ms"] == 100
            assert calls[0]["error"] is None
        finally:
            reset_collection(token)
        assert not is_collecting()
        assert get_collected() == []

    def test_get_collected_returns_copy(self):
        token = start_collection()
        try:
            record_llm_call(purpose="embedding")
            snapshot = get_collected()
            snapshot.append({"purpose": "fake"})
            assert len(get_collected()) == 1
        finally:
            reset_collection(token)

    def test_parallel_tasks_and_threads_share_list(self):
        """asyncio.gather の並列タスクと thread からの記録が同じ list に届く。"""

        async def main():
            token = start_collection()
            try:
                record_llm_call(purpose="context_classifier")

                async def task_record():
                    record_llm_call(purpose="state_updater")

                async def thread_record():
                    await asyncio.to_thread(record_llm_call, purpose="embedding")

                await asyncio.gather(
                    asyncio.create_task(task_record()), thread_record()
                )
                record_llm_call(purpose="chat_generate")
                return get_collected()
            finally:
                reset_collection(token)

        calls = asyncio.run(main())
        purposes = {c["purpose"] for c in calls}
        assert purposes == {
            "context_classifier",
            "state_updater",
            "embedding",
            "chat_generate",
        }

    def test_nested_collection_restores_outer(self):
        outer = start_collection()
        try:
            record_llm_call(purpose="embedding")
            inner = start_collection()
            record_llm_call(purpose="keyword_extract")
            assert [c["purpose"] for c in get_collected()] == ["keyword_extract"]
            reset_collection(inner)
            assert [c["purpose"] for c in get_collected()] == ["embedding"]
        finally:
            reset_collection(outer)


# ===================================================================
# 記録ポイント (実モジュール + mock provider)
# ===================================================================


@pytest.fixture
def collection():
    token = start_collection()
    yield
    reset_collection(token)


class TestRecordPoints:
    def test_context_classifier_records(self, collection):
        from butly_core.core.gatekeeper.context_classifier import ContextClassifier

        provider = MagicMock()
        provider.classify.return_value = (
            '```json{"llm_scoring": {"response_complexity": 0.2, '
            '"emotional_weight": 0.1, "continuity_need": 0.1}, '
            '"need_intent": null}```'
        )
        with patch(
            "butly_core.llm.factory.ProviderFactory.create", return_value=provider
        ):
            ContextClassifier().classify("こんにちは", [])

        calls = [c for c in get_collected() if c["purpose"] == "context_classifier"]
        assert len(calls) == 1
        assert calls[0]["error"] is None
        assert calls[0]["prompt_chars"] > 0
        assert calls[0]["duration_ms"] >= 0

    def test_context_classifier_records_error(self, collection):
        from butly_core.core.gatekeeper.context_classifier import ContextClassifier

        with patch(
            "butly_core.llm.factory.ProviderFactory.create",
            side_effect=RuntimeError("api down"),
        ):
            result = ContextClassifier().classify("こんにちは", [])

        assert result["tier"] == "mid"  # フォールバック動作は不変
        calls = [c for c in get_collected() if c["purpose"] == "context_classifier"]
        assert len(calls) == 1
        assert "api down" in calls[0]["error"]

    def test_state_updater_records(self, collection):
        from butly_core.core.gatekeeper.state_updater import StateUpdater

        provider = MagicMock()
        provider.classify.return_value = '```json{"topic": "旅行", "mood": null}```'
        with patch(
            "butly_core.llm.factory.ProviderFactory.create", return_value=provider
        ):
            StateUpdater().update("沖縄いきたい", [], {"topic": "", "mood": "neutral"})

        calls = [c for c in get_collected() if c["purpose"] == "state_updater"]
        assert len(calls) == 1
        assert calls[0]["error"] is None

    def test_brain_embedding_records(self, collection, tmp_path):
        from butly_core.core.brain import ButlyBrain

        brain = ButlyBrain(tmp_path)
        provider = MagicMock()
        provider.embed.return_value = [0.1, 0.2]
        with patch.object(brain, "_get_provider", return_value=provider):
            assert brain.get_embedding("テキスト") == [0.1, 0.2]

        calls = [c for c in get_collected() if c["purpose"] == "embedding"]
        assert len(calls) == 1
        assert calls[0]["prompt_chars"] == len("テキスト")

    def test_brain_embedding_records_error(self, collection, tmp_path):
        from butly_core.core.brain import ButlyBrain

        brain = ButlyBrain(tmp_path)
        with patch.object(
            brain, "_get_provider", side_effect=RuntimeError("embed down")
        ):
            assert brain.get_embedding("t") is None

        calls = [c for c in get_collected() if c["purpose"] == "embedding"]
        assert len(calls) == 1
        assert "embed down" in calls[0]["error"]

    def test_brain_keyword_extract_records_once_on_parse_failure(
        self, collection, tmp_path
    ):
        """LLM 呼び出しは成功、後段の JSON パースが失敗 → error なしで 1 件記録。"""
        from butly_core.core.brain import ButlyBrain

        brain = ButlyBrain(tmp_path)
        provider = MagicMock()
        provider.classify.return_value = "not a json"
        with patch.object(brain, "_get_provider", return_value=provider):
            assert brain.extract_keywords("hello") == {"keywords": []}

        calls = [c for c in get_collected() if c["purpose"] == "keyword_extract"]
        assert len(calls) == 1
        assert calls[0]["error"] is None

    def test_brain_keyword_extract_records_call_error(self, collection, tmp_path):
        from butly_core.core.brain import ButlyBrain

        brain = ButlyBrain(tmp_path)
        provider = MagicMock()
        provider.classify.side_effect = RuntimeError("classify down")
        with patch.object(brain, "_get_provider", return_value=provider):
            assert brain.extract_keywords("hello") == {"keywords": []}

        calls = [c for c in get_collected() if c["purpose"] == "keyword_extract"]
        assert len(calls) == 1
        assert "classify down" in calls[0]["error"]


# ===================================================================
# builder: 補助 LLM ノード
# ===================================================================


def _sample_calls():
    return [
        {
            "purpose": "context_classifier",
            "model": "flash",
            "connection_id": "google",
            "duration_ms": 120,
            "prompt_chars": 800,
            "error": None,
        },
        {
            "purpose": "embedding",
            "model": "emb-2",
            "connection_id": "google",
            "duration_ms": 40,
            "prompt_chars": 20,
            "error": None,
        },
        {
            "purpose": "embedding",
            "model": "emb-2",
            "connection_id": "google",
            "duration_ms": 42,
            "prompt_chars": 25,
            "error": None,
        },
        {
            "purpose": "state_updater",
            "model": "flash",
            "connection_id": "google",
            "duration_ms": 95,
            "prompt_chars": 400,
            "error": "boom",
        },
        {
            "purpose": "chat_generate",
            "model": "gpt-4o",
            "connection_id": "openai",
            "duration_ms": 1500,
            "prompt_chars": 5000,
            "error": None,
        },
    ]


class TestBuilderAuxNodes:
    def test_aux_nodes_created_with_parents(self):
        trace = build_chat_trace(**_base_trace_kwargs(llm_calls=_sample_calls()))
        ids = [n.id for n in trace.nodes]
        assert "llm_context_classifier" in ids
        assert "llm_embedding" in ids
        assert "llm_embedding_2" in ids  # 同一 purpose は連番
        assert "llm_state_updater" in ids
        assert "llm_chat_generate" not in ids  # main は既存 llm_call に統合

        edges = {(e.source, e.target) for e in trace.edges}
        assert ("gatekeeper", "llm_context_classifier") in edges
        assert ("memory_probe", "llm_embedding") in edges
        assert ("state_update", "llm_state_updater") in edges

    def test_aux_node_error_status(self):
        trace = build_chat_trace(**_base_trace_kwargs(llm_calls=_sample_calls()))
        su = next(n for n in trace.nodes if n.id == "llm_state_updater")
        assert su.status == "error"
        assert su.metadata["error"] == "boom"

    def test_aux_metadata_and_flag(self):
        trace = build_chat_trace(**_base_trace_kwargs(llm_calls=_sample_calls()))
        cc = next(n for n in trace.nodes if n.id == "llm_context_classifier")
        assert cc.metadata["aux"] is True
        assert cc.metadata["purpose"] == "context_classifier"
        assert cc.metadata["duration_ms"] == 120
        assert "flash" in cc.summary and "120ms" in cc.summary

    def test_chat_generate_enriches_llm_call(self):
        trace = build_chat_trace(**_base_trace_kwargs(llm_calls=_sample_calls()))
        llm = next(n for n in trace.nodes if n.id == "llm_call")
        assert llm.metadata["prompt_chars"] == 5000

    def test_no_llm_calls_keeps_main_flow_only(self):
        trace = build_chat_trace(**_base_trace_kwargs(llm_calls=None))
        assert not any(n.metadata.get("aux") for n in trace.nodes)

    def test_unknown_purpose_attaches_to_llm_call(self):
        calls = [{"purpose": "mcp_tool", "model": "m", "duration_ms": 5}]
        trace = build_chat_trace(**_base_trace_kwargs(llm_calls=calls))
        assert any(n.id == "llm_mcp_tool" for n in trace.nodes)
        edges = {(e.source, e.target) for e in trace.edges}
        assert ("llm_call", "llm_mcp_tool") in edges


# ===================================================================
# filters / mermaid 表示
# ===================================================================


class TestFilters:
    def _trace(self):
        return build_chat_trace(**_base_trace_kwargs(llm_calls=_sample_calls()))

    def test_full_is_passthrough(self):
        t = self._trace()
        assert filter_trace(t) is t

    def test_summary_hides_aux_nodes(self):
        t = filter_trace(self._trace(), detail="summary")
        assert not any(n.metadata.get("aux") for n in t.nodes)
        # メインフローは残る
        ids = {n.id for n in t.nodes}
        assert {"user_message", "gatekeeper", "llm_call", "response"} <= ids

    def test_hidden_by_purpose_hides_all_instances(self):
        t = filter_trace(self._trace(), hidden_nodes=["embedding"])
        ids = {n.id for n in t.nodes}
        assert "llm_embedding" not in ids and "llm_embedding_2" not in ids
        assert "llm_context_classifier" in ids

    def test_hidden_by_node_id(self):
        t = filter_trace(self._trace(), hidden_nodes=["web_search"])
        assert "web_search" not in {n.id for n in t.nodes}
        assert not any("web_search" in (e.source, e.target) for e in t.edges)

    def test_render_mermaid_applies_filters(self):
        full = render_mermaid(self._trace())
        summary = render_mermaid(self._trace(), detail="summary")
        hidden = render_mermaid(self._trace(), hidden_nodes=["embedding"])
        assert "llm_context_classifier" in full
        assert "llm_context_classifier" not in summary
        assert "llm_embedding" not in hidden
        assert "llm_context_classifier" in hidden


# ===================================================================
# 設定: trace.enabled=False で保存しない
# ===================================================================


class TestTraceSettingsGate:
    def test_trace_section_in_defaults(self):
        from butly_core.settings import SystemConfig

        cfg = SystemConfig()
        assert cfg.trace.get("enabled") is True
        assert cfg.trace.get("detail") == "full"
        assert cfg.trace.get("hidden_nodes") == []

    def test_disabled_skips_save(self, tmp_path):
        from butly_core.chat.service import _build_and_save_trace
        from butly_core.settings import (
            RootSettings,
            SystemConfig,
            override_settings,
        )

        instance_dir = tmp_path / "Inst"
        instance_dir.mkdir()
        prepared = MagicMock()
        prepared.memory_blocks = {}
        prepared.gk_enabled = True
        prepared.gk_error = None
        prepared.web_search_status = ""
        prepared.web_search_count = 0
        prepared.has_attachments = False
        request = MagicMock()
        request.text = "hi"
        request.source = "web"
        session_state = MagicMock()
        session_state.to_dict.return_value = {"turn_count": 1}

        common = dict(
            instance_dir=instance_dir,
            instance_name="Inst",
            request=request,
            prepared=prepared,
            tier="mid",
            gk_result={"state_delta": {}},
            assistant_response="ok",
            debug_info={"timing": {}, "token_estimate": {}},
            session_state=session_state,
            provider=MagicMock(),
            connection_id="openai",
            model_name="gpt-4o",
        )

        disabled = RootSettings(system=SystemConfig(trace={"enabled": False}))
        with override_settings(disabled):
            _build_and_save_trace(**common)
        assert not (instance_dir / "traces").exists()

        enabled = RootSettings(system=SystemConfig(trace={"enabled": True}))
        with override_settings(enabled):
            _build_and_save_trace(**common)
        assert (instance_dir / "traces" / "latest.json").exists()


# ===================================================================
# usage_metadata / aggregate_token_usage (API 実測トークンのコスト観測)
# ===================================================================

class TestTokenUsageAggregation:
    def _usage(self, prompt, completion):
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "source": "api",
        }

    def test_usage_metadata_pops_provider_slot(self):
        from butly_core.trace.collector import usage_metadata

        provider = MagicMock(spec=["pop_last_token_usage"])
        provider.pop_last_token_usage.return_value = self._usage(100, 10)
        assert usage_metadata(provider) == {"token_usage": self._usage(100, 10)}

        provider.pop_last_token_usage.return_value = None
        assert usage_metadata(provider) is None

    def test_usage_metadata_tolerates_provider_without_slot(self):
        from butly_core.trace.collector import usage_metadata

        assert usage_metadata(object()) is None

    def test_aggregate_sums_by_purpose(self):
        from butly_core.trace.collector import aggregate_token_usage

        token = start_collection()
        try:
            record_llm_call(
                purpose="context_classifier",
                metadata={"token_usage": self._usage(500, 50)},
            )
            record_llm_call(purpose="embedding")  # usage 無し → 集計対象外
            record_llm_call(
                purpose="chat_generate",
                metadata={"token_usage": self._usage(4000, 30)},
            )
            record_llm_call(
                purpose="state_updater",
                metadata={"token_usage": self._usage(300, 20)},
            )
            total = aggregate_token_usage(get_collected())
        finally:
            reset_collection(token)

        assert total["prompt_tokens"] == 4800
        assert total["completion_tokens"] == 100
        assert total["calls"] == 3
        assert total["by_purpose"]["chat_generate"]["prompt_tokens"] == 4000
        assert total["by_purpose"]["context_classifier"]["calls"] == 1

    def test_aggregate_none_when_no_usage(self):
        from butly_core.trace.collector import aggregate_token_usage

        assert aggregate_token_usage([]) is None
        assert (
            aggregate_token_usage([{"purpose": "embedding", "metadata": {}}]) is None
        )
