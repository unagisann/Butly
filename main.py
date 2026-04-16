"""
main.py
-------
FastAPI app 生成 + lifespan + include_router のみ。
ビジネスロジックは各 routers/ に分離済み。
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
parser.add_argument("--parent-pid", type=int, default=None, dest="parent_pid",
                    help="親プロセス(Flutter)のPID。このプロセスが死んだらサーバーも終了する。")
parser.add_argument("--port", type=int, default=48266,
                    help="サーバーのポート番号 (デフォルト: 48266)")
args, _unknown = parser.parse_known_args()

# =====================================================================
# ゾンビプロセス防止: 親PIDの死亡を監視するバックグラウンドスレッド
# =====================================================================
def _watch_parent(parent_pid: int):
    """親プロセス(Flutter)が終了したらこのサーバーも強制終了する"""
    import time
    import ctypes
    print(f"[Server] Watching parent PID: {parent_pid}")
    while True:
        time.sleep(5)
        try:
            if sys.platform == "win32":
                SYNCHRONIZE = 0x00100000
                handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, parent_pid)
                if handle == 0:
                    print(f"[Server] Parent PID {parent_pid} is dead. Terminating server.")
                    os._exit(0)
                ctypes.windll.kernel32.CloseHandle(handle)
            else:
                os.kill(parent_pid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            print(f"[Server] Parent PID {parent_pid} is dead. Terminating server.")
            os._exit(0)

if args.parent_pid:
    _watcher = threading.Thread(target=_watch_parent, args=(args.parent_pid,), daemon=True)
    _watcher.start()

# =====================================================================
# データディレクトリの解決
# =====================================================================
if getattr(sys, 'frozen', False):
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
                if line and not line.startswith('#') and '=' in line:
                    key, _, value = line.partition('=')
                    os.environ.setdefault(key.strip(), value.strip())
        print(f"[Server] Loaded .env from {env_path}")
    else:
        print(f"[Server] No .env found at {env_path}. API key will need to be set via UI.")

_load_env_from_data_dir()

sys.path.append(str(Path(__file__).resolve().parent))

# =====================================================================
# 共有依存の初期化
# =====================================================================
import dependencies as deps
from butly_core.core.instance_manager import InstanceManager
from butly_core.core.gatekeeper import Gatekeeper, MemoryBlockBuilder

deps.DATA_DIR = DATA_DIR
deps.BASE_DIR = DATA_DIR
deps.INSTANCES_DIR = DATA_DIR / "butly_core" / "instances"
deps.instance_manager = InstanceManager(DATA_DIR)
deps.gatekeeper = Gatekeeper(base_dir=DATA_DIR)
deps.mem_block_builder = MemoryBlockBuilder()

# =====================================================================
# Lifespan
# =====================================================================
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    if getattr(sys, 'frozen', False):
        import shutil
        bundle_dir = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
        for config_file in ["user_config.json", "user_prompts.json", ".env"]:
            src = bundle_dir / config_file
            dst = DATA_DIR / config_file
            if src.exists() and not dst.exists():
                shutil.copy2(src, dst)
                print(f"[Server] Bootstrapped {config_file} to {dst}")

    if not deps.INSTANCES_DIR.exists():
        deps.INSTANCES_DIR.mkdir(parents=True, exist_ok=True)

    print("Butly Server Started.")
    yield
    print("Butly Server Stopped.")

# =====================================================================
# FastAPI App + CORS + Routers
# =====================================================================
app = FastAPI(lifespan=lifespan, title="Butly API")

from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from routers import chat, instances, settings, sleeptime, database, devices, dashboard

app.include_router(chat.router)
app.include_router(instances.router)
app.include_router(settings.router)
app.include_router(sleeptime.router)
app.include_router(database.router)
app.include_router(devices.router)
app.include_router(dashboard.router)

# 起動時に永続化設定を適用
settings.apply_startup_settings()

# =====================================================================
# __main__: uvicorn 起動
# =====================================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=args.port, reload=False)
