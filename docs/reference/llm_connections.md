# LLM Connections and API-key management

🌐 [日本語](llm_connections.ja.md) | **English**

> Last updated: 2026-07-26

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
| `GET` | `/settings/model_candidates?role=...` | Return role-specific `(connection_id, model_name)` candidates |

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

## NanoGPT

### Separate pay-as-you-go and Pro

Register NanoGPT as two Connections:

- `nanogpt`: pay-as-you-go at `https://nano-gpt.com/api/v1`
- `nanogpt-sub`: Pro/subscription-only models at
  `https://nano-gpt.com/api/subscription/v1`

Both use `NANOGPT_API_KEY`. Saving it from either row configures both;
clearing it from either row affects both.

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

### Official NanoGPT documentation

- [Text Generation](https://docs.nano-gpt.com/api-reference/text-generation)
- [Chat Completion](https://docs.nano-gpt.com/api-reference/endpoint/chat-completion)
- [Models](https://docs.nano-gpt.com/api-reference/endpoint/models)
- [Embeddings](https://docs.nano-gpt.com/api-reference/endpoint/embeddings)
- [Pay-As-You-Go Billing Override](https://docs.nano-gpt.com/api-reference/miscellaneous/billing-override)
