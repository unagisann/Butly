# Butly — アーキテクチャ図集

🌐 **日本語** | [English](DIAGRAMS.md)

このファイルには Butly のシステム設計を可視化した Mermaid 図をまとめています。

---

## 1. 全体アーキテクチャ（メインフロー）

ユーザー発言から返答生成・記憶保存までの処理フローです。

```mermaid
flowchart TD
    A((ユーザー発言)) --> B["⧫ Gatekeeper<br/>ContextClassifier + MemoryProbe"]
    B --> C["構造化出力<br/>tier / need / need_intent / probe candidates"]
    C --> E{tier}
    E -->|reflex| F["⚡ reflex<br/>最小コンテキスト"]
    E -->|mid| G["◎ mid<br/>注入記憶あり"]
    C --> N{need?<br/>tier 非依存}
    N -->|有| H["⌕ RAG ブロック<br/>MemoryProbe candidates から"]
    H --> I[("⛁ ナレッジDB<br/>(SQLite + Embeddings)")]
    F --> J["◆ ChatService<br/>Provider.generate() / async_generate_stream()"]
    G --> J
    H --> J
    J -->|非Gemini + 検索ON| WS["🔍 SearchModule<br/>Tavily / Ollama Cloud"]
    WS -->|検索結果を context に注入| J
    J -.->|asyncio.gather で並列| SU["⟳ StateUpdater<br/>(post-response)"]
    SU -.->|state_delta| D["◈ Session State<br/>topic / mood / turn_count"]
    D -.->|次ターンで参照| B
    J --> K((返答 / SSE chunks))
    K --> L["▣ short_term_json 保存"]
    L -.->|定期処理| M["⚙ Sleeptime<br/>日次 + 週次バッチ"]
    M -.->|ナレッジ生成| I
    M -.->|recent_digest_headlines| B
```

---

## 2. system_instruction 注入順序

LLM に渡すコンテキストブロックの構築順序（上部が不変、下部が可変）です。

```mermaid
block-beta
    columns 1
    A["1. SYSTEM INSTRUCTION — 性格設定（不変）"]
    B["2. KEY MEMORY — 根幹記憶（不変）"]
    C["3. CURRENT TIME — 現在時刻"]
    D["4. GLOSSARY — 共通言語辞書・意味記憶"]
    E["5. MID-TERM — 中期記憶 digest + relationship（低頻度更新）"]
    F["6. RAG — 長期記憶検索結果（※参考情報注釈付き）"]
    G["7. FLOATING — 直近の会話要約（※直近文脈注釈付き）"]
    H["8. TIER INFO — 思考モード"]
    I["9. WEB SEARCH RESULTS — Web検索結果（非Gemini + 検索ON時のみ）"]

    style A fill:#1a1a2e,color:#ec4899
    style B fill:#1a1a2e,color:#f59e0b
    style C fill:#1a1a2e,color:#8899aa
    style D fill:#1a1a2e,color:#a78bfa
    style E fill:#1a1a2e,color:#10b981
    style F fill:#1a1a2e,color:#ef4444
    style G fill:#1a1a2e,color:#3b82f6
    style H fill:#1a1a2e,color:#556677
    style I fill:#1a1a2e,color:#f97316
```

---

## 3. Mid-term 二層要約構造

short_term_json（RAW）からの記憶パイプラインです。

```mermaid
flowchart LR
    RAW["short_term_json<br/>(RAW)"] --> INT["1_integrated<br/>(RAW保管)"]
    INT --> S1a["Stage 1a<br/>mid_term.txt<br/>RAW蓄積"]
    INT --> S1b["Stage 1b ★<br/>mid_term_digest.txt<br/>エピソード付き差分追記"]
    S1b --> S1c["Stage 1c ★<br/>mid_term_relationship.txt<br/>週次上書き"]
    INT --> S2["Stage 2<br/>knowledge_cards<br/>episode生成"]
    INT --> KN["2_knowledgeized<br/>(RAW永久保管)"]

    style S1b fill:#065f46,color:#10b981
    style S1c fill:#4c1d95,color:#8b5cf6
    style S1a fill:#1f2937,color:#8899aa
    style S2 fill:#065f46,color:#10b981
```

---

## 4. Sleeptime ステージ構成

日次・週次バッチ処理の構成です。

```mermaid
flowchart TD
    subgraph "日次バッチ"
        S1a["Stage 1a<br/>Mid-term RAW蓄積"]
        S1b["Stage 1b ★<br/>エピソード付きDigest生成<br/><i>当日RAW → digest追記</i><br/><i>★ 日付ヘッダ区切りチャンク分割対応</i>"]
        SKIP{★ skip_knowledge?}
        S2["Stage 2<br/>ナレッジカード生成<br/><i>RAW → episode cards</i><br/><i>★ ファイル単位チャンク分割対応</i>"]
        S2SKIP["スキップ<br/>RAWを 1_integrated に保持"]
    end
    subgraph "週次バッチ"
        S1c["Stage 1c ★<br/>関係性Snapshot更新<br/><i>digest → relationship上書き</i>"]
        S3["Stage 3 ※未実装<br/>統合記憶生成<br/><i>episodes → reflection等</i>"]
    end

    S1a --> S1b --> S1c
    S1a --> SKIP
    SKIP -->|false| S2
    SKIP -->|true| S2SKIP
    S2 -.-> S3

    style S1b fill:#065f46,color:#10b981
    style S1c fill:#4c1d95,color:#8b5cf6
    style S3 fill:#1f2937,color:#556677,stroke-dasharray: 5 5
    style SKIP fill:#92400e,color:#fbbf24
    style S2SKIP fill:#1f2937,color:#8899aa,stroke-dasharray: 5 5
```

---

## 5. マルチプロバイダー構成

LLM プロバイダー抽象化レイヤーの構成です。

```mermaid
flowchart TD
    CS["ChatService<br/>(オーケストレーション<br/>+ ストリーミング)"]
    GK["Gatekeeper<br/>(tier + need_intent)"]
    HK["Sleeptime<br/>(記憶整理)"]
    PF["ProviderFactory<br/>(model_name → プロバイダー自動ルーティング)"]
    GE["GeminiProvider<br/>gemini-*"]
    OA["OpenAIProvider<br/>gpt-* / o1 / o3 / o4"]
    XA["XaiProvider<br/>grok-* / xai/*"]
    OL["OllamaProvider<br/>ollama/*"]
    CMP["_openai_compat.py<br/>(共通ヘルパー)"]

    CS --> PF
    GK --> PF
    HK --> PF
    PF --> GE
    PF --> OA
    PF --> XA
    PF --> OL
    OA -.->|使用| CMP
    XA -.->|使用| CMP
    OL -.->|使用| CMP
```

---

## 5b. SSE ストリーミングフロー (`POST /chat/stream`)

`ChatService.execute_stream()` と Provider の `async_generate_stream()` の協調動作。

```mermaid
sequenceDiagram
    participant UI as Streamlit UI
    participant API as FastAPI /chat/stream
    participant CS as ChatService.execute_stream
    participant GK as Gatekeeper
    participant SU as StateUpdater (並列)
    participant P as Provider.async_generate_stream

    UI->>API: POST /chat/stream (use_streaming=true)
    API->>CS: execute_stream() 呼び出し
    CS->>GK: classify(user_input, history, ...)
    GK-->>CS: tier / need / probe
    CS-->>UI: event: metadata (tier, need, scores)
    CS->>SU: asyncio.create_task(update_state)
    CS->>P: async for chunk in stream
    loop done まで繰り返し
        P-->>CS: {"type": "chunk", "text": ...}
        CS-->>UI: event: chunk
    end
    P-->>CS: {"type": "done", debug, sources}
    CS->>SU: await state_task
    CS->>CS: save_single_turn + maintain_memory + debug log
    CS-->>UI: event: done (debug_info, session_state, sources)
```

---

## 6. インスタンス別ディレクトリ構成

各 AI インスタンスの記憶ファイル配置です。

```mermaid
flowchart TD
    ROOT["butly_core/instances/"]
    I1["instance_name/"]
    CFG["config.json"]
    SI["system_instruction.txt"]
    KM["Key_Memory.txt"]
    MT["mid_term.txt"]
    MD["mid_term_digest.txt"]
    MR["mid_term_relationship.txt"]
    SS["session_state.json"]
    GL["glossary.yaml"]
    DB["butly_memory.db"]
    ST["short_term_json/"]
    FS["floating_summaries/"]
    DL["debug_logs/"]
    DLH["debug_logs/history/"]
    RH["recent_digest_headlines.json"]
    AR["memory_archive/"]
    A1["1_integrated/"]
    A2["2_knowledgeized/"]
    A3["3_log/"]

    ROOT --> I1
    I1 --> CFG
    I1 --> SI
    I1 --> KM
    I1 --> MT
    I1 --> MD
    I1 --> MR
    I1 --> SS
    I1 --> GL
    I1 --> DB
    I1 --> ST
    I1 --> FS
    I1 --> DL
    DL --> DLH
    I1 --> RH
    I1 --> AR
    AR --> A1
    AR --> A2
    AR --> A3
```
