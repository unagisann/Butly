---
title: "Housekeeper 管理画面に実行履歴を追加"
labels:
  - ui
  - enhancement
assignees:
  - unagisann
---

## 概要

**Priority**: Low

Housekeeper の最終実行日時と、最後に生成されたナレッジカードの情報を表示する。

## タスク

- [ ] Housekeeper 実行完了時にタイムスタンプを保存する仕組みを追加
  - `instance_dir/housekeeper_last_run.json` など
  - 内容: `{"last_run": "2026-03-29T03:00:00", "cards_created": 5}`
- [ ] `render_housekeeper_screen()` に最終実行日時と作成カード数を表示
- [ ] DB ブラウザ画面に最新カードの作成日時を表示（既存の `created_at` フィールドを利用）
