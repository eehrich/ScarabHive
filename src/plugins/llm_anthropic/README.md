# Anthropic LLM Provider

Claude through Anthropic's own Messages API, via the official `anthropic` SDK. An LLM provider plugin: no tools, no
hooks, no panel.

- **`anthropic`** -- chat for agents: streaming, extended/adaptive thinking with verbatim thinking-block replay,
  prompt caching with `cache_control` markers, structured output.
- **`anthropic` (batch)** -- the Message Batches API, for entries with `batch_provider: anthropic`.

Nothing to enable: write `provider: anthropic` in a model entry of `config/llm.yaml` and point a profile at it. The
key comes from the entry's `api_key` or `ANTHROPIC_API_KEY`. Claude behind OpenRouter is not this plugin
(`openai_httpx`).

The full manual -- every model entry key, timeouts and streaming, retries, thinking, prompt caching (and why
`prompt_cache_mode: off` still writes), what the hooks and cost figures see, errors, batch -- is the plugin's guide,
`llm_anthropic.guide`, in the Help panel.

## License

Apache-2.0 -- see `LICENSE`.
