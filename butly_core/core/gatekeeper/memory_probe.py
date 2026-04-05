"""
memory_probe.py
---------------
事実ベースの記憶検索プローブ。
LLM 呼び出しなし。実際の検索結果に基づいて判定する。

Phase 1.5: MemoryJudge (LLM版) を置換。

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
        "前に", "以前", "あの時", "あのとき", "この前", "昔",
        "前回", "前話した", "覚えてる",
    ]
    # 明示的な過去参照パターン（英語）
    en_patterns = [
        "before", "remember", "last time", "we discussed",
        "we talked about", "you mentioned",
    ]
    input_lower = user_input.lower()
    for marker in ja_patterns + en_patterns:
        if marker in input_lower:
            return True

    # 「〜だっけ？」「〜でしたっけ」「〜どうなった？」パターン
    if re.search(r"(だっけ|でしたっけ|どうなった|どうした|の件)", user_input):
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
    ) -> dict:
        """
        Returns:
            {
                "status": "hit" | "no_hit" | "deep_search",
                "candidates": [...],
                "glossary_hits": [...],
            }
        """
        t0 = time.time()

        probe_conf = SYSTEM_CONFIG.get("memory_probe", {})
        vector_limit = probe_conf.get("vector_search_limit", 3)
        vector_threshold = probe_conf.get("vector_search_threshold", 0.6)
        deep_enabled = probe_conf.get("deep_search_enabled", True)

        # Layer 1: Quick Vector Search
        candidates = self._quick_vector_search(
            user_input, brain, instance_name,
            limit=vector_limit, threshold=vector_threshold,
            override_config=override_config,
        )

        # Layer 1.5: Glossary Match
        glossary_hits = self._match_glossary(user_input, memory_manager)

        t1 = time.time()

        # Layer 1 でヒットあり → 即返却
        if candidates:
            print(f"[MemoryProbe] Layer 1 hit: {len(candidates)} candidates, "
                  f"glossary={len(glossary_hits)} ({int((t1-t0)*1000)}ms)")
            return {
                "status": "hit",
                "candidates": candidates,
                "glossary_hits": glossary_hits,
            }

        # Layer 2 トリガー判定
        if not deep_enabled:
            print(f"[MemoryProbe] no_hit (deep_search disabled), "
                  f"glossary={len(glossary_hits)} ({int((t1-t0)*1000)}ms)")
            return {
                "status": "no_hit",
                "candidates": [],
                "glossary_hits": glossary_hits,
            }

        headline_match = self._check_headline_match(user_input, recent_headlines)

        if should_deep_search(user_input, candidates, headline_match, bool(glossary_hits)):
            deep_candidates = self._deep_search(
                user_input, brain, instance_name, override_config
            )
            t2 = time.time()
            if deep_candidates:
                print(f"[MemoryProbe] deep_search hit: {len(deep_candidates)} candidates, "
                      f"glossary={len(glossary_hits)} ({int((t2-t0)*1000)}ms)")
                return {
                    "status": "deep_search",
                    "candidates": deep_candidates,
                    "glossary_hits": glossary_hits,
                }
            print(f"[MemoryProbe] deep_search no_hit, "
                  f"glossary={len(glossary_hits)} ({int((t2-t0)*1000)}ms)")
        else:
            print(f"[MemoryProbe] no_hit (no deep_search trigger), "
                  f"glossary={len(glossary_hits)} ({int((t1-t0)*1000)}ms)")

        return {
            "status": "no_hit",
            "candidates": [],
            "glossary_hits": glossary_hits,
        }

    def _quick_vector_search(
        self, user_input, brain, instance_name,
        limit=3, threshold=0.6, override_config=None,
    ) -> list:
        """Layer 1: キーワード抽出なしの純粋なベクトル検索。"""
        try:
            results = brain.quick_vector_search(
                user_input, instance_name,
                limit=limit, threshold=threshold,
                override_config=override_config,
            )
            return results
        except Exception as e:
            print(f"[MemoryProbe] Quick vector search error: {e}")
            return []

    def _match_glossary(self, user_input, memory_manager) -> list:
        """Layer 1.5: user_input に含まれる glossary エントリをマッチングする。"""
        try:
            glossary_data = memory_manager.get_glossary_raw()
        except Exception:
            return []

        entries = glossary_data.get("entries", [])
        hits = []
        input_lower = user_input.lower()

        for entry in entries:
            if entry.get("status") != "active":
                continue

            term = entry.get("term", "")
            aliases = entry.get("aliases", [])
            definition = entry.get("definition", "")

            if term.lower() in input_lower:
                hits.append({
                    "term": term,
                    "definition": definition,
                    "aliases": aliases,
                    "match_type": "term",
                })
                continue

            for alias in aliases:
                if alias.lower() in input_lower:
                    hits.append({
                        "term": term,
                        "definition": definition,
                        "aliases": aliases,
                        "match_type": "alias",
                    })
                    break

        return hits

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

    def _deep_search(self, user_input, brain, instance_name, override_config=None) -> list:
        """Layer 2: キーワード抽出 + ハイブリッド検索。"""
        try:
            keyword_data = brain.extract_keywords(user_input, override_config)
            keywords = keyword_data.get("keywords", [])
            if not keywords:
                return []

            limit = SYSTEM_CONFIG["brain"]["search_limit"]
            results = brain.search_knowledge(
                keywords, user_input,
                instance_name=instance_name,
                limit=limit,
                override_config=override_config,
            )
            for r in results:
                r["source"] = "keyword"
            return results
        except Exception as e:
            print(f"[MemoryProbe] Deep search error: {e}")
            return []
