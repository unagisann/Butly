---
title: "MemoryProbe を『必要な時だけ記憶を探す』構造に改修する"
labels:
  - enhancement
  - gatekeeper
assignees:
  - unagisann
---

## 概要

現在の `MemoryProbe` は毎回 vector 検索（Layer 1）を走らせており、何か当たると `need = "memory_probe_hit"` として扱われる。結果として、「そもそも過去記憶を参照する必要がない発話」でも need が埋まり、tier が cortex に昇格してしまう。

理想は **「本当に必要な時だけ記憶を探し、不要な時は `need=null` のまま抜ける」** 構造。
LLM が既に出力している `continuity_need (cn)` などの意図スコアを活かしきれていないのが根本課題。

## 背景 / 現状の流れ

1. `ContextClassifier` が rc/ew/cn の 3 スコアを出力 → reflex / mid を決定
   - cn（continuity_need）は **tier 判定以外には使われていない**
2. `MemoryProbe` は cn に関係なく、Layer 1 (vector search) + Layer 1.5 (glossary match) を **無条件で実行**
3. Layer 1 がヒットしたら即 return
4. Gatekeeper の互換レイヤーが「mid + probe ヒット」を検知して tier を cortex に昇格 + `need = "memory_probe_{status}"` をセット

→ 実質的に「probe がヒットするかどうか」だけが need を決めている状態。

関連コード:
- [butly_core/core/gatekeeper/__init__.py:100-126](../../butly_core/core/gatekeeper/__init__.py#L100-L126)
- [butly_core/core/gatekeeper/memory_probe.py:98-118](../../butly_core/core/gatekeeper/memory_probe.py#L98-L118)
- [butly_core/core/gatekeeper/context_classifier.py:115-125](../../butly_core/core/gatekeeper/context_classifier.py#L115-L125)

## 再現ログ

```
[ContextClassifier] user='将来的にはGLOSSARYは会話からの部分一致とかで検索結果'
  scores: rc=0.70, ew=0.20, cn=0.80
  → tier=mid (1700ms)
[MemoryProbe] Layer 1 hit: 3 candidates, glossary=2 (1037ms)
[Gatekeeper] MemoryBlock: cortex（+ digest 2983文字 + relationship 357文字 = 3340文字）
[Gatekeeper] MemoryBlock: cortex（RAG probe hits=3）
```

→ 発話は未来の設計に関する話題だが、vector 類似で過去会話が引っかかり cortex 昇格している。

## 解決方針（検討中）

### A. 数値スコアでプローブをゲートする
- `cn < 閾値` ならプローブ丸ごとスキップ → `need=null` 確定
- **Pros:** 既存スコアを活用、LLM を問わず実装が容易、ブレにくい
- **Cons:** 閾値調整が必要。「ロボット的な判断」になりやすく、意図の文脈が落ちる

### B. `need` を LLM の明示出力に昇格させる
- ContextClassifier に `need: "past_fact" | "glossary" | "relationship" | null` を出させる
- `need=null` ならプローブ全スキップ。それ以外は種別に応じて検索先を絞る
- **Pros:** 意図が明示的で、search_targets も意味を持つ。`need=null` が自然な出力
- **Cons:** プロンプト改修、LLM 依存度↑、モデル差でブレが出やすい

### C. Glossary / Headline 先行 → vector 抑制
- 軽量な文字列マッチで拾えれば vector を省略
- **Pros:** 高速・軽量
- **Cons:** 「用語が載っている＝RAG 不要」は必ずしも真ではない。need の根本解決にはならない

### 方針所感
- A は数値閾値で機械的に切るため、モデル非依存で実装コストも低いが、
  「意図を汲む」という本来やりたい挙動とは方向性が異なる。
- B は理想に近いがモデル依存が強い。
- A + B のハイブリッド（スコアで一次ゲート、hit 後に need 種別で絞る）も選択肢。

## タスク

- [ ] 方針決定（A / B / C / ハイブリッド）
- [ ] `need` セマンティクスの再定義（null の意味を明文化）
- [ ] Gatekeeper の互換レイヤー（mid → cortex 昇格ロジック）を方針に合わせて整理
- [ ] MemoryProbe のゲート条件を実装
- [ ] reflex / mid / cortex それぞれの典型発話でログ確認
- [ ] `docs/gatekeeper_io_summary.ja.md` の更新

## 関連

- Phase 2 で互換レイヤー削除予定（`__init__.py` コメント参照）のタイミングに合わせるのが自然
