# Gatekeeper I/O Specification & Prompt Injection Summary

🌐 [日本語](gatekeeper_io_summary.ja.md) | **English**

Based on the current implementation of `butly_core/core/gatekeeper.py` and related files, this document summarizes the input/output data of the Gatekeeper (frontal lobe) module and the information ultimately passed to the main AI (Brain) based on the determined Tier (layer).

---

## 1. Information Gatekeeper **Receives** (Input at classification time)

Context passed to the lightweight LLM (Gemini) for Gatekeeper to judge "the complexity of the user's utterance and the required memory level."

- **User's latest utterance** (`user_input`)
- **Recent conversation history** (`history_msgs`): Recent exchanges (up to the last 3 turns).
- **Session state** (`session_state`): Dynamic state information per session.
  - `Topic`: Current topic
  - `Mood`: User's or conversation's mood
  - `Goals`: Current goal list (up to 5 items)
  - `Unresolved`: List of unresolved items (up to 8 items)
  - `Turn`: Number of elapsed turns in the session

---

## 2. Gatekeeper **Output** (Classification result)

Output after classification by the lightweight LLM (data for updating internal state).

- **`tier`**: Determined tier (`reflex`, `mid`, or `cortex`)
- **`state_delta`**: Delta data for adding, resolving, or updating the session state (Goals, Unresolved items, etc.).
- **`need` / `search_targets`**: Generated only when classified as `cortex` (cerebral cortex) — "additional information needed" and "search target keywords."

---

## 3. Information **Passed to the Main AI** After Each Tier Classification (Injected Prompt)

After receiving Gatekeeper's classification result (Tier), `MemoryBlockBuilder` assembles the system prompt (System Instruction) and passes it to the main AI (response-generation LLM).

### 🔘 Information **Always Passed** Across All Tiers

Regardless of which Tier is determined, these are always passed as the AI's premises.
1. **SYSTEM INSTRUCTION**: The AI's basic personality and system settings.
2. **KEY MEMORY**: Core and immutable memories about the master (user).
3. **CURRENT TIME**: Current timestamp (system note).
4. **Recent Conversation History (Short Term)**: Typically the last 6 turns of actual message history.
5. **FLOATING SUMMARY (Unorganized memory)**: A floating summary of the latest dialogue flow.
6. **TIER INFO**: Explicit instruction on which tier (reflex / mid / cortex) the AI should think in.

---

### 🔵 Information Added Per Tier

#### 【 Tier 1 】 reflex (Spinal reflex)
- **Additional information**: **None**
- **Characteristics**:
  - Responds immediately using only the "commonly passed information (recent exchanges and short-term summaries)."
  - Triggered by greetings, back-channeling, "ok got it" type replies — content that should be answered promptly without waiting for knowledge retrieval.

#### 【 Tier 2 】 mid (Midbrain / Emotional system)
- **Additional information**: One of the following (depending on settings)
  - **MID-TERM MEMORY**: The full mid-term memory text (RAW data)
  - Or **MID-TERM DIGEST (fact digest) + RELATIONSHIP SNAPSHOT (relationship)** (when summary mode is configured)
- **Characteristics**:
  - Triggered for specific questions about the current topic or conversations that require context from a short while ago.
  - Covers facts and context within the session, but does not yet need to dig into long-term past memories.

#### 【 Tier 3 】 cortex (Cerebral cortex)
- **Additional information**:
  - **All mid information** (MID-TERM MEMORY etc.)
  - ➕ **LONG-TERM MEMORY (RAG)**: Search results from the database (`butly_memory.db`)
- **Characteristics**:
  - Triggered by references to the past such as "that time when…" or questions requiring deep specialized/technical analysis.
  - RAG search is performed against past memories using keywords extracted by Gatekeeper, and the retrieved related information (episodes, etc.) is added to the prompt for the response.
