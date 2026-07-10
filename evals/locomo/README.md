# Butly LoCoMo Evaluation

This package replays fixed LoCoMo conversations through Butly's existing memory
pipeline. It does not add routes or evaluation behavior to the production chat
API.

## Phase 2 Scope

- Parse the official LoCoMo JSON shape into typed DTOs.
- Create a run-scoped workspace outside the production instances tree.
- Replay each session and run Sleeptime synchronously after it.
- Ask one or more questions through `ButlyRuntime.chat()` with RAG enabled and
  Google/Web search disabled.
- Save replay, Sleeptime, QA, Trace, environment, dataset manifest, and compact
  memory snapshot artifacts.

Scoring, checkpoint/resume, reports, and Colab notebooks are deferred to later
phases.

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

`--model-name` and `--connection` override the chat role for QA. Sleeptime,
Gatekeeper, and embedding roles continue to use Butly's existing model and
connection configuration. Existing run directories are preserved; replacing
one requires both the same `--run-id` and explicit `--clean`.

Phase 2 defaults to one question. When `--question-limit` is greater than one,
questions run sequentially and their answers are not sent through Sleeptime,
but earlier QA turns remain in short-term memory. Per-question workspace cloning
is deferred with the checkpoint/isolation work.

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
  run_config.json
  dataset_manifest.json
  environment.json
```

API keys and environment-variable values are never written to these artifacts.
