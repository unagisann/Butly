import sqlite3
import os
from datetime import datetime, timezone, timedelta

from butly_core.core.card_content import (
    compute_content_hash,
    normalize_maturation_time,
    utc_now_stamp,
)
from butly_core.core.hybrid_search import ensure_fts_index


def _table_columns(cursor, table: str) -> set:
    """PRAGMA table_info でテーブルの既存カラム名集合を返す。"""
    return {row[1] for row in cursor.execute(f"PRAGMA table_info({table})")}


def _ensure_column(cursor, table: str, column: str, decl: str) -> bool:
    """カラムが無ければ追加する。duplicate 以外の migration エラーは表面化させる。

    従来の try/except OperationalError 方式は duplicate 以外の失敗
    （ロック・ディスク等）まで握り潰していたため、存在確認してから ALTER する。
    Returns: 追加した場合 True。
    """
    if column in _table_columns(cursor, table):
        return False
    cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    return True


class ButlyDatabase:
    def __init__(self, db_path="butly_memory.db"):
        self.db_path = db_path
        self.fts_status: dict = {}
        self._initialize_db()

    def _get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _initialize_db(self):
        """スプレッドシートの定義に基づいたテーブル構築"""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # 1. ナレッジカード本体テーブル
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_cards (
                id TEXT PRIMARY KEY,
                type TEXT DEFAULT 'Master', -- 'Master' または 'Variant'
                category TEXT NOT NULL,      -- Project, Hobby, Life, Tech, Unclassified
                title TEXT NOT NULL,
                tags TEXT,                  -- カンマ区切りの文字列
                ai_importance INTEGER,      -- 1-10
                humanity_importance INTEGER, -- 1-10
                summary TEXT,
                episode TEXT,               -- AIの所感
                count INTEGER DEFAULT 1,    -- 累積回数
                raw_reference TEXT,         -- 元のJSONファイル名
                embedding_blob BLOB,        -- Embedding Vector (float32 bytes)
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_pinned INTEGER DEFAULT 0,
                is_archived INTEGER DEFAULT 0
            )
            """)

            # 2. アクセスログテーブル（記憶の統合・タイムスタンプ用）
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS access_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id TEXT,
                accessed_at DATE,
                FOREIGN KEY(card_id) REFERENCES knowledge_cards(id)
            )
            """)

            # DB Migration: knowledge_cards の追加カラム。
            # PRAGMA table_info で存在確認してから追加し、duplicate 以外の
            # migration エラーを表面化させる（Stage 3 計画 §3.1）。
            _ensure_column(cursor, "knowledge_cards", "is_pinned", "INTEGER DEFAULT 0")
            _ensure_column(cursor, "knowledge_cards", "is_archived", "INTEGER DEFAULT 0")
            _ensure_column(cursor, "knowledge_cards", "usage_count", "INTEGER DEFAULT 0")
            _ensure_column(cursor, "knowledge_cards", "last_counted_at", "TEXT")

            # source_date: 元会話の日付 (YYYY-MM-DD)。time decay の基準。
            # 履歴インポートや後日の知識化でも「出来事の古さ」で減衰できる。
            # source_files: 生成に使った RAW ファイル名の JSON 配列
            # (memory_archive/2_knowledgeized/ 配下への遡及用)。
            _ensure_column(cursor, "knowledge_cards", "source_date", "TEXT")
            _ensure_column(cursor, "knowledge_cards", "source_files", "TEXT")

            # --- Stage 3 レビューキュー (§5.1) ---
            # content_hash: prompt に渡す意味内容の SHA-256（版識別子）
            # last_matured_content_hash: 最後に成功レビューした版。NULL または
            #   content_hash と不一致ならキュー内
            # maturation_queued_at: 現在の版がキューへ入った固定長 UTC 時刻（FIFO 用）
            # last_matured_at / last_matured_run_id: 監査専用（再レビュー判定に使わない）
            _ensure_column(cursor, "knowledge_cards", "content_hash", "TEXT")
            _ensure_column(
                cursor, "knowledge_cards", "last_matured_content_hash", "TEXT"
            )
            _ensure_column(cursor, "knowledge_cards", "maturation_queued_at", "TEXT")
            _ensure_column(cursor, "knowledge_cards", "last_matured_at", "TEXT")
            _ensure_column(cursor, "knowledge_cards", "last_matured_run_id", "TEXT")

            # --- 保存済み embedding の素性（モデル差し替え検知用） ---
            # 1 行だけ持つ。embedding_blob を書いた側が upsert し、起動時
            # チェックが現在の設定と突き合わせる。DB と一緒にクローンされる
            # 必要があるので instance DB 内に置く（eval の workspace 複製で
            # そのまま付いてくる）。
            cursor.execute(
                """
            CREATE TABLE IF NOT EXISTS embedding_meta (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                model_name TEXT,
                profile TEXT,
                dim INTEGER,
                updated_at TEXT
            )
            """
            )

            # --- Stage 3: Knowledge Maturation tables ---
            cursor.execute(
                """
            CREATE TABLE IF NOT EXISTS memory_maturation_runs (
                id TEXT PRIMARY KEY,
                instance_name TEXT NOT NULL,
                status TEXT NOT NULL,
                reviewed_card_count INTEGER DEFAULT 0,
                created_node_count INTEGER DEFAULT 0,
                linked_source_count INTEGER DEFAULT 0,
                superseded_node_count INTEGER DEFAULT 0,
                started_at TEXT,
                completed_at TEXT,
                error TEXT,
                metadata_json TEXT
            )
            """
            )

            cursor.execute(
                """
            CREATE TABLE IF NOT EXISTS memory_nodes (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                subject TEXT,
                topic TEXT,
                statement TEXT NOT NULL,
                confidence REAL DEFAULT 0.0,
                status TEXT DEFAULT 'candidate',
                superseded_by_node_id TEXT,
                metadata_json TEXT,
                created_by_run_id TEXT,
                updated_by_run_id TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_reinforced_at TEXT,
                FOREIGN KEY (superseded_by_node_id) REFERENCES memory_nodes(id),
                FOREIGN KEY (created_by_run_id) REFERENCES memory_maturation_runs(id),
                FOREIGN KEY (updated_by_run_id) REFERENCES memory_maturation_runs(id)
            )
            """
            )

            cursor.execute(
                """
            CREATE TABLE IF NOT EXISTS memory_node_sources (
                node_id TEXT NOT NULL,
                card_id TEXT NOT NULL,
                relation TEXT DEFAULT 'supports',
                confidence REAL DEFAULT 0.0,
                note TEXT,
                created_by_run_id TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (node_id, card_id, relation),
                FOREIGN KEY (node_id) REFERENCES memory_nodes(id),
                FOREIGN KEY (card_id) REFERENCES knowledge_cards(id),
                FOREIGN KEY (created_by_run_id) REFERENCES memory_maturation_runs(id)
            )
            """
            )

            # --- Stage 3 reflection（§7 staleness 減衰） ---
            # last_decay_at: 最後に減衰を適用した基準時刻。同じ stale 期間に
            # 何度 run しても二重減点しないための anchor。
            _ensure_column(cursor, "memory_nodes", "last_decay_at", "TEXT")

            # run に渡したカード版の記録（§5.1）。再開・監査・失敗分類用。
            # status: queued / applied / no_changes / failed / changed_during_run / abandoned
            cursor.execute(
                """
            CREATE TABLE IF NOT EXISTS memory_maturation_run_cards (
                run_id TEXT NOT NULL,
                card_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                error TEXT,
                diagnostic TEXT,
                created_at TEXT,
                updated_at TEXT,
                UNIQUE (run_id, card_id, content_hash),
                FOREIGN KEY (run_id) REFERENCES memory_maturation_runs(id),
                FOREIGN KEY (card_id) REFERENCES knowledge_cards(id)
            )
            """
            )

            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_nodes_status_kind_topic "
                "ON memory_nodes(status, kind, topic)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_node_sources_card "
                "ON memory_node_sources(card_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_node_sources_node "
                "ON memory_node_sources(node_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_maturation_runs_instance_started "
                "ON memory_maturation_runs(instance_name, started_at)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_knowledge_cards_last_counted_at "
                "ON knowledge_cards(last_counted_at)"
            )
            # レビューキュー走査（§5.3 FIFO）と run 追跡用 index
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_knowledge_cards_maturation_queue "
                "ON knowledge_cards(is_archived, maturation_queued_at)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_maturation_run_cards_run_status "
                "ON memory_maturation_run_cards(run_id, status)"
            )

            # 既存カードの content_hash / maturation_queued_at backfill（§5.1）。
            # queued_at は parse 可能な created_at を UTC へ正規化し、不能時は
            # migration 開始時刻を使う。
            self._backfill_content_hashes(cursor)

            # ハイブリッド検索用の FTS5(trigram) 索引（検索改修計画 §3.1）。
            # 既存 migration と同じトランザクション内で作り、backfill も同時に
            # 終わらせる。FTS5/trigram が使えない SQLite では索引を作らず、
            # 検索側が vector へフォールバックする（起動は止めない）。
            self.fts_status = ensure_fts_index(conn)

            conn.commit()

    @staticmethod
    def _backfill_content_hashes(cursor) -> int:
        """content_hash が NULL のカードへ hash と queue 時刻を backfill する。

        Returns: backfill した行数。
        """
        rows = cursor.execute(
            """
            SELECT id, title, summary, episode, tags, category, source_date, created_at
            FROM knowledge_cards
            WHERE content_hash IS NULL
            """
        ).fetchall()
        if not rows:
            return 0
        migration_stamp = utc_now_stamp()
        updates = []
        for row in rows:
            card = {
                "title": row[1],
                "summary": row[2],
                "episode": row[3],
                "tags": row[4],
                "category": row[5],
                "source_date": row[6],
            }
            content_hash = compute_content_hash(card)
            queued_at = normalize_maturation_time(row[7], fallback=migration_stamp)
            updates.append((content_hash, queued_at, row[0]))
        cursor.executemany(
            """
            UPDATE knowledge_cards
            SET content_hash = ?, maturation_queued_at = ?
            WHERE id = ? AND content_hash IS NULL
            """,
            updates,
        )
        return len(updates)

    @staticmethod
    def refresh_card_content_hash(cursor, card_id, *, now_stamp=None) -> bool:
        """カード本文の変更後に content_hash を再計算する共通経路（§5.2）。

        hash が変わった時だけ maturation_queued_at を更新する。
        pin/archive/usage 等、本文を変えない操作からは呼ばなくてよい
        （呼んでも hash 不変なので queue 時刻は動かない）。
        Returns: hash が変わって再キューした場合 True。
        """
        row = cursor.execute(
            """
            SELECT title, summary, episode, tags, category, source_date, content_hash
            FROM knowledge_cards WHERE id = ?
            """,
            (card_id,),
        ).fetchone()
        if row is None:
            return False
        card = {
            "title": row[0],
            "summary": row[1],
            "episode": row[2],
            "tags": row[3],
            "category": row[4],
            "source_date": row[5],
        }
        new_hash = compute_content_hash(card)
        if new_hash == row[6]:
            return False
        cursor.execute(
            """
            UPDATE knowledge_cards
            SET content_hash = ?, maturation_queued_at = ?
            WHERE id = ?
            """,
            (new_hash, now_stamp or utc_now_stamp(), card_id),
        )
        return True

    def register_knowledge(self, card_data):
        """ナレッジカードをDBに登録または更新する"""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # 既存のIDがあるか確認（統合ロジック）
            cursor.execute(
                "SELECT count FROM knowledge_cards WHERE id = ?", (card_data["id"],)
            )
            row = cursor.fetchone()

            if row:
                # 既存カードの更新（カウントアップ）
                new_count = row[0] + 1
                cursor.execute(
                    """
                UPDATE knowledge_cards SET 
                    count = ?, 
                    updated_at = CURRENT_TIMESTAMP 
                WHERE id = ?
                """,
                    (new_count, card_data["id"]),
                )
            else:
                # 新規登録
                cursor.execute(
                    """
                INSERT INTO knowledge_cards (
                    id, type, category, title, tags, ai_importance, 
                    humanity_importance, summary, episode, raw_reference
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        card_data["id"],
                        card_data.get("type", "Master"),
                        card_data["category"],
                        card_data["title"],
                        card_data.get("tags", ""),
                        card_data["ai_importance"],
                        card_data["humanity_importance"],
                        card_data["summary"],
                        card_data["episode"],
                        card_data["raw_reference"],
                    ),
                )
                self.refresh_card_content_hash(cursor, card_data["id"])

            # アクセスログの記録
            cursor.execute(
                "INSERT INTO access_logs (card_id, accessed_at) VALUES (?, ?)",
                (card_data["id"], datetime.now().date().isoformat()),
            )

            conn.commit()
            return True

    def get_cards(self, limit=50, offset=0, category=None, search=None):
        """カード一覧を取得する（embedding_blobを除く）"""
        with self._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            query = """
            SELECT id, type, category, title, tags, ai_importance, humanity_importance, updated_at, is_pinned, is_archived 
            FROM knowledge_cards 
            WHERE 1=1
            """
            params = []

            if category:
                query += " AND category = ?"
                params.append(category)

            if search:
                query += " AND (title LIKE ? OR summary LIKE ? OR tags LIKE ?)"
                search_term = f"%{search}%"
                params.extend([search_term, search_term, search_term])

            query += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_card(self, card_id):
        """カード詳細を取得する（embedding_blobを除く）"""
        with self._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute(
                """
            SELECT id, type, category, title, tags, ai_importance, humanity_importance, 
                   summary, episode, count, raw_reference, created_at, updated_at, is_pinned, is_archived 
            FROM knowledge_cards 
            WHERE id = ?
            """,
                (card_id,),
            )

            row = cursor.fetchone()
            return dict(row) if row else None

    def toggle_pin(self, card_id, pin_status: bool):
        """カードのピン留め状態を切り替える"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            status_int = 1 if pin_status else 0
            cursor.execute(
                """
            UPDATE knowledge_cards SET 
                is_pinned = ?, 
                updated_at = CURRENT_TIMESTAMP 
            WHERE id = ?
            """,
                (status_int, card_id),
            )

            if cursor.rowcount == 0:
                return False
            conn.commit()
            return True

    def toggle_archive(self, card_id, archive_status: bool):
        """カードのアーカイブ状態を切り替える"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            status_int = 1 if archive_status else 0
            cursor.execute(
                """
            UPDATE knowledge_cards SET 
                is_archived = ?, 
                updated_at = CURRENT_TIMESTAMP 
            WHERE id = ?
            """,
                (status_int, card_id),
            )

            if cursor.rowcount == 0:
                return False
            conn.commit()
            return True

    def update_card(self, card_id, data):
        """カード情報を更新する"""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # 更新可能なフィールド
            fields = [
                "category",
                "title",
                "tags",
                "ai_importance",
                "humanity_importance",
                "summary",
                "episode",
            ]
            set_clauses = []
            params = []

            for field in fields:
                if field in data:
                    set_clauses.append(f"{field} = ?")
                    params.append(data[field])

            if not set_clauses:
                return False

            set_clauses.append("updated_at = CURRENT_TIMESTAMP")

            query = f"UPDATE knowledge_cards SET {', '.join(set_clauses)} WHERE id = ?"
            params.append(card_id)

            cursor.execute(query, params)
            updated = cursor.rowcount > 0
            if updated:
                # 本文が変わった版は Stage 3 レビューキューへ再投入する（§5.2）
                self.refresh_card_content_hash(cursor, card_id)
            conn.commit()
            return updated

    def record_card_usage(self, card_ids: list, dedup_hours: int = 6) -> list:
        """RAG 経由で Brain に渡されたカードの usage_count をインクリメントする。

        同一カードが dedup_hours 以内に既にカウントされていればスキップする。
        Returns: 実際に usage_count を増やしたカードの id リスト。
        """
        card_ids = list(dict.fromkeys(card_ids))  # 入力側で重複除去
        if not card_ids:
            return []

        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat(timespec="seconds")
        threshold = (now_dt - timedelta(hours=dedup_hours)).isoformat(
            timespec="seconds"
        )

        placeholders = ",".join("?" for _ in card_ids)
        query = f"""
            UPDATE knowledge_cards
            SET usage_count = usage_count + 1,
                last_counted_at = ?
            WHERE id IN ({placeholders})
              AND (last_counted_at IS NULL OR last_counted_at < ?)
            RETURNING id
        """
        params = [now, *card_ids, threshold]

        with self._get_connection() as conn:
            cursor = conn.execute(query, params)
            updated_ids = [row[0] for row in cursor.fetchall()]
            conn.commit()

        print(f"[usage_count] incremented {len(updated_ids)}/{len(card_ids)} cards")
        return updated_ids

    def delete_card(self, card_id):
        """カードを削除する"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # access_logsも削除
            cursor.execute("DELETE FROM access_logs WHERE card_id = ?", (card_id,))
            cursor.execute("DELETE FROM knowledge_cards WHERE id = ?", (card_id,))
            conn.commit()
            return cursor.rowcount > 0
