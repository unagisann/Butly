"""
test_chat_types.py
------------------
ChatRequest / ChatResponse / Attachment の DTO と
バリデーション関数のユニットテスト。
API キー不要。
"""

import base64

import pytest

from butly_core.chat.types import (
    Attachment,
    ChatRequest,
    ChatResponse,
    normalize_ws_payload,
    strip_data_url_header,
    validate_attachments,
    MAX_ATTACHMENTS,
    ALLOWED_IMAGE_MIMES,
)


class TestStripDataUrlHeader:
    """data URL ヘッダ除去のテスト"""

    def test_strips_jpeg_header(self):
        raw = "data:image/jpeg;base64,/9j/4AAQ..."
        result = strip_data_url_header(raw)
        assert result == "/9j/4AAQ..."

    def test_strips_png_header(self):
        raw = "data:image/png;base64,iVBORw0KGgo..."
        result = strip_data_url_header(raw)
        assert result == "iVBORw0KGgo..."

    def test_no_header_unchanged(self):
        raw = "/9j/4AAQ..."
        result = strip_data_url_header(raw)
        assert result == raw


class TestValidateAttachments:
    """添付ファイルバリデーションのテスト"""

    def _make_valid_attachment(self, size_bytes=100) -> Attachment:
        """有効なテスト用 Attachment を生成"""
        data = base64.b64encode(b"\x00" * size_bytes).decode()
        return Attachment(
            kind="image",
            mime_type="image/jpeg",
            data_base64=data,
        )

    def test_empty_list_is_valid(self):
        assert validate_attachments([]) is None

    def test_valid_attachment_passes(self):
        att = self._make_valid_attachment()
        assert validate_attachments([att]) is None

    def test_too_many_attachments(self):
        atts = [self._make_valid_attachment() for _ in range(MAX_ATTACHMENTS + 1)]
        error = validate_attachments(atts)
        assert error is not None
        assert "最大" in error

    def test_invalid_kind(self):
        """不正な kind は Pydantic が ValidationError を発生させる"""
        with pytest.raises(Exception):
            Attachment(
                kind="video",  # type: ignore
                mime_type="image/jpeg",
                data_base64=base64.b64encode(b"\x00").decode(),
            )

    def test_invalid_mime(self):
        att = Attachment(
            kind="image",
            mime_type="image/gif",
            data_base64=base64.b64encode(b"\x00").decode(),
        )
        error = validate_attachments([att])
        assert error is not None
        assert "サポートされていない画像形式" in error

    def test_invalid_base64(self):
        att = Attachment(
            kind="image",
            mime_type="image/jpeg",
            data_base64="not-valid-base64!!!",
        )
        error = validate_attachments([att])
        assert error is not None
        assert "解析に失敗" in error

    def test_data_url_header_stripped(self):
        """data URL ヘッダ付きの base64 が正しく処理される"""
        raw_data = base64.b64encode(b"\x00" * 10).decode()
        att = Attachment(
            kind="image",
            mime_type="image/jpeg",
            data_base64=f"data:image/jpeg;base64,{raw_data}",
        )
        error = validate_attachments([att])
        assert error is None
        # ヘッダが除去されている
        assert not att.data_base64.startswith("data:")


class TestNormalizeWsPayload:
    """WebSocket payload 正規化のテスト"""

    def test_string_payload(self):
        """文字列 payload → ChatRequest"""
        result = normalize_ws_payload("こんにちは")

        assert isinstance(result, ChatRequest)
        assert result.text == "こんにちは"
        assert result.attachments == []

    def test_dict_payload_text_only(self):
        """dict payload（テキストのみ）"""
        result = normalize_ws_payload({
            "text": "テストメッセージ",
            "instance_name": "01_test",
        })

        assert result.text == "テストメッセージ"
        assert result.instance_name == "01_test"

    def test_dict_payload_with_attachments(self):
        """dict payload（添付あり）"""
        b64 = base64.b64encode(b"\x00").decode()
        result = normalize_ws_payload({
            "text": "画像付き",
            "attachments": [{
                "kind": "image",
                "mime_type": "image/jpeg",
                "data_base64": b64,
            }],
        })

        assert result.text == "画像付き"
        assert len(result.attachments) == 1
        assert result.attachments[0].mime_type == "image/jpeg"

    def test_legacy_images_format(self):
        """旧 images 互換形式"""
        b64 = base64.b64encode(b"\x00").decode()
        result = normalize_ws_payload({
            "text": "旧形式",
            "images": [b64],
        })

        assert len(result.attachments) == 1
        assert result.attachments[0].kind == "image"

    def test_invalid_payload_raises(self):
        """不正な形式で ValueError"""
        with pytest.raises(ValueError):
            normalize_ws_payload(12345)


class TestChatRequestDefaults:
    """ChatRequest のデフォルト値テスト"""

    def test_defaults(self):
        req = ChatRequest(text="テスト")

        assert req.instance_name == "00_master"
        assert req.use_rag is True
        assert req.use_google_search is False
        assert req.attachments == []


class TestChatResponseDefaults:
    """ChatResponse のデフォルト値テスト"""

    def test_defaults(self):
        resp = ChatResponse(text="応答テスト")

        assert resp.keywords == []
        assert resp.refs == []
        assert resp.sources == []
        assert resp.tier == ""
