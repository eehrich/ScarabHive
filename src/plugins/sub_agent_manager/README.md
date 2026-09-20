# Sub-Agent Manager

Lets a coordinator agent spawn other agents, keep them alive across turns, and
continue them later with their history intact. This is the plugin the writer
pipeline is built on, and the largest one here — the reason it is large is that
"spawn an agent" is the easy half; the rest is sessions, limits, concurrency
and keeping a sub-agent's prompt in sync with the coordinator's state.

## What it provides

`type = ["tool-server", "web"]`, no pip dependencies.

| Surface | Name |
|---|---|
| Tool | `sub_agent_manager_manage_sub_agent` — one tool, nine operations |
| Hook | `inject_sub_agent_context` (`pre_llm_call`, off by default) |
| Web | the Sub-Agents panel (see below) |

| Operation | Effect |
|---|---|
| `create` | spawn and run (`blocking=true` by default) |
| `continue` | send a further message to an existing instance |
| `poll` | non-blocking status of an async run |
| `wait` / `wait_all` | block until one / all finish |
| `cancel` | stop a running instance |
| `list` | active sub-agents |
| `info` | read a transcript (see pagination below) |
| `delete` | archive the instance |

Parallel work is `create(blocking=false)` several times, then `wait_all` — or
`wake_when_done` and no waiting at all (below).

## A sub-agent is a session, not an object

`SubAgentManager` deliberately does **not** extend `SessionTracker`; it goes
through `SessionManager` for every persistence step. A sub-agent is a session
file linked to its parent, which is why history survives a restart and why
`continue` is cheaper than a fresh spawn — the whole point of the plugin. The
tool description says it outright: do not delete-and-recreate to get a clean
slate, you are throwing away the context you paid for.

If the parent session does not exist yet (a CLI/ephemeral run), it is created
on the fly from `_agent` in the tool params — and that is why a missing
`_agent` is a hard error rather than a default: without the parent's agent name
and profile the session metadata would be silently wrong.

## Context vars: inherited once, refreshed on continue

A sub-agent inherits the coordinator's `context_vars` when its session is
created. Without a refresh, a later `continue` would render its prompt from
that frozen snapshot — the coordinator sets `aufgabe=World`, the task text says
"World", and `{{ aufgabe }}` still says "Idee". Two contradicting instructions
in one prompt.

`merge_parent_context_vars` resolves it by provenance: a key whose value
differs from the inherited snapshot was set by the sub-agent itself and wins;
every other key follows the parent, whose tracker is the live source of truth;
keys only the sub-agent has are kept. Sessions predating
`context_vars_inherited` are treated as fully inherited, which lets the
parent's current values through — the intended behaviour for old sessions.

## Sleeping instead of polling

`create(blocking=false, wake_when_done=true)` lets the caller end its turn over
a background job. When the job ends — finished, failed or cancelled, there is no
second ending — the manager tells the core that input is waiting for the calling
session (`core/session_presence.py`): a session another process holds reads that
at its next step, a session nobody holds is continued in a run of its own. The
woken run is told that input waits; it polls the instance and reads the result
with `info`.

Three things bound it, and none of them are this plugin's:

* **The job lives in the process that started it.** In the API that process
  outlives the turn, which is what makes sleeping possible. A `agent-cli run`
  that ends its turn takes its background jobs with it — there is nothing left
  to finish the job, let alone wake anybody. Use it from sessions the API runs.
* **`session_presence` can be off.** Then nothing is woken and the caller polls,
  exactly as every job did before. The flag costs nothing and changes nothing.
* **`max_wake_depth`** (core, default 3) stops wake chains: a run woken that
  deep wakes nobody, and the input waits for the session's next run.

A wake that cannot be delivered is logged and costs the caller a poll, never the
job: the run's ending is recorded before anyone is told about it.

## Limits and the guards behind them

| Knob | Default | Guards against |
|---|---|---|
| `max_nesting_depth` | 5 | an agent spawning itself forever |
| `max_sub_agents_per_session` | 10 | one coordinator exhausting the host |
| `max_sub_agents_per_type` | 3 | ten copies of the same reviewer |
| `max_message_history` | 100 | unbounded transcripts |
| `default_wait_timeout` | 3600 s | a `wait_all` that never returns |
| `auto_archive_on_limit` | false | — when true, the oldest is archived instead of refusing |

`max_nesting_depth` counts **levels below the session that calls this
manager**, not absolute depth in the session tree: `1` lets a coordinator
spawn workers that cannot spawn anything themselves, `5` allows five levels
below the caller. Each sub-session inherits the remaining budget, and every
manager further down takes the smaller of that budget and its own knob — so a
strict manager bounds its entire subtree, and it goes on working unchanged
when its own agent is somebody else's sub-agent.

Two more that are not limits but guards:

* **No concurrent run of the same instance.** `_running_agents` plus a lock;
  a second `create`/`continue` on a busy instance is refused rather than
  interleaved into one transcript.
* **Instance ids do not collide across parents or restarts.** The counter is
  class-level (shared by every manager instance) behind a class lock, and
  seeded from the time of day rather than zero, so a restart does not re-issue
  the ids of the session still on disk.
* **A run that ended without saying so is healed by `list`.** A sub-agent that
  still looks like it runs but that nobody has in hand is marked `interrupted`
  and its stale activity cleared — otherwise a crash leaves it *running* for
  good. "Nobody" is asked beyond this process: this manager's own jobs, and the
  lock file a run holds next to its session (`core/session_presence.py`), which
  answers the same in every process. Asking only ourselves would declare the
  writer worker's live sub-agents dead, and a woken coordinator would do it to
  the very job it was woken for. With `session_presence` off there is no such
  answer and it falls back to asking itself. This is the only sub-agent state
  `list` writes; the panel writes none at all.

## Which agents may be spawned

`allowed_agents` (`['*']` means all) minus `blocked_agents`, with underscore
names filtered out. This list is rendered into the tool description, so the
model only ever sees spawnable agents — **an agent missing here is not
spawnable, and that only shows up at runtime.**

`phase_filtering` narrows the list further from a session template variable
(e.g. `workflow_phase`): planning phases expose the planning agents, later
phases expose others. It affects both the rendered schema and the `create`
validation, so it is a gate and not just a hint.

`allow_advanced_model` is an instance-level veto over the `use_advanced_model`
argument. A suppressed request is logged, not silently rerouted, and the
sub-agent runs on its normal profile chain.

`advanced_create_only_agents` is the same veto one notch finer: for the agent
types listed there, `use_advanced_model` is honoured on `create` only — a
`continue` on such an instance always runs the normal chain, logged like
above. Built for sub-agents whose advanced chain is a premium model: the
caller's prompt may legitimately ask for advanced continues (synthesis,
stuck), and each of those would be a premium call over the whole accumulated
context. Unlisted types keep the plain `allow_advanced_model` behaviour.

## Reading a transcript

`info` returns the **tail** by default — the most recent messages, which is
what you want right after a run. Reading from the start means paging:
`offset=0`, then `offset=<limit>`, until `window.has_more_after` is false.
Bounds live in `info_default_limit` / `info_max_limit` /
`info_default_max_chars`.

## Config reload

`reload_config()` refreshes the filter and limit knobs from a freshly parsed
config — `POST /admin/reload-config`, `agent-cli reload` — without tearing down
the instance, its running sub-agents or their history, and returns exactly
which fields changed. What it cannot do is add a new agent *definition*: the
agent has to be registered, and that still needs a restart.

## Configuration

```yaml
sub_agent_manager:
  type: sub_agent_manager
  enabled: true
  max_nesting_depth: 5
  max_sub_agents_per_type: 6
  auto_archive_on_limit: true
```

The hook is off by default and enabled per coordinator; its options sit in the
server entry's `hook_config.inject_sub_agent_context` block, and
`max_sub_agents_shown` bounds how much of the list reaches the request on every
call. The block is appended as a `developer` turn at the end and written only
when a
sub-agent is added, removed or changes status (newest created first, no usage
counters or times) -- every change costs the provider cache behind it.

## The panel

**Sub-Agents** (category `agents`, one per manager instance — `sam_writer`,
`sam_skills`, …) shows the sub-agents this instance spawned in the session open
in the chat, or in the one a session link names (`?session_id=`). Without a
session it says so and asks nothing.

- **Figures:** sub-agents, running, idle, interrupted, archived or ended.
  *Running* is an active sub-agent reporting an activity that is not over
  (the rule `list` uses). The panel shows the **stored** state and never
  writes, where the tool's `list` heals what a crash left behind (above).
- **Phase:** with `phase_filtering` on, the session's phase and the agents it
  lets the tool spawn, by the tool's own rule.
- **Cards:** state, id, agent type, messages, last use, task and activity.
  The filter shows the open ones (default), the running ones, or all.
- **Transcript** opens in a drawer on the tail; *Earlier messages* loads the
  pages above it.
- **Archive** (active and interrupted ones) asks first and does what the
  tool's `delete` does: the history stays, `continue` reactivates it.

The list refreshes every 10 s while the panel is visible.

| Endpoint | Answer |
|---|---|
| `GET /plugins/<name>/` | the panel |
| `GET /sub-agents?session_id=` | `{instances: [...], phase: {variable, current, agents, allowed_agents} \| null}` — every sub-agent, archived included, in `list`'s fields, read-only |
| `GET /sub-agents/{id}?session_id=&offset=&limit=` | the transcript window, as `info` pages it |
| `DELETE /sub-agents/{id}?session_id=` | archived, as `delete` |

A sub-agent that does not exist or belongs to another session is a 404.

## Tests

Fourteen files under `tests/`, split by concern — `_manager` (lifecycle),
`_server` (tool surface), `_hooks` / `_hook_integration` (injection),
`_e2e_lifecycle`, `_info_pagination`, `_llm_profiles`,
`_request_id_hierarchy`, `_user_id_injection`, `_context_refresh`,
`_performance`, `_schemas`, and `_panel`: the panel in headless Chromium
against the real router and handlers over session files in `tmp_path`
(`panel_tests.html` holds the checks; skipped without a Chromium browser).

## License

Apache-2.0 — see `LICENSE`.
