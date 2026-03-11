
import os
import json
import re
import sqlite3
import time
import numpy as np
from pathlib import Path
from datetime import timedelta, datetime
from dotenv import load_dotenv
from google import genai
from google.genai import types

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
        self._configure_api()
        
        # インスタンスごとのDBを参照（全インスタンス共通DBはinstances/00_master/butly_memory.db）
        db_name = SYSTEM_CONFIG["paths"]["db_name"]
        # ルート直下ではなく、現在のインスタンスのDB（デフォルト: 00_master）を参照する
        # app.py から instance_name を渡していないため、共通DBの00_masterを使用する
        self.db_path = self.base_dir / "butly_core" / "instances" / "00_master" / db_name

    def _configure_api(self):
        env_files = ["APIkey.env", ".env"]
        api_key = None
        for f in env_files:
            env_path = self.base_dir / f
            if env_path.exists():
                load_dotenv(dotenv_path=env_path, override=True)
                api_key = os.getenv("GOOGLE_API_KEY")
                if api_key: break
        
        if not api_key:
            api_key = os.getenv("GOOGLE_API_KEY")
        
        if not api_key:
            raise ValueError("[Brain] API Keyが見つかりません。")
        
        # Initialize Google GenAI Client
        self.client = genai.Client(api_key=api_key)

    def _calculate_cosine_similarity(self, vec1, vec2):
        if vec1 is None or vec2 is None: return 0.0
        try:
            # numpy array conversion
            v1 = np.array(vec1)
            v2 = np.array(vec2)
            if v1.shape != v2.shape: return 0.0
            
            norm1 = np.linalg.norm(v1)
            norm2 = np.linalg.norm(v2)
            
            if norm1 == 0 or norm2 == 0: return 0.0
            return float(np.dot(v1, v2) / (norm1 * norm2))
        except Exception as e:
            print(f"[Brain] Cosine Sim Error: {e}")
            return 0.0

    def get_embedding(self, text):
        try:
            # Configからモデル名を取得
            model_name = AI_CONFIG["embedding"]["model_name"]
            # Using new client
            result = self.client.models.embed_content(
                model=model_name,
                contents=text,
                config=types.EmbedContentConfig(task_type="RETRIEVAL_QUERY")
            )
            # Accessing embedding attributes directly or via dict depending on obj
            # google-genai returns an object, usually .embeddings[0].values
            if result.embeddings:
                return result.embeddings[0].values
            return None
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
            # ConfigからChatモデル設定を使用 (Intelligence重視)
            chat_conf = AI_CONFIG["chat"]
            if override_config and "chat" in override_config:
                chat_conf = self._merge_config(chat_conf, override_config["chat"])

            prompt = prompts.BRAIN_EXTRACT_KEYWORDS_PROMPT.format(user_input=user_input)
            
            # Use synchronous generation for keyword extraction (internal utility)
            response = self.client.models.generate_content(
                model=chat_conf["model_name"],
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=chat_conf["generation_config"].get("temperature"),
                    top_p=chat_conf["generation_config"].get("top_p"),
                    top_k=chat_conf["generation_config"].get("top_k"),
                    max_output_tokens=chat_conf["generation_config"].get("max_output_tokens"),
                    safety_settings=chat_conf.get("safety_settings") # List of dicts works with new library
                )
            )
            # Response handling
            text = response.text.strip() if response.text else ""
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
        (BLOB対応 & フォールバック機能付き)
        """
        brain_conf = SYSTEM_CONFIG["brain"].copy()
        if override_config and "brain" in override_config:
             brain_conf.update(override_config["brain"])

        if limit is None:
            limit = brain_conf["search_limit"]

        # ルートの共通DBパスを取得するように変更
        instance_db_path = self.db_path
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
            
            # Instance Filter
            base_conditions = []
            params = []
            # override_config がマージ済みの brain_conf を参照する（SYSTEM_CONFIG 直参照バグを修正）
            if brain_conf.get("filter_memory_by_type", True):
                # "Master" への変換は廃止。instance_name をそのままDBの type として使用する
                base_conditions.append("type = ?")
                params.append(instance_name)
            
            # Keyword Filter
            kw_conditions = []
            kw_params = []
            if keywords:
                for k in keywords:
                    kw_conditions.append("(title LIKE ? OR summary LIKE ?)")
                    kw_params.extend([f"%{k}%", f"%{k}%"])
            
            # --- Primary Search (Keyword Filter) ---
            if kw_conditions:
                where_clauses = base_conditions + [f"({' OR '.join(kw_conditions)})"]
                where_sql = " AND ".join(where_clauses)
                current_params = params + kw_params
                
                # Fetch more candidates for reranking
                fetch_limit = brain_conf["fallback_fetch_limit"]
                query = f"""
                    SELECT {select_cols}
                    FROM knowledge_cards 
                    WHERE {where_sql}
                    ORDER BY created_at DESC
                    LIMIT {fetch_limit}
                """
                cursor.execute(query, current_params)
                rows = cursor.fetchall()

            # --- Fallback Logic ---
            # キーワード検索結果が少なすぎる場合、直近のデータを無条件で取得して対象にする
            KEYWORD_HIT_THRESHOLD = brain_conf["keyword_hit_threshold"]
            
            if len(rows) < KEYWORD_HIT_THRESHOLD:
                # フォールバック: 直近50件 (キーワード条件なし)
                print(f"[Brain] Fallback triggered (Hits: {len(rows)}). Fetching recent logs.")
                
                where_sql_fb = " AND ".join(base_conditions) if base_conditions else "1=1"
                fetch_limit = brain_conf["fallback_fetch_limit"]
                query_fb = f"""
                    SELECT {select_cols}
                    FROM knowledge_cards 
                    WHERE {where_sql_fb}
                    ORDER BY created_at DESC
                    LIMIT {fetch_limit}
                """
                cursor.execute(query_fb, params) # params contains instance filter only
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
            
            import math
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

    def _filter_hallucinations(self, text: str) -> str:
        """Gemini 3 Preview等が誤って出力する内部の注釈テキストを除外する"""
        if not text:
            return text
        
        # 不要な注釈のパターン
        unwanted_patterns = [
            r"No citation needed( as it's a personal response)?\.?",
            r"\[No citation needed\]",
            r"Citation not needed\.?"
        ]
        
        filtered_text = text
        for pattern in unwanted_patterns:
            filtered_text = re.sub(pattern, "", filtered_text, flags=re.IGNORECASE)
            
        return filtered_text.strip()

    def _extract_response(self, response):
        """レスポンスからテキストとソースを抽出するヘルパー"""
        sources = []
        response_text = ""
        finish_reason = None
        
        # finish_reasonの取得
        if response.candidates:
            finish_reason = response.candidates[0].finish_reason
        
        # --- Grounding Metadata (Sources) の抽出 ---
        try:
            if response.candidates and response.candidates[0].grounding_metadata:
                metadata = response.candidates[0].grounding_metadata
                if metadata.grounding_chunks:
                    for chunk in metadata.grounding_chunks:
                        if chunk.web:
                            sources.append({
                                "title": chunk.web.title or "Google Search",
                                "url": chunk.web.uri
                            })
        except Exception as e:
            print(f"[Brain] Grounding Extraction Error: {e}")

        # --- テキスト抽出 ---
        try:
            if response.text:
                response_text = response.text
        except (ValueError, AttributeError):
            pass
        
        if not response_text and response.candidates:
            try:
                for part in response.candidates[0].content.parts:
                    if hasattr(part, 'text') and part.text:
                        response_text += part.text
            except (AttributeError, IndexError):
                pass
                
        response_text = self._filter_hallucinations(response_text)

        return response_text, sources, finish_reason

    async def generate_response_with_rag(self, user_input, memory_manager, history, cached_content=None, override_config=None, use_google_search=False, images=None, memory_blocks=None):
        """Web UIから呼ばれるメインメソッド (Async)

        memory_blocks が指定された場合は Gatekeeper の tier 別記憶ブロックを使用する。
        Google検索有効時は3段階のリトライ/フォールバック:
          1. First Try: 通常の検索付きリクエスト
          2. Retry 1: MALFORMED_FUNCTION_CALL時、修正指示を追加して再試行
          3. Fallback: それでもダメなら検索なしで回答 + エラー注釈
        """
        current_instance = memory_manager.instance_dir.name
        tier = memory_blocks.get("tier", "mid") if memory_blocks else "mid"
        
        # 1. キーワード抽出 & RAG検索
        # cortex 時は memory_blocks 内に RAG 結果を含むため、再度検索しない
        keyword_data = self.extract_keywords(user_input, override_config)
        keywords = keyword_data.get("keywords", [])

        if memory_blocks and tier == "cortex" and memory_blocks.get("rag_context"):
            # cortex: Gatekeeper が構築済みの RAG 結果を流用
            knowledge_list = []  # generate_response_with_rag の戻り値用リストは空にする
            rag_context = memory_blocks["rag_context"]
            print(f"[Brain] RAG: cortex モード，Gatekeeper 済み RAG を流用")
        else:
            limit = SYSTEM_CONFIG["brain"]["search_limit"]
            knowledge_list = self.search_knowledge(
                keywords,
                user_input,
                instance_name=current_instance,
                limit=limit,
                override_config=override_config
            )
            rag_context = ""
            print(f"[Brain] RAG Search: keywords={keywords}, hits={len(knowledge_list)}, use_google_search={use_google_search}")
            if knowledge_list:
                rag_context = "\n\n[過去の記憶]\n"
                for k in knowledge_list:
                    rag_context += f"・{k['title']}: {k['summary']}\n"
                    if k['episode']:
                        rag_context += f"  (補足: {k['episode']})\n"
                rag_context += "\n"

        full_prompt = f"{rag_context}\nユーザー: {user_input}"
        if use_google_search and rag_context:
            full_prompt += "\n\n(Note: Prioritize the local database information above. Use Google Search only if the database lacks sufficient information or for checking the latest facts.)"

        
        # 3. チャット実行 (リトライ/フォールバック付き)
        try:
            if use_google_search:
                response_text, sources = await self._try_search_with_retry(
                    full_prompt, cached_content, history, memory_manager,
                    override_config, images=images, memory_blocks=memory_blocks
                )
            else:
                # 検索無効時: 通常のチャット
                chat_session = await self.start_chat(
                    cached_content=cached_content,
                    history=history,
                    memory_manager=memory_manager,
                    override_config=override_config,
                    use_google_search=False,
                    memory_blocks=memory_blocks,
                )
                
                prompt_parts = [full_prompt]
                if images:
                    import base64
                    for b64 in images:
                        try:
                            img_bytes = base64.b64decode(b64)
                            part = types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")
                            prompt_parts.append(part)
                        except Exception as e:
                            print(f"[Brain] Image decode error: {e}")
                            
                response = await chat_session.send_message(prompt_parts)
                response_text, sources, _ = self._extract_response(response)
            
            return response_text, keywords, knowledge_list, sources
        except Exception as e:
            import traceback
            traceback.print_exc()
            return f"Error: {e}", keywords, knowledge_list, []

    async def _try_search_with_retry(self, full_prompt, cached_content, history, memory_manager, override_config, images=None, memory_blocks=None):
        """Google検索付きリクエストの3段階リトライ/フォールバック
        
        1. First Try: 通常の検索付きリクエスト
        2. Retry 1: MALFORMED_FUNCTION_CALL時、修正指示を追加して再試行
        3. Fallback: それでもダメなら検索なしで回答 + エラー注釈
        """
        
        # =====================================================================
        # First Try: 通常の検索付きリクエスト
        # =====================================================================
        print("[Brain] Search: First Try (normal search)")
        try:
            chat_session = await self.start_chat(
                cached_content=cached_content,
                history=history,
                memory_manager=memory_manager,
                override_config=override_config,
                use_google_search=True,
                memory_blocks=memory_blocks,
            )
            
            prompt_parts = [full_prompt]
            if images:
                import base64
                for b64 in images:
                    try:
                        img_bytes = base64.b64decode(b64)
                        part = types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")
                        prompt_parts.append(part)
                    except: pass
                    
            response = await chat_session.send_message(prompt_parts)
            response_text, sources, finish_reason = self._extract_response(response)
            
            # MALFORMED_FUNCTION_CALL チェック
            if finish_reason and "MALFORMED" in str(finish_reason):
                print(f"[Brain] Search: First Try failed - {finish_reason}")
            elif response_text:
                return response_text, sources
            else:
                print("[Brain] Search: First Try returned empty response")
        except Exception as e:
            print(f"[Brain] Search: First Try exception - {e}")
        
        # =====================================================================
        # Retry 1: 修正指示を追加して再試行
        # =====================================================================
        print("[Brain] Search: Retry 1 (with corrective instruction)")
        try:
            corrected_prompt = full_prompt + "\n\n【システム注意】前回のGoogle検索ツール呼び出しが不正な形式でした。google_searchツールは自動的に実行されます。関数を明示的に呼び出す必要はありません。通常通り回答してください。"
            
            chat_session = await self.start_chat(
                cached_content=cached_content,
                history=history,
                memory_manager=memory_manager,
                override_config=override_config,
                use_google_search=True,
                memory_blocks=memory_blocks,
            )
            
            prompt_parts = [corrected_prompt]
            if images:
                import base64
                for b64 in images:
                    try:
                        img_bytes = base64.b64decode(b64)
                        part = types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")
                        prompt_parts.append(part)
                    except: pass
                    
            response = await chat_session.send_message(prompt_parts)
            response_text, sources, finish_reason = self._extract_response(response)
            
            if finish_reason and "MALFORMED" in str(finish_reason):
                print(f"[Brain] Search: Retry 1 also failed - {finish_reason}")
            elif response_text:
                return response_text, sources
            else:
                print("[Brain] Search: Retry 1 returned empty response")
        except Exception as e:
            print(f"[Brain] Search: Retry 1 exception - {e}")
        
        # =====================================================================
        # Fallback: 検索なしで回答 + エラー注釈
        # =====================================================================
        print("[Brain] Search: Fallback (without search)")
        chat_session = await self.start_chat(
            cached_content=cached_content,
            history=history,
            memory_manager=memory_manager,
            override_config=override_config,
            use_google_search=False,   # 検索完全無効
            memory_blocks=memory_blocks,
        )
        
        prompt_parts = [full_prompt]
        if images:
            import base64
            for b64 in images:
                try:
                    img_bytes = base64.b64decode(b64)
                    part = types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")
                    prompt_parts.append(part)
                except: pass
                
        response = await chat_session.send_message(prompt_parts)
        response_text, sources, _ = self._extract_response(response)
        
        # エラー注釈を先頭に追加
        if response_text:
            response_text = "【検索エラー】外部情報にアクセスできませんでした。以下は内部知識に基づく回答です。\n\n" + response_text
        else:
            response_text = "【検索エラー】外部情報にアクセスできませんでした。回答を生成できませんでした。"
        
        return response_text, sources

    # =========================================================================
    #  記憶管理・キャッシュ制御
    # =========================================================================

    def prepare_cache(self, memory_manager, ttl_hours=None, override_config=None):
        """
        中期記憶をキャッシュ化する。(New API)
        """
        if ttl_hours is None:
            ttl_hours = SYSTEM_CONFIG["brain"]["cache_ttl_hours"]

        use_cache = True
        if override_config and "brain" in override_config and "use_context_cache" in override_config["brain"]:
             use_cache = override_config["brain"]["use_context_cache"]
        else:
             use_cache = SYSTEM_CONFIG["brain"].get("use_context_cache", True)

        if not use_cache:
            # print("[Brain] Context Caching is DISABLED in config.")
            return None

        system_instruction = memory_manager.get_system_instruction()
        mid_term_text = memory_manager.get_mid_term_text_content()
        combined_context = f"【中期記憶（過去の重要な記録）】\n{mid_term_text}"
        
        cache_id_file = memory_manager.instance_dir / "current_cache_id.txt"
        
        # 1. Update existing cache
        if cache_id_file.exists():
            cached_name = cache_id_file.read_text(encoding="utf-8").strip()
            if cached_name:
                try:
                    # Sync util for cache operations usually fine, or use self.client.caches
                    # self.client.caches.get(name=...)
                    # We can stick to sync calls here as it's background/prep task
                    cache = self.client.caches.get(name=cached_name)
                    # Update TTL: The new library might have .update method?
                    # client.caches.update(name=..., config=...)
                    # Check docs/types. But usually new cache with same content re-uses it or explicit update.
                    # For now, let's try update if available or ignore validation.
                    # Actually, let's assume create new one if update semantics are complex, or just return existing.
                    return cache
                except:
                    pass

        # 2. Create new cache
        try:
            if len(combined_context) < 1000:
                return None

            # types.Content(parts=[types.Part(text=...)])
            contents = [types.Content(parts=[types.Part(text=combined_context)])]
            
            # Use sync client for cache creation (utility)
            new_cache = self.client.caches.create(
                model=self.model_name,
                config=types.CreateCachedContentConfig(
                    system_instruction=types.Content(parts=[types.Part(text=system_instruction)]),
                    contents=contents,
                    ttl=f"{int(ttl_hours * 3600)}s" # TTL as duration string for new API? Or seconds?
                    # API expects duration string e.g. "3600s" often
                )
            )
            cache_id_file.write_text(new_cache.name, encoding="utf-8")
            return new_cache
            
        except Exception as e:
            if "too small" in str(e) or "400" in str(e):
                return None
            else:
                print(f"[Brain] Cache Error: {e}")
                return None

    async def start_chat(self, cached_content=None, history=[], memory_manager=None, override_config=None, use_google_search=False, memory_blocks=None):
        """
        キャッシュ有無に関わらず、浮動要約(Floating)を注入してチャットを開始する
        """
        floating_summary = ""
        if memory_manager:
            raw_summary = memory_manager.get_floating_summary()
            if raw_summary:
                floating_summary = f"\n\n========================================\n【直近の未整理記憶（浮動要約）】\n{raw_summary}"

        # Config Reading
        chat_conf = AI_CONFIG["chat"]
        if override_config and "chat" in override_config:
            chat_conf = self._merge_config(chat_conf, override_config["chat"])

        # History structure conversion if needed
        # Google GenAI library history format usually: [types.Content(role=, parts=[...])]
        # Our `history` input is likely list of dicts/Content objects. We need to ensure compatibility.
        history_contents = []

        for h in history:
            # transform dict to types.Content if necessary
            if isinstance(h, dict):
                 role = h.get("role")
                 parts = [types.Part(text=p) if isinstance(p, str) else types.Part(text=str(p)) for p in h.get("parts", [])]
                 history_contents.append(types.Content(role=role, parts=parts))
            else:
                 history_contents.append(h) # Assume it's already compatible object

        # Tools Configuration
        tools_config = None
        if use_google_search:
            # Gemini 2.0/3.0 uses 'google_search' tool.
            # Dynamic retrieval config is removed as requested.
            tools_config = [
                {"google_search": {}}
            ]

        # Config Object
        generate_config = types.GenerateContentConfig(
            temperature=chat_conf["generation_config"].get("temperature"),
            top_p=chat_conf["generation_config"].get("top_p"),
            top_k=chat_conf["generation_config"].get("top_k"),
            max_output_tokens=chat_conf["generation_config"].get("max_output_tokens"),
            safety_settings=chat_conf.get("safety_settings"),
            tools=tools_config,
            system_instruction=None # Set below
        )

        model_name = chat_conf["model_name"]
        
        if cached_content:
            # Use cached content name as model? Or pass cached_content object?
            # In new library: model='cachedContentName' ?
            # Or pass `cached_content` arg to chats.create?
            # Usually we use the cached content name as the model identifier if supported, or pass via config.
            # Docs: client.chats.create(model=..., config=...)
            # We might need to rely on the cache Name.
            # But normally we create the chat against the base model, and include the cache context?
            # Actually, `cached_content` is usually passed in GenerateContentConfig or similar?
            # The library has `cached_content` argument in `models.generate_content`.
            # For chats, we might need to rely on system instruction or context injection if cache isn't directly supported in Chats API yet.
            # BUT, usually CachedContent acts as a model resource.
            # Let's try passing the cache name as 'model' if strict, OR pass `cached_content` in config?
            # Let's assume we pass it in config for now if possible: cached_content=...
            # The type definition suggests 'cached_content' field in GenerateContentConfig.
            generate_config.cached_content = cached_content.name
            
        else:
            # ===========================================================
            # 記憶の構造化: Gatekeeper が構築した memory_blocks を優先使用
            # なければ従来通り全記憶を注入（後方互換）
            # ===========================================================
            if memory_blocks is not None:
                # --- Gatekeeper 経由: tier 別記憶ブロックを使用 ---
                from butly_core.core.gatekeeper import build_system_instruction_from_blocks
                base_instruction = build_system_instruction_from_blocks(
                    blocks=memory_blocks,
                    memory_manager=memory_manager,
                    use_google_search=use_google_search,
                )
            else:
                # --- フォールバック: 全記憶を注入（従来動作）---
                sections = []

                # 1. SYSTEM INSTRUCTION (性格設定)
                agent_name = SYSTEM_CONFIG["agent"]["agent_name"]
                sys_inst = memory_manager.get_system_instruction() if memory_manager else f"あなたは{agent_name}です。"
                sections.append(f"=== SYSTEM INSTRUCTION ===\n{sys_inst}")

                # 2. KEY MEMORY (根幹記憶)
                if memory_manager:
                    key_mem = memory_manager.get_key_memory()
                    if key_mem:
                        sections.append(f"=== KEY MEMORY (根幹記憶) ===\n{key_mem}")

                # 2.5 MID TERM MEMORY (中期記憶)
                if memory_manager:
                    mid_term = memory_manager.get_mid_term_text_content()
                    if mid_term:
                        sections.append(f"=== MID-TERM MEMORY (中期記憶) ===\n{mid_term}")

                # 3. CURRENT TIME (現在時刻)
                from butly_core.core.chronos import ButlyChronos
                current_time = ButlyChronos().get_system_note()
                sections.append(f"=== CURRENT TIME (現在時刻) ===\n{current_time}\n※上記の時刻情報は文脈理解のための参考情報です。随時日付や時間を読み上げる必要はありません。あくまで自然な対話を最優先してください。")

                # 4. FLOATING SUMMARY (未整理記憶)
                if floating_summary:
                    sections.append(f"=== FLOATING SUMMARY (未整理記憶) ===\n{floating_summary.strip()}")

                if not use_google_search:
                    sections.append("")

                base_instruction = "\n\n".join(sections)

            generate_config.system_instruction = base_instruction

            # 5. SHORT-TERM (最近の会話) は history として渡す
            chat = self.client.aio.chats.create(
                model=model_name,
                config=generate_config,
                history=history_contents
            )

        return chat

    def summarize_conversation(self, conversation_text, override_config=None):
        """
        あふれた会話ログを要約する
        """
        try:
            summary_conf = AI_CONFIG["summary"]
            
            # Config Override
            brain_conf = SYSTEM_CONFIG["brain"].copy()
            if override_config and "brain" in override_config:
                brain_conf.update(override_config["brain"])
            
            char_limit = brain_conf["summary_char_limit"]
            
            prompt = prompts.BRAIN_SUMMARIZE_CONVERSATION_PROMPT.format(
                agent_name=SYSTEM_CONFIG.get("agent", {}).get("agent_name", "ジャービス"),
                char_limit=char_limit,
                conversation_text=conversation_text
            )
            
            response = self.client.models.generate_content(
                model=summary_conf["model_name"],
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=summary_conf["generation_config"].get("temperature"),
                    safety_settings=summary_conf.get("safety_settings")
                )
            )
            return response.text.strip() if response.text else "要約なし"
        except Exception as e:
            print(f"[Brain] Summarize Error: {e}")
            return "（要約作成に失敗）"

    def chat_with_interactions(
        self,
        user_input: str,
        memory_manager=None,
        override_config: dict = None,
        use_google_search: bool = False,
        previous_interaction_id: str = None,
        memory_blocks: dict = None,
    ):
        """
        Interactions API （ステートフルセッション）を使って1ターン応答を生成する。
        - previous_interaction_id が指定された場合: 既存の会話を継続
        - None の場合: 新しい会話を開始
        - system_instruction / tools は仕様上毎ターン再指定が必要
        - memory_blocks が渡された場合: Gatekeeper による tier 別記憶ブロックを使用

        Returns:
            (response_text: str, interaction_id: str, sources: list)
        """
        # Config Override
        chat_conf = AI_CONFIG["chat"].copy()
        if override_config:
            if "AI_CONFIG" in override_config and "chat" in override_config["AI_CONFIG"]:
                chat_conf.update(override_config["AI_CONFIG"]["chat"])
            elif "chat" in override_config:
                chat_conf.update(override_config["chat"])

        model_name = chat_conf["model_name"]

        # ----- system_instruction を毎ターン構築 -----
        if memory_blocks is not None:
            # Gatekeeper 経由: tier 別記憶ブロックを使用
            from butly_core.core.gatekeeper import build_system_instruction_from_blocks
            system_instruction = build_system_instruction_from_blocks(
                blocks=memory_blocks,
                memory_manager=memory_manager,
                use_google_search=use_google_search,
            )
        else:
            # フォールバック: 全記憶を注入（従来動作）
            sections = []

            # 1. SYSTEM INSTRUCTION (性格設定)
            agent_name = SYSTEM_CONFIG.get("agent", {}).get("agent_name", "ジャービス")
            sys_inst = memory_manager.get_system_instruction() if memory_manager else f"あなたは{agent_name}です。"
            sections.append(f"=== SYSTEM INSTRUCTION ===\n{sys_inst}")

            # 2. KEY MEMORY (根幹記憶)
            if memory_manager:
                key_mem = memory_manager.get_key_memory()
                if key_mem:
                    sections.append(f"=== KEY MEMORY (根幹記憶) ===\n{key_mem}")

            # 3. MID-TERM MEMORY (中期記憶)
            if memory_manager:
                mid_term = memory_manager.get_mid_term_text_content()
                if mid_term:
                    sections.append(f"=== MID-TERM MEMORY (中期記憶) ===\n{mid_term}")

            # 4. CURRENT TIME (現在時刻)
            from butly_core.core.chronos import ButlyChronos
            current_time = ButlyChronos().get_system_note()
            sections.append(f"=== CURRENT TIME (現在時刻) ===\n{current_time}\n※上記の時刻情報は文脈理解のための参考情報です。随時日付や時間を読み上げる必要はありません。あくまで自然な対話を最優先してください。")

            # 5. FLOATING SUMMARY (未整理記憶)
            if memory_manager:
                floating_summary = memory_manager.get_floating_summary()
                if floating_summary:
                    sections.append(f"=== FLOATING SUMMARY (未整理記憶) ===\n{floating_summary.strip()}")

            system_instruction = "\n\n".join(sections)

        # ----- Tools -----
        tools_config = None
        if use_google_search:
            tools_config = [{"google_search": {}}]

        # ----- 呼び出し -----
        try:
            call_kwargs = {
                "model": model_name,
                "input": user_input,
                "generation_config": types.GenerateContentConfig(
                    temperature=chat_conf["generation_config"].get("temperature"),
                    top_p=chat_conf["generation_config"].get("top_p"),
                    top_k=chat_conf["generation_config"].get("top_k"),
                    max_output_tokens=chat_conf["generation_config"].get("max_output_tokens"),
                    safety_settings=chat_conf.get("safety_settings")
                ),
                "system_instruction": system_instruction,
                "tools": tools_config,
            }
            if previous_interaction_id:
                call_kwargs["previous_interaction_id"] = previous_interaction_id

            interaction = self.client.interactions.create(**call_kwargs)

            # テキスト取得
            response_text = ""
            if interaction.outputs:
                last_output = interaction.outputs[-1]
                if hasattr(last_output, "text") and last_output.text:
                    response_text = self._filter_hallucinations(last_output.text)

            # ----- グラウンディングメタデータ（参照URL）の抽出 -----
            sources = []
            if use_google_search:
                try:
                    # interaction.outputs 内の各outputsからgroundingMetadataを探索
                    for output in (interaction.outputs or []):
                        metadata = getattr(output, "grounding_metadata", None)
                        if metadata and hasattr(metadata, "grounding_chunks"):
                            for chunk in (metadata.grounding_chunks or []):
                                web = getattr(chunk, "web", None)
                                if web:
                                    sources.append({
                                        "title": getattr(web, "title", "Google検索結果") or "Google検索結果",
                                        "url": getattr(web, "uri", "")
                                    })
                        # candidates形式からのフォールバック
                        if not sources:
                            candidates = getattr(output, "candidates", None)
                            if candidates:
                                for candidate in candidates:
                                    gm = getattr(candidate, "grounding_metadata", None)
                                    if gm and hasattr(gm, "grounding_chunks"):
                                        for chunk in (gm.grounding_chunks or []):
                                            web = getattr(chunk, "web", None)
                                            if web:
                                                sources.append({
                                                    "title": getattr(web, "title", "Google検索結果") or "Google検索結果",
                                                    "url": getattr(web, "uri", "")
                                                })
                except Exception as e:
                    print(f"[Brain] Grounding metadata extraction error: {e}")

            return response_text, interaction.id, sources

        except Exception as e:
            print(f"[Brain] chat_with_interactions error: {e}")
            raise
