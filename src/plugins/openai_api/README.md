# openai_api

Every ScarabHive agent as a model behind the OpenAI **Responses** and **Chat
Completions** API. Any OpenAI client can then talk to the agents — the `openai`
SDK, Open WebUI, LibreChat, n8n, IDE plugins — with the agent's own prompt,
tools, sub-agents and memory doing the work.

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/plugins/openai_api/v1", api_key="<your ScarabHive API key>")

first = client.responses.create(model="chat_agent", input="Remember the code word QUARTZ-17.")
second = client.responses.create(model="chat_agent", previous_response_id=first.id,
                                 input="Which code word did I give you?")
print(second.output_text)

for chunk in client.chat.completions.create(model="chat_agent", stream=True,
                                            messages=[{"role": "user", "content": "Hello!"}]):
    print(chunk.choices[0].delta.content or "", end="")
```

## Setup

```yaml
plugins:
  servers:
    openai_api:
      type: openai_api
      enabled: true
      # agents: ["chat_agent", "research_*"]   # default: the agents the web UI lists
      # blocked_agents: ["sysadmin_*"]
      # responses_db: data/openai_api/responses.db
```

The key is the user's **API key**: `POST /auth/api-key` (logged in) or
`agent-cli users generate-api-key <user>`.
OpenAI clients send it as `Authorization: Bearer <key>`; `X-API-Key` works too,
and so does an access token. The plugin declares `accept_api_keys` in its
security config — every other plugin route stays tokens-only. The calls run as
that user: their sessions, their permissions.

## Endpoints

Base URL: `/plugins/<instance>/v1`.

| Endpoint | What it does |
|---|---|
| `GET /models`, `GET /models/{id}` | The agents offered: the ones the web UI lists (`metadata.visibility` ui/both), narrowed by `agents` / `blocked_agents`. |
| `POST /responses` | One agent turn. The conversation is a stored session of the user (it shows up in the web UI). `previous_response_id` continues it; `store: false` runs on a throwaway session. `stream: true` sends the Responses events (`response.created` … `response.output_text.delta` … `response.completed`, or `response.failed`). |
| `POST /chat/completions` | One agent turn, stateless as at OpenAI: the earlier turns come with the call and no session is kept. `stream: true` sends `chat.completion.chunk`s and `[DONE]`; `stream_options.include_usage` adds the usage chunk. |

Requests are JSON (`Content-Type: application/json`, else 415) — a form or text
post is what a page of another origin could send with a logged-in user's cookie.
Errors come as OpenAI errors (`{"error": {message, type, param, code}}`), also
for a malformed request, and a streamed request is refused with the same
status as a JSON one, before its stream starts (409, 403, 404) —
`response.failed` only ends a turn that failed after it started, and an
`error` event with the code `conflict` one refused after it started. Not in that
shape: a missing or wrong key, which the app's auth layer refuses before the
request reaches the plugin (401, `{"detail": "Authentication required", …}`);
the `openai` SDK raises its `AuthenticationError` for it all the same.

### How a request becomes a turn

- The last user message is the turn; the user/assistant messages before it are
  the earlier turns (Chat Completions: all of them; Responses: those in `input`,
  on top of the stored conversation).
- `system`/`developer` messages and the Responses `instructions` reach the agent
  in front of the turn, under `Instructions from the client application:` —
  the agent keeps its own system prompt. Unlike at OpenAI, instructions stay in
  a stored conversation (they are part of its turn); the same instructions sent
  again on a later turn (the Agents SDK sends them every time) are not added
  again, changed ones are.
- Only text. Image parts, tool messages and client `tools` are refused with a
  400 naming the field (the agent calls its own tools); `n` other than 1 and
  `background: true` too. Sampling parameters (`temperature`, `max_tokens`, …)
  are ignored: the agent's LLM profile decides.
- The answer is the agent's final message as it wrote it (markdown), not the
  web UI's HTML: the JSON answer of both APIs, and the final texts of a
  Responses stream (`response.output_text.done`, `content_part.done`,
  `output_item.done`, `response.completed`).
- A stream's deltas are the work as it happens: what every LLM call of the turn
  writes, the calls set apart by a blank line — the steps of a multi-step run
  (a note before a tool call, say), and a call asked again after it failed
  (what it wrote before it broke off went out already). So the deltas of a
  multi-step turn are more than its final message. A Chat Completions stream
  has no final text: its deltas are all a client gets. What of a call's content
  never came as a delta follows it: the rest a client salvaged at the end of a
  stream, or — when the turn's last call runs on a model that does not stream
  (a model that never does, or a fallback) — its whole final message, as one
  delta at the end. A structured turn streams differently, see below.
- `usage` sums the LLM calls of the turn (all steps).

### Structured output

```python
class Trip(BaseModel):
    city: str
    days: int

trip = client.chat.completions.parse(model="chat_agent", response_format=Trip,
                                     messages=[{"role": "user", "content": "Plan a weekend trip."}])
print(trip.choices[0].message.parsed)          # Trip(city=..., days=...)
print(client.responses.parse(model="chat_agent", input="Plan one.", text_format=Trip).output_parsed)
```

- Chat Completions `response_format` and Responses `text.format` take
  `json_schema` (`name` and `schema`; `strict` and `description` optional) and
  `json_object`; `text` is the default and changes nothing. The rest of `text`
  (`verbosity`) is ignored like a sampling parameter.
- The format holds for the agent's **final** answer — the step without tool
  calls. The steps before it call the agent's tools as always.
- Where the agent's model takes structured output — its model entry declares
  `capabilities.structured_output` (for a schema and for plain JSON mode;
  `json_mode` is not read), and its route has the field
  (`agent_system/llm/structured_output.py`) — the format goes to the provider as
  its own field, on every call of the turn: the same field on every call keeps
  the provider's cached prefix. Where it does not, the format is described to
  the model in the conversation. Either way the answer is checked against the
  schema, sent back once with what is wrong with it, and a turn whose answer
  still does not match fails: `500 server_error`, "Structured output: the final
  answer does not match the requested format after one correction: …", `code:
  structured_output_invalid`, with `x-should-retry: false` — the run had its
  correction round, and the `openai` SDK would otherwise buy two more whole
  runs for the same verdict (a stream ends on `response.failed`, or on an error
  chunk with that code). An agent that cannot hold its answer to a format at all
  — a state machine behind the `stategraph` facade — is refused before it runs:
  `400`, `code: unsupported_parameter`.
- The answer is the JSON as the agent wrote it — a Markdown fence around the
  whole answer taken off, nothing else — so the SDK's `parse()` helpers work.
- **A strict subset of JSON Schema**, as OpenAI's strict mode has one. Allowed:
  `type`, `properties`, `required`, `additionalProperties`, `items`,
  `prefixItems`, `enum`, `const`, `anyOf`, `$defs` (at the root), `$ref` (only
  `#` and `#/$defs/<name>`), `description`, `title`, `pattern`, `format` (not
  checked), `minimum`/`maximum`/`exclusiveMinimum`/`exclusiveMaximum`,
  `multipleOf` (decided exactly: 2.3 is a multiple of 0.1), `minLength`/
  `maxLength`, `minItems`/`maxItems`; `$schema` only at the root, 2020-12,
  2019-09 or draft-07 (all checked as 2020-12), and a root `definitions`
  (draft-07, what zod-to-json-schema and openai-node's `zodResponseFormat`
  write) is taken as `$defs`, its `#/definitions/…` references with it — both
  together are refused. `default`, `examples`, `$comment`, `deprecated`,
  `readOnly`, `writeOnly` and `x-*` are taken out. Limits: 10 levels, 5000
  properties, 1000 enum/const values, 100 000 characters, patterns of 1000
  characters (what `regex` knows, `\p{L}` included) whose counted repeats stay
  small — counted by their minimum, which is what `regex` writes out:
  `(a{120}){120}` yes, one level more no, `[\s\S]{0,30000}` yes; 200 000 units
  for all patterns of a schema together; no inline flags, comments or recursion
  — patterns are ECMA-262; no
  `anyOf`/`$ref` fan-out over 1000 subschemas or cycle on one value. pydantic's
  and the `openai` SDK's schemas of plain models are inside it — fields, nested
  and recursive models, lists, `Optional`, unions (`anyOf`); sets
  (`uniqueItems`), constrained dicts (`minProperties`) and discriminated unions
  (`oneOf`) are not. Anything outside is a `400 invalid_value` on `…schema`
  naming the keyword and where it stands.
- **The checks run in a process of their own.** The schema is a client's input,
  and jsonschema and regex are not built to be bounded: building the format
  (the subset, the meta-schema) and checking the answer run in a schema worker
  (`agent_system/llm/schema_worker.py`), at most two at once, each request under
  a deadline (5 s) past which the worker is killed — by the handle that started
  it — and the request fails closed (a schema: `400`; an answer: the turn fails
  as above, without a correction round); the next request gets a fresh worker.
  A `pattern` is matched with a time limit of its own (0.1 s per match, 0.5 s
  per answer, `regex` instead of `re`): running out is a mismatch. An answer's
  whole check has 2 s (`anyOf` decides each branch once per value, so a
  recursive union stays linear); past them it fails closed. On Linux a
  worker's address space is limited to 1 GB; macOS takes the limit without
  enforcing it, and there only the deadline bounds a worker's memory. A worker
  whose memory grew past 256 MB is replaced after its answer; one that runs a
  request for 30 s ends itself (for a server that was killed and cannot kill
  it), and an idle one exits after 5 minutes. More than two checks at once
  wait, 30 s at most (then `503`, the checker is busy) — and of one user's
  requests only one runs at a time, the rest wait in that user's own queue
  (same 30 s): a user's flood holds one worker, not both. A checker that is
  busy or broke (crashed, did not start) gives no verdict on the answer: `503`,
  `code: structured_output_unavailable`, retryable (no `x-should-retry`).
- Streamed, a structured turn sends its answer as **one** delta once it has been
  checked, and nothing of the steps before it: the joined deltas are the JSON.
  Meanwhile the stream sends an SSE comment (`: keep-alive`) every 15 s, so a
  proxy does not drop a long run.
- A format that cannot be asked for is a `400 invalid_request_error` before the
  agent runs, with the parameter path: an unknown `type`, a schema outside the
  subset or a `name` outside `[A-Za-z0-9_-]{1,64}` (`code: invalid_value`), a
  missing `name` or `schema` (`missing_required_parameter`), a value of the
  wrong kind — a `strict` that is not a boolean, say (`invalid_type`). Nothing
  outside the schema is ever fetched.

### Conversations

- A response id belongs to a stored conversation (`data/openai_api/responses.db`
  maps it to the session). Only the **latest** response of a conversation can
  be continued — a session is one line of turns, and continuing from an older
  one would silently build on turns the client did not mean (409). Another
  user's response id is unknown (404).
- A conversation that is no longer stored — deleted in the web UI, moved away
  by the session archive, a file that does not read any more — is not
  continued: 404 `previous_response_not_found`, "the conversation of this
  response is no longer stored", before the agent runs. Its file is not
  touched.
- One turn per conversation at a time (409 while one runs). The conversation
  is held (session presence, as `/run` and `/events` hold theirs) from before
  it is read until its turn is settled, and a conversation another run has in
  hand is refused with a 409: "the conversation is running in another process"
  (an `agent-cli` run, a session woken by a sub-agent), or "the conversation is
  running right now" (a run in this process, the web chat).
- A stored conversation is an ordinary session of the user, shared with the
  web UI: it is listed there — named by its first user message, the user's own
  text, not the instructions in front of it — and can be continued there. What
  the web chat adds is part of the conversation, and the next API turn (from
  the latest response id) builds on it. An API turn takes the agent's session
  lock when it opens the conversation, and its run lets go of it at its end,
  after its last save (before the session-end hooks). Until then a web-chat or
  `/run` request on the conversation leaves it as it is, and its run is refused
  at once and saves nothing; opened meanwhile, it runs once the lock is free,
  on the conversation as the API turn left it — or as its put back left it.
  A put back only restores what the API turn's run left: a request that has
  run on the conversation since (a stream whose client stopped reading gives
  it time) keeps its turn, and the API turn stays in the conversation with it,
  even when it failed or never reached its client. What the user appended to
  the conversation meanwhile (`POST /sessions/{id}/append`, beside the run or
  handed to it) stays either way: a put back restores the conversation as it
  was before the turn, with those messages after it. An append still saving
  holds the agent's session lock for that moment; the turn's opening and its
  put back wait up to 5 s for it (past that: 409, or the turn left in, as
  beside a run). (An opener that reads the
  conversation back between an API turn's opening and its run would leave the
  turn without the earlier input it came with: the turn is refused then and
  does not run — 409, or in a stream an `error` event with the code
  `conflict`.)
- A turn is kept only once it is delivered: its session is saved, then its
  response id recorded. Any other end puts the conversation back to what it
  held before — the agent saves every run, so without that a retry from the
  last response would build on a turn the client never got. That covers a
  turn that fails or is stopped, a client that leaves before the answer went
  out (also after the agent finished), and a save or an id that could not be
  written (the client gets an error). A new conversation is deleted then; a
  continued one is only ever restored, never deleted — its messages and its
  variables (what the turn's tools set). The sub-agents a failed turn started
  stay, as they do in the web UI.
- A client that disconnects stops the agent — a stream hears it at once, a JSON
  answer asks the connection every second, and once more before its turn is
  kept. The run's request is cancelled (its token) before the task, so an agent
  inside a tool call stops too; the conversation stays busy until the run has
  really stopped. At shutdown the plugin waits up to 12 s for runs still
  stopping, so their turns are put back before the process ends; stopped, it
  records nothing more (503, and a turn that ends after it is put back).
- What no server sees: a client that leaves while its answer is being sent.
  Its turn is kept and its id recorded, the client never got the id, and a
  call from the response before it is refused (409, not the latest). Such a
  client starts a new conversation.
- Stateless calls (Chat Completions, `store: false`) leave no session. An agent
  that starts sub-agents makes the sub-agent manager write a parent record
  (`Coordinator Session`); it is deleted after the turn. The sub-agents' own
  sessions stay, hidden below it — as when a session is deleted in the web UI.
  Sub-agents a stateless turn left running in the background are stopped when
  the turn ends: nobody can continue the session to read their answers. For
  the same reason such a session is never woken (`wake_when_done`), and the
  agent is told so when it starts a background job — it has to wait for it
  within the turn.

## Known gaps

- No images, audio or files in the input; no client-side function calling.
- No `background` mode, no `GET /responses/{id}`, no conversation items API.
- Continuing from an older response (a branch) is refused, not forked.
- Runs through this API are not mirrored as web UI jobs: the chat panel does not
  follow them live, and the stored conversation appears after the turn.
- The sub-agents a stateless call starts keep their (hidden) sessions on disk.
- With `session_presence` off nothing holds a conversation: a run of it in
  another process is not refused.
- A Gemini model's thought summaries reach the deltas: its clients stream them
  as text (the web chat shows them while the call runs and replaces them with
  the answer when it ends). A Chat Completions stream keeps them in its text;
  the final texts of a Responses stream and every JSON answer do not have them.
- Structured output: no `refusal` is ever answered — a model's refusal is no
  answer in the format, and the turn fails. The format belongs to one turn, as
  at OpenAI: a stored conversation continued without it runs as text again —
  and the provider's cached prefix changes once there, because the field is
  part of it. The description a model without the field gets is a note of the
  run, like the loop's other notes: it is not stored with the conversation.
