# OpenRouter SDK Provider

OpenRouter's official Python SDK as a second route to the same `/responses`
endpoint `llm_openai_compat` already serves. Built to be **compared** against
it, not to replace it unmeasured.

## What it provides

| Name | Kind | Client |
|---|---|---|
| `openrouter_sdk` | LLM | `OpenRouterSDKClient` |

```yaml
my-model:
  extends: openrouter-base
  provider: openrouter_sdk        # instead of openai_responses
```

Requires `pip install openrouter` (declared in `plugin.toml`, folded into
`requirements/all.txt`). No model entry uses this provider today — until a
comparison decides otherwise, it is an experiment.

## How it differs — and how it deliberately does not

`OpenRouterSDKClient` **inherits** from `OpenAIResponsesClient` and overrides
exactly one method, `_post`. Payload building, cache breakpoints, tool
conversion, response parsing, the retry/healing loop and hook notification
are the sibling's, unchanged. A comparison therefore measures the transport
and nothing else.

The request goes out typed through the SDK. The **response is read from the
raw body**, for two reasons:

1. The inherited loop needs the raw body anyway — it decides on status codes,
   detects body-level errors inside an HTTP 200, and matches
   reasoning-artifact rejections against the body text.
2. The typed result can lose data silently: `usage` is `OptionalNullable` and
   its `UsageCostDetails` requires `upstream_inference_input_cost` /
   `_output_cost`. Without them the whole `usage` block degrades to
   `Unset()` — tokens, cost and cache hits gone, no error. Reproduced against
   openrouter 1.1.108; never seen on a live response, and a test pins that
   premise so it goes red if a future SDK fixes it.

## Known gaps

| Field | Behaviour |
|---|---|
| `prompt_cache_marker_style: anthropic` | **refused at construction** — the httpx route sends per-part `cache_control`, this one cannot, and losing it costs cache hits with no error |
| `safety_settings` | warns only — OpenRouter drops the field on `/responses` either way, so refusing would invent a difference that does not exist |

The SDK also substitutes one default: it always sends `service_tier: "auto"`,
so the flex-tier drop sends the standard tier explicitly where the httpx
route omits the key.

## Tests

`tests/test_openrouter_sdk_client.py` — every case runs `chat_tools()` down
through the real SDK and stops at an httpx `MockTransport`. Faking the SDK
would defeat the purpose: these tests exist to catch a typed model that stops
carrying one of our fields.

## License

Apache-2.0 — see `LICENSE`.
