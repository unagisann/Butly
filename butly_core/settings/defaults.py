"""Default settings shared by the pydantic settings layer."""

from __future__ import annotations

AI_CONFIG = {
    "chat": {
        "connection": "google",
        "model_name": "gemini-3.5-flash",
        "generation_config": {
            "temperature": 0.7,
            "top_p": 0.95,
            "top_k": 64,
            "max_output_tokens": 8192,
        },
        "safety_settings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ],
    },
    "summary": {
        "connection": "google",
        "model_name": "gemini-3.1-flash-lite",
        "generation_config": {
            "temperature": 0.3,
            "max_output_tokens": 4096,
        },
        "safety_settings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ],
    },
    "knowledge": {
        "connection": "google",
        "model_name": "gemini-3.1-pro-preview",
        "generation_config": {
            # カード JSON の形状安定を最優先 (config.py AI_CONFIG と同値に保つ)
            "temperature": 0.2,
            "max_output_tokens": 8192,
        },
        "safety_settings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ],
    },
    "embedding": {
        "connection": "google",
        "model_name": "gemini-embedding-2",
    },
    "gatekeeper": {
        "connection": "google",
        "model_name": "gemini-3.1-flash-lite",
        "generation_config": {
            "temperature": 0.0,
            "max_output_tokens": 512,
        },
        "safety_settings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ],
    },
    "context_classifier": {},
}

SYSTEM_CONFIG = {
    "agent": {"agent_name": "Butly", "user_name": "User", "locale": "en"},
    "paths": {
        "db_name": "butly_memory.db",
        "system_instruction": "system_instruction.txt",
        "key_memory": "Key_Memory.txt",
    },
    "memory": {
        "max_raw_tokens": 4096,
        "raw_injection_format": "plaintext",
        "short_term_limit": 6,
        "generate_mid_term_summaries": True,
        "max_digest_chars": 8000,
        "relationship_update_interval_days": 7,
        "use_summarized_mid_term": True,
        "count_dedup_hours": 6,
        # --- RAG 注入ソース（parent-document retrieval） ---
        # "cards": カード(summary/episode)のみ / "raw": 当時の会話原文のみ /
        # "both": カード + 原文。原文はカードの source_files から遅延解決する。
        "rag_source_mode": "cards",
        # 原文抜粋の合計文字数上限（0 = 無制限。超過ファイルは greedy skip）
        # config.py AI/SYSTEM_CONFIG と同値に保つ（6000→2500 の経緯はそちら参照）
        "rag_raw_max_chars": 2500,
        # 原文を展開する上位カード数（raw/both 時）。1 = 最上位カードの原文のみ、
        # 残りはサマリ。0 以下で全カードの原文を greedy 注入（従来の both 挙動）。
        "rag_raw_top_k": 1,
        # --- Stage 3: Knowledge Maturation（content hash 式レビューキュー） ---
        # 旧キー (interval_days / window_days / max_cards / min_usage_count) は
        # 廃止。max_cards はインスタンス設定に残っていれば batch_size として
        # 読み替えられる（後方互換フォールバック）。
        "knowledge_maturation_enabled": False,
        "knowledge_maturation_batch_size": 40,
        "knowledge_maturation_max_batches_per_run": 1,
        "knowledge_maturation_bootstrap_max_cards": 2000,
        "knowledge_maturation_prompt_max_chars": 40000,
        "knowledge_maturation_retry_max_calls_per_run": 8,
        "memory_node_candidate_threshold": 0.65,
        "memory_node_active_threshold": 0.75,
        "memory_node_promotion_threshold": 0.85,
        "memory_node_promotion_min_sources": 2,
        # --- Stage 3 reflection（§7 staleness 減衰。opt-in） ---
        "memory_node_decay_enabled": False,
        "memory_node_stale_days": 30,
        "memory_node_decay_per_period": 0.05,
    },
    "brain": {
        "search_limit": 3,
        "keyword_hit_threshold": 5,
        "fallback_fetch_limit": 100,
        "time_decay_rate": 0.003,
        "summary_char_limit": 200,
        "readable_instances": ["self"],
        "dynamic_threshold": 0.6,
        "default_use_google_search": False,
        # --- ハイブリッド検索（検索改修計画 §3.5） ---
        # "vector" = 従来のベクトル単独。"hybrid" = BM25(FTS5/trigram) と RRF 融合。
        # "dual_query" = 元発話と Gatekeeper の自己完結検索文を各15件検索し、
        # RRFで重複排除・融合して最大25件の診断候補を残す（注入は通常どおり上位3件）。
        # eval で効果を確認してから既定を昇格させる。
        "search_mode": "vector",
        "bm25_candidates": 20,
        "vector_candidates": 20,
        "dual_query_candidates": 15,
        "dual_query_pool_limit": 25,
        "rrf_k": 60,
        # bm25() の column weight。trigram トークン上の BM25 は語単位 BM25 と
        # 挙動が異なるため、この値は推測でしかない（offline replay のスイープ対象）。
        "bm25_weights": {"title": 5.0, "tags": 3.0, "summary": 2.0, "episode": 1.0},
        # この比率を超えて出現する語は「弱い語」。弱い語しか一致していない
        # カードは候補から落とす（会話の主役名だけで候補が埋まるのを防ぐ）。
        "bm25_max_df_ratio": 0.5,
        # 件数の少ない DB を守る床。カード3枚で「2枚に出る語」は比率上は
        # 高DFだが、ノイズではない。df がこの件数未満なら弱い語にしない。
        "bm25_min_weak_df": 5,
        # df 計算と語境界検証のためにスキャンする最大ヒット数。
        "bm25_scan_limit": 500,
    },
    "backup": {"generations": 7, "dir_name": "db_backups"},
    "search": {
        "provider": "tavily",
        "max_results": 3,
        "search_depth": "basic",
    },
    "memory_probe": {
        "vector_search_limit": 3,
        "vector_search_threshold": 0.4,
        "deep_search_enabled": True,
        # 検索の実行と、検索結果をプロンプトへ注入するかの判定を分離する
        # （検索改修計画 §3.3）。
        # retrieval_execution: "always" = need_intent に関わらず検索する
        #                      "intent_gated" = 旧挙動（past_fact/relationship のみ）
        # injection_policy: "intent_gated" = 旧挙動どおり need_intent で注入判定
        #                   "retrieval_assisted" = 分類器が null でも強い検索根拠
        #                     （BM25 とベクトルが同じカードを支持）なら注入。hybrid 専用
        #                   "candidates" = 候補があれば注入する（分類器の判定を使わない）。
        #                     v26 の実測では、cosine・順位差・BM25 一致のいずれも
        #                     「注入すべき問」と cat5 の adversarial 問を分離できなかった。
        #                     retrieval 側に効くゲートが無いので、候補の有無だけで決める
        "retrieval_execution": "always",
        "injection_policy": "intent_gated",
    },
    "gatekeeper": {
        "tier_rc_threshold": 0.4,
        "tier_cn_threshold": 0.3,
    },
    "chat": {
        "streaming_enabled": True,
    },
    "glossary": {
        "scan_depth": 0,
        "scan_target": "both",
        "max_entries": 20,
        "max_chars": 4000,
    },
    # Trace Graph (issue #51)
    # enabled: trace.json の保存 ON/OFF。
    # detail / hidden_nodes: 表示フィルタ (保存は常に full)。
    #   detail: "full" | "summary" (summary は補助 LLM ノードを非表示)
    #   hidden_nodes: purpose または node id のリスト (例: ["embedding"])
    "trace": {
        "enabled": True,
        "detail": "full",
        "hidden_nodes": [],
    },
}
