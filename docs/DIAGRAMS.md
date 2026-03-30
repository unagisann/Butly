# Butly — Architecture Diagrams

🌐 [日本語](DIAGRAMS.ja.md) | **English**

This file compiles Mermaid diagrams that visualize the system design of Butly.

---

## 1. Overall Architecture (Main Flow)

The processing flow from user input to response generation and memory storage.

```mermaid
flowchart TD
    A((User Input)) --> B["⧫ Gatekeeper<br/>Provider.classify()"]
    B --> C["Structured Output<br/>tier / need / search_targets / state_delta"]
    C -.->|state_delta| D["◈ Session State<br/>topic / mood"]
    D -.->|reference| B
    C --> E{tier}
    E -->|reflex| F["⚡ reflex<br/>Minimal context"]
    E -->|mid| G["◎ mid<br/>Memory injected"]
    E -->|cortex| H["⌕ Missing prerequisite search<br/>need-driven retrieval"]
    H --> I[("⛁ Integrated Memory DB<br/>episode / reflection<br/>generalization / self_model")]
    F --> J["◆ ChatService<br/>Provider.generate()"]
    G --> J
    I -->|search results| J
    J -->|non-Gemini + search ON| WS["🔍 SearchModule<br/>Tavily API"]
    WS -->|inject results into context| J
    J --> K((Response))
    K --> L["▣ short_term_json save"]
    L -.->|scheduled| M["⚙ Housekeeper<br/>Daily + Weekly batch"]
    M -.->|integrated memory generation| I
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
    G["7. FLOATING — Recent conversation summary (※ annotated as recent context)"]
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

## 4. Housekeeper Stage Configuration

The configuration of daily and weekly batch processing.

```mermaid
flowchart TD
    subgraph "Daily Batch"
        S1a["Stage 1a<br/>Mid-term RAW accumulation"]
        S1b["Stage 1b ★<br/>Episode-tagged Digest generation<br/><i>Today's RAW → append to digest</i>"]
        S2["Stage 2<br/>Knowledge card generation<br/><i>RAW → episode cards</i>"]
    end
    subgraph "Weekly Batch"
        S1c["Stage 1c ★<br/>Relationship Snapshot update<br/><i>digest → overwrite relationship</i>"]
        S3["Stage 3 (not yet implemented)<br/>Integrated memory generation<br/><i>episodes → reflection etc.</i>"]
    end

    S1a --> S1b --> S1c
    S1a --> S2
    S2 -.-> S3

    style S1b fill:#065f46,color:#10b981
    style S1c fill:#4c1d95,color:#8b5cf6
    style S3 fill:#1f2937,color:#556677,stroke-dasharray: 5 5
```

---

## 5. Multi-provider Configuration

The structure of the LLM provider abstraction layer.

```mermaid
flowchart TD
    CS["ChatService<br/>(Orchestration)"]
    GK["Gatekeeper<br/>(Tier classification)"]
    HK["Housekeeper<br/>(Memory maintenance)"]
    PF["ProviderFactory<br/>(model_name → provider auto-routing)"]
    GE["GeminiProvider<br/>gemini-*"]
    OA["OpenAIProvider<br/>gpt-* / o1 / o3 / o4"]
    OL["OllamaProvider<br/>ollama/*"]

    CS --> PF
    GK --> PF
    HK --> PF
    PF --> GE
    PF --> OA
    PF --> OL
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
    FS["floating_summaries/"]
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
    I1 --> AR
    AR --> A1
    AR --> A2
    AR --> A3
```

---

## 7. Implementation Roadmap

```mermaid
timeline
    title Butly Memory Architecture v2 — Implementation Roadmap
    Phase 1 ✅ : Gatekeeper v2
                : Gemini API migration
                : Structured JSON output
                : SessionState introduction
    Phase 2 ✅ : Caller-side integration
                : classify() switch
                : SessionState live operation
                : Memory injection order optimization
    Phase 3 ✅ : Two-layer summary pipeline
                : Episode-tagged Digest (daily)
                : Relationship Snapshot (weekly)
                : sys_inst+key_memory reference
    Phase 4 ✅ : Summary injection switch
             : build_system_instruction refactor
             : RAW→Summary toggle switch
             : Quality validation
    Multi-Provider ✅ : Multi-provider support
    Web Search ✅ : Generic web search module
                  : Tavily API integration
                  : Non-Gemini provider support
             : OpenAI / Ollama added
             : google.genai isolation
             : Embedding migration
    Phase 5 ✅ : Glossary (Semantic memory)
             : need:null RAG skip
             : ChatService RAG consolidation
             : Gatekeeper / RAG ON/OFF
    Phase 5.5 ✅ : Gatekeeper headlines + session_state compactification
             : session_state goals/unresolved removal
             : recent_digest_headlines introduction
             : StateUpdater output simplification
    Phase 6 : Integrated memory generation
             : Housekeeper Stage3
             : reflection / generalization
             : self_model accumulation start
    Phase 6 : GK neuroscience tuning
             : Semantic memory vs Episodic memory
             : Tier classification accuracy improvement
    Final Form : Full autonomy
                : system_instruction single line
                : Autonomous persona reconstruction from memory
```
