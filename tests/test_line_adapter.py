import base64
import hashlib
import hmac

from butly_core.external.line_adapter import (
    EventDeduplicator,
    build_line_chat_request,
    message_batches,
    normalize_line_event,
    partition_reply_and_push,
    split_line_message,
    utf16_length,
    verify_line_signature,
)
from butly_core.external.reply_profiles import LINE_PROFILE, get_profile


def _event(source=None):
    return {
        "type": "message",
        "webhookEventId": "event-1",
        "replyToken": "reply-1",
        "source": source or {"type": "user", "userId": "user-1"},
        "message": {"type": "text", "text": "こんにちは"},
    }


def test_signature_verification_uses_raw_body():
    body = b'{"events":[]}'
    secret = "secret"
    signature = base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha256).digest()
    ).decode()

    assert verify_line_signature(body, signature, secret)
    assert not verify_line_signature(body + b" ", signature, secret)
    assert not verify_line_signature(body, "invalid", secret)


def test_normalize_direct_text_event():
    normalized = normalize_line_event(_event())
    assert normalized.text == "こんにちは"
    assert normalized.user_id == "user-1"
    assert normalized.channel_id is None
    assert normalized.push_target == "user-1"
    assert normalized.is_direct_message


def test_normalize_group_event_keeps_group_as_push_target():
    normalized = normalize_line_event(
        _event({"type": "group", "userId": "user-1", "groupId": "group-1"})
    )
    assert normalized.channel_id == "group-1"
    assert normalized.push_target == "group-1"
    assert not normalized.is_direct_message


def test_normalize_ignores_non_text_event():
    event = _event()
    event["message"] = {"type": "image", "id": "image-1"}
    assert normalize_line_event(event) is None


def test_build_chat_request_uses_line_profile_without_polluting_text():
    request = build_line_chat_request(
        text="普通の本文",
        instance_name="Butly",
        user_id="secret-user",
        channel_id=None,
    )
    assert request.text == "普通の本文"
    assert "secret-user" not in request.text
    assert request.source == "line"
    assert request.external_user_id == "secret-user"
    assert request.metadata["reply_profile"] == "line"
    assert request.metadata["hard_char_limit"] == 4500
    assert get_profile("line") == LINE_PROFILE


def test_reply_is_limited_to_five_and_overflow_is_pushed():
    reply, push = partition_reply_and_push("x" * (4500 * 6 + 1))
    assert len(reply) == 5
    assert len(push) == 2
    assert all(len(chunk) <= 4500 for chunk in reply + push)


def test_line_splitter_counts_utf16_code_units():
    chunks = split_line_message("😀" * 3000)
    assert len(chunks) == 2
    assert all(utf16_length(chunk) <= 4500 for chunk in chunks)
    assert "".join(chunks) == "😀" * 3000


def test_message_batches_obey_api_limit():
    assert message_batches(["1", "2", "3", "4", "5", "6"]) == [
        ["1", "2", "3", "4", "5"],
        ["6"],
    ]


def test_event_deduplicator_accepts_once_and_evicts_lru():
    dedup = EventDeduplicator(max_size=2)
    assert dedup.accept("a")
    assert not dedup.accept("a")
    assert dedup.accept("b")
    assert dedup.accept("c")
    assert dedup.accept("a")
    assert dedup.accept(None)
