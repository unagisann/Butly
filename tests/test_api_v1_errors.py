"""
test_api_v1_errors.py
---------------------
/api/v1 の共通 error envelope（404 / 409 / 422 / 500）と
legacy route の default 挙動維持のテスト。
"""

import logging

import pytest
from fastapi import APIRouter, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from butly_api import create_app
from butly_api.errors import ApiException

ENVELOPE_KEYS = {"code", "message", "details", "request_id"}


class _EchoBody(BaseModel):
    name: str
    count: int


def _build_app():
    """error 経路検証用の一時 endpoint を足した app を作る。"""
    test_router = APIRouter()

    @test_router.post("/api/v1/_test/echo")
    def echo(body: _EchoBody):
        return {"name": body.name}

    @test_router.get("/api/v1/_test/conflict")
    def conflict():
        raise ApiException(
            409, "instance_already_exists", "Instance 'foo' already exists."
        )

    @test_router.get("/api/v1/_test/http404")
    def http404():
        raise HTTPException(status_code=404, detail="Instance 'foo' was not found.")

    @test_router.get("/api/v1/_test/boom")
    def boom():
        raise RuntimeError("secret internal detail should not leak")

    @test_router.get("/legacy/_test/http404")
    def legacy_http404():
        raise HTTPException(status_code=404, detail="legacy not found")

    @test_router.get("/legacy/_test/boom")
    def legacy_boom():
        raise RuntimeError("legacy boom")

    @test_router.get("/api/v1evil")
    def prefix_lookalike():
        raise HTTPException(status_code=404, detail="lookalike")

    @test_router.get("/api/v10/foo")
    def v10_route():
        raise HTTPException(status_code=404, detail="v10")

    return create_app(extra_routers=[test_router])


@pytest.fixture
def client():
    return TestClient(_build_app(), raise_server_exceptions=False)


# ===================================================================
# /api/v1: envelope への正規化
# ===================================================================


def test_404_unknown_path_is_enveloped(client):
    res = client.get("/api/v1/no-such-endpoint")
    assert res.status_code == 404
    body = res.json()
    assert set(body) == ENVELOPE_KEYS
    assert body["code"] == "not_found"
    assert body["request_id"]


def test_404_http_exception_is_enveloped(client):
    res = client.get("/api/v1/_test/http404")
    assert res.status_code == 404
    body = res.json()
    assert body["code"] == "not_found"
    assert body["message"] == "Instance 'foo' was not found."
    assert "detail" not in body


def test_409_api_exception(client):
    res = client.get("/api/v1/_test/conflict")
    assert res.status_code == 409
    body = res.json()
    assert body["code"] == "instance_already_exists"
    assert set(body) == ENVELOPE_KEYS


def test_422_validation_error_enveloped_without_input_echo(client):
    res = client.post(
        "/api/v1/_test/echo", json={"name": "x", "count": "not-a-number"}
    )
    assert res.status_code == 422
    body = res.json()
    assert body["code"] == "validation_error"
    assert set(body) == ENVELOPE_KEYS
    errors = body["details"]["errors"]
    assert errors and all(set(e) == {"loc", "msg", "type"} for e in errors)
    # 入力値の echo back をしない（secret 混入防止）
    assert "not-a-number" not in str(body)


def test_500_enveloped_without_exception_string(client):
    res = client.get("/api/v1/_test/boom")
    assert res.status_code == 500
    body = res.json()
    assert body["code"] == "internal_error"
    assert body["request_id"]
    assert "secret internal detail" not in str(body)


def test_error_response_carries_request_id_header(client):
    res = client.get(
        "/api/v1/_test/conflict", headers={"X-Request-ID": "trace-me"}
    )
    assert res.headers["X-Request-ID"] == "trace-me"
    assert res.json()["request_id"] == "trace-me"


# ===================================================================
# legacy route: 既存挙動の維持
# ===================================================================


def test_legacy_http_exception_keeps_detail_shape(client):
    res = client.get("/legacy/_test/http404")
    assert res.status_code == 404
    assert res.json() == {"detail": "legacy not found"}


def test_legacy_500_keeps_plain_text(client):
    res = client.get("/legacy/_test/boom")
    assert res.status_code == 500
    assert res.text == "Internal Server Error"


def test_legacy_500_is_logged(client, caplog):
    caplog.set_level(logging.ERROR, logger="butly_api.errors")
    res = client.get("/legacy/_test/boom")
    assert res.status_code == 500
    assert "[legacy] unhandled error" in caplog.text
    assert "legacy boom" in caplog.text


def test_v1_prefix_lookalikes_are_not_enveloped(client):
    """/api/v1evil や /api/v10/... は v1 contract の対象外（default 挙動維持）。"""
    for path in ["/api/v1evil", "/api/v10/foo"]:
        res = client.get(path)
        assert res.status_code == 404
        assert set(res.json()) == {"detail"}, path

    # 未定義 path も同様に default 404
    res = client.get("/api/v10/no-such")
    assert res.json() == {"detail": "Not Found"}
