---
name: stategraph-authoring
description: How to write a stategraph state machine in YAML -- states, transitions with Python guards and effects, the activity kinds (agent, tool, decide, call, machine, parallel, map), templates, events and wait states, errors and limits, reuse by submachines -- and how to prove it with a mocked test run. Load before writing or changing a machine.
metadata:
  version: '0.1.0'
---

# stategraph machines

A machine is one YAML file `<id>.yaml` (format `stategraph: 1`), optionally with a
companion Python module. States run activities; transitions carry Python guards and
effects; a deterministic engine runs it, journals every step and resumes without
repeating finished work. Unknown keys are errors, so a typo never silently drops
behaviour.

The full reference is `references/format.md`. Read it before your first machine:
`skills_read(name="stategraph-authoring", path="references/format.md")`.
`references/patterns.md` has ready patterns (review loop, retry with feedback,
fan-out, map, human approval, submachines, shared error handling, the v6 ritual);
`references/debugging.md` covers mocks, breakpoints, resume and fork.

## The loop

1. `stategraph_catalog` -- the activity kinds and their fields, the agents a machine may
   run (`agents: "v6_*"` narrows the list), the tools the runner may call, the decision
   profiles. Use nothing else.
2. Write the tree: `<id>.yaml`, its companion `.py` if it has one, any imported machine.
3. `stategraph_validate_machine(files={...})` until it reports no error. Fix warnings
   or say why they stay.
4. `stategraph_save_machine(files={...})` (pass `expected_versions` when you changed a
   machine you read with `stategraph_get_machine`).
5. `stategraph_run_machine(machine_id, params, mocks, mock_only=true)`: a mock for every
   agent, tool and decide state; `$visits` for states a loop enters again. It must
   reach a final state. A run in status `waiting` lists the events it accepts: send
   them with `stategraph_send_event`. Read failures with `stategraph_get_run`.
6. Hand over: machine id, files, validation result, test run id and the final state.

## A complete machine

```yaml
stategraph: 1
id: summarise_url
title: Summarise a page
params:
  url: {type: string, required: true}
context:
  summary: null
  problem: null
initial: summarise
states:
  summarise:
    max_visits: 2
    do:
      agent: research_worker
      task: "Read {{ params.url }} and summarise it in five sentences."
      retry: {attempts: 2, backoff: 10s}
    transitions:
      - target: check
        effect: ctx.summary = out
      - trigger: error
        target: failed
        effect: ctx.problem = error.message
  check:
    do:
      decide: noul
      question: Is this a faithful, complete summary of a web page?
      input: "{{ ctx.summary }}"
    transitions:
      - target: done
        guard: out["value"] >= 0.6
      - target: summarise
        guard: else
      - trigger: error
        target: failed
        effect: ctx.problem = error.message
  done:
    type: final
    output: {summary: "{{ ctx.summary }}"}
  failed:
    type: final
    status: failed
    output: {problem: "{{ ctx.problem }}"}
```

Mocked test: `mocks: {"summarise": "A summary.", "check": {"value": 0.9, "confidence": null, "probabilities": null}}`.
`summarise` is entered at most twice (`max_visits: 2`), so this loop always ends: the
third entry raises `loop_limit`, and the error transition leads to `failed`.

## Rules that decide whether it works

- **Names** (ids, states, params, context keys, events, branches, aliases):
  `[a-z][a-z0-9_]*`. State names are unique in the whole file, nesting included.
- **Code fields are plain Python, no braces:** `guard` (expression or `else`),
  `effect`, `entry`, `exit` (statements), `map`, `error_if` (expressions). Code that
  contains `: ` or ` #` (a dict literal, a string with a colon) goes into a block
  scalar (`effect: |`), or YAML reads it as structure.
- **Template fields** (`task`, `args`, `params`, `vars`, `question`, `criteria`,
  `input`, `continue`, `output`) are literal unless they contain `{{ expr }}`. A value
  that is exactly `"{{ expr }}"` keeps its type (list, dict, number); in mixed text
  each expression is inserted as text (JSON for dicts and lists). Quote a value that
  starts with `{{`. Everything else is literal.
- **Access:** `ctx.draft`, `params.url`, `error.type`, `event.data`, `run.id`,
  `activity.instance_id` -- attribute access at the top level only; below that, item
  access: `ctx.critique["notes"]`, `out["value"]`, `event.data["reason"]`.
  `out` is plain data: `out["x"]`, never `out.x`.
- **What is bound where:**

  | place | names |
  |---|---|
  | everywhere | `ctx`, `params`, `run`, companion functions |
  | completion transitions (`guard`, `effect`) | + `out`, `activity` |
  | `error_if` | + `out` |
  | error transitions | + `error`, `activity` |
  | event transitions | + `event` |
  | inside `map.each` | + the item variable (`as`, default `item`), `index` |

  `entry`, `exit`, activity templates, `map` and a final's `output` see only the
  first row: store what you need in `ctx` with an effect first.
- **ctx is written only in `effect`, `entry` and `exit`.** Guards, templates, `map` and
  `error_if` are read-only; a change there raises `guard_failed`. ctx holds JSON only.
- **Pure code:** no I/O, clock, randomness, environment, `set` (also no set literal:
  write `x in ("a", "b")`) and no `hash()` in code fields, templates and the companion
  functions they call. Reading the outside world is an activity (`tool`, `call`).
- **Every state either completes or waits.** A state with `do` completes when the
  activity ends; one without `do` but with a completion transition completes at once.
  A completion must find an enabled transition, else `no_transition` is raised: end a
  guarded list with `guard: else`. A **wait state** (no `do`, no completion transition)
  takes only declared `events:` and needs an `error` transition for its `timeout`.
- **Every loop is bounded:** `max_visits` on a state of the loop (the (n+1)-th entry
  raises `loop_limit` in that state) or a guard on a counter in `ctx`.
- **Handle errors where they happen:** `- trigger: error` (optionally
  `guard: error.type == "tool_failed"`), on the state or on an enclosing composite.
  An unhandled error fails the machine.
- **Only what the catalog lists:** agents, tools (flat names with
  their instance prefix, e.g. `stategraph_json_manage_json`) the runner may call,
  decision profiles. The validator refuses anything else (SG007).
- **Finals** have only `type`, `status` (`succeeded`/`failed`, root region only),
  `output` and `description`.

## Activity kinds (`do:` has exactly one kind key)

| kind | keys | `out` |
|---|---|---|
| `agent: <name>` | `task`; `schema`, `parse`, `parse_retries`, `vars`, `advanced`, `continue` | answer text, or the parsed value |
| `tool: <flat tool name>` | `args`, `error_if` | the tool's result; an error result raises `tool_failed` |
| `decide: noul\|choice\|score` | `question`, `input`, `criteria`, `profile`, or `by: <agent>` (an agent decides) | `{value, confidence, probabilities}` |
| `decide: questions` | `questions: {name: {type, question, criteria}}`, `input` | `{name: {value, confidence, probabilities}}` |
| `call: <companion function>` | `args` | the return value (a sync function runs in a worker thread; `sg.tool()` needs `async def`) |
| `machine: <import alias>` | `params` | the submachine's final output |
| `parallel: {branch: activity}` | `fail: fast\|collect` | `{branch: out}` |
| `map: <Python expression>` | `each`, `as`, `concurrency`, `fail` | a list in item order |

Common keys: `retry: {attempts, backoff, errors}`, `timeout` (per attempt), `idempotent`,
`description`. Durations: `500ms`, `30s`, `10m`, `2h` or seconds.

A state or the machine may have `finally: <activity>` (runs once on every exit, reads
`ending`); a machine may declare `resources` (external state per frame, e.g. a store
namespace, as `resources.<name>`); an async `call` function may make journaled tool calls
with `await sg.tool(name, args)`. Details: `references/format.md` §14.

## Mocks for the test run

- Keys are state paths: `write`; a submachine's state `review/critique`; a parallel
  branch `review/style`; a map item `summarise/0`. A mock on the state that runs a
  `machine`, `parallel` or `map` answers the whole activity.
- The value is `out` as the machine sees it (after `schema`/`parse`), e.g. a decide
  mock is `{"value": 0.8, "confidence": null, "probabilities": null}`.
- `{"$visits": [first, second, ...]}` answers per use of that path in the run (the last repeats),
  also across repeated calls of a submachine;
  `{"$error": {"type": "tool_failed", "message": "..."}}` fails the activity.
- `call` activities run for real even in a mock-only run; mock them if they reach
  outside.

## Validation codes

| code | means | usual fix |
|---|---|---|
| SG001 | YAML or schema: unknown key, wrong type, file not named `<id>.yaml`, a param default that does not fit, a param default or enum that is no JSON data (an unquoted date), `stategraph:` not the integer 1, an invalid JSON schema in `events.<x>.data` | check the key against `references/format.md` |
| SG002 | unknown state, target, `initial` or undeclared event | declare it; fix the name |
| SG003 | structure: choice without `else`, `else` not last, bad final, wait state without events | see the rule it names |
| SG004 | Python: syntax, unknown name, a name not bound there, braces in a code field, undeclared `params.x`, `out.x` / `ctx.a.b` / `ctx.get()` on plain data, assigning to `params`/`run`/`error`/`event`/… (only `ctx` is writable) | move data into `ctx` first; drop the braces; use `out["x"]` |
| SG005 | activity: unknown kind, several kind keys, bad fields, an invalid JSON `schema`, a computed `agent:`/`tool:`/`by:` | one kind key; the kind's fields only; literal names, or `"{{ params.x }}"` of a param with an `enum` |
| SG006 | submachine: unknown alias, cycle, missing/unknown parameter | `imports:`, the callee's `params` |
| SG007 | configuration: agent not configured or one that reaches machines, tool not callable, stategraph's own tool, a SAM's tool | pick from `stategraph_catalog`; agents through agent activities; another machine as a submachine |
| SG101-SG110 | warnings: unreachable, no path to a final, unbounded loop, dead transition, undeclared ctx field, impure code, forgotten braces, ignored timeout, no completion transition where a state completes, `retry.errors` type unknown or `interrupted` | fix, or explain in the handover |
