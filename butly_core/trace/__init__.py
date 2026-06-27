"""
butly_core.trace
----------------
Trace Graph: 1 回答の生成フローをノード + エッジのグラフとして記録・可視化する
デバッグ機能（issue #51）。

公開 API:
  - ``build_chat_trace(...)``  : ChatService の実行事実から TraceGraph を構築
  - ``render_mermaid(trace)``  : TraceGraph を Mermaid flowchart 文字列へ変換
  - ``TraceGraph`` / ``TraceNode`` / ``TraceEdge`` : DTO
"""

from butly_core.trace.builder import build_chat_trace
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
    "TraceGraph",
    "TraceNode",
    "TraceEdge",
    "TRACE_SCHEMA_VERSION",
    "summarize_text",
]
