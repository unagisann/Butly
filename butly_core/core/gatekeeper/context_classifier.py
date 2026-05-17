"""
context_classifier.py
---------------------
LLM に 3 つのスコア（rc/ew/cn）を出力させ、Python 側で reflex/mid の 2 値 tier を決定する。
"""

import json
import re
import time
from pathlib import Path

from butly_core.config import AI_CONFIG, SYSTEM_CONFIG
from butly_core.prompts import PromptLoader


class ContextClassifier:
    """rc/ew/cn の 3 スコアで reflex/mid を判定する。"""

    def __init__(self, base_dir: Path = None):
        self.base_dir = base_dir or Path(__file__).resolve().parent.parent.parent.parent
        self._prompt_loader = PromptLoader(
            locale=SYSTEM_CONFIG.get("agent", {}).get("locale", "ja")
        )
        self._resolve_model_config()

    def _resolve_model_config(self):
        """context_classifier → gatekeeper の順で設定を解決する。"""
        cc_config = AI_CONFIG.get("context_classifier")
        if cc_config and cc_config.get("model_name"):
            self.model_name = cc_config["model_name"]
            self.gatekeeper_config = cc_config
        else:
            # フォールバック: 既存の gatekeeper 設定を使用
            gk_config = AI_CONFIG.get("gatekeeper", {})
            self.model_name = gk_config.get("model_name", "gemini-3.1-flash-lite-preview")
            self.gatekeeper_config = gk_config if gk_config else {
                "model_name": "gemini-3.1-flash-lite-preview",
                "generation_config": {"temperature": 0.0, "max_output_tokens": 256},
                "safety_settings": [],
            }

    def _resolve_config(self, override_config=None):
        """override_config から設定を解決する。
        優先順位: override[context_classifier] → AI_CONFIG[context_classifier]
                  → override[gatekeeper] → AI_CONFIG[gatekeeper]
        """
        # override に context_classifier があればそれを使う
        if override_config and "context_classifier" in override_config:
            merged = {**self.gatekeeper_config, **override_config["context_classifier"]}
            if "generation_config" in self.gatekeeper_config and "generation_config" in override_config.get("context_classifier", {}):
                merged["generation_config"] = {**self.gatekeeper_config.get("generation_config", {}), **override_config["context_classifier"]["generation_config"]}
            return merged.get("model_name", self.model_name), merged

        # override に gatekeeper があればフォールバック
        if override_config and "gatekeeper" in override_config:
            merged = {**self.gatekeeper_config, **override_config["gatekeeper"]}
            if "generation_config" in self.gatekeeper_config and "generation_config" in override_config.get("gatekeeper", {}):
                merged["generation_config"] = {**self.gatekeeper_config.get("generation_config", {}), **override_config["gatekeeper"]["generation_config"]}
            return merged.get("model_name", self.model_name), merged

        return self.model_name, self.gatekeeper_config

    def classify(self, user_input: str, history_msgs: list,
                 current_topic: str = "", recent_headlines: str = "",
                 override_config=None, agent_name: str = None) -> dict:
        """
        Returns:
            {
                "tier": "reflex" | "mid",
                "llm_scoring": {
                    "response_complexity": float,
                    "emotional_weight": float,
                    "continuity_need": float
                }
            }
        """
        if not self.gatekeeper_config:
            return self._default_output()

        model_name, gk_config = self._resolve_config(override_config)

        t0 = time.time()

        _agent_name = agent_name or SYSTEM_CONFIG["agent"]["agent_name"]
        history_text = self._format_history(history_msgs, max_turns=3, agent_name=_agent_name)
        prompt = self._prompt_loader.get(
            "context_classifier",
            agent_name=_agent_name,
            current_topic=current_topic or "(未設定)",
            history_text=history_text,
            user_input=user_input,
        )

        try:
            from butly_core.llm.factory import ProviderFactory
            provider = ProviderFactory.create(model_name)
            raw_text = provider.classify(prompt, gk_config)
            result = self._parse_response(raw_text)
        except Exception as e:
            print(f"[ContextClassifier] API呼び出しエラー: {e}")
            result = self._default_output()

        t1 = time.time()
        scores = result.get("llm_scoring", {})
        print(
            f"[ContextClassifier] user='{user_input[:30]}'\n"
            f"  scores: rc={scores.get('response_complexity', 0):.2f}, "
            f"ew={scores.get('emotional_weight', 0):.2f}, "
            f"cn={scores.get('continuity_need', 0):.2f}\n"
            f"  → tier={result['tier']} ({int((t1-t0)*1000)}ms)"
        )
        return result

    def _determine_tier_from_scores(self, scores: dict) -> str:
        """llm_scoring のスコアから tier を決定する (reflex/mid の 2 値)。"""
        rc = scores.get("response_complexity", 0)
        cn = scores.get("continuity_need", 0)

        # reflex: 軽い発話
        if rc <= 0.4 and cn <= 0.3:
            return "reflex"

        # mid: それ以外
        return "mid"

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

            llm_scoring = data.get("llm_scoring", {})
            # スコア値を 0-1 にクランプ
            for key in ("response_complexity", "emotional_weight", "continuity_need"):
                if key in llm_scoring:
                    llm_scoring[key] = max(0.0, min(1.0, float(llm_scoring[key])))

            tier = self._determine_tier_from_scores(llm_scoring)

            return {
                "tier": tier,
                "llm_scoring": llm_scoring,
            }

        except Exception as e:
            print(f"[ContextClassifier] JSONパースエラー: {e}\nRaw: '{raw_text}'")
            return default

    def _default_output(self) -> dict:
        return {
            "tier": "mid",
            "llm_scoring": {},
        }

    def _format_history(self, history_msgs: list, max_turns: int = 3, agent_name: str = None) -> str:
        """history_msgs の末尾 max_turns 件を文字列へ変換する。"""
        if not history_msgs:
            return "（履歴なし）"

        _agent_name = agent_name or SYSTEM_CONFIG["agent"]["agent_name"]
        recent = history_msgs[-(max_turns * 2):]
        lines = []
        for msg in recent:
            role = msg.get("role", "unknown")
            parts = msg.get("parts", [""])
            text = parts[0] if parts else ""
            if isinstance(text, str) and len(text) > 80:
                text = text[:80] + "…"
            label = "ユーザー" if role == "user" else _agent_name
            lines.append(f"{label}: {text}")

        return "\n".join(lines) if lines else "（履歴なし）"
