# Old (Frozen Documents)

[日本語](README.ja.md) | **English**

> ⚠️ **Documents in this directory are frozen. They are not the source of truth.**

This directory holds documents that used to be authoritative but have since been
superseded by other documents or by the code itself. They are snapshots of the
moment they were written and are **not updated**.

## Source-of-truth order

1. Current code
2. Tests (`tests/`)
3. [`docs/reference/`](../reference/) / [`docs/guides/`](../guides/)
4. (reference only) `docs/planning/archived/`
5. (reference only) this directory

## Contents

| Document | Frozen at | Successor |
|---|---|---|
| [project_status.md](project_status.md) / [.ja.md](project_status.ja.md) | 2026-05-24 | root [README.md](../../README.md) and [`docs/reference/`](../reference/) |
| [recent_changes.md](recent_changes.md) / [.ja.md](recent_changes.ja.md) | 2026-05-24 | `git log` (authoritative change history) and [`docs/planning/`](../planning/) |

### Why they were frozen

- **project_status**: describes a Streamlit-only UI with no Stage 3 and no settings
  layer. That conflicts with the current architecture, which includes the Tauri
  desktop frontend, `butly_api`, Stage 3 Knowledge Maturation, and
  `butly_core/settings/`. Its role is covered by the root README and `docs/reference/`.
- **recent_changes**: a hand-written changelog with no entries after 2026-05-24.
  The project moved to Conventional Commits, so `git log` is the change history.

## Why keep them instead of deleting

They remain useful as primary sources for tracing design decisions. Do not cite
them as justification for current implementation behavior.
