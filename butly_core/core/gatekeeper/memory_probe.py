"""
memory_probe.py
---------------
事実ベースの記憶検索プローブ。
LLM 呼び出しなし。実際の検索結果に基づいて判定する。

Layer 1: Quick Vector Search (50-100ms)
  user_input の embedding → cosine similarity で上位 N 件取得

Layer 1.5: Glossary Match (数ms)
  user_input の単語 → glossary entries の term/aliases とマッチ

Layer 2: Deep Search (1-2s, 条件付き)
  Layer 1 でヒットなし、かつ具体的な過去参照がある場合のみ
"""

import re
import time
from pathlib import Path

from butly_core.config import SYSTEM_CONFIG


def should_deep_search(user_input, layer1_hits, headline_match, glossary_match):
    """Layer 1 でヒットなし時に deep search を実行すべきか判定する。"""
    if layer1_hits:
        return False

    if headline_match or glossary_match:
        if not asks_for_specific_past_detail(user_input):
            return False

    if asks_for_specific_past_detail(user_input):
        return True

    return False


def asks_for_specific_past_detail(user_input):
    """ユーザー発言に具体的な過去参照パターンが含まれるか判定する。"""
    # 明示的な過去参照パターン（日本語）
    ja_patterns = [
        "前に",
        "以前",
        "あの時",
        "あのとき",
        "この前",
        "昔",
        "前回",
        "前話した",
        "覚えてる",
    ]
    # 明示的な過去参照パターン（英語）
    en_patterns = [
        "before",
        "remember",
        "last time",
        "we discussed",
        "we talked about",
        "you mentioned",
    ]
    input_lower = user_input.lower()
    for marker in ja_patterns + en_patterns:
        if marker in input_lower:
            return True

    # 「〜だっけ？」「〜でしたっけ」「〜どうなった？」パターン
    if re.search(r"(だっけ|でしたっけ|どうなった|どうした|の件)", user_input):
        return True

    # 過去の出来事の日付・時点を尋ねる疑問文。
    # "what time" は現在時刻の質問（What time is it?）を拾わないよう過去形限定。
    if re.search(r"\bwhen\s+(did|was|were)\b", input_lower):
        return True
    if re.search(r"\bwhat\s+(date|day|year)\b", input_lower):
        return True
    if re.search(r"\bwhat\s+time\s+(did|was|were)\b", input_lower):
        return True
    # 「いつ〜した/だった」系。「いつも」「いつか」「いつの間に」は除外
    if re.search(r"いつ(?!も|か|の間).{0,15}(した|った|でした|ました)", user_input):
        return True

    return False


class MemoryProbe:
    """
    事実ベースの記憶検索プローブ。
    LLM 呼び出しなし。実際の検索結果に基づいて判定する。
    """

    def __init__(self, base_dir: Path = None):
        self.base_dir = base_dir

    def probe(
        self,
        user_input: str,
        brain,
        memory_manager,
        instance_name: str = "00_master",
        recent_headlines: str = "",
        override_config: dict = None,
        history_msgs: list = None,
        need_intent: str | None = None,
    ) -> dict:
        """
        Returns:
            {
                "status": "hit" | "no_hit" | "deep_search" | "skipped",
                "candidates": [...],
                "glossary_hits": [...],
            }

        Parameters
        ----------
        history_msgs : list | None
            直近の会話履歴。glossary 履歴スキャンに使用。
        need_intent : str | None
            ContextClassifier が出した意図種別。
              - None              : Layer 1.5 (glossary) のみ実行。vector/deep スキップ
              - "glossary"        : Layer 1.5 のみ実行 (vector skip)
              - "past_fact"       : Layer 1 (vector) + 1.5 + 条件付き Layer 2
              - "relationship"    : 同上

        Note
        ----
        Glossary scan は regex のみで LLM 不要・~ms オーダーなので、
        need_intent に関わらず常時実行する。
        """
        t0 = time.time()
        layers: dict = {}

        # Layer 1.5: Glossary Match (常時実行 — LLM 不要で軽量)
        glossary_hits = (
            self._match_glossary(
                user_input,
                memory_manager,
                history_msgs=history_msgs,
                override_config=override_config,
            )
            if memory_manager is not None
            else []
        )
        layers["glossary"] = {
            "executed": memory_manager is not None,
            "matches": len(glossary_hits),
        }

        # need_intent が無い or "glossary" → Layer 1.5 のみで終了
        if need_intent is None or need_intent == "glossary":
            t1 = time.time()
            status = "hit" if glossary_hits else "no_hit"
            tag = "intent=None" if need_intent is None else "glossary-only"
            print(
                f"[MemoryProbe] {tag}: glossary={len(glossary_hits)} "
                f"({int((t1-t0)*1000)}ms)"
            )
            return {
                "status": status,
                "candidates": [],
                "glossary_hits": glossary_hits,
                "layers": layers,
            }

        # past_fact / relationship: brain が無ければ glossary だけで返す
        if brain is None:
            t1 = time.time()
            status = "hit" if glossary_hits else "no_hit"
            layers["vector"] = {"executed": False, "reason": "no brain"}
            print(
                f"[MemoryProbe] no brain (intent={need_intent}): "
                f"glossary={len(glossary_hits)} ({int((t1-t0)*1000)}ms)"
            )
            return {
                "status": status,
                "candidates": [],
                "glossary_hits": glossary_hits,
                "layers": layers,
            }

        probe_conf = SYSTEM_CONFIG.get("memory_probe", {})
        vector_limit = probe_conf.get("vector_search_limit", 3)
        vector_threshold = probe_conf.get("vector_search_threshold", 0.4)
        deep_enabled = probe_conf.get("deep_search_enabled", True)

        # Layer 1: Quick Vector Search (past_fact / relationship)
        v_diag = self._quick_vector_search_diag(
            user_input,
            brain,
            instance_name,
            limit=vector_limit,
            threshold=vector_threshold,
            override_config=override_config,
        )
        candidates = v_diag["results"]
        layers["vector"] = {
            "executed": True,
            **v_diag.get("diagnostics", {}),
            "result_count": len(candidates),
        }

        t1 = time.time()

        # Layer 1 でヒットあり → 即返却
        if candidates:
            print(
                f"[MemoryProbe] Layer 1 hit (intent={need_intent}): "
                f"{len(candidates)} candidates, "
                f"glossary={len(glossary_hits)} ({int((t1-t0)*1000)}ms)"
            )
            return {
                "status": "hit",
                "candidates": candidates,
                "glossary_hits": glossary_hits,
                "layers": layers,
            }

        # Layer 2 トリガー判定
        if not deep_enabled:
            layers["deep"] = {"executed": False, "reason": "disabled"}
            print(
                f"[MemoryProbe] no_hit (intent={need_intent}, deep_search disabled), "
                f"glossary={len(glossary_hits)} ({int((t1-t0)*1000)}ms)"
            )
            return {
                "status": "no_hit",
                "candidates": [],
                "glossary_hits": glossary_hits,
                "layers": layers,
            }

        headline_match = self._check_headline_match(user_input, recent_headlines)

        if should_deep_search(
            user_input, candidates, headline_match, bool(glossary_hits)
        ):
            deep_data = self._deep_search_diag(
                user_input, brain, instance_name, override_config
            )
            deep_candidates = deep_data["results"]
            layers["deep"] = {
                "executed": True,
                "trigger": "past_ref_pattern",
                "keywords": deep_data.get("keywords", []),
                "result_count": len(deep_candidates),
            }
            t2 = time.time()
            if deep_candidates:
                print(
                    f"[MemoryProbe] deep_search hit (intent={need_intent}): "
                    f"{len(deep_candidates)} candidates, "
                    f"glossary={len(glossary_hits)} ({int((t2-t0)*1000)}ms)"
                )
                return {
                    "status": "deep_search",
                    "candidates": deep_candidates,
                    "glossary_hits": glossary_hits,
                    "layers": layers,
                }
            print(
                f"[MemoryProbe] deep_search no_hit (intent={need_intent}), "
                f"glossary={len(glossary_hits)} ({int((t2-t0)*1000)}ms)"
            )
        else:
            layers["deep"] = {"executed": False, "reason": "no past_ref_pattern"}
            print(
                f"[MemoryProbe] no_hit (intent={need_intent}, no deep_search trigger), "
                f"glossary={len(glossary_hits)} ({int((t1-t0)*1000)}ms)"
            )

        return {
            "status": "no_hit",
            "candidates": [],
            "glossary_hits": glossary_hits,
            "layers": layers,
        }

    def _quick_vector_search(
        self,
        user_input,
        brain,
        instance_name,
        limit=3,
        threshold=0.6,
        override_config=None,
    ) -> list:
        """Layer 1: キーワード抽出なしの純粋なベクトル検索。"""
        return self._quick_vector_search_diag(
            user_input,
            brain,
            instance_name,
            limit,
            threshold,
            override_config,
        )["results"]

    def _quick_vector_search_diag(
        self,
        user_input,
        brain,
        instance_name,
        limit=3,
        threshold=0.6,
        override_config=None,
    ) -> dict:
        """Layer 1 + 診断情報。Returns {"results": [...], "diagnostics": {...}}"""
        try:
            return brain.quick_vector_search_diag(
                user_input,
                instance_name,
                limit=limit,
                threshold=threshold,
                override_config=override_config,
            )
        except Exception as e:
            print(f"[MemoryProbe] Quick vector search error: {e}")
            return {"results": [], "diagnostics": {"error": str(e)}}

    def _match_glossary(
        self,
        user_input,
        memory_manager,
        history_msgs: list = None,
        override_config: dict = None,
    ) -> list:
        """
        Layer 1.5: user_input + 直近履歴に含まれる glossary エントリをマッチングする。

        フィルタ・ソート・整形は行わない。raw hits を返却し、
        priority / max_entries / max_chars 等は呼び出し側 (_build_glossary) で処理する。

        各 hit は以下の dict:
            {
                "term": str,
                "definition": str,
                "aliases": list,
                "match_type": "term" | "alias",
                "match_source": "user" | "history",
                "priority": int,            # 未指定は 100
                "_yaml_index": int,         # YAML 内の元の位置 (安定ソート用)
            }
        重複 term は 1 件に集約。match_source は user_input ヒット優先。
        """
        try:
            glossary_data = memory_manager.get_glossary_raw()
        except Exception:
            return []

        entries = glossary_data.get("entries", [])
        if not entries:
            return []

        # --- 設定解決 ---
        glossary_conf = dict(SYSTEM_CONFIG.get("glossary", {}))
        if override_config:
            glossary_conf.update(override_config.get("glossary", {}))
        scan_depth = int(glossary_conf.get("scan_depth", 2))
        scan_target = glossary_conf.get("scan_target", "both")

        # --- スキャン対象テキスト構築 ---
        # user_input は常に「user」ソース扱い
        sources: list[tuple[str, str]] = [("user", user_input or "")]

        if history_msgs and scan_depth > 0:
            history_text_pairs = self._extract_history_text(
                history_msgs, scan_depth, scan_target
            )
            sources.extend(history_text_pairs)

        # --- マッチング ---
        # term 単位で集約。user ソース優先（後勝ちで上書きしない）
        hits_by_term: dict = {}
        for entry_idx, entry in enumerate(entries):
            if entry.get("status") != "active":
                continue

            term = entry.get("term", "")
            aliases = entry.get("aliases", []) or []
            definition = entry.get("definition", "")
            priority = int(entry.get("priority", 100))

            term_lower = term.lower()
            alias_lower = [a.lower() for a in aliases]

            matched_source = None
            matched_type = None

            for source_label, text in sources:
                if not text:
                    continue
                text_lower = text.lower()

                if term_lower and term_lower in text_lower:
                    matched_source = source_label
                    matched_type = "term"
                    break

                hit_alias = False
                for al in alias_lower:
                    if al and al in text_lower:
                        hit_alias = True
                        break
                if hit_alias:
                    matched_source = source_label
                    matched_type = "alias"
                    break

            if not matched_source:
                continue

            # 既存 hit があれば、user ソースを優先して上書き
            existing = hits_by_term.get(term)
            if existing is not None:
                if existing["match_source"] == "user":
                    continue  # すでに user 由来 → 何もしない
                # 既存が history で新規が user の場合のみ上書き
                if matched_source != "user":
                    continue

            hits_by_term[term] = {
                "term": term,
                "definition": definition,
                "aliases": aliases,
                "match_type": matched_type,
                "match_source": matched_source,
                "priority": priority,
                "_yaml_index": entry_idx,
            }

        return list(hits_by_term.values())

    def _extract_history_text(
        self,
        history_msgs: list,
        scan_depth: int,
        scan_target: str,
    ) -> list:
        """
        履歴から直近 scan_depth ペア分のメッセージを取り出して
        [(source_label, text), ...] のリストを返す。

        1 ターン = user+assistant 1 ペア (= 最大 2 メッセージ)。
        scan_depth=2 → 直近 4 メッセージから scan_target に合うものだけ抽出。
        """
        if not history_msgs or scan_depth <= 0:
            return []

        max_msgs = scan_depth * 2
        recent = history_msgs[-max_msgs:]

        result = []
        for m in recent:
            role = m.get("role", "")
            # role は "user" / "assistant" / "model" 等
            if role in ("assistant", "model"):
                normalized_role = "assistant"
            elif role == "user":
                normalized_role = "user"
            else:
                continue

            if scan_target == "user" and normalized_role != "user":
                continue
            if scan_target == "assistant" and normalized_role != "assistant":
                continue

            # parts (Gemini 風) と content (OpenAI 風) の両方に対応
            text = ""
            if "parts" in m:
                parts = m.get("parts", [])
                if parts:
                    first = parts[0]
                    if isinstance(first, dict):
                        text = first.get("text", "")
                    else:
                        text = str(first)
            elif "content" in m:
                content = m.get("content", "")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list) and content:
                    first = content[0]
                    if isinstance(first, dict):
                        text = first.get("text", "")

            if text:
                result.append(("history", text))

        return result

    def _check_headline_match(self, user_input, recent_headlines) -> bool:
        """recent_headlines のキーワードと user_input の単語がマッチするか。"""
        if not recent_headlines or recent_headlines == "(no recent headlines)":
            return False

        headline_words = set()
        for line in recent_headlines.split("\n"):
            match = re.search(r"\]\s*(.+)", line)
            if match:
                headline_words.update(match.group(1).split())

        if not headline_words:
            return False

        input_words = set(user_input.split())
        overlap = headline_words & input_words

        return len(overlap) >= 1

    def _deep_search(
        self, user_input, brain, instance_name, override_config=None
    ) -> list:
        """後方互換: 結果リストのみ。"""
        return self._deep_search_diag(
            user_input, brain, instance_name, override_config
        )["results"]

    def _deep_search_diag(
        self, user_input, brain, instance_name, override_config=None
    ) -> dict:
        """Layer 2 + 診断情報。Returns {"results": [...], "keywords": [...]}"""
        try:
            keyword_data = brain.extract_keywords(user_input, override_config)
            keywords = keyword_data.get("keywords", [])
            if not keywords:
                return {"results": [], "keywords": []}

            limit = SYSTEM_CONFIG["brain"]["search_limit"]
            results = brain.search_knowledge(
                keywords,
                user_input,
                instance_name=instance_name,
                limit=limit,
                override_config=override_config,
            )
            for r in results:
                r["source"] = "keyword"
            return {"results": results, "keywords": keywords}
        except Exception as e:
            print(f"[MemoryProbe] Deep search error: {e}")
            return {"results": [], "keywords": []}
