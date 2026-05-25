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
            "temperature": 0.7,
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
        # --- Stage 3: Knowledge Maturation (§13) ---
        "knowledge_maturation_enabled": False,
        "knowledge_maturation_interval_days": 1,
        "knowledge_maturation_window_days": 7,
        "knowledge_maturation_max_cards": 30,
        "knowledge_maturation_min_usage_count": 1,
        "memory_node_candidate_threshold": 0.65,
        "memory_node_active_threshold": 0.75,
        "memory_node_promotion_threshold": 0.85,
        "memory_node_promotion_min_sources": 2,
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
    },
    "gatekeeper": {
        "tier_rc_threshold": 0.4,
        "tier_cn_threshold": 0.3,
    },
    "chat": {
        "streaming_enabled": True,
    },
    "glossary": {
        "scan_depth": 2,
        "scan_target": "both",
        "max_entries": 20,
        "max_chars": 4000,
    },
}
