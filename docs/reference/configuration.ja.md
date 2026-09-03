# 設定レイヤー仕様

🌐 **日本語** | [English](configuration.md)

> 最終更新: 2026-08-22

Butly の設定は「**グローバル設定**（全インスタンス共通）」と
「**インスタンス設定**（ペルソナ単位の上書き）」の 2 段構えです。
グローバル設定は pydantic-settings ベースの `butly_core/settings/` に集約中で、
`butly_core.config` の module-level dict は移行が終わるまでの**互換シム**です。

---

## 1. 全体像

```
butly_core/settings/defaults.py          ← AI_CONFIG / SYSTEM_CONFIG の既定値
        ↓ recursive_update（セクション単位の深いマージ）
<data_dir>/user_config.json              ← AI_CONFIG / SYSTEM_CONFIG
                                            / LLM_CONNECTIONS / LLM_CAPABILITY_OVERRIDES
        ↓
get_settings(config_path) → RootSettings（typed・lru_cache）
        ↓ apply_runtime_settings(data_dir)
butly_core.config.AI_CONFIG / SYSTEM_CONFIG（in-place 更新される互換シム）
+ ConnectionRegistry.reset_to_builtin() → user 定義 Connection 登録
+ configure_capability_runtime(data_dir, overrides)
        ↓ 実行時にさらに上書き
インスタンス `config.json`（`override_config` として各コンポーネントへ渡る）
        ↓
リクエスト単位 override（`POST /chat` / `/api/v1/chat` の `model_name` 等）
```

**優先順位（後勝ち）**: defaults → `user_config.json` → インスタンス
`config.json` → リクエスト単位 override。

環境変数はこのチェーンには**入りません**（§4 参照）。APIキーだけが別経路で
`os.environ` から読まれます。

---

## 2. `butly_core/settings/` の構成

| ファイル | 役割 |
|---|---|
| `defaults.py` | `AI_CONFIG` / `SYSTEM_CONFIG` の既定値（**設定値の一次情報源**） |
| `sources.py` | `load_settings_data()`。defaults を deepcopy → `user_config.json` を `recursive_update` → `normalize_ai_config()` で connection を補完・整合検査 |
| `root.py` | `RootSettings`（`BaseSettings`）、`get_settings()`、`clear_settings_cache()`、`override_settings()` |
| `ai.py` | `AIConfig` / `RoleConfig` / `GenerationConfig` / `SafetySetting` |
| `system.py` | `SystemConfig`（セクションごとの dict） |
| `connections.py` | `LLMConnection`。id / env 名 / base_url / extra_headers を field_validator で検証 |
| `instance.py` | `InstanceConfig`。現状は緩いプレースホルダ（厳密化は後続 Phase） |
| `bootstrap.py` | `apply_runtime_settings(data_dir)`。typed settings を legacy global・ConnectionRegistry・Capability runtime へ反映 |

`AIConfig` / `SystemConfig` は Phase 1 の互換優先方針により、
各ロール・各セクションを **dict のまま**保持します（legacy dict と byte 単位で一致させるため）。
`RoleConfig` / `GenerationConfig` は既に定義済みで、後続 Phase で段階的に厳密化します。

### コードからの読み方

```python
from butly_core.settings import get_settings

settings = get_settings()               # 既定は <project_root>/user_config.json
chat = settings.AI_CONFIG["chat"]       # legacy 互換の dict（deepcopy 済み）
probe = settings.SYSTEM_CONFIG["memory_probe"]
```

`AI_CONFIG` / `SYSTEM_CONFIG` / `LLM_CONNECTIONS` / `LLM_CAPABILITY_OVERRIDES`
プロパティはいずれも **deepcopy を返す**ので、戻り値を書き換えてもキャッシュは汚れません。

### テストでの差し替え

```python
from butly_core.settings import clear_settings_cache, get_settings, override_settings

# A. 別の user_config.json を読ませる
settings = get_settings(tmp_path / "user_config.json")

# B. 組み立て済み RootSettings を一時的に強制する
with override_settings(my_settings):
    ...

# C. ファイル/環境変数を書き換えた後にキャッシュを捨てる
clear_settings_cache()          # get_settings.cache_clear() でも同じ
```

legacy global（`butly_core.config.AI_CONFIG`）の直接 mutation は、
既存テストの互換用途に限定します。

---

## 3. グローバル設定: `user_config.json`

`user_config.json.example` が雛形です（**`user_config.json` 本体は gitignore 済み**）。

| トップレベルキー | 内容 |
|---|---|
| `AI_CONFIG` | ロール別の `connection` + `model_name` + `generation_config` + `safety_settings` |
| `SYSTEM_CONFIG` | `agent` / `paths` / `memory` / `brain` / `backup` / `search` / `memory_probe` / `gatekeeper` / `chat` / `glossary` / `trace` |
| `LLM_CONNECTIONS` | ユーザー定義 Connection の配列（OpenAI 互換 provider の追加） |
| `LLM_CAPABILITY_OVERRIDES` | `{connection_id: {model_id: {...}}}` の manual override |
| `LLM_PROVIDERS` | provider 固有の補助設定（例: `ollama.base_url`） |

### AI_CONFIG のロール

| ロール | 用途 |
|---|---|
| `chat` | 応答生成 |
| `summary` | 中期ダイジェスト・関係性・会話圧縮ログ（session digest） |
| `knowledge` | ナレッジカード生成、Stage 3 Knowledge Maturation |
| `embedding` | ベクトル埋め込み |
| `gatekeeper` | tier 判定・StateUpdater |
| `context_classifier` | 空なら `gatekeeper` を継承 |

`connection` を省略すると `normalize_ai_config()` が `model_name` の prefix から推定します
（旧形式互換）。built-in connection と model_name の組み合わせが矛盾する場合は
推定値で置き換え、警告を print します。

### SYSTEM_CONFIG の主要セクション

| セクション | 代表キー |
|---|---|
| `agent` | `agent_name` / `user_name` / `locale` |
| `paths` | `db_name` / `system_instruction` / `key_memory` |
| `memory` | `short_term_limit` / `use_summarized_mid_term` / `rag_source_mode` / `rag_raw_max_chars` / `rag_raw_top_k` / `rag_raw_neighbor_radius` / Stage 3 の `knowledge_maturation_*` と `memory_node_*` |
| `brain` | `search_mode` / `search_limit` / `time_decay_rate` / `dynamic_threshold` / `readable_instances` / BM25・RRF・Evidence Fusion のパラメータ |
| `memory_probe` | `retrieval_execution` / `injection_policy` / `vector_search_limit` / `vector_search_threshold` / `deep_search_enabled` |
| `gatekeeper` | `tier_rc_threshold` / `tier_cn_threshold` |
| `chat` | `streaming_enabled` |
| `glossary` | `scan_depth` / `scan_target` / `max_entries` / `max_chars` |
| `search` | `provider`（`tavily` / `ollama`） / `max_results` / `search_depth` |
| `backup` | `generations` / `dir_name` |
| `trace` | `enabled` / `detail` / `hidden_nodes` |

既定値の正本は `butly_core/settings/defaults.py` です。
本ドキュメントとズレた場合は **defaults.py が正**。

---

## 4. 環境変数

### 設定の上書きに環境変数は使えません（意図的）

`RootSettings` は環境変数 source を**持っていません**。設定を変えるときは
`user_config.json` かインスタンス `config.json` を編集してください。

理由は 2 つあります。

1. `get_settings()` は `load_settings_data()` の結果を **init kwargs** として
   `RootSettings(**data)` に渡します。pydantic-settings の優先順位は
   `init > env > dotenv` なので、4 フィールドすべてが init で埋まる以上、
   env source はどのみち勝てません。
2. 仮に有効化すると**壊れます**。セクションが `dict[str, Any]` なので、
   env は**マージではなく置換**として適用されます。

   | 設定した env | 結果 |
   |---|---|
   | `BUTLY_SYSTEM__brain__search_mode=hybrid` | `brain` が **23 キー → 1 キー**。`search_limit` / `time_decay_rate` / `bm25_weights` / `rrf_k` などが全消滅 |
   | `BUTLY_SYSTEM__brain__search_limit=5` | `'5'`（int ではなく **str**） |

env による上書きを入れるなら、セクションの型付け
（[pydantic-settings 設定統合計画](../planning/active/pydantic_settings_plan.ja.md)
の Phase 2/3）と、マージを保つ `settings_customise_sources` がセットで必要です。
それまでは「効くように見える宣言」を置きません。

### APIキー（別経路）

APIキー類（`GOOGLE_API_KEY` / `GEMINI_API_KEY` / `OPENAI_API_KEY` / `XAI_API_KEY` /
`TAVILY_API_KEY` / `OLLAMA_WEB_SEARCH_API_KEY` など）は `BUTLY_` 接頭辞を持たず、
settings チェーンも通りません。

- `main.py:_load_env_from_data_dir()` が起動時に `<data_dir>/.env` を読み、
  `os.environ.setdefault()` で環境変数へ流し込む（既存の環境変数は上書きしない）
- provider 側（`gemini.py` / `_openai_compat.py` / `brain.py` / `sleeptime.py`）も
  `load_dotenv()` でフォールバック読み込みする
- Connection 定義の `api_key_env` / `api_key_fallback_envs` が参照する環境変数名を決める

雛形は `.env.example`。**`.env` / `APIkey.env` はコミットしない。** gitignore 済みです。

### 実際に効いている `BUTLY_*`（すべて `os.environ` 直読み）

これらは settings チェーンを通らず、参照側が直接 `os.environ` を読みます。

| 変数 | 読む場所 | 用途 |
|---|---|---|
| `BUTLY_DESKTOP_TOKEN` | `butly_api/auth.py` / `server.py` | desktop sidecar の per-launch Bearer token。未設定なら `/api/v1` 認証オフ（dev / Streamlit） |
| `BUTLY_DEVELOPER_MODE` | `main.py` / `butly_api/server.py` | 開発者モード |
| `BUTLY_CHRONOS_NOW` | `butly_core/core/chronos.py` | 現在時刻の固定（評価・テスト用） |
| `BUTLY_API_URL` | `app.py` | Streamlit → backend の URL |
| `BUTLY_DEV_BACKEND_PORT` / `BUTLY_DEV_BACKEND_URL` | `frontend/src-tauri/src/backend.rs` / `vite.config.ts` | 開発モードの backend 接続先 |
| `BUTLY_EVALUATION_OUTPUT_DIR` / `BUTLY_DIALOGUE_AB_OUTPUT_DIR` / `BUTLY_LOCOMO_DATASET` | `evals/locomo/web_jobs.py` | 評価の入出力パス |
| `BUTLY_SIDECAR_ONEFILE` | `scripts/build_backend_sidecar.py` | PyInstaller の build モード |

**これらは「設定」ではなく「起動時の実行環境」です。** `AI_CONFIG` / `SYSTEM_CONFIG`
の値を env で差し替える経路は存在しません。

---

## 5. インスタンス設定: `config.json`

`butly_core/instances/<name>/config.json`。グローバル設定を**セクション単位で上書き**します。

| セクション | 内容 |
|---|---|
| `agent_profile` | `ai_name` / `locale` など。ペルソナの名乗り |
| `user_profile` | `user_name` / `preferred_call` / `birthday` など |
| `brain` | `search_limit` / `default_use_google_search` / `readable_instances` / `use_context_cache` |
| `chat` | ロール上書き（`connection` + `model_name`） |
| `memory` | `use_summarized_mid_term` / Stage 3 パラメータなど |
| `sleeptime` | `max_digest_chars` / `max_relationship_chars` / `relationship_update_interval_days` / `update_targets` |
| `gatekeeper` | `tier_rc_threshold` / `tier_cn_threshold` |
| `context_levels` | プロンプト各ブロックの詳細度プリセット（[context_levels 仕様](context_levels.ja.md)） |

インスタンス config は各コンポーネントへ `override_config` として渡り、
`_merge_config()` でグローバル値と深くマージされます。

`InstanceManager` が `config.json` / `system_instruction.txt` を書くときは
`atomic_write_text` を使います（[コーディング規約](coding_conventions.ja.md)）。

---

## 6. `system_config.json`（legacy UI 用・別系統）

legacy Streamlit の設定画面が保存する UI 設定です。
`routers/settings.py` が `deps.BASE_DIR / "system_config.json"` を直接読み書きしており、
**pydantic settings チェーンには入りません**。正式デスクトップ UI への移行完了時に整理予定です。

---

## 7. 秘密情報の扱い

以下は gitignore 済みです。**コミットしない・出力に貼らない。**

```
.env  APIkey.env  *.env
user_config.json  user_prompts.json  system_config.json
external_accounts.json  persons.json
llm_capabilities.json          ← Capability の観測キャッシュ（<data_dir> 直下）
*.db  butly_core/instances/
```

雛形（`*.example`）を参照・更新してください:
`.env.example` / `user_config.json.example` / `persons.json.example`。

APIキーは Web UI から保存でき、**保存済みの秘密値は再表示されません**
（詳細は [LLM Connection / APIキー管理](llm_connections.ja.md)）。

---

## 8. 関連ドキュメント

- [LLM Connection / APIキー管理](llm_connections.ja.md) — Connection・Capability 解決の詳細
- [context_levels 仕様](context_levels.ja.md) — プロンプトブロックの詳細度プリセット
- [記憶ライフサイクル](memory_lifecycle.ja.md) — `memory` セクションが効く場所
- [ファイル構成](FILE_STRUCTURE.ja.md) — 各モジュールの責務
