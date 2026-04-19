"""
memory_builder.py
-----------------
MemoryBlockBuilder: tier → 記憶ブロック dict を構築するクラス。
build_system_instruction_from_blocks: ブロック → system_instruction 変換関数。
build_context_prefix: 可変コンテキスト変換関数。
"""

from butly_core.config import SYSTEM_CONFIG


# セクション順序のデフォルト定義
DEFAULT_CONTEXT_ORDER = {
    "system_instruction": ["system_instruction", "key_memory"],
    "context_prefix": [
        "label_notes",
        "current_time",
        "glossary",
        "mid_term",
        "rag",
        "floating",
        "tier_info",
        "google_search",
        "web_search",
    ],
    "system_instruction_position": "top",
}

# ===================================================================
# context_levels プリセット定義
# ===================================================================

CONTEXT_LEVEL_PRESETS = {
    "normal": {
        "system_instruction": "high",
        "key_memory": "high",
        "label_notes": "high",
        "current_time": "high",
        "glossary": "high",
        "mid_term": "high",
        "rag": "high",
        "floating": "high",
        "tier_info": "high",
        "web_search": "high",
    },
    "compact": {
        "system_instruction": "high",
        "key_memory": "low",
        "label_notes": "off",
        "current_time": "high",
        "glossary": "off",
        "mid_term": "mid",
        "rag": "high",
        "floating": "high",
        "tier_info": "high",
        "web_search": "high",
    },
    "low": {
        "system_instruction": "low",
        "key_memory": "low",
        "label_notes": "off",
        "current_time": "low",
        "glossary": "off",
        "mid_term": "low",
        "rag": "low",
        "floating": "off",
        "tier_info": "off",
        "web_search": "high",
    },
}


# ===================================================================
# 後方互換マイグレーション
# ===================================================================

def migrate_context_order_to_levels(config: dict) -> dict:
    """旧 context_order → 新 context_levels への変換。"""
    old = config.get("context_order")
    if old is None:
        return config
    if "context_levels" in config:
        return config  # すでに新形式

    si_order = old.get("system_instruction", DEFAULT_CONTEXT_ORDER["system_instruction"])
    cp_order = old.get("context_prefix", DEFAULT_CONTEXT_ORDER["context_prefix"])
    position = old.get("system_instruction_position", "top")

    all_si = DEFAULT_CONTEXT_ORDER["system_instruction"]
    all_cp = DEFAULT_CONTEXT_ORDER["context_prefix"]

    levels = {}
    for s in all_si:
        levels[s] = "high" if s in si_order else "off"
    for s in all_cp:
        levels[s] = "high" if s in cp_order else "off"

    config["context_levels"] = {
        "preset": "custom",
        "levels": levels,
        "order": {
            "system_instruction": si_order,
            "context_prefix": cp_order,
        },
        "system_instruction_position": position,
    }
    return config


# ===================================================================
# レベル解決ヘルパー
# ===================================================================

def _resolve_levels(context_levels: dict | None, context_order: dict | None) -> dict:
    """context_levels または context_order からレベル辞書を解決する。"""
    if context_levels:
        return context_levels.get("levels", {})
    if context_order:
        all_sections = (
            DEFAULT_CONTEXT_ORDER["system_instruction"]
            + DEFAULT_CONTEXT_ORDER["context_prefix"]
        )
        si_order = context_order.get("system_instruction", [])
        cp_order = context_order.get("context_prefix", [])
        active = set(si_order + cp_order)
        return {s: ("high" if s in active else "off") for s in all_sections}
    return {s: "high" for s in
            DEFAULT_CONTEXT_ORDER["system_instruction"]
            + DEFAULT_CONTEXT_ORDER["context_prefix"]}


def _resolve_order(context_levels: dict | None, context_order: dict | None, key: str) -> list:
    """セクション順序を解決する。off要素も順序上は保持される。"""
    if context_levels and "order" in context_levels:
        return context_levels["order"].get(key, DEFAULT_CONTEXT_ORDER[key])
    if context_order:
        return context_order.get(key, DEFAULT_CONTEXT_ORDER[key])
    return DEFAULT_CONTEXT_ORDER[key]


class MemoryBlockBuilder:
    """
    tier に応じて brain.py に渡す記憶ブロック辞書を構築する。

    返却する辞書のキー:
        "short_term"  : 最近の会話履歴（list[dict]）
        "floating"    : 浮動要約テキスト（str）
        "mid_term"    : 中期記憶テキスト（str、mid 以上）
        "rag_context" : RAG 検索結果テキスト（str、need 有時のみ）
        "tier"        : 使用した tier（str、デバッグ用）
        "topic"       : 現在の話題（str）
    """

    def build(
        self,
        tier: str,
        memory_manager,
        brain=None,
        user_input: str = "",
        instance_name: str = "00_master",
        override_config: dict = None,
        gatekeeper_output: dict = None,
    ) -> dict:
        """
        Parameters
        ----------
        tier : str
            "reflex" | "mid"
        memory_manager : ButlyMemory
            記憶管理オブジェクト。
        brain : ButlyBrain | None
            RAG 検索に使用。None の場合 RAG はスキップ。
        user_input : str
            RAG 検索のクエリ文字列。
        instance_name : str
            RAG 検索時のインスタンス名。
        override_config : dict | None
            brain.search_knowledge へ渡すオーバーライド設定。
        gatekeeper_output : dict | None
            Gatekeeperの出力する構造化辞書

        Returns
        -------
        dict
            記憶ブロック辞書。
        """
        # --- 全 tier 共通 ---
        short_term_msgs, _ = memory_manager.load_recent_sessions(limit=6)
        floating = memory_manager.get_floating_summary()

        # topic を gatekeeper_output から取得
        topic = ""
        if gatekeeper_output:
            topic = gatekeeper_output.get("topic", "")

        blocks = {
            "tier": tier,
            "short_term": short_term_msgs,
            "floating": floating,
            "mid_term": "",
            "rag_context": "",
            "topic": topic,
            "need": gatekeeper_output.get("need") if gatekeeper_output else None,
            "search_targets": gatekeeper_output.get("search_targets") if gatekeeper_output else None,
        }

        if tier == "reflex":
            # short_term + floating のみ
            print("[Gatekeeper] MemoryBlock: reflex（short_term + floating）")
            return blocks

        # --- mid 以上: mid_term を追加 ---
        # インスタンス設定を優先、なければシステムデフォルト
        _inst_mem = override_config.get("memory", {}) if override_config else {}
        use_summary = _inst_mem.get(
            "use_summarized_mid_term",
            SYSTEM_CONFIG.get("memory", {}).get("use_summarized_mid_term", True),
        )

        if use_summary:
            # 要約モード: digest + recent_snapshot を使用
            digest = memory_manager.get_mid_term_digest()
            recent_snapshot = memory_manager.get_recent_snapshot()

            if digest or recent_snapshot:
                blocks["mid_term_digest"] = digest
                blocks["mid_term_recent_snapshot"] = recent_snapshot
                blocks["mid_term"] = ""  # RAWは空にする
                blocks["mid_term_mode"] = "summary"
                total_chars = len(digest) + len(recent_snapshot)
                print(f"[Gatekeeper] MemoryBlock: {tier}（+ digest {len(digest)}文字 + recent_snapshot {len(recent_snapshot)}文字 = {total_chars}文字）")
            else:
                # 要約ファイルが存在しない → RAWにフォールバック
                mid_term = memory_manager.get_raw_memory()
                blocks["mid_term"] = mid_term
                blocks["mid_term_mode"] = "raw_fallback"
                print(f"[Gatekeeper] MemoryBlock: {tier}（要約なし → RAWフォールバック {len(mid_term)}文字）")
        else:
            # RAWモード: 1_integrated + 2_knowledgeized から読み込み
            mid_term = memory_manager.get_raw_memory()
            blocks["mid_term"] = mid_term
            blocks["mid_term_mode"] = "raw"
            print(f"[Gatekeeper] MemoryBlock: {tier}（+ mid_term RAW {len(mid_term)}文字）")

        # --- RAG: need がある場合、probe candidates から RAG コンテキストを構築 ---
        need = blocks.get("need")
        probe = gatekeeper_output.get("memory_probe", {}) if gatekeeper_output else {}
        candidates = probe.get("candidates", [])
        glossary_hits = probe.get("glossary_hits", [])

        if glossary_hits:
            blocks["glossary_hits"] = glossary_hits

        if need and candidates:
            rag_lines = ["【過去の記憶（RAG）】"]
            for c in candidates:
                rag_lines.append(f"・{c['title']}: {c['summary']}")
                if c.get("episode"):
                    rag_lines.append(f"  (補足: {c['episode']})")
            blocks["rag_context"] = "\n".join(rag_lines)
            print(f"[Gatekeeper] MemoryBlock: {tier}（RAG probe hits={len(candidates)}）")
        elif not need:
            print(f"[Gatekeeper] MemoryBlock: {tier}（need=null のため RAG スキップ）")
        else:
            print(f"[Gatekeeper] MemoryBlock: {tier}（probe candidates なし）")

        return blocks


def build_system_instruction_from_blocks(
    blocks: dict,
    memory_manager,
    use_google_search: bool = False,
    context_order: dict | None = None,
    context_levels: dict | None = None,
) -> str:
    """
    MemoryBlockBuilder.build() の戻り値から system_instruction 文字列を生成する。

    不変セクションのみを含む:
      1. SYSTEM INSTRUCTION（性格設定）
      2. KEY MEMORY（根幹記憶）

    セクション順序は context_levels["order"]["system_instruction"] または
    context_order["system_instruction"] で制御可能。

    Parameters
    ----------
    blocks : dict
        MemoryBlockBuilder.build() の戻り値。
    memory_manager : ButlyMemory
        system_instruction.txt / Key_Memory.txt の読み取りに使用。
    use_google_search : bool
        （後方互換のため残すが、現在は未使用）
    context_order : dict | None
        セクション順序設定（後方互換）。None の場合はデフォルト順。
    context_levels : dict | None
        新形式のレベル設定。

    Returns
    -------
    str
        LLM に渡す system_instruction 文字列。
    """
    from butly_core.prompts import PromptLoader

    loader = PromptLoader()
    h = loader.get_section_header

    levels = _resolve_levels(context_levels, context_order)
    order = _resolve_order(context_levels, context_order, "system_instruction")

    si_level = levels.get("system_instruction", "high")
    km_level = levels.get("key_memory", "high")

    # LOW版ファイルの読み取り
    if si_level == "low" and memory_manager:
        sys_inst = memory_manager.get_system_instruction_low()
    else:
        sys_inst = memory_manager.get_system_instruction() if memory_manager else ""

    if km_level == "low" and memory_manager:
        key_mem = memory_manager.get_key_memory_low()
    else:
        key_mem = memory_manager.get_key_memory() if memory_manager else ""

    agent_profile_header = (
        memory_manager.get_agent_profile_header() if memory_manager else ""
    )

    builders = {
        "system_instruction": lambda: _build_si_section(sys_inst, si_level, h, agent_profile_header),
        "key_memory": lambda: _build_km_section(key_mem, km_level, h),
    }

    sections = []
    for section_id in order:
        if levels.get(section_id, "high") == "off" and section_id != "system_instruction":
            continue  # off はスキップ。system_instruction は off 不可
        builder = builders.get(section_id)
        if builder:
            result = builder()
            if result:
                sections.append(result)

    return "\n\n".join(sections)


def _build_si_section(sys_inst: str, level: str, h, agent_profile_header: str = "") -> str | None:
    if not sys_inst and not agent_profile_header:
        return None
    body = (sys_inst or "").strip()
    header = (agent_profile_header or "").strip()
    if level == "low":
        parts = [p for p in (header, body) if p]
        return "\n\n".join(parts) if parts else None
    # high / mid
    combined = "\n\n".join(p for p in (header, body) if p)
    return f"{h('system_instruction')}\n{combined}" if combined else None


def _build_km_section(key_mem: str, km_level: str, h) -> str | None:
    if not key_mem or km_level == "off":
        return None
    if km_level == "low":
        return f"[会話核] {key_mem.strip()}"
    # high / mid
    return f"{h('key_memory')}\n{key_mem}"


def build_context_prefix(
    blocks: dict,
    memory_manager=None,
    use_google_search: bool = False,
    context_order: dict | None = None,
    context_levels: dict | None = None,
) -> str:
    """
    可変コンテキスト（背景情報）を生成する。
    各 Provider が会話履歴の先頭に user メッセージとして注入する。

    セクション順序は context_levels["order"]["context_prefix"] または
    context_order["context_prefix"] で制御可能。

    Parameters
    ----------
    blocks : dict
        MemoryBlockBuilder.build() の戻り値。
    memory_manager : ButlyMemory | None
        （後方互換のため残すが、現在は未使用）
    use_google_search : bool
        Google 検索使用フラグ（注意書き用）。
    context_order : dict | None
        セクション順序設定（後方互換）。None の場合はデフォルト順。
    context_levels : dict | None
        新形式のレベル設定。

    Returns
    -------
    str
        可変コンテキスト文字列。空の場合は空文字列。
    """
    from butly_core.core.chronos import ButlyChronos
    from butly_core.prompts import PromptLoader

    loader = PromptLoader()
    h = loader.get_section_header

    levels = _resolve_levels(context_levels, context_order)
    order = _resolve_order(context_levels, context_order, "context_prefix")
    tier = blocks.get("tier", "mid")

    builders = {
        "label_notes": lambda: _build_label_notes(levels.get("label_notes", "high"), h),
        "current_time": lambda: _build_current_time(levels.get("current_time", "high"), h),
        "glossary": lambda: _build_glossary(blocks, memory_manager, levels.get("glossary", "high"), h),
        "mid_term": lambda: _build_mid_term(blocks, levels.get("mid_term", "high"), tier, h),
        "rag": lambda: _build_rag(blocks, levels.get("rag", "high"), tier, h),
        "floating": lambda: _build_floating(blocks, levels.get("floating", "high"), h),
        "tier_info": lambda: _build_tier_info(blocks, levels.get("tier_info", "high"), h),
        "google_search": lambda: _build_google_search(levels.get("google_search", "high"), use_google_search, h),
        "web_search": lambda: _build_web_search(blocks, levels.get("web_search", "high"), h),
    }

    sections = []
    for section_id in order:
        if levels.get(section_id, "high") == "off":
            continue
        builder = builders.get(section_id)
        if builder:
            result = builder()
            if result:
                sections.append(result)

    return "\n\n".join(sections) if sections else ""


# ===================================================================
# 各要素のレベル別ビルダー関数
# ===================================================================


def _build_label_notes(level: str, h) -> str | None:
    if level in ("off", "low"):
        return None
    parts = []
    label = h('context_prefix_label')
    if label and label != 'context_prefix_label':
        parts.append(label)
    note = h('note_context_prefix')
    if note and note != 'note_context_prefix':
        parts.append(note)
    return "\n\n".join(parts) if parts else None


def _build_current_time(level: str, h) -> str | None:
    if level == "off":
        return None
    from butly_core.core.chronos import ButlyChronos
    current_time = ButlyChronos().get_system_note()
    if level == "low":
        return current_time.split("\n")[0].strip()  # 時刻のみ1行
    # high / mid
    return f"{h('current_time')}\n{current_time}\n{h('note_current_time')}"


def _build_glossary(blocks: dict, memory_manager, level: str, h) -> str | None:
    if level in ("off", "low"):
        return None

    # Phase 1.5: probe の glossary_hits があればそれだけ注入
    glossary_hits = blocks.get("glossary_hits", [])
    if glossary_hits:
        lines = []
        for hit in glossary_hits:
            lines.append(f"- {hit['term']}: {hit['definition']}")
        return (
            f"{h('glossary')}\n"
            f"{h('note_glossary')}\n"
            + "\n".join(lines)
        )

    # フォールバック: 従来の全件注入
    if not memory_manager:
        return None
    glossary_text = memory_manager.get_glossary()
    if glossary_text:
        return (
            f"{h('glossary')}\n"
            f"{h('note_glossary')}\n"
            f"{glossary_text}"
        )
    return None


def _build_mid_term(blocks: dict, level: str, tier: str, h) -> str | None:
    if level == "off":
        return None
    if tier not in ("mid",):
        return None

    mid_term_mode = blocks.get("mid_term_mode", "raw")

    if level == "low":
        if mid_term_mode == "summary":
            digest = blocks.get("mid_term_digest", "")
            recent_snapshot = blocks.get("mid_term_recent_snapshot", "")
            text = (digest + "\n" + recent_snapshot).strip()
        else:
            text = blocks.get("mid_term", "")
        if not text:
            return None
        lines = [l for l in text.strip().split("\n") if l.strip()]
        truncated = lines[-4:] if len(lines) > 4 else lines
        return "[直近の背景]\n" + "\n".join(truncated)

    # high / mid
    parts = []
    if mid_term_mode == "summary":
        digest = blocks.get("mid_term_digest", "")
        recent_snapshot = blocks.get("mid_term_recent_snapshot", "")
        if digest:
            parts.append(
                f"{h('mid_term_digest')}\n"
                f"{h('note_mid_term_digest')}\n"
                f"{digest}"
            )
        if recent_snapshot:
            parts.append(
                f"{h('recent_snapshot')}\n"
                f"{h('note_recent_snapshot')}\n"
                f"{recent_snapshot}"
            )
    else:
        mid_term = blocks.get("mid_term", "")
        if mid_term:
            parts.append(f"{h('mid_term_memory')}\n{mid_term}")
    return "\n\n".join(parts) if parts else None


def _build_rag(blocks: dict, level: str, tier: str, h) -> str | None:
    if level == "off":
        return None
    if tier == "reflex":
        return None
    rag_context = blocks.get("rag_context", "")
    if not rag_context:
        return None
    if level == "low":
        lines = [l for l in rag_context.strip().split("\n") if l.strip()]
        truncated = lines[:3] if len(lines) > 3 else lines
        return "[関連記憶]\n" + "\n".join(truncated)
    # high / mid
    return f"{h('long_term_memory')}\n{h('note_rag')}\n{rag_context}"


def _build_floating(blocks: dict, level: str, h) -> str | None:
    if level in ("off", "low"):
        return None
    floating = blocks.get("floating", "")
    if not floating:
        return None
    return f"{h('floating_summary')}\n{h('note_floating')}\n{floating.strip()}"


def _build_tier_info(blocks: dict, level: str, h) -> str | None:
    if level in ("off", "low"):
        return None
    tier = blocks.get("tier", "mid")
    tier_text = h('tier_mode').format(tier=tier)
    topic = blocks.get("topic", "")
    if tier in ("mid",) and topic:
        tier_text += "\n" + h('tier_topic').format(topic=topic)
    return f"{h('tier_info')}\n{tier_text}"


def _build_google_search(level: str, use_google_search: bool, h) -> str | None:
    if level == "off" or not use_google_search:
        return None
    return h('note_google_search')


def _build_web_search(blocks: dict, level: str, h) -> str | None:
    if level == "off":
        return None
    web_search_ctx = blocks.get("web_search_context", "")
    if not web_search_ctx:
        return None
    if level == "low":
        return web_search_ctx.strip()[:300]
    # high / mid
    return f"{h('web_search')}\n{web_search_ctx}"
