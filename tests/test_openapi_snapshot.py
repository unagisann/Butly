"""
test_openapi_snapshot.py
------------------------
OpenAPI 生成の deterministic 性・snapshot 一致・contract 規約のテスト。
"""

import json
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = PROJECT_ROOT / "openapi" / "butly.openapi.json"


def _render() -> str:
    import sys

    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    from generate_openapi import render_openapi_json

    return render_openapi_json()


@pytest.fixture(scope="module")
def spec() -> dict:
    return json.loads(_render())


def test_generation_is_deterministic():
    assert _render() == _render()


def test_snapshot_matches_generated_spec():
    assert SNAPSHOT_PATH.exists(), (
        "openapi/butly.openapi.json がありません。"
        "scripts/generate_openapi.py を実行してください。"
    )
    on_disk = SNAPSHOT_PATH.read_text(encoding="utf-8")
    assert on_disk == _render(), (
        "OpenAPI snapshot が現在の schema と一致しません。"
        "venv/bin/python scripts/generate_openapi.py で再生成してください。"
    )


def test_spec_is_openapi_31(spec):
    assert spec["openapi"].startswith("3.1")


def test_all_v1_operations_have_stable_ids_tags_and_responses(spec):
    seen_ids = set()
    for path, operations in spec["paths"].items():
        assert path.startswith("/api/v1"), f"versioned path 以外が混入: {path}"
        for method, op in operations.items():
            label = f"{method.upper()} {path}"
            assert op.get("operationId"), f"{label} に operationId がない"
            assert op["operationId"] not in seen_ids, f"operationId 重複: {label}"
            seen_ids.add(op["operationId"])
            assert op.get("tags"), f"{label} に tags がない"
            assert op.get("summary"), f"{label} に summary がない"
            assert "200" in op["responses"], f"{label} に成功 response がない"
            content = op["responses"]["200"]["content"]["application/json"]
            assert "schema" in content, f"{label} に response schema がない"


def test_contract_schemas_are_published(spec):
    schemas = spec["components"]["schemas"]
    for name in [
        "ApiError",
        "ChatRequest",
        "ChatResult",
        "Message",
        "MessagePage",
        "InstanceSummary",
        "InstanceListResponse",
        "ChatStreamEvent",
        "HealthResponse",
        "ReadinessResponse",
        "AppInfoResponse",
        "CapabilitiesResponse",
    ]:
        assert name in schemas, f"components.schemas に {name} がない"


def test_chat_stream_event_is_discriminated_union(spec):
    stream = spec["components"]["schemas"]["ChatStreamEvent"]
    assert "oneOf" in stream
    discriminator = stream["discriminator"]
    assert discriminator["propertyName"] == "event"
    assert set(discriminator["mapping"]) == {"metadata", "chunk", "done", "error"}


def test_api_error_requires_all_envelope_keys(spec):
    api_error = spec["components"]["schemas"]["ApiError"]
    assert sorted(api_error["required"]) == [
        "code",
        "details",
        "message",
        "request_id",
    ]


def test_openapi_excludes_legacy_routes():
    """extra_routers で legacy route を同居させても公開 contract は /api/v1 のみ。"""
    from fastapi import APIRouter
    from fastapi.testclient import TestClient

    from butly_api import create_app

    legacy_router = APIRouter()

    @legacy_router.get("/instances")
    def legacy_instances():
        return []

    @legacy_router.get("/api/v1evil")
    def prefix_lookalike():
        return {}

    app = create_app(extra_routers=[legacy_router])
    paths = app.openapi()["paths"]
    assert all(p.startswith("/api/v1/") for p in paths)
    assert "/instances" not in paths
    assert "/api/v1evil" not in paths

    # runtime の legacy route は生きている
    client = TestClient(app)
    assert client.get("/instances").status_code == 200
    assert client.get("/openapi.json").json()["paths"].keys() == paths.keys()


def test_spec_contains_no_user_or_secret_data(spec):
    text = json.dumps(spec, ensure_ascii=False)
    for needle in ["API_KEY", "api_key=", "/home/", "C:\\\\", ".env"]:
        assert needle not in text, f"OpenAPI に想定外の文字列が混入: {needle}"
