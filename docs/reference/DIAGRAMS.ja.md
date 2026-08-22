# Butly — アーキテクチャ図集

🌐 **日本語** | [English](DIAGRAMS.md)

> 最終更新: 2026-08-22

このファイルには Butly のシステム設計を可視化した Mermaid 図をまとめています。
図はあくまで俯瞰用です。数値・キー名の正は
[ファイル構成](FILE_STRUCTURE.ja.md) /
[記憶ライフサイクル](memory_lifecycle.ja.md) /
[設定レイヤー](configuration.ja.md) と、最終的には現行コードです。

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
    C --> N{"注入する?<br/>injection_policy<br/>(tier 非依存)"}
    N -->|する| H["⌕ RAG ブロック<br/>MemoryProbe candidates から"]
    H --> I[("⛁ ナレッジDB<br/>(SQLite + Embeddings)")]
    H -.->|Stage 3 有効時| MN[("◈ memory_nodes<br/>active node 併走注入")]
    F --> MB["▤ MemoryBlockBuilder<br/>context_levels でブロック整形"]
    G --> MB
    H --> MB
    MN --> MB
    MB --> J["◆ ChatService<br/>CanonicalRequest 構築"]
    J --> CAP["⚙ Capability Resolver<br/>token 上限パラメータ / reasoning / temperature"]
    CAP --> PV["▶ Provider<br/>generate() / async_generate_stream()"]
    PV -->|非Gemini + 検索ON| WS["🔍 SearchModule<br/>Tavily / Ollama Cloud"]
    WS -->|検索結果を context に注入| PV
    PV -.->|asyncio で並列| SU["⟳ StateUpdater<br/>(post-response)"]
    SU -.->|state_delta| D["◈ Session State<br/>topic / mood / turn_count"]
    D -.->|次ターンで参照| B
    PV --> K((返答 / SSE chunks))
    K --> L["▣ short_term_json 保存"]
    B -.->|全工程を記録| TR["◇ TraceCollector<br/>trace.json"]
    PV -.-> TR
    L -.->|定期処理| M["⚙ Sleeptime<br/>Stage 1 / 2 / 3"]
    M -.->|Stage 2 カード生成| I
    M -.->|Stage 3 ノード蒸留| MN
    M -.->|recent_digest_headlines| B
```

---

## 2. コンテキスト注入順序

LLM に渡すコンテキストの構築順序です。
`system_instruction`（不変）と `context_prefix`（可変）の 2 系統に分かれ、
順序の既定は `memory_builder.DEFAULT_CONTEXT_ORDER` が持ちます。

```mermaid
block-beta
    columns 1
    T1["── system_instruction（不変） ──"]
    A["1. SYSTEM INSTRUCTION — 性格設定"]
    B["2. KEY MEMORY — 根幹記憶"]
    T2["── context_prefix（可変・履歴先頭に user として注入） ──"]
    C["3. LABEL NOTES — 文脈ラベル・記憶利用規則"]
    D["4. CURRENT TIME — 現在時刻（Chronos）"]
    E["5. GLOSSARY — 共通言語辞書・意味記憶"]
    F["6. MID-TERM — digest + recent_snapshot、または RAW キャッシュ（mid 以上）"]
    G["7. RAG — 長期記憶検索結果 + active nodes（need 有時・tier 非依存）"]
    H["8. SESSION DIGEST — 会話圧縮ログ"]
    I["9. TIER INFO — 思考モード"]
    J["10. GOOGLE SEARCH — グラウンディング注意書き（Gemini）"]
    K["11. WEB SEARCH RESULTS — Tavily / Ollama Cloud（非Gemini + 検索ON）"]

    style T1 fill:#0f172a,color:#94a3b8
    style T2 fill:#0f172a,color:#94a3b8
    style A fill:#1a1a2e,color:#ec4899
    style B fill:#1a1a2e,color:#f59e0b
    style C fill:#1a1a2e,color:#64748b
    style D fill:#1a1a2e,color:#8899aa
    style E fill:#1a1a2e,color:#a78bfa
    style F fill:#1a1a2e,color:#10b981
    style G fill:#1a1a2e,color:#ef4444
    style H fill:#1a1a2e,color:#3b82f6
    style I fill:#1a1a2e,color:#556677
    style J fill:#1a1a2e,color:#22d3ee
    style K fill:#1a1a2e,color:#f97316
```

各ブロックは `context_levels`（`high` / `mid` / `low` / `off`）で詳細度を切り替えられます。
プリセットは `normal` / `compact` / `low` の 3 種。詳細は
[context_levels 仕様](context_levels.ja.md)。

---

## 3. 記憶パイプライン

short_term_json（RAW）から各層への流れです。
`mid_term.txt` への追記方式は廃止され、RAW は `2_knowledgeized/` から
`raw_memory_cache.txt` へ**再構築**されます。

```mermaid
flowchart LR
    ST["short_term_json<br/>(RAW ターンログ)"] --> SD["session_digests<br/>(溢れ時の圧縮ログ)"]
    ST --> INT["memory_archive/<br/>1_integrated<br/>(処理待ち RAW)"]
    SD -.->|Sleeptime で削除| X(("×"))
    INT --> S1b["Stage 1<br/>mid_term_digest.txt<br/>差分追記"]
    S1b --> S1c["Stage 1<br/>recent_snapshot.txt<br/>7日ごと上書き"]
    S1b --> S1d["Stage 1<br/>recent_digest_headlines.json<br/>最大4件"]
    INT --> S2["Stage 2<br/>knowledge_cards<br/>+ embedding"]
    S2 --> KN["memory_archive/<br/>2_knowledgeized<br/>(RAW 永久保管)"]
    KN --> RC["Stage 1<br/>raw_memory_cache.txt<br/>全上書き再生成"]
    S2 --> S3["Stage 3 ★opt-in<br/>memory_nodes<br/>統合知識"]

    style S1b fill:#065f46,color:#10b981
    style S1c fill:#4c1d95,color:#8b5cf6
    style S1d fill:#4c1d95,color:#8b5cf6
    style S2 fill:#065f46,color:#10b981
    style S3 fill:#7c2d12,color:#fb923c
    style RC fill:#1f2937,color:#8899aa
    style X fill:#1f2937,color:#666
```

> Stage 1 は Stage 2 より先に走るため、`raw_memory_cache.txt` に載るのは
> **前回までにナレッジ化済み**の会話です。

---

## 4. Sleeptime ステージ構成

```mermaid
flowchart TD
    RUN["ButlySleeptime.run()"] --> PI["process_instance(instance_path)"]
    PI --> S1["Stage 1: stage_1_cleanup"]
    subgraph "Stage 1（日次）"
        S1a["Step 0: short_term_json → 1_integrated"]
        S1b["Step 2: session_digests クリア"]
        S1c["Step 3: raw_memory_cache.txt 再生成"]
        S1d["Step 4: 日次 digest 生成<br/><i>日付ヘッダ区切りチャンク分割対応</i>"]
        S1e["Step 5: recent_digest_headlines"]
        S1f["Step 6: recent_snapshot（7日間隔）"]
        S1g["Step 7: Key Memory 提案<br/><i>既定 OFF</i>"]
        S1a --> S1b --> S1c --> S1d --> S1e --> S1f --> S1g
    end
    S1 --> S1a
    S1g --> GATE2{knowledge_cards<br/>有効?}
    GATE2 -->|false| SKIP2["スキップ<br/>RAW を 1_integrated に保持"]
    GATE2 -->|true| S2["Stage 2: stage_2_knowledgeize<br/><i>ファイル単位チャンク分割</i>"]
    S2 --> GATE3{knowledge_maturation<br/>有効?<br/><i>既定 OFF</i>}
    SKIP2 --> GATE3
    GATE3 -->|false| SKIP3["スキップ"]
    GATE3 -->|true| S3["Stage 3: stage_3_mature_knowledge<br/>process lock → 回収 → preflight<br/>→ FIFO batch → LLM → 単一 transaction"]

    style S1c fill:#1f2937,color:#8899aa
    style S1d fill:#065f46,color:#10b981
    style S1f fill:#4c1d95,color:#8b5cf6
    style S1g fill:#1f2937,color:#556677,stroke-dasharray: 5 5
    style S2 fill:#065f46,color:#10b981
    style S3 fill:#7c2d12,color:#fb923c
    style SKIP2 fill:#1f2937,color:#8899aa,stroke-dasharray: 5 5
    style SKIP3 fill:#1f2937,color:#8899aa,stroke-dasharray: 5 5
    style GATE2 fill:#92400e,color:#fbbf24
    style GATE3 fill:#92400e,color:#fbbf24
```

各ゲートはインスタンス `config.json` の `sleeptime.update_targets` で制御します。

---

## 5. Stage 3: Knowledge Maturation（opt-in）

content hash 式レビューキューと、単一 transaction での適用。

```mermaid
flowchart TD
    L["instance 単位 process lock<br/>(non-blocking flock)"] --> AB["前 process の running run を<br/>abandoned 回収"]
    AB --> PF["preflight<br/>非アーカイブ NULL hash の backfill"]
    PF -->|失敗| RF(["run failed"])
    PF --> Q["レビューキュー選択<br/>last_matured_content_hash が<br/>NULL または content_hash と不一致<br/>→ maturation_queued_at 昇順 FIFO"]
    Q --> LLM["LLM: stage3_node_review"]
    LLM --> CLS{結果分類}
    CLS -->|ok / no_changes| TX["単一 SQLite transaction<br/>node/source 更新<br/>+ run counters<br/>+ カード版 stamp<br/>+ run 完了"]
    CLS -->|truncated / empty / parse_error<br/>/ provider_error| RETRY["有限 retry → batch 半分割<br/>→ 1 件隔離<br/><i>stamp せずキューに残す</i>"]
    TX --> HASH{適用時に<br/>content_hash 再検証}
    HASH -->|不一致| CD["changed_during_run<br/>batch 全体を適用しない"]
    HASH -->|一致| OK(["completed"])
    OK --> DEC["reflection（既定 OFF）<br/>staleness 減衰スイープ"]
    DEC --> PROP["memory_node_proposals.json<br/>Key Memory 昇格候補"]

    style TX fill:#065f46,color:#10b981
    style RETRY fill:#92400e,color:#fbbf24
    style CD fill:#7f1d1d,color:#fca5a5
    style RF fill:#7f1d1d,color:#fca5a5
    style DEC fill:#1f2937,color:#556677,stroke-dasharray: 5 5
```

---

## 6. LLM レイヤー構成（Canonical + Capability）

Core / 評価コードは provider 固有のパラメータ名を選びません。

```mermaid
flowchart TD
    CS["ChatService / Brain /<br/>Gatekeeper / Sleeptime /<br/>Semantic Judge"]
    CR["CanonicalRequest<br/>llm/canonical.py<br/><i>provider 非依存</i>"]
    CAP["Capability Resolver<br/>llm/capabilities.py"]
    META["provider metadata"]
    OBS["観測キャッシュ<br/>llm_capabilities.json"]
    OVR["LLM_CAPABILITY_OVERRIDES<br/>user_config.json"]
    REG["ConnectionRegistry<br/>llm/connections.py<br/><i>built-in 4 + user 定義</i>"]
    PF["ProviderFactory<br/>llm/factory.py<br/><i>ModelRef → Adapter</i>"]
    OC["OpenAICompatAdapter<br/>protocols/openai_compat.py"]
    GN["GeminiNativeAdapter<br/>protocols/gemini_native.py"]
    P1["OpenAI / xAI / Ollama /<br/>Groq / NanoGPT / …"]
    P2["Gemini"]

    CS --> CR
    CR --> CAP
    META --> CAP
    OBS --> CAP
    OVR --> CAP
    CAP --> PF
    REG --> PF
    PF --> OC
    PF --> GN
    OC --> P1
    GN --> P2

    style CR fill:#1e3a5f,color:#93c5fd
    style CAP fill:#4c1d95,color:#c4b5fd
    style OVR fill:#92400e,color:#fbbf24
```

Capability の解決順は **provider metadata → 観測キャッシュ → manual override**。
モデル名の prefix では判定しません。詳細は
[LLM Connection / APIキー管理](llm_connections.ja.md)。

---

## 7. 検索モード（`brain.search_mode`）

```mermaid
flowchart TD
    Q["ユーザー発言<br/>(+ Gatekeeper の自己完結検索文)"] --> MODE{search_mode}
    MODE -->|vector<br/>既定| V["ベクトル cosine<br/>+ 時間減衰"]
    MODE -->|hybrid| HB["BM25 (FTS5/trigram)<br/>+ ベクトル → RRF 融合"]
    MODE -->|dual_query| DQ["元発話 top15<br/>+ 検索文 top15<br/>→ 等重み RRF (最大25)"]
    MODE -->|hybrid_evidence_fusion| HEF["hybrid top-N を<br/>Episode / RAW MaxP で再評価<br/>→ 重み付き融合 (既定 0.70/0.30)"]
    V --> RR{reranker<br/>有効?}
    HB --> RR
    DQ --> RR
    HEF --> RR
    RR -->|off| TOP["上位 search_limit 件を注入<br/>(既定 3)"]
    RR -->|cross_encoder / llm| RRK["候補プールを再順位付け<br/><i>fail-open: 失敗時は元順位</i>"]
    RRK --> TOP
    TOP --> SRC{rag_source_mode}
    SRC -->|cards 既定| C1["カード summary / episode"]
    SRC -->|raw| C2["当時の会話原文<br/>(source_files から遅延解決)"]
    SRC -->|both| C3["カード + 原文<br/>(rag_raw_top_k 件のみ展開)"]

    style V fill:#065f46,color:#10b981
    style HB fill:#1f2937,color:#8899aa
    style DQ fill:#1f2937,color:#8899aa
    style HEF fill:#1f2937,color:#8899aa
    style RRK fill:#4c1d95,color:#c4b5fd
```

`vector` 以外と reranker は**評価で効果を確認してから昇格**させる方針です。
比較手順は [LoCoMo Evaluation Web Console](evaluation_web_console.ja.md)。

---

## 8. SSE ストリーミングフロー（`POST /api/v1/chat/stream`）

```mermaid
sequenceDiagram
    participant UI as Desktop UI (React)
    participant API as butly_api /api/v1/chat/stream
    participant CS as ChatService.execute_stream
    participant GK as Gatekeeper
    participant SU as StateUpdater (並列)
    participant P as Provider.async_generate_stream

    UI->>API: POST /api/v1/chat/stream (request_id 付き)
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
    UI--)API: POST /api/v1/chat/requests/{id}/cancel（任意）
    P-->>CS: {"type": "done", debug, sources}
    CS->>SU: await state_task
    CS->>CS: save_single_turn + maintain_memory + trace 保存
    CS-->>UI: event: done (debug_info, session_state, sources)
    Note over UI,API: 失敗時は event: error。<br/>出力前の失敗は同じ request_id で冪等に再送できる
```

legacy `POST /chat/stream`（Streamlit 互換）も同じ `ChatService.execute_stream()` を通ります。

---

## 9. Trace Graph

1 応答の内部フローをノード + エッジで保存します（`trace.json`、schema version 1）。

```mermaid
flowchart LR
    subgraph "ノード種別"
        N1["input"] --> N2["loader"] --> N3["decision"]
        N3 --> N4["retrieval"] --> N5["tool"]
        N5 --> N6["context"] --> N7["provider"] --> N8["llm"]
        N8 --> N9["formatter"] --> N10["memory"] --> N11["housekeeper"] --> N12["end"]
    end
```

| status | 意味 | Mermaid での表現 |
|---|---|---|
| `active` | 実際に通った処理 | 緑塗り・実線 |
| `skipped` | 候補だったが使われなかった | 灰色・破線 |
| `fallback` | フォールバックとして使われた | 橙塗り・破線 |
| `error` | 失敗した処理 | 赤塗り |
| `warning` | 成功したが注意が必要 | 黄塗り |

保存は常に full。表示側のフィルタは `SYSTEM_CONFIG["trace"]` の
`detail`（`full` / `summary`）と `hidden_nodes` で制御します。
`butly_core/trace/mermaid.py` が frontend 非依存の Mermaid 文字列を生成し、
デスクトップ UI と評価画面の両方が同じ出力を描画します。

---

## 10. 設定の解決順

```mermaid
flowchart TD
    D["settings/defaults.py<br/>AI_CONFIG / SYSTEM_CONFIG"] --> U["user_config.json<br/><i>recursive_update</i>"]
    U --> NORM["normalize_ai_config()<br/>connection 推定・整合検査"]
    NORM --> RS["RootSettings<br/><i>get_settings() / lru_cache</i>"]
    RS --> BOOT["apply_runtime_settings(data_dir)"]
    BOOT --> LEG["butly_core.config<br/>AI_CONFIG / SYSTEM_CONFIG<br/><i>互換シム・in-place</i>"]
    BOOT --> CREG["ConnectionRegistry"]
    BOOT --> CRT["Capability runtime"]
    LEG --> INST["インスタンス config.json<br/><i>override_config として深いマージ</i>"]
    INST --> REQ["リクエスト単位 override<br/><i>model_name 等</i>"]
    ENV["BUTLY_* 環境変数"] -.->|init kwargs が勝つため<br/>現状 no-op| RS

    style ENV fill:#7f1d1d,color:#fca5a5,stroke-dasharray: 5 5
    style LEG fill:#1f2937,color:#8899aa
```

詳細は [設定レイヤー](configuration.ja.md)。

---

## 11. インスタンス別ディレクトリ構成

```mermaid
flowchart TD
    ROOT["butly_core/instances/"]
    I1["instance_name/"]
    CFG["config.json"]
    SI["system_instruction.txt"]
    KM["Key_Memory.txt / .yaml"]
    RC["raw_memory_cache.txt"]
    MD["mid_term_digest.txt"]
    RS["recent_snapshot.txt"]
    SS["session_state.json"]
    GL["glossary.yaml"]
    DB["butly_memory.db<br/>knowledge_cards<br/>memory_nodes ほか"]
    ST["short_term_json/"]
    FS["session_digests/"]
    DL["debug_logs/"]
    DLH["debug_logs/history/"]
    TRJ["trace.json"]
    RH["recent_digest_headlines.json"]
    NP["memory_node_proposals.json"]
    AR["memory_archive/"]
    A1["1_integrated/"]
    A2["2_knowledgeized/{date}/"]
    A3["3_log/"]

    ROOT --> I1
    I1 --> CFG
    I1 --> SI
    I1 --> KM
    I1 --> RC
    I1 --> MD
    I1 --> RS
    I1 --> SS
    I1 --> GL
    I1 --> DB
    I1 --> ST
    I1 --> FS
    I1 --> DL
    DL --> DLH
    I1 --> TRJ
    I1 --> RH
    I1 --> NP
    I1 --> AR
    AR --> A1
    AR --> A2
    AR --> A3
```

`memory_nodes` / `memory_node_sources` / `memory_maturation_runs` /
`memory_maturation_run_cards` は `knowledge_cards` と**同じ `butly_memory.db`** の中にあります。
