"""
routers/instances.py
────────────────────
`/api/v1` の typed instance list / message history endpoint（Phase 0 PR2-A）。

Streamlit / 新 frontend が `INSTANCES_DIR.iterdir()` や `ButlyMemory` を
直接読む構造（frontend_migration_plan.ja.md §3.3）を撤去するための置換 API。
transport adapter と正規化だけを行い、保存形式の変更はしない
（正式な Store / Repository 化は記憶ストア正規化計画の責務）。

実装ノート:
- `GET /instances` は読み取り専用に保つため ButlyMemory を使わない。
  ButlyMemory.__init__ は不足 subdirectory / system_instruction.txt を
  書き込む side effect があるため、一覧列挙には不適。
- `GET /instances/{name}/messages` は既存の
  ButlyMemory.load_recent_sessions() / get_last_interaction_time() に委譲する
  （legacy `/history/{name}` と同じ読み出し経路）。
- Phase 2 以降の turn meta にある添付要約と引用元は履歴 DTO へ復元する。
  base64 や provider raw object は保存・返却しない。旧 turn は空配列のまま。
- 保存 timestamp は naive local time（datetime.now().isoformat()）なので、
  `.astimezone()` で local tz を付与して tz-aware ISO 8601 として返す。
"""

import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, Request

from butly_api.context import ApiContext
from butly_api.errors import ApiException
from butly_api.schemas.chat import Message, MessagePage
from butly_api.schemas.common import ApiError
from butly_api.schemas.instances import InstanceListResponse, InstanceSummary
from butly_api.schemas.trace import TraceGraphResponse
from butly_api.version import API_V1_PREFIX

logger = logging.getLogger(__name__)

router = APIRouter(prefix=API_V1_PREFIX, tags=["instances"])

DATABASE_FILENAME = "butly_memory.db"

# updated_at 算出対象（ButlyMemory と同じ会話記憶の置き場所）
_MEMORY_JSON_DIRS = [
    "short_term_json",
    "memory_archive/1_integrated",
    "memory_archive/2_knowledgeized",
]

_NOT_READY = {503: {"model": ApiError, "description": "Backend is not ready"}}


def _require_instances_dir(request: Request) -> Path:
    ctx: Optional[ApiContext] = getattr(request.app.state, "api_context", None)
    if ctx is None or ctx.instances_dir is None:
        raise ApiException(
            503,
            "backend_not_ready",
            "Backend runtime context is not initialized.",
        )
    return ctx.instances_dir


# ===================================================================
# GET /api/v1/instances
# ===================================================================


def _load_instance_config(instance_dir: Path) -> Dict[str, Any]:
    config_path = instance_dir / "config.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        # 破損 config で一覧全体を落とさない。path は log のみに残す
        logger.warning("[api] failed to read instance config (%s): %s", instance_dir.name, e)
        return {}


def _latest_memory_timestamp(instance_dir: Path) -> Optional[datetime]:
    """会話記憶 JSON の最新 mtime を tz-aware で返す。読めなければ None。"""
    latest: Optional[float] = None
    for rel in _MEMORY_JSON_DIRS:
        d = instance_dir / rel
        if not d.is_dir():
            continue
        try:
            for f in d.glob("**/*.json"):
                mtime = os.path.getmtime(f)
                if latest is None or mtime > latest:
                    latest = mtime
        except OSError as e:
            logger.warning(
                "[api] failed to scan memory files (%s/%s): %s",
                instance_dir.name,
                rel,
                e,
            )
    if latest is None:
        return None
    return datetime.fromtimestamp(latest).astimezone()


def _summarize_instance(instance_dir: Path) -> InstanceSummary:
    config = _load_instance_config(instance_dir)
    profile = config.get("agent_profile") or config.get("agent") or {}
    if not isinstance(profile, dict):
        profile = {}
    ai_name = profile.get("ai_name") or None
    locale = profile.get("locale") or None
    return InstanceSummary(
        name=instance_dir.name,
        ai_name=ai_name if isinstance(ai_name, str) else None,
        locale=locale if isinstance(locale, str) else None,
        updated_at=_latest_memory_timestamp(instance_dir),
        has_database=(instance_dir / DATABASE_FILENAME).is_file(),
    )


@router.get(
    "/instances",
    operation_id="list_instances",
    response_model=InstanceListResponse,
    summary="List AI instances",
    description=(
        "instance の typed 一覧を返す。legacy `GET /instances`（文字列配列）の"
        "versioned 置換。破損した instance config があってもその instance は"
        "fallback 値で返し、一覧全体は失敗させない。"
    ),
    responses=_NOT_READY,
)
def list_instances(request: Request) -> InstanceListResponse:
    instances_dir = _require_instances_dir(request)
    if not instances_dir.is_dir():
        return InstanceListResponse(items=[])

    items: List[InstanceSummary] = []
    for p in sorted(instances_dir.iterdir()):
        if not p.is_dir() or p.name.startswith("."):
            continue
        items.append(_summarize_instance(p))
    return InstanceListResponse(items=items)


# ===================================================================
# GET /api/v1/instances/{name}/messages
# ===================================================================


def _resolve_instance_dir(instances_dir: Path, name: str) -> Path:
    # path traversal（".."、hidden、separator 混入）は存在有無を隠して 404
    if name != Path(name).name or name.startswith("."):
        raise ApiException(
            404, "instance_not_found", f"Instance '{name}' was not found."
        )
    instance_dir = instances_dir / name
    if not instance_dir.is_dir():
        raise ApiException(
            404, "instance_not_found", f"Instance '{name}' was not found."
        )
    return instance_dir


def _to_aware_datetime(value: Any) -> Optional[datetime]:
    """保存形式の naive local timestamp を tz-aware に正規化する。"""
    if isinstance(value, datetime):
        return value.astimezone()
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone()
    except ValueError:
        return None


def _extract_text(raw_msg: Dict[str, Any]) -> str:
    parts = raw_msg.get("parts") or []
    content = parts[0] if parts else ""
    if isinstance(content, dict):
        content = content.get("text", "")
    return content if isinstance(content, str) else str(content)


def _normalize_messages(raw_messages: List[Dict[str, Any]]) -> List[Message]:
    """load_recent_sessions() の `{"role", "parts", "timestamp"?}` を Message へ。

    message ID: 現行保存形式に ID がないため、(timestamp, role, text, 同一内容の
    出現回数) の hash から deterministic に生成する。同じデータからは常に同じ
    ID になるが、正式な安定 ID は記憶ストア正規化計画で保存形式に導入する。
    """
    items: List[Message] = []
    seen: Dict[str, int] = {}
    for raw in raw_messages:
        if not isinstance(raw, dict):
            continue
        role_raw = raw.get("role")
        if role_raw == "user":
            role = "user"
        elif role_raw in ("model", "assistant"):
            role = "assistant"
        else:
            continue

        text = _extract_text(raw)
        ts = raw.get("timestamp")
        key = f"{ts or ''}|{role}|{text}"
        occurrence = seen.get(key, 0)
        seen[key] = occurrence + 1
        digest = hashlib.sha1(f"{key}|{occurrence}".encode("utf-8")).hexdigest()[:16]

        meta = raw.get("meta")
        meta = meta if isinstance(meta, dict) else {}
        raw_attachments = meta.get("attachments")
        attachments = []
        if role == "user" and isinstance(raw_attachments, list):
            from butly_core.chat.types import ALLOWED_IMAGE_MIMES, MAX_ATTACHMENTS

            for attachment in raw_attachments[:MAX_ATTACHMENTS]:
                if not isinstance(attachment, dict):
                    continue
                if attachment.get("kind", "image") != "image":
                    continue
                mime_type = attachment.get("mime_type")
                if mime_type not in ALLOWED_IMAGE_MIMES:
                    continue
                try:
                    attachments.append(
                        {
                            "kind": "image",
                            "mime_type": mime_type,
                            "name": (
                                attachment.get("name")[:255]
                                if isinstance(attachment.get("name"), str)
                                else None
                            ),
                            "size_bytes": (
                                attachment.get("size_bytes")
                                if isinstance(attachment.get("size_bytes"), int)
                                and not isinstance(
                                    attachment.get("size_bytes"), bool
                                )
                                and attachment.get("size_bytes") >= 0
                                else None
                            ),
                        }
                    )
                except (TypeError, ValueError):
                    continue
        raw_sources = meta.get("sources")
        sources = []
        if role == "assistant" and isinstance(raw_sources, list):
            for source in raw_sources[:50]:
                if not isinstance(source, dict):
                    continue
                sources.append(
                    {
                        "title": (
                            source.get("title")[:500]
                            if isinstance(source.get("title"), str)
                            else ""
                        ),
                        "url": (
                            source.get("url")[:4096]
                            if isinstance(source.get("url"), str)
                            else ""
                        ),
                    }
                )

        items.append(
            Message(
                id=f"msg_{digest}",
                role=role,
                text=text,
                created_at=_to_aware_datetime(ts),
                # 旧 turn の meta 欠落は空配列として後方互換に倒す。
                attachments=attachments,
                sources=sources,
                status="completed",
            )
        )
    return items


@router.get(
    "/instances/{name}/messages",
    operation_id="list_instance_messages",
    response_model=MessagePage,
    summary="Get chat message history",
    description=(
        "instance の会話履歴を時系列（古い順）で返す。legacy `GET /history/{name}` の"
        "versioned 置換で、timestamp と last_interaction_at を追加する。"
        "`before` cursor は予約のみで現時点では無視される（next_cursor も常に null）。"
    ),
    responses={
        404: {"model": ApiError, "description": "Instance not found"},
        # FastAPI default の HTTPValidationError ではなく実際の envelope を明示
        422: {"model": ApiError, "description": "Validation error"},
        **_NOT_READY,
    },
)
def list_instance_messages(
    name: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200, description="返す最大 message 数"),
    before: Optional[str] = Query(
        None,
        description="pagination cursor（予約。現行 storage では未実装のため無視される）",
    ),
) -> MessagePage:
    instances_dir = _require_instances_dir(request)
    _resolve_instance_dir(instances_dir, name)

    # ButlyMemory は base_dir/butly_core/instances/<name> という layout 契約を持つ。
    # Store/Repository 化（記憶ストア正規化計画）までこの結合を許容する。
    from butly_core.core.memory import ButlyMemory

    base_dir = instances_dir.parent.parent
    memory = ButlyMemory(base_dir, instance_name=name)

    result = memory.load_recent_sessions(limit=limit)
    raw_messages = result[0] if isinstance(result, tuple) else result
    items = _normalize_messages(raw_messages)[-limit:]

    # next_cursor は常に None: 現行 storage（mtime 順の turn JSON 群）では
    # 安定した cursor 位置を表現できない。cursor pagination は
    # 記憶ストア正規化後に実装する。
    return MessagePage(
        items=items,
        next_cursor=None,
        last_interaction_at=_to_aware_datetime(memory.get_last_interaction_time()),
    )


# =====================================================================
# Trace Graph（issue #51）
# =====================================================================

# Mermaid のノードラベルに載せる summary の上限。応答本文がそのまま入ると
# グラフが読めなくなるうえ、UI へ raw な生成文を流すことにもなる。
TRACE_SUMMARY_MAX_CHARS = 80

TRACE_LATEST_FILENAME = "latest.json"


def _require_developer_mode(request: Request) -> None:
    """trace は debug 系の情報なので、chat debug と同じ gate を通す。"""
    ctx: Optional[ApiContext] = getattr(request.app.state, "api_context", None)
    if ctx is None or not ctx.developer_mode:
        raise ApiException(
            403,
            "debug_not_available",
            "Trace graphs are available only in developer mode.",
        )


def _trim_trace_summaries(trace: Any) -> Any:
    """表示用に summary を丸める。metadata は Mermaid へ出ないため触らない。"""
    for node in trace.nodes:
        summary = node.summary or ""
        if len(summary) > TRACE_SUMMARY_MAX_CHARS:
            node.summary = summary[:TRACE_SUMMARY_MAX_CHARS].rstrip() + "…"
    return trace


@router.get(
    "/instances/{name}/trace",
    operation_id="get_instance_trace",
    response_model=TraceGraphResponse,
    summary="Get the latest trace graph",
    description=(
        "直近 1 ターンの回答生成フローを Mermaid flowchart として返す（issue #51）。"
        "Gatekeeper / RAG / Context Assembly / Provider / LLM / Memory Write を "
        "active・skipped・fallback・error で塗り分ける。developer mode 専用で、"
        "TraceNode の metadata（原文クエリ・検索候補など）は返さない。"
    ),
    responses={
        403: {"model": ApiError, "description": "Developer mode is disabled"},
        404: {"model": ApiError, "description": "Instance or trace not found"},
        **_NOT_READY,
    },
)
def get_instance_trace(name: str, request: Request) -> TraceGraphResponse:
    _require_developer_mode(request)
    instances_dir = _require_instances_dir(request)
    instance_dir = _resolve_instance_dir(instances_dir, name)

    trace_path = instance_dir / "traces" / TRACE_LATEST_FILENAME
    if not trace_path.is_file():
        raise ApiException(
            404,
            "trace_not_found",
            f"No trace has been recorded for instance '{name}' yet.",
        )
    try:
        raw = json.loads(trace_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("failed to read trace for %s: %s", name, exc)
        raise ApiException(
            404,
            "trace_not_found",
            f"The stored trace for instance '{name}' could not be read.",
        ) from exc

    from butly_core.trace.mermaid import render_mermaid
    from butly_core.trace.types import TraceGraph

    try:
        trace = TraceGraph.model_validate(raw)
    except Exception as exc:  # schema drift は 404 として扱い、詳細は出さない
        logger.warning("stored trace for %s did not validate: %s", name, exc)
        raise ApiException(
            404,
            "trace_not_found",
            f"The stored trace for instance '{name}' is not readable.",
        ) from exc

    node_counts: Dict[str, int] = {}
    for node in trace.nodes:
        node_counts[node.status] = node_counts.get(node.status, 0) + 1

    return TraceGraphResponse(
        trace_id=trace.trace_id,
        turn_id=trace.turn_id,
        source=trace.source,
        created_at=trace.created_at,
        mermaid=render_mermaid(_trim_trace_summaries(trace)),
        node_counts=node_counts,
    )
