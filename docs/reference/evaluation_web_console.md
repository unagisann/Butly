# LoCoMo Evaluation Web Console

[日本語](evaluation_web_console.ja.md) | **English**

The Butly Web Console can launch, stop, resume, inspect, and compare existing
LoCoMo evaluator runs.

`evals/locomo/` remains the source of truth. The Web API is a thin persistent
subprocess manager around the existing CLI; it does not duplicate Replay,
Sleeptime, QA, checkpoint, or scoring logic.

## Screens

Open the console from the home-screen `📊` button.

| Tab | Purpose |
|---|---|
| New evaluation | Colab-parameter-equivalent run and model settings |
| Jobs | Progress, phase, logs, stop, and checkpoint resume |
| History / compare | Saved-run metrics, two-to-eight-run comparison, question deltas |

The start form covers:

- `RUN_ID`;
- `RUN_MODE`: `standard`, `stage3-full`, `stage3-source`, `stage3-off`, or
  `stage3-on`;
- `SOURCE_MEMORY_RUN_ID`;
- QA mode and locale;
- all/limited sample, session, and question scopes;
- Current Time, Mid-term, Session Digest, and RAG switches;
- RAG source, RAW top-k and character cap, and time decay;
- Stage 3 batch size and bootstrap card cap;
- Connection-then-model selection for chat, gatekeeper, summary, knowledge,
  and embedding;
- the embedding prefix convention (defaults to `auto`, inferred from the model
  name — see
  [memory_lifecycle.md](memory_lifecycle.md#embedding-profiles-per-model-input-conventions));
- role temperatures and the Gatekeeper output limit;
- an "run despite embedding mismatch" override, shown only when
  `SOURCE_MEMORY_RUN_ID` is set.

Connections and API keys are shared with the normal Web Console. The form
never receives key values. The evaluation subprocess inherits the Backend
process environment.

### Embedding compatibility check for memory-reusing runs

A run with `SOURCE_MEMORY_RUN_ID` (`rerun-qa`) reuses the source run's cards and
`embedding_blob` values as-is. If the embedding model or its prefix convention
changed since then, stored vectors and query vectors live in **different
spaces** and retrieval breaks silently — no exception, no log line.

`POST /evaluations/jobs` therefore compares the `embedding_meta` of the source
run's workspace instance DBs against the requested embedding config before
starting, and rejects the job with 400 on any difference. **Matching dimensions
are not enough** — a changed convention (e.g. cards built before prefixes were
applied) is rejected too.

Pass `allow_embedding_mismatch: true` (the UI checkbox) to run anyway. Retrieval
metrics from such a run cannot be compared against others.

## Job API

The legacy Web Console Backend exposes:

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/evaluations/config` | Output root, dataset candidates, and run modes |
| `POST` | `/evaluations/jobs` | Start a run |
| `GET` | `/evaluations/jobs` | List jobs |
| `GET` | `/evaluations/jobs/{job_id}` | Read status and progress |
| `POST` | `/evaluations/jobs/{job_id}/stop` | Stop the subprocess and children |
| `POST` | `/evaluations/jobs/{job_id}/resume` | Resume through the existing CLI |
| `GET` | `/evaluations/jobs/{job_id}/log` | Tail the combined log |
| `GET` | `/evaluations/runs` | List saved runs and headline metrics |
| `POST` | `/evaluations/runs/compare` | Compare metrics and questions for 2–8 runs |

The manager permits one active Web job at a time. This avoids conflicting
writes and local LLM/GPU overload; the same conservative rule initially
applies to remote providers such as NanoGPT.

### State transitions

```text
queued → running → completed
                 ├→ failed
                 └→ stopping → stopped

running ── missing PID after Backend restart ─→ interrupted
stopped / failed / interrupted ── resume ─→ running
```

Stopping preserves every checkpointed unit. Resume executes
`python -m evals.locomo.cli resume` against the existing `run_config.json` and
checkpoint. A job stopped before its run directory was created cannot resume;
start again with a new `RUN_ID`.

If Stage 3 ON bootstrap stops before durable completion proof exists, the
existing evaluator safety rule may reject resume. Start a new ON arm instead
of evaluating partial nodes.

## Subprocess and durable state

Jobs use `sys.executable -m evals.locomo.cli ...`. Combined stdout/stderr goes
directly to a log file and therefore survives Streamlit reruns. After a
Backend restart, PID and process creation time are checked together so stop
cannot target a reused PID.

```text
DATA_DIR/eval_runs/
├─ runs/
│  └─ <run_id>/
├─ jobs/
│  ├─ <job_id>.json
│  └─ <job_id>.log
└─ profiles/
   └─ <job_id>.yaml
```

`eval_runs/` is ignored by Git. Job JSON and generated profiles contain model
names, Connection IDs, and evaluation settings, but no API keys.

The default run output root and history/comparison source is
`DATA_DIR/eval_runs/runs/`. It changes only when the environment variable is
set:

1. `BUTLY_EVALUATION_OUTPUT_DIR`;
2. `DATA_DIR/eval_runs/runs/`.

Dataset candidates come from `BUTLY_LOCOMO_DATASET`,
`data/locomo10.json`, and the synthetic mini fixture. Any Backend-readable
absolute dataset path may be entered manually.

## History and comparison

History scans `*/run_config.json` below the output root. When `scores.json`
exists it shows overall score, question count, exact match, containment,
evidence retrieval, **RAG trigger rate, classifier fallback rate**, mean
latency, token totals, card count, Sleeptime failures, source run, QA mode,
and scope.

The first selected run is the baseline and the last is the primary comparison
target. The API joins questions by `question_id` and returns the
`official_score` delta plus each prediction. The UI sorts by ascending delta
so regressions appear first.

### Reading the retrieval metrics

`evidence retrieval rate` is averaged over **all** questions. Questions where
RAG never fired count as zero, so the number drops when the trigger rate drops
even if retrieval quality is unchanged. Always read it next to `rag_trigger`.

A high `classifier fallback rate` means the ContextClassifier fell over (empty
response or parse error), so `need_intent` was never set and RAG was skipped
entirely. The UI flags runs at or above 0.2. The usual cause is a Gatekeeper
model that emits thinking (Qwen3 and friends) with a small `max output tokens`:
the budget is spent before the classification JSON is written and the content
comes back empty. The start form warns about that combination up front
(2048 or more is recommended).
