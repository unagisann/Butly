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
from butly_core.core.gatekeeper.context_classifier import ContextClassifier
from butly_core.core.gatekeeper.state_updater import StateUpdater
from butly_core.core.gatekeeper.memory_probe import MemoryProbe
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
    外部API の facade。
    ContextClassifier + MemoryProbe を pre-response で実行。
    StateUpdater は post-response (update_state) で別途実行され、generate と並列化できる。
    tier は reflex/mid の 2 値。RAG 判定は need（MemoryProbe 結果）で独立制御。
    """

    def __init__(self, base_dir: Path = None):
        self.base_dir = base_dir
        self.context_classifier = ContextClassifier(base_dir)
        self.memory_probe = MemoryProbe(base_dir)
        self.state_updater = StateUpdater(base_dir)

    def classify(
        self,
        user_input: str,
        history_msgs: list,
        session_state: dict,
        current_topic: str = "",
        override_config: dict = None,
        instance_dir: Path = None,
        brain=None,
        memory_manager=None,
    ) -> dict:
        """
        Pre-response 判定 (ContextClassifier + MemoryProbe)。
        StateUpdater は呼ばない — 別途 `update_state()` を post-response で実行すること。

        Returns
        -------
        dict
            tier, topic, need, need_intent, search_targets, state_delta,
            llm_scoring, memory_probe
            (state_delta は常に空 dict。後方互換のためフィールド自体は残置)
        """
        # A. headlines 読み込み
        recent_headlines = self._load_headlines(instance_dir)

        # A2. agent_name をインスタンス config から解決
        agent_name = self._resolve_agent_name(instance_dir)

        # B. ContextClassifier (LLM 呼び出し)
        instance_name = instance_dir.name if instance_dir else "00_master"

        # topic は post-response の StateUpdater が育てるため、classify 時点では
        # session_state の既存 topic (1 ターン前の値) を使う。引数 current_topic が
        # 明示されていればそちらを優先 (classify_tier_only 互換)。
        if not current_topic and isinstance(session_state, dict):
            current_topic = session_state.get("topic", "") or ""

        ctx_result = self.context_classifier.classify(
            user_input,
            history_msgs,
            current_topic,
            recent_headlines=recent_headlines,
            override_config=override_config,
            agent_name=agent_name,
        )

        # CC が出した意図種別を取り出して probe ゲートに使う
        need_intent = ctx_result.get("need_intent")

        # C. MemoryProbe
        # Glossary scan (Layer 1.5) は regex のみで軽量なので常時実行する。
        # Vector / Deep (Layer 1, 2) は need_intent で内部ゲートされる。
        # probe 内部で memory_manager/brain の有無に応じて適切にレイヤーをスキップする。
        probe_result = self.memory_probe.probe(
            user_input=user_input,
            brain=brain,
            memory_manager=memory_manager,
            instance_name=instance_name,
            recent_headlines=recent_headlines,
            override_config=override_config,
            history_msgs=history_msgs,
            need_intent=need_intent,
        )

        tier = ctx_result["tier"]  # "reflex" or "mid"
        candidates = probe_result.get("candidates", [])
        glossary_hits = probe_result.get("glossary_hits", [])

        # 最終 need 決定: LLM 意図 + 該当 evidence の組み合わせで判定
        need = None
        search_targets = None
        if need_intent == "glossary" and glossary_hits:
            need = "glossary"
            search_targets = [g.get("term", "") for g in glossary_hits[:3]]
        elif need_intent in ("past_fact", "relationship") and candidates:
            need = need_intent
            search_targets = [c.get("title", "") for c in candidates[:3]]

        return {
            "tier": tier,
            "topic": current_topic,
            "need": need,
            "need_intent": need_intent,
            "search_targets": search_targets,
            "state_delta": {},  # post-response で別途更新
            "llm_scoring": ctx_result.get("llm_scoring"),
            "classifier_status": ctx_result.get("classifier_status"),
            "fallback_reason": ctx_result.get("fallback_reason"),
            "original_need_intent": ctx_result.get("original_need_intent"),
            "intent_floor_applied": ctx_result.get("intent_floor_applied"),
            "memory_probe": probe_result,
        }

    def update_state(
        self,
        user_input: str,
        history_msgs: list,
        session_state: dict,
        assistant_response: str = "",
        override_config: dict = None,
        instance_dir: Path = None,
    ) -> dict:
        """
        Post-response の StateUpdater 実行。
        assistant_response は将来 prompt に含めるための窓口 (現状は未使用)。

        Returns
        -------
        dict
            state_delta: {"topic": str | None, "mood": str | None}
        """
        agent_name = self._resolve_agent_name(instance_dir)
        return self.state_updater.update(
            user_input,
            history_msgs,
            session_state,
            override_config=override_config,
            agent_name=agent_name,
        )

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

    def classify_tier_only(
        self, user_input: str, history_msgs: list, current_topic: str = ""
    ) -> str:
        """後方互換用: tier文字列のみを返す。"""
        result = self.classify(
            user_input, history_msgs, session_state={}, current_topic=current_topic
        )
        return result.get("tier", "mid")


__all__ = [
    "Gatekeeper",
    "SessionState",
    "MemoryBlockBuilder",
    "build_system_instruction_from_blocks",
    "build_context_prefix",
    "ContextClassifier",
    "MemoryProbe",
    "StateUpdater",
]
