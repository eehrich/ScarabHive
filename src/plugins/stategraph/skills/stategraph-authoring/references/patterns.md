# Patterns

Each pattern is a complete machine that validates against the shipped configuration
(agents `stategraph_example_agent`, the plain agent this plugin ships, and `research_worker`;
tools of `stategraph_json` through the runner). Name agents whose answer is plain text.
Swap in the agents and tools
`stategraph_catalog` lists for you. The machines in `src/plugins/stategraph/machines/`
are the same patterns, runnable.

Contents: 1 Review loop with a decision model · 2 Retry with feedback to the same instance ·
3 Tool error → repair state · 4 Fan-out and join · 5 Map over data · 6 Human
approval · 7 Submachine reuse · 8 Shared error handling · 9 The v6 ritual ·
10 Small ones: initial router, counter loop, reading the outside

---

## 1. Review loop with a decision model

Write, let a calibrated decision model judge, revise until good enough or out of
rounds. Two bounds: the guard on `ctx.round` (the planned end) and `max_visits` (the
safety net). The judge's threshold is yours; `out["value"]` of a `noul` is a
probability.

```yaml
stategraph: 1
id: review_loop
title: Write, judge, revise
params:
  brief:      {type: string, required: true}
  max_rounds: {type: integer, default: 3}
context:
  draft: null
  round: 0
  score: null
initial: write
states:
  write:
    max_visits: 5
    entry: ctx.round += 1
    do:
      agent: stategraph_example_agent
      task: |
        {{ 'Revise the draft below so that it fulfils the brief.' if ctx.draft else 'Write a draft that fulfils the brief.' }}
        Brief: {{ params.brief }}
        {{ ctx.draft or '' }}
    transitions:
      - target: judge
        effect: ctx.draft = out
      - trigger: error
        target: failed
  judge:
    do:
      decide: noul
      question: Does this draft fulfil the brief completely and read well?
      input: "{{ {'brief': params.brief, 'draft': ctx.draft} }}"
    transitions:
      - target: done
        guard: out["value"] >= 0.7
        effect: ctx.score = out["value"]
      - target: write
        guard: ctx.round < params.max_rounds
      - target: best_effort
        guard: else
        effect: ctx.score = out["value"]
      - trigger: error
        target: failed
  done:
    type: final
    output: {draft: "{{ ctx.draft }}", rounds: "{{ ctx.round }}", score: "{{ ctx.score }}"}
  best_effort:
    type: final
    status: failed
    output: {draft: "{{ ctx.draft }}", score: "{{ ctx.score }}", reason: out of rounds}
  failed:
    type: final
    status: failed
```

Test: `mocks: {"write": {"$visits": ["d1", "d2"]}, "judge": {"$visits": [{"value": 0.3,
"confidence": null, "probabilities": null}, {"value": 0.8, "confidence": null,
"probabilities": null}]}}` ends in `done` after two rounds.

With notes for the revision, put a critic between writer and judge -- see
`machines/scene_review.yaml`, which also shows a companion function building the task.

Where no decision profile fits the question, let an agent judge: `by: <agent>` on the
`decide`. `out` keeps its shape (with `confidence` and `probabilities` `null`), so the
guards and the mocks stay the same.

---

## 2. Retry with feedback to the same instance

Two layers.

- **Format problems** are caught by `parse` (or `schema`): the parser raises
  `ValueError`, the engine sends its message to the same instance, up to
  `parse_retries` times, then raises `parse_failed`.
- **Content problems** found later (a check, a store refusing the result) go back with
  `continue:` to the same instance, which still has its conversation. Bound that loop
  with `max_visits`.

```python
# counted.py
def parse_count(text):
    for line in reversed(text.splitlines()):
        if line.strip().upper().startswith("COUNT:"):
            value = line.split(":", 1)[1].strip()
            if value.isdigit():
                return int(value)
    raise ValueError("end your answer with one line 'COUNT: <whole number>'")


def check_count(count, expected_max):
    if count > expected_max:
        return {"ok": False, "problem": f"{count} is more than the list can hold ({expected_max})."}
    return {"ok": True, "problem": None}
```

```yaml
stategraph: 1
id: counted
title: Count with checked feedback
python: counted.py
params:
  text: {type: string, required: true}
  expected_max: {type: integer, default: 100}
context:
  count: null
  counter: null
  problem: null
initial: count
states:
  count:
    do:
      agent: stategraph_example_agent
      task: "How many distinct people are named in this text? Think it through, then end with COUNT: <number>.\n\n{{ params.text }}"
      parse: parse_count
      parse_retries: 2
    transitions:
      - target: check
        effect: |
          ctx.count = out
          ctx.counter = activity.instance_id
      - trigger: error
        target: failed
  check:
    do:
      call: check_count
      args: {count: "{{ ctx.count }}", expected_max: "{{ params.expected_max }}"}
    transitions:
      - target: done
        guard: out["ok"]
      - target: recount
        guard: else
        effect: ctx.problem = out["problem"]
  recount:
    max_visits: 2
    do:
      agent: stategraph_example_agent
      continue: "{{ ctx.counter }}"
      task: "Your count cannot be right: {{ ctx.problem }} Count again and end with COUNT: <number>."
      parse: parse_count
    transitions:
      - target: check
        effect: ctx.count = out
      - trigger: error              # loop_limit on the third recount, or the agent failed
        target: failed
  done:
    type: final
    output: {count: "{{ ctx.count }}"}
  failed:
    type: final
    status: failed
```

Mocks answer after parsing: `{"count": 4}` means `out` = 4. The `check` activity is a
`call` and runs for real in the test run.

---

## 3. Tool error → repair state

A tool that refuses its input raises `tool_failed` with the tool's message. Send the
message to an agent that repairs the input, then call the tool again -- bounded in the
guard of the error transition that enters the repair state. Not by `max_visits` alone:
the repair state is entered by an error transition, and a `loop_limit` raised while an
error transition runs ends the frame -- no transition catches it (design §3.5).
`max_visits` stays as the declared bound. A transient failure (a timeout, a busy
service) is better handled by `retry` on the tool activity itself.

```yaml
stategraph: 1
id: store_record
title: Store a record, repair it when refused
params:
  record_text: {type: string, required: true, description: A JSON object as text}
context:
  json_text: null
  tool_error: null
initial: prepare
states:
  prepare:
    entry: ctx.json_text = params.record_text
    transitions:
      - target: save
  save:
    do:
      tool: stategraph_json_manage_json
      args:
        operation: write
        doc: record
        if_exists: replace
        json_text: "{{ ctx.json_text }}"
      retry: {attempts: 2, backoff: 5s, errors: [timeout]}
      timeout: 30s
    transitions:
      - target: done
      - trigger: error
        guard: error.type == "tool_failed" and run.visits.get("repair", 0) < 2
        target: repair
        effect: ctx.tool_error = error.message
      - trigger: error
        target: failed
  repair:
    max_visits: 2
    do:
      agent: stategraph_example_agent
      task: |
        The store refused this JSON: {{ ctx.tool_error }}
        Fix it and answer with the corrected JSON only.

        {{ ctx.json_text }}
    transitions:
      - target: save
        effect: ctx.json_text = out
      - trigger: error
        target: failed
  done:
    type: final
    output: {stored: true}
  failed:
    type: final
    status: failed
    output: {problem: "{{ ctx.tool_error }}"}
```

Test the repair path: `mocks: {"save": {"$visits": [{"$error": {"type": "tool_failed",
"message": "invalid JSON at line 1"}}, {"status": "success"}]}, "repair": "{\"a\": 1}"}`.

---

## 4. Fan-out and join

`parallel` starts all branches at once and joins when all are done; `out` is
`{branch: out}`.

- `fail: fast` (default): the first failing branch cancels the others and is raised,
  with `error.branch`. Use it when every branch is needed.
- `fail: collect`: every branch runs to its end; `out[branch]` is
  `{"status": "succeeded", "out": …}` or `{"status": "failed", "error": …}`. Use it
  when some answers are better than none.

```yaml
stategraph: 1
id: three_views
title: Three views at once
params:
  question: {type: string, required: true}
context:
  views: {}
  missing: []
initial: ask
states:
  ask:
    do:
      parallel:
        optimist: {agent: stategraph_example_agent, task: "Argue for: {{ params.question }}"}
        skeptic: {agent: stategraph_example_agent, task: "Argue against: {{ params.question }}"}
        facts: {agent: research_worker, task: "Collect the facts behind: {{ params.question }}"}
      fail: collect
    transitions:
      - target: done
        guard: any(r["status"] == "succeeded" for r in out.values())
        effect: |
          ctx.views = {name: r["out"] for name, r in out.items() if r["status"] == "succeeded"}
          ctx.missing = sorted(name for name, r in out.items() if r["status"] == "failed")
      - target: failed
        guard: else
  done:
    type: final
    output: {views: "{{ ctx.views }}", missing: "{{ ctx.missing }}"}
  failed:
    type: final
    status: failed
```

Mocks per branch: `ask/optimist`, `ask/skeptic`, `ask/facts`. Each branch's agent
runs on its own session, so branches may set different `vars`.

---

## 5. Map over data

`map` runs one activity per item; `out` is a list in item order.

- `concurrency: 1` (default) runs strictly one after another in list order -- use it
  when order matters or a service must not be hit in parallel.
- `concurrency: n` runs up to n at once; the result order is still the item order.

```yaml
stategraph: 1
id: summarise_all
title: Summarise every document
params:
  documents: {type: array, required: true, description: "[{title, text}]"}
context:
  summaries: []
  failed_items: []
initial: summarise
states:
  summarise:
    do:
      map: params.documents
      as: doc
      concurrency: 3
      fail: collect
      each:
        agent: stategraph_example_agent
        task: "Summarise '{{ doc['title'] }}' (document {{ index + 1 }}) in two sentences.\n\n{{ doc['text'] }}"
    transitions:
      - target: done
        effect: |
          ctx.summaries = [r["out"] if r["status"] == "succeeded" else None for r in out]
          ctx.failed_items = [i for i, r in enumerate(out) if r["status"] == "failed"]
  done:
    type: final
    output: {summaries: "{{ ctx.summaries }}", failed: "{{ ctx.failed_items }}"}
```

Mocks per item: `summarise/0`, `summarise/1`, ... Items that are dicts use item access:
`doc['title']`. For a sequence where each step needs the previous result, use
`concurrency: 1` and a submachine per item whose params carry what it needs, or loop
over states with an index in `ctx` (pattern 10).

---

## 6. Human approval

A **wait state** (no `do`, no completion transition) takes declared events. Send them
with `stategraph_send_event(run_id, name, data)`. An internal transition (no `target`)
handles an event and keeps waiting. `timeout` raises `wait_timeout` in the state.

```yaml
stategraph: 1
id: publish_gate
title: Publish after approval
params:
  text: {type: string, required: true}
events:
  approve: {description: Publish it}
  reject:
    description: Do not publish
    data: {type: object, required: [reason], properties: {reason: {type: string}}}
  note: {description: A remark; keeps waiting}
context:
  notes: []
  reason: null
initial: awaiting
states:
  awaiting:
    timeout: 24h
    transitions:
      - trigger: note
        effect: ctx.notes.append(event.data)
      - trigger: approve
        target: approved
      - trigger: reject
        target: rejected
        effect: ctx.reason = event.data["reason"]
      - trigger: error
        guard: error.type == "wait_timeout"
        target: expired
  approved:
    type: final
    output: {published: true, notes: "{{ ctx.notes }}"}
  rejected:
    type: final
    status: failed
    output: {reason: "{{ ctx.reason }}"}
  expired:
    type: final
    status: failed
    output: {reason: no decision within 24 hours}
```

A test run stops in status `waiting` and lists what it accepts; then send `approve`.
Inside a submachine the wait works the same -- also inside a `parallel` branch or a
map item; name the `frame` when several frames accept the same event.
`machines/approval.yaml` adds a draft/reject loop.

---

## 7. Submachine reuse

A machine with `params` and final `output`s is a function: import it, call it with
parameters, as often as needed -- in sequence, per `parallel` branch, per map item.

```yaml
stategraph: 1
id: double_critique
title: Two critiques of one draft
imports:
  critique_round: ./critique_round.yaml
params:
  draft: {type: string, required: true}
context:
  gentle: null
  strict: null
initial: critique
states:
  critique:
    do:
      parallel:
        gentle: {machine: critique_round, params: {text: "{{ params.draft }}"}}
        strict: {machine: critique_round, params: {text: "{{ params.draft }}", strict: true}}
    transitions:
      - target: done
        effect: |
          ctx.gentle = out["gentle"]["notes"]
          ctx.strict = out["strict"]["notes"]
      - trigger: error
        guard: error.type == "submachine_failed"
        target: failed
  done:
    type: final
    output: {gentle: "{{ ctx.gentle }}", strict: "{{ ctx.strict }}"}
  failed:
    type: final
    status: failed
```

- Every instance is its own frame: its own `ctx`, steps and visit counts. Nothing
  leaks in or out except `params` and the final `output`.
- A mock on `critique/gentle` answers that whole submachine; `critique/gentle/critique`
  answers only the state `critique` inside it.
- A failed final inside raises `submachine_failed` with `error.data` = its output; an
  unhandled error inside raises it with `error.cause` = the inner error.
- A variant is a parameter (`strict`), not a copy of the file.

---

## 8. Shared error handling with a composite

An `error` transition on a composite handles every error inside it, inner states
first. Handle the specific cases on the state, the rest once on the composite.

```yaml
stategraph: 1
id: produce_article
title: Outline, draft, check -- one error path
params:
  topic: {type: string, required: true}
context:
  outline: null
  article: null
  problem: null
initial: production
states:
  production:
    initial: outline
    states:
      outline:
        do: {agent: stategraph_example_agent, task: "Outline an article on {{ params.topic }} in five points.", timeout: 5m}
        transitions:
          - target: draft
            effect: ctx.outline = out
      draft:
        do:
          agent: stategraph_example_agent
          task: "Write the article for this outline:\n\n{{ ctx.outline }}"
          retry: {attempts: 2, backoff: 30s}
        transitions:
          - target: produced
            effect: ctx.article = out
      produced:
        type: final
        output: {article: "{{ ctx.article }}"}
    transitions:
      - target: done                   # the composite completed: out = produced's output
        effect: ctx.article = out["article"]
      - trigger: error
        guard: error.type == "timeout"
        target: too_slow
        effect: ctx.problem = error.state + " took too long"
      - trigger: error                 # everything else from inside production
        target: failed
        effect: |                      # a block scalar: the code contains ": "
          ctx.problem = error.type + " in " + error.state + ": " + error.message
  done:
    type: final
    output: {article: "{{ ctx.article }}"}
  too_slow:
    type: final
    status: failed
    output: {problem: "{{ ctx.problem }}"}
  failed:
    type: final
    status: failed
    output: {problem: "{{ ctx.problem }}"}
```

`error.state` names the state where the error was raised. Entering `production`
again (e.g. from a retry state) resets the visit counts of everything inside.

---

## 9. One step, called many times: the v6 ritual

The writer v6 story design repeats one ritual for every step: a panel of agents works, its
delta is merged into the story document, keys are dropped, the step is recorded. As a machine
it is one submachine called once per step with parameters: `v6_ritual.yaml`, called by
`v6_story.yaml` (both in `src/plugins_writer/writer_pipeline_v6/machines/`). Its moves:

1. **Template vars per call.** `vars: "{{ {**params.vars, 'phase': params.phase, 'aufgabe':
   params.aufgabe} }}"` -- the v6 prompts branch on these names. The merge order (§11 of
   format.md) puts a submachine's own vars over its caller's, so every call sets its own
   `phase`.
2. **A marker line, parsed.** The panel ends its answer with one line such as
   `DELTA_DOC=<id> | STATUS_DOC=<id> | DELETE_KEYS=a,b`. `parse:` turns it into a mapping; a
   missing or broken line (a `ValueError` from the parse function) goes back to the same
   instance as feedback, `parse_retries` times.
3. **The store's error goes to a new agent.** The merge's error transition (guard
   `error.type in ("tool_failed", "issues_failed")`) goes back to the initial choice with the
   error text in `ctx`, and a fresh panel gets it in its task: in v6 a panel that closed its
   forum channel cannot act again, so no `continue`. The bound is `max_visits: 3` on the panel
   states (the first try and two store errors). The third store error enters a panel a fourth
   time and raises `loop_limit` while an error transition runs, which ends the ritual's frame
   (design §3.5); the caller's error transition around the ritual call takes it. Inside one
   machine, bound such a loop in the guard instead (§3).
4. **Repeatable store calls, decided per call.** The store calls run in a companion function
   through `sg.tool(..., idempotent=...)`: a read, a merge of the same delta, a write of a named
   document may be repeated by a resumed run; a delete by list index may not -- repeated, it
   would take the next element.
5. **A given result skips the agent.** An initial choice routes straight to the merge when
   the caller passes the marker itself.

The caller runs it per step, `do: {machine: ritual, params: {phase: synopsis, ...}}`, each call
with its own error transition.

Secrets a writer tool needs (`write_key`) come from the plugin's `inject_params`,
never from the machine. A step that creates something (a story row) is not
idempotent: give it an error path for `interrupted` that looks the object up by
`run.origin` before creating it again. `run.origin` is the id of the first run of a
fork chain, the same in a run and all its forks; `run.id` differs in a fork (see
`debugging.md` §6).

---

## 10. Small ones

**Initial router.** `initial` may name a choice:

```yaml
initial: route
states:
  route:
    type: choice
    transitions:
      - target: quick
        guard: len(params.text) < 2000
      - target: thorough
        guard: else
```

**Counter loop over states** (when each step needs the previous result):

```yaml
context:
  index: 0
  results: []
states:
  step:
    max_visits: 50
    do:
      agent: stategraph_example_agent
      task: "Continue the list. Previous: {{ ctx.results[-1] if ctx.results else 'none' }}. Item: {{ params.items[ctx.index] }}"
    transitions:
      - target: step
        guard: ctx.index + 1 < len(params.items)
        effect: |
          ctx.results.append(out)
          ctx.index += 1
      - target: done
        guard: else
        effect: ctx.results.append(out)
```

(The guard sees `ctx` before the effect runs.)

**Reading the outside world.** Never in a guard or template -- in a `call` or `tool`
activity, whose result is journaled and replayed:

```yaml
  load:
    do:
      tool: stategraph_json_manage_json
      args: {operation: read, doc: settings}
    transitions:
      - target: work
        effect: ctx.settings = out
```
