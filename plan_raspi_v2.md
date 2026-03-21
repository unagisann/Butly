# J.A.R.V.I.S 画像付きチャット対応・責務分離 実装計画書 v1.1
**対象**: Claude Code 実装指示用  
**主目的**: `main.py` に画像付きチャットを追加しつつ、Gemini 固有実装が `main.py` / `brain.py` に広がらない構造へ寄せる  
**優先度**: 高  
**前提**: `interaction_id` は今回スコープ外。将来利用予定が薄いため無視してよい。

---

## 1. この計画で解決したいこと

現状の計画では、Windows 側ダッシュボードが WebSocket で `chat_message` を送り、ラズパイ側 `main.py` が受けて `brain.generate_response_with_rag(...)` に流す想定になっている。  
ただし、現行案は `{"text": "...", "images": ["base64..."]}` 前提で、Gemini の画像入力都合に寄ったままアプリ全体へ広がる構造になっている。

このまま最短実装を進めると、後で OpenAI / Ollama / Llama / Qwen-VL などを追加した際に次の問題が起きやすい。

- `main.py` に provider 固有分岐が増える
- `brain.py` が画像変換や SDK 呼び出し責務まで持つ
- フロントの `images: string[]` 仕様が固定化する
- MIME type や将来の添付拡張に弱い
- 画像入力の実装変更がバックエンド全体へ波及する

そのため今回は、「今すぐ全 provider 対応を完成させる」のではなく、**Gemini でまず動かしつつ、差し替え可能な境界線だけ先に作る** 方針を採用する。

---

## 2. 今やるべきこと / 後でよいこと

### 今やるべきこと
1. `main.py` が旧形式・新形式の `chat_message` を両対応で受けられるようにする  
2. 受信直後に共通 DTO (`ChatRequest`, `Attachment`) に正規化する（**Pydantic で統一**）
3. `main.py` から Gemini 固有コードを排除する  
4. `ChatService` と `Provider` の境界を導入する  
5. Gemini provider 内に **inline / Files API** の二経路を閉じ込める  
6. 画像入力のバリデーション、ログ、エラー応答を追加する

### 後でよいこと
1. OpenAI provider の実装  
2. Ollama / Llama / Qwen-VL などローカル vision provider の実装  
3. PDF / 音声 / ドキュメント等の添付種別対応  
4. Files API の再利用最適化  
5. モデル切替の高度な capability 管理  
6. 添付履歴の永続保存

---

## 3. 設計方針

### 方針A: アプリ内の標準を Gemini 仕様にしない
Gemini には画像入力として次の2系統がある。

- 小さい画像: inline data として送る
- 大きい画像 / 再利用用途: Files API にアップロードして URI 参照する

しかしアプリ内部ではこれをそのまま標準にしない。  
アプリ内では **共通 DTO（Pydantic）** を標準とし、Gemini 形式への変換は provider 層でのみ行う。

### 方針B: `main.py` は薄くする
`main.py` の責務は次に限定する。

- WebSocket 受信
- payload 判定
- DTO 正規化
- バリデーション
- service 呼び出し
- WebSocket 返却

Gemini SDK / OpenAI SDK / Ollama API などの個別実装は置かない。  
**`main.py` への Gemini SDK の import は禁止。**

### 方針C: `brain.py` は読み取り専用として扱う
`brain.py` には会話履歴整形、RAG、memory block 構築、system instruction 組み立てなどの役割を残す。  
**今回の実装では `brain.py` を直接改修しない。** SDK の payload 生成や画像の inline/file 分岐など「どう送るか」は provider 層へ逃がし、`brain.py` は「何を答えるか（会話設計・prompt assembly）」に集中させる。  
`ChatService` から `brain.py` の既存メソッドを呼ぶ形は許容するが、`brain.py` 内に provider 固有コードが入ることは禁止。

### 方針D: `images` ではなく `attachments`
今は画像だけでも、将来の添付拡張を見越してフィールド名は `attachments` を採用する。  
今回実装する `kind` は `image` のみでよい。

### 方針E: DTO は Pydantic で統一
既存の `main.py` はすでに Pydantic を使っている。`types.py` も Pydantic で統一し、dataclass との混在を避ける。

### 方針F: ChatService はリクエストごとに生成する
`instance_store` のキャッシュ対象はあくまでインスタンスコンポーネント（memory / brain / chronos）とし、`ChatService` はリクエストごとに生成する。状態を持たないステートレスな設計にする。

---

## 4. 目標アーキテクチャ

```text
[Dashboard / Windows]
  ChatPanel.tsx
     ↓ WebSocket chat_message
[main.py]
  payload受信 / DTO正規化（Pydantic） / validation
     ↓
[ChatService]  ← リクエストごとに生成・ステートレス
  history / memory / prompt / provider選択
  brain.py の既存メソッドを呼ぶ（brain.py 自体は改修しない）
     ↓
[ProviderFactory]
     ↓
[GeminiProvider]
  inline / Files API 分岐
     ↓
[Gemini API]
```

### 責務分離
| レイヤー | 責務 |
|---|---|
| `main.py` | IO・配線・DTO正規化・validation のみ |
| `ChatService` | 会話実行 orchestration・brain/provider の橋渡し |
| `brain.py` | 会話文脈・RAG・memory（**今回は改修しない**） |
| `Provider` | 各LLM向け変換とAPI呼び出し |

---

## 5. 共通データ仕様（アプリ内標準）

### 5.1 WebSocket 新旧互換仕様

#### 旧形式（維持）
```json
{
  "type": "chat_message",
  "payload": "こんにちは"
}
```

#### 新形式（今回追加）
```json
{
  "type": "chat_message",
  "payload": {
    "text": "この画像を説明して",
    "attachments": [
      {
        "kind": "image",
        "mime_type": "image/jpeg",
        "data_base64": "...",
        "name": "sample.jpg",
        "size": 123456
      }
    ]
  }
}
```

### 5.2 Attachment（Pydantic モデル）
```python
class Attachment(BaseModel):
    kind: Literal["image"]           # 今回は "image" のみ
    mime_type: str                   # image/jpeg / image/png / image/webp
    data_base64: str                 # data URL ヘッダなしの本体のみ
    name: Optional[str] = None       # 任意
    size: Optional[int] = None       # バイト数・任意
```

### 5.3 ChatRequest（Pydantic モデル）
```python
class ChatRequest(BaseModel):
    text: str = ""
    attachments: List[Attachment] = []
    instance_name: str = "00_master"
    model_name: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
```

### 5.4 ChatResponse（Pydantic モデル）
```python
class ChatResponse(BaseModel):
    text: str
    keywords: List[str] = []
    refs: List[Dict[str, Any]] = []
    sources: List[Dict[str, Any]] = []
```

---

## 6. ファイル構成

```text
main.py                                   # 既存・WebSocket配線のみに絞る
butly_core/core/brain.py                  # 既存・今回は改修しない
butly_core/chat/__init__.py               # 新規
butly_core/chat/types.py                  # 新規: Pydantic DTO定義
butly_core/chat/service.py                # 新規: ChatService（ステートレス）
butly_core/llm/__init__.py                # 新規
butly_core/llm/base.py                    # 新規: Provider抽象基底クラス
butly_core/llm/factory.py                 # 新規: ProviderFactory
butly_core/llm/providers/__init__.py      # 新規
butly_core/llm/providers/gemini.py        # 新規: GeminiProvider
```

---

## 7. 実装スコープ（今回）

### 7.1 `main.py`
- `chat_message` を文字列 payload / オブジェクト payload の両方に対応
- オブジェクト payload の場合は `attachments` を受ける（`images` は互換変換で吸収し内部では使わない）
- DTO 正規化関数を作る（Pydantic）
- validation を行う
- `ChatService` を呼ぶ（brain.py の直叩きをやめる）
- 既存の `chat_response` 形式で返す
- `request_status` や config/prompts 更新など既存の非チャット仕様は維持する
- **Gemini SDK の import は書かない**

### 7.2 `ChatService`（ステートレス）
- `main.py` から呼ばれる会話実行層
- インスタンスコンポーネントは `get_instance_components()` 経由で取得（既存の `instance_store` を流用）
- prompt / memory / history / cached_content の組み立て
- provider 選択（ProviderFactory 経由）
- `brain.py` の既存メソッドを呼ぶ（brain.py 自体は改修しない）

### 7.3 `brain.py`
- **今回は改修しない**
- 会話文脈・RAG・memory の役割として現状を維持する
- SDK 直叩きや画像変換ロジックは provider へ移す
- ChatService から既存メソッドを呼ぶことで間接的に機能する

### 7.4 `GeminiProvider`
- `ChatRequest` を Gemini contents / parts に変換
- 小さい画像は inline data（1枚 2MB 以下かつ合計 8MB 以下）
- 大きい画像は Files API（tempfile を使用、ローカルファイルは finally でクリーンアップ）
- **Gemini Files API にアップロードしたファイルは削除しない**（サーバー側で48時間後に自動削除されるため、今回はコスト対効果を考慮してスキップ）
- 複数画像の順序保持
- `supports_vision(model_name)` の判定ロジックを持つ

#### `supports_vision` の実装方針
文字列マッチングは将来の新モデル追加時に保守コストが高い。  
以下のホワイトリスト + フォールバック方式を採用する。

```python
# 明示的な非対応リスト（こちらを管理する）
VISION_UNSUPPORTED_MODELS = {
    "gemini-3.1-flash-lite-preview",   # テキスト特化軽量モデル等
}

def supports_vision(model_name: str) -> bool:
    """
    非対応モデルリストに含まれていなければ vision 対応とみなす。
    新モデルはデフォルトで対応扱い。非対応が判明したら追加する。
    """
    return model_name not in VISION_UNSUPPORTED_MODELS
```

---

## 8. 実装スコープ外（今回やらない）
- `brain.py` の改修
- OpenAI provider 実装
- Ollama/Llama provider 実装
- 音声やPDFの添付対応
- 添付の永続保存
- UI 側での大規模リサイズ/圧縮最適化
- モデル選択UIの高度化
- 自動 capability discovery
- Gemini Files API へのアップロードファイルの明示的削除

---

## 9. 詳細実装タスクと順序

**順序を守ること。** 特に Phase 4（ChatService）が完成する前に Phase 5（main.py 改修）を進めると、service 未完のまま配線だけできてしまい動作確認ができない。

### Phase 1: 共通 DTO 導入（`types.py`）
**目的**: WebSocket 受信データを LLM 非依存の形へ正規化する。

**作業**
- `butly_core/chat/types.py` を新規作成
- `Attachment`, `ChatRequest`, `ChatResponse` を **Pydantic** で定義（dataclass は使わない）
- `__init__.py` を追加

**完了条件**
- `main.py` から DTO を import して使える
- provider 側も同じ型を受け取れる

---

### Phase 2: Provider 抽象導入（`base.py`, `factory.py`）
**目的**: Gemini 固有の payload 構築を backend の中心ロジックから切り離す。

**作業**
- `butly_core/llm/base.py` に抽象基底クラス `BaseProvider` を追加
  - 必須メソッド: `async generate(request: ChatRequest, context: dict) -> ChatResponse`
  - クラスメソッド: `supports_vision(model_name: str) -> bool`
- `butly_core/llm/factory.py` を追加
  - `model_name` が `"gemini-"` プレフィックスを持つ場合は `GeminiProvider` を返す
  - それ以外は `NotImplementedError`（将来の拡張ポイント）
- `__init__.py` を追加

**完了条件**
- `ChatService` が ProviderFactory 経由で provider を取得できる
- `main.py` が provider 名を知らない

---

### Phase 3: GeminiProvider 実装（`providers/gemini.py`）
**目的**: Gemini 公式の2経路（inline / Files API）を adapter 内に閉じ込める。

**作業**
- `butly_core/llm/providers/gemini.py` を追加
- `ChatRequest` の text / attachments を Gemini `contents` / `parts` に変換
- inline 判定: 1枚 2MB 以下かつ合計 8MB 以下 → inline data
- それ以上 → Files API（`tempfile` でローカル一時ファイル作成 → アップロード → `finally` でローカルファイルを削除）
- 複数画像を順番通りに送る
- `supports_vision` は非対応モデルリスト方式で実装（Section 7.4 参照）
- `__init__.py` を追加

**完了条件**
- テキストのみ / テキスト+画像 / 画像のみ を Gemini で処理できる
- `main.py` に Gemini SDK 依存がない
- `brain.py` に画像変換コードがない

---

### Phase 4: ChatService 実装（`service.py`）
**目的**: `main.py` と `brain.py` の間に orchestration 層を挟み、責務を整理する。

**作業**
- `butly_core/chat/service.py` を新規作成
- **ステートレス設計**（クラスに状態を持たせない。毎リクエスト生成してよい）
- インスタンスコンポーネントは `get_instance_components()` を使って取得（`instance_store` を流用）
- prompt / memory / history / cached_content の組み立てロジックを集約
- ProviderFactory 経由で provider を取得
- `brain.py` の既存メソッドは **呼ぶだけ**（brain.py 自体は改修しない）

**完了条件**
- `main.py` は `await ChatService.execute(chat_request, ws_manager)` のような単純な呼び出しで済む
- `brain.py` の直叩きが `main.py` から消える

---

### Phase 5: `main.py` の WebSocket 改修
**目的**: 画像付き payload を安全に受ける入口を作る。

**前提**: Phase 4 が完了していること。

**作業**
- `chat_message` で旧形式（文字列）/ 新形式（オブジェクト）を両対応
- payload 正規化関数を作る
- 旧 `images: string[]` が来た場合は `Attachment` に互換変換して内部では `attachments` に統一（`images` を内部で使い続けない）
- `text` も `attachments` も空なら reject
- 受信後は `ChatService` へ委譲
- **Gemini SDK の import を書かない**

**完了条件**
- 文字列 payload の従来チャットが動く
- オブジェクト payload でも動く
- `main.py` が薄い

---

### Phase 6: バリデーション・ログ・エラー応答
**目的**: 画像入力で落ちるポイントを `main.py` / provider の境界で明確にする。

**バリデーション要件**
- `text` も `attachments` も空なら reject
- 添付は最大 3 枚まで
- `kind != "image"` は reject
- MIME は `image/jpeg`, `image/png`, `image/webp` のみ許可
- base64 decode 失敗時は reject
- 1枚あたり 20MB 超は reject
- data URL ヘッダ（`data:image/...;base64,`）が来たらDTO化前に除去

**ログ要件（画像本体は絶対ログに出さない）**
- 受信 text 長
- attachment 数
- MIME 一覧
- 選択 provider
- vision 対応可否
- inline / Files API の分岐結果
- ローカル一時ファイル作成 / 削除

**エラー応答メッセージ例**
- 「サポートされていない画像形式です（許可: jpeg / png / webp）」
- 「画像サイズが上限を超えています（1枚あたり 20MB 以内）」
- 「選択中のモデルは画像入力に対応していません」
- 「画像データの解析に失敗しました」
- 「添付は最大3枚までです」

---

## 10. フロントへの要求仕様（今回固定しておくべき点）

今回フロント全実装まではやらなくてもよいが、次の仕様は先に固定する。

### 必須
- `images` ではなく `attachments` を送る
- 各 attachment に `mime_type` を持たせる
- `data_base64` は data URL ヘッダを除去した本体だけ送る（`data:image/jpeg;base64,` の部分は除く）
- `name`, `size` が取れるなら送る
- 旧形式文字列 payload は互換維持のため残す

### 推奨
- 最大3枚
- 読込中は送信ボタンを disable にする
- MIME は `image/jpeg`, `image/png`, `image/webp` のみ受け付ける
- localStorage に画像本体を保存しない（チャット履歴永続化時も `attachments` は除外する）

---

## 11. リスクと先回り対策

### リスク1: `images: string[]` が既成事実化する
対策: `main.py` 改修で `attachments` 標準へ寄せる。旧 `images` は互換変換のみで吸収し、内部では使わない。

### リスク2: Gemini 固有コードが `main.py` に入り込む
対策: Gemini SDK の import は provider ファイル（`providers/gemini.py`）のみに限定。ClaudeCode へのレビュー観点として明示する。

### リスク3: Files API のためにローカル保存が恒久化する
対策: `tempfile` のみ使用。`finally` でローカルファイルをクリーンアップ。Gemini Files API 側のファイルは48時間で自動削除される仕様を前提とし、今回は明示的削除しない。

### リスク4: `brain.py` の責務が曖昧なまま肥大化する
対策: 今回は `brain.py` を改修しない。ChatService から既存メソッドを呼ぶだけ。次フェーズで prompt assembly と provider call の境界を整理しやすい状態にする。

### リスク5: `supports_vision` のモデル名マッチングが保守不能になる
対策: ホワイトリストではなく「非対応モデルの除外リスト」方式を採用。新モデルはデフォルトで vision 対応とみなし、非対応が判明したときだけリストに追加する。

### リスク6: Phase順序の乱れ
対策: **Phase 4（ChatService）が完成する前に Phase 5（main.py改修）を進めない。** service 未完のまま配線だけできてしまい、動作確認が取れなくなる。

---

## 12. 段階的実装順序（Claude Code向け）

1. `types.py` を追加して Pydantic DTO 定義
2. `base.py`, `factory.py` を追加して Provider 抽象導入
3. `providers/gemini.py` を追加して GeminiProvider 実装
4. `service.py` を追加して ChatService 実装（ステートレス）
5. `main.py` の `chat_message` を新旧両対応に変更し ChatService へ委譲
6. バリデーション / ログ / エラー応答を追加
7. 最小テストを追加
8. 動作確認を実施

**順序は変えないこと。特に 4 → 5 の前後を守ること。**

---

## 13. テスト計画

### 最低限の確認
1. 旧形式文字列 payload のチャットが従来通り動く
2. テキスト + 画像1枚（JPEG）で応答できる
3. 画像のみ（テキストなし）でも応答できる
4. PNG / WebP が通る
5. 不正 base64 は弾かれる
6. 非対応 MIME は弾かれる
7. 複数画像（2〜3枚）が順序保持で通る
8. vision 非対応モデル選択時にエラーメッセージが返る
9. 20MB 超の画像は弾かれる（Files API 分岐が必要なサイズは別途確認）
10. ローカル一時ファイルが cleanup される（`tempfile` の削除）
11. `main.py` に Gemini SDK の import がないことを確認

### 望ましいテスト単位
- payload 正規化関数（文字列 / オブジェクト / 旧 images 形式）
- Attachment バリデーション（MIME / サイズ / base64 デコード）
- GeminiProvider の inline / Files API 分岐
- `supports_vision()` 判定（非対応リストの境界）

---

## 14. Claude Code への明示的な指示

### 必須制約
- 既存動作を壊さず、後方互換を維持すること
- **`main.py` に Gemini SDK 固有コードを書かないこと**
- **`brain.py` は改修しないこと**（呼び出しは OK、コードの変更は NG）
- `types.py` の DTO は Pydantic で実装すること（dataclass は使わない）
- `ChatService` はステートレス設計にすること（インスタンスに状態を持たせない）
- Phase 4（ChatService）完成前に Phase 5（main.py 改修）を進めないこと

### レビュー観点
- `main.py` が薄くなっているか
- `main.py` に Gemini SDK の import がないか
- `brain.py` が変更されていないか
- provider 境界が見えるか
- 旧テキスト only チャットが壊れていないか
- 添付仕様が `attachments` に寄っているか（`images` が内部で使われていないか）
- Gemini 以外を足す未来を壊していないか

### 実装後の報告フォーマット
実装完了後、以下を報告すること。
1. 変更・追加ファイル一覧
2. 各ファイルの責務の説明
3. `main.py` から Gemini 依存がなくなったことの確認
4. 旧チャット動作の確認結果
5. 残課題・次フェーズへの申し送り

---

## 15. 完了条件

次を満たしたら今回の計画は完了とする。

- `main.py` が画像付き `chat_message`（`attachments` 形式）を受信できる
- 旧形式文字列 payload が壊れていない
- **`main.py` に Gemini 固有実装がない**
- **`brain.py` が改修されていない**
- Provider 抽象が導入されている
- GeminiProvider 内で inline / Files API を扱える
- DTO が Pydantic で統一されている
- `ChatService` がステートレスである
- validation / logging / error response がある
- 今後 OpenAI / Ollama を足しやすい構造になっている

---

## 16. 最後の判断基準

今回の作業で目指すのは「マルチ provider 完成」ではない。  
**"Gemini でまず動く" と "Gemini 前提が広がらない" を両立すること** が成功条件。

OpenAI や Llama 系の実装は後回しでよい。  
ただし、`main.py` の受信仕様・DTO・service/provider の境界だけは今やること。
