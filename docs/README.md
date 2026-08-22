# Butly Documentation

[日本語](README.ja.md) | **English**

Documents are grouped by purpose:

| Directory | Contents |
|---|---|
| `guides/` | Setup and operating guides |
| `reference/` | **Current architecture and feature specifications (source of truth)** |
| `history/` | Evaluation reports and long-running experiment records |
| `planning/` | Active and archived plans |
| `Old/` | Frozen documents: superseded former sources of truth |

## Source-of-truth order

**Current code → tests (`tests/`) → `reference/` / `guides/` → `planning/archived/` → `Old/`**

`planning/archived/` and `Old/` record design history and may conflict with later safety
requirements and implementation details.

---

## Setup Guides

- [Desktop UI Startup (normal / development / browser)](guides/desktop_dev_setup.md)
- [Discord Integration Setup](guides/discord_integration_setup.md)
- [LINE Integration Setup](guides/line_integration_setup.md)

## Architecture And Reference

**Overview**
- [Architecture Diagrams](reference/DIAGRAMS.md) — Mermaid diagrams of the main flows
- [File Structure](reference/FILE_STRUCTURE.md) — per-module responsibilities
- [Coding Conventions](reference/coding_conventions.md)

**Memory and context**
- [Memory Lifecycle](reference/memory_lifecycle.md) — persistence, promotion, and overflow per layer
- [Gatekeeper I/O Specification](reference/gatekeeper_io_summary.md) — tier classification and the two-stage RAG decision
- [Context Levels](reference/context_levels.md) — verbosity presets for prompt blocks

**Configuration and LLM**
- [Configuration Layer](reference/configuration.md) — settings resolution order, `user_config.json`, instance `config.json`
- [LLM Connections and API-key management](reference/llm_connections.md) — connections, capability resolution, secrets

**Frontend**
- [Desktop sidecar specification](reference/desktop_sidecar.md) — startup sequence, auth, packaging
- [Official Desktop Chat UI](reference/frontend_chat.md) — screens, chat API, Trace Graph

**Evaluation**
- [LoCoMo Evaluation Web Console](reference/evaluation_web_console.md) — running, cancelling, comparing
- [LoCoMo Evaluation Data and QA Flow](reference/locomo_evaluation_flow.md) — workspace isolation and scoring

## Evaluation Reports

- [RAG evaluation and improvement report (Japanese)](history/rag_evaluation_report.ja.md)
- [Evaluation datasets](history/rag_evaluation_data/) — CSVs for LoCoMo runs, dialogue A/B, and retrieval comparisons

## Planning

- [Planning Index](planning/README.md)
- Active plans belong in `planning/active/`; completed plans with lasting design value
  belong in `planning/archived/`.

## Frozen Documents

- [About Old](Old/README.md) — what was frozen and why

## Change History

The hand-written changelog is retired. **`git log` is the change history**
(Conventional Commits: `feat:` `fix:` `refactor:` `docs:` `chore:` `ci:` `style:`).
