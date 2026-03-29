---
title: "記憶 DB の UI 分離（通常記憶 vs Lorebook）"
labels:
  - ui
  - memory
assignees:
  - unagisann
---

## 概要

**Priority**: Medium（Lorebook issue と同時に対応）

Lorebook テーブルを追加した後、設定画面で「通常記憶（knowledge_cards）」と「Lorebook」を明確に分離して管理する。
将来的に、チャット時にどの記憶ソースを使うか選択可能にする。

## UI 構成案

```
設定画面
├── 📚 記憶管理
│   ├── 🧠 ナレッジカード（既存の DB ブラウザ）
│   │   - RAG 検索（embedding 類似度）で使用
│   │   - cortex tier でのみ検索
│   └── 📖 Lorebook（新規）
│       - キーワードトリガーで使用
│       - 全 tier で常時検索
│       - エントリ一覧・追加・編集・削除・ON/OFF
├── ⚙️ 記憶ソース設定（将来）
│   ├── [✓] ナレッジカード（RAG）を使用
│   ├── [✓] Lorebook を使用
│   └── [✓] Mid-term summary を使用
```

## タスク

- [ ] DB ブラウザ画面のタブ分離（ナレッジカード / Lorebook）
- [ ] Lorebook 専用の CRUD UI
- [ ] 記憶ソース ON/OFF 設定（config.json に `memory_sources` セクション追加）
- [ ] memory_builder.py で config の記憶ソース設定を参照

## 前提 Issue

- Lorebook（キーワードトリガー型コンテキスト注入）
