# AI Model Configuration
AI_CONFIG = {
    # 1. Chat Model (Main Brain)
    "chat": {
        "model_name": "gemini-3-flash-preview", # Updated to Gemini 3 Preview as per user request
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
        ]
    },
    
    # 2. Summary Model (Cost-effective, good for long context)
    "summary": {
        "model_name": "gemini-3.1-flash-lite-preview", 
        "generation_config": {
            "temperature": 0.3,
            "max_output_tokens": 4096,
        },
        "safety_settings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ]
    },
    
    # 3. Knowledge Model (High reasoning for distillation)
    "knowledge": {
        "model_name": "gemini-3.1-pro-preview", 
        "generation_config": {
            "temperature": 0.7, # Consistent output preferred
            "max_output_tokens": 8192,
        },
        "safety_settings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ]
    },
    
    # 4. Embedding Model (Vector search)
    "embedding": {
        "model_name": "models/gemini-embedding-001",
        # Retrieval task type is usually set at call time
    },
    
    # 5. Gatekeeper Model (Tier classification, fast evaluation)
    "gatekeeper": {
        "model_name": "gemini-3.1-flash-lite-preview",
        "generation_config": {
            "temperature": 0.0,      # 判定の一貫性を最大化
            "max_output_tokens": 512, # JSON出力に十分な量
        },
        "safety_settings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ]
    },

    # 6. ContextClassifier Model (未設定時は gatekeeper にフォールバック)
    "context_classifier": {},
}

# System Configuration (Paths, Thresholds, Limits)
SYSTEM_CONFIG = {
    "agent": {
        "agent_name": "Butly",
        "user_name": "User",
        "locale": "en"
    },
    "paths": {
        "db_name": "butly_memory.db",
        "system_instruction": "system_instruction.txt",
        "key_memory": "Key_Memory.txt"
    },
    "memory": {
        "max_raw_tokens": 4096,                 # RAW 読み込みトークン上限
        "raw_injection_format": "plaintext",     # "markdown" | "plaintext" | "compact"
        "short_term_limit": 6,
        "generate_mid_term_summaries": True,   # Phase 3: 二層要約の生成を有効化
        "max_digest_chars": 8000,              # Phase 3: digest上限（超過分はアーカイブ）
        "relationship_update_interval_days": 7, # Phase 3: 関係性スナップショットの更新間隔（日数）
        "use_summarized_mid_term": True,  # ★NEW: True=要約注入 / False=RAW注入
    },
    "brain": {
        "search_limit": 3,
        "keyword_hit_threshold": 5,
        "fallback_fetch_limit": 100,  # 0.005 decay 下で 3ヶ月以上前のカードも候補に残るよう拡大
        "time_decay_rate": 0.003,  # 日数あたりの減衰率。 small = old cards retain visibility
        "summary_char_limit": 200,
        "readable_instances": ["self"], # ["self"], ["self", "00_master"] 等で横断検索
        "dynamic_threshold": 0.6, # Google Search Dynamic Retrieval Threshold (0.0 - 1.0)
        "default_use_google_search": False # デフォルトでGoogle検索グラウンディングを使用するか
    },
    "backup": {
        "generations": 7,
        "dir_name": "db_backups"
    },
    "search": {
        "provider": "tavily",
        "max_results": 3,
        "search_depth": "basic",
    },
    "memory_probe": {
        "vector_search_limit": 3,
        "vector_search_threshold": 0.4,  # 緩和: 0.6→0.4 (時間減衰込みの実効値で判定するため)
        "deep_search_enabled": True,
    },
    "gatekeeper": {
        # tier 判定閾値: rc<=tier_rc_threshold AND cn<=tier_cn_threshold → reflex、それ以外 → mid
        # 人/会話スタイルによる感じ方の違いを吸収するため設定化。
        # instance_config["gatekeeper"] で上書き可。
        "tier_rc_threshold": 0.4,
        "tier_cn_threshold": 0.3,
    },
    "glossary": {
        # 1 ターン = user+assistant 1 ペア。0 で user_input のみ
        "scan_depth": 2,
        # "user" | "assistant" | "both"
        "scan_target": "both",
        # 1リクエストで注入する最大エントリ数
        "max_entries": 20,
        # 注入合計文字数の上限（greedy skip）
        "max_chars": 4000,
    },
}

# --- User Config Override ---
import json
from pathlib import Path

def _recursive_update(base_dict, update_dict):
    for key, value in update_dict.items():
        if isinstance(value, dict) and key in base_dict and isinstance(base_dict[key], dict):
            _recursive_update(base_dict[key], value)
        else:
            base_dict[key] = value

USER_CONFIG_PATH = Path(__file__).parent.parent / "user_config.json"
if USER_CONFIG_PATH.exists():
    try:
        with open(USER_CONFIG_PATH, "r", encoding="utf-8") as f:
            user_config = json.load(f)
            if "AI_CONFIG" in user_config:
                _recursive_update(AI_CONFIG, user_config["AI_CONFIG"])
            if "SYSTEM_CONFIG" in user_config:
                _recursive_update(SYSTEM_CONFIG, user_config["SYSTEM_CONFIG"])
        print("[Config] Loaded user_config.json")
    except Exception as e:
        print(f"[Config] Failed to load user_config.json: {e}")
