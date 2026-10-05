# Task Switch

Phase control for agents: an agent says which task it is on (plan, do, check ...), the plugin stores the name in a
session variable, and the agent's prompt template shows the instructions for that task from the next LLM call on.
A switch can be guarded by a gate check that asks another tool first. The variables are kept per session and come
back when the session is reopened; there is no panel and no hook.

- **Tools** `<instance>_set_task` (switch the task, optionally restricted to `allowed_tasks` and guarded by
  `task_preconditions`) and `<instance>_set_context` (set any other session variables, as a JSON object string).

Enable it with a server entry of `type: task_switch` (in `config/plugins.yaml` or the agent's own YAML), allow
`+<instance>/*` in the agent's tool list and use the variable (`current_task` by default) in its prompt template.

The full manual -- every answer and error, the gate checks, where the variables live, sub-agents, and what a switch
does to the prompt cache -- is the plugin's guide, `task_switch.guide`, in the Help panel.
