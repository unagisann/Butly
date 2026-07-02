"""
filters.py
----------
TraceGraph の表示フィルタ (issue #51)。

trace.json には常に全ノードを保存し、表示側 (Mermaid / 将来の Debug UI) で
このフィルタを適用する。収集を設定で止めると「問題が起きたターンに限って
データがない」状態になるため、設定 (``SYSTEM_CONFIG["trace"]``) の
``detail`` / ``hidden_nodes`` は保存ではなく表示に効かせる。
"""

from typing import Sequence

from butly_core.trace.types import TraceGraph, TraceNode


def _is_hidden(node: TraceNode, detail: str, hidden: set) -> bool:
    if detail == "summary" and node.metadata.get("aux"):
        return True
    if node.id in hidden:
        return True
    purpose = node.metadata.get("purpose")
    return purpose in hidden if purpose else False


def filter_trace(
    trace: TraceGraph,
    *,
    detail: str = "full",
    hidden_nodes: Sequence[str] = (),
) -> TraceGraph:
    """表示用にノードを絞った TraceGraph のコピーを返す。

    Parameters
    ----------
    detail : str
        "full" (既定) は全ノード。"summary" は補助 LLM ノード
        (``metadata.aux=True``) を除いたメインフローのみ。
    hidden_nodes : Sequence[str]
        非表示にするノードの purpose (例: "embedding", "keyword_extract")
        または node id (例: "web_search")。embedding のように同一 purpose が
        複数ノードになるものは purpose 指定でまとめて隠せる。

    非表示ノードに接続するエッジも取り除く。メインフローの中間ノードを隠すと
    グラフが分断され得るため、主用途は補助 LLM ノード (葉) の非表示。
    """
    hidden = set(hidden_nodes or ())
    if not hidden and detail == "full":
        return trace

    kept_nodes = [n for n in trace.nodes if not _is_hidden(n, detail, hidden)]
    kept_ids = {n.id for n in kept_nodes}
    kept_edges = [
        e for e in trace.edges if e.source in kept_ids and e.target in kept_ids
    ]
    return trace.model_copy(update={"nodes": kept_nodes, "edges": kept_edges})
