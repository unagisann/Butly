"""
trace.py
────────
`GET /api/v1/instances/{name}/trace` の contract schema（issue #51）。

公開するのは **Mermaid 文字列と要約カウントだけ**で、TraceNode の
``metadata`` は返さない。metadata には Gatekeeper の原文クエリや検索候補など、
UI 用 DTO に載せない約束の情報（frontend_chat.ja.md「添付、引用、安全性」）が
入るため、描画に必要な最小限へ落とす。

Mermaid の生成は `butly_core.trace.mermaid.render_mermaid` を正本として再利用し、
API 側では表示用の要約長トリムだけを行う。
"""

from typing import Dict, Optional

from pydantic import BaseModel, Field


class TraceGraphResponse(BaseModel):
    """1 ターン分の回答生成フローを Mermaid flowchart として返す。"""

    trace_id: str = Field(description="trace の識別子（例: `turn_13`）")
    turn_id: Optional[int] = Field(
        default=None, description="session 内の turn 番号。未記録なら null"
    )
    source: str = Field(default="web", description="発生元（web / discord / line 等）")
    created_at: Optional[str] = Field(
        default=None, description="trace 生成時刻（保存時の ISO 8601 文字列）"
    )
    mermaid: str = Field(description="Mermaid flowchart 文字列")
    node_counts: Dict[str, int] = Field(
        default_factory=dict,
        description="status（active / skipped / fallback / error / warning）ごとのノード数",
    )
