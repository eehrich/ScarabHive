# llm_router

Lets an agent ask another model: it sends messages to an LLM profile of `config/llm.yaml` -- one call, no tools,
no fallback -- and returns the text answer as a tool result. A cheap agent can get a second opinion from a strong
model, or hand bulk work to a cheap one, without a sub-agent.

- **Tools** `llm_router_chat` (profile + `messages` or `message`) and `llm_router_list_profiles` (every profile
  with its model entry).
- **Hooks / panel** -- none.

Off by default: set `enabled: true` on `llm_router` in `config/plugins.yaml` and allow `+llm_router/*` in the
agent's `tools.allowed`.

The full manual -- parameters, answers and errors, what the model sees, known gaps -- is the plugin's guide,
`llm_router.guide`, in the Help panel.
