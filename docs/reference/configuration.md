# Configuration Layer

[日本語](configuration.ja.md) | 🌐 **English**

> Last updated: 2026-08-22

Butly's configuration has two tiers: **global settings** shared by every instance, and
**instance settings** that override them per persona. Global settings are consolidating
into `butly_core/settings/`, built on pydantic-settings; the module-level dicts in
`butly_core.config` are a **compatibility shim** until that migration finishes.

---

## 1. Overview

```
butly_core/settings/defaults.py          <- defaults for AI_CONFIG / SYSTEM_CONFIG
        | recursive_update (deep merge per section)
<data_dir>/user_config.json              <- AI_CONFIG / SYSTEM_CONFIG
                                            / LLM_CONNECTIONS / LLM_CAPABILITY_OVERRIDES
        |
get_settings(config_path) -> RootSettings (typed, lru_cache)
        | apply_runtime_settings(data_dir)
butly_core.config.AI_CONFIG / SYSTEM_CONFIG (compatibility shim, updated in place)
+ ConnectionRegistry.reset_to_builtin() -> register user connections
+ configure_capability_runtime(data_dir, overrides)
        | further overridden at runtime
instance config.json (passed to components as `override_config`)
        |
per-request override (`model_name` on `POST /chat` / `/api/v1/chat`, etc.)
```

**Precedence (last wins)**: defaults → `user_config.json` → instance `config.json` →
per-request override.

Environment variables are **not** part of this chain (see §4). API keys are the one
exception, and they arrive through a separate path.

---

## 2. Inside `butly_core/settings/`

| File | Responsibility |
|---|---|
| `defaults.py` | Default values for `AI_CONFIG` / `SYSTEM_CONFIG` (**the primary source for setting values**) |
| `sources.py` | `load_settings_data()`: deepcopy defaults → `recursive_update` from `user_config.json` → `normalize_ai_config()` fills in and sanity-checks `connection` |
| `root.py` | `RootSettings` (a `BaseSettings`), `get_settings()`, `clear_settings_cache()`, `override_settings()` |
| `ai.py` | `AIConfig` / `RoleConfig` / `GenerationConfig` / `SafetySetting` |
| `system.py` | `SystemConfig` (one dict per section) |
| `connections.py` | `LLMConnection`. Validates id, env names, base_url, and extra_headers with field validators |
| `instance.py` | `InstanceConfig`. Currently a permissive placeholder; tightened in a later phase |
| `bootstrap.py` | `apply_runtime_settings(data_dir)`: pushes typed settings into the legacy globals, the ConnectionRegistry, and the capability runtime |

Per the Phase 1 compatibility-first approach, `AIConfig` and `SystemConfig` keep each
role and section as a **plain dict**, so the output matches the legacy dict exactly.
`RoleConfig` and `GenerationConfig` already exist and will be tightened incrementally.

### Reading settings from code

```python
from butly_core.settings import get_settings

settings = get_settings()               # defaults to <project_root>/user_config.json
chat = settings.AI_CONFIG["chat"]       # legacy-compatible dict (deep-copied)
probe = settings.SYSTEM_CONFIG["memory_probe"]
```

The `AI_CONFIG` / `SYSTEM_CONFIG` / `LLM_CONNECTIONS` / `LLM_CAPABILITY_OVERRIDES`
properties all **return deep copies**, so mutating a result never dirties the cache.

### Substituting settings in tests

```python
from butly_core.settings import clear_settings_cache, get_settings, override_settings

# A. Load a different user_config.json
settings = get_settings(tmp_path / "user_config.json")

# B. Temporarily force a prebuilt RootSettings
with override_settings(my_settings):
    ...

# C. Drop the cache after changing files or the environment
clear_settings_cache()          # get_settings.cache_clear() is the same hook
```

Direct mutation of the legacy globals (`butly_core.config.AI_CONFIG`) is reserved for
compatibility with existing tests.

---

## 3. Global settings: `user_config.json`

`user_config.json.example` is the template (**`user_config.json` itself is gitignored**).

| Top-level key | Contents |
|---|---|
| `AI_CONFIG` | Per-role `connection` + `model_name` + `generation_config` + `safety_settings` |
| `SYSTEM_CONFIG` | `agent` / `paths` / `memory` / `brain` / `backup` / `search` / `memory_probe` / `gatekeeper` / `chat` / `glossary` / `trace` |
| `LLM_CONNECTIONS` | Array of user-defined connections (adding OpenAI-compatible providers) |
| `LLM_CAPABILITY_OVERRIDES` | Manual overrides shaped as `{connection_id: {model_id: {...}}}` |
| `LLM_PROVIDERS` | Provider-specific extras (for example `ollama.base_url`) |

### AI_CONFIG roles

| Role | Purpose |
|---|---|
| `chat` | Response generation |
| `summary` | Mid-term digest, relationship snapshot, session digest |
| `knowledge` | Knowledge card generation, Stage 3 Knowledge Maturation |
| `embedding` | Vector embeddings |
| `gatekeeper` | Tier classification, StateUpdater |
| `context_classifier` | Inherits `gatekeeper` when empty |

Omitting `connection` makes `normalize_ai_config()` infer it from the `model_name` prefix
(legacy compatibility). When a built-in connection and the model name disagree, the
inferred value replaces it and a warning is printed.

### Key SYSTEM_CONFIG sections

| Section | Representative keys |
|---|---|
| `agent` | `agent_name` / `user_name` / `locale` |
| `paths` | `db_name` / `system_instruction` / `key_memory` |
| `memory` | `short_term_limit` / `use_summarized_mid_term` / `rag_source_mode` / `rag_raw_max_chars` / `rag_raw_top_k` / `rag_raw_neighbor_radius` / Stage 3's `knowledge_maturation_*` and `memory_node_*` |
| `brain` | `search_mode` / `search_limit` / `time_decay_rate` / `dynamic_threshold` / `readable_instances` / BM25, RRF, and Evidence Fusion parameters |
| `memory_probe` | `retrieval_execution` / `injection_policy` / `vector_search_limit` / `vector_search_threshold` / `deep_search_enabled` |
| `gatekeeper` | `tier_rc_threshold` / `tier_cn_threshold` |
| `chat` | `streaming_enabled` |
| `glossary` | `scan_depth` / `scan_target` / `max_entries` / `max_chars` |
| `search` | `provider` (`tavily` / `ollama`) / `max_results` / `search_depth` |
| `backup` | `generations` / `dir_name` |
| `trace` | `enabled` / `detail` / `hidden_nodes` |

`butly_core/settings/defaults.py` is authoritative for default values. If this document
and `defaults.py` disagree, **`defaults.py` wins**.

---

## 4. Environment variables

### Environment variables cannot override settings (by design)

`RootSettings` has **no environment-variable source**. To change a setting, edit
`user_config.json` or an instance `config.json`.

There are two reasons for this.

1. `get_settings()` passes the result of `load_settings_data()` to
   `RootSettings(**data)` as **init kwargs**. pydantic-settings resolves sources in the
   order `init > env > dotenv`, so with all four fields filled by init, an env source
   could never win anyway.
2. Enabling it would **break things**. Because sections are `dict[str, Any]`, env values
   apply as a **replacement, not a merge**.

   | Env set | Result |
   |---|---|
   | `BUTLY_SYSTEM__brain__search_mode=hybrid` | `brain` drops from **23 keys to 1**; `search_limit`, `time_decay_rate`, `bm25_weights`, `rrf_k`, and the rest vanish |
   | `BUTLY_SYSTEM__brain__search_limit=5` | `'5'` — a **str**, not an int |

Adding env overrides requires typing the sections (Phase 2/3 of the
[pydantic-settings consolidation plan](../planning/active/pydantic_settings_plan.md))
together with a `settings_customise_sources` that preserves merge semantics. Until then,
no declaration that looks like it works is kept in the code.

### API keys (separate path)

API keys (`GOOGLE_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY`,
`TAVILY_API_KEY`, `OLLAMA_WEB_SEARCH_API_KEY`, …) carry no `BUTLY_` prefix and do not go
through the settings chain.

- `main.py:_load_env_from_data_dir()` reads `<data_dir>/.env` at startup and pushes it
  into the environment with `os.environ.setdefault()` (never overwriting existing values)
- Providers (`gemini.py`, `_openai_compat.py`, `brain.py`, `sleeptime.py`) also fall back
  to `load_dotenv()`
- A connection's `api_key_env` / `api_key_fallback_envs` decide which variable names are read

`.env.example` is the template. **Never commit `.env` or `APIkey.env`** — both are gitignored.

### The `BUTLY_*` variables that do work (all read `os.environ` directly)

These bypass the settings chain; each consumer reads `os.environ` itself.

| Variable | Read by | Purpose |
|---|---|---|
| `BUTLY_DESKTOP_TOKEN` | `butly_api/auth.py` / `server.py` | Per-launch Bearer token for the desktop sidecar. Unset means `/api/v1` auth is off (dev / Streamlit) |
| `BUTLY_DEVELOPER_MODE` | `main.py` / `butly_api/server.py` | Developer mode |
| `BUTLY_CHRONOS_NOW` | `butly_core/core/chronos.py` | Pins "now" (for evaluation and tests) |
| `BUTLY_API_URL` | `app.py` | Streamlit → backend URL |
| `BUTLY_DEV_BACKEND_PORT` / `BUTLY_DEV_BACKEND_URL` | `frontend/src-tauri/src/backend.rs` / `vite.config.ts` | Backend target in development mode |
| `BUTLY_EVALUATION_OUTPUT_DIR` / `BUTLY_DIALOGUE_AB_OUTPUT_DIR` / `BUTLY_LOCOMO_DATASET` | `evals/locomo/web_jobs.py` | Evaluation input/output paths |
| `BUTLY_SIDECAR_ONEFILE` | `scripts/build_backend_sidecar.py` | PyInstaller build mode |

**These describe the runtime environment, not configuration.** There is no path for
overriding `AI_CONFIG` / `SYSTEM_CONFIG` values through the environment.

---

## 5. Instance settings: `config.json`

Lives at `butly_core/instances/<name>/config.json` and overrides global settings
**section by section**.

| Section | Contents |
|---|---|
| `agent_profile` | `ai_name`, `locale`, etc. — how the persona identifies itself |
| `user_profile` | `user_name` / `preferred_call` / `birthday`, etc. |
| `brain` | `search_limit` / `default_use_google_search` / `readable_instances` / `use_context_cache` |
| `chat` | Role overrides (`connection` + `model_name`) |
| `memory` | `use_summarized_mid_term`, Stage 3 parameters, etc. |
| `sleeptime` | `max_digest_chars` / `max_relationship_chars` / `relationship_update_interval_days` / `update_targets` |
| `gatekeeper` | `tier_rc_threshold` / `tier_cn_threshold` |
| `context_levels` | Verbosity presets for each prompt block (see [Context Levels](context_levels.md)) |

Instance config reaches components as `override_config` and is deep-merged with global
values by `_merge_config()`.

When `InstanceManager` writes `config.json` or `system_instruction.txt` it uses
`atomic_write_text` (see [Coding Conventions](coding_conventions.md)).

---

## 6. `system_config.json` (legacy UI, separate channel)

UI settings saved by the legacy Streamlit settings screen.
`routers/settings.py` reads and writes `deps.BASE_DIR / "system_config.json"` directly,
and it **is not part of the pydantic settings chain**. It will be reconciled once the
migration to the official desktop UI completes.

---

## 7. Handling secrets

The following are gitignored. **Never commit them, and never paste their contents into
output.**

```
.env  APIkey.env  *.env
user_config.json  user_prompts.json  system_config.json
external_accounts.json  persons.json
llm_capabilities.json          <- observed capability cache (under <data_dir>)
*.db  butly_core/instances/
```

Refer to and update the `*.example` templates instead:
`.env.example`, `user_config.json.example`, `persons.json.example`.

API keys can be saved from the Web UI, and **stored secrets are never displayed again**
(see [LLM Connections and API-key management](llm_connections.md)).

---

## 8. Related documents

- [LLM Connections and API-key management](llm_connections.md) — connections and capability resolution in detail
- [Context Levels](context_levels.md) — verbosity presets for prompt blocks
- [Memory Lifecycle](memory_lifecycle.md) — where the `memory` section takes effect
- [File Structure](FILE_STRUCTURE.md) — module responsibilities
