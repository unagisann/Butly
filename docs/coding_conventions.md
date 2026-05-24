# Coding Conventions

🌐 [日本語](coding_conventions.ja.md) | **English**

> Lightweight, evolving conventions for the Butly codebase. Solo project — these are notes-to-self, not hard rules, but follow them in new code unless there is a reason not to.

## Exception handling

### Rule of thumb
- **Do not silently swallow exceptions.** A bare `except Exception: pass` (or `except: pass`) hides bugs and makes regressions un-diagnosable.
- **Catch narrowly.** Prefer `except FileNotFoundError`, `except json.JSONDecodeError`, `except yaml.YAMLError`, etc. over `except Exception`.
- **If you really need a broad catch** (e.g. third-party SDK with unstable exception hierarchy, or background-task isolation), log the traceback via `logger.exception(...)` or `print(...)` — never `pass`.
- **Never catch `BaseException`** unless you immediately re-raise (it includes `KeyboardInterrupt` / `SystemExit`).

### Allowed broad-catch sites
- Background daemons / watcher threads where a single bad iteration must not kill the loop (see `sleeptime.py`, `main.py:_watch_parent`).
- Fallback config loading where a corrupted JSON file should not crash startup — but the failure **must** be logged.
- Debug logging / telemetry where the save itself failing should not affect the response (see `ChatService` debug_logs).

### Existing code
- ~168 `except Exception:` sites exist as of 2026-05. No mass migration planned — fix opportunistically when touching the surrounding code.

## File writes

### Rule of thumb
- **Any file that gets rewritten in-place must use `butly_core.io_utils.atomic_write_text` (or `atomic_write_bytes`).** A crash mid-write must never leave the original file truncated or empty.
- One-shot writes during fresh instance creation (`InstanceManager.create_instance`) are exempt — failure means "no instance" which is recoverable by retry.
- Debug / transient logs (e.g. `ChatService` `latest.json`) are exempt by judgment — they are rolling, redundant, and reconstructable.

### Atomic-required callsites (as of 2026-05)
- `butly_core/core/memory.py` — glossary, session-turn JSON, floating summaries
- `butly_core/core/key_memory.py` — `Key_Memory.yaml`, proposals JSON
- `butly_core/core/gatekeeper/session_state.py` — `session_state.json` (written every turn)
- `butly_core/core/instance_manager.py` — `config.json` updates, `system_instruction.txt` updates, rename-time fan-out
- `butly_core/search/usage_tracker.py` — `search_usage.json`

## Type hints

- Public function signatures in `butly_core/` should carry type hints. Internal helpers may skip them when the body makes the type obvious.
- No `mypy` in CI yet — type hints are documentation, not enforcement.
- `Optional[T]` over `T | None` for now (most of the codebase still uses the former).

## Config access

- New code should not directly import `butly_core.config.AI_CONFIG` / `SYSTEM_CONFIG`. They remain for Phase 1 compatibility, but new or touched code should read settings through `butly_core.settings.get_settings()`.
- Tests that need config overrides should use `override_settings()` or `get_settings.cache_clear()` / `clear_settings_cache()`. Direct mutation of legacy globals is kept only for existing compatibility tests until the migration reaches those callsites.

## Comments

- Default: no comments. Names should explain the *what*.
- Write a comment when the *why* is non-obvious: a workaround, a subtle invariant, a known footgun, a date-bounded hack.
- Do not write comments that narrate the implementation ("loop through items, summing each"). The code already says that.

## Logging vs print

- `print("[Module] ...")` is acceptable for top-level startup / shutdown / one-shot events.
- Use Python `logging` for anything inside a hot path or that benefits from level filtering (per-turn diagnostics, gatekeeper traces, provider calls).
- New modules should prefer `logging` from the start; old `print`-based modules can stay until touched.
