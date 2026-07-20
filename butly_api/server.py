"""
server.py
─────────
desktop sidecar 用 CLI entrypoint（frontend_migration_plan.ja.md §6.1 / §6.2）。

- Tauri shell が `--host / --port / --parent-pid / --data-dir` を渡して起動する。
- 既定 bind は `127.0.0.1`。`0.0.0.0` は明示指定されたときだけ許可する。
- desktop token は環境変数 ``BUTLY_DESKTOP_TOKEN`` **のみ**で受け取る。
  CLI 引数を用意せず、log / URL / ready line にも出さない。
- `--port 0` を受け付け、OS が割り当てた実 port を 1 行の機械可読 JSON として
  stdout へ通知する（§6.4 の port 0 + ready 通知方式）。readiness 自体は
  Tauri 側が通知された port へ `/api/v1/health` を polling して判定する。
- legacy routers（Streamlit 互換）は含めない。sidecar が公開するのは
  `/api/v1` contract と、graceful shutdown 用の `POST /api/v1/shutdown` だけ。
- `main.py` は従来起動（Streamlit 併用 / 開発）の互換 entrypoint として残る。
"""

import argparse
import contextlib
import json
import logging
import os
import socket
import sys
import threading
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

# PyInstaller / 直接実行の両方で repo root import を成立させる
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.append(str(_REPO_ROOT))

from butly_api.auth import DESKTOP_TOKEN_ENV  # noqa: E402
from butly_api.version import (  # noqa: E402
    API_V1_PREFIX,
    API_VERSION,
    BACKEND_VERSION,
)

# production CORS allowlist（§6.3）。Tauri v2 の Windows WebView origin のみ。
PRODUCTION_CORS_ORIGINS = ["http://tauri.localhost"]

# development 用の明示 allowlist。Vite dev server（Tauri 規約 port 1420 と
# Vite 既定 5173）の localhost / 127.0.0.1 origin。
DEV_CORS_ORIGINS = [
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """sidecar CLI 引数を strict に parse する。

    token 用の引数は意図的に存在しない（環境変数 ``BUTLY_DESKTOP_TOKEN`` のみ。
    command line は process list から見えるため）。
    """
    parser = argparse.ArgumentParser(
        prog="butly-backend",
        description="Butly FastAPI sidecar server (desktop shell 用)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind するホスト。既定は 127.0.0.1。0.0.0.0 は明示指定時のみ。",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="bind するポート。0 なら OS が割り当て、実 port を JSON で通知する。",
    )
    parser.add_argument(
        "--parent-pid",
        type=int,
        default=None,
        dest="parent_pid",
        help="親プロセス（Tauri shell）の PID。死亡を検知したら自己終了する。",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        dest="data_dir",
        help="ユーザーデータディレクトリ。未指定時は frozen なら "
        "%%LOCALAPPDATA%%\\Butly、開発実行なら repository root。",
    )
    parser.add_argument(
        "--dev-cors",
        action="store_true",
        dest="dev_cors",
        help="Vite dev server の localhost origin を CORS allowlist に加える。",
    )
    return parser.parse_args(argv)


def resolve_data_dir(data_dir_arg: Optional[str]) -> Path:
    """--data-dir / frozen 状態からデータディレクトリを確定する。"""
    if data_dir_arg:
        return Path(data_dir_arg).expanduser().resolve()
    if getattr(sys, "frozen", False):
        appdata = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(appdata) / "Butly"
    return _REPO_ROOT


def _load_env_from_data_dir(data_dir: Path) -> None:
    """data dir の .env から API key 等を環境変数へロードする（main.py と同方針）。"""
    env_path = data_dir / ".env"
    if not env_path.exists():
        print(f"[Sidecar] No .env found at {env_path}.")
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())
    print(f"[Sidecar] Loaded .env from {env_path}")


def _watch_parent(parent_pid: int) -> None:
    """親プロセス（Tauri shell）が死んだら自己終了する残存 process 防止 fallback。"""
    import time

    import psutil

    print(f"[Sidecar] Watching parent PID: {parent_pid}")
    while True:
        time.sleep(5)
        try:
            alive = psutil.pid_exists(parent_pid)
            if alive:
                try:
                    proc = psutil.Process(parent_pid)
                    if proc.status() == psutil.STATUS_ZOMBIE:
                        alive = False
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    alive = False
        except Exception as e:
            # 監視スレッドは 1 回の失敗で止めない（coding_conventions.ja.md）
            print(f"[Sidecar] Parent watch error (ignored): {e}")
            continue

        if not alive:
            print(f"[Sidecar] Parent PID {parent_pid} is dead. Terminating.")
            os._exit(0)


def start_parent_watch(parent_pid: Optional[int]) -> None:
    if not parent_pid:
        return
    threading.Thread(
        target=_watch_parent, args=(parent_pid,), daemon=True
    ).start()


def _build_shutdown_router() -> Any:
    """graceful shutdown 用 endpoint。

    `/api/v1` 配下なので desktop token 認証の対象になる（health 以外は token
    必須の原則を維持）。OpenAPI snapshot は extra_routers なしの
    `create_app()` から生成するため、公開 contract には現れない。
    """
    from fastapi import APIRouter, Request

    from butly_api.errors import ApiException

    router = APIRouter(prefix=API_V1_PREFIX, tags=["system"])

    @router.post("/shutdown", status_code=202, include_in_schema=False)
    async def post_shutdown(request: Request) -> dict:
        server = getattr(request.app.state, "uvicorn_server", None)
        if server is None:
            raise ApiException(
                503,
                "shutdown_unavailable",
                "Server handle is not attached; cannot shut down gracefully.",
            )
        server.should_exit = True
        return {"status": "shutting_down"}

    return router


def build_app(data_dir: Path, *, dev_cors: bool = False) -> Any:
    """sidecar 用 FastAPI app を構築する。

    - ButlyRuntime を直接生成し ApiContext 経由で `/api/v1` へ渡す。
      legacy routers / dependencies モジュールには依存しない。
    - desktop token は環境変数から ApiContext へ渡す（未設定なら認証無効の
      開発モード。Tauri 経由の production 起動では必ず設定される）。
    """
    from fastapi.middleware.cors import CORSMiddleware

    from butly_api import create_app
    from butly_api.context import ApiContext
    from butly_core.runtime import ButlyRuntime

    instances_dir = data_dir / "butly_core" / "instances"
    instances_dir.mkdir(parents=True, exist_ok=True)

    runtime = ButlyRuntime(
        data_dir=data_dir,
        base_dir=data_dir,
        instances_dir=instances_dir,
    )

    context = ApiContext(
        data_dir=data_dir,
        instances_dir=instances_dir,
        runtime_supplier=lambda: runtime,
        auth_token=os.environ.get(DESKTOP_TOKEN_ENV) or None,
    )

    @contextlib.asynccontextmanager
    async def lifespan(app):
        print("[Sidecar] Butly sidecar started.")
        yield
        print("[Sidecar] Butly sidecar stopped.")

    app = create_app(
        context=context,
        lifespan=lifespan,
        extra_routers=[_build_shutdown_router()],
    )

    origins = list(PRODUCTION_CORS_ORIGINS)
    if dev_cors:
        origins += DEV_CORS_ORIGINS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    )

    # 起動時設定の適用は現状 no-op（routers.settings.apply_startup_settings）。
    # butly_api は legacy routers を import しない境界を守るため、ここでは
    # 適用済み扱いにするだけとする。実装が入る際は共有 service へ切り出す。
    context.settings_loaded = True

    return app


def bind_socket(host: str, port: int) -> socket.socket:
    """listen socket を自前で bind し、port 0 のときの実 port を確定させる。"""
    if host not in _LOOPBACK_HOSTS:
        # 既定は loopback。0.0.0.0 等は明示指定されたときだけ通す（§6.3）。
        print(
            f"[Sidecar] WARNING: binding non-loopback host {host!r}. "
            "Use this only for explicit developer / remote mode."
        )
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    if os.name != "nt":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    return sock


def require_token_for_non_loopback(host: str) -> None:
    """非 loopback 公開時に認証なしで起動する事故を防ぐ。"""
    if host not in _LOOPBACK_HOSTS and not os.environ.get(DESKTOP_TOKEN_ENV):
        raise SystemExit(
            "BUTLY_DESKTOP_TOKEN is required when binding a non-loopback host."
        )


def emit_listening_line(host: str, port: int, stream: Any = None) -> None:
    """Tauri が parse する 1 行 JSON を stdout へ書く。

    token / secret はここに **絶対に** 含めない。
    """
    stream = stream if stream is not None else sys.stdout
    payload = {
        "event": "listening",
        "host": host,
        "port": port,
        "pid": os.getpid(),
        "backend_version": BACKEND_VERSION,
        "api_version": API_VERSION,
    }
    print(json.dumps(payload, separators=(",", ":")), file=stream, flush=True)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)

    data_dir = resolve_data_dir(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    print(f"[Sidecar] Data directory: {data_dir}")

    _load_env_from_data_dir(data_dir)
    require_token_for_non_loopback(args.host)
    start_parent_watch(args.parent_pid)

    app = build_app(data_dir, dev_cors=args.dev_cors)

    sock = bind_socket(args.host, args.port)
    actual_port = sock.getsockname()[1]

    import uvicorn

    config = uvicorn.Config(
        app,
        host=args.host,
        port=actual_port,
        log_level="info",
        reload=False,
    )
    server = uvicorn.Server(config)
    app.state.uvicorn_server = server

    emit_listening_line(args.host, actual_port)
    server.run(sockets=[sock])


if __name__ == "__main__":
    main()
