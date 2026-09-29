# Mid-Run User Message Injection

**Status:** Active
**Last Updated:** 2026-06-13

Users can send messages **while an agent run is in progress** (e.g. while tools
are executing or an LLM call is in flight). The running agent picks the message
up at its next step boundary and reacts to it — no cancel/restart required.
This mirrors the steering behavior of CLI coding agents.

## Behavior

1. User types into the chat while a run is active (`hasActiveRequest()`) and submits
   with the action button or Ctrl/Cmd+Enter. That button is a single slot driven
   by state (`updateActionButton()` in `chat_module.js`):

   | state | button |
   |---|---|
   | idle | **Run** |
   | running, input empty | **Stop** |
   | running, input has text | **Send** (tooltip "Send to running agent") |

   Clearing the input flips Send back to Stop. Before this, a run only ever
   showed Stop, which made the whole feature unreachable by mouse.
2. Frontend POSTs to `/events/{request_id}/append?fallback=none` instead of
   starting a new request.
3. The message is queued on the **owning agent instance** (resolved via the
   request's `BackgroundJob.agent_name` — not the default agent).
4. The agent loop drains queued messages:
   - **before each step** (before the next LLM call), and
   - **before finalizing**: if a message arrived while the LLM produced a
     text-only ("final") answer, the loop does NOT finalize — it continues so
     the next LLM call reacts to the new input. The interim answer stays in the
     history as a normal assistant message.
5. Messages that arrive **too late** to be answered (run already finalizing)
   are flushed into the session during `_finalize_request`, so they persist
   and are answered by the next run on the session — never silently dropped.

## API

`POST /events/{request_id}/append` — body `{"content": "..."}`

| Outcome | Response |
|---|---|
| Run active, message queued | `200 {"status": "appended", "request_id": ...}` |
| Run finished, `fallback=session` (default) | message appended to the persisted session (stored, but only answered by the next run): `200 {"status": "appended", "session_id": ...}` |
| Run finished, `fallback=none` | `404` — caller should start a new request with the message as task |
| `?session_id=...` query | append directly to a session (ownership-checked), ignores `fallback` |
| Session, die ein Lauf dieses Prozesses gerade hat (`?session_id=`, `fallback=session`, `POST /sessions/{id}/append`) | die Nachricht geht an diesen Lauf, auf dem Agenten, auf dem er läuft, auch ohne Job (`200`); nimmt er keine mehr an, weil er gerade abschließt, `409`. Neben den Lauf in die Session geschrieben, war sie „appended“ und beim nächsten Speichern des Laufs weg. |
| Session, die ein Lauf erst nach dieser Frage nimmt | der Append hält vom Lesen bis zum Speichern das Session-Lock des Agenten, dem die Session gehört (eine abschließende API-Runde vor dem Record, dann `agent_name` im Record, sonst der Einstiegs-Agent) — ein Lauf, der es hat, bekommt die Nachricht (`200`), sonst `409`. Es ist ein Schreiber-Lock: ein zweiter Append, ein startender Lauf, das Öffnen der Session (`open_for_run` liest und setzt selbst unter diesem Lock), eine öffnende oder zurücksetzende API-Runde warten den Augenblick ab (bis 5 s), statt abgewiesen zu werden; übernimmt danach ein Lauf, wird wer noch wartet sofort abgewiesen. Ohne das Lock schrieb der Append neben den Lauf, und eine abschließende API-Runde (openai_api) setzte die Konversation darüber zurück oder warf sie vor dem Speichern des Appends aus dem Speicher. |
| Session, die dieser Prozess gespeichert und losgelassen hat (eine abgeschlossene API-Runde tut das) | wird von der Platte gelesen und angehängt (`200`); vorher `404` für eine Konversation, die sichtbar da ist |
| Speichern schlägt fehl | `500`, und die Nachricht ist wieder heraus — im Speicher gelassen, schrieb sie der nächste Save doch, neben der Kopie, die ein Client nach dem Fehler erneut schickt |
| Request eines anderen Nutzers | `403` (Admins ausgenommen); ebenso Status und Cancel |
| Session, die der Lauf eines anderen Nutzers gerade hat | `403` — auch solange sie noch nicht gespeichert ist: den Besitzer nennt dann nur der Lauf |

The web frontend uses `fallback=none`: on 404 the message goes back into the
input, and sending it again starts a new request -- it is never stored unanswered,
and never stored twice.

## Frontend rendering

Nach einem angenommenen Append zieht `chat_module.js` den Lauf beim **nächsten
Schritt** in einen neuen Block unter der Nachricht um (`pendingAppendRebind`,
`rebindLiveBlock`: dasselbe Block-Objekt, das `handleSSEEvent` hält) — nicht sofort,
sonst risse der Schritt, der gerade streamt, in zwei Teile. Antwortet der Lauf ohne
weiteren Schritt, kam die Nachricht zu spät: die Antwort bleibt, wo sie ist, und
eine Notiz sagt, dass die Nachricht in der Session liegt und der nächste Lauf sie
beantwortet. Eine Notiz, die der Chat selbst schreibt (ein Befehl mitten im Lauf),
setzt denselben Umzug in Gang (`'note'` statt `'message'`), aber nie bei `final`.

## Key code

| Piece | Location |
|---|---|
| Append endpoint + agent resolution | `src/agent_system/app.py` (`append_event`, `resolve_agent_for_request`) |
| Queue + drain | `servers/agent/components/session_tracking.py` (`append_user_message`, `drain_appended_messages`) |
| Pre-step drain | `servers/agent/server.py` (`_execute_llm_loop`, step start) |
| Pre-final drain ("never finalize past fresh user input") | `servers/agent/server.py` (no-tool-call branch before final) |
| Late-message flush | `servers/agent/server.py` (`_finalize_request`) |
| Frontend append + block rebind | `static/js/chat_module.js` (submit handler) |

## Tests

- `tests/agent/test_agent_message_append.py` — queue consumption, pre-final
  drain (loop continues), late-message flush to session.
- `tests/app/test_app_append_agent_resolution.py` — owning-agent resolution
  with fallback to the default agent.
- `tests/app/test_app_append.py` — HTTP integration.

## Limitations

- Text only, and only once the run's `start` event has named it: a message with
  files, or one sent before that, waits in the composer, since a second run in
  the same session would be refused. An append that is not confirmed -- the
  server did not take it, did not answer, or answered 404 because the run has
  just finished -- puts the message back into the input, after anything typed
  meanwhile; sending it again appends it again, or starts a new run once the run
  has finished. Without an answer the message may have arrived after all, and
  sending it again delivers it twice.
- When a run's stream connection is lost, the chat says so and lets the run go,
  but the tab keeps it stored with its session: a reload follows the run again,
  checking the request status first (GET /events with an id the job manager no
  longer holds would start a new, empty run), and holds the composer meanwhile.
  Showing another session, New or `/new` lets the stored run go; the start page
  the tab falls back to when a session cannot be shown does not, but a message
  sent there starts a run that takes the stored run's place -- a tab stores one
  run. Leaving a session whose run is going cancels NOTHING: the chat lets go
  of the stream, the run keeps working, and coming back to the session asks the
  server whether one is going and joins it again. Only DELETING a session asks
  first, cancels its run, and waits for its stream to bring the cancel or end,
  and a choice made meanwhile -- a later pick, New, a message into the session
  -- wins. What is cancelled is the run the viewer was asked about: cut off
  while they were asked, it is cancelled all the same; past its answer or its
  cancel meanwhile, it is not -- a cancel would take its background sub-agents
  and session-end hooks along -- and neither is a stored run of an earlier
  message. A message sent in the session
  before a reload starts a new run, which the server refuses while the old one
  still holds the session -- the refusal keeps the stored run.
- A run saves its session at its end, and its request handler once more after
  it, so a delete could come before either. The server (SessionManager) never
  writes a session again that it has deleted, refuses a new run or a message to
  an idle session for it, and a delete cancels every run of the session the
  server process holds that has not answered yet -- another tab's too, which
  sees the run cancelled -- so no delete waits for a save. A run past its answer
  only finishes, its save refused: a cancel would take its background sub-agents
  along. The panel spares it for the same reason when it cancels before the
  delete, reading `answered` from `GET /api/sessions/active`. A message
  appended to a cancelled run in the moment before its cancel takes is lost
  with it. Once a delete is past its questions,
  the session opens no more, and a message into it -- shown again by a load that
  was on its way -- waits in the composer. The server forgets a deleted session
  when it restarts, or when another process (agent-cli, a woken run) writes the
  session again; without session presence a run of the server still going may
  then save over it, as two processes on one session do anyway.
- ⚠️ **The CHAT's load of a session and the DELETE of it are the same URL, and
  changing that changes which of two paths the delete takes.** `remove()` handles
  both -- a load that has already answered (the session is open, so the chat lets
  go of it) and one still on its way (it is discarded by the loading counter) --
  but which of them runs is decided by whether the browser serialises the two
  requests, which it does for the same URL; the comment in `sessions.js` calls
  that out. Measured, not reasoned: adding `?descendants=false` to the chat's GET
  flipped it to the second path and failed the test that pins the first. What was
  NOT established is the mechanism (connection reuse would explain it just as
  well) or that the second path is wrong -- only that a query parameter on that
  GET silently changes observable delete behaviour, while looking harmless.
  That is why `descendants` on `GET /api/sessions/{id}` is off by DEFAULT and the
  Session Info panel asks for it, rather than the chat asking to do without.
  The rule is about the chat's load: the panel loads the same session at
  `?descendants=true` and that is fine, because nothing it renders ever opens or
  restores a session.
- Stop, or deleting the session, asks the server to cancel the run; the run's
  stream still brings its end, and a message sent meanwhile waits in the
  composer -- the stopping run would save it unanswered. That holds even when
  the answer to the ask failed or went missing, since the cancel may have been
  taken all the same; the tab remembers the ask across a reload, and across
  leaving the session and coming back. Deleting before the run's start has
  named it cancels nothing, and the chat says so.
- A run's own `final` or `cancelled` event ends its answer: the controls go
  idle, with nothing left to stop, a message sent after it waits in the composer
  as well, a reload no longer follows the run -- it shows the answer from the
  session the run saves -- and a stream that stops afterwards is no lost
  connection. Leaving the session then asks nothing: the chosen session takes
  the chat at once, and the chat follows the stream no more -- it would only
  wait for the run's save and session-end hooks. The stream still reads on to
  its end, its events ignored: closing it would cut the end of its request
  short -- a run with files saves in its request, and a message's request saves
  once more and releases the run after it. A stream that ends so loads the
  session list again, which the save has changed; one that breaks does not. Only those events, `end`, or the
  server closing its stream end a run for the chat: a stream that breaks before
  them is a lost connection -- for a run followed again after a reload, whose
  EventSource does not tell a closed stream from a broken one, any stop before
  them is.
- A connection that dies without the browser noticing (no reset) leaves the chat
  following it until the browser gives up on the read; a reload recovers.
- Only a run started as a background job (a text message) is stored: a run with
  files runs inline in its request, a reload does not follow it, and it leaves
  the stored run alone. Files attached while it starts stay attached, and a
  refused run keeps its files.
- A message arriving in the microseconds between the pre-final drain and
  request unregistration is flushed to the session (persisted, answered next
  run) rather than answered in the same run.
- If a tool replaced the history via `set_compacted_messages` in the same
  instant the run finalizes, the late-flush can lose against the compacted
  snapshot (edge case, message still acknowledged by the API).
