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


class MemoryBlockBuilder:
    """
    tier に応じて brain.py に渡す記憶ブロック辞書を構築する。

    返却する辞書のキー:
        "short_term"  : 最近の会話履歴（list[dict]）
        "floating"    : 浮動要約テキスト（str）
        "mid_term"    : 中期記憶テキスト（str、mid 以上）
        "rag_context" : RAG 検索結果テキスト（str、cortex のみ）
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
            "reflex" | "mid" | "cortex"
        memory_manager : ButlyMemory
            記憶管理オブジェクト。
        brain : ButlyBrain | None
            cortex 時の RAG 検索に使用。None の場合 RAG はスキップ。
        user_input : str
            cortex 用 RAG 検索のクエリ文字列。
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
        use_summary = SYSTEM_CONFIG.get("memory", {}).get("use_summarized_mid_term", False)

        if use_summary:
            # 要約モード: digest + relationship を使用
            digest = memory_manager.get_mid_term_digest()
            relationship = memory_manager.get_mid_term_relationship()

            if digest or relationship:
                blocks["mid_term_digest"] = digest
                blocks["mid_term_relationship"] = relationship
                blocks["mid_term"] = ""  # RAWは空にする
                blocks["mid_term_mode"] = "summary"
                total_chars = len(digest) + len(relationship)
                print(f"[Gatekeeper] MemoryBlock: {tier}（+ digest {len(digest)}文字 + relationship {len(relationship)}文字 = {total_chars}文字）")
            else:
                # 要約ファイルが存在しない → RAWにフォールバック
                mid_term = memory_manager.get_mid_term_text_content()
                blocks["mid_term"] = mid_term
                blocks["mid_term_mode"] = "raw_fallback"
                print(f"[Gatekeeper] MemoryBlock: {tier}（要約なし → RAWフォールバック {len(mid_term)}文字）")
        else:
            # RAWモード: 従来通り
            mid_term = memory_manager.get_mid_term_text_content()
            blocks["mid_term"] = mid_term
            blocks["mid_term_mode"] = "raw"
            print(f"[Gatekeeper] MemoryBlock: {tier}（+ mid_term RAW {len(mid_term)}文字）")

        if tier == "mid":
            return blocks

        # --- cortex のみ: RAG 検索を実施 ---
        need = blocks.get("need")
        if brain and user_input and need:
            try:
                keyword_data = brain.extract_keywords(user_input, override_config)
                keywords = keyword_data.get("keywords", [])
                limit = SYSTEM_CONFIG["brain"]["search_limit"]
                knowledge_list = brain.search_knowledge(
                    keywords,
                    user_input,
                    instance_name=instance_name,
                    limit=limit,
                    override_config=override_config,
                )
                if knowledge_list:
                    rag_lines = ["【過去の記憶（RAG）】"]
                    for k in knowledge_list:
                        rag_lines.append(f"・{k['title']}: {k['summary']}")
                        if k.get("episode"):
                            rag_lines.append(f"  (補足: {k['episode']})")
                    blocks["rag_context"] = "\n".join(rag_lines)
                    print(f"[Gatekeeper] MemoryBlock: cortex（RAG hits={len(knowledge_list)}）")
            except Exception as e:
                print(f"[Gatekeeper] RAG 検索エラー: {e}")
        elif not need:
            print("[Gatekeeper] MemoryBlock: cortex（need=null のため RAG スキップ）")
        else:
            print("[Gatekeeper] MemoryBlock: cortex（brain/user_input 未指定のため RAG スキップ）")

        return blocks


def build_system_instruction_from_blocks(
    blocks: dict,
    memory_manager,
    use_google_search: bool = False,
    context_order: dict | None = None,
) -> str:
    """
    MemoryBlockBuilder.build() の戻り値から system_instruction 文字列を生成する。

    不変セクションのみを含む:
      1. SYSTEM INSTRUCTION（性格設定）
      2. KEY MEMORY（根幹記憶）

    セクション順序は context_order["system_instruction"] で制御可能。
    context_order が None の場合はデフォルト順を使用。

    Parameters
    ----------
    blocks : dict
        MemoryBlockBuilder.build() の戻り値。
    memory_manager : ButlyMemory
        system_instruction.txt / Key_Memory.txt の読み取りに使用。
    use_google_search : bool
        （後方互換のため残すが、現在は未使用）
    context_order : dict | None
        セクション順序設定。None の場合はデフォルト順。

    Returns
    -------
    str
        LLM に渡す system_instruction 文字列。
    """
    from butly_core.prompts import PromptLoader

    loader = PromptLoader()
    h = loader.get_section_header

    order = (context_order or DEFAULT_CONTEXT_ORDER).get(
        "system_instruction", DEFAULT_CONTEXT_ORDER["system_instruction"]
    )

    sys_inst = memory_manager.get_system_instruction()
    key_mem = memory_manager.get_key_memory()

    builders = {
        "system_instruction": lambda: f"{h('system_instruction')}\n{sys_inst}",
        "key_memory": lambda: (f"{h('key_memory')}\n{key_mem}" if key_mem else None),
    }

    sections = []
    for section_id in order:
        builder = builders.get(section_id)
        if builder:
            result = builder()
            if result:
                sections.append(result)

    return "\n\n".join(sections)


def build_context_prefix(
    blocks: dict,
    memory_manager=None,
    use_google_search: bool = False,
    context_order: dict | None = None,
) -> str:
    """
    可変コンテキスト（背景情報）を生成する。
    各 Provider が会話履歴の先頭に user メッセージとして注入する。

    セクション順序は context_order["context_prefix"] で制御可能。
    context_order が None の場合はデフォルト順を使用。

    Parameters
    ----------
    blocks : dict
        MemoryBlockBuilder.build() の戻り値。
    memory_manager : ButlyMemory | None
        （後方互換のため残すが、現在は未使用）
    use_google_search : bool
        Google 検索使用フラグ（注意書き用）。
    context_order : dict | None
        セクション順序設定。None の場合はデフォルト順。

    Returns
    -------
    str
        可変コンテキスト文字列。空の場合は空文字列。
    """
    from butly_core.core.chronos import ButlyChronos
    from butly_core.prompts import PromptLoader

    loader = PromptLoader()
    h = loader.get_section_header

    tier = blocks.get("tier", "mid")

    order = (context_order or DEFAULT_CONTEXT_ORDER).get(
        "context_prefix", DEFAULT_CONTEXT_ORDER["context_prefix"]
    )

    # --- セクションビルダー定義 ---
    def _build_label_notes():
        parts = []
        label = h('context_prefix_label')
        if label and label != 'context_prefix_label':
            parts.append(label)
        note = h('note_context_prefix')
        if note and note != 'note_context_prefix':
            parts.append(note)
        return "\n\n".join(parts) if parts else None

    def _build_current_time():
        current_time = ButlyChronos().get_system_note()
        return (
            f"{h('current_time')}\n{current_time}\n"
            f"{h('note_current_time')}"
        )

    def _build_mid_term():
        if tier not in ("mid", "cortex"):
            return None
        mid_term_mode = blocks.get("mid_term_mode", "raw")
        parts = []
        if mid_term_mode == "summary":
            digest = blocks.get("mid_term_digest", "")
            relationship = blocks.get("mid_term_relationship", "")
            if digest:
                parts.append(
                    f"{h('mid_term_digest')}\n"
                    f"{h('note_mid_term_digest')}\n"
                    f"{digest}"
                )
            if relationship:
                parts.append(
                    f"{h('relationship_snapshot')}\n"
                    f"{h('note_relationship')}\n"
                    f"{relationship}"
                )
        else:
            mid_term = blocks.get("mid_term", "")
            if mid_term:
                parts.append(f"{h('mid_term_memory')}\n{mid_term}")
        return "\n\n".join(parts) if parts else None

    def _build_rag():
        rag_context = blocks.get("rag_context", "")
        if tier == "cortex" and rag_context:
            return f"{h('long_term_memory')}\n{h('note_rag')}\n{rag_context}"
        return None

    def _build_floating():
        floating = blocks.get("floating", "")
        if floating:
            return f"{h('floating_summary')}\n{h('note_floating')}\n{floating.strip()}"
        return None

    def _build_tier_info():
        tier_text = h('tier_mode').format(tier=tier)
        topic = blocks.get("topic", "")
        if tier in ("mid", "cortex") and topic:
            tier_text += "\n" + h('tier_topic').format(topic=topic)
        return f"{h('tier_info')}\n{tier_text}"

    def _build_google_search():
        if use_google_search:
            return h('note_google_search')
        return None

    def _build_web_search():
        web_search_ctx = blocks.get("web_search_context", "")
        if web_search_ctx:
            return f"{h('web_search')}\n{web_search_ctx}"
        return None

    def _build_glossary():
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

    builders = {
        "label_notes": _build_label_notes,
        "current_time": _build_current_time,
        "glossary": _build_glossary,
        "mid_term": _build_mid_term,
        "rag": _build_rag,
        "floating": _build_floating,
        "tier_info": _build_tier_info,
        "google_search": _build_google_search,
        "web_search": _build_web_search,
    }

    sections = []
    for section_id in order:
        builder = builders.get(section_id)
        if builder:
            result = builder()
            if result:
                sections.append(result)

    return "\n\n".join(sections) if sections else ""
