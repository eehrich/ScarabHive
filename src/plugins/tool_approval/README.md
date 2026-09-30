# tool_approval

Approvals before tool calls. A `pre_tool_call` hook decides every call of an
agent that switched it on, before it runs: deny rules block it, allow rules let
it through, and in `ask` mode every other call is put to the person watching the
run in the web chat, who answers *allow once*, *allow for this session* or
*deny*. Sub-agents inherit the rules of the run that started them; work that no
approval would reach (Claude Code, a state machine, a sub-agent with approvals
off) is asked with a warning or blocked.

- **Hook:** `check_tool_call` (`pre_tool_call`), off for every agent until it
  sets `hooks.overrides.tool_approval.check_tool_call.enabled: true`.
- **Approval UI:** the question appears as a row of the run in the web chat,
  with the call's arguments and the buttons; the answer goes to
  `POST /plugins/tool_approval/answer`. No tools, no panel.

Enable it in `config/plugins.yaml` (`type: tool_approval`; the default
configuration loads it) and switch the hook on per agent.

The full manual -- approving a call, rules and modes, sub-agents, what the
model sees, settings -- is `tool_approval.guide` in the Help panel.
