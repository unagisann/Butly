"""
LINE Messaging API と Butly の間で使う SDK 非依存の純粋ヘルパー。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
from typing import Any, Mapping, Optional, Sequence

from butly_core.chat.types import ChatRequest
from butly_core.external.message_splitter import split_message
from butly_core.external.reply_profiles import LINE_PROFILE, ReplyProfile

LINE_REPLY_MAX_MESSAGES = 5
LINE_ACK_MESSAGE = "ただいま考えています。少しお待ちください。"
LINE_FAILURE_MESSAGE = "申し訳ありません、応答の生成に失敗しました。"


@dataclass(frozen=True)
class NormalizedLineEvent:
    webhook_event_id: Optional[str]
    reply_token: str
    text: str
    user_id: str
    channel_id: Optional[str]
    source_type: str

    @property
    def is_direct_message(self) -> bool:
        return self.source_type == "user"

    @property
    def push_target(self) -> str:
        return self.channel_id or self.user_id


def verify_line_signature(raw_body: bytes, signature: str, channel_secret: str) -> bool:
    """LINE の ``X-Line-Signature`` を raw body に対して検証する。"""
    if not signature or not channel_secret:
        return False
    digest = hmac.new(
        channel_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature)


def normalize_line_event(event: Mapping[str, Any]) -> Optional[NormalizedLineEvent]:
    """text message event を内部表現に正規化する。対象外イベントは ``None``。"""
    if event.get("type") != "message":
        return None
    message = event.get("message")
    source = event.get("source")
    if not isinstance(message, Mapping) or message.get("type") != "text":
        return None
    if not isinstance(source, Mapping):
        return None

    text = message.get("text")
    user_id = source.get("userId")
    reply_token = event.get("replyToken")
    source_type = source.get("type")
    if not all(isinstance(v, str) and v for v in (text, user_id, reply_token, source_type)):
        return None

    channel_id = None
    if source_type == "group":
        channel_id = source.get("groupId")
    elif source_type == "room":
        channel_id = source.get("roomId")
    elif source_type != "user":
        return None

    if channel_id is not None and not isinstance(channel_id, str):
        return None
    return NormalizedLineEvent(
        webhook_event_id=event.get("webhookEventId"),
        reply_token=reply_token,
        text=text,
        user_id=user_id,
        channel_id=channel_id,
        source_type=source_type,
    )


def build_line_chat_request(
    *,
    text: str,
    instance_name: str,
    user_id: str,
    channel_id: Optional[str],
    profile: ReplyProfile = LINE_PROFILE,
) -> ChatRequest:
    """LINE 受信内容から共通 ``ChatRequest`` を組み立てる。"""
    return ChatRequest(
        text=text,
        instance_name=instance_name,
        source="line",
        external_user_id=str(user_id),
        external_channel_id=str(channel_id) if channel_id is not None else None,
        metadata=profile.to_metadata(),
    )


def split_line_message(
    text: str, profile: ReplyProfile = LINE_PROFILE
) -> list[str]:
    """LINE の UTF-16 code unit 上限内に収まるよう本文を分割する。"""
    chunks: list[str] = []
    for initial in split_message(text, hard_limit=profile.hard_char_limit):
        remaining = initial
        while utf16_length(remaining) > profile.hard_char_limit:
            index = _utf16_prefix_index(remaining, profile.hard_char_limit)
            window = remaining[:index]
            split_at = _preferred_split_index(window)
            head = remaining[:split_at].rstrip()
            remaining = remaining[split_at:].lstrip()
            if not head:
                head = window
                remaining = remaining[index:].lstrip()
            chunks.append(head)
        if remaining:
            chunks.append(remaining)
    return chunks


def utf16_length(text: str) -> int:
    """LINE の文字数基準に合わせた UTF-16 code unit 数を返す。"""
    return len(text.encode("utf-16-le")) // 2


def _utf16_prefix_index(text: str, limit: int) -> int:
    """UTF-16 code unit が limit 以下となる最長 prefix の Python index。"""
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if utf16_length(text[:mid]) <= limit:
            low = mid
        else:
            high = mid - 1
    return low


def _preferred_split_index(window: str) -> int:
    """空行、改行、句点、強制カットの順で分割位置を選ぶ。"""
    index = window.rfind("\n\n")
    if index > 0:
        return index
    index = window.rfind("\n")
    if index > 0:
        return index
    index = window.rfind("。")
    if index >= 0:
        return index + 1
    return len(window)


def partition_reply_and_push(
    text: str,
    *,
    profile: ReplyProfile = LINE_PROFILE,
    reply_limit: int = LINE_REPLY_MAX_MESSAGES,
) -> tuple[list[str], list[str]]:
    """本回答を reply 用（最大 5 件）と push 続き用に分ける。"""
    chunks = split_line_message(text, profile)
    return chunks[:reply_limit], chunks[reply_limit:]


def message_batches(
    messages: Sequence[str], batch_size: int = LINE_REPLY_MAX_MESSAGES
) -> list[list[str]]:
    """LINE API の 1 リクエスト上限に合わせてメッセージ列を分割する。"""
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    return [
        list(messages[index:index + batch_size])
        for index in range(0, len(messages), batch_size)
    ]


class EventDeduplicator:
    """webhook event ID を保持する小さなプロセス内 LRU set。"""

    def __init__(self, max_size: int = 4096):
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        self.max_size = max_size
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lock = Lock()

    def accept(self, event_id: Optional[str]) -> bool:
        """未処理 ID なら記録して True、既処理なら False。ID 無しは常に True。"""
        if not event_id:
            return True
        with self._lock:
            if event_id in self._seen:
                self._seen.move_to_end(event_id)
                return False
            self._seen[event_id] = None
            while len(self._seen) > self.max_size:
                self._seen.popitem(last=False)
            return True
