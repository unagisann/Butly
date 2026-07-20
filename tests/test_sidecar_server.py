"""
test_sidecar_server.py
----------------------
desktop sidecar CLI（butly_api/server.py）のテスト。

- CLI parse: 既定 host は 127.0.0.1、port 0 許容、token 引数は存在しない
- data-dir 解決: 明示指定 / frozen（LOCALAPPDATA）/ 開発（repo root）
- listening 通知: 1 行 JSON に実 port が入り、token を含まない
- build_app: token 有効時に /api/v1（health 除く）が 401、CORS allowlist
- subprocess E2E: port 0 起動 → JSON 通知 → health 200 → token なし/不正 401
  → graceful shutdown → process 残存なし
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from io import StringIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from butly_api import server
from butly_api.auth import DESKTOP_TOKEN_ENV

TOKEN = "sidecar-test-token"

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ===================================================================
# CLI parse
# ===================================================================


class TestParseArgs:
    def test_defaults_are_loopback_and_ephemeral_port(self):
        args = server.parse_args([])
        assert args.host == "127.0.0.1"
        assert args.port == 0
        assert args.parent_pid is None
        assert args.data_dir is None
        assert args.dev_cors is False

    def test_accepts_sidecar_arguments(self):
        args = server.parse_args(
            [
                "--host",
                "127.0.0.1",
                "--port",
                "48266",
                "--parent-pid",
                "1234",
                "--data-dir",
                "/tmp/butly-data",
            ]
        )
        assert args.port == 48266
        assert args.parent_pid == 1234
        assert args.data_dir == "/tmp/butly-data"

    def test_token_cli_argument_does_not_exist(self):
        """token は環境変数のみ。CLI 引数は process list に見えるため拒否する。"""
        with pytest.raises(SystemExit):
            server.parse_args(["--auth-token", "secret"])
        with pytest.raises(SystemExit):
            server.parse_args(["--token", "secret"])


# ===================================================================
# data-dir 解決
# ===================================================================


class TestResolveDataDir:
    def test_explicit_data_dir_wins(self, tmp_path):
        resolved = server.resolve_data_dir(str(tmp_path / "custom"))
        assert resolved == (tmp_path / "custom").resolve()

    def test_frozen_defaults_to_localappdata(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert server.resolve_data_dir(None) == tmp_path / "Butly"

    def test_dev_defaults_to_repo_root(self, monkeypatch):
        monkeypatch.delattr(sys, "frozen", raising=False)
        assert server.resolve_data_dir(None) == PROJECT_ROOT


# ===================================================================
# listening 通知
# ===================================================================


class TestListeningLine:
    def test_line_is_machine_readable_json(self, monkeypatch):
        monkeypatch.setenv(DESKTOP_TOKEN_ENV, TOKEN)
        out = StringIO()
        server.emit_listening_line("127.0.0.1", 43210, stream=out)
        raw = out.getvalue()
        assert raw.count("\n") == 1
        payload = json.loads(raw)
        assert payload["event"] == "listening"
        assert payload["port"] == 43210
        assert payload["host"] == "127.0.0.1"
        assert payload["pid"] == os.getpid()
        assert payload["backend_version"]
        assert payload["api_version"] == "v1"
        # token は絶対に stdout へ出さない
        assert TOKEN not in raw

    def test_bind_socket_port_zero_assigns_real_port(self):
        sock = server.bind_socket("127.0.0.1", 0)
        try:
            port = sock.getsockname()[1]
            assert port > 0
        finally:
            sock.close()

    def test_non_loopback_requires_token(self, monkeypatch):
        monkeypatch.delenv(DESKTOP_TOKEN_ENV, raising=False)
        with pytest.raises(SystemExit, match="BUTLY_DESKTOP_TOKEN"):
            server.require_token_for_non_loopback("0.0.0.0")

        monkeypatch.setenv(DESKTOP_TOKEN_ENV, TOKEN)
        server.require_token_for_non_loopback("0.0.0.0")


# ===================================================================
# build_app（in-process）
# ===================================================================


def _make_client(tmp_path, monkeypatch, *, token=None, dev_cors=False):
    if token is None:
        monkeypatch.delenv(DESKTOP_TOKEN_ENV, raising=False)
    else:
        monkeypatch.setenv(DESKTOP_TOKEN_ENV, token)
    app = server.build_app(tmp_path, dev_cors=dev_cors)
    return TestClient(app)


class TestBuildApp:
    def test_creates_instances_dir(self, tmp_path, monkeypatch):
        _make_client(tmp_path, monkeypatch)
        assert (tmp_path / "butly_core" / "instances").is_dir()

    def test_ready_ok_without_token(self, tmp_path, monkeypatch):
        client = _make_client(tmp_path, monkeypatch)
        resp = client.get("/api/v1/ready")
        assert resp.status_code == 200
        assert resp.json()["ready"] is True

    def test_token_enforced_on_v1_except_health(self, tmp_path, monkeypatch):
        client = _make_client(tmp_path, monkeypatch, token=TOKEN)
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/ready").status_code == 401
        resp = client.get(
            "/api/v1/ready", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert resp.status_code == 200

    def test_shutdown_requires_token_and_stops_server(
        self, tmp_path, monkeypatch
    ):
        client = _make_client(tmp_path, monkeypatch, token=TOKEN)

        class DummyServer:
            should_exit = False

        dummy = DummyServer()
        client.app.state.uvicorn_server = dummy

        assert client.post("/api/v1/shutdown").status_code == 401
        assert dummy.should_exit is False

        resp = client.post(
            "/api/v1/shutdown", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert resp.status_code == 202
        assert resp.json() == {"status": "shutting_down"}
        assert dummy.should_exit is True

    def test_shutdown_without_server_handle_is_503(self, tmp_path, monkeypatch):
        client = _make_client(tmp_path, monkeypatch, token=TOKEN)
        resp = client.post(
            "/api/v1/shutdown", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert resp.status_code == 503
        assert resp.json()["code"] == "shutdown_unavailable"

    def test_shutdown_is_hidden_from_openapi(self, tmp_path, monkeypatch):
        client = _make_client(tmp_path, monkeypatch, token=TOKEN)
        schema = client.get("/openapi.json").json()
        assert "/api/v1/shutdown" not in schema["paths"]


class TestCors:
    def _preflight(self, client, origin):
        return client.options(
            "/api/v1/health",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
            },
        )

    def test_production_allows_only_tauri_origin(self, tmp_path, monkeypatch):
        client = _make_client(tmp_path, monkeypatch)
        allowed = self._preflight(client, "http://tauri.localhost")
        assert (
            allowed.headers.get("access-control-allow-origin")
            == "http://tauri.localhost"
        )
        denied = self._preflight(client, "http://localhost:1420")
        assert "access-control-allow-origin" not in denied.headers
        wildcard = self._preflight(client, "http://evil.example")
        assert "access-control-allow-origin" not in wildcard.headers

    def test_dev_cors_adds_vite_origins(self, tmp_path, monkeypatch):
        client = _make_client(tmp_path, monkeypatch, dev_cors=True)
        for origin in (
            "http://localhost:1420",
            "http://127.0.0.1:1420",
            "http://localhost:5173",
        ):
            resp = self._preflight(client, origin)
            assert (
                resp.headers.get("access-control-allow-origin") == origin
            ), origin

    def test_no_wildcard_credentials(self, tmp_path, monkeypatch):
        client = _make_client(tmp_path, monkeypatch)
        resp = self._preflight(client, "http://tauri.localhost")
        assert resp.headers.get("access-control-allow-credentials") != "true"


# ===================================================================
# subprocess E2E（実 uvicorn / 実 socket）
# ===================================================================


def _http(method, url, *, token=None, timeout=10):
    req = urllib.request.Request(url, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _process_lines(proc, timeout):
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


class TestSidecarProcess:
    def test_full_lifecycle(self, tmp_path):
        """port 0 起動 → JSON 通知 → health → 401 → graceful shutdown → 終了。"""
        env = dict(os.environ)
        env[DESKTOP_TOKEN_ENV] = TOKEN
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "butly_api.server",
                "--port",
                "0",
                "--data-dir",
                str(tmp_path),
            ],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            listening = None
            for line in _process_lines(proc, 90):
                line = line.strip()
                if line.startswith("{"):
                    payload = json.loads(line)
                    if payload.get("event") == "listening":
                        listening = payload
                        break
            assert listening is not None, "listening line was not emitted"
            assert listening["host"] == "127.0.0.1"
            port = listening["port"]
            assert port > 0

            base = f"http://127.0.0.1:{port}"

            # health は無認証で 200 になるまで少し待つ（uvicorn 起動猶予）
            status = None
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    status, health = _http("GET", f"{base}/api/v1/health")
                    break
                except (urllib.error.URLError, ConnectionError):
                    time.sleep(0.2)
            assert status == 200
            assert health["api_version"] == "v1"

            # token なし / 不正 token は 401
            status, body = _http("GET", f"{base}/api/v1/ready")
            assert status == 401
            assert body["code"] == "unauthorized"
            status, _ = _http("GET", f"{base}/api/v1/ready", token="wrong")
            assert status == 401

            # 正しい token で ready
            status, ready = _http("GET", f"{base}/api/v1/ready", token=TOKEN)
            assert status == 200
            assert ready["ready"] is True

            # graceful shutdown → process 残存なし
            status, body = _http(
                "POST", f"{base}/api/v1/shutdown", token=TOKEN
            )
            assert status == 202
            proc.wait(timeout=30)
            assert proc.returncode == 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
