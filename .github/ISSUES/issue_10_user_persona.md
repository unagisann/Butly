---
title: "ユーザーペルソナ（User Persona）"
labels:
  - feature
  - persona
assignees:
  - unagisann
---

## 概要

**Priority**: Medium

SillyTavern では「ユーザー側」も複数ペルソナを持て、キャラごとに自動切り替えできる。
Butly の Key_Memory には user_name / nickname があるが、
ユーザーの詳細な自己記述をプロンプトに注入する仕組みがない。

コンパニオン用途でも「仕事モードの自分」「趣味モードの自分」の切り替えは自然なニーズ。

## 現状の Key_Memory 構造

```
AI Name: ジャービス
User Name: 悠希
Nickname: ゆうき
```

## 拡張案

```
butly_core/
├── user_personas/
│   ├── default.txt      ← デフォルトのユーザー記述
│   ├── work.txt         ← 仕事モード用
│   └── hobby.txt        ← 趣味モード用
```

- `build_context_prefix()` に `=== USER PERSONA ===` セクションを追加
- KEY_MEMORY の直後、MID-TERM の前に配置
- インスタンスごとに「どのユーザーペルソナを使うか」を config で指定
- SillyTavern のようにキャラ↔ペルソナの自動バインドも将来対応

## タスク

- [ ] ユーザーペルソナのファイル構造と読み込みロジック設計
- [ ] `memory_builder.py` に user_persona 注入を追加
- [ ] `build_context_prefix()` に `=== USER PERSONA ===` セクション追加
- [ ] config.json に `user_persona` フィールド追加
- [ ] UI: ユーザーペルソナ管理画面（作成・編集・切り替え）
- [ ] UI: インスタンス設定画面にユーザーペルソナ選択ドロップダウン追加
