# Recent Changes

## 最新の実装：Phase 4 中期記憶要約の動的注入切替 (2026-03-15)
- **Gatekeeper改修**: `gatekeeper.py` の `build_system_instruction_from_blocks` にて、生の `mid_term.txt` だけでなく、Housekeeperが作成した「事実ダイジェスト (`mid_term_digest.txt`)」と「関係性スナップショット (`mid_term_relationship.txt`)」を個別の論理セクションとしてプロンプトへ注入する機能を追加。
- **メモリI/O**: `memory.py` に `get_mid_term_digest()` および `get_mid_term_relationship()` を実装。
- **設定の追加**: `butly_core/config.py` に `use_summarized_mid_term` （要約モードトグル）を追加し、要約ファイル不在時は自動的にRAWへフォールバックする安全機構を導入。
- **UIの更新**: `app.py` の詳細設定画面に、要約注入モードのOn/Offを直感的に切り替えられるトグルを追加。
- **プロンプト微調整**: `prompts.py` の `MIDTERM_DIGEST_PROMPT` と `MIDTERM_RELATIONSHIP_PROMPT` を最適化し、事実ダイジェストをより簡潔な索引に、関係性をより端的なステータス記録になるようにプロンプト命令を修正。

## 直近の主要な実装履歴
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
