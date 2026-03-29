---
title: "チャット画面 — 直近メッセージの削除と再生成（Swipe）"
labels:
  - ui
  - feature
assignees:
  - unagisann
---

## 概要

**Priority**: Medium

現在、直近の会話ターン（user + model）をUI上から削除する手段がない。
また、応答の再生成機能もない。
SillyTavern の Swipe（応答を複数生成して選択）や Backyard AI の再生成機能に相当。

## タスク

- [ ] 直近の model 応答の横に「🗑️ 削除」ボタンを追加
  - `session_state.messages` から末尾の user + model を削除
  - 対応する `short_term_json` ファイルも削除（API エンドポイント追加が必要な場合あり）
- [ ] 直近の model 応答の横に「🔄 再生成」ボタンを追加
  - `session_state.messages` から末尾の model 応答のみ削除
  - 同じ user メッセージを `/chat` に再送信
  - 対応する `short_term_json` の model 応答も更新

## 注意点

- Streamlit の再描画モデルとの相性に注意（`st.rerun()` のタイミング）
- 再生成時は Gatekeeper の再判定も走るため、tier が変わる可能性がある（これは許容）
- 将来的に Swipe（複数候補の保持と選択）に拡張する余地を残す設計にする
