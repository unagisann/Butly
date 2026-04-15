"""
migrate_key_memory.py
---------------------
旧 Key_Memory.txt → Key_Memory.yaml マイグレーションスクリプト。

使い方:
  python migrate_key_memory.py --all           # 全インスタンス
  python migrate_key_memory.py --instance NAME # 特定インスタンス
  python migrate_key_memory.py --all --dry-run # ドライラン（書き込みなし）
"""

import argparse
import re
from pathlib import Path

from butly_core.core.key_memory import save_yaml, YAML_FILENAME, TXT_FILENAME

BASE_DIR = Path(__file__).resolve().parent
INSTANCES_DIR = BASE_DIR / "butly_core" / "instances"

# プロファイル行として除外するパターン
_PROFILE_PATTERNS = re.compile(
    r"^(AI名|ユーザー名|呼称|性別|誕生日|名前|ニックネーム|Name|Gender|Birthday|Nickname)\s*[:：]",
    re.IGNORECASE,
)


def parse_txt(txt_path: Path) -> list[dict]:
    """Key_Memory.txt をパースしてエントリリストを返す。"""
    text = txt_path.read_text(encoding="utf-8")
    entries = []
    counter = 0

    for line in text.split("\n"):
        line = line.strip()
        # 空行・コメント行をスキップ
        if not line or line.startswith("#"):
            continue
        # プロファイル行をスキップ
        if _PROFILE_PATTERNS.match(line):
            print(f"  [skip] profile line: {line}")
            continue
        # "- " で始まる行は先頭の "- " を除去
        if line.startswith("- "):
            content = line[2:].strip()
        else:
            content = line

        if not content:
            continue

        counter += 1
        entries.append({
            "id": f"km_{counter:03d}",
            "target": "human",
            "content": content,
        })

    return entries


def migrate_instance(instance_dir: Path, dry_run: bool = False) -> bool:
    """単一インスタンスをマイグレーション。"""
    txt_path = instance_dir / TXT_FILENAME
    yaml_path = instance_dir / YAML_FILENAME

    if not txt_path.exists():
        print(f"  [skip] {TXT_FILENAME} not found")
        return False

    if yaml_path.exists():
        print(f"  [skip] {YAML_FILENAME} already exists")
        return False

    entries = parse_txt(txt_path)
    if not entries:
        print(f"  [skip] No entries parsed from {TXT_FILENAME}")
        return False

    print(f"  Parsed {len(entries)} entries")
    for e in entries:
        print(f"    [{e['target']}] {e['content'][:60]}")

    if dry_run:
        print("  [dry-run] Would write YAML")
        return True

    save_yaml(entries, yaml_path)
    print(f"  Wrote {YAML_FILENAME}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Migrate Key_Memory.txt to Key_Memory.yaml")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--instance", type=str, help="Migrate a specific instance")
    group.add_argument("--all", action="store_true", help="Migrate all instances")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    if args.instance:
        instance_dir = INSTANCES_DIR / args.instance
        if not instance_dir.exists():
            print(f"Instance '{args.instance}' not found at {instance_dir}")
            return
        print(f"Migrating {args.instance}...")
        migrate_instance(instance_dir, dry_run=args.dry_run)
    else:
        for instance_dir in sorted(INSTANCES_DIR.iterdir()):
            if not instance_dir.is_dir() or instance_dir.name.startswith("."):
                continue
            print(f"Migrating {instance_dir.name}...")
            migrate_instance(instance_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
