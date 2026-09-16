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
  total the filters match on each tab. Filters: agent, session, request, plus type (turns) and provider and
  direction (requests). A request takes the calls under it along -- tool calls (`<id>_001`) and sub-agents
  (`<id>_sub_...`), which the request column shows by what follows the id; a session filter shows that session's
  own calls, a sub-agent's are in its own session. A click on a column head sorts the entries loaded by it.
- With the auto refresh off -- as the panel starts -- the lists hold still at the last refresh: a page more,
  another tab or other filters show nothing captured since, the refresh button brings it. After a prune or a
  clear the lists show the database as it is then.
- A row opens its entry in a drawer: the messages as cards (text as text, JSON tool results and tool arguments as
  trees, every other field by name), the LLM response, or the request payload, response, usage and error. Each
  part copies as JSON; the arrows step to the next newer or older entry; the filter buttons narrow the lists to
  the entry's session or request.
- The menu prunes the database and compacts the file, or clears all captured data -- cost history included,
  which `writer-costs` reads. A prune reports what it did in a dialog; after a clear, pruning gives the freed
  space back to the disk.

## API

All under `/plugins/message_debugger/`:

| Route | |
|---|---|
| `GET turns` | `agent_name`, `session_id`, `request_id` (with the calls under it), `snapshot_type`, `max_id`, `limit` (≤ 500), `offset` → `{total, turns, as_of_id}`; a row carries `usage_json` (the usage of its LLM response), not the messages |
| `GET turns/{turn_id}` | the whole turn |
| `GET llm-requests` | `agent_name`, `session_id`, `request_id` (with the calls under it), `direction`, `provider`, `max_id`, `limit`, `offset` → `{total, requests, as_of_id}`, without payload and response |
| `GET llm-requests/{entry_id}` | the whole log entry |
| `GET stats` | counts, agents, providers, errors, database size |
| `POST prune?vacuum=true` | retention down to ~75 %, then VACUUM when the file has free pages (can take minutes; a VACUUM failure is reported, not raised) |
| `DELETE clear` | deletes everything |
| `GET /` | the panel |

`as_of_id` is the newest id a list was answered as of; passed back as `max_id`, the list and its total leave out
what was captured since. Ids only grow, a clear included.

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
