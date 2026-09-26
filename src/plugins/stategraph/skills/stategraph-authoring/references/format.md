# The stategraph format, version 1

The complete reference for machine authors. The contract behind it is
`docs/stategraph_design.md` §2–§4; this file must never contradict it.

Contents: 1 Files · 2 Top-level keys · 3 States · 4 Transitions · 5 Activities ·
6 Python and templates · 7 Events · 8 Errors · 9 Purity and data · 10 Counters,
limits, time · 11 Agent template vars · 12 Reuse · 13 Validation

---

## 1. Files

A machine is one YAML file `<id>.yaml` in a machine root. Next to it may sit:

- a **companion module** (`python: <file>.py`) whose public functions the machine's
  code can call;
- the machines it **imports** (`imports:`);
- a layout sidecar `<id>.layout.json` (editor positions; the engine never reads it).

Machine roots, searched in order (the first root that has an id wins):
`data/stategraph/machines` (writable, where `stategraph_save_machine` writes, not
versioned) and `src/plugins*/*/machines` (shipped with a plugin, versioned). A save writes
the whole tree or nothing, as UTF-8 with LF line endings on every OS; the versions it returns
are those a later `stategraph_get_machine` reports.

Unknown keys are errors everywhere, so a typo never silently drops behaviour. There is
no `on`, `yes` or `no` key, so YAML 1.1 readers cannot corrupt a file.

---

## 2. Top-level keys

| Key | Type | Req. | Meaning |
|---|---|---|---|
| `stategraph` | `1` | yes | Format version: the integer `1`. Other values are refused, `true` and `1.0` too. |
| `id` | name | yes | Machine id, unique across all roots; the file is `<id>.yaml`. |
| `title`, `description` | string | | Shown in the panel and the catalog. |
| `group` | string | | Its folder in the panel's machine list, nested by `/` (`Writer/v6`). Empty: the folder of where it comes from ("My machines" for the writable root, else the plugin that ships it). |
| `python` | path | | Companion module, relative to this file (`\` reads as `/`; not an absolute path). |
| `imports` | alias → ref | | Submachines: `./file.yaml` (relative; `\` reads as `/`, not an absolute path) or a machine id. |
| `params` | name → field | | The machine's parameters (run input, or a submachine's `params:`). |
| `events` | name → `{description, data}` | | The named events this machine accepts (§7). |
| `context` | name → JSON | | The machine's variables (`ctx`) with their initial values. Plain JSON, not templates. |
| `vars` | name → template, or one template | | Agent template vars for every agent this machine spawns (§11). |
| `vars_from` | agent name | | Take that agent's configured `template_vars` as vars (§11). |
| `limits` | `{max_steps, timeout}` | | `max_steps` (default 1000) per frame; `timeout` of the whole run, root machine only (§10). |
| `resources` | name → `{open, fork, close}` | | External state of each frame of this machine: a store namespace, a forum group (§14). |
| `finally` | activity | | Runs once when a frame of this machine ends, however it ends (§14). |
| `initial` | state name | yes | The first state: a top-level state, a choice or a junction. |
| `states` | name → state | yes | The top-level region (§3). |

**Names** -- ids, states, params, context keys, aliases, events, parallel branches,
question names, resources -- match `[a-z][a-z0-9_]*`. `finally` and `resources` are not state names. `done`, `error` and `completion` are
reserved and cannot name an event, parameter or context key.

### params

```yaml
params:
  topic:     {type: string, required: true, description: What to write about}
  max_rounds: {type: integer, default: 3}
  tone:      {type: string, enum: [formal, casual], default: casual}
  options:   {type: object, default: {}}
```

Field keys: `type` (`string`, `integer`, `number`, `boolean`, `object`, `array`,
`any` = default), `required`, `default`, `enum`, `description`. A required
parameter without a default must be given. A wrong type, a value outside `enum`, a
missing required or an unknown parameter is `params_invalid`: the run does not start
(or, for a submachine, its calling state fails). `true` is not an `integer`. Code
reads them with `params.topic`; they are read-only.

### context

```yaml
context:
  draft: null
  round: 0
  issues: []
  scores: {}
```

`ctx` starts as a deep copy of this mapping in every frame (every run, every
submachine instance). Declare every field you read: reading one that is neither
declared nor assigned anywhere is warning SG105.

---

## 3. States

State names are unique within a machine file, nested states included, so a target is
always a plain name.

| Key | Applies to | Meaning |
|---|---|---|
| `type` | all | `state` (default), `choice`, `junction`, `final` |
| `description` | all | Free text for the editor |
| `entry` / `exit` | state | Python statements run on entering / leaving |
| `do` | simple state | The activity (§5); its end is the state's completion |
| `finally` | simple and composite state | An activity that runs once on every exit of the state, however it is left (§14) |
| `states` + `initial` | composite | A nested region; `initial` names a direct child |
| `transitions` | state, choice, junction | Ordered list (§4) |
| `max_visits` | state | The (n+1)-th entry within one activation of the parent region raises `loop_limit` in this state (§10) |
| `timeout` | wait state | A duration; when it expires, `wait_timeout` is raised in the waiting state |
| `status` | final of the root region | `succeeded` (default) or `failed` |
| `output` | final | A template value (§6): the machine's result, or `out` of its composite |

### Simple states: completing or waiting

A state without nested states is one of two kinds.

- It **completes** when its `do` finishes -- or right after entry if it has no `do`
  but has a completion transition. The completion must select a transition; if none
  is enabled, `no_transition` is raised in that state. Silent idling is impossible.
- It is a **wait state** if it has no `do` and no completion transition. It waits for
  declared named events (§7). A wait state that accepts no event is an error (SG003).

```yaml
states:
  prepare:                       # completes right after entry: an action-only state
    entry: ctx.attempt = 0
    transitions:
      - target: work
  work:                          # completes when the agent answers
    do: {agent: stategraph_example_agent, task: "Say hello."}
    transitions:
      - target: wait_for_ok
        effect: ctx.greeting = out
  wait_for_ok:                   # a wait state: no do, no completion transition
    timeout: 1h
    transitions:
      - trigger: ok
        target: done
      - trigger: error
        guard: error.type == "wait_timeout"
        target: done
  done:
    type: final
```

(`ok` must be declared under `events:`.)

### Composite states

A composite groups states: one nested region with its own `initial`. It has no `do`.
It **completes when one of its direct `final` children is entered**; that final's
`output` becomes `out` for the composite's completion transitions. An `error`
transition on the composite handles errors of every state inside it; an event
transition on it is offered while any state inside waits.

```yaml
  review:
    initial: critique
    states:
      critique:
        do: {agent: stategraph_example_agent, task: "Critique: {{ ctx.draft }}"}
        transitions:
          - target: reviewed
            effect: ctx.notes = out
      reviewed:
        type: final
        output: {notes: "{{ ctx.notes }}"}
    transitions:
      - target: publish                  # the composite's completion: out = {notes: ...}
        effect: ctx.review = out
      - trigger: error                   # any error inside review lands here
        target: failed
```

Entering a composite resets the visit counts of all its descendants, and continues
into its `initial`. A nested final never ends the machine; only a final of the root
region does.

### Final states

A final has only `type`, `status`, `output` and `description`: no `do`, `entry`,
`exit`, `transitions`, `max_visits`. `status` belongs only to finals of the root
region. The root final's `output` is the run's output -- or, for a submachine, `out`
of the state that called it. A root final with `status: failed` fails the machine
(for a submachine: the caller gets `submachine_failed`, §8).

```yaml
  done:
    type: final
    output: {summary: "{{ ctx.summary }}", rounds: "{{ ctx.round }}"}
  gave_up:
    type: final
    status: failed
    output: {reason: "not good enough after {{ ctx.round }} rounds"}
```

`output` sees `ctx`, `params`, `run` and the companion functions -- not `out` or
`error`. Store what the output needs in `ctx` on the way there.

### Choice and junction

Pseudostates: they are passed through within one transition, never stay active.

- Their transitions have **no trigger** and **need a target**.
- A **choice** evaluates its guards **after** the incoming transition's effect, so
  they see the updated `ctx`. It must end with `guard: else`.
- A **junction** evaluates its guards **together with** the incoming transition's
  guard, before anything runs. If no branch holds, the incoming transition is not
  enabled and selection moves on to the next candidate.
- `initial` may name a choice or junction (an initial router).
- A cycle made only of pseudostates is an error.

```yaml
initial: route
states:
  route:
    type: choice
    transitions:
      - target: short_path
        guard: len(params.text) < 2000
      - target: long_path
        guard: else
```

---

## 4. Transitions

```yaml
transitions:
  - trigger: done              # default; or "error", or a declared event name
    guard: out["value"] > 0.5  # Python expression, or "else"; optional
    effect: ctx.score = out["value"]   # Python statements; optional
    target: next_state         # required, except for internal event transitions
    description: why           # optional
```

- `trigger` defaults to `done`: the **completion** of the state (its activity ended,
  or it has no `do`, or -- on a composite -- a direct final child was entered).
  `error` catches errors (§8). Any other trigger must be declared under `events:`.
- `guard: else` always holds. It must be the last transition of its trigger within
  the state; a transition after an unguarded one of the same trigger never fires
  (SG104). A blank guard (`guard: ""`, only spaces) is no guard.
- A transition **without `target`** is an **internal transition**: its effect runs,
  no state is exited or entered. It needs a named-event trigger. Completion and error
  transitions always need a target.
- `effect` holds Python statements; several lines with `|`:

  ```yaml
      effect: |
        ctx.round += 1
        ctx.history.append(out)
  ```

### Selection

- **Completion is local.** Only the completing state's own `done` transitions are
  candidates. A nested state's completion never selects an enclosing composite's
  completion transition; a composite completes only through a direct final child.
- **Errors and named events go inner-first**: the active leaf's transitions first,
  then each enclosing composite's, each in list order.
- The first transition whose trigger matches and whose guard holds fires.

### Execution

1. The source leaf and its ancestors are exited from the inside out, up to (not
   including) the innermost state that contains both source and target (the
   *domain*; the machine itself if none). Their `exit` actions run in that order.
2. The transition's `effect` runs.
3. States are entered from just below the domain down to the target; their `entry`
   actions run in that order. Entering a composite continues into its `initial`.

Transitions are external: a self-transition exits and re-enters its state (entry,
activity and `max_visits` count again). A transition from a composite to one of its
own descendants exits and re-enters the composite.

A transition through junctions or choices runs **segment by segment**: each segment exits
up to the domain of its own source and target and runs its effect; the entries come last.
So a junction outside a composite exits that composite and re-enters it, even when the
source and the final target both lie inside it: its `exit` and `entry` run again and the
visit counts inside it reset. (UML takes the domain of the whole transition here; the
validator and the engine both follow the segments.) Keep a junction inside the composite
whose states it connects.

A transition is **atomic**. If an exit action, the effect, a choice guard or an entry
action raises, `ctx`, the active states and the visit counts are restored to where
they were, and `action_failed` (or `guard_failed`) is raised in the source leaf. A
guard that raises is `guard_failed`.

---

## 5. Activities (`do:`)

A `do` mapping has exactly one **kind key**; the other keys belong to that kind or are
common keys. The kinds are a registry -- `stategraph_catalog` lists all of them with
their fields, including kinds other plugins add.

### Common keys

| Key | Meaning |
|---|---|
| `retry` | `attempts`: total tries (default 1, at most 20). `backoff`: a duration, or `{initial, factor, max}` (defaults 1, 2, 60). `errors`: the error types to retry (unknown names get SG110). |
| `timeout` | Deadline of **one attempt**. On expiry the attempt is cancelled and `timeout` is raised. Agents have no timeout by default. |
| `idempotent` | Whether a resumed run may start the activity again if it was in flight at a crash. Default `true`, for `tool` `false` (§8, `interrupted`). |
| `description` | Free text. |

Without `errors`, every error type is retried except `cancelled`, `interrupted`,
`timeout`, `template_failed`, `params_invalid`, `not_serialisable`, `unmocked`,
`no_backend`, `config` and `tool_denied`. A timeout is retried only if `errors` lists it.
The engine is the only retry layer.

A retry of a composite (`machine`, `parallel`, `map`) runs its children again, so two more
rules apply:

- Once an activity inside the composite raised `interrupted`, the composite is not retried --
  handled or not. That activity is not idempotent; a retry would start it a second time. A
  `finally` or `close` inside does not count: the retry's frame runs its own.
- A type from the list above in the error's chain of **unhandled** causes (`error.cause`, its
  `cause`, ...) stops the composite's retry too, unless `errors` names it.
  `errors: [submachine_failed, timeout]` retries a submachine that ended because a child
  timed out; `errors: [submachine_failed]` alone does not. If the submachine handles the
  timeout itself and then fails, its own failure is the cause, and the default rule applies.

```yaml
    do:
      agent: research_worker
      task: "Find three sources on {{ params.topic }}."
      timeout: 15m
      retry: {attempts: 3, backoff: {initial: 10s, factor: 2, max: 2m}, errors: [agent_failed, timeout]}
```

Durations: a number of seconds, or a string `500ms`, `30s`, `10m`, `2h`.

### agent

Runs an agent -- a new instance, or a follow-up of an existing one -- and waits for its
answer. Any configured agent: the backend runs the registered agent itself, on an
instance session of its own; there is no list of spawnable agents to add it to.

| Key | Meaning |
|---|---|
| `agent` | Agent name. A literal, or `"{{ params.x }}"` where parameter `x` has an `enum` (every value is checked). Required, also with `continue`. |
| `task` | The message (template). Required. |
| `schema` | JSON schema: the answer is parsed as JSON and validated. One that is not a valid JSON schema is SG005. |
| `parse` | A companion function name (or `package.module:function`), called as `fn(text)`; its return value is `out`. Raising `ValueError` sends the message back to the same instance as feedback. |
| `parse_retries` | Feedback rounds for `schema`/`parse` failures (default 1, at most 5). |
| `vars` | Template vars for this call (templates), over the machine's (§11). |
| `advanced` | `true` uses the agent's advanced model profile. |
| `continue` | Template: an instance id (of this run, and of the agent `agent` names) to follow up instead of starting a new instance. The instance keeps its conversation, also across a resume. One call per instance at a time: a second concurrent `continue` of it fails with `config`. |

`out` is the answer text, or the parsed value when `schema` or `parse` is set (with
both, `parse` runs first and its result is validated against `schema`). After the
feedback rounds, a still unusable answer raises `parse_failed` (with `parse`) or
`schema_invalid`. `activity.instance_id` names the instance, for a later `continue`.

```yaml
  rate:
    do:
      agent: stategraph_example_agent
      task: |
        Rate this text from 1 to 5 and name its biggest problem.
        Answer with JSON only: {"score": <1-5>, "problem": "<one sentence>"}

        {{ ctx.text }}
      schema:
        type: object
        required: [score, problem]
        properties:
          score: {type: integer, minimum: 1, maximum: 5}
          problem: {type: string}
    transitions:
      - target: done
        guard: out["score"] >= 4
      - target: improve
        guard: else
        effect: |
          ctx.problem = out["problem"]
          ctx.rater = activity.instance_id
```

A parser in the companion module:

```python
def parse_verdict(text):
    """'VERDICT: yes|no' on the last such line -> bool; anything else goes back as feedback."""
    for line in reversed(text.splitlines()):
        if line.strip().upper().startswith("VERDICT:"):
            value = line.split(":", 1)[1].strip().lower()
            if value in ("yes", "no"):
                return value == "yes"
    raise ValueError("end your answer with one line 'VERDICT: yes' or 'VERDICT: no'")
```

```yaml
    do:
      agent: stategraph_example_agent
      task: "Is the plan below complete? Explain briefly, then end with VERDICT: yes or VERDICT: no.\n\n{{ ctx.plan }}"
      parse: parse_verdict
      parse_retries: 2
```

Following up the same instance:

```yaml
  shorten:
    do:
      agent: stategraph_example_agent
      continue: "{{ ctx.rater }}"
      task: "Now rewrite the text so that its biggest problem is gone. Answer with the text only."
```

### tool

Calls a tool directly, without an LLM, through the plugin's runner agent. Only tools
in the runner's allowlist can be called, and never stategraph's own tools.

| Key | Meaning |
|---|---|
| `tool` | The flat tool name as agents see it, **with its instance prefix** (`stategraph_json_manage_json`). A literal, or `"{{ params.x }}"` with an `enum`. |
| `args` | Arguments (a template map). |
| `error_if` | Python expression over `out`; true turns the result into `tool_failed`. |

`out` is the tool's result. An error-shaped result (`{"status": "error", ...}` and
the other shapes the core recognises) or a true `error_if` raises `tool_failed` with
`error.data` = the whole result. A tool the runner may not call raises `tool_denied`.
Configured secrets (`inject_params` of the plugin) are added after rendering, so they
never appear in a machine file. A tool activity is not idempotent by default.

```yaml
  save:
    do:
      tool: stategraph_json_manage_json
      args:
        operation: write
        doc: result
        if_exists: replace
        data: "{{ {'summary': ctx.summary, 'rounds': ctx.round} }}"
    transitions:
      - target: done
      - trigger: error
        guard: error.type == "tool_failed"
        target: report_problem
        effect: ctx.problem = error.message
```

### decide

Asks a decision model -- one call, no prose -- or, with `by:`, an agent. Which model
decides is configuration, never part of the machine: `llm_system.default_decision_profile`,
or a `profile` of `llm_system.decision_profiles` (`stategraph_catalog` lists them).

| Key | Meaning |
|---|---|
| `decide` | `noul` (probability of yes), `choice` (one option), `score` (a point on a scale), or `questions` (several named questions in one call) |
| `question` | What to decide (template). Required for noul/choice/score. |
| `criteria` | choice: `{option: meaning}` with ≥ 2 options; score: `[lowest, …, highest]` with ≥ 2 entries; noul: optional `{"true": …, "false": …}` (quote the keys: unquoted, YAML reads them as booleans) |
| `input` | The content to judge (template, text or object). Required and not empty (empty: `template_failed`). |
| `questions` | For `decide: questions`: `{name: {type, question, criteria}}` |
| `profile` | Decision profile (`llm_system.decision_profiles`); default: the configured default. |
| `by` | An agent that decides instead of a decision model: a literal, or `{{ params.x }}` with an enum -- checked like an `agent:` (SG005/SG007). Not together with `profile`. |
| `advanced` | With `by`: `true` uses the agent's advanced model profile. |
| `parse_retries` | With `by`: feedback rounds when its answer is not usable (default 1, at most 5). |

`out` is `{value, confidence, probabilities}`; `confidence` and `probabilities` may be
`null`. `value` is a probability 0–1 (noul), the option name (choice), or a position
on the scale counted from 0 (score; it may fall between points, e.g. `2.96`). For
`decide: questions`, `out` is `{name: {value, confidence, probabilities}}`.

With `by:` the agent gets every question with the form of its answer (a probability, one of
the options, one of the levels) and the content, and answers in JSON; an answer outside the
form goes back to the same instance as feedback, and after `parse_retries` rounds raises
`schema_invalid`. The questions reach it as JSON, as a decision model gets them: a choice
option is a string (`2` becomes `"2"`). It runs on an instance of its own with the machine's vars, like an agent
activity. `out` has the same shape: `value` as above (a score is the position of the level it
names, a whole number), `confidence` and `probabilities` are `null`. `activity.instance_id` is
the agent's instance. Criteria that a template renders into the wrong shape raise
`template_failed` before the agent is asked. A mock is the same `out` either way.

```yaml
  ending:
    do:
      decide: choice
      by: stategraph_example_agent                     # an agent judges instead of a decision model
      question: Which ending fits this story?
      criteria: {open: leaves the question open, closed: resolves it, twist: turns it around}
      input: "{{ ctx.story }}"
```

```yaml
  ready:
    do:
      decide: noul
      question: Is this chapter ready for a reader without further revision?
      criteria: {"true": "polished, consistent, complete", "false": "needs another pass"}
      input: "{{ {'chapter': ctx.chapter, 'notes': ctx.notes} }}"
    transitions:
      - target: publish
        guard: out["value"] >= 0.7
      - target: revise
        guard: else

  route_ticket:
    do:
      decide: choice
      question: Which team should handle this ticket?
      criteria:
        billing: payments, invoices, refunds
        tech: bugs, errors, outages
        sales: prices, upgrades, new customers
      input: "{{ params.ticket }}"
    transitions:
      - target: billing
        guard: out["value"] == "billing"
      - target: tech
        guard: out["value"] == "tech"
      - target: sales
        guard: else

  clarity:
    do:
      decide: score
      question: How clear is this explanation for a beginner?
      criteria: [unusable, confusing, acceptable, clear, excellent]
      input: "{{ ctx.explanation }}"
    transitions:
      - target: done
        guard: out["value"] >= 3          # "clear" or better
      - target: rewrite
        guard: else

  tags:
    do:
      decide: questions
      input: "{{ ctx.synopsis }}"
      questions:
        romance: {type: noul, question: Is romance a main thread of this story?}
        audience:
          type: choice
          question: Who is the main audience?
          criteria: {children: under 12, young_adult: 12 to 17, adult: 18 and older}
    transitions:
      - target: done
        effect: |
          ctx.romance = out["romance"]["value"] >= 0.5
          ctx.audience = out["audience"]["value"]
```

### call

Calls a Python function: a companion function by name, or `package.module:function`.
A first parameter named `sg` receives the read-only scope, `sg.tool()` for journaled tool
calls and `sg.Error` (§14).

| Key | Meaning |
|---|---|
| `call` | `build_index` (companion) or `package.module:function` |
| `args` | Keyword arguments (a template map) |

The function is called as `fn(**args)`, or `fn(sg, **args)` if its first parameter is
named `sg` -- it then receives the read-only scope (`sg.ctx`, `sg.params`, `sg.run`).
It may be sync or async. `out` is its return value, normalised to JSON. An exception
raises `call_failed`.

A sync function runs in a worker thread (the plugin's own pool of 8), so terminate and
`timeout` take effect while it works. A hung call holds its thread; once all 8 are held, the
next sync call waits, and the wait counts against its `timeout`. A thread cannot be killed: on a timeout or terminate the activity ends at once and the
thread's late result is dropped, but the thread runs on to its end. `sg.tool()` works only on
the run's event loop: make a function that calls tools `async` (a sync one may only
`return sg.tool(...)`, which is then awaited).

A `call` activity is the place for deterministic computation that is too big for an
effect, and for reading the outside world (a file, a database row): its result is
journaled, so a resumed run gets the same value without calling again. Unlike code
fields, a `call` activity may do I/O. It runs for real even in a mock-only run.

```python
# companion module
def check_plan(chapters, min_words):
    short = [c["title"] for c in chapters if c.get("words", 0) < min_words]
    return {"ok": not short, "short": short}
```

```yaml
  check:
    do:
      call: check_plan
      args: {chapters: "{{ ctx.plan['chapters'] }}", min_words: 800}
    transitions:
      - target: done
        guard: out["ok"]
      - target: fix_plan
        guard: else
        effect: ctx.short = out["short"]
```

### machine

Runs an imported machine as a submachine, in its own frame (§12).

| Key | Meaning |
|---|---|
| `machine` | An import alias (always literal) |
| `params` | Its parameters (a template map) |

`out` is the `output` of the final it ends in. A final with `status: failed` raises
`submachine_failed` with `error.data` = that output; an unhandled error inside raises
`submachine_failed` with `error.cause` = the inner error.

```yaml
imports:
  critique_round: ./critique_round.yaml
states:
  review:
    do:
      machine: critique_round
      params: {text: "{{ ctx.draft }}", strict: true}
    transitions:
      - target: judge
        effect: ctx.notes = out["notes"]
      - trigger: error
        guard: error.type == "submachine_failed"
        target: failed
```

### parallel

Runs several activities at once and joins when all are done.

| Key | Meaning |
|---|---|
| `parallel` | `{branch: activity}` (≥ 1 branch; names like state names) |
| `fail` | `fast` (default): the first failure cancels the other branches and is raised, with `error.branch`. `collect`: all branches run to the end; nothing is raised. |

`out` is `{branch: out}`. With `fail: collect`, `out[branch]` is
`{"status": "succeeded", "out": …}` or `{"status": "failed", "error": {…}}`.

```yaml
  opinions:
    do:
      parallel:
        style: {agent: stategraph_example_agent, task: "Judge the style only:\n\n{{ ctx.text }}"}
        facts: {agent: research_worker, task: "Check the facts only:\n\n{{ ctx.text }}"}
      fail: collect
    transitions:
      - target: merge
        effect: |
          ctx.opinions = {name: r["out"] for name, r in out.items() if r["status"] == "succeeded"}
```

A branch may be any activity: a `machine` (a submachine per branch), a `map`, another
`parallel`. Each agent instance has its own session, so parallel agent activities may
set different `vars` (§11).

### map

Runs one activity per item of a list.

| Key | Meaning |
|---|---|
| `map` | A Python expression giving the list (a code field: no braces) |
| `each` | The activity per item |
| `as` | Name of the item variable (default `item`) |
| `concurrency` | Items at once (default 1 = strictly one after another in list order; at most 64) |
| `fail` | As in `parallel`; a fast failure carries `error.index` |

`out` is a list in item order (`[]` for no items), whatever order the items finish
in. Inside `each`, the item variable and `index` (0-based) are in scope.

`error.branch` and `error.index` always name the failed child of the state's own join: a
`map` or `parallel` nested in that child set them first, and each join on the way out
overwrites its key with its own child and drops the other one.

```yaml
  translate:
    do:
      map: ctx.paragraphs
      as: paragraph
      concurrency: 4
      each:
        agent: stategraph_example_agent
        task: "Translate paragraph {{ index + 1 }} into German. Answer with the translation only.\n\n{{ paragraph }}"
    transitions:
      - target: done
        effect: ctx.translated = out
```

For a loop that must see the previous item's result, use `concurrency: 1` with a
submachine per item and carry the state in the item, or loop with states and a
counter in `ctx` (see `patterns.md`).

---

## 6. Python and templates

### Code fields -- plain Python, no braces

`guard` (expression, or `else`), `effect`, `entry`, `exit` (statements), `map` and
`error_if` (expressions). Writing `{{ }}` around code is error SG004.

YAML reads `: ` and ` #` inside a plain value as structure and comment. Code that
contains them -- a dict literal, a string with a colon -- goes into a block scalar:

```yaml
    effect: |
      ctx.problem = error.state + ": " + error.message
      ctx.summary = {"state": error.state, "type": error.type}
```

### Template fields

Exactly these: `task`, `args`, `params`, `vars`, `question`, `criteria`, `input`,
`continue`, `output`, and a kind value of the form `"{{ params.<name> }}"`. Everything
else is literal: `schema`, `retry`, `timeout`, `as`, `concurrency`, `fail`,
`description`, `context` values, the kind value otherwise.

A template value is literal unless it contains `{{ }}`.

- A value that is **exactly one** `{{ expr }}` keeps the expression's type:
  `data: "{{ ctx.scores }}"` passes a dict, `items: "{{ ctx.list[:3] }}"` a list.
- In **mixed text**, each expression is inserted as text: strings as they are, dicts
  and lists as JSON, `None` as empty text.
- An expression ends at the first `}}` that closes a parseable Python expression, so
  dict literals work: `"{{ {'a': ctx.x} }}"`.
- A literal `{{` is written `{{ '{{' }}`.
- In a mapping or list, every string inside is rendered.
- Quote a YAML value that starts with `{{` -- unquoted, YAML reads it as a mapping
  (SG001).

```yaml
task: "Summarise {{ params.title }} in {{ params.sentences }} sentences."   # text
args:
  doc: "{{ params.doc_id }}"                # keeps the string
  data: "{{ {'title': params.title, 'tags': ctx.tags} }}"   # a dict
  limit: 10                                 # literal
input: "{{ ctx.draft }}"
```

A value that looks like a reference but has no braces (`input: ctx.draft`) is the
literal text `ctx.draft` -- warning SG107.

### Names in scope

| Name | What | Bound in |
|---|---|---|
| `ctx` | the frame's context | everywhere |
| `params` | bound parameters, read-only | everywhere |
| `run` | `id`, `origin` (the first run of a fork chain; use it to name external things), `step` (frame-local), `state`, `visits` (visit counts by state name, frame-local), `frame` | everywhere |
| companion names | every public name of the companion module | everywhere |
| `out` | result of the activity that completed (plain data), or the output of the nested final that completed a composite | completion transitions, `error_if` |
| `activity` | meta of that activity: `instance_id`, `request_id`, `attempts`, `duration_s`, `mocked`, and per kind `agent`, `feedback_rounds`, `model`, `cost` -- always present, `null` when they do not apply; whether an outcome was replayed is not exposed | completion and error transitions |
| `error` | `type`, `message`, `state`, `data`, `cause` (and `branch` / `index` from parallel / map) | error transitions |
| `event` | `name`, `data` | event transitions |
| item variable, `index` | the map variables (name set by `as`) | inside `map.each` |
| `resources` | `resources.<name>`: what each resource's `open` (or `fork`) returned, read-only; `null` while it is not opened (its `open` failed, or has not run yet) | everywhere, in a machine that declares `resources` |
| `ending` | `reason` (`transition`, `finished`, `failed`, `cancelled`), `state`, `error` | `finally` activities and resource `close` |
| `fork_source` | the source run's value of this resource | a resource's `fork` |

`entry`, `exit`, activity templates, `map` and a final's `output` see only the
"everywhere" names. Using a name where it is not bound (`out` in an entry, `error` in
a completion transition) is SG004. Python builtins are available (`len`, `sorted`,
`min`, `any`, `str`, …).

**Access rules.** `ctx`, `params`, `run`, `error`, `event`, `activity`, `resources` and `ending` allow
attribute access at the top level (`ctx.draft`, `error.type`) and item access
(`ctx["draft"]`). Below the top level the values are plain JSON data -- dicts, lists,
strings, numbers -- so use item access: `ctx.critique["notes"]`,
`event.data["reason"]`, `error.data["error"]`. `out` is plain data itself:
`out["value"]`, `out[0]`, or `out` for a text answer.

**Writing ctx.** Only `effect`, `entry` and `exit` may change `ctx`:
`ctx.x = …`, `ctx["x"] = …`, `ctx.items.append(…)`, `ctx.count += 1`, `del ctx.x`.
Guards, templates, `map`, `error_if` and debugger expressions are read-only; a change
there raises `guard_failed` ("ctx mutated"). The other namespaces are read-only
everywhere: assigning or deleting a field of `params`, `run`, `resources`, `error`,
`event`, `activity` or `ending` (`error.message = …`) is SG004 -- unless the code has a
local variable of that name (`for event in ctx.events: ...`).

### The companion module

```yaml
python: review_loop.py
```

Every public name of the module (not starting with `_`) is in scope in all code and
templates of this machine file, and its functions can be `parse` and `call` targets.
The module is not executed to validate a machine -- its names come from a scan of what it
binds at the top level, also inside top-level `if`/`try`/`with`/`for` blocks (an import
with a fallback). The scan takes as a function a `def`, a class, a name imported with
`from … import`, and a name assigned anything but data (`build = partial(...)`, a
lambda) -- data being a literal, arithmetic or a comparison of data, or an `and`/`or` of
data (`LIMIT = -1` is data); `a, b = 1, f` pairs each name with its own value, and a name
unpacked from anything else (`a, b = make()`), or bound by a loop, a `with` or a walrus, counts as a function;
a star import's names are unknown to it, so with one it takes any name and the run decides. Companion functions used by code
fields and templates must be pure (§9). An imported machine has its own companion module;
names do not leak between files.

For a run the module is executed as a real module of its own (in `sys.modules` while the
run holds it), so dataclasses, pydantic models -- also with `from __future__ import
annotations` -- and `typing.get_type_hints` work as in any module.

---

## 7. Events

```yaml
events:
  approve:
    description: The reviewer accepts.
  reject:
    description: The reviewer sends it back.
    data: {type: object, required: [reason], properties: {reason: {type: string}}}
```

- **Declaration.** Every named trigger must be declared under `events:` (SG002).
  `data` is an optional JSON schema for the payload; one that is not a valid JSON schema
  is SG001.
- **Sending.** `stategraph_send_event(run_id, name, data?, frame?)` (or the panel)
  records the event before it returns, so it survives a restart.
- **Routing.** Without `frame`, the event goes to the frame whose active states
  accept it (some active state has a transition with that trigger). If several
  frames accept it, the call is refused: name the frame. If none accepts it yet, it
  waits in the run's inbox until one does -- it is deferred, not dropped.
- **Delivery.** A frame takes events only in a wait state, in arrival order. Events
  never interrupt a running activity. Every submachine frame waits and receives
  independently -- a submachine waiting for approval inside a `parallel` branch or a
  map item works.
- **Handling.** `event.name` and `event.data` are bound in event transitions. An
  internal transition (no `target`) handles an event and keeps waiting.

```yaml
  awaiting_review:
    timeout: 48h
    transitions:
      - trigger: comment                # internal: note it, keep waiting
        effect: ctx.comments.append(event.data["text"])
      - trigger: approve
        target: publish
      - trigger: reject
        target: revise
        effect: ctx.reason = event.data["reason"]
      - trigger: error
        guard: error.type == "wait_timeout"
        target: expired
```

---

## 8. Errors

`error.type` is one of:

| Group | Types |
|---|---|
| activities | `agent_failed`, `schema_invalid`, `parse_failed`, `tool_failed`, `tool_denied`, `decision_failed`, `call_failed`, `submachine_failed`, `activity_failed` (an unexpected exception in a kind from another plugin), `timeout`, `interrupted`, `template_failed`, `params_invalid`, `unmocked`, `no_backend`, `config` |
| engine | `loop_limit`, `no_transition`, `guard_failed`, `action_failed`, `wait_timeout`, `not_serialisable` |

- **Handling.** An error is dispatched as the `error` event of the state where it was
  raised; selection walks outward (the state, then each enclosing composite). Filter
  with a guard: `guard: error.type in ("tool_failed", "timeout")`.
- An error raised while an error transition is selected or executed ends the frame;
  it is not handled twice. That includes a `loop_limit` raised by entering the
  transition's target past its `max_visits` (`error.cause` is the error the transition
  handled). So a state entered by an error transition cannot catch its own `loop_limit`:
  bound the loop in that transition's guard, e.g. `run.visits.get("repair", 0) < 2`, and
  let the next error transition take the end (patterns §3).
- **Unhandled errors** end their frame as failed. For a submachine, the calling state
  then receives `submachine_failed` with `error.cause` = the inner error; its message
  ends with the cause, `<alias> ended in <state> (failed): <cause type>: <cause
  message>` (the cause cut at 500 characters). Only a failing root frame fails the run.
- A `CancelledError` that the run did not cause -- a library cancelled a future inside
  an agent, tool, decide or call activity -- fails the activity (`agent_failed`,
  `tool_failed`, `decision_failed`, `call_failed`), and its error transition fires. Only
  the run's own cancel (terminate, the run's `limits.timeout`, a failing fast sibling)
  ends an activity without an error.
- **Not catchable** -- these end the run: more than `limits.max_steps` dispatches in
  a frame (`step_limit`), the run timeout (`timed_out`), terminate (`cancelled`), and a
  replay divergence (`diverged`).
- `interrupted`: a non-idempotent activity (by default every `tool`) was in flight
  when the process stopped; a resume does not start it again. `error.data` holds its
  rendered inputs. Handle it where a second call would do harm, e.g. by looking up
  whether the first call took effect. A non-idempotent submachine first ends the frame it
  had started (its `finally` and `close` run); in a run being terminated it ends
  `cancelled` instead.
- `unmocked`: a mock-only run met an agent, tool or decide activity without a mock.

---

## 9. Purity and data

All code fields and templates, and every companion function they call, must be
**pure functions of the scope**: no I/O, no clock, no randomness, no environment, no
set iteration order and no `hash()`. The engine replays a run by re-running this code
against the journal, and checks it: a different result is a divergence and stops the
run. Reading the outside world is an activity (`tool`, `call`) whose result goes into
`ctx`.

SG106 warns about calls into `random`, `time`, `uuid`, `secrets`,
`datetime.now/utcnow/today`, `date.today`, `os.getenv/urandom`, and `open()`,
`input()`, `hash()`, `set()`, `frozenset()`, `id()` and set literals. Write membership
tests with tuples: `x in ("a", "b")`.

**Data is JSON.** `ctx` must hold JSON data; a value that is not (a set, an object)
raises `not_serialisable`. So does a final `output` that renders to something that is not
JSON (a dict with tuple keys): the frame fails, and its `finally` activities run. Every value that crosses a boundary -- activity results,
submachine params, map items, final outputs, event data, debugger edits -- goes
through a canonical JSON round trip: tuples become lists, dict keys become strings,
and every activity result is a fresh copy (dataclasses and pydantic models are dumped
first).

---

## 10. Counters, limits, time

- `run.step` counts the dispatches of the current frame; `run.visits["state"]` its
  entries of a state. Both are frame-local.
- `max_visits: n` on a state: the (n+1)-th entry within one activation of the parent
  region makes the state active **without** running its entry action or activity and
  raises `loop_limit` in it. Entering a composite resets the counts of everything
  inside it. Every loop needs `max_visits` on one of its states or a guard on a
  counter (SG103).
- `limits.max_steps` (default 1000) bounds the dispatches of each frame of the machine
  that declares it; exceeding it ends the run (`step_limit`).
- An activity `timeout` applies per attempt.
- A wait state's `timeout` starts when the wait starts; its deadline is stored, so a
  resumed run waits only the rest. An internal transition does not restart it.
- `limits.timeout` bounds the whole run, counting only time in status `running` (not
  `waiting`, `paused`, `interrupted`). It is honoured on the root machine only
  (SG108 warns when such a machine is used as a submachine).

---

## 11. Agent template vars

Agents are steered by session template vars (prompt branches, store namespaces,
shared prompt blocks). A machine sets them declaratively:

```yaml
vars:                                   # for every agent this machine spawns
  phase: "{{ params.phase }}"
  genre: thriller
vars_from: v6_story_coordinator         # that agent's configured template_vars
states:
  write:
    do:
      agent: stategraph_example_agent
      task: "..."
      vars: {tone: "{{ ctx.tone }}"}    # only for this call
```

The effective vars of an agent activity are merged in this order, each layer over the
one before: the calling frame's effective vars (a submachine inherits its caller's),
then this machine's `vars_from` and `vars`, then the activity's own `vars`. So a
submachine's own var overrides the caller's var of the same key -- a reusable
submachine sets `phase: "{{ params.phase }}"` per call and the caller's `phase`
cannot shadow it -- and an activity's `vars` win over both. The instance's session
holds exactly these effective vars during the call, over the agent's own configured
`template_vars`: they are replaced for every agent call, never accumulated. A
follow-up (`continue`) sees the values of its own call. They are journaled with the
activity. Every instance has its own session, so concurrent agent activities never
see each other's vars.

Use `params` in machine-level `vars`; for values from `ctx` use the activity's own
`vars`.

---

## 12. Reuse

**Submachines** are the unit of reuse. A machine with `params` and final `output`s is
imported under an alias and run with `do: {machine: <alias>, params: …}`.

- Each instance is its own **frame** with its own `ctx`, step counter and visit
  counts. Data flows in only through `params` (deep-copied) and out only through the
  final `output`.
- A submachine can be used any number of times with different parameters, inside
  `parallel` and `map`, and nested to any depth. Import cycles are refused.
- A variant is a parameter, or a submachine for the part that varies. There is no
  inheritance and no text include: the graph you see is the graph that runs.

**Composite states** group states that share transitions: one `error` transition on
the composite covers everything inside; an event transition on it is offered while
any state inside waits.

---

## 13. Validation

`stategraph_validate_machine`, every save and every run perform the same checks.

| Code | Level | Check |
|---|---|---|
| SG001 | error | YAML syntax, duplicate keys, a non-string mapping key (an unquoted `{{ … }}`, or a `true`/`yes` key read as a boolean), a format version other than the integer `1`, schema (unknown key, wrong type, a param default that does not fit its type or enum), an `events.<name>.data` that is not a valid JSON schema, YAML nested more than 100 levels deep or expanding through its aliases to more than 100,000 values or 10,000,000 characters of text (an alias inside the node it names counts as too deep), a machine whose file is not `<id>.yaml` |
| SG002 | error | Unknown or duplicate state name; unknown target or `initial`; an undeclared event trigger |
| SG003 | error | Structure: a choice without `else`; `else` not last; triggers on choice/junction; a final with other keys; `status` on a nested final; a composite with `do` or without `initial`; an internal completion or error transition; a wait state that accepts no event; a cycle of pseudostates only |
| SG004 | error | Python does not compile, or is nested too deeply for Python's parser; unknown name; a name not bound at that place; `{{ }}` in a code field; `params.<name>` not declared; `out.value` (write `out["value"]`), `ctx.a.b` (write `ctx.a["b"]`), `ctx.get(...)` (namespaces have no dict methods); assigning or deleting a field of `params`, `run`, `resources`, `error`, `event`, `activity` or `ending`; a companion function that does not exist; `python:`/`imports:` outside the machine roots |
| SG005 | error | Activity: unknown kind, several kind keys, invalid fields (including decide criteria shapes, per-question keys, a map `as` that shadows a scope name, `by` together with `profile`), a `schema` that is not a valid JSON schema, a computed `agent:`/`tool:`/`by:` other than `{{ params.<name> }}` with an enum |
| SG006 | error | Submachine: unknown alias, import cycle, an absolute import path, missing required or unknown parameter |
| SG007 | error | Configuration: an agent (`agent:`, `decide`'s `by:`) that is not configured, not enabled or not an agent, or one that reaches machines (the runner, a machine facade -- use it as a submachine --, an agent that may save, run or control machines, or start such an agent through a SAM), the runner may not call the tool, the tool is a SAM's (use an agent activity), the tool is stategraph's own, an unknown decision profile, a `vars_from` agent that is not configured |
| SG101 | warning | A state is unreachable from `initial` |
| SG102 | warning | No path leads from a state to a root final |
| SG103 | warning | A loop without `max_visits` on any of its states |
| SG104 | warning | A transition after an unguarded one of the same trigger never fires |
| SG105 | warning | `ctx.<name>` is read but neither declared in `context` nor assigned |
| SG106 | warning | Impure code in a code field or template |
| SG107 | warning | A data field whose whole value looks like a reference, without braces |
| SG108 | warning | A root machine with `limits.timeout` used as a submachine |
| SG110 | warning | `retry.errors` names an error type the engine does not raise, or `interrupted` (never retried) |

Every problem names a path (`states.judge.transitions[1].guard`), the file and, when
known, the line.

## 14. Multi-call steps, cleanup, external state

**`sg.tool()` in a call.** An `async` companion function whose first parameter is `sg` may
call tools: `await sg.tool("v6_story_json_manage_json", {"operation": "read", ...})`. A sync
function runs in a worker thread: it can only return `sg.tool(...)`, not run it (§5 `call`).

- Each call is journaled as a child tool activity (`<key>/t.<n>`, path
  `<call path>/<tool>`, e.g. `review/store_read`: mocks answer by that path, `$visits` for
  repeated calls).
- The function runs again from the top on a resume: finished calls replay, so it must be
  deterministic apart from these calls. `args` is data, not a template.
- A call in flight at a crash raises `interrupted` unless `idempotent=True`; catch it
  with `except sg.Error as exc:` and reconcile. Tool failures are `sg.Error` too.
- Use it for steps whose data must not pass through `ctx`: read documents, map them,
  write a row, check it.

**`finally`.** An activity on a state or on the machine.

- It runs once per exit of its state, however the state is left: a transition (after it
  committed, before the target's `do` -- also on the initial entry), an unhandled error,
  terminate, the run timeout, a failing parallel sibling. The machine's `finally` runs when
  the frame ends. Innermost first.
- It reads `ending.reason` (`transition`, `finished`, `failed`, `cancelled`),
  `ending.state` and `ending.error`; it cannot change `ctx`; its result is dropped. A root
  final with `status: failed` ends the frame with reason `failed` and `ending.error` =
  `{type: final, message: "ended in the failed final state <name>"}`.
- A failing `finally` is journaled and traced (`finally_failed`); the exit goes on.
- It does not run when the process stops -- the run is `interrupted`, and the resume
  completes the ending. After a terminate it runs although the run is cancelled (60 s unless it
  has a `timeout`, counted from the terminate; one the bound cuts ends `timeout` and is not run
  again after a crash); agents and decisions there are called without the run's cancellation token.
  Under a cancelled caller (the agent facade's request, or one above it) a `finally` has 10 s,
  not 60: the platform then force-cancels the caller's request tree, the `finally`'s tool
  calls and agent runs included. Keep it short.
- A terminate that arrives while a `finally` runs lets it finish (and the rest, within the
  60 s); a second terminate is ignored, and the run's timeout no longer fires. Nothing pauses
  an ending run: breakpoints stay silent, a held pause lets go. After a crash each frame ends
  where it stood when the terminate came, so the `finally` of the states it stood in runs, not
  of the states a transition after it would have entered. No `finally` runs once a resumed run
  diverged, and the run ends `diverged` even if a call catches that error. Mock paths: `<state>/finally`,
  and the machine's `<machine id>.finally` (under a submachine: `<calling state>/<machine id>.finally`).

**`resources`.** Per-frame external state, e.g. a store namespace per run.

```yaml
resources:
  store:
    open:  {call: namespace_for, args: {run_id: "{{ run.id }}"}}
    fork:  {call: copy_store, args: {source: "{{ fork_source }}", run_id: "{{ run.id }}"}}
    close: {tool: v6_story_json_manage_json, args: {operation: stats, namespace: "{{ resources.store }}"}}
vars: {json_namespace: "{{ resources.store }}"}
```

- `open` runs when the frame starts (before `vars`), in declaration order; its result is
  `resources.<name>` everywhere in the machine. A resume replays it. A resource that was
  never opened (its `open` failed) reads as `null`: a `finally` can check it.
- `fork` runs instead of `open` in a forked run's root frame; without it a fork opens
  afresh. `close` runs when the frame ends, in reverse order, like a `finally`.
- Replay compares hashes with each resource value replaced by a token, so a fork with its
  own namespace still replays its prefix. Make resource values distinctive ids: strings of
  6+ characters, whole objects and lists, and the 6+-character strings inside them are
  replaced; numbers are not.
- An `open` or `fork` sees only the resources declared before it.

**`vars` as one template.** `vars: "{{ {**ctx.vars, 'phase': 'idee'} }}"` passes a whole
object (machine or agent activity); it must render to an object of names (else
`template_failed`; a string that is not exactly one template is SG005).

