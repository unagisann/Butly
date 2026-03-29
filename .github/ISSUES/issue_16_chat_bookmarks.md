---
title: "チャット分岐（Chat Bookmarks）"
labels:
  - feature
  - ui
assignees:
  - unagisann
---

## 概要

**Priority**: Low

SillyTavern ではチャットの任意の地点に Bookmark を打ち、そこから分岐チャットを作成できる。
コンパニオン用途では優先度低いが、RP 用途では「もしあの時こう答えていたら」を試せる。

## タスク

- [ ] short_term_json の特定ターンからの分岐コピー機能
- [ ] 分岐チャットの一覧・選択 UI
- [ ] memory_builder が参照するチャット履歴の切り替え

## 前提 Issue

- チャット画面 — 直近メッセージの削除と再生成（Swipe）
