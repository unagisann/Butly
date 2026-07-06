"""
butly_core.trace
----------------
Trace Graph: 1 回答の生成フローをノード + エッジのグラフとして記録・可視化する
デバッグ機能（issue #51）。

公開 API:
  - ``build_chat_trace(...)``  : ChatService の実行事実から TraceGraph を構築
  - ``render_mermaid(trace)``  : TraceGraph を Mermaid flowchart 文字列へ変換
  - ``filter_trace(trace)``    : detail / hidden_nodes による表示フィルタ
  - collector (``start_collection`` / ``record_llm_call`` 等):
    ターン中の LLM 呼び出しを ContextVar 経由で収集
  - ``TraceGraph`` / ``TraceNode`` / ``TraceEdge`` : DTO
"""

from butly_core.trace.builder import build_chat_trace
from butly_core.trace.collector import (
    get_collected,
    is_collecting,
    record_llm_call,
    reset_collection,
    start_collection,
)
from butly_core.trace.filters import filter_trace
from butly_core.trace.mermaid import render_mermaid
from butly_core.trace.types import (
    TRACE_SCHEMA_VERSION,
    TraceEdge,
    TraceGraph,
    TraceNode,
    summarize_text,
)

__all__ = [
    "build_chat_trace",
    "render_mermaid",
    "filter_trace",
    "start_collection",
    "reset_collection",
    "record_llm_call",
    "get_collected",
    "is_collecting",
    "TraceGraph",
    "TraceNode",
    "TraceEdge",
    "TRACE_SCHEMA_VERSION",
    "summarize_text",
]
