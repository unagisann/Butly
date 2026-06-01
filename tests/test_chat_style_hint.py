"""
test_chat_style_hint.py
-----------------------
ChatService の reply profile style_hint フックの単体テスト。

検証:
  - request.metadata["style_hint"] がある場合、full_prompt に注入される。
  - request.text 自体は汚染されない（= 記憶本文は不変）。
  - metadata=None（Web 経路）では従来どおり full_prompt に hint が入らない。

_prepare_chat_context を Gatekeeper 無効・Provider/ModelRef をモックして直接呼ぶ。
"""

import asyncio
from unittest.mock import MagicMock, patch

from butly_core.chat import service as svc
from butly_core.chat.types import ChatRequest


def _components():
    memory = MagicMock()
    memory.load_recent_sessions.return_value = ([], None)
    memory.get_last_interaction_time.return_value = None
    brain = MagicMock()
    chronos = MagicMock()
    chronos.get_system_note.return_value = "SYSNOTE"
    return {"memory": memory, "brain": brain, "chronos": chronos}


async def _prepare(metadata, tmp_path):
    instances_dir = tmp_path / "butly_core" / "instances"
    (instances_dir / "Butly").mkdir(parents=True)

    comps = _components()
    fake_ref = MagicMock()
    fake_ref.model_name = "gemini-x"
    fake_ref.connection_id = "google"
    fake_provider = MagicMock()
    fake_provider.supports_vision.return_value = True

    instance_manager = MagicMock()
    instance_manager.get_instance_config.return_value = {
        "gatekeeper": {"enabled": False}
    }
    mem_block_builder = MagicMock()
    mem_block_builder.build.return_value = {}

    req = ChatRequest(
        text="今日の予定は？",
        instance_name="Butly",
        source="discord",
        metadata=metadata,
    )

    with patch.object(svc, "_resolve_chat_model_ref", return_value=fake_ref), patch.object(
        svc.ProviderFactory, "create", return_value=fake_provider
    ):
        prepared = await svc._prepare_chat_context(
            request=req,
            get_instance_components=lambda name: comps,
            instance_manager=instance_manager,
            instances_dir=instances_dir,
            gatekeeper=MagicMock(),
            mem_block_builder=mem_block_builder,
            ai_config_chat={"connection": "google", "model_name": "gemini-3.5-flash"},
        )
    return prepared, req


class TestStyleHintInjection:
    def test_style_hint_injected_into_prompt(self, tmp_path):
        prepared, req = asyncio.run(
            _prepare({"style_hint": "簡潔に返してください。"}, tmp_path)
        )
        assert prepared.error is None
        assert "[応答スタイル指示: 簡潔に返してください。]" in prepared.full_prompt
        assert "今日の予定は？" in prepared.full_prompt

    def test_request_text_not_polluted(self, tmp_path):
        prepared, req = asyncio.run(
            _prepare({"style_hint": "簡潔に返してください。"}, tmp_path)
        )
        # 記憶保存に使う request.text には hint が混ざらない
        assert req.text == "今日の予定は？"
        assert "応答スタイル指示" not in req.text

    def test_no_metadata_no_hint(self, tmp_path):
        prepared, req = asyncio.run(_prepare(None, tmp_path))
        assert prepared.full_prompt == "SYSNOTE\n\n今日の予定は？"
        assert "応答スタイル指示" not in prepared.full_prompt

    def test_metadata_without_style_hint_no_injection(self, tmp_path):
        prepared, req = asyncio.run(
            _prepare({"reply_profile": "discord"}, tmp_path)
        )
        assert "応答スタイル指示" not in prepared.full_prompt
