"""
test_api_v1_schemas.py
----------------------
Phase 0 で設計した chat / history / instances contract schema の
validation 挙動テスト（endpoint 実装前の contract 固定）。
"""

import pytest
from pydantic import TypeAdapter, ValidationError
from starlette.requests import Request
from types import SimpleNamespace

from butly_api.context import ApiContext
from butly_api.errors import ApiException
from butly_api.routers.chat import (
    _debug_from_done,
    _normalize_sources,
    _safe_scores,
    _validate_debug_access,
)
from butly_api.schemas.chat import (
    AttachmentInput,
    ChatChunkEvent,
    ChatDoneEvent,
    ChatErrorEvent,
    ChatMetadataEvent,
    ChatRequest,
    ChatStreamEvent,
    Message,
    MessagePage,
)
from butly_api.schemas.instances import InstanceListResponse, InstanceSummary

STREAM_ADAPTER = TypeAdapter(ChatStreamEvent)


# ===================================================================
# ChatRequest / attachments
# ===================================================================


def test_chat_request_minimal():
    req = ChatRequest(instance_name="00_master", text="こんにちは")
    assert req.use_rag is True
    assert req.attachments == []
    assert req.client_request_id is None


def test_chat_request_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        ChatRequest(instance_name="00_master", text="hi", messagge="typo")


def test_chat_request_rejects_empty_instance_name():
    with pytest.raises(ValidationError):
        ChatRequest(instance_name="", text="hi")


def test_chat_request_rejects_more_than_three_attachments():
    att = {"mime_type": "image/png", "data_base64": "aGk="}
    with pytest.raises(ValidationError):
        ChatRequest(instance_name="a", attachments=[att] * 4)


def test_attachment_rejects_unsupported_mime():
    with pytest.raises(ValidationError):
        AttachmentInput(mime_type="image/gif", data_base64="aGk=")


def test_search_flags_are_mutually_exclusive():
    with pytest.raises(ValidationError):
        ChatRequest(
            instance_name="a", text="hi", use_google_search=True, use_web_search=True
        )
    # 片方だけなら valid
    assert ChatRequest(
        instance_name="a", text="hi", use_google_search=True
    ).use_web_search is False
    assert ChatRequest(
        instance_name="a", text="hi", use_web_search=True
    ).use_google_search is False


def test_chat_request_requires_text_or_attachment():
    with pytest.raises(ValidationError):
        ChatRequest(instance_name="a", text=" \n ")

    image_only = ChatRequest(
        instance_name="a",
        attachments=[{"mime_type": "image/png", "data_base64": "aGk="}],
    )
    assert image_only.text == ""
    assert len(image_only.attachments) == 1


def test_attachment_base64_length_limit_matches_core_constant():
    from butly_core.chat.types import MAX_ATTACHMENT_SIZE

    from butly_api.schemas.chat import MAX_DATA_BASE64_LENGTH

    # decoded 20MB を base64 化した文字長（4 * ceil(n/3)）と一致すること
    assert MAX_DATA_BASE64_LENGTH == 4 * ((MAX_ATTACHMENT_SIZE + 2) // 3)

    with pytest.raises(ValidationError):
        AttachmentInput(
            mime_type="image/png",
            data_base64="a" * (MAX_DATA_BASE64_LENGTH + 4),
        )


def test_debug_summary_drops_nonfinite_scores_and_raw_payloads():
    assert _safe_scores(
        {"ok": 0.5, "nan": float("nan"), "inf": float("inf")}
    ) == {"ok": 0.5}
    summary = _debug_from_done(
        {
            "debug_info": {
                "prompt_full": "secret prompt",
                "raw_response": "secret provider response",
                "gatekeeper": {
                    "tier": "mid",
                    "need": "past_fact",
                    "llm_scoring": {"continuity": 0.8},
                },
                "rag": {
                    "results": [{"raw": "secret memory"}],
                    "active_nodes": {
                        "prompt_included_count": 1,
                        "nodes": [{"id": "card-1", "content": "secret"}],
                    },
                },
            }
        }
    )
    rendered = summary.model_dump_json()
    assert summary.gatekeeper.tier == "mid"
    assert summary.rag.candidate_count == 1
    assert summary.rag.active_nodes == ["card-1"]
    assert "secret" not in rendered
    assert "prompt_full" not in rendered
    bounded = _safe_scores(
        {f"{index}-" + "k" * 1000: 1.0 for index in range(100)}
    )
    assert len(bounded) <= 50
    assert all(len(key) <= 100 for key in bounded)


def test_debug_access_uses_developer_mode_gate():
    body = ChatRequest(instance_name="a", text="hi", include_debug=True)

    def request(developer_mode):
        return Request(
            {
                "type": "http",
                "app": SimpleNamespace(
                    state=SimpleNamespace(
                        api_context=ApiContext(
                            developer_mode=developer_mode,
                            auth_token=None,
                        )
                    )
                ),
            }
        )

    with pytest.raises(ApiException) as caught:
        _validate_debug_access(request(False), body)
    assert caught.value.status_code == 403
    _validate_debug_access(request(True), body)


def test_public_sources_are_count_and_length_bounded():
    sources = _normalize_sources(
        [
            {
                "title": "t" * 1000,
                "url": "https://example.com/" + "x" * 5000,
            }
            for _ in range(100)
        ]
    )
    assert len(sources) == 50
    assert len(sources[0].title) == 500
    assert len(sources[0].url) == 4096


# ===================================================================
# SSE discriminated union
# ===================================================================


def test_stream_events_parse_by_discriminator():
    cases = {
        "metadata": (
            {"event": "metadata", "request_id": "r1", "data": {"tier": "mid"}},
            ChatMetadataEvent,
        ),
        "chunk": (
            {
                "event": "chunk",
                "request_id": "r1",
                "sequence": 0,
                "data": {"text": "やあ"},
            },
            ChatChunkEvent,
        ),
        "done": (
            {"event": "done", "request_id": "r1", "data": {"full_text": "やあ"}},
            ChatDoneEvent,
        ),
        "error": (
            {
                "event": "error",
                "request_id": "r1",
                "data": {"code": "provider_error", "message": "failed"},
                "recoverable": True,
            },
            ChatErrorEvent,
        ),
    }
    for payload, expected_type in cases.values():
        event = STREAM_ADAPTER.validate_python(payload)
        assert isinstance(event, expected_type)
        assert event.request_id == "r1"


def test_stream_event_rejects_unknown_event_name():
    with pytest.raises(ValidationError):
        STREAM_ADAPTER.validate_python(
            {"event": "heartbeat", "request_id": "r1", "data": {}}
        )


def test_chunk_sequence_must_be_non_negative():
    with pytest.raises(ValidationError):
        ChatChunkEvent(request_id="r1", sequence=-1, data={"text": "x"})


# ===================================================================
# Message history / instances
# ===================================================================


def test_message_defaults_keep_backward_compatibility():
    msg = Message(id="m1", role="assistant", text="やあ")
    assert msg.created_at is None
    assert msg.attachments == []
    assert msg.sources == []
    assert msg.status == "completed"


def test_message_page_cursor_is_optional():
    page = MessagePage(items=[])
    assert page.next_cursor is None
    assert page.last_interaction_at is None


def test_instance_list_response():
    res = InstanceListResponse(
        items=[InstanceSummary(name="00_master", ai_name="Butly")]
    )
    assert res.items[0].has_database is False
