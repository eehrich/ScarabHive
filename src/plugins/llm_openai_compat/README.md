# OpenAI-Compatible LLM Providers

httpx clients for every endpoint that speaks the OpenAI wire format -- OpenRouter, api.openai.com, DeepSeek, local
servers such as LM Studio, vLLM or llama.cpp. No vendor SDK. An LLM provider plugin: no tools, no hooks, no panel.

- **`openai_httpx`** -- Chat Completions. DeepSeek, local servers, Claude behind OpenRouter.
- **`openai_responses`** -- the Responses API; replays a reasoning model's output items verbatim. GPT-5.x and Gemini
  behind OpenRouter.
- **`openai_httpx` (batch)** -- the OpenAI Batch API, for `provider: batch` entries.
- **`openai_speech`** -- text to speech over `/audio/speech`, with voice cloning on OpenRouter.

Nothing to enable: name one of the providers in a model entry of `config/llm.yaml` and point a profile at it. The
API key follows the endpoint (`OPENROUTER_API_KEY`, `OPENAI_API_KEY`, ...) unless the entry sets `api_key`.

The full manual -- which route to take, every model entry key, timeouts and streaming, retries, reasoning, prompt
caching, OpenRouter routing, what the hooks and cost figures see, errors, batch and speech -- is the plugin's guide,
`llm_openai_compat.guide`, in the Help panel.

## License

Apache-2.0 -- see `LICENSE`.
