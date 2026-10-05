# Sub-Agent Manager

Lets an agent hand work to other agents: each sub-agent runs in a session of its own, keeps its history and can be
continued later, in the foreground or in the background, several side by side. Each server entry (a "SAM") names the
agents it may start and its limits. The **Sub-Agents** panel shows the sub-agents of a session -- nested, with what each
is doing and its transcript -- and lets you archive one.

- **Tool** `<instance>_manage_sub_agent` -- one tool with the operations `create`, `continue`, `poll`, `wait`,
  `wait_all`, `cancel`, `list`, `info` and `delete`; a background run can wake its caller when it ends.
- **Hook** `inject_sub_agent_context` (off by default) -- appends the session's sub-agents and their state to the
  history before an LLM call, only when that changed, so the cached prompt stays intact.
- **Panel** Sub-Agents -- a map and a list of the sub-agents of the session in the chat, with transcripts.

Enable an instance in `config/plugins.yaml` (`sub_agent_manager: {type: sub_agent_manager, enabled: true,
allowed_agents: [...]}`) and allow `+<instance>/*` in the calling agent's tool list.

The full manual -- the panel, every parameter and answer, limits and waking, the hook's settings, the server settings
and how to make an agent spawnable -- is the plugin's guide, `sub_agent_manager.guide`, in the Help panel.
