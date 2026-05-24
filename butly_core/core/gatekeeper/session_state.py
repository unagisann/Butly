"""
session_state.py
----------------
セッション全体の内部状態を管理する。
JSONファイルベースで永続化。
"""

import copy
import json
from pathlib import Path


class SessionState:
    """
    セッション全体の内部状態を管理する。
    JSONファイルベースで永続化。
    """

    DEFAULT_STATE = {
        "topic": "",
        "mood": "neutral",
        "turn_count": 0,
        "last_tier": "mid",
        "topic_set_at_turn": 0,  # 内部管理用。プロンプト/外部には出さない
    }

    def __init__(self, instance_dir: Path):
        self.state_file = instance_dir / "session_state.json"
        self.state = self._load()

    def _load(self) -> dict:
        """ファイルから状態を読み込む。なければデフォルトを返す。
        旧フォーマット（goals/unresolved含む）のJSONも安全に読み込める。
        DEFAULT_STATEのキーのみ採用するため、旧フィールドは自動的に無視される。
        """
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    result = copy.deepcopy(self.DEFAULT_STATE)
                    for k in self.DEFAULT_STATE:
                        if k in data:
                            result[k] = data[k]
                    return result
            except Exception as e:
                print(f"[SessionState] 状態ファイルの読み込みエラー: {e}")
        return copy.deepcopy(self.DEFAULT_STATE)

    def _save(self):
        """現在の状態をファイルに書き出す。"""
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[SessionState] 状態ファイルの保存エラー: {e}")

    def apply_delta(self, delta: dict):
        """
        Gatekeeperのstate_deltaを適用する。
        """
        if not delta:
            return

        if delta.get("topic") is not None:
            self.state["topic"] = delta["topic"]
            self.state["topic_set_at_turn"] = self.state["turn_count"]

        if delta.get("mood") is not None:
            self.state["mood"] = delta["mood"]

        self._save()

    def _has_recent_topic_reference(self, history_msgs: list) -> bool:
        """直近3ターン内で現在の topic への言及があるかチェックする。"""
        topic = self.state.get("topic", "")
        if not topic:
            return False
        recent = history_msgs[-(3 * 2) :] if history_msgs else []
        for msg in recent:
            parts = msg.get("parts", [""])
            text = parts[0] if parts else ""
            if isinstance(text, str) and topic.lower() in text.lower():
                return True
        return False

    def increment_turn(self, tier: str, history_msgs: list = None):
        self.state["turn_count"] += 1
        self.state["last_tier"] = tier
        # topic 寿命チェック: 10ターン経過 かつ 直近3ターン内でtopicへの言及がない場合のみリセット
        turns_since_topic = self.state["turn_count"] - self.state.get(
            "topic_set_at_turn", 0
        )
        if self.state["topic"] and turns_since_topic >= 10:
            if not self._has_recent_topic_reference(history_msgs or []):
                self.state["topic"] = ""
        self._save()

    def to_prompt_text(self) -> str:
        """Gatekeeperのプロンプトに注入するテキスト形式に変換する。"""
        lines = [
            f"Topic: {self.state['topic'] or '(none)'}",
            f"Mood: {self.state['mood']}",
            f"Turn: {self.state['turn_count']}",
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """外部向け辞書。内部管理フィールドは除外する。"""
        return {k: v for k, v in self.state.items() if k != "topic_set_at_turn"}

    def reset(self):
        """セッションリセット。"""
        self.state = copy.deepcopy(self.DEFAULT_STATE)
        self._save()
