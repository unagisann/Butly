import json
import os
from pathlib import Path
from datetime import datetime

# ★設定ファイルのインポート
try:
    from butly_core.config import SYSTEM_CONFIG
except ImportError:
    # パス解決のためのフォールバック
    import sys
    sys.path.append(str(Path(__file__).resolve().parent.parent.parent))
    from butly_core.config import SYSTEM_CONFIG

class ButlyMemory:
    def __init__(self, base_dir, instance_name="00_master"):
        self.base_dir = Path(base_dir)
        # インスタンスごとにフォルダを分離
        self.instance_dir = self.base_dir / "butly_core" / "instances" / instance_name
        
        # 各種パス定義
        self.mid_term_file = self.instance_dir / SYSTEM_CONFIG["paths"]["mid_term"]
        self.instruction_file = self.instance_dir / SYSTEM_CONFIG["paths"]["system_instruction"]
        self.short_term_json_dir = self.instance_dir / "short_term_json"
        self.floating_summary_dir = self.instance_dir / "floating_summaries"
        
        # アーカイブ階層（読み込み対象）
        self.archive_root = self.instance_dir / "memory_archive"
        self.archive_integrated = self.archive_root / "1_integrated"
        self.archive_knowledgeized = self.archive_root / "2_knowledgeized"
        self.archive_log = self.archive_root / "3_log"
        
        # フォルダ作成
        for p in [self.short_term_json_dir, self.archive_integrated, self.archive_knowledgeized, self.archive_log, self.floating_summary_dir]:
            p.mkdir(parents=True, exist_ok=True)
            
        if not self.mid_term_file.exists():
            self.mid_term_file.write_text("", encoding="utf-8")
        if not self.instruction_file.exists():
            self.instruction_file.write_text("あなたは有能なAIアシスタントです。", encoding="utf-8")

        self.floating_summary_file = self.instance_dir / "floating_summary.txt"
        if not self.floating_summary_file.exists():
            self.floating_summary_file.write_text("", encoding="utf-8")    

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

    def get_key_memory(self):
        """根幹記憶 (Key_Memory.txt) を読み込んで返す"""
        key_mem_file = self.instance_dir / SYSTEM_CONFIG["paths"]["key_memory"]
        try:
            text = key_mem_file.read_text(encoding="utf-8").strip()
            return text if text else ""
        except:
            return ""

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
            with open(glossary_file, "w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            return True
        except Exception as e:
            print(f"[Memory] Failed to save glossary: {e}")
            return False

    def get_mid_term_text_content(self):
        try:
            text = self.mid_term_file.read_text(encoding="utf-8").strip()
            # ★ 修正: Configから制限文字数を取得
            max_chars = self._get_config_value("memory", "max_mid_term_chars", SYSTEM_CONFIG["memory"]["max_mid_term_chars"])
            if len(text) > max_chars:
                # 安全なカット処理: 改行を探す
                cut_idx = text.find("\n", len(text) - max_chars)
                if cut_idx != -1:
                    return "...(古い記録を省略)...\n" + text[cut_idx+1:]
                return "...(古い記録を省略)...\n" + text[-max_chars:]
            return text
        except:
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

    def get_mid_term_relationship(self):
        """関係性スナップショット (mid_term_relationship.txt) を読み込んで返す"""
        rel_file = self.instance_dir / "mid_term_relationship.txt"
        try:
            if rel_file.exists():
                text = rel_file.read_text(encoding="utf-8").strip()
                return text if text else ""
        except Exception as e:
            print(f"[Memory] Failed to read mid_term_relationship: {e}")
        return ""

    def load_recent_sessions(self, limit=None):
        """
        最新の履歴をロードする。
        """
        if limit is None:
            limit = self._get_config_value("memory", "short_term_limit", SYSTEM_CONFIG["memory"]["short_term_limit"])

        try:
            target_dirs = [
                self.short_term_json_dir,
                self.archive_integrated,
                self.archive_knowledgeized
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
                                except: pass
                        # ▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲

                        file_msgs = data.get("messages") or data.get("contents") or []
                        if not file_msgs: continue
                        
                        ts_str = data.get("timestamp")
                        if ts_str:
                            for msg in file_msgs:
                                msg["timestamp"] = ts_str
                        
                        collected_files.append(file_msgs)
                        msg_count += len(file_msgs)
                        
                        if msg_count >= limit:
                            break
                except: continue
            
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

    def save_single_turn(self, user_text, model_text):
        """ローカル記憶用に short_term_json へ保存"""
        if not user_text and not model_text: return None
        
        save_path = self.short_term_json_dir
        save_path.mkdir(parents=True, exist_ok=True)
        
        file_name = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        full_path = save_path / file_name
        
        turn_data = {
            "timestamp": datetime.now().isoformat(),
            "messages": [
                {"role": "user", "parts": [user_text]},
                {"role": "model", "parts": [model_text]}
            ]
        }
        
        with open(full_path, "w", encoding="utf-8") as f:
            json.dump(turn_data, f, ensure_ascii=False, indent=2)
            
        return file_name

    def get_floating_summary(self):
        """
        現在保持している浮動要約(floating summary)のテキストをすべて結合して返す
        1. 従来の floating_summary.txt (互換性のため)
        2. 新しい floating_summaries フォルダ内の各ファイル
        """
        combined = ""
        
        # 1. 従来ファイルの読み込み
        try:
            if self.floating_summary_file.exists():
                legacy_text = self.floating_summary_file.read_text(encoding="utf-8").strip()
                if legacy_text:
                    combined += legacy_text + "\n\n"
        except Exception as e:
            print(f"[Memory] Failed to read legacy floating summary: {e}")
            
        # 2. 新フォルダ内のファイル読み込み
        try:
            if self.floating_summary_dir.exists():
                # 古いものから順に読み込む (時系列を保つため)
                for summary_file in sorted(self.floating_summary_dir.glob("*.txt"), key=os.path.getmtime):
                    text = summary_file.read_text(encoding="utf-8").strip()
                    if text:
                        combined += f"--- Source: {summary_file.name} ---\n{text}\n\n"
        except Exception as e:
            print(f"[Memory] Failed to read floating summaries dir: {e}")
            
        return combined.strip()

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
            return # 何もしない
            
        # 3. 溢れたファイル（古いものリスト）
        overflow_files = sorted_files[keep_files:]
        
        print(f"[Memory] Compressing {len(overflow_files)} old conversations...")
        
        for json_file in overflow_files:
            try:
                # 中身を読み込む
                with open(json_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    
                messages = data.get("messages", [])
                if not messages: continue
                
                # テキスト化（Brainに読ませるため）
                conv_text = ""
                timestamp = data.get("timestamp", "")
                for m in messages:
                    role = m.get("role", "unknown")
                    text = m.get("parts", [""])[0]
                    conv_text += f"[{role}]: {text}\n"
                
                # Brainで要約
                # インスタンス設定を読み込んで渡す
                instance_config = self._load_config()
                summary = brain.summarize_conversation(conv_text, override_config=instance_config)
                
                # ★修正: 個別ファイルとして保存 (Race Condition回避)
                # JSONファイル名に対応する .txt を作成
                summary_filename = json_file.stem + ".txt"
                summary_path = self.floating_summary_dir / summary_filename
                
                with open(summary_path, "w", encoding="utf-8") as f:
                    f.write(f"Time: {timestamp}\n{summary}\n")
                    
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
            self.archive_knowledgeized
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
        