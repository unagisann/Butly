import asyncio
import base64
import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import dependencies as deps
from butly_core.external.account_mapping import ExternalAccountStore
from butly_core.external.line_adapter import EventDeduplicator, LINE_ACK_MESSAGE
from butly_core.external.pairing import PairingStore
from routers import line as line_router
from routers import pairing as pairing_router


def _event(*, source=None, event_id="event-1", text="hello"):
    return {
        "type": "message",
        "webhookEventId": event_id,
        "replyToken": f"reply-{event_id}",
        "source": source or {"type": "user", "userId": "user-1"},
        "message": {"type": "text", "text": text},
    }


class FakeSender:
    def __init__(self):
        self.replies = []
        self.pushes = []

    async def reply(self, token, messages):
        self.replies.append((token, list(messages)))

    async def push(self, target, messages):
        self.pushes.append((target, list(messages)))


class AckFailingSender(FakeSender):
    async def reply(self, token, messages):
        if messages == [LINE_ACK_MESSAGE]:
            raise RuntimeError("reply failed")
        await super().reply(token, messages)


class FakeRuntime:
    def __init__(self, text="answer", delay=0):
        self.text = text
        self.delay = delay
        self.requests = []
        self.completed = False

    async def chat(self, request):
        self.requests.append(request)
        await asyncio.sleep(self.delay)
        self.completed = True
        return SimpleNamespace(text=self.text)


@pytest.fixture
def stores(tmp_path):
    instances_dir = tmp_path / "butly_core" / "instances"
    (instances_dir / "Butly").mkdir(parents=True)
    account_store = ExternalAccountStore(tmp_path, instances_dir)
    return account_store, PairingStore(tmp_path, account_store)


def test_unapproved_user_receives_pairing_without_chat(stores):
    account_store, pairing_store = stores
    sender = FakeSender()
    runtime = FakeRuntime()

    asyncio.run(
        line_router.process_line_event(
            _event(),
            runtime=runtime,
            account_store=account_store,
            pairing_store=pairing_store,
            sender=sender,
        )
    )

    assert runtime.requests == []
    assert len(pairing_store.pending()) == 1
    assert "連携コード:" in sender.replies[0][1][0]


def test_group_event_is_ignored_even_when_user_is_approved(stores):
    account_store, pairing_store = stores
    account_store.bind_external_user(
        source="line", user_id="user-1", instance_name="Butly"
    )
    sender = FakeSender()
    runtime = FakeRuntime()

    asyncio.run(
        line_router.process_line_event(
            _event(
                source={"type": "group", "userId": "user-1", "groupId": "group-1"}
            ),
            runtime=runtime,
            account_store=account_store,
            pairing_store=pairing_store,
            sender=sender,
        )
    )

    assert runtime.requests == []
    assert sender.replies == []


def test_fast_generation_replies_with_answer(stores):
    account_store, pairing_store = stores
    account_store.bind_external_user(
        source="line", user_id="user-1", instance_name="Butly"
    )
    sender = FakeSender()
    runtime = FakeRuntime(text="fast answer")

    asyncio.run(
        line_router.process_line_event(
            _event(),
            runtime=runtime,
            account_store=account_store,
            pairing_store=pairing_store,
            sender=sender,
            reply_timeout=0.1,
        )
    )

    assert sender.replies == [("reply-event-1", ["fast answer"])]
    assert sender.pushes == []
    assert runtime.requests[0].source == "line"


def test_timeout_replies_ack_then_pushes_and_does_not_cancel(stores):
    account_store, pairing_store = stores
    account_store.bind_external_user(
        source="line", user_id="user-1", instance_name="Butly"
    )
    sender = FakeSender()
    runtime = FakeRuntime(text="slow answer", delay=0.02)

    asyncio.run(
        line_router.process_line_event(
            _event(),
            runtime=runtime,
            account_store=account_store,
            pairing_store=pairing_store,
            sender=sender,
            reply_timeout=0.001,
        )
    )

    assert sender.replies == [("reply-event-1", [LINE_ACK_MESSAGE])]
    assert sender.pushes == [("user-1", ["slow answer"])]
    assert runtime.completed


def test_timeout_still_pushes_when_ack_reply_fails(stores):
    account_store, pairing_store = stores
    account_store.bind_external_user(
        source="line", user_id="user-1", instance_name="Butly"
    )
    sender = AckFailingSender()
    runtime = FakeRuntime(text="slow answer", delay=0.02)

    asyncio.run(
        line_router.process_line_event(
            _event(),
            runtime=runtime,
            account_store=account_store,
            pairing_store=pairing_store,
            sender=sender,
            reply_timeout=0.001,
        )
    )

    assert sender.pushes == [("user-1", ["slow answer"])]
    assert runtime.completed


@pytest.fixture
def webhook_client(tmp_path, monkeypatch):
    instances_dir = tmp_path / "butly_core" / "instances"
    (instances_dir / "Butly").mkdir(parents=True)
    deps.DATA_DIR = tmp_path
    deps.INSTANCES_DIR = instances_dir
    deps.runtime = FakeRuntime()
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "secret")
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "token")
    monkeypatch.setattr(line_router, "_validate_with_sdk", lambda *args: None)
    monkeypatch.setattr(line_router, "_deduplicator", EventDeduplicator())

    app = FastAPI()
    app.include_router(line_router.router)
    app.include_router(pairing_router.router)
    return TestClient(app, base_url="http://localhost")


def _signed_request(client, payload):
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = base64.b64encode(
        hmac.new(b"secret", body, hashlib.sha256).digest()
    ).decode()
    return client.post(
        "/line/webhook",
        content=body,
        headers={"X-Line-Signature": signature, "Content-Type": "application/json"},
    )


def test_verify_webhook_accepts_empty_events(webhook_client):
    response = _signed_request(webhook_client, {"events": []})
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_webhook_rejects_invalid_signature(webhook_client):
    response = webhook_client.post(
        "/line/webhook",
        content=b'{"events":[]}',
        headers={"X-Line-Signature": "invalid"},
    )
    assert response.status_code == 400


def test_webhook_is_disabled_without_credentials(monkeypatch):
    monkeypatch.delenv("LINE_CHANNEL_SECRET", raising=False)
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(line_router.router)

    response = TestClient(app).post("/line/webhook", content=b'{"events":[]}')

    assert response.status_code == 503


def test_webhook_deduplicates_before_background_processing(
    webhook_client, monkeypatch
):
    seen = []

    async def record(events, **kwargs):
        seen.extend(events)

    monkeypatch.setattr(line_router, "process_line_events", record)
    payload = {"events": [_event(event_id="same")]}
    assert _signed_request(webhook_client, payload).status_code == 200
    assert _signed_request(webhook_client, payload).status_code == 200
    assert len(seen) == 1


def test_pairing_api_can_approve_pending(webhook_client):
    account_store = ExternalAccountStore(deps.DATA_DIR, deps.INSTANCES_DIR)
    pending = PairingStore(deps.DATA_DIR, account_store).issue(
        source="line", external_user_id="user-1"
    )
    response = webhook_client.post(
        "/pairing/approve", json={"code": pending.code, "instance_name": "Butly"}
    )
    assert response.status_code == 200
    assert account_store.resolve_line(user_id="user-1").instance_name == "Butly"


def test_pairing_api_rejects_forwarded_external_request(webhook_client):
    response = webhook_client.get(
        "/pairing/pending", headers={"CF-Connecting-IP": "203.0.113.10"}
    )
    assert response.status_code == 403
