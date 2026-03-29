---
title: "表情・スプライト表示（SessionState mood 連動）"
labels:
  - feature
  - multimedia
  - ui
assignees:
  - unagisann
---

## 概要

**Priority**: Low（RP 対応時に優先度上昇）

SillyTavern はキャラの感情に応じてスプライト画像を自動切替し、
さらに Live2D / VRM アニメーションもサポートする。
Butly の SessionState には `mood` フィールドがあるため、
そこからスプライト切替に繋げるパスは設計上自然。

## 最小 MVP

1. instance_dir に `sprites/` フォルダを追加
   - `neutral.png`, `happy.png`, `sad.png`, `angry.png`, `thinking.png` 等
2. SessionState の `mood` 値からスプライト画像を選択
3. チャット画面の AI アバター部分に表示

## タスク

- [ ] スプライト画像のフォルダ構造と命名規則を決定
- [ ] mood → スプライト名のマッピング設定（config or 固定ルール）
- [ ] `app.py` のチャット描画部分にスプライト表示ロジック追加
- [ ] Character Card V2 インポート時にアバター画像を default sprite として保存

## 将来拡張

- LLM による感情分類（SillyTavern の Classify 拡張に相当）
- Live2D / VRM 対応（Next.js 移行後が現実的）
