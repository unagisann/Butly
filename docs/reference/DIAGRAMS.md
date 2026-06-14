# Butly — Architecture Diagrams

🌐 [日本語](DIAGRAMS.ja.md) | **English**

This file compiles Mermaid diagrams that visualize the system design of Butly.

---

## 1. Overall Architecture (Main Flow)

The processing flow from user input to response generation and memory storage.

```mermaid
flowchart TD
    A((User Input)) --> B["⧫ Gatekeeper<br/>ContextClassifier + MemoryProbe"]
    B --> C["Structured Output<br/>tier / need / need_intent / probe candidates"]
    C --> E{tier}
    E -->|reflex| F["⚡ reflex<br/>Minimal context"]
    E -->|mid| G["◎ mid<br/>Memory injected"]
    C --> N{need?<br/>tier-independent}
    N -->|set| H["⌕ RAG block<br/>from MemoryProbe candidates"]
    H --> I[("⛁ Knowledge DB<br/>(SQLite + Embeddings)")]
    F --> J["◆ ChatService<br/>Provider.generate() / async_generate_stream()"]
    G --> J
    H --> J
    J -->|non-Gemini + search ON| WS["🔍 SearchModule<br/>Tavily / Ollama Cloud"]
    WS -->|inject results into context| J
    J -.->|parallel via asyncio.gather| SU["⟳ StateUpdater<br/>(post-response)"]
    SU -.->|state_delta| D["◈ Session State<br/>topic / mood / turn_count"]
    D -.->|next-turn reference| B
    J --> K((Response / SSE chunks))
    K --> L["▣ short_term_json save"]
    L -.->|scheduled| M["⚙ Sleeptime<br/>Daily + Weekly batch"]
    M -.->|knowledge generation| I
    M -.->|recent_digest_headlines| B
```

---

## 2. system_instruction Injection Order

The order in which context blocks are built when passed to the LLM (upper = immutable, lower = variable).

```mermaid
block-beta
    columns 1
    A["1. SYSTEM INSTRUCTION — Personality settings (immutable)"]
    B["2. KEY MEMORY — Core memory (immutable)"]
    C["3. CURRENT TIME — Current timestamp"]
    D["4. GLOSSARY — Shared vocabulary / Semantic memory"]
    E["5. MID-TERM — Mid-term memory digest + relationship (low-frequency update)"]
    F["6. RAG — Long-term memory search results (※ annotated as reference)"]
    G["7. SESSION DIGEST — Compressed overflow conversation log (※ annotated as recent context)"]
    H["8. TIER INFO — Thinking mode"]
    I["9. WEB SEARCH RESULTS — Web search results (non-Gemini + search ON only)"]

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

## 3. Mid-term Two-layer Summary Structure

The memory pipeline from short_term_json (RAW).

```mermaid
flowchart LR
    RAW["short_term_json<br/>(RAW)"] --> INT["1_integrated<br/>(RAW archive)"]
    INT --> S1a["Stage 1a<br/>mid_term.txt<br/>RAW accumulation"]
    INT --> S1b["Stage 1b ★<br/>mid_term_digest.txt<br/>Episode-tagged incremental append"]
    S1b --> S1c["Stage 1c ★<br/>mid_term_relationship.txt<br/>Weekly overwrite"]
    INT --> S2["Stage 2<br/>knowledge_cards<br/>episode generation"]
    INT --> KN["2_knowledgeized<br/>(RAW permanent archive)"]

    style S1b fill:#065f46,color:#10b981
    style S1c fill:#4c1d95,color:#8b5cf6
    style S1a fill:#1f2937,color:#8899aa
    style S2 fill:#065f46,color:#10b981
```

---

## 4. Sleeptime Stage Configuration

The configuration of daily and weekly batch processing.

```mermaid
flowchart TD
    subgraph "Daily Batch"
        S1a["Stage 1a<br/>Mid-term RAW accumulation"]
        S1b["Stage 1b ★<br/>Episode-tagged Digest generation<br/><i>Today's RAW → append to digest</i><br/><i>★ Date-header-aware chunk splitting</i>"]
        SKIP{★ skip_knowledge?}
        S2["Stage 2<br/>Knowledge card generation<br/><i>RAW → episode cards</i><br/><i>★ File-boundary chunk splitting</i>"]
        S2SKIP["Skip<br/>Keep RAW in 1_integrated"]
    end
    subgraph "Weekly Batch"
        S1c["Stage 1c ★<br/>Relationship Snapshot update<br/><i>digest → overwrite relationship</i>"]
        S3["Stage 3 (not yet implemented)<br/>Integrated memory generation<br/><i>episodes → reflection etc.</i>"]
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

## 5. Multi-provider Configuration

The structure of the LLM provider abstraction layer.

```mermaid
flowchart TD
    CS["ChatService<br/>(Orchestration<br/>+ streaming)"]
    GK["Gatekeeper<br/>(Tier + need_intent)"]
    HK["Sleeptime<br/>(Memory maintenance)"]
    PF["ProviderFactory<br/>(model_name → provider auto-routing)"]
    GE["GeminiProvider<br/>gemini-*"]
    OA["OpenAIProvider<br/>gpt-* / o1 / o3 / o4"]
    XA["XaiProvider<br/>grok-* / xai/*"]
    OL["OllamaProvider<br/>ollama/*"]
    CMP["_openai_compat.py<br/>(shared helpers)"]

    CS --> PF
    GK --> PF
    HK --> PF
    PF --> GE
    PF --> OA
    PF --> XA
    PF --> OL
    OA -.->|uses| CMP
    XA -.->|uses| CMP
    OL -.->|uses| CMP
```

---

## 5b. SSE Streaming Flow (`POST /chat/stream`)

How `ChatService.execute_stream()` and the Provider's `async_generate_stream()` cooperate.

```mermaid
sequenceDiagram
    participant UI as Streamlit UI
    participant API as FastAPI /chat/stream
    participant CS as ChatService.execute_stream
    participant GK as Gatekeeper
    participant SU as StateUpdater (parallel)
    participant P as Provider.async_generate_stream

    UI->>API: POST /chat/stream (use_streaming=true)
    API->>CS: invoke execute_stream()
    CS->>GK: classify(user_input, history, ...)
    GK-->>CS: tier / need / probe
    CS-->>UI: event: metadata (tier, need, scores)
    CS->>SU: asyncio.create_task(update_state)
    CS->>P: async for chunk in stream
    loop until done
        P-->>CS: {"type": "chunk", "text": ...}
        CS-->>UI: event: chunk
    end
    P-->>CS: {"type": "done", debug, sources}
    CS->>SU: await state_task
    CS->>CS: save_single_turn + maintain_memory + debug log
    CS-->>UI: event: done (debug_info, session_state, sources)
```

---

## 6. Per-instance Directory Structure

The memory file layout for each AI instance.

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
    FS["session_digests/"]
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
