# Anthropic LLM Provider

Claude via the official `anthropic` SDK, plus the Message Batches backend.

## What it provides

| Name | Kind | Module |
|---|---|---|
| `anthropic` | LLM | `anthropic_client.py` |
| `anthropic` | batch | `anthropic_batch.py` |

```yaml
my-model:
  provider: anthropic
  model: claude-opus-4-8
  api_key: ${ANTHROPIC_API_KEY}
  batch_provider: anthropic     # optional, same name as the client
```

`dependencies = ["anthropic>=0.40.0"]` — the SDK import is lazy inside the
factory, so a missing package surfaces at `registry.build_client()` with the
plugin named, not as a bare `No module named 'anthropic'` during bootstrap.

## Prompt caching

`cache_control: {"type": "ephemeral"}` markers are placed by the shared
policy in `agent_system/llm/cache_key.py` — **not** here and not in agent
YAMLs. Anthropic allows at most four breakpoints per request, and the markers
this client sets on the system prompt and the tool block count against that
budget, so the cap is enforced centrally. Details:
`docs/prompt_cache_design.md` §3.4.

## SSL

`test_anthropic_ssl_verify.py` pins that `ssl_verify=False` reaches the SDK's
transport — corporate proxies need it, and the SDK does not accept the flag
the way httpx does.

## Tests

`tests/` next to the code: client behaviour, thinking round-trip, batch
submit/cancel, and the utils.

## License

Apache-2.0 — see `LICENSE`.
