# Butly Memory Lifecycle Specification

🌐 [日本語](memory_lifecycle.ja.md) | **English**

Butly's memory system is organized into multiple layers based on the freshness and importance of conversations.
This document describes each layer's role, write timing, promotion flow, overflow handling, and configuration parameters.

---

## Overall Flow

```
During chat (every turn)
  ↓
[1] short_term_json/  ← raw turn logs (JSON)
  ↓ when short_term_limit is exceeded
[2] floating_summaries/  ← per-conversation floating summaries (txt)
  ↓
[3] memory_archive/1_integrated/  ← transit staging zone

──── During Sleeptime run ───────────────────────────────

[Stage 0] short_term_json/* → moved entirely to 1_integrated/

[Stage 1] Read 1_integrated JSONs → format text
  ├─ [4] mid_term.txt ← append RAW log
  ├─ [5] mid_term_digest.txt ← LLM daily fact digest (incremental append)
  └─ [6] mid_term_relationship.txt ← LLM relationship snapshot (full overwrite every 7 days)

[Stage 2] 1_integrated JSONs → group by date → LLM knowledge extraction
  └─ [7] butly_memory.db ← long-term vector DB (RAG)
          ↓ after processing
         memory_archive/2_knowledgeized/{date}/
```

---

## Memory Layer Details

### 1. Short-Term JSON (Short-Term Log)

| Item | Content |
|---|---|
| **Location** | `instances/{name}/short_term_json/session_YYYYMMDD_HHMMSS.json` |
| **Written by** | `memory.save_single_turn()` called automatically after every response |
| **Format** | `{"timestamp": "...", "messages": [{"role": "user", "parts": [...]}, {"role": "model", "parts": [...]}]}` |
| **Limit** | `short_term_limit` (default: 6 files) |
| **Overflow handling** | `memory.maintain_memory()` summarizes older files via LLM, saves to floating_summaries/, then moves to 1_integrated/ |
| **Injected by Gatekeeper** | Short Term block (last 6 turns) — injected at all tiers |

**Config parameters:**
```python
SYSTEM_CONFIG["memory"]["short_term_limit"] = 6  # number of files to retain
```

---

### 2. Floating Summaries (Floating Summary)

| Item | Content |
|---|---|
| **Location** | `instances/{name}/floating_summaries/*.txt` (legacy: `floating_summary.txt`) |
| **Written by** | `memory.maintain_memory()` calls `brain.summarize_conversation()` on overflow |
| **Format** | One txt file per conversation. Body is summary text; legacy first line `Time: {timestamp}` is stripped on read. |
| **Read format** | `ButlyMemory.get_floating_summary()` concatenates files with **relative-time headers** (e.g. `--- about 30 minutes ago ---`) — file names and absolute timestamps are intentionally omitted to keep the LLM from treating each timestamp as a separate conversation. |
| **Injected by Gatekeeper** | FLOATING SUMMARY block — injected at all tiers (serves as recent conversation context) |
| **Lifecycle** | All files deleted at Sleeptime run (no data loss since raw JSON exists in 1_integrated) |
| **Model used** | `AI_CONFIG["summary"]["model_name"]` (cost-effective, long-context) |

> **Design intent:** floating_summaries provide temporary context for "the current conversation flow."
> Permanent storage is handled by mid_term.txt and butly_memory.db.

---

### 3. Memory Archive / 1_integrated (Transit Staging Zone)

| Item | Content |
|---|---|
| **Location** | `instances/{name}/memory_archive/1_integrated/` |
| **Written by** | ① `maintain_memory()` moves overflow files here; ② Sleeptime Stage 0 flushes all short_term_json/* here |
| **Content** | Same raw JSON files as short_term_json |
| **Role** | Both Stage 1 (mid_term update) and Stage 2 (knowledgeize) read from here |
| **Post-processing** | Moved to `2_knowledgeized/{date}/` after Stage 2 completion |

---

### 4. mid_term.txt (Mid-Term RAW Log)

| Item | Content |
|---|---|
| **Location** | `instances/{name}/mid_term.txt` |
| **Written by** | Sleeptime Stage 1 (`stage_1_cleanup`) — formats 1_integrated JSONs as text and appends |
| **Limit** | `max_mid_term_chars` (default: 30,000 characters) |
| **Overflow** | Oldest characters archived to `memory_archive/3_log/archive_long_term.txt`; only recent portion retained |
| **Injected by Gatekeeper** | When `use_summarized_mid_term = False` (RAW mode): injected as MID-TERM MEMORY block at the mid tier |
| **Format** | Line-format text: `[YYYY-MM-DD HH:MM:SS] {role_label}: {text}` |

**Config parameters:**
```python
SYSTEM_CONFIG["memory"]["max_mid_term_chars"] = 30000
SYSTEM_CONFIG["memory"]["use_summarized_mid_term"] = True  # True = summary injection, False = RAW injection
```

---

### 5. mid_term_digest.txt (Fact Digest)

| Item | Content |
|---|---|
| **Location** | `instances/{name}/mid_term_digest.txt` |
| **Written by** | Sleeptime Stage 1 `_generate_daily_digest()` — extracts facts from today's raw log via LLM, incremental append |
| **Input** | Only today's raw log text (`new_text`). Never summarizes a summary |
| **Input chunking** | When `digest_max_input_chars` is set, splits at date headers `[YYYY-MM-DD ...]` into chunks, sends each to LLM, then combines results |
| **Limit** | `max_digest_chars` (default: 8,000 characters) |
| **Overflow** | Appended to `memory_archive/3_log/archive_digest.txt`; only recent portion retained |
| **Injected by Gatekeeper** | When `use_summarized_mid_term = True` (summary mode): injected as MID-TERM DIGEST block at the mid tier |
| **Skip conditions** | `new_text` shorter than 200 characters, or `generate_mid_term_summaries = False` |
| **Model used** | `AI_CONFIG["summary"]["model_name"]` (Flash Lite class) |

**Config parameters:**
```python
SYSTEM_CONFIG["memory"]["generate_mid_term_summaries"] = True
SYSTEM_CONFIG["memory"]["max_digest_chars"] = 8000
# config.json > sleeptime section:
digest_max_input_chars = 0   # Max input chars per LLM call. 0 = unlimited
```

---

### 5b. recent_digest_headlines.json (Recent Headlines)

| Item | Content |
|---|---|
| **Location** | `instances/{name}/recent_digest_headlines.json` |
| **Written by** | Sleeptime Stage 1 `_generate_recent_headlines()` — extracts up to 4 headlines from digest via LLM |
| **Input** | Tail of `mid_term_digest.txt` (max 10,000 chars) |
| **Format** | JSON array of `{"type": "topic" or "event", "headline": "20-40 char summary"}` |
| **Used by** | `Gatekeeper.__init__()` loads headlines and passes to ContextClassifier for scoring |
| **Lifecycle** | Overwritten on every Sleeptime run |
| **Model used** | `AI_CONFIG["summary"]["model_name"]` (Flash Lite class) |

---

### 6. mid_term_relationship.txt (Relationship Snapshot)

| Item | Content |
|---|---|
| **Location** | `instances/{name}/mid_term_relationship.txt` |
| **Written by** | Sleeptime Stage 1 `_update_relationship_if_due()` — full overwrite every 7 days |
| **Input** | `mid_term_digest.txt` (accumulated fact digest) — does NOT use daily fragments directly |
| **Update frequency** | Only updated when `relationship_update_interval_days` (default: 7 days) have elapsed since last update |
| **Injected by Gatekeeper** | When `use_summarized_mid_term = True`: injected as RELATIONSHIP SNAPSHOT block at the mid tier |
| **Skip conditions** | `mid_term_digest.txt` shorter than 200 characters, or interval not yet reached |
| **Model used** | `AI_CONFIG["knowledge"]["model_name"]` (high-reasoning Pro class) |
| **Design intent** | Relationships change gradually; daily overwriting causes instability. Weekly cadence is appropriate |

**Config parameters:**
```python
SYSTEM_CONFIG["memory"]["relationship_update_interval_days"] = 7
```

---

### 7. butly_memory.db (Long-Term Vector DB)

| Item | Content |
|---|---|
| **Location** | `instances/{name}/butly_memory.db` |
| **Written by** | Sleeptime Stage 2 (`stage_2_knowledgeize`) — LLM extracts knowledge cards per date group, INSERTs into DB |
| **Input** | Raw JSON from 1_integrated, combined per date |
| **Input chunking** | When `knowledge_max_input_chars` is set, splits at JSON file boundaries. "Adding the next file would exceed the limit → commit current chunk" |
| **Skip feature** | When `skip_knowledge_generation = true`, Stage 2 is skipped entirely. RAW data remains in 1_integrated for later batch processing with a higher-capability model |
| **Schema** | `knowledge_cards` table (see below) |
| **Embedding** | `title + tags + summary` embedded via `AI_CONFIG["embedding"]["model_name"]` → stored as BLOB |
| **Search** | `ButlyBrain.search_memories()` re-ranks by cosine similarity between query and embedding_blob |
| **Injected by Gatekeeper** | Whenever `need` is set (tier-independent): RAG block built from MemoryProbe candidates, injected as LONG-TERM MEMORY block |
| **Post-processing** | Processed JSONs moved to `memory_archive/2_knowledgeized/{date}/` |
| **Backup** | Rotation backup saved to `butly_core/db_backups/` (generations: `backup.generations`) |

**knowledge_cards table — key columns:**

| Column | Type | Content |
|---|---|---|
| `id` | TEXT | Unique ID in `{db_type}_{YYYYMMDD}_{seq}` format |
| `type` | TEXT | Instance name (db_type) |
| `category` | TEXT | LLM-assigned category |
| `title` | TEXT | Title of the episode |
| `tags` | TEXT | Comma-separated search tags |
| `summary` | TEXT | Factual summary text |
| `episode` | TEXT | Episode details |
| `ai_importance` | REAL | Importance to AI (0-1) |
| `humanity_importance` | REAL | Importance to humanity (0-1) |
| `embedding_blob` | BLOB | float32 byte array for cosine similarity search |

---

## Sleeptime Execution Flow (Detailed)

The Sleeptime runs via manual trigger or scheduled execution, processing each instance in sequence.

```
ButlySleeptime.run()
  ↓
  process_instance(instance_path)  ← called for each instance
    ├── stage_1_cleanup(instance_path)
    │     ├── [Stage 0] Flush: short_term_json/* → 1_integrated/
    │     ├── [Step 1] Format 1_integrated JSONs to text
    │     ├── [Step 2] Delete floating_summaries/* (clear temporary context)
    │     ├── [Step 3] Append to mid_term.txt (archive overflow to 3_log/)
    │     ├── [Step 4] _generate_daily_digest() → incremental update mid_term_digest.txt
    │     │          └── ★ Chunks by date headers when digest_max_input_chars is exceeded
    │     ├── [Step 5] _generate_recent_headlines() → recent_digest_headlines.json (up to 4 headlines)
    │     └── [Step 6] _update_relationship_if_due() → mid_term_relationship.txt (7-day interval)
    │
    └── stage_2_knowledgeize(instance_path, db_type)
          ├── ★ skip_knowledge_generation check (skip if true)
          ├── Group 1_integrated JSONs by date
          ├── Chunk by file boundary (controlled by knowledge_max_input_chars)
          ├── Per chunk: ask_gemini_to_summarize() → generate knowledge_cards
          ├── Generate embedding per card → INSERT into butly_memory.db
          └── Move processed JSONs → 2_knowledgeized/{date}/
```

---

## Real-Time Processing During Chat

Independent of the Sleeptime, the following processing occurs during every chat turn:

```
1. User message received
2. ChatService → Gatekeeper.classify()  (tier determination)
3. MemoryBlockBuilder.build()  (memory block construction)
       ↓ source for each block
       SYSTEM INSTRUCTION   ← system_instruction.txt
       KEY MEMORY           ← Key_Memory.txt
       MID-TERM DIGEST      ← mid_term_digest.txt  (summary mode)
       MID-TERM MEMORY      ← mid_term.txt         (RAW mode)
       CURRENT TIME         ← system clock
       LONG-TERM (RAG)      ← butly_memory.db      (when need is set, tier-independent)
       FLOATING SUMMARY     ← floating_summaries/*.txt
       TIER INFO            ← tier string
       SHORT TERM           ← short_term_json/*.json (last 6 turns)
4. Brain.generate() produces response
5. memory.save_single_turn() → saves JSON to short_term_json/
6. memory.maintain_memory() → checks short_term_limit
       If exceeded: summarize old files via LLM → floating_summaries/ + 1_integrated/
```

---

## Archive Directory Structure

```
instances/{name}/
├── short_term_json/           # ① active raw turn logs
├── floating_summaries/        # ② temporary floating summaries (from short-term overflow)
├── floating_summary.txt       # ② legacy file (backward compatibility)
├── mid_term.txt               # ④ mid-term RAW log (latest 30,000 chars)
├── mid_term_digest.txt        # ⑤ fact digest (latest 8,000 chars)
├── mid_term_relationship.txt  # ⑥ relationship snapshot (updated every 7 days)
├── Key_Memory.txt             # immutable core memory (manual edit)
├── system_instruction.txt     # AI personality definition (manual edit)
├── session_state.json         # Gatekeeper session state
├── recent_digest_headlines.json  # recent conversation headlines (Gatekeeper input)
├── glossary.yaml              # Glossary / Lorebook (term / aliases / category / status / priority)
├── debug_logs/                # ChatService debug auto-save
│   ├── latest.json            # latest turn (overwrite)
│   └── history/               # last 20 turns (rotated)
├── butly_memory.db            # ⑦ long-term vector DB
└── memory_archive/
    ├── 1_integrated/          # ③ raw JSONs awaiting Sleeptime processing
    ├── 2_knowledgeized/       # knowledgeized JSONs (per-date folders)
    │   └── {YYYY-MM-DD}/
    └── 3_log/
        ├── archive_long_term.txt  # overflow from mid_term.txt
        └── archive_digest.txt     # overflow from mid_term_digest.txt
```

---

## Model Usage Summary

| Process | Config key | Purpose |
|---|---|---|
| Chat response generation | `AI_CONFIG["chat"]["model_name"]` | Brain main response |
| Conversation summarization (floating) | `AI_CONFIG["summary"]["model_name"]` | Floating summary on short-term overflow |
| Fact digest generation | `AI_CONFIG["summary"]["model_name"]` | Generate mid_term_digest |
| Relationship snapshot | `AI_CONFIG["knowledge"]["model_name"]` | Generate mid_term_relationship |
| Recent headlines extraction | `AI_CONFIG["summary"]["model_name"]` | Generate recent_digest_headlines.json |
| Knowledge card extraction | `AI_CONFIG["knowledge"]["model_name"]` | Stage 2 RAG DB extraction |
| Embedding vector generation | `AI_CONFIG["embedding"]["model_name"]` | knowledge_cards.embedding_blob |
| Tier classification | `AI_CONFIG["gatekeeper"]["model_name"]` | ContextClassifier 3-score output |
