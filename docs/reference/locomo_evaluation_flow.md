# LoCoMo Evaluation Data and QA Flow

[日本語](locomo_evaluation_flow.ja.md) | **English**

This document shows how the current `evals/locomo/` implementation persists
LoCoMo conversations, when QA begins, and how `independent` and `sequential`
QA modes differ.

## End-to-end order

The evaluator does not ingest every sample before starting all QA. It repeats
the following sequence **per sample**:

1. Create one canonical evaluation instance.
2. Replay each selected session and complete Sleeptime for that session.
3. After every selected session in that sample is complete, run its selected
   questions.
4. Move to the next sample.

```mermaid
flowchart TD
    A["LoCoMo JSON"] --> B["dataset.py<br/>Conversation / Session / Turn / Question DTOs"]
    B --> C["Select sample"]
    C --> D["Create canonical evaluation instance"]
    D --> E["Replay next session"]
    E --> F["Persist source conversation to short_term_json"]
    F --> G["Checkpoint replayed_sessions"]
    G --> H["before_sleeptime snapshot"]
    H --> I["Sleeptime Stage 1 / Stage 2"]
    I --> J["after_sleeptime snapshot"]
    J --> K["Checkpoint sleeptime_completed"]
    K --> L{"More selected sessions?"}
    L -- Yes --> E
    L -- No --> M["Set QA clock to one day after the last session"]
    M --> N{"qa_mode"}
    N -- independent --> O["Ask in a disposable clone"]
    N -- sequential --> P["Ask in the canonical instance"]
    O --> Q["Persist QA result / Trace / checkpoint"]
    P --> Q
    Q --> R{"More selected questions?"}
    R -- Yes --> N
    R -- No --> S{"More samples?"}
    S -- Yes --> D
    S -- No --> T["Score and write summary"]
```

## Initial conversation persistence

### Dataset mapping

`dataset.py` maps the official JSON into immutable DTOs:

- one `sample_id` becomes one evaluation instance;
- `conversation.session_N` entries become chronological sessions;
- each `dia_id` is retained for evidence provenance;
- `qa` entries become questions, reference answers, categories, and evidence;
- an image caption is appended to turn text as `[Image: ...]`.

### Speaker mapping

`ReplayAdapter` applies a fixed role mapping:

| LoCoMo | Butly |
|---|---|
| `speaker_a` | `user` |
| `speaker_b` | `assistant` / `model` |

A user utterance and the following assistant utterance are normally persisted
as one Butly turn. Consecutive utterances from the same role use an empty
opposite side so source order and every `dia_id` remain intact.

### `short_term_json`

Replay uses the normal `ButlyMemory.save_single_turn()` writer. It passes the
source session timestamp rather than the evaluation wall-clock time.

```text
LoCoMo turn
  → ReplayAdapter.replay_session()
  → ButlyMemory.save_single_turn(
        user_text,
        assistant_text,
        created_at=source LoCoMo timestamp,
        meta=LoCoMo provenance
    )
  → workspace/butly_core/instances/<instance>/short_term_json/session_*.json
```

The metadata retains the sample ID, session ID, dialog IDs, original speaker
and timestamp, role mapping, and `source=eval`. `results/replay_log.jsonl`
also records the saved filenames and dialog IDs.

## Per-session Sleeptime

Immediately after all turns in one session are saved, the evaluator runs
Sleeptime synchronously on that instance. Its default evaluation targets are:

- Stage 1: flush short-term conversation data and update the mid-term digest,
  recent snapshot, RAW memory cache, and related derived memory;
- Stage 2: extract knowledge cards from RAW conversation data and store them in
  `butly_memory.db`;
- disabled: key-memory updates and knowledge maturation.

```text
short_term_json/session_*.json
  └─ Sleeptime Stage 1
      ├─ memory_archive/1_integrated/
      ├─ mid_term_digest.txt
      ├─ recent_snapshot.txt
      └─ RAW memory cache
          └─ Sleeptime Stage 2
              ├─ knowledge_cards in butly_memory.db
              └─ memory_archive/2_knowledgeized/
```

The `before_sleeptime` and `after_sleeptime` directories are compact audit
snapshots, not full instance clones. QA begins only after every selected
session in the current sample is listed in `sleeptime_completed`.

## Shared QA path

Both modes use the same Butly chat path; only the target instance differs.

```mermaid
flowchart LR
    A["LoCoMo Question"] --> B["QARunner builds ChatRequest"]
    B --> C["ButlyRuntime.chat()"]
    C --> D["ChatService preparation"]
    D --> E["Chronos + recent history"]
    E --> F["Gatekeeper"]
    F --> G["MemoryBlockBuilder"]
    G --> H["RAG<br/>knowledge cards / RAW references"]
    H --> I["Chat model generation"]
    F --> J["StateUpdater"]
    I --> K["Persist question and answer to short_term_json"]
    J --> K
    K --> L["Trace latest.json"]
    L --> M["QARunner copies durable artifacts"]
    M --> N["qa_results.jsonl + per-question Trace"]
    N --> O["Advance checkpoint.qa_completed"]
```

The fixed QA request policy enables RAG, disables Google and web search, keeps
the original LoCoMo question text, and requests concise English answers for
official-scorer compatibility. The QA clock is fixed to one day after the last
selected session.

The evaluation instance System Instruction treats every supplied memory section
as answer evidence. If a knowledge card, source RAW excerpt, or active node
directly answers the question, the model is told to use it and interpret tense
relative to the original conversation date. It may return
`No information available` only when none of the supplied memories contains the
answer. `qa_prompt_version` in `run_config.json` records the template version;
scores from different versions are not the same evaluation condition.

After generation, ChatService persists the question and answer as a normal
conversation turn in the target instance and updates session state.

`diagnostics.rag` in `qa_results.jsonl` and each per-question Trace preserve the
retrieved card IDs, dates, source instances, and RAW-reference state. With Stage 3
enabled they also record the active-node lookup reason, linked card IDs, render
candidates, and final-Provider-prompt inclusion result. This distinguishes no
node match, context-level exclusion, and a node that was injected but not used in
the answer. The full prompt body is not copied into evaluation artifacts.

Pure vector retrieval scores every knowledge card in the instance regardless
of age. It then applies time decay, archive weighting, and the score threshold
before returning the top `limit` RAG candidates. `fallback_fetch_limit` belongs
only to keyword-search fallback and does not restrict this vector candidate
pool. In `memory_probe_layers.vector` traces, `fetch_limit: null` denotes the
full-card scan and `fetched_count` is the number of cards actually scored.
An evaluation profile can override retrieval recency through
`brain.time_decay_rate`. The Colab default of `0.0` is an ablation that ranks
old and new cards by semantic similarity alone; it does not change the system
default for normal instances.

### Hybrid retrieval (`brain.search_mode`)

`brain.search_mode: hybrid` switches retrieval to FTS5(trigram) BM25 candidates
fused with vector candidates via RRF (default remains `vector`). Profiles may
override `bm25_candidates` / `vector_candidates` / `rrf_k` / `bm25_weights` /
`bm25_max_df_ratio` / `bm25_min_weak_df` / `bm25_scan_limit`. In hybrid results
`score` is the **RRF score** and cosine moves to `vector_score`; each candidate
also carries `retrieval_source` (vector/bm25/both) and both ranks.

### Gatekeeper dual-query retrieval (`brain.search_mode: dual_query`)

`dual_query` independently vector-searches the original utterance and the
standalone `retrieval_query` emitted in the same Gatekeeper classification.
By default it deduplicates each top 15 by card ID, equally RRF-fuses them
(`rrf_k: 60`), retains at most 25 diagnostic candidates, and returns only the
normal top three requested by MemoryProbe. The rewrite resolves pronouns or
omitted subjects from recent history while preserving names, dates, negation,
and relationships. A missing, invalid, or unchanged query skips the second
embedding and exactly falls back to the original vector order.

The controls are `dual_query_candidates` (15), `dual_query_pool_limit` (25),
and `rrf_k`. No additional generative call is needed during normal QA because
the existing Gatekeeper response owns the extra field. `retrieval_source`
continues to mean vector/BM25 evidence; query overlap is recorded separately
as `query_source` and per-query ranks.

The run-history section has an "offline retrieval replay" panel
that starts a persistent job through
`POST /evaluations/runs/retrieval-replay/jobs` (the previous synchronous
endpoint remains for compatibility). It compares Recall@1/3/20 for `bm25` /
`vector` / `hybrid` / `dual_query` / `reranked` / `evidence_rerank` without
generating answers. Percentage,
current mode/question ID, and recent logs refresh every two seconds; work
continues after leaving the page and can be stopped or rerun with the same
settings. The same panel displays aggregates and per-question
rescue/harm/fallback, candidate order, raw scores, and errors after completion.
The result is also written to `retrieval_replay.json` inside the run. `bm25`
needs no embedding calls; `vector` / `hybrid` call the embedding model once per
question. `dual_query` reuses a QA-time saved query and calls embeddings twice;
for an old run without one, it also calls the source Gatekeeper once per
problem. `reranked` additionally batch-scores the selected candidate
pool. Per-question `details` retain the question, evidence, original vector
order, actually selected zero-to-three cards, all raw scores, and errors for
threshold calibration and rescue/harm review.

`evidence_rerank` is an evaluation-only two-stage retrieval mode. It takes the
vector top N (20 by default) from the existing Title / Tags / Summary card
embedding, embeds each candidate's Episode and linked RAW conversation in the
same space, and selects the top three by each card's maximum evidence cosine
(MaxP). RAW is split into 1,800-character chunks with 180-character overlap;
only legacy cards with neither Episode nor RAW fall back to Summary. Document
embeddings are prepared once and cached together with question embeddings in
`retrieval_cache/evidence_embeddings.sqlite3`. Cache keys include model,
profile, query/document prefixes, and text hash, so configuration or text
changes cannot silently reuse an old vector. A single cached question vector
is shared by the Summary search and evidence scoring, avoiding a duplicate
embedding call. The cache contains hashes and vectors only, and the source
instance database, cards, and RAW files are not changed.

A remote Embedding Connection receives Episode and RAW text as normal embedding
input. Its API key is used only for Connection authentication and is not stored
in the cache or artifacts. For problem-level review,
`retrieval_replay.json` does retain a preview of up to 600 characters for each
selected evidence unit. Aggregates and the UI separate document-indexing
progress, cache hits/misses/writes, completion/fallback, added latency, and
top-three rescue/harm relative to the original vector order.

The Web Console exposes `search_mode` / `retrieval_execution` /
`injection_policy` under "検索設定（Dual Query / Hybrid / Reranker）"; `hybrid` additionally
reveals `bm25_candidates` / `vector_candidates` / `rrf_k` /
`bm25_max_df_ratio`. Those land in the profile YAML's `brain` and
`memory_probe` sections (BM25 keys are omitted from `vector` runs), and the run
history / comparison tables gain `search_exec`, `recall@3`, and `bm25_rescue`.
`dual_query` writes its candidates-per-query, pool cap, and RRF k; history and
comparison surfaces include query generation, original/rewrite Recall@3, and
top-three rescue/harm.

### Memory reranker (`reranker`)

An optional `reranker` profile section takes the top vector candidates
(20 by default), reorders them, and injects at most three through the normal
RAG path. The recommended non-generative `cross_encoder` path batch-scores each
question/card pair using bounded `title`, `summary`, `episode`, and
`source_date` text.

The reviewed multilingual presets are
`cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` and
`Alibaba-NLP/gte-multilingual-reranker-base`. Install their optional runtime
with `pip install -r requirements-reranker.txt`; models load on first use and
are cached in-process. When `score_threshold` is set, the reranker may select
zero cards and suppresses the Deep Search bypass. Raw scores are not portable
probabilities, so calibrate a threshold per model through offline replay.

```yaml
reranker:
  enabled: true
  engine: cross_encoder
  model_name: cross-encoder/mmarco-mMiniLMv2-L12-H384-v1
  candidate_limit: 20
  max_candidate_chars: 1600
  batch_size: 20
  device: auto
  score_threshold: null
```

The legacy `llm` engine remains available for reproducible comparisons. It
sends opaque, untrusted candidate JSON to a Connection and requires strict
JSON Schema plus local validation. Either engine falls back to the untouched
vector order on runtime errors.

Version 1 supports `search_mode: vector` only; Web/API validation rejects a
Hybrid/Dual-Query+reranker combination. Omitting or disabling the section preserves the
previous behavior, and normal production instances can use the same optional
section. Diagnostics retain the original `vector_candidate_ids`, the final
`effective_candidate_ids`, engine, completion/fallback state, added latency,
and LLM usage when applicable.

Retrieval execution and prompt injection are controlled independently by
`memory_probe.retrieval_execution` (default `always`) and
`memory_probe.injection_policy` (default `intent_gated`). With `always`, Quick
Retrieval runs even when `need_intent` is null, so **`rag_trigger_rate` is no
longer an execution rate**: use `search_execution_rate` for execution and
`memory_injection_rate` (identical to `rag_trigger_rate`) for injection.

Retrieval metrics emitted by the scorer:

| Metric | Denominator | Meaning |
|---|---|---|
| `search_execution_rate` | all questions | share where Quick Retrieval ran |
| `retrieval_candidate_rate` | all questions | share with at least one candidate |
| `memory_injection_rate` | all questions | share injected into the prompt |
| `retrieval_recall_at_1/3/20` | questions with an oracle card | evidence-turn coverage of the top-k candidates |
| `vector_only_recall_at_3` | same | control value with BM25 removed |
| `bm25_rescue_rate` | same | share where fused top-3 beat vector-only top-3 |
| `reranker_completion_rate` / `reranker_fallback_rate` | reranker attempts | share with valid structured output / share restored to vector order |
| `reranker_rescue_rate_at_3` / `reranker_harm_rate_at_3` | completed reranks with oracle cards | share where effective top-3 improved / degraded vector top-3 |
| `reranker_latency_ms_p50/p95` | reranker attempts | added reranker-only latency |
| `retrieval_latency_ms_p50/p95` | executed questions | retrieval-only latency (includes the embedding call) |
| `bm25_short_term_hit_rate` | executed questions | share where the 2-char CJK LIKE fallback contributed |

`evidence_retrieval_rate` measures **injected** cards and therefore reflects the
injection policy; judge ranking quality with `retrieval_recall_at_k` instead.
`python -m evals.locomo.retrieval_replay` compares those recalls offline (no
answer generation) before spending QA tokens.

An evaluation profile can set `current_time`, `mid_term`, `session_digest`,
and `rag` independently under `context_levels.levels` to `high` or `'off'`.
Fully disabling RAG requires both `rag: 'off'` to suppress prompt injection and
`brain.use_rag: false` to skip retrieval. The Colab Parameters cell exposes all
four as booleans and writes both RAG settings. Quote `'off'` in hand-written
YAML because YAML 1.1 parses an unquoted `off` as boolean false.

The `chat`, `gatekeeper`, `summary`, and `knowledge` roles can each carry an
independent `generation_config.temperature`. Chat controls final answers,
gatekeeper controls retrieval decisions, and summary/knowledge operate during
Sleeptime digest/card construction.

## Rerun QA with the same cards

`rerun-qa` clones a source run's canonical instance into a new run, skips
Replay and Sleeptime, and starts QA again at question zero.

```bash
python -m evals.locomo.cli rerun-qa \
  --source-run ./eval_runs/qwen3_14b_colab_v16 \
  --dataset /path/to/locomo10.json \
  --output-dir ./eval_runs \
  --run-id qwen3_14b_colab_v16_no_time \
  --all-questions \
  --profile /path/to/qa-ablation.yaml
```

The following safety properties apply:

- The source must use `qa_mode=independent`; only independent QA preserves the
  canonical post-Sleeptime instance.
- The command verifies the dataset SHA-256, selected-session Replay/Sleeptime
  checkpoint, and an empty canonical `short_term_json` directory.
- It never writes to the source. The instance (including its card database)
  and Replay/Sleeptime logs are copied, while QA results, Traces, and a new
  checkpoint are created in the destination.
- The copied instance's LoCoMo answer System Instruction is updated to the
  current `qa_prompt_version`; cards, RAW sources, and active nodes are unchanged.
- `memory_reused_from_run_id` in `run_config.json` records card provenance.
- Chat/gatekeeper temperatures and context switches affect a QA-only rerun.
  Summary/knowledge temperatures do not rebuild the already-complete memory.
- If the dataset moved, `--dataset` can point to its new location; a file whose
  SHA-256 differs from the source manifest is rejected.
- Resume stops instead of falling back to Replay/Sleeptime when a reuse run's
  pre-completed memory checkpoint is missing or incomplete.

In Colab, set `SOURCE_MEMORY_RUN_ID` to the source run ID and choose a new
`RUN_ID`. Leaving it blank executes the normal Replay → Sleeptime → QA path.
The sample/session scope stays fixed to the source card corpus; only question
scope can change.

## Stage 3 (memory_nodes) ON/OFF evaluation

Measuring Stage 3 uses a clone A/B that varies **only the node layer over the
exact same knowledge-card set** (two full replays would differ through Stage 2
LLM variance, so that approach is not used).

```bash
# 1. Baseline source run (Stage 3 stays off by default)
python -m evals.locomo.cli run --dataset ... --output-dir ./eval_runs \
  --run-id stage3-source --qa-mode independent --all-questions

# 2. OFF clone: QA over identical cards without nodes
python -m evals.locomo.cli rerun-qa --source-run ./eval_runs/stage3-source \
  --run-id stage3-off --profile evals/locomo/profiles/stage3_off.example.yaml

# 3. ON clone: verify card identity → drain the queue via stage3-bootstrap → QA with node injection
python -m evals.locomo.cli rerun-qa --source-run ./eval_runs/stage3-source \
  --run-id stage3-on --stage3-bootstrap \
  --profile evals/locomo/profiles/stage3_on.example.yaml
```

The Colab notebook renders its Parameters cell as a form. `RUN_ID`,
`SOURCE_MEMORY_RUN_ID`, evaluation scope, and primary paths can be changed
from the form controls. Choose `RUN_MODE` from this dropdown:

| `RUN_MODE` | Behavior |
|---|---|
| `standard` | Normal evaluation. A blank source ID starts at Replay; a source ID keeps the existing QA-only card-reuse behavior |
| `stage3-full` | Single source-free run: execute Replay → Stage 2 → Stage 3 for every session, then inject the nodes created by that run into final QA. This is a production-like integration evaluation, not a same-card A/B |
| `stage3-source` | Build the formal A/B post-Stage 2 source with Stage 3 explicitly off; source ID must be blank and `QA_MODE=independent` |
| `stage3-off` | Clone the source's identical cards and run QA without nodes; source ID is required |
| `stage3-on` | Clone the same source, automatically enable `--stage3-bootstrap` and node injection, then run QA; source ID is required |

Use distinct `RUN_ID` values for OFF and ON, with the same
`SOURCE_MEMORY_RUN_ID`. `STAGE3_BATCH_SIZE` is exposed for weaker local
models, and `STAGE3_BOOTSTRAP_MAX_CARDS` controls the safety cap.

The same parameters are available from the Butly Web Console `📊` screen.
The Web implementation builds a profile and CLI arguments rather than
reimplementing Notebook evaluation logic. It displays durable CLI progress,
can resume a stopped checkpoint, and compares saved-run metrics and
per-question deltas. See
[LoCoMo Evaluation Web Console](evaluation_web_console.md) for the API, state
transitions, and storage rules.

Properties and artifacts:

- Right after cloning, `knowledge_cards` ids and canonical content hashes
  (the normalization in `butly_core/core/card_content.py`) are compared with
  the source; any mismatch fails the run before QA. The result is written to
  `card_identity.json` in the run directory. Since OFF and ON both verify
  against the same source, their card sets are transitively identical.
- `--stage3-bootstrap` is the ON arm. Bootstrap statistics go to
  `results/stage3_bootstrap_log.jsonl`; any status other than `completed`
  (including `partial`) invalidates the arm and fails the run. Card identity
  is re-verified after bootstrap.
- If Colab disconnects during ON bootstrap before `card_identity.json` contains
  durable completion proof for every sample, `resume` refuses to continue QA
  over partial nodes. Re-run the ON arm with a new `RUN_ID`.
- Bootstrap injects the same clock as QA: the day after the last session.
- Node injection is enabled by `memory.knowledge_maturation_enabled=true`
  (the stage3_on profile). Automatic Key Memory application stays off here.
- Comparison metrics: QA scores (existing scorer), counts/digests in
  `card_identity.json`, and node/failure/LLM-call counts in
  `stage3_bootstrap_log.jsonl`.

### Integration path: per-session Stage 3

Notebook `stage3-full` (or passing the `stage3_on` profile to a normal `run`)
does not clone a source. It merges the profile's `sleeptime` section
recursively, and the SleeptimeRunner executes Stage 3 after a
successful Stage 2, injecting the session's original timestamp as the clock
(no dependency on `BUTLY_CHRONOS_NOW`, which is only set during QA).
`sleeptime_log.jsonl` records `stage_3_status` / `stage_3_reviewed_cards` /
`stage_3_created_nodes` / `stage_3_linked_sources` / `stage_3_failed_cards` /
`stage_3_llm_calls` / `stage_3_prompt_tokens` / `stage_3_completion_tokens`
separately from Stages 1/2, so a Stage 3 failure never masquerades as a
Stage 2 success. This path evaluates production-like node creation during
memory accumulation and node use in final QA; the official Stage 3-only
accuracy A/B is the clone procedure above.

## `independent` QA

The goal is to start every question from exactly the same post-Sleeptime state.

```mermaid
flowchart TD
    A["Canonical instance<br/>post-Sleeptime state P0"]
    A --> B["copytree once to temporary baseline"]
    B --> C["Before Q1:<br/>copy baseline to active instance"]
    C --> D["Run Q1 with a fresh Runtime"]
    D --> E["Q1/Answer mutate only active"]
    E --> F["Persist result and Trace in run directory"]
    F --> G["Before Q2:<br/>delete active and copy baseline again"]
    G --> H["Run Q2 with a fresh Runtime"]
    H --> I["Q2 cannot observe Q1"]
    I --> J["Repeat reset for every question"]
    J --> K["Delete temporary workspace after all questions"]
    A -. "Not mutated by QA" .-> K
```

Conceptual temporary layout:

```text
/tmp/butly-locomo-qa-<instance>-*/
├─ baseline/<instance>/                 # copied once from canonical
└─ active/butly_core/instances/<instance>/
                                          # recreated before each question
```

Properties:

- QA never mutates the canonical instance.
- A previous question, answer, or session state is invisible to the next one.
- A fresh Runtime prevents per-instance cache leakage.
- QA turns written to the active instance are discarded at the next reset.
- `qa_results.jsonl` and per-question Traces remain durable.
- Copy cost is one canonical-to-baseline copy plus one baseline-to-active copy
  per question.

This is the default mode for comparable model and version measurements.

To isolate the effect of session count, keep the model, profile, and question
scope fixed and use separate run IDs for
`--session-limit 3 --question-limit 10` and
`--all-sessions --question-limit 10`. The first reproduces the older bounded
condition; the second exercises retrieval after the full conversation has
been stored.

## `sequential` QA

The goal is to accumulate QA conversation in one instance, like an operational
endurance run.

```mermaid
flowchart TD
    A["Canonical instance<br/>post-Sleeptime state P0"]
    A --> B["Create pre-Q1 recovery point"]
    B --> C["Run Q1 in canonical instance"]
    C --> D["Persist Q1/Answer and session state<br/>state P1"]
    D --> E["Persist result → checkpoint → clear recovery point"]
    E --> F["Create pre-Q2 recovery point"]
    F --> G["Run Q2 in state P1"]
    G --> H["Q2 can observe Q1/Answer in recent history<br/>state P2"]
    H --> I["Persist result → checkpoint → next question"]
```

Properties:

- QA runs directly in the canonical instance used for Replay and Sleeptime.
- Each question, answer, and session-state change carries into later questions.
- No explicit Sleeptime pass runs between QA questions.
- Normal ChatService short-term persistence and memory maintenance still run.
- A durable recovery point protects the instance, QA-results offset, and Trace
  before every question.
- Resume rolls back an uncommitted interrupted question to its pre-question
  state.

This mode measures history contamination, topic/session-state drift, and RAG
behavior across long operational question sequences.

## Mode comparison

| Dimension | `independent` | `sequential` |
|---|---|---|
| Start state | Same post-Sleeptime baseline every time | State accumulated through prior QA |
| QA persistence target | Temporary active instance under `/tmp` | Canonical instance in the run directory |
| Prior questions and answers | Invisible | Available as recent history |
| Canonical instance | Unchanged by QA | Mutated after each QA |
| Runtime | Fresh per question | Reused |
| Sleeptime between questions | None | None |
| Primary use | Model/version comparison | Operational endurance |
| Main extra cost | Per-question instance copy | Per-question recovery copy |

## Durable run artifacts

```text
<output-dir>/<run-id>/
├─ run_config.json
├─ dataset_manifest.json
├─ environment.json
├─ workspace/
│  └─ butly_core/instances/<instance>/   # canonical instance
├─ checkpoints/
│  ├─ checkpoint.json
│  └─ sequential_qa/                     # in-flight sequential recovery
├─ snapshots/
│  └─ <instance>/<session>/
│     ├─ before_sleeptime/
│     └─ after_sleeptime/
├─ results/
│  ├─ replay_log.jsonl
│  ├─ sleeptime_log.jsonl
│  └─ qa_results.jsonl
└─ traces/
   └─ <sample>/<question-id>.json
```

Independent QA instances live outside this tree in the OS temporary directory
and are deleted after all questions for the sample.

## CLI and Colab live progress

`run`, `resume`, and `rerun-qa` emit flushed progress before and after long
model operations.
The Colab run cell inherits the child process's stderr, so no notebook-side
output reader is required.

```text
[LoCoMo  41.2%] [11/24] sleeptime | conv-26 session_6 completed
```

The overall percentage is a simple completed-work indicator rather than an
elapsed-time estimate:

- one replayed session is one unit;
- one Sleeptime pass is one unit;
- one answered question is one unit;
- those units map to 0–90%;
- scoring completes at 96%, and `summary.md` generation completes at 100%.

Units have equal weight, so wall-clock progress varies with model and session
length. A resumed run includes checkpointed units in its initial percentage and
therefore continues near its prior position. Progress uses stderr, while the
existing final result JSON remains the last stdout line.

## Reading the checkpoint

For each sample, `checkpoints/checkpoint.json` records:

- `replayed_sessions`: source turns have been committed to `short_term_json`;
- `sleeptime_completed`: Sleeptime and the after-snapshot are committed;
- `qa_completed`: number of questions committed after result persistence;
- `status`: `replaying`, `qa`, or `completed`.

A session present in `replayed_sessions` but absent from
`sleeptime_completed` therefore means “Replay complete, Sleeptime in progress
or incomplete.” QA has not started yet.
