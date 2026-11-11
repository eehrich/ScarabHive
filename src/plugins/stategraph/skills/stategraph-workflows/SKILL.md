---
name: stategraph-workflows
description: How to plan a task as a dynamic workflow -- a small stategraph machine written for this task only, that fans work out to many agents and verifies it, while the engine (not you) drives every step. When it is worth it, the subset of the format a workflow needs, the patterns (pipeline, fan-out, adversarial verify, loop until nothing new), limits and cost. Load before writing a workflow.
metadata:
  version: '0.1.0'
---

# Dynamic workflows

A workflow is a plan you write once, as a stategraph machine, for the task in front of you. The engine
then runs it: it starts the agents, waits for them, collects their answers and follows your transitions.
You read only the final output. Intermediate results never enter your context, and no step needs a
turn of yours.

## When

- The task splits into many **independent** pieces (files, modules, findings, variants, directions of
  a review) that agents can work on at once, and/or every result must be **checked by others** before
  it counts.
- **Not** for a handful of tool calls, a lookup, or work whose next step depends on judging the last
  one in ways you cannot write down as a guard. Do that yourself.
- A workflow can start dozens of agents. Write one when the user asked for it or the task clearly
  needs the breadth, and say what its cap on output tokens is.

**Scout first, then fan out.** List the files, read the diff, find the modules yourself; then write
the workflow over the list you found and pass it in as `params`.

## The loop

1. `stategraph_catalog(for_workflow=true)` -- the agents you may use: the ones you could start
   yourself. Use no other agent; a workflow naming one is refused.
2. Write the machine: one self-contained YAML text, submachines under `machines:`, no `imports`,
   no `python:`. Give it an id of its own (not one of a saved machine).
3. `stategraph_validate_machine(yaml=...)` until it reports no error.
4. `stategraph_run_workflow(yaml=..., params=..., max_output_tokens=...)` starts it in the background. The
   answer says whether you will be woken and whether the result then comes by itself; its `next`
   says what to do. Otherwise read it with `stategraph_get_run(run_id, wait="finish")`.

## The subset

Use only these. No `python:`, no `call:`, no `resources:`, no `callback`, no wait states.

```yaml
stategraph: 1
id: <name>                      # [a-z][a-z0-9_]*
title: <one line>
params:                         # what you pass in
  files: {type: array, required: true}
limits: {max_output_tokens: 200000, concurrency: 4, max_steps: 400, timeout: 1h}   # always set max_output_tokens
context: {results: []}          # ctx: the workflow's variables (JSON)
machines:                       # local submachines: a box per item, see "Pipeline"
  one: {params: {...}, context: {...}, initial: ..., states: {...}}
initial: <state>
states:
  <state>:
    do: <activity>
    transitions:
      - {target: <state>, guard: <python>, effect: <python>}
      - {target: <state>, guard: else}
      - {trigger: error, target: failed, effect: ctx.problem = error.message}
  done: {type: final, output: {results: "{{ ctx.results }}"}}
  failed: {type: final, status: failed, output: {problem: "{{ ctx.problem }}"}}
```

| Activity | Keys | `out` |
|---|---|---|
| `agent: <name>` | `task` (template), `schema` (JSON schema: the answer is parsed and validated, a bad one goes back to the agent), `llm_profile`, `parse_retries` | text, or the parsed value with `schema` |
| `map: <python list>` | `each` (an activity), `as` (item name), `concurrency`, `fail: fast\|collect`, `until` | list in item order |
| `parallel: {branch: activity}` | `fail: fast\|collect`, or `join: first\|{count: n}` | `{branch: out}` |
| `machine: <local name>` | `params` | the submachine's final `output` |
| `decide: noul` | `question`, `input` | `{value: probability of yes, ...}` -- a cheap yes/no judge |
| `emit: <text>` | -- | a status line for the caller |

- **A `map` without `concurrency` runs its items one after another.** Set it on every map whose
  items may run at once -- the outer one of a pipeline too, or it is no pipeline.
- With `fail: collect` each result is `{"status": "succeeded", "out": ...}` or
  `{"status": "failed", "error": {...}}`. Nothing is raised; you decide in the effect.
- **Code fields** (`guard`, `effect`, `map`, `until`) are plain Python, without braces:
  `len(out["findings"]) > 0`. **Templates** (`task`, `params`, `output`) are text with
  `{{ expr }}`; a value that is exactly `"{{ expr }}"` keeps its type.
- Read `ctx.x`, `params.x`, `out["x"]`, `error.type`. Only `effect` writes `ctx`. Code is pure: no
  I/O, clock, randomness, sets.
- `guard: else` is the last transition of its trigger. A loop needs `max_visits: n` on one of its
  states.
- In `each`, the item variable (`as`) and `index` are in scope.

## Patterns

**Fan-out with a barrier**: `parallel`, or `map` whose `each` is a single agent. The state completes
when every item is done; the next state sees all results together. Use it only when the next step
needs all of them (deduplicate, merge, rank).

**Pipeline, no barrier**: `map` whose `each` is a local submachine with several states. Every item
walks through its own stages; item A can be verified while item B is still being found. This is the
default for multi-stage work.

```yaml
states:
  per_module:
    do:
      map: params.modules
      as: module
      concurrency: 4
      fail: collect
      each: {machine: fix_one, params: {module: "{{ module }}"}}
```

**Adversarial verify**: every claim is checked by several skeptics told to refute it; it survives a
majority of "not refuted". A skeptic that fails is no vote.

```yaml
  verify:
    do:
      map: ctx.findings
      as: finding
      concurrency: 3
      each:
        map: range(3)
        as: skeptic
        fail: collect
        each:
          agent: <reviewer>
          task: "Try to refute this finding. Default to refuted=true if unsure.\n\n{{ finding }}"
          schema: {type: object, required: [refuted], properties: {refuted: {type: boolean}, reason: {type: string}}}
    transitions:
      - target: done
        effect: |
          ctx.findings = [f for f, votes in zip(ctx.findings, out)
                          if sum(1 for v in votes if v["status"] == "succeeded" and not v["out"]["refuted"]) >= 2]
```

Use skeptics on another `llm_profile` than the finders where the catalog offers one: a model judges
its own family's work too kindly. For a plain yes/no, `decide: noul` is cheaper than an agent.

**Loop until nothing new**: a finder state that loops while a round still brings something new, with
the keys seen so far in `ctx` (a list, not a set).

```yaml
  find:
    max_visits: 6
    do: {agent: <finder>, task: "Find problems not in this list:\n{{ ctx.seen }}", schema: {...}}
    transitions:
      - target: find
        guard: 'len([f for f in out["findings"] if f["key"] not in ctx.seen]) > 0'
        effect: |
          ctx.found += [f for f in out["findings"] if f["key"] not in ctx.seen]
          ctx.seen += [f["key"] for f in out["findings"] if f["key"] not in ctx.seen]
      - {target: verify, guard: else}
```

**Judge panel**: `parallel` of attempts from different angles, then one agent (or a `map` of judges)
scoring them with a `schema`, then the winner in the output.

## Rules

- Always `limits.max_output_tokens`: what its agents may write, thinking included -- the same on every model. Start small; a cap that is hit ends the run with `budget_exceeded`,
  which a transition can catch to return what was done so far.
- Prefer the pipeline to a barrier.
- Say what was dropped. A `fail: collect` that lost items, or an `until` that stopped early, goes
  into the output (`lost: [...]`), never silently away.
- Every agent answer you compute with has a `schema`. No parsing of prose in guards.
- The output is what you will read: keep it to what the caller needs, not every intermediate result.
