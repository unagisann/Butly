"""
migrate_profiles.py
-------------------
既存インスタンスの Key_Memory.txt からプロファイル情報（AI名・ユーザー名等）を
インスタンスごとの config.json["agent"] セクションへ移行する。

実行後:
- config.json に "agent" セクションが追加される
- Key_Memory.txt からプロファイルヘッダ行が除去され、ボディのみになる

使い方:
    python migrate_profiles.py [--dry-run]
"""

import json
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
INSTANCES_DIR = BASE_DIR / "butly_core" / "instances"

# Key_Memory のプロファイル行パターン (ja/en 両対応)
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
# 合体マッピング (優先度: 完全一致)
_ALL_PROFILE_KEYS = {**_PROFILE_KEYS_JA, **_PROFILE_KEYS_EN}


def _detect_locale(text: str) -> str:
    """テキストから言語を推定する。"""
    for ja_key in _PROFILE_KEYS_JA:
        if f"{ja_key}:" in text:
            return "ja"
    return "en"


def parse_profile_from_key_memory(text: str) -> tuple[dict, str]:
    """
    Key_Memory テキストからプロファイル行を抽出し、
    (profile_dict, body_text) を返す。
    ボディはプロファイルヘッダ部分を除いたもの。
    """
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
    header_done = False  # プロファイルブロックを通過したか

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
            # プロファイルブロック内の空行は除去（header_done=False の空行はスキップ）

    body = "\n".join(body_lines).strip()
    return profile, body


def migrate_instance(instance_dir: Path, dry_run: bool = False) -> None:
    """1インスタンスをマイグレーションする。"""
    name = instance_dir.name
    config_path = instance_dir / "config.json"
    km_path = instance_dir / "Key_Memory.txt"

    # --- config.json 読み込み ---
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [SKIP] config.json 読み込みエラー: {e}")
            return
    else:
        config = {}

    # すでに agent セクションがある場合はスキップ
    if "agent" in config:
        print(f"  [SKIP] 既に agent セクションが存在します。スキップ。")
        return

    # --- Key_Memory.txt からプロファイル抽出 ---
    km_text = km_path.read_text(encoding="utf-8") if km_path.exists() else ""
    profile, body = parse_profile_from_key_memory(km_text)

    # Jarvis 等: system_instruction.txt から ai_name を補完
    if not profile["ai_name"]:
        si_path = instance_dir / "system_instruction.txt"
        if si_path.exists():
            si_text = si_path.read_text(encoding="utf-8")
            # 「私（名前）は」または「私は 名前 として」パターンを探す
            m = re.search(r"私[（(]([^）)]+)[）)]", si_text)
            if m:
                profile["ai_name"] = m.group(1).strip()

    print(f"  Profile extracted: {profile}")
    print(f"  Body ({len(body)} chars): {body[:80]}{'...' if len(body) > 80 else ''}")

    if dry_run:
        print(f"  [DRY RUN] 変更はスキップされました。")
        return

    # --- config.json に agent セクション追加 ---
    config["agent"] = profile
    config_path.write_text(
        json.dumps(config, indent=4, ensure_ascii=False), encoding="utf-8"
    )
    print(f"  config.json に agent セクションを追加しました。")

    # --- Key_Memory.txt のボディのみを保存 ---
    if km_path.exists() and km_text.strip():
        km_path.write_text(body, encoding="utf-8")
        print(f"  Key_Memory.txt からプロファイルヘッダを除去しました。")


def main():
    dry_run = "--dry-run" in sys.argv

    if dry_run:
        print("=== DRY RUN モード（ファイルは変更されません）===\n")
    else:
        print("=== プロファイルマイグレーション開始 ===\n")

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

    for instance_dir in instances:
        print(f"\n[{instance_dir.name}]")
        migrate_instance(instance_dir, dry_run=dry_run)

    print("\n=== 完了 ===")


if __name__ == "__main__":
    main()
