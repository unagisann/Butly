"""Knowledge card の canonical content と content hash（Stage 3 レビューキューの版識別子）。

Stage 3 計画 §5.1/§5.2 の契約:
  - hash 対象は Stage 3 prompt に渡す意味内容のみ:
    title / summary / episode / tags / category / source_date（固定順）。
    type・importance・usage・embedding・pin/archive は hash にも prompt にも含めない。
  - canonical 化: 各フィールドを str 化 → 改行を LF に統一 → 前後空白を strip。
    None は空文字。固定キー順の JSON (UTF-8, separators 固定) を SHA-256 する。
  - カード本文を書く経路（Stage 2 INSERT / update_card / 将来の merge）は必ず
    この helper で content_hash を更新し、hash が変わった時だけ
    maturation_queued_at を更新する。
  - Stage 3 が書く運用時刻は固定長 UTC "YYYY-MM-DDTHH:MM:SSZ"。
    TEXT 辞書順が時系列順になることをキュー FIFO が前提にする。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping

# hash / prompt に含めるフィールド（固定順）。順序変更は全カードの再レビューを誘発する。
CONTENT_HASH_FIELDS: tuple[str, ...] = (
    "title",
    "summary",
    "episode",
    "tags",
    "category",
    "source_date",
)


class CardContentError(ValueError):
    """canonical content / hash を生成できない場合に送出する。

    run preflight はこれを黙って握り潰さず、run を明示的に失敗させる（§5.3）。
    """


def _normalize_field(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        raise CardContentError("card content field must not be bytes")
    text = value if isinstance(value, str) else str(value)
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def canonical_card_content(card: Mapping[str, Any]) -> str:
    """カードの意味内容を固定順 JSON 文字列に正規化する。"""
    try:
        payload = {field: _normalize_field(card.get(field)) for field in CONTENT_HASH_FIELDS}
    except CardContentError:
        raise
    except Exception as exc:
        raise CardContentError(f"cannot canonicalize card content: {exc}") from exc
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def compute_content_hash(card: Mapping[str, Any]) -> str:
    """canonical content の SHA-256 hex を返す。"""
    canonical = canonical_card_content(card)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ----------------------------------------------------------
# Stage 3 運用時刻（固定長 UTC）
# ----------------------------------------------------------

MATURATION_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def format_maturation_time(dt: datetime) -> str:
    """datetime を固定長 UTC "YYYY-MM-DDTHH:MM:SSZ" に正規化する。

    naive datetime は UTC として扱う（Stage 3 の注入 clock は UTC 前提）。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(MATURATION_TIME_FORMAT)


def utc_now_stamp() -> str:
    return format_maturation_time(datetime.now(timezone.utc))


def normalize_maturation_time(value: Any, *, fallback: str) -> str:
    """既存の時刻表現（offset 付き / 小数秒 / "YYYY-MM-DD HH:MM:SS"）を固定長 UTC へ揃える。

    parse 不能なら fallback（呼び出し側の migration 開始時刻など）を返す。
    """
    if isinstance(value, datetime):
        return format_maturation_time(value)
    if not isinstance(value, str) or not value.strip():
        return fallback
    text = value.strip()
    # "YYYY-MM-DD HH:MM:SS" (SQLite CURRENT_TIMESTAMP) と ISO 8601 の両方を受ける
    candidate = text.replace(" ", "T", 1) if "T" not in text else text
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        # 日付のみ (YYYY-MM-DD)
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return fallback
    return format_maturation_time(parsed)
