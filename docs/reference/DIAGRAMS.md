# Butly — Architecture Diagrams

[日本語](DIAGRAMS.ja.md) | 🌐 **English**

> Last updated: 2026-08-22

This file collects the Mermaid diagrams that visualize Butly's system design.
They are overviews only. For exact values and key names, defer to
[File Structure](FILE_STRUCTURE.md),
[Memory Lifecycle](memory_lifecycle.md), and
[Configuration Layer](configuration.md) — and ultimately to the current code.

---

## 1. Overall architecture (main flow)

From a user message through response generation to memory persistence.

```mermaid
flowchart TD
    A((User message)) --> B["⧫ Gatekeeper<br/>ContextClassifier + MemoryProbe"]
    B --> C["Structured output<br/>tier / need / need_intent / probe candidates"]
    C --> E{tier}
    E -->|reflex| F["⚡ reflex<br/>minimal context"]
    E -->|mid| G["◎ mid<br/>memory injected"]
    C --> N{"Inject?<br/>injection_policy<br/>(tier-independent)"}
    N -->|yes| H["⌕ RAG block<br/>from MemoryProbe candidates"]
    H --> I[("⛁ Knowledge DB<br/>(SQLite + embeddings)")]
    H -.->|when Stage 3 enabled| MN[("◈ memory_nodes<br/>active nodes injected alongside")]
    F --> MB["▤ MemoryBlockBuilder<br/>shapes blocks per context_levels"]
    G --> MB
    H --> MB
    MN --> MB
    MB --> J["◆ ChatService<br/>builds CanonicalRequest"]
    J --> CAP["⚙ Capability Resolver<br/>token-limit parameter / reasoning / temperature"]
    CAP --> PV["▶ Provider<br/>generate() / async_generate_stream()"]
    PV -->|non-Gemini + search on| WS["🔍 SearchModule<br/>Tavily / Ollama Cloud"]
    WS -->|results injected into context| PV
    PV -.->|in parallel via asyncio| SU["⟳ StateUpdater<br/>(post-response)"]
    SU -.->|state_delta| D["◈ Session State<br/>topic / mood / turn_count"]
    D -.->|read next turn| B
    PV --> K((Response / SSE chunks))
    K --> L["▣ Save to short_term_json"]
    B -.->|records every step| TR["◇ TraceCollector<br/>trace.json"]
    PV -.-> TR
    L -.->|batch| M["⚙ Sleeptime<br/>Stage 1 / 2 / 3"]
    M -.->|Stage 2 cards| I
    M -.->|Stage 3 nodes| MN
    M -.->|recent_digest_headlines| B
```

---

## 2. Context injection order

The order in which context is assembled for the LLM. It splits into
`system_instruction` (immutable) and `context_prefix` (variable); the default order
lives in `memory_builder.DEFAULT_CONTEXT_ORDER`.

```mermaid
block-beta
    columns 1
    T1["── system_instruction (immutable) ──"]
    A["1. SYSTEM INSTRUCTION — personality"]
    B["2. KEY MEMORY — core memory"]
    T2["── context_prefix (variable, injected as the first user message) ──"]
    C["3. LABEL NOTES — context labels and memory-use rules"]
    D["4. CURRENT TIME — current time (Chronos)"]
    E["5. GLOSSARY — shared vocabulary / semantic memory"]
    F["6. MID-TERM — digest + recent_snapshot, or the RAW cache (mid tier and up)"]
    G["7. RAG — long-term retrieval + active nodes (when need is set, tier-independent)"]
    H["8. SESSION DIGEST — compressed conversation log"]
    I["9. TIER INFO — thinking mode"]
    J["10. GOOGLE SEARCH — grounding notice (Gemini)"]
    K["11. WEB SEARCH RESULTS — Tavily / Ollama Cloud (non-Gemini + search on)"]

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

Each block's verbosity is switchable through `context_levels`
(`high` / `mid` / `low` / `off`), with three presets: `normal`, `compact`, `low`.
See [Context Levels](context_levels.md).

---

## 3. Memory pipeline

How raw turns flow into each layer. The append-to-`mid_term.txt` scheme is retired; RAW
is now **rebuilt** into `raw_memory_cache.txt` from `2_knowledgeized/`.

```mermaid
flowchart LR
    ST["short_term_json<br/>(raw turn logs)"] --> SD["session_digests<br/>(compressed on overflow)"]
    ST --> INT["memory_archive/<br/>1_integrated<br/>(RAW awaiting processing)"]
    SD -.->|deleted by Sleeptime| X(("×"))
    INT --> S1b["Stage 1<br/>mid_term_digest.txt<br/>incremental append"]
    S1b --> S1c["Stage 1<br/>recent_snapshot.txt<br/>overwritten every 7 days"]
    S1b --> S1d["Stage 1<br/>recent_digest_headlines.json<br/>max 4"]
    INT --> S2["Stage 2<br/>knowledge_cards<br/>+ embedding"]
    S2 --> KN["memory_archive/<br/>2_knowledgeized<br/>(RAW kept permanently)"]
    KN --> RC["Stage 1<br/>raw_memory_cache.txt<br/>full-overwrite rebuild"]
    S2 --> S3["Stage 3 ★opt-in<br/>memory_nodes<br/>consolidated knowledge"]

    style S1b fill:#065f46,color:#10b981
    style S1c fill:#4c1d95,color:#8b5cf6
    style S1d fill:#4c1d95,color:#8b5cf6
    style S2 fill:#065f46,color:#10b981
    style S3 fill:#7c2d12,color:#fb923c
    style RC fill:#1f2937,color:#8899aa
    style X fill:#1f2937,color:#666
```

> Stage 1 runs before Stage 2, so `raw_memory_cache.txt` only contains conversations
> **already turned into knowledge on an earlier run**.

---

## 4. Sleeptime stages

```mermaid
flowchart TD
    RUN["ButlySleeptime.run()"] --> PI["process_instance(instance_path)"]
    PI --> S1["Stage 1: stage_1_cleanup"]
    subgraph "Stage 1 (daily)"
        S1a["Step 0: short_term_json → 1_integrated"]
        S1b["Step 2: clear session_digests"]
        S1c["Step 3: rebuild raw_memory_cache.txt"]
        S1d["Step 4: daily digest<br/><i>chunked on date headers</i>"]
        S1e["Step 5: recent_digest_headlines"]
        S1f["Step 6: recent_snapshot (7-day interval)"]
        S1g["Step 7: Key Memory proposals<br/><i>off by default</i>"]
        S1a --> S1b --> S1c --> S1d --> S1e --> S1f --> S1g
    end
    S1 --> S1a
    S1g --> GATE2{knowledge_cards<br/>enabled?}
    GATE2 -->|false| SKIP2["skip<br/>RAW stays in 1_integrated"]
    GATE2 -->|true| S2["Stage 2: stage_2_knowledgeize<br/><i>chunked per file</i>"]
    S2 --> GATE3{knowledge_maturation<br/>enabled?<br/><i>off by default</i>}
    SKIP2 --> GATE3
    GATE3 -->|false| SKIP3["skip"]
    GATE3 -->|true| S3["Stage 3: stage_3_mature_knowledge<br/>process lock → reclaim → preflight<br/>→ FIFO batch → LLM → single transaction"]

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

Each gate is controlled by `sleeptime.update_targets` in the instance `config.json`.

---

## 5. Stage 3: Knowledge Maturation (opt-in)

A content-hash review queue applied inside a single transaction.

```mermaid
flowchart TD
    L["per-instance process lock<br/>(non-blocking flock)"] --> AB["reclaim a previous process's<br/>running run as abandoned"]
    AB --> PF["preflight<br/>backfill non-archived NULL hashes"]
    PF -->|fails| RF(["run failed"])
    PF --> Q["select review queue<br/>last_matured_content_hash is<br/>NULL or differs from content_hash<br/>→ FIFO by maturation_queued_at"]
    Q --> LLM["LLM: stage3_node_review"]
    LLM --> CLS{classify result}
    CLS -->|ok / no_changes| TX["single SQLite transaction<br/>node/source updates<br/>+ run counters<br/>+ card-version stamps<br/>+ run completion"]
    CLS -->|truncated / empty / parse_error<br/>/ provider_error| RETRY["bounded retry → halve batch<br/>→ isolate single card<br/><i>no stamp; stays queued</i>"]
    TX --> HASH{re-verify content_hash<br/>at apply time}
    HASH -->|mismatch| CD["changed_during_run<br/>whole batch not applied"]
    HASH -->|match| OK(["completed"])
    OK --> DEC["reflection (off by default)<br/>staleness decay sweep"]
    DEC --> PROP["memory_node_proposals.json<br/>Key Memory promotion candidates"]

    style TX fill:#065f46,color:#10b981
    style RETRY fill:#92400e,color:#fbbf24
    style CD fill:#7f1d1d,color:#fca5a5
    style RF fill:#7f1d1d,color:#fca5a5
    style DEC fill:#1f2937,color:#556677,stroke-dasharray: 5 5
```

---

## 6. LLM layer (Canonical + Capability)

Core and evaluation code never pick provider-specific parameter names.

```mermaid
flowchart TD
    CS["ChatService / Brain /<br/>Gatekeeper / Sleeptime /<br/>Semantic Judge"]
    CR["CanonicalRequest<br/>llm/canonical.py<br/><i>provider-agnostic</i>"]
    CAP["Capability Resolver<br/>llm/capabilities.py"]
    META["provider metadata"]
    OBS["observed cache<br/>llm_capabilities.json"]
    OVR["LLM_CAPABILITY_OVERRIDES<br/>user_config.json"]
    REG["ConnectionRegistry<br/>llm/connections.py<br/><i>4 built-in + user-defined</i>"]
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

Capabilities resolve in the order **provider metadata → observed cache → manual
override**, never from the model-name prefix. See
[LLM Connections and API-key management](llm_connections.md).

---

## 7. Retrieval modes (`brain.search_mode`)

```mermaid
flowchart TD
    Q["User message<br/>(+ the Gatekeeper's self-contained query)"] --> MODE{search_mode}
    MODE -->|vector<br/>default| V["vector cosine<br/>+ time decay"]
    MODE -->|hybrid| HB["BM25 (FTS5/trigram)<br/>+ vector → RRF fusion"]
    MODE -->|dual_query| DQ["utterance top15<br/>+ query top15<br/>→ equal-weight RRF (max 25)"]
    MODE -->|hybrid_evidence_fusion| HEF["re-score hybrid top-N<br/>with Episode / RAW MaxP<br/>→ weighted fusion (default 0.70/0.30)"]
    V --> RR{reranker<br/>enabled?}
    HB --> RR
    DQ --> RR
    HEF --> RR
    RR -->|off| TOP["inject top search_limit<br/>(default 3)"]
    RR -->|cross_encoder / llm| RRK["reorder the candidate pool<br/><i>fail-open: original order on failure</i>"]
    RRK --> TOP
    TOP --> SRC{rag_source_mode}
    SRC -->|cards, default| C1["card summary / episode"]
    SRC -->|raw| C2["original conversation text<br/>(resolved lazily from source_files)"]
    SRC -->|both| C3["cards + raw text<br/>(only rag_raw_top_k expanded)"]

    style V fill:#065f46,color:#10b981
    style HB fill:#1f2937,color:#8899aa
    style DQ fill:#1f2937,color:#8899aa
    style HEF fill:#1f2937,color:#8899aa
    style RRK fill:#4c1d95,color:#c4b5fd
```

Anything beyond `vector`, including the reranker, is **promoted only after evaluation
confirms it helps**. For the comparison procedure see
[LoCoMo Evaluation Web Console](evaluation_web_console.md).

---

## 8. SSE streaming flow (`POST /api/v1/chat/stream`)

```mermaid
sequenceDiagram
    participant UI as Desktop UI (React)
    participant API as butly_api /api/v1/chat/stream
    participant CS as ChatService.execute_stream
    participant GK as Gatekeeper
    participant SU as StateUpdater (parallel)
    participant P as Provider.async_generate_stream

    UI->>API: POST /api/v1/chat/stream (with request_id)
    API->>CS: call execute_stream()
    CS->>GK: classify(user_input, history, ...)
    GK-->>CS: tier / need / probe
    CS-->>UI: event: metadata (tier, need, scores)
    CS->>SU: asyncio.create_task(update_state)
    CS->>P: async for chunk in stream
    loop until done
        P-->>CS: {"type": "chunk", "text": ...}
        CS-->>UI: event: chunk
    end
    UI--)API: POST /api/v1/chat/requests/{id}/cancel (optional)
    P-->>CS: {"type": "done", debug, sources}
    CS->>SU: await state_task
    CS->>CS: save_single_turn + maintain_memory + persist trace
    CS-->>UI: event: done (debug_info, session_state, sources)
    Note over UI,API: Failures arrive as event: error.<br/>A failure before any output can be retried<br/>idempotently with the same request_id.
```

The legacy `POST /chat/stream` (Streamlit compatibility) goes through the same
`ChatService.execute_stream()`.

---

## 9. Trace Graph

Each response's internal flow is stored as nodes and edges (`trace.json`, schema version 1).

```mermaid
flowchart LR
    subgraph "Node types"
        N1["input"] --> N2["loader"] --> N3["decision"]
        N3 --> N4["retrieval"] --> N5["tool"]
        N5 --> N6["context"] --> N7["provider"] --> N8["llm"]
        N8 --> N9["formatter"] --> N10["memory"] --> N11["housekeeper"] --> N12["end"]
    end
```

| status | Meaning | Mermaid rendering |
|---|---|---|
| `active` | Actually executed | Green fill, solid edge |
| `skipped` | A candidate that went unused | Grey, dashed |
| `fallback` | Used as a fallback | Orange fill, dashed |
| `error` | Failed | Red fill |
| `warning` | Succeeded but noteworthy | Yellow fill |

Traces are always saved in full; display filtering is controlled by `detail`
(`full` / `summary`) and `hidden_nodes` under `SYSTEM_CONFIG["trace"]`.
`butly_core/trace/mermaid.py` produces a frontend-agnostic Mermaid string, so the
desktop UI and the evaluation screens render the same output.

---

## 10. Settings resolution order

```mermaid
flowchart TD
    D["settings/defaults.py<br/>AI_CONFIG / SYSTEM_CONFIG"] --> U["user_config.json<br/><i>recursive_update</i>"]
    U --> NORM["normalize_ai_config()<br/>infer and validate connection"]
    NORM --> RS["RootSettings<br/><i>get_settings() / lru_cache</i>"]
    RS --> BOOT["apply_runtime_settings(data_dir)"]
    BOOT --> LEG["butly_core.config<br/>AI_CONFIG / SYSTEM_CONFIG<br/><i>compat shim, in place</i>"]
    BOOT --> CREG["ConnectionRegistry"]
    BOOT --> CRT["Capability runtime"]
    LEG --> INST["instance config.json<br/><i>deep-merged as override_config</i>"]
    INST --> REQ["per-request override<br/><i>model_name, etc.</i>"]
    ENV["BUTLY_* env vars"] -.->|no env source:<br/>cannot override settings| RS

    style ENV fill:#7f1d1d,color:#fca5a5,stroke-dasharray: 5 5
    style LEG fill:#1f2937,color:#8899aa
```

See [Configuration Layer](configuration.md).

---

## 11. Per-instance directory layout

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
    DB["butly_memory.db<br/>knowledge_cards<br/>memory_nodes and more"]
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

`memory_nodes`, `memory_node_sources`, `memory_maturation_runs`, and
`memory_maturation_run_cards` live in the **same `butly_memory.db`** as `knowledge_cards`.
