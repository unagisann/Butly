import json
import os
import re
from pathlib import Path
from datetime import datetime


def _parse_session_filename_timestamp(name: str):
    """`session_YYYYMMDD_HHMMSS[_ffffff](.txt)` 形式から日時を抽出。

    Returns datetime or None.
    """
    m = re.match(r"session_(\d{8})_(\d{6})", name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _strip_legacy_time_line(text: str) -> str:
    """旧形式 session digest の冒頭 `Time: ...` 行を除去 (後方互換)。"""
    lines = text.split("\n", 1)
    if lines and lines[0].startswith("Time: "):
        return lines[1] if len(lines) > 1 else ""
    return text


def _format_relative_time(dt: datetime, now: datetime) -> str:
    """相対時間表現を返す。Sleeptime は日次なので主に分/時間オーダー。"""
    delta = now - dt
    seconds = delta.total_seconds()
    if seconds < 60:
        return "たった今"
    if seconds < 3600:
        return f"約{int(seconds // 60)}分前"
    if seconds < 86400:
        hours = seconds / 3600
        if hours < 2:
            # 1〜2 時間の境界は 0.5 刻みでより具体的に
            return f"約{hours:.1f}時間前"
        return f"約{int(hours)}時間前"
    days = int(seconds // 86400)
    return f"約{days}日前"


# ★設定ファイルのインポート
try:
    from butly_core.config import SYSTEM_CONFIG
except ImportError:
    # パス解決のためのフォールバック
    import sys

    sys.path.append(str(Path(__file__).resolve().parent.parent.parent))
    from butly_core.config import SYSTEM_CONFIG


def _migrate_legacy_agent(config: dict) -> dict:
    """旧 `agent` セクションを `agent_profile` + `user_profile` に変換（in-memory、ファイルは変更しない）。"""
    if (
        "agent" in config
        and "agent_profile" not in config
        and "user_profile" not in config
    ):
        old = config["agent"]
        config["agent_profile"] = {
            "ai_name": old.get("ai_name", ""),
            "ai_gender": "",
            "locale": old.get("locale", "ja"),
        }
        config["user_profile"] = {
            "user_name": old.get("user_name", ""),
            "preferred_call": old.get("nickname", ""),
            "gender": old.get("gender", ""),
            "birthday": old.get("birthday", ""),
            "location": "",
        }
    return config


class ButlyMemory:
    def __init__(self, base_dir, instance_name="00_master"):
        self.base_dir = Path(base_dir)
        # インスタンスごとにフォルダを分離
        self.instance_dir = self.base_dir / "butly_core" / "instances" / instance_name

        # 各種パス定義
        self.instruction_file = (
            self.instance_dir / SYSTEM_CONFIG["paths"]["system_instruction"]
        )
        self.short_term_json_dir = self.instance_dir / "short_term_json"
        self.session_digest_dir = self.instance_dir / "session_digests"
        self.legacy_floating_summary_dir = self.instance_dir / "floating_summaries"

        # アーカイブ階層（読み込み対象）
        self.archive_root = self.instance_dir / "memory_archive"
        self.archive_integrated = self.archive_root / "1_integrated"
        self.archive_knowledgeized = self.archive_root / "2_knowledgeized"
        self.archive_log = self.archive_root / "3_log"

        # フォルダ作成
        for p in [
            self.short_term_json_dir,
            self.archive_integrated,
            self.archive_knowledgeized,
            self.archive_log,
            self.session_digest_dir,
        ]:
            p.mkdir(parents=True, exist_ok=True)

        if not self.instruction_file.exists():
            self.instruction_file.write_text(
                "あなたは有能なAIアシスタントです。", encoding="utf-8"
            )

        self.session_digest_file = self.instance_dir / "session_digest.txt"
        if not self.session_digest_file.exists():
            self.session_digest_file.write_text("", encoding="utf-8")

        # Backward-compatible attribute names for old callers/tests.
        self.floating_summary_dir = self.session_digest_dir
        self.floating_summary_file = self.instance_dir / "floating_summary.txt"

    def _load_config(self):
        """インスタンス固有のconfig.jsonを読み込む"""
        config_path = self.instance_dir / "config.json"
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except:
                pass
        return {}

    def _get_config_value(self, section, key, default):
        """インスタンス設定 > デフォルト設定 の優先順位で値を取得"""
        instance_config = self._load_config()
        if section in instance_config and key in instance_config[section]:
            return instance_config[section][key]
        return default

    def get_system_instruction(self):
        try:
            return self.instruction_file.read_text(encoding="utf-8").strip()
        except:
            return "あなたは有能な執事です。"

    def get_system_instruction_low(self) -> str:
        """LOW版system_instructionを取得。ファイルがないか空なら通常版にフォールバック。"""
        low_path = self.instance_dir / "system_instruction_low.txt"
        if low_path.exists():
            content = low_path.read_text(encoding="utf-8").strip()
            lines = [
                l
                for l in content.split("\n")
                if l.strip() and not l.strip().startswith("#")
            ]
            if lines:
                return "\n".join(lines)
        return self.get_system_instruction()

    def get_key_memory_low(self) -> str:
        """LOW版Key_Memoryを取得。Key_Memory_low.txt → YAML low モード → 通常版にフォールバック。"""
        low_path = self.instance_dir / "Key_Memory_low.txt"
        if low_path.exists():
            content = low_path.read_text(encoding="utf-8").strip()
            lines = [
                l
                for l in content.split("\n")
                if l.strip() and not l.strip().startswith("#")
            ]
            if lines:
                return self._compose_key_memory("\n".join(lines))

        # YAML low モード
        from butly_core.core.key_memory import load_yaml, yaml_to_text, YAML_FILENAME

        yaml_path = self.instance_dir / YAML_FILENAME
        if yaml_path.exists():
            entries = load_yaml(yaml_path)
            if entries:
                body = yaml_to_text(entries, mode="low")
                return body

        return self.get_key_memory()

    def _load_config_migrated(self) -> dict:
        """config.json を読み込み、旧 `agent` セクションを in-memory で新スキーマへ変換する。"""
        return _migrate_legacy_agent(self._load_config())

    _AGENT_KNOWN_KEYS = {"ai_name", "ai_gender", "locale"}

    def get_agent_profile(self) -> dict:
        """
        AI 側プロファイル（SI 注入用）を返す。
        instance config["agent_profile"]（旧 agent セクションからのマイグレーション含む）を返す。
        インスタンス config に該当セクションがない場合は空値辞書を返す。
        固定キーに加え、ユーザーが追加したカスタムフィールドもそのまま含む。

        Returns:
            {"ai_name": str, "ai_gender": str, "locale": str, ...custom_fields}
        """
        cfg = self._load_config_migrated()
        ap = cfg.get("agent_profile", {})
        result = {
            "ai_name": ap.get("ai_name", ""),
            "ai_gender": ap.get("ai_gender", ""),
            "locale": ap.get("locale", "ja"),
        }
        for key, value in ap.items():
            if key not in self._AGENT_KNOWN_KEYS:
                result[key] = value
        return result

    _USER_KNOWN_KEYS = {"user_name", "preferred_call", "gender", "birthday", "location"}

    def get_user_profile(self) -> dict:
        """
        ユーザー側プロファイル（KM 注入用）を返す。
        instance config["user_profile"]（旧 agent セクションからのマイグレーション含む）を返す。
        インスタンス config に該当セクションがない場合は空値辞書を返す。
        固定キーに加え、ユーザーが追加したカスタムフィールドもそのまま含む。

        Returns:
            {"user_name": str, "preferred_call": str, "gender": str,
             "birthday": str, "location": str, ...custom_fields}
        """
        cfg = self._load_config_migrated()
        up = cfg.get("user_profile", {})
        result = {
            "user_name": up.get("user_name", ""),
            "preferred_call": up.get("preferred_call", ""),
            "gender": up.get("gender", ""),
            "birthday": up.get("birthday", ""),
            "location": up.get("location", ""),
        }
        for key, value in up.items():
            if key not in self._USER_KNOWN_KEYS:
                result[key] = value
        return result

    def get_agent_profile_header(self) -> str:
        """SI 先頭に注入する Agent Profile ブロックを生成する。ai_name が空なら空文字。"""
        profile = self.get_agent_profile()
        ai_name = profile.get("ai_name", "").strip()
        if not ai_name:
            return ""

        locale = profile.get("locale", "ja")
        lines = ["# Agent Profile"]
        if locale == "ja":
            lines.append(f"私は {ai_name}。")
        else:
            lines.append(f"I am {ai_name}.")

        ai_gender = profile.get("ai_gender", "").strip()
        if ai_gender:
            lines.append(f"Gender: {ai_gender}")

        # カスタムフィールドを追加
        for key, value in profile.items():
            if key not in self._AGENT_KNOWN_KEYS and value:
                label = key.replace("_", " ").title()
                lines.append(f"{label}: {value}")

        return "\n".join(lines)

    def get_user_profile_header(self) -> str:
        """KM 先頭に注入する User Profile ブロックを生成する。全項目空なら空文字。"""
        profile = self.get_user_profile()
        order = [
            ("Name", profile.get("user_name", "")),
            ("Preferred call", profile.get("preferred_call", "")),
            ("Gender", profile.get("gender", "")),
            ("Location", profile.get("location", "")),
            ("Date of Birth", profile.get("birthday", "")),
        ]
        # カスタムフィールドを追加
        for key, value in profile.items():
            if key not in self._USER_KNOWN_KEYS and value:
                label = key.replace("_", " ").title()
                order.append((label, value))

        lines = [f"- {label}: {value}" for label, value in order if value]
        if not lines:
            return ""
        return "=== User Profile ===\n" + "\n".join(lines)

    def get_key_memory(self) -> str:
        """
        根幹記憶を読み込み、User Profile ブロックを先頭に付与して返す。
        Key_Memory.yaml を先に探し、なければ Key_Memory.txt にフォールバック。
        プロファイルは config["user_profile"] から動的生成。
        """
        from butly_core.core.key_memory import load_yaml, yaml_to_text, YAML_FILENAME

        yaml_path = self.instance_dir / YAML_FILENAME
        if yaml_path.exists():
            entries = load_yaml(yaml_path)
            if entries:
                body = yaml_to_text(entries, mode="high")
                return self._compose_key_memory(body)

        # フォールバック: 旧 Key_Memory.txt
        key_mem_file = self.instance_dir / SYSTEM_CONFIG["paths"]["key_memory"]
        try:
            body = key_mem_file.read_text(encoding="utf-8").strip()
        except Exception:
            body = ""
        return self._compose_key_memory(body)

    def get_key_memory_entries(self) -> list[dict]:
        """Key_Memory.yaml のエントリ一覧を返す。YAML がなければ空リスト。"""
        from butly_core.core.key_memory import load_yaml, YAML_FILENAME

        yaml_path = self.instance_dir / YAML_FILENAME
        return load_yaml(yaml_path)

    def save_key_memory_entries(self, entries: list[dict]) -> None:
        """Key_Memory.yaml にエントリを保存する。"""
        from butly_core.core.key_memory import save_yaml, YAML_FILENAME

        yaml_path = self.instance_dir / YAML_FILENAME
        save_yaml(entries, yaml_path)

    def _compose_key_memory(self, body: str) -> str:
        """User Profile ヘッダと Core Memories 本文を合成する。"""
        header = self.get_user_profile_header()
        body = (body or "").strip()
        if header and body:
            return header + "\n\n=== Core Memories ===\n" + body
        if header:
            return header
        return body

    def get_glossary(self) -> str:
        """Glossary (共通言語辞書) を読み込み、active エントリをテキスト形式で返す。"""
        glossary_file = self.instance_dir / "glossary.yaml"
        if not glossary_file.exists():
            return ""
        try:
            import yaml

            with open(glossary_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if not data or "entries" not in data:
                return ""
            lines = []
            for entry in data["entries"]:
                if entry.get("status") != "active":
                    continue
                term = entry.get("term", "")
                definition = entry.get("definition", "")
                if term and definition:
                    lines.append(f"- {term}: {definition}")
            return "\n".join(lines)
        except Exception as e:
            print(f"[Memory] Failed to read glossary: {e}")
            return ""

    def get_glossary_raw(self) -> dict:
        """Glossary の生データ (dict) を返す。UI/API 用。"""
        glossary_file = self.instance_dir / "glossary.yaml"
        if not glossary_file.exists():
            return {"version": 1, "entries": []}
        try:
            import yaml

            with open(glossary_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return data if data else {"version": 1, "entries": []}
        except Exception as e:
            print(f"[Memory] Failed to read glossary raw: {e}")
            return {"version": 1, "entries": []}

    def save_glossary(self, data: dict) -> bool:
        """Glossary データを YAML ファイルに保存する。"""
        glossary_file = self.instance_dir / "glossary.yaml"
        try:
            import yaml

            from butly_core.io_utils import atomic_write_text

            text = yaml.dump(
                data,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            )
            atomic_write_text(glossary_file, text)
            return True
        except Exception as e:
            print(f"[Memory] Failed to save glossary: {e}")
            return False

    def get_raw_memory(self) -> str:
        """raw_memory_cache.txt を読み込んで返す。
        キャッシュは Sleeptime 実行時に生成される。"""
        from butly_core.core.raw_memory_reader import CACHE_FILENAME

        cache_path = self.instance_dir / CACHE_FILENAME
        try:
            if cache_path.exists():
                return cache_path.read_text(encoding="utf-8").strip()
        except Exception as e:
            print(f"[Memory] Failed to read raw_memory_cache: {e}")
        return ""

    def get_mid_term_digest(self):
        """エピソード付き事実ダイジェスト (mid_term_digest.txt) を読み込んで返す"""
        digest_file = self.instance_dir / "mid_term_digest.txt"
        try:
            if digest_file.exists():
                text = digest_file.read_text(encoding="utf-8").strip()
                return text if text else ""
        except Exception as e:
            print(f"[Memory] Failed to read mid_term_digest: {e}")
        return ""

    def get_recent_snapshot(self):
        """近況スナップショット (recent_snapshot.txt) を読み込んで返す。
        旧ファイル mid_term_relationship.txt へのフォールバック付き。"""
        for filename in ("recent_snapshot.txt", "mid_term_relationship.txt"):
            rel_file = self.instance_dir / filename
            try:
                if rel_file.exists():
                    text = rel_file.read_text(encoding="utf-8").strip()
                    if text:
                        return text
            except Exception as e:
                print(f"[Memory] Failed to read {filename}: {e}")
        return ""

    # 後方互換エイリアス
    def get_mid_term_relationship(self):
        return self.get_recent_snapshot()

    def load_recent_sessions(self, limit=None):
        """
        最新の履歴をロードする。
        """
        if limit is None:
            limit = self._get_config_value(
                "memory",
                "short_term_limit",
                SYSTEM_CONFIG["memory"]["short_term_limit"],
            )

        try:
            target_dirs = [
                self.short_term_json_dir,
                self.archive_integrated,
                self.archive_knowledgeized,
            ]

            all_json_files = []
            for d in target_dirs:
                if d.exists():
                    all_json_files.extend(list(d.glob("**/*.json")))

            # 新しい順にソート（最新が先頭）
            sorted_files = sorted(all_json_files, key=os.path.getmtime, reverse=True)

            messages = []
            latest_timestamp = None

            # 必要な数だけ集める
            collected_files = []
            msg_count = 0

            for file_path in sorted_files:
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)

                        # ▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼
                        # ★修正: 最新のファイルからタイムスタンプを取得する処理を追加
                        if latest_timestamp is None:
                            ts_str = data.get("timestamp")
                            if ts_str:
                                try:
                                    latest_timestamp = datetime.fromisoformat(ts_str)
                                except:
                                    pass
                        # ▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲

                        file_msgs = data.get("messages") or data.get("contents") or []
                        if not file_msgs:
                            continue

                        ts_str = data.get("timestamp")
                        if ts_str:
                            for msg in file_msgs:
                                msg["timestamp"] = ts_str

                        collected_files.append(file_msgs)
                        msg_count += len(file_msgs)

                        if msg_count >= limit:
                            break
                except:
                    continue

            # ここまでは「新しい順」に集めたので、リストを反転させて「古い順（時系列）」に戻す
            for file_msgs in reversed(collected_files):
                messages.extend(file_msgs)

            # 最後に溢れた分を先頭からカットして Limit に合わせる
            if len(messages) > limit:
                messages = messages[-limit:]

            return messages, latest_timestamp

        except Exception as e:
            print(f"[Memory] Load Error: {e}")
            return [], None

    def save_single_turn(self, user_text, model_text, meta=None):
        """ローカル記憶用に short_term_json へ保存。

        meta: 話者帰属メタ (person_id / display_name / lane / source /
        channel_key)。指定時は user メッセージに構造化メタデータとして載せる。
        None なら従来形式のまま（meta 欠落は owner / direct / web と解釈される
        後方互換規則があるため、Web チャットはこの経路で正しい）。
        """
        if not user_text and not model_text:
            return None

        save_path = self.short_term_json_dir
        save_path.mkdir(parents=True, exist_ok=True)

        created_at = datetime.now()
        file_name = f"session_{created_at.strftime('%Y%m%d_%H%M%S_%f')}.json"
        full_path = save_path / file_name

        user_msg = {"role": "user", "parts": [user_text]}
        if meta:
            user_msg["meta"] = meta

        turn_data = {
            "timestamp": created_at.isoformat(),
            "messages": [
                user_msg,
                {"role": "model", "parts": [model_text]},
            ],
        }

        from butly_core.io_utils import atomic_write_text

        atomic_write_text(
            full_path,
            json.dumps(turn_data, ensure_ascii=False, indent=2),
        )

        return file_name

    def get_session_digest(self):
        """
        recent sessions から溢れた会話の圧縮ログ(session digest)を結合して返す。

        Format (現行):
            --- 約N時間前 ---
            <要約本文>

        ファイル名や絶対タイムスタンプは LLM のノイズになりやすいため、
        相対時間ヘッダー (例: 「約30分前」「約2時間前」) で代替する。
        Sleeptime が日次で整理する前提なので、半日以内のスパンしか出ない想定。

        旧形式 (`floating_summary.txt` / `floating_summaries/` および
        `Time: 2026-...` を 1 行目に含むファイル) との後方互換あり。
        """
        from datetime import datetime

        now = datetime.now()
        combined = ""

        for file_path, label in [
            (self.session_digest_file, "session digest"),
            (self.floating_summary_file, "legacy floating summary"),
        ]:
            try:
                if file_path.exists():
                    text = file_path.read_text(encoding="utf-8").strip()
                    if text:
                        combined += _strip_legacy_time_line(text) + "\n\n"
            except Exception as e:
                print(f"[Memory] Failed to read {label}: {e}")

        for summary_dir, label in [
            (self.session_digest_dir, "session digests dir"),
            (self.legacy_floating_summary_dir, "legacy floating summaries dir"),
        ]:
            try:
                if summary_dir.exists():
                    for summary_file in sorted(
                        summary_dir.glob("*.txt"), key=os.path.getmtime
                    ):
                        raw = summary_file.read_text(encoding="utf-8").strip()
                        if not raw:
                            continue

                        cleaned = _strip_legacy_time_line(raw)
                        file_dt = _parse_session_filename_timestamp(
                            summary_file.name
                        ) or datetime.fromtimestamp(summary_file.stat().st_mtime)
                        rel = _format_relative_time(file_dt, now)
                        combined += f"--- {rel} ---\n{cleaned}\n\n"
            except Exception as e:
                print(f"[Memory] Failed to read {label}: {e}")

        return combined.strip()

    def get_floating_summary(self):
        """Backward-compatible alias for get_session_digest()."""
        return self.get_session_digest()

    def maintain_memory(self, brain):
        """
        短期記憶が溢れていないかチェックし、溢れていれば要約してアーカイブおよび個別要約ファイル作成
        Limit: 設定(short_term_limit)ファイル数を残し、それより古いものを圧縮
        """
        instance_config = self._load_config()
        keep_files = instance_config.get("memory", {}).get("short_term_limit", 3)

        # 1. ファイル一覧取得
        all_json_files = list(self.short_term_json_dir.glob("**/*.json"))
        # 新しい順にソート
        sorted_files = sorted(all_json_files, key=os.path.getmtime, reverse=True)

        # 2. 溢れているか判定
        if len(sorted_files) <= keep_files:
            return  # 何もしない

        # 3. 溢れたファイル（古いものリスト）
        overflow_files = sorted_files[keep_files:]

        print(f"[Memory] Compressing {len(overflow_files)} old conversations...")

        # 要約モデルにアシスタント名を捏造させないため、実名ロールラベルを用意する。
        # 匿名ラベル ([model] / [user]) のまま渡すと、summary モデルが物語化の際に
        # "ルナ" のような架空のアシスタント名を補完してしまう (session digest 汚染の原因)。
        agent_profile = self.get_agent_profile()
        user_profile = self.get_user_profile()
        agent_name = (agent_profile.get("ai_name") or "").strip() or SYSTEM_CONFIG[
            "agent"
        ].get("agent_name", "AI")
        user_name = (
            user_profile.get("preferred_call") or user_profile.get("user_name") or ""
        ).strip() or SYSTEM_CONFIG["agent"].get("user_name", "User")

        # 先に全ファイルを読み、バッチ全体で複数話者かを判定する。
        # 1 ファイル = 1 ターンのため、ファイル単体では複数話者になり得ない。
        from butly_core.core.turn_meta import has_multiple_speakers, user_label

        loaded_files = []
        for json_file in overflow_files:
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    print(f"[Memory] Skipping non-dict JSON: {json_file.name}")
                    continue
                loaded_files.append((json_file, data))
            except Exception as e:
                print(f"[Memory] Error reading {json_file.name}: {e}")

        multi_speaker = has_multiple_speakers(
            [m for _, d in loaded_files for m in d.get("messages", [])]
        )

        for json_file, data in loaded_files:
            try:
                messages = data.get("messages", [])
                if not messages:
                    continue

                # テキスト化（Brainに読ませるため）。役割は実名でラベリングする。
                # 複数話者バッチでは user を meta の display_name でラベリングする。
                conv_text = ""
                timestamp = data.get("timestamp", "")
                for m in messages:
                    role = m.get("role", "unknown")
                    text = m.get("parts", [""])[0]
                    if role == "user":
                        label = user_label(m, user_name, multi_speaker=multi_speaker)
                    elif role in ("model", "assistant"):
                        label = agent_name
                    else:
                        label = role
                    conv_text += f"[{label}]: {text}\n"

                # Brainで要約
                # インスタンス設定を読み込んで渡す
                instance_config = self._load_config()
                summary = brain.summarize_conversation(
                    conv_text, override_config=instance_config
                )

                # ★修正: 個別ファイルとして保存 (Race Condition回避)
                # JSONファイル名に対応する .txt を作成
                summary_filename = json_file.stem + ".txt"
                summary_path = self.session_digest_dir / summary_filename

                from butly_core.io_utils import atomic_write_text

                atomic_write_text(summary_path, f"{summary}\n")

                # ファイルをアーカイブフォルダへ移動
                new_path = self.archive_integrated / json_file.name
                os.rename(json_file, new_path)
                print(f"[Memory] Archived: {json_file.name} -> {summary_filename}")

            except Exception as e:
                print(f"[Memory] Error processing {json_file.name}: {e}")

    def get_last_interaction_time(self):
        """
        全フォルダを走査して、最も新しい対話の日時を特定します。
        """
        target_dirs = [
            self.short_term_json_dir,
            self.archive_integrated,
            self.archive_knowledgeized,
        ]

        all_json_files = []
        for d in target_dirs:
            if d.exists():
                all_json_files.extend(list(d.glob("**/*.json")))

        if not all_json_files:
            return None

        # ファイルの更新日時(mtime)が最新のものを特定
        latest_file = max(all_json_files, key=os.path.getmtime)

        try:
            with open(latest_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                ts_str = data.get("timestamp")
                if ts_str:
                    return datetime.fromisoformat(ts_str)
        except:
            pass

        # JSON内のタイムスタンプが取れない場合はファイルの更新日時を使用
        return datetime.fromtimestamp(latest_file.stat().st_mtime)
