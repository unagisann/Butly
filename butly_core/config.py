# AI Model Configuration
#
# 各 role の dict は ``connection`` (built-in: google/openai/xai/ollama または
# user 定義 LLM_CONNECTIONS) と ``model_name`` のペアで Provider を決定する。
# ``connection`` 省略時は ``model_name`` の prefix から推定される (旧形式互換)。
AI_CONFIG = {
    # 1. Chat Model (Main Brain)
    "chat": {
        "connection": "google",
        "model_name": "gemini-3.5-flash",  # stable: 旧 gemini-3-flash-preview から差し替え
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
    # 2. Summary Model (Cost-effective, good for long context)
    "summary": {
        "connection": "google",
        "model_name": "gemini-3.1-flash-lite",  # stable: preview → stable へ昇格
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
    # 3. Knowledge Model (High reasoning for distillation)
    "knowledge": {
        "connection": "google",
        "model_name": "gemini-3.1-pro-preview",
        "generation_config": {
            # カード JSON の形状安定を最優先 (0.7 では配列パース成功率が run ごとに揺れ、
            # v10 でセッション丸ごとカード0枚が発生)
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
    # 4. Embedding Model (Vector search)
    "embedding": {
        "connection": "google",
        "model_name": "gemini-embedding-2",  # 旧 models/gemini-embedding-001 から差し替え
        # Retrieval task type is usually set at call time
    },
    # 5. Gatekeeper Model (Tier classification, fast evaluation)
    "gatekeeper": {
        "connection": "google",
        "model_name": "gemini-3.1-flash-lite",  # stable: preview → stable へ昇格
        "generation_config": {
            "temperature": 0.0,  # 判定の一貫性を最大化
            "max_output_tokens": 512,  # JSON出力に十分な量
        },
        "safety_settings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ],
    },
    # 6. ContextClassifier Model (未設定時は gatekeeper にフォールバック)
    "context_classifier": {},
}

# System Configuration (Paths, Thresholds, Limits)
SYSTEM_CONFIG = {
    "agent": {"agent_name": "Butly", "user_name": "User", "locale": "en"},
    "paths": {
        "db_name": "butly_memory.db",
        "system_instruction": "system_instruction.txt",
        "key_memory": "Key_Memory.txt",
    },
    "memory": {
        "max_raw_tokens": 4096,  # RAW 読み込みトークン上限
        "raw_injection_format": "plaintext",  # "markdown" | "plaintext" | "compact"
        "short_term_limit": 6,
        "generate_mid_term_summaries": True,  # Phase 3: 二層要約の生成を有効化
        "max_digest_chars": 8000,  # Phase 3: digest上限（超過分はアーカイブ）
        "relationship_update_interval_days": 7,  # Phase 3: 関係性スナップショットの更新間隔（日数）
        "use_summarized_mid_term": True,  # ★NEW: True=要約注入 / False=RAW注入
        "count_dedup_hours": 6,  # usage_count dedup 窓（同一カードの再カウント抑制時間）
        # --- RAG 注入ソース（parent-document retrieval） ---
        # "cards": カード(summary/episode)のみ / "raw": 当時の会話原文のみ /
        # "both": カード + 原文。原文はカードの source_files から遅延解決する。
        "rag_source_mode": "cards",
        # 原文抜粋の合計文字数上限（0 = 無制限。超過ファイルは greedy skip）
        "rag_raw_max_chars": 6000,
        # 原文を展開する上位カード数（raw/both 時）。1 = 最上位カードの原文のみ、
        # 残りはサマリ（カード）で渡す＝弱い読み手での希釈・needle-in-haystack を
        # 避ける既定。0 以下で全カードの原文を greedy 注入（従来の both 挙動）。
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
        "fallback_fetch_limit": 100,  # 0.005 decay 下で 3ヶ月以上前のカードも候補に残るよう拡大
        "time_decay_rate": 0.003,  # 日数あたりの減衰率。 small = old cards retain visibility
        "summary_char_limit": 200,
        "readable_instances": ["self"],  # ["self"], ["self", "00_master"] 等で横断検索
        "dynamic_threshold": 0.6,  # Google Search Dynamic Retrieval Threshold (0.0 - 1.0)
        "default_use_google_search": False,  # デフォルトでGoogle検索グラウンディングを使用するか
    },
    "backup": {"generations": 7, "dir_name": "db_backups"},
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
    "chat": {
        # ストリーミング応答の有効化フラグ。
        # /chat/stream エンドポイントの動作・UI のデフォルトトグル状態に影響。
        # instance_config["chat"]["streaming_enabled"] で上書き可。
        "streaming_enabled": True,
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
    # Trace Graph (issue #51)。enabled は trace.json 保存の ON/OFF、
    # detail / hidden_nodes は表示フィルタ (保存は常に full)。
    "trace": {
        "enabled": True,
        "detail": "full",
        "hidden_nodes": [],
    },
}

# --- User Config Override ---
import copy
from pathlib import Path


def _recursive_update(base_dict, update_dict):
    for key, value in update_dict.items():
        if (
            isinstance(value, dict)
            and key in base_dict
            and isinstance(base_dict[key], dict)
        ):
            _recursive_update(base_dict[key], value)
        else:
            base_dict[key] = value


def _normalize_ai_config(ai_config: dict) -> None:
    """AI_CONFIG の各 role を Phase 2 形式に正規化する。

    処理内容:
      1. ``connection`` 未指定なら ``model_name`` から推定して補完
      2. ``connection`` が built-in でかつ ``model_name`` の prefix と不整合な場合は
         推定結果で上書き (ユーザーが user_config.json で model_name だけ
         書き換えたケースに対応)
      3. 推定不能ならログだけ出して放置 (Provider 呼び出し時に検出)

    user 定義 connection (LLM_CONNECTIONS で登録した Groq 等) は推定できないので
    そのまま尊重する (built-in との区別で判定)。

    import 循環を避けるため遅延 import。
    """
    # 遅延 import (model_registry / connections は config.py を import してはいけない)
    from butly_core.llm.model_registry import infer_connection_id
    from butly_core.llm.connections import is_builtin_connection

    for role, cfg in ai_config.items():
        if not isinstance(cfg, dict):
            continue
        model_name = cfg.get("model_name")
        if not model_name:
            continue
        existing = cfg.get("connection")
        inferred = infer_connection_id(model_name)

        if not existing:
            if inferred:
                cfg["connection"] = inferred
            else:
                print(
                    f"[Config] role={role!r} model_name={model_name!r} の "
                    f"connection を推定できませんでした。明示的に "
                    f'"connection" を指定してください。'
                )
            continue

        # 既存 connection と推定結果が異なるケース
        if inferred and inferred != existing and is_builtin_connection(existing):
            # built-in 同士の不整合 → user は model_name 上書きで意図が変わった
            # と判断して推定を優先する
            print(
                f"[Config] role={role!r} connection={existing!r} と "
                f"model_name={model_name!r} の整合が取れません。"
                f"推定 connection {inferred!r} で置き換えます "
                f"(明示が必要なら user_config.json で connection を指定してください)。"
            )
            cfg["connection"] = inferred


def _register_user_connections(connections_list) -> None:
    """user_config.json の LLM_CONNECTIONS をレジストリに登録する。

    フォーマット (list of dict):
      [
        {"id": "groq", "protocol": "openai_compat",
         "base_url": "https://api.groq.com/openai/v1",
         "api_key_env": "GROQ_API_KEY", "label": "Groq"},
        ...
      ]
    """
    if not isinstance(connections_list, list):
        print(
            f"[Config] LLM_CONNECTIONS は list で指定してください (got {type(connections_list).__name__})"
        )
        return

    from butly_core.llm.connections import (  # 遅延 import
        Connection,
        is_builtin_connection,
        register_connection,
    )

    for entry in connections_list:
        if not isinstance(entry, dict):
            print(f"[Config] LLM_CONNECTIONS の各要素は dict であるべきです: {entry!r}")
            continue
        cid = entry.get("id")
        protocol = entry.get("protocol")
        if not cid or not protocol:
            print(f"[Config] LLM_CONNECTIONS エントリに id/protocol が必須: {entry!r}")
            continue
        if is_builtin_connection(cid):
            print(f"[Config] built-in connection は上書き不可: {cid!r}")
            continue
        try:
            conn = Connection(
                id=cid,
                protocol=protocol,
                base_url=entry.get("base_url"),
                base_url_env=entry.get("base_url_env"),
                api_key_env=entry.get("api_key_env"),
                api_key_fallback_envs=tuple(entry.get("api_key_fallback_envs", ())),
                label=entry.get("label"),
                extra_headers=dict(entry.get("extra_headers") or {}),
                embeddings_supported=entry.get("embeddings_supported", True),
                embedding_model_env=entry.get("embedding_model_env"),
                default_embedding_model=entry.get("default_embedding_model"),
                model_name_strip_prefix=entry.get("model_name_strip_prefix"),
            )
            register_connection(conn, overwrite_user=True)
            print(f"[Config] Registered user LLM connection: {cid}")
        except Exception as e:
            print(f"[Config] Failed to register connection {cid!r}: {e}")


USER_CONFIG_PATH = Path(__file__).parent.parent / "user_config.json"
try:
    from butly_core.settings import get_settings

    _settings = get_settings(USER_CONFIG_PATH)
    AI_CONFIG = copy.deepcopy(_settings.AI_CONFIG)
    SYSTEM_CONFIG = copy.deepcopy(_settings.SYSTEM_CONFIG)
    _register_user_connections(_settings.LLM_CONNECTIONS)
    if USER_CONFIG_PATH.exists():
        print("[Config] Loaded user_config.json")
except Exception as e:
    print(f"[Config] Failed to load settings: {e}")

# user_config.json マージ後・instance_config 読み込み前に AI_CONFIG を normalize する。
# 旧形式 (model_name only) で書かれた role に connection を補完。
# user_config.json が無い場合も default AI_CONFIG に connection が入っているので no-op。
try:
    _normalize_ai_config(AI_CONFIG)
except Exception as e:
    print(f"[Config] _normalize_ai_config failed: {e}")
