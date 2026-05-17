# Provider Refactoring v3.1 — 変更まとめ（アーカイブ）

> 🗄️ **Archive (historical, Japanese only)** — このドキュメントは v3.1 当時の変更まとめを保存したものです。
> 最新の更新履歴は [recent_changes.ja.md](recent_changes.ja.md) / [recent_changes.md](recent_changes.md) を参照。
>
> 実施日: 2026-04-19
> ベースラインテスト数: 391 → 最終テスト数: **459** (68テスト追加、回帰 0)

---

## 概要

LLMプロバイダーレイヤーのリファクタリング。OpenAI互換コードの共通化、xAIプロバイダー追加、Ollama Web Search統合、UsageTracker provider別カウント対応、UI拡張を実施。

---

## 変更ファイル一覧

### 新規作成

| ファイル | 目的 |
|---|---|
| `butly_core/llm/_openai_compat.py` | OpenAI互換プロバイダー共通ヘルパー (env読込、メッセージ構築、reasoning model判定等) |
| `butly_core/llm/providers/xai.py` | xAI (Grok) プロバイダー |
| `butly_core/search/ollama_provider.py` | Ollama Cloud Web Search プロバイダー |
| `tests/test_openai_compat.py` | _openai_compat ユニットテスト (40件) |
| `tests/test_xai_provider.py` | xAI プロバイダーテスト (12件) |
| `tests/test_ollama_web_search.py` | Ollama Web Search テスト (6件) |
| `tests/test_search_factory.py` | 検索ファクトリテスト (6件) |

### 修正

| ファイル | 変更内容 |
|---|---|
| `butly_core/llm/providers/openai.py` | `generate()` を `_openai_compat` ヘルパーに委譲。`_build_system_instruction()` / `_build_user_content()` を削除 |
| `butly_core/llm/providers/ollama.py` | 同上パターンで `_openai_compat` に委譲 |
| `butly_core/llm/factory.py` | xAI ルーティング追加 (`grok-*` / `xai/*` → XaiProvider) |
| `butly_core/search/__init__.py` | `create_search_provider(chat_model="")` シグネチャ変更。Ollama chat + key → OllamaWebSearchProvider |
| `butly_core/search/usage_tracker.py` | provider別カウント (B6)。旧 `{YYYY-MM: int}` → 新 `{YYYY-MM: {tavily: N, ollama: M}}` lazy migration |
| `butly_core/search/tavily_provider.py` | `UsageTracker.increment("tavily")` 明示的 provider 指定 |
| `butly_core/chat/service.py` | `create_search_provider(chat_model=model_name)` に更新 |
| `routers/settings.py` | `key_type→env_name` 明示マップ (gemini/openai/xai/ollama_web_search)、不明 key_type → 400エラー、api_key_status に xai/ollama_web_search 追加 |
| `app.py` | B4修正 (`o1`/`o3`/`o4` ハイフンなし判定)、xAI ラベル追加、APIキー4列UI、Web検索トグル OLLAMA_WEB_SEARCH_API_KEY 対応、_API_PRESETS に grok モデル追加 |
| `user_config.json.example` | xAI設定例、reasoning model設定例、コメント更新 |
| `tests/test_usage_tracker.py` | 既存テスト修正 (新形式対応) + provider別カウントテスト4件追加 |

---

## 主要変更の詳細

### 1. _openai_compat.py — 共通ヘルパー抽出

OpenAI / Ollama / xAI で重複していたコードを共通化:

- `load_env_file()` — APIkey.env / .env の読み込み
- `is_reasoning_model(model)` — o1/o3/o4 推論モデル判定
- `build_messages()` — system instruction + history + user content のメッセージ配列構築
- `build_chat_completion_kwargs()` — reasoning モデルと通常モデルで kwargs を分岐 (temperature 除外、max_completion_tokens、reasoning_effort)
- `build_chat_response()` — API レスポンス → ChatResponse 変換
- `build_debug_messages()` — デバッグ用メッセージプレビュー

**設計判断**: Gatekeeper の `build_system_instruction_from_blocks` / `build_context_prefix` は循環 import 回避のため関数本体内で lazy import。

### 2. xAI (Grok) プロバイダー

- OpenAI SDK + `base_url="https://api.x.ai/v1"` で実装
- `XAI_API_KEY` / `XAI_BASE_URL` 環境変数
- Vision: grok-code-fast 以外は全モデル対応
- Embedding: xAI は未対応のため `None` を返却

### 3. Ollama Web Search

- `https://ollama.com/api/web_search` を `urllib.request` で叩く REST プロバイダー
- `OLLAMA_WEB_SEARCH_API_KEY` で Bearer 認証
- 検索ファクトリが chat モデル名を見て、Ollama チャット + key あり → OllamaWebSearchProvider を自動選択

### 4. UsageTracker provider 別カウント (B6)

- `increment(provider="tavily")` — デフォルトは tavily (後方互換)
- `get_current_month_count(provider=None)` — None で全合計
- 旧形式 `{YYYY-MM: int}` は `{YYYY-MM: {tavily: N}}` に lazy migration

### 5. get_provider_label B4 バグ修正

**修正前**: `model_name.startswith(("o1-", "o3-", "o4-"))` — `o3`, `o4-mini` 等のハイフンなしモデルを誤判定

**修正後**: `model_name.startswith(("o1", "o3", "o4"))` — 正しく OpenAI として判定

### 6. routers/settings.py — key_type バリデーション強化

- 明示的マッピング: `{"gemini": "GOOGLE_API_KEY", "openai": "OPENAI_API_KEY", "xai": "XAI_API_KEY", "ollama_web_search": "OLLAMA_WEB_SEARCH_API_KEY"}`
- 不明な key_type は HTTP 400 で拒否 (セキュリティ強化)

### 7. app.py UI 拡張

- APIキー管理: 2列 → 4列 (Gemini / OpenAI / xAI / Ollama WebSearch)
- モデルプリセット: grok-3-fast / grok-3-mini-fast を追加
- Web検索トグル: TAVILY_API_KEY **または** OLLAMA_WEB_SEARCH_API_KEY のいずれかがあれば有効化

---

## テスト結果

```
459 passed in 16.01s
```

| テストファイル | テスト数 | 状態 |
|---|---|---|
| test_openai_compat.py | 40 | 新規 |
| test_xai_provider.py | 12 | 新規 |
| test_ollama_web_search.py | 6 | 新規 |
| test_search_factory.py | 6 | 新規 |
| test_usage_tracker.py | 10 (+4) | 修正+追加 |
| その他既存テスト | 385 | 変更なし |

---

## 環境変数一覧 (v3.1 で追加)

| 変数名 | 用途 |
|---|---|
| `XAI_API_KEY` | xAI (Grok) API 認証キー |
| `XAI_BASE_URL` | xAI API エンドポイント (デフォルト: `https://api.x.ai/v1`) |
| `OLLAMA_WEB_SEARCH_API_KEY` | Ollama Cloud Web Search API 認証キー |
