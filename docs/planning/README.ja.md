# 計画書一覧

**日本語** | [English](README.md)

## 進行中

- [正式フロントエンド移行計画](active/frontend_migration_plan.ja.md)
  - Windows-first の Tauri + React/TypeScript desktop app へ段階移行します。
  - FastAPI / OpenAPI を正式な frontend contract とし、Streamlit は parity 完了まで残します。
- [記憶ストア正規化計画](active/memory_store_normalization_plan.ja.md)
  - 記憶レイヤを役割別に正規化し、ファイル構成とアクセス層を整理します。
- [pydantic-settings 設定統合計画](active/pydantic_settings_plan.ja.md)
  - Phase 1 の互換シムは実装済みです。
  - Phase 2 以降の移行作業が残っています。

## 実装済み・アーカイブ

以下は実装済みで、設計判断の履歴として保管している計画書です。

- [外部チャット連携前の土台整備計画](archived/external_chat_preflight_plan.ja.md)
- [外部チャット連携 設計決定メモ](archived/external_chat_design_decisions.ja.md)
- [Discord 連携実装計画](archived/discord_integration_plan.ja.md)
- [LINE 連携実装計画](archived/line_integration_plan.ja.md)

アーカイブ済み計画は、後続の安全要件や実装詳細と異なる場合があります。
現行コード・テスト・セットアップ資料を正としてください。
