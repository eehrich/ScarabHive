# State Graph

Agent workflows as state machines in YAML: states run an agent, a tool, a decision, a Python function, another
machine or a fan-out; transitions carry Python guards and effects. The engine journals every step, resumes an
interrupted run without repeating finished work, and pauses on breakpoints and watches. The **State Graph** panel
shows a machine as a graph to edit, run and debug it; the agent `stategraph_author` writes machines for you.

- **Tools** `stategraph_catalog`, `stategraph_list_machines`, `stategraph_get_machine`, `stategraph_validate_machine`,
  `stategraph_save_machine` -- build machines; `stategraph_run_machine`, `stategraph_get_run`, `stategraph_list_runs`,
  `stategraph_control_run`, `stategraph_send_event` -- run and debug them. Slash commands `/stategraph-run`,
  `/stategraph-runs`, `/stategraph-stop`.
- **Panel** State Graph (admins) -- machine list, graph editor with inspector, YAML, runs with result, history and
  debugger.
- **Agents** `stategraph_author` (writes, validates, saves and test-runs machines), `stategraph_runner` (the default
  runner: hosts runs; its tool allowlist is what a machine may call).
- No hook.

Nothing to enable: `agents/stategraph.yaml` and `agents/stategraph_author.yaml` ship the instances and are included
by `config/config.yaml`. A tool a machine should call goes into its runner's allowlist: a plugin that ships machines
ships a runner of its own that names their folder in `runs_machines_in` (guide, "Runners"); the rest run with
`stategraph_runner`. An agent that should use the tools allows `+stategraph/*`.

The full manual -- the panel, the machine format in short, runs, every tool with its parameters and answers,
schedules, callback URLs, the server settings and security -- is the plugin's guide, `stategraph.guide`, in the Help
panel. The complete format: `skills/stategraph-authoring/references/format.md`; new activity kinds:
`docs/extending.md`; design: `docs/stategraph_design.md` (repository root).
