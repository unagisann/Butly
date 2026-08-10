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


def _resolve_section(section: str, override_config: dict | None) -> dict:
    """SYSTEM_CONFIG[section] を instance/profile 設定で上書きして返す。

    memory / brain セクションと同じ上書き規則を probe 側にも揃える
    （従来は SYSTEM_CONFIG 直読みで instance ごとの調整ができなかった）。
    """
    conf = dict(SYSTEM_CONFIG.get(section, {}))
    if override_config:
        overrides = override_config.get(section)
        if isinstance(overrides, dict):
            conf.update(overrides)
    return conf


def should_deep_search(
    user_input, layer1_hits, headline_match, glossary_match, need_intent=None
):
    """Layer 1 でヒットなし時に deep search を実行すべきか判定する。

    LLM が past_fact と判定した場合は正規表現パターン一致を要求しない。
    パターンは need_intent が信頼できない時（fallback 経路）の安全網であり、
    LLM が意味的に拾ったケースを二重ゲートで潰さない。
    relationship は常時 Deep に送るか未判断のため、従来どおりパターン必須
    （Trace でヒット率と遅延を確認してから判断する）。
    """
    if layer1_hits:
        return False

    explicit_ask = (
        need_intent == "past_fact" or asks_for_specific_past_detail(user_input)
    )

    if headline_match or glossary_match:
        if not explicit_ask:
            return False

    return explicit_ask


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

    # 以前語られた予定・約束の時期を尋ねる疑問文（"When is X planning to...?"）。
    # 予定を表す明示語を要求し、"When is the meeting?" のような一般疑問での
    # 過剰発火を避ける。floor は probe 候補ゼロなら need=null に戻るので安全側。
    if re.search(
        r"\bwhen\s+(is|are|will)\b.*\b(plan|plans|planning|planned|scheduled)\b",
        input_lower,
    ):
        return True
    # 「いつ〜する予定/つもり」系
    if re.search(r"いつ(?!も|か|の間).{0,20}(予定|つもり)", user_input):
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
        retrieval_query: str | None = None,
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

        probe_conf = _resolve_section("memory_probe", override_config)
        execution_policy = probe_conf.get("retrieval_execution", "always")
        injection_policy = probe_conf.get("injection_policy", "intent_gated")
        intent_wants_memory = need_intent in ("past_fact", "relationship")

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

        retrieval: dict = {
            "execution_policy": execution_policy,
            "injection_policy": injection_policy,
            "executed": False,
            "reason": None,
            "candidate_count": 0,
            "candidate_ids": [],
            "injection_allowed": False,
            "injection_reason": "no_candidates",
        }

        # 検索の実行と、結果をプロンプトへ入れるかは別判断（計画書 §3.3）。
        # retrieval_execution="always" では分類器の結果に関わらず検索する。
        if brain is None:
            retrieval["reason"] = "no_brain"
            layers["vector"] = {"executed": False, "reason": "no brain"}
        elif execution_policy != "always" and not intent_wants_memory:
            retrieval["reason"] = f"intent_gated:{need_intent}"
            layers["vector"] = {"executed": False, "reason": "intent gated"}

        if retrieval["reason"] is not None:
            t1 = time.time()
            status = "hit" if glossary_hits else "no_hit"
            print(
                f"[MemoryProbe] retrieval skipped ({retrieval['reason']}): "
                f"glossary={len(glossary_hits)} ({int((t1-t0)*1000)}ms)"
            )
            return {
                "status": status,
                "candidates": [],
                "glossary_hits": glossary_hits,
                "layers": layers,
                "retrieval": retrieval,
            }

        vector_limit = probe_conf.get("vector_search_limit", 3)
        vector_threshold = probe_conf.get("vector_search_threshold", 0.4)
        deep_enabled = probe_conf.get("deep_search_enabled", True)

        # Layer 1: Quick Retrieval（search_mode に応じて
        # vector / hybrid / dual_query）
        v_diag = self._quick_vector_search_diag(
            user_input,
            brain,
            instance_name,
            limit=vector_limit,
            threshold=vector_threshold,
            override_config=override_config,
            retrieval_query=retrieval_query,
        )
        candidates = v_diag["results"]
        diagnostics = v_diag.get("diagnostics", {})
        layers["vector"] = {
            "executed": True,
            **diagnostics,
            "result_count": len(candidates),
        }
        retrieval["executed"] = True
        retrieval["mode"] = diagnostics.get("mode", "vector")
        retrieval["latency_ms"] = diagnostics.get("latency_ms")
        retrieval["source"] = "vector"

        t1 = time.time()
        status = "no_hit"
        reranker_diag = diagnostics.get("reranker") or {}
        reranker_abstained = (
            reranker_diag.get("engine") == "cross_encoder"
            and reranker_diag.get("status") == "completed"
            and reranker_diag.get("selected_count") == 0
            and reranker_diag.get("score_threshold") is not None
        )

        if candidates:
            status = "hit"
            print(
                f"[MemoryProbe] Layer 1 hit (intent={need_intent}): "
                f"{len(candidates)} candidates, "
                f"glossary={len(glossary_hits)} ({int((t1-t0)*1000)}ms)"
            )
        elif reranker_abstained:
            layers["deep"] = {
                "executed": False,
                "reason": "reranker_abstained",
            }
            print(
                f"[MemoryProbe] no_hit (intent={need_intent}, reranker "
                f"abstained), glossary={len(glossary_hits)} "
                f"({int((t1-t0)*1000)}ms)"
            )
        elif not deep_enabled or not (
            intent_wants_memory
            or injection_policy in ("retrieval_assisted", "candidates")
        ):
            # Layer 2 は LLM 呼び出し（vector モードのキーワード抽出）を伴うので、
            # 「検索しても注入され得ない」ケースでは走らせない。常時検索は
            # 軽い Layer 1 までに留める。
            reason = "disabled" if not deep_enabled else "injection_gated"
            layers["deep"] = {"executed": False, "reason": reason}
            print(
                f"[MemoryProbe] no_hit (intent={need_intent}, deep {reason}), "
                f"glossary={len(glossary_hits)} ({int((t1-t0)*1000)}ms)"
            )
        else:
            headline_match = self._check_headline_match(user_input, recent_headlines)
            if should_deep_search(
                user_input,
                candidates,
                headline_match,
                bool(glossary_hits),
                need_intent=need_intent,
            ):
                deep_data = self._deep_search_diag(
                    user_input, brain, instance_name, override_config
                )
                deep_candidates = deep_data["results"]
                layers["deep"] = {
                    "executed": True,
                    "trigger": (
                        "past_ref_pattern"
                        if asks_for_specific_past_detail(user_input)
                        else "llm_intent"
                    ),
                    "keywords": deep_data.get("keywords", []),
                    "result_count": len(deep_candidates),
                }
                t2 = time.time()
                if deep_candidates:
                    candidates = deep_candidates
                    status = "deep_search"
                    retrieval["source"] = "deep"
                    print(
                        f"[MemoryProbe] deep_search hit (intent={need_intent}): "
                        f"{len(deep_candidates)} candidates, "
                        f"glossary={len(glossary_hits)} ({int((t2-t0)*1000)}ms)"
                    )
                else:
                    print(
                        f"[MemoryProbe] deep_search no_hit (intent={need_intent}), "
                        f"glossary={len(glossary_hits)} ({int((t2-t0)*1000)}ms)"
                    )
            else:
                layers["deep"] = {"executed": False, "reason": "no deep trigger"}
                print(
                    f"[MemoryProbe] no_hit (intent={need_intent}, "
                    f"no deep_search trigger), glossary={len(glossary_hits)} "
                    f"({int((t1-t0)*1000)}ms)"
                )

        retrieval["candidate_count"] = len(candidates)
        retrieval["candidate_ids"] = [str(c.get("id")) for c in candidates]
        retrieval["fused_candidate_ids"] = diagnostics.get("fused_candidate_ids", [])
        retrieval["effective_candidate_ids"] = diagnostics.get(
            "effective_candidate_ids",
            diagnostics.get("fused_candidate_ids", []),
        )
        retrieval["reranked_candidate_ids"] = diagnostics.get(
            "reranked_candidate_ids", []
        )
        retrieval["reranker"] = diagnostics.get("reranker")
        retrieval["vector_candidate_ids"] = diagnostics.get("vector_candidate_ids", [])
        retrieval["original_candidate_ids"] = diagnostics.get(
            "original_candidate_ids", []
        )
        retrieval["retrieval_query_candidate_ids"] = diagnostics.get(
            "retrieval_query_candidate_ids", []
        )
        retrieval["retrieval_query"] = diagnostics.get("retrieval_query")
        retrieval["query_fusion"] = diagnostics.get("query_fusion")
        retrieval["bm25_candidate_ids"] = diagnostics.get("bm25_candidate_ids", [])
        retrieval["retrieval_sources"] = diagnostics.get("retrieval_sources")
        bm25_diags = list((diagnostics.get("bm25") or {}).values())
        if bm25_diags:
            retrieval["bm25_terms"] = sorted(
                {t for d in bm25_diags for t in (d.get("terms") or [])}
            )
            retrieval["short_term_hits"] = sum(
                int(d.get("short_term_hits") or 0) for d in bm25_diags
            )
            retrieval["weak_terms"] = sorted(
                {t for d in bm25_diags for t in (d.get("weak_terms") or [])}
            )

        allowed, reason = self._resolve_injection(
            candidates,
            injection_policy=injection_policy,
            intent_wants_memory=intent_wants_memory,
        )
        retrieval["injection_allowed"] = allowed
        retrieval["injection_reason"] = reason
        if allowed and not intent_wants_memory:
            # 分類器が拾えなかったが検索根拠が強い場合の need（gatekeeper が使う）
            retrieval["need_hint"] = "past_fact"

        if not allowed:
            # 検索は走ったが注入しない場合の status は従来（検索を実行しなかった
            # 頃）と同じ意味に保つ。status を見ている trace / debug の互換のため。
            status = "hit" if glossary_hits else "no_hit"

        return {
            "status": status,
            "candidates": candidates if allowed else [],
            "retrieved_candidates": candidates,
            "glossary_hits": glossary_hits,
            "layers": layers,
            "retrieval": retrieval,
        }

    @staticmethod
    def _resolve_injection(
        candidates: list,
        *,
        injection_policy: str,
        intent_wants_memory: bool,
    ) -> tuple:
        """検索結果をプロンプトへ注入してよいかを判定する（計画書 §3.3）。

        - ``intent_gated``: 従来どおり分類器の判定に従う
        - ``retrieval_assisted``: ベクトルと BM25 の双方が同じカードを支持した
          ときだけ昇格させる（hybrid 専用。vector では発火しない）
        - ``candidates``: 候補があれば注入する

        ``candidates`` が乱暴に見えるのは承知のうえで、v26 の実測に基づく:
        cosine 絶対値・順位差・BM25 一致のどれも、注入すべき問（cat1-4）と
        LoCoMo cat5 の adversarial 問を分離できなかった。cat5 は実在する話題の
        主語や属性だけを差し替えて作られているので、検索側の信号では原理的に
        見分けられない。一方で読み手は、既に記憶が注入されている cat5 42問でも
        正解率 0.810 を保っており（未注入5問は 0.800）、誤注入の実害は小さい。
        """
        if not candidates:
            return False, "no_candidates"
        if intent_wants_memory:
            return True, "intent"
        if injection_policy == "candidates":
            return True, "candidates"
        if injection_policy != "retrieval_assisted":
            return False, "intent_gated"
        if any(c.get("retrieval_source") == "both" for c in candidates):
            return True, "retrieval_assisted"
        return False, "weak_evidence"

    def _quick_vector_search(
        self,
        user_input,
        brain,
        instance_name,
        limit=3,
        threshold=0.6,
        override_config=None,
        retrieval_query=None,
    ) -> list:
        """Layer 1: キーワード抽出なしの純粋なベクトル検索。"""
        return self._quick_vector_search_diag(
            user_input,
            brain,
            instance_name,
            limit,
            threshold,
            override_config,
            retrieval_query,
        )["results"]

    def _quick_vector_search_diag(
        self,
        user_input,
        brain,
        instance_name,
        limit=3,
        threshold=0.6,
        override_config=None,
        retrieval_query=None,
    ) -> dict:
        """Layer 1 + 診断情報。Returns {"results": [...], "diagnostics": {...}}"""
        try:
            return brain.quick_vector_search_diag(
                user_input,
                instance_name,
                limit=limit,
                threshold=threshold,
                override_config=override_config,
                retrieval_query=retrieval_query,
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
        scan_depth = int(glossary_conf.get("scan_depth", 0))
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
            brain_conf = _resolve_section("brain", override_config)
            # hybrid では検索語を決定論的に組み立てるため、LLM のキーワード抽出は
            # 呼ばない（計画書 §3.6）。vector では従来どおり。
            if brain_conf.get("search_mode") == "hybrid":
                keywords = []
            else:
                keyword_data = brain.extract_keywords(user_input, override_config)
                keywords = keyword_data.get("keywords", [])
                if not keywords:
                    return {"results": [], "keywords": []}

            limit = brain_conf.get("search_limit", 3)
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
