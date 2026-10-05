# LLM Common

The code the LLM provider plugins share: which API key may go to which host, which HTTP statuses are
retried, how a cancel reaches a running call, and the OpenAI and Gemini wire shapes more than one route
speaks. A library (`type = ["library"]`, no `provides`): the provider plugins import it, the LLM registry
skips it.

- **API keys** -- an OpenAI-compatible entry without `api_key:` gets OPENROUTER_API_KEY, OPENAI_API_KEY or
  TYPESAFE_API_KEY by the host it talks to (local hosts get OPENAI_API_KEY); any other host must set
  `api_key:`, or building the client fails.
- No tools, hooks or panel.

Nothing to enable. The full manual -- the key rules, what a misconfiguration looks like, the modules and
who uses them -- is the plugin's guide, `llm_common.guide`, in the Help panel.

## License

Apache-2.0 -- see `LICENSE`.
