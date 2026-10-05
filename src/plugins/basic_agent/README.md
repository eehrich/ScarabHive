# Basic Agent

The plain agent: it takes a message, runs the LLM loop with the tools it is
allowed to use, and answers. What an agent of this type can do comes entirely
from its configuration (LLM profiles, system prompt, tool list, hook settings);
`chat_agent` and many other agents are of this type. It is also the reference
for writing an agent with code on `SchemaBasedAgent`.

- **Tools** (for other agents that may call it): `<instance>_execute_task`
  runs one task through the agent and returns its answer and tool calls;
  `<instance>_list_available_tools` lists the tools the agent may call.
- **Hooks**: none of its own; the agent's `agent_config.hooks` decides which
  hook plugins apply.
- **Panel**: none; the agent is used in the chat.

Enable it by writing an agent entry with `type: basic_agent` (see
`config/agents/agents.yaml`). Another agent calls it as a tool when this one
has `metadata.visibility: tool` or `both` and the caller allows
`+<instance>/*`.

The full manual is `basic_agent.guide` in the Help panel.

License: Apache-2.0, see `LICENSE`.
