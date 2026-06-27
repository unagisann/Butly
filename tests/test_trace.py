"""
test_trace.py
-------------
Trace Graph 機能 (issue #51) のユニットテスト。

- build_chat_trace(): 実行事実から TraceGraph を再構成するビルダー
- render_mermaid():   TraceGraph → Mermaid flowchart 文字列
- _save_trace():      traces/latest.json + history/ ローテーション保存
"""

import json
import time

import pytest

from butly_core.chat.service import _save_trace
from butly_core.trace import build_chat_trace, render_mermaid
from butly_core.trace.mermaid import _sanitize
from butly_core.trace.types import TRACE_SCHEMA_VERSION

# 期待するノード ID（メインフロー）
EXPECTED_NODE_IDS = [
    "user_message",
    "gatekeeper",
    "memory_probe",
    "rag",
    "web_search",
    "context_assembly",
    "provider",
    "llm_call",
    "memory_write",
    "state_update",
    "response",
]


def _full_trace(**overrides):
    """全ノード active の代表的な trace を作るヘルパー。overrides で差し替え可能。"""
    base = dict(
        instance_name="00_master",
        user_input="テスト入力",
        assistant_response="テスト応答",
        gk_result={
            "tier": "mid",
            "need": "past_fact",
            "need_intent": "past_fact",
            "llm_scoring": {
                "response_complexity": 0.7,
                "emotional_weight": 0.2,
                "continuity_need": 0.5,
            },
            "memory_probe": {
                "status": "hit",
                "candidates": [{"id": 1}],
                "glossary_hits": [],
                "layers": {"glossary": {"executed": True}},
            },
            "state_delta": {"topic": "design"},
        },
        tier="mid",
        memory_blocks={
            "short_term": [1, 2],
            "session_digest": "digest",
            "mid_term": "mid",
            "glossary_hits": [{"term": "Butly"}],
            "rag_context": "ctx",
            "rag_results_raw": [{"id": 1, "title": "Design notes", "score": 0.71}],
            "web_search_context": "web",
            "mid_term_mode": "raw",
        },
        gk_enabled=True,
        web_search_status="active",
        web_search_count=3,
        connection_id="google",
        model_name="gemini-2.5-flash",
        provider_name="GeminiProvider",
        has_attachments=False,
        timing={"generation_ms": 1200, "ttfb_ms": 300, "state_update_ms": 50},
        token_estimate={"prompt": 2000, "response": 400},
        turn_id=42,
        source="web",
    )
    base.update(overrides)
    return build_chat_trace(**base)


# ===================================================================
# build_chat_trace
# ===================================================================


class TestBuildChatTrace:
    def test_node_ids_and_order(self):
        trace = _full_trace()
        assert [n.id for n in trace.nodes] == EXPECTED_NODE_IDS

    def test_schema_version_and_trace_id(self):
        trace = _full_trace(turn_id=42)
        d = trace.to_json_dict()
        assert d["schema_version"] == TRACE_SCHEMA_VERSION
        assert d["trace_id"] == "turn_42"
        assert d["turn_id"] == 42

    def test_trace_id_unknown_when_no_turn(self):
        trace = _full_trace(turn_id=None)
        assert trace.trace_id == "turn_unknown"

    def test_edge_serializes_with_from_to_alias(self):
        d = _full_trace().to_json_dict()
        first = d["edges"][0]
        assert first["from"] == "user_message"
        assert first["to"] == "gatekeeper"
        assert "source" not in first and "target" not in first

    def test_all_active_path(self):
        statuses = {n.id: n.status for n in _full_trace().nodes}
        assert all(s == "active" for s in statuses.values()), statuses

    def test_gatekeeper_disabled_marks_skipped(self):
        trace = _full_trace(
            gk_enabled=False,
            gk_result={"tier": "mid", "need": None, "state_delta": {}},
            memory_blocks={"short_term": [1]},
        )
        st = {n.id: n.status for n in trace.nodes}
        assert st["gatekeeper"] == "skipped"
        assert st["memory_probe"] == "skipped"
        assert st["state_update"] == "skipped"

    def test_gatekeeper_error_marks_fallback(self):
        trace = _full_trace(gk_error="boom")
        gk = next(n for n in trace.nodes if n.id == "gatekeeper")
        assert gk.status == "fallback"
        assert gk.metadata["error"] == "boom"

    def test_rag_active_metadata(self):
        rag = next(n for n in _full_trace().nodes if n.id == "rag")
        assert rag.status == "active"
        assert rag.metadata["result_count"] == 1
        assert rag.metadata["results"][0]["title"] == "Design notes"

    def test_rag_skipped_reason_when_need_null(self):
        trace = _full_trace(
            gk_result={"tier": "mid", "need": None, "state_delta": {}},
            memory_blocks={"short_term": [1]},
        )
        rag = next(n for n in trace.nodes if n.id == "rag")
        assert rag.status == "skipped"
        assert "need=null" in rag.summary

    @pytest.mark.parametrize(
        "status,expected",
        [
            ("active", "active"),
            ("no_results", "warning"),
            ("unavailable", "warning"),
            ("disabled", "skipped"),
            ("native_google", "skipped"),
            ("", "skipped"),
        ],
    )
    def test_web_search_status_mapping(self, status, expected):
        trace = _full_trace(web_search_status=status)
        web = next(n for n in trace.nodes if n.id == "web_search")
        assert web.status == expected

    def test_context_assembly_included_excluded(self):
        trace = _full_trace(
            memory_blocks={"short_term": [1], "rag_context": "x"},
        )
        ctx = next(n for n in trace.nodes if n.id == "context_assembly")
        assert "short_term" in ctx.metadata["included"]
        assert "rag" in ctx.metadata["included"]
        assert "glossary" in ctx.metadata["excluded"]
        assert "mid_term" in ctx.metadata["excluded"]

    def test_summary_is_truncated(self):
        trace = _full_trace(user_input="あ" * 200)
        user = next(n for n in trace.nodes if n.id == "user_message")
        assert len(user.summary) <= 80
        assert user.summary.endswith("…")

    def test_provider_metadata(self):
        prov = next(n for n in _full_trace().nodes if n.id == "provider")
        assert prov.metadata["connection_id"] == "google"
        assert prov.metadata["model"] == "gemini-2.5-flash"
        assert prov.metadata["provider"] == "GeminiProvider"

    def test_generation_error_marks_llm_call_error(self):
        trace = _full_trace(generation_error="provider exploded")
        st = {n.id: n.status for n in trace.nodes}
        # 失敗箇所が error、以降は skipped で「どこで止まったか」が分かる
        assert st["llm_call"] == "error"
        assert st["memory_write"] == "skipped"
        assert st["response"] == "skipped"
        assert st["state_update"] == "skipped"
        # 失敗より前のノードは通常どおり
        assert st["gatekeeper"] == "active"
        assert st["context_assembly"] == "active"
        assert st["provider"] == "active"

    def test_generation_error_metadata_and_summary(self):
        trace = _full_trace(generation_error="boom")
        llm = next(n for n in trace.nodes if n.id == "llm_call")
        assert llm.metadata["error"] == "boom"
        assert "boom" in llm.summary
        resp = next(n for n in trace.nodes if n.id == "response")
        assert "応答なし" in resp.summary

    def test_generation_error_downstream_edges_skipped(self):
        d = _full_trace(generation_error="boom").to_json_dict()
        edges = {(e["from"], e["to"]): e["status"] for e in d["edges"]}
        # provider -> llm_call は到達したので active、その先は skipped
        assert edges[("provider", "llm_call")] == "active"
        assert edges[("llm_call", "memory_write")] == "skipped"
        assert edges[("memory_write", "response")] == "skipped"
        assert edges[("llm_call", "state_update")] == "skipped"


# ===================================================================
# render_mermaid
# ===================================================================


class TestRenderMermaid:
    def test_header_and_nodes(self):
        m = render_mermaid(_full_trace())
        assert m.startswith("flowchart TD")
        assert 'gatekeeper["Gatekeeper' in m
        assert "user_message --> gatekeeper" in m

    def test_direction_override(self):
        assert render_mermaid(_full_trace(), direction="LR").startswith("flowchart LR")

    def test_skipped_edge_style(self):
        trace = _full_trace(
            gk_result={"tier": "mid", "need": None, "state_delta": {}},
            memory_blocks={"short_term": [1]},
        )
        m = render_mermaid(trace)
        assert "-. skipped .->" in m

    def test_classdef_only_for_used_statuses(self):
        m = render_mermaid(_full_trace())  # all active
        assert "classDef active" in m
        assert "classDef skipped" not in m
        assert "classDef fallback" not in m

    def test_class_assignment_groups_ids(self):
        trace = _full_trace(gk_error="x")
        m = render_mermaid(trace)
        assert "class gatekeeper fallback;" in m

    def test_sanitize_replaces_breaking_chars(self):
        assert _sanitize('a "b" [c]') == "a 'b' (c)"
        assert _sanitize("line1\nline2") == "line1 line2"

    def test_label_with_quotes_does_not_break_syntax(self):
        trace = _full_trace(user_input='say "hello" [now]')
        m = render_mermaid(trace)
        user_line = next(
            ln for ln in m.splitlines() if ln.strip().startswith("user_message[")
        )
        # ラベルを囲む " は 2 個のみ（内部の " はサニタイズ済み）
        assert user_line.count('"') == 2

    def test_error_status_rendered(self):
        m = render_mermaid(_full_trace(generation_error="boom"))
        assert "classDef error" in m
        assert "class llm_call error;" in m


# ===================================================================
# _save_trace
# ===================================================================


@pytest.fixture
def instance_dir(tmp_path):
    d = tmp_path / "TestInstance"
    d.mkdir()
    return d


class TestSaveTrace:
    def test_creates_traces_dir_and_latest(self, instance_dir):
        payload = _full_trace().to_json_dict()
        _save_trace(instance_dir, payload)

        latest = instance_dir / "traces" / "latest.json"
        assert latest.exists()
        loaded = json.loads(latest.read_text(encoding="utf-8"))
        assert loaded["trace_id"] == "turn_42"
        assert loaded["edges"][0]["from"] == "user_message"

    def test_history_rotation(self, instance_dir):
        for i in range(5):
            _save_trace(instance_dir, {"turn_id": i}, max_history=3)
            time.sleep(0.01)
        files = list((instance_dir / "traces" / "history").glob("*.json"))
        assert len(files) == 3
        turns = sorted(json.loads(f.read_text())["turn_id"] for f in files)
        assert turns == [2, 3, 4]

    def test_save_failure_does_not_raise(self, instance_dir, monkeypatch):
        from pathlib import Path

        def fail_mkdir(*args, **kwargs):
            raise PermissionError("denied")

        monkeypatch.setattr(Path, "mkdir", fail_mkdir)
        _save_trace(instance_dir, {"x": 1})  # should not raise
