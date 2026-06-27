"""
types.py
--------
Trace Graph の DTO（Pydantic モデル）。

1 回答の生成フローを「ノード + エッジ」のグラフとして表現する。
回答生成の内部処理（Gatekeeper / Context Assembly / RAG / Provider / LLM /
Memory Write 等）がどの順番で実行され、どこがスキップ・分岐・フォールバック・
失敗したかをデバッグ目的で可視化するためのデータ構造。

設計方針:
  - フロントエンド非依存。trace.json として保存し、Mermaid 等へ変換する。
  - グラフ上は「概要」のみを持つ。長文会話本文をそのまま残さないよう、長文は
    ``summarize_text`` で短く切り詰める（あくまで長さの切り詰めであり、
    個人情報の除去・匿名化ではない点に注意）。
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

# ===================================================================
# 定数
# ===================================================================

# ノード種別（issue #51 のノード種別案に対応）
NodeType = Literal[
    "input",
    "loader",
    "decision",
    "retrieval",
    "tool",
    "context",
    "provider",
    "llm",
    "formatter",
    "memory",
    "housekeeper",
    "end",
]

# ノード/エッジの状態
#   active   : 実際に通った処理
#   skipped  : 候補だったが使われなかった処理
#   fallback : フォールバックとして使われた処理
#   error    : 失敗した処理
#   warning  : 成功したが注意が必要な処理
TraceStatus = Literal["active", "skipped", "fallback", "error", "warning"]

# trace.json スキーマのバージョン。破壊的変更時にインクリメントする。
TRACE_SCHEMA_VERSION = 1

# summary / ラベルに入れるテキストの既定の最大文字数
DEFAULT_SUMMARY_LIMIT = 80


# ===================================================================
# Pydantic モデル
# ===================================================================


class TraceNode(BaseModel):
    """処理フロー上の 1 ノード。"""

    id: str
    label: str
    type: NodeType
    status: TraceStatus = "active"
    summary: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TraceEdge(BaseModel):
    """ノード間の有向エッジ。

    issue の例では ``["from", "to"]`` の配列形式だが、status を持たせて
    skipped/fallback を線種で表現できるようにオブジェクト形式にする。
    JSON では ``from`` / ``to`` キーで出力する（``from`` は Python の予約語の
    ため、フィールド名は ``source`` / ``target``、alias で出力名を合わせる）。
    """

    model_config = {"populate_by_name": True}

    source: str = Field(alias="from")
    target: str = Field(alias="to")
    status: TraceStatus = "active"
    label: Optional[str] = None


class TraceGraph(BaseModel):
    """1 ターン分の回答生成フロー全体。"""

    schema_version: int = TRACE_SCHEMA_VERSION
    trace_id: str
    instance: str
    turn_id: Optional[int] = None
    source: str = "web"
    created_at: Optional[str] = None
    nodes: List[TraceNode] = Field(default_factory=list)
    edges: List[TraceEdge] = Field(default_factory=list)

    def to_json_dict(self) -> Dict[str, Any]:
        """trace.json 保存用の dict（edge は ``from`` / ``to`` で出力）。"""
        return self.model_dump(by_alias=True)


# ===================================================================
# ヘルパー
# ===================================================================


def summarize_text(text: Optional[str], limit: int = DEFAULT_SUMMARY_LIMIT) -> str:
    """ノード summary 用にテキストを 1 行へ切り詰める。

    改行・連続空白を 1 スペースに畳み、``limit`` 超過分は省略記号にする。
    長文会話本文をそのまま trace に残さないための「長さの切り詰め」であり、
    PII の除去・匿名化ではない（``limit`` 文字以内にも個人情報は残り得る）。
    """
    if not text:
        return ""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"
