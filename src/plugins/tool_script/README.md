# Tool Script

Scripted tool chains: instead of one tool call per model turn, the agent writes one small Python script that calls
its other tools on the server. The results in between stay in the script; only the script's `result` reaches the
model -- fewer turns, fewer tokens, no value typed twice. A script can call only what the agent itself may call,
and runs in a sandbox with no imports, no files and no network.

- **Tool** `<instance>_run_script` -- runs a script (`script`, optional `timeout`, `dry_run`); inside it
  `call_tool`, `log`, `parse_json` and `ToolCallError`. A failure reports every executed call, the failing
  statement's line and the script's variables, so the agent can continue where it stopped.

Enable it as a server of type `tool_script` (the reference setup is `config/agents/workflow_agent.yaml`, server
`wf_pipe`) and allow `wf_pipe/*` in the agent's tool list next to the tools its scripts may call.

The full manual -- what a script may call, the script language, the limits, the answers and the server settings --
is the plugin's guide, `tool_script.guide`, in the Help panel.
