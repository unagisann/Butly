
import os
import json
import re
import sqlite3
import math
import numpy as np
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

# ★設定ファイルのインポート
try:
    from butly_core.config import AI_CONFIG, SYSTEM_CONFIG
    from butly_core import prompts
except ImportError:
    # パス解決のためのフォールバック (実行環境による)
    import sys
    sys.path.append(str(Path(__file__).resolve().parent.parent))
    from butly_core.config import AI_CONFIG, SYSTEM_CONFIG
    from butly_core import prompts

class ButlyBrain:
    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        # Configから読み込む (デフォルト値はConfig依存)
        self.model_name = AI_CONFIG["chat"]["model_name"]
        
        # db_path はインスタンスごとに _get_db_path() で解決するため、固定値を持たない
        self.instances_dir = self.base_dir / "butly_core" / "instances"
        self.db_name = SYSTEM_CONFIG["paths"]["db_name"]

    def _get_db_path(self, instance_name: str) -> Path:
        """インスタンス名からDBパスを解決"""
        return self.instances_dir / instance_name / self.db_name

    # --- Provider アクセス ---
    def _get_provider(self, model_name=None):
        """指定モデル名（またはデフォルト chat モデル）の Provider を取得する"""
        from butly_core.llm.factory import ProviderFactory
        return ProviderFactory.create(model_name or self.model_name)

    def _calculate_cosine_similarity(self, vec1, vec2):
        if vec1 is None or vec2 is None: return 0.0
        try:
            # numpy array conversion
            v1 = np.array(vec1)
            v2 = np.array(vec2)
            if v1.shape != v2.shape:
                print(f"[Brain] Dimension mismatch: {v1.shape} vs {v2.shape}. Skipping.")
                return 0.0
            
            norm1 = np.linalg.norm(v1)
            norm2 = np.linalg.norm(v2)
            
            if norm1 == 0 or norm2 == 0: return 0.0
            return float(np.dot(v1, v2) / (norm1 * norm2))
        except Exception as e:
            print(f"[Brain] Cosine Sim Error: {e}")
            return 0.0

    def get_embedding(self, text):
        try:
            model_name = AI_CONFIG["embedding"]["model_name"]
            provider = self._get_provider(model_name)
            return provider.embed(text)
        except Exception as e:
            print(f"[Brain] Embedding Error: {e}")
            return None

    def _merge_config(self, base_conf, override_conf):
        """Deep merge config dictionaries"""
        if not override_conf:
            return base_conf
        
        merged = base_conf.copy()
        for key, value in override_conf.items():
            if isinstance(value, dict) and key in merged and isinstance(merged[key], dict):
                merged[key] = self._merge_config(merged[key], value)
            else:
                merged[key] = value
        return merged

    def extract_keywords(self, user_input, override_config=None):
        """ユーザー発言からDB検索用のキーワードを抽出"""
        try:
            chat_conf = AI_CONFIG["chat"]
            if override_config and "chat" in override_config:
                chat_conf = self._merge_config(chat_conf, override_config["chat"])

            from butly_core.prompts import PromptLoader
            loader = PromptLoader()
            prompt = loader.get("brain_extract_keywords", user_input=user_input)
            
            provider = self._get_provider(chat_conf["model_name"])
            text = provider.classify(prompt, chat_conf)
            text = text.strip() if text else ""
            print(f"[Brain] Raw Keyword Response: {text}")
            
            match = re.search(r"```(?:json)?(.*?)```", text, re.DOTALL)
            if match: text = match.group(1).strip()
            return json.loads(text)
        except Exception as e:
            print(f"[Brain] Keyword Extraction Error: {e}")
            return {"keywords": []}

    def search_knowledge(self, keywords, user_query, instance_name="00_master", limit=None, override_config=None):
        """
        ハイブリッド検索: キーワードフィルター + ベクトル類似度
        readable_instances に基づき複数DB横断検索に対応。
        """
        brain_conf = SYSTEM_CONFIG["brain"].copy()
        if override_config and "brain" in override_config:
             brain_conf.update(override_config["brain"])

        if limit is None:
            limit = brain_conf["search_limit"]

        # readable_instances の解決
        readable = brain_conf.get("readable_instances", ["self"])
        target_instances = []
        for r in readable:
            if r == "self":
                target_instances.append(instance_name)
            else:
                target_instances.append(r)
        # 重複除去（selfと明示指定が被る場合）
        target_instances = list(dict.fromkeys(target_instances))

        # 単一DBの場合（高速パス）
        if len(target_instances) == 1:
            return self._search_single_db(
                keywords, user_query, target_instances[0], limit, brain_conf
            )

        # 複数DBの場合: 各DBに個別クエリ → マージしてリランキング
        return self._search_multi_db(
            keywords, user_query, target_instances, limit, brain_conf
        )

    def _search_multi_db(self, keywords, user_query, instances, limit, brain_conf):
        """複数インスタンスのDBを横断検索"""
        all_results = []
        for inst in instances:
            db_path = self._get_db_path(inst)
            if not db_path.exists():
                continue
            results = self._search_single_db(
                keywords, user_query, inst, limit, brain_conf
            )
            for r in results:
                r["source_instance"] = inst
            all_results.extend(results)

        # スコアでリランキング → 上位 limit 件
        all_results.sort(key=lambda x: x.get("score", 0), reverse=True)
        return all_results[:limit]

    def _search_single_db(self, keywords, user_query, instance_name, limit, brain_conf):
        """
        単一インスタンスDBに対するハイブリッド検索
        (キーワードフィルター + ベクトル類似度リランキング)
        """
        instance_db_path = self._get_db_path(instance_name)
        if not instance_db_path.exists(): return []

        try:
            conn = sqlite3.connect(instance_db_path)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # ---------------------------------------------------------
            # 1. Broad Keyword Search (Filter)
            # ---------------------------------------------------------
            rows = []
            
            # 共通カラム定義: embedding -> embedding_blob
            select_cols = "id, title, summary, episode, type, embedding_blob, created_at, is_archived"
            
            # Keyword Filter
            kw_conditions = []
            kw_params = []
            if keywords:
                for k in keywords:
                    kw_conditions.append("(title LIKE ? OR summary LIKE ?)")
                    kw_params.extend([f"%{k}%", f"%{k}%"])
            
            # --- Primary Search (Keyword Filter) ---
            if kw_conditions:
                where_sql = f"({' OR '.join(kw_conditions)})"
                
                # Fetch more candidates for reranking
                fetch_limit = brain_conf["fallback_fetch_limit"]
                query = f"""
                    SELECT {select_cols}
                    FROM knowledge_cards 
                    WHERE {where_sql}
                    ORDER BY created_at DESC
                    LIMIT {fetch_limit}
                """
                cursor.execute(query, kw_params)
                rows = cursor.fetchall()

            # --- Fallback Logic ---
            # キーワード検索結果が少なすぎる場合、直近のデータを無条件で取得して対象にする
            KEYWORD_HIT_THRESHOLD = brain_conf["keyword_hit_threshold"]
            
            if len(rows) < KEYWORD_HIT_THRESHOLD:
                # フォールバック: 直近50件 (キーワード条件なし)
                print(f"[Brain] Fallback triggered (Hits: {len(rows)}). Fetching recent logs.")
                
                fetch_limit = brain_conf["fallback_fetch_limit"]
                query_fb = f"""
                    SELECT {select_cols}
                    FROM knowledge_cards 
                    ORDER BY created_at DESC
                    LIMIT {fetch_limit}
                """
                cursor.execute(query_fb)
                fallback_rows = cursor.fetchall()
                
                # 重複排除しながらマージ (IDをキーにする)
                existing_ids = {row['id'] for row in rows}
                for fb_row in fallback_rows:
                    if fb_row['id'] not in existing_ids:
                        rows.append(fb_row)

            conn.close()
            
            if not rows: return []

            # ---------------------------------------------------------
            # 2. Vector Reranking (BLOB / Float32)
            # ---------------------------------------------------------
            query_embedding = self.get_embedding(user_query)
            if not query_embedding:
                # ベクトル生成失敗時はそのまま返す(上位limit件)
                return [dict(row) for row in rows[:limit]]
            
            scored_results = []
            
            # --- Scoring Modifiers ---
            decay_rate = brain_conf.get("time_decay_rate", 0.005) # 0.005 means half-life of ~138 days
            now = datetime.now()
            
            for row in rows:
                row_dict = dict(row)
                blob_data = row_dict.pop('embedding_blob') # Remove from result dict
                created_at_str = row_dict.get('created_at')
                is_archived = row_dict.get('is_archived') or 0
                
                score = 0.0
                if blob_data:
                    try:
                        # BLOB -> NumPy (float32)
                        db_emb = np.frombuffer(blob_data, dtype=np.float32)
                        score = float(self._calculate_cosine_similarity(query_embedding, db_emb))
                        
                        # Apply Time Decay
                        if created_at_str:
                            try:
                                # SQLite DEFAULT CURRENT_TIMESTAMP format is 'YYYY-MM-DD HH:MM:SS'
                                created_at_dt = datetime.strptime(created_at_str, "%Y-%m-%d %H:%M:%S")
                                days_diff = (now - created_at_dt).days
                                if days_diff > 0:
                                    decay_factor = math.exp(-decay_rate * days_diff)
                                    score *= decay_factor
                            except ValueError:
                                pass # Ignore parse errors
                        
                        # Apply Archive Penalty
                        if is_archived:
                            score *= 0.5
                            
                    except Exception as e: 
                        pass
                
                row_dict['score'] = float(score)  # Ensure it is a float
                scored_results.append(row_dict)
            
            # Sort by score descending
            scored_results.sort(key=lambda x: x['score'], reverse=True)
            
            return scored_results[:limit]

        except Exception as e:
            print(f"[Brain] Search Error: {e}")
            return []
