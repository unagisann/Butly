"""
memory_judge.py
---------------
長期記憶 (RAG) の検索が必要かを毎ターン判定し、必要なら検索キーワードを生成する。
Phase 1: search_planner.py からリネーム＋責務拡大（cortex 限定→毎ターン実行）。
"""

import json
import re
import time
from pathlib import Path

from butly_core.config import AI_CONFIG, SYSTEM_CONFIG
from butly_core.prompts import PromptLoader


class MemoryJudge:
    """長期記憶 (RAG) の検索が必要かを判定し、必要なら検索キーワードを生成する。"""

    def __init__(self, base_dir: Path = None):
        self.base_dir = base_dir or Path(__file__).resolve().parent.parent.parent.parent
        self._prompt_loader = PromptLoader(
            locale=SYSTEM_CONFIG.get("agent", {}).get("locale", "ja")
        )
        self._resolve_model_config()

    def _resolve_model_config(self):
        """memory_judge → gatekeeper の順で設定を解決する。"""
        mj_config = AI_CONFIG.get("memory_judge")
        if mj_config and mj_config.get("model_name"):
            self.model_name = mj_config["model_name"]
            self.gatekeeper_config = mj_config
        else:
            # フォールバック: 既存の gatekeeper 設定を使用
            gk_config = AI_CONFIG.get("gatekeeper", {})
            self.model_name = gk_config.get("model_name", "gemini-3.1-flash-lite-preview")
            self.gatekeeper_config = gk_config if gk_config else {
                "model_name": "gemini-3.1-flash-lite-preview",
                "generation_config": {"temperature": 0.0, "max_output_tokens": 512},
                "safety_settings": [],
            }

    def _resolve_config(self, override_config=None):
        """override_config から設定を解決する。
        優先順位: override[memory_judge] → AI_CONFIG[memory_judge]
                  → override[gatekeeper] → AI_CONFIG[gatekeeper]
        """
        # override に memory_judge があればそれを使う
        if override_config and "memory_judge" in override_config:
            merged = {**self.gatekeeper_config, **override_config["memory_judge"]}
            if "generation_config" in self.gatekeeper_config and "generation_config" in override_config.get("memory_judge", {}):
                merged["generation_config"] = {**self.gatekeeper_config.get("generation_config", {}), **override_config["memory_judge"]["generation_config"]}
            return merged.get("model_name", self.model_name), merged

        # override に gatekeeper があればフォールバック
        if override_config and "gatekeeper" in override_config:
            merged = {**self.gatekeeper_config, **override_config["gatekeeper"]}
            if "generation_config" in self.gatekeeper_config and "generation_config" in override_config.get("gatekeeper", {}):
                merged["generation_config"] = {**self.gatekeeper_config.get("generation_config", {}), **override_config["gatekeeper"]["generation_config"]}
            return merged.get("model_name", self.model_name), merged

        return self.model_name, self.gatekeeper_config

    def judge(self, user_input: str, history_msgs: list,
              current_topic: str = "", recent_headlines: str = "",
              override_config=None, agent_name: str = None) -> dict:
        """
        Returns:
            {
                "need": str | None,       # null なら RAG 不要
                "search_targets": list[str] | None,
            }
        """
        if not self.gatekeeper_config:
            return self._default_output()

        model_name, gk_config = self._resolve_config(override_config)

        t0 = time.time()

        _agent_name = agent_name or SYSTEM_CONFIG["agent"]["agent_name"]
        history_text = self._format_history(history_msgs, max_turns=3, agent_name=_agent_name)
        prompt = self._prompt_loader.get(
            "memory_judge",
            agent_name=_agent_name,
            current_topic=current_topic or "(未設定)",
            history_text=history_text,
            user_input=user_input,
            recent_headlines=recent_headlines or "(no recent headlines)",
        )

        try:
            from butly_core.llm.factory import ProviderFactory
            provider = ProviderFactory.create(model_name)
            raw_text = provider.classify(prompt, gk_config)
            result = self._parse_response(raw_text)
        except Exception as e:
            print(f"[MemoryJudge] API呼び出しエラー: {e}")
            result = self._default_output()

        t1 = time.time()
        need = result.get("need")
        targets = result.get("search_targets")
        print(f"[MemoryJudge] need='{need}', targets={targets} ({int((t1-t0)*1000)}ms)")
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

            need = data.get("need")
            search_targets = data.get("search_targets")

            # LLM が "None" / "null" を文字列として出力するケースを正規化
            if need in ("None", "null", ""):
                need = None
            if search_targets in ("None", "null", []):
                search_targets = None

            return {
                "need": need,
                "search_targets": search_targets,
            }

        except Exception as e:
            print(f"[MemoryJudge] JSONパースエラー: {e}\nRaw: '{raw_text}'")
            return default

    def _default_output(self) -> dict:
        return {
            "need": None,
            "search_targets": None,
        }

    def _format_history(self, history_msgs: list, max_turns: int = 3, agent_name: str = None) -> str:
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
            label = "ユーザー" if role == "user" else (agent_name or SYSTEM_CONFIG["agent"]["agent_name"])
            lines.append(f"{label}: {text}")

        return "\n".join(lines) if lines else "（履歴なし）"
