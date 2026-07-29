# LoCoMo Evaluation Web Console

[日本語](evaluation_web_console.ja.md) | **English**

The Butly Web Console can launch, stop, resume, inspect, and compare LoCoMo
runs and the dedicated Japanese production-dialogue A/B.

`evals/locomo/` remains the source of truth. The Web API is a thin persistent
subprocess manager around the existing CLI; it does not duplicate Replay,
Sleeptime, QA, checkpoint, or scoring logic.

## Screens

Open the console from the home-screen `📊` button. Only the selected section is
rendered, so hidden forms do not fetch model candidates or run history.

| Section | Purpose |
|---|---|
| LoCoMo evaluation | Colab-parameter-equivalent run and model settings |
| Japanese dialogue A/B | Production-dialogue comparison of `intent_gated` and `candidates` |
| Jobs | Progress, phase, logs, stop, and checkpoint resume |
| LoCoMo history / compare | Saved-run metrics, two-to-eight-run comparison, question deltas |

The two-second Jobs refresh runs inside a Streamlit fragment and does not rerun
the evaluation forms. Model candidates are cached for ten minutes and are
refetched only through the explicit **Refresh model list** action.

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

The new-evaluation form automatically uses the last Web job's normalized
request as its initial state. This restores the dataset, run mode, source run,
scope, RAG, retrieval, Stage 3, role-model, temperature, and output-limit
settings even after Streamlit or the Backend restarts. To avoid collisions,
only `RUN_ID` changes: a trailing `_vNN` is incremented, otherwise a
time-based suggestion is generated. `allow_embedding_mismatch` is a hazardous
per-run acknowledgement and always resets to off.

### Japanese dialogue A/B

`data/ja_dialogue_ab_prompts_v1.json` contains ten memory seeds and thirty
prompts: ten that require memory, ten ordinary turns where memory is
irrelevant, and ten where memory may help.

The runner replays and knowledgeizes the seed once, optionally including
Stage 3. Every prompt then runs against a fresh disposable clone of that same
memory under two arms:

1. `retrieval_execution=always`, `injection_policy=intent_gated`;
2. `retrieval_execution=always`, `injection_policy=candidates`.

Prompts do not accumulate each other's requests or responses. Chat temperature
defaults to 0.0 and remains configurable in the Web form.

#### Seeding from a real instance

Instead of a synthetic seed, the run can **snapshot an existing instance** so
that token counts and memory-leak behaviour reflect real usage (real card
volume, System Instruction, and digest). Pick it under 記憶の種 in the form, or
declare it in the dataset:

```json
"memory_source": {"type": "instance", "name": "Jarvis"}
```

- The source instance is **read-only**; only the run-side copy is touched.
- Sleeptime is skipped, so cards and digests stay exactly as snapshotted.
- `debug_logs` / `traces` / `*.log` are not copied.
- Stored embedding vectors are reused. Enable 再embedding (`--reembed`) only
  when comparing a different embedding model.
- An empty `embedding_meta` is recorded as a warning in `seed_instance.json`
  (the vectors' origin model cannot be proven); confirm retrieval still works
  via the memory-required prompts.

Datasets in this form may express `prompts` as an object keyed by category.
`expected_memory_behavior` falls back to a per-category default, and evidence
cards can be referenced with `source_card_id`.

The automatic report records RAG and search rates, mean prompt tokens,
latency, target-term recall for memory-required prompts, and a seed-term
mention proxy for memory-irrelevant prompts. The last two are mechanical
proxies; reviewers use the side-by-side answers for the final naturalness and
over-personalization judgment.

Each prompt result is an atomic JSON artifact. Resume skips completed
`(policy, prompt_id)` pairs. If seed generation is interrupted, the runner
rebuilds the dedicated instance instead of trusting partial memory.

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
| `GET` | `/evaluations/config` | Output root, dataset candidates, run modes, and the last evaluation request |
| `POST` | `/evaluations/jobs` | Start a run |
| `GET` | `/evaluations/jobs` | List jobs |
| `GET` | `/evaluations/jobs/{job_id}` | Read status and progress |
| `POST` | `/evaluations/jobs/{job_id}/stop` | Stop the subprocess and children |
| `POST` | `/evaluations/jobs/{job_id}/resume` | Resume through the existing CLI |
| `GET` | `/evaluations/jobs/{job_id}/log` | Tail the combined log |
| `GET` | `/evaluations/runs` | List saved runs and headline metrics |
| `POST` | `/evaluations/runs/compare` | Compare metrics and questions for 2–8 runs |
| `POST` | `/evaluations/dialogue-ab/jobs` | Start the Japanese dialogue A/B |
| `GET` | `/evaluations/dialogue-ab/runs` | List Japanese dialogue A/B runs |
| `GET` | `/evaluations/dialogue-ab/runs/{run_id}` | Read policy and prompt results |

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
├─ dialogue_ab/
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

Japanese dialogue A/B output uses `BUTLY_DIALOGUE_AB_OUTPUT_DIR`, falling back
to `DATA_DIR/eval_runs/dialogue_ab/`.

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
