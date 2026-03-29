---
title: "Character Card V2 インポート"
labels:
  - feature
  - integration
assignees:
  - unagisann
---

## 概要

**Priority**: Medium

SillyTavern / Backyard AI エコシステムの Character Card（V2 仕様: PNG 画像にメタデータ埋め込み）を
Butly のインスタンスとしてインポートする機能。
ローカル LLM ユーザーが既存のキャラ資産をそのまま Butly で使える。

## Character Card V2 のフィールド → Butly マッピング

| Card V2 フィールド | Butly 対応先 |
|---|---|
| `name` | config.json `agent.name` / Key_Memory の AI名 |
| `description` | system_instruction.txt に統合 |
| `personality` | system_instruction.txt に追記 |
| `scenario` | system_instruction.txt に追記（またはセッション初期コンテキスト） |
| `first_mes` | 初回チャット時の AI 応答として使用 |
| `mes_example` | system_instruction.txt の few-shot セクションに追記 |
| `creator_notes` | インポート時に表示のみ（注入しない） |
| `system_prompt` | system_instruction.txt の先頭に配置 |
| `post_history_instructions` | build_context_prefix の末尾に注入（Author's Note 相当） |
| `tags` | config.json のメタデータとして保存 |
| `character_book` (embedded lorebook) | → Lorebook issue の lorebook_entries にインポート |

## タスク

- [ ] PNG メタデータ（tEXt チャンク）からの Character Card JSON 抽出
- [ ] フィールドマッピング + インスタンス自動生成ロジック
- [ ] embedded lorebook の自動インポート（lorebook_entries へ）
- [ ] UI: インポート画面（ファイルアップロード → プレビュー → 確認 → 生成）
- [ ] Backyard AI の `.byaf` 形式対応は将来検討

## 注意点

- `description` と `personality` の統合方法はユーザーに選択させる（結合 or 分離）
- `system_prompt` がある場合、既存の personality テンプレートとの競合処理
- avatar 画像の保存先を決める必要がある（instance_dir/avatar.png 等）

## 前提 Issue

- Lorebook（キーワードトリガー型コンテキスト注入）— embedded lorebook のインポート先
- Author's Note（動的注入位置制御）— post_history_instructions のインポート先
