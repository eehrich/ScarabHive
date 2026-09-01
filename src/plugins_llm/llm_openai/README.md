# OpenAI SDK Provider

api.openai.com through the official `openai` SDK, plus the Realtime session
adapter.

## What it provides

| Name | Kind | Module |
|---|---|---|
| `openai` | LLM | `openai_client.py` |
| `openai` | batch | see below |

```yaml
my-model:
  provider: openai
  model: gpt-5.6
  api_key: ${OPENAI_API_KEY}
```

`dependencies = ["openai>=1.37.0"]`, imported lazily in the factory.
`default_base_url` is declared in `plugin.toml` so the config resolver can
tell a base-URL-less entry apart from one that talks to a gateway.

## Where the batch backend actually lives

`provides_batch = ["openai"]` names this plugin, but the `/v1/batches`
implementation sits in `llm_openai_compat` — it is plain HTTP and never
touched the SDK. The factory here reaches it through the registry. The
naming rule is deliberate: a batch backend always carries the **same name as
the client it belongs to**, so `batch_provider: openai` pairs the SDK client
with this backend and `batch_provider: openai_httpx` pairs the httpx client
with its own. No mapping table anywhere.

## Realtime

`realtime_adapter.py` / `realtime_session.py` wrap the Realtime API for
voice-style sessions. They are not part of the LLM provider surface — the
registry never builds them; callers use them directly.

## `provider_routing` does not apply here

The gateway `provider` object (`{order: [...], allow_fallbacks: false}`) is
OpenRouter vocabulary. This SDK path does not forward it and would drop it
silently — the config field's docstring says so, and no catalogue entry uses
this provider with routing set. Models that need routing belong on
`openai_httpx` or `openai_responses`.

## Tests

`tests/` next to the code: client behaviour, vision input, and the
multimodal tool-result injection.

## License

Apache-2.0 — see `LICENSE`.
