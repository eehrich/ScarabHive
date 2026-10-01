# The stategraph machine format

The complete reference for machine authors is the skill reference
[`skills/stategraph-authoring/references/format.md`](../skills/stategraph-authoring/references/format.md)
-- the same text the authoring agent reads. Patterns:
[`references/patterns.md`](../skills/stategraph-authoring/references/patterns.md);
testing and debugging: [`references/debugging.md`](../skills/stategraph-authoring/references/debugging.md).
The contract behind them is `docs/stategraph_design.md` (§2 format, §3 semantics,
§4 validation); where they disagree, the design document wins. Runnable examples:
[`../machines/`](../machines/).

## Cheat sheet

```yaml
stategraph: 1                       # format version: the integer 1
id: my_machine                      # = file name; names match [a-z][a-z0-9_]*
title: My machine
# notes: {why: "free text"}         # drawn as notes on the panel's canvas; never run
# group: Reviews/nightly            # folder in the panel's machine list
# python: my_machine.py             # companion module: its public functions are in scope
# imports: {sub: ./sub.yaml}        # submachines, by alias: do: {machine: sub, params: {...}}
params:                             # run input / submachine parameters
  text: {type: string, required: true}
  rounds: {type: integer, default: 3}
  tone: {type: string, enum: [plain, lively], default: plain}
events:                             # named events this machine accepts
  approve: {description: go on}
context: {draft: null, round: 0}    # ctx: the machine's variables (JSON)
vars: {tone: "{{ params.tone }}"}   # template vars for every agent it spawns
limits: {max_steps: 1000, timeout: 2h}
initial: write
states:
  write:                            # a state with an activity: completes when it ends
    entry: ctx.round += 1           # Python statements
    max_visits: 5                   # 6th entry raises loop_limit here
    do:
      agent: stategraph_example_agent
      task: "Write about {{ params.text }}"      # a template
      retry: {attempts: 2, backoff: 5s}
      timeout: 10m                  # per attempt
    transitions:
      - target: judge               # trigger: done (completion) is the default
        effect: ctx.draft = out     # Python statements; out = the activity's result
      - trigger: error
        guard: error.type == "timeout"
        target: failed
  judge:
    do: {decide: noul, question: "Good enough?", input: "{{ ctx.draft }}"}
    transitions:
      - {target: hold, guard: 'out["value"] >= 0.7'}
      - {target: write, guard: ctx.round < params.rounds}
      - {target: failed, guard: else}           # else: last of its trigger
  hold:                             # a wait state: no do, no completion transition
    timeout: 24h                    # -> wait_timeout
    transitions:
      - {trigger: approve, target: done}
      - {trigger: error, target: failed}
  done: {type: final, output: {draft: "{{ ctx.draft }}"}}
  failed: {type: final, status: failed}
```

| Kind (`do:`) | Keys | `out` |
|---|---|---|
| `agent: <name>` | `task`, `schema`, `parse`, `parse_retries`, `vars`, `llm_profile`, `llm_params`, `advanced`, `continue` | answer text or parsed value |
| `tool: <flat tool name>` | `args`, `error_if` | the tool's result |
| `decide: noul\|choice\|score` | `question`, `input`, `criteria`, `profile` -- or `by` (an agent decides), `advanced`, `parse_retries` | `{value, confidence, probabilities}` |
| `decide: questions` | `questions: {name: {type, question, criteria}}`, `input`, `profile` or `by` | `{name: {...}}` |
| `call: <function>` | `args` | return value; `fn(sg, …)` gets `sg.tool()` (journaled tool calls) and `sg.Error`; a sync function runs in a worker thread (a timeout or terminate drops its late result and sets `sg.cancelled`, a `threading.Event` a long one checks; `sg.tool()` only from an async one, or returned) |
| `machine: <alias>` | `params` | the submachine's final output |
| `parallel: {branch: activity}` | `fail: fast\|collect` | `{branch: out}` |
| `map: <expression>` | `each`, `as`, `concurrency`, `fail` | list in item order |

Common keys: `retry {attempts, backoff, errors}`, `timeout`, `idempotent`, `description`.

Cleanup and external state: `finally: <activity>` on a state or the machine runs once on every
exit (reads `ending.reason/state/error`); `resources: {name: {open, fork, close}}` gives each
frame its own external state as `resources.<name>` (`null` until it is opened). `vars` may be
one template that renders to an object.

| Field kind | Fields | Syntax |
|---|---|---|
| code (Python, no braces) | `guard`, `effect`, `entry`, `exit`, `map`, `error_if` | `ctx.round < params.rounds` |
| template (literal unless `{{ }}`) | `task`, `args`, `params`, `vars`, `question`, `criteria`, `input`, `continue`, `output` | `"{{ ctx.draft }}"` keeps the type; mixed text interpolates |

| Bound | Where |
|---|---|
| `ctx`, `params`, `run`, companion names | everywhere |
| `out`, `activity` | completion transitions (`out` also in `error_if`) |
| `error`, `activity` | error transitions |
| `event` | event transitions |
| item variable, `index` | inside `map.each` |

Access: `ctx.draft`, `params.text`, `error.type` at the top level; below it item
access -- `ctx.notes["x"]`, `out["value"]`, `event.data["reason"]`. Only `effect`,
`entry` and `exit` write `ctx`; `params`, `run`, `resources`, `error`, `event`, `activity`
and `ending` are read-only (SG004). A blank guard is no guard. Code and templates are pure:
no I/O, clock, randomness, environment, sets or `hash()`.

Error types: `agent_failed`, `schema_invalid`, `parse_failed`, `tool_failed`,
`tool_denied`, `decision_failed`, `call_failed`, `submachine_failed`, `activity_failed`, `timeout`,
`interrupted`, `template_failed`, `params_invalid`, `unmocked`, `no_backend`, `config`;
engine: `loop_limit`, `no_transition`, `guard_failed`, `action_failed`,
`wait_timeout`, `not_serialisable`. Not catchable: `step_limit`, `timed_out`,
`cancelled`, `diverged`.

Mocks (test runs): `{"state": out}`, paths `review/critique` (submachine),
`opinions/style` (branch), `translate/2` (map item); `{"$visits": [...]}` per use of the path in the run;
`{"$error": {"type", "message"}}` to fail; `{"$timeout": true}` on a wait state with `timeout` (or a timer state, `after`): its time is up at once.
