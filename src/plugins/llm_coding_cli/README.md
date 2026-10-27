# Coding CLI as an LLM

A coding CLI -- Claude Code or Antigravity -- as one link of an agent's profile chain. The agent runs as always;
its model call goes to a [coding_cli](../coding_cli/README.md) instance instead of an API, typically on a
subscription. The CLI gets the conversation as one task, runs its own tool loop through the tools of the instance's
workdir (the [MCP gateway](../mcp_gateway/README.md)), and its final answer comes back as the assistant message.

- **Providers** `claude_code` and `antigravity`. A model entry names the coding_cli instance as `model`; the
  instance decides the CLI, model, effort, subscription threshold and the tools. The provider has to match the
  instance's `cli`.
- **One run per agent run**: a second model call in the same agent run resumes that CLI run with the messages added
  since.
- **Fallback**: the subscription's limit blocks the link until it resets, every other failure (instance busy or
  missing, a failed run, no answer) moves the chain on at once -- with a log line naming why.

The full manual -- the model entry, what the CLI sees, sessions, cancelling, the fallback cases and the gaps -- is
the plugin's guide, `llm_coding_cli.guide`, in the Help panel.
