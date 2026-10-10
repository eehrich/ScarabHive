# stategraph: backlog from the review of 2026-09-27

Six reviewers, one direction each: runtime bugs, model/panel bugs, missing features,
usability, debuggability, use by user and agent. The bug findings are backed by scripts
(scripts in the session scratchpad, `review_a/`, `review_b/`, `review_e/`) or checked
against the code. The user approved everything ("tackle all of it").

Phase 1 status: built, review of the fix round incorporated (cancellation via the caller now also protects
`finally`/`close` -- `CancellationManager.protect`; steps per frame; a watchpoint pause answers an open
"pause"; breakpoints/`run_to` on states without this hook: 422; the panel carries breakpoints along on rename and
discards stale ones on start; the facade check runs in a worker thread; `expected_versions` as a
JSON string). Open gap, named: the fake DOM does not see that the panel calls `keepingChoices` -- the
helper itself is tested in the browser.

Way of working: phase by phase. Per phase: build, every new test checked with a mutation,
adversarial review of the diff, fix findings, commit limited to the paths. A finished item
gets `[x]` and the commit.

## Phase 1: bugs and gaps

### Runtime and facade

- [x] **R1 Terminate cuts off a running `finally`/`close`.** `server.py` `_cancel_requests` aborts
  every `<run>_NNN` token, including that of the cleanup agent that is currently running (§3.10 promises the
  opposite). Fix: remember the running finally/close request IDs in the RunContext and skip them.
- [x] **R2 `step` in parallel frames gets lost.** `engine/debugger.py` `pause()` sets `mode = "run"`
  as soon as any frame halts; one frame's step is consumed by the other. Fix: bind the step to the
  frame that received it.
- [x] **R3 `run_key` is not bound to machine, `mock_only` and params.** `service.start_run` /
  `journal.latest_by_key`: a mock run with key k later answers a live run with k. Fix: on a
  differing machine, mock_only or params hash, 409 "use a new key".
- [x] **R4 Tool arguments unchecked.** `params` as a JSON string gives a nonsense message, `mocks` as a
  string leaves a `running` row behind (create_run before the check), `max_wait: "abc"` and `files`
  as a string slip past `_run_tool`. Fix: check types/ranges in the tool bodies, parse JSON strings,
  check mocks before create_run.
- [x] **R5 Debug names unchecked.** Breakpoints, `run_to` and mock paths are not checked against the machine;
  a typo in a mock silently lets the real agent run. Fix: check state names (422),
  report `unused_mocks` at the end of the run.
- [x] **R6 Facade: a foreign session ID returns a foreign run.** `facade.py` continue path does not check
  `sees_run`. Fix: check `sees_run` before answering.
- [x] **R7 Facade: the same request in the same session rejected after a transient error.** Fix: if
  `row.run_key == <agent>:<request id>`, go through `_start`.
- [x] **R8 Facade config checked only at the first call.** Fix: check at startup (machine exists
  and validates, `task_param` declared, required params covered), log error.

### Model and panel

- [x] **M1 Event selection jumps back during polling** (`panel.js` drawDebugPane): the chosen value is lost
  on redraw, "Send" sends the wrong event. Same for eventFrame, runToState,
  forkStep. Fix: preserve the selection.
- [x] **M2 Transition form sends all fields**; a multi-line guard becomes one
  line in the `<input>`. Fix: via `field()`/`changedFields`, multi-line as a textarea.
- [x] **M3 Number field with a typo silently deletes** (in the browser `type=number` then returns `""`).
  Fix: `validity.badInput` → FormError.
- [x] **M4 Flow style `states: {x: …}`**: the YAML section shows the sibling map, Apply nests it
  into the state. Fix: block the fragment when the state is not at the start of a line; the server refuses.
- [—] **M5 Scalar anchor not blocked** — REFUTED in the review of the fix round: without the block an edit changes only
  its own state; ruamel moves the anchor to the alias, whose value stays the same (probe `probe_m5.py`). The
  block would only have taken away a harmless path; reverted.
- [x] **M6 `graph_view` bypasses `yaml_bounds`** (alias bomb blocks the API). Fix: on `doc is None`
  an empty graph, bounds also on the re-parse.
- [x] **M7 Validator overlooks a state with `do` without a completion transition** (certain `no_transition`).
  Built as warning SG109, not as an error: an activity that continues only via its error transitions
  is a machine that runs (the semantics tests run exactly such machines).
- [x] **M8 Reserved names `finally`/`resources`** allowed as state names. Fix: `check_state_name` in
  `_new_name`, the same list in the panel.
- [x] **M9 YAML date as a param default** validates, fails at runtime. Fix: non-JSON scalars in the
  loader as SG001.
- [x] **M10 Windows paths with backslash** in `files` (store.relative): problem jump into subfolders
  fails. Fix: `relative()` returns `/`.

### Docs

- [x] **D1 `get_run` fields** in `debugging.md`/design §8.1 wrong (`status` instead of `run_status`,
  `view.frames` instead of `frames`); SKILL line on SG005 (an enum param is allowed).
- [x] **D2 Facade README without `metadata.visibility`** → private, invisible to SAM and chat.
- [x] **D3 403 when saving a bundled machine** without the hint "save under a new id".

## Phase 2: debuggability

Status: built, review of the fix round incorporated (traceback keeps its end; a paused fork halts at the
fork point, not at a state without a preceding `do`; `waiting_since` once per entry, across resume and in the
saved view; `failures` survive a crash in backoff; guards also on initial choice and
discarded event; `full_output` and an overall cap of 200,000 characters for `get_run`; a toast on copy).
N3 (capped responses) is done along with this.

- [x] **G1 Rendered input stays in the journal** (task, tool args, call args, decide input) — today
  the end row overwrites `inputs`. Display in the panel.
- [x] **G2 Guard evaluation** `[{owner, index, guard, result|error}]` in the transition trace and in
  `error.data` of `no_transition`.
- [x] **G3 Traceback** (shortened, frames in the companion module) in `error.data.traceback`; `logger.warning`
  for activity_failed/agent_failed/internal.
- [x] **G4 Failed retry attempts** in `meta.failures`.
- [x] **G5 Fork with `pause` and `mocks`** (RunManager.fork can already do it; service/tool/panel pass it
  through); `pause_at_start` also on the tool `run_machine`.
- [x] **G6 `get_run` with `after`/`kinds`/`key`** and truncation of long fields; panel history with timestamp
  and filter.
- [x] **G7 Waiting made visible**: `waiting_since`, `deadline` in the frame view, `inbox` in the tool response.
- [x] **G8 Panel shows `error.data`/`cause`** on activity errors; request ID copyable.

## Phase 3: use by user and agent

Status: built; interim commit 8b016ff6b, after that the review findings: an aborted `get_run` no longer ends
the run and also waits for runs of other processes; an answer after a restart (or from agent-cli,
one process per message) resumes the run and answers its wait state; as a tool of another agent
`ask` blocks; frames that take the same event are named and chosen via `frame`; an event
discarded by the guard is reported with the guards; data after the event name stays whole; `key=value` reads
by the declared type and keeps backslashes; an invalid `on_wait` is also shown by the panel.

- [x] **N1 Tool `stategraph_list_runs`** (read-only, filtered by `sees_run`); slash commands
  `/stategraph-runs`, `/stategraph-stop`.
- [x] **N2 Keep waiting**: `wait`/`max_wait` on `get_run`; the response on `running` says how to continue;
  `max_wait` capped.
- [x] **N3 Results capped**: `out`/ctx values in tool responses truncated, with length information.
- [x] **N4 `/stategraph-run` with params** (`<id> {json}`).
- [x] **N5 `catalog` returns real tools** with description and parameters instead of allowlist patterns.
- [x] **N6 Facade: wait state in the conversation** — if the run waits for an event, the facade ends the
  round with the question (description, allowed events, schema); the next message in the session becomes
  the event. `on_wait: ask | block` (a machine driven by a job chain stays `block`, because that chain counts every answer as a
  success).
- [x] **N7 Release as an agent in the machine file** (`agent:` block) instead of a config of its own — first check
  whether the registry accepts plugin agents at runtime; otherwise a button that shows the YAML entry.
  Checked: it accepts none (new agents on reload are explicitly not supported; for that the
  core would need a `Runtime.declare(name, ToolServerConfig)` including materialization -- the user's decision, another
  area). The workaround is built: the panel shows a machine's agents, their problems and an entry
  to copy. Addendum 2026-09-28 (user: build it): `agent:` block in the machine file; the core got
  `Runtime.declare` and asks every plugin factory in every process for `offered_servers` before the build
  (announced to the other workstream); a new block counts from the next start, the panel says so. Default
  `visibility: private` (otherwise every SAM with `allowed_agents: ['*']` would offer a freshly saved machine
  to its LLM); a config reload keeps the offered agents. Problems of the block are warnings (SG111):
  the machine itself keeps running.
- [x] **N8 Params in the facade description**, `input: json` takes an object.

## Phase 4: usability

Status: built, review incorporated (duplicating does not write imported files into the writable root,
but refers by machine id, and replaces the whole `python:` line; undo/redo one at a time, also
via key; a form gets its typed text back only if it still shows the same thing; a renamed state stays
shown; "New event…" declares and selects instead of bypassing the form; `events:` without a value; delete
forgets params and undo steps; switching machines restarts the runs list). Redo at the user's request.

- [x] **U1 Several apply forms** of one state: changes in another form survive the
  apply (or "Apply all").
- [x] **U2 Agent/tool/`by`/profile/machine as a selection** (`<datalist>` from the catalog, new route);
  field descriptions visible instead of only in the tooltip.
- [x] **U3 Undo** for graph edits (write the last file text back); auto-layout with confirmation.
- [x] **U4 Answer a wait state**: buttons for the accepted events in the debug bar, event
  preselected, description visible.
- [x] **U5 Runs tab**: result card visible (above the list or the list in a scroller); older runs
  (cursor), status filter.
- [x] **U6 Params/events**: the machine's settings form on top, duplicate tables gone, form as help;
  "New event…" in the trigger select.
- [x] **U7 State search** in the graph bar; mouse wheel pans, Ctrl+wheel zooms.
- [x] **U8 Duplicate** a (also bundled) machine under a new id.
- [x] **U9 Narrow width**: machine list collapses as soon as a machine is open; palette as a menu.
- [x] **U10 "Again with these inputs"** on the result card; params remembered per machine.
- [x] **U11 Name dialogs** keep the input on error; an unchanged name is not an error.
- [x] **U12 Error badge clickable**; overview shows all problems.

## Phase 5: features

Status: built (tests in test_plugin_stategraph_backlog_features.py). F1: the end order of a join policy
is written to the journal by the join itself (`<kind>:joined`), because an activity row keeps the seq of its start.
F8: the route `/plugins/<instance>/callback*` lies under the plugin's admin rule -- it becomes public only
when the user opens it in BOTH layers, `auth.endpoint_security` and `auth.plugin_security` (their
security decision; example in the README under Security). The token is in the query
(`/callback?token=...`), because `network.remote_paths` lets only exact paths through -- a token in the path would
never be reachable from another machine; URLs with the token in the path (issued before the move) remain valid.
The logs mask the token in both forms (core, `loggable_path`: the path form earlier, the query form with
this move, announced to the other workstream).
F9: a slot runs at most three times after a transient failure, five minutes after the last end; the lease applies per
instance and is released on stop.

- [x] **F1 Join policy** `join: first | {count: n}` for `parallel`, `until:` for `map`.
- [x] **F2 Check function in the agent activity** (`check:` with `sg`, may be async, feedback to the same
  instance).
- [x] **F3 Local submachines** in the same file (`machines:`), share the companion module.
- [x] **F4 Timer state** `after: 10m` (expiry = completion).
- [x] **F5 `limits.concurrency`** for leaf activities of a run.
- [x] **F6 `emit`**: intermediate state to the caller and the run session.
- [x] **F7 Operator repair**: `set` on `out` at the exit/error breakpoint.
- [x] **F8 Callback URL per event** (one-time, limited to run and event) — needs its own
  security review.
- [x] **F9 Schedules** (`schedules:` in the plugin config, `run_key` per slot).

## Addenda

- [x] **W1 Fork hook sees the ctx at the fork point** (from a downstream project's review, user 2026-09-28: build it):
  the resources of a fork's root open at the fork point, not at the start; before that they read the source's value
  (hashes: a token), afterwards ctx swaps every value of the source -- and of the runs before it -- for that of the fork, vars
  anew. Journal row `fork_resources` {sources, chain [{values, until}], at}: a fork of a fork replays every
  step with the values its run had there. The point lies before the step's work (also before the
  finally of the abandoned state) and never behind the source's journal; a fork root without a live part
  (ended before the point, or its fork hooks failed) runs no finally -- the source already did --
  and closes only what it opened itself; a fork at step 0 opens at the start. Incidentally: a fork of a fork diverged at the HEAD if ctx carried a value of the
  original source from an output (measured).
