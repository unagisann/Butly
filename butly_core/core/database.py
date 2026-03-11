import sqlite3
import os
from datetime import datetime

class ButlyDatabase:
    def __init__(self, db_path="butly_memory.db"):
        self.db_path = db_path
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
                episode TEXT,               -- ジャービスの所感
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
            
            # DB Migration: Add is_pinned and is_archived columns if not exist
            try:
                cursor.execute("ALTER TABLE knowledge_cards ADD COLUMN is_pinned INTEGER DEFAULT 0;")
            except sqlite3.OperationalError:
                pass
                
            try:
                cursor.execute("ALTER TABLE knowledge_cards ADD COLUMN is_archived INTEGER DEFAULT 0;")
            except sqlite3.OperationalError:
                pass

            conn.commit()

    def register_knowledge(self, card_data):
        """ナレッジカードをDBに登録または更新する"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # 既存のIDがあるか確認（統合ロジック）
            cursor.execute("SELECT count FROM knowledge_cards WHERE id = ?", (card_data['id'],))
            row = cursor.fetchone()
            
            if row:
                # 既存カードの更新（カウントアップ）
                new_count = row[0] + 1
                cursor.execute("""
                UPDATE knowledge_cards SET 
                    count = ?, 
                    updated_at = CURRENT_TIMESTAMP 
                WHERE id = ?
                """, (new_count, card_data['id']))
            else:
                # 新規登録
                cursor.execute("""
                INSERT INTO knowledge_cards (
                    id, type, category, title, tags, ai_importance, 
                    humanity_importance, summary, episode, raw_reference
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    card_data['id'], card_data.get('type', 'Master'), 
                    card_data['category'], card_data['title'], card_data.get('tags', ''),
                    card_data['ai_importance'], card_data['humanity_importance'],
                    card_data['summary'], card_data['episode'], card_data['raw_reference']
                ))
            
            # アクセスログの記録
            cursor.execute("INSERT INTO access_logs (card_id, accessed_at) VALUES (?, ?)", 
                           (card_data['id'], datetime.now().date().isoformat()))
            
            
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
            
            cursor.execute("""
            SELECT id, type, category, title, tags, ai_importance, humanity_importance, 
                   summary, episode, count, raw_reference, created_at, updated_at, is_pinned, is_archived 
            FROM knowledge_cards 
            WHERE id = ?
            """, (card_id,))
            
            row = cursor.fetchone()
            return dict(row) if row else None

    def toggle_pin(self, card_id, pin_status: bool):
        """カードのピン留め状態を切り替える"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            status_int = 1 if pin_status else 0
            cursor.execute("""
            UPDATE knowledge_cards SET 
                is_pinned = ?, 
                updated_at = CURRENT_TIMESTAMP 
            WHERE id = ?
            """, (status_int, card_id))
            
            if cursor.rowcount == 0:
                return False
            conn.commit()
            return True

    def toggle_archive(self, card_id, archive_status: bool):
        """カードのアーカイブ状態を切り替える"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            status_int = 1 if archive_status else 0
            cursor.execute("""
            UPDATE knowledge_cards SET 
                is_archived = ?, 
                updated_at = CURRENT_TIMESTAMP 
            WHERE id = ?
            """, (status_int, card_id))
            
            if cursor.rowcount == 0:
                return False
            conn.commit()
            return True

    def update_card(self, card_id, data):
        """カード情報を更新する"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # 更新可能なフィールド
            fields = ['category', 'title', 'tags', 'ai_importance', 'humanity_importance', 'summary', 'episode']
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
            conn.commit()
            return cursor.rowcount > 0

    def delete_card(self, card_id):
        """カードを削除する"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # access_logsも削除
            cursor.execute("DELETE FROM access_logs WHERE card_id = ?", (card_id,))
            cursor.execute("DELETE FROM knowledge_cards WHERE id = ?", (card_id,))
            conn.commit()
            return cursor.rowcount > 0