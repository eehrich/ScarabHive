# Context Usage Tracker Plugin

Records tokens, cache, cost and latency of every LLM call, and shows them per call, agent, model and session in the
**Context & Cost Usage** panel.

## What it records

A `post_llm_call` hook (`track_usage`) records one snapshot per call: agent, session, request id, model, prompt,
output and total tokens, cached (read) and cache-write tokens, tool-definition tokens, message count, the context
window and its share in use, latency, and the cost -- billed by the provider, or estimated from `llm_pricing.yaml`
(`cost_is_estimate`), or none.

The context window comes from the LLM instance of the call (so a profile chosen in the chat counts), else from the
agent's configured profile.

## Storage

SQLite, `usage.db` in a directory named after `storage_path` (default `data/context_usage_tracker/usage.db`), opened
on first use. Every record is one transaction: the snapshot and the agent's all-time totals, added up in SQL, so two
processes recording at once add up. A legacy `context_usage_tracker.json` next to it is imported once.

| Config key | Default | Meaning |
|---|---|---|
| `storage_path` | `data/context_usage_tracker.json` | where the store lives (see above) |
| `max_history` | `1000` | how many of the newest calls the statistics and the panel look at; the database keeps more |

## Session scope

A session's calls include its sub-agents at any depth: their request ids carry the parent's as a prefix
(`<request>_sub_<id>`), so the session's own request ids expand the filter (`UsageDatabase.recent_snapshots`).

## The panel

In the launcher under **Context**; a session's info button offers it too, opened on that session.

- **This session / All sessions** in the toolbar; this session is the one open in the chat, or the one the link
  named. With no session open it says so rather than counting every session. Auto refresh runs every 5 s.
- Figures: total cost (estimates marked `~`, billed and estimated calls counted apart), calls, output and prompt
  tokens, cached tokens and their share of the prompt, cache writes, and the context in use now (`stale` after a
  context optimisation).
- **Overview**: tokens and cost of the newest 60 calls (Chart.js from `static/vendor/chartjs`), per agent if chosen,
  and current, min, max and average context. **Agents**: sums per agent. **LLMs**: sums per model with average and
  p95 latency. **Calls**: the newest 50 to 500, per agent; a sub-agent's call is marked `↳`, a row opens the call
  in a drawer. A click on a column head sorts a table by it; the order holds through the auto refresh.
- Costs are shown in cents. Clearing asks first and deletes every call and all agent totals, of every session.

## API

Under `/plugins/context_usage_tracker/`: `GET usage?session_id=` (latest snapshot, agent totals, statistics),
`GET history?last_n=&session_id=&agent_id=`, `POST clear`, `GET /` (the panel).

Who sees what (`agent_system/auth/session_access.py`, shared with the context summarizer and engineer): the tracker
records calls by session id and keeps no owner, so a session is held against its owner
as the app knows it -- a run of this process that has the session names its user (its first turn is not on disk
yet), otherwise the session store answers under the viewer (the signed-in user, else `anonymous`, the rule of
`/sessions`). A user gets her own sessions' figures; another user's session answers empty. Every session at once
(no `session_id`, the panel's **All sessions**) and `POST clear` are for an admin; anyone else gets 403. With
authentication off, one person uses the instance and sees everything. Without a session store in the process the
answer is 503, not an empty page.

Known gap: the rows carry no owner. An id that another user's DELETED session had, taken for a new session of one's
own, brings that user's old calls along. Closing it belongs to the per-user separation of plugin data
(`docs/multiuser_datentrennung_konzept.md`: the data in the user's own path, not a `WHERE` on an owner column).
