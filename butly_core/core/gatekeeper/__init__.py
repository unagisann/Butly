"""
butly_core.core.gatekeeper
--------------------------
Gatekeeper パッケージ。
外部APIを維持する facade として機能する。

Re-exports:
  - Gatekeeper
  - SessionState
  - MemoryBlockBuilder
  - build_system_instruction_from_blocks
"""

import json

from butly_core.core.gatekeeper.session_state import SessionState
from butly_core.core.gatekeeper.tier_classifier import TierClassifier
from butly_core.core.gatekeeper.state_updater import StateUpdater
from butly_core.core.gatekeeper.search_planner import SearchPlanner
from butly_core.core.gatekeeper.memory_builder import (
    MemoryBlockBuilder,
    build_system_instruction_from_blocks,
    build_context_prefix,
    DEFAULT_CONTEXT_ORDER,
    CONTEXT_LEVEL_PRESETS,
    migrate_context_order_to_levels,
)

from pathlib import Path


class Gatekeeper:
    """
    外部API互換の facade。
    ChatService からの呼び出しインターフェースを維持する。
    """

    def __init__(self, base_dir: Path = None):
        self.base_dir = base_dir
        self.tier_classifier = TierClassifier(base_dir)
        self.state_updater = StateUpdater(base_dir)
        self.search_planner = SearchPlanner(base_dir)

    def classify(
        self,
        user_input: str,
        history_msgs: list,
        session_state: dict,
        current_topic: str = "",
        override_config: dict = None,
        instance_dir: Path = None,
    ) -> dict:
        """
        既存と同じシグネチャ・同じ返却形式を維持。

        Returns
        -------
        dict
            tier, topic, need, search_targets, state_delta, llm_scoring
        """
        # A. headlines 読み込み
        recent_headlines = self._load_headlines(instance_dir)

        # A2. agent_name をインスタンス config から解決
        agent_name = self._resolve_agent_name(instance_dir)

        # B. tier判定（headlines 付き）
        tier_result = self.tier_classifier.classify(
            user_input, history_msgs, current_topic,
            recent_headlines=recent_headlines,
            override_config=override_config,
            agent_name=agent_name,
        )
        tier = tier_result["tier"]

        # C. state_delta生成
        state_delta = self.state_updater.update(
            user_input, history_msgs, session_state,
            override_config=override_config,
            agent_name=agent_name,
        )

        # D. cortex時のみ検索計画
        need = None
        search_targets = None
        if tier == "cortex":
            plan = self.search_planner.plan(
                user_input, history_msgs, current_topic,
                override_config=override_config,
                agent_name=agent_name,
            )
            need = plan.get("need")
            search_targets = plan.get("search_targets")

        # 既存と同じ返却形式
        return {
            "tier": tier,
            "topic": state_delta.get("topic") or
                     (session_state.get("topic", current_topic) if isinstance(session_state, dict) else current_topic),
            "need": need,
            "search_targets": search_targets,
            "state_delta": state_delta,
            # デバッグ用
            "llm_scoring": tier_result.get("llm_scoring"),
        }

    def _load_headlines(self, instance_dir: Path = None) -> str:
        """recent_digest_headlines.json を読み込んでテキスト化する。"""
        if not instance_dir:
            return "(no recent headlines)"
        headlines_file = instance_dir / "recent_digest_headlines.json"
        if not headlines_file.exists():
            return "(no recent headlines)"
        try:
            data = json.loads(headlines_file.read_text(encoding="utf-8"))
            items = data.get("headlines", [])
            if not items:
                return "(no recent headlines)"
            lines = []
            for item in items:
                prefix = "Topic" if item.get("type") == "topic" else "Event"
                lines.append(f"- [{prefix}] {item.get('text', '')}")
            return "\n".join(lines)
        except Exception:
            return "(no recent headlines)"

    def _resolve_agent_name(self, instance_dir: Path = None) -> str:
        """instance_dir の config.json["agent"]["ai_name"] を読んで返す。見つからない場合は SYSTEM_CONFIG フォールバック。"""
        if instance_dir:
            config_path = instance_dir / "config.json"
            if config_path.exists():
                try:
                    cfg = json.loads(config_path.read_text(encoding="utf-8"))
                    name = cfg.get("agent", {}).get("ai_name", "")
                    if name:
                        return name
                except Exception:
                    pass
        from butly_core.config import SYSTEM_CONFIG
        return SYSTEM_CONFIG["agent"].get("agent_name", "Butly")

    def classify_tier_only(self, user_input: str, history_msgs: list,
                           current_topic: str = "") -> str:
        """後方互換用: tier文字列のみを返す。"""
        result = self.classify(
            user_input, history_msgs,
            session_state={}, current_topic=current_topic
        )
        return result.get("tier", "mid")


__all__ = [
    "Gatekeeper",
    "SessionState",
    "MemoryBlockBuilder",
    "build_system_instruction_from_blocks",
    "build_context_prefix",
    "TierClassifier",
    "StateUpdater",
    "SearchPlanner",
]
