# stategraph_machine

One [stategraph](../stategraph/README.md) machine, addressed as an agent. A SAM spawns it,
AgentCaller calls it, writer_jobs' `/events` dispatches to it -- exactly as to any agent.
It runs its configured machine through the `stategraph` instance (so the panel shows,
pauses and terminates these runs) and answers with the run's output as a JSON object.
Design: `docs/stategraph_design.md` §10.

## Setup

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
```

The keys sit next to `type`, not under `agent_config` (which forbids unknown keys). The
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

## Model Experience

A caller sees an agent that takes one message and, when the machine has finished, answers
with a single JSON object -- the machine's output, e.g. `{"story_id": 812, "title": "…"}`
(a non-object output comes as `{"output": …}`). There is no conversation: a follow-up does
not reach an LLM, it gets the same answer again. Errors arrive as the SAM's usual
`Error: …` with the run id, so the run can be inspected in the State Graph panel.
