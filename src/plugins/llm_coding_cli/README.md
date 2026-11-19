# Coding CLI as an LLM

A coding CLI -- Claude Code or Antigravity -- as one link of an agent's profile chain. The agent runs as always;
its model call goes to a [coding_cli](../coding_cli/README.md) instance instead of an API, typically on a
subscription. The CLI gets the conversation as one task, runs its own tool loop -- with the agent's own tools,
handed to it through the [MCP gateway](../mcp_gateway/README.md) -- and its final answer comes back as the
assistant message.

- **Ready-made profiles** (`agents/llm_coding_cli.yaml`): `claude-code-opus`, `claude-code-sonnet`,
  `claude-code-haiku` (Claude Code, with the agent's tools) and `agy-gemini-flash`, `-hydrogen`, `-argon`, `-carbon`, `-neon`
  (Antigravity, with the agent's tools beside its web search). Pick one in the chat's profile selector, with
  `/model` or `--llm`, or put it first in an agent's profile chain.

- **Providers** `claude_code` and `antigravity`. A model entry names the coding_cli instance as `model`, alone or
  as `<instance>/<model>` for a model the instance lists; its `thinking_level` is the run's effort. The instance
  decides the CLI, the subscription threshold and the tools: with a `caller` gateway the tools of the agent's own
  call, run by that agent. The provider has to match the instance's `cli`.
- **One CLI conversation per session**: a later model call of the same session -- in the same agent run, or the next
  turn of a continued sub-agent -- resumes that CLI run with the messages added since.
- **Fallback**: the subscription's limit blocks the link until it resets, every other failure (instance busy or
  missing, a failed run, no answer) moves the chain on at once -- with a log line naming why. An agy run that
  failed on its login before its first turn starts once more first.

The full manual -- the profiles, the model entry, what the CLI sees, sessions, cancelling, the fallback cases and
the gaps -- is the plugin's guide, `llm_coding_cli.guide`, in the Help panel.

## Model Experience

- **What the model sees**: the CLI reads the conversation as one text (`# Instructions`, then `## user`,
  `## assistant`, `## tool` blocks) and, with a `caller` gateway, the agent's tools as MCP tools
  `mcp__gateway__<tool>` (agy: `mcp_gateway_<tool>`). The agent's own model sees nothing of this plugin.
- **Token and cache effect**: none on the agent's requests. The runs of an instance without files share one room,
  so a conversation's common start is read from the subscription's cache.
- **Known gaps**: the CLI's tool calls are not in the agent's history, step budget or loop detection; agy takes
  about 55,000 tokens of a conversation, and its web search runs beside the agent's tools (`with_web`); images are
  named, not passed; no streaming.
