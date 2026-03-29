---
title: "Interactions API 残骸の完全クリーンアップ"
labels:
  - cleanup
assignees:
  - unagisann
---

## 概要

**Priority**: High

Part A で Interactions API のコードは削除済みだが、既存インスタンスに `last_interaction_id.txt` が残っている可能性がある。

## タスク

- [ ] マイグレーションスクリプト or Housekeeper に `last_interaction_id.txt` の自動削除を追加
  - 実害はないが、ファイル構成の清潔さのために
- [ ] README のファイル構成から `last_interaction_id.txt` の記載がないことを確認
