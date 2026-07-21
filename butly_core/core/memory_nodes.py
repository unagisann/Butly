"""Stage 3: Knowledge Maturation repository.

memory_maturation_runs / memory_nodes / memory_node_sources /
memory_maturation_run_cards を扱うレポジトリ層。
enum (kind / status / source relation) は Python 側で validation する。

構成（Stage 3 計画 §5.4）:
  - SQL 本体は module-level の ``_op_*`` 関数（connection を受け取り commit しない）。
  - ``MemoryNodeRepository``: 従来互換。呼び出しごとに接続し即 commit する。
  - ``MaturationUnitOfWork``: 1 connection を所有し途中 commit しない。
    node/source 更新・run counters・カード版スタンプ・run 完了を
    同一 SQLite transaction で確定するために使う。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Iterable, Iterator, Sequence

from butly_core.core.card_content import utc_now_stamp


# --- Enum allow-list (Section 4.2 / 4.3) ---

NODE_KINDS = {"preference", "fact", "habit", "other"}
NODE_STATUSES = {"candidate", "active", "uncertain", "superseded"}
SOURCE_RELATIONS = {"supports", "contradicts", "context"}
# abandoned: lock 取得後に回収された前 process の残骸 run（§5.4）
RUN_STATUSES = {"running", "completed", "failed", "skipped", "abandoned"}
# run に投入したカード版の状態（§5.1 memory_maturation_run_cards.status）
RUN_CARD_STATUSES = {
    "queued",
    "applied",
    "no_changes",
    "failed",
    "changed_during_run",
    "abandoned",
}


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


def validate_run_card_status(value: Any) -> str:
    if isinstance(value, str) and value in RUN_CARD_STATUSES:
        return value
    raise ValueError(f"invalid memory_maturation_run_cards.status: {value!r}")


def _short_uuid() -> str:
    return uuid.uuid4().hex[:12]


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    return conn


# ==================================================================
# module-level ops（connection を受け取り commit しない）
# ==================================================================

def _op_start_run(
    conn: sqlite3.Connection,
    instance_name: str,
    metadata: dict | None,
    now_stamp: str,
) -> str:
    run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{_short_uuid()}"
    meta_json = json.dumps(metadata or {}, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO memory_maturation_runs (
            id, instance_name, status, started_at, metadata_json
        ) VALUES (?, ?, 'running', ?, ?)
        """,
        (run_id, instance_name, now_stamp, meta_json),
    )
    return run_id


def _op_update_run_counters(
    conn: sqlite3.Connection, run_id: str, counters: dict
) -> None:
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
    conn.execute(
        f"UPDATE memory_maturation_runs SET {', '.join(sets)} WHERE id = ?",
        params,
    )


def _op_complete_run(
    conn: sqlite3.Connection, run_id: str, status: str, now_stamp: str
) -> None:
    if status not in RUN_STATUSES:
        raise ValueError(f"invalid run status: {status!r}")
    conn.execute(
        "UPDATE memory_maturation_runs SET status = ?, completed_at = ? WHERE id = ?",
        (status, now_stamp, run_id),
    )


def _op_fail_run(
    conn: sqlite3.Connection, run_id: str, error: str, now_stamp: str
) -> None:
    conn.execute(
        """
        UPDATE memory_maturation_runs
        SET status = 'failed', completed_at = ?, error = ?
        WHERE id = ?
        """,
        (now_stamp, str(error)[:4000], run_id),
    )


def _op_create_node(
    conn: sqlite3.Connection,
    *,
    kind: str,
    statement: str,
    subject: str | None,
    topic: str | None,
    confidence: float,
    status: str,
    metadata: dict | None,
    created_by_run_id: str | None,
    now_stamp: str,
) -> str:
    kind = normalize_kind(kind)
    status = validate_status(status)
    node_id = f"node_{datetime.now().strftime('%Y%m%d')}_{_short_uuid()}"
    meta_json = json.dumps(metadata or {}, ensure_ascii=False)
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
            now_stamp,
            now_stamp,
            now_stamp,
        ),
    )
    return node_id


def _op_update_node(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    statement: str | None,
    confidence: float | None,
    status: str | None,
    metadata: dict | None,
    updated_by_run_id: str | None,
    reinforce: bool,
    now_stamp: str,
    last_decay_at: str | None = None,
) -> bool:
    sets: list[str] = ["updated_at = ?"]
    params: list[Any] = [now_stamp]

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
        params.append(now_stamp)
    if last_decay_at is not None:
        sets.append("last_decay_at = ?")
        params.append(last_decay_at)

    params.append(node_id)
    cur = conn.execute(
        f"UPDATE memory_nodes SET {', '.join(sets)} WHERE id = ?",
        params,
    )
    return cur.rowcount > 0


def _op_supersede_node(
    conn: sqlite3.Connection,
    *,
    old_node_id: str,
    new_node_id: str,
    updated_by_run_id: str | None,
    now_stamp: str,
) -> bool:
    if old_node_id == new_node_id:
        return False
    cur = conn.execute(
        """
        UPDATE memory_nodes
        SET status = 'superseded',
            superseded_by_node_id = ?,
            updated_at = ?,
            updated_by_run_id = COALESCE(?, updated_by_run_id)
        WHERE id = ?
        """,
        (new_node_id, now_stamp, updated_by_run_id, old_node_id),
    )
    return cur.rowcount > 0


def _op_get_node(conn: sqlite3.Connection, node_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM memory_nodes WHERE id = ?", (node_id,)
    ).fetchone()
    return dict(row) if row else None


def _op_find_nodes(
    conn: sqlite3.Connection,
    *,
    statuses: Iterable[str] | None,
    kind: str | None,
    topic: str | None,
    limit: int,
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
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _op_upsert_source(
    conn: sqlite3.Connection,
    *,
    node_id: str,
    card_id: str,
    relation: str,
    confidence: float,
    note: str | None,
    created_by_run_id: str | None,
) -> bool:
    relation = validate_relation(relation)
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
    return cur.rowcount > 0


def _op_count_sources(
    conn: sqlite3.Connection, node_id: str, relation: str | None
) -> int:
    sql = "SELECT COUNT(*) FROM memory_node_sources WHERE node_id = ?"
    params: list[Any] = [node_id]
    if relation:
        sql += " AND relation = ?"
        params.append(validate_relation(relation))
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row else 0


def _op_distinct_support_days(conn: sqlite3.Connection, node_id: str) -> int:
    """supports ソースカードの "ユニークな日付" 数（promotion の複数日条件用）。

    出来事の日付 source_date を優先し、欠損時だけ created_at の日付へ
    フォールバックする（§7: 履歴 import や同日 bootstrap でも評価可能に）。
    """
    rows = conn.execute(
        """
        SELECT DISTINCT COALESCE(
                   NULLIF(substr(c.source_date, 1, 10), ''),
                   substr(c.created_at, 1, 10)
               ) AS d
        FROM memory_node_sources s
        JOIN knowledge_cards c ON c.id = s.card_id
        WHERE s.node_id = ?
          AND s.relation = 'supports'
        """,
        (node_id,),
    ).fetchall()
    return len({r[0] for r in rows if r[0]})


def _op_record_run_cards(
    conn: sqlite3.Connection,
    run_id: str,
    card_versions: Sequence[tuple[str, str]],
    now_stamp: str,
) -> None:
    """run に投入したカード版を status='queued' で記録する。"""
    conn.executemany(
        """
        INSERT INTO memory_maturation_run_cards (
            run_id, card_id, content_hash, status, created_at, updated_at
        ) VALUES (?, ?, ?, 'queued', ?, ?)
        """,
        [(run_id, cid, chash, now_stamp, now_stamp) for cid, chash in card_versions],
    )


def _op_mark_run_cards(
    conn: sqlite3.Connection,
    run_id: str,
    card_ids: Sequence[str],
    *,
    status: str,
    error: str | None,
    diagnostic: str | None,
    now_stamp: str,
) -> int:
    validate_run_card_status(status)
    if not card_ids:
        return 0
    placeholders = ",".join("?" for _ in card_ids)
    cur = conn.execute(
        f"""
        UPDATE memory_maturation_run_cards
        SET status = ?, error = ?, diagnostic = ?, updated_at = ?
        WHERE run_id = ? AND card_id IN ({placeholders})
        """,
        [status, error, diagnostic, now_stamp, run_id, *card_ids],
    )
    return cur.rowcount


def _op_get_card_hashes(
    conn: sqlite3.Connection, card_ids: Sequence[str]
) -> dict[str, str | None]:
    if not card_ids:
        return {}
    placeholders = ",".join("?" for _ in card_ids)
    rows = conn.execute(
        f"SELECT id, content_hash FROM knowledge_cards WHERE id IN ({placeholders})",
        list(card_ids),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def _op_stamp_card_version(
    conn: sqlite3.Connection,
    *,
    card_id: str,
    content_hash: str,
    run_id: str,
    now_stamp: str,
) -> bool:
    """成功レビューした版をスタンプする。版一致条件付き（§5.4-5）。"""
    cur = conn.execute(
        """
        UPDATE knowledge_cards
        SET last_matured_content_hash = ?,
            last_matured_at = ?,
            last_matured_run_id = ?
        WHERE id = ? AND content_hash = ?
        """,
        (content_hash, now_stamp, run_id, card_id, content_hash),
    )
    return cur.rowcount > 0


# ==================================================================
# Repository（従来互換: 呼び出しごとに接続・即 commit）
# ==================================================================

class MemoryNodeRepository:
    """memory_maturation_runs / memory_nodes / memory_node_sources を操作する。

    呼び出し側で ButlyDatabase をインスタンス化することで migration を確定させ、
    その同じ DB パスを使う前提（テーブル定義は ButlyDatabase 側）。
    batch transaction が必要な適用経路は MaturationUnitOfWork を使うこと。
    """

    def __init__(self, db_path: str):
        self.db_path = db_path

    # ----------------------------------------------------------
    # connection
    # ----------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        return _connect(self.db_path)

    # ----------------------------------------------------------
    # maturation runs
    # ----------------------------------------------------------
    def start_run(
        self,
        instance_name: str,
        metadata: dict | None = None,
        *,
        now_stamp: str | None = None,
    ) -> str:
        with self._connect() as conn:
            run_id = _op_start_run(
                conn, instance_name, metadata, now_stamp or utc_now_stamp()
            )
            conn.commit()
        return run_id

    def update_run_counters(self, run_id: str, **counters: int) -> None:
        with self._connect() as conn:
            _op_update_run_counters(conn, run_id, counters)
            conn.commit()

    def complete_run(
        self,
        run_id: str,
        status: str = "completed",
        *,
        now_stamp: str | None = None,
    ) -> None:
        with self._connect() as conn:
            _op_complete_run(conn, run_id, status, now_stamp or utc_now_stamp())
            conn.commit()

    def fail_run(
        self, run_id: str, error: str, *, now_stamp: str | None = None
    ) -> None:
        with self._connect() as conn:
            _op_fail_run(conn, run_id, error, now_stamp or utc_now_stamp())
            conn.commit()

    def get_run(self, run_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM memory_maturation_runs WHERE id = ?", (run_id,)
            ).fetchone()
            return dict(row) if row else None

    def recover_orphan_runs(
        self, instance_name: str, *, now_stamp: str | None = None
    ) -> int:
        """lock 取得後に残っている 'running' run を 'abandoned' として回収する。

        lock が取れた時点で live な並行 run は存在しないため、残骸は前 process の
        ものと断定できる（§5.4-1）。run_cards の 'queued' も 'abandoned' にし、
        カード版は未処理のまま再選択可能に保つ。
        Returns: 回収した run 数。
        """
        stamp = now_stamp or utc_now_stamp()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id FROM memory_maturation_runs
                WHERE instance_name = ? AND status = 'running'
                """,
                (instance_name,),
            ).fetchall()
            run_ids = [r[0] for r in rows]
            for rid in run_ids:
                _op_complete_run(conn, rid, "abandoned", stamp)
                conn.execute(
                    """
                    UPDATE memory_maturation_run_cards
                    SET status = 'abandoned', updated_at = ?
                    WHERE run_id = ? AND status = 'queued'
                    """,
                    (stamp, rid),
                )
            conn.commit()
        return len(run_ids)

    # ----------------------------------------------------------
    # run cards（監査・失敗記録。成功 stamp は UnitOfWork 経由のみ）
    # ----------------------------------------------------------
    def record_run_cards(
        self,
        run_id: str,
        card_versions: Sequence[tuple[str, str]],
        *,
        now_stamp: str | None = None,
    ) -> None:
        with self._connect() as conn:
            _op_record_run_cards(
                conn, run_id, card_versions, now_stamp or utc_now_stamp()
            )
            conn.commit()

    def mark_run_cards(
        self,
        run_id: str,
        card_ids: Sequence[str],
        *,
        status: str,
        error: str | None = None,
        diagnostic: str | None = None,
        now_stamp: str | None = None,
    ) -> int:
        with self._connect() as conn:
            n = _op_mark_run_cards(
                conn,
                run_id,
                card_ids,
                status=status,
                error=error,
                diagnostic=diagnostic,
                now_stamp=now_stamp or utc_now_stamp(),
            )
            conn.commit()
            return n

    def list_run_cards(self, run_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM memory_maturation_run_cards WHERE run_id = ?",
                (run_id,),
            ).fetchall()
            return [dict(r) for r in rows]

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
        with self._connect() as conn:
            node_id = _op_create_node(
                conn,
                kind=kind,
                statement=statement,
                subject=subject,
                topic=topic,
                confidence=confidence,
                status=status,
                metadata=metadata,
                created_by_run_id=created_by_run_id,
                now_stamp=utc_now_stamp(),
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
        with self._connect() as conn:
            ok = _op_update_node(
                conn,
                node_id,
                statement=statement,
                confidence=confidence,
                status=status,
                metadata=metadata,
                updated_by_run_id=updated_by_run_id,
                reinforce=reinforce,
                now_stamp=utc_now_stamp(),
            )
            conn.commit()
            return ok

    def supersede_node(
        self,
        *,
        old_node_id: str,
        new_node_id: str,
        updated_by_run_id: str | None = None,
    ) -> bool:
        """旧 node を `superseded` にし、`superseded_by_node_id` に新 node を設定する。"""
        with self._connect() as conn:
            ok = _op_supersede_node(
                conn,
                old_node_id=old_node_id,
                new_node_id=new_node_id,
                updated_by_run_id=updated_by_run_id,
                now_stamp=utc_now_stamp(),
            )
            conn.commit()
            return ok

    def get_node(self, node_id: str) -> dict | None:
        with self._connect() as conn:
            return _op_get_node(conn, node_id)

    def find_nodes(
        self,
        *,
        statuses: Iterable[str] | None = None,
        kind: str | None = None,
        topic: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        with self._connect() as conn:
            return _op_find_nodes(
                conn, statuses=statuses, kind=kind, topic=topic, limit=limit
            )

    def iter_nodes_by_status(
        self,
        statuses: Iterable[str],
        *,
        page_size: int = 500,
    ) -> Iterator[dict]:
        """status 条件の全 node を keyset pagination で列挙する（§8: LIMIT 200 撤去）。"""
        statuses = list(statuses)
        if not statuses:
            return
        placeholders = ",".join("?" for _ in statuses)
        last_id = ""
        while True:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT * FROM memory_nodes
                    WHERE status IN ({placeholders}) AND id > ?
                    ORDER BY id ASC LIMIT ?
                    """,
                    [*statuses, last_id, int(page_size)],
                ).fetchall()
            if not rows:
                return
            for r in rows:
                yield dict(r)
            last_id = rows[-1]["id"]
            if len(rows) < page_size:
                return

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
        with self._connect() as conn:
            ok = _op_upsert_source(
                conn,
                node_id=node_id,
                card_id=card_id,
                relation=relation,
                confidence=confidence,
                note=note,
                created_by_run_id=created_by_run_id,
            )
            conn.commit()
            return ok

    def count_sources(self, node_id: str, relation: str | None = None) -> int:
        with self._connect() as conn:
            return _op_count_sources(conn, node_id, relation)

    def list_sources(self, node_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM memory_node_sources WHERE node_id = ?",
                (node_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def distinct_support_days(self, node_id: str) -> int:
        with self._connect() as conn:
            return _op_distinct_support_days(conn, node_id)


# ==================================================================
# Unit of Work（§5.4: 単一 transaction の適用境界）
# ==================================================================

class MaturationUnitOfWork:
    """1 connection を所有し、close まで commit しない Stage 3 適用境界。

    使い方::

        with MaturationUnitOfWork(db_path, now_stamp=stamp) as uow:
            ... node/source 更新・run counters・カード版スタンプ・run 完了 ...
        # with 正常終了で commit / 例外で rollback

    LLM 後・DB 適用中のクラッシュは rollback され、再実行で node が
    二重作成されない（§5.4）。
    """

    def __init__(self, db_path: str, *, now_stamp: str | None = None):
        self.db_path = db_path
        self.now_stamp = now_stamp or utc_now_stamp()
        self.conn: sqlite3.Connection | None = None

    def __enter__(self) -> "MaturationUnitOfWork":
        self.conn = _connect(self.db_path)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        assert self.conn is not None
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()
            self.conn = None

    def _c(self) -> sqlite3.Connection:
        if self.conn is None:
            raise RuntimeError("MaturationUnitOfWork used outside 'with' block")
        return self.conn

    # --- nodes / sources（repo と同名 API。apply_* 関数から duck typing で使う） ---
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
        return _op_create_node(
            self._c(),
            kind=kind,
            statement=statement,
            subject=subject,
            topic=topic,
            confidence=confidence,
            status=status,
            metadata=metadata,
            created_by_run_id=created_by_run_id,
            now_stamp=self.now_stamp,
        )

    def update_node(self, node_id: str, **kwargs) -> bool:
        kwargs.setdefault("statement", None)
        kwargs.setdefault("confidence", None)
        kwargs.setdefault("status", None)
        kwargs.setdefault("metadata", None)
        kwargs.setdefault("updated_by_run_id", None)
        kwargs.setdefault("reinforce", False)
        return _op_update_node(
            self._c(), node_id, now_stamp=self.now_stamp, **kwargs
        )

    def supersede_node(
        self,
        *,
        old_node_id: str,
        new_node_id: str,
        updated_by_run_id: str | None = None,
    ) -> bool:
        return _op_supersede_node(
            self._c(),
            old_node_id=old_node_id,
            new_node_id=new_node_id,
            updated_by_run_id=updated_by_run_id,
            now_stamp=self.now_stamp,
        )

    def get_node(self, node_id: str) -> dict | None:
        return _op_get_node(self._c(), node_id)

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
        return _op_upsert_source(
            self._c(),
            node_id=node_id,
            card_id=card_id,
            relation=relation,
            confidence=confidence,
            note=note,
            created_by_run_id=created_by_run_id,
        )

    def count_sources(self, node_id: str, relation: str | None = None) -> int:
        return _op_count_sources(self._c(), node_id, relation)

    # --- run / run_cards / card stamps ---
    def update_run_counters(self, run_id: str, **counters: int) -> None:
        _op_update_run_counters(self._c(), run_id, counters)

    def complete_run(self, run_id: str, status: str = "completed") -> None:
        _op_complete_run(self._c(), run_id, status, self.now_stamp)

    def fail_run(self, run_id: str, error: str) -> None:
        _op_fail_run(self._c(), run_id, error, self.now_stamp)

    def mark_run_cards(
        self,
        run_id: str,
        card_ids: Sequence[str],
        *,
        status: str,
        error: str | None = None,
        diagnostic: str | None = None,
    ) -> int:
        return _op_mark_run_cards(
            self._c(),
            run_id,
            card_ids,
            status=status,
            error=error,
            diagnostic=diagnostic,
            now_stamp=self.now_stamp,
        )

    def get_card_hashes(self, card_ids: Sequence[str]) -> dict[str, str | None]:
        return _op_get_card_hashes(self._c(), card_ids)

    def stamp_card_version(
        self, *, card_id: str, content_hash: str, run_id: str
    ) -> bool:
        return _op_stamp_card_version(
            self._c(),
            card_id=card_id,
            content_hash=content_hash,
            run_id=run_id,
            now_stamp=self.now_stamp,
        )
