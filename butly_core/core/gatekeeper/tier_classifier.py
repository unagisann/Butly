"""
tier_classifier.py
------------------
LLM に 4 つのスコア（0-1）を出力させ、Python 側で tier を決定する。
"""

import json
import re
import time
from pathlib import Path

from butly_core.config import AI_CONFIG, SYSTEM_CONFIG
from butly_core.prompts import PromptLoader


class TierClassifier:
    """LLM に 4 スコアを出力させ、Python 側で tier を最終決定する。"""

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

    def classify(self, user_input: str, history_msgs: list,
                 current_topic: str = "", override_config=None) -> dict:
        """
        Returns:
            {
                "tier": "reflex" | "mid" | "cortex",
                "llm_scoring": {
                    "response_complexity": float,
                    "emotional_weight": float,
                    "memory_reference_likelihood": float,
                    "continuity_need": float
                }
            }
        """
        if not self.gatekeeper_config:
            return self._default_output()

        model_name, gk_config = self._resolve_config(override_config)

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
            from butly_core.llm.factory import ProviderFactory
            provider = ProviderFactory.create(model_name)
            raw_text = provider.classify(prompt, gk_config)
            result = self._parse_response(raw_text)
        except Exception as e:
            print(f"[TierClassifier] API呼び出しエラー: {e}")
            result = self._default_output()

        t1 = time.time()
        scores = result.get("llm_scoring", {})
        print(
            f"[TierClassifier] user='{user_input[:30]}'\n"
            f"  scores: rc={scores.get('response_complexity', 0):.2f}, "
            f"ew={scores.get('emotional_weight', 0):.2f}, "
            f"ml={scores.get('memory_reference_likelihood', 0):.2f}, "
            f"cn={scores.get('continuity_need', 0):.2f}\n"
            f"  → tier={result['tier']} ({int((t1-t0)*1000)}ms)"
        )
        return result

    def _determine_tier_from_scores(self, scores: dict) -> str:
        """llm_scoring のスコアから tier を決定する。"""
        rc = scores.get("response_complexity", 0)
        ew = scores.get("emotional_weight", 0)
        ml = scores.get("memory_reference_likelihood", 0)
        cn = scores.get("continuity_need", 0)

        # cortex: 記憶参照の可能性が高い
        if ml >= 0.7:
            return "cortex"

        # reflex: 複雑さも記憶参照も低く、連続性も低い
        if rc <= 0.2 and ml <= 0.3 and cn <= 0.3:
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
            for key in ("response_complexity", "emotional_weight",
                        "memory_reference_likelihood", "continuity_need"):
                if key in llm_scoring:
                    llm_scoring[key] = max(0.0, min(1.0, float(llm_scoring[key])))

            tier = self._determine_tier_from_scores(llm_scoring)

            return {
                "tier": tier,
                "llm_scoring": llm_scoring,
            }

        except Exception as e:
            print(f"[TierClassifier] JSONパースエラー: {e}\nRaw: '{raw_text}'")
            return default

    def _default_output(self) -> dict:
        return {
            "tier": "mid",
            "llm_scoring": {},
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
