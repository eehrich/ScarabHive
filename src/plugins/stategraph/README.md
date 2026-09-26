# stategraph

Agent workflows as UML-style state machines in YAML. States run activities -- an agent
call, a tool call, a decision model, a Python function, a submachine, a fan-out;
transitions carry Python guards and effects. A deterministic engine runs a machine,
journals every step, resumes after a crash without repeating finished work, and pauses
on breakpoints and watchpoints. The **State Graph** panel shows a machine as a graph to
edit, run and debug it; the agent `stategraph_author` writes machines.

- Design and contract: `docs/stategraph_design.md` (repo root)
- Format reference for authors: `skills/stategraph-authoring/references/format.md`
  (cheat sheet: `docs/format.md`); patterns and debugging next to it
- New activity kinds: `docs/extending.md`
- Examples: `machines/`

## Setup

Nothing to register. `agents/stategraph.yaml` and `agents/stategraph_author.yaml` are
included by `config/config.yaml` (`../src/plugins*/*/agents/*.yaml`) and ship these
instances, all enabled:

| Instance | Type | Role |
|---|---|---|
| `stategraph` | `stategraph` | the tools and the panel |
| `stategraph_runner` | `basic_agent` | hosts runs; its tool allowlist is what a machine may call |
| `stategraph_json` | `json_store` | JSON documents for machines' tool activities |
| `stategraph_author` | `multi_turn_agent` | writes, validates, saves and test-runs machines |

Configuration of `stategraph` (defaults in code):

| Key | Shipped value | Meaning |
|---|---|---|
| `machine_dirs` | `data/stategraph/machines`, `src/plugins*/*/machines` | machine roots in search order; the first root with an id wins |
| `writable_machine_dirs` | `data/stategraph/machines` | where saves go (not versioned: `/data` is gitignored) |
| `runs_db` | `data/stategraph/runs.db` | runs and their journal (SQLite) |
| `runner_agent` | `stategraph_runner` | host of runs, boundary of tool activities |
| `allowed_users` | `[]` | users besides admins who may validate, save, run and control machines |
| `inject_params` | `{}` | `{tool pattern: {param: value}}` added to tool activities after rendering (secrets) |
| `default_max_wait` | `600` | seconds `run_machine` waits with `wait: finish` |

**Letting machines use more.** An agent activity runs any configured, enabled agent
directly -- the registered instance, on a session of its own -- so a new agent needs no
entry anywhere: the machine file names it, and machines are admin work. A tool a machine
should call goes into `stategraph_runner`'s `tools.allowed` -- never a `stategraph/*` tool.
A new agent and a changed tool allowlist need a restart.

**Delegating to the author.** Another agent reaches `stategraph_author` through its SAM:
add `stategraph_author` to that SAM's `allowed_agents`.

**A machine as an agent.** The plugin type `stategraph_machine`
([src/plugins/stategraph_machine](../stategraph_machine/README.md)) makes one machine
addressable like any agent -- SAM spawns, AgentCaller, writer_jobs' `/events`. It runs the
machine through this instance (the panel sees and controls those runs) and answers with the
output as JSON. Example: `v6_story_machine`, the writer v6 story design as a machine
(`src/plugins_writer/writer_pipeline_v6/machines/`). A writer machine's tools are in
`stategraph_runner`'s allowlist; the issues write key comes from `inject_params`.

## Tools

| Tool | Parameters | Result | Admin¹ |
|---|---|---|---|
| `stategraph_catalog` | `agents?` (fnmatch pattern) | activity kinds with fields, the agents a machine may run, callable tools, decision profiles, example ids | |
| `stategraph_list_machines` | | id, title, file, writable, validates | |
| `stategraph_get_machine` | `machine_id` | `files {path: text}`, `versions {path: version}`, problems | |
| `stategraph_validate_machine` | `files` or `yaml`, `machine_id?` | problems | yes |
| `stategraph_save_machine` | `files`, `machine_id?`, `expected_versions?` | versions; refused on errors or a version conflict | yes |
| `stategraph_run_machine` | `machine_id`, `params`, `mocks`, `mock_only`, `breakpoints`, `watchpoints`, `run_key`, `wait`, `max_wait` | run id, status, state, output, error, accepted events | yes |
| `stategraph_get_run` | `run_id`, `steps?` | status, frames, context, output or error, last journal rows | |
| `stategraph_control_run` | `run_id`, `action`, action args | pause, continue, step, run_to, terminate, resume, fork, set_breakpoints, set_watchpoints, evaluate, set | yes |
| `stategraph_send_event` | `run_id`, `name`, `data?`, `frame?` | accepted, or why not | yes |

¹ The handler checks `_user_id`: an active admin, or a user in `allowed_users`.

Slash command: `/stategraph-run <machine id>` starts a run in the background.
REST for the panel: `/plugins/stategraph/api/…` (design §8.2), admin-only.

## Security

Machines contain Python that runs in the API process with the rights of plugin code,
and they run agents and tools. Therefore:

- **Routes** `/plugins/stategraph/*` require the admin role (`config/config.yaml`,
  `auth.plugin_security`).
- **Tools** that validate, save, run, control or send events require an admin or a user
  in `allowed_users`. Validating and saving never execute a machine's companion module
  (its names come from a scan); only a run does.
- **Recursion.** The runner's allowlist never contains stategraph's own tools, and the
  validator refuses them in a machine (SG007): a machine cannot save, start or control
  machines.
- **Agents** a machine runs are the ones its file names (literal, or a parameter with an
  enum, so validation sees every one); an agent activity runs no agent the machine does not
  name. None that reaches machines: not `stategraph_runner`, not a machine facade, not an
  agent whose allowlist reaches a stategraph tool beyond the read-only ones (such as
  `stategraph_author`), and none that can start such an agent through a SAM -- SG007, and
  again at run time.
- **Secrets** come from `inject_params`, applied after rendering, so they never appear in
  machine files or the journal's rendered inputs; a tool result that echoes them is
  redacted before the machine, the journal or an error message sees it.
- **Files.** `python:` and `imports:` resolve only inside the machine roots, so a machine
  cannot pull another file of the host into `get_machine` or a run.
- **Computed names.** `agent:` and `tool:` are literals or `{{ params.x }}` with an enum
  (SG005); the tool check runs again at run time before every tool call.
- **Mock-only runs** (the author's test runs) have no backend at all: a missing mock
  fails with `unmocked`/`no_backend` instead of reaching an agent or a tool.

## Limits

- **Cross-process debugging.** Only runs of the API process can be paused from the
  panel; runs of other processes are visible through their journal (design §11).
- **Browser tests.** The panel is checked with JavaScriptCore, not in a browser; the
  first browser session is a manual check in both themes.
- **Cost.** Agent calls report no usage to the engine; only `decide` reports cost.
- **External state is not forked.** A fork replays the machine's own journal; stores,
  database rows and agent conversations keep what the source run did.
- **`call` activities run in mock-only runs.** They are in-process Python; mock the
  ones that reach outside.

## Model Experience

### What the model sees

**`stategraph_author`** gets, in its system prompt, `agents/prompts/stategraph_author.md`
(role, the loop, rules, handover format) followed by the body of the skill
`stategraph-authoring` (`skills.always`), verbatim: the method, one complete machine,
the rules, the kinds, mock syntax and the validation codes. It reads
`references/format.md`, `patterns.md` and `debugging.md` on demand with `skills_read`.

Its tools, with the descriptions from `schema.yaml`:

- `stategraph_catalog` -- "What a machine may use: activity kinds with their fields, the agents an agent activity can run, the tools the runner may call, decision profiles, example machine ids. Use only what this lists."
- `stategraph_list_machines` -- "Machines in the machine roots: id, title, file, whether it is writable, whether it validates."
- `stategraph_get_machine` -- "A machine as its file tree: files {relative path: text} (the YAML, its companion .py, imported machines in the same root), versions {path: version} to pass back when saving, and its validation problems."
- `stategraph_validate_machine` -- "Check a machine without saving: format, graph, Python (compiles, names exist, purity), activities, submachine parameters, and whether the configuration can run it (agents that exist, tools the runner may call). Pass the whole tree as files, root file first, or a single yaml."
- `stategraph_save_machine` -- "Validate, then write the machine tree into the writable machine root. Refused if validation finds errors, or if a file changed since the versions you read (pass expected_versions from get_machine; omit for new files)."
- `stategraph_run_machine` -- "Run a machine. mocks {state path: out} answer instead of the activity ({"$visits": [out1, out2]} per use of the path in the run, {"$error": {type, message}} to fail it); mock_only refuses every unmocked agent, tool or decision. With wait=finish the call returns when the run ends, pauses at a breakpoint or waits for an event (at most max_wait seconds). run_key attaches to an unfinished run with the same key instead of starting a second one."
- `stategraph_get_run` -- "A run's status, frames (active states, context), output or error, what it waits for, and its last journal rows."
- `stategraph_control_run` -- "Debugger and lifecycle: pause, continue, step, run_to (state), terminate, resume (an interrupted run), fork (from top-level step at_step), set_breakpoints, set_watchpoints, evaluate (expr, read-only), set (path, expr; only while paused)."
- `stategraph_send_event` -- "Send a declared event to a run. It goes to the frame whose active states accept it; name frame when several do. An event nobody accepts yet waits in the run's inbox."

Validation problems arrive as `{level, code, message, path, file, line}`. Messages
name the fix, e.g.:

- `SG007 agent 'coder' is not configured or not enabled`
- `SG007 tool 'stategraph_run_machine' belongs to stategraph itself: a machine may not save, run or control machines`
- `SG002 trigger 'approved' is not a declared event (declare it under events:, or use done / error)`
- `SG004 'out' is not bound here (out: completion transitions; error: error transitions; event: event transitions)`
- `SG004 code fields are plain Python: remove the {{ }}`

A failed test run's `error` names the state and the cause, e.g.
`unmocked: <path>: mock-only run and no mock for this agent activity`, or
`loop_limit: panel_fix entered 3 times (max_visits 2)`.

**Agents a machine runs** see only their task and the template vars the machine sets;
nothing about stategraph. Each instance is a sub-session of the run's session
(`sg_<run id>`), so it stays out of the session list. **`stategraph_runner`** is never talked to; its prompt only
tells a stray visitor where to go.

No hook: the plugin injects nothing into any conversation.

### Token and cache effect

- The skill body (about 9 KB, roughly 2.3k tokens) and the author prompt (about 4 KB)
  sit in the author's system prompt: static, no ticking values -- a stable cache prefix.
- References are read on demand and **appended** to the history (`format.md` about
  38 KB, `patterns.md` about 20 KB, `debugging.md` about 10 KB); nothing rewrites
  earlier messages.
- Tool results grow with the machine: `get_machine` returns the whole tree,
  `validate_machine` the problem list, `get_run` at most `steps` journal rows (default
  30, at most 200). `context_engineer` is on for the author and caps large results.
- Runs themselves cost no author tokens: the engine calls agents and tools directly.

### Known gaps

- The author proves logic with mocks, not agent behaviour: prompts, tool arguments
  and timeouts are only tested by a live run, which it starts only when asked.
- A mock-only test cannot drive a wait state's `timeout` path without waiting that
  long; the author tests the event paths and names the untested timeout.
- The full format reference is not in the system prompt (size); the author has to read
  it once per conversation.
