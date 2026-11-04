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
  "subject": {"title": "Assess the issues"}, "state": "working",
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
and none stays at work for good.

## Routes

Each person reads their own runs (auth off: everyone is `local`).

| Route | Answers |
|-------|---------|
| `GET /api/agent-runs?limit=20` | The runs, those not ended first. |
| `GET /api/agent-runs/{run_id}/events?after_seq=0` | The run and its events after `after_seq`. |
| `POST /api/agent-runs/{run_id}/ack` | The person checked the result or saw the failure (409 when the run waits for nobody). |
| `POST /api/agent-runs/{run_id}/cancel` | Stop a run at work or waiting for its person's answer: through its own request id, as `/api/requests/{id}/cancel` stops any run; its end comes as `run.cancelled` with `payload.by`. 409 when no run of this server works there. |
| `GET /api/agent-runs/stream` | Server-Sent Events: first `{"type": "runs.snapshot", "runs": [...]}`, then each event as one `data:` line, a kept one with `id: <event_id>` (a heartbeat has none, so `Last-Event-ID` stays at the last kept event). A reconnect with `Last-Event-ID` (the browser sends it) reads on from there instead. A comment every 15 s keeps the connection open. |

## Where a person sees them

| Place | Shows | Code |
|-------|-------|------|
| The shell's head, on every tab | A pill while agents work: "2 agents at work", the newest one's step in plain words, a breathing dot; a warning badge for runs silent for 30 s. Hover lists every run; a click opens the panel that started the newest. Hidden while none works. | `static/js/shell/agent_runs.js` |
| A panel's "Agent runs" button | A breathing dot instead of the icon while one of its runs works; its tooltip names the step. | `static/kit/ai-tasks.js` |
| A running run in the "Agent runs" sheet | "Step 2 · Reading the findings" in the page's language (`labels`), the running time, a progress bar when the total is known, "taking longer than usual" after 2 min, a warning after 30 s without a sign of life. | `static/kit/ai-tasks.js` |
| An open run in the sheet | Its steps as a timeline: done ones ticked with their time, the current one marked. | `static/kit/ai-tasks.js` |
| A run at work in the sheet | "Stop" (asks first). | `static/kit/ai-tasks.js` |
| A run that needs its person, in the sheet | "Done -- please check it", where to look in the panel, "Checked"; or "Handed over to you", "Seen". Its badge reads "needs you"; the runs button counts them. | `static/kit/ai-tasks.js` |
| A result to check, in the sheet | A task a person checks (`AgentTask(review=True)`) asks the agent for three sections in the panel's language (`### Changed`, `### Could go wrong`, `### Checked`; `review_note()` in `agent_tasks.py`). When all three are there, the sheet shows them as three cards instead of the raw answer, and "Ask first" puts the cursor in the reply box. | `static/kit/ai-tasks.js` (`reviewParts`) |
| A question of the agent, in the sheet | The question, its options (radio, or checkboxes where several fit), a field for own words, how long it waits; "Answer" posts to `answer_url`. What is picked and typed survives the redraws. The badge reads "has a question". | `static/kit/ai-tasks.js` (`questionView`) |
| The shell's pill | "1 needs you" beside the agents at work; a click leads to the run that needs the person first. A bell beside it asks once to allow notifications; then a run that comes to need its person (or ends) while they look elsewhere is told as a system notification. | `static/js/shell/agent_runs.js` |

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
