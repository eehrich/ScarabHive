# Ollama Provider

Local models through Ollama, in either of two modes.

## What it provides

| Name | Kind | Module |
|---|---|---|
| `ollama` | LLM | `ollama_client.py`, `ollama_utils.py` |

```yaml
my-model:
  provider: ollama
  model: llama3.3
  base_url: http://localhost:11434
  ollama_mode: native          # native | openai_compat (default)
  context_window: 32768        # becomes num_ctx
  thinking_level: high         # native only: becomes `think` (none = off)
  max_tokens: 4096             # native only: becomes num_predict
```

No pip dependency: `native` mode talks to Ollama's HTTP API with httpx, which
is a core requirement.

## The two modes

* **`native`** — Ollama's own `/api/chat`. Needed for `num_ctx` and the other
  options Ollama exposes outside the OpenAI dialect.
* **`openai_compat`** (default) — Ollama's OpenAI-compatible endpoint. The
  factory does **not** import `llm_openai` directly; it delegates through the
  registry to the `openai` provider. Plugin-to-plugin imports would make the
  load order matter, and the registry is the seam that already answers "who
  serves this name". Consequence: this mode needs `llm_openai` present.

## Thinking

A thinking model's reasoning arrives in `message.thinking`, beside the answer.
`native` streams it as `thinking_delta` and stores it as `reasoning_content`,
like every other route, and sends it back as `thinking` on the next request.
`thinking_level` maps onto Ollama's three levels (`minimal` -> `low`,
`xhigh`/`max` -> `high`, `none` -> `false`); without it the model decides.
`thinking_budget` has no Ollama counterpart. Some models ignore `think: false`
(qwen3-vl always thinks; the `qwen3:4b` tag then thinks in the answer text).

`openai_compat` gets no `num_ctx`: Ollama's `/v1` ignores it, and the model
loads with the server's `OLLAMA_CONTEXT_LENGTH` instead -- on a small GPU that
can mean most of the model runs on the CPU.

## Tests

`tests/` next to the code: client behaviour and the option/format helpers.

## License

Apache-2.0 — see `LICENSE`.
