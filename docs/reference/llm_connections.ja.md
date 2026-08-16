# LLM Connection / APIキー管理

🌐 **日本語** | [English](llm_connections.md)

> 最終更新: 2026-08-16

## 概要

Butly は、モデルと接続先を別の値として管理する。

- `Connection`: API の接続先、protocol、認証に使う環境変数など
- `model_name`: 接続先へ渡すモデル ID
- `ModelRef`: `connection` と `model_name` の組

たとえば同じモデル ID を OpenAI と NanoGPT の両方が提供していても、
`connection` を保持するため意図した接続先へ送信できる。旧形式の
`{"model_name": "..."}` も互換動作するが、新規設定では必ず
`connection` と `model_name` の両方を保存する。

```json
{
  "AI_CONFIG": {
    "chat": {
      "connection": "nanogpt-sub",
      "model_name": "Qwen/Qwen3-14B"
    }
  }
}
```

## LLMリクエストとCapability解決

生成系の呼び出しは、モデル名による分岐ではなく次の境界を通る。

`Butly Core → CanonicalGenerationRequest → Protocol Adapter → Provider SDK`

- Coreは`temperature`、`max_output_tokens`、`reasoning_effort`などの共通語彙だけを使う。
- `openai_compat`はChat Completionsの`max_tokens` / `max_completion_tokens`へ変換する。
- `gemini_native`は`GenerateContentConfig`、JSON Schema、Thinking Configへ変換する。
- chat、summary、classify、streamは同じCanonical変換を使う。Embeddingは対象外。

Capabilityはフィールド単位で、後の情報を優先して重ねる。

1. protocol Adapterの既定値
2. Butlyの静的モデルプリセット
3. `/models`等が返すprovider metadata / `supported_parameters`
4. 成功した自動補正の観測キャッシュ
5. `LLM_CAPABILITY_OVERRIDES`の手動指定

metadataが無い、または不完全な場合も、未知のモデル名を推測して分岐しない。
明示指定されていないparameterはProvider公式defaultへ委ねられる。Semantic Judgeでは
`reasoning_effort`が未指定なら、Capabilityに公式defaultがあればそれを、reasoning対応
だけ判明していれば`medium`を使う。Capability自体が不明ならparameterを送らず、
Provider公式defaultを使う。ユーザーの明示指定はこの自動既定より優先される。

### 観測キャッシュと安全な1回補正

OpenAI互換endpointが400で`unsupported_parameter`または`unknown_parameter`と
parameter名を明示した場合だけ、出力前に安全な補正を1回行う。現在の補正対象は、
`max_tokens`と`max_completion_tokens`の別名切替、およびユーザーが明示していない
`temperature` / `reasoning_effort`の省略である。曖昧なエラー、認証、429、明示指定の
削除は対象外である。

補正後の呼び出しまで成功した場合だけ、Connection + API送信時のmodel IDをキーに
`DATA_DIR/llm_capabilities.json`へatomic保存する。このファイルはgit管理外で、APIキーや
promptは含まない。2回目も失敗した場合は保存しない。Connectionの変更・削除、または
モデル一覧の明示更新で該当キャッシュを破棄する。

### モデル単位のManual Override

Provider metadataも自動補正も利用できない場合は`user_config.json`で上書きできる。
モデルキーには`model_name_strip_prefix`を除去した、実際にAPIへ送るIDを指定する。

```json
{
  "LLM_CAPABILITY_OVERRIDES": {
    "nanogpt-sub": {
      "gpt-5.6-luna": {
        "token_limit_parameter": "max_completion_tokens",
        "supports_reasoning": true,
        "reasoning_efforts": ["none", "low", "medium", "high", "xhigh", "max"],
        "default_reasoning_effort": "medium",
        "temperature_supported": false,
        "structured_outputs_supported": true
      }
    }
  }
}
```

overrideは必要なフィールドだけ指定できる。利用可能な`token_limit_parameter`は
`max_tokens`、`max_completion_tokens`、`max_output_tokens`である。

## Connection のフィールド

ユーザー定義 Connection は `user_config.json` の `LLM_CONNECTIONS` に保存される。
APIキーの値はここには保存しない。

| フィールド | 型 / 既定値 | 説明 |
|---|---|---|
| `id` | string / 必須 | Connection 識別子。小文字英数字で開始し、小文字英数字・`_`・`-`のみ、最大64文字 |
| `protocol` | string / 必須 | `openai_compat` または `gemini_native` |
| `base_url` | string or null | SDK / Adapter が使う絶対 URL |
| `base_url_env` | string or null | 設定時は、この環境変数の値で `base_url` を上書き |
| `api_key_env` | string or null | APIキーを読む環境変数名。認証不要なら `null` |
| `api_key_fallback_envs` | string[] / `[]` | 主環境変数が未設定のとき、順番に試す代替名 |
| `label` | string or null | Web Console の表示名。未設定なら `id` |
| `extra_headers` | object / `{}` | 全リクエストへ加える固定ヘッダー。秘密値は入れない |
| `embeddings_supported` | boolean / `true` | この接続で embedding を許可するか |
| `embedding_model_env` | string or null | embedding モデル ID を上書きする環境変数名 |
| `default_embedding_model` | string or null | embedding モデルの最終フォールバック |
| `model_name_strip_prefix` | string or null | API送信前にモデル ID から除去する接頭辞 |

`google`、`openai`、`xai`、`ollama` は built-in Connection であり、
ユーザー設定からの上書き・削除はできない。OpenAI互換サービスは通常、
Providerクラスを追加せず `openai_compat` のユーザー定義 Connection として追加する。

built-in の接続先を変えたい場合は `base_url_env` を使う。Ollama を別PCで
動かしている場合は設定画面の「Ollama (ローカルLLM)」で接続先URLを保存すると、
`DATA_DIR/.env` の `OLLAMA_BASE_URL` に書き込まれる（`POST /settings/ollama_url`）。
UI と接続テストは root 形（`http://<ホスト>:11434`）を扱い、保存時に OpenAI 互換の
`/v1` を付ける。`Connection.resolve_base_url()` は毎回 env を読むため再起動は不要。

## Web Console での操作

### Connection とAPIキー

1. 設定画面の「Connection / APIキー管理」を開く。
2. 既存 Connection の状態と接続先を確認する。
3. 必要なら「Connectionを追加」でテンプレートまたはカスタムを選ぶ。
4. 登録後、同じ一覧でAPIキーを入力し「保存」を押す。
5. 「疎通テスト」でモデル一覧を取得できることを確認する。

APIキー入力欄は常に空で表示され、保存済みの値を読み戻さない。
表示するのは設定済みかどうかだけである。

### プロバイダーからモデルを選ぶ

グローバル設定とインスタンス設定の各ロールでは、次の順に選択する。

1. プロバイダー / Connection
2. その Connection が提供するモデル

候補はモデルプリセット、現在保存中の値、接続先の `/models` から構成される。
一覧に無いモデルは「モデルIDを直接入力」で指定できる。保存時は
`connection` と `model_name` の両方が保存される。

接続先のモデル一覧は選択中のConnectionだけ遅延取得し、Connection単位で10分間
キャッシュする。Chat、Summaryなど複数ロールで同じConnectionを使う場合は一覧を
再利用し、ロールを切り替えるたびに外部`/models`を呼ばない。Connection・
APIキー・Ollama URLを変更した場合は該当キャッシュを自動破棄する。
プロバイダー側の変更をすぐ反映する場合は、画面の「モデル一覧を更新」または
`POST /settings/model_catalog/refresh`を使用する。

Embeddingロールでは `embeddings_supported=false` の Connection は選択候補から
除外される。Embedding Connectionまたはモデルを変更した場合、既存ベクトルと
次元が一致しなくなる可能性があるため `migrate_embeddings.py` で再生成する。

## APIキーの保存と秘密情報

APIキーはバックエンドが実行時の `DATA_DIR/.env` に保存し、同時に現在の
プロセス環境へ反映する。既存 `.env` のコメント、空行、無関係な設定は保持する。
同じ環境変数の重複行は保存時に1行へまとめる。

クライアントは Connection ID とキーの値だけを送る。書き込み先の環境変数名は
登録済み Connection からサーバー側で解決するため、APIキー保存APIで任意の
環境変数名を指定することはできない。

レスポンスと Connection 一覧は秘密値を返さず、`api_key_set` と
`affected_connections` だけを返す。ログ、`user_config.json`、スクリーンショット、
Issue、コミットへキーを含めないこと。

同じ `api_key_env` を複数の Connection が共有する場合、1つから保存したキーは
全該当 Connection へ反映される。解除は対象 Connection の主環境変数と
fallback環境変数を削除するため、共有先にも影響する。

Connection の削除とAPIキーの解除は別操作である。不要な秘密値も消す場合は、
その環境変数を使う他の Connection がないことを確認してから先に解除する。

## 入力検証

登録時と設定読込時に次を検証する。

- `id`: `^[a-z0-9][a-z0-9_-]{0,63}$`
- 環境変数名: `^[A-Z_][A-Z0-9_]*$`
- `HOME`、`PATH`、`PYTHONPATH` などの予約済み実行環境名は使用不可
- `base_url`: 絶対 `http://` または `https://` URL
- `protocol`: `openai_compat` または `gemini_native`
- `extra_headers`: キーと値は文字列で、改行を含まない
- APIキー: 空文字と制御文字を含まない

ユーザー定義 Connection では built-in ID を使用できない。Web Console の
カスタム追加フォームは現在 `openai_compat` を対象とする。

## 削除時の参照保護

ユーザー定義 Connection がグローバル `AI_CONFIG` またはインスタンスの
`config.json` から参照されている場合、通常の削除は `409 Conflict` になる。
先に各モデル割り当てを別 Connection へ変更する。

`DELETE /settings/connections/{connection_id}?force=true` で強制削除できるが、
参照元の設定は自動修正されない。壊れた `ModelRef` が残るため、参照箇所を
把握した移行時以外は使用しない。

## 正式デスクトップ UI の preflight

正式 UI は read-only の `GET /api/v1/preflight` を使い、active chat role と
embedding role が実際に利用可能かを確認する。これは設定を変更する legacy の
Connection 管理 API とは別の起動前診断である。

- 必須 role が参照する Connection を並列・timeout 付きで検査する。
- Ollama (`openai_compat`) は設定した root URL の native `/api/tags` を使う。
- それ以外は protocol に応じた安全な model-list probe を使う。
- embedding は model list の名前一致だけでなく、固定短文を実際に embed し、
  vector が非空かつ有限であることと dimension を確認する。
- 応答は `ready` / `degraded` / `unavailable` と個別 reason code を返すが、API key、
  base URL、header、provider の raw error は返さない。

preflight は connection を保存・修正せず、結果も credential の正本にしない。
設定変更は当面 Streamlit の legacy Settings API、将来は versioned settings API が担う。

## Legacy Settings API

以下は Streamlit Web Console が利用する未versionedの互換routeである。

| Method | Route | 用途 |
|---|---|---|
| `GET` | `/settings/connections` | built-in + ユーザー定義 Connection とキー設定状態 |
| `GET` | `/settings/connection_templates` | 秘密値を含まないプロバイダーテンプレート |
| `POST` | `/settings/connections` | ユーザー定義 Connection の追加または同一IDの更新 |
| `DELETE` | `/settings/connections/{connection_id}` | ユーザー定義 Connection の削除。参照中は409 |
| `POST` | `/settings/connections/{connection_id}/api_key` | `{"api_key": "..."}` を保存 |
| `DELETE` | `/settings/connections/{connection_id}/api_key` | Connectionが参照するキーを解除 |
| `POST` | `/settings/test_connection` | Connectionの疎通とモデル一覧を確認 |
| `GET` | `/settings/model_candidates?role=...&connection_id=...` | ロール別候補。`connection_id`指定時はその接続先だけ動的取得 |
| `POST` | `/settings/model_catalog/refresh` | 全Connectionまたは指定Connectionのモデル一覧キャッシュを破棄 |

APIキー保存・解除レスポンスにキー値は含まれない。

## プロバイダーテンプレート

テンプレートは初期値を入力するだけで、登録後は通常のユーザー定義 Connection
として扱う。

| ID | Base URL | APIキー環境変数 | Embedding |
|---|---|---|---|
| `nanogpt-sub` | `https://nano-gpt.com/api/subscription/v1` | `NANOGPT_API_KEY` | 非対応 |
| `nanogpt` | `https://nano-gpt.com/api/v1` | `NANOGPT_API_KEY` | 対応 |
| `groq` | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` | 非対応 |
| `openrouter` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` | 非対応 |
| `together` | `https://api.together.xyz/v1` | `TOGETHER_API_KEY` | 対応 |
| `deepinfra` | `https://api.deepinfra.com/v1/openai` | `DEEPINFRA_API_KEY` | 対応 |

外部サービスのURL、モデル提供状況、料金は変わり得る。登録前に各サービスの
公式仕様を確認する。

## ストリーミングの即時リトライ

OpenAI 互換プロバイダーのストリームが**出力前に即座に失敗**した場合、
`butly_core/llm/_openai_compat.py` が **1 回だけ**引き直す（`MAX_STREAM_ATTEMPTS`）。
上流が SSE を開いたまま本文を返さず `Upstream returned an empty response` で
落ちる事象が実際に起きており、同じ入力の手動再送で成功するため transient と
扱える。手動再送は Gatekeeper の分類からやり直しになるので、provider 層で
引き直すほうが待ち時間が短い。

再試行するのは待ち時間をほとんど増やさない種類に限る（`is_retryable_stream_error`）。

| 種類 | 再試行 | 理由 |
|---|---|---|
| 上流の空応答（status を持たない `APIError`） | する | 即座に失敗し、再試行で成功する |
| 接続断（`APIConnectionError`） | する | 同上 |
| 5xx（`InternalServerError`） | する | 上流の一時障害 |
| **timeout（`APITimeoutError`）** | **しない** | 待ち時間が倍になる。体感を最も損なう |
| 429 / 401 / 一般の400（`APIStatusError`） | しない | 間を置かない再試行が無意味か有害 |

**1 文字でも送出済みなら再試行しない。**二重生成を避けるための条件で、
呼び出し側が `full_text` の空を確認してから引き直す。2 回目も失敗したときだけ
`error` event を出すので、UI から見た契約（`metadata` → `chunk` → `done` /
終端 `error`）は変わらない。再試行はサーバーログにのみ残る。

LoCoMo 評価側の QA リトライ（3 回・1/2/4 秒バックオフ）とは別物で、
対話では待たせないことを優先して 1 回・即時とする。
明確なunsupported parameterの1回補正も同じ「出力前・最大2回」の枠を共有する。
Semantic Judgeでこの補正後も同じ設定エラーが残る場合は、全設問で失敗を繰り返さず
評価runを即時停止する。

## NanoGPT

### Pay-as-you-go と Pro を分ける

NanoGPT は用途別に2つの Connectionとして登録する。

- `nanogpt`: Pay-as-you-go。`https://nano-gpt.com/api/v1`
- `nanogpt-sub`: Pro / Subscription対象モデルに限定する。
  `https://nano-gpt.com/api/subscription/v1`

両方が同じ `NANOGPT_API_KEY` を共有するため、どちらかの画面で保存すれば
両方が設定済みになる。片方から解除すると両方へ影響する。

同じ表示名のモデルでも、NanoGPT上のroute/model IDが異なる場合がある。
サブスク対象かどうかは表示名ではなく、`/api/subscription/v1/models` が返す
正確な`id`で判定する。`/api/paid/v1/models`だけにある同名IDとは置き換えない。

### Proで従量課金overrideを使わない

`nanogpt-sub` は購読対象に限定するための Connection である。次の指定は
リクエストをPay-as-you-go扱いにして購読対象外の課金を発生させ得るため、
追加しない。

- `X-Provider`
- `X-Billing-Mode: paygo`
- bodyの `billing_mode` / `billingMode`
- provider指定や `:fast` / `:cheap` などのrouting suffix

Butly の `nanogpt-sub` テンプレートは `extra_headers={}` のままで、
`embeddings_supported=false` である。

### PAYG embeddingは正確なモデルIDを直接指定する

NanoGPT の通常の `/api/v1/models` はテキスト生成モデルの一覧であり、
embeddingの正本は `/api/v1/embedding-models` である。そのためWeb Consoleの
Embeddingモデル候補にNanoGPTのembeddingモデルが自動表示されない場合がある。

`nanogpt` Connectionを選び、「モデルIDを直接入力」へ
`/api/v1/embedding-models` が返す `id` を正確に入力する。
例: `text-embedding-3-small`、`BAAI/bge-m3`、
`Qwen/Qwen3-Embedding-0.6B`。`nanogpt/` のようなButly独自prefixは付けない。

モデル一覧と料金は変わるため、例を固定的な提供保証として扱わない。

### 大きなモデル一覧も省略しない

NanoGPTのテキストモデル一覧は200件を超える。Butlyは`/models`が返す全件を
Connection単位でキャッシュし、選択中のConnectionの候補へ反映する。候補の後半に
追加されたモデルも、先頭件数によって切り捨てない。

### NanoGPT公式資料

- [Text Generation](https://docs.nano-gpt.com/api-reference/text-generation)
- [Chat Completion](https://docs.nano-gpt.com/api-reference/endpoint/chat-completion)
- [Models](https://docs.nano-gpt.com/api-reference/endpoint/models)
- [Embeddings](https://docs.nano-gpt.com/api-reference/endpoint/embeddings)
- [Pay-As-You-Go Billing Override](https://docs.nano-gpt.com/api-reference/miscellaneous/billing-override)
