# Context Summarizer

Shrinks a long conversation: before an LLM call a second, cheaper model summarizes the older messages and the
summaries take their place; the prompts and the most recent messages stay as they were. It fires by itself above a
number of messages (200 in the shipped configuration -- in practice the trigger) or a share of the answering model's
context window. The **Context Summarizer** panel lists every run with what it saved and what it wrote.

- **Tools** `context_summarizer_summarize` (summarize now; `/summarize`) and `context_summarizer_check_stats`
  (message count, tokens, utilization, a recommendation; `/stats`).
- **Hook** `summarize_context` (on for every agent) -- rewrites the history behind the prompts when it fires, so the
  cached prompt prefix is lost once per run; off or a different `max_messages` per agent via
  `hooks.overrides.context_summarizer.summarize_context`.
- **Panel** Context Summarizer -- the runs of a session or of all, their figures, the summaries and the messages they
  replaced.

Enable it in `config/plugins.yaml` (`context_summarizer: {type: context_summarizer, enabled: true}`); the hook then
runs for every agent, the tools for an agent that allows `+context_summarizer/*`.

The full manual -- the panel, both tools with their answers, when the hook fires and what the model loses, the
server settings -- is the plugin's guide, `context_summarizer.guide`, in the Help panel.
