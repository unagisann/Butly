---
title: "RP 用設定画面（ロールプレイモード）"
labels:
  - feature
  - ui
  - roleplay
assignees:
  - unagisann
---

## 概要

**Priority**: Low（ペルソナ対応の基盤整備後）

コンパニオン用途とロールプレイ用途で必要な設定が異なる。
RP 用の設定画面を別タブまたはモードとして用意する。

## コンパニオンモード vs RP モードで異なる設定

| 設定項目 | コンパニオン | RP |
|---|---|---|
| Lorebook | 任意 | ほぼ必須 |
| Author's Note | 不要 | 必須（シーン制御） |
| ユーザーペルソナ | シンプル（名前程度） | 詳細（キャラ設定） |
| Swipe / 再生成 | あると便利 | 必須 |
| 表情・スプライト | あると良い | ほぼ必須 |
| グループチャット | 不要 | あると良い |
| 記憶の永続化 | 重要（年単位） | チャット単位 |
| SessionState | 自動更新 | 手動制御もほしい |
| first_mes (初回メッセージ) | 不要 | 必須 |

## UI 構成案

```
インスタンス設定
├── 基本設定（既存）
├── 🎭 RP 設定（新規タブ）
│   ├── Author's Note 編集
│   ├── Scenario 設定
│   ├── First Message 設定
│   ├── Lorebook リンク
│   ├── 表情マッピング設定
│   └── 出力フォーマット設定（アクション記法 *...* 等）
```

## タスク

- [ ] RP 設定タブの UI 設計
- [ ] config.json に `mode: "companion" | "roleplay"` フィールド追加
- [ ] モードに応じた UI 表示の出し分けロジック
- [ ] RP モード固有の設定項目の実装（Author's Note, Scenario, First Message）

## 前提 Issue

- Lorebook（キーワードトリガー型コンテキスト注入）
- ユーザーペルソナ（User Persona）
- Author's Note（動的注入位置制御）
