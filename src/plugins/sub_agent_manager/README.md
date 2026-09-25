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
| `list` | the sub-agents and their status: running, idle, or how the last run ended |
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
keys only the sub-agent has are kept. A key the parent no longer has stays too:
an empty read of the parent's vars cannot be told from a failed one, and taking
it for a removal would wipe every inherited var on a tracker that did not answer. Sessions predating
`context_vars_inherited` are treated as fully inherited, which lets the
parent's current values through — the intended behaviour for old sessions.

## Sleeping instead of polling

`create(blocking=false, wake_when_done=true)` lets the caller end its turn over
a background job. When the job ends — finished, failed, or cancelled by
something other than its caller (below) — the manager tells the core that input
is waiting for the calling session (`core/session_presence.wake_session`): a
session another process holds reads that at its next step, a session nobody
holds is continued in a run of its own. The woken run is told that input waits;
it polls the instance and reads the result with `info`, and its task arrives as
a `developer` message, so the model can tell a wake from somebody typing.

Two things make that hold rather than nearly hold:

* **The bell is rung again while the session stays held.** A job that ends
  inside the caller's own turn leaves only a marker, and the next LLM step of
  that turn takes the marker because a pre-LLM hook is expected to hand the
  waiting input over. Nothing hands over "your sub-agent is done", so a single
  ring lands in that window and is thrown away — the caller then sleeps over a
  finished job. Measured: a turn that ended 4.3 s before its job did. The core
  repeats the ring, and the manager tells it when to stop: the ending sits in
  the job marked `_awaiting_poll` until somebody reads it, and ringing past that
  would start a second woken run, a whole turn on the user's money.
* **The promise is checked before the caller sleeps on it.** `create` asks what
  can be answered up front — `session_presence` off, no session behind the call,
  a wake chain already at `max_wake_depth`, and the caller being a sub-agent's
  own session, which is never woken (the run that spawned it takes its answer)
  — and says so in its answer, with the reason. The core leaves the last one out
  of `wake_blocked`, since it means reading the session file; a `create` has
  just read and written that very session, so the manager asks it. Told it may
  sleep, a sub-agent that started a job ended its turn over it: its caller got
  "I am waiting" for an answer, and the job's result reached nobody. "Armed" is
  still not a guarantee — the process holding the job has to outlive the turn
  (below). "Not armed" is a certainty, and it used to be the one thing the
  caller was never told.

Three things bound it, and none of them are this plugin's:

* **The job lives in the process that started it, and runs while that process
  drives its event loop.** In the API that process outlives the turn, which is
  what makes sleeping possible. A `agent-cli run` that ends its turn takes its
  background jobs with it — the teardown cancels the task, so the wake it
  still sends carries no result. `agent-cli chat` keeps them, and its prompt now waits *on* that
  loop rather than blocking the thread (`9d9c4652`), so a job runs on while
  nobody types and the wake arrives at the prompt. The same run before and
  after that change: four minutes of wall clock in which it never finished,
  against fourteen seconds. Still no wake on a redirected input path, whose
  fallback reader blocks — and nothing here lets a job outlive its process.
* **`session_presence` can be off.** Then nothing is woken and the caller polls,
  exactly as every job did before. The flag costs nothing and changes nothing.
* **`max_wake_depth`** (core, default 3) stops wake chains: a run woken that
  deep wakes nobody, and the input waits for the session's next run.

What never rings is a job the caller **called off itself**: a `cancel`, or a
`delete` while it runs. Either is the caller saying it is not waiting any more,
said awake, in a turn of its own — a ring up to five minutes later would wake an
idle session for a whole turn about a job it dropped. The two operations mark
the job; the ending reads the mark rather than guessing from a status, because a
status is terminal too when an earlier ending was cut short. Nor does a job
ring whose ending somebody already took (a poll, a wait, a `continue`) — that
somebody is the caller, awake. What still rings: a task cancelled from
elsewhere (its request tree going down, the process shutting down) and an
archiving to make room — both happen behind the caller's back, and it is asleep
over a job it has not heard the end of.

The ringing stops once the ending is read -- wherever it is read. The entry in the
job's process says so for a reader there; a caller woken into a run of its own
reads the stored state instead, where this process's entry says nothing. So an
ending the bell rings for is stored `ending_unread`, and a reader of the stored
state hands it over (poll and wait, a `continue` reopening the instance, the
caller's `delete`); the bell asks both (`_ending_still_unread`, which the core
awaits between rings). It used to ring on over an ending read elsewhere, up to its
budget, and the marker of the last ring woke a second, paid run when that caller's
turn let go. An ending that could not be stored has only its entry to go by.

A wake that cannot be delivered is logged and costs the caller a poll, never the
job: the run's ending is recorded before anyone is told about it. A caller woken
into a process of its own reads that stored ending, so the job's entry in this
process goes once the ring has a run of the session on its way (started by this
ring or another) -- held on, it kept the result for the life of the process, one
per woken job. A caller whose turn outlasts the ringing is woken at its release
instead; that run reads the stored ending too, and the entry here stays until
this process ends or reads it.
An archiving to make room changes nothing about the job: it dropped a finished
job's entry, which could then no longer say the ending is unread, and the ringing
stopped for a caller asleep over it. And the bell
rings outside what turns a cancel into an ending: a shutdown that cancels a job
while it rings — up to five minutes, while the caller's session is held — used
to record a second ending, *cancelled* over the finished one, and ring again.

## Limits and the guards behind them

| Knob | Default | Guards against |
|---|---|---|
| `max_nesting_depth` | 5 | an agent spawning itself forever |
| `max_sub_agents_per_session` | 10 | one coordinator exhausting the host |
| `max_sub_agents_per_type` | 3 | ten copies of the same reviewer |
| `max_message_history` | 100 | unbounded transcripts |
| `default_wait_timeout` | 3600 s | a `wait_all` that never returns |
| `auto_archive_on_limit` | false | — when true, the oldest is archived instead of refusing |

`auto_archive_on_limit` picks the **oldest by creation**, running or not, and
the caller is not told. So it can archive a sub-agent somebody is still waiting
for. That run is not disturbed: while it goes on, a `poll` says *running*; a
clean ending stays archived (rather than putting the instance back into the
count and undoing the room that was made), while *failed* and *cancelled* keep
their own verdict; and its answer is read from the transcript afterwards. What
changes is that the instance is out of `list` from then on. If the archiving
cannot be written, no room was made and the spawn is refused rather than
quietly taking the session over its own limit. With the flag off, the limit
refuses the spawn in the first place. A `continue` of an instance the limits
do not count (anything but active: failed, cancelled, interrupted, archived)
takes a place the same way — it used to reopen past them, and the session
stayed over its limit for good.

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
  interleaved into one transcript. A run of another process counts too: a
  woken coordinator continues from a process of its own, while the job it
  continues may still run in the API. `continue` asks the lock beside the
  sub-session (`core/session_presence.py`) as `list` does, and refuses — two
  runs on one transcript each saved their own, the later over the other. A
  refusal of a busy, missing or foreign instance is the caller's mistake and
  logged at INFO; a slot no running task holds is a leak and logged as an error.
* **The manager writes a parent's sub-agent entries one at a time.** An entry
  is written whole, and a limit is a count that a spawn reads and then fills; a
  lock per parent session holds both. The creates of a fan-out are counted one
  after another — six of them used to pass a limit of three — and an archive
  beside a running sub-agent's activity update is no longer written away. Only
  a `continue` opens an archived instance again, through the limits; a clean
  ending or `list`'s healing leaves it archived, and the healing writes only
  over the state it judged. The lock is per process and holds among the
  manager's writes. A whole-file save of the parent session (the core's
  checkpoint) is not held off by it, and no longer needs to be: `save_session`
  lets the metadata already in the file win (`b579f62fe`), so a checkpoint
  does not write an older `sub_agents` back.
* **Instance ids stay apart across parents, processes and restarts.** The
  counter is class-level (shared by every manager instance) and starts at random
  in each process, so two processes land on one id only by chance; an id already
  on disk is skipped. It started at the time of day, and processes started in the
  same second -- the parallel agent-cli runs of a batch -- counted through the
  same ids.
* **One archiving makes room at both limits.** At the type's limit the oldest of
  that type goes, which frees a place under the session's limit too; the
  session's limit was checked first, archived the oldest of any type, and with
  the type still full a second one.
* **A finished run answers with its own words, job or no job.** The background
  job holds the result text only until somebody reads it, and after a restart
  or an archiving there is none at all. `poll` then reads the last thing the
  run said from its transcript, instead of a fixed sentence about a persisted
  session that a model reads as the answer. An archived instance is found too:
  making room at a limit happens behind the caller's back, and its poll used to
  answer "not found" about a run it started itself. A run **cut off in a tool
  call** has no answer to give — neither the empty step nor the sentence a model
  narrates before working ("let me look at the configuration first"), which
  handed over as a result reads as the sub-agent's finding. Nor has a run that
  **nobody finished**: a process that dies mid-run leaves the sub-agent's
  activity behind, and `list` calls that one *interrupted*. `poll` answers the
  same — *interrupted*, and no result — where it used to hand the transcript
  of a half-done run over as *completed*; `wait_all` counts it as failed.
* **A run this process has no job for is not finished by that.** A blocking run
  never had a job here, a job lives in the process that started it, and a woken
  coordinator polls from a process of its own — so `poll` asks both questions
  `list` asks before calling a sub-agent dead: what runs in this process, and
  the lock a run holds beside its session (`core/session_presence.py`), which
  answers the same in every process. Either one and the answer is *running*,
  rather than a transcript that is still being written handed over as the
  result. `wait` waits that one out: *running* is not an ending, and it used to
  fall through to "disappeared during wait" about a sub-agent that was working.
  A wait ends when the job ends or when `default_wait_timeout` does, and while
  it has no job of its own to watch it looks less and less often — each of
  those looks reads the parent's session, re-parsed whenever a sub-agent wrote
  its activity.
* **Running or idle is asked of the run, not read from the status.** This
  process's own runs answer first, then the lock beside the sub-agent's
  session — a probe of the lock file, not a read of the session, since the
  injected list asks before every LLM call. A run holds that lock from right
  after `start`, through its setup, until after its ending event. It reports
  an activity only inside that span: cleared at the ending event, because the
  run lets go of the lock before its trailing lines and `end`. An activity
  nobody holds is what a crash leaves; a word outside the span would heal a
  run that is fine. Where nobody holds it, time decides, not words (mid-run a
  tool's status line ends with "completed" too): an activity written after
  the last recorded end (`last_used`) was left by a run whose end nobody
  recorded — its process died, or its caller's (measured 10 such records,
  9 of them crashes mid-LLM-call); one written before it is the last line of a run from
  before the activity was cleared at every ending — idle, 1118 records. A
  `continue` reopens an instance without such a leftover. With
  `session_presence` off only this manager's own runs answer, so a run of
  another process — or, in the map, of another SAM instance — cannot be told
  from a crash and reads *interrupted*, the word `list` heals it to. A run
  that aborted — its answer "Error: ..." or "Cancelled: ..." — or raised
  stores `failed` or `cancelled`, blocking or in the background; a clean one
  opens the instance again, unless it was archived while it ran.
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

Both vetoes filter that argument only. A sub-agent with
`agent_config.inherit_parent_llm` follows a caller that was switched to
another profile, a premium one included (`docs/config_based_agents.md`).

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
when a sub-agent is added, removed or changes status (open ones first, each
group newest created first, with its task, no usage counters or times): a change costs that turn, and
nothing before it.

The status column says what a sub-agent is doing, the same words `list`,
`info` and the panel use: `running` while a run is under way, `idle` once it is over, or
`interrupted`, `failed`, `cancelled` for a last run that did not finish.
Stored, running and idle are both `active` -- a clean ending stores it too --
so the block would read the same before and after a worker finished; the run
itself tells them apart (see the limits and guards above). A failed or cancelled one is listed like an
open one, since the coordinator may not have ended it itself; archived ones
only with `show_completed`.

## The panel

**Sub-Agents** (category `agents`, one per manager instance — `sam_writer`,
`sam_skills`, …) opens on the session open in the chat, or on the one a session
link names (`?session_id=`). Without a session it says so and asks nothing. Two
tabs over the same session: the **map** it opens on, and the **list**. Both
show what **any** manager instance spawned, so every instance's panel shows the
same session alike; a transcript and an archive go to the instance that spawned
the sub-agent, which holds its runs and its jobs. (The list used to show the
panel's own instance only, and was empty beside a full map on every session
whose agent spawns through another.)

It shows its viewer's own sessions only, by the rule of `/sessions` (the signed-in
user, else `anonymous`), and reads them under that user. A session of another
user shows no sub-agents and maps to itself alone, and a transcript or an archive
of one is not found: each asks first whether the session and the sub-agent are
the viewer's (`belongs_to`, a stat). Figures and whether one runs are looked up
for her own sub-sessions only — a session's metadata is its user's to write, and
an entry naming another user's sub-agent had those looked up by the bare id. It
is shown as its entry says, as one whose sub-session was deleted is, so neither
tells whether an id exists elsewhere. The panel used to ask the session
directories whose a session id is, and answered anyone who named one.

Both carry the same two figures per sub-agent, each left out where it is not
known:

- **messages** — its transcript's length as of its last save, from the
  sub-index of its parent (`.subs.<parent>.index.json`, one small file per
  parent, rewritten at every save of a sub-session). No transcript is read for
  it. The count in the parent's entry is no substitute: it lags a run behind
  and was measured wrong besides (0, 2 or 9 where the transcripts held 4
  to 27).
- **tokens** — what its last LLM call carried, as the provider counted it
  (prompt tokens), with the share of the window, or "(compacted since)" once
  the context was rewritten after that call. From `context_usage_tracker`,
  whose database holds the calls of every process that runs it — the number the
  chat shows for a session. Without that plugin, or before a first call,
  there is none: an estimate of the panel's own would be a second number beside
  that one.

### Map

The session and everything below it, nested: a sub-agent's own sub-agents hang
under it. A node names its state, what it was given to do, its agent type, its
id, its messages and tokens (above) and how long it was at it (creation to last
use), and a click opens its transcript — read against its own parent, not
against the session in the chat.

A node is read only when a stat on its sub-index says it has sub-agents at all;
a leaf's figures come from the sub-index above it and the tracker, not from its
own file. A node whose last sub-agent was *deleted* loses that sub-index with it
and shows as the leaf it has become.

The walk is bounded three ways — the sub-agents it answers with (300), the
sessions it reads (60; `load_session` reads the whole transcript with them) and
the depth. Whichever one cuts something off, the answer says `truncated` and
the panel says so too: a branch cut short looks exactly like a leaf otherwise.

### List

- **Figures:** sub-agents, running, idle, interrupted, and failed,
  cancelled or archived. The state is the server's, in the words and by the
  rule of the tool's `list` (above) — it asks the run, not the stored
  `active`. Unlike `list` the panel never writes: a run that died shows
  `interrupted` here, and stays so stored until `list` heals it.
- **Phase:** with `phase_filtering` on, the session's phase and the agents it
  lets the tool spawn, by the tool's own rule.
- **Cards:** state, id, agent type, messages, tokens, last use, task and
  activity.
  The filter shows the running, idle and interrupted ones (default), the
  running ones, or all — it
  belongs to this tab; the map always shows the whole session.
- **Transcript** opens in a drawer on the tail; *Earlier messages* loads the
  pages above it.
- **Archive** (running, idle and interrupted ones) asks first and does what the
  tool's `delete` does: the history stays, `continue` reactivates it (taking
  a place under the limits).

Both refresh every 10 s while the panel is visible, whichever tab is showing.

| Endpoint | Answer |
|---|---|
| `GET /plugins/<name>/` | the panel |
| `GET /sub-agents?session_id=` | `{instances: [...], phase: {variable, current, agents, allowed_agents} \| null}` — every sub-agent of the session, any instance's, archived included, in `list`'s fields with `message_count` (`null` where unknown) and `context_tokens`/`context_window`/`context_stale` (left out where unknown), read-only |
| `GET /agent-map?session_id=` | `{root: {instance_id, title, agent_type, children: [...]}, truncated}` — the tree, every node carrying its `parent_session_id`, its own `children` and the list's figures, read-only |
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
