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

from butly_core.core.gatekeeper.session_state import SessionState
from butly_core.core.gatekeeper.tier_classifier import TierClassifier
from butly_core.core.gatekeeper.state_updater import StateUpdater
from butly_core.core.gatekeeper.search_planner import SearchPlanner
from butly_core.core.gatekeeper.memory_builder import (
    MemoryBlockBuilder,
    build_system_instruction_from_blocks,
    build_context_prefix,
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
    ) -> dict:
        """
        既存と同じシグネチャ・同じ返却形式を維持。

        Returns
        -------
        dict
            tier, topic, need, search_targets, state_delta, llm_tier, llm_reasoning
        """
        # A. tier判定
        tier_result = self.tier_classifier.classify(
            user_input, history_msgs, current_topic,
            override_config=override_config,
        )
        tier = tier_result["tier"]

        # B. state_delta生成
        state_delta = self.state_updater.update(
            user_input, history_msgs, session_state,
            override_config=override_config,
        )

        # C. cortex時のみ検索計画
        need = None
        search_targets = None
        if tier == "cortex":
            plan = self.search_planner.plan(
                user_input, history_msgs, current_topic,
                override_config=override_config,
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
