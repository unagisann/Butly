
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

class ButlyHousekeeper:
    def __init__(self):
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
        """インスタンス別の Key_Memory を取得（YAML → TXT → グローバル フォールバック）"""
        if instance_name:
            # YAML を優先
            from butly_core.core.key_memory import load_yaml, yaml_to_text, YAML_FILENAME
            yaml_path = self.instances_dir / instance_name / YAML_FILENAME
            if yaml_path.exists():
                entries = load_yaml(yaml_path)
                if entries:
                    return yaml_to_text(entries, mode="high")
            # フォールバック: TXT
            instance_km_path = self.instances_dir / instance_name / "Key_Memory.txt"
            if instance_km_path.exists():
                content = instance_km_path.read_text(encoding="utf-8").strip()
                if content:
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

    def get_instance_agent_name(self, instance_name=None) -> str:
        """インスタンス別の AI 名を取得（フォールバック: グローバル SYSTEM_CONFIG）"""
        if instance_name:
            config_path = self.instances_dir / instance_name / "config.json"
            if config_path.exists():
                try:
                    from butly_core.core.memory import _migrate_legacy_agent
                    cfg = _migrate_legacy_agent(json.loads(config_path.read_text(encoding="utf-8")))
                    name = cfg.get("agent_profile", {}).get("ai_name", "")
                    if name:
                        return name
                except Exception:
                    pass
        return SYSTEM_CONFIG["agent"].get("agent_name", "Butly")

    def get_instance_user_name(self, instance_name=None) -> str:
        """インスタンス別のユーザー名（呼称優先）を取得（フォールバック: グローバル SYSTEM_CONFIG）"""
        if instance_name:
            config_path = self.instances_dir / instance_name / "config.json"
            if config_path.exists():
                try:
                    from butly_core.core.memory import _migrate_legacy_agent
                    cfg = _migrate_legacy_agent(json.loads(config_path.read_text(encoding="utf-8")))
                    up = cfg.get("user_profile", {})
                    name = up.get("preferred_call", "") or up.get("user_name", "")
                    if name:
                        return name
                except Exception:
                    pass
        return SYSTEM_CONFIG["agent"].get("user_name", "User")

    def _get_instance_config(self, instance_name: str) -> dict:
        """インスタンスの config.json を読み込む（エラー時は空dict）"""
        if instance_name:
            config_path = self.instances_dir / instance_name / "config.json"
            if config_path.exists():
                try:
                    return json.loads(config_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
        return {}

    def _should_update(self, inst_cfg: dict, target: str) -> bool:
        """update_targets 設定に基づいて更新すべきかを判定する。"""
        targets = inst_cfg.get("housekeeper", {}).get("update_targets", {})
        defaults = {
            "digest": True,
            "recent_snapshot": True,
            "key_memory": False,
            "knowledge_cards": True,
            "raw_memory_cache": True,
        }
        # skip_knowledge_generation 後方互換
        if target == "knowledge_cards":
            if inst_cfg.get("housekeeper", {}).get("skip_knowledge_generation", False):
                return False
        return targets.get(target, defaults.get(target, True))

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
            from butly_core.llm.factory import ProviderFactory
            model_name = AI_CONFIG["embedding"]["model_name"]
            provider = ProviderFactory.create(model_name)

            result = self._robust_api_call(lambda: provider.embed(text))
            return result
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
        
        # インスタンス固有のトークン数上書き
        inst_cfg = self._get_instance_config(db_type)
        _hk = inst_cfg.get("housekeeper", {})
        if "knowledge_max_output_tokens" in _hk:
            k_conf = dict(self.k_conf)
            gc = dict(k_conf.get("generation_config", {}))
            gc["max_output_tokens"] = _hk["knowledge_max_output_tokens"]
            k_conf["generation_config"] = gc
        else:
            k_conf = self.k_conf
        
        # ナレッジカード化は各インスタンス固有の人格・記憶で行う
        agent_instruction = self.get_instance_instruction(db_type)
        agent_key_memory = self.get_instance_key_memory(db_type)
        
        from butly_core.prompts import PromptLoader
        loader = PromptLoader()
        prompt = loader.get(
            "housekeeper_summarize",
            agent_name=self.get_instance_agent_name(db_type),
            system_instruction=agent_instruction,
            key_memory=agent_key_memory,
            session_text=session_text
        )
        try:
            from butly_core.llm.factory import ProviderFactory
            provider = ProviderFactory.create(k_conf["model_name"])

            def _call():
                return provider.classify(prompt, k_conf)

            text = self._robust_api_call(_call)
            if not text:
                return None
                
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
        
        # Stage 2: ナレッジ化（update_targets で抑制可能）
        inst_cfg = self._get_instance_config(instance_id)
        if not self._should_update(inst_cfg, "knowledge_cards"):
            print(f"[Housekeeper] Stage 2 skipped for {instance_id} (knowledge_cards disabled)")
            return
        self.stage_2_knowledgeize(instance_path, db_type)

    def stage_1_cleanup(self, instance_name):
        """
        Phase 1: 整理と要約生成
        1_integrated にある生ログから daily digest を生成し、
        floating_summary をクリアする。
        mid_term.txt への追記は廃止（RAW は JSON 正本から直接読み込み）。
        """
        instance_path = INSTANCES_DIR / instance_name
        integrated_dir = instance_path / "memory_archive" / "1_integrated"
        
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
        
        # 1. 処理対象を取得（Stage1 追記済みファイルはスキップ）
        tracker_file = integrated_dir / ".mid_term_processed.json"
        processed_set = set()
        if tracker_file.exists():
            try:
                processed_set = set(json.loads(tracker_file.read_text(encoding="utf-8")))
            except Exception:
                processed_set = set()

        all_json_files = sorted(integrated_dir.glob("*.json"))
        json_files = [f for f in all_json_files if f.name not in processed_set]
        
        if not json_files and not list(floating_summary_dir.glob("*.txt")) and not legacy_floating_file.exists():
            print(f"[Housekeeper] Phase 1: No logs or summaries to process in {instance_name}.")
            return

        print(f"[Housekeeper] Phase 1: Appending {len(json_files)} logs to mid_term...")

        # 2. JSONを読み込み、テキスト形式に整形
        new_text = ""
        newly_processed = []
        for jf in json_files:
            try:
                with open(jf, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    ts_raw = data.get('timestamp', 'Unknown')
                    ts = ts_raw.split('.')[0].replace('T', ' ')
                    _agent_name = self.get_instance_agent_name(instance_name)
                    _user_name = self.get_instance_user_name(instance_name)
                    for msg in data.get("messages", []):
                        role_label = _user_name if msg["role"] == "user" else _agent_name
                        content = msg.get("parts", [""])[0]
                        if isinstance(content, dict): content = content.get("text", "")
                        new_text += f"[{ts}] {role_label}: {content}\n"
                newly_processed.append(jf.name)
            except Exception as e:
                print(f"[Housekeeper] Error reading {jf.name}: {e}")

        # 2b. トラッキング更新（mid_term追記前に記録し、追記済みとマーク）
        if newly_processed:
            processed_set.update(newly_processed)
            tracker_file.write_text(
                json.dumps(sorted(processed_set), ensure_ascii=False), encoding="utf-8"
            )

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

        # 3. RAW メモリキャッシュの再生成（2_knowledgeized から）
        from butly_core.core.raw_memory_reader import build_raw_memory_cache
        inst_cfg = self._get_instance_config(instance_name)
        mem_cfg = inst_cfg.get("memory", {})
        if self._should_update(inst_cfg, "raw_memory_cache"):
            max_raw_tokens = mem_cfg.get("max_raw_tokens", SYSTEM_CONFIG["memory"].get("max_raw_tokens", 4096))
            raw_format = mem_cfg.get("raw_injection_format", SYSTEM_CONFIG["memory"].get("raw_injection_format", "plaintext"))
            _agent_name_cache = self.get_instance_agent_name(instance_name)
            _user_name_cache = self.get_instance_user_name(instance_name)
            build_raw_memory_cache(
                instance_path,
                max_tokens=max_raw_tokens,
                injection_format=raw_format,
                agent_name=_agent_name_cache,
                user_name=_user_name_cache,
            )
        else:
            print(f"[Housekeeper] RAW memory cache rebuild skipped (raw_memory_cache disabled)")

        # 4. Short Term JSON の空フォルダ削除
        short_term_dir = instance_path / "short_term_json"
        self.remove_empty_folders(short_term_dir)

        # --- 6. ★NEW: 二層要約の日次生成 ---
        if new_text.strip() and self._should_update(inst_cfg, "digest"):
            self._generate_daily_digest(instance_path, new_text)

        # --- 7. recent_digest_headlines 生成 ---
        self._generate_recent_headlines(instance_path)

        # 近況スナップショット更新は新規ログの有無に関わらず常時チェック
        # （7日インターバルで制御されるため、毎日呼んでも問題ない）
        if self._should_update(inst_cfg, "recent_snapshot"):
            self._update_recent_snapshot_if_due(instance_path)
        else:
            print(f"[Housekeeper] Recent snapshot update skipped (recent_snapshot disabled)")

        # --- 8. Key Memory 提案生成（デフォルト OFF） ---
        if self._should_update(inst_cfg, "key_memory"):
            self._propose_key_memory_updates_if_due(instance_path)
        else:
            print(f"[Housekeeper] Key Memory proposal skipped (key_memory disabled)")

    # --- Chunk Splitting Helpers ---
    @staticmethod
    def _split_text_by_date_headers(text: str, max_chars: int) -> list:
        """
        テキストを日付ヘッダ ([YYYY-MM-DD ...]) を区切りとして
        max_chars 以内のチャンクに分割する。
        max_chars が 0 以下の場合は分割せず全体を1チャンクとして返す。
        """
        if max_chars <= 0 or len(text) <= max_chars:
            return [text]

        # 日付ヘッダパターン: 行頭の [YYYY-MM-DD ...]
        date_header_re = re.compile(r'^(\[\d{4}-\d{2}-\d{2})', re.MULTILINE)

        lines = text.split('\n')
        chunks = []
        current_lines = []
        current_len = 0

        for line in lines:
            line_len = len(line) + 1  # +1 for '\n'

            # 日付ヘッダ行で、追加すると上限超過する場合 → ここで区切る
            if date_header_re.match(line) and current_lines and (current_len + line_len) > max_chars:
                chunks.append('\n'.join(current_lines))
                current_lines = []
                current_len = 0

            current_lines.append(line)
            current_len += line_len

        if current_lines:
            chunks.append('\n'.join(current_lines))

        return chunks

    # --- Mid-term Summaries (Phase 3) ---
    def _get_provider(self, model_name=None):
        """指定モデル名の Provider を取得する。"""
        from butly_core.llm.factory import ProviderFactory
        return ProviderFactory.create(model_name or AI_CONFIG["summary"]["model_name"])

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
        
        from butly_core.prompts import PromptLoader
        
        try:
            summary_conf = dict(AI_CONFIG.get("summary", {}))
            model_name = summary_conf.get("model_name", "gemini-3.1-flash-lite-preview")
            
            digest_file = instance_path / "mid_term_digest.txt"
            archive_digest_file = instance_path / "memory_archive" / "3_log" / "archive_digest.txt"
            archive_digest_file.parent.mkdir(parents=True, exist_ok=True)
            
            # インスタンス固有の system_instruction と key_memory を取得
            instance_name = instance_path.name
            inst_cfg = self._get_instance_config(instance_name)
            
            # インスタンス固有のトークン数上書き
            _hk = inst_cfg.get("housekeeper", {})
            if "summary_max_output_tokens" in _hk:
                gc = dict(summary_conf.get("generation_config", {}))
                gc["max_output_tokens"] = _hk["summary_max_output_tokens"]
                summary_conf["generation_config"] = gc
            system_instruction = self.get_instance_instruction(instance_name)
            key_memory = self.get_instance_key_memory(instance_name)
            max_digest = inst_cfg.get("housekeeper", {}).get(
                "max_digest_chars",
                SYSTEM_CONFIG.get("memory", {}).get("max_digest_chars", 8000)
            )
            digest_max_input = _hk.get("digest_max_input_chars", 0)

            # チャンク分割: 日付ヘッダ ([YYYY-MM-DD ...]) を区切りにして上限内に収める
            text_chunks = self._split_text_by_date_headers(new_text, digest_max_input)
            if len(text_chunks) > 1:
                print(f"[Housekeeper] Daily digest: Split into {len(text_chunks)} chunks (limit: {digest_max_input} chars)")

            loader = PromptLoader()
            provider = self._get_provider(model_name)
            digest_parts = []

            for ci, chunk in enumerate(text_chunks):
                if len(text_chunks) > 1:
                    print(f"[Housekeeper] Daily digest: Processing chunk {ci+1}/{len(text_chunks)} ({len(chunk)} chars)")
                digest_prompt = loader.get(
                    "midterm_digest",
                    agent_name=self.get_instance_agent_name(instance_name),
                    user_name=self.get_instance_user_name(instance_name),
                    system_instruction=system_instruction,
                    key_memory=key_memory,
                    raw_text=chunk,
                    max_chars=max_digest,
                )
                result = provider.classify(digest_prompt, summary_conf)
                if result and result.strip():
                    digest_parts.append(result.strip())
                # チャンク間のAPI待機
                if ci < len(text_chunks) - 1:
                    time.sleep(3)

            digest_new = "\n".join(digest_parts)
            
            if digest_new:
                # 既存digestに追記
                current_digest = digest_file.read_text(encoding="utf-8") if digest_file.exists() else ""
                combined_digest = current_digest + "\n" + digest_new if current_digest else digest_new
                
                # 上限チェック & アーカイブ
                max_digest_chars = max_digest
                
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

    def _generate_recent_headlines(self, instance_path: Path):
        """
        mid_term_digest.txt から recent_digest_headlines.json を生成する。
        毎回上書き（蓄積しない）。
        digest は既に RAW から事実抽出済みのため、同じ文字数でもカバー範囲が広い。
        """
        from butly_core.config import SYSTEM_CONFIG, AI_CONFIG

        digest_file = instance_path / "mid_term_digest.txt"
        headlines_file = instance_path / "recent_digest_headlines.json"

        if not digest_file.exists():
            print("[Housekeeper] recent_headlines: mid_term_digest.txt not found, skipping.")
            return

        digest_text = digest_file.read_text(encoding="utf-8")
        if len(digest_text.strip()) < 100:
            print(f"[Housekeeper] recent_headlines: digest too short ({len(digest_text)} chars), skipping.")
            headlines_file.write_text(
                json.dumps({"generated_at": datetime.now().isoformat(), "headlines": []},
                           ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            return

        # 上限 10,000 文字。超過時は末尾を採用
        if len(digest_text) > 10000:
            digest_text = digest_text[-10000:]

        print(f"[Housekeeper] recent_headlines: Generating from {len(digest_text)} chars of digest...")

        from butly_core.prompts import PromptLoader
        try:
            summary_conf = AI_CONFIG.get("summary", {})
            model_name = summary_conf.get("model_name", "gemini-3.1-flash-lite-preview")

            instance_name = instance_path.name
            inst_cfg = self._get_instance_config(instance_name)
            summary_max_tokens = inst_cfg.get("housekeeper", {}).get(
                "summary_max_output_tokens",
                summary_conf.get("generation_config", {}).get("max_output_tokens", 4096),
            )

            loader = PromptLoader()
            prompt = loader.get(
                "recent_headlines",
                agent_name=self.get_instance_agent_name(instance_name),
                digest_text=digest_text,
            )
            provider = self._get_provider(model_name)
            raw_response = provider.classify(
                prompt,
                {"model_name": model_name, "generation_config": {"temperature": 0.0, "max_output_tokens": summary_max_tokens}},
            )

            # JSON パース
            match = re.search(r"```json(.*?)```", raw_response, re.DOTALL)
            json_str = match.group(1).strip() if match else raw_response.strip()
            data = json.loads(json_str)

            # バリデーション: headlines は最大4件
            headlines = data.get("headlines", [])[:4]
            output = {
                "generated_at": datetime.now().isoformat(),
                "headlines": headlines,
            }

            headlines_file.write_text(
                json.dumps(output, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            print(f"[Housekeeper] recent_headlines: Generated {len(headlines)} headlines.")

        except Exception as e:
            print(f"[Housekeeper] recent_headlines generation error: {e}")

    def _update_recent_snapshot_if_due(self, instance_path: Path):
        """
        近況スナップショットを条件付きで更新する。
        前回の更新から relationship_update_interval_days（デフォルト7日）以上
        経過している場合のみ再生成する。
        
        入力は mid_term_digest.txt（蓄積された事実ダイジェスト）を使用する。
        日々の断片ではなく、最近の全体像から近況パターンを抽出するため。
        
        近況は緩やかに変化するもの。毎日書き換えると不安定になるため、
        週次程度の頻度で更新する。
        """
        from butly_core.config import SYSTEM_CONFIG, AI_CONFIG
        from datetime import datetime
        import os
        
        if not SYSTEM_CONFIG.get("memory", {}).get("generate_mid_term_summaries", True):
            return
        
        # 新ファイル名を優先し、旧ファイル名にフォールバック
        rel_file = instance_path / "recent_snapshot.txt"
        legacy_rel_file = instance_path / "mid_term_relationship.txt"
        digest_file = instance_path / "mid_term_digest.txt"
        instance_name = instance_path.name
        inst_cfg = self._get_instance_config(instance_name)
        interval_days = inst_cfg.get("housekeeper", {}).get(
            "relationship_update_interval_days",
            SYSTEM_CONFIG.get("memory", {}).get("relationship_update_interval_days", 7)
        )
        
        # 前回更新日の確認（新旧どちらのファイルも確認）
        should_update = False
        check_file = rel_file if rel_file.exists() else legacy_rel_file
        if not check_file.exists():
            should_update = True
            print("[Housekeeper] Recent snapshot: File not found, creating initial snapshot.")
        else:
            last_modified = datetime.fromtimestamp(os.path.getmtime(check_file))
            days_since = (datetime.now() - last_modified).days
            if days_since >= interval_days:
                should_update = True
                print(f"[Housekeeper] Recent snapshot: {days_since} days since last update (interval: {interval_days}), updating.")
            else:
                print(f"[Housekeeper] Recent snapshot: {days_since} days since last update (interval: {interval_days}), skipping.")
        
        if not should_update:
            return
        
        # 入力: 蓄積された事実ダイジェスト
        if not digest_file.exists():
            print("[Housekeeper] Recent snapshot: No digest file yet, skipping.")
            return
        
        digest_text = digest_file.read_text(encoding="utf-8").strip()
        if len(digest_text) < 200:
            print("[Housekeeper] Recent snapshot: digest too short, skipping.")
            return
        
        from butly_core.prompts import PromptLoader
        
        try:
            k_conf = dict(AI_CONFIG.get("knowledge", {}))
            model_name = k_conf.get("model_name", "gemini-3.1-pro-preview")
            
            # インスタンス固有の system_instruction と key_memory を取得
            system_instruction = self.get_instance_instruction(instance_name)
            key_memory = self.get_instance_key_memory(instance_name)
            max_rel_chars = inst_cfg.get("housekeeper", {}).get("max_relationship_chars", 600)
            
            # インスタンス固有のトークン数上書き
            _hk = inst_cfg.get("housekeeper", {})
            if "knowledge_max_output_tokens" in _hk:
                gc = dict(k_conf.get("generation_config", {}))
                gc["max_output_tokens"] = _hk["knowledge_max_output_tokens"]
                k_conf["generation_config"] = gc
            
            loader = PromptLoader()
            rel_prompt = loader.get(
                "recent_snapshot",
                agent_name=self.get_instance_agent_name(instance_name),
                system_instruction=system_instruction,
                key_memory=key_memory,
                digest_text=digest_text,
                max_chars=max_rel_chars,
            )
            provider = self._get_provider(model_name)
            rel_text = provider.classify(rel_prompt, k_conf)
            rel_text = rel_text.strip() if rel_text else ""
            
            if rel_text:
                rel_file.write_text(rel_text, encoding="utf-8")
                print(f"[Housekeeper] Recent snapshot updated: {len(rel_text)} chars.")
            
        except Exception as e:
            print(f"[Housekeeper] Recent snapshot generation error: {e}")

    def _propose_key_memory_updates_if_due(self, instance_path: Path):
        """
        Key Memory の更新提案を生成して key_memory_proposals.json に保存する。
        前回の提案生成から key_memory_proposal_interval_days（デフォルト180日）
        以上経過している場合のみ実行する。YAML は変更しない（承認時に適用）。
        """
        from butly_core.config import SYSTEM_CONFIG, AI_CONFIG
        from butly_core.core.key_memory import (
            load_yaml, yaml_to_llm_format, parse_proposals,
            load_proposals, save_proposals, YAML_FILENAME, PROPOSALS_FILENAME,
        )

        instance_name = instance_path.name
        inst_cfg = self._get_instance_config(instance_name)
        interval_days = inst_cfg.get("housekeeper", {}).get(
            "key_memory_proposal_interval_days",
            SYSTEM_CONFIG.get("memory", {}).get("key_memory_proposal_interval_days", 180),
        )

        # インターバルチェック: proposals ファイルの mtime で判定
        proposals_file = instance_path / PROPOSALS_FILENAME
        if proposals_file.exists():
            import os
            last_modified = datetime.fromtimestamp(os.path.getmtime(proposals_file))
            days_since = (datetime.now() - last_modified).days
            if days_since < interval_days:
                print(
                    f"[Housekeeper] Key Memory proposal: {days_since} days since last "
                    f"(interval: {interval_days}), skipping."
                )
                return

        # 入力: Key Memory + digest
        yaml_path = instance_path / YAML_FILENAME
        entries = load_yaml(yaml_path) if yaml_path.exists() else []
        current_key_memory = yaml_to_llm_format(entries) if entries else "(なし)"

        digest_file = instance_path / "mid_term_digest.txt"
        if not digest_file.exists():
            print("[Housekeeper] Key Memory proposal: No digest file yet, skipping.")
            return
        digest_text = digest_file.read_text(encoding="utf-8").strip()
        if len(digest_text) < 200:
            print("[Housekeeper] Key Memory proposal: digest too short, skipping.")
            return

        from butly_core.prompts import PromptLoader

        try:
            k_conf = dict(AI_CONFIG.get("knowledge", {}))
            model_name = k_conf.get("model_name", "gemini-3.1-pro-preview")

            _hk = inst_cfg.get("housekeeper", {})
            if "knowledge_max_output_tokens" in _hk:
                gc = dict(k_conf.get("generation_config", {}))
                gc["max_output_tokens"] = _hk["knowledge_max_output_tokens"]
                k_conf["generation_config"] = gc

            loader = PromptLoader()
            prompt = loader.get(
                "key_memory_proposal",
                agent_name=self.get_instance_agent_name(instance_name),
                current_key_memory=current_key_memory,
                digest_text=digest_text,
            )

            provider = self._get_provider(model_name)
            llm_output = provider.classify(prompt, k_conf)
            llm_output = llm_output.strip() if llm_output else ""

            if not llm_output or "NO_PROPOSALS" in llm_output:
                print("[Housekeeper] Key Memory proposal: LLM returned no proposals.")
                # proposals ファイルを更新して次回インターバルをリセット
                save_proposals([], instance_path)
                return

            proposals = parse_proposals(llm_output, entries)
            if not proposals:
                print("[Housekeeper] Key Memory proposal: No parseable proposals.")
                save_proposals([], instance_path)
                return

            # status=pending を付与して保存
            now = datetime.now().isoformat()
            for p in proposals:
                p["status"] = "pending"
                p["proposed_at"] = now

            save_proposals(proposals, instance_path)
            print(f"[Housekeeper] Key Memory proposal: {len(proposals)} proposals saved.")

        except Exception as e:
            print(f"[Housekeeper] Key Memory proposal generation error: {e}")

    # --- Backup Logic ---
    def backup_database(self, instance_name):
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
        
        # チャンク分割の上限文字数を取得
        inst_cfg = self._get_instance_config(db_type)
        knowledge_max_chars = inst_cfg.get("housekeeper", {}).get("knowledge_max_input_chars", 0)

        # グループごとに処理
        for date_str, items in grouped_files.items():
            print(f"[{db_type}] Processing items for {date_str} ({len(items)} files)...")
            
            # 時系列順にソート
            items.sort(key=lambda x: x[1].get("timestamp", ""))
            
            instance_db_path = str(INSTANCES_DIR / target_instance / "butly_memory.db")
            _agent_name = self.get_instance_agent_name(db_type)
            _user_name = self.get_instance_user_name(db_type)

            # ファイル単位でテキストを事前生成
            file_texts = []  # [(f_path, text_for_this_file)]
            for f_path, data in items:
                ts = data.get('timestamp', 'Unknown').replace('T', ' ').split('.')[0]
                file_text = f"\n--- Source: {f_path.name} ({ts}) ---\n"
                for msg in data.get("messages", []):
                    role_label = _user_name if msg["role"] == "user" else _agent_name
                    content = msg.get("parts", [""])[0]
                    if isinstance(content, dict): content = content.get("text", "")
                    file_text += f"[{ts}] {role_label}: {content}\n"
                file_texts.append((f_path, file_text))

            # チャンク分割: ファイル単位で上限を超えない範囲にまとめる
            chunks = []  # [(combined_text, [f_path, ...])]
            current_text = ""
            current_files = []

            for f_path, file_text in file_texts:
                if knowledge_max_chars > 0 and current_text and (len(current_text) + len(file_text)) > knowledge_max_chars:
                    # 現在のチャンクを確定し、新しいチャンクを開始
                    chunks.append((current_text, current_files))
                    current_text = ""
                    current_files = []
                current_text += file_text
                current_files.append(f_path)

            # 残りを最後のチャンクとして追加
            if current_text:
                chunks.append((current_text, current_files))

            if len(chunks) > 1:
                print(f"[{db_type}] Split into {len(chunks)} chunks (limit: {knowledge_max_chars} chars)")

            # チャンクごとにナレッジ抽出
            all_processed_files = []
            for chunk_idx, (combined_text, files_in_batch) in enumerate(chunks):
                if len(chunks) > 1:
                    print(f"[{db_type}]   Chunk {chunk_idx+1}/{len(chunks)}: {len(combined_text)} chars, {len(files_in_batch)} files")

                cards = self.ask_gemini_to_summarize(combined_text, db_type)

                if cards:
                    print(f"[{db_type}] Generated {len(cards)} knowledge cards.")
                    for card in cards:
                        db_id = self._get_next_id(db_type, date_str, instance_db_path)
                        self.insert_knowledge(card, db_id, db_type, f"{date_str}_raw_combined", instance_db_path)
                    all_processed_files.extend(files_in_batch)
                else:
                    print(f"[{db_type}] ナレッジ抽出なし（スキップまたはエラー） for {date_str} chunk {chunk_idx+1}")

                # チャンク間のAPI待機
                if chunk_idx < len(chunks) - 1:
                    time.sleep(5)

            # 移動処理: 正常に処理できたファイルのみ移動
            if all_processed_files:
                dest_folder = knowledgeized_root / date_str
                dest_folder.mkdir(parents=True, exist_ok=True)

                for jf_path in all_processed_files:
                    try:
                        shutil.move(str(jf_path), str(dest_folder / jf_path.name))
                    except Exception as e:
                        print(f"Move Error: {e}")

                # トラッキングファイルから移動済みファイルを除去
                tracker_file = integrated_dir / ".mid_term_processed.json"
                if tracker_file.exists():
                    try:
                        tracked = set(json.loads(tracker_file.read_text(encoding="utf-8")))
                        moved_names = {f.name for f in all_processed_files}
                        tracked -= moved_names
                        if tracked:
                            tracker_file.write_text(
                                json.dumps(sorted(tracked), ensure_ascii=False), encoding="utf-8"
                            )
                        else:
                            tracker_file.unlink()
                    except Exception:
                        pass

                print(f"[{db_type}] 処理完了・移動済み: {dest_folder}")
            
            # APIレート制限対策: 日付グループ間に少し待機
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
            inst_cfg = self._get_instance_config(instance_name)
            if not self._should_update(inst_cfg, "knowledge_cards"):
                print(f"[Housekeeper] Stage 2 skipped for {instance_name} (knowledge_cards disabled)")
                self.update_status(instance_name, "running", 85.0, "ナレッジ化をスキップしました")
            else:
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
    # 全インスタンスを処理
    hk.run()
    
    print("=== All Tasks Completed ===")