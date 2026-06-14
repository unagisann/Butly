# LINE 連携実装計画

> 作成日: 2026-06-14
> 対象: Butly の LINE adapter 実装。Discord adapter（実装済）の分離思想を踏襲する。
> 位置づけ: 設計段階ドキュメント（`_` プレフィックス）。実装は Claude Code に引き渡す。
> 前提ドキュメント: `external_chat_design_decisions.ja.md` Phase 5/6、`discord_integration_plan.ja.md`

## 目的

LINE から Butly に話しかけられるようにする。

チャット生成・記憶・Gatekeeper・RAG・Sleeptime などの中核処理は、Web チャットや Discord と同じ `ButlyRuntime.chat()` 経路を使う。LINE 側では、webhook 受信・署名検証・イベント正規化・instance 解決・返信整形・LINE API 送信（reply / push）だけを担当する。

## 確定した方針（本計画の前提）

| 項目 | 決定 |
| --- | --- |
| webhook 外部公開 | **Cloudflare Tunnel**（Pi 常設） |
| 返信方式 | **reply 優先ハイブリッド**（reply は quota 無料なので原則 reply） |
| reply timeout | **内部 8 秒**（まず短めに設定。reply token の時間制限には依存しない） |
| timeout 超過時 | **ack reply（受付メッセージ）→ 生成完了後に push 本回答** |
| 未承認ユーザー | **pairing 必須**。未承認には応答せず pairing code を案内する |
| pairing 承認 | **Streamlit UI から操作**。ただしロジックは `butly_core` + FastAPI 側に置き、Streamlit は薄い UI に留める |

## Discord との決定的な非対称

Discord は Gateway への**アウトバウンド**接続（別プロセス `run_discord_bot.py`、NAT 背後で公開不要）だった。
LINE は webhook への**インバウンド** HTTP POST を受ける必要がある。これが「Discord を先にやった理由」そのものであり、以下の非対称を**正しい非対称として受容**する。

- **起動方式**: Discord は別プロセス。LINE は webhook = HTTP サーバーが必須なので、新規プロセスを立てず既存 `main.py`（FastAPI）に router として相乗りする。
- **到達手段**: Cloudflare Tunnel で Pi の FastAPI を公開する。webhook URL は `https://<tunnel-domain>/line/webhook`。
- **分離思想は別レイヤーで揃える**: プロセス構成は揃わなくても、`discord_adapter.py` が実践した「SDK 非依存の純粋関数 + I/O 層の分離」は LINE でも踏襲する（後述）。

## 利用ライブラリ

LINE API との接続には `line-bot-sdk`（v3）を使う。

- reply / push 送信、メッセージオブジェクト構築、webhook イベントのパースを自前実装しない。
- ただし**署名検証は raw body を自前で扱う**（SDK の parser に渡す前段でも、生バイト列での HMAC 検証を明示的に行う。理由は後述）。
- 依存は optional にする。
  - 例: `requirements-line.txt`
  - 将来 `pyproject.toml` に移行する場合は extra dependency にする。
- token 未設定なら LINE 機能は無効化し、FastAPI 本体は通常起動できること。

## 想定ディレクトリ

Discord と同じく `butly_core/external/` に薄く足す。

```text
butly_core/
  external/
    __init__.py
    account_mapping.py      # 既存（ExternalAccountStore, line キー解決済）
    reply_profiles.py       # 既存（LINE_PROFILE を追記）
    message_splitter.py     # 既存（5000 字対応を追記）
    discord_adapter.py      # 既存
    line_adapter.py         # 新規: SDK 非依存の純粋関数
    pairing.py              # 新規: PairingStore（pending 管理・承認・有効期限）

routers/
    line.py                 # 新規: webhook 受け口（即200・署名検証・dedup・BackgroundTask）
    pairing.py              # 新規: pairing 承認 API（pending 取得・承認・却下）
```

### 役割

| ファイル | 役割 |
| --- | --- |
| `line_adapter.py` | webhook event 正規化 → `ChatRequest`、reply/push 判定、署名検証ヘルパー、送信メッセージ整形（SDK 非依存・単体テスト対象） |
| `pairing.py` | pairing code の発行・検証・有効期限管理、承認時に `ExternalAccountStore` へ書き込み（純粋ロジック） |
| `routers/line.py` | FastAPI webhook 入口。raw body 取得 → 署名検証 → 即200 → `BackgroundTasks` で非同期処理。event ID で dedup |
| `routers/pairing.py` | pending 一覧・承認・却下の HTTP エンドポイント。Streamlit / CLI 双方から叩ける |

## チャット処理フロー

```text
LINE webhook (POST /line/webhook)
  ↓
routers/line.py
  - raw body 取得（pydantic パース前）
  - X-Line-Signature を raw body で HMAC-SHA256 検証
  - webhook event ID で dedup（処理済みはスキップ）
  - 即 200 を返す
  ↓ （BackgroundTask）
line_adapter: event 正規化
  ↓
PairingStore: external_user_id が承認済みか確認
  ├─ 未承認 → pairing code 発行 → 案内メッセージを reply（チャット処理に進まない）
  └─ 承認済み ↓
ExternalAccountStore で instance_name 解決
  （group:{group_id}:{user_id} → user:{user_id}）
  ↓
ChatRequest(
  text=message,
  instance_name=resolved_instance,
  source="line",
  external_user_id=user_id,
  external_channel_id=group_id or None,
  metadata={"reply_profile": "line"}
)
  ↓
runtime.chat(request)  ← 内部 8 秒の締め切りで待つ
  ├─ 8 秒以内に完了 → reply API で本回答（quota 無料）
  └─ 8 秒超過 → ack reply（受付）→ 生成継続 → 完了後 push API で本回答
  ↓
LINE_PROFILE で整形 → 5000 字で分割 → 送信
```

## 返信方針（reply 優先ハイブリッド）

### 基本

- 原則 **reply**。Reply API のメッセージは quota にカウントされないため、push は遅延時のフォールバックに限定する。
- reply token は **1 回限り・webhook 受信後おおむね 1 分以内**。LINE 公式は「時間制限に依存した実装をするな、できるだけ早く使え」と明示しているため、**reply token の有効期限には賭けず、Butly 内部の短い締め切り（まず 8 秒）で reply / push を切り替える**。

### 8 秒判定

```text
result = await wait_for(runtime.chat(request), timeout=8s)
  - 完了      → reply(reply_token, 本回答)
  - timeout   → reply(reply_token, "ただいま考えています。少しお待ちください。")
                生成は継続させ、完了後に push(to=user_or_group_id, 本回答)
```

実装上の注意:

- timeout してもチャット生成タスクは**キャンセルしない**。`wait_for` は timeout 時にタスクを cancel するため、`asyncio.shield` で保護するか、生成タスクを別途 `create_task` で保持し、timeout は「ack を出すトリガ」としてのみ使う。
- どちらの分岐でも reply token は 1 回だけ消費する（本回答 or ack のどちらか）。
- push の宛先（userId / 1:1 でない場合は groupId）は、ack を出す時点で必ず確保しておく。
- 失敗時の文言:
  - 受付（生成中）: 「ただいま考えています。少しお待ちください。」
  - 生成失敗: 「申し訳ありません、応答の生成に失敗しました。」

### 長文分割

- LINE のテキスト上限は **5000 字（UTF-16 code unit 基準）**。初期版は安全余白を取り、hard limit を **4500** に下げる（理由は「追加の安全方針と運用上の限界」を参照）。
- reply は 1 リクエスト最大 5 メッセージオブジェクト。これを超える分割が必要なら、超過分は push で続きを送る。
- 分割境界の優先順位（Discord と共通）: 空行 → 改行 → 句点 → 強制カット。

### loading animation（任意・後回し可）

- 1:1 トークに限り `chat.loading.start` が使える。ack テキストの代替/併用として将来検討する。グループでは使えないため初期実装では必須にしない。

## pairing 方針（pairing 必須）

### 基本

- **未承認の external_user には応答しない**。代わりに pairing code を発行し、案内メッセージを reply する。
- これは既存の「未登録でも `default_instance` で応答」方針を、LINE に限り上書きする。LINE は不特定多数から到達し得るため、明示承認を必須にする。
- `default_instance` フォールバックは LINE では使わない（pairing 必須ガードが優先）。
- **グループでは pairing code を出さない**。pairing 案内は 1:1 トークに限る（後述「追加の安全方針と運用上の限界」）。

### PairingStore（`butly_core/external/pairing.py`）

- pending pairing を管理する: `code → {source, external_user_id, external_channel_id, created_at, expires_at}`。
- 保存先は最初は単一 JSON（`DATA_DIR/pending_pairings.json`）。SQLite 化は後回し。
- code 形式: 6 桁（数字 or 英数字）。有効期限はまず 10 分程度。期限切れは無効。
- **再利用**: 同一 `source + external_user_id` に有効な pending がある場合は、新しい code を発行せず既存 code を再案内する（code の乱発と pending の肥大化を防ぐ）。
- 承認時に `ExternalAccountStore`（`external_accounts.json`）へ `line.user:{user_id}: <instance>` を書き込み、pending から削除する。
- 純粋ロジックのみ。HTTP も Streamlit も知らない。

### 承認フロー（ロジックは FastAPI / butly_core、操作は Streamlit）

```text
未承認ユーザーが LINE で発話
  ↓
PairingStore.issue() で code 発行
  ↓
"連携コード: 123456 を管理画面で承認してください" を reply
  ↓
（管理者）Streamlit の連携管理ページを開く
  ↓
Streamlit → GET /pairing/pending を叩いて一覧表示
  ↓
承認ボタン → POST /pairing/approve {code, instance_name}
  ↓
routers/pairing.py → PairingStore.approve() → ExternalAccountStore 更新
  ↓
以降、そのユーザーは通常どおり Butly と会話できる
```

- **Streamlit にはロジックを置かない**。pending 取得と承認/却下は HTTP エンドポイント（`/pairing/pending`、`/pairing/approve`、`/pairing/reject`）越しに行う。
- 同じエンドポイントを将来 CLI からも叩ける（`tools/pairing_cli.py` 等は後回しでよいが、API 設計はこれを妨げないこと）。
- 承認時に instance を選べるようにする（未指定なら `default_instance`）。

## 署名検証・即200・非同期・冪等性

- **署名検証は raw body 必須**。`X-Line-Signature` は生のリクエストボディに対する HMAC-SHA256。FastAPI で pydantic にパースする前に `await request.body()` で生バイト列を取得して検証する。パース済み dict を再シリアライズすると署名が一致しない。
  - 検証前に body サイズ上限とタイムアウトを設け、巨大ボディで処理を始めない。
- **即 200**: 署名・ペイロード検証が通ったら即座に 200 を返し、チャット処理は `BackgroundTasks` で非同期に行う。
- **冪等性（dedup）**: 即 200 設計では通常リトライは起きないが、webhook 再送に備え、webhook event の ID で処理済みを弾く。再送 webhook の reply token も受信後 1 分以内なら有効なため、dedup がないと同一発話を二重処理し得る（Sleeptime の二重消費も防げる）。
  - まずメモリ上の LRU set で十分。永続化は後回し。
- **LINE Console の Verify 対応**: 検証用の webhook（署名付き・空 events）に対しても 200 を返せること。events が空なら何もせず 200。

## 記憶方針

Discord と同じ。LINE からの会話も通常チャットと同じ記憶機構に保存する。

- `memory.save_single_turn(...)` は通常どおり実行する。
- `request.text` に LINE user id / group id は混ぜない。外部 ID は instance 解決と pairing 判定にだけ使う。
- debug log には `source="line"` のみ残す。外部 ID は残さない。
- channel 由来情報（LINE であること）は `build_context_prefix()` に ambient frame として渡す。`system_instruction` には入れない／長期記憶にも保存しない。source が違っても同一 instance / Key Memory に収束させる。

## 返信プロファイル

`reply_profiles.py` に `LINE_PROFILE` を追加する。

- hard limit: 4500 字（LINE 上限 5000 に対する安全余白。UTF-16 カウント差分を吸収）。
- soft limit: チャットアプリ向けにやや短め（例: 1200〜2000 字程度）。
- 生成前の style hint は Discord と同様、会話記憶本文に混ぜず専用 context として渡す。

## Secret 管理

`.env` に以下（`.env.example` には追記済み）。

```env
LINE_CHANNEL_SECRET=...
LINE_CHANNEL_ACCESS_TOKEN=...
```

- token / secret は debug log に出さない。
- 未設定なら LINE webhook ルートを無効化（登録しない or 503）し、FastAPI 本体は通常起動できること。

## 追加の安全方針と運用上の限界

### 管理 API の外部公開禁止

Cloudflare Tunnel で外部公開するのは **`/line/webhook` のみ**とする。`/pairing/*` はローカル管理用エンドポイントであり、外部から到達させない。

- Tunnel の ingress 設定で公開パスを `/line/webhook` に限定する。
- `/chat`、`/settings/*`、`/pairing/*` などが Tunnel 経由で叩けないことを構築時に確認する。
- やむを得ず同一 FastAPI を公開する場合は、`/pairing/*` を管理トークンまたは Cloudflare Access 等で保護する。

### グループチャットの初期制限

初期版は **1:1 トークを主対象**とする。

- グループでは未承認ユーザーに pairing code を**表示しない**（グループ内の他者に code が見える／不特定多数への code 乱発を避ける）。
- グループ応答は、明示 prefix / mention 相当の条件を満たす場合のみ、または後続 Phase で対応する。
- `ExternalAccountStore` の `group:{group_id}:{user_id}` 解決ロジック自体は残すが、初期版での発火条件は絞る。

### pairing code 再利用

同一 `source + external_user_id` に有効な pending pairing がある場合、新しい code を発行せず既存 code を再案内する。code の乱発と pending の肥大化を防ぐ（`PairingStore.issue()` に実装）。

### LINE 文字数カウント（UTF-16）

LINE の文字数上限は **UTF-16 code unit 基準**。Python の `len()`（コードポイント単位）とはサロゲートペア（絵文字・一部の漢字等）でずれ、`len()` で 5000 ちょうどに切ると実際は超過し得る。

- 初期版は LINE hard limit を **4500** 程度に下げ、安全余白を取る。
- 将来必要になれば、LINE 専用の UTF-16 length splitter を実装する。

### BackgroundTasks の限界

初期版は FastAPI `BackgroundTasks` を使うが、これは**配送保証のあるジョブキューではない**。プロセス再起動やクラッシュで処理中タスク（特に timeout 後の push 本回答）は失われ得る。

- 移行トリガー: 長時間処理・エージェント連携・確実な push 配送が必要になった段階。
- そのとき SQLite 等の**永続ジョブキュー**へ移行する。Butly の「トリガーが出るまで複雑性を遅延」方針に従い、初期版では導入しない。

## 実装フェーズ

### Phase L1: optional dependency と疎通

- `requirements-line.txt` を追加（`line-bot-sdk` v3）。
- Cloudflare Tunnel をセットアップし、`https://<tunnel-domain>/line/webhook` が Pi の FastAPI に届くことを確認。
- `routers/line.py` を最小実装（受信 → 即 200 を返すだけ）。

完了条件: LINE Console の Verify が成功する。optional 依存未導入でも本体が起動する。

### Phase L2: 署名検証 + 即200 + dedup

- raw body 取得 → `X-Line-Signature` 検証。
- webhook event ID で dedup。
- 検証通過後に `BackgroundTasks` へ処理を委譲する骨格。

完了条件: 不正署名を弾く。正当な webhook で即 200 を返す。再送で二重処理しない。

### Phase L3: イベント正規化（`line_adapter.py` 純粋関数）

- text message event を正規化し `ChatRequest(source="line", ...)` を組み立てる純粋関数。
- reply/push 判定ヘルパー、5000 字分割呼び出し。
- SDK 非依存で単体テスト可能にする。

完了条件: イベント → `ChatRequest` 変換が mock でテストできる。

### Phase L4: 送信ラッパ + reply 優先ハイブリッド

- `line-bot-sdk` 経由の reply / push 送信ラッパ。
- 内部 8 秒で `runtime.chat()` を待ち、完了→reply / 超過→ack reply + push。
- timeout で生成タスクをキャンセルしない実装（shield / 別タスク保持）。

完了条件: 短い応答は reply 1 発、遅い応答は ack→push になる。push 宛先が確保されている。

### Phase L5: pairing（必須ガード + 承認 API + Streamlit UI）

- `PairingStore` 実装（発行・検証・有効期限・承認）。
- `routers/pairing.py`（pending / approve / reject）。
- 未承認ユーザーは pairing code 案内のみ、チャット処理に進ませない。
- Streamlit に連携管理ページ（ロジックなし、エンドポイントを叩くだけ）。

完了条件: 未承認は応答しない。承認後に会話できる。承認操作が Streamlit から行える。code 期限切れが無効になる。

### Phase L6: reply profile / 文字数分割

- `LINE_PROFILE` 追加、`message_splitter` の hard limit 4500 対応。

完了条件: LINE の文字数上限を超えない。reply 5 メッセージ超過分が push で続く。

### Phase L7: テスト

- 署名検証（正/不正）、event 正規化、reply/push 判定（8 秒分岐）、5000 字分割、dedup、pairing 発行/承認/期限切れ の単体テスト。
- LINE SDK は mock。実 LINE への接続は手動スモークテスト。

完了条件: 既存フルテストが通る。LINE なし環境でも通常テストが通る。

## テスト方針

### 自動テスト対象

- raw body での署名検証（一致／不一致）
- 空 events での 200（Verify 対応）
- 同一 event ID の dedup
- text event → `ChatRequest(source="line")` 正規化
- 8 秒以内＝reply / 超過＝ack→push の分岐（時間は mock）
- 4500 字での分割、reply 5 メッセージ超過時の push 継続
- pairing code の発行・承認・却下・有効期限切れ
- 未承認ユーザーがチャット処理に進まない（pairing 案内のみ）

### 手動テスト対象

- Cloudflare Tunnel 経由の Console Verify
- 未承認ユーザーへの pairing code 案内
- Streamlit からの承認 → 会話可能化
- 短い応答（reply 1 発）と遅い応答（ack→push）
- 長文分割送信
- 記憶保存と次ターンでの参照
- 1:1 トークでの instance 解決（グループは初期版では応答条件を制限。「追加の安全方針と運用上の限界」参照）

## 初期スコープ外

- streaming 表示
- リッチメニュー
- 画像 / スタンプ / 位置情報など text 以外の高度な受信
- Flex メッセージの作り込み
- pairing 承認の CLI（API 設計だけ妨げないようにする。実装は後回し）
- pending_pairings / external_accounts の SQLite 化
- 複数 LINE チャネル / 複数 access token

## 完了判定

以下を満たしたら LINE 連携の初期版は完了。

- Cloudflare Tunnel 経由で LINE Console の Verify が成功する。
- 不正署名を弾き、正当な webhook で即 200 を返す。再送で二重処理しない。
- 未承認ユーザーには pairing code を案内し、チャット処理に進ませない。
- Streamlit から pairing を承認でき、承認後に通常会話できる（ロジックは FastAPI / `butly_core` 側）。
- 短い応答は reply 1 発、8 秒を超える応答は ack→push になる。
- LINE 会話が通常の記憶機構に保存され、`source="line"` で扱われる。
- LINE の文字数上限（5000 UTF-16 code unit）に対し、hard limit 4500 で安全に収める。
- optional dependency 未導入でも FastAPI / Streamlit 本体は壊れない。
- フルテストが通る。

## 補足: reply token の仕様について（実装時の注意）

- reply token は 1 回限り・受信後おおむね 1 分以内。ただし LINE 公式は時間制限が予告なく変わり得るとし、**時間制限に依存しない実装**を求めている。本計画の「内部 8 秒締め切り」はこの方針に沿う（外部の 1 分に賭けず、自前の短い締め切りで判断する）。
- 二次情報には「reply token 約 30 秒」とする記述もあるが、これは公式と食い違う。実装は公式仕様と「依存しない」原則に従うこと。
