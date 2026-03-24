"""
state_updater.py
----------------
ユーザー発言からsession_stateの差分（state_delta）を生成する。
"""

import json
import re
import time
from pathlib import Path

from butly_core.config import AI_CONFIG, SYSTEM_CONFIG
from butly_core.prompts import PromptLoader


class StateUpdater:
    """ユーザー発言から session_state の差分を生成する。"""

    def __init__(self, base_dir: Path = None):
        self.base_dir = base_dir or Path(__file__).resolve().parent.parent.parent.parent
        self._prompt_loader = PromptLoader(
            locale=SYSTEM_CONFIG.get("agent", {}).get("locale", "ja")
        )
        try:
            self.model_name = AI_CONFIG["gatekeeper"]["model_name"]
            self.gatekeeper_config = AI_CONFIG["gatekeeper"]
        except KeyError:
            self.model_name = "gemini-3.1-flash-lite-preview"
            self.gatekeeper_config = {
                "model_name": "gemini-3.1-flash-lite-preview",
                "generation_config": {"temperature": 0.0, "max_output_tokens": 512},
                "safety_settings": [],
            }

    def _get_provider(self):
        from butly_core.llm.factory import ProviderFactory
        return ProviderFactory.create(self.model_name)

    def _resolve_config(self, override_config=None):
        """override_config から gatekeeper 設定を解決する。"""
        if not override_config or "gatekeeper" not in override_config:
            return self.model_name, self.gatekeeper_config
        merged = {**self.gatekeeper_config, **override_config["gatekeeper"]}
        if "generation_config" in self.gatekeeper_config and "generation_config" in override_config.get("gatekeeper", {}):
            merged["generation_config"] = {**self.gatekeeper_config["generation_config"], **override_config["gatekeeper"]["generation_config"]}
        model = merged.get("model_name", self.model_name)
        return model, merged

    def update(self, user_input: str, history_msgs: list,
               current_state: dict, override_config=None) -> dict:
        """
        Returns:
            {
                "topic": str | None,
                "mood": str | None,
                "add_goal": str | None,
                "add_unresolved": str | None,
                "resolve": str | None
            }
        """
        if not self.gatekeeper_config:
            return self._default_output()

        model_name, gk_config = self._resolve_config(override_config)

        t0 = time.time()

        history_text = self._format_history(history_msgs, max_turns=3)

        # Format session state text
        state_text = self._format_state(current_state)

        prompt = self._prompt_loader.get(
            "state_updater",
            agent_name=SYSTEM_CONFIG["agent"]["agent_name"],
            session_state=state_text,
            history_text=history_text,
            user_input=user_input,
        )

        try:
            from butly_core.llm.factory import ProviderFactory
            provider = ProviderFactory.create(model_name)
            raw_text = provider.classify(prompt, gk_config)
            result = self._parse_response(raw_text)
        except Exception as e:
            print(f"[StateUpdater] API呼び出しエラー: {e}")
            result = self._default_output()

        t1 = time.time()
        print(f"[StateUpdater] ({int((t1-t0)*1000)}ms)")
        return result

    def _parse_response(self, raw_text: str) -> dict:
        """LLM応答からJSONを抽出しパースする。"""
        default = self._default_output()
        if not raw_text:
            return default

        try:
            match = re.search(r"```json(.*?)```", raw_text, re.DOTALL)
            if match:
                json_str = match.group(1).strip()
            else:
                json_str = raw_text.strip()
                if json_str.startswith("{") and "}" in json_str:
                    json_str = json_str[json_str.find("{"):json_str.rfind("}") + 1]

            data = json.loads(json_str)

            # Validate and normalize
            result = {}
            for key in ("topic", "mood", "add_goal", "add_unresolved", "resolve"):
                val = data.get(key)
                result[key] = val if val else None

            return result

        except Exception as e:
            print(f"[StateUpdater] JSONパースエラー: {e}\nRaw: '{raw_text}'")
            return default

    def _default_output(self) -> dict:
        return {
            "topic": None,
            "mood": None,
            "add_goal": None,
            "add_unresolved": None,
            "resolve": None,
        }

    def _format_state(self, state: dict) -> str:
        """セッション状態をプロンプト用テキストに変換する。"""
        if not isinstance(state, dict):
            return "(未設定)"
        lines = [
            f"Topic: {state.get('topic', '') or '(未設定)'}",
            f"Mood: {state.get('mood', 'neutral')}",
            f"Goals: {', '.join(state.get('goals', [])) if state.get('goals') else '(なし)'}",
            f"Unresolved: {', '.join(state.get('unresolved', [])) if state.get('unresolved') else '(なし)'}",
            f"Turn: {state.get('turn_count', 0)}",
        ]
        return "\n".join(lines)

    def _format_history(self, history_msgs: list, max_turns: int = 3) -> str:
        """history_msgs の末尾 max_turns 件を文字列へ変換する。"""
        if not history_msgs:
            return "（履歴なし）"

        recent = history_msgs[-(max_turns * 2):]
        lines = []
        for msg in recent:
            role = msg.get("role", "unknown")
            parts = msg.get("parts", [""])
            text = parts[0] if parts else ""
            if isinstance(text, str) and len(text) > 80:
                text = text[:80] + "…"
            label = "ユーザー" if role == "user" else SYSTEM_CONFIG["agent"]["agent_name"]
            lines.append(f"{label}: {text}")

        return "\n".join(lines) if lines else "（履歴なし）"
