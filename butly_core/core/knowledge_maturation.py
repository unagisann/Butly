"""Stage 3: Knowledge Maturation orchestration.

ButlySleeptime から呼ばれる。
責務:
  - レビュー対象カードの収集 (§7)
  - LLM への入力構築 / 出力パースと enum validation (§4, §8, §9)
  - 既存 node への link / 新規 candidate node 作成 (§8, §9)
  - confidence と status の更新 / supersede 反映 (§10)
  - proposal 生成 (§11)
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from butly_core.core.json_extract import extract_json_str
from butly_core.core.memory_nodes import (
    MemoryNodeRepository,
    SOURCE_RELATIONS,
    normalize_kind,
)


# ----------------------------------------------------------
# Review card collection (§7)
# ----------------------------------------------------------

def collect_review_cards(
    db_path: str,
    *,
    window_days: int,
    max_cards: int,
    min_usage_count: int,
) -> list[dict]:
    """Stage 3 のレビュー対象カードを返す。

    §7 で挙げられている優先度を、既存資産だけで近似する:
      - usage_count >= min_usage_count を最優先
      - usage_count desc, last_counted_at desc, updated_at desc
      - access_logs.accessed_at が window 内のカードも対象に加える
    """
    if max_cards <= 0:
        return []

    threshold_date = datetime.now().date()
    if window_days > 0:
        from datetime import timedelta

        threshold_date = threshold_date - timedelta(days=window_days)
    threshold_str = threshold_date.isoformat()

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    try:
        # usage_count を持つカードを優先
        primary_sql = """
            SELECT id, title, summary, episode, tags, category, type,
                   usage_count, last_counted_at, updated_at
            FROM knowledge_cards
            WHERE COALESCE(is_archived, 0) = 0
              AND COALESCE(usage_count, 0) >= ?
            ORDER BY usage_count DESC,
                     COALESCE(last_counted_at, '') DESC,
                     COALESCE(updated_at, '') DESC
            LIMIT ?
        """
        primary = [dict(r) for r in conn.execute(primary_sql, (min_usage_count, max_cards))]
        seen = {c["id"] for c in primary}

        remaining = max_cards - len(primary)
        if remaining > 0 and window_days > 0:
            access_sql = """
                SELECT c.id, c.title, c.summary, c.episode, c.tags, c.category, c.type,
                       c.usage_count, c.last_counted_at, c.updated_at,
                       MAX(a.accessed_at) AS last_accessed
                FROM knowledge_cards c
                JOIN access_logs a ON a.card_id = c.id
                WHERE COALESCE(c.is_archived, 0) = 0
                  AND a.accessed_at >= ?
                GROUP BY c.id
                ORDER BY last_accessed DESC,
                         COALESCE(c.updated_at, '') DESC
                LIMIT ?
            """
            for row in conn.execute(access_sql, (threshold_str, remaining)):
                d = dict(row)
                if d["id"] in seen:
                    continue
                primary.append(d)
                seen.add(d["id"])

        return primary
    finally:
        conn.close()


# ----------------------------------------------------------
# LLM IO (§8 / §9)
# ----------------------------------------------------------

def build_review_prompt(
    *,
    loader,
    agent_name: str,
    system_instruction: str,
    key_memory: str,
    existing_nodes: list[dict],
    review_cards: list[dict],
) -> str:
    def _trim(s: str, n: int) -> str:
        return s if len(s) <= n else s[:n] + "..."

    def _node_view(n: dict) -> dict:
        return {
            "id": n.get("id"),
            "kind": n.get("kind"),
            "status": n.get("status"),
            "subject": n.get("subject"),
            "topic": n.get("topic"),
            "statement": _trim(n.get("statement") or "", 400),
            "confidence": round(float(n.get("confidence") or 0.0), 3),
        }

    def _card_view(c: dict) -> dict:
        return {
            "id": c.get("id"),
            "title": _trim(c.get("title") or "", 120),
            "tags": _trim(c.get("tags") or "", 200),
            "category": c.get("category"),
            "summary": _trim(c.get("summary") or "", 600),
            "episode": _trim(c.get("episode") or "", 400),
            "usage_count": c.get("usage_count") or 0,
        }

    existing_json = json.dumps(
        [_node_view(n) for n in existing_nodes], ensure_ascii=False, indent=2
    )
    cards_json = json.dumps(
        [_card_view(c) for c in review_cards], ensure_ascii=False, indent=2
    )

    return loader.get(
        "stage3_node_review",
        agent_name=agent_name,
        system_instruction=_trim(system_instruction, 1500),
        key_memory=_trim(key_memory, 1500),
        existing_nodes_json=existing_json,
        review_cards_json=cards_json,
    )


def parse_llm_output(raw: str) -> dict:
    """LLM 応答を JSON に解釈する。失敗時は空構造を返す。"""
    if not raw:
        return {"link_existing": [], "new_nodes": []}
    try:
        data = json.loads(extract_json_str(raw))
    except Exception:
        return {"link_existing": [], "new_nodes": []}
    if not isinstance(data, dict):
        return {"link_existing": [], "new_nodes": []}
    data.setdefault("link_existing", [])
    data.setdefault("new_nodes", [])
    if not isinstance(data["link_existing"], list):
        data["link_existing"] = []
    if not isinstance(data["new_nodes"], list):
        data["new_nodes"] = []
    return data


# ----------------------------------------------------------
# Application: link / create / supersede (§8, §9, §10)
# ----------------------------------------------------------

def _coerce_confidence(value: Any, default: float = 0.0) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


def apply_link_existing(
    *,
    repo: MemoryNodeRepository,
    entries: list[dict],
    valid_node_ids: set[str],
    valid_card_ids: set[str],
    run_id: str,
) -> tuple[int, list[str]]:
    """既存 node への source link を適用する。

    Returns: (linked_count, uncertain_node_ids)
        uncertain_node_ids は contradicts が多い node の id 候補。
    """
    linked = 0
    contradict_count: dict[str, int] = {}
    support_count: dict[str, int] = {}

    for e in entries:
        if not isinstance(e, dict):
            continue
        node_id = e.get("node_id")
        card_id = e.get("card_id")
        if not node_id or not card_id:
            continue
        if node_id not in valid_node_ids:
            continue
        if card_id not in valid_card_ids:
            continue
        try:
            relation = e.get("relation", "supports")
            if relation not in SOURCE_RELATIONS:
                continue
            confidence = _coerce_confidence(e.get("confidence"), 0.5)
            note = e.get("note")
            repo.upsert_source(
                node_id=node_id,
                card_id=card_id,
                relation=relation,
                confidence=confidence,
                note=note if isinstance(note, str) else None,
                created_by_run_id=run_id,
            )
            linked += 1
            if relation == "supports":
                support_count[node_id] = support_count.get(node_id, 0) + 1
            elif relation == "contradicts":
                contradict_count[node_id] = contradict_count.get(node_id, 0) + 1
        except Exception as exc:  # pragma: no cover - 防御的ログのみ
            print(f"[Stage3] link_existing skip ({node_id} / {card_id}): {exc}")

    # uncertain 候補: contradicts が supports と同等以上の node
    uncertain = [
        nid
        for nid, cc in contradict_count.items()
        if cc >= support_count.get(nid, 0)
    ]
    return linked, uncertain


def apply_new_nodes(
    *,
    repo: MemoryNodeRepository,
    entries: list[dict],
    valid_card_ids: set[str],
    run_id: str,
    candidate_threshold: float,
    active_threshold: float,
) -> tuple[int, int]:
    """新規 candidate node を作成し、supersedes 指定があれば旧 node を superseded 化。

    Returns: (created_count, superseded_count)
    """
    created = 0
    superseded = 0

    for e in entries:
        if not isinstance(e, dict):
            continue
        statement = e.get("statement")
        if not statement or not isinstance(statement, str):
            continue
        confidence = _coerce_confidence(e.get("confidence"), 0.0)
        if confidence < candidate_threshold:
            # §15: confidence が低い candidate は作らない
            continue

        # confidence の高さに応じて初期 status を判定
        if confidence >= active_threshold:
            status = "active"
        else:
            status = "candidate"

        kind = normalize_kind(e.get("kind"))
        source_ids_raw = e.get("source_card_ids") or []
        source_ids = [s for s in source_ids_raw if s in valid_card_ids]

        try:
            new_id = repo.create_node(
                kind=kind,
                statement=statement.strip(),
                subject=e.get("subject") if isinstance(e.get("subject"), str) else None,
                topic=e.get("topic") if isinstance(e.get("topic"), str) else None,
                confidence=confidence,
                status=status,
                metadata={"created_via": "stage3_node_review"},
                created_by_run_id=run_id,
            )
            created += 1
        except ValueError as ve:
            print(f"[Stage3] new_nodes validation skip: {ve}")
            continue

        for cid in source_ids:
            try:
                repo.upsert_source(
                    node_id=new_id,
                    card_id=cid,
                    relation="supports",
                    confidence=confidence,
                    created_by_run_id=run_id,
                )
            except Exception as exc:
                print(f"[Stage3] new_nodes source skip ({new_id} / {cid}): {exc}")

        old_id = e.get("supersedes_node_id")
        if old_id and isinstance(old_id, str) and old_id != new_id:
            if repo.supersede_node(
                old_node_id=old_id,
                new_node_id=new_id,
                updated_by_run_id=run_id,
            ):
                superseded += 1

    return created, superseded


def mark_uncertain_nodes(
    *,
    repo: MemoryNodeRepository,
    node_ids: list[str],
    run_id: str,
) -> int:
    """contradicts が優勢な node を `uncertain` に降格させる。"""
    n = 0
    for nid in node_ids:
        node = repo.get_node(nid)
        if not node:
            continue
        if node.get("status") in ("active", "candidate"):
            if repo.update_node(
                nid,
                status="uncertain",
                updated_by_run_id=run_id,
            ):
                n += 1
    return n


# ----------------------------------------------------------
# Promotion proposals (§11)
# ----------------------------------------------------------

def collect_promotion_proposals(
    *,
    repo: MemoryNodeRepository,
    confidence_threshold: float,
    min_sources: int,
) -> list[dict]:
    """§11 の昇格条件を全て満たす node を proposal として返す。

    条件:
      - status='active'
      - confidence >= confidence_threshold
      - `supports` ソースが min_sources 以上
      - サポートカードが複数日に分散している
    """
    active_nodes = repo.find_nodes(statuses=["active"], limit=200)
    proposals = []
    for n in active_nodes:
        if float(n.get("confidence") or 0.0) < confidence_threshold:
            continue
        support_n = repo.count_sources(n["id"], relation="supports")
        if support_n < min_sources:
            continue
        days = repo.distinct_support_days(n["id"])
        if days < 2:
            continue
        proposals.append(
            {
                "node_id": n["id"],
                "kind": n.get("kind"),
                "subject": n.get("subject"),
                "topic": n.get("topic"),
                "statement": n.get("statement"),
                "confidence": n.get("confidence"),
                "support_sources": support_n,
                "support_days": days,
                "status": "pending",
                "proposed_at": datetime.now().isoformat(),
            }
        )
    return proposals


def write_promotion_proposals_file(instance_path: Path, proposals: list[dict]) -> Path:
    """memory_node_proposals.json に書き出す。

    既存の Key Memory proposals (key_memory_proposals.json) とは別ファイル。
    自動反映は行わず、レビュー用の出力にとどめる (§11)。
    """
    out = instance_path / "memory_node_proposals.json"
    payload = {
        "generated_at": datetime.now().isoformat(),
        "proposals": proposals,
    }
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out
