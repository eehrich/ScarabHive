# coding_cli

Hands programming tasks to Claude Code: each task is one headless Claude Code process on the operator's
subscription (the Claude Code login of the account ScarabHive runs under), in a fresh git worktree of a listed
repository. What the run changed is committed on a branch of its own; nothing is merged or pushed. Runs are locked
down (no MCP server but those the operator names per workdir, a fixed tool list, no shell unless allowed, files with
secrets kept out of the worktree), only listed users start them, and none starts while a subscription window is at
or past the configured share (`max_window_utilization`). A long run goes on in the background and wakes the session that started it.

With `cli: antigravity` an instance runs Google's Antigravity CLI (`agy`) instead, on its Google login: same tools
and answers, no shell, web search in every run (its workdirs say `web: true`), no MCP servers, no subscription guard.

- **Tools** -- `coding_cli_run_task` (start a run, optionally with a `json_schema` for a structured answer, or
  `resume` an ended one), `coding_cli_get_run`, `coding_cli_cancel_run` and `coding_cli_quota` (the subscription's
  windows, without starting a run). They are offered only when the Claude Code executable and a usable workdir are
  there.
- **Agent** -- `claude_code_agent`, which only relays work to Claude Code; the coder agent has the tools too.
- **Hooks / panel** -- none.

The entry ships in the plugin's own `agents/coding_cli.yaml` (workdirs with `exclude`, `mcp_servers` and `web`,
`allowed_users`, `allowed_commands`, limits such as `max_parallel` and `max_window_utilization`); an agent gets the
tools with `+coding_cli/*` in its tool list.

The full manual -- how a run goes, what a run may do, the tools and their answers, the settings and the known gaps --
is the plugin's guide, `coding_cli.guide`, in the Help panel.
