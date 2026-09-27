# Testing and debugging machines

1 Mocked test runs · 2 Reading a run · 3 When a run fails · 4 Breakpoints and
watchpoints · 5 Resume · 6 Fork · 7 Limits

---

## 1. Mocked test runs

```text
stategraph_run_machine(machine_id="scene_review", params={"premise": "a heist"},
                       mock_only=true, mocks={...}, wait="finish")
```

- `mock_only: true` refuses every agent, tool and decide activity without a mock
  (error `unmocked`). A mock-only run has no backend at all: even a forgotten mock
  cannot reach an agent or a tool.
- `call` activities, code fields and templates run for real. Mock a `call` that
  reaches outside the process.
- A mocked activity still renders its templates, so a broken template shows in a
  mocked run too (`template_failed`).

### Mock keys: state paths

| What | Key |
|---|---|
| a state of the machine (nested or not) | `write` |
| a state inside a submachine called from state `review` | `review/critique` |
| a branch of a `parallel` in state `opinions` | `opinions/style` |
| item 2 of a `map` in state `translate` (0-based) | `translate/2` |
| a state of a submachine run by map item 0 | `translate/0/critique` |

A mock on the state that runs a `machine`, `parallel` or `map` answers the whole
activity: nothing inside runs. A mock on an inner path answers only that part.

### Mock values

The value is `out` as the machine sees it -- after `schema` or `parse`:

| Activity | Mock |
|---|---|
| agent, plain | `"the answer text"` |
| agent with `schema`/`parse` | the parsed value: `{"notes": "...", "blocking": true}`, `4`, `true` |
| tool | the tool's result: `{"status": "success", "doc": "result"}` |
| decide noul/choice/score | `{"value": 0.8, "confidence": null, "probabilities": null}` |
| decide questions | `{"name": {"value": ..., "confidence": null, "probabilities": null}, ...}` |
| machine | its final output: `{"notes": "..."}` |
| parallel | `{"branch": out, ...}` (or per branch with inner paths) |
| map | the list of results (or per item with inner paths) |

Special forms:

- `{"$visits": [first, second, third]}` -- answers by use of that path in the run: the
  first use gets `first`, the second `second`; the last value repeats. The count runs
  across the whole run, so a submachine called again (a new frame each time) still gets
  the next answer, and a resumed run continues the count. Every state a loop enters more
  than once needs this, or the loop sees the same answer every round.
- `{"$error": {"type": "tool_failed", "message": "doc not found", "data": {...}}}` --
  the activity fails with that error. Combine: `{"$visits": [{"$error": {...}},
  "second try works"]}`.

### Proving a machine

A machine is proven when a mocked run reaches a final state along the path that
matters, and each error path you rely on has been driven once with `$error`. Check
`run_status` and `state` of the result -- its `status` is the tool call's own (`success`
or `error`), not the run's. A mock path no activity used shows in `mocks_unused`: a
typo there lets the real activity run. A run in status `waiting` lists the events
it `accepts`: send one with `stategraph_send_event(run_id, name, data)`, then read the
run with `stategraph_get_run`. A run in status `paused` stopped at a breakpoint
(section 4).

---

## 2. Reading a run

`stategraph_get_run(run_id, steps=30)` returns (besides `status`, the call's own
`success` or `error`):

- `run_status`: `running`, `waiting` (a wait state waits for an event), `paused`
  (debugger), `interrupted` (the process stopped; resumable), `succeeded`, `failed`,
  `cancelled`.
- `state` (the final state once the run ended, else the root's active state),
  `output`, `error` (`type`, `message`, `state`, `data`, `cause`), `paused` (where the
  debugger holds it), `mocks_unused`.
- `frames`: one entry per active frame (the root, and every running submachine):
  its active states, `ctx`, `params`, step, visit counts, and the events it accepts.
- `accepts`: per waiting frame, the events it takes now.
- `journal`: the last rows -- `activity` (key, state, status `started`/`done`/`error`,
  `out` or `error`, meta with `mocked`, `attempts`, `instance_id`), `trace` (enter,
  exit, transition, final), `event`, `edit`, `timer`.

Journal keys name the step: `s3` is the activity of top-level step 3, `s3/b.style` a
parallel branch, `s3/i.2` map item 2, `s3/m/s1` step 1 inside the submachine that
step 3 started.

A run belongs to the user who started it: `get_run`, `control_run` and `send_event`
answer another user's run as missing, unless you are an admin or auth is off.

---

## 3. When a run fails

| `error.type` | Usual cause | Fix |
|---|---|---|
| `unmocked` | a mock-only run met an agent/tool/decide state without a mock | add a mock for that path (the message names it) |
| `no_transition` | a state completed and no completion transition's guard held | end the guarded list with `guard: else` |
| `loop_limit` | a state was entered more than `max_visits` times | a mock returns the same value every round (`$visits`), or the loop's exit guard never holds |
| `guard_failed` | a guard raised, or read-only code changed `ctx` | read the message; move mutations into an `effect` |
| `action_failed` | an `effect`, `entry` or `exit` raised (KeyError, TypeError, …) | the message names the field; the transition was rolled back |
| `template_failed` | a template raised (a missing ctx field, a name not bound there) | render only what exists at that moment |
| `not_serialisable` | `ctx` got a non-JSON value (a set, an object) | convert: `sorted(...)`, `list(...)`, `str(...)` |
| `params_invalid` | a missing required, unknown or wrongly typed parameter | fix `params` of the run or the `machine` activity |
| `schema_invalid` / `parse_failed` | the agent's answer stayed unusable after the feedback rounds | clearer task; more `parse_retries`; a more tolerant parser |
| `submachine_failed` | the submachine ended in a failed final (`error.data`) or failed inside (`error.cause`); the message ends with the cause: `<alias> ended in <state> (failed): <cause type>: <cause message>` (cause cut at 500 characters) | read `error.cause` |
| `step_limit` | more than `limits.max_steps` dispatches in one frame | an unbounded loop |
| `diverged` | a resumed or forked run did not repeat the recorded run | impure code, or the definition changed (section 5) |

---

## 4. Breakpoints and watchpoints

Set them when starting (`stategraph_run_machine(..., breakpoints=[...],
watchpoints=[...])`) or later (`stategraph_control_run(run_id,
action="set_breakpoints", breakpoints=[...])`; the list replaces the old one).

**Breakpoints** pause at a hook of a state:

| Form | Pauses |
|---|---|
| `"judge"` | on `enter`: the state is entered, its activity has not started; `ctx` can be read and changed |
| `"judge@exit"` | on `exit`: the activity finished, `out` is known, no transition chosen yet |
| `"judge@error"` | on `error`: an error is about to be dispatched |
| `{"state": "judge", "at": "exit", "condition": "out[\"value\"] < 0.5", "machine": "scene_review"}` | only when the condition holds, only in that machine's frames |

**Watchpoints** pause when a value changes: `"ctx.round"`, `"len(ctx.issues)"`, or
`{"expr": "ctx.score", "condition": "new < old"}` (`old` and `new` are bound). The
value is compared after every dispatch, per frame.

Conditions and watch expressions are read-only Python over the scope at that hook.

**Controls** (`stategraph_control_run(run_id, action=...)`):

| Action | Effect |
|---|---|
| `pause` | pause at the next hook (a running agent call finishes first) |
| `continue` | run on until the next breakpoint |
| `step` | run to the next hook |
| `run_to` + `state` | a one-off breakpoint on that state's entry (`state` is required) |
| `evaluate` + `expr` | a read-only expression against the paused scope: `ctx.draft[:200]`, `out`. The answer is JSON; a value that is not data (a function, a module) comes as its `repr`, as watch values do |
| `set` + `path` + `expr` | while paused: `ctx.<path> = <expr>` (e.g. `path="round"`, `expr="0"`); journaled, so a resume applies it again |
| `terminate` | cancel the run and its running agent calls; its `finally` activities run first. An interrupted run is resumed into its termination, so they run there too; if its definition no longer loads, it is marked cancelled without them, and its error says so |

Arguments of the wrong type are refused before anything acts, and so are malformed
breakpoints and watchpoints (a list of strings or objects; `enabled` a boolean). A stored
point that no longer parses is dropped, not refused: it never blocks a resume, terminate or
fork. `steps` (1-200) makes the answer carry that many journal rows.

`run_machine` with `wait: "finish"` also returns when the run pauses, so you can
inspect and continue.

---

## 5. Resume

`stategraph_control_run(run_id, action="resume")` continues an `interrupted` run (its
process stopped). The engine re-runs the machine from the start against the run's own
definition snapshot:

- Every activity with a recorded outcome returns that outcome without running again.
  Events, debugger edits and fired timers are applied at the same steps.
- An activity that was **in flight** at the stop runs again if it is idempotent (the
  default, except `tool`). A non-idempotent one raises `interrupted` in its state, with
  `error.data` holding its rendered inputs: handle it, e.g. by checking whether the
  first call took effect. A non-idempotent submachine ends the frame it had started first,
  so its `finally` and `close` run.
- A wait state's deadline is stored; the resumed run waits only for the rest. So is a
  retry's backoff: a resume inside it waits out the rest and goes on with the next attempt.
- The debugger's state survives the stop: a run that stopped while paused pauses again at
  its first live hook, and a pending `step` or `run_to` still applies.
- It runs as the run's own user, whoever resumes it.
- If the machine does not repeat the recorded run -- another kind or path at a step,
  other rendered inputs, another `ctx` after a step -- the run stops with `diverged`,
  naming the first differing key. That means impure code (a clock, randomness, a set's
  order, I/O in a guard or template) or a changed definition. A diverged run never
  continues on another path.

A `run_key` makes a request idempotent: `run_machine` with the same key gets the newest
run of that key instead of a second one -- attached to while it runs (`attached: false`
if another process runs it), resumed when interrupted, and once it ended its outcome
again (`ended: <status>`: the same output, failure or cancel). Only a failure that is
transient -- `interrupted`, `timeout`, `agent_failed`, `decision_failed`, `internal` or
`diverged` as the error or an unhandled cause -- starts a new run.

---

## 6. Fork

`stategraph_control_run(run_id, action="fork", at_step=N)` starts a new run that
replays the source run's journal below top-level step N and runs live from there.

- `definition: "snapshot"` (default) uses the source run's definition;
  `definition: "current"` uses the machine as it is now -- fix a guard, then fork from
  the step before it. A divergence in the replayed part aborts the fork and names the
  key.
- **External state is not forked.** Stores, database rows and agent conversations keep
  what the source run did after step N. The fork gets its own session, so a
  `continue` into an agent instance created before the fork point fails.
- The fork is a new run of the user who forks. A wait state it reaches at the fork point
  starts its `timeout` afresh.
- A fork has its own `run.id`. A replayed step whose rendered inputs, or whose effect
  on `ctx`, contain `run.id` no longer matches the journal, and the fork stops with
  `diverged` at that step. Name external things with `run.origin` instead: it is the
  id of the first run of the fork chain, the same in the source run and every fork,
  so replayed steps match and the fork finds what the prefix created (and shares it
  with the source run -- external state is not forked).

---

## 7. Limits

- Only runs of the process that serves the panel can be paused from it; runs of other
  processes are visible through their journal.
- Pausing is cooperative: it takes effect at the next hook; an agent call in progress
  is never frozen.
- A mocked run proves the machine's logic, not the agents: a live run is still the
  test of prompts, tools and timeouts.
