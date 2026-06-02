"""
test_session_digest_naming.py
-----------------------------
回帰テスト: セッション要約 (ButlyMemory.maintain_memory) が、要約モデルへ渡す
会話テキストで役割を **実名** ([ai_name] / [preferred_call]) でラベリングすること。

背景: 以前は ``[model]`` / ``[user]`` という匿名ラベルで渡していたため、summary
モデルが物語化の際にアシスタント名を捏造し ("ルナ" 等)、session_digest を汚染して
いた。本テストはその再発を防ぐ。
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from butly_core.core.memory import ButlyMemory


def _make_instance(base_dir: Path, name: str, config: dict) -> Path:
    inst = base_dir / "butly_core" / "instances" / name
    (inst / "short_term_json").mkdir(parents=True, exist_ok=True)
    (inst / "config.json").write_text(
        json.dumps(config, ensure_ascii=False), encoding="utf-8"
    )
    return inst


def _write_session(inst: Path, fname: str, messages: list):
    (inst / "short_term_json" / fname).write_text(
        json.dumps({"timestamp": "2026-06-03T00:00:00", "messages": messages},
                   ensure_ascii=False),
        encoding="utf-8",
    )


@pytest.fixture
def captured_conv():
    """summarize_conversation に渡される conv_text を捕捉する mock brain。"""
    brain = MagicMock()
    seen = []

    def _summarize(conv_text, override_config=None):
        seen.append(conv_text)
        return "ダミー要約"

    brain.summarize_conversation.side_effect = _summarize
    brain._captured = seen
    return brain


def test_session_summary_uses_real_names(tmp_path, captured_conv):
    inst = _make_instance(
        tmp_path,
        "named_inst",
        {
            "agent_profile": {"ai_name": "ジャービス"},
            "user_profile": {"preferred_call": "主人", "user_name": "悠希"},
            "memory": {"short_term_limit": 1},
        },
    )
    # 2 セッション作成 → limit=1 なので 1 つが圧縮（要約）対象になる
    _write_session(inst, "session_20260603_000001.json",
                   [{"role": "user", "parts": ["古い発話"]},
                    {"role": "model", "parts": ["古い応答"]}])
    _write_session(inst, "session_20260603_000002.json",
                   [{"role": "user", "parts": ["新しい発話"]},
                    {"role": "model", "parts": ["新しい応答"]}])

    mem = ButlyMemory(tmp_path, instance_name="named_inst")
    mem.maintain_memory(captured_conv)

    assert captured_conv._captured, "summarize_conversation が呼ばれていない"
    conv = captured_conv._captured[0]
    # 実名ラベルが使われ、匿名ラベルが消えていること
    assert "[ジャービス]:" in conv
    assert "[主人]:" in conv
    assert "[model]:" not in conv
    assert "[user]:" not in conv


def test_falls_back_to_role_when_no_profile(tmp_path, captured_conv):
    """ai_name / preferred_call が空でも捏造せず、設定のフォールバック名を使う。"""
    inst = _make_instance(
        tmp_path,
        "bare_inst",
        {"memory": {"short_term_limit": 1}},
    )
    _write_session(inst, "session_20260603_000001.json",
                   [{"role": "user", "parts": ["a"]},
                    {"role": "model", "parts": ["b"]}])
    _write_session(inst, "session_20260603_000002.json",
                   [{"role": "user", "parts": ["c"]},
                    {"role": "model", "parts": ["d"]}])

    mem = ButlyMemory(tmp_path, instance_name="bare_inst")
    mem.maintain_memory(captured_conv)

    conv = captured_conv._captured[0]
    # 匿名の [model] / [user] は使われない（フォールバック名に置換される）
    assert "[model]:" not in conv
    assert "[user]:" not in conv
