# Cognitive Stack

Working memory as a stack: an agent pushes what it is about to interrupt, works on the interruption and pops back,
instead of keeping the nesting in prose. Each frame holds a description and optional structured data. The stack is
kept in memory per conversation and is gone after a restart; it has no panel.

- **Tool** `cognitive_stack` -- one tool, five operations: `push_batch`, `pop_batch`, `peek`, `list`, `clear`.
- **Hook** `inject_stack_context` (off by default) -- appends the top frames of the conversation's stack to the
  history before an LLM call, only when they changed, so the cached prompt stays intact.

Enable it in `config/plugins.yaml` (`cognitive_stack: {type: cognitive_stack, enabled: true}`) and allow
`+cognitive_stack/*` in an agent's tool list.

The full manual -- every operation, parameter, answer and error, the hook and its setting, the server settings and
how long the state lives -- is the plugin's guide, `cognitive_stack.guide`, in the Help panel.

## License

Apache-2.0 -- see `LICENSE`.
