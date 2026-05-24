# Recent Changes

🌐 [日本語](recent_changes.ja.md) | **English**

## Model routing & stream turn-counting fix (2026-05-24)

Follow-up to Phase 1–3 LLM refactor. Fixes a stream-path turn counter regression and a few `ProviderFactory.create()` resolution edge cases (instance config / per-request override interaction). Regression coverage added in `tests/test_chat_stream.py` and `tests/test_chatservice_connection_routing.py`.

## Settings: Gemini model list polish (2026-05)

- `model_candidates` endpoint now exposes dynamically discovered Gemini models (the `gemini_native` path was previously missing from discovery).
- `test_connection` displays Gemini model names without the `models/` prefix to match the rest of the UI.

## Phase 3 LLM refactor: UI + Dynamic Discovery + per-request override (2026-05)

- **Settings UI**: model dropdowns are grouped per Connection (built-in + user-defined entries from `user_config.json["LLM_CONNECTIONS"]`).
- **Dynamic discovery**: Gemini model list pulled live from the Google API; non-discoverable connections fall back to `ModelPreset` entries from `model_registry.py`.
- **Per-request override**: `POST /chat` / `POST /chat/stream` honor a per-call `model_name` (instance > request > global, unchanged precedence) and resolve via `ProviderFactory.create(ModelRef)`.

## Phase 2 LLM refactor: AI_CONFIG + ChatService on the ModelRef route (2026-05)

- Every `AI_CONFIG` role entry now carries `connection` + `model_name`.
- `ChatService`, `Brain`, `ContextClassifier`, `StateUpdater`, and `sleeptime` all go through `ProviderFactory.create(ModelRef)` instead of legacy string lookup.
- Legacy string `model_name` still accepted (uses `infer_connection_id()` to backfill the connection).

## Phase 1 LLM refactor: Connection / ModelRef / OpenAICompatAdapter (2026-05)

- **`butly_core/llm/connections.py`** — `Connection` dataclass + `ConnectionRegistry`. Built-in 4 (`openai` / `xai` / `ollama` / `google`); user-defined via `user_config.json["LLM_CONNECTIONS"]` (wired up in Phase 2).
- **`butly_core/llm/model_registry.py`** — `ModelRef` (connection_id + model_name), `ModelPreset` per role, `normalize_model_ref()` accepts str / dict / ModelRef, `infer_connection_id()` keeps legacy prefix routing alive.
- **`butly_core/llm/protocols/`** — `OpenAICompatAdapter` (drives OpenAI / xAI / Ollama / Groq / …) and `GeminiNativeAdapter` (delegates to `providers/gemini.py`).
- **`butly_core/llm/providers/{openai,ollama,xai}.py`** — collapsed into thin shims that pin a `Connection` to its Adapter (`OpenAIProvider(OpenAICompatAdapter)` etc.). `_get_client()` / `_VISION_MODELS` retained as test patch points.
- `ProviderFactory.create(model)` no longer string-routes directly; it normalizes → resolves Connection → instantiates the right Adapter.

## Gemini model name refresh + `usage_count` for knowledge cards (2026-05)

- `AI_CONFIG` Gemini model names refreshed to the current stable IDs.
- New `usage_count` field on `knowledge_cards` — tracks RAG-driven card hits separately from `last_accessed_at` so the actual reach of each card is visible.

## Relative-time Floating Summary header (2026-05-17)

`ButlyMemory.get_floating_summary()` no longer emits filenames or absolute timestamps; each entry is now headed by a relative-time label (e.g., `--- about 30 minutes ago ---`). This stops the LLM from misreading two timestamps as separate conversations.

- New helpers: `_format_relative_time(dt, now)`, `_parse_session_filename_timestamp(name)`, `_strip_legacy_time_line(text)`.
- Legacy files whose first line is `Time: 2026-...` are stripped on read.
- Spans are assumed to be sub-half-day (Sleeptime clears the directory daily).
- The legacy `floating_summary.txt` is still read for backward compatibility.

## Looser RAG vector threshold / decay + per-layer diagnostics (2026-05-17)

- `SYSTEM_CONFIG["memory_probe"]["vector_search_threshold"]`: 0.6 → 0.4 (gating happens on the post-decay effective value).
- `SYSTEM_CONFIG["brain"]["time_decay_rate"]`: 0.005 → 0.003 (half-life ~138d → ~230d).
- `SYSTEM_CONFIG["brain"]["fallback_fetch_limit"]` widened so 3-month-old cards still surface under 0.005 decay.
- `MemoryProbe.probe()` now returns a `layers` dict (`vector` / `glossary` / `deep`) with `executed` / `reason` / `result_count`.
- `ChatService.debug_info.gatekeeper.memory_probe_layers` carries this to the UI and debug log.

## Glossary scan ungated from need_intent (2026-05-17)

`MemoryProbe._match_glossary()` is regex-only and runs in ~ms, so it now runs every turn regardless of `need_intent`. Proper-noun / alias recognition stabilizes and glossary entries are pre-injected as semantic memory. Layer 1 (vector) and Layer 2 (deep search) remain gated by `need_intent`.

## Streaming Stage 1+2 — SSE endpoint + Streamlit UI (#43, 2026-05)

### SSE endpoint
- **New endpoint**: `POST /chat/stream` — returns `text/event-stream`.
- **Event order**: `metadata` (Gatekeeper decision, sent immediately) → `chunk` (incremental) → `done` (debug_info / session_state / sources). Recoverable / non-recoverable `error` events can interrupt at any point.
- **`ChatService.execute_stream()`**: goes through the same Gatekeeper / MemoryBlockBuilder / provider path as the buffered version, then calls each provider's `async_generate_stream()`. StateUpdater runs in parallel and finalizes before `done`.
- **Provider side**: the default `BaseProvider.async_generate_stream()` falls back to executing `generate()` in a threadpool and yielding a single chunk. Gemini / OpenAI / Ollama / xAI override with true streaming.

### Streamlit UI
- A streaming toggle is permanently visible in the chat header.
- When ON, the UI consumes SSE via `requests.post(..., stream=True)` and processes `metadata` / `chunk` / `done` sequentially.
- Switching between streaming and the buffered `POST /chat` is the user's call per turn.

## ChatService debug_info auto-save (2026-05)

Every turn writes debug info to `instance_dir/debug_logs/latest.json` (overwrite) and rotates `history/{YYYYMMDD_HHMMSS_uuid}.json` (default cap: 20). Save failures only emit a warning log; they never affect the response.

Fields:
- `timing`: `gatekeeper_ms` / `memory_build_ms` / `generation_ms` / `state_update_ms` / `total_ms` (`ttfb_ms` on streaming).
- `token_estimate`: heuristic prompt / response counts.
- `gatekeeper`: `tier` / `scores` / `need` / `need_intent` / `memory_probe_status` / `memory_probe_layers` / `session_state`.
- `rag`: `query` / `results` (title, score, episode).
- `prompt` / `prompt_full`: full message array.
- `provider` / `model`.

## Configurable tier thresholds + post-response parallel StateUpdater (2026-05)

- **Tier thresholds**: `SYSTEM_CONFIG["gatekeeper"]["tier_rc_threshold"]` (default 0.4) and `tier_cn_threshold` (default 0.3). Per-instance override via `instance_config["gatekeeper"]`.
- **Parallel StateUpdater**: No longer called inside `Gatekeeper.classify()`. `ChatService` runs it alongside response generation via `asyncio.gather()`, then `session_state.apply_delta()` after the response completes.
- **Compatibility**: The current turn's `topic` reads the previous-turn session_state (one-turn lag, intentional).

## MemoryProbe gated by need_intent (#42, 2026-04)

ContextClassifier added the `need_intent` field (`past_fact` / `glossary` / `relationship` / `null`). MemoryProbe switches layer execution accordingly:

| need_intent | Layer 1 (vector) | Layer 1.5 (glossary) | Layer 2 (deep) |
|---|---|---|---|
| `past_fact` | ✅ | ✅ | conditional |
| `glossary` | ✗ | ✅ | ✗ |
| `relationship` | ✅ | ✅ | conditional |
| `null` | ✗ | ✅ (always-on) | ✗ |

Final `need` is set only when both LLM intent and MemoryProbe fact-check agree. The `asks_for_specific_past_detail()`-based fallback is retained.

## Cortex / legacy Gatekeeper cleanup (#40, 2026-04)

Tier collapsed to `reflex` / `mid` (2 values). Removed `tier_classifier.py`, `search_planner.py`, `memory_judge.py`, `prompts/control/tier_classifier.txt`, etc.

## Lorebook (Glossary extension) (#24, 2026-04)

- **glossary.yaml fields**: term / definition / aliases / category / status (`active`) / priority.
- **`_match_glossary()`**: history scan controlled by `scan_depth` (default 2 turns) and `scan_target` (`user` / `assistant` / `both`). Returns raw hits annotated with `priority` / `_yaml_index` / `match_source`.
- **`_build_glossary()` partitioning**: definitions containing newlines route to the "related setting" section, single-line definitions to the "term explanation" section. `max_entries` / `max_chars` apply with greedy skip.
- **`SYSTEM_CONFIG["glossary"]`**: `scan_depth=2`, `scan_target="both"`, `max_entries=20`, `max_chars=4000`.

## Provider Refactoring v3.1: xAI + shared OpenAI-compat + bug fixes (2026-04-19)

Extracted common OpenAI-compat code, added xAI (Grok) provider, integrated Ollama Cloud Web Search, fixed three critical bugs uncovered during testing.

### `_openai_compat.py` — shared OpenAI-compat helpers
- **New file**: `butly_core/llm/_openai_compat.py`. Consolidates duplicated code from OpenAI / Ollama / xAI:
  - `load_env_file()`: read APIkey.env / .env.
  - `is_reasoning_model()`: detect OpenAI o1/o3/o4 (no temperature, uses `max_completion_tokens`).
  - `resolve_position()` / `resolve_system_instruction()` / `resolve_context_prefix()`: locate system_instruction placement.
  - `build_user_content()`: convert text + images to OpenAI format.
  - `convert_history()`: convert Butly history to OpenAI messages (incl. `role: "model"` → `"assistant"` mapping).
  - `build_messages()`: order system / context / history / user according to position.
  - `merge_chat_config()` / `build_chat_completion_kwargs()` / `build_chat_response()`: API call parameter construction.
- **OpenAI / Ollama providers**: `generate()` now delegates to `_openai_compat`. Removed private `_build_system_instruction()` / `_build_user_content()`.

### xAI (Grok) provider
- **New file**: `butly_core/llm/providers/xai.py`. Uses OpenAI SDK with `base_url="https://api.x.ai/v1"`.
- **Vision**: supported on grok-4 family; grok-code-fast is text-only.
- **Embedding**: xAI does not expose an embedding API — returns `None`.
- **factory.py**: routes `grok-*` / `xai/*` to `XaiProvider`.

### Ollama Cloud Web Search
- **New file**: `butly_core/search/ollama_provider.py`. Uses `https://ollama.com/api/web_search` authenticated with `OLLAMA_WEB_SEARCH_API_KEY`.
- **`search/__init__.py`**: `create_search_provider(chat_model="")` — Ollama chat + key → OllamaWebSearchProvider, else TavilySearchProvider.

### UsageTracker per-provider counts
- Old `{YYYY-MM: int}` → new `{YYYY-MM: {tavily: N, ollama: M}}` with lazy migration. `increment(provider)` is now explicit.

### Bug fixes (found during xAI testing)
1. **ChatService model_name priority** (`chat/service.py`): `request.model_name or AI_CONFIG["chat"]["model_name"]` always picked the global Gemini default. Fixed to a 3-stage priority: instance config → request → global.
2. **convert_history role mapping** (`_openai_compat.py`): Gemini uses `role: "model"` but OpenAI/xAI require `role: "assistant"`. Added `_ROLE_MAP = {"model": "assistant"}`.
3. **SleepTime instance config support** (`sleeptime.py`): added `_resolve_conf()` helper; 6 methods now respect per-instance config.

### UI changes (`app.py`)
- Added xAI models (grok-4-1-fast-non-reasoning / grok-4-1-mini-fast-non-reasoning) to the model list.
- API-key management got a 4-column UI (Gemini / OpenAI / xAI / Ollama Web Search).

### Tests
- `tests/test_openai_compat.py` (new, 40): helpers.
- `tests/test_xai_provider.py` (new, 12): xAI provider.
- `tests/test_ollama_web_search.py` (new, 6): Ollama Web Search.
- `tests/test_search_factory.py` (new, 6): factory.
- Total: 459 pass (391 → 459, +68, 0 regressions).

## Gatekeeper Phase 1.5: MemoryJudge → MemoryProbe (fact-based) (2026-04-06)

Removed the MemoryJudge LLM call and replaced it with fact-based judgment grounded in actual search results. Reduces latency and enables selective Glossary injection.

### MemoryProbe 3-layer structure
- **Layer 1: Quick Vector Search (~100 ms)**: added `Brain.quick_vector_search()` — compares user_input embedding to knowledge_cards cosine similarity directly (no keyword extraction). Hits above threshold (default 0.6, later 0.4) go to candidates.
- **Layer 1.5: Glossary Match (few ms)**: matches user_input words to glossary `term` / `aliases`. Hits → `glossary_hits`.
- **Layer 2: Deep Search (1–2 s, conditional)**: only runs when Layer 1 missed AND user_input contains a specific past-reference pattern ("前に", "覚えてる", "だっけ", etc.). Executes `Brain.extract_keywords()` + `search_knowledge()`.

### Gatekeeper Facade changes
- **Parallelism**: 3-way (CC + MemoryJudge + StateUpdater) → 2-way (CC + StateUpdater). MemoryProbe runs serially since it needs no LLM.
- **New arguments**: `Gatekeeper.classify()` now takes `brain` / `memory_manager`, passed in from `chat/service.py`.
- **Return value**: added `memory_probe` dict (`status` / `candidates` / `glossary_hits`).

### MemoryBlockBuilder changes
- **RAG search dropped**: removed `brain.extract_keywords()` + `brain.search_knowledge()` calls inside `build()`. `rag_context` is now built directly from probe candidates.
- **Selective glossary injection**: `_build_glossary()` injects only related entries when `glossary_hits` is non-empty; otherwise falls back to injecting all (later changed to always run — see 2026-05-17 entry).

### Config / file changes
- **config.py**: added `SYSTEM_CONFIG["memory_probe"]` (`vector_search_limit` / `vector_search_threshold` / `deep_search_enabled`).
- **prompt_registry.yaml**: removed the `memory_judge` entry.
- **Deleted**: `memory_judge.py`, `control/memory_judge.txt`, `test_memory_judge.py`.

### Tests
- `tests/test_memory_probe.py` (new, 46 tests): pattern detection, Layer 2 trigger, glossary match, headline match, probe integration, Gatekeeper integration.
- Existing tests included: all 330 pass.
- **Expected latency**: Gatekeeper + MemoryBuild combined ~5 s → ~1.5 s.

## Sleeptime resource tuning: Stage 2 skip + chunking (2026-04-04)

Improves robustness for local LLMs and long-context APIs.

### Stage 2 skip
- **`skip_knowledge_generation`** (bool): added to `config.json > sleeptime`. When `true`, Stage 2 (knowledgeize) is skipped; raw data stays in `1_integrated` for later batch processing with a more capable model.

### Stage 1 (Digest) chunking
- **`digest_max_input_chars`** (int): max chars per LLM call inside `_generate_daily_digest()`. `0` = unlimited.
- Splits on date headers `[YYYY-MM-DD ...]` so a date line never gets cut mid-string.
- New helper `_split_text_by_date_headers()`.

### Stage 2 (Knowledge) chunking
- **`knowledge_max_input_chars`** (int): max chars per LLM call inside `stage_2_knowledgeize()`. `0` = unlimited.
- Splits at JSON file boundary ("adding the next file would exceed the limit → flush this chunk"); never cuts a file mid-content.

### UI (`app.py`)
- Sleeptime settings panel added 3 fields: "Skip knowledgeize" checkbox, "Max Digest input chars", "Max Knowledge input chars".

## General-purpose web search module (2026-03-31)

Enables web search for non-Gemini providers (OpenAI / Ollama).

### New package: `butly_core/search/`
- **base.py**: `BaseSearchProvider` ABC (`search()` / `is_available()`).
- **tavily_provider.py**: Tavily Search API implementation. Authenticated via `TAVILY_API_KEY`.
- **types.py**: `SearchResult` DTO (title / url / content / score).
- **usage_tracker.py**: `UsageTracker` — monthly counts saved to `butly_core/search_usage.json`.
- **__init__.py**: `create_search_provider()` factory.

### ChatService integration
- **service.py**: added `_is_gemini_model()`. For non-Gemini + `use_web_search=True`, runs Tavily search and stores the result in `memory_blocks["web_search_context"]`. Source URLs are appended to `result.sources`.
- **Pattern A** (user toggle decides ON/OFF). Pattern B (LLM decides) is deferred to a separate issue.

### MemoryBuilder
- **memory_builder.py**: `DEFAULT_CONTEXT_ORDER` now includes `web_search`. `_build_web_search()` emits the section only when `web_search_context` is set.
- **section_headers.yaml**: ja/en `web_search` headers with reference-style annotation.

### DTO / Router / UI
- **types.py**: `ChatRequest` adds `use_web_search: bool = False`. `normalize_ws_payload()` carries it.
- **routers/chat.py**: REST `ChatRequest` adds `use_web_search`, passes it to the internal request.
- **app.py**: shows 🔍 toggle on non-Gemini (disabled when `TAVILY_API_KEY` is missing). Includes `use_web_search` in the payload.

### Config / deps
- **config.py**: `SYSTEM_CONFIG["search"]` default (`provider` / `max_results` / `search_depth`).
- **requirements.txt**: `tavily-python>=0.5.0`.
- **.env.example**: `TAVILY_API_KEY` description.

### Tests
- `tests/test_search_types.py`: SearchResult DTO (4).
- `tests/test_tavily_provider.py`: TavilySearchProvider — `is_available` / mocked search / error handling (7).
- `tests/test_usage_tracker.py`: `increment` / `get` / corrupt-file handling (6).
- All 57 pass.

## Glossary (semantic memory) + RAG unification + GK/RAG toggles (2026-04-02)

### Glossary (shared-vocabulary dictionary)
- **glossary.yaml** per instance: `instances/{name}/glossary.yaml`. Fields: term / definition / aliases / category / status.
- **memory.py**: added `get_glossary()` / `get_glossary_raw()` / `save_glossary()`.
- **memory_builder.py**: `_build_glossary()` injected via `build_context_prefix()` for all tiers (after CURRENT TIME, before MID-TERM).
- **section_headers.yaml**: ja/en headers + `note_glossary` annotation.
- **API**: `GET` / `POST /instances/{name}/glossary`.
- **UI**: glossary management in instance settings (filter / add / delete / status / save).

### SearchPlanner `need=null`
- **search_planner.txt**: allow `need: null` / `search_targets: null`. Added an "Important" block explaining the "no search needed" case.
- **search_planner.py**: normalizes LLM "None" / "null" / "" strings to Python `None`.
- **state_updater.py**: same "None" / "null" normalization.
- **memory_builder.py**: skip RAG search when `need` is null (saves cost even on cortex).

### ChatService RAG unification
- **service.py**: removed ChatService's own RAG path (keyword extract + search). RAG is now a single path through MemoryBlockBuilder.
- **Gatekeeper ON/OFF**: `config.gatekeeper.enabled` disables the entire Gatekeeper (defaults to mid tier with no RAG).
- **RAG ON/OFF**: `config.brain.use_rag` disables RAG; even on cortex, `brain` is not passed when `use_rag=False`.
- **UI**: added "🧬 Gatekeeper" and "RAG search" toggles in `app.py`.

### Misc
- Fixed Streamlit checkbox empty-label warnings (`label_visibility="collapsed"`).
- **Tests**: `TestContextPrefixGlossary` (6) added. All 216 pass.

## Custom model name input in model selector (2026-03-29)

- Each role (Chat / Summary / Gatekeeper / Embedding) gains a "✏️ Custom Input..." option.
- Use case: latest / legacy API models, many Ollama local LLMs.
- Ollama guide: reminds to prefix with `ollama/`.
- Empty-string guard added to prevent saving blank model names.

## Provider sync/async unification (2026-03-23)

Resolves `asyncio.run() cannot be called from a running event loop` at the root.

- **`BaseProvider`**: `generate()` / `summarize()` `@abstractmethod` changed from `async def` → `def`.
- **Future-proofing**: `async_generate()` / `async_summarize()` / `async_embed()` default impls in `BaseProvider` wrap the sync versions via `run_in_threadpool`. Migrating any provider to true async is a single override.
- **`GeminiProvider`**: 4 methods (`generate`, `summarize`, `_start_chat`, `_try_search_with_retry`) became sync. Switched `client.aio.chats.create()` → `client.chats.create()`. Removed all `await`.
- **`OpenAIProvider` / `OllamaProvider`**: `generate()` / `summarize()` → sync (no body changes).
- **`ChatService`**: `await provider.generate(...)` → `await run_in_threadpool(provider.generate, ...)`.
- **Cleanup**: removed unused `import asyncio` in `sleeptime.py` (`generate_embedding`) and `migrate_embeddings.py`.
- **Project-wide**: zero usages of `asyncio.run()`. 137 tests pass.

## Multi-provider refactor (2026-03-22)

Reworked Gemini-only architecture into provider-agnostic. OpenAI / Ollama added.

- **BaseProvider extension**: added `summarize()` / `embed()` / `classify()` abstract methods.
- **brain.py rewrite**: removed all `google.genai` dependencies, slimmed to a pure RAG engine on top of ProviderFactory (~861 → ~253 lines).
- **ChatService unification**: RAG search moved to ChatService; all paths go through `Provider.generate()`.
- **GeminiProvider polishing**: absorbed Gemini-specific logic from brain.py (search retry, context cache, hallucination filter).
- **OpenAIProvider**: GPT-4o etc.; vision, `embed` (text-embedding-3-small), classify.
- **OllamaProvider**: local LLM via OpenAI-compat API (`localhost:11434/v1`).
- **Gatekeeper / Sleeptime**: removed `google.genai` dependency; switched to `Provider.classify()` / `embed()`.
- **Embedding migration**: `migrate_embeddings.py` regenerates `embedding_blob` when switching providers.
- **Config**: `.env.example` lists all provider keys; `user_config.json.example` gains OpenAI / Ollama examples.
- **app.py / main.py**: `brain.prepare_cache()` runs through Provider (with hasattr check for non-Gemini).

## Raspi V2 — image chat & responsibility separation (2026-03-21)

- **DTOs**: created `butly_core/chat/types.py` defining `ChatRequest`, `ChatResponse`, `Attachment`. Added input normalization for WebSocket and REST.
- **Provider abstraction**: created `butly_core/llm/` with `GeminiProvider`. Gemini-specific image handling (inline / Files API branching), previously scattered in `main.py` / `brain.py`, is hidden inside the Provider.
- **ChatService**: introduced stateless orchestrator `butly_core/chat/service.py`; removed LLM-dependent code from `main.py`.
- **brain.py cleanup**: image conversion moved out to Provider; `images` argument dropped. Memory injection logic untouched.

## Earlier history (selected)

1. **Phase 4 — dynamic mid-term summary injection toggle**.
2. **Phase 3 — two-layer summary pipeline**: extended `sleeptime.py` to produce "fact digest" + "relationship snapshot".
3. **OSS prep / refactor**: removed hardcoded names like "Jarvis" / personal info; templated dynamically into the generic "Butly" platform.
4. **Stateful API (Interactions API)** *(removed — dropped for multi-provider unification)*: shifted to Gemini's session history.
5. **FastAPI + Streamlit split**: separated API server and frontend for async + background tasks (Sleeptime) stability.
6. **Per-instance memory isolation**: each instance under `butly_core/instances/` has its own DB and files.
