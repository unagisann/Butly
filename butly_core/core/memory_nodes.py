"""Stage 3: Knowledge Maturation repository.

memory_maturation_runs / memory_nodes / memory_node_sources を扱う最小レポジトリ。
enum (kind / status / source relation) は Python 側で validation する。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable


# --- Enum allow-list (Section 4.2 / 4.3) ---

NODE_KINDS = {"preference", "fact", "habit", "other"}
NODE_STATUSES = {"candidate", "active", "uncertain", "superseded"}
SOURCE_RELATIONS = {"supports", "contradicts", "context"}
RUN_STATUSES = {"running", "completed", "failed", "skipped"}


def normalize_kind(value: Any) -> str:
    """未知の kind は "other" にフォールバック。"""
    if isinstance(value, str) and value in NODE_KINDS:
        return value
    return "other"


def validate_status(value: Any) -> str:
    """status はクローズドな集合。違反は ValueError。"""
    if isinstance(value, str) and value in NODE_STATUSES:
        return value
    raise ValueError(f"invalid memory_nodes.status: {value!r}")


def validate_relation(value: Any) -> str:
    """source relation はクローズドな集合。違反は ValueError。"""
    if isinstance(value, str) and value in SOURCE_RELATIONS:
        return value
    raise ValueError(f"invalid memory_node_sources.relation: {value!r}")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _short_uuid() -> str:
    return uuid.uuid4().hex[:12]


class MemoryNodeRepository:
    """memory_maturation_runs / memory_nodes / memory_node_sources を操作する。

    呼び出し側で ButlyDatabase をインスタンス化することで migration を確定させ、
    その同じ DB パスを使う前提（テーブル定義は ButlyDatabase 側）。
    """

    def __init__(self, db_path: str):
        self.db_path = db_path

    # ----------------------------------------------------------
    # connection
    # ----------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.row_factory = sqlite3.Row
        return conn

    # ----------------------------------------------------------
    # maturation runs
    # ----------------------------------------------------------
    def start_run(self, instance_name: str, metadata: dict | None = None) -> str:
        run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{_short_uuid()}"
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_maturation_runs (
                    id, instance_name, status, started_at, metadata_json
                ) VALUES (?, ?, 'running', ?, ?)
                """,
                (run_id, instance_name, _utc_now_iso(), meta_json),
            )
            conn.commit()
        return run_id

    def update_run_counters(self, run_id: str, **counters: int) -> None:
        allowed = {
            "reviewed_card_count",
            "created_node_count",
            "linked_source_count",
            "superseded_node_count",
        }
        sets = []
        params: list[Any] = []
        for k, v in counters.items():
            if k in allowed:
                sets.append(f"{k} = ?")
                params.append(int(v))
        if not sets:
            return
        params.append(run_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE memory_maturation_runs SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            conn.commit()

    def complete_run(self, run_id: str, status: str = "completed") -> None:
        if status not in RUN_STATUSES:
            raise ValueError(f"invalid run status: {status!r}")
        with self._connect() as conn:
            conn.execute(
                "UPDATE memory_maturation_runs SET status = ?, completed_at = ? WHERE id = ?",
                (status, _utc_now_iso(), run_id),
            )
            conn.commit()

    def fail_run(self, run_id: str, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE memory_maturation_runs
                SET status = 'failed', completed_at = ?, error = ?
                WHERE id = ?
                """,
                (_utc_now_iso(), str(error)[:4000], run_id),
            )
            conn.commit()

    def get_run(self, run_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM memory_maturation_runs WHERE id = ?", (run_id,)
            ).fetchone()
            return dict(row) if row else None

    # ----------------------------------------------------------
    # nodes
    # ----------------------------------------------------------
    def create_node(
        self,
        *,
        kind: str,
        statement: str,
        subject: str | None = None,
        topic: str | None = None,
        confidence: float = 0.0,
        status: str = "candidate",
        metadata: dict | None = None,
        created_by_run_id: str | None = None,
    ) -> str:
        kind = normalize_kind(kind)
        status = validate_status(status)
        node_id = f"node_{datetime.now().strftime('%Y%m%d')}_{_short_uuid()}"
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_nodes (
                    id, kind, subject, topic, statement, confidence, status,
                    metadata_json, created_by_run_id, updated_by_run_id,
                    created_at, updated_at, last_reinforced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node_id,
                    kind,
                    subject,
                    topic,
                    statement,
                    float(confidence),
                    status,
                    meta_json,
                    created_by_run_id,
                    created_by_run_id,
                    now,
                    now,
                    now,
                ),
            )
            conn.commit()
        return node_id

    def update_node(
        self,
        node_id: str,
        *,
        statement: str | None = None,
        confidence: float | None = None,
        status: str | None = None,
        metadata: dict | None = None,
        updated_by_run_id: str | None = None,
        reinforce: bool = False,
    ) -> bool:
        sets: list[str] = ["updated_at = ?"]
        params: list[Any] = [_utc_now_iso()]

        if statement is not None:
            sets.append("statement = ?")
            params.append(statement)
        if confidence is not None:
            sets.append("confidence = ?")
            params.append(float(confidence))
        if status is not None:
            sets.append("status = ?")
            params.append(validate_status(status))
        if metadata is not None:
            sets.append("metadata_json = ?")
            params.append(json.dumps(metadata, ensure_ascii=False))
        if updated_by_run_id is not None:
            sets.append("updated_by_run_id = ?")
            params.append(updated_by_run_id)
        if reinforce:
            sets.append("last_reinforced_at = ?")
            params.append(_utc_now_iso())

        params.append(node_id)
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE memory_nodes SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            conn.commit()
            return cur.rowcount > 0

    def supersede_node(
        self,
        *,
        old_node_id: str,
        new_node_id: str,
        updated_by_run_id: str | None = None,
    ) -> bool:
        """旧 node を `superseded` にし、`superseded_by_node_id` に新 node を設定する。"""
        if old_node_id == new_node_id:
            return False
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE memory_nodes
                SET status = 'superseded',
                    superseded_by_node_id = ?,
                    updated_at = ?,
                    updated_by_run_id = COALESCE(?, updated_by_run_id)
                WHERE id = ?
                """,
                (new_node_id, _utc_now_iso(), updated_by_run_id, old_node_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def get_node(self, node_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM memory_nodes WHERE id = ?", (node_id,)
            ).fetchone()
            return dict(row) if row else None

    def find_nodes(
        self,
        *,
        statuses: Iterable[str] | None = None,
        kind: str | None = None,
        topic: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        sql = "SELECT * FROM memory_nodes WHERE 1=1"
        params: list[Any] = []
        if statuses is not None:
            statuses = list(statuses)
            if statuses:
                placeholders = ",".join("?" for _ in statuses)
                sql += f" AND status IN ({placeholders})"
                params.extend(statuses)
        if kind:
            sql += " AND kind = ?"
            params.append(normalize_kind(kind))
        if topic:
            sql += " AND topic = ?"
            params.append(topic)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def find_active_nodes_for_card(self, card_id: str) -> list[dict]:
        """指定 card に紐づく active node を返す（Chat 経路の opt-in 利用用）。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT n.*
                FROM memory_nodes n
                JOIN memory_node_sources s ON s.node_id = n.id
                WHERE s.card_id = ?
                  AND n.status = 'active'
                ORDER BY n.confidence DESC, n.updated_at DESC
                """,
                (card_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ----------------------------------------------------------
    # sources (node ↔ card)
    # ----------------------------------------------------------
    def upsert_source(
        self,
        *,
        node_id: str,
        card_id: str,
        relation: str = "supports",
        confidence: float = 0.0,
        note: str | None = None,
        created_by_run_id: str | None = None,
    ) -> bool:
        relation = validate_relation(relation)
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO memory_node_sources (
                    node_id, card_id, relation, confidence, note, created_by_run_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id, card_id, relation) DO UPDATE SET
                    confidence = excluded.confidence,
                    note = excluded.note
                """,
                (
                    node_id,
                    card_id,
                    relation,
                    float(confidence),
                    note,
                    created_by_run_id,
                ),
            )
            conn.commit()
            return cur.rowcount > 0

    def count_sources(self, node_id: str, relation: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM memory_node_sources WHERE node_id = ?"
        params: list[Any] = [node_id]
        if relation:
            sql += " AND relation = ?"
            params.append(validate_relation(relation))
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return int(row[0]) if row else 0

    def list_sources(self, node_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM memory_node_sources WHERE node_id = ?",
                (node_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def distinct_support_days(self, node_id: str) -> int:
        """supports 関係のソースカードが生成された "ユニークな日付" 数。

        promotion 判定の "複数日で補強されている" 条件用。
        知識カード生成日 (created_at) を日付に丸めて重複排除する。
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT substr(c.created_at, 1, 10) AS d
                FROM memory_node_sources s
                JOIN knowledge_cards c ON c.id = s.card_id
                WHERE s.node_id = ?
                  AND s.relation = 'supports'
                  AND c.created_at IS NOT NULL
                """,
                (node_id,),
            ).fetchall()
            return len({r[0] for r in rows if r[0]})
