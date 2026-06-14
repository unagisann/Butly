"""
LINE webhook の HTTP I/O と Messaging API 送信。

LINE SDK はこのモジュールのトップレベルでは import しない。未導入または
credential 未設定時は webhook が 503 を返すだけで、FastAPI 本体は起動できる。
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Sequence

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

import dependencies as deps
from butly_core.external.account_mapping import ExternalAccountStore
from butly_core.external.line_adapter import (
    EventDeduplicator,
    LINE_ACK_MESSAGE,
    LINE_FAILURE_MESSAGE,
    build_line_chat_request,
    message_batches,
    normalize_line_event,
    partition_reply_and_push,
    split_line_message,
    verify_line_signature,
)
from butly_core.external.pairing import PairingStore

router = APIRouter()

LINE_REPLY_TIMEOUT_SECONDS = 8.0
LINE_WEBHOOK_BODY_LIMIT = 1024 * 1024
LINE_WEBHOOK_READ_TIMEOUT_SECONDS = 3.0
_deduplicator = EventDeduplicator()


class LineMessagingSender:
    """line-bot-sdk v3 を使う非同期送信ラッパ。"""

    def __init__(self, access_token: str):
        self.access_token = access_token

    def _send(self, method: str, target: str, messages: Sequence[str]) -> None:
        from linebot.v3.messaging import (
            ApiClient,
            Configuration,
            MessagingApi,
            PushMessageRequest,
            ReplyMessageRequest,
            TextMessage,
        )

        payload = [TextMessage(text=text) for text in messages]
        with ApiClient(Configuration(access_token=self.access_token)) as api_client:
            api = MessagingApi(api_client)
            if method == "reply":
                api.reply_message(
                    ReplyMessageRequest(reply_token=target, messages=payload)
                )
            else:
                api.push_message(PushMessageRequest(to=target, messages=payload))

    async def reply(self, reply_token: str, messages: Sequence[str]) -> None:
        await asyncio.to_thread(self._send, "reply", reply_token, messages)

    async def push(self, target: str, messages: Sequence[str]) -> None:
        await asyncio.to_thread(self._send, "push", target, messages)


def _line_credentials() -> tuple[str, str]:
    secret = (os.environ.get("LINE_CHANNEL_SECRET") or "").strip()
    token = (os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") or "").strip()
    return secret, token


def _validate_with_sdk(raw_body: bytes, signature: str, channel_secret: str) -> None:
    """SDK parser にも payload を通し、LINE event schema と署名を検証する。"""
    try:
        from linebot.v3 import WebhookParser
        from linebot.v3.exceptions import InvalidSignatureError
    except ImportError as exc:
        raise RuntimeError(
            "line-bot-sdk が未導入です。requirements-line.txt をインストールしてください。"
        ) from exc

    try:
        WebhookParser(channel_secret).parse(raw_body.decode("utf-8"), signature)
    except InvalidSignatureError as exc:
        raise ValueError("invalid signature") from exc
    except Exception as exc:
        raise ValueError("invalid webhook payload") from exc


async def _push_batches(sender: Any, target: str, messages: Sequence[str]) -> None:
    for batch in message_batches(messages):
        await sender.push(target, batch)


async def _reply_answer(
    sender: Any, reply_token: str, push_target: str, answer: str
) -> None:
    reply_messages, overflow = partition_reply_and_push(answer)
    if not reply_messages:
        reply_messages = [LINE_FAILURE_MESSAGE]
    await sender.reply(reply_token, reply_messages)
    await _push_batches(sender, push_target, overflow)


async def process_line_event(
    event: dict,
    *,
    runtime: Any,
    account_store: ExternalAccountStore,
    pairing_store: PairingStore,
    sender: Any,
    reply_timeout: float = LINE_REPLY_TIMEOUT_SECONDS,
) -> None:
    """1 件の LINE event を pairing guard 経由で処理する。"""
    normalized = normalize_line_event(event)
    if normalized is None or not normalized.text.strip():
        return

    # 初期版は 1:1 のみ。グループ/ルームでは pairing code も表示しない。
    if not normalized.is_direct_message:
        return

    resolved = account_store.resolve_line(
        user_id=normalized.user_id, group_id=normalized.channel_id
    )
    if resolved is None:
        pending = pairing_store.issue(
            source="line",
            external_user_id=normalized.user_id,
            external_channel_id=normalized.channel_id,
        )
        await sender.reply(
            normalized.reply_token,
            [
                f"連携コード: {pending.code}\n"
                "管理画面の「LINE連携」から10分以内に承認してください。"
            ],
        )
        return

    if not resolved.exists:
        await sender.reply(
            normalized.reply_token,
            ["連携先の Butly instance が見つかりません。管理画面で再承認してください。"],
        )
        return

    request = build_line_chat_request(
        text=normalized.text,
        instance_name=resolved.instance_name,
        user_id=normalized.user_id,
        channel_id=normalized.channel_id,
    )
    generation = asyncio.create_task(runtime.chat(request))
    done, _ = await asyncio.wait({generation}, timeout=reply_timeout)

    if generation in done:
        try:
            response = generation.result()
        except Exception:
            await sender.reply(normalized.reply_token, [LINE_FAILURE_MESSAGE])
            return
        await _reply_answer(
            sender, normalized.reply_token, normalized.push_target, response.text
        )
        return

    # asyncio.wait は timeout 時に generation をキャンセルしない。
    try:
        await sender.reply(normalized.reply_token, [LINE_ACK_MESSAGE])
    except Exception as exc:
        # reply が失敗しても生成を回収し、push 本回答を試みる。
        print(f"[line] ack reply failed: {type(exc).__name__}")
    try:
        response = await generation
        messages = split_line_message(response.text) or [LINE_FAILURE_MESSAGE]
    except Exception:
        messages = [LINE_FAILURE_MESSAGE]
    await _push_batches(sender, normalized.push_target, messages)


async def process_line_events(
    events: Sequence[dict],
    *,
    runtime: Any,
    account_store: ExternalAccountStore,
    pairing_store: PairingStore,
    sender: Any,
) -> None:
    for event in events:
        try:
            await process_line_event(
                event,
                runtime=runtime,
                account_store=account_store,
                pairing_store=pairing_store,
                sender=sender,
            )
        except Exception as exc:
            # 外部 ID、token、secret、本文はログに出さない。
            print(f"[line] event processing failed: {type(exc).__name__}")


@router.post("/line/webhook")
async def line_webhook(request: Request, background_tasks: BackgroundTasks):
    channel_secret, access_token = _line_credentials()
    if not channel_secret or not access_token:
        raise HTTPException(status_code=503, detail="LINE integration is disabled.")

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > LINE_WEBHOOK_BODY_LIMIT:
                raise HTTPException(status_code=413, detail="Webhook body is too large.")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length.")

    try:
        raw_body = await asyncio.wait_for(
            request.body(), timeout=LINE_WEBHOOK_READ_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=408, detail="Webhook body read timed out.")
    if len(raw_body) > LINE_WEBHOOK_BODY_LIMIT:
        raise HTTPException(status_code=413, detail="Webhook body is too large.")

    signature = request.headers.get("x-line-signature", "")
    if not verify_line_signature(raw_body, signature, channel_secret):
        raise HTTPException(status_code=400, detail="Invalid LINE signature.")
    try:
        _validate_with_sdk(raw_body, signature, channel_secret)
        payload = json.loads(raw_body)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Invalid LINE webhook payload.")

    raw_events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(raw_events, list):
        raise HTTPException(status_code=400, detail="Invalid LINE webhook payload.")

    events = [
        event
        for event in raw_events
        if isinstance(event, dict)
        and _deduplicator.accept(event.get("webhookEventId"))
    ]
    if events:
        account_store = ExternalAccountStore(deps.DATA_DIR, deps.INSTANCES_DIR)
        pairing_store = PairingStore(deps.DATA_DIR, account_store)
        background_tasks.add_task(
            process_line_events,
            events,
            runtime=deps.runtime,
            account_store=account_store,
            pairing_store=pairing_store,
            sender=LineMessagingSender(access_token),
        )
    return {"ok": True}
