---
title: "モデルパラメータ設定の拡充（top_p 等）"
labels:
  - ui
  - enhancement
assignees:
  - unagisann
---

## 概要

**Priority**: Low

インスタンス設定画面に `temperature` と `max_tokens` はあるが、`top_p` が UI に露出していない。
また、プロバイダによる対応差を明示する。

## タスク

- [ ] インスタンス設定画面に `top_p` スライダーを追加
- [ ] `top_k` は Gemini 固有のため、Gemini 選択時のみ表示（または caption で注記）
- [ ] 各パラメータの横に対応プロバイダの注記を追加
  - `temperature`: 全プロバイダ対応
  - `max_tokens`: 全プロバイダ対応
  - `top_p`: 全プロバイダ対応
  - `top_k`: Gemini のみ

## 対象外（この Issue では扱わない）

- `reasoning` 設定: プロバイダ横断の統一仕様がないため見送り
- `streaming`: Next.js 移行時に本格対応
