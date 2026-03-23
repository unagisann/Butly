"""
tier_classifier.py
------------------
LLM にシグナル（bool値）を出力させ、Python 側で tier を再決定する。
"""

import copy
import json
import re
import time
from pathlib import Path

from butly_core.config import AI_CONFIG, SYSTEM_CONFIG
from butly_core.prompts import PromptLoader


class TierClassifier:
    """LLM にシグナルを出力させ、Python 側で tier を最終決定する。"""

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

    def classify(self, user_input: str, history_msgs: list,
                 current_topic: str = "") -> dict:
        """
        Returns:
            {
                "tier": "reflex" | "mid" | "cortex",
                "llm_tier": str,  # LLMが出した元のtier（デバッグ用）
                "llm_reasoning": {
                    "recent_context_sufficient": bool,
                    "needs_user_memory": bool,
                    "needs_relationship_state": bool,
                    "needs_long_term_search": bool
                }
            }
        """
        if not self.gatekeeper_config:
            return self._default_output()

        t0 = time.time()

        history_text = self._format_history(history_msgs, max_turns=3)

        prompt = self._prompt_loader.get(
            "tier_classifier",
            agent_name=SYSTEM_CONFIG["agent"]["agent_name"],
            current_topic=current_topic or "(未設定)",
            history_text=history_text,
            user_input=user_input,
        )

        try:
            provider = self._get_provider()
            raw_text = provider.classify(prompt, self.gatekeeper_config)
            result = self._parse_response(raw_text)
        except Exception as e:
            print(f"[TierClassifier] API呼び出しエラー: {e}")
            result = self._default_output()

        t1 = time.time()
        print(f"[TierClassifier] user='{user_input[:30]}' → tier={result['tier']} ({int((t1-t0)*1000)}ms)")
        return result

    def _determine_tier(self, llm_output: dict) -> str:
        """llm_reasoning のシグナルから tier を再決定する。"""
        signals = llm_output.get("llm_reasoning", {})

        recent_ok = signals.get("recent_context_sufficient", False)
        needs_mem = signals.get("needs_user_memory", False)
        needs_rel = signals.get("needs_relationship_state", False)
        needs_lt = signals.get("needs_long_term_search", False)

        if needs_lt:
            return "cortex"
        if recent_ok and not needs_mem and not needs_rel:
            return "reflex"
        if needs_rel:
            return "cortex"
        if needs_mem:
            return "mid"

        # シグナル全falseの場合はLLMのtierをフォールバック
        return llm_output.get("tier", "mid")

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

            llm_tier = data.get("tier", "mid")
            if llm_tier not in ("reflex", "mid", "cortex"):
                llm_tier = "mid"

            llm_reasoning = data.get("llm_reasoning", {})

            result = {
                "tier": llm_tier,
                "llm_tier": llm_tier,
                "llm_reasoning": llm_reasoning,
            }
            # Python側で再判定
            result["tier"] = self._determine_tier(result)
            return result

        except Exception as e:
            print(f"[TierClassifier] JSONパースエラー: {e}\nRaw: '{raw_text}'")
            return default

    def _default_output(self) -> dict:
        return {
            "tier": "mid",
            "llm_tier": "mid",
            "llm_reasoning": {},
        }

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
