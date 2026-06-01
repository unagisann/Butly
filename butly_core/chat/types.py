"""
types.py
--------
チャット機能で使う共通 DTO（Pydantic モデル）とバリデーション関数。
LLM プロバイダーに依存しない、アプリ内標準のデータ型。
"""

import base64
import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel

# ===================================================================
# 定数
# ===================================================================

# 許可する MIME タイプ
ALLOWED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp"}

# 最大添付枚数
MAX_ATTACHMENTS = 3

# 1枚あたりの最大サイズ（バイト）
MAX_ATTACHMENT_SIZE = 20 * 1024 * 1024  # 20MB

# inline 送信の閾値（Provider 側で使用）
INLINE_SIZE_PER_FILE = 2 * 1024 * 1024  # 2MB
INLINE_SIZE_TOTAL = 8 * 1024 * 1024  # 8MB

# data URL ヘッダのパターン
_DATA_URL_PATTERN = re.compile(r"^data:[^;]+;base64,")


# ===================================================================
# Pydantic モデル
# ===================================================================


class Attachment(BaseModel):
    """添付ファイル（現在は画像のみ対応）"""

    kind: Literal["image"] = "image"
    mime_type: str
    data_base64: str
    name: Optional[str] = None
    size: Optional[int] = None


class ChatRequest(BaseModel):
    """チャットリクエスト（アプリ内標準 DTO）"""

    text: str = ""
    attachments: List[Attachment] = []
    instance_name: str = "00_master"
    model_name: Optional[str] = None
    # Phase 2: provider/connection を明示するためのオプション。
    # 未指定なら model_name から推定 (旧形式互換)。
    # user 定義 connection (Groq 等) を指定する場合は必須。
    connection: Optional[str] = None
    use_rag: bool = True
    use_google_search: bool = False
    use_web_search: bool = False
    metadata: Optional[Dict[str, Any]] = None

    # 外部チャット入口（Discord / LINE 等）用の任意フィールド。
    # Web UI からは未指定（None）。ChatService の生成・記憶処理には影響しない
    # メタ情報であり、会話記憶に保存する本文（request.text）には混ぜない。
    # debug log / ルーティングのために source 情報を保持する用途で使う。
    source: Literal["web", "discord", "line", "cli", "api"] = "web"
    external_user_id: Optional[str] = None
    external_channel_id: Optional[str] = None


class ChatResponse(BaseModel):
    """チャットレスポンス（アプリ内標準 DTO）"""

    text: str
    keywords: List[str] = []
    refs: List[Dict[str, Any]] = []
    sources: List[Dict[str, Any]] = []
    tier: str = ""
    need: Optional[str] = None
    search_targets: Optional[List[str]] = None
    session_state: Optional[Dict[str, Any]] = None
    gatekeeper_scores: Optional[Dict[str, float]] = None
    debug_info: Optional[Dict[str, Any]] = None


# ===================================================================
# バリデーション関数
# ===================================================================


def strip_data_url_header(data_base64: str) -> str:
    """
    data URL ヘッダ（data:image/jpeg;base64,...）が含まれていた場合に除去する。
    """
    return _DATA_URL_PATTERN.sub("", data_base64)


def validate_attachments(attachments: List[Attachment]) -> Optional[str]:
    """
    添付ファイルリストをバリデーションする。

    Returns:
        None: 問題なし
        str: エラーメッセージ
    """
    if not attachments:
        return None

    # 枚数チェック
    if len(attachments) > MAX_ATTACHMENTS:
        return f"添付は最大{MAX_ATTACHMENTS}枚までです（{len(attachments)}枚受信）"

    for i, att in enumerate(attachments):
        # kind チェック
        if att.kind != "image":
            return f"サポートされていない添付種別です: {att.kind}（許可: image）"

        # MIME チェック
        if att.mime_type not in ALLOWED_IMAGE_MIMES:
            allowed = " / ".join(sorted(ALLOWED_IMAGE_MIMES))
            return (
                f"サポートされていない画像形式です: {att.mime_type}（許可: {allowed}）"
            )

        # data URL ヘッダ除去（副作用あり: 元の att を書き換える）
        att.data_base64 = strip_data_url_header(att.data_base64)

        # base64 デコード検証
        try:
            decoded = base64.b64decode(att.data_base64, validate=True)
        except Exception:
            return f"画像データの解析に失敗しました（添付 {i + 1}）"

        # サイズチェック
        actual_size = len(decoded)
        if actual_size > MAX_ATTACHMENT_SIZE:
            size_mb = actual_size / (1024 * 1024)
            limit_mb = MAX_ATTACHMENT_SIZE / (1024 * 1024)
            return f"画像サイズが上限を超えています（{size_mb:.1f}MB / 上限 {limit_mb:.0f}MB、添付 {i + 1}）"

    return None


def normalize_ws_payload(raw_payload) -> ChatRequest:
    """
    WebSocket の chat_message payload を ChatRequest に正規化する。

    対応形式:
    - 旧形式（文字列）: "こんにちは"
    - 新形式（オブジェクト）: {"text": "...", "attachments": [...]}
    - 旧 images 互換: {"text": "...", "images": ["base64..."]}
    """
    # 旧形式: payload が文字列
    if isinstance(raw_payload, str):
        return ChatRequest(text=raw_payload)

    # 新形式: payload がオブジェクト
    if isinstance(raw_payload, dict):
        text = raw_payload.get("text", "")
        instance_name = raw_payload.get("instance_name", "00_master")
        use_google_search = raw_payload.get("use_google_search", False)
        use_web_search = raw_payload.get("use_web_search", False)
        # Phase 3: per-request モデル切替 (UI/サードパーティ拡張向け)
        model_name = raw_payload.get("model_name") or None
        connection = raw_payload.get("connection") or None

        attachments = []

        # 新形式: attachments
        if "attachments" in raw_payload:
            for att_data in raw_payload["attachments"]:
                attachments.append(Attachment(**att_data))

        # 旧 images 互換: images: string[] → Attachment に変換
        elif "images" in raw_payload:
            for b64 in raw_payload["images"]:
                cleaned = strip_data_url_header(b64)
                attachments.append(
                    Attachment(
                        kind="image",
                        mime_type="image/jpeg",  # 旧形式は MIME 情報がないため JPEG と仮定
                        data_base64=cleaned,
                    )
                )

        return ChatRequest(
            text=text,
            attachments=attachments,
            instance_name=instance_name,
            model_name=model_name,
            connection=connection,
            use_google_search=use_google_search,
            use_web_search=use_web_search,
        )

    # 不正な形式
    raise ValueError(f"不正な payload 形式です: {type(raw_payload)}")
