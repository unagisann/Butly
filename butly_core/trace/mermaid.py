"""
mermaid.py
----------
TraceGraph を Mermaid flowchart 文字列へ変換する。

フロントエンド非依存の軽量レンダラー。出力された Mermaid テキストは Debug 画面や
ドキュメント、外部ツールでそのまま描画できる。active / skipped / fallback / error /
warning を classDef で塗り分け、skipped/fallback/error のエッジは線種で区別する。
"""

from typing import Dict, List

from butly_core.trace.types import TraceEdge, TraceGraph, TraceNode, TraceStatus

# status ごとの classDef（描画スタイル）
_CLASS_DEFS: Dict[str, str] = {
    "active": "fill:#d5f5e3,stroke:#27ae60,color:#111;",
    "skipped": "fill:#eeeeee,stroke:#999999,color:#666666,stroke-dasharray: 5 5;",
    "fallback": "fill:#fdebd0,stroke:#e67e22,color:#111;",
    "error": "fill:#f9d6d5,stroke:#c0392b,color:#111;",
    "warning": "fill:#fcf3cf,stroke:#f39c12,color:#111;",
}

_STATUS_ORDER: List[TraceStatus] = [
    "active",
    "skipped",
    "fallback",
    "error",
    "warning",
]


def _sanitize(text: str) -> str:
    """Mermaid のノードラベルに安全に埋め込めるよう文字列を整形する。

    ダブルクォート・改行・角括弧など、`["..."]` 構文を壊す文字を置換する。
    """
    if not text:
        return ""
    out = " ".join(text.split())
    replacements = {
        '"': "'",
        "[": "(",
        "]": ")",
        "{": "(",
        "}": ")",
        "|": "/",
        "`": "'",
        "<": "(",
        ">": ")",
    }
    for src, dst in replacements.items():
        out = out.replace(src, dst)
    return out


def _node_line(node: TraceNode) -> str:
    label = _sanitize(node.label)
    summary = _sanitize(node.summary or "")
    text = f"{label}<br/>{summary}" if summary else label
    return f'    {node.id}["{text}"]'


def _edge_line(edge: TraceEdge) -> str:
    if edge.status == "skipped":
        connector = "-. skipped .->"
    elif edge.status == "fallback":
        connector = "-. fallback .->"
    elif edge.status == "error":
        connector = "== error ==>"
    else:
        connector = "-->"
    return f"    {edge.source} {connector} {edge.target}"


def render_mermaid(trace: TraceGraph, *, direction: str = "TD") -> str:
    """TraceGraph を Mermaid flowchart 文字列へ変換する。"""
    lines: List[str] = [f"flowchart {direction}"]

    for node in trace.nodes:
        lines.append(_node_line(node))

    lines.append("")
    for edge in trace.edges:
        lines.append(_edge_line(edge))

    lines.append("")
    used_statuses = {node.status for node in trace.nodes}
    for status in _STATUS_ORDER:
        if status in used_statuses:
            lines.append(f"    classDef {status} {_CLASS_DEFS[status]}")

    by_status: Dict[str, List[str]] = {}
    for node in trace.nodes:
        by_status.setdefault(node.status, []).append(node.id)
    for status in _STATUS_ORDER:
        ids = by_status.get(status)
        if ids:
            lines.append(f"    class {','.join(ids)} {status};")

    return "\n".join(lines)
