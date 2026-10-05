# Context & Cost Usage

Records what every LLM call costs -- tokens, cache reads and writes, latency, price (billed, or estimated from
`config/llm_pricing.yaml`) and how full the context window was -- in an SQLite database shared by every process. The
**Context & Cost Usage** panel adds it up per session (sub-agents included), per agent and per model; the chat's
`/costs` and `/context` and the context-compacting plugins read the same records.

- **Hooks** `track_usage` (every call an agent makes) and `track_non_agent_usage` (calls without an agent: decisions,
  speech synthesis that reports tokens) -- both on by default, no settings; they change nothing the model sees.
- **Panel** Context & Cost Usage -- figures, a chart of the newest calls, tables per agent, per model and per call.
- No tools.

Enable it in `config/plugins.yaml` (`context_usage_tracker: {type: context_usage_tracker, enabled: true}`); keep that
instance name, the other readers look for it.

The full manual -- the panel, what each hook records, who reads the figures, the web API and the server settings --
is the plugin's guide, `context_usage_tracker.guide`, in the Help panel.
