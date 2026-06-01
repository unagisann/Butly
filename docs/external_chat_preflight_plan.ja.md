# Discord / LINE 連携前の土台整備計画

> 作成日: 2026-06-01
> 対象: Discord / LINE など、FastAPI / Streamlit 以外のチャット入口を追加する前の準備

## 目的

Discord や LINE から Butly に話しかけられるようにする前に、チャット実行経路を「Web UI 専用」から「複数入口で共有できる中核機能」に整える。

この計画では、外部連携そのものの実装はまだ行わない。先に、Discord bot、LINE webhook、将来の CLI / Desktop app などが同じチャット処理を安全に呼べる土台を作る。

## 方針

- FastAPI 採用は維持する。構成としておかしいわけではないため、全面作り直しはしない。
- 既存の `ChatService.execute()` / `execute_stream()` を中核として活かす。
- Discord / LINE 導入前に必要な整理だけを先に行う。
- 個人用ローカルアプリとしての軽さを保ち、過剰な DI / async 化 / 設定全面移行は後回しにする。

## 完了後の理想状態

外部チャット連携側は、HTTP ルータや Streamlit の都合を知らずに、次のような薄い呼び出しだけで Butly の応答を得られる。

```python
request = ChatRequest(
    text=user_text,
    instance_name=instance_name,
    use_rag=True,
    use_web_search=False,
)

result = await runtime.chat(request)
```

その結果、Discord / LINE 側は以下だけに集中できる。

- ユーザー ID と Butly instance の対応づけ
- メッセージ受信イベントの正規化
- 返信制限時間への対応
- 署名検証や bot token 管理
- 添付画像やファイルの取り込み

## Phase 1: 共通 Runtime の導入

### 目的

`dependencies.py` のグローバル状態をいきなり全廃せず、まずは外部入口から扱いやすい `ButlyRuntime` のような小さな実行コンテナを用意する。

### 実施内容

- `butly_core/runtime.py` を追加する。
- `ButlyRuntime` に以下を持たせる。
  - `data_dir`
  - `base_dir`
  - `instances_dir`
  - `instance_manager`
  - `gatekeeper`
  - `mem_block_builder`
  - `instance_store`
- `get_instance_components(instance_name)` を Runtime のメソッドに移す。
- `chat(request)` で `ChatService.execute()` を呼ぶ。
- `chat_stream(request)` で `ChatService.execute_stream()` を呼ぶ。
- 当面は `dependencies.py` から Runtime を参照してもよい。完全削除は目標にしない。

### 完了条件

- FastAPI ルータ以外の Python コードから `ButlyRuntime.chat()` を呼べる。
- 既存の `/chat` と `/chat/stream` の挙動が変わらない。
- 既存テストが通る。

### 優先度

必須。

## Phase 2: FastAPI ルータを Runtime 経由に寄せる

### 目的

`routers/chat.py` が `dependencies.py` の個別グローバルを直接触る状態を減らし、HTTP は「リクエスト変換とレスポンス変換だけ」の薄い層にする。

### 実施内容

- `/chat` は REST 用 DTO を内部 `butly_core.chat.types.ChatRequest` に変換し、`runtime.chat()` を呼ぶ。
- `/chat/stream` は内部 DTO に変換し、`runtime.chat_stream()` を呼ぶ。
- WebSocket `/ws` も可能なら `runtime.chat()` を呼ぶ。
- 添付データの変換処理を小さい helper に切り出し、REST / SSE で重複させない。

### 完了条件

- `routers/chat.py` から `deps.instance_manager`、`deps.gatekeeper`、`deps.mem_block_builder` を直接渡す箇所がなくなる、または最小化される。
- HTTP、SSE、WebSocket の既存テストが通る。
- 外部連携 adapter が FastAPI router を import しなくてもチャット実行できる。

### 優先度

必須。

## Phase 3: ChatService の前処理共通化

### 目的

`execute()` と `execute_stream()` にある Gatekeeper、memory block、provider 解決などの重複を減らし、今後の仕様追加で片方だけ直す事故を防ぐ。

### 実施内容

- `ChatService` 内に内部 helper を追加する。
  - 例: `_prepare_chat_context(...)`
  - 例: `_build_history_fmt(...)`
  - 例: `_run_gatekeeper(...)`
  - 例: `_resolve_context_levels(...)`
- `execute()` と `execute_stream()` が同じ準備結果を使うようにする。
- まずは振る舞いを変えないリファクタに限定する。
- `debug_info` の構造は既存互換を維持する。

### 完了条件

- `execute()` と `execute_stream()` の前処理重複が明確に減っている。
- `tests/test_chat_stream.py`、`tests/test_chat_stream_route.py`、`tests/test_chatservice_connection_routing.py` が通る。
- フルテストが通る。

### 優先度

強く推奨。Discord / LINE 導入前にやる価値が高い。

## Phase 4: 外部チャット用 DTO / 結果モデルの確認

### 目的

Discord / LINE から来るメッセージを、Butly 内部の `ChatRequest` に無理なく変換できるか確認する。

### 実施内容

- `butly_core/chat/types.py` の `ChatRequest` が外部入口にも十分か確認する。
- 必要なら以下の任意フィールドを追加する。
  - `source`: `"web" | "discord" | "line" | ...`
  - `external_user_id`
  - `external_channel_id`
  - `metadata`
- ただし、会話記憶に保存する本文へ外部 ID を混ぜない。
- debug log には source 情報を残せるようにする。

### 完了条件

- Discord / LINE の受信イベントを内部 `ChatRequest` に変換する設計が決まっている。
- 外部 ID を記憶本文に漏らさない方針が明文化されている。
- 既存 Web UI のリクエスト互換性が壊れていない。

### 優先度

必須。

## Phase 5: インスタンス解決方針の決定

### 目的

Discord / LINE のユーザーやチャンネルが、どの Butly instance と会話するのかを決める。

### 実施内容

- 外部ユーザーと instance の対応表をどこに保存するか決める。
- 最初は JSON ファイルでよい。
  - 例: `DATA_DIR/external_accounts.json`
- 対応キー候補を決める。
  - Discord: guild_id + channel_id + user_id
  - LINE: user_id または group_id + user_id
- 未登録ユーザーのデフォルト instance を決める。
- 管理者だけが対応づけを変更できるようにするか決める。

### 完了条件

- 「誰が話しかけたら、どの instance が応答するか」が一意に決まる。
- 未登録時の挙動が決まっている。
- ユーザー ID をログや記憶にどう扱うかの方針が決まっている。

### 優先度

必須。

## Phase 6: 外部連携の返信制約を吸収する方針決定

### 目的

Discord と LINE は返信 API の性質が違うため、Butly の生成時間が長い場合でも破綻しない設計にする。

### 実施内容

- Discord はまず通常応答で始める。
  - 必要なら typing indicator を出す。
  - 長文は分割送信する。
- LINE は webhook の応答制限を考慮する。
  - 即時 reply で間に合わない場合は push message を使う設計にする。
  - まずは「受付メッセージ → 後続 push」の方針でもよい。
- 外部連携では最初から streaming を必須にしない。
- タイムアウト時の文言を決める。

### 完了条件

- Discord / LINE それぞれで、生成が遅い場合の振る舞いが決まっている。
- 長文応答の分割ルールが決まっている。
- 外部 adapter 側で `ChatService.execute()` を使う前提が固まっている。

### 優先度

必須。

## Phase 7: Secret / 設定の置き場所を決める

### 目的

Discord bot token、LINE channel secret / access token を既存の API key 管理と矛盾なく扱う。

### 実施内容

- `.env` に追加する環境変数名を決める。
  - `DISCORD_BOT_TOKEN`
  - `LINE_CHANNEL_SECRET`
  - `LINE_CHANNEL_ACCESS_TOKEN`
- `.env.example` を更新する。
- UI から保存するか、手動 `.env` 設定にするかを決める。
- ログに token が出ないようにする。

### 完了条件

- secret 名と読み込み方法が決まっている。
- `.env.example` に説明がある。
- token を debug log に含めないことが確認されている。

### 優先度

必須。

## Phase 8: 最小テスト計画

### 目的

外部連携を追加しても、Butly 本体のチャット経路を壊さないようにする。

### 実施内容

- Runtime の単体テストを追加する。
  - `runtime.chat()` が `ChatService.execute()` に正しい依存を渡す。
  - 存在しない instance で 404 相当の例外になる。
- `routers/chat.py` の既存テストを維持する。
- `ChatService` 共通化後の回帰テストを追加または既存テストで確認する。
- 外部 adapter 導入時は、Discord / LINE SDK を直接叩かず、イベント正規化と送信分割を mock でテストする。

### 完了条件

- フルテストが通る。
- Runtime 経由のチャット実行がテストされている。
- 外部 adapter を入れる前に、Web UI の `/chat` と `/chat/stream` が壊れていないことを確認できる。

### 優先度

必須。

## 後回しでよい項目

以下は重要だが、Discord / LINE 導入前の必須条件にはしない。

- `main.py` の完全な `create_app()` 化
- `dependencies.py` の完全削除
- SQLite / ファイル I/O の全面 async 化
- pydantic-settings への完全移行
- 複数 worker / 水平スケール対応
- 本格的な job queue 導入
- 外部チャット用の管理 UI

## 推奨実装順

1. `ButlyRuntime` を追加する。
2. `routers/chat.py` を Runtime 経由に寄せる。
3. `ChatService.execute()` / `execute_stream()` の前処理を共通化する。
4. 外部入口用の `ChatRequest` 拡張要否を決める。
5. Discord / LINE ユーザーと instance の対応表を設計する。
6. 返信制約、長文分割、タイムアウト方針を決める。
7. `.env.example` に外部連携 secret を追加する。
8. Runtime と chat route の回帰テストを通す。

## 導入前チェックリスト

- [ ] FastAPI 以外から `ButlyRuntime.chat()` を呼べる
- [ ] `/chat` が Runtime 経由で動く
- [ ] `/chat/stream` が Runtime 経由で動く
- [ ] WebSocket `/ws` のチャット実行経路が Runtime 経由、または移行方針が決まっている
- [ ] `execute()` と `execute_stream()` の前処理重複が減っている
- [ ] 外部 source / user_id / channel_id の扱い方が決まっている
- [ ] 外部ユーザーと instance の対応方針が決まっている
- [ ] Discord / LINE の遅延応答・長文分割方針が決まっている
- [ ] Discord / LINE の secret 名と読み込み方法が決まっている
- [ ] フルテストが通る

## Discord / LINE 実装に入る判断基準

上記チェックリストのうち、少なくとも以下が完了したら外部連携実装に入ってよい。

- `ButlyRuntime.chat()` が存在する
- `/chat` が Runtime 経由で動いている
- 外部ユーザーと instance の対応方針が決まっている
- secret の置き場所が決まっている
- フルテストが通っている

`execute_stream()` の共通化や WebSocket 移行は、作業量が大きければ Discord adapter の直前または直後に回してもよい。ただし LINE まで広げる前には済ませておくのが望ましい。
