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
        assert ss.state["turn_count"] == 0
        assert ss.state["last_tier"] == "mid"

    def test_load_existing_state(self, test_instance_dir: Path):
        """既存の状態ファイルからロードできる"""
        state_file = test_instance_dir / "session_state.json"
        state_file.write_text(
            json.dumps({
                "topic": "テスト話題",
                "mood": "playful",
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

    def test_legacy_format_with_goals_unresolved(self, test_instance_dir: Path):
        """旧フォーマット JSON（goals/unresolved あり）読み込みで正常動作（後方互換）"""
        state_file = test_instance_dir / "session_state.json"
        state_file.write_text(
            json.dumps({
                "topic": "旧フォーマット",
                "mood": "focused",
                "goals": ["ゴールA", "ゴールB"],
                "unresolved": ["課題X"],
                "turn_count": 5,
                "last_tier": "mid",
            }),
            encoding="utf-8",
        )

        ss = SessionState(test_instance_dir)

        assert ss.state["topic"] == "旧フォーマット"
        assert ss.state["mood"] == "focused"
        assert ss.state["turn_count"] == 5
        # 旧フィールドは無視される
        assert "goals" not in ss.state
        assert "unresolved" not in ss.state


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

    def test_topic_update_sets_topic_set_at_turn(self, test_instance_dir: Path):
        """topic 更新で topic_set_at_turn が更新される"""
        ss = SessionState(test_instance_dir)
        ss.reset()
        ss.increment_turn("mid")  # turn_count = 1
        ss.increment_turn("mid")  # turn_count = 2
        ss.increment_turn("mid")  # turn_count = 3

        ss.apply_delta({"topic": "新トピック"})

        assert ss.state["topic_set_at_turn"] == 3

    def test_topic_set_at_turn_updates_on_subsequent_change(self, test_instance_dir: Path):
        """topic を再度変更すると topic_set_at_turn も更新される"""
        ss = SessionState(test_instance_dir)
        ss.reset()
        ss.apply_delta({"topic": "最初のトピック"})
        assert ss.state["topic_set_at_turn"] == 0

        for _ in range(5):
            ss.increment_turn("mid")

        ss.apply_delta({"topic": "次のトピック"})
        assert ss.state["topic_set_at_turn"] == 5

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

    def test_topic_reset_after_10_turns_no_reference(self, test_instance_dir: Path):
        """10ターン経過後に直近3ターンでtopicへの言及がなければtopicが空にリセットされる"""
        ss = SessionState(test_instance_dir)
        ss.reset()
        ss.apply_delta({"topic": "古いトピック"})

        # 10ターン進める（topicへの言及なし）
        for _ in range(10):
            ss.increment_turn("mid", history_msgs=[])

        assert ss.state["topic"] == ""

    def test_topic_preserved_after_10_turns_with_reference(self, test_instance_dir: Path):
        """10ターン経過後でも直近3ターンでtopicへの言及があればtopicが保持される"""
        ss = SessionState(test_instance_dir)
        ss.reset()
        ss.apply_delta({"topic": "古いトピック"})

        # 9ターン進める
        for _ in range(9):
            ss.increment_turn("mid", history_msgs=[])

        # 10ターン目で「古いトピック」に言及する履歴を渡す
        history_with_reference = [
            {"role": "user", "parts": ["古いトピックの件だけど"]},
            {"role": "model", "parts": ["はい、古いトピックについてですね"]},
        ]
        ss.increment_turn("mid", history_msgs=history_with_reference)

        assert ss.state["topic"] == "古いトピック"

    def test_topic_not_reset_before_10_turns(self, test_instance_dir: Path):
        """10ターン未満ではtopicはリセットされない"""
        ss = SessionState(test_instance_dir)
        ss.reset()
        ss.apply_delta({"topic": "まだ新しいトピック"})

        for _ in range(9):
            ss.increment_turn("mid", history_msgs=[])

        assert ss.state["topic"] == "まだ新しいトピック"


class TestSessionStatePersistence:
    """ファイル永続化のテスト"""

    def test_state_persists_after_delta(self, test_instance_dir: Path):
        """delta 適用後に状態がファイルに保存される"""
        ss = SessionState(test_instance_dir)
        ss.reset()
        ss.apply_delta({"topic": "永続化テスト", "mood": "focused"})

        # 新しいインスタンスで読み直し
        ss2 = SessionState(test_instance_dir)

        assert ss2.state["topic"] == "永続化テスト"
        assert ss2.state["mood"] == "focused"

    def test_reset_clears_all(self, test_instance_dir: Path):
        """reset で全状態がクリアされる"""
        state_file = test_instance_dir / "session_state.json"
        state_file.write_text("{}", encoding="utf-8")

        ss = SessionState(test_instance_dir)
        ss.apply_delta({"topic": "消える話題"})
        assert ss.state["topic"] == "消える話題"

        ss.reset()

        assert ss.state["topic"] == ""
        assert ss.state["turn_count"] == 0

    def test_to_dict_excludes_internal_fields(self, test_instance_dir: Path):
        """to_dict() に topic_set_at_turn が含まれない"""
        ss = SessionState(test_instance_dir)
        ss.reset()
        ss.apply_delta({"topic": "テスト"})

        d = ss.to_dict()

        assert "topic_set_at_turn" not in d
        assert "topic" in d
        assert "mood" in d
        assert "turn_count" in d


class TestSessionStatePromptText:
    """to_prompt_text のテスト"""

    def test_prompt_text_format(self, test_instance_dir: Path):
        """プロンプト用テキストが正しい形式で生成される"""
        ss = SessionState(test_instance_dir)
        ss.reset()
        ss.apply_delta({
            "topic": "テスト",
            "mood": "focused",
        })

        text = ss.to_prompt_text()

        assert "Topic: テスト" in text
        assert "Mood: focused" in text
        assert "Turn:" in text
        # Goals/Unresolved は廃止済み
        assert "Goals" not in text
        assert "Unresolved" not in text
