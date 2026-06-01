"""
run_discord_bot.py
------------------
Discord bot 単体起動用エントリポイント（optional integration）。

FastAPI の lifecycle と Discord Gateway 接続を混ぜないため、別プロセスで起動する::

    python run_discord_bot.py

前提:
  - `.env` に `DISCORD_BOT_TOKEN` を設定していること（未設定なら即終了）。
  - discord.py をインストールしていること（`pip install -r requirements-discord.txt`）。
  - Discord Developer Portal で Bot の "Message Content Intent" を有効化していること
    （メンション本文を読むために必須）。

本体（FastAPI / Streamlit）と同じ DATA_DIR を参照し、同じ ButlyRuntime 経路
（runtime.chat）で応答を生成する。会話は通常チャットと同じ記憶機構に保存される。

任意の環境変数:
  - DISCORD_DEV_GUILD_ID: 指定すると slash command を当該 guild に即時 sync する
    （手動テスト用。未指定なら global sync で反映に時間がかかる）。
"""

import os
import sys
from pathlib import Path


def _resolve_data_dir() -> Path:
    """main.py と同じ規則で DATA_DIR を解決する。"""
    if getattr(sys, "frozen", False):
        appdata = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        data_dir = Path(appdata) / "Butly"
    else:
        data_dir = Path(__file__).resolve().parent
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "butly_core" / "instances").mkdir(parents=True, exist_ok=True)
    return data_dir


def _load_env(data_dir: Path) -> None:
    """DATA_DIR / プロジェクトルートの .env を読み込む（既存 env は上書きしない）。"""
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None

    candidates = [
        data_dir / ".env",
        Path(__file__).resolve().parent / ".env",
        Path(__file__).resolve().parent / "APIkey.env",
    ]
    for env_path in candidates:
        if not env_path.exists():
            continue
        if load_dotenv is not None:
            load_dotenv(dotenv_path=env_path, override=False)
        else:  # フォールバック（python-dotenv 不在時）
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())


def main() -> int:
    data_dir = _resolve_data_dir()
    print(f"[discord] Data directory: {data_dir}")

    _load_env(data_dir)

    token = (os.environ.get("DISCORD_BOT_TOKEN") or "").strip()
    if not token:
        print(
            "[discord] DISCORD_BOT_TOKEN が未設定です。"
            ".env に DISCORD_BOT_TOKEN=... を設定してください。",
            file=sys.stderr,
        )
        return 1

    # プロジェクトルートを import path に追加（dependencies / butly_core 解決用）
    project_root = Path(__file__).resolve().parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    # 本体と同じ規則で ButlyRuntime を生成し、deps にも配線する
    # （sleeptime / memory 等が deps を参照しても本体と同じ状態を見るように）。
    import dependencies as deps
    from butly_core.runtime import ButlyRuntime

    instances_dir = data_dir / "butly_core" / "instances"
    deps.DATA_DIR = data_dir
    deps.BASE_DIR = data_dir
    deps.INSTANCES_DIR = instances_dir
    deps.runtime = ButlyRuntime(
        data_dir=data_dir, base_dir=data_dir, instances_dir=instances_dir
    )
    deps.instance_manager = deps.runtime.instance_manager
    deps.gatekeeper = deps.runtime.gatekeeper
    deps.mem_block_builder = deps.runtime.mem_block_builder
    deps.instance_store = deps.runtime.instance_store

    from butly_core.external.account_mapping import ExternalAccountStore
    from butly_core.external.discord_adapter import create_bot

    store = ExternalAccountStore(data_dir, instances_dir)
    dev_guild_id = (os.environ.get("DISCORD_DEV_GUILD_ID") or "").strip() or None

    try:
        bot = create_bot(deps.runtime, store, dev_guild_id=dev_guild_id)
    except RuntimeError as e:
        # discord.py 未インストール
        print(f"[discord] {e}", file=sys.stderr)
        return 1

    print("[discord] Starting Discord bot...")
    # token はログに出さない。discord.py の run に直接渡す。
    bot.run(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
