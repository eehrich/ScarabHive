# OpenAI SDK Provider

OpenAI through the official `openai` SDK -- Chat Completions for ordinary models, the Realtime API for the realtime
models. An LLM provider plugin: no tools, no hooks, no panel.

- **`openai`** -- Chat Completions, streamed or not, with the client's own retries (the SDK's are off).
- **`openai` (realtime)** -- a model whose `capabilities.default_api_type` is `realtime` is served over one Realtime
  WebSocket per call, text output only.
- **`openai` (batch)** -- `batch_provider: openai`, delegated to llm_openai_compat's Batch API backend.

Nothing to enable: write `provider: openai` in a model entry of `config/llm.yaml` and point a profile at it. The API
key follows the endpoint (`OPENAI_API_KEY` for api.openai.com) unless the entry sets `api_key`.

The full manual -- model entry keys, timeouts and streaming, retries, the Realtime API, what the hooks and cost
figures see, errors -- is the plugin's guide, `llm_openai.guide`, in the Help panel.

## License

Apache-2.0 -- see `LICENSE`.
