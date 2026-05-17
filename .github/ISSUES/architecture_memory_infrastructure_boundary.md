# Butly Memory を外部境界のある記憶基盤として整理する

## 背景

Butly は汎用的なエージェント/LLMフレームワークではなく、**多層的な記憶を持つパーソナルAIコンパニオン基盤**として設計されている。

現在の Butly にはすでに以下の強みがある。

- 短期記憶
- floating summary
- mid-term digest
- relationship snapshot
- knowledge cards / RAG
- Key Memory
- Glossary
- Gatekeeper / MemoryProbe
- Sleeptime
- マルチインスタンス
- マルチプロバイダー

一方で、Mem0 / LangMem / MemOS / Letta などの記憶・エージェント系OSSと比較すると、Butly に足りないものは「記憶の発想」そのものではなく、**記憶を外部から扱える基盤として見せるための境界・API・スキーマ・評価**である。

Butly は汎用OSSを目指す必要はないが、将来的に「Butly Memory」を他アプリやエージェント層からも使えるようにするため、記憶基盤としての外部境界を整理したい。

---

## 目的

この Issue では、Butly の記憶システムを以下の方向に整理する。

1. Butly アプリ内部専用の記憶処理から、外部アプリ/AgentService から呼び出せる Memory API へ整理する
2. 記憶オブジェクトの共通スキーマを定義する
3. 記憶の監査・編集・削除・昇格を扱える Memory Inspector の方向性を明確にする
4. Butly らしい companion memory evaluation を用意する
5. 将来の AgentService / ToolRegistry / MCP Adapter と接続しやすい設計にする

---

## 非目的

この Issue では以下は扱わない。

- 大規模マルチテナントSaaS化
- 認証/課金/組織管理
- LangChain 的な大量コネクタ統合
- 常時自律実行型エージェント
- 本格的な MCP 実装
- 分散DB / クラウドスケール対応

Butly の主戦場はあくまで **個人AIコンパニオン向けの記憶管理** とする。

---

## 現状の課題

### 1. Memory API の外部境界が弱い

現在の記憶処理は Butly アプリ内部の会話フローに強く結びついている。

将来的には以下のような API 境界を用意したい。

```python
add_memory(...)
search_memory(...)
update_memory(...)
delete_memory(...)
consolidate_memory(...)
promote_to_key_memory(...)
export_memory(...)
import_memory(...)
```

これにより、通常チャットだけでなく、AgentService や外部アプリからも Butly Memory を利用できるようにする。

---

### 2. 記憶スキーマが層ごとに分散している

Butly には Key Memory / Glossary / Knowledge Card / Digest / Relationship Snapshot などの記憶層がある。

ただし、外部基盤として扱うには共通メタデータが必要になる。

候補となる共通フィールド:

```yaml
id: string
type: key_memory | glossary | knowledge_card | digest | relationship_snapshot | raw_episode
content: string
summary: string | null
source: conversation | user_edit | sleeptime | import | tool_result
source_ref: string | null
instance_name: string
created_at: datetime
updated_at: datetime
confidence: float | null
importance: float | null
status: active | archived | deleted | pending
visibility: private | shared | system
provenance:
  conversation_id: string | null
  turn_ids: list[string]
  tool_run_id: string | null
embedding:
  provider: string | null
  model: string | null
  dimension: int | null
version: int
```

最初から全て実装する必要はないが、将来的に記憶の移行・監査・編集・評価を行うための基本形を定義したい。

---

### 3. Memory Inspector を Butly の主要機能として明確化したい

コンパニオンAIでは「何を覚えているか」が重要になる。

Memory Inspector では以下を扱えるようにしたい。

- AI が何を覚えているか表示する
- いつ・どの会話から覚えたか表示する
- なぜその記憶が使われたか表示する
- 記憶を編集する
- 記憶を削除する
- Key Memory に昇格する
- 記憶を archive / active 切り替えする
- 「これは覚えないで」を反映する
- conflict / duplicate を確認する

既存の DB Browser / Card Edit 画面を発展させ、Butly の中核機能として扱う。

---

### 4. Butly らしい評価セットが必要

既存の単体テスト・統合テストに加えて、記憶品質を測る evaluation が必要。

特に Butly では汎用QAよりも、以下のような companion memory eval が重要。

#### 評価ケース例

- 過去に話した予定を正しく思い出せるか
- ユーザーの好みを正しく反映できるか
- 古い好みと新しい好みが矛盾した時に新しい情報を優先できるか
- Key Memory に昇格すべき情報を見分けられるか
- 一時的な話題を永続記憶にしすぎないか
- Glossary の固有名詞を必要な時だけ注入できるか
- relationship snapshot が過剰に人格化しないか
- 不要な記憶注入を避けられるか
- RAG が不要な場面で検索しないか
- reflex でも必要な Glossary / RAG を拾えるか

#### 評価カテゴリ案

```text
memory_recall
preference_tracking
temporal_update
conflict_resolution
key_memory_promotion
glossary_precision
relationship_safety
rag_gating
context_minimization
```

---

### 5. AgentService / ToolRegistry と接続しやすくする

将来的に Agent 機能を追加する場合、通常会話の ChatService に ReAct / tool execution を混ぜるべきではない。

以下のように分離したい。

```text
ChatService
  - 通常会話
  - 記憶注入
  - 応答生成

AgentService
  - 明示的に起動されるタスク実行
  - Planner
  - ToolRegistry
  - ToolExecutor
  - ApprovalPolicy
  - AgentRunState

Butly Memory
  - ChatService / AgentService の両方から利用される記憶基盤
```

MCP は Butly に直接混ぜ込むのではなく、ToolRegistry の実装元の1つとして扱う。

```text
ToolRegistry
  ├─ LocalTool
  ├─ WebSearchTool
  ├─ GitHubTool
  └─ MCPToolAdapter
```

---

## 提案する実装ステップ

### Phase 1: Memory API の薄いFacadeを追加

まずは既存実装を大きく壊さず、薄い facade を作る。

候補ファイル:

```text
butly_core/memory_api/
  __init__.py
  service.py
  types.py
```

候補クラス:

```python
class ButlyMemoryService:
    def add_memory(...): ...
    def search_memory(...): ...
    def update_memory(...): ...
    def delete_memory(...): ...
    def promote_to_key_memory(...): ...
    def export_memory(...): ...
    def import_memory(...): ...
```

この段階では内部的に既存の `ButlyMemory`, `ButlyDatabase`, `ButlyBrain` を呼ぶだけでよい。

---

### Phase 2: Memory Object Schema を定義

`types.py` に Pydantic model を定義する。

候補:

```python
class MemoryObject(BaseModel):
    id: str
    type: MemoryType
    content: str
    summary: str | None = None
    source: MemorySource
    source_ref: str | None = None
    instance_name: str
    created_at: datetime
    updated_at: datetime
    confidence: float | None = None
    importance: float | None = None
    status: MemoryStatus = "active"
    provenance: MemoryProvenance | None = None
    version: int = 1
```

既存の knowledge_cards / glossary / key_memory をこの形式に段階的にマッピングする。

---

### Phase 3: Memory Inspector の整理

既存の DB Browser / Card Edit を拡張し、以下を扱う。

- MemoryObject 一覧
- type / status / source / instance でフィルタ
- source conversation への参照
- edit / delete / archive
- promote to Key Memory
- duplicate / conflict の候補表示

---

### Phase 4: Companion Memory Eval を追加

候補ディレクトリ:

```text
evals/memory/
  cases.yaml
  runner.py
  metrics.py
```

最初は LLM judge ではなく、期待される memory hit / no hit / injected section を確認する軽量テストから始める。

例:

```yaml
- id: preference_update_001
  category: temporal_update
  setup:
    memories:
      - "ユーザーは以前コーヒーが好きと言っていた"
      - "最近、ユーザーはカフェインを控えていると言った"
  input: "飲み物なにがよさそう？"
  expected:
    should_retrieve:
      - "カフェインを控えている"
    should_not_prioritize:
      - "コーヒーが好き"
```

---

### Phase 5: AgentService / ToolRegistry から利用可能にする

Memory API ができた後、AgentService からも同じ Memory API を呼ぶ。

例:

- tool result を記憶候補として保存
- Agent run の決定事項を digest に送る
- ユーザー承認済みの内容のみ Key Memory に昇格
- MCP 経由の情報を provenance 付きで保存

---

## 受け入れ条件

### 最小完了条件

- [ ] Butly Memory の外部境界に関する方針が docs または issue として整理されている
- [ ] `ButlyMemoryService` の最小 facade が追加されている
- [ ] `MemoryObject` の初期スキーマが定義されている
- [ ] 既存 knowledge card を `MemoryObject` として読める
- [ ] 最低限の memory eval ケースが追加されている

### 発展条件

- [ ] Glossary / Key Memory も `MemoryObject` として扱える
- [ ] Memory Inspector で type / source / status を表示できる
- [ ] promote / archive / delete の操作ができる
- [ ] AgentService から Memory API を呼べる
- [ ] Tool / MCP 由来の記憶に provenance を付与できる

---

## メモ

Butly は汎用Memory OSを目指す必要はない。

むしろ、以下のように分けて考える。

```text
Butly App
  = コンパニオンAIとしての体験

Butly Memory
  = コンパニオンAI向けの多層記憶基盤

Butly Agent
  = 明示実行型の外部行動・Tool実行層
```

この Issue の中心は `Butly Memory` の外部境界を作ること。

汎用OSSとの差分を埋めるというより、Butly の強みである「コンパニオンAIの記憶管理」を外からも扱える形に整える。