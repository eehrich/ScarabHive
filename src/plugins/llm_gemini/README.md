# Google Gemini Providers

Two ways to reach Gemini directly, the batch backend, and the TTS client.

## What it provides

| Name | Kind | Module | Transport |
|---|---|---|---|
| `gemini` | LLM | `gemini_client.py` | httpx against Gemini's native REST API |
| `gemini_sdk` | LLM | `gemini_sdk_client.py` | official `google-genai` SDK |
| `gemini` | batch | `gemini_batch.py` | Batch API |
| `gemini_tts` | TTS | `gemini_tts_client.py` | speech synthesis |

```yaml
my-model:
  provider: gemini_sdk
  model: gemini-3-pro
  api_key: ${GOOGLE_API_KEY}
```

`dependencies = ["google-genai>=1.50.0"]`, imported lazily in the factory.
The `gemini` client speaks the native REST API rather than Google's
OpenAI-compatibility layer on purpose: that layer mishandles `tool_calls`
indices and the `thought_signature` requirement.

## Direct vs. via OpenRouter

Gemini is also reachable through `llm_openai_compat` (`provider:
openai_responses` with an `openrouter.ai` base URL). One difference decides
which to use: **`safety_settings` only work on the direct route.** OpenRouter
drops the field on `/responses` (measured 2026-09-01), and its own default
sets every harm category to `OFF`. So the `gemini-3-*` entries — the ones
that carry thresholds — use this plugin; the `openrouter-gemini*` entries
carry none.

Consecutive same-role messages and system messages need reshaping for the
Gemini content format; both clients do that and both have tests for it
(`test_gemini_consecutive_messages.py`,
`test_gemini_client_system_messages.py`), because the two clients drifted
apart on exactly this before.

## TTS

`gemini_tts` is dispatched through the same registry seam as the LLM
providers (`provides_tts` in `plugin.toml`). Since 2026-08-26 the core's
`agent_system/llm/tts.py` is service definition only — no client code.

## Tests

`tests/` next to the code, including batch cancel and the TTS client.

## License

Apache-2.0 — see `LICENSE`.
