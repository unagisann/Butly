
import json
import os
import shutil
import sqlite3
import re
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from dotenv import load_dotenv
import numpy as np

from butly_core.core.json_extract import extract_json_array, extract_json_str
from butly_core.core.embedding_check import record_embedding_meta
from butly_core.llm.embedding_profiles import (
    DOCUMENT,
    apply_prefix as apply_embedding_prefix,
)

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

class ButlySleeptime:
    def __init__(
        self,
        base_dir: Optional[Path] = None,
        instances_dir: Optional[Path] = None,
    ):
        self.base_dir = Path(base_dir) if base_dir is not None else BASE_DIR
        self.instances_dir = (
            Path(instances_dir)
            if instances_dir is not None
            else self.base_dir / "butly_core" / "instances"
        )

        # Configからナレッジモデル設定を使用
        self.k_conf = AI_CONFIG["knowledge"]

        # API 実測トークン数の累積（コスト観測用。pop_llm_usage() で取り出し）
        self._llm_usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
        
        # 根幹情報の読み込み (グローバルデフォルト)
        sys_inst_path = self.base_dir / SYSTEM_CONFIG["paths"]["system_instruction"]
        key_mem_path = self.base_dir / SYSTEM_CONFIG["paths"]["key_memory"]
        
        self.instruction = sys_inst_path.read_text(encoding="utf-8") if sys_inst_path.exists() else "有能な執事"
        self.key_memory = key_mem_path.read_text(encoding="utf-8") if key_mem_path.exists() else "根幹記憶なし"

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

    def get_instance_config(self, instance_name: str) -> dict:
        """インスタンスの config.json を読み込む（エラー時は空dict）"""
        if instance_name:
            config_path = self.instances_dir / instance_name / "config.json"
            if config_path.exists():
                try:
                    return json.loads(config_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
        return {}

    def get_instance_prompt_loader(self, instance_name=None):
        """Return a prompt loader bound to the instance's locale and policy."""
        from butly_core.prompts import (
            PromptLoader,
            resolve_prompt_locale,
            user_prompt_overrides_enabled,
        )

        inst_cfg = self.get_instance_config(instance_name) if instance_name else {}
        return PromptLoader(
            locale=resolve_prompt_locale(inst_cfg),
            allow_user_overrides=user_prompt_overrides_enabled(inst_cfg),
        )

    def should_update(self, inst_cfg: dict, target: str) -> bool:
        """update_targets 設定に基づいて更新すべきかを判定する。"""
        targets = inst_cfg.get("sleeptime", {}).get("update_targets", {})
        defaults = {
            "digest": True,
            "recent_snapshot": True,
            "key_memory": False,
            "knowledge_cards": True,
            "raw_memory_cache": True,
            "knowledge_maturation": False,
        }
        # skip_knowledge_generation 後方互換
        if target == "knowledge_cards":
            if inst_cfg.get("sleeptime", {}).get("skip_knowledge_generation", False):
                return False
        return targets.get(target, defaults.get(target, True))

    def _resolve_conf(self, inst_cfg: dict, section: str) -> dict:
        """インスタンス設定 → グローバル AI_CONFIG のフォールバックで設定を解決する。

        inst_cfg に *section* キーがあればそちらを優先し、なければグローバルを返す。
        sleeptime セクション内の max_output_tokens 上書きも統合する。
        """
        base = dict(AI_CONFIG.get(section, {}))
        override = inst_cfg.get(section)
        if override:
            # generation_config はマージ
            merged = {**base, **override}
            if "generation_config" in base and "generation_config" in override:
                merged["generation_config"] = {**base["generation_config"], **override["generation_config"]}
            elif "generation_config" in base:
                merged["generation_config"] = dict(base["generation_config"])
            base = merged

        # sleeptime セクションの max_output_tokens 上書き
        _hk = inst_cfg.get("sleeptime", {})
        tokens_key = f"{section}_max_output_tokens" if section != "summary" else "summary_max_output_tokens"
        if tokens_key in _hk:
            gc = dict(base.get("generation_config", {}))
            gc["max_output_tokens"] = _hk[tokens_key]
            base["generation_config"] = gc

        return base

    def _track_llm_usage(self, provider) -> None:
        """直近 API 呼び出しの実測トークン数を累積する（呼び出し直後に使う）。

        usage を返さない provider / 経路は無視。累積値は pop_llm_usage() で
        取り出す（eval の sleeptime_log などコスト観測用）。
        """
        pop = getattr(provider, "pop_last_token_usage", None)
        usage = pop() if callable(pop) else None
        # モック等の非 dict 汚染に耐える（実 provider は dict か None を返す）
        if not isinstance(usage, dict):
            return
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        self._llm_usage["prompt_tokens"] += prompt if isinstance(prompt, int) else 0
        self._llm_usage["completion_tokens"] += (
            completion if isinstance(completion, int) else 0
        )
        self._llm_usage["calls"] += 1

    def pop_llm_usage(self):
        """累積した実測トークン数を返してリセットする（1 件も無ければ None）。"""
        acc = self._llm_usage
        self._llm_usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
        return acc if acc["calls"] else None

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

    def resolve_embedding_conf(self, instance_name=None):
        """instance/global を解決した embedding 設定を返す。"""
        if instance_name:
            inst_cfg = self.get_instance_config(instance_name)
            return self._resolve_conf(inst_cfg, "embedding")
        return AI_CONFIG["embedding"]

    def generate_embedding(self, text, instance_name=None, kind=DOCUMENT):
        try:
            from butly_core.llm.factory import ProviderFactory
            emb_conf = self.resolve_embedding_conf(instance_name)
            # 書き込み側は文書 (search_document:)、検索側はクエリ
            # (search_query:) と prefix が異なる。揃っていないと保存済み
            # ベクトルとクエリベクトルが別空間になる。
            text = apply_embedding_prefix(text, emb_conf, kind)
            # Phase 2: dict 全体 (connection + model_name) を Factory に渡す
            provider = ProviderFactory.create(emb_conf)

            result = self._robust_api_call(lambda: provider.embed(text, config=emb_conf))
            self._track_llm_usage(provider)
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

    @staticmethod
    def resolve_card_source_files(card, chunk_file_names):
        """カード自己申告の source_files を検証し、(files, granularity) を返す。

        LLM は「そのカードの根拠ファイル」を出力するが、ファイル名を幻覚したり
        省略したりしうる。チャンクの実ファイル一覧に含まれる名前だけを採用し、
        1 件も残らなければ従来どおりチャンク全体を根拠として扱う。

        カード単位に絞れると RAG の原文注入が「その日の全会話」ではなく
        「そのカードを作った会話」だけになり、注入量が大きく減る。

        Returns
        -------
        (list[str], str)
            granularity は "card"（カード単位に確定）/ "chunk"（フォールバック）。
        """
        allowed = list(dict.fromkeys(chunk_file_names or []))
        if not isinstance(card, dict):
            return allowed, "chunk"

        raw = card.get("source_files")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return allowed, "chunk"

        allowed_set = set(allowed)
        # basename 一致も許容する（LLM がパス付きで書いた場合の救済）
        picked = []
        for name in raw:
            if not isinstance(name, str):
                continue
            candidate = name.strip().strip("`\"'")
            if not candidate:
                continue
            if candidate not in allowed_set:
                candidate = Path(candidate).name
            if candidate in allowed_set and candidate not in picked:
                picked.append(candidate)

        if not picked or set(picked) == allowed_set:
            # 空・全件幻覚・チャンク全体と同義ならフォールバック扱い
            return allowed, "chunk"
        return picked, "card"

    def ask_gemini_to_summarize(self, session_text, db_type):
        """各インスタンスの視点でナレッジカードを生成する。
        各インスタンス固有の人格・記憶を使用してナレッジ化を行い、
        DBへのTypeには各インスタンス名を使用する。

        Returns:
            (cards, status):
              cards  — list[dict] | None（失敗時 None、正当な抽出なしは []）
              status — "ok" | "no_cards" | "empty_response"
                       | "parse_error" | "provider_error"
            失敗を黙って None にしない: 0 枚と失敗が観測上区別できないと、
            生成分散の原因調査ができない（v10 で session まるごと消えた前例）。
        """

        # インスタンス設定 → グローバル knowledge のフォールバック
        inst_cfg = self.get_instance_config(db_type)
        k_conf = self._resolve_conf(inst_cfg, "knowledge")

        # ナレッジカード化は各インスタンス固有の人格・記憶で行う
        agent_instruction = self.get_instance_instruction(db_type)
        agent_key_memory = self.get_instance_key_memory(db_type)

        loader = self.get_instance_prompt_loader(db_type)
        prompt = loader.get(
            "sleeptime_summarize",
            agent_name=self.get_instance_agent_name(db_type),
            system_instruction=agent_instruction,
            key_memory=agent_key_memory,
            session_text=session_text
        )
        try:
            from butly_core.llm.factory import ProviderFactory
            # Phase 2: dict 全体 (connection + model_name) を Factory に渡す
            provider = ProviderFactory.create(k_conf)

            def _call():
                return provider.classify(prompt, k_conf)

            text = self._robust_api_call(_call)
            self._track_llm_usage(provider)
        except Exception as e:
            print(f"[Sleeptime] Knowledgeize provider error: {e}")
            return None, "provider_error"

        if not text or not text.strip():
            print("[Sleeptime] Knowledgeize: empty response from LLM")
            return None, "empty_response"

        try:
            cards = json.loads(extract_json_array(text))
        except Exception as e:
            print(
                f"[Sleeptime] Knowledgeize parse error: {e}\n"
                f"Raw (先頭200字): '{text[:200]}'"
            )
            return None, "parse_error"
        if not isinstance(cards, list):
            print(f"[Sleeptime] Knowledgeize: 配列でない応答 ({type(cards).__name__})")
            return None, "parse_error"
        if not cards:
            return [], "no_cards"
        return cards, "ok"

    def insert_knowledge(
        self, card, db_id, db_type, raw_ref, instance_db_path,
        source_date=None, source_files=None,
    ):
        """カードを knowledge_cards へ登録する。

        source_date: 元会話の日付 (YYYY-MM-DD)。time decay の基準になる。
        source_files: 生成に使った RAW ファイル名のリスト（遡及参照用）。
        """
        from butly_core.core.card_content import compute_content_hash, utc_now_stamp

        conn = sqlite3.connect(instance_db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        cursor = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Unclassified の場合は理由を要約に含める
        summary_text = card['summary']
        if card['category'] == "Unclassified" and 'reason' in card:
            summary_text = f"【分類不能理由: {card['reason']}】\n{summary_text}"

        # Stage 3 レビューキュー用の版識別子（共通 helper 経由。§5.2）
        content_hash = compute_content_hash({
            "title": card['title'],
            "summary": summary_text,
            "episode": card['episode'],
            "tags": card['tags'],
            "category": card['category'],
            "source_date": source_date,
        })
        queued_at = utc_now_stamp()

        # Embedding生成
        content_to_embed = f"Title: {card['title']}\nTags: {card['tags']}\nSummary: {summary_text}"
        embedding_list = self.generate_embedding(content_to_embed, instance_name=db_type)
        
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
                summary, episode, count, raw_reference, created_at, updated_at, embedding_blob,
                source_date, source_files, content_hash, maturation_queued_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                db_id, db_type, card['category'], card['title'], card['tags'],
                card['ai_importance'], card['humanity_importance'],
                summary_text, card['episode'], 1, raw_ref, now, now, embedding_blob,
                source_date,
                json.dumps(source_files, ensure_ascii=False) if source_files else None,
                content_hash, queued_at,
            ))
            conn.commit()
            if embedding_blob:
                # どの model/profile で書いたベクトルかを DB に刻む。
                # 起動時チェックが設定と突き合わせ、差し替えを検知する。
                record_embedding_meta(
                    instance_db_path, self.resolve_embedding_conf(db_type)
                )
            return True
        except Exception as e:
            print(f"[DB Error] {e}")
            return False
        finally:
            conn.close()

    def run(self):
        for instance_path in sorted(self.instances_dir.iterdir()):
            if instance_path.is_dir():
                self.process_instance(instance_path)

    def process_instance(self, instance_path):
        instance_id = instance_path.name
        # "Master" への変換は廃止。インスタンス名をそのまま db_type として使用する
        db_type = instance_id

        # Stage 1: 整理
        self.stage_1_cleanup(instance_path)

        # Stage 2: ナレッジ化（update_targets で抑制可能）
        inst_cfg = self.get_instance_config(instance_id)
        if self.should_update(inst_cfg, "knowledge_cards"):
            self.stage_2_knowledgeize(instance_path, db_type)
        else:
            print(f"[Sleeptime] Stage 2 skipped for {instance_id} (knowledge_cards disabled)")

        # Stage 3: 知識熟成（デフォルト OFF）
        if self._should_run_stage_3(inst_cfg):
            self.stage_3_mature_knowledge(instance_path)
        else:
            print(f"[Sleeptime] Stage 3 skipped for {instance_id} (knowledge_maturation disabled)")

    def stage_1_cleanup(self, instance_name):
        """
        Phase 1: 整理と要約生成
        1_integrated にある生ログから daily digest を生成し、
        floating_summary をクリアする。
        mid_term.txt への追記は廃止（RAW は JSON 正本から直接読み込み）。
        """
        instance_path = self.instances_dir / instance_name
        integrated_dir = instance_path / "memory_archive" / "1_integrated"
        
        legacy_floating_file = instance_path / "floating_summary.txt"
        floating_summary_dir = instance_path / "floating_summaries"
        short_term_json_dir = instance_path / "short_term_json"
        
        # 0. short_term_json の全ファイルを 1_integrated へ先行移動（ハウスキーパー実行時に完全リセット）
        integrated_dir.mkdir(parents=True, exist_ok=True)
        if short_term_json_dir.exists():
            short_term_files = sorted(short_term_json_dir.glob("*.json"))
            if short_term_files:
                print(f"[Sleeptime] Flushing {len(short_term_files)} short-term files to 1_integrated...")
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

        all_json_files = sorted(f for f in integrated_dir.glob("*.json") if not f.name.startswith("."))
        json_files = [f for f in all_json_files if f.name not in processed_set]
        
        if not json_files and not list(floating_summary_dir.glob("*.txt")) and not legacy_floating_file.exists():
            print(f"[Sleeptime] Phase 1: No logs or summaries to process in {instance_name}.")
            return

        print(f"[Sleeptime] Phase 1: Appending {len(json_files)} logs to mid_term...")

        # 2. JSONを読み込み、テキスト形式に整形
        # 先に全ファイルを読み、バッチ全体で複数話者かを判定する
        # (1 ファイル = 1 ターンなので、ファイル単体では複数話者になり得ない)。
        # 複数話者のときだけ user 発言を 「display_name」 でラベリングし、
        # 1:1 (オーナーのみ) は従来どおり呼称のまま (group_context_lanes_plan §4.3)。
        from butly_core.core import turn_meta

        loaded_files = []
        newly_processed = []
        for jf in json_files:
            try:
                with open(jf, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    print(f"[Sleeptime] Skipping non-dict JSON: {jf.name}")
                    continue
                loaded_files.append((jf, data))
            except Exception as e:
                print(f"[Sleeptime] Error reading {jf.name}: {e}")

        multi_speaker = turn_meta.has_multiple_speakers(
            [m for _, d in loaded_files for m in d.get("messages", [])]
        )
        from butly_core.prompts import resolve_prompt_locale

        _locale = resolve_prompt_locale(self.get_instance_config(instance_name))
        _agent_name = self.get_instance_agent_name(instance_name)
        _user_name = self.get_instance_user_name(instance_name)

        new_text = ""
        appearances = {}
        for jf, data in loaded_files:
            try:
                ts_raw = data.get('timestamp', 'Unknown')
                ts = ts_raw.split('.')[0].replace('T', ' ')
                for msg in data.get("messages", []):
                    if msg["role"] == "user":
                        if _locale == "ja":
                            role_label = turn_meta.user_label(
                                msg,
                                _user_name,
                                multi_speaker=multi_speaker,
                            )
                        else:
                            role_label = turn_meta.speaker_label(msg, _user_name)
                        self._tally_appearance(appearances, msg, ts_raw)
                    else:
                        role_label = _agent_name
                    content = msg.get("parts", [""])[0]
                    if isinstance(content, dict): content = content.get("text", "")
                    new_text += f"[{ts}] {role_label}: {content}\n"
                newly_processed.append(jf.name)
            except Exception as e:
                print(f"[Sleeptime] Error formatting {jf.name}: {e}")

        # 登場回数の集計を persons.json (stats) へ反映 (adoption gate 用。
        # 昇格閾値 N/M の判断は実データが溜まるまで保留)
        self._record_person_appearances(appearances)

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
                print(f"[Sleeptime] Cleared temp floating summary: {summary_file.name}")

        # (B) 旧方式: floating_summary.txt もクリアのみ（互換性維持）
        if legacy_floating_file.exists():
            legacy_floating_file.write_text("", encoding="utf-8")

        # 3. RAW メモリキャッシュの再生成（2_knowledgeized から）
        from butly_core.core.raw_memory_reader import build_raw_memory_cache
        inst_cfg = self.get_instance_config(instance_name)
        mem_cfg = inst_cfg.get("memory", {})
        if self.should_update(inst_cfg, "raw_memory_cache"):
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
                locale=_locale,
            )
        else:
            print(f"[Sleeptime] RAW memory cache rebuild skipped (raw_memory_cache disabled)")

        # 4. Short Term JSON の空フォルダ削除
        short_term_dir = instance_path / "short_term_json"
        self.remove_empty_folders(short_term_dir)

        # --- 6. ★NEW: 二層要約の日次生成 ---
        if new_text.strip() and self.should_update(inst_cfg, "digest"):
            self._generate_daily_digest(instance_path, new_text)

        # --- 7. recent_digest_headlines 生成 ---
        self._generate_recent_headlines(instance_path)

        # 近況スナップショット更新は新規ログの有無に関わらず常時チェック
        # （7日インターバルで制御されるため、毎日呼んでも問題ない）
        if self.should_update(inst_cfg, "recent_snapshot"):
            self._update_recent_snapshot_if_due(instance_path)
        else:
            print(f"[Sleeptime] Recent snapshot update skipped (recent_snapshot disabled)")

        # --- 8. Key Memory 提案生成（デフォルト OFF） ---
        if self.should_update(inst_cfg, "key_memory"):
            self._propose_key_memory_updates_if_due(instance_path)
        else:
            print(f"[Sleeptime] Key Memory proposal skipped (key_memory disabled)")

    # --- Person Appearance Helpers (group_context_lanes_plan §4.5) ---
    @staticmethod
    def _tally_appearance(appearances: dict, msg: dict, ts_iso: str) -> None:
        """meta 付き user メッセージの登場を集計 dict に加算する。

        meta の無い message (owner の Web チャット等) は数えない。
        adoption gate が対象とするのは外部帰属付きの登場のみ。
        """
        meta = msg.get("meta")
        pid = meta.get("person_id") if isinstance(meta, dict) else None
        if not pid:
            return
        ts = ts_iso if isinstance(ts_iso, str) and ts_iso and ts_iso != "Unknown" else None
        row = appearances.setdefault(pid, {"count": 0, "first_seen": ts, "last_seen": ts})
        row["count"] += 1
        if ts:
            if not row["first_seen"] or ts < row["first_seen"]:
                row["first_seen"] = ts
            if not row["last_seen"] or ts > row["last_seen"]:
                row["last_seen"] = ts

    def _record_person_appearances(self, appearances: dict) -> None:
        """person 登場集計を persons.json (stats) に反映する。失敗しても処理は継続。"""
        if not appearances:
            return
        try:
            from butly_core.external.person_registry import PersonRegistry
            PersonRegistry(self.base_dir).record_appearances(appearances)
            print(f"[Sleeptime] Person appearances recorded: {len(appearances)} person(s)")
        except Exception as e:
            print(f"[Sleeptime] Person appearance recording error: {e}")

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
    def _get_provider(self, model=None):
        """Provider を取得する (str / dict / ModelRef 受付)。"""
        from butly_core.llm.factory import ProviderFactory
        if model is None:
            model = AI_CONFIG["summary"]
        return ProviderFactory.create(model)

    def _generate_daily_digest(self, instance_path: Path, new_text: str):
        """
        当日分のRAWテキストからエピソード付き事実ダイジェストを生成し、
        mid_term_digest.txt に差分追記する。
        上限超過時は古い部分を archive_digest.txt へアーカイブ。
        
        入力は常に当日分のRAW（new_text）のみ。要約の要約は絶対にしない。
        """
        from butly_core.config import SYSTEM_CONFIG, AI_CONFIG
        
        if not SYSTEM_CONFIG.get("memory", {}).get("generate_mid_term_summaries", True):
            print("[Sleeptime] Mid-term summary generation is disabled in config.")
            return
        
        if len(new_text.strip()) < 200:
            print(f"[Sleeptime] Daily digest: new_text too short ({len(new_text)} chars), skipping.")
            return
        
        print(f"[Sleeptime] Daily digest: Generating from {len(new_text)} chars of today's raw text...")
        
        try:
            # インスタンス設定 → グローバル summary のフォールバック
            instance_name = instance_path.name
            inst_cfg = self.get_instance_config(instance_name)
            summary_conf = self._resolve_conf(inst_cfg, "summary")

            digest_file = instance_path / "mid_term_digest.txt"
            archive_digest_file = instance_path / "memory_archive" / "3_log" / "archive_digest.txt"
            archive_digest_file.parent.mkdir(parents=True, exist_ok=True)
            
            system_instruction = self.get_instance_instruction(instance_name)
            key_memory = self.get_instance_key_memory(instance_name)
            max_digest = inst_cfg.get("sleeptime", {}).get(
                "max_digest_chars",
                SYSTEM_CONFIG.get("memory", {}).get("max_digest_chars", 8000)
            )
            digest_max_input = inst_cfg.get("sleeptime", {}).get("digest_max_input_chars", 0)

            # チャンク分割: 日付ヘッダ ([YYYY-MM-DD ...]) を区切りにして上限内に収める
            text_chunks = self._split_text_by_date_headers(new_text, digest_max_input)
            if len(text_chunks) > 1:
                print(f"[Sleeptime] Daily digest: Split into {len(text_chunks)} chunks (limit: {digest_max_input} chars)")

            loader = self.get_instance_prompt_loader(instance_name)
            provider = self._get_provider(summary_conf)
            digest_parts = []

            for ci, chunk in enumerate(text_chunks):
                if len(text_chunks) > 1:
                    print(f"[Sleeptime] Daily digest: Processing chunk {ci+1}/{len(text_chunks)} ({len(chunk)} chars)")
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
                self._track_llm_usage(provider)
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
                    print(f"[Sleeptime] Digest archived: {len(overflow_text)} chars to archive_digest.txt")
                    digest_file.write_text(kept_text, encoding="utf-8")
                else:
                    digest_file.write_text(combined_digest, encoding="utf-8")
                
                print(f"[Sleeptime] Digest updated: +{len(digest_new)} chars")
            
        except Exception as e:
            print(f"[Sleeptime] Daily digest generation error: {e}")

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
            print("[Sleeptime] recent_headlines: mid_term_digest.txt not found, skipping.")
            return

        digest_text = digest_file.read_text(encoding="utf-8")
        if len(digest_text.strip()) < 100:
            print(f"[Sleeptime] recent_headlines: digest too short ({len(digest_text)} chars), skipping.")
            headlines_file.write_text(
                json.dumps({"generated_at": datetime.now().isoformat(), "headlines": []},
                           ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            return

        # 上限 10,000 文字。超過時は末尾を採用
        if len(digest_text) > 10000:
            digest_text = digest_text[-10000:]

        print(f"[Sleeptime] recent_headlines: Generating from {len(digest_text)} chars of digest...")

        try:
            instance_name = instance_path.name
            inst_cfg = self.get_instance_config(instance_name)
            summary_conf = self._resolve_conf(inst_cfg, "summary")

            loader = self.get_instance_prompt_loader(instance_name)
            prompt = loader.get(
                "recent_headlines",
                agent_name=self.get_instance_agent_name(instance_name),
                digest_text=digest_text,
            )
            provider = self._get_provider(summary_conf)
            raw_response = provider.classify(
                prompt,
                {**summary_conf, "generation_config": {"temperature": 0.0, "max_output_tokens": summary_conf.get("generation_config", {}).get("max_output_tokens", 4096)}},
            )
            self._track_llm_usage(provider)

            # JSON パース (閉じフェンス欠落にも耐える共通ヘルパー)
            data = json.loads(extract_json_str(raw_response))

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
            print(f"[Sleeptime] recent_headlines: Generated {len(headlines)} headlines.")

        except Exception as e:
            print(f"[Sleeptime] recent_headlines generation error: {e}")

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
        inst_cfg = self.get_instance_config(instance_name)
        interval_days = inst_cfg.get("sleeptime", {}).get(
            "relationship_update_interval_days",
            SYSTEM_CONFIG.get("memory", {}).get("relationship_update_interval_days", 7)
        )
        
        # 前回更新日の確認（新旧どちらのファイルも確認）
        should_update = False
        check_file = rel_file if rel_file.exists() else legacy_rel_file
        if not check_file.exists():
            should_update = True
            print("[Sleeptime] Recent snapshot: File not found, creating initial snapshot.")
        else:
            last_modified = datetime.fromtimestamp(os.path.getmtime(check_file))
            days_since = (datetime.now() - last_modified).days
            if days_since >= interval_days:
                should_update = True
                print(f"[Sleeptime] Recent snapshot: {days_since} days since last update (interval: {interval_days}), updating.")
            else:
                print(f"[Sleeptime] Recent snapshot: {days_since} days since last update (interval: {interval_days}), skipping.")
        
        if not should_update:
            return
        
        # 入力: 蓄積された事実ダイジェスト
        if not digest_file.exists():
            print("[Sleeptime] Recent snapshot: No digest file yet, skipping.")
            return
        
        digest_text = digest_file.read_text(encoding="utf-8").strip()
        if len(digest_text) < 200:
            print("[Sleeptime] Recent snapshot: digest too short, skipping.")
            return
        
        try:
            k_conf = self._resolve_conf(inst_cfg, "knowledge")

            # インスタンス固有の system_instruction と key_memory を取得
            system_instruction = self.get_instance_instruction(instance_name)
            key_memory = self.get_instance_key_memory(instance_name)
            max_rel_chars = inst_cfg.get("sleeptime", {}).get("max_relationship_chars", 600)
            
            loader = self.get_instance_prompt_loader(instance_name)
            rel_prompt = loader.get(
                "recent_snapshot",
                agent_name=self.get_instance_agent_name(instance_name),
                system_instruction=system_instruction,
                key_memory=key_memory,
                digest_text=digest_text,
                max_chars=max_rel_chars,
            )
            provider = self._get_provider(k_conf)
            rel_text = provider.classify(rel_prompt, k_conf)
            self._track_llm_usage(provider)
            rel_text = rel_text.strip() if rel_text else ""
            
            if rel_text:
                rel_file.write_text(rel_text, encoding="utf-8")
                print(f"[Sleeptime] Recent snapshot updated: {len(rel_text)} chars.")
            
        except Exception as e:
            print(f"[Sleeptime] Recent snapshot generation error: {e}")

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
        inst_cfg = self.get_instance_config(instance_name)
        interval_days = inst_cfg.get("sleeptime", {}).get(
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
                    f"[Sleeptime] Key Memory proposal: {days_since} days since last "
                    f"(interval: {interval_days}), skipping."
                )
                return

        # 入力: Key Memory + digest
        yaml_path = instance_path / YAML_FILENAME
        entries = load_yaml(yaml_path) if yaml_path.exists() else []
        current_key_memory = yaml_to_llm_format(entries) if entries else "(なし)"

        digest_file = instance_path / "mid_term_digest.txt"
        if not digest_file.exists():
            print("[Sleeptime] Key Memory proposal: No digest file yet, skipping.")
            return
        digest_text = digest_file.read_text(encoding="utf-8").strip()
        if len(digest_text) < 200:
            print("[Sleeptime] Key Memory proposal: digest too short, skipping.")
            return

        try:
            k_conf = self._resolve_conf(inst_cfg, "knowledge")

            loader = self.get_instance_prompt_loader(instance_name)
            prompt = loader.get(
                "key_memory_proposal",
                agent_name=self.get_instance_agent_name(instance_name),
                current_key_memory=current_key_memory,
                digest_text=digest_text,
            )

            provider = self._get_provider(k_conf)
            llm_output = provider.classify(prompt, k_conf)
            self._track_llm_usage(provider)
            llm_output = llm_output.strip() if llm_output else ""

            if not llm_output or "NO_PROPOSALS" in llm_output:
                print("[Sleeptime] Key Memory proposal: LLM returned no proposals.")
                # proposals ファイルを更新して次回インターバルをリセット
                save_proposals([], instance_path)
                return

            proposals = parse_proposals(llm_output, entries)
            if not proposals:
                print("[Sleeptime] Key Memory proposal: No parseable proposals.")
                save_proposals([], instance_path)
                return

            # status=pending を付与して保存
            now = datetime.now().isoformat()
            for p in proposals:
                p["status"] = "pending"
                p["proposed_at"] = now

            save_proposals(proposals, instance_path)
            print(f"[Sleeptime] Key Memory proposal: {len(proposals)} proposals saved.")

        except Exception as e:
            print(f"[Sleeptime] Key Memory proposal generation error: {e}")

    # --- Stage 3: Knowledge Maturation ---
    def _should_run_stage_3(self, inst_cfg: dict) -> bool:
        """update_targets の `knowledge_maturation`、設定の `knowledge_maturation_enabled`
        が両方有効な場合のみ Stage 3 を走らせる。"""
        if not self.should_update(inst_cfg, "knowledge_maturation"):
            return False
        mem_cfg = inst_cfg.get("memory", {})
        enabled = mem_cfg.get(
            "knowledge_maturation_enabled",
            SYSTEM_CONFIG.get("memory", {}).get("knowledge_maturation_enabled", False),
        )
        return bool(enabled)

    def _stage_3_param(self, inst_cfg: dict, key: str, default):
        """memory section の Stage 3 用パラメータをインスタンス→グローバルの順で取得。"""
        mem_cfg = inst_cfg.get("memory", {})
        if key in mem_cfg:
            return mem_cfg[key]
        return SYSTEM_CONFIG.get("memory", {}).get(key, default)

    def _stage3_params(self, inst_cfg: dict) -> dict:
        """Stage 3 の設定を解決する（§11 新キー。旧 max_cards は読み替え互換）。"""
        # batch_size の優先順位:
        #   1. instance の新キー knowledge_maturation_batch_size
        #   2. instance の旧キー knowledge_maturation_max_cards（後方互換）
        #   3. グローバル新キー既定 40
        # グローバルには常に新キー既定 40 が入っているため、_stage_3_param の
        # instance→global フォールバックだけでは instance 側の旧値が握り潰される。
        mem_cfg = inst_cfg.get("memory", {})
        if "knowledge_maturation_batch_size" in mem_cfg:
            batch_size = int(mem_cfg["knowledge_maturation_batch_size"])
        elif "knowledge_maturation_max_cards" in mem_cfg:
            batch_size = int(mem_cfg["knowledge_maturation_max_cards"])
        else:
            batch_size = int(
                self._stage_3_param(inst_cfg, "knowledge_maturation_batch_size", 40)
            )
        return {
            "batch_size": batch_size,
            "max_batches_per_run": int(
                self._stage_3_param(
                    inst_cfg, "knowledge_maturation_max_batches_per_run", 1
                )
            ),
            "bootstrap_max_cards": int(
                self._stage_3_param(
                    inst_cfg, "knowledge_maturation_bootstrap_max_cards", 2000
                )
            ),
            "prompt_max_chars": int(
                self._stage_3_param(
                    inst_cfg, "knowledge_maturation_prompt_max_chars", 40000
                )
            ),
            "retry_max_calls": int(
                self._stage_3_param(
                    inst_cfg, "knowledge_maturation_retry_max_calls_per_run", 8
                )
            ),
            "candidate_threshold": float(
                self._stage_3_param(inst_cfg, "memory_node_candidate_threshold", 0.65)
            ),
            "active_threshold": float(
                self._stage_3_param(inst_cfg, "memory_node_active_threshold", 0.75)
            ),
            "promotion_threshold": float(
                self._stage_3_param(inst_cfg, "memory_node_promotion_threshold", 0.85)
            ),
            "promotion_min_sources": int(
                self._stage_3_param(inst_cfg, "memory_node_promotion_min_sources", 2)
            ),
            "decay_enabled": bool(
                self._stage_3_param(inst_cfg, "memory_node_decay_enabled", False)
            ),
            "stale_days": int(
                self._stage_3_param(inst_cfg, "memory_node_stale_days", 30)
            ),
            "decay_per_period": float(
                self._stage_3_param(inst_cfg, "memory_node_decay_per_period", 0.05)
            ),
        }

    def stage_3_mature_knowledge(self, instance_path, now=None):
        """Stage 3: Knowledge Maturation（content hash 式レビューキュー。計画 §5）。

        フロー:
          1. instance 単位 process lock（non-blocking。取れなければ skip）
          2. 前 process が残した running run を abandoned として回収
          3. preflight: 非アーカイブ NULL hash の自己修復 backfill
          4. FIFO でキューから batch 選択 → LLM → 結果分類 →
             単一 transaction で node/source/counters/版 stamp/run 完了を commit
          5. promotion proposal 出力（DB commit 後に再生成可能な派生 artifact）

        now: 評価経路（LoCoMo）が session 時刻を注入するための clock。
             None なら UTC 実時刻。
        """
        from datetime import timezone as _tz

        from butly_core.core.database import ButlyDatabase
        from butly_core.core.memory_nodes import MemoryNodeRepository
        from butly_core.core import knowledge_maturation as km
        from butly_core.core.card_content import format_maturation_time

        instance_name = Path(instance_path).name
        instance_dir = self.instances_dir / instance_name
        instance_db_path = str(instance_dir / "butly_memory.db")
        if not Path(instance_db_path).exists():
            print(f"[Stage3] DB not found, skipping: {instance_db_path}")
            return None

        now_dt = now or datetime.now(_tz.utc)
        now_stamp = format_maturation_time(now_dt)

        inst_cfg = self.get_instance_config(instance_name)
        params = self._stage3_params(inst_cfg)

        totals = {
            "status": "completed",
            "batches": 0,
            "llm_calls": 0,
            "applied_cards": 0,
            "reviewed_cards": 0,
            "created": 0,
            "linked": 0,
            "superseded": 0,
            "failed_cards": [],
            "unprocessed_cards": [],
            "outcomes": [],
        }

        with km.stage3_process_lock(instance_dir) as acquired:
            if not acquired:
                print(
                    f"[Stage3] Another Stage 3 process holds the lock for "
                    f"{instance_name}; skipping."
                )
                totals["status"] = "locked"
                return totals

            # migration 保証 + 残骸回収
            ButlyDatabase(db_path=instance_db_path)
            repo = MemoryNodeRepository(instance_db_path)
            recovered = repo.recover_orphan_runs(instance_name, now_stamp=now_stamp)
            if recovered:
                print(f"[Stage3] Recovered {recovered} orphan running run(s) as abandoned.")

            try:
                backfilled = km.preflight_backfill_hashes(
                    instance_db_path, now_stamp=now_stamp
                )
                if backfilled:
                    print(f"[Stage3] Preflight backfilled content_hash for {backfilled} card(s).")
            except km.MaturationPreflightError as exc:
                print(f"[Stage3] Preflight failed: {exc}")
                run_id = repo.start_run(
                    instance_name,
                    metadata={"reason": "preflight_failed"},
                    now_stamp=now_stamp,
                )
                repo.fail_run(run_id, str(exc), now_stamp=now_stamp)
                totals["status"] = "preflight_failed"
                totals["error"] = str(exc)
                return totals

            ctx = {
                "instance_name": instance_name,
                "db_path": instance_db_path,
                "repo": repo,
                "inst_cfg": inst_cfg,
                "params": params,
                "now_stamp": now_stamp,
                "mode": "nightly",
                "extra_calls_used": 0,
            }

            for batch_index in range(params["max_batches_per_run"]):
                cards = km.select_queue_cards(
                    instance_db_path, batch_size=params["batch_size"]
                )
                if not cards:
                    if batch_index == 0:
                        print(f"[Stage3] Queue empty for {instance_name}, skipping.")
                        run_id = repo.start_run(
                            instance_name,
                            metadata={"reason": "queue_empty"},
                            now_stamp=now_stamp,
                        )
                        repo.complete_run(run_id, status="skipped", now_stamp=now_stamp)
                    break
                stats = self._stage3_process_cards(ctx, cards)
                self._stage3_merge_stats(totals, stats)
                if stats.get("aborted"):
                    totals["status"] = "partial"
                    break

            if totals["failed_cards"]:
                totals["status"] = "partial"

            # reflection: staleness 減衰スイープ（§7。LLM 呼び出し無し・opt-in）
            if params["decay_enabled"]:
                try:
                    decay_stats = km.apply_staleness_decay(
                        instance_db_path,
                        now=now_dt,
                        stale_days=params["stale_days"],
                        decay_per_period=params["decay_per_period"],
                        active_threshold=params["active_threshold"],
                    )
                    totals["decay"] = decay_stats
                    if any(decay_stats.values()):
                        print(
                            f"[Stage3] Decay sweep: decayed={decay_stats['decayed']} "
                            f"demoted={decay_stats['demoted']} "
                            f"stale_marked={decay_stats['stale_marked']}"
                        )
                except Exception as de:
                    print(f"[Stage3] decay sweep skipped: {de}")

            backlog = km.count_queue_backlog(instance_db_path)
            totals.update(backlog)
            print(
                f"[Stage3] Run summary ({instance_name}): status={totals['status']} "
                f"batches={totals['batches']} applied={totals['applied_cards']} "
                f"failed={len(totals['failed_cards'])} backlog={backlog['backlog']} "
                f"oldest_queued_at={backlog['oldest_queued_at']}"
            )

            # promotion proposal（派生 artifact。§8）
            try:
                proposals = km.collect_promotion_proposals(
                    repo=repo,
                    confidence_threshold=params["promotion_threshold"],
                    min_sources=params["promotion_min_sources"],
                    now_iso=now_stamp,
                )
                km.write_promotion_proposals_file(
                    instance_dir, proposals, now_iso=now_stamp
                )
                print(f"[Stage3] Promotion proposals: {len(proposals)}")
            except Exception as pe:
                print(f"[Stage3] proposal generation skipped: {pe}")

            return totals

    @staticmethod
    def _stage3_merge_stats(totals: dict, stats: dict) -> None:
        totals["batches"] += stats.get("batches", 0)
        totals["llm_calls"] += stats.get("llm_calls", 0)
        totals["applied_cards"] += stats.get("applied_cards", 0)
        totals["reviewed_cards"] += stats.get("reviewed_cards", 0)
        totals["created"] += stats.get("created", 0)
        totals["linked"] += stats.get("linked", 0)
        totals["superseded"] += stats.get("superseded", 0)
        totals["failed_cards"].extend(stats.get("failed_cards", []))
        totals["unprocessed_cards"].extend(stats.get("unprocessed_cards", []))
        totals["outcomes"].extend(stats.get("outcomes", []))

    def _stage3_take_budget(self, ctx: dict) -> bool:
        """retry/split の追加 LLM 呼び出し予算を 1 消費する（§5.6）。"""
        if ctx["extra_calls_used"] >= ctx["params"]["retry_max_calls"]:
            return False
        ctx["extra_calls_used"] += 1
        return True

    def _stage3_process_cards(self, ctx: dict, cards: list) -> dict:
        """1 batch を処理する。retryable 失敗は retry → 半分分割で縮小再試行する。

        予算 (retry_max_calls_per_run) を使い切ったら残りを未処理のまま返す。
        1 件まで縮小しても失敗するカードは failed_cards として隔離する（§6）。
        """
        from butly_core.core import knowledge_maturation as km

        stats = {
            "batches": 0,
            "llm_calls": 0,
            "applied_cards": 0,
            "reviewed_cards": 0,
            "created": 0,
            "linked": 0,
            "superseded": 0,
            "failed_cards": [],
            "unprocessed_cards": [],
            "outcomes": [],
            "aborted": False,
        }

        result = self._stage3_review_batch(ctx, cards)
        self._stage3_absorb_batch_result(stats, result, cards)
        outcome = result["outcome"]
        if outcome in (km.OUTCOME_OK, km.OUTCOME_NO_CHANGES):
            return stats
        if outcome == km.OUTCOME_PROVIDER_ERROR:
            stats["aborted"] = True
            stats["failed_cards"].extend(c["id"] for c in cards)
            return stats
        if outcome == "changed_during_run":
            # 新版はキューに残っており次 run で再選択される。ここでは再試行しない。
            return stats
        if outcome == "db_error":
            stats["failed_cards"].extend(c["id"] for c in cards)
            return stats

        # retryable (truncated / empty / parse_error)
        if outcome in km.RETRYABLE_OUTCOMES:
            if self._stage3_take_budget(ctx):
                retry = self._stage3_review_batch(ctx, cards)
                self._stage3_absorb_batch_result(stats, retry, cards)
                if retry["outcome"] in (km.OUTCOME_OK, km.OUTCOME_NO_CHANGES):
                    return stats
                if retry["outcome"] == km.OUTCOME_PROVIDER_ERROR:
                    stats["aborted"] = True
                    stats["failed_cards"].extend(c["id"] for c in cards)
                    return stats
                outcome = retry["outcome"]

            if len(cards) > 1:
                mid = len(cards) // 2
                for half in (cards[:mid], cards[mid:]):
                    if not half:
                        continue
                    if not self._stage3_take_budget(ctx):
                        stats["unprocessed_cards"].extend(c["id"] for c in half)
                        print(
                            "[Stage3] Retry budget exhausted; "
                            f"{len(half)} card(s) left unprocessed in queue."
                        )
                        continue
                    sub = self._stage3_process_cards(ctx, half)
                    self._stage3_merge_stats_into(stats, sub)
                    if sub.get("aborted"):
                        stats["aborted"] = True
                        break
            else:
                # 1 件まで縮小しても失敗 → 隔離（stamp しないので次回 run で再試行可能）
                stats["failed_cards"].append(cards[0]["id"])
                print(
                    f"[Stage3] Card {cards[0]['id']} isolated after repeated "
                    f"{outcome}; it stays in the queue for future runs."
                )
        return stats

    @staticmethod
    def _stage3_merge_stats_into(stats: dict, sub: dict) -> None:
        stats["batches"] += sub.get("batches", 0)
        stats["llm_calls"] += sub.get("llm_calls", 0)
        stats["applied_cards"] += sub.get("applied_cards", 0)
        stats["reviewed_cards"] += sub.get("reviewed_cards", 0)
        stats["created"] += sub.get("created", 0)
        stats["linked"] += sub.get("linked", 0)
        stats["superseded"] += sub.get("superseded", 0)
        stats["failed_cards"].extend(sub.get("failed_cards", []))
        stats["unprocessed_cards"].extend(sub.get("unprocessed_cards", []))
        stats["outcomes"].extend(sub.get("outcomes", []))

    @staticmethod
    def _stage3_absorb_batch_result(stats: dict, result: dict, cards: list) -> None:
        stats["batches"] += 1
        stats["llm_calls"] += result.get("llm_calls", 0)
        stats["outcomes"].append(result["outcome"])
        if result["outcome"] in ("ok", "no_changes"):
            stats["applied_cards"] += len(cards)
            stats["reviewed_cards"] += len(cards)
            stats["created"] += result.get("created", 0)
            stats["linked"] += result.get("linked", 0)
            stats["superseded"] += result.get("superseded", 0)

    def _stage3_review_batch(self, ctx: dict, cards: list) -> dict:
        """1 batch = 1 run。LLM 1 回 → 結果分類 → 成功時のみ単一 transaction 適用。

        失敗時（truncated/empty/parse/provider/db）はカード版を stamp せず、
        run と run_cards に failed を記録する（§5.4）。
        """
        from butly_core.core import knowledge_maturation as km
        from butly_core.core.memory_nodes import MaturationUnitOfWork

        repo = ctx["repo"]
        params = ctx["params"]
        now_stamp = ctx["now_stamp"]
        instance_name = ctx["instance_name"]
        card_ids = [c["id"] for c in cards]
        selected_versions = {c["id"]: c["content_hash"] for c in cards}

        run_id = repo.start_run(
            instance_name,
            metadata={"mode": ctx["mode"], "batch_cards": len(cards)},
            now_stamp=now_stamp,
        )
        repo.record_run_cards(
            run_id,
            [(cid, selected_versions[cid]) for cid in card_ids],
            now_stamp=now_stamp,
        )

        context_nodes = km.select_context_nodes(ctx["db_path"], cards)
        prompt = km.build_review_prompt(
            loader=self.get_instance_prompt_loader(instance_name),
            agent_name=self.get_instance_agent_name(instance_name),
            system_instruction=self.get_instance_instruction(instance_name),
            key_memory=self.get_instance_key_memory(instance_name),
            existing_nodes=context_nodes,
            review_cards=cards,
            prompt_max_chars=params["prompt_max_chars"],
        )

        k_conf = self._resolve_conf(ctx["inst_cfg"], "knowledge")
        provider = self._get_provider(k_conf)
        try:
            raw = self._robust_api_call(lambda: provider.classify(prompt, k_conf))
            self._track_llm_usage(provider)
            if raw is None:
                # _robust_api_call がリトライ枯渇で None を返した
                # （classify の正当な空応答は "" であって None ではない）
                raise RuntimeError("provider retries exhausted")
            pop_meta = getattr(provider, "pop_last_completion_metadata", None)
            meta = pop_meta() if callable(pop_meta) else None
            finish_reason = (
                meta.get("finish_reason") if isinstance(meta, dict) else None
            )
        except Exception as exc:
            error = f"provider_error: {exc}"
            print(f"[Stage3] {error}")
            repo.mark_run_cards(
                run_id, card_ids, status="failed", error=error, now_stamp=now_stamp
            )
            repo.fail_run(run_id, error, now_stamp=now_stamp)
            return {
                "outcome": km.OUTCOME_PROVIDER_ERROR,
                "run_id": run_id,
                "llm_calls": 1,
                "error": str(exc),
            }

        outcome, parsed, error = km.classify_review_response(raw, finish_reason)
        if outcome in km.RETRYABLE_OUTCOMES:
            message = f"{outcome}: {error}"
            print(f"[Stage3] {message} (cards={len(cards)})")
            repo.mark_run_cards(
                run_id, card_ids, status="failed", error=message, now_stamp=now_stamp
            )
            repo.fail_run(run_id, message, now_stamp=now_stamp)
            return {
                "outcome": outcome,
                "run_id": run_id,
                "llm_calls": 1,
                "error": error,
            }

        # ok / no_changes → 単一 transaction で適用（§5.4-4/-5）
        valid_card_ids = set(card_ids)
        valid_node_ids = {n["id"] for n in context_nodes}
        reviewed_diag = km.check_reviewed_card_ids(parsed, valid_card_ids)
        if reviewed_diag:
            print(f"[Stage3] Warning: {reviewed_diag}")

        try:
            changed_ids: list = []
            counters = {"created": 0, "linked": 0, "superseded": 0}
            with MaturationUnitOfWork(ctx["db_path"], now_stamp=now_stamp) as uow:
                current_hashes = uow.get_card_hashes(card_ids)
                changed_ids = [
                    cid
                    for cid in card_ids
                    if current_hashes.get(cid) != selected_versions[cid]
                ]
                if changed_ids:
                    unchanged = [c for c in card_ids if c not in changed_ids]
                    uow.mark_run_cards(
                        run_id, changed_ids, status="changed_during_run"
                    )
                    if unchanged:
                        uow.mark_run_cards(run_id, unchanged, status="abandoned")
                    uow.fail_run(
                        run_id, f"changed_during_run: {sorted(changed_ids)}"
                    )
                else:
                    linked, uncertain_ids, diag1 = km.apply_link_existing(
                        repo=uow,
                        entries=parsed["link_existing"],
                        valid_node_ids=valid_node_ids,
                        valid_card_ids=valid_card_ids,
                        run_id=run_id,
                    )
                    self._reinforce_linked_nodes(
                        repo=uow,
                        run_id=run_id,
                        link_entries=parsed["link_existing"],
                        valid_node_ids=valid_node_ids,
                        active_threshold=params["active_threshold"],
                        candidate_threshold=params["candidate_threshold"],
                    )
                    created, superseded, diag2 = km.apply_new_nodes(
                        repo=uow,
                        entries=parsed["new_nodes"],
                        valid_node_ids=valid_node_ids,
                        valid_card_ids=valid_card_ids,
                        run_id=run_id,
                        candidate_threshold=params["candidate_threshold"],
                        active_threshold=params["active_threshold"],
                    )
                    km.mark_uncertain_nodes(
                        repo=uow, node_ids=uncertain_ids, run_id=run_id
                    )
                    uow.update_run_counters(
                        run_id,
                        reviewed_card_count=len(cards),
                        created_node_count=created,
                        linked_source_count=linked,
                        superseded_node_count=superseded,
                    )
                    diagnostics = [d for d in [reviewed_diag, *diag1, *diag2] if d]
                    for d in diagnostics:
                        if d != reviewed_diag:
                            print(f"[Stage3] Warning: {d}")
                    card_status = (
                        "applied" if outcome == km.OUTCOME_OK else "no_changes"
                    )
                    uow.mark_run_cards(
                        run_id,
                        card_ids,
                        status=card_status,
                        diagnostic="; ".join(diagnostics) or None,
                    )
                    for cid in card_ids:
                        if not uow.stamp_card_version(
                            card_id=cid,
                            content_hash=selected_versions[cid],
                            run_id=run_id,
                        ):
                            # BEGIN IMMEDIATE 下で再検証済みなので通常起きない。
                            # 万一 stamp できない版があれば、node だけ commit して
                            # カードを未処理のまま取り残さないよう例外で rollback する。
                            raise RuntimeError(
                                f"card version stamp failed for {cid}; "
                                "rolling back batch"
                            )
                    uow.complete_run(run_id, status="completed")
                    counters = {
                        "created": created,
                        "linked": linked,
                        "superseded": superseded,
                    }
        except Exception as exc:
            error = f"db_error: {exc}"
            print(f"[Stage3] Apply transaction failed (rolled back): {error}")
            try:
                repo.mark_run_cards(
                    run_id, card_ids, status="failed", error=error, now_stamp=now_stamp
                )
                repo.fail_run(run_id, error, now_stamp=now_stamp)
            except Exception:
                pass
            return {
                "outcome": "db_error",
                "run_id": run_id,
                "llm_calls": 1,
                "error": str(exc),
            }

        if changed_ids:
            print(
                f"[Stage3] Batch dropped: content changed during run for "
                f"{sorted(changed_ids)}; new versions stay queued."
            )
            return {
                "outcome": "changed_during_run",
                "run_id": run_id,
                "llm_calls": 1,
            }

        print(
            f"[Stage3] Run completed: run={run_id} cards={len(cards)} "
            f"linked={counters['linked']} created={counters['created']} "
            f"superseded={counters['superseded']} outcome={outcome}"
        )
        return {"outcome": outcome, "run_id": run_id, "llm_calls": 1, **counters}

    def _reinforce_linked_nodes(
        self,
        *,
        repo,
        run_id: str,
        link_entries: list,
        valid_node_ids: set,
        active_threshold: float = 0.75,
        candidate_threshold: float = 0.65,
    ) -> None:
        """supports relation で link された node の confidence をわずかに引き上げ、
        contradicts であれば下げる。リインフォース日時も更新する。

        repo は MemoryNodeRepository / MaturationUnitOfWork のどちらでもよい。
        """
        deltas: dict[str, float] = {}
        for e in link_entries:
            if not isinstance(e, dict):
                continue
            nid = e.get("node_id")
            if nid not in valid_node_ids:
                continue
            relation = e.get("relation", "supports")
            try:
                base = max(0.0, min(1.0, float(e.get("confidence", 0.5))))
            except (TypeError, ValueError):
                base = 0.5
            if relation == "supports":
                deltas[nid] = deltas.get(nid, 0.0) + 0.05 + 0.05 * base
            elif relation == "contradicts":
                deltas[nid] = deltas.get(nid, 0.0) - 0.10 - 0.05 * base
            # context は confidence に影響を与えない

        for nid, delta in deltas.items():
            node = repo.get_node(nid)
            if not node:
                continue
            new_conf = float(node.get("confidence") or 0.0) + delta
            new_conf = max(0.0, min(1.0, new_conf))
            reinforce = delta > 0
            repo.update_node(
                nid,
                confidence=new_conf,
                updated_by_run_id=run_id,
                reinforce=reinforce,
            )
            # confidence の昇降で status を遷移させる
            current_status = node.get("status")
            if current_status not in ("superseded", "uncertain"):
                if new_conf >= active_threshold and current_status != "active":
                    repo.update_node(nid, status="active", updated_by_run_id=run_id)
                elif new_conf < candidate_threshold and current_status == "active":
                    repo.update_node(nid, status="candidate", updated_by_run_id=run_id)

    # --- Stage 3: Bootstrap (§6) ---
    def stage3_bootstrap(self, instance_name, now=None, *, max_cards_override=None):
        """既存インスタンスのレビューキューを一括 drain する明示 CLI 用エントリ。

        - キューが空になるまで batch_size 単位で通常レビュー（§5.4 共通 executor）
          を反復する。安全上限 bootstrap_max_cards で総投入カード数を制限。
        - retryable 失敗は retry → 半分割 → 1 件隔離。隔離カードは
          **この invocation 内だけ**選択から除外し、版は stamp しないので
          次回 run / bootstrap で再試行できる。
        - 冪等・再開可能: batch transaction が commit 済みの版だけ処理済みになる。
          途中クラッシュしても再実行は未処理版から続く。
        - 失敗が残れば status="partial" として失敗一覧・未処理数を返す。
        """
        from datetime import timezone as _tz

        from butly_core.core.database import ButlyDatabase
        from butly_core.core.memory_nodes import MemoryNodeRepository
        from butly_core.core import knowledge_maturation as km
        from butly_core.core.card_content import format_maturation_time

        instance_dir = self.instances_dir / instance_name
        instance_db_path = str(instance_dir / "butly_memory.db")
        if not Path(instance_db_path).exists():
            print(f"[Stage3][bootstrap] DB not found: {instance_db_path}")
            return {"status": "no_db", "instance": instance_name}

        now_dt = now or datetime.now(_tz.utc)
        now_stamp = format_maturation_time(now_dt)

        inst_cfg = self.get_instance_config(instance_name)
        params = self._stage3_params(inst_cfg)
        max_cards = (
            int(max_cards_override)
            if max_cards_override is not None
            else params["bootstrap_max_cards"]
        )

        totals = {
            "status": "completed",
            "instance": instance_name,
            "batches": 0,
            "llm_calls": 0,
            "applied_cards": 0,
            "reviewed_cards": 0,
            "created": 0,
            "linked": 0,
            "superseded": 0,
            "attempted_cards": 0,
            "failed_cards": [],
            "unprocessed_cards": [],
            "outcomes": [],
        }

        with km.stage3_process_lock(instance_dir) as acquired:
            if not acquired:
                print(
                    f"[Stage3][bootstrap] Another Stage 3 process holds the lock "
                    f"for {instance_name}; aborting."
                )
                totals["status"] = "locked"
                return totals

            ButlyDatabase(db_path=instance_db_path)
            repo = MemoryNodeRepository(instance_db_path)
            recovered = repo.recover_orphan_runs(instance_name, now_stamp=now_stamp)
            if recovered:
                print(
                    f"[Stage3][bootstrap] Recovered {recovered} orphan running "
                    f"run(s) as abandoned."
                )

            try:
                backfilled = km.preflight_backfill_hashes(
                    instance_db_path, now_stamp=now_stamp
                )
                if backfilled:
                    print(
                        f"[Stage3][bootstrap] Preflight backfilled content_hash "
                        f"for {backfilled} card(s)."
                    )
            except km.MaturationPreflightError as exc:
                print(f"[Stage3][bootstrap] Preflight failed: {exc}")
                totals["status"] = "preflight_failed"
                totals["error"] = str(exc)
                return totals

            start_backlog = km.count_queue_backlog(instance_db_path)
            print(
                f"[Stage3][bootstrap] Start: backlog={start_backlog['backlog']} "
                f"oldest_queued_at={start_backlog['oldest_queued_at']} "
                f"safety_limit={max_cards}"
            )

            excluded: set = set()
            ctx = {
                "instance_name": instance_name,
                "db_path": instance_db_path,
                "repo": repo,
                "inst_cfg": inst_cfg,
                "params": params,
                "now_stamp": now_stamp,
                "mode": "bootstrap",
                "extra_calls_used": 0,
            }

            hit_safety_limit = False
            while totals["attempted_cards"] < max_cards:
                batch_limit = min(
                    params["batch_size"], max_cards - totals["attempted_cards"]
                )
                cards = km.select_queue_cards(
                    instance_db_path,
                    batch_size=batch_limit,
                    exclude_ids=tuple(excluded),
                )
                if not cards:
                    break

                # retry/split 予算は run（= batch 反復）ごとにリセットする
                ctx["extra_calls_used"] = 0
                stats = self._stage3_process_cards(ctx, cards)
                self._stage3_merge_stats(totals, stats)
                totals["attempted_cards"] += len(cards)

                newly_failed = set(stats.get("failed_cards", []))
                excluded |= newly_failed

                backlog = km.count_queue_backlog(instance_db_path)
                remaining = max(0, backlog["backlog"] - len(excluded))
                print(
                    f"[Stage3][bootstrap] applied {totals['applied_cards']} / "
                    f"failed {len(totals['failed_cards'])} / remaining {remaining} "
                    f"(oldest_queued_at={backlog['oldest_queued_at']})"
                )

                if stats.get("aborted"):
                    print(
                        "[Stage3][bootstrap] Provider unrecoverable; "
                        "stopping early. Re-run to resume from the queue."
                    )
                    totals["status"] = "partial"
                    totals["aborted"] = True
                    break

                if not stats.get("applied_cards") and not newly_failed:
                    # 前進の無い反復（予算切れの未処理のみ等）は無限ループ防止で終了
                    print(
                        "[Stage3][bootstrap] No progress in this iteration; "
                        "stopping. Re-run to retry the remaining cards."
                    )
                    totals["status"] = "partial"
                    break

            if totals["attempted_cards"] >= max_cards:
                hit_safety_limit = True

            end_backlog = km.count_queue_backlog(instance_db_path)
            totals.update(end_backlog)
            if totals["failed_cards"] or totals["unprocessed_cards"]:
                totals["status"] = "partial"
            if hit_safety_limit and end_backlog["backlog"] > len(excluded):
                totals["status"] = "partial"
                totals["safety_limit_reached"] = True
                print(
                    f"[Stage3][bootstrap] Safety limit {max_cards} reached with "
                    f"{end_backlog['backlog']} card(s) still queued."
                )

            print(
                f"[Stage3][bootstrap] Done: status={totals['status']} "
                f"applied={totals['applied_cards']} "
                f"failed={len(totals['failed_cards'])} "
                f"backlog={end_backlog['backlog']} llm_calls={totals['llm_calls']}"
            )

            # promotion proposal は drain 後にまとめて再生成（派生 artifact）
            try:
                proposals = km.collect_promotion_proposals(
                    repo=repo,
                    confidence_threshold=params["promotion_threshold"],
                    min_sources=params["promotion_min_sources"],
                    now_iso=now_stamp,
                )
                km.write_promotion_proposals_file(
                    instance_dir, proposals, now_iso=now_stamp
                )
                print(f"[Stage3][bootstrap] Promotion proposals: {len(proposals)}")
            except Exception as pe:
                print(f"[Stage3][bootstrap] proposal generation skipped: {pe}")

            return totals

    # --- Backup Logic ---
    def backup_database(self, instance_name):
        """
        データベースのバックアップを作成し、古いものをローテーション（削除）する。
        保存先: butly_core/db_backups/
        世代数: Config参照
        """
        backup_dir = (
            self.base_dir / "butly_core" / SYSTEM_CONFIG["backup"]["dir_name"]
        )
        backup_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = backup_dir / f"{instance_name}_butly_memory_{timestamp}.db"
        
        try:
            instance_db_path = self.instances_dir / instance_name / "butly_memory.db"
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
        失敗チャンクの RAW は移動せず残す（次回パスで再試行される）。

        Returns:
            {"chunks": int, "failed_chunks": int, "cards_created": int,
             "failures": [{"date", "chunk", "reason"}, ...]}
        """
        print(f"--- Stage 2: Knowledgeize (RAW) for {target_instance} ({db_type}) ---")
        # source_files_card / _chunk: カード単位の根拠に絞れた枚数と、
        # LLM が特定できずチャンク全体にフォールバックした枚数（RAG 原文注入量に直結）
        stats = {
            "chunks": 0,
            "failed_chunks": 0,
            "cards_created": 0,
            "failures": [],
            "source_files_card": 0,
            "source_files_chunk": 0,
        }

        instance_dir = self.instances_dir / target_instance
        integrated_dir = instance_dir / "memory_archive" / "1_integrated"
        # 修正: 情報純度維持のため RAW JSON を保管する先
        knowledgeized_root = instance_dir / "memory_archive" / "2_knowledgeized"
        knowledgeized_root.mkdir(parents=True, exist_ok=True)

        # DB migration 保証: 直接 sqlite3.connect する前に ButlyDatabase で初期化する
        instance_db_path = str(self.instances_dir / target_instance / "butly_memory.db")
        from butly_core.core.database import ButlyDatabase as _ButlyDB
        _ButlyDB(db_path=instance_db_path)

        if not integrated_dir.exists():
            print(f"[{db_type}] No integrated directory.")
            return stats

        # 処理対象ファイル収集 (JSON) — ドットファイル (.mid_term_processed.json 等) を除外
        json_files = sorted(f for f in integrated_dir.glob("*.json") if not f.name.startswith("."))
        if not json_files:
            print(f"[{db_type}] No JSON files to process in 1_integrated.")
            return stats

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
        inst_cfg = self.get_instance_config(db_type)
        from butly_core.prompts import resolve_prompt_locale

        locale = resolve_prompt_locale(inst_cfg)
        knowledge_max_chars = inst_cfg.get("sleeptime", {}).get("knowledge_max_input_chars", 0)

        # グループごとに処理
        for date_str, items in grouped_files.items():
            print(f"[{db_type}] Processing items for {date_str} ({len(items)} files)...")
            
            # 時系列順にソート
            items.sort(key=lambda x: x[1].get("timestamp", ""))
            
            instance_db_path = str(
                self.instances_dir / target_instance / "butly_memory.db"
            )
            _agent_name = self.get_instance_agent_name(db_type)
            _user_name = self.get_instance_user_name(db_type)

            # 日付グループ全体で複数話者かを判定 (stage_1 と同じ規則)
            from butly_core.core import turn_meta
            multi_speaker = turn_meta.has_multiple_speakers(
                [m for _, d in items for m in d.get("messages", [])]
            )

            # ファイル単位でテキストを事前生成
            file_texts = []  # [(f_path, text_for_this_file)]
            for f_path, data in items:
                ts = data.get('timestamp', 'Unknown').replace('T', ' ').split('.')[0]
                file_text = f"\n--- Source: {f_path.name} ({ts}) ---\n"
                for msg in data.get("messages", []):
                    if msg["role"] == "user":
                        if locale == "ja":
                            role_label = turn_meta.user_label(
                                msg,
                                _user_name,
                                multi_speaker=multi_speaker,
                            )
                        else:
                            role_label = turn_meta.speaker_label(msg, _user_name)
                    else:
                        role_label = _agent_name
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

                cards, status = self.ask_gemini_to_summarize(combined_text, db_type)
                stats["chunks"] += 1

                if status == "ok":
                    print(f"[{db_type}] Generated {len(cards)} knowledge cards.")
                    chunk_file_names = [f.name for f in files_in_batch]
                    for card in cards:
                        db_id = self._get_next_id(db_type, date_str, instance_db_path)
                        card_files, granularity = self.resolve_card_source_files(
                            card, chunk_file_names
                        )
                        stats[f"source_files_{granularity}"] += 1
                        self.insert_knowledge(
                            card, db_id, db_type,
                            f"{date_str}_raw_combined", instance_db_path,
                            source_date=date_str,
                            source_files=card_files,
                        )
                    stats["cards_created"] += len(cards)
                    all_processed_files.extend(files_in_batch)
                elif status == "no_cards":
                    # 正当な「抽出対象なし」— 再処理し続けないよう処理済み扱いで移動
                    print(f"[{db_type}] ナレッジ抽出対象なし for {date_str} chunk {chunk_idx+1}")
                    all_processed_files.extend(files_in_batch)
                else:
                    # 失敗チャンクの RAW は 1_integrated に残し、次回の Sleeptime で再試行する
                    stats["failed_chunks"] += 1
                    stats["failures"].append(
                        {"date": date_str, "chunk": chunk_idx + 1, "reason": status}
                    )
                    print(
                        f"[{db_type}] ナレッジ抽出失敗 ({status}) for {date_str} "
                        f"chunk {chunk_idx+1} — RAW は保持し次回再試行"
                    )

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

        if stats["failed_chunks"]:
            print(
                f"[{db_type}] Stage 2: {stats['failed_chunks']}/{stats['chunks']} "
                f"チャンクが失敗 (RAW 保持済み): {stats['failures']}"
            )
        return stats


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
                    print(f"[Sleeptime] Removed empty folder: {d_path.name}")
                except OSError:
                    # 中身がある場合は無視
                    pass

    # --- 実行用 (ファイルの末尾) ---

    # --- Status Management ---
    def update_status(self, instance_name, state, progress=0.0, message=""):
        sleeptime_store[instance_name] = {
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

        json_files = [f for f in integrated_dir.glob("*.json") if not f.name.startswith(".")]
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
            inst_cfg = self.get_instance_config(instance_name)
            if not self.should_update(inst_cfg, "knowledge_cards"):
                print(f"[Sleeptime] Stage 2 skipped for {instance_name} (knowledge_cards disabled)")
                self.update_status(instance_name, "running", 85.0, "ナレッジ化をスキップしました")
            else:
                self.stage_2_knowledgeize(instance_name, db_type)

            # Stage 3: 知識熟成（opt-in。既定 OFF）。CLI の process_instance と
            # 同じゲートで判定し、Web/UI 実行でも有効時に走らせる。
            if self._should_run_stage_3(inst_cfg):
                self.update_status(instance_name, "running", 80.0, "知識の熟成中 (Stage 3)...")
                self.stage_3_mature_knowledge(instance_path)
            else:
                print(f"[Sleeptime] Stage 3 skipped for {instance_name} (knowledge_maturation disabled)")

            # DB バックアップ
            self.update_status(instance_name, "running", 90.0, "データベースのバックアップ中...")
            self.backup_database(instance_name)
            
            self.update_status(instance_name, "completed", 100.0, "完了しました")
            
        except Exception as e:
            print(f"[Sleeptime] Error: {e}")
            self.update_status(instance_name, "error", 0.0, str(e))

# Global Status Store
sleeptime_store = {}

def _build_arg_parser():
    import argparse

    parser = argparse.ArgumentParser(
        description="Butly Sleeptime housekeeping (デフォルト: 全インスタンス処理)"
    )
    sub = parser.add_subparsers(dest="command")

    bootstrap = sub.add_parser(
        "stage3-bootstrap",
        help="Stage 3 レビューキューを一括 drain する（計画 §6）",
    )
    bootstrap.add_argument("--instance", required=True, help="対象インスタンス名")
    bootstrap.add_argument(
        "--max-cards",
        type=int,
        default=None,
        help="安全上限の上書き (既定: knowledge_maturation_bootstrap_max_cards)",
    )
    return parser


if __name__ == "__main__":
    import sys

    args = _build_arg_parser().parse_args()
    hk = ButlySleeptime()

    if args.command == "stage3-bootstrap":
        result = hk.stage3_bootstrap(
            args.instance, max_cards_override=args.max_cards
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        sys.exit(0 if isinstance(result, dict) and result.get("status") == "completed" else 1)

    # サブコマンド無し: 従来どおり全インスタンスを処理
    hk.run()

    print("=== All Tasks Completed ===")
