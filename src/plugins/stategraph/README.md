# State Graph

Agent workflows as state machines in YAML: states run an agent, a tool, a decision, a Python function, another
machine or a fan-out; transitions carry Python guards and effects. The engine journals every step, resumes an
interrupted run without repeating finished work, and pauses on breakpoints and watches like a debugger. The
**State Graph** panel shows a machine as a graph to edit, run and debug it; the agent `stategraph_author` writes
machines for you.

- **Tools** `stategraph_catalog`, `stategraph_list_machines`, `stategraph_get_machine`, `stategraph_validate_machine`,
  `stategraph_save_machine` -- build machines; `stategraph_run_machine`, `stategraph_get_run`, `stategraph_list_runs`,
  `stategraph_control_run`, `stategraph_send_event` -- run and debug them. Slash commands `/stategraph-run`,
  `/stategraph-runs`, `/stategraph-stop`. `stategraph_run_workflow` -- start a dynamic workflow: a machine an agent
  wrote for its task, unsaved, bounded by what that agent may reach, under the instance's caps on output tokens (skill
  `stategraph-workflows`; off until the instance has a `workflows:` block).
- **Hooks** `count_call_spend` (post_llm_response, on by default) -- counts the LLM calls made under a `call`
  activity into its cost; it changes nothing in any call. `deliver_workflow_results` (pre_llm_call, off by default) --
  appends the result of a dynamic workflow that ended to its caller's history, once; `confirm_workflow_results`
  (post_llm_call, off by default) -- marks it delivered once a call was made with it. An agent that writes workflows
  switches both on in its `hooks.overrides`.
- **Panel** State Graph (admins) -- machine list, graph editor with inspector, YAML, runs with result, history and
  debugger.
- **Agents** `stategraph_author` (writes, validates, saves and test-runs machines) and `stategraph_runner` (the
  default runner: hosts runs; its tool allowlist is what a machine may call).
- **Also** schedules (machines the clock starts), one-time callback URLs that send an event to a run, and
  `service.run_to_end(...)` to run a machine from Python code.

Nothing to enable: `agents/stategraph.yaml` and `agents/stategraph_author.yaml` ship the instances and are included
by `config/config.yaml`. An agent that should use the tools allows `+stategraph/*`. A plugin that ships machines
ships a runner of its own for their folder (`runs_machines_in`).

The full manual -- the panel, machines and runs, every tool with its parameters and answers, runners, schedules,
callback URLs, the hook, the server settings and security -- is the plugin's guide, `stategraph.guide`, in the Help
panel. The complete format: `skills/stategraph-authoring/references/format.md` (cheat sheet: `docs/format.md`); new
activity kinds: `docs/extending.md`.
