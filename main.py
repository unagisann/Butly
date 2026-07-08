"""
main.py
-------
互換 entrypoint。app 本体の構築は butly_api.create_app() に委譲し、
ここでは起動引数・データディレクトリ・.env・Runtime 初期化・
legacy routers / CORS の注入だけを行う。
"""

from fastapi import FastAPI
from pathlib import Path
import sys
import os
import argparse
import threading
import contextlib

# =====================================================================
# 起動引数のパース（--parent-pid, --port）
# =====================================================================
parser = argparse.ArgumentParser(description="Butly Backend Server", add_help=False)
parser.add_argument(
    "--parent-pid",
    type=int,
    default=None,
    dest="parent_pid",
    help="親プロセス(Flutter)のPID。このプロセスが死んだらサーバーも終了する。",
)
parser.add_argument(
    "--port", type=int, default=48266, help="サーバーのポート番号 (デフォルト: 48266)"
)
args, _unknown = parser.parse_known_args()


# =====================================================================
# ゾンビプロセス防止: 親PIDの死亡を監視するバックグラウンドスレッド
# =====================================================================
def _watch_parent(parent_pid: int):
    """親プロセス(Flutter等)が終了したらこのサーバーも強制終了する。

    psutil による OS 非依存実装。Windows / macOS / Linux で同一コードパスを通る。
    """
    import time

    import psutil

    print(f"[Server] Watching parent PID: {parent_pid}")
    while True:
        time.sleep(5)
        try:
            alive = psutil.pid_exists(parent_pid)
            if alive:
                # ゾンビ状態の親も「死亡」とみなして終了する
                try:
                    proc = psutil.Process(parent_pid)
                    if proc.status() == psutil.STATUS_ZOMBIE:
                        alive = False
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    alive = False
        except Exception as e:
            # psutil 内部の予期せぬ例外で監視を止めない
            print(f"[Server] Parent watch error (ignored): {e}")
            continue

        if not alive:
            print(f"[Server] Parent PID {parent_pid} is dead. Terminating server.")
            os._exit(0)


if args.parent_pid:
    _watcher = threading.Thread(
        target=_watch_parent, args=(args.parent_pid,), daemon=True
    )
    _watcher.start()

# =====================================================================
# データディレクトリの解決
# =====================================================================
if getattr(sys, "frozen", False):
    _appdata = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    DATA_DIR = Path(_appdata) / "Butly"
else:
    DATA_DIR = Path(__file__).resolve().parent

DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "butly_core" / "instances").mkdir(parents=True, exist_ok=True)
print(f"[Server] Data directory: {DATA_DIR}")


# =====================================================================
# .env からAPIキーを読み込む
# =====================================================================
def _load_env_from_data_dir():
    env_path = DATA_DIR / ".env"
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())
        print(f"[Server] Loaded .env from {env_path}")
    else:
        print(
            f"[Server] No .env found at {env_path}. API key will need to be set via UI."
        )


_load_env_from_data_dir()

sys.path.append(str(Path(__file__).resolve().parent))

# =====================================================================
# 共有依存の初期化
# =====================================================================
import dependencies as deps
from butly_core.runtime import ButlyRuntime

deps.DATA_DIR = DATA_DIR
deps.BASE_DIR = DATA_DIR
deps.INSTANCES_DIR = DATA_DIR / "butly_core" / "instances"

# 中核 Runtime を生成。FastAPI ルータ以外（Discord / LINE 等）からも
# runtime.chat() を呼べる。deps の別名は Runtime と同一オブジェクトを指す。
deps.runtime = ButlyRuntime(
    data_dir=DATA_DIR,
    base_dir=DATA_DIR,
    instances_dir=deps.INSTANCES_DIR,
)
deps.instance_manager = deps.runtime.instance_manager
deps.gatekeeper = deps.runtime.gatekeeper
deps.mem_block_builder = deps.runtime.mem_block_builder
deps.instance_store = deps.runtime.instance_store


# =====================================================================
# Lifespan
# =====================================================================
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    if getattr(sys, "frozen", False):
        import shutil

        bundle_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        for config_file in ["user_config.json", "user_prompts.json", ".env"]:
            src = bundle_dir / config_file
            dst = DATA_DIR / config_file
            if src.exists() and not dst.exists():
                shutil.copy2(src, dst)
                print(f"[Server] Bootstrapped {config_file} to {dst}")

    if not deps.INSTANCES_DIR.exists():
        deps.INSTANCES_DIR.mkdir(parents=True, exist_ok=True)

    # 埋め込みベクトルの次元一致チェック（警告のみ、起動は止めない）
    try:
        from butly_core.config import AI_CONFIG
        from butly_core.core.embedding_check import log_startup_check

        configured_model = AI_CONFIG.get("embedding", {}).get("model_name")
        log_startup_check(deps.INSTANCES_DIR, configured_model)
    except Exception as e:
        print(f"[Server] Embedding dim check skipped: {e}")

    print("Butly Server Started.")
    yield
    print("Butly Server Stopped.")


# =====================================================================
# FastAPI App (butly_api.create_app) + legacy routers + CORS
# =====================================================================
from butly_api import create_app
from butly_api.context import ApiContext

from routers import (
    chat,
    instances,
    settings,
    sleeptime,
    database,
    devices,
    dashboard,
    line as line_router,
    pairing,
)

_api_context = ApiContext(
    data_dir=DATA_DIR,
    instances_dir=deps.INSTANCES_DIR,
    runtime_supplier=lambda: deps.runtime,
)

app = create_app(
    context=_api_context,
    lifespan=lifespan,
    extra_routers=[
        chat.router,
        instances.router,
        settings.router,
        sleeptime.router,
        database.router,
        devices.router,
        dashboard.router,
        line_router.router,
        pairing.router,
    ],
)

# legacy 互換の wildcard CORS。正式配布時に allowlist へ絞る（移行計画 §6.3）
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 起動時に永続化設定を適用
settings.apply_startup_settings()
_api_context.settings_loaded = True

# =====================================================================
# __main__: uvicorn 起動
# =====================================================================
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=args.port, reload=False)
