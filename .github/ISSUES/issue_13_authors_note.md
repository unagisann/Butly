---
title: "動的プロンプト注入位置の制御（Author's Note）"
labels:
  - feature
  - architecture
assignees:
  - unagisann
---

## 概要

**Priority**: Low

SillyTavern の Author's Note に相当する機能。
プロンプトの任意の深さ（depth）に動的に情報を挿入できる。
「今のシーンの雰囲気」「物語の方向性」等をリアルタイムに変更可能。

現在の `build_context_prefix()` の注入順序は固定:

```
[ラベル + 注意文] → CURRENT TIME → MID-TERM → RAG → FLOATING → TIER INFO
```

## 拡張案

- インスタンスごとに `authors_note.txt` を持てるようにする
- UI で内容をリアルタイム編集可能
- 注入位置（depth）を設定可能にする（SillyTavern は会話履歴の N ターン前に挿入）
- Character Card V2 の `post_history_instructions` のインポート先としても機能

## タスク

- [ ] `authors_note.txt` のファイル管理と読み込みロジック
- [ ] `build_context_prefix()` に注入位置制御ロジック追加
- [ ] config.json に `authors_note_depth` フィールド追加
- [ ] UI: チャット画面からアクセスできる Author's Note 編集パネル
