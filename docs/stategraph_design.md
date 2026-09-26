# stategraph: design and architecture

Status: prototype (plugin `src/plugins/stategraph/`, branch `stategraph`).
Audience: software and AI developers who model agent workflows and extend the engine.

stategraph turns an agent workflow into a **UML-style state machine** stored as YAML.
States run activities: an agent call, a tool call, a decision model, a Python function, a
submachine, or a fan-out. Transitions carry **Python guards and effects**. A deterministic
interpreter executes the machine. It journals every step, resumes after a crash without
repeating finished work, and pauses on breakpoints and watchpoints. A panel shows the
machine as a graph, where it can be edited, run and debugged. An authoring agent with its
own skill writes machines.

The long-term goal is to express writer v6 and later writer v4 as such machines. v6 is
today an LLM coordinator following a "strict ritual" in its prompt. As a machine, its
control flow becomes code you can read, validate, test and debug, and the agents stay the
leaves.

This document is the contract: the file format (§2), its semantics (§3), validation (§4),
execution and the journal (§5), the debugger (§6), and the plugin around them (§7–§9).
The plugin's `docs/format.md` is the reference for machine authors. It must not contradict
this document.

---

## 1. Decisions

| # | Decision | Alternatives | Reason |
|---|---|---|---|
| D1 | Own small interpreter, no workflow library | python-statemachine, transitions, sismic, Burr, LangGraph, pydantic-graph, Temporal, Prefect, DBOS | Measured in `docs/agent_workflow_orchestration_design.md` §5, `docs/repair_state_machine_design.md` §2 and again for this plugin. Every library leaves at least one of three things to us: our own keys in the YAML, resume without repeating finished agent calls, and pause/edit at step boundaries. The heavier ones bring 38–104 transitive packages. Libraries are used around the core: pydantic, ruamel.yaml, networkx, jsonschema, elkjs. |
| D2 | YAML with a version key (`stategraph: 1`), validated by pydantic models; the JSON schema is generated from them | JSON; Python DSL; SCXML | YAML is what the repo uses for agents and config. ruamel keeps comments on round trip, and LLMs write YAML reliably. SCXML has no agent concepts. |
| D3 | A **subset of UML 2.5 state machines** (§3.1) | flat graph (n8n, Dify); ASL `Next` | The user asked for UML conformance, and v4 needs hierarchy four levels deep. The subset is stated explicitly, so nobody expects what it leaves out. |
| D4 | Guards, effects, entry/exit actions and template expressions are **real Python** | Jinja, simpleeval, CEL | Python is the platform. Machine code is trusted like plugin code. Every stategraph tool that saves, runs or debugs a machine requires the admin role (§8.3). All code is compiled and name-checked at validation time. |
| D5 | One templating rule, for a closed list of data fields: `{{ expr }}` holds a Python expression, and a value that is exactly `{{ expr }}` keeps its type | Jinja templates | One expression language everywhere. |
| D6 | Reuse through **parametrised submachines** imported from other files; composite states for grouping | text includes; inheritance (`extends` with overrides) | Composition keeps every file self-describing: the graph you see is the graph that runs. Inheritance hides the effective graph across files, and text includes break line numbers and validation. A submachine is a box in the editor and a frame in the debugger. |
| D7 | Durability by **deterministic replay** of a step journal (the Temporal model) | snapshot/restore of the configuration | Replay handles nesting, parallel branches, map items and submachines uniformly and never repeats a finished activity. It also gives a what-if fork on the machine's own state; external state is not rolled back (§5.6). It requires pure guards and actions, and the engine checks that with input and context hashes (§5.4). |
| D8 | Agents via `AgentCaller` on a SAM; tools via `dispatch_tool_call` of a dedicated runner agent; decisions via `create_decisions_from_profile` | direct `run_events` transport | These are the existing primitives. Sub-sessions show up in the Sub-Agents panel. Each kind has one authorization boundary: the SAM's `allowed_agents` for agents, the runner's tool allowlist for tools. |
| D9 | Activity kinds are a **registry**; other plugins add kinds without touching the engine | a closed union | Extensibility is a requirement, and v4 migration needs code nodes and new kinds. |
| D10 | Editor: own SVG canvas + vendored **elkjs** (EPL-2.0) for compound auto-layout; YAML edits happen server-side and keep comments | AntV X6, JointJS, diagram-js, Drawflow | The panel rules forbid CDNs and build steps. X6 (583 KB) and diagram-js (needs a bundler) bring an object model we would have to mirror. Our graphs are small (tens of states), SVG styled with kit tokens works in both themes, and ELK lays out nested states. |
| D11 | Debugger primitives follow DAP names: breakpoints (enter/exit/error, with condition), watchpoints (expression, break on change), pause/continue/step/run_to/terminate, evaluate, set, fork | step-through only | LangGraph, Burr, Temporal and n8n converged on this set. DAP names keep a VS Code adapter possible. |

---

## 2. The file format (`stategraph: 1`)

A machine is one YAML file `<id>.yaml` in a machine root (§7.2). It may have a companion
Python module and a layout sidecar `<id>.layout.json`, which holds editor positions and is
never read by the engine. Keys are chosen so YAML 1.1 readers cannot corrupt them: there is
no `on`, `yes` or `no` key. Unknown keys are errors (`extra="forbid"`), so a typo never
silently drops behaviour.

### 2.1 Example

```yaml
# machines/scene_review.yaml
stategraph: 1
id: scene_review
title: Scene review loop
description: Write a scene, critique it, let Jev judge readiness, revise until ready.
python: scene_review.py          # companion module: its public names are in scope
imports:
  critique_round: ./critique_round.yaml

params:                          # bound by whoever starts the machine
  premise:    {type: string, required: true}
  max_rounds: {type: integer, default: 3}

context:                         # the machine's variables, with initial values (JSON)
  draft: null
  critique: null
  round: 0

vars:                            # template vars every spawned agent inherits
  genre: thriller

initial: write

states:
  write:
    entry: ctx["round"] += 1
    max_visits: 5                # the 6th entry raises loop_limit in this state
    do:
      agent: scene_writer
      task: "{{ writer_task(ctx, params) }}"
      retry: {attempts: 2, backoff: 5s}
    transitions:
      - target: review
        effect: ctx.draft = out
      - trigger: error
        target: failed

  review:                        # a submachine state: parametrised, reusable
    do:
      machine: critique_round
      params: {text: "{{ ctx.draft }}", strict: true}
    transitions:
      - target: judge
        effect: ctx.critique = out["notes"]

  judge:
    do:
      decide: noul
      question: Is this scene ready to publish without another revision?
      input: "{{ {'scene': ctx.draft, 'critique': ctx.critique} }}"
    transitions:
      - target: done
        guard: out["value"] >= 0.7
      - target: write
        guard: ctx.round < params.max_rounds
      - target: give_up
        guard: else

  done:
    type: final
    output: {draft: "{{ ctx.draft }}", rounds: "{{ ctx.round }}"}
  give_up:
    type: final
    status: failed
    output: {reason: "not ready after {{ ctx.round }} rounds"}
  failed:
    type: final
    status: failed
```

```python
# machines/scene_review.py -- pure functions only (§3.6)
def writer_task(ctx, params):
    task = f"Write the scene for this premise: {params.premise}"
    if ctx.critique:
        task += f"\n\nRevise this draft:\n{ctx.draft}\n\nusing this critique:\n{ctx.critique}"
    return task
```

### 2.2 Top-level keys

| Key | Type | Req. | Meaning |
|---|---|---|---|
| `stategraph` | `1` | yes | Format version. The loader refuses unknown versions. |
| `id` | name | yes | Machine id, unique across all machine roots. The file is `<id>.yaml`. |
| `title`, `description` | string | | Shown in the panel and the catalog. |
| `python` | path | | Companion module, relative to the file. Its public names are in scope for all code of this machine. |
| `imports` | alias → ref | | Submachines this machine uses. A ref is a relative path (`./x.yaml`) or a machine id. `do: {machine: …}` names an alias, never an id. |
| `params` | name → field | | The machine's parameters. For a top-level run they are the run input; for a submachine, the `params:` of the calling activity. Field keys: `type` (`string`, `integer`, `number`, `boolean`, `object`, `array`, `any`), `required`, `default`, `enum`, `description`. |
| `events` | name → `{description, data}` | | The named events this machine accepts (§3.4). A trigger that is neither `done`, `error` nor declared here is an error. `data` is an optional JSON schema for the payload. |
| `context` | name → JSON | | The machine's variables with their initial values. They are plain JSON, not templates. |
| `vars` | name → template | | Agent template vars (§3.9) for every agent this machine spawns. Submachines inherit them. |
| `vars_from` | agent name | | Import the `template_vars` of that agent's configuration under `vars`. This keeps shared prompt blocks (e.g. v6's Verbote/Klischees) in one place. |
| `sam` | string | | SAM instance for agent activities (default: the plugin's `default_sam`). |
| `limits` | `{max_steps, timeout}` | | `max_steps` (default 1000) bounds the dispatches of each frame of this machine. `timeout` bounds the running time of a whole run; it is honoured on the root machine only. |
| `resources` | name → `{open, fork, close, description}` | | External state that belongs to each frame of this machine (§2.8). |
| `finally` | activity | | Runs once when a frame of this machine ends, whatever the cause (§2.8). |
| `initial` | state name | yes | Target of the top-level initial pseudostate: a top-level state or a choice/junction. |
| `states` | name → state | yes | The top-level region. |

Names (`id`, states, params, context keys, aliases, events, branches, resources) match
`[a-z][a-z0-9_]*`. `finally` and `resources` are not state names: activity paths use them.

### 2.3 States

State names are **unique within a machine**, including nested states, so a transition
target is always a plain name.

| Key | Applies to | Meaning |
|---|---|---|
| `type` | all | `state` (default), `choice`, `junction`, `final`. |
| `description` | all | Free text for the editor. |
| `entry` / `exit` | state | Python statements run on entering / leaving. |
| `do` | simple state | The do-activity (§2.5). Its completion is the state's completion. |
| `finally` | simple and composite state | An activity that runs once on every exit of the state, whatever the cause (§2.8). |
| `states` + `initial` | composite | A nested region (one region per composite). The composite completes when one of its direct `final` children is entered. |
| `transitions` | state, choice, junction | Ordered list (§2.4). |
| `max_visits` | state | The (n+1)-th entry within one activation of the parent region raises `loop_limit` in this state (§3.7). |
| `timeout` | wait state | A duration. When it expires, `wait_timeout` is raised in the waiting state. |
| `status` | final of the root region | `succeeded` (default) or `failed`. |
| `output` | final | A template value. The root final's output is the frame's output: the run output, or `out` for the caller of a submachine. A nested final's output is `out` for its composite's completion transitions. |

A simple state is one of two kinds.

- A **wait state** has no `do` and no completion transition. It waits for named events.
- Otherwise the state **completes**: when its `do` finishes, or right after entry if it has
  no `do`. Its completion must select a transition, or `no_transition` is raised (§3.3).

### 2.4 Transitions

```yaml
transitions:
  - trigger: done              # default: completion. Or "error", or a declared event.
    guard: out["value"] > 0.5  # Python expression or "else"; optional
    effect: ctx.x = out        # Python statements; optional
    target: next_state         # required, except for internal event transitions
```

- A transition **without `target`** is an internal transition: its effect runs with no
  exit and no entry. It is allowed only with a named-event trigger.
- `else` holds always. It must be the last transition of its trigger within the state.
- `choice` and `junction` transitions have no trigger (they are taken immediately), need a
  target, and a choice must end with `else`.

### 2.5 Activities (`do:`)

A `do` mapping has exactly one **kind key**. The remaining keys belong to that kind or are
common keys.

**Common keys**

| Key | Meaning |
|---|---|
| `retry` | `attempts` counts the total tries (default 1). `backoff` is a duration, or `{initial, factor, max}`. `errors` lists the error types to retry (unknown names get SG110); the default is every type except `cancelled`, `interrupted`, `timeout`, `template_failed`, `params_invalid`, `not_serialisable`, `unmocked`, `no_backend`, `config`, `tool_denied`. A composite (`machine`, `parallel`, `map`) retries by running its children again, so two more rules apply. It is never retried once an activity inside it raised `interrupted`, whether that error ended the attempt, was handled inside a submachine, or was journaled next to another branch's failure -- except a `finally` or `close` of a frame inside it (§5.5; naming `interrupted` gets SG110). And a type from the default-excluded list in the error's chain of unhandled causes (`error.cause`, its `cause`, ...) excludes the composite too, unless `errors` names that type; a cause the submachine handled itself is not in that chain. The engine is the only retry layer: AgentCaller runs with `retries=0`. |
| `timeout` | Deadline of **one attempt**. On expiry the attempt is cancelled (for an agent: its sub-run, through the request-id prefix) and error `timeout` is raised. A timeout is not retried unless `errors` lists it. An agent activity has no timeout by default: v6 panels run 20+ minutes. |
| `idempotent` | Whether a resumed run may start this activity again if it was in flight at the crash (§5.5). Default `true`, except `tool`: `false`. |
| `description` | Free text. |

**Kinds** (the fields listed as *templates* are rendered, §2.6; everything else is literal)

| Kind | Keys | `out` |
|---|---|---|
| `agent: <name>` | `task` (template, required); `schema` (JSON schema: parse the answer as JSON and validate); `parse` (a companion function or `module:func`, `fn(text) -> out`; raising `ValueError` sends the message back to the same instance); `parse_retries` (feedback rounds, default 1); `vars` (template map, over the machine's vars); `sam`; `advanced`; `continue` (template: an instance id to follow up instead of spawning) | the answer text, or the parsed value. `activity.instance_id` names the instance for a later `continue`. |
| `tool: <flat tool name>` | `args` (template map); `error_if` (Python expression over `out`) | the tool's result. An error-shaped result (the core predicate `tools/base.py::_error_result_message`) or a true `error_if` raises `tool_failed` with `error.data` = the full result. |
| `decide: noul\|choice\|score` | `question` (template); `criteria` (choice: `{option: meaning}` with ≥2 options; score: `[lowest, …, highest]` with ≥2; noul: optional `{true: …, false: …}`); `input` (template, the content to judge, not empty); `profile` | `{value, confidence, probabilities}`. `value` is a probability (noul), an option (choice) or a scale point (score). `confidence` and `probabilities` may be `null`. |
| `decide: questions` | `questions: {name: {type, question, criteria}}`; `input`; `profile` | `{name: {value, confidence, probabilities}}`, from one call. |
| `call: <companion function>` or `module:func` | `args` (template map) | The return value of `fn(**args)`, or `fn(sg, **args)` if its first parameter is named `sg`, which then receives the read-only scope, `sg.tool()` and `sg.Error`. Sync or async. |
| `machine: <import alias>` | `params` (template map) | The output of the final the submachine ends in. A `failed` final raises `submachine_failed` with `error.data` = that output. |
| `parallel: {branch: <activity>}` | `fail: fast\|collect` | `{branch: out}`. `fast`: the first failure cancels the other branches and is raised (with `error.branch`). `collect`: all branches run to the end, and `out[branch]` = `{status, out}` or `{status, error}`. Nothing is raised. |
| `map: <Python expression>` | `each` (an activity); `as` (item variable, default `item`); `concurrency` (default 1 = strictly in list order); `fail` | A list in item order (`[]` for no items). `item`/`index` are in scope in `each`. |

A kind value (`agent`, `tool`) is a literal, or `{{ params.<name> }}` where that parameter
has an `enum`. The validator then checks every enum value (§4). `machine` is always a
literal alias.

### 2.6 Python and templates

**Code fields** hold plain Python, with no braces. They are `guard` (expression), `effect`,
`entry`, `exit` (statements), `map` and `error_if` (expressions).

**Template fields** are exactly these: `task`, `args`, `params`, `vars`, `question`,
`criteria`, `input`, `continue`, `output`, and a kind value of the form
`{{ params.<name> }}`. A template value is literal unless it contains `{{ }}`.

- A value that is exactly one `{{ expr }}` keeps the expression's type.
- In mixed text, each expression is interpolated: `str`, or JSON for dicts and lists.
- `{{ … }}` ends at the first `}}` that closes a parseable expression, so dict literals work.
- A literal `{{` is written `{{ '{{' }}`.

Everything else (kind keys other than the parameter form, `sam`, `schema`, `retry`,
`timeout`, `as`, `concurrency`, `fail`, `description`, `context` values) is literal.

**Names in scope**

| Name | What | Bound in |
|---|---|---|
| `ctx` | the frame's context: attribute access at the top level (`ctx.draft`), item access everywhere (`ctx["draft"]`, `ctx.critique["notes"]`); the values are plain JSON data | everywhere |
| `params` | bound parameters, read-only, same access rules | everywhere |
| `out` | result of the activity that completed (plain data), or the output of the nested final that completed a composite | completion transitions, `error_if` |
| `activity` | meta of that activity: `instance_id`, `request_id`, `attempts`, `duration_s`, `mocked` (always present, `null` when they do not apply; whether an outcome was replayed is not exposed, because it differs between a run and its replay) | completion and error transitions |
| `error` | `type`, `message`, `state`, `data`, `cause` | error transitions |
| `event` | `name`, `data` | event transitions |
| `run` | `id`, `origin` (the first run of a fork chain; stable across forks), `step` (frame-local), `state`, `visits` (frame-local), `frame` (the frame's key prefix) | everywhere |
| `item`, `index` | the map variables (name set by `as`) | inside `map.each` |
| companion names | every public name of the companion module | everywhere |

A name used where it is not bound (e.g. `out` in an error transition) is SG004.

### 2.7 Reuse and hierarchy

- **Composite states** group states that share transitions and entry/exit behaviour. An
  `error` transition on the composite covers every nested state.
- **Submachines** are the reuse unit. A machine file with `params` and final `output`s is
  imported under an alias and instantiated by `do: {machine: <alias>, params: …}`.
  - Each instance is its own **frame**, with its own `ctx`, step counter and visit counts.
    Data flows in only through `params` and out only through the final `output`, so there
    is no hidden coupling. Parameters are deep-copied at binding (§3.6).
  - A submachine can be used any number of times with different parameters (the v6 ritual
    appears 20 times, one line each), inside `parallel` and `map`, and nested to any depth.
  - Import cycles are refused.
- **No inheritance, no text includes** (D6). A variant is a parameter, or a submachine for
  the part that varies.

---

### 2.8 Multi-call steps, cleanup and external state

**`sg.tool()`.** A `call` function makes tool calls with `await sg.tool(name, args, idempotent=False)`.

- Each call is a child `tool` activity of the call: key `<call key>/t.<n>` (n counts the calls of one
  attempt, in call order), path `<call path>/<tool name>` -- the call activity's path, e.g.
  `review/store_read` (mocks answer by that path; `$visits` answers repeated calls in order). It is
  journaled like a `tool` activity.
- The function must be deterministic apart from these calls (§3.6). On resume it runs again from
  the top: finished calls replay, a changed call diverges.
- `args` is data, never a template. A call in flight at a crash raises `interrupted` from
  `sg.tool()` unless `idempotent=True`; the function may catch it (`except sg.Error as exc:
  exc.type`) and reconcile, e.g. look the object up.
- It serves steps whose data must not pass through `ctx`: read three documents, map them, create
  a row, verify it.

**`finally`.** An activity on a state or on the machine.

```yaml
states:
  drafting:
    finally: {tool: debate_forum_post, args: {text: "drafting ended: {{ ending.reason }}"}}
finally: {call: report_end}
```

- It runs once per exit of its state, whatever the cause: a transition that leaves the state,
  an unhandled error that ends the frame, a terminate, the run's `limits.timeout`, or a parallel
  sibling failing fast. The machine's `finally` runs when a frame of the machine ends.
- Paths (for mocks): a state's is `<state path>/finally`, the machine's `<frame path>/<machine id>.finally`.
- It reads the scope read-only plus `ending`: `reason` (`transition`, `finished`, `failed`,
  `cancelled`), `state`, `error` (the error that caused the exit, or null). Its `out` is
  discarded; it cannot change `ctx`.
- A failing `finally` is journaled and traced (`finally_failed`); the exit goes on.
- It does not run when the process stops (the run is `interrupted` and resumes) or in a process
  that lost the run.
- It calls agents and decisions without the run's cancellation token, so a terminate does not
  refuse them. Under a caller whose own request was cancelled (the agent facade), the platform
  still force-cancels the caller's request tree after its cleanup timeout (10 s by default): keep
  a `finally` short, or make it a tool or a call.

**`resources`.** External state that belongs to one frame of a machine, such as a store
namespace or a forum group per run.

```yaml
resources:
  store:
    open:  {call: namespace_for, args: {origin: "{{ run.origin }}"}}
    fork:  {call: copy_namespace, args: {source: "{{ fork_source }}"}}
    close: {tool: v6_story_json_manage_json, args: {operation: stats, namespace: "{{ resources.store }}"}}
vars:
  json_namespace: "{{ resources.store }}"
```

- `open` runs when the frame starts, before `vars` are rendered, in declaration order. Its `out`
  is `resources.<name>` in every code field and template of the machine (read-only).
- `fork`, when present, runs instead of `open` in the root frame of a forked run, with
  `fork_source` = the source run's value. Without it, a fork opens the resource afresh.
- `close` runs when the frame ends (after the `finally` activities, in reverse order), reads
  `ending`, and follows the rules of `finally`.
- A resume replays the recorded values; nothing is opened twice.
- Hashes are taken with each resource value replaced by a token (§5.4) -- in a fork also the
  source's values, which its replayed outputs still hold -- so a fork whose resources differ from
  its source's still replays its prefix. Replaced are strings of 6+ characters, whole objects and
  lists, and the 6+-character strings inside them; numbers are not (make ids strings, or let the
  fork hook keep the source's value).

## 3. Semantics

### 3.1 The UML subset

- A composite has one region. There are no orthogonal regions, no history, no fork/join
  pseudostates, no deferred-event declarations, and no time events other than wait-state
  timeouts.
- Events never interrupt a running do-activity. This deviates from UML; `terminate` and
  activity timeouts cover that case.
- `initial` targets a direct child (or a choice/junction). A final has only `type`,
  `status`, `output` and `description`.
- Transitions are external: a self-transition exits and re-enters its state. A transition
  from a composite to its own descendant exits and re-enters the composite. There is no
  `kind: local`.

### 3.2 Run-to-completion

A frame processes one **dispatch** at a time. A dispatch is one of: a completed activity,
a failed activity, the completion of a state without `do`, the completion of a composite
(a direct final child entered), a consumed event, a fired wait timeout, and an error
raised by an action or guard. Each dispatch increments the frame's step `N` by one.

1. The debugger's `exit` hook runs (or `error` for an error). `out`/`error` are known; no
   transition has been chosen yet.
2. **Selection.**
   - **Completion is local.** A state's completion transitions are candidates only for that
     state's own completion. A leaf's completion never selects an ancestor's completion
     transition.
   - **Named events and `error` are offered inner-first**: the active leaf first, then
     each ancestor, each in list order.
   - The first transition whose trigger matches and whose guard holds fires. Guards of
     junction branches are evaluated together with it, before anything executes.
3. **Execution.** The transition domain is the innermost state that is a proper ancestor of
   both source and target, or the machine root.
   - Exit actions run from the active leaf up to, but not including, the domain.
   - Then the effect runs, then the entry actions from just below the domain down to the
     target. Entering a composite continues into its `initial`.
   - A **choice** evaluates its guards after the incoming effect, so they see the updated
     `ctx`.
4. The debugger's `enter` hook runs on the new leaf, then its activity starts.

A transition is **atomic**. If an exit action, effect, choice guard or entry action raises,
`ctx`, configuration and visit counts are restored to their values at the start of the
dispatch. The error (`action_failed` or `guard_failed`) is then raised in the source leaf;
`error.state` names the state whose action or guard failed. Actions are pure (§3.6), so the
rollback is exact.

### 3.3 Completion without an enabled transition

If a completion (activity finished, state without `do` completed, composite completed)
finds no enabled transition, `no_transition` is raised in that state. Silent idling is
impossible: a state waits only if it is a wait state (§2.3).

### 3.4 Events

- **Declaration.** Named events must be declared under `events:`. `done` and `error` are
  reserved.
- **Recording.** `send_event(run, name, data, frame=None)` checks `data` against the event's
  declared `data` schema, then writes an inbox row before it returns, so a received event
  survives a restart. The run must be active in the process that receives the call; an
  interrupted run is resumed first.
- **Routing.**
  - Without `frame`, the event goes to the frame whose active configuration accepts it:
    some active state has a transition with that trigger.
  - If several frames accept it, the call is refused: name the frame.
  - If none accepts it, it waits in the run's inbox until some frame's configuration
    accepts it. It is **deferred**, not dropped.
  - A frame whose active states accept the event but that is busy with an activity
    receives it at its next wait state.
- **Delivery.** A frame takes events only at a stable point, i.e. a wait state. It takes
  them in arrival order, and completion is always processed first. An event that the frame
  does not accept stays queued.
- **Frames.** Every frame waits and receives independently. A submachine waiting for
  approval inside a `parallel` branch or a map item works; it is not blocked by its
  parent's running activity.

### 3.5 Errors

`error.type` is one of:

| Group | Types |
|---|---|
| activities | `agent_failed`, `schema_invalid`, `parse_failed`, `tool_failed`, `tool_denied`, `decision_failed`, `call_failed`, `submachine_failed`, `activity_failed` (an unexpected exception in a kind from another plugin), `timeout`, `interrupted`, `template_failed`, `params_invalid`, `unmocked`, `no_backend`, `config` |
| engine | `loop_limit`, `no_transition`, `guard_failed`, `action_failed`, `wait_timeout`, `not_serialisable` |

- **Handling.** An error is dispatched as the `error` event of the state where it was
  raised, and selection walks outward (§3.2). An error raised while an error transition is
  selected or executed ends the frame. It is not handled twice.
- **Unhandled errors** end their frame as failed. For a submachine, the calling state then
  receives `submachine_failed` with `error.cause` = the inner error. Only a failing root
  frame fails the run.
- **Not catchable.** These end the run: `limits.max_steps` exceeded (`step_limit`), the
  run timeout (`timed_out`), terminate (`cancelled`), and a replay divergence
  (`diverged`, §5.4).

### 3.6 Purity and normalisation

All code fields and all templates, and every companion function they call, are **pure
functions** of the scope. That means no I/O, no clock, no randomness, no environment, no
set iteration order and no `hash()`. Reading external state is an activity (`call`/`tool`)
whose result goes into `ctx`. Replay depends on this rule, and the engine checks it (§5.4).
SG106 warns about the obvious violations.

- **Read-only code.** Guards, templates, `map`, `error_if`, breakpoint conditions,
  watchpoints and debugger `evaluate` see `ctx` read-only. The engine hashes the canonical
  JSON of `ctx` before and after, and a change raises `guard_failed` ("ctx mutated").
- **Normalisation.** Every value that crosses a boundary is normalised by a canonical JSON
  round trip, which also deep-copies it. That covers:
  - activity results, before they are bound to `out` and journaled, so the live and the
    replayed `out` are identical;
  - submachine params and map items at binding;
  - final outputs, event data and debugger `set` values.

  Dataclasses and pydantic models are dumped first. `ctx` must stay JSON; after every
  action the engine checks it (`not_serialisable`).

### 3.7 Counters and limits

- `run.step` and `run.visits` are **frame-local**.
- `max_visits` counts entries within one activation of the parent region: entering a
  composite resets the visit counts of all its descendants.
  - On the (n+1)-th entry the state becomes active **without** running its entry action
    or activity, and raises `loop_limit` in itself.
  - Loops over data use `map`, or a guard on a counter in `ctx`.
- `limits.max_steps` applies to each frame of the machine that declares it.

### 3.8 Time

- An activity `timeout` applies per attempt.
- A wait state's `timeout` stores its absolute deadline when the wait starts (once per
  visit: an internal event transition does not restart it); a resume re-arms the
  remaining time.
- `limits.timeout` of the root machine counts only time in status `running`, not
  `waiting`, `paused` or `interrupted`.

### 3.9 Agent template vars

v6 steers its agents through session template vars: prompt branches, store namespaces and
shared prompt blocks. stategraph makes them first-class.

- The effective vars of an agent activity are merged in this order, each layer over the
  one before it: the calling frame's effective vars (submachines inherit them), then the
  machine's own `vars_from` and `vars`, then the activity's own `vars`. A submachine's own
  vars win over its caller's: a reusable ritual sets `phase` per call from its params, and
  the caller's `phase` must not shadow it.
- The engine sets them as template vars on the run's session right before the SAM call.
  A sub-agent created under that session inherits them, and a `continue` sees the refreshed
  values.
- The run's session holds exactly the call's effective vars: they are replaced for every
  agent call, never accumulated, so a later call does not inherit an earlier call's keys.
- They are journaled with the activity.

Two parallel agent activities of one run with **different** vars for the same key race,
because the session is shared. Such branches must not set conflicting keys (SG109).

---

### 3.10 Ending: finally, close, terminate

- **After a transition.** The `finally` of each state a transition left runs once that
  transition has committed and its step is journaled, innermost first, before the target's
  `do`. A transition that fails and rolls back (§3.2) has left nothing, so nothing runs.
- **When a frame ends** (final state, unhandled error, cancel): the pending `finally`
  activities, then the `finally` of every still active state (innermost first), then the
  machine's `finally`, then `close` of each opened resource in reverse order. Each runs once per
  frame.
- **How a frame ends is journaled** (trace `<frame>end`: `reason`, `error`, and whether the run's
  terminate ended it) before these run; their keys carry the reason (§5.2).
- **On cancel** they run although the run is cancelled; one without its own `timeout` gets 60 s
  (`FINALLY_CANCEL_TIMEOUT`), counted from when the run's ending began or from its own start if
  later -- also a `finally` another branch of a resumed run is running when the terminate is
  carried out elsewhere, one a resume runs again before it reaches the point where it carries a
  journaled terminate out, one of a branch a join had cancelled before the crash that replays
  into its end, and one below a frame that a cancel reached while it replayed. Of the frames a non-idempotent composite ends after a crash (§5.5), one that had
  reached an end of its own (finished, failed) completes that ending without the bound, as the
  crashed run would have; the others end as cancelled, within it. A `finally` that is a submachine gets that for each of its own activities
  too, so nesting adds up. One the bound cuts ends `timeout`; that is journaled as its outcome, so a
  resume does not run it again.
- **A terminate that arrives while they run** does not cut the running one: it finishes, and so do
  the rest, each within the 60 s. A root frame that had already finished or failed keeps that
  outcome; a nested frame passes the terminate on to its parent. A `step_limit` abort that came
  first keeps its outcome -- also one in a join's branch while the other branches end: the run ends
  `failed` (`step_limit`), its frames' endings bounded.
  Terminate is idempotent, and `limits.timeout` does not fire once a terminate came: a second
  cancel would not stop anything anyway, since a cancel that reaches a running finally only
  bounds it. A terminate that comes before the run's task ran its first line is carried out by
  the run as it begins: at the first resource the root opens, or else right after the initial
  state was entered, whose `finally` then runs too.
- **A halt stops them**: the process stops (shutdown), or it lost the run. The running one is
  cancelled and waited for -- nothing of it writes after the run's end -- no further one starts,
  and the run is `interrupted`; the resume completes the frame's end.
- **Nothing pauses an ending run**: breakpoints and watchpoints are silent once a terminate or
  timeout reached it, a pause held at that moment -- say in a `finally`'s submachine -- lets go, and
  one that waited for its turn behind it does not pause either. Nor does a frame that only replays
  into its end.
- **No finally runs once a replay diverged**: the run's state is not trusted then, and the other
  branches of a join end without theirs as well. The run ends `diverged` even where something
  catches the divergence -- a call's `except` around `sg.tool()`, a join's cleanup -- and nothing
  runs live after it. A frame whose own code raises an engine error
  ends without its `finally` too (in a submachine the activity then fails with `activity_failed`,
  and the parent goes on).
- **Terminate and timeout are journaled** (trace `cancel`, with `timed_out`); the first one counts.
  A process that stops or crashes while the `finally` activities run leaves the run
  `interrupted`. Its resume replays the journal, and every frame replays to the point it stood at
  when the run was terminated: the terminate is carried out at the first leaf activity or wait
  past the frame's journal, before a step the journal does not have (a frame that stood at an
  exit or error hook ends in the state it stood in, before the transition's actions run), or at a
  frame's end -- never at a composite, which goes on so its children replay into their frames'
  ends, and then ends terminated instead of recording a new outcome. That holds for a composite
  that is not idempotent too: it does not end `interrupted` then. A frame the terminate had ended
  ends that way again; a frame that had finished or failed before it keeps that outcome (the
  terminate came during its `finally` activities), also when another branch carried the
  terminate out first.
- **A cancel that reaches a replay** -- a terminate while a resumed run replays, a fail-fast
  sibling's live failure, an activity's own `timeout` -- takes effect where the frame's journal
  ends: a replay takes no time, so the frame replays on as far as the crashed run had got
  (applying an edit made at the enter hook it paused at) and ends there, not in states that run
  had already left. A branch the crashed run had started that the cancel stopped before it began
  replays into its end afterwards.
- **A fail-fast join's cancelled branches**: after a crash the join replays its recorded failure
  without running anything live, but first replays each branch the crashed run had started into
  its frame's end -- also one that took the join's cancel inside a transition's `finally`, before
  its end was journaled -- so its `finally` and `close` complete (the branch goes no further than it
  had). A branch that only replays into its end records no outcome of its own, not even
  `interrupted`; its frames' `finally` and `close` activities run live.
- **A local cancel is not journaled** -- a fail-fast join's cancel of the other branches, an
  activity's own `timeout`. A frame such a cancel ended decides its end anew when a resume reaches
  it (outside the join case above): an activity timeout restarts on resume, so the frame may end
  another way, and then runs that ending's `finally` activities under their own keys.
- **Terminate of a run no process runs** (`interrupted`) resumes it into its termination the
  same way; `control_run` waits up to 10 s for the run to end before it answers. A timeout
  journaled before the crash still counts: the run ends `failed` (`timed_out`).

## 4. Validation

`stategraph_validate_machine`, the panel on every edit, and every save and run run the same
checks. The companion module is **not executed** for validation. Its public names come
from an AST scan.

| Code | Level | Check |
|---|---|---|
| SG001 | error | YAML syntax, duplicate keys, a non-string mapping key (an unquoted `{{ … }}`, or a `true`/`yes` key read as a boolean), schema (unknown key, wrong type, a param default that does not fit its type or enum), a machine whose file is not `<id>.yaml` |
| SG002 | error | Unknown or duplicate state name; unknown target or `initial`; an undeclared event trigger |
| SG003 | error | Pseudostate and structure rules: a choice without `else`; `else` not last; triggers on choice/junction; a final with anything but type/status/output/description; `status` on a nested final; a composite with `do` or without `initial`; an internal completion or error transition; a wait state that accepts no event; a cycle of pseudostates only |
| SG004 | error | Python does not compile; unknown name; a name that is not bound at that place; `{{ }}` in a code field; `params.<name>` that is not declared; access that always fails on plain data (`out.value` instead of `out["value"]`, `ctx.a.b` instead of `ctx.a["b"]`, dict methods on a namespace such as `ctx.get(...)`); a companion function that does not exist; `python:`/`imports:` outside the machine roots |
| SG005 | error | Activity: unknown kind, several kind keys, invalid fields (including decide criteria shapes, per-question keys, a map `as` that shadows a scope name), a computed `agent:`/`tool:` value other than `{{ params.<name> }}` with an enum |
| SG006 | error | Submachine: unknown alias, import cycle, missing required or unknown parameter |
| SG007 | error | Configuration: the agent cannot be spawned by the SAM, the named SAM is not a `sub_agent_manager`, the runner may not call the SAM's `<sam>_manage_sub_agent` (agent activities spawn through it), the tool is not in the runner's allowlist, the tool is a SAM's (agents start through agent activities), the tool belongs to stategraph itself, an unknown decision profile, a `vars_from` agent that is not configured; enum values of a parametrised agent/tool are checked one by one (the tool check runs again at run time) |
| SG101 | warning | A state is unreachable from `initial` |
| SG102 | warning | No path leads from a state to a root final |
| SG103 | warning | A loop without `max_visits` on any of its states |
| SG104 | warning | A transition after an unguarded one of the same trigger never fires |
| SG105 | warning | `ctx.<name>` is read but neither declared in `context` nor assigned |
| SG106 | warning | Impure code in a code field or template (`random`, `time`, `datetime.now`, `uuid`, `os.environ`, `open`, `set(...)`, `hash`) |
| SG107 | warning | A data field whose whole value looks like a reference (`ctx.x`, `out[...]`) without braces: did you mean `{{ ctx.x }}`? |
| SG108 | warning | A root machine with `limits.timeout` used as a submachine (ignored there) |
| SG109 | warning | Concurrent agent activities with `vars`: parallel branches (submachines included) or a `map` with `concurrency` > 1 -- they share the run session |
| SG110 | warning | `retry.errors` names an error type the engine does not raise, or `interrupted` (never retried) |

Every problem carries a path (`states.judge.transitions[1].guard`), the file, and the
line when it is known.

---

## 5. Execution and the journal

### 5.1 Engine structure

```
model/     spec.py (pydantic format)  code.py (Python, templates, scopes)  loader.py  validate.py
           yamledit.py (comment-preserving edits)  graph.py (editor view)
kinds/     base.py (registry, KindSpec, ActivityError)  builtin.py (the seven kinds)
engine/    machine.py (compiled tree, LCA)  interpreter.py (frame, RTC)  activity.py (mocks,
           replay, retries, journal)  runner.py (RunContext, RunManager)  journal.py (SQLite)
           debugger.py  backend.py (ScarabHive + config checks)
store.py   machine roots, versions   server.py  web_endpoints.py
```

### 5.2 The journal

`data/stategraph/runs.db` holds:

- `runs`: id, machine id, status, definition snapshot, params, mocks, debug state,
  `user_id`, `session_id` (`sg_<run id>`, the parent of the run's sub-agents), `owner`,
  `lease_until`, `run_key`, `parent_run`, `fork_step`, `journal_format` (1), output,
  error, and the display view.
- `journal`: rows `(run_id, seq, kind, key, state, status, data)`, unique on
  `(run_id, kind, key)`. `seq` is append order, for display only.

**Key grammar**

```
key   := frame "s" N
frame := "" | frame "s" N "/" ("m" | "b." NAME | "i." INDEX) "/"
```

- `N` is the frame's step when the activity starts.
- A child of a composite activity extends its parent's key: `s3/b.style`, `s3/i.2`; in
  attempt n > 1 of the parent: `s3/a2/b.style`.
- A submachine frame's prefix is `<activity key>/m/`.
- Example: map item 2 of step 3 runs a submachine whose fourth step is keyed
  `s3/i.2/m/s4`.
- The n-th `sg.tool()` call of a call activity: `<call key>/t.<n>` (attempt-scoped like other children).
- The `finally` of a state a transition left: `<frame>s<N>.fin.<state>`, with N the step of that
  transition -- beside the step's activity `s<N>`, not below it (a key below it would count as
  one of its children, §5.5). A state left again within the same step gets `.2`, `.3`, ...
- A frame's end: trace `<frame>end`; its activities `<frame>end.<reason>.<state>` (the `finally`
  of a still active state), `<frame>end.<reason>.finally` (the machine's), and
  `<frame>end.<reason>.close.<resource>`. The reason in the key keeps a frame that a resume ends
  another way (§3.10) off the other ending's rows.
- Resources: `<frame>r.<name>` (open or fork). A frame's resource and end keys have no step of
  their own (a submachine frame below them counts its steps); the root frame's are never copied
  into a fork.

**Row kinds**

| Kind | Key | Content |
|---|---|---|
| `activity` | activity key | `status` started → done / error. `data`: kind, state path, `input_hash`, attempt, `out` or `error`, meta (instance id, vars, cost, mocked). A composite activity (`parallel`, `map`, `machine`) records its aggregate outcome under its own key; its children have their own rows. |
| `event` | `pending:<id>`, then `<frame>s<N>:event` once consumed | name and data (the inbox) |
| `edit` | `<frame>s<N>:<hook>:<n>` | a debugger `set`: path and the evaluated JSON value |
| `timer` | `<frame>s<N>:timer` | a fired wait timeout |
| `trace` | `<frame>s<N>:<what>:<state>` | enter/exit/transition/final records, the `ctx_hash` after each dispatch, the wait deadline, `finally_failed`; unkeyed by step: `cancel` (a journaled terminate) and `resource_sources` (a fork's source values) |

### 5.3 Resume

A resume re-runs the interpreter from the start against the run's own definition snapshot.

- **Activities.** Every activity renders its inputs and looks up its key first. A recorded
  outcome is returned without executing. Only activities without an outcome run.
- **Events, edits and timers** are re-applied at the same frame and step.
- **Side channels are silent** until the replay reaches the last recorded step: no debugger
  hooks, no watch comparisons (watches are primed with their value at the frontier), and
  no status events.

### 5.4 Divergence

A replay lookup succeeds only if key, state path, kind and `input_hash` match. After every
replayed dispatch, the step hash (over `ctx` and the names of the active states) must equal
the recorded one. Both hashes are taken with every resource value of the frame and its parents
replaced by a token (`⟨resources.store⟩`), so a fork with its own resources replays its source's
prefix; the inputs themselves keep the real values. Both hashes must match, so a changed guard that leads into another state diverges at once. Any mismatch stops the run
with status `failed`, error type `diverged`, and names the first diverging key. A diverged
run never continues on a different path.

### 5.5 In-flight activities and idempotency

An activity row is written with status `started` before the first attempt; its outcome
updates the row. On resume, a `started` row without an outcome means the activity was in
flight at the crash.

- An **idempotent** activity runs again. It continues the attempt it was in: a crash is not a
  failed attempt, so the retry budget is unchanged, and a composite's children that finished
  in that attempt replay under that attempt's keys (children of attempt n > 1 are keyed
  `<key>/a<n>/…`, so a retry runs its children again instead of replaying the failed ones).
- A **non-idempotent** activity is not run again. It raises `interrupted` in its state,
  with `error.data` = the rendered inputs. An error transition, or an operator in the
  debugger, decides what happens next. A composite first replays the frames below it into
  their ends -- never live -- so their `finally` and `close` activities run. No retry runs it
  again either: a retry of an enclosing composite would start it under the next attempt's
  keys, so a composite inside which an activity raised `interrupted` is never retried --
  handled or not, and whatever `retry.errors` says. An interrupted `finally` or `close` does not
  count: it belongs to that attempt's frame, and the retry's frame has its own ending. For writer
  tools that create things (a story row), the error path reconciles, e.g. by looking the object
  up by `run.id`.

### 5.6 Fork

A fork starts a new run from **top-level step N** of an existing run.

- **Journal.** It copies every journal row whose top-level step is below N; rows nested
  under those steps come with them.
- **Definition.** `definition: snapshot` (the default) reuses the source run's definition.
  `definition: current` loads the current files: the "fix the guard and fork" case. A
  divergence in the replayed prefix aborts the fork and names the key.
- **External state is not forked** unless a resource says how: its `fork` hook runs in the
  fork's root frame with `fork_source` = the source's value (§2.8), e.g. to copy a store
  namespace. It sees the source's external state as the source left it, not as it was at step N.
  Everything else -- database rows, SAM conversations -- keeps what the source did after step N.
  - The fork gets its own session `sg_<fork id>`. A `continue` into an instance created
    before the fork point therefore fails at the SAM.
  - `run.id` differs in a fork, so a replayed step whose inputs or effects contain it
    diverges. External things are named with `run.origin` (the first run of the fork
    chain): replayed steps match, and the fork finds what the prefix created. Isolating a
    fork's external state from its source needs the `resources:` hook (§10).

### 5.7 Process model

A run executes as an asyncio task in the process that started it (usually the API).

- **Lease.** The owning process holds a lease on the run row (`owner`, `lease_until`) and
  renews it every 20 s.
- **Interrupted runs.** `start_plugin` runs in every process. Its sweep marks a run
  `interrupted` only after the lease has expired, never merely because some process
  started. The sweep checks the lease again when it writes, so a run renewed between the
  sweep's read and its write stays untouched.
- **Resume** takes the lease with one conditional update (only an expired lease, or our
  own), so two processes can never run the same run. It reads the run row again once it
  holds the lease: breakpoints stored meanwhile apply, and a run that succeeded or was
  cancelled meanwhile is not started again.
- **Fencing.** Every write of an owner -- the run row, every journal row, the heartbeat --
  is conditional on still being the owner (checked and written in one transaction). A
  process whose write is refused has lost the run: it stops its local copy without
  writing anything more. A heartbeat that finds its live run swept to `interrupted`
  restores the status together with the lease.
- **Controls follow the lease.** Debugger commands, `set`, `evaluate` and events act in
  the owning process only; a process that has lost the run refuses them ("another process
  owns run ... now"), and a debugger edit it could not journal leaves its context
  unchanged. Breakpoints of a run that no process holds a live lease on are stored in its
  row and apply when it is resumed; while another process holds it they are refused with
  that owner's name.
- **`run_key`.** A caller may pass a `run_key` (e.g. its request id). A second
  `run_machine` with the same key attaches to the unfinished run, or resumes it, instead
  of starting a duplicate.

### 5.8 ScarabHive integration

| Need | Primitive |
|---|---|
| agent call | One `AgentCaller(runner, sam_instance, session_id=sg_<run>, user_id, request_id=<run>_NNN, cancellation_token, retries=0)` per attempt. `call_text` creates; `follow_up_text` continues and gives feedback. The caller's `last_instance_id` belongs to this one attempt. |
| tool call | `runner.dispatch_tool_call(tool, args, …)`. Configured `inject_params` (fnmatch pattern → params, as in tool_script) are applied after rendering, so secrets never live in machine files. A `ToolDispatchError` becomes `tool_denied`. |
| decision | `create_decisions_from_profile(system_config, profile).decide(input, questions)` |
| vars | `runner._session_tracker.set_session_template_vars(sg_<run>, vars)` before the SAM call |
| cancellation | `get_cancellation_manager()`: the run's backend holds a token under the run id. Terminate calls `cancel_sub_requests(<run id>)`, which reaches every `<run>_NNN` sub-run in flight -- not the run's own token: a cancelled token has the platform force-cancel its whole request tree after the cleanup timeout, and the `finally` activities start sub-runs after the terminate. They call agents and decisions without the token (§2.8). A cancel from outside that reaches the run's token -- a caller whose request id prefixes the run id, e.g. a book cancel above the agent facade -- terminates the run the same way, so its `finally` activities run. An activity that ends because the run was cancelled is not journaled as an error, and no error transition fires. |
| status | The run task sets `current_request_id` to the run id. The tool that started the run ends with one end line naming the result. |

### 5.9 Run status

A run is `paused` while the debugger holds it, `running` while any leaf activity (agent,
tool, decide, call) executes, `waiting` when no activity executes and at least one frame
waits for an event, and `running` otherwise. `limits.timeout` counts only `running` time,
across resumes. The terminal statuses are `succeeded`, `failed` and `cancelled`;
`interrupted` means the owner stopped and the run can be resumed.

---

## 6. Debugger

| Primitive | Meaning |
|---|---|
| breakpoint | `{state, at: enter\|exit\|error, machine?, condition?}`. `enter`: the state is entered and its activity has not started; `ctx` can be inspected and edited. `exit`: the activity finished and `out` is known, no transition chosen yet. `error`: an error is about to be dispatched. `condition` is a read-only Python expression over the scope. |
| watchpoint | A read-only Python expression (`ctx.round`, `len(ctx.issues)`). After every dispatch its value is compared with the previous one in the same frame, and a change pauses the run. An optional `condition` sees `old` and `new`. |
| pause / continue | Cooperative: pause takes effect at the next hook. A running agent call is never frozen. |
| step | Continue until the next hook. |
| run_to | A temporary breakpoint on a state's `enter`. |
| terminate | Cancel the run (§5.8). |
| evaluate | A read-only Python expression against the paused scope. |
| set | `ctx.<path> = <expr>` while paused. The evaluated JSON value is journaled as an `edit`; several edits at one hook replay in `seq` order. |
| fork | New run from a top-level step (§5.6). |

- **Storage.** Breakpoints and watchpoints belong to the run and are stored with it.
- **Watchpoints** compare values between dispatches: the first value is taken after the
  first dispatch of a frame, so the initial context never counts as a change.
- **After a resume** a breakpoint at the last recorded step may pause again: that step is
  where the run was when it stopped.
- **Panel.** The panel polls `GET /runs/{id}` and sends commands with
  `POST /runs/{id}/control`.
- **Scope.** Only runs of the API process can be paused from the panel. Runs of other
  processes are visible through the journal.

---

## 7. Plugin layout

### 7.1 Files

```
src/plugins/stategraph/
  plugin.toml  plugin.py  server.py  schema.yaml  web_endpoints.py  store.py  README.md
  model/  kinds/  engine/                         (§5.1)
  machines/        shipped example machines (found by the root glob)
  agents/          stategraph.yaml (tool instance, SAM, runner), stategraph_author.yaml, prompts/
  skills/stategraph-authoring/   SKILL.md + references/ (format, patterns, debugging)
  docs/            format.md (authors' reference), extending.md (new kinds)
  static/          panel.js, graph.js, panel.css, vendor/elkjs/
  templates/       panel.html
  schemas/         machine.schema.json (generated from model/spec.py)
  tests/
```

### 7.2 Machine roots

- `data/stategraph/machines` is writable and not versioned.
- `src/plugins*/*/machines` is versioned; writer machines live next to their plugin.
- The first root that has an id wins, and writable roots come first.

---

## 8. Tools, endpoints, security

### 8.1 Tools (`stategraph_*`)

| Tool | Parameters | Result |
|---|---|---|
| `catalog` | `sam?` | Activity kinds with their fields; the agents the SAM may spawn; the tools the runner may call; decision profiles; example machine ids |
| `list_machines` | | id, title, file, writable, and whether it validates |
| `get_machine` | `machine_id` | the tree: `files {relative path: text}`, `versions {path: sha}`, problems |
| `validate_machine` | `files` (or `yaml`), `machine_id?` | problems |
| `save_machine` | `files`, `expected_versions?` | versions; refused with errors or on a version conflict |
| `run_machine` | `machine_id`, `params`, `mocks` (`{state path: out}`, `{"$visits": [...]}`, `{"$error": {...}}`), `mock_only`, `breakpoints`, `watchpoints`, `run_key`, `wait: finish\|background`, `max_wait` | `{run_id, status, state, output, error, accepts}`. `wait: finish` also returns when the run pauses or waits for an event. |
| `get_run` | `run_id`, `steps?` | status, frames, context, recent journal rows |
| `control_run` | `run_id`, `action` (pause, continue, step, run_to, terminate, resume, fork, set_breakpoints, set_watchpoints, evaluate, set), + action args | run state |
| `send_event` | `run_id`, `name`, `data?`, `frame?` | accepted, or why not |

Slash command: `/stategraph-run <id>`, with `wait: background`.

### 8.2 REST (`/plugins/stategraph/…`, JSON)

- `GET /` is the panel.
- **Machines.** `GET /api/machines`; `GET|PUT /api/machines/{id}` (tree and versions,
  409 on conflict); `POST /api/machines` (new from template); `POST /api/validate`;
  `POST /api/machines/{id}/edit` (graph operations); `PUT /api/machines/{id}/layout`.
- **Palette.** `GET /api/kinds`.
- **Runs.** `GET|POST /api/runs`; `GET /api/runs/{id}`; `GET /api/runs/{id}/journal`;
  `POST /api/runs/{id}/control`; `POST /api/runs/{id}/events`.

### 8.3 Security

Machines contain Python and run agents and tools.

- **Routes.** `/plugins/stategraph/*` is admin-only (`auth.plugin_security`).
- **Tools.** Every tool that validates, saves, runs, controls or sends events checks
  `params['_user_id']` in its handler: the user must be an active admin, or listed in the
  instance's `allowed_users`. `catalog`, `list_machines`, `get_machine` and `get_run` are
  read-only.
- **Recursion.** The runner's allowlist must never contain stategraph's own tools (SG007
  refuses them), so a machine cannot rewrite or start machines.
- **SAM.** `stategraph_sam.allowed_agents` is an explicit list, never `*`. The runner calls the SAM's
  tool for every agent activity (AgentCaller runs as the runner), so its allowlist names each SAM
  machines use (`stategraph_sam/*`, `v6_story_sam/*`); SG007 checks it. A `tool` activity or
  `sg.tool()` may not call a SAM's tools (SG007, and the same check at run time): sub-agents made
  that way would not be journaled, would outlive the run, and could cancel or delete the ones its
  agent activities made.

---

## 9. Authoring agent and skill

`stategraph_author` is a multi-turn agent with the skill `stategraph-authoring`. Its loop:

1. `stategraph_catalog`.
2. Write the machine tree (YAML plus companion Python).
3. `validate_machine` until clean.
4. `save_machine`.
5. A mocked `run_machine` (`mock_only: true`, a mock for every agent, tool and decision
   state) that must reach a final state.
6. Hand over.

The skill teaches the format, the Python and purity rules, reuse by submachines, and
patterns: review loop, retry with feedback, fan-out/join, human approval, and the v6
ritual.

---

## 10. Mapping writer v6

The coordinator ritual becomes one submachine `v6_ritual(phase, aufgabe, target_doc)`:

1. `vars: {phase, aufgabe}` replaces `set_context`.
2. The panel (agent `v6_story_panel`) runs with `parse: parse_marker`. A missing marker
   line goes back to the same instance as feedback.
3. `merge_doc` (tool). On `tool_failed` the machine returns to the panel with the store
   error as feedback, bounded by `max_visits`.
4. `delete_keys` (tool, guarded).
5. Checkpoint (tool).

The top level is the S0–S24 list of the research, with:

- `check_brief` as a choice, then forum bootstrap and corpus hints;
- the synopsis steps as ritual instances;
- the judge (agent plus a guard on the `STRUKTUR-FIXES` block), then the audits;
- the milestone loop as `map` with `concurrency: 1` over the milestones;
- the drift loop, at most 2 rounds;
- the chapter plan with a deterministic check (call);
- the story row: created as `idea` (a call whose tool call is `idempotent`: a second `idea` row
  after a crash is harmless), then the issue events, and only in the last step set to `developed`
  (`hand_over`) -- the status v4 and the job chain pick a story up by.

Genre and audience tags become one `decide: questions` with a noul per tag. The machine
returns `story_id` as typed output. `write_key` comes from `inject_params`. The panel
moderator stays an agent until a machine version of it has been measured against it.

v4 follows as a strangler. First the phases become `call` activities that wrap the
existing mixin methods, and the entry router becomes a choice. Then the smaller loops (3a/3b,
repair) become composites. `_score_chapter_impl` stays one call until its exits are
covered by tests.

**The v6 migration added four constructs** (built 2026-09-26, with the machine that needed them):

1. **The agent facade** -- `type: stategraph_machine` (`src/plugins/stategraph_machine`, class
   `plugins.stategraph.facade.MachineAgent`). A server entry names one machine
   (`machine: v6_story`); SAM spawns, AgentCaller and writer_jobs' `/events` address it like any
   agent. It runs the machine through the `stategraph` instance's RunManager (the panel sees and
   controls these runs) and answers with the output as a JSON object; `promote` copies output keys
   onto the final event.
   - Run key = `<agent>:<request id>`, bound to the user who started the run; run id =
     `<request id>_sg<n>`: a re-dispatch attaches, resumes, or answers a succeeded run again; cancel,
     status lines and cost attribution stay under the caller.
   - Which run a session belongs to is kept in runs.db (table `callers`), so a `continue` in any
     process finds it. As another agent's tool, every call is a request of its own.
   - A cancel of the request (or any request above it) terminates the run -- its `finally` runs; a
     bare task cancel (the process stops, the client leaves) leaves it `interrupted`.
   - A `continue` answers the output again, resumes an interrupted run, and never starts a second one.
   - The same request id again from a new session attaches, resumes, or answers a succeeded run
     again -- but after a `failed` or `cancelled` run it starts a new one. Whether a re-dispatch
     should get the failure again instead is open (a retry wants a new run, a duplicate delivery
     the old answer).
   - Failures are `error` events, never a `final` (writer_jobs counts any `final` as success).
2. **`finally:`** (§2.8, §3.10).
3. **`resources:`** (§2.8): `open`/`fork`/`close`; hashes take resource values as tokens (§5.4).
4. **`sg.tool()`** (§2.8): journaled inner tool calls of a `call` activity.

Two smaller extensions came with them: `vars` may be one template that renders to an object (the
machine passes its whole var set per call), and a cancel that reaches the run's token terminates the
run (§5.8).

**The machine.** `src/plugins_writer/writer_pipeline_v6/machines/v6_story.yaml` is S0-S24 with the
ritual as the submachine `v6_ritual.yaml`; its companion modules hold the parsing, the checks and the
DB transfer. Its facade entry is `v6_story_machine`. Where it deliberately differs from the
coordinator's prompt, and what the mapping found wrong in today's v6, is in
`writer_pipeline_v6/docs/v6_machine.md`. v4 and writer_jobs still call the coordinator; switching
is configuration only (`story_designer_agent` plus `v4_sam.allowed_agents`, and
`WRITER_STORY_DESIGN_DISPATCH_AGENT_NAME`).

---

## 11. Known limits of the prototype

- **Cross-process debugging.** Runs of other processes cannot be paused from the panel.
  Commands would go through the run row, polled at hooks. That is the next step once runs
  execute in writer_jobs.
- **Browser tests.** This Mac has no Chromium, so the panel is checked for syntax and logic
  with JavaScriptCore (`jsc`), not in a browser. The first real browser session is a manual
  check in both themes.
- **The v6 machine has not run live yet.** It is tested against a simulated v6 world (store
  semantics, forum, story row, agents as fakes, a crash and resume in the beats); its first run
  with real panels is a manual step, best with a breakpoint after the worlds.
- **Cost.** AgentCaller returns no usage. `run.cost` and `limits.max_cost` need the usage
  tracker's per-request-id sums; only `decide` reports cost today.
