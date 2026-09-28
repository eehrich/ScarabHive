# ask_user

The model asks the person watching the run a question and waits for the
answer (F12 -- what Claude Code calls AskUserQuestion and Hermes `clarify`).
One tool, `ask_user`:

- a **question**, self-contained;
- optionally **2 to 4 options** -- the person clicks one, or writes an answer
  in their own words;
- optionally **multi_select** -- the person ticks several options;
- the **answer comes back as the tool result**.

A run nobody watches -- the OpenAI API, agent-run, a JSON `/run`, a job, a run
whose tab was closed -- cannot be asked: the tool returns **at once** with an
error that tells the model to decide itself and say what it assumed.

The machinery is shared with `tool_approval` (`agent_system.core.run_questions`
for the broker, the status row and the wait; `agent_system.api.question_routes`
for the answer route and who may answer; `syncQuestionActions` in
`static/js/chat_module.js` for the box in the chat).

## Switching it on

The shipped `config/plugins.yaml` loads the instance:

```yaml
ask_user:
  type: ask_user
  enabled: true
  config:
    ask_timeout: 300        # seconds a question waits -- the only limit on the wait
    reask_seconds: 20       # the question is sent again this often (a reloaded page gets it back)
    gone_after_seconds: 15  # a run nobody has read this long is not asked any more
```

An agent gets the tool through its allowlist: `ask_user/*` (the tool is named
like the instance, `ask_user`). Shipped with it: the agents a person works
with in the web chat on multi-step tasks, where a wrong guess costs a run --
`coder` (and `gamedev`, which inherits the coder's list), `sysadmin_agent`,
`research_agent`. `stategraph_author`: not enabled; the stategraph owners
decide. Not given: writer agents, and
sub-agents whose answer a coordinator reads (`research_worker` takes it away
again with `!ask_user/*`: its questions are its coordinator's to ask). Not
`skills_agent` either: the writer's `state_graph_agent` inherits its list.
`test_plugin_ask_user_config.py` pins that set against the real configuration.

The tool list is built per agent, not per run, so an agent that has the tool
is offered it in every run -- also in one nobody watches. It refuses there at
call time, with a text that makes the model decide itself (below). A per-run
tool list would change the tool schemas between runs of the same session and
break the prompt cache; one refused call costs less.

`tool_approval`'s shipped instance config allows `ask_user/*`: approving a
question before it is asked would ask the same person twice, and a run nobody
watches would get the approval's block instead of this tool's "decide
yourself". An agent that switches approvals on keeps that allow rule.

## Where the person is reached

The question is a **status line of the call's own row**, with `meta.ask_user`:
`{id, question, options, multi_select, request_id, session_id, agent,
asked_at, expires_at, answer_url}`. The web chat draws the question, a button
per option (boxes to tick with `multi_select`), a field for an answer in the
person's own words and a *Send* button. A click on an option sends it together
with what is typed in the field. The row's last line (answered / no answer /
cancelled / nobody reads the run) takes the box down in every tab showing the
run.

```
POST /plugins/ask_user/answer   {"question_id", "choices": [...], "text": "..."}
GET  /plugins/ask_user/pending  [?session_id=…&request_id=…]
```

`choices` are option texts as the question lists them; `text` is free text
(at most 4000 characters). At least one of the two. More than one choice only
with `multi_select`. An answer that does not fit is refused with 422 and the
question keeps waiting; one for a question that no longer waits gets 404.

**Who may answer** -- the same rule as tool_approval: the user the run
belongs to, or an admin, signed in with an access token (never an API key: a
program answering a person's question makes the question pointless). With
authentication off there is one user -- and anyone who reaches the API can
answer, the model's own HTTP tools included.

**Who can be asked at all** -- also tool_approval's rule
(`status_forwarding.attended_stream_of`): a run started by a client that
shows its questions to the person who started it (the web chat sends
`attended`), while a tab reads it, or a run under such a run (a sub-agent's
lines reach its caller's stream, and so does its question):

| Started by | Asked? |
|---|---|
| web chat (`/events`, `/run` with files), signed in or auth off, a tab reading the run | yes |
| a sub-agent or an agent called as a tool, under such a run | yes, in the caller's stream |
| the tab was closed (no reader for `gone_after_seconds`) | no; a waiting question stops |
| openai_api, agent-run, agent-cli, JSON `/run`, a job, the writer's dispatches | no |
| an async sub-agent after its caller's run ended | no |
| a script (tool_script), a state machine, a slash command | no -- see below |

**Only the model's own call asks.** The model's call comes through the agent
loop with the run's cancellation token and a status row of its own
(`call_with_status`). A call from a script, a state machine or a slash command
reaches the tool through `Agent.dispatch_tool_call` without the token -- and
its caller has a timeout of its own (tool_script's `per_call_timeout`, 60 s)
that would cut the question off. The tool refuses such a call at once, and
any call without its row.

## Waiting

The call waits until the first of:

- **the answer** -- the result is the answer;
- **`ask_timeout`** -- the result says no answer came;
- **the run's cancellation** (the chat's Stop cancels every token under the
  run) -- the call returns the framework's cancelled shape and the run stops;
- **nobody reads the run any more** -- the tab was closed and not reopened
  within `gone_after_seconds`;
- **the person writes in the chat** instead of the question's box -- into
  the call's own run, or into a run above it -- see below;
- **the run is torn down** while it waits (the task is cancelled) -- the row
  ends with *question cut off while waiting*, and the cancellation goes on.

The row **always ends** -- by the tool's own last line, or, cut off from
outside, by the shared helper before the cancellation propagates -- and the
question is closed before that line is written: an answer that arrives then
is refused (404), never taken for a question already settled.

### The tool's own timeout is the only one

A question may wait for minutes. What else could cut it off, looked up in the
code:

- **Tool calls have no framework timeout.** The agent loop polls a running
  call without a limit (`tool_execution.py`, "No hard iteration limit -- tools
  can run as long as needed"). The cancellation manager's force timeout
  (`tool_cleanup_timeout`, 30 s) starts only after a cancel.
- **`agent_watchdog`** judges every n *steps* and every n *thinking
  characters*, never by time; a waiting call adds neither.
- **Loop detection** counts repeated identical calls, not time. Asking the
  same question five times in a row gets blocked -- as it should.
- **LLM timeouts** (`request_timeout`, `stream_silence_timeout`,
  `llm_task_max_iterations`) run only while a model call streams.
- **Hook timeouts** do not apply: this is a tool, not a hook. (tool_approval's
  hook runs *before* the call; the shipped allow rule lets it through.)
- **tool_script's `per_call_timeout`** would apply to a call from a script --
  which is refused (above).
- **The sub-agent manager's `default_wait_timeout`** (3600 s) bounds a
  caller's `wait` operation, not the sub-run: a sub-agent's question keeps
  waiting, and the caller can wait again.
- **Jobs** (`background_job_manager`) have no run-time limit; the SSE stream
  sends keepalives, and the question itself is re-sent every `reask_seconds`.

So `ask_timeout` bounds the wait, nothing else does.

### When the person writes in the chat instead

A message typed in the chat's main field while the question waits is a
mid-run message of the run (`/events/{id}/append`). The wait notices it
within a second and ends (the row: *not answered here: the user wrote in the
chat instead*): the result tells the model a message came in, and the message
itself follows as the next user message, as every mid-run message does
(`SessionTracker.has_appended`). The same holds for a message that came while
the model was still thinking and that it has not read yet: the question ends
at once, and the model reads the message first.

**A sub-agent's question** is asked in the stream of the run the person
watches, and the person may answer it by writing to that conversation
instead. The message goes to the run above (the parent, which sits blocked on
the sub-agent), not to the sub-agent. So the question ends too, and the
sub-agent is told that the user wrote to the main conversation, that it does
not see the message, and to finish its task with what it has, say what it
assumed and return -- the parent then reads the message at its next step.
Which runs are "above" comes from the request-id ancestry, as with
tool_approval (a call is `<run>_<nnn>`, a sub-agent's run `<call>_sub_<id>`);
the runs are looked up in every agent's tracker
(`session_tracking.message_waits_for`), since the sub-agent runs on an agent
of its own. A message waiting for a run outside the chain ends nothing.

## Model Experience

### What the model sees

The tool description, verbatim (`schema.yaml`):

> Ask the user -- the person watching this run -- one question, and wait for the answer.
>
> Ask only when you cannot reasonably decide yourself: the request can be read in ways that lead to different results, the choice depends on the user's preference or on facts only they know, or a step is hard to undo. Do not ask to confirm what you were told to do, to report progress, or for anything your other tools can find out -- decide, and say what you assumed.
>
> Write a question that stands on its own: the user may not have followed the run. Where there are clear alternatives, give 2 to 4 short options; the user picks one (several with multi_select: true) or writes their own answer. Without options they answer in their own words.
>
> Returns {"status": "success", "choices": [...], "text": "..."}: choices are the options picked, exactly as you wrote them; text is what the user typed (may be empty). Returns status "error" when nobody can answer -- the run is not watched (an API call, a job), no answer came in time, or the user left. Then do not ask again: decide yourself, go on, and say in your reply what you assumed.

Parameters: `question` (string, required, at most 2000 characters), `options`
(2-4 strings, at most 200 characters each, distinct), `multi_select`
(boolean, only with options).

Results:

| Case | Result |
|---|---|
| answered | `{"status": "success", "choices": ["SQLite"], "text": "for now"}` -- choices in the order the question lists them |
| the user wrote in the chat instead | `{"status": "success", "choices": [], "text": "", "replied_in_chat": true, "note": "A message from the user came in while the question waited; it follows as their next message. Read it -- it may answer the question -- and go on from there."}` |
| a sub-agent's question, the user wrote to the main conversation | `{"status": "error", "reason": "replied_above", "error": "The user wrote to the main conversation instead of answering. That message goes to the agent that started you, and you do not see it. Do not ask again: finish your task with what you have, say in your reply what you assumed, and return, so that agent can read the message."}` |
| nobody watches | `{"status": "error", "reason": "unattended", "error": "Nobody can answer: no person is watching this run (it runs over the API, as a job, or the chat was closed). Do not ask again in this run. Decide yourself, go on, and say in your reply what you assumed."}` |
| the reader left while it waited | `{"status": "error", "reason": "gone", "error": "Nobody can answer any more: the person stopped watching this run while the question waited. Do not ask again in this run. Decide yourself, go on, and say in your reply what you assumed."}` |
| no answer in time | `{"status": "error", "reason": "timeout", "error": "No answer came within <n> seconds. Do not ask the same question again right away: decide yourself, go on, and say in your reply what you assumed -- the user can correct it."}` |
| not the model's own call | `{"status": "error", "reason": "not_own_call", "error": "ask_user answers only a call the model makes itself, in its own turn -- not one made from a script or by another tool. Call it directly."}` |
| cancelled | `{"error": "Tool 'ask_user' was cancelled.", "cancelled": true, "forced": false}` |
| bad arguments | `{"status": "error", "error": "<what to send instead>"}`, e.g. *options has 5 entries; give 2 to 4, or leave options out and let the user answer in their own words.* |

**Ask or decide?** The description draws the line: ask when the request is
ambiguous in a way that changes the result, when the choice is the user's
(preference, facts only they know), or before a step that is hard to undo.
Decide -- and say what was assumed -- for everything else, and always when the
tool says nobody can answer. A question that got no answer is not asked again
right away.

Nothing else reaches the model: the tool injects no message and changes no
prompt.

### Token and cache effect

- **Tool list:** the schema adds about 440 tokens (cl100k) to the tool list of every
  agent that allows it -- part of the cached prefix, changed once when the
  allowlist changes, not per run.
- **Per call:** append-only. The call and its result (a few dozen tokens)
  land at the end of the history; nothing earlier is rewritten. A reply in
  the chat instead arrives as the ordinary mid-run user message.
- **A long wait costs the cache.** The next model call after the answer comes
  when the person answers; providers keep a prompt cache only for minutes
  (Anthropic: 5 minutes by default, the length of the default `ask_timeout`).
  A question answered after that pays for the whole prefix again, once.

### Known gaps

- **Nothing is persisted.** Open questions live in the process; a restart ends
  the run that waits.
- **agent-cli chat cannot be asked.** A person reads the terminal, but the CLI
  neither shows `meta.ask_user` nor posts answers, so its runs are unattended
  and the tool refuses. Making the CLI a client that answers is its own piece
  of work (it would need the in-process broker instead of the HTTP route).
- **Another client** that shows a person the stream (a custom UI) must send
  `attended` itself and render `meta.ask_user`; `GET …/pending` serves one
  that missed the line.
- **An async sub-agent's question** (the parent runs on meanwhile) ends only
  if the message still waits for the parent's next step when the question
  looks (about once a second) -- the parent usually reads it first, and the
  question then keeps waiting. When it does end, it ends even for a message
  meant for the parent alone; the parent reads the message either way.
- **One question per call.** Several open questions of one run (parallel
  calls) each get their own row and box; the model is told to ask one thing
  per call.
