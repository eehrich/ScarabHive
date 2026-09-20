# OpenAI-Compatible LLM Providers

httpx clients for every endpoint that speaks the OpenAI wire format —
OpenRouter, DeepSeek, api.openai.com, local gateways. No SDK dependency:
httpx is a core requirement, so this plugin installs with the framework.

## What it provides

| Name | Kind | Client | Endpoint |
|---|---|---|---|
| `openai_httpx` | LLM | `HTTPXOpenAIClient` | `/chat/completions` |
| `openai_responses` | LLM | `OpenAIResponsesClient` | `/responses` |
| `openai_httpx` | batch | `OpenAIBatchClient` | `/v1/batches` (OpenAI only) |
| `openai_speech` | TTS | `openai_speech_client` | `/audio/speech` (+ voice cloning via `input_references`) |

```yaml
# config/llm*.yaml
my-model:
  provider: openai_responses          # or openai_httpx
  api_key: ${OPENROUTER_API_KEY}
  base_url: https://openrouter.ai/api/v1
```

## Voice cloning on the speech client

Put the sample on the voice — `TTSVoice(name="clone:egon",
reference_audio=wav_bytes, reference_text=transcript)` — and the client
sends it as OpenRouter's stateless `input_references` (base64 data URI in
every request, ≤ 15 MiB, no upload step) and omits `voice`. OpenRouter
routes only to endpoints flagged `supports_voice_cloning` in the endpoints
API and answers 404 otherwise; as of 2026-09 that is `fish-audio/s2.1-pro`
alone (Voxtral and MAI-Voice-2 advertise cloning but are not flagged).

## Why there are two LLM clients

`openai_httpx` speaks Chat Completions. `openai_responses` speaks the
Responses API, where a reasoning model's output items (`reasoning` with
`encrypted_content`, `function_call`, `message`) round-trip **verbatim**
instead of being flattened into one assistant message and reconstructed by
the gateway on the next turn. That translation intermittently produced
undecryptable blobs (`HTTP 400 "encrypted content ... could not be
verified"`), which is why all GPT-5.x and Gemini entries moved to
`openai_responses`. `httpx_client._recover_encrypted_reasoning` is the
reactive healing that remains for the Chat route.

Both clients are non-trivial mostly because of what they repair, not because
of HTTP: Anthropic cache-control capping, Gemini tool-schema sanitising,
multimodal injection, flex-tier 429 fallback, body-level errors inside an
HTTP 200, and reasoning-artifact recovery.

## The key follows the endpoint

`llm_common.api_keys.resolve_api_key` decides which secret may be used from
the **effective** base URL, not from the provider name. An env fallback that
ignored `base_url` would hand the OpenAI key to whatever host a model entry
happens to name. Each factory declares the endpoint it falls back to in
`plugin.toml` (`default_base_url`); the agreement between manifest and
factory is pinned by `tests/llm/test_llm_openrouter_routing_default.py`.

## OpenRouter specifics

* **Routing transparency** — requests carry `X-OpenRouter-Metadata: enabled`,
  and every `post_llm_response` hook gets a `routing` field naming the
  backend that actually answered. Without the header no response says.
* **Sticky routing** — the resolved `prompt_cache_key` is also sent as
  `session_id`. OpenRouter's prompt cache is backend-local, so calls sharing
  a prefix only hit it while they share a backend.
* **Pass-through, unset by default** — `plugins`, `prompt_cache_options`,
  `safety_identifier`. Each changes what the model sees or costs; see
  `docs/llm_catalog.md` for the measurements behind leaving them off.
* `safety_settings` reach the provider on `/chat/completions` and are
  **dropped on `/responses`** (measured 2026-09-01).

## Tests

`tests/` next to the code. Notable: `test_openai_responses_retry.py` drives
the real retry/healing loop against a fake transport,
`test_openrouter_gateway_extras.py` pins the gateway fields above, and
`test_httpx_sig_bypass_status400.py` covers the signature-bypass recovery.

## License

Apache-2.0 — see `LICENSE`.
