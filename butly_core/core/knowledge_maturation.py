"""Stage 3: Knowledge Maturation orchestration.

ButlySleeptime から呼ばれる。
責務:
  - run preflight（非アーカイブ NULL hash の自己修復 backfill）
  - content hash 式レビューキューの FIFO 選択（§5.3）
  - 既存 node 文脈のスコープ化（§5.5）
  - LLM への入力構築 / 厳密 parse / 結果分類（§5.4）
  - 既存 node への link / 新規 candidate node 作成（参照整合検証つき）
  - proposal 生成（§8: 全 eligible node を pagination）
  - instance 単位 process lock

# LLM 結果分類と run/card 状態遷移の契約（Phase 0）

| 分類 | 条件 | run status | run_cards status | 版 stamp |
|---|---|---|---|---|
| ok | schema-valid JSON で操作あり | completed | applied | する |
| no_changes | schema-valid JSON で両配列が空 | completed | no_changes | する |
| truncated_response | provider が truncation 終了を報告 | failed | failed | しない |
| empty_response | 応答が空 | failed | failed | しない |
| parse_error | JSON/schema 違反 | failed | failed | しない |
| provider_error | provider 例外 | failed | failed | しない |
| (DB error) | 適用 transaction 中の例外 | failed (rollback 後) | failed | しない |
| changed_during_run | 適用時に content_hash 不一致 | failed | changed_during_run / abandoned | しない |
| (前 process 残骸) | lock 取得後に残る running run | abandoned | abandoned | しない |

- `reviewed_card_ids` の不一致は診断 (diagnostic) に留め、成功条件にしない。
- node 操作が入力外の card/node id を参照する場合は当該操作のみ拒否し、
  diagnostic へ記録する（§5.4-3）。
- 成功時は部分 stamp せず、投入した全カード版を stamp する（§5.4-4）。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from butly_core.core.card_content import (
    CardContentError,
    compute_content_hash,
    utc_now_stamp,
)
from butly_core.core.json_extract import extract_json_str
from butly_core.core.memory_nodes import (
    SOURCE_RELATIONS,
    normalize_kind,
)


# --- LLM 結果分類（§5.4-3） ---
OUTCOME_OK = "ok"
OUTCOME_NO_CHANGES = "no_changes"
OUTCOME_TRUNCATED = "truncated_response"
OUTCOME_EMPTY = "empty_response"
OUTCOME_PARSE_ERROR = "parse_error"
OUTCOME_PROVIDER_ERROR = "provider_error"

# retry → batch 分割の対象（§5.4-6）
RETRYABLE_OUTCOMES = {OUTCOME_TRUNCATED, OUTCOME_EMPTY, OUTCOME_PARSE_ERROR}


class MaturationPreflightError(RuntimeError):
    """preflight backfill で hash を生成できない行があった（§5.3: 黙って除外しない）。"""


class ReviewParseError(ValueError):
    """LLM 応答が schema-valid な JSON でない。"""


# ----------------------------------------------------------
# instance 単位 process lock（§5.4-1）
# ----------------------------------------------------------

STAGE3_LOCK_FILENAME = ".stage3.lock"


@contextmanager
def stage3_process_lock(instance_path: Path):
    """non-blocking な instance 単位 lock。yield 値は取得成否 (bool)。

    lock 保持中に process が死んでも OS が解放するため、stale lock は残らない。
    取得できた時点で live な並行 run は存在しないと断定できる。
    """
    lock_path = Path(instance_path) / STAGE3_LOCK_FILENAME
    fh = open(lock_path, "a+")
    acquired = False
    try:
        try:
            if os.name == "nt":  # pragma: no cover - Windows sidecar 用
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            acquired = False
        yield acquired
    finally:
        if acquired:
            try:
                if os.name == "nt":  # pragma: no cover
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh, fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


# ----------------------------------------------------------
# Queue: preflight / FIFO 選択 / backlog 観測（§5.3）
# ----------------------------------------------------------

_QUEUE_CONDITION = """
    COALESCE(is_archived, 0) = 0
    AND content_hash IS NOT NULL
    AND (last_matured_content_hash IS NULL
         OR last_matured_content_hash <> content_hash)
"""


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    return conn


def preflight_backfill_hashes(db_path: str, *, now_stamp: str | None = None) -> int:
    """非アーカイブかつ content_hash IS NULL のカードを自己修復 backfill する。

    hash を生成できない行があれば MaturationPreflightError で run を明示的に
    失敗させ、キューから静かに除外しない（§5.3）。
    Returns: backfill した行数。
    """
    stamp = now_stamp or utc_now_stamp()
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, title, summary, episode, tags, category, source_date
            FROM knowledge_cards
            WHERE COALESCE(is_archived, 0) = 0 AND content_hash IS NULL
            """
        ).fetchall()
        failures: list[str] = []
        updates: list[tuple[str, str, str]] = []
        for row in rows:
            try:
                updates.append((compute_content_hash(dict(row)), stamp, row["id"]))
            except CardContentError as exc:
                failures.append(f"{row['id']}: {exc}")
        if failures:
            raise MaturationPreflightError(
                "content_hash backfill failed for cards: " + "; ".join(failures)
            )
        if updates:
            conn.executemany(
                """
                UPDATE knowledge_cards
                SET content_hash = ?, maturation_queued_at = ?
                WHERE id = ? AND content_hash IS NULL
                """,
                updates,
            )
            conn.commit()
        return len(updates)
    finally:
        conn.close()


def select_queue_cards(
    db_path: str,
    *,
    batch_size: int,
    exclude_ids: tuple[str, ...] | list[str] = (),
) -> list[dict]:
    """レビューキューから最古の未処理版を FIFO で選択する（§5.3）。

    exclude_ids は bootstrap の invocation 内失敗隔離用（§6）。
    """
    if batch_size <= 0:
        return []
    exclude_ids = list(exclude_ids)
    exclude_sql = ""
    params: list[Any] = []
    if exclude_ids:
        placeholders = ",".join("?" for _ in exclude_ids)
        exclude_sql = f"AND id NOT IN ({placeholders})"
        params.extend(exclude_ids)
    params.append(int(batch_size))

    conn = _connect(db_path)
    try:
        rows = conn.execute(
            f"""
            SELECT id, title, summary, episode, tags, category, source_date,
                   usage_count, content_hash, maturation_queued_at
            FROM knowledge_cards
            WHERE {_QUEUE_CONDITION}
              {exclude_sql}
            ORDER BY maturation_queued_at ASC,
                     COALESCE(usage_count, 0) DESC,
                     id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_queue_backlog(db_path: str) -> dict:
    """backlog 件数と最古待ちの queue 時刻を返す（§5.3 の運用指標）。"""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS backlog, MIN(maturation_queued_at) AS oldest_queued_at
            FROM knowledge_cards
            WHERE {_QUEUE_CONDITION}
            """
        ).fetchone()
        return {
            "backlog": int(row["backlog"] or 0),
            "oldest_queued_at": row["oldest_queued_at"],
        }
    finally:
        conn.close()


# ----------------------------------------------------------
# 既存 node 文脈のスコープ化（§5.5）
# ----------------------------------------------------------

_CONTEXT_STATUSES = ("candidate", "active", "uncertain")


def _vocab_tokens(*texts: Any) -> set[str]:
    tokens: set[str] = set()
    for text in texts:
        if not text or not isinstance(text, str):
            continue
        lowered = text.lower()
        tokens.update(t for t in re.findall(r"[^\W_]{2,}", lowered))
        # カンマ区切りの tag はタグ全体でも照合する
        for part in lowered.split(","):
            part = part.strip()
            if len(part) >= 2:
                tokens.add(part)
    return tokens


def select_context_nodes(
    db_path: str,
    batch_cards: list[dict],
    *,
    limit: int = 200,
    scan_limit: int = 500,
) -> list[dict]:
    """prompt に載せる既存 node を §5.5 の優先順で合成する。

    1. バッチ card と memory_node_sources で既に結ばれた node
    2. カードの title/tags/category と node の topic/statement の語彙一致
    3. 残枠を直近更新 node で補う
    件数上限はここで、文字数上限は build_review_prompt 側で適用する。
    """
    card_ids = [c["id"] for c in batch_cards]
    conn = _connect(db_path)
    try:
        linked: list[dict] = []
        if card_ids:
            placeholders = ",".join("?" for _ in card_ids)
            status_ph = ",".join("?" for _ in _CONTEXT_STATUSES)
            rows = conn.execute(
                f"""
                SELECT DISTINCT n.*
                FROM memory_nodes n
                JOIN memory_node_sources s ON s.node_id = n.id
                WHERE s.card_id IN ({placeholders})
                  AND n.status IN ({status_ph})
                ORDER BY n.updated_at DESC
                """,
                [*card_ids, *_CONTEXT_STATUSES],
            ).fetchall()
            linked = [dict(r) for r in rows]

        status_ph = ",".join("?" for _ in _CONTEXT_STATUSES)
        recent_rows = conn.execute(
            f"""
            SELECT * FROM memory_nodes
            WHERE status IN ({status_ph})
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            [*_CONTEXT_STATUSES, int(scan_limit)],
        ).fetchall()
        recent = [dict(r) for r in recent_rows]
    finally:
        conn.close()

    card_vocab: set[str] = set()
    for c in batch_cards:
        card_vocab |= _vocab_tokens(c.get("title"), c.get("tags"), c.get("category"))

    vocab_matched = [
        n
        for n in recent
        if card_vocab & _vocab_tokens(n.get("topic"), n.get("statement"))
    ]

    ordered: list[dict] = []
    seen: set[str] = set()
    for group in (linked, vocab_matched, recent):
        for n in group:
            if n["id"] in seen:
                continue
            seen.add(n["id"])
            ordered.append(n)
            if len(ordered) >= limit:
                return ordered
    return ordered


# ----------------------------------------------------------
# LLM IO（§5.4）
# ----------------------------------------------------------

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
    # §5.2: usage/importance/type は Stage 3 prompt の意味内容ではないため
    # 含めない。source_date は出来事の時間的意味に関わるため含める。
    return {
        "id": c.get("id"),
        "title": _trim(c.get("title") or "", 120),
        "tags": _trim(c.get("tags") or "", 200),
        "category": c.get("category"),
        "source_date": c.get("source_date"),
        "summary": _trim(c.get("summary") or "", 600),
        "episode": _trim(c.get("episode") or "", 400),
    }


# prompt 文字数予算のうち、テンプレート本文＋persona（各 1500 字に trim 済み）
# のための予約分。nodes に割ける残量の見積もりに使う。
_PROMPT_TEMPLATE_RESERVE = 6000


def build_review_prompt(
    *,
    loader,
    agent_name: str,
    system_instruction: str,
    key_memory: str,
    existing_nodes: list[dict],
    review_cards: list[dict],
    prompt_max_chars: int = 0,
) -> str:
    """レビュー prompt を構築する。

    prompt_max_chars > 0 のとき、カード（バッチで固定）を優先し、
    existing_nodes を優先順の先頭から予算内に収まる分だけ載せる（§5.5）。
    """
    card_views = [_card_view(c) for c in review_cards]
    cards_json = json.dumps(card_views, ensure_ascii=False, indent=2)

    node_views: list[dict] = []
    if prompt_max_chars > 0:
        budget = prompt_max_chars - len(cards_json) - _PROMPT_TEMPLATE_RESERVE
        used = 0
        for n in existing_nodes:
            view = _node_view(n)
            cost = len(json.dumps(view, ensure_ascii=False)) + 8
            if used + cost > budget:
                break
            node_views.append(view)
            used += cost
    else:
        node_views = [_node_view(n) for n in existing_nodes]

    existing_json = json.dumps(node_views, ensure_ascii=False, indent=2)

    return loader.get(
        "stage3_node_review",
        agent_name=agent_name,
        system_instruction=_trim(system_instruction, 1500),
        key_memory=_trim(key_memory, 1500),
        existing_nodes_json=existing_json,
        review_cards_json=cards_json,
    )


def _coerce_number(value: Any) -> float | None:
    """int/float/数値文字列を float 化。それ以外は None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _clamp_confidence(value: float) -> float:
    return max(0.0, min(1.0, value))


def parse_review_output(raw: str) -> dict:
    """LLM 応答を厳密に parse する。schema 違反は ReviewParseError。

    正規化済みの {"link_existing": [...], "new_nodes": [...],
    "reviewed_card_ids": [...] | None} を返す。
    参照整合（入力外 id）はここでは検証しない（apply 側で操作単位に拒否）。
    """
    try:
        data = json.loads(extract_json_str(raw))
    except Exception as exc:
        raise ReviewParseError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ReviewParseError(f"root must be an object, got {type(data).__name__}")
    for key in ("link_existing", "new_nodes"):
        if key not in data:
            raise ReviewParseError(f"missing required key: {key}")
        if not isinstance(data[key], list):
            raise ReviewParseError(f"{key} must be an array")

    link_entries: list[dict] = []
    for i, e in enumerate(data["link_existing"]):
        if not isinstance(e, dict):
            raise ReviewParseError(f"link_existing[{i}] must be an object")
        node_id = e.get("node_id")
        card_id = e.get("card_id")
        if not isinstance(node_id, str) or not node_id.strip():
            raise ReviewParseError(f"link_existing[{i}].node_id must be a string")
        if not isinstance(card_id, str) or not card_id.strip():
            raise ReviewParseError(f"link_existing[{i}].card_id must be a string")
        relation = e.get("relation", "supports")
        if relation not in SOURCE_RELATIONS:
            raise ReviewParseError(
                f"link_existing[{i}].relation must be one of {sorted(SOURCE_RELATIONS)}"
            )
        confidence = _coerce_number(e.get("confidence", 0.5))
        if confidence is None:
            raise ReviewParseError(f"link_existing[{i}].confidence must be a number")
        note = e.get("note")
        link_entries.append(
            {
                "node_id": node_id.strip(),
                "card_id": card_id.strip(),
                "relation": relation,
                "confidence": _clamp_confidence(confidence),
                "note": note if isinstance(note, str) else None,
            }
        )

    new_entries: list[dict] = []
    for i, e in enumerate(data["new_nodes"]):
        if not isinstance(e, dict):
            raise ReviewParseError(f"new_nodes[{i}] must be an object")
        statement = e.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            raise ReviewParseError(f"new_nodes[{i}].statement must be a string")
        confidence = _coerce_number(e.get("confidence", 0.0))
        if confidence is None:
            raise ReviewParseError(f"new_nodes[{i}].confidence must be a number")
        source_ids = e.get("source_card_ids", [])
        if source_ids is None:
            source_ids = []
        if not isinstance(source_ids, list) or any(
            not isinstance(s, str) for s in source_ids
        ):
            raise ReviewParseError(
                f"new_nodes[{i}].source_card_ids must be an array of strings"
            )
        supersedes = e.get("supersedes_node_id")
        if supersedes is not None and not isinstance(supersedes, str):
            raise ReviewParseError(
                f"new_nodes[{i}].supersedes_node_id must be a string"
            )
        subject = e.get("subject")
        topic = e.get("topic")
        new_entries.append(
            {
                "kind": normalize_kind(e.get("kind")),
                "statement": statement.strip(),
                "subject": subject if isinstance(subject, str) else None,
                "topic": topic if isinstance(topic, str) else None,
                "confidence": _clamp_confidence(confidence),
                "source_card_ids": [s.strip() for s in source_ids if s.strip()],
                "supersedes_node_id": supersedes,
            }
        )

    reviewed = data.get("reviewed_card_ids")
    if reviewed is not None:
        if not isinstance(reviewed, list):
            raise ReviewParseError("reviewed_card_ids must be an array of strings")
        reviewed = [s for s in reviewed if isinstance(s, str)]

    return {
        "link_existing": link_entries,
        "new_nodes": new_entries,
        "reviewed_card_ids": reviewed,
    }


def _is_truncation_finish(finish_reason: Any) -> bool:
    if finish_reason is None:
        return False
    fr = str(finish_reason).strip().lower()
    if not fr:
        return False
    return "max_token" in fr or "max_output_token" in fr or fr.endswith("length")


def classify_review_response(
    raw: str | None, finish_reason: Any = None
) -> tuple[str, dict | None, str | None]:
    """LLM 応答を (outcome, parsed, error) に分類する（§5.4-3）。

    - provider が truncation 終了を報告 → truncated_response
    - 空応答 → empty_response
    - JSON/schema 違反 → parse_error
    - 正当な空配列 → no_changes / 操作あり → ok
    終了理由を提供しない provider (finish_reason=None) は、schema-valid な
    完全 JSON を parse できた場合のみ受理される（parse が門番になる）。
    """
    if _is_truncation_finish(finish_reason):
        return OUTCOME_TRUNCATED, None, f"finish_reason={finish_reason}"
    if raw is None or not str(raw).strip():
        return OUTCOME_EMPTY, None, "empty LLM response"
    try:
        parsed = parse_review_output(str(raw))
    except ReviewParseError as exc:
        return OUTCOME_PARSE_ERROR, None, str(exc)
    if not parsed["link_existing"] and not parsed["new_nodes"]:
        return OUTCOME_NO_CHANGES, parsed, None
    return OUTCOME_OK, parsed, None


def check_reviewed_card_ids(
    parsed: dict, expected_card_ids: set[str]
) -> str | None:
    """`reviewed_card_ids` の不一致を診断文字列にする（成功条件にはしない）。"""
    reviewed = parsed.get("reviewed_card_ids")
    if reviewed is None:
        return None
    reviewed_set = set(reviewed)
    missing = sorted(expected_card_ids - reviewed_set)
    unknown = sorted(reviewed_set - expected_card_ids)
    if not missing and not unknown:
        return None
    parts = []
    if missing:
        parts.append(f"missing={missing}")
    if unknown:
        parts.append(f"unknown={unknown}")
    return "reviewed_card_ids mismatch: " + " ".join(parts)


# ----------------------------------------------------------
# Application: link / create / supersede
# ----------------------------------------------------------

def apply_link_existing(
    *,
    repo,
    entries: list[dict],
    valid_node_ids: set[str],
    valid_card_ids: set[str],
    run_id: str,
) -> tuple[int, list[str], list[str]]:
    """既存 node への source link を適用する。

    repo は MemoryNodeRepository / MaturationUnitOfWork のどちらでもよい。
    入力外の node/card id を参照する操作は拒否し diagnostics へ記録する。
    DB エラーは握り潰さず propagate させる（transaction 全体を失敗させる）。

    Returns: (linked_count, uncertain_node_ids, diagnostics)
    """
    linked = 0
    diagnostics: list[str] = []
    contradict_count: dict[str, int] = {}
    support_count: dict[str, int] = {}

    for e in entries:
        node_id = e["node_id"]
        card_id = e["card_id"]
        if node_id not in valid_node_ids:
            diagnostics.append(f"link_existing rejected: unknown node_id {node_id}")
            continue
        if card_id not in valid_card_ids:
            diagnostics.append(f"link_existing rejected: unknown card_id {card_id}")
            continue
        relation = e["relation"]
        repo.upsert_source(
            node_id=node_id,
            card_id=card_id,
            relation=relation,
            confidence=e["confidence"],
            note=e.get("note"),
            created_by_run_id=run_id,
        )
        linked += 1
        if relation == "supports":
            support_count[node_id] = support_count.get(node_id, 0) + 1
        elif relation == "contradicts":
            contradict_count[node_id] = contradict_count.get(node_id, 0) + 1

    # uncertain 候補: contradicts が supports と同等以上の node
    uncertain = [
        nid
        for nid, cc in contradict_count.items()
        if cc >= support_count.get(nid, 0)
    ]
    return linked, uncertain, diagnostics


def apply_new_nodes(
    *,
    repo,
    entries: list[dict],
    valid_node_ids: set[str],
    valid_card_ids: set[str],
    run_id: str,
    candidate_threshold: float,
    active_threshold: float,
) -> tuple[int, int, list[str]]:
    """新規 candidate node を作成し、supersedes 指定があれば旧 node を superseded 化。

    supersedes_node_id が入力（既存 node 文脈）に無い場合は当該 supersede のみ
    拒否し diagnostics へ記録する（§5.4-3）。

    Returns: (created_count, superseded_count, diagnostics)
    """
    created = 0
    superseded = 0
    diagnostics: list[str] = []

    for e in entries:
        confidence = e["confidence"]
        if confidence < candidate_threshold:
            # confidence が低い candidate は作らない
            continue

        status = "active" if confidence >= active_threshold else "candidate"

        source_ids = []
        for cid in e["source_card_ids"]:
            if cid in valid_card_ids:
                source_ids.append(cid)
            else:
                diagnostics.append(
                    f"new_nodes source rejected: unknown card_id {cid}"
                )

        new_id = repo.create_node(
            kind=e["kind"],
            statement=e["statement"],
            subject=e.get("subject"),
            topic=e.get("topic"),
            confidence=confidence,
            status=status,
            metadata={"created_via": "stage3_node_review"},
            created_by_run_id=run_id,
        )
        created += 1

        for cid in source_ids:
            repo.upsert_source(
                node_id=new_id,
                card_id=cid,
                relation="supports",
                confidence=confidence,
                created_by_run_id=run_id,
            )

        old_id = e.get("supersedes_node_id")
        if old_id and old_id != new_id:
            if old_id not in valid_node_ids:
                diagnostics.append(
                    f"supersede rejected: unknown node_id {old_id}"
                )
            elif repo.supersede_node(
                old_node_id=old_id,
                new_node_id=new_id,
                updated_by_run_id=run_id,
            ):
                superseded += 1

    return created, superseded, diagnostics


def mark_uncertain_nodes(
    *,
    repo,
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
# Promotion proposals（§8: 提案は常時出力）
# ----------------------------------------------------------

def collect_promotion_proposals(
    *,
    repo,
    confidence_threshold: float,
    min_sources: int,
    now_iso: str | None = None,
) -> list[dict]:
    """昇格条件を全て満たす node を proposal として返す。

    条件:
      - status='active'
      - confidence >= confidence_threshold
      - `supports` ソースが min_sources 以上
      - サポートカードが複数日（source_date 優先）に分散している
    全 eligible node を pagination して評価する（旧 LIMIT 200 は撤去）。
    """
    proposals = []
    for n in repo.iter_nodes_by_status(["active"]):
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
                "proposed_at": now_iso or datetime.now().isoformat(),
            }
        )
    return proposals


def write_promotion_proposals_file(
    instance_path: Path, proposals: list[dict], *, now_iso: str | None = None
) -> Path:
    """memory_node_proposals.json に書き出す。

    既存の Key Memory proposals (key_memory_proposals.json) とは別ファイル。
    自動反映は行わず、レビュー用の出力にとどめる。DB commit 後に再生成可能な
    派生 artifact であり、書き出し失敗しても成熟結果自体は巻き戻さない（§5.4）。
    """
    out = instance_path / "memory_node_proposals.json"
    payload = {
        "generated_at": now_iso or datetime.now().isoformat(),
        "proposals": proposals,
    }
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out
