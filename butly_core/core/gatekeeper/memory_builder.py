"""
memory_builder.py
-----------------
MemoryBlockBuilder: tier → 記憶ブロック dict を構築するクラス。
build_system_instruction_from_blocks: ブロック → system_instruction 変換関数。
build_context_prefix: 可変コンテキスト変換関数。
"""

from butly_core.config import SYSTEM_CONFIG


def is_long_definition(definition: str) -> bool:
    """definition が複数行（= 長文 / 関連設定扱い）かどうかを判定する。

    YAML の `|` 記法は末尾改行が付くため、判定前に strip() する。
    """
    normalized = (definition or "").strip()
    return "\n" in normalized


def _resolve_prompt_loader(
    *,
    blocks: dict | None = None,
    memory_manager=None,
    override_config: dict | None = None,
):
    """Build a PromptLoader from per-instance settings without global mutation."""
    from butly_core.prompts import (
        PromptLoader,
        resolve_prompt_locale,
        user_prompt_overrides_enabled,
    )

    config = override_config if isinstance(override_config, dict) else {}
    block_locale = (blocks or {}).get("locale")
    if isinstance(block_locale, str) and block_locale.strip():
        locale = block_locale.strip()
    else:
        agent_profile = config.get("agent_profile") or {}
        legacy_agent = config.get("agent") or {}
        config_locale = agent_profile.get("locale") or legacy_agent.get("locale")
        profile_locale = None
        if (
            not config_locale
            and memory_manager
            and hasattr(memory_manager, "get_agent_profile")
        ):
            profile = memory_manager.get_agent_profile()
            if isinstance(profile, dict):
                profile_locale = profile.get("locale")
        if isinstance(config_locale, str) and config_locale.strip():
            locale = config_locale.strip()
        elif isinstance(profile_locale, str) and profile_locale.strip():
            locale = profile_locale.strip()
        else:
            locale = resolve_prompt_locale(config)

    block_policy = (blocks or {}).get("allow_user_prompt_overrides")
    if isinstance(block_policy, bool):
        allow_user_overrides = block_policy
    else:
        allow_user_overrides = user_prompt_overrides_enabled(config)

    return PromptLoader(
        locale=locale,
        allow_user_overrides=allow_user_overrides,
    )


# セクション順序のデフォルト定義
DEFAULT_CONTEXT_ORDER = {
    "system_instruction": ["system_instruction", "key_memory"],
    "context_prefix": [
        "label_notes",
        "current_time",
        "glossary",
        "mid_term",
        "rag",
        "session_digest",
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
        "session_digest": "high",
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
        "session_digest": "high",
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
        "session_digest": "off",
        "tier_info": "off",
        "web_search": "high",
    },
}


def _normalize_context_key(key: str) -> str:
    """旧 `floating` セクション名を `session_digest` に読み替える。"""
    return "session_digest" if key == "floating" else key


def _normalize_context_list(items: list | None) -> list:
    normalized = [_normalize_context_key(i) for i in (items or [])]
    return list(dict.fromkeys(normalized))


def _normalize_context_levels(levels: dict | None) -> dict:
    out = {}
    for key, value in (levels or {}).items():
        normalized_key = _normalize_context_key(key)
        if normalized_key not in out:
            out[normalized_key] = value
    return out


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

    si_order = _normalize_context_list(
        old.get("system_instruction", DEFAULT_CONTEXT_ORDER["system_instruction"])
    )
    cp_order = _normalize_context_list(
        old.get("context_prefix", DEFAULT_CONTEXT_ORDER["context_prefix"])
    )
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
        return _normalize_context_levels(context_levels.get("levels", {}))
    if context_order:
        all_sections = (
            DEFAULT_CONTEXT_ORDER["system_instruction"]
            + DEFAULT_CONTEXT_ORDER["context_prefix"]
        )
        si_order = _normalize_context_list(context_order.get("system_instruction", []))
        cp_order = _normalize_context_list(context_order.get("context_prefix", []))
        active = set(si_order + cp_order)
        return {s: ("high" if s in active else "off") for s in all_sections}
    return {
        s: "high"
        for s in DEFAULT_CONTEXT_ORDER["system_instruction"]
        + DEFAULT_CONTEXT_ORDER["context_prefix"]
    }


def _resolve_order(
    context_levels: dict | None, context_order: dict | None, key: str
) -> list:
    """セクション順序を解決する。off要素も順序上は保持される。"""
    if context_levels and "order" in context_levels:
        return _normalize_context_list(
            context_levels["order"].get(key, DEFAULT_CONTEXT_ORDER[key])
        )
    if context_order:
        return _normalize_context_list(
            context_order.get(key, DEFAULT_CONTEXT_ORDER[key])
        )
    return DEFAULT_CONTEXT_ORDER[key]


class MemoryBlockBuilder:
    """
    tier に応じて brain.py に渡す記憶ブロック辞書を構築する。

    返却する辞書のキー:
        "short_term"  : 最近の会話履歴（list[dict]）
        "session_digest" : recent sessions 超過分の圧縮ログ（str）
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
        if hasattr(memory_manager, "get_session_digest"):
            session_digest = memory_manager.get_session_digest()
        else:
            session_digest = memory_manager.get_floating_summary()

        prompt_loader = _resolve_prompt_loader(
            memory_manager=memory_manager,
            override_config=override_config,
        )

        # topic を gatekeeper_output から取得
        topic = ""
        if gatekeeper_output:
            topic = gatekeeper_output.get("topic", "")

        blocks = {
            "tier": tier,
            "short_term": short_term_msgs,
            "session_digest": session_digest,
            "mid_term": "",
            "rag_context": "",
            "topic": topic,
            "locale": prompt_loader.locale,
            "allow_user_prompt_overrides": prompt_loader.allow_user_overrides,
            "need": gatekeeper_output.get("need") if gatekeeper_output else None,
            "search_targets": (
                gatekeeper_output.get("search_targets") if gatekeeper_output else None
            ),
        }

        if tier == "reflex":
            # short_term + session_digest（mid_term なし）。
            # RAG / glossary は need に連動する tier 非依存の判定なので
            # ここで return せず後段で処理する（docs の仕様どおり）。
            print("[Gatekeeper] MemoryBlock: reflex（short_term + session_digest）")
        else:
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
                    print(
                        f"[Gatekeeper] MemoryBlock: {tier}（+ digest {len(digest)}文字 + recent_snapshot {len(recent_snapshot)}文字 = {total_chars}文字）"
                    )
                else:
                    # 要約ファイルが存在しない → RAWにフォールバック
                    mid_term = memory_manager.get_raw_memory()
                    blocks["mid_term"] = mid_term
                    blocks["mid_term_mode"] = "raw_fallback"
                    print(
                        f"[Gatekeeper] MemoryBlock: {tier}（要約なし → RAWフォールバック {len(mid_term)}文字）"
                    )
            else:
                # RAWモード: 1_integrated + 2_knowledgeized から読み込み
                mid_term = memory_manager.get_raw_memory()
                blocks["mid_term"] = mid_term
                blocks["mid_term_mode"] = "raw"
                print(
                    f"[Gatekeeper] MemoryBlock: {tier}（+ mid_term RAW {len(mid_term)}文字）"
                )

        # --- RAG: need がある場合、probe candidates から RAG コンテキストを構築 ---
        # tier 非依存（reflex でも need が立てば注入する）
        need = blocks.get("need")
        probe = gatekeeper_output.get("memory_probe", {}) if gatekeeper_output else {}
        candidates = probe.get("candidates", [])
        glossary_hits = probe.get("glossary_hits", [])
        _inst_mem = override_config.get("memory", {}) if override_config else {}
        _sys_mem = SYSTEM_CONFIG.get("memory", {})
        _km_enabled = _inst_mem.get(
            "knowledge_maturation_enabled",
            _sys_mem.get("knowledge_maturation_enabled", False),
        )

        active_node_lookup = {
            "enabled": bool(_km_enabled),
            "attempted": False,
            "candidate_count": len(candidates),
            "matched_count": 0,
            "reason": "disabled",
        }
        if _km_enabled:
            if not need:
                active_node_lookup["reason"] = "need_null"
            elif not candidates:
                active_node_lookup["reason"] = "no_candidates"
            elif brain is None:
                active_node_lookup["reason"] = "brain_unavailable"
            else:
                active_node_lookup["reason"] = "completed"
        blocks["active_node_lookup"] = active_node_lookup

        if glossary_hits:
            blocks["glossary_hits"] = glossary_hits

        # probe が走ったかどうか。走った場合は glossary_hits が空でも全件 fallback しない
        blocks["_probe_ran"] = bool(probe)

        if need and candidates:
            h = prompt_loader.get_section_header

            rag_lines = [h("rag_cards")]
            rag_bullet = h("rag_bullet")
            if any(c.get("source_date") for c in candidates):
                rag_lines.append(h("rag_date_note"))
            for c in candidates:
                # summary は箇条書き・複数行を許容する。継続行はインデントして
                # カード境界（行頭の「・」）と区別できるようにする。
                summary = str(c.get("summary") or "").strip().replace("\n", "\n  ")
                date_prefix = (
                    f"[{c['source_date']}] " if c.get("source_date") else ""
                )
                rag_lines.append(f"{rag_bullet}{date_prefix}{c['title']}: {summary}")
                if c.get("episode"):
                    episode = str(c["episode"]).strip().replace("\n", "\n  ")
                    rag_lines.append(f"  ({h('rag_episode_label')}: {episode})")
            cards_text = "\n".join(rag_lines)

            # --- RAG 注入ソースの解決（cards / raw / both） ---
            # カード = 検索インデックス、事実の根拠 = 原文（parent-document
            # retrieval）。RAW の読み込みは raw を要求するモードのときだけ行う。
            rag_mode = _inst_mem.get(
                "rag_source_mode", _sys_mem.get("rag_source_mode", "cards")
            )
            if rag_mode not in ("cards", "raw", "both"):
                print(
                    f"[Gatekeeper] 不明な rag_source_mode={rag_mode!r} — cards で続行"
                )
                rag_mode = "cards"

            raw_ref = None
            if rag_mode in ("raw", "both") and brain is not None:
                raw_ref = _resolve_raw_block(
                    candidates=candidates,
                    brain=brain,
                    instance_name=instance_name,
                    override_config=override_config,
                    inst_mem=_inst_mem,
                    sys_mem=_sys_mem,
                    locale=prompt_loader.locale,
                )

            if raw_ref:
                if rag_mode == "raw":
                    blocks["rag_context"] = (
                        f"{h('rag_raw')}\n"
                        f"{h('rag_raw_note')}\n"
                        + raw_ref["text"]
                    )
                else:
                    blocks["rag_context"] = (
                        cards_text
                        + f"\n\n{h('rag_source_raw')}\n"
                        f"{h('rag_source_raw_note')}\n"
                        + raw_ref["text"]
                    )
                blocks["rag_raw_reference"] = {
                    "status": "ok",
                    "files": raw_ref["files"],
                    "file_count": len(raw_ref["files"]),
                    "inferred_neighbor_files": raw_ref.get(
                        "inferred_neighbor_files", []
                    ),
                    "inferred_neighbor_file_count": len(
                        raw_ref.get("inferred_neighbor_files", [])
                    ),
                    "missing": raw_ref["missing"],
                    "truncated": raw_ref["truncated"],
                    "chars": raw_ref["chars"],
                    "top_k": raw_ref.get("top_k"),
                    "neighbor_radius": raw_ref.get("neighbor_radius", 0),
                }
                print(
                    f"[Gatekeeper] MemoryBlock: RAG raw reference "
                    f"{len(raw_ref['files'])} files / {raw_ref['chars']} chars "
                    f"(mode={rag_mode}, top_k={raw_ref.get('top_k')}, "
                    f"neighbor_radius={raw_ref.get('neighbor_radius', 0)})"
                )
            else:
                # source_files 無し・ファイル欠落等で解決できない場合はカード注入
                blocks["rag_context"] = cards_text
                if rag_mode != "cards":
                    # raw を要求したのに注入できなかったことを無音にしない
                    # （v10 の Stage 2 無音失敗の教訓）
                    blocks["rag_raw_reference"] = {
                        "status": "fallback_cards",
                        "files": [],
                        "inferred_neighbor_files": [],
                        "inferred_neighbor_file_count": 0,
                        "missing": [],
                        "truncated": False,
                        "chars": 0,
                        "neighbor_radius": 0,
                    }
                    print(
                        "[Gatekeeper] MemoryBlock: RAW 参照を解決できず"
                        f"カード注入にフォールバック (mode={rag_mode})"
                    )
            blocks["rag_source_mode"] = rag_mode
            blocks["rag_card_ids"] = [c["id"] for c in candidates]  # usage_count 用
            blocks["rag_results_raw"] = candidates  # debug_info 用

            # --- Stage 3 opt-in: ヒットしたカードに紐づく active node を併走 ---
            if _km_enabled and brain is not None:
                active_nodes = _lookup_active_nodes_for_candidates(
                    brain=brain,
                    instance_name=instance_name,
                    candidates=candidates,
                )
                active_node_lookup["attempted"] = True
                active_node_lookup["matched_count"] = len(active_nodes)
                if active_nodes:
                    blocks["active_nodes"] = active_nodes

            print(
                f"[Gatekeeper] MemoryBlock: {tier}（RAG probe hits={len(candidates)}）"
            )
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
    loader = _resolve_prompt_loader(
        blocks=blocks,
        memory_manager=memory_manager,
    )
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
        "system_instruction": lambda: _build_si_section(
            sys_inst, si_level, h, agent_profile_header
        ),
        "key_memory": lambda: _build_km_section(key_mem, km_level, h),
    }

    sections = []
    for section_id in order:
        if (
            levels.get(section_id, "high") == "off"
            and section_id != "system_instruction"
        ):
            continue  # off はスキップ。system_instruction は off 不可
        builder = builders.get(section_id)
        if builder:
            result = builder()
            if result:
                sections.append(result)

    return "\n\n".join(sections)


def _build_si_section(
    sys_inst: str, level: str, h, agent_profile_header: str = ""
) -> str | None:
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
        return f"{h('conversation_core')} {key_mem.strip()}"
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
    loader = _resolve_prompt_loader(
        blocks=blocks,
        memory_manager=memory_manager,
    )
    h = loader.get_section_header

    levels = _resolve_levels(context_levels, context_order)
    order = _resolve_order(context_levels, context_order, "context_prefix")
    tier = blocks.get("tier", "mid")

    builders = {
        "label_notes": lambda: _build_label_notes(levels.get("label_notes", "high"), h),
        "current_time": lambda: _build_current_time(
            levels.get("current_time", "high"), h, loader.locale
        ),
        "glossary": lambda: _build_glossary(
            blocks, memory_manager, levels.get("glossary", "high"), h
        ),
        "mid_term": lambda: _build_mid_term(
            blocks, levels.get("mid_term", "high"), tier, h
        ),
        "rag": lambda: _build_rag(blocks, levels.get("rag", "high"), tier, h),
        "session_digest": lambda: _build_session_digest(
            blocks, levels.get("session_digest", "high"), h
        ),
        "floating": lambda: _build_session_digest(
            blocks, levels.get("session_digest", levels.get("floating", "high")), h
        ),
        "tier_info": lambda: _build_tier_info(
            blocks, levels.get("tier_info", "high"), h
        ),
        "google_search": lambda: _build_google_search(
            levels.get("google_search", "high"), use_google_search, h
        ),
        "web_search": lambda: _build_web_search(
            blocks, levels.get("web_search", "high"), h
        ),
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
    return "\n".join(
        (
            h("context_prefix_label"),
            h("memory_usage"),
            h("memory_usage_note"),
        )
    )


def _build_current_time(level: str, h, locale: str = "ja") -> str | None:
    if level == "off":
        return None
    from butly_core.core.chronos import ButlyChronos

    current_time = ButlyChronos(locale=locale).get_system_note()
    if level == "low":
        return current_time.split("\n")[0].strip()  # 時刻のみ1行
    # high / mid
    return f"{h('current_time')}\n{current_time}\n{h('note_current_time')}"


def _build_glossary(blocks: dict, memory_manager, level: str, h) -> str | None:
    """Glossary セクションを組み立てる (Lorebook 統合版)。

    - 短文 (1行 definition) → "用語説明" セクション
    - 長文 (複数行 definition) → "関連設定" セクション
    - ソート: (priority ASC, _yaml_index ASC) で安定
    - 上限: max_entries（件数）+ max_chars（合計 definition 長、greedy skip）
    - level: high/mid = 注入、low/off = 注入なし (既存挙動維持)
    """
    if level in ("off", "low"):
        return None

    glossary_conf = dict(SYSTEM_CONFIG.get("glossary", {}))
    glossary_conf.update(blocks.get("glossary_config", {}) or {})
    max_entries = int(glossary_conf.get("max_entries", 20))
    max_chars = int(glossary_conf.get("max_chars", 4000))

    glossary_hits = blocks.get("glossary_hits", [])

    # --- probe ヒットがあれば優先（履歴スキャン込みの選別済み） ---
    if glossary_hits:
        short_hits, long_hits = _split_short_long(glossary_hits)
        ordered = _sort_hits(short_hits) + _sort_hits(long_hits)
        selected = _apply_caps(ordered, max_entries, max_chars)
        return _render_glossary(selected, h)

    # --- probe が走った上でヒット 0 → 注入なし (意図された挙動) ---
    if blocks.get("_probe_ran"):
        return None

    # --- フォールバック: 全件注入 (probe 未実行ケースのみ。例: Gatekeeper 無効時) ---
    if not memory_manager:
        return None
    glossary_text = memory_manager.get_glossary()
    if glossary_text:
        return f"{h('glossary')}\n" f"{h('note_glossary')}\n" f"{glossary_text}"
    return None


def _split_short_long(hits: list) -> tuple:
    """hits を短文 / 長文 に振り分ける。"""
    short_hits = []
    long_hits = []
    for hit in hits:
        if is_long_definition(hit.get("definition", "")):
            long_hits.append(hit)
        else:
            short_hits.append(hit)
    return short_hits, long_hits


def _sort_hits(hits: list) -> list:
    """(priority ASC, _yaml_index ASC) で安定ソート。

    priority / _yaml_index が無いエントリ（フォールバック等）はそれぞれ 100 / 0 として扱う。
    """
    return sorted(
        hits,
        key=lambda h: (int(h.get("priority", 100)), int(h.get("_yaml_index", 0))),
    )


def _apply_caps(ordered_hits: list, max_entries: int, max_chars: int) -> list:
    """max_entries（件数）+ max_chars（合計 definition 長）の greedy skip を適用。

    順序は維持。総量を超過するエントリは個別にスキップして次へ進む。
    """
    selected = []
    total = 0
    for entry in ordered_hits:
        if len(selected) >= max_entries:
            break
        cost = len((entry.get("definition") or "").strip())
        if total + cost > max_chars:
            continue
        selected.append(entry)
        total += cost
    return selected


def _render_glossary(selected: list, h) -> str | None:
    """選別済み hits をフォーマット済み文字列に変換する。"""
    if not selected:
        return None

    short_hits, long_hits = _split_short_long(selected)

    sections = [h("glossary"), h("note_glossary")]

    if short_hits:
        short_label = h("glossary_short_label")
        lines = [short_label]
        for hit in short_hits:
            term = hit.get("term", "")
            definition = (hit.get("definition") or "").strip()
            lines.append(f"- {term}: {definition}")
        sections.append("\n".join(lines))

    if long_hits:
        long_label = h("glossary_long_label")
        lines = [long_label]
        for hit in long_hits:
            term = hit.get("term", "")
            definition = (hit.get("definition") or "").strip()
            lines.append(f"### {term}")
            lines.append(definition)
            lines.append("")  # エントリ間の空行
        # 末尾の空行を除去
        while lines and lines[-1] == "":
            lines.pop()
        sections.append("\n".join(lines))

    return "\n\n".join(sections)


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
        lines = [ln for ln in text.strip().split("\n") if ln.strip()]
        truncated = lines[-4:] if len(lines) > 4 else lines
        return f"{h('recent_context')}\n" + "\n".join(truncated)

    # high / mid
    parts = []
    if mid_term_mode == "summary":
        digest = blocks.get("mid_term_digest", "")
        recent_snapshot = blocks.get("mid_term_recent_snapshot", "")
        if digest:
            parts.append(
                f"{h('mid_term_digest')}\n" f"{h('note_mid_term_digest')}\n" f"{digest}"
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


def _node_slots(node: dict) -> str:
    """active node の subject / topic を ``[主語 | トピック] `` として前置する。

    statement だけを出すと「誰の話か」が本文に書かれていないノードが主語なし
    の事実として読まれ、別人の質問へ流用される（LoCoMo cat5 の典型的な誤答）。
    subject/topic は memory_nodes に既にあるので、注入時に落とさず出す。
    ラベル語を置かないのは locale 非依存にするため。空欄は詰める。
    """
    slots = [
        str(node.get(key) or "").strip().replace("\n", " ")
        for key in ("subject", "topic")
    ]
    filled = [s for s in slots if s]
    return f"[{' | '.join(filled)}] " if filled else ""


def _build_rag(blocks: dict, level: str, tier: str, h) -> str | None:
    # RAG は need 連動の tier 非依存注入（reflex でも rag_context があれば描画）
    if level == "off":
        return None
    rag_context = blocks.get("rag_context", "")
    if not rag_context:
        return None
    if level == "low":
        lines = [ln for ln in rag_context.strip().split("\n") if ln.strip()]
        truncated = lines[:3] if len(lines) > 3 else lines
        return f"{h('related_memory')}\n" + "\n".join(truncated)
    # high / mid
    body = f"{h('long_term_memory')}\n{h('note_rag')}\n{rag_context}"

    # Stage 3: active node の小量併記 (opt-in)
    active_nodes = blocks.get("active_nodes") or []
    if active_nodes:
        node_lines = []
        for n in active_nodes[:5]:
            statement = (n.get("statement") or "").strip().replace("\n", " ")
            if not statement:
                continue
            confidence = n.get("confidence")
            conf_str = f" (conf={confidence:.2f})" if isinstance(confidence, (int, float)) else ""
            node_lines.append(
                f"{h('rag_bullet')}{_node_slots(n)}{statement}{conf_str}"
            )
        if node_lines:
            body += (
                f"\n{h('active_nodes')}\n{h('note_active_nodes')}\n"
                + "\n".join(node_lines)
            )
    return body


def _lookup_active_nodes_for_candidates(
    *, brain, instance_name: str, candidates: list
) -> list[dict]:
    """RAG candidates に紐づく status='active' node を集めて重複除去して返す。"""
    import sqlite3
    try:
        from butly_core.core.memory_nodes import MemoryNodeRepository
    except Exception:
        return []

    if not candidates:
        return []

    out: dict[str, dict] = {}
    # source_instance ごとに DB を切り替えて検索する
    by_inst: dict[str, list[str]] = {}
    for c in candidates:
        cid = c.get("id")
        if not cid:
            continue
        src = c.get("source_instance") or instance_name
        by_inst.setdefault(src, []).append(cid)

    for src, card_ids in by_inst.items():
        try:
            db_path = brain._get_db_path(src)
        except Exception:
            continue
        if not db_path.exists():
            continue
        try:
            repo = MemoryNodeRepository(str(db_path))
            for cid in card_ids:
                for node in repo.find_active_nodes_for_card(cid):
                    nid = node.get("id")
                    if not nid:
                        continue
                    if nid not in out:
                        observed_node = dict(node)
                        observed_node["source_instance"] = src
                        observed_node["matched_card_ids"] = [cid]
                        out[nid] = observed_node
                    elif cid not in out[nid]["matched_card_ids"]:
                        out[nid]["matched_card_ids"].append(cid)
        except sqlite3.OperationalError:
            # memory_nodes テーブル未作成 (Stage 3 未実行) — 静かにスキップ
            continue
        except Exception as e:
            print(f"[Gatekeeper] active_nodes lookup skipped ({src}): {e}")

    # confidence 降順で並べ替えて返す
    nodes = list(out.values())
    nodes.sort(key=lambda n: float(n.get("confidence") or 0.0), reverse=True)
    return nodes


def _resolve_raw_block(
    *,
    candidates: list,
    brain,
    instance_name: str,
    override_config: dict | None,
    inst_mem: dict,
    sys_mem: dict,
    locale: str = "ja",
) -> dict | None:
    """RAG 候補カードの RAW 原文抜粋を解決する（失敗時 None でカード注入へ）。

    話者ラベルは sleeptime Stage 2 と同じ規則（AI 名 = agent_profile.ai_name、
    ユーザー名 = preferred_call 優先）で解決する。
    """
    from butly_core.core.gatekeeper.raw_reference import resolve_raw_reference

    max_chars = int(
        inst_mem.get("rag_raw_max_chars", sys_mem.get("rag_raw_max_chars", 2500))
    )
    top_k = inst_mem.get("rag_raw_top_k", sys_mem.get("rag_raw_top_k", 1))
    try:
        top_k = int(top_k)
    except (TypeError, ValueError):
        top_k = 1
    neighbor_radius = inst_mem.get(
        "rag_raw_neighbor_radius",
        sys_mem.get("rag_raw_neighbor_radius", 0),
    )
    try:
        neighbor_radius = max(0, int(neighbor_radius))
    except (TypeError, ValueError):
        neighbor_radius = 0
    agent_conf = SYSTEM_CONFIG.get("agent", {})
    conf = override_config or {}
    agent_name = (conf.get("agent_profile") or {}).get("ai_name") or agent_conf.get(
        "agent_name", "AI"
    )
    user_profile = conf.get("user_profile") or {}
    user_name = (
        user_profile.get("preferred_call")
        or user_profile.get("user_name")
        or agent_conf.get("user_name", "User")
    )
    try:
        return resolve_raw_reference(
            candidates,
            brain.instances_dir,
            instance_name,
            max_chars,
            user_name=user_name,
            agent_name=agent_name,
            locale=locale,
            top_k=top_k,
            neighbor_radius=neighbor_radius,
        )
    except Exception as e:
        print(f"[Gatekeeper] RAW 参照の解決に失敗: {e}")
        return None


def _build_session_digest(blocks: dict, level: str, h) -> str | None:
    if level in ("off", "low"):
        return None
    session_digest = blocks.get("session_digest") or blocks.get("floating", "")
    if not session_digest:
        return None
    return (
        f"{h('session_digest')}\n"
        f"{h('note_session_digest')}\n"
        f"{session_digest.strip()}"
    )


def _build_tier_info(blocks: dict, level: str, h) -> str | None:
    if level in ("off", "low"):
        return None
    tier = blocks.get("tier", "mid")
    tier_text = h("tier_mode").format(tier=tier)
    topic = blocks.get("topic", "")
    if tier in ("mid",) and topic:
        tier_text += "\n" + h("tier_topic").format(topic=topic)
    return f"{h('tier_info')}\n{tier_text}"


def _build_google_search(level: str, use_google_search: bool, h) -> str | None:
    if level == "off" or not use_google_search:
        return None
    return h("note_google_search")


def _build_web_search(blocks: dict, level: str, h) -> str | None:
    if level == "off":
        return None
    web_search_ctx = blocks.get("web_search_context", "")
    if not web_search_ctx:
        return None
    if level == "low":
        return web_search_ctx.strip()[:300]
    # high / mid
    return f"{h('web_search')}\n{h('web_search_note')}\n{web_search_ctx}"
