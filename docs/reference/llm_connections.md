# LLM Connections and API-key management

🌐 [日本語](llm_connections.ja.md) | **English**

> Last updated: 2026-08-16

## Overview

Butly stores a model separately from the endpoint used to call it:

- `Connection`: endpoint, protocol, and authentication environment
- `model_name`: model ID sent to that endpoint
- `ModelRef`: the `connection` and `model_name` pair

This keeps routing unambiguous when multiple services expose the same model ID.
The legacy `{"model_name": "..."}` form remains compatible, but new settings
must persist both values.

```json
{
  "AI_CONFIG": {
    "chat": {
      "connection": "nanogpt-sub",
      "model_name": "Qwen/Qwen3-14B"
    }
  }
}
```

## LLM requests and capability resolution

Generation calls cross a protocol boundary instead of branching on model-name
substrings:

`Butly Core → CanonicalGenerationRequest → Protocol Adapter → Provider SDK`

- Core uses only canonical fields such as `temperature`, `max_output_tokens`,
  and `reasoning_effort`.
- `openai_compat` maps those fields to Chat Completions, including the
  `max_tokens` / `max_completion_tokens` distinction.
- `gemini_native` maps them to `GenerateContentConfig`, JSON Schema, and
  Thinking Config.
- Chat, summary, classification, and streaming share this conversion path.
  Embeddings are outside this generation contract.

Capabilities are overlaid field by field, with later sources taking priority:

1. protocol-Adapter defaults
2. Butly's static model preset
3. provider metadata or `supported_parameters` returned by `/models`
4. an observed setting from a successful automatic correction
5. a manual `LLM_CAPABILITY_OVERRIDES` entry

Missing or incomplete metadata does not cause Butly to guess from a model-name
substring. An unspecified parameter may instead use the provider's official
default. For the semantic judge, an omitted `reasoning_effort` uses the
capability's advertised default, or `medium` when reasoning support alone is
known. If the capability itself is unknown, the parameter is omitted and the
provider default applies. An explicit user value always takes precedence over
this automatic policy.

### Observed cache and one safe correction

An OpenAI-compatible endpoint is corrected only when a 400 response explicitly
identifies an `unsupported_parameter` or `unknown_parameter`. Before any output,
Butly may switch the `max_tokens` / `max_completion_tokens` alias, or omit a
non-user-specified `temperature` or `reasoning_effort`, and retry exactly once.
Ambiguous failures, authentication, 429s, and removal of explicit user values
are not eligible.

Only a successful corrected call is saved atomically under Connection + the
model ID actually sent to the API in `DATA_DIR/llm_capabilities.json`. This
git-ignored file contains no API keys or prompts. A failed second call is not
saved. Changing/deleting a Connection or explicitly refreshing its model list
invalidates the affected cache.

### Per-model manual override

Use `user_config.json` when neither metadata nor safe correction is available.
The model key is the ID sent to the API after `model_name_strip_prefix` removal.

```json
{
  "LLM_CAPABILITY_OVERRIDES": {
    "nanogpt-sub": {
      "gpt-5.6-luna": {
        "token_limit_parameter": "max_completion_tokens",
        "supports_reasoning": true,
        "reasoning_efforts": ["none", "low", "medium", "high", "xhigh", "max"],
        "default_reasoning_effort": "medium",
        "temperature_supported": false,
        "structured_outputs_supported": true
      }
    }
  }
}
```

Specify only the fields that need overriding. Valid `token_limit_parameter`
values are `max_tokens`, `max_completion_tokens`, and `max_output_tokens`.

## Connection fields

User-defined Connections are stored in `user_config.json["LLM_CONNECTIONS"]`.
API-key values are never stored there.

| Field | Type / default | Purpose |
|---|---|---|
| `id` | string / required | Identifier: starts with a lowercase letter or digit, then lowercase letters, digits, `_`, or `-`; 64 characters maximum |
| `protocol` | string / required | `openai_compat` or `gemini_native` |
| `base_url` | string or null | Absolute URL used by the SDK or Adapter |
| `base_url_env` | string or null | Environment variable that overrides `base_url` when set |
| `api_key_env` | string or null | Environment variable containing the API key; `null` for unauthenticated services |
| `api_key_fallback_envs` | string[] / `[]` | Alternative key variables checked in order |
| `label` | string or null | Web Console label; defaults to `id` |
| `extra_headers` | object / `{}` | Fixed headers added to requests. Do not put secrets here |
| `embeddings_supported` | boolean / `true` | Whether this Connection may be used for embeddings |
| `embedding_model_env` | string or null | Environment variable that overrides the embedding model ID |
| `default_embedding_model` | string or null | Final embedding-model fallback |
| `model_name_strip_prefix` | string or null | Prefix stripped from a model ID before the API call |

`google`, `openai`, `xai`, and `ollama` are built in and cannot be overwritten
or deleted. An OpenAI-compatible service normally needs only a user-defined
`openai_compat` Connection, not a new Provider class.

To point a built-in Connection somewhere else, use its `base_url_env`. For an
Ollama server on another machine, save the URL under **Ollama (local LLM)** in
Settings; it is written to `OLLAMA_BASE_URL` in `DATA_DIR/.env` (`POST
/settings/ollama_url`). The UI and the connection test use the root form
(`http://<host>:11434`) and the OpenAI-compatible `/v1` suffix is added on save.
`Connection.resolve_base_url()` reads the environment on every call, so no
restart is needed.

## Web Console workflow

### Connections and API keys

1. Open **Connection / API key management** in Settings.
2. Review existing Connections, key status, and endpoints.
3. Use **Add Connection** and select a template or a custom endpoint.
4. Save the API key from the newly registered Connection's row.
5. Run the connection test and confirm that the model catalog is reachable.

The password field is always empty on load. Butly displays only whether a key
is configured and never reads a stored key back into the UI.

### Select provider, then model

For every global and instance role, choose:

1. Provider / Connection
2. A model offered through that Connection

Candidates combine built-in presets, the currently saved value, and the
endpoint's `/models` response. Use the direct model-ID field when a model is not
listed. Butly saves both `connection` and `model_name`.

The selected Connection's provider catalog is loaded lazily and cached for ten
minutes. Role-specific candidate requests reuse that catalog instead of calling
the external `/models` endpoint for every role. Connection, API-key, and Ollama
URL changes invalidate the affected cache automatically. Use **Refresh model list** or
`POST /settings/model_catalog/refresh` to pick up provider-side changes
immediately.

Connections with `embeddings_supported=false` are excluded from the Embedding
role. After changing an embedding Connection or model, regenerate existing
vectors with `migrate_embeddings.py` because their dimensions may differ.

## Secret storage

The backend writes API keys to the runtime `DATA_DIR/.env` and immediately
updates the current process environment. It preserves unrelated variables,
comments, and blank lines, and collapses duplicate definitions of the target
variable.

The client sends only a Connection ID and key value. The server resolves the
allowed environment-variable name from the registered Connection, so the
secret API cannot be used to choose an arbitrary environment variable.

Responses and Connection listings never return a secret value. They expose
only `api_key_set` and `affected_connections`. Never place keys in
`user_config.json`, logs, screenshots, issues, or commits.

When Connections share an `api_key_env`, saving from one affects all of them.
Clearing a key removes the selected Connection's primary and fallback key
variables, so shared Connections are affected as well.

Deleting a Connection and clearing its key are separate operations. To remove
an unused secret too, first confirm that no other Connection shares the
variable, then clear it before deleting the Connection.

## Validation

Both registration and configuration loading enforce:

- `id`: `^[a-z0-9][a-z0-9_-]{0,63}$`
- environment names: `^[A-Z_][A-Z0-9_]*$`
- reserved runtime variables such as `HOME`, `PATH`, and `PYTHONPATH` are denied
- `base_url`: absolute `http://` or `https://` URL
- `protocol`: `openai_compat` or `gemini_native`
- `extra_headers`: string keys and values with no newlines
- API keys: non-empty and contain no control characters

A user-defined Connection cannot reuse a built-in ID. The current custom form
in the Web Console creates `openai_compat` Connections.

## Reference-safe deletion

Deleting a user-defined Connection returns `409 Conflict` while it is
referenced by global `AI_CONFIG` or an instance `config.json`. Reassign those
roles first.

`DELETE /settings/connections/{connection_id}?force=true` bypasses the guard,
but it does not rewrite references. This leaves invalid `ModelRef` settings and
should be reserved for controlled migrations.

## Official desktop preflight

The official UI uses read-only `GET /api/v1/preflight` to determine whether the
active chat and embedding roles are actually usable. This startup diagnostic is
separate from the legacy Connection-management API that mutates settings.

- Connections required by active roles are checked concurrently with timeouts.
- Ollama (`openai_compat`) uses native `/api/tags` against its configured root.
- Other Connections use a safe protocol-specific model-list probe.
- Embedding preflight performs a real fixed-text embedding, requiring a non-empty,
  finite vector and reporting its dimension instead of relying only on a model name.
- Results expose `ready`, `degraded`, or `unavailable` plus stable reason codes,
  but never API keys, base URLs, headers, or raw provider errors.

Preflight neither saves nor changes a Connection and is not a credential source
of truth. Settings remain in the Streamlit legacy API until the versioned
settings UI is implemented.

## Legacy Settings API

These unversioned compatibility routes are currently used by the Streamlit Web
Console.

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/settings/connections` | List built-in and user Connections with key status |
| `GET` | `/settings/connection_templates` | List provider templates without secrets |
| `POST` | `/settings/connections` | Add a user Connection or update the same ID |
| `DELETE` | `/settings/connections/{connection_id}` | Delete a user Connection; 409 while referenced |
| `POST` | `/settings/connections/{connection_id}/api_key` | Save `{"api_key": "..."}` |
| `DELETE` | `/settings/connections/{connection_id}/api_key` | Clear key variables used by the Connection |
| `POST` | `/settings/test_connection` | Test a Connection and list models |
| `GET` | `/settings/model_candidates?role=...&connection_id=...` | Return role candidates; optional `connection_id` limits dynamic discovery |
| `POST` | `/settings/model_catalog/refresh` | Invalidate all or one Connection's model-catalog cache |

Secret create/delete responses never contain the key value.

## Provider templates

Templates only prefill registration fields. Once registered, they are ordinary
user-defined Connections.

| ID | Base URL | API-key variable | Embeddings |
|---|---|---|---|
| `nanogpt-sub` | `https://nano-gpt.com/api/subscription/v1` | `NANOGPT_API_KEY` | No |
| `nanogpt` | `https://nano-gpt.com/api/v1` | `NANOGPT_API_KEY` | Yes |
| `groq` | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` | No |
| `openrouter` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` | No |
| `together` | `https://api.together.xyz/v1` | `TOGETHER_API_KEY` | Yes |
| `deepinfra` | `https://api.deepinfra.com/v1/openai` | `DEEPINFRA_API_KEY` | Yes |

External URLs, catalogs, and pricing can change. Verify each service's current
documentation before registering it.

## Immediate streaming retry

When an OpenAI-compatible stream fails **before emitting any output**,
`butly_core/llm/_openai_compat.py` re-issues it **once** (`MAX_STREAM_ATTEMPTS`).
Upstreams have been observed opening the SSE stream and closing it without a
body (`Upstream returned an empty response`); the same input succeeds on a
manual resend, so the failure is transient. A manual resend restarts from
Gatekeeper classification, which makes retrying at the provider layer the
faster path.

Only failures that barely add to the wait are retried
(`is_retryable_stream_error`).

| Failure | Retried | Why |
|---|---|---|
| Empty upstream response (`APIError` without status) | yes | Fails instantly and usually succeeds on the next attempt |
| Connection drop (`APIConnectionError`) | yes | Same |
| 5xx (`InternalServerError`) | yes | Transient upstream fault |
| **Timeout (`APITimeoutError`)** | **no** | Doubles the wait — the worst case for perceived latency |
| 429 / 401 / ordinary 400 (`APIStatusError`) | no | An immediate retry is useless or harmful |

**No retry happens once any text has been emitted**, which is what keeps a
retry from producing duplicate output; the caller checks that `full_text` is
still empty. Only a second failure emits the `error` event, so the contract
seen by the UI (`metadata` → `chunk` → `done`, or a terminal `error`) is
unchanged. Retries appear only in the server log.

This is separate from the LoCoMo QA retry (three attempts with 1/2/4s backoff);
conversation favors not keeping the user waiting, so it retries once,
immediately.
The clear unsupported-parameter correction shares the same before-output,
two-attempt limit. If a semantic-judge configuration error remains after that
correction, the evaluation run stops immediately instead of repeating the same
failure for every question.

## NanoGPT

### Separate pay-as-you-go and Pro

Register NanoGPT as two Connections:

- `nanogpt`: pay-as-you-go at `https://nano-gpt.com/api/v1`
- `nanogpt-sub`: Pro/subscription-only models at
  `https://nano-gpt.com/api/subscription/v1`

Both use `NANOGPT_API_KEY`. Saving it from either row configures both;
clearing it from either row affects both.

NanoGPT may expose multiple route/model IDs with the same display name.
Subscription eligibility follows the exact `id` returned by
`/api/subscription/v1/models`, not the display name. Do not substitute a
same-named ID that appears only in `/api/paid/v1/models`.

### Do not add pay-as-you-go overrides to Pro

`nanogpt-sub` exists to keep calls on subscription-covered routing. Do not add
the following, because they can force pay-as-you-go billing:

- `X-Provider`
- `X-Billing-Mode: paygo`
- body `billing_mode` / `billingMode`
- provider or routing suffixes such as `:fast` and `:cheap`

Butly's `nanogpt-sub` template keeps `extra_headers={}` and sets
`embeddings_supported=false`.

### Enter an exact model ID for PAYG embeddings

NanoGPT's regular `/api/v1/models` catalog lists text-generation models. The
source of truth for embeddings is `/api/v1/embedding-models`, so NanoGPT
embedding models may not appear automatically in Butly's Embedding picker.

Select the `nanogpt` Connection and paste the exact `id` returned by
`/api/v1/embedding-models` into the direct model-ID field. Examples include
`text-embedding-3-small`, `BAAI/bge-m3`, and
`Qwen/Qwen3-Embedding-0.6B`. Do not add a Butly-specific `nanogpt/` prefix.

Availability and pricing change; the examples are not availability guarantees.

### Do not truncate large model catalogs

NanoGPT's text-model catalog can contain more than 200 entries. Butly caches
the complete `/models` response per Connection and exposes all of it to the
selected Connection's picker, including models late in the provider's order.

### Official NanoGPT documentation

- [Text Generation](https://docs.nano-gpt.com/api-reference/text-generation)
- [Chat Completion](https://docs.nano-gpt.com/api-reference/endpoint/chat-completion)
- [Models](https://docs.nano-gpt.com/api-reference/endpoint/models)
- [Embeddings](https://docs.nano-gpt.com/api-reference/endpoint/embeddings)
- [Pay-As-You-Go Billing Override](https://docs.nano-gpt.com/api-reference/miscellaneous/billing-override)
