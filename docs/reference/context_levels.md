# context_levels Specification

🌐 [日本語](context_levels.ja.md) | **English**

`context_levels` lets you assign one of four levels — `high` / `mid` / `low` / `off` — to each context section passed to the LLM. Presets (`normal` / `compact` / `low` / `custom`) toggle them all at once, which is useful for shrinking the prompt for small local LLMs or optimizing API costs.

---

## Preset overview

| Preset | Use case | Notes |
|---|---|---|
| `normal` | API / default | All sections at `high`; maximum information. |
| `compact` | Mid-size local LLMs | `label_notes` and `glossary` off, `key_memory` low. |
| `low` | ~7B-class local LLMs | No headers / annotations; aggressively compressed. |
| `custom` | Per-section | Mix freely. |

---

## Per-section output

### `system_instruction` (personality)
| Level | Output |
|---|---|
| `high` | `=== SYSTEM INSTRUCTION ===\n{system_instruction.txt full text}` |
| `low`  | `{system_instruction_low.txt}` — no header. Falls back to the normal file if absent. |
| `off`  | Cannot turn off; falls back to the normal file. |

### `key_memory` (core memory)
| Level | Output |
|---|---|
| `high` | `=== KEY MEMORY ===\n{Key_Memory.txt full text}` |
| `low`  | `[Conversation core] {Key_Memory_low.txt}` — no header. Falls back to the normal file if absent. |
| `off`  | Omitted. |

### `label_notes` (labels / advisories)
| Level | Output |
|---|---|
| `high` | `[Background information]\n※ Priority order: ...` |
| `low`  | Omitted (same as off). |
| `off`  | Omitted. |

### `current_time`
| Level | Output |
|---|---|
| `high` | `=== CURRENT TIME ===\n{timestamp}\n※ Reference info for context...` |
| `low`  | `{single-line timestamp}` (e.g. `2026-04-04 21:00 (Fri)`) |
| `off`  | Omitted. |

### `glossary`
| Level | Output |
|---|---|
| `high` | `=== GLOSSARY ===\n{all entries}` |
| `low`  | Omitted (same as off). |
| `off`  | Omitted. |

### `mid_term`
| Level | Output |
|---|---|
| `high` | `=== MID-TERM DIGEST ===\n※ The following is...\n{full text}` + relationship snapshot |
| `low`  | `[Recent background]\n{last 4 lines}` — no header / annotations. |
| `off`  | Omitted. |

> In `low`, lines are taken from the tail to avoid mid-sentence cuts.

### `rag` (long-term memory)
| Level | Output |
|---|---|
| `high` | `=== LONG-TERM MEMORY (RAG) ===\n※ The following is...\n{all results}` |
| `low`  | `[Related memory]\n{first 3 lines}` — no header / annotations. |
| `off`  | Omitted. |

> In `low`, lines are taken from the head to keep complete sentences.

### `session_digest` (session digest)
| Level | Output |
|---|---|
| `high` | `=== SESSION DIGEST ===\n{full text with relative-time headers}` |
| `low`  | Omitted (same as off). Recent dialog summary substitutes. |
| `off`  | Omitted. |

### `tier_info`
| Level | Output |
|---|---|
| `high` | `=== TIER INFO ===\nCurrent mode: {tier}\nCurrent topic: {topic}` |
| `low`  | Omitted (same as off). Small models won't use it meaningfully. |
| `off`  | Omitted. |

### `web_search`
| Level | Output |
|---|---|
| `high` | `=== WEB SEARCH RESULTS ===\n{all results}` |
| `low`  | `{results-only, ~300 chars, no header}` |
| `off`  | Omitted. |

---

## LOW-version files

The `low` levels of `system_instruction` and `key_memory` read a dedicated simplified file.

### Layout

```
instances/{name}/
├── system_instruction.txt       ← normal version (existing)
├── system_instruction_low.txt   ← LOW version (simplified)
├── Key_Memory.txt               ← normal version (existing)
└── Key_Memory_low.txt           ← LOW version (simplified)
```

### Fallback rule

1. If `_low.txt` exists AND has any non-comment (`#`) lines → use it.
2. Otherwise → use the normal version.

> When an instance is created, `_low.txt` is auto-generated as a comment-only template. Leaving the comments untouched falls back to the normal version automatically.

---

## Prompt composition examples

### `normal` preset (API-friendly, full info)

```
[system message]
=== SYSTEM INSTRUCTION ===
I am an entity that stays close to the user and provides comfortable dialogue.
My purpose is to support the user so they can think and act with peace of mind.
I use a polite, soft tone and value warmth and familiarity.
(... full text ...)

=== KEY MEMORY ===
AI name: Luna
User name: User
Address form: Master

[user message - context prefix]
[Background information]
※ The following supports the current conversation.
※ Priority order: recent history > ...

=== CURRENT TIME ===
2026-04-04 21:00 (Fri)
※ The timestamp above is for context.

=== GLOSSARY ===
- Luna: AI name. Friendly, warm dialog style.

=== MID-TERM DIGEST ===
※ The following is the AI's subjective memory.
(... full text ...)

=== RELATIONSHIP SNAPSHOT ===
(... full text ...)

=== SESSION DIGEST ===
--- about 30 minutes ago ---
We talked about the weather.

=== TIER INFO ===
Current mode: mid
Current topic: hobby development and AI interest
```

---

### `low` preset (small LLMs, minimal)

```
[system message]
Stay close to the user and provide comfortable dialogue. Warm, polite tone.
[Conversation core] AI: Luna / User: Yuki (Master) / Role: partner extending thought

[user message - context prefix]
2026-04-04 21:00 (Fri)

[Recent background]
- Yuki and AI Luna are running a dialogue test on the Butly platform.
- Discussing hobby development and local LLM optimization.
- AI development is the user's hobby and a way to refresh.
- Lives in Miyazaki, busy period at the start of the fiscal year.

[Related memory]
- For Streamlit UI implementation, agreed on combining Selectbox and custom input.
- DEBUG mode implementation completed.
- Resolved an issue with broken Ollama model template setting.

[history - recent turns]
[current user input]
```

---

## Tier branching when Gatekeeper is OFF

Using the `low` preset, Gatekeeper-OFF is recommended (a warning is shown in the UI). The behavior is:

| Gatekeeper | RAG | tier | Description |
|---|---|---|---|
| ON | — | dynamic | Default. Even in LOW mode, ON is allowed. |
| OFF | ON | `mid` (fixed) | Sets `need="rag_search"` and always performs RAG search. |
| OFF | OFF | `mid` (fixed) | No RAG. |

---

## How to configure

### Instance settings screen (`app.py`)

1. Open the instance settings screen.
2. In "🧩 Context injection" pick a preset.
3. When picking `low`, disabling the Gatekeeper is recommended.
4. With "Custom" you can change each section individually.
5. The "Order" setting lets you reorder sections.

### `config.json` (direct edit)

```json
{
  "context_levels": {
    "preset": "low",
    "levels": {
      "system_instruction": "low",
      "key_memory": "low",
      "label_notes": "off",
      "current_time": "low",
      "glossary": "off",
      "mid_term": "low",
      "rag": "low",
      "session_digest": "off",
      "tier_info": "off",
      "web_search": "high"
    },
    "order": {
      "system_instruction": ["system_instruction", "key_memory"],
      "context_prefix": ["label_notes", "current_time", "glossary", "mid_term", "rag", "session_digest", "tier_info", "web_search"]
    },
    "system_instruction_position": "top"
  }
}
```

### Backward compatibility

Configs in the old `context_order` form are auto-migrated to `context_levels` on first chat (`preset: "custom"`, old-ON → `high`, old-OFF → `off`).

---

## Future extensions

- **`mid` level**: middle output for 13B–30B-class models (headers but no annotations, etc.)
- **`glossary low`**: extract ~3 entries by direct string match against user input.
- **Auto-generation of `_low.txt`**: confirmation flow that summarizes a simplified version with a summary model.
- **Dynamic context budgeting**: auto-pick a preset by detecting the model's context length.
- **More presets**: `ultra-low` (3B-class), `api-optimized` (cost-optimized), etc.
