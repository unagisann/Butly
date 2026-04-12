"""
migrate_profiles.py
-------------------
プロファイル情報のマイグレーションツール。

Phase 1 (default):
    既存インスタンスの Key_Memory.txt からプロファイル情報を抽出し、
    config.json["agent"] セクションへ移行する（旧スキーマ）。
    Key_Memory.txt からはプロファイルヘッダ行が除去される。

Phase 2 (--phase2):
    旧 config.json["agent"] セクションを
    新スキーマ agent_profile + user_profile に分割する。
    冪等性: config に agent_profile が既に存在する場合はスキップ。

使い方:
    python migrate_profiles.py [--dry-run]
    python migrate_profiles.py --phase2 [--dry-run]
"""

import json
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
INSTANCES_DIR = BASE_DIR / "butly_core" / "instances"

_PROFILE_KEYS_JA = {
    "AI名": "ai_name",
    "ユーザー名": "user_name",
    "呼称": "nickname",
    "性別": "gender",
    "生年月日": "birthday",
}
_PROFILE_KEYS_EN = {
    "AI Name": "ai_name",
    "User Name": "user_name",
    "Preferred Name": "nickname",
    "Gender": "gender",
    "Date of Birth": "birthday",
}
_ALL_PROFILE_KEYS = {**_PROFILE_KEYS_JA, **_PROFILE_KEYS_EN}


def _detect_locale(text: str) -> str:
    for ja_key in _PROFILE_KEYS_JA:
        if f"{ja_key}:" in text:
            return "ja"
    return "en"


def parse_profile_from_key_memory(text: str) -> tuple[dict, str]:
    profile = {
        "ai_name": "",
        "user_name": "",
        "nickname": "",
        "gender": "",
        "birthday": "",
        "locale": _detect_locale(text),
    }

    lines = text.split("\n")
    body_lines = []
    header_done = False

    for line in lines:
        matched = False
        for label, field in _ALL_PROFILE_KEYS.items():
            pattern = rf"^{re.escape(label)}\s*:\s*(.+)$"
            m = re.match(pattern, line.strip())
            if m:
                profile[field] = m.group(1).strip()
                matched = True
                break

        if not matched:
            stripped = line.strip()
            if stripped:
                header_done = True
            if header_done:
                body_lines.append(line)

    body = "\n".join(body_lines).strip()
    return profile, body


def migrate_instance_phase1(instance_dir: Path, dry_run: bool = False) -> None:
    """Phase 1: Key_Memory → config.json["agent"]"""
    name = instance_dir.name
    config_path = instance_dir / "config.json"
    km_path = instance_dir / "Key_Memory.txt"

    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [SKIP] config.json 読み込みエラー: {e}")
            return
    else:
        config = {}

    if "agent" in config or "agent_profile" in config:
        print(f"  [SKIP] 既に agent/agent_profile セクションが存在します。スキップ。")
        return

    km_text = km_path.read_text(encoding="utf-8") if km_path.exists() else ""
    profile, body = parse_profile_from_key_memory(km_text)

    if not profile["ai_name"]:
        si_path = instance_dir / "system_instruction.txt"
        if si_path.exists():
            si_text = si_path.read_text(encoding="utf-8")
            m = re.search(r"私[(]([^)]+)[)]", si_text)
            if m:
                profile["ai_name"] = m.group(1).strip()

    print(f"  Profile extracted: {profile}")
    print(f"  Body ({len(body)} chars): {body[:80]}{'...' if len(body) > 80 else ''}")

    if dry_run:
        print(f"  [DRY RUN] 変更はスキップされました。")
        return

    config["agent"] = profile
    config_path.write_text(
        json.dumps(config, indent=4, ensure_ascii=False), encoding="utf-8"
    )
    print(f"  config.json に agent セクションを追加しました。")

    if km_path.exists() and km_text.strip():
        km_path.write_text(body, encoding="utf-8")
        print(f"  Key_Memory.txt からプロファイルヘッダを除去しました。")


def migrate_instance_phase2(instance_dir: Path, dry_run: bool = False) -> None:
    """Phase 2: config.json["agent"] → config.json["agent_profile"] + ["user_profile"]"""
    config_path = instance_dir / "config.json"
    if not config_path.exists():
        print(f"  [SKIP] config.json が存在しません。")
        return

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [SKIP] config.json 読み込みエラー: {e}")
        return

    if "agent_profile" in config or "user_profile" in config:
        print(f"  [SKIP] 既に agent_profile/user_profile が存在します。")
        return

    old = config.get("agent", {})
    if not old:
        print(f"  [SKIP] 旧 agent セクションが存在しません。")
        return

    agent_profile = {
        "ai_name": old.get("ai_name", ""),
        "ai_gender": "",
        "locale": old.get("locale", "ja"),
    }
    user_profile = {
        "user_name": old.get("user_name", ""),
        "preferred_call": old.get("nickname", ""),
        "gender": old.get("gender", ""),
        "birthday": old.get("birthday", ""),
        "location": "",
    }

    print(f"  agent_profile: {agent_profile}")
    print(f"  user_profile:  {user_profile}")

    if dry_run:
        print(f"  [DRY RUN] 変更はスキップされました。")
        return

    config["agent_profile"] = agent_profile
    config["user_profile"] = user_profile
    del config["agent"]
    config_path.write_text(
        json.dumps(config, indent=4, ensure_ascii=False), encoding="utf-8"
    )
    print(f"  新スキーマに移行完了（旧 agent キーは削除）。")


def main():
    dry_run = "--dry-run" in sys.argv
    phase2 = "--phase2" in sys.argv

    if dry_run:
        print("=== DRY RUN モード（ファイルは変更されません）===\n")
    else:
        mode = "Phase 2" if phase2 else "Phase 1"
        print(f"=== プロファイルマイグレーション開始 ({mode}) ===\n")

    if not INSTANCES_DIR.exists():
        print("インスタンスディレクトリが見つかりません。")
        return

    instances = sorted([
        d for d in INSTANCES_DIR.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    ])

    if not instances:
        print("インスタンスが見つかりません。")
        return

    migrate_fn = migrate_instance_phase2 if phase2 else migrate_instance_phase1
    for instance_dir in instances:
        print(f"\n[{instance_dir.name}]")
        migrate_fn(instance_dir, dry_run=dry_run)

    print("\n=== 完了 ===")


if __name__ == "__main__":
    main()
