import os
import json
import re
from pathlib import Path

class InstanceManager:
    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        self.instances_dir = self.base_dir / "butly_core" / "instances"
        self.instances_dir.mkdir(parents=True, exist_ok=True)

    def get_next_id(self):
        """既存のフォルダを走査して、次の連番ID (xx) を決定する"""
        existing_ids = []
        for d in self.instances_dir.iterdir():
            if d.is_dir() and "_" in d.name:
                try:
                    # "01_flutter" -> 1
                    prefix = d.name.split("_")[0]
                    existing_ids.append(int(prefix))
                except ValueError:
                    continue
        
        if not existing_ids:
            return "01" # 00_master以外で最初なら01
        
        next_num = max(existing_ids) + 1
        return f"{next_num:02d}"

    def create_instance(self, name, template_text):
        """新しい人格フォルダと構成を作成"""
        # 半角英数チェック
        if not re.match(r"^[a-zA-Z0-9_]+$", name):
            return False, "名前は半角英数字、アンダースコアのみで入力してください。"
    
        # 前後の空白だけは念のため除去（フォルダ名のトラブル防止）
        name = name.strip()

        next_id = self.get_next_id()
        folder_name = f"{next_id}_{name}"
        new_instance_dir = self.instances_dir / folder_name

        if new_instance_dir.exists():
            return False, "そのIDまたは名前は既に存在します。"

        try:
            # 1. フォルダ作成
            new_instance_dir.mkdir()
            (new_instance_dir / "short_term_json").mkdir()
            (new_instance_dir / "mid_term_json").mkdir() # 念のため
            
            # アーカイブ階層
            archive_root = new_instance_dir / "memory_archive"
            (archive_root / "1_integrated").mkdir(parents=True)
            (archive_root / "2_knowledgeized").mkdir(parents=True)
            (archive_root / "3_log").mkdir(parents=True)

            # 2. 空ファイルの作成
            (new_instance_dir / "mid_term.txt").write_text("", encoding="utf-8")
            (new_instance_dir / "current_cache_id.txt").write_text("", encoding="utf-8")
            (new_instance_dir / "Key_Memory.txt").write_text("", encoding="utf-8")

            # 3. system_instruction.txt の作成
            # ユーザー指定のテンプレ + プロジェクト名
            final_instruction = template_text.replace("プロジェクト名：", f"プロジェクト名：{name}")
            (new_instance_dir / "system_instruction.txt").write_text(final_instruction, encoding="utf-8")

            return True, f"プロジェクト '{folder_name}' を作成しました。"
        
        except Exception as e:
            return False, f"作成エラー: {str(e)}"

    def update_instruction(self, instance_name, new_text):
        """性格設定を上書き保存"""
        target_file = self.instances_dir / instance_name / "system_instruction.txt"
        try:
            target_file.write_text(new_text, encoding="utf-8")
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
        system_instruction = si_path.read_text(encoding="utf-8") if si_path.exists() else ""
        
        # Key_Memory.txt
        km_path = instance_dir / "Key_Memory.txt"
        key_memory = km_path.read_text(encoding="utf-8") if km_path.exists() else ""
        
        return {
            "system_instruction": system_instruction,
            "key_memory": key_memory
        }

    def update_instance_prompts(self, instance_name, data):
        """インスタンスごとのプロンプトテキストを保存"""
        instance_dir = self.instances_dir / instance_name
        if not instance_dir.exists():
            return False, "インスタンスが存在しません。"
        
        try:
            if "system_instruction" in data:
                (instance_dir / "system_instruction.txt").write_text(
                    data["system_instruction"], encoding="utf-8"
                )
            if "key_memory" in data:
                (instance_dir / "Key_Memory.txt").write_text(
                    data["key_memory"], encoding="utf-8"
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
            config_path.write_text(json_str, encoding="utf-8")
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

        # Extract ID from old name
        # Expected format: "01_oldname"
        match = re.match(r"^(\d+)_(.+)$", old_name)
        if not match:
             return False, "フォルダ名の形式が不正です(IDが取得できません)。"
        
        instance_id = match.group(1)
        
        # Construct new directory path
        new_folder_name = f"{instance_id}_{new_name}"
        new_dir = self.instances_dir / new_folder_name
        
        if new_dir.exists():
            return False, "その名前のインスタンスは既に存在します。"
            
        try:
            old_dir.rename(new_dir)
            
            # Start: Update system_instruction.txt if it contains the old name
            # This is optional but nice to have.
            si_path = new_dir / "system_instruction.txt"
            if si_path.exists():
                content = si_path.read_text(encoding="utf-8")
                # Simple replacement of project name if it follows the template pattern
                # "プロジェクト名：old_name" -> "プロジェクト名：new_name"
                # Be careful not to replace unintended parts.
                # Just replacing the filename part might be safer for now? 
                # Or let's just leave the content as is to be safe, 
                # or maybe just replace "プロジェクト名：{old_name_without_id}"
                
                old_real_name = match.group(2)
                if f"プロジェクト名：{old_real_name}" in content:
                     new_content = content.replace(f"プロジェクト名：{old_real_name}", f"プロジェクト名：{new_name}")
                     si_path.write_text(new_content, encoding="utf-8")

            return True, new_folder_name
        except Exception as e:
            return False, f"リネームエラー: {str(e)}"

    def delete_instance(self, instance_name):
        """インスタンスを削除する（フォルダごと）"""
        if instance_name == "00_master":
            return False, "00_masterは削除できません。"
            
        target_dir = self.instances_dir / instance_name
        if not target_dir.exists():
            return False, "インスタンスが存在しません。"
            
        try:
            import shutil
            shutil.rmtree(target_dir)
            return True, f"インスタンス '{instance_name}' を削除しました。"
        except Exception as e:
            return False, f"削除エラー: {str(e)}"