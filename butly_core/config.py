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
    }
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
        "key_memory": "Key_Memory.txt",
        "mid_term": "mid_term.txt"
    },
    "memory": {
        "max_mid_term_chars": 30000,
        "short_term_limit": 6,
        "generate_mid_term_summaries": True,   # Phase 3: 二層要約の生成を有効化
        "max_digest_chars": 8000,              # Phase 3: digest上限（超過分はアーカイブ）
        "relationship_update_interval_days": 7, # Phase 3: 関係性スナップショットの更新間隔（日数）
        "use_summarized_mid_term": True,  # ★NEW: True=要約注入 / False=RAW注入
    },
    "brain": {
        "search_limit": 3,
        "keyword_hit_threshold": 5,
        "fallback_fetch_limit": 50,
        "cache_ttl_hours": 3,
        "summary_char_limit": 200,
        "use_context_cache": False, # Set to False to disable Context Caching
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
    }
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
