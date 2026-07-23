# Mid-Run User Message Injection

**Status:** Active
**Last Updated:** 2026-06-13

Users can send messages **while an agent run is in progress** (e.g. while tools
are executing or an LLM call is in flight). The running agent picks the message
up at its next step boundary and reacts to it — no cancel/restart required.
This mirrors the steering behavior of CLI coding agents.

## Behavior

1. User types into the chat while a run is active (`streamActive`) and submits
   with the run button or Ctrl/Cmd+Enter. The run button **stays visible during
   a run** (tooltip: "Send to running agent") and the Stop button appears
   *alongside* it — not instead of it. Hiding the run button while running made
   the whole feature unreachable by mouse.
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

The web frontend uses `fallback=none` and falls back to a regular new request
on 404, so the message is stored exactly once and always gets answered.

## Frontend rendering

On a successful append, `chat_module.js` rebinds the live stream's block object
(`activeStreamBlk`, same object identity as the `blk` passed to
`handleSSEEvent`) to a fresh assistant block — the agent's reaction renders
**below** the injected user message instead of into the previous block (same
in-place rebind pattern as the `continuation` event handler).

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

- Text only — multimodal appends (files) start a new request instead.
- A message arriving in the microseconds between the pre-final drain and
  request unregistration is flushed to the session (persisted, answered next
  run) rather than answered in the same run.
- If a tool replaced the history via `set_compacted_messages` in the same
  instant the run finalizes, the late-flush can lose against the compacted
  snapshot (edge case, message still acknowledged by the API).
