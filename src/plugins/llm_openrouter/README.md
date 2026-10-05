# OpenRouter SDK provider

OpenRouter's official Python SDK as a second transport for the Responses API. The client is
`openai_responses` from `llm_openai_compat` with only the sending replaced, so the two can be compared on
the transport alone: payload, caching, routing, retries, parsing and hook reports are shared.

- **Provider** `openrouter_sdk` -- `POST <base_url>/responses` through the SDK, never streamed. The answer
  is read from the raw body, so usage and OpenRouter's cost fields arrive as on the httpx route.
- `safety_settings`, and routing or plugins the SDK cannot type, are refused when the client is built; a
  field the SDK would drop, change or refuse is refused before the request leaves.
- No tools, hooks or panel.

Use it by naming `provider: openrouter_sdk` in a model entry of `config/llm.yaml`. It needs the `openrouter`
package (declared in `plugin.toml`).

The full manual -- how it differs from `openai_responses`, the model entry keys, what the SDK carries and
refuses, errors and setup -- is the plugin's guide, `llm_openrouter.guide`, in the Help panel.
