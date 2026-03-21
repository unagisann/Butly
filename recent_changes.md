# Recent Changes

## 最新の実装：Raspi V2 画像付きチャット対応と責務分離 (2026-03-21)
- **DTOの導入**: `butly_core/chat/types.py` を作成し、`ChatRequest`, `ChatResponse`, `Attachment` の標準モデルを定義。WebSocketとRESTからの入力を正規化する処理を追加。
- **Provider抽象化**: `butly_core/llm/` に Provider 抽象層を作成し、`GeminiProvider` を実装。これまで `main.py` や `brain.py` に点在していた Gemini 固有の画像処理（inline送信 / Files API 分岐）を Provider 内に隠蔽化。
- **ChatService導入**: チャットのオーケストレーションを担うステートレスな `ChatService` (`butly_core/chat/service.py`) を実装し、`main.py` から LLM 依存コードを排除。
- **brain.py のクリーンアップ**: 画像変換ロジックを Provider 側に移行し、`brain.py` の画像関連引数 (`images`) を削除。※記憶注入ロジックは変更なし。

## 直近の主要な実装履歴
1. **Phase 4 中期記憶要約の動的注入切替**:
1. **Phase 3 二層要約パイプラインの実装**:
   - `housekeeper.py` における中期記憶の整理機能を拡張し、出来事と決定事項をまとめた「事実ダイジェスト」と、AIとユーザーの距離感を示す「関係性スナップショット」の二層ファイル生成パイプラインを構築。
2. **OSS向けオープン化準備 / リファクタリング**:
   - 「Jarvis」などのハードコードされた初期名や個人情報を排除し、設定ファイルやテンプレートから動的に読み込む汎用的な「Butly」プラットフォームへと改修。
3. **ステートフルAPI (Interactions API) の導入**:
   - 会話ターンごとに長期履歴を全て手動で挿入する状態から、Google Gemini側のセッション履歴保持機構に移行し、不要なトークン消費を抑制。
4. **FastAPI + Streamlit への分離**:
   - 処理の非同期化とバックグラウンドタスク（Housekeeper）の安定稼働、UI側のレスポンス向上のため、単一スクリプトからAPIサーバーとフロントエンドの構成に分離。
5. **インスタンス別記憶の分離**:
   - 複数の別キャラクター・別用途AIを同時に動かせるよう、`butly_core/instances/` ディレクトリ配下で記憶DBとファイルを完全分離。
