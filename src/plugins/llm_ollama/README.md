# Ollama Provider

Models served by Ollama, local or on another machine. An LLM provider plugin: no tools, no hooks, no panel.

- **`ollama`, `ollama_mode: native`** -- Ollama's own `/api/chat` over httpx: sends `context_window` as `num_ctx`,
  `thinking_level` as `think`, `max_tokens` as `num_predict`; streams text, thinking and tool calls, with its own
  retries.
- **`ollama`, `ollama_mode: openai_compat`** (the default) -- Ollama's `/v1` endpoint through the `openai` provider
  of the llm_openai plugin (delegated through the registry). No context size, no thinking switch.

Nothing to enable: write `provider: ollama` in a model entry of `config/llm.yaml` and point a profile at it. No pip
dependency of its own; openai_compat mode needs llm_openai.

The full manual -- the two modes, model entry keys, context size and GPU memory, thinking, timeouts and retries,
what the hooks and cost figures see, errors -- is the plugin's guide, `llm_ollama.guide`, in the Help panel.

## License

Apache-2.0 -- see `LICENSE`.
