# Butly ドキュメント

**日本語** | [English](README.md)

目的別に文書を整理しています。

| ディレクトリ | 内容 |
|---|---|
| `guides/` | セットアップ・運用手順 |
| `reference/` | **現行アーキテクチャ・機能仕様（正本）** |
| `history/` | 評価レポート・長期実験の記録 |
| `planning/` | 進行中・実装済み計画書 |
| `Old/` | 凍結ドキュメント。後継に置き換わった旧正本 |

## 正の優先順位

**現行コード → テスト（`tests/`） → `reference/` / `guides/` → `planning/archived/` → `Old/`**

`planning/archived/` と `Old/` は設計判断の履歴であり、後続の安全要件・実装と食い違うことがあります。

---

## セットアップ

- [デスクトップ UI の起動手順（通常 / 開発 / browser）](guides/desktop_dev_setup.ja.md)
- [Discord 連携セットアップ](guides/discord_integration_setup.ja.md)
- [LINE 連携セットアップ](guides/line_integration_setup.ja.md)

## アーキテクチャ・仕様

**全体像**
- [アーキテクチャ図集](reference/DIAGRAMS.ja.md) — 主要フローの Mermaid 図
- [ファイル構成](reference/FILE_STRUCTURE.ja.md) — モジュールごとの責務
- [コーディング規約](reference/coding_conventions.ja.md)

**記憶とコンテキスト**
- [記憶ライフサイクル](reference/memory_lifecycle.ja.md) — 各記憶層の保存・昇格・オーバーフロー
- [Gatekeeper 入出力仕様](reference/gatekeeper_io_summary.ja.md) — tier 判定・RAG 注入の 2 段構え
- [context_levels 仕様](reference/context_levels.ja.md) — プロンプトブロックの詳細度プリセット

**設定と LLM**
- [設定レイヤー](reference/configuration.ja.md) — settings の解決順・`user_config.json` / インスタンス `config.json`
- [LLM Connection / APIキー管理](reference/llm_connections.ja.md) — Connection・Capability 解決・秘密情報

**フロントエンド**
- [Desktop sidecar 仕様](reference/desktop_sidecar.ja.md) — 起動シーケンス・認証・packaging
- [正式デスクトップ Chat UI](reference/frontend_chat.ja.md) — 画面・Chat API・Trace Graph

**評価**
- [LoCoMo Evaluation Web Console](reference/evaluation_web_console.ja.md) — 実行・停止・比較の操作
- [LoCoMo評価のデータ保存・QA実行フロー](reference/locomo_evaluation_flow.ja.md) — workspace 隔離・採点

## 評価レポート・実験記録

- [RAG評価・改善レポート（Web Console移行以降）](history/rag_evaluation_report.ja.md)
- [評価数値データ](history/rag_evaluation_data/) — LoCoMo run / 対話A/B / 検索比較の CSV

## 計画書

- [計画書一覧](planning/README.ja.md)
- 進行中の計画は `planning/active/`、実装済みで設計履歴として価値がある計画は `planning/archived/`。

## 凍結ドキュメント

- [Old の説明](Old/README.ja.md) — 何を、なぜ凍結したか

## 変更履歴

手書きの changelog は廃止しました。**変更履歴は `git log` が正本**です
（Conventional Commits: `feat:` `fix:` `refactor:` `docs:` `chore:` `ci:` `style:`）。
