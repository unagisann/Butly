# 計画書一覧

**日本語** | [English](README.md)

> ⚠️ 計画書は**現行仕様の正ではありません**。
> 正の優先順位: 現行コード → テスト → [`docs/reference/`](../reference/) → 計画書。

## 進行中（`active/`）

| 計画書 | ステータス | 残作業 |
|---|---|---|
| [正式フロントエンド移行計画](active/frontend_migration_plan.ja.md) | Phase 2 実装済み（2026-08-12） | Phase 3（Onboarding / instance 基本設定）。Phase 1 の Windows 実機 installer / CI 初回検証も未完了 |
| [Stage 3 知識熟成計画](active/stage3_knowledge_maturation_plan.ja.md) | Phase 0〜5 実装済み（2026-07-21） | Phase 5 の実 LoCoMo A/B 実走、Phase 6（Key Memory 自動反映）、Phase 7（node 独立検索）、Phase 8（クリーンアップ）、proposal 承認 API |
| [検索改修計画（ハイブリッド検索 / RRF）](active/retrieval_hybrid_search_plan.ja.md) | Phase 1 + 日本語対話 A/B 完了（2026-08-09） | **hybrid は不採用（既定 `vector`）／常時検索は採用**。`dual_query` の offline Recall / rescue・harm 実測と採否判断 |
| [pydantic-settings 設定統合計画](active/pydantic_settings_plan.ja.md) | Phase 1 互換シム実装済み | Phase 2 以降（モジュール毎の typed access 移行、legacy globals 削除、セクション型付け後の env 上書き対応） |
| [多人数コンテキスト計画](active/group_context_lanes_plan.ja.md) | Phase 1 実装済み（2026-07-08） | Phase 2 以降は未着手（フロントエンド土台・observability との兼ね合いで後回し） |
| [LoCoMo 長期記憶評価計画](active/locomo_evaluation_plan.ja.md) | Phase 1〜4 完了 | Phase 5（実データ試験）のみ |
| [記憶ストア正規化計画](active/memory_store_normalization_plan.ja.md) | 未着手（提案段階） | 全 Phase |

## 実装済み・アーカイブ（`archived/`）

設計判断の履歴として保管している計画書です。

- [LLM 接続まわりの整理計画](archived/llm_connection_refinement_plan.ja.md) — Canonical Request / Capability Resolver の設計
- [外部チャット連携前の土台整備計画](archived/external_chat_preflight_plan.ja.md)
- [外部チャット連携 設計決定メモ](archived/external_chat_design_decisions.ja.md)
- [Discord 連携実装計画](archived/discord_integration_plan.ja.md)
- [LINE 連携実装計画](archived/line_integration_plan.ja.md)

---

## 運用ルール

- 進行中の計画は `active/`。**完了条件を満たしたら `archived/` へ移す**。
- 各計画書の冒頭に「ステータス / 残作業」を書き、実装が進んだら更新する。
- アーカイブ済み計画は、後続の安全要件や実装詳細と異なる場合があります。
  現行コード・テスト・`docs/reference/` を正としてください。
