"""
smoke_test_sidecar.py
---------------------
PyInstaller で build した sidecar 実行ファイルの起動確認。
CI（Windows）で Tauri build の前に実行する。

検証項目:
1. `--port 0` 起動で listening JSON 行が stdout に出る
2. 通知された port の `/api/v1/health` が無認証で 200
3. token なし / 不正 token の `/api/v1/ready` が 401
4. 正しい token で `/api/v1/ready` が 200 / ready=true
5. `POST /api/v1/shutdown`（token 付き）で graceful に終了し、
   プロセスが残らない
6. 起動時間（spawn → listening、spawn → health 200）を記録する

usage:
  python scripts/smoke_test_sidecar.py [path/to/butly-backend(.exe)]
"""

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN = "smoke-test-token"
LISTEN_TIMEOUT = 120
HEALTH_TIMEOUT = 60


def find_default_exe() -> Path:
    exe_name = "butly-backend.exe" if os.name == "nt" else "butly-backend"
    pyinstaller_outputs = [
        REPO_ROOT
        / "packaging"
        / "pyinstaller"
        / "dist"
        / "butly-backend"
        / exe_name,
        REPO_ROOT / "packaging" / "pyinstaller" / "dist" / exe_name,
    ]
    for path in pyinstaller_outputs:
        if path.is_file():
            return path

    binaries = REPO_ROOT / "frontend" / "src-tauri" / "binaries"
    candidates = sorted(binaries.glob("butly-backend-*"))
    candidates = [p for p in candidates if p.is_file() and p.suffix != ".gitkeep"]
    if not candidates:
        raise SystemExit(f"no sidecar binary found under {binaries}")
    return candidates[0]


def http(method: str, url: str, token: str = ""):
    req = urllib.request.Request(url, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def process_lines(proc, timeout: float):
    lines = queue.Queue()

    def read_stdout():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=read_stdout, daemon=True).start()
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timed out waiting for sidecar stdout")
        try:
            line = lines.get(timeout=remaining)
        except queue.Empty as exc:
            raise TimeoutError("timed out waiting for sidecar stdout") from exc
        if line is None:
            return
        yield line


def main() -> None:
    exe = Path(sys.argv[1]) if len(sys.argv) > 1 else find_default_exe()
    print(f"[smoke] sidecar: {exe}")

    env = dict(os.environ)
    env["BUTLY_DESKTOP_TOKEN"] = TOKEN

    with tempfile.TemporaryDirectory(prefix="butly-smoke-") as tmp:
        started = time.monotonic()
        proc = subprocess.Popen(
            [str(exe), "--port", "0", "--data-dir", tmp],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            listening = None
            for line in process_lines(proc, LISTEN_TIMEOUT):
                line = line.strip()
                print(f"[sidecar] {line}")
                if line.startswith("{"):
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if payload.get("event") == "listening":
                        listening = payload
                        break
            assert listening, "listening line was not emitted"
            t_listening = time.monotonic() - started
            assert TOKEN not in json.dumps(listening), "token leaked to stdout"

            port = listening["port"]
            assert listening["host"] == "127.0.0.1"
            assert port > 0
            base = f"http://127.0.0.1:{port}"

            status = None
            deadline = time.monotonic() + HEALTH_TIMEOUT
            while time.monotonic() < deadline:
                try:
                    status, health = http("GET", f"{base}/api/v1/health")
                    break
                except (urllib.error.URLError, ConnectionError):
                    time.sleep(0.3)
            t_health = time.monotonic() - started
            assert status == 200, f"health status {status}"
            assert health["api_version"] == "v1"
            print(
                f"[smoke] startup time: listening={t_listening:.2f}s "
                f"health200={t_health:.2f}s"
            )

            status, body = http("GET", f"{base}/api/v1/ready")
            assert status == 401, f"ready without token: {status}"
            assert body["code"] == "unauthorized"

            status, _ = http("GET", f"{base}/api/v1/ready", token="wrong-token")
            assert status == 401, f"ready with wrong token: {status}"

            status, ready = http("GET", f"{base}/api/v1/ready", token=TOKEN)
            assert status == 200, f"ready with token: {status}"
            assert ready["ready"] is True, f"ready payload: {ready}"

            status, _ = http("POST", f"{base}/api/v1/shutdown", token=TOKEN)
            assert status == 202, f"shutdown status: {status}"
            proc.wait(timeout=30)
            print(f"[smoke] sidecar exited with code {proc.returncode}")
            assert proc.returncode == 0, "sidecar exit code was not 0"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=15)
                print("[smoke] sidecar did not exit; killed")

    print("[smoke] all sidecar smoke checks passed")


if __name__ == "__main__":
    main()
