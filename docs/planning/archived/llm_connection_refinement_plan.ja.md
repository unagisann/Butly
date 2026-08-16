# LLM Connection 洗練計画（NanoGPT / OpenAI 互換 provider 対応）

対象: ユーザー定義 LLM Connection の設定・保存・利用フロー全体
状態: 実装済み（Phase 4の案はCanonical Request / Capability Resolverで置換）
最終更新: 2026-08-16

> この文書は当時の設計判断を残す履歴であり、現行仕様ではない。
> 生成parameterの現行仕様は
> [`docs/reference/llm_connections.ja.md`](../../reference/llm_connections.ja.md)
> を参照する。特に、下記Phase 4のConnection単位`unsupported_params`案は採用せず、
> protocol Adapter、provider metadata、成功時のみ保存する観測cache、モデル単位manual
> overrideへ置き換えた。

---

## 0. 背景と現状の問題

Butly は既に `Connection`（どこに繋ぐか）+ `ModelRef`（connection_id + model_name）という
LiteLLM の `api_base` / `api_key` / `model` 分離に近い構造を持っている。
土台は妥当なので**新しい抽象は足さず、欠けている経路を埋める**方針を取る。

現状のバグ（コード確認済み）:

| # | 箇所 | 症状 |
|---|------|------|
| A | `app.py:227-307` `_model_selector()` | `model_name` 文字列しか返さない。候補の識別も `model_name` 単独（`model_names.index(...)`）なので、同名モデルが複数 Connection にあると誤選択する |
| B | `app.py:1090-1093`（グローバル設定保存） | `model_name` のみ更新し `connection` を触らない。→ 旧 `connection: "google"` が残ったまま Groq のモデル名が保存され、**無言で誤った provider に投げる** |
| C | `app.py:2816-2823` `_set_model_ref()`（インスタンス設定保存） | `infer_connection_id()` が失敗すると `connection` を `pop` する。ユーザー定義 Connection は保存の瞬間に必ず失われ、次回起動時に `NotImplementedError` |
| D | `routers/settings.py:96-135` `/settings/api_key` | `_KEY_TYPE_MAP` の 4 種（gemini/openai/xai/ollama_web_search）のみ。ユーザー定義 Connection の APIキーを保存する口が無い |
| E | `routers/settings.py:259-273` `ConnectionPayload` / `config.py:250-300` | `id` / `api_key_env` / `base_url` を一切検証しない。`api_key_env: "PATH"` や改行入り文字列も登録できる |
| F | `protocols/openai_compat.py:101-104` | `supports_vision()` が無条件 `True`。未知モデルに画像を送って毎回 API エラー |
| G | `_openai_compat.py:305-337` | 生成パラメータ分岐が `is_reasoning_model()`（`o1/o3/o4` prefix 決め打ち）のみ。サードパーティ経由の reasoning モデルや、`max_tokens` を受け付けない endpoint に対応できない |

B と C が「NanoGPT を設定しても使えない」の直接原因。A はその上で同名モデルを扱うために必要。

---

## 1. 設計方針（LiteLLM から採る／採らないもの）

採る:

- **Provider テンプレートカタログ**: LiteLLM の provider 一覧に相当。
  `base_url` / `api_key_env` / `protocol` を前埋めした雛形を持ち、UI から 1 クリックで
  **user 定義 Connection として**登録する。built-in には入れない
  （built-in は上書き・削除不可なので、後から直せなくなる）。
- **model_info の上書き階層**: capabilities を preset → Connection 既定 → 保守的既定 の順で解決。
- **drop_params 相当**: Connection 単位で「送ってはいけないパラメータ」を宣言できるようにする。

採らない（今回のスコープ外、将来検討）:

- Router 層のフォールバック / リトライ / レート制御 / コスト計算
- 仮想キー・チーム別予算などの key management
- モデル alias（`AI_CONFIG` が既に role → ModelRef の間接参照になっているため不要）

**NanoGPT 専用 Provider クラスは作らない。**
`ConnectionRegistry` + `OpenAICompatAdapter` で完結させる（元計画どおり）。

---

## 2. Phase 0 — 選択ロジックの切り出し（テスト可能化）

`app.py` は Streamlit スクリプトで import 時に UI が走るため、現状 `_model_selector` は
ユニットテストできない。純粋関数を先に分離する。

新規 `butly_core/llm/selection.py`:

```python
@dataclass(frozen=True)
class ModelChoice:
    connection_id: str | None
    model_name: str

def normalize_candidates(candidates: list) -> list[dict]:
    """list[str]（旧形式）/ list[dict] を dict に統一する。"""

def candidate_key(c: dict) -> tuple[str | None, str]:
    """(connection_id, model_name) を候補の一意キーとして返す。"""

def find_current_index(candidates: list[dict], current: ModelChoice) -> int:
    """(connection_id, model_name) 一致 → model_name のみ一致 → 0 の順でフォールバック。"""

def ensure_current_in_candidates(candidates: list[dict], current: ModelChoice) -> list[dict]:
    """保存済みの値が候補に無ければ末尾に追加する（カスタム値の保持）。"""
```

`app.py` の `_model_selector` はこれを呼ぶだけの薄い UI 層にする。
テストは `selection.py` に対して書く（Streamlit 非依存）。

---

## 3. Phase 1 — connection_id を失わない（最優先・バグ修正）

### 3.1 `_model_selector` の戻り値を `ModelChoice` にする

- 候補の識別キーを `(connection_id, model_name)` に変更。
- selectbox のラベルは既存の `_candidate_label()`（`🔌 groq / llama-3.3-70b`）を流用。
- **直接入力欄には Connection の selectbox を併設**する。
  候補は `GET /settings/connections` の全 Connection。既定値は
  「現在保存されている connection」→ 無ければ `infer_connection_id(入力値)` → 無ければ先頭。
- 戻り値は `ModelChoice(connection_id, model_name)`。呼び出し側 6 箇所を追随させる。

### 3.2 保存経路 2 本を両方直す

- グローバル（`app.py:1088-1094`）:
  `provider_ai_cfg[role]["model_name"]` と **`["connection"]` を必ず両方書く**。
- インスタンス（`app.py:2816-2823` `_set_model_ref`）:
  シグネチャを `_set_model_ref(target: dict, choice: ModelChoice)` に変更。
  - `choice.connection_id` があればそれを書く（最優先）。
  - 無ければ `infer_connection_id()`。
  - どちらも無い場合のみ `pop`（旧挙動を残す＝Gemini 等の prefix 推定に委ねる）。

### 3.3 ChatService 側の互換確認

`chat/service.py:283-290` の「instance_config に model_name はあるが connection が無い →
connection を捨てて再推定」する分岐は、**旧 config ファイル救済のため残す**。
Phase 1 以降に保存された config は必ず connection を持つので、この分岐には落ちない。

### 3.4 参照中 Connection の削除ガード

`DELETE /settings/connections/{id}` は、その id が `AI_CONFIG` またはいずれかの
instance_config で参照されている場合 **409** を返す（`?force=true` で強制削除可）。
現状は無条件削除でき、次回のチャットで `KeyError` になる。

---

## 4. Phase 2 — APIキー保存とバリデーション

### 4.1 登録時の検証（**先に入れること**）

`ConnectionPayload` / `_register_user_connections()` の両方で:

- `id`: `^[a-z0-9][a-z0-9_-]{0,63}$`
- `api_key_env` / `base_url_env` / `embedding_model_env`: `^[A-Z_][A-Z0-9_]*$`
- `base_url`: `http://` または `https://` のみ許可
- `extra_headers` のキー/値に改行を含まない

これが無いと 4.2 の「env 名をクライアントから受け取らない」設計が
**登録経路から迂回される**（`api_key_env: "PATH"` を登録して任意 env を書ける）。

### 4.2 `POST /settings/connections/{connection_id}/api_key`

> エンドポイント名は既存の snake_case（`/settings/api_key`, `/settings/test_connection`,
> `/settings/model_candidates`）に合わせる。元計画の `api-key` は表記揺れになる。

- リクエストは `{"api_key": "..."}` のみ。**env 名は受け取らない。**
- 書き込み先 env 名は registry の `connection.api_key_env` から取得。
- 未登録 Connection → 404 / `api_key_env` が無い Connection → 400 / 空キー → 400。
- キー値に改行・`\0` を含む場合は 400（`.env` 行注入の防止）。
- `DATA_DIR/.env` へ `atomic_write_text` で保存し、`os.environ` にも即時反映。
- レスポンスにも UI にも**キー本体を返さない**（`{"message": ..., "api_key_set": true}`）。
- 既存 `set_api_key()` の `.env` 読み書きを共通ヘルパー
  `butly_core/io_utils.py::upsert_env_var(path, name, value)` に切り出して両者で使う。
  現状の実装はコメント行を落とすので、**コメント・空行を保持する行単位 upsert** にする。

### 4.3 削除

`DELETE /settings/connections/{connection_id}/api_key` も同時に入れる
（キーの入れ替え失敗時に .env を手編集させないため）。

### 4.4 UI

Connection 一覧（`app.py:1124-1160` 付近）の各行に
`st.text_input(type="password")` + 「🔑 保存」ボタンを追加。
保存後は `api_key_set` の ✅/❌ 表示のみ更新し、値は再表示しない。

---

## 5. Phase 3 — Provider テンプレートカタログ

新規 `butly_core/llm/provider_catalog.py`:

```python
@dataclass(frozen=True)
class ProviderTemplate:
    id: str                # 既定の connection id
    label: str
    protocol: str = "openai_compat"
    base_url: str | None = None
    api_key_env: str | None = None
    embeddings_supported: bool = True
    extra_headers: dict[str, str] = field(default_factory=dict)
    notes: str | None = None
```

初期エントリ:

| id | label | base_url | api_key_env | embeddings |
|----|-------|----------|-------------|-----------|
| `nanogpt` | NanoGPT (Pay-as-you-go) | `https://nano-gpt.com/api/v1` | `NANOGPT_API_KEY` | 要確認 |
| `nanogpt-sub` | NanoGPT Pro (Subscription) | `https://nano-gpt.com/api/subscription/v1` | `NANOGPT_API_KEY` | false |
| `groq` | Groq | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` | false |
| `openrouter` | OpenRouter | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` | false |
| `together` | Together AI | `https://api.together.xyz/v1` | `TOGETHER_API_KEY` | true |
| `deepinfra` | DeepInfra | `https://api.deepinfra.com/v1/openai` | `DEEPINFRA_API_KEY` | true |

**NanoGPT の扱い（元計画の要件を維持）**:

- Subscription と Pay-as-you-go は**別 Connection**。同じ `NANOGPT_API_KEY` を共有する。
- Subscription Connection には**従量課金へ切り替わるヘッダを付けない**
  （`extra_headers` を空のままにする。UI からも編集させない旨を `notes` に書く）。
- `nanogpt-sub` は `embeddings_supported=false`。

> ⚠️ base_url は実装前に NanoGPT の公式ドキュメントで再確認すること。
> URL が変わっていた場合はカタログの 1 行修正で済む（built-in にしない理由がこれ）。

API: `GET /settings/connection_templates` でカタログを返す。
UI: 「テンプレートから追加」selectbox → 前埋めフォーム → `POST /settings/connections`。
登録後そのまま 4.2 のキー入力へ誘導する。

---

## 6. Phase 4 — capabilities と生成パラメータ

### 6.1 vision 判定

- `Connection` に追加:
  - `default_capabilities: tuple[Capability, ...] = ("chat",)`
  - `model_capabilities: dict[str, tuple[Capability, ...]] = {}`（モデル名 → 上書き）
- 解決順: `MODEL_PRESETS.capabilities` → `connection.model_capabilities[model]`
  → `connection.default_capabilities` → 保守的既定（**vision 無し**）。
- `OpenAICompatAdapter.supports_vision` を staticmethod → インスタンスメソッドに変更。
  呼び出しは `chat/service.py:750` の 1 箇所のみ。
  built-in shim（openai/xai/ollama/gemini）の override は現状維持なので既存テストは無傷。
- UI: Connection 編集フォームに「画像入力に対応」トグルと、モデル別上書き欄を置く。

### 6.2 生成パラメータの差異吸収

- `Connection` に追加:
  - `unsupported_params: tuple[str, ...] = ()`（LiteLLM の `drop_params` 相当）
  - `reasoning_param_style: Literal["openai", "none"] = "openai"`
- `build_chat_completion_kwargs(chat_conf, messages, model_name, connection=None)` に
  connection を渡し、最後に `unsupported_params` を除去する。
  `connection=None` のときは現在と完全に同じ挙動（既存呼び出し・テストを壊さない）。
- `is_reasoning_model()` の prefix 判定はそのまま残し、
  `model_capabilities` に `"reasoning"` が明示されている場合はそちらを優先する。

---

## 7. Phase 5 — テスト

`selection.py` / registry / router に対して書く（Streamlit 非依存）。

**Phase 0-1**
1. `find_current_index` が `(connection_id, model_name)` で一致する
2. 同名 `model_name` を持つ異なる Connection を区別して選択できる
3. `_set_model_ref` 相当が user 定義 connection_id を保存し、`pop` しない
4. グローバル保存で `connection` が更新され、旧値が残らない
5. 候補に無い保存済み値（カスタム）が候補末尾に保持される

**Phase 2**
6. `POST /settings/connections/{id}/api_key` が `api_key_env` の env に保存される
7. リクエストに env 名を含めても無視される（任意 env を書けない）
8. `api_key_env` 無し Connection → 400 / 未登録 → 404 / 空キー → 400 / 改行入り → 400
9. `api_key_env: "PATH"` や小文字 env 名の Connection 登録が 400 で弾かれる
10. `.env` のコメント・他の変数が保存後も保持される
11. レスポンスにキー本体が含まれない

**Phase 3**
12. `nanogpt-sub` テンプレートから登録した Connection が `OpenAICompatAdapter` を返す
13. `nanogpt-sub` の `extra_headers` が空（従量課金ヘッダが付かない）
14. `nanogpt` と `nanogpt-sub` が独立した Connection として共存できる
15. `/settings/model_candidates` の結果に user 定義 Connection のモデルが並ぶ（`/models` はモック）

**Phase 4**
16. 未知モデルの `supports_vision` が既定で False
17. `model_capabilities` で vision を有効化できる
18. `unsupported_params` に指定したキーが kwargs から除去される
19. `connection=None` のとき kwargs が従来と一致する（回帰）

**回帰**
20. 既存の Gemini / OpenAI / xAI / Ollama のテストが全て緑
    （`./scripts/check_before_push.sh`）

---

## 8. 実装順序と依存

```
Phase 0 (selection.py 切り出し)
  └─ Phase 1 (connection_id を失わない)   ← ここまでで NanoGPT が実用可能
       └─ Phase 2 (env 名検証 → APIキー保存)
            └─ Phase 3 (テンプレートカタログ)
                 └─ Phase 4 (capabilities / params)
```

Phase 1 と 2 は独立に見えるが、**2.1 の env 名検証は 2.2 より必ず先**に入れる。
Phase 1 まで完了すれば `user_config.json` を手書きすれば NanoGPT は使える状態になる。

---

## 9. 今回スコープ外（既知の課題として記録）

- **プロセス間の registry 不整合**: `POST /settings/connections` は API プロセスの
  registry にしか反映されない。Streamlit プロセスは `user_config.json` を
  import 時に読むため、追加直後は候補の local fallback に出てこない。
  現状は API 経由の候補取得が主なので実害は小さいが、将来 registry の
  reload エンドポイントを検討する。
- **`/settings/model_candidates` の負荷**: 全 Connection の `/models` を毎回直列に叩く
  （`routers/settings.py:560-614`）。Connection が増えると 1 role あたり数秒。
  サーバ側 TTL キャッシュ or 並列化が必要になったら対応。
- **embedding 次元の変更**: embedding Connection を切り替えると既存ベクトルと次元が
  合わなくなる。`migrate_embeddings.py` の再実行が必要な旨を UI で警告する。
- Router 層のフォールバック / リトライ / コスト計算（LiteLLM Router 相当）。

---

## 10. ドキュメント更新

- 新規 `docs/reference/llm_connections.ja.md`（+ `.md`）:
  Connection の全フィールド、テンプレート一覧、APIキー保存の流れ、
  capabilities の解決順序。
- `docs/reference/FILE_STRUCTURE.ja.md`: `selection.py` / `provider_catalog.py` を追記。
- `user_config.json.example`: `LLM_CONNECTIONS` に NanoGPT の 2 エントリ例を追加
  （APIキーは書かず、`api_key_env` のみ）。
