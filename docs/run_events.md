# Run events

What a panel's agent run tells a person while it works, so that nobody wonders
whether anything is happening: the events, their store, the stream and the
reporter, and the places in the panels and the shell that draw them (see
"Where a person sees them").

## The pieces

| Piece | Where | Does |
|-------|-------|------|
| `RunReporter` | `agent_system/run_events.py` | The one way to write a run's events: `started`, `step`, `heartbeat`, and exactly one end (`done`, `failed`, `cancelled`, `handed_over`, `review_ready`). `keep_alive()` sends a heartbeat whenever the run said nothing for 5 s. Never raises into the run. |
| `RunEventStore` | `agent_system/run_events.py` | `agent_runs` (each run as it stands) and `agent_run_events` (its timeline) in `data/run_events/run_events.db`. An event is kept before it is sent. The newest 500 runs are kept. |
| `RunEventBus` | `agent_system/run_events.py` | Hands each event to every open stream. |
| Turns | `agent_system/core/turn_runs.py` | Every turn of an agent is a run, under the id of the session it works in: a chat's turn (`source` `chat`) and, below it (`parent_session`), the sub-agents and machines it called (`sub_agent`). Its steps are the model thinking and the tools it calls; see "Turns" below. |
| Panel runs | `agent_system/plugins/agent_tasks.py` | `launch()` reports `started`, keeps the run alive and reports its end from how `/run` answered. Every AI task of every panel gets this without changes. |
| Steps | a hook plugin | A `pre_tool_call` hook can turn a tool call into a step in plain words (`reporter_for(session_id).step(...)`). |
| Routes | `api/run_event_routes.py` | See below. |

## States

`queued` → `working`; `working` ends in `done`, `review`, `failed` or
`cancelled`; `review` → `done` once its person checked it; `working` →
`needs_input` while the agent waits for its person's answer, and back to
`working` with `run.answered`. A run **needs its
person** (`needs_you`) while it asks (`needs_input`), is in `review` (a result to check) or ended
`failed`/`handed_over` and nobody has seen it yet; `POST .../ack` settles both
(a checked result becomes `done`, a seen failure stays `failed` and stops
asking: `run.acknowledged`). A run at work (`queued`, `working`) that said
nothing for 30 s is shown `silent: true` (its process may be gone or hang).

Which panel runs end in `review`: an AI task whose work a person checks is
declared `AgentTask(..., review=True)`, for example an assessment a person
confirms.

Which panel runs ask: an agent allowed `ask_user/*` (one that decides
on facts only the person knows). In a panel run nobody
reads a chat row, so `ask_user` writes `run.needs_input` (payload: `question`,
`options`, `multi_select`, `question_id`, `answer_url`, `wait_s`), waits up to
`panel_ask_timeout` (30 min) for `POST /plugins/ask_user/answer` and then writes
`run.answered` (`outcome`: answered, timeout, cancelled, gone). While a run
waits, `GET /api/agent-runs` and the stream's snapshot carry its `question`.

## An event

```json
{
  "v": 1, "event_id": "evt_18a3c…", "run_id": "a1b2c3", "seq": 4,
  "ts": "2026-10-31T20:41:05.120Z", "type": "run.step",
  "agent": "code_analyst", "source": "code_audit", "task": "assess",
  "subject": {"title": "Assess the issues"}, "parent_session": "",
  "state": "working",
  "step": {"index": 2, "total": null, "key": "read_code",
           "label": "Reading the code behind a finding",
           "labels": {"de": "Liest den Code hinter einem Befund"}},
  "progress": null, "eta_s": null,
  "touching": ["F-002"], "payload": null
}
```

`seq` has no gaps among the kept events of a run: a client that sees one
reads the missing events from `/events`. A heartbeat (`run.heartbeat`, every
5 s while a run says nothing else) is sent but not kept: it repeats the step
the run is on and the run's last `seq`, and only tells that the run is alive.
`total` and `progress` are only set when the agent's steps are known ahead
(`plan`).

Each run names the process that reports it. When the store is opened by a later
process (a restart, a crash before it), every run that was still queued, at
work or asking and whose process is gone is ended as `run.failed` ("interrupted:
the process that ran it has ended"): it needs its person until they see it,
and none stays at work for good -- except a background run (below): its session
shows how it stands and its starter may take it over, so it needs nobody. A
server that stays up does the same every 30 s while a stream is open
(`SWEEP_S`): an agent-cli closed mid-turn says no end.

A process is looked for only where its id counts: the owner names its host
and, on Linux, its pid namespace. A run held in another container or on
another host -- a server in a container, an agent-cli beside it on the same
data directory -- is ended once it gave no sign of life for five minutes
(`ORPHAN_S`), never on what a process list here says.

## Routes

Each person reads their own runs (auth off: everyone is `local`).

| Route | Answers |
|-------|---------|
| `GET /api/agent-runs?limit=20` | The runs, those not ended first. |
| `GET /api/agent-runs/{run_id}/events?after_seq=0` | The run and its events after `after_seq`. |
| `POST /api/agent-runs/{run_id}/ack` | The person checked the result or saw the failure (409 when the run waits for nobody). |
| `POST /api/agent-runs/{run_id}/cancel` | Stop a run at work or waiting for its person's answer: through its own request id, as `/api/requests/{id}/cancel` stops any run; its end comes as `run.cancelled` with `payload.by`. A background run is stopped by whoever started it (its `on_cancel`), and ends as `run.cancelled` without `payload.by`. 409 "This run has ended" for one that has; 409 with the header `X-Run-Held-Elsewhere: 1` when another process holds it (an agent-cli's turn), with `X-Run-Takes-No-Stop: 1` when this server holds it and has nothing to stop it by. |
| `GET /api/agent-runs/text?width=100` | The runs as a terminal reads them (`run_tree.render`), as one text: what `/runs` shows in the chat. |
| `GET /api/agent-runs/stream` | Server-Sent Events: first `{"type": "runs.snapshot", "runs": [...]}`, then each event as one `data:` line, a kept one with `id: <event_id>` (a heartbeat has none, so `Last-Event-ID` stays at the last kept event). A reconnect with `Last-Event-ID` (the browser sends it) reads on from there instead. A comment every 15 s keeps the connection open. |

## Where a person sees them

| Place | Shows | Code |
|-------|-------|------|
| The shell's session pane, above the sessions | What works now, before any session is picked: every run at work as a tree -- a turn, the sub-agents below it, theirs below them --, each with its agent, what it is about, how long, and the step it is on; a warning for one silent for 30 s. Under it what needs the person (a question, a result to check, a failure not seen -- with why, the one that came to wait last first) and the six runs that ended within the last hour, the sub-agents not among them. A click opens a run where it is followed (a turn in its session, a panel's task in its panel, work that outlived its turn in the sub-session it is kept in -- read along as it goes, the call at hand included: the server hands out what the run holds, not only what it has written (`background_runs.kept_so_far`); in its caller's chat its box hangs at the call that started it, which may be many turns up) and marks a failure seen; Stop ends a run at work, a sub-agent's alone that sub-agent -- one another process holds says so and offers none (`X-Run-Held-Elsewhere` on the 409). While the list shows, "All sessions" folds the sessions away below it, as the person left it. Hidden while there is nothing to say. | `static/js/shell/activity.js`, `static/js/shell/shell.js` (`openRun`) |
| The chat, a run's session opened by itself | A sub-agent's run streams to the turn that started it; its session opened from the tree is read again as the run's events say it took a step, and once more at its end (`chat_module.js` `openRunMoved`). Read again, not picked: nothing closes for it and what the viewer chose for the next message stays. Not under a viewer who scrolled up or unfolded something, nor over a command's answer that stands in the chat alone: what they miss is offered at the chat's end ("New steps") and read when they take it or scroll back there. The session names the one it was called from and leads back; the box of a run read back in its caller's chat, and of one that goes on after its turn, offers its session by itself. |
| A terminal | `agent-cli runs` prints the same reading -- at work as a tree, needs you, just ended -- of every user's runs; `--watch` stays and follows. In a chat, `/runs` (`/ps`, `/tasks`): the terminal prints it, the browser's chat shows the asker's runs in the same words (`GET /api/agent-runs/text`). | `agent_system/run_tree.py`, `cli_utils/commands/runs.py` |
| The shell's head, on every tab | A pill while agents work: "2 agents at work", the newest one's step in plain words, a breathing dot; a warning badge for runs silent for 30 s. Hover lists every run; a click opens the newest where it is followed (a panel's task in its panel, a turn in its session). Hidden while none works. While the tree above the sessions shows the same runs, the pill shows only its warnings ("1 quiet", "1 needs you"): the count and the step are out of sight there, still told to a screen reader, and shown again under the keyboard's focus or with the sessions closed. The tree lists every run the pill counts -- runs that name each other as the session they work below stand on their own. | `static/js/shell/agent_runs.js`, `static/css/shell.css` |
| A panel's "Agent runs" button | A breathing dot instead of the icon while one of its runs works; its tooltip names the step. | `static/kit/ai-tasks.js` |
| A running run in the "Agent runs" sheet | "Step 2 · Reading the findings" in the page's language (`labels`), the running time, a progress bar when the total is known, "taking longer than usual" after 2 min, a warning after 30 s without a sign of life. | `static/kit/ai-tasks.js` |
| An open run in the sheet | Its steps as a timeline: done ones ticked with their time, the current one marked. | `static/kit/ai-tasks.js` |
| A run at work in the sheet | "Stop" (asks first). | `static/kit/ai-tasks.js` |
| A run that needs its person, in the sheet | "Done -- please check it", where to look in the panel, "Checked"; or "Handed over to you", "Seen". Its badge reads "needs you"; the runs button counts them. | `static/kit/ai-tasks.js` |
| A result to check, in the sheet | A task a person checks (`AgentTask(review=True)`) asks the agent for three sections in the panel's language (`### Changed`, `### Could go wrong`, `### Checked`; `review_note()` in `agent_tasks.py`). When all three are there, the sheet shows them as three cards instead of the raw answer, and "Ask first" puts the cursor in the reply box. | `static/kit/ai-tasks.js` (`reviewParts`) |
| A question of the agent, in the sheet | The question, its options (radio, or checkboxes where several fit), a field for own words, how long it waits; "Answer" posts to `answer_url`. What is picked and typed survives the redraws. The badge reads "has a question". | `static/kit/ai-tasks.js` (`questionView`) |
| The shell's pill | "1 needs you" beside the agents at work; a click leads to the run that needs the person first. A bell beside it asks once to allow notifications; then a run that comes to need its person (or ends) while they look elsewhere is told as a system notification. A click on a background run opens its session in the chat, and marks a failed one seen. | `static/js/shell/agent_runs.js`, `static/js/shell/shell.js` |
| The chat, under the call that started a background run | Its box with its steps so far (read from its sub-session once it is in view), the step it is on, "Stop" while it works, its answer or why it ended -- after the turn that started it has ended too. The session pane's activity tick asks `/api/sessions/active` for it (`background`). | `static/js/chat_module.js` (`showBackgroundRuns`) |

Both follow the stream through `static/kit/run-events.js`: ONE `EventSource`
for every page of the origin, held by the SharedWorker
`static/kit/run-events-worker.js` -- the shell, its panel frames and a panel
in a tab of its own. A stream holds one of the browser's six HTTP/1.1
connections per host for good: with one per page, the shell and five panels
left none, and a panel opened in a new tab got its page but no data. Without a
SharedWorker the panels framed in a tab share the shell's stream. A page that
joins gets the runs as they stand (`GET /api/agent-runs`), the last one to let
go closes it, one the browser gave up for good is opened anew, and a page the
back/forward cache brings back follows again. The stream is the account's that
opened it: the worker asks `/auth/me` before it lets a page in, and when
someone else is signed in now (a log-out in one tab while another stays open)
it opens the stream anew -- one account's runs never reach the next. The
browser reconnects and sends `Last-Event-ID` by itself. The sheet keeps
asking its plugin as before, so it works without the stream too; the stream
makes a step appear the moment it is said and tells an end at once. The
running time is written into its place every second and never redraws the
sheet: an open fold or a selection stays.

## Background runs

Work a tool call starts that goes on after the turn that called it (a coding
CLI) is a run too: `agent_system/core/background_runs.py` (`BackgroundRun`)
reports it with `source` and `task` `background`, under the id of a
sub-session of the calling session that holds its steps as an agent's run is
stored, and with that calling session as `parent_session`. The parent lists
it in `metadata.background_runs`. `RunEventStore.background(parents, who)`
answers the runs still held below given sessions.

Only a process that keeps sessions (the server) writes the sub-session. One
that keeps none and takes the work over (a CLI, while the server was away)
keeps the run said at work: no new steps in its box, and no Stop -- a Stop
reaches a run through the process that holds it, and `/api/sessions/active`
says per run whether this server does (`stoppable`). The end of such a run
is written by the server: whoever started the work leaves it on disk until a
process that keeps sessions has ended its box.

## Turns

What works now, on which step, below whom -- for every agent, not only a
panel's: `core/turn_runs.py` tells each turn as a run where the agent's turn
begins and ends (`servers/agent/mixins/run.py`; a machine addressed as an
agent: `plugins/stategraph/facade.py`).

- **Its id is its session's**, as a panel run's is. A session's next turn
  begins the same run again: its clock starts over, it is named after the
  first line of the new question, and of its events the newest 300 stay.
- **`parent_session`** is the session whose turn started it: a sub-agent of
  the sub-agent manager, an agent called as a tool, an agent a machine's step
  runs. The runs below a chat's turn are a tree by it.
- **Steps**: `Thinking` while the model works, then the tools of its answer
  in one line -- the first call's server, tool and what it is called on, and
  `(+2)` for two more beside it. A machine's step is the state it stands in.
- **Its end** is how the turn ended: done with its answer, cancelled (a stop,
  a reader that left), failed with its last error. A failure needs its person
  only where nobody saw it: not one in a chat somebody watches as it ends,
  and not a sub-agent's -- the turn that called it is told. Written by a
  thread of its own (`RunReporter.end_detached`): a turn ends in the
  `finally` of a task that may be being cancelled. The session's next turn
  waits for it.
- **Which**: a turn a person started -- one that is attended
  (`request_context.run_is_attended`: a chat in the browser or at the
  terminal, a session the server woke) -- and whatever runs below a run that
  is told, a panel's task included. A session woken as a process of its own
  (`agent-cli run --woken`, where the wake comes from outside the server) is
  its person's as well, though nobody is at that process: it tells every turn
  it runs (`report_turns(every=True)`). What a program sends the server (a
  worker's job, a call through the OpenAI API, `/run` without a person) and
  what runs below it tells nothing. The process says at its start that it
  tells at all (the server, agent-cli); `AGENT_TURN_RUNS=0` silences a
  process, `=1` makes any process tell every turn.
- A session whose run is somebody else's starts no second one: a panel's
  task at work there, or one that waits for its person (a result to check, a
  failure not seen) -- a chat in that session is not what they wait for. A
  turn's reporter is not found by `reporter_for`: `ask_user` and a hook that
  names a panel run's steps speak through a panel's run only.
- **Stop**: `POST /api/agent-runs/{id}/cancel` stops a turn as the chat's
  own Stop does (its request, its sub-requests with it); a sub-agent's run
  alone ends that sub-agent, and the turn that called it goes on.

Turns and the other runs are counted apart (the newest 500 ended ones of
each): a day of chat turns does not push out a panel run that waits for its
person. A run not ended is never pruned.

The list a page starts with (the 60 first of `GET /api/agent-runs`, the
stream's snapshot) reads: the runs not ended, the newest started first; then
those that ended and still wait for their person; then the ones that ended
last. A sub-agent that ended and needs nobody is not in it -- nothing shows
it, and a turn that called sixty of them would be cut from its own list. A
row that waits says why: `question` and `asked` (since when) for one that
asks, `reason` for one that failed or was handed over.

A reader keeps what it heard: an event older than what it knows of a run
(`seq`) changes nothing, and a list that arrives after an end does not take
that end back (`static/kit/run-events.js` `liveRun`, `activity.js`). A stream
sends none such either: a step kept as its turn ended is read from the store
after the end went out, and is left out. `run.queued` and `run.started` are
the exception -- a run that begins under an id the store had let go of
counts from one again.

A process that only looks opens the store without ending anybody's runs
(`run_event_reader()`, `RunEventStore(sweep=False)`).

The stream hands out what this process says through its bus. What another
process wrote -- an agent-cli's turn -- it reads from the store every 2 s
(`TAIL_S`), with a sign of life for each run at work that process holds
(`RunEventStore.beats`), so a run held elsewhere is not shown silent.

## Writing events for a new kind of run

```python
from agent_system.run_events import RunReporter

reporter = RunReporter(run_id, "inventory_agent", who=user, title="Inventory of the code", source="inventory_scan")
await reporter.started(plan=["Reading the build files", "Resolving versions", "Writing the inventory"])
reporter.keep_alive()
await reporter.step("Reading the build files", touching=["pyproject.toml"])
...
await reporter.done("142 components")
```

Labels speak to someone who does not know the tool, in the present tense, at
most 80 characters.
