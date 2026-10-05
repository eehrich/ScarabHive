# Decision model provider

The client for decision models -- TypeSafe's Jev (through OpenRouter or TypeSafe's own endpoint), Laya on a local
`laya-serve`, and Ollama's decision models (Nimble, Tev1). A decision model answers named questions about a piece of content with a probability, a named option or
a position on a scale; it writes no text. Nothing here can serve `chat()`: the plugin declares only
`provides_decisions`, and its models live under `llm_system.decision_models`, reached by decision profile. The
`decision` plugin gives agents tools on top of it.

- **Providers** `openrouter_decisions`, `systemone_decisions` and `ollama_decisions` -- one client for the one wire; they differ in the
  default endpoint, whether OpenRouter's `session_id` is sent, the name a call is booked under, and whether it bills
  (an Ollama call costs 0).
- **No tools, hooks or panel.** Every call reports itself to `pre_llm_request` / `post_llm_response`, so the usage
  tracker and the message debugger see it.

Nothing to enable: name a provider in a `decision_models` entry of `config/llm.yaml`. Dependency: `httpx`.

The full manual -- configuration, keys, question types, retries, hooks and cost, every error -- is the plugin's
guide, `llm_decisions.guide`, in the Help panel.
