# Gatekeeper tier/RAG 分離リファクタリング — 影響範囲一覧

> 作成日: 2026-04-04
> 目的: `"cortex"` 廃止 + tier/RAG 分離に向けた影響箇所の洗い出し
> 対象: `butly_core/` および `tests/` 配下（`docs/` は除外）
>
> **ステータス（2026-05-17）: 完了。** Phase 1.5 + cleanup により本ドキュメントの全項目は対応済み。
> 現状の仕様は [gatekeeper_io_summary.ja.md](gatekeeper_io_summary.ja.md) を参照。
> 本ファイルは作業履歴として保存。

---

## 1. `"cortex"` を参照している箇所

### butly_core/ (ソースコード)

| ファイル | 行 / 関数 | 内容 | cortex廃止の影響 |
|---|---|---|---|
| `butly_core/core/gatekeeper/tier_classifier.py` | L55 docstring | 返却値の型注釈 `"reflex" \| "mid" \| "cortex"` | **影響あり** |
| `butly_core/core/gatekeeper/tier_classifier.py` | L110-112 `_determine_tier_from_scores()` | `ml >= 0.7` のとき `return "cortex"` | **影響あり**（分岐の削除/変更） |
| `butly_core/core/gatekeeper/__init__.py` | L83-86 `Gatekeeper.classify()` | `if tier == "cortex":` → SearchPlanner を呼ぶ | **影響あり**（RAG分離の中核） |
| `butly_core/core/gatekeeper/search_planner.py` | L4 モジュールdocstring | 「cortex 判定時のみ呼ばれ」 | **影響あり**（呼び出し条件変更） |
| `butly_core/core/gatekeeper/search_planner.py` | L17 クラスdocstring | 「cortex 判定時に RAG 検索のキーワードを生成する」 | **影響あり** |
| `butly_core/core/gatekeeper/memory_builder.py` | L149 docstring | `"rag_context" : cortex のみ` | **影響あり** |
| `butly_core/core/gatekeeper/memory_builder.py` | L168 `build()` docstring | tier パラメータの型 `"reflex" \| "mid" \| "cortex"` | **影響あり** |
| `butly_core/core/gatekeeper/memory_builder.py` | L207 `build()` | `if tier == "reflex":` → short_term のみで return | 要確認 |
| `butly_core/core/gatekeeper/memory_builder.py` | L240 `build()` | `if tier == "mid":` → mid_term まで return | 要確認 |
| `butly_core/core/gatekeeper/memory_builder.py` | L243-270 `build()` | `# cortex のみ: RAG 検索を実施` ブロック全体 | **影響あり**（RAG実行条件の変更） |
| `butly_core/core/gatekeeper/memory_builder.py` | L482 `_build_mid_term()` | `if tier not in ("mid", "cortex"): return None` | **影響あり** |
| `butly_core/core/gatekeeper/memory_builder.py` | L528 `_build_rag()` | `if tier != "cortex" or not rag_context: return None` | **影響あり**（RAG表示条件） |
| `butly_core/core/gatekeeper/memory_builder.py` | L553 `_build_tier_info()` | `if tier in ("mid", "cortex") and topic:` | **影響あり** |
| `butly_core/chat/service.py` | L143-145 `send_message()` | GK無効時: `tier = "cortex" if use_rag else "mid"` | **影響あり**（フォールバック変更） |
| `butly_core/chat/service.py` | L164 `send_message()` | `brain=brain if (tier == "cortex" and use_rag) else None` | **影響あり**（RAG渡し条件） |
| `butly_core/chat/service.py` | L189 コメント | `cortex + need有効時のみ` | 影響なし（コメントのみ） |

### butly_core/prompts/

| ファイル | 行 | 内容 | cortex廃止の影響 |
|---|---|---|---|
| `butly_core/prompts/control/tier_classifier.txt` | L1 | `"prefrontal cortex (gatekeeper)"` — メタファー使用 | 影響なし（比喩表現） |
| `butly_core/prompts/control/tier_classifier.txt` | L32 | `memory_reference_likelihood` のスコア基準定義 | 要確認（tier名の出力形式が変わる場合） |
| `butly_core/prompts/prompt_registry.yaml` | L20 | `description: "cortex only"` | **影響あり** |

### tests/

| ファイル | 行 / クラス | 内容 | cortex廃止の影響 |
|---|---|---|---|
| `tests/test_tier_classifier.py` | L21-29 `test_high_memory_reference_returns_cortex` | ml=0.8 → cortex を期待 | **影響あり** |
| `tests/test_tier_classifier.py` | L81-89 `test_boundary_memory_reference_0_7_returns_cortex` | ml=0.7 → cortex 境界値テスト | **影響あり** |
| `tests/test_tier_classifier.py` | L92 `test_boundary_memory_reference_0_69` | ml=0.69 → cortex にはならないテスト | **影響あり** |
| `tests/test_tier_classifier.py` | L116-124 `test_explicit_past_reference_returns_cortex` | 過去参照 → cortex テスト | **影響あり** |
| `tests/test_tier_classifier.py` | L138 パースの統合テスト | `result["tier"] == "cortex"` | **影響あり** |
| `tests/test_tier_classifier.py` | L201 統合テスト | `tier in ("reflex", "mid", "cortex")` — 3値前提 | **影響あり** |
| `tests/test_memory_block_builder.py` | L134-198 `TestCortexTier` クラス全体 | cortex tier のブロック構築テスト (5テスト) | **影響あり** |
| `tests/test_memory_block_builder.py` | L210 パラメトライズ | `["reflex", "mid", "cortex"]` — 3値前提 | **影響あり** |
| `tests/test_memory_block_builder.py` | L225 パラメトライズ | `["reflex", "mid", "cortex"]` — 3値前提 | **影響あり** |
| `tests/test_system_instruction_builder.py` | L164-186 `TestCortexInstruction` クラス | cortex の system_instruction テスト (2テスト) | **影響あり** |
| `tests/test_system_instruction_builder.py` | L205 テストケース | `"tier": "cortex"` | **影響あり** |
| `tests/test_system_instruction_builder.py` | L231 パラメトライズ | `["reflex", "mid", "cortex"]` — 3値前提 | **影響あり** |
| `tests/test_system_instruction_builder.py` | L306-325 `test_cortex_includes_rag` 等 | cortex の RAG context_prefix テスト | **影響あり** |
| `tests/test_system_instruction_builder.py` | L357 パラメトライズ | `["reflex", "mid", "cortex"]` — 3値前提 | **影響あり** |
| `tests/test_system_instruction_builder.py` | L374 パラメトライズ | `["reflex", "mid", "cortex"]` — 3値前提 | **影響あり** |
| `tests/test_system_instruction_builder.py` | L397-449 複数テスト | `"tier": "cortex"` のテストケース (4テスト) | **影響あり** |
| `tests/test_context_levels.py` | L254 テストデータ | `"tier": "cortex"` | **影響あり** |
| `tests/test_context_levels.py` | L328 テストデータ | `"tier": "cortex"` | **影響あり** |
| `tests/test_session_state.py` | L39 テストデータ | `"last_tier": "cortex"` | **影響あり** |
| `tests/test_session_state.py` | L171-186 `increment_turn` テスト | `increment_turn("cortex")` | **影響あり** |
| `tests/test_integration.py` | L54-68 `test_classify_technical_as_mid_or_cortex` | 3値前提のアサーション | **影響あり** |
| `tests/test_integration.py` | L70-86 `test_classify_past_reference_as_cortex` | cortex 判定の統合テスト | **影響あり** |
| `tests/conftest.py` | L111 フィクスチャ | `"last_tier": "mid"` — デフォルト値 | 影響なし |

---

## 2. tier 3値分岐 (`reflex`/`mid`/`cortex`) の前提コード

| ファイル | 行 / 関数 | 内容 |
|---|---|---|
| `butly_core/core/gatekeeper/tier_classifier.py` | L107-119 `_determine_tier_from_scores()` | 3値分岐ロジック本体 |
| `butly_core/core/gatekeeper/memory_builder.py` | L207-270 `MemoryBlockBuilder.build()` | reflex→return / mid→return / cortex→RAG の3段階 |
| `butly_core/core/gatekeeper/memory_builder.py` | L482 `_build_mid_term()` | `tier not in ("mid", "cortex")` |
| `butly_core/core/gatekeeper/memory_builder.py` | L528 `_build_rag()` | `tier != "cortex"` |
| `butly_core/core/gatekeeper/memory_builder.py` | L553 `_build_tier_info()` | `tier in ("mid", "cortex")` |
| `butly_core/core/gatekeeper/__init__.py` | L86 `Gatekeeper.classify()` | `if tier == "cortex"` → SearchPlanner |
| `butly_core/chat/service.py` | L145 `send_message()` | `"cortex" if use_rag else "mid"` |
| `butly_core/chat/service.py` | L164 `send_message()` | `tier == "cortex" and use_rag` |
| `butly_core/core/gatekeeper/session_state.py` | L23 `DEFAULT_STATE` | `"last_tier": "mid"` — デフォルト値 |

---

## 3. `SearchPlanner` の参照箇所

| ファイル | 行 | 内容 | cortex廃止の影響 |
|---|---|---|---|
| `butly_core/core/gatekeeper/search_planner.py` | L16 | `class SearchPlanner` — クラス定義 | **影響あり**（呼び出し条件変更） |
| `butly_core/core/gatekeeper/__init__.py` | L19 | `from ...search_planner import SearchPlanner` | 影響なし |
| `butly_core/core/gatekeeper/__init__.py` | L42 | `self.search_planner = SearchPlanner(base_dir)` | 影響なし |
| `butly_core/core/gatekeeper/__init__.py` | L87 | `self.search_planner.plan(...)` — cortex時のみ実行 | **影響あり** |
| `butly_core/core/gatekeeper/__init__.py` | L160 | `__all__` re-export | 影響なし |
| `butly_core/prompts/prompt_registry.yaml` | L18-20 | `search_planner` テンプレート登録 | 要確認 |
| `tests/test_prompt_loader.py` | L36 | テンプレート名一覧に `"search_planner"` 含む | 影響なし |

---

## 4. `TierClassifier` の参照箇所

| ファイル | 行 | 内容 | cortex廃止の影響 |
|---|---|---|---|
| `butly_core/core/gatekeeper/tier_classifier.py` | L16 | `class TierClassifier` — クラス定義 | **影響あり**（`_determine_tier_from_scores` 改修） |
| `butly_core/core/gatekeeper/__init__.py` | L17 | import | 影響なし |
| `butly_core/core/gatekeeper/__init__.py` | L40 | `self.tier_classifier = TierClassifier(base_dir)` | 影響なし |
| `butly_core/core/gatekeeper/__init__.py` | L68 | `self.tier_classifier.classify(...)` | 影響なし（呼ぶこと自体は変わらず） |
| `butly_core/core/gatekeeper/__init__.py` | L158 | `__all__` re-export | 影響なし |
| `butly_core/prompts/__init__.py` | L33 | `"tier_classifier": "GATEKEEPER_CLASSIFY_PROMPT"` | 影響なし |
| `butly_core/prompts/__init__.py` | L187 | `GATEKEEPER_CLASSIFY_PROMPT = ...("tier_classifier")` | 影響なし |
| `tests/test_tier_classifier.py` | L10 | `from ...tier_classifier import TierClassifier` | 影響なし |
| `tests/test_tier_classifier.py` | L19 | `return TierClassifier()` — フィクスチャ | 影響なし |
| `tests/test_prompt_loader.py` | L19-96 | 4箇所で `tier_classifier` テンプレート参照 | 影響なし |

---

## 5. `memory_reference_likelihood` の参照箇所

| ファイル | 行 | 内容 | cortex廃止の影響 |
|---|---|---|---|
| `butly_core/core/gatekeeper/tier_classifier.py` | L59 docstring | スコアキー定義 | 要確認（スコア自体は残す可能性あり） |
| `butly_core/core/gatekeeper/tier_classifier.py` | L97 | `ml=...` のログ出力 | 要確認 |
| `butly_core/core/gatekeeper/tier_classifier.py` | L107 `_determine_tier_from_scores()` | `ml = scores.get("memory_reference_likelihood", 0)` — **cortex判定の閾値条件** | **影響あり** |
| `butly_core/core/gatekeeper/tier_classifier.py` | L141 `_parse_response()` | クランプ対象キーの一覧 | 要確認 |
| `butly_core/prompts/control/tier_classifier.txt` | L32-57 | スコア基準定義 + 出力例 | 要確認 |
| `tests/test_tier_classifier.py` | 全テスト (16箇所) | 各テストケースのスコア値設定 | **影響あり** |

---

## 6. tier 依存の動作モード (`low` モード / `context_levels`)

| ファイル | 行 | 内容 | cortex廃止の影響 |
|---|---|---|---|
| `butly_core/core/gatekeeper/memory_builder.py` | L33 `CONTEXT_LEVEL_PRESETS` | プリセット定義（normal/compact/low/custom） | 影響なし（tier とは独立） |
| `butly_core/core/gatekeeper/memory_builder.py` | L114 `_resolve_levels()` | context_levels からレベル辞書を解決 | 影響なし |
| `butly_core/core/gatekeeper/memory_builder.py` | L323 | `si_level == "low"` → `get_system_instruction_low()` | 影響なし |
| `butly_core/core/gatekeeper/memory_builder.py` | L328 | `km_level == "low"` → `get_key_memory_low()` | 影響なし |
| `tests/test_context_levels.py` | 全体 (35テスト) | context_levels テスト群 | 影響なし（tier とは独立だが、テストデータに `"cortex"` を含むものが2件 → セクション1で列挙済み） |

---

## サマリー

| カテゴリ | **影響あり** | 要確認 | 影響なし |
|---|---|---|---|
| ソース (`butly_core/`) | **14箇所** | 4箇所 | 10箇所 |
| テスト (`tests/`) | **22箇所** | 0箇所 | 8箇所 |
| プロンプト | **1箇所** | 2箇所 | 0箇所 |

---

## 改修の中核ファイル（優先度順）

1. **`butly_core/core/gatekeeper/tier_classifier.py`** — `_determine_tier_from_scores()`: cortex 分岐の削除 / RAGフラグ分離
2. **`butly_core/core/gatekeeper/__init__.py`** — `Gatekeeper.classify()`: `if tier == "cortex"` の条件変更
3. **`butly_core/core/gatekeeper/memory_builder.py`** — `build()` / `_build_rag()` / `_build_mid_term()`: 3段階→2段階+RAGフラグ
4. **`butly_core/chat/service.py`** — GK無効時フォールバック + brain 渡し条件
5. **`butly_core/core/gatekeeper/search_planner.py`** — docstring更新 + 呼び出し条件の変更
