# Planning Documents

[日本語](README.ja.md) | **English**

> ⚠️ Plans are **not the source of truth for current behavior**.
> Order of authority: current code → tests → [`docs/reference/`](../reference/) → plans.

## Active (`active/`)

| Plan | Status | Remaining |
|---|---|---|
| [Official Frontend Migration Plan](active/frontend_migration_plan.ja.md) (ja) | Phase 2 implemented (2026-08-12) | Phase 3 (onboarding / instance basic settings). Phase 1's Windows installer and first CI verification are also outstanding |
| [Stage 3 Knowledge Maturation Plan](active/stage3_knowledge_maturation_plan.ja.md) (ja) | Phases 0–5 implemented (2026-07-21) | Phase 5's live LoCoMo A/B run, Phase 6 (automatic Key Memory promotion), Phase 7 (independent node retrieval), Phase 8 (cleanup), proposal approval API |
| [Retrieval Overhaul Plan (hybrid search / RRF)](active/retrieval_hybrid_search_plan.ja.md) (ja) | Phase 1 + Japanese dialogue A/B complete (2026-08-09) | **Hybrid was rejected (default stays `vector`); always-on retrieval was adopted.** Measure `dual_query` offline recall and rescue/harm, then decide |
| [Config Unification Plan: pydantic-settings](active/pydantic_settings_plan.md) | Phase 1 compatibility shim implemented | Phase 2 onward (per-module typed access, removing the legacy globals, making env overrides effective) |
| [Group Context Lanes Plan](active/group_context_lanes_plan.ja.md) (ja) | Phase 1 implemented (2026-07-08) | Phase 2 onward not started (deferred behind frontend groundwork and observability) |
| [LoCoMo Long-Term Memory Evaluation Plan](active/locomo_evaluation_plan.ja.md) (ja) | Phases 1–4 complete | Phase 5 (real-data trial) only |
| [Memory Store Normalization Plan](active/memory_store_normalization_plan.ja.md) (ja) | Not started (proposal) | All phases |

## Archived (`archived/`)

Retained for design history.

- [LLM Connection Refinement Plan](archived/llm_connection_refinement_plan.ja.md) (ja) — design of the Canonical Request and Capability Resolver
- [External Chat Preflight Plan](archived/external_chat_preflight_plan.ja.md) (ja)
- [External Chat Design Decisions](archived/external_chat_design_decisions.ja.md) (ja)
- [Discord Integration Plan](archived/discord_integration_plan.ja.md) (ja)
- [LINE Integration Plan](archived/line_integration_plan.ja.md) (ja)

---

## Conventions

- Active plans live in `active/`. **Move a plan to `archived/` once its completion criteria are met.**
- Each plan opens with its status and remaining work; update that as implementation progresses.
- Archived plans may differ from later safety decisions or implementation details.
  Use current code, tests, and `docs/reference/` as the source of truth.
