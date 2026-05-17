# Project Status: Butly AI Agent Platform

🌐 [日本語](project_status.ja.md) | **English**

> Last updated: 2026-05-17

## Overview
Butly is a personal AI assistant platform with a layered memory system (short-term / floating summary / mid-term digest + relationship / long-term vector DB / glossary / key memory). It runs on FastAPI backend + Streamlit UI, supports SSE streaming responses, multi-provider LLMs (Gemini / OpenAI / xAI / Ollama), tier + RAG control via Gatekeeper, and Sleeptime batch memory consolidation.

## Architecture
- **Frontend:** Streamlit (`app.py`) — web UI, chat, DB browser, Sleeptime management, streaming toggle.
- **Backend:** FastAPI (`main.py`) — REST `/chat`, SSE `/chat/stream`, WebSocket `/ws`, settings endpoints.
- **Database:** SQLite (`butly_memory.db`) — per-instance knowledge cards + embeddings.
- **LLM engine:** multi-provider (Google Gemini / OpenAI / Ollama / xAI)
  - `butly_core/llm/base.py`: abstract base (`generate`, `summarize`, `embed`, `classify`, `async_generate_stream`)
  - `butly_core/llm/factory.py`: model-name-prefix routing
  - `butly_core/llm/_openai_compat.py`: shared helper for OpenAI/Ollama/xAI
  - `butly_core/llm/providers/{gemini,openai,ollama,xai}.py`: provider implementations
- **Core package:** `butly_core/`
  - `chat/service.py`: chat orchestrator with two paths — `execute()` (buffered) and `execute_stream()` (SSE).
  - `core/gatekeeper/`: ContextClassifier (tier + need_intent) / MemoryProbe (fact-check) / StateUpdater (parallel) / MemoryBlockBuilder.
  - `core/memory.py`: file-based layered memory I/O; floating summary uses relative-time headers.
  - `core/brain.py`: RAG engine (`quick_vector_search_diag` / `search_knowledge` / time-decay).
  - `core/key_memory.py`: structured Key_Memory utilities.
  - `sleeptime.py`: daily / weekly batch memory consolidation.
- **Search module:** `butly_core/search/` — Tavily / Ollama Cloud Web Search auto-routing.

## Current Phase & Status

- **Relative-time floating summary (2026-05-17)**: `ButlyMemory.get_floating_summary()` strips filenames and absolute timestamps in favor of relative headers like "about 30 minutes ago". Sleeptime cleans the directory daily, so the in-flight span stays sub-half-day. Legacy `Time: 2026-...` lines are removed for backward compatibility.

- **Looser RAG vector threshold + per-layer diagnostics (2026-05-17)**: `vector_search_threshold` relaxed from 0.6 → 0.4 (the effective post-decay value is what we want to gate on); `time_decay_rate` lowered so older cards remain reachable. MemoryProbe now returns per-layer diagnostics (executed flag, hit count, reason) surfaced via `debug_info.gatekeeper.memory_probe_layers`.

- **Glossary scan ungated from need_intent (2026-05-17)**: Glossary matching is regex-only (~ms), so it now runs every turn regardless of `need_intent`. This stabilizes proper-noun / alias recognition by pre-injecting semantic-memory entries.

- **Streaming Stage 1+2: SSE endpoint + Streamlit UI (#43, 2026-05)**: Added `POST /chat/stream`. `ChatService.execute_stream()` calls each provider's `async_generate_stream()` and emits SSE events in order `metadata` → `chunk` → `done`. The Streamlit chat header always exposes a streaming toggle. All four providers (Gemini / OpenAI / Ollama / xAI) support streaming.

- **ChatService debug_info auto-save (2026-05)**: Saves debug payload to `instance_dir/debug_logs/latest.json` + rolling `history/{ts}.json` (max 20). Includes timing, token estimate, gatekeeper, RAG, and full prompt. Save failures never affect the response.

- **Configurable tier thresholds + parallel StateUpdater (2026-05)**: `SYSTEM_CONFIG["gatekeeper"]["tier_rc_threshold"]` (default 0.4) / `tier_cn_threshold` (default 0.3), overridable per instance. StateUpdater runs in parallel with response generation, off the critical path (with a one-turn lag carried into the next context).

- **MemoryProbe gated by need_intent (#42, 2026-04)**: ContextClassifier emits `need_intent` (`past_fact` / `glossary` / `relationship` / `null`); MemoryProbe handles fact-check. `need` is set only when both LLM intent and fact-check agree. Glossary scan is the sole exception that runs unconditionally (see above).

- **Lorebook (Glossary extension, #24, 2026-04)**: Glossary extended into a Lorebook format with term / aliases / category / status / priority. `_match_glossary` scans `user_input` plus the most recent `scan_depth` turns with `scan_target` (`user` / `assistant` / `both`). MemoryBlockBuilder partitions hits into short-definition vs related-setting buckets, capped by `max_entries` / `max_chars`.

- **Cortex retirement → 2-tier Gatekeeper (2026-04)**: Tier collapsed to `reflex` / `mid`. RAG is decided independently by `need`, not tier. Legacy modules (`tier_classifier.py`, `search_planner.py`, `memory_judge.py`) cleaned up.

- **Provider Refactoring v3.1: xAI + shared compat (2026-04-19)**: Extracted shared OpenAI-compat helpers (`_openai_compat.py`) used by OpenAI / Ollama / xAI. Added xAI (Grok) provider, Ollama Cloud Web Search provider, per-provider UsageTracker counts. Fixed `ChatService` model-name priority (instance > request > global). `convert_history` maps `role: "model"` → `"assistant"`. 459 tests pass (+68, 0 regressions).

- **Sleeptime resource tuning (2026-04-04)**: Robustness for local LLMs and long-context APIs. Stage 2 skip (`skip_knowledge_generation`), date-header-aware Stage 1 Digest chunking (`digest_max_input_chars`), file-boundary Stage 2 Knowledge chunking (`knowledge_max_input_chars`).

- **General-purpose web search module (2026-03-31)**: Tavily / Ollama Cloud Web Search usable from non-Gemini providers. Pluggable design under `butly_core/search/`. `ChatService` injects via `memory_blocks["web_search_context"]`.

- **Multi-provider refactor (2026-03-22)**: Gemini-only architecture rewritten provider-agnostic. `google.genai` is isolated to `GeminiProvider`. `migrate_embeddings.py` regenerates vectors when switching.

- **Multi-instance**: Each instance lives in `butly_core/instances/[name]` with its own DB, configs, and prompts.

## Sync / Async Design
- All provider methods (`generate` / `summarize` / `embed` / `classify`) are sync (`def`). `ChatService` runs them via `run_in_threadpool()` so the FastAPI event loop never blocks.
- Streaming uses a separate path: `async_generate_stream(text, attachments, context)` yields `{"type": "chunk", ...}` events from inside each provider.
- For gradual async migration, `BaseProvider` ships default `async_generate()` / `async_summarize()` / `async_embed()` implementations that wrap the sync versions in `run_in_threadpool`. Override per provider when ready.

## Notes for Other AI Agents
- **Config precedence**: `butly_core/config.py` (global) → `user_config.json` (user) → `instances/[name]/config.json` (instance).
- **Model selection precedence**: instance config > request `model_name` > global `AI_CONFIG`.
- **Memory-block construction**: routed through Gatekeeper. `MemoryBlockBuilder.build_system_instruction_from_blocks()` builds the immutable half; `build_context_prefix()` builds the variable half. The variable half is compressible via `context_levels` presets (`normal` / `compact` / `low` / `custom`) with per-section `high` / `low` / `off`.
- **Background work**: `sleeptime.py` is heavy and runs via API endpoint on a separate thread.
- **Provider abstraction**: ChatService → ProviderFactory → provider. OpenAI / Ollama / xAI share `_openai_compat.py`; Gemini keeps its own logic (`google.genai`).
- **Gatekeeper flow**: ContextClassifier (LLM, ~1 s) and MemoryProbe (~100 ms) run serially → MemoryBlockBuilder → response generation runs in parallel with StateUpdater (LLM, ~1 s).
