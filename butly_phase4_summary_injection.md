# Butly Phase 4: 要約注入切替
## build_system_instruction_from_blocks + MemoryBlockBuilder の改修

## 概要

Phase 3で生成パイプラインが完成した二層要約ファイル（mid_term_digest.txt / mid_term_relationship.txt）を、
実際のLLM応答生成時に注入する。config.pyのスイッチで RAW / 要約 を切り替え可能にする。

**このフェーズの目標**:
1. MemoryBlockBuilder.build() でdigest + relationship を読み込む
2. build_system_instruction_from_blocks() で要約を注入する
3. config スイッチで RAW ↔ 要約 を切り替え可能にする
4. 要約ファイルが存在しない場合はRAWにフォールバックする

**変更しないもの**:
- mid_term.txt の蓄積・アーカイブパイプライン（Phase 3で不変のまま）
- Housekeeper の処理
- main.py / app.py

---

## 対象ファイル

| ファイル | 変更種別 | 説明 |
|---------|---------|------|
| `butly_core/core/gatekeeper.py` | **改修** | MemoryBlockBuilder.build() と build_system_instruction_from_blocks() |
| `butly_core/core/memory.py` | **追記** | digest / relationship の読み込みメソッド追加 |
| `butly_core/config.py` | **追記** | 切替スイッチ追加 |

---

## 変更1: config.py への追記

SYSTEM_CONFIG["memory"] に切替スイッチを追加する。

```python
"memory": {
    "max_mid_term_chars": 30000,
    "short_term_limit": 6,
    "generate_mid_term_summaries": True,
    "max_digest_chars": 8000,
    "relationship_update_interval_days": 7,
    "use_summarized_mid_term": True,  # ★NEW: True=要約注入 / False=RAW注入
}
```

---

## 変更2: memory.py にメソッド追加

ButlyMemory クラスに以下の2メソッドを追加する。

```python
    def get_mid_term_digest(self):
        """エピソード付き事実ダイジェスト (mid_term_digest.txt) を読み込んで返す"""
        digest_file = self.instance_dir / "mid_term_digest.txt"
        try:
            if digest_file.exists():
                text = digest_file.read_text(encoding="utf-8").strip()
                return text if text else ""
        except Exception as e:
            print(f"[Memory] Failed to read mid_term_digest: {e}")
        return ""

    def get_mid_term_relationship(self):
        """関係性スナップショット (mid_term_relationship.txt) を読み込んで返す"""
        rel_file = self.instance_dir / "mid_term_relationship.txt"
        try:
            if rel_file.exists():
                text = rel_file.read_text(encoding="utf-8").strip()
                return text if text else ""
        except Exception as e:
            print(f"[Memory] Failed to read mid_term_relationship: {e}")
        return ""
```

---

## 変更3: gatekeeper.py — MemoryBlockBuilder.build() の改修

### 3.1 mid_term の読み込みロジック変更

現行コード（mid以上の処理部分）:

```python
        # --- mid 以上: mid_term を追加 ---
        mid_term = memory_manager.get_mid_term_text_content()
        blocks["mid_term"] = mid_term
        print(f"[Gatekeeper] MemoryBlock: {tier}（+ mid_term {len(mid_term)}文字）")
```

変更後:

```python
        # --- mid 以上: mid_term を追加 ---
        from butly_core.config import SYSTEM_CONFIG
        use_summary = SYSTEM_CONFIG.get("memory", {}).get("use_summarized_mid_term", False)
        
        if use_summary:
            # 要約モード: digest + relationship を使用
            digest = memory_manager.get_mid_term_digest()
            relationship = memory_manager.get_mid_term_relationship()
            
            if digest or relationship:
                blocks["mid_term_digest"] = digest
                blocks["mid_term_relationship"] = relationship
                blocks["mid_term"] = ""  # RAWは空にする
                blocks["mid_term_mode"] = "summary"
                total_chars = len(digest) + len(relationship)
                print(f"[Gatekeeper] MemoryBlock: {tier}（+ digest {len(digest)}文字 + relationship {len(relationship)}文字 = {total_chars}文字）")
            else:
                # 要約ファイルが存在しない → RAWにフォールバック
                mid_term = memory_manager.get_mid_term_text_content()
                blocks["mid_term"] = mid_term
                blocks["mid_term_mode"] = "raw_fallback"
                print(f"[Gatekeeper] MemoryBlock: {tier}（要約なし → RAWフォールバック {len(mid_term)}文字）")
        else:
            # RAWモード: 従来通り
            mid_term = memory_manager.get_mid_term_text_content()
            blocks["mid_term"] = mid_term
            blocks["mid_term_mode"] = "raw"
            print(f"[Gatekeeper] MemoryBlock: {tier}（+ mid_term RAW {len(mid_term)}文字）")
```

### 3.2 blocks dict のキー拡張

MemoryBlockBuilder が返す blocks dict に新しいキーが追加される:

| キー | 型 | 説明 |
|------|---|------|
| `mid_term` | str | RAWモード時: mid_term.txt の内容。要約モード時: 空文字 |
| `mid_term_digest` | str | 要約モード時: digest の内容。RAWモード時: 未設定 |
| `mid_term_relationship` | str | 要約モード時: relationship の内容。RAWモード時: 未設定 |
| `mid_term_mode` | str | `"summary"` / `"raw"` / `"raw_fallback"` |

---

## 変更4: gatekeeper.py — build_system_instruction_from_blocks() の改修

### 4.1 セクション3（MID-TERM MEMORY）の変更

現行コード:

```python
    # 3. MID-TERM MEMORY（中期記憶）— mid 以上
    if tier in ("mid", "cortex") and blocks.get("mid_term"):
        sections.append(f"=== MID-TERM MEMORY (中期記憶) ===\n{blocks['mid_term']}")
```

変更後:

```python
    # 3. MID-TERM MEMORY（中期記憶）— mid 以上
    if tier in ("mid", "cortex"):
        mid_term_mode = blocks.get("mid_term_mode", "raw")
        
        if mid_term_mode == "summary":
            # 要約モード: digest + relationship を個別セクションで注入
            digest = blocks.get("mid_term_digest", "")
            relationship = blocks.get("mid_term_relationship", "")
            
            if digest:
                sections.append(
                    f"=== MID-TERM DIGEST (中期記憶・事実ダイジェスト) ===\n"
                    f"※以下はAIの主観的な記憶です。直近の会話と矛盾する場合は、直近の会話を優先してください。\n"
                    f"{digest}"
                )
            if relationship:
                sections.append(
                    f"=== RELATIONSHIP SNAPSHOT (関係性スナップショット) ===\n"
                    f"※以下は現在の関係性のステータスです。Key Memoryの根幹を補完する参考情報として扱ってください。\n"
                    f"{relationship}"
                )
        else:
            # RAWモード（raw / raw_fallback）: 従来通り
            mid_term = blocks.get("mid_term", "")
            if mid_term:
                sections.append(f"=== MID-TERM MEMORY (中期記憶) ===\n{mid_term}")
```

### 4.2 注釈の意図

digest と relationship にはそれぞれ注釈を付与する:

- **digest**: 「※以下はAIの主観的な記憶です」— エピソード感情が含まれるため、LLMが「確定した事実」ではなく「記憶」として柔軟に扱えるようにする
- **relationship**: 「※Key Memoryの根幹を補完する参考情報」— Key Memoryとの役割分担を明示し、重複や矛盾時にKey Memoryが優先されるようにする

### 4.3 注入順序（最終形）

要約モード時:

```
1. SYSTEM INSTRUCTION（性格設定）
2. KEY MEMORY（根幹記憶）
3a. MID-TERM DIGEST（事実ダイジェスト）※注釈付き
3b. RELATIONSHIP SNAPSHOT（関係性）※注釈付き
4. CURRENT TIME（現在時刻）
5. RAG（cortex時のみ）※注釈付き
6. FLOATING SUMMARY（未整理記憶）※注釈付き
7. TIER INFO
```

---

## テスト計画

### 5.1 要約モードのテスト

1. `use_summarized_mid_term: true` に設定
2. mid_term_digest.txt と mid_term_relationship.txt が存在する状態でチャット
3. ログで「digest XXXX文字 + relationship XXXX文字」が表示されること
4. 応答品質を確認（RAWモードと比較）
5. トークン数の削減を確認（ログのprompt_len等で比較）

### 5.2 RAWモードのテスト

1. `use_summarized_mid_term: false` に設定
2. チャットしてログで「mid_term RAW XXXXX文字」が表示されること
3. 従来と同じ動作であること

### 5.3 フォールバックのテスト

1. `use_summarized_mid_term: true` に設定
2. mid_term_digest.txt と mid_term_relationship.txt を削除
3. チャットしてログで「要約なし → RAWフォールバック」が表示されること
4. RAWが正しく注入されていること

### 5.4 品質比較テスト（手動）

以下のパターンで同じ質問をして応答品質を比較:
- RAWモード: `use_summarized_mid_term: false`
- 要約モード: `use_summarized_mid_term: true`

確認項目:
- 過去の出来事を正しく参照できているか
- 会話のトーン・関係性が維持されているか
- 応答が不自然でないか

---

## 実装上の注意事項

1. **main.py / app.py は変更しない**: memory_blocks の構造が変わるが、main.py は blocks dict を直接参照せず build_system_instruction_from_blocks() に渡すだけなので影響なし。

2. **brain.py のフォールバック経路**: brain.py 内にも `memory_blocks is None` 時のフォールバック（従来の全記憶注入）が存在する。この経路は今回は変更しない。memory_blocksが渡される正規経路のみ改修する。

3. **blocks dict の後方互換**: `mid_term_mode` キーが存在しない古い blocks dict が渡された場合、`blocks.get("mid_term_mode", "raw")` でデフォルト "raw" になるため、後方互換は維持される。

4. **config の反映**: `use_summarized_mid_term` は `SYSTEM_CONFIG` に追加するため、`user_config.json` でオーバーライド可能。インスタンス別のconfig.jsonでもオーバーライド可能にしたい場合は `_get_config_value()` 経由で取得するが、Phase 4ではグローバル設定のみで十分。

5. **digest と relationship は個別セクション**: 1つの「MID-TERM」セクションにまとめるのではなく、別セクションとして注入する。理由は、LLMがセクションヘッダーを手がかりに情報を区別するため、「事実」と「関係性」を明確に分離した方が参照精度が上がるため。

---

## 完了の定義

- [ ] config.py に `use_summarized_mid_term` スイッチが追加されている
- [ ] memory.py に `get_mid_term_digest()` と `get_mid_term_relationship()` が追加されている
- [ ] MemoryBlockBuilder.build() が config に応じて要約/RAWを切り替える
- [ ] build_system_instruction_from_blocks() が要約モード時にdigest + relationship を個別注入する
- [ ] 注釈（※注釈付き）が各セクションに付与されている
- [ ] 要約ファイル不在時にRAWにフォールバックする
- [ ] `use_summarized_mid_term: false` で従来と同じ動作になる
- [ ] main.py / app.py / housekeeper.py に変更がない
