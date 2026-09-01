# LLM Common

Shared wire-format helpers for the LLM provider plugins. **A library, not a
provider** — `type = ["library"]`, no `provides`, so the LLM registry skips
this package entirely. The other `plugins_llm/*` packages import it directly.

## What lives here

| Module | Purpose |
|---|---|
| `api_keys.py` | `resolve_api_key()` — the key follows the ENDPOINT |
| `openai_utils.py` | OpenAI wire-format helpers (e.g. audio → `input_audio`) |
| `schema_sanitize.py` | `sanitize_schema_for_gemini()` — strips JSON Schema keywords Gemini's function declarations reject |

## Why these three and not more

Something belongs here when **two or more** provider plugins would otherwise
copy it:

* `openai_utils` — both `llm_openai` and `llm_openai_compat` speak the OpenAI
  wire format.
* `schema_sanitize` — Gemini reachable natively (`llm_gemini`) and through a
  gateway (`llm_openai_compat`) needs the same schema surgery, and a second
  copy would drift the moment one route hits a new rejected keyword.
* `api_keys` — the rule "an env fallback must not hand the OpenAI secret to
  whatever host a model entry names" has to be the same rule everywhere.

Anything used by exactly one plugin stays in that plugin.

## Why it has a plugin.toml at all

It declares no provider, but the manifest makes the package visible to
`scripts/aggregate_plugin_deps.py` and to the import-declaration guard
(`tests/pluginsystem/test_plugin_deps_declared.py`), so it is covered like
every other plugin.

## License

Apache-2.0 — see `LICENSE`.
