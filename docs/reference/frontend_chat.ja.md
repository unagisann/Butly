# 正式デスクトップ Chat UI

この文書は、Tauri v2 + React + TypeScript で提供する Butly の正式 Chat UI と、
`/api/v1` の接続契約を定義する。LoCoMo、日本語 A/B、検索比較などの評価機能は
正式 UI へ移さず、Streamlit の Evaluation Web Console に残す。

## 構成

- Tauri が FastAPI sidecar の起動、動的 port / token の受け渡し、再起動と終了を管理する。
- React は `frontend/src/features/` の instance、chat、preflight ごとの feature slice で構成する。
- 通常の JSON API は OpenAPI から生成した TypeScript client と型を使う。
- SSE framing だけは `frontend/src/api/sse.ts` で解析し、OpenAPI の event DTO に変換する。
- UI 文言は日本語と英語を同じ辞書で管理し、実行中でも切り替えられる。

## 画面と状態

左ペインで instance と connection / embedding preflight を確認し、メインペインで
履歴、stream 中の応答、引用元、添付画像を表示する。developer mode で許可された
Gatekeeper / RAG は composer 付近の折りたたみ診断に安全な要約だけを表示する。prompt、生成 raw
response、API key、接続 URL、ローカル path は UI 用 DTO に含めない。

Chat は少なくとも次の状態を区別する。

- backend 起動中、接続済み、切断、再接続中、sidecar crash、version mismatch
- 履歴読み込み中、空、取得失敗、再取得
- 送信待ち、生成中、完了、キャンセル、再送可能な失敗、再送不可の失敗
- preflight の ready、degraded、unavailable

sidecar の process 状態と HTTP 到達性は別に扱う。ready 後も API を定期確認し、
通信失敗時は composer を無効化して再接続操作を表示する。instance や入力途中の
内容は、一時的な切断だけでは破棄しない。

## Chat API

履歴は `GET /api/v1/instances/{name}/messages`、通常応答は
`POST /api/v1/chat`、正式 UI の生成は `POST /api/v1/chat/stream` を使う。
各送信で UI が `client_request_id` を採番する。同じ ID と同じ payload の
再送は実行中 request へ再接続するか、完了済み event を replay する。異なる
payload で ID を再利用した場合は `409 idempotency_conflict` とする。失敗または
キャンセルされた attempt の明示的な再送は、同じ client ID の新しい attempt とする。

SSE の正常系は `metadata`、0 個以上の `chunk`、`done` の順である。各 event の
`request_id` は同一で、`chunk.sequence` は単調増加し、`done.full_text` は chunk
の連結と一致する。Gemini Google Search のように provider が逐次 chunk を返さない
場合は、完成文を 1 chunk で返す buffered fallback として扱う。
`error` は終端 event で、`done` と同時には送らない。parser は LF / CRLF、UTF-8
境界、複数 `data:` 行、分割 frame に対応し、異常終了時は reader を解放する。

`GET /api/v1/chat/requests/{request_id}` で process-local 状態を確認し、
`POST /api/v1/chat/requests/{request_id}/cancel` で生成をキャンセルする。server が
request ID を通知する前は client 側 transport を中断し、通知後は server cancel
を要求してから transport を閉じる。最初の SSE subscriber が接続するまで生成を
開始せず、永続化開始後の最終確定区間はキャンセル不可とする。これにより、切断した
request の孤児実行と、保存済み turn の再送による二重保存を防ぐ。

provider SDK が同期処理を worker thread で実行する場合、async task の cancel は
Butly 側の event 転送と保存を停止するが、provider 側の通信を直ちに停止できない
ことがある。この制限は UI の「キャンセル済み」という保存状態とは分けて扱う。

## Preflight と capability

`GET /api/v1/preflight` は active chat / embedding role に必要な connection を中心に
疎通確認する。Ollama は native model list、ほかの connection は protocol に応じた
安全な model probe を使い、embedding は固定短文の実 embed と非空・有限 vector を
確認する。応答には status、reason code、latency、model ID、embedding dimension を
含められるが、secret、base URL、provider の raw error は含めない。

全体 status は必須 role を基準に `ready` / `degraded` / `unavailable` とする。
個別 connection の不調でも利用可能な機能を一律に隠さない。画像、Google Search、
generic Web Search、developer debug は `GET /api/v1/capabilities` の active model /
connection に基づく capability で表示可否を決める。

developer debug は sidecar の developer mode でだけ利用できる。無効時に
`include_debug=true` を送ると `403 debug_not_available` となる。表示するのは
Gatekeeper の tier / need / score / fallback と、RAG の候補数 / 注入数 / active
node 識別子などの要約に限る。

### Trace Graph（issue #51）

debug パネルからは、直近 1 ターンの回答生成フローを Mermaid flowchart として
表示できる。`GET /api/v1/instances/{name}/trace` が Gatekeeper / RAG /
Context Assembly / Provider / LLM / Memory Write を active・skipped・fallback・
error で塗り分けた Mermaid 文字列を返し、frontend は描画だけを行う。

- **Mermaid の生成は backend が正本**（`butly_core/trace/mermaid.py`）。frontend で
  組み立て直さない。保存済み `traces/latest.json` から生成する。
- **TraceNode の `metadata` は返さない。**原文クエリや検索候補が入るため、公開
  するのは label / summary から作った Mermaid 文字列と status 別ノード数だけ。
  ノード summary は表示用に 80 文字で切る（応答本文がそのまま載らないように）。
- chat debug と同じ developer mode gate。無効時は `403 debug_not_available`。
  trace 未記録の instance は `404 trace_not_found`。
- 描画側は Mermaid を `securityLevel: "strict"` / `htmlLabels: false` で扱い、
  ラベルを HTML として解釈させない。mermaid 本体は panel を開いたときだけ
  動的 import する（bundle を初期表示に載せない）。
- パネル内に収まらないので「拡大」で全画面オーバーレイへ開ける（Esc / 背景
  クリックで閉じる）。常設カラムにはしない。会話の幅を恒久的に削るうえ、
  列幅ではグラフが結局狭いため。
- `direction`（`TD` / `LR`）で向きを切り替えられる。縦長のグラフは横長の画面に
  収まらないため、生成は `render_mermaid(direction=...)` に委ね、frontend では
  SVG を組み替えない。
- 挿入後に viewBox を実測 bounding box から作り直し、mermaid が焼き込む
  `max-width` を外す。`htmlLabels: false` では文字幅の見積もりがずれることが
  あり、算出された viewBox の外にラベルが出ると切れるため。

## 添付、引用、安全性

- 画像は JPEG / PNG / WebP、最大 3 枚、1 枚あたり decoded 20 MB。data URL header を
  除いた base64 を送る。ファイル選択のほか **composer への貼り付け**でも添付でき、
  枚数・サイズ・MIME の上限は同じ経路で検証する。クリップボード画像は名前を持たない
  ことがあるため、その場合は `pasted-image.<ext>` を補う。
- 引用 URL は `http:` / `https:` だけを表示対象にする。既定の操作は **OS 既定ブラウザで
  開く**（Tauri では `shell:allow-open`、browser dev では別タブ）で、clipboard への
  copy は副操作として残す。webview 内では遷移させない。
- assistant の応答は Markdown として描画する。**raw HTML は描画せず**、リモート画像も
  読み込まない（リンクに落とす）。ユーザーの発言は Markdown 解釈せず、入力どおりに出す。
- backend からの文字列は React text node として描画し、HTML として挿入しない。
- token は Tauri lifecycle から memory 上で渡し、log、URL、永続ストレージへ保存しない。
- UI には公開 error code と request ID を表示し、provider の raw error は返さない。

## 検証

frontend unit test は SSE の分割 frame / buffered fallback / error / abort、状態遷移、
再送、capability gate、i18n を確認する。backend contract test は preflight、debug
認可、cancel / idempotency、秘密値の非露出を確認する。mock E2E は native
multi-chunk と Gemini buffered の両方で、履歴取得 → 送信 → stream → 永続化 →
履歴再取得が一致することを完了条件とする。

通常の確認は `./scripts/check_before_push.sh` を正とし、実 API key を使う
`-m integration` は通常の Phase 2 検証には含めない。
