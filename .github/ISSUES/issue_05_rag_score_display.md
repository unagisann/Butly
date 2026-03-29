---
title: "RAG デバッグにスコア表示を追加"
labels:
  - ui
  - debug
assignees:
  - unagisann
---

## 概要

**Priority**: Low

Debug モードの「Brain Process」セクションに RAG 検索のスコア（cosine similarity）が表示されていない。

## タスク

- [ ] `ChatResponse` にスコア情報を含める（`refs` の各要素に `score` フィールド追加）
- [ ] `brain.py` の `search_knowledge` がスコアを返すようにする（既に内部では計算済みのはず）
- [ ] `app.py` の Debug 表示で各 ref のスコアを表示
