# coding_cli

Hands programming tasks to Claude Code: each task is one headless Claude Code process on the operator's
subscription (the Claude Code login of the account ScarabHive runs under), in a fresh git worktree of a listed
repository. What the run changed is committed on a branch of its own; nothing is merged or pushed. Runs are locked
down (no MCP, a fixed tool list, no shell unless allowed), only listed users start them, and none starts while the
subscription is near its limit. A long run goes on in the background and wakes the session that started it.

- **Tools** -- `coding_cli_run_task`, `coding_cli_get_run`, `coding_cli_cancel_run`; offered only when the Claude
  Code executable and a usable workdir are there.
- **Agent** -- `claude_code_agent`, which only relays work to Claude Code; the coder agent has the tools too.
- **Hooks / panel** -- none.

Configured in the plugin's own `agents/coding_cli.yaml` (workdirs, `allowed_users`, `allowed_commands`, limits); an
agent gets the tools with `+coding_cli/*`. Concept and measurements: `docs/coding_cli_plugin_konzept.md`.

The full manual -- how a run goes, what a run may do, the tools and their answers, the settings and the known gaps --
is the plugin's guide, `coding_cli.guide`, in the Help panel.
