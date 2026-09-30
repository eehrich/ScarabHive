# Agent Continuation

Keeps an agent working when it answers with a status report instead of a result. After each text reply of an agent
that switched it on, the hook decides whether the reply is final; if not, the agent loop appends a user message
("Continue executing the remaining steps of your task ...") and the agent goes on. It can also send a fixed list of
follow-up questions after the final answer, and send an answer back when a required sub-agent was never started.

- **Hook** `evaluate_completion` (`post_llm_call`, off by default) -- decides by keyword and length rules, a chat
  model, a decision model's probability, or rules first and a model after. Every message it adds carries
  `injected_by` (`agent_continuation`, `agent_continuation.followup`, `agent_continuation.required_spawns`); it is
  appended at the end of the history, so the cached prompt stays intact. `max_continuations` (10 per request) and the
  agent's `max_steps` stop it.
- No tools, no panel.

Enable it in `config/plugins.yaml` (`agent_continuation: {type: agent_continuation, enabled: true}`, on by default),
then switch it on per agent with `hooks.overrides."agent_continuation.evaluate_completion": {enabled: true, ...}`.

The full manual -- when it fires, the exact messages, the rules and judges, follow-ups, required spawns, the limits
and every setting -- is the plugin's guide, `agent_continuation.guide`, in the Help panel.
