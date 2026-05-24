import os
import json
import re
from pathlib import Path

from butly_core.io_utils import atomic_write_text


class InstanceManager:
    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        self.instances_dir = self.base_dir / "butly_core" / "instances"
        self.instances_dir.mkdir(parents=True, exist_ok=True)

    def create_instance(
        self,
        name,
        template_text,
        key_memory="",
        agent_profile: dict = None,
        user_profile: dict = None,
    ):
        """新しい人格フォルダと構成を作成"""
        # 半角英数チェック
        if not re.match(r"^[a-zA-Z0-9_]+$", name):
            return False, "名前は半角英数字、アンダースコアのみで入力してください。"

        # 前後の空白だけは念のため除去（フォルダ名のトラブル防止）
        name = name.strip()

        folder_name = name
        new_instance_dir = self.instances_dir / folder_name

        if new_instance_dir.exists():
            return False, "その名前のインスタンスは既に存在します。"

        try:
            # 1. フォルダ作成
            new_instance_dir.mkdir()
            (new_instance_dir / "short_term_json").mkdir()

            # アーカイブ階層
            archive_root = new_instance_dir / "memory_archive"
            (archive_root / "1_integrated").mkdir(parents=True)
            (archive_root / "2_knowledgeized").mkdir(parents=True)
            (archive_root / "3_log").mkdir(parents=True)

            # 2. 空ファイルの作成
            (new_instance_dir / "mid_term.txt").write_text("", encoding="utf-8")
            (new_instance_dir / "current_cache_id.txt").write_text("", encoding="utf-8")
            (new_instance_dir / "Key_Memory.txt").write_text(
                key_memory, encoding="utf-8"
            )

            # Key_Memory.yaml の作成
            import yaml

            km_yaml_data = {"version": 1, "entries": []}
            (new_instance_dir / "Key_Memory.yaml").write_text(
                yaml.dump(
                    km_yaml_data,
                    allow_unicode=True,
                    default_flow_style=False,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            # 3. system_instruction.txt の作成
            # ユーザー指定のテンプレ + プロジェクト名
            final_instruction = template_text.replace(
                "プロジェクト名：", f"プロジェクト名：{name}"
            )
            (new_instance_dir / "system_instruction.txt").write_text(
                final_instruction, encoding="utf-8"
            )

            # 3.5 LOW版ファイルの作成
            (new_instance_dir / "system_instruction_low.txt").write_text(
                "# LOW版: 通常版を簡略化した性格設定を記述してください。\n"
                "# 空のままでも動作します（通常版がフォールバックとして使用されます）。\n",
                encoding="utf-8",
            )
            (new_instance_dir / "Key_Memory_low.txt").write_text(
                "# LOW版: 通常版を簡略化した根幹記憶を記述してください。\n"
                "# 空のままでも動作します（通常版がフォールバックとして使用されます）。\n",
                encoding="utf-8",
            )

            # 4. config.json (デフォルト設定)
            default_agent_profile = {
                "ai_name": "",
                "ai_gender": "",
                "locale": "ja",
            }
            if agent_profile:
                default_agent_profile.update(agent_profile)

            default_user_profile = {
                "user_name": "",
                "preferred_call": "",
                "gender": "",
                "birthday": "",
                "location": "",
            }
            if user_profile:
                default_user_profile.update(user_profile)

            default_config = {
                "agent_profile": default_agent_profile,
                "user_profile": default_user_profile,
                "brain": {
                    "use_context_cache": False,
                    "default_use_google_search": True,
                    "search_limit": 3,
                    "fallback_fetch_limit": 50,
                    "keyword_hit_threshold": 5,
                    "cache_ttl_hours": 3,
                    "readable_instances": ["self"],
                },
                "chat": {},
                "memory": {},
                "sleeptime": {
                    "max_digest_chars": 3000,
                    "max_relationship_chars": 5000,
                    "relationship_update_interval_days": 7,
                    "summary_max_output_tokens": 4096,
                    "knowledge_max_output_tokens": 8192,
                },
            }
            (new_instance_dir / "config.json").write_text(
                json.dumps(default_config, indent=4, ensure_ascii=False),
                encoding="utf-8",
            )

            # 5. glossary.yaml (空の辞書)
            (new_instance_dir / "glossary.yaml").write_text(
                "version: 1\nentries: []\n", encoding="utf-8"
            )

            return True, f"プロジェクト '{folder_name}' を作成しました。"

        except Exception as e:
            return False, f"作成エラー: {str(e)}"

    def update_instruction(self, instance_name, new_text):
        """性格設定を上書き保存"""
        target_file = self.instances_dir / instance_name / "system_instruction.txt"
        try:
            atomic_write_text(target_file, new_text)
            return True, "性格設定を更新しました。"
        except Exception as e:
            return False, str(e)

    def get_instance_prompts(self, instance_name):
        """インスタンスごとのプロンプトテキストを取得"""
        instance_dir = self.instances_dir / instance_name
        if not instance_dir.exists():
            return None

        # system_instruction.txt
        si_path = instance_dir / "system_instruction.txt"
        system_instruction = (
            si_path.read_text(encoding="utf-8") if si_path.exists() else ""
        )

        # Key_Memory.txt
        km_path = instance_dir / "Key_Memory.txt"
        key_memory = km_path.read_text(encoding="utf-8") if km_path.exists() else ""

        return {"system_instruction": system_instruction, "key_memory": key_memory}

    def update_instance_prompts(self, instance_name, data):
        """インスタンスごとのプロンプトテキストを保存"""
        instance_dir = self.instances_dir / instance_name
        if not instance_dir.exists():
            return False, "インスタンスが存在しません。"

        try:
            if "system_instruction" in data:
                atomic_write_text(
                    instance_dir / "system_instruction.txt",
                    data["system_instruction"],
                )
            if "key_memory" in data:
                atomic_write_text(
                    instance_dir / "Key_Memory.txt",
                    data["key_memory"],
                )
            return True, "プロンプトを更新しました。"
        except Exception as e:
            return False, f"保存エラー: {str(e)}"

    def get_instance_config(self, instance_name):
        """インスタンスごとの設定(config.json)を取得"""
        # instance_name check
        if not (self.instances_dir / instance_name).exists():
            return {}

        config_path = self.instances_dir / instance_name / "config.json"
        if not config_path.exists():
            return {}
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except:
            return {}

    def update_instance_config(self, instance_name, new_config):
        """インスタンスごとの設定を保存"""
        # instance_name check
        if not (self.instances_dir / instance_name).exists():
            return False, "インスタンスが存在しません。"

        config_path = self.instances_dir / instance_name / "config.json"
        try:
            # Validate JSON serializable
            json_str = json.dumps(new_config, indent=4, ensure_ascii=False)
            atomic_write_text(config_path, json_str)
            return True, "設定を更新しました。"
        except Exception as e:
            return False, f"設定保存エラー: {str(e)}"

    def rename_instance(self, old_name, new_name):
        """インスタンス名を変更する"""
        # Validate new name
        if not re.match(r"^[a-zA-Z0-9_]+$", new_name):
            return False, "名前は半角英数字、アンダースコアのみで入力してください。"

        new_name = new_name.strip()

        # Find old directory
        old_dir = self.instances_dir / old_name
        if not old_dir.exists():
            return False, "変更元のインスタンスが存在しません。"

        new_dir = self.instances_dir / new_name

        if new_dir.exists():
            return False, "その名前のインスタンスは既に存在します。"

        try:
            old_dir.rename(new_dir)

            # ========================================
            # DB内のtypeカラムを更新
            # ========================================
            db_path = new_dir / "butly_memory.db"
            if db_path.exists():
                try:
                    import sqlite3

                    conn = sqlite3.connect(db_path)
                    conn.execute("PRAGMA journal_mode=WAL;")
                    conn.execute(
                        "UPDATE knowledge_cards SET type = ? WHERE type = ?",
                        (new_name, old_name),
                    )
                    conn.execute(
                        "UPDATE knowledge_cards SET type = REPLACE(type, ?, ?) "
                        "WHERE type LIKE ?",
                        (old_name + "/", new_name + "/", old_name + "/%"),
                    )
                    updated = conn.total_changes
                    conn.commit()
                    conn.close()
                    print(f"[InstanceManager] Updated {updated} type entries in DB")
                except Exception as e:
                    print(f"[InstanceManager] DB type update warning: {e}")

            # ========================================
            # 他インスタンスの readable_instances を更新
            # ========================================
            for inst_dir in self.instances_dir.iterdir():
                if not inst_dir.is_dir() or inst_dir.name == new_name:
                    continue
                config_path = inst_dir / "config.json"
                if not config_path.exists():
                    continue
                try:
                    config = json.loads(config_path.read_text(encoding="utf-8"))
                    readable = config.get("brain", {}).get("readable_instances", [])
                    if old_name in readable:
                        readable = [new_name if r == old_name else r for r in readable]
                        config["brain"]["readable_instances"] = readable
                        atomic_write_text(
                            config_path,
                            json.dumps(config, indent=2, ensure_ascii=False),
                        )
                        print(
                            f"[InstanceManager] Updated readable_instances in {inst_dir.name}"
                        )
                except Exception:
                    pass

            # ========================================
            # エージェントのparent設定を更新
            # ========================================
            agents_dir = new_dir / "agents"
            if agents_dir.exists():
                for agent_dir in agents_dir.iterdir():
                    if not agent_dir.is_dir():
                        continue
                    agent_config_path = agent_dir / "config.json"
                    if not agent_config_path.exists():
                        continue
                    try:
                        agent_config = json.loads(
                            agent_config_path.read_text(encoding="utf-8")
                        )
                        if agent_config.get("parent") == old_name:
                            agent_config["parent"] = new_name
                            atomic_write_text(
                                agent_config_path,
                                json.dumps(agent_config, indent=2, ensure_ascii=False),
                            )
                            print(
                                f"[InstanceManager] Updated parent in agent {agent_dir.name}"
                            )
                    except Exception:
                        pass

            # Update system_instruction.txt if it contains the old name
            si_path = new_dir / "system_instruction.txt"
            if si_path.exists():
                content = si_path.read_text(encoding="utf-8")
                if f"プロジェクト名：{old_name}" in content:
                    new_content = content.replace(
                        f"プロジェクト名：{old_name}", f"プロジェクト名：{new_name}"
                    )
                    atomic_write_text(si_path, new_content)

            return True, new_name
        except Exception as e:
            return False, f"リネームエラー: {str(e)}"

    def delete_instance(self, instance_name):
        """インスタンスを削除する（フォルダごと）"""
        target_dir = self.instances_dir / instance_name
        if not target_dir.exists():
            return False, "インスタンスが存在しません。"

        try:
            import shutil

            shutil.rmtree(target_dir)
            return True, f"インスタンス '{instance_name}' を削除しました。"
        except Exception as e:
            return False, f"削除エラー: {str(e)}"
