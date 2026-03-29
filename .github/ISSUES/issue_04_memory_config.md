---
title: "Memory 設定の補完（digest/relationship サイズ制御）"
labels:
  - ui
  - config
assignees:
  - unagisann
---

## 概要

**Priority**: Medium

中期記憶の要約版（digest / relationship）のサイズ制御が不足している。

## タスク

- [ ] `mid_term_digest` の最大文字数設定を `config.json` に追加
  - 現在はプロンプト側での制御のみ
  - config 値をプロンプトの `{max_chars}` 変数に渡す形が理想
- [ ] `mid_term_relationship` の最大文字数設定を `config.json` に追加
  - 同上
- [ ] インスタンス設定画面に上記2つの設定項目を追加
  - 既存の「長期記憶 最大文字数」の近くに配置

## 注意点

- 現在の `max_mid_term_chars` は RAW 累積テキスト用
- digest と relationship は別ファイルなので、別の設定値が必要
- プロンプト側（`midterm_digest.txt`, `midterm_relationship.txt`）に `{max_chars}` 変数を追加する修正も含む
