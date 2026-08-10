# Butly LoCoMo Evaluation

This package replays fixed LoCoMo conversations through Butly's existing memory
pipeline. It does not add routes or evaluation behavior to the production chat
API.

## Scope (Phases 2–4)

- Parse the official LoCoMo JSON shape into typed DTOs.
- Create a run-scoped workspace outside the production instances tree.
- Replay each session and run Sleeptime synchronously after it.
- Ask one or more questions through `ButlyRuntime.chat()` with RAG enabled and
  Google/Web search disabled.
- Save replay, Sleeptime, QA, Trace, environment, dataset manifest, and compact
  memory snapshot artifacts.
- Score answers with official-compatible rules and write `scores.json`,
  `errors.jsonl`, and a Markdown `summary.md`.
- Checkpoint after every session, Sleeptime pass, and question so interrupted
  runs continue with `resume`.
- Drive everything from Colab through `colab/butly_locomo_eval.ipynb`, a thin
  notebook with no evaluation logic of its own.

## Scoring

`score` follows the official LoCoMo evaluation: answers are normalized
(comma strip, lowercase, punctuation and `a/an/the/and` removal) and
Porter-stemmed before token F1. Category 1 averages, over comma-separated gold
parts, the best F1 against any comma-separated predicted part; category 3
grades only the gold text before the first semicolon; category 5 checks for
"no information available" / "not mentioned" phrases. Category numbers map to
question types as 1 = multi-hop, 2 = temporal reasoning, 3 = open-domain
knowledge, 4 = single-hop, 5 = adversarial (the official repo defines only the
scoring rules per number; the names follow the dataset contents — every
category-2 question is a "When ..." question — and common usage in other
LoCoMo evaluations). Exact match and answer
containment are auxiliary metrics, not official scores. Stemming uses a
self-contained original-Porter implementation (no nltk dependency), so rare
words may stem slightly differently from the official nltk stemmer.

Butly-specific metrics (RAG trigger rates, retrieved cards, latency
percentiles, tier / need_intent distributions, classifier fallback / intent
floor rates, Sleeptime card counts and stage-2 chunk failures) are reported
under a separate `butly` key. A knowledgeization chunk that fails to parse is
counted in `stage2_chunk_failures` and its session row is marked
`stage_2_status: "partial"` — zero cards with zero failures means the model
genuinely extracted nothing.
The evidence-retrieval rate is provenance-based: retrieved card ids are
resolved through `knowledge_cards.source_files` to the saved-turn files whose
`locomo_dialog_ids` contain the gold evidence ids. Stage 2 asks the extraction
model which file each card came from and keeps only names that exist in the
chunk, so `source_files` is per-card when the model can tell and chunk-wide
otherwise — precision therefore varies per card (`knowledge_source_files_card`
/ `_chunk` in the summary report the split). It requires the
run's workspace (instance DB and turn files) and reports n/a without it; the
dataset argument to `score` is no longer needed.

When `brain.search_mode: dual_query` is selected, the normal Gatekeeper response
also emits a standalone memory-search query. Butly vector-searches the original
question and that query for 15 candidates each, deduplicates and equally
RRF-fuses a pool capped at 25, then supplies only the normal requested top-k
(three by default). A missing, invalid, or unchanged query falls back to the
original vector order. `scores.json.butly` keeps query availability,
original/rewrite Recall@3, and fused top-three rescue/harm separate from the
official score.

### Optional semantic judge

Token F1 remains the official LoCoMo score. An optional, separately selected
LLM judge can add a meaning-based diagnostic for paraphrases, translated
answers, missing facts, and reversed claims. Its output is written to
`semantic_scores.json` and `results/semantic_judge/`; it never replaces or
modifies `scores.json.official`.

Enable it in a profile with an evaluation-only top-level section:

```yaml
judge:
  connection: nanogpt
  model_name: TEE/gemma4-31b
  generation_config:
    max_output_tokens: 2048
```

The judge section is not copied into an evaluation instance. Judge
temperature is fixed at `0.0`, and the model receives only the question,
reference answer, candidate answer, category, and judging rubric. Results are
cached per question using model, prompt, and input fingerprints, so an
interrupted judgment can resume without rerunning QA or successful judgments.
OpenAI-compatible Connections such as NanoGPT additionally receive a strict
`response_format=json_schema` contract; Butly still validates the returned
object locally before accepting the artifact.

To judge an existing run after the fact:

```bash
python -m evals.locomo.cli judge \
  --run-dir ./eval_runs/<run-id> \
  --judge-connection nanogpt \
  --judge-model-name TEE/gemma4-31b \
  --judge-max-output-tokens 2048
```

`semantic_scores.json` reports `completed` only when every question has a
valid structured judgment. Provider or schema errors are retained as error
artifacts and make the command fail safely; rerunning retries only those
questions. Category-level semantic averages and disagreements with official
F1 are review aids, not benchmark-compatible replacements.

Before a report or Web/API result uses these diagnostics, it verifies the
aggregate `question_set_fingerprint` against the current questions,
references, predictions, and judge configuration. A mismatch is reported as
`stale`, hides the old semantic metrics and judgments, and requires re-judging.
The problem-level Web result flags partial answers, contradictions, critical
omissions, low-confidence judgments, and both directions of disagreement with
the official score for review.

## Speaker Mapping

`speaker_a` maps to Butly's `user` role and `speaker_b` maps to `assistant`.
Alternating source utterances are saved as one Butly turn. If a session begins
with `speaker_b`, or one speaker talks twice in a row, the adapter saves an empty
opposite role so source order and every dialog ID remain intact. Pair metadata
stores the representative source turn plus all dialog IDs and role mappings.

Image turns keep the original text and append `[Image: <blip_caption>]`. The
fixture under `tests/evals/fixtures/` is fully synthetic and contains no excerpt
from the CC BY-NC 4.0 LoCoMo conversations.

## Run

```bash
python -m evals.locomo.cli run \
  --dataset /path/to/locomo10.json \
  --output-dir ./eval_runs \
  --qa-mode independent \
  --sample-limit 1 \
  --session-limit 2 \
  --question-limit 1 \
  --profile /path/to/profile.yaml
```

`run` finishes with scoring and a summary; pass `--skip-scoring` to stop after
QA. `--model-name` and `--connection` override the chat role for QA.
`--profile <yaml>` applies role sections (`chat` / `gatekeeper` / `summary` /
`knowledge` / `embedding`) plus the optional runtime `reranker` and non-model `memory`, `brain`,
`memory_probe`, and `context_levels` sections
(e.g. `rag_source_mode: both` to inject original-conversation excerpts next to
the RAG cards, `rag_raw_max_chars` to cap them, `rag_raw_top_k: 1` to give only
the single most relevant card its raw (the rest stay summaries) instead of
every retrieved card's raw, or `brain.time_decay_rate: 0.0` to disable recency
weighting for a retrieval ablation) to the evaluation instance config — see
`profiles/*.example.yaml`.
Each model role accepts its own `generation_config.temperature`. Context
injection can be ablated independently, for example:

```yaml
chat:
  generation_config:
    temperature: 0.0
gatekeeper:
  generation_config:
    temperature: 0.0
brain:
  use_rag: false
context_levels:
  preset: custom
  levels:
    current_time: 'off'
    mid_term: high
    session_digest: high
    rag: 'off'
```

Quote `'off'` in hand-written YAML; an unquoted `off` is parsed as boolean
false by YAML 1.1. To disable RAG completely, set both `brain.use_rag: false`
(skip retrieval) and `context_levels.levels.rag: 'off'` (skip injection).

An optional Memory Reranker can reorder the vector top 20 before the normal
top-3 injection. The recommended path is a non-generative local Cross-Encoder.
Install it only where needed with `pip install -r requirements-reranker.txt`.
It is disabled when the section is absent and falls back to vector order on
runtime errors:

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

The second reviewed preset is
`Alibaba-NLP/gte-multilingual-reranker-base`. `score_threshold: null` always
returns the best top three; once calibrated per model, a numeric threshold
allows zero-card abstention. The previous `engine: llm` configuration remains
available for comparison runs.

Evaluate ranking first without regenerating answers:

```bash
python -m evals.locomo.retrieval_replay \
  --run ./eval_runs/runs/qwen3_14b_web_v27 \
  --modes vector reranked \
  --limit 20 \
  --profile ./eval_runs/profiles/reranker.yaml
```

The output compares Recall@1/3/20 and reports top-3 rescue/harm, fallback, and
added latency (plus token usage for the LLM engine). Its per-question details
retain the question/evidence, vector and selected IDs, raw scores, errors, and
pinned model/code revisions. Full LoCoMo runs use the same profile section in
the QA path and retain both original and effective candidate ranks.

To test Summary candidate generation followed by Episode/RAW embedding
selection, use the evaluation-only evidence mode:

```bash
python -m evals.locomo.retrieval_replay \
  --run ./eval_runs/runs/qwen3_14b_web_v27 \
  --modes vector evidence_rerank \
  --limit 20 \
  --profile ./eval_runs/profiles/<source-job>.yaml
```

The first stage is the existing card vector (Title / Tags / Summary) top 20.
The second stage embeds each candidate card's Episode and linked RAW chunks
with the source run's embedding configuration, takes the maximum evidence
cosine per card (MaxP), and selects the top three. RAW chunks default to 1,800
characters with 180-character overlap. This mode does not alter production QA
retrieval or the source instance database.

Document and question vectors are cached in
`retrieval_cache/evidence_embeddings.sqlite3`. Keys include the embedding
model/profile, query/document prefixes, and text hash; reruns therefore reuse
valid vectors and automatically miss after model or source-text changes. The
same cached question vector is reused by both retrieval stages. The cache
stores hashes and vectors, not Episode/RAW text. A remote Embedding
Connection does receive that text as normal embedding input, with its API key
used only for Connection authentication. For review, the separate
`retrieval_replay.json` artifact intentionally retains up to 600 characters of
the evidence selected for each top-three card. The Web Console shows the
document-indexing phase, cache activity, completion/fallback, added latency,
and vector-to-evidence top-three rescue/harm.

Gatekeeper query fusion can be evaluated in the same replay without generating
answers:

```bash
python -m evals.locomo.retrieval_replay \
  --run ./eval_runs/runs/qwen3_14b_web_v27 \
  --modes vector dual_query \
  --limit 25 \
  --profile ./eval_runs/profiles/<source-job>.yaml
```

For a new dual-query run, replay reuses the query saved in each QA result. For
an older run it calls the Gatekeeper from the supplied/source profile once per
problem and then makes two embedding calls. The result records original,
rewritten, and fused Recall@1/3/20, query source/status, and top-three
rescue/harm. It never writes to the source workspace.

The Web Console starts this replay as a persistent background job. Its run
history panel refreshes progress, current mode/question ID, and recent logs
every two seconds, then restores aggregate and per-question results from
`retrieval_replay.json` after a page or Backend restart. The generic Jobs tab
and the replay panel can stop or rerun the same job.

`run`, `resume`, `rerun-qa`, and `retrieval_replay` emit flushed live progress
to stderr, so the Colab run cell shows the active sample, session, or question
even during long model calls.
Replay, Sleeptime, and QA each count as one equal work unit and occupy 0–90%;
scoring occupies 90–96%, and report generation finishes at 100%. The percentage
is therefore a simple completed-work indicator, not an elapsed-time estimate.
The final result JSON remains the last stdout line.

```text
[LoCoMo   0.0%] [0/24] setup      | run=example; samples=1, sessions=11, questions=2, ...
[LoCoMo  41.2%] [11/24] sleeptime | conv-26 session_6 completed
[LoCoMo  86.2%] [23/24] qa         | conv-26 conv-26-qa-1 completed
[LoCoMo  90.0%] score      | Official-compatible scoring starting
[LoCoMo 100.0%] complete   | report completed; .../summary.md
```

On `resume`, already checkpointed Replay, Sleeptime, and QA units are included
in the initial percentage instead of restarting the display from zero.

The default QA mode is `independent`. Each question starts from the same
post-Sleeptime memory state, so an earlier evaluation answer cannot affect a
later question. Use `--qa-mode sequential` for an operational endurance run in
which QA turns intentionally remain in short-term memory and session state:

```bash
python -m evals.locomo.cli run \
  --dataset /path/to/locomo10.json \
  --output-dir ./eval_runs \
  --qa-mode sequential \
  --sample-limit 1 \
  --all-sessions \
  --question-limit 100
```

QA mode and dataset scope are independent settings. `--sample-limit`,
`--session-limit`, and `--question-limit` select bounded subsets. Each has a
mutually exclusive all-items form. A full LoCoMo run must select all three
dimensions:

```bash
python -m evals.locomo.cli run \
  --dataset /path/to/locomo10.json \
  --output-dir ./eval_runs \
  --qa-mode independent \
  --all-samples \
  --all-sessions \
  --all-questions \
  --profile /path/to/profile.yaml
```

The defaults remain one sample, all sessions, and one question. Explicit
`--all-*` flags make full, potentially expensive runs visible in saved commands
and Colab parameters.

For an apples-to-apples check against a prior three-session run, keep the same
model/profile and select one sample, `--session-limit 3`, and
`--question-limit 10`. Then use a different run ID and change only the session
scope to `--all-sessions`. Pure vector retrieval scores the instance's complete
knowledge-card set in both runs; `fallback_fetch_limit` limits only the
keyword-search fallback path. A vector trace therefore reports
`fetch_limit: null` and the actual full-card count in `fetched_count`.

### Evaluation prompt locale

The internal prompt and memory-output locale defaults to English. It can be
stored in the profile:

```yaml
name: full_local
locale: en

chat:
  connection: colab_local
  model_name: qwen3-14b
```

or overridden for one CLI run with `--locale en` / `--locale ja`. Resolution
order is:

```text
--locale > profile top-level locale > en
```

Locale selects Butly's internal localized prompts and memory-output language;
it does not translate the LoCoMo dataset, questions, or gold answers. QA
answers remain explicitly English to match the official LoCoMo questions,
reference answers, and token-F1 scorer. A true Japanese benchmark therefore
requires a separately translated dataset and Japanese-compatible scoring,
rather than only `--locale ja`.
Evaluation instances disable project-local `user_prompts.json` overrides so a
machine-specific custom prompt cannot silently change the selected language or
invalidate a cross-version comparison. Normal Butly instances retain their
existing override behavior.

The resolved locale is persisted in `run_config.json` and used again by
`resume`. Keep locale, QA mode, scope, profile, model parameters, and the
post-Sleeptime input fixed when comparing versions. Otherwise prompt-language
changes, regenerated memory, and sequential QA history can all move the score
independently of the code change being measured.

Existing run directories are preserved; replacing one requires both the same
`--run-id` and explicit `--clean`.

To answer questions again with the exact same knowledge-card corpus, clone the
canonical post-Sleeptime instance into a new run and execute QA only:

```bash
python -m evals.locomo.cli rerun-qa \
  --source-run ./eval_runs/<independent-source-run-id> \
  --dataset /path/to/locomo10.json \
  --output-dir ./eval_runs \
  --run-id <new-run-id> \
  --all-questions \
  --profile /path/to/new-qa-profile.yaml
```

`rerun-qa` never writes to the source run and never executes Replay or
Sleeptime. It verifies the dataset digest and completed session checkpoint,
copies the source instance and Replay/Sleeptime logs, then starts a new QA
checkpoint at zero. The source must have used `qa_mode=independent`, because
only independent QA guarantees that its canonical instance is still the clean
post-Sleeptime baseline. Chat and gatekeeper temperatures/context switches can
affect this QA-only rerun; summary and knowledge temperatures cannot, because
those roles created the already-reused memory during Sleeptime.
`--dataset` is optional when the source's saved path still exists; use it when
the run directory or dataset was moved.
Resuming a reuse run refuses to execute if its pre-completed memory checkpoint
is missing or incomplete, preventing accidental Replay/Sleeptime duplication.

```bash
# continue an interrupted run (skips completed sessions and questions)
python -m evals.locomo.cli resume --run-dir ./eval_runs/<run-id>

# re-score / re-report an existing run
python -m evals.locomo.cli score --run-dir ./eval_runs/<run-id> --dataset /path/to/locomo10.json
python -m evals.locomo.cli report --run-dir ./eval_runs/<run-id>
```

The checkpoint under `checkpoints/checkpoint.json` records replayed sessions,
completed Sleeptime passes, and answered questions. A session interrupted
mid-replay is discarded (its partly saved turns are removed by metadata match)
and replayed in full, so resuming never double-ingests a conversation.

In independent mode, interrupted questions are recreated from the same
post-Sleeptime state before retrying. In sequential mode, the checkpoint keeps
the cumulative QA conversation and resumes after the latest completed
question. Before each sequential question, a durable recovery point captures
the canonical instance, QA-result offset, and existing trace. If execution
stops after writing the answer but before committing the checkpoint, `resume`
rolls all three back before retrying, so the QA history is not duplicated.
Scoring still applies last-write deduplication as a compatibility safeguard for
older run artifacts. Neither mode sends QA turns through Sleeptime.

## Artifacts

```text
<output>/<run-id>/
  workspace/butly_core/instances/<instance>/
  results/replay_log.jsonl
  results/sleeptime_log.jsonl
  results/qa_results.jsonl
  traces/<sample-id>/<question-id>.json
  snapshots/<instance>/<session>/before_sleeptime/
  snapshots/<instance>/<session>/after_sleeptime/
  checkpoints/checkpoint.json
  checkpoints/sequential_qa/  # present only while a sequential QA is in flight
  run_config.json
  dataset_manifest.json
  environment.json
  scores.json
  errors.jsonl
  summary.md
```

API keys and environment-variable values are never written to these artifacts.

## Colab

`colab/butly_locomo_eval.ipynb` mounts Drive, clones Butly, and starts one
OpenAI-compatible llama.cpp server per model configured in its `MODELS`
role map (chat / gatekeeper / summary / knowledge / embedding; roles sharing
a model share a server). A role may carry a `generation_config` override that
is written into the generated profile. The Parameters cell exposes separate
chat, gatekeeper, summary, and knowledge temperatures; the gatekeeper default
raises
`max_output_tokens` to 2048 because a reasoning model's thinking can consume
the stock 512-token budget and return an empty classification. Butly's RAG
needs a real embedding endpoint, so the `embedding`
role must point at an embeddings-capable server (llama.cpp with
`--embeddings`), never at a chat connection. The notebook registers every
server as a connection, generates `profiles/colab_roles.yaml`, runs the CLI
with artifacts on Drive, and after a runtime disconnect resumes via the
Resume cell. llama.cpp is built fresh each session (Drive binary caching was
removed after repeated shared-library breakage). The notebook must stay
logic-free: anything beyond setup and CLI invocation belongs in this package.
Its Parameters cell is rendered as a Colab form and exposes editable `RUN_ID`,
repository/Drive paths, QA mode, locale, separate all/limit controls, context
switches, and a `RUN_MODE` dropdown. `stage3-full` runs Replay → per-session
Sleeptime Stage 2 → Stage 3 in one source-free run, then evaluates QA with
the nodes created by that run; this is the production-like integration path,
not a same-card A/B. For a formal Stage 3 A/B, run
`stage3-source`, then run `stage3-off` and `stage3-on` with distinct run IDs and
the same `SOURCE_MEMORY_RUN_ID`; the ON mode automatically adds
`--stage3-bootstrap` and enables node injection. `standard` preserves the prior
behavior: set `SOURCE_MEMORY_RUN_ID` to route through `rerun-qa`, or leave it
blank for normal Replay/Sleeptime. If ON bootstrap is interrupted before its
durable card-identity completion proof is written, Resume refuses partial
nodes and requires a new ON run ID.

Profiles set a `connection` per role. Using a user-defined connection (e.g.
`colab_local`) for every role exercises code paths that built-in providers
(gemini/openai) mask — earlier a Sleeptime bug dropped the connection for the
summary/knowledge roles and only surfaced with local models. If you see
`Cannot infer connection for model_name=...`, a role is being resolved from a
bare model name instead of its `{connection, model_name}` pair.
