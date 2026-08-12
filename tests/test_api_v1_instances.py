"""
test_api_v1_instances.py
------------------------
/api/v1/instances と /api/v1/instances/{name}/messages の contract テスト。
tmp ディレクトリ上の instance 構造だけを使い、実 instance には触れない。
"""

import json
import os
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from butly_api import create_app
from butly_api.context import ApiContext


@pytest.fixture
def instances_dir(tmp_path: Path) -> Path:
    # ButlyMemory の layout 契約（base_dir/butly_core/instances）に合わせる
    d = tmp_path / "butly_core" / "instances"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def client(tmp_path: Path, instances_dir: Path) -> TestClient:
    context = ApiContext(
        data_dir=tmp_path,
        instances_dir=instances_dir,
        runtime_supplier=lambda: object(),
        settings_loaded=True,
    )
    return TestClient(create_app(context=context))


def _make_instance(instances_dir: Path, name: str, config: dict | None = None) -> Path:
    inst = instances_dir / name
    inst.mkdir()
    if config is not None:
        (inst / "config.json").write_text(
            json.dumps(config, ensure_ascii=False), encoding="utf-8"
        )
    return inst


def _write_turn(
    inst: Path,
    filename: str,
    timestamp: str,
    user_text: str,
    model_text: str,
    *,
    mtime: float | None = None,
) -> Path:
    d = inst / "short_term_json"
    d.mkdir(exist_ok=True)
    f = d / filename
    f.write_text(
        json.dumps(
            {
                "timestamp": timestamp,
                "messages": [
                    {"role": "user", "parts": [user_text]},
                    {"role": "model", "parts": [model_text]},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    if mtime is not None:
        os.utime(f, (mtime, mtime))
    return f


def test_history_reload_preserves_safe_attachment_and_citation_metadata(
    tmp_path, instances_dir
):
    from butly_api.routers.instances import _normalize_messages
    from butly_core.chat.service import (
        _assistant_turn_meta,
        _build_turn_meta,
    )
    from butly_core.chat.types import Attachment, ChatRequest
    from butly_core.core.memory import ButlyMemory

    _make_instance(instances_dir, "phase2")
    memory = ButlyMemory(tmp_path, instance_name="phase2")
    request = ChatRequest(
        instance_name="phase2",
        text="この画像は？",
        source="api",
        attachments=[
            Attachment(
                mime_type="image/png",
                data_base64="aGk=",
                name="tiny.png",
            )
        ],
    )
    user_meta = _build_turn_meta(request)
    assistant_meta = _assistant_turn_meta(
        [{"title": "Reference", "uri": "https://example.com/source"}]
    )
    filename = memory.save_single_turn(
        request.text,
        "小さな画像です。",
        meta=user_meta,
        assistant_meta=assistant_meta,
    )

    saved = (memory.short_term_json_dir / filename).read_text(encoding="utf-8")
    assert "data_base64" not in saved
    assert "aGk=" not in saved
    raw, _timestamp = memory.load_recent_sessions(limit=10)
    messages = _normalize_messages(raw)
    assert messages[0].attachments[0].mime_type == "image/png"
    assert messages[0].attachments[0].name == "tiny.png"
    assert messages[0].attachments[0].size_bytes == 2
    assert messages[1].sources[0].title == "Reference"
    assert messages[1].sources[0].url == "https://example.com/source"


def test_history_meta_is_bounded_and_invalid_attachments_are_skipped():
    from butly_api.routers.instances import _normalize_messages

    raw = [
        {
            "role": "user",
            "parts": ["image"],
            "meta": {
                "attachments": [
                    {"kind": "file", "mime_type": "application/pdf"},
                    *(
                        {
                            "kind": "image",
                            "mime_type": "image/png",
                            "name": "n" * 1000,
                            "size_bytes": 1,
                        }
                        for _ in range(10)
                    ),
                ]
            },
        },
        {
            "role": "model",
            "parts": ["answer"],
            "meta": {
                "sources": [
                    {"title": "t" * 1000, "url": "u" * 5000}
                    for _ in range(100)
                ]
            },
        },
    ]
    messages = _normalize_messages(raw)
    # First three entries include the invalid leading entry, so only two valid
    # attachment summaries survive the global MAX_ATTACHMENTS bound.
    assert len(messages[0].attachments) == 2
    assert all(item.kind == "image" for item in messages[0].attachments)
    assert all(len(item.name) == 255 for item in messages[0].attachments)
    assert len(messages[1].sources) == 50
    assert len(messages[1].sources[0].title) == 500
    assert len(messages[1].sources[0].url) == 4096


# ===================================================================
# GET /api/v1/instances
# ===================================================================


def test_list_instances_empty(client):
    res = client.get("/api/v1/instances")
    assert res.status_code == 200
    assert res.json() == {"items": []}


def test_list_instances_requires_context():
    client = TestClient(create_app())
    res = client.get("/api/v1/instances")
    assert res.status_code == 503
    body = res.json()
    assert body["code"] == "backend_not_ready"
    assert set(body) == {"code", "message", "details", "request_id"}


def test_list_instances_returns_summaries(client, instances_dir):
    _make_instance(
        instances_dir,
        "alpha",
        {"agent_profile": {"ai_name": "アルファ", "locale": "ja"}},
    )
    beta = _make_instance(instances_dir, "beta")
    (beta / "butly_memory.db").write_bytes(b"")
    # 無視対象: hidden directory とただの file
    (instances_dir / ".hidden").mkdir()
    (instances_dir / "stray.txt").write_text("x", encoding="utf-8")

    res = client.get("/api/v1/instances")
    assert res.status_code == 200
    items = {i["name"]: i for i in res.json()["items"]}
    assert set(items) == {"alpha", "beta"}

    assert items["alpha"]["ai_name"] == "アルファ"
    assert items["alpha"]["locale"] == "ja"
    assert items["alpha"]["has_database"] is False

    assert items["beta"]["ai_name"] is None
    assert items["beta"]["has_database"] is True


def test_list_instances_legacy_agent_section(client, instances_dir):
    _make_instance(instances_dir, "old", {"agent": {"ai_name": "旧形式", "locale": "en"}})
    items = client.get("/api/v1/instances").json()["items"]
    assert items[0]["ai_name"] == "旧形式"
    assert items[0]["locale"] == "en"


def test_list_instances_updated_at_from_memory_files(client, instances_dir):
    inst = _make_instance(instances_dir, "alpha")
    _write_turn(inst, "session_1.json", "2026-01-01T12:00:00", "hi", "hello")
    body = client.get("/api/v1/instances").json()
    updated_at = body["items"][0]["updated_at"]
    assert updated_at is not None
    # tz-aware ISO 8601 であること
    assert datetime.fromisoformat(updated_at).tzinfo is not None


def test_list_instances_survives_broken_config(client, instances_dir, tmp_path):
    inst = _make_instance(instances_dir, "broken")
    (inst / "config.json").write_text("{not-json", encoding="utf-8")
    res = client.get("/api/v1/instances")
    assert res.status_code == 200
    items = res.json()["items"]
    assert items[0]["name"] == "broken"
    assert items[0]["ai_name"] is None
    # file path / 内部情報が response に漏れない
    assert str(tmp_path) not in res.text


# ===================================================================
# GET /api/v1/instances/{name}/messages
# ===================================================================


def test_messages_unknown_instance_returns_typed_404(client):
    res = client.get("/api/v1/instances/no_such/messages")
    assert res.status_code == 404
    body = res.json()
    assert body["code"] == "instance_not_found"
    assert set(body) == {"code", "message", "details", "request_id"}


def test_messages_rejects_path_traversal_names(client, instances_dir):
    _make_instance(instances_dir, "alpha")
    for name in ["..", ".hidden", "%2e%2e"]:
        res = client.get(f"/api/v1/instances/{name}/messages")
        assert res.status_code == 404, name
        assert res.json()["code"] in {"instance_not_found", "not_found"}, name


def test_messages_empty_history(client, instances_dir):
    _make_instance(instances_dir, "alpha")
    res = client.get("/api/v1/instances/alpha/messages")
    assert res.status_code == 200
    body = res.json()
    assert body["items"] == []
    assert body["next_cursor"] is None
    assert body["last_interaction_at"] is None


def test_messages_normalizes_legacy_turn_format(client, instances_dir):
    inst = _make_instance(instances_dir, "alpha")
    _write_turn(inst, "session_1.json", "2026-01-01T12:00:00", "今日の天気は？", "晴れです。")

    res = client.get("/api/v1/instances/alpha/messages")
    assert res.status_code == 200
    body = res.json()
    items = body["items"]
    assert [m["role"] for m in items] == ["user", "assistant"]
    assert items[0]["text"] == "今日の天気は？"
    assert items[1]["text"] == "晴れです。"
    for m in items:
        assert m["attachments"] == []
        assert m["sources"] == []
        assert m["status"] == "completed"
        assert m["created_at"] is not None
        assert datetime.fromisoformat(m["created_at"]).tzinfo is not None
    assert body["last_interaction_at"] is not None
    # 現行 storage では cursor pagination 未実装（router のコメント参照）
    assert body["next_cursor"] is None


def test_messages_handles_dict_parts(client, instances_dir):
    inst = _make_instance(instances_dir, "alpha")
    d = inst / "short_term_json"
    d.mkdir(exist_ok=True)
    (d / "session_1.json").write_text(
        json.dumps(
            {
                "timestamp": "2026-01-01T12:00:00",
                "messages": [{"role": "user", "parts": [{"text": "dict形式"}]}],
            }
        ),
        encoding="utf-8",
    )
    items = client.get("/api/v1/instances/alpha/messages").json()["items"]
    assert items[0]["text"] == "dict形式"


def test_messages_ids_are_deterministic_and_unique(client, instances_dir):
    inst = _make_instance(instances_dir, "alpha")
    # 同一内容の turn を2つ（重複 text でも ID が衝突しないこと）
    _write_turn(inst, "session_1.json", "2026-01-01T12:00:00", "同じ", "同じ", mtime=1000)
    _write_turn(inst, "session_2.json", "2026-01-01T12:00:00", "同じ", "同じ", mtime=2000)

    first = client.get("/api/v1/instances/alpha/messages").json()["items"]
    second = client.get("/api/v1/instances/alpha/messages").json()["items"]

    ids = [m["id"] for m in first]
    assert len(set(ids)) == len(ids)
    assert ids == [m["id"] for m in second]


def test_messages_respects_limit(client, instances_dir):
    inst = _make_instance(instances_dir, "alpha")
    for i in range(3):
        _write_turn(
            inst,
            f"session_{i}.json",
            f"2026-01-0{i + 1}T12:00:00",
            f"質問{i}",
            f"応答{i}",
            mtime=1000 + i,
        )

    res = client.get("/api/v1/instances/alpha/messages", params={"limit": 2})
    items = res.json()["items"]
    assert len(items) == 2
    # 最新 turn の user → assistant が時系列で返る
    assert [m["text"] for m in items] == ["質問2", "応答2"]


def test_messages_limit_validation_uses_envelope(client, instances_dir):
    _make_instance(instances_dir, "alpha")
    res = client.get("/api/v1/instances/alpha/messages", params={"limit": 999})
    assert res.status_code == 422
    assert res.json()["code"] == "validation_error"
