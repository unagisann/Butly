
import json
import os
import shutil
import sqlite3
import re
import time
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv
import numpy as np
from google import genai
from google.genai import types

# ★設定ファイルのインポート
try:
    from butly_core.config import AI_CONFIG, SYSTEM_CONFIG
    from butly_core import prompts
    from butly_core.core.database import ButlyDatabase
except ImportError:
    # 実行環境によりパスが通らない場合の保険
    import sys
    sys.path.append(str(Path(__file__).resolve().parent))
    from butly_core.config import AI_CONFIG, SYSTEM_CONFIG
    from butly_core import prompts
    from butly_core.core.database import ButlyDatabase

# --- 設定エリア ---
BASE_DIR = Path(__file__).resolve().parent
INSTANCES_DIR = BASE_DIR / "butly_core" / "instances"
MAX_MID_TERM_CHARS = SYSTEM_CONFIG["memory"]["max_mid_term_chars"]

def load_api_key():
    # Try APIkey.env first, then .env
    env_path = BASE_DIR / "APIkey.env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=True)
    else:
        env_path = BASE_DIR / ".env"
        if env_path.exists():
            load_dotenv(dotenv_path=env_path, override=True)
            
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("API Keyが見つかりません。")
    return api_key

class ButlyHousekeeper:
    def __init__(self):
        self.api_key = load_api_key()
        self.client = genai.Client(api_key=self.api_key)
        
        # Configからナレッジモデル設定を使用
        self.k_conf = AI_CONFIG["knowledge"]
        
        # 根幹情報の読み込み (グローバルデフォルト)
        sys_inst_path = BASE_DIR / SYSTEM_CONFIG["paths"]["system_instruction"]
        key_mem_path = BASE_DIR / SYSTEM_CONFIG["paths"]["key_memory"]
        
        self.instruction = sys_inst_path.read_text(encoding="utf-8") if sys_inst_path.exists() else "有能な執事"
        self.key_memory = key_mem_path.read_text(encoding="utf-8") if key_mem_path.exists() else "根幹記憶なし"
        
        # インスタンスディレクトリのベースパス
        self.instances_dir = BASE_DIR / "butly_core" / "instances"

    def get_instance_key_memory(self, instance_name=None):
        """インスタンス別の Key_Memory を取得（フォールバック: グローバル）"""
        if instance_name:
            instance_km_path = self.instances_dir / instance_name / "Key_Memory.txt"
            if instance_km_path.exists():
                content = instance_km_path.read_text(encoding="utf-8").strip()
                if content:  # 空でなければインスタンス固有を使用
                    return content
        return self.key_memory

    def get_instance_instruction(self, instance_name=None):
        """インスタンス別の system_instruction を取得（フォールバック: グローバル）"""
        if instance_name:
            instance_si_path = self.instances_dir / instance_name / "system_instruction.txt"
            if instance_si_path.exists():
                content = instance_si_path.read_text(encoding="utf-8").strip()
                if content:
                    return content
        return self.instruction

    def _robust_api_call(self, func, *args, retries=5, base_delay=5, **kwargs):
        """API呼び出しの堅牢化（指数バックオフ付きリトライ）"""
        for i in range(retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                # 429: Too Many Requests, 503: Service Unavailable, 500: Internal Error
                error_str = str(e)
                if "429" in error_str or "503" in error_str or "500" in error_str:
                    wait_time = base_delay * (2 ** i) # 5, 10, 20, 40, 80...
                    print(f"[API Retry] {e} - Retrying in {wait_time}s... ({i+1}/{retries})")
                    time.sleep(wait_time)
                else:
                    # その他のエラーは即時レイズ（またはNoneを返す設計ならログ出して終了）
                    print(f"[API Error] Non-retriable error: {e}")
                    raise e
        print(f"[API Failed] Max retries exceeded.")
        return None

    def generate_embedding(self, text):
        try:
            # ConfigからEmbeddingモデルを取得
            model_name = AI_CONFIG["embedding"]["model_name"]
            
            def _call():
                return self.client.models.embed_content(
                    model=model_name,
                    contents=text,
                    config=types.EmbedContentConfig(task_type="RETRIEVAL_DOCUMENT")
                )
            
            result = self._robust_api_call(_call)
            
            if result and result.embeddings:
                return result.embeddings[0].values
            return None
        except Exception as e:
            print(f"[Embedding Error] {e}")
            return None

    def _get_next_id(self, db_type, date_str, instance_db_path):
        conn = sqlite3.connect(instance_db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        cursor = conn.cursor()
        date_suffix = date_str.replace("-", "")
        prefix = f"{db_type.lower()}_{date_suffix}_"
        cursor.execute("SELECT id FROM knowledge_cards WHERE id LIKE ? ORDER BY id DESC LIMIT 1", (prefix + "%",))
        row = cursor.fetchone()
        conn.close()
        if row:
            last_num = int(row[0].split("_")[-1])
            return f"{prefix}{last_num + 1:03d}"
        return f"{prefix}001"

    def ask_gemini_to_summarize(self, session_text, db_type):
        """各インスタンスの視点でナレッジカードを生成する。
        各インスタンス固有の人格・記憶を使用してナレッジ化を行い、
        DBへのTypeには各インスタンス名を使用する。
        """
        
        # トークン節約のアドバイス：
        # 大量のデータを一気に処理する場合、ここでCachedContentを使用することも検討できますが、
        # 現状は1日1回のバッチ処理により、呼び出し回数自体を抑えています。
        
        # ナレッジカード化は各インスタンス固有の人格・記憶で行う
        agent_instruction = self.get_instance_instruction(db_type)
        agent_key_memory = self.get_instance_key_memory(db_type)
        
        prompt = prompts.HOUSEKEEPER_SUMMARIZE_PROMPT.format(
            agent_name=SYSTEM_CONFIG["agent"]["agent_name"],
            system_instruction=agent_instruction,
            key_memory=agent_key_memory,
            session_text=session_text
        )
        try:
            def _call():
                return self.client.models.generate_content(
                    model=self.k_conf["model_name"],
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=self.k_conf["generation_config"].get("temperature"),
                        safety_settings=self.k_conf.get("safety_settings")
                    )
                )
            
            response = self._robust_api_call(_call)
            
            # Check response.text or candidates
            if not response:
                return None
            
            text = response.text if response.text else ""
            if not text and response.candidates and response.candidates[0].content.parts:
                text = response.candidates[0].content.parts[0].text
                
            json_match = re.search(r'\[.*\]', text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
        except Exception as e:
            print(f"[Gemini Error] {e}")
        return None

    def insert_knowledge(self, card, db_id, db_type, raw_ref, instance_db_path):
        conn = sqlite3.connect(instance_db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        cursor = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Unclassified の場合は理由を要約に含める
        summary_text = card['summary']
        if card['category'] == "Unclassified" and 'reason' in card:
            summary_text = f"【分類不能理由: {card['reason']}】\n{summary_text}"

        # Embedding生成
        content_to_embed = f"Title: {card['title']}\nTags: {card['tags']}\nSummary: {summary_text}"
        embedding_list = self.generate_embedding(content_to_embed)
        
        # BLOB変換 (float32)
        embedding_blob = None
        if embedding_list:
            try:
                embedding_blob = np.array(embedding_list, dtype=np.float32).tobytes()
            except Exception as e:
                print(f"[Embedding Conv Error] {e}")

        try:
            # embedding (TEXT) は NULL にし、embedding_blob (BLOB) に保存
            cursor.execute("""
            INSERT INTO knowledge_cards (
                id, type, category, title, tags, ai_importance, humanity_importance, 
                summary, episode, count, raw_reference, created_at, updated_at, embedding_blob
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                db_id, db_type, card['category'], card['title'], card['tags'],
                card['ai_importance'], card['humanity_importance'],
                summary_text, card['episode'], 1, raw_ref, now, now, embedding_blob
            ))
            conn.commit()
            return True
        except Exception as e:
            print(f"[DB Error] {e}")
            return False
        finally:
            conn.close()

    def run(self):
        for instance_path in sorted(INSTANCES_DIR.iterdir()):
            if instance_path.is_dir():
                self.process_instance(instance_path)

    def process_instance(self, instance_path):
        instance_id = instance_path.name
        # "Master" への変換は廃止。インスタンス名をそのまま db_type として使用する
        db_type = instance_id
        
        # Stage 1: 整理
        self.stage_1_cleanup(instance_path)
        
        # Stage 2: ナレッジ化
        self.stage_2_knowledgeize(instance_path, db_type)

    def stage_1_cleanup(self, instance_name="00_master"):
        """
        Phase 1: 中期記憶の更新
        1_integrated にある生ログを読んで mid_term.txt に追記する。
        溌れた古い記憶は archive_long_term.txt の末尾に追記する。
        floating_summaryは Interactions API 移行により廃止。
        """
        instance_path = INSTANCES_DIR / instance_name
        integrated_dir = instance_path / "memory_archive" / "1_integrated"
        mid_term_file = instance_path / "mid_term.txt"
        long_term_file = instance_path / "memory_archive" / "3_log" / "archive_long_term.txt"
        long_term_file.parent.mkdir(parents=True, exist_ok=True)
        
        legacy_floating_file = instance_path / "floating_summary.txt"
        floating_summary_dir = instance_path / "floating_summaries"
        short_term_json_dir = instance_path / "short_term_json"
        
        # 0. short_term_json の全ファイルを 1_integrated へ先行移動（ハウスキーパー実行時に完全リセット）
        integrated_dir.mkdir(parents=True, exist_ok=True)
        if short_term_json_dir.exists():
            short_term_files = sorted(short_term_json_dir.glob("*.json"))
            if short_term_files:
                print(f"[Housekeeper] Flushing {len(short_term_files)} short-term files to 1_integrated...")
                for stf in short_term_files:
                    dest = integrated_dir / stf.name
                    # ファイル名重複を避けるため、既存ファイルがあればサフィックスを付与
                    if dest.exists():
                        dest = integrated_dir / (stf.stem + "_dup" + stf.suffix)
                    stf.rename(dest)
        
        # 1. 処理対象を取得
        json_files = sorted(integrated_dir.glob("*.json"))
        
        if not json_files and not list(floating_summary_dir.glob("*.txt")) and not legacy_floating_file.exists():
            print(f"[Housekeeper] Phase 1: No logs or summaries to process in {instance_name}.")
            return

        print(f"[Housekeeper] Phase 1: Appending {len(json_files)} logs to mid_term...")

        # 2. JSONを読み込み、テキスト形式に整形
        new_text = ""
        for jf in json_files:
            try:
                with open(jf, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    ts_raw = data.get('timestamp', 'Unknown')
                    ts = ts_raw.split('.')[0].replace('T', ' ')
                    for msg in data.get("messages", []):
                        role_label = SYSTEM_CONFIG["agent"]["user_name"] if msg["role"] == "user" else SYSTEM_CONFIG["agent"]["agent_name"]
                        content = msg.get("parts", [""])[0]
                        if isinstance(content, dict): content = content.get("text", "")
                        new_text += f"[{ts}] {role_label}: {content}\n"
            except Exception as e:
                print(f"[Housekeeper] Error reading {jf.name}: {e}")

        # (A) floating_summaries は一時コンテキスト専用ファイル
        # ハウスキーパー時は mid_term へのマージは行わず削除のみ
        # （生JSON が 1_integrated に存在するため、二重書き込みになるのを防ぐ）
        if floating_summary_dir.exists():
            for summary_file in floating_summary_dir.glob("*.txt"):
                summary_file.unlink()
                print(f"[Housekeeper] Cleared temp floating summary: {summary_file.name}")

        # (B) 旧方式: floating_summary.txt もクリアのみ（互換性維持）
        if legacy_floating_file.exists():
            legacy_floating_file.write_text("", encoding="utf-8")

        # 3. 中期記憶の更新と長期アーカイブ処理
        current = mid_term_file.read_text(encoding="utf-8") if mid_term_file.exists() else ""
        combined = current + "\n" + new_text
        
        if len(combined) > MAX_MID_TERM_CHARS:
            min_overflow = len(combined) - MAX_MID_TERM_CHARS
            cut_point = combined.find('\n', min_overflow)
            if cut_point == -1:
                cut_point = min_overflow
            else:
                cut_point += 1
            overflow_text = combined[:cut_point]
            kept_text = combined[cut_point:]
            with open(long_term_file, "a", encoding="utf-8") as f:
                f.write(overflow_text)
            print(f"[Housekeeper] Archived {len(overflow_text)} chars to archive_long_term.txt.")
            mid_term_file.write_text(kept_text, encoding="utf-8")
        else:
            mid_term_file.write_text(combined, encoding="utf-8")

        # 4. Short Term JSON の空フォルダ削除
        short_term_dir = instance_path / "short_term_json"
        self.remove_empty_folders(short_term_dir)

        # --- 6. ★NEW: 二層要約の日次生成 ---
        if new_text.strip():
            self._generate_daily_digest(instance_path, new_text)
            self._update_relationship_if_due(instance_path)

    # --- Mid-term Summaries (Phase 3) ---
    def _get_genai_client(self):
        """Gemini APIクライアントを取得する。"""
        import os
        from dotenv import load_dotenv
        from google import genai
        
        api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        
        if not api_key:
            env_files = [BASE_DIR / "APIkey.env", BASE_DIR / ".env"]
            for env_file in env_files:
                if env_file.exists():
                    load_dotenv(dotenv_path=env_file, override=True)
                    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
                    if api_key:
                        break
        
        if not api_key:
            return None
        
        return genai.Client(api_key=api_key)

    def _generate_daily_digest(self, instance_path: Path, new_text: str):
        """
        当日分のRAWテキストからエピソード付き事実ダイジェストを生成し、
        mid_term_digest.txt に差分追記する。
        上限超過時は古い部分を archive_digest.txt へアーカイブ。
        
        入力は常に当日分のRAW（new_text）のみ。要約の要約は絶対にしない。
        """
        from butly_core.config import SYSTEM_CONFIG, AI_CONFIG
        
        if not SYSTEM_CONFIG.get("memory", {}).get("generate_mid_term_summaries", True):
            print("[Housekeeper] Mid-term summary generation is disabled in config.")
            return
        
        if len(new_text.strip()) < 200:
            print(f"[Housekeeper] Daily digest: new_text too short ({len(new_text)} chars), skipping.")
            return
        
        print(f"[Housekeeper] Daily digest: Generating from {len(new_text)} chars of today's raw text...")
        
        from google import genai
        from google.genai import types as genai_types
        from butly_core.prompts import MIDTERM_DIGEST_PROMPT
        
        try:
            client = self._get_genai_client()
            if not client:
                print("[Housekeeper] Daily digest: API client not available, skipping.")
                return
            
            summary_conf = AI_CONFIG.get("summary", {})
            model_name = summary_conf.get("model_name", "gemini-3.1-flash-lite-preview")
            safety = summary_conf.get("safety_settings")
            temp = summary_conf.get("generation_config", {}).get("temperature", 0.3)
            
            digest_file = instance_path / "mid_term_digest.txt"
            archive_digest_file = instance_path / "memory_archive" / "3_log" / "archive_digest.txt"
            archive_digest_file.parent.mkdir(parents=True, exist_ok=True)
            
            # インスタンス固有の system_instruction と key_memory を取得
            instance_name = instance_path.name
            system_instruction = self.get_instance_instruction(instance_name)
            key_memory = self.get_instance_key_memory(instance_name)
            
            digest_prompt = MIDTERM_DIGEST_PROMPT.format(
                agent_name=SYSTEM_CONFIG["agent"]["agent_name"],
                user_name=SYSTEM_CONFIG["agent"]["user_name"],
                system_instruction=system_instruction,
                key_memory=key_memory,
                raw_text=new_text,
            )
            digest_response = client.models.generate_content(
                model=model_name,
                contents=digest_prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=temp,
                    max_output_tokens=2048,
                    safety_settings=safety,
                ),
            )
            digest_new = digest_response.text.strip() if digest_response.text else ""
            
            if digest_new:
                # 既存digestに追記
                current_digest = digest_file.read_text(encoding="utf-8") if digest_file.exists() else ""
                combined_digest = current_digest + "\n" + digest_new if current_digest else digest_new
                
                # 上限チェック & アーカイブ（mid_term.txtと同じパターン）
                max_digest_chars = SYSTEM_CONFIG.get("memory", {}).get("max_digest_chars", 8000)
                
                if len(combined_digest) > max_digest_chars:
                    min_overflow = len(combined_digest) - max_digest_chars
                    cut_point = combined_digest.find('\n', min_overflow)
                    if cut_point == -1:
                        cut_point = min_overflow
                    else:
                        cut_point += 1
                    overflow_text = combined_digest[:cut_point]
                    kept_text = combined_digest[cut_point:]
                    
                    with open(archive_digest_file, "a", encoding="utf-8") as f:
                        f.write(overflow_text)
                    print(f"[Housekeeper] Digest archived: {len(overflow_text)} chars to archive_digest.txt")
                    digest_file.write_text(kept_text, encoding="utf-8")
                else:
                    digest_file.write_text(combined_digest, encoding="utf-8")
                
                print(f"[Housekeeper] Digest updated: +{len(digest_new)} chars")
            
        except Exception as e:
            print(f"[Housekeeper] Daily digest generation error: {e}")

    def _update_relationship_if_due(self, instance_path: Path):
        """
        関係性スナップショットを条件付きで更新する。
        前回の更新から relationship_update_interval_days（デフォルト7日）以上
        経過している場合のみ再生成する。
        
        入力は mid_term_digest.txt（蓄積された事実ダイジェスト）を使用する。
        日々の断片ではなく、最近の全体像から関係性パターンを抽出するため。
        
        関係性は緩やかに変化するもの。毎日書き換えると不安定になるため、
        週次程度の頻度で更新する。
        """
        from butly_core.config import SYSTEM_CONFIG, AI_CONFIG
        from datetime import datetime
        import os
        
        if not SYSTEM_CONFIG.get("memory", {}).get("generate_mid_term_summaries", True):
            return
        
        rel_file = instance_path / "mid_term_relationship.txt"
        digest_file = instance_path / "mid_term_digest.txt"
        interval_days = SYSTEM_CONFIG.get("memory", {}).get("relationship_update_interval_days", 7)
        
        # 前回更新日の確認
        should_update = False
        if not rel_file.exists():
            should_update = True
            print("[Housekeeper] Relationship: File not found, creating initial snapshot.")
        else:
            last_modified = datetime.fromtimestamp(os.path.getmtime(rel_file))
            days_since = (datetime.now() - last_modified).days
            if days_since >= interval_days:
                should_update = True
                print(f"[Housekeeper] Relationship: {days_since} days since last update (interval: {interval_days}), updating.")
            else:
                print(f"[Housekeeper] Relationship: {days_since} days since last update (interval: {interval_days}), skipping.")
        
        if not should_update:
            return
        
        # 入力: 蓄積された事実ダイジェスト
        if not digest_file.exists():
            print("[Housekeeper] Relationship: No digest file yet, skipping.")
            return
        
        digest_text = digest_file.read_text(encoding="utf-8").strip()
        if len(digest_text) < 200:
            print("[Housekeeper] Relationship: digest too short, skipping.")
            return
        
        from google import genai
        from google.genai import types as genai_types
        from butly_core.prompts import MIDTERM_RELATIONSHIP_PROMPT
        
        try:
            client = self._get_genai_client()
            if not client:
                print("[Housekeeper] Relationship: API client not available, skipping.")
                return
            
            k_conf = AI_CONFIG.get("knowledge", {})
            model_name = k_conf.get("model_name", "gemini-3.1-pro-preview")
            safety = k_conf.get("safety_settings")
            temp = k_conf.get("generation_config", {}).get("temperature", 0.7)
            
            # インスタンス固有の system_instruction と key_memory を取得
            instance_name = instance_path.name
            system_instruction = self.get_instance_instruction(instance_name)
            key_memory = self.get_instance_key_memory(instance_name)
            
            rel_prompt = MIDTERM_RELATIONSHIP_PROMPT.format(
                agent_name=SYSTEM_CONFIG["agent"]["agent_name"],
                system_instruction=system_instruction,
                key_memory=key_memory,
                digest_text=digest_text,
            )
            rel_response = client.models.generate_content(
                model=model_name,
                contents=rel_prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=temp,
                    max_output_tokens=1024,
                    safety_settings=safety,
                ),
            )
            rel_text = rel_response.text.strip() if rel_response.text else ""
            
            if rel_text:
                rel_file.write_text(rel_text, encoding="utf-8")
                print(f"[Housekeeper] Relationship snapshot updated: {len(rel_text)} chars.")
            
        except Exception as e:
            print(f"[Housekeeper] Relationship generation error: {e}")

    # --- Backup Logic ---
    def backup_database(self, instance_name="00_master"):
        """
        データベースのバックアップを作成し、古いものをローテーション（削除）する。
        保存先: butly_core/db_backups/
        世代数: Config参照
        """
        backup_dir = BASE_DIR / "butly_core" / SYSTEM_CONFIG["backup"]["dir_name"]
        backup_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = backup_dir / f"{instance_name}_butly_memory_{timestamp}.db"
        
        try:
            instance_db_path = INSTANCES_DIR / instance_name / "butly_memory.db"
            # Copy DB file
            if instance_db_path.exists():
                shutil.copy2(instance_db_path, backup_file)
                print(f"[Backup] Created: {backup_file.name}")
            
            # Rotation
            generations = SYSTEM_CONFIG["backup"]["generations"]
            backups = sorted(list(backup_dir.glob("butly_memory_*.db")))
            if len(backups) > generations:
                for old_bk in backups[:-generations]:
                    old_bk.unlink()
                    print(f"[Backup] Rotated (Deleted): {old_bk.name}")
                    
        except Exception as e:
            print(f"[Backup] Error: {e}")

    def stage_2_knowledgeize(self, target_instance, db_type="00_master"):
        """
        Stage 2: 知識化 (Knowledgeize) - RAWデータ版
        1_integrated にある生ログ (JSON) を読み込み、
        日付ごとにまとめてAIにナレッジ抽出させる。
        処理完了後、JSONファイルは 2_knowledgeized フォルダへ移動する。
        """
        print(f"--- Stage 2: Knowledgeize (RAW) for {target_instance} ({db_type}) ---")
        
        instance_dir = INSTANCES_DIR / target_instance
        integrated_dir = instance_dir / "memory_archive" / "1_integrated"
        # 修正: 情報純度維持のため RAW JSON を保管する先
        knowledgeized_root = instance_dir / "memory_archive" / "2_knowledgeized"
        knowledgeized_root.mkdir(parents=True, exist_ok=True)
        
        if not integrated_dir.exists():
            print(f"[{db_type}] No integrated directory.")
            return

        # 処理対象ファイル収集 (JSON)
        json_files = sorted(list(integrated_dir.glob("*.json")))
        if not json_files:
            print(f"[{db_type}] No JSON files to process in 1_integrated.")
            return

        # 日付ごとにグループ化
        grouped_files = {} # { "YYYY-MM-DD": [file1, file2...] }
        for f in json_files:
            try:
                # ファイルの中身からタイムスタンプを確認 (JSON)
                with open(f, "r", encoding="utf-8") as jf_obj:
                    data = json.load(jf_obj)
                    ts_str = data.get("timestamp", "")
                    # "2026-02-16T..." -> "2026-02-16"
                    if ts_str:
                        date_key = ts_str.split("T")[0]
                    else:
                        # タイムスタンプがない場合はファイルの更新日を使用
                        mtime = datetime.fromtimestamp(f.stat().st_mtime)
                        date_key = mtime.strftime("%Y-%m-%d")
                    
                    if date_key not in grouped_files:
                        grouped_files[date_key] = []
                    grouped_files[date_key].append((f, data))
            except Exception as e:
                print(f"[Stage2] Error grouping file {f.name}: {e}")
        
        # グループごとに処理
        for date_str, items in grouped_files.items():
            print(f"[{db_type}] Processing items for {date_str} ({len(items)} files)...")
            
            # 時系列順にソート
            items.sort(key=lambda x: x[1].get("timestamp", ""))
            
            # テキスト統合
            combined_text = ""
            files_in_batch = []
            
            for f_path, data in items:
                files_in_batch.append(f_path)
                
                # 表示用時刻
                ts = data.get('timestamp', 'Unknown').replace('T', ' ').split('.')[0]
                combined_text += f"\n--- Source: {f_path.name} ({ts}) ---\n"
                
                for msg in data.get("messages", []):
                    role_label = SYSTEM_CONFIG["agent"]["user_name"] if msg["role"] == "user" else SYSTEM_CONFIG["agent"]["agent_name"]
                    content = msg.get("parts", [""])[0]
                    if isinstance(content, dict): content = content.get("text", "")
                    combined_text += f"[{ts}] {role_label}: {content}\n"

            # AIによるナレッジ抽出 (RAWデータを使用)
            # 以前のコードでの引数不足バグもここで修正 (db_typeを追加)
            cards = self.ask_gemini_to_summarize(combined_text, db_type)
            
            instance_db_path = str(INSTANCES_DIR / target_instance / "butly_memory.db")
            
            if cards:
                print(f"[{db_type}] Generated {len(cards)} knowledge cards.")
                for card in cards:
                    db_id = self._get_next_id(db_type, date_str, instance_db_path)
                    self.insert_knowledge(card, db_id, db_type, f"{date_str}_raw_combined", instance_db_path)
                
                # 移動処理: 日付フォルダを作成して移動
                # ユーザー要望: 確実に日付フォルダに格納する
                dest_folder = knowledgeized_root / date_str
                dest_folder.mkdir(parents=True, exist_ok=True)
                
                for jf_path in files_in_batch:
                    try:
                        shutil.move(str(jf_path), str(dest_folder / jf_path.name))
                    except Exception as e:
                        print(f"Move Error: {e}")
                    
                print(f"[{db_type}] 処理完了・移動済み: {dest_folder}")
            else:
                print(f"[{db_type}] ナレッジ抽出なし（スキップまたはエラー） for {date_str}")
            
            # APIレート制限対策: バッチ間に少し待機
            time.sleep(5)


        # ★追加: DBバックアップ (ここで呼ぶと早期リターン時にスキップされるため削除)
        # self.backup_database()


    def remove_empty_folders(self, method_dir):
        """指定ディレクトリ以下の空フォルダを再帰的に削除"""
        if not method_dir.exists():
            return
            
        # 下層から順に削除するために walk topdown=False
        for root, dirs, files in os.walk(method_dir, topdown=False):
            for name in dirs:
                d_path = Path(root) / name
                try:
                    # 空なら削除 (rmdir は空でないとエラーになるので安全)
                    d_path.rmdir()
                    print(f"[Housekeeper] Removed empty folder: {d_path.name}")
                except OSError:
                    # 中身がある場合は無視
                    pass

    # --- 実行用 (ファイルの末尾) ---

    # --- Status Management ---
    def update_status(self, instance_name, state, progress=0.0, message=""):
        global housekeeper_store
        housekeeper_store[instance_name] = {
            "state": state, # "idle", "running", "completed", "error"
            "progress": progress,
            "message": message,
            "updated_at": datetime.now().isoformat()
        }

    def estimate_workload(self, instance_name):
        """
        未処理のJSONファイルグループ数から所要時間を予測する
        予測: 1グループ(日付)あたり約30秒
        """
        instance_dir = self.instances_dir / instance_name
        integrated_dir = instance_dir / "memory_archive" / "1_integrated"
        
        if not integrated_dir.exists():
            return {"group_count": 0, "estimated_seconds": 0}

        json_files = list(integrated_dir.glob("*.json"))
        if not json_files:
             return {"group_count": 0, "estimated_seconds": 0}

        # Group by date
        groups = set()
        for f in json_files:
            try:
                # Try to extract date from content or filename/mtime
                # For speed, just use mtime if we don't want to open every file, 
                # but opening is safer if files are small.
                # Let's stick to the logic in stage_2 but simplified for estimation
                # Actually, reading all might be slow. 
                # Let's assume file timestamps are close enough for estimation.
                # Or just count files? The prompt said "based on ... group counts".
                # Let's try to be consistent with stage 2.
                with open(f, "r", encoding="utf-8") as jf_obj:
                    data = json.load(jf_obj)
                    ts_str = data.get("timestamp", "")
                    if ts_str:
                        date_key = ts_str.split("T")[0]
                        groups.add(date_key)
                    else:
                        mtime = datetime.fromtimestamp(f.stat().st_mtime)
                        groups.add(mtime.strftime("%Y-%m-%d"))
            except:
                continue
        
        count = len(groups)
        # 1 group = 30 seconds
        estimated = count * 30
        
        return {"group_count": count, "estimated_seconds": estimated}

    def run_with_progress(self, instance_name):
        """Web APIからの実行用: 進捗を更新しながら実行"""
        try:
            self.update_status(instance_name, "running", 0.0, "初期化中...")
            
            instance_path = self.instances_dir / instance_name
            if not instance_path.exists():
                raise ValueError(f"Instance {instance_name} not found")

            # Phase 1
            self.update_status(instance_name, "running", 10.0, "記憶の統合中 (Stage 1)...")
            self.stage_1_cleanup(instance_name)
            
            # Phase 2 Preparation
            self.update_status(instance_name, "running", 30.0, "ナレッジ化の準備中...")
            
            # Re-calculate groups for accurate progress in Stage 2 if we wanted fine-grained
            # For now, just call stage 2 and let it run. 
            # Ideally stage 2 would callback progress, but we can just jump to 90% after it returns
            # since it's synchronous block.
            # To make it better, we could inject a callback into stage_2, 
            # but let's keep it simple as requested: wrap sync execution.
            
            self.update_status(instance_name, "running", 40.0, "記憶の整理と深層分析中 (Stage 2)...")
            
            # "Master" への変換は廃止。インスタンス名をそのまま使用する
            db_type = instance_name
            self.stage_2_knowledgeize(instance_name, db_type)
            
            # Phase 3
            self.update_status(instance_name, "running", 90.0, "データベースのバックアップ中...")
            self.backup_database(instance_name)
            
            self.update_status(instance_name, "completed", 100.0, "完了しました")
            
        except Exception as e:
            print(f"[Housekeeper] Error: {e}")
            self.update_status(instance_name, "error", 0.0, str(e))

# Global Status Store
housekeeper_store = {}

if __name__ == "__main__":
    hk = ButlyHousekeeper()
    # テスト対象
    target = "00_master"
        
    print(f"=== Housekeeper Logic Started for {target} ===")
    
    # 1. 中期記憶の更新 (Stage 1)
    hk.stage_1_cleanup(target)
    
    # 2. ナレッジ化とアーカイブ (Stage 2)
    hk.stage_2_knowledgeize(target, target)
    
    # 3. DBバックアップ (Stage 3)
    print("--- Stage 3: Database Backup ---")
    hk.backup_database(target)
    
    print("=== All Tasks Completed ===")