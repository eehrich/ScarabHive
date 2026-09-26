# Message Debugger Plugin

Captures what agents send to their LLM and what comes back, in a SQLite database, and shows it in a panel.

## What it captures

Four hooks write into `data/message_debugger/debugger.db` (or `db_path`), through a background writer thread so a
slow database never stalls an agent:

| Hook | Table | What |
|---|---|---|
| `pre_llm_call` | `turns` (`pre_llm`) | the messages of a step, as the LLM gets them |
| `post_llm_call` | `turns` (`post_llm`) | the messages and the LLM response |
| `pre_llm_request` | `llm_requests` (`request`) | the raw payload sent to the provider |
| `post_llm_response` | `llm_requests` (`response`) | the raw response, usage, duration, error; a failed retry attempt is logged with `finish_reason: retry` and an error starting `[RETRY n/m]`; `served_by` is the backend a gateway routed the call to, as the LLM client read it from the response (OpenRouter clients: e.g. `Google AI Studio`, `Google` for Vertex; streamed answers included) |

A turn stores every field of every message (role, content, tool calls, `served_by`, `reasoning_details`, ...), so a
field added to `ChatMessage` shows up without changes here. `content` and `tool_calls` are kept whole; any other
string longer than `max_field_chars` is stored as a preview that names its full length, because every snapshot
repeats the whole history. The LLM response of a post-LLM turn is stored the same way, but every long string in
it is cut, its text included. The snapshot adds `index`, `content_length`, `estimated_tokens` (with
`include_token_estimates`) and `tool_call_count`/`is_tool_result` (with `include_tool_calls`).

## Retention

`max_db_size_mb` caps the data size: above ~90 % the oldest request payloads are stripped (their cost columns
stay) and the oldest turns deleted, down to ~75 %. The file plateaus and reuses freed pages; only the panel's
prune action gives them back to the disk (VACUUM). `0` disables retention.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `capture_enabled` | `true` | all capture on or off |
| `capture_pre_llm`, `capture_post_llm` | `true` | the two turn snapshots |
| `capture_llm_requests` | `true` | the raw requests and responses |
| `include_tool_calls` | `true` | tool call details in the snapshot |
| `include_token_estimates` | `true` | a token estimate per message |
| `max_field_chars` | `500` | preview length of long non-content fields; `0` keeps everything |
| `db_path` | `""` | database file; empty means `data/message_debugger/debugger.db` |
| `max_db_size_mb` | `5120` | retention cap, see above |
| `capture_queue_max` | `2000` | bound of the write queue; on overflow the newest capture is dropped and logged |

## The panel

In the launcher under **Debug**. The chat offers it on a response's request ID and on a session: opened that way,
both lists start filtered to that request or session.

- **Turns** and **LLM requests**, newest first, 50 at a time (at most the newest 500 a filter matches), with the
  total the filters match on each tab. Filters: agent, session, request, for an admin the user, plus type
  (turns) and provider and direction (requests). A request takes the calls under it along -- tool calls (`<id>_001`) and sub-agents
  (`<id>_sub_...`), which the request column shows by what follows the id; a session filter shows that session's
  own calls, a sub-agent's are in its own session. A click on a column head sorts the entries loaded by it.
- With the auto refresh off -- as the panel starts -- the lists hold still at the last refresh: a page more,
  another tab or other filters show nothing captured since, the refresh button brings it. After a prune or a
  clear the lists show the database as it is then.
- A row opens its entry in a drawer: the messages as cards (text as text, JSON tool results and tool arguments as
  trees, every other field by name), the LLM response, or the request payload, response, usage and error. Each
  part copies as JSON; the arrows step to the next newer or older entry; the filter buttons narrow the lists to
  the entry's session or request.
- The menu -- an admin's -- prunes the database and compacts the file, or clears all captured data -- cost history included,
  which `writer-costs` reads. A prune reports what it did in a dialog; after a clear, pruning gives the freed
  space back to the disk.

## API

All under `/plugins/message_debugger/`:

| Route | |
|---|---|
| `GET turns` | `agent_name`, `session_id`, `request_id` (with the calls under it), `snapshot_type`, `user_id` (admins), `max_id`, `limit` (≤ 500), `offset` → `{total, turns, as_of_id}`; a row carries `usage_json` (the usage of its LLM response), not the messages |
| `GET turns/{turn_id}` | the whole turn |
| `GET llm-requests` | `agent_name`, `session_id`, `request_id` (with the calls under it), `direction`, `provider`, `user_id` (admins), `max_id`, `limit`, `offset` → `{total, requests, as_of_id}`, without payload and response |
| `GET llm-requests/{entry_id}` | the whole log entry |
| `GET stats` | `user_id` (admins) → counts, agents, providers, errors; for admins the size of the whole file |
| `POST prune?vacuum=true` | admins: retention down to ~75 %, then VACUUM when the file has free pages (can take minutes; a VACUUM failure is reported, not raised) |
| `DELETE clear` | admins: deletes everything |
| `GET /` | the panel |

`as_of_id` is the newest id a list was answered as of; passed back as `max_id`, the list and its total leave out
what was captured since. Ids only grow, a clear included.

### Who reads what

Each row carries the user whose call it was (`user_id`): the user the run's session was opened for, else the
user its request was registered under (`HookContext.user_id`). A call no run names a user for -- a decision or
TTS call outside a run -- and every row captured before the column are nobody's.

- **A user** reads their own rows: the lists, the entries by id, the statistics (without the database size:
  the file is everyone's rows). Another user's entry answers 404, like one that does not exist; naming another
  user (`user_id=`) is 403.
- **An admin** reads everyone's, nobody's rows included, and may pass `user_id` to the lists and the statistics
  to see one user's. With authentication off everyone is an admin.
- **The identities without an account** -- `anonymous` (nobody signed in; runs whose owner nobody knew were
  recorded under it too) and `cli_user` (agent-cli's default) -- get 403.
- **Clearing and pruning** act on everyone's rows and stay an admin's.

The database takes `owner` as a required keyword on every read -- `EVERYONE`, a name (every row under it: an
admin asking for one user), or an `Account(name, since_ms)`, what a signed-in user reads with -- and refuses a
read that names nobody, so a forgotten owner fails instead of answering with everybody's rows. A new read for
users passes an `Account`: a bare name would hand a new account a deleted namesake's rows. A user's reads are
served by one index alone (`idx_<table>_user`; the other columns go in as `+column`, in a select list too), and it
holds every column a user filters and counts by: `user_id` and `error` sit behind the payloads, and reading
either from a row walks every payload -- the statistics the panel asks for every few seconds included. Only a
list page reads its rows from the table. The index is built at the first start after the column came -- once,
seconds on a multi-GB file, reading no payload (the rows from before do not hold the column).

The writer's cost capture (`writer_jobs/cost_capture.py`, `writer-costs`) reads the file directly and is not
affected: the owner is a column, not a file per user.

Known gap: row ids count up over everyone's rows. A user sees from the gaps between their own ids how many calls
others made in between, and roughly when -- never what. Hiding it would take ids a user cannot read (encrypted per
request, in every endpoint and the panel); left as is while an instance has no strangers on it.

Known gap: a row's owner is a name. `agent-cli --session-user NAME` records under a name that need not be an
account, and registration is open: whoever registers NAME later reads what is captured under it from then on.
The name space is the CLI's and the account store's to separate (the same holds for `data/sessions/NAME`).

## Files

```
message_debugger/
├── plugin.py          # hybrid plugin: hooks + web, static assets
├── hooks.py           # capture
├── database.py        # SQLite storage, retention, write queue
├── web_endpoints.py   # API and panel route
├── schema.yaml        # hooks, config, panel catalogue entry, endpoints
├── templates/panel.html
├── static/panel.js, panel.css
└── tests/             # unit tests; test_plugin_message_debugger_panel.py drives the panel in a browser
```
