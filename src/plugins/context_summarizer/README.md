# Context Summarizer

Shrinks a conversation by having an LLM summarize its older messages. It runs
automatically as a `pre_llm_call` hook when the context grows past a share of
the answering model's window, and an agent (or a person, through the slash
commands) can trigger it by tool. System messages and the most recent messages
stay untouched; assistant messages with `tool_calls` stay paired with their
tool results.

## How a run works

1. **Trigger.** The prompt tokens — the larger of the context usage tracker's
   last measured count and an estimate of the messages — are compared with
   `summarization_trigger_percentage` of the answering model's context window.
   `max_messages` (optionally per agent via `hook_config`) triggers on the
   message count instead; either condition is enough. A manual run skips the
   check.
2. **Rate limit.** An automatic run within `min_time_between_summarizations`
   seconds of the last run in the same session that called the summarizing
   LLM is skipped — applied, rejected or failed alike, since each spent LLM
   calls. A run that sends no LLM call — skipped at step 3, cancelled before
   the first chunk, or without a summarizing LLM — starts no pause.
3. **Split.** Messages become system messages, the last
   `preserve_recent_count` messages and the older ones. Fewer than two older
   messages, or older messages too small to ever reach
   `min_summary_reduction`, end the run as *skipped*.
4. **Summarize.** The older messages are summarized in chunks of
   `summarization_chunk_size` (at most `max_chunks` in parallel; the chunk size
   grows instead) with the `llm_profile` and `summary_prompt_template`. Each
   summary is marked with `summary_marker_format`.
5. **Check.** If the new context is not at least `min_summary_reduction`
   smaller, the original messages stay and the run ends as *rejected*.
   Otherwise the run is *applied*: the messages are replaced, provider
   reasoning artifacts over the replaced span are invalidated, and the usage
   tracker's figures for the session are marked stale.

Progress goes to the status bus (START, PROGRESS, END) under the run's own
request id.

## Tools and commands

| Tool | Command | What it does |
|---|---|---|
| `context_summarizer_summarize` | `/summarize [reason]` | Summarizes now, regardless of the threshold. Optional `chunk_size` and `preserve_recent` override the configuration for this run. |
| `context_summarizer_check_stats` | `/stats` | Message count, tokens (measured and estimated, tool definitions included), context window, utilization and whether a summarization is recommended. |

An agent gets the tools through its allowlist (`context_summarizer/*`); the
commands follow the tools.

## Configuration

Defaults are in `schema.yaml`; `config/plugins.yaml` overrides them.

| Key | Default | Meaning |
|---|---|---|
| `summarization_trigger_percentage` | 0.60 | share of the context window that triggers a run |
| `max_messages` | 0 | message count that triggers a run (0: off) |
| `summarization_chunk_size` | 10 | older messages per summary |
| `max_chunks` | 10 | parallel summaries per run (0: no limit) |
| `preserve_recent_count` | 10 | recent messages never summarized |
| `preserve_system_messages` | true | keep system messages |
| `llm_profile` | turbo | profile of the summarizing LLM |
| `summary_prompt_template` | see schema | prompt, with `{messages}`; empty is refused at start |
| `min_summary_reduction` | 0.3 | reduction a run needs to be applied |
| `summary_marker_format` | `[Summary of {count} messages from {start_time} to {end_time}]` | heading of a summary |
| `store_original_metadata` | true | keep the original messages in the summary's metadata |
| `min_time_between_summarizations` | 200 | seconds between automatic runs per session |
| `max_tracked_sessions` | 200 | sessions remembered for the rate limit |

The hook runs after `context_engineer`, which externalizes and archives first.

## The panel

**Context Summarizer** in the panel launcher (category *context*), or from a
session's info in the chat. It shows the runs of the session open in the chat
— or of the session a link names (`?session_id=`) — or, with *All sessions*,
of every session.

- **Figures:** runs (applied, rejected, skipped), tokens saved, messages
  summarized and the average reduction. Tokens, messages and reduction count
  applied runs only.
- **Runs:** newest first, the newest 100, with their status, why a run was not
  applied, messages and tokens before and after, tokens saved and the
  reduction; with all sessions, the session of each run.
- **A run** opens in a drawer (click, or Enter on the row): its figures, the
  summaries it wrote and the messages they replaced.
- **Clear** (trash icon) forgets every run of every session, after asking.

It refreshes every five seconds. The runs live in the memory of the process
the hook runs in — at most the last 1000 — and are gone after a restart.

## Endpoints

Under `/plugins/<instance>/`:

| Method | Path | Answer |
|---|---|---|
| GET | `/` | the panel |
| GET | `/history?session_id=&limit=` | `{events, stats}`: the newest `limit` runs (1–1000, default 100; 422 outside) of the session, of all without it, newest first and without their messages; `stats` counts every run asked for |
| GET | `/events/{id}` | one run with `before_messages` and `after_messages`; 404 when it is no longer in the history |
| POST | `/clear` | forgets every run |

## Tests

```bash
pytest src/plugins/context_summarizer/tests -q
```

`test_plugin_context_summarizer_panel.py` drives the panel in a real Chromium
browser against the real plugin, seeded through the hook with the summarizing
LLM stubbed; it is skipped without an installed Chromium-based browser.
`test_plugin_context_summarizer_web_ui.py` covers the endpoints and the
history.
