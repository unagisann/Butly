"""
state_updater.py
----------------
ユーザー発言からsession_stateの差分（state_delta）を生成する。
"""

import json
import time
from pathlib import Path

from butly_core.config import AI_CONFIG, SYSTEM_CONFIG
from butly_core.prompts import PromptLoader
from butly_core.core.json_extract import extract_json_str
from butly_core.trace.collector import record_llm_call


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
            self.model_name = "gemini-3.1-flash-lite"
            self.gatekeeper_config = {
                "model_name": "gemini-3.1-flash-lite",
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
        if (
            "generation_config" in self.gatekeeper_config
            and "generation_config" in override_config.get("gatekeeper", {})
        ):
            merged["generation_config"] = {
                **self.gatekeeper_config["generation_config"],
                **override_config["gatekeeper"]["generation_config"],
            }
        model = merged.get("model_name", self.model_name)
        return model, merged

    def update(
        self,
        user_input: str,
        history_msgs: list,
        current_state: dict,
        override_config=None,
        agent_name: str = None,
    ) -> dict:
        """
        Returns:
            {
                "topic": str | None,
                "mood": str | None,
            }
        """
        if not self.gatekeeper_config:
            return self._default_output()

        model_name, gk_config = self._resolve_config(override_config)

        t0 = time.time()

        from butly_core.prompts import resolve_prompt_locale

        locale = resolve_prompt_locale(override_config)
        _agent_name = agent_name or SYSTEM_CONFIG["agent"]["agent_name"]
        history_text = self._format_history(
            history_msgs,
            max_turns=3,
            agent_name=_agent_name,
            locale=locale,
        )

        # Format session state text
        state_text = self._format_state(current_state)
        prompt = self._prompt_loader.get(
            "state_updater",
            agent_name=_agent_name,
            session_state=state_text,
            history_text=history_text,
            user_input=user_input,
        )

        try:
            from butly_core.llm.factory import ProviderFactory

            # Phase 2: dict (connection + model_name) を Factory に渡す
            provider = ProviderFactory.create(
                gk_config if gk_config.get("model_name") else model_name
            )
            raw_text = provider.classify(prompt, gk_config)
            from butly_core.trace.collector import usage_metadata

            record_llm_call(
                purpose="state_updater",
                model=model_name,
                connection_id=gk_config.get("connection", ""),
                duration_ms=int((time.time() - t0) * 1000),
                prompt_chars=len(prompt),
                metadata=usage_metadata(provider),
            )
            result = self._parse_response(raw_text)
        except Exception as e:
            print(f"[StateUpdater] API呼び出しエラー: {e}")
            record_llm_call(
                purpose="state_updater",
                model=model_name,
                connection_id=gk_config.get("connection", ""),
                duration_ms=int((time.time() - t0) * 1000),
                prompt_chars=len(prompt),
                error=str(e),
            )
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
            data = json.loads(extract_json_str(raw_text))

            # Validate and normalize — topic, mood のみ抽出
            result = {}
            for key in ("topic", "mood"):
                val = data.get(key)
                # LLM が "None" / "null" を文字列として出力するケースを正規化
                if val in (None, "None", "null", ""):
                    val = None
                result[key] = val

            return result

        except Exception as e:
            print(f"[StateUpdater] JSONパースエラー: {e}\nRaw: '{raw_text}'")
            return default

    def _default_output(self) -> dict:
        return {"topic": None, "mood": None}

    def _format_state(self, state: dict) -> str:
        """セッション状態をプロンプト用テキストに変換する。"""
        if not isinstance(state, dict):
            return "(none)"
        lines = [
            f"Topic: {state.get('topic', '') or '(none)'}",
            f"Mood: {state.get('mood', 'neutral')}",
            f"Turn: {state.get('turn_count', 0)}",
        ]
        return "\n".join(lines)

    def _format_history(
        self,
        history_msgs: list,
        max_turns: int = 3,
        agent_name: str = None,
        locale: str = "ja",
    ) -> str:
        """history_msgs の末尾 max_turns 件を文字列へ変換する。"""
        if not history_msgs:
            return "(no history)" if locale != "ja" else "（履歴なし）"

        _agent_name = agent_name or SYSTEM_CONFIG["agent"]["agent_name"]
        recent = history_msgs[-(max_turns * 2) :]
        lines = []
        for msg in recent:
            role = msg.get("role", "unknown")
            parts = msg.get("parts", [""])
            text = parts[0] if parts else ""
            if isinstance(text, str) and len(text) > 80:
                text = text[:80] + "…"
            user_label = "User" if locale != "ja" else "ユーザー"
            label = user_label if role == "user" else _agent_name
            lines.append(f"{label}: {text}")

        if lines:
            return "\n".join(lines)
        return "(no history)" if locale != "ja" else "（履歴なし）"
