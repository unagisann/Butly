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
        "goals": [],
        "unresolved": [],
        "turn_count": 0,
        "last_tier": "mid",
    }

    def __init__(self, instance_dir: Path):
        self.state_file = instance_dir / "session_state.json"
        self.state = self._load()

    def _load(self) -> dict:
        """ファイルから状態を読み込む。なければデフォルトを返す。"""
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # 欠損キーをデフォルトで補完
                    for k, v in self.DEFAULT_STATE.items():
                        if k not in data:
                            data[k] = copy.deepcopy(v)
                    return data
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

        # topic: nullでなければ上書き
        if delta.get("topic") is not None:
            self.state["topic"] = delta["topic"]

        # mood: nullでなければ上書き
        if delta.get("mood") is not None:
            self.state["mood"] = delta["mood"]

        # add_goal: nullでなければgoalsに追加（重複チェック）
        add_goal = delta.get("add_goal")
        if add_goal and add_goal not in self.state["goals"]:
            self.state["goals"].append(add_goal)

        # add_unresolved: nullでなければunresolvedに追加（重複チェック）
        add_unresolved = delta.get("add_unresolved")
        if add_unresolved and add_unresolved not in self.state["unresolved"]:
            self.state["unresolved"].append(add_unresolved)

        # resolve: nullでなければunresolvedから部分一致で除去
        resolve = delta.get("resolve")
        if resolve:
            self.state["unresolved"] = [
                item for item in self.state["unresolved"]
                if resolve.lower() not in item.lower()
            ]

        # 上限管理
        if len(self.state["goals"]) > 5:
            self.state["goals"] = self.state["goals"][-5:]
        if len(self.state["unresolved"]) > 8:
            self.state["unresolved"] = self.state["unresolved"][-8:]

        self._save()

    def increment_turn(self, tier: str):
        self.state["turn_count"] += 1
        self.state["last_tier"] = tier
        self._save()

    def to_prompt_text(self) -> str:
        """Gatekeeperのプロンプトに注入するテキスト形式に変換する。"""
        lines = [
            f"Topic: {self.state['topic'] or '(未設定)'}",
            f"Mood: {self.state['mood']}",
            f"Goals: {', '.join(self.state['goals']) if self.state['goals'] else '(なし)'}",
            f"Unresolved: {', '.join(self.state['unresolved']) if self.state['unresolved'] else '(なし)'}",
            f"Turn: {self.state['turn_count']}",
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return self.state.copy()

    def reset(self):
        """セッションリセット。"""
        self.state = copy.deepcopy(self.DEFAULT_STATE)
        self._save()
