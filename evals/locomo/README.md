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
"no information available" / "not mentioned" phrases. Exact match and answer
containment are auxiliary metrics, not official scores. Stemming uses a
self-contained original-Porter implementation (no nltk dependency), so rare
words may stem slightly differently from the official nltk stemmer.

Butly-specific metrics (RAG trigger rates, retrieved cards, latency
percentiles, tier / need_intent distributions, Sleeptime card counts) are
reported under a separate `butly` key. The evidence-retrieval rate is a
token-overlap heuristic between evidence turns and retrieved card texts and is
only computed when the dataset is available at scoring time.

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
  --sample-limit 1 \
  --session-limit 2 \
  --question-limit 1
```

`run` finishes with scoring and a summary; pass `--skip-scoring` to stop after
QA. `--model-name` and `--connection` override the chat role for QA;
`--profile <yaml>` applies role sections (`chat` / `gatekeeper` / `summary` /
`knowledge` / `embedding`) to the evaluation instance config — see
`profiles/*.example.yaml`. Existing run directories are preserved; replacing
one requires both the same `--run-id` and explicit `--clean`.

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
and replayed in full, so resuming never double-ingests a conversation. If a
crash lands between a QA write and its checkpoint update, the duplicate answer
is deduplicated at scoring time (the newest record wins).

The default is one question. When `--question-limit` is greater than one,
questions run sequentially and their answers are not sent through Sleeptime,
but earlier QA turns remain in short-term memory. Per-question workspace
cloning is still deferred.

## Artifacts

```text
<output>/<run-id>/
  workspace/butly_core/instances/<instance>/
  results/replay_log.jsonl
  results/sleeptime_log.jsonl
  results/qa_results.jsonl
  traces/<question-id>.json
  snapshots/<instance>/<session>/before_sleeptime/
  snapshots/<instance>/<session>/after_sleeptime/
  checkpoints/checkpoint.json
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
a model share a server). Reasoning models are slow, so the memory-pipeline
roles default to a Non-Reasoning model while chat stays the model under
evaluation. Butly's RAG needs a real embedding endpoint, so the `embedding`
role must point at an embeddings-capable server (llama.cpp with
`--embeddings`), never at a chat connection. The notebook registers every
server as a connection, generates `profiles/colab_roles.yaml`, runs the CLI
with artifacts on Drive, and after a runtime disconnect resumes via the
Resume cell. llama.cpp is built fresh each session (Drive binary caching was
removed after repeated shared-library breakage). The notebook must stay
logic-free: anything beyond setup and CLI invocation belongs in this package.

Profiles set a `connection` per role. Using a user-defined connection (e.g.
`colab_local`) for every role exercises code paths that built-in providers
(gemini/openai) mask — earlier a Sleeptime bug dropped the connection for the
summary/knowledge roles and only surfaced with local models. If you see
`Cannot infer connection for model_name=...`, a role is being resolved from a
bare model name instead of its `{connection, model_name}` pair.
