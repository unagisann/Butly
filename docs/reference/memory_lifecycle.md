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
[2] session_digests/  ← per-conversation session digests (txt)
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
| **Location** | `instances/{name}/short_term_json/session_YYYYMMDD_HHMMSS_ffffff[_NNN].json` |
| **Written by** | `memory.save_single_turn()` called automatically after every response |
| **Format** | `{"timestamp": "...", "messages": [{"role": "user", "parts": [...]}, {"role": "model", "parts": [...]}]}` |
| **Timestamp override** | Live chat uses the current time. History import can pass `save_single_turn(..., created_at=...)`, which applies the source time to both the filename and JSON `timestamp`. Repeated identical timestamps receive `_001` and later suffixes instead of overwriting an existing turn. |
| **Speaker attribution meta** | Turns from external entrances (Discord / LINE) carry a `meta` on the user message: `{"person_id": "...", "display_name": "...", "lane": "direct", "source": "discord", "channel_key": "guild:channel"}`. **Missing meta is interpreted as owner / direct / web** (backward compatible, no migration needed) |
| **Limit** | `short_term_limit` (default: 6 files) |
| **Overflow handling** | `memory.maintain_memory()` summarizes older files via LLM, saves to session_digests/, then moves to 1_integrated/ |
| **Injected by Gatekeeper** | Short Term block (last 6 turns) — injected at all tiers |

**Config parameters:**
```python
SYSTEM_CONFIG["memory"]["short_term_limit"] = 6  # number of files to retain
```

> **Speaker attribution (person_id):** external accounts are resolved to a person_id
> by `ButlyRuntime` via `PersonRegistry` (`DATA_DIR/persons.json`,
> `butly_core/external/person_registry.py`). Unregistered users get a deterministic
> provisional ID `p_{source}_{hash}` without exposing the external ID directly in
> RAW logs. When Sleeptime, maintain_memory, or raw_memory_cache format a batch
> that contains multiple speakers, user utterances are labeled with
> `「display_name」:` (1:1 conversations keep the legacy format).
> See `docs/planning/active/group_context_lanes_plan.ja.md` Phase 1.

---

### 2. Session Digests

| Item | Content |
|---|---|
| **Location** | `instances/{name}/session_digests/*.txt` (single-file fallback: `session_digest.txt`; legacy `floating_*` paths are still read) |
| **Written by** | `memory.maintain_memory()` calls `brain.summarize_conversation()` on overflow |
| **Format** | One txt file per conversation. Body is summary text; legacy first line `Time: {timestamp}` is stripped on read. |
| **Read format** | `ButlyMemory.get_session_digest()` concatenates files with **relative-time headers** (e.g. `--- about 30 minutes ago ---`) — file names and absolute timestamps are intentionally omitted to keep the LLM from treating each timestamp as a separate conversation. |
| **Injected by Gatekeeper** | SESSION DIGEST block — injected at all tiers (serves as recent conversation context) |
| **Lifecycle** | All files deleted at Sleeptime run (no data loss since raw JSON exists in 1_integrated) |
| **Model used** | `AI_CONFIG["summary"]["model_name"]` (cost-effective, long-context) |

> **Design intent:** session digests compress conversation that overflowed recent sessions, preserving flow that would otherwise fall out of the live context window.
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
| **Card granularity** | One primary memory unit per card (event, decision, status change, ongoing state, or relationship development). Supporting facts about the same event may stay together, but independently retrievable events, time anchors, or source-file subsets are split. File boundaries are storage boundaries, not a one-file-one-card rule |
| **Skip feature** | When `skip_knowledge_generation = true`, Stage 2 is skipped entirely. RAW data remains in 1_integrated for later batch processing with a higher-capability model |
| **Schema** | `knowledge_cards` table (see below) |
| **Embedding** | `title + tags + summary` embedded via `AI_CONFIG["embedding"]["model_name"]` → stored as BLOB. The **embedding profile**'s document prefix is applied first (see below) |
| **Search** | `ButlyBrain.search_memories()` re-ranks by cosine similarity between query and embedding_blob. The query side gets the **query prefix** |
| **Injected by Gatekeeper** | Whenever `need` is set (tier-independent): RAG block built from MemoryProbe candidates, injected as LONG-TERM MEMORY block. The injected source is controlled by `memory.rag_source_mode`: `"cards"` (default, cards only) / `"raw"` (original conversation excerpts only) / `"both"` (cards + excerpts). For raw/both, each card's `source_files` is lazily resolved back to the RAW conversation JSON and excerpts are injected up to `memory.rag_raw_max_chars` characters in total (default 2500, 0 = unlimited, oversized files are greedy-skipped) — parent-document retrieval: cards act as the search index, the original text carries the facts. `memory.rag_raw_top_k` limits how many top cards get raw (default 1 = only the most relevant card's raw, the rest as summaries; 0/negative = every card). Falls back to card injection when nothing can be resolved |
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
| `summary` | TEXT | Factual summary. Bullet points and multiple lines allowed. Preserves the source's explicit 5W1H; relative time expressions are converted to absolute dates based on conversation timestamps |
| `episode` | TEXT | Episode details (the AI's impression; multiple sentences OK if concise) |
| `ai_importance` | REAL | Importance to AI (0-1) |
| `humanity_importance` | REAL | Importance to humanity (0-1) |
| `embedding_blob` | BLOB | float32 byte array for cosine similarity search |
| `source_date` | TEXT | Date of the source conversation (YYYY-MM-DD). Search time decay is computed from this "event age" (older cards without it fall back to `created_at`) |
| `source_files` | TEXT | JSON array of the RAW file names that directly support the card's primary memory unit; pointer back to the original conversations under `memory_archive/2_knowledgeized/{date}/`, used to resolve the RAG raw-excerpt injection when `rag_source_mode` is raw/both. Stage 2 asks the extraction model to name each card's own sources and keeps only names that actually exist in the chunk (hallucinated or unidentifiable ones fall back to the whole chunk). The narrower the per-card sources, the smaller the injected raw excerpt |
| `content_hash` | TEXT | SHA-256 over the normalized semantic content passed to the Stage 3 prompt (title/summary/episode/tags/category/source_date) — the card body's **version identifier**. Every body-writing path (Stage 2 INSERT / `update_card` / `register_knowledge`) must update it via the shared helper (`butly_core/core/card_content.py`) |
| `last_matured_content_hash` | TEXT | The version Stage 3 last **successfully reviewed**. The card is in the review queue while this is NULL or differs from `content_hash` |
| `maturation_queued_at` | TEXT | Fixed-length UTC time (`YYYY-MM-DDTHH:MM:SSZ`) when the current version entered the queue; the FIFO ordering key |
| `last_matured_at` / `last_matured_run_id` | TEXT | Last success time and run id (audit only; never used to decide re-review) |

---

### 8. memory_nodes (Stage 3 / Knowledge Maturation, opt-in)

While Stages 1/2 accumulate episodes, Stage 3 distills **current interpretations
(memory_nodes)** from the card population. Off by default
(`memory.knowledge_maturation_enabled=False` and
`sleeptime.update_targets.knowledge_maturation=False`).

| Item | Details |
|---|---|
| **Tables** | `memory_nodes` (kind/subject/topic/statement/confidence/status/last_decay_at), `memory_node_sources` (node↔card supports/contradicts/context links), `memory_maturation_runs` (run log), `memory_maturation_run_cards` (card versions fed to a run and their outcome) |
| **Review queue** | Content-hash based. Non-archived cards whose `last_matured_content_hash` is NULL or differs from `content_hash` are queued. FIFO by `maturation_queued_at` guarantees full coverage (usage only breaks ties within the same queue time). Body edits re-queue the card as a new version automatically |
| **Run flow** | Per-instance process lock (non-blocking flock) → recover orphan `running` runs as `abandoned` → preflight (self-heal NULL hashes; fail the run if impossible) → select a batch → LLM (`stage3_node_review` prompt) → outcome classification → commit node/source updates, run counters, card-version stamps, and run completion in a **single SQLite transaction** |
| **Outcome classes** | `ok` / `no_changes` (legitimate empty result; stamped) / `truncated_response` (provider finish_reason) / `empty_response` / `parse_error` / `provider_error` (not stamped; stays queued). Retryable failures get a finite retry → batch halving → single-card isolation; extra LLM calls are capped by `knowledge_maturation_retry_max_calls_per_run` |
| **Concurrent-edit guard** | All card `content_hash` values are re-verified at transaction start; if any changed, the whole batch is dropped as `changed_during_run` and the new versions stay queued |
| **Bootstrap** | `venv/bin/python sleeptime.py stage3-bootstrap --instance <name> [--max-cards N]`. Repeats batches until the queue drains (safety cap `knowledge_maturation_bootstrap_max_cards`=2000). Failed cards are isolated only within the invocation; reports `partial` with the failure list and remaining count. Idempotent and resumable at transaction granularity |
| **Reflection (decay)** | With `memory_node_decay_enabled=True`, a SQL sweep runs at the end of each run: confidence decays by unapplied stale periods (units of `memory_node_stale_days`=30) × `memory_node_decay_per_period`=0.05, anchored on `last_reinforced_at`/`last_decay_at` so repeated runs in the same period never double-decay. Active nodes falling below `memory_node_active_threshold` demote to uncertain; uncertain nodes neglected for 2+ periods get `metadata.stale=true` (never deleted) |
| **Promotion proposals** | Nodes that are active ∧ confidence≥`memory_node_promotion_threshold` ∧ supports≥`memory_node_promotion_min_sources` ∧ span multiple days (preferring `source_date`) are written to `memory_node_proposals.json` (all eligible nodes, paginated). Automatic Key Memory application is not implemented (plan Phase 6) |
| **Chat/QA injection** | With `knowledge_maturation_enabled=True`, up to 5 `status='active'` nodes linked to RAG-hit cards ride along (no card hit → no nodes). Each line renders as `- [subject \| topic] statement (conf=…)`, and `note_active_nodes` only identifies the section as consolidated knowledge and describes the line format. Trace and `debug_info.rag.active_nodes` preserve the lookup reason, candidates, linked card IDs, subject/topic, and whether each rendered fragment was present in the final Provider prompt |
| **Config keys** | `knowledge_maturation_batch_size`=40 / `_max_batches_per_run`=1 / `_prompt_max_chars`=40000 / `_retry_max_calls_per_run`=8 / `_bootstrap_max_cards`=2000. A legacy `max_cards` left in instance config is read as batch_size; legacy `window_days`/`min_usage_count`/`interval_days` are retired |

---

## Sleeptime Execution Flow (Detailed)

The Sleeptime runs via manual trigger or scheduled execution, processing each instance in sequence.
Normal operation keeps using the project root. Isolated runs can pass
`ButlySleeptime(base_dir=..., instances_dir=...)`; Stages 1-3, database backups,
and person-appearance statistics all use the injected paths.

```
ButlySleeptime.run()
  ↓
  process_instance(instance_path)  ← called for each instance
    ├── stage_1_cleanup(instance_path)
    │     ├── [Stage 0] Flush: short_term_json/* → 1_integrated/
    │     ├── [Step 1] Format 1_integrated JSONs to text
    │     ├── [Step 2] Delete session_digests/* (clear temporary context)
    │     ├── [Step 3] Append to mid_term.txt (archive overflow to 3_log/)
    │     ├── [Step 4] _generate_daily_digest() → incremental update mid_term_digest.txt
    │     │          └── ★ Chunks by date headers when digest_max_input_chars is exceeded
    │     ├── [Step 5] _generate_recent_headlines() → recent_digest_headlines.json (up to 4 headlines)
    │     └── [Step 6] _update_relationship_if_due() → mid_term_relationship.txt (7-day interval)
    │
    ├── stage_2_knowledgeize(instance_path, db_type)
    │     ├── ★ skip_knowledge_generation check (skip if true)
    │     ├── Group 1_integrated JSONs by date
    │     ├── Chunk by file boundary (controlled by knowledge_max_input_chars)
    │     ├── Per chunk: ask_gemini_to_summarize() → generate knowledge_cards
    │     ├── Generate embedding + content_hash per card → INSERT into butly_memory.db
    │     └── Move processed JSONs → 2_knowledgeized/{date}/
    │
    └── stage_3_mature_knowledge(instance_path)  ← opt-in (see §8; off by default)
          ├── process lock → recover abandoned runs → preflight backfill
          ├── FIFO batch selection → LLM review → outcome classification
          ├── single transaction: nodes/sources/version stamps/run completion
          ├── (opt-in) staleness decay sweep
          └── write memory_node_proposals.json
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
       SESSION DIGEST     ← session_digests/*.txt
       TIER INFO            ← tier string
       SHORT TERM           ← short_term_json/*.json (last 6 turns)
4. Brain.generate() produces response
5. memory.save_single_turn() → saves JSON to short_term_json/
6. memory.maintain_memory() → checks short_term_limit
       If exceeded: summarize old files via LLM → session_digests/ + 1_integrated/
```

---

## Archive Directory Structure

```
instances/{name}/
├── short_term_json/           # ① active raw turn logs
├── session_digests/        # ② compressed overflow session digests (from short-term overflow)
├── session_digest.txt       # ② legacy file (backward compatibility)
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
| Conversation summarization (session_digest) | `AI_CONFIG["summary"]["model_name"]` | Session digest on short-term overflow |
| Fact digest generation | `AI_CONFIG["summary"]["model_name"]` | Generate mid_term_digest |
| Relationship snapshot | `AI_CONFIG["knowledge"]["model_name"]` | Generate mid_term_relationship |
| Recent headlines extraction | `AI_CONFIG["summary"]["model_name"]` | Generate recent_digest_headlines.json |
| Knowledge card extraction | `AI_CONFIG["knowledge"]["model_name"]` | Stage 2 RAG DB extraction |
| Embedding vector generation | `AI_CONFIG["embedding"]["model_name"]` | knowledge_cards.embedding_blob |

---

## Embedding profiles (per-model input conventions)

Most retrieval embedding models require different prefixes on the query side and the
document side. Omitting them collapses every vector into one cone and destroys cosine
discrimination (measured: with nomic and no prefixes, **card-to-card cosine averaged
0.756 while question-to-correct-card averaged 0.733** — unrelated cards were closer to
each other than a question was to its own answer, so ranking could not work).

`butly_core/llm/embedding_profiles.py` resolves the convention from the model name.

| Profile | Query side | Document side | Models |
|---|---|---|---|
| `nomic` | `search_query: ` | `search_document: ` | nomic-embed-text v1/v1.5 |
| `e5` | `query: ` | `passage: ` | multilingual-e5-* and other E5 models |
| `bge-instruct` | instruction | none | bge-large/base/small-en |
| `qwen3-embedding` | instruction | none | Qwen3-Embedding |
| `bge-m3` / `gemini` / `openai` / `mxbai` | none | none | models that need no prefix |
| `plain` | none | none | unknown models (fallback) |

**Resolution order** (`resolve_profile`):

1. explicit `embedding.query_prefix` / `embedding.document_prefix` (escape hatch for unknown models)
2. explicit `embedding.profile` (profile id; `"plain"` disables prefixes)
3. `embedding.profile` absent or `"auto"` → inferred from `model_name`
4. otherwise `plain`

Example (works in instance config and eval profiles alike):

```json
"embedding": {
  "connection": "local_embedding",
  "model_name": "nomic-embed-text",
  "profile": "auto"
}
```

**Protection when swapping models**: whoever writes embeddings records `model_name` /
`profile` / `dim` into the single-row `embedding_meta` table inside the instance DB. The
startup check (`embedding_check.log_startup_check`) compares it against the current config
and warns on any difference. **Matching dimensions are not enough** — changing the
convention (e.g. nomic without prefixes → with prefixes) produces a different space. After
swapping, run `python migrate_embeddings.py --all` to re-embed.
| Tier classification | `AI_CONFIG["gatekeeper"]["model_name"]` | ContextClassifier 3-score output |
