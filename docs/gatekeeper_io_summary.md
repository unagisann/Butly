# Gatekeeper I/O Specification & Prompt Injection Summary

🌐 [日本語](gatekeeper_io_summary.ja.md) | **English**

Based on the current implementation of the `butly_core/core/gatekeeper/` package, this document summarizes the input/output data of the Gatekeeper (frontal lobe) module and the information ultimately passed to the main AI (Brain) based on the determined Tier.

---

## Architecture Overview

Gatekeeper is split into 4 components.
The `Gatekeeper` class acts as a facade that orchestrates each component.

| Component | File | Role |
|---|---|---|
| `TierClassifier` | `tier_classifier.py` | Has LLM output 4 scores; Python side makes final tier decision |
| `StateUpdater` | `state_updater.py` | Generates state_delta (diff) from user utterance |
| `SearchPlanner` | `search_planner.py` | Called only on cortex; generates RAG search keywords. Can return `need: null` to skip RAG |
| `MemoryBlockBuilder` | `memory_builder.py` | Builds memory block dict per tier and passes it to Brain |

### Processing Flow

```
User utterance
  ↓
[A] TierClassifier.classify()   ← LLM call (parallel)
[B] StateUpdater.update()       ← LLM call (parallel)
[C] SearchPlanner.plan()        ← cortex only
  ↓
Gatekeeper.classify() merges results and returns
  ↓
MemoryBlockBuilder.build()  → constructs prompt for Brain
```

---

## 1. Information Gatekeeper **Receives** (Input to each component)

### Input to TierClassifier

- **User's latest utterance** (`user_input`)
- **Recent conversation history** (`history_msgs`): Recent exchanges (up to the last 3 turns)
- **Current topic** (`current_topic`): Topic string passed from SessionState

### Input to StateUpdater

- **User's latest utterance** (`user_input`)
- **Recent conversation history** (`history_msgs`)
- **Current session state** (`current_state`): SessionState with the following fields:
  - `topic`: Current topic
  - `mood`: Conversation mood (default: `neutral`)
  - `goals`: Goal list (up to 5 items)
  - `unresolved`: List of unresolved items (up to 8 items)
  - `turn_count`: Number of elapsed turns
  - `last_tier`: The tier used in the previous turn

### Input to SearchPlanner (cortex only)

- **User's latest utterance** (`user_input`)
- **Recent conversation history** (`history_msgs`)
- **Current topic** (`current_topic`)

---

## 2. Gatekeeper **Output** (Return value of `Gatekeeper.classify()`)

```python
{
    "tier": "reflex" | "mid" | "cortex",
    "topic": str,          # from state_delta or current topic
    "need": str | None,   # cortex only. null = skip RAG search
    "search_targets": list[str] | None,  # cortex only. null when need is null
    "state_delta": {
        "topic": str | None,
        "mood": str | None,
        "add_goal": str | None,
        "add_unresolved": str | None,
        "resolve": str | None
    },
    "llm_scoring": {
        "response_complexity": float,      # 0-1
        "emotional_weight": float,         # 0-1
        "memory_reference_likelihood": float,  # 0-1
        "continuity_need": float           # 0-1
    }
}
```

### Tier Classification Rules (TierClassifier)

The tier is determined on the Python side using the 4 scores output by the LLM:

| Condition | Result |
|---|---|
| `memory_reference_likelihood >= 0.7` | → `cortex` |
| `response_complexity >= 0.8` or `continuity_need >= 0.8` | → `mid` or above |
| `emotional_weight >= 0.7` | → `mid` or above |
| None of the above | → `reflex` |

---

## 3. Information **Passed to the Main AI** After Each Tier Classification (Injected Prompt)

`MemoryBlockBuilder.build()` assembles a memory block dict per tier and passes it to the Brain (response-generation LLM).

### 🔘 Information **Always Passed** Across All Tiers

| Order | Information | Content |
|---|---|---|
| 1 | **SYSTEM INSTRUCTION** | AI's basic personality and system settings |
| 2 | **KEY MEMORY** | Core and immutable memories about the user |
| 3 | **CURRENT TIME** | Current timestamp (system note) |
| 4 | **GLOSSARY** | Shared vocabulary (semantic memory from active glossary.yaml entries) |
| 5 | **MID-TERM (conditional)** | mid and above only (see below) |
| 6 | **RAG (conditional)** | cortex + need present only (see below) |
| 7 | **FLOATING SUMMARY** | Floating summary of the latest dialogue flow |
| 8 | **TIER INFO** | Current thinking mode (reflex / mid / cortex) |
| 9 | **WEB SEARCH RESULTS** (conditional) | Non-Gemini + use_web_search=True only. Web search results via Tavily API |
| 10 | **Short Term** | Last 6 turns of conversation history |

### 🔵 Information Added Per Tier

#### 【 Tier 1 】 reflex (Spinal reflex)
- **Additional information**: None
- Triggered by greetings, back-channeling, "ok got it" — responds immediately without waiting for knowledge retrieval.

#### 【 Tier 2 】 mid (Midbrain / Emotional system)
- **Additional information**: Switches based on `use_summarized_mid_term` setting:
  - `False` (RAW mode): **MID-TERM MEMORY** (full text of `mid_term.txt`)
  - `True` (summary mode): **MID-TERM DIGEST** + **RELATIONSHIP SNAPSHOT** set
    (falls back to RAW if summary files don't exist)
- Triggered for specific questions about the current topic or conversations requiring context from a short while ago.

#### 【 Tier 3 】 cortex (Cerebral cortex)
- **Additional information**:
  - All mid information
  - ➕ **LONG-TERM MEMORY (RAG)**: Search results from `butly_memory.db`
    (using `search_targets` generated by SearchPlanner as keywords)
  - ※ When SearchPlanner returns `need: null`, RAG search is skipped
- Triggered by references to the past such as "that time when…" or questions requiring deep analysis.

---

## 4. SessionState Persistence

The `SessionState` class handles reading/writing `session_state.json`. On the `ChatService` side, `state_delta` generated by `StateUpdater` is applied and saved via `SessionState.apply_delta()`.

```python
# state_delta structure
state_delta = {
    "topic": str | None,           # Update topic
    "mood": str | None,            # Update mood
    "add_goal": str | None,        # Add a goal
    "add_unresolved": str | None,  # Add an unresolved item
    "resolve": str | None          # Move an unresolved item to resolved
}
```
