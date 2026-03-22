"""
test_session_state.py
---------------------
SessionState のユニットテスト。
API キー不要 — ファイルI/Oのロジックのみ検証。
"""

import json
from pathlib import Path

import pytest

from butly_core.core.gatekeeper import SessionState


class TestSessionStateInit:
    """初期化と永続化のテスト"""

    def test_default_state_when_no_file(self, test_instance_dir: Path):
        """状態ファイルがない場合にデフォルト値が設定される"""
        state_file = test_instance_dir / "session_state.json"
        state_file.unlink(missing_ok=True)

        ss = SessionState(test_instance_dir)

        assert ss.state["topic"] == ""
        assert ss.state["mood"] == "neutral"
        assert ss.state["goals"] == []
        assert ss.state["unresolved"] == []
        assert ss.state["turn_count"] == 0
        assert ss.state["last_tier"] == "mid"

    def test_load_existing_state(self, test_instance_dir: Path):
        """既存の状態ファイルからロードできる"""
        state_file = test_instance_dir / "session_state.json"
        state_file.write_text(
            json.dumps({
                "topic": "テスト話題",
                "mood": "playful",
                "goals": ["ゴールA"],
                "unresolved": ["課題X"],
                "turn_count": 10,
                "last_tier": "cortex",
            }),
            encoding="utf-8",
        )

        ss = SessionState(test_instance_dir)

        assert ss.state["topic"] == "テスト話題"
        assert ss.state["mood"] == "playful"
        assert ss.state["turn_count"] == 10

    def test_missing_keys_filled_with_defaults(self, test_instance_dir: Path):
        """ファイルに欠損キーがある場合、デフォルトで補完される"""
        state_file = test_instance_dir / "session_state.json"
        state_file.write_text(
            json.dumps({"topic": "部分的なデータ"}),
            encoding="utf-8",
        )

        ss = SessionState(test_instance_dir)

        assert ss.state["topic"] == "部分的なデータ"
        assert ss.state["mood"] == "neutral"  # デフォルト補完
        assert ss.state["goals"] == []


class TestSessionStateDelta:
    """apply_delta のテスト"""

    def test_topic_update(self, test_instance_dir: Path):
        """topic が更新される"""
        ss = SessionState(test_instance_dir)
        ss.reset()

        ss.apply_delta({"topic": "新しい話題"})

        assert ss.state["topic"] == "新しい話題"

    def test_topic_null_preserves_current(self, test_instance_dir: Path):
        """topic が null の場合は現在の値を保持"""
        ss = SessionState(test_instance_dir)
        ss.reset()
        ss.apply_delta({"topic": "元の話題"})

        ss.apply_delta({"topic": None})

        assert ss.state["topic"] == "元の話題"

    def test_add_goal(self, test_instance_dir: Path):
        """goals に新しいゴールが追加される"""
        ss = SessionState(test_instance_dir)
        ss.reset()

        ss.apply_delta({"add_goal": "テストを書く"})

        assert "テストを書く" in ss.state["goals"]

    def test_duplicate_goal_not_added(self, test_instance_dir: Path):
        """重複するゴールは追加されない"""
        ss = SessionState(test_instance_dir)
        ss.reset()

        ss.apply_delta({"add_goal": "テストを書く"})
        ss.apply_delta({"add_goal": "テストを書く"})

        assert ss.state["goals"].count("テストを書く") == 1

    def test_add_unresolved(self, test_instance_dir: Path):
        """unresolved に課題が追加される"""
        ss = SessionState(test_instance_dir)
        ss.reset()

        ss.apply_delta({"add_unresolved": "バグ修正"})

        assert "バグ修正" in ss.state["unresolved"]

    def test_resolve_removes_matching(self, test_instance_dir: Path):
        """resolve で部分一致する unresolved が除去される"""
        ss = SessionState(test_instance_dir)
        ss.reset()
        ss.apply_delta({"add_unresolved": "プロンプト最適化"})
        ss.apply_delta({"add_unresolved": "DB設計"})

        ss.apply_delta({"resolve": "プロンプト"})

        assert "プロンプト最適化" not in ss.state["unresolved"]
        assert "DB設計" in ss.state["unresolved"]

    def test_goals_max_limit(self, test_instance_dir: Path):
        """goals が上限（5件）を超えた場合、古いものが切り捨てられる"""
        ss = SessionState(test_instance_dir)
        ss.reset()

        for i in range(8):
            ss.apply_delta({"add_goal": f"ゴール{i}"})

        assert len(ss.state["goals"]) == 5
        # 最後の5件が残る
        assert ss.state["goals"][-1] == "ゴール7"

    def test_unresolved_max_limit(self, test_instance_dir: Path):
        """unresolved が上限（8件）を超えた場合、古いものが切り捨てられる"""
        ss = SessionState(test_instance_dir)
        ss.reset()

        for i in range(12):
            ss.apply_delta({"add_unresolved": f"課題{i}"})

        assert len(ss.state["unresolved"]) == 8

    def test_mood_update(self, test_instance_dir: Path):
        """mood が更新される"""
        ss = SessionState(test_instance_dir)
        ss.reset()

        ss.apply_delta({"mood": "excited"})

        assert ss.state["mood"] == "excited"

    def test_empty_delta_no_change(self, test_instance_dir: Path):
        """空の delta では状態が変わらない"""
        ss = SessionState(test_instance_dir)
        ss.reset()
        original = ss.state.copy()

        ss.apply_delta({})

        assert ss.state["topic"] == original["topic"]
        assert ss.state["mood"] == original["mood"]

    def test_none_delta_no_crash(self, test_instance_dir: Path):
        """None の delta でもクラッシュしない"""
        ss = SessionState(test_instance_dir)
        ss.reset()

        ss.apply_delta(None)  # type: ignore — 防御テスト


class TestSessionStateTurn:
    """ターン管理のテスト"""

    def test_increment_turn(self, test_instance_dir: Path):
        """turn_count がインクリメントされ、last_tier が記録される"""
        ss = SessionState(test_instance_dir)
        ss.reset()

        ss.increment_turn("cortex")

        assert ss.state["turn_count"] == 1
        assert ss.state["last_tier"] == "cortex"

    def test_multiple_increments(self, test_instance_dir: Path):
        """連続的なインクリメント"""
        ss = SessionState(test_instance_dir)
        ss.reset()

        ss.increment_turn("reflex")
        ss.increment_turn("mid")
        ss.increment_turn("cortex")

        assert ss.state["turn_count"] == 3
        assert ss.state["last_tier"] == "cortex"


class TestSessionStatePersistence:
    """ファイル永続化のテスト"""

    def test_state_persists_after_delta(self, test_instance_dir: Path):
        """delta 適用後に状態がファイルに保存される"""
        ss = SessionState(test_instance_dir)
        ss.reset()
        ss.apply_delta({"topic": "永続化テスト", "add_goal": "保存確認"})

        # 新しいインスタンスで読み直し
        ss2 = SessionState(test_instance_dir)

        assert ss2.state["topic"] == "永続化テスト"
        assert "保存確認" in ss2.state["goals"]

    def test_reset_clears_all(self, test_instance_dir: Path):
        """reset で全状態がクリアされる"""
        # 前のテストの残骸をクリアするため、まず空状態を書き込む
        state_file = test_instance_dir / "session_state.json"
        state_file.write_text("{}", encoding="utf-8")

        ss = SessionState(test_instance_dir)
        ss.apply_delta({"topic": "消える話題", "add_goal": "消えるゴール"})
        assert ss.state["topic"] == "消える話題"

        ss.reset()

        assert ss.state["topic"] == ""
        assert ss.state["goals"] == []
        assert ss.state["turn_count"] == 0


class TestSessionStatePromptText:
    """to_prompt_text のテスト"""

    def test_prompt_text_format(self, test_instance_dir: Path):
        """プロンプト用テキストが正しい形式で生成される"""
        ss = SessionState(test_instance_dir)
        ss.reset()
        ss.apply_delta({
            "topic": "テスト",
            "mood": "focused",
            "add_goal": "完了させる",
        })

        text = ss.to_prompt_text()

        assert "Topic: テスト" in text
        assert "Mood: focused" in text
        assert "完了させる" in text
