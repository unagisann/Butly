# 記憶ストア正規化計画書: 役割別フォーマット統一 + アクセス層の抽象化

🌐 **日本語**（English 版なし）

> **ステータス: 未着手（提案段階）。** 段階移行を前提に `planning/active` で管理する。
> 完了条件（§9）を満たしたら `archived` へ移す。
>
> 起票: 2026-06-16 / 想定スパン: 段階的、急がない / 利用者: 自分のみ（破壊的変更可）
>
> 関連: [memory_lifecycle.ja.md](../../reference/memory_lifecycle.ja.md)（記憶層の正本仕様） /
> [pydantic_settings_plan.ja.md](pydantic_settings_plan.ja.md)（設定レイヤの先行例）

## 1. なぜやるか（モチベーション）

記憶は「DB に統一すべきか？」という相談から出発した。実際の構造を棚卸しした結論は
**「DB が向くデータはすでに DB に入っており、残りは“役割が違う”ので一律 DB 化は逆効果」** だった。
本当の問題はストレージエンジンではなく、次の 2 点にある。

1. **フォーマットのドリフトと移行の中途半端さ**: 同じレイヤに旧形式と新形式のファイルが同居し、
   コードのあちこちにフォールバックが残る。インスタンス間でも状態がバラバラ（§3.2）。
2. **「1 インスタンスを構成するファイル群」の正本がない**: パス定義が
   [config.py](../../../butly_core/config.py#L82-L85)（一部）・
   [memory.py `__init__`](../../../butly_core/core/memory.py#L83-L124)（大半）・
   [sleeptime.py](../../../sleeptime.py) にベタ書きで散在し、読み手も書き手も追いづらい。
   これが「記憶がバラバラに保存されている」という体感の正体。

この 2 点を、**(1) 役割別にフォーマットを固定して互換を撤去**し、
**(2) アクセス層（Repository）を 1 枚噛ませて将来のバックエンド差し替えに備える** ことで解消する。

## 2. ゴール / 非ゴール

### ゴール
- **役割別の正規化**: 各記憶レイヤを A/B/C の 3 区分（§3.1）に整理し、区分内でフォーマットを 1 つに固定。
- **互換コードの撤去**: 旧ファイル・フォールバック・旧パスを、移行スクリプト実行 → コード削除の順で消す。
- **レイアウトの単一正本**: 全ファイル名を 1 箇所（`InstanceLayout`）が所有する。
- **アクセス層の抽象化**: レイヤ単位の小さな Store に read/write を集約し、`ButlyMemory` は薄い facade にする。
  呼び出し側（[runtime.py:87](../../../butly_core/runtime.py#L87) / [app.py:317](../../../app.py#L317) /
  [memory_builder.py](../../../butly_core/core/gatekeeper/memory_builder.py)）は**無改修**。
- **小型ローカル LLM 対応の土台**: A 層を YAML（構造）/ MD・txt（自由文）に揃え、注入テキストを手チューニングできる状態を保つ。

### 非ゴール（やらないこと）
- **全レイヤの DB 統一はしない。** B 層（ベクトル DB）は現状維持、A/C 層をファイルから DB へ移す変更は含めない。
  理由: A 層は LLM へ逐語注入され人間も編集する。DB 化は「行 → YAML/MD へ再レンダリング」の往復を増やし、
  小型 LLM 向けの手チューニング性を失う。C 層のローリングバッファ／ステージング移動はファイル操作の方がアト
  ミックで単純。
- **短期記憶のターン数は変えない。** `short_term_limit=6`（[config.py](../../../butly_core/config.py#L90)）と直近 N 注入
  （[memory_builder.py:230](../../../butly_core/core/gatekeeper/memory_builder.py#L230)）はそのまま。Store 化しても挙動は不変。
- **Stage 3（knowledge maturation / `memory_nodes`）の設計には踏み込まない。** 休眠中（`knowledge_maturation_enabled=False`）の別宿題。

## 3. 現状の棚卸し

### 3.1 役割で 3 区分に分かれる

| 区分 | レイヤ | 現フォーマット | 正本の所在 |
|---|---|---|---|
| **A. プロンプト注入 & 手編集** | `system_instruction.txt`(+`_low`) / `Key_Memory.yaml`(+旧`.txt`,`_low.txt`) / `glossary.yaml` / `mid_term_digest.txt` / `recent_snapshot.txt`(旧`mid_term_relationship.txt`) / `raw_memory_cache.txt`(派生) | txt / YAML | 手 or LLM が逐語で読む |
| **B. 構造化 & 検索** | `butly_memory.db`: `knowledge_cards`(+ベクトル) / `memory_nodes` / `memory_node_sources` / `access_logs` / `memory_maturation_runs` | **SQLite** | コサイン類似検索・索引・トランザクション |
| **C. 一時 / 派生 / 運用** | `short_term_json/`(直近6) / `session_digests/`(sleeptime で削除) / `memory_archive/{1_integrated,2_knowledgeized,3_log}` / `session_state.json` / `recent_digest_headlines.json` / `debug_logs/` | JSON / txt / ディレクトリ | ローリング & 「ファイル move = 昇格」 |

**要点: DB が活きるデータ（B）はもう DB にある。** だから "統一" で得をするのは B だけで、それは済んでいる。

### 3.2 ドリフト・移行の中途半端さ（撤去対象）

実インスタンスを確認した具体的な証拠:

| # | 症状 | 証拠 | あるべき姿 |
|---|---|---|---|
| 1 | Key_Memory が txt/yaml 二重 | `Butly` は `.yaml`+`.txt`+`_low.txt`、`Jarvis`/`test_luna` は `.txt` のみ。`SYSTEM_CONFIG["paths"]["key_memory"]` は今も `Key_Memory.txt`（[config.py:85](../../../butly_core/config.py#L85)） | **YAML 正本**。txt フォールバックと旧パスを撤去 |
| 2 | session digest が単一/ディレクトリ二重 | `session_digest.txt`（旧）を全インスタンスで seed しつつ `session_digests/`（新）も作る。さらに `floating_summaries/` / `floating_summary.txt` / `get_floating_summary()` の死蔵互換（[memory.py:603](../../../butly_core/core/memory.py#L603)） | **`session_digests/` 正本**。旧 seed と floating_* を撤去 |
| 3 | relationship の二重命名 | `recent_snapshot.txt`（新）と `mid_term_relationship.txt`（旧）が同居、getter は新→旧の順にフォールバック（[memory.py:421-438](../../../butly_core/core/memory.py#L421-L438)） | **`recent_snapshot.txt` 正本**。フォールバックと旧ファイルを撤去 |
| 4 | doc が実装より古い | `memory_lifecycle.ja.md` は「mid_term.txt に RAW 追記」と書くが、実装は **追記廃止**（[sleeptime.py:325](../../../sleeptime.py#L325)）。RAW は JSON 正本から読む | doc を実装に合わせる。`mid_term.txt` が完全に死んでいれば削除（§8 要確認） |
| 5 | プロファイルの移行残 | `Key_Memory.txt` → `config.json` の `agent_profile`/`user_profile` へ二段移行済み（[migrate_profiles.py](../../../migrate_profiles.py)） | 移行完了を全インスタンスで確認し、旧ヘッダ抽出経路を撤去 |

### 3.3 既存の移行スクリプト（再利用する）

- [migrate_key_memory.py](../../../migrate_key_memory.py) — `Key_Memory.txt → Key_Memory.yaml`（`--all` / `--instance` / `--dry-run`）。
- [migrate_profiles.py](../../../migrate_profiles.py) — プロファイル 2 段移行（冪等）。
- [migrate_embeddings.py](../../../migrate_embeddings.py) — B 層の re-embed。本計画では触らない。

## 4. Item 1 — 役割別フォーマットの固定と正規化

### 4.1 正本フォーマット（決定表）

| レイヤ | 正本ファイル | 形式 | 根拠 |
|---|---|---|---|
| 人格定義 | `system_instruction.txt`(+`_low`) | txt（MD 可） | 大きめの自由文。逐語注入 |
| 根幹記憶 | `Key_Memory.yaml`(+`_low` は YAML の low モード) | **YAML**(`version`+`entries`) | 構造あり・UI 編集・ID 採番（[key_memory.py](../../../butly_core/core/key_memory.py)） |
| 用語集 | `glossary.yaml` | **YAML**(`version`+`entries`) | 既に正規化済み |
| 事実ダイジェスト | `mid_term_digest.txt` | txt | LLM 生成の自由文。差分追記＋上限 |
| 関係性 | `recent_snapshot.txt` | txt | LLM 生成の自由文 |
| RAW 読みキャッシュ | `raw_memory_cache.txt` | txt（派生） | `2_knowledgeized` から `RawMemoryReader` が再生成可能 |

**原則: 構造があるもの = YAML(`version`+`entries`)、自由文 = txt/MD。** これが小型 LLM 向け
（`raw_injection_format: plaintext\|markdown\|compact`）の前提とも噛み合う。

### 4.2 互換撤去の手順（順序が肝）

各レイヤについて **「移行スクリプトを全インスタンスへ 1 回流す → コードのフォールバックを削除 → 旧ファイル削除」** の順で進める。逆順だと読めなくなるインスタンスが出る。

1. **棚卸しと凍結**: §4.1 を正本として固定。`InstanceLayout`（§5.1）に正本名を集約する前提で着手。
2. **Key_Memory**: `migrate_key_memory.py --all` 実行 → 全インスタンスに `.yaml` 生成を確認 →
   `get_key_memory()` の txt フォールバック（[memory.py:290-312](../../../butly_core/core/memory.py#L290-L312)）と
   `key_memory.py` の `TXT_FILENAME` 経路を削除 → `SYSTEM_CONFIG["paths"]["key_memory"]` を撤廃/yaml 化 → `.txt` 削除。
3. **relationship**: `recent_snapshot.txt` を正本に確定 → `get_recent_snapshot()` の旧名フォールバック削除 → `mid_term_relationship.txt` 削除。
4. **session digest**: `session_digests/` を正本に確定 → `session_digest.txt` の seed と `floating_*`／`get_floating_summary()` を削除 → 旧ファイル削除。
5. **mid_term.txt**: 死蔵確認（§8）後に getter・seed・ファイルを削除。
6. **doc/テスト同期**: `memory_lifecycle.ja.md`/`.md` を実装に合わせて更新。`tests/test_*memory*` の互換前提を更新。
7. **移行スクリプトの後始末**: 全インスタンス移行完了後、役目を終えた `migrate_*` を `scripts/` へ隔離 or 削除。

## 5. Item 2 — アクセス層（Repository）の抽象化

### 5.1 レイアウトの単一正本: `InstanceLayout`

全ファイル名・パスを 1 つの dataclass に集約し、`memory.py` / `sleeptime.py` / `config.py["paths"]` の
ベタ書きを置き換える。**「1 インスタンスが何で構成されるか」がここを読めば分かる**状態にする。

```python
# butly_core/core/memory_store/layout.py
@dataclass(frozen=True)
class InstanceLayout:
    instance_dir: Path

    @property
    def key_memory_yaml(self) -> Path: return self.instance_dir / "Key_Memory.yaml"
    @property
    def glossary_yaml(self) -> Path:   return self.instance_dir / "glossary.yaml"
    @property
    def system_instruction(self) -> Path: return self.instance_dir / "system_instruction.txt"
    @property
    def mid_term_digest(self) -> Path: return self.instance_dir / "mid_term_digest.txt"
    @property
    def recent_snapshot(self) -> Path: return self.instance_dir / "recent_snapshot.txt"
    @property
    def short_term_dir(self) -> Path:  return self.instance_dir / "short_term_json"
    @property
    def session_digest_dir(self) -> Path: return self.instance_dir / "session_digests"
    @property
    def session_state(self) -> Path:   return self.instance_dir / "session_state.json"
    @property
    def db(self) -> Path:              return self.instance_dir / "butly_memory.db"
    # ... archive_*, raw_memory_cache, debug_logs, recent_digest_headlines ...
```

### 5.2 レイヤ単位の Store

各レイヤを「読み書きを所有する小さなオブジェクト」に切り出す。書き込みは
[atomic_write_text](../../../butly_core/io_utils.py#L26) を必ず経由（規約準拠）。

| Store | 担当ファイル | 主メソッド | 区分 |
|---|---|---|---|
| `InstructionStore` | system_instruction(+low) | `get()` / `get_low()` | A |
| `KeyMemoryStore` | Key_Memory.yaml | `get_text()` / `entries()` / `save(entries)`（`key_memory.py` をラップ） | A |
| `GlossaryStore` | glossary.yaml | `get_text()` / `raw()` / `save(data)` | A |
| `DigestStore` | mid_term_digest.txt | `get()` / `append(text)`（上限→3_log アーカイブ） | A |
| `RelationshipStore` | recent_snapshot.txt | `get()` / `write(text)` | A |
| `RawMemoryCacheStore` | raw_memory_cache.txt | `get()` / `rebuild()`（`RawMemoryReader` をラップ） | A(派生) |
| `ShortTermStore` | short_term_json/ | `append_turn()` / `recent(limit)` / `count()` / `flush_oldest()` | C |
| `SessionDigestStore` | session_digests/ | `get_joined()` / `add()` / `clear()` | C |
| `SessionStateStore` | session_state.json | `load()` / `save()` | C |
| `KnowledgeRepository` | butly_memory.db | 検索/INSERT（既存 `ButlyBrain`/DB 層をラップ） | B |

> **短期記憶のターン数は `ShortTermStore` に閉じる。** `short_term_limit` 参照も直近 N 注入もここだけが知る。
> 挙動は現状と完全に同一（リファクタのみ）。

### 5.3 `ButlyMemory` は facade に

既存の公開メソッド名（`get_key_memory()` / `get_mid_term_digest()` / `load_recent_sessions()` …）は
**そのまま残し、内部で対応する Store に委譲するだけ**にする。呼び出し側は一切変えない。

```python
class ButlyMemory:
    def __init__(self, base_dir, instance_name="00_master"):
        layout = InstanceLayout(Path(base_dir) / "butly_core" / "instances" / instance_name)
        self._key_memory = KeyMemoryStore(layout)
        self._digest = DigestStore(layout)
        self._short_term = ShortTermStore(layout)
        # ...
    def get_key_memory(self) -> str:          return self._key_memory.get_text()
    def load_recent_sessions(self, limit=None): return self._short_term.recent(limit)
```

これが「将来対応」の本体: 後で特定レイヤだけ files→DB に替えたくなっても、**その Store の中だけ**を
直せば facade も呼び出し側も無傷。「今 DB にするか」を今決める必要がなくなる。

### 5.4 書き手の集約（任意・後段）

A 層の書き込みは現状 [sleeptime.py](../../../sleeptime.py) に散在する。Phase C（§6）で sleeptime 側の
mid_term_digest / recent_snapshot 書き込みを `DigestStore` / `RelationshipStore` 経由に寄せれば、
「読み手も書き手も同じ Store」になり、パスのベタ書きが消える。

## 6. 段階移行（Item 1 + 2 の合流）

| Phase | 内容 | 破壊性 | 完了の目印 |
|---|---|---|---|
| **0** | `InstanceLayout` 追加、`memory.py.__init__` のパス定義をそこ経由に置換 | なし（純リファクタ） | 既存テスト緑 |
| **1** | 既存の移行スクリプトを全インスタンスへ実行（Key_Memory→yaml、profiles 確認） | データ移行のみ | 全インスタンスに正本ファイルが揃う |
| **2** | A 層を Store 化（`ButlyMemory` は facade 委譲）。互換フォールバックを削除（§4.2 の 2〜5） | 旧形式は移行済み前提 | フォールバック分岐が grep で 0 |
| **3** | C 層を Store 化（`ShortTermStore` 等）。挙動不変を確認 | なし | `test_memory*` 緑、ターン数挙動不変 |
| **4** | sleeptime の書き手を Store に寄せる（§5.4）。doc/テスト同期 | なし | パスのベタ書きが Layout に一本化 |
| **5** | 死蔵ファイル・旧パス・役目を終えた移行スクリプトの削除 | クリーンアップ | §9 完了条件 |

各 Phase 末で `./scripts/check_before_push.sh` を緑にしてから次へ。

## 7. ディレクトリレイアウト（提案）

```
butly_core/core/memory_store/
├── __init__.py        # Store 群と InstanceLayout の再エクスポート
├── layout.py          # InstanceLayout（パスの単一正本）
├── instruction.py     # InstructionStore
├── key_memory.py      # KeyMemoryStore（既存 core/key_memory.py をラップ）
├── glossary.py        # GlossaryStore
├── digest.py          # DigestStore / RelationshipStore
├── short_term.py      # ShortTermStore
├── session_digest.py  # SessionDigestStore
├── session_state.py   # SessionStateStore
└── raw_cache.py       # RawMemoryCacheStore（既存 raw_memory_reader.py をラップ）
```

`butly_core/core/memory.py` は `ButlyMemory` facade として残す（公開 API 不変）。

## 8. リスク・未確定事項

1. **`mid_term.txt` は本当に死蔵か**: 追記は廃止（[sleeptime.py:325](../../../sleeptime.py#L325)）だが、
   `use_summarized_mid_term=False`（RAW モード）の読み経路が残っていないか要確認。生きていれば削除はしない。
2. **インスタンス間の移行状態差**: `Jarvis`/`test_luna` が Key_Memory.txt のままなので、Phase 1 を**全**インスタンスに
   流すこと。流す前にコードのフォールバックを消すと読めなくなる（順序厳守）。
3. **`_low` 系の扱い**: `Key_Memory_low.txt` / `system_instruction_low.txt` は YAML low モードと併存。
   low を YAML 派生にするか txt のまま残すか、Phase 2 着手時に確定。
4. **テストの互換前提**: `tests/test_key_memory.py` 等が txt フォールバックを前提にしていれば更新が必要。
5. **`00_master` テンプレート**: 新規インスタンス生成時の seed を新正本に合わせる（旧ファイルを seed しない）。

## 9. 完了条件

- [ ] 全インスタンスが §4.1 の正本ファイルのみを持つ（旧 `.txt`／`mid_term_relationship.txt`／`session_digest.txt`／`floating_*` が無い）。
- [ ] パス定義が `InstanceLayout` に一本化され、`memory.py`/`sleeptime.py` 内のベタ書きが消えている。
- [ ] `ButlyMemory` の公開メソッドが Store へ委譲し、呼び出し側（runtime.py/app.py/memory_builder.py）が無改修。
- [ ] 互換フォールバック分岐が grep で 0。
- [ ] `memory_lifecycle.ja.md`/`.md` が実装と一致。
- [ ] `./scripts/check_before_push.sh` 緑。短期記憶のターン数挙動が現状と不変。

## 10. 影響範囲（ファイル）

- 追加: `butly_core/core/memory_store/`（§7）
- 改修: [memory.py](../../../butly_core/core/memory.py)（facade 化）/ [sleeptime.py](../../../sleeptime.py)（Phase 4）/
  [config.py](../../../butly_core/config.py#L82-L85)（`paths` 整理）
- 同期: [memory_lifecycle.ja.md](../../reference/memory_lifecycle.ja.md) / `.md`、`tests/test_*memory*`
- 後始末: [migrate_key_memory.py](../../../migrate_key_memory.py) / [migrate_profiles.py](../../../migrate_profiles.py)（移行完了後に隔離/削除）
