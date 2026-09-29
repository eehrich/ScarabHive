# stategraph_machine

One [stategraph](../stategraph/README.md) machine, addressed as an agent. A SAM spawns it,
AgentCaller calls it, writer_jobs' `/events` dispatches to it -- exactly as to any agent.
It runs its configured machine through the `stategraph` instance (so the panel shows,
pauses and terminates these runs) and answers with the run's output as a JSON object.
Design: `docs/stategraph_design.md` §10.

## Setup

**In the machine's own file -- no config entry.** An `agent:` block offers the machine as an
agent; every process that starts (the API, agent-cli) declares it (`Runtime.declare`, through
this type's `offered_servers`), for each enabled stategraph instance whose machine folders hold
the file:

```yaml
agent:                       # {} takes every default
  name: helper_agent         # default <machine id>_agent
  description: "..."         # default the machine's title or description
  input: text                # default text for one string param, else json
  task_param: topic          # default that one param
  on_wait: ask               # default ask
  params: {}                 # literal params, under the ones from the message
  promote: []
  visibility: tool           # default private: only callers that name it
```

The block is the entry below, with `type`, `machine` and `stategraph` filled in (and
`from_machine_file: true`). A new or changed block takes effect when the process starts next (the
panel says whether it is declared); a config reload keeps it. A name a config entry holds stays the
config's -- validation warns (SG111), also for an entry of this machine pasted into a config file:
its settings would run, not the block's; the machine itself runs -- and a file that does not parse
offers nothing. Of stategraph instances sharing a machine folder, the first (by name) offers it.

**Or as a config entry:**

```yaml
plugins:
  servers:
    v6_story_machine:
      type: stategraph_machine
      enabled: true
      description: "Story design as a stategraph machine; answers {story_id, ...} as JSON"
      machine: v6_story          # the machine id -- never taken from the message
      stategraph: stategraph     # the StateGraphServer instance (default: stategraph)
      input: text                # text: the message becomes params[task_param]; json: it IS the params
      task_param: task
      params: {}                 # literal params, under the ones from the message
      promote: [story_id]        # output keys copied onto the final event's top level
      on_wait: block             # ask: a wait state asks in the conversation (below); block: the request waits
      metadata:
        visibility: tool         # tool: a SAM offers it to its LLM; both: the chat's agent list too
```

The keys sit next to `type`, not under `agent_config` (which forbids unknown keys).
**Without `metadata.visibility` the agent is private:** a SAM leaves it out of the agents its LLM
may start, even when `allowed_agents` names it, and the chat does not list it -- only callers that
address it by name (writer_jobs, AgentCaller, `agent-cli --agent`) reach it. `v6_story_machine`
is private on purpose. The stategraph instance checks every machine agent over it shortly after
its start and logs what keeps one from running (a missing or broken machine, a `task_param` the
machine does not declare, a required param nothing passes). The
machine's agents get their prompt vars from the machine (`vars`, `vars_from`), not from the
caller's session. The run's own session (`sg_<run id>`, with its agents' sessions below it)
sits below the facade's session, and its agents count one sub-agent level below the facade:
a SAM's `max_nesting_depth` bounds them as if the facade had spawned them.

## Behaviour

| Situation | What happens |
|---|---|
| a message | one run, run key = `<agent>:<request id>` (the user's), run id = `<request id>_sg<n>` (cancel, status lines and cost stay under the caller) |
| the same request again | attaches to its live run, resumes it after a crash, or -- once it ended -- answers its outcome again: the output, the failure, the cancel. Only a transient failure (`interrupted`, `timeout`, `agent_failed`, `decision_failed`, `internal`, `diverged` as the error or an unhandled cause) starts a new run (design §5.7); another user's request id gets nothing |
| a `continue` of the instance | answers the output again (succeeded), resumes (interrupted), or refuses (failed, cancelled) -- in any process: the session's run is kept in runs.db |
| a call as another agent's tool | a request of its own each time (its own session, a request id below the caller's) |
| a request cancelled before its run started | answers `cancelled`, starts nothing |
| a cancel of the request or of any request above it | the run is terminated; its `finally` activities run -- within 10 s: then the platform force-cancels the caller's request tree, their tool calls and agent runs included; the answer is `cancelled` |
| the process stops, or the client goes away | the run is left alone and ends `interrupted`; the next dispatch resumes it |
| the machine fails | an `error` event (never a `final`: writer_jobs counts any `final` as success) |
| the run waits for an event, `on_wait: ask` | the turn ends with a question: the waiting state (and its `description`), the events it takes with their descriptions and data schema, how to reply. The `final` event carries `waiting: {states, events}`. The next message in the session is the answer: the event's name (`approve`, case does not matter; its data may follow it, after a space, a colon or a line break), or `{"event": "reject", "data": {...}}`; where several frames (parallel submachines) take the event, the question names them and the answer names one with `"frame"`. One the run does not take gets the question again with the reason (`Not sent: ...`), one a guard discards with the guards it met (`Not taken: ...`); a taken one runs the machine on to its end or its next wait. After a restart -- or from agent-cli, a process per message -- the answer resumes the run into its wait and answers it there. Called as another agent's tool the facade has no conversation to ask in: a wait blocks there |
| the run waits for an event, `on_wait: block` (default) | the request waits until the run ends; the event comes from elsewhere (the panel, `send_event`). writer_jobs needs this: it takes any answer for the result |
| a message that does not fit the params | the error names what the machine takes: `it takes: task (string, required) -- ...` |

## Model Experience

A caller sees an agent that takes one message and, when the machine has finished, answers
with a single JSON object -- the machine's output, e.g. `{"story_id": 812, "title": "…"}`
(a non-object output comes as `{"output": …}`). A follow-up does not reach an LLM: it gets
the same answer again -- except, with `on_wait: ask`, while the run waits: then the answer
is a question, and the follow-up is the event that answers it.

The State Graph panel shows, for each machine, the machine agents that run it (their
visibility, `on_wait`, and what keeps one from running) and a ready entry to add one. Errors arrive as the SAM's usual
`Error: …` with the run id, so the run can be inspected in the State Graph panel.
