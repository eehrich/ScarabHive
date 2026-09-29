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
- Examples: `machines/`; `showcase.yaml` uses every element of the format once (a test keeps it so)

## Setup

Nothing to register. `agents/stategraph.yaml` and `agents/stategraph_author.yaml` are
included by `config/config.yaml` (`../src/plugins*/*/agents/*.yaml`) and ship these
instances, all enabled:

| Instance | Type | Role |
|---|---|---|
| `stategraph` | `stategraph` | the tools and the panel |
| `stategraph_runner` | `basic_agent` | hosts runs; its tool allowlist is what a machine may call |
| `stategraph_json` | `json_store` | JSON documents for machines' tool activities; kept without expiry (`file_retention_hours: 0`): an interrupted run's journal still names them when it resumes |
| `stategraph_author` | `multi_turn_agent` | writes, validates, saves and test-runs machines |
| `stategraph_example_agent` | `basic_agent` | the agent of the example machines: plain text, no tools, private. Not `chat_agent`: its markdown formatter hands a machine HTML |

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
| `public_url` | `""` | the app's URL as a system outside reaches it (`https://hive.example.com`): the base of the URLs of `callback` activities; empty: the URL is a path. The route `/plugins/<instance>/callback` is admin-only until the operator opens it (Security) |
| `schedules` | `[]` | machines started by the clock: `[{name, machine, every, offset?, late?, params?, user?}]` -- slots at 00:00 UTC plus multiples of `every` (at least 1m) plus `offset`; a slot runs once (run key `schedule:<name>:<slot>`; again only after a transient failure, at most 3 runs, 5 minutes after the last ended), one first seen later than `late` after its start (default `every`, at most 1h, at least 1m) is left out; one process starts an instance's slots, whoever holds its lease in `runs.db` (given up at a stop); a slot's run that a stopped process left is resumed. A wrong entry is logged and left out (`schedules.py`) |

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
output as JSON. An `agent:` block in the machine's file offers it without a config entry
(declared as each process starts). Example: `v6_story_machine`, the writer v6 story design as a machine
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
| `stategraph_run_machine` | `machine_id`, `params`, `mocks`, `mock_only`, `breakpoints`, `watchpoints`, `run_key`, `wait`, `max_wait` | run id, status, state, output, error, accepted events; with `run_key` also `attached`, `resumed` or `ended` | yes |
| `stategraph_get_run` | `run_id`, `steps?` | status, frames, context, output or error, last journal rows | |
| `stategraph_control_run` | `run_id`, `action`, action args | pause, continue, step, run_to, terminate, resume, fork, set_breakpoints, set_watchpoints, evaluate, set | yes |
| `stategraph_send_event` | `run_id`, `name`, `data?`, `frame?` | accepted, or why not | yes |

¹ The handler checks `_user_id`: an active admin, or a user in `allowed_users`.

**`run_key`** -- the same request again. Every call looks up the newest run of the key: a
live one is attached to, one another process holds answers `attached: false`, an interrupted
one is resumed, and an ended one answers `{run_id, ended: <status>}` -- the same output, failure
or cancel again. Only a failure that is transient (`interrupted`, `timeout`, `agent_failed`,
`decision_failed`, `internal`, `diverged` as the error or an unhandled cause;
`runner.TRANSIENT_ERRORS`) starts a new
run. A key of another user's run is refused.

**`control_run`** checks its arguments' types, `steps` included (a wrong one is an error,
nothing acts); with `steps` its answer carries that many journal rows;
`run_to` needs a `state`; `evaluate` answers JSON (a value that is not data comes as its
`repr`), as do watch values. The debugger's state -- pause, step, `run_to` -- survives a stop
and resume.

Slash commands: `/stategraph-run <machine id> [{json params} | key=value ...]` starts a run in the
background (a `key=value` value reads as the param's declared type -- `n=3`, `flag=true` --, quotes group a
value with spaces, a backslash stays); `/stategraph-runs [machine id]` lists the newest runs you may see; `/stategraph-stop
<run id>` terminates one (its `finally` activities run). Every tool answer about a run says in
`next` what it asks of you (running: wait with `get_run(wait='finish')`; waiting: `send_event`;
paused: `continue` or `step`; interrupted: `resume`).
REST for the panel: `/plugins/stategraph/api/…` (design §8.2), admin-only. It also deletes a
machine, which no tool does: `DELETE /plugins/stategraph/api/machines/{id}` with
`{"expected_version": …}` removes the file, its layout sidecar and its companion module unless
another machine uses that module or it lies outside the writable roots; refused (409) while
another machine imports it. Its runs keep their snapshot.

## Panel

**State Graph** lists the machines in collapsible folders by `group` (`Writer/v6`; without
one, "My machines" for the writable root, else the plugin the machine comes with); the open
folders are remembered, and a search opens what it finds. The machines pane and the inspector
fold away (toolbar buttons). A writable machine can be deleted; a double-click on a state
renames it; inspector edits not yet applied are asked about before they are dropped. The
inspector has a form for every field: a state's activity (its kind, and each field of that kind's
schema: `decide`, `input`, `question`, `criteria`, `by`, `timeout`, `retry` ...), its settings
(type, description, max_visits, timeout, entry, exit, a final's status and output, finally) and,
with nothing selected, the machine's (title, description, group, vars_from, params, events,
context, vars, imports, resources, limits, finally). Objects are YAML text; Apply sends the
changed fields only (edits `update_state` / `update_machine`), the file's comments stay. A state's
YAML in the inspector is read in its place in the file, so it may use an alias of an anchor
elsewhere. In the YAML tab, **Python module** gives a writable machine without one its companion
module: `python: <id>.py` after the id line and the file, both drafts until Save (a file of that
name left from an earlier module can be used as it is); a `.py` file is coloured as Python. Each run
has a **Result** card: its output or error, the end state of every frame, and every finished
activity folded with its full answer; an agent's session and the run's own session open in the
chat. A failed poll keeps polling and says "not refreshed"; the run list follows a terminate;
an interrupted run can be terminated (its `finally` activities run).

Working with it: the graph bar finds a state by name, **Undo** (Ctrl+Z) writes back the file as it was
before the last edit, **Redo** (Ctrl+Shift+Z, Ctrl+Y) what the undo replaced, **Auto layout** asks before it drops the positions dragged by hand; the wheel
scrolls the graph, Ctrl+wheel zooms; the palette adds a **Composite** with a first state inside (one edit,
one undo step); Ctrl or Shift+click selects several states and transitions (on a state also +Enter), a Ctrl or Shift+drag box
the states in it -- dragging one moves them all, Delete removes them in one edit (a state inside a selected
composite goes with it), **Group** puts the states into a new composite (placed by hand, they keep their place;
an undo puts their positions back too);
narrow, the state palette is a menu and the machine list folds
away once a machine is open. **Duplicate** copies a machine (a shipped one too) under a new id into
the writable root, its companion module as `<id>.py`; what it imports from a file it names by machine id. The error badge in the head opens the overview
with every problem, each a link to its place. The machine's settings stand first in the overview;
an agent, tool, `by`, `profile` or `machine` field offers the catalog's names (`GET /api/catalog`),
every field says what it is for, and a transition's trigger has **New event…**, which declares the
event and picks it as the trigger (Apply applies it). Applying one form keeps what the other forms hold (the state's
YAML box is asked about: the edit changes the state it shows). A name that is no name, or taken, is
asked again with what was typed. A waiting run has a button per event it takes in the debug bar (an
event with data, or one several frames wait for, opens the event form, which picks the event the
wait takes and says what it is). The runs list scrolls, filters by status and loads older runs;
**Run again** on the Result card starts the run's params and mocks anew, and the start form keeps a
machine's last params.

## Security

Machines contain Python that runs in the API process with the rights of plugin code,
and they run agents and tools. Therefore:

- **Routes** `/plugins/stategraph/*` require the admin role (`config/config.yaml`,
  `auth.plugin_security`).
- **Callback URLs** (`callback` activity) are bearer keys: whoever holds one sends its one event
  to its run once, until it expires (at most 30 days). runs.db keeps only the token's hash, but the
  URL (`/plugins/<instance>/callback?token=...`) lies in the clear in the activity's out (journal,
  ctx, wherever the machine passes it); the server's logs mask its token. Their route is admin-only
  like the rest until the operator
  opens it -- in both layers, each rule before any rule that matches the plugin's other routes
  (`callback*`: URLs made before the token moved into the query carry it in the path,
  `/callback/<token>`, and still work):

  ```yaml
  auth:
    endpoint_security:
      rules:
        - pattern: "/plugins/stategraph/callback*"
          policy: "allow_anonymous"
    plugin_security:
      endpoint_rules:
        - pattern: "/plugins/stategraph/callback*"   # above "/plugins/stategraph/*"
          policy: "allow_anonymous"
  ```

  Opened, a caller without a token that holds gets 404 before any body is read; a body is at
  most 64 KB. With `network.remote_paths` set, a caller on another machine reaches only the paths
  listed there, exactly: add `"/plugins/stategraph/callback"` (a URL with the token in its path
  cannot be listed).
- **Tools** that validate, save, run, control or send events require an admin or a user
  in `allowed_users`. Validating and saving never execute a machine's companion module
  (its names come from a scan); only a run does.
- **Runs belong to their user.** `get_run`, `control_run` and `send_event` (tools and REST)
  answer another user's run as missing unless the asker is an admin or auth is off; a run of
  nobody is visible to all. A resumed or terminated run runs as its own user, whoever
  triggered it; a fork is a new run of the forking user and never continues the source's
  agent instances.
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
- **Browser tests.** The panel is checked with JavaScriptCore, or with node where `jsc` is
  missing (`tests/js/node_jsc.mjs`), not in a browser; the first browser session is a manual
  check in both themes.
- **Cost.** Agent calls report no usage to the engine; only `decide` reports cost.
- **External state is not forked.** A fork replays the machine's own journal; stores,
  database rows and agent conversations keep what the source run did.
- **`call` activities run in mock-only runs.** They are in-process Python; mock the
  ones that reach outside.
- **A sync `call` cannot be stopped.** It runs in a worker thread, so terminate and timeouts
  take effect at once -- the activity ends, the thread's late result is dropped -- but the
  thread runs on to its end. `sg.tool()` works only on the event loop: an async function
  awaits it, a sync one may only return it.
- **A `finally` under a cancelled caller** (the agent facade's request, or one above it) has
  10 s, not 60: the platform then force-cancels the caller's request tree, the `finally`'s tool
  calls and agent runs included.

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
- `stategraph_run_machine` -- "Run a machine. mocks {state path: out} answer instead of the activity ({"$visits": [out1, out2]} per use of the path in the run, {"$error": {type, message}} to fail it); mock_only refuses every unmocked agent, tool or decision. With wait=finish the call returns when the run ends, pauses at a breakpoint or waits for an event (at most max_wait seconds). run_key makes it the same request again: the run with that key is attached to while it runs, resumed when interrupted, and answered again (ended: its status) once it ended; only a transient failure starts a new run."
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
nothing about stategraph. An agent that decides (`decide` with `by:`) gets the questions with
the form of each answer and the content, and answers in JSON.

**The run's own session.** A run with a backend creates the session `sg_<run id>` (title
`<machine title> · <run id>`, agent `stategraph_runner`): at the top level of its user's
session list, or below the session of the agent that started it (the facade, a tool call). It
holds a user message with the params and, when the run ends, an assistant message with status,
final state, and output or error (both `injected_by: stategraph`); a resume finds it there.
Each agent instance is a sub-session of it, so the list shows the run with its
agents' conversations below it. A mock-only run has none. It carries a `depth`, so the chat
opens it read-only: **`stategraph_runner`** is private and never talked to; its prompt only
tells a stray visitor where to go.

No hook: the plugin writes only into its runs' own sessions, never into another conversation.

### Token and cache effect

- The skill body (about 10 KB, roughly 2.6k tokens) and the author prompt (about 4 KB)
  sit in the author's system prompt: static, no ticking values -- a stable cache prefix.
- References are read on demand and **appended** to the history (`format.md` about
  51 KB, `patterns.md` about 20 KB, `debugging.md` about 12 KB); nothing rewrites
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
