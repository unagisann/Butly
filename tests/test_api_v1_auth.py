"""
test_api_v1_auth.py
-------------------
desktop token 認証（butly_api/auth.py）のテスト。

- token 未設定なら従来どおり全 route が開いている（dev / Streamlit 併用モード）
- token 設定時は /api/v1/*（health 除く）に Bearer token を要求する
- legacy route（/api/v1 以外）と OPTIONS は対象外
- OpenAPI に securityScheme / operation security が現れる
"""

from pathlib import Path

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from butly_api import create_app
from butly_api.context import ApiContext

TOKEN = "test-desktop-token-123"

legacy_router = APIRouter()


@legacy_router.get("/legacy-ping")
def legacy_ping():
    return {"ok": True}


def make_client(tmp_path: Path, *, auth_token=None) -> TestClient:
    instances_dir = tmp_path / "butly_core" / "instances"
    instances_dir.mkdir(parents=True, exist_ok=True)
    context = ApiContext(
        data_dir=tmp_path,
        instances_dir=instances_dir,
        runtime_supplier=lambda: object(),
        settings_loaded=True,
        auth_token=auth_token,
    )
    return TestClient(
        create_app(context=context, extra_routers=[legacy_router])
    )


class TestAuthDisabled:
    def test_open_without_token_config(self, tmp_path):
        client = make_client(tmp_path, auth_token=None)
        assert client.get("/api/v1/instances").status_code == 200
        assert client.get("/api/v1/health").status_code == 200


class TestAuthEnabled:
    def test_missing_token_401(self, tmp_path):
        client = make_client(tmp_path, auth_token=TOKEN)
        resp = client.get("/api/v1/instances")
        assert resp.status_code == 401
        body = resp.json()
        assert body["code"] == "unauthorized"
        # 401 にも request ID が付く（RequestIDMiddleware が外側）
        assert body["request_id"] == resp.headers["X-Request-ID"]
        assert resp.headers["WWW-Authenticate"] == "Bearer"

    def test_wrong_token_401(self, tmp_path):
        client = make_client(tmp_path, auth_token=TOKEN)
        resp = client.get(
            "/api/v1/instances", headers={"Authorization": "Bearer wrong"}
        )
        assert resp.status_code == 401

    def test_non_bearer_scheme_401(self, tmp_path):
        client = make_client(tmp_path, auth_token=TOKEN)
        resp = client.get(
            "/api/v1/instances", headers={"Authorization": f"Basic {TOKEN}"}
        )
        assert resp.status_code == 401

    def test_correct_token_passes(self, tmp_path):
        client = make_client(tmp_path, auth_token=TOKEN)
        resp = client.get(
            "/api/v1/instances", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert resp.status_code == 200

    def test_health_exempt(self, tmp_path):
        """起動 polling 用の health は無認証で応答する（移行計画 §6.3）"""
        client = make_client(tmp_path, auth_token=TOKEN)
        assert client.get("/api/v1/health").status_code == 200

    def test_ready_requires_token(self, tmp_path):
        client = make_client(tmp_path, auth_token=TOKEN)
        assert client.get("/api/v1/ready").status_code == 401

    def test_legacy_route_exempt(self, tmp_path):
        """/api/v1 以外（Streamlit 互換 / LINE webhook）は対象外"""
        client = make_client(tmp_path, auth_token=TOKEN)
        assert client.get("/legacy-ping").status_code == 200

    def test_options_preflight_exempt(self, tmp_path):
        """CORS preflight は認証より先に通す（401 にしない）"""
        client = make_client(tmp_path, auth_token=TOKEN)
        resp = client.options("/api/v1/instances")
        assert resp.status_code != 401


class TestOpenApiSecurity:
    def test_security_scheme_and_requirements(self, tmp_path):
        client = make_client(tmp_path, auth_token=None)
        schema = client.get("/openapi.json").json()

        schemes = schema["components"]["securitySchemes"]
        assert schemes["DesktopToken"]["type"] == "http"
        assert schemes["DesktopToken"]["scheme"] == "bearer"

        paths = schema["paths"]
        # health だけ明示的に無認証、他の operation は DesktopToken を要求
        assert paths["/api/v1/health"]["get"]["security"] == []
        assert paths["/api/v1/ready"]["get"]["security"] == [{"DesktopToken": []}]
        assert paths["/api/v1/chat"]["post"]["security"] == [{"DesktopToken": []}]
