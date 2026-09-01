# Sub-Agent Manager

Lets a coordinator agent spawn other agents, keep them alive across turns, and
continue them later with their history intact. This is the plugin the writer
pipeline is built on, and the largest one here — the reason it is large is that
"spawn an agent" is the easy half; the rest is sessions, limits, concurrency
and keeping a sub-agent's prompt in sync with the coordinator's state.

## What it provides

`type = ["mcp-server", "web"]`, no pip dependencies.

| Surface | Name |
|---|---|
| Tool | `sub_agent_manager_manage_sub_agent` — one tool, nine operations |
| Hook | `inject_sub_agent_context` (`pre_llm_call`, off by default) |
| Web | panel plus `/list`, `/delete`, `/stats`, `/phase-info` |

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

Parallel work is `create(blocking=false)` several times, then `wait_all`.

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

## Limits and the guards behind them

| Knob | Default | Guards against |
|---|---|---|
| `max_nesting_depth` | 5 | an agent spawning itself forever |
| `max_sub_agents_per_session` | 10 | one coordinator exhausting the host |
| `max_sub_agents_per_type` | 3 | ten copies of the same reviewer |
| `max_message_history` | 100 | unbounded transcripts |
| `default_wait_timeout` | 3600 s | a `wait_all` that never returns |
| `auto_archive_on_limit` | false | — when true, the oldest is archived instead of refusing |

Two more that are not limits but guards:

* **No concurrent run of the same instance.** `_running_agents` plus a lock;
  a second `create`/`continue` on a busy instance is refused rather than
  interleaved into one transcript.
* **Instance ids do not collide across parents or restarts.** The counter is
  class-level (shared by every manager instance) behind a class lock, and
  seeded from the time of day rather than zero, so a restart does not re-issue
  the ids of the session still on disk.

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

The hook is off by default and enabled per coordinator; `max_sub_agents_shown`
bounds how much of the list reaches the system prompt on every call.

## Tests

Thirteen files under `tests/`, split by concern — `_manager` (lifecycle),
`_server` (tool surface), `_hooks` / `_hook_integration` (injection),
`_e2e_lifecycle`, `_info_pagination`, `_llm_profiles`,
`_request_id_hierarchy`, `_user_id_injection`, `_context_refresh`,
`_performance`, `_schemas`.

## License

Apache-2.0 — see `LICENSE`.
