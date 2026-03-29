---
title: "Lorebook（キーワードトリガー型コンテキスト注入）"
labels:
  - feature
  - memory
  - architecture
assignees:
  - unagisann
---

## 概要

**Priority**: High（将来の RP 対応・ペルソナ対応の基盤）

SillyTavern の World Info / Lorebook に相当する機能。
会話中にキーワードが出現したとき、対応するエントリの内容をプロンプトに自動注入する。
既存の knowledge_cards（RAG = embedding 類似度検索）とは**別の DB / 別のテーブル**として管理する。

Lorebook は「確定的トリガー（キーワード一致）→ 確定的注入」であり、
RAG の「曖昧検索 → スコア上位を注入」とは検索原理が異なる。
**tier に関係なく常に検索をかける（reflex でも）** 点が、既存 RAG（cortex のみ）と決定的に違う。

## SillyTavern の Lorebook データ構造（参考）

- `keys`: トリガーキーワード（カンマ区切り、正規表現対応）
- `secondary_keys`: AND 条件の副キーワード（任意）
- `content`: 注入テキスト本文
- `comment`: 作成者用メモ（注入されない）
- `insertion_order`: 複数エントリ発火時の優先度
- `position`: プロンプト内の注入位置（before/after system, before/after chat 等）
- `depth`: 注入深度（チャット履歴の何ターン前に挿入するか）
- `scan_depth`: 何ターン分の会話をキーワードスキャン対象にするか
- `case_sensitive`: 大文字小文字の区別
- `match_whole_words`: 単語単位マッチ
- `enabled`: ON/OFF
- `constant`: 常時注入（キーワード不要で常にプロンプトに含める）

## Butly への統合方針（設計メモ）

1. **保存先**: `butly_memory.db` 内に `lorebook_entries` テーブルを新設
   - knowledge_cards とは完全に別テーブル
   - ペルソナ対応時に instance 単位で持つので、既存の per-instance DB に収まる
2. **検索タイミング**: `memory_builder.py` の `build()` 内で、tier 判定**後**・RAG 検索**前**に実行
   - 全 tier（reflex 含む）で常にスキャン
   - スキャン対象: user_input + 直近 N ターンの会話テキスト（scan_depth で制御）
3. **注入位置**: `build_context_prefix()` に新セクション `=== LOREBOOK ===` を追加
   - RAG の前に配置（Lorebook は確定情報、RAG は参考情報）
4. **最小 MVP**: keys（カンマ区切り）+ content + enabled のみ
   - position / depth / regex / recursion 等は将来拡張

## タスク

- [ ] `lorebook_entries` テーブル設計（最小: id, keys, content, enabled, insertion_order, created_at, updated_at）
- [ ] `database.py` に Lorebook CRUD メソッド追加
- [ ] `memory_builder.py` にキーワードスキャン + 注入ロジック追加
- [ ] `build_context_prefix()` に `=== LOREBOOK ===` セクション追加
- [ ] API エンドポイント追加（`/lorebook/{instance_name}/entries`）
- [ ] UI: Lorebook 管理画面（エントリ一覧・追加・編集・削除・ON/OFF トグル）

## 将来拡張（この Issue では扱わない）

- secondary_keys（AND 条件）
- 正規表現キーワード
- position / depth 制御（注入位置の柔軟化 → Issue #13 と連携）
- character_filter（グループチャット時）
- constant エントリ（常時注入）
- SillyTavern Lorebook JSON のインポート機能
- delay_until_recursion（再帰スキャン）
