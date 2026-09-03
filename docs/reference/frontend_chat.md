# Official Desktop Chat UI

[日本語](frontend_chat.ja.md) | 🌐 **English**

This document defines Butly's official Tauri v2 + React + TypeScript chat UI and
its `/api/v1` contracts. Evaluation workflows such as LoCoMo, Japanese A/B, and
retrieval comparisons remain in the Streamlit Evaluation Web Console.

## Architecture

- Tauri manages the FastAPI sidecar lifecycle, dynamic port and token handoff,
  restart, and shutdown.
- React is split into instance, chat, and preflight feature slices under
  `frontend/src/features/`.
- Ordinary JSON endpoints use the generated OpenAPI TypeScript client and types.
- Only SSE framing is parsed manually in `frontend/src/api/sse.ts`, then mapped
  to the OpenAPI event DTOs.
- Japanese and English UI strings share one typed dictionary and can be switched
  at runtime.

## UI and states

The left pane selects an instance and shows connection/embedding preflight. The
main pane shows history, the streaming response, citations, and image
attachments. A collapsible diagnostic near the composer shows sanitized
Gatekeeper/RAG summaries only when developer mode allows them. UI DTOs never
expose prompts, raw generated
responses, API keys, connection URLs, or local paths.

The UI distinguishes at least these states:

- backend starting, connected, disconnected, reconnecting, sidecar crashed, and
  version mismatch;
- history loading, empty, failed, and retrying;
- queued, generating, completed, cancelled, retryable failure, and terminal
  failure;
- preflight ready, degraded, and unavailable.

Sidecar process state and HTTP reachability are separate. The UI continues to
probe the API after readiness, disables the composer on transport failure, and
offers reconnection without discarding the selected instance or draft.

### Memory-retrieval settings

The chat-header settings button opens a dialog for the selected instance. Basic controls cover search mode,
final card count, injected content, maximum RAW characters, cards expanded to RAW, and same-date RAW
neighbors. Advanced controls cover the Fusion Hybrid/Base weight, Evidence passage characters, and vector
and BM25 pool sizes.

A value is stored on the instance only when “Override for this instance” is enabled. Disabling it sends
`null` and restores global inheritance. The dialog displays effective values and their source. Fusion fields
are disabled outside Fusion mode, RAW fields are disabled for cards-only injection, and the BM25 pool is
disabled in vector mode, without deleting preserved values. A zero RAW character limit is available only
through an explicit Unlimited toggle and is labeled as a character—not token—budget with a growth warning.
Neighbor choices are displayed as None / ±1 … ±10 while the persisted value is a non-negative radius.

The dialog reads and writes `GET/PATCH /api/v1/instances/{name}/settings/memory-retrieval`. It rebuilds
local state from the successful backend response and never leaves an optimistic success state after an
error. The global resource is `GET/PATCH /api/v1/settings/memory-retrieval`.

## Chat API

History uses `GET /api/v1/instances/{name}/messages`, non-streaming fallback uses
`POST /api/v1/chat`, and the official UI generates through
`POST /api/v1/chat/stream`. The UI assigns a `client_request_id` to each send.
Repeating the same ID and payload attaches to a running request or replays its
completed events. Reusing the ID with a different payload returns
`409 idempotency_conflict`. An explicit retry after failure or cancellation
creates a new attempt under the same client ID.

A successful SSE sequence is `metadata`, zero or more `chunk` events, then
`done`. Every event has the same `request_id`, chunk sequences increase, and
`done.full_text` equals concatenated chunks. Providers that cannot stream while
using features such as Gemini Google Search return the completed text as one
chunk; this is the buffered fallback. `error` is terminal and is
never followed by `done`. The parser supports LF and CRLF, UTF-8 boundaries,
multiple `data:` lines, and split frames, and releases the reader on abnormal
termination.

`GET /api/v1/chat/requests/{request_id}` reports process-local state and
`POST /api/v1/chat/requests/{request_id}/cancel` requests cancellation. Before a
request ID arrives, the client aborts its transport; afterwards it asks the
server to cancel before closing the transport. Generation starts only after the
first SSE subscriber connects. Once persistence enters its final commit section,
cancellation is declined. These rules prevent orphaned generation after early
disconnects and duplicate turns when a committed request is retried.

When a provider SDK runs synchronous work in a worker thread, cancelling the
async task stops Butly event delivery and persistence but may not immediately
stop the provider-side request. This limitation is separate from the UI's
cancelled persistence state.

## Preflight and capabilities

`GET /api/v1/preflight` checks connections needed by the active chat and
embedding roles. Ollama uses its native model list; other connections use a safe
protocol-specific model probe. Embedding preflight embeds a fixed short string
and requires a non-empty, finite vector. Results may include status, reason
codes, latency, model IDs, and embedding dimension, but never secrets, base URLs,
or raw provider errors.

Overall status is `ready`, `degraded`, or `unavailable`, based on required roles.
One unavailable connection does not hide unrelated working features. Image,
Google Search, generic Web Search, and developer-debug controls are gated by
`GET /api/v1/capabilities`, derived from the active model and connection.

Developer debug is available only when the sidecar runs in developer mode.
Sending `include_debug=true` otherwise returns `403 debug_not_available`.
The UI displays only summaries such as Gatekeeper tier, need, scores, and
fallback, plus RAG candidate/injection counts and active-node identifiers.

### Trace graph (issue #51)

The debug panel can show the response-generation flow of the latest turn as a
Mermaid flowchart. `GET /api/v1/instances/{name}/trace` returns a Mermaid string
where Gatekeeper, RAG, Context Assembly, Provider, LLM, and Memory Write are
colored by active / skipped / fallback / error; the frontend only renders it.

- **The backend owns Mermaid generation** (`butly_core/trace/mermaid.py`); the
  frontend never rebuilds it. The source is the stored `traces/latest.json`.
- **TraceNode `metadata` is never returned.** It holds original queries and
  retrieval candidates, so the response exposes only the Mermaid string built
  from labels/summaries plus per-status node counts. Summaries are trimmed to 80
  characters so a full response body never lands in the graph.
- Same developer-mode gate as chat debug: `403 debug_not_available` when
  disabled, `404 trace_not_found` when the instance has no recorded trace.
- The renderer runs Mermaid with `securityLevel: "strict"` and
  `htmlLabels: false` so labels are never interpreted as HTML, and imports
  Mermaid dynamically when the panel is opened to keep it out of the initial
  bundle.
- Graphs outgrow the panel, so "expand" opens a full-window overlay (closed with
  Escape or a backdrop click). A permanent third column is deliberately avoided:
  it would take chat width forever and still leave the graph narrow.
- `direction` (`TD` / `LR`) switches the layout, because a tall graph never fits
  a wide window. Generation stays with `render_mermaid(direction=...)`; the
  frontend never rearranges the SVG.
- After insertion the viewBox is rebuilt from the measured bounding box and
  Mermaid's baked-in `max-width` is removed. With `htmlLabels: false` the text
  metrics can be off, and labels falling outside the computed viewBox would be
  clipped.

## Attachments, citations, and safety

- Images may be JPEG, PNG, or WebP: at most three and 20 MB decoded per image.
  Requests contain base64 without a data-URL header. Images can be attached by
  **pasting into the composer** as well as by file selection; both go through the
  same count/size/MIME validation. Clipboard images sometimes have no file name,
  in which case `pasted-image.<ext>` is supplied.
- Only `http:` and `https:` citation URLs are displayed. The default action
  **opens them in the OS browser** (`shell:allow-open` under Tauri, a new tab in
  browser dev) and copying stays as a secondary action; the webview never
  navigates to them.
- Assistant responses render as Markdown. **Raw HTML is never rendered** and
  remote images are not loaded (they degrade to links). User messages are shown
  verbatim, without Markdown interpretation.
- Backend strings render as React text nodes and are never inserted as HTML.
- The desktop token stays in lifecycle memory and is never logged or persisted.
- The UI exposes public error codes and request IDs, not raw provider errors.

## Verification

Frontend unit tests cover split SSE frames, buffered fallback, errors, abort,
state transitions, retry, capability gates, and i18n. Backend contract tests
cover preflight, debug authorization, cancellation/idempotency, and secret
redaction. Mock E2E tests cover both native multi-chunk and Gemini-buffered flows:
history load, send, stream, persistence, and history reload must agree.

`./scripts/check_before_push.sh` is the canonical local check. Real-key
`-m integration` tests are not part of the normal Phase 2 verification.
