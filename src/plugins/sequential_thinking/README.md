# Sequential Thinking

A scratchpad for step-by-step reasoning: an agent writes a hard problem down one thought per tool call, numbers the
steps, estimates how many it needs, revises an earlier thought and tries alternatives on branches. The chain is
kept in memory per conversation and is gone after a restart; it has no panel.

- **Tools** `sequential_thinking` (add a thought, revise, branch), `sequential_thinking_get_summary` (a session's
  whole chain) and `sequential_thinking_clear_history` (remove a session, or all of the conversation's).
- **Hook** `inject_active_sessions` (off by default) -- appends the latest thoughts of the conversation's session to
  the history before an LLM call, only when they changed, so the cached prompt stays intact.

Enable it in `config/plugins.yaml` (`sequential_thinking: {type: sequential_thinking, enabled: true}`) and allow
`+sequential_thinking/*` in an agent's tool list.

The full manual -- every parameter, answer and error, revisions and branches, the hook's settings, the server
settings and how long the state lives -- is the plugin's guide, `sequential_thinking.guide`, in the Help panel.
