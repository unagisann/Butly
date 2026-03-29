---
title: "Context Cache の UI 削除"
labels:
  - ui
  - cleanup
assignees:
  - unagisann
---

## 概要

**Priority**: High

Context Cache はデフォルト OFF で、現在のトークン規模（〜1万トークン）では恩恵がない。
設定画面からトグルを削除し、`app.py` の `initialize_system` 内の `prepare_cache` 呼び出しも削除する。
`gemini.py` のキャッシュ関連メソッド自体はコード上残す（将来のエージェント機能用）。

## タスク

- [ ] `app.py` `render_instance_settings_screen()` から「コンテキストキャッシュ」トグルを削除
- [ ] `app.py` `initialize_system()` 内の `prepare_cache` 呼び出しを削除
- [ ] `cached_content` の戻り値を `initialize_system` から除去（または常に `None`）
- [ ] config のデフォルト値 `use_context_cache: false` は維持

