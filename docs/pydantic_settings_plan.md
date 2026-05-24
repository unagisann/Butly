# Config Unification Plan: pydantic-settings

🌐 [日本語](pydantic_settings_plan.ja.md) | **English**

> Filed: 2026-05-24 / Cadence: incremental, no rush / Audience: solo dev (breaking changes OK)

## 1. Motivation

The current config story stacks five layers by hand, with these specific pains:

1. **No type safety** — `AI_CONFIG["chat"]["generation_config"]["temperature"]` typos either raise `KeyError` at runtime or silently return `None`. No editor completion.
2. **`AI_CONFIG` / `SYSTEM_CONFIG` are mutable module globals** — tests rewrite them directly (`cfg_mod.AI_CONFIG["chat"] = ...` in [tests/test_settings_model_candidates.py:62](../tests/test_settings_model_candidates.py#L62)). This is an actual test-isolation hazard: `test_embedding_role_excludes_chat_only` already fails in full-suite runs but passes alone.
3. **Import-time side effects** — [butly_core/config.py:268-289](../butly_core/config.py#L268-L289) loads `user_config.json` at import. Hard to stub per test. To dodge circular imports there are ~40 lazy `from butly_core.config import ...` calls inside functions.
4. **No validation** — broken `safety_settings` in `user_config.json` only surface deep inside provider calls.
5. **Fragile `.env` parser** — [main.py:86-99](../main.py#L86-L99) does `line.partition("=")` by hand; no support for quotes, multiline values, inline comments.
6. **Implicit instance-config schema** — `Jarvis/config.json` carries keys like `cache_ttl_hours`, `use_rag`, `use_context_cache` that are not defined centrally. Hard to know what's authoritative.
7. **`_recursive_update` is silent** — a typo in `user_config.json` gets merged with no warning.

## 2. Goals

- **Single schema** — every config knob defined once in pydantic-settings models. Defaults, types, and validation live in code.
- **Explicit layering** — defaults → `user_config.json` → environment variables → instance config, with a documented precedence that matches the code.
- **Backwards-compatible** — existing `user_config.json`, `.env`, and per-instance `config.json` keep working. Breaking changes are allowed (solo project) but unnecessary churn isn't.
- **Test isolation** — `Settings(ai=...)` produces a fresh instance. No global mutation.
- **Incremental** — no big-bang. **Phase 1 ships a compat shim**, then modules migrate one by one, then legacy globals are deleted.

## 3. Library choice

**pydantic-settings v2** (the dedicated package, split off from pydantic v2 core).

| Candidate | Verdict | Reason |
|---|---|---|
| **pydantic-settings v2** | ✅ Adopt | JSON file source (`JsonConfigSettingsSource`), native `.env`, nested env var support via `env_nested_delimiter`, structured validation errors, seamless with pydantic. De-facto standard. |
| dynaconf | ❌ | Weaker validation than pydantic; Settings is dict-like, no type completion. |
| Hand-rolled pydantic BaseModel + custom loader | ❌ | Reinventing env / json / nested-merge for no gain. |
| dataclasses + ad-hoc validation | ❌ | Validation stays fragile; we'd just be re-stating the problem. |

Dependency: add `pydantic-settings>=2.5,<3` to `requirements.txt`. pydantic itself is already pulled in via FastAPI / Google SDK.

## 4. Schema design

### 4.1 Layout

```
butly_core/settings/
├── __init__.py          # get_settings(), RootSettings re-export
├── root.py              # RootSettings — composite root
├── ai.py                # AIConfig / RoleConfig / GenerationConfig / SafetySetting
├── system.py            # SystemConfig and subsections
├── connections.py       # LLMConnection (user_config.json LLM_CONNECTIONS)
├── instance.py          # InstanceConfig (per-instance config.json)
└── sources.py           # custom SettingsSource for recursive merge
```

### 4.2 Model sketch

```python
# butly_core/settings/ai.py
class GenerationConfig(BaseModel):
    """Provider-shared sampling knobs. Keep loose (extra='allow') because
    semantics vary per provider; tighten later via discriminated union if
    desired."""
    model_config = {"extra": "allow"}

    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    max_output_tokens: Optional[int] = None
    reasoning_effort: Optional[Literal["low", "medium", "high"]] = None


class RoleConfig(BaseModel):
    connection: Optional[str] = None
    model_name: Optional[str] = None
    generation_config: GenerationConfig = Field(default_factory=GenerationConfig)
    safety_settings: list[SafetySetting] = Field(default_factory=list)

    @model_validator(mode="after")
    def _infer_connection(self) -> "RoleConfig":
        """Mirrors the legacy `_normalize_ai_config` — infer connection from
        model_name when the user omitted it."""
        if self.model_name and not self.connection:
            from butly_core.llm.model_registry import infer_connection_id
            inferred = infer_connection_id(self.model_name)
            if inferred:
                self.connection = inferred
        return self


class AIConfig(BaseModel):
    chat: RoleConfig = Field(default_factory=RoleConfig)
    summary: RoleConfig = Field(default_factory=RoleConfig)
    knowledge: RoleConfig = Field(default_factory=RoleConfig)
    embedding: RoleConfig = Field(default_factory=RoleConfig)
    gatekeeper: RoleConfig = Field(default_factory=RoleConfig)
    context_classifier: RoleConfig = Field(default_factory=RoleConfig)
```

`SystemConfig` follows the same shape — one BaseModel per subsection (`MemoryConfig`, `BrainConfig`, `GatekeeperConfig`, `GlossaryConfig`, `BackupConfig`, `SearchConfig`, `MemoryProbeConfig`, `ChatConfig`, `AgentConfig`, `PathsConfig`), composed into one `SystemConfig`.

```python
# butly_core/settings/root.py
class RootSettings(BaseSettings):
    """Top-level Butly settings.

    Load order (low → high precedence):
      1. BaseModel defaults (this file + ai.py + system.py)
      2. user_config.json (project root)
      3. .env / APIkey.env (API keys)
      4. environment variables (BUTLY_AI__CHAT__MODEL_NAME etc.)

    Instance overlays via `Settings.with_instance(name)` at runtime.
    """
    model_config = SettingsConfigDict(
        env_prefix="BUTLY_",
        env_nested_delimiter="__",
        env_file=(".env", "APIkey.env"),
        env_file_encoding="utf-8",
        json_file="user_config.json",
        extra="ignore",  # Tolerate `_AI_CONFIG_*_example` etc. at root.
    )

    ai: AIConfig = Field(default_factory=AIConfig, alias="AI_CONFIG")
    system: SystemConfig = Field(default_factory=SystemConfig, alias="SYSTEM_CONFIG")
    llm_connections: list[LLMConnection] = Field(default_factory=list, alias="LLM_CONNECTIONS")

    @classmethod
    def settings_customise_sources(cls, ...) -> tuple[...]:
        # Custom RecursiveJsonSettingsSource preserves the legacy
        # `_recursive_update` deep-merge semantics.
        ...


@lru_cache(maxsize=1)
def get_settings() -> RootSettings:
    return RootSettings()
```

### 4.3 Preserving merge semantics

The legacy `_recursive_update` is an **unbounded-depth dict merge**:

```python
# Legacy: user_config can override AI_CONFIG.chat.generation_config.temperature alone
{"chat": {"generation_config": {"temperature": 0.5}}}  # top_k, max_output_tokens etc. stay default
```

pydantic-settings' default JSON source does **field-level** overrides (whole-dict replacement). To preserve the existing behavior, write a custom `SettingsSource`. In practice this is small: pydantic v2's source merging plus `default_factory` on nested BaseModels naturally yields the right result for our shape. Implementation should ship with a golden test comparing pre/post merges across the existing `user_config.json` fixtures.

### 4.4 Environment variable naming

```
BUTLY_AI__CHAT__MODEL_NAME=gemini-3.5-flash           # ai.chat.model_name
BUTLY_SYSTEM__MEMORY__SHORT_TERM_LIMIT=8              # system.memory.short_term_limit
BUTLY_AI__CHAT__GENERATION_CONFIG__TEMPERATURE=0.9    # nested
```

API keys stay under their existing names (no `BUTLY_` prefix):

| Existing env var | Handling in pydantic-settings |
|---|---|
| `GOOGLE_API_KEY` / `GEMINI_API_KEY` | Loaded from `.env` into `os.environ`; providers continue to read them directly. |
| `OPENAI_API_KEY` / `XAI_API_KEY` / `OLLAMA_BASE_URL` | Same. |
| `TAVILY_API_KEY` / `OLLAMA_WEB_SEARCH_API_KEY` | Same. |
| `FIRE_TV_IP` / `FIRE_TV_PORT` | Pulled into RootSettings with alias so they're accessible without the `BUTLY_` prefix. |

→ API keys keep flowing through `Connection.api_key_env`; pydantic-settings is only the `.env` loading infra.

## 5. Migration phases

### Phase 0 — Inventory (no code changes, ~½ day)

- [ ] List every config key in use; write `docs/config_inventory.md`.
- [ ] Identify dead keys (the `_AI_CONFIG_*_example` blocks — 4 known instances).
- [ ] Decide what to do with `context_classifier` role ([config.py:79](../butly_core/config.py#L79) is `{}`) — remove or normalize.
- [ ] Scan every instance `config.json` for unknown keys.

### Phase 1 — Add pydantic-settings + compat shim (~1–2 days)

- [ ] `pydantic-settings>=2.5,<3` to `requirements.txt`.
- [ ] Create `butly_core/settings/` package (models only, not used yet).
- [ ] Re-implement `_recursive_update` / `_normalize_ai_config` / `_register_user_connections` on the pydantic side.
- [ ] **Compat shim** — rewrite `butly_core/config.py`:
  ```python
  from butly_core.settings import get_settings
  _settings = get_settings()
  AI_CONFIG = _settings.ai.model_dump(by_alias=False, exclude_none=False)
  SYSTEM_CONFIG = _settings.system.model_dump(by_alias=False, exclude_none=False)
  ```
- [ ] All existing tests pass (we should hold 765 / 766 — the one pre-existing flake stays a known flake).
- [ ] Add tests that the pydantic validator reproduces `_normalize_ai_config` behavior.

**Done = no other file is touched, but tests still pass through the shim.**

### Phase 2 — Migrate call sites to typed access (incremental, no rush)

Recommended order (smallest blast radius → largest):

1. `butly_core/search/usage_tracker.py`
2. `butly_core/core/fire_tv.py`
3. `butly_core/search/__init__.py`
4. `butly_core/core/brain.py`
5. `butly_core/core/gatekeeper/*`
6. `butly_core/chat/service.py`
7. `butly_core/llm/providers/*`
8. `sleeptime.py`
9. `routers/*`
10. `app.py` (last — UI-coupled, high churn)

Per module:
- Import `get_settings()`.
- Replace `AI_CONFIG["chat"]["model_name"]` with `get_settings().ai.chat.model_name`.
- Add a golden test that old-dict and new-typed access return the same value.
- Merge.

### Phase 3 — Type the instance config (~1–2 days)

- [ ] Define `InstanceConfig` in `butly_core/settings/instance.py` mirroring observed schema.
- [ ] Change `InstanceManager.get_instance_config(name)` return type to `InstanceConfig`. Provide `.model_dump()` for legacy callers.
- [ ] Replace `_load_config()` / `_load_config_migrated()` with typed loaders.
- [ ] Load every existing instance `config.json` (e.g. [butly_core/instances/Jarvis/config.json](../butly_core/instances/Jarvis/config.json)) and confirm validation passes.
- [ ] Unknown keys (`cache_ttl_hours`, etc.) accepted via `extra="allow"` with a warning log so we can audit and tighten later.

### Phase 4 — Type LLM_CONNECTIONS (~½ day)

- [ ] Define `LLMConnection` pydantic model.
- [ ] Replace `_register_user_connections` body with pydantic validation.
- [ ] Surface malformed entries at startup with explicit errors.

### Phase 5 — Drop legacy globals (~½–1 day)

- [ ] Remove the `AI_CONFIG = ...` / `SYSTEM_CONFIG = ...` module-level assignments from `butly_core/config.py`.
- [ ] Grep for any remaining references; replace with `get_settings()` access.
- [ ] Rewrite tests that mutate `cfg_mod.AI_CONFIG[...]` to either `monkeypatch.setattr(_settings, ...)` or constructing a fresh `Settings(ai=AIConfig(...))`.
- [ ] Observe whether previously-flaky tests like `test_embedding_role_excludes_chat_only` stabilize.

### Phase 6 — Clean up `.env` loader (~½ day)

- [ ] Delete the hand-rolled parser at [main.py:86-99](../main.py#L86-L99).
- [ ] Let pydantic-settings' `env_file` handle it.
- [ ] Keep the frozen-mode MEIPASS → LOCALAPPDATA bootstrap (cleanest as-is).

## 6. Risks & mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Existing `user_config.json` triggers unexpected validation failure | Server won't start | Phase 1 must include a "load current user_config.json, dump, compare to legacy" golden test |
| Subtle merge-semantics difference between `_recursive_update` and the new source | Hidden defaults override | Write the custom `RecursiveJsonSettingsSource` first, guard with golden tests |
| `connection` auto-inference diverges | Provider resolution shifts | Reuse the existing `_normalize_ai_config` behavioral tests |
| Circular imports re-emerge | Startup failure | Keep `infer_connection_id` import lazy inside the validator |
| Test monkey-patches all need rewriting | Trickle of one-line PRs | Defer to Phase 5; Phases 1–4 dynamically rebuild the `AI_CONFIG` dict so existing patches keep working |
| pydantic v2 vs Google `genai` (pydantic v1 internally) | Likely fine | Run the full test suite after `pip install pydantic-settings` to confirm |
| `app.py` (3424 lines) churn | Big diff | Phase 2 leaves it alone; touch only when the separate "app.py split" plan happens |

## 7. Definition of Done

- [ ] Module-level `AI_CONFIG` / `SYSTEM_CONFIG` assignments gone from `butly_core/config.py`.
- [ ] Every reader uses `from butly_core.settings import get_settings`.
- [ ] A typo in `user_config.json` produces an explicit warning at startup.
- [ ] Tests can pass a `Settings(...)` instance directly (no global mutation needed).
- [ ] `docs/config_inventory.md` enumerates every config key with its location, type, and default.
- [ ] Existing `user_config.json` runs unchanged (intentional breaking changes only).

## 8. Effort estimate (~4–6 days, splittable)

| Phase | Estimate | Parallelizable |
|---|---|---|
| 0. Inventory | 0.5 day | — |
| 1. Shim | 1–2 days | — |
| 2. Migrate (10 modules) | 0.2–0.5 day each | Yes (modules are independent) |
| 3. Instance config typing | 1 day | — |
| 4. LLM_CONNECTIONS | 0.5 day | — |
| 5. Drop legacy | 0.5–1 day | Requires Phase 2 done |
| 6. `.env` cleanup | 0.5 day | — |

## 9. Non-Goals

- **Rebuilding the settings UI** — Streamlit Settings tab stays as-is; only the underlying access changes (Phase 5).
- **Cleaning up unknown instance-config fields** — accept via `extra="allow"`; tightening is a separate task.
- **YAML / TOML migration** — stays JSON. Format change is a separate plan.
- **JSON Schema export** — easy but nobody reads it. Skip.
- **Rewriting every `AI_CONFIG` reference in `app.py`** — handled in the separate `app.py` split plan; until then they read through the shim.

## 10. Pre-flight checklist

- [ ] Confirm the Phase 1 compat shim breaks zero tests via `pytest --co` dry-run before starting code changes.
- [ ] Pin `pydantic-settings>=2.5,<3`.
- [ ] Decide whether to keep existing lazy-import patterns (`from butly_core.config import AI_CONFIG` inside functions) or break the cycle at `root.py` — verify before Phase 2.
- [ ] Make explicit that `app.py` updates are out of scope for this plan (avoid scope creep).
