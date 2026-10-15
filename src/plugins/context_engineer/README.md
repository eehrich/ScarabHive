# Context Engineer

Keeps a long agent conversation within the model's context without losing what it takes out. Before an LLM call it
stores large tool results, attached files and media on disk and leaves a short placeholder; when that is not enough it
archives old messages, or removes them after archiving. The agent looks through what was moved out and reads it back
piece by piece, and keeps important facts in a small core memory. The **Context Engineer** panel shows every
compaction, what a session's stores hold and its facts.

- **Tools** `context_engineer_list` (browse or search what was moved out), `context_engineer_read` (one item by ref,
  a piece at a time, or media loaded back), `context_engineer_store_fact` (core memory) and `context_engineer_compact`
  (compact now; `/compact` at the chat prompt).
- **Hook** `engineer_context` (off by default; on for `multi_turn_agent` and the agents built on it) -- before each LLM
  call: oversized new tool results stored on arrival, a message limit, media passes, and three layers from reversible
  (tool results stored) to archiving and removing old messages, spaced out by a hysteresis and tried on a copy first,
  so the prompt cache is broken rarely and deeply.
- **Panel** Context Engineer -- the compactions of a session or of all, its stored results, archived messages and facts.

It is enabled in the shipped `config/plugins.yaml` (`context_engineer: {type: context_engineer, enabled: true}`, with
its settings under `config:`). For another agent, switch the hook on in its `hooks.overrides`
(`context_engineer.engineer_context: {enabled: true}`, plus any setting for that agent) and allow `+context_engineer/*`
in its tool list.

The full manual -- the panel, every tool's parameters and answers, what each layer does, the hook's settings and the
server settings -- is the plugin's guide, `context_engineer.guide`, in the Help panel.
